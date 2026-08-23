"""state/memory.py 的 (host, upstream) 负面记忆测试。

对应设计：docs/design/DD_ROUTING.md §6
"""

from __future__ import annotations

from r_proxy.state.memory import RouteBlock, RouteMemory


class TestBlocking:
    def test_unknown_pair_is_not_blocked(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        assert memory.is_blocked("a.com", "proxy", now=0.0) is False

    def test_blocked_pair_is_reported(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy", now=1.0) is True

    def test_block_is_scoped_to_the_pair_not_the_upstream(self) -> None:
        """SW-09：route_error 只影响该 host，其他 host 不受影响。"""
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert memory.is_blocked("b.com", "proxy", now=1.0) is False

    def test_block_is_scoped_to_the_pair_not_the_host(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy-a", now=0.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy-b", now=1.0) is False

    def test_block_counts_repeated_failures(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        for i in range(3):
            memory.block("a.com", "proxy", now=float(i), reason="route_error")
        entry = memory.entry("a.com", "proxy")
        assert entry is not None
        assert entry.fail_count == 3

    def test_reblocking_extends_the_ttl(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.block("a.com", "proxy", now=500.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy", now=1000.0) is True

    def test_records_the_latest_reason(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.block("a.com", "proxy", now=1.0, reason="tunnel_premature_death")
        entry = memory.entry("a.com", "proxy")
        assert entry is not None
        assert entry.last_reason == "tunnel_premature_death"


class TestExpiry:
    def test_expires_after_the_ttl(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy", now=599.0) is True
        assert memory.is_blocked("a.com", "proxy", now=600.0) is False

    def test_expired_entry_is_dropped_on_query(self) -> None:
        """惰性过期：把清理成本摊到查询上，不需要定时扫全表。"""
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.is_blocked("a.com", "proxy", now=700.0)
        assert memory.size == 0

    def test_expired_then_reblocked_starts_a_fresh_count(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.is_blocked("a.com", "proxy", now=700.0)
        memory.block("a.com", "proxy", now=700.0, reason="route_error")
        entry = memory.entry("a.com", "proxy")
        assert entry is not None
        assert entry.fail_count == 1


class TestClearOnSuccess:
    def test_success_clears_the_memory_completely(self) -> None:
        """网络恢复是二值的：一次成功就说明这条路通了。"""
        memory = RouteMemory(capacity=10, ttl=600.0)
        for i in range(5):
            memory.block("a.com", "proxy", now=float(i), reason="route_error")
        memory.clear("a.com", "proxy")
        assert memory.is_blocked("a.com", "proxy", now=6.0) is False
        assert memory.entry("a.com", "proxy") is None

    def test_clearing_an_unknown_pair_is_a_no_op(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.clear("a.com", "proxy")
        assert memory.size == 0

    def test_clear_does_not_touch_other_pairs(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy-a", now=0.0, reason="route_error")
        memory.block("a.com", "proxy-b", now=0.0, reason="route_error")
        memory.clear("a.com", "proxy-a")
        assert memory.is_blocked("a.com", "proxy-b", now=1.0) is True

    def test_clear_reports_whether_anything_was_removed(self) -> None:
        """调用方据此决定是否入队 DELETE：多数成功请求本来就没有记忆。"""
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert memory.clear("a.com", "proxy") is True
        assert memory.clear("a.com", "proxy") is False


class TestRestore:
    def test_a_restored_block_keeps_blocking(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.restore(
            RouteBlock(
                host="a.com",
                upstream="proxy",
                blocked_until=300.0,
                fail_count=4,
                last_reason="route_error",
            )
        )
        assert memory.is_blocked("a.com", "proxy", now=299.0) is True
        entry = memory.entry("a.com", "proxy")
        assert entry is not None and entry.fail_count == 4

    def test_restore_respects_the_capacity(self) -> None:
        memory = RouteMemory(capacity=2, ttl=600.0)
        for i in range(5):
            memory.restore(
                RouteBlock(
                    host=f"h{i}.com",
                    upstream="proxy",
                    blocked_until=300.0,
                    fail_count=1,
                    last_reason="route_error",
                )
            )
        assert memory.size == 2


class TestCapacity:
    def test_evicts_the_least_recently_used_entry(self) -> None:
        """RL-04：容量到顶后内存不再增长。"""
        memory = RouteMemory(capacity=2, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.block("b.com", "proxy", now=1.0, reason="route_error")
        memory.block("c.com", "proxy", now=2.0, reason="route_error")
        assert memory.size == 2
        assert memory.is_blocked("a.com", "proxy", now=3.0) is False

    def test_a_query_protects_an_entry_from_eviction(self) -> None:
        """LRU 顺序近似「最久未访问」，与「最可能已过期」高度相关。"""
        memory = RouteMemory(capacity=2, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.block("b.com", "proxy", now=1.0, reason="route_error")
        memory.is_blocked("a.com", "proxy", now=2.0)
        memory.block("c.com", "proxy", now=3.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy", now=4.0) is True
        assert memory.is_blocked("b.com", "proxy", now=4.0) is False

    def test_stays_bounded_under_many_distinct_pairs(self) -> None:
        memory = RouteMemory(capacity=50, ttl=600.0)
        for i in range(1000):
            memory.block(f"h{i}.com", "proxy", now=float(i), reason="route_error")
        assert memory.size == 50

    def test_zero_capacity_never_blocks_anything(self) -> None:
        memory = RouteMemory(capacity=0, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy", now=1.0) is False


class TestIntrospection:
    def test_entries_returns_live_blocks_only(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.block("b.com", "proxy", now=700.0, reason="route_error")
        hosts = {e.host for e in memory.entries(now=800.0)}
        assert hosts == {"b.com"}

    def test_entry_is_a_copy(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        entry = memory.entry("a.com", "proxy")
        assert entry is not None
        entry.fail_count = 999
        live = memory.entry("a.com", "proxy")
        assert live is not None
        assert live.fail_count == 1

    def test_reconfigure_keeps_existing_memory(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.reconfigure(capacity=5, ttl=60.0)
        assert memory.is_blocked("a.com", "proxy", now=1.0) is True

    def test_reconfigure_trims_to_the_new_capacity(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        for i in range(6):
            memory.block(f"h{i}.com", "proxy", now=float(i), reason="route_error")
        memory.reconfigure(capacity=2, ttl=600.0)
        assert memory.size == 2

    def test_reconfigure_does_not_retroactively_shorten_ttl(self) -> None:
        """缩短 TTL 若回溯生效，会让一批记忆同时失效、造成重试风暴。"""
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        memory.reconfigure(capacity=10, ttl=10.0)
        assert memory.is_blocked("a.com", "proxy", now=100.0) is True

    def test_reconfigured_ttl_applies_to_new_blocks(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.reconfigure(capacity=10, ttl=10.0)
        memory.block("a.com", "proxy", now=0.0, reason="route_error")
        assert memory.is_blocked("a.com", "proxy", now=11.0) is False

    def test_forget_upstreams_drops_entries_for_removed_upstreams(self) -> None:
        memory = RouteMemory(capacity=10, ttl=600.0)
        memory.block("a.com", "gone", now=0.0, reason="route_error")
        memory.block("a.com", "kept", now=0.0, reason="route_error")
        memory.forget_upstreams_except({"kept"})
        assert memory.is_blocked("a.com", "gone", now=1.0) is False
        assert memory.is_blocked("a.com", "kept", now=1.0) is True
