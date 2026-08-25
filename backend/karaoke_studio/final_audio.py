from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
import soundfile as sf

from .db import Store
from .media import MediaError, resolve_executable, run, sha256_file, waveform_envelope
from .separation import StemCandidate, load_candidates
from .settings import Settings

EventCallback = Callable[[float, str], None]
FINAL_AUDIO_SCHEMA_VERSION = "2.0"
MODEL_LICENSE_NOTICE = (
    "Community checkpoint used locally. Redistribution is not asserted by Karaoke Studio."
)


@dataclass(frozen=True)
class FinalAudioProfile:
    id: str
    label: str
    quality_profile: str
    preset: str
    description: str
    models: tuple[tuple[str, str], ...]


FINAL_AUDIO_PROFILES = (
    FinalAudioProfile(
        id="final_full",
        label="Đầy nhạc · RoFormer Full",
        quality_profile="full",
        preset="instrumental_full",
        description="Ưu tiên giữ piano, guitar, cymbal, ambience và stereo width.",
        models=(
            ("melband_roformer_inst_v1e_plus.ckpt", "config_melbandroformer_inst.yaml"),
            (
                "mel_band_roformer_instrumental_becruily.ckpt",
                "config_mel_band_roformer_instrumental_becruily.yaml",
            ),
        ),
    ),
    FinalAudioProfile(
        id="final_balanced",
        label="Cân bằng · RoFormer Ensemble",
        quality_profile="balanced",
        preset="instrumental_balanced",
        description="Cân bằng độ đầy của nhạc và lượng giọng còn sót.",
        models=(
            (
                "mel_band_roformer_instrumental_instv8_gabox.ckpt",
                "config_mel_band_roformer_instrumental_gabox.yaml",
            ),
            (
                "bs_roformer_instrumental_resurrection_unwa.ckpt",
                "config_bs_roformer_instrumental_resurrection_unwa.yaml",
            ),
        ),
    ),
    FinalAudioProfile(
        id="final_clean",
        label="Sạch giọng · RoFormer Clean",
        quality_profile="clean",
        preset="instrumental_clean",
        description="Ưu tiên giảm vocal bleed; có thể mỏng hơn ở nhạc cụ tinh tế.",
        models=(
            (
                "mel_band_roformer_instrumental_fv7z_gabox.ckpt",
                "config_mel_band_roformer_instrumental_gabox.yaml",
            ),
            (
                "bs_roformer_instrumental_resurrection_unwa.ckpt",
                "config_bs_roformer_instrumental_resurrection_unwa.yaml",
            ),
        ),
    ),
)


def prepare_final_audio_project(
    job_id: str,
    project_id: str,
    settings: Settings,
    event: EventCallback,
) -> list[StemCandidate]:
    del job_id
    store = Store(settings)
    project = store.get_project(project_id)
    if not project:
        raise KeyError(project_id)
    project_dir = store.project_dir(project_id)
    mix = project_dir / "work" / "mix.wav"
    if not mix.is_file():
        raise MediaError("Project chưa có PCM nguồn; hãy chạy phân tích trước.")

    protected = _protected_timing_hashes(project_dir)
    mix_sha256 = sha256_file(mix)
    final_root = project_dir / "work" / "final-audio"
    final_root.mkdir(parents=True, exist_ok=True)
    separator_input = _prepare_separator_input(mix, final_root, settings, mix_sha256)
    event(0.04, "Đã khóa PCM nguồn; timeline và dữ liệu căn lời sẽ không bị thay đổi.")

    candidates: list[StemCandidate] = []
    for index, profile in enumerate(FINAL_AUDIO_PROFILES):
        start = 0.07 + index * 0.27
        event(start, f"Đang tạo candidate {profile.label}…")
        candidates.append(
            _prepare_profile(
                profile,
                separator_input,
                mix,
                mix_sha256,
                project_dir,
                settings,
            )
        )
        event(start + 0.25, f"Đã hoàn tất {profile.label}.")

    _merge_candidates(project_dir, candidates, settings, mix_sha256)
    _update_waveforms(project_dir, candidates)
    _assert_timing_unchanged(project_dir, protected)
    event(0.92, "Audio Final đã sẵn sàng để nghe A/B; timing được giữ nguyên byte-for-byte.")
    return candidates


def _prepare_separator_input(
    mix: Path,
    final_root: Path,
    settings: Settings,
    mix_sha256: str,
) -> Path:
    output = final_root / "source-44100-f32.wav"
    manifest = final_root / "source-manifest.json"
    resample_filter, resampler = _resampler_filter(settings.ffmpeg, 44100)
    request = {
        "mix_sha256": mix_sha256,
        "sample_rate": 44100,
        "codec": "pcm_f32le",
        "resampler": resampler,
    }
    if output.is_file() and manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("request") == request and payload.get("sha256") == sha256_file(output):
                return output
        except (OSError, json.JSONDecodeError):
            pass
    run(
        [
            settings.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(mix),
            "-vn",
            "-ac",
            "2",
            "-af",
            resample_filter,
            "-c:a",
            "pcm_f32le",
            str(output),
        ]
    )
    manifest.write_text(
        json.dumps(
            {"schema_version": "1.0", "request": request, "sha256": sha256_file(output)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output


def _prepare_profile(
    profile: FinalAudioProfile,
    separator_input: Path,
    mix: Path,
    mix_sha256: str,
    project_dir: Path,
    settings: Settings,
) -> StemCandidate:
    output_dir = project_dir / "work" / "stems" / profile.id
    output_dir.mkdir(parents=True, exist_ok=True)
    instrumental = output_dir / "instrumental.wav"
    vocals = output_dir / "vocals.wav"
    manifest_path = output_dir / "final-audio-manifest.json"
    request = {
        "schema_version": FINAL_AUDIO_SCHEMA_VERSION,
        "profile": profile.id,
        "preset": profile.preset,
        "mix_sha256": mix_sha256,
        "sample_rate": 48000,
        "normalization_threshold": 1.0,
        "amplification_threshold": 0.0,
        "writer": "soundfile-float32-to-pcm24",
    }
    if _reusable_profile(manifest_path, request, instrumental, vocals):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _candidate_from_profile(profile, instrumental, vocals, payload["output"])

    executable = resolve_executable("audio-separator")
    if not executable:
        raise MediaError("Không tìm thấy audio-separator để tạo Audio Final cực đại.")
    model_dir = settings.data_dir / "models" / "audio-separator"
    model_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    before = set(raw_dir.glob("*.wav"))
    run(
        [
            executable,
            str(separator_input),
            "--ensemble_preset",
            profile.preset,
            "--output_dir",
            str(raw_dir),
            "--model_file_dir",
            str(model_dir),
            "--output_format",
            "WAV",
            "--use_soundfile",
            "--normalization",
            "1.0",
            "--amplification",
            "0.0",
            "--sample_rate",
            "44100",
            "--custom_output_names",
            json.dumps({"Instrumental": "instrumental", "Vocals": "vocals"}),
        ]
    )
    created = sorted(set(raw_dir.glob("*.wav")) - before)
    raw_instrumental = _resolve_stem(raw_dir, created, "instrument")
    raw_vocals = _resolve_stem(raw_dir, created, "vocal")
    if not raw_instrumental or not raw_vocals:
        raise MediaError(f"Preset {profile.preset} không trả về đủ Instrumental/Vocals.")

    source_info = sf.info(mix)
    inst_qa = _convert_to_master_pcm(
        raw_instrumental,
        instrumental,
        source_info.frames,
        source_info.samplerate,
        settings,
    )
    _convert_to_master_pcm(
        raw_vocals,
        vocals,
        source_info.frames,
        source_info.samplerate,
        settings,
    )
    models = _model_snapshot(profile, model_dir)
    output = {
        **inst_qa,
        "instrumental_sha256": sha256_file(instrumental),
        "vocals_sha256": sha256_file(vocals),
    }
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": FINAL_AUDIO_SCHEMA_VERSION,
                "request": request,
                "profile": asdict(profile),
                "engine": {
                    "id": "audio-separator",
                    "version": _package_version("audio-separator"),
                },
                "models": models,
                "license_notice": MODEL_LICENSE_NOTICE,
                "output": output,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return _candidate_from_profile(profile, instrumental, vocals, output)


def _resolve_stem(directory: Path, created: list[Path], needle: str) -> Path | None:
    exact = directory / f"{needle.rstrip('s')}al.wav" if needle == "instrument" else directory / "vocals.wav"
    if exact.is_file():
        return exact
    return next((path for path in created if needle in path.name.casefold()), None)


def _convert_to_master_pcm(
    source: Path,
    output: Path,
    expected_frames: int,
    sample_rate: int,
    settings: Settings,
) -> dict[str, object]:
    source_peak = _peak(source)
    gain = min(1.0, 0.999 / source_peak) if source_peak > 0 else 1.0
    resample_filter, resampler = _resampler_filter(settings.ffmpeg, sample_rate)
    filters = [resample_filter]
    if gain < 1.0:
        filters.append(f"volume={gain:.12f}")
    filters.extend(("apad", f"atrim=end_sample={expected_frames}"))
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
            "-af",
            ",".join(filters),
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s24le",
            str(output),
        ]
    )
    info = sf.info(output)
    peak = _peak(output)
    status = (
        "PASS"
        if info.samplerate == sample_rate
        and info.channels == 2
        and info.frames == expected_frames
        and math.isfinite(peak)
        and peak <= 1.0
        else "FAIL"
    )
    if status != "PASS":
        raise MediaError(f"Audio QA thất bại cho {output.name}.")
    return {
        "sample_rate": info.samplerate,
        "channels": info.channels,
        "frames": info.frames,
        "subtype": info.subtype,
        "peak": round(peak, 9),
        "source_peak": round(source_peak, 9),
        "gain_db": round(20 * math.log10(gain), 6) if gain > 0 else float("-inf"),
        "resampler": resampler,
        "limiter": False,
        "loudness_normalization": False,
        "status": status,
    }


def _peak(path: Path) -> float:
    peak = 0.0
    with sf.SoundFile(path) as handle:
        for block in handle.blocks(blocksize=262144, dtype="float32", always_2d=True):
            if block.size:
                peak = max(peak, float(np.max(np.abs(block))))
    return peak


@lru_cache(maxsize=8)
def _has_soxr(ffmpeg: str) -> bool:
    result = run([ffmpeg, "-version"])
    build = f"{result.stdout}\n{result.stderr}".casefold()
    return "--enable-libsoxr" in build


def _resampler_filter(ffmpeg: str, sample_rate: int) -> tuple[str, str]:
    if _has_soxr(ffmpeg):
        return (
            f"aresample={sample_rate}:resampler=soxr:precision=28:cheby=1",
            "soxr-precision-28",
        )
    # Some official/static FFmpeg builds omit libsoxr. SWR with a larger filter
    # and high-pass triangular dither is deterministic and avoids silently
    # falling back to the low-quality default resampler.
    return (
        f"aresample={sample_rate}:resampler=swr:filter_size=64:phase_shift=10:"
        "cutoff=0.97:dither_method=triangular_hp",
        "swr-filter64-triangular-hp",
    )


def _candidate_from_profile(
    profile: FinalAudioProfile,
    instrumental: Path,
    vocals: Path,
    output: dict[str, object],
) -> StemCandidate:
    return StemCandidate(
        id=profile.id,
        label=profile.label,
        engine=f"audio-separator:{profile.preset}",
        instrumental=str(instrumental),
        vocals=str(vocals),
        production_grade=True,
        warning=profile.description,
        quality_profile=profile.quality_profile,
        analysis_eligible=False,
        export_eligible=True,
        pcm_sha256=str(output["instrumental_sha256"]),
        signal_path=(
            "source PCM float32 → RoFormer ensemble → "
            f"{output.get('resampler', 'resampler chất lượng cao')} 48k → PCM24; "
            "không limiter/normalize loudness"
        ),
        audio_qa={
            key: output[key]
            for key in (
                "sample_rate",
                "channels",
                "frames",
                "peak",
                "gain_db",
                "resampler",
                "status",
            )
        },
    )


def _reusable_profile(
    manifest: Path,
    request: dict[str, object],
    instrumental: Path,
    vocals: Path,
) -> bool:
    if not manifest.is_file() or not instrumental.is_file() or not vocals.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        output = payload["output"]
        return bool(
            payload.get("request") == request
            and output.get("instrumental_sha256") == sha256_file(instrumental)
            and output.get("vocals_sha256") == sha256_file(vocals)
            and output.get("status") == "PASS"
        )
    except (KeyError, OSError, json.JSONDecodeError):
        return False


def _model_snapshot(profile: FinalAudioProfile, model_dir: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for checkpoint, config in profile.models:
        files: dict[str, object] = {}
        for filename in (checkpoint, config):
            path = model_dir / filename
            if path.is_file():
                files[filename] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if checkpoint not in files:
            raise MediaError(f"Checkpoint {checkpoint} chưa được tải đầy đủ.")
        result[checkpoint] = {"files": files, "license_notice": MODEL_LICENSE_NOTICE}
    return result


def _merge_candidates(
    project_dir: Path,
    candidates: list[StemCandidate],
    settings: Settings,
    mix_sha256: str,
) -> None:
    manifest = project_dir / "work" / "stems" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    ids = {candidate.id for candidate in candidates}
    retained = [item for item in payload.get("candidates", []) if item.get("id") not in ids]
    retained.extend(
        {
            **asdict(candidate),
            "instrumental": Path(candidate.instrumental).relative_to(project_dir).as_posix(),
            "vocals": Path(candidate.vocals).relative_to(project_dir).as_posix(),
        }
        for candidate in candidates
    )
    payload["schema_version"] = FINAL_AUDIO_SCHEMA_VERSION
    payload["candidates"] = retained
    payload["final_audio"] = {
        "schema_version": FINAL_AUDIO_SCHEMA_VERSION,
        "mix_sha256": mix_sha256,
        "engine_version": _package_version("audio-separator"),
        "profiles": [profile.id for profile in FINAL_AUDIO_PROFILES],
        "resampler": _resampler_filter(settings.ffmpeg, 48000)[1],
        "master_format": "pcm_s24le-48000-stereo",
        "limiter": False,
        "loudness_normalization": False,
        "ffmpeg": run([settings.ffmpeg, "-version"]).stdout.splitlines()[0],
    }
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest)


def _update_waveforms(project_dir: Path, candidates: list[StemCandidate]) -> None:
    path = project_dir / "work" / "waveforms.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
        "mix": waveform_envelope(project_dir / "work" / "mix.wav"),
        "candidates": {},
    }
    candidate_payload = payload.setdefault("candidates", {})
    for candidate in candidates:
        candidate_payload[candidate.id] = {
            "label": candidate.label,
            "production_grade": candidate.production_grade,
            "warning": candidate.warning,
            "quality_profile": candidate.quality_profile,
            "analysis_eligible": candidate.analysis_eligible,
            "export_eligible": candidate.export_eligible,
            "pcm_sha256": candidate.pcm_sha256,
            "signal_path": candidate.signal_path,
            "audio_qa": candidate.audio_qa,
            "instrumental": waveform_envelope(Path(candidate.instrumental)),
            "vocals": waveform_envelope(Path(candidate.vocals)),
        }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _protected_timing_hashes(project_dir: Path) -> dict[str, str]:
    protected: dict[str, str] = {}
    for relative in (
        "timeline.json",
        "work/alignment-manifest.json",
        "work/alignment-evidence.json",
        "work/alignment-report.json",
    ):
        path = project_dir / relative
        if path.is_file():
            protected[relative] = sha256_file(path)
    return protected


def _assert_timing_unchanged(project_dir: Path, expected: dict[str, str]) -> None:
    actual = {
        relative: sha256_file(project_dir / relative)
        for relative in expected
        if (project_dir / relative).is_file()
    }
    if actual != expected:
        raise RuntimeError("Final Audio đã chạm vào dữ liệu timing; kết quả bị từ chối.")


def _package_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def all_candidates(project_dir: Path) -> list[StemCandidate]:
    return load_candidates(project_dir)
