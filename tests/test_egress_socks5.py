"""SOCKS5 客户端协议实现测试。

对应设计：docs/design/DD_SOCKS5.md。
验证二进制报文解析、两阶段防吞错应答解析、认证协商与错误映射。
"""

from __future__ import annotations

import asyncio

import pytest

from r_proxy.config.model import UpstreamAuth
from r_proxy.contracts import AddressFamily, FailureKind, Method, RequestTarget
from r_proxy.egress.connector import ConnectorError
from r_proxy.egress.socks5 import (
    Socks5Reply,
    _classify_rep,
    build_connect_request,
    socks5_handshake,
)
from tests.mocks.mock_socks5 import start_mock_socks5


def target(host: str = "example.com", port: int = 80) -> RequestTarget:
    return RequestTarget(
        host=host,
        port=port,
        method=Method.GET,
        url=f"http://{host}:{port}/",
        is_connect=False,
        family=AddressFamily.of_literal(host),
    )


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    if not writer.is_closing():
        writer.close()
    try:
        await writer.wait_closed()
    except (OSError, asyncio.CancelledError):
        pass


class TestBuildConnectRequest:
    def test_domain_target(self) -> None:
        req = build_connect_request(target("example.com", 80))
        # VER(1B)=0x05, CMD=0x01, RSV=0x00, ATYP=0x03, LEN=11, "example.com", PORT=80
        assert req == b"\x05\x01\x00\x03\x0bexample.com\x00\x50"

    def test_ipv4_target(self) -> None:
        req = build_connect_request(target("1.2.3.4", 8080))
        # VER=0x05, CMD=0x01, RSV=0x00, ATYP=0x01, 1.2.3.4 (4B), PORT=8080 (0x1f90)
        assert req == b"\x05\x01\x00\x01\x01\x02\x03\x04\x1f\x90"

    def test_ipv6_target_strips_brackets(self) -> None:
        # 带有方括号的 IPv6 地址必须能正确清洗并作为 ATYP=0x04 打包
        req = build_connect_request(target("[2001:db8::1]", 443))
        # ATYP=0x04
        assert req[:4] == b"\x05\x01\x00\x04"
        assert len(req) == 4 + 16 + 2
        assert req[-2:] == b"\x01\xbb"  # 443 = 0x01bb

    def test_domain_too_long_raises(self) -> None:
        long_host = "a" * 256 + ".com"
        with pytest.raises(ConnectorError) as exc_info:
            build_connect_request(target(long_host, 80))
        assert exc_info.value.kind is FailureKind.CAPABILITY_MISMATCH
        assert exc_info.value.error == "DomainTooLong"

    def test_domain_non_ascii_raises(self) -> None:
        with pytest.raises(ConnectorError) as exc_info:
            build_connect_request(target("测试.com", 80))
        assert exc_info.value.kind is FailureKind.CAPABILITY_MISMATCH
        assert exc_info.value.error == "DomainNotAscii"


class TestRepClassification:
    def test_route_errors(self) -> None:
        for rep in (
            Socks5Reply.NETWORK_UNREACHABLE,
            Socks5Reply.HOST_UNREACHABLE,
            Socks5Reply.CONNECTION_REFUSED,
            Socks5Reply.TTL_EXPIRED,
        ):
            assert _classify_rep(rep) is FailureKind.ROUTE_ERROR

    def test_capability_mismatch(self) -> None:
        kind = _classify_rep(Socks5Reply.ADDRESS_TYPE_NOT_SUPPORTED)
        assert kind is FailureKind.CAPABILITY_MISMATCH

    def test_upstream_errors(self) -> None:
        for rep in (
            Socks5Reply.GENERAL_FAILURE,
            Socks5Reply.RULE_FORBIDDEN,
            Socks5Reply.COMMAND_NOT_SUPPORTED,
            0x09,  # 未分配代码
        ):
            assert _classify_rep(rep) is FailureKind.UPSTREAM_ERROR


class TestSocks5Handshake:
    async def test_handshake_anonymous_success(self) -> None:
        mock_server = await start_mock_socks5()
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            await socks5_handshake(
                reader, writer, target("example.com", 80), auth=None, timeout=2.0
            )
            assert len(mock_server.received_targets) == 1
            assert mock_server.received_targets[0] == ("example.com", 80)

            # 隧道就绪，可以进行通信
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_handshake_auth_success(self) -> None:
        mock_server = await start_mock_socks5(auth=("admin", "secret123"))
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        auth = UpstreamAuth(username="admin", password="secret123")
        try:
            await socks5_handshake(
                reader, writer, target("api.service.local", 443), auth=auth, timeout=2.0
            )
            assert mock_server.auth_attempts == [("admin", "secret123")]
            assert mock_server.received_targets == [("api.service.local", 443)]
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_handshake_auth_required_but_not_provided(self) -> None:
        # 服务端强制要求认证，客户端未配 auth (发送 METHODS=[0x00])
        mock_server = await start_mock_socks5(auth=("admin", "secret123"))
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("example.com", 80), auth=None, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.UPSTREAM_ERROR
            assert exc_info.value.error == "NO_ACCEPTABLE_AUTH"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_handshake_auth_failed(self) -> None:
        mock_server = await start_mock_socks5(auth=("admin", "secret123"))
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        auth = UpstreamAuth(username="admin", password="wrong_password")
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("example.com", 80), auth=auth, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.UPSTREAM_ERROR
            assert exc_info.value.error == "AUTH_FAILED"
            # 确认错误信息绝不泄露密码明文
            assert "wrong_password" not in str(exc_info.value)
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_handshake_rep_connection_refused_is_route_error(self) -> None:
        # SOCKS5 代理返回 0x05 (Connection Refused)，必须归为 ROUTE_ERROR
        mock_server = await start_mock_socks5(rep=Socks5Reply.CONNECTION_REFUSED)
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("blocked.host", 80), auth=None, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.ROUTE_ERROR
            assert exc_info.value.error == "SOCKS5_REP_05"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_handshake_rep_general_failure_is_upstream_error(self) -> None:
        # SOCKS5 代理返回 0x01 (General Failure)，必须归为 UPSTREAM_ERROR 计入熔断
        mock_server = await start_mock_socks5(rep=Socks5Reply.GENERAL_FAILURE)
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("example.com", 80), auth=None, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.UPSTREAM_ERROR
            assert exc_info.value.error == "SOCKS5_REP_01"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_two_phase_parsing_prevents_misclassification_on_premature_disconnect(
        self,
    ) -> None:
        """核心契约测试：两阶段防吞错机制。

        若非标代理在返回 REP=0x05 拒绝后立即掐断连接 (EOF)，
        客户端必须依然判定为 ROUTE_ERROR，绝不能被 IncompleteRead 吞为 UPSTREAM_ERROR。
        """
        mock_server = await start_mock_socks5(
            rep=Socks5Reply.CONNECTION_REFUSED, disconnect_after_rep=True
        )
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("deadhost.org", 80), auth=None, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.ROUTE_ERROR
            assert exc_info.value.error == "SOCKS5_REP_05"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_handshake_timeout(self) -> None:
        mock_server = await start_mock_socks5(hang_greeting=True)
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("example.com", 80), auth=None, timeout=0.1
                )
            assert exc_info.value.kind is FailureKind.UPSTREAM_ERROR
            assert exc_info.value.error == "Socks5HandshakeTimeout"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_bad_socks_version_response(self) -> None:
        mock_server = await start_mock_socks5(bad_greeting_version=True)
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("example.com", 80), auth=None, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.UPSTREAM_ERROR
            assert exc_info.value.error == "BAD_SOCKS_VERSION"
        finally:
            await _close_writer(writer)
            await mock_server.close()

    async def test_premature_disconnect_during_greeting(self) -> None:
        mock_server = await start_mock_socks5(disconnect_on_greeting=True)
        reader, writer = await asyncio.open_connection(mock_server.host, mock_server.port)
        try:
            with pytest.raises(ConnectorError) as exc_info:
                await socks5_handshake(
                    reader, writer, target("example.com", 80), auth=None, timeout=2.0
                )
            assert exc_info.value.kind is FailureKind.UPSTREAM_ERROR
            assert exc_info.value.error == "Socks5IOError"
        finally:
            await _close_writer(writer)
            await mock_server.close()
