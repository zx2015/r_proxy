"""规则匹配：first match wins、只匹配主机名、后缀分段、IP 规范化、提前退出。

对应设计：docs/design/DD_RULES.md §5，需求 docs/requirements/RULES_CONFIG.md §4.2。
验收点 M5-01 ~ M5-05、M5-09、M5-10。
"""

from __future__ import annotations

import logging

import pytest

from r_proxy.contracts import AddressFamily, Method, RequestTarget
from r_proxy.rules.loader import compile_rules
from r_proxy.rules.matcher import MAX_SUBJECT, match
from r_proxy.rules.model import EMPTY_RULE_SET, RuleSet


def rule_set(*pairs: tuple[str, str]) -> RuleSet:
    """按给定顺序编译规则，数组下标即 `position`。"""
    result = compile_rules([(i, c, u) for i, (c, u) in enumerate(pairs)])
    assert result.errors == [], result.errors
    return result.rule_set


def http(host: str, path: str = "/", *, port: int = 80) -> RequestTarget:
    return RequestTarget(
        host=host,
        port=port,
        method=Method.GET,
        url=f"http://{host}{path}" if port == 80 else f"http://{host}:{port}{path}",
        is_connect=False,
        family=AddressFamily.of_literal(host),
    )


def connect(host: str, port: int = 443) -> RequestTarget:
    return RequestTarget(
        host=host,
        port=port,
        method=Method.CONNECT,
        url=None,
        is_connect=True,
        family=AddressFamily.of_literal(host),
    )


def target_of(rules: RuleSet, target: RequestTarget) -> str | None:
    found = match(rules, target)
    return found.target if found is not None else None


class TestUniqueMatch:
    def test_reordering_disjoint_rules_does_not_change_decision(self) -> None:
        pairs = [("*.github.com", "a"), ("*.gitlab.com", "b"), ("a*b.test", "c")]
        for order in (pairs, list(reversed(pairs))):
            rules = rule_set(*order)
            assert target_of(rules, http("api.github.com")) == "a"
            assert target_of(rules, http("gitlab.com")) == "b"
            assert target_of(rules, http("axxb.test")) == "c"

    def test_match_reports_snapshot_position_and_condition(self) -> None:
        rules = rule_set(("*.github.com", "a"), ("*.gitlab.com", "b"))
        found = match(rules, http("gitlab.com"))
        assert found is not None
        assert (found.position, found.condition) == (1, "*.gitlab.com")

    def test_unmatched_host_falls_through_to_automatic_routing(self) -> None:
        assert match(rule_set(("*.github.com", "direct")), http("example.com")) is None

    def test_empty_rule_set_never_matches(self) -> None:
        assert match(EMPTY_RULE_SET, http("example.com")) is None


class TestHostOnly:
    def test_http_and_connect_hit_the_same_rule(self) -> None:
        """M5-09：v1 里 HTTP 匹配 URL、CONNECT 匹配 host:port，同一条规则会
        对 HTTPS 静默失效。v2 两者都只看主机名。"""
        rules = rule_set(("*.example.com", "proxy-a"))
        assert target_of(rules, http("api.example.com", "/v1")) == "proxy-a"
        assert target_of(rules, connect("api.example.com")) == "proxy-a"

    def test_path_and_port_do_not_change_host_rule_match(self) -> None:
        rules = rule_set(("api.example.com", "a"))
        assert target_of(rules, http("api.example.com", "/admin", port=8080)) == "a"
        assert target_of(rules, connect("api.example.com", port=8443)) == "a"


class TestDomainSuffix:
    def test_suffix_matches_the_domain_itself(self) -> None:
        """M5-02：`*.example.com` 含 apex。"""
        assert target_of(rule_set(("*.example.com", "direct")), http("example.com")) == "direct"

    def test_suffix_matches_subdomains_at_any_depth(self) -> None:
        rules = rule_set(("*.example.com", "direct"))
        for host in ("www.example.com", "api.v2.example.com", "a.b.c.example.com"):
            assert target_of(rules, http(host)) == "direct", host

    def test_suffix_does_not_match_a_longer_label(self) -> None:
        """M5-02：按点分段是关键，字符串包含判断会让 notexample.com 落进来。"""
        rules = rule_set(("*.example.com", "direct"))
        for host in ("notexample.com", "fakeexample.com", "example.com.evil.net"):
            assert match(rules, http(host)) is None, host

    def test_host_is_matched_case_insensitively(self) -> None:
        rules = rule_set(("*.Example.COM", "direct"))
        assert target_of(rules, http("www.example.com")) == "direct"


class TestWildcard:
    def test_star_crosses_dots(self) -> None:
        """M5-04。"""
        assert target_of(rule_set(("192.168.*", "direct")), http("192.168.1.10")) == "direct"

    def test_wildcard_is_anchored(self) -> None:
        """M5-04：`10.192.168.1` 含有 `192.168.` 但不该命中。"""
        assert match(rule_set(("192.168.*", "direct")), http("10.192.168.1")) is None

    def test_dot_in_a_wildcard_is_literal(self) -> None:
        """M5-05。"""
        rules = rule_set(("a.c*", "direct"))
        assert match(rules, http("abcd.test")) is None
        assert target_of(rules, http("a.cdn")) == "direct"

    def test_a_non_apex_star_does_not_match_the_bare_domain(self) -> None:
        """M5-03：`*.example.*` 是通配符而非域名及子域，不含 apex。"""
        rules = rule_set(("*.example.*", "direct"))
        assert match(rules, http("example.com")) is None
        assert target_of(rules, http("www.example.com")) == "direct"


class TestExactHost:
    def test_exact_match(self) -> None:
        rules = rule_set(("api.github.com", "proxy-a"))
        assert target_of(rules, http("api.github.com")) == "proxy-a"

    def test_exact_does_not_match_subdomains(self) -> None:
        assert match(rule_set(("api.github.com", "proxy-a")), http("v2.api.github.com")) is None

    def test_overlapping_exact_and_suffix_are_rejected_instead_of_ranked(self) -> None:
        for pairs in [
            (("api.github.com", "direct"), ("*.github.com", "a")),
            (("*.github.com", "a"), ("api.github.com", "direct")),
        ]:
            result = compile_rules([(i, c, u) for i, (c, u) in enumerate(pairs)])
            assert not result.ok
            assert result.errors[0].code == "E_RULE_OVERLAP"


class TestIpLiterals:
    def test_ipv4_literal(self) -> None:
        assert target_of(rule_set(("127.0.0.1", "direct")), http("127.0.0.1")) == "direct"

    def test_ipv6_written_differently_still_matches(self) -> None:
        """地址比较基于整数值，前导零与 :: 压缩位置差异不影响匹配。"""
        rules = rule_set(("[2001:0db8:0000:0000:0000:0000:0000:0001]", "direct"))
        assert target_of(rules, connect("2001:db8::1")) == "direct"

    def test_ipv6_case_is_irrelevant(self) -> None:
        assert target_of(rule_set(("[2001:DB8::1]", "direct")), connect("2001:db8::1")) == "direct"

    def test_a_suffix_rule_never_matches_an_ip(self) -> None:
        """host 被识别为 IP 后走地址比较分支，`*.1.10` 不该匹配 192.168.1.10。"""
        assert match(rule_set(("*.1.10", "direct")), http("192.168.1.10")) is None

    def test_a_different_address_does_not_match(self) -> None:
        assert match(rule_set(("[2001:db8::1]", "direct")), connect("2001:db8::2")) is None

    def test_ipv4_mapped_ipv6_is_a_distinct_address(self) -> None:
        assert match(rule_set(("192.168.1.1", "direct")), connect("::ffff:192.168.1.1")) is None


class TestLinearBucket:
    def test_an_overlong_host_skips_wildcards_but_keeps_indexed_rules(self) -> None:
        host = "a" * (MAX_SUBJECT + 1)
        assert match(rule_set(("a*", "b")), http(host)) is None
        assert target_of(rule_set(("*", "direct")), http(host)) == "direct"

    def test_the_skip_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        rules = rule_set(("a*", "b"))
        with caplog.at_level(logging.WARNING, logger="r_proxy.rules.matcher"):
            assert match(rules, http("a" * (MAX_SUBJECT + 1))) is None
        assert "主机名过长" in caplog.text

    def test_host_at_limit_is_matched(self) -> None:
        assert target_of(rule_set(("a*", "b")), http("a" * MAX_SUBJECT)) == "b"


class TestAnyPattern:
    def test_star_matches_everything_including_ips_and_connect(self) -> None:
        rules = rule_set(("*", "proxy-a"))
        for target in (http("example.com"), connect("2001:db8::1"), http("10.0.0.1")):
            assert target_of(rules, target) == "proxy-a"
