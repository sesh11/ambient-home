"""Voice tool for dispatching remote engineering work."""

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

from ambient_home.runtime import get_job_service
from ambient_home.jobs.board import JobStatus


class DispatchJob(Tool):
    """Hand off a software engineering task to a remote engineering agent."""

    name = "dispatch_job"
    description = (
        "Hand off a software engineering task (code change, bug fix, new feature, PR) "
        "to the remote engineering agent. Returns immediately."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "request": {"type": "string"},
            "repository": {"type": "string", "description": "GitHub owner/name, if known"},
        },
        "required": ["request"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: object) -> dict[str, object]:
        """Create a queued job and return immediately."""
        request = str(kwargs.get("request", "")).strip()
        if not request:
            return {"error": "request must be a non-empty string"}
        repository_value = kwargs.get("repository")
        repository = str(repository_value).strip() if repository_value is not None else None
        job = await get_job_service().dispatch(request, repository or None)
        if job.status is JobStatus.failed:
            return {"job_id": job.id, "status": "refused", "message": job.error or "Dispatch refused."}
        return {
            "job_id": job.id,
            "status": "dispatched",
            "message": "Started. I'll tell you when the PR is up.",
        }
