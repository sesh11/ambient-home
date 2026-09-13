"""Voice tool for answering blocked engineering jobs."""

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

from ambient_home.runtime import get_job_service


class AnswerJob(Tool):
    """Send a decision or answer to a blocked remote job."""

    name = "answer_job"
    description = "Answer a question from a blocked remote engineering job."
    parameters_schema = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "answer": {"type": "string"},
        },
        "required": ["job_id", "answer"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: object) -> dict[str, object]:
        """Answer a job and return its resulting status."""
        job_id = str(kwargs.get("job_id", "")).strip()
        answer = str(kwargs.get("answer", "")).strip()
        if not job_id or not answer:
            return {"error": "job_id and answer are required"}
        job = await get_job_service().answer(job_id, answer)
        if job is None:
            return {"error": "unknown job"}
        return {"job_id": job.id, "status": job.status.value}
