"""decision/router.py 的候选链构造测试。

对应设计：docs/design/DD_ROUTING.md §2、§3、§5.3。

``source`` 是断言的重点之一：它区分候选链的链首是怎么定的（规则、手动绑定、
上次成功、纯优先级），Web 与请求日志都据此解释路由结果。
"""

from __future__ import annotations

import pytest

from r_proxy.config.model import CircuitBreakerConfig, RoutingConfig
from r_proxy.contracts import AddressFamily, FailureKind, Method, RequestTarget
from r_proxy.decision.router import Router
from r_proxy.rules.loader import compile_rules
from r_proxy.rules.model import EMPTY_RULE_SET, RuleSet
from r_proxy.state.runtime import RuntimeState
from r_proxy.state.sticky import StickyEntry
from tests.conftest import make_snapshot, upstream

BREAKER = RoutingConfig(circuit_breaker=CircuitBreakerConfig(fail_threshold=1, cooldown_seconds=60))


def target(
    host: str = "example.com", family: AddressFamily = AddressFamily.UNKNOWN
) -> RequestTarget:
    return RequestTarget(
        host=host,
        port=443,
        method=Method.CONNECT,
        url=None,
        is_connect=True,
        family=family,
    )


def open_breaker(state: RuntimeState, name: str, *, now: float = 0.0) -> None:
    state.health.record_result(name, ok=False, kind=FailureKind.UPSTREAM_ERROR, now=now)


def rules(*pairs: tuple[str, str]) -> RuleSet:
    """按 (条件, 出口) 顺序编译，数组下标即 `position`。"""
    result = compile_rules([(i, c, u) for i, (c, u) in enumerate(pairs)])
    assert result.errors == [], result.errors
    return result.rule_set


@pytest.fixture
def router() -> Router:
    return Router()


class TestPriorityOrdering:
    def test_orders_by_ascending_priority(self, router: Router) -> None:
        """数字越小越优先。"""
        snap = make_snapshot(
            upstream("c", priority=50), upstream("a", priority=10), upstream("d", priority=100)
        )
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert decision.chain == ("a", "c", "d")

    def test_source_is_priority_when_no_rule_or_sticky_applies(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"))
        state = RuntimeState.from_snapshot(snap)
        assert (
            router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).source == "priority"
        )

    def test_chain_is_switchable_without_a_rule(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"))
        state = RuntimeState.from_snapshot(snap)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).switchable is True

    def test_chain_contains_every_usable_upstream(self, router: Router) -> None:
        """候选链不截断：耗尽全部出口才算失败。"""
        snap = make_snapshot(*[upstream(f"u{i}", priority=i) for i in range(8)])
        state = RuntimeState.from_snapshot(snap)
        assert len(router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).chain) == 8


class TestPrivateTargets:
    """内网 IP 字面量把 ``direct`` 提到链首（DD_ROUTING §3.2b）。

    只重排、不裁剪：内网未必等于直连可达——实测中有的网段只有上级代理到得了，
    裁掉其余出口会让那部分流量彻底不通。
    """

    def test_direct_leads_the_chain_for_a_private_literal(self, router: Router) -> None:
        """冷启动第一次访问内网地址就该走直连，而不是先赔两次上级代理超时。"""
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        chain = router.build_chain(target("192.0.2.100"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert chain.chain == ("direct", "proxy")

    def test_the_other_upstreams_stay_in_the_chain(self, router: Router) -> None:
        """只有上级代理到得了的内网段必须还能顺延过去。"""
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        chain = router.build_chain(target("198.51.100.116"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert "proxy" in chain.chain

    def test_sticky_still_wins_over_the_hoist(self, router: Router) -> None:
        """已经学会走上级代理的内网主机不受影响：粘性仍然定链首。"""
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        state.sticky.restore(
            StickyEntry(host="198.51.100.116", upstream="proxy", source="auto", hit_count=9)
        )
        decision = router.build_chain(
            target("198.51.100.116"), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.chain[0] == "proxy"
        assert decision.source == "auto"

    def test_public_addresses_keep_the_priority_order(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        chain = router.build_chain(target("1.1.1.1"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert chain.chain == ("proxy", "direct")

    def test_a_domain_name_is_not_treated_as_private(self, router: Router) -> None:
        """域名要解析才知道落在哪个网段，而热路径上不做解析——只认字面量。"""
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        chain = router.build_chain(target("nas.internal"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert chain.chain == ("proxy", "direct")

    def test_loopback_leads_with_direct(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        chain = router.build_chain(target("127.0.0.1"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert chain.chain[0] == "direct"

    def test_the_hoist_does_not_resurrect_an_unusable_direct(self, router: Router) -> None:
        """direct 被禁用时不能因为目标是内网就把它塞回链里。"""
        snap = make_snapshot(
            upstream("proxy", priority=10),
            upstream("direct", priority=100, direct=True, enabled=False),
        )
        state = RuntimeState.from_snapshot(snap)
        chain = router.build_chain(target("192.0.2.100"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert chain.chain == ("proxy",)


class TestRoundRobin:
    def test_same_priority_members_alternate_as_chain_head(self, router: Router) -> None:
        """M2-02：连续请求交替作为链首。"""
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=10))
        state = RuntimeState.from_snapshot(snap)
        heads = [
            router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).chain[0]
            for _ in range(4)
        ]
        assert heads == ["a", "b", "a", "b"]

    def test_rotation_keeps_all_members_in_the_chain(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=10))
        state = RuntimeState.from_snapshot(snap)
        router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).chain == (
            "b",
            "a",
        )

    def test_rotation_is_per_priority_group(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10),
            upstream("b", priority=10),
            upstream("c", priority=50),
            upstream("d", priority=50),
        )
        state = RuntimeState.from_snapshot(snap)
        router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).chain == (
            "b",
            "a",
            "d",
            "c",
        )

    def test_single_member_group_does_not_consume_a_cursor(self, router: Router) -> None:
        """取模后偏移恒为 0，推进游标只是无谓的状态变化。"""
        snap = make_snapshot(upstream("solo", priority=10))
        state = RuntimeState.from_snapshot(snap)
        for _ in range(3):
            router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert state.cursors.next(10) == 0

    def test_cursor_advances_when_building_not_when_succeeding(self, router: Router) -> None:
        """只在成功时推进的话，某出口连续失败时每个请求都从同一个坏出口开始。"""
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=10))
        state = RuntimeState.from_snapshot(snap)
        router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert state.cursors.next(10) == 1

    def test_rotation_is_stable_when_a_member_becomes_unavailable(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10),
            upstream("b", priority=10),
            upstream("c", priority=10),
            routing=BREAKER,
        )
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "b")
        chains = {
            router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0).chain
            for _ in range(6)
        }
        assert chains == {("a", "c"), ("c", "a")}


class TestFilters:
    def test_disabled_upstream_is_excluded(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10), upstream("off", priority=20, enabled=False)
        )
        state = RuntimeState.from_snapshot(snap)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).chain == ("a",)

    def test_open_breaker_excludes_the_upstream(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10), upstream("b", priority=20), routing=BREAKER
        )
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "a")
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0).chain == ("b",)

    def test_route_memory_excludes_the_upstream_for_that_host_only(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("blocked.com", "a", now=0.0, reason="route_error")
        assert router.build_chain(
            target("blocked.com"), snap, EMPTY_RULE_SET, state, now=1.0
        ).chain == ("b",)
        assert router.build_chain(
            target("other.com"), snap, EMPTY_RULE_SET, state, now=1.0
        ).chain == ("a", "b")

    def test_expired_route_memory_no_longer_excludes(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10),
            upstream("b", priority=20),
            routing=RoutingConfig(route_block_ttl=60),
        )
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("example.com", "a", now=0.0, reason="route_error")
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=61.0).chain == (
            "a",
            "b",
        )

    def test_half_open_upstream_stays_in_the_chain(self, router: Router) -> None:
        """闸门由执行层在尝试前争取，路由层不能提前把它排除。"""
        snap = make_snapshot(
            upstream("a", priority=10), upstream("b", priority=20), routing=BREAKER
        )
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "a")
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=100.0).chain == (
            "a",
            "b",
        )


class TestAddressFamilyFilter:
    def test_ipv6_only_target_skips_direct_without_ipv6_egress(self, router: Router) -> None:
        """M2-16：结构性不可达，不连接也不写负面记忆。"""
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("2001:db8::1", AddressFamily.IPV6_ONLY), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.chain == ("proxy",)

    def test_ipv6_only_target_keeps_direct_when_egress_exists(self, router: Router) -> None:
        """用全局地址而非 ``2001:db8::``：文档段被 ``ipaddress`` 归为 private，
        会触发内网直连前置（§5.4）而改变链序，本条测的是地址族过滤，不是链序。"""
        snap = make_snapshot(
            upstream("proxy", priority=10), upstream("direct", priority=100, direct=True)
        )
        state = RuntimeState.from_snapshot(snap)
        state.set_ipv6_egress(True)
        decision = router.build_chain(
            target("2606:4700::1111", AddressFamily.IPV6_ONLY), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.chain == ("proxy", "direct")

    def test_domain_target_keeps_direct_even_without_ipv6_egress(self, router: Router) -> None:
        """AF-04：域名的地址族要 DNS 才知道，而决策层禁止 I/O。"""
        snap = make_snapshot(upstream("direct", direct=True))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("example.com", AddressFamily.UNKNOWN), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.chain == ("direct",)

    def test_ipv4_target_keeps_direct(self, router: Router) -> None:
        snap = make_snapshot(upstream("direct", direct=True))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("192.0.2.1", AddressFamily.IPV4_ONLY), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.chain == ("direct",)

    def test_family_filter_does_not_apply_to_upstream_proxies(self, router: Router) -> None:
        """经上级代理时目标由上级解析，我们无从判断地址族。"""
        snap = make_snapshot(upstream("proxy"))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("2001:db8::1", AddressFamily.IPV6_ONLY), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.chain == ("proxy",)


class TestRelaxation:
    def test_all_breakers_open_relaxes_to_the_full_chain(self, router: Router) -> None:
        """宁可试一次，也别让用户完全上不去网。"""
        snap = make_snapshot(
            upstream("a", priority=10), upstream("b", priority=20), routing=BREAKER
        )
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "a")
        open_breaker(state, "b")
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0).chain == (
            "a",
            "b",
        )

    def test_relaxation_ignores_route_memory(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"))
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("example.com", "a", now=0.0, reason="route_error")
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0).chain == ("a",)

    def test_relaxation_never_revives_disabled_upstreams(self, router: Router) -> None:
        """enabled 是用户的明确指令，不是系统的推断。"""
        snap = make_snapshot(upstream("off", enabled=False))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert decision.chain == ()
        assert decision.empty_reason == "no_available_upstream"

    def test_relaxation_never_revives_family_mismatch(self, router: Router) -> None:
        """AF-06：放宽后再试一次的结果必然还是 ENETUNREACH。"""
        snap = make_snapshot(upstream("direct", direct=True), routing=BREAKER)
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "direct")
        decision = router.build_chain(
            target("2001:db8::1", AddressFamily.IPV6_ONLY), snap, EMPTY_RULE_SET, state, now=1.0
        )
        assert decision.chain == ()
        assert decision.empty_reason == "ipv6_unavailable"

    def test_relaxation_preserves_priority_order(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10),
            upstream("b", priority=10),
            upstream("c", priority=50),
            routing=BREAKER,
        )
        state = RuntimeState.from_snapshot(snap)
        for name in ("a", "b", "c"):
            open_breaker(state, name)
        chain = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0).chain
        assert set(chain[:2]) == {"a", "b"}
        assert chain[2] == "c"


class TestEmptyChain:
    def test_no_upstreams_at_all(self, router: Router) -> None:
        snap = make_snapshot()
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert decision.chain == ()
        assert decision.empty_reason == "no_available_upstream"

    def test_only_ipv6_incapable_direct_reports_the_family_reason(self, router: Router) -> None:
        """空链的原因要区分开：日志里「没有出口」和「地址族不支持」是不同的故障。"""
        snap = make_snapshot(upstream("direct", direct=True))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("2001:db8::1", AddressFamily.IPV6_ONLY), snap, EMPTY_RULE_SET, state, now=0.0
        )
        assert decision.empty_reason == "ipv6_unavailable"

    def test_empty_chain_is_not_switchable(self, router: Router) -> None:
        snap = make_snapshot()
        state = RuntimeState.from_snapshot(snap)
        assert (
            router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).switchable is False
        )


class TestForcedRouting:
    """规则命中：跳过优先级链与粘性，失败不切换。"""

    def test_rule_hit_yields_a_single_candidate(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, rules(("*", "b")), state, now=0.0)
        assert decision.chain == ("b",)

    def test_rule_hit_is_not_switchable(self, router: Router) -> None:
        """链上没有顺延余地：规则表达的是「只走这个出口」。"""
        snap = make_snapshot(upstream("a"), upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, rules(("*", "b")), state, now=0.0)
        assert decision.switchable is False
        assert decision.source == "rule"

    def test_rule_position_is_carried_for_diagnosis(self, router: Router) -> None:
        snap = make_snapshot(upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target(), snap, rules(("api.other.com", "b"), ("*", "b")), state, now=0.0
        )
        assert decision.rule_position == 1

    def test_an_unmatched_host_still_goes_through_automatic_routing(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("other.com"), snap, rules(("*.example.com", "b")), state, now=0.0
        )
        assert decision.chain == ("a", "b")
        assert decision.source == "priority"
        assert decision.switchable is True

    def test_a_tripped_breaker_does_not_block_a_rule(self, router: Router) -> None:
        """熔断是系统的推断，规则是用户的指令——让它去尝试并失败，语义更清晰。"""
        snap = make_snapshot(upstream("a"), upstream("b"), routing=BREAKER)
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "b")
        decision = router.build_chain(target(), snap, rules(("*", "b")), state, now=1.0)
        assert decision.chain == ("b",)

    def test_route_memory_does_not_block_a_rule(self, router: Router) -> None:
        snap = make_snapshot(upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        state.memory.block("example.com", "b", now=0.0, reason="route_error")
        decision = router.build_chain(target(), snap, rules(("*", "b")), state, now=1.0)
        assert decision.chain == ("b",)

    def test_a_disabled_target_is_a_dead_end_before_connecting(self, router: Router) -> None:
        """M3-14：不发起连接，给出可诊断的错误——用户要知道的是规则配错了。"""
        snap = make_snapshot(upstream("a"), upstream("off", enabled=False))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, rules(("*", "off")), state, now=0.0)
        assert decision.chain == ()
        assert decision.empty_reason == "rule_target_disabled"
        assert decision.rule_position == 0

    def test_a_disabled_target_never_falls_back_to_another_upstream(self, router: Router) -> None:
        """回退到别的出口等于悄悄违背用户的路由意图。"""
        snap = make_snapshot(upstream("a"), upstream("off", enabled=False))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, rules(("*", "off")), state, now=0.0)
        assert "a" not in decision.chain

    def test_rule_to_direct_with_ipv6_only_target_and_no_egress(self, router: Router) -> None:
        """M3-15：规则不能改变目标的地址族。"""
        snap = make_snapshot(upstream("direct", direct=True), upstream("proxy"))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(
            target("2001:db8::1", AddressFamily.IPV6_ONLY),
            snap,
            rules(("[2001:db8::1]", "direct")),
            state,
            now=0.0,
        )
        assert decision.chain == ()
        assert decision.empty_reason == "ipv6_unavailable"
        assert decision.rule_position == 0

    def test_rule_to_direct_with_ipv6_target_and_egress_present(self, router: Router) -> None:
        snap = make_snapshot(upstream("direct", direct=True))
        state = RuntimeState.from_snapshot(snap)
        state.set_ipv6_egress(True)
        decision = router.build_chain(
            target("2001:db8::1", AddressFamily.IPV6_ONLY),
            snap,
            rules(("[2001:db8::1]", "direct")),
            state,
            now=0.0,
        )
        assert decision.chain == ("direct",)

    def test_a_rule_pointing_at_an_upstream_removed_by_reload(self, router: Router) -> None:
        """启动校验拒绝这种规则，但热路径不能因此崩掉。"""
        snap = make_snapshot(upstream("a"))
        state = RuntimeState.from_snapshot(snap)
        decision = router.build_chain(target(), snap, rules(("*", "gone")), state, now=0.0)
        assert decision.chain == ()
        assert decision.empty_reason == "rule_target_unknown"

    def test_the_cursor_is_untouched_by_a_rule_hit(self, router: Router) -> None:
        """规则跳过优先级链，不该消耗轮询游标。"""
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=10))
        state = RuntimeState.from_snapshot(snap)
        router.build_chain(target(), snap, rules(("*", "b")), state, now=0.0)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).chain == (
            "a",
            "b",
        )


class TestStickyPrepend:
    """粘性把上次成功的出口提到链首，其余保持优先级序（DD_ROUTING §3.3）。"""

    def test_the_sticky_upstream_becomes_the_chain_head(self, router: Router) -> None:
        snap = make_snapshot(
            upstream("a", priority=10), upstream("b", priority=10), upstream("c", priority=50)
        )
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "c", now=0.0)
        decision = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert decision.chain == ("c", "a", "b")

    def test_the_head_is_not_duplicated_further_down_the_chain(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        decision = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0)
        assert decision.chain == ("b", "a")

    def test_the_source_reports_how_the_head_was_chosen(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).source == "auto"

    def test_a_manual_binding_reports_itself_as_manual(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.restore(StickyEntry(host="example.com", upstream="b", source="manual"))
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).source == "manual"

    def test_the_chain_stays_switchable(self, router: Router) -> None:
        """粘性是偏好，不是硬绑定：失败照常沿链顺延。要求「只走某出口」得写规则。"""
        snap = make_snapshot(upstream("a"), upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=0.0).switchable is True

    def test_a_sticky_upstream_in_cooldown_is_neither_prepended_nor_cleared(
        self, router: Router
    ) -> None:
        """清除是执行层的职责。路由层因熔断暂时跳过它，不代表这个绑定是错的。"""
        snap = make_snapshot(
            upstream("a", priority=10), upstream("b", priority=20), routing=BREAKER
        )
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        open_breaker(state, "b")
        decision = router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0)
        assert decision.chain == ("a",)
        assert decision.source == "priority"
        assert state.sticky.get("example.com") is not None

    def test_a_sticky_upstream_with_negative_memory_is_not_prepended(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        state.memory.block("example.com", "b", now=0.0, reason="route_error")
        assert router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0).chain == ("a",)

    def test_a_rule_hit_never_consults_sticky(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"), upstream("b"))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "a", now=0.0)
        decision = router.build_chain(target(), snap, rules(("*", "b")), state, now=0.0)
        assert decision.chain == ("b",)
        assert decision.source == "rule"

    def test_sticky_applies_per_host(self, router: Router) -> None:
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        assert router.build_chain(
            target("other.com"), snap, EMPTY_RULE_SET, state, now=0.0
        ).chain == (
            "a",
            "b",
        )

    def test_expired_auto_sticky_is_not_prepended(self, router: Router) -> None:
        """空闲超 sticky_ttl 的 auto 绑定在路由决策时被惰性删除，等同于没学过。"""
        snap = make_snapshot(
            upstream("a", priority=10),
            upstream("b", priority=20),
            routing=RoutingConfig(sticky_ttl=100),
        )
        state = RuntimeState.from_snapshot(snap)
        state.sticky.record_success("example.com", "b", now=0.0)
        # 150 秒后发起请求，已超 100 秒 TTL
        decision = router.build_chain(target("example.com"), snap, EMPTY_RULE_SET, state, now=150.0)
        assert decision.chain == ("a", "b")
        assert decision.source == "priority"
        # 确认内存已被惰性清除
        assert state.sticky.get("example.com") is None


class TestPurity:
    def test_router_holds_no_per_request_state(self, router: Router) -> None:
        """同一个 Router 实例服务所有请求，不得在实例上缓存请求相关数据。"""
        snap = make_snapshot(upstream("a", priority=10), upstream("b", priority=20))
        state = RuntimeState.from_snapshot(snap)
        first = router.build_chain(target("a.com"), snap, EMPTY_RULE_SET, state, now=0.0)
        second = router.build_chain(target("b.com"), snap, EMPTY_RULE_SET, state, now=0.0)
        assert first.chain == second.chain

    def test_build_chain_does_not_mutate_health_state(self, router: Router) -> None:
        snap = make_snapshot(upstream("a"), routing=BREAKER)
        state = RuntimeState.from_snapshot(snap)
        open_breaker(state, "a")
        before = state.health.snapshot_of("a").consecutive_failures
        router.build_chain(target(), snap, EMPTY_RULE_SET, state, now=1.0)
        assert state.health.snapshot_of("a").consecutive_failures == before
