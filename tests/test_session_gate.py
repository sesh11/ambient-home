"""Session gate tests."""

from ambient_home.session_gate import GateState, CloseReason, SessionGate


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def make_gate(clock: Clock, used: float = 0.0) -> SessionGate:
    return SessionGate(
        idle_timeout_s=60,
        max_session_s=600,
        daily_budget_s=3600,
        seconds_used_today=used,
        clock=clock,
    )


def test_idle_ignores_assistant_activity() -> None:
    clock = Clock()
    gate = make_gate(clock)
    gate.wake()
    clock.value = 59
    gate.note_user_speech()
    clock.value = 118
    assert gate.close_reason_due() is None
    clock.value = 119
    assert gate.close_reason_due() is CloseReason.IDLE


def test_max_session_wins_over_idle() -> None:
    clock = Clock()
    gate = make_gate(clock)
    gate.wake()
    clock.value = 600
    assert gate.close_reason_due() is CloseReason.MAX_SESSION


def test_daily_budget_closes_and_blocks_next_wake() -> None:
    clock = Clock()
    gate = make_gate(clock, used=3550)
    gate.wake()
    clock.value = 50
    assert gate.close_reason_due() is CloseReason.DAILY_BUDGET
    gate.close()
    assert gate.state is GateState.ASLEEP
    assert not gate.can_wake()


def test_requested_close_has_priority_and_accumulates() -> None:
    clock = Clock()
    gate = make_gate(clock)
    gate.wake()
    gate.request_close(CloseReason.USER_DISMISSED)
    assert gate.close_reason_due() is CloseReason.USER_DISMISSED
    clock.value = 7
    assert gate.close() == 7
    assert gate.seconds_used_today == 7
