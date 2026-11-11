"""命令行入口：参数解析、日志配置、信号处理、组件装配。

对应设计：docs/design/ARCH_OVERVIEW.md §7、§8。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from r_proxy import __version__
from r_proxy.app import Application, StartupError
from r_proxy.config.loader import (
    ENV_CONFIG_PATH,
    ConfigError,
    default_config_path,
    expand_user_path,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="r-proxy",
        description="轻量级 HTTP/HTTPS 正向代理",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"配置文件路径（默认 ${ENV_CONFIG_PATH} 或平台用户配置目录）",
    )
    parser.add_argument("--host", default=None, help="覆盖 listen.host")
    parser.add_argument("--port", type=int, default=None, help="覆盖 listen.port")
    parser.add_argument("--no-web", action="store_true", help="不启动 Web 管理界面")
    parser.add_argument("--check", action="store_true", help="只校验配置并退出，不绑定端口")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def build_overrides(args: argparse.Namespace) -> dict[str, object]:
    """把命令行标志翻译成点分键覆盖项。

    只放**显式给出**的标志：argparse 的默认值不能进来，否则会盖掉配置文件里
    的设置，让「文件 < 环境变量 < 命令行」的优先级失效。
    """
    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["listen.host"] = args.host
    if args.port is not None:
        overrides["listen.port"] = args.port
    if args.no_web:
        overrides["webui.enabled"] = False
    return overrides


def resolve_config_path(args: argparse.Namespace) -> Path:
    explicit: Path | None = args.config
    if explicit is not None:
        return expand_user_path(explicit)
    return default_config_path()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        app = Application(config_path=resolve_config_path(args), overrides=build_overrides(args))
        if args.check:
            return _run_check(app)
        asyncio.run(_serve(app))
    except (ConfigError, StartupError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _run_check(app: Application) -> int:
    try:
        issues = app.check()
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    for issue in issues:
        where = f"[{issue.location}] " if issue.location else ""
        stream = sys.stderr if issue.level == "error" else sys.stdout
        print(f"{issue.level.upper()} {issue.code}: {where}{issue.message}", file=stream)

    if any(i.level == "error" for i in issues):
        return EXIT_CONFIG_ERROR
    print("配置校验通过。")
    return EXIT_OK


async def _serve(app: Application) -> None:
    loop = asyncio.get_running_loop()
    reload_requested = asyncio.Event()

    installed = []
    previous: dict[int, Any] = {}
    if sys.platform == "win32":
        # Windows asyncio loops do not implement add_signal_handler.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda _sig, _frame: loop.call_soon_threadsafe(app.request_stop))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.request_stop)
            installed.append(sig)
        loop.add_signal_handler(signal.SIGHUP, reload_requested.set)
        installed.append(signal.SIGHUP)

    task = asyncio.create_task(app.run())
    try:
        while not task.done():
            waiter = asyncio.create_task(reload_requested.wait())
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                reload_requested.clear()
                try:
                    await app.reload()
                except StartupError as exc:
                    logging.getLogger(__name__).error("配置重载失败，沿用旧配置：%s", exc)
                except Exception:
                    # `reload()` 的文档承诺是「失败时保留正在生效的快照」，这条
                    # 承诺对未预期的异常同样要成立——否则一次重载里的意外 bug
                    # 会打断这个循环，使 `app.run()` 永远等不到 `request_stop()`，
                    # 而此时信号处理器已在 finally 里被摘掉，进程就此挂起。
                    logging.getLogger(__name__).exception("配置重载出现未预期的异常，沿用旧配置")
            else:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        for previous_sig, handler in previous.items():
            signal.signal(previous_sig, handler)
        await task


if __name__ == "__main__":
    sys.exit(main())
