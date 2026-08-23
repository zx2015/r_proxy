"""``cli._serve`` 的信号驱动重载循环。

对应设计：ARCH_OVERVIEW.md §7、§8。
"""

from __future__ import annotations

import asyncio
import os
import signal

from r_proxy.app import StartupError
from r_proxy.cli import _serve


class FakeApp:
    """只模拟 `_serve` 用到的三个方法。"""

    def __init__(self, *, reload_error: Exception | None = None) -> None:
        self.reload_calls = 0
        self._reload_error = reload_error
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self._stop.wait()

    async def reload(self) -> None:
        self.reload_calls += 1
        if self._reload_error is not None:
            raise self._reload_error


class TestServe:
    async def test_a_startup_error_during_reload_keeps_serving(self) -> None:
        app = FakeApp(reload_error=StartupError("配置坏了"))
        task = asyncio.create_task(_serve(app))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.05)
        assert app.reload_calls == 1
        assert not task.done()

        app.request_stop()
        await asyncio.wait_for(task, timeout=1.0)

    async def test_an_unexpected_reload_exception_does_not_hang_shutdown(self) -> None:
        """`reload()` 的既有承诺是「失败时沿用旧配置」；未预期的异常同样不能
        打断这个循环，否则 `app.run()` 永远等不到 `request_stop()`，而信号
        处理器此时已被摘掉，进程会挂起而非优雅退出（新发现 3）。"""
        app = FakeApp(reload_error=RuntimeError("意外 bug"))
        task = asyncio.create_task(_serve(app))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.05)
        assert app.reload_calls == 1
        assert not task.done()  # 循环仍在跑，没有被未预期异常打断

        app.request_stop()
        await asyncio.wait_for(task, timeout=1.0)
