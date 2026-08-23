"""上级代理（出口）接口。

对应设计：docs/design/DD_WEB.md §7.3、§7.4、§8.1、§8.2，需求：WEBUI_SPEC.md §2.2、§3.2。

增删改都要写回 `config.toml`，因此一律经 :class:`ConfigWriter`：它负责版本比对、
候选校验、备份与原子写。本模块只做「意图 → 文本变换」的翻译和请求级校验。
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request

from r_proxy.rules.model import Rule
from r_proxy.web import probe as probing
from r_proxy.web import toml_edit, views
from r_proxy.web.config_writer import ConfigConflict, ConfigInvalid
from r_proxy.web.deps import AppDep, Authenticated, WriterDep, client_ip
from r_proxy.web.errors import invalid_config, version_conflict
from r_proxy.web.schemas import (
    ConfigVersionResponse,
    PriorityGroupsRequest,
    ProbeResult,
    UpstreamCreateRequest,
    UpstreamsResponse,
    UpstreamUpdateRequest,
)

router = APIRouter()

NamePath = Annotated[str, Path(min_length=1, max_length=64)]
# DELETE 不宜带请求体，版本号只能走 query。
VersionQuery = Annotated[str, Query(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")]

# 拖拽重排的步长按组数选，避免撞上 999 上限（WEBUI_SPEC §2.2）。
PRIORITY_MAX = 999
_STEPS = ((99, 10), (199, 5))


@router.get("/upstreams", dependencies=[Authenticated])
async def list_upstreams(app: AppDep) -> UpstreamsResponse:
    """出口列表，按优先级升序。凭据只以 ``has_auth`` 体现。"""
    now = time.monotonic()
    table = app.state.health
    return UpstreamsResponse(
        upstreams=[
            views.upstream_item(cfg, table, now=now)
            for cfg in views.by_priority(app.snapshot.upstreams)
        ]
    )


@router.post("/upstreams", status_code=201, dependencies=[Authenticated])
async def create_upstream(
    body: UpstreamCreateRequest, request: Request, app: AppDep, writer: WriterDep
) -> ConfigVersionResponse:
    if app.snapshot.upstream(body.name) is not None:
        raise HTTPException(
            409, detail={"code": "UPSTREAM_EXISTS", "message": f"出口已存在: {body.name}"}
        )
    fields: dict[str, object] = {
        "type": body.type,
        "address": body.address,
        "priority": body.priority,
        "enabled": body.enabled,
    }
    return await _write(
        writer,
        lambda text: toml_edit.upsert_upstream(text, name=body.name, fields=fields),
        version=body.config_version,
        actor=client_ip(request),
        action="upstream.create",
        target=body.name,
    )


@router.put("/upstreams/priorities", dependencies=[Authenticated])
async def set_priorities(
    body: PriorityGroupsRequest, request: Request, app: AppDep, writer: WriterDep
) -> ConfigVersionResponse:
    """按拖拽后的分组顺序重算优先级。

    **批量原子**：先算出全部数值，一次文本变换、一次写入。逐个 `PUT` 会在中途
    失败时留下一个乱序的中间状态。

    必须声明在 `/upstreams/{name}` **之前**：FastAPI 按注册顺序匹配，反过来的话
    `priorities` 会被当成出口名，请求体也就对不上模型（`422`）。
    """
    priorities = _reassign(body.groups, known={u.name for u in app.snapshot.upstreams})
    return await _write(
        writer,
        lambda text: toml_edit.set_priorities(text, priorities),
        version=body.config_version,
        actor=client_ip(request),
        action="upstream.priorities",
        target=",".join(priorities),
    )


@router.put("/upstreams/{name}", dependencies=[Authenticated])
async def update_upstream(
    name: NamePath,
    body: UpstreamUpdateRequest,
    request: Request,
    app: AppDep,
    writer: WriterDep,
) -> ConfigVersionResponse:
    """改地址、优先级、启用状态。

    只写本次提交的字段：`[upstreams.auth]` 与表内注释必须原样保留，否则「改一下
    优先级」会顺手把上级代理的凭据抹掉。
    """
    if app.snapshot.upstream(name) is None:
        raise HTTPException(404, detail="出口不存在")
    fields = {
        key: value
        for key, value in body.model_dump(exclude_unset=True).items()
        if key != "config_version"
    }
    if not fields:
        raise HTTPException(400, detail="没有要修改的字段")
    return await _write(
        writer,
        lambda text: toml_edit.upsert_upstream(text, name=name, fields=fields),
        version=body.config_version,
        actor=client_ip(request),
        action="upstream.update",
        target=name,
    )


@router.delete("/upstreams/{name}", dependencies=[Authenticated])
async def delete_upstream(
    name: NamePath,
    config_version: VersionQuery,
    request: Request,
    app: AppDep,
    writer: WriterDep,
) -> ConfigVersionResponse:
    """删除出口。被规则引用时拒绝，并给出**每一处**引用位置。

    回「被引用」而不回位置，用户还要自己逐行找；回规则序号与条件可以直接跳到
    那一行（[DD_WEB §8.1](../../../docs/design/DD_WEB.md)）。位置全部放在
    `details`：`message` 只说有多少处，既避免与界面拼接后重复，也不会让第一处
    看起来像是唯一的阻碍。
    """
    if app.snapshot.upstream(name) is None:
        raise HTTPException(404, detail="出口不存在")
    if refs := [r for r in app.rules.rules if r.target == name]:
        raise HTTPException(
            409,
            detail={
                "code": "UPSTREAM_IN_USE",
                "message": f"无法删除 {name}：仍被 {len(refs)} 条规则引用",
                "details": _reference_details(refs),
            },
        )
    return await _write(
        writer,
        lambda text: toml_edit.remove_upstream(text, name),
        version=config_version,
        actor=client_ip(request),
        action="upstream.delete",
        target=name,
    )


@router.post("/upstreams/{name}/test", dependencies=[Authenticated])
async def test_upstream(name: NamePath, app: AppDep) -> ProbeResult:
    """经指定出口做一次连通性探测。

    **只接受已配置的出口名**，探测目标由服务端决定（DD_WEB §7.3）。想测一个新
    地址就得先把它加进配置——而那是一个需要认证并留审计的操作。
    """
    cfg = app.snapshot.upstream(name)
    if cfg is None:
        raise HTTPException(404, detail="出口不存在")
    return await probing.probe(cfg)


def _reassign(groups: list[list[str]], *, known: set[str]) -> dict[str, int]:
    """分组顺序 → 每个出口的优先级数值。

    要求**每个已配置的出口恰好出现一次**：漏掉一个就会留着旧数值，与新顺序不
    自洽；重复出现则无法确定它属于哪一组。两种情况都在这里拒绝，一个字节都不写。
    """
    seen: list[str] = [name for group in groups for name in group]
    if len(seen) != len(set(seen)):
        raise HTTPException(400, detail="同一个出口不能出现在多个分组里")
    if unknown := sorted(set(seen) - known):
        raise HTTPException(400, detail=f"出口不存在: {', '.join(unknown)}")
    if missing := sorted(known - set(seen)):
        raise HTTPException(400, detail=f"缺少出口: {', '.join(missing)}")
    if any(not group for group in groups):
        raise HTTPException(400, detail="分组不能为空")

    step = _step_for(len(groups))
    if len(groups) * step > PRIORITY_MAX:
        # 不静默截断：两个本应不同优先级的组被压成同一个数值会变成轮询组，
        # 悄悄改变路由行为（WEBUI_SPEC §2.2）。
        raise HTTPException(400, detail=f"分组过多，优先级会超出 {PRIORITY_MAX}，请手动整理")
    return {name: (index + 1) * step for index, group in enumerate(groups) for name in group}


def _step_for(groups: int) -> int:
    """组数越多步长越小。留间隔是为了后续插入新组时不必重排全部。"""
    for limit, step in _STEPS:
        if groups <= limit:
            return step
    return 1


def _reference_details(refs: list[Rule]) -> list[dict[str, object]]:
    return [{"position": r.position, "condition": r.raw} for r in refs]


async def _write(
    writer: WriterDep,
    transform: toml_edit.Transform,
    *,
    version: str,
    actor: str,
    action: str,
    target: str,
) -> ConfigVersionResponse:
    """把写入过程中的三类失败翻译成 HTTP 语义。

    `TomlEditError` 归 `400`：它意味着配置文件的结构与预期不符（比如 upstreams
    不是表数组），属于请求无法在当前文件上完成，而不是服务端故障。
    """
    try:
        new_version = await writer.edit_config(
            transform, expected_version=version, actor=actor, action=action, target=target
        )
    except ConfigConflict as exc:
        raise version_conflict(exc) from exc
    except ConfigInvalid as exc:
        raise invalid_config(exc) from exc
    except toml_edit.TomlEditError as exc:
        raise HTTPException(400, detail={"code": "CONFIG_SHAPE", "message": str(exc)}) from exc
    return ConfigVersionResponse(config_version=new_version)
