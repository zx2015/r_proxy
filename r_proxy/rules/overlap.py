"""无序规则交集检查：索引处理精确/后缀，星号乘积自动机处理其余组合。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from r_proxy.config.validate import ValidationIssue
from r_proxy.rules.glob import HOST_START, HostState, host_accepts, host_step, normalized_host
from r_proxy.rules.matcher import matches_host
from r_proxy.rules.model import PatternKind, Rule

MAX_WORK = 2_000_000
MAX_PAIR_STATES = 100_000
MAX_ISSUES = 200


class AnalysisLimit(Exception):
    """未完成分析不能解释为无交集。"""


@dataclass(slots=True)
class Budget:
    remaining: int = MAX_WORK
    pair_states: int = MAX_PAIR_STATES

    def spend(self, amount: int = 1) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise AnalysisLimit("交集检查超过累计工作上限，请简化规则后重试")


State = tuple[int, int, HostState]


def glob_intersection(
    a: str, b: str, *, domain_only: bool, budget: Budget, ipv4_only: bool = False
) -> str | None:
    """返回真实规范 host 的共同见证；穷尽可达图后才返回无交集。"""
    # 必需字面前/后缀相斥是证明，不是以外观相似度猜测交叠。
    ap, bp = a.split("*", 1)[0], b.split("*", 1)[0]
    az, bz = a.rsplit("*", 1)[-1], b.rsplit("*", 1)[-1]
    if not (ap.startswith(bp) or bp.startswith(ap)):
        return None
    if not (az.endswith(bz) or bz.endswith(az)):
        return None
    alphabet = tuple(sorted((set(a + b) - {"*"}) | set("0123456789abcdefg.-:_")))
    if ipv4_only:
        alphabet = tuple(char for char in alphabet if char in "0123456789.")
    start: State = (0, 0, HOST_START)
    parents: dict[State, tuple[State, str] | None] = {start: None}
    queue = deque([start])

    def visit(state: State, parent: State, char: str) -> None:
        budget.spend()
        if state in parents:
            return
        if len(parents) >= budget.pair_states:
            raise AnalysisLimit("单对通配符交集检查超限，请简化规则后重试")
        parents[state] = parent, char
        queue.append(state)

    while queue:
        state = queue.popleft()
        i, j, host_state = state
        if (
            i == len(a)
            and j == len(b)
            and host_accepts(host_state, domain_only=domain_only, ipv4_only=ipv4_only)
        ):
            chars: list[str] = []
            cursor = state
            while (parent := parents[cursor]) is not None:
                cursor, char = parent
                if char:
                    chars.append(char)
            return "".join(reversed(chars))
        if i < len(a) and a[i] == "*":
            visit((i + 1, j, host_state), state, "")
        if j < len(b) and b[j] == "*":
            visit((i, j + 1, host_state), state, "")
        if i == len(a) or j == len(b):
            continue
        left, right = a[i], b[j]
        if left != "*" and right != "*" and left != right:
            continue
        candidates = alphabet if left == right == "*" else (right if left == "*" else left,)
        for char in candidates:
            budget.spend()
            next_host = host_step(host_state, char, domain_only=domain_only, ipv4_only=ipv4_only)
            if next_host is not None:
                visit(
                    (i if left == "*" else i + 1, j if right == "*" else j + 1, next_host),
                    state,
                    char,
                )
    return None


def _patterns(rule: Rule) -> tuple[str, ...]:
    if rule.kind is PatternKind.DOMAIN_SUFFIX:
        suffix = rule.suffix or ""
        return suffix, "*." + suffix
    return (rule.regex.pattern if rule.regex is not None else rule.raw,)


def pair_witness(a: Rule, b: Rule, budget: Budget) -> str | None:
    ipv4_only = a.kind is PatternKind.IPV4_WILDCARD or b.kind is PatternKind.IPV4_WILDCARD
    if ipv4_only and (a.kind is PatternKind.DOMAIN_SUFFIX or b.kind is PatternKind.DOMAIN_SUFFIX):
        budget.spend()
        return None
    budget.spend(1 + len(a.raw) + len(b.raw))
    fixed = {PatternKind.EXACT_HOST, PatternKind.IP_LITERAL}
    if a.kind in fixed or b.kind in fixed:
        specific, other = (a, b) if a.kind in fixed else (b, a)
        host = str(specific.address) if specific.address is not None else specific.exact or ""
        return host if matches_host(other, host) else None
    if a.kind is PatternKind.ANY or b.kind is PatternKind.ANY:
        other = b if a.kind is PatternKind.ANY else a
        if other.kind is PatternKind.ANY:
            return "a"
        if other.kind is PatternKind.DOMAIN_SUFFIX:
            host = other.suffix or ""
            if not normalized_host(host, domain_only=True):
                host = "a." + host
            if (
                normalized_host(host, domain_only=True)
                and matches_host(a, host)
                and matches_host(b, host)
            ):
                return host
            raise AnalysisLimit("域名后缀的交集证据未通过规范 host 校验")
    domain_only = a.kind is PatternKind.DOMAIN_SUFFIX or b.kind is PatternKind.DOMAIN_SUFFIX
    for left in _patterns(a):
        for right in _patterns(b):
            if (
                witness := glob_intersection(
                    left, right, domain_only=domain_only, budget=budget, ipv4_only=ipv4_only
                )
            ) is not None:
                if not matches_host(a, witness) or not matches_host(b, witness):
                    raise AnalysisLimit("生成的交集证据未通过生产匹配器校验")
                return witness
    return None


def find_conflicts(
    rules: Sequence[Rule],
    *,
    work_limit: int = MAX_WORK,
    pair_limit: int = MAX_PAIR_STATES,
    issue_limit: int = MAX_ISSUES,
) -> list[ValidationIssue]:
    """索引构造与诊断有界；相同出口也不能跳过。"""
    issues: list[ValidationIssue] = []
    reported: set[tuple[int, int]] = set()
    budget = Budget(work_limit, pair_limit)

    def check(i: int, j: int) -> None:
        if i == j:
            return
        i, j = min(i, j), max(i, j)
        if (i, j) in reported:
            return
        a, b = rules[i], rules[j]
        if (host := pair_witness(a, b, budget)) is None:
            return
        if len(issues) >= issue_limit:
            raise AnalysisLimit("冲突明细达到返回上限，结果已截断；请分批修复后再校验")
        reported.add((i, j))
        code = "E_RULE_DUPLICATE" if a.dedup_key == b.dedup_key else "E_RULE_OVERLAP"
        issues.append(
            ValidationIssue(
                "error",
                code,
                f"与 {a.location} 存在共同匹配的 host：{host}，请修改后保存",
                b.location,
                {
                    "related_location": a.location,
                    "condition": b.raw,
                    "upstream": b.target,
                    "related_condition": a.raw,
                    "related_upstream": a.target,
                    "example_host": host,
                },
            )
        )

    keys: dict[tuple[str, str], list[int]] = {}
    suffixes: dict[str, list[int]] = {}
    wildcards: list[int] = []
    any_rules: list[int] = []
    try:
        for i, rule in enumerate(rules):
            budget.spend()
            for j in keys.get(rule.dedup_key, ()):
                check(j, i)
            keys.setdefault(rule.dedup_key, []).append(i)
            if rule.kind is PatternKind.DOMAIN_SUFFIX:
                suffixes.setdefault(rule.suffix or "", []).append(i)
            elif rule.kind in {PatternKind.WILDCARD, PatternKind.IPV4_WILDCARD}:
                wildcards.append(i)
            elif rule.kind is PatternKind.ANY:
                any_rules.append(i)
        for i, rule in enumerate(rules):
            if rule.kind not in {PatternKind.EXACT_HOST, PatternKind.DOMAIN_SUFFIX}:
                continue
            name = rule.exact if rule.exact is not None else rule.suffix or ""
            parts = name.split(".")
            for n in range(len(parts)):
                budget.spend(len(parts) - n)
                for j in suffixes.get(".".join(parts[n:]), ()):
                    check(i, j)
        for i in any_rules:
            for j in range(len(rules)):
                check(i, j)
        for i in wildcards:
            for j, rule in enumerate(rules):
                if rule.kind is PatternKind.ANY or (
                    rule.kind in {PatternKind.WILDCARD, PatternKind.IPV4_WILDCARD} and j < i
                ):
                    continue
                check(i, j)
    except AnalysisLimit as exc:
        issues.append(ValidationIssue("error", "E_RULE_CHECK_LIMIT", str(exc), "rules"))
    return issues
