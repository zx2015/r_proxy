"""内存状态 → 响应模型的投影。

对应设计：docs/design/DD_WEB.md §4.1、§8.5。

`/api/health` 与 `/api/upstreams` 展示同一份健康数据，只是形状不同（前者扁平、
后者内嵌）。投影集中在这里，两个 router 就不会各自算出一套略有差别的口径——
「同一个出口在两个页面上显示不同的成功率」是最难解释的一类缺陷。
"""

from __future__ import annotations

from r_proxy.config.model import UpstreamConfig
from r_proxy.state.health import HealthTable
from r_proxy.state.sticky import StickyEntry
from r_proxy.web.schemas import (
    StickyItem,
    UpstreamHealthInfo,
    UpstreamHealthItem,
    UpstreamItem,
)


def health_info(cfg: UpstreamConfig, table: HealthTable, *, now: float) -> UpstreamHealthInfo:
    """``state_of`` 与 ``is_available`` **必须**经这里调用。

    两者都会顺带完成 ``open → half_open`` 的惰性迁移；绕过它们直接读字段会显示
    一个早已不成立的 ``open``，运维会以为出口还被拦着。
    """
    health = table.snapshot_of(cfg.name)
    state = table.state_of(cfg.name, now=now)
    attempts = health.total_success + health.total_failure
    return UpstreamHealthInfo(
        circuit_state=state.name.lower(),
        available=table.is_available(cfg.name, now=now),
        consecutive_failures=health.consecutive_failures,
        total_success=health.total_success,
        total_failure=health.total_failure,
        # 零尝试报 0.0 而不是 1.0：「还没跑过」不是「全部成功」。
        success_rate=0.0 if attempts == 0 else health.total_success / attempts,
        auth_error=health.auth_error,
        last_error=health.last_error,
        last_success_age_seconds=_age(health.last_success_at, now=now),
        bytes_up_total=health.total_bytes_up,
        bytes_down_total=health.total_bytes_down,
    )


def health_item(cfg: UpstreamConfig, table: HealthTable, *, now: float) -> UpstreamHealthItem:
    return UpstreamHealthItem(
        name=cfg.name,
        type=cfg.type,
        address=cfg.address,
        priority=cfg.priority,
        enabled=cfg.enabled,
        **health_info(cfg, table, now=now).model_dump(),
    )


def upstream_item(cfg: UpstreamConfig, table: HealthTable, *, now: float) -> UpstreamItem:
    return UpstreamItem(
        name=cfg.name,
        type=cfg.type,
        address=cfg.address,
        priority=cfg.priority,
        enabled=cfg.enabled,
        # 只报「配了没配」。用户名与密码明文永不出响应（WEBUI_SPEC §3.2）。
        has_auth=cfg.auth is not None,
        health=health_info(cfg, table, now=now),
    )


def sticky_item(entry: StickyEntry, *, now: float) -> StickyItem:
    return StickyItem(
        host=entry.host,
        upstream=entry.upstream,
        source=entry.source,
        fail_count=entry.fail_count,
        hit_count=entry.hit_count,
        last_used_age_seconds=_age(entry.last_used_at, now=now),
    )


def by_priority(upstreams: tuple[UpstreamConfig, ...]) -> list[UpstreamConfig]:
    """按候选链的实际顺序排列，界面不必自己重排。

    同优先级组内按名称，只为让输出稳定——真实轮询顺序由游标决定，展示一个
    「本次恰好的顺序」反而会让人以为它是固定的。
    """
    return sorted(upstreams, key=lambda u: (u.priority, u.name))


def _age(timestamp: float, *, now: float) -> float | None:
    """monotonic 时间戳 → 距今秒数。从未发生过（``0.0``）时返回 ``None``。

    绝对值对客户端毫无意义：monotonic 的原点是任意的。
    """
    return None if timestamp == 0.0 else max(0.0, now - timestamp)
