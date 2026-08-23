"""本机出口能力探测。

对应设计：docs/design/ARCH_OVERVIEW.md §7。

探测结果决定纯 IPv6 目标是否值得尝试 ``direct``：没有 IPv6 出口时直接跳过，
既不写负面记忆也不计熔断（那是配置/环境问题，不是出口故障）。
"""

from __future__ import annotations

import socket

# 全球单播地址，仅用于让内核挑选源地址；UDP connect 不发任何数据包。
_PROBE_TARGET = ("2001:4860:4860::8888", 53)


def probe_ipv6_egress() -> bool:
    """本机是否有可用的 IPv6 出口。

    用 UDP socket 的 ``connect`` 触发内核路由查找：它不发送任何数据包、
    不产生网络流量，也不会因为对端不可达而阻塞——只检查有没有能到达
    全球 IPv6 地址的路由与源地址。
    """
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0)
            sock.connect(_PROBE_TARGET)
            local_address = sock.getsockname()[0]
    except OSError:
        return False
    return not local_address.startswith("::")
