"""DDL、迁移与写连接的打开方式。

对应设计：docs/design/DD_STORAGE.md §2、§3。

三库划分的收益不是「避免争用」——WAL 下每个库只有一个写者，本来就没有写写
争用。``state.db`` 与 ``logs.db`` 分开是为了**运维上的可丢弃性**：日志库可以
独立轮转、清空、删除重建而不牵连路由状态。``rules.db`` 分开则是为了保住
「每个库只有一个写者」这条不变量——它的写者是 Web 的配置写入器而非写者线程
（§2.1）。
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 1

# STRICT 表需要 SQLite 3.37+。默认的动态类型会让「写字符串到 INTEGER 列」
# 静默成功，等到读取时才炸——那时已经无从追溯是谁写坏的。
MIN_SQLITE_VERSION = (3, 37, 0)

VERSION_KEY = "schema_version"


class StorageError(Exception):
    """数据库无法按预期使用。启动阶段抛出即拒绝启动。"""


class Database(StrEnum):
    STATE = "state"
    LOGS = "logs"
    RULES = "rules"


_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT;
"""

# 版本号存在 schema_meta 而非 PRAGMA user_version：后者是单个整数，无法记录
# 迁移时间、程序版本等辅助信息。
_STATE_V1 = """
CREATE TABLE IF NOT EXISTS host_upstream (
    host              TEXT    PRIMARY KEY,
    upstream_name     TEXT    NOT NULL,
    source            TEXT    NOT NULL CHECK (source IN ('auto', 'manual')),
    last_url          TEXT,
    last_success_at   INTEGER NOT NULL DEFAULT 0,
    last_http_status  INTEGER,
    fail_count        INTEGER NOT NULL DEFAULT 0,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_hu_upstream ON host_upstream(upstream_name);
CREATE INDEX IF NOT EXISTS idx_hu_updated  ON host_upstream(updated_at DESC);

CREATE TABLE IF NOT EXISTS route_block (
    host              TEXT    NOT NULL,
    upstream_name     TEXT    NOT NULL,
    fail_count        INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    last_failure_at   INTEGER NOT NULL,
    blocked_until     INTEGER NOT NULL,
    PRIMARY KEY (host, upstream_name)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_rb_until ON route_block(blocked_until);

CREATE TABLE IF NOT EXISTS upstream_health (
    upstream_name         TEXT    PRIMARY KEY,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    total_success         INTEGER NOT NULL DEFAULT 0,
    total_failure         INTEGER NOT NULL DEFAULT 0,
    avg_latency_ms        INTEGER NOT NULL DEFAULT 0,
    circuit_state         TEXT    NOT NULL DEFAULT 'closed'
                                  CHECK (circuit_state IN
                                         ('closed', 'open', 'half_open')),
    cooldown_until        INTEGER NOT NULL DEFAULT 0,
    auth_error            INTEGER NOT NULL DEFAULT 0,
    updated_at            INTEGER NOT NULL
) STRICT;
"""

_LOGS_V1 = """
CREATE TABLE IF NOT EXISTS request_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id         TEXT    NOT NULL,
    host               TEXT    NOT NULL,
    url                TEXT,
    method             TEXT    NOT NULL,
    upstream_name      TEXT    NOT NULL,
    upstream_priority  INTEGER,
    attempt_index      INTEGER NOT NULL DEFAULT 0,
    decision_source    TEXT,
    rule_origin        TEXT,
    http_status        INTEGER,
    error              TEXT,
    failure_kind       TEXT,
    keep_reason        TEXT,
    elapsed_ms         INTEGER NOT NULL,
    bytes_up           INTEGER NOT NULL DEFAULT 0,
    bytes_down         INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_rl_host    ON request_log(host, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rl_up      ON request_log(upstream_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rl_created ON request_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rl_reqid   ON request_log(request_id);

CREATE TABLE IF NOT EXISTS config_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor           TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    diff            TEXT,
    version_before  TEXT,
    version_after   TEXT,
    created_at      INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_ca_created ON config_audit(created_at DESC);
"""

# position 不加唯一约束：交换两行在唯一索引下要变成三步（先写一个不冲突的
# 临时值）。规则的每次保存都是整表替换，position 在事务内重编号为 0..N-1，
# 天然唯一。代价是外部手工改库可能造成重复值，读取侧用 ORDER BY position, id
# 兜底。condition 也不加 UNIQUE：条件重复是告警而非错误。
_RULES_V1 = """
CREATE TABLE IF NOT EXISTS rule (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position    INTEGER NOT NULL,
    condition   TEXT    NOT NULL,
    upstream    TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_rule_position ON rule(position);

CREATE TABLE IF NOT EXISTS rule_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT;

INSERT INTO rule_meta (key, value) VALUES ('revision', '0')
    ON CONFLICT(key) DO NOTHING;
"""

_MIGRATIONS: dict[Database, dict[int, str]] = {
    Database.STATE: {1: _STATE_V1},
    Database.LOGS: {1: _LOGS_V1},
    Database.RULES: {1: _RULES_V1},
}


def open_write(path: Path, database: Database) -> sqlite3.Connection:
    """打开写连接并迁移到当前版本。只应由写者线程调用。

    ``isolation_level=None`` 关闭 sqlite3 模块的隐式事务管理，由我们显式
    控制 ``BEGIN IMMEDIATE``；``check_same_thread`` 保留默认的 True，连接被
    误用到其他线程时立即报错，而不是产生难以复现的损坏。
    """
    _require_sqlite_version()
    fresh = not path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        if fresh:
            # auto_vacuum 必须在建库时设置，对已有数据库无效。
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL 下 NORMAL 足够安全（崩溃不损坏，最多丢最后几个事务），
        # 比 FULL 快一个数量级。
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn, database)
    except (sqlite3.Error, OSError) as exc:
        # 目录建不出来与库打不开是同一类故障：数据目录配错了。
        raise StorageError(f"打开数据库失败 {path.name}: {exc}") from exc
    return conn


def migrate(conn: sqlite3.Connection, database: Database, *, target: int = SCHEMA_VERSION) -> int:
    """把库迁移到 ``target`` 版本，返回迁移后的版本号。

    **降级明确拒绝**：用旧版程序打开新版数据库可能因缺少列而静默写入错误
    数据，报错退出比冒险继续安全。
    """
    conn.executescript(_SCHEMA_META)
    current = read_version(conn)
    if current > target:
        raise StorageError(
            f"数据库版本 {current} 高于本程序支持的 {target}，请升级 r-proxy 或使用新的数据目录"
        )
    steps = _MIGRATIONS[database]
    for step in range(current + 1, target + 1):
        with conn:  # 自动事务：迁移与版本号必须一起生效
            conn.executescript(steps[step])
            _write_version(conn, step)
    return target


def read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (VERSION_KEY,)).fetchone()
    return int(row[0]) if row is not None else 0


def _write_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (VERSION_KEY, str(version)),
    )


def _require_sqlite_version() -> None:
    current = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
    if current < MIN_SQLITE_VERSION:
        want = ".".join(str(p) for p in MIN_SQLITE_VERSION)
        raise StorageError(f"需要 SQLite {want} 及以上（STRICT 表），当前 {sqlite3.sqlite_version}")
