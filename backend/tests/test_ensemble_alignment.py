from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from karaoke_studio.alignment import LYRIC_CTC_SPEC, SPEECH_CTC_SPEC
from karaoke_studio.ensemble import (
    AlignmentRun,
    _merge_runs,
    _refine_acoustic_boundaries,
    _select_global_line_centers,
    align_timeline_ensemble,
    evidence_review_issues,
)
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import TimingSource
from karaoke_studio.timeline import validate_timeline


def _candidate_run(base, spec, stem_id: str, shift_us: int) -> AlignmentRun:
    candidate = base.model_copy(deep=True)
    for line in candidate.lines:
        line.source = TimingSource.CTC
        line.start_us += shift_us
        line.end_us += shift_us
        line.confidence = 0.82
        for token in line.tokens:
            token.start_us += shift_us
            token.end_us += shift_us
            token.source = TimingSource.CTC
            token.confidence = 0.82
    return AlignmentRun(
        spec=spec,
        stem_id=stem_id,
        timeline=candidate,
        acoustic_support={token.id: 0.8 for line in candidate.lines for token in line.tokens},
        sustain_uncertain=set(),
    )


def test_four_way_ensemble_auto_accepts_only_diverse_consensus() -> None:
    base = parse_lrc("[00:01.00]Xin chào Việt Nam", duration_us=5_000_000)
    runs = [
        _candidate_run(base, LYRIC_CTC_SPEC, "mel", -8_000),
        _candidate_run(base, LYRIC_CTC_SPEC, "demucs", 6_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "mel", -3_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "demucs", 9_000),
    ]

    centers = _select_global_line_centers(base, runs)
    merged, evidence = _merge_runs(
        base,
        runs,
        centers,
        "maximum",
        expected_models=2,
        expected_stems=2,
    )

    assert all(item.auto_accepted for item in evidence)
    assert all(item.consensus_count == 4 for item in evidence)
    assert all(token.confidence >= 0.90 for token in merged.lines[0].tokens)
    assert all(not item.reason_codes for item in evidence)


def test_missing_second_model_fails_closed_with_reason_code() -> None:
    base = parse_lrc("[00:01.00]Hát thật nhanh", duration_us=3_000_000)
    runs = [
        _candidate_run(base, LYRIC_CTC_SPEC, "mel", 0),
        _candidate_run(base, LYRIC_CTC_SPEC, "demucs", 5_000),
    ]

    merged, evidence = _merge_runs(
        base,
        runs,
        _select_global_line_centers(base, runs),
        "maximum",
        expected_models=2,
        expected_stems=2,
    )

    assert all(not item.auto_accepted for item in evidence)
    assert all("MODEL_DISAGREEMENT" in item.reason_codes for item in evidence)
    assert all(token.confidence < 0.78 for token in merged.lines[0].tokens)


def test_large_lrc_drift_is_safe_when_four_independent_candidates_agree() -> None:
    base = parse_lrc("[00:01.00]LRC bị lệch", duration_us=6_000_000)
    runs = [
        _candidate_run(base, LYRIC_CTC_SPEC, "mel", 2_000_000),
        _candidate_run(base, LYRIC_CTC_SPEC, "demucs", 2_008_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "mel", 1_996_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "demucs", 2_004_000),
    ]

    _merged, evidence = _merge_runs(
        base,
        runs,
        _select_global_line_centers(base, runs),
        "maximum",
        expected_models=2,
        expected_stems=2,
    )

    assert all(item.auto_accepted for item in evidence)
    assert all("ANCHOR_DRIFT" in item.reason_codes for item in evidence)


def test_slow_sustain_extends_last_token_to_vocal_offset() -> None:
    sample_rate = 16_000
    duration_seconds = 2.0
    audio = np.zeros(round(sample_rate * duration_seconds), dtype=np.float32)
    time = np.arange(len(audio), dtype=np.float32) / sample_rate
    audio[int(0.10 * sample_rate) : int(0.22 * sample_rate)] = 0.5 * np.sin(
        2 * np.pi * 210 * time[int(0.10 * sample_rate) : int(0.22 * sample_rate)]
    )
    audio[int(0.28 * sample_rate) : int(0.40 * sample_rate)] = 0.5 * np.sin(
        2 * np.pi * 240 * time[int(0.28 * sample_rate) : int(0.40 * sample_rate)]
    )
    audio[int(0.46 * sample_rate) : int(1.46 * sample_rate)] = 0.45 * np.sin(
        2 * np.pi * 260 * time[int(0.46 * sample_rate) : int(1.46 * sample_rate)]
    )
    line = parse_lrc("[00:00.00]Ngân thật lâu", 2_000_000).lines[0]
    starts = [100_000, 280_000, 460_000]
    ends = [280_000, 460_000, 600_000]
    for token, start_us, end_us in zip(line.tokens, starts, ends, strict=True):
        token.start_us = start_us
        token.end_us = end_us
        token.source = TimingSource.CTC
        token.confidence = 0.8
    line.start_us = starts[0]
    line.end_us = ends[-1]

    refined, _support, uncertain = _refine_acoustic_boundaries(
        line,
        audio,
        sample_rate,
        # A soft LRC anchor may be earlier than the singer after global drift;
        # sustain detection must still follow the vocal signal instead of clipping.
        next_line_anchor_us=400_000,
        frame_us=16_667,
    )

    assert refined.tokens[-1].end_us >= 1_400_000
    assert refined.tokens[-1].end_us <= 1_550_000
    assert uncertain is False
    assert all(
        previous.end_us == current.start_us
        for previous, current in zip(refined.tokens, refined.tokens[1:], strict=False)
    )


def test_energy_fallback_cannot_silently_verify(tmp_path: Path) -> None:
    sample_rate = 16_000
    audio = np.zeros(sample_rate * 2, dtype=np.float32)
    audio[2_000:24_000] = 0.3
    vocal = tmp_path / "vocal.wav"
    sf.write(vocal, audio, sample_rate)
    timeline = parse_lrc("[00:00.00]Xin chào", 2_000_000)

    result, evidence, report = align_timeline_ensemble(
        timeline,
        {"fixture": vocal},
        {"fixture": "sha"},
        accept_noncommercial_license=False,
        profile="maximum",
        event=lambda _progress, _message: None,
        settings=None,  # type: ignore[arg-type]
        cache_dir=tmp_path / "cache",
    )

    assert evidence.degraded is True
    assert report["policy"] == "energy-valley-v2"
    assert all(token.confidence < 0.78 for line in result.lines for token in line.tokens)
    assert evidence_review_issues(result, evidence)


def test_human_locked_line_is_never_overwritten() -> None:
    base = parse_lrc("[00:01.00]Con đã duyệt", duration_us=4_000_000)
    base.lines[0].locked = True
    base.lines[0].verified = True
    for token in base.lines[0].tokens:
        token.locked = True
        token.verified = True
    original = base.model_dump()
    runs = [
        _candidate_run(base, LYRIC_CTC_SPEC, "mel", 500_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "demucs", -500_000),
    ]

    merged, evidence = _merge_runs(
        base,
        runs,
        _select_global_line_centers(base, runs),
        "maximum",
        expected_models=2,
        expected_stems=2,
    )

    assert merged.model_dump() == original
    assert all(item.auto_accepted for item in evidence)


def test_out_of_order_model_path_is_regularized_and_flagged() -> None:
    base = parse_lrc(
        "[00:01.00]Câu trước\n[00:03.00]Câu sau",
        duration_us=6_000_000,
    )
    runs = [
        _candidate_run(base, LYRIC_CTC_SPEC, "mel", 1_500_000),
        _candidate_run(base, LYRIC_CTC_SPEC, "demucs", 1_500_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "mel", 1_500_000),
        _candidate_run(base, SPEECH_CTC_SPEC, "demucs", 1_500_000),
    ]
    for run in runs:
        second = run.timeline.lines[1]
        delta_us = -3_000_000
        second.start_us += delta_us
        second.end_us += delta_us
        for token in second.tokens:
            token.start_us += delta_us
            token.end_us += delta_us

    merged, evidence = _merge_runs(
        base,
        runs,
        _select_global_line_centers(base, runs),
        "maximum",
        expected_models=2,
        expected_stems=2,
    )

    assert not [issue for issue in validate_timeline(merged) if issue.severity == "error"]
    second_ids = {token.id for token in merged.lines[1].tokens}
    assert all(
        not item.auto_accepted and "MODEL_DISAGREEMENT" in item.reason_codes
        for item in evidence
        if item.token_id in second_ids
    )


def test_fast_tokens_are_never_shorter_than_one_output_frame() -> None:
    base = parse_lrc("[00:01.00]Một hai ba", duration_us=3_000_000)
    runs = [
        _candidate_run(base, LYRIC_CTC_SPEC, "mel", 0),
        _candidate_run(base, LYRIC_CTC_SPEC, "demucs", 0),
        _candidate_run(base, SPEECH_CTC_SPEC, "mel", 0),
        _candidate_run(base, SPEECH_CTC_SPEC, "demucs", 0),
    ]
    for run in runs:
        line = run.timeline.lines[0]
        start_us = 1_000_000
        for index, token in enumerate(line.tokens):
            token.start_us = start_us + index * 5_000
            token.end_us = token.start_us + 5_000
        line.start_us = line.tokens[0].start_us
        line.end_us = line.tokens[-1].end_us

    merged, evidence = _merge_runs(
        base,
        runs,
        _select_global_line_centers(base, runs),
        "maximum",
        expected_models=2,
        expected_stems=2,
    )

    frame_us = round(1_000_000 / 60)
    assert all(
        token.end_us - token.start_us >= frame_us for token in merged.lines[0].tokens
    )
    assert all(
        "FAST_PHRASE" in item.reason_codes and not item.auto_accepted
        for item in evidence
    )
