"""LaunchAgent installer tests."""

import plistlib
from pathlib import Path

import pytest

from ambient_home import service
from ambient_home.service import LABELS, APP_LABEL, install, uninstall, render_plist, resolve_paths


REPO_DIR = Path(__file__).resolve().parent.parent


def _fake_uv(tmp_path: Path) -> Path:
    uv_bin = tmp_path / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    return uv_bin


def _paths(tmp_path: Path) -> service.ServicePaths:
    return resolve_paths(REPO_DIR, uv_bin=_fake_uv(tmp_path), home=tmp_path)


def test_only_app_agent_is_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(service, "launchctl", lambda *args, check=True: calls.append(args) or Result())
    monkeypatch.setattr(service, "domain", lambda: "gui/501")
    paths = _paths(tmp_path)
    assert LABELS == (APP_LABEL,)

    written = install(paths)
    assert [path.name for path in written] == [f"{APP_LABEL}.plist"]
    app = plistlib.loads(render_plist(APP_LABEL, paths).encode())
    assert app["ProgramArguments"] == [str(tmp_path / "bin" / "uv"), "run", "ambient-home"]
    assert app["WorkingDirectory"] == str(REPO_DIR)
    assert calls == [
        ("bootout", f"gui/501/{APP_LABEL}"),
        ("bootstrap", "gui/501", str(paths.plist_path(APP_LABEL))),
    ]
    calls.clear()
    assert uninstall(paths) == [paths.plist_path(APP_LABEL)]
    assert calls == [("bootout", f"gui/501/{APP_LABEL}")]


def test_resolve_paths_rejects_missing_repo_and_uv(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_paths(tmp_path, uv_bin=_fake_uv(tmp_path), home=tmp_path)
    with pytest.raises(FileNotFoundError, match="uv not found"):
        resolve_paths(REPO_DIR, uv_bin=tmp_path / "missing" / "uv", home=tmp_path)
