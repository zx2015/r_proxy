"""decision/switching.py 的三道判据测试。

对应设计：docs/design/DD_SWITCHING.md §3。

判据顺序不可调换，每条顺序约束都有对应的测试（见 TestGateOrdering）。
"""

from __future__ import annotations

import pytest

from r_proxy.config.model import RateLimitConfig, RoutingConfig
from r_proxy.contracts import FailureKind, Headers, Method
from r_proxy.decision.limiter import SwitchRateLimiter
from r_proxy.decision.model import (
    AttemptOutcome,
    KeepReason,
    SwitchContext,
    SwitchReason,
)
from r_proxy.decision.switching import SwitchPolicy

ROUTING = RoutingConfig(status_switch_rate_limit=RateLimitConfig(max_switches_per_host=3))


def outcome(
    *,
    status: int | None = None,
    kind: FailureKind = FailureKind.ROUTE_ERROR,
    headers: Headers | None = None,
    error: str | None = None,
) -> AttemptOutcome:
    return AttemptOutcome(
        upstream="proxy",
        ok=False,
        status=status,
        error=error,
        kind=kind,
        response_headers=headers if headers is not None else Headers(),
    )


def context(
    *,
    method: Method = Method.GET,
    is_connect: bool = False,
    request_sent: bool = True,
    replayable: bool = True,
    response_started: bool = False,
    host: str = "example.com",
    client_body_timeout: bool = False,
) -> SwitchContext:
    return SwitchContext(
        method=method,
        is_connect=is_connect,
        request_sent=request_sent,
        replayable=replayable,
        response_started=response_started,
        host=host,
        client_body_timeout=client_body_timeout,
    )


@pytest.fixture
def policy() -> SwitchPolicy:
    return SwitchPolicy()


@pytest.fixture
def limiter() -> SwitchRateLimiter:
    return SwitchRateLimiter()


def decide(
    policy: SwitchPolicy,
    limiter: SwitchRateLimiter,
    out: AttemptOutcome,
    ctx: SwitchContext,
    *,
    cfg: RoutingConfig = ROUTING,
    now: float = 0.0,
):  # type: ignore[no-untyped-def]
    return policy.should_switch(out, ctx, cfg, now=now, limiter=limiter)


class TestGateOneTransportFailure:
    def test_no_status_means_switch(self, policy: SwitchPolicy, limiter: SwitchRateLimiter) -> None:
        """传输层失败（超时/RST/DNS）任何方法均可切换。"""
        verdict = decide(policy, limiter, outcome(error="TimeoutError"), context())
        assert verdict.switch is True
        assert verdict.switch_reason is SwitchReason.TRANSPORT_FAILURE

    def test_transport_failure_adopts_the_egress_classification(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """errno 级归类由 egress 层做，判据原样采纳。"""
        verdict = decide(
            policy,
            limiter,
            outcome(kind=FailureKind.CAPABILITY_MISMATCH, error="ENETUNREACH"),
            context(),
        )
        assert verdict.failure_kind is FailureKind.CAPABILITY_MISMATCH

    def test_non_idempotent_post_still_switches_before_bytes_are_sent(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """TCP 阶段就失败时字节没发出去，POST 重投是安全的。"""
        verdict = decide(
            policy,
            limiter,
            outcome(error="ECONNREFUSED"),
            context(method=Method.POST, request_sent=False),
        )
        assert verdict.switch is True

    def test_transport_failure_is_not_rate_limited(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-15：限流只管状态码触发的切换，不能削弱链路故障的自愈。"""
        for _ in range(20):
            verdict = decide(policy, limiter, outcome(error="TimeoutError"), context())
            assert verdict.switch is True

    def test_transport_failure_ignores_replayability(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        verdict = decide(policy, limiter, outcome(error="TimeoutError"), context(replayable=False))
        assert verdict.switch is True

    def test_non_idempotent_post_does_not_switch_after_bytes_are_sent(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """请求（含非幂等的 POST 请求体）已完整发出后再遇到传输层失败
        （等响应超时、连接被对端悄悄断开等），不能再无条件切换——出口可能
        已经收到并处理了这次调用，重投等价于让下单/扣款类调用被执行两次。

        与 ``test_non_idempotent_post_still_switches_before_bytes_are_sent``
        对照：区别只在 ``request_sent``，划清「安全重投」与「危险重投」的界线。
        """
        verdict = decide(
            policy,
            limiter,
            outcome(error="TimeoutError"),
            context(method=Method.POST, request_sent=True),
        )
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.NON_IDEMPOTENT

    def test_transport_failure_after_response_started_never_switches(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """``response_started`` 比幂等门控更强：即便方法幂等也不能再切换。"""
        verdict = decide(
            policy,
            limiter,
            outcome(error="TimeoutError"),
            context(method=Method.GET, request_sent=True, response_started=True),
        )
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.RESPONSE_STARTED

    def test_transport_failure_before_send_still_switches_for_idempotent(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """请求已发出但方法幂等（如 GET）时，传输层失败仍可切换。"""
        verdict = decide(
            policy,
            limiter,
            outcome(error="TimeoutError"),
            context(method=Method.GET, request_sent=True),
        )
        assert verdict.switch is True

    def test_client_body_timeout_does_not_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """客户端读 body 超时：换哪个出口都等不到客户端补发数据，换出口只会
        让每个出口各自重复同一次超时，因此必须直接终止候选链。"""
        verdict = decide(
            policy,
            limiter,
            outcome(error="TimeoutError"),
            context(request_sent=False, client_body_timeout=True),
        )
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.CLIENT_BODY_TIMEOUT

    def test_client_body_timeout_is_distinct_from_replayability(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """``switch_buffer_bytes: 0`` 时 ``replayable`` 从一开始就是 False，
        但传输层失败（字节还没发出）仍要能切换（
        ``test_transport_failure_ignores_replayability``）。`client_body_timeout`
        必须是独立信号，不能靠复用 `replayable` 来实现，否则会把那条既有
        规则连带破坏。"""
        verdict = decide(
            policy,
            limiter,
            outcome(error="TimeoutError"),
            context(replayable=False, client_body_timeout=False),
        )
        assert verdict.switch is True


class TestGateTwoStatusAndOrigin:
    @pytest.mark.parametrize("status", [404, 500, 400, 401, 405, 410, 422, 501])
    def test_target_handled_never_switches(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter, status: int
    ) -> None:
        """M2-03、M2-04：目标已经处理了请求，换出口只会得到同样的回答。"""
        verdict = decide(policy, limiter, outcome(status=status), context())
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.TARGET_HANDLED
        assert verdict.failure_kind is FailureKind.NOT_A_FAILURE

    @pytest.mark.parametrize("status", list(range(520, 527)))
    def test_cloudflare_origin_errors_neither_switch_nor_count(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter, status: int
    ) -> None:
        """M2-05：520–526 证明出口是通的。"""
        verdict = decide(policy, limiter, outcome(status=status), context())
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.CDN_ORIGIN_ERROR
        assert verdict.failure_kind is FailureKind.NOT_A_FAILURE

    def test_cdn_error_stays_unswitchable_even_if_configured(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-06：用户把 521 写进 switch_on_status 也不切换，这是硬约束。"""
        cfg = RoutingConfig(switch_on_status=frozenset({521, 503}))
        verdict = decide(policy, limiter, outcome(status=521), context(), cfg=cfg)
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.CDN_ORIGIN_ERROR

    def test_target_handled_stays_unswitchable_even_if_configured(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        cfg = RoutingConfig(switch_on_status=frozenset({404, 500}))
        for status in (404, 500):
            verdict = decide(policy, limiter, outcome(status=status), context(), cfg=cfg)
            assert verdict.switch is False, status

    def test_status_outside_switch_on_status_does_not_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        cfg = RoutingConfig(switch_on_status=frozenset({503}))
        verdict = decide(policy, limiter, outcome(status=403), context(), cfg=cfg)
        assert verdict.switch is False

    def test_unknown_status_does_not_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """未知状态码大概率是目标应用的自定义码。"""
        verdict = decide(policy, limiter, outcome(status=418), context())
        assert verdict.switch is False

    def test_configured_unknown_status_switches_and_says_so(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """分类表不认识它，切换原因不能冒充 AMBIGUOUS——日志会误导排查。"""
        cfg = RoutingConfig(switch_on_status=frozenset({418}))
        verdict = decide(policy, limiter, outcome(status=418), context(), cfg=cfg)
        assert verdict.switch is True
        assert verdict.switch_reason is SwitchReason.CONFIGURED_STATUS

    def test_connect_503_is_from_the_proxy_and_switches(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-07：CONNECT 的非 2xx 必然来自上级代理。"""
        verdict = decide(
            policy, limiter, outcome(status=503), context(is_connect=True, method=Method.CONNECT)
        )
        assert verdict.switch is True
        assert verdict.switch_reason is SwitchReason.AMBIGUOUS_STATUS

    def test_http_503_from_squid_switches(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        verdict = decide(
            policy,
            limiter,
            outcome(status=503, headers=Headers([("X-Squid-Error", "ERR_CONNECT_FAIL")])),
            context(),
        )
        assert verdict.switch is True

    def test_http_503_from_nginx_does_not_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-08：目标自己的网关坏了，换出口无用。"""
        verdict = decide(
            policy, limiter, outcome(status=503, headers=Headers([("Server", "nginx")])), context()
        )
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.STATUS_FROM_TARGET
        assert verdict.failure_kind is FailureKind.NOT_A_FAILURE

    def test_http_503_without_signals_switches(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """判定不出时切换：多试一次的代价远小于本可恢复却直接失败。"""
        verdict = decide(policy, limiter, outcome(status=503), context())
        assert verdict.switch is True

    @pytest.mark.parametrize("status", [403, 429, 451])
    def test_egress_related_statuses_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter, status: int
    ) -> None:
        verdict = decide(policy, limiter, outcome(status=status), context())
        assert verdict.switch is True
        assert verdict.switch_reason is SwitchReason.EGRESS_RELATED_STATUS

    def test_407_switches_and_blames_the_upstream(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """凭据错误是出口自身的问题，要计入熔断。"""
        verdict = decide(policy, limiter, outcome(status=407), context())
        assert verdict.switch is True
        assert verdict.failure_kind is FailureKind.UPSTREAM_ERROR
        assert verdict.switch_reason is SwitchReason.PROXY_LAYER_STATUS

    def test_511_switches(self, policy: SwitchPolicy, limiter: SwitchRateLimiter) -> None:
        verdict = decide(policy, limiter, outcome(status=511), context())
        assert verdict.switch is True

    def test_503_from_upstream_is_a_route_error_not_upstream_error(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """代理返回 503 说明它活着，只是到不了目标。"""
        verdict = decide(policy, limiter, outcome(status=503), context(is_connect=True))
        assert verdict.failure_kind is FailureKind.ROUTE_ERROR


class TestIdleConnectionRecycled:
    def test_408_before_the_request_was_sent_retries_the_same_upstream(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """复用的空闲连接被服务端回收：既不切换也不放弃。"""
        verdict = decide(policy, limiter, outcome(status=408), context(request_sent=False))
        assert verdict.switch is False
        assert verdict.retry_same_upstream is True
        assert verdict.keep_reason is KeepReason.IDLE_CONNECTION_RECYCLED
        assert verdict.failure_kind is FailureKind.NOT_A_FAILURE

    def test_408_after_the_request_was_sent_switches(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        cfg = RoutingConfig(switch_on_status=frozenset({408}))
        verdict = decide(policy, limiter, outcome(status=408), context(), cfg=cfg)
        assert verdict.switch is True
        assert verdict.failure_kind is FailureKind.ROUTE_ERROR

    def test_408_special_case_precedes_classification(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """408 在分类表里属于「切换」，特例必须先判，否则永远走不到第三态。"""
        cfg = RoutingConfig(switch_on_status=frozenset({408}))
        verdict = decide(policy, limiter, outcome(status=408), context(request_sent=False), cfg=cfg)
        assert verdict.retry_same_upstream is True

    def test_idle_recycle_does_not_consume_rate_limit_quota(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        for _ in range(10):
            decide(policy, limiter, outcome(status=408), context(request_sent=False))
        assert limiter.peek("example.com", now=0.0, cfg=ROUTING.status_switch_rate_limit) == 3


class TestGateThreeIdempotencyAndReplay:
    def test_non_idempotent_method_after_send_does_not_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-09：POST 已发出后不得重试，否则可能重复下单。"""
        verdict = decide(
            policy, limiter, outcome(status=503), context(method=Method.POST, request_sent=True)
        )
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.NON_IDEMPOTENT

    def test_non_idempotent_failure_is_still_recorded(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """不切换不等于不记失败：当前请求救不回来，但要让后续请求受益。"""
        verdict = decide(
            policy, limiter, outcome(status=503), context(method=Method.POST, request_sent=True)
        )
        assert verdict.failure_kind is FailureKind.ROUTE_ERROR

    @pytest.mark.parametrize("method", [Method.POST, Method.PATCH, Method.OTHER])
    def test_all_non_idempotent_methods_are_blocked(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter, method: Method
    ) -> None:
        verdict = decide(policy, limiter, outcome(status=503), context(method=method))
        assert verdict.switch is False

    @pytest.mark.parametrize(
        "method", [Method.GET, Method.HEAD, Method.PUT, Method.DELETE, Method.OPTIONS]
    )
    def test_idempotent_methods_may_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter, method: Method
    ) -> None:
        verdict = decide(policy, limiter, outcome(status=503), context(method=method))
        assert verdict.switch is True

    def test_unreplayable_bytes_block_the_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-10：请求体已流式转发，无法在新连接上重现。"""
        verdict = decide(policy, limiter, outcome(status=503), context(replayable=False))
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.NOT_REPLAYABLE

    def test_response_started_blocks_the_switch(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """响应体一旦开始写给客户端就无法撤回。"""
        verdict = decide(policy, limiter, outcome(status=503), context(response_started=True))
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.RESPONSE_STARTED

    def test_response_started_outranks_idempotency(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """它是更强的约束：无论方法是否幂等都不可撤回。"""
        verdict = decide(
            policy,
            limiter,
            outcome(status=503),
            context(method=Method.GET, response_started=True),
        )
        assert verdict.keep_reason is KeepReason.RESPONSE_STARTED


class TestRateLimit:
    def test_switches_beyond_the_quota_are_blocked(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """M2-14：同 host 短时间内反复切换说明问题不在出口。"""
        for _ in range(3):
            assert decide(policy, limiter, outcome(status=503), context()).switch is True
        verdict = decide(policy, limiter, outcome(status=503), context())
        assert verdict.switch is False
        assert verdict.keep_reason is KeepReason.RATE_LIMITED

    def test_rate_limited_failure_is_still_recorded(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        for _ in range(3):
            decide(policy, limiter, outcome(status=503), context())
        verdict = decide(policy, limiter, outcome(status=503), context())
        assert verdict.failure_kind is FailureKind.ROUTE_ERROR

    def test_quota_is_per_host(self, policy: SwitchPolicy, limiter: SwitchRateLimiter) -> None:
        for _ in range(3):
            decide(policy, limiter, outcome(status=503), context(host="a.com"))
        verdict = decide(policy, limiter, outcome(status=503), context(host="b.com"))
        assert verdict.switch is True

    def test_quota_recovers_after_the_window(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        for _ in range(3):
            decide(policy, limiter, outcome(status=503), context())
        assert decide(policy, limiter, outcome(status=503), context(), now=61.0).switch is True


class TestGateOrdering:
    def test_idempotency_rejection_does_not_consume_quota(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """限流在最后：前置判据已否决的请求不该白白消耗配额。"""
        for _ in range(10):
            decide(policy, limiter, outcome(status=503), context(method=Method.POST))
        assert limiter.peek("example.com", now=0.0, cfg=ROUTING.status_switch_rate_limit) == 3

    def test_unreplayable_rejection_does_not_consume_quota(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        for _ in range(10):
            decide(policy, limiter, outcome(status=503), context(replayable=False))
        assert limiter.peek("example.com", now=0.0, cfg=ROUTING.status_switch_rate_limit) == 3

    def test_target_handled_rejection_does_not_consume_quota(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        for _ in range(10):
            decide(policy, limiter, outcome(status=404), context())
        assert limiter.peek("example.com", now=0.0, cfg=ROUTING.status_switch_rate_limit) == 3

    def test_transport_failure_does_not_consume_quota(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        for _ in range(10):
            decide(policy, limiter, outcome(error="TimeoutError"), context())
        assert limiter.peek("example.com", now=0.0, cfg=ROUTING.status_switch_rate_limit) == 3

    def test_successful_switch_consumes_exactly_one_quota(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        decide(policy, limiter, outcome(status=503), context())
        assert limiter.peek("example.com", now=0.0, cfg=ROUTING.status_switch_rate_limit) == 2


class TestPurity:
    def test_same_input_gives_same_output(
        self, policy: SwitchPolicy, limiter: SwitchRateLimiter
    ) -> None:
        """判据是纯函数，限流是唯一的可变依赖，且它由调用方显式传入。"""
        out, ctx = outcome(status=404), context()
        first = policy.should_switch(out, ctx, ROUTING, now=0.0, limiter=limiter)
        second = policy.should_switch(out, ctx, ROUTING, now=0.0, limiter=limiter)
        assert first == second
