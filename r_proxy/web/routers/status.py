"""状态与监控接口。

对应设计：docs/design/DD_WEB.md §4.1、§4.2、§8.4，需求：WEBUI_SPEC.md §3.1。

内存状态**不经 `to_thread`**：几个字典查找是微秒级的，放进线程池只会增加调度
开销，还会和 DNS 解析抢线程。反过来，``logs.db`` 的每一次查询都必须经
`to_thread`——它和代理转发跑在同一个事件循环上。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterable, Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from r_proxy import __version__
from r_proxy.storage.queue import config_audit
from r_proxy.web import queries, views
from r_proxy.web.deps import AppDep, Authenticated, client_ip
from r_proxy.web.schemas import (
    ConnectionsInfo,
    HealthResponse,
    HostTrafficItem,
    HostTrafficQuery,
    HostTrafficResponse,
    LogItem,
    LogPage,
    LogQuery,
    ProxyInfo,
    RequestsInfo,
    StatusResponse,
    StorageInfo,
    SwitchAttempt,
    SwitchItem,
    SwitchPage,
    UpstreamHealthItem,
)

router = APIRouter()

LogQueryDep = Annotated[LogQuery, Depends()]
HostTrafficQueryDep = Annotated[HostTrafficQuery, Depends()]


@router.get("/healthz", include_in_schema=False)
async def healthz() -> Response:
    """存活探针。**不需要认证**，也不返回任何内部信息。"""
    return Response(content="ok", media_type="text/plain")


@router.get("/status", dependencies=[Authenticated])
async def status(app: AppDep) -> StatusResponse:
    health = app.state.health.all(now=time.monotonic())
    success = sum(h.total_success for h in health)
    failure = sum(h.total_failure for h in health)
    attempts = success + failure
    host, port = app.proxy_address
    metrics = app.storage.metrics()
    return StatusResponse(
        version=__version__,
        uptime_seconds=app.uptime_seconds,
        config_version=app.snapshot.config_version,
        proxy=ProxyInfo(host=host, port=port),
        connections=ConnectionsInfo(
            active=app.active_connections,
            rejected=app.rejected_connections,
            limit=app.snapshot.limits.max_client_connections,
        ),
        requests=RequestsInfo(
            attempts=attempts,
            success=success,
            failure=failure,
            # 没有任何尝试时报 0.0 而不是 1.0：「还没跑过」不是「全部成功」。
            success_rate=0.0 if attempts == 0 else success / attempts,
        ),
        storage=StorageInfo(
            queue_size=metrics.queue_size,
            queue_capacity=metrics.queue_capacity,
            queue_high_water=metrics.queue_high_water,
            dropped_lossy=metrics.dropped_lossy,
            dropped_normal=metrics.dropped_normal,
            dropped_critical=metrics.dropped_critical,
            write_errors=metrics.write_errors,
            last_flush_at=metrics.last_flush_at,
            flush_duration_p99_ms=metrics.flush_duration_p99_ms,
            merge_ratio=metrics.merge_ratio,
        ),
    )


@router.get("/logs", dependencies=[Authenticated])
async def list_logs(params: LogQueryDep, app: AppDep) -> LogPage:
    rows = await asyncio.to_thread(queries.query_logs, app.storage.logs_reader, params)
    return LogPage(
        items=[LogItem.model_validate(dict(row)) for row in rows[: params.page_size]],
        page=params.page,
        page_size=params.page_size,
        has_more=len(rows) > params.page_size,
    )


@router.get("/logs/switches", dependencies=[Authenticated])
async def list_switches(params: LogQueryDep, app: AppDep) -> SwitchPage:
    """只返回切换过出口的请求，每条带完整尝试链。

    分两次查询：先定位 request_id，再取这些 ID 的全部尝试行。一次查询做不到
    ——分页的单位是「请求」，而筛选条件作用在「尝试行」上，用 ``LIMIT`` 直接
    截行会把某个请求的尝试链切断一半。
    """
    reader = app.storage.logs_reader
    found = await asyncio.to_thread(queries.query_switch_request_ids, reader, params)
    request_ids = found[: params.page_size]
    rows = await asyncio.to_thread(queries.query_attempts, reader, request_ids)
    return SwitchPage(
        items=_chains(request_ids, rows),
        page=params.page,
        page_size=params.page_size,
        has_more=len(found) > params.page_size,
    )


@router.get("/traffic/hosts", dependencies=[Authenticated])
async def host_traffic(params: HostTrafficQueryDep, app: AppDep) -> HostTrafficResponse:
    """当日（或指定区间）各 host 的流量排行。"""
    since, until = params.resolved_range()
    rows = await asyncio.to_thread(
        queries.query_host_traffic, app.storage.logs_reader, since, until, params.limit
    )
    return HostTrafficResponse(
        items=[HostTrafficItem.model_validate(dict(row)) for row in rows],
        since=since,
        until=until,
    )


@router.get("/health", dependencies=[Authenticated])
async def health(app: AppDep) -> HealthResponse:
    """出口健康表。按优先级升序，与候选链的实际顺序一致。"""
    now = time.monotonic()
    table = app.state.health
    return HealthResponse(
        upstreams=[
            views.health_item(cfg, table, now=now)
            for cfg in views.by_priority(app.snapshot.upstreams)
        ]
    )


@router.post("/health/{name}/reset", dependencies=[Authenticated])
async def reset_health(name: str, request: Request, app: AppDep) -> UpstreamHealthItem:
    """手动解除熔断。直接改内存状态，落盘由周期任务负责。"""
    cfg = app.snapshot.upstream(name)
    if cfg is None:
        raise HTTPException(404, detail="出口不存在")
    app.state.health.clear_circuit(name)
    # 「这个出口怎么突然恢复了」必须能追溯，因此运维动作也进审计。
    app.storage.queue.put(
        config_audit(
            actor=client_ip(request),
            action="reset_health",
            target=name,
            diff=None,
            version_before=app.snapshot.config_version,
            version_after=app.snapshot.config_version,
            now_unix=int(time.time()),
        )
    )
    return views.health_item(cfg, app.state.health, now=time.monotonic())


def _chains(request_ids: Sequence[str], rows: Iterable[sqlite3.Row]) -> list[SwitchItem]:
    """把尝试行按 request_id 归组，顺序沿用查询给出的顺序。"""
    grouped: dict[str, list[sqlite3.Row]] = {rid: [] for rid in request_ids}
    for row in rows:
        grouped[str(row["request_id"])].append(row)
    items: list[SwitchItem] = []
    for rid in request_ids:
        attempts = grouped[rid]
        # 两次查询之间保留策略可能刚好清掉这几行，此时跳过而不是回一条空链。
        if not attempts:
            continue
        first = attempts[0]
        items.append(
            SwitchItem(
                request_id=rid,
                host=first["host"],
                url=first["url"],
                method=first["method"],
                attempts=[
                    SwitchAttempt(
                        attempt_index=row["attempt_index"],
                        upstream_name=row["upstream_name"],
                        upstream_priority=row["upstream_priority"],
                        http_status=row["http_status"],
                        error=row["error"],
                        failure_kind=row["failure_kind"],
                        elapsed_ms=row["elapsed_ms"],
                        created_at=row["created_at"],
                    )
                    for row in attempts
                ],
            )
        )
    return items
