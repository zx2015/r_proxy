"""复现并验证修复：分块编码响应经代理转发后不能被裁掉 ``Transfer-Encoding``。

背景：`ClientConnection._handle_http` 用 :func:`r_proxy.protocol.relay.pump`
把上游响应体原样转发给客户端，从不解析、不重新编码 ``chunked`` 分块。若响应
头里的 ``Transfer-Encoding`` 被当作逐跳头部摘掉（同时又强制补上
``Connection: close``），客户端会失去唯一能识别分块框架的线索，只能把连接
关闭当成消息边界——于是每个分块前面的十六进制长度行、结尾的 ``0\\r\\n\\r\\n``
终止块，都被当成消息体内容的一部分，实际应用层数据被framing字节污染。

真实场景：浏览器通过 r-proxy 访问某内网 Web 应用（例如下载管理器）的
``/downloads`` 接口，该接口用 ``Transfer-Encoding: chunked`` 返回 JSON。经
代理转发后前端 ``JSON.parse()`` 直接失败，页面呈现「Failed to load this
section. Please try again.」一类的通用错误态。

本测试用一个真实的假上游（真实 TCP 服务端，见 ``tests/conftest.py`` 的
设计理由）返回分块响应，驱动一次完整的 ``Application`` 实例，断言客户端
收到的响应：头部保留 ``Transfer-Encoding: chunked``、分块框架原样透传，且
按分块解码后能还原出与上游一致的应用层数据。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from r_proxy.app import Application
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

# 与真实故障场景一致：动态生成、事先不知道长度的 JSON 响应体。
PAYLOAD = b'{"downloads": [], "total": 0}'
CHUNKED_BODY = f"{len(PAYLOAD):x}".encode() + b"\r\n" + PAYLOAD + b"\r\n0\r\n\r\n"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-8")
    return path


async def _chunked_origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """只会用 ``Transfer-Encoding: chunked``、不声明 ``Content-Length`` 的上游。"""
    await reader.readuntil(b"\r\n\r\n")
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + CHUNKED_BODY
    )
    await writer.drain()
    writer.close()


def _dechunk(body: bytes) -> bytes:
    """按 RFC 9112 §7.1 手动解码分块编码，还原应用层数据。

    真实客户端只有在响应头正确声明 ``Transfer-Encoding: chunked`` 时才会
    这么做；本测试用它验证「假设客户端信任了这个头」的前提下能否正确解码，
    从而确认头部与线上字节框架是一致的。
    """
    out = bytearray()
    rest = body
    while True:
        size_line, _, rest = rest.partition(b"\r\n")
        size = int(size_line.split(b";", 1)[0].strip(), 16)
        if size == 0:
            return bytes(out)
        out += rest[:size]
        rest = rest[size + 2 :]  # 跳过数据与结尾的 CRLF


async def _read_until_closed(reader: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


class TestChunkedResponsePassThrough:
    async def test_transfer_encoding_header_is_preserved(self, tmp_path: Path) -> None:
        """响应头必须保留 ``Transfer-Encoding: chunked``，与实际转发的分块框架一致。"""
        origin = await start_server(_chunked_origin)
        app = Application(config_path=write_config(tmp_path))
        await app.start()
        try:
            host, port = app.proxy_address
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                f"GET http://{origin.address}/downloads HTTP/1.1\r\n"
                f"Host: {origin.address}\r\n\r\n".encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(_read_until_closed(reader), timeout=10)
            writer.close()
        finally:
            await app.stop()

        head, _, body = response.partition(b"\r\n\r\n")
        header_text = head.decode("latin-1").lower()

        assert "transfer-encoding: chunked" in header_text, (
            "Transfer-Encoding 被裁掉，但转发的字节仍是分块编码：客户端会把"
            "分块长度行当成消息体内容解析——这正是「Failed to load this "
            "section」类故障的根因。"
        )
        assert "content-length:" not in header_text, (
            "分块响应不应该同时出现 Content-Length，否则客户端对消息边界的"
            "判断会产生歧义。"
        )

    async def test_body_bytes_pass_through_unmodified_and_decode_correctly(
        self, tmp_path: Path
    ) -> None:
        """转发的分块字节必须与上游发出的完全一致，解码后等于原始 payload。"""
        origin = await start_server(_chunked_origin)
        app = Application(config_path=write_config(tmp_path))
        await app.start()
        try:
            host, port = app.proxy_address
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                f"GET http://{origin.address}/downloads HTTP/1.1\r\n"
                f"Host: {origin.address}\r\n\r\n".encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(_read_until_closed(reader), timeout=10)
            writer.close()
        finally:
            await app.stop()

        _, _, body = response.partition(b"\r\n\r\n")
        assert body == CHUNKED_BODY
        assert _dechunk(body) == PAYLOAD
