from __future__ import annotations

import importlib.util
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from .media import MediaError, sha256_file

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
CHUNK_SIZE = 882_000
NUM_OVERLAP = 2

ProgressCallback = Callable[[float, str], None]


def polarformer_dependencies_available() -> bool:
    return all(
        importlib.util.find_spec(package) is not None
        for package in ("librosa", "onnxruntime", "torch")
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
) -> None:
    if not polarformer_dependencies_available():
        raise MediaError("Thiếu onnxruntime, torch hoặc librosa cho BS PolarFormer.")
    if not _model_is_valid(model_path):
        raise MediaError("Checkpoint BS PolarFormer FP32 chưa hợp lệ.")

    import librosa
    import onnxruntime as ort
    import torch

    audio, _original_rate = librosa.load(str(mix), sr=SAMPLE_RATE, mono=False)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = np.stack((audio, audio))
    elif audio.shape[0] > 2:
        audio = audio[:2]
    if audio.shape[0] != 2 or audio.shape[1] == 0:
        raise MediaError("BS PolarFormer cần audio stereo có dữ liệu.")

    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    total_samples = audio.shape[1]
    step = CHUNK_SIZE // NUM_OVERLAP
    starts = list(range(0, total_samples, step))
    vocals = np.zeros((2, total_samples), dtype=np.float32)
    weights = np.zeros(total_samples, dtype=np.float32)
    stft_window = torch.hann_window(WIN_LENGTH)
    stft_options = {
        "n_fft": N_FFT,
        "hop_length": HOP_LENGTH,
        "win_length": WIN_LENGTH,
        "normalized": False,
    }

    for index, start in enumerate(starts):
        end = min(start + CHUNK_SIZE, total_samples)
        chunk = audio[:, start:end]
        if chunk.shape[1] < CHUNK_SIZE:
            chunk = np.pad(chunk, ((0, 0), (0, CHUNK_SIZE - chunk.shape[1])))
        estimate = _infer_chunk(session, torch, chunk, stft_window, stft_options)
        actual_length = end - start
        vocals[:, start:end] += estimate[:, :actual_length]
        weights[start:end] += 1.0
        if progress:
            progress(
                (index + 1) / len(starts),
                f"BS PolarFormer FP32: {index + 1}/{len(starts)} đoạn",
            )

    vocals /= np.maximum(weights, 1.0)[None, :]
    instrumental = audio - vocals
    instrumental_path.parent.mkdir(parents=True, exist_ok=True)
    vocals_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(instrumental_path, instrumental.T, SAMPLE_RATE, subtype="FLOAT")
    sf.write(vocals_path, vocals.T, SAMPLE_RATE, subtype="FLOAT")


def _infer_chunk(session: object, torch: object, audio: np.ndarray, window: object, options: dict) -> np.ndarray:
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
