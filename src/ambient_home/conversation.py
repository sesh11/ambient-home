"""Wake-gated conversation orchestration independent of device and voice providers."""

import json
import uuid
import asyncio
import logging
from typing import Protocol
from datetime import datetime, timezone
from dataclasses import dataclass
from collections.abc import Callable, Sequence

import numpy as np
from garage_sdk import emit
from numpy.typing import NDArray

from ambient_device_sdk import ToolSpec, SessionState
from ambient_home.audio import to_bytes
from ambient_home.config import AmbientSettings
from ambient_home.persona import DEFAULT_GREETING_PROMPT, Profile
from ambient_home.spend_log import SpendLog, SpendRecord
from ambient_home.session_gate import GateState, CloseReason, SessionGate
from ambient_home.connectors.voice.base import (
    Usage,
    Closed,
    AudioOut,
    ToolCall,
    VoiceError,
    VoiceModel,
    VoiceSession,
    TranscriptDelta,
    MalformedToolCall,
    VoiceSessionConfig,
    VoiceConnectionLost,
    VoiceStartupTimeout,
)


logger = logging.getLogger(__name__)
_ADDENDUM = (
    "You are woken by 'hey jarvis'. Keep answers short. When the user says "
    "that's all, go to sleep, thanks, that's it, or similar, delegate end_conversation."
)


class ConversationHub(Protocol):
    """Device gateway operations required by a conversation."""

    owner: str | None

    def owner_tools(self) -> list[ToolSpec]:
        """Return tools supplied by the owning device."""
        ...

    def preroll(self, device_id: str) -> NDArray[np.int16]:
        """Drain buffered wake-word audio."""
        ...

    async def play(self, pcm16_16k_mono: bytes) -> None:
        """Play audio through the owning device."""
        ...

    async def stop_playback(self) -> None:
        """Stop audio playback."""
        ...

    async def notify(self, state: SessionState) -> None:
        """Notify the owner device of session state."""
        ...

    def release(self, device_id: str) -> None:
        """Release ownership for one device."""
        ...


@dataclass(frozen=True)
class LiveStatus:
    """Read-only conversation and budget status."""

    state: GateState
    session_elapsed_s: float
    seconds_used_today: float
    seconds_remaining_today: float
    last_close_reason: CloseReason | None


class Conversation:
    """Own the single active conversation and its wake/session lifecycle."""

    def __init__(
        self,
        settings: AmbientSettings,
        *,
        voice_model: VoiceModel,
        gate: SessionGate,
        spend_log: SpendLog,
        profile: Profile,
        core_tools: Sequence[ToolSpec],
        hub: ConversationHub,
        startup_voice: str | None = None,
        on_transcript: Callable[[str, str], None] | None = None,
    ) -> None:
        """Configure providers, state, tools, and the owning gateway."""
        self.settings = settings
        self.voice_model = voice_model
        self.gate = gate
        self.spend_log = spend_log
        self.profile = profile
        self.core_tools = tuple(core_tools)
        self.hub = hub
        self.on_transcript = on_transcript
        self._voice_override = startup_voice if startup_voice in voice_model.voices else None
        self._wake_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._pending_device: str | None = None
        self._session: VoiceSession | None = None
        self._session_id: str | None = None
        self._session_started_at: datetime | None = None
        self._last_usage_seconds: float | None = None
        self._last_server_reason: str | None = None
        self._pending_announcements: list[str] = []
        self._budget_warning_logged = False
        self._user_transcript = ""
        self._assistant_transcript = ""
        self._user_transcript_task: asyncio.Task[None] | None = None
        self._assistant_transcript_task: asyncio.Task[None] | None = None
        self._speaking_task: asyncio.Task[None] | None = None
        self._tool_tasks: set[asyncio.Task[None]] = set()
        self._in_flight_tool_calls: set[str] = set()
        self._active_tools: dict[str, ToolSpec] = {}
        self._listening = False
        self._speaking = False

    def request_wake(self, device_id: str) -> bool:
        """Claim a wake request only when no wake or session is pending."""
        if self._pending_device is not None or self._session is not None or self._wake_event.is_set():
            return False
        self._pending_device = device_id
        self._wake_event.set()
        return True

    def device_lost(self, device_id: str) -> None:
        """Close the conversation if its owner device is lost."""
        if self._pending_device == device_id:
            self._pending_device = None
            self._wake_event.clear()
            self.hub.release(device_id)
            return
        if self._session is not None and self.hub.owner == device_id:
            self.gate.request_close(CloseReason.ERROR)

    def is_streaming(self) -> bool:
        """Return whether a voice session is open."""
        return self._session is not None

    async def send_audio(self, mono: NDArray[np.int16]) -> None:
        """Forward mono PCM16 audio to the active session."""
        if self._session is not None:
            await self._session.send_audio(to_bytes(mono))

    async def run(self) -> None:
        """Wait for wake requests and run each session."""
        while not self._shutdown_event.is_set():
            await self._wake_event.wait()
            self._wake_event.clear()
            device_id = self._pending_device
            self._pending_device = None
            if device_id is None:
                continue
            if not self.gate.can_wake():
                if not self._budget_warning_logged:
                    logger.warning("Daily Live audio budget exhausted; remaining asleep.")
                    emit("live.budget_exhausted", "live", "warning")
                    self._budget_warning_logged = True
                self.hub.release(device_id)
                continue
            self._budget_warning_logged = False
            if not self.voice_model.available():
                logger.error("OPENAI_API_KEY is required to wake a Live session")
                emit("live.missing_api_key", "live", "error")
                self.hub.release(device_id)
                continue
            try:
                await self._run_session(device_id)
            except asyncio.CancelledError:
                raise
            except VoiceStartupTimeout:
                self.hub.release(device_id)
            except VoiceConnectionLost as exc:
                logger.warning("Live connection dropped (%s); back to sleep.", exc)
                emit("live.connection_dropped", "live", "warning", error=str(exc))
                if self.gate.state is GateState.LIVE:
                    self.gate.close(CloseReason.ERROR)
                self.hub.release(device_id)
                await asyncio.sleep(2.0)
            except Exception as exc:
                logger.exception("Live session failed")
                emit("live.session_failed", "live", "error", exc_type=type(exc).__name__, error=str(exc))
                if self.gate.state is GateState.LIVE:
                    self.gate.close(CloseReason.ERROR)
                self.hub.release(device_id)
                await asyncio.sleep(2.0)

    async def _run_session(self, device_id: str) -> None:
        self._last_server_reason = None
        self._last_usage_seconds = None
        self._listening = False
        self._speaking = False
        self._user_transcript = ""
        self._assistant_transcript = ""
        owner_tools = self.hub.owner_tools()
        selected = self._select_tools(owner_tools, device_id)
        self._active_tools = {tool.name: tool for tool in selected}
        config = VoiceSessionConfig(
            instructions=f"{self.profile.instructions}\n\n{_ADDENDUM}",
            voice=self.get_current_voice(),
            tools=[tool.schema() for tool in selected],
        )
        async with self.voice_model.open(config) as session:
            self._session = session
            self._session_id = uuid.uuid4().hex[:12]
            close_watch: asyncio.Task[None] | None = None
            gate_started = False
            try:
                self.gate.wake()
                gate_started = True
                self._session_started_at = datetime.now(timezone.utc)
                await self.hub.notify(SessionState.wake(self._session_id))
                preroll = self.hub.preroll(device_id)
                if preroll.size:
                    await session.send_audio(to_bytes(preroll))
                await session.say((self.profile.greeting or DEFAULT_GREETING_PROMPT).strip())
                if self._pending_announcements:
                    announcement = "While you were away: " + " ".join(self._pending_announcements)
                    self._pending_announcements.clear()
                    await session.say(announcement)
                close_watch = asyncio.create_task(self._gate_watch(session))
                async for event in session.events():
                    await self._handle_event(event)
                    if isinstance(event, Closed):
                        self._last_server_reason = event.reason
                        break
            except Exception:
                if self.gate.state is GateState.LIVE:
                    self.gate.request_close(CloseReason.ERROR)
                raise
            finally:
                if close_watch is not None:
                    close_watch.cancel()
                    await asyncio.gather(close_watch, return_exceptions=True)
                for task in (self._user_transcript_task, self._assistant_transcript_task, self._speaking_task):
                    if task is not None:
                        task.cancel()
                for task in self._tool_tasks:
                    task.cancel()
                if self._tool_tasks:
                    await asyncio.gather(*self._tool_tasks, return_exceptions=True)
                self._tool_tasks.clear()
                self._in_flight_tool_calls.clear()
                self._session = None
                if gate_started:
                    reason = self.gate.close_reason_due() or CloseReason.SERVER_CLOSED
                    elapsed = self.gate.close(reason)
                    self._record_spend(elapsed, reason)
                    await self.hub.notify(SessionState.sleep(self._session_id, reason.value))
                self.hub.release(device_id)
                self._session_id = None
                self._session_started_at = None

    def _select_tools(self, owner_tools: Sequence[ToolSpec], device_id: str) -> list[ToolSpec]:
        allow = set(self.profile.tools)
        if not allow:
            allow = {tool.name for tool in (*self.core_tools, *owner_tools)}
        core = [tool for tool in self.core_tools if tool.name in allow]
        names = {tool.name for tool in core}
        selected = list(core)
        for tool in owner_tools:
            if tool.name not in allow:
                continue
            if tool.name in names:
                emit("device.tool_conflict", "gateway", "warning", device=device_id, tool=tool.name)
                continue
            selected.append(tool)
            names.add(tool.name)
        known = {tool.name for tool in (*self.core_tools, *owner_tools)}
        for name in sorted(allow - known):
            logger.debug("Ignoring unknown profile tool %s", name)
        return selected

    async def _gate_watch(self, session: VoiceSession) -> None:
        while self._session is session:
            await asyncio.sleep(0.5)
            if self.gate.close_reason_due() is not None:
                try:
                    await session.close()
                except VoiceConnectionLost:
                    pass
                return

    async def _handle_event(self, event: object) -> None:
        if isinstance(event, AudioOut):
            await self.hub.play(event.pcm16)
            await self._set_flags(speaking=True)
            self._arm_speaking_timer()
        elif isinstance(event, TranscriptDelta):
            if event.role == "user":
                self._user_transcript += event.text
                self.gate.note_user_speech()
                await self._set_flags(listening=True)
                self._arm_user_transcript_timer()
            else:
                self._assistant_transcript += event.text
                self._arm_assistant_transcript_timer()
        elif isinstance(event, ToolCall):
            self._in_flight_tool_calls.add(event.call_id)
            task = asyncio.create_task(self._run_tool(event))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
        elif isinstance(event, MalformedToolCall):
            data = {"tool": event.name} if event.name is not None else {}
            emit("live.malformed_tool_call", "live", "error", **data)
        elif isinstance(event, Usage):
            self._last_usage_seconds = event.seconds
        elif isinstance(event, Closed):
            self._last_server_reason = event.reason
            if event.usage_seconds is not None:
                self._last_usage_seconds = event.usage_seconds
        elif isinstance(event, VoiceError):
            error_data: dict[str, object] = {}
            if event.code is not None:
                error_data["code"] = event.code
            if event.error_type is not None:
                error_data["error_type"] = event.error_type
            if event.message is not None:
                error_data["error"] = event.message
            emit("live.api_error", "live", "error", **error_data)

    async def _run_tool(self, call: ToolCall) -> None:
        result = await self._dispatch_tool(call.name, call.arguments)
        session = self._session
        try:
            if session is not None:
                await session.send_tool_result(call.call_id, result)
        finally:
            self._in_flight_tool_calls.discard(call.call_id)
        if session is not None and not self._in_flight_tool_calls:
            await session.request_response()

    async def _dispatch_tool(self, name: str, args_json: str) -> dict[str, object]:
        try:
            parsed = json.loads(args_json or "{}")
            arguments = parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.warning("Malformed tool arguments for %s", name)
            arguments = {}
        tool = self._active_tools.get(name)
        if tool is None:
            result: dict[str, object] = {"error": f"unknown tool: {name}"}
        else:
            try:
                result = await tool.handler(arguments)
            except asyncio.CancelledError:
                result = {"error": "Tool cancelled"}
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
                logger.error("Tool %s failed (%s)", name, type(exc).__name__)
        if "error" in result:
            error_text = str(result["error"])
            emit("tool.completed", "live", "error", tool=name, ok=False, error=error_text)
            return {"error": error_text}
        emit("tool.completed", "live", tool=name, ok=True)
        return result

    async def _set_flags(self, *, listening: bool | None = None, speaking: bool | None = None) -> None:
        previous = (self._listening, self._speaking)
        if listening is not None:
            self._listening = listening
        if speaking is not None:
            self._speaking = speaking
        if self._session_id is not None and previous != (self._listening, self._speaking):
            await self.hub.notify(
                SessionState.active(self._session_id, listening=self._listening, speaking=self._speaking)
            )

    async def _finalize_user_transcript(self) -> None:
        await asyncio.sleep(self.settings.transcript_finalize_s)
        text, self._user_transcript = self._user_transcript, ""
        if text:
            if self.on_transcript is not None:
                self.on_transcript("user", text)
            await self._set_flags(listening=False)

    async def _finalize_assistant_transcript(self) -> None:
        await asyncio.sleep(self.settings.transcript_finalize_s)
        text, self._assistant_transcript = self._assistant_transcript, ""
        if text and self.on_transcript is not None:
            self.on_transcript("assistant", text)

    async def _finish_speaking(self) -> None:
        await asyncio.sleep(0.4)
        await self._set_flags(speaking=False)

    def _arm_user_transcript_timer(self) -> None:
        if self._user_transcript_task is not None:
            self._user_transcript_task.cancel()
        self._user_transcript_task = asyncio.create_task(self._finalize_user_transcript())

    def _arm_assistant_transcript_timer(self) -> None:
        if self._assistant_transcript_task is not None:
            self._assistant_transcript_task.cancel()
        self._assistant_transcript_task = asyncio.create_task(self._finalize_assistant_transcript())

    def _arm_speaking_timer(self) -> None:
        if self._speaking_task is not None:
            self._speaking_task.cancel()
        self._speaking_task = asyncio.create_task(self._finish_speaking())

    def _record_spend(self, elapsed: float, reason: CloseReason) -> None:
        ended_at = datetime.now(timezone.utc)
        self.spend_log.record(
            SpendRecord(
                started_at=self._session_started_at or ended_at,
                ended_at=ended_at,
                seconds=elapsed,
                reason=reason.value,
                reported_usage_seconds=self._last_usage_seconds,
            )
        )
        emit("live.session_ended", "live", reason=reason.value, seconds=elapsed)

    async def say(self, text: str) -> None:
        """Speak text in the active session."""
        if not text.strip():
            raise ValueError("say: empty text")
        if self._session is None:
            raise RuntimeError("say: no active session")
        await self._session.say(text)

    async def announce(self, text: str) -> None:
        """Speak or queue an announcement."""
        if not text.strip():
            return
        if self._session is not None:
            await self.say(text)
        else:
            self._pending_announcements.append(text.strip())

    async def request_stop(self) -> None:
        """Request closure of the active session."""
        self.gate.request_close(CloseReason.USER_DISMISSED)

    async def get_available_voices(self) -> list[str]:
        """Return the voice names supported by the provider."""
        return list(self.voice_model.voices)

    def get_current_voice(self) -> str:
        """Return the selected voice."""
        candidate = self._voice_override or self.profile.voice or self.settings.voice
        return candidate if candidate in self.voice_model.voices else "ballad"

    async def change_voice(self, voice: str) -> str:
        """Select a voice for the next session."""
        if voice not in self.voice_model.voices:
            raise ValueError(f"Unsupported Live voice: {voice}")
        self._voice_override = voice
        return "Voice changed. Takes effect on the next wake."

    def status(self) -> LiveStatus:
        """Return the current session and budget status."""
        return LiveStatus(
            state=self.gate.state,
            session_elapsed_s=self.gate.session_elapsed_s,
            seconds_used_today=self.gate.seconds_used_today,
            seconds_remaining_today=self.gate.seconds_remaining_today,
            last_close_reason=self.gate.last_close_reason,
        )

    async def shutdown(self) -> None:
        """Wake and stop the session loop."""
        self._shutdown_event.set()
        self._wake_event.set()
        if self._session is not None:
            await self._session.close()
        for task in (
            *self._tool_tasks,
            self._user_transcript_task,
            self._assistant_transcript_task,
            self._speaking_task,
        ):
            if task is not None:
                task.cancel()
