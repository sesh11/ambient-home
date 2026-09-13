"""Focused GPT-Live handler tests."""

import json
import base64
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from openai.types.live.server_event import ServerEvent
from openai.types.live.session_usage import SessionUsage
from openai.types.live.session_resource import SessionResource
from openai.types.live.session_closed_event import SessionClosedEvent
from openai.types.live.session_started_event import SessionStartedEvent
from reachy_mini_conversation_app.tools.core_tools import ToolDependencies
from reachy_mini_conversation_app.tools.tool_constants import ToolState
from reachy_mini_conversation_app.tools.background_tool_manager import ToolNotification

from ambient_home.config import AmbientSettings
from ambient_home.spend_log import SpendLog
from ambient_home.live_handler import GPTLiveHandler
from ambient_home.session_gate import CloseReason, SessionGate


class FakeMovement:
    def __init__(self) -> None:
        self.listening = False
        self.speaking = False

    def set_listening(self, value: bool) -> None:
        self.listening = value

    def set_speaking(self, value: bool) -> None:
        self.speaking = value

    def start(self) -> None:
        pass

    def stop(self, *, reset_to_neutral: bool) -> None:
        pass


class FakeDetector:
    def feed(self, samples: np.ndarray) -> bool:
        return False

    def reset(self) -> None:
        pass


class FakeSession:
    def __init__(self) -> None:
        self.started: list[object] = []
        self.audio: list[str] = []
        self.commentary_text: list[str] = []
        self.closed = 0
        self.input_audio = SimpleNamespace(append=self.input_audio_append)
        self.commentary = SimpleNamespace(append=self.commentary_append)

    async def start(self, *, session: object) -> None:
        self.started.append(session)

    async def input_audio_append(self, *, audio: str) -> None:
        self.audio.append(audio)

    async def commentary_append(self, *, content: str, delegation_id: str | None) -> None:
        self.commentary_text.append(content)

    async def close(self) -> None:
        self.closed += 1


class FakeResponseItem:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    async def create(self, *, item: dict[str, object]) -> None:
        self.items.append(item)


class FakeResponse:
    def __init__(self) -> None:
        self.item = FakeResponseItem()
        self.created = 0

    async def create(self) -> None:
        self.created += 1


class FakeConnection:
    def __init__(self, events: list[ServerEvent]) -> None:
        self.session = FakeSession()
        self.response = FakeResponse()
        self.events = events

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for event in self.events:
            yield event


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeLive:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeConnectionContext:
        return FakeConnectionContext(self.connection)


class FakeClient:
    def __init__(self, connection: FakeConnection) -> None:
        self.live = FakeLive(connection)


def handler(tmp_path) -> GPTLiveHandler:
    movement = FakeMovement()
    robot = SimpleNamespace(
        enable_motors=lambda: None,
        wake_up=lambda: None,
        disable_wobbling=lambda: None,
        goto_sleep=lambda: None,
    )
    deps = ToolDependencies(
        reachy_mini=robot,
        movement_manager=movement,
        camera_enabled=True,
    )
    settings = AmbientSettings(
        openai_api_key="test",
        live_model="gpt-live-1",
        backend_model="gpt-5.6-terra",
        vision_model="gpt-5.6-terra",
        voice="marin",
        wake_word="hey_jarvis",
        wake_threshold=0.5,
        preroll_seconds=2,
        idle_timeout_s=60,
        max_session_s=600,
        daily_budget_s=3600,
        data_dir=tmp_path,
        transcript_finalize_s=0.01,
    )
    gate = SessionGate(
        idle_timeout_s=60,
        max_session_s=600,
        daily_budget_s=3600,
        seconds_used_today=0,
    )
    return GPTLiveHandler(
        deps,
        settings,
        wake_detector=FakeDetector(),
        gate=gate,
        spend_log=SpendLog(tmp_path / "spend.jsonl"),
    )


def session_resource() -> SessionResource:
    return SessionResource(id="session-1", expires_at=1_000_000, model="gpt-live-1", status="active")


def test_session_flushes_preroll_and_records_close(tmp_path) -> None:
    live_handler = handler(tmp_path)
    live_handler._preroll.append(np.array([10, 11], dtype=np.int16))
    connection = FakeConnection(
        [
            SessionStartedEvent(event_id="event-1", session=session_resource(), type="session.started"),
            SessionClosedEvent(
                event_id="event-2",
                reason="close_requested",
                session=session_resource(),
                usage=SessionUsage(seconds=2.0),
                type="session.closed",
            ),
        ]
    )
    live_handler.client = FakeClient(connection)

    asyncio.run(live_handler._run_live_session())

    assert connection.session.started
    assert np.frombuffer(base64.b64decode(connection.session.audio[0]), dtype=np.int16).tolist() == [10, 11]
    assert connection.session.commentary_text
    assert live_handler.gate.state.value == "asleep"
    assert live_handler.spend_log.path.read_text().count("\n") == 1


def test_announcement_is_sent_after_greeting(tmp_path) -> None:
    live_handler = handler(tmp_path)
    asyncio.run(live_handler.announce("Your job is finished."))
    connection = FakeConnection(
        [
            SessionStartedEvent(event_id="event-1", session=session_resource(), type="session.started"),
            SessionClosedEvent(
                event_id="event-2",
                reason="close_requested",
                session=session_resource(),
                usage=SessionUsage(seconds=1.0),
                type="session.closed",
            ),
        ]
    )
    live_handler.client = FakeClient(connection)

    asyncio.run(live_handler._run_live_session())

    assert connection.session.commentary_text[-1] == "While you were away: Your job is finished."


@pytest.mark.asyncio
async def test_request_stop_closes_session_via_gate_watch(tmp_path) -> None:
    live_handler = handler(tmp_path)
    connection = FakeConnection([])
    live_handler.connection = connection
    live_handler.gate.wake()
    await live_handler.request_stop()
    await live_handler._gate_watch()
    assert connection.session.closed == 1


@pytest.mark.asyncio
async def test_transcript_delta_finalizes_user_output(tmp_path) -> None:
    live_handler = handler(tmp_path)
    live_handler.gate.wake()
    await live_handler._handle_event({"type": "session.input_transcript.delta", "delta": "hello"})
    await asyncio.sleep(0.04)

    outputs = []
    while not live_handler.output_queue.empty():
        outputs.append(live_handler.output_queue.get_nowait())
    assert any(output.args[0]["role"] == "user" for output in outputs)
    assert live_handler.gate.session_elapsed_s >= 0


@pytest.mark.asyncio
async def test_assistant_deltas_do_not_delay_user_transcript(tmp_path) -> None:
    live_handler = handler(tmp_path)
    live_handler.gate.wake()
    await live_handler._handle_event({"type": "session.input_transcript.delta", "delta": "hello"})
    await asyncio.sleep(0.005)
    await live_handler._handle_event({"type": "session.output_transcript.delta", "delta": "answer"})
    await asyncio.sleep(0.04)

    outputs = []
    while not live_handler.output_queue.empty():
        outputs.append(live_handler.output_queue.get_nowait())
    assert any(output.args[0]["role"] == "user" for output in outputs)
    assert any(output.args[0]["role"] == "assistant" for output in outputs)


@pytest.mark.asyncio
async def test_nested_function_call_and_tool_result(tmp_path, monkeypatch) -> None:
    live_handler = handler(tmp_path)
    connection = FakeConnection([])
    live_handler.connection = connection
    started: list[tuple[str, str]] = []

    async def start_tool(manager, *, call_id, tool_call_routine, is_idle_tool_call):
        started.append((call_id, tool_call_routine.tool_name))

    monkeypatch.setattr(type(live_handler.tool_manager), "start_tool", start_tool)
    await live_handler._handle_event(
        {
            "type": "response.event",
            "event": {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "call_id": "call-1", "name": "look", "arguments": "{}"},
            },
        }
    )
    await live_handler._handle_tool_result(
        ToolNotification(
            id="call-1",
            tool_name="look",
            is_idle_tool_call=False,
            status=ToolState.COMPLETED,
            result={"description": "a robot"},
        )
    )

    assert started == [("call-1", "look")]
    assert json.loads(str(connection.response.item.items[0]["output"])) == {"description": "a robot"}
    assert connection.response.created == 1


@pytest.mark.asyncio
async def test_gate_watch_closes_due_session(tmp_path) -> None:
    live_handler = handler(tmp_path)
    live_handler.gate.wake()
    connection = FakeConnection([])
    live_handler.connection = connection
    live_handler.gate.request_close(CloseReason.USER_DISMISSED)
    await live_handler._gate_watch()

    assert connection.session.closed == 1


def test_session_config_contains_live_audio_and_delegation(tmp_path) -> None:
    config = handler(tmp_path)._session_config()
    assert config["model"] == "gpt-live-1"
    assert config["audio"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert config["audio"]["output"]["voice"] == "marin"
    assert config["delegation"]["type"] == "responses"


def test_output_audio_event_is_queued(tmp_path) -> None:
    live_handler = handler(tmp_path)
    asyncio.run(live_handler._handle_event({"type": "session.output_audio.delta", "delta": "AQACAQ=="}))
    rate, samples = asyncio.run(live_handler.output_queue.get())
    assert rate == 16000
    assert samples.dtype == np.int16
    assert samples.shape == (1, 2)
