"""Vision tool backed by OpenAI Responses."""

import base64
import logging
from typing import Any

from openai import AsyncOpenAI
from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies
from openai.types.responses.response_input_item_param import Message
from openai.types.responses.response_input_text_param import ResponseInputTextParam
from openai.types.responses.response_input_image_param import ResponseInputImageParam

from ambient_home.runtime import get_settings


logger = logging.getLogger(__name__)


class Look(Tool):
    """Answer a question about a current camera frame."""

    name = "look"
    description = "Use the camera and vision model to answer a question about what is visible."
    parameters_schema = {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Capture a frame and ask the configured vision model."""
        question = str(kwargs.get("question", "")).strip()
        if not question:
            return {"error": "question must be a non-empty string"}
        if not deps.camera_enabled:
            return {"error": "Camera is disabled"}
        try:
            jpeg_bytes = deps.reachy_mini.media.get_frame_jpeg()
            if not jpeg_bytes:
                return {"error": "No frame available"}
            settings = get_settings()
            client = AsyncOpenAI(api_key=settings.openai_api_key or "DUMMY")
            response = await client.responses.create(
                model=settings.vision_model,
                input=[
                    Message(
                        role="user",
                        content=[
                            ResponseInputTextParam(type="input_text", text=question),
                            ResponseInputImageParam(
                                type="input_image",
                                image_url=f"data:image/jpeg;base64,{base64.b64encode(jpeg_bytes).decode('ascii')}",
                                detail="auto",
                            ),
                        ],
                    )
                ],
            )
            return {"description": response.output_text}
        except Exception as exc:
            logger.exception("Vision tool failed")
            return {"error": str(exc)}
