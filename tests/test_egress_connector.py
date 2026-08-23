"""egress/connector.py 的出口连接测试。

对应设计：docs/design/DD_PROXY.md §7、§7.1
"""

from __future__ import annotations

import asyncio
import errno

import pytest

from r_proxy.config.model import RoutingConfig, UpstreamConfig
from r_proxy.contracts import AddressFamily, FailureKind, Headers, Method, RequestTarget
from r_proxy.egress.connector import (
    ConnectorError,
    UpstreamConnector,
    classify_transport,
)
from tests.conftest import FakeServer, start_server

DIRECT = UpstreamConfig(name="direct", type="direct", address=None)


def target(host: str = "example.com", port: int = 80, *, connect: bool = False) -> RequestTarget:
    return RequestTarget(
        host=host,
        port=port,
        method=Method.CONNECT if connect else Method.GET,
        url=None if connect else f"http://{host}:{port}/",
        is_connect=connect,
        family=AddressFamily.of_literal(host),
    )


def http_upstream(address: str, **kw: object) -> UpstreamConfig:
    return UpstreamConfig(name="proxy", type="http", address=address, **kw)  # type: ignore[arg-type]


@pytest.fixture
def routing() -> RoutingConfig:
    return RoutingConfig(connect_timeout=2.0, read_timeout=2.0)


class TestDirectConnect:
    async def test_connects_to_the_target_itself(
        self, echo_server: FakeServer, routing: RoutingConfig
    ) -> None:
        conn = await UpstreamConnector().connect(
            DIRECT, target(echo_server.host, echo_server.port), routing
        )
        try:
            conn.writer.write(b"ping")
            await conn.writer.drain()
            assert await conn.reader.readexactly(4) == b"ping"
        finally:
            await conn.close()

    async def test_refused_connection_raises_connector_error(
        self, closed_port: int, routing: RoutingConfig
    ) -> None:
        with pytest.raises(ConnectorError) as e:
            await UpstreamConnector().connect(DIRECT, target("127.0.0.1", closed_port), routing)
        assert e.value.kind is FailureKind.ROUTE_ERROR

    async def test_error_message_never_leaks_the_address(
        self, closed_port: int, routing: RoutingConfig
    ) -> None:
        """错误信息可能被拼进响应体，不得暴露内网拓扑。"""
        with pytest.raises(ConnectorError) as e:
            await UpstreamConnector().connect(DIRECT, target("127.0.0.1", closed_port), routing)
        text = str(e.value) + (e.value.error or "")
        assert "127.0.0.1" not in text
        assert str(closed_port) not in text

    async def test_connect_timeout(
        self, blackhole_server: FakeServer, routing: RoutingConfig
    ) -> None:
        """连接超时用不可路由地址触发，而非能立即建连的黑洞服务端。"""
        cfg = RoutingConfig(connect_timeout=0.05)
        with pytest.raises(ConnectorError) as e:
            # 192.0.2.0/24 是 TEST-NET-1，保证不可路由。
            await UpstreamConnector().connect(DIRECT, target("192.0.2.1", 80), cfg)
        assert e.value.error in ("TimeoutError", "ENETUNREACH", "EHOSTUNREACH")

    async def test_per_upstream_timeout_override_is_used(self, routing: RoutingConfig) -> None:
        slow = UpstreamConfig(name="direct", type="direct", address=None, connect_timeout=0.05)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ConnectorError):
            await UpstreamConnector().connect(slow, target("192.0.2.1", 80), routing)
        assert loop.time() - started < 1.0  # 用了 0.05 而不是全局的 2.0


class TestHappyEyeballs:
    """RFC 8305 用标准库自带实现，这里断言参数确实传了下去。

    自己实现「并行发起、延迟启动第二族、取消落败任务」只会在取消与异常
    传播的边角上引入 bug，收益为零；因此真正要守住的是这两个参数。
    """

    async def test_direct_connect_enables_happy_eyeballs(
        self, echo_server: FakeServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = await self._capture_kwargs(
            monkeypatch, echo_server, RoutingConfig(happy_eyeballs_delay=0.25), DIRECT
        )
        assert seen["happy_eyeballs_delay"] == 0.25

    async def test_address_families_are_interleaved(
        self, echo_server: FakeServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不传 interleave 时候选地址仍按 getaddrinfo 原序，同族地址会连成一片，
        第二族要等前面全试完才轮到——那不是 RFC 8305 描述的行为。"""
        seen = await self._capture_kwargs(
            monkeypatch, echo_server, RoutingConfig(happy_eyeballs_delay=0.25), DIRECT
        )
        assert seen["interleave"] == 1

    async def test_zero_delay_disables_the_parameters(
        self, echo_server: FakeServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """退化为按 getaddrinfo 返回顺序串行尝试。"""
        seen = await self._capture_kwargs(
            monkeypatch, echo_server, RoutingConfig(happy_eyeballs_delay=0.0), DIRECT
        )
        assert "happy_eyeballs_delay" not in seen
        assert "interleave" not in seen

    async def test_dual_stack_target_falls_back_to_ipv4(self, routing: RoutingConfig) -> None:
        """M2-17：localhost 双栈但 IPv6 侧无人监听时仍应连上 IPv4。"""
        server = await start_server(_echo, host="127.0.0.1")
        conn = await UpstreamConnector().connect(
            DIRECT,
            target("localhost", server.port),
            RoutingConfig(connect_timeout=2.0, happy_eyeballs_delay=0.25),
        )
        try:
            conn.writer.write(b"ping")
            await conn.writer.drain()
            assert await conn.reader.readexactly(4) == b"ping"
        finally:
            await conn.close()

    @staticmethod
    async def _capture_kwargs(
        monkeypatch: pytest.MonkeyPatch,
        server: FakeServer,
        routing: RoutingConfig,
        upstream: UpstreamConfig,
    ) -> dict[str, object]:
        from r_proxy.egress import connector as module

        seen: dict[str, object] = {}
        real = module.asyncio.open_connection

        async def spy(
            host: str, port: int, **kwargs: object
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            seen.update(kwargs)
            return await real(host, port)

        monkeypatch.setattr(module.asyncio, "open_connection", spy)
        conn = await UpstreamConnector().connect(
            upstream, target(server.host, server.port), routing
        )
        await conn.close()
        return seen


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(4096):
        writer.write(chunk)
        await writer.drain()


class TestPlainUpstreamConnect:
    async def test_connects_to_the_proxy_not_the_target(
        self, echo_server: FakeServer, routing: RoutingConfig
    ) -> None:
        """普通 HTTP 转发时 TCP 连的是上级代理，目标写在请求行里。"""
        conn = await UpstreamConnector().connect(
            http_upstream(echo_server.address), target("example.com", 80), routing
        )
        try:
            assert conn.peer_port == echo_server.port
        finally:
            await conn.close()

    async def test_unreachable_proxy_is_an_upstream_error(
        self, closed_port: int, routing: RoutingConfig
    ) -> None:
        """连不上上级代理即出口整体不可用，必须计入熔断。"""
        with pytest.raises(ConnectorError) as e:
            await UpstreamConnector().connect(
                http_upstream(f"127.0.0.1:{closed_port}"), target(), routing
            )
        assert e.value.kind is FailureKind.UPSTREAM_ERROR

    async def test_unparseable_address_is_a_capability_mismatch(
        self, routing: RoutingConfig
    ) -> None:
        """地址非法应当在启动校验就拦下；运行时兜底不得计入熔断。"""
        with pytest.raises(ConnectorError) as e:
            await UpstreamConnector().connect(http_upstream("nonsense"), target(), routing)
        assert e.value.kind is FailureKind.CAPABILITY_MISMATCH


class TestConnectTunnel:
    async def test_direct_tunnel_needs_no_handshake(
        self, echo_server: FakeServer, routing: RoutingConfig
    ) -> None:
        """直连没有上级代理可握手，状态人为置 200。"""
        tunnel = await UpstreamConnector().connect_tunnel(
            DIRECT, target(echo_server.host, echo_server.port, connect=True), routing
        )
        try:
            assert tunnel.status == 200
            assert echo_server.received == []  # 未发出任何 CONNECT 报文
        finally:
            await tunnel.close()

    async def test_sends_connect_request_to_upstream(self, routing: RoutingConfig) -> None:
        seen: list[bytes] = []

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await w.drain()
            await asyncio.sleep(0.5)

        srv = await start_server(handler)
        try:
            tunnel = await UpstreamConnector().connect_tunnel(
                http_upstream(srv.address), target("example.com", 443, connect=True), routing
            )
            assert tunnel.status == 200
            request = seen[0].decode()
            assert request.startswith("CONNECT example.com:443 HTTP/1.1\r\n")
            assert "host: example.com:443\r\n" in request.lower()
            await tunnel.close()
        finally:
            await srv.close()

    async def test_ipv6_target_gets_brackets_in_connect_line(self, routing: RoutingConfig) -> None:
        """规范化时去掉的方括号，写回 CONNECT 行时必须加回来。"""
        seen: list[bytes] = []

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await w.drain()
            await asyncio.sleep(0.5)

        srv = await start_server(handler)
        try:
            tunnel = await UpstreamConnector().connect_tunnel(
                http_upstream(srv.address), target("2001:db8::1", 443, connect=True), routing
            )
            assert seen[0].startswith(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n")
            await tunnel.close()
        finally:
            await srv.close()

    async def test_non_200_status_is_returned_not_raised(self, routing: RoutingConfig) -> None:
        """407 需要交给切换策略判定，不是连接器的错误。"""

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await w.drain()

        srv = await start_server(handler)
        try:
            tunnel = await UpstreamConnector().connect_tunnel(
                http_upstream(srv.address), target("example.com", 443, connect=True), routing
            )
            assert tunnel.status == 407
            assert not tunnel.established
            await tunnel.close()
        finally:
            await srv.close()

    async def test_upstream_closing_mid_handshake(self, routing: RoutingConfig) -> None:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.close()

        srv = await start_server(handler)
        try:
            with pytest.raises(ConnectorError) as e:
                await UpstreamConnector().connect_tunnel(
                    http_upstream(srv.address), target("example.com", 443, connect=True), routing
                )
            assert e.value.kind is FailureKind.UPSTREAM_ERROR
        finally:
            await srv.close()

    async def test_handshake_timeout_is_a_route_error(self) -> None:
        """代理收下 CONNECT 却迟迟不回应答，是它到不了目标，不是它自己坏了。

        典型场景：目标被墙且丢包，上级 squid 卡在自己的 connect 超时上，而我们的
        ``read_timeout`` 先到。若记成 ``upstream_error``，连着访问几个被墙站点就
        会把一个完全健康的出口熔断，其余目标跟着一起不可用——正是 DD_ROUTING
        §4.6 为 ``direct`` 设防的那种拖累，上级代理同样受不起。
        """

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            await asyncio.sleep(5)  # 一个字节都不回

        srv = await start_server(handler)
        cfg = RoutingConfig(connect_timeout=2.0, read_timeout=0.05)
        try:
            with pytest.raises(ConnectorError) as e:
                await UpstreamConnector().connect_tunnel(
                    http_upstream(srv.address), target("example.com", 443, connect=True), cfg
                )
            assert e.value.kind is FailureKind.ROUTE_ERROR
            assert e.value.error == "TimeoutError"
        finally:
            await srv.close()

    async def test_garbage_handshake_response(self, routing: RoutingConfig) -> None:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"NOT-HTTP\r\n\r\n")
            await w.drain()
            await asyncio.sleep(0.2)

        srv = await start_server(handler)
        try:
            with pytest.raises(ConnectorError) as e:
                await UpstreamConnector().connect_tunnel(
                    http_upstream(srv.address), target("example.com", 443, connect=True), routing
                )
            assert e.value.kind is FailureKind.UPSTREAM_ERROR
        finally:
            await srv.close()

    async def test_handshake_response_headers_are_captured(self, routing: RoutingConfig) -> None:
        """来源判定需要看 Server / X-Squid-Error 等头。"""

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 503 Service Unavailable\r\nServer: squid/5.7\r\n\r\n")
            await w.drain()

        srv = await start_server(handler)
        try:
            tunnel = await UpstreamConnector().connect_tunnel(
                http_upstream(srv.address), target("example.com", 443, connect=True), routing
            )
            assert tunnel.headers.get("server") == "squid/5.7"
            await tunnel.close()
        finally:
            await srv.close()

    async def test_non_ascii_header_value_does_not_fail_the_handshake(
        self, routing: RoutingConfig
    ) -> None:
        """非标代理可能在 Server 等头里塞非 ASCII 字节；能收到字节说明代理是通的，
        不该因为一个头的编码就整次判为 upstream_error 并计入全局熔断。"""

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            # 非 ASCII 字节，之前会在 `raw.decode("ascii")` 上抛 UnicodeDecodeError。
            w.write(b"HTTP/1.1 200 OK\r\nServer: \xe4\xbb\xa3\xe7\x86\x9d\r\n\r\n")
            await w.drain()
            await asyncio.sleep(0.2)

        srv = await start_server(handler)
        try:
            tunnel = await UpstreamConnector().connect_tunnel(
                http_upstream(srv.address), target("example.com", 443, connect=True), routing
            )
            assert tunnel.status == 200
            await tunnel.close()
        finally:
            await srv.close()

    async def test_status_line_with_extra_spaces_is_still_parsed(
        self, routing: RoutingConfig
    ) -> None:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1  200  Connection Established\r\n\r\n")
            await w.drain()
            await asyncio.sleep(0.2)

        srv = await start_server(handler)
        try:
            tunnel = await UpstreamConnector().connect_tunnel(
                http_upstream(srv.address), target("example.com", 443, connect=True), routing
            )
            assert tunnel.status == 200
            await tunnel.close()
        finally:
            await srv.close()

    async def test_no_credentials_in_connect_request(self, routing: RoutingConfig) -> None:
        """本期不实现上级代理认证，绝不能凭空发出凭据头。"""
        seen: list[bytes] = []

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await w.drain()
            await asyncio.sleep(0.3)

        srv = await start_server(handler)
        try:
            up = http_upstream(srv.address)
            tunnel = await UpstreamConnector().connect_tunnel(
                up, target("example.com", 443, connect=True), routing
            )
            assert b"proxy-authorization" not in seen[0].lower()
            await tunnel.close()
        finally:
            await srv.close()


class TestClassifyTransport:
    @pytest.mark.parametrize("code", [errno.ENETUNREACH, errno.EAFNOSUPPORT, errno.EHOSTUNREACH])
    @pytest.mark.parametrize("is_direct", [True, False])
    def test_address_family_errors_are_capability_mismatch(
        self, code: int, is_direct: bool
    ) -> None:
        """地址族不可达不记负面记忆、不累加熔断。"""
        assert (
            classify_transport(OSError(code, "x"), is_direct=is_direct)
            is FailureKind.CAPABILITY_MISMATCH
        )

    def test_refused_target_is_a_route_error(self) -> None:
        """direct 连的是目标：连不上说明这条路走不通，不是出口坏了。"""
        assert (
            classify_transport(OSError(errno.ECONNREFUSED, "x"), is_direct=True)
            is FailureKind.ROUTE_ERROR
        )

    def test_refused_upstream_proxy_is_an_upstream_error(self) -> None:
        """同一个 ECONNREFUSED 在两种场景下含义相反：代理连不上就是出口整体不可用。"""
        assert (
            classify_transport(OSError(errno.ECONNREFUSED, "x"), is_direct=False)
            is FailureKind.UPSTREAM_ERROR
        )

    def test_timeout_follows_the_same_split(self) -> None:
        assert classify_transport(TimeoutError(), is_direct=True) is FailureKind.ROUTE_ERROR
        assert classify_transport(TimeoutError(), is_direct=False) is FailureKind.UPSTREAM_ERROR

    def test_dns_failure_of_a_target_is_a_route_error(self) -> None:
        exc = OSError(errno.ENOENT, "Name or service not known")
        assert classify_transport(exc, is_direct=True) is FailureKind.ROUTE_ERROR


class TestConnHelpers:
    async def test_close_is_idempotent(
        self, echo_server: FakeServer, routing: RoutingConfig
    ) -> None:
        conn = await UpstreamConnector().connect(
            DIRECT, target(echo_server.host, echo_server.port), routing
        )
        await conn.close()
        await conn.close()

    def test_headers_default_to_empty(self) -> None:
        assert len(Headers()) == 0
