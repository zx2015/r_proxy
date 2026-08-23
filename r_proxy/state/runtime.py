"""运行时状态聚合：路由决策读的唯一权威。

对应设计：docs/design/DD_ROUTING.md §3.2。

IPv6 出口能力以布尔值注入而非持有 :mod:`r_proxy.egress.capability` 的对象：
探测要开套接字，而状态层禁止 I/O，也不允许依赖 egress 层。
"""

from __future__ import annotations

from typing import Protocol

from r_proxy.config.model import ConfigSnapshot
from r_proxy.state.health import HealthTable
from r_proxy.state.memory import RouteMemory
from r_proxy.state.sticky import StickyCache


class CursorTable:
    """每个优先级组一个游标。事件循环内的读改写无 ``await``，天然原子。"""

    __slots__ = ("_cursors",)

    def __init__(self) -> None:
        self._cursors: dict[int, int] = {}

    def next(self, priority: int) -> int:
        """取当前值并推进。**单调递增不取模**——取模在使用点做，
        这样组成员数量因热重载变化时，旧游标不会越界。"""
        value = self._cursors.get(priority, 0)
        self._cursors[priority] = value + 1
        return value

    def reset_all(self) -> None:
        self._cursors.clear()


class RuntimeStateView(Protocol):
    """``Router`` 看到的只读视图：拿不到任何写方法。

    状态变更由执行层在得到结果后执行，两者职责不混淆。
    """

    @property
    def health(self) -> HealthTable: ...

    @property
    def memory(self) -> RouteMemory: ...

    @property
    def sticky(self) -> StickyCache: ...

    @property
    def cursors(self) -> CursorTable: ...

    @property
    def has_ipv6_egress(self) -> bool: ...


class RuntimeState:
    __slots__ = ("_has_ipv6_egress", "cursors", "health", "memory", "sticky")

    def __init__(self, health: HealthTable, memory: RouteMemory, sticky: StickyCache) -> None:
        self.health = health
        self.memory = memory
        self.sticky = sticky
        self.cursors = CursorTable()
        self._has_ipv6_egress = False

    @classmethod
    def from_snapshot(cls, snapshot: ConfigSnapshot) -> RuntimeState:
        return cls(
            health=HealthTable(snapshot.routing.circuit_breaker),
            memory=RouteMemory(
                capacity=snapshot.limits.route_block_cache_size,
                ttl=float(snapshot.routing.route_block_ttl),
            ),
            sticky=StickyCache(capacity=snapshot.limits.sticky_cache_size),
        )

    @property
    def has_ipv6_egress(self) -> bool:
        return self._has_ipv6_egress

    def set_ipv6_egress(self, value: bool) -> None:
        self._has_ipv6_egress = value

    def on_reload(self, snapshot: ConfigSnapshot) -> None:
        """按新快照调整状态，但保留仍在配置中的出口的观测结果。

        不整体重建：出口的熔断状态与负面记忆是花时间学来的，
        改一行无关配置就把它们清空会让代理重新踩一遍所有坑。
        IPv6 能力保持原值，由调用方重探后覆盖。
        """
        names = set(snapshot.by_name)
        self.health.forget_except(names)
        self.health.reconfigure(snapshot.routing.circuit_breaker)
        self.memory.forget_upstreams_except(names)
        self.memory.reconfigure(
            capacity=snapshot.limits.route_block_cache_size,
            ttl=float(snapshot.routing.route_block_ttl),
        )
        self.sticky.forget_upstreams_except(names)
        self.sticky.reconfigure(capacity=snapshot.limits.sticky_cache_size)
        self.cursors.reset_all()
