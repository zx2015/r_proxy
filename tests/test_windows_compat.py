"""Platform capability regressions runnable on Linux and Windows."""

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from r_proxy.app import _nofile_limit
from r_proxy.web.config_writer import ConfigWriter


def test_resource_module_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    original = builtins.__import__

    def missing(name: str, *args: object, **kwargs: object) -> object:
        if name == "resource":
            raise ImportError("Windows has no resource module")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert _nofile_limit() is None


def test_windows_replace_preserves_bytes_without_unix_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from r_proxy.web import config_writer

    path = tmp_path / "config.toml"
    path.write_bytes(b"old\n")
    app = SimpleNamespace(
        snapshot=SimpleNamespace(
            source_path=path,
            database=SimpleNamespace(state_path=tmp_path / "state.db", backup_keep=10),
        )
    )
    writer = ConfigWriter(app)  # type: ignore[arg-type]
    text = '# 中文\r\nvalue = "ok"\r\n'
    with monkeypatch.context() as patch:
        patch.setattr(config_writer.os, "name", "nt")
        patch.delattr(config_writer.os, "fchmod", raising=False)
        patch.delattr(config_writer.os, "O_DIRECTORY", raising=False)
        writer._replace(path, text)
    assert path.read_bytes() == text.encode("utf-8")


async def test_windows_sigbreak_requests_stop_and_restores_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from r_proxy import cli

    handlers = {}
    stopped = asyncio.Event()
    previous = object()

    async def run() -> None:
        handler = handlers[21]
        handler(21, None)
        await asyncio.wait_for(stopped.wait(), 1)

    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(cli.signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(cli.signal, "getsignal", lambda sig: previous)
    monkeypatch.setattr(cli.signal, "signal", handlers.__setitem__)
    await cli._serve(SimpleNamespace(run=run, request_stop=stopped.set))
    assert handlers[21] is previous


@pytest.mark.parametrize("persistent", [False, True])
def test_windows_replace_retries_sharing_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    from r_proxy.web import config_writer

    path = tmp_path / "config.toml"
    path.write_bytes(b"old\n")
    app = SimpleNamespace(
        snapshot=SimpleNamespace(
            source_path=path,
            database=SimpleNamespace(state_path=tmp_path / "state.db", backup_keep=10),
        )
    )
    writer = ConfigWriter(app)
    replace = config_writer.os.replace
    delays = []
    attempts = 0

    def sharing_violation(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if persistent or attempts <= 2:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        replace(source, destination)

    text = "# 中文\r\n"
    with monkeypatch.context() as patch:
        patch.setattr(config_writer.os, "name", "nt")
        patch.setattr(config_writer.os, "replace", sharing_violation)
        patch.setattr(config_writer.time, "sleep", delays.append)
        if persistent:
            with pytest.raises(PermissionError, match="sharing violation"):
                writer._replace(path, text)
        else:
            writer._replace(path, text)
    assert attempts == (4 if persistent else 3)
    assert len(delays) == attempts - 1
    assert path.read_bytes() == (b"old\n" if persistent else text.encode("utf-8"))
    assert not path.with_name("config.toml.tmp").exists()


def test_windows_default_config_preserves_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from r_proxy import cli
    from r_proxy.config import loader

    monkeypatch.setattr(loader, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(loader.ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    args = cli.parse_args([])
    preferred = tmp_path / "Roaming/r-proxy/config.toml"
    assert cli.resolve_config_path(args) == preferred
    legacy = tmp_path / ".config/r-proxy/config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    assert cli.resolve_config_path(args) == legacy
    preferred.parent.mkdir(parents=True)
    preferred.touch()
    assert cli.resolve_config_path(args) == preferred


def test_windows_cli_emits_utf8_to_redirected_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from r_proxy import cli

    path = tmp_path / "config.toml"
    path.write_text('[[upstreams]]\nname = "direct"\ntype = "direct"\n', encoding="utf-8")
    output, errors = tmp_path / "out.txt", tmp_path / "err.txt"
    with (
        output.open("w", encoding="cp1252") as stdout,
        errors.open("w", encoding="cp1252") as stderr,
    ):
        with monkeypatch.context() as patch:
            patch.setattr(
                cli, "sys", SimpleNamespace(platform="win32", stdout=stdout, stderr=stderr)
            )
            patch.setattr(sys, "stdout", stdout)
            assert cli.main(["--check", "--config", str(path)]) == 0
            assert stderr.encoding == "utf-8"
    assert "配置校验通过" in output.read_text(encoding="utf-8")
