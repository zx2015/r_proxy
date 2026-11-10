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
    app = SimpleNamespace(snapshot=SimpleNamespace(
        source_path=path,
        database=SimpleNamespace(state_path=tmp_path / "state.db", backup_keep=10),
    ))
    writer = ConfigWriter(app)  # type: ignore[arg-type]
    text = '# 中文\r\nvalue = "ok"\r\n'
    with monkeypatch.context() as patch:
        patch.setattr(config_writer.os, "name", "nt")
        patch.delattr(config_writer.os, "fchmod", raising=False)
        patch.delattr(config_writer.os, "O_DIRECTORY", raising=False)
        writer._replace(path, text)
    assert path.read_bytes() == text.encode("utf-8")
