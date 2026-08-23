"""条件识别：六种类型的自动推断与拒绝理由。

对应设计：docs/design/DD_RULES.md §4，需求 docs/requirements/RULES_CONFIG.md §3。
验收点 M5-02 ~ M5-08。
"""

from __future__ import annotations

from ipaddress import ip_address

import pytest

from r_proxy.rules.condition import ConditionError, classify, compile_wildcard, looks_risky
from r_proxy.rules.model import PatternKind


def kind_of(text: str) -> PatternKind:
    return classify(text).kind


def refuse(text: str) -> str:
    with pytest.raises(ConditionError) as excinfo:
        classify(text)
    return str(excinfo.value)


class TestKinds:
    def test_star_is_any(self) -> None:
        assert kind_of("*") is PatternKind.ANY

    def test_apex_wildcard_is_a_domain_suffix(self) -> None:
        pattern = classify("*.Example.COM")
        assert pattern.kind is PatternKind.DOMAIN_SUFFIX
        assert pattern.suffix == "example.com"

    def test_star_elsewhere_is_a_wildcard(self) -> None:
        """M5-03：`*.example.*` 不是 apex 特例形状，落到通配符。"""
        assert kind_of("*.example.*") is PatternKind.WILDCARD
        assert kind_of("192.168.*") is PatternKind.WILDCARD

    def test_caret_is_a_regex(self) -> None:
        pattern = classify(r"^cdn\d+\.example\.com$")
        assert pattern.kind is PatternKind.REGEX
        assert pattern.regex is not None
        assert pattern.regex.search("cdn12.example.com")

    def test_slash_delimited_is_a_regex_without_the_slashes(self) -> None:
        pattern = classify(r"/\.cn$/")
        assert pattern.kind is PatternKind.REGEX
        assert pattern.regex is not None
        assert pattern.regex.pattern == r"\.cn$"

    def test_ipv4_literal(self) -> None:
        pattern = classify("127.0.0.1")
        assert pattern.kind is PatternKind.IP_LITERAL
        assert pattern.address == ip_address("127.0.0.1")

    def test_bracketed_ipv6_literal(self) -> None:
        assert classify("[2001:db8::1]").address == ip_address("2001:db8::1")

    def test_bare_ipv6_literal(self) -> None:
        """条件里没有端口分隔歧义（端口一律不接受），裸写可以。"""
        assert classify("fd00::1").address == ip_address("fd00::1")

    def test_exact_host_lowercases(self) -> None:
        pattern = classify("API.GitHub.com")
        assert pattern.kind is PatternKind.EXACT_HOST
        assert pattern.exact == "api.github.com"

    def test_localhost_is_an_exact_host_not_an_ip(self) -> None:
        assert kind_of("localhost") is PatternKind.EXACT_HOST


class TestOrderOfJudgement:
    def test_ipv6_wildcard_is_not_reported_as_having_a_port(self) -> None:
        """M5-06：端口检测排在通配符之后，否则 `2001:db8:*` 会被误判。"""
        assert kind_of("2001:db8:*") is PatternKind.WILDCARD
        assert kind_of("fd00:*") is PatternKind.WILDCARD

    def test_bare_ipv6_is_not_reported_as_having_a_port(self) -> None:
        """端口检测排在 IP 之后，否则 `2001:db8::1` 会被误判。"""
        assert kind_of("2001:db8::1") is PatternKind.IP_LITERAL

    def test_regex_containing_a_colon_and_digits_is_still_a_regex(self) -> None:
        assert kind_of(r"^example\.com:443$") is PatternKind.REGEX


class TestRejections:
    def test_empty_is_rejected(self) -> None:
        assert refuse("")

    def test_host_with_port_is_rejected(self) -> None:
        """M5-07：条件只匹配主机名。带端口多半是把 v1 的写法搬了过来。"""
        assert "端口" in refuse("example.com:8443")
        assert "端口" in refuse("[2001:db8::1]:443")

    def test_bad_ipv6_in_brackets_errors_instead_of_becoming_a_domain(self) -> None:
        """M5-08：方括号明示了意图。退化为域名会产生一条永不匹配的规则。"""
        assert "IPv6" in refuse("[2001:db8::zz]")

    def test_zone_id_is_rejected(self) -> None:
        assert "zone id" in refuse("fe80::1%eth0")
        assert "zone id" in refuse("[fe80::1%eth0]")

    def test_bare_colon_that_is_not_an_address_is_rejected(self) -> None:
        """`2001:db8:zz` 既不是 IP 也不该变成主机名。"""
        assert "方括号" in refuse("2001:db8:zz")

    def test_invalid_regex_is_rejected(self) -> None:
        assert "正则" in refuse("^https?://(unclosed")

    def test_overlong_regex_is_rejected(self) -> None:
        assert refuse("^" + "a" * 1000)

    def test_a_regex_at_the_length_limit_is_accepted(self) -> None:
        assert kind_of("^" + "a" * 999) is PatternKind.REGEX


class TestWildcardCompilation:
    def test_star_crosses_dots(self) -> None:
        """M5-04：`192.168.*` 必须能匹配 `192.168.1.10`。"""
        assert compile_wildcard("192.168.*").search("192.168.1.10")

    def test_wildcard_is_anchored_at_both_ends(self) -> None:
        """M5-04：`10.192.168.1` 含有 `192.168.` 但不该命中。"""
        assert compile_wildcard("192.168.*").search("10.192.168.1") is None

    def test_dot_is_literal_not_a_regex_metacharacter(self) -> None:
        """M5-05：`a.c` 里的点只能匹配点。"""
        assert compile_wildcard("a.c*").search("abc.example") is None
        assert compile_wildcard("a.c*").search("a.cdn") is not None

    def test_other_metacharacters_are_literal_too(self) -> None:
        """自己转译而非 `fnmatch.translate`：后者把 `?` 与 `[seq]` 也当元字符。"""
        assert compile_wildcard("a?c*").search("abcd") is None
        assert compile_wildcard("a?c*").search("a?cd") is not None

    def test_wildcard_matching_is_case_insensitive_via_lowering(self) -> None:
        assert compile_wildcard("*.EXAMPLE.*").search("www.example.cn")


class TestRedosHeuristic:
    def test_nested_quantifier_is_flagged(self) -> None:
        text = "^(a+)+$"
        assert looks_risky(classify(text), text)

    def test_plain_regex_is_not_flagged(self) -> None:
        text = r"^cdn\d+\."
        assert not looks_risky(classify(text), text)

    def test_wildcard_is_never_flagged(self) -> None:
        """通配符由我们自己转译，不可能含嵌套量词。"""
        text = "*.example.*"
        assert not looks_risky(classify(text), text)
