"""schema、批次合并、写者线程、清理。

对应设计：docs/design/DD_STORAGE.md §3–§6。

这里用真实的 SQLite 文件而非 mock：被测的东西恰恰是 SQL 语义（``STRICT``
类型检查、``CHECK`` 约束、``ON CONFLICT`` 的自增与覆盖、事务隔离级别），
mock 掉数据库就等于什么都没测。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from r_proxy.config.model import DatabaseConfig
from r_proxy.storage.queue import (
    OpKind,
    WriteOp,
    WriteQueue,
    health_counters,
    route_block_delete,
    route_block_upsert,
    sticky_delete,
    sticky_hit,
    sticky_manual_upsert,
    sticky_upsert,
)
from r_proxy.storage.retention import Retention
from r_proxy.storage.schema import (
    SCHEMA_VERSION,
    Database,
    StorageError,
    migrate,
    open_write,
    read_version,
)
from r_proxy.storage.writer import WriterThread, merge


def db_config(tmp_path: Path, **kwargs: object) -> DatabaseConfig:
    defaults: dict[str, object] = {
        "state_path": tmp_path / "state.db",
        "logs_path": tmp_path / "logs.db",
        "rules_path": tmp_path / "rules.db",
        "flush_interval_ms": 10,
        "flush_batch_size": 500,
    }
    defaults.update(kwargs)
    return DatabaseConfig(**defaults)  # type: ignore[arg-type]


def request_log(request_id: str = "rid", host: str = "example.com") -> WriteOp:
    return WriteOp(
        OpKind.REQUEST_LOG,
        (
            request_id,
            host,
            f"http://{host}/",
            "GET",
            "a",
            10,
            0,
            "priority",
            None,
            200,
            None,
            None,
            None,
            5,
            0,
            0,
            int(time.time()),
        ),
    )


class RunningWriter:
    """启动写者线程并保证测试结束时把它停掉。"""

    def __init__(self, tmp_path: Path, **kwargs: object) -> None:
        self.cfg = db_config(tmp_path, **kwargs)
        self.queue = WriteQueue(maxsize=1000)
        self.thread = WriterThread(self.queue, self.cfg)

    def __enter__(self) -> RunningWriter:
        self.thread.start_and_wait()
        return self

    def __exit__(self, *exc: object) -> None:
        self.thread.stop()

    def flush(self, timeout: float = 5.0) -> None:
        assert self.thread.wait_until_drained(timeout=timeout), "写者线程未在超时内落盘"

    def state_rows(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        return _query(self.cfg.state_path, sql, params)

    def log_rows(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        return _query(self.cfg.logs_path, sql, params)


def _query(path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


class TestSchema:
    def test_fresh_database_is_at_the_current_version(self, tmp_path: Path) -> None:
        conn = open_write(tmp_path / "state.db", Database.STATE)
        try:
            assert read_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        open_write(path, Database.STATE).close()
        conn = open_write(path, Database.STATE)
        try:
            assert migrate(conn, Database.STATE) == SCHEMA_VERSION
        finally:
            conn.close()

    def test_a_newer_database_is_refused(self, tmp_path: Path) -> None:
        """用旧版程序打开新版库可能因缺列而静默写坏数据，报错退出更安全。"""
        path = tmp_path / "state.db"
        conn = open_write(path, Database.STATE)
        try:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION + 5),),
            )
        finally:
            conn.close()
        with pytest.raises(StorageError):
            open_write(path, Database.STATE)

    def test_strict_tables_reject_a_string_in_an_integer_column(self, tmp_path: Path) -> None:
        conn = open_write(tmp_path / "state.db", Database.STATE)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO host_upstream"
                    " (host, upstream_name, source, last_success_at, updated_at)"
                    " VALUES ('a.com', 'a', 'auto', 'not-a-number', 1)"
                )
        finally:
            conn.close()

    def test_the_source_check_constraint_rejects_rule(self, tmp_path: Path) -> None:
        """规则命中不写粘性——把这条语义固化进 schema。"""
        conn = open_write(tmp_path / "state.db", Database.STATE)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO host_upstream (host, upstream_name, source, updated_at)"
                    " VALUES ('a.com', 'a', 'rule', 1)"
                )
        finally:
            conn.close()

    def test_wal_mode_is_enabled(self, tmp_path: Path) -> None:
        conn = open_write(tmp_path / "state.db", Database.STATE)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()

    def test_incremental_auto_vacuum_is_enabled_on_a_fresh_database(self, tmp_path: Path) -> None:
        conn = open_write(tmp_path / "logs.db", Database.LOGS)
        try:
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        finally:
            conn.close()

    def test_the_parent_directory_is_created(self, tmp_path: Path) -> None:
        conn = open_write(tmp_path / "nested" / "deeper" / "state.db", Database.STATE)
        conn.close()
        assert (tmp_path / "nested" / "deeper" / "state.db").is_file()

    def test_the_two_databases_hold_different_tables(self, tmp_path: Path) -> None:
        """拆库的收益是运维上的可丢弃性：logs.db 能直接删掉重建。"""
        state = open_write(tmp_path / "state.db", Database.STATE)
        logs = open_write(tmp_path / "logs.db", Database.LOGS)
        try:
            names = {
                db: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for db, c in ((Database.STATE, state), (Database.LOGS, logs))
            }
        finally:
            state.close()
            logs.close()
        assert "host_upstream" in names[Database.STATE]
        assert "host_upstream" not in names[Database.LOGS]
        assert "request_log" in names[Database.LOGS]
        assert "request_log" not in names[Database.STATE]


class TestMerge:
    def test_repeated_increments_become_one(self) -> None:
        ops = [sticky_hit(host="a.com", now_unix=1) for _ in range(5)]
        merged = merge(ops)
        assert len(merged) == 1
        assert merged[0].payload[0] == 5

    def test_increments_for_different_hosts_stay_separate(self) -> None:
        ops = [sticky_hit(host="a.com", now_unix=1), sticky_hit(host="b.com", now_unix=1)]
        assert len(merge(ops)) == 2

    def test_the_later_upsert_wins(self) -> None:
        ops = [
            sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200),
            sticky_upsert(host="a.com", upstream="b", url=None, now_unix=2, status=200),
        ]
        merged = merge(ops)
        assert len(merged) == 1
        assert merged[0].payload[1] == "b"

    def test_inserts_are_never_merged(self) -> None:
        assert len(merge([request_log(), request_log()])) == 2

    def test_delete_wipes_out_earlier_operations_on_the_same_row(self) -> None:
        ops = [
            sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200),
            sticky_hit(host="a.com", now_unix=1),
            sticky_delete(host="a.com"),
        ]
        merged = merge(ops)
        assert [op.kind for op in merged] == [OpKind.STICKY_DELETE]

    def test_a_write_after_a_delete_survives(self) -> None:
        """「upsert → delete → upsert」的最终状态是「存在」。压成
        「upsert → delete」会让粘性凭空消失。"""
        ops = [
            sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200),
            sticky_delete(host="a.com"),
            sticky_upsert(host="a.com", upstream="b", url=None, now_unix=2, status=200),
        ]
        merged = merge(ops)
        assert [op.kind for op in merged] == [OpKind.STICKY_DELETE, OpKind.STICKY_UPSERT]
        assert merged[1].payload[1] == "b"

    def test_a_delete_does_not_touch_another_row(self) -> None:
        ops = [
            sticky_hit(host="b.com", now_unix=1),
            sticky_delete(host="a.com"),
        ]
        assert len(merge(ops)) == 2

    def test_health_deltas_add_while_authoritative_fields_are_overwritten(self) -> None:
        """混淆这两类字段是最容易出的错：把覆盖写成自增，计数会翻倍。"""
        ops = [
            health_counters(
                upstream="a",
                success_delta=1,
                failure_delta=0,
                consecutive_failures=0,
                avg_latency_ms=10,
                circuit_state="closed",
                cooldown_until=0,
                auth_error=0,
                now_unix=1,
            ),
            health_counters(
                upstream="a",
                success_delta=2,
                failure_delta=3,
                consecutive_failures=3,
                avg_latency_ms=40,
                circuit_state="open",
                cooldown_until=99,
                auth_error=0,
                now_unix=2,
            ),
        ]
        merged = merge(ops)
        assert len(merged) == 1
        assert merged[0].payload[1:6] == (3, 3, 3, 40, "open")

    def test_route_block_upserts_are_not_summed(self) -> None:
        """fail_count 的自增在 SQL 侧（+1 per 语句），payload 里没有增量列。

        合并成一条会少记一次失败——这里保留最后一条，与设计一致：负面记忆
        的精确计数不影响路由，TTL 才影响。
        """
        ops = [
            route_block_upsert(
                host="a.com", upstream="a", reason="timeout", now_unix=1, blocked_until=601
            ),
            route_block_upsert(
                host="a.com", upstream="a", reason="reset", now_unix=2, blocked_until=602
            ),
        ]
        merged = merge(ops)
        assert len(merged) == 1
        assert merged[0].payload[2] == "reset"


class TestWriterThread:
    def test_sticky_upsert_reaches_the_database(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path) as w:
            w.queue.put(
                sticky_upsert(host="a.com", upstream="proxy-a", url=None, now_unix=7, status=200)
            )
            w.flush()
            rows = w.state_rows("SELECT upstream_name, source, hit_count FROM host_upstream")
        assert rows == [("proxy-a", "auto", 1)]

    def test_manual_bindings_are_never_overwritten(self, tmp_path: Path) -> None:
        """CC-06 / M3-05：RC-04 的数据库侧防线。"""
        with RunningWriter(tmp_path) as w:
            conn = sqlite3.connect(w.cfg.state_path)
            conn.execute(
                "INSERT INTO host_upstream (host, upstream_name, source, updated_at)"
                " VALUES ('a.com', 'manual-choice', 'manual', 1)"
            )
            conn.commit()
            conn.close()

            w.queue.put(
                sticky_upsert(
                    host="a.com", upstream="auto-choice", url=None, now_unix=9, status=200
                )
            )
            w.flush()
            rows = w.state_rows("SELECT upstream_name, source FROM host_upstream")
        assert rows == [("manual-choice", "manual")]

    def test_a_manual_binding_converts_an_automatic_row(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path) as w:
            w.queue.put(
                sticky_upsert(host="a.com", upstream="auto", url=None, now_unix=1, status=200)
            )
            w.queue.put(sticky_manual_upsert(host="a.com", upstream="pinned-a", now_unix=2))
            w.flush()
            rows = w.state_rows("SELECT upstream_name, source FROM host_upstream")
        assert rows == [("pinned-a", "manual")]

    def test_rebinding_overwrites_a_row_that_is_already_manual(self, tmp_path: Path) -> None:
        """手动绑定是唯一不受 ``source != 'manual'`` 护栏约束的写入。

        两次绑定必须分批落盘：同一批里它们会先被合并成一条，冲突路径根本不会
        执行，护栏有没有都测不出来。
        """
        with RunningWriter(tmp_path) as w:
            w.queue.put(sticky_manual_upsert(host="a.com", upstream="pinned-a", now_unix=2))
            w.flush()
            w.queue.put(sticky_manual_upsert(host="a.com", upstream="pinned-b", now_unix=3))
            w.flush()
            rows = w.state_rows("SELECT upstream_name, source FROM host_upstream")
        assert rows == [("pinned-b", "manual")]

    def test_a_manual_binding_keeps_the_accumulated_hits(self, tmp_path: Path) -> None:
        """改绑不代表发生过一次成功，也不该抹掉这个 host 的历史命中数。"""
        with RunningWriter(tmp_path) as w:
            for _ in range(3):
                w.queue.put(
                    sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200)
                )
                w.flush()
            w.queue.put(sticky_manual_upsert(host="a.com", upstream="b", now_unix=9))
            w.flush()
            rows = w.state_rows("SELECT hit_count, fail_count FROM host_upstream")
        assert rows == [(3, 0)]

    def test_counters_are_incremented_on_the_sql_side(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path) as w:
            w.queue.put(sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200))
            w.flush()
            for _ in range(4):
                w.queue.put(sticky_hit(host="a.com", now_unix=2))
                w.flush()
            rows = w.state_rows("SELECT hit_count FROM host_upstream")
        assert rows == [(5,)]

    def test_timestamps_never_go_backwards(self, tmp_path: Path) -> None:
        """CC-08：乱序落盘的批次不该让「最后成功时间」倒退。"""
        with RunningWriter(tmp_path) as w:
            w.queue.put(
                sticky_upsert(host="a.com", upstream="a", url=None, now_unix=100, status=200)
            )
            w.flush()
            w.queue.put(
                sticky_upsert(host="a.com", upstream="a", url=None, now_unix=50, status=200)
            )
            w.flush()
            rows = w.state_rows("SELECT last_success_at FROM host_upstream")
        assert rows == [(100,)]

    def test_concurrent_increments_lose_nothing(self, tmp_path: Path) -> None:
        """M3-04：4 个线程各 500 次自增，最终必须是 2000。

        这直接对应实测中 deferred 事务丢失 75% 更新的场景，是
        ``BEGIN IMMEDIATE`` + SQL 侧自增的回归防护。
        """
        with RunningWriter(tmp_path) as w:
            w.queue.put(sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200))
            w.flush()

            def hammer() -> None:
                for _ in range(500):
                    w.queue.put(sticky_hit(host="a.com", now_unix=2))

            threads = [threading.Thread(target=hammer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            deadline = time.monotonic() + 10
            while w.queue.size and time.monotonic() < deadline:
                time.sleep(0.01)
            w.flush()
            rows = w.state_rows("SELECT hit_count FROM host_upstream")
        # 1 来自 UPSERT 自身，2000 来自四个线程。
        assert rows == [(2001,)]

    def test_route_block_and_its_removal(self, tmp_path: Path) -> None:
        # 用真实的 Unix 时间戳：清理任务会删掉过期超过一天的负面记忆，
        # 拿 1 当时间戳的记录在它眼里已经过期五十多年了。
        now = int(time.time())
        with RunningWriter(tmp_path) as w:
            w.queue.put(
                route_block_upsert(
                    host="a.com",
                    upstream="a",
                    reason="timeout",
                    now_unix=now,
                    blocked_until=now + 600,
                )
            )
            w.flush()
            assert w.state_rows("SELECT fail_count FROM route_block") == [(1,)]

            w.queue.put(route_block_delete(host="a.com", upstream="a"))
            w.flush()
            assert w.state_rows("SELECT COUNT(*) FROM route_block") == [(0,)]

    def test_request_logs_are_appended(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path) as w:
            for i in range(3):
                w.queue.put(request_log(request_id=f"rid-{i}"))
            w.flush()
            rows = w.log_rows("SELECT request_id FROM request_log ORDER BY id")
        assert [r[0] for r in rows] == ["rid-0", "rid-1", "rid-2"]

    def test_state_and_logs_go_to_their_own_files(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path) as w:
            w.queue.put(sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200))
            w.queue.put(request_log())
            w.flush()
            assert w.state_rows("SELECT COUNT(*) FROM host_upstream") == [(1,)]
            assert w.log_rows("SELECT COUNT(*) FROM request_log") == [(1,)]

    def test_the_queue_is_drained_before_the_databases_close(self, tmp_path: Path) -> None:
        """M3-08：SIGTERM 时队列排空后才关库，不丢最后一批写入。"""
        writer = RunningWriter(tmp_path, flush_interval_ms=10_000)
        writer.thread.start_and_wait()
        writer.queue.put(
            sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200)
        )
        writer.thread.stop()
        assert writer.state_rows("SELECT COUNT(*) FROM host_upstream") == [(1,)]

    def test_a_broken_statement_does_not_kill_the_writer(self, tmp_path: Path) -> None:
        """M3-09 的同类：写入失败记 ERROR，代理继续服务。"""
        with RunningWriter(tmp_path) as w:
            w.queue.put(WriteOp(OpKind.STICKY_UPSERT, ("a.com",)))  # 参数个数不对
            w.flush()
            assert w.thread.metrics.write_errors >= 1

            w.queue.put(sticky_upsert(host="b.com", upstream="a", url=None, now_unix=1, status=200))
            w.flush()
            assert w.state_rows("SELECT host FROM host_upstream") == [("b.com",)]

    def test_a_failed_batch_does_not_take_the_other_database_down(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path) as w:
            w.queue.put(WriteOp(OpKind.STICKY_UPSERT, ("a.com",)))
            w.queue.put(request_log())
            w.flush()
            assert w.log_rows("SELECT COUNT(*) FROM request_log") == [(1,)]

    def test_opening_an_unwritable_path_is_reported_to_the_caller(self, tmp_path: Path) -> None:
        """建库失败必须在启动阶段暴露：带着死掉的写者线程跑，症状是
        「所有状态都记不住」，没有任何线索指向数据库。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        cfg = db_config(tmp_path, state_path=blocker / "state.db")
        thread = WriterThread(WriteQueue(maxsize=10), cfg)
        with pytest.raises(StorageError):
            thread.start_and_wait()
        thread.stop()

    def test_metrics_track_merging(self, tmp_path: Path) -> None:
        with RunningWriter(tmp_path, flush_interval_ms=50) as w:
            for _ in range(10):
                w.queue.put(sticky_hit(host="a.com", now_unix=1))
            w.flush()
            assert w.thread.metrics.rows_merged_away >= 1
            assert w.thread.metrics.last_flush_at > 0


class TestRetention:
    def open_both(self, tmp_path: Path) -> dict[Database, sqlite3.Connection]:
        return {
            Database.STATE: open_write(tmp_path / "state.db", Database.STATE),
            Database.LOGS: open_write(tmp_path / "logs.db", Database.LOGS),
        }

    def insert_logs(self, conn: sqlite3.Connection, ages_days: list[int]) -> None:
        now = int(time.time())
        conn.executemany(
            "INSERT INTO request_log"
            " (request_id, host, method, upstream_name, elapsed_ms, created_at)"
            " VALUES ('r', 'h', 'GET', 'a', 1, ?)",
            [(now - age * 86400,) for age in ages_days],
        )

    def test_logs_older_than_the_retention_window_are_deleted(self, tmp_path: Path) -> None:
        conns = self.open_both(tmp_path)
        try:
            self.insert_logs(conns[Database.LOGS], [0, 5, 31, 400])
            Retention(db_config(tmp_path, retention_days=30)).run(conns)
            assert conns[Database.LOGS].execute("SELECT COUNT(*) FROM request_log").fetchone() == (
                2,
            )
        finally:
            for c in conns.values():
                c.close()

    def test_only_the_newest_rows_are_kept(self, tmp_path: Path) -> None:
        conns = self.open_both(tmp_path)
        try:
            self.insert_logs(conns[Database.LOGS], [0] * 10)
            Retention(db_config(tmp_path, max_log_rows=4)).run(conns)
            rows = conns[Database.LOGS].execute("SELECT id FROM request_log ORDER BY id").fetchall()
            assert [r[0] for r in rows] == [7, 8, 9, 10]
        finally:
            for c in conns.values():
                c.close()

    def test_expired_route_blocks_are_kept_for_a_day(self, tmp_path: Path) -> None:
        """fail_count 的历史值对诊断有价值，内存侧的惰性过期已保证路由不受影响。"""
        conns = self.open_both(tmp_path)
        now = int(time.time())
        try:
            conns[Database.STATE].executemany(
                "INSERT INTO route_block"
                " (host, upstream_name, last_failure_at, blocked_until) VALUES (?, 'a', ?, ?)",
                [("fresh.com", now, now + 600), ("old.com", now, now - 90000)],
            )
            Retention(db_config(tmp_path)).run(conns)
            rows = conns[Database.STATE].execute("SELECT host FROM route_block").fetchall()
            assert [r[0] for r in rows] == ["fresh.com"]
        finally:
            for c in conns.values():
                c.close()

    def test_the_interval_gates_repeat_runs(self, tmp_path: Path) -> None:
        conns = self.open_both(tmp_path)
        try:
            retention = Retention(db_config(tmp_path), interval_seconds=3600)
            metrics = WriterThread(WriteQueue(maxsize=1), db_config(tmp_path)).metrics
            assert retention.maybe_run(conns, metrics) is True
            assert retention.maybe_run(conns, metrics) is False
            assert metrics.retention_runs == 1
        finally:
            for c in conns.values():
                c.close()
