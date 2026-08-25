from __future__ import annotations

import argparse
import os
import signal

from .db import Store
from .models import JobState, ProjectState
from .pipeline import JobCancelled, prepare_final_audio_job, process_project, render_project
from .settings import Settings

_cancelled = False


def _handle_signal(_signum, _frame) -> None:
    global _cancelled
    _cancelled = True
    raise JobCancelled("Job đã bị dừng.")


def _windows_break_signal() -> int | None:
    if os.name != "nt":
        return None
    return getattr(signal, "SIGBREAK", None)


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    windows_break = _windows_break_signal()
    if windows_break is not None:
        signal.signal(windows_break, _handle_signal)


def execute(job_id: str, settings: Settings) -> None:
    _install_signal_handlers()
    store = Store(settings)
    job = store.get_job(job_id)
    if not job:
        raise KeyError(job_id)
    if job.state == JobState.CANCELLED:
        return
    try:
        store.update_job(
            job_id, state=JobState.RUNNING, pid=os.getpid(), message="Worker đã bắt đầu."
        )
        if job.kind == "process":
            process_project(job.id, job.project_id, job.options, settings)
        elif job.kind == "final_audio":
            prepare_final_audio_job(job.id, job.project_id, job.options, settings)
        else:
            render_project(job.id, job.project_id, job.options, settings)
        current = store.get_job(job_id)
        if current and current.state == JobState.CANCELLED:
            return
        store.update_job(job_id, state=JobState.COMPLETE, progress=1.0)
    except JobCancelled as exc:
        store.update_job(job_id, state=JobState.CANCELLED, message=str(exc), error=str(exc))
    except Exception as exc:
        current = store.get_job(job_id)
        if current and current.state == JobState.CANCELLED:
            return
        store.update_job(job_id, state=JobState.FAILED, message="Tác vụ thất bại.", error=str(exc))
        store.update_project(job.project_id, state=ProjectState.FAILED, error=str(exc))
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    settings = Settings.load()
    execute(args.job_id, settings)


if __name__ == "__main__":
    main()
