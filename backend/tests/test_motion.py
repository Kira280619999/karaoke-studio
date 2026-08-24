from __future__ import annotations

from pathlib import Path

from karaoke_studio.alignment import LYRIC_CTC_SPEC, SPEECH_CTC_SPEC
from karaoke_studio.ensemble import AlignmentRun, _ensemble_token_sweep
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.motion import (
    GraphemeSpan,
    RhythmGrid,
    canonical_progress_ppm,
    evaluate_sweep_ppm,
    line_progress_ppm,
    linear_sweep,
    remap_timeline_sweep_font,
    resolve_font,
    split_graphemes,
)
from karaoke_studio.timeline import validate_timeline


def _run(line, spec, stem_id: str, shift_us: int) -> AlignmentRun:
    token = line.tokens[0]
    spans = [
        GraphemeSpan(0, "c", 1_000_000 + shift_us, 1_100_000 + shift_us, 0.92),
        GraphemeSpan(1, "a", 1_100_000 + shift_us, 1_260_000 + shift_us, 0.94),
    ]
    return AlignmentRun(
        spec=spec,
        stem_id=stem_id,
        timeline=parse_lrc("[00:01.00]ca", 2_500_000),
        acoustic_support={token.id: 0.9},
        sustain_uncertain=set(),
        graphemes={token.id: spans},
    )


def test_vietnamese_nfd_marks_remain_in_one_display_grapheme() -> None:
    decomposed = "o\u031b\u0323"
    assert split_graphemes(decomposed) == [(0, len(decomposed), decomposed)]
    assert [item[2] for item in split_graphemes("Đức Chúa")] == [
        "Đ",
        "ứ",
        "c",
        " ",
        "C",
        "h",
        "ú",
        "a",
    ]


def test_sung_vowel_owns_the_sustain_in_non_linear_curve(test_settings) -> None:
    line = parse_lrc("[00:01.00]ca", 2_500_000).lines[0]
    runs = [
        _run(line, LYRIC_CTC_SPEC, "mel", -7_000),
        _run(line, LYRIC_CTC_SPEC, "demucs", 5_000),
        _run(line, SPEECH_CTC_SPEC, "mel", -3_000),
        _run(line, SPEECH_CTC_SPEC, "demucs", 8_000),
    ]
    curve, candidates, spread, beat_support, reasons = _ensemble_token_sweep(
        line,
        0,
        runs,
        required=3,
        motion_profile="vocal_hybrid",
        rhythm=RhythmGrid((1_130_000,), (1.0,)),
        font_path=resolve_font(test_settings),
        sustain_uncertain=False,
        boundary_auto_accepted=True,
    )

    assert not reasons
    assert len(candidates) == 8
    assert spread <= 20_000
    assert beat_support == 0.0
    assert curve.points[1].time_us < 1_130_000  # high-consensus vocal is not beat-snapped
    assert curve.points[-1].time_us == line.tokens[0].end_us
    assert curve.points[-1].time_us - curve.points[1].time_us > 1_000_000
    assert all(
        current.time_us - previous.time_us >= 16_667
        for previous, current in zip(curve.points, curve.points[1:], strict=False)
    )
    samples = [evaluate_sweep_ppm(curve, value) for value in range(1_000_000, 2_500_001, 16_667)]
    assert samples == sorted(samples)


def test_linear_fallback_sweeps_spaces_and_punctuation_without_backward_motion(
    test_settings,
) -> None:
    timeline = parse_lrc("[00:00.00]Ngài   là Chúa.", 3_000_000)
    line = timeline.lines[0]
    font_path: Path = resolve_font(test_settings)
    for token_index, token in enumerate(line.tokens):
        token.sweep = linear_sweep(
            line, token_index, "energy_linear", 0.6, False, font_path
        )
    samples = [
        line_progress_ppm(line, value)
        for value in range(line.start_us, line.end_us + 1, 16_667)
    ]
    assert None not in samples
    numeric = [int(value) for value in samples if value is not None]
    assert numeric == sorted(numeric)
    assert numeric[-1] > 990_000


def test_timeline_rejects_a_jump_between_token_sweep_curves(test_settings) -> None:
    timeline = parse_lrc("[00:00.00]Ngài là", 2_000_000)
    line = timeline.lines[0]
    font_path: Path = resolve_font(test_settings)
    for token_index, token in enumerate(line.tokens):
        token.sweep = linear_sweep(
            line, token_index, "energy_linear", 0.6, False, font_path
        )
    line.tokens[1].sweep.points[0].line_progress_ppm += 1

    codes = {issue.code for issue in validate_timeline(timeline)}

    assert "SWEEP_DISCONTINUITY" in codes


def test_font_change_remaps_visual_progress_without_moving_acoustic_time(test_settings) -> None:
    timeline = parse_lrc("[00:00.00]Đức Thánh Linh dẫn con", 3_000_000)
    line = timeline.lines[0]
    for token_index, token in enumerate(line.tokens):
        token.sweep = linear_sweep(
            line,
            token_index,
            "energy_linear",
            0.8,
            False,
            resolve_font(test_settings, "noto_sans"),
        )
    original_times = [
        point.time_us for token in line.tokens for point in token.sweep.points
    ]

    remapped = remap_timeline_sweep_font(
        timeline, resolve_font(test_settings, "barlow_condensed")
    )
    remapped_line = remapped.lines[0]
    remapped_times = [
        point.time_us for token in remapped_line.tokens for point in token.sweep.points
    ]
    first_end = len(remapped_line.tokens[0].text) + 1
    expected_first_end = canonical_progress_ppm(
        remapped_line.text,
        [first_end],
        resolve_font(test_settings, "barlow_condensed"),
    )[0]

    assert remapped_times == original_times
    assert remapped_line.tokens[0].sweep.points[-1].line_progress_ppm == expected_first_end
    assert not [issue for issue in validate_timeline(remapped) if issue.severity == "error"]
