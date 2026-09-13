"""Ambient-home command-line entry point."""

import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

from ambient_home.config import settings_from_env
from ambient_home.runtime import set_settings
from ambient_home.spend_log import SpendLog
from ambient_home.wake_word import OpenWakeWordDetector
from ambient_home.session_gate import CloseReason, SessionGate


logger = logging.getLogger(__name__)


def main() -> None:
    """Start ambient-home and keep wake-word listening active."""
    load_dotenv()
    package_root = Path(__file__).parent
    os.environ.setdefault("REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY", str(package_root / "profiles"))
    os.environ.setdefault("REACHY_MINI_CUSTOM_PROFILE", "jarvis")
    os.environ.setdefault("REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY", str(package_root / "reachy_tools"))

    from reachy_mini import ReachyMini
    from reachy_mini_conversation_app.moves import MovementManager
    from reachy_mini_conversation_app.console import LocalStream
    from reachy_mini_conversation_app.tools.core_tools import ToolDependencies, initialize_tools

    from ambient_home.live_handler import GPTLiveHandler

    settings = settings_from_env()
    set_settings(settings)
    spend_log = SpendLog(settings.data_dir / "spend.jsonl")
    gate = SessionGate(
        idle_timeout_s=settings.idle_timeout_s,
        max_session_s=settings.max_session_s,
        daily_budget_s=settings.daily_budget_s,
        seconds_used_today=spend_log.seconds_today(datetime.now().astimezone()),
    )
    wake_detector = OpenWakeWordDetector(settings.wake_word, settings.wake_threshold)
    robot = ReachyMini()
    movement_manager = MovementManager(current_robot=robot)

    def request_sleep() -> dict[str, object]:
        """Request the current session close without stopping the app."""
        gate.request_close(CloseReason.USER_DISMISSED)
        return {"status": "close_requested"}

    deps = ToolDependencies(
        reachy_mini=robot,
        movement_manager=movement_manager,
        camera_enabled=True,
        go_to_sleep=request_sleep,
    )
    initialize_tools(force=True)

    def build_handler(startup_voice: str | None = None) -> GPTLiveHandler:
        """Build a fresh wake-gated Live handler."""
        return GPTLiveHandler(
            deps,
            settings,
            wake_detector=wake_detector,
            gate=gate,
            spend_log=spend_log,
            startup_voice=startup_voice,
        )

    handler = build_handler(settings.voice)
    logger.info(
        "Starting ambient-home model=%s backend=%s voice=%s wake=%s daily_budget_s=%s",
        settings.live_model,
        settings.backend_model,
        settings.voice,
        settings.wake_word,
        settings.daily_budget_s,
    )
    asyncio.run(handler.prepare_asleep())
    stream = LocalStream(handler, robot, handler_factory=build_handler, startup_voice=settings.voice)
    stream.launch()
