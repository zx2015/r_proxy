"""粘性映射与负面记忆的管理接口。

对应设计：docs/design/DD_WEB.md §4.1、§8.6，需求：WEBUI_SPEC.md §2.3、§3.3。

权威在内存，因此每个改动都要两处都验：内存立刻生效（下一个请求就按新绑定走），
库里最终也有（重启后还在）。只验一处会漏掉最难查的那类缺陷——界面显示已改、
实际行为没变，或者反过来。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from r_proxy.app import Application
from r_proxy.storage.reader import ReadOnlyPool
from r_proxy.web.app import create_app

CONFIG = """
[listen]
host = "127.0.0.1"
port = 0

[webui]
enabled = false

[database]
state_path = "{state}"
logs_path = "{logs}"
rules_path = "{rules}"

[[upstreams]]
name = "proxy-a"
type = "http"
address = "127.0.0.1:1"
priority = 10

[[upstreams]]
name = "proxy-off"
type = "http"
address = "127.0.0.1:2"
priority = 20
enabled = false

[[upstreams]]
name = "direct"
type = "direct"
priority = 100
"""


def config_text(tmp_path: Path) -> str:
    return CONFIG.format(
        state=tmp_path / "state.db", logs=tmp_path / "logs.db", rules=tmp_path / "rules.db"
    )


async def running(tmp_path: Path) -> Application:
    path = tmp_path / "config.toml"
    path.write_text(config_text(tmp_path), encoding="utf-8")
    app = Application(config_path=path)
    await app.start()
    return app


def client_for(app: Application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), client=("127.0.0.1", 12345)),
        base_url="http://webui.test",
    )


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[Application]:
    instance = await running(tmp_path)
    yield instance
    await instance.stop()


@pytest.fixture
async def client(app: Application) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app) as c:
        yield c


class TestStickyList:
    async def test_an_empty_cache_lists_nothing(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/sticky")).json()
        assert (body["items"], body["total"]) == ([], 0)

    async def test_entries_learned_by_the_proxy_show_up(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        app.state.sticky.record_success("a.example", "proxy-a", now=time.monotonic())
        item = (await client.get("/api/sticky")).json()["items"][0]
        assert (item["host"], item["upstream"], item["source"]) == ("a.example", "proxy-a", "auto")
        assert item["last_used_age_seconds"] is not None

    async def test_the_host_filter_matches_substrings(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        now = time.monotonic()
        app.state.sticky.record_success("a.example", "proxy-a", now=now)
        app.state.sticky.record_success("b.other", "proxy-a", now=now)
        body = (await client.get("/api/sticky?q=example")).json()
        assert [i["host"] for i in body["items"]] == ["a.example"]

    async def test_the_upstream_filter_is_exact(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        now = time.monotonic()
        app.state.sticky.record_success("a.example", "proxy-a", now=now)
        app.state.sticky.record_success("b.example", "direct", now=now)
        body = (await client.get("/api/sticky?upstream=direct")).json()
        assert [i["host"] for i in body["items"]] == ["b.example"]

    async def test_sorting_by_failures_surfaces_the_problem_bindings(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        now = time.monotonic()
        sticky = app.state.sticky
        sticky.record_success("good.example", "proxy-a", now=now)
        sticky.record_success("bad.example", "proxy-a", now=now - 100)
        sticky.record_failure("bad.example", threshold=99)
        body = (await client.get("/api/sticky?sort=fails")).json()
        assert [i["host"] for i in body["items"]] == ["bad.example", "good.example"]

    async def test_pages_slice_the_filtered_total(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        now = time.monotonic()
        for i in range(5):
            app.state.sticky.record_success(f"h{i}.example", "proxy-a", now=now + i)
        body = (await client.get("/api/sticky?page=2&page_size=2")).json()
        assert body["total"] == 5
        assert [i["host"] for i in body["items"]] == ["h2.example", "h1.example"]


class TestManualBinding:
    async def test_binding_takes_effect_in_memory(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        response = await client.put("/api/sticky/a.example", json={"upstream": "proxy-a"})
        assert response.status_code == 200
        assert response.json()["source"] == "manual"
        entry = app.state.sticky.get("a.example")
        assert entry is not None
        assert (entry.upstream, entry.source) == ("proxy-a", "manual")

    async def test_binding_is_persisted_as_manual(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """自动路径的 UPSERT 带 ``WHERE source != 'manual'``，用它写手动绑定会静默失效。"""
        await client.put("/api/sticky/a.example", json={"upstream": "proxy-a"})
        row = await _wait_for_row(
            app.storage.state_reader,
            "SELECT upstream_name, source FROM host_upstream WHERE host = 'a.example'",
        )
        assert (row["upstream_name"], row["source"]) == ("proxy-a", "manual")

    async def test_rebinding_replaces_the_previous_choice(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        await client.put("/api/sticky/a.example", json={"upstream": "proxy-a"})
        # 等第一次绑定落盘再改绑：同一批里两次写入会合并成一条，走不到
        # ON CONFLICT 分支，也就验不到「manual 行能被 manual 覆盖」。
        await _wait_for_row(
            app.storage.state_reader,
            "SELECT 1 AS n FROM host_upstream WHERE host = 'a.example' AND source = 'manual'",
        )
        await client.put("/api/sticky/a.example", json={"upstream": "direct"})
        row = await _wait_for_row(
            app.storage.state_reader,
            "SELECT upstream_name FROM host_upstream WHERE host = 'a.example'"
            " AND upstream_name = 'direct'",
        )
        assert row["upstream_name"] == "direct"
        entry = app.state.sticky.get("a.example")
        assert entry is not None and entry.upstream == "direct"

    async def test_the_host_is_normalised_like_the_proxy_does(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """键必须与代理侧一致，否则绑定看起来成功了却永远不被命中。"""
        await client.put("/api/sticky/EXAMPLE.COM.", json={"upstream": "proxy-a"})
        assert app.state.sticky.get("example.com") is not None

    async def test_an_unknown_upstream_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.put("/api/sticky/a.example", json={"upstream": "ghost"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UPSTREAM_NOT_FOUND"

    async def test_a_disabled_upstream_is_rejected(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """禁用的出口不进候选链，绑上去等于什么都没发生。"""
        response = await client.put("/api/sticky/a.example", json={"upstream": "proxy-off"})
        assert response.status_code == 409
        assert app.state.sticky.get("a.example") is None

    async def test_an_empty_body_is_rejected(self, client: httpx.AsyncClient) -> None:
        assert (await client.put("/api/sticky/a.example", json={})).status_code == 422

    async def test_the_binding_is_audited(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """「这个 host 为什么走了那个出口」要能追溯到人。"""
        await client.put("/api/sticky/a.example", json={"upstream": "proxy-a"})
        row = await _wait_for_row(
            app.storage.logs_reader,
            "SELECT actor, action, target, diff FROM config_audit ORDER BY id DESC LIMIT 1",
        )
        assert (row["action"], row["target"], row["actor"]) == (
            "bind_sticky",
            "a.example",
            "127.0.0.1",
        )
        assert row["diff"] == "None -> proxy-a"


class TestClearing:
    async def test_clearing_removes_it_from_memory_and_the_database(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        exists = "SELECT 1 AS n FROM host_upstream WHERE host = 'a.example'"
        await client.put("/api/sticky/a.example", json={"upstream": "proxy-a"})
        await _wait_for_row(app.storage.state_reader, exists)

        response = await client.delete("/api/sticky/a.example")
        assert response.json()["cleared"] == 1
        assert app.state.sticky.get("a.example") is None
        await _wait_until_gone(app.storage.state_reader, exists)

    async def test_clearing_an_unbound_host_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.delete("/api/sticky/nobody.example")).status_code == 404

    async def test_a_batch_by_upstream_spares_the_others(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        now = time.monotonic()
        sticky = app.state.sticky
        sticky.record_success("a.example", "proxy-a", now=now)
        sticky.record_success("b.example", "proxy-a", now=now)
        sticky.record_success("c.example", "direct", now=now)
        body = (await client.request("DELETE", "/api/sticky", json={"upstream": "proxy-a"})).json()
        assert body["cleared"] == 2
        assert [e.host for e in sticky.entries(now=now)] == ["c.example"]

    async def test_a_batch_by_hosts_normalises_each_entry(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        app.state.sticky.record_success("a.example", "proxy-a", now=time.monotonic())
        body = (
            await client.request("DELETE", "/api/sticky", json={"hosts": ["A.EXAMPLE.", "ghost"]})
        ).json()
        assert body["cleared"] == 1
        assert app.state.sticky.size == 0

    async def test_an_empty_criterion_is_refused(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """空条件不该被理解成「清空全部」：一次手滑不能抹掉整份路由记忆。"""
        app.state.sticky.record_success("a.example", "proxy-a", now=time.monotonic())
        assert (await client.request("DELETE", "/api/sticky", json={})).status_code == 400
        assert app.state.sticky.size == 1

    async def test_an_oversized_host_list_is_refused(self, client: httpx.AsyncClient) -> None:
        hosts = [f"h{i}.example" for i in range(1001)]
        response = await client.request("DELETE", "/api/sticky", json={"hosts": hosts})
        assert response.status_code == 422


class TestPromotion:
    """把粘性映射固化成规则（WEBUI_SPEC §2.3、DD_WEB §8.9）。

    固化改变的是**语义**而不只是存续时间：规则命中后候选链长度恒为 1、失败原样
    返回，而粘性只是把出口提到链首。因此每个用例都要验到「规则真的进了表首并
    热重载生效」，只验接口回了 200 说明不了什么。
    """

    async def test_the_rule_lands_at_the_top_and_takes_effect(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        app.state.sticky.record_success("a.example", "proxy-a", now=time.monotonic())
        response = await client.post(
            "/api/sticky/a.example/promote",
            json={"condition": "a.example", "upstream": "proxy-a"},
        )
        body = response.json()
        assert (response.status_code, body["position"]) == (200, 0)
        assert app.rules_store.read().rows == ((0, "a.example", "proxy-a"),)
        # 热重载已经完成：接口返回时新规则就该在决策用的规则集里。
        assert [r.raw for r in app.rules.rules] == ["a.example"]

    async def test_an_existing_table_keeps_its_order_below_the_new_rule(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """插表首而不是表尾：表尾的新规则被前面更宽的条件一遮就永不生效。"""
        await client.post(
            "/api/sticky/b.example/promote",
            json={"condition": "*.example", "upstream": "direct"},
        )
        await client.post(
            "/api/sticky/a.example/promote",
            json={"condition": "a.example", "upstream": "proxy-a"},
        )
        assert [c for _p, c, _u in app.rules_store.read().rows] == ["a.example", "*.example"]

    async def test_the_condition_may_generalise_the_host(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """确认框允许把精确主机改成 `*.上级域`，服务端按提交的条件写。"""
        app.state.sticky.record_success("api.example", "proxy-a", now=time.monotonic())
        await client.post(
            "/api/sticky/api.example/promote",
            json={"condition": "*.example", "upstream": "proxy-a"},
        )
        assert [r.raw for r in app.rules.rules] == ["*.example"]

    async def test_the_promoted_sticky_entry_is_cleared(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """规则接管后这条粘性再也不会被读到，留着只会显示一个不再变化的命中数。"""
        exists = "SELECT 1 AS n FROM host_upstream WHERE host = 'a.example'"
        await client.put("/api/sticky/a.example", json={"upstream": "proxy-a"})
        await _wait_for_row(app.storage.state_reader, exists)

        body = (
            await client.post(
                "/api/sticky/a.example/promote",
                json={"condition": "a.example", "upstream": "proxy-a"},
            )
        ).json()
        assert body["sticky_cleared"] is True
        assert app.state.sticky.get("a.example") is None
        await _wait_until_gone(app.storage.state_reader, exists)

    async def test_other_hosts_now_covered_by_the_new_rule_are_swept_too(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """`*.modelscope.cn` 固化后，`api-inference.modelscope.cn` 这类同域名下的
        粘性映射会被新规则短路，留着只会显示一批不再变化的绑定，应一并清掉。"""
        app.state.sticky.record_success(
            "api-inference.modelscope.cn", "proxy-a", now=time.monotonic()
        )
        app.state.sticky.record_success("unrelated.example", "proxy-a", now=time.monotonic())

        body = (
            await client.post(
                "/api/sticky/www.modelscope.cn/promote",
                json={"condition": "*.modelscope.cn", "upstream": "proxy-a"},
            )
        ).json()

        assert body["swept_hosts"] == ["api-inference.modelscope.cn"]
        assert app.state.sticky.get("api-inference.modelscope.cn") is None
        # 不受新规则覆盖的条目必须原样保留，不能被连带清掉。
        assert app.state.sticky.get("unrelated.example") is not None

    async def test_the_host_is_normalised_before_clearing(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        app.state.sticky.record_success("example.com", "proxy-a", now=time.monotonic())
        body = (
            await client.post(
                "/api/sticky/EXAMPLE.COM./promote",
                json={"condition": "example.com", "upstream": "proxy-a"},
            )
        ).json()
        assert body["sticky_cleared"] is True
        assert app.state.sticky.size == 0

    async def test_promoting_without_a_sticky_entry_still_writes_the_rule(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """条目可能在点击与提交之间被 LRU 淘汰。规则内容全在请求体里，没有理由
        因此失败——报错只会让用户重来一遍同样的操作。"""
        body = (
            await client.post(
                "/api/sticky/gone.example/promote",
                json={"condition": "gone.example", "upstream": "proxy-a"},
            )
        ).json()
        assert body["sticky_cleared"] is False
        assert [r.raw for r in app.rules.rules] == ["gone.example"]

    async def test_a_broader_rule_already_covering_the_host_is_reported(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """回固化**前**的命中，界面据此说明「新规则从此优先于 rules[i]」。"""
        await client.post(
            "/api/sticky/b.example/promote",
            json={"condition": "*.example", "upstream": "direct"},
        )
        body = (
            await client.post(
                "/api/sticky/a.example/promote",
                json={"condition": "a.example", "upstream": "proxy-a"},
            )
        ).json()
        assert body["previous_match"] == {"position": 0, "condition": "*.example"}

    async def test_a_duplicate_condition_is_refused(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """重复点两次只会在表首堆出一条让原规则永不生效的死行。"""
        first = {"condition": "a.example", "upstream": "proxy-a"}
        await client.post("/api/sticky/a.example/promote", json=first)
        before = app.rules_store.read()

        response = await client.post(
            "/api/sticky/a.example/promote", json={"condition": "a.example", "upstream": "direct"}
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "RULE_EXISTS"
        assert error["details"] == {"position": 0, "condition": "a.example", "upstream": "proxy-a"}
        assert app.rules_store.read() == before

    async def test_differently_spelled_ip_literals_count_as_duplicates(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """比的是编译后的形态：`[2001:0db8::1]` 与 `[2001:db8::1]` 是同一条规则。"""
        await client.post(
            "/api/sticky/2001:db8::1/promote",
            json={"condition": "[2001:0db8::1]", "upstream": "proxy-a"},
        )
        response = await client.post(
            "/api/sticky/2001:db8::1/promote",
            json={"condition": "[2001:db8::1]", "upstream": "proxy-a"},
        )
        assert response.status_code == 409

    async def test_an_unknown_upstream_is_rejected(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/sticky/a.example/promote", json={"condition": "a.example", "upstream": "ghost"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UPSTREAM_NOT_FOUND"
        assert app.rules_store.read().rows == ()

    async def test_a_disabled_upstream_is_rejected(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """规则指向禁用出口在路由层是死路：链上没有顺延余地，直接 502。"""
        response = await client.post(
            "/api/sticky/a.example/promote",
            json={"condition": "a.example", "upstream": "proxy-off"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "UPSTREAM_DISABLED"
        assert app.rules_store.read().rows == ()

    async def test_an_invalid_condition_leaves_the_table_untouched(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/sticky/a.example/promote",
            json={"condition": "a.example:443", "upstream": "proxy-a"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_RULES"
        assert app.rules_store.read().rows == ()

    async def test_the_sticky_entry_survives_a_refused_promotion(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        """清除在写库成功之后：反过来会在校验失败时白丢一条有用的绑定。"""
        app.state.sticky.record_success("a.example", "proxy-a", now=time.monotonic())
        await client.post(
            "/api/sticky/a.example/promote",
            json={"condition": "a.example:443", "upstream": "proxy-a"},
        )
        assert app.state.sticky.get("a.example") is not None

    async def test_the_promotion_is_audited_as_a_rules_write(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            "/api/sticky/a.example/promote",
            json={"condition": "*.example", "upstream": "proxy-a"},
        )
        row = await _wait_for_row(
            app.storage.logs_reader,
            "SELECT action, target, diff, version_before, version_after"
            " FROM config_audit ORDER BY id DESC LIMIT 1",
        )
        assert (row["action"], row["target"]) == ("rules.promote", "a.example")
        assert (row["version_before"], row["version_after"]) == ("0", "1")
        assert "+*.example\tproxy-a" in row["diff"]


class TestRouteBlocks:
    async def test_blocks_are_listed_with_their_remaining_time(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        app.state.memory.block("a.example", "proxy-a", now=time.monotonic(), reason="ECONNRESET")
        item = (await client.get("/api/route-blocks")).json()["items"][0]
        assert (item["host"], item["upstream"], item["reason"]) == (
            "a.example",
            "proxy-a",
            "ECONNRESET",
        )
        assert 0 < item["expires_in_seconds"] <= 600

    async def test_expired_blocks_are_not_listed(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        app.state.memory.block(
            "old.example", "proxy-a", now=time.monotonic() - 3600, reason="ECONNRESET"
        )
        assert (await client.get("/api/route-blocks")).json()["total"] == 0

    async def test_clearing_a_block_lets_the_upstream_back_in(
        self, app: Application, client: httpx.AsyncClient
    ) -> None:
        now = time.monotonic()
        app.state.memory.block("a.example", "proxy-a", now=now, reason="ECONNRESET")
        response = await client.delete("/api/route-blocks/a.example/proxy-a")
        assert response.json()["cleared"] == 1
        assert app.state.memory.is_blocked("a.example", "proxy-a", now=now) is False

    async def test_clearing_a_missing_block_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.delete("/api/route-blocks/ghost.example/proxy-a")).status_code == 404


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/sticky"),
            ("GET", "/api/route-blocks"),
            ("GET", "/api/upstreams"),
            ("PUT", "/api/sticky/a.example"),
            ("POST", "/api/sticky/a.example/promote"),
            ("DELETE", "/api/sticky/a.example"),
            ("POST", "/api/upstreams/proxy-a/test"),
        ],
    )
    async def test_every_endpoint_requires_the_token(
        self, tmp_path: Path, method: str, path: str
    ) -> None:
        """只读接口也不例外：粘性表暴露内部 host，出口列表暴露内网地址。"""
        config = tmp_path / "config.toml"
        # 只替换第一处：`enabled = false` 在 [webui] 与被禁用的出口里各有一次，
        # 全局替换会把 auth_token 塞进 [[upstreams]]，启动直接失败。
        config.write_text(
            config_text(tmp_path).replace(
                "enabled = false", 'enabled = false\nauth_token = "tok-abcdefghijklmnop"', 1
            ),
            encoding="utf-8",
        )
        instance = Application(config_path=config)
        await instance.start()
        try:
            async with client_for(instance) as c:
                response = await c.request(method, path, json={"upstream": "proxy-a"})
        finally:
            await instance.stop()
        assert response.status_code == 401


async def _wait_for_row(pool: ReadOnlyPool, sql: str, *, timeout: float = 5.0) -> sqlite3.Row:
    """等写者线程把这一批落盘。批量落盘有时间窗，不能立刻查。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rows = await asyncio.to_thread(pool.query, sql)
        if rows:
            return rows[0]
        await asyncio.sleep(0.05)
    raise AssertionError(f"未等到落盘：{sql}")


async def _wait_until_gone(pool: ReadOnlyPool, sql: str, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not await asyncio.to_thread(pool.query, sql):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"记录仍在库里：{sql}")
