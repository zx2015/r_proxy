"""启动回填与只读访问。

对应设计：docs/design/DD_STORAGE.md §5、§7。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from r_proxy.config.model import DatabaseConfig, LimitsConfig
from r_proxy.storage.expiry import StickyExpiryPolicy
from r_proxy.storage.reader import InitialState, ReadOnlyPool, load_initial_state, to_monotonic
from r_proxy.storage.schema import Database, open_write


def db_config(tmp_path: Path) -> DatabaseConfig:
    return DatabaseConfig(
        state_path=tmp_path / "state.db",
        logs_path=tmp_path / "logs.db",
        rules_path=tmp_path / "rules.db",
    )


def seed(tmp_path: Path, statements: list[tuple[str, tuple[object, ...]]]) -> DatabaseConfig:
    cfg = db_config(tmp_path)
    conn = open_write(cfg.state_path, Database.STATE)
    try:
        for sql, params in statements:
            conn.execute(sql, params)
    finally:
        conn.close()
    return cfg


def sticky_row(
    host: str, upstream: str, *, source: str = "auto", updated_at: int, hit_count: int = 0
) -> tuple[str, tuple[object, ...]]:
    return (
        "INSERT INTO host_upstream"
        " (host, upstream_name, source, last_success_at, fail_count, hit_count, updated_at)"
        " VALUES (?, ?, ?, ?, 0, ?, ?)",
        (host, upstream, source, updated_at, hit_count, updated_at),
    )


def block_row(
    host: str, upstream: str, *, blocked_until: int, fail_count: int = 1
) -> tuple[str, tuple[object, ...]]:
    return (
        "INSERT INTO route_block"
        " (host, upstream_name, fail_count, last_error, last_failure_at, blocked_until)"
        " VALUES (?, ?, ?, 'boom', ?, ?)",
        (host, upstream, fail_count, blocked_until - 600, blocked_until),
    )


def load(
    cfg: DatabaseConfig,
    limits: LimitsConfig | None = None,
    *,
    now: float | None = None,
    expiry: StickyExpiryPolicy | None = None,
) -> InitialState:
    now_unix = time.time() if now is None else now
    return load_initial_state(
        cfg,
        limits or LimitsConfig(),
        now_unix=now_unix,
        now_mono=1000.0,
        sticky_policy=expiry,
    )


class TestStickyBackfill:
    def test_a_missing_database_yields_an_empty_state(self, tmp_path: Path) -> None:
        """首次启动没有库。状态是可重新学习的，不该为它拒绝启动。"""
        initial = load(db_config(tmp_path))
        assert initial.is_empty

    def test_sticky_bindings_are_restored(self, tmp_path: Path) -> None:
        now = int(time.time())
        cfg = seed(tmp_path, [sticky_row("a.com", "up1", updated_at=now, hit_count=7)])
        (entry,) = load(cfg).sticky
        assert (entry.host, entry.upstream, entry.source) == ("a.com", "up1", "auto")
        assert entry.hit_count == 7

    def test_manual_bindings_keep_their_source(self, tmp_path: Path) -> None:
        now = int(time.time())
        cfg = seed(tmp_path, [sticky_row("a.com", "up1", source="manual", updated_at=now)])
        assert load(cfg).sticky[0].source == "manual"

    def test_only_the_most_recent_rows_are_restored(self, tmp_path: Path) -> None:
        """回填数量超过 LRU 容量时只取最近用过的——它们最有价值。"""
        now = int(time.time())
        cfg = seed(
            tmp_path,
            [sticky_row(f"h{i}.com", "up1", updated_at=now - i) for i in range(5)],
        )
        restored = load(cfg, LimitsConfig(sticky_cache_size=2)).sticky
        assert [e.host for e in restored] == ["h0.com", "h1.com"]

    def test_timestamps_are_converted_to_the_monotonic_clock(self, tmp_path: Path) -> None:
        """库存 Unix 时间（可跨重启比较），内存用 monotonic（不受调时影响）。"""
        now = int(time.time())
        cfg = seed(tmp_path, [sticky_row("a.com", "up1", updated_at=now - 30)])
        (entry,) = load(cfg, now=now).sticky
        assert entry.last_used_at == pytest.approx(970.0)


class TestStickyRestoreExpiry:
    """回填过滤：过期 auto 不入内存；manual 不论新旧都进（DD_STORAGE §5.1）。"""

    def test_expired_auto_rows_are_skipped(self, tmp_path: Path) -> None:
        now = int(time.time())
        rows = [sticky_row(f"h{i}.com", "up1", updated_at=now - 10_000 - i) for i in range(5)]
        cfg = seed(tmp_path, rows)
        restored = load(cfg, now=now, expiry=StickyExpiryPolicy(ttl_seconds=3_600.0)).sticky
        assert restored == ()

    def test_recent_auto_rows_still_come_through(self, tmp_path: Path) -> None:
        now = int(time.time())
        rows = [sticky_row("fresh.com", "up1", updated_at=now - 60)]
        cfg = seed(tmp_path, rows)
        (entry,) = load(cfg, now=now, expiry=StickyExpiryPolicy(ttl_seconds=3_600.0)).sticky
        assert entry.host == "fresh.com"

    def test_manual_rows_are_always_restored(self, tmp_path: Path) -> None:
        """manual 是用户声明，跨重启必须保留：无论多久没动都回填。"""
        now = int(time.time())
        cfg = seed(
            tmp_path, [sticky_row("pinned.com", "up1", source="manual", updated_at=now - 10_000)]
        )
        (entry,) = load(cfg, now=now, expiry=StickyExpiryPolicy(ttl_seconds=3_600.0)).sticky
        assert (entry.host, entry.source) == ("pinned.com", "manual")

    def test_zero_ttl_restores_everything(self, tmp_path: Path) -> None:
        """``ttl=0`` 是「禁用过期」，回填不过滤任何 auto——与 ``get_live`` 一致。"""
        now = int(time.time())
        cfg = seed(
            tmp_path,
            [
                sticky_row("old.com", "up1", updated_at=now - 10_000),
                sticky_row("pinned.com", "up1", source="manual", updated_at=now - 10_000),
            ],
        )
        restored = load(cfg, now=now, expiry=StickyExpiryPolicy(ttl_seconds=0.0)).sticky
        assert [e.host for e in restored] == ["old.com", "pinned.com"]

    def test_the_default_policy_disables_filtering(self, tmp_path: Path) -> None:
        """不传 policy 时回填行为与旧版完全一致：不过滤 auto。"""
        now = int(time.time())
        cfg = seed(tmp_path, [sticky_row("old.com", "up1", updated_at=now - 10_000)])
        assert load(cfg, now=now).sticky[0].host == "old.com"

    def test_last_used_at_falls_back_to_updated_at(self, tmp_path: Path) -> None:
        """``last_success_at=0``（管理员手工 insert 常见）时，回填以
        ``updated_at`` 作最后访问时间——否则会被 TTL 误判为远古过期。"""
        now = int(time.time())
        cfg = seed(
            tmp_path,
            [
                (
                    "INSERT INTO host_upstream"
                    " (host, upstream_name, source, last_success_at,"
                    "  fail_count, hit_count, updated_at)"
                    " VALUES (?, ?, 'auto', 0, 0, 1, ?)",
                    ("a.com", "up1", now - 60),
                )
            ],
        )
        (entry,) = load(cfg, now=now, expiry=StickyExpiryPolicy(ttl_seconds=3_600.0)).sticky
        # now_mono=1000.0、距今 60 秒 → 940.0，而不是 last_success_at=0 推出的极负值。
        assert entry.last_used_at == pytest.approx(940.0)


class TestBlockBackfill:
    def test_unexpired_blocks_are_restored(self, tmp_path: Path) -> None:
        now = int(time.time())
        cfg = seed(tmp_path, [block_row("a.com", "up1", blocked_until=now + 300, fail_count=4)])
        (block,) = load(cfg, now=now).blocks
        assert (block.host, block.upstream, block.fail_count) == ("a.com", "up1", 4)
        assert block.blocked_until == pytest.approx(1300.0)
        assert block.reason == "boom"

    def test_expired_blocks_are_not_restored(self, tmp_path: Path) -> None:
        """回填进内存只会白占 LRU 容量。"""
        now = int(time.time())
        cfg = seed(tmp_path, [block_row("a.com", "up1", blocked_until=now - 1)])
        assert load(cfg, now=now).blocks == ()

    def test_the_block_count_respects_the_cache_limit(self, tmp_path: Path) -> None:
        now = int(time.time())
        cfg = seed(
            tmp_path,
            [block_row(f"h{i}.com", "up1", blocked_until=now + 300) for i in range(4)],
        )
        assert len(load(cfg, LimitsConfig(route_block_cache_size=2), now=now).blocks) == 2


class TestHealthBackfill:
    def test_counters_are_restored(self, tmp_path: Path) -> None:
        cfg = seed(
            tmp_path,
            [
                (
                    "INSERT INTO upstream_health"
                    " (upstream_name, total_success, total_failure, avg_latency_ms,"
                    "  circuit_state, updated_at)"
                    " VALUES ('up1', 40, 2, 33, 'open', 1)",
                    (),
                )
            ],
        )
        (row,) = load(cfg).health
        assert (row.upstream, row.total_success, row.total_failure) == ("up1", 40, 2)
        assert row.avg_latency_ms == 33

    def test_the_circuit_state_is_not_exposed_for_backfill(self, tmp_path: Path) -> None:
        """重启可能正是运维在修网络，带着旧的 open 启动会让刚修好的出口继续被拒。"""
        cfg = seed(
            tmp_path,
            [
                (
                    "INSERT INTO upstream_health (upstream_name, circuit_state, updated_at)"
                    " VALUES ('up1', 'open', 1)",
                    (),
                )
            ],
        )
        (row,) = load(cfg).health
        assert not hasattr(row, "circuit_state")

    def test_traffic_bytes_are_restored(self, tmp_path: Path) -> None:
        cfg = seed(
            tmp_path,
            [
                (
                    "INSERT INTO upstream_health"
                    " (upstream_name, bytes_up_total, bytes_down_total, updated_at)"
                    " VALUES ('up1', 300, 400, 1)",
                    (),
                )
            ],
        )
        (row,) = load(cfg).health
        assert (row.total_bytes_up, row.total_bytes_down) == (300, 400)

    def test_missing_traffic_columns_fall_back_without_losing_other_state(
        self, tmp_path: Path
    ) -> None:
        """升级前一次重启：state.db 还是旧 schema，两个字节列不存在。

        这一个查询失败不能拖累整个回填——粘性映射与负面记忆必须照常恢复。
        """
        cfg = seed(
            tmp_path,
            [
                sticky_row("a.com", "up1", updated_at=100),
                (
                    "INSERT INTO upstream_health"
                    " (upstream_name, total_success, total_failure, updated_at)"
                    " VALUES ('up1', 5, 1, 1)",
                    (),
                ),
            ],
        )
        conn = sqlite3.connect(cfg.state_path)
        try:
            conn.execute("ALTER TABLE upstream_health DROP COLUMN bytes_up_total")
            conn.execute("ALTER TABLE upstream_health DROP COLUMN bytes_down_total")
            conn.commit()
        finally:
            conn.close()

        state = load(cfg)
        assert len(state.sticky) == 1
        (row,) = state.health
        assert (row.total_success, row.total_failure) == (5, 1)
        assert (row.total_bytes_up, row.total_bytes_down) == (0, 0)


class TestCorruptDatabase:
    def test_a_broken_file_yields_an_empty_state(self, tmp_path: Path) -> None:
        """损坏的状态库不该阻止代理启动：路由状态可以重新学。"""
        cfg = db_config(tmp_path)
        cfg.state_path.write_bytes(b"this is not a sqlite database")
        assert load(cfg).is_empty

    def test_a_database_without_the_tables_yields_an_empty_state(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(cfg_path := tmp_path / "state.db")
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.close()
        assert cfg_path.is_file()
        assert load(db_config(tmp_path)).is_empty


class TestReadOnlyPool:
    def test_queries_return_rows_by_name(self, tmp_path: Path) -> None:
        now = int(time.time())
        cfg = seed(tmp_path, [sticky_row("a.com", "up1", updated_at=now)])
        pool = ReadOnlyPool(cfg.state_path)
        rows = pool.query("SELECT host, upstream_name FROM host_upstream")
        assert rows[0]["upstream_name"] == "up1"

    def test_writes_are_refused(self, tmp_path: Path) -> None:
        """Web 侧永不直连写库——文件层 mode=ro 与 query_only 双保险。"""
        cfg = seed(tmp_path, [sticky_row("a.com", "up1", updated_at=1)])
        pool = ReadOnlyPool(cfg.state_path)
        with pytest.raises(sqlite3.OperationalError):
            pool.query("DELETE FROM host_upstream")

    def test_the_same_thread_reuses_one_connection(self, tmp_path: Path) -> None:
        cfg = seed(tmp_path, [sticky_row("a.com", "up1", updated_at=1)])
        pool = ReadOnlyPool(cfg.state_path)
        assert pool.connection() is pool.connection()


def test_monotonic_conversion_preserves_intervals() -> None:
    older = to_monotonic(100.0, now_unix=160.0, now_mono=500.0)
    newer = to_monotonic(130.0, now_unix=160.0, now_mono=500.0)
    assert (older, newer) == (440.0, 470.0)
