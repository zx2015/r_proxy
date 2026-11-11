"""出口健康统计（0.3.3 起不再包含全局熔断）。

对应设计：docs/design/DD_ROUTING.md §4。

统计完全在内存中，定期落盘；兼容接口不再影响路由准入。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto

from r_proxy.config.model import DIRECT_NAME, CircuitBreakerConfig
from r_proxy.contracts import FailureKind


class HealthState(Enum):
    """Deprecated since 0.3.3：仅兼容旧存储/API；移除版本尚未确定。"""

    CLOSED = auto()


@dataclass(slots=True)
class UpstreamHealth:
    name: str
    state: HealthState = HealthState.CLOSED
    consecutive_failures: int = 0
    total_success: int = 0
    total_failure: int = 0
    last_success_at: float = 0.0
    last_error: str | None = None
    # 认证错误仅作观察标志，不限制路由。
    auth_error: bool = False
    # 与熔断状态机无关的两个纯累计字段，见 HealthTable.add_traffic。
    total_bytes_up: int = 0
    total_bytes_down: int = 0


class HealthTable:
    def __init__(self, cfg: CircuitBreakerConfig) -> None:
        # 旧配置仅为兼容参数，不参与健康统计或路由。
        self._health: dict[str, UpstreamHealth] = {}

    def is_available(self, name: str, *, now: float) -> bool:
        """Deprecated since 0.3.3：恒为 True，不参与路由准入。"""
        return True

    def acquire_probe(self, name: str, *, allow_open: bool = False) -> bool:
        """Deprecated since 0.3.3：恒为 True，不再预留探针。"""
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
        # direct 失败属于目标路径问题，不累加出口级连续失败。
        # 放在唯一收敛点上，比依赖每个调用方都传对 kind 更可靠。
        if name == DIRECT_NAME and not ok:
            kind = FailureKind.ROUTE_ERROR

        health = self._health.setdefault(name, UpstreamHealth(name))

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
        """Deprecated since 0.3.3：兼容值恒为 CLOSED，不反映可用性。"""
        return HealthState.CLOSED

    def snapshot_of(self, name: str) -> UpstreamHealth:
        """状态副本。调用方改动它不影响权威状态。"""
        health = self._health.get(name)
        return dataclasses.replace(health) if health is not None else UpstreamHealth(name)

    def all(self, *, now: float) -> list[UpstreamHealth]:
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

        历史熔断状态不恢复；兼容状态始终为 closed。
        """
        health = self._health.setdefault(name, UpstreamHealth(name))
        health.total_success = total_success
        health.total_failure = total_failure
        health.total_bytes_up = total_bytes_up
        health.total_bytes_down = total_bytes_down

    def reset(self, name: str) -> None:
        self._health.pop(name, None)

    def clear_circuit(self, name: str) -> None:
        """兼容旧重置接口：清连续失败与认证标志，保留累计计数和流量。"""
        health = self._health.get(name)
        if health is None:
            return
        health.state = HealthState.CLOSED
        health.consecutive_failures = 0
        health.auth_error = False

    def forget_except(self, names: Iterable[str]) -> None:
        """热重载后清掉已删除出口的状态。"""
        keep = set(names)
        for name in list(self._health):
            if name not in keep:
                del self._health[name]

    def reconfigure(self, cfg: CircuitBreakerConfig) -> None:
        """兼容历史配置热重载，不改变累计统计。"""
        # 旧配置仅为兼容参数，不参与健康统计或路由。
