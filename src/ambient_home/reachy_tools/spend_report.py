"""Voice tool for reporting local running costs."""

from datetime import datetime

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

from ambient_home.costs import spoken_summary
from ambient_home.runtime import get_cost_tracker


class SpendReport(Tool):
    """Report Live audio minutes and worker ACUs with dollar estimates."""

    name = "spend_report"
    description = "Report what the assistant has spent today: Live audio minutes, worker ACUs, and dollar estimates."
    parameters_schema = {"type": "object", "properties": {}}

    async def __call__(self, deps: ToolDependencies, **kwargs: object) -> dict[str, object]:
        """Return the cost report plus a sentence ready to be spoken."""
        tracker = get_cost_tracker()
        now = datetime.now().astimezone()
        report = tracker.report(now)
        report["speech"] = spoken_summary(tracker, now)
        return report
