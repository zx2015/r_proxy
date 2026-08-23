"""粘性映射：host → 上次成功的出口。

对应设计：docs/design/DD_ROUTING.md §7。

只在内存中做决策，落盘由存储层异步完成（RC-03 接受最终一致：内存立即生效，
落盘只影响重启后）。因此这里的方法返回「是否产生了需要落盘的变更」，让调用
方决定发 UPSERT 还是发自增语句。
"""

from __future__ import annotations

import dataclasses
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

StickySource = Literal["auto", "manual"]


@dataclass(slots=True)
class StickyEntry:
    host: str
    upstream: str
    source: StickySource
    fail_count: int = 0
    last_used_at: float = 0.0
    hit_count: int = 0


class StickyCache:
    """容量受 ``limits.sticky_cache_size`` 限制（默认 10000），超限按 LRU 淘汰。"""

    __slots__ = ("_capacity", "_entries")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[str, StickyEntry] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._entries)

    def get(self, host: str) -> StickyEntry | None:
        """返回**活引用**而非副本：这是每请求都走的热路径。

        调用方只读。需要快照的场合用 :meth:`entries`。
        """
        entry = self._entries.get(host)
        if entry is not None:
            self._entries.move_to_end(host)
        return entry

    def record_success(self, host: str, upstream: str, *, now: float) -> bool:
        """返回 ``True`` 表示绑定关系变了、需要 UPSERT。

        纯计数变化返回 ``False``：``hit_count`` 与 ``last_used_at`` 变化频繁但
        不改变路由，每次都发 UPSERT 会让写入量与请求量同数量级。
        """
        entry = self._entries.get(host)
        if entry is not None and entry.source == "manual":
            # 不覆盖手动绑定。数据库侧还有 WHERE source != 'manual' 兜底，
            # 两层都要有：只有内存保护时，重启回填后的窗口期可能丢绑定；
            # 只有 SQL 保护时，内存已经改错，落盘被拒反而造成两边不一致。
            self._touch(entry, now=now)
            return False
        if entry is not None and entry.upstream == upstream:
            entry.fail_count = 0
            self._touch(entry, now=now)
            self._entries.move_to_end(host)
            return False
        self._put(
            StickyEntry(host=host, upstream=upstream, source="auto", last_used_at=now, hit_count=1)
        )
        return True

    def record_failure(self, host: str, *, threshold: int) -> bool:
        """返回 ``True`` 表示粘性被清除。

        ``manual`` 的 ``fail_count`` 照常累加——它是给用户看的健康指标，
        只是不触发清除（见 DD_ROUTING §7.2b）。
        """
        entry = self._entries.get(host)
        if entry is None:
            return False
        entry.fail_count += 1
        if entry.source == "manual":
            return False
        if entry.fail_count >= threshold:
            del self._entries[host]
            return True
        return False

    def bind_manual(self, host: str, upstream: str, *, now: float) -> None:
        """管理员手动绑定。覆盖任何已有条目，包括另一条 ``manual``。

        失败计数清零：改绑的意图就是「换一条路重新开始」，留着上一个出口攒下的
        失败次数只会让界面显示一个与当前绑定无关的数字。
        """
        self._put(StickyEntry(host=host, upstream=upstream, source="manual", last_used_at=now))

    def clear(self, host: str) -> bool:
        return self._entries.pop(host, None) is not None

    def entries(self) -> list[StickyEntry]:
        """副本列表，供 Web 与测试查看，改它不影响权威状态。"""
        return [dataclasses.replace(e) for e in self._entries.values()]

    def restore(self, entry: StickyEntry) -> None:
        """启动回填。按 ``updated_at`` 降序调用，最近用过的最有价值。"""
        self._put(entry)

    def forget_upstreams_except(self, names: set[str]) -> None:
        """热重载后清掉指向已删除出口的绑定。

        包括 ``manual``：出口都不存在了，保留用户的绑定意图也无从执行。
        """
        for host, entry in list(self._entries.items()):
            if entry.upstream not in names:
                del self._entries[host]

    def reconfigure(self, *, capacity: int) -> None:
        self._capacity = capacity
        self._evict_to_capacity()

    def _put(self, entry: StickyEntry) -> None:
        if self._capacity <= 0:
            return
        self._entries[entry.host] = entry
        self._entries.move_to_end(entry.host)
        self._evict_to_capacity()

    def _evict_to_capacity(self) -> None:
        """超容时优先淘汰 ``auto``：手动绑定是用户明确表达的意图，被一批自动学到
        的映射挤掉后下一次请求会重新按优先级选路，而库里的 ``manual`` 行还在，
        重启后又变回来——这种「时好时坏」比直接丢掉更难排查。

        全是 ``manual`` 时仍然淘汰最旧的那条：容量上限是硬约束，不能因为条目
        类型而失效。前向扫描的代价与 ``manual`` 条目数同阶，而它在实际配置里
        是个位数。
        """
        while len(self._entries) > self._capacity:
            victim = self._oldest_auto()
            if victim is None:
                self._entries.popitem(last=False)
            else:
                del self._entries[victim]

    def _oldest_auto(self) -> str | None:
        """最久未用的 ``auto`` 条目。``OrderedDict`` 前端即最旧端。"""
        for host, entry in self._entries.items():
            if entry.source != "manual":
                return host
        return None

    @staticmethod
    def _touch(entry: StickyEntry, *, now: float) -> None:
        entry.hit_count += 1
        # 单调保护：乱序完成的请求（先发起的后完成）不该让时间倒退。
        entry.last_used_at = max(entry.last_used_at, now)
