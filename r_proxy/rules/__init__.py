"""规则引擎：把 ``rules.db`` 里的两列规则表编译成可匹配的不可变规则集。

对应设计：docs/design/DD_RULES.md。

规则命中即终止决策——不查粘性、不走优先级链、失败不切换。匹配本身是纯函数，
本包只在加载时读一次库，热路径上既不读文件也不查数据库。
"""

from r_proxy.rules.condition import ConditionError, Pattern, classify
from r_proxy.rules.loader import LoadResult, compile_rules, load_rules
from r_proxy.rules.matcher import MAX_SUBJECT, match
from r_proxy.rules.model import (
    EMPTY_RULE_SET,
    PatternKind,
    Rule,
    RuleMatch,
    RuleSet,
)

__all__ = [
    "EMPTY_RULE_SET",
    "MAX_SUBJECT",
    "ConditionError",
    "LoadResult",
    "Pattern",
    "PatternKind",
    "Rule",
    "RuleMatch",
    "RuleSet",
    "classify",
    "compile_rules",
    "load_rules",
    "match",
]
