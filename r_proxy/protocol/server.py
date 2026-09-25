"""监听套接字与连接数上限。

对应设计：docs/design/DD_PROXY.md §8.1。
"""

from __future__ import annotations

import asyncio
import logging

from r_proxy.config.model import ConfigSnapshot
from r_proxy.decision.limiter import SwitchRateLimiter
from r_proxy.decision.router import Router
from r_proxy.decision.switching import SwitchPolicy
from r_proxy.egress.connector import UpstreamConnector
from r_proxy.egress.executor import AttemptExecutor
from r_proxy.protocol.connection import (
    DEFAULT_HEAD_READ_TIMEOUT,
    ClientConnection,
)
from r_proxy.protocol.parse import MAX_HEAD_BYTES
from r_proxy.protocol.relay import DEFAULT_IDLE_TIMEOUT
from r_proxy.rules.model import EMPTY_RULE_SET, RuleSet
from r_proxy.state.runtime import RuntimeState
from r_proxy.storage.queue import WriteSink

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = 10.0

_OVERLOAD_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)


class ProxyServer:
    def __init__(
        self,
        snapshot: ConfigSnapshot,
        *,
        rule_set: RuleSet = EMPTY_RULE_SET,
        connector: UpstreamConnector | None = None,
        state: RuntimeState | None = None,
        sink: WriteSink | None = None,
        head_read_timeout: float = DEFAULT_HEAD_READ_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._cfg = snapshot
        self._rules = rule_set
        self._connector = connector if connector is not None else UpstreamConnector()
        self._head_read_timeout = head_read_timeout
        self._idle_timeout = idle_timeout
        # 路由与执行器全进程共享：熔断状态、负面记忆、轮询游标、限流窗口
        # 都必须跨请求累积，每连接新建一份等于让这些机制失效。
        self.state = state if state is not None else RuntimeState.from_snapshot(snapshot)
        self._router = Router()
        self._executor = AttemptExecutor(
            state=self.state, policy=SwitchPolicy(), limiter=SwitchRateLimiter(), sink=sink
        )
        self._server: asyncio.Server | None = None
        # 必须持强引用：create_task 返回的 Task 只被事件循环弱引用，
        # 不持有强引用时可能在运行中被 GC 回收，表现为随机的连接中断。
        self._active: set[asyncio.Task[None]] = set()
        self.rejected_connections = 0

    def update_snapshot(self, snapshot: ConfigSnapshot, rule_set: RuleSet) -> None:
        """热重载：单次引用赋值替换配置与规则集。

        已在处理中的连接继续用它开始时取到的那一份快照，不会中途换配置。
        两者必须一起换：规则引用出口名，只换其中一个会出现「规则指向的出口
        在当前快照里不存在」的窗口。
        """
        self._cfg = snapshot
        self._rules = rule_set
        self.state.on_reload(snapshot)

    @property
    def bound_address(self) -> tuple[str, int]:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("服务器尚未启动")
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    @property
    def active_connections(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client,
            self._cfg.listen.host,
            self._cfg.listen.port,
            limit=MAX_HEAD_BYTES,
        )
        host, port = self.bound_address
        logger.info("代理监听于 %s:%s", host, port)

    async def stop(self, *, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()  # 立即停止 accept，已建立的连接不受影响

        if self._active:
            _, still_running = await asyncio.wait(list(self._active), timeout=drain_timeout)
            for task in still_running:
                task.cancel()
            if still_running:
                logger.warning("强制关闭 %d 个未完成的连接", len(still_running))
                await asyncio.gather(*still_running, return_exceptions=True)

        if server is not None:
            # Python 3.12.1 起 wait_closed() 会一直等到所有连接处理完毕，
            # 必须放在排空之后；仍加超时兜底，避免第三方持有的连接把关停拖死。
            try:
                async with asyncio.timeout(drain_timeout):
                    await server.wait_closed()
            except TimeoutError:
                logger.warning("等待监听套接字关闭超时")

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._active) >= self._cfg.limits.max_client_connections:
            self.rejected_connections += 1
            await _reject(writer)
            return

        conn = ClientConnection(
            reader,
            writer,
            self._cfg,
            self._rules,
            self._connector,
            self._router,
            self._executor,
            head_read_timeout=self._head_read_timeout,
            idle_timeout=self._idle_timeout,
            client_addr=_client_addr(writer),
        )
        task = asyncio.create_task(conn.handle())
        # add_done_callback 同时承担计数与清理，不需要额外的计数器。
        self._active.add(task)
        task.add_done_callback(self._active.discard)


def _client_addr(writer: asyncio.StreamWriter) -> str | None:
    """连接层面只采集一次（DD_PROXY.md §4.4），随连接对象传下去。

    ``peername`` 对 IPv4 是 ``(host, port)``，对 IPv6 可能是 4 元组（含
    ``flowinfo``、``scopeid``）；两种情况都只取 ``host``，端口留着没有审计
    价值。理论上 ``get_extra_info`` 也可能拿不到（连接已被撤销等边界情况），
    此时返回 ``None``，不应让整条请求日志因此写入失败。
    """
    peer = writer.get_extra_info("peername")
    if not peer:
        return None
    return str(peer[0])


async def _reject(writer: asyncio.StreamWriter) -> None:
    try:
        writer.write(_OVERLOAD_RESPONSE)
        await writer.drain()
    except (OSError, ConnectionError):
        pass
    finally:
        writer.close()
