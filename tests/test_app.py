"""app.py 与 cli.py 的启动、校验、关闭、重载测试。

对应设计：docs/design/ARCH_OVERVIEW.md §7、§8
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from r_proxy import cli
from r_proxy.app import Application, StartupError
from r_proxy.egress.capability import probe_ipv6_egress
from r_proxy.storage.rules_store import RulesStore
from r_proxy.storage.schema import StorageError

MINIMAL = """
[listen]
host = "127.0.0.1"
port = 0

[webui]
enabled = false

[[upstreams]]
name = "direct"
type = "direct"
"""


def write_config(tmp_path: Path, text: str = MINIMAL) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestCapabilityProbe:
    def test_returns_a_bool_without_sending_traffic(self) -> None:
        assert isinstance(probe_ipv6_egress(), bool)

    def test_matches_whether_ipv6_sockets_can_be_created(self) -> None:
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s.close()
        except OSError:
            assert probe_ipv6_egress() is False


class TestStartup:
    async def test_starts_and_binds(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path))
        await app.start()
        try:
            host, port = app.proxy_address
            assert host == "127.0.0.1"
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
        finally:
            await app.stop()

    async def test_a_failed_bind_rolls_the_startup_back(self, tmp_path: Path) -> None:
        """启动到一半失败要把已经拉起来的东西收回去。

        写者线程不是 daemon：漏掉关库这一步，进程就永远退不出去——表现是
        「端口被占用之后连 Ctrl-C 都没反应」，而报错信息只提到端口。
        """
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = int(blocker.sockets[0].getsockname()[1])
        text = MINIMAL.replace("port = 0", f"port = {port}")
        app = Application(config_path=write_config(tmp_path, text))
        try:
            with pytest.raises(OSError):
                await app.start()
        finally:
            blocker.close()
            await blocker.wait_closed()

        with pytest.raises(RuntimeError):
            app.storage  # noqa: B018 - 属性访问本身就是被测行为
        assert not [t for t in threading.enumerate() if t.name == "r-proxy-writer"]

    async def test_snapshot_is_available_after_start(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path))
        await app.start()
        try:
            assert app.snapshot.upstreams[0].name == "direct"
            assert app.snapshot.config_version
        finally:
            await app.stop()

    async def test_cli_overrides_win_over_the_file(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path), overrides={"webui.enabled": False})
        await app.start()
        try:
            assert app.snapshot.webui.enabled is False
        finally:
            await app.stop()

    async def test_missing_config_file_is_a_startup_error(self, tmp_path: Path) -> None:
        app = Application(config_path=tmp_path / "nope.toml")
        with pytest.raises(StartupError) as e:
            await app.start()
        assert "nope.toml" in str(e.value)

    async def test_validation_error_blocks_startup(self, tmp_path: Path) -> None:
        path = write_config(
            tmp_path,
            """
[listen]
port = 0

[webui]
enabled = false

[[upstreams]]
name = "direct"
type = "direct"
enabled = false
""",
        )
        app = Application(config_path=path)
        with pytest.raises(StartupError) as e:
            await app.start()
        assert "E_NO_UPSTREAM" in str(e.value)

    async def test_storage_startup_failure_cleans_up_without_leaving_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """存储层启动失败时，整个 Application 必须妥善清理，不遗留写者线程。"""
        from r_proxy.storage.writer import WriterThread

        def mock_fail(self: Any, **_kw: Any) -> None:
            raise StorageError("模拟打开库超时")

        cfg_path = write_config(tmp_path)
        app = Application(config_path=cfg_path)
        monkeypatch.setattr(WriterThread, "start_and_wait", mock_fail)
        with pytest.raises(StartupError) as exc_info:
            await app.start()
        assert "模拟打开库超时" in str(exc_info.value)
        assert app._storage is None
        # 确认没有残留活跃的 r-proxy-writer 线程
        threads = [t for t in threading.enumerate() if t.name == "r-proxy-writer" and t.is_alive()]
        assert threads == []

    async def test_warnings_do_not_block_startup(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """影响正确性的问题拒绝启动，影响可用性的问题告警后降级。"""
        path = write_config(
            tmp_path,
            """
[listen]
port = 0

[webui]
enabled = false

[[upstreams]]
name = "a"
type = "http"
address = "127.0.0.1:3128"

[[upstreams]]
name = "b"
type = "http"
address = "127.0.0.1:3129"
""",
        )
        app = Application(config_path=path)
        with caplog.at_level(logging.WARNING):
            await app.start()
        try:
            assert any("W_ALL_SAME_PRIORITY" in r.message for r in caplog.records)
        finally:
            await app.stop()

    async def test_check_only_reports_issues_without_binding(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path))
        issues = app.check()
        assert [i for i in issues if i.level == "error"] == []
        with pytest.raises(RuntimeError):
            _ = app.proxy_address


class TestShutdown:
    async def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path))
        await app.start()
        await app.stop()
        await app.stop()

    async def test_port_is_released_after_stop(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path))
        await app.start()
        host, port = app.proxy_address
        await app.stop()
        with pytest.raises(OSError):
            _, w = await asyncio.open_connection(host, port)
            w.close()

    async def test_run_returns_when_stop_is_requested(self, tmp_path: Path) -> None:
        app = Application(config_path=write_config(tmp_path))
        task = asyncio.create_task(app.run())
        await app.wait_started()
        app.request_stop()
        await asyncio.wait_for(task, timeout=5)


class TestReload:
    async def test_reload_replaces_the_snapshot(self, tmp_path: Path) -> None:
        path = write_config(tmp_path)
        app = Application(config_path=path)
        await app.start()
        try:
            before = app.snapshot
            path.write_text(
                MINIMAL
                + '\n[[upstreams]]\nname = "p"\ntype = "http"\naddress = "127.0.0.1:3128"\n',
                encoding="utf-8",
            )
            await app.reload()
            assert app.snapshot is not before
            assert len(app.snapshot.upstreams) == 2
        finally:
            await app.stop()

    async def test_reload_keeps_the_old_snapshot_on_error(self, tmp_path: Path) -> None:
        """重载失败必须保留正在生效的配置，否则一次手滑就让代理停摆。"""
        path = write_config(tmp_path)
        app = Application(config_path=path)
        await app.start()
        try:
            before = app.snapshot
            path.write_text("this is not = valid toml [[", encoding="utf-8")
            with pytest.raises(StartupError):
                await app.reload()
            assert app.snapshot is before
        finally:
            await app.stop()

    async def test_reload_does_not_drop_the_listener(self, tmp_path: Path) -> None:
        path = write_config(tmp_path)
        app = Application(config_path=path)
        await app.start()
        try:
            host, port = app.proxy_address
            await app.reload()
            assert app.proxy_address == (host, port)
            _, w = await asyncio.open_connection(host, port)
            w.close()
        finally:
            await app.stop()


class TestRuleLoading:
    """规则来自 rules.db。验收点 M5-20、M5-24、M5-25。"""

    def seed(self, tmp_path: Path, *pairs: tuple[str, str]) -> Path:
        """直接往规则库里塞几条规则，绕开 Web 层。"""
        path = tmp_path / "rules.db"
        store = RulesStore(path)
        store.ensure_schema()
        store.replace(list(pairs), expected_revision=0, now_unix=0)
        return path

    def config_with_rules_db(self, tmp_path: Path, *, enabled: bool | None = None) -> Path:
        text = MINIMAL + f'\n[database]\nrules_path = "{tmp_path / "rules.db"}"\n'
        if enabled is not None:
            text += f"\n[rules]\nenabled = {str(enabled).lower()}\n"
        return write_config(tmp_path, text)

    async def test_rules_are_loaded_at_startup(self, tmp_path: Path) -> None:
        self.seed(tmp_path, ("*.example.com", "direct"))
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        await app.start()
        try:
            assert [(r.position, r.raw) for r in app.rules.rules] == [(0, "*.example.com")]
        finally:
            await app.stop()

    async def test_a_missing_rules_database_is_not_an_error(self, tmp_path: Path) -> None:
        """首次启动时库还不存在。这不是故障，是空规则集。"""
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        await app.start()
        try:
            assert app.rules.is_empty
        finally:
            await app.stop()

    async def test_the_rules_database_is_created_at_startup(self, tmp_path: Path) -> None:
        """建库在启动时做完，Web 第一次保存不必先处理「库不存在」。"""
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        await app.start()
        try:
            assert (tmp_path / "rules.db").exists()
        finally:
            await app.stop()

    async def test_disabled_rules_neither_open_nor_create_the_database(
        self, tmp_path: Path
    ) -> None:
        """M5-20：救场开关。库可能正是坏的那个，连碰都不该碰。"""
        self.seed(tmp_path, ("*", "direct"))
        (tmp_path / "rules.db").write_bytes(b"not a database at all")
        app = Application(config_path=self.config_with_rules_db(tmp_path, enabled=False))
        await app.start()
        try:
            assert app.rules.is_empty
        finally:
            await app.stop()

    def test_a_rule_pointing_at_an_unknown_upstream_refuses_startup(self, tmp_path: Path) -> None:
        """拼错出口名永远是 bug，不存在任何场景下它是故意的。"""
        self.seed(tmp_path, ("*", "typo-proxy"))
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        issues = app.check()
        assert [i.code for i in issues if i.level == "error"] == ["E_RULE_TARGET"]
        assert any(i.location == "rules[0]" for i in issues)

    def test_a_rule_pointing_at_a_disabled_upstream_only_warns(self, tmp_path: Path) -> None:
        """禁用出口是高频运维操作，用户通常不会同时改规则。"""
        self.seed(tmp_path, ("*.example.com", "off"))
        path = write_config(
            tmp_path,
            MINIMAL
            + '\n[[upstreams]]\nname = "off"\ntype = "http"\naddress = "127.0.0.1:3128"\n'
            + "enabled = false\n"
            + f'\n[database]\nrules_path = "{tmp_path / "rules.db"}"\n',
        )
        issues = Application(config_path=path).check()
        assert not [i for i in issues if i.level == "error"]
        assert [i.code for i in issues if "RULE" in i.code] == ["W_RULE_DISABLED_TARGET"]

    async def test_a_bad_condition_refuses_startup(self, tmp_path: Path) -> None:
        """M5-24。库里的条件非法只可能来自手工改库，但启动不能带病运行。"""
        self.seed(tmp_path, ("^(unclosed", "direct"))
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        with pytest.raises(StartupError):
            await app.start()

    def test_check_reports_every_offending_rule_at_once(self, tmp_path: Path) -> None:
        """M5-24：一次报告全部问题，界面据此高亮多行。"""
        self.seed(tmp_path, ("^(unclosed", "direct"), ("ok.com:8443", "direct"))
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        errors = [i for i in app.check() if i.level == "error"]
        assert [i.location for i in errors] == ["rules[0]", "rules[1]"]

    async def test_reload_keeps_the_old_rule_set_on_a_bad_condition(self, tmp_path: Path) -> None:
        """M5-25（承接 M3-17）：规则出错时代理继续按旧规则工作。

        退化为无规则意味着全部流量转为自动路由——那是一次静默的、全局的路由变更。
        """
        path = self.seed(tmp_path, ("*.example.com", "direct"))
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        await app.start()
        try:
            before = app.rules
            RulesStore(path).replace([("^(unclosed", "direct")], expected_revision=1, now_unix=0)
            with pytest.raises(StartupError):
                await app.reload()
            assert app.rules is before
            host, port = app.proxy_address
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
        finally:
            await app.stop()

    async def test_reload_picks_up_edited_rules(self, tmp_path: Path) -> None:
        path = self.seed(tmp_path, ("*.example.com", "direct"))
        app = Application(config_path=self.config_with_rules_db(tmp_path))
        await app.start()
        try:
            RulesStore(path).replace([("*.other.com", "direct")], expected_revision=1, now_unix=0)
            await app.reload()
            assert [r.raw for r in app.rules.rules] == ["*.other.com"]
        finally:
            await app.stop()


class TestCli:
    def test_check_mode_returns_zero_for_valid_config(self, tmp_path: Path) -> None:
        assert cli.main(["--config", str(write_config(tmp_path)), "--check"]) == 0

    def test_check_mode_returns_two_for_invalid_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_config(
            tmp_path,
            "[listen]\nport = 0\n[webui]\nenabled = false\n"
            '[[upstreams]]\nname = "d"\ntype = "direct"\nenabled = false\n',
        )
        assert cli.main(["--config", str(path), "--check"]) == 2
        assert "E_NO_UPSTREAM" in capsys.readouterr().err

    def test_missing_config_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--config", str(tmp_path / "nope.toml"), "--check"]) == 2
        assert "nope.toml" in capsys.readouterr().err

    def test_no_web_flag_disables_the_web_ui(self, tmp_path: Path) -> None:
        path = write_config(
            tmp_path,
            "[listen]\nport = 0\n[webui]\nenabled = true\nport = 6061\n"
            '[[upstreams]]\nname = "d"\ntype = "direct"\n',
        )
        overrides = cli.build_overrides(cli.parse_args(["--config", str(path), "--no-web"]))
        assert overrides["webui.enabled"] is False

    def test_port_override(self, tmp_path: Path) -> None:
        args = cli.parse_args(["--config", str(tmp_path / "c.toml"), "--port", "7070"])
        assert cli.build_overrides(args)["listen.port"] == 7070

    def test_no_overrides_when_flags_absent(self) -> None:
        assert cli.build_overrides(cli.parse_args([])) == {}

    def test_version_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as e:
            cli.parse_args(["--version"])
        assert e.value.code == 0
