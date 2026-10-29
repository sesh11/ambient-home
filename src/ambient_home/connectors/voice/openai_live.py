"""OpenAI GPT-Live connector."""

import json
import base64
import asyncio
import logging
from typing import Any
from contextlib import asynccontextmanager
from collections.abc import Callable, AsyncIterator

from garage_sdk import emit
from websockets.exceptions import ConnectionClosed

from ambient_home.connectors.voice.base import (
    SAMPLE_RATE,
    Usage,
    Closed,
    AudioOut,
    ToolCall,
    VoiceError,
    VoiceEvent,
    VoiceSession,
    TranscriptDelta,
    MalformedToolCall,
    VoiceSessionConfig,
    VoiceConnectionLost,
    VoiceStartupTimeout,
)


logger = logging.getLogger(__name__)


class OpenAILiveVoiceModel:
    """Lazily open an OpenAI realtime speech session."""

    name = "openai-gpt-live"
    voices: tuple[str, ...] = (
        "alloy",
        "ash",
        "ballad",
        "beacon",
        "bossa",
        "cedar",
        "cinder",
        "coral",
        "delta",
        "echo",
        "gleam",
        "marin",
        "meridian",
        "quartz",
        "ripple",
        "sage",
        "shimmer",
        "stone",
        "tempo",
        "verse",
        "vesper",
        "willow",
    )

    def __init__(
        self,
        api_key: str | None,
        live_model: str,
        backend_model: str,
        *,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        """Configure the lazy OpenAI client."""
        self.api_key = api_key
        self.live_model = live_model
        self.backend_model = backend_model
        self._client_factory = client_factory
        self._client: Any = None

    def available(self) -> bool:
        """Return whether an API key is configured."""
        return bool(self.api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            factory = self._client_factory
            if factory is None:
                from openai import AsyncOpenAI

                factory = AsyncOpenAI
            self._client = factory(api_key=self.api_key)
        return self._client

    @asynccontextmanager
    async def open(self, config: VoiceSessionConfig) -> AsyncIterator[VoiceSession]:
        """Open a Live session and wait for provider startup."""
        client = self._get_client()
        try:
            async with client.live.connect() as connection:
                await connection.session.start(session=self._session_config(config))
                session = _OpenAISession(connection)
                try:
                    async with asyncio.timeout(10.0):
                        async for event in connection:
                            decoded = _decode_event(event.model_dump())
                            if decoded is not None:
                                session._pending.append(decoded)
                            if event.type == "session.started":
                                emit("live.session_started", "live")
                                break
                        else:
                            raise VoiceStartupTimeout("Live stream ended before session.started")
                except TimeoutError as exc:
                    logger.error("Timed out waiting for session.started")
                    emit("live.startup_timeout", "live", "error")
                    raise VoiceStartupTimeout("Timed out waiting for session.started") from exc
                except ConnectionClosed as exc:
                    raise VoiceConnectionLost(str(exc)) from exc
                if not any(isinstance(event, Closed) for event in session._pending):
                    yield session
                else:
                    raise VoiceStartupTimeout("Live stream ended before session.started")
        except ConnectionClosed as exc:
            raise VoiceConnectionLost(str(exc)) from exc

    def _session_config(self, config: VoiceSessionConfig) -> dict[str, object]:
        tools = [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
                "strict": False,
            }
            for tool in config.tools
        ]
        return {
            "model": self.live_model,
            "audio": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}, "output": {"voice": config.voice}},
            "instructions": config.instructions,
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": self.backend_model,
                    "instructions": "Execute robot tools when asked and return concise text results.",
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                },
            },
        }


class _OpenAISession:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._pending: list[VoiceEvent] = []

    async def send_audio(self, pcm16: bytes) -> None:
        try:
            await self.connection.session.input_audio.append(audio=base64.b64encode(pcm16).decode("ascii"))
        except ConnectionClosed as exc:
            raise VoiceConnectionLost(str(exc)) from exc

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while self._pending:
            yield self._pending.pop(0)
        try:
            async for event in self.connection:
                decoded = _decode_event(event.model_dump())
                if decoded is not None:
                    yield decoded
        except ConnectionClosed as exc:
            raise VoiceConnectionLost(str(exc)) from exc

    async def send_tool_result(self, call_id: str, result: dict[str, object]) -> None:
        try:
            await self.connection.response.item.create(
                item={"type": "function_call_output", "call_id": call_id, "output": json.dumps(result)}
            )
        except ConnectionClosed as exc:
            raise VoiceConnectionLost(str(exc)) from exc

    async def request_response(self) -> None:
        try:
            await self.connection.response.create()
        except ConnectionClosed as exc:
            raise VoiceConnectionLost(str(exc)) from exc

    async def say(self, text: str) -> None:
        try:
            await self.connection.session.commentary.append(content=text, delegation_id=None)
        except ConnectionClosed as exc:
            raise VoiceConnectionLost(str(exc)) from exc

    async def close(self) -> None:
        try:
            await self.connection.session.close()
        except ConnectionClosed:
            return


def _decode_event(event: dict[str, object]) -> VoiceEvent | None:
    event_type = event.get("type")
    if event_type == "session.output_audio.delta":
        delta = event.get("delta")
        return AudioOut(base64.b64decode(delta)) if isinstance(delta, str) else None
    if event_type == "session.input_transcript.delta":
        return TranscriptDelta("user", str(event.get("delta", "")))
    if event_type == "session.output_transcript.delta":
        return TranscriptDelta("assistant", str(event.get("delta", "")))
    if event_type == "response.event":
        nested = event.get("event")
        if isinstance(nested, dict):
            nested_type = nested.get("type")
            if nested_type == "response.output_item.done":
                item = nested.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
                    if isinstance(call_id, str) and isinstance(name, str) and isinstance(arguments, str):
                        return ToolCall(call_id, name, arguments)
                    logger.error("Malformed Live function call")
                    return MalformedToolCall(name if isinstance(name, str) else None)
            if nested_type in {"response.completed", "response.failed", "response.incomplete"}:
                logger.info("Responses event: %s", nested_type)
        return None
    if event_type == "session.usage.updated":
        usage = event.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("seconds"), (int, float)):
            return Usage(float(usage["seconds"]))
        return None
    if event_type == "session.closed":
        usage = event.get("usage")
        seconds = usage.get("seconds") if isinstance(usage, dict) else None
        return Closed(
            str(event.get("reason") or "server_closed"),
            float(seconds) if isinstance(seconds, (int, float)) else None,
        )
    if event_type == "error":
        payload = event.get("error")
        if isinstance(payload, dict):
            code = payload.get("code")
            error_type = payload.get("type")
            message = payload.get("message")
            safe_code = code if isinstance(code, (str, int, float, bool)) else None
            safe_type = error_type if isinstance(error_type, str) else None
            safe_message = message if isinstance(message, str) else None
        else:
            safe_code = safe_type = safe_message = None
        logger.error("Live API error code=%s type=%s", safe_code, safe_type)
        return VoiceError(safe_code, safe_type, safe_message)
    if event_type in {"session.started", "session.delegation.created"}:
        return None
    logger.debug("Ignoring Live event %s", event_type)
    return None
