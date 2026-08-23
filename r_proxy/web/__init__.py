"""Web 管理界面（可选）。

对应设计：docs/design/DD_WEB.md §2。

**本包是全代码库中唯一导入 `fastapi` / `uvicorn` / `tomlkit` 的地方**，而
``r_proxy.app`` 只在确认需要启动 Web 时才导入本包。这个两层结构保证
``--no-web`` 形态即使完全没装第三方依赖也能正常运行。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uvicorn

    from r_proxy.app import Application

logger = logging.getLogger(__name__)

DEFAULT_STOP_TIMEOUT = 5.0


class WebRunner:
    """已启动的 Web 界面。持有 uvicorn 服务器与承载它的任务。"""

    __slots__ = ("_server", "_task")

    def __init__(self, server: uvicorn.Server, task: asyncio.Task[None]) -> None:
        self._server = server
        self._task = task

    @property
    def task(self) -> asyncio.Task[None]:
        return self._task

    @property
    def bound_address(self) -> tuple[str, int]:
        for server in self._server.servers:
            for socket in server.sockets:
                host, port = socket.getsockname()[:2]
                return str(host), int(port)
        raise RuntimeError("Web 界面尚未绑定端口")

    async def wait_started(self, *, timeout: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._server.started:
            if self._task.done():
                # 端口被占用时 serve() 直接返回，不等它就会在这里死等。
                await self._task
                raise RuntimeError("Web 界面启动失败")
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("Web 界面未在超时内就绪")
            await asyncio.sleep(0.01)

    async def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """请求优雅退出。

        用 ``should_exit`` 而非 ``task.cancel()``：取消会让 uvicorn 在关闭中途
        被打断，已建立的连接不会收到响应。超时后才强制取消。
        """
        self._server.should_exit = True
        try:
            async with asyncio.timeout(timeout):
                # 用 gather 收异常而不是直接 await：serve 抛过的异常已经由
                # _on_exit 记进日志，关停时再抛一次只会打断调用方后续的清理。
                await asyncio.gather(self._task, return_exceptions=True)
        except TimeoutError:
            logger.warning("Web 界面未能在 %.1fs 内退出，强制取消", timeout)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


async def start(application: Application) -> WebRunner:
    """在当前事件循环里起 uvicorn。

    不用 ``uvicorn.run()``：它会自己创建事件循环，而 Web 必须和代理跑在同一个
    循环里才能直接读内存状态。
    """
    import uvicorn

    from r_proxy.web.app import create_app

    cfg = application.snapshot.webui
    config = uvicorn.Config(
        create_app(application),
        host=cfg.host,
        port=cfg.port,
        log_config=None,  # 复用代理的日志配置
        access_log=False,  # 访问日志会记下完整 URL 与请求头，按需再开
        # 冗余保险：workers > 1 会 fork 出第二个写者线程，启动校验已经拒绝，
        # 这里再钉一次，避免将来有人从别的入口装配。
        workers=1,
        lifespan="off",  # 启停由 Application 统一编排
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="webui")
    task.add_done_callback(_on_exit)
    return WebRunner(server, task)


def _on_exit(task: asyncio.Task[None]) -> None:
    """Web 崩溃不影响代理。

    也**不自动重启**：反复崩溃会刷屏，而崩溃原因（端口被占、依赖损坏）通常
    不会自愈。
    """
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        logger.error("Web 界面异常退出，代理服务继续运行", exc_info=exc)
