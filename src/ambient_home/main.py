"""Ambient-home command-line entry point."""

import os
import time
import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from garage_sdk import EventLog, Resource, EventLoggingHandler, emit, configure
from garage_sdk.resource import install_id, version_stamp

from ambient_home import __version__
from ambient_home.ui import create_app
from ambient_home.costs import CostRates, CostTracker
from ambient_home.config import AmbientSettings, settings_from_env
from ambient_home.gateway import DeviceGateway, load_device_configs
from ambient_home.persona import load_profile
from ambient_home.spend_log import SpendLog
from ambient_home.wake_word import OpenWakeWordDetector
from ambient_home.jobs.board import JobBoard
from ambient_home.tools.core import build_core_tools
from ambient_home.conversation import Conversation
from ambient_home.jobs.service import JobService
from ambient_home.session_gate import CloseReason, SessionGate
from ambient_home.connectors.coding.devin import NullWorker, DevinWorker
from ambient_home.connectors.coding.worker import Worker
from ambient_home.connectors.garage_requests import HttpRequestLog
from ambient_home.connectors.voice.openai_live import OpenAILiveVoiceModel
from ambient_home.connectors.vision.openai_vision import OpenAIVision


logger = logging.getLogger(__name__)


@dataclass
class App:
    """Configured ambient-home services."""

    ui: FastAPI
    gateway: DeviceGateway
    conversation: Conversation
    job_service: JobService
    settings: AmbientSettings

    async def run(self) -> None:
        """Run the UI, device gateway, conversation, poller and heartbeat."""
        server = uvicorn.Server(
            uvicorn.Config(self.ui, host=self.settings.ui_host, port=self.settings.ui_port, log_level="warning")
        )

        async def heartbeat() -> None:
            started_at = time.monotonic()
            while True:
                await asyncio.sleep(self.settings.heartbeat_s)
                emit(
                    "app.heartbeat",
                    "app",
                    state=self.conversation.status().state.value,
                    uptime_s=time.monotonic() - started_at,
                )

        try:
            await asyncio.gather(
                server.serve(),
                self.job_service.run_poller(self.settings.job_poll_s),
                heartbeat(),
                self.gateway.run(),
                self.conversation.run(),
            )
        finally:
            await self.conversation.shutdown()
            await self.gateway.stop()
            configure(None)


def build_app(settings: AmbientSettings) -> App:
    """Compose the hub without opening providers, loading wake models, or requiring devices."""
    resource = None
    if settings.event_resource:
        resource = Resource(
            app="ambient-home",
            version=version_stamp(__version__, None),
            install=install_id(settings.data_dir / "install_id"),
        )
    event_log = EventLog(settings.events_dir, retention_days=settings.event_retention_days, resource=resource)
    configure(event_log)
    event_log.prune()
    root_logger = logging.getLogger()
    if not any(isinstance(handler, EventLoggingHandler) for handler in root_logger.handlers):
        root_logger.addHandler(EventLoggingHandler())

    worker: Worker
    if settings.devin_api_key and settings.devin_org_id:
        worker = DevinWorker(
            settings.devin_api_key,
            settings.devin_org_id,
            base_url=settings.devin_api_base_url,
            max_acu=settings.devin_max_acu,
        )
    else:
        missing = [
            name
            for name, value in (("DEVIN_API_KEY", settings.devin_api_key), ("DEVIN_ORG_ID", settings.devin_org_id))
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
    costs = CostTracker(
        spend_log,
        board,
        rates=CostRates(live_usd_per_minute=settings.live_usd_per_minute, acu_usd=settings.acu_usd),
        history_days=settings.cost_history_days,
        live_session_seconds=lambda: gate.session_elapsed_s,
    )
    request_log = (
        HttpRequestLog(settings.garage_url, settings.garage_repos, token=settings.garage_token)
        if settings.garage_url is not None
        else None
    )
    conversation_ref: Conversation | None = None

    async def announce(text: str) -> None:
        if conversation_ref is not None:
            await conversation_ref.announce(text)

    jobs = JobService(board, worker, announce, request_log=request_log)
    profile = load_profile(settings.profile, settings.profiles_dir)
    voice_model = OpenAILiveVoiceModel(settings.openai_api_key, settings.live_model, settings.backend_model)
    vision = OpenAIVision(settings.openai_api_key, settings.vision_model) if settings.openai_api_key else None
    gateway = DeviceGateway(
        load_device_configs(settings.devices_file),
        wake_detector_factory=lambda: OpenWakeWordDetector(settings.wake_word, settings.wake_threshold),
        preroll_seconds=settings.preroll_seconds,
        describe_image=vision,
    )
    core_tools = build_core_tools(
        jobs,
        costs,
        lambda: gate.request_close(CloseReason.USER_DISMISSED),
    )
    conversation = Conversation(
        settings,
        voice_model=voice_model,
        gate=gate,
        spend_log=spend_log,
        profile=profile,
        core_tools=core_tools,
        hub=gateway,
        startup_voice=profile.voice or settings.voice,
    )
    conversation_ref = conversation
    gateway.set_listener(conversation)
    ui = create_app(
        conversation.status,
        jobs,
        conversation.request_stop,
        costs,
        devices=gateway.status,
    )
    emit(
        "app.started",
        "app",
        wake_word=settings.wake_word,
        live_model=settings.live_model,
        backend_model=settings.backend_model,
    )
    logger.info(
        "Starting ambient-home model=%s backend=%s voice=%s wake=%s profile=%s profiles_dir=%s daily_budget_s=%s",
        settings.live_model,
        settings.backend_model,
        settings.voice,
        settings.wake_word,
        profile.name,
        settings.profiles_dir,
        settings.daily_budget_s,
    )
    return App(ui, gateway, conversation, jobs, settings)


def main() -> None:
    """Load environment settings and run the ambient-home hub."""
    load_dotenv()
    log_level = logging.getLevelNamesMapping().get(os.getenv("AMBIENT_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = build_app(settings_from_env())
    asyncio.run(app.run())
