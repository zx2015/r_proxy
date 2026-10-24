"""``rules.db`` 的读写：唯一不经写者线程的库。

对应设计：docs/design/DD_STORAGE.md §2.1、§4.8。

这不是对「唯一写者」的破例——约束的实质是「每个库只有一个写者」，``rules.db``
的唯一写者是 Web 的配置写入器。写者线程的批量合并循环做不了规则保存需要的四
件事：校验通过才写、事务内整体替换、同步等落盘后才能回响应、乐观锁冲突检测。

写连接**每次操作现开现关**而不是长期持有：``sqlite3`` 连接绑定创建它的线程，
而调用方走 ``asyncio.to_thread``（线程池会复用不同线程）。规则写入的频率是
「用户点保存」，开连接的成本可以忽略。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from r_proxy.rules.condition import normalize_condition
from r_proxy.storage.reader import connect_readonly
from r_proxy.storage.schema import Database, StorageError, open_write

REVISION_KEY = "revision"

# 空库与「库还不存在」共用这个版本号：两者对客户端是同一件事——还没有人写过。
INITIAL_REVISION = 0

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def canonical_rules(rules: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """规范化规则条件并按与 SQLite NOCASE 一致的字母序生成存储快照。"""
    return sorted(
        ((normalize_condition(condition), upstream) for condition, upstream in rules),
        key=lambda row: (row[0].translate(_ASCII_LOWER), row[0]),
    )


# (position, condition, upstream)
RuleRow = tuple[int, str, str]


class RulesConflict(Exception):
    """库内 ``revision`` 已经不是客户端读到的那一个。"""

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__("规则已被其他会话修改")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class RulesSnapshot:
    revision: int
    rows: tuple[RuleRow, ...]


class RulesStore:
    """``rules.db`` 的门面。全部方法同步执行，调用方负责放进 ``to_thread``。"""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def ensure_schema(self) -> None:
        """建库、建表、写入 ``revision = 0``。已存在时只做迁移。"""
        open_write(self._path, Database.RULES).close()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read(self) -> RulesSnapshot:
        """规则与当前 ``revision``。库不存在时回空集，**不建库**。

        不建库是因为读取路径会在 ``rules.enabled = false`` 之外的许多地方被
        调用（启动加载、Web 展示），而建库是写者的职责。
        """
        if not self._path.exists():
            return RulesSnapshot(revision=INITIAL_REVISION, rows=())
        conn = connect_readonly(self._path)
        try:
            return RulesSnapshot(revision=_read_revision(conn), rows=_read_rows(conn))
        except sqlite3.Error as exc:
            raise StorageError(f"读取规则库失败 {self._path.name}: {exc}") from exc
        finally:
            conn.close()

    def read_rules(self) -> tuple[RuleRow, ...]:
        """只要规则行。实现 :class:`r_proxy.rules.loader.RulesSource` 协议。"""
        return self.read().rows

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def replace(
        self,
        rules: Sequence[tuple[str, str]],
        *,
        expected_revision: int,
        now_unix: int,
    ) -> int:
        """整表替换，返回新的 ``revision``。

        ``rules`` 是完整规则集合；调用方先完成交集校验。通过后规范化并按条件
        字母序存储，``position`` 仅表示该快照的位置，不参与出口选择。

        ``expected_revision`` 在事务内再比对一次——调用方持锁时读到的值到这里
        理论上不会变，但把检查放进同一个事务才是真正的原子性。
        """
        self.ensure_schema()
        conn = open_write(self._path, Database.RULES)
        try:
            return _replace_in_transaction(
                conn, canonical_rules(rules), expected_revision, now_unix
            )
        except sqlite3.Error as exc:
            raise StorageError(f"写入规则库失败 {self._path.name}: {exc}") from exc
        finally:
            conn.close()


def _replace_in_transaction(
    conn: sqlite3.Connection,
    rules: Sequence[tuple[str, str]],
    expected_revision: int,
    now_unix: int,
) -> int:
    # BEGIN IMMEDIATE 而非 deferred：deferred 事务在并发下丢更新（实测丢 75%）。
    # 这里只有一个写者，但「写事务一律显式取写锁」是本项目的通行约定。
    conn.execute("BEGIN IMMEDIATE")
    try:
        actual = _read_revision(conn)
        if actual != expected_revision:
            raise RulesConflict(expected=expected_revision, actual=actual)
        conn.execute("DELETE FROM rule")
        conn.executemany(
            "INSERT INTO rule (position, condition, upstream, updated_at) VALUES (?, ?, ?, ?)",
            [(i, condition, upstream, now_unix) for i, (condition, upstream) in enumerate(rules)],
        )
        # SQL 侧自增，不在 Python 侧读值再写回——与计数器约定同源（§4.3）。
        conn.execute(
            "UPDATE rule_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = ?",
            (REVISION_KEY,),
        )
        after = _read_revision(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return after


def _read_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM rule_meta WHERE key = ?", (REVISION_KEY,)).fetchone()
    return INITIAL_REVISION if row is None else int(row[0])


def _read_rows(conn: sqlite3.Connection) -> tuple[RuleRow, ...]:
    # 读取不改写历史规则；position 在返回值中只是本快照的字母序位置。
    rows = conn.execute(
        "SELECT condition, upstream FROM rule "
        "ORDER BY condition COLLATE NOCASE, condition COLLATE BINARY, id"
    ).fetchall()
    return tuple((position, str(row[0]), str(row[1])) for position, row in enumerate(rows))
