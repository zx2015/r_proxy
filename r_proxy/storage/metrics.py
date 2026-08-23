"""存储子系统的可观测性：指标快照与周期性上报。

对应设计：docs/design/DD_STORAGE.md §8。

本模块只做「读已有计数并呈现」，不参与写入路径，因此**不导入队列与写者**：
指标是要给 Web 与日志两个消费方共享的契约，把它钉在具体实现上会让 Web 为了
读一个数字而 import 写者线程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 面向「长期后台运行」的采样间隔。取 5 分钟而非几秒：这些数字是给人读日志用
# 的，不是时序数据库的输入，一分钟一行会在一天里堆出 1440 行噪声。
DEFAULT_REPORT_INTERVAL_S = 300.0


@dataclass(frozen=True, slots=True)
class StorageMetrics:
    """某一时刻的存储指标。不可变：Web 与日志读到的必须是同一份一致的截面。"""

    queue_size: int
    queue_capacity: int
    queue_high_water: int
    accepted: int
    dropped_lossy: int
    dropped_normal: int
    dropped_critical: int
    flushes: int
    rows_written: int
    rows_merged_away: int
    write_errors: int
    retention_runs: int
    last_flush_at: float
    flush_duration_p99_ms: float

    @property
    def merge_ratio(self) -> float:
        """合并后条数 / 合并前条数。越小说明合并省下的写入越多。

        没有任何落盘时返回 ``1.0``（「一条都没省下」），而不是 0——0 会在
        界面上显示成「合并率极佳」，与事实相反。
        """
        before = self.rows_written + self.rows_merged_away
        return 1.0 if before == 0 else self.rows_written / before


class MetricsReporter:
    """周期性把指标写进日志，并对异常升级为 WARNING / ERROR。

    没有 Web 界面时（``--no-web``），日志是唯一的观测出口；有 Web 界面时，
    日志仍是唯一能事后回溯的出口。两种情况都需要它。
    """

    __slots__ = ("_clock", "_interval", "_previous", "_source", "_started_at")

    def __init__(
        self,
        source: Callable[[], StorageMetrics],
        *,
        interval_s: float = DEFAULT_REPORT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._interval = interval_s
        self._clock = clock
        self._previous = source()
        self._started_at = clock()

    async def run(self) -> None:
        """由 ``Application`` 起成后台任务。取消即结束，无需清理。"""
        while True:
            await asyncio.sleep(self._interval)
            self.report()

    def report(self) -> None:
        current = self._source()
        previous = self._previous
        self._previous = current

        if (dropped := current.dropped_critical - previous.dropped_critical) > 0:
            logger.error("写入队列丢弃了 %d 条关键操作，对应的粘性或熔断状态重启后会丢失", dropped)
        if (errors := current.write_errors - previous.write_errors) > 0:
            logger.error("批量写入失败 %d 次，检查磁盘空间与数据库文件权限", errors)
        if (lossy := current.dropped_lossy - previous.dropped_lossy) > 0:
            logger.warning("写入队列丢弃了 %d 条请求日志，代理转发未受影响", lossy)

        if self._writer_looks_stalled(current):
            logger.error(
                "写者线程可能已卡死：队列积压 %d 条，最近 %.0fs 内没有落盘",
                current.queue_size,
                self._clock() - (current.last_flush_at or self._started_at),
            )
            return
        if current.accepted > previous.accepted or current.flushes > previous.flushes:
            logger.info(
                "存储：入队 %d 条，落盘 %d 批 / %d 行，合并率 %.2f，队列 %d/%d（峰值 %d），"
                "p99 %.1fms，丢弃 lossy=%d normal=%d critical=%d，写失败 %d，清理 %d 次",
                current.accepted - previous.accepted,
                current.flushes - previous.flushes,
                current.rows_written - previous.rows_written,
                current.merge_ratio,
                current.queue_size,
                current.queue_capacity,
                current.queue_high_water,
                current.flush_duration_p99_ms,
                current.dropped_lossy,
                current.dropped_normal,
                current.dropped_critical,
                current.write_errors,
                current.retention_runs,
            )

    def _writer_looks_stalled(self, current: StorageMetrics) -> bool:
        """队列有积压却整个采样周期都没落盘，判为卡死。

        必须由外部观察者来判：卡死的写者线程自己什么也报不出来，而
        ``last_flush_at`` 停止前进正是它唯一留下的痕迹。空队列不算卡死——
        空闲的代理本来就不该有落盘。
        """
        if current.queue_size == 0:
            return False
        baseline = current.last_flush_at or self._started_at
        return self._clock() - baseline > self._interval
