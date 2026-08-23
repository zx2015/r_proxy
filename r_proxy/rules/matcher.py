"""规则匹配：first match wins，只匹配主机名。

对应设计：docs/design/DD_RULES.md §5。

纯函数、无状态、无 I/O。取**位置最靠前**的匹配项——这与 v1 的「取最靠后」
相反（[RULES §4.2](../requirements/RULES_CONFIG.md)）。

匹配对象是主机名，HTTP 与 CONNECT 完全一致。v1 的差异（HTTP 用完整 URL、
CONNECT 用 ``host:port``）会让同一条正则对 HTTP 生效、对 HTTPS 静默失效。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from r_proxy.contracts import RequestTarget
from r_proxy.rules.model import Rule, RuleMatch, RuleSet, parse_ip

logger = logging.getLogger(__name__)

# 线性桶的匹配输入长度上限。Python 的 re 不支持超时，限制输入即限制最坏情况。
# v2 只匹配主机名，而主机名在协议层已受限（DNS 上限 253 字节），这个上限因此
# 只是廉价的兜底，正常流量不会触及。
MAX_SUBJECT = 8192


def match(rule_set: RuleSet, target: RequestTarget) -> RuleMatch | None:
    """返回命中的规则，未命中返回 ``None``（交给自动路由）。

    ``target.host`` 假设已由协议层归一化（小写、去方括号、去末尾点、去端口）。
    归一化放在入口处做一次，保证规则匹配、粘性键、负面记忆键、日志用的是
    同一个 host 字符串。
    """
    if rule_set.is_empty:
        return None

    best: Rule | None = None

    def consider(rules: Iterable[Rule]) -> None:
        nonlocal best
        for rule in rules:
            if best is None or rule.position < best.position:
                best = rule

    host = target.host
    consider(rule_set.any_rules)
    consider(rule_set.exact_index.get(host, ()))

    if (addr := parse_ip(host)) is not None:
        # host 是 IP 时不进入后缀匹配：否则 `*.1.10` 会匹配 192.168.1.10。
        consider(rule_set.ip_index.get(addr, ()))
    else:
        for suffix in _suffix_candidates(host):
            consider(rule_set.suffix_index.get(suffix, ()))

    best = _consider_linear(rule_set, host, best)
    return RuleMatch(rule=best, target=best.target) if best is not None else None


def _consider_linear(rule_set: RuleSet, host: str, best: Rule | None) -> Rule | None:
    """通配符与正则：逐条求值，但可以提前退出。

    ``linear_rules`` 按 ``position`` 升序，因此一旦已有候选比当前规则更靠前，
    后面的规则即使命中也不可能更优。这是 first-match-wins 带来的实际收益——
    last-match-wins 下必须求值全部正则才能确定最靠后的命中项。
    """
    if not rule_set.linear_rules:
        return best
    if len(host) > MAX_SUBJECT:
        logger.warning("主机名过长，跳过通配符与正则规则: %d 字节", len(host))
        return best
    for rule in rule_set.linear_rules:
        if best is not None and rule.position > best.position:
            break
        if rule.regex is not None and rule.regex.search(host):
            best = rule
    return best


def _suffix_candidates(host: str) -> Iterator[str]:
    """``www.api.example.com`` → 自身、``api.example.com``、``example.com``、``com``。

    首项是 host 自身，因此 ``*.example.com`` 能匹配 ``example.com``——域名及
    子域包含 apex。按点分段而非字符串 ``endswith``：分段法从结构上排除了
    ``notexample.com`` 一类误伤。
    """
    parts = host.split(".")
    for i in range(len(parts)):
        yield ".".join(parts[i:])
