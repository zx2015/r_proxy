"""字节中继与背压。

对应设计：docs/design/DD_PROXY.md §5.2、§6.2。

``pump`` 同时服务于 HTTP 响应体转发与 CONNECT 隧道，因此它不知道任何隧道
统计的存在——字节计数通过 ``on_bytes`` 回调交给调用方。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

CHUNK_SIZE = 65536

# 空闲超时而非整体超时：整体超时会误杀大文件下载与长轮询，
# 空闲超时只杀真正卡死的连接（DD_PROXY §4.3）。
DEFAULT_IDLE_TIMEOUT = 300.0


@dataclass(slots=True)
class RelayStats:
    """隧道的字节与时长统计，供早夭判定使用（判定逻辑在决策层）。"""

    bytes_up: int = 0
    bytes_down: int = 0
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    # 哪一侧先关闭。早夭判定要靠它区分「上游把隧道掐了」与「客户端自己放弃」，
    # 而字节数做不到这个区分：上游一回 200 就断开时，客户端的 ClientHello
    # 往往还没被读到中继就已经收尾，``bytes_up`` 是 0 却并非客户端的意思。
    closed_by: Literal["client", "upstream"] | None = None

    @property
    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return int((end - self.started_at) * 1000)


async def pump(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    *,
    idle_timeout: float,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """把 ``src`` 的字节搬到 ``dst``，直到 ``src`` EOF。返回搬运的总字节数。

    ``await dst.drain()`` 是全部所需的背压机制：对端读得慢时 drain 挂起，
    上游的读取随之减速（TCP 窗口收缩）。漏掉它会让写缓冲无限增长——慢客户端
    下载大文件时内存被吃光，这是代理实现中最常见的内存问题。
    """
    total = 0
    while True:
        async with asyncio.timeout(idle_timeout):
            chunk = await src.read(CHUNK_SIZE)
        if not chunk:
            return total
        total += len(chunk)
        if on_bytes is not None:
            on_bytes(len(chunk))
        dst.write(chunk)
        await dst.drain()


async def relay_bidirectional(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    stats: RelayStats | None = None,
) -> RelayStats:
    """双向中继，任一方向结束即收尾。"""
    stats = stats if stats is not None else RelayStats()

    up = asyncio.create_task(
        pump(
            client_reader,
            upstream_writer,
            idle_timeout=idle_timeout,
            on_bytes=lambda n: _add_up(stats, n),
        )
    )
    down = asyncio.create_task(
        pump(
            upstream_reader,
            client_writer,
            idle_timeout=idle_timeout,
            on_bytes=lambda n: _add_down(stats, n),
        )
    )

    try:
        # FIRST_COMPLETED 而非 ALL_COMPLETED：严格来说 TCP 半关闭后另一方向仍可
        # 传输，但 HTTPS 隧道极少这么用，而等两个方向都结束会让「客户端关了但
        # 服务端不关」的连接一直挂着。
        done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        # 两个方向都已结束时归给客户端：判定的用途是「要不要怪上游」，
        # 客户端明确关过就足以让这次不算上游的账。
        stats.closed_by = "client" if up in done else "upstream"
    finally:
        # 放在 finally 里：本协程被取消时也要收走两个子任务，否则它们会脱离
        # 管理继续跑，关停时无从等待。
        for task in (up, down):
            task.cancel()
        # 必须 await 到真正结束：否则统计的字节数还在变，且会产生
        # 「Task was destroyed but it is pending」告警。
        await asyncio.gather(up, down, return_exceptions=True)
        stats.ended_at = time.monotonic()
    return stats


def _add_up(stats: RelayStats, n: int) -> None:
    stats.bytes_up += n


def _add_down(stats: RelayStats, n: int) -> None:
    stats.bytes_down += n
