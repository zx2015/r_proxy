"""请求行与头部解析、host 规范化、IPv6 字面量处理。

对应设计：docs/design/DD_PROXY.md §3。

所有限制在**读取时**生效，而不是读完再检查——否则客户端一行不换地发 1GB
数据就能耗尽内存。上限由 ``StreamReader`` 的 ``limit`` 在流层面拦截。
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from r_proxy.contracts import AddressFamily, Headers, Method, RequestTarget

MAX_REQUEST_LINE_BYTES = 8192
MAX_HEADER_LINE_BYTES = 8192
MAX_HEAD_BYTES = 65536
MAX_HEADER_COUNT = 100

SCHEME_DEFAULT_PORT = {"http": 80, "https": 443}

# RFC 9110 §7.6.1：代理必须移除逐跳头部，不得转发。
#
# ``transfer-encoding`` **不在此列**：它不是单纯的逐跳元数据，而是描述消息体
# 边界的框架头。请求体（``protocol/body.py``）与响应体（``protocol/relay.py``
# 的 ``pump``）都是把 ``chunked`` 编码的原始字节（分块长度行 + 数据 + 结尾
# CRLF）逐字节转发给下一跳，从不解码再重新编码。若在转发头部时把
# ``Transfer-Encoding`` 摘掉，对端收到的头部会说「这是一条定长或到连接关闭
# 为止的消息」，而线上的字节其实仍然带着分块长度行——对端的 HTTP 解析器会把
# 十六进制长度行当成消息体内容，产生的响应/请求在语义上已损坏（表现为客户端
# 侧 JSON 解析失败、页面局部功能报错）。只要转发逻辑不解码分块编码，这个头
# 就必须原样传递，否则头部与实际字节流互相矛盾。
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        # 非标准但被浏览器广泛使用，同样必须终止于代理。
        "proxy-connection",
    }
)


class ProtocolError(Exception):
    """客户端请求无法处理。子类决定返回哪个状态码。"""


class BadRequest(ProtocolError):
    """400。"""


class RequestLineTooLong(ProtocolError):
    """414。"""


class HeaderTooLarge(ProtocolError):
    """431。"""


class ClientDisconnected(ProtocolError):
    """客户端在请求头读完前断开。不返回响应，直接关闭。"""


@dataclass(frozen=True, slots=True)
class RawHead:
    method: str
    target: str
    version: str
    headers: Headers


async def read_head(reader: asyncio.StreamReader, *, timeout: float) -> RawHead:
    """读取并解析完整请求头。

    :raises TimeoutError: 超时未读到完整头部（对应 ``408``）。
    :raises HeaderTooLarge: 超过 reader 的 limit（对应 ``431``）。
    :raises ClientDisconnected: 头部读完前连接断开。
    """
    try:
        async with asyncio.timeout(timeout):
            data = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise HeaderTooLarge(f"请求头超过 {MAX_HEAD_BYTES} 字节上限") from exc
    except ValueError as exc:
        # 防御性兜底：``asyncio.LimitOverrunError`` 在当前 Python 版本下不是
        # ``ValueError`` 的子类（已用 ``issubclass()`` 验证），上一条分支已经
        # 单独接住它；这里接的是 ``readuntil`` 理论上可能抛出的其他
        # ``ValueError``（如实现变化导致的边界情形），而不是它的子类。
        raise HeaderTooLarge(f"请求头超过 {MAX_HEAD_BYTES} 字节上限") from exc
    except asyncio.IncompleteReadError as exc:
        raise ClientDisconnected("客户端在请求头读完前断开") from exc
    return parse_head(data)


def parse_head(data: bytes) -> RawHead:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BadRequest("请求头必须是 ASCII") from exc

    lines = text.split("\r\n")
    # 按字面 "\r\n" 切分后，任何一行仍残留裸露的 \r 或 \n，说明存在没有配对
    # 成行终止符的控制字符——例如目标里嵌了单独的 \n。这类字段会被后续原样
    # 拼进转发给出口/上级代理的请求（CONNECT 行、Host 头等），不挡住就是一次
    # HTTP 请求走私/头部注入。
    if any("\r" in line or "\n" in line for line in lines):
        raise BadRequest("请求头包含裸露的 \\r 或 \\n")
    method, target, version = _parse_request_line(lines[0])

    header_lines = [line for line in lines[1:] if line]
    if len(header_lines) > MAX_HEADER_COUNT:
        raise HeaderTooLarge(f"头部数量超过 {MAX_HEADER_COUNT} 条上限")

    items: list[tuple[str, str]] = []
    for line in header_lines:
        if len(line) > MAX_HEADER_LINE_BYTES:
            raise HeaderTooLarge(f"单个头部行超过 {MAX_HEADER_LINE_BYTES} 字节上限")
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise BadRequest(f"头部格式非法: {line[:80]!r}")
        items.append((name, value.strip()))

    return RawHead(method=method, target=target, version=version, headers=Headers(items))


def _parse_request_line(line: str) -> tuple[str, str, str]:
    if len(line) > MAX_REQUEST_LINE_BYTES:
        raise RequestLineTooLong(f"请求行超过 {MAX_REQUEST_LINE_BYTES} 字节上限")
    parts = line.split(" ")
    if len(parts) != 3:
        raise BadRequest(f"请求行格式非法: {line[:80]!r}")
    method, target, version = parts
    if not method or not target or not version.startswith("HTTP/"):
        raise BadRequest(f"请求行格式非法: {line[:80]!r}")
    return method, target, version


def normalize_host(raw: str) -> str:
    """归一化 host，供规则匹配、粘性键、负面记忆键、日志共用。

    必须在此处一次性完成：分散归一化必然产生不一致的键，导致同一目标
    的记忆分裂到多条记录上（DD_RULES §5.5）。
    """
    host = raw.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


def parse_target(method: str, target: str, headers: Headers) -> RequestTarget:
    """从请求行与头部推导路由决策所需的目标信息。"""
    if method.upper() == "CONNECT":
        host, port = _split_authority(target, default_port=None)
        if port is None:
            raise BadRequest("CONNECT 目标必须是 host:port")
        url = None
    elif "://" in target:
        parsed = urlsplit(target)
        default_port = SCHEME_DEFAULT_PORT.get(parsed.scheme.lower())
        if default_port is None:
            raise BadRequest(f"不支持的协议: {parsed.scheme!r}")
        host, port = _split_authority(parsed.netloc, default_port=default_port)
        url = target
    else:
        # origin-form：请求行只有路径，权威 host 来自 Host 头。
        host_header = headers.get("host")
        if host_header is None:
            raise BadRequest("origin-form 请求缺少 Host 头")
        host, port = _split_authority(host_header, default_port=80)
        url = f"http://{host_header}{target}"

    if port is None:
        raise BadRequest("无法确定目标端口")

    normalized = normalize_host(host)
    return RequestTarget(
        host=normalized,
        port=port,
        method=Method.parse(method),
        url=url,
        is_connect=method.upper() == "CONNECT",
        family=AddressFamily.of_literal(normalized),
    )


def _split_authority(authority: str, *, default_port: int | None) -> tuple[str, int | None]:
    """拆分 ``host:port``。无方括号的 IPv6 一律拒绝。"""
    text = authority.strip()
    if not text:
        raise BadRequest("目标 authority 为空")

    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise BadRequest("IPv6 地址缺少右方括号")
        host = text[1:end]
        rest = text[end + 1 :]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise BadRequest(f"方括号内不是合法的 IPv6 地址: {host!r}") from exc
        if not rest:
            return host, default_port
        if not rest.startswith(":"):
            raise BadRequest(f"方括号后存在非法字符: {rest!r}")
        return host, _parse_port(rest[1:])

    if text.count(":") > 1:
        # `2001:db8::1:443` 中最后一段既可能是端口也可能是地址的一部分。
        # 猜错会连到完全不同的地址，且失败信息毫无提示价值——只能拒绝。
        raise BadRequest(f"IPv6 地址必须使用方括号，如 [2001:db8::1]:443（收到 {text!r}）")

    if ":" in text:
        host, _, raw_port = text.partition(":")
        if not host:
            raise BadRequest("目标 host 为空")
        return host, _parse_port(raw_port)

    return text, default_port


def _parse_port(raw: str) -> int:
    if not raw.isdigit():
        raise BadRequest(f"端口不是数字: {raw!r}")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise BadRequest(f"端口超出 1..65535: {port}")
    return port


def strip_hop_by_hop(headers: Headers) -> Headers:
    """移除逐跳头部。

    ``Connection`` 头中列出的字段同样是逐跳的，漏掉会把本应终止于代理的
    头部转发出去。``proxy-authorization`` 在移除之列，这同时满足了「不记录
    敏感头」——它在此处即被丢弃，不会进入后续流程或日志。
    """
    drop = set(HOP_BY_HOP)
    for value in headers.get_all("connection"):
        for token in value.split(","):
            if stripped := token.strip().lower():
                drop.add(stripped)
    return Headers((k, v) for k, v in headers.items() if k.lower() not in drop)
