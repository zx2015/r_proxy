"""有界队列与分级丢弃。

对应设计：docs/design/DD_STORAGE.md §4.2、§4.6。

``put`` 在事件循环中被调用，**绝不阻塞**——阻塞就等于卡住所有连接。容量
控制因此只能表现为「丢弃并计数」，不能表现为「等待」。
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from r_proxy.storage.queue import OpKind, Priority, WriteOp, WriteQueue, sticky_hit, sticky_upsert


def log_op(host: str = "example.com") -> WriteOp:
    return WriteOp(OpKind.REQUEST_LOG, ("rid", host, None, "GET", "a", None, 0) + (None,) * 7)


def audit_op() -> WriteOp:
    return WriteOp(OpKind.CONFIG_AUDIT, ("web", "update", "routing", None, "v1", "v2", 0))


def upsert_op(host: str = "example.com") -> WriteOp:
    return sticky_upsert(host=host, upstream="a", url=None, now_unix=1, status=200)


class TestPriorityAndRouting:
    def test_priority_is_derived_from_the_operation(self) -> None:
        """优先级不由调用方传入：传错会让粘性变更被当成日志丢掉。"""
        assert upsert_op().priority is Priority.CRITICAL
        assert sticky_hit(host="a.com", now_unix=1).priority is Priority.NORMAL
        assert log_op().priority is Priority.LOSSY

    def test_the_target_database_is_derived_from_the_operation(self) -> None:
        assert upsert_op().database == "state"
        assert log_op().database == "logs"
        assert audit_op().database == "logs"

    def test_inserts_are_never_mergeable(self) -> None:
        """每条请求日志都要保留，合并会丢掉审计记录。"""
        assert log_op().merge_key is None
        assert audit_op().merge_key is None

    def test_keyed_operations_expose_their_primary_key(self) -> None:
        assert upsert_op("a.com").merge_key == ("a.com",)
        assert sticky_hit(host="a.com", now_unix=1).merge_key == ("a.com",)


class TestCapacity:
    def test_accepts_everything_below_the_watermark(self) -> None:
        q = WriteQueue(maxsize=10)
        assert all(q.put(log_op()) for _ in range(10 - 1))
        assert q.size == 9

    def test_lossy_is_dropped_at_the_watermark(self) -> None:
        q = WriteQueue(maxsize=4)
        for _ in range(4):
            q.put(log_op())
        assert q.put(log_op()) is False
        assert q.dropped_lossy == 1
        assert q.size == 4

    def test_critical_survives_a_queue_full_of_logs(self) -> None:
        """RL-03 的另一半：日志把队列填满时，粘性变更仍须落盘。"""
        q = WriteQueue(maxsize=4)
        for _ in range(20):
            q.put(log_op())
        assert q.put(upsert_op()) is True
        assert q.dropped_critical == 0

    def test_normal_survives_the_first_watermark(self) -> None:
        q = WriteQueue(maxsize=4)
        for _ in range(5):
            q.put(log_op())
        assert q.put(sticky_hit(host="a.com", now_unix=1)) is True

    def test_everything_is_dropped_at_twice_the_capacity(self) -> None:
        """硬上限是必要的兜底：CRITICAL 完全不设限时写者卡死会 OOM，
        而丢粘性只是重新学习。"""
        q = WriteQueue(maxsize=2)
        for _ in range(4):
            q.put(upsert_op())
        assert q.size == 4
        assert q.put(upsert_op()) is False
        assert q.dropped_critical == 1

    def test_dropping_a_critical_operation_is_logged_as_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        q = WriteQueue(maxsize=1)
        q.put(upsert_op())
        q.put(upsert_op())
        with caplog.at_level(logging.ERROR, logger="r_proxy.storage.queue"):
            q.put(upsert_op())
        assert caplog.records and caplog.records[0].levelno == logging.ERROR

    def test_severe_drops_are_rate_limited(self, caplog: pytest.LogCaptureFixture) -> None:
        """极端积压下逐条打印本身会变成新的 CPU 负担：同一秒内的丢弃只报一次摘要，
        但计数不能跟着漏——限的是日志，不是统计。"""
        q = WriteQueue(maxsize=1)
        q.put(upsert_op())
        q.put(upsert_op())
        with caplog.at_level(logging.ERROR, logger="r_proxy.storage.queue"):
            for _ in range(50):
                q.put(upsert_op())
        assert len(caplog.records) == 1
        assert q.dropped_critical == 50

    def test_draining_makes_room_again(self) -> None:
        q = WriteQueue(maxsize=2)
        for _ in range(2):
            q.put(log_op())
        assert q.put(log_op()) is False
        q.drain(max_items=10, timeout=0.0)
        assert q.size == 0
        assert q.put(log_op()) is True


class TestDrain:
    def test_drain_returns_operations_in_order(self) -> None:
        q = WriteQueue(maxsize=10)
        for host in ("a", "b", "c"):
            q.put(upsert_op(host))
        batch = q.drain(max_items=10, timeout=0.05)
        assert [op.payload[0] for op in batch] == ["a", "b", "c"]

    def test_drain_stops_at_max_items(self) -> None:
        q = WriteQueue(maxsize=10)
        for _ in range(5):
            q.put(log_op())
        assert len(q.drain(max_items=2, timeout=0.05)) == 2
        assert q.size == 3

    def test_drain_returns_empty_after_the_timeout(self) -> None:
        q = WriteQueue(maxsize=10)
        started = time.monotonic()
        assert q.drain(max_items=10, timeout=0.05) == []
        assert time.monotonic() - started >= 0.04

    def test_drain_collects_operations_that_arrive_during_the_window(self) -> None:
        """攒满时间窗才落盘是有意的：批量后单行成本降到 1/44–1/188。"""
        q = WriteQueue(maxsize=10)
        timer = threading.Timer(0.01, lambda: q.put(upsert_op()))
        timer.start()
        try:
            assert len(q.drain(max_items=10, timeout=0.2)) == 1
        finally:
            timer.cancel()

    def test_drain_returns_as_soon_as_max_items_is_reached(self) -> None:
        q = WriteQueue(maxsize=100)
        for _ in range(3):
            q.put(log_op())
        started = time.monotonic()
        assert len(q.drain(max_items=3, timeout=5.0)) == 3
        assert time.monotonic() - started < 1.0

    def test_drain_all_takes_everything_without_waiting(self) -> None:
        q = WriteQueue(maxsize=10)
        for _ in range(3):
            q.put(log_op())
        assert len(q.drain_all()) == 3
        assert q.drain_all() == []

    def test_size_is_consistent_across_threads(self) -> None:
        """生产者只加、消费者只减：每个计数器都只有一个写线程，
        因此不需要锁也不会丢更新。"""
        q = WriteQueue(maxsize=10_000)
        total = 500

        def produce() -> None:
            for _ in range(total):
                q.put(log_op())

        producer = threading.Thread(target=produce)
        producer.start()
        drained = 0
        while drained < total:
            drained += len(q.drain(max_items=64, timeout=0.5))
        producer.join()
        assert drained == total
        assert q.size == 0
