"""切换判据：这次失败值不值得换个出口再试。

对应设计：docs/design/DD_SWITCHING.md §3。

判据不决定换成谁（那是 :mod:`r_proxy.decision.router`），不发起连接，
不访问任何状态存储。
"""

from __future__ import annotations

from r_proxy.config.model import RoutingConfig
from r_proxy.contracts import FailureKind
from r_proxy.decision.classify import Origin, StatusCategory, classify_status, determine_origin
from r_proxy.decision.limiter import SwitchRateLimiter
from r_proxy.decision.model import (
    AttemptOutcome,
    KeepReason,
    SwitchContext,
    SwitchReason,
    SwitchVerdict,
)

_REASON_BY_CATEGORY = {
    StatusCategory.PROXY_LAYER: SwitchReason.PROXY_LAYER_STATUS,
    StatusCategory.AMBIGUOUS: SwitchReason.AMBIGUOUS_STATUS,
    StatusCategory.EGRESS_RELATED: SwitchReason.EGRESS_RELATED_STATUS,
    StatusCategory.INCOMPLETE_REQUEST: SwitchReason.INCOMPLETE_REQUEST,
}


class SwitchPolicy:
    def should_switch(
        self,
        outcome: AttemptOutcome,
        ctx: SwitchContext,
        cfg: RoutingConfig,
        *,
        now: float,
        limiter: SwitchRateLimiter,
    ) -> SwitchVerdict:
        """依次通过三道判据，任一否决即不切换。

        ``limiter`` 单独传入而不是塞进 ``ctx``：它是唯一的可变依赖，
        放在冻结的上下文里会掩盖「这次调用可能消耗配额」这个副作用。
        """
        # 判据一：失败层次。没有状态码时后续的分类与来源判定全都不适用。
        #
        # 但「没有状态码」本身不能豁免幂等门控：TCP/DNS 层面就失败（连接被拒、
        # 超时、RST）时请求字节还没发出去，任何方法重投都是安全的——这是
        # ``request_sent=False`` 的情形。可一旦请求（含非幂等的 POST 请求体）
        # 已经完整写到这个出口的 socket 上，后续无论是等响应超时、连接被对端
        # 悄悄断开，还是握手中途失败，都不再是「没发生过」：出口很可能已经
        # 收到并可能已经处理了这次调用。此时仍无条件切换，会把同一个 POST
        # 重放到下一个出口，等价于让一次下单/扣款类调用被悄悄执行两次——这
        # 正是 AGENTS.md/CLAUDE.md 明确列为红线的「非幂等方法已发出后不得重试」。
        # 检查顺序与判据三保持一致：response_started 是更强的约束，排在前面。
        if outcome.status is None:
            if ctx.response_started:
                return SwitchVerdict(
                    switch=False,
                    keep_reason=KeepReason.RESPONSE_STARTED,
                    failure_kind=outcome.kind,
                )
            if ctx.request_sent and not ctx.method.idempotent:
                return SwitchVerdict(
                    switch=False,
                    keep_reason=KeepReason.NON_IDEMPOTENT,
                    failure_kind=outcome.kind,
                )
            return SwitchVerdict(
                switch=True,
                switch_reason=SwitchReason.TRANSPORT_FAILURE,
                failure_kind=outcome.kind,
            )

        status = outcome.status

        # 408 的空闲连接回收特例必须先于分类：408 在分类表中属于「切换」，
        # 放在分类之后就永远走不到这个第三态。
        if status == 408 and not ctx.request_sent:
            return SwitchVerdict(
                switch=False,
                retry_same_upstream=True,
                keep_reason=KeepReason.IDLE_CONNECTION_RECYCLED,
                failure_kind=FailureKind.NOT_A_FAILURE,
            )

        # 判据二：状态码与来源。
        category = classify_status(status)
        hard_keep = _hard_keep_reason(category)
        if hard_keep is not None:
            # 硬约束：即便用户把 521 加进 switch_on_status 也不切换。
            return SwitchVerdict(
                switch=False, keep_reason=hard_keep, failure_kind=FailureKind.NOT_A_FAILURE
            )
        if status not in cfg.switch_on_status:
            return SwitchVerdict(
                switch=False,
                keep_reason=KeepReason.TARGET_HANDLED,
                failure_kind=FailureKind.NOT_A_FAILURE,
            )

        if category is StatusCategory.AMBIGUOUS:
            origin = determine_origin(outcome.response_headers, is_connect=ctx.is_connect)
            if origin is Origin.TARGET:
                return SwitchVerdict(
                    switch=False,
                    keep_reason=KeepReason.STATUS_FROM_TARGET,
                    failure_kind=FailureKind.NOT_A_FAILURE,
                )

        # 407 是出口自身的凭据问题，计入全局熔断；其余状态码只怪这条路由。
        kind = FailureKind.UPSTREAM_ERROR if status == 407 else FailureKind.ROUTE_ERROR

        # 判据三：幂等性、可重放性、频率。归类与是否切换正交——以下每条
        # 都保留 kind，让「不切换但记失败」成立（PRD §4.3.4）。
        if ctx.response_started:
            return SwitchVerdict(
                switch=False, keep_reason=KeepReason.RESPONSE_STARTED, failure_kind=kind
            )
        if ctx.request_sent and not ctx.method.idempotent:
            return SwitchVerdict(
                switch=False, keep_reason=KeepReason.NON_IDEMPOTENT, failure_kind=kind
            )
        if not ctx.replayable:
            return SwitchVerdict(
                switch=False, keep_reason=KeepReason.NOT_REPLAYABLE, failure_kind=kind
            )
        # 限流放在最后：前置判据已否决的请求不消耗配额。
        if not limiter.try_consume(ctx.host, now=now, cfg=cfg.status_switch_rate_limit):
            return SwitchVerdict(
                switch=False, keep_reason=KeepReason.RATE_LIMITED, failure_kind=kind
            )

        return SwitchVerdict(
            switch=True,
            switch_reason=_REASON_BY_CATEGORY.get(category, SwitchReason.CONFIGURED_STATUS),
            failure_kind=kind,
        )


def _hard_keep_reason(category: StatusCategory) -> KeepReason | None:
    """不受 ``switch_on_status`` 影响的两类。"""
    if category is StatusCategory.TARGET_HANDLED:
        return KeepReason.TARGET_HANDLED
    if category is StatusCategory.CDN_ORIGIN_ERROR:
        return KeepReason.CDN_ORIGIN_ERROR
    return None
