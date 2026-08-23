"""依赖注入：Application 取用、token 校验、认证失败限流。

对应设计：docs/design/DD_WEB.md §5。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request

if TYPE_CHECKING:
    from r_proxy.app import Application
    from r_proxy.web.config_writer import ConfigWriter

logger = logging.getLogger(__name__)

MAX_AUTH_FAILURES = 10
AUTH_WINDOW_S = 60.0
# 被跟踪的客户端 IP 上限。可增长资源必须有界，否则伪造源地址的请求能把内存
# 撑满——虽然这里只可能来自本机或内网，但代价太低不值得省。
MAX_TRACKED_CLIENTS = 1024

_UNKNOWN_CLIENT = "unknown"


class AuthThrottle:
    """按客户端 IP 限制认证失败频率。

    与切换频率限流（DD_SWITCHING §6）结构相似但**独立实现**：两者的窗口与
    阈值语义完全不同，共用一份实现只会让参数含义混淆。
    """

    __slots__ = ("_capacity", "_clock", "_failures", "_max_failures", "_window")

    def __init__(
        self,
        *,
        max_failures: int = MAX_AUTH_FAILURES,
        window: float = AUTH_WINDOW_S,
        capacity: int = MAX_TRACKED_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window = window
        self._capacity = capacity
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def max_failures(self) -> int:
        return self._max_failures

    def blocked(self, ip: str) -> bool:
        """该 IP 是否已超限。超限后一律拒绝，不再做 token 比较。"""
        return len(self._recent(ip)) >= self._max_failures

    def record_failure(self, ip: str) -> int:
        """记一次失败，返回该 IP 在当前窗口内的累计次数。

        返回计数是为了让调用方决定要不要打日志：一次页面加载会并发五个请求，
        五条内容一致的 WARNING 没有任何增量信息，只会把真正的事件挤出屏幕。
        """
        recent = self._recent(ip)
        recent.append(self._clock())
        self._failures[ip] = recent
        self._failures.move_to_end(ip)
        while len(self._failures) > self._capacity:
            self._failures.popitem(last=False)
        return len(recent)

    def _recent(self, ip: str) -> deque[float]:
        """窗口内的失败时间戳。顺带丢弃过期项，因此不需要单独的清理任务。"""
        recent = self._failures.get(ip)
        if recent is None:
            return deque()
        cutoff = self._clock() - self._window
        while recent and recent[0] <= cutoff:
            recent.popleft()
        return recent


def get_app(request: Request) -> Application:
    application: Application = request.app.state.r_proxy
    return application


def client_ip(request: Request) -> str:
    """连接的对端地址。

    **不看 `X-Forwarded-For`**：Web 界面直接绑定在本机，转发头完全由客户端
    控制，采信它等于让攻击者用一个伪造头绕开失败限流。
    """
    return request.client.host if request.client is not None else _UNKNOWN_CLIENT


async def require_token(request: Request) -> None:
    """校验 token。未配置 token 时（仅回环绑定允许）直接放行。

    只读接口同样需要 token：``/api/logs`` 会返回用户访问过的全部 URL，
    ``/api/upstreams`` 会返回内网代理地址，比写接口更值得保护。
    """
    throttle: AuthThrottle = request.app.state.auth_throttle
    ip = client_ip(request)
    if throttle.blocked(ip):
        raise HTTPException(429, detail="认证失败次数过多，请稍后再试")

    expected = get_app(request).snapshot.webui.auth_token
    if expected is None:
        return

    provided = _extract_token(request)
    if provided is None or not _matches(provided, expected):
        count = throttle.record_failure(ip)
        # 只记指纹：明文 token 进日志等于把它写进一个权限宽松得多的文件。
        # 一个窗口内只说两次话：第一次失败，以及触发锁定的那一次（带总次数）。
        # 中间那些只是同一个页面的并发请求，逐条打印是纯噪声。
        if count == 1:
            logger.warning("Web 认证失败，来自 %s，token 指纹 %s", ip, fingerprint(provided))
        elif count == throttle.max_failures:
            logger.warning("来自 %s 的连续认证失败已达 %d 次，暂时拒绝该来源", ip, count)
        # 「无 token」与「token 错误」返回完全一致的响应，不给探测者信息。
        raise HTTPException(401, detail="认证失败")


def _matches(provided: str, expected: str) -> bool:
    """常量时间比较，防止用响应时间差逐字节爆破 token。

    必须先编码成 bytes：``compare_digest`` 对 ``str`` 只接受纯 ASCII，非 ASCII
    的 token 会让它抛 ``TypeError``，认证于是变成 ``500`` 而不是 ``401``。
    """
    return secrets.compare_digest(provided.encode(), expected.encode())


def fingerprint(token: str | None) -> str:
    if token is None:
        return "none"
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _extract_token(request: Request) -> str | None:
    """从请求头取 token。

    **不接受 query string 中的 token**：URL 会进入浏览器历史、`Referer` 头与
    反向代理的访问日志，那些地方的留存时间远超会话本身。
    """
    header = request.headers.get("authorization")
    if header and header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return request.headers.get("x-auth-token")


def get_writer(request: Request) -> ConfigWriter:
    """每个应用实例一个 :class:`ConfigWriter`。

    写锁在它身上，因此必须全应用共享同一个实例——每请求新建一个等于没有锁。
    """
    writer: ConfigWriter = request.app.state.config_writer
    return writer


AppDep = Annotated["Application", Depends(get_app)]
WriterDep = Annotated["ConfigWriter", Depends(get_writer)]
Authenticated = Depends(require_token)
