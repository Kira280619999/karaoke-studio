from __future__ import annotations

from karaoke_studio.lrc import parse_lrc
from karaoke_studio.motion import linear_sweep
from karaoke_studio.timeline import frame_duration_us, snap_to_frame, validate_timeline


def test_frame_rounding_is_integer_and_stable() -> None:
    timeline = parse_lrc("[00:00.50]Xin chào", duration_us=2_000_000)
    assert frame_duration_us(timeline) == 16_667
    assert snap_to_frame(1_008_000, timeline) == 1_000_020


def test_overlap_and_low_confidence_are_reported() -> None:
    timeline = parse_lrc("[00:00.50]Xin chào", duration_us=2_000_000)
    timeline.lines[0].tokens[1].start_us = timeline.lines[0].tokens[0].start_us
    issues = validate_timeline(timeline, require_verified=True)
    codes = {issue.code for issue in issues}
    assert "TOKEN_OVERLAP" in codes
    assert "LOW_CONFIDENCE" in codes
    assert all(issue.severity == "error" for issue in issues if issue.code == "LOW_CONFIDENCE")


def test_high_confidence_ai_timing_does_not_require_manual_confirmation() -> None:
    timeline = parse_lrc("[00:00.50]Xin chào", duration_us=2_000_000)
    timeline.lines[0].confidence = 0.96
    for token in timeline.lines[0].tokens:
        token.confidence = 0.96
    codes = {issue.code for issue in validate_timeline(timeline, require_verified=True)}
    assert "LOW_CONFIDENCE" not in codes


def test_manual_edit_always_requires_confirmation_even_with_high_confidence() -> None:
    timeline = parse_lrc("[00:00.50]Xin chào", duration_us=2_000_000)
    token = timeline.lines[0].tokens[0]
    token.source = "manual"
    token.confidence = 0.99
    codes = {issue.code for issue in validate_timeline(timeline, require_verified=True)}
    assert "MANUAL_REVIEW" in codes


def test_final_requires_valid_verified_sweep_curve() -> None:
    timeline = parse_lrc("[00:00.50]Xin chào", duration_us=2_000_000)
    codes = {issue.code for issue in validate_timeline(timeline, require_verified=True)}
    assert "SWEEP_MISSING" in codes

    for token_index, token in enumerate(timeline.lines[0].tokens):
        token.sweep = linear_sweep(
            timeline.lines[0], token_index, "lrc_linear", 0.9, True
        )
    codes = {issue.code for issue in validate_timeline(timeline, require_verified=True)}
    assert "SWEEP_MISSING" not in codes
    assert "SWEEP_UNVERIFIED" not in codes
