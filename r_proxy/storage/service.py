"""存储子系统的门面：一个写入队列、一个写者线程、两个只读连接池。

对应设计：docs/design/DD_STORAGE.md §4.1。

存在的意义是把「唯一写者」这条约束收进一个对象里。写者线程与队列如果散在
调用方手上，早晚会出现第二个写者——两个写者会各自持有 WAL 写锁，表现为随机
的 ``database is locked``，且内存权威状态不再唯一。
"""

from __future__ import annotations

import logging
import time

from r_proxy.config.model import DatabaseConfig, LimitsConfig
from r_proxy.storage.metrics import StorageMetrics
from r_proxy.storage.queue import Priority, WriteQueue
from r_proxy.storage.reader import InitialState, ReadOnlyPool, load_initial_state
from r_proxy.storage.writer import WriterThread

logger = logging.getLogger(__name__)

DEFAULT_STOP_TIMEOUT = 10.0


class StorageService:
    __slots__ = ("_cfg", "_limits", "_thread", "logs_reader", "queue", "state_reader")

    def __init__(self, cfg: DatabaseConfig, limits: LimitsConfig) -> None:
        self._cfg = cfg
        self._limits = limits
        self.queue = WriteQueue(maxsize=cfg.write_queue_size)
        self._thread = WriterThread(self.queue, cfg)
        self.state_reader = ReadOnlyPool(cfg.state_path)
        self.logs_reader = ReadOnlyPool(cfg.logs_path)

    def load_initial_state(self) -> InitialState:
        """在写者线程启动**之前**调用：此时库里的内容就是上次退出时的样子。"""
        return load_initial_state(
            self._cfg, self._limits, now_unix=time.time(), now_mono=time.monotonic()
        )

    def start(self) -> None:
        """打开两个库并启动写者线程。库打不开时抛 ``StorageError``，拒绝启动。"""
        self._thread.start_and_wait()

    def metrics(self) -> StorageMetrics:
        """队列与写者的计数汇成一份截面。供日志上报与 Web 状态接口共用。

        队列侧与写者侧的计数由不同线程推进，读到的截面可能相差一个批次。
        这对水位与告警判断无影响，因此不加锁——加锁会把读指标塞进热路径。
        """
        writer = self._thread.metrics
        return StorageMetrics(
            queue_size=self.queue.size,
            queue_capacity=self._cfg.write_queue_size,
            queue_high_water=self.queue.high_water,
            accepted=self.queue.accepted,
            dropped_lossy=self.queue.dropped[Priority.LOSSY],
            dropped_normal=self.queue.dropped[Priority.NORMAL],
            dropped_critical=self.queue.dropped[Priority.CRITICAL],
            flushes=writer.flushes,
            rows_written=writer.rows_written,
            rows_merged_away=writer.rows_merged_away,
            write_errors=writer.write_errors,
            retention_runs=writer.retention_runs,
            last_flush_at=writer.last_flush_at,
            flush_duration_p99_ms=writer.flush_duration_p99,
        )

    def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """排空队列后关库。收到 ``SIGTERM`` 时的粘性变更不能丢。"""
        self._thread.stop(timeout=timeout)
        dropped = self.queue.dropped_critical
        if dropped:
            logger.error("有 %d 条关键写入被丢弃，重启后会丢失对应状态", dropped)
        # 退出时留一份总账：长跑进程的中间上报可能已被日志轮转冲掉。
        final = self.metrics()
        logger.info(
            "存储关闭：累计入队 %d 条，落盘 %d 批 / %d 行，合并率 %.2f，队列峰值 %d，"
            "丢弃 lossy=%d normal=%d critical=%d，写失败 %d",
            final.accepted,
            final.flushes,
            final.rows_written,
            final.merge_ratio,
            final.queue_high_water,
            final.dropped_lossy,
            final.dropped_normal,
            final.dropped_critical,
            final.write_errors,
        )
