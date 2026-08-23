"""config/model.py 的数据契约测试。

对应设计：docs/design/DD_CONFIG.md §2
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from r_proxy.config.model import (
    ConfigSnapshot,
    DatabaseConfig,
    LimitsConfig,
    ListenConfig,
    RoutingConfig,
    UpstreamConfig,
    WebUIConfig,
)


def _snapshot(*upstreams: UpstreamConfig, routing: RoutingConfig | None = None) -> ConfigSnapshot:
    return ConfigSnapshot.build(
        listen=ListenConfig(),
        webui=WebUIConfig(),
        database=DatabaseConfig(
            state_path=Path("/tmp/state.db"),
            logs_path=Path("/tmp/logs.db"),
            rules_path=Path("/tmp/rules.db"),
        ),
        routing=routing or RoutingConfig(),
        limits=LimitsConfig(),
        upstreams=upstreams,
        rules_enabled=True,
        config_version="0" * 16,
        source_path=Path("/tmp/config.toml"),
        loaded_at=0.0,
    )


class TestUpstreamConfig:
    def test_direct_type_is_recognised(self) -> None:
        assert UpstreamConfig(name="direct", type="direct", address=None).is_direct

    def test_http_type_is_not_direct(self) -> None:
        assert not UpstreamConfig(name="p", type="http", address="10.0.0.1:8080").is_direct

    def test_is_frozen(self) -> None:
        u = UpstreamConfig(name="p", type="http", address="10.0.0.1:8080")
        with pytest.raises(dataclasses.FrozenInstanceError):
            u.priority = 1  # type: ignore[misc]

    def test_password_is_not_in_repr(self) -> None:
        from r_proxy.config.model import UpstreamAuth

        auth = UpstreamAuth(username="alice", password="s3cret-do-not-log")
        assert "s3cret-do-not-log" not in repr(auth)
        assert "alice" in repr(auth)


class TestTimeoutInheritance:
    """出口未设置超时时继承全局值，设置了则覆盖。DD_CONFIG §2。"""

    def test_inherits_global_when_unset(self) -> None:
        snap = _snapshot(UpstreamConfig(name="p", type="http", address="10.0.0.1:8080"))
        assert snap.timeout_for("p") == (10.0, 30.0)

    def test_upstream_value_overrides_global(self) -> None:
        snap = _snapshot(
            UpstreamConfig(name="direct", type="direct", address=None, connect_timeout=3.0)
        )
        assert snap.timeout_for("direct") == (3.0, 30.0)

    def test_read_timeout_can_be_overridden_independently(self) -> None:
        snap = _snapshot(
            UpstreamConfig(name="p", type="http", address="1.1.1.1:1", read_timeout=5.0)
        )
        assert snap.timeout_for("p") == (10.0, 5.0)


class TestDerivedIndex:
    def test_lookup_by_name(self) -> None:
        u = UpstreamConfig(name="p", type="http", address="10.0.0.1:8080")
        assert _snapshot(u).upstream("p") is u

    def test_unknown_name_returns_none(self) -> None:
        assert _snapshot().upstream("nope") is None

    def test_priority_groups_sorted_ascending(self) -> None:
        snap = _snapshot(
            UpstreamConfig(name="c", type="direct", address=None, priority=100),
            UpstreamConfig(name="a", type="http", address="1.1.1.1:1", priority=10),
            UpstreamConfig(name="b", type="http", address="1.1.1.2:1", priority=10),
        )
        assert snap.priority_groups == ((10, ("a", "b")), (100, ("c",)))

    def test_by_name_cannot_be_mutated(self) -> None:
        """快照被所有并发请求共享，派生索引必须不可写。"""
        snap = _snapshot(UpstreamConfig(name="p", type="http", address="1.1.1.1:1"))
        with pytest.raises(TypeError):
            snap.by_name["evil"] = None  # type: ignore[index]


class TestDefaults:
    def test_routing_defaults_match_prd(self) -> None:
        r = RoutingConfig()
        assert r.switch_on_status == frozenset({403, 407, 408, 429, 451, 502, 503, 504, 511})
        assert r.sticky_fail_threshold == 3
        assert r.happy_eyeballs_delay == 0.25
        assert r.circuit_breaker.fail_threshold == 5
        assert r.status_switch_rate_limit.max_switches_per_host == 10

    def test_listen_defaults_to_loopback_6060(self) -> None:
        assert (ListenConfig().host, ListenConfig().port) == ("127.0.0.1", 6060)

    def test_webui_defaults_to_loopback_6061(self) -> None:
        w = WebUIConfig()
        assert (w.host, w.port, w.enabled, w.workers) == ("127.0.0.1", 6061, True, 1)

    def test_auth_token_is_not_in_repr(self) -> None:
        assert "hunter2" not in repr(WebUIConfig(auth_token="hunter2"))
