"""端到端的代理行为测试：真实监听、真实客户端、真实上游。

对应设计：docs/design/DD_PROXY.md §4、§5、§6、§8、§9
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from r_proxy.config.model import (
    ConfigSnapshot,
    DatabaseConfig,
    LimitsConfig,
    ListenConfig,
    RoutingConfig,
    UpstreamConfig,
    WebUIConfig,
)
from r_proxy.protocol.server import ProxyServer, _client_addr
from r_proxy.storage.queue import OpKind, WriteOp
from tests.conftest import FakeServer, start_server


class _FakeWriter:
    """只为 ``_client_addr`` 测试打桩：只需要 ``get_extra_info``。"""

    def __init__(self, peername: object) -> None:
        self._peername = peername

    def get_extra_info(self, name: str) -> object:
        assert name == "peername"
        return self._peername


class TestClientAddrExtraction:
    """采集点见 DD_PROXY.md §4.4：只取 host，不取端口，取不到就是 ``None``。"""

    def test_ipv4_peername_yields_the_host(self) -> None:
        assert _client_addr(_FakeWriter(("203.0.113.7", 54321))) == "203.0.113.7"

    def test_ipv6_four_tuple_yields_only_the_host(self) -> None:
        assert _client_addr(_FakeWriter(("::1", 54321, 0, 0))) == "::1"

    def test_missing_peername_yields_none(self) -> None:
        assert _client_addr(_FakeWriter(None)) is None


class RecordingSink:
    """收下写入事件而不落盘。这里只关心「发了没有、发的是什么」。"""

    def __init__(self) -> None:
        self.ops: list[WriteOp] = []

    def put(self, op: WriteOp) -> bool:
        self.ops.append(op)
        return True


def snapshot(
    *upstreams: UpstreamConfig,
    limits: LimitsConfig | None = None,
    routing: RoutingConfig | None = None,
    webui: WebUIConfig | None = None,
) -> ConfigSnapshot:
    return ConfigSnapshot.build(
        listen=ListenConfig(host="127.0.0.1", port=0),
        webui=webui or WebUIConfig(enabled=False),
        database=DatabaseConfig(
            state_path=Path("/tmp/s.db"),
            logs_path=Path("/tmp/l.db"),
            rules_path=Path("/tmp/r.db"),
        ),
        routing=routing or RoutingConfig(connect_timeout=2.0, read_timeout=2.0),
        limits=limits or LimitsConfig(),
        upstreams=upstreams or (UpstreamConfig(name="direct", type="direct", address=None),),
        rules_enabled=True,
        config_version="test",
        source_path=Path("/tmp/config.toml"),
        loaded_at=time.time(),
    )


class RunningProxy:
    def __init__(self, server: ProxyServer, host: str, port: int) -> None:
        self.server = server
        self.host = host
        self.port = port

    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(self.host, self.port)

    async def request(self, raw: bytes, *, read: int = 65536) -> bytes:
        reader, writer = await self.open()
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(read), timeout=5)
        writer.close()
        return data


async def run_proxy(cfg: ConfigSnapshot) -> RunningProxy:
    server = ProxyServer(cfg)
    await server.start()
    host, port = server.bound_address
    return RunningProxy(server, host, port)


@pytest.fixture
async def http_origin() -> AsyncIterator[FakeServer]:
    """一个最小 HTTP 服务端，把收到的请求行回显在响应体里。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        request_line = head.split(b"\r\n", 1)[0]
        body = request_line
        w.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await w.drain()
        w.close()

    srv = await start_server(handler)
    yield srv
    await srv.close()


@pytest.fixture
async def fake_proxy() -> AsyncIterator[FakeServer]:
    """一个最小上级 HTTP 代理：对 CONNECT 回 200 并回显，对普通请求回显请求行。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        request_line = head.split(b"\r\n", 1)[0]
        if request_line.startswith(b"CONNECT"):
            w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await w.drain()
            while chunk := await r.read(4096):
                w.write(b"echo:" + chunk)
                await w.drain()
            return
        body = request_line
        w.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await w.drain()
        w.close()

    srv = await start_server(handler)
    yield srv
    await srv.close()


class TestHttpForwarding:
    async def test_direct_uses_origin_form(self, http_origin: FakeServer) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(
                f"GET http://{http_origin.address}/hello HTTP/1.1\r\n"
                f"Host: {http_origin.address}\r\n\r\n".encode()
            )
            assert resp.startswith(b"HTTP/1.1 200 OK")
            assert resp.endswith(b"GET /hello HTTP/1.1")
        finally:
            await proxy.server.stop()

    async def test_upstream_uses_absolute_form(self, fake_proxy: FakeServer) -> None:
        cfg = snapshot(UpstreamConfig(name="p", type="http", address=fake_proxy.address))
        proxy = await run_proxy(cfg)
        try:
            resp = await proxy.request(
                b"GET http://example.com/hello HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            assert resp.endswith(b"GET http://example.com/hello HTTP/1.1")
        finally:
            await proxy.server.stop()

    async def test_hop_by_hop_headers_are_not_forwarded(self, http_origin: FakeServer) -> None:
        seen: list[bytes] = []

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await w.drain()
            w.close()

        origin = await start_server(handler)
        proxy = await run_proxy(snapshot())
        try:
            await proxy.request(
                f"GET http://{origin.address}/ HTTP/1.1\r\n"
                f"Host: {origin.address}\r\n"
                f"Proxy-Authorization: Basic c2VjcmV0\r\n"
                f"Proxy-Connection: keep-alive\r\n"
                f"Connection: X-Custom\r\n"
                f"X-Custom: drop-me\r\n"
                f"X-Keep: keep-me\r\n\r\n".encode()
            )
            forwarded = seen[0].lower()
            assert b"proxy-authorization" not in forwarded
            assert b"proxy-connection" not in forwarded
            assert b"x-custom" not in forwarded
            assert b"x-keep: keep-me" in forwarded
        finally:
            await proxy.server.stop()

    async def test_host_header_is_rewritten_for_ipv6(self) -> None:
        seen: list[bytes] = []

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await w.drain()
            w.close()

        upstream = await start_server(handler)
        cfg = snapshot(UpstreamConfig(name="p", type="http", address=upstream.address))
        proxy = await run_proxy(cfg)
        try:
            await proxy.request(
                b"GET http://[2001:db8::1]:8080/x HTTP/1.1\r\nHost: [2001:db8::1]:8080\r\n\r\n"
            )
            assert b"host: [2001:db8::1]:8080" in seen[0].lower()
        finally:
            await proxy.server.stop()

    async def test_request_body_is_forwarded(self, fake_proxy: FakeServer) -> None:
        seen: list[bytes] = []

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            head = await r.readuntil(b"\r\n\r\n")
            body = await r.readexactly(11)
            seen.append(head + body)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await w.drain()
            w.close()

        origin = await start_server(handler)
        proxy = await run_proxy(snapshot())
        try:
            await proxy.request(
                f"POST http://{origin.address}/ HTTP/1.1\r\n"
                f"Host: {origin.address}\r\n"
                f"Content-Length: 11\r\n\r\nhello world".encode()
            )
            assert seen[0].endswith(b"hello world")
        finally:
            await proxy.server.stop()

    async def test_response_body_is_streamed_back(self) -> None:
        payload = b"y" * 500_000

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
            w.write(payload)
            await w.drain()
            w.close()

        origin = await start_server(handler)
        proxy = await run_proxy(snapshot())
        try:
            reader, writer = await proxy.open()
            addr = origin.address
            writer.write(f"GET http://{addr}/big HTTP/1.1\r\nHost: {addr}\r\n\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), timeout=10)
            writer.close()
            assert data.endswith(payload)
        finally:
            await proxy.server.stop()

    async def test_unreachable_target_returns_502_without_leaking_address(
        self, closed_port: int
    ) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(
                f"GET http://127.0.0.1:{closed_port}/ HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{closed_port}\r\n\r\n".encode()
            )
            assert resp.startswith(b"HTTP/1.1 502")
            body = resp.split(b"\r\n\r\n", 1)[1]
            assert b"127.0.0.1" not in body
            assert str(closed_port).encode() not in body
            assert b"direct" not in body
        finally:
            await proxy.server.stop()

    async def test_1xx_interim_response_is_skipped(self) -> None:
        """上游先回 ``100 Continue`` 再回真正的最终响应：客户端必须只看到
        最终响应，而不是把 100 Continue 当成结果、把真正的响应当成它的 body。
        """
        payload = b"final-body"

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await w.drain()
            w.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + payload
            )
            await w.drain()
            w.close()

        origin = await start_server(handler)
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(
                f"GET http://{origin.address}/ HTTP/1.1\r\n"
                f"Host: {origin.address}\r\n"
                f"Expect: 100-continue\r\n\r\n".encode()
            )
            assert resp.startswith(b"HTTP/1.1 200 OK")
            assert resp.endswith(payload)
            assert b"100 Continue" not in resp
        finally:
            await proxy.server.stop()

    async def test_slow_request_body_does_not_hang_forever(self) -> None:
        """客户端声明了 ``Content-Length`` 却不发（或发得极慢）：读取 body
        必须有超时兜底，否则这个连接与已经建立的出口 socket 会永久挂起。
        用真实可达的本地源站，确保触发的是「读 body 超时」而非「连不上出口」。
        """

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            # 正常情况下永远不会跑到这——测试要验证的是代理压根等不到完整
            # body 就已经放弃，不会真的把（不完整的）请求转发过去。
            await r.read(-1)

        origin = await start_server(handler)
        cfg = snapshot()
        server = ProxyServer(cfg, head_read_timeout=0.1)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(*server.bound_address)
            addr = origin.address
            writer.write(
                f"POST http://{addr}/ HTTP/1.1\r\n"
                f"Host: {addr}\r\n"
                f"Content-Length: 100\r\n\r\n"
                f"only-a-few-bytes".encode()  # 声明 100 字节，只发一小段就不再发送
            )
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(300), timeout=5)
            assert resp.startswith(b"HTTP/1.1 502")
            writer.close()
        finally:
            await server.stop()


class TestConnectTunnel:
    async def test_direct_tunnel_relays_both_ways(self, echo_server: FakeServer) -> None:
        proxy = await run_proxy(snapshot())
        try:
            reader, writer = await proxy.open()
            writer.write(f"CONNECT {echo_server.address} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")

            writer.write(b"tls-hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(9), 5) == b"tls-hello"
            writer.close()
        finally:
            await proxy.server.stop()

    async def test_prerun_bytes_before_200_are_not_lost(self, echo_server: FakeServer) -> None:
        """客户端常在收到 200 前就发出 ClientHello，这些抢跑字节必须重放给上游。"""
        proxy = await run_proxy(snapshot())
        try:
            reader, writer = await proxy.open()
            writer.write(
                f"CONNECT {echo_server.address} HTTP/1.1\r\n\r\n".encode() + b"EARLY-HELLO"
            )
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            assert await asyncio.wait_for(reader.readexactly(11), 5) == b"EARLY-HELLO"
            writer.close()
        finally:
            await proxy.server.stop()

    async def test_tunnel_via_upstream_proxy(self, fake_proxy: FakeServer) -> None:
        cfg = snapshot(UpstreamConfig(name="p", type="http", address=fake_proxy.address))
        proxy = await run_proxy(cfg)
        try:
            reader, writer = await proxy.open()
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"abc")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(8), 5) == b"echo:abc"
            writer.close()
        finally:
            await proxy.server.stop()

    async def test_upstream_rejecting_connect_yields_502(self) -> None:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await w.drain()
            w.close()

        up = await start_server(handler)
        cfg = snapshot(UpstreamConfig(name="p", type="http", address=up.address))
        proxy = await run_proxy(cfg)
        try:
            resp = await proxy.request(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            assert resp.startswith(b"HTTP/1.1 502")
        finally:
            await proxy.server.stop()

    async def test_unreachable_target_yields_502(self, closed_port: int) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(f"CONNECT 127.0.0.1:{closed_port} HTTP/1.1\r\n\r\n".encode())
            assert resp.startswith(b"HTTP/1.1 502")
        finally:
            await proxy.server.stop()


class TestErrorResponses:
    async def test_malformed_request_line_yields_400(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(b"GARBAGE\r\n\r\n")
            assert resp.startswith(b"HTTP/1.1 400")
        finally:
            await proxy.server.stop()

    async def test_bare_ipv6_connect_yields_400_with_bracket_hint(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(b"CONNECT 2001:db8::1:443 HTTP/1.1\r\n\r\n")
            assert resp.startswith(b"HTTP/1.1 400")
            assert "方括号".encode() in resp
        finally:
            await proxy.server.stop()

    async def test_oversized_request_line_yields_414(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(
                b"GET http://example.com/" + b"a" * 9000 + b" HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 414")
        finally:
            await proxy.server.stop()

    async def test_too_many_headers_yields_431(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            bulk = b"".join(f"X-H{i}: v\r\n".encode() for i in range(200))
            resp = await proxy.request(
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n" + bulk + b"\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 431")
        finally:
            await proxy.server.stop()

    async def test_smuggling_attempt_yields_400(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(
                b"POST http://example.com/ HTTP/1.1\r\nHost: example.com\r\n"
                b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 400")
        finally:
            await proxy.server.stop()

    async def test_slow_client_head_yields_408(self) -> None:
        cfg = snapshot()
        server = ProxyServer(cfg, head_read_timeout=0.1)
        await server.start()
        host, port = server.bound_address
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"GET http://example.com/ HTTP/1.1\r\n")  # 头部不发完
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(200), timeout=5)
            assert resp.startswith(b"HTTP/1.1 408")
            writer.close()
        finally:
            await server.stop()

    async def test_client_disconnect_produces_no_response(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            reader, writer = await proxy.open()
            writer.write(b"GET http://example.com/ HTTP/1.1\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
        finally:
            await proxy.server.stop()

    async def test_error_response_carries_request_id(self) -> None:
        proxy = await run_proxy(snapshot())
        try:
            resp = await proxy.request(b"GARBAGE\r\n\r\n")
            assert b"X-R-Proxy-Request-Id:" in resp
        finally:
            await proxy.server.stop()

    async def test_no_usable_upstream_yields_502(self) -> None:
        cfg = snapshot(UpstreamConfig(name="d", type="direct", address=None, enabled=False))
        proxy = await run_proxy(cfg)
        try:
            resp = await proxy.request(
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 502")
            assert "没有可用的出口".encode() in resp
        finally:
            await proxy.server.stop()


class TestRequestLogging:
    """生产端接线的端到端守护。

    存储层与 Web 层各自测得过、中间没接上，正是 ``request_log`` 空了一整个
    里程碑的原因：两侧的测试都自己构造 ``WriteOp``，谁都不验「一次真实的代理
    请求会不会产生这一行」。这个类专门盯这条接缝。
    """

    @staticmethod
    def rows(sink: RecordingSink) -> list[tuple[object, ...]]:
        return [op.payload for op in sink.ops if op.kind is OpKind.REQUEST_LOG]

    async def test_a_forwarded_request_is_logged(self, http_origin: FakeServer) -> None:
        sink = RecordingSink()
        server = ProxyServer(snapshot(), sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            await RunningProxy(server, host, port).request(
                f"GET http://{http_origin.address}/hello HTTP/1.1\r\n"
                f"Host: {http_origin.address}\r\n\r\n".encode()
            )
            rows = self.rows(sink)
            assert len(rows) == 1
            assert rows[0][1] == "127.0.0.1"
            assert rows[0][2] == http_origin.host
            assert rows[0][4] == "GET"
            assert rows[0][5] == "direct"
            assert rows[0][10] == 200
        finally:
            await server.stop()

    async def test_a_request_to_the_web_ui_port_is_not_logged(
        self, http_origin: FakeServer
    ) -> None:
        """轮询仪表盘的流量不该混进 `request_log`（见 `egress/executor.py`）。"""
        sink = RecordingSink()
        server = ProxyServer(
            snapshot(webui=WebUIConfig(enabled=True, port=http_origin.port)), sink=sink
        )
        await server.start()
        host, port = server.bound_address
        try:
            await RunningProxy(server, host, port).request(
                f"GET http://{http_origin.address}/api/status HTTP/1.1\r\n"
                f"Host: {http_origin.address}\r\n\r\n".encode()
            )
            assert self.rows(sink) == []
        finally:
            await server.stop()

    async def test_a_tunnel_is_logged(self, fake_proxy: FakeServer) -> None:
        sink = RecordingSink()
        cfg = snapshot(UpstreamConfig(name="p", type="http", address=fake_proxy.address))
        server = ProxyServer(cfg, sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            await RunningProxy(server, host, port).request(
                b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", read=64
            )
            rows = self.rows(sink)
            assert len(rows) == 1
            assert rows[0][1] == "127.0.0.1"
            assert (rows[0][2], rows[0][4], rows[0][5]) == ("example.com", "CONNECT", "p")
        finally:
            await server.stop()

    async def test_a_dead_end_502_is_logged(self) -> None:
        """客户端拿到的 502 不含任何线索，这一行是唯一能追溯的地方。"""
        sink = RecordingSink()
        cfg = snapshot(UpstreamConfig(name="d", type="direct", address=None, enabled=False))
        server = ProxyServer(cfg, sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            resp = await RunningProxy(server, host, port).request(
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 502")
            rows = self.rows(sink)
            assert len(rows) == 1
            assert rows[0][1] == "127.0.0.1"
            assert rows[0][11] == "no_available_upstream"
        finally:
            await server.stop()

    async def test_every_attempt_of_a_switch_shares_one_request_id(
        self, http_origin: FakeServer, closed_port: int
    ) -> None:
        """切换链要能按 request_id 归组，否则界面画不出「先试了谁」。

        目标写 ``localhost`` 而不是 ``127.0.0.1``：回环字面量会触发内网直连
        前置（DD_ROUTING §3.2b），``direct`` 被提到链首后第一次就成功，也就
        没有切换可测了。域名不做字面量判定，链序保持优先级序。
        """
        sink = RecordingSink()
        cfg = snapshot(
            UpstreamConfig(name="dead", type="http", address=f"127.0.0.1:{closed_port}"),
            UpstreamConfig(name="direct", type="direct", address=None, priority=200),
        )
        server = ProxyServer(cfg, sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            origin = f"localhost:{http_origin.port}"
            await RunningProxy(server, host, port).request(
                f"GET http://{origin}/ HTTP/1.1\r\nHost: {origin}\r\n\r\n".encode()
            )
            rows = self.rows(sink)
            assert [(row[5], row[7]) for row in rows] == [("dead", 0), ("direct", 1)]
            assert len({row[0] for row in rows}) == 1
            assert len({row[1] for row in rows}) == 1  # 同一连接，来源地址不变
        finally:
            await server.stop()


class TestTrafficLogging:
    """字节统计的产出与去向：DD_PROXY §5.2.2/§6.4、DD_STORAGE §4.3b。

    ``request_log`` 的字节列恒为 0（见该类的姊妹说明），真实数字走独立的
    ``traffic_log``，只在数据传输真正结束（响应体转发完 / 隧道关闭）时才写。
    """

    @staticmethod
    def rows(sink: RecordingSink) -> list[tuple[object, ...]]:
        return [op.payload for op in sink.ops if op.kind is OpKind.TRAFFIC_LOG]

    @staticmethod
    async def wait_for(sink: RecordingSink, count: int, *, timeout: float = 5.0) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = TestTrafficLogging.rows(sink)
            if len(rows) >= count:
                return rows
            await asyncio.sleep(0.01)
        raise AssertionError(f"未在 {timeout}s 内看到 {count} 条 traffic_log 记录")

    async def test_a_forwarded_http_request_is_logged(self, http_origin: FakeServer) -> None:
        sink = RecordingSink()
        server = ProxyServer(snapshot(), sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            await RunningProxy(server, host, port).request(
                f"GET http://{http_origin.address}/hello HTTP/1.1\r\n"
                f"Host: {http_origin.address}\r\n\r\n".encode()
            )
            (row,) = await self.wait_for(sink, 1)
            request_id, host_col, upstream, bytes_up, bytes_down, _ = row
            assert (host_col, upstream) == (http_origin.host, "direct")
            # 响应体是回显的请求行（direct 出口按 origin-form 重写请求行，
            # 具体字节数由 build_request 决定），这里只断言「确实发生过」。
            assert bytes_down > 0
            assert bytes_up > 0
        finally:
            await server.stop()

    async def test_a_request_to_the_web_ui_port_is_not_logged(
        self, http_origin: FakeServer
    ) -> None:
        sink = RecordingSink()
        server = ProxyServer(
            snapshot(webui=WebUIConfig(enabled=True, port=http_origin.port)), sink=sink
        )
        await server.start()
        host, port = server.bound_address
        try:
            await RunningProxy(server, host, port).request(
                f"GET http://{http_origin.address}/api/status HTTP/1.1\r\n"
                f"Host: {http_origin.address}\r\n\r\n".encode()
            )
            await asyncio.sleep(0.05)
            assert self.rows(sink) == []
        finally:
            await server.stop()

    async def test_a_tunnel_is_logged_with_actual_byte_counts(
        self, fake_proxy: FakeServer
    ) -> None:
        sink = RecordingSink()
        cfg = snapshot(UpstreamConfig(name="p", type="http", address=fake_proxy.address))
        server = ProxyServer(cfg, sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            proxy = RunningProxy(server, host, port)
            reader, writer = await proxy.open()
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"abc")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(8), 5) == b"echo:abc"
            writer.close()

            (row,) = await self.wait_for(sink, 1)
            request_id, host_col, upstream, bytes_up, bytes_down, _ = row
            assert (host_col, upstream) == ("example.com", "p")
            assert bytes_up == 3  # "abc"
            assert bytes_down == 8  # "echo:abc"
        finally:
            await server.stop()

    async def test_a_dead_end_502_produces_no_traffic_row(self) -> None:
        """一次尝试都没发起，没有字节可记——与 ``request_log`` 不同，后者仍留一行。"""
        sink = RecordingSink()
        cfg = snapshot(UpstreamConfig(name="d", type="direct", address=None, enabled=False))
        server = ProxyServer(cfg, sink=sink)
        await server.start()
        host, port = server.bound_address
        try:
            resp = await RunningProxy(server, host, port).request(
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 502")
            await asyncio.sleep(0.05)
            assert self.rows(sink) == []
        finally:
            await server.stop()


class TestConnectionLimits:
    async def test_exceeding_max_connections_yields_503(self, blackhole_server: FakeServer) -> None:
        cfg = snapshot(limits=LimitsConfig(max_client_connections=2))
        proxy = await run_proxy(cfg)
        held: list[asyncio.StreamWriter] = []
        try:
            for _ in range(2):
                _, w = await proxy.open()
                w.write(b"GET http://example.com/ HTTP")  # 占住连接，头部不发完
                await w.drain()
                held.append(w)
            await asyncio.sleep(0.05)

            resp = await proxy.request(
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            assert resp.startswith(b"HTTP/1.1 503")
        finally:
            for w in held:
                w.close()
            await proxy.server.stop()

    async def test_connections_are_released_after_completion(self, http_origin: FakeServer) -> None:
        cfg = snapshot(limits=LimitsConfig(max_client_connections=1))
        proxy = await run_proxy(cfg)
        try:
            for _ in range(3):
                resp = await proxy.request(
                    f"GET http://{http_origin.address}/ HTTP/1.1\r\n"
                    f"Host: {http_origin.address}\r\n\r\n".encode()
                )
                assert resp.startswith(b"HTTP/1.1 200")
                await asyncio.sleep(0.02)
        finally:
            await proxy.server.stop()


class TestLifecycle:
    async def test_stop_waits_for_active_connections(self, echo_server: FakeServer) -> None:
        proxy = await run_proxy(snapshot())
        reader, writer = await proxy.open()
        writer.write(f"CONNECT {echo_server.address} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")

        stopping = asyncio.create_task(proxy.server.stop(drain_timeout=1.0))
        await asyncio.sleep(0.05)
        assert not stopping.done()  # 活跃隧道未被立刻掐断
        writer.close()
        await asyncio.wait_for(stopping, timeout=3)

    async def test_stop_force_closes_after_drain_timeout(
        self, blackhole_server: FakeServer
    ) -> None:
        proxy = await run_proxy(snapshot())
        reader, writer = await proxy.open()
        writer.write(f"CONNECT {blackhole_server.address} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")

        started = time.monotonic()
        await asyncio.wait_for(proxy.server.stop(drain_timeout=0.2), timeout=3)
        assert time.monotonic() - started < 2
        writer.close()

    async def test_stop_is_idempotent(self) -> None:
        proxy = await run_proxy(snapshot())
        await proxy.server.stop()
        await proxy.server.stop()

    async def test_port_is_no_longer_accepting_after_stop(self) -> None:
        proxy = await run_proxy(snapshot())
        await proxy.server.stop()
        with pytest.raises(OSError):
            _, w = await asyncio.open_connection(proxy.host, proxy.port)
            w.close()
