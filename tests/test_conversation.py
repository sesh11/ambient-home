"""Provider-neutral conversation orchestration tests."""

from contextlib import asynccontextmanager
from dataclasses import replace

import numpy as np
import pytest

from ambient_device_sdk import ToolSpec, SessionPhase
from ambient_home.config import settings_from_env
from ambient_home.persona import load_profile
from ambient_home.spend_log import SpendLog
from ambient_home.conversation import Conversation
from ambient_home.session_gate import CloseReason, SessionGate
from ambient_home.connectors.voice.base import Closed


class Session:
    def __init__(self) -> None:
        self.spoken = []

    async def send_audio(self, pcm16: bytes) -> None:
        return

    async def events(self):
        yield Closed(CloseReason.USER_DISMISSED.value, None)

    async def send_tool_result(self, call_id: str, result: dict[str, object]) -> None:
        return

    async def request_response(self) -> None:
        return

    async def say(self, text: str) -> None:
        self.spoken.append(text)

    async def close(self) -> None:
        return


class Voice:
    name = "fake"
    voices = ("ballad", "alloy")

    def __init__(self) -> None:
        self.session = Session()
        self.config = None

    def available(self) -> bool:
        return True

    @asynccontextmanager
    async def open(self, config):
        self.config = config
        yield self.session


class Hub:
    def __init__(self) -> None:
        self.owner = "device-1"
        self.notifications = []

    def owner_tools(self):
        return []

    def preroll(self, device_id: str):
        return np.empty(0, dtype=np.int16)

    async def play(self, pcm16_16k_mono: bytes) -> None:
        return

    async def stop_playback(self) -> None:
        return

    async def notify(self, state) -> None:
        self.notifications.append(state)

    def release(self, device_id: str) -> None:
        self.owner = None


@pytest.mark.asyncio
async def test_session_sends_greeting_notifies_owner_and_records_spend(tmp_path) -> None:
    settings = replace(settings_from_env(), data_dir=tmp_path, transcript_finalize_s=0.01)
    gate = SessionGate(
        idle_timeout_s=60,
        max_session_s=600,
        daily_budget_s=3600,
        seconds_used_today=0,
    )
    voice = Voice()
    hub = Hub()
    conversation = Conversation(
        settings,
        voice_model=voice,
        gate=gate,
        spend_log=SpendLog(tmp_path / "spend.jsonl"),
        profile=load_profile(),
        core_tools=[],
        hub=hub,
    )
    await conversation._run_session("device-1")
    assert voice.session.spoken == [load_profile().greeting]
    assert hub.notifications[0].phase is SessionPhase.WAKE
    assert hub.notifications[-1].phase is SessionPhase.SLEEP
    assert hub.notifications[-1].reason == CloseReason.SERVER_CLOSED.value
    assert gate.state.value == "asleep"
    assert (tmp_path / "spend.jsonl").exists()


def test_core_tools_precede_owner_tools_and_unknown_profile_names_are_ignored() -> None:
    async def handler(_: dict[str, object]) -> dict[str, object]:
        return {}

    core = ToolSpec("same", "core", {"type": "object"}, handler)
    owner = ToolSpec("same", "device", {"type": "object"}, handler)
    settings = settings_from_env()
    conversation = Conversation(
        settings,
        voice_model=Voice(),
        gate=SessionGate(idle_timeout_s=1, max_session_s=2, daily_budget_s=3, seconds_used_today=0),
        spend_log=SpendLog(settings.data_dir / "conversation-test-spend.jsonl"),
        profile=replace(load_profile(), tools=("same", "missing")),
        core_tools=[core],
        hub=Hub(),
    )
    assert conversation._select_tools([owner], "device-1") == [core]
