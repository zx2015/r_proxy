"""只读访问与启动回填。

对应设计：docs/design/DD_STORAGE.md §5、§7。

WAL 模式下读者不阻塞写者、写者不阻塞读者，因此 Web 查询与写者线程可以完全
并行——这是选择 WAL 的主要理由。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from r_proxy.config.model import DatabaseConfig, LimitsConfig
from r_proxy.storage.expiry import StickyExpiryPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StickyRow:
    host: str
    upstream: str
    source: str
    fail_count: int
    hit_count: int
    last_used_at: float  # 已转换为 monotonic


@dataclass(frozen=True, slots=True)
class BlockRow:
    host: str
    upstream: str
    fail_count: int
    reason: str
    blocked_until: float  # 已转换为 monotonic


@dataclass(frozen=True, slots=True)
class HealthRow:
    """只回填累计计数，供 Web 展示历史成功率。

    **不含 ``circuit_state``**：熔断状态一律重置为 ``closed``，见 §5。
    """

    upstream: str
    total_success: int
    total_failure: int
    avg_latency_ms: int
    total_bytes_up: int = 0
    total_bytes_down: int = 0


@dataclass(frozen=True, slots=True)
class InitialState:
    sticky: tuple[StickyRow, ...] = ()
    blocks: tuple[BlockRow, ...] = ()
    health: tuple[HealthRow, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.sticky or self.blocks or self.health)


def load_initial_state(
    cfg: DatabaseConfig,
    limits: LimitsConfig,
    *,
    now_unix: float,
    now_mono: float,
    sticky_policy: StickyExpiryPolicy | None = None,
) -> InitialState:
    """从 ``state.db`` 读出重启后仍然有效的状态。

    首次启动（库还不存在）或库读不出来时返回空状态：状态是可重新学习的，
    为了它拒绝启动不合理。
    """
    if not cfg.state_path.exists():
        return InitialState()
    try:
        conn = connect_readonly(cfg.state_path)
    except sqlite3.Error as exc:
        logger.error("状态库打开失败，以空状态启动: %s", exc)
        return InitialState()
    try:
        return InitialState(
            sticky=_load_sticky(
                conn,
                limits,
                now_unix=now_unix,
                now_mono=now_mono,
                expiry=sticky_policy or StickyExpiryPolicy(),
            ),
            blocks=_load_blocks(conn, limits, now_unix=now_unix, now_mono=now_mono),
            health=_load_health(conn),
        )
    except sqlite3.Error as exc:
        logger.error("状态回填失败，以空状态启动: %s", exc)
        return InitialState()
    finally:
        conn.close()


def _load_sticky(
    conn: sqlite3.Connection,
    limits: LimitsConfig,
    *,
    now_unix: float,
    now_mono: float,
    expiry: StickyExpiryPolicy,
) -> tuple[StickyRow, ...]:
    # ``manual`` 不论新旧都回填——用户的声明跨重启必须保留；``auto`` 已被
    # TTL 淘汰的行也不回填，回填进内存只会白占 LRU 容量（DD_STORAGE §5.1）。
    # 禁用过期时 cutoff=0，``updated_at >= 0`` 恒真，等价于不过滤。
    # 按 updated_at 降序取前 N 条（N = LRU 容量）：最近用过的最有价值。
    rows = conn.execute(
        """
        SELECT host, upstream_name, source, fail_count, hit_count,
               MAX(last_success_at, updated_at)
          FROM host_upstream
         WHERE source = 'manual' OR updated_at >= ?
         ORDER BY updated_at DESC
         LIMIT ?
        """,
        (expiry.cutoff(now_unix=int(now_unix)), limits.sticky_cache_size),
    ).fetchall()
    return tuple(
        StickyRow(
            host=row[0],
            upstream=row[1],
            source=row[2],
            fail_count=row[3],
            hit_count=row[4],
            last_used_at=to_monotonic(row[5], now_unix=now_unix, now_mono=now_mono),
        )
        for row in rows
    )


def _load_blocks(
    conn: sqlite3.Connection, limits: LimitsConfig, *, now_unix: float, now_mono: float
) -> tuple[BlockRow, ...]:
    # 只回填未过期的：已过期的记录回填进内存只会白占 LRU 容量。
    rows = conn.execute(
        """
        SELECT host, upstream_name, fail_count, last_error, blocked_until
          FROM route_block
         WHERE blocked_until > ?
         ORDER BY last_failure_at DESC
         LIMIT ?
        """,
        (int(now_unix), limits.route_block_cache_size),
    ).fetchall()
    return tuple(
        BlockRow(
            host=row[0],
            upstream=row[1],
            fail_count=row[2],
            reason=row[3] or "",
            blocked_until=to_monotonic(row[4], now_unix=now_unix, now_mono=now_mono),
        )
        for row in rows
    )


def _load_health(conn: sqlite3.Connection) -> tuple[HealthRow, ...]:
    try:
        rows = conn.execute(
            "SELECT upstream_name, total_success, total_failure, avg_latency_ms, "
            "bytes_up_total, bytes_down_total FROM upstream_health"
        ).fetchall()
        return tuple(HealthRow(row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows)
    except sqlite3.OperationalError:
        # 读连接在写者线程启动、完成迁移**之前**打开（见 load_initial_state
        # 的调用顺序），因此从 STATE schema 1 升级到 2 的那一次重启会撞上
        # 「新列还不存在」。不让这一个查询的失败拖累整个回填（否则粘性映射
        # 与负面记忆会跟着一起丢），退回旧列集，字节数按「还没测过」处理为 0
        # ——这本来就是事实：旧库里从未记过这两列。
        rows = conn.execute(
            "SELECT upstream_name, total_success, total_failure, avg_latency_ms "
            "FROM upstream_health"
        ).fetchall()
        return tuple(HealthRow(row[0], row[1], row[2], row[3]) for row in rows)


def to_monotonic(unix_ts: float, *, now_unix: float, now_mono: float) -> float:
    """库里存的是 ``time.time()``（可跨重启比较），内存用的是 ``time.monotonic()``
    （不受时钟调整影响）。转换在回填时一次性完成。"""
    return now_mono - (now_unix - unix_ts)


def connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = None
    # 双保险：即便 URI 参数被改，写入仍会被拒绝。
    conn.execute("PRAGMA query_only=ON")
    # 比写者的 5000 短：Web 查询宁可快速失败，也不要挂住线程池里的线程。
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


class ReadOnlyPool:
    """每线程一个只读连接。``sqlite3`` 连接绑定线程，不可跨线程使用。

    ``asyncio.to_thread`` 用的线程池会复用线程，因此「每线程一个连接」是
    合适的粒度。连接不显式关闭：线程池的线程与进程同寿。
    """

    __slots__ = ("_local", "_path")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect_readonly(self._path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """同步执行。**必须**经 ``await asyncio.to_thread(...)`` 调用——
        ``sqlite3`` 是同步库，直接在事件循环里执行会卡住整个进程。"""
        return list(self.connection().execute(sql, params).fetchall())


def now_pair() -> tuple[float, float]:
    """同一时刻的 ``(time.time(), time.monotonic())``，供回填做时钟转换。"""
    return time.time(), time.monotonic()
