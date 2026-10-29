"""Application composition smoke tests."""

from dataclasses import replace

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
