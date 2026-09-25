"""回填装配与健康计数的周期落盘。

对应设计：docs/design/DD_STORAGE.md §4.7、§5。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from r_proxy.config.model import DatabaseConfig, LimitsConfig
from r_proxy.contracts import FailureKind
from r_proxy.persistence import HealthPersister, apply_initial_state
from r_proxy.state.health import HealthState
from r_proxy.state.runtime import RuntimeState
from r_proxy.storage.queue import OpKind, WriteOp
from r_proxy.storage.reader import BlockRow, HealthRow, InitialState, StickyRow
from r_proxy.storage.schema import Database, open_write
from r_proxy.storage.service import StorageService
from tests.conftest import make_snapshot, upstream


class FakeSink:
    def __init__(self) -> None:
        self.ops: list[WriteOp] = []

    def put(self, op: WriteOp) -> bool:
        self.ops.append(op)
        return True


def state() -> RuntimeState:
    return RuntimeState.from_snapshot(make_snapshot(upstream("a"), upstream("b")))


class TestApplyInitialState:
    def test_sticky_bindings_land_in_the_cache(self) -> None:
        rt = state()
        apply_initial_state(
            rt,
            InitialState(
                sticky=(
                    StickyRow(
                        host="a.com",
                        upstream="a",
                        source="manual",
                        fail_count=2,
                        hit_count=9,
                        last_used_at=5.0,
                    ),
                )
            ),
        )
        entry = rt.sticky.get("a.com")
        assert entry is not None
        assert (entry.upstream, entry.source, entry.hit_count) == ("a", "manual", 9)

    def test_an_unknown_source_is_treated_as_auto(self) -> None:
        """库文件可能被手工改过。把未知值当手动绑定会让它永不被自动逻辑纠正。"""
        rt = state()
        apply_initial_state(
            rt,
            InitialState(
                sticky=(
                    StickyRow(
                        host="a.com",
                        upstream="a",
                        source="whatever",
                        fail_count=0,
                        hit_count=0,
                        last_used_at=0.0,
                    ),
                )
            ),
        )
        entry = rt.sticky.get("a.com")
        assert entry is not None and entry.source == "auto"

    def test_route_blocks_keep_blocking(self) -> None:
        rt = state()
        now = time.monotonic()
        apply_initial_state(
            rt,
            InitialState(
                blocks=(
                    BlockRow(
                        host="a.com",
                        upstream="a",
                        fail_count=3,
                        reason="route_error",
                        blocked_until=now + 300,
                    ),
                )
            ),
        )
        assert rt.memory.is_blocked("a.com", "a", now=now) is True

    def test_health_counters_are_restored_but_the_circuit_stays_closed(self) -> None:
        """M3-03：重启本身就是「重新开始」的语义。"""
        rt = state()
        apply_initial_state(
            rt,
            InitialState(
                health=(
                    HealthRow(
                        upstream="a",
                        total_success=10,
                        total_failure=4,
                        avg_latency_ms=12,
                        total_bytes_up=300,
                        total_bytes_down=400,
                    ),
                )
            ),
        )
        snapshot = rt.health.snapshot_of("a")
        assert (snapshot.total_success, snapshot.total_failure) == (10, 4)
        assert (snapshot.total_bytes_up, snapshot.total_bytes_down) == (300, 400)
        assert rt.health.state_of("a", now=0.0) is HealthState.CLOSED


class TestHealthPersister:
    def test_only_the_delta_since_the_last_flush_is_written(self) -> None:
        """传累计值会让 SQL 侧的 ``+ excluded`` 变成重复累加。"""
        rt = state()
        sink = FakeSink()
        persister = HealthPersister(sink)
        for _ in range(3):
            rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        assert persister.flush(rt.health.all(now=0.0), now_unix=1) == 1
        assert sink.ops[0].kind is OpKind.HEALTH_COUNTERS
        assert sink.ops[0].payload[1] == 3

        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        persister.flush(rt.health.all(now=0.0), now_unix=2)
        assert sink.ops[1].payload[1] == 1

    def test_an_unchanged_upstream_is_not_written(self) -> None:
        rt = state()
        sink = FakeSink()
        persister = HealthPersister(sink)
        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        persister.flush(rt.health.all(now=0.0), now_unix=1)
        assert persister.flush(rt.health.all(now=0.0), now_unix=2) == 0

    def test_a_removed_upstream_does_not_produce_a_negative_delta(self) -> None:
        """热重载删掉再加回同名出口时，基线必须一起丢掉。"""
        rt = state()
        sink = FakeSink()
        persister = HealthPersister(sink)
        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        persister.flush(rt.health.all(now=0.0), now_unix=1)

        rt.health.reset("a")
        persister.flush(rt.health.all(now=0.0), now_unix=2)
        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        persister.flush(rt.health.all(now=0.0), now_unix=3)
        assert [op.payload[1] for op in sink.ops] == [1, 1]

    def test_restored_counters_are_not_written_back_at_startup(self) -> None:
        """回填的历史值已经在库里。基线不跟着回填，首次 flush 的增量就是整个
        历史总量，而 SQL 侧是 ``+ excluded``——每重启一次累计计数翻一倍。"""
        rt = state()
        initial = InitialState(
            health=(HealthRow(upstream="a", total_success=10, total_failure=4, avg_latency_ms=12),)
        )
        apply_initial_state(rt, initial)
        sink = FakeSink()
        persister = HealthPersister(sink, initial=initial)
        assert persister.flush(rt.health.all(now=0.0), now_unix=1) == 0
        assert sink.ops == []

    def test_only_the_traffic_since_the_restart_is_written(self) -> None:
        rt = state()
        initial = InitialState(
            health=(HealthRow(upstream="a", total_success=10, total_failure=4, avg_latency_ms=12),)
        )
        apply_initial_state(rt, initial)
        persister = HealthPersister(sink := FakeSink(), initial=initial)
        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        assert persister.flush(rt.health.all(now=0.0), now_unix=1) == 1
        assert (sink.ops[0].payload[1], sink.ops[0].payload[2]) == (2, 0)

    def test_byte_deltas_are_flushed_too(self) -> None:
        """字节累计走同一条自增路径，payload 末两个槽位是本批次的字节增量。"""
        rt = state()
        sink = FakeSink()
        persister = HealthPersister(sink)
        rt.health.add_traffic("a", bytes_up=100, bytes_down=200)
        assert persister.flush(rt.health.all(now=0.0), now_unix=1) == 1
        assert sink.ops[0].payload[9:11] == (100, 200)

        rt.health.add_traffic("a", bytes_up=50, bytes_down=25)
        persister.flush(rt.health.all(now=0.0), now_unix=2)
        assert sink.ops[1].payload[9:11] == (50, 25)

    def test_a_baseline_for_an_upstream_no_longer_configured_is_dropped(self) -> None:
        """库里留着已删出口的计数很正常。它不在内存健康表里，基线要能被裁掉，
        否则同名出口日后重建时第一个增量会是负数。"""
        rt = state()
        initial = InitialState(
            health=(HealthRow(upstream="gone", total_success=7, total_failure=1, avg_latency_ms=0),)
        )
        apply_initial_state(rt, initial)
        rt.health.reset("gone")
        persister = HealthPersister(sink := FakeSink(), initial=initial)
        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        persister.flush(rt.health.all(now=0.0), now_unix=1)
        assert [op.payload[0] for op in sink.ops] == ["a"]

    def test_the_circuit_state_is_written_as_its_sql_name(self) -> None:
        rt = state()
        sink = FakeSink()
        rt.health.record_result("a", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=0.0)
        HealthPersister(sink).flush(rt.health.all(now=0.0), now_unix=1)
        assert sink.ops[0].payload[5] in {"closed", "open", "half_open"}

    async def test_cancelling_run_flushes_the_partial_window(self) -> None:
        """关停发生在两次周期之间：不能把这最后不足一个周期的增量丢在内存里。"""
        rt = state()
        sink = FakeSink()
        persister = HealthPersister(sink, interval=100.0)
        task = asyncio.create_task(persister.run(rt))
        await asyncio.sleep(0)  # 让任务跑到 `await asyncio.sleep(100.0)` 那一行

        rt.health.record_result("a", ok=True, kind=FailureKind.NOT_A_FAILURE, now=0.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert sink.ops and sink.ops[0].payload[0] == "a"
        assert sink.ops[0].payload[1] == 1


class TestStorageService:
    def test_the_initial_state_is_read_before_the_writer_starts(self, tmp_path: Path) -> None:
        cfg = DatabaseConfig(
            state_path=tmp_path / "state.db",
            logs_path=tmp_path / "logs.db",
            rules_path=tmp_path / "rules.db",
        )
        conn = open_write(cfg.state_path, Database.STATE)
        try:
            conn.execute(
                "INSERT INTO host_upstream (host, upstream_name, source, updated_at)"
                " VALUES ('a.com', 'a', 'auto', ?)",
                (int(time.time()),),
            )
        finally:
            conn.close()

        service = StorageService(cfg, LimitsConfig())
        initial = service.load_initial_state()
        service.start()
        try:
            assert [row.host for row in initial.sticky] == ["a.com"]
        finally:
            service.stop()

    def test_a_first_run_starts_from_an_empty_state(self, tmp_path: Path) -> None:
        cfg = DatabaseConfig(
            state_path=tmp_path / "state.db",
            logs_path=tmp_path / "logs.db",
            rules_path=tmp_path / "rules.db",
        )
        service = StorageService(cfg, LimitsConfig())
        assert service.load_initial_state().is_empty
        service.start()
        service.stop()
        assert cfg.state_path.is_file()
        assert cfg.logs_path.is_file()
