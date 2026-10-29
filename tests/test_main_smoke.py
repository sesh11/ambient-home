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
