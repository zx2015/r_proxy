"""规则表格的读取、整表替换、校验与路由测试。

对应设计：docs/design/DD_WEB.md §6.3、§8.3，需求：WEBUI_SPEC.md §2.4、§3.4。

接口**不接受任何文件标识或路径参数**。v1 用白名单等值查找防路径穿越，v2 规则
进了库，那道防线连同它要防的攻击面一起消失了——这是把规则搬进数据库顺带得到的
安全收益。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from r_proxy.contracts import Headers
from r_proxy.decision.router import Router
from r_proxy.protocol.parse import BadRequest, parse_target
from r_proxy.rules.matcher import match
from r_proxy.storage.rules_store import RulesConflict
from r_proxy.web.config_writer import ConfigInvalid, issue_details
from r_proxy.web.deps import AppDep, Authenticated, WriterDep, client_ip
from r_proxy.web.errors import invalid_rules, revision_conflict
from r_proxy.web.schemas import (
    IssueItem,
    MatchedRule,
    RouteTestRequest,
    RouteTestResponse,
    RuleRow,
    RulesResponse,
    RulesSaveRequest,
    RulesSaveResponse,
    RulesValidateRequest,
    RulesValidateResponse,
)

router = APIRouter()


@router.get("/rules", dependencies=[Authenticated])
async def read_rules(app: AppDep, writer: WriterDep) -> RulesResponse:
    """完整规则列表与 ``revision``。客户端保存时回传 ``revision`` 做并发检测。"""
    snapshot = await writer.read_rules()
    return RulesResponse(
        revision=snapshot.revision,
        enabled=app.snapshot.rules_enabled,
        rules=[
            RuleRow(condition=condition, upstream=upstream)
            for _position, condition, upstream in snapshot.rows
        ],
    )


@router.put("/rules", dependencies=[Authenticated])
async def save_rules(
    body: RulesSaveRequest, request: Request, writer: WriterDep
) -> RulesSaveResponse:
    """校验后在单个事务里整表替换，随后热重载。校验失败时**不写库**。"""
    try:
        revision, issues = await writer.write_rules(
            [(row.condition, row.upstream) for row in body.rules],
            expected_revision=body.revision,
            actor=client_ip(request),
        )
    except RulesConflict as exc:
        raise revision_conflict(exc) from exc
    except ConfigInvalid as exc:
        raise invalid_rules(exc) from exc
    return RulesSaveResponse(
        revision=revision, issues=[IssueItem.model_validate(d) for d in issue_details(issues)]
    )


@router.post("/rules/validate", dependencies=[Authenticated])
async def validate_rules(body: RulesValidateRequest, writer: WriterDep) -> RulesValidateResponse:
    """只校验，不保存。用于界面的即时反馈。

    与保存路径共用 `validate_rules`，因此「校验说通过」与「保存能成功」口径
    一致——两份实现必然漂移，而漂移的方向总是「校验放过了保存拒绝的东西」。
    """
    rules = [(row.condition, row.upstream) for row in body.rules]
    try:
        issues = writer.validate_rules(rules)
    except ConfigInvalid as exc:
        return RulesValidateResponse(
            ok=False,
            issues=[IssueItem.model_validate(d) for d in issue_details(exc.issues)],
            rule_count=len(rules),
        )
    return RulesValidateResponse(
        ok=True,
        issues=[IssueItem.model_validate(d) for d in issue_details(issues)],
        rule_count=len(rules),
    )


@router.post("/route-test", dependencies=[Authenticated])
async def route_test(body: RouteTestRequest, app: AppDep) -> RouteTestResponse:
    """路由决策测试。

    直接调用与真实请求**同一个** `Router.build_chain`，绝不重新实现一份「用于
    测试的」决策逻辑——那必然与真实逻辑漂移，而路由测试的全部价值就在于它反映
    真实行为（[DD_WEB §8.3](../../../docs/design/DD_WEB.md)）。

    纯计算，不需要 `to_thread`：决策层无 I/O。
    """
    try:
        # 与真实请求同一个解析器，因此 host 的归一化口径也一致——否则测出来的
        # 命中结果可能与实际不同。
        target = parse_target("GET", body.url, Headers())
    except BadRequest as exc:
        raise HTTPException(400, detail=f"无法解析 URL: {exc}") from exc

    decision = Router().build_chain(
        target, app.snapshot, app.rules, app.state, now=time.monotonic()
    )
    hit = match(app.rules, target)
    return RouteTestResponse(
        url=body.url,
        host=target.host,
        port=target.port,
        decision=decision.source,
        upstream=decision.chain[0] if decision.chain else None,
        matched_rule=(
            None if hit is None else MatchedRule(position=hit.position, condition=hit.condition)
        ),
        candidate_chain=list(decision.chain),
    )
