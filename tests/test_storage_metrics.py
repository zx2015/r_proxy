"""指标快照与周期性上报。

对应设计：docs/design/DD_STORAGE.md §8。

长跑观测的价值全部落在「异常能被看见」上，因此这里的重点不是数字算得对，
而是**异常路径真的会产生日志**：丢弃关键操作、写入失败、写者卡死。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from r_proxy.config.model import DatabaseConfig, LimitsConfig
from r_proxy.storage.metrics import MetricsReporter, StorageMetrics
from r_proxy.storage.queue import OpKind, WriteOp, WriteQueue, sticky_upsert
from r_proxy.storage.service import StorageService

LOGGER = "r_proxy.storage.metrics"


def sample(**kwargs: object) -> StorageMetrics:
    defaults: dict[str, object] = {
        "queue_size": 0,
        "queue_capacity": 100,
        "queue_high_water": 0,
        "accepted": 0,
        "dropped_lossy": 0,
        "dropped_normal": 0,
        "dropped_critical": 0,
        "flushes": 0,
        "rows_written": 0,
        "rows_merged_away": 0,
        "write_errors": 0,
        "retention_runs": 0,
        "last_flush_at": 0.0,
        "flush_duration_p99_ms": 0.0,
    }
    defaults.update(kwargs)
    return StorageMetrics(**defaults)  # type: ignore[arg-type]


class Clock:
    """手动推进的时钟：卡死判定基于时间差，用真实时间只能靠 sleep。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Source:
    """可变的指标源，模拟两次采样之间发生的变化。"""

    def __init__(self, first: StorageMetrics) -> None:
        self.current = first

    def __call__(self) -> StorageMetrics:
        return self.current


def log_op() -> WriteOp:
    head = ("rid", "example.com", None, "GET", "a", None, 0)
    return WriteOp(OpKind.REQUEST_LOG, head + (None,) * 7)


class TestMergeRatio:
    def test_no_flush_yet_reports_no_savings(self) -> None:
        """0 会在界面上显示成「合并率极佳」，与「什么都没发生」相反。"""
        assert sample().merge_ratio == 1.0

    def test_ratio_is_after_over_before(self) -> None:
        assert sample(rows_written=25, rows_merged_away=75).merge_ratio == 0.25

    def test_nothing_merged_reports_one(self) -> None:
        assert sample(rows_written=40, rows_merged_away=0).merge_ratio == 1.0


class TestQueueHighWater:
    def test_high_water_survives_draining(self) -> None:
        """周期采样看不到峰值：涨上去又落回来的那一次最需要知道。"""
        q = WriteQueue(maxsize=100)
        for _ in range(7):
            q.put(log_op())
        q.drain_all()
        assert q.size == 0
        assert q.high_water == 7

    def test_dropped_operations_do_not_raise_the_water_mark(self) -> None:
        q = WriteQueue(maxsize=3)
        for _ in range(10):
            q.put(log_op())
        assert q.high_water == 3


class TestReporting:
    def test_an_idle_interval_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """空闲的本地代理会跑几个月，每个周期都记一行等于把日志填满噪声。"""
        reporter = MetricsReporter(Source(sample()), interval_s=300.0, clock=Clock())
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            reporter.report()
        assert caplog.records == []

    def test_activity_is_summarised_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = Clock()
        source = Source(sample())
        reporter = MetricsReporter(source, interval_s=300.0, clock=clock)
        source.current = sample(accepted=120, flushes=3, rows_written=90, last_flush_at=clock.now)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert [r.levelno for r in caplog.records] == [logging.INFO]
        assert "入队 120 条" in caplog.records[0].getMessage()

    def test_deltas_are_relative_to_the_previous_report(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """长跑进程的累计值没有可读性：要的是「这个周期发生了什么」。"""
        clock = Clock()
        source = Source(sample(accepted=1_000, flushes=10))
        reporter = MetricsReporter(source, interval_s=300.0, clock=clock)
        source.current = sample(accepted=1_050, flushes=11, last_flush_at=clock.now)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert "入队 50 条" in caplog.records[0].getMessage()

    def test_dropped_critical_is_an_error(self, caplog: pytest.LogCaptureFixture) -> None:
        source = Source(sample())
        reporter = MetricsReporter(source, interval_s=300.0, clock=Clock())
        source.current = sample(accepted=5, dropped_critical=2)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "关键操作" in errors[0].getMessage()

    def test_write_errors_are_reported_once_per_occurrence(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """只报增量：累计值非零会让同一次磁盘故障在之后每个周期都报一次。"""
        source = Source(sample(write_errors=3))
        reporter = MetricsReporter(source, interval_s=300.0, clock=Clock())
        source.current = sample(write_errors=3, accepted=10)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert [r.levelno for r in caplog.records] == [logging.INFO]

    def test_dropped_logs_are_only_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """请求日志可丢：降级可观测性优于阻塞转发。"""
        source = Source(sample())
        reporter = MetricsReporter(source, interval_s=300.0, clock=Clock())
        source.current = sample(accepted=99, dropped_lossy=17)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert {r.levelno for r in caplog.records} == {logging.WARNING, logging.INFO}


class TestStallDetection:
    def test_backlog_without_a_flush_is_an_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """卡死的写者线程自己什么都报不出来，只能由外部观察者判定。"""
        clock = Clock()
        source = Source(sample(last_flush_at=clock.now))
        reporter = MetricsReporter(source, interval_s=60.0, clock=clock)
        source.current = sample(queue_size=500, accepted=500, last_flush_at=clock.now)
        clock.now += 61.0
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "卡死" in errors[0].getMessage()

    def test_a_stalled_writer_suppresses_the_summary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """卡死时的吞吐数字没有意义，且会把告警挤出视野。"""
        clock = Clock()
        source = Source(sample(last_flush_at=clock.now))
        reporter = MetricsReporter(source, interval_s=60.0, clock=clock)
        source.current = sample(queue_size=500, accepted=500, last_flush_at=clock.now)
        clock.now += 61.0
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert not [r for r in caplog.records if r.levelno == logging.INFO]

    def test_a_backlog_that_is_being_worked_off_is_not_a_stall(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = Clock()
        source = Source(sample(last_flush_at=clock.now))
        reporter = MetricsReporter(source, interval_s=60.0, clock=clock)
        clock.now += 61.0
        source.current = sample(
            queue_size=500, accepted=5_000, flushes=40, last_flush_at=clock.now - 1.0
        )
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]

    def test_an_idle_queue_is_never_a_stall(self, caplog: pytest.LogCaptureFixture) -> None:
        """空闲代理本来就不该有落盘，把它判成卡死会天天误报。"""
        clock = Clock()
        reporter = MetricsReporter(Source(sample()), interval_s=60.0, clock=clock)
        clock.now += 86_400.0
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert caplog.records == []

    def test_a_writer_that_never_flushed_is_measured_from_startup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``last_flush_at == 0`` 是「一次都没落盘」，不是「1970 年落过盘」。"""
        clock = Clock()
        source = Source(sample())
        reporter = MetricsReporter(source, interval_s=60.0, clock=clock)
        source.current = sample(queue_size=10, accepted=10)
        clock.now += 30.0
        with caplog.at_level(logging.INFO, logger=LOGGER):
            reporter.report()
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]


class TestServiceMetrics:
    def test_metrics_reflect_the_running_writer(self, tmp_path: Path) -> None:
        cfg = DatabaseConfig(
            state_path=tmp_path / "state.db",
            logs_path=tmp_path / "logs.db",
            rules_path=tmp_path / "rules.db",
            flush_interval_ms=10,
            write_queue_size=64,
        )
        service = StorageService(cfg, LimitsConfig())
        service.start()
        try:
            service.queue.put(
                sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200)
            )
        finally:
            # 关停会排空队列，因此停完再读指标就不需要额外的同步手段。
            service.stop()
        metrics = service.metrics()
        assert metrics.queue_capacity == 64
        assert metrics.accepted == 1
        assert metrics.flushes >= 1
        assert metrics.rows_written >= 1
        assert metrics.last_flush_at > 0.0
        assert metrics.write_errors == 0

    def test_closing_logs_a_final_tally(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """中间上报可能已被日志轮转冲掉，退出时留一份总账。"""
        cfg = DatabaseConfig(
            state_path=tmp_path / "state.db",
            logs_path=tmp_path / "logs.db",
            rules_path=tmp_path / "rules.db",
        )
        service = StorageService(cfg, LimitsConfig())
        service.start()
        with caplog.at_level(logging.INFO, logger="r_proxy.storage.service"):
            service.stop()
        assert any("存储关闭" in r.getMessage() for r in caplog.records)
