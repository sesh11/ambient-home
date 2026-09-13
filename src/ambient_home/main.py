"""Ambient-home command-line entry point."""

import os
import asyncio
import logging
from datetime import datetime
from collections.abc import Callable, Awaitable

import uvicorn
from dotenv import load_dotenv

from ambient_home.ui import create_app
from ambient_home.costs import CostCaps, CostRates, CostTracker
from ambient_home.config import settings_from_env
from ambient_home.runtime import set_settings, set_job_service, set_cost_tracker
from ambient_home.mic_check import ensure_microphone_audio
from ambient_home.spend_log import SpendLog
from ambient_home.wake_word import OpenWakeWordDetector
from ambient_home.jobs.board import JobBoard
from ambient_home.jobs.devin import NullWorker, DevinWorker
from ambient_home.jobs.worker import Worker
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.session_gate import GateState, CloseReason, SessionGate


logger = logging.getLogger(__name__)


def main() -> None:
    """Start ambient-home and keep wake-word listening active."""
    load_dotenv()
    log_level = logging.getLevelNamesMapping().get(os.getenv("AMBIENT_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from reachy_mini import ReachyMini
    from reachy_mini_conversation_app.moves import MovementManager
    from reachy_mini_conversation_app.config import config as reachy_config
    from reachy_mini_conversation_app.console import LocalStream
    from reachy_mini_conversation_app.tools.core_tools import ToolDependencies, initialize_tools

    from ambient_home.live_handler import GPTLiveHandler

    settings = settings_from_env()
    set_settings(settings)
    current_handler: GPTLiveHandler | None = None
    background_factories: list[Callable[[], Awaitable[None]]] = []

    async def announce(text: str) -> None:
        if current_handler is not None:
            await current_handler.announce(text)

    if settings.devin_api_key and settings.devin_org_id:
        worker: Worker = DevinWorker(
            settings.devin_api_key,
            settings.devin_org_id,
            base_url=settings.devin_api_base_url,
            max_acu=settings.devin_max_acu,
        )
    else:
        missing = [
            name
            for name, value in (
                ("DEVIN_API_KEY", settings.devin_api_key),
                ("DEVIN_ORG_ID", settings.devin_org_id),
            )
            if not value
        ]
        message = f"{', '.join(missing)} is not configured; dispatched jobs will fail clearly."
        logger.warning(message)
        worker = NullWorker(error=message)
    board = JobBoard(settings.data_dir / "jobs.json")
    spend_log = SpendLog(settings.data_dir / "spend.jsonl")
    gate = SessionGate(
        idle_timeout_s=settings.idle_timeout_s,
        max_session_s=settings.max_session_s,
        daily_budget_s=settings.daily_budget_s,
        seconds_used_today=spend_log.seconds_today(datetime.now().astimezone()),
    )
    cost_tracker = CostTracker(
        spend_log,
        board,
        rates=CostRates(
            live_usd_per_minute=settings.live_usd_per_minute,
            acu_usd=settings.acu_usd,
        ),
        caps=CostCaps(live_daily_s=settings.daily_budget_s, acu_monthly=settings.monthly_acu_cap),
        history_days=settings.cost_history_days,
        live_session_seconds=lambda: gate.session_elapsed_s,
    )
    set_cost_tracker(cost_tracker)
    job_service = JobService(
        board,
        worker,
        announce,
        dispatch_guard=lambda: cost_tracker.dispatch_refusal(datetime.now().astimezone()),
    )
    set_job_service(job_service)
    wake_detector = OpenWakeWordDetector(settings.wake_word, settings.wake_threshold)
    if settings.mic_autorecover:
        ensure_microphone_audio()
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
        nonlocal current_handler
        current_handler = GPTLiveHandler(
            deps,
            settings,
            wake_detector=wake_detector,
            gate=gate,
            spend_log=spend_log,
            startup_voice=startup_voice,
            background_tasks=background_factories,
        )
        return current_handler

    async def request_stop() -> None:
        if current_handler is not None:
            await current_handler.request_stop()

    def status() -> LiveStatus:
        if current_handler is not None:
            return current_handler.status()
        return LiveStatus(
            state=GateState.ASLEEP,
            session_elapsed_s=0.0,
            seconds_used_today=gate.seconds_used_today,
            seconds_remaining_today=gate.seconds_remaining_today,
            last_close_reason=gate.last_close_reason,
        )

    handler = build_handler(settings.voice)
    ui_app = create_app(status, job_service, request_stop, cost_tracker)
    ui_server = uvicorn.Server(
        uvicorn.Config(ui_app, host=settings.ui_host, port=settings.ui_port, log_level="warning")
    )

    async def run_ui() -> None:
        await ui_server.serve()

    background_factories.extend(
        [
            lambda: job_service.run_poller(settings.job_poll_s),
            lambda: cost_tracker.run_alert_poller(settings.job_poll_s, announce),
            run_ui,
        ]
    )
    logger.info(
        "Starting ambient-home model=%s backend=%s voice=%s wake=%s profile=%s profiles_dir=%s daily_budget_s=%s",
        settings.live_model,
        settings.backend_model,
        settings.voice,
        settings.wake_word,
        reachy_config.REACHY_MINI_CUSTOM_PROFILE,
        reachy_config.PROFILES_DIRECTORY,
        settings.daily_budget_s,
    )
    asyncio.run(handler.prepare_asleep())
    stream = LocalStream(handler, robot, handler_factory=build_handler, startup_voice=settings.voice)
    stream.launch()
