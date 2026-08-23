"""状态码触发的切换频率限流。

对应设计：docs/design/DD_SWITCHING.md §6。

**只限状态码触发的切换**：传输层失败是真实的链路故障，限流它会让代理在
网络抖动时失去自愈能力。
"""

from __future__ import annotations

from collections import OrderedDict, deque

from r_proxy.config.model import RateLimitConfig

DEFAULT_CAPACITY = 10000


class SwitchRateLimiter:
    """每 host 的滑动窗口计数。不持久化——60 秒生命期的计数器不值得落盘。"""

    __slots__ = ("_capacity", "_windows")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._windows: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def tracked_hosts(self) -> int:
        return len(self._windows)

    def try_consume(self, host: str, *, now: float, cfg: RateLimitConfig) -> bool:
        """消耗一个配额。返回 ``False`` 表示超限，调用方不应切换。"""
        window = self._window_for(host)
        _evict_expired(window, now=now, cfg=cfg)
        if len(window) >= cfg.max_switches_per_host:
            return False
        window.append(now)
        return True

    def peek(self, host: str, *, now: float, cfg: RateLimitConfig) -> int:
        """剩余配额。供 Web 界面展示，**不消耗**配额。

        与 :meth:`try_consume` 分开而不是合并成带 ``dry_run`` 的方法：
        合并后调用点更容易传错，而传错的后果是静默地把配额用光。
        """
        window = self._windows.get(host)
        if window is None:
            return cfg.max_switches_per_host
        _evict_expired(window, now=now, cfg=cfg)
        return max(0, cfg.max_switches_per_host - len(window))

    def window_size(self, host: str) -> int:
        window = self._windows.get(host)
        return 0 if window is None else len(window)

    def _window_for(self, host: str) -> deque[float]:
        window = self._windows.get(host)
        if window is None:
            if len(self._windows) >= self._capacity:
                self._windows.popitem(last=False)
            window = deque()
            self._windows[host] = window
        else:
            self._windows.move_to_end(host)
        return window


def _evict_expired(window: deque[float], *, now: float, cfg: RateLimitConfig) -> None:
    cutoff = now - cfg.window_seconds
    while window and window[0] < cutoff:
        window.popleft()
