from __future__ import annotations

import json
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .media import MediaError, resolve_executable, run, sha256_file
from .settings import Settings

EventCallback = Callable[[float, str], None]


@dataclass(frozen=True)
class StemCandidate:
    id: str
    label: str
    engine: str
    instrumental: str
    vocals: str
    production_grade: bool
    warning: str | None = None


class SeparatorAdapter(ABC):
    id: str
    label: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def separate(self, mix: Path, output_dir: Path, settings: Settings) -> StemCandidate: ...


class AudioSeparatorAdapter(SeparatorAdapter):
    id = "mel_band_roformer"
    label = "Mel-Band RoFormer"
    model = "model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt"

    def available(self) -> bool:
        return resolve_executable("audio-separator") is not None

    def separate(self, mix: Path, output_dir: Path, settings: Settings) -> StemCandidate:
        output_dir.mkdir(parents=True, exist_ok=True)
        model_dir = settings.data_dir / "models" / "audio-separator"
        model_dir.mkdir(parents=True, exist_ok=True)
        before = set(output_dir.glob("*.wav"))
        executable = resolve_executable("audio-separator")
        if not executable:
            raise MediaError("Không tìm thấy audio-separator trong environment hiện tại.")
        run(
            [
                executable,
                str(mix),
                "--model_filename",
                self.model,
                "--output_dir",
                str(output_dir),
                "--model_file_dir",
                str(model_dir),
                "--output_format",
                "WAV",
            ]
        )
        _write_model_manifest(
            model_dir / f"{self.model}.manifest.json",
            model_id=self.model,
            engine="audio-separator",
            model_files=[
                model_dir / self.model,
                model_dir / f"{Path(self.model).stem}.yaml",
            ],
        )
        created = sorted(set(output_dir.glob("*.wav")) - before)
        instrumental = next(
            (path for path in created if "instrument" in path.name.casefold()), None
        )
        vocals = next((path for path in created if "vocal" in path.name.casefold()), None)
        if not instrumental or not vocals:
            raise MediaError("Audio Separator không trả về đủ instrumental/vocals WAV.")
        final_instrumental = output_dir / "instrumental.wav"
        final_vocals = output_dir / "vocals.wav"
        instrumental.replace(final_instrumental)
        vocals.replace(final_vocals)
        return StemCandidate(
            self.id, self.label, self.id, str(final_instrumental), str(final_vocals), True
        )


class BsRoformerAdapter(AudioSeparatorAdapter):
    id = "bs_roformer_viperx_1297"
    label = "BS-RoFormer ViperX 1297"
    model = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"


class DemucsAdapter(SeparatorAdapter):
    id = "htdemucs_ft"
    label = "HTDemucs Fine-tuned"

    def available(self) -> bool:
        import importlib.util

        return importlib.util.find_spec("demucs") is not None

    def separate(self, mix: Path, output_dir: Path, settings: Settings) -> StemCandidate:
        temporary = output_dir / "demucs-output"
        model_dir = settings.data_dir / "models" / "demucs"
        model_dir.mkdir(parents=True, exist_ok=True)
        run(
            [
                sys.executable,
                "-m",
                "demucs",
                "-n",
                self.id,
                "--two-stems",
                "vocals",
                "--float32",
                "-o",
                str(temporary),
                str(mix),
            ],
            env={
                "HF_HOME": str(model_dir / "huggingface"),
                "HF_HUB_DISABLE_XET": "1",
                "TORCH_HOME": str(model_dir),
            },
        )
        checkpoints = sorted(model_dir.rglob("*.safetensors"))
        model_specs = sorted(model_dir.rglob(f"{self.id}.yaml"))
        _write_model_manifest(
            model_dir / f"{self.id}.manifest.json",
            model_id=self.id,
            engine="demucs",
            model_files=[path for path in [*checkpoints, *model_specs] if path.is_file()],
        )
        stem_dir = temporary / self.id / mix.stem
        instrumental_source = stem_dir / "no_vocals.wav"
        vocals_source = stem_dir / "vocals.wav"
        if not instrumental_source.exists() or not vocals_source.exists():
            raise MediaError("Demucs không trả về đủ instrumental/vocals WAV.")
        output_dir.mkdir(parents=True, exist_ok=True)
        instrumental = output_dir / "instrumental.wav"
        vocals = output_dir / "vocals.wav"
        shutil.copy2(instrumental_source, instrumental)
        shutil.copy2(vocals_source, vocals)
        return StemCandidate(self.id, self.label, self.id, str(instrumental), str(vocals), True)


class CenterCancelAdapter(SeparatorAdapter):
    id = "center_cancel"
    label = "Center-cancel fallback"

    def available(self) -> bool:
        return True

    def separate(self, mix: Path, output_dir: Path, settings: Settings) -> StemCandidate:
        output_dir.mkdir(parents=True, exist_ok=True)
        instrumental = output_dir / "instrumental.wav"
        vocals = output_dir / "vocals.wav"
        run(
            [
                settings.ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(mix),
                "-af",
                "pan=stereo|c0=c0-c1|c1=c1-c0",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s24le",
                str(instrumental),
            ]
        )
        run(
            [
                settings.ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(mix),
                "-af",
                "pan=mono|c0=0.5*c0+0.5*c1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s24le",
                str(vocals),
            ]
        )
        return StemCandidate(
            self.id,
            self.label,
            self.id,
            str(instrumental),
            str(vocals),
            False,
            "Fallback center-cancel không phải AI separation; bắt buộc nghe và xác nhận trước Final.",
        )


def separate_candidates(
    mix: Path,
    project_dir: Path,
    settings: Settings,
    quality: str,
    event: EventCallback,
) -> list[StemCandidate]:
    selected = selected_adapters(quality)

    candidates: list[StemCandidate] = []
    failures: list[str] = []
    for index, adapter in enumerate(selected):
        event(0.24 + 0.30 * index / max(1, len(selected)), f"Đang tách giọng bằng {adapter.label}…")
        candidate_dir = project_dir / "work" / "stems" / adapter.id
        restored = _restore_existing_candidate(adapter, candidate_dir)
        if restored is not None:
            candidates.append(restored)
            event(
                0.24 + 0.30 * index / max(1, len(selected)),
                f"Đã khôi phục stem {adapter.label} từ lần phân tích trước.",
            )
            continue
        try:
            candidates.append(adapter.separate(mix, candidate_dir, settings))
        except Exception as exc:  # adapters are intentionally isolated
            failures.append(f"{adapter.label}: {exc}")
    if not candidates:
        fallback = CenterCancelAdapter()
        candidates.append(
            fallback.separate(mix, project_dir / "work" / "stems" / fallback.id, settings)
        )
    payload = {
        "request": separator_request_signature(quality),
        "candidates": [
            {
                **asdict(candidate),
                "instrumental": Path(candidate.instrumental).relative_to(project_dir).as_posix(),
                "vocals": Path(candidate.vocals).relative_to(project_dir).as_posix(),
            }
            for candidate in candidates
        ],
        "models": _model_manifests(candidates, settings),
        "failures": failures,
    }
    manifest = project_dir / "work" / "stems" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return candidates


def selected_adapters(quality: str) -> list[SeparatorAdapter]:
    # Preserve the original analysis profile, then prepare ViperX separately as
    # the single default Final stem. Pipeline alignment deliberately ignores it.
    adapters: list[SeparatorAdapter] = [AudioSeparatorAdapter(), DemucsAdapter()]
    selected = [adapter for adapter in adapters if adapter.available()]
    if quality == "balanced":
        selected = selected[:1]
    elif quality == "fast":
        selected = []
    if len(selected) < 2 and quality == "highest":
        selected.append(CenterCancelAdapter())
    if not selected:
        selected = [CenterCancelAdapter()]
    viperx = BsRoformerAdapter()
    if viperx.available():
        selected.append(viperx)
    return selected


def _restore_existing_candidate(
    adapter: SeparatorAdapter, output_dir: Path
) -> StemCandidate | None:
    instrumental = output_dir / "instrumental.wav"
    vocals = output_dir / "vocals.wav"
    if not instrumental.is_file() or not vocals.is_file():
        return None
    fallback = isinstance(adapter, CenterCancelAdapter)
    return StemCandidate(
        id=adapter.id,
        label=adapter.label,
        engine=adapter.id,
        instrumental=str(instrumental),
        vocals=str(vocals),
        production_grade=not fallback,
        warning=(
            "Fallback center-cancel không phải AI separation; bắt buộc nghe và xác nhận trước Final."
            if fallback
            else None
        ),
    )


def separator_request_signature(quality: str) -> dict[str, object]:
    return {"quality": quality, "adapters": [adapter.id for adapter in selected_adapters(quality)]}


def load_candidates(project_dir: Path) -> list[StemCandidate]:
    payload = json.loads(
        (project_dir / "work" / "stems" / "manifest.json").read_text(encoding="utf-8")
    )
    candidates = []
    for candidate in payload["candidates"]:
        candidate = dict(candidate)
        for key in ("instrumental", "vocals"):
            path = Path(candidate[key])
            candidate[key] = str(path if path.is_absolute() else project_dir / path)
        candidates.append(StemCandidate(**candidate))
    return candidates


def _package_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _write_model_manifest(
    output: Path,
    *,
    model_id: str,
    engine: str,
    model_files: list[Path],
) -> None:
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in model_files
        if path.is_file()
    }
    if not files:
        raise MediaError(f"Không tìm thấy checkpoint đã tải cho {model_id}.")
    package = "audio-separator" if engine == "audio-separator" else "demucs"
    output.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "engine": engine,
                "engine_version": _package_version(package),
                "model_id": model_id,
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _model_manifests(
    candidates: list[StemCandidate], settings: Settings
) -> dict[str, object]:
    paths = {
        "mel_band_roformer": settings.data_dir
        / "models"
        / "audio-separator"
        / f"{AudioSeparatorAdapter.model}.manifest.json",
        "bs_roformer_viperx_1297": settings.data_dir
        / "models"
        / "audio-separator"
        / f"{BsRoformerAdapter.model}.manifest.json",
        "htdemucs_ft": settings.data_dir / "models" / "demucs" / "htdemucs_ft.manifest.json",
    }
    manifests: dict[str, object] = {}
    for candidate in candidates:
        path = paths.get(candidate.id)
        if path and path.exists():
            manifests[candidate.id] = json.loads(path.read_text(encoding="utf-8"))
    return manifests
