"""Provider-neutral conversation lifecycle and dispatch tests."""

import json
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace

import numpy as np
import pytest
from garage_sdk import EventLog, configure

import ambient_home.conversation as conversation_module
from ambient_device_sdk import ToolSpec, SessionPhase, SessionState
from ambient_home.config import settings_from_env
from ambient_home.persona import load_profile
from ambient_home.spend_log import SpendLog
from ambient_home.conversation import Conversation
from ambient_home.session_gate import GateState, CloseReason, SessionGate
from ambient_home.connectors.voice.base import (
    Usage,
    Closed,
    AudioOut,
    ToolCall,
    TranscriptDelta,
    MalformedToolCall,
    VoiceConnectionLost,
    VoiceStartupTimeout,
)


class Session:
    def __init__(
        self,
        events=(),
        *,
        wait_for_close: bool = False,
        delay_after_event: float = 0,
        events_error: Exception | None = None,
    ) -> None:
        self.events_script = list(events)
        self.wait_for_close = wait_for_close
        self.delay_after_event = delay_after_event
        self.events_error = events_error
        self.spoken: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self.tool_results: list[tuple[str, dict[str, object]]] = []
        self.responses = 0
        self.close_count = 0
        self.events_started = asyncio.Event()
        self.closed = asyncio.Event()

    async def send_audio(self, pcm16: bytes) -> None:
        self.calls.append(("audio", pcm16))

    async def events(self):
        self.events_started.set()
        for event in self.events_script:
            yield event
            await asyncio.sleep(self.delay_after_event)
        if self.wait_for_close:
            await self.closed.wait()
            yield Closed(CloseReason.USER_DISMISSED.value, None)
        if self.events_error is not None:
            raise self.events_error

    async def send_tool_result(self, call_id: str, result: dict[str, object]) -> None:
        self.tool_results.append((call_id, result))

    async def request_response(self) -> None:
        self.responses += 1

    async def say(self, text: str) -> None:
        self.spoken.append(text)
        self.calls.append(("say", text))

    async def close(self) -> None:
        self.close_count += 1
        self.closed.set()


class Voice:
    name = "fake"
    voices = ("ballad", "alloy")

    def __init__(
        self,
        session: Session | None = None,
        *,
        available: bool = True,
        open_error: Exception | None = None,
        open_gate: asyncio.Event | None = None,
    ) -> None:
        self.session = session or Session([Closed(CloseReason.SERVER_CLOSED.value, None)])
        self._available = available
        self.open_error = open_error
        self.open_gate = open_gate
        self.opens = 0
        self.config = None

    def available(self) -> bool:
        return self._available

    @asynccontextmanager
    async def open(self, config):
        self.opens += 1
        self.config = config
        if self.open_gate is not None:
            await self.open_gate.wait()
        if self.open_error is not None:
            raise self.open_error
        yield self.session


class Hub:
    def __init__(self, preroll: np.ndarray | None = None) -> None:
        self.owner: str | None = "device-1"
        self.notifications: list[SessionState] = []
        self.releases: list[str] = []
        self.preroll_samples = preroll if preroll is not None else np.empty(0, dtype=np.int16)
        self.played: list[bytes] = []
        self.owner_tool_list: list[ToolSpec] = []

    def owner_tools(self) -> list[ToolSpec]:
        return self.owner_tool_list

    def preroll(self, _device_id: str) -> np.ndarray:
        samples = self.preroll_samples
        self.preroll_samples = np.empty(0, dtype=np.int16)
        return samples

    async def play(self, pcm16_16k_mono: bytes) -> None:
        self.played.append(pcm16_16k_mono)

    async def stop_playback(self) -> None:
        return

    async def notify(self, state: SessionState) -> None:
        self.notifications.append(state)

    def release(self, device_id: str) -> None:
        self.releases.append(device_id)
        if self.owner == device_id:
            self.owner = None


def _make_conversation(
    tmp_path,
    *,
    session: Session | None = None,
    available: bool = True,
    open_error: Exception | None = None,
    open_gate: asyncio.Event | None = None,
    gate: SessionGate | None = None,
    hub: Hub | None = None,
    on_transcript=None,
    finalize_s: float = 0.01,
) -> tuple[Conversation, Voice, Hub]:
    settings = replace(settings_from_env(), data_dir=tmp_path, transcript_finalize_s=finalize_s)
    voice = Voice(session, available=available, open_error=open_error, open_gate=open_gate)
    actual_hub = hub or Hub()
    conversation = Conversation(
        settings,
        voice_model=voice,
        gate=gate or SessionGate(idle_timeout_s=60, max_session_s=600, daily_budget_s=3600, seconds_used_today=0),
        spend_log=SpendLog(tmp_path / "spend.jsonl"),
        profile=load_profile(),
        core_tools=[],
        hub=actual_hub,
        on_transcript=on_transcript,
    )
    return conversation, voice, actual_hub


def _record_events(monkeypatch) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(conversation_module, "emit", lambda *args, **data: events.append((args, data)))
    return events


async def _wait_for(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


async def _stop_run(conversation: Conversation, task: asyncio.Task[None]) -> None:
    await conversation.shutdown()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_preroll_is_flushed_before_greeting_and_spend_is_recorded(tmp_path) -> None:
    samples = np.array([10, 11], dtype=np.int16)
    session = Session([Closed(CloseReason.SERVER_CLOSED.value, None)])
    conversation, voice, hub = _make_conversation(tmp_path, session=session, hub=Hub(samples))

    await conversation._run_session("device-1")

    assert session.calls[0] == ("audio", samples.tobytes())
    assert session.calls[1] == ("say", load_profile().greeting)
    assert hub.notifications[0].phase is SessionPhase.WAKE
    assert hub.notifications[-1].phase is SessionPhase.SLEEP
    assert hub.notifications[-1].reason == CloseReason.SERVER_CLOSED.value
    assert conversation.gate.state is GateState.ASLEEP
    assert (tmp_path / "spend.jsonl").read_text().count("\n") == 1
    assert voice.config.voice == "ballad"


@pytest.mark.asyncio
async def test_announcement_queued_while_asleep_follows_greeting(tmp_path) -> None:
    session = Session([Closed(CloseReason.SERVER_CLOSED.value, None)])
    conversation, _, _ = _make_conversation(tmp_path, session=session)
    await conversation.announce("Your job is finished.")

    await conversation._run_session("device-1")

    assert session.spoken == [load_profile().greeting, "While you were away: Your job is finished."]


@pytest.mark.asyncio
async def test_request_stop_closes_session_when_gate_watch_sees_due_reason(tmp_path) -> None:
    session = Session(wait_for_close=True)
    conversation, _, hub = _make_conversation(tmp_path, session=session)
    conversation.request_wake("device-1")
    task = asyncio.create_task(conversation.run())
    await asyncio.wait_for(session.events_started.wait(), timeout=1)

    await conversation.request_stop()
    await asyncio.wait_for(session.closed.wait(), timeout=1)
    await _stop_run(conversation, task)

    assert session.close_count == 1
    assert conversation.gate.last_close_reason is CloseReason.USER_DISMISSED
    assert hub.owner is None


@pytest.mark.asyncio
async def test_user_transcript_debounce_is_not_delayed_by_assistant_deltas(tmp_path) -> None:
    transcript: list[tuple[str, str]] = []
    conversation, _, _ = _make_conversation(
        tmp_path,
        on_transcript=lambda role, text: transcript.append((role, text)),
        finalize_s=0.04,
    )
    conversation.gate.wake()
    conversation._session_id = "session"

    await conversation._handle_event(TranscriptDelta("user", "user text"))
    await asyncio.sleep(0.01)
    await conversation._handle_event(TranscriptDelta("assistant", "assistant text"))
    await asyncio.sleep(0.035)

    assert transcript == [("user", "user text")]
    await asyncio.sleep(0.02)
    assert transcript == [("user", "user text"), ("assistant", "assistant text")]


@pytest.mark.asyncio
async def test_tool_results_request_one_response_after_all_in_flight_tools(monkeypatch, tmp_path) -> None:
    _record_events(monkeypatch)
    session = Session()
    conversation, _, _ = _make_conversation(tmp_path, session=session)
    conversation._session = session
    conversation._active_tools = {
        "one": ToolSpec("one", "one", {"type": "object"}, lambda _: asyncio.sleep(0, result={"ok": 1})),
    }

    await conversation._handle_event(ToolCall("call-1", "one", "{}"))
    await asyncio.gather(*tuple(conversation._tool_tasks))
    assert session.tool_results == [("call-1", {"ok": 1})]
    assert session.responses == 1

    started: list[str] = []
    release = asyncio.Event()

    async def parallel_handler(arguments: dict[str, object]) -> dict[str, object]:
        started.append(str(arguments["value"]))
        if len(started) == 2:
            release.set()
        await release.wait()
        return {"value": arguments["value"]}

    session.tool_results.clear()
    session.responses = 0
    conversation._active_tools = {
        "parallel": ToolSpec("parallel", "parallel", {"type": "object"}, parallel_handler),
    }
    await conversation._handle_event(ToolCall("call-2", "parallel", '{"value":"a"}'))
    await conversation._handle_event(ToolCall("call-3", "parallel", '{"value":"b"}'))
    await asyncio.gather(*tuple(conversation._tool_tasks))
    assert {call_id for call_id, _ in session.tool_results} == {"call-2", "call-3"}
    assert session.responses == 1


@pytest.mark.asyncio
async def test_unknown_and_failing_tools_return_error_results_and_failure_events(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    session = Session()
    conversation, _, _ = _make_conversation(tmp_path, session=session)
    conversation._session = session

    async def fail(_arguments: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("tool failure")

    conversation._active_tools = {"fail": ToolSpec("fail", "fail", {"type": "object"}, fail)}
    await conversation._handle_event(ToolCall("unknown-id", "unknown", "{}"))
    await conversation._handle_event(ToolCall("fail-id", "fail", "{}"))
    await asyncio.gather(*tuple(conversation._tool_tasks))

    assert session.tool_results == [
        ("unknown-id", {"error": "unknown tool: unknown"}),
        ("fail-id", {"error": "RuntimeError: tool failure"}),
    ]
    failures = [event for event in events if event[0][0] == "tool.completed"]
    assert [event[1]["ok"] for event in failures] == [False, False]
    assert [event[1]["error"] for event in failures] == ["unknown tool: unknown", "RuntimeError: tool failure"]


def test_malformed_tool_call_emits_compatibility_event(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    conversation, _, _ = _make_conversation(tmp_path)

    asyncio.run(conversation._handle_event(MalformedToolCall("bad_tool")))

    assert events[0] == (("live.malformed_tool_call", "live", "error"), {"tool": "bad_tool"})


@pytest.mark.asyncio
async def test_budget_exhaustion_emits_once_and_releases_owner(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    gate = SessionGate(idle_timeout_s=60, max_session_s=600, daily_budget_s=10, seconds_used_today=10)
    conversation, _, hub = _make_conversation(tmp_path, gate=gate)
    task = asyncio.create_task(conversation.run())
    assert conversation.request_wake("device-1")
    await _wait_for(lambda: len(hub.releases) == 1)
    assert conversation.request_wake("device-1")
    await _wait_for(lambda: len(hub.releases) == 2)
    assert len([event for event in events if event[0][0] == "live.budget_exhausted"]) == 1
    await _stop_run(conversation, task)


@pytest.mark.asyncio
async def test_missing_api_key_emits_and_releases_owner(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    conversation, voice, hub = _make_conversation(tmp_path, available=False)
    task = asyncio.create_task(conversation.run())
    assert conversation.request_wake("device-1")
    await _wait_for(lambda: bool(hub.releases))

    assert voice.opens == 0
    assert any(event[0][0] == "live.missing_api_key" for event in events)
    await _stop_run(conversation, task)


@pytest.mark.asyncio
async def test_connection_loss_closes_gate_as_error_and_releases_owner(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    session = Session(events_error=VoiceConnectionLost("connection lost"))
    conversation, _, hub = _make_conversation(tmp_path, session=session)
    task = asyncio.create_task(conversation.run())
    assert conversation.request_wake("device-1")
    await _wait_for(lambda: bool(events) and bool(hub.releases))

    assert conversation.gate.state is GateState.ASLEEP
    assert conversation.gate.last_close_reason is CloseReason.ERROR
    assert any(event[0][0] == "live.connection_dropped" for event in events)
    assert hub.owner is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_generic_session_exception_emits_failure_and_releases_owner(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    session = Session(events_error=RuntimeError("session failure"))
    conversation, _, hub = _make_conversation(tmp_path, session=session)
    task = asyncio.create_task(conversation.run())
    assert conversation.request_wake("device-1")
    await _wait_for(lambda: bool(events) and bool(hub.releases))

    assert any(event[0][0] == "live.session_failed" for event in events)
    assert hub.owner is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_startup_timeout_releases_owner_without_failure_event(monkeypatch, tmp_path) -> None:
    events = _record_events(monkeypatch)
    conversation, _, hub = _make_conversation(tmp_path, open_error=VoiceStartupTimeout("not started"))
    task = asyncio.create_task(conversation.run())
    assert conversation.request_wake("device-1")
    await _wait_for(lambda: bool(hub.releases))

    assert events == []
    assert conversation.gate.state is GateState.ASLEEP
    await _stop_run(conversation, task)


@pytest.mark.asyncio
async def test_device_lost_during_session_closes_with_error(tmp_path) -> None:
    session = Session(wait_for_close=True)
    conversation, _, hub = _make_conversation(tmp_path, session=session)
    conversation.request_wake("device-1")
    task = asyncio.create_task(conversation.run())
    await asyncio.wait_for(session.events_started.wait(), timeout=1)

    conversation.device_lost("device-1")
    await asyncio.wait_for(session.closed.wait(), timeout=1)
    await _stop_run(conversation, task)

    assert conversation.gate.last_close_reason is CloseReason.ERROR
    assert hub.owner is None


@pytest.mark.asyncio
async def test_session_state_order_includes_speaking_idle_then_sleep(tmp_path) -> None:
    session = Session([AudioOut(b"\x00\x00"), Closed(CloseReason.SERVER_CLOSED.value, None)], delay_after_event=0.42)
    conversation, _, hub = _make_conversation(tmp_path, session=session)

    await conversation._run_session("device-1")

    assert [state.phase for state in hub.notifications] == [
        SessionPhase.WAKE,
        SessionPhase.SPEAKING,
        SessionPhase.IDLE,
        SessionPhase.SLEEP,
    ]
    assert hub.notifications[-1].reason == CloseReason.SERVER_CLOSED.value


@pytest.mark.asyncio
async def test_usage_event_is_saved_in_spend_record(tmp_path) -> None:
    session = Session([Usage(12.5), Closed(CloseReason.SERVER_CLOSED.value, None)])
    conversation, _, _ = _make_conversation(tmp_path, session=session)

    await conversation._run_session("device-1")

    record = json.loads((tmp_path / "spend.jsonl").read_text().splitlines()[0])
    assert record["reported_usage_seconds"] == 12.5


@pytest.mark.asyncio
async def test_transcript_sentinel_is_absent_from_logs_and_event_jsonl(tmp_path, caplog) -> None:
    sentinel = "private transcript sentinel"
    transcript: list[tuple[str, str]] = []
    session = Session(
        [TranscriptDelta("user", sentinel), Closed(CloseReason.SERVER_CLOSED.value, None)],
        delay_after_event=0.03,
    )
    conversation, _, _ = _make_conversation(
        tmp_path,
        session=session,
        on_transcript=lambda role, text: transcript.append((role, text)),
        finalize_s=0.005,
    )
    log = EventLog(tmp_path / "events")
    caplog.set_level(logging.DEBUG, logger="ambient_home.conversation")
    configure(log)
    try:
        await conversation._run_session("device-1")
    finally:
        configure(None)

    event_text = "".join(path.read_text() for path in (tmp_path / "events").glob("*.jsonl"))
    assert transcript == [("user", sentinel)]
    assert sentinel not in caplog.text
    assert sentinel not in event_text


@pytest.mark.asyncio
async def test_change_voice_and_status_remain_available(tmp_path) -> None:
    conversation, _, _ = _make_conversation(tmp_path)

    assert await conversation.get_available_voices() == ["ballad", "alloy"]
    assert await conversation.change_voice("alloy") == "Voice changed. Takes effect on the next wake."
    assert conversation.get_current_voice() == "alloy"
    assert conversation.status().state is GateState.ASLEEP
    assert conversation.status().last_close_reason is None


def test_core_tools_precede_owner_tools_and_unknown_profile_names_are_ignored(tmp_path) -> None:
    async def handler(_arguments: dict[str, object]) -> dict[str, object]:
        return {}

    core = ToolSpec("same", "core", {"type": "object"}, handler)
    owner = ToolSpec("same", "device", {"type": "object"}, handler)
    conversation, _, _ = _make_conversation(tmp_path)
    conversation.profile = replace(load_profile(), tools=("same", "missing"))
    conversation.core_tools = (core,)

    assert conversation._select_tools([owner], "device-1") == [core]
