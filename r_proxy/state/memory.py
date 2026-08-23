"""路由级负面记忆：``(host, upstream)`` → 暂时别再走这条路。

对应设计：docs/design/DD_ROUTING.md §6。

只在内存中，不落盘。它记的是「经这个出口到不了这个目标」，与出口自身的
健康状态（:mod:`r_proxy.state.health`）是两件事——正是这个区分让 ``direct``
不会因为用户访问一批被墙站点而整体失效。
"""

from __future__ import annotations

import dataclasses
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(slots=True)
class RouteBlock:
    host: str
    upstream: str
    blocked_until: float
    fail_count: int
    last_reason: str


class RouteMemory:
    """容量受 ``limits.route_block_cache_size`` 限制，超限按 LRU 淘汰。"""

    __slots__ = ("_blocks", "_capacity", "_ttl")

    def __init__(self, capacity: int, ttl: float) -> None:
        self._capacity = capacity
        self._ttl = ttl
        self._blocks: OrderedDict[tuple[str, str], RouteBlock] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._blocks)

    @property
    def ttl(self) -> float:
        """当前生效的记忆时长。日志里要能说清「多久之后会再试一次」。"""
        return self._ttl

    def is_blocked(self, host: str, upstream: str, *, now: float) -> bool:
        key = (host, upstream)
        block = self._blocks.get(key)
        if block is None:
            return False
        if now >= block.blocked_until:
            # 惰性过期：定时扫全表要遍历，摊到查询上则只清理被访问到的键。
            del self._blocks[key]
            return False
        self._blocks.move_to_end(key)
        return True

    def block(self, host: str, upstream: str, *, now: float, reason: str) -> None:
        if self._capacity <= 0:
            return
        key = (host, upstream)
        block = self._blocks.get(key)
        if block is None:
            block = RouteBlock(host, upstream, 0.0, 0, reason)
            if len(self._blocks) >= self._capacity:
                self._blocks.popitem(last=False)
        block.blocked_until = now + self._ttl
        block.fail_count += 1
        block.last_reason = reason
        self._blocks[key] = block
        self._blocks.move_to_end(key)

    def clear(self, host: str, upstream: str) -> bool:
        """成功一次即完全清除，包括 ``fail_count``。返回是否真的清掉了记录。

        不做递减：网络恢复是二值的，一次成功就说明这条路通了，
        没必要让它经历多次成功才摘掉标记。

        返回值让调用方只在真有变化时才入队落盘——绝大多数成功请求的
        ``(host, upstream)`` 本来就没有记忆，无条件发 DELETE 会让写入量与
        请求量同数量级。
        """
        return self._blocks.pop((host, upstream), None) is not None

    def restore(self, block: RouteBlock) -> None:
        """启动回填。调用方只传未过期的记录（``blocked_until`` 已转 monotonic）。"""
        if self._capacity <= 0:
            return
        self._blocks[block.host, block.upstream] = block
        self._blocks.move_to_end((block.host, block.upstream))
        while len(self._blocks) > self._capacity:
            self._blocks.popitem(last=False)

    def entry(self, host: str, upstream: str) -> RouteBlock | None:
        block = self._blocks.get((host, upstream))
        return dataclasses.replace(block) if block is not None else None

    def entries(self, *, now: float) -> list[RouteBlock]:
        return [dataclasses.replace(b) for b in self._blocks.values() if now < b.blocked_until]

    def forget_upstreams_except(self, names: set[str]) -> None:
        """热重载后清掉已删除出口的记忆。"""
        for key in list(self._blocks):
            if key[1] not in names:
                del self._blocks[key]

    def reconfigure(self, *, capacity: int, ttl: float) -> None:
        """热重载时调整容量与 TTL，已有记忆保留。

        新 TTL 只作用于后续的 :meth:`block`：已算出的 ``blocked_until``
        不回溯修改，否则缩短 TTL 会让一批记忆同时失效、造成重试风暴。
        """
        self._capacity = capacity
        self._ttl = ttl
        while len(self._blocks) > capacity:
            self._blocks.popitem(last=False)
