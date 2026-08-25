from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

import karaoke_studio.polarformer as polarformer


def test_equal_power_overlap_sums_to_one() -> None:
    previous = polarformer._chunk_weights(8, 4, first=True, last=False)
    following = polarformer._chunk_weights(8, 4, first=False, last=True)

    np.testing.assert_allclose(previous[-4:] + following[:4], 1.0, atol=1e-6)


def test_streaming_separation_is_constant_length_and_preserves_mix(
    tmp_path: Path, monkeypatch
) -> None:
    frames = polarformer.SAMPLE_RATE * 6 + 137
    phase = np.arange(frames, dtype=np.float32) / polarformer.SAMPLE_RATE
    source_audio = np.column_stack(
        (
            0.2 * np.sin(2 * np.pi * 220 * phase),
            0.2 * np.sin(2 * np.pi * 330 * phase),
        )
    ).astype(np.float32)
    mix = tmp_path / "mix.wav"
    sf.write(mix, source_audio, polarformer.SAMPLE_RATE, subtype="FLOAT")

    model = tmp_path / "model.onnx"
    model.write_bytes(b"test")
    instrumental = tmp_path / "instrumental.wav"
    vocals = tmp_path / "vocals.wav"
    events: list[tuple[float, str]] = []

    monkeypatch.setattr(polarformer, "polarformer_dependencies_available", lambda: True)
    monkeypatch.setattr(polarformer, "_model_is_valid", lambda _path: True)
    # The regular cross-platform CI environment intentionally omits the optional
    # AI runtime. Keep this unit test lightweight while still exercising the
    # complete streaming/overlap/write path; the optional-runtime CI job covers
    # the real imports separately.
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(hann_window=lambda _length: object()),
    )
    monkeypatch.setattr(polarformer, "_create_session", lambda _ort, _path: object())
    monkeypatch.setattr(polarformer, "_configure_torch", lambda _torch: None)
    monkeypatch.setattr(
        polarformer,
        "_infer_chunk",
        lambda _session, _torch, audio, _window, _options: audio * 0.25,
    )

    polarformer.separate_with_polarformer(
        mix,
        model,
        instrumental,
        vocals,
        progress=lambda value, message: events.append((value, message)),
    )

    instrumental_audio, instrumental_rate = sf.read(instrumental, dtype="float32")
    vocal_audio, vocal_rate = sf.read(vocals, dtype="float32")
    assert instrumental_rate == vocal_rate == polarformer.SAMPLE_RATE
    assert instrumental_audio.shape == vocal_audio.shape == source_audio.shape
    np.testing.assert_allclose(vocal_audio, source_audio * 0.25, atol=2e-6)
    np.testing.assert_allclose(instrumental_audio, source_audio * 0.75, atol=2e-6)
    assert events[-1][0] == 1.0
    assert "low-memory" in events[-1][1]
    assert not list(tmp_path.glob("*.part.wav"))


def test_stream_chunk_environment_is_safely_clamped(monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_POLARFORMER_CHUNK_SECONDS", "20")
    assert polarformer._stream_chunk_seconds() == 4
    monkeypatch.setenv("KARAOKE_POLARFORMER_CHUNK_SECONDS", "1")
    assert polarformer._stream_chunk_seconds() == 2
