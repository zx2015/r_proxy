"""config/loader.py 的加载与未知键检查测试。

对应设计：docs/design/DD_CONFIG.md §3、§3.0、§3.1
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from r_proxy.config.loader import ConfigError, load, load_text

MINIMAL = """
[[upstreams]]
name = "direct"
type = "direct"
"""


def write(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class TestBasicLoading:
    def test_minimal_config_fills_all_defaults(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL))
        assert snap.listen.host == "127.0.0.1"
        assert snap.listen.port == 6060
        assert snap.webui.port == 6061
        assert snap.routing.connect_timeout == 10.0
        assert snap.limits.max_client_connections == 1000
        assert len(snap.upstreams) == 1

    def test_direct_upstream_needs_no_address(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL))
        assert snap.upstream("direct") is not None
        assert snap.upstream("direct").address is None  # type: ignore[union-attr]

    def test_explicit_values_override_defaults(self, tmp_path: Path) -> None:
        snap = load(
            write(
                tmp_path,
                MINIMAL
                + """
[listen]
port = 7070

[routing]
connect_timeout = 4
""",
            )
        )
        assert snap.listen.port == 7070
        assert snap.routing.connect_timeout == 4.0

    def test_int_is_coerced_to_float_for_timeouts(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL + "\n[routing]\nread_timeout = 15\n"))
        assert isinstance(snap.routing.read_timeout, float)
        assert snap.routing.read_timeout == 15.0

    def test_switch_on_status_becomes_frozenset(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL + "\n[routing]\nswitch_on_status = [502, 503]\n"))
        assert snap.routing.switch_on_status == frozenset({502, 503})

    def test_nested_subtables_are_loaded(self, tmp_path: Path) -> None:
        snap = load(
            write(
                tmp_path,
                MINIMAL
                + """
[routing.circuit_breaker]
fail_threshold = 9

[routing.status_switch_rate_limit]
window_seconds = 30
""",
            )
        )
        assert snap.routing.circuit_breaker.fail_threshold == 9
        assert snap.routing.circuit_breaker.cooldown_seconds == 60  # 未指定项保持默认
        assert snap.routing.status_switch_rate_limit.window_seconds == 30

    def test_upstreams_preserve_file_order(self, tmp_path: Path) -> None:
        snap = load(
            write(
                tmp_path,
                """
[[upstreams]]
name = "b"
type = "http"
address = "10.0.0.2:8080"

[[upstreams]]
name = "a"
type = "http"
address = "10.0.0.1:8080"
""",
            )
        )
        assert [u.name for u in snap.upstreams] == ["b", "a"]


class TestConfigVersion:
    def test_is_sha256_prefix_of_file_bytes(self, tmp_path: Path) -> None:
        p = write(tmp_path, MINIMAL)
        expected = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        assert load(p).config_version == expected

    def test_changes_when_only_a_comment_changes(self, tmp_path: Path) -> None:
        """内容哈希对任何字节变化敏感，这是外部编辑检测的基础。"""
        first = load(write(tmp_path, MINIMAL)).config_version
        second = load(write(tmp_path, MINIMAL + "\n# 只加了一行注释\n")).config_version
        assert first != second


class TestUnknownKeys:
    def test_unknown_top_level_key_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as e:
            load(write(tmp_path, MINIMAL + "\n[whatever]\nx = 1\n"))
        assert "E_UNKNOWN_KEY" in str(e.value)
        assert "whatever" in str(e.value)

    def test_typo_gets_a_suggestion(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as e:
            load(write(tmp_path, MINIMAL + "\n[routing]\nconnect_timout = 5\n"))
        assert "connect_timeout" in str(e.value)

    def test_legal_key_in_wrong_table_names_the_right_table(self, tmp_path: Path) -> None:
        """TOML 表头陷阱：[[upstreams]] 之后的裸键归属该表。DD_CONFIG §1.1.1。"""
        with pytest.raises(ConfigError) as e:
            load(
                write(
                    tmp_path,
                    """
[[upstreams]]
name = "direct"
type = "direct"
sticky_fail_threshold = 5
""",
                )
            )
        msg = str(e.value)
        assert "sticky_fail_threshold" in msg
        assert "routing" in msg

    def test_all_unknown_keys_reported_at_once(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as e:
            load(write(tmp_path, MINIMAL + "\n[listen]\nfoo = 1\nbar = 2\n"))
        assert "foo" in str(e.value) and "bar" in str(e.value)

    def test_known_keys_pass(self, tmp_path: Path) -> None:
        load(write(tmp_path, MINIMAL + "\n[webui]\nenabled = false\nauth_token = 'x'\n"))


class TestMalformedInput:
    def test_syntax_error_reports_path(self, tmp_path: Path) -> None:
        p = write(tmp_path, "[listen\nport = 1")
        with pytest.raises(ConfigError) as e:
            load(p)
        assert str(p) in str(e.value)

    def test_non_utf8_gets_a_readable_message(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_bytes("[listen]\nhost = '127.0.0.1'  # \u4e2d\u6587".encode("gbk"))
        with pytest.raises(ConfigError) as e:
            load(p)
        assert "UTF-8" in str(e.value)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as e:
            load(tmp_path / "nope.toml")
        assert "nope.toml" in str(e.value)

    def test_upstream_without_name_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as e:
            load(write(tmp_path, '[[upstreams]]\ntype = "direct"\n'))
        assert "name" in str(e.value)

    def test_wrong_scalar_type_is_rejected(self, tmp_path: Path) -> None:
        p = write(tmp_path, MINIMAL + '\n[listen]\nport = "6060"\n')
        with pytest.raises(ConfigError) as e:
            load(p)
        assert "listen.port" in str(e.value)

    def test_every_error_names_the_file(self, tmp_path: Path) -> None:
        """多配置文件场景下，不指明文件名的报错无法定位。"""
        p = write(tmp_path, MINIMAL + '\n[listen]\nport = "6060"\n')
        with pytest.raises(ConfigError) as e:
            load(p)
        assert str(p) in str(e.value)


class TestRulesSection:
    def test_rules_are_enabled_by_default(self, tmp_path: Path) -> None:
        assert load(write(tmp_path, MINIMAL)).rules_enabled is True

    def test_enabled_false_is_read(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL + "\n[rules]\nenabled = false\n"))
        assert snap.rules_enabled is False

    def test_leftover_files_key_is_rejected_with_instructions(self, tmp_path: Path) -> None:
        """M5-19：静默忽略会让用户以为文件里的规则还在生效。"""
        with pytest.raises(ConfigError) as e:
            load(write(tmp_path, MINIMAL + '\n[rules]\nfiles = ["user.rules"]\n'))
        message = str(e.value)
        assert "E_RULES_FILES_REMOVED" in message
        assert "rules.db" in message
        assert "RULES_CONFIG.md" in message

    def test_rules_path_defaults_next_to_the_other_databases(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL))
        assert snap.database.rules_path.name == "rules.db"
        assert snap.database.rules_path.parent == snap.database.state_path.parent

    def test_rules_path_is_configurable(self, tmp_path: Path) -> None:
        text = MINIMAL + f'\n[database]\nrules_path = "{tmp_path / "custom.db"}"\n'
        assert load(write(tmp_path, text)).database.rules_path == tmp_path / "custom.db"


class TestOverridePrecedence:
    def test_env_supplies_web_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("R_PROXY_WEB_TOKEN", "from-env-0123456789")
        assert load(write(tmp_path, MINIMAL)).webui.auth_token == "from-env-0123456789"

    def test_env_beats_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("R_PROXY_WEB_TOKEN", "from-env-0123456789")
        snap = load(write(tmp_path, MINIMAL + "\n[webui]\nauth_token = 'from-file'\n"))
        assert snap.webui.auth_token == "from-env-0123456789"

    def test_cli_beats_env_and_file(self, tmp_path: Path) -> None:
        snap = load(
            write(tmp_path, MINIMAL + "\n[webui]\nport = 1111\n"),
            cli_overrides={"webui.port": 2222},
        )
        assert snap.webui.port == 2222

    def test_no_web_override(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL), cli_overrides={"webui.enabled": False})
        assert snap.webui.enabled is False

    def test_config_version_reflects_file_only(self, tmp_path: Path) -> None:
        """覆盖来自 CLI 而非文件，不应改变 config_version（它是文件的指纹）。"""
        p = write(tmp_path, MINIMAL)
        assert load(p).config_version == load(p, cli_overrides={"webui.port": 9}).config_version


class TestPathExpansion:
    def test_tilde_in_database_paths_is_expanded(self, tmp_path: Path) -> None:
        snap = load(write(tmp_path, MINIMAL + '\n[database]\nstate_path = "~/x/state.db"\n'))
        assert not str(snap.database.state_path).startswith("~")
        assert snap.database.state_path.is_absolute()


def test_legacy_enabled_circuit_config_is_accepted_but_cannot_enable_it(tmp_path: Path) -> None:
    snap = load(
        write(
            tmp_path, MINIMAL + "\n[routing.circuit_breaker]\nenabled = true\nfail_threshold = 1\n"
        )
    )
    assert snap.routing.circuit_breaker.enabled is False


def test_relative_database_paths_use_the_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    text = (
        MINIMAL
        + """
[database]
state_path = "data/state.db"
logs_path = "data/logs.db"
rules_path = "data/rules.db"
"""
    )
    path = write(config_dir, text)
    for cwd in (tmp_path, config_dir):
        monkeypatch.chdir(cwd)
        for snapshot in (load(path), load_text(text, path)):
            assert snapshot.database.state_path == config_dir / "data/state.db"
            assert snapshot.database.logs_path == config_dir / "data/logs.db"
            assert snapshot.database.rules_path == config_dir / "data/rules.db"


@pytest.mark.skipif(os.name != "posix", reason="POSIX rejects Windows drive paths")
def test_windows_drive_paths_are_rejected_on_posix(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL + "\n[database]\nstate_path = 'C:\\r-proxy\\state.db'\n")
    with pytest.raises(ConfigError, match="database.state_path.*Windows 盘符路径"):
        load(path)


def test_database_home_expansion_error_names_the_user_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.expanduser

    def unavailable(path: Path) -> Path:
        if str(path).startswith("~unknownuser"):
            raise RuntimeError("home unavailable")
        return original(path)

    monkeypatch.setattr(Path, "expanduser", unavailable)
    config = write(tmp_path, MINIMAL + "\n[database]\nstate_path = '~unknownuser/state.db'\n")
    with pytest.raises(ConfigError, match="无法展开用户目录.*~unknownuser"):
        load(config)


@pytest.mark.parametrize("candidate", ["missing", "empty", "foreign_schema", "valid", "unrelated"])
def test_legacy_databases_are_classified_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    candidate: str,
) -> None:
    from r_proxy.storage.schema import Database, open_write

    cwd = tmp_path / "cwd"
    conf = tmp_path / "conf"
    cwd.mkdir()
    conf.mkdir()
    text = MINIMAL + '\n[database]\nstate_path = "state.db"\nlogs_path = "logs.db"\n'
    text += 'rules_path = "rules.db"\n'
    for name, kind in (
        ("state", Database.STATE),
        ("logs", Database.LOGS),
        ("rules", Database.RULES),
    ):
        old, new = cwd / f"{name}.db", conf / f"{name}.db"
        if candidate == "unrelated":
            old.write_text("not a database", encoding="utf-8")
        else:
            open_write(old, kind).close()
        if candidate == "empty":
            new.touch()
        elif candidate == "foreign_schema":
            with closing(sqlite3.connect(new)) as conn:
                conn.execute("CREATE TABLE unrelated (value TEXT)")
        elif candidate == "valid":
            open_write(new, kind).close()
    monkeypatch.chdir(cwd)
    config = write(conf, text)
    if candidate in {"missing", "empty", "foreign_schema"}:
        with pytest.raises(ConfigError, match="E_LEGACY_DB_PATH") as exc:
            load(config)
        for name in ("state", "logs", "rules"):
            assert f"database.{name}_path" in str(exc.value)
            assert str(cwd / f"{name}.db") in str(exc.value)
            assert str(conf / f"{name}.db") in str(exc.value)
    else:
        assert load(config).database.rules_path == conf / "rules.db"
        warnings = [msg for msg in caplog.messages if "W_LEGACY_DB_PATH" in msg]
        assert len(warnings) == (3 if candidate == "valid" else 0)
