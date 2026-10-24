"""规则加载与严格互斥分析，启动、重载、保存和只读盘点共用。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from r_proxy.config.validate import ValidationIssue
from r_proxy.rules.condition import ConditionError, classify
from r_proxy.rules.model import EMPTY_RULE_SET, PatternKind, Rule, RuleSet
from r_proxy.rules.overlap import MAX_ISSUES, MAX_PAIR_STATES, MAX_WORK, find_conflicts

logger = logging.getLogger(__name__)
ANALYSIS_VERSION = "exclusive-v2"
MAX_RULES = 5000
E_CONDITION = "E_RULE_CONDITION"
W_CATCH_ALL = "W_RULE_CATCH_ALL"
# 历史诊断码保留供外部报告/导入兼容；新分析不再产生顺序相关告警。
W_DUPLICATE = "W_RULE_DUPLICATE"
W_SHADOWED = "W_RULE_SHADOWED"
W_REDOS = "W_RULE_REDOS"
RuleRow = tuple[int, str, str]


class RulesSource(Protocol):
    def read_rules(self) -> tuple[RuleRow, ...]: ...


@dataclass(frozen=True, slots=True)
class LoadResult:
    rule_set: RuleSet
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def load_rules(source: RulesSource, *, enabled: bool) -> LoadResult:
    if not enabled:
        logger.warning("规则已被全局禁用（rules.enabled = false），全部流量走自动路由")
        return LoadResult(rule_set=EMPTY_RULE_SET)
    return compile_rules(source.read_rules())


def compile_rules(
    rows: Sequence[RuleRow], *, work_limit: int = MAX_WORK, pair_limit: int = MAX_PAIR_STATES
) -> LoadResult:
    """纯函数：保留提交索引定位；只有 ok 的快照才能发布给请求路径。"""
    if len(rows) > MAX_RULES:
        return LoadResult(
            EMPTY_RULE_SET,
            [
                ValidationIssue(
                    "error", "E_RULE_CHECK_LIMIT", f"规则超过 {MAX_RULES} 条上限", "rules"
                )
            ],
        )
    rules: list[Rule] = []
    issues: list[ValidationIssue] = []
    for position, condition, upstream in rows:
        if len(issues) >= MAX_ISSUES:
            issues.append(
                ValidationIssue(
                    "error", "E_RULE_CHECK_LIMIT", "问题明细达到返回上限，结果已截断", "rules"
                )
            )
            break
        location = f"rules[{position}]"
        try:
            pattern = classify(condition)
        except ConditionError as exc:
            issues.append(ValidationIssue("error", exc.code, str(exc), location))
            continue
        rules.append(
            Rule(
                position=position,
                kind=pattern.kind,
                raw=condition.strip(),
                target=upstream,
                suffix=pattern.suffix,
                exact=pattern.exact,
                address=pattern.address,
                regex=pattern.regex,
            )
        )
        if pattern.kind is PatternKind.ANY:
            issues.append(
                ValidationIssue(
                    "warning",
                    W_CATCH_ALL,
                    "全匹配规则会让所有流量强制走指定出口，该出口故障时不会自动切换",
                    location,
                )
            )
    issues.extend(
        find_conflicts(
            rules,
            work_limit=work_limit,
            pair_limit=pair_limit,
            issue_limit=max(0, MAX_ISSUES - len(issues)),
        )
    )
    return LoadResult(RuleSet.build(rules), issues)


def detect_shadowing(rules: Sequence[Rule]) -> list[ValidationIssue]:
    """兼容旧调用名，实际执行与顺序无关的互斥检查。"""
    return find_conflicts(rules)
