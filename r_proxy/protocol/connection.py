"""单个客户端连接的生命周期。

对应设计：docs/design/DD_PROXY.md §4、§5、§6、§9。

这里是决策层与执行层的接入点：路由产出候选链，执行器沿链驱动，本模块只
负责「一次尝试」的协议细节，以及把最终结果交给客户端。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from r_proxy.config.model import ConfigSnapshot, UpstreamConfig
from r_proxy.contracts import AddressFamily, FailureKind, Headers, RequestTarget
from r_proxy.decision.model import AttemptOutcome, Decision, SwitchContext
from r_proxy.decision.router import Router
from r_proxy.egress.connector import ConnectorError, TunnelConn, UpstreamConn, UpstreamConnector
from r_proxy.egress.executor import AttemptExecutor, AttemptResult
from r_proxy.protocol.body import BodyPlan, plan_body, stream_body
from r_proxy.protocol.parse import (
    BadRequest,
    ClientDisconnected,
    HeaderTooLarge,
    ProtocolError,
    RawHead,
    RequestLineTooLong,
    parse_target,
    read_head,
    strip_hop_by_hop,
)
from r_proxy.protocol.relay import DEFAULT_IDLE_TIMEOUT, RelayStats, pump, relay_bidirectional
from r_proxy.protocol.replay import ReplayBuffer
from r_proxy.rules.model import RuleSet

logger = logging.getLogger(__name__)

DEFAULT_HEAD_READ_TIMEOUT = 30.0

# 1xx 中间响应的最大连续跳数。真实场景最多见到一次 100 Continue，
# 上限只为防御性地拒绝行为异常（或恶意）的上游无限吐 1xx。
MAX_INFORMATIONAL_HOPS = 8

HTTP_REASON = {
    400: "Bad Request",
    408: "Request Timeout",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

# 响应体文本只能来自这个集合。绝不拼接异常信息、出口名称或地址
# （PRD §4.3.9）——那会把内网拓扑回显给客户端。
MSG_CHAIN_EXHAUSTED = "所有可用出口均未能完成该请求。"
MSG_NO_UPSTREAM = "没有可用的出口。"
MSG_BAD_REQUEST = "请求格式不正确。"
MSG_HEAD_TIMEOUT = "读取请求头超时。"
MSG_TOO_LARGE = "请求头超出上限。"
MSG_OVERLOADED = "服务器连接数已达上限。"


def new_request_id() -> str:
    return secrets.token_hex(8)


@dataclass(slots=True)
class HttpAttempt:
    """一次成功读到响应头的 HTTP 尝试。响应体尚未转发。"""

    conn: UpstreamConn
    raw_head: bytes
    status: int
    headers: Headers
    # 请求侧已经发往出口的字节数（请求行+头部+body），设计中 v2.2.0
    # （DD_PROXY §5.2.2）：用于最终交付后的流量统计，不参与判据。
    bytes_up: int = 0


class ClientConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        snapshot: ConfigSnapshot,
        rule_set: RuleSet,
        connector: UpstreamConnector,
        router: Router,
        executor: AttemptExecutor,
        *,
        head_read_timeout: float = DEFAULT_HEAD_READ_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        client_addr: str | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._cfg = snapshot
        self._rules = rule_set
        self._connector = connector
        self._router = router
        self._executor = executor
        self._head_read_timeout = head_read_timeout
        self._idle_timeout = idle_timeout
        # 采集点在 ``ProxyServer._on_client``（DD_PROXY.md §4.4）：一条连接上
        # 只解析一次请求（§4.2 连接不复用），这里只是接住并原样带下去，不
        # 重新取值。``None`` 表示 ``get_extra_info("peername")`` 没拿到。
        self._client_addr = client_addr
        self.request_id = new_request_id()
        # 一旦置位就不可撤回：响应头已经发出，此后任何失败都只能断开连接。
        self._response_started = False
        self._replay = ReplayBuffer(snapshot.routing.switch_buffer_bytes)
        # 请求体是否已从客户端读完。读完之后的重试必须走重放缓冲，
        # 因为客户端不会再发一遍。
        self._body_consumed = False
        self._request_sent = False

    async def handle(self) -> None:
        try:
            await self._run()
        except ClientDisconnected:
            pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            await self._close()

    async def _run(self) -> None:
        try:
            head = await read_head(self._reader, timeout=self._head_read_timeout)
            target = parse_target(head.method, head.target, head.headers)
        except TimeoutError:
            await self._send_error(408, MSG_HEAD_TIMEOUT)
            return
        except RequestLineTooLong:
            await self._send_error(414, MSG_BAD_REQUEST)
            return
        except HeaderTooLarge:
            await self._send_error(431, MSG_TOO_LARGE)
            return
        except BadRequest as exc:
            # 唯一带具体信息的错误：它描述客户端自己发来的请求的格式问题，
            # 不泄露任何服务端信息，且不给出正确写法用户无从改起。
            await self._send_error(400, str(exc))
            return
        except ProtocolError:
            await self._send_error(400, MSG_BAD_REQUEST)
            return

        decision = self._router.build_chain(
            target, self._cfg, self._rules, self._executor.state, now=time.monotonic()
        )
        if not decision.chain:
            self._log_empty_chain(decision)
            self._executor.note_dead_end(
                target,
                decision,
                self._cfg,
                request_id=self.request_id,
                client_addr=self._client_addr,
            )
            await self._send_error(502, MSG_NO_UPSTREAM)
            return

        if target.is_connect:
            await self._handle_connect(target, decision)
        else:
            await self._handle_http(target, head, decision)

    def _log_empty_chain(self, decision: Decision) -> None:
        """规则命中却无处可走时，日志必须给出规则序号。

        客户端只会看到一个通用的 ``502``（不得回显拓扑），用户唯一能据此
        判断「是规则配错了」而不是「网络坏了」的地方就是这条日志。
        """
        if decision.rule_position is None:
            logger.info("请求 %s 无可用出口: %s", self.request_id, decision.empty_reason)
            return
        logger.warning(
            "请求 %s 命中规则 rules[%d] 但该出口不可用: %s",
            self.request_id,
            decision.rule_position,
            decision.empty_reason,
        )

    # -- CONNECT ------------------------------------------------------------

    async def _handle_connect(self, target: RequestTarget, decision: Decision) -> None:
        """CONNECT 的抢跑字节不需要显式缓存。

        规范要求客户端等 ``200`` 再发 ClientHello，但部分客户端不等。我们在
        隧道建立前**从不读取**客户端，那些字节就一直留在 ``StreamReader`` 里，
        切换出口时天然不会丢；缓冲区满时 ``StreamReader`` 自动停止读取，
        背压由 TCP 承担。丢弃这些字节会让 TLS 握手挂到超时，且只在特定客户端
        上出现——那是最难定位的一类 bug。
        """
        result = await self._executor.execute(
            target,
            decision,
            self._cfg,
            attempt=lambda u: self._attempt_tunnel(u, target),
            discard=_close_tunnel,
            request_id=self.request_id,
            client_addr=self._client_addr,
        )
        delivered = result.delivered
        tunnel = delivered.payload if delivered is not None else None
        if delivered is None or tunnel is None or not tunnel.established:
            # 判据可能决定不切换而把非 2xx 的握手结果交回来，此时连接要关掉：
            # 客户端只该看到通用的 502，不该看到上级代理的应答。
            if tunnel is not None:
                await tunnel.close()
            await self._send_error(502, MSG_CHAIN_EXHAUSTED)
            return

        # 先回 200、再进入透传：tunnel_established 与 200 的发出严格对齐，
        # 不存在「已发 200 但标志未置」的中间态（DD_PROXY §6.1）。
        self._writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await self._writer.drain()
        self._response_started = True

        upstream_name = delivered.outcome.upstream
        stats = RelayStats()
        try:
            await relay_bidirectional(
                self._reader,
                self._writer,
                tunnel.reader,
                tunnel.writer,
                idle_timeout=self._idle_timeout,
                stats=stats,
            )
        except (OSError, TimeoutError):
            pass
        finally:
            await tunnel.close()
            self._note_premature_death(target, upstream_name, stats)
            self._executor.note_traffic(
                target,
                upstream_name,
                self._cfg,
                bytes_up=stats.bytes_up,
                bytes_down=stats.bytes_down,
                request_id=self.request_id,
            )

    async def _attempt_tunnel(
        self, upstream: UpstreamConfig, target: RequestTarget
    ) -> AttemptResult[TunnelConn]:
        ctx = self._switch_context(target, request_sent=False)
        try:
            tunnel = await self._connector.connect_tunnel(upstream, target, self._cfg.routing)
        except ConnectorError as exc:
            return AttemptResult(
                outcome=AttemptOutcome(
                    upstream=upstream.name, ok=False, error=exc.error, kind=exc.kind
                ),
                switch_context=ctx,
            )
        return AttemptResult(
            outcome=AttemptOutcome(
                upstream=upstream.name,
                ok=tunnel.established,
                status=tunnel.status,
                response_headers=tunnel.headers,
            ),
            # 隧道握手完成前没有向客户端写出任何字节，因此始终可切换。
            switch_context=self._switch_context(target, request_sent=False),
            payload=tunnel,
        )

    def _note_premature_death(
        self, target: RequestTarget, upstream: str, stats: RelayStats
    ) -> None:
        """上游先掐断、一个字节没回、隧道很快关闭：为**下一次**请求积累认知。

        三个条件缺一不可：只看时长会误判正常的短连接；只看上游字节会误判建立后
        长时间无数据但最终正常关闭的连接；不看是谁先关，则浏览器的预连接与连接池
        探活会被算到出口头上——它们从本进程的视角与「ClientHello 发出后被 RST」
        几乎同形，区别只在关闭是谁发起的。实测中这类假标记占了负面记忆的近一半
        （DD_SWITCHING §8）。

        用「谁先关」而不是「客户端发过字节没有」：上游一回 `200` 就断开时，
        客户端的 ClientHello 常常还没被中继读到就已经收尾，此时字节数是 0
        却并不代表客户端没说话——那正是本判定要抓的场景，用字节数会漏掉它。
        """
        window = self._cfg.routing.tunnel_probe_window
        if stats.closed_by != "upstream":
            return
        if stats.bytes_down == 0 and stats.duration_ms < window * 1000:
            self._executor.note_tunnel_premature_death(
                target.host, upstream, request_id=self.request_id
            )

    # -- HTTP ---------------------------------------------------------------

    async def _handle_http(self, target: RequestTarget, head: RawHead, decision: Decision) -> None:
        try:
            plan = plan_body(head.headers)
        except BadRequest as exc:
            await self._send_error(400, str(exc))
            return

        try:
            result = await self._executor.execute(
                target,
                decision,
                self._cfg,
                attempt=lambda u: self._attempt_http(u, target, head, plan),
                discard=_close_http,
                request_id=self.request_id,
                client_addr=self._client_addr,
            )
        except BadRequest as exc:
            # 客户端自己的请求体格式错误。让它穿过执行器而不是转成一次「失败
            # 尝试」：换出口不可能修好一个畸形的 chunked 编码。
            await self._send_error(400, str(exc))
            return

        delivered = result.delivered
        attempted = delivered.payload if delivered is not None else None
        if attempted is None or delivered is None:
            await self._send_error(502, MSG_CHAIN_EXHAUSTED)
            return

        upstream_name = delivered.outcome.upstream
        bytes_down = 0
        try:
            self._writer.write(sanitize_response_head(attempted.raw_head))
            await self._writer.drain()
            self._response_started = True
            self._replay.give_up()
            bytes_down = await pump(
                attempted.conn.reader, self._writer, idle_timeout=self._idle_timeout
            )
        except (OSError, TimeoutError):
            pass
        finally:
            await attempted.conn.close()
            self._executor.note_traffic(
                target,
                upstream_name,
                self._cfg,
                bytes_up=attempted.bytes_up,
                bytes_down=bytes_down,
                request_id=self.request_id,
            )

    async def _attempt_http(
        self,
        upstream: UpstreamConfig,
        target: RequestTarget,
        head: RawHead,
        plan: BodyPlan,
    ) -> AttemptResult[HttpAttempt]:
        self._request_sent = False
        ctx = self._switch_context(target, request_sent=False)
        try:
            conn = await self._connector.connect(upstream, target, self._cfg.routing)
        except ConnectorError as exc:
            return AttemptResult(
                outcome=AttemptOutcome(
                    upstream=upstream.name, ok=False, error=exc.error, kind=exc.kind
                ),
                switch_context=ctx,
            )

        _, read_timeout = self._cfg.timeout_for(upstream.name)
        try:
            request_head = build_request(target, head, upstream)
            conn.writer.write(request_head)
            body_bytes = await self._send_body(conn, plan)
            await conn.writer.drain()
            self._request_sent = True

            async with asyncio.timeout(read_timeout):
                raw_head = await conn.reader.readuntil(b"\r\n\r\n")
                status, headers = parse_response_head(raw_head)
                # 1xx（100 Continue、103 Early Hints 等）是中间响应，不是这次
                # 请求的最终结果。我们从不实现 Expect: 100-continue 握手优化
                # ——请求体在 ``_send_body`` 里已经无条件全部发出——但上游仍可能
                # 主动回一个 1xx。若把它当成最终响应转发，客户端会把紧随其后的
                # 真正响应（状态行 + 头 + 体）整段误当成这次 1xx 响应的 body 收下：
                # 表现为客户端看到的状态码是 100/103，body 里却混进一段 HTTP
                # 头部文本——这是能被真实网站触发的协议正确性缺陷，而不是假设
                # 场景。循环跳过所有 1xx 直到读到最终响应，全程仍受同一个
                # ``read_timeout`` 约束，不会无界等待；异常多的 1xx 跳数本身
                # 就说明上游不正常，超过上限直接判为失败而不是继续等。
                hops = 0
                while 100 <= status < 200:
                    hops += 1
                    if hops > MAX_INFORMATIONAL_HOPS:
                        raise ValueError("上游连续返回过多 1xx 中间响应")
                    logger.info(
                        "请求 %s 收到出口 %s 的中间响应 %d，继续等待最终响应",
                        self.request_id,
                        upstream.name,
                        status,
                    )
                    raw_head = await conn.reader.readuntil(b"\r\n\r\n")
                    status, headers = parse_response_head(raw_head)
        except BadRequest:
            await conn.close()
            raise
        except (OSError, TimeoutError, EOFError, ValueError, asyncio.IncompleteReadError) as exc:
            await conn.close()
            return AttemptResult(
                outcome=AttemptOutcome(
                    upstream=upstream.name,
                    ok=False,
                    error=type(exc).__name__,
                    kind=FailureKind.ROUTE_ERROR,
                ),
                switch_context=self._switch_context(target, request_sent=self._request_sent),
            )

        return AttemptResult(
            # 2xx/3xx 无需判据介入；其余交给判据判断是否值得换个出口。
            outcome=AttemptOutcome(
                upstream=upstream.name,
                ok=200 <= status < 400,
                status=status,
                response_headers=headers,
            ),
            switch_context=self._switch_context(target, request_sent=True),
            payload=HttpAttempt(
                conn=conn,
                raw_head=raw_head,
                status=status,
                headers=headers,
                bytes_up=len(request_head) + body_bytes,
            ),
        )

    async def _send_body(self, conn: UpstreamConn, plan: BodyPlan) -> int:
        """首次尝试边转发边缓存，重试时改从缓冲重放。返回本次实际转发的字节数。

        读取客户端请求体的每一次阻塞读用 ``head_read_timeout`` 兜底：客户端
        声明了 ``Content-Length``/chunked 之后却慢吞吞地发（或干脆不再发）
        body，此前这里没有任何超时，会让本次连接与已经建立的出口 socket
        一起挂到天荒地老——``max_client_connections`` 防的是连接数堆积，
        防不了每个连接各自卡死在读 body 这一步，二者叠加就是一个慢速请求体
        就能耗尽连接槽位的资源枯竭点（AGENTS §5.2「所有可增长资源都要有
        上限」同样适用于「一个连接占用多久」）。超时按普通传输层失败处理：
        请求还没读完自然也没能完整发往出口，``request_sent`` 保持 False，
        候选链可以正常切到下一个出口重试。

        字节数**不能**读 ``self._replay.size``：body 超过 ``switch_buffer_bytes``
        时 ``ReplayBuffer.give_up()`` 会把 ``size`` 清零，那之后这个数字就不再
        代表「转发了多少」，只代表「缓存里还留着多少」（设计中 v2.2.0，
        DD_PROXY §5.2.2）。因此用独立计数器，不依赖重放缓冲的状态。
        """
        if self._body_consumed:
            replayed = self._replay.size
            self._replay.replay_into(conn.writer.write)
            await conn.writer.drain()
            return replayed

        sent = 0

        def sink(chunk: bytes) -> None:
            nonlocal sent
            sent += len(chunk)
            self._replay.append(chunk)
            conn.writer.write(chunk)

        try:
            async with asyncio.timeout(self._head_read_timeout):
                await stream_body(self._reader, sink, plan)
        except TimeoutError:
            logger.warning(
                "请求 %s 读取客户端请求体超时（>%.0fs），放弃该次尝试",
                self.request_id,
                self._head_read_timeout,
            )
            raise
        self._body_consumed = True
        return sent

    def _switch_context(self, target: RequestTarget, *, request_sent: bool) -> SwitchContext:
        return SwitchContext(
            method=target.method,
            is_connect=target.is_connect,
            request_sent=request_sent,
            replayable=self._replay.replayable,
            response_started=self._response_started,
            host=target.host,
        )

    # -- 收尾 ----------------------------------------------------------------

    async def _send_error(self, status: int, message: str) -> None:
        if self._response_started:
            return
        body = f"{message}\n请求 ID: {self.request_id}\n".encode()
        head = (
            f"HTTP/1.1 {status} {HTTP_REASON[status]}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"X-R-Proxy-Request-Id: {self.request_id}\r\n"
            f"\r\n"
        ).encode("ascii")
        self._response_started = True
        try:
            self._writer.write(head + body)
            await self._writer.drain()
        except (OSError, ConnectionError):
            pass

    async def _close(self) -> None:
        if self._writer.is_closing():
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass


def build_request(target: RequestTarget, head: RawHead, upstream: UpstreamConfig) -> bytes:
    """构造发往出口的请求。

    出口类型决定请求行形式，这是 ``direct`` 与上级代理的**唯一**协议差异：
    向目标服务器发 absolute-form 时不少服务器会返回 400；反过来向上级代理发
    origin-form，代理无从知道目标是谁。
    """
    if upstream.is_direct:
        request_line = f"{head.method} {_path_qs(target, head)} HTTP/1.1"
    else:
        request_line = f"{head.method} {target.url} HTTP/1.1"

    headers = strip_hop_by_hop(head.headers)
    items = [(k, v) for k, v in headers.items() if k.lower() not in ("host", "connection")]
    items.insert(0, ("Host", _host_header(target)))
    items.append(("Connection", "close"))

    lines = [request_line, *(f"{k}: {v}" for k, v in items)]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _path_qs(target: RequestTarget, head: RawHead) -> str:
    if head.target.startswith("/"):
        return head.target
    parts = urlsplit(head.target)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _host_header(target: RequestTarget) -> str:
    """规范化时去掉的方括号，在写回 Host 头时必须加回来。"""
    host = f"[{target.host}]" if target.family is AddressFamily.IPV6_ONLY else target.host
    return host if target.port == 80 else f"{host}:{target.port}"


async def _close_tunnel(tunnel: TunnelConn) -> None:
    await tunnel.close()


async def _close_http(attempt: HttpAttempt) -> None:
    await attempt.conn.close()


def parse_response_head(raw: bytes) -> tuple[int, Headers]:
    """取出状态码与响应头，供切换判据判定来源。

    无法解析时返回 ``502``：拿到了字节但对端不像 HTTP，与「上游坏了」等价。
    """
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 502

    items: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if sep:
            items.append((name.strip(), value.strip()))
    return status, Headers(items)


def sanitize_response_head(raw: bytes) -> bytes:
    """剥离响应中的逐跳头，并声明连接将关闭。

    本期不复用客户端连接，必须让客户端知道响应体以连接关闭为界，否则它会
    等一个永远不来的下一个响应。
    """
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    status_line = lines[0]

    items: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if sep:
            items.append((name.strip(), value.strip()))

    kept = strip_hop_by_hop(Headers(items))
    out = [status_line]
    out.extend(f"{k}: {v}" for k, v in kept.items() if k.lower() != "connection")
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")
