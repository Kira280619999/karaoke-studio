from __future__ import annotations

import gc
import importlib.util
import os
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import numpy as np
import soundfile as sf

from .media import MediaError, run, sha256_file

POLARFORMER_MODEL_ID = "bgkb/bs_polarformer"
POLARFORMER_REVISION = "9158719ee2173edd480a735764627526506fe4af"
POLARFORMER_FILENAME = "bs_polarformer.onnx"
POLARFORMER_SHA256 = "1c6857c34556c72d4094d4515c5725549bf987a63a1a8c37a7e7fc111b525c50"
POLARFORMER_BYTES = 210_652_828
POLARFORMER_URL = (
    f"https://huggingface.co/{POLARFORMER_MODEL_ID}/resolve/"
    f"{POLARFORMER_REVISION}/{POLARFORMER_FILENAME}"
)

SAMPLE_RATE = 44_100
N_FFT = 2_048
HOP_LENGTH = 512
WIN_LENGTH = 2_048
# Benchmarked on an 18-core M5 Pro and bounded for a 24-GB deployment target.
# Six seconds is faster per useful audio second than four or eight seconds while
# staying below 5 GiB RSS for the model process. A 500 ms equal-power overlap is
# long enough to hide estimator boundaries without spending 25% of inference on
# duplicate audio. CoreML EP is intentionally not selected: this graph partitions
# into 125 CoreML subgraphs and peaked near 19 GiB RSS even with a compiled cache.
STREAM_CHUNK_SECONDS = 6
STREAM_OVERLAP_MILLISECONDS = 500
STREAM_MAX_CHUNK_SECONDS = 6
ORT_MAX_THREADS = 10

ProgressCallback = Callable[[float, str], None]


def polarformer_dependencies_available() -> bool:
    return all(
        importlib.util.find_spec(package) is not None
        for package in ("onnxruntime", "torch")
    )


def polarformer_model_path(data_dir: Path) -> Path:
    return data_dir / "models" / "bs-polarformer" / POLARFORMER_FILENAME


def ensure_polarformer_model(
    data_dir: Path,
    progress: ProgressCallback | None = None,
) -> Path:
    destination = polarformer_model_path(data_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _model_is_valid(destination):
        return destination

    partial = destination.with_suffix(destination.suffix + ".part")
    downloaded = partial.stat().st_size if partial.is_file() else 0
    if downloaded >= POLARFORMER_BYTES:
        partial.unlink(missing_ok=True)
        downloaded = 0

    headers = {"User-Agent": "Karaoke-Studio/0.1.0"}
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"
    request = urllib.request.Request(POLARFORMER_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            resumed = downloaded > 0 and getattr(response, "status", 200) == 206
            mode = "ab" if resumed else "wb"
            if not resumed:
                downloaded = 0
            with partial.open(mode) as handle:
                while chunk := response.read(4 * 1024 * 1024):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(
                            min(1.0, downloaded / POLARFORMER_BYTES),
                            f"Đang tải BS PolarFormer FP32: {downloaded / 1_048_576:.1f} / "
                            f"{POLARFORMER_BYTES / 1_048_576:.1f} MiB",
                        )
    except Exception as exc:
        raise MediaError(
            "Không tải được BS PolarFormer FP32; file tạm đã được giữ để tiếp tục lần sau."
        ) from exc

    if not _model_is_valid(partial):
        raise MediaError("BS PolarFormer FP32 tải xong nhưng sai kích thước hoặc SHA-256.")
    os.replace(partial, destination)
    return destination


def separate_with_polarformer(
    mix: Path,
    model_path: Path,
    instrumental_path: Path,
    vocals_path: Path,
    progress: ProgressCallback | None = None,
    ffmpeg: str = "ffmpeg",
) -> None:
    if not polarformer_dependencies_available():
        raise MediaError("Thiếu onnxruntime hoặc torch cho BS PolarFormer.")
    if not _model_is_valid(model_path):
        raise MediaError("Checkpoint BS PolarFormer FP32 chưa hợp lệ.")

    import onnxruntime as ort
    import torch

    chunk_seconds = _stream_chunk_seconds()
    chunk_size = chunk_seconds * SAMPLE_RATE
    overlap_size = _stream_overlap_samples(chunk_size)
    step = chunk_size - overlap_size
    threads = _polarformer_threads()
    time_frames = chunk_size // HOP_LENGTH + 1
    session = _create_session(ort, model_path, time_frames=time_frames, threads=threads)
    _configure_torch(torch, threads)
    stft_window = torch.hann_window(WIN_LENGTH)
    stft_options = {
        "n_fft": N_FFT,
        "hop_length": HOP_LENGTH,
        "win_length": WIN_LENGTH,
        "normalized": False,
        "center": True,
    }

    instrumental_path.parent.mkdir(parents=True, exist_ok=True)
    vocals_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_mix = instrumental_path.parent / ".polarformer-input-44100.wav"
    instrumental_partial = instrumental_path.parent / ".instrumental.polarformer.part.wav"
    vocals_partial = vocals_path.parent / ".vocals.polarformer.part.wav"
    created_normalized_mix = False
    try:
        source = _prepare_stream_source(mix, normalized_mix, ffmpeg)
        created_normalized_mix = source == normalized_mix
        _stream_separate(
            source,
            instrumental_partial,
            vocals_partial,
            session,
            torch,
            stft_window,
            stft_options,
            chunk_size,
            overlap_size,
            step,
            progress,
        )
        os.replace(instrumental_partial, instrumental_path)
        os.replace(vocals_partial, vocals_path)
    finally:
        instrumental_partial.unlink(missing_ok=True)
        vocals_partial.unlink(missing_ok=True)
        if created_normalized_mix or normalized_mix.exists():
            normalized_mix.unlink(missing_ok=True)


def _create_session(
    ort: object,
    model_path: Path,
    *,
    time_frames: int,
    threads: int,
) -> object:
    options = ort.SessionOptions()
    # ONNX Runtime's default arena retains multi-gigabyte intermediates after
    # every window. Disabling it trades a little speed for predictable RSS.
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    # Every inference window is padded to the same sample count. Binding the two
    # symbolic axes lets ORT pre-plan the FP32 graph instead of resolving dynamic
    # shapes again for every chunk.
    options.add_free_dimension_override_by_name("batch", 1)
    options.add_free_dimension_override_by_name("time_frames", time_frames)
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return ort.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def _configure_torch(torch: object, threads: int) -> None:
    torch.set_num_threads(threads)
    with suppress(RuntimeError):
        torch.set_num_interop_threads(1)


def _stream_chunk_seconds() -> int:
    raw = os.environ.get("KARAOKE_POLARFORMER_CHUNK_SECONDS", str(STREAM_CHUNK_SECONDS))
    try:
        requested = int(raw)
    except ValueError:
        requested = STREAM_CHUNK_SECONDS
    return max(2, min(STREAM_MAX_CHUNK_SECONDS, requested))


def _stream_overlap_samples(chunk_size: int) -> int:
    raw = os.environ.get(
        "KARAOKE_POLARFORMER_OVERLAP_MS",
        str(STREAM_OVERLAP_MILLISECONDS),
    )
    try:
        requested = int(raw)
    except ValueError:
        requested = STREAM_OVERLAP_MILLISECONDS
    milliseconds = max(250, min(1_000, requested))
    return min(round(milliseconds * SAMPLE_RATE / 1_000), chunk_size // 2)


def _polarformer_threads(cpu_count: int | None = None) -> int:
    cores = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 4))
    adaptive = min(ORT_MAX_THREADS, cores, max(2, round(cores * 0.56)))
    raw = os.environ.get("KARAOKE_POLARFORMER_THREADS")
    if raw is None:
        return adaptive
    try:
        requested = int(raw)
    except ValueError:
        return adaptive
    return max(1, min(ORT_MAX_THREADS, cores, requested))


def _prepare_stream_source(mix: Path, normalized_mix: Path, ffmpeg: str) -> Path:
    info = sf.info(mix)
    if info.samplerate == SAMPLE_RATE and info.channels == 2 and info.frames > 0:
        return mix
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mix),
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_f32le",
            str(normalized_mix),
        ]
    )
    info = sf.info(normalized_mix)
    if info.samplerate != SAMPLE_RATE or info.channels != 2 or info.frames <= 0:
        raise MediaError("Không chuẩn hóa được audio stereo 44,1 kHz cho PolarFormer.")
    return normalized_mix


def _stream_separate(
    source_path: Path,
    instrumental_partial: Path,
    vocals_partial: Path,
    session: object,
    torch: object,
    stft_window: object,
    stft_options: dict,
    chunk_size: int,
    overlap_size: int,
    step: int,
    progress: ProgressCallback | None,
) -> None:
    with sf.SoundFile(source_path) as source:
        total_samples = len(source)
        if source.channels != 2 or source.samplerate != SAMPLE_RATE or total_samples <= 0:
            raise MediaError("BS PolarFormer cần audio stereo 44,1 kHz có dữ liệu.")
        starts = list(range(0, total_samples, step))
        vocal_accumulator = np.zeros((2, 0), dtype=np.float32)
        weight_accumulator = np.zeros(0, dtype=np.float32)
        buffer_start = 0

        with sf.SoundFile(
            instrumental_partial,
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=2,
            subtype="FLOAT",
            format="WAV",
        ) as instrumental_output, sf.SoundFile(
            vocals_partial,
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=2,
            subtype="FLOAT",
            format="WAV",
        ) as vocals_output:
            for index, start in enumerate(starts):
                flush_count = min(max(0, start - buffer_start), weight_accumulator.size)
                if flush_count:
                    _flush_accumulator(
                        source,
                        instrumental_output,
                        vocals_output,
                        buffer_start,
                        vocal_accumulator[:, :flush_count],
                        weight_accumulator[:flush_count],
                    )
                    vocal_accumulator = vocal_accumulator[:, flush_count:]
                    weight_accumulator = weight_accumulator[flush_count:]
                    buffer_start += flush_count

                source.seek(start)
                raw = source.read(chunk_size, dtype="float32", always_2d=True)
                actual_length = raw.shape[0]
                if actual_length <= 0:
                    continue
                chunk = raw.T
                if actual_length < chunk_size:
                    chunk = np.pad(chunk, ((0, 0), (0, chunk_size - actual_length)))
                estimate = _infer_chunk(session, torch, chunk, stft_window, stft_options)
                estimate = estimate[:, :actual_length]
                weights = _chunk_weights(
                    actual_length,
                    overlap_size,
                    first=index == 0,
                    last=index == len(starts) - 1,
                )
                required = start - buffer_start + actual_length
                if required > weight_accumulator.size:
                    extension = required - weight_accumulator.size
                    vocal_accumulator = np.pad(vocal_accumulator, ((0, 0), (0, extension)))
                    weight_accumulator = np.pad(weight_accumulator, (0, extension))
                offset = start - buffer_start
                vocal_accumulator[:, offset : offset + actual_length] += estimate * weights
                weight_accumulator[offset : offset + actual_length] += weights
                if progress:
                    progress(
                        (index + 1) / len(starts),
                        f"BS PolarFormer FP32 low-memory: {index + 1}/{len(starts)} đoạn",
                    )
                del raw, chunk, estimate, weights
                if index % 4 == 3:
                    gc.collect()

            if weight_accumulator.size:
                _flush_accumulator(
                    source,
                    instrumental_output,
                    vocals_output,
                    buffer_start,
                    vocal_accumulator,
                    weight_accumulator,
                )


def _chunk_weights(length: int, overlap: int, *, first: bool, last: bool) -> np.ndarray:
    weights = np.ones(length, dtype=np.float32)
    fade_length = min(overlap, length)
    if fade_length <= 0:
        return weights
    phase = (np.arange(fade_length, dtype=np.float32) + 0.5) / overlap
    if not first:
        weights[:fade_length] *= np.sin(phase * np.pi / 2.0) ** 2
    if not last:
        weights[-fade_length:] *= np.cos(phase * np.pi / 2.0) ** 2
    return weights


def _flush_accumulator(
    source: sf.SoundFile,
    instrumental_output: sf.SoundFile,
    vocals_output: sf.SoundFile,
    start: int,
    vocal_accumulator: np.ndarray,
    weight_accumulator: np.ndarray,
) -> None:
    count = weight_accumulator.size
    source.seek(start)
    original = source.read(count, dtype="float32", always_2d=True)
    vocals = vocal_accumulator[:, : original.shape[0]] / np.maximum(
        weight_accumulator[: original.shape[0]], np.finfo(np.float32).eps
    )[None, :]
    instrumental = original.T - vocals
    instrumental_output.write(instrumental.T)
    vocals_output.write(vocals.T)


def _infer_chunk(
    session: object,
    torch: object,
    audio: np.ndarray,
    window: object,
    options: dict,
) -> np.ndarray:
    with torch.inference_mode():
        audio_tensor = torch.from_numpy(audio).float()
        stft = torch.stft(audio_tensor, **options, window=window, return_complex=True)
        stft_real = torch.view_as_real(stft)
        channels, frequencies, frames, complex_parts = stft_real.shape
        packed = (
            stft_real.permute(1, 0, 2, 3)
            .reshape(1, frequencies * channels, frames, complex_parts)
            .contiguous()
        )
        features = (
            packed.permute(0, 2, 1, 3)
            .reshape(1, frames, frequencies * channels * complex_parts)
            .numpy()
        )
        mask = session.run(None, {"stft_features": features})[0]
        packed_complex = torch.view_as_complex(packed.unsqueeze(1).contiguous())
        mask_complex = torch.view_as_complex(torch.from_numpy(mask).contiguous())
        masked = packed_complex * mask_complex
        masked = (
            masked.reshape(1, 1, frequencies, channels, frames)
            .permute(0, 1, 3, 2, 4)
            .reshape(channels, frequencies, frames)
        )
        masked[:, 0, :] = 0.0
        reconstructed = torch.istft(
            masked,
            **options,
            window=window,
            return_complex=False,
            length=audio.shape[1],
        )
        return reconstructed.numpy().astype(np.float32, copy=False)


def _model_is_valid(path: Path) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == POLARFORMER_BYTES
        and sha256_file(path) == POLARFORMER_SHA256
    )
