"""Core tool contracts and result semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ambient_home.tools.core import build_core_tools


@pytest.mark.asyncio
async def test_tool_names_schemas_and_results(monkeypatch) -> None:
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
    tools = {tool.name: tool for tool in build_core_tools(jobs, costs, close)}

    assert set(tools) == {"dispatch_job", "job_status", "answer_job", "spend_report", "end_conversation"}
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
async def test_end_conversation_requests_close_and_returns_legacy_shape() -> None:
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    tool = next(
        tool
        for tool in build_core_tools(SimpleNamespace(), SimpleNamespace(), close)
        if tool.name == "end_conversation"
    )
    assert await tool.handler({}) == {"status": "close_requested", "reason": "user_dismissed"}
    assert closed
