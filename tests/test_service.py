"""LaunchAgent installer tests."""

import plistlib
from pathlib import Path

import pytest

from ambient_home import service
from ambient_home.service import LABELS, APP_LABEL, DAEMON_LABEL, install, uninstall, render_plist, resolve_paths


REPO_DIR = Path(__file__).resolve().parent.parent


def _fake_uv(tmp_path: Path) -> Path:
    uv_bin = tmp_path / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    return uv_bin


def _paths(tmp_path: Path) -> service.ServicePaths:
    return resolve_paths(REPO_DIR, uv_bin=_fake_uv(tmp_path), home=tmp_path)


def test_render_plists_substitute_machine_paths(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    daemon = plistlib.loads(render_plist(DAEMON_LABEL, paths).encode())
    app = plistlib.loads(render_plist(APP_LABEL, paths).encode())

    assert daemon["Label"] == DAEMON_LABEL
    assert daemon["ProgramArguments"] == ["/bin/sh", str(REPO_DIR / "scripts" / "run-daemon.sh")]
    assert daemon["EnvironmentVariables"]["UV_BIN"] == str(tmp_path / "bin" / "uv")
    assert daemon["StandardErrorPath"] == str(tmp_path / "Library/Logs/ambient-home/daemon.log")

    assert app["ProgramArguments"] == [str(tmp_path / "bin" / "uv"), "run", "ambient-home"]
    assert app["WorkingDirectory"] == str(REPO_DIR)
    assert app["KeepAlive"] == {"SuccessfulExit": False}
    assert app["RunAtLoad"] is True
    assert app["ThrottleInterval"] == 10
    assert app["StandardOutPath"] == str(tmp_path / "Library/Logs/ambient-home/app.log")
    assert set(app["EnvironmentVariables"]) == {"PATH", "HOME"}
    assert str(tmp_path / "bin") in app["EnvironmentVariables"]["PATH"]


def test_render_plists_escape_xml_metacharacters(tmp_path: Path) -> None:
    home = tmp_path / "R&D <home>"
    paths = resolve_paths(REPO_DIR, uv_bin=_fake_uv(home), home=home)

    app = plistlib.loads(render_plist(APP_LABEL, paths).encode())

    assert app["ProgramArguments"][0] == str(home / "bin" / "uv")
    assert app["EnvironmentVariables"]["HOME"] == str(home)


def test_resolve_paths_requires_repo(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_paths(tmp_path, uv_bin=_fake_uv(tmp_path), home=tmp_path)


def test_resolve_paths_rejects_missing_uv_bin(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="uv not found"):
        resolve_paths(REPO_DIR, uv_bin=tmp_path / "missing" / "uv", home=tmp_path)

    paths = resolve_paths(REPO_DIR, uv_bin=tmp_path / "missing" / "uv", home=tmp_path, require_uv=False)
    assert paths.uv_bin == (tmp_path / "missing" / "uv").resolve()


def test_install_and_uninstall_drive_launchctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(service, "launchctl", lambda *args, check=True: calls.append(args) or Result())
    monkeypatch.setattr(service, "domain", lambda: "gui/501")
    paths = _paths(tmp_path)

    written = install(paths)

    assert [path.name for path in written] == [f"{label}.plist" for label in LABELS]
    assert all(path.is_file() for path in written)
    assert paths.log_dir.is_dir()
    assert calls == [
        ("bootout", f"gui/501/{DAEMON_LABEL}"),
        ("bootstrap", "gui/501", str(paths.plist_path(DAEMON_LABEL))),
        ("bootout", f"gui/501/{APP_LABEL}"),
        ("bootstrap", "gui/501", str(paths.plist_path(APP_LABEL))),
    ]

    calls.clear()
    removed = uninstall(paths)

    assert [path.name for path in removed] == [f"{APP_LABEL}.plist", f"{DAEMON_LABEL}.plist"]
    assert not any(path.exists() for path in written)
    assert calls == [("bootout", f"gui/501/{APP_LABEL}"), ("bootout", f"gui/501/{DAEMON_LABEL}")]


def test_install_dry_run_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(service, "launchctl", lambda *args, check=True: pytest.fail("launchctl must not run"))
    paths = _paths(tmp_path)

    install(paths, dry_run=True)

    assert not paths.agents_dir.exists()
    assert DAEMON_LABEL in capsys.readouterr().out
