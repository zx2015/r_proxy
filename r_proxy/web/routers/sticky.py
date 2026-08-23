"""粘性映射与路由级负面记忆接口。

对应设计：docs/design/DD_WEB.md §4.1，需求：WEBUI_SPEC.md §2.3、§3.3。

两者的权威都在**内存**里，因此这里既不读 `state.db` 也不经 `to_thread`：读库只
会拿到滞后的副本，而界面要显示的是当前真实生效的状态。变更走「先改内存、再入
队落盘」——顺序反过来会出现「界面显示已改、下一个请求仍用旧绑定」。

例外是「固化为规则」（§4.9 / DD_WEB §8.9）：它要写 `rules.db`，因此把读改写整个
交给 `ConfigWriter`——规则库的唯一写者是它，这里不自己开库。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from r_proxy.contracts import AddressFamily, Method, RequestTarget
from r_proxy.protocol.parse import normalize_host
from r_proxy.rules.matcher import match
from r_proxy.state.sticky import StickyEntry
from r_proxy.storage.queue import (
    config_audit,
    route_block_delete,
    sticky_delete,
    sticky_manual_upsert,
)
from r_proxy.storage.rules_store import RulesConflict
from r_proxy.web import views
from r_proxy.web.config_writer import ConfigInvalid, RuleExists, issue_details
from r_proxy.web.deps import AppDep, Authenticated, WriterDep, client_ip
from r_proxy.web.errors import invalid_rules, revision_conflict, rule_exists
from r_proxy.web.schemas import (
    ClearedResponse,
    IssueItem,
    MatchedRule,
    Pagination,
    RouteBlockItem,
    RouteBlockPage,
    StickyBindRequest,
    StickyClearRequest,
    StickyItem,
    StickyPage,
    StickyPromoteRequest,
    StickyPromoteResponse,
    StickyQuery,
)

if TYPE_CHECKING:
    from r_proxy.app import Application

router = APIRouter()

StickyQueryDep = Annotated[StickyQuery, Depends()]
PageDep = Annotated[Pagination, Depends()]
# host 出现在路径里，必须限长：它会成为内存字典的键。
HostPath = Annotated[str, Path(min_length=1, max_length=255)]


@router.get("/sticky", dependencies=[Authenticated])
async def list_sticky(params: StickyQueryDep, app: AppDep) -> StickyPage:
    now = time.monotonic()
    entries = [e for e in app.state.sticky.entries() if _matches(e, params)]
    entries.sort(key=_sort_key(params.sort), reverse=True)
    window = entries[params.offset : params.offset + params.page_size]
    return StickyPage(
        items=[views.sticky_item(e, now=now) for e in window],
        page=params.page,
        page_size=params.page_size,
        total=len(entries),
    )


@router.put("/sticky/{host}", dependencies=[Authenticated])
async def bind_sticky(
    host: HostPath, body: StickyBindRequest, request: Request, app: AppDep
) -> StickyItem:
    """手动把 host 绑到指定出口，``source`` 置为 ``manual``。"""
    # 必须与代理侧同一个归一化函数：键不一致时绑定看起来成功了，却永远不会被
    # 真实流量命中（DD_WEB §8.6、DD_ROUTING §7.6）。
    key = normalize_host(host)
    if not key:
        raise HTTPException(400, detail="host 不能为空")
    cfg = app.snapshot.upstream(body.upstream)
    if cfg is None:
        raise HTTPException(
            400, detail={"code": "UPSTREAM_NOT_FOUND", "message": f"出口不存在：{body.upstream}"}
        )
    if not cfg.enabled:
        # 禁用的出口不进候选链，绑上去等于什么都没发生——这种「设置了但不生效」
        # 比直接报错难查得多。
        raise HTTPException(
            409,
            detail={"code": "UPSTREAM_DISABLED", "message": f"出口已禁用：{body.upstream}"},
        )

    previous = app.state.sticky.get(key)
    before = previous.upstream if previous is not None else None
    now = time.monotonic()
    app.state.sticky.bind_manual(key, cfg.name, now=now)
    app.storage.queue.put(
        sticky_manual_upsert(host=key, upstream=cfg.name, now_unix=int(time.time()))
    )
    _audit(app, request, action="bind_sticky", target=key, diff=f"{before} -> {cfg.name}")

    entry = app.state.sticky.get(key)
    # 刚写进去就取不到，只可能是容量为 0（sticky_cache_size = 0 关闭了粘性）。
    if entry is None:
        raise HTTPException(409, detail={"code": "STICKY_DISABLED", "message": "粘性缓存已关闭"})
    return views.sticky_item(entry, now=now)


@router.post("/sticky/{host}/promote", dependencies=[Authenticated])
async def promote_sticky(
    host: HostPath, body: StickyPromoteRequest, request: Request, app: AppDep, writer: WriterDep
) -> StickyPromoteResponse:
    """把这条粘性映射固化成一条规则（DD_WEB §8.9）。

    规则与粘性不是「更持久」与「不那么持久」的关系：命中规则的候选链长度恒为
    1、失败原样返回，而粘性只是把某个出口提到链首、失败照常切换。固化因此是一次
    语义变更，界面必须在确认前说清这一点。

    规则生效后这条粘性再也不会被读到（命中规则会短路掉粘性），留着只会显示一个
    不再变化的命中数，所以顺带清掉。清除放在写库成功**之后**：反过来会在校验
    失败时白丢一条有用的绑定。
    """
    key = normalize_host(host)
    if not key:
        raise HTTPException(400, detail="host 不能为空")
    cfg = app.snapshot.upstream(body.upstream)
    if cfg is None:
        raise HTTPException(
            400, detail={"code": "UPSTREAM_NOT_FOUND", "message": f"出口不存在：{body.upstream}"}
        )
    if not cfg.enabled:
        # 规则指向禁用出口在路由层是死路：链上没有顺延余地，直接 502。校验只查
        # 出口是否存在，因此这一条得在这里拦。
        raise HTTPException(
            409,
            detail={"code": "UPSTREAM_DISABLED", "message": f"出口已禁用：{body.upstream}"},
        )

    # 固化前该 host 命中的规则。要在写库前算：写完热重载后命中的必然是新规则。
    previous = match(app.rules, _probe(key))
    try:
        position, revision, issues = await writer.insert_rule(
            body.condition, cfg.name, actor=client_ip(request), target=key
        )
    except RuleExists as exc:
        raise rule_exists(exc) from exc
    except ConfigInvalid as exc:
        raise invalid_rules(exc) from exc
    except RulesConflict as exc:
        raise revision_conflict(exc) from exc

    cleared = app.state.sticky.clear(key)
    if cleared:
        app.storage.queue.put(sticky_delete(host=key))
    return StickyPromoteResponse(
        position=position,
        revision=revision,
        rules_enabled=app.snapshot.rules_enabled,
        sticky_cleared=cleared,
        previous_match=(
            None
            if previous is None
            else MatchedRule(position=previous.position, condition=previous.condition)
        ),
        issues=[IssueItem.model_validate(d) for d in issue_details(issues)],
    )


def _probe(host: str) -> RequestTarget:
    """匹配器的输入。只有 ``host`` 参与匹配，其余字段取占位值。

    端口与方法不进匹配是 v2 的语义（只匹配主机名），因此这里不需要知道用户
    实际访问的是哪个端口——粘性键里本来也没有端口。
    """
    return RequestTarget(
        host=host,
        port=443,
        method=Method.CONNECT,
        url=None,
        is_connect=True,
        family=AddressFamily.of_literal(host),
    )


@router.delete("/sticky/{host}", dependencies=[Authenticated])
async def clear_sticky(host: HostPath, request: Request, app: AppDep) -> ClearedResponse:
    key = normalize_host(host)
    if not app.state.sticky.clear(key):
        raise HTTPException(404, detail="该 host 没有粘性映射")
    app.storage.queue.put(sticky_delete(host=key))
    _audit(app, request, action="clear_sticky", target=key, diff=None)
    return ClearedResponse(cleared=1)


@router.delete("/sticky", dependencies=[Authenticated])
async def clear_sticky_batch(
    body: StickyClearRequest, request: Request, app: AppDep
) -> ClearedResponse:
    """按出口或 host 列表批量清除。

    两个条件都为空时**拒绝**而不是清空全部：一次手滑的空请求不该把整份路由
    记忆抹掉。要清空全部必须显式列出 host。
    """
    if body.upstream is None and not body.hosts:
        raise HTTPException(400, detail="必须指定 upstream 或 hosts")

    sticky = app.state.sticky
    if body.upstream is not None:
        targets = [e.host for e in sticky.entries() if e.upstream == body.upstream]
    else:
        targets = [normalize_host(h) for h in (body.hosts or [])]

    cleared = 0
    for key in targets:
        if sticky.clear(key):
            app.storage.queue.put(sticky_delete(host=key))
            cleared += 1
    _audit(
        app,
        request,
        action="clear_sticky_batch",
        target=body.upstream or f"{len(targets)} hosts",
        diff=f"cleared={cleared}",
    )
    return ClearedResponse(cleared=cleared)


@router.get("/route-blocks", dependencies=[Authenticated])
async def list_route_blocks(params: PageDep, app: AppDep) -> RouteBlockPage:
    """``(host, upstream)`` 负面记忆。已过期的不返回。"""
    now = time.monotonic()
    blocks = app.state.memory.entries(now=now)
    blocks.sort(key=lambda b: b.blocked_until, reverse=True)
    window = blocks[params.offset : params.offset + params.page_size]
    return RouteBlockPage(
        items=[
            RouteBlockItem(
                host=b.host,
                upstream=b.upstream,
                fail_count=b.fail_count,
                reason=b.last_reason,
                expires_in_seconds=max(0.0, b.blocked_until - now),
            )
            for b in window
        ],
        page=params.page,
        page_size=params.page_size,
        total=len(blocks),
    )


@router.delete("/route-blocks/{host}/{upstream}", dependencies=[Authenticated])
async def clear_route_block(
    host: HostPath, upstream: str, request: Request, app: AppDep
) -> ClearedResponse:
    """解除屏蔽，让该出口立即重新参与这个 host 的候选。"""
    key = normalize_host(host)
    if not app.state.memory.clear(key, upstream):
        raise HTTPException(404, detail="没有对应的负面记忆")
    app.storage.queue.put(route_block_delete(host=key, upstream=upstream))
    _audit(app, request, action="clear_route_block", target=f"{key}/{upstream}", diff=None)
    return ClearedResponse(cleared=1)


def _matches(entry: StickyEntry, params: StickyQuery) -> bool:
    if params.upstream is not None and entry.upstream != params.upstream:
        return False
    return not params.q or params.q.lower() in entry.host


def _sort_key(sort: str) -> Callable[[StickyEntry], tuple[float, float]]:
    """次级键让同值条目的顺序稳定，翻页时不会来回跳。"""
    if sort == "fails":
        return lambda e: (float(e.fail_count), e.last_used_at)
    return lambda e: (e.last_used_at, float(e.fail_count))


def _audit(
    app: Application, request: Request, *, action: str, target: str, diff: str | None
) -> None:
    """运维动作一律留痕：「这个 host 为什么走了那个出口」要能追溯到人。"""
    app.storage.queue.put(
        config_audit(
            actor=client_ip(request),
            action=action,
            target=target,
            diff=diff,
            version_before=app.snapshot.config_version,
            version_after=app.snapshot.config_version,
            now_unix=int(time.time()),
        )
    )
