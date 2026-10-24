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

from r_proxy.rules.glob import GlobPattern
from r_proxy.rules.model import ZONE_ID_SEPARATOR, IPAddress, PatternKind, parse_ip

# 所有条件（含规范化产物）的统一长度上限。
MAX_PATTERN_LENGTH = 1000

# `host:port` 的形状。带方括号的 IPv6 也要覆盖，否则 `[2001:db8::1]:443`
# 会漏网。
_PORT_SUFFIX = re.compile(r"\A(?:\[.*\]|[^:\[\]]+):\d{1,5}\Z")

# 「域名及子域」的特例形状：`*.` 开头且其余不再含 `*`。
_APEX_WILDCARD = re.compile(r"\A\*\.([^*]+)\Z")


class ConditionError(Exception):
    """条件无法编译；错误码供加载、Web 与只读盘点共用。"""

    def __init__(self, message: str, *, code: str = "E_RULE_CONDITION") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Pattern:
    """识别结果：类型 + 该类型用到的那一个字段。"""

    kind: PatternKind
    suffix: str | None = None
    exact: str | None = None
    address: IPAddress | None = None
    regex: GlobPattern | None = None

    def fields(self) -> dict[str, object]:
        """构造 :class:`~r_proxy.rules.model.Rule` 时展开用。"""
        return {
            "suffix": self.suffix,
            "exact": self.exact,
            "address": self.address,
            "regex": self.regex,
        }


def normalize_condition(text: str) -> str:
    """与请求规范化保持一致；保留普通 glob 的类型，避免 **.host 变为后缀特例。"""
    text = text.strip()
    if not text:
        raise ConditionError("条件不能为空")
    if len(text) > MAX_PATTERN_LENGTH:
        raise ConditionError(f"条件超过 {MAX_PATTERN_LENGTH} 字符上限")
    if text.startswith("^") or text.startswith("/"):
        raise ConditionError(
            "不再支持正则规则，请改为精确 host 或 * 通配符", code="E_RULE_REGEX_UNSUPPORTED"
        )
    if any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in text):
        raise ConditionError("条件不能包含空白或控制字符")
    if "/" in text or "@" in text:
        raise ConditionError("条件只匹配主机名，不能包含 URL、路径或用户信息")
    if text.startswith("["):
        if text.endswith("]"):
            return str(_address(text[1:-1], bracketed=True))
        if "]:" in text:
            raise ConditionError("条件只匹配主机名，不能带端口")
        raise ConditionError("IPv6 地址的方括号未闭合")
    had_tail_dot = text.endswith(".")
    text = text.rstrip(".").lower()
    if not text:
        raise ConditionError("条件不能为空")
    if had_tail_dot and set(text) == {"*"}:
        raise ConditionError("域名后缀不完整，请填写 *.example.com 这样的完整条件")
    if len(text) > MAX_PATTERN_LENGTH:
        raise ConditionError(f"规范化后的条件超过 {MAX_PATTERN_LENGTH} 字符上限")
    if (addr := _try_address(text)) is not None:
        return str(addr)
    return text


def classify(text: str) -> Pattern:
    """识别可验证条件；纯数字IPv4前缀与域名/普通星号语义分离。"""
    text = normalize_condition(text)
    if text == "*":
        return Pattern(PatternKind.ANY)
    if (addr := _try_address(text)) is not None:
        return Pattern(PatternKind.IP_LITERAL, address=addr)
    if "*" in text:
        if text[0] in "0123456789" and "." in text and set(text) <= set("0123456789.*"):
            _validate_ipv4_wildcard(text)
            return Pattern(PatternKind.IPV4_WILDCARD, regex=compile_wildcard(text))
        if ":" in text and any(c not in "0123456789abcdef:.*" for c in text):
            raise ConditionError("条件只匹配主机名，不能带端口")
        if (apex := _APEX_WILDCARD.match(text)) is not None:
            if ":" in apex.group(1):
                raise ConditionError("域名后缀不能包含端口或 IPv6 地址")
            return Pattern(PatternKind.DOMAIN_SUFFIX, suffix=apex.group(1))
        return Pattern(PatternKind.WILDCARD, regex=compile_wildcard(text))
    if _PORT_SUFFIX.match(text):
        raise ConditionError("条件只匹配主机名，不能带端口")
    if ":" in text:
        raise ConditionError("含冒号但不是合法的 IP 地址；IPv6 请用方括号包裹")
    return Pattern(PatternKind.EXACT_HOST, exact=text)


def _validate_ipv4_wildcard(text: str) -> None:
    if text.count(".") > 3 or ".." in text:
        raise ConditionError("IPv4前缀最多四段，不能包含空段")
    fixed = [part for part in text.split(".") if "*" not in part]
    # 首个星号前的部分也是首个/当前IPv4段的必需前缀。
    fixed.extend(part for part in text.split("*", 1)[0].split(".") if part)
    for part in fixed:
        if len(part) > 3 or int(part) > 255 or (len(part) > 1 and part[0] == "0"):
            raise ConditionError("IPv4前缀固定数字段必须为0～255且不能有前导零")


def compile_wildcard(text: str) -> GlobPattern:
    """星号唯一元字符，返回不使用 Python 正则的整串匹配器。"""
    return GlobPattern(re.sub(r"\*+", "*", text.lower()))


def looks_risky(pattern: Pattern, text: str) -> bool:
    """兼容旧诊断调用；新语法没有可执行的用户正则。"""
    return False


def _address(text: str, *, bracketed: bool) -> IPAddress:
    if (addr := _try_address(text)) is None:
        hint = "方括号内不是合法的 IPv6 地址" if bracketed else "不是合法的 IP 地址"
        raise ConditionError(f"{hint}: {text}")
    if bracketed and addr.version != 6:
        raise ConditionError("方括号仅用于 IPv6 地址")
    return addr


def _try_address(text: str) -> IPAddress | None:
    """zone id 显式报错而非返回 ``None``。

    ``ip_address("fe80::1%eth0")`` 抛 ``ValueError``，若就此当作域名处理，
    用户写的地址会变成一条永不匹配的规则——静默失效，且极难发现。
    """
    if ZONE_ID_SEPARATOR in text:
        raise ConditionError(f"不支持 IPv6 zone id（含 {ZONE_ID_SEPARATOR}）: {text}")
    return parse_ip(text)
