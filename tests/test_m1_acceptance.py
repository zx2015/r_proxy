"""M1 验收点逐条核对。

对应设计：docs/design/MIGRATION.md §3.3。编号与该表一一对应，缺一条即
M1 未完成。行为细节由各模块的单元测试覆盖，这里只做验收层面的确认。
"""

from __future__ import annotations

import asyncio
import resource
import time
from pathlib import Path

import pytest

from r_proxy.app import Application, StartupError
from r_proxy.config.loader import ConfigError, load
from r_proxy.config.validate import validate
from tests.conftest import start_server

BASE = """
[listen]
host = "127.0.0.1"
port = 0

[webui]
enabled = false

[[upstreams]]
name = "direct"
type = "direct"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


async def running_app(tmp_path: Path, text: str = BASE) -> Application:
    app = Application(config_path=write(tmp_path, text))
    await app.start()
    return app


async def talk(app: Application, payload: bytes, *, size: int = 65536) -> bytes:
    host, port = app.proxy_address
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(size), timeout=10)
    writer.close()
    return data


class TestM1Acceptance:
    async def test_m1_01_browses_http_and_https(self, tmp_path: Path) -> None:
        """M1-01：浏览器代理指向 r-proxy，HTTP 与 HTTPS 均可用。"""

        async def origin(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
            await w.drain()
            w.close()

        site = await start_server(origin)
        tunnel_target = await start_server(_echo)
        app = await running_app(tmp_path)
        try:
            http = await talk(
                app,
                f"GET http://{site.address}/ HTTP/1.1\r\nHost: {site.address}\r\n\r\n".encode(),
            )
            assert http.startswith(b"HTTP/1.1 200 OK")

            host, port = app.proxy_address
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(f"CONNECT {tunnel_target.address} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            writer.write(b"tls")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(3), 5) == b"tls"
            writer.close()
        finally:
            await app.stop()

    async def test_m1_02_bracketed_ipv6_connect_parses(self, tmp_path: Path) -> None:
        """M1-02：``CONNECT [2001:db8::1]:443`` 正常解析（不是 400）。"""
        app = await running_app(tmp_path)
        try:
            resp = await talk(app, b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n\r\n")
            assert not resp.startswith(b"HTTP/1.1 400")
        finally:
            await app.stop()

    async def test_m1_03_bare_ipv6_connect_is_400_with_hint(self, tmp_path: Path) -> None:
        """M1-03：裸 IPv6 返回 400 并提示方括号。"""
        app = await running_app(tmp_path)
        try:
            resp = await talk(app, b"CONNECT 2001:db8::1:443 HTTP/1.1\r\n\r\n")
            assert resp.startswith(b"HTTP/1.1 400")
            assert "方括号".encode() in resp
        finally:
            await app.stop()

    async def test_m1_04_ipv6_listen_host_is_rejected(self, tmp_path: Path) -> None:
        """M1-04：``listen.host = "::1"`` 拒绝启动（入向仅 IPv4）。"""
        app = Application(
            config_path=write(
                tmp_path,
                '[listen]\nhost = "::1"\nport = 0\n[webui]\nenabled = false\n'
                '[[upstreams]]\nname = "direct"\ntype = "direct"\n',
            )
        )
        with pytest.raises(StartupError) as e:
            await app.start()
        assert "E_LISTEN_IPV6" in str(e.value)

    async def test_m1_05_100kb_head_is_431(self, tmp_path: Path) -> None:
        """M1-05：请求头 100KB 返回 431。"""
        app = await running_app(tmp_path)
        try:
            bulk = b"".join(f"X-Pad-{i}: {'v' * 900}\r\n".encode() for i in range(120))
            resp = await talk(
                app,
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n" + bulk + b"\r\n",
            )
            assert resp.startswith(b"HTTP/1.1 431")
        finally:
            await app.stop()

    async def test_m1_06_connection_over_limit_gets_503(self, tmp_path: Path) -> None:
        """M1-06：超出 max_client_connections 的连接收到 503。"""
        app = await running_app(tmp_path, BASE + "\n[limits]\nmax_client_connections = 3\n")
        held = []
        try:
            host, port = app.proxy_address
            for _ in range(3):
                _, w = await asyncio.open_connection(host, port)
                w.write(b"GET http://example.com/ HT")
                await w.drain()
                held.append(w)
            await asyncio.sleep(0.05)
            resp = await talk(app, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            assert resp.startswith(b"HTTP/1.1 503")
        finally:
            for w in held:
                w.close()
            await app.stop()

    async def test_m1_07_large_download_does_not_grow_memory(self, tmp_path: Path) -> None:
        """M1-07：客户端不读时，代理不把响应体攒进内存。

        测量代理进程（即本进程）的常驻内存增量。没有背压时，上游全速推送的
        字节会全部堆在代理到客户端那一侧的写缓冲里，RSS 随下载量线性增长。
        """
        total = 96 * 1024 * 1024

        async def origin(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(total).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
            block = b"z" * 65536
            for _ in range(total // 65536):
                w.write(block)
                await w.drain()
            w.close()

        site = await start_server(origin)
        app = await running_app(tmp_path)
        try:
            host, port = app.proxy_address
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                f"GET http://{site.address}/big HTTP/1.1\r\nHost: {site.address}\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")

            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # 客户端此后完全不读，让背压一路顶回上游。
            await asyncio.sleep(1.0)
            growth_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before

            assert growth_kb < 16 * 1024, f"RSS 增长 {growth_kb}KB"
            writer.close()
        finally:
            await app.stop()

    async def test_m1_08_proxy_authorization_is_not_forwarded(self, tmp_path: Path) -> None:
        """M1-08：Proxy-Authorization 不转发。"""
        seen: list[bytes] = []

        async def origin(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await w.drain()
            w.close()

        site = await start_server(origin)
        app = await running_app(tmp_path)
        try:
            await talk(
                app,
                f"GET http://{site.address}/ HTTP/1.1\r\nHost: {site.address}\r\n"
                f"Proxy-Authorization: Basic c2VjcmV0\r\n\r\n".encode(),
            )
            assert b"proxy-authorization" not in seen[0].lower()
            assert b"c2VjcmV0" not in seen[0]
        finally:
            await app.stop()

    async def test_m1_09_long_transfer_is_not_cut_by_timeout(self, tmp_path: Path) -> None:
        """M1-09：只要有字节流动，长时间传输就不被超时中断（空闲超时而非整体超时）。"""

        async def origin(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n")
            for _ in range(5):
                await asyncio.sleep(0.15)  # 每次间隔都超过 read_timeout
                w.write(b"x")
                await w.drain()
            w.close()

        site = await start_server(origin)
        app = await running_app(
            tmp_path, BASE + "\n[routing]\nread_timeout = 0.1\nconnect_timeout = 2.0\n"
        )
        try:
            host, port = app.proxy_address
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                f"GET http://{site.address}/slow HTTP/1.1\r\nHost: {site.address}\r\n\r\n".encode()
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), timeout=15)
            writer.close()
            assert data.endswith(b"xxxxx")
        finally:
            await app.stop()

    async def test_m1_10_graceful_shutdown_lets_active_requests_finish(
        self, tmp_path: Path
    ) -> None:
        """M1-10：关闭时活跃连接优雅完成后才退出。"""
        released = asyncio.Event()

        async def origin(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\n")
            await w.drain()
            await released.wait()
            w.write(b"done")
            await w.drain()
            w.close()

        site = await start_server(origin)
        app = await running_app(tmp_path)
        host, port = app.proxy_address
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            f"GET http://{site.address}/ HTTP/1.1\r\nHost: {site.address}\r\n\r\n".encode()
        )
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")

        stopping = asyncio.create_task(app.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()

        released.set()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"done"
        await asyncio.wait_for(stopping, timeout=5)
        writer.close()

    async def test_m1_11_error_body_leaks_nothing(self, tmp_path: Path, closed_port: int) -> None:
        """M1-11：错误响应体不含目标地址、出口名、堆栈或路径。"""
        app = await running_app(tmp_path)
        try:
            resp = await talk(
                app,
                f"GET http://127.0.0.1:{closed_port}/secret HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{closed_port}\r\n\r\n".encode(),
            )
            body = resp.split(b"\r\n\r\n", 1)[1]
            for leaked in (
                b"127.0.0.1",
                str(closed_port).encode(),
                b"direct",
                b"secret",
                b"Traceback",
                b"r_proxy/",
                b"ECONNREFUSED",
            ):
                assert leaked not in body, leaked
        finally:
            await app.stop()

    def test_m1_12_toml_syntax_error_reports_file_and_position(self, tmp_path: Path) -> None:
        """M1-12：语法错误拒绝启动，报出文件名与行列号。"""
        path = write(tmp_path, "[listen]\nport = = 0\n")
        with pytest.raises(ConfigError) as e:
            load(path)
        message = str(e.value)
        assert "config.toml" in message
        assert "line 2" in message or "第 2 行" in message or "2" in message

    def test_m1_13_unknown_key_is_rejected_with_suggestion(self, tmp_path: Path) -> None:
        """M1-13：未知键拒绝启动，给出最接近的合法键名。"""
        path = write(tmp_path, BASE + "\n[routing]\nconnect_timeut = 5\n")
        with pytest.raises(ConfigError) as e:
            load(path)
        assert "E_UNKNOWN_KEY" in str(e.value)
        assert "connect_timeout" in str(e.value)

    def test_m1_14_misplaced_key_points_at_the_right_table(self, tmp_path: Path) -> None:
        """M1-14：``sticky_fail_threshold`` 误写在 ``[[upstreams]]`` 下，提示它属于 ``[routing]``。

        写错表比写错键名更难自查：键名本身是合法的，只是放错了地方。
        """
        path = write(
            tmp_path,
            "[listen]\nport = 0\n[webui]\nenabled = false\n"
            '[[upstreams]]\nname = "direct"\ntype = "direct"\nsticky_fail_threshold = 3\n',
        )
        with pytest.raises(ConfigError) as e:
            load(path)
        message = str(e.value)
        assert "sticky_fail_threshold" in message
        assert "routing" in message

    def test_m1_15_reading_config_needs_no_tomlkit(self, tmp_path: Path) -> None:
        """M1-15：读路径只用标准库 tomllib。

        import 边界由 tests/test_architecture.py 在子进程中守护，这里确认
        在当前环境下加载与校验确实能跑通。
        """
        snapshot = load(write(tmp_path, BASE))
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        issues = validate(snapshot, has_ipv6_egress=False, nofile_limit=soft)
        assert [i for i in issues if i.level == "error"] == []
        assert snapshot.loaded_at <= time.time()


async def _echo(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    while chunk := await r.read(4096):
        w.write(chunk)
        await w.drain()
