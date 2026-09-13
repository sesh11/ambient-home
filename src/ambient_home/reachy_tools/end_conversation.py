"""End the current wake-gated conversation."""

from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

from ambient_home.session_gate import CloseReason


class EndConversation(Tool):
    """Request that the current voice session close."""

    name = "end_conversation"
    description = "Use when the user is done: 'that's all', 'go to sleep', or 'thanks, that's it'."
    parameters_schema = {"type": "object", "properties": {}}
    needs_response = False

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Request a session close while leaving wake-word listening active."""
        if deps.go_to_sleep is None:
            return {"error": "Session gate is unavailable"}
        result = deps.go_to_sleep()
        return {"status": "going_to_sleep", **result, "reason": CloseReason.USER_DISMISSED.value}
