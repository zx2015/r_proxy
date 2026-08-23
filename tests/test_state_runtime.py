"""state/runtime.py 的游标与状态聚合测试。

对应设计：docs/design/DD_ROUTING.md §3.2
"""

from __future__ import annotations

from r_proxy.config.model import LimitsConfig
from r_proxy.contracts import FailureKind
from r_proxy.state.runtime import CursorTable, RuntimeState
from tests.conftest import make_snapshot, upstream


class TestCursorTable:
    def test_starts_at_zero(self) -> None:
        assert CursorTable().next(10) == 0

    def test_advances_monotonically(self) -> None:
        cursors = CursorTable()
        assert [cursors.next(10) for _ in range(4)] == [0, 1, 2, 3]

    def test_never_wraps_around(self) -> None:
        """不取模：取模在使用点做，组成员数量变化时旧游标不会越界。"""
        cursors = CursorTable()
        for _ in range(100):
            cursors.next(10)
        assert cursors.next(10) == 100

    def test_each_priority_group_has_its_own_cursor(self) -> None:
        cursors = CursorTable()
        cursors.next(10)
        cursors.next(10)
        assert cursors.next(50) == 0

    def test_reset_all_clears_every_cursor(self) -> None:
        cursors = CursorTable()
        cursors.next(10)
        cursors.reset_all()
        assert cursors.next(10) == 0


class TestRuntimeState:
    def test_builds_memory_with_the_configured_capacity(self) -> None:
        snap = make_snapshot(upstream("proxy"), limits=LimitsConfig(route_block_cache_size=32))
        state = RuntimeState.from_snapshot(snap)
        for i in range(100):
            state.memory.block(f"h{i}.com", "proxy", now=float(i), reason="route_error")
        assert state.memory.size == 32

    def test_memory_ttl_comes_from_routing_config(self) -> None:
        from r_proxy.config.model import RoutingConfig

        snap = make_snapshot(upstream("proxy"), routing=RoutingConfig(route_block_ttl=30))
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert state.memory.is_blocked("a.com", "proxy", now=29.0) is True
        assert state.memory.is_blocked("a.com", "proxy", now=31.0) is False

    def test_ipv6_egress_defaults_to_absent(self) -> None:
        state = RuntimeState.from_snapshot(make_snapshot())
        assert state.has_ipv6_egress is False

    def test_ipv6_egress_capability_is_injected_not_probed(self) -> None:
        """状态层不做 I/O：探测在 egress 层，结果由 app 注入。"""
        state = RuntimeState.from_snapshot(make_snapshot())
        state.set_ipv6_egress(True)
        assert state.has_ipv6_egress is True

    def test_reload_drops_state_for_removed_upstreams(self) -> None:
        state = RuntimeState.from_snapshot(make_snapshot(upstream("gone"), upstream("kept")))
        state.health.record_result("gone", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        state.health.record_result("kept", ok=True, kind=FailureKind.ROUTE_ERROR, now=1.0)
        state.memory.block("a.com", "gone", now=1.0, reason="route_error")

        state.on_reload(make_snapshot(upstream("kept")))

        assert {h.name for h in state.health.all(now=2.0)} == {"kept"}
        assert state.memory.is_blocked("a.com", "gone", now=2.0) is False

    def test_reload_keeps_health_of_surviving_upstreams(self) -> None:
        """热重载不该把仍在配置里的出口的熔断状态抹掉。"""
        state = RuntimeState.from_snapshot(make_snapshot(upstream("kept")))
        for i in range(5):
            state.health.record_result(
                "kept", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i)
            )
        state.on_reload(make_snapshot(upstream("kept")))
        assert state.health.is_available("kept", now=5.0) is False

    def test_reload_keeps_the_ipv6_capability_until_reprobed(self) -> None:
        state = RuntimeState.from_snapshot(make_snapshot())
        state.set_ipv6_egress(True)
        state.on_reload(make_snapshot())
        assert state.has_ipv6_egress is True

    def test_reload_resets_cursors(self) -> None:
        """组成员可能变了，旧的轮询位置不再有意义。"""
        state = RuntimeState.from_snapshot(make_snapshot())
        state.cursors.next(10)
        state.on_reload(make_snapshot())
        assert state.cursors.next(10) == 0

    def test_reload_rebuilds_memory_capacity(self) -> None:
        state = RuntimeState.from_snapshot(make_snapshot())
        state.on_reload(make_snapshot(limits=LimitsConfig(route_block_cache_size=2)))
        for i in range(10):
            state.memory.block(f"h{i}.com", "proxy", now=float(i), reason="route_error")
        assert state.memory.size == 2

    def test_reload_preserves_route_memory_of_surviving_upstreams(self) -> None:
        state = RuntimeState.from_snapshot(make_snapshot(upstream("kept")))
        state.memory.block("a.com", "kept", now=0.0, reason="route_error")
        state.on_reload(make_snapshot(upstream("kept")))
        assert state.memory.is_blocked("a.com", "kept", now=1.0) is True

    def test_builds_sticky_with_the_configured_capacity(self) -> None:
        snap = make_snapshot(upstream("proxy"), limits=LimitsConfig(sticky_cache_size=4))
        state = RuntimeState.from_snapshot(snap)
        for i in range(10):
            state.sticky.record_success(f"h{i}.com", "proxy", now=float(i))
        assert state.sticky.size == 4

    def test_reload_drops_sticky_bindings_to_removed_upstreams(self) -> None:
        """出口都不存在了，保留用户的绑定意图也无从执行。"""
        state = RuntimeState.from_snapshot(make_snapshot(upstream("gone"), upstream("kept")))
        state.sticky.record_success("a.com", "gone", now=0.0)
        state.sticky.record_success("b.com", "kept", now=0.0)

        state.on_reload(make_snapshot(upstream("kept")))

        assert state.sticky.get("a.com") is None
        assert state.sticky.get("b.com") is not None

    def test_reload_rebuilds_sticky_capacity(self) -> None:
        state = RuntimeState.from_snapshot(make_snapshot(upstream("proxy")))
        for i in range(10):
            state.sticky.record_success(f"h{i}.com", "proxy", now=float(i))
        state.on_reload(make_snapshot(upstream("proxy"), limits=LimitsConfig(sticky_cache_size=3)))
        assert state.sticky.size == 3
