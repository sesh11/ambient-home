"""OpenAI GPT-Live conversation handler."""

import json
import time
import base64
import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from collections.abc import Callable, Awaitable, AsyncIterator

import numpy as np
from openai import AsyncOpenAI
from numpy.typing import NDArray
from websockets.exceptions import ConnectionClosed
from openai.resources.live.live import AsyncLiveConnection
from openai.types.live.server_event import ServerEvent
from openai.types.live.built_in_voice import BuiltInVoice
from reachy_mini_conversation_app.config import set_custom_profile
from reachy_mini_conversation_app.prompts import get_session_instructions, get_session_greeting_prompt
from openai.types.live.function_tool_param import FunctionToolParam
from openai.types.live.session_config_param import SessionConfigParam, DelegationResponses
from reachy_mini_conversation_app.streaming import AdditionalOutputs
from reachy_mini_conversation_app.tools.core_tools import (
    ToolDependencies,
    get_tool_specs,
    initialize_tools,
)
from reachy_mini_conversation_app.conversation_handler import ConversationHandler
from openai.types.live.responses_delegation_config_param import ResponsesDelegationConfigParam
from reachy_mini_conversation_app.tools.background_tool_manager import (
    ToolCallRoutine,
    ToolNotification,
    BackgroundToolManager,
)

from ambient_home.audio import PrerollBuffer, pcm16_to_base64, frame_to_mono_int16
from ambient_home.config import AmbientSettings
from ambient_home.spend_log import SpendLog, SpendRecord
from ambient_home.wake_word import WakeWordDetector
from ambient_home.session_gate import GateState, CloseReason, SessionGate


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveStatus:
    """Read-only status for a future UI."""

    state: GateState
    session_elapsed_s: float
    seconds_used_today: float
    seconds_remaining_today: float
    last_close_reason: CloseReason | None


class GPTLiveHandler(ConversationHandler):
    """Conversation handler backed by OpenAI's GPT-Live WebSocket API."""

    SAMPLE_RATE = 16_000
    _VOICES: tuple[BuiltInVoice, ...] = (
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
        deps: ToolDependencies,
        settings: AmbientSettings,
        *,
        wake_detector: WakeWordDetector,
        gate: SessionGate,
        spend_log: SpendLog,
        instance_path: str | None = None,
        startup_voice: str | None = None,
        client: AsyncOpenAI | None = None,
        background_tasks: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        """Initialize the Live handler and local wake gate."""
        super().__init__()
        self.deps = deps
        self.settings = settings
        self.wake_detector = wake_detector
        self.gate = gate
        self.spend_log = spend_log
        self.instance_path = instance_path
        self.client = client or AsyncOpenAI(api_key=settings.openai_api_key or "DUMMY")
        self.connection: AsyncLiveConnection | None = None
        self.output_queue: asyncio.Queue[tuple[int, NDArray[np.int16]] | AdditionalOutputs] = asyncio.Queue()
        self.tool_manager = BackgroundToolManager()
        self._preroll = PrerollBuffer(settings.preroll_seconds, self.SAMPLE_RATE)
        self._wake_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._voice_override = startup_voice if startup_voice in self._VOICES else None
        self._user_transcript = ""
        self._assistant_transcript = ""
        self._user_transcript_task: asyncio.Task[None] | None = None
        self._assistant_transcript_task: asyncio.Task[None] | None = None
        self._speaking_task: asyncio.Task[None] | None = None
        self._in_flight_tool_calls: set[str] = set()
        self._last_usage_seconds: float | None = None
        self._last_server_reason: str | None = None
        self._session_started_at: datetime | None = None
        self._budget_warning_logged = False
        self._pending_announcements: list[str] = []
        self._background_task_factories = background_tasks if background_tasks is not None else []
        self._background_tasks: list[asyncio.Task[None]] = []
        self._audio_input_started = False
        self._asleep_heartbeat_at = time.monotonic()
        self._asleep_peak_mic_rms = 0.0
        self._received_audio_seconds = 0.0
        self._saw_nonzero_audio = False
        self._silence_error_logged = False

    def _is_connected(self) -> bool:
        """Return whether a Live connection is active."""
        return self.connection is not None

    async def start_up(self) -> None:
        """Run the wake-gated session supervisor until shutdown."""
        if not self._background_tasks:
            self._background_tasks = [
                asyncio.create_task(self._run_background_task(factory), name=f"ambient-background-{index}")
                for index, factory in enumerate(self._background_task_factories)
            ]
        while not self._shutdown_event.is_set():
            await self._wake_event.wait()
            self._wake_event.clear()
            if self._shutdown_event.is_set():
                break
            if not self.gate.can_wake():
                if not self._budget_warning_logged:
                    logger.warning("Daily Live audio budget exhausted; remaining asleep.")
                    self._budget_warning_logged = True
                continue
            self._budget_warning_logged = False
            try:
                await self._run_live_session()
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                logger.warning("Live connection dropped (%s); back to sleep.", exc)
                if self.gate.state is GateState.LIVE:
                    self.gate.close(CloseReason.ERROR)
                await asyncio.sleep(2.0)
            except Exception:
                logger.exception("Live session failed")
                if self.gate.state is GateState.LIVE:
                    self.gate.close(CloseReason.ERROR)
                await asyncio.sleep(2.0)

    async def shutdown(self) -> None:
        """Stop supervision and close the active Live session."""
        self._shutdown_event.set()
        self._wake_event.set()
        if self.connection is not None:
            await self._close_connection()
        if self._user_transcript_task is not None:
            self._user_transcript_task.cancel()
        if self._assistant_transcript_task is not None:
            self._assistant_transcript_task.cancel()
        if self._speaking_task is not None:
            self._speaking_task.cancel()
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        await self.tool_manager.shutdown()

    async def _run_background_task(self, factory: Callable[[], Awaitable[None]]) -> None:
        await factory()

    async def receive(self, frame: tuple[int, NDArray[np.int16]]) -> None:
        """Receive a local audio frame in either asleep or Live state."""
        if not self._audio_input_started:
            logger.info("Audio input started: %d Hz, frame shape %s", frame[0], frame[1].shape)
            self._audio_input_started = True
        mono = frame_to_mono_int16(frame[1])
        if frame[0] > 0:
            self._received_audio_seconds += len(mono) / frame[0]
        if np.any(mono):
            self._saw_nonzero_audio = True
        if self._received_audio_seconds >= 5.0 and not self._saw_nonzero_audio and not self._silence_error_logged:
            logger.error(
                "Microphone has delivered only zeros for 5 s. Known Reachy Mini Lite XMOS startup fault "
                "(pollen-robotics/reachy_mini#770). Run `uv run ambient-mic-check --reboot-xmos`, then "
                "restart ambient-home; if still silent, check the microphone FPC cable."
            )
            self._silence_error_logged = True
        if not self._is_connected():
            self._preroll.append(mono)
        if self.gate.state is GateState.ASLEEP:
            self._asleep_peak_mic_rms = max(
                self._asleep_peak_mic_rms,
                float(np.sqrt(np.mean(np.square(mono, dtype=np.float32)))),
            )
            wake_detected = self.wake_detector.feed(mono)
            now = time.monotonic()
            if now - self._asleep_heartbeat_at >= 5.0:
                logger.info(
                    "Asleep: mic_rms=%.0f wake_score=%.2f",
                    self._asleep_peak_mic_rms,
                    self.wake_detector.take_peak_score(),
                )
                self._asleep_heartbeat_at = now
                self._asleep_peak_mic_rms = 0.0
            if wake_detected:
                logger.info("Wake word detected (score=%.2f)", self.wake_detector.last_score)
                self._wake_event.set()
            return
        if self.connection is not None:
            session = self.connection.session
            await session.input_audio.append(audio=pcm16_to_base64(mono))

    def _session_config(self) -> SessionConfigParam:
        """Build immutable Live startup configuration."""
        tool_specs = get_tool_specs()
        tools: list[FunctionToolParam] = [
            {
                "type": "function",
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
                "strict": False,
            }
            for spec in tool_specs
        ]
        addendum = (
            "You are woken by 'hey jarvis'. Keep answers short. When the user says "
            "that's all, go to sleep, thanks, that's it, or similar, delegate end_conversation."
        )
        responses: ResponsesDelegationConfigParam = {
            "model": self.settings.backend_model,
            "instructions": "Execute robot tools when asked and return concise text results.",
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
        delegation: DelegationResponses = {
            "type": "responses",
            "responses": responses,
        }
        return {
            "model": self.settings.live_model,
            "audio": {"format": {"type": "audio/pcm", "rate": 16_000}, "output": {"voice": self.get_current_voice()}},
            "instructions": f"{get_session_instructions(self.instance_path)}\n\n{addendum}",
            "delegation": delegation,
        }

    async def _run_live_session(self) -> None:
        """Run one Live connection from startup through terminal close."""
        if not self.settings.openai_api_key:
            logger.error("OPENAI_API_KEY is required to wake a Live session")
            return
        self._last_server_reason = None
        self._last_usage_seconds = None
        async with self.client.live.connect() as connection:
            await connection.session.start(session=self._session_config())
            started = False
            try:
                async with asyncio.timeout(10.0):
                    event_stream: AsyncIterator[ServerEvent] = connection.__aiter__()
                    async for event in event_stream:
                        event_data = event.model_dump()
                        await self._handle_event(event_data)
                        if event_data.get("type") == "session.started":
                            started = True
                            break
            except TimeoutError:
                logger.error("Timed out waiting for session.started")
                return
            if not started:
                return
            self.connection = connection
            self.gate.wake()
            self._session_started_at = datetime.now(timezone.utc)
            await self._wake_robot()
            self.tool_manager.start_up(tool_callbacks=[self._handle_tool_result])
            preroll = self._preroll.drain()
            if preroll.size:
                await connection.session.input_audio.append(audio=pcm16_to_base64(preroll))
            greeting = get_session_greeting_prompt().strip()
            if greeting:
                await connection.session.commentary.append(content=greeting, delegation_id=None)
            if self._pending_announcements:
                announcements = "While you were away: " + " ".join(self._pending_announcements)
                self._pending_announcements.clear()
                await connection.session.commentary.append(content=announcements, delegation_id=None)
            close_watch = asyncio.create_task(self._gate_watch())
            try:
                event_stream = connection.__aiter__()
                async for event in event_stream:
                    await self._handle_event(event.model_dump())
                    if self._last_server_reason is not None:
                        break
            finally:
                close_watch.cancel()
                try:
                    await close_watch
                except asyncio.CancelledError:
                    pass
                await self.tool_manager.shutdown()
                self.connection = None
                reason = self.gate.close_reason_due() or CloseReason.SERVER_CLOSED
                elapsed = self.gate.close(reason)
                self._record_spend(elapsed, reason)
                self._session_started_at = None
                await self._sleep_robot()

    async def _gate_watch(self) -> None:
        """Close a session when its gate reports a due limit."""
        close_sent = False
        while self.connection is not None and not close_sent:
            await asyncio.sleep(0.5)
            reason = self.gate.close_reason_due()
            if reason is not None and self.connection is not None:
                close_sent = True
                try:
                    await self.connection.session.close()
                except ConnectionClosed:
                    pass

    async def _handle_event(self, event: dict[str, object]) -> None:
        """Dispatch one decoded Live server event."""
        event_type = event.get("type")
        if event_type == "session.output_audio.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                pcm = np.frombuffer(base64.b64decode(delta), dtype=np.int16).reshape(1, -1)
                await self.output_queue.put((self.SAMPLE_RATE, pcm))
                self._mark_activity("assistant_audio_delta")
                self.deps.movement_manager.set_speaking(True)
                self._arm_speaking_timer()
        elif event_type == "session.input_transcript.delta":
            self._user_transcript += str(event.get("delta", ""))
            self.gate.note_user_speech()
            self._mark_activity("user_transcript_delta")
            self.deps.movement_manager.set_listening(True)
            await self.output_queue.put(AdditionalOutputs({"role": "user_partial", "content": self._user_transcript}))
            self._arm_user_transcript_timer()
        elif event_type == "session.output_transcript.delta":
            self._assistant_transcript += str(event.get("delta", ""))
            self._mark_activity("assistant_transcript_delta")
            self._arm_assistant_transcript_timer()
        elif event_type == "session.delegation.created":
            delegation = event.get("delegation")
            await self.output_queue.put(
                AdditionalOutputs({"role": "assistant", "content": f"Delegating: {delegation}"})
            )
        elif event_type == "response.event":
            nested = event.get("event")
            if isinstance(nested, dict):
                await self._handle_nested_response_event(nested)
        elif event_type == "session.usage.updated":
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("seconds"), (int, float)):
                self._last_usage_seconds = float(usage["seconds"])
        elif event_type == "session.closed":
            self._last_server_reason = str(event.get("reason", "server_closed"))
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("seconds"), (int, float)):
                self._last_usage_seconds = float(usage["seconds"])
        elif event_type == "error":
            logger.error("Live API error: %s", event)
            await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": f"[error] {event}"}))
        else:
            logger.debug("Ignoring Live event %s", event_type)

    async def _handle_nested_response_event(self, event: dict[str, object]) -> None:
        """Handle tool calls nested inside a Responses event envelope."""
        if event.get("type") != "response.output_item.done":
            if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                logger.info("Responses event: %s", event.get("type"))
            return
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
            logger.error("Malformed Live function call: %s", item)
            return
        self._in_flight_tool_calls.add(call_id)
        await self.tool_manager.start_tool(
            call_id=call_id,
            tool_call_routine=ToolCallRoutine(tool_name=name, args_json_str=arguments, deps=self.deps),
            is_idle_tool_call=False,
        )
        await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": f"Calling {name}"}))

    async def _handle_tool_result(self, completed_tool: ToolNotification) -> None:
        """Submit a completed Pollen tool result to Responses delegation."""
        if completed_tool.error is not None:
            result: object = {"error": completed_tool.error}
            logger.error("Tool %s failed: %s", completed_tool.tool_name, completed_tool.error)
        else:
            result = completed_tool.result or {"error": "No result returned from tool execution"}
        await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": json.dumps(result)}))
        connection = self.connection
        if connection is None or completed_tool.is_idle_tool_call:
            return
        await connection.response.item.create(
            item={"type": "function_call_output", "call_id": completed_tool.id, "output": json.dumps(result)}
        )
        self._in_flight_tool_calls.discard(completed_tool.id)
        if not self._in_flight_tool_calls:
            await connection.response.create()

    async def say(self, text: str) -> None:
        """Ask the active Live model to speak a short commentary."""
        if not text.strip():
            raise ValueError("say: empty text")
        if self.connection is None:
            raise RuntimeError("say: no active session")
        await self.connection.session.commentary.append(content=text, delegation_id=None)
        self._mark_activity("say")

    async def announce(self, text: str) -> None:
        """Speak an announcement now or queue it for the next wake."""
        if not text.strip():
            return
        if self.connection is not None:
            await self.say(text)
        else:
            self._pending_announcements.append(text.strip())

    async def request_stop(self) -> None:
        """Request that the current Live session stop."""
        self.gate.request_close(CloseReason.USER_DISMISSED)

    async def apply_personality(self, profile: str | None) -> str:
        """Apply a profile for the next session."""
        set_custom_profile(profile)
        initialize_tools(self.instance_path, force=True)
        return "Applied personality. Takes effect on the next wake."

    async def get_available_voices(self) -> list[str]:
        """Return built-in Live voices."""
        return list(self._VOICES)

    def get_current_voice(self) -> str:
        """Return the configured Live voice."""
        return self._voice_override or (self.settings.voice if self.settings.voice in self._VOICES else "ballad")

    async def change_voice(self, voice: str) -> str:
        """Set a voice override for the next session."""
        if voice not in self._VOICES:
            raise ValueError(f"Unsupported Live voice: {voice}")
        self._voice_override = voice
        return "Voice changed. Takes effect on the next wake."

    def status(self) -> LiveStatus:
        """Return current gate and spend status."""
        return LiveStatus(
            state=self.gate.state,
            session_elapsed_s=self.gate.session_elapsed_s,
            seconds_used_today=self.gate.seconds_used_today,
            seconds_remaining_today=self.gate.seconds_remaining_today,
            last_close_reason=self.gate.last_close_reason,
        )

    def _arm_user_transcript_timer(self) -> None:
        if self._user_transcript_task is not None:
            self._user_transcript_task.cancel()
        self._user_transcript_task = asyncio.create_task(self._finalize_user_transcript())

    async def _finalize_user_transcript(self) -> None:
        await asyncio.sleep(self.settings.transcript_finalize_s)
        if self._user_transcript:
            text = self._user_transcript
            self._user_transcript = ""
            await self.output_queue.put(AdditionalOutputs({"role": "user", "content": text}))
            self._emit_transcript("user", text, True)
            self.deps.movement_manager.set_listening(False)

    def _arm_assistant_transcript_timer(self) -> None:
        if self._assistant_transcript_task is not None:
            self._assistant_transcript_task.cancel()
        self._assistant_transcript_task = asyncio.create_task(self._finalize_assistant_transcript())

    async def _finalize_assistant_transcript(self) -> None:
        await asyncio.sleep(self.settings.transcript_finalize_s)
        if self._assistant_transcript:
            text = self._assistant_transcript
            self._assistant_transcript = ""
            await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": text}))
            self._emit_transcript("assistant", text, True)

    def _arm_speaking_timer(self) -> None:
        if self._speaking_task is not None:
            self._speaking_task.cancel()
        self._speaking_task = asyncio.create_task(self._finish_speaking())

    async def _finish_speaking(self) -> None:
        await asyncio.sleep(0.4)
        self.deps.movement_manager.set_speaking(False)

    async def _close_connection(self) -> None:
        if self.connection is not None:
            try:
                await self.connection.session.close()
            except ConnectionClosed:
                pass
            self.connection = None

    async def _wake_robot(self) -> None:
        try:
            await asyncio.to_thread(self.deps.reachy_mini.enable_motors)
            await asyncio.to_thread(self.deps.reachy_mini.wake_up)
            await asyncio.to_thread(self.deps.movement_manager.start)
            self.deps.movement_manager.set_listening(False)
        except Exception:
            logger.exception("Failed to wake robot")

    async def _sleep_robot(self) -> None:
        try:
            try:
                await asyncio.to_thread(self.deps.reachy_mini.disable_wobbling)
            except Exception:
                logger.debug("Failed to disable robot wobbling before sleep", exc_info=True)
            await asyncio.to_thread(self.deps.movement_manager.stop, reset_to_neutral=False)
            await asyncio.to_thread(self.deps.reachy_mini.goto_sleep)
        except Exception:
            logger.exception("Failed to put robot to sleep")

    async def prepare_asleep(self) -> None:
        """Put the robot in its sleep pose before audio streaming starts."""
        await self._sleep_robot()

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
