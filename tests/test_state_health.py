"""state/health.py 的熔断状态机测试。

对应设计：docs/design/DD_ROUTING.md §4
"""

from __future__ import annotations

import pytest

from r_proxy.config.model import CircuitBreakerConfig
from r_proxy.contracts import FailureKind
from r_proxy.state.health import HealthState, HealthTable

CFG = CircuitBreakerConfig(fail_threshold=5, cooldown_seconds=60)


@pytest.fixture
def table() -> HealthTable:
    return HealthTable(CFG)


def fail(table: HealthTable, name: str, *, times: int, now: float = 0.0) -> None:
    for i in range(times):
        table.record_result(
            name, ok=False, kind=FailureKind.UPSTREAM_ERROR, now=now + i, error="ECONNREFUSED"
        )


class TestUnknownUpstream:
    def test_never_seen_upstream_is_available(self, table: HealthTable) -> None:
        assert table.is_available("proxy", now=0.0) is True

    def test_state_of_unknown_upstream_is_closed(self, table: HealthTable) -> None:
        assert table.state_of("proxy", now=0.0) is HealthState.CLOSED


class TestOpening:
    def test_stays_closed_below_the_threshold(self, table: HealthTable) -> None:
        fail(table, "proxy", times=4)
        assert table.is_available("proxy", now=10.0) is True
        assert table.state_of("proxy", now=10.0) is HealthState.CLOSED

    def test_opens_at_the_threshold(self, table: HealthTable) -> None:
        fail(table, "proxy", times=5)
        assert table.is_available("proxy", now=10.0) is False
        assert table.state_of("proxy", now=10.0) is HealthState.OPEN

    def test_success_clears_the_failure_streak(self, table: HealthTable) -> None:
        """必须是「连续」失败：中间一次成功就重新计数。"""
        fail(table, "proxy", times=4)
        table.record_result("proxy", ok=True, kind=FailureKind.ROUTE_ERROR, now=5.0)
        fail(table, "proxy", times=4, now=6.0)
        assert table.is_available("proxy", now=20.0) is True

    def test_route_error_does_not_count_toward_the_breaker(self, table: HealthTable) -> None:
        """route_error 说明代理活着，只是到不了这个目标。"""
        for i in range(20):
            table.record_result("proxy", ok=False, kind=FailureKind.ROUTE_ERROR, now=float(i))
        assert table.is_available("proxy", now=30.0) is True

    def test_capability_mismatch_does_not_count_toward_the_breaker(
        self, table: HealthTable
    ) -> None:
        for i in range(20):
            table.record_result(
                "proxy", ok=False, kind=FailureKind.CAPABILITY_MISMATCH, now=float(i)
            )
        assert table.is_available("proxy", now=30.0) is True

    def test_not_a_failure_does_not_count_toward_the_breaker(self, table: HealthTable) -> None:
        for i in range(20):
            table.record_result("proxy", ok=False, kind=FailureKind.NOT_A_FAILURE, now=float(i))
        assert table.is_available("proxy", now=30.0) is True


class TestDirectNeverOpens:
    def test_direct_never_opens_however_many_failures(self, table: HealthTable) -> None:
        """M2-12：direct 被熔断会让内网与 localhost 全部不可访问。"""
        for i in range(20):
            table.record_result("direct", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
        assert table.is_available("direct", now=100.0) is True
        assert table.state_of("direct", now=100.0) is HealthState.CLOSED

    def test_direct_failures_are_still_counted_for_display(self, table: HealthTable) -> None:
        fail(table, "direct", times=3)
        assert table.snapshot_of("direct").total_failure == 3

    def test_direct_downgrade_happens_at_the_table_entry(self, table: HealthTable) -> None:
        """强制降级放在唯一收敛点，不依赖每个调用方都传对 kind。"""
        table.record_result(
            "direct", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=0.0, error="TimeoutError"
        )
        assert table.snapshot_of("direct").consecutive_failures == 0


class TestCooldownAndHalfOpen:
    def test_stays_unavailable_during_cooldown(self, table: HealthTable) -> None:
        fail(table, "proxy", times=5)
        assert table.is_available("proxy", now=4.0 + 59.0) is False

    def test_migrates_to_half_open_after_cooldown(self, table: HealthTable) -> None:
        """迁移在查询时惰性判定，不用定时器。"""
        fail(table, "proxy", times=5)
        assert table.is_available("proxy", now=200.0) is True
        assert table.state_of("proxy", now=200.0) is HealthState.HALF_OPEN

    def test_half_open_admits_exactly_one_probe(self, table: HealthTable) -> None:
        """M2-13：half_open 期间 10 个并发只放行 1 个。"""
        fail(table, "proxy", times=5)
        table.is_available("proxy", now=200.0)
        admitted = [table.acquire_probe("proxy") for _ in range(10)]
        assert admitted.count(True) == 1

    def test_closed_state_never_needs_a_probe_slot(self, table: HealthTable) -> None:
        assert all(table.acquire_probe("proxy") for _ in range(10))

    def test_successful_probe_closes_the_breaker(self, table: HealthTable) -> None:
        fail(table, "proxy", times=5)
        table.is_available("proxy", now=200.0)
        assert table.acquire_probe("proxy") is True
        table.record_result("proxy", ok=True, kind=FailureKind.ROUTE_ERROR, now=201.0)
        assert table.state_of("proxy", now=201.0) is HealthState.CLOSED
        assert table.is_available("proxy", now=201.0) is True

    def test_failed_probe_reopens_and_restarts_the_cooldown(self, table: HealthTable) -> None:
        """不重置 opened_at 会让下一个请求立刻又变 half_open，退化为无冷却重试。"""
        fail(table, "proxy", times=5)
        table.is_available("proxy", now=200.0)
        table.acquire_probe("proxy")
        table.record_result("proxy", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=201.0)

        assert table.state_of("proxy", now=201.0) is HealthState.OPEN
        assert table.is_available("proxy", now=250.0) is False
        assert table.is_available("proxy", now=262.0) is True

    def test_a_single_upstream_error_in_half_open_reopens_it(self, table: HealthTable) -> None:
        """half_open 下不需要再攒够 threshold 次。"""
        fail(table, "proxy", times=5)
        table.is_available("proxy", now=200.0)
        table.record_result("proxy", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=201.0)
        assert table.state_of("proxy", now=201.0) is HealthState.OPEN

    def test_route_error_in_half_open_releases_the_slot_without_reopening(
        self, table: HealthTable
    ) -> None:
        fail(table, "proxy", times=5)
        table.is_available("proxy", now=200.0)
        table.acquire_probe("proxy")
        table.record_result("proxy", ok=False, kind=FailureKind.ROUTE_ERROR, now=201.0)
        assert table.state_of("proxy", now=201.0) is HealthState.HALF_OPEN
        assert table.acquire_probe("proxy") is True

    def test_probe_slot_is_released_when_the_result_arrives(self, table: HealthTable) -> None:
        fail(table, "proxy", times=5)
        table.is_available("proxy", now=200.0)
        table.acquire_probe("proxy")
        assert table.acquire_probe("proxy") is False
        table.record_result("proxy", ok=False, kind=FailureKind.ROUTE_ERROR, now=201.0)
        assert table.acquire_probe("proxy") is True


class TestMonotonicTimestamps:
    def test_out_of_order_success_does_not_move_the_timestamp_backwards(
        self, table: HealthTable
    ) -> None:
        """RC-06：先发起后完成的请求会带来更早的时间戳。"""
        table.record_result("proxy", ok=True, kind=FailureKind.ROUTE_ERROR, now=100.0)
        table.record_result("proxy", ok=True, kind=FailureKind.ROUTE_ERROR, now=50.0)
        assert table.snapshot_of("proxy").last_success_at == 100.0


class TestAuthError:
    def test_auth_error_is_recorded_but_keeps_the_upstream_usable(self, table: HealthTable) -> None:
        """用户可能正在修凭据，或该代理只对部分目标要求认证。"""
        table.mark_auth_error("proxy")
        assert table.snapshot_of("proxy").auth_error is True
        assert table.is_available("proxy", now=1.0) is True

    def test_auth_error_is_cleared_on_success(self, table: HealthTable) -> None:
        table.mark_auth_error("proxy")
        table.record_result("proxy", ok=True, kind=FailureKind.ROUTE_ERROR, now=2.0)
        assert table.snapshot_of("proxy").auth_error is False

    def test_repeated_auth_errors_still_open_the_breaker_through_upstream_error(
        self, table: HealthTable
    ) -> None:
        """407 计 UPSTREAM_ERROR，连续 5 次自然熔断，不需要额外的移除逻辑。"""
        for i in range(5):
            table.mark_auth_error("proxy")
            table.record_result("proxy", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
        assert table.is_available("proxy", now=6.0) is False


class TestObservability:
    def test_counts_successes_and_failures(self, table: HealthTable) -> None:
        table.record_result("proxy", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        table.record_result("proxy", ok=False, kind=FailureKind.ROUTE_ERROR, now=2.0)
        snap = table.snapshot_of("proxy")
        assert (snap.total_success, snap.total_failure) == (1, 1)

    def test_records_the_last_error(self, table: HealthTable) -> None:
        table.record_result(
            "proxy", ok=False, kind=FailureKind.ROUTE_ERROR, now=1.0, error="TimeoutError"
        )
        assert table.snapshot_of("proxy").last_error == "TimeoutError"

    def test_snapshot_is_a_copy(self, table: HealthTable) -> None:
        """Web 层拿到的快照不得能改动权威状态。"""
        snap = table.snapshot_of("proxy")
        snap.total_failure = 999
        assert table.snapshot_of("proxy").total_failure == 0

    def test_all_returns_every_known_upstream(self, table: HealthTable) -> None:
        table.record_result("a", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        table.record_result("b", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        assert {h.name for h in table.all(now=1.0)} == {"a", "b"}

    def test_state_queries_use_the_same_lazy_migration_as_routing(self, table: HealthTable) -> None:
        """Web 查询也必须传 now 走同一份判定，否则显示 open 而实际冷却已过。"""
        fail(table, "proxy", times=5)
        assert table.state_of("proxy", now=10.0) is HealthState.OPEN
        assert table.state_of("proxy", now=200.0) is HealthState.HALF_OPEN


class TestTraffic:
    def test_add_traffic_accumulates_on_unknown_upstream(self, table: HealthTable) -> None:
        table.add_traffic("proxy", bytes_up=100, bytes_down=200)
        snap = table.snapshot_of("proxy")
        assert (snap.total_bytes_up, snap.total_bytes_down) == (100, 200)

    def test_add_traffic_accumulates_across_calls(self, table: HealthTable) -> None:
        table.add_traffic("proxy", bytes_up=100, bytes_down=200)
        table.add_traffic("proxy", bytes_up=50, bytes_down=25)
        snap = table.snapshot_of("proxy")
        assert (snap.total_bytes_up, snap.total_bytes_down) == (150, 225)

    def test_add_traffic_is_independent_of_the_circuit_breaker(self, table: HealthTable) -> None:
        """已经跑出去的字节是真实流量，即便这次尝试最终判定为失败。"""
        fail(table, "proxy", times=5)
        table.add_traffic("proxy", bytes_up=10, bytes_down=20)
        snap = table.snapshot_of("proxy")
        assert (snap.total_bytes_up, snap.total_bytes_down) == (10, 20)
        assert table.state_of("proxy", now=10.0) is HealthState.OPEN

    def test_restore_counters_seeds_bytes_from_backfill(self, table: HealthTable) -> None:
        table.restore_counters(
            "proxy", total_success=1, total_failure=2, total_bytes_up=300, total_bytes_down=400
        )
        snap = table.snapshot_of("proxy")
        assert (snap.total_bytes_up, snap.total_bytes_down) == (300, 400)

    def test_restore_counters_defaults_bytes_to_zero(self, table: HealthTable) -> None:
        """旧库升级前从未记过这两列，回填按「还没测过」处理为 0。"""
        table.restore_counters("proxy", total_success=1, total_failure=0)
        snap = table.snapshot_of("proxy")
        assert (snap.total_bytes_up, snap.total_bytes_down) == (0, 0)


class TestReset:
    def test_reset_clears_all_state(self, table: HealthTable) -> None:
        fail(table, "proxy", times=5)
        table.reset("proxy")
        assert table.is_available("proxy", now=10.0) is True
        assert table.snapshot_of("proxy").total_failure == 0

    def test_reconfigure_applies_the_new_threshold_without_losing_history(
        self, table: HealthTable
    ) -> None:
        fail(table, "proxy", times=3)
        table.reconfigure(CircuitBreakerConfig(fail_threshold=3, cooldown_seconds=60))
        fail(table, "proxy", times=1, now=10.0)
        assert table.is_available("proxy", now=11.0) is False

    def test_disabled_breaker_never_opens(self, table: HealthTable) -> None:
        table.reconfigure(CircuitBreakerConfig(enabled=False))
        fail(table, "proxy", times=50)
        assert table.is_available("proxy", now=60.0) is True

    def test_forget_removes_upstreams_dropped_by_a_reload(self, table: HealthTable) -> None:
        table.record_result("gone", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        table.record_result("kept", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        table.forget_except({"kept"})
        assert {h.name for h in table.all(now=1.0)} == {"kept"}
