"""唯一写者线程：批次合并 + ``BEGIN IMMEDIATE`` 落盘。

对应设计：docs/design/DD_STORAGE.md §4.3、§4.4、§4.5。

**全进程只有一个写者线程**，它持有两个库的写连接。不为每个库开一个线程——
两个写者线程会引入线程间的批次协调问题，而写入本身不是瓶颈（批量后单行
0.0025ms）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace

from r_proxy.config.model import DatabaseConfig
from r_proxy.storage.queue import OpKind, WriteOp, WriteQueue
from r_proxy.storage.retention import Retention
from r_proxy.storage.schema import Database, open_write

logger = logging.getLogger(__name__)

# 计数器一律 SQL 侧自增（`c = c + ?`），Python 侧从不读取当前值：实测 4 线程
# 各 500 次的读改写只落库 509 次，丢了 75%。
_SQL: dict[OpKind, str] = {
    OpKind.STICKY_UPSERT: """
        INSERT INTO host_upstream
            (host, upstream_name, source, last_url, last_success_at,
             last_http_status, fail_count, hit_count, updated_at)
        VALUES (?, ?, 'auto', ?, ?, ?, 0, 1, ?)
        ON CONFLICT(host) DO UPDATE SET
            upstream_name    = excluded.upstream_name,
            last_url         = excluded.last_url,
            last_success_at  = MAX(host_upstream.last_success_at,
                                   excluded.last_success_at),
            last_http_status = excluded.last_http_status,
            fail_count       = 0,
            hit_count        = host_upstream.hit_count + 1,
            updated_at       = excluded.updated_at
        WHERE host_upstream.source != 'manual'
    """,
    # 唯一允许写 source='manual' 的语句，也是唯一不带 source 护栏的 UPSERT：
    # 护栏防的是自动逻辑覆盖手动意图，而这条语句就是手动意图本身。
    OpKind.STICKY_MANUAL_UPSERT: """
        INSERT INTO host_upstream
            (host, upstream_name, source, last_success_at, fail_count,
             hit_count, updated_at)
        VALUES (?, ?, 'manual', 0, 0, 0, ?)
        ON CONFLICT(host) DO UPDATE SET
            upstream_name = excluded.upstream_name,
            source        = 'manual',
            fail_count    = 0,
            updated_at    = excluded.updated_at
    """,
    OpKind.STICKY_HIT: """
        UPDATE host_upstream
           SET hit_count = hit_count + ?,
               last_success_at = MAX(last_success_at, ?),
               updated_at = ?
         WHERE host = ?
    """,
    OpKind.STICKY_DELETE: "DELETE FROM host_upstream WHERE host = ?",
    OpKind.ROUTE_BLOCK_UPSERT: """
        INSERT INTO route_block
            (host, upstream_name, fail_count, last_error,
             last_failure_at, blocked_until)
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT(host, upstream_name) DO UPDATE SET
            fail_count      = route_block.fail_count + 1,
            last_error      = excluded.last_error,
            last_failure_at = excluded.last_failure_at,
            blocked_until   = excluded.blocked_until
    """,
    OpKind.ROUTE_BLOCK_DELETE: "DELETE FROM route_block WHERE host = ? AND upstream_name = ?",
    OpKind.HEALTH_COUNTERS: """
        INSERT INTO upstream_health
            (upstream_name, total_success, total_failure,
             consecutive_failures, avg_latency_ms, circuit_state,
             cooldown_until, auth_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(upstream_name) DO UPDATE SET
            total_success        = upstream_health.total_success
                                   + excluded.total_success,
            total_failure        = upstream_health.total_failure
                                   + excluded.total_failure,
            consecutive_failures = excluded.consecutive_failures,
            avg_latency_ms       = excluded.avg_latency_ms,
            circuit_state        = excluded.circuit_state,
            cooldown_until       = excluded.cooldown_until,
            auth_error           = excluded.auth_error,
            updated_at           = excluded.updated_at
    """,
    OpKind.REQUEST_LOG: """
        INSERT INTO request_log
            (request_id, host, url, method, upstream_name, upstream_priority,
             attempt_index, decision_source, rule_origin, http_status, error,
             failure_kind, keep_reason, elapsed_ms, bytes_up, bytes_down,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    OpKind.CONFIG_AUDIT: """
        INSERT INTO config_audit
            (actor, action, target, diff, version_before, version_after,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
}


@dataclass(slots=True)
class WriterMetrics:
    """供 Web 展示与告警。``dropped_critical > 0`` 应当显著告警。"""

    flushes: int = 0
    rows_written: int = 0
    rows_merged_away: int = 0
    write_errors: int = 0
    last_flush_at: float = 0.0
    last_flush_duration_ms: float = 0.0
    retention_runs: int = 0
    _durations: list[float] = field(default_factory=list, repr=False)

    def observe(self, duration_ms: float, rows: int) -> None:
        self.flushes += 1
        self.rows_written += rows
        self.last_flush_at = time.time()
        self.last_flush_duration_ms = duration_ms
        self._durations.append(duration_ms)
        # 只保留最近的样本：这是给人看的水位，不是时序数据库。
        if len(self._durations) > 512:
            del self._durations[:256]

    @property
    def flush_duration_p99(self) -> float:
        if not self._durations:
            return 0.0
        ordered = sorted(self._durations)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]


class WriterThread(threading.Thread):
    """``daemon=False``：守护线程会在主线程退出时被直接杀死，丢掉最后一批写入。"""

    def __init__(
        self,
        write_queue: WriteQueue,
        cfg: DatabaseConfig,
        *,
        retention: Retention | None = None,
    ) -> None:
        super().__init__(name="r-proxy-writer", daemon=False)
        self._queue = write_queue
        self._cfg = cfg
        # 不叫 _stop：Thread 自己有一个 _stop() 方法，join() 会调它。
        # 覆盖掉它的表现是 join 抛 TypeError，与本模块的逻辑毫无关系。
        self._stopping = threading.Event()
        self._opened = threading.Event()
        self._open_error: Exception | None = None
        self._retention = retention if retention is not None else Retention(cfg)
        self.metrics = WriterMetrics()
        # 已处理完的操作数（合并前）。与 queue.accepted 比较即可判断
        # 「某个时刻之前入队的写入是否都已落盘」，见 wait_until_drained。
        self._processed = 0

    def run(self) -> None:
        try:
            connections = {
                Database.STATE: open_write(self._cfg.state_path, Database.STATE),
                Database.LOGS: open_write(self._cfg.logs_path, Database.LOGS),
            }
        except Exception as exc:  # noqa: BLE001 - 启动线程的一方需要看到原因
            self._open_error = exc
            self._opened.set()
            return
        self._opened.set()
        try:
            while not self._stopping.is_set():
                batch = self._queue.drain(
                    max_items=self._cfg.flush_batch_size,
                    timeout=self._cfg.flush_interval_ms / 1000,
                )
                if batch:
                    self._flush(batch, connections)
                    # 计数在落盘之后推进：先推进会让 wait_until_drained 在
                    # 数据还没提交时就返回。
                    self._processed += len(batch)
                self._retention.maybe_run(connections, self.metrics)
            # 排空：停止信号到达时队列里可能还压着未落盘的粘性变更。
            if remaining := self._queue.drain_all():
                self._flush(remaining, connections)
                self._processed += len(remaining)
        finally:
            for conn in connections.values():
                conn.close()

    def start_and_wait(self, *, timeout: float = 10.0) -> None:
        """启动线程并等到两个库都打开成功，打不开就抛错。

        建库失败必须在启动阶段暴露：让代理带着一个死掉的写者线程继续跑，
        表现是「所有状态都记不住」，而没有任何直接症状指向数据库。
        """
        self.start()
        if not self._opened.wait(timeout):
            raise TimeoutError("写者线程未能在超时内打开数据库")
        if self._open_error is not None:
            raise self._open_error

    def wait_until_drained(self, *, timeout: float = 5.0) -> bool:
        """阻塞到调用时刻之前入队的写入全部落盘。

        供关停、备份与测试使用。**不要在事件循环里调用**：它会阻塞线程。
        """
        target = self._queue.accepted
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._processed >= target:
                return True
            time.sleep(0.002)
        return False

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stopping.set()
        # 唤醒可能正阻塞在时间窗上的 drain，否则 join 要等满 flush_interval_ms。
        self._queue.interrupt()
        self.join(timeout)
        if self.is_alive():
            logger.error("写者线程未能在 %.1fs 内退出，可能有未落盘的写入", timeout)

    def _flush(self, batch: list[WriteOp], connections: dict[Database, sqlite3.Connection]) -> None:
        merged = merge(batch)
        self.metrics.rows_merged_away += len(batch) - len(merged)
        started = time.monotonic()
        for database, ops in _split_by_database(merged).items():
            self._write(connections[database], ops)
        self.metrics.observe((time.monotonic() - started) * 1000, len(merged))

    def _write(self, conn: sqlite3.Connection, ops: list[WriteOp]) -> None:
        try:
            # BEGIN IMMEDIATE 不可省略：deferred 事务读时只拿共享锁、写时才
            # 升级，两个连接同时请求升级会死锁，busy_timeout 对此无效。
            conn.execute("BEGIN IMMEDIATE")
            for kind, rows in _group_by_kind(ops):
                conn.executemany(_SQL[kind], rows)
            conn.commit()
        except sqlite3.Error as exc:
            # 写入失败不重试：数据可丢，代理不能停。重试会阻塞队列消费，
            # 让内存跟着涨。
            _rollback(conn)
            self.metrics.write_errors += 1
            logger.error("批量写入失败，丢弃 %d 条: %s", len(ops), exc)


def merge(ops: list[WriteOp]) -> list[WriteOp]:
    """批次内合并。同一行的多次自增合成一次 ``+ N``。

    合并必须保持顺序语义：**同一行上出现 delete 时，清空该行之前累积的所有
    操作**。否则「upsert → delete → upsert」会被压成「upsert → delete」，
    最终状态从「存在」变成「不存在」。
    """
    out: list[WriteOp] = []
    # 被 delete 抹掉的槽位。标记而不是从 out 里删除：删除会让其余槽位下标
    # 全部失效，而 slots 里存的就是下标。
    dropped: set[int] = set()
    slots: dict[tuple[str, tuple[object, ...]], dict[OpKind, int]] = {}

    for op in ops:
        row = op.row_key
        if row is None:
            out.append(op)  # INSERT 类不合并，每条都要保留
            continue
        group = slots.setdefault(row, {})
        if op.spec.deletes_row:
            dropped.update(group.values())
            group.clear()
        slot = group.get(op.kind)
        if slot is None:
            group[op.kind] = len(out)
            out.append(op)
        else:
            out[slot] = _combine(out[slot], op)

    return [op for index, op in enumerate(out) if index not in dropped]


def _combine(previous: WriteOp, current: WriteOp) -> WriteOp:
    """增量列相加，其余列取后写的那一份（它是更新的权威值）。"""
    if not (slots := current.spec.delta_slots):
        return current
    payload = list(current.payload)
    for index in slots:
        payload[index] = _as_int(previous.payload[index]) + _as_int(current.payload[index])
    return replace(current, payload=tuple(payload))


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _split_by_database(ops: list[WriteOp]) -> dict[Database, list[WriteOp]]:
    grouped: dict[Database, list[WriteOp]] = {}
    for op in ops:
        grouped.setdefault(op.database, []).append(op)
    return grouped


def _group_by_kind(ops: list[WriteOp]) -> list[tuple[OpKind, list[tuple[object, ...]]]]:
    """相邻的同种操作合成一次 ``executemany``。

    不做全局按种类分组：那会打乱「delete 在 upsert 之前」这类跨种类的顺序。
    """
    groups: list[tuple[OpKind, list[tuple[object, ...]]]] = []
    for op in ops:
        if groups and groups[-1][0] is op.kind:
            groups[-1][1].append(op.payload)
        else:
            groups.append((op.kind, [op.payload]))
    return groups


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
