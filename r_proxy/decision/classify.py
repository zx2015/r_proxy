"""状态码分类与 ``502``/``503``/``504`` 的来源判定。

对应设计：docs/design/DD_SWITCHING.md §4、§5。

分类结果中 ``TARGET_HANDLED`` 与 ``CDN_ORIGIN_ERROR`` 是**硬约束**：即便用户
把 ``521`` 写进 ``switch_on_status`` 也不切换，因为它们证明目标已经参与了对话。
"""

from __future__ import annotations

from enum import Enum, auto

from r_proxy.contracts import Headers


class StatusCategory(Enum):
    TARGET_HANDLED = auto()
    INCOMPLETE_REQUEST = auto()
    PROXY_LAYER = auto()
    AMBIGUOUS = auto()
    EGRESS_RELATED = auto()
    CDN_ORIGIN_ERROR = auto()
    INFORMATIONAL = auto()
    UNKNOWN = auto()


class Origin(Enum):
    PROXY = auto()
    TARGET = auto()
    UNDETERMINED = auto()


# 目标已经处理了请求：换个出口只会得到同样的回答。
_TARGET_HANDLED = frozenset({400, 401, 404, 405, 406, 409, 410, 415, 421, 422, 500, 501, 505})
_PROXY_LAYER = frozenset({407, 511})
_AMBIGUOUS = frozenset({502, 503, 504})
_EGRESS_RELATED = frozenset({403, 429, 451})
# Cloudflare 源站错误：CDN 连上了，坏的是它到源站那一段。
_CDN_ORIGIN = frozenset(range(520, 527))

_PROXY_ERROR_HEADERS = frozenset({"x-squid-error", "x-cache", "x-tinyproxy", "proxy-connection"})
_PROXY_SERVER_PREFIXES = ("squid", "tinyproxy", "privoxy", "polipo", "mitmproxy")
_PROXY_VIA_TOKENS = ("squid", "tinyproxy", "proxy")
_TARGET_SERVER_PREFIXES = (
    "nginx",
    "apache",
    "cloudflare",
    "openresty",
    "gunicorn",
    "iis",
    "caddy",
    "envoy",
    "istio",
)


def classify_status(status: int) -> StatusCategory:
    """把状态码归入切换判据所需的类别。

    ``UNKNOWN`` 的兜底行为是不切换：未知状态码大概率是目标应用的自定义码。
    """
    if 100 <= status < 200:
        return StatusCategory.INFORMATIONAL
    if 200 <= status < 400:
        return StatusCategory.TARGET_HANDLED
    # 必须先于 _TARGET_HANDLED 判断：520–526 落在 5xx 区间，
    # 而它需要在用户误把 521 加进 switch_on_status 时仍然生效。
    if status in _CDN_ORIGIN:
        return StatusCategory.CDN_ORIGIN_ERROR
    if status == 408:
        return StatusCategory.INCOMPLETE_REQUEST
    if status in _PROXY_LAYER:
        return StatusCategory.PROXY_LAYER
    if status in _AMBIGUOUS:
        return StatusCategory.AMBIGUOUS
    if status in _EGRESS_RELATED:
        return StatusCategory.EGRESS_RELATED
    if status in _TARGET_HANDLED:
        return StatusCategory.TARGET_HANDLED
    return StatusCategory.UNKNOWN


def determine_origin(response_headers: Headers, *, is_connect: bool) -> Origin:
    """判定歧义状态码由谁生成。

    不使用「响应耗时」作为判据：那需要知道到目标的 RTT，而我们没有——
    能直连测 RTT 就不需要代理了。固定阈值会在本地代理（RTT < 1ms）与
    跨国代理（RTT > 200ms）两种场景下给出相反的错误判断。
    """
    # 信号 1：CONNECT 的非 2xx 必然来自上级代理，可靠性 100%。
    if is_connect:
        return Origin.PROXY

    # 信号 2：代理软件特征头。先判代理再判目标——代理透传目标响应头时，
    # 它自己加的头比透传来的 Server 更能说明是谁生成了这个错误。
    if any(name in response_headers for name in _PROXY_ERROR_HEADERS):
        return Origin.PROXY

    server = (response_headers.get("server") or "").lower()
    if server.startswith(_PROXY_SERVER_PREFIXES):
        return Origin.PROXY

    via = (response_headers.get("via") or "").lower()
    if any(token in via for token in _PROXY_VIA_TOKENS):
        return Origin.PROXY

    if server.startswith(_TARGET_SERVER_PREFIXES):
        return Origin.TARGET

    return Origin.UNDETERMINED
