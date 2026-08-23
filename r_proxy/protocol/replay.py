"""请求字节的可重放缓冲。

对应设计：docs/design/DD_SWITCHING.md §7。

一旦离开「可重放」状态就不可逆，没有回去的路径：字节已经流式转发到某个
出口，或客户端已经看到部分响应，两者都无法撤回。
"""

from __future__ import annotations

from collections.abc import Callable


class ReplayBuffer:
    """超出上限后转为流式并永久标记不可重放。"""

    __slots__ = ("_chunks", "_limit", "_replayable", "_size")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._chunks: list[bytes] = []
        self._size = 0
        # limit == 0 表示禁用缓存：请求发出后一律不可切换。
        self._replayable = limit > 0

    @property
    def replayable(self) -> bool:
        return self._replayable

    @property
    def size(self) -> int:
        return self._size

    def append(self, data: bytes) -> None:
        if not self._replayable:
            return
        if self._size + len(data) > self._limit:
            self.give_up()
            return
        self._chunks.append(data)
        self._size += len(data)

    def give_up(self) -> None:
        """主动放弃重放能力并立即释放内存。

        既然已确定不可重放，留着那 64KB 毫无用处；而在 1000 个并发连接下，
        不清空意味着最坏 64MB 的无效驻留。
        """
        self._replayable = False
        self._chunks.clear()
        self._size = 0

    def replay_into(self, write: Callable[[bytes], None]) -> None:
        """把缓存字节写入新连接。可重复调用——候选链可能有多个出口。"""
        for chunk in self._chunks:
            write(chunk)
