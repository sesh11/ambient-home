"""Provider-neutral core tools."""

import asyncio
import logging
from pathlib import Path
from datetime import datetime
from collections.abc import Callable

from ambient_device_sdk import ToolSpec
from ambient_home.costs import CostTracker, spoken_summary
from ambient_home.memory import add_memory_fact, forget_memory_fact
from ambient_home.jobs.service import JobService, RequestNotFiled


logger = logging.getLogger(__name__)


def build_core_tools(
    job_service: JobService,
    cost_tracker: CostTracker,
    request_close: Callable[[], None],
    memory_file: Path,
) -> list[ToolSpec]:
    """Build the core tools with their exact model-facing schemas."""

    async def file_request(arguments: dict[str, object]) -> dict[str, object]:
        request_value = arguments.get("request")
        if not isinstance(request_value, str) or not request_value.strip():
            return {
                "error": (
                    "request must be a non-empty string. Tell the user. "
                    "Only start a coding session directly if they ask you to."
                )
            }
        repository_value = arguments.get("repository")
        repository = str(repository_value).strip() if repository_value is not None else None
        try:
            job = await job_service.file_request(request_value.strip(), repository or None)
        except RequestNotFiled as exc:
            messages = {
                "not_configured": "Garage is not set up, so nothing was filed.",
                "not_covered": "Garage doesn't cover that repository, so nothing was filed.",
                "declined": "Garage did not file it (request filing is off or today's limit is reached).",
                "failed": "Garage could not file the request.",
            }
            return {
                "error": (
                    f"{messages[exc.reason]} Tell the user. Only start a coding session directly if they ask you to."
                )
            }
        return {
            "job_id": job.id,
            "status": "filed",
            "issue_number": job.issue_number,
            "issue_url": job.issue_url,
            "message": (
                f"Filed as issue #{job.issue_number}. Garage will start on it and I'll tell you when the PR is up."
            ),
        }

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
        if job.worker == "garage" and job.error:
            return {"error": job.error}
        return {"job_id": job.id, "status": job.status.value}

    async def spend_report(_: dict[str, object]) -> dict[str, object]:
        now = datetime.now().astimezone()
        report = cost_tracker.report(now)
        report["speech"] = spoken_summary(cost_tracker, now)
        return report

    async def end_conversation(_: dict[str, object]) -> dict[str, object]:
        request_close()
        return {"status": "close_requested", "reason": "user_dismissed"}

    async def remember(arguments: dict[str, object]) -> dict[str, object]:
        fact = arguments.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            logger.warning("remember: empty fact")
            return {"error": "fact must be a non-empty string"}

        stored = await asyncio.to_thread(add_memory_fact, memory_file, fact)
        if stored is None:
            return {"error": "fact was empty or invalid; nothing was saved"}

        logger.info("Tool call: remember memory_id=%s", stored.id)
        return {"saved": stored.text, "memory_id": stored.id}

    async def forget(arguments: dict[str, object]) -> dict[str, object]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            logger.warning("forget: empty query")
            return {"error": "query must be a non-empty string"}

        result = await asyncio.to_thread(forget_memory_fact, memory_file, query=query)
        if result.removed is None:
            logger.info("Tool call: forget no_match")
            return {"error": f'no memory matched "{query}"; nothing was removed'}

        response: dict[str, object] = {"removed": result.removed.text, "memory_id": result.removed.id}
        if len(result.candidates) > 1:
            response["other_matches"] = [fact.text for fact in result.candidates[1:]]
        logger.info("Tool call: forget memory_id=%s other_matches=%s", result.removed.id, len(result.candidates) - 1)
        return response

    async def idle_do_nothing(arguments: dict[str, object]) -> dict[str, object]:
        reason = arguments.get("reason", "idle turn")
        logger.info("Tool call: idle_do_nothing")
        return {"status": "idle", "reason": reason}

    return [
        ToolSpec(
            "file_request",
            (
                "Default for ALL software engineering work (code changes, bug fixes, features, pull requests): "
                "file it in Garage's work queue as a GitHub issue. Garage starts the coding session. Use this unless "
                "the user explicitly asks you to start a coding session yourself."
            ),
            {
                "type": "object",
                "properties": {
                    "request": {"type": "string"},
                    "repository": {"type": "string", "description": "GitHub owner/name"},
                },
                "required": ["request"],
            },
            file_request,
        ),
        ToolSpec(
            "dispatch_job",
            "Start a remote coding session directly, bypassing Garage. ONLY when the user explicitly asks you to start "
            "or kick off a coding session (for example 'start a Devin session for this'). For all other software "
            "engineering work use file_request.",
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
        ToolSpec(
            "remember",
            (
                "Save ONE short fact about the user to long-term memory so it is available in future sessions. "
                "Use this for stable user information they explicitly shared: name, preferences, hobbies, recurring projects, "
                "important people, or plans. Keep each fact atomic and under one sentence. Do not save sensitive data "
                "(passwords, addresses, payment info, health diagnoses) or fleeting details. Use this silently in the "
                'background; acknowledge naturally without saying "I will remember that".'
            ),
            {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": (
                            "A short, third-person statement about the user, such as "
                            '"Has a dog named Mochi" or "Prefers replies in French". One fact per call.'
                        ),
                    }
                },
                "required": ["fact"],
            },
            remember,
        ),
        ToolSpec(
            "forget",
            (
                "Remove a previously saved fact from long-term memory. Call this when the user asks you to forget something, "
                "or when saved information becomes obsolete. Match by a specific free-text phrase present in the fact."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A short search phrase that should be present in the fact to remove. Matching is case-insensitive."
                        ),
                    }
                },
                "required": ["query"],
            },
            forget,
        ),
        ToolSpec(
            "idle_do_nothing",
            (
                "Use only in response to an idle time update when you intentionally want Reachy to stay still and silent "
                "instead of choosing another idle action."
            ),
            {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for staying idle during this idle turn.",
                    }
                },
                "required": [],
            },
            idle_do_nothing,
        ),
    ]
