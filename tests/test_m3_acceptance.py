"""M3 验收点逐条核对。

对应设计：docs/design/MIGRATION.md §5.3。编号与该表一一对应，缺一条即
M3 未完成。存储内部语义（``STRICT``、``ON CONFLICT``、合并、清理）的细节在
tests/test_storage_*.py，这里验证端到端可观察的行为：真实套接字、真实
SQLite 文件、真实的进程重启（同一份配置起第二个 ``Application``）。

规则相关的 M3-10 ~ M3-17 已随 M5 重构整体移交 tests/test_m5_acceptance.py：
M3-10（last match wins）被 M5-01 反转废止，其余在新的数据源与语义下重做
（[MIGRATION §7.4](../docs/design/MIGRATION.md)）。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from r_proxy.app import Application
from r_proxy.state.health import HealthState
from r_proxy.storage.queue import WriteQueue, sticky_hit, sticky_upsert
from r_proxy.storage.schema import Database, open_write
from r_proxy.storage.writer import WriterThread
from tests.conftest import start_server

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def config(*upstreams: str, tmp_path: Path, extra: str = "") -> str:
    return (
        '[listen]\nhost = "127.0.0.1"\nport = 0\n'
        "[webui]\nenabled = false\n"
        "[database]\n"
        f'state_path = "{tmp_path / "state.db"}"\n'
        f'logs_path = "{tmp_path / "logs.db"}"\n'
        f'rules_path = "{tmp_path / "rules.db"}"\n' + extra + "".join(upstreams)
    )


def http_upstream(name: str, address: str, *, priority: int = 100, enabled: bool = True) -> str:
    return (
        f'\n[[upstreams]]\nname = "{name}"\ntype = "http"\n'
        f'address = "{address}"\npriority = {priority}\nenabled = {str(enabled).lower()}\n'
    )


def direct_upstream(name: str = "direct", *, priority: int = 100) -> str:
    return f'\n[[upstreams]]\nname = "{name}"\ntype = "direct"\npriority = {priority}\n'


def responder(status_line: bytes, *, seen: list[str] | None = None) -> Handler:
    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen.append(head.split(b"\r\n")[0].decode())
        w.write(status_line + b"\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()

    return handler


async def running_app(path: Path) -> Application:
    app = Application(config_path=path)
    await app.start()
    return app


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


async def get(app: Application, host: str = "site.test", *, path: str = "/") -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    request = f"GET http://{host}{path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


async def get_authority(app: Application, authority: str, *, host: str) -> bytes:
    """绝对 URI 里带端口，用于指定 IP 字面量目标。"""
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    writer.write(
        f"GET http://{authority}/ HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n\r\n".encode()
    )
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


def query(path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def seed_state(tmp_path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    """在启动之前把行写进 ``state.db``，模拟上一次运行留下的状态。"""
    conn = open_write(tmp_path / "state.db", Database.STATE)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


class TestStickyReuse:
    async def test_m3_01_the_second_request_reuses_the_successful_upstream(
        self, tmp_path: Path
    ) -> None:
        """M3-01：同优先级两出口本会轮询，粘性让同一 host 固定沿用第一个。"""
        seen_a: list[str] = []
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_a))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("a", a.address, priority=10),
                    http_upstream("b", b.address, priority=10),
                    tmp_path=tmp_path,
                ),
            )
        )
        try:
            for _ in range(4):
                await get(app)
            assert len(seen_a) == 4
            assert seen_b == []
            assert app.state.sticky.get("site.test") is not None
        finally:
            await app.stop()

    async def test_m3_02_sticky_survives_a_restart(self, tmp_path: Path) -> None:
        """M3-02：粘性映射被回填。第二个进程的轮询游标从 0 开始，
        若回填失效，请求会落到 ``a``。"""
        seen_a: list[str] = []
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_a))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        path = write_config(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=10),
                tmp_path=tmp_path,
            ),
        )
        # 先让 b 成为粘性出口：让 a 在首轮被跳过的唯一办法是直接写库，
        # 因此这里用「上一次运行留下的绑定」的形式播种。
        seed_state(
            tmp_path,
            "INSERT INTO host_upstream (host, upstream_name, source, updated_at)"
            " VALUES ('site.test', 'b', 'auto', ?)",
            (int(time.time()),),
        )
        app = await running_app(path)
        try:
            entry = app.state.sticky.get("site.test")
            assert entry is not None and entry.upstream == "b"
            await get(app)
            assert len(seen_b) == 1
            assert seen_a == []
        finally:
            await app.stop()

    async def test_m3_03_the_circuit_state_is_not_restored(self, tmp_path: Path) -> None:
        """M3-03：重启可能正是运维在修网络，带着旧的 ``open`` 启动会让刚
        修好的出口继续被拒绝一整个冷却期。"""
        seed_state(
            tmp_path,
            "INSERT INTO upstream_health"
            " (upstream_name, consecutive_failures, total_failure, circuit_state, updated_at)"
            " VALUES ('direct', 9, 9, 'open', ?)",
            (int(time.time()),),
        )
        app = await running_app(
            write_config(tmp_path, config(direct_upstream(), tmp_path=tmp_path))
        )
        try:
            now = time.monotonic()
            assert app.state.health.state_of("direct", now=now) is HealthState.CLOSED
            # 累计计数照常回填：Web 要据此展示历史成功率。
            assert app.state.health.snapshot_of("direct").total_failure == 9
        finally:
            await app.stop()

    async def test_m3_05_an_automatic_success_never_overwrites_a_manual_binding(
        self, tmp_path: Path
    ) -> None:
        """M3-05：手动绑定的出口失败、请求改走别的出口成功，绑定仍不变。

        内存与数据库两层都要守住：只有内存保护时，重启回填后的窗口期可能
        丢绑定；只有 SQL 保护时，内存已经改错，落盘被拒反而两边不一致。
        """
        good = await start_server(responder(b"HTTP/1.1 200 OK"))
        path = write_config(
            tmp_path,
            config(
                http_upstream("good", good.address, priority=10),
                http_upstream("dead", "127.0.0.1:1", priority=20),
                tmp_path=tmp_path,
            ),
        )
        seed_state(
            tmp_path,
            "INSERT INTO host_upstream (host, upstream_name, source, updated_at)"
            " VALUES ('site.test', 'dead', 'manual', ?)",
            (int(time.time()),),
        )
        app = await running_app(path)
        try:
            assert (await get(app)).startswith(b"HTTP/1.1 200 OK")
            entry = app.state.sticky.get("site.test")
            assert entry is not None
            assert (entry.upstream, entry.source) == ("dead", "manual")
        finally:
            await app.stop()
        assert query(tmp_path / "state.db", "SELECT upstream_name, source FROM host_upstream") == [
            ("dead", "manual")
        ]


class TestWritePath:
    def test_m3_04_concurrent_increments_lose_nothing(self, tmp_path: Path) -> None:
        """M3-04：4 个线程各 500 次自增，库里最终 2000。

        实测的反例是 deferred 事务 + Python 侧读改写：只落了 509 次，丢了
        75%。防线是 ``BEGIN IMMEDIATE`` 加 SQL 侧 ``hit_count = hit_count + ?``。
        """
        from r_proxy.config.model import DatabaseConfig

        cfg = DatabaseConfig(
            state_path=tmp_path / "state.db",
            logs_path=tmp_path / "logs.db",
            rules_path=tmp_path / "rules.db",
            flush_interval_ms=10,
        )
        queue = WriteQueue(maxsize=10_000)
        thread = WriterThread(queue, cfg)
        thread.start_and_wait()
        try:
            queue.put(sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200))
            assert thread.wait_until_drained(timeout=5.0)

            def hammer() -> None:
                for _ in range(500):
                    queue.put(sticky_hit(host="a.com", now_unix=1))

            workers = [threading.Thread(target=hammer) for _ in range(4)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
            assert thread.wait_until_drained(timeout=10.0)
        finally:
            thread.stop()
        # 1 来自 UPSERT 自身，2000 来自自增。
        assert query(cfg.state_path, "SELECT hit_count FROM host_upstream") == [(2001,)]

    def test_m3_06_and_07_a_full_queue_drops_logs_but_keeps_sticky_changes(self) -> None:
        """M3-06、M3-07：队列满时日志被丢弃并计数，粘性变更照常接受。

        两条验收点是同一个分级策略的两面，用同一个满队列一起验证。``put``
        永不阻塞（无界底层容器 + 自行维护水位），因此事件循环不会被拖住。
        """
        from r_proxy.storage.queue import OpKind, WriteOp

        queue = WriteQueue(maxsize=2)
        log = WriteOp(
            OpKind.REQUEST_LOG, ("r", "h", None, "GET", "a", 1, 0) + (None,) * 6 + (1, 0, 0, 1)
        )
        assert [queue.put(log) for _ in range(3)] == [True, True, False]
        assert queue.dropped_lossy == 1

        sticky = sticky_upsert(host="a.com", upstream="a", url=None, now_unix=1, status=200)
        assert queue.put(sticky) is True
        assert queue.dropped_critical == 0

    async def test_m3_08_shutdown_drains_the_queue_before_closing(self, tmp_path: Path) -> None:
        """M3-08：SIGTERM 后队列排空才关库。落盘间隔调得比测试时长还大，
        因此写入只能是关停流程排空的那一批。"""
        good = await start_server(responder(b"HTTP/1.1 200 OK"))
        app = await running_app(
            write_config(
                tmp_path,
                config(
                    http_upstream("good", good.address),
                    tmp_path=tmp_path,
                    extra="flush_interval_ms = 600000\n",
                ),
            )
        )
        await get(app)
        assert query(tmp_path / "state.db", "SELECT host FROM host_upstream") == []
        app.request_stop()
        await app.stop()
        assert query(tmp_path / "state.db", "SELECT host FROM host_upstream") == [("site.test",)]

    async def test_m3_09_a_failing_write_does_not_stop_the_proxy(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """M3-09：磁盘满等写入失败只记 ERROR，代理继续转发。

        用「删掉表」制造与磁盘满同类的故障：写者线程的每个批次都失败，
        但请求路径完全不受影响。
        """
        good = await start_server(responder(b"HTTP/1.1 200 OK"))
        app = await running_app(
            write_config(tmp_path, config(http_upstream("good", good.address), tmp_path=tmp_path))
        )
        try:
            conn = sqlite3.connect(tmp_path / "state.db")
            try:
                conn.execute("DROP TABLE host_upstream")
                conn.commit()
            finally:
                conn.close()
            with caplog.at_level(logging.ERROR):
                for i in range(3):
                    assert (await get(app, host=f"h{i}.test")).startswith(b"HTTP/1.1 200 OK")
                await asyncio.sleep(0.3)
            assert any("批量写入失败" in r.message for r in caplog.records)
        finally:
            await app.stop()

    def test_m3_18_logs_beyond_the_retention_policy_are_cleaned(self, tmp_path: Path) -> None:
        """M3-18：30 天与 10 万条取先达到者。清理在写者线程内串行执行，
        放到独立线程会引入第二个写者。"""
        from r_proxy.config.model import DatabaseConfig
        from r_proxy.storage.retention import Retention

        cfg = DatabaseConfig(
            state_path=tmp_path / "state.db",
            logs_path=tmp_path / "logs.db",
            rules_path=tmp_path / "rules.db",
            retention_days=30,
            max_log_rows=3,
        )
        conns = {
            Database.STATE: open_write(cfg.state_path, Database.STATE),
            Database.LOGS: open_write(cfg.logs_path, Database.LOGS),
        }
        now = int(time.time())
        try:
            conns[Database.LOGS].executemany(
                "INSERT INTO request_log"
                " (request_id, host, method, upstream_name, elapsed_ms, created_at)"
                " VALUES ('r', 'h', 'GET', 'a', 1, ?)",
                [(now - 40 * 86400,), (now,), (now,), (now,), (now,)],
            )
            Retention(cfg).run(conns)
            assert conns[Database.LOGS].execute("SELECT COUNT(*) FROM request_log").fetchone() == (
                3,
            )
        finally:
            for c in conns.values():
                c.close()
