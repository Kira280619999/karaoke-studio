from __future__ import annotations

import os

import pytest

from karaoke_studio.db import Store, now_iso
from karaoke_studio.jobs import JobManager
from karaoke_studio.models import JobRecord, JobState, ProjectRecord, ProjectState


def _project() -> ProjectRecord:
    timestamp = now_iso()
    return ProjectRecord(
        id="proj_jobs",
        title="Job test",
        state=ProjectState.IMPORTED,
        created_at=timestamp,
        updated_at=timestamp,
        source_name="input.mp4",
        lrc_name="lyrics.lrc",
        source_sha256="b" * 64,
        duration_us=1_000_000,
        width=640,
        height=360,
        fps="30/1",
        has_audio=True,
    )


def test_duplicate_start_reuses_live_job_and_blocks_other_kind(test_settings) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    timestamp = now_iso()
    active = JobRecord(
        id="job_live",
        project_id="proj_jobs",
        kind="process",
        state=JobState.RUNNING,
        progress=0.4,
        message="running",
        created_at=timestamp,
        updated_at=timestamp,
        pid=os.getpid(),
    )
    store.add_job(active)
    manager = JobManager(store, test_settings)
    assert manager.start("proj_jobs", "process", {}).id == "job_live"
    with pytest.raises(RuntimeError):
        manager.start("proj_jobs", "render", {})


def test_pending_job_without_pid_can_be_cancelled(test_settings) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    timestamp = now_iso()
    store.add_job(
        JobRecord(
            id="job_pending",
            project_id="proj_jobs",
            kind="process",
            state=JobState.PENDING,
            progress=0,
            message="pending",
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    cancelled = JobManager(store, test_settings).cancel("job_pending")
    assert cancelled.state == JobState.CANCELLED
