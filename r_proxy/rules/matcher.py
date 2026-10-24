"""已验证互斥规则集的唯一命中查找；position 只用于快照定位。"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from r_proxy.contracts import RequestTarget
from r_proxy.rules.model import PatternKind, Rule, RuleMatch, RuleSet, parse_ip

logger = logging.getLogger(__name__)
MAX_SUBJECT = 8192


def matches_host(rule: Rule, host: str) -> bool:
    """单规则语义供运行时、冲突证据验证共用。host 必须已规范化。"""
    match rule.kind:
        case PatternKind.ANY:
            return True
        case PatternKind.EXACT_HOST:
            return host == rule.exact
        case PatternKind.IP_LITERAL:
            return parse_ip(host) == rule.address
        case PatternKind.DOMAIN_SUFFIX:
            suffix = rule.suffix or ""
            return parse_ip(host) is None and (host == suffix or host.endswith("." + suffix))
        case PatternKind.WILDCARD | PatternKind.IPV4_WILDCARD:
            if rule.kind is PatternKind.IPV4_WILDCARD:
                address = parse_ip(host)
                if address is None or address.version != 4:
                    return False
            return (
                len(host) <= MAX_SUBJECT
                and rule.regex is not None
                and bool(rule.regex.search(host))
            )
        case _:
            return False


def match(rule_set: RuleSet, target: RequestTarget) -> RuleMatch | None:
    """只查询编译时已经证明互斥的快照，不引入类型或位置优先级。"""
    host = target.host
    indexed = rule_set.any_rules or rule_set.exact_index.get(host, ())
    if not indexed:
        if (addr := parse_ip(host)) is not None:
            indexed = rule_set.ip_index.get(addr, ())
        else:
            for suffix in _suffix_candidates(host):
                indexed = rule_set.suffix_index.get(suffix, ())
                if indexed:
                    break
    if indexed:
        return RuleMatch(rule=indexed[0], target=indexed[0].target)
    if len(host) > MAX_SUBJECT:
        if rule_set.linear_rules:
            logger.warning("主机名过长，跳过通配符规则: %d 字节", len(host))
        return None
    for rule in rule_set.linear_rules:
        if matches_host(rule, host):
            return RuleMatch(rule=rule, target=rule.target)
    return None


def _suffix_candidates(host: str) -> Iterator[str]:
    parts = host.split(".")
    for i in range(len(parts)):
        yield ".".join(parts[i:])
