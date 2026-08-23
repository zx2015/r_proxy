"""候选链驱动：按顺序尝试出口，按判据决定跳过、顺延还是终止。

对应设计：docs/design/DD_ROUTING.md §9、docs/design/DD_SWITCHING.md §9。

单次尝试的协议细节由调用方以 ``attempt`` 回调注入——``egress`` 不得依赖
``protocol``。这也让执行层的控制流可以在没有任何网络的情况下测透。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from r_proxy.config.model import ConfigSnapshot, UpstreamConfig
from r_proxy.contracts import FailureKind, RequestTarget
from r_proxy.decision.limiter import SwitchRateLimiter
from r_proxy.decision.model import AttemptOutcome, Decision, SwitchContext, SwitchVerdict
from r_proxy.decision.switching import SwitchPolicy
from r_proxy.state.runtime import RuntimeState
from r_proxy.storage.queue import (
    WriteOp,
    WriteSink,
    request_log,
    route_block_delete,
    route_block_upsert,
    sticky_delete,
    sticky_hit,
    sticky_upsert,
)

logger = logging.getLogger(__name__)

TUNNEL_PREMATURE_DEATH = "tunnel_premature_death"

# 同一出口最多尝试两次：408 空闲连接回收是「换条连接重试」而非「换个出口」，
# 但连续 408 不能变成死循环。
MAX_ATTEMPTS_PER_UPSTREAM = 2


@dataclass(slots=True)
class AttemptResult[T]:
    """单次尝试的产物。

    ``switch_context`` 由执行尝试的一方给出：只有它知道字节到底发出去了没有、
    响应体有没有开始写给客户端。
    """

    outcome: AttemptOutcome
    switch_context: SwitchContext
    # 可交给客户端的连接/响应。切换走或候选链耗尽时由 ``discard`` 回收。
    payload: T | None = None


@dataclass(slots=True)
class ExecutionResult[T]:
    """``delivered is None`` 表示没有任何可交付客户端的结果，调用方应回 502。"""

    delivered: AttemptResult[T] | None
    last_outcome: AttemptOutcome | None = None
    verdict: SwitchVerdict | None = None
    attempts: int = 0


class AttemptExecutor:
    def __init__(
        self,
        *,
        state: RuntimeState,
        policy: SwitchPolicy,
        limiter: SwitchRateLimiter,
        clock: Callable[[], float] = time.monotonic,
        sink: WriteSink | None = None,
        unix_clock: Callable[[], float] = time.time,
    ) -> None:
        self._state = state
        self._policy = policy
        self._limiter = limiter
        self._clock = clock
        # ``sink is None`` 时只更新内存：粘性与负面记忆照常生效，只是重启后
        # 丢失。测试与 ``--no-web`` 之外的裁剪场景都靠这个默认值运行。
        self._sink = sink
        # 内存用 monotonic（不受调时影响），落盘用 Unix 时间（可跨重启比较）。
        self._unix_clock = unix_clock

    @property
    def state(self) -> RuntimeState:
        """供路由构造使用。``Router`` 只按只读协议取用其中的表。"""
        return self._state

    async def execute[T](
        self,
        target: RequestTarget,
        decision: Decision,
        snapshot: ConfigSnapshot,
        *,
        attempt: Callable[[UpstreamConfig], Awaitable[AttemptResult[T]]],
        discard: Callable[[T], Awaitable[None]],
        request_id: str | None = None,
    ) -> ExecutionResult[T]:
        """沿候选链执行。

        ``continue`` 与 ``return`` 的区别是本模块的核心语义：``continue``
        跳过这个出口且**不算**一次失败尝试（未发起连接）；``return`` 终止
        整个候选链，把结果原样交给客户端。混淆二者会导致「判据说不该切换
        但代码继续试了下一个出口」——例如 POST 被重复投递。

        ``request_id`` 缺省时不记请求日志：这些行的全部价值在于能按请求归组
        成切换链，没有请求身份的调用方（测试）写下去也无从对应。
        """
        attempts = 0
        last_outcome: AttemptOutcome | None = None
        last_verdict: SwitchVerdict | None = None

        for name in decision.chain:
            upstream = snapshot.upstream(name)
            if upstream is None:
                # 候选链构造后配置被热重载删掉了这个出口。
                continue
            if not self._admit(name, decision):
                continue

            tries = 0
            while tries < MAX_ATTEMPTS_PER_UPSTREAM:
                tries += 1
                attempts += 1
                started = self._clock()
                result = await attempt(upstream)
                last_outcome = result.outcome
                now = self._clock()
                # 耗时在这里测而不是让尝试回调自己填：回调有四个返回分支
                # （连接失败、握手非 2xx、正常应答……），漏掉任何一个都会在
                # 界面上留下一列无声的 0。
                elapsed_ms = int((now - started) * 1000)

                if result.outcome.ok:
                    self._record_success(target, name, result.outcome, snapshot, decision, now=now)
                    self._log_attempt(
                        request_id,
                        target,
                        decision,
                        upstream,
                        result.outcome,
                        None,
                        attempts - 1,
                        elapsed_ms,
                        snapshot,
                    )
                    return ExecutionResult(
                        delivered=result, last_outcome=result.outcome, attempts=attempts
                    )

                verdict = self._policy.should_switch(
                    result.outcome,
                    result.switch_context,
                    snapshot.routing,
                    now=now,
                    limiter=self._limiter,
                )
                last_verdict = verdict
                self._record_failure(
                    target, name, result.outcome, verdict, snapshot, decision, now=now
                )
                self._log_attempt(
                    request_id,
                    target,
                    decision,
                    upstream,
                    result.outcome,
                    verdict,
                    attempts - 1,
                    elapsed_ms,
                    snapshot,
                )
                _log_failed_attempt(request_id, target, name, result.outcome, verdict, elapsed_ms)

                if verdict.retry_same_upstream and tries < MAX_ATTEMPTS_PER_UPSTREAM:
                    # 换条连接重试同一出口，不推进候选链。
                    await _discard(result.payload, discard)
                    continue

                if not verdict.switch and not verdict.retry_same_upstream:
                    # 终止：把这个响应原样交给客户端，连接不能关。
                    return ExecutionResult(
                        delivered=result if result.payload is not None else None,
                        last_outcome=result.outcome,
                        verdict=verdict,
                        attempts=attempts,
                    )

                await _discard(result.payload, discard)
                break

            # 规则强制路由失败不顺延，原样返回错误。
            if not decision.switchable:
                break

        logger.warning(
            "请求 %s 无出口可交付：host=%s 已尝试 %d 次，候选链 %s",
            request_id,
            target.host,
            attempts,
            "/".join(decision.chain),
        )
        return ExecutionResult(
            delivered=None, last_outcome=last_outcome, verdict=last_verdict, attempts=attempts
        )

    def note_dead_end(
        self,
        target: RequestTarget,
        decision: Decision,
        snapshot: ConfigSnapshot,
        *,
        request_id: str,
    ) -> None:
        """候选链为空：一次尝试都没发起，但仍要留一行。

        客户端只会收到不含任何拓扑信息的 `502`，这一行是用户判断「规则配错了」
        还是「网络坏了」的唯一依据（[RULES_CONFIG](../../docs/requirements/RULES_CONFIG.md) §4.4）。

        ``upstream_name`` 记空串而非某个出口名：没有任何出口被尝试过，填一个
        名字会让它在「按出口筛选」时冒充成一次真实尝试。
        """
        if self._sink is None or _is_webui_traffic(target, snapshot):
            return
        self._sink.put(
            self._row(
                request_id,
                target,
                decision,
                upstream="",
                priority=None,
                attempt_index=0,
                status=None,
                error=decision.empty_reason,
                failure_kind=None,
                keep_reason=None,
                elapsed_ms=0,
                bytes_up=0,
                bytes_down=0,
            )
        )

    def note_tunnel_premature_death(self, host: str, upstream: str, *, request_id: str) -> None:
        """隧道建立后上游零字节且很快关闭。

        计 ``route_error`` 而非 ``upstream_error``：隧道能建立说明上级代理是
        活的，问题在代理到目标那一段。

        必须留一行日志：这个标记会让后续十分钟的请求绕开该出口，而它诞生的
        那次请求在 ``request_log`` 里记的是 `200 成功`——不打日志就完全无从
        解释「为什么这个出口突然被绕开了」。
        """
        logger.warning(
            "请求 %s 隧道早夭：host=%s 出口=%s，后续 %s 秒内该组合不再被选中",
            request_id,
            host,
            upstream,
            int(self._state.memory.ttl),
        )
        self._block(host, upstream, TUNNEL_PREMATURE_DEATH, now=self._clock())

    # ----------------------------------------------------------------------

    def _log_attempt(
        self,
        request_id: str | None,
        target: RequestTarget,
        decision: Decision,
        upstream: UpstreamConfig,
        outcome: AttemptOutcome,
        verdict: SwitchVerdict | None,
        attempt_index: int,
        elapsed_ms: int,
        snapshot: ConfigSnapshot,
    ) -> None:
        """一次尝试一行。``verdict is None`` 表示这次尝试成功，无需判据。

        归类取 ``verdict.failure_kind`` 而非 ``outcome.kind``：前者是判据链的
        结论，也是真正被用来记熔断与负面记忆的那一个。记另一个会让日志与系统
        的实际行为对不上——而对不上的时候，用户信的是日志。
        """
        if self._sink is None or request_id is None or _is_webui_traffic(target, snapshot):
            return
        self._sink.put(
            self._row(
                request_id,
                target,
                decision,
                upstream=upstream.name,
                priority=upstream.priority,
                attempt_index=attempt_index,
                status=outcome.status,
                error=outcome.error,
                failure_kind=None if verdict is None else verdict.failure_kind.name.lower(),
                keep_reason=(
                    None
                    if verdict is None or verdict.keep_reason is None
                    else verdict.keep_reason.name.lower()
                ),
                elapsed_ms=elapsed_ms,
                # 字节数记 0：这一行在尝试结束时就写下，而响应体是之后才流式
                # 转发的，此刻还没有可记的数字。要填准只能在传输结束后回来
                # UPDATE，而 request_log 是只插不改的（DD_STORAGE §4.4）。
                bytes_up=outcome.bytes_up,
                bytes_down=outcome.bytes_down,
            )
        )

    def _row(
        self,
        request_id: str,
        target: RequestTarget,
        decision: Decision,
        *,
        upstream: str,
        priority: int | None,
        attempt_index: int,
        status: int | None,
        error: str | None,
        failure_kind: str | None,
        keep_reason: str | None,
        elapsed_ms: int,
        bytes_up: int,
        bytes_down: int,
    ) -> WriteOp:
        return request_log(
            request_id=request_id,
            host=target.host,
            url=target.url,
            method=target.method.name,
            upstream=upstream,
            upstream_priority=priority,
            attempt_index=attempt_index,
            decision_source=decision.source,
            # 与校验报错、界面行号同一套写法，用户不需要做任何换算。
            rule_origin=(
                None if decision.rule_position is None else f"rules[{decision.rule_position}]"
            ),
            http_status=status,
            error=error,
            failure_kind=failure_kind,
            keep_reason=keep_reason,
            elapsed_ms=elapsed_ms,
            bytes_up=bytes_up,
            bytes_down=bytes_down,
            now_unix=int(self._unix_clock()),
        )

    def _admit(self, name: str, decision: Decision) -> bool:
        """尝试前重新检查健康，不在请求开始时固化（PRD §4.9.4）。

        规则强制路由跳过这道闸门：熔断是自动路由避开坏出口的机制，而规则
        表达的是用户的明确意图，因熔断拒绝执行会让用户认为规则失效了。
        """
        if not decision.switchable:
            return True
        if not self._state.health.is_available(name, now=self._clock()):
            return False
        return self._state.health.acquire_probe(name)

    def _record_success(
        self,
        target: RequestTarget,
        name: str,
        outcome: AttemptOutcome,
        snapshot: ConfigSnapshot,
        decision: Decision,
        *,
        now: float,
    ) -> None:
        self._state.health.record_result(name, ok=True, kind=FailureKind.NOT_A_FAILURE, now=now)
        if self._state.memory.clear(target.host, name) and self._sink is not None:
            self._sink.put(route_block_delete(host=target.host, upstream=name))
        if decision.source != "rule":
            self._record_sticky(target, name, outcome, now=now)

    def _record_sticky(
        self, target: RequestTarget, name: str, outcome: AttemptOutcome, *, now: float
    ) -> None:
        changed = self._state.sticky.record_success(target.host, name, now=now)
        if self._sink is None:
            return
        now_unix = int(self._unix_clock())
        if changed:
            self._sink.put(
                sticky_upsert(
                    host=target.host,
                    upstream=name,
                    url=target.url,
                    now_unix=now_unix,
                    status=outcome.status,
                )
            )
        else:
            # 纯计数变化走 SQL 侧自增，且同批次内可合并为一条 ``+ N``。
            self._sink.put(sticky_hit(host=target.host, now_unix=now_unix))

    def _record_failure(
        self,
        target: RequestTarget,
        name: str,
        outcome: AttemptOutcome,
        verdict: SwitchVerdict,
        snapshot: ConfigSnapshot,
        decision: Decision,
        *,
        now: float,
    ) -> None:
        kind = verdict.failure_kind
        if kind is FailureKind.NOT_A_FAILURE:
            # 404、521、408 空闲回收都证明这条路是通的：按成功记，
            # 顺带清掉可能已经过时的负面记忆。
            self._record_success(target, name, outcome, snapshot, decision, now=now)
            return

        if outcome.status == 407:
            self._state.health.mark_auth_error(name)

        self._state.health.record_result(
            name, ok=False, kind=kind, now=now, error=outcome.error or _status_error(outcome)
        )
        if kind is FailureKind.ROUTE_ERROR:
            self._block(target.host, name, outcome.error or _status_error(outcome), now=now)
        if decision.source != "rule":
            self._record_sticky_failure(target.host, snapshot)

    def _record_sticky_failure(self, host: str, snapshot: ConfigSnapshot) -> None:
        """失败计数记在 host 上，与失败的是哪个出口无关：它记的是
        「上次成功的那个出口现在还灵不灵」。

        ``manual`` 绑定达到阈值也不清除——那是用户的声明，不是缓存。
        """
        if not self._state.sticky.record_failure(
            host, threshold=snapshot.routing.sticky_fail_threshold
        ):
            return
        if self._sink is not None:
            self._sink.put(sticky_delete(host=host))

    def _block(self, host: str, upstream: str, reason: str, *, now: float) -> None:
        self._state.memory.block(host, upstream, now=now, reason=reason)
        if self._sink is None:
            return
        entry = self._state.memory.entry(host, upstream)
        if entry is None:
            # 容量为 0 时 block 是空操作，没有内存记录就没有要落盘的东西。
            return
        now_unix = int(self._unix_clock())
        self._sink.put(
            route_block_upsert(
                host=host,
                upstream=upstream,
                reason=reason,
                now_unix=now_unix,
                # 库里存 Unix 时间：内存的 monotonic 值跨重启没有意义，
                # 因此按「还剩多久解封」换算。
                blocked_until=now_unix + int(entry.blocked_until - now),
            )
        )


def _log_failed_attempt(
    request_id: str | None,
    target: RequestTarget,
    name: str,
    outcome: AttemptOutcome,
    verdict: SwitchVerdict,
    elapsed_ms: int,
) -> None:
    """每次失败的尝试一行。

    出口名与失败原因**可以**进 stderr——不得回显的是给客户端的响应
    （[PRD §4.3.9](../../docs/requirements/PRD_OVERVIEW.md)），运维日志正相反：
    没有出口名的切换记录等于没有记录。

    一行的量级由失败次数决定，不是每请求一行：正常运行时这里应当是安静的，
    一旦刷屏本身就是信号。
    """
    logger.warning(
        "请求 %s 尝试失败：host=%s 出口=%s %s 耗时=%dms 归类=%s 处置=%s",
        request_id,
        target.host,
        name,
        f"status={outcome.status}" if outcome.error is None else outcome.error,
        elapsed_ms,
        verdict.failure_kind.name.lower(),
        _disposition(verdict),
    )


def _disposition(verdict: SwitchVerdict) -> str:
    if verdict.retry_same_upstream:
        return "换连接重试"
    if verdict.switch:
        return "切换下一个出口"
    return f"原样返回（{verdict.keep_reason.name.lower() if verdict.keep_reason else '未定'}）"


async def _discard[T](payload: T | None, discard: Callable[[T], Awaitable[None]]) -> None:
    if payload is not None:
        await discard(payload)


def _status_error(outcome: AttemptOutcome) -> str:
    return f"status_{outcome.status}" if outcome.status is not None else "unknown"


def _is_webui_traffic(target: RequestTarget, snapshot: ConfigSnapshot) -> bool:
    """目标端口是否正是本进程自己的 Web UI。

    只比端口，不比 host：Web UI 可能绑在 ``0.0.0.0``，客户端却是用局域网 IP
    连过来的，字面 host 永远对不上绑定地址。默认端口 6061 撞车的概率可忽略。
    """
    return snapshot.webui.enabled and target.port == snapshot.webui.port
