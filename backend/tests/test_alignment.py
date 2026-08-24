from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from karaoke_studio import alignment
from karaoke_studio.lrc import parse_lrc


class _FakeBackend:
    def __init__(self, available: bool):
        self.available = available

    def is_available(self) -> bool:
        return self.available


class _FakeTorch:
    def __init__(self, *, cuda: bool, mps: bool):
        self.cuda = _FakeBackend(cuda)
        self.backends = type("Backends", (), {"mps": _FakeBackend(mps)})()

    @staticmethod
    def device(name: str) -> str:
        return name


def test_ctc_failure_falls_back_without_losing_project(monkeypatch, test_settings) -> None:
    timeline = parse_lrc("[00:00.00]Xin chào Việt Nam\n", duration_us=2_000_000)
    events: list[str] = []

    monkeypatch.setattr(alignment.VietnameseCTCAligner, "available", lambda _self: True)

    def fail_ctc(_self, _timeline, _vocal_wav, _event):
        raise RuntimeError("incompatible checkpoint")

    monkeypatch.setattr(alignment.VietnameseCTCAligner, "align", fail_ctc)
    monkeypatch.setattr(
        alignment.EnergyAwareAligner,
        "align",
        lambda _self, candidate, _vocal_wav, _event: candidate,
    )

    result = alignment.align_timeline(
        timeline,
        Path("unused.wav"),
        accept_noncommercial_license=True,
        event=lambda _progress, message: events.append(message),
        settings=test_settings,
    )

    assert result is timeline
    assert any("RuntimeError" in message and "energy-aware" in message for message in events)


def test_ctc_viterbi_handles_fast_repeated_characters_and_free_lead_in() -> None:
    probabilities = np.full((11, 3), 0.01, dtype=np.float64)
    probabilities[:, 0] = 0.98
    for frame, token_id in ((4, 1), (6, 1), (8, 2)):
        probabilities[frame] = 0.01
        probabilities[frame, token_id] = 0.98
    probabilities /= probabilities.sum(axis=1, keepdims=True)

    spans, scores = alignment._ctc_path(np.log(probabilities), [1, 1, 2], blank_id=0)

    assert [span[0] for span in spans] == [4, 6, 8]
    assert all(score > 0.9 for score in scores)


def test_ctc_normalization_exposes_hyphenated_vietnamese_syllables() -> None:
    assert alignment._ctc_normalize("Giê-xu, Đấng!") == "giê xu đấng"


def test_ctc_search_window_expands_around_lrc_anchor_without_leaving_audio() -> None:
    timeline = parse_lrc(
        "[00:01.00]Một hai ba\n[00:03.00]Bốn năm sáu\n[00:05.00]Bảy tám chín",
        duration_us=7_000_000,
    )

    start_us, end_us = alignment._line_search_window(timeline.lines, 1, 7_000_000)

    assert start_us < timeline.lines[1].start_us
    assert end_us > timeline.lines[1].end_us
    assert start_us >= timeline.lines[0].start_us
    assert end_us <= timeline.lines[2].end_us


def test_torch_device_auto_prefers_cuda_then_mps_then_cpu(monkeypatch) -> None:
    monkeypatch.delenv(alignment.TORCH_DEVICE_ENV, raising=False)

    assert alignment._select_torch_device(_FakeTorch(cuda=True, mps=True)) == "cuda"
    assert alignment._select_torch_device(_FakeTorch(cuda=False, mps=True)) == "mps"
    assert alignment._select_torch_device(_FakeTorch(cuda=False, mps=False)) == "cpu"


def test_torch_device_explicit_unavailable_accelerator_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(alignment.TORCH_DEVICE_ENV, "cuda")

    with pytest.raises(RuntimeError, match="CUDA"):
        alignment._select_torch_device(_FakeTorch(cuda=False, mps=False))


def test_torch_device_explicit_cpu_is_portable(monkeypatch) -> None:
    monkeypatch.setenv(alignment.TORCH_DEVICE_ENV, "cpu")

    assert alignment._select_torch_device(_FakeTorch(cuda=True, mps=True)) == "cpu"
