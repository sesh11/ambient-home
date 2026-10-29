"""Persistent Alfred memory compatibility and privacy tests."""

import json
from pathlib import Path

import pytest

from ambient_home.config import settings_from_env
from ambient_home.memory import (
    MAX_FACTS,
    MAX_FACT_CHARS,
    memory_path,
    add_memory_fact,
    list_memory_facts,
    clear_memory_facts,
    forget_memory_fact,
    normalize_memory_text,
    format_memory_for_prompt,
)


@pytest.fixture
def baseline_memory_file(tmp_path: Path) -> Path:
    path = tmp_path / "reachy_mini_conversation_app" / "memory.v1.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "facts": [
                    {"id": "m_2_newest", "text": "  Prefers   tea  ", "createdAt": 2000},
                    {"id": "m_1_older", "text": "Has a cat named Miso", "createdAt": 1000},
                    {"id": 42, "text": "malformed id", "createdAt": 500},
                    {"id": "m_bad_text", "text": None, "createdAt": 400},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_reads_baseline_schema_and_skips_malformed_entries(baseline_memory_file: Path) -> None:
    facts = list_memory_facts(baseline_memory_file)

    assert [(fact.id, fact.text, fact.created_at) for fact in facts] == [
        ("m_2_newest", "Prefers tea", 2000),
        ("m_1_older", "Has a cat named Miso", 1000),
    ]


def test_add_deduplicates_newest_first_caps_and_truncates(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    first = add_memory_fact(path, "  Likes   coffee ")
    duplicate = add_memory_fact(path, "likes coffee")
    assert first is not None and duplicate is not None
    assert duplicate.id == first.id

    clear_memory_facts(path)
    for index in range(MAX_FACTS + 5):
        assert add_memory_fact(path, f"fact {index}") is not None
    facts = list_memory_facts(path)
    assert len(facts) == MAX_FACTS
    assert facts[0].text == f"fact {MAX_FACTS + 4}"
    assert facts[-1].text == "fact 5"

    truncated = normalize_memory_text("x" * (MAX_FACT_CHARS + 1))
    assert len(truncated) == MAX_FACT_CHARS
    assert truncated.endswith("...")
    assert add_memory_fact(path, "y" * (MAX_FACT_CHARS + 1)) is not None
    assert list_memory_facts(path)[0].text == "y" * (MAX_FACT_CHARS - 3) + "..."


def test_forget_removes_first_case_insensitive_match_and_reports_candidates(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    add_memory_fact(path, "Mochi loves walks")
    add_memory_fact(path, "Mochi likes naps")

    result = forget_memory_fact(path, query="mochi")
    assert result.removed is not None
    assert result.removed.text == "Mochi likes naps"
    assert [fact.text for fact in result.candidates] == ["Mochi likes naps", "Mochi loves walks"]
    assert [fact.text for fact in list_memory_facts(path)] == ["Mochi loves walks"]
    assert forget_memory_fact(path, query="unknown").removed is None


def test_written_file_keeps_baseline_shape_and_format(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    stored = add_memory_fact(path, "Prefers replies in French")

    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert text.endswith("\n")
    assert text.splitlines()[1].startswith("  ")
    assert payload == {
        "version": 1,
        "facts": [{"id": stored.id, "text": "Prefers replies in French", "createdAt": stored.created_at}],
    }


def test_memory_path_uses_baseline_xdg_directory_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("AMBIENT_MEMORY_FILE", raising=False)
    settings = settings_from_env()
    assert memory_path(settings) == tmp_path / "xdg" / "reachy_mini_conversation_app" / "memory.v1.json"

    override = tmp_path / "custom" / "memory.json"
    monkeypatch.setenv("AMBIENT_MEMORY_FILE", str(override))
    assert memory_path(settings_from_env()) == override


def test_unreadable_and_invalid_files_fail_closed_without_logging_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "memory.json"
    sentinel = "private memory sentinel"
    path.write_text(f"{{broken {sentinel}", encoding="utf-8")
    assert list_memory_facts(path) == []
    assert sentinel not in caplog.text

    original_read_text = Path.read_text

    def unreadable(target: Path, *args: object, **kwargs: object) -> str:
        if target == path:
            raise PermissionError("access denied")
        return original_read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert list_memory_facts(path) == []
    assert sentinel not in caplog.text


def test_prompt_format_matches_baseline_wording(baseline_memory_file: Path) -> None:
    assert format_memory_for_prompt(baseline_memory_file) == (
        "Things you remember about the user (use this context naturally,\n"
        "do not recite the list verbatim):\n"
        "- Prefers tea\n"
        "- Has a cat named Miso"
    )
