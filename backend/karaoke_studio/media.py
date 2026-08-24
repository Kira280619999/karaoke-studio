from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .settings import Settings

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration_us: int
    video_duration_us: int
    width: int
    height: int
    fps: str
    fps_float: float
    variable_frame_rate: bool
    has_audio: bool
    audio_duration_us: int | None
    rotation: int
    video_frames: int | None
    video_has_b_frames: int
    video_time_base: str
    video_profile: str | None
    video_level: int | None


def safe_filename(filename: str, fallback: str) -> str:
    candidate = SAFE_NAME.sub("-", Path(filename).name).strip(".-")
    return candidate[:160] or fallback


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _ratio(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator) if float(denominator) else 0.0


def probe(path: Path, settings: Settings) -> MediaInfo:
    command = [
        settings.ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = run(command)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe không trả về JSON hợp lệ.") from exc
    video = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if not video:
        raise MediaError("File không có video stream.")
    audio = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    format_duration_raw = payload.get("format", {}).get("duration")
    video_duration_raw = video.get("duration") or format_duration_raw
    audio_duration_raw = (audio.get("duration") or format_duration_raw) if audio else None
    duration_candidates = [
        float(value)
        for value in (video_duration_raw, audio_duration_raw, format_duration_raw)
        if value not in {None, "N/A"}
    ]
    if not duration_candidates or not video_duration_raw:
        raise MediaError("Không đọc được duration của video.")
    average = _ratio(video.get("avg_frame_rate"))
    real = _ratio(video.get("r_frame_rate"))
    fps = video.get("avg_frame_rate") if average else video.get("r_frame_rate", "30/1")
    rotation = int(video.get("tags", {}).get("rotate", 0) or 0)
    for side_data in video.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = int(side_data["rotation"])
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if abs(rotation) in {90, 270}:
        width, height = height, width
    return MediaInfo(
        # Karaoke follows the longest playable stream. Some downloaded MP4 files
        # have a shorter video track than their complete song audio track.
        duration_us=round(max(duration_candidates) * 1_000_000),
        video_duration_us=round(float(video_duration_raw) * 1_000_000),
        width=width,
        height=height,
        fps=fps,
        fps_float=average or real or 30.0,
        variable_frame_rate=bool(average and real and abs(average - real) > 0.01),
        has_audio=audio is not None,
        audio_duration_us=round(float(audio_duration_raw) * 1_000_000)
        if audio_duration_raw
        else None,
        rotation=rotation,
        video_frames=(
            int(video["nb_frames"])
            if video.get("nb_frames") not in {None, "N/A"}
            else None
        ),
        video_has_b_frames=int(video.get("has_b_frames", 0) or 0),
        video_time_base=str(video.get("time_base", "0/0")),
        video_profile=video.get("profile"),
        video_level=(int(video["level"]) if video.get("level") is not None else None),
    )


def run(
    command: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"Không tìm thấy công cụ: {command[0]}") from exc
    if result.returncode:
        raise MediaError(result.stderr.strip()[-4000:] or f"Lệnh thất bại: {command[0]}")
    return result


def extract_audio(source: Path, work_dir: Path, settings: Settings) -> tuple[Path, Path]:
    mix = work_dir / "mix.wav"
    alignment = work_dir / "alignment.wav"
    run(
        [
            settings.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s24le",
            str(mix),
        ]
    )
    run(
        [
            settings.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(alignment),
        ]
    )
    return mix, alignment


def make_proxy(source: Path, output: Path, settings: Settings) -> None:
    info = probe(source, settings)
    video_padding_us = max(0, info.duration_us - info.video_duration_us)
    video_filter = (
        "fps=30,scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    if video_padding_us:
        video_filter += (
            ",tpad=stop_mode=clone:"
            f"stop_duration={video_padding_us / 1_000_000:.6f}"
        )
    command = [
            settings.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
    ]
    if info.has_audio:
        command.extend(["-af", "apad"])
    command.extend(
        [
            "-t",
            f"{info.duration_us / 1_000_000:.6f}",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    run(command)


def waveform_envelope(wav_path: Path, points: int = 1200) -> list[float]:
    samples, _sample_rate = sf.read(wav_path, dtype="float32", always_2d=True)
    samples = samples.mean(axis=1)
    if not len(samples):
        return [0.0] * points
    chunk = max(1, math.ceil(len(samples) / points))
    envelope = [
        float(np.sqrt(np.mean(np.square(samples[index : index + chunk]))))
        for index in range(0, len(samples), chunk)
    ]
    peak = max(envelope, default=1.0) or 1.0
    normalized = [round(value / peak, 6) for value in envelope[:points]]
    return normalized + [0.0] * (points - len(normalized))


def tool_capabilities(settings: Settings) -> dict[str, object]:
    return {
        "ffmpeg": shutil.which(settings.ffmpeg) is not None,
        "ffprobe": shutil.which(settings.ffprobe) is not None,
        "audio_separator": resolve_executable("audio-separator") is not None,
        "demucs": _module_available("demucs"),
        "vietnamese_ctc": _module_available("transformers") and _module_available("torch"),
    }


def resolve_executable(name: str) -> str | None:
    discovered = shutil.which(name)
    if discovered:
        return discovered
    local = Path(sys.executable).parent / name
    return str(local) if local.is_file() else None


def _module_available(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None
