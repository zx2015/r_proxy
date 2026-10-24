"""星号整串匹配与规范 host 接受状态；不执行用户正则。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GlobPattern:
    pattern: str

    def search(self, host: str) -> bool | None:
        """兼容旧调用点的 search 形状，实际始终整串匹配。"""
        pattern = self.pattern
        i = j = 0
        star = -1
        retry = 0
        while j < len(host):
            if i < len(pattern) and pattern[i] == "*":
                star, retry = i, j
                i += 1
            elif i < len(pattern) and pattern[i] == host[j]:
                i += 1
                j += 1
            elif star >= 0:
                retry += 1
                i, j = star + 1, retry
            else:
                return None
        while i < len(pattern) and pattern[i] == "*":
            i += 1
        return True if i == len(pattern) else None


# 非空、末尾非点，IPv4 的 parts/value/digits 用于后缀规则排除 IP。
DomainState = tuple[bool, bool, int, int, int]
# 已完成组的零标志、当前组长度/零标志、压缩位置、待定冒号数。
V6State = tuple[tuple[bool, ...], int, bool, int, int]
HostState = tuple[DomainState | None, V6State | None]
HOST_START: HostState = ((False, False, 0, 0, 0), ((), 0, False, -1, 0))


def normalized_host(host: str, *, domain_only: bool = False, ipv4_only: bool = False) -> bool:
    """验证固定候选也属于自动机使用的同一个规范 host 语言。"""
    state = HOST_START
    for char in host:
        next_state = host_step(state, char, domain_only=domain_only, ipv4_only=ipv4_only)
        if next_state is None:
            return False
        state = next_state
    return host_accepts(state, domain_only=domain_only, ipv4_only=ipv4_only)


def host_step(
    state: HostState, char: str, *, domain_only: bool, ipv4_only: bool = False
) -> HostState | None:
    """共享 normalize_host 的像：小写、非空、无尾点，IP 为规范字面量。"""
    if char.isspace() or ord(char) < 33 or char.lower() != char:
        return None
    domain, v6 = state
    next_domain = _domain_step(domain, char) if domain is not None else None
    if ipv4_only and next_domain is not None and next_domain[2] < 0:
        next_domain = None
    next_v6 = _v6_step(v6, char) if v6 is not None and not domain_only and not ipv4_only else None
    if next_domain is None and next_v6 is None:
        return None
    return next_domain, next_v6


def host_accepts(state: HostState, *, domain_only: bool, ipv4_only: bool = False) -> bool:
    domain, v6 = state
    if domain is not None:
        started, last_dot, parts, _value, digits = domain
        is_ipv4 = parts == 3 and digits > 0
        if (
            started
            and not last_dot
            and (not domain_only or not is_ipv4)
            and (not ipv4_only or is_ipv4)
        ):
            return True
    return not domain_only and not ipv4_only and v6 is not None and _v6_accepts(v6)


def _domain_step(state: DomainState, char: str) -> DomainState | None:
    started, _last_dot, parts, value, digits = state
    # 冒号只能经带方括号 IPv6 进入协议层；开头方括号不能作为普通 host。
    if char == ":" or (not started and char == "["):
        return None
    if parts >= 0:
        if char in "0123456789":
            if digits == 3 or (digits > 0 and value == 0):
                parts = -1
            else:
                value = value * 10 + int(char)
                digits += 1
                if value > 255:
                    parts = -1
        elif char == "." and digits and parts < 3:
            parts += 1
            value = digits = 0
        else:
            parts = -1
    if parts < 0:
        value = digits = 0
    return True, char == ".", parts, value, digits


def _v6_step(state: V6State, char: str) -> V6State | None:
    groups, length, zero, gap, colons = state
    if char in "0123456789abcdef":
        if colons == 1 and not groups:
            return None  # 单个开头冒号不能接十六进制组。
        if length == 4 or (length > 0 and zero):
            return None  # 规范组无前导零。
        if not length:
            return groups, 1, char == "0", gap, 0
        return groups, length + 1, False, gap, 0
    if char != ":":
        return None
    if length:
        if len(groups) >= 7:
            return None
        return (*groups, zero), 0, False, gap, 1
    if colons == 1 and gap < 0:
        return groups, 0, False, len(groups), 2
    if not groups and colons == 0:
        return groups, 0, False, gap, 1
    return None


def _v6_accepts(state: V6State) -> bool:
    groups, length, zero, gap, colons = state
    expanded: tuple[bool, ...]
    if length:
        groups = (*groups, zero)
    elif colons != 2:
        return False
    if gap < 0:
        if len(groups) != 8:
            return False
        expanded = groups
    else:
        missing = 8 - len(groups)
        if missing < 2:
            return False
        expanded = (*groups[:gap], *((True,) * missing), *groups[gap:])
    best_start = best_length = run = 0
    for i, is_zero in enumerate(expanded):
        run = run + 1 if is_zero else 0
        if run > best_length:
            best_start, best_length = i - run + 1, run
    if gap < 0:
        return best_length < 2
    return best_start == gap and best_length == 8 - len(groups)
