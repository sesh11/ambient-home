"""Core tool contracts and result semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ambient_home.tools.core import build_core_tools


@pytest.mark.asyncio
async def test_tool_names_schemas_and_results(monkeypatch, tmp_path) -> None:
    dispatched = SimpleNamespace(id="job-1")
    answered = SimpleNamespace(id="job-1", status=SimpleNamespace(value="working"))
    jobs = SimpleNamespace(
        dispatch=AsyncMock(return_value=dispatched),
        answer=AsyncMock(return_value=answered),
        status_summary=lambda: [{"id": "job-1", "status": "working"}],
    )
    costs = SimpleNamespace(report=lambda now: {"today": {"usd": 1.0}})

    def close() -> None:
        return

    monkeypatch.setattr("ambient_home.tools.core.spoken_summary", lambda tracker, now: "$1.00")
    tools = {tool.name: tool for tool in build_core_tools(jobs, costs, close, tmp_path / "memory.json")}

    assert set(tools) == {
        "dispatch_job",
        "job_status",
        "answer_job",
        "spend_report",
        "end_conversation",
        "remember",
        "forget",
        "idle_do_nothing",
    }
    assert tools["dispatch_job"].schema()["parameters"]["required"] == ["request"]
    assert await tools["dispatch_job"].handler({"request": "Fix issue"}) == {
        "job_id": "job-1",
        "status": "dispatched",
        "message": "Started. I'll tell you when the PR is up.",
    }
    assert await tools["job_status"].handler({}) == {"jobs": [{"id": "job-1", "status": "working"}]}
    assert await tools["answer_job"].handler({"job_id": "job-1", "answer": "Proceed"}) == {
        "job_id": "job-1",
        "status": "working",
    }
    assert (await tools["spend_report"].handler({}))["speech"] == "$1.00"
    assert await tools["dispatch_job"].handler({"request": " "}) == {"error": "request must be a non-empty string"}


@pytest.mark.asyncio
async def test_end_conversation_requests_close_and_returns_legacy_shape(tmp_path) -> None:
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    tool = next(
        tool
        for tool in build_core_tools(SimpleNamespace(), SimpleNamespace(), close, tmp_path / "memory.json")
        if tool.name == "end_conversation"
    )
    assert await tool.handler({}) == {"status": "close_requested", "reason": "user_dismissed"}
    assert closed


@pytest.mark.asyncio
async def test_memory_tool_schemas_and_results_match_pollen(monkeypatch, tmp_path) -> None:
    tools = {
        tool.name: tool
        for tool in build_core_tools(SimpleNamespace(), SimpleNamespace(), lambda: None, tmp_path / "memory.json")
    }
    remember = tools["remember"].schema()
    assert remember["description"] == (
        "Save ONE short fact about the user to long-term memory so it is available in future sessions. "
        "Use this for stable user information they explicitly shared: name, preferences, hobbies, recurring projects, "
        "important people, or plans. Keep each fact atomic and under one sentence. Do not save sensitive data "
        "(passwords, addresses, payment info, health diagnoses) or fleeting details. Use this silently in the "
        'background; acknowledge naturally without saying "I will remember that".'
    )
    assert remember["parameters"] == {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": (
                    "A short, third-person statement about the user, such as "
                    '"Has a dog named Mochi" or "Prefers replies in French". One fact per call.'
                ),
            }
        },
        "required": ["fact"],
    }
    assert tools["forget"].schema()["description"] == (
        "Remove a previously saved fact from long-term memory. Call this when the user asks you to forget something, "
        "or when saved information becomes obsolete. Match by a specific free-text phrase present in the fact."
    )
    assert tools["forget"].schema()["parameters"] == {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A short search phrase that should be present in the fact to remove. Matching is case-insensitive."
                ),
            }
        },
        "required": ["query"],
    }

    remembered = await tools["remember"].handler({"fact": "Has a dog named Mochi"})
    assert remembered["saved"] == "Has a dog named Mochi"
    assert isinstance(remembered["memory_id"], str)
    assert await tools["remember"].handler({"fact": " "}) == {"error": "fact must be a non-empty string"}
    monkeypatch.setattr("ambient_home.tools.core.add_memory_fact", lambda _path, _fact: None)
    assert await tools["remember"].handler({"fact": "valid fact"}) == {
        "error": "fact was empty or invalid; nothing was saved"
    }
    assert await tools["forget"].handler({"query": "missing"}) == {
        "error": 'no memory matched "missing"; nothing was removed'
    }
    assert await tools["forget"].handler({"query": ""}) == {"error": "query must be a non-empty string"}


@pytest.mark.asyncio
async def test_forget_returns_first_match_and_other_matches(tmp_path) -> None:
    tools = {
        tool.name: tool
        for tool in build_core_tools(SimpleNamespace(), SimpleNamespace(), lambda: None, tmp_path / "memory.json")
    }
    await tools["remember"].handler({"fact": "Mochi loves walks"})
    await tools["remember"].handler({"fact": "Mochi likes naps"})

    forgotten = await tools["forget"].handler({"query": "mochi"})

    assert forgotten["removed"] == "Mochi likes naps"
    assert forgotten["other_matches"] == ["Mochi loves walks"]
    assert isinstance(forgotten["memory_id"], str)


@pytest.mark.asyncio
async def test_idle_do_nothing_default_and_explicit_reason(tmp_path) -> None:
    tool = next(
        tool
        for tool in build_core_tools(SimpleNamespace(), SimpleNamespace(), lambda: None, tmp_path / "memory.json")
        if tool.name == "idle_do_nothing"
    )
    assert tool.schema()["description"] == (
        "Use only in response to an idle time update when you intentionally want Reachy to stay still and silent "
        "instead of choosing another idle action."
    )
    assert tool.schema()["parameters"] == {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Optional reason for staying idle during this idle turn."}
        },
        "required": [],
    }
    assert await tool.handler({}) == {"status": "idle", "reason": "idle turn"}
    assert await tool.handler({"reason": "waiting"}) == {"status": "idle", "reason": "waiting"}
