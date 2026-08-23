"""M4 验收点逐条核对。

对应设计：docs/design/MIGRATION.md §6.3。编号与该表一一对应。

这里只放**端到端可观察**的行为：真实的 uvicorn 监听、真实的依赖缺失场景。
中间件与依赖层面的细节在 tests/test_web_app.py。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from r_proxy.app import Application, StartupError
from tests.conftest import start_server

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "acceptance-token-long-enough-1234"
# 假查询的自我放行时限：正常路径下测试会主动放行，这只是防止用例挂死。
SLOW_QUERY_S = 10.0


def write_config(
    tmp_path: Path,
    *,
    web_enabled: bool,
    token: str | None = None,
    workers: int = 1,
) -> Path:
    token_line = f'auth_token = "{token}"\n' if token else ""
    text = (
        '[listen]\nhost = "127.0.0.1"\nport = 0\n'
        f"[webui]\nenabled = {str(web_enabled).lower()}\n"
        f'host = "127.0.0.1"\nport = 0\nworkers = {workers}\n{token_line}'
        "[database]\n"
        f'state_path = "{tmp_path / "state.db"}"\n'
        f'logs_path = "{tmp_path / "logs.db"}"\n'
        '\n[[upstreams]]\nname = "direct"\ntype = "direct"\n'
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


async def started(path: Path) -> Application:
    app = Application(config_path=path)
    await app.start()
    return app


async def _wait_for_flag(flag: threading.Event, *, timeout: float = 5.0) -> None:
    """轮询一个线程事件。

    不用 ``to_thread`` 等它：那会再占一个池里的线程，而这里要验证的恰恰是线程
    池的可用性。轮询靠 ``asyncio.sleep`` 推进，事件循环一旦被卡住就必然超时——
    这正是我们要检测的失败模式。
    """
    deadline = time.monotonic() + timeout
    while not flag.is_set():
        if time.monotonic() > deadline:
            raise AssertionError("查询没能在事件循环之外开始，to_thread 边界失效")
        await asyncio.sleep(0.01)


async def _proxy_get(app: Application, target: str) -> bytes:
    host, port = app.proxy_address
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(f"GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        return await asyncio.wait_for(reader.read(4096), timeout=10)
    finally:
        writer.close()


def run_without_web_dependencies(
    tmp_path: Path, *, web_enabled: bool
) -> subprocess.CompletedProcess[str]:
    """在子进程里屏蔽 fastapi / uvicorn 后启动一次代理。

    必须用子进程：本进程里 ``r_proxy.web`` 早已被其它测试导入，删掉再导会生成
    新的类对象，污染整个会话（见 .learnings 里 importlib.reload 那一条）。
    """
    config = write_config(tmp_path, web_enabled=web_enabled)
    script = f"""
import asyncio, logging, sys
from pathlib import Path

BLOCKED = {{"fastapi", "uvicorn", "tomlkit", "starlette", "pydantic"}}


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{{name}} 被测试屏蔽", name=name)
        return None


sys.meta_path.insert(0, Blocker())
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                    format="%(levelname)s %(name)s %(message)s")

from r_proxy.app import Application


async def main() -> None:
    app = Application(config_path=Path({str(config)!r}))
    await app.start()
    host, port = app.proxy_address
    print(f"PROXY {{host}} {{port}}", flush=True)
    print(f"WEB {{app.web is not None}}", flush=True)
    await app.stop()
    print("STOPPED", flush=True)


asyncio.run(main())
"""
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )


class TestOptionalDependency:
    def test_m4_01_no_web_starts_cleanly_without_fastapi(self, tmp_path: Path) -> None:
        """M4-01：`--no-web` 且未装 fastapi → 代理正常启动，**无告警**。"""
        result = run_without_web_dependencies(tmp_path, web_enabled=False)
        assert result.returncode == 0, result.stderr
        assert "PROXY 127.0.0.1" in result.stdout
        assert "WEB False" in result.stdout
        assert "STOPPED" in result.stdout
        # 关掉了就不该有任何抱怨——每次启动刷一行无关告警会训练用户忽略日志。
        assert "WARNING" not in result.stderr, result.stderr

    def test_m4_02_enabled_without_fastapi_warns_and_keeps_serving(self, tmp_path: Path) -> None:
        """M4-02：启用了但依赖缺失 → 告警 + 安装提示，代理照常服务。"""
        result = run_without_web_dependencies(tmp_path, web_enabled=True)
        assert result.returncode == 0, result.stderr
        assert "PROXY 127.0.0.1" in result.stdout
        assert "WEB False" in result.stdout
        assert 'pip install "r-proxy[web]"' in result.stderr
        assert "--no-web" in result.stderr

    async def test_m4_03_multiple_workers_are_refused(self, tmp_path: Path) -> None:
        """M4-03：`webui.workers: 4` → 启动被拒绝。

        多进程会 fork 出第二个写者线程，直接摧毁单一写者约束。
        """
        app = Application(config_path=write_config(tmp_path, web_enabled=True, workers=4))
        with pytest.raises(StartupError) as caught:
            await app.start()
        assert "E_WEB_WORKERS" in str(caught.value)


class TestCrashIsolation:
    async def test_m4_04_a_crashing_web_task_does_not_stop_the_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """M4-04：Web 任务抛异常 → 记 `ERROR`，代理继续服务。"""
        import uvicorn

        async def exploding_serve(self: uvicorn.Server, sockets: object = None) -> None:
            raise RuntimeError("uvicorn 起不来")

        monkeypatch.setattr(uvicorn.Server, "serve", exploding_serve)
        path = write_config(tmp_path, web_enabled=True)
        with caplog.at_level(logging.ERROR):
            app = await started(path)
            # 让 serve 的异常有机会传到 done callback。
            await asyncio.sleep(0.05)
            try:
                host, port = app.proxy_address
                reader, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
            finally:
                await app.stop()

        assert any("Web 界面异常退出" in r.getMessage() for r in caplog.records)


class TestServedOverRealPort:
    async def test_m4_05_the_api_requires_a_token(self, tmp_path: Path) -> None:
        """M4-05：无 token 访问 `/api/status` → `401`。"""
        app = await started(write_config(tmp_path, web_enabled=True, token=TOKEN))
        runner = app.web
        assert runner is not None
        await runner.wait_started()
        host, port = runner.bound_address
        proxy_port = app.proxy_address[1]
        try:
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                anonymous = await client.get("/api/status")
                authorized = await client.get(
                    "/api/status", headers={"Authorization": f"Bearer {TOKEN}"}
                )
                probe = await client.get("/api/healthz")
        finally:
            await app.stop()

        assert anonymous.status_code == 401
        assert authorized.status_code == 200
        # 报的是真实在听的那个端口，而不是配置里写的 0。
        assert authorized.json()["proxy"]["port"] == proxy_port
        # 存活探针不需要认证，否则监控要么拿不到状态要么得配 token。
        assert probe.status_code == 200

    async def test_m4_06_repeated_auth_failures_are_throttled(self, tmp_path: Path) -> None:
        """M4-06：60 秒内 11 次认证失败 → `429`。"""
        app = await started(write_config(tmp_path, web_enabled=True, token=TOKEN))
        runner = app.web
        assert runner is not None
        await runner.wait_started()
        host, port = runner.bound_address
        try:
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
                codes = [
                    (await client.get("/api/status", headers={"X-Auth-Token": "wrong"})).status_code
                    for _ in range(11)
                ]
        finally:
            await app.stop()

        assert codes[:10] == [401] * 10
        assert codes[10] == 429

    async def test_m4_07_an_oversized_page_size_is_refused(self, tmp_path: Path) -> None:
        """M4-07：`page_size=100000` → `422`。

        分页上限不是美观问题：一次百万行的查询会占住线程池数秒，而 DNS 解析
        与它共用同一个池，表现为「打开管理界面后新连接变慢」。
        """
        app = await started(write_config(tmp_path, web_enabled=True, token=TOKEN))
        runner = app.web
        assert runner is not None
        await runner.wait_started()
        host, port = runner.bound_address
        try:
            async with httpx.AsyncClient(
                base_url=f"http://{host}:{port}", headers={"X-Auth-Token": TOKEN}
            ) as client:
                oversized = await client.get("/api/logs?page_size=100000")
                deep = await client.get("/api/logs?page=999999999")
                fine = await client.get("/api/logs?page_size=1000")
        finally:
            await app.stop()

        assert oversized.status_code == 422
        assert deep.status_code == 422
        assert fine.status_code == 200

    async def test_m4_08_a_slow_query_does_not_block_the_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M4-08：Web 查询期间的代理请求不受阻塞（`to_thread` 真的生效）。

        假查询进入后**卡住不返回**，直到代理请求已经完成才放行——「查询仍在
        进行中而代理已经答完」比比较耗时更直接。同步阻塞正是 SQLite 深翻页的
        真实行为：它无法从外部取消，只能等它跑完。

        变异验证：把路由里的 `to_thread` 换成直接调用，假查询会把事件循环卡住
        整整 `SLOW_QUERY_S`，代理请求只能排在它后面，`still_running` 断言失败。
        """
        import sqlite3

        from r_proxy.web import queries

        entered = threading.Event()
        release = threading.Event()

        def slow_query(pool: object, params: object) -> list[sqlite3.Row]:
            entered.set()
            release.wait(timeout=SLOW_QUERY_S)
            return []

        async def origin(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
            await w.drain()
            w.close()

        monkeypatch.setattr(queries, "query_logs", slow_query)
        site = await start_server(origin)
        app = await started(write_config(tmp_path, web_enabled=True, token=TOKEN))
        runner = app.web
        assert runner is not None
        await runner.wait_started()
        host, port = runner.bound_address
        try:
            async with httpx.AsyncClient(
                base_url=f"http://{host}:{port}",
                headers={"X-Auth-Token": TOKEN},
                timeout=30.0,
            ) as client:
                query = asyncio.create_task(client.get("/api/logs"))
                await _wait_for_flag(entered)
                response = await _proxy_get(app, site.address)
                # 代理已经答完，而查询这会儿还卡在线程池里没返回。
                still_running = not query.done()
                release.set()
                assert (await query).status_code == 200
        finally:
            release.set()
            await app.stop()

        assert response.startswith(b"HTTP/1.1 200 OK")
        assert still_running

    async def test_m4_10_the_upstream_list_never_carries_credentials(self, tmp_path: Path) -> None:
        """M4-10：`GET /api/upstreams` 含 `has_auth`，不含密码。

        在**原始响应文本**上断言，而不是逐个字段查：凭据泄露最可能的形态是它被
        塞进某个错误消息或调试字段里，那种情况按字段名检查查不出来。
        """
        config = write_config(tmp_path, web_enabled=True, token=TOKEN)
        config.write_text(
            config.read_text(encoding="utf-8")
            + '\n[[upstreams]]\nname = "proxy-a"\ntype = "http"\naddress = "127.0.0.1:3128"\n'
            + '\n[upstreams.auth]\nusername = "squid-user"\npassword = "s3cr3t-pass"\n',
            encoding="utf-8",
        )
        app = await started(config)
        runner = app.web
        assert runner is not None
        await runner.wait_started()
        host, port = runner.bound_address
        try:
            async with httpx.AsyncClient(
                base_url=f"http://{host}:{port}", headers={"X-Auth-Token": TOKEN}
            ) as client:
                response = await client.get("/api/upstreams")
        finally:
            await app.stop()

        assert response.status_code == 200
        by_name = {u["name"]: u for u in response.json()["upstreams"]}
        assert by_name["proxy-a"]["has_auth"] is True
        assert by_name["direct"]["has_auth"] is False
        assert "s3cr3t-pass" not in response.text
        assert "squid-user" not in response.text

    async def test_the_web_port_is_separate_from_the_proxy_port(self, tmp_path: Path) -> None:
        """两个 `port = 0` 各自由内核分配，不构成端口冲突。"""
        app = await started(write_config(tmp_path, web_enabled=True, token=TOKEN))
        runner = app.web
        assert runner is not None
        await runner.wait_started()
        try:
            assert runner.bound_address[1] != app.proxy_address[1]
        finally:
            await app.stop()
