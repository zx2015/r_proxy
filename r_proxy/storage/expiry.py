"""粘性过期策略：把 ``routing.sticky_ttl`` 换算成存储侧要用的 Unix 截止点。

对应设计：docs/design/DD_STORAGE.md §5.1、§6.3。

为什么单独一个对象而不是到处传秒数：

- 内存侧（:mod:`r_proxy.state.sticky`）用 monotonic 域判定，只需要裸的 ``ttl``
  秒数；存储侧（回填过滤、磁盘清理）用 Unix 域判定。两侧的时间基准不同，
  共用一个对象反而要把两套语义揉在一起。
- ``ttl <= 0`` 表示「禁用过期」，这个边界只在这里定义一次，避免 reader 与
  retention 各写一遍 ``if ttl > 0`` 而漏掉其中一处。

放 ``storage/`` 而非 ``state/``：它只服务于存储层的 SQL 过滤，``state/`` 禁止
I/O 也不该知道库表结构。数值仍源自配置，存储层不理解「粘性」的路由语义，
只把它当作一行「多久没更新就算旧」的时间条件。
"""

from __future__ import annotations

from dataclasses import dataclass

# 配置单位是秒，且默认 30 天。
DEFAULT_TTL_SECONDS = 2_592_000


@dataclass(frozen=True, slots=True)
class StickyExpiryPolicy:
    """``auto`` 粘性行的存活判据。``ttl_seconds <= 0`` 表示永不因空闲失效。"""

    ttl_seconds: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def cutoff(self, *, now_unix: int) -> int:
        """早于该 Unix 时间戳的 ``auto`` 行即视为过期。

        禁用过期时返回 ``0``：SQL 里的 ``updated_at >= 0`` 恒真，等价于「不过滤」。
        ``updated_at`` 是非负整数，因此 ``0`` 不会误伤任何真实行。
        """
        if not self.enabled:
            return 0
        return now_unix - int(self.ttl_seconds)
