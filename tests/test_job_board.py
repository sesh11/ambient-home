"""Job board persistence tests."""

import json

from ambient_home.jobs.board import Job, JobBoard, JobStatus


def test_job_board_persists_and_tracks_announcements(tmp_path) -> None:
    path = tmp_path / "jobs.json"
    board = JobBoard(path)
    job = Job(request="Fix the release script")
    board.add(job)
    job.status = JobStatus.finished
    board.update(job)

    restored = JobBoard(path)
    assert restored.get(job.id) is not None
    assert restored.unannounced()[0].id == job.id
    restored.mark_announced(job)
    assert restored.unannounced() == []


def test_corrupt_job_board_starts_empty(tmp_path) -> None:
    path = tmp_path / "jobs.json"
    path.write_text("{not json")

    assert JobBoard(path).recent() == []


def test_job_board_replaces_file_atomically(tmp_path) -> None:
    path = tmp_path / "jobs.json"
    board = JobBoard(path)
    board.add(Job(request="Write tests"))

    assert isinstance(json.loads(path.read_text()), list)
