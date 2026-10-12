"""SOCKS5 端到端集成测试与自动故障切换验证。

对应需求：docs/requirements/SOCKS5_UPSTREAM.md
对应设计：docs/design/DD_SOCKS5.md

验证点：
1. HTTP 明文请求经由 SOCKS5 隧道转发，发送 origin-form（GET /path HTTP/1.1）。
2. HTTPS CONNECT 请求经由 SOCKS5 隧道双向盲转发。
3. RFC 1929 用户名密码认证集成。
4. SOCKS5 目标不可达（REP != 0x00，如 0x05 Connection refused）时自动切换到下一个出口。
5. SOCKS5 代理自身网络故障时（UPSTREAM_ERROR）自动切换并计入健康熔断。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from r_proxy.config.model import (
    ConfigSnapshot,
    DatabaseConfig,
    LimitsConfig,
    ListenConfig,
    RoutingConfig,
    UpstreamAuth,
    UpstreamConfig,
    WebUIConfig,
)
from r_proxy.protocol.server import ProxyServer
from r_proxy.state.health import HealthState
from tests.mocks.mock_socks5 import start_mock_socks5


def build_snapshot(
    *upstreams: UpstreamConfig,
    routing: RoutingConfig | None = None,
) -> ConfigSnapshot:
    return ConfigSnapshot.build(
        listen=ListenConfig(host="127.0.0.1", port=0),
        webui=WebUIConfig(enabled=False),
        database=DatabaseConfig(
            state_path=Path("/tmp/test_s.db"),
            logs_path=Path("/tmp/test_l.db"),
            rules_path=Path("/tmp/test_r.db"),
        ),
        routing=routing or RoutingConfig(connect_timeout=2.0, read_timeout=2.0),
        limits=LimitsConfig(),
        upstreams=upstreams,
        rules_enabled=True,
        config_version="test_socks5",
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

    async def request(self, raw: bytes, *, read_bytes: int = 65536) -> bytes:
        reader, writer = await self.open()
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(read_bytes), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        return data


async def start_proxy(cfg: ConfigSnapshot) -> RunningProxy:
    server = ProxyServer(cfg)
    await server.start()
    host, port = server.bound_address
    return RunningProxy(server, host, port)


class TestSocks5HttpForwarding:
    """验证 HTTP 明文请求经 SOCKS5 代理转发。"""

    async def test_socks5_http_forwarding_origin_form(self) -> None:
        received_http_request: list[bytes] = []

        async def http_responder(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            # 读取客户端（r-proxy）发来的 HTTP 请求
            data = await reader.readuntil(b"\r\n\r\n")
            received_http_request.append(data)
            # 返回标准的 HTTP 响应
            body = b"hello from socks5 target"
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(resp)
            await writer.drain()
            writer.close()

        mock_socks = await start_mock_socks5(custom_data_handler=http_responder)
        upstream = UpstreamConfig(
            name="socks-1",
            type="socks5",
            address=f"127.0.0.1:{mock_socks.port}",
            priority=10,
        )
        proxy = await start_proxy(build_snapshot(upstream))

        try:
            raw_req = (
                b"GET http://example.com/api/v1/resource HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"User-Agent: test-client\r\n\r\n"
            )
            resp = await proxy.request(raw_req)

            assert resp.startswith(b"HTTP/1.1 200 OK")
            assert b"hello from socks5 target" in resp

            # 核心断言 1: SOCKS5 目标解析正确
            assert mock_socks.received_targets == [("example.com", 80)]

            # 核心断言 2: SOCKS5 隧道中转发必须是 origin-form（GET /api/v1/resource HTTP/1.1）
            assert len(received_http_request) == 1
            request_line = received_http_request[0].split(b"\r\n", 1)[0]
            assert request_line == b"GET /api/v1/resource HTTP/1.1"
        finally:
            await proxy.server.stop()
            await mock_socks.close()


class TestSocks5ConnectTunnel:
    """验证 HTTPS CONNECT 隧道转发。"""

    async def test_socks5_connect_tunnel_relay(self) -> None:
        # MockSocks5Server 默认在握手成功后作为 Echo 服务
        mock_socks = await start_mock_socks5()
        upstream = UpstreamConfig(
            name="socks-tunnel",
            type="socks5",
            address=f"127.0.0.1:{mock_socks.port}",
            priority=10,
        )
        proxy = await start_proxy(build_snapshot(upstream))

        try:
            reader, writer = await proxy.open()
            connect_req = (
                b"CONNECT secure.test.org:443 HTTP/1.1\r\n"
                b"Host: secure.test.org:443\r\n\r\n"
            )
            writer.write(connect_req)
            await writer.drain()

            # 客户端读取 200 Connection Established
            established = await reader.readuntil(b"\r\n\r\n")
            assert established.startswith(b"HTTP/1.1 200 Connection Established")

            # 经隧道发送数据并接收 Echo 回显
            writer.write(b"TLS_CLIENT_HELLO_SIMULATION")
            await writer.drain()

            echoed = await reader.read(len(b"TLS_CLIENT_HELLO_SIMULATION"))
            assert echoed == b"TLS_CLIENT_HELLO_SIMULATION"

            writer.close()
            await writer.wait_closed()

            assert mock_socks.received_targets == [("secure.test.org", 443)]
        finally:
            await proxy.server.stop()
            await mock_socks.close()


class TestSocks5Auth:
    """验证 SOCKS5 用户名密码认证。"""

    async def test_socks5_auth_success(self) -> None:
        async def responder(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nAUTH")
            await writer.drain()
            writer.close()

        mock_socks = await start_mock_socks5(
            auth=("proxy_admin", "p@ssw0rd!"),
            custom_data_handler=responder,
        )
        upstream = UpstreamConfig(
            name="socks-auth",
            type="socks5",
            address=f"127.0.0.1:{mock_socks.port}",
            auth=UpstreamAuth(username="proxy_admin", password="p@ssw0rd!"),
            priority=10,
        )
        proxy = await start_proxy(build_snapshot(upstream))

        try:
            req = b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
            resp = await proxy.request(req)
            assert resp.startswith(b"HTTP/1.1 200 OK")
            assert b"AUTH" in resp
            assert mock_socks.auth_attempts == [("proxy_admin", "p@ssw0rd!")]
        finally:
            await proxy.server.stop()
            await mock_socks.close()


class TestSocks5Failover:
    """验证 SOCKS5 失败时的自动故障切换 (Failover) 逻辑。"""

    async def test_socks5_target_unreachable_failover_to_next_upstream(self) -> None:
        """核心场景：SOCKS5 连接目标返回 0x05 (Connection refused) 时，
        自动切换到下一个出口重试并成功。同时验证不计入全局熔断，只记负面记忆。
        """
        # 1. 主出口：SOCKS5 返回 REP=0x05 (连接被目标主机拒绝)
        mock_fail = await start_mock_socks5(rep=0x05)

        # 2. 备选出口：SOCKS5 正常响应
        async def backup_responder(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            body = b"served from backup socks5"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        mock_backup = await start_mock_socks5(custom_data_handler=backup_responder)

        u_primary = UpstreamConfig(
            name="socks-primary-fail",
            type="socks5",
            address=f"127.0.0.1:{mock_fail.port}",
            priority=10,
        )
        u_backup = UpstreamConfig(
            name="socks-secondary-ok",
            type="socks5",
            address=f"127.0.0.1:{mock_backup.port}",
            priority=20,
        )

        proxy = await start_proxy(build_snapshot(u_primary, u_backup))

        try:
            req = b"GET http://target.example.org/test HTTP/1.1\r\nHost: target.example.org\r\n\r\n"
            resp = await proxy.request(req)

            # 客户端透明收到 200 响应，来自备用代理
            assert resp.startswith(b"HTTP/1.1 200 OK")
            assert b"served from backup socks5" in resp

            # 验证两个代理都被访问过
            assert mock_fail.received_targets == [("target.example.org", 80)]
            assert mock_backup.received_targets == [("target.example.org", 80)]

            # 验证健康状态：REP=0x05 是 route_error（目标不可达），不得触发 primary 的全局熔断
            health_primary = proxy.server.state.health.snapshot_of("socks-primary-fail")
            assert health_primary.consecutive_failures == 0
            assert health_primary.state is HealthState.CLOSED

            # 负面记忆：在 memory 表中有 target.example.org 对应 primary 的记录
            assert proxy.server.state.memory.is_blocked(
                "target.example.org", "socks-primary-fail", now=time.monotonic()
            )
        finally:
            await proxy.server.stop()
            await mock_fail.close()
            await mock_backup.close()

    async def test_socks5_server_dead_failover_and_circuit_breaker(self) -> None:
        """SOCKS5 代理自身不可达（如端口未监听），归类为 UPSTREAM_ERROR，
        自动切换到备用出口，并且计入全局熔断计数。
        """
        async def backup_responder(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            body = b"served from alive backup"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        mock_backup = await start_mock_socks5(custom_data_handler=backup_responder)

        # primary 端口 1 不可连接
        u_dead = UpstreamConfig(
            name="socks-dead",
            type="socks5",
            address="127.0.0.1:1",
            priority=10,
        )
        u_backup = UpstreamConfig(
            name="socks-alive",
            type="socks5",
            address=f"127.0.0.1:{mock_backup.port}",
            priority=20,
        )

        proxy = await start_proxy(build_snapshot(u_dead, u_backup))

        try:
            req = b"GET http://target.example.org/test HTTP/1.1\r\nHost: target.example.org\r\n\r\n"
            resp = await proxy.request(req)

            assert resp.startswith(b"HTTP/1.1 200 OK")
            assert b"served from alive backup" in resp

            # 验证 dead 出口记录了 UPSTREAM_ERROR，连续失败计数自增
            health_dead = proxy.server.state.health.snapshot_of("socks-dead")
            assert health_dead.consecutive_failures == 1
            assert health_dead.total_failure == 1
        finally:
            await proxy.server.stop()
            await mock_backup.close()
