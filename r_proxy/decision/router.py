"""候选链构造：这个请求应该按什么顺序尝试哪些出口。

对应设计：docs/design/DD_ROUTING.md §2、§3、§5.3。

``Router`` 不发起连接、不判断失败是否值得切换（那是
:mod:`r_proxy.decision.switching`）、不读写数据库。``now`` 由调用方传入而非
内部取 ``time.monotonic()``：使冷却期、TTL 等时间相关逻辑在测试中可控。
"""

from __future__ import annotations

from r_proxy.config.model import ConfigSnapshot, UpstreamConfig
from r_proxy.contracts import AddressFamily, RequestTarget
from r_proxy.decision.model import Decision, DecisionSource
from r_proxy.rules.matcher import match
from r_proxy.rules.model import RuleMatch, RuleSet
from r_proxy.state.runtime import RuntimeStateView

EMPTY_NO_UPSTREAM = "no_available_upstream"
EMPTY_IPV6_UNAVAILABLE = "ipv6_unavailable"
EMPTY_RULE_TARGET_DISABLED = "rule_target_disabled"
EMPTY_RULE_TARGET_UNKNOWN = "rule_target_unknown"


class Router:
    """无状态。每请求状态一律通过参数传入，实例可被所有请求共享。"""

    def build_chain(
        self,
        target: RequestTarget,
        snapshot: ConfigSnapshot,
        rule_set: RuleSet,
        state: RuntimeStateView,
        *,
        now: float,
    ) -> Decision:
        if (hit := match(rule_set, target)) is not None:
            return self._forced(hit, target, snapshot, state)

        usable = self._filter_usable(target, snapshot, state, now=now, relaxed=False)
        if not usable:
            # 放宽：熔断与负面记忆都是基于历史观测的推断，可能已经不准。
            # 宁可试一次，也别让用户完全上不去网。
            usable = self._filter_usable(target, snapshot, state, now=now, relaxed=True)
        if not usable:
            return Decision(
                chain=(),
                source="priority",
                switchable=False,
                empty_reason=self._empty_reason(target, snapshot, state),
            )
        ordered = _direct_first(self._order(usable, snapshot, state), target, snapshot)
        chain, source = self._apply_sticky(ordered, target, state, usable)
        return Decision(chain=chain, source=source)

    def _apply_sticky(
        self,
        chain: tuple[str, ...],
        target: RequestTarget,
        state: RuntimeStateView,
        usable: set[str],
    ) -> tuple[tuple[str, ...], DecisionSource]:
        """把上次成功的出口提到链首，其余保持优先级序。

        粘性出口已不在 ``usable`` 中（被禁用、熔断、有负面记忆、地址族不匹配）
        时**不前置也不清除**：清除是执行层的职责，只有真正尝试失败才累加
        ``fail_count``。路由层因熔断暂时跳过它，不代表这个绑定是错的。
        """
        entry = state.sticky.get(target.host)
        if entry is None or entry.upstream not in usable:
            return chain, "priority"
        head = entry.upstream
        return (head, *(u for u in chain if u != head)), entry.source

    def _forced(
        self,
        hit: RuleMatch,
        target: RequestTarget,
        snapshot: ConfigSnapshot,
        state: RuntimeStateView,
    ) -> Decision:
        """规则命中：候选链长度恒为 1，失败原样返回。

        **不检查熔断与负面记忆**：那是自动路由用来避开坏出口的推断，而规则
        是用户的明确指令。因系统推断而拒绝执行指令，用户会认为规则失效了。

        但「出口被禁用」与「地址族结构性不可达」必须在发起连接**之前**判定：
        链上没有顺延余地，连过去也只是白等一次超时。
        """
        upstream = snapshot.upstream(hit.target)
        if upstream is None:
            # 启动校验会拒绝指向不存在出口的规则（E_RULE_TARGET），正常到不了
            # 这里。仍然处理：崩在热路径上比返回 502 严重得多。
            return self._rule_dead_end(hit, EMPTY_RULE_TARGET_UNKNOWN)
        if not upstream.enabled:
            return self._rule_dead_end(hit, EMPTY_RULE_TARGET_DISABLED)
        if not _family_ok(upstream, target, has_ipv6_egress=state.has_ipv6_egress):
            return self._rule_dead_end(hit, EMPTY_IPV6_UNAVAILABLE)
        return Decision(
            chain=(hit.target,), source="rule", rule_position=hit.position, switchable=False
        )

    def _rule_dead_end(self, hit: RuleMatch, reason: str) -> Decision:
        return Decision(
            chain=(),
            source="rule",
            rule_position=hit.position,
            switchable=False,
            empty_reason=reason,
        )

    def _filter_usable(
        self,
        target: RequestTarget,
        snapshot: ConfigSnapshot,
        state: RuntimeStateView,
        *,
        now: float,
        relaxed: bool,
    ) -> set[str]:
        """四道过滤。``enabled`` 与地址族**永不放宽**：前者是用户的明确指令，
        后者是结构性事实，放宽只会多消耗一次超时。"""
        usable: set[str] = set()
        for u in snapshot.upstreams:
            if not u.enabled:
                continue
            if not _family_ok(u, target, has_ipv6_egress=state.has_ipv6_egress):
                continue
            if not relaxed:
                if not state.health.is_available(u.name, now=now):
                    continue
                if state.memory.is_blocked(target.host, u.name, now=now):
                    continue
            usable.add(u.name)
        return usable

    def _order(
        self, usable: set[str], snapshot: ConfigSnapshot, state: RuntimeStateView
    ) -> tuple[str, ...]:
        chain: list[str] = []
        for priority, members in snapshot.priority_groups:
            present = [m for m in members if m in usable]
            if not present:
                continue
            if len(present) == 1:
                chain.append(present[0])
                continue
            # 游标在构造时推进而非成功时：否则某出口连续失败的场景下
            # 游标不动，每个请求都从同一个坏出口开始。
            offset = state.cursors.next(priority) % len(present)
            chain.extend(present[offset:] + present[:offset])
        return tuple(chain)

    def _empty_reason(
        self, target: RequestTarget, snapshot: ConfigSnapshot, state: RuntimeStateView
    ) -> str:
        """区分「没有出口可用」与「地址族不支持」——日志里这是两种故障。"""
        for u in snapshot.upstreams:
            if u.enabled and not _family_ok(u, target, has_ipv6_egress=state.has_ipv6_egress):
                return EMPTY_IPV6_UNAVAILABLE
        return EMPTY_NO_UPSTREAM


def _direct_first(
    chain: tuple[str, ...], target: RequestTarget, snapshot: ConfigSnapshot
) -> tuple[str, ...]:
    """内网 IP 字面量把 ``direct`` 提到链首（DD_ROUTING §3.2b）。

    优先级链是按「出公网哪条路更好」排的，对内网目标恰好排反了：上级代理通常
    优先级更高，于是第一次访问一台内网主机要先赔满两次连接超时，才轮到那个
    5 毫秒就能成功的 ``direct``。

    **只重排，不裁剪。** 内网不等于直连可达——实测中确实存在只有上级代理到得了
    的网段，把其余出口裁掉会让那部分流量彻底不通。重排之后 :meth:`_apply_sticky`
    仍可能把粘性出口顶到前面，那些已经学会走代理的内网主机因此不受影响：
    本调整只改变「还没学到任何东西」时的第一步。
    """
    if not target.is_private_literal:
        return chain
    direct = next(
        (n for n in chain if (u := snapshot.upstream(n)) is not None and u.is_direct), None
    )
    if direct is None:
        # 被禁用、熔断或不在配置里。不能因为目标是内网就把它塞回链中：
        # 那几道过滤各有各的理由，绕过去只会白等一次超时。
        return chain
    return (direct, *(n for n in chain if n != direct))


def _family_ok(u: UpstreamConfig, target: RequestTarget, *, has_ipv6_egress: bool) -> bool:
    """只有 ``direct`` 由我们自己解析目标，能确定知道地址族。

    经上级代理时目标由上级解析，我们连它有没有 IPv6 地址都不知道——那类
    问题交给 ``(host, upstream)`` 负面记忆自然学习。
    """
    if not u.is_direct:
        return True
    if has_ipv6_egress:
        return True
    return target.family is not AddressFamily.IPV6_ONLY
