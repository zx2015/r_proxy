"""历史规则迁移盘点只能读取部署配置与 SQLite 快照。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from r_proxy.rules.audit import AuditError, audit_config, main


def fixture_config(tmp_path: Path, rows: list[tuple[int, str, str]]) -> tuple[Path, Path]:
    database = tmp_path / "rules #测试.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE rule (id INTEGER PRIMARY KEY, position INTEGER, condition TEXT, "
            "upstream TEXT, updated_at INTEGER);"
            "CREATE TABLE rule_meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO rule_meta VALUES ('revision', '17');"
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO schema_meta VALUES ('schema_version', '1');"
        )
        conn.executemany(
            "INSERT INTO rule (position,condition,upstream,updated_at) VALUES (?,?,?,0)", rows
        )
    config = tmp_path / "config.toml"
    config.write_text(
        '[rules]\nenabled = true\n[database]\nrules_path = "' + str(database) + '"\n'
        '[[upstreams]]\nname = "disabled"\nenabled = false\n'
        '[[upstreams]]\nname = "proxy"\nenabled = true\n'
        '[upstreams.auth]\nusername = "private-user"\npassword = "private-secret"\n'
        '[webui]\nauth_token = "private-token"\n',
        encoding="utf-8",
    )
    return config, database


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path, [])
    database.unlink()
    with pytest.raises(AuditError, match="不存在"):
        audit_config(config)
    assert not database.exists()


def test_inventory_reads_real_positions_and_does_not_write(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path, [(7, "api.test", "direct")])
    before = database.read_bytes()
    report = audit_config(config)
    assert report.revision == "17"
    assert report.schema_version == "1"
    assert report.rule_count == 1
    assert report.rows[0].position == 7
    assert report.snapshot_consistent
    assert report.source_changes == 0
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM rule_meta WHERE key='revision'").fetchone() == (
            "17",
        )


def test_container_data_path_requires_explicit_mapping(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path, [(0, "a.test", "direct")])
    config.write_text('[database]\nrules_path = "/data/' + database.name + '"\n', encoding="utf-8")
    report = audit_config(config, data_dir=tmp_path)
    assert report.database_path == str(database)
    assert report.ready


def test_reports_regular_expressions_and_overlaps(tmp_path: Path) -> None:
    config, _ = fixture_config(
        tmp_path,
        [(0, "api.test", "direct"), (1, "*.test", "direct"), (2, "^old.*", "proxy")],
    )
    report = audit_config(config)
    codes = {issue.code for issue in report.issues}
    assert report.regex_count == 1
    assert "E_RULE_REGEX_UNSUPPORTED" in codes
    assert "E_RULE_OVERLAP" in codes
    assert not report.ready
    assert report.analysis_complete


def test_reports_unknown_disabled_targets_and_bad_positions(tmp_path: Path) -> None:
    config, _ = fixture_config(
        tmp_path,
        [(2, "a.test", "missing"), (2, "b.test", "disabled"), (-1, "c.test", "direct")],
    )
    report = audit_config(config)
    codes = {issue.code for issue in report.issues}
    assert {"E_RULE_TARGET_UNKNOWN", "W_RULE_TARGET_DISABLED", "W_RULE_POSITION"} <= codes
    assert not report.ready


def test_cli_summary_and_detailed_report_do_not_leak_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _ = fixture_config(tmp_path, [(0, "private-host.test", "direct")])
    output = tmp_path / "report.json"
    assert main(["--config", str(config), "--output", str(output)]) == 0
    console = capsys.readouterr().out
    detailed = output.read_text(encoding="utf-8")
    assert "private-host.test" not in console
    assert "private-host.test" in detailed
    for secret in ("private-user", "private-secret", "private-token"):
        assert secret not in console
        assert secret not in detailed
    assert json.loads(console)["ready"] is True


def test_bad_schema_returns_a_clear_error_without_migration(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path, [])
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE rule")
    before = database.read_bytes()
    with pytest.raises(AuditError, match="rule"):
        audit_config(config)
    assert database.read_bytes() == before


def test_cli_output_cannot_overwrite_the_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _ = fixture_config(tmp_path, [(0, "a.test", "direct")])
    before = config.read_bytes()
    assert main(["--config", str(config), "--output", str(config)]) == 2
    assert config.read_bytes() == before
    assert "private-token" not in capsys.readouterr().err


def test_legacy_analyzer_never_claims_exclusive_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from r_proxy.rules import loader

    config, _ = fixture_config(tmp_path, [(0, "a.test", "direct")])
    monkeypatch.setattr(loader, "ANALYSIS_VERSION", "legacy", raising=False)
    report = audit_config(config)
    assert not report.ready
    assert not report.analysis_complete
    assert any(issue.code == "E_RULE_CHECK_INCOMPLETE" for issue in report.issues)
