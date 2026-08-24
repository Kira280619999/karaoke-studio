from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from karaoke_studio.settings import Settings


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    root = Path(__file__).resolve().parents[2]
    settings = Settings(
        root=root,
        data_dir=tmp_path / "data",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        host="127.0.0.1",
        port=8000,
        frontend_origin="http://127.0.0.1:3000",
    )
    settings.ensure()
    return settings


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    output = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x101820:s=640x360:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(output),
        ],
        check=True,
    )
    return output


@pytest.fixture
def silent_video(tmp_path: Path) -> Path:
    output = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=24:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
    )
    return output
