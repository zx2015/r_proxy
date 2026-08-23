"""条件识别：一小段文本 → 已编译的模式。

对应设计：docs/design/DD_RULES.md §4，需求：docs/requirements/RULES_CONFIG.md §3。

本模块被两条路径共用：加载时逐条编译，以及 Web 保存前校验。共用一份实现是
「界面校验通过」与「启动能加载」口径一致的唯一保证，否则会出现「存得下、
起不来」。

类型由写法自动推断，用户不需要先选类型。判定顺序必须互斥且无歧义——尤其是
两处「必须早于端口检测」，见 :func:`classify`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from r_proxy.rules.model import ZONE_ID_SEPARATOR, IPAddress, PatternKind, parse_ip

# 单条正则的长度上限。ReDoS 的最坏耗时随模式复杂度增长，限制长度是编译期
# 唯一能做的事——Python 的 re 不支持匹配超时。
MAX_PATTERN_LENGTH = 1000

# 嵌套量词的启发式检测：``(a+)+``、``(x*)*`` 一类。只告警不阻断——判定不可能
# 精确，误杀合法正则的代价比放过一条可疑正则更大。匹配侧另有输入长度上限。
NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*]\)[+*]")

# `host:port` 的形状。带方括号的 IPv6 也要覆盖，否则 `[2001:db8::1]:443`
# 会漏网。
_PORT_SUFFIX = re.compile(r"\A(?:\[.*\]|[^:\[\]]+):\d{1,5}\Z")

# 「域名及子域」的特例形状：`*.` 开头且其余不再含 `*`。
_APEX_WILDCARD = re.compile(r"\A\*\.([^*]+)\Z")


class ConditionError(Exception):
    """条件无法编译。调用方补上 ``rules[i]`` 位置后转成 ``ValidationIssue``。"""


@dataclass(frozen=True, slots=True)
class Pattern:
    """识别结果：类型 + 该类型用到的那一个字段。"""

    kind: PatternKind
    suffix: str | None = None
    exact: str | None = None
    address: IPAddress | None = None
    regex: re.Pattern[str] | None = None

    def fields(self) -> dict[str, object]:
        """构造 :class:`~r_proxy.rules.model.Rule` 时展开用。"""
        return {
            "suffix": self.suffix,
            "exact": self.exact,
            "address": self.address,
            "regex": self.regex,
        }


def classify(text: str) -> Pattern:
    """识别条件类型。

    正则先于 IP 与域名判定：``^`` 与 ``/`` 都不是合法的主机名字符，先判正则
    不会抢走其他类型。IP 先于域名：``127.0.0.1`` 能被 ``ip_address`` 解析，
    落到域名分支会变成一条永不匹配的精确主机规则。

    **端口检测必须排在 IP 与通配符之后。** 冒号在 IPv6 里是合法字符：提前到
    IP 之前会让 ``2001:db8::1`` 报「带端口」，提前到通配符之前会让
    ``2001:db8:*`` 与 ``fd00:*`` 同样报错。
    """
    if not text:
        raise ConditionError("条件不能为空")

    if text == "*":
        return Pattern(PatternKind.ANY)

    if text.startswith("^"):
        return Pattern(PatternKind.REGEX, regex=_compile_regex(text))
    if len(text) >= 2 and text.startswith("/") and text.endswith("/"):
        return Pattern(PatternKind.REGEX, regex=_compile_regex(text[1:-1]))

    # 方括号明示了意图是 IPv6。解析失败必须报错而非退化为域名：退化会让写错
    # 的地址变成一条永不匹配的规则，静默失效且极难发现。
    if text.startswith("[") and text.endswith("]"):
        return Pattern(PatternKind.IP_LITERAL, address=_address(text[1:-1], bracketed=True))

    if (addr := _try_address(text)) is not None:
        return Pattern(PatternKind.IP_LITERAL, address=addr)

    if (apex := _APEX_WILDCARD.match(text)) is not None:
        return Pattern(PatternKind.DOMAIN_SUFFIX, suffix=apex.group(1).lower())
    if "*" in text:
        return Pattern(PatternKind.WILDCARD, regex=compile_wildcard(text))

    if _PORT_SUFFIX.match(text):
        raise ConditionError("条件只匹配主机名，不能带端口")
    if ":" in text:
        raise ConditionError("含冒号但不是合法的 IP 地址；IPv6 请用方括号包裹")

    return Pattern(PatternKind.EXACT_HOST, exact=text.lower())


def compile_wildcard(text: str) -> re.Pattern[str]:
    """``*`` 展开为 ``.*``，其余字符一律字面转义，首尾锚定。

    ``*`` 展开为 ``.*`` 而非 ``[^.]*``：``192.168.*`` 必须能匹配
    ``192.168.1.10``，星号要能跨越点号。

    锚定放进模式而不是让调用方改用 ``fullmatch``：WILDCARD 与 REGEX 共用
    ``linear_rules`` 桶，匹配时统一调 ``search()``，桶内因此不需要按 kind 分支。

    逐字符 ``re.escape`` 而非 ``fnmatch.translate``：后者把 ``?`` 与 ``[seq]``
    也当元字符，且 ``*`` 的实现细节随版本变动。自己转译才能保证「除 ``*`` 外
    一律字面」这条写进文档的承诺。
    """
    body = "".join(".*" if ch == "*" else re.escape(ch) for ch in text.lower())
    return re.compile(rf"\A{body}\Z")


def looks_risky(pattern: Pattern, text: str) -> bool:
    """是否值得给一条 ReDoS 告警。只对用户手写的正则判断。"""
    return pattern.kind is PatternKind.REGEX and NESTED_QUANTIFIER.search(text) is not None


def _compile_regex(pattern: str) -> re.Pattern[str]:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ConditionError(f"正则超过 {MAX_PATTERN_LENGTH} 字符上限")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConditionError(f"正则语法错误: {exc}") from exc


def _address(text: str, *, bracketed: bool) -> IPAddress:
    if (addr := _try_address(text)) is None:
        hint = "方括号内不是合法的 IPv6 地址" if bracketed else "不是合法的 IP 地址"
        raise ConditionError(f"{hint}: {text}")
    return addr


def _try_address(text: str) -> IPAddress | None:
    """zone id 显式报错而非返回 ``None``。

    ``ip_address("fe80::1%eth0")`` 抛 ``ValueError``，若就此当作域名处理，
    用户写的地址会变成一条永不匹配的规则——静默失效，且极难发现。
    """
    if ZONE_ID_SEPARATOR in text:
        raise ConditionError(f"不支持 IPv6 zone id（含 {ZONE_ID_SEPARATOR}）: {text}")
    return parse_ip(text)
