"""Provider-neutral core tools."""

from datetime import datetime
from collections.abc import Callable

from ambient_device_sdk import ToolSpec
from ambient_home.costs import CostTracker, spoken_summary
from ambient_home.jobs.service import JobService


def build_core_tools(
    job_service: JobService,
    cost_tracker: CostTracker,
    request_close: Callable[[], None],
) -> list[ToolSpec]:
    """Build the core tools with their exact model-facing schemas."""

    async def dispatch_job(arguments: dict[str, object]) -> dict[str, object]:
        request = str(arguments.get("request", "")).strip()
        if not request:
            return {"error": "request must be a non-empty string"}
        repository_value = arguments.get("repository")
        repository = str(repository_value).strip() if repository_value is not None else None
        job = await job_service.dispatch(request, repository or None)
        return {
            "job_id": job.id,
            "status": "dispatched",
            "message": "Started. I'll tell you when the PR is up.",
        }

    async def job_status(arguments: dict[str, object]) -> dict[str, object]:
        job_id = str(arguments["job_id"]).strip() if arguments.get("job_id") is not None else None
        jobs = job_service.status_summary()
        if job_id:
            jobs = [job for job in jobs if job["id"] == job_id]
        return {"jobs": jobs}

    async def answer_job(arguments: dict[str, object]) -> dict[str, object]:
        job_id = str(arguments.get("job_id", "")).strip()
        answer = str(arguments.get("answer", "")).strip()
        if not job_id or not answer:
            return {"error": "job_id and answer are required"}
        job = await job_service.answer(job_id, answer)
        if job is None:
            return {"error": "unknown job"}
        return {"job_id": job.id, "status": job.status.value}

    async def spend_report(_: dict[str, object]) -> dict[str, object]:
        now = datetime.now().astimezone()
        report = cost_tracker.report(now)
        report["speech"] = spoken_summary(cost_tracker, now)
        return report

    async def end_conversation(_: dict[str, object]) -> dict[str, object]:
        request_close()
        return {"status": "close_requested", "reason": "user_dismissed"}

    return [
        ToolSpec(
            "dispatch_job",
            "Hand off a software engineering task (code change, bug fix, new feature, PR) to the remote engineering agent. Returns immediately.",
            {
                "type": "object",
                "properties": {
                    "request": {"type": "string"},
                    "repository": {"type": "string", "description": "GitHub owner/name, if known"},
                },
                "required": ["request"],
            },
            dispatch_job,
        ),
        ToolSpec(
            "job_status",
            "Check the status of a remote engineering job or all recent jobs.",
            {"type": "object", "properties": {"job_id": {"type": "string"}}},
            job_status,
        ),
        ToolSpec(
            "answer_job",
            "Answer a question from a blocked remote engineering job.",
            {
                "type": "object",
                "properties": {"job_id": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["job_id", "answer"],
            },
            answer_job,
        ),
        ToolSpec(
            "spend_report",
            "Report what the assistant has spent today: Live audio minutes, worker ACUs, and dollar estimates.",
            {"type": "object", "properties": {}},
            spend_report,
        ),
        ToolSpec(
            "end_conversation",
            "Use when the user is done: 'that's all', 'go to sleep', or 'thanks, that's it'.",
            {"type": "object", "properties": {}},
            end_conversation,
        ),
    ]
