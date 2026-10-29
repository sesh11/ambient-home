"""Import boundary and runtime purity checks."""

import ast
import tomllib
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "src" / "ambient_home"


def test_openai_imports_are_limited_to_connectors() -> None:
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        if any(name == "openai" or name.startswith("openai.") for name in imports):
            assert "connectors" in path.parts
        assert not any(
            name.startswith(("reachy_mini", "ambient_devices")) or name == "garage" or name.startswith("garage.")
            for name in imports
        )
        assert not any(
            name.startswith(("ambient_home.factory", "ambient_home.incidents", "ambient_home.telemetry"))
            for name in imports
        )

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    dependencies = [*pyproject["project"]["dependencies"], *pyproject["dependency-groups"]["dev"]]
    assert not any("reachy" in dependency.lower() for dependency in dependencies)
