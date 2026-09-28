"""粘性映射的内存侧行为。

对应设计：docs/design/DD_ROUTING.md §7。

粘性是**偏好**，不是硬绑定：失败时本次请求正常切换，绑定关系该不该动取决于
它的来源——``auto`` 是缓存（可失效），``manual`` 是用户的声明（不可静默推翻）。
"""

from __future__ import annotations

from r_proxy.state.sticky import StickyCache, StickyEntry


def cache(
    capacity: int = 10, *, entries: list[StickyEntry] | None = None, ttl: float = 0.0
) -> StickyCache:
    c = StickyCache(capacity, ttl=ttl)
    for entry in entries or []:
        c.restore(entry)
    return c


def manual(host: str, upstream: str) -> StickyEntry:
    return StickyEntry(host=host, upstream=upstream, source="manual")


class TestRecordSuccess:
    def test_first_success_creates_an_auto_binding(self) -> None:
        c = cache()
        assert c.record_success("a.com", "proxy-a", now=1.0) is True
        entry = c.get("a.com")
        assert entry is not None
        assert (entry.upstream, entry.source, entry.hit_count) == ("proxy-a", "auto", 1)

    def test_repeating_the_same_upstream_is_only_a_counter_change(self) -> None:
        """返回 False 表示「绑定关系没变」——落盘走自增语句而非 UPSERT，
        否则写入量会与请求量同数量级。"""
        c = cache()
        c.record_success("a.com", "proxy-a", now=1.0)
        assert c.record_success("a.com", "proxy-a", now=2.0) is False
        entry = c.get("a.com")
        assert entry is not None and entry.hit_count == 2

    def test_a_different_upstream_rebinds(self) -> None:
        c = cache()
        c.record_success("a.com", "proxy-a", now=1.0)
        assert c.record_success("a.com", "proxy-b", now=2.0) is True
        entry = c.get("a.com")
        assert entry is not None and entry.upstream == "proxy-b"

    def test_success_clears_the_failure_counter(self) -> None:
        c = cache()
        c.record_success("a.com", "proxy-a", now=1.0)
        c.record_failure("a.com", threshold=3)
        c.record_success("a.com", "proxy-a", now=2.0)
        entry = c.get("a.com")
        assert entry is not None and entry.fail_count == 0

    def test_a_manual_binding_is_never_overwritten(self) -> None:
        """CC-06 / M3-05：内存侧防线。数据库侧还有 WHERE source != 'manual'。"""
        c = cache(entries=[manual("a.com", "manual-choice")])
        assert c.record_success("a.com", "auto-choice", now=1.0) is False
        entry = c.get("a.com")
        assert entry is not None
        assert (entry.upstream, entry.source) == ("manual-choice", "manual")

    def test_a_manual_binding_still_counts_hits(self) -> None:
        c = cache(entries=[manual("a.com", "manual-choice")])
        c.record_success("a.com", "manual-choice", now=1.0)
        entry = c.get("a.com")
        assert entry is not None and entry.hit_count == 1

    def test_last_used_at_never_goes_backwards(self) -> None:
        """RC-06：乱序完成的请求不该让「最后使用时间」倒退。"""
        c = cache()
        c.record_success("a.com", "proxy-a", now=100.0)
        c.record_success("a.com", "proxy-a", now=50.0)
        entry = c.get("a.com")
        assert entry is not None and entry.last_used_at == 100.0


class TestRecordFailure:
    def test_failures_below_the_threshold_keep_the_binding(self) -> None:
        c = cache()
        c.record_success("a.com", "proxy-a", now=1.0)
        assert c.record_failure("a.com", threshold=3) is False
        assert c.record_failure("a.com", threshold=3) is False
        assert c.get("a.com") is not None

    def test_reaching_the_threshold_clears_an_auto_binding(self) -> None:
        """auto 粘性是「上次哪个出口能用」的缓存，内容过时就该作废重学。"""
        c = cache()
        c.record_success("a.com", "proxy-a", now=1.0)
        for _ in range(2):
            c.record_failure("a.com", threshold=3)
        assert c.record_failure("a.com", threshold=3) is True
        assert c.get("a.com") is None

    def test_a_manual_binding_survives_the_threshold(self) -> None:
        """自动清除 manual 等于系统单方面、静默地推翻用户配置。避免无谓重试
        由 (host, upstream) 负面记忆负责，清除绑定不产生额外收益。"""
        c = cache(entries=[manual("a.com", "manual-choice")])
        for _ in range(10):
            assert c.record_failure("a.com", threshold=3) is False
        assert c.get("a.com") is not None

    def test_a_manual_binding_still_accumulates_the_failure_count(self) -> None:
        """它是给用户看的健康指标，只是不触发清除。"""
        c = cache(entries=[manual("a.com", "manual-choice")])
        c.record_failure("a.com", threshold=3)
        entry = c.get("a.com")
        assert entry is not None and entry.fail_count == 1

    def test_failure_on_an_unknown_host_is_a_no_op(self) -> None:
        assert cache().record_failure("nobody.com", threshold=3) is False


class TestBindManual:
    def test_binding_marks_the_entry_manual(self) -> None:
        c = cache()
        c.bind_manual("a.com", "proxy-a", now=5.0)
        entry = c.get("a.com")
        assert entry is not None
        assert (entry.upstream, entry.source) == ("proxy-a", "manual")

    def test_rebinding_overrides_an_existing_manual_entry(self) -> None:
        """改绑必须生效。沿用自动路径的「不覆盖 manual」护栏会让改绑静默失败。"""
        c = cache(entries=[manual("a.com", "proxy-a")])
        c.bind_manual("a.com", "proxy-b", now=5.0)
        entry = c.get("a.com")
        assert entry is not None and entry.upstream == "proxy-b"

    def test_binding_clears_the_failure_count(self) -> None:
        c = cache()
        c.record_success("a.com", "proxy-a", now=1.0)
        c.record_failure("a.com", threshold=99)
        c.bind_manual("a.com", "proxy-b", now=2.0)
        entry = c.get("a.com")
        assert entry is not None and entry.fail_count == 0

    def test_an_automatic_success_does_not_overwrite_the_binding(self) -> None:
        c = cache()
        c.bind_manual("a.com", "proxy-a", now=1.0)
        assert c.record_success("a.com", "proxy-b", now=2.0) is False
        entry = c.get("a.com")
        assert entry is not None and entry.upstream == "proxy-a"


class TestCapacity:
    def test_the_least_recently_used_entry_is_evicted(self) -> None:
        c = cache(capacity=2)
        c.record_success("a.com", "u", now=1.0)
        c.record_success("b.com", "u", now=2.0)
        c.record_success("c.com", "u", now=3.0)
        assert c.get("a.com") is None
        assert c.size == 2

    def test_reading_an_entry_refreshes_its_position(self) -> None:
        c = cache(capacity=2)
        c.record_success("a.com", "u", now=1.0)
        c.record_success("b.com", "u", now=2.0)
        c.get("a.com")
        c.record_success("c.com", "u", now=3.0)
        assert c.get("a.com") is not None
        assert c.get("b.com") is None

    def test_zero_capacity_disables_the_cache(self) -> None:
        c = cache(capacity=0)
        c.record_success("a.com", "u", now=1.0)
        assert c.get("a.com") is None

    def test_a_manual_binding_outlives_automatic_ones(self) -> None:
        """自动学到的一批映射不该把手动绑定挤掉。

        被挤掉的表现最难查：内存里改回自动选路，库里的 ``manual`` 行还在，
        重启后绑定又回来了。
        """
        c = cache(capacity=2)
        c.bind_manual("pinned.com", "proxy-a", now=1.0)
        c.record_success("b.com", "u", now=2.0)
        c.record_success("c.com", "u", now=3.0)
        assert c.get("pinned.com") is not None
        assert c.get("b.com") is None

    def test_capacity_still_holds_when_everything_is_manual(self) -> None:
        """上限是硬约束：全是 manual 时仍然淘汰最旧的那条，否则内存无界。"""
        c = cache(capacity=2)
        for i in range(4):
            c.bind_manual(f"h{i}.com", "u", now=float(i))
        assert c.size == 2
        assert c.get("h0.com") is None
        assert c.get("h3.com") is not None

    def test_shrinking_the_capacity_evicts_the_oldest(self) -> None:
        c = cache(capacity=4)
        for host in ("a", "b", "c", "d"):
            c.record_success(f"{host}.com", "u", now=1.0)
        c.reconfigure(capacity=2, ttl=0.0)
        assert c.size == 2
        assert c.get("a.com") is None
        assert c.get("d.com") is not None


class TestInspection:
    def test_clear_removes_the_binding(self) -> None:
        c = cache()
        c.record_success("a.com", "u", now=1.0)
        assert c.clear("a.com") is True
        assert c.clear("a.com") is False

    def test_entries_are_returned_as_copies(self) -> None:
        """Web 界面拿到的快照不该能改到权威状态。"""
        c = cache()
        c.record_success("a.com", "u", now=1.0)
        snapshot = c.entries(now=1.0)
        snapshot[0].upstream = "tampered"
        entry = c.get("a.com")
        assert entry is not None and entry.upstream == "u"

    def test_get_returns_a_live_reference_for_the_hot_path(self) -> None:
        """路由决策每请求都要读粘性，复制一份对象是无谓的开销。"""
        c = cache()
        c.record_success("a.com", "u", now=1.0)
        assert c.get("a.com") is c.get("a.com")

    def test_restore_respects_the_capacity(self) -> None:
        c = StickyCache(2)
        for i in range(5):
            c.restore(StickyEntry(host=f"h{i}.com", upstream="u", source="auto"))
        assert c.size == 2


def auto(host: str, upstream: str, *, last_used_at: float) -> StickyEntry:
    return StickyEntry(host=host, upstream=upstream, source="auto", last_used_at=last_used_at)


class TestTtl:
    """``auto`` 空闲超过 ``ttl`` 即失效，``manual`` 永不失效（DD_ROUTING §7.7）。"""

    def test_an_idle_auto_entry_expires_on_the_next_lookup(self) -> None:
        c = cache(ttl=100.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        assert c.get_live("a.com", now=100.0) is None
        # 惰性删除：条目真的没了，不只是这次返回 None。
        assert c.size == 0

    def test_an_auto_entry_just_inside_the_window_survives(self) -> None:
        c = cache(ttl=100.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        entry = c.get_live("a.com", now=99.9)
        assert entry is not None and entry.upstream == "u"

    def test_the_boundary_counts_as_expired(self) -> None:
        """``>=`` 而非 ``>``：恰好满 TTL 即失效，与 RouteMemory 一致。"""
        c = cache(ttl=100.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        assert c.get_live("a.com", now=100.0) is None

    def test_a_manual_entry_never_expires(self) -> None:
        c = cache(ttl=100.0, entries=[manual("a.com", "u")])
        assert c.get_live("a.com", now=10_000_000.0) is not None

    def test_zero_ttl_disables_expiry(self) -> None:
        """``0`` 表示关闭过期，不是「立即过期」（与 happy_eyeballs_delay 同一约定）。"""
        c = cache(ttl=0.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        assert c.get_live("a.com", now=10_000_000.0) is not None

    def test_plain_get_does_not_expire(self) -> None:
        """``get`` 承诺无副作用读取：过期判定只属于 ``get_live``。"""
        c = cache(ttl=100.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        assert c.get("a.com") is not None
        assert c.size == 1

    def test_a_success_refreshes_the_idle_clock(self) -> None:
        """命中一次就重置计时：不然活跃使用的绑定也会被当成冷数据清掉。"""
        c = cache(ttl=100.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        c.record_success("a.com", "u", now=90.0)
        assert c.get_live("a.com", now=150.0) is not None

    def test_reconfigure_does_not_evict_existing_entries_by_the_new_ttl(self) -> None:
        """改小 TTL 不立即清空旧条目——与 RouteMemory.reconfigure 同一立场：
        集体失效等于制造一次集中的重新学习。旧条目在下次访问时才淘汰。"""
        c = cache(ttl=0.0, entries=[auto("a.com", "u", last_used_at=0.0)])
        c.reconfigure(capacity=10, ttl=1.0)
        assert c.size == 1
        assert c.get_live("a.com", now=1000.0) is None

    def test_entries_hides_expired_auto_but_keeps_manual(self) -> None:
        c = cache(
            ttl=100.0,
            entries=[auto("old.com", "u", last_used_at=0.0), manual("pinned.com", "u")],
        )
        hosts = [e.host for e in c.entries(now=100.0)]
        assert hosts == ["pinned.com"]

    def test_entries_does_not_delete_what_it_hides(self) -> None:
        """列表查询只读：过滤靠判定，删除留给 get_live 或 LRU。"""
        c = cache(ttl=100.0, entries=[auto("old.com", "u", last_used_at=0.0)])
        assert c.entries(now=100.0) == []
        assert c.size == 1
