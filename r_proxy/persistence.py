"""内存状态与磁盘之间的粘合层。

对应设计：docs/design/DD_STORAGE.md §4.7、§5。

放在顶层而不放进 ``state/`` 或 ``storage/``：它同时知道两边，而两层各自都
不该知道对方——``state`` 禁止 I/O，``storage`` 不该理解路由语义。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from r_proxy.state.health import UpstreamHealth
from r_proxy.state.memory import RouteBlock
from r_proxy.state.runtime import RuntimeState
from r_proxy.state.sticky import StickyEntry
from r_proxy.storage.queue import WriteSink, health_counters
from r_proxy.storage.reader import InitialState

logger = logging.getLogger(__name__)

# 健康计数在高 QPS 下每秒变化数百次，逐次入队是浪费：落盘只服务于「Web 展示 +
# 重启后粗略恢复」，5 秒粒度足够。
DEFAULT_HEALTH_FLUSH_INTERVAL = 5.0


def apply_initial_state(state: RuntimeState, initial: InitialState) -> None:
    """把回填结果装进运行时状态。时间戳已由 reader 转成 monotonic。"""
    for row in initial.sticky:
        state.sticky.restore(
            StickyEntry(
                host=row.host,
                upstream=row.upstream,
                source=_source_of(row.source),
                fail_count=row.fail_count,
                last_used_at=row.last_used_at,
                hit_count=row.hit_count,
            )
        )
    for block in initial.blocks:
        state.memory.restore(
            RouteBlock(
                host=block.host,
                upstream=block.upstream,
                blocked_until=block.blocked_until,
                fail_count=block.fail_count,
                last_reason=block.reason,
            )
        )
    for health in initial.health:
        state.health.restore_counters(
            health.upstream,
            total_success=health.total_success,
            total_failure=health.total_failure,
            total_bytes_up=health.total_bytes_up,
            total_bytes_down=health.total_bytes_down,
        )
    logger.info(
        "回填粘性 %d 条、负面记忆 %d 条、出口计数 %d 项",
        len(initial.sticky),
        len(initial.blocks),
        len(initial.health),
    )


def _source_of(raw: str) -> Literal["auto", "manual"]:
    """库里的 ``source`` 受 CHECK 约束，但回填路径不能依赖它——库文件可能是
    手工改过的。任何非 ``manual`` 的值都当 ``auto``：把未知值当手动绑定会让
    它永不被自动逻辑纠正。"""
    return "manual" if raw == "manual" else "auto"


class HealthPersister:
    """周期性把出口健康的**增量**入队。

    只有累计计数是增量（SQL 侧 `+ excluded`），其余字段是内存里的权威值
    （覆盖写）。混淆两者是本模块最容易出的错：把覆盖写成自增，计数会翻倍。
    """

    __slots__ = ("_interval", "_last", "_sink")

    def __init__(
        self,
        sink: WriteSink,
        *,
        initial: InitialState | None = None,
        interval: float = DEFAULT_HEALTH_FLUSH_INTERVAL,
    ) -> None:
        self._sink = sink
        self._interval = interval
        # 上次已落盘的累计值。增量 = 当前 - 上次，因此不需要读数据库。
        #
        # 基线必须用启动回填的同一批数值播种：`apply_initial_state` 已经把库里的
        # 历史累计值装进了内存健康表，基线若从零起算，首次 flush 的「增量」就是
        # 整个历史总量，被 `+ excluded` 再加一遍——每重启一次计数翻一倍。
        # 播种取库里的值而非内存快照：从 `start()` 到首次 flush 之间完成的请求
        # 确实是新增量，用内存快照当基线会把这些请求吞掉。
        self._last: dict[str, tuple[int, int, int, int]] = (
            {
                h.upstream: (h.total_success, h.total_failure, h.total_bytes_up, h.total_bytes_down)
                for h in initial.health
            }
            if initial is not None
            else {}
        )

    def flush(self, entries: list[UpstreamHealth], *, now_unix: int) -> int:
        """返回入队条数。无变化的出口不入队。"""
        written = 0
        # 热重载删掉的出口会从健康表里消失，其基线必须一起丢掉：否则同名出口
        # 重建后第一个增量会是负数，把库里的累计值改小。
        live = {e.name for e in entries}
        self._last = {name: v for name, v in self._last.items() if name in live}
        for health in entries:
            success, failure, bytes_up, bytes_down = self._last.get(health.name, (0, 0, 0, 0))
            delta = (
                health.total_success - success,
                health.total_failure - failure,
                health.total_bytes_up - bytes_up,
                health.total_bytes_down - bytes_down,
            )
            if delta == (0, 0, 0, 0):
                continue
            self._last[health.name] = (
                health.total_success,
                health.total_failure,
                health.total_bytes_up,
                health.total_bytes_down,
            )
            self._sink.put(
                health_counters(
                    upstream=health.name,
                    success_delta=delta[0],
                    failure_delta=delta[1],
                    consecutive_failures=health.consecutive_failures,
                    # 内存里尚未统计延迟与冷却截止时间，落 0 占位；两者都只供
                    # Web 展示，缺失不影响路由。
                    avg_latency_ms=0,
                    circuit_state=health.state.name.lower(),
                    cooldown_until=0,
                    auth_error=int(health.auth_error),
                    now_unix=now_unix,
                    bytes_up_delta=delta[2],
                    bytes_down_delta=delta[3],
                )
            )
            written += 1
        return written

    async def run(self, state: RuntimeState) -> None:
        """后台任务：周期落盘，取消时补一次。

        取消发生在两次周期之间的任意时刻，落在 `finally` 里能覆盖到最后一个
        不足整周期的窗口——否则关停前 `interval` 秒内的计数增量永远不会落盘，
        重启回填时这段流量就凭空消失了。
        """
        try:
            while True:
                await asyncio.sleep(self._interval)
                now = time.monotonic()
                self.flush(state.health.all(now=now), now_unix=int(time.time()))
        finally:
            now = time.monotonic()
            self.flush(state.health.all(now=now), now_unix=int(time.time()))
