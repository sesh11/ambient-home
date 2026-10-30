"""OpenAI Live adapter contract tests without provider calls."""

import base64
import logging

import pytest
from websockets.exceptions import ConnectionClosed

from ambient_home.connectors.voice.base import (
    AudioOut,
    ToolCall,
    VoiceError,
    TranscriptDelta,
    VoiceSessionConfig,
    VoiceConnectionLost,
    VoiceStartupTimeout,
)
from ambient_home.connectors.voice.openai_live import OpenAILiveVoiceModel, _decode_event, _OpenAISession


def test_client_is_lazy_and_session_configuration_matches_live_contract() -> None:
    created = []

    class Client:
        pass

    model = OpenAILiveVoiceModel(
        "secret", "live-model", "backend-model", client_factory=lambda **kw: created.append(kw) or Client()
    )
    assert model.available()
    assert created == []
    schema = {"name": "end_conversation", "description": "Finish", "parameters": {"type": "object"}}
    config = VoiceSessionConfig("instructions", "ballad", [schema])
    payload = model._session_config(config)
    assert payload == {
        "model": "live-model",
        "audio": {"format": {"type": "audio/pcm", "rate": 16000}, "output": {"voice": "ballad"}},
        "instructions": "instructions",
        "delegation": {
            "type": "responses",
            "responses": {
                "model": "backend-model",
                "instructions": "Execute robot tools when asked and return concise text results.",
                "tools": [{"type": "function", **schema, "strict": False}],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
        },
    }
    model._get_client()
    assert created == [{"api_key": "secret"}]


def test_live_event_mapping_is_provider_neutral() -> None:
    assert _decode_event(
        {"type": "session.output_audio.delta", "delta": base64.b64encode(b"pcm").decode()}
    ) == AudioOut(b"pcm")
    assert _decode_event({"type": "session.input_transcript.delta", "delta": "hello"}) == TranscriptDelta(
        "user", "hello"
    )
    assert _decode_event({"type": "session.output_transcript.delta", "delta": "hello"}) == TranscriptDelta(
        "assistant", "hello"
    )
    event = {
        "type": "response.event",
        "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "call-1", "name": "end_conversation", "arguments": "{}"},
        },
    }
    assert _decode_event(event) == ToolCall("call-1", "end_conversation", "{}")
    error = _decode_event({"type": "error", "error": {"code": "bad", "type": "invalid_request", "message": "No"}})
    assert error == VoiceError("bad", "invalid_request", "No")


@pytest.mark.asyncio
async def test_open_is_lazy_and_ends_if_started_event_never_arrives() -> None:
    class FakeEvent:
        type = "session.closed"

        def model_dump(self):
            return {"type": self.type, "reason": "server_closed"}

    class Session:
        async def start(self, *, session):
            return

    class Connection:
        session = Session()

        def __aiter__(self):
            async def events():
                yield FakeEvent()

            return events()

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return None

    class Client:
        class live:
            @staticmethod
            def connect():
                return ConnectionContext()

    created = []
    model = OpenAILiveVoiceModel(
        "secret", "live", "backend", client_factory=lambda **kwargs: created.append(kwargs) or Client()
    )
    assert created == []
    with pytest.raises(VoiceStartupTimeout, match="before session.started"):
        async with model.open(VoiceSessionConfig("instructions", "ballad", [])):
            pytest.fail("session should not start")
    assert created == [{"api_key": "secret"}]


class WireInputAudio:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def append(self, **data: object) -> None:
        self.calls.append(data)


class WireCommentary:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def append(self, **data: object) -> None:
        self.calls.append(data)


class WireSession:
    def __init__(self) -> None:
        self.input_audio = WireInputAudio()
        self.commentary = WireCommentary()
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class WireResponseItem:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **data: object) -> None:
        self.calls.append(data)


class WireResponse:
    def __init__(self) -> None:
        self.item = WireResponseItem()
        self.created = 0

    async def create(self) -> None:
        self.created += 1


class WireConnection:
    def __init__(self) -> None:
        self.session = WireSession()
        self.response = WireResponse()


@pytest.mark.asyncio
async def test_session_methods_emit_exact_provider_wire_calls() -> None:
    connection = WireConnection()
    session = _OpenAISession(connection)

    await session.send_audio(b"\x01\x02")
    await session.send_tool_result("call-1", {"ok": True})
    await session.request_response()
    await session.say("Hello")

    assert connection.session.input_audio.calls == [{"audio": base64.b64encode(b"\x01\x02").decode("ascii")}]
    assert connection.response.item.calls == [
        {"item": {"type": "function_call_output", "call_id": "call-1", "output": '{"ok": true}'}}
    ]
    assert connection.response.created == 1
    assert connection.session.commentary.calls == [{"content": "Hello", "delegation_id": None}]


@pytest.mark.asyncio
async def test_websocket_connection_closed_maps_for_audio_send_and_events() -> None:
    connection = WireConnection()
    closed = ConnectionClosed(None, None)

    async def fail_append(**_data: object) -> None:
        raise closed

    connection.session.input_audio.append = fail_append
    session = _OpenAISession(connection)
    with pytest.raises(VoiceConnectionLost):
        await session.send_audio(b"pcm")

    class BrokenConnection(WireConnection):
        def __aiter__(self):
            async def broken_events():
                raise ConnectionClosed(None, None)
                yield None

            return broken_events()

    with pytest.raises(VoiceConnectionLost):
        async for _event in _OpenAISession(BrokenConnection()).events():
            pass


def test_error_event_maps_without_logging_user_content(caplog) -> None:
    sentinel = "private error transcript sentinel"
    caplog.set_level(logging.ERROR, logger="ambient_home.connectors.voice.openai_live")

    event = _decode_event(
        {"type": "error", "error": {"code": "bad_request", "type": "invalid_request", "message": sentinel}}
    )

    assert event == VoiceError("bad_request", "invalid_request", sentinel)
    assert sentinel not in caplog.text
