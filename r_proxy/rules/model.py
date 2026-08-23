"""规则的数据契约。

对应设计：docs/design/DD_RULES.md §3。

``RuleSet`` 不可变，热重载时整体替换引用——与 ``ConfigSnapshot`` 同样的理由：
飞行中的请求持有旧规则集直到结束，单个请求看到的路由意图始终自洽。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from ipaddress import IPv4Address, IPv6Address, ip_address
from types import MappingProxyType

IPAddress = IPv4Address | IPv6Address

ZONE_ID_SEPARATOR = "%"


def parse_ip(text: str) -> IPAddress | None:
    """把文本按 IP 字面量解析，不是 IP 则返回 ``None``。

    规则侧与请求侧共用同一个解析器：两边对「什么算 IP」的判断必须一致，
    否则会出现「规则按域名编译、请求按 IP 匹配」这类永不命中的组合。
    zone id 的取舍由调用方决定——规则侧报错，请求侧当作域名。
    """
    try:
        return ip_address(text)
    except ValueError:
        return None


class PatternKind(Enum):
    ANY = auto()  # *
    DOMAIN_SUFFIX = auto()  # *.example.com，含 apex
    WILDCARD = auto()  # 192.168.*，* 可在任意位置
    EXACT_HOST = auto()  # api.github.com
    IP_LITERAL = auto()  # 192.168.1.10 / [2001:db8::1]
    REGEX = auto()  # ^cdn\d+\. 或 /.../


@dataclass(frozen=True, slots=True)
class Rule:
    """一条已编译的规则。``position`` 决定优先级——数值**小**者胜。"""

    position: int
    kind: PatternKind
    raw: str
    target: str

    # 按 kind 使用其中之一。WILDCARD 与 REGEX 共用 regex。
    suffix: str | None = None
    exact: str | None = None
    address: IPAddress | None = None
    regex: re.Pattern[str] | None = None

    @property
    def location(self) -> str:
        """``rules[3]`` 形式，用于校验报告与日志。

        与前端数组下标一一对应，界面据此高亮那一行，不需要任何换算。
        """
        return f"rules[{self.position}]"

    @property
    def dedup_key(self) -> tuple[str, str]:
        """判定「同一个条件写了两遍」用的键。

        用编译后的形态而非原始文本：``[2001:0db8::1]`` 与 ``[2001:db8::1]``
        是同一条规则，写法差异不该逃过重复检测。
        """
        if self.kind is PatternKind.ANY:
            return ("any", "*")
        if self.kind is PatternKind.DOMAIN_SUFFIX:
            return ("suffix", self.suffix or "")
        if self.kind is PatternKind.EXACT_HOST:
            return ("exact", self.exact or "")
        if self.kind is PatternKind.IP_LITERAL:
            return ("ip", str(self.address))
        kind = "wildcard" if self.kind is PatternKind.WILDCARD else "regex"
        return (kind, self.regex.pattern if self.regex is not None else self.raw)


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule: Rule
    target: str

    @property
    def position(self) -> int:
        return self.rule.position

    @property
    def condition(self) -> str:
        return self.rule.raw


@dataclass(frozen=True, slots=True)
class RuleSet:
    """编译后的规则集。通过 :meth:`build` 构造，它负责分桶索引的计算。"""

    rules: tuple[Rule, ...]  # 按 position 升序
    any_rules: tuple[Rule, ...] = ()
    exact_index: Mapping[str, tuple[Rule, ...]] = field(default_factory=dict, compare=False)
    suffix_index: Mapping[str, tuple[Rule, ...]] = field(default_factory=dict, compare=False)
    ip_index: Mapping[IPAddress, tuple[Rule, ...]] = field(default_factory=dict, compare=False)
    # WILDCARD + REGEX：都无法用哈希索引，都靠 pattern.search() 逐条求值。
    # 合并成一个按 position 升序的桶，才能实现匹配时的提前退出。
    linear_rules: tuple[Rule, ...] = ()

    @classmethod
    def build(cls, rules: Iterable[Rule]) -> RuleSet:
        ordered = tuple(sorted(rules, key=lambda r: r.position))
        any_rules: list[Rule] = []
        exact: dict[str, list[Rule]] = {}
        suffix: dict[str, list[Rule]] = {}
        ip: dict[IPAddress, list[Rule]] = {}
        linear: list[Rule] = []

        for rule in ordered:
            match rule.kind:
                case PatternKind.ANY:
                    any_rules.append(rule)
                case PatternKind.EXACT_HOST:
                    exact.setdefault(rule.exact or "", []).append(rule)
                case PatternKind.DOMAIN_SUFFIX:
                    suffix.setdefault(rule.suffix or "", []).append(rule)
                case PatternKind.IP_LITERAL:
                    if rule.address is not None:
                        ip.setdefault(rule.address, []).append(rule)
                case PatternKind.WILDCARD | PatternKind.REGEX:
                    linear.append(rule)

        return cls(
            rules=ordered,
            any_rules=tuple(any_rules),
            exact_index=MappingProxyType({k: tuple(v) for k, v in exact.items()}),
            suffix_index=MappingProxyType({k: tuple(v) for k, v in suffix.items()}),
            ip_index=MappingProxyType({k: tuple(v) for k, v in ip.items()}),
            linear_rules=tuple(linear),
        )

    @property
    def is_empty(self) -> bool:
        return not self.rules

    def targets(self) -> Mapping[str, str]:
        """出口名 → 首次出现的位置，供配置校验判定出口是否存在。

        取首次出现：出口不存在时报错只需指一处，指最靠前的那处最便于修改。
        """
        found: dict[str, str] = {}
        for rule in self.rules:
            found.setdefault(rule.target, rule.location)
        return found


EMPTY_RULE_SET = RuleSet.build(())
