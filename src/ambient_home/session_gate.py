"""Pure session and budget gate state machine."""

import time
from enum import Enum
from collections.abc import Callable


class GateState(Enum):
    """Session gate states."""

    ASLEEP = "asleep"
    LIVE = "live"


class CloseReason(Enum):
    """Reasons a Live session can close."""

    IDLE = "idle"
    MAX_SESSION = "max_session"
    DAILY_BUDGET = "daily_budget"
    USER_DISMISSED = "user_dismissed"
    SERVER_CLOSED = "server_closed"
    ERROR = "error"


class SessionGate:
    """Track wake state, idle timeout, and daily budget."""

    def __init__(
        self,
        *,
        idle_timeout_s: float,
        max_session_s: float,
        daily_budget_s: float,
        seconds_used_today: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an asleep gate with injected monotonic time."""
        self.idle_timeout_s = idle_timeout_s
        self.max_session_s = max_session_s
        self.daily_budget_s = daily_budget_s
        self._clock = clock
        self._state = GateState.ASLEEP
        self._seconds_used_today = seconds_used_today
        self._session_started_at: float | None = None
        self._last_user_speech_at: float | None = None
        self._requested_reason: CloseReason | None = None
        self._last_close_reason: CloseReason | None = None

    @property
    def state(self) -> GateState:
        """Return current gate state."""
        return self._state

    def can_wake(self) -> bool:
        """Return whether a new session is allowed."""
        return self._state is GateState.ASLEEP and self._seconds_used_today < self.daily_budget_s

    def wake(self) -> None:
        """Enter Live state and reset session clocks."""
        if not self.can_wake():
            raise RuntimeError("session gate cannot wake")
        now = self._clock()
        self._state = GateState.LIVE
        self._session_started_at = now
        self._last_user_speech_at = now
        self._requested_reason = None

    def note_user_speech(self) -> None:
        """Reset idle timeout from user speech only."""
        if self._state is GateState.LIVE:
            self._last_user_speech_at = self._clock()

    def request_close(self, reason: CloseReason) -> None:
        """Request session closure with explicit priority."""
        self._requested_reason = reason

    def close_reason_due(self) -> CloseReason | None:
        """Return the highest-priority reason currently due."""
        if self._state is GateState.ASLEEP or self._session_started_at is None:
            return None
        if self._requested_reason is not None:
            return self._requested_reason
        elapsed = self.session_elapsed_s
        if elapsed >= self.max_session_s:
            return CloseReason.MAX_SESSION
        if self._seconds_used_today + elapsed >= self.daily_budget_s:
            return CloseReason.DAILY_BUDGET
        if self._last_user_speech_at is not None and self._clock() - self._last_user_speech_at >= self.idle_timeout_s:
            return CloseReason.IDLE
        return None

    def close(self, reason: CloseReason | None = None) -> float:
        """Close the current session and return elapsed seconds."""
        if self._state is GateState.ASLEEP or self._session_started_at is None:
            return 0.0
        elapsed = self.session_elapsed_s
        self._seconds_used_today += elapsed
        self._last_close_reason = reason or self.close_reason_due() or CloseReason.SERVER_CLOSED
        self._state = GateState.ASLEEP
        self._session_started_at = None
        self._last_user_speech_at = None
        self._requested_reason = None
        return elapsed

    @property
    def session_elapsed_s(self) -> float:
        """Return current session duration."""
        return 0.0 if self._session_started_at is None else self._clock() - self._session_started_at

    @property
    def seconds_used_today(self) -> float:
        """Return accumulated daily duration."""
        return self._seconds_used_today

    @property
    def seconds_remaining_today(self) -> float:
        """Return remaining daily budget."""
        return max(0.0, self.daily_budget_s - self._seconds_used_today)

    @property
    def last_close_reason(self) -> CloseReason | None:
        """Return the most recent close reason."""
        return self._last_close_reason
