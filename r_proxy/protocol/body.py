"""请求体的边界判定与流式读取。

对应设计：docs/design/DD_PROXY.md §3.5。

请求体**分块读取**而非一次 ``readexactly(n)``：``Content-Length`` 可能声明
1GB，一次读入会直接耗尽内存。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from r_proxy.contracts import Headers
from r_proxy.protocol.parse import BadRequest

CHUNK_SIZE = 65536
MAX_CHUNK_LINE = 8192

BodyKind = Literal["none", "length", "chunked"]


@dataclass(frozen=True, slots=True)
class BodyPlan:
    kind: BodyKind
    length: int | None


class BodyReadTimeout(TimeoutError):
    """客户端请求体未在每次读取期限内继续发送。"""


def plan_body(headers: Headers) -> BodyPlan:
    """依据框架头判定请求体边界。

    同时出现 ``Content-Length`` 与 ``Transfer-Encoding`` 必须拒绝：这是 HTTP
    请求走私的经典入口——代理按其中一个解释边界、上游按另一个解释，攻击者
    可以把第二个请求偷渡进去。RFC 9112 §6.1 允许拒绝，作为代理直接拒绝。
    """
    lengths = headers.get_all("content-length")
    encodings = headers.get_all("transfer-encoding")

    if lengths and encodings:
        raise BadRequest("Content-Length 与 Transfer-Encoding 不能同时出现（请求走私风险）")

    if encodings:
        if len(encodings) > 1 or encodings[0].strip().lower() != "chunked":
            raise BadRequest(f"不支持的 Transfer-Encoding: {encodings[0]!r}")
        return BodyPlan(kind="chunked", length=None)

    if not lengths:
        return BodyPlan(kind="none", length=0)

    if len({v.strip() for v in lengths}) > 1:
        raise BadRequest("Content-Length 出现多次且值不同（请求走私风险）")

    raw = lengths[0].strip()
    if not raw.isdigit():
        raise BadRequest(f"Content-Length 不是非负整数: {raw!r}")
    length = int(raw)
    return BodyPlan(kind="none", length=0) if length == 0 else BodyPlan("length", length)


async def stream_body(
    reader: asyncio.StreamReader,
    sink: Callable[[bytes], object],
    plan: BodyPlan,
    *,
    idle_timeout: float | None = None,
) -> int:
    """按 ``plan`` 从 ``reader`` 读出请求体，逐块交给 ``sink``。返回总字节数。"""
    if plan.kind == "none":
        return 0
    if plan.kind == "length":
        assert plan.length is not None
        return await _stream_fixed(reader, sink, plan.length, idle_timeout)
    return await _stream_chunked(reader, sink, idle_timeout)


async def _stream_fixed(
    reader: asyncio.StreamReader,
    sink: Callable[[bytes], object],
    length: int,
    idle_timeout: float | None,
) -> int:
    remaining = length
    while remaining > 0:
        chunk = await _read_with_timeout(reader.read(min(CHUNK_SIZE, remaining)), idle_timeout)
        if not chunk:
            raise EOFError(f"请求体在读满前结束，还差 {remaining} 字节")
        result = sink(chunk)
        if inspect.isawaitable(result):
            await result
        remaining -= len(chunk)
    return length


async def _stream_chunked(
    reader: asyncio.StreamReader, sink: Callable[[bytes], object], idle_timeout: float | None
) -> int:
    """原样转发 chunked 编码。

    不解码再重新编码：上游同样按 chunked 解析，逐字节转发既省一次编解码，
    也避免我们在重新编码时改变分块边界（某些上游对此敏感）。
    """
    total = 0
    while True:
        line = await _read_line(reader, idle_timeout)
        total += len(line)
        result = sink(line)
        if inspect.isawaitable(result):
            await result

        size_field = line[:-2].split(b";", 1)[0].strip()
        try:
            size = int(size_field, 16)
        except ValueError as exc:
            raise BadRequest(f"chunk 长度不是十六进制: {size_field[:40]!r}") from exc
        if size < 0:
            raise BadRequest("chunk 长度为负")

        if size == 0:
            total += await _forward_trailer(reader, sink, idle_timeout)
            return total

        remaining = size + 2  # 数据 + 结尾的 CRLF
        while remaining > 0:
            data = await _read_with_timeout(reader.read(min(CHUNK_SIZE, remaining)), idle_timeout)
            if not data:
                raise EOFError("chunked 请求体在读满前结束")
            result = sink(data)
            if inspect.isawaitable(result):
                await result
            total += len(data)
            remaining -= len(data)


async def _forward_trailer(
    reader: asyncio.StreamReader, sink: Callable[[bytes], object], idle_timeout: float | None
) -> int:
    """转发终止 chunk 之后的 trailer 段，直到空行。"""
    total = 0
    while True:
        line = await _read_line(reader, idle_timeout)
        result = sink(line)
        if inspect.isawaitable(result):
            await result
        total += len(line)
        if line == b"\r\n":
            return total


async def _read_line(reader: asyncio.StreamReader, idle_timeout: float | None = None) -> bytes:
    try:
        line = await _read_with_timeout(reader.readuntil(b"\r\n"), idle_timeout)
    except BodyReadTimeout:
        raise
    except asyncio.IncompleteReadError as exc:
        raise EOFError("chunked 请求体在读满前结束") from exc
    except asyncio.LimitOverrunError as exc:
        # 必须显式列出：它只继承 `Exception`，不是 `ValueError` 的子类，漏掉它
        # 异常会一路逃出连接处理函数，客户端收不到任何响应。chunk 长度行超过
        # reader 的 limit（64KB）时触发——与下面的长度检查是同一类问题，
        # 只是先被 reader 拦下了。
        raise BadRequest("chunk 长度行过长") from exc
    except ValueError as exc:
        raise BadRequest("chunk 长度行过长") from exc
    if len(line) > MAX_CHUNK_LINE:
        raise BadRequest("chunk 长度行过长")
    return line


async def _read_with_timeout(awaitable: Awaitable[bytes], idle_timeout: float | None) -> bytes:
    if idle_timeout is None:
        return await awaitable
    try:
        async with asyncio.timeout(idle_timeout):
            return await awaitable
    except TimeoutError as exc:
        raise BodyReadTimeout from exc
