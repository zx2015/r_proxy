"""全部 SQL 查询。

对应设计：docs/design/DD_WEB.md §4.2、§4.3。

**每个函数都是同步的，必须经 ``await asyncio.to_thread(...)`` 调用**：
``sqlite3`` 是同步库，直接在事件循环里执行会连带卡住代理的转发——它们在同一个
循环上。集中在一个模块里也是为了审查方便：「有没有 SQL 拼接」「有没有漏掉
`to_thread`」只需看这一个文件加它的调用点。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from r_proxy.storage.reader import ReadOnlyPool
from r_proxy.web.schemas import AuditQuery, LogQuery

# 显式列出列名而非 ``SELECT *``：加一列 schema 不应该自动变成 API 变化，而
# 响应模型是 ``extra="forbid"`` 的，静默多出的列会变成 500。
_COLUMNS = (
    "id, request_id, host, url, method, upstream_name, upstream_priority, "
    "attempt_index, decision_source, rule_origin, http_status, error, "
    "failure_kind, keep_reason, elapsed_ms, bytes_up, bytes_down, created_at"
)


def query_logs(pool: ReadOnlyPool, q: LogQuery) -> list[sqlite3.Row]:
    """按筛选条件取一页请求日志，多取一条用于判断是否还有下一页。

    不返回总数：``COUNT(*)`` 要把满足条件的行全数过一遍，而看板只需要知道
    「下一页」按钮能不能点。
    """
    clause, args = _where(q)
    sql = (
        f"SELECT {_COLUMNS} FROM request_log {clause} "
        # ``id`` 是必需的次级排序键：``created_at`` 只到秒，同秒内几十条日志
        # 很常见，只按它排序时两次翻页的边界会漂，同一行可能出现两次或被跳过。
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    return pool.query(sql, (*args, q.page_size + 1, q.offset))


def query_switch_request_ids(pool: ReadOnlyPool, q: LogQuery) -> list[str]:
    """发生过切换的请求 ID，按最近一次尝试倒序；同样多取一条判断下一页。

    判据是「存在 ``attempt_index > 0`` 的尝试行」：第 0 次尝试是首选出口，有
    第 1 次就说明换过出口。筛选条件作用在**尝试行**上，例如 ``status=502``
    的含义是「某次尝试拿到 502 且该请求切换过」。
    """
    clause, args = _where(q, extra=("attempt_index > 0",))
    sql = (
        "SELECT request_id, MAX(created_at) AS last_at, MAX(id) AS last_id "
        f"FROM request_log {clause} GROUP BY request_id "
        "ORDER BY last_at DESC, last_id DESC LIMIT ? OFFSET ?"
    )
    rows = pool.query(sql, (*args, q.page_size + 1, q.offset))
    return [str(row["request_id"]) for row in rows]


def query_attempts(pool: ReadOnlyPool, request_ids: Sequence[str]) -> list[sqlite3.Row]:
    """取这些请求的全部尝试行，按发生顺序返回。"""
    if not request_ids:
        return []
    # 占位符个数由列表长度决定，与用户输入的内容无关；request_id 本身仍然是
    # 参数。上游的 page_size 上限 1000 保证不会撞上 SQLite 的参数个数限制。
    placeholders = ",".join("?" * len(request_ids))
    sql = (
        f"SELECT {_COLUMNS} FROM request_log WHERE request_id IN ({placeholders}) "
        "ORDER BY created_at, id"
    )
    return pool.query(sql, tuple(request_ids))


_AUDIT_COLUMNS = "id, actor, action, target, diff, version_before, version_after, created_at"


def query_audit(pool: ReadOnlyPool, q: AuditQuery) -> list[sqlite3.Row]:
    """配置操作审计，最近的在前；同样多取一条判断下一页。"""
    where: list[str] = []
    args: list[object] = []
    if q.action:
        where.append("action = ?")
        args.append(q.action)
    if q.since is not None:
        where.append("created_at >= ?")
        args.append(q.since)
    if q.until is not None:
        where.append("created_at <= ?")
        args.append(q.until)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT {_AUDIT_COLUMNS} FROM config_audit {clause} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    return pool.query(sql, (*args, q.page_size + 1, q.offset))


def _where(q: LogQuery, *, extra: Sequence[str] = ()) -> tuple[str, tuple[object, ...]]:
    """动态 WHERE 的**唯一**构造方式：条件是固定字面量，值全部走 ``?``。

    把列名或值拼进 SQL 就是注入入口，因此这里连列名都不接受参数化。
    """
    where: list[str] = list(extra)
    args: list[object] = []
    if q.host:
        where.append("host = ?")
        args.append(q.host)
    if q.upstream:
        where.append("upstream_name = ?")
        args.append(q.upstream)
    if q.status is not None:
        where.append("http_status = ?")
        args.append(q.status)
    if q.since is not None:
        where.append("created_at >= ?")
        args.append(q.since)
    if q.until is not None:
        where.append("created_at <= ?")
        args.append(q.until)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, tuple(args)
