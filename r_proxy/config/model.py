"""配置的不可变数据契约。

对应设计：docs/design/DD_CONFIG.md §2。

所有类型均为 frozen dataclass。``ConfigSnapshot`` 在热重载时整体替换引用，
绝不原地修改——飞行中的请求持有旧快照直到结束，因此单个请求看到的配置始终自洽。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

UpstreamType = Literal["http", "direct"]

DIRECT_NAME = "direct"

DEFAULT_SWITCH_ON_STATUS = frozenset({403, 407, 408, 429, 451, 502, 503, 504, 511})


def is_ipv6_literal(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


class AddressProblem(StrEnum):
    """``parse_endpoint`` 的拒绝理由。

    六种输入都无法解析，但成因互不相同，必须分别告知：只讲 IPv6 方括号会把
    漏写端口的用户引向更深的坑——照提示写成 ``[10.0.0.1]:8080`` 同样被拒。
    """

    NO_PORT = "no_port"
    NO_HOST = "no_host"
    BARE_IPV6 = "bare_ipv6"
    BRACKET_SYNTAX = "bracket_syntax"
    BRACKET_NOT_IPV6 = "bracket_not_ipv6"
    BAD_PORT = "bad_port"


def parse_endpoint(address: str) -> tuple[str, int] | AddressProblem:
    """解析上级代理的 ``host:port``，失败时返回具体的拒绝理由。

    IPv6 必须带方括号：``2001:db8::1:8080`` 中最后一段既可能是端口也可能是
    地址的一部分，猜错会连到完全不同的主机。校验层据此报 ``E_ADDRESS_FORMAT``。
    """
    text = address.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or not text[end + 1 :].startswith(":"):
            return AddressProblem.BRACKET_SYNTAX
        host = text[1:end]
        raw_port = text[end + 2 :]
        if not is_ipv6_literal(host):
            return AddressProblem.BRACKET_NOT_IPV6
    else:
        host, sep, raw_port = text.rpartition(":")
        if not sep:
            return AddressProblem.NO_PORT
        if ":" in host:
            return AddressProblem.BARE_IPV6

    if not host:
        return AddressProblem.NO_HOST
    if not raw_port.isdigit():
        return AddressProblem.BAD_PORT
    return host, int(raw_port)


def split_host_port(address: str) -> tuple[str, int] | None:
    """``parse_endpoint`` 的布尔化包装，供只关心成败的调用方使用。"""
    parsed = parse_endpoint(address)
    return parsed if isinstance(parsed, tuple) else None


@dataclass(frozen=True, slots=True)
class UpstreamAuth:
    """上级代理认证。本期预留，不参与连接建立。"""

    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    name: str
    type: UpstreamType
    address: str | None
    priority: int = 100
    enabled: bool = True
    connect_timeout: float | None = None
    read_timeout: float | None = None
    auth: UpstreamAuth | None = None

    @property
    def is_direct(self) -> bool:
        return self.type == "direct"

    @property
    def endpoint(self) -> tuple[str, int] | None:
        """上级代理的 ``(host, port)``。``direct`` 或地址非法时为 ``None``。"""
        if self.is_direct or not self.address:
            return None
        return split_host_port(self.address)


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    max_switches_per_host: int = 10
    window_seconds: int = 60


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    enabled: bool = True
    fail_threshold: int = 5
    cooldown_seconds: int = 60


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    switch_on_status: frozenset[int] = DEFAULT_SWITCH_ON_STATUS
    sticky_fail_threshold: int = 3
    # ``auto`` 粘性条目多久没被访问就失效（秒）。``0`` 表示禁用过期。
    # 只作用于 ``auto``：``manual`` 是用户的声明，不随空闲作废（DD_ROUTING §7.7）。
    sticky_ttl: int = 2_592_000
    route_block_ttl: int = 600
    tunnel_probe_window: float = 5.0
    switch_buffer_bytes: int = 65536
    happy_eyeballs_delay: float = 0.25
    status_switch_rate_limit: RateLimitConfig = RateLimitConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    max_client_connections: int = 1000
    max_connections_per_upstream: int = 200
    sticky_cache_size: int = 10000
    route_block_cache_size: int = 50000


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    state_path: Path
    logs_path: Path
    rules_path: Path
    retention_days: int = 30
    max_log_rows: int = 100000
    write_queue_size: int = 10000
    flush_interval_ms: int = 200
    flush_batch_size: int = 500
    backup_keep: int = 10


@dataclass(frozen=True, slots=True)
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 6060


@dataclass(frozen=True, slots=True)
class WebUIConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 6061
    auth_token: str | None = field(default=None, repr=False)
    workers: int = 1


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """不可变配置快照。热重载时整体替换，绝不原地修改。

    通过 :meth:`build` 构造，它负责派生索引的计算与冻结。
    """

    listen: ListenConfig
    webui: WebUIConfig
    database: DatabaseConfig
    routing: RoutingConfig
    limits: LimitsConfig
    upstreams: tuple[UpstreamConfig, ...]
    # 救场开关。false 时忽略全部规则，所有流量走自动路由——规则进库后无法用
    # 文本编辑器修改，这是「一条错规则把自己关在网外」时唯一的逃生口。
    rules_enabled: bool

    config_version: str
    source_path: Path
    loaded_at: float

    # 派生索引，加载时一次性构建，避免热路径重复计算。
    by_name: Mapping[str, UpstreamConfig] = field(compare=False)
    priority_groups: tuple[tuple[int, tuple[str, ...]], ...] = field(compare=False)

    @classmethod
    def build(
        cls,
        *,
        listen: ListenConfig,
        webui: WebUIConfig,
        database: DatabaseConfig,
        routing: RoutingConfig,
        limits: LimitsConfig,
        upstreams: tuple[UpstreamConfig, ...],
        rules_enabled: bool,
        config_version: str,
        source_path: Path,
        loaded_at: float,
    ) -> ConfigSnapshot:
        return cls(
            listen=listen,
            webui=webui,
            database=database,
            routing=routing,
            limits=limits,
            upstreams=upstreams,
            rules_enabled=rules_enabled,
            config_version=config_version,
            source_path=source_path,
            loaded_at=loaded_at,
            by_name=MappingProxyType({u.name: u for u in upstreams}),
            priority_groups=_priority_groups(upstreams),
        )

    def upstream(self, name: str) -> UpstreamConfig | None:
        return self.by_name.get(name)

    def timeout_for(self, name: str) -> tuple[float, float]:
        """返回该出口生效的 ``(connect_timeout, read_timeout)``。"""
        u = self.by_name[name]
        return (
            u.connect_timeout if u.connect_timeout is not None else self.routing.connect_timeout,
            u.read_timeout if u.read_timeout is not None else self.routing.read_timeout,
        )


def _priority_groups(
    upstreams: tuple[UpstreamConfig, ...],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    buckets: dict[int, list[str]] = {}
    for u in upstreams:
        buckets.setdefault(u.priority, []).append(u.name)
    return tuple((p, tuple(buckets[p])) for p in sorted(buckets))
