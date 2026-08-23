"""判断一份配置能不能跑。

对应设计：docs/design/DD_CONFIG.md §4。

与加载分离：加载只负责「把文件变成对象」。分开的好处是 Web 界面写回配置时
可以复用同一套校验，而不必真正加载生效。

**一次返回全部问题**，不是发现第一个就抛——用户改配置时希望一次看到所有错误。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from r_proxy.config.model import (
    DIRECT_NAME,
    AddressProblem,
    ConfigSnapshot,
    UpstreamConfig,
    is_ipv6_literal,
    parse_endpoint,
)

Level = Literal["error", "warning"]

# 每条拒绝理由都要给出可照做的写法。笼统的「格式错误」等于让用户去读源码。
ADDRESS_HINTS: Mapping[AddressProblem, str] = {
    AddressProblem.NO_PORT: "缺少端口，需写成 host:port，如 192.168.1.100:8080",
    AddressProblem.NO_HOST: "缺少主机名或 IP，需写成 host:port，如 192.168.1.100:8080",
    AddressProblem.BARE_IPV6: "IPv6 地址必须使用方括号，如 [2001:db8::1]:8080",
    AddressProblem.BRACKET_SYNTAX: "方括号形式需写成 [IPv6]:port，如 [2001:db8::1]:8080",
    AddressProblem.BRACKET_NOT_IPV6: "方括号是 IPv6 字面量专用语法，IPv4 与域名不要加方括号",
    AddressProblem.BAD_PORT: "端口必须是 1..65535 的数字，如 192.168.1.100:8080",
}

# 证明目标已经处理了请求的状态码。把它们放进 switch_on_status 只会白白遍历出口。
FUTILE_SWITCH_STATUS = frozenset({404, 500}) | frozenset(range(520, 527))

MIN_TOKEN_LENGTH = 16
RECOMMENDED_TOKEN_LENGTH = 32

# 每条客户端连接对应一条出口连接，另加监听、数据库、日志等固定开销。
FD_OVERHEAD = 64


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    level: Level
    code: str
    message: str
    location: str | None = None


def validate(
    snapshot: ConfigSnapshot,
    *,
    has_ipv6_egress: bool,
    nofile_limit: int | None = None,
    rule_targets: Mapping[str, str] | None = None,
) -> list[ValidationIssue]:
    """校验配置快照。

    :param has_ipv6_egress: 本机是否具备 IPv6 出口能力，由启动探测提供。
    :param nofile_limit: ``RLIMIT_NOFILE`` 软限制；``None`` 表示跳过该项检查。
    :param rule_targets: 规则中出现的出口名到首次出现位置（``rules[i]``）的映射。
        规则尚未加载时传 ``None``。
    """
    issues: list[ValidationIssue] = []
    _check_upstreams(snapshot, has_ipv6_egress, issues)
    _check_listen(snapshot, issues)
    _check_webui(snapshot, issues)
    _check_routing(snapshot, issues)
    _check_resources(snapshot, nofile_limit, issues)
    _check_rule_targets(snapshot, rule_targets or {}, issues)
    return issues


# --------------------------------------------------------------------------
# 出口
# --------------------------------------------------------------------------


def _check_upstreams(
    s: ConfigSnapshot, has_ipv6_egress: bool, issues: list[ValidationIssue]
) -> None:
    if not any(u.enabled for u in s.upstreams):
        issues.append(
            ValidationIssue(
                "error",
                "E_NO_UPSTREAM",
                "至少需要一个 enabled = true 的出口，否则所有请求都无路可走",
                "upstreams",
            )
        )

    seen: set[str] = set()
    for index, u in enumerate(s.upstreams):
        loc = f"upstreams[{index}]"
        if u.name in seen:
            issues.append(
                ValidationIssue("error", "E_DUP_NAME", f"出口名重复: {u.name}", f"{loc}.name")
            )
        seen.add(u.name)

        if u.name == DIRECT_NAME and not u.is_direct:
            issues.append(
                ValidationIssue(
                    "error",
                    "E_DIRECT_TYPE",
                    f'{DIRECT_NAME} 是保留名称，必须配 type = "direct"',
                    f"{loc}.type",
                )
            )

        if not u.is_direct:
            _check_address(u, loc, has_ipv6_egress, issues)

        if not 1 <= u.priority <= 999:
            issues.append(
                ValidationIssue(
                    "error",
                    "E_PRIORITY_RANGE",
                    f"priority 必须落在 1..999，得到 {u.priority}",
                    f"{loc}.priority",
                )
            )

        for field_name in ("connect_timeout", "read_timeout"):
            value = getattr(u, field_name)
            if value is not None and value <= 0:
                issues.append(
                    ValidationIssue(
                        "error",
                        "E_TIMEOUT_POSITIVE",
                        f"{field_name} 必须为正数，得到 {value}",
                        f"{loc}.{field_name}",
                    )
                )

    enabled = [u for u in s.upstreams if u.enabled]
    if len(enabled) > 1 and len({u.priority for u in enabled}) == 1:
        issues.append(
            ValidationIssue(
                "warning",
                "W_ALL_SAME_PRIORITY",
                "所有出口优先级相同，切换退化为纯轮询，没有故障降级层次",
                "upstreams",
            )
        )


def _check_address(
    u: UpstreamConfig, loc: str, has_ipv6_egress: bool, issues: list[ValidationIssue]
) -> None:
    where = f"{loc}.address"
    if not u.address:
        issues.append(
            ValidationIssue(
                "error", "E_ADDRESS_REQUIRED", 'type = "http" 的出口必须提供 address', where
            )
        )
        return

    parsed = parse_endpoint(u.address)
    if not isinstance(parsed, tuple):
        issues.append(
            ValidationIssue(
                "error",
                "E_ADDRESS_FORMAT",
                f"无法解析为 host:port: {u.address!r}。{ADDRESS_HINTS[parsed]}",
                where,
            )
        )
        return

    host, port = parsed
    if not 1 <= port <= 65535:
        issues.append(
            ValidationIssue("error", "E_ADDRESS_FORMAT", f"端口超出 1..65535: {port}", where)
        )
        return

    if is_ipv6_literal(host) and not has_ipv6_egress:
        issues.append(
            ValidationIssue(
                "warning",
                "W_UPSTREAM_IPV6",
                f"出口 {u.name} 是 IPv6 地址，但本机没有 IPv6 出口能力，该出口将始终不可达",
                where,
            )
        )


# --------------------------------------------------------------------------
# 监听与 Web
# --------------------------------------------------------------------------


def _check_listen(s: ConfigSnapshot, issues: list[ValidationIssue]) -> None:
    if is_ipv6_literal(s.listen.host):
        issues.append(
            ValidationIssue(
                "error",
                "E_LISTEN_IPV6",
                f"入向仅支持 IPv4，listen.host 不能是 IPv6 地址: {s.listen.host}",
                "listen.host",
            )
        )


def _check_webui(s: ConfigSnapshot, issues: list[ValidationIssue]) -> None:
    web = s.webui
    if not web.enabled:
        return

    if is_ipv6_literal(web.host):
        issues.append(
            ValidationIssue(
                "error",
                "E_LISTEN_IPV6",
                f"入向仅支持 IPv4，webui.host 不能是 IPv6 地址: {web.host}",
                "webui.host",
            )
        )

    if web.workers != 1:
        issues.append(
            ValidationIssue(
                "error",
                "E_WEB_WORKERS",
                "必须为 1。多进程会产生第二个数据库写者，破坏单一写者约束",
                "webui.workers",
            )
        )

    # 两个 0 不算冲突：0 表示「由内核分配」，两次分配必然得到不同端口。
    if web.port != 0 and s.listen.port == web.port:
        issues.append(
            ValidationIssue(
                "error",
                "E_PORT_CONFLICT",
                f"代理与 Web 界面不能共用端口 {web.port}",
                "webui.port",
            )
        )

    _check_web_token(web.host, web.auth_token, issues)


def _check_web_token(host: str, token: str | None, issues: list[ValidationIssue]) -> None:
    # 先查这一条：它与「绑在哪」无关。HTTP 头部按 latin-1 传输，非 ASCII 的
    # token 客户端根本发不出去，不在启动时拒绝，用户看到的就是「token 配了但
    # 认证永远失败」，而且毫无线索指向配置。
    if token and not token.isascii():
        issues.append(
            ValidationIssue(
                "error",
                "E_WEB_TOKEN_NON_ASCII",
                "auth_token 只能包含 ASCII 字符：HTTP 头部无法承载非 ASCII 值",
                "webui.auth_token",
            )
        )

    if _is_loopback(host):
        return

    if not token:
        issues.append(
            ValidationIssue(
                "error",
                "E_WEB_TOKEN_REQUIRED",
                f"Web 界面绑定非回环地址 {host} 时必须配置 auth_token"
                f"（也可用环境变量 R_PROXY_WEB_TOKEN 提供）",
                "webui.auth_token",
            )
        )
        return

    if len(token) < MIN_TOKEN_LENGTH:
        issues.append(
            ValidationIssue(
                "error",
                "E_WEB_TOKEN_SHORT",
                f"auth_token 至少需要 {MIN_TOKEN_LENGTH} 个字符，当前 {len(token)}",
                "webui.auth_token",
            )
        )
    elif len(token) < RECOMMENDED_TOKEN_LENGTH:
        issues.append(
            ValidationIssue(
                "warning",
                "W_TOKEN_WEAK",
                f"auth_token 建议至少 {RECOMMENDED_TOKEN_LENGTH} 个字符，当前 {len(token)}",
                "webui.auth_token",
            )
        )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# --------------------------------------------------------------------------
# 路由参数
# --------------------------------------------------------------------------


def _check_routing(s: ConfigSnapshot, issues: list[ValidationIssue]) -> None:
    r = s.routing

    blocking = sorted(c for c in r.switch_on_status if 200 <= c < 400)
    if blocking:
        issues.append(
            ValidationIssue(
                "error",
                "E_SWITCH_STATUS_2XX",
                f"switch_on_status 不能包含 2xx/3xx，它们表示请求已成功: {blocking}",
                "routing.switch_on_status",
            )
        )

    futile = sorted(r.switch_on_status & FUTILE_SWITCH_STATUS)
    if futile:
        issues.append(
            ValidationIssue(
                "warning",
                "W_SWITCH_STATUS_FUTILE",
                f"{futile} 证明目标已处理了请求，换出口不会改变结果，只会增加延迟",
                "routing.switch_on_status",
            )
        )

    for name in ("connect_timeout", "read_timeout", "tunnel_probe_window"):
        value = getattr(r, name)
        if value <= 0:
            issues.append(
                ValidationIssue(
                    "error",
                    "E_TIMEOUT_POSITIVE",
                    f"{name} 必须为正数，得到 {value}",
                    f"routing.{name}",
                )
            )

    # happy_eyeballs_delay 的 0 是「关闭该特性」，不是非法值。
    if r.happy_eyeballs_delay < 0:
        issues.append(
            ValidationIssue(
                "error",
                "E_TIMEOUT_POSITIVE",
                f"happy_eyeballs_delay 不能为负，得到 {r.happy_eyeballs_delay}（0 表示关闭）",
                "routing.happy_eyeballs_delay",
            )
        )


def _check_resources(
    s: ConfigSnapshot, nofile_limit: int | None, issues: list[ValidationIssue]
) -> None:
    if nofile_limit is None:
        return
    needed = s.limits.max_client_connections * 2 + FD_OVERHEAD
    if nofile_limit < needed:
        issues.append(
            ValidationIssue(
                "warning",
                "W_NOFILE_LOW",
                f"当前 RLIMIT_NOFILE={nofile_limit}，建议 ≥ {needed}。"
                f"执行 `ulimit -n {needed}` 或调低 max_client_connections",
                "limits.max_client_connections",
            )
        )


def _check_rule_targets(
    s: ConfigSnapshot, rule_targets: Mapping[str, str], issues: list[ValidationIssue]
) -> None:
    """规则目标不可用拆成两个条件，见 DD_CONFIG §4.2。

    目标名写错永远是 bug，拒绝启动；目标被禁用是用户的刻意行为，只告警。
    """
    for target, location in rule_targets.items():
        upstream = s.upstream(target)
        if upstream is None:
            issues.append(
                ValidationIssue(
                    "error",
                    "E_RULE_TARGET",
                    f"规则指向的出口不存在: {target}",
                    location,
                )
            )
        elif not upstream.enabled:
            issues.append(
                ValidationIssue(
                    "warning",
                    "W_RULE_DISABLED_TARGET",
                    f"规则指向的出口 {target} 已被禁用，命中该规则的请求将返回 502",
                    location,
                )
            )
