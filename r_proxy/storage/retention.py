"""日志清理与负面记忆清理。

对应设计：docs/design/DD_STORAGE.md §6。

**清理在写者线程内执行**，与业务写入串行。放到独立线程会引入第二个写者，
违反核心约束。删除十万行可能耗时数百毫秒，期间写者不消费队列——队列有
一万容量，这点积压远未触及水位。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import TYPE_CHECKING

from r_proxy.config.model import DatabaseConfig
from r_proxy.storage.expiry import StickyExpiryPolicy
from r_proxy.storage.schema import Database

if TYPE_CHECKING:
    from r_proxy.storage.writer import WriterMetrics

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600.0

DAY_SECONDS = 86400

# 过期后再留一天才删：fail_count 的历史值对诊断有价值（「这个 host 经这个
# 出口失败过 47 次」）。内存侧的惰性过期已保证过期记录不影响路由。
BLOCK_GRACE_SECONDS = DAY_SECONDS

# 第二条 DELETE 用 `id <=` 加子查询而非 `ORDER BY ... LIMIT`：后者需要
# SQLite 编译时启用 SQLITE_ENABLE_UPDATE_DELETE_LIMIT，并非所有发行版都有。
_TRIM_BY_ROWS = """
    DELETE FROM request_log
     WHERE id <= (
         SELECT id FROM request_log
          ORDER BY id DESC
          LIMIT 1 OFFSET ?
     )
"""

# traffic_log 复用与 request_log 同一份保留配置（DD_STORAGE.md §6.1）：
# 行数增长速率是同一数量级（约等于成功交付的请求数，甚至更少），没有证据
# 表明这张表需要独立的保留期。
_TRIM_TRAFFIC_BY_ROWS = """
    DELETE FROM traffic_log
     WHERE id <= (
         SELECT id FROM traffic_log
          ORDER BY id DESC
          LIMIT 1 OFFSET ?
     )
"""


class Retention:
    """按固定间隔在写者线程内执行清理。"""

    __slots__ = ("_cfg", "_expiry", "_interval", "_next_run")

    def __init__(
        self,
        cfg: DatabaseConfig,
        *,
        expiry: StickyExpiryPolicy | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        first_run_immediately: bool = True,
    ) -> None:
        self._cfg = cfg
        # 默认禁用粘性清理：只有 StorageService 显式注入策略时才生效，
        # 让旧调用点（不带 expiry 的测试）保持原行为。
        self._expiry = expiry if expiry is not None else StickyExpiryPolicy()
        self._interval = interval_seconds
        # 默认启动后立刻跑一次：进程可能刚好在上次清理前崩了，日志表已经
        # 超限，再等一小时只会让它继续涨。
        self._next_run = 0.0 if first_run_immediately else time.monotonic() + interval_seconds

    def maybe_run(
        self, connections: dict[Database, sqlite3.Connection], metrics: WriterMetrics
    ) -> bool:
        now = time.monotonic()
        if now < self._next_run:
            return False
        self._next_run = now + self._interval
        deleted = self.run(connections)
        metrics.retention_runs += 1
        if deleted:
            logger.info("清理完成，删除 %d 行", deleted)
        return True

    def run(self, connections: dict[Database, sqlite3.Connection]) -> int:
        state_conn = connections[Database.STATE]
        return (
            self._clean_logs(connections[Database.LOGS])
            + self._clean_blocks(state_conn)
            + self._clean_sticky(state_conn)
        )

    def _clean_logs(self, conn: sqlite3.Connection) -> int:
        cutoff = int(time.time()) - self._cfg.retention_days * DAY_SECONDS
        return self._in_transaction(
            conn,
            (
                ("DELETE FROM request_log WHERE created_at < ?", (cutoff,)),
                (_TRIM_BY_ROWS, (self._cfg.max_log_rows,)),
                ("DELETE FROM traffic_log WHERE created_at < ?", (cutoff,)),
                (_TRIM_TRAFFIC_BY_ROWS, (self._cfg.max_log_rows,)),
            ),
        )

    def _clean_blocks(self, conn: sqlite3.Connection) -> int:
        cutoff = int(time.time()) - BLOCK_GRACE_SECONDS
        return self._in_transaction(
            conn, (("DELETE FROM route_block WHERE blocked_until < ?", (cutoff,)),)
        )

    def _clean_sticky(self, conn: sqlite3.Connection) -> int:
        """删除磁盘上早于 TTL 的 ``auto`` 粘性行。``manual`` 与 ``ttl=0`` 跳过。

        不留宽限期（与 :meth:`_clean_blocks` 的 ``BLOCK_GRACE_SECONDS`` 相反）：
        ``auto`` 是「上次哪条路能用」的缓存，价值随时间衰减，也没有需要保留的
        诊断字段。``manual`` 永不删——用户的声明跨重启必须保留。

        与内存侧的惰性过期是**两条独立路径**：这里只动磁盘副本，**不**发
        ``sticky_delete`` 入队通知内存。最终一致：内存权威在路由热路径，
        磁盘副本短暂「出现又消失」对路由不可观测（DD_ROUTING §7.7）。
        """
        if not self._expiry.enabled:
            return 0
        cutoff = self._expiry.cutoff(now_unix=int(time.time()))
        return self._in_transaction(
            conn,
            (
                (
                    "DELETE FROM host_upstream WHERE source = 'auto' AND updated_at < ?",
                    (cutoff,),
                ),
            ),
        )

    def _in_transaction(
        self, conn: sqlite3.Connection, statements: tuple[tuple[str, tuple[object, ...]], ...]
    ) -> int:
        deleted = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sql, params in statements:
                deleted += conn.execute(sql, params).rowcount
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            logger.error("清理失败: %s", exc)
            return 0
        # 增量回收：VACUUM 要重写整个文件并持排他锁，几百 MB 会锁住数秒。
        try:
            conn.execute("PRAGMA incremental_vacuum(1000)")
        except sqlite3.Error as exc:
            logger.warning("增量回收失败: %s", exc)
        return deleted
