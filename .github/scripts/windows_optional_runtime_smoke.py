"""Offline Windows smoke test for the optional karaoke production runtimes.

This deliberately does not instantiate a pretrained model. It verifies the
installed wheels, CLI entry points, application adapter discovery, a tiny CPU
tensor operation, and FFmpeg handling of a Vietnamese Unicode path.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from karaoke_studio.alignment import VietnameseCTCAligner
from karaoke_studio.jobs import _terminate_windows_process_tree, _windows_pid_alive
from karaoke_studio.media import tool_capabilities
from karaoke_studio.separation import separator_request_signature
from karaoke_studio.settings import Settings


def _run(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Required executable is not on PATH: {name}")
    return executable


def _windows_process_tree_round_trip(temporary: Path) -> None:
    """Prove that native Windows cancellation reaches an FFmpeg-like descendant."""
    pid_path = temporary / "descendant.pid"
    launcher = temporary / "spawn-descendant.py"
    launcher.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    root = subprocess.Popen(
        [sys.executable, str(launcher), str(pid_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    descendant_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and root.poll() is None:
            try:
                published_pid = pid_path.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                published_pid = ""
            if published_pid:
                try:
                    descendant_pid = int(published_pid)
                except ValueError:
                    # The child writes through a normal text file. On Windows the
                    # reader can observe it between creation and the completed write.
                    descendant_pid = None
                else:
                    break
            time.sleep(0.05)
        if descendant_pid is None:
            raise RuntimeError("Windows process-tree fixture did not publish its child PID.")
        if not _windows_pid_alive(root.pid) or not _windows_pid_alive(descendant_pid):
            raise RuntimeError("Windows process-tree fixture exited before cancellation.")

        _terminate_windows_process_tree(root.pid)
        root.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _windows_pid_alive(descendant_pid):
            time.sleep(0.05)
        if _windows_pid_alive(descendant_pid):
            raise RuntimeError("Windows cancellation left a descendant process running.")
    finally:
        if root.poll() is None:
            _terminate_windows_process_tree(root.pid)
        if descendant_pid is not None and _windows_pid_alive(descendant_pid):
            _terminate_windows_process_tree(descendant_pid)


def main(expected_system: str = "Windows") -> None:
    current_system = platform.system()
    if current_system != expected_system:
        raise RuntimeError(
            f"This CI smoke test requires {expected_system}, got {platform.platform()}"
        )
    if expected_system == "Windows" and (
        sys.maxsize <= 2**32 or platform.machine().casefold() not in {"amd64", "x86_64"}
    ):
        raise RuntimeError("The supported Windows target is 64-bit (x86-64).")
    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        raise RuntimeError("Model hubs must stay offline during the smoke test.")

    modules = (
        "audio_separator.separator.separator",
        "audioread",
        "demucs.apply",
        "demucs.audio",
        "demucs.separate",
        "huggingface_hub",
        "librosa",
        "onnxruntime",
        "torch",
        "transformers",
    )
    for module in modules:
        importlib.import_module(module)

    ffmpeg = _require_executable("ffmpeg")
    ffprobe = _require_executable("ffprobe")
    audio_separator = _require_executable("audio-separator")
    _run([ffmpeg, "-version"])
    _run([ffprobe, "-version"])
    _run([audio_separator, "--help"])
    _run([sys.executable, "-m", "demucs", "--help"])

    import torch

    values = torch.tensor([1.0, 2.0], device="cpu")
    if values.square().sum().item() != 5.0:
        raise RuntimeError("PyTorch CPU smoke calculation returned the wrong result.")

    with tempfile.TemporaryDirectory() as temporary:
        data_dir = Path(temporary) / "dữ-liệu-kiểm-thử"
        data_dir.mkdir()
        if current_system == "Windows":
            _windows_process_tree_round_trip(data_dir)
        source = data_dir / "nhạc-nguồn.wav"
        converted = data_dir / "nhạc-16-khz.wav"
        sf.write(source, np.zeros(1600, dtype=np.float32), 16_000)
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-ar",
                "16000",
                str(converted),
            ]
        )
        samples, sample_rate = sf.read(converted, dtype="float32")
        if sample_rate != 16_000 or len(samples) != 1600:
            raise RuntimeError("FFmpeg/SoundFile Unicode-path round trip failed.")

        settings = Settings(
            root=Path.cwd(),
            data_dir=data_dir,
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            host="127.0.0.1",
            port=8000,
            frontend_origin="http://127.0.0.1:3000",
        )
        capabilities = tool_capabilities(settings)
        expected = {
            "ffmpeg": True,
            "ffprobe": True,
            "audio_separator": True,
            "demucs": True,
            "vietnamese_ctc": True,
        }
        if capabilities != expected:
            raise RuntimeError(f"Unexpected application capabilities: {capabilities!r}")
        if separator_request_signature("highest")["adapters"] != [
            "mel_band_roformer",
            "htdemucs_ft",
            "bs_polarformer_fp32",
        ]:
            raise RuntimeError("Highest-quality separation adapters were not discovered.")
        if not VietnameseCTCAligner(settings).available():
            raise RuntimeError("Vietnamese CTC adapter did not discover its local runtime.")

    print(f"{expected_system} optional runtime smoke test passed without model downloads.")


if __name__ == "__main__":
    main()
