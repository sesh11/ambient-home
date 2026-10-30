"""Application composition smoke tests."""

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ambient_home.main import build_app
from ambient_home.config import settings_from_env
from ambient_home.connectors.voice.openai_live import OpenAILiveVoiceModel


def test_no_device_app_serves_ui_without_creating_provider_clients(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "ambient_home.main.OpenWakeWordDetector",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("wake model loaded")),
    )
    monkeypatch.setattr(
        OpenAILiveVoiceModel,
        "_get_client",
        lambda self: (_ for _ in ()).throw(AssertionError("OpenAI client created")),
    )
    settings = replace(
        settings_from_env(),
        data_dir=tmp_path,
        devices_file=tmp_path / "missing-devices.toml",
        events_dir=tmp_path / "events",
        openai_api_key=None,
        devin_api_key=None,
        devin_org_id=None,
    )
    app = build_app(settings)
    assert app.gateway.configs == ()
    assert app.conversation.voice_model._client is None
    with TestClient(app.ui) as client:
        assert client.get("/jobs").status_code == 200
        response = client.get("/api/events")
        assert response.status_code == 200
        assert any(event["type"] == "app.started" for event in response.json()["events"])


@pytest.mark.asyncio
async def test_no_device_gateway_and_conversation_idle_and_shutdown_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = replace(
        settings_from_env(),
        data_dir=tmp_path,
        devices_file=tmp_path / "missing-devices.toml",
        events_dir=tmp_path / "events",
        openai_api_key=None,
    )
    app = build_app(settings)
    gateway_task = asyncio.create_task(app.gateway.run())
    conversation_task = asyncio.create_task(app.conversation.run())
    await asyncio.sleep(0.02)

    assert app.gateway.status() == []
    assert not gateway_task.done()
    assert not conversation_task.done()

    await app.conversation.shutdown()
    await app.gateway.stop()
    await asyncio.gather(gateway_task, conversation_task)
    assert gateway_task.done()
    assert conversation_task.done()


@pytest.mark.asyncio
async def test_app_run_cleans_up_when_ui_server_returns(tmp_path, monkeypatch) -> None:
    settings = replace(
        settings_from_env(),
        data_dir=tmp_path,
        devices_file=tmp_path / "missing-devices.toml",
        events_dir=tmp_path / "events",
        openai_api_key=None,
        devin_api_key=None,
        devin_org_id=None,
    )
    app = build_app(settings)
    cancellations: set[str] = set()
    stop_calls = 0
    original_gateway_run = app.gateway.run
    original_conversation_run = app.conversation.run
    original_stop = app.gateway.stop

    async def tracked_gateway_run() -> None:
        try:
            await original_gateway_run()
        except asyncio.CancelledError:
            cancellations.add("gateway")
            raise

    async def tracked_conversation_run() -> None:
        try:
            await original_conversation_run()
        except asyncio.CancelledError:
            cancellations.add("conversation")
            raise

    async def tracked_stop() -> None:
        nonlocal stop_calls
        stop_calls += 1
        await original_stop()

    class ReturningServer:
        def __init__(self, _config) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            await asyncio.sleep(0)

    monkeypatch.setattr("ambient_home.main.uvicorn.Server", ReturningServer)
    monkeypatch.setattr(app.gateway, "run", tracked_gateway_run)
    monkeypatch.setattr(app.conversation, "run", tracked_conversation_run)
    monkeypatch.setattr(app.gateway, "stop", tracked_stop)

    await asyncio.wait_for(app.run(), timeout=1)

    assert cancellations == {"gateway", "conversation"}
    assert stop_calls == 1


@pytest.mark.asyncio
async def test_app_run_stops_server_when_critical_task_raises(tmp_path, monkeypatch) -> None:
    settings = replace(
        settings_from_env(),
        data_dir=tmp_path,
        devices_file=tmp_path / "missing-devices.toml",
        events_dir=tmp_path / "events",
        openai_api_key=None,
        devin_api_key=None,
        devin_org_id=None,
    )
    app = build_app(settings)
    servers = []

    class BlockingServer:
        def __init__(self, _config) -> None:
            self.should_exit = False
            self.cancelled = False
            servers.append(self)

        async def serve(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def failing_poller(_interval_s: float) -> None:
        raise RuntimeError("poller failed")

    monkeypatch.setattr("ambient_home.main.uvicorn.Server", BlockingServer)
    monkeypatch.setattr(app.job_service, "run_poller", failing_poller)

    with pytest.raises(RuntimeError, match="poller failed"):
        await asyncio.wait_for(app.run(), timeout=1)

    assert servers[0].should_exit
    assert servers[0].cancelled
