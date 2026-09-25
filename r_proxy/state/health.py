"""出口健康与熔断状态机。

对应设计：docs/design/DD_ROUTING.md §4。

状态完全在内存中。``open → half_open`` 的迁移在**查询时**惰性判定而非用定时器：
定时器要为每个出口维护 ``call_later`` 句柄，热重载删除出口时容易泄漏；而没有
请求时的状态迁移本身毫无意义。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto

from r_proxy.config.model import DIRECT_NAME, CircuitBreakerConfig
from r_proxy.contracts import FailureKind


class HealthState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


@dataclass(slots=True)
class UpstreamHealth:
    name: str
    state: HealthState = HealthState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False
    total_success: int = 0
    total_failure: int = 0
    last_success_at: float = 0.0
    last_error: str | None = None
    # 叠加在健康状态之上的独立标志，不是第四个状态：出口仍留在候选链里。
    auth_error: bool = False
    # 与熔断状态机无关的两个纯累计字段，见 HealthTable.add_traffic。
    total_bytes_up: int = 0
    total_bytes_down: int = 0


class HealthTable:
    def __init__(self, cfg: CircuitBreakerConfig) -> None:
        self._cfg = cfg
        self._health: dict[str, UpstreamHealth] = {}

    def is_available(self, name: str, *, now: float) -> bool:
        """该出口现在能否进入候选链。

        会顺带完成 ``open → half_open`` 的惰性迁移，因此对 ``half_open``
        出口连续调用只有第一次配合 :meth:`acquire_probe` 能放行探测。
        """
        health = self._health.get(name)
        if health is None or health.state is HealthState.CLOSED:
            return True
        if health.state is HealthState.OPEN:
            if now - health.opened_at < self._cfg.cooldown_seconds:
                return False
            health.state = HealthState.HALF_OPEN
            health.probe_in_flight = False
        return not health.probe_in_flight

    def acquire_probe(self, name: str) -> bool:
        """尝试获取 ``half_open`` 的探测名额。

        **必须是同步方法**：中间若有 ``await``，两个请求可能都读到
        ``probe_in_flight == False`` 然后都置 ``True``，闸门就失效了。
        """
        health = self._health.get(name)
        if health is None or health.state is not HealthState.HALF_OPEN:
            return True
        if health.probe_in_flight:
            return False
        health.probe_in_flight = True
        return True

    def record_result(
        self,
        name: str,
        *,
        ok: bool,
        kind: FailureKind,
        now: float,
        error: str | None = None,
    ) -> None:
        # direct 的失败一律降级：它被熔断会导致内网与 localhost 全部不可访问。
        # 放在唯一收敛点上，比依赖每个调用方都传对 kind 更可靠。
        if name == DIRECT_NAME and not ok:
            kind = FailureKind.ROUTE_ERROR

        health = self._health.setdefault(name, UpstreamHealth(name))
        health.probe_in_flight = False

        if ok:
            health.total_success += 1
            health.consecutive_failures = 0
            # 乱序完成的请求会用较早的时间戳覆盖较晚的，取最大值即可（RC-06）。
            health.last_success_at = max(health.last_success_at, now)
            health.state = HealthState.CLOSED
            health.auth_error = False
            return

        health.total_failure += 1
        health.last_error = error

        if kind is not FailureKind.UPSTREAM_ERROR:
            return

        health.consecutive_failures += 1
        if health.state is HealthState.HALF_OPEN:
            health.state = HealthState.OPEN
            # 必须重置：否则 now - opened_at 仍大于冷却期，
            # 下一个请求立刻又变 half_open，退化为无冷却的连续重试。
            health.opened_at = now
        elif self._cfg.enabled and health.consecutive_failures >= self._cfg.fail_threshold:
            health.state = HealthState.OPEN
            health.opened_at = now

    def mark_auth_error(self, name: str) -> None:
        """标记凭据错误。不移出候选链——用户可能正在修，或只有部分目标要求认证。"""
        self._health.setdefault(name, UpstreamHealth(name)).auth_error = True

    def add_traffic(self, name: str, *, bytes_up: int, bytes_down: int) -> None:
        """累加字节数，与熔断状态机完全无关（DD_ROUTING §4.8）。

        独立于 :meth:`record_result`：字节数只有在 relay/pump 完成之后才知道，
        比响应头读到、判断成功/失败的时刻晚得多。不区分成功/失败——已经跑出去
        的字节是真实发生过的流量，即便这次尝试最终判定为失败。
        """
        health = self._health.setdefault(name, UpstreamHealth(name))
        health.total_bytes_up += bytes_up
        health.total_bytes_down += bytes_down

    def state_of(self, name: str, *, now: float) -> HealthState:
        """当前状态。Web 查询也必须经此，否则会显示 ``open`` 而实际冷却已过。"""
        self.is_available(name, now=now)
        health = self._health.get(name)
        return HealthState.CLOSED if health is None else health.state

    def snapshot_of(self, name: str) -> UpstreamHealth:
        """状态副本。调用方改动它不影响权威状态。"""
        health = self._health.get(name)
        return dataclasses.replace(health) if health is not None else UpstreamHealth(name)

    def all(self, *, now: float) -> list[UpstreamHealth]:
        for name in list(self._health):
            self.is_available(name, now=now)
        return [dataclasses.replace(h) for h in self._health.values()]

    def restore_counters(
        self,
        name: str,
        *,
        total_success: int,
        total_failure: int,
        total_bytes_up: int = 0,
        total_bytes_down: int = 0,
    ) -> None:
        """启动回填累计计数，供 Web 展示历史成功率。

        **不回填熔断状态**：重启可能正是运维在修网络，带着旧的 ``open``
        启动会让刚修好的出口继续被拒绝一整个冷却期。
        """
        health = self._health.setdefault(name, UpstreamHealth(name))
        health.total_success = total_success
        health.total_failure = total_failure
        health.total_bytes_up = total_bytes_up
        health.total_bytes_down = total_bytes_down

    def reset(self, name: str) -> None:
        self._health.pop(name, None)

    def clear_circuit(self, name: str) -> None:
        """手动解除熔断（Web 界面的「重置」按钮）。

        与 :meth:`reset` 的区别是**保留累计计数**：重置熔断的意图是「再给它
        一次机会」，不是「忘掉这个出口的历史」，而看板上的历史成功率正来自
        这两个计数。顺带清掉 ``auth_error``——运维点重置通常正是因为刚改完
        凭据，标志会在下一次认证失败时自己回来。
        """
        health = self._health.get(name)
        if health is None:
            return
        health.state = HealthState.CLOSED
        health.consecutive_failures = 0
        health.probe_in_flight = False
        health.opened_at = 0.0
        health.auth_error = False

    def forget_except(self, names: Iterable[str]) -> None:
        """热重载后清掉已删除出口的状态。"""
        keep = set(names)
        for name in list(self._health):
            if name not in keep:
                del self._health[name]

    def reconfigure(self, cfg: CircuitBreakerConfig) -> None:
        """热重载熔断参数。已有观测结果保留——它们是花时间学来的。"""
        self._cfg = cfg
