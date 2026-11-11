"""Regression coverage for storage reload and CONNECT backpressure."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from r_proxy.app import Application, StartupError
from r_proxy.config.loader import load
from r_proxy.config.model import DatabaseConfig
from r_proxy.contracts import ConnectorError, FailureKind, Headers
from r_proxy.egress.connector import UpstreamConn, UpstreamConnector
from r_proxy.protocol.parse import parse_target
from r_proxy.storage.expiry import StickyExpiryPolicy
from r_proxy.storage.retention import Retention
from r_proxy.storage.schema import Database, open_write
from r_proxy.storage.service import StorageService


def configured(tmp_path: Path) -> tuple[Application, Path]:
    path = tmp_path / "config.toml"
    path.write_text(f'''[webui]
enabled = false
[database]
state_path = "{(tmp_path / "state.db").as_posix()}"
logs_path = "{(tmp_path / "logs.db").as_posix()}"
rules_path = "{(tmp_path / "rules.db").as_posix()}"
retention_days = 30
[routing]
sticky_ttl = 2592000
[[upstreams]]
name = "direct"
type = "direct"
''')
    app = Application(config_path=path)
    snapshot = load(path)
    # Assemble the real storage and reload path without a competing writer thread.
    app._snapshot = snapshot
    app._storage = StorageService(
        snapshot.database, snapshot.limits, sticky_policy=StickyExpiryPolicy(2592000)
    )
    return app, path


@pytest.mark.parametrize("days,ttl,expected", [(1, 0, 0), (60, 0, 1), (30, 1, 1)])
async def test_reload_updates_disk_cleanup(
    tmp_path: Path, days: int, ttl: int, expected: int
) -> None:
    app, path = configured(tmp_path)
    cfg = app.snapshot.database
    conns = {
        Database.STATE: open_write(cfg.state_path, Database.STATE),
        Database.LOGS: open_write(cfg.logs_path, Database.LOGS),
    }
    try:
        now = int(time.time())
        conns[Database.LOGS].execute(
            "INSERT INTO config_audit (created_at, actor, action, target) "
            "VALUES (?, 'test', 'test', 'test')",
            (now - (40 if days == 60 else 2) * 86400,),
        )
        conns[Database.STATE].execute(
            "INSERT INTO host_upstream (host, upstream_name, source, updated_at) "
            "VALUES ('old.test', 'direct', 'auto', ?)",
            (now - 40 * 86400,),
        )
        for conn in conns.values():
            conn.commit()
        path.write_text(
            path.read_text()
            .replace("retention_days = 30", f"retention_days = {days}")
            .replace("sticky_ttl = 2592000", f"sticky_ttl = {ttl}")
        )
        await app.reload()
        app.storage._thread._retention.run(conns)
        assert (
            conns[Database.LOGS].execute("SELECT count(*) FROM config_audit").fetchone()[0]
            == expected
        )
        assert conns[Database.STATE].execute("SELECT count(*) FROM host_upstream").fetchone()[
            0
        ] == (1 if ttl == 0 else 0)
    finally:
        for conn in conns.values():
            conn.close()


@pytest.mark.parametrize("field", ["state_path", "logs_path", "write_queue_size"])
async def test_reload_rejects_structural_storage_changes(tmp_path: Path, field: str) -> None:
    app, path = configured(tmp_path)
    before = app.snapshot
    if field.endswith("_path"):
        text = path.read_text().replace(
            getattr(before.database, field).as_posix(), (tmp_path / "new.db").as_posix()
        )
    else:
        text = path.read_text().replace("[database]", "[database]\nwrite_queue_size = 1234")
    path.write_text(text)
    with pytest.raises(StartupError, match=field):
        await app.reload()
    assert app.snapshot is before


async def test_connect_drain_is_bounded_and_upstream_failure() -> None:
    writer = Mock()
    writer.drain = AsyncMock(side_effect=lambda: None)

    async def blocked() -> None:
        await asyncio.Event().wait()

    writer.drain.side_effect = blocked
    conn = UpstreamConn(asyncio.StreamReader(), writer, "proxy.test", 80)
    target = parse_target("CONNECT", "example.com:443", Headers())
    with pytest.raises(ConnectorError) as caught:
        await asyncio.wait_for(UpstreamConnector()._handshake(conn, target, 0.01), 0.2)
    assert caught.value.kind is FailureKind.UPSTREAM_ERROR


async def test_reload_updates_max_log_rows(tmp_path: Path) -> None:
    app, path = configured(tmp_path)
    cfg = app.snapshot.database
    conns = {
        Database.STATE: open_write(cfg.state_path, Database.STATE),
        Database.LOGS: open_write(cfg.logs_path, Database.LOGS),
    }
    try:
        conns[Database.LOGS].executemany(
            "INSERT INTO config_audit (created_at, actor, action, target) "
            "VALUES (?, 'test', 'test', 'test')",
            [(int(time.time()),)] * 1001,
        )
        conns[Database.LOGS].commit()
        path.write_text(path.read_text().replace("[database]", "[database]\nmax_log_rows = 1000"))
        await app.reload()
        app.storage._thread._retention.run(conns)
        assert (
            conns[Database.LOGS].execute("SELECT count(*) FROM config_audit").fetchone()[0] == 1000
        )
    finally:
        for conn in conns.values():
            conn.close()


async def test_reload_waits_for_active_writer_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path = configured(tmp_path)
    path.write_text("[listen]\nport = 0\n" + path.read_text())
    app = Application(config_path=path)
    cleaning = threading.Event()
    release = threading.Event()
    applying = threading.Event()
    observed: list[tuple[int, float]] = []
    clean_logs = Retention._clean_logs
    clean_sticky = Retention._clean_sticky
    reconfigure = Retention.reconfigure

    def pause_after_logs(self: Retention, conn: sqlite3.Connection) -> int:
        deleted = clean_logs(self, conn)
        cleaning.set()
        release.wait(5)
        return deleted

    def capture_sticky(self: Retention, conn: sqlite3.Connection) -> int:
        observed.append((self._cfg.retention_days, self._expiry.ttl_seconds))
        return clean_sticky(self, conn)

    def signal_reconfigure(
        self: Retention, cfg: DatabaseConfig, expiry: StickyExpiryPolicy
    ) -> None:
        applying.set()
        reconfigure(self, cfg, expiry)

    monkeypatch.setattr(Retention, "_clean_logs", pause_after_logs)
    monkeypatch.setattr(Retention, "_clean_sticky", capture_sticky)
    monkeypatch.setattr(Retention, "reconfigure", signal_reconfigure)
    await app.start()
    reload_task = None
    try:
        assert await asyncio.to_thread(cleaning.wait, 3)
        before = app.snapshot
        path.write_text(
            path.read_text()
            .replace("retention_days = 30", "retention_days = 1")
            .replace("sticky_ttl = 2592000", "sticky_ttl = 0")
        )
        reload_task = asyncio.create_task(app.reload())
        assert await asyncio.to_thread(applying.wait, 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reload_task), 0.05)
        assert app.snapshot is before
        release.set()
        await asyncio.wait_for(reload_task, 3)
        assert observed == [(30, 2592000)]
        assert app.snapshot.database.retention_days == 1
        assert app.snapshot.routing.sticky_ttl == 0
    finally:
        release.set()
        if reload_task is not None:
            await asyncio.gather(reload_task, return_exceptions=True)
        await app.stop()
