"""出口连通性测试。

对应设计：docs/design/DD_WEB.md §7.3，需求：WEBUI_SPEC.md §2.2。

这是 Web 界面里**唯一会主动发起外部连接**的功能，也因此是典型的 SSRF 入口。
三条约束把它关死：

1. 目标地址只能来自**已配置的出口**，请求体不参与选址（调用方负责查表）
2. 探测目标是本模块的常量，同样不接受请求参数
3. 只回「成功/失败 + 耗时 + 状态码 + 错误类型」，**绝不回响应体**

探测结果**不写入健康状态**：手动诊断不该改变路由行为——一次失败的探测把出口
熔断掉，或一次成功的探测把冷却期抹掉，都会让「测一下」变成「改一下」。
"""

from __future__ import annotations

import dataclasses
import logging
import time

from r_proxy.config.model import RoutingConfig, UpstreamConfig
from r_proxy.contracts import AddressFamily, Method, RequestTarget
from r_proxy.egress.connector import ConnectorError, UpstreamConnector
from r_proxy.web.schemas import ProbeResult

logger = logging.getLogger(__name__)

# 固定 5 秒，不取配置里的 connect_timeout：那可能配到 30 秒，而线程池与事件
# 循环都不该为一次手动诊断挂这么久。
PROBE_TIMEOUT_S = 5.0

# 探测目标写死在代码里。做成请求参数就等于把它变成内网扫描接口；做成配置项是
# 切片 d（settings）的事，届时仍由服务端读配置、不由请求携带。
PROBE_HOST = "www.gstatic.com"
PROBE_PORT = 443


def probe_target() -> RequestTarget:
    return RequestTarget(
        host=PROBE_HOST,
        port=PROBE_PORT,
        method=Method.CONNECT,
        url=None,
        is_connect=True,
        family=AddressFamily.of_literal(PROBE_HOST),
    )


async def probe(cfg: UpstreamConfig, *, connector: UpstreamConnector | None = None) -> ProbeResult:
    """经该出口做一次 CONNECT 探测。

    用与真实请求**同一个** ``UpstreamConnector``：另写一份探测逻辑必然与真实
    路径漂移，而探测的全部价值就在于它反映真实行为。
    """
    target = probe_target()
    # 覆盖两处超时：出口自己配的 connect_timeout 优先级更高，只改 routing
    # 的那一份会被它盖掉。
    upstream = dataclasses.replace(cfg, connect_timeout=PROBE_TIMEOUT_S)
    routing = RoutingConfig(connect_timeout=PROBE_TIMEOUT_S, read_timeout=PROBE_TIMEOUT_S)

    started = time.monotonic()
    try:
        tunnel = await (connector or UpstreamConnector()).connect_tunnel(upstream, target, routing)
    except ConnectorError as exc:
        return _result(cfg, ok=False, started=started, status=None, error=exc.error)
    except (OSError, TimeoutError) as exc:
        # connector 只把它归类过的异常包成 ConnectorError；剩下的仍要收敛成
        # 一个结果对象，否则一次探测失败会变成 500。
        return _result(cfg, ok=False, started=started, status=None, error=type(exc).__name__)
    try:
        return _result(
            cfg,
            ok=tunnel.established,
            started=started,
            status=tunnel.status,
            error=None if tunnel.established else "PROXY_REFUSED_CONNECT",
        )
    finally:
        await tunnel.close()


def _result(
    cfg: UpstreamConfig, *, ok: bool, started: float, status: int | None, error: str | None
) -> ProbeResult:
    elapsed = (time.monotonic() - started) * 1000
    logger.info(
        "连通性测试 %s：%s，%.0fms，状态 %s", cfg.name, "成功" if ok else "失败", elapsed, status
    )
    return ProbeResult(
        name=cfg.name,
        ok=ok,
        elapsed_ms=elapsed,
        target=f"{PROBE_HOST}:{PROBE_PORT}",
        http_status=status,
        error=error,
    )
