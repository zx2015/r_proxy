"""全局设置、重载、备份恢复与审计。

对应设计：docs/design/DD_WEB.md §6，需求：WEBUI_SPEC.md §2.5、§3.5、§6.5。

`GET /api/settings` 的响应带 `ETag`，`PUT` 必须回传 `config_version`（或 `If-Match`
头），不匹配即 `409`。**响应绝不包含 `auth_token`**：它是访问这套接口的凭据本身。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from r_proxy.app import StartupError
from r_proxy.config.model import ConfigSnapshot
from r_proxy.storage.queue import config_audit
from r_proxy.web import queries, toml_edit
from r_proxy.web.config_writer import ConfigConflict, ConfigInvalid
from r_proxy.web.deps import AppDep, Authenticated, WriterDep, client_ip
from r_proxy.web.errors import invalid_config, version_conflict
from r_proxy.web.schemas import (
    AuditItem,
    AuditPage,
    AuditQuery,
    BackupItem,
    BackupsResponse,
    CircuitBreakerSettings,
    ConfigVersionResponse,
    ListenSettings,
    RateLimitSettings,
    RestoreRequest,
    RoutingSettings,
    SettingsResponse,
    SettingsUpdateRequest,
    StorageSettings,
)

logger = logging.getLogger(__name__)

router = APIRouter()

AuditQueryDep = Annotated[AuditQuery, Depends()]

# 改了要重启才生效的项。界面据此标注，避免用户以为改完就生效了（WEBUI_SPEC §6.3）。
RESTART_REQUIRED = ("listen.host", "listen.port", "webui.host", "webui.port", "webui.auth_token")


@router.get("/settings", dependencies=[Authenticated])
async def read_settings(app: AppDep, response: Response) -> SettingsResponse:
    snapshot = app.snapshot
    response.headers["ETag"] = f'"{snapshot.config_version}"'
    return _project(snapshot)


@router.put("/settings", dependencies=[Authenticated])
async def update_settings(
    body: SettingsUpdateRequest,
    request: Request,
    writer: WriterDep,
    if_match: Annotated[str | None, Header(alias="If-Match", max_length=64)] = None,
) -> ConfigVersionResponse:
    """按点分键改写 `config.toml`，只动本次提交的项。

    键名由服务端的白名单决定，客户端只能选字段（见 `SettingsUpdateRequest`）：
    让客户端指定点分键就等于允许它写 `webui.auth_token`，或者写进任何一个加载器
    不认识的键——后者会让下一次启动直接失败。
    """
    changes = body.changes()
    if not changes:
        raise HTTPException(400, detail="没有要修改的设置")
    expected = _expected_version(body.config_version, if_match)
    try:
        version = await writer.edit_config(
            lambda text: toml_edit.apply_settings(text, changes),
            expected_version=expected,
            actor=client_ip(request),
            action="settings.update",
            target="config.toml",
        )
    except ConfigConflict as exc:
        raise version_conflict(exc) from exc
    except ConfigInvalid as exc:
        raise invalid_config(exc) from exc
    return ConfigVersionResponse(config_version=version)


@router.post("/reload", dependencies=[Authenticated])
async def reload_config(request: Request, app: AppDep) -> ConfigVersionResponse:
    """重新读取 `config.toml` 与 `rules.db` 中的规则表。

    失败时保留正在生效的快照——一次手滑的编辑不该让代理停摆——因此这里回 `400`
    而不是 `500`：服务仍在正常转发，是提交的内容不可用。
    """
    before = app.snapshot.config_version
    try:
        await app.reload()
    except StartupError as exc:
        raise HTTPException(
            400,
            detail={"code": "CONFIG_INVALID", "message": _sanitize(str(exc), app.snapshot)},
        ) from exc
    app.storage.queue.put(
        config_audit(
            actor=client_ip(request),
            action="config.reload",
            target=app.snapshot.source_path.name,
            diff=None,
            version_before=before,
            version_after=app.snapshot.config_version,
            now_unix=int(time.time()),
        )
    )
    return ConfigVersionResponse(config_version=app.snapshot.config_version)


@router.get("/config/backups", dependencies=[Authenticated])
async def list_backups(writer: WriterDep) -> BackupsResponse:
    backups = await writer.list_backups()
    return BackupsResponse(
        backups=[
            BackupItem(filename=b.filename, size=b.size, created_at=b.created_at) for b in backups
        ]
    )


@router.post("/config/restore", dependencies=[Authenticated])
async def restore_backup(
    body: RestoreRequest, request: Request, writer: WriterDep
) -> ConfigVersionResponse:
    """从备份恢复。

    **恢复也要过校验**：备份可能很久以前就存下了，其中引用的出口在当前规则里
    已不存在。恢复前校验避免把服务恢复到一个起不来的状态。

    恢复本身也会先备份当前配置（在 `edit_config` 内部），因此这个操作可以再被
    撤销。
    """
    try:
        content = await writer.read_backup(body.filename)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="备份不存在") from exc
    except OSError as exc:
        raise HTTPException(500, detail="备份无法读取") from exc
    try:
        version = await writer.edit_config(
            lambda _current: content,
            expected_version=body.config_version,
            actor=client_ip(request),
            action="config.restore",
            target=body.filename,
        )
    except ConfigConflict as exc:
        raise version_conflict(exc) from exc
    except ConfigInvalid as exc:
        raise invalid_config(exc) from exc
    return ConfigVersionResponse(config_version=version)


@router.get("/audit", dependencies=[Authenticated])
async def list_audit(params: AuditQueryDep, app: AppDep) -> AuditPage:
    rows = await asyncio.to_thread(queries.query_audit, app.storage.logs_reader, params)
    items = [AuditItem(**dict(row)) for row in rows[: params.page_size]]
    return AuditPage(
        items=items,
        page=params.page,
        page_size=params.page_size,
        has_more=len(rows) > params.page_size,
    )


def _expected_version(from_body: str, if_match: str | None) -> str:
    """请求体与 `If-Match` 头都给了就必须一致。

    不一致时无从判断客户端到底以哪个为准，静默取一个可能覆盖掉它并不知道的
    改动。
    """
    if if_match is None:
        return from_body
    header = if_match.strip().strip('"')
    if header != from_body:
        raise HTTPException(400, detail="If-Match 与请求体中的 config_version 不一致")
    return header


def _project(snapshot: ConfigSnapshot) -> SettingsResponse:
    r = snapshot.routing
    d = snapshot.database
    return SettingsResponse(
        config_version=snapshot.config_version,
        routing=RoutingSettings(
            connect_timeout=r.connect_timeout,
            read_timeout=r.read_timeout,
            switch_on_status=sorted(r.switch_on_status),
            sticky_fail_threshold=r.sticky_fail_threshold,
            sticky_ttl=r.sticky_ttl,
            route_block_ttl=r.route_block_ttl,
            tunnel_probe_window=r.tunnel_probe_window,
            switch_buffer_bytes=r.switch_buffer_bytes,
            happy_eyeballs_delay=r.happy_eyeballs_delay,
            status_switch_rate_limit=RateLimitSettings(
                max_switches_per_host=r.status_switch_rate_limit.max_switches_per_host,
                window_seconds=r.status_switch_rate_limit.window_seconds,
            ),
            circuit_breaker=CircuitBreakerSettings(
                enabled=r.circuit_breaker.enabled,
                fail_threshold=r.circuit_breaker.fail_threshold,
                cooldown_seconds=r.circuit_breaker.cooldown_seconds,
            ),
        ),
        storage=StorageSettings(
            retention_days=d.retention_days,
            max_log_rows=d.max_log_rows,
            backup_keep=d.backup_keep,
        ),
        listen=ListenSettings(
            listen_host=snapshot.listen.host,
            listen_port=snapshot.listen.port,
            webui_host=snapshot.webui.host,
            webui_port=snapshot.webui.port,
        ),
        restart_required_fields=list(RESTART_REQUIRED),
    )


def _sanitize(message: str, snapshot: ConfigSnapshot) -> str:
    """去掉消息里的绝对路径。

    校验失败必须告诉用户哪里写错了，但响应体不该泄露部署路径（DD_WEB §7.4）。
    """
    paths = (snapshot.source_path, snapshot.database.rules_path)
    for path in paths:
        message = message.replace(str(path), path.name)
    return message
