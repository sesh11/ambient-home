"""Runtime configuration for ambient-home."""

import os
from pathlib import Path
from dataclasses import field, dataclass


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
    profile: str = "alfred"
    profiles_dir: Path | None = None
    memory_file: Path | None = None
    devices_file: Path = field(default_factory=lambda: Path("~/.ambient-home/devices.toml").expanduser())
    garage_url: str | None = None
    garage_token: str | None = None
    garage_repos: frozenset[str] = frozenset({"sesh11/ambient-home"})
    event_resource: bool = False
    devin_api_key: str | None = None
    devin_org_id: str | None = None
    devin_api_base_url: str = "https://api.devin.ai"
    devin_max_acu: int = 5
    live_usd_per_minute: float = 0.3
    acu_usd: float = 2.25
    cost_history_days: int = 7
    job_poll_s: float = 20.0
    ui_host: str = "127.0.0.1"
    ui_port: int = 8765
    events_dir: Path = field(default_factory=lambda: Path("~/Library/Logs/ambient-home/events").expanduser())
    event_retention_days: int = 14
    heartbeat_s: float = 300.0


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


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def settings_from_env() -> AmbientSettings:
    """Load ambient-home settings from environment variables."""
    backend_model = os.getenv("AMBIENT_BACKEND_MODEL", "gpt-5.6-terra")
    data_dir = Path(os.getenv("AMBIENT_DATA_DIR", "~/.ambient-home")).expanduser()
    profiles_dir = os.getenv("AMBIENT_PROFILES_DIR")
    return AmbientSettings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        live_model=os.getenv("AMBIENT_LIVE_MODEL", "gpt-live-1"),
        backend_model=backend_model,
        vision_model=os.getenv("AMBIENT_VISION_MODEL") or backend_model,
        voice=os.getenv("AMBIENT_VOICE", "ballad"),
        wake_word=os.getenv("AMBIENT_WAKE_WORD", "hey_jarvis"),
        wake_threshold=_float_env("AMBIENT_WAKE_THRESHOLD", 0.5),
        preroll_seconds=_float_env("AMBIENT_PREROLL_SECONDS", 2.0),
        idle_timeout_s=_float_env("AMBIENT_IDLE_TIMEOUT_S", 60.0),
        max_session_s=_float_env("AMBIENT_MAX_SESSION_S", 600.0),
        daily_budget_s=_float_env("AMBIENT_DAILY_BUDGET_S", 3600.0),
        data_dir=data_dir,
        transcript_finalize_s=_float_env("AMBIENT_TRANSCRIPT_FINALIZE_S", 1.0),
        profile=os.getenv("AMBIENT_PROFILE", "alfred"),
        profiles_dir=Path(profiles_dir).expanduser() if profiles_dir else None,
        memory_file=Path(os.environ["AMBIENT_MEMORY_FILE"]).expanduser() if os.getenv("AMBIENT_MEMORY_FILE") else None,
        devices_file=Path(os.getenv("AMBIENT_DEVICES_FILE", str(data_dir / "devices.toml"))).expanduser(),
        garage_url=os.getenv("AMBIENT_GARAGE_URL") or None,
        garage_token=os.getenv("AMBIENT_GARAGE_TOKEN") or None,
        garage_repos=frozenset(
            repository.strip()
            for repository in os.getenv("AMBIENT_GARAGE_REPOS", "sesh11/ambient-home").split(",")
            if repository.strip()
        ),
        event_resource=_bool_env("AMBIENT_EVENT_RESOURCE", False),
        devin_api_key=os.getenv("DEVIN_API_KEY") or None,
        devin_org_id=os.getenv("DEVIN_ORG_ID") or None,
        devin_api_base_url=os.getenv("DEVIN_API_BASE_URL", "https://api.devin.ai"),
        devin_max_acu=_int_env("AMBIENT_DEVIN_MAX_ACU", 5),
        live_usd_per_minute=_float_env("AMBIENT_LIVE_USD_PER_MINUTE", 0.3),
        acu_usd=_float_env("AMBIENT_ACU_USD", 2.25),
        cost_history_days=_int_env("AMBIENT_COST_HISTORY_DAYS", 7),
        job_poll_s=_float_env("AMBIENT_JOB_POLL_S", 20.0),
        ui_host=os.getenv("AMBIENT_UI_HOST", "127.0.0.1"),
        ui_port=_int_env("AMBIENT_UI_PORT", 8765),
        events_dir=Path(os.getenv("AMBIENT_EVENTS_DIR") or "~/Library/Logs/ambient-home/events").expanduser(),
        event_retention_days=_int_env("AMBIENT_EVENT_RETENTION_DAYS", 14),
        heartbeat_s=_float_env("AMBIENT_HEARTBEAT_S", 300.0),
    )
