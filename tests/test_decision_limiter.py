"""decision/limiter.py 的每 host 滑动窗口限流测试。

对应设计：docs/design/DD_SWITCHING.md §6
"""

from __future__ import annotations

from r_proxy.config.model import RateLimitConfig
from r_proxy.decision.limiter import SwitchRateLimiter

CFG = RateLimitConfig(max_switches_per_host=3, window_seconds=60)


class TestQuota:
    def test_allows_up_to_the_limit(self) -> None:
        limiter = SwitchRateLimiter()
        assert [limiter.try_consume("a.com", now=0.0, cfg=CFG) for _ in range(3)] == [True] * 3

    def test_rejects_beyond_the_limit(self) -> None:
        limiter = SwitchRateLimiter()
        for _ in range(3):
            limiter.try_consume("a.com", now=0.0, cfg=CFG)
        assert limiter.try_consume("a.com", now=0.0, cfg=CFG) is False

    def test_quota_is_per_host(self) -> None:
        limiter = SwitchRateLimiter()
        for _ in range(3):
            limiter.try_consume("a.com", now=0.0, cfg=CFG)
        assert limiter.try_consume("b.com", now=0.0, cfg=CFG) is True

    def test_quota_recovers_as_the_window_slides(self) -> None:
        limiter = SwitchRateLimiter()
        for _ in range(3):
            limiter.try_consume("a.com", now=0.0, cfg=CFG)
        assert limiter.try_consume("a.com", now=59.9, cfg=CFG) is False
        assert limiter.try_consume("a.com", now=60.1, cfg=CFG) is True

    def test_window_slides_gradually_not_all_at_once(self) -> None:
        limiter = SwitchRateLimiter()
        for offset in (0.0, 10.0, 20.0):
            assert limiter.try_consume("a.com", now=offset, cfg=CFG) is True
        # 只有第一次的时间戳滑出窗口，配额恢复 1 个。
        assert limiter.try_consume("a.com", now=61.0, cfg=CFG) is True
        assert limiter.try_consume("a.com", now=61.0, cfg=CFG) is False

    def test_zero_limit_rejects_everything(self) -> None:
        limiter = SwitchRateLimiter()
        cfg = RateLimitConfig(max_switches_per_host=0, window_seconds=60)
        assert limiter.try_consume("a.com", now=0.0, cfg=cfg) is False


class TestPeek:
    def test_peek_reports_remaining_quota(self) -> None:
        limiter = SwitchRateLimiter()
        assert limiter.peek("a.com", now=0.0, cfg=CFG) == 3
        limiter.try_consume("a.com", now=0.0, cfg=CFG)
        assert limiter.peek("a.com", now=0.0, cfg=CFG) == 2

    def test_peek_does_not_consume_quota(self) -> None:
        """Web 界面查询剩余配额时不能把配额用掉。"""
        limiter = SwitchRateLimiter()
        for _ in range(10):
            limiter.peek("a.com", now=0.0, cfg=CFG)
        assert limiter.try_consume("a.com", now=0.0, cfg=CFG) is True

    def test_peek_accounts_for_the_sliding_window(self) -> None:
        limiter = SwitchRateLimiter()
        for _ in range(3):
            limiter.try_consume("a.com", now=0.0, cfg=CFG)
        assert limiter.peek("a.com", now=0.0, cfg=CFG) == 0
        assert limiter.peek("a.com", now=61.0, cfg=CFG) == 3

    def test_peek_never_reports_negative(self) -> None:
        limiter = SwitchRateLimiter()
        cfg = RateLimitConfig(max_switches_per_host=1, window_seconds=60)
        limiter.try_consume("a.com", now=0.0, cfg=cfg)
        assert limiter.peek("a.com", now=0.0, cfg=cfg) == 0


class TestCapacity:
    def test_evicts_least_recently_used_hosts(self) -> None:
        """大量不同 host 不能把内存撑爆。"""
        limiter = SwitchRateLimiter(capacity=2)
        limiter.try_consume("a.com", now=0.0, cfg=CFG)
        limiter.try_consume("b.com", now=0.0, cfg=CFG)
        limiter.try_consume("c.com", now=0.0, cfg=CFG)
        assert limiter.tracked_hosts == 2

    def test_recent_use_protects_a_host_from_eviction(self) -> None:
        limiter = SwitchRateLimiter(capacity=2)
        limiter.try_consume("a.com", now=0.0, cfg=CFG)
        limiter.try_consume("b.com", now=0.0, cfg=CFG)
        limiter.try_consume("a.com", now=1.0, cfg=CFG)  # a 变为最近使用
        limiter.try_consume("c.com", now=2.0, cfg=CFG)  # 淘汰 b
        assert limiter.peek("a.com", now=2.0, cfg=CFG) == 1
        assert limiter.peek("b.com", now=2.0, cfg=CFG) == 3

    def test_window_entries_are_bounded_by_the_limit(self) -> None:
        """窗口内最多留 max_switches_per_host 个时间戳，内存可忽略。"""
        limiter = SwitchRateLimiter()
        for i in range(100):
            limiter.try_consume("a.com", now=float(i) * 0.1, cfg=CFG)
        assert limiter.window_size("a.com") <= CFG.max_switches_per_host
