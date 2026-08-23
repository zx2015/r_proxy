"""config/validate.py 的校验清单测试。

对应设计：docs/design/DD_CONFIG.md §4.1、§4.2
"""

from __future__ import annotations

from pathlib import Path

from r_proxy.config.model import (
    ConfigSnapshot,
    DatabaseConfig,
    LimitsConfig,
    ListenConfig,
    RoutingConfig,
    UpstreamConfig,
    WebUIConfig,
)
from r_proxy.config.validate import ValidationIssue, validate

DIRECT = UpstreamConfig(name="direct", type="direct", address=None)


def snap(
    *upstreams: UpstreamConfig,
    listen: ListenConfig | None = None,
    webui: WebUIConfig | None = None,
    routing: RoutingConfig | None = None,
    limits: LimitsConfig | None = None,
) -> ConfigSnapshot:
    return ConfigSnapshot.build(
        listen=listen or ListenConfig(),
        webui=webui or WebUIConfig(),
        database=DatabaseConfig(
            state_path=Path("/tmp/s.db"),
            logs_path=Path("/tmp/l.db"),
            rules_path=Path("/tmp/r.db"),
        ),
        routing=routing or RoutingConfig(),
        limits=limits or LimitsConfig(),
        upstreams=upstreams or (DIRECT,),
        rules_enabled=True,
        config_version="0" * 16,
        source_path=Path("/tmp/config.toml"),
        loaded_at=0.0,
    )


def codes(issues: list[ValidationIssue], level: str | None = None) -> set[str]:
    return {i.code for i in issues if level is None or i.level == level}


def check(s: ConfigSnapshot, **kw: object) -> list[ValidationIssue]:
    kw.setdefault("has_ipv6_egress", True)
    kw.setdefault("nofile_limit", 65536)
    return validate(s, **kw)  # type: ignore[arg-type]


class TestHappyPath:
    def test_default_config_has_no_errors(self) -> None:
        assert codes(check(snap()), "error") == set()

    def test_issues_are_stable_dataclasses(self) -> None:
        issues = check(snap(UpstreamConfig(name="a", type="http", address="bad")))
        assert issues and all(isinstance(i, ValidationIssue) for i in issues)
        assert all(i.level in ("error", "warning") for i in issues)


class TestUpstreamChecks:
    def test_no_enabled_upstream(self) -> None:
        s = snap(UpstreamConfig(name="direct", type="direct", address=None, enabled=False))
        assert "E_NO_UPSTREAM" in codes(check(s), "error")

    def test_empty_upstream_list(self) -> None:
        s = ConfigSnapshot.build(
            listen=ListenConfig(),
            webui=WebUIConfig(),
            database=DatabaseConfig(
                state_path=Path("/tmp/s"),
                logs_path=Path("/tmp/l"),
                rules_path=Path("/tmp/r"),
            ),
            routing=RoutingConfig(),
            limits=LimitsConfig(),
            upstreams=(),
            rules_enabled=True,
            config_version="0" * 16,
            source_path=Path("/tmp/config.toml"),
            loaded_at=0.0,
        )
        assert "E_NO_UPSTREAM" in codes(check(s), "error")

    def test_duplicate_name(self) -> None:
        s = snap(
            UpstreamConfig(name="p", type="http", address="10.0.0.1:1"),
            UpstreamConfig(name="p", type="http", address="10.0.0.2:1"),
        )
        assert "E_DUP_NAME" in codes(check(s), "error")

    def test_direct_name_must_be_direct_type(self) -> None:
        s = snap(UpstreamConfig(name="direct", type="http", address="10.0.0.1:1"))
        assert "E_DIRECT_TYPE" in codes(check(s), "error")

    def test_http_requires_address(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address=None))
        assert "E_ADDRESS_REQUIRED" in codes(check(s), "error")

    def test_priority_out_of_range(self) -> None:
        s = snap(UpstreamConfig(name="direct", type="direct", address=None, priority=0))
        assert "E_PRIORITY_RANGE" in codes(check(s), "error")
        s = snap(UpstreamConfig(name="direct", type="direct", address=None, priority=1000))
        assert "E_PRIORITY_RANGE" in codes(check(s), "error")

    def test_all_same_priority_warns(self) -> None:
        s = snap(
            UpstreamConfig(name="a", type="http", address="10.0.0.1:1", priority=10),
            UpstreamConfig(name="b", type="http", address="10.0.0.2:1", priority=10),
        )
        assert "W_ALL_SAME_PRIORITY" in codes(check(s), "warning")

    def test_mixed_priority_does_not_warn(self) -> None:
        s = snap(
            UpstreamConfig(name="a", type="http", address="10.0.0.1:1", priority=10),
            UpstreamConfig(name="b", type="http", address="10.0.0.2:1", priority=50),
        )
        assert "W_ALL_SAME_PRIORITY" not in codes(check(s), "warning")


class TestAddressFormat:
    def test_plain_host_port_is_ok(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="proxy.local:8080"))
        assert "E_ADDRESS_FORMAT" not in codes(check(s), "error")

    def test_bracketed_ipv6_is_ok(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="[2001:db8::1]:8080"))
        assert "E_ADDRESS_FORMAT" not in codes(check(s), "error")

    def test_bare_ipv6_is_rejected(self) -> None:
        """无方括号的 IPv6 端点有歧义，必须报错而非猜测。"""
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="2001:db8::1:8080"))
        issues = [i for i in check(s) if i.code == "E_ADDRESS_FORMAT"]
        assert issues
        assert "[" in issues[0].message

    def test_missing_port(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="10.0.0.1"))
        assert "E_ADDRESS_FORMAT" in codes(check(s), "error")

    def test_missing_port_message_names_the_port_not_brackets(self) -> None:
        """漏写端口是最常见的手误，提示必须说端口。

        五条拒绝路径若共用一条讲 IPv6 方括号的文案，用户会照着去写
        ``[10.0.0.1]:8080``——那同样被拒，等于把人引向更深的坑。
        """
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="10.0.0.1"))
        issues = [i for i in check(s) if i.code == "E_ADDRESS_FORMAT"]
        assert issues
        assert "端口" in issues[0].message
        assert "[" not in issues[0].message

    def test_bracketed_non_ipv6_is_rejected_with_reason(self) -> None:
        """方括号是 IPv6 专用语法，套在 IPv4 上要说明白，否则用户无从下手。"""
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="[10.0.0.1]:8080"))
        issues = [i for i in check(s) if i.code == "E_ADDRESS_FORMAT"]
        assert issues
        assert "IPv6" in issues[0].message

    def test_non_numeric_port_is_rejected_with_reason(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="proxy.local:http"))
        issues = [i for i in check(s) if i.code == "E_ADDRESS_FORMAT"]
        assert issues
        assert "端口" in issues[0].message
        assert "[" not in issues[0].message

    def test_port_out_of_range(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="10.0.0.1:70000"))
        assert "E_ADDRESS_FORMAT" in codes(check(s), "error")

    def test_ipv6_upstream_without_ipv6_egress_warns(self) -> None:
        s = snap(DIRECT, UpstreamConfig(name="p", type="http", address="[2001:db8::1]:8080"))
        assert "W_UPSTREAM_IPV6" in codes(check(s, has_ipv6_egress=False), "warning")
        assert "W_UPSTREAM_IPV6" not in codes(check(s, has_ipv6_egress=True), "warning")


class TestListenAndWebUI:
    def test_ipv6_listen_host_is_rejected(self) -> None:
        """入向仅 IPv4，见 PRD §4.2.4。"""
        s = snap(listen=ListenConfig(host="::1"))
        assert "E_LISTEN_IPV6" in codes(check(s), "error")

    def test_ipv6_webui_host_is_rejected(self) -> None:
        s = snap(webui=WebUIConfig(host="::1", auth_token="x" * 32))
        assert "E_LISTEN_IPV6" in codes(check(s), "error")

    def test_workers_must_be_one(self) -> None:
        """多进程会产生第二个数据库写者，破坏单一写者约束。"""
        s = snap(webui=WebUIConfig(workers=4))
        issues = [i for i in check(s) if i.code == "E_WEB_WORKERS"]
        assert issues and "写者" in issues[0].message

    def test_non_loopback_binding_requires_token(self) -> None:
        s = snap(webui=WebUIConfig(host="0.0.0.0"))
        assert "E_WEB_TOKEN_REQUIRED" in codes(check(s), "error")

    def test_loopback_binding_needs_no_token(self) -> None:
        assert "E_WEB_TOKEN_REQUIRED" not in codes(check(snap()), "error")

    def test_short_token_is_rejected(self) -> None:
        s = snap(webui=WebUIConfig(host="0.0.0.0", auth_token="short"))
        assert "E_WEB_TOKEN_SHORT" in codes(check(s), "error")

    def test_medium_token_warns(self) -> None:
        s = snap(webui=WebUIConfig(host="0.0.0.0", auth_token="x" * 20))
        assert "W_TOKEN_WEAK" in codes(check(s), "warning")
        assert codes(check(s), "error") == set()

    def test_long_token_is_clean(self) -> None:
        s = snap(webui=WebUIConfig(host="0.0.0.0", auth_token="x" * 32))
        assert codes(check(s)) == set()

    def test_non_ascii_token_is_rejected(self) -> None:
        """HTTP 头部按 latin-1 传输，非 ASCII 的 token 客户端发不出去。"""
        s = snap(webui=WebUIConfig(host="0.0.0.0", auth_token="令牌" + "x" * 30))
        assert "E_WEB_TOKEN_NON_ASCII" in codes(check(s), "error")

    def test_non_ascii_token_is_rejected_on_loopback_too(self) -> None:
        """回环绑定可以不配 token，但配了就必须是能发出去的 token。"""
        s = snap(webui=WebUIConfig(host="127.0.0.1", auth_token="令牌" + "x" * 30))
        assert "E_WEB_TOKEN_NON_ASCII" in codes(check(s), "error")

    def test_disabled_webui_skips_token_checks(self) -> None:
        s = snap(webui=WebUIConfig(enabled=False, host="0.0.0.0", workers=4))
        assert codes(check(s), "error") == set()

    def test_port_conflict(self) -> None:
        s = snap(listen=ListenConfig(port=6060), webui=WebUIConfig(port=6060))
        assert "E_PORT_CONFLICT" in codes(check(s), "error")

    def test_port_conflict_ignored_when_web_disabled(self) -> None:
        s = snap(listen=ListenConfig(port=6060), webui=WebUIConfig(enabled=False, port=6060))
        assert "E_PORT_CONFLICT" not in codes(check(s), "error")

    def test_two_ephemeral_ports_are_not_a_conflict(self) -> None:
        """``0`` 表示由内核分配，两次分配必然得到不同端口。"""
        s = snap(listen=ListenConfig(port=0), webui=WebUIConfig(port=0))
        assert "E_PORT_CONFLICT" not in codes(check(s), "error")


class TestRoutingChecks:
    def test_2xx_in_switch_on_status_is_rejected(self) -> None:
        s = snap(routing=RoutingConfig(switch_on_status=frozenset({200, 502})))
        assert "E_SWITCH_STATUS_2XX" in codes(check(s), "error")

    def test_3xx_in_switch_on_status_is_rejected(self) -> None:
        s = snap(routing=RoutingConfig(switch_on_status=frozenset({302})))
        assert "E_SWITCH_STATUS_2XX" in codes(check(s), "error")

    def test_futile_status_warns_but_starts(self) -> None:
        """404/500/520-526 证明目标已处理，切换只是浪费时间。"""
        s = snap(routing=RoutingConfig(switch_on_status=frozenset({404, 500, 521})))
        assert "W_SWITCH_STATUS_FUTILE" in codes(check(s), "warning")
        assert codes(check(s), "error") == set()

    def test_non_positive_timeout_is_rejected(self) -> None:
        assert "E_TIMEOUT_POSITIVE" in codes(check(snap(routing=RoutingConfig(connect_timeout=0))))
        assert "E_TIMEOUT_POSITIVE" in codes(check(snap(routing=RoutingConfig(read_timeout=-1))))

    def test_zero_happy_eyeballs_delay_is_allowed(self) -> None:
        """0 表示关闭该特性，不是非法值。"""
        s = snap(routing=RoutingConfig(happy_eyeballs_delay=0.0))
        assert "E_TIMEOUT_POSITIVE" not in codes(check(s), "error")

    def test_upstream_timeout_override_must_be_positive(self) -> None:
        s = snap(UpstreamConfig(name="direct", type="direct", address=None, connect_timeout=0.0))
        assert "E_TIMEOUT_POSITIVE" in codes(check(s), "error")


class TestResourceWarnings:
    def test_low_nofile_warns(self) -> None:
        s = snap(limits=LimitsConfig(max_client_connections=1000))
        issues = [i for i in check(s, nofile_limit=1024) if i.code == "W_NOFILE_LOW"]
        assert issues
        assert "1024" in issues[0].message

    def test_sufficient_nofile_is_silent(self) -> None:
        s = snap(limits=LimitsConfig(max_client_connections=1000))
        assert "W_NOFILE_LOW" not in codes(check(s, nofile_limit=65536), "warning")


class TestRuleTargets:
    """规则目标不可用拆成两个条件，见 DD_CONFIG §4.2。"""

    def test_missing_upstream_is_error(self) -> None:
        issues = check(snap(DIRECT), rule_targets={"ghost": "user.rules:12"})
        assert "E_RULE_TARGET" in codes(issues, "error")

    def test_disabled_upstream_is_only_a_warning(self) -> None:
        s = snap(
            DIRECT,
            UpstreamConfig(name="office", type="http", address="10.0.0.1:1", enabled=False),
        )
        issues = check(s, rule_targets={"office": "user.rules:12"})
        assert "W_RULE_DISABLED_TARGET" in codes(issues, "warning")
        assert codes(issues, "error") == set()

    def test_warning_names_the_rule_location(self) -> None:
        s = snap(
            DIRECT,
            UpstreamConfig(name="office", type="http", address="10.0.0.1:1", enabled=False),
        )
        issues = [
            i
            for i in check(s, rule_targets={"office": "user.rules:12"})
            if i.code == "W_RULE_DISABLED_TARGET"
        ]
        assert issues and issues[0].location == "user.rules:12"

    def test_enabled_target_is_clean(self) -> None:
        s = snap(
            DIRECT,
            UpstreamConfig(name="office", type="http", address="10.0.0.1:1", priority=10),
        )
        assert codes(check(s, rule_targets={"office": "user.rules:12"})) == set()


class TestReportsEverything:
    def test_multiple_errors_all_reported(self) -> None:
        """用户改配置时希望一次看到所有问题，不是修一个再报一个。"""
        s = snap(
            UpstreamConfig(name="direct", type="http", address="bad", priority=0),
            listen=ListenConfig(host="::1"),
            webui=WebUIConfig(workers=4, host="0.0.0.0"),
        )
        found = codes(check(s), "error")
        assert {
            "E_DIRECT_TYPE",
            "E_ADDRESS_FORMAT",
            "E_PRIORITY_RANGE",
            "E_LISTEN_IPV6",
            "E_WEB_WORKERS",
            "E_WEB_TOKEN_REQUIRED",
        } <= found
