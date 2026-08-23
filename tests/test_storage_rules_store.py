"""``rules.db`` 的读写：乐观锁、整表替换的原子性、排序的确定性。

对应设计：docs/design/DD_STORAGE.md §4.8。验收点 M5-11、M5-13 ~ M5-16。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from r_proxy.storage.rules_store import RulesConflict, RulesSnapshot, RulesStore
from r_proxy.storage.schema import StorageError


def store_at(tmp_path: Path) -> RulesStore:
    return RulesStore(tmp_path / "rules.db")


def seeded(tmp_path: Path, *pairs: tuple[str, str]) -> RulesStore:
    store = store_at(tmp_path)
    store.ensure_schema()
    store.replace(list(pairs), expected_revision=0, now_unix=0)
    return store


class TestRead:
    def test_a_missing_database_reads_as_empty(self, tmp_path: Path) -> None:
        """库不存在与库是空的，对客户端是同一件事：还没有人写过。"""
        assert store_at(tmp_path).read() == RulesSnapshot(revision=0, rows=())

    def test_reading_does_not_create_the_database(self, tmp_path: Path) -> None:
        """建库是写者的职责。读路径建库会让 `enabled = false` 的救场场景失效。"""
        store_at(tmp_path).read()
        assert not (tmp_path / "rules.db").exists()

    def test_an_empty_database_starts_at_revision_zero(self, tmp_path: Path) -> None:
        store = store_at(tmp_path)
        store.ensure_schema()
        assert store.read() == RulesSnapshot(revision=0, rows=())

    def test_ensure_schema_is_idempotent(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"))
        store.ensure_schema()
        assert [c for _, c, _ in store.read().rows] == ["a.test"]

    def test_read_rules_implements_the_loader_protocol(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"))
        assert store.read_rules() == ((0, "a.test", "direct"),)

    def test_an_unreadable_database_names_the_file(self, tmp_path: Path) -> None:
        """启动失败时用户手上只有这一行文本，得知道是哪个库坏了。"""
        (tmp_path / "rules.db").write_bytes(b"not a database")
        with pytest.raises(StorageError, match="rules.db"):
            store_at(tmp_path).read()


class TestReplace:
    def test_m5_13_positions_are_assigned_from_the_array_index(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"), ("b.test", "proxy"), ("c.test", "direct"))
        assert [p for p, _, _ in store.read().rows] == [0, 1, 2]

    def test_m5_13_the_revision_increments(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"))
        assert store.read().revision == 1
        assert store.replace([], expected_revision=1, now_unix=0) == 2

    def test_m5_15_saving_identical_content_still_increments(self, tmp_path: Path) -> None:
        """revision 是并发凭据而非内容指纹：不递增会让「同时改回原样」这类
        冲突检测不出来。"""
        store = seeded(tmp_path, ("a.test", "direct"))
        store.replace([("a.test", "direct")], expected_revision=1, now_unix=0)
        assert store.read().revision == 2

    def test_replacing_removes_the_rows_that_are_gone(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"), ("b.test", "direct"))
        store.replace([("b.test", "direct")], expected_revision=1, now_unix=0)
        assert [c for _, c, _ in store.read().rows] == ["b.test"]

    def test_replace_creates_the_database_when_needed(self, tmp_path: Path) -> None:
        store_at(tmp_path).replace([("a.test", "direct")], expected_revision=0, now_unix=0)
        assert (tmp_path / "rules.db").exists()


class TestOptimisticLock:
    def test_m5_11_a_stale_revision_is_refused(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"))
        with pytest.raises(RulesConflict) as caught:
            store.replace([("b.test", "direct")], expected_revision=0, now_unix=0)
        assert (caught.value.expected, caught.value.actual) == (0, 1)

    def test_m5_11_a_refused_write_changes_nothing(self, tmp_path: Path) -> None:
        store = seeded(tmp_path, ("a.test", "direct"))
        before = store.read()
        with pytest.raises(RulesConflict):
            store.replace([("b.test", "direct")], expected_revision=99, now_unix=0)
        assert store.read() == before

    def test_a_revision_from_the_future_is_refused_too(self, tmp_path: Path) -> None:
        """只判「不相等」，不判「小于」：手工改库留下的更大的值同样是不一致。"""
        store = seeded(tmp_path, ("a.test", "direct"))
        with pytest.raises(RulesConflict):
            store.replace([], expected_revision=7, now_unix=0)


class TestAtomicity:
    def test_m5_14_a_failure_midway_rolls_back_to_the_complete_old_set(
        self, tmp_path: Path
    ) -> None:
        """删旧行与插新行必须同生共死。

        中途失败若留下「旧的删了、新的没进去」，代理会在下一次热重载时突然
        变成无规则状态——一次静默的、全局的路由变更。
        """
        store = seeded(tmp_path, ("a.test", "direct"), ("b.test", "proxy"))
        before = store.read()

        # STRICT 表拒绝 None：删旧行已经执行过，插新行在第二条上炸掉。
        rules = [("c.test", "direct"), ("d.test", None)]
        with pytest.raises(StorageError):
            store.replace(rules, expected_revision=1, now_unix=0)  # type: ignore[arg-type]

        assert store.read() == before

    def test_m5_16_duplicate_positions_sort_deterministically(self, tmp_path: Path) -> None:
        """手工改库可能留下重复的 position，`ORDER BY position, id` 兜住它。"""
        store = seeded(tmp_path, ("a.test", "direct"))
        conn = sqlite3.connect(store.path)
        try:
            conn.execute(
                "INSERT INTO rule (position, condition, upstream, updated_at)"
                " VALUES (0, 'b.test', 'direct', 0)"
            )
            conn.commit()
        finally:
            conn.close()
        assert [c for _, c, _ in store.read().rows] == ["a.test", "b.test"]
