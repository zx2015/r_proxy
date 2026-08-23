"""配置写回：出口增删改、设置、规则编辑、备份与审计。

对应设计：docs/design/DD_WEB.md §6，需求：WEBUI_SPEC.md §6。

这一层的验证有三条主线，缺一条都不够：

1. **磁盘**：写成功后文件是不是新内容、注释还在不在；被拒绝时是不是一个字节都
   没动（「拒绝了但已经改了」比直接写坏更难发现）
2. **内存**：写完立刻生效，下一个请求就按新配置走
3. **审计**：改了什么有记录，且记录里不能有明文凭据
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import tomllib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from r_proxy.app import Application
from r_proxy.storage.reader import ReadOnlyPool
from r_proxy.storage.rules_store import RulesStore
from r_proxy.web import config_writer
from r_proxy.web.app import create_app
from r_proxy.web.config_writer import ConfigWriter

CONFIG = """\
# r-proxy 测试配置
# 这一行注释必须活到最后

[listen]
host = "127.0.0.1"
port = 0

[webui]
enabled = false

[database]
state_path = "{state}"
logs_path = "{logs}"
rules_path = "{rules}"
backup_keep = {backup_keep}

[routing]
connect_timeout = 3.0

# 上游代理甲
[[upstreams]]
name = "proxy-a"
type = "http"
address = "127.0.0.1:1"
priority = 10

[upstreams.auth]
username = "squid-user"
password = "s3cr3t-pass"

# 兜底直连
[[upstreams]]
name = "direct"
type = "direct"
priority = 100
"""

RULES = (("*.pinned.test", "proxy-a"),)


def write_files(tmp_path: Path, *, backup_keep: int = 10) -> Path:
    store = RulesStore(tmp_path / "rules.db")
    store.ensure_schema()
    store.replace(list(RULES), expected_revision=0, now_unix=0)
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(
            state=tmp_path / "state.db",
            logs=tmp_path / "logs.db",
            rules=tmp_path / "rules.db",
            backup_keep=backup_keep,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[Application]:
    instance = Application(config_path=write_files(tmp_path))
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
async def client(app: Application) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), client=("127.0.0.1", 12345)),
        base_url="http://webui.test",
    ) as c:
        yield c


def version(app: Application) -> str:
    return app.snapshot.config_version


def config_text(app: Application) -> str:
    return app.snapshot.source_path.read_text(encoding="utf-8")


def upstreams_in_file(app: Application) -> dict[str, dict[str, object]]:
    data = tomllib.loads(config_text(app))
    return {u["name"]: u for u in data["upstreams"]}


async def rows_of(pool: ReadOnlyPool, sql: str, *, timeout: float = 5.0) -> list[sqlite3.Row]:
    """等写者线程把这一批落盘。批量落盘有时间窗，不能立刻查。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rows = await asyncio.to_thread(pool.query, sql)
        if rows:
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(f"未等到落盘：{sql}")


async def audit_rows(app: Application) -> list[sqlite3.Row]:
    return await rows_of(app.storage.logs_reader, "SELECT * FROM config_audit ORDER BY id")


async def _with_token(tmp_path: Path, token: str) -> Application:
    path = write_files(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "enabled = false", f'enabled = false\nauth_token = "{token}"', 1
        ),
        encoding="utf-8",
    )
    instance = Application(config_path=path)
    await instance.start()
    return instance


def _client_for(
    instance: Application, *, token: str | None = None, ip: str = "127.0.0.1"
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(instance), client=(ip, 12345)),
        base_url="http://webui.test",
        headers={"Authorization": f"Bearer {token}"} if token else None,
    )


async def wait_until(predicate: Callable[[], bool], *, what: str) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


class TestCreateUpstream:
    async def test_a_new_upstream_becomes_effective(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        body = {
            "config_version": version(app),
            "name": "proxy-b",
            "type": "http",
            "address": "127.0.0.1:3",
            "priority": 20,
        }
        response = await client.post("/api/upstreams", json=body)

        assert response.status_code == 201
        assert response.json()["config_version"] != body["config_version"]
        # 写回即热重载：不需要重启，也不需要再发一次 reload。
        assert app.snapshot.upstream("proxy-b") is not None
        assert upstreams_in_file(app)["proxy-b"]["address"] == "127.0.0.1:3"

    async def test_the_handwritten_comments_survive(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.post(
            "/api/upstreams",
            json={
                "config_version": version(app),
                "name": "proxy-b",
                "type": "direct",
                "priority": 20,
            },
        )
        text = config_text(app)
        assert "# r-proxy 测试配置" in text
        assert "# 上游代理甲" in text
        assert "# 兜底直连" in text

    async def test_a_duplicate_name_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = config_text(app)
        response = await client.post(
            "/api/upstreams",
            json={"config_version": version(app), "name": "proxy-a", "type": "direct"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "UPSTREAM_EXISTS"
        assert config_text(app) == before

    async def test_a_hostile_name_is_refused_before_it_reaches_the_file(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.post(
            "/api/upstreams",
            json={"config_version": version(app), "name": 'x"\nport = 9', "type": "direct"},
        )
        assert response.status_code == 422

    async def test_an_http_upstream_without_an_address_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = config_text(app)
        response = await client.post(
            "/api/upstreams",
            json={"config_version": version(app), "name": "proxy-b", "type": "http"},
        )
        assert response.status_code == 400
        assert config_text(app) == before

    async def test_the_change_is_audited(self, client: httpx.AsyncClient, app: Application) -> None:
        await client.post(
            "/api/upstreams",
            json={"config_version": version(app), "name": "proxy-b", "type": "direct"},
        )
        rows = await audit_rows(app)
        assert [r["action"] for r in rows] == ["upstream.create"]
        assert rows[0]["target"] == "proxy-b"
        assert rows[0]["actor"] == "127.0.0.1"
        assert rows[0]["version_after"] == version(app)


class TestUpdateUpstream:
    async def test_the_priority_change_is_effective(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/upstreams/proxy-a", json={"config_version": version(app), "priority": 50}
        )
        assert response.status_code == 200
        cfg = app.snapshot.upstream("proxy-a")
        assert cfg is not None and cfg.priority == 50

    async def test_the_credentials_are_not_dropped(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """「改一下优先级」不该顺手把上级代理的凭据抹掉。"""
        await client.put(
            "/api/upstreams/proxy-a", json={"config_version": version(app), "priority": 50}
        )
        assert upstreams_in_file(app)["proxy-a"]["auth"] == {
            "username": "squid-user",
            "password": "s3cr3t-pass",
        }
        cfg = app.snapshot.upstream("proxy-a")
        assert cfg is not None and cfg.auth is not None

    async def test_disabling_it_removes_it_from_the_chain(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/upstreams/proxy-a", json={"config_version": version(app), "enabled": False}
        )
        assert response.status_code == 200
        cfg = app.snapshot.upstream("proxy-a")
        assert cfg is not None and cfg.enabled is False

    async def test_disabling_every_upstream_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """M4-12：候选链会空掉，所有请求都无路可走。文件必须保持原样。"""
        await client.put(
            "/api/upstreams/proxy-a", json={"config_version": version(app), "enabled": False}
        )
        before = config_text(app)
        response = await client.put(
            "/api/upstreams/direct", json={"config_version": version(app), "enabled": False}
        )
        assert response.status_code == 400
        codes = [d["code"] for d in response.json()["error"]["details"]]
        assert "E_NO_UPSTREAM" in codes
        assert config_text(app) == before

    async def test_an_empty_body_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put("/api/upstreams/proxy-a", json={"config_version": version(app)})
        assert response.status_code == 400

    async def test_an_unknown_upstream_is_a_404(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/upstreams/ghost", json={"config_version": version(app), "priority": 5}
        )
        assert response.status_code == 404

    async def test_a_stale_version_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """M4-13：并发写保护。第一次写完之后旧版本号就作废了。"""
        stale = version(app)
        await client.put("/api/upstreams/proxy-a", json={"config_version": stale, "priority": 30})

        response = await client.put(
            "/api/upstreams/proxy-a", json={"config_version": stale, "priority": 40}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VERSION_CONFLICT"
        cfg = app.snapshot.upstream("proxy-a")
        assert cfg is not None and cfg.priority == 30

    async def test_an_external_edit_is_detected(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """M4-13 的要点：版本比对必须重读磁盘。

        比对内存里缓存的版本号时，管理员用编辑器做的修改会被静默覆盖——内存值
        与客户端提交的值一致，校验会通过。
        """
        current = version(app)
        path = app.snapshot.source_path
        path.write_text(config_text(app) + "\n# 管理员手工加的一行\n", encoding="utf-8")

        response = await client.put(
            "/api/upstreams/proxy-a", json={"config_version": current, "priority": 30}
        )
        assert response.status_code == 409
        assert "# 管理员手工加的一行" in config_text(app)


class TestDeleteUpstream:
    async def test_m4_14_a_referenced_upstream_cannot_be_deleted(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = config_text(app)
        response = await client.delete(f"/api/upstreams/proxy-a?config_version={version(app)}")

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "UPSTREAM_IN_USE"
        # M5-21：回序号与条件而不只回「被引用」，否则用户还要自己逐条翻找。
        assert error["details"] == [{"position": 0, "condition": "*.pinned.test"}]
        # 位置只放 details，不放 message：界面把两者拼成
        # `message（details）`，两边都塞位置就会渲染成
        # 「仍被 rules[0] 引用（rules[0]）」这种结巴。
        assert "rules[0]" not in error["message"]
        assert config_text(app) == before

    async def test_every_referencing_rule_is_listed(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """多处引用必须全列出。

        只报第一处会让用户以为改完那一处就能删，删不掉再回来看第二处——
        而 message 里若只写第一处，看起来更像「唯一的阻碍」。
        """
        await client.put(
            "/api/rules",
            json={
                "revision": 1,
                "rules": [
                    {"condition": "*.pinned.test", "upstream": "proxy-a"},
                    {"condition": "*.second.test", "upstream": "proxy-a"},
                ],
            },
        )
        await wait_until(lambda: len(app.rules.rules) == 2, what="规则热重载")

        response = await client.delete(f"/api/upstreams/proxy-a?config_version={version(app)}")

        assert response.status_code == 409
        error = response.json()["error"]
        assert [d["position"] for d in error["details"]] == [0, 1]
        assert "2 条规则" in error["message"]

    async def test_an_unreferenced_upstream_is_deleted(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.post(
            "/api/upstreams",
            json={"config_version": version(app), "name": "proxy-b", "type": "direct"},
        )
        response = await client.delete(f"/api/upstreams/proxy-b?config_version={version(app)}")

        assert response.status_code == 200
        assert app.snapshot.upstream("proxy-b") is None
        assert "proxy-b" not in upstreams_in_file(app)

    async def test_an_unknown_upstream_is_a_404(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.delete(f"/api/upstreams/ghost?config_version={version(app)}")
        assert response.status_code == 404

    async def test_a_malformed_version_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.delete("/api/upstreams/proxy-a?config_version=nope")
        assert response.status_code == 422


class TestPriorities:
    async def test_groups_are_renumbered_by_step(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/upstreams/priorities",
            json={"config_version": version(app), "groups": [["direct"], ["proxy-a"]]},
        )
        assert response.status_code == 200
        assert {u.name: u.priority for u in app.snapshot.upstreams} == {
            "direct": 10,
            "proxy-a": 20,
        }

    async def test_upstreams_sharing_a_group_share_the_value(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """同优先级即轮询组。拖拽只重排分组，不拆散组内成员。"""
        await client.put(
            "/api/upstreams/priorities",
            json={"config_version": version(app), "groups": [["direct", "proxy-a"]]},
        )
        assert {u.priority for u in app.snapshot.upstreams} == {10}

    async def test_a_missing_upstream_changes_nothing(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """漏掉一个出口就会留着旧数值，与新顺序不自洽。"""
        before = config_text(app)
        response = await client.put(
            "/api/upstreams/priorities",
            json={"config_version": version(app), "groups": [["direct"]]},
        )
        assert response.status_code == 400
        assert "proxy-a" in response.json()["error"]["message"]
        assert config_text(app) == before

    async def test_a_duplicated_upstream_changes_nothing(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = config_text(app)
        response = await client.put(
            "/api/upstreams/priorities",
            json={
                "config_version": version(app),
                "groups": [["direct", "proxy-a"], ["proxy-a"]],
            },
        )
        assert response.status_code == 400
        assert config_text(app) == before

    async def test_an_unknown_upstream_changes_nothing(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = config_text(app)
        response = await client.put(
            "/api/upstreams/priorities",
            json={"config_version": version(app), "groups": [["direct"], ["proxy-a"], ["ghost"]]},
        )
        assert response.status_code == 400
        assert config_text(app) == before


class TestReassignPureFunction:
    """步长自适应不必真造出几百个出口，它是纯函数。"""

    def test_the_step_shrinks_as_groups_grow(self) -> None:
        from r_proxy.web.routers.upstreams import PRIORITY_MAX, _reassign

        for count in (99, 100, 199, 200, 999):
            names = {f"u{i}" for i in range(count)}
            groups = [[f"u{i}"] for i in range(count)]
            assigned = _reassign(groups, known=names)
            assert max(assigned.values()) <= PRIORITY_MAX
            assert len(set(assigned.values())) == count

    def test_too_many_groups_are_refused_instead_of_truncated(self) -> None:
        """静默截断会把两个不同优先级的组压成一个轮询组，悄悄改变路由行为。"""
        from fastapi import HTTPException

        from r_proxy.web.routers.upstreams import _reassign

        names = {f"u{i}" for i in range(1000)}
        groups = [[f"u{i}"] for i in range(1000)]
        with pytest.raises(HTTPException) as caught:
            _reassign(groups, known=names)
        assert caught.value.status_code == 400


class TestSettings:
    async def test_the_etag_carries_the_config_version(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.get("/api/settings")
        assert response.status_code == 200
        assert response.headers["etag"] == f'"{version(app)}"'
        assert response.json()["config_version"] == version(app)

    async def test_the_response_never_carries_the_auth_token(self, tmp_path: Path) -> None:
        """`auth_token` 是访问这套接口的凭据本身，任何接口都不返回它。

        断言的是**值**不出现：字段名 `webui.auth_token` 会作为「需重启」的提示项
        出现在响应里，那是给界面标注用的，与泄露凭据是两回事。
        """
        token = "settings-token-long-enough"
        instance = await _with_token(tmp_path, token)
        try:
            async with _client_for(instance, token=token) as c:
                response = await c.get("/api/settings")
            assert response.status_code == 200
            assert token not in response.text
        finally:
            await instance.stop()

    async def test_a_setting_change_is_effective_and_minimal(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 7.5}
        )
        assert response.status_code == 200
        assert app.snapshot.routing.connect_timeout == 7.5

        text = config_text(app)
        assert "# 这一行注释必须活到最后" in text
        # 只写被改的键：未提交的项不该被默认值显式写进文件。
        assert "read_timeout" not in text

    async def test_nested_settings_are_reachable(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/settings",
            json={
                "config_version": version(app),
                "circuit_breaker_fail_threshold": 7,
                "max_switches_per_host": 4,
            },
        )
        assert response.status_code == 200
        assert app.snapshot.routing.circuit_breaker.fail_threshold == 7
        assert app.snapshot.routing.status_switch_rate_limit.max_switches_per_host == 4

    async def test_m4_12_a_success_status_in_switch_on_status_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """勾上 2xx 会把成功响应当失败，每个正常请求都遍历全部出口。"""
        before = config_text(app)
        response = await client.put(
            "/api/settings",
            json={"config_version": version(app), "switch_on_status": [200, 502]},
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "CONFIG_INVALID"
        assert "E_SWITCH_STATUS_2XX" in [d["code"] for d in error["details"]]
        assert config_text(app) == before
        assert app.snapshot.routing.switch_on_status != {200, 502}

    async def test_an_out_of_range_status_is_refused_by_the_model(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/settings", json={"config_version": version(app), "switch_on_status": [700]}
        )
        assert response.status_code == 422

    async def test_an_if_match_header_is_accepted(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        current = version(app)
        response = await client.put(
            "/api/settings",
            json={"config_version": current, "connect_timeout": 4.0},
            headers={"If-Match": f'"{current}"'},
        )
        assert response.status_code == 200

    async def test_a_disagreeing_if_match_header_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """两处都给了版本号却不一致时，无从判断客户端以哪个为准。"""
        response = await client.put(
            "/api/settings",
            json={"config_version": version(app), "connect_timeout": 4.0},
            headers={"If-Match": '"0000000000000000"'},
        )
        assert response.status_code == 400

    async def test_an_empty_change_set_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put("/api/settings", json={"config_version": version(app)})
        assert response.status_code == 400

    async def test_an_unknown_key_cannot_be_smuggled_in(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """键名由服务端白名单决定：客户端能指定键就能写 `webui.auth_token`。"""
        response = await client.put(
            "/api/settings",
            json={"config_version": version(app), "webui.auth_token": "smuggled"},
        )
        assert response.status_code == 422
        assert "smuggled" not in config_text(app)

    async def test_the_change_is_audited_with_a_diff(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 9.0}
        )
        rows = await audit_rows(app)
        assert rows[0]["action"] == "settings.update"
        assert "connect_timeout" in (rows[0]["diff"] or "")


class TestAuditMasking:
    async def test_m4_16_credentials_never_appear_in_the_audit_diff(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """审计会被 `GET /api/audit` 返回，diff 里留明文等于把凭据存进可查询的表。

        用「恢复一份密码不同的备份」来构造：这是唯一一条能让凭据真的出现在
        diff 两侧的路径。拿改优先级来验是验不到的——那种 diff 里根本没有密码行，
        脱敏被整个删掉也照样通过。
        """
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        backups = (await client.get("/api/config/backups")).json()["backups"]

        app.snapshot.source_path.write_text(
            config_text(app).replace('password = "s3cr3t-pass"', 'password = "rotated-pass"'),
            encoding="utf-8",
        )
        await app.reload()

        response = await client.post(
            "/api/config/restore",
            json={"config_version": version(app), "filename": backups[0]["filename"]},
        )
        assert response.status_code == 200

        diff = "\n".join(row["diff"] or "" for row in await audit_rows(app))
        assert "password" in diff  # 这一行确实进了 diff，否则下面两条断言是空转的
        assert "rotated-pass" not in diff
        assert "s3cr3t-pass" not in diff

    async def test_the_masking_covers_every_secret_shape(self) -> None:
        from r_proxy.web.config_writer import mask_secrets

        diff = (
            '-password = "old"\n'
            '+password = "new"\n'
            '+auth_token = "abcdef"\n'
            '+username = "alice"\n'
            "+priority = 10"
        )
        masked = mask_secrets(diff) or ""
        assert "old" not in masked and "new" not in masked
        assert "abcdef" not in masked and "alice" not in masked
        # 非敏感项要留原值，否则 diff 就没用了。
        assert "priority = 10" in masked

    async def test_the_audit_api_returns_the_records(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 8.0}
        )
        await audit_rows(app)

        body = (await client.get("/api/audit")).json()
        assert [item["action"] for item in body["items"]] == ["settings.update"]
        assert body["has_more"] is False

    async def test_the_audit_api_filters_by_action(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 8.0}
        )
        await audit_rows(app)

        body = (await client.get("/api/audit", params={"action": "upstream.create"})).json()
        assert body["items"] == []


class TestRules:
    """整表读写。验收点 M5-11 ~ M5-17、M5-21。"""

    async def test_the_table_is_returned_with_its_revision(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/rules")).json()
        assert body["rules"] == [{"condition": "*.pinned.test", "upstream": "proxy-a"}]
        assert body["revision"] == 1
        assert body["enabled"] is True

    async def test_the_enabled_flag_mirrors_the_config(self, tmp_path: Path) -> None:
        path = write_files(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n[rules]\nenabled = false\n", encoding="utf-8"
        )
        instance = Application(config_path=path)
        await instance.start()
        try:
            async with _client_for(instance) as c:
                body = (await c.get("/api/rules")).json()
            # 规则仍然读得出来、改得动，只是当前不生效。
            assert body["enabled"] is False
            assert len(body["rules"]) == 1
            assert instance.rules.is_empty
        finally:
            await instance.stop()

    async def test_m5_13_a_valid_save_takes_effect(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/rules",
            json={
                "revision": 1,
                "rules": [
                    {"condition": "api.pinned.test", "upstream": "direct"},
                    {"condition": "*.pinned.test", "upstream": "proxy-a"},
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 2
        await wait_until(
            lambda: [r.raw for r in app.rules.rules] == ["api.pinned.test", "*.pinned.test"],
            what="规则热重载",
        )
        assert [r.position for r in app.rules.rules] == [0, 1]

    async def test_m5_13_positions_are_renumbered_from_zero(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """整表替换：位置由数组下标重算，不保留任何旧编号。"""
        await client.put(
            "/api/rules",
            json={"revision": 1, "rules": [{"condition": "a.test", "upstream": "direct"}]},
        )
        await client.put(
            "/api/rules",
            json={
                "revision": 2,
                "rules": [
                    {"condition": "b.test", "upstream": "direct"},
                    {"condition": "a.test", "upstream": "direct"},
                ],
            },
        )
        rows = app.rules_store.read().rows
        assert [(position, condition) for position, condition, _ in rows] == [
            (0, "b.test"),
            (1, "a.test"),
        ]

    async def test_m5_15_saving_the_same_content_twice_bumps_the_revision_each_time(
        self, client: httpx.AsyncClient
    ) -> None:
        """revision 是并发检测的凭据，不是内容指纹：内容没变也必须递增。"""
        body = {"rules": [{"condition": "*.pinned.test", "upstream": "proxy-a"}]}
        first = await client.put("/api/rules", json={"revision": 1, **body})
        second = await client.put("/api/rules", json={"revision": 2, **body})
        assert (first.json()["revision"], second.json()["revision"]) == (2, 3)

    async def test_m5_11_a_stale_revision_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/rules",
            json={"revision": 0, "rules": [{"condition": "x.test", "upstream": "direct"}]},
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["details"] == {"expected": 0, "actual": 1}
        # 库里一个字节都没动。
        assert [c for _, c, _ in app.rules_store.read().rows] == ["*.pinned.test"]

    async def test_m5_12_an_unknown_upstream_is_refused(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """规则指向不存在的出口时启动校验也会拒绝，两处口径必须一致。"""
        before = app.rules_store.read()
        response = await client.put(
            "/api/rules",
            json={"revision": 1, "rules": [{"condition": "x.test", "upstream": "ghost"}]},
        )
        assert response.status_code == 400
        details = response.json()["error"]["details"]
        assert [d["code"] for d in details] == ["E_RULE_TARGET"]
        assert details[0]["location"] == "rules[0]"
        assert app.rules_store.read() == before

    async def test_m5_12_a_bad_condition_points_at_its_row(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = app.rules_store.read()
        response = await client.put(
            "/api/rules",
            json={
                "revision": 1,
                "rules": [
                    {"condition": "ok.test", "upstream": "direct"},
                    {"condition": "^(unclosed", "upstream": "direct"},
                    {"condition": "bad.test:8443", "upstream": "direct"},
                ],
            },
        )
        assert response.status_code == 400
        details = response.json()["error"]["details"]
        assert [d["location"] for d in details] == ["rules[1]", "rules[2]"]
        assert app.rules_store.read() == before

    async def test_m5_17_warnings_do_not_block_the_save(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        response = await client.put(
            "/api/rules",
            json={
                "revision": 1,
                "rules": [
                    {"condition": "*", "upstream": "direct"},
                    {"condition": "*.pinned.test", "upstream": "proxy-a"},
                    {"condition": "*.pinned.test", "upstream": "direct"},
                ],
            },
        )
        assert response.status_code == 200
        codes = [i["code"] for i in response.json()["issues"]]
        assert codes == ["W_RULE_CATCH_ALL", "W_RULE_SHADOWED", "W_RULE_SHADOWED"]
        assert all(i["level"] == "warning" for i in response.json()["issues"])
        await wait_until(lambda: len(app.rules.rules) == 3, what="规则热重载")

    async def test_m5_17_a_duplicate_warning_points_at_the_later_row(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/rules/validate",
            json={
                "rules": [
                    {"condition": "*.pinned.test", "upstream": "proxy-a"},
                    {"condition": "*.pinned.test", "upstream": "direct"},
                ]
            },
        )
        issues = response.json()["issues"]
        assert [(i["code"], i["location"]) for i in issues] == [("W_RULE_DUPLICATE", "rules[1]")]

    async def test_validation_alone_writes_nothing(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        before = app.rules_store.read()
        response = await client.post(
            "/api/rules/validate",
            json={"rules": [{"condition": "x.test", "upstream": "ghost"}]},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert app.rules_store.read() == before

    async def test_validation_accepts_a_good_table(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/rules/validate",
            json={"rules": [{"condition": "*.x.test", "upstream": "direct"}]},
        )
        assert response.json() == {"ok": True, "issues": [], "rule_count": 1}

    async def test_m5_13_the_save_is_audited_and_backed_up(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.put(
            "/api/rules",
            json={"revision": 1, "rules": [{"condition": "*.pinned.test", "upstream": "direct"}]},
        )
        rows = await audit_rows(app)
        assert rows[0]["action"] == "rules.update"
        assert rows[0]["target"] == "rules"
        # diff 是文本快照的差异：两侧都要能看懂，恢复时也照抄这份格式。
        assert "*.pinned.test\tproxy-a" in rows[0]["diff"]
        assert "*.pinned.test\tdirect" in rows[0]["diff"]

        snapshots = sorted((ConfigWriter(app).backup_dir).glob("rules-*"))
        assert [p.read_text(encoding="utf-8") for p in snapshots] == ["*.pinned.test\tproxy-a\n"]

    async def test_the_rules_snapshot_is_not_offered_for_restore(
        self, client: httpx.AsyncClient
    ) -> None:
        """快照只供人工查阅：把它列进配置备份，一次误点就会用规则文本覆盖
        `config.toml`。"""
        await client.put(
            "/api/rules",
            json={"revision": 1, "rules": [{"condition": "*.pinned.test", "upstream": "direct"}]},
        )
        listed = (await client.get("/api/config/backups")).json()["backups"]
        assert listed == []

    async def test_the_request_carries_no_path_of_any_kind(self, client: httpx.AsyncClient) -> None:
        """M5-12 的安全面：v1 的 `{file_id}` 连同路径穿越一起消失了。

        写死一个曾经能穿越的路径，现在它只是个不存在的路由。
        """
        response = await client.put(
            "/api/rules/../../etc/passwd", json={"revision": 1, "rules": []}
        )
        assert response.status_code in (404, 405)

    async def test_an_empty_table_is_allowed(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """删光规则是合法操作：全部流量回到自动路由。"""
        response = await client.put("/api/rules", json={"revision": 1, "rules": []})
        assert response.status_code == 200
        await wait_until(lambda: app.rules.is_empty, what="规则热重载")

    async def test_too_many_rules_are_refused_by_the_model(self, client: httpx.AsyncClient) -> None:
        rules = [{"condition": f"h{i}.test", "upstream": "direct"} for i in range(5_001)]
        response = await client.put("/api/rules", json={"revision": 1, "rules": rules})
        assert response.status_code == 422

    async def test_m5_16_duplicate_positions_in_the_database_do_not_crash(
        self, app: Application
    ) -> None:
        """手工改库可能留下重复的 position。排序补上 id 作为决胜项。"""
        conn = sqlite3.connect(app.rules_store.path)
        try:
            conn.execute(
                "INSERT INTO rule (position, condition, upstream, updated_at)"
                " VALUES (0, 'second.test', 'direct', 0)"
            )
            conn.commit()
        finally:
            conn.close()
        rows = app.rules_store.read().rows
        assert [c for _, c, _ in rows] == ["*.pinned.test", "second.test"]


class TestRouteTest:
    async def test_m4_15_the_route_test_agrees_with_a_real_request(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """路由测试必须调用与真实请求同一个决策函数，否则两者必然漂移。"""
        predicted = (
            await client.post("/api/route-test", json={"url": "http://a.pinned.test/"})
        ).json()
        assert predicted["decision"] == "rule"
        assert predicted["candidate_chain"] == ["proxy-a"]
        assert predicted["matched_rule"] == {"position": 0, "condition": "*.pinned.test"}

        # 真实请求：proxy-a 指向 127.0.0.1:1，连必然失败，但健康表会留下痕迹——
        # 哪个出口被真的试过了，这是「实际走了哪条路」的直接证据。
        host, port = app.proxy_address
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(b"GET http://a.pinned.test/ HTTP/1.1\r\nHost: a.pinned.test\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=10)
        finally:
            writer.close()

        tried = {
            item.name
            for item in app.state.health.all(now=time.monotonic())
            if item.total_success + item.total_failure > 0
        }
        assert tried == set(predicted["candidate_chain"])

    async def test_an_unmatched_host_falls_back_to_the_priority_chain(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.post("/api/route-test", json={"url": "http://other.test/"})).json()
        assert body["decision"] == "priority"
        assert body["matched_rule"] is None
        assert body["candidate_chain"] == ["proxy-a", "direct"]

    async def test_a_malformed_url_is_a_400(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/route-test", json={"url": "ftp://x/"})
        assert response.status_code == 400


class TestBackups:
    async def test_a_backup_is_taken_before_每次写入(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        original = config_text(app)
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        backups = (await client.get("/api/config/backups")).json()["backups"]
        assert len(backups) == 1

        writer = ConfigWriter(app)
        saved = await writer.read_backup(backups[0]["filename"])
        assert saved == original

    async def test_m4_18_rotation_keeps_the_configured_count(self, tmp_path: Path) -> None:
        """备份超过 `backup_keep` 时删掉最旧的，且删的不能是最新的那份。"""
        instance = Application(config_path=write_files(tmp_path, backup_keep=2))
        await instance.start()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=create_app(instance), client=("127.0.0.1", 12345)
                ),
                base_url="http://webui.test",
            ) as c:
                for value in (4.0, 5.0, 6.0, 7.0):
                    response = await c.put(
                        "/api/settings",
                        json={"config_version": version(instance), "connect_timeout": value},
                    )
                    assert response.status_code == 200
                backups = (await c.get("/api/config/backups")).json()["backups"]

            assert len(backups) == 2
            writer = ConfigWriter(instance)
            newest = await writer.read_backup(backups[0]["filename"])
            # 最新的备份是上一次写入的内容（connect_timeout = 6.0）。
            assert "connect_timeout = 6.0" in newest
        finally:
            await instance.stop()

    async def test_restoring_brings_back_the_old_content(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        original = config_text(app)
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        backups = (await client.get("/api/config/backups")).json()["backups"]

        response = await client.post(
            "/api/config/restore",
            json={"config_version": version(app), "filename": backups[0]["filename"]},
        )
        assert response.status_code == 200
        assert config_text(app) == original
        assert app.snapshot.routing.connect_timeout == 3.0

    async def test_restoring_backs_up_the_current_file_first(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """恢复本身也要能被撤销。"""
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        first = (await client.get("/api/config/backups")).json()["backups"]
        await client.post(
            "/api/config/restore",
            json={"config_version": version(app), "filename": first[0]["filename"]},
        )
        after = (await client.get("/api/config/backups")).json()["backups"]
        assert len(after) == len(first) + 1

    async def test_a_backup_outside_the_list_is_a_404(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        for name in ("../../etc/passwd", "config-20200101-000000.toml", "state.db"):
            response = await client.post(
                "/api/config/restore",
                json={"config_version": version(app), "filename": name},
            )
            assert response.status_code == 404, name


class TestAtomicWrite:
    """M4-17：写回后崩溃时配置文件要么是旧版要么是新版，不出现截断。

    直接驱动 `ConfigWriter` 而不走 HTTP：这里要断言的是异常发生瞬间磁盘的样子，
    经过 ASGI 层只会看到一个 500，看不到文件状态。
    """

    async def test_a_failure_before_rename_leaves_the_original_intact(
        self, app: Application, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = config_text(app)
        writer = ConfigWriter(app)
        monkeypatch.setattr(config_writer.os, "replace", _boom)

        with pytest.raises(OSError):
            await writer.edit_config(
                lambda text: text.replace("connect_timeout = 3.0", "connect_timeout = 6.0"),
                expected_version=version(app),
                actor="test",
                action="settings.update",
                target="config.toml",
            )
        assert config_text(app) == original
        assert app.snapshot.routing.connect_timeout == 3.0

    async def test_the_target_file_is_never_truncated_in_place(
        self, app: Application, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """以 "w" 打开目标文件会先把它截断：崩在这一刻配置就只剩半截。"""
        target = str(app.snapshot.source_path)
        opened: list[str] = []
        real_open = open

        def spy(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
            if "w" in mode or "a" in mode:
                opened.append(str(file))
            return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr("builtins.open", spy)
        await ConfigWriter(app).edit_config(
            lambda text: text.replace("connect_timeout = 3.0", "connect_timeout = 6.0"),
            expected_version=version(app),
            actor="test",
            action="settings.update",
            target="config.toml",
        )
        assert target not in opened
        assert f"{target}.tmp" in opened

    async def test_the_temporary_file_is_not_left_behind_on_success(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        assert not (app.snapshot.source_path.parent / "config.toml.tmp").exists()

    async def test_the_written_file_keeps_owner_only_permissions(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """`config.toml` 含明文 `auth_token`，写回不能让权限变宽——曾经默认 644。"""
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        mode = app.snapshot.source_path.stat().st_mode & 0o777
        assert mode == 0o600

    async def test_the_backup_file_keeps_owner_only_permissions(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        await client.put(
            "/api/settings", json={"config_version": version(app), "connect_timeout": 6.0}
        )
        backups = list((ConfigWriter(app).backup_dir).glob("config-*"))
        assert backups
        for backup in backups:
            assert backup.stat().st_mode & 0o777 == 0o600


def _boom(*_args: object, **_kwargs: object) -> None:
    raise OSError("模拟崩溃")


class TestAuthentication:
    """写接口全部需要 token。只读接口也一样，见 DD_WEB §7.1.1。"""

    ENDPOINTS = (
        ("GET", "/api/settings", None),
        ("PUT", "/api/settings", {"config_version": "0" * 16, "read_timeout": 5}),
        ("POST", "/api/upstreams", {"config_version": "0" * 16, "name": "x", "type": "direct"}),
        ("PUT", "/api/upstreams/proxy-a", {"config_version": "0" * 16, "priority": 5}),
        ("DELETE", f"/api/upstreams/proxy-a?config_version={'0' * 16}", None),
        ("PUT", "/api/upstreams/priorities", {"config_version": "0" * 16, "groups": [["direct"]]}),
        ("GET", "/api/rules", None),
        ("PUT", "/api/rules", {"revision": 0, "rules": []}),
        ("POST", "/api/rules/validate", {"rules": []}),
        ("POST", "/api/route-test", {"url": "http://x.test/"}),
        ("POST", "/api/reload", None),
        ("GET", "/api/config/backups", None),
        (
            "POST",
            "/api/config/restore",
            {"config_version": "0" * 16, "filename": "x-20200101-000000.toml"},
        ),
        ("GET", "/api/audit", None),
    )

    async def test_every_endpoint_requires_a_token(self, tmp_path: Path) -> None:
        """每个端点换一个客户端 IP：失败限流是按 IP 计的，同一个 IP 连打十几次会
        从第 11 次起返回 `429`，把本来要验的 `401` 盖住。"""
        instance = await _with_token(tmp_path, "settings-token-long-enough")
        try:
            for index, (method, url, payload) in enumerate(self.ENDPOINTS):
                async with _client_for(instance, ip=f"127.0.1.{index + 1}") as c:
                    response = await c.request(method, url, json=payload)
                assert response.status_code == 401, f"{method} {url}"
        finally:
            await instance.stop()


class TestReload:
    async def test_reload_picks_up_an_external_edit(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        path = app.snapshot.source_path
        path.write_text(
            config_text(app).replace("connect_timeout = 3.0", "connect_timeout = 11.0"),
            encoding="utf-8",
        )
        response = await client.post("/api/reload")

        assert response.status_code == 200
        assert app.snapshot.routing.connect_timeout == 11.0
        assert response.json()["config_version"] == version(app)

    async def test_a_broken_file_keeps_the_running_snapshot(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        """一次手滑的编辑不该让代理停摆，因此回 400 而不是 500。"""
        before = app.snapshot.config_version
        app.snapshot.source_path.write_text("this is not toml [[[", encoding="utf-8")

        response = await client.post("/api/reload")
        assert response.status_code == 400
        assert app.snapshot.config_version == before

    async def test_the_error_does_not_leak_the_deployment_path(
        self, client: httpx.AsyncClient, app: Application
    ) -> None:
        path = app.snapshot.source_path
        path.write_text("this is not toml [[[", encoding="utf-8")

        response = await client.post("/api/reload")
        assert str(path) not in response.text
        assert "config.toml" in response.json()["error"]["message"]
