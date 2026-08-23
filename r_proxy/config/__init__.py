"""配置加载与校验。

读路径只使用标准库 ``tomllib``；写回配置属于 Web 界面能力，位于 ``r_proxy.web``。
本包严禁导入 ``tomlkit``，否则 ``--no-web`` 形态会失去零第三方依赖的性质。
"""

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

__all__ = [
    "CircuitBreakerConfig",
    "ConfigSnapshot",
    "DatabaseConfig",
    "LimitsConfig",
    "ListenConfig",
    "RateLimitConfig",
    "RoutingConfig",
    "UpstreamAuth",
    "UpstreamConfig",
    "UpstreamType",
    "WebUIConfig",
]
