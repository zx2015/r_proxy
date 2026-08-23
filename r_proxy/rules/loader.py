"""从 ``rules.db`` 读出规则并编译成不可变规则集。

对应设计：docs/design/DD_RULES.md §6。

**一次返回全部问题**，不是发现第一个就抛——与配置校验同样的理由：用户改规则
时希望一次看到所有错误，界面据此高亮多行。因此坏行被跳过，好行照常编译，调用
方按 :attr:`LoadResult.errors` 是否为空决定采不采纳。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from r_proxy.config.validate import ValidationIssue
from r_proxy.rules.condition import ConditionError, classify, looks_risky
from r_proxy.rules.model import EMPTY_RULE_SET, PatternKind, Rule, RuleSet

logger = logging.getLogger(__name__)

E_CONDITION = "E_RULE_CONDITION"
W_DUPLICATE = "W_RULE_DUPLICATE"
W_SHADOWED = "W_RULE_SHADOWED"
W_CATCH_ALL = "W_RULE_CATCH_ALL"
W_REDOS = "W_RULE_REDOS"

# (position, condition, upstream)
RuleRow = tuple[int, str, str]


class RulesSource(Protocol):
    """规则行的来源。用协议而非具体类型，让 ``rules`` 包不依赖 ``storage``。"""

    def read_rules(self) -> tuple[RuleRow, ...]: ...


@dataclass(frozen=True, slots=True)
class LoadResult:
    """加载产物。``errors`` 非空时调用方应保留原有规则集。"""

    rule_set: RuleSet
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def load_rules(source: RulesSource, *, enabled: bool) -> LoadResult:
    """从规则库读取并编译。

    ``enabled`` 为 ``False`` 时**根本不查库**，直接返回空规则集。不是「查出来
    再丢掉」——救场场景下那会白白依赖一个可能已经损坏的库。
    """
    if not enabled:
        logger.warning("规则已被全局禁用（rules.enabled = false），全部流量走自动路由")
        return LoadResult(rule_set=EMPTY_RULE_SET)
    return compile_rules(source.read_rules())


def compile_rules(rows: Sequence[RuleRow]) -> LoadResult:
    """纯函数：规则行 → 规则集 + 问题清单。供加载与 Web 保存前校验共用。"""
    rules: list[Rule] = []
    issues: list[ValidationIssue] = []

    for position, condition, upstream in rows:
        location = f"rules[{position}]"
        try:
            pattern = classify(condition)
        except ConditionError as exc:
            issues.append(ValidationIssue("error", E_CONDITION, str(exc), location))
            continue
        rules.append(
            Rule(
                position=position,
                kind=pattern.kind,
                raw=condition,
                target=upstream,
                suffix=pattern.suffix,
                exact=pattern.exact,
                address=pattern.address,
                regex=pattern.regex,
            )
        )
        if looks_risky(pattern, condition):
            issues.append(
                ValidationIssue("warning", W_REDOS, "正则含嵌套量词，可能导致灾难性回溯", location)
            )

    issues.extend(detect_shadowing(rules))
    return LoadResult(rule_set=RuleSet.build(rules), issues=issues)


def detect_shadowing(rules: Sequence[Rule]) -> list[ValidationIssue]:
    """首匹配胜出下，被前面更宽的条件覆盖的规则永不生效。

    只检测可判定的组合：``*`` 之后的一切、完全重复的条件、域名及子域对其下的
    精确主机与子域。**涉及通配符或正则的包含关系一律不检测**——那在一般情况下
    等价于正则语言包含问题，对回溯正则更是不可行。宁可漏报也不误报：一条被误
    判为「永不生效」的告警会让用户删掉实际有用的规则。
    """
    issues: list[ValidationIssue] = []
    seen: dict[tuple[str, str], Rule] = {}
    suffixes: dict[str, Rule] = {}
    catch_all: Rule | None = None

    for rule in rules:
        if catch_all is not None:
            issues.append(
                ValidationIssue(
                    "warning",
                    W_SHADOWED,
                    f"被 {catch_all.location} 的全匹配规则遮蔽，永远不会生效",
                    rule.location,
                )
            )
        elif (earlier := seen.get(rule.dedup_key)) is not None:
            # 顺序语义反转后文案必须指向**后**出现的那条：v1 是 last match
            # wins，说的是「先出现的那条不生效」。
            issues.append(
                ValidationIssue(
                    "warning",
                    W_DUPLICATE,
                    f"条件与 {earlier.location} 重复，后出现的这条永远不会生效",
                    rule.location,
                )
            )
        elif (broader := _shadowing_suffix(rule, suffixes)) is not None:
            issues.append(
                ValidationIssue(
                    "warning",
                    W_SHADOWED,
                    f"被 {broader.location} 的 *.{broader.suffix} 遮蔽，永远不会生效",
                    rule.location,
                )
            )

        seen.setdefault(rule.dedup_key, rule)
        if rule.kind is PatternKind.ANY:
            if catch_all is None:
                catch_all = rule
                issues.append(
                    ValidationIssue(
                        "warning",
                        W_CATCH_ALL,
                        "全匹配规则会让所有流量强制走指定出口，该出口故障时不会自动切换",
                        rule.location,
                    )
                )
        elif rule.kind is PatternKind.DOMAIN_SUFFIX and rule.suffix is not None:
            suffixes.setdefault(rule.suffix, rule)

    return issues


def _shadowing_suffix(rule: Rule, suffixes: dict[str, Rule]) -> Rule | None:
    """前面是否有一条域名及子域规则覆盖了这一条。

    只对精确主机与更窄的域名及子域判定。IP 字面量不进后缀匹配（匹配器对 IP
    走地址比较分支），因此 ``*.1.10`` 不算遮蔽 ``192.168.1.10``。
    """
    if rule.kind is PatternKind.EXACT_HOST:
        name = rule.exact
    elif rule.kind is PatternKind.DOMAIN_SUFFIX:
        name = rule.suffix
    else:
        return None
    if name is None:
        return None

    parts = name.split(".")
    # 从自身开始逐级剥离，与匹配器的后缀候选生成同一口径。
    for i in range(len(parts)):
        if (broader := suffixes.get(".".join(parts[i:]))) is not None:
            return broader
    return None
