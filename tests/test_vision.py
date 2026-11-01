"""Lazy OpenAI Responses vision connector tests."""

import base64
from types import SimpleNamespace

import pytest

from ambient_home.connectors.vision.openai_vision import OpenAIVision


@pytest.mark.asyncio
async def test_vision_client_is_lazy_and_uses_responses_image_shape() -> None:
    calls = []

    class Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text="A chair.")

    class Client:
        responses = Responses()

    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return Client()

    vision = OpenAIVision("key", "vision-model", client_factory=factory)
    assert created == []
    assert await vision(b"jpeg", "What is in view?") == "A chair."
    assert created == [{"api_key": "key"}]
    request = calls[0]
    assert request["model"] == "vision-model"
    content = request["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "What is in view?"}
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "auto"
    assert content[1]["image_url"] == f"data:image/jpeg;base64,{base64.b64encode(b'jpeg').decode()}"
