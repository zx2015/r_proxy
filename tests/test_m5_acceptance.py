"""M5 验收点逐条核对。

对应设计：docs/design/MIGRATION.md §7.4。编号与该表一一对应。

单元层面的条目在各自的模块测试里（条件识别 tests/test_rules_condition.py、
遮蔽检测 tests/test_rules_loader.py、匹配语义 tests/test_rules_matcher.py、
库的乐观锁 tests/test_storage_rules_store.py、Web 接口
tests/test_web_config_write.py、前端源码 tests/test_web_frontend.py）。这里验证
端到端可观察的行为：真实套接字、真实 SQLite 文件、真实的 HTTP 与 CONNECT。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from r_proxy.app import Application, StartupError
from r_proxy.storage.rules_store import RulesStore
from r_proxy.web.app import create_app
from tests.conftest import start_server

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def config(*upstreams: str, tmp_path: Path, extra: str = "") -> str:
    return (
        '[listen]\nhost = "127.0.0.1"\nport = 0\n'
        "[webui]\nenabled = false\n"
        "[database]\n"
        f'state_path = "{tmp_path / "state.db"}"\n'
        f'logs_path = "{tmp_path / "logs.db"}"\n'
        f'rules_path = "{tmp_path / "rules.db"}"\n' + extra + "".join(upstreams)
    )


def http_upstream(name: str, address: str, *, priority: int = 100, enabled: bool = True) -> str:
    return (
        f'\n[[upstreams]]\nname = "{name}"\ntype = "http"\n'
        f'address = "{address}"\npriority = {priority}\nenabled = {str(enabled).lower()}\n'
    )


def direct_upstream(name: str = "direct", *, priority: int = 100) -> str:
    return f'\n[[upstreams]]\nname = "{name}"\ntype = "direct"\npriority = {priority}\n'


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def seed_rules(tmp_path: Path, *pairs: tuple[str, str]) -> RulesStore:
    store = RulesStore(tmp_path / "rules.db")
    store.ensure_schema()
    store.replace(list(pairs), expected_revision=0, now_unix=0)
    return store


def responder(status_line: bytes, *, seen: list[str] | None = None) -> Handler:
    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen.append(head.split(b"\r\n")[0].decode())
        w.write(status_line + b"\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()

    return handler


async def running_app(path: Path) -> Application:
    app = Application(config_path=path)
    await app.start()
    return app


async def get(app: Application, host: str = "site.test", *, path: str = "/") -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    writer.write(
        f"GET http://{host}{path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n\r\n".encode()
    )
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


async def get_authority(app: Application, authority: str, *, host: str) -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    writer.write(
        f"GET http://{authority}/ HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n\r\n".encode()
    )
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


async def connect(app: Application, host: str = "site.test", port: int = 443) -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


class TestMatchingSemantics:
    async def test_m5_01_the_first_matching_rule_wins(self, tmp_path: Path) -> None:
        """M5-01：`*` 写在前面就赢，与已废止的 M3-10 相反。

        这是整次重构里唯一「代码能跑但结果全错」的改动——分桶索引不变、类型
        不变、接口不变，只有一个比较符号。端到端跑一遍，看真正的字节去了哪里。
        """
        seen_all: list[str] = []
        seen_gh: list[str] = []
        catch_all = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_all))
        github = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_gh))
        seed_rules(tmp_path, ("*", "all"), ("*.github.com", "gh"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("all", catch_all.address),
                    http_upstream("gh", github.address),
                    tmp_path=tmp_path,
                ),
            )
        )
        try:
            await get(app, host="api.github.com")
            assert len(seen_all) == 1
            assert seen_gh == []
        finally:
            await app.stop()

    async def test_m5_01_reordering_the_same_two_rules_flips_the_result(
        self, tmp_path: Path
    ) -> None:
        """顺序是唯一的变量：换个顺序，同一个请求必须走另一个出口。"""
        seen_all: list[str] = []
        seen_gh: list[str] = []
        catch_all = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_all))
        github = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_gh))
        seed_rules(tmp_path, ("*.github.com", "gh"), ("*", "all"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("all", catch_all.address),
                    http_upstream("gh", github.address),
                    tmp_path=tmp_path,
                ),
            )
        )
        try:
            await get(app, host="api.github.com")
            assert len(seen_gh) == 1
            assert seen_all == []
        finally:
            await app.stop()

    async def test_m5_09_http_and_connect_hit_the_same_rule(self, tmp_path: Path) -> None:
        """M5-09：v1 里 HTTP 匹配 URL、CONNECT 匹配 `host:port`，同一条规则会
        对 HTTPS 静默失效。v2 两者都只看主机名。"""
        seen: list[str] = []
        ruled = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen))
        auto = await start_server(responder(b"HTTP/1.1 200 OK"))
        seed_rules(tmp_path, ("*.pinned.test", "ruled"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("ruled", ruled.address, priority=50),
                    http_upstream("auto", auto.address, priority=10),
                    tmp_path=tmp_path,
                ),
            )
        )
        try:
            await get(app, host="api.pinned.test")
            await connect(app, host="api.pinned.test")
            assert [line.split()[0] for line in seen] == ["GET", "CONNECT"]
        finally:
            await app.stop()

    async def test_m5_20_disabled_rules_send_everything_through_automatic_routing(
        self, tmp_path: Path
    ) -> None:
        """M5-20：救场开关。规则仍在库里，但一条都不生效。"""
        seen_ruled: list[str] = []
        seen_auto: list[str] = []
        ruled = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_ruled))
        auto = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_auto))
        seed_rules(tmp_path, ("*", "ruled"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("ruled", ruled.address, priority=50),
                    http_upstream("auto", auto.address, priority=10),
                    tmp_path=tmp_path,
                    extra="[rules]\nenabled = false\n",
                ),
            )
        )
        try:
            await get(app)
            assert len(seen_auto) == 1
            assert seen_ruled == []
        finally:
            await app.stop()


class TestDeadEnds:
    async def test_m5_23_a_disabled_target_reports_its_position(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """M5-23（承接 M3-14）：客户端只拿到通用 `502`，不得回显拓扑；用户能
        据此判断「规则配错了」的地方只有带序号的日志。"""
        healthy = await start_server(responder(b"HTTP/1.1 200 OK"))
        seed_rules(tmp_path, ("*.other.test", "healthy"), ("site.test", "off"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("off", healthy.address, enabled=False),
                    http_upstream("healthy", healthy.address),
                    tmp_path=tmp_path,
                ),
            )
        )
        try:
            with caplog.at_level(logging.WARNING):
                response = await get(app)
            assert response.startswith(b"HTTP/1.1 502")
            assert b"off" not in response
            assert any("rules[1]" in r.getMessage() for r in caplog.records), [
                r.getMessage() for r in caplog.records
            ]
        finally:
            await app.stop()

    async def test_m5_23_a_rule_to_direct_for_an_ipv6_target_without_egress(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """M5-23（承接 M3-15）：结构性不可达。不发起连接、不写负面记忆，
        日志里带 `ipv6_unavailable` 与规则序号。"""
        seed_rules(tmp_path, ("[2001:db8::1]", "direct"))
        path = write_config(tmp_path, config(direct_upstream(), tmp_path=tmp_path))
        app = await running_app(path)
        app.state.set_ipv6_egress(False)
        try:
            with caplog.at_level(logging.WARNING):
                response = await get_authority(app, "[2001:db8::1]:80", host="[2001:db8::1]")
            assert response.startswith(b"HTTP/1.1 502")
            assert any(
                "ipv6_unavailable" in r.getMessage() and "rules[0]" in r.getMessage()
                for r in caplog.records
            ), [r.getMessage() for r in caplog.records]
            assert app.state.memory.size == 0
        finally:
            await app.stop()


class TestConfigCompatibility:
    def leftover(self, tmp_path: Path) -> Path:
        return write_config(
            tmp_path,
            config(direct_upstream(), tmp_path=tmp_path, extra='[rules]\nfiles = ["user.rules"]\n'),
        )

    def test_m5_19_a_leftover_files_key_is_refused_with_instructions(self, tmp_path: Path) -> None:
        """M5-19：静默忽略会让用户以为文件里的规则还在生效，而实际上所有流量
        都在走自动路由——一个不报错也看不出来的路由行为变化。"""
        with pytest.raises(StartupError) as caught:
            Application(config_path=self.leftover(tmp_path)).check()
        assert "E_RULES_FILES_REMOVED" in str(caught.value)
        assert "rules.db" in str(caught.value)

    async def test_m5_19_start_refuses_too(self, tmp_path: Path) -> None:
        """``--check`` 与真正启动走的是同一条加载路径，但只有后者会绑端口。"""
        with pytest.raises(StartupError):
            await Application(config_path=self.leftover(tmp_path)).start()


class TestWebWrites:
    """规则写入与配置写入共用同一把锁，且跨库校验双向成立。"""

    async def web_app(self, tmp_path: Path) -> tuple[Application, httpx.AsyncClient]:
        seed_rules(tmp_path, ("*.pinned.test", "proxy-a"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("proxy-a", "127.0.0.1:1", priority=10),
                    direct_upstream(),
                    tmp_path=tmp_path,
                ),
            )
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app), client=("127.0.0.1", 12345)),
            base_url="http://webui.test",
        )
        return app, client

    async def test_m5_22_removing_a_referenced_upstream_by_hand_is_refused_on_reload(
        self, tmp_path: Path
    ) -> None:
        """M5-22：候选配置校验必须从 `rules.db` 读规则。

        不改的话，绕过出口接口（手工编辑 + 重载，或设置页的写入）删掉一个被
        规则引用的出口不会触发引用检查，代理会带着一条指向不存在出口的规则跑。
        """
        app, client = await self.web_app(tmp_path)
        try:
            path = app.snapshot.source_path
            text = path.read_text(encoding="utf-8")
            start = text.index('\n[[upstreams]]\nname = "proxy-a"')
            end = text.index('\n[[upstreams]]\nname = "direct"')
            path.write_text(text[:start] + text[end:], encoding="utf-8")

            async with client as c:
                response = await c.post("/api/reload")
            assert response.status_code == 400
            assert "E_RULE_TARGET" in response.text
            assert app.snapshot.upstream("proxy-a") is not None
        finally:
            await app.stop()

    async def test_m5_27_saving_rules_and_config_concurrently_serialises(
        self, tmp_path: Path
    ) -> None:
        """M5-27：两条写入路径共用一把锁。

        不共用时它们会各自以自己读到的基线为准热重载，后完成的那个把先完成的
        改动挤掉——症状是「保存成功了但改动不见了」。
        """
        app, client = await self.web_app(tmp_path)
        try:
            async with client as c:
                rules, settings = await asyncio.gather(
                    c.put(
                        "/api/rules",
                        json={
                            "revision": 1,
                            "rules": [{"condition": "*.pinned.test", "upstream": "direct"}],
                        },
                    ),
                    c.put(
                        "/api/settings",
                        json={
                            "config_version": app.snapshot.config_version,
                            "connect_timeout": 7.0,
                        },
                    ),
                )
            assert (rules.status_code, settings.status_code) == (200, 200)
            # 两者都必须落在最终状态里：任何一个被挤掉都说明锁没生效。
            assert app.snapshot.routing.connect_timeout == 7.0
            assert [r.target for r in app.rules.rules] == ["direct"]
        finally:
            await app.stop()
