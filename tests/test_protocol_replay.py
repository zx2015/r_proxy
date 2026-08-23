"""protocol/replay.py 的字节重放缓冲测试。

对应设计：docs/design/DD_SWITCHING.md §7
"""

from __future__ import annotations

from r_proxy.protocol.replay import ReplayBuffer


class TestBuffering:
    def test_starts_replayable(self) -> None:
        assert ReplayBuffer(limit=1024).replayable is True

    def test_accumulates_within_the_limit(self) -> None:
        buffer = ReplayBuffer(limit=1024)
        buffer.append(b"a" * 100)
        buffer.append(b"b" * 100)
        assert buffer.size == 200
        assert buffer.replayable is True

    def test_replays_bytes_in_order(self) -> None:
        buffer = ReplayBuffer(limit=1024)
        buffer.append(b"hello ")
        buffer.append(b"world")
        written: list[bytes] = []
        buffer.replay_into(written.append)
        assert b"".join(written) == b"hello world"

    def test_can_be_replayed_more_than_once(self) -> None:
        """候选链可能有多个出口，同一份字节要能重放多次。"""
        buffer = ReplayBuffer(limit=1024)
        buffer.append(b"clienthello")
        for _ in range(3):
            written: list[bytes] = []
            buffer.replay_into(written.append)
            assert b"".join(written) == b"clienthello"

    def test_exactly_at_the_limit_stays_replayable(self) -> None:
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"x" * 100)
        assert buffer.replayable is True
        assert buffer.size == 100

    def test_empty_append_does_not_change_anything(self) -> None:
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"")
        assert (buffer.size, buffer.replayable) == (0, True)


class TestOverflow:
    def test_exceeding_the_limit_gives_up_replay(self) -> None:
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"x" * 101)
        assert buffer.replayable is False

    def test_overflow_releases_the_buffered_bytes_immediately(self) -> None:
        """RL-06：已确定不可重放，留着那 64KB 毫无用处。

        1000 个并发连接下不清空意味着最坏 64MB 的无效驻留。
        """
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"x" * 80)
        buffer.append(b"y" * 80)
        assert buffer.size == 0
        # 直接看私有槽：size 归零可以靠一行赋值伪造，真正要断言的是字节被释放了。
        assert buffer._chunks == []  # noqa: SLF001

    def test_replay_after_overflow_writes_nothing(self) -> None:
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"x" * 200)
        written: list[bytes] = []
        buffer.replay_into(written.append)
        assert written == []

    def test_appends_after_overflow_are_dropped_without_accumulating(self) -> None:
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"x" * 200)
        for _ in range(1000):
            buffer.append(b"y" * 1000)
        assert buffer.size == 0

    def test_overflow_is_irreversible(self) -> None:
        """状态机没有回到 Buffering 的路径。"""
        buffer = ReplayBuffer(limit=100)
        buffer.append(b"x" * 200)
        buffer.append(b"tiny")
        assert buffer.replayable is False

    def test_cumulative_overflow_counts_all_appends(self) -> None:
        buffer = ReplayBuffer(limit=100)
        for _ in range(3):
            buffer.append(b"x" * 40)
        assert buffer.replayable is False


class TestDisabled:
    def test_zero_limit_is_never_replayable(self) -> None:
        """switch_buffer_bytes: 0 表示禁用缓存。"""
        assert ReplayBuffer(limit=0).replayable is False

    def test_zero_limit_buffers_nothing(self) -> None:
        buffer = ReplayBuffer(limit=0)
        buffer.append(b"data")
        assert buffer.size == 0


class TestGiveUp:
    def test_give_up_marks_unreplayable(self) -> None:
        buffer = ReplayBuffer(limit=1024)
        buffer.append(b"data")
        buffer.give_up()
        assert buffer.replayable is False

    def test_give_up_releases_the_bytes(self) -> None:
        buffer = ReplayBuffer(limit=1024)
        buffer.append(b"data")
        buffer.give_up()
        assert buffer.size == 0

    def test_give_up_is_idempotent(self) -> None:
        buffer = ReplayBuffer(limit=1024)
        buffer.give_up()
        buffer.give_up()
        assert buffer.replayable is False


class TestMemoryBound:
    def test_never_holds_more_than_the_limit(self) -> None:
        """最坏内存 = max_client_connections × switch_buffer_bytes。"""
        buffer = ReplayBuffer(limit=65536)
        for _ in range(100):
            buffer.append(b"x" * 1024)
        assert buffer.size <= 65536
