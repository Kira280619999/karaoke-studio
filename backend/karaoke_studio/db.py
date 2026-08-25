from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .models import JobRecord, JobState, ProjectRecord, ProjectState, TimelineV1
from .settings import Settings


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RevisionConflict(RuntimeError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure()
        self.db_path = settings.data_dir / "karaoke-studio.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    lrc_name TEXT NOT NULL,
                    background_name TEXT,
                    background_mode TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    duration_us INTEGER NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    fps TEXT NOT NULL,
                    has_audio INTEGER NOT NULL,
                    selected_instrumental TEXT,
                    selected_instrumental_sha256 TEXT,
                    instrumental_confirmed INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )
                """
            )
            project_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(projects)")
            }
            if "selected_instrumental_sha256" not in project_columns:
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN selected_instrumental_sha256 TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    progress REAL NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    pid INTEGER,
                    error TEXT,
                    options_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_project_created ON jobs(project_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_active_state ON jobs(state) WHERE state IN ('PENDING', 'RUNNING')"
            )
            connection.execute("PRAGMA optimize")

    def project_dir(self, project_id: str) -> Path:
        return self.settings.data_dir / "projects" / project_id

    def timeline_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "timeline.json"

    def save_timeline(
        self, project_id: str, timeline: TimelineV1, expected_revision: int | None = None
    ) -> TimelineV1:
        path = self.timeline_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if path.exists():
                current = self._read_timeline(path)
                if expected_revision is not None and current.revision != expected_revision:
                    raise RevisionConflict(
                        f"Timeline đang ở revision {current.revision}, không phải {expected_revision}."
                    )
                timeline.revision = current.revision + 1
            history = path.parent / "history"
            history.mkdir(exist_ok=True)
            if path.exists():
                previous = self._read_timeline(path)
                (history / f"timeline-r{previous.revision:05d}.json").write_text(
                    previous.model_dump_json(indent=2), encoding="utf-8"
                )
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(timeline.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(path)
        return timeline

    def load_timeline(self, project_id: str) -> TimelineV1:
        return self._read_timeline(self.timeline_path(project_id))

    @staticmethod
    def _read_timeline(path: Path) -> TimelineV1:
        timeline = TimelineV1.model_validate_json(path.read_text(encoding="utf-8"))
        # TimelineV1.1 is additive. Old projects remain readable and are saved
        # as 1.1 the next time analysis or an editor revision is committed.
        if timeline.schema_version == "1.0":
            timeline.schema_version = "1.1"
        return timeline

    def add_project(self, project: ProjectRecord) -> None:
        values = project.model_dump()
        values["state"] = project.state.value
        values["has_audio"] = int(project.has_audio)
        values["instrumental_confirmed"] = int(project.instrumental_confirmed)
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    id, title, artist, state, created_at, updated_at, source_name, lrc_name,
                    background_name, background_mode, source_sha256, duration_us, width, height,
                    fps, has_audio, selected_instrumental, selected_instrumental_sha256,
                    instrumental_confirmed, error
                ) VALUES (
                    :id, :title, :artist, :state, :created_at, :updated_at, :source_name, :lrc_name,
                    :background_name, :background_mode, :source_sha256, :duration_us, :width, :height,
                    :fps, :has_audio, :selected_instrumental, :selected_instrumental_sha256,
                    :instrumental_confirmed, :error
                )
                """,
                values,
            )

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._project_from_row(row) if row else None

    def list_projects(self) -> list[ProjectRecord]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [self._project_from_row(row) for row in rows]

    def update_project(self, project_id: str, **fields: object) -> ProjectRecord:
        allowed = {
            "title",
            "artist",
            "state",
            "duration_us",
            "selected_instrumental",
            "selected_instrumental_sha256",
            "instrumental_confirmed",
            "error",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Trường project update không hợp lệ.")
        normalized: dict[str, object] = {}
        for key, value in fields.items():
            if isinstance(value, ProjectState):
                value = value.value
            if key == "instrumental_confirmed":
                value = int(bool(value))
            normalized[key] = value
        normalized["updated_at"] = now_iso()
        clause = ", ".join(f"{key} = :{key}" for key in normalized)
        normalized["id"] = project_id
        with self._lock, self.connect() as connection:
            cursor = connection.execute(f"UPDATE projects SET {clause} WHERE id = :id", normalized)
            if cursor.rowcount != 1:
                raise KeyError(project_id)
        project = self.get_project(project_id)
        assert project is not None
        return project

    def job_ids_for_project(self, project_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE project_id = ?", (project_id,)
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def delete_project(self, project_id: str) -> bool:
        with self._lock, self.connect() as connection:
            cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return cursor.rowcount == 1

    def add_job(self, job: JobRecord) -> None:
        values = job.model_dump()
        values["state"] = job.state.value
        values["options_json"] = json.dumps(values.pop("options"), ensure_ascii=False)
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, project_id, kind, state, progress, message, created_at, updated_at,
                    pid, error, options_json
                ) VALUES (
                    :id, :project_id, :kind, :state, :progress, :message, :created_at, :updated_at,
                    :pid, :error, :options_json
                )
                """,
                values,
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row else None

    def get_active_job(self, project_id: str) -> JobRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE project_id = ? AND state IN ('PENDING', 'RUNNING')
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        return self._job_from_row(row) if row else None

    def update_job(self, job_id: str, **fields: object) -> JobRecord:
        allowed = {"state", "progress", "message", "pid", "error"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Trường job update không hợp lệ.")
        normalized: dict[str, object] = {}
        for key, value in fields.items():
            normalized[key] = value.value if isinstance(value, JobState) else value
        normalized["updated_at"] = now_iso()
        normalized["id"] = job_id
        clause = ", ".join(f"{key} = :{key}" for key in normalized if key != "id")
        with self._lock, self.connect() as connection:
            cursor = connection.execute(f"UPDATE jobs SET {clause} WHERE id = :id", normalized)
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        job = self.get_job(job_id)
        assert job is not None
        return job

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> ProjectRecord:
        data = dict(row)
        data["has_audio"] = bool(data["has_audio"])
        data["instrumental_confirmed"] = bool(data["instrumental_confirmed"])
        return ProjectRecord.model_validate(data)

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        data = dict(row)
        data["options"] = json.loads(data.pop("options_json"))
        return JobRecord.model_validate(data)
