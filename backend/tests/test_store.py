from __future__ import annotations

import pytest

from karaoke_studio.db import RevisionConflict, Store, now_iso
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import JobRecord, JobState, ProjectRecord, ProjectState


def _project() -> ProjectRecord:
    timestamp = now_iso()
    return ProjectRecord(
        id="proj_test",
        title="Kiểm thử",
        state=ProjectState.IMPORTED,
        created_at=timestamp,
        updated_at=timestamp,
        source_name="input.mp4",
        lrc_name="lyrics.lrc",
        source_sha256="a" * 64,
        duration_us=2_000_000,
        width=1920,
        height=1080,
        fps="60/1",
        has_audio=True,
    )


def test_sqlite_project_and_optimistic_timeline_revision(test_settings) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    timeline = parse_lrc("[00:00.10]Xin chào", 2_000_000)
    store.save_timeline("proj_test", timeline)
    saved = store.save_timeline("proj_test", timeline, expected_revision=1)
    assert saved.revision == 2
    assert (store.project_dir("proj_test") / "history" / "timeline-r00001.json").exists()
    with pytest.raises(RevisionConflict):
        store.save_timeline("proj_test", saved, expected_revision=1)


def test_sqlite_indexes_exist(test_settings) -> None:
    store = Store(test_settings)
    with store.connect() as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index' AND name LIKE 'idx_%'"
            )
        }
    assert "idx_projects_updated_at" in indexes
    assert "idx_jobs_project_created" in indexes


def test_delete_project_cascades_its_jobs_only(test_settings) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    timestamp = now_iso()
    store.add_job(
        JobRecord(
            id="job_delete_test",
            project_id="proj_test",
            kind="process",
            state=JobState.CANCELLED,
            progress=0,
            message="test",
            created_at=timestamp,
            updated_at=timestamp,
            options={},
        )
    )

    assert store.job_ids_for_project("proj_test") == ["job_delete_test"]
    assert store.delete_project("proj_test") is True
    assert store.get_project("proj_test") is None
    assert store.get_job("job_delete_test") is None
    assert store.delete_project("proj_test") is False
