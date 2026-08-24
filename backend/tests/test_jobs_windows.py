from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from karaoke_studio import jobs, worker
from karaoke_studio.db import Store, now_iso
from karaoke_studio.jobs import JobManager
from karaoke_studio.models import JobRecord, JobState, ProjectRecord, ProjectState


def _project() -> ProjectRecord:
    timestamp = now_iso()
    return ProjectRecord(
        id="proj_windows_jobs",
        title="Windows job test",
        state=ProjectState.IMPORTED,
        created_at=timestamp,
        updated_at=timestamp,
        source_name="input.mp4",
        lrc_name="lyrics.lrc",
        source_sha256="c" * 64,
        duration_us=1_000_000,
        width=640,
        height=360,
        fps="30/1",
        has_audio=True,
    )


def _add_job(store: Store, *, job_id: str, state: JobState, pid: int | None = None) -> None:
    timestamp = now_iso()
    store.add_job(
        JobRecord(
            id=job_id,
            project_id="proj_windows_jobs",
            kind="process",
            state=state,
            progress=0,
            message=state.value,
            created_at=timestamp,
            updated_at=timestamp,
            pid=pid,
        )
    )


def test_windows_start_creates_a_new_process_group(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    captured: dict[str, object] = {}

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=3210)

    monkeypatch.setattr(jobs, "_is_windows", lambda: True)
    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)

    started = JobManager(store, test_settings).start("proj_windows_jobs", "process", {})

    assert started.pid == 3210
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == jobs._WINDOWS_CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in kwargs
    assert kwargs["env"]["PYTHONUTF8"] == "1"


def test_posix_start_keeps_a_new_session(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    captured: dict[str, object] = {}

    def fake_popen(*args, **kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=3211)

    monkeypatch.setattr(jobs, "_is_windows", lambda: False)
    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)

    JobManager(store, test_settings).start("proj_windows_jobs", "process", {})

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_windows_pid_probe_never_uses_os_kill(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "_is_windows", lambda: True)
    monkeypatch.setattr(jobs, "_windows_pid_alive", lambda pid: pid == 42)

    def fail_os_kill(*_args) -> None:
        pytest.fail("os.kill(pid, 0) must not be used as a Windows liveness probe")

    monkeypatch.setattr(jobs.os, "kill", fail_os_kill)

    assert jobs._pid_alive(42) is True
    assert jobs._pid_alive(43) is False


def test_posix_permission_error_still_means_pid_is_alive(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "_is_windows", lambda: False)

    def deny_signal(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(jobs.os, "kill", deny_signal)
    assert jobs._pid_alive(1234) is True


def test_windows_cancel_marks_state_before_killing_process_tree(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    _add_job(store, job_id="job_windows_running", state=JobState.RUNNING, pid=4242)
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        current = store.get_job("job_windows_running")
        assert current is not None
        assert current.state == JobState.CANCELLED
        # Simulate a worker which read PENDING before cancellation and writes
        # RUNNING while taskkill is being invoked.
        store.update_job("job_windows_running", state=JobState.RUNNING)
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(jobs, "_is_windows", lambda: True)
    monkeypatch.setattr(JobManager, "_pid_alive", staticmethod(lambda _pid: True))
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")

    cancelled = JobManager(store, test_settings).cancel("job_windows_running")

    assert cancelled.state == JobState.CANCELLED
    assert observed["command"] == [
        r"C:\Windows\System32\taskkill.exe",
        "/PID",
        "4242",
        "/T",
        "/F",
    ]
    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["check"] is False
    assert kwargs["creationflags"] == jobs._WINDOWS_CREATE_NO_WINDOW


def test_cancel_records_unconfirmed_tree_termination(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    _add_job(store, job_id="job_windows_stuck", state=JobState.RUNNING, pid=5252)

    monkeypatch.setattr(JobManager, "_pid_alive", staticmethod(lambda _pid: True))

    def fail_tree(_pid: int) -> None:
        raise jobs.ProcessTreeTerminationError("still alive")

    monkeypatch.setattr(jobs, "_terminate_process_tree", fail_tree)

    cancelled = JobManager(store, test_settings).cancel("job_windows_stuck")

    assert cancelled.state == JobState.CANCELLED
    assert cancelled.error == "CANCEL_TERMINATION_FAILED: still alive"
    assert "chưa thể xác nhận" in cancelled.message


def test_worker_does_not_restart_an_already_cancelled_job(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    _add_job(store, job_id="job_cancelled_before_start", state=JobState.CANCELLED)
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda: None)

    def fail_process(*_args, **_kwargs) -> None:
        pytest.fail("a cancelled job must not enter the pipeline")

    monkeypatch.setattr(worker, "process_project", fail_process)

    worker.execute("job_cancelled_before_start", test_settings)

    current = store.get_job("job_cancelled_before_start")
    assert current is not None
    assert current.state == JobState.CANCELLED


def test_worker_never_overwrites_cancellation_with_complete(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    _add_job(store, job_id="job_cancelled_at_finish", state=JobState.PENDING)
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda: None)

    def cancel_during_pipeline(*_args, **_kwargs) -> None:
        Store(test_settings).update_job("job_cancelled_at_finish", state=JobState.CANCELLED)

    monkeypatch.setattr(worker, "process_project", cancel_during_pipeline)

    worker.execute("job_cancelled_at_finish", test_settings)

    current = store.get_job("job_cancelled_at_finish")
    assert current is not None
    assert current.state == JobState.CANCELLED


def test_worker_never_overwrites_cancellation_with_failure(test_settings, monkeypatch) -> None:
    store = Store(test_settings)
    store.add_project(_project())
    _add_job(store, job_id="job_cancelled_on_error", state=JobState.PENDING)
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda: None)

    def cancel_then_fail(*_args, **_kwargs) -> None:
        Store(test_settings).update_job("job_cancelled_on_error", state=JobState.CANCELLED)
        raise subprocess.CalledProcessError(1, ["ffmpeg"])

    monkeypatch.setattr(worker, "process_project", cancel_then_fail)

    worker.execute("job_cancelled_on_error", test_settings)

    current = store.get_job("job_cancelled_on_error")
    project = store.get_project("proj_windows_jobs")
    assert current is not None
    assert current.state == JobState.CANCELLED
    assert project is not None
    assert project.state == ProjectState.IMPORTED


def test_worker_registers_windows_break_handler(monkeypatch) -> None:
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(worker, "_windows_break_signal", lambda: 21)
    monkeypatch.setattr(
        worker.signal, "signal", lambda number, handler: calls.append((number, handler))
    )

    worker._install_signal_handlers()

    assert calls == [
        (worker.signal.SIGTERM, worker._handle_signal),
        (21, worker._handle_signal),
    ]


def test_pid_probe_rejects_non_positive_values() -> None:
    assert jobs._pid_alive(None) is False
    assert jobs._pid_alive(0) is False
    assert jobs._pid_alive(-1) is False


def test_current_posix_pid_is_alive() -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only smoke check")
    assert jobs._pid_alive(os.getpid()) is True
