"""互斥规则：所有写入口依赖的集合不变量与星号交集。"""

from itertools import product

import pytest

from r_proxy.contracts import AddressFamily, Method, RequestTarget
from r_proxy.rules.loader import LoadResult, compile_rules
from r_proxy.rules.matcher import match


def compile_conditions(*conditions: str) -> LoadResult:
    return compile_rules([(i, value, "direct") for i, value in enumerate(conditions)])


@pytest.mark.parametrize(
    "a,b,witness",
    [
        ("api.example.com", "*.example.com", "api.example.com"),
        ("*.example.com", "*.api.example.com", "api.example.com"),
        ("api-*.example.com", "*-prod.example.com", "api-prod.example.com"),
        ("192.168.*", "192.168.1.10", "192.168.1.10"),
        ("2001:db8:*", "2001:*", "2001:db8::"),
        ("*", "anything.test", "anything.test"),
        ("a*b", "ab*", "ab"),
    ],
)
def test_any_overlap_is_rejected_even_for_same_upstream(a: str, b: str, witness: str) -> None:
    for conditions in ((a, b), (b, a)):
        result = compile_conditions(*conditions)
        assert not result.ok
        issue = next(i for i in result.errors if i.code == "E_RULE_OVERLAP")
        assert issue.conflict is not None
        host = issue.conflict["example_host"]
        # 证据独立通过两个单规则生产匹配器，不能只断言生成了某字符串。
        for condition in conditions:
            target = RequestTarget(
                host, 443, Method.CONNECT, None, True, AddressFamily.of_literal(host)
            )
            assert match(compile_conditions(condition).rule_set, target) is not None
        for condition in conditions:
            known = RequestTarget(
                witness, 80, Method.GET, None, False, AddressFamily.of_literal(witness)
            )
            assert match(compile_conditions(condition).rule_set, known) is not None


@pytest.mark.parametrize("condition", [r"^api.*$", r"/\.com$/"])
def test_regex_is_rejected_explicitly(condition: str) -> None:
    result = compile_conditions(condition)
    assert not result.ok
    assert result.errors[0].code == "E_RULE_REGEX_UNSUPPORTED"


def test_ip_normalization_and_case_cannot_bypass_duplicate_check() -> None:
    for a, b in [("API.TEST.", "api.test"), ("[2001:0db8::1]", "2001:db8::1")]:
        result = compile_conditions(a, b)
        assert not result.ok
        assert result.errors[0].code == "E_RULE_DUPLICATE"


@pytest.mark.parametrize(
    "a,b", [("*.example.com", "*.example.net"), ("a*", "b*"), ("*.1.10", "192.168.1.10")]
)
def test_disjoint_rules_are_valid(a: str, b: str) -> None:
    assert compile_conditions(a, b).ok


def test_glob_intersection_agrees_with_exhaustive_short_hosts() -> None:
    # 这些短模式的共同见证若存在，长度不会超过字面字符总长度。
    patterns = ["a*", "*b", "a*b", "*a*", "b*a", "a*b*", "b*b"]
    hosts = ["".join(chars) for n in range(1, 7) for chars in product("ab", repeat=n)]
    for a, b in product(patterns, repeat=2):

        def accepts(pattern: str, host: str) -> bool:
            target = RequestTarget(host, 80, Method.GET, None, False, AddressFamily.UNKNOWN)
            return match(compile_conditions(pattern).rule_set, target) is not None

        intersects = any(accepts(a, host) and accepts(b, host) for host in hosts)
        assert (not compile_conditions(a, b).ok) is intersects, (a, b)


def test_resource_exhaustion_is_an_error_not_a_proof_of_disjointness() -> None:
    rows = [(0, "api-*.example.com", "direct"), (1, "*-prod.example.com", "direct")]
    for limits in ({"work_limit": 0}, {"pair_limit": 1}):
        result = compile_rules(rows, **limits)
        assert not result.ok
        assert any(issue.code == "E_RULE_CHECK_LIMIT" for issue in result.errors)


def test_numeric_ipv4_glob_is_disjoint_from_domain_suffix() -> None:
    result = compile_conditions("10.0.96.*", "*.example.com")
    assert result.ok


@pytest.mark.parametrize(
    "condition,host,expected",
    [
        ("192.168.*", "192.168.1.10", True),
        ("192.168.*", "192.168.example.com", False),
        ("192.168.*", "192.168.1.999", False),
        ("192.168.*", "192.168.01.1", False),
        ("192.168.*", "::ffff:c0a8:10a", False),
        ("192.168.1*", "192.168.12.3", True),
        ("192.168.1*", "192.168.2.3", False),
        ("192.168.1*", "192.168.1example.com", False),
        ("10.*.*", "10.1.2.3", True),
        ("10.*.*", "10.example.com", False),
    ],
)
def test_numeric_prefix_requires_a_real_ipv4(condition: str, host: str, expected: bool) -> None:
    rules = compile_conditions(condition)
    assert rules.ok
    assert rules.rule_set.rules[0].kind.name == "IPV4_WILDCARD"
    target = RequestTarget(host, 80, Method.GET, None, False, AddressFamily.of_literal(host))
    assert (match(rules.rule_set, target) is not None) is expected


@pytest.mark.parametrize(
    "a,b,overlap",
    [
        ("192.168.*", "192.*", True),
        ("192.168.*", "192.168.1.10", True),
        ("192.168.*", "*1", True),
        ("10.*.*", "10.*.2*", True),
        ("192.168.*", "10.*", False),
        ("192.168.*", "*.1.10", False),
        ("192.168.*", "192.168.*.example.com", False),
        ("192.168.*", "192.168.example.com", False),
    ],
)
def test_ipv4_scope_is_shared_by_overlap_checker(a: str, b: str, overlap: bool) -> None:
    from ipaddress import IPv4Address

    for conditions in ((a, b), (b, a)):
        result = compile_conditions(*conditions)
        assert result.ok is not overlap
        if overlap:
            issue = next(i for i in result.errors if i.conflict is not None)
            assert issue.conflict is not None
            IPv4Address(issue.conflict["example_host"])


@pytest.mark.parametrize("condition", ["256.*", "192.168.999.*", "01.2.*", "10..*", "1.2.3.4.*"])
def test_invalid_numeric_ipv4_prefix_is_rejected(condition: str) -> None:
    result = compile_conditions(condition)
    assert not result.ok
    assert result.errors[0].code == "E_RULE_CONDITION"


def test_ipv6_constraint_accepts_canonical_addresses_and_rejects_noncanonical_forms() -> None:
    from ipaddress import IPv6Address

    from r_proxy.rules.glob import HOST_START, host_accepts, host_step

    def accepted(host: str) -> bool:
        state = HOST_START
        for char in host:
            next_state = host_step(state, char, domain_only=False)
            if next_state is None:
                return False
            state = next_state
        return host_accepts(state, domain_only=False)

    # 每一种零/非零组布局都走一遍，覆盖最长压缩与相同长度取左侧的规范。
    for flags in product((False, True), repeat=8):
        expanded = ":".join("0" if zero else "a1" for zero in flags)
        canonical = str(IPv6Address(expanded))
        assert accepted(canonical), canonical
        if canonical != expanded:
            assert not accepted(expanded), expanded
    for invalid in (":", ":::1", "2001:db8:", "2001:0db8::1", "1:2:3:4:5:6:7"):
        assert not accepted(invalid), invalid
