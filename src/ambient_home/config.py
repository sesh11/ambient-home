"""Runtime configuration for ambient-home."""

import os
from pathlib import Path
from dataclasses import field, dataclass

from ambient_home.incidents.base import parse_duration


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
    devin_org_id: str | None = None
    devin_api_base_url: str = "https://api.devin.ai"
    devin_max_acu: int = 5
    live_usd_per_minute: float = 0.3
    acu_usd: float = 2.25
    cost_history_days: int = 7
    job_poll_s: float = 20.0
    ui_host: str = "127.0.0.1"
    ui_port: int = 8765
    mic_autorecover: bool = True
    daemon_status_url: str = "http://127.0.0.1:8000/api/daemon/status"
    daemon_wait_s: float = 60.0
    daemon_watchdog_s: float = 5.0
    daemon_watchdog_failures: int = 3
    events_dir: Path = field(default_factory=lambda: Path("~/Library/Logs/ambient-home/events").expanduser())
    event_retention_days: int = 14
    heartbeat_s: float = 300.0
    detector: str = "rules"
    incident_rules_path: Path | None = None
    incident_lookback_s: float = 86400.0
    factory_report: bool = False
    github_token: str | None = None
    factory_repo: str = "sesh11/ambient-home"
    factory_max_new_issues_per_day: int = 5
    factory_dispatch: bool = False
    factory_dispatch_interval_s: float = 3600.0
    factory_outpost: str = "my-mac"


def _duration_env(name: str, default_s: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default_s
    try:
        return parse_duration(value.strip()).total_seconds()
    except ValueError as exc:
        raise ValueError(f"{name} must be a duration such as 30m, 1h or 6h") from exc


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
        data_dir=Path(os.getenv("AMBIENT_DATA_DIR", "~/.ambient-home")).expanduser(),
        transcript_finalize_s=_float_env("AMBIENT_TRANSCRIPT_FINALIZE_S", 1.0),
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
        mic_autorecover=_bool_env("AMBIENT_MIC_AUTORECOVER", True),
        daemon_status_url=os.getenv("AMBIENT_DAEMON_STATUS_URL", "http://127.0.0.1:8000/api/daemon/status"),
        daemon_wait_s=_float_env("AMBIENT_DAEMON_WAIT_S", 60.0),
        daemon_watchdog_s=_float_env("AMBIENT_DAEMON_WATCHDOG_S", 5.0),
        daemon_watchdog_failures=_int_env("AMBIENT_DAEMON_WATCHDOG_FAILURES", 3),
        events_dir=Path(os.getenv("AMBIENT_EVENTS_DIR") or "~/Library/Logs/ambient-home/events").expanduser(),
        event_retention_days=_int_env("AMBIENT_EVENT_RETENTION_DAYS", 14),
        heartbeat_s=_float_env("AMBIENT_HEARTBEAT_S", 300.0),
        detector=os.getenv("AMBIENT_DETECTOR", "rules"),
        incident_rules_path=(
            Path(os.environ["AMBIENT_INCIDENT_RULES"]).expanduser() if os.getenv("AMBIENT_INCIDENT_RULES") else None
        ),
        incident_lookback_s=_float_env("AMBIENT_INCIDENT_LOOKBACK_S", 86400.0),
        factory_report=_bool_env("AMBIENT_FACTORY_REPORT", False),
        github_token=os.getenv("AMBIENT_GITHUB_TOKEN") or None,
        factory_repo=os.getenv("AMBIENT_FACTORY_REPO") or "sesh11/ambient-home",
        factory_max_new_issues_per_day=_int_env("AMBIENT_FACTORY_MAX_NEW_ISSUES_PER_DAY", 5),
        factory_dispatch=_bool_env("AMBIENT_FACTORY_DISPATCH", False),
        factory_dispatch_interval_s=_duration_env("AMBIENT_FACTORY_DISPATCH_INTERVAL", 3600.0),
        factory_outpost=os.getenv("AMBIENT_FACTORY_OUTPOST") or "my-mac",
    )
