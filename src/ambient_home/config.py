"""Runtime configuration for ambient-home."""

import os
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class AmbientSettings:
    """Resolved ambient-home environment settings."""

    openai_api_key: str | None
    live_model: str
    backend_model: str
    vision_model: str
    voice: str
    wake_word: str
    wake_threshold: float
    preroll_seconds: float
    idle_timeout_s: float
    max_session_s: float
    daily_budget_s: float
    data_dir: Path
    transcript_finalize_s: float
    devin_api_key: str | None = None
    devin_api_base_url: str = "https://api.devin.ai"
    devin_max_acu: int = 5
    job_poll_s: float = 20.0
    ui_host: str = "127.0.0.1"
    ui_port: int = 8765


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def settings_from_env() -> AmbientSettings:
    """Load ambient-home settings from environment variables."""
    backend_model = os.getenv("AMBIENT_BACKEND_MODEL", "gpt-5.6-terra")
    return AmbientSettings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        live_model=os.getenv("AMBIENT_LIVE_MODEL", "gpt-live-1"),
        backend_model=backend_model,
        vision_model=os.getenv("AMBIENT_VISION_MODEL") or backend_model,
        voice=os.getenv("AMBIENT_VOICE", "marin"),
        wake_word=os.getenv("AMBIENT_WAKE_WORD", "hey_jarvis"),
        wake_threshold=_float_env("AMBIENT_WAKE_THRESHOLD", 0.5),
        preroll_seconds=_float_env("AMBIENT_PREROLL_SECONDS", 2.0),
        idle_timeout_s=_float_env("AMBIENT_IDLE_TIMEOUT_S", 60.0),
        max_session_s=_float_env("AMBIENT_MAX_SESSION_S", 600.0),
        daily_budget_s=_float_env("AMBIENT_DAILY_BUDGET_S", 3600.0),
        data_dir=Path(os.getenv("AMBIENT_DATA_DIR", "~/.ambient-home")).expanduser(),
        transcript_finalize_s=_float_env("AMBIENT_TRANSCRIPT_FINALIZE_S", 1.0),
        devin_api_key=os.getenv("DEVIN_API_KEY") or None,
        devin_api_base_url=os.getenv("DEVIN_API_BASE_URL", "https://api.devin.ai"),
        devin_max_acu=_int_env("AMBIENT_DEVIN_MAX_ACU", 5),
        job_poll_s=_float_env("AMBIENT_JOB_POLL_S", 20.0),
        ui_host=os.getenv("AMBIENT_UI_HOST", "127.0.0.1"),
        ui_port=_int_env("AMBIENT_UI_PORT", 8765),
    )
