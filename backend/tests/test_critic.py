from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from karaoke_studio.alignment import LYRIC_CTC_SPEC, SPEECH_CTC_SPEC
from karaoke_studio.critic import run_automatic_sweep_critic
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import (
    AlignmentCandidateEvidence,
    GraphemeAlignmentCandidate,
    SweepCurveV1,
    SweepPointV1,
    TimingSource,
    TokenAlignmentEvidence,
)
from karaoke_studio.motion import split_graphemes


def _fixture(tmp_path: Path, sustain_end_us: int = 1_100_000):
    timeline = parse_lrc("[00:00.10]thay", 2_000_000)
    line = timeline.lines[0]
    token = line.tokens[0]
    token.start_us = 100_000
    token.end_us = 1_100_000
    token.source = TimingSource.CTC
    line.start_us = token.start_us
    line.end_us = token.end_us
    token.sweep = SweepCurveV1(
        source="ensemble_ctc",
        confidence=0.76,
        points=[
            SweepPointV1(time_us=100_000, line_progress_ppm=0),
            SweepPointV1(time_us=200_000, line_progress_ppm=250_000),
            SweepPointV1(time_us=300_000, line_progress_ppm=500_000),
            SweepPointV1(time_us=760_000, line_progress_ppm=750_000),
            SweepPointV1(time_us=1_100_000, line_progress_ppm=1_000_000),
        ],
    )
    specs = [LYRIC_CTC_SPEC, LYRIC_CTC_SPEC, SPEECH_CTC_SPEC, SPEECH_CTC_SPEC]
    stems = ["mel", "demucs", "mel", "demucs"]
    offsets = [-5_000, 4_000, -2_000, 6_000]
    candidates = [
        AlignmentCandidateEvidence(
            model_id=spec.id,
            model_revision=spec.revision,
            stem_id=stem,
            start_us=100_000 + offset,
            end_us=1_100_000 + offset,
            confidence=0.9,
        )
        for spec, stem, offset in zip(specs, stems, offsets, strict=True)
    ]
    boundaries = [
        (100_000, 200_000),
        (200_000, 300_000),
        (300_000, 900_000),
        (900_000, 1_100_000),
    ]
    graphemes = []
    for index, (_start, _end, text) in enumerate(split_graphemes(token.text)):
        start_us, end_us = boundaries[index]
        for spec, stem, offset in zip(specs, stems, offsets, strict=True):
            graphemes.append(
                GraphemeAlignmentCandidate(
                    model_id=spec.id,
                    model_revision=spec.revision,
                    stem_id=stem,
                    grapheme_index=index,
                    text=text,
                    start_us=start_us + offset,
                    end_us=end_us + offset,
                    confidence=0.9,
                )
            )
    evidence = TokenAlignmentEvidence(
        line_id=line.id,
        token_id=token.id,
        text=token.text,
        selected_start_us=token.start_us,
        selected_end_us=token.end_us,
        start_spread_us=11_000,
        end_spread_us=11_000,
        acoustic_support=0.9,
        consensus_count=4,
        auto_accepted=False,
        reason_codes=["SWEEP_DISAGREEMENT"],
        candidates=candidates,
        selected_sweep=token.sweep,
        grapheme_candidates=graphemes,
    )
    sample_rate = 16_000
    audio = np.zeros(sample_rate * 2, dtype=np.float32)
    start = round(0.1 * sample_rate)
    end = round(sustain_end_us * sample_rate / 1_000_000)
    time = np.arange(end - start, dtype=np.float32) / sample_rate
    audio[start:end] = 0.45 * np.sin(2 * np.pi * 230 * time)
    vocals: dict[str, Path] = {}
    for stem in {"mel", "demucs"}:
        path = tmp_path / f"{stem}.wav"
        sf.write(path, audio, sample_rate)
        vocals[stem] = path
    return timeline, evidence, vocals


def test_critic_iterates_until_grapheme_curve_converges(tmp_path: Path) -> None:
    timeline, evidence, vocals = _fixture(tmp_path)

    report = run_automatic_sweep_critic(
        timeline,
        [evidence],
        vocals,
        "maximum",
    )

    token = timeline.lines[0].tokens[0]
    assert token.sweep is not None
    assert token.sweep.points[3].time_us == 898_000
    assert evidence.critic_iterations == 3
    assert evidence.critic_converged is True
    assert evidence.auto_accepted is True
    assert "SWEEP_DISAGREEMENT" not in evidence.reason_codes
    assert report["corrections_applied"] == 3
    assert all(
        previous.time_us < current.time_us
        and previous.line_progress_ppm <= current.line_progress_ppm
        for previous, current in zip(token.sweep.points, token.sweep.points[1:], strict=False)
    )


def test_critic_never_changes_manual_or_reviewed_motion(tmp_path: Path) -> None:
    timeline, evidence, vocals = _fixture(tmp_path)
    token = timeline.lines[0].tokens[0]
    token.source = TimingSource.MANUAL
    token.verified = True
    original = token.sweep.model_dump() if token.sweep else None

    report = run_automatic_sweep_critic(timeline, [evidence], vocals, "maximum")

    assert token.sweep is not None
    assert token.sweep.model_dump() == original
    assert evidence.critic_corrections == []
    assert evidence.critic_converged is True
    assert report["corrections_applied"] == 0


def test_critic_fails_closed_when_sustain_continues_past_boundary(tmp_path: Path) -> None:
    timeline, evidence, vocals = _fixture(tmp_path, sustain_end_us=1_350_000)

    run_automatic_sweep_critic(timeline, [evidence], vocals, "maximum")

    assert evidence.critic_converged is False
    assert evidence.auto_accepted is False
    assert "CRITIC_SUSTAIN_MISMATCH" in evidence.reason_codes
    assert "CRITIC_NOT_CONVERGED" in evidence.reason_codes
