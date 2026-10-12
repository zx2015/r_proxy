"""轻量级 SOCKS5 模拟服务端。

用于单元测试与集成测试，覆盖：
- 匿名握手与 RFC 1929 用户名密码认证
- 各种异常与错误分支 (REP != 0x00, METHOD == 0xFF, AUTH_FAILED, 握手超时, 连接重置等)
- 两阶段防吞错机制验证 (发送 4 字节应答头后立即掐断连接)
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from tests.conftest import FakeServer, start_server


@dataclass
class MockSocks5Server:
    """可编程配置的 Mock SOCKS5 服务端。"""

    fake: FakeServer
    # 配置选项
    expected_auth: tuple[str, str] | None = None
    rep: int = 0x00
    bnd_addr: str = "127.0.0.1"
    bnd_port: int = 1080
    hang_greeting: bool = False
    hang_connect: bool = False
    bad_greeting_version: bool = False
    bad_connect_version: bool = False
    disconnect_after_rep: bool = False
    disconnect_on_greeting: bool = False
    corrupt_auth_response: bool = False
    custom_data_handler: (
        Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]] | None
    ) = None

    # 运行期记录
    received_targets: list[tuple[str, int]] = field(default_factory=list)
    auth_attempts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def host(self) -> str:
        return self.fake.host

    @property
    def port(self) -> int:
        return self.fake.port

    @property
    def address(self) -> str:
        return self.fake.address

    async def close(self) -> None:
        await self.fake.close()


async def start_mock_socks5(
    *,
    auth: tuple[str, str] | None = None,
    rep: int = 0x00,
    disconnect_after_rep: bool = False,
    hang_greeting: bool = False,
    hang_connect: bool = False,
    bad_greeting_version: bool = False,
    bad_connect_version: bool = False,
    disconnect_on_greeting: bool = False,
    corrupt_auth_response: bool = False,
    custom_data_handler: (
        Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]] | None
    ) = None,
) -> MockSocks5Server:
    """启动一个轻量级 Mock SOCKS5 服务端。"""
    mock_instance: MockSocks5Server

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if mock_instance.hang_greeting:
            await asyncio.sleep(3600)
            return

        if mock_instance.disconnect_on_greeting:
            writer.close()
            return

        if mock_instance.bad_greeting_version:
            # 模拟返回非 SOCKS5 响应，例如 HTTP 响应
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        # 1. 协商认证方法
        head = await reader.readexactly(2)
        ver, nmethods = struct.unpack("!BB", head)
        if ver != 0x05:
            writer.write(b"\x00\xFF")
            await writer.drain()
            return

        methods = await reader.readexactly(nmethods)

        if mock_instance.expected_auth is None:
            # 不需要认证
            if 0x00 in methods:
                writer.write(b"\x05\x00")
                await writer.drain()
            else:
                writer.write(b"\x05\xFF")
                await writer.drain()
                return
        else:
            # 需要密码认证
            if 0x02 in methods:
                writer.write(b"\x05\x02")
                await writer.drain()

                if mock_instance.corrupt_auth_response:
                    writer.write(b"\xFF\xFF")
                    await writer.drain()
                    return

                # 读取 RFC 1929 认证请求
                _subver = (await reader.readexactly(1))[0]
                ulen = (await reader.readexactly(1))[0]
                uname = (await reader.readexactly(ulen)).decode("utf-8", errors="replace")
                plen = (await reader.readexactly(1))[0]
                passwd = (await reader.readexactly(plen)).decode("utf-8", errors="replace")
                mock_instance.auth_attempts.append((uname, passwd))

                exp_u, exp_p = mock_instance.expected_auth
                if uname == exp_u and passwd == exp_p:
                    writer.write(b"\x01\x00")  # 认证成功
                    await writer.drain()
                else:
                    writer.write(b"\x01\x01")  # 认证失败
                    await writer.drain()
                    return
            else:
                writer.write(b"\x05\xFF")
                await writer.drain()
                return

        # 2. 读取 CONNECT 请求
        if mock_instance.hang_connect:
            await asyncio.sleep(3600)
            return

        c_head = await reader.readexactly(4)
        c_ver, cmd, _rsv, atyp = struct.unpack("!BBBB", c_head)
        if c_ver != 0x05 or cmd != 0x01:
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return

        target_host: str
        if atyp == 0x01:  # IPv4
            raw_ip = await reader.readexactly(4)
            target_host = str(ipaddress.IPv4Address(raw_ip))
        elif atyp == 0x03:  # Domain
            dlen = (await reader.readexactly(1))[0]
            raw_domain = await reader.readexactly(dlen)
            target_host = raw_domain.decode("ascii", errors="replace")
        elif atyp == 0x04:  # IPv6
            raw_ip6 = await reader.readexactly(16)
            target_host = str(ipaddress.IPv6Address(raw_ip6))
        else:
            writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return

        target_port = struct.unpack("!H", await reader.readexactly(2))[0]
        mock_instance.received_targets.append((target_host, target_port))

        # 3. 发送 CONNECT 响应
        resp_ver = 0x04 if mock_instance.bad_connect_version else 0x05
        rep_code = mock_instance.rep

        if mock_instance.disconnect_after_rep:
            # 仅发送前 4 字节头，随后立即掐断连接 (EOF)
            writer.write(struct.pack("!BBBB", resp_ver, rep_code, 0x00, 0x01))
            await writer.drain()
            writer.close()
            return

        # 正常构建完整响应 (默认 IPv4 绑定 127.0.0.1:1080)
        bnd_ip = ipaddress.IPv4Address(mock_instance.bnd_addr).packed
        reply = (
            struct.pack("!BBBB", resp_ver, rep_code, 0x00, 0x01)
            + bnd_ip
            + struct.pack("!H", mock_instance.bnd_port)
        )
        writer.write(reply)
        await writer.drain()

        if rep_code != 0x00:
            return

        if mock_instance.custom_data_handler is not None:
            await mock_instance.custom_data_handler(reader, writer)
            return

        # 握手成功后，作为一个 Echo 服务或简单透传保持连接
        while not reader.at_eof():
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()

    fake = await start_server(handler)
    mock_instance = MockSocks5Server(
        fake=fake,
        expected_auth=auth,
        rep=rep,
        disconnect_after_rep=disconnect_after_rep,
        hang_greeting=hang_greeting,
        hang_connect=hang_connect,
        bad_greeting_version=bad_greeting_version,
        bad_connect_version=bad_connect_version,
        disconnect_on_greeting=disconnect_on_greeting,
        corrupt_auth_response=corrupt_auth_response,
        custom_data_handler=custom_data_handler,
    )
    return mock_instance
