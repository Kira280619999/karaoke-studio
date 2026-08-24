from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from .db import Store, now_iso
from .models import JobRecord, JobState
from .settings import Settings

_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class ProcessTreeTerminationError(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


def _worker_process_group_options() -> dict[str, object]:
    if _is_windows():
        return {"creationflags": _WINDOWS_CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _windows_pid_alive(pid: int) -> bool:
    """Check a Windows PID without using os.kill(pid, 0).

    Unlike POSIX, Windows implements os.kill through TerminateProcess for most
    signal values. Opening the process and reading its exit code is a read-only
    liveness check.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied still proves that the PID exists. Other failures (most
        # commonly ERROR_INVALID_PARAMETER) mean there is no live process.
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Be conservative if a live handle cannot be queried: blocking a
            # duplicate worker is safer than launching two jobs for a project.
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if _is_windows():
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM means the process exists but this account cannot signal it.
        return True
    except OSError:
        return False
    return True


def _terminate_windows_process_tree(pid: int) -> None:
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    taskkill = (
        str(PureWindowsPath(system_root) / "System32" / "taskkill.exe")
        if system_root
        else "taskkill.exe"
    )
    result = subprocess.run(
        [taskkill, "/PID", str(pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
        creationflags=_WINDOWS_CREATE_NO_WINDOW,
    )
    if result.returncode != 0 and _windows_pid_alive(pid):
        raise ProcessTreeTerminationError(
            f"taskkill không thể dừng cây tiến trình PID {pid} (mã {result.returncode})."
        )


def _terminate_process_tree(pid: int) -> None:
    if _is_windows():
        _terminate_windows_process_tree(pid)
        return
    os.killpg(pid, signal.SIGTERM)


class JobManager:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def start(self, project_id: str, kind: str, options: dict) -> JobRecord:
        active = self.store.get_active_job(project_id)
        if active and self._pid_alive(active.pid):
            if active.kind != kind:
                raise RuntimeError(f"Project đang chạy job {active.kind}.")
            return active
        if active:
            self.store.update_job(
                active.id,
                state=JobState.FAILED,
                message="Worker trước đã dừng; job mới sẽ tiếp tục từ manifest.",
                error="STALE_WORKER",
            )
        job_id = f"job_{uuid4().hex[:16]}"
        timestamp = now_iso()
        job = JobRecord(
            id=job_id,
            project_id=project_id,
            kind=kind,
            state=JobState.PENDING,
            progress=0,
            message="Đang xếp tác vụ…",
            created_at=timestamp,
            updated_at=timestamp,
            options=options,
        )
        self.store.add_job(job)
        log_dir = self.settings.data_dir / "worker-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        env = os.environ.copy()
        env["KARAOKE_STUDIO_DATA"] = str(self.settings.data_dir)
        if _is_windows():
            env.setdefault("PYTHONUTF8", "1")
        backend_path = str(self.settings.root / "backend")
        env["PYTHONPATH"] = backend_path + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "karaoke_studio.worker", "--job-id", job_id],
                cwd=self.settings.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                **_worker_process_group_options(),
            )
        return self.store.update_job(job_id, pid=process.pid)

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        return _pid_alive(pid)

    def cancel(self, job_id: str) -> JobRecord:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job.state in {JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED}:
            return job
        self.store.update_job(job_id, state=JobState.CANCELLED, message="Đã yêu cầu dừng tác vụ.")
        termination_error: Exception | None = None
        if job.pid and self._pid_alive(job.pid):
            try:
                _terminate_process_tree(job.pid)
            except ProcessLookupError:
                pass
            except (OSError, subprocess.SubprocessError, ProcessTreeTerminationError) as exc:
                termination_error = exc
        if termination_error is not None:
            return self.store.update_job(
                job_id,
                state=JobState.CANCELLED,
                message="Đã hủy job nhưng chưa thể xác nhận toàn bộ tiến trình đã dừng.",
                error=f"CANCEL_TERMINATION_FAILED: {termination_error}",
            )
        # Write the terminal state again after termination. This closes the
        # startup race where a just-spawned worker read PENDING before the first
        # update and wrote RUNNING while cancellation was in progress.
        return self.store.update_job(
            job_id,
            state=JobState.CANCELLED,
            message="Đã yêu cầu dừng tác vụ.",
            error=None,
        )

    def event_path(self, job_id: str) -> Path:
        return self.settings.data_dir / "job-events" / f"{job_id}.jsonl"

    def read_events(self, job_id: str, after: int = -1) -> list[dict]:
        path = self.event_path(job_id)
        if not path.exists():
            return []
        events = []
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if index > after:
                payload = json.loads(line)
                payload["sequence"] = index
                events.append(payload)
        return events
