"""Daemon readiness and watchdog tests."""

import httpx

from ambient_home.daemon import watch_daemon, wait_for_daemon, fetch_daemon_state


def _client(states: list[object]) -> httpx.AsyncClient:
    calls = iter(states)

    def handler(request: httpx.Request) -> httpx.Response:
        state = next(calls)
        if state is None:
            raise httpx.ConnectError("refused", request=request)
        if isinstance(state, int):
            return httpx.Response(state)
        return httpx.Response(200, json={"state": state, "robot_name": "reachy"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_daemon_state_handles_errors() -> None:
    async with _client([None, 503, "running"]) as client:
        assert await fetch_daemon_state(client, "http://d/status") is None
        assert await fetch_daemon_state(client, "http://d/status") is None
        assert await fetch_daemon_state(client, "http://d/status") == "running"


async def test_wait_for_daemon_polls_until_running() -> None:
    async with _client([None, "starting", "running"]) as client:
        assert await wait_for_daemon("http://d/status", timeout_s=10.0, poll_s=0.0, client=client)


async def test_wait_for_daemon_times_out() -> None:
    async with _client([None] * 50) as client:
        assert not await wait_for_daemon("http://d/status", timeout_s=0.0, poll_s=0.0, client=client)


async def test_watch_daemon_reports_loss_after_consecutive_failures() -> None:
    lost: list[str] = []

    async def on_lost(state: str) -> None:
        lost.append(state)

    async with _client(["running", None, "running", None, "error", None]) as client:
        await watch_daemon("http://d/status", poll_s=0.0, max_failures=3, on_lost=on_lost, client=client)

    assert lost == ["unreachable"]
