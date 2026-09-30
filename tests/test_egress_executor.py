"""egress/executor.py 的候选链驱动测试。

对应设计：docs/design/DD_ROUTING.md §9、docs/design/DD_SWITCHING.md §9。

单次尝试由调用方注入，因此这里不需要任何网络：被测的是「跳过、终止、
顺延」三种控制流的区别，以及状态记录是否落在正确的表上。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from r_proxy.config.model import (
    CircuitBreakerConfig,
    RateLimitConfig,
    RoutingConfig,
    UpstreamConfig,
    WebUIConfig,
)
from r_proxy.contracts import AddressFamily, FailureKind, Headers, Method, RequestTarget
from r_proxy.decision.limiter import SwitchRateLimiter
from r_proxy.decision.model import AttemptOutcome, Decision, KeepReason, SwitchContext
from r_proxy.decision.switching import SwitchPolicy
from r_proxy.egress.executor import AttemptExecutor, AttemptResult
from r_proxy.state.health import HealthState
from r_proxy.state.runtime import RuntimeState
from r_proxy.state.sticky import StickyEntry
from r_proxy.storage.queue import OpKind, WriteOp
from tests.conftest import make_snapshot, upstream

ROUTING = RoutingConfig(
    circuit_breaker=CircuitBreakerConfig(fail_threshold=2, cooldown_seconds=60),
    status_switch_rate_limit=RateLimitConfig(max_switches_per_host=3, window_seconds=60),
)


def target(
    host: str = "example.com", *, connect: bool = False, port: int | None = None
) -> RequestTarget:
    return RequestTarget(
        host=host,
        port=port if port is not None else (443 if connect else 80),
        method=Method.CONNECT if connect else Method.GET,
        url=None if connect else f"http://{host}/",
        is_connect=connect,
        family=AddressFamily.UNKNOWN,
    )


def switch_context(
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


@dataclass
class FakeAttempts:
    """按出口名给出预设结果，并记录实际尝试顺序与被丢弃的载荷。"""

    results: dict[str, list[AttemptResult[str]]]
    tried: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)

    async def attempt(self, u: UpstreamConfig) -> AttemptResult[str]:
        self.tried.append(u.name)
        queue = self.results[u.name]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def discard(self, payload: str) -> None:
        self.discarded.append(payload)


def ok_result(name: str) -> AttemptResult[str]:
    return AttemptResult(
        outcome=AttemptOutcome(upstream=name, ok=True, status=200),
        switch_context=switch_context(),
        payload=f"conn-{name}",
    )


def transport_failure(
    name: str, *, kind: FailureKind = FailureKind.ROUTE_ERROR
) -> AttemptResult[str]:
    return AttemptResult(
        outcome=AttemptOutcome(upstream=name, ok=False, error="TimeoutError", kind=kind),
        switch_context=switch_context(),
    )


def status_failure(
    name: str,
    status: int,
    *,
    headers: Headers | None = None,
    ctx: SwitchContext | None = None,
) -> AttemptResult[str]:
    return AttemptResult(
        outcome=AttemptOutcome(
            upstream=name,
            ok=False,
            status=status,
            response_headers=headers if headers is not None else Headers(),
        ),
        switch_context=ctx if ctx is not None else switch_context(),
        payload=f"conn-{name}",
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build(clock: Clock, state: RuntimeState) -> AttemptExecutor:
    return AttemptExecutor(
        state=state,
        policy=SwitchPolicy(),
        limiter=SwitchRateLimiter(),
        clock=clock,
    )


class TestChainWalking:
    async def test_first_success_returns_immediately(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [ok_result("a")], "b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a"]
        assert result.delivered is not None
        assert result.delivered.payload == "conn-a"

    async def test_transport_failure_advances_to_the_next_upstream(self, clock: Clock) -> None:
        """M2-01：首个出口连不上，自动切换，客户端无感知。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a", "b"]
        assert result.delivered is not None
        assert result.delivered.outcome.ok is True

    async def test_client_body_timeout_does_not_advance_to_the_next_upstream(
        self, clock: Clock
    ) -> None:
        """客户端读 body 超时后换哪个出口都等不到剩下的数据：继续遍历候选链
        只会让每个出口各自重复同一次超时，因此判据必须直接终止候选链。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        client_timeout = AttemptResult(
            outcome=AttemptOutcome(upstream="a", ok=False, error="TimeoutError"),
            switch_context=switch_context(request_sent=False, client_body_timeout=True),
        )
        fake = FakeAttempts({"a": [client_timeout], "b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a"]
        assert result.delivered is None

    async def test_exhausted_chain_reports_no_deliverable_result(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [transport_failure("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a", "b"]
        assert result.delivered is None

    async def test_empty_chain_attempts_nothing(self, clock: Clock) -> None:
        snap = make_snapshot(routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=(), source="priority", switchable=False),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == []
        assert result.delivered is None

    async def test_attempts_are_counted(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), upstream("c"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts(
            {
                "a": [transport_failure("a")],
                "b": [transport_failure("b")],
                "c": [ok_result("c")],
            }
        )

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b", "c"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert result.attempts == 3

    async def test_unknown_upstream_in_the_chain_is_skipped(self, clock: Clock) -> None:
        """热重载删掉了出口而候选链已构造好——不能因此崩掉整个请求。"""
        snap = make_snapshot(upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("gone", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["b"]
        assert result.delivered is not None


class TestSwitchVerdictControlsTheLoop:
    async def test_keep_verdict_terminates_the_chain(self, clock: Clock) -> None:
        """M2-09：判据说不切换就必须终止，绝不能「继续试下一个」。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        ctx = switch_context(method=Method.POST, request_sent=True)
        fake = FakeAttempts({"a": [status_failure("a", 503, ctx=ctx)], "b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a"]
        assert result.delivered is not None
        assert result.delivered.outcome.status == 503
        assert result.verdict is not None
        assert result.verdict.keep_reason is KeepReason.NON_IDEMPOTENT

    async def test_kept_response_is_handed_back_not_discarded(self, clock: Clock) -> None:
        """不切换意味着把这个响应原样交给客户端，连接不能被关掉。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [status_failure("a", 404)]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert result.delivered is not None
        assert result.delivered.payload == "conn-a"
        assert fake.discarded == []

    async def test_switched_away_payload_is_discarded(self, clock: Clock) -> None:
        """切换走的连接必须关闭，否则每次切换泄漏一个套接字。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [status_failure("a", 503)], "b": [ok_result("b")]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.discarded == ["conn-a"]

    async def test_exhausted_chain_discards_every_payload(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [status_failure("a", 503)], "b": [status_failure("b", 503)]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert result.delivered is None
        assert fake.discarded == ["conn-a", "conn-b"]

    async def test_rate_limited_verdict_terminates_the_chain(self, clock: Clock) -> None:
        snap = make_snapshot(*[upstream(f"u{i}", priority=i) for i in range(6)], routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({f"u{i}": [status_failure(f"u{i}", 503)] for i in range(6)})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=tuple(f"u{i}" for i in range(6)), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        # 配额为 3：前三次切换成功，第四次被限流后终止。
        assert len(fake.tried) == 4
        assert result.verdict is not None
        assert result.verdict.keep_reason is KeepReason.RATE_LIMITED


class TestRuleForcedRouting:
    async def test_unswitchable_decision_never_advances(self, clock: Clock) -> None:
        """规则强制路由失败不切换，原样返回错误。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="rule", rule_position=0, switchable=False),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a"]
        assert result.delivered is None

    async def test_unswitchable_decision_skips_the_health_gate(self, clock: Clock) -> None:
        """规则表达的是用户意图：因熔断而拒绝执行会让用户认为规则失效了。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for i in range(2):
            state.health.record_result("a", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
        assert state.health.is_available("a", now=1.0) is False
        fake = FakeAttempts({"a": [ok_result("a")]})

        clock.now = 1.0
        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="rule", switchable=False),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a"]
        assert result.delivered is not None


class TestHealthGates:
    async def test_upstream_that_opened_mid_flight_is_skipped(self, clock: Clock) -> None:
        """PRD §4.9.4：候选链每次尝试前重新检查健康，不在请求开始时固化。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for i in range(2):
            state.health.record_result("b", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [ok_result("b")]})

        clock.now = 1.0
        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a"]
        assert result.delivered is None

    async def test_skipping_is_not_a_failed_attempt(self, clock: Clock) -> None:
        """跳过与终止的区别：跳过不算尝试，也不写任何失败记录。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for i in range(2):
            state.health.record_result("a", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
        before = state.health.snapshot_of("a").total_failure
        fake = FakeAttempts({"b": [ok_result("b")]})

        clock.now = 1.0
        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert result.attempts == 1
        assert state.health.snapshot_of("a").total_failure == before

    async def test_half_open_probe_slot_is_acquired_before_attempting(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for i in range(2):
            state.health.record_result("a", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
        clock.now = 100.0
        assert state.health.state_of("a", now=100.0) is HealthState.HALF_OPEN
        state.health.acquire_probe("a")  # 另一个请求已占用名额
        fake = FakeAttempts({"a": [ok_result("a")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == []
        assert result.delivered is None


class TestStateRecording:
    async def test_success_closes_the_breaker_and_clears_route_memory(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("example.com", "a", now=0.0, reason="route_error")
        fake = FakeAttempts({"a": [ok_result("a")]})

        clock.now = 5.0
        await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.memory.is_blocked("example.com", "a", now=5.0) is False
        assert state.health.snapshot_of("a").total_success == 1

    async def test_route_error_writes_negative_memory_for_that_host(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [status_failure("a", 503)], "b": [ok_result("b")]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.memory.is_blocked("example.com", "a", now=0.0) is True
        assert state.memory.is_blocked("other.com", "a", now=0.0) is False

    async def test_route_error_does_not_open_the_breaker(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for _ in range(5):
            fake = FakeAttempts({"a": [status_failure("a", 503)], "b": [ok_result("b")]})
            await build(clock, state).execute(
                target(f"h{_}.com"),
                Decision(chain=("a", "b"), source="priority"),
                snap,
                attempt=fake.attempt,
                discard=fake.discard,
            )
        assert state.health.state_of("a", now=0.0) is HealthState.CLOSED

    async def test_upstream_error_counts_toward_the_breaker(self, clock: Clock) -> None:
        """M2-11：上级代理连续失败达阈值即熔断。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for _ in range(2):
            fake = FakeAttempts(
                {
                    "a": [transport_failure("a", kind=FailureKind.UPSTREAM_ERROR)],
                    "b": [ok_result("b")],
                }
            )
            await build(clock, state).execute(
                target(),
                Decision(chain=("a", "b"), source="priority"),
                snap,
                attempt=fake.attempt,
                discard=fake.discard,
            )
        assert state.health.state_of("a", now=0.0) is HealthState.OPEN

    async def test_upstream_error_does_not_write_negative_memory(self, clock: Clock) -> None:
        """出口整体不可用与「经该出口到不了此目标」是两件事。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts(
            {
                "a": [transport_failure("a", kind=FailureKind.UPSTREAM_ERROR)],
                "b": [ok_result("b")],
            }
        )
        await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )
        assert state.memory.is_blocked("example.com", "a", now=0.0) is False

    async def test_capability_mismatch_records_nothing(self, clock: Clock) -> None:
        """M2-16：结构性不可达，记住它只会白占 LRU 并掩盖真实故障。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts(
            {
                "a": [transport_failure("a", kind=FailureKind.CAPABILITY_MISMATCH)],
                "b": [ok_result("b")],
            }
        )
        await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )
        assert state.memory.is_blocked("example.com", "a", now=0.0) is False
        assert state.health.state_of("a", now=0.0) is HealthState.CLOSED

    async def test_target_handled_status_is_recorded_as_a_working_route(self, clock: Clock) -> None:
        """404 证明这条路是通的，负面记忆应当被清除。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("example.com", "a", now=0.0, reason="route_error")
        fake = FakeAttempts({"a": [status_failure("a", 404)]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.memory.is_blocked("example.com", "a", now=0.0) is False
        assert state.health.snapshot_of("a").total_failure == 0

    async def test_cloudflare_origin_error_is_not_an_upstream_failure(self, clock: Clock) -> None:
        """M2-05：521 不切换，也不计出口失败。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [status_failure("a", 521)]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.health.snapshot_of("a").total_failure == 0
        assert state.memory.is_blocked("example.com", "a", now=0.0) is False

    async def test_407_marks_the_auth_error_flag(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [status_failure("a", 407)], "b": [ok_result("b")]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.health.snapshot_of("a").auth_error is True

    async def test_direct_failures_never_open_the_breaker(self, clock: Clock) -> None:
        """M2-12：direct 被熔断会让内网与 localhost 全部不可访问。"""
        snap = make_snapshot(upstream("direct", direct=True), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for i in range(20):
            fake = FakeAttempts(
                {"direct": [transport_failure("direct", kind=FailureKind.UPSTREAM_ERROR)]}
            )
            clock.now = float(i)
            await build(clock, state).execute(
                target(f"blocked{i}.com"),
                Decision(chain=("direct",), source="priority"),
                snap,
                attempt=fake.attempt,
                discard=fake.discard,
            )
        assert state.health.state_of("direct", now=100.0) is HealthState.CLOSED


class TestRetrySameUpstream:
    async def test_408_before_send_retries_the_same_upstream_once(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        ctx = switch_context(request_sent=False)
        fake = FakeAttempts(
            {
                "a": [status_failure("a", 408, ctx=ctx), ok_result("a")],
                "b": [ok_result("b")],
            }
        )

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried == ["a", "a"]
        assert result.delivered is not None
        assert result.delivered.outcome.ok is True

    async def test_retry_is_not_repeated_indefinitely(self, clock: Clock) -> None:
        """连续 408 不能变成死循环。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        ctx = switch_context(request_sent=False)
        fake = FakeAttempts({"a": [status_failure("a", 408, ctx=ctx)], "b": [ok_result("b")]})

        result = await build(clock, state).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.tried.count("a") == 2
        assert result.delivered is not None
        assert result.delivered.outcome.ok is True

    async def test_idle_recycle_is_not_recorded_as_a_failure(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        ctx = switch_context(request_sent=False)
        fake = FakeAttempts({"a": [status_failure("a", 408, ctx=ctx), ok_result("a")]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.health.snapshot_of("a").total_failure == 0

    async def test_retried_payload_is_discarded(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        ctx = switch_context(request_sent=False)
        fake = FakeAttempts({"a": [status_failure("a", 408, ctx=ctx), ok_result("a")]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert fake.discarded == ["conn-a"]


class FakeSink:
    """收下写入事件而不落盘。断言的是「发了什么」，SQL 语义在 storage 层测。"""

    def __init__(self) -> None:
        self.ops: list[WriteOp] = []

    def put(self, op: WriteOp) -> bool:
        self.ops.append(op)
        return True

    def kinds(self) -> list[OpKind]:
        return [op.kind for op in self.ops]


def build_with_sink(clock: Clock, state: RuntimeState, sink: FakeSink) -> AttemptExecutor:
    return AttemptExecutor(
        state=state,
        policy=SwitchPolicy(),
        limiter=SwitchRateLimiter(),
        clock=clock,
        sink=sink,
        unix_clock=lambda: 1_700_000_000.0,
    )


class TestStickyRecording:
    """成功即记住走通的出口；失败按阈值作废（DD_ROUTING §7）。"""

    async def test_a_success_binds_the_host_to_the_upstream(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        entry = state.sticky.get("example.com")
        assert entry is not None
        assert (entry.upstream, entry.source) == ("a", "auto")
        assert OpKind.STICKY_UPSERT in sink.kinds()

    async def test_repeating_the_same_binding_only_bumps_the_counter(self, clock: Clock) -> None:
        """纯计数变化走 SQL 侧自增：每次成功都发 UPSERT 会让写入量与请求量同级。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        executor = build_with_sink(clock, state, sink)

        for _ in range(3):
            fake = FakeAttempts({"a": [ok_result("a")]})
            await executor.execute(
                target(),
                Decision(chain=("a",), source="priority"),
                snap,
                attempt=fake.attempt,
                discard=fake.discard,
            )

        assert sink.kinds() == [OpKind.STICKY_UPSERT, OpKind.STICKY_HIT, OpKind.STICKY_HIT]

    async def test_a_rule_forced_route_neither_reads_nor_writes_sticky(self, clock: Clock) -> None:
        """M3-13：规则每次都命中同一出口，写粘性只会在规则删除后继续生效。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="rule", rule_position=0, switchable=False),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.sticky.get("example.com") is None
        assert sink.ops == []

    async def test_a_success_on_another_upstream_rebinds(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        entry = state.sticky.get("example.com")
        assert entry is not None and entry.upstream == "a"

    async def test_a_manual_binding_survives_a_success_elsewhere(self, clock: Clock) -> None:
        """M3-05：内存侧不改绑定，落盘侧的 UPSERT 也不发——两层都不动它。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        state.sticky.restore(StickyEntry(host="example.com", upstream="dead", source="manual"))
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        entry = state.sticky.get("example.com")
        assert entry is not None
        assert (entry.upstream, entry.source) == ("dead", "manual")
        assert OpKind.STICKY_UPSERT not in sink.kinds()

    async def test_repeated_failures_drop_the_binding(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "a", now=0.0)
        sink = FakeSink()
        executor = build_with_sink(clock, state, sink)

        for _ in range(ROUTING.sticky_fail_threshold):
            fake = FakeAttempts({"a": [transport_failure("a")]})
            await executor.execute(
                target(),
                Decision(chain=("a",), source="priority"),
                snap,
                attempt=fake.attempt,
                discard=fake.discard,
            )

        assert state.sticky.get("example.com") is None
        assert OpKind.STICKY_DELETE in sink.kinds()

    async def test_negative_memory_is_persisted_and_cleared(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        executor = build_with_sink(clock, state, sink)

        fake = FakeAttempts({"a": [status_failure("a", 503)], "b": [ok_result("b")]})
        await executor.execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )
        assert OpKind.ROUTE_BLOCK_UPSERT in sink.kinds()

        sink.ops.clear()
        fake = FakeAttempts({"a": [ok_result("a")]})
        await executor.execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )
        assert OpKind.ROUTE_BLOCK_DELETE in sink.kinds()

    async def test_a_success_without_negative_memory_writes_no_delete(self, clock: Clock) -> None:
        """绝大多数成功请求本来就没有记忆，无条件发 DELETE 会让写入量翻倍。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert OpKind.ROUTE_BLOCK_DELETE not in sink.kinds()

    async def test_state_is_still_recorded_without_a_sink(self, clock: Clock) -> None:
        """没有存储时粘性照常生效，只是重启后丢失。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build(clock, state).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert state.sticky.get("example.com") is not None


class TestRequestLog:
    """每次尝试一行，同一 request_id 的多行按 attempt_index 构成切换链。

    对应设计：docs/design/ARCH_OVERVIEW.md §6、docs/design/DD_STORAGE.md §4.2。
    """

    @staticmethod
    def rows(sink: FakeSink) -> list[tuple[object, ...]]:
        return [op.payload for op in sink.ops if op.kind is OpKind.REQUEST_LOG]

    async def test_a_successful_attempt_is_recorded(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a", priority=7), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-1",
        )

        rows = self.rows(sink)
        assert len(rows) == 1
        (
            request_id,
            client_addr,
            host,
            url,
            method,
            upstream_name,
            priority,
            attempt_index,
            decision_source,
            rule_origin,
            status,
            error,
            failure_kind,
            keep_reason,
            *_,
        ) = rows[0]
        assert (request_id, host, method) == ("req-1", "example.com", "GET")
        assert client_addr is None
        assert url == "http://example.com/"
        assert (upstream_name, priority, attempt_index) == ("a", 7, 0)
        assert (decision_source, rule_origin) == ("priority", None)
        assert (status, error, failure_kind, keep_reason) == (200, None, None, None)

    async def test_client_addr_is_carried_from_execute_into_the_row(self, clock: Clock) -> None:
        """来源地址在连接层面采集一次，随 ``execute()`` 一路传进日志行。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [ok_result("b")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-addr",
            client_addr="203.0.113.7",
        )

        rows = self.rows(sink)
        # 切换出口不会换客户端：同一 request_id 的每一行都是同一个来源。
        assert {row[1] for row in rows} == {"203.0.113.7"}

    async def test_client_addr_is_carried_into_a_dead_end_row(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()

        build_with_sink(clock, state, sink).note_dead_end(
            target(),
            Decision(chain=(), source="rule", rule_position=2, switchable=False, empty_reason="x"),
            snap,
            request_id="req-dead",
            client_addr="203.0.113.9",
        )

        assert self.rows(sink)[0][1] == "203.0.113.9"

    async def test_every_attempt_of_a_switch_gets_its_own_row(self, clock: Clock) -> None:
        """切换链靠这些行重建。只记最终结果就看不到「先试了谁、为什么放弃」。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [ok_result("b")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-2",
        )

        rows = self.rows(sink)
        assert [(row[5], row[7]) for row in rows] == [("a", 0), ("b", 1)]
        assert {row[0] for row in rows} == {"req-2"}

    async def test_a_failed_attempt_carries_its_classification(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [transport_failure("a")], "b": [ok_result("b")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-3",
        )

        first = self.rows(sink)[0]
        assert first[11] == "TimeoutError"
        assert first[12] == "route_error"

    async def test_a_kept_response_records_why_it_was_not_switched(self, clock: Clock) -> None:
        """`keep_reason` 是「为什么没切」的唯一记录，落不了盘就等于没有。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [status_failure("a", 404)], "b": [ok_result("b")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a", "b"), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-4",
        )

        row = self.rows(sink)[0]
        assert row[10] == 404
        assert row[13] == KeepReason.TARGET_HANDLED.name.lower()

    async def test_a_rule_hit_records_the_rule_position(self, clock: Clock) -> None:
        """用户要能把一行日志对回规则页上的那一行。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="rule", rule_position=3, switchable=False),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-5",
        )

        row = self.rows(sink)[0]
        assert (row[8], row[9]) == ("rule", "rules[3]")

    async def test_the_duration_is_measured_around_the_attempt(self, clock: Clock) -> None:
        """耗时由执行器夹在 attempt 两侧测量，而不是让每个尝试回调自己填：
        回调有四个返回分支，漏掉任何一个都会在界面上留下一列 0。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()

        async def slow(_: UpstreamConfig) -> AttemptResult[str]:
            clock.now += 0.25
            return ok_result("a")

        async def discard(_: str) -> None:
            return None

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=slow,
            discard=discard,
            request_id="req-7",
        )

        assert self.rows(sink)[0][14] == 250

    async def test_nothing_is_written_without_a_request_id(self, clock: Clock) -> None:
        """没有请求身份就没有可归组的链，写下去的行无法与任何请求对应。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
        )

        assert self.rows(sink) == []

    async def test_a_dead_end_leaves_a_row_although_nothing_was_tried(self, clock: Clock) -> None:
        """候选链为空时客户端只拿到通用 502，这一行是唯一的追溯依据
        （RULES_CONFIG §4.4）。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()

        build_with_sink(clock, state, sink).note_dead_end(
            target(),
            Decision(
                chain=(),
                source="rule",
                rule_position=2,
                switchable=False,
                empty_reason="rule_target_disabled",
            ),
            snap,
            request_id="req-6",
        )

        rows = self.rows(sink)
        assert len(rows) == 1
        assert (rows[0][0], rows[0][5], rows[0][9]) == ("req-6", "", "rules[2]")
        assert rows[0][11] == "rule_target_disabled"

    async def test_a_request_to_the_web_ui_itself_is_not_logged(self, clock: Clock) -> None:
        """仪表盘每 3 秒轮询 `/api/status`，走代理时不该把这些噪声记进
        `request_log`——它们不是被代理的流量，是操作者自己的运维请求。"""
        snap = make_snapshot(
            upstream("a"), routing=ROUTING, webui=WebUIConfig(enabled=True, port=6061)
        )
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(host="192.0.2.100", port=6061),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-8",
        )

        assert self.rows(sink) == []

    async def test_the_web_ui_exclusion_needs_webui_enabled(self, clock: Clock) -> None:
        """`webui.enabled=False`（如 ``--no-web``）时端口号没有意义，不能拿来误伤。"""
        snap = make_snapshot(
            upstream("a"), routing=ROUTING, webui=WebUIConfig(enabled=False, port=6061)
        )
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        fake = FakeAttempts({"a": [ok_result("a")]})

        await build_with_sink(clock, state, sink).execute(
            target(host="192.0.2.100", port=6061),
            Decision(chain=("a",), source="priority"),
            snap,
            attempt=fake.attempt,
            discard=fake.discard,
            request_id="req-9",
        )

        assert len(self.rows(sink)) == 1

    async def test_a_dead_end_to_the_web_ui_is_not_logged(self, clock: Clock) -> None:
        snap = make_snapshot(
            upstream("a"), routing=ROUTING, webui=WebUIConfig(enabled=True, port=6061)
        )
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()

        build_with_sink(clock, state, sink).note_dead_end(
            target(host="192.0.2.100", port=6061),
            Decision(chain=(), source="priority", empty_reason="no_candidates"),
            snap,
            request_id="req-10",
        )

        assert self.rows(sink) == []


class TestTunnelPrematureDeath:
    async def test_records_negative_memory(self, clock: Clock) -> None:
        """M2-19：隧道能建立说明代理是活的，问题在代理到目标那一段。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        clock.now = 10.0
        build(clock, state).note_tunnel_premature_death("example.com", "a", request_id="r1")
        assert state.memory.is_blocked("example.com", "a", now=10.0) is True

    async def test_does_not_open_the_breaker(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        executor = build(clock, state)
        for _ in range(5):
            executor.note_tunnel_premature_death("example.com", "a", request_id="r1")
        assert state.health.state_of("a", now=0.0) is HealthState.CLOSED


class TestNoteTraffic:
    """`note_traffic` 与熔断、粘性无关，只做内存累计与落盘两件事（DD_ROUTING §4.8）。"""

    def rows(self, sink: FakeSink) -> list[WriteOp]:
        return [op for op in sink.ops if op.kind is OpKind.TRAFFIC_LOG]

    async def test_accumulates_bytes_on_the_health_table(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        build(clock, state).note_traffic(
            target(host="example.com"),
            "a",
            snap,
            bytes_up=100,
            bytes_down=200,
            request_id="r1",
        )
        snapshot = state.health.snapshot_of("a")
        assert (snapshot.total_bytes_up, snapshot.total_bytes_down) == (100, 200)

    async def test_accumulates_regardless_of_the_circuit_state(self, clock: Clock) -> None:
        """已经跑出去的字节是真实流量，与这次尝试最终是否判定失败无关。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        for _ in range(5):
            state.health.record_result("a", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=0.0)
        assert state.health.state_of("a", now=0.0) is HealthState.OPEN

        build(clock, state).note_traffic(
            target(host="example.com"), "a", snap, bytes_up=10, bytes_down=20, request_id="r1"
        )
        snapshot = state.health.snapshot_of("a")
        assert (snapshot.total_bytes_up, snapshot.total_bytes_down) == (10, 20)

    async def test_queues_a_traffic_log_row(self, clock: Clock) -> None:
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        build_with_sink(clock, state, sink).note_traffic(
            target(host="example.com"),
            "a",
            snap,
            bytes_up=10,
            bytes_down=20,
            request_id="req-1",
        )
        (op,) = self.rows(sink)
        assert op.payload == ("req-1", "example.com", "a", 10, 20, 1_700_000_000)

    async def test_no_sink_still_updates_memory(self, clock: Clock) -> None:
        """``--no-web`` 之类没有存储层的场景：内存累计照常生效，只是不落盘。"""
        snap = make_snapshot(upstream("a"), routing=ROUTING)
        state = RuntimeState.from_snapshot(snap)
        build(clock, state).note_traffic(
            target(host="example.com"), "a", snap, bytes_up=5, bytes_down=5, request_id="r1"
        )
        assert state.health.snapshot_of("a").total_bytes_up == 5

    async def test_web_ui_traffic_is_not_recorded(self, clock: Clock) -> None:
        """否则仪表盘轮询自己产生的连接会把出口健康与主机流量榜都搅乱。"""
        snap = make_snapshot(
            upstream("a"), routing=ROUTING, webui=WebUIConfig(enabled=True, port=6061)
        )
        state = RuntimeState.from_snapshot(snap)
        sink = FakeSink()
        build_with_sink(clock, state, sink).note_traffic(
            target(host="192.0.2.100", port=6061),
            "a",
            snap,
            bytes_up=10,
            bytes_down=20,
            request_id="req-1",
        )
        assert self.rows(sink) == []
        assert state.health.snapshot_of("a").total_bytes_up == 0
