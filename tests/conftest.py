"""测试共用的真实 TCP 服务端。

代理的行为几乎全部取决于套接字层的真实语义（半关闭、背压、RST、
连接拒绝）。用 mock 替换套接字会让测试验证 mock 而非代码，因此这里
提供真实的轻量服务端。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from r_proxy.config.model import (
    ConfigSnapshot,
    DatabaseConfig,
    LimitsConfig,
    ListenConfig,
    RoutingConfig,
    UpstreamConfig,
    WebUIConfig,
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def make_snapshot(
    *upstreams: UpstreamConfig,
    routing: RoutingConfig | None = None,
    limits: LimitsConfig | None = None,
    listen: ListenConfig | None = None,
    webui: WebUIConfig | None = None,
) -> ConfigSnapshot:
    """构造配置快照，只需给出与被测行为相关的字段。"""
    return ConfigSnapshot.build(
        listen=listen or ListenConfig(host="127.0.0.1", port=0),
        webui=webui or WebUIConfig(enabled=False),
        database=DatabaseConfig(
            state_path=Path("/tmp/state.db"),
            logs_path=Path("/tmp/logs.db"),
            rules_path=Path("/tmp/rules.db"),
        ),
        routing=routing or RoutingConfig(),
        limits=limits or LimitsConfig(),
        upstreams=upstreams,
        rules_enabled=True,
        config_version="test",
        source_path=Path("/tmp/config.toml"),
        loaded_at=0.0,
    )


def upstream(
    name: str,
    *,
    priority: int = 100,
    address: str = "127.0.0.1:3128",
    enabled: bool = True,
    direct: bool = False,
) -> UpstreamConfig:
    return UpstreamConfig(
        name=name,
        type="direct" if direct else "http",
        address=None if direct else address,
        priority=priority,
        enabled=enabled,
    )


@dataclass
class FakeServer:
    """监听在回环地址随机端口上的服务端。"""

    host: str
    port: int
    server: asyncio.Server
    received: list[bytes] = field(default_factory=list)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def close(self) -> None:
        self.server.close()
        # Python 3.12.1 起 wait_closed() 会等所有连接处理完毕，
        # 必须先收走处理任务，否则永远等着睡 3600 秒的那个 handler。
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.server.wait_closed()


# 本次测试中创建的全部服务端与连接处理任务，由 _cleanup_servers 统一回收。
# 不回收会留下 pending task，在事件循环关闭时刷出「Task was destroyed」告警，
# 把真正的问题淹没在噪声里。
_LIVE_SERVERS: list[FakeServer] = []


async def start_server(handler: Handler, host: str = "127.0.0.1") -> FakeServer:
    fake: FakeServer

    async def wrapped(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            fake.tasks.add(task)
            task.add_done_callback(fake.tasks.discard)
        try:
            await handler(r, w)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            if not w.is_closing():
                w.close()

    server = await asyncio.start_server(wrapped, host, 0)
    port = server.sockets[0].getsockname()[1]
    fake = FakeServer(host=host, port=port, server=server)
    _LIVE_SERVERS.append(fake)
    return fake


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把 ``$HOME`` 指向每个测试独有的目录。

    ``database.state_path`` 默认是 ``~/.r-proxy/state.db``。不隔离的话，凡是
    启动 ``Application`` 的测试都会写进开发者的真实家目录，并且彼此共用同一个
    状态库——上一个测试学到的粘性映射会改变下一个测试的路由结果。
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
async def _cleanup_servers() -> AsyncIterator[None]:
    yield
    for srv in list(_LIVE_SERVERS):
        await srv.close()
    _LIVE_SERVERS.clear()


@pytest.fixture
async def echo_server() -> AsyncIterator[FakeServer]:
    """把收到的字节原样回送，直到对端半关闭。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        while chunk := await r.read(4096):
            w.write(chunk)
            await w.drain()

    srv = await start_server(handler)
    yield srv
    await srv.close()


@pytest.fixture
async def closed_port() -> AsyncIterator[int]:
    """一个确定无人监听的端口：先绑定拿到端口号，再立刻释放。"""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    yield port


@pytest.fixture
async def blackhole_server() -> AsyncIterator[FakeServer]:
    """接受连接后永不回应，用于超时测试。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await asyncio.sleep(3600)

    srv = await start_server(handler)
    yield srv
    await srv.close()
