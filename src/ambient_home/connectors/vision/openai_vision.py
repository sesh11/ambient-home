"""OpenAI Responses vision connector."""

import base64
from typing import Any
from collections.abc import Callable


class OpenAIVision:
    """Ask a vision model about one supplied JPEG frame."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        """Configure the lazy Responses client."""
        self.api_key = api_key
        self.model = model
        self._client_factory = client_factory
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            factory = self._client_factory
            if factory is None:
                from openai import AsyncOpenAI

                factory = AsyncOpenAI
            self._client = factory(api_key=self.api_key)
        return self._client

    async def __call__(self, jpeg: bytes, question: str) -> str:
        """Ask the model about a JPEG image."""
        data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
        response = await self._get_client().responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": question},
                        {"type": "input_image", "image_url": data_url, "detail": "auto"},
                    ],
                }
            ],
        )
        return str(response.output_text)
