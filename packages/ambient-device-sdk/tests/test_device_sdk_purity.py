"""Import boundary and built-wheel package checks."""

import ast
import sys
import shutil
import zipfile
import subprocess
from pathlib import Path

import pytest

from ambient_device_sdk import __version__


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "packages" / "ambient-device-sdk" / "src"


def test_source_imports_only_stdlib_and_ambient_device_sdk() -> None:
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module.split(".", maxsplit=1)[0])
        assert set(modules) <= sys.stdlib_module_names | {"ambient_device_sdk"}, path


def test_import_does_not_load_home_or_external_dependencies() -> None:
    code = (
        "import ambient_device_sdk, ambient_device_sdk.testing, sys; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'numpy','ambient_home','openai','reachy_mini','reachy_mini_conversation_app',"
        "'garage','garage_sdk','pydantic'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "[]"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_built_wheel_is_pure_and_contains_typing_marker(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
            "packages/ambient-device-sdk",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        files = set(archive.namelist())
        assert "ambient_device_sdk/py.typed" in files
        metadata_path = next(name for name in files if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_path).decode()
    assert f"Version: {__version__}" in metadata
    assert "Requires-Dist:" not in metadata
