"""FastAPI 应用装配：中间件、错误处理、路由挂载。

对应设计：docs/design/DD_WEB.md §3、§4.4、§7.2。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from r_proxy import __version__
from r_proxy.web import errors
from r_proxy.web.config_writer import ConfigWriter
from r_proxy.web.deps import AuthThrottle
from r_proxy.web.routers import rules, settings, status, sticky, upstreams

if TYPE_CHECKING:
    from r_proxy.app import Application

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SLOW_REQUEST_S = 1.0

# 单页应用的深链接。磁盘上没有这些文件，全部回 index.html 由前端路由接手。
SPA_PAGES = ("upstreams", "sticky", "rules", "settings")

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
)

Handler = Callable[[Request], Awaitable[Response]]


def create_app(application: Application) -> FastAPI:
    app = FastAPI(
        title="r-proxy 管理界面",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    # 请求处理函数经依赖注入取用，不用全局变量：测试里可以为每个用例装配一份
    # 独立的应用，互不影响。
    app.state.r_proxy = application
    app.state.auth_throttle = AuthThrottle()
    # 全应用共用一个写者：写锁在它身上，每请求新建一个等于没有锁。
    app.state.config_writer = ConfigWriter(application)

    errors.install(app)
    app.middleware("http")(_security_headers)
    app.middleware("http")(_slow_request_guard)
    app.include_router(status.router, prefix="/api")
    app.include_router(upstreams.router, prefix="/api")
    app.include_router(sticky.router, prefix="/api")
    app.include_router(rules.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")

    if STATIC_DIR.is_dir():
        _install_spa(app)
    return app


def _install_spa(app: FastAPI) -> None:
    """页面深链接 + 静态资源。

    资源挂在 `/static` 而**不是** `/`：挂在 `/` 的 `StaticFiles` 匹配一切，此后
    注册的任何路由都永远到不了——包括 `/api/*`。那是一个静默的陷阱，症状是新加的
    接口无论怎么调都回 `404`。

    页面深链接走**白名单**，不做「非 `/api` 一律回 index.html」的兜底：兜底会让
    `/static/js/app.jsx` 这类笔误拿到一个 `200` 的 HTML，浏览器只报「MIME 类型
    不匹配」，指不到真正的原因。白名单下笔误就是 `404`。
    """
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for path in ("/", *(f"/{page}" for page in SPA_PAGES)):
        app.add_api_route(path, _index, methods=["GET"], include_in_schema=False)


async def _index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def _security_headers(request: Request, call_next: Handler) -> Response:
    """纵深防御：即便某处渲染忘了转义，`script-src 'self'` 也会拦下内联脚本。

    代价是前端不能用内联 ``<script>`` 与 ``onclick="..."``，一律
    ``addEventListener``。
    """
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    # URL 里可能含敏感路径；no-referrer 保证它不会被带到外部站点。
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = _caching_for(request.url.path)
    return response


def _caching_for(path: str) -> str:
    """静态资源 `no-cache`，接口 `no-store`。

    静态资源的 URL 不带版本号（无构建，也就没有内容哈希文件名）。不发缓存头时
    浏览器按启发式规则自己定新鲜期，升级后会**继续执行缓存里的旧脚本**——实际踩
    过一次：新加的按钮在界面上根本不出现，而服务端发的明明是新文件。`no-cache`
    的语义是「用之前必须回源校验」，配合 `StaticFiles` 已有的 ETag 就是一次 304，
    界面部署在本机，这点开销无关紧要。

    接口用更严的 `no-store`：请求日志含完整 URL、出口列表含内网地址，这些不该被
    写进磁盘缓存留在盘上。
    """
    return "no-store" if path.startswith("/api/") else "no-cache"


async def _slow_request_guard(request: Request, call_next: Handler) -> Response:
    """慢请求只记录、不中断。

    ``sqlite3`` 的执行无法从外部取消，中断一个跑到一半的查询并不能让线程回来，
    只会让用户看到错误而线程仍被占用。记录足以让人发现问题。
    """
    started = time.monotonic()
    response = await call_next(request)
    if (elapsed := time.monotonic() - started) > SLOW_REQUEST_S:
        logger.warning("Web 慢请求 %.2fs：%s %s", elapsed, request.method, request.url.path)
    return response
