"""Voice tool for checking background engineering jobs."""

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

from ambient_home.runtime import get_job_service


class JobStatusTool(Tool):
    """Report the state of remote engineering jobs."""

    name = "job_status"
    description = "Check the status of a remote engineering job or all recent jobs."
    parameters_schema = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: object) -> dict[str, object]:
        """Return job summaries, optionally filtered by identifier."""
        job_id = str(kwargs["job_id"]).strip() if kwargs.get("job_id") is not None else None
        jobs = get_job_service().status_summary()
        if job_id:
            jobs = [job for job in jobs if job["id"] == job_id]
        return {"jobs": jobs}
