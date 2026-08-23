"""建立到出口的连接：``direct`` 直连目标，``http`` 经上级代理。

对应设计：docs/design/DD_PROXY.md §7。

连接器只负责「把连接建起来」并对传输层失败做**初步**归类。收到 HTTP 响应
后的最终归类需要请求上下文（是不是 CONNECT、是不是 direct），由
``decision.switching`` 决定。
"""

from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass, field

from r_proxy.config.model import RoutingConfig, UpstreamConfig
from r_proxy.contracts import FailureKind, Headers, RequestTarget

# 明确表示地址族不可达。归为 CAPABILITY_MISMATCH：记入日志便于诊断，
# 但不写负面记忆、不累加熔断计数（PRD §4.3.6）。
CAPABILITY_ERRNOS = frozenset(
    {
        errno.ENETUNREACH,
        errno.EAFNOSUPPORT,
        # EHOSTUNREACH 也可能是目标真的下线。归入此类的后果是「不记负面记忆」，
        # 即下次仍会尝试同一出口，代价是偶尔白试一次；反过来误记负面记忆会让
        # 一个健康出口被拉黑 route_block_ttl 秒。取不污染记忆这一边。
        errno.EHOSTUNREACH,
    }
)

MAX_HANDSHAKE_BYTES = 8192


class ConnectorError(Exception):
    """无法建立到出口的连接。

    ``error`` 只存异常类型名或 errno 名，**不含地址**——它可能被拼进
    响应体，泄露内网拓扑（ARCH「跨层数据契约」约定）。
    """

    def __init__(self, kind: FailureKind, error: str) -> None:
        super().__init__(f"出口连接失败: {error}")
        self.kind = kind
        self.error = error


@dataclass(slots=True)
class UpstreamConn:
    """已建立的出口连接。"""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer_host: str
    peer_port: int

    async def close(self) -> None:
        if self.writer.is_closing():
            return
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            # 对端已 RST 时 wait_closed 会抛；连接已经没了，无需处理。
            pass


@dataclass(slots=True)
class TunnelConn:
    """CONNECT 隧道。``status`` 非 200 时隧道未建立，但连接仍需关闭。"""

    conn: UpstreamConn
    status: int
    headers: Headers = field(default_factory=Headers)

    @property
    def established(self) -> bool:
        return 200 <= self.status < 300

    @property
    def reader(self) -> asyncio.StreamReader:
        return self.conn.reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        return self.conn.writer

    async def close(self) -> None:
        await self.conn.close()


def classify_transport(exc: BaseException, *, is_direct: bool) -> FailureKind:
    """对传输层异常做初步归类。

    连接对象决定归类：``direct`` 连的是**目标**，连不上说明这条路走不通
    （``route_error``，且 direct 永不熔断）；上级代理连的是**代理自己**，
    连不上说明这个出口整体不可用（``upstream_error``，计入熔断）。
    只看 errno 无法区分二者——同一个 ``ECONNREFUSED`` 在两种场景下含义相反。
    """
    if isinstance(exc, OSError) and exc.errno in CAPABILITY_ERRNOS:
        return FailureKind.CAPABILITY_MISMATCH
    return FailureKind.ROUTE_ERROR if is_direct else FailureKind.UPSTREAM_ERROR


def error_name(exc: BaseException) -> str:
    """异常的稳定标识，不含地址。"""
    if isinstance(exc, OSError) and exc.errno is not None:
        return errno.errorcode.get(exc.errno, f"errno{exc.errno}")
    return type(exc).__name__


class UpstreamConnector:
    async def connect(
        self, upstream: UpstreamConfig, target: RequestTarget, routing: RoutingConfig
    ) -> UpstreamConn:
        """为普通 HTTP 转发建立连接。

        ``direct`` 连目标本身；``http`` 连上级代理，目标写在请求行里。
        """
        timeout = upstream.connect_timeout or routing.connect_timeout
        if upstream.is_direct:
            return await self._open(target.host, target.port, timeout, routing, is_direct=True)
        host, port = self._upstream_endpoint(upstream)
        return await self._open(host, port, timeout, routing, is_direct=False)

    async def connect_tunnel(
        self, upstream: UpstreamConfig, target: RequestTarget, routing: RoutingConfig
    ) -> TunnelConn:
        """为 CONNECT 建立隧道。

        直连时没有上级代理可握手，``status`` 人为置 200，好让上层的切换
        逻辑不必区分两种情况。
        """
        timeout = upstream.connect_timeout or routing.connect_timeout
        if upstream.is_direct:
            conn = await self._open(target.host, target.port, timeout, routing, is_direct=True)
            return TunnelConn(conn=conn, status=200)

        host, port = self._upstream_endpoint(upstream)
        conn = await self._open(host, port, timeout, routing, is_direct=False)
        try:
            status, headers = await self._handshake(conn, target, timeout)
        except BaseException:
            await conn.close()
            raise
        return TunnelConn(conn=conn, status=status, headers=headers)

    # ----------------------------------------------------------------------

    @staticmethod
    def _upstream_endpoint(upstream: UpstreamConfig) -> tuple[str, int]:
        endpoint = upstream.endpoint
        if endpoint is None:
            # 启动校验（E_ADDRESS_FORMAT）本应拦下，走到这里说明配置绕过了校验。
            # 归为 CAPABILITY_MISMATCH：这不是网络故障，熔断计数没有意义。
            raise ConnectorError(FailureKind.CAPABILITY_MISMATCH, "BAD_UPSTREAM_ADDRESS")
        return endpoint

    async def _open(
        self, host: str, port: int, timeout: float, routing: RoutingConfig, *, is_direct: bool
    ) -> UpstreamConn:
        """RFC 8305 用标准库自带实现（Python 3.8+），不自己写：自己写「并行发起、
        延迟启动第二族、取消落败任务」只会在取消与异常传播的边角引入 bug。

        对上级代理连接同样生效——它解决的是「域名有多个地址、某些路径不通」，
        与出口是 ``direct`` 还是上级代理无关；上级代理地址是 IP 字面量时这两个
        参数本就是空操作，不值得为此加分支。
        """
        try:
            async with asyncio.timeout(timeout):
                delay = routing.happy_eyeballs_delay
                if delay > 0:
                    # interleave=1 必须显式传：不传时候选地址仍按 getaddrinfo
                    # 原序排列，同族地址会连成一片，第二族要等前面全试过才轮到。
                    reader, writer = await asyncio.open_connection(
                        host, port, happy_eyeballs_delay=delay, interleave=1
                    )
                else:
                    # 退化为按 getaddrinfo 返回顺序串行尝试。
                    reader, writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError) as exc:
            raise ConnectorError(
                classify_transport(exc, is_direct=is_direct), error_name(exc)
            ) from exc
        return UpstreamConn(reader=reader, writer=writer, peer_host=host, peer_port=port)

    async def _handshake(
        self, conn: UpstreamConn, target: RequestTarget, timeout: float
    ) -> tuple[int, Headers]:
        authority = target.authority
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Proxy-Connection: keep-alive\r\n"
            f"\r\n"
        ).encode("ascii")

        try:
            conn.writer.write(request)
            await conn.writer.drain()
            async with asyncio.timeout(timeout):
                raw = await conn.reader.readuntil(b"\r\n\r\n")
        # TimeoutError 是 OSError 的子类，必须排在前面，否则永远走不到。
        except TimeoutError as exc:
            # TCP 已经连上代理，它却一个字节都不回：代理是活的，卡在它到目标
            # 那一段。归 upstream_error 会让被墙的目标把健康出口整个熔断，
            # 连带其余目标一起不可用（DD_SWITCHING §8 同一条理由）。
            raise ConnectorError(FailureKind.ROUTE_ERROR, error_name(exc)) from exc
        except OSError as exc:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, error_name(exc)) from exc
        except asyncio.IncompleteReadError as exc:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, "IncompleteRead") from exc
        except ValueError as exc:
            raise ConnectorError(FailureKind.UPSTREAM_ERROR, "HandshakeTooLarge") from exc

        return _parse_handshake_response(raw)


def _parse_handshake_response(raw: bytes) -> tuple[int, Headers]:
    """解析上级代理对 CONNECT 的应答。

    任何无法解析的应答都归为 ``UPSTREAM_ERROR``：能收到字节说明连接是通的，
    但对端不是一个正常工作的 HTTP 代理。

    用 ``latin-1`` 而非 ``ascii`` 解码：单字节编码对任何字节序列都不会抛异常，
    与 ``protocol/connection.py`` 解析普通响应头的方式一致。少数非标代理会在
    ``Server`` 等头里塞非 ASCII 字符，用 ``ascii`` 严格解码会让整次握手直接判
    为 ``UPSTREAM_ERROR``，进而计入全局熔断——代理本身是通的，不该因为一个
    头的字节而被拉黑。
    """
    if len(raw) > MAX_HANDSHAKE_BYTES:
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "HandshakeTooLarge")
    text = raw.decode("latin-1")

    lines = text.split("\r\n")
    parts = lines[0].split()
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise ConnectorError(FailureKind.UPSTREAM_ERROR, "MalformedStatusLine")

    items: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if sep:
            items.append((name.strip(), value.strip()))
    return int(parts[1]), Headers(items)
