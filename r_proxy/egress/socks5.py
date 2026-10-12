"""SOCKS5 客户端协议实现。

对应设计：docs/design/DD_SOCKS5.md。
遵循零第三方依赖红线，仅使用标准库 struct 与 asyncio。
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
from enum import IntEnum

from r_proxy.config.model import UpstreamAuth
from r_proxy.contracts import ConnectorError, FailureKind, RequestTarget


class Socks5Reply(IntEnum):
    SUCCEEDED = 0x00
    GENERAL_FAILURE = 0x01
    RULE_FORBIDDEN = 0x02
    NETWORK_UNREACHABLE = 0x03
    HOST_UNREACHABLE = 0x04
    CONNECTION_REFUSED = 0x05
    TTL_EXPIRED = 0x06
    COMMAND_NOT_SUPPORTED = 0x07
    ADDRESS_TYPE_NOT_SUPPORTED = 0x08


def _classify_rep(rep: int) -> FailureKind:
    """根据 SOCKS5 REP 响应码归类失败类型。

    映射规则详见 docs/design/DD_SOCKS5.md §3：
    - 0x03..0x06 为目标网络/主机/端口问题，只记目标负面记忆 (ROUTE_ERROR)。
    - 0x08 为地址类型不支持 (CAPABILITY_MISMATCH)，不记熔断不记负面记忆。
    - 其余 (0x01, 0x02, 0x07 及未分配代码) 归为上级代理故障或限制 (UPSTREAM_ERROR)。
    """
    if rep in (
        Socks5Reply.NETWORK_UNREACHABLE,
        Socks5Reply.HOST_UNREACHABLE,
        Socks5Reply.CONNECTION_REFUSED,
        Socks5Reply.TTL_EXPIRED,
    ):
        return FailureKind.ROUTE_ERROR
    if rep == Socks5Reply.ADDRESS_TYPE_NOT_SUPPORTED:
        return FailureKind.CAPABILITY_MISMATCH
    return FailureKind.UPSTREAM_ERROR


def build_connect_request(target: RequestTarget) -> bytes:
    """构造 SOCKS5 CONNECT 请求报文。

    报文格式 (RFC 1928 §4):
    +----+-----+-------+------+----------+----------+
    |VER | CMD |  RSV  | ATYP | DST.ADDR | DST.PORT |
    +----+-----+-------+------+----------+----------+
    | 1  |  1  | X'00' |  1   | Variable |    2     |
    +----+-----+-------+------+----------+----------+
    """
    clean_host = target.host.strip("[]")
    port = target.port

    try:
        ip = ipaddress.ip_address(clean_host)
        if ip.version == 4:
            return (
                struct.pack("!BBBB", 0x05, 0x01, 0x00, 0x01)
                + ip.packed
                + struct.pack("!H", port)
            )
        return (
            struct.pack("!BBBB", 0x05, 0x01, 0x00, 0x04)
            + ip.packed
            + struct.pack("!H", port)
        )
    except ValueError:
        try:
            encoded_host = clean_host.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ConnectorError(FailureKind.CAPABILITY_MISMATCH, "DomainNotAscii") from exc

        if len(encoded_host) > 255:
            raise ConnectorError(FailureKind.CAPABILITY_MISMATCH, "DomainTooLong") from None

        return (
            struct.pack("!BBBB", 0x05, 0x01, 0x00, 0x03)
            + struct.pack("!B", len(encoded_host))
            + encoded_host
            + struct.pack("!H", port)
        )


async def _negotiate_auth(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    auth: UpstreamAuth | None,
) -> None:
    """执行 RFC 1928 / RFC 1929 认证协商。"""
    if auth is None:
        # 仅无需认证: VER=0x05, NMETHODS=1, METHODS=[0x00]
        writer.write(b"\x05\x01\x00")
    else:
        # 支持无需认证与用户名/密码认证: VER=0x05, NMETHODS=2, METHODS=[0x00, 0x02]
        writer.write(b"\x05\x02\x00\x02")
    await writer.drain()

    # 读取服务端选择的方法: VER (1B) + METHOD (1B)
    choice = await reader.readexactly(2)
    ver, method = struct.unpack("!BB", choice)
    if ver != 0x05:
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "BAD_SOCKS_VERSION")

    if method == 0xFF:
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "NO_ACCEPTABLE_AUTH")
    if method == 0x00:
        return
    if method == 0x02:
        if auth is None:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, "AUTH_REQUIRED")

        uname = auth.username.encode("utf-8")
        passwd = auth.password.encode("utf-8")
        if len(uname) > 255 or len(passwd) > 255:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, "CREDENTIAL_TOO_LONG")

        # RFC 1929 认证请求: VER=0x01, ULEN, UNAME, PLEN, PASSWD
        auth_req = (
            struct.pack("!BB", 0x01, len(uname))
            + uname
            + struct.pack("!B", len(passwd))
            + passwd
        )
        writer.write(auth_req)
        await writer.drain()

        # 读取 RFC 1929 认证应答: VER=0x01, STATUS
        auth_resp = await reader.readexactly(2)
        subver, status = struct.unpack("!BB", auth_resp)
        if subver != 0x01 or status != 0x00:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, "AUTH_FAILED")
        return

    raise ConnectorError(FailureKind.UPSTREAM_ERROR, "UNSUPPORTED_AUTH_METHOD")


async def read_connect_reply(reader: asyncio.StreamReader) -> None:
    """读取并解析 SOCKS5 CONNECT 应答。

    两阶段防吞错机制（详见 docs/design/DD_SOCKS5.md §4.3）：
    1. 阶段一：原子读取前 4 字节，第一时间捕获 REP 响应码。
    2. 阶段二：读取服务端返回的 BND 绑定地址与端口，对齐传输流。
       部分非标代理在建连失败（如 rep=0x05）时会立即掐断连接 (EOF)。
       若此时抛出 IncompleteReadError，绝不能覆盖已解析出的 rep 归类，否则会导致误熔断。
    """
    head = await reader.readexactly(4)
    ver, rep, _rsv, atyp = struct.unpack("!BBBB", head)
    if ver != 0x05:
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "BAD_SOCKS_VERSION")

    try:
        if atyp == 0x01:  # IPv4 (4 字节地址 + 2 字节端口)
            await reader.readexactly(4 + 2)
        elif atyp == 0x03:  # 域名 (1 字节长度 + N 字节域名 + 2 字节端口)
            dlen = (await reader.readexactly(1))[0]
            await reader.readexactly(dlen + 2)
        elif atyp == 0x04:  # IPv6 (16 字节地址 + 2 字节端口)
            await reader.readexactly(16 + 2)
        else:
            if rep == Socks5Reply.SUCCEEDED:
                raise ConnectorError(FailureKind.UPSTREAM_ERROR, "BAD_ATYP_IN_REPLY")
    except (asyncio.IncompleteReadError, OSError) as exc:
        if rep == Socks5Reply.SUCCEEDED:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, "IncompleteReadInBindAddr") from exc

    if rep != Socks5Reply.SUCCEEDED:
        kind = _classify_rep(rep)
        raise ConnectorError(kind, f"SOCKS5_REP_{rep:02x}")


async def socks5_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target: RequestTarget,
    auth: UpstreamAuth | None,
    timeout: float,
) -> None:
    """在已连通的 TCP 套接字上执行 SOCKS5 握手并连通目标。

    :raises ConnectorError: 握手或连通失败，携带规范归类的 FailureKind。
    """
    try:
        async with asyncio.timeout(timeout):
            await _negotiate_auth(reader, writer, auth)
            req = build_connect_request(target)
            writer.write(req)
            await writer.drain()
            await read_connect_reply(reader)
    except TimeoutError as exc:
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "Socks5HandshakeTimeout") from exc
    except ConnectorError:
        raise
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "Socks5IOError") from exc
