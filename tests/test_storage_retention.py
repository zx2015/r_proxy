"""Retention 定时清理测试：日志保留、负面记忆清理、粘性映射老化。

对应设计：docs/design/DD_STORAGE.md §6。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from r_proxy.config.model import DatabaseConfig
from r_proxy.storage.expiry import StickyExpiryPolicy
from r_proxy.storage.retention import Retention
from r_proxy.storage.schema import Database, open_write


def make_configs(tmp_path: Path) -> DatabaseConfig:
    return DatabaseConfig(
        state_path=tmp_path / "state.db",
        logs_path=tmp_path / "logs.db",
        rules_path=tmp_path / "rules.db",
        retention_days=1,
    )


def open_connections(cfg: DatabaseConfig) -> dict[Database, sqlite3.Connection]:
    return {
        Database.STATE: open_write(cfg.state_path, Database.STATE),
        Database.LOGS: open_write(cfg.logs_path, Database.LOGS),
    }


def insert_sticky(
    conn: sqlite3.Connection,
    host: str,
    upstream: str,
    *,
    source: str = "auto",
    updated_at: int,
) -> None:
    conn.execute(
        """
        INSERT INTO host_upstream
            (host, upstream_name, source, last_success_at, fail_count, hit_count, updated_at)
        VALUES (?, ?, ?, ?, 0, 1, ?)
        """,
        (host, upstream, source, updated_at, updated_at),
    )
    conn.commit()


def count_sticky(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM host_upstream").fetchone()[0]


class TestRetentionStickyClean:
    def test_expired_auto_rows_are_deleted(self, tmp_path: Path) -> None:
        cfg = make_configs(tmp_path)
        conns = open_connections(cfg)
        try:
            now = int(time.time())
            insert_sticky(conns[Database.STATE], "old1.com", "u", updated_at=now - 1000)
            insert_sticky(conns[Database.STATE], "old2.com", "u", updated_at=now - 2000)
            insert_sticky(conns[Database.STATE], "fresh.com", "u", updated_at=now - 10)

            ret = Retention(
                cfg,
                expiry=StickyExpiryPolicy(ttl_seconds=500.0),
                first_run_immediately=True,
            )
            deleted = ret.run(conns)
            assert deleted == 2
            assert count_sticky(conns[Database.STATE]) == 1
        finally:
            for c in conns.values():
                c.close()

    def test_manual_rows_are_never_deleted_by_retention(self, tmp_path: Path) -> None:
        """manual 是用户声明，无论多久没动都不由清理任务自动丢弃。"""
        cfg = make_configs(tmp_path)
        conns = open_connections(cfg)
        try:
            now = int(time.time())
            insert_sticky(
                conns[Database.STATE],
                "pinned.com",
                "u",
                source="manual",
                updated_at=now - 10_000_000,
            )
            ret = Retention(
                cfg,
                expiry=StickyExpiryPolicy(ttl_seconds=10.0),
                first_run_immediately=True,
            )
            deleted = ret.run(conns)
            assert deleted == 0
            assert count_sticky(conns[Database.STATE]) == 1
        finally:
            for c in conns.values():
                c.close()

    def test_zero_ttl_disables_sticky_cleaning(self, tmp_path: Path) -> None:
        cfg = make_configs(tmp_path)
        conns = open_connections(cfg)
        try:
            now = int(time.time())
            insert_sticky(conns[Database.STATE], "old.com", "u", updated_at=now - 10_000_000)
            ret = Retention(
                cfg,
                expiry=StickyExpiryPolicy(ttl_seconds=0.0),
                first_run_immediately=True,
            )
            deleted = ret.run(conns)
            assert deleted == 0
            assert count_sticky(conns[Database.STATE]) == 1
        finally:
            for c in conns.values():
                c.close()
