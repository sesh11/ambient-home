"""Command-line option tests for the ambient-home entry point."""

import pytest

import ambient_home.main as main_module
from ambient_home import __version__


def test_version_prints_and_exits_without_starting_hub(monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("runtime started")

    monkeypatch.setattr(main_module, "load_dotenv", fail)
    monkeypatch.setattr(main_module, "settings_from_env", fail)
    monkeypatch.setattr(main_module, "build_app", fail)

    with pytest.raises(SystemExit) as exc:
        main_module.main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ambient-home {__version__}"


def test_no_arguments_still_runs_hub(monkeypatch) -> None:
    calls: list[str] = []

    class FakeApp:
        async def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(main_module, "load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setattr(main_module, "settings_from_env", lambda: object())
    monkeypatch.setattr(main_module, "build_app", lambda settings: FakeApp())

    main_module.main([])

    assert calls == ["dotenv", "run"]


def test_version_matches_package_metadata() -> None:
    from importlib.metadata import version

    assert __version__ == version("ambient-home")
