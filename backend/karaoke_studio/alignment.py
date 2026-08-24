from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .lrc import normalize_token
from .media import sha256_file
from .models import (
    LineTiming,
    TimelineV1,
    TimingSource,
    TimingSuggestionResponse,
    TokenTiming,
    TokenTimingSuggestion,
)
from .motion import (
    GraphemeSpan,
    is_sung_grapheme,
    linear_sweep,
    resolve_font,
    split_graphemes,
)
from .settings import Settings

VIETNAMESE_MODEL_ID = "nguyenvulebinh/wav2vec2-base-vietnamese-250h"
VIETNAMESE_MODEL_REVISION = "69e9000591623e5a4fc2f502407860bcdc0de0b2"
VIETNAMESE_MODEL_LICENSE = "CC-BY-NC-4.0"
VIETNAMESE_MODEL_LICENSE_FILE = "CC-BY-NC-SA-4.0.txt"
VIETNAMESE_MODEL_FILES = (
    VIETNAMESE_MODEL_LICENSE_FILE,
    "README.md",
    "config.json",
    "preprocessor_config.json",
    "pytorch_model.bin",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.json",
)
LYRIC_MODEL_ID = "nguyenvulebinh/lyric-alignment"
LYRIC_MODEL_REVISION = "2d0b61d13e9da6cae03a6c01fe3eeebdb6e4b4ed"
LYRIC_MODEL_LICENSE = "CC-BY-NC-4.0"
LYRIC_MODEL_FILES = (
    "README.md",
    "added_tokens.json",
    "config.json",
    "preprocessor_config.json",
    "pytorch_model.bin",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.json",
)
EventCallback = Callable[[float, str], None]
CTC_SEARCH_PADDING_US = 900_000
MIN_CTC_TOKEN_CONFIDENCE = 0.15
SINGLE_CTC_POLICY = "single-production-stem-windowed-ctc-v2"
ENERGY_POLICY = "energy-valley-v2"
CONSENSUS_POLICY = "dual-production-stem-windowed-ctc-v2"
_CTC_RUNTIME_LOCK = threading.Lock()
_CTC_RUNTIMES: dict[str, tuple[object, object, object]] = {}
SONG_CHUNK_SECONDS = 20
SONG_CHUNK_OVERLAP_SECONDS = 2
TORCH_DEVICE_ENV = "KARAOKE_STUDIO_TORCH_DEVICE"


def _torch_backend_available(torch_module: object, backend: str) -> bool:
    """Return backend availability without assuming every PyTorch build exposes it."""
    owner = (
        getattr(torch_module, "cuda", None)
        if backend == "cuda"
        else getattr(getattr(torch_module, "backends", None), "mps", None)
    )
    check = getattr(owner, "is_available", None)
    if not callable(check):
        return False
    try:
        return bool(check())
    except Exception:
        return False


def _select_torch_device(torch_module: object) -> object:
    """Choose CUDA on Windows/Linux, MPS on macOS, then portable CPU.

    An explicit override is useful for reproducibility and for diagnosing driver
    issues. Explicit unavailable accelerators fail closed instead of silently
    running a long Maximum Accuracy job on CPU.
    """
    requested = os.environ.get(TORCH_DEVICE_ENV, "auto").strip().casefold()
    if requested not in {"auto", "cpu", "cuda", "mps"}:
        raise RuntimeError(
            f"{TORCH_DEVICE_ENV} phải là auto, cpu, cuda hoặc mps; nhận {requested!r}."
        )

    if requested == "cpu":
        return torch_module.device("cpu")
    if requested in {"cuda", "mps"}:
        if not _torch_backend_available(torch_module, requested):
            raise RuntimeError(
                f"Đã yêu cầu PyTorch {requested.upper()} nhưng runtime/driver hiện tại "
                "không cung cấp accelerator đó."
            )
        return torch_module.device(requested)

    if _torch_backend_available(torch_module, "cuda"):
        return torch_module.device("cuda")
    if _torch_backend_available(torch_module, "mps"):
        return torch_module.device("mps")
    return torch_module.device("cpu")


@dataclass(frozen=True)
class SongEmissions:
    values: np.ndarray
    times_us: np.ndarray


@dataclass(frozen=True)
class CTCModelSpec:
    id: str
    revision: str
    license: str
    files: tuple[str, ...]
    cache_name: str
    label: str
    weight: float
    min_confidence: float
    runtime_version: str


SPEECH_CTC_SPEC = CTCModelSpec(
    id=VIETNAMESE_MODEL_ID,
    revision=VIETNAMESE_MODEL_REVISION,
    license=VIETNAMESE_MODEL_LICENSE,
    files=VIETNAMESE_MODEL_FILES,
    cache_name="vietnamese-ctc",
    label="Vietnamese speech CTC",
    weight=1.0,
    min_confidence=0.10,
    runtime_version="stock-wav2vec2-ctc-v1",
)
LYRIC_CTC_SPEC = CTCModelSpec(
    id=LYRIC_MODEL_ID,
    revision=LYRIC_MODEL_REVISION,
    license=LYRIC_MODEL_LICENSE,
    files=LYRIC_MODEL_FILES,
    cache_name="vietnamese-lyric-ctc",
    label="Vietnamese singing lyric CTC",
    weight=1.2,
    min_confidence=0.03,
    runtime_version="lyric-feature-transform-v1",
)


class AlignerAdapter(ABC):
    id: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def align(self, timeline: TimelineV1, vocal_wav: Path, event: EventCallback) -> TimelineV1: ...


class EnergyAwareAligner(AlignerAdapter):
    id = "energy_aware"

    def available(self) -> bool:
        return True

    def align(self, timeline: TimelineV1, vocal_wav: Path, event: EventCallback) -> TimelineV1:
        audio, sample_rate = sf.read(vocal_wav, dtype="float32", always_2d=True)
        mono = audio.mean(axis=1)
        updated_lines: list[LineTiming] = []
        for index, line in enumerate(timeline.lines):
            event(
                0.63 + 0.16 * index / max(1, len(timeline.lines)),
                f"Đang căn dòng {index + 1}/{len(timeline.lines)}…",
            )
            if line.source == TimingSource.LRC_ENHANCED or _line_is_protected(line):
                updated_lines.append(line)
                continue
            updated_lines.append(self._align_line(line, mono, sample_rate))
        timeline.lines = updated_lines
        return timeline

    def _align_line(self, line: LineTiming, audio: np.ndarray, sample_rate: int) -> LineTiming:
        start_sample = max(0, round(line.start_us * sample_rate / 1_000_000))
        end_sample = min(len(audio), round(line.end_us * sample_rate / 1_000_000))
        segment = audio[start_sample:end_sample]
        if len(segment) < sample_rate // 20:
            return line
        window = max(1, round(sample_rate * 0.02))
        hop = max(1, round(sample_rate * 0.01))
        energies = np.array(
            [
                float(np.sqrt(np.mean(np.square(segment[position : position + window])) + 1e-12))
                for position in range(0, max(1, len(segment) - window + 1), hop)
            ]
        )
        if not len(energies) or float(energies.max()) < 1e-5:
            return line
        floor = float(np.percentile(energies, 25))
        ceiling = float(np.percentile(energies, 90))
        contrast = max(0.0, (ceiling - floor) / max(ceiling, 1e-6))
        threshold = floor + (ceiling - floor) * 0.22
        active = np.where(energies >= threshold)[0]
        if not len(active):
            return line
        active_start = line.start_us + round(active[0] * hop * 1_000_000 / sample_rate)
        active_end = line.start_us + round((active[-1] * hop + window) * 1_000_000 / sample_rate)
        active_start = max(line.start_us, min(line.end_us - 1, active_start))
        active_end = max(active_start + 1, min(line.end_us, active_end))

        start_bin = max(0, active[0])
        end_bin = min(len(energies), active[-1] + 1)
        active_energies = energies[start_bin:end_bin]
        smoothed = np.convolve(active_energies, np.array([0.25, 0.5, 0.25]), mode="same")
        normalized_energy = (smoothed - smoothed.min()) / max(
            1e-6, float(smoothed.max() - smoothed.min())
        )

        tokens: list[TokenTiming] = []
        # Vietnamese whitespace tokens are usually sung syllables. Square-root
        # weighting avoids giving long words an unrealistically large share.
        token_weights = [
            max(1.0, math.sqrt(max(1, len(normalize_token(token.text)))))
            for token in line.tokens
        ]
        total = sum(token_weights)
        cursor = active_start
        consumed = 0
        confidence = min(0.76, 0.48 + contrast * 0.26)
        for token_index, (token, token_weight) in enumerate(
            zip(line.tokens, token_weights, strict=True)
        ):
            consumed += token_weight
            if token_index == len(line.tokens) - 1:
                token_end = active_end
            else:
                target = consumed / total
                expected = round(target * max(0, len(normalized_energy) - 1))
                average_span = max(2, round(len(normalized_energy) / max(1, len(line.tokens))))
                radius = max(2, round(average_span * 0.45))
                lower = max(1, expected - radius)
                upper = min(len(normalized_energy) - 1, expected + radius)
                candidates = np.arange(lower, max(lower + 1, upper + 1))
                distance = np.abs(candidates - expected) / max(1, radius)
                score = 0.64 * distance + 0.36 * normalized_energy[candidates]
                energy_index = int(candidates[int(np.argmin(score))])
                token_end = line.start_us + round(
                    (start_bin + energy_index) * hop * 1_000_000 / sample_rate
                )
                token_end = max(cursor + 1, min(active_end, token_end))
            tokens.append(
                token.model_copy(
                    update={
                        "start_us": cursor,
                        "end_us": token_end,
                        "confidence": confidence,
                        "source": TimingSource.ENERGY,
                        "verified": False,
                    }
                )
            )
            cursor = token_end
        return line.model_copy(
            update={
                "confidence": confidence,
                "source": TimingSource.ENERGY,
                "tokens": tokens,
                "verified": False,
            }
        )


class VietnameseCTCAligner(AlignerAdapter):
    id = "vietnamese_ctc"

    def __init__(self, settings: Settings, spec: CTCModelSpec = SPEECH_CTC_SPEC):
        self.settings = settings
        self.spec = spec

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            return True
        except ImportError:
            return False

    def align(self, timeline: TimelineV1, vocal_wav: Path, event: EventCallback) -> TimelineV1:
        processor, model, device = self.runtime(event)
        audio, sample_rate = sf.read(vocal_wav, dtype="float32", always_2d=True)
        mono = audio.mean(axis=1)
        fallback = EnergyAwareAligner()
        updated: list[LineTiming] = []
        audio_duration_us = round(len(mono) * 1_000_000 / sample_rate)
        fallback_count = 0
        ctc_count = 0
        for index, line in enumerate(timeline.lines):
            event(
                0.64 + 0.16 * index / max(1, len(timeline.lines)),
                f"CTC đang căn dòng {index + 1}/{len(timeline.lines)}…",
            )
            if line.source == TimingSource.LRC_ENHANCED or _line_is_protected(line):
                updated.append(line)
                continue
            try:
                search_start_us, search_end_us = _line_search_window(
                    timeline.lines, index, audio_duration_us
                )
                updated.append(
                    self._align_line(
                        line,
                        mono,
                        sample_rate,
                        processor,
                        model,
                        device,
                        search_start_us=search_start_us,
                        search_end_us=search_end_us,
                    )
                )
                ctc_count += 1
            except Exception:
                fallback_count += 1
                updated.append(fallback._align_line(line, mono, sample_rate))
        timeline.lines = _regularize_line_sequence(updated)
        if fallback_count:
            event(
                0.805,
                f"CTC căn được {ctc_count}/{ctc_count + fallback_count} câu cần phân tích; "
                f"{fallback_count} câu không đủ bằng chứng đã hạ confidence để kiểm duyệt.",
            )
        return timeline

    def runtime(self, event: EventCallback) -> tuple[object, object, object]:
        key = (
            f"{self.settings.data_dir.resolve()}::{self.spec.id}@{self.spec.revision}"
            f"::{self.spec.runtime_version}"
        )
        with _CTC_RUNTIME_LOCK:
            cached = _CTC_RUNTIMES.get(key)
            if cached is not None:
                event(0.60, "Vietnamese CTC đã sẵn sàng trong bộ nhớ.")
                return cached

            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            import torch
            from huggingface_hub import snapshot_download
            from transformers import (
                AutoFeatureExtractor,
                AutoModelForCTC,
                AutoProcessor,
                AutoTokenizer,
                Wav2Vec2Processor,
            )

            event(0.60, f"Đang nạp {self.spec.label} (CC-BY-NC-4.0)…")
            snapshot = Path(
                snapshot_download(
                    repo_id=self.spec.id,
                    revision=self.spec.revision,
                    cache_dir=self.settings.data_dir / "models" / "huggingface",
                    allow_patterns=list(self.spec.files),
                )
            )
            self._write_snapshot_manifest(snapshot)
            try:
                processor = AutoProcessor.from_pretrained(snapshot)
            except (OSError, ValueError):
                processor = Wav2Vec2Processor(
                    feature_extractor=AutoFeatureExtractor.from_pretrained(snapshot),
                    tokenizer=AutoTokenizer.from_pretrained(snapshot),
                )
            if self.spec == LYRIC_CTC_SPEC:
                from .lyric_model import LyricWav2Vec2ForCTC

                model = LyricWav2Vec2ForCTC.from_pretrained(snapshot)
            else:
                model = AutoModelForCTC.from_pretrained(snapshot)
            device = _select_torch_device(torch)
            model.to(device).eval()
            runtime = (processor, model, device)
            _CTC_RUNTIMES[key] = runtime
            return runtime

    def _write_snapshot_manifest(self, snapshot: Path) -> None:
        file_hashes: dict[str, str] = {}
        combined = hashlib.sha256()
        for relative in self.spec.files:
            path = snapshot / relative
            if not path.is_file():
                continue
            relative = path.relative_to(snapshot).as_posix()
            digest = sha256_file(path)
            file_hashes[relative] = digest
            combined.update(relative.encode())
            combined.update(digest.encode())
        manifest = {
            "schema_version": "1.0",
            "id": self.spec.id,
            "revision": self.spec.revision,
            "license": self.spec.license,
            "runtime_version": self.spec.runtime_version,
            "snapshot_sha256": combined.hexdigest(),
            "files": file_hashes,
        }
        path = self.settings.data_dir / "models" / f"{self.spec.cache_name}-manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def song_emissions(
        self,
        audio: np.ndarray,
        sample_rate: int,
        processor,
        model,
        device,
        cache_path: Path,
    ) -> SongEmissions:
        """Create one reusable, overlapping emission timeline for the whole song."""
        import torch

        if cache_path.is_file():
            try:
                cached = np.load(cache_path, allow_pickle=False)
                values = cached["emissions"].astype(np.float32, copy=False)
                times_us = cached["times_us"].astype(np.int64, copy=False)
                if values.ndim == 2 and len(values) == len(times_us) and len(values):
                    return SongEmissions(values=values, times_us=times_us)
            except (OSError, ValueError, KeyError):
                pass

        model_sample_rate = int(processor.feature_extractor.sampling_rate)
        if sample_rate != model_sample_rate:
            from scipy.signal import resample_poly

            divisor = math.gcd(sample_rate, model_sample_rate)
            audio = resample_poly(
                audio,
                model_sample_rate // divisor,
                sample_rate // divisor,
            ).astype(np.float32)
            sample_rate = model_sample_rate

        chunk_samples = SONG_CHUNK_SECONDS * sample_rate
        overlap_samples = SONG_CHUNK_OVERLAP_SECONDS * sample_rate
        step_samples = max(1, chunk_samples - overlap_samples)
        emissions: list[np.ndarray] = []
        frame_times: list[np.ndarray] = []
        total_samples = len(audio)
        starts = list(range(0, max(1, total_samples), step_samples))
        for chunk_index, start in enumerate(starts):
            end = min(total_samples, start + chunk_samples)
            segment = audio[start:end]
            if not len(segment):
                continue
            inputs = processor(segment, sampling_rate=sample_rate, return_tensors="pt")
            with torch.inference_mode():
                values = torch.log_softmax(
                    model(inputs.input_values.to(device)).logits[0], dim=-1
                ).cpu().numpy().astype(np.float32)
            if not len(values):
                continue
            chunk_start_us = round(start * 1_000_000 / sample_rate)
            chunk_end_us = round(end * 1_000_000 / sample_rate)
            frame_us = (chunk_end_us - chunk_start_us) / len(values)
            times = chunk_start_us + (np.arange(len(values), dtype=np.float64) + 0.5) * frame_us
            keep_start_us = (
                chunk_start_us
                if chunk_index == 0
                else chunk_start_us + SONG_CHUNK_OVERLAP_SECONDS * 500_000
            )
            keep_end_us = (
                chunk_end_us
                if end >= total_samples
                else chunk_end_us - SONG_CHUNK_OVERLAP_SECONDS * 500_000
            )
            keep = (times >= keep_start_us) & (times < keep_end_us)
            emissions.append(values[keep])
            frame_times.append(np.rint(times[keep]).astype(np.int64))
            if end >= total_samples:
                break
        if not emissions:
            raise ValueError("Model không tạo được emission cho toàn bài.")
        joined = np.concatenate(emissions, axis=0)
        joined_times = np.concatenate(frame_times, axis=0)
        order = np.argsort(joined_times, kind="stable")
        joined = joined[order]
        joined_times = joined_times[order]
        unique = np.concatenate(([True], np.diff(joined_times) > 0))
        result = SongEmissions(joined[unique], joined_times[unique])
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp.npz")
        np.savez_compressed(
            temporary,
            emissions=result.values.astype(np.float32),
            times_us=result.times_us.astype(np.int64),
        )
        temporary.replace(cache_path)
        return result

    def _align_line(
        self,
        line: LineTiming,
        audio: np.ndarray,
        sample_rate: int,
        processor,
        model,
        device,
        search_start_us: int | None = None,
        search_end_us: int | None = None,
        emission_cache_path: Path | None = None,
    ) -> LineTiming:
        aligned, _trace = self._align_line_with_trace(
            line,
            audio,
            sample_rate,
            processor,
            model,
            device,
            search_start_us=search_start_us,
            search_end_us=search_end_us,
            emission_cache_path=emission_cache_path,
        )
        return aligned

    def _align_line_with_trace(
        self,
        line: LineTiming,
        audio: np.ndarray,
        sample_rate: int,
        processor,
        model,
        device,
        search_start_us: int | None = None,
        search_end_us: int | None = None,
        emission_cache_path: Path | None = None,
        song_emissions: SongEmissions | None = None,
    ) -> tuple[LineTiming, dict[str, list[GraphemeSpan]]]:
        import torch

        segment_start_us = line.start_us if search_start_us is None else search_start_us
        segment_end_us = line.end_us if search_end_us is None else search_end_us
        start_sample = max(0, round(segment_start_us * sample_rate / 1_000_000))
        end_sample = min(len(audio), round(segment_end_us * sample_rate / 1_000_000))
        segment = audio[start_sample:end_sample]
        if not len(segment):
            raise ValueError("empty segment")
        model_sample_rate = int(processor.feature_extractor.sampling_rate)
        if sample_rate != model_sample_rate:
            from scipy.signal import resample_poly

            divisor = math.gcd(sample_rate, model_sample_rate)
            segment = resample_poly(
                segment,
                model_sample_rate // divisor,
                sample_rate // divisor,
            ).astype(np.float32)
        emissions = None
        emission_times_us: np.ndarray | None = None
        if song_emissions is not None:
            selected = (song_emissions.times_us >= segment_start_us) & (
                song_emissions.times_us < segment_end_us
            )
            emissions = torch.from_numpy(song_emissions.values[selected])
            emission_times_us = song_emissions.times_us[selected]
        elif emission_cache_path is not None and emission_cache_path.is_file():
            try:
                cached = np.load(emission_cache_path, allow_pickle=False)
                emissions = torch.from_numpy(cached["emissions"])
            except (OSError, ValueError, KeyError):
                emissions = None
        if emissions is None:
            inputs = processor(
                segment, sampling_rate=model_sample_rate, return_tensors="pt"
            )
            input_values = inputs.input_values.to(device)
            with torch.inference_mode():
                emissions = torch.log_softmax(model(input_values).logits[0], dim=-1).cpu()
            if emission_cache_path is not None:
                emission_cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = emission_cache_path.with_suffix(".tmp.npz")
                np.savez_compressed(temporary, emissions=emissions.numpy().astype(np.float32))
                temporary.replace(emission_cache_path)

        tokenizer = processor.tokenizer
        target_ids: list[int] = []
        word_ranges: list[tuple[int, int]] = []
        grapheme_ranges: list[list[tuple[int, str, int, int]]] = []
        delimiter = tokenizer.word_delimiter_token_id
        for word_index, token in enumerate(line.tokens):
            normalized = _ctc_normalize(token.text)
            ids = tokenizer(normalized, add_special_tokens=False).input_ids
            if tokenizer.unk_token_id is not None and tokenizer.unk_token_id in ids:
                raise ValueError(f"CTC không có ký tự cho token: {token.text}")
            if not ids:
                raise ValueError("unknown token")
            start = len(target_ids)
            target_ids.extend(ids)
            word_ranges.append((start, len(target_ids)))
            grapheme_ranges.append(
                [
                    (display_index, text, start + range_start, start + range_end)
                    for display_index, text, range_start, range_end in _grapheme_id_ranges(
                        token.text, tokenizer, ids
                    )
                ]
            )
            if word_index + 1 < len(line.tokens) and delimiter is not None:
                target_ids.append(delimiter)

        blank_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else 0
        spans, scores = _ctc_path(emissions, target_ids, blank_id)
        frame_us = (segment_end_us - segment_start_us) / max(1, emissions.shape[0])

        def frame_time(index: int, end: bool = False) -> int:
            if emission_times_us is None:
                return segment_start_us + round((index + (1 if end else 0)) * frame_us)
            if not len(emission_times_us):
                raise ValueError("Cửa sổ emission toàn bài không có frame.")
            index = max(0, min(len(emission_times_us) - 1, index))
            if not end:
                return int(emission_times_us[index])
            if index + 1 < len(emission_times_us):
                return int(emission_times_us[index + 1])
            step = int(np.median(np.diff(emission_times_us))) if len(emission_times_us) > 1 else 20_000
            return int(emission_times_us[index]) + max(1, step)

        aligned_tokens: list[TokenTiming] = []
        trace: dict[str, list[GraphemeSpan]] = {}
        for word_index, (range_start, range_end) in enumerate(word_ranges):
            word_spans = [spans[index] for index in range(range_start, range_end)]
            token_start = frame_time(min(span[0] for span in word_spans))
            if word_index + 1 < len(word_ranges):
                next_start = min(spans[index][0] for index in range(*word_ranges[word_index + 1]))
                token_end = frame_time(next_start)
            else:
                token_end = frame_time(max(span[1] for span in word_spans) - 1, end=True)
            token_end = max(token_start + 1, min(segment_end_us, token_end))
            confidence = float(np.mean([scores[index] for index in range(range_start, range_end)]))
            aligned_tokens.append(
                line.tokens[word_index].model_copy(
                    update={
                        "start_us": token_start,
                        "end_us": token_end,
                        "confidence": min(1.0, max(0.0, confidence)),
                        "source": TimingSource.CTC,
                        "verified": False,
                    }
                )
            )
            token_trace: list[GraphemeSpan] = []
            for display_index, text, grapheme_start, grapheme_end in grapheme_ranges[word_index]:
                indexes = range(grapheme_start, grapheme_end)
                occupied = [spans[index] for index in indexes]
                grapheme_start_us = frame_time(min(span[0] for span in occupied))
                grapheme_end_us = frame_time(max(span[1] for span in occupied) - 1, end=True)
                token_trace.append(
                    GraphemeSpan(
                        grapheme_index=display_index,
                        text=text,
                        start_us=max(token_start, grapheme_start_us),
                        end_us=max(grapheme_start_us + 1, min(token_end, grapheme_end_us)),
                        confidence=float(np.mean([scores[index] for index in indexes])),
                    )
                )
            trace[line.tokens[word_index].id] = token_trace
        line_confidence = float(np.mean([token.confidence for token in aligned_tokens]))
        return line.model_copy(
            update={
                "start_us": aligned_tokens[0].start_us,
                "end_us": aligned_tokens[-1].end_us,
                "tokens": aligned_tokens,
                "confidence": line_confidence,
                "source": TimingSource.CTC,
                "verified": False,
            }
        ), trace


def _grapheme_id_ranges(text: str, tokenizer, token_ids: list[int]) -> list[tuple[int, str, int, int]]:
    display = split_graphemes(text)
    sung = [
        (index, value, _ctc_normalize(value))
        for index, (_start, _end, value) in enumerate(display)
        if is_sung_grapheme(value) and _ctc_normalize(value)
    ]
    if not sung or not token_ids:
        return []

    isolated: list[int] = []
    isolated_ranges: list[tuple[int, str, int, int]] = []
    for display_index, value, normalized in sung:
        ids = tokenizer(normalized, add_special_tokens=False).input_ids
        start = len(isolated)
        isolated.extend(ids)
        isolated_ranges.append((display_index, value, start, len(isolated)))
    if isolated == token_ids and all(end > start for _index, _value, start, end in isolated_ranges):
        return isolated_ranges

    # Slow tokenizers used by the pinned Vietnamese models are character based.
    # If a future tokenizer merges context, retain the exact lyric contract and
    # distribute model labels monotonically across visible graphemes.
    weights = [max(1, len(normalized)) for _index, _value, normalized in sung]
    total = sum(weights)
    result: list[tuple[int, str, int, int]] = []
    consumed = 0
    cursor = 0
    for local_index, ((display_index, value, _normalized), weight) in enumerate(
        zip(sung, weights, strict=True)
    ):
        consumed += weight
        end = (
            len(token_ids)
            if local_index == len(sung) - 1
            else max(cursor + 1, round(consumed * len(token_ids) / total))
        )
        end = min(len(token_ids), end)
        if end > cursor:
            result.append((display_index, value, cursor, end))
        cursor = end
    return result


def _ctc_path(
    emission, tokens: list[int], blank_id: int
) -> tuple[list[tuple[int, int]], list[float]]:
    """Viterbi-align a CTC target, including mandatory blanks for repeats.

    Prefix and suffix audio are free, so an LRC search window may include
    instrumental lead-in/out without pulling the first word to the window edge.
    Returned spans use half-open emission-frame indexes.
    """
    if hasattr(emission, "detach"):
        emission = emission.detach().cpu().numpy()
    log_probs = np.asarray(emission, dtype=np.float64)
    if log_probs.ndim != 2:
        raise ValueError("CTC emission phải có dạng [frames, vocabulary].")
    frames, vocabulary = log_probs.shape
    target_count = len(tokens)
    if target_count == 0 or frames < target_count:
        raise ValueError("CTC segment quá ngắn.")
    if blank_id < 0 or blank_id >= vocabulary or any(
        token < 0 or token >= vocabulary for token in tokens
    ):
        raise ValueError("CTC token id nằm ngoài vocabulary.")

    labels = [blank_id]
    for token in tokens:
        labels.extend((token, blank_id))
    state_count = len(labels)
    previous = np.full(state_count, -np.inf, dtype=np.float64)
    previous[0] = 0.0
    backtrack = np.full((frames, state_count), -1, dtype=np.int16)
    best_end_score = -np.inf
    best_end_frame = -1
    best_end_state = -1

    for frame in range(frames):
        current = np.full(state_count, -np.inf, dtype=np.float64)
        for state, label in enumerate(labels):
            candidates: list[tuple[float, int]] = [(previous[state], state)]
            if state > 0:
                candidates.append((previous[state - 1], state - 1))
            if (
                state > 1
                and state % 2 == 1
                and labels[state] != labels[state - 2]
            ):
                candidates.append((previous[state - 2], state - 2))
            score, predecessor = max(candidates, key=lambda candidate: candidate[0])
            if np.isfinite(score):
                current[state] = score + log_probs[frame, label]
                backtrack[frame, state] = predecessor

        # Do not charge blank probability for arbitrary music before the lyric.
        if current[0] < 0.0:
            current[0] = 0.0
            backtrack[frame, 0] = 0

        for end_state in ({state_count - 2, state_count - 1} if target_count else {0}):
            if current[end_state] > best_end_score:
                best_end_score = current[end_state]
                best_end_frame = frame
                best_end_state = end_state
        previous = current

    if best_end_frame < 0 or not np.isfinite(best_end_score):
        raise ValueError("CTC không tìm được đường forced alignment.")

    token_frames: list[list[int]] = [[] for _ in tokens]
    state = best_end_state
    for frame in range(best_end_frame, -1, -1):
        if state % 2 == 1:
            token_frames[state // 2].append(frame)
        predecessor = int(backtrack[frame, state])
        if predecessor < 0:
            break
        state = predecessor

    if any(not occupied for occupied in token_frames):
        raise ValueError("CTC backtrack không phủ hết target.")
    spans: list[tuple[int, int]] = []
    scores: list[float] = []
    for token_id, occupied in zip(tokens, token_frames, strict=True):
        occupied.sort()
        spans.append((occupied[0], occupied[-1] + 1))
        probabilities = np.exp(log_probs[occupied, token_id])
        scores.append(float(np.mean(probabilities)))
    return spans, scores


def _ctc_normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text).casefold()
    characters = [
        character if unicodedata.category(character)[0] in {"L", "N"} else " "
        for character in normalized
    ]
    return " ".join("".join(characters).split())


def _line_is_protected(line: LineTiming) -> bool:
    return bool(
        line.locked
        or line.verified
        or any(
            token.locked or token.verified or token.source == TimingSource.MANUAL
            for token in line.tokens
        )
    )


def _line_search_window(
    lines: list[LineTiming], index: int, audio_duration_us: int
) -> tuple[int, int]:
    line = lines[index]
    anchor_duration = max(1, line.end_us - line.start_us)
    padding = min(CTC_SEARCH_PADDING_US, max(300_000, anchor_duration // 3))
    start_us = max(0, line.start_us - padding)
    end_us = min(audio_duration_us, line.end_us + padding)
    if index > 0:
        start_us = max(start_us, lines[index - 1].start_us)
    if index + 1 < len(lines):
        end_us = min(end_us, lines[index + 1].end_us)
    if end_us <= start_us:
        raise ValueError("Cửa sổ CTC của câu không hợp lệ.")
    return start_us, end_us


def _regularize_line_sequence(lines: list[LineTiming]) -> list[LineTiming]:
    """Resolve tiny cross-line overlaps while preserving human-locked timing."""
    result = [line.model_copy(deep=True) for line in lines]
    for previous, current in zip(result, result[1:], strict=False):
        if previous.end_us <= current.start_us:
            continue
        previous_last = previous.tokens[-1]
        current_first = current.tokens[0]
        lower = previous_last.start_us + 1
        upper = current_first.end_us - 1
        if lower > upper:
            previous.confidence = min(previous.confidence, 0.70)
            current.confidence = min(current.confidence, 0.70)
            continue
        if _line_is_protected(previous):
            boundary = min(upper, previous.end_us)
        elif _line_is_protected(current):
            boundary = max(lower, current.start_us)
        else:
            boundary = max(
                lower,
                min(upper, round((previous.end_us + current.start_us) / 2)),
            )
        previous_last.end_us = boundary
        previous.end_us = boundary
        current_first.start_us = boundary
        current.start_us = boundary
    return result


def _regularize_token_sequence(line: LineTiming, tolerance_us: int) -> LineTiming:
    result = line.model_copy(deep=True)
    for previous, current in zip(result.tokens, result.tokens[1:], strict=False):
        delta_us = abs(previous.end_us - current.start_us)
        total_confidence = max(1e-6, previous.confidence + current.confidence)
        boundary = round(
            (
                previous.end_us * previous.confidence
                + current.start_us * current.confidence
            )
            / total_confidence
        )
        boundary = max(previous.start_us + 1, min(current.end_us - 1, boundary))
        previous.end_us = boundary
        current.start_us = boundary
        if delta_us > tolerance_us:
            previous.confidence = min(previous.confidence, 0.77)
            current.confidence = min(current.confidence, 0.77)
    result.start_us = result.tokens[0].start_us
    result.end_us = result.tokens[-1].end_us
    result.confidence = float(np.mean([token.confidence for token in result.tokens]))
    return result


def merge_ctc_consensus(
    primary: TimelineV1, secondary: TimelineV1
) -> tuple[TimelineV1, dict[str, int | str]]:
    if len(primary.lines) != len(secondary.lines):
        raise ValueError("Hai timeline consensus không có cùng số dòng.")
    tolerance_us = round(
        2_000_000 * primary.fps_denominator / primary.fps_numerator
    )
    merged = primary.model_copy(deep=True)
    agreed = 0
    review = 0
    max_delta_us = 0
    for merged_line, primary_line, secondary_line in zip(
        merged.lines, primary.lines, secondary.lines, strict=True
    ):
        if primary_line.id != secondary_line.id or len(primary_line.tokens) != len(
            secondary_line.tokens
        ):
            raise ValueError("Hai timeline consensus không cùng token contract.")
        merged_tokens: list[TokenTiming] = []
        for primary_token, secondary_token in zip(
            primary_line.tokens, secondary_line.tokens, strict=True
        ):
            if primary_token.id != secondary_token.id:
                raise ValueError("Hai timeline consensus bị lệch token id.")
            delta_us = max(
                abs(primary_token.start_us - secondary_token.start_us),
                abs(primary_token.end_us - secondary_token.end_us),
            )
            max_delta_us = max(max_delta_us, delta_us)
            ctc_pair = (
                primary_token.source == TimingSource.CTC
                and secondary_token.source == TimingSource.CTC
                and min(primary_token.confidence, secondary_token.confidence)
                >= MIN_CTC_TOKEN_CONFIDENCE
            )
            if ctc_pair and delta_us <= tolerance_us:
                merged_tokens.append(
                    primary_token.model_copy(
                        update={
                            "start_us": round(
                                (primary_token.start_us + secondary_token.start_us) / 2
                            ),
                            "end_us": round(
                                (primary_token.end_us + secondary_token.end_us) / 2
                            ),
                            "confidence": max(
                                0.90, primary_token.confidence, secondary_token.confidence
                            ),
                            "verified": False,
                        }
                    )
                )
                agreed += 1
            else:
                selected = max(
                    (primary_token, secondary_token), key=lambda token: token.confidence
                )
                merged_tokens.append(
                    selected.model_copy(
                        update={"confidence": min(0.77, selected.confidence), "verified": False}
                    )
                )
                review += 1
        merged_line.tokens = merged_tokens
        merged_line.start_us = merged_tokens[0].start_us
        merged_line.end_us = merged_tokens[-1].end_us
        merged_line.confidence = float(
            np.mean([token.confidence for token in merged_tokens])
        )
        merged_line.source = (
            TimingSource.CTC
            if all(token.source == TimingSource.CTC for token in merged_tokens)
            else primary_line.source
        )
        merged_line.verified = False
        regularized = _regularize_token_sequence(merged_line, tolerance_us)
        merged_line.start_us = regularized.start_us
        merged_line.end_us = regularized.end_us
        merged_line.confidence = regularized.confidence
        merged_line.tokens = regularized.tokens
    merged.lines = _regularize_line_sequence(merged.lines)
    return merged, {
        "policy": CONSENSUS_POLICY,
        "tolerance_us": tolerance_us,
        "auto_accepted_tokens": agreed,
        "review_required_tokens": review,
        "max_delta_us": max_delta_us,
    }


def suggest_line_timing(
    line: LineTiming,
    vocal_inputs: list[tuple[str, Path]],
    accept_noncommercial_license: bool,
    settings: Settings,
    alignment_profile: str = "maximum",
    motion_profile: str = "vocal_hybrid",
    karaoke_font: str | None = None,
) -> TimingSuggestionResponse:
    """Return a read-only proposal generated by the same production ensemble."""
    if not vocal_inputs:
        raise ValueError("Project chưa có vocal stem để gợi ý timing.")
    alignment_profile = (
        alignment_profile
        if alignment_profile in {"maximum", "balanced", "fast"}
        else "maximum"
    )
    motion_profile = (
        motion_profile
        if motion_profile in {"vocal_hybrid", "vocal_only", "linear"}
        else "vocal_hybrid"
    )
    used_inputs = vocal_inputs[: 1 if alignment_profile == "fast" else 2]
    if accept_noncommercial_license:
        from .ensemble import align_timeline_ensemble

        duration_us = max(
            line.end_us + 1,
            *[
                round(sf.info(path).frames * 1_000_000 / sf.info(path).samplerate)
                for _candidate_id, path in used_inputs
            ],
        )
        unlocked = line.model_copy(deep=True)
        unlocked.locked = False
        unlocked.verified = False
        unlocked.source = TimingSource.LRC_LINE
        for token in unlocked.tokens:
            token.locked = False
            token.verified = False
            token.source = TimingSource.LRC_LINE
        base = TimelineV1(
            duration_us=duration_us,
            fps_numerator=60,
            fps_denominator=1,
            metadata={"karaoke_font": karaoke_font or "noto_sans"},
            lines=[unlocked],
        )
        vocal_map = dict(used_inputs)
        vocal_hashes = {candidate_id: sha256_file(path) for candidate_id, path in used_inputs}
        proposal_timeline, evidence, _report = align_timeline_ensemble(
            base,
            vocal_map,
            vocal_hashes,
            True,
            alignment_profile,
            lambda _progress, _message: None,
            settings,
            settings.data_dir / "models" / "suggestion-emissions",
            motion_profile=motion_profile,
        )
        restored = proposal_timeline.lines[0]
        evidence_by_token = {item.token_id: item for item in evidence.tokens}
    else:
        mono, sample_rate = _read_line_audio(used_inputs[0][1], line)
        localized = _localize_line(line)
        proposal = EnergyAwareAligner()._align_line(
            localized.model_copy(deep=True), mono, sample_rate
        )
        restored = _restore_line(proposal, line.start_us)
        evidence_by_token = {}
    font_path = resolve_font(settings, karaoke_font)
    for token_index, token in enumerate(restored.tokens):
        if token.sweep is None:
            token.sweep = linear_sweep(
                restored,
                token_index,
                "energy_linear",
                min(0.76, token.confidence),
                False,
                font_path,
            )
    return TimingSuggestionResponse(
        line_id=line.id,
        source=restored.source,
        confidence=restored.confidence,
        used_vocal_stems=[candidate_id for candidate_id, _path in used_inputs],
        license_accepted=accept_noncommercial_license,
        license_required_for_ctc=not accept_noncommercial_license,
        alignment_profile=alignment_profile,
        motion_profile=motion_profile,
        tokens=[
            TokenTimingSuggestion(
                token_id=suggested.id,
                text=suggested.text,
                start_us=suggested.start_us,
                end_us=suggested.end_us,
                confidence=suggested.confidence,
                source=suggested.source,
                delta_start_us=suggested.start_us - current.start_us,
                delta_end_us=suggested.end_us - current.end_us,
                consensus=evidence_by_token.get(suggested.id).auto_accepted
                if suggested.id in evidence_by_token
                else False,
                reason_codes=evidence_by_token.get(suggested.id).reason_codes
                if suggested.id in evidence_by_token
                else [],
                candidates=evidence_by_token.get(suggested.id).candidates
                if suggested.id in evidence_by_token
                else [],
                sweep=suggested.sweep,
            )
            for current, suggested in zip(line.tokens, restored.tokens, strict=True)
        ],
    )


def _read_line_audio(vocal_wav: Path, line: LineTiming) -> tuple[np.ndarray, int]:
    with sf.SoundFile(vocal_wav) as handle:
        sample_rate = handle.samplerate
        start = max(0, round(line.start_us * sample_rate / 1_000_000))
        end = min(len(handle), round(line.end_us * sample_rate / 1_000_000))
        if end <= start:
            raise ValueError("Khoảng audio của câu không hợp lệ.")
        handle.seek(start)
        audio = handle.read(end - start, dtype="float32", always_2d=True)
    return audio.mean(axis=1), sample_rate


def _read_audio_window(
    vocal_wav: Path, line: LineTiming, padding_us: int
) -> tuple[np.ndarray, int, int, int]:
    with sf.SoundFile(vocal_wav) as handle:
        sample_rate = handle.samplerate
        audio_duration_us = round(len(handle) * 1_000_000 / sample_rate)
        window_start_us = max(0, line.start_us - padding_us)
        window_end_us = min(audio_duration_us, line.end_us + padding_us)
        start = round(window_start_us * sample_rate / 1_000_000)
        end = round(window_end_us * sample_rate / 1_000_000)
        if end <= start:
            raise ValueError("Cửa sổ audio của câu không hợp lệ.")
        handle.seek(start)
        audio = handle.read(end - start, dtype="float32", always_2d=True)
    return audio.mean(axis=1), sample_rate, window_start_us, window_end_us


def _localize_line(line: LineTiming) -> LineTiming:
    duration_us = max(1, line.end_us - line.start_us)
    return line.model_copy(
        deep=True,
        update={
            "start_us": 0,
            "end_us": duration_us,
            "tokens": [
                token.model_copy(
                    update={
                        "start_us": max(0, token.start_us - line.start_us),
                        "end_us": max(1, token.end_us - line.start_us),
                    }
                )
                for token in line.tokens
            ],
        },
    )


def _restore_line(line: LineTiming, offset_us: int) -> LineTiming:
    return line.model_copy(
        deep=True,
        update={
            "start_us": line.start_us + offset_us,
            "end_us": line.end_us + offset_us,
            "tokens": [
                token.model_copy(
                    update={
                        "start_us": token.start_us + offset_us,
                        "end_us": token.end_us + offset_us,
                    }
                )
                for token in line.tokens
            ],
        },
    )


def _shift_line(line: LineTiming, offset_us: int) -> LineTiming:
    return line.model_copy(
        deep=True,
        update={
            "start_us": line.start_us + offset_us,
            "end_us": line.end_us + offset_us,
            "tokens": [
                token.model_copy(
                    update={
                        "start_us": token.start_us + offset_us,
                        "end_us": token.end_us + offset_us,
                    }
                )
                for token in line.tokens
            ],
        },
    )


def align_timeline(
    timeline: TimelineV1,
    vocal_wav: Path,
    accept_noncommercial_license: bool,
    event: EventCallback,
    settings: Settings,
) -> TimelineV1:
    ctc = VietnameseCTCAligner(settings)
    if accept_noncommercial_license and ctc.available():
        try:
            return ctc.align(timeline, vocal_wav, event)
        except Exception as exc:
            event(
                0.61,
                "Vietnamese CTC không khả dụng trong lần chạy này "
                f"({type(exc).__name__}); chuyển sang energy-aware và đưa mọi điểm yếu vào kiểm duyệt.",
            )
    event(0.61, "Dùng energy-aware alignment; các điểm yếu sẽ được đưa vào hàng kiểm duyệt.")
    return EnergyAwareAligner().align(timeline, vocal_wav, event)
