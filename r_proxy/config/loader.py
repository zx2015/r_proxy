"""从 TOML 文件构建不可变配置快照。

对应设计：docs/design/DD_CONFIG.md §3。

只使用标准库 ``tomllib``。写回配置需要保留注释，属于 Web 界面能力，
由 ``r_proxy.web.config_writer`` 用 ``tomlkit`` 实现——本模块不涉及。

本模块只负责「把文件变成对象」，不判断这份配置能不能跑；后者是
``r_proxy.config.validate`` 的职责。分开的好处是 Web 写回时可以只做校验
而不真正加载生效。
"""

from __future__ import annotations

import difflib
import hashlib
import os
import time
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from r_proxy.config.model import (
    CircuitBreakerConfig,
    ConfigSnapshot,
    DatabaseConfig,
    LimitsConfig,
    ListenConfig,
    RateLimitConfig,
    RoutingConfig,
    UpstreamAuth,
    UpstreamConfig,
    UpstreamType,
    WebUIConfig,
)

ENV_CONFIG_PATH = "R_PROXY_CONFIG"
ENV_WEB_TOKEN = "R_PROXY_WEB_TOKEN"

DEFAULT_STATE_PATH = "~/.r-proxy/state.db"
DEFAULT_LOGS_PATH = "~/.r-proxy/logs.db"
DEFAULT_RULES_PATH = "~/.r-proxy/rules.db"

E_RULES_FILES_REMOVED = "E_RULES_FILES_REMOVED"


class ConfigError(Exception):
    """配置无法被加载。启动阶段抛出，调用方应打印后以退出码 2 结束。"""


# --------------------------------------------------------------------------
# schema：未知键检查的依据
# --------------------------------------------------------------------------

_SCHEMA: dict[str, frozenset[str]] = {
    "": frozenset({"listen", "webui", "database", "limits", "routing", "rules", "upstreams"}),
    "listen": frozenset({"host", "port"}),
    "webui": frozenset({"enabled", "host", "port", "auth_token", "workers"}),
    "database": frozenset(
        {
            "state_path",
            "logs_path",
            "rules_path",
            "retention_days",
            "max_log_rows",
            "write_queue_size",
            "flush_interval_ms",
            "flush_batch_size",
            "backup_keep",
        }
    ),
    "limits": frozenset(
        {
            "max_client_connections",
            "max_connections_per_upstream",
            "sticky_cache_size",
            "route_block_cache_size",
        }
    ),
    "routing": frozenset(
        {
            "connect_timeout",
            "read_timeout",
            "switch_on_status",
            "sticky_fail_threshold",
            "sticky_ttl",
            "route_block_ttl",
            "tunnel_probe_window",
            "switch_buffer_bytes",
            "happy_eyeballs_delay",
            "status_switch_rate_limit",
            "circuit_breaker",
        }
    ),
    "routing.status_switch_rate_limit": frozenset({"max_switches_per_host", "window_seconds"}),
    "routing.circuit_breaker": frozenset({"enabled", "fail_threshold", "cooldown_seconds"}),
    # `files` 仍列在这里，但不是为了接受它：留着才能让 _resolve_rules_enabled
    # 给出「已迁移到界面」的专门提示，而不是被未知键检查笼统地拒绝。
    "rules": frozenset({"enabled", "files"}),
    "upstreams[]": frozenset(
        {
            "name",
            "type",
            "address",
            "priority",
            "enabled",
            "connect_timeout",
            "read_timeout",
            "auth",
        }
    ),
    "upstreams[].auth": frozenset({"username", "password"}),
}

# 每个合法键名归属哪些表。用于「合法项写错了表」的定位提示，这是 TOML
# 表头陷阱（DD_CONFIG §1.1.1）最常见的表现形式。
_KEY_OWNERS: dict[str, list[str]] = {}
for _section, _keys in _SCHEMA.items():
    for _k in _keys:
        _KEY_OWNERS.setdefault(_k, []).append(_section)


def load(
    path: Path,
    cli_overrides: Mapping[str, object] | None = None,
) -> ConfigSnapshot:
    """读取并构建配置快照。

    :param cli_overrides: 点分键到值的映射，如 ``{"webui.port": 7061}``。
        优先级：CLI > 环境变量 > 配置文件 > 内置默认值。
    """
    return _build(_read_bytes(path), path, cli_overrides)


def load_text(
    text: str,
    path: Path,
    cli_overrides: Mapping[str, object] | None = None,
) -> ConfigSnapshot:
    """从内存中的文本构建快照，**不读也不写** ``path``。

    供 Web 在写回之前校验候选配置（[DD_CONFIG §5.2](../../docs/design/DD_CONFIG.md)）：
    校验失败时磁盘文件必须一个字节都没动过。

    ``path`` 仍然必须传：``source_path`` 会进快照，错误消息也要指明是哪个文件。
    """
    return _build(text.encode("utf-8"), path, cli_overrides)


def _build(
    raw_bytes: bytes,
    path: Path,
    cli_overrides: Mapping[str, object] | None,
) -> ConfigSnapshot:
    version = hashlib.sha256(raw_bytes).hexdigest()[:16]
    data = _parse(raw_bytes, path)

    _reject_unknown_keys(data, path)

    data = _apply_env(data)
    data = _apply_overrides(data, cli_overrides or {})

    with _errors_prefixed_with(path):
        upstreams = tuple(
            _build_upstream(item, index)
            for index, item in enumerate(_section_list(data, "upstreams"))
        )
        return ConfigSnapshot.build(
            listen=_build_listen(_table(data, "listen")),
            webui=_build_webui(_table(data, "webui")),
            database=_build_database(_table(data, "database")),
            routing=_build_routing(_table(data, "routing")),
            limits=_build_limits(_table(data, "limits")),
            upstreams=upstreams,
            rules_enabled=_resolve_rules_enabled(data),
            config_version=version,
            source_path=path,
            loaded_at=time.time(),
        )


@contextmanager
def _errors_prefixed_with(path: Path) -> Iterator[None]:
    """给构建阶段的报错统一补上文件名。

    取值函数只知道配置项路径（如 ``listen.port``），不知道来自哪个文件。
    在此处统一补全，好过把 path 穿透到每个函数签名里。
    """
    try:
        yield
    except ConfigError as exc:
        message = str(exc)
        if message.startswith(str(path)):
            raise
        raise ConfigError(f"{path}: {message}") from None


def default_config_path() -> Path:
    env = os.environ.get(ENV_CONFIG_PATH)
    return Path(env).expanduser() if env else Path("config.toml")


# --------------------------------------------------------------------------
# 读取与解析
# --------------------------------------------------------------------------


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {path}") from None
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc


def _parse(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: 配置文件必须是 UTF-8 编码") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML 语法错误: {exc}") from exc


# --------------------------------------------------------------------------
# 未知键检查（DD_CONFIG §3.0）
# --------------------------------------------------------------------------


def _reject_unknown_keys(data: Mapping[str, Any], path: Path) -> None:
    issues: list[str] = []
    _walk_table(data, "", "", issues)
    if issues:
        raise ConfigError(f"{path}:\n  " + "\n  ".join(issues))


def _walk_table(table: Mapping[str, Any], section: str, prefix: str, issues: list[str]) -> None:
    allowed = _SCHEMA.get(section)
    if allowed is None:
        return
    for key, value in table.items():
        if key not in allowed:
            issues.append(f"E_UNKNOWN_KEY {prefix}{key}: {_explain_unknown(key, allowed)}")
            continue
        loc = f"{prefix}{key}"
        if section == "" and key == "upstreams" and isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _walk_table(item, "upstreams[]", f"upstreams[{i}].", issues)
        elif isinstance(value, dict):
            _walk_table(value, _child_section(section, key), f"{loc}.", issues)


def _child_section(section: str, key: str) -> str:
    return f"{section}.{key}" if section else key


def _explain_unknown(key: str, allowed: frozenset[str]) -> str:
    owners = [s for s in _KEY_OWNERS.get(key, []) if s != ""]
    if owners:
        where = "、".join(f"[{o}]" for o in owners)
        return f"未知配置项。该项属于 {where}，可能是写错了表"
    close = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
    if close:
        return f"未知配置项，是否想写 {close[0]}？"
    return "未知配置项"


# --------------------------------------------------------------------------
# 覆盖来源（DD_CONFIG §3.1）
# --------------------------------------------------------------------------


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    token = os.environ.get(ENV_WEB_TOKEN)
    if token:
        data = dict(data)
        webui = dict(data.get("webui") or {})
        webui["auth_token"] = token
        data["webui"] = webui
    return data


def _apply_overrides(data: dict[str, Any], overrides: Mapping[str, object]) -> dict[str, Any]:
    if not overrides:
        return data
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    for dotted, value in overrides.items():
        head, _, tail = dotted.partition(".")
        if tail:
            table = dict(data.get(head) or {})
            table[tail] = value
            data[head] = table
        else:
            data[head] = value
    return data


# --------------------------------------------------------------------------
# 取值与类型转换
# --------------------------------------------------------------------------


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是表")
    return value


def _section_list(data: Mapping[str, Any], name: str) -> list[Any]:
    value = data.get(name, [])
    if not isinstance(value, list):
        raise ConfigError(f"{name} 必须是数组表 [[{name}]]")
    return value


def _get_int(table: Mapping[str, Any], key: str, default: int, loc: str) -> int:
    value = table.get(key, default)
    # bool 是 int 的子类，但把 true 当 1 接受会掩盖类型写错。
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{loc}.{key}: 期望整数，得到 {value!r}")
    return int(value)


def _get_float(table: Mapping[str, Any], key: str, default: float, loc: str) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{loc}.{key}: 期望数字，得到 {value!r}")
    return float(value)


def _get_opt_float(table: Mapping[str, Any], key: str, loc: str) -> float | None:
    if key not in table:
        return None
    return _get_float(table, key, 0.0, loc)


def _get_bool(table: Mapping[str, Any], key: str, default: bool, loc: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{loc}.{key}: 期望布尔值，得到 {value!r}")
    return value


def _get_str(table: Mapping[str, Any], key: str, default: str, loc: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{loc}.{key}: 期望字符串，得到 {value!r}")
    return value


def _get_opt_str(table: Mapping[str, Any], key: str, loc: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{loc}.{key}: 期望字符串，得到 {value!r}")
    return value


# --------------------------------------------------------------------------
# 各节构建
# --------------------------------------------------------------------------


def _build_listen(t: Mapping[str, Any]) -> ListenConfig:
    return ListenConfig(
        host=_get_str(t, "host", "127.0.0.1", "listen"),
        port=_get_int(t, "port", 6060, "listen"),
    )


def _build_webui(t: Mapping[str, Any]) -> WebUIConfig:
    return WebUIConfig(
        enabled=_get_bool(t, "enabled", True, "webui"),
        host=_get_str(t, "host", "127.0.0.1", "webui"),
        port=_get_int(t, "port", 6061, "webui"),
        auth_token=_get_opt_str(t, "auth_token", "webui"),
        workers=_get_int(t, "workers", 1, "webui"),
    )


def _build_database(t: Mapping[str, Any]) -> DatabaseConfig:
    return DatabaseConfig(
        state_path=_expand(_get_str(t, "state_path", DEFAULT_STATE_PATH, "database")),
        logs_path=_expand(_get_str(t, "logs_path", DEFAULT_LOGS_PATH, "database")),
        rules_path=_expand(_get_str(t, "rules_path", DEFAULT_RULES_PATH, "database")),
        retention_days=_get_int(t, "retention_days", 30, "database"),
        max_log_rows=_get_int(t, "max_log_rows", 100000, "database"),
        write_queue_size=_get_int(t, "write_queue_size", 10000, "database"),
        flush_interval_ms=_get_int(t, "flush_interval_ms", 200, "database"),
        flush_batch_size=_get_int(t, "flush_batch_size", 500, "database"),
        backup_keep=_get_int(t, "backup_keep", 10, "database"),
    )


def _build_limits(t: Mapping[str, Any]) -> LimitsConfig:
    return LimitsConfig(
        max_client_connections=_get_int(t, "max_client_connections", 1000, "limits"),
        max_connections_per_upstream=_get_int(t, "max_connections_per_upstream", 200, "limits"),
        sticky_cache_size=_get_int(t, "sticky_cache_size", 10000, "limits"),
        route_block_cache_size=_get_int(t, "route_block_cache_size", 50000, "limits"),
    )


def _build_routing(t: Mapping[str, Any]) -> RoutingConfig:
    defaults = RoutingConfig()
    rl = t.get("status_switch_rate_limit", {})
    cb = t.get("circuit_breaker", {})
    if not isinstance(rl, dict) or not isinstance(cb, dict):
        raise ConfigError("routing: status_switch_rate_limit 与 circuit_breaker 必须是表")

    return RoutingConfig(
        connect_timeout=_get_float(t, "connect_timeout", defaults.connect_timeout, "routing"),
        read_timeout=_get_float(t, "read_timeout", defaults.read_timeout, "routing"),
        switch_on_status=_build_switch_on_status(t, defaults),
        sticky_fail_threshold=_get_int(
            t, "sticky_fail_threshold", defaults.sticky_fail_threshold, "routing"
        ),
        sticky_ttl=_get_int(t, "sticky_ttl", defaults.sticky_ttl, "routing"),
        route_block_ttl=_get_int(t, "route_block_ttl", defaults.route_block_ttl, "routing"),
        tunnel_probe_window=_get_float(
            t, "tunnel_probe_window", defaults.tunnel_probe_window, "routing"
        ),
        switch_buffer_bytes=_get_int(
            t, "switch_buffer_bytes", defaults.switch_buffer_bytes, "routing"
        ),
        happy_eyeballs_delay=_get_float(
            t, "happy_eyeballs_delay", defaults.happy_eyeballs_delay, "routing"
        ),
        status_switch_rate_limit=RateLimitConfig(
            max_switches_per_host=_get_int(
                rl, "max_switches_per_host", 10, "routing.status_switch_rate_limit"
            ),
            window_seconds=_get_int(rl, "window_seconds", 60, "routing.status_switch_rate_limit"),
        ),
        circuit_breaker=CircuitBreakerConfig(
            enabled=_get_bool(cb, "enabled", True, "routing.circuit_breaker"),
            fail_threshold=_get_int(cb, "fail_threshold", 5, "routing.circuit_breaker"),
            cooldown_seconds=_get_int(cb, "cooldown_seconds", 60, "routing.circuit_breaker"),
        ),
    )


def _build_switch_on_status(t: Mapping[str, Any], defaults: RoutingConfig) -> frozenset[int]:
    if "switch_on_status" not in t:
        return defaults.switch_on_status
    value = t["switch_on_status"]
    if not isinstance(value, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        raise ConfigError("routing.switch_on_status: 期望整数数组")
    return frozenset(value)


def _build_upstream(item: Any, index: int) -> UpstreamConfig:
    loc = f"upstreams[{index}]"
    if not isinstance(item, dict):
        raise ConfigError(f"{loc} 必须是表")

    name = _get_opt_str(item, "name", loc)
    if not name:
        raise ConfigError(f"{loc}.name 缺失，每个出口必须有唯一名称")

    raw_type = _get_opt_str(item, "type", loc)
    type_: UpstreamType
    if raw_type == "http":
        type_ = "http"
    elif raw_type == "direct":
        type_ = "direct"
    else:
        raise ConfigError(f"{loc}.type 必须是 'http' 或 'direct'，得到 {raw_type!r}")

    return UpstreamConfig(
        name=name,
        type=type_,
        address=_get_opt_str(item, "address", loc),
        priority=_get_int(item, "priority", 100, loc),
        enabled=_get_bool(item, "enabled", True, loc),
        connect_timeout=_get_opt_float(item, "connect_timeout", loc),
        read_timeout=_get_opt_float(item, "read_timeout", loc),
        auth=_build_auth(item.get("auth"), loc),
    )


def _build_auth(value: Any, loc: str) -> UpstreamAuth | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{loc}.auth 必须是表")
    username = _get_opt_str(value, "username", f"{loc}.auth")
    password = _get_opt_str(value, "password", f"{loc}.auth")
    if username is None or password is None:
        raise ConfigError(f"{loc}.auth 需要同时提供 username 与 password")
    return UpstreamAuth(username=username, password=password)


def _resolve_rules_enabled(data: Mapping[str, Any]) -> bool:
    rules = data.get("rules") or {}
    if not isinstance(rules, dict):
        raise ConfigError("[rules] 必须是表")
    if "files" in rules:
        # 不静默忽略：忽略会让用户以为文件里的规则还在生效，而实际上所有流量
        # 都在走自动路由——一个不报错也看不出来的路由行为变化。
        raise ConfigError(
            f"{E_RULES_FILES_REMOVED} rules.files 已废弃：规则改由 Web 管理界面维护并存入 "
            f"rules.db。请删除该键，原有规则按 docs/requirements/RULES_CONFIG.md §9.2 "
            f"的换算表在界面上重录"
        )
    return _get_bool(rules, "enabled", True, "rules")


def _expand(value: str) -> Path:
    return Path(value).expanduser().absolute()
