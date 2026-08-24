from __future__ import annotations

from karaoke_studio.alignment import CONSENSUS_POLICY, merge_ctc_consensus
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import TimingSource


def test_dual_stem_consensus_auto_accepts_only_two_frame_agreement() -> None:
    primary = parse_lrc("[00:00.00]Xin chào Việt Nam", duration_us=4_000_000)
    secondary = primary.model_copy(deep=True)
    for timeline in (primary, secondary):
        for token in timeline.lines[0].tokens:
            token.source = TimingSource.CTC
            token.confidence = 0.45

    secondary.lines[0].tokens[0].start_us += 20_000
    secondary.lines[0].tokens[0].end_us += 20_000
    secondary.lines[0].tokens[1].start_us += 80_000
    secondary.lines[0].tokens[1].end_us += 80_000

    merged, report = merge_ctc_consensus(primary, secondary)

    assert report["policy"] == CONSENSUS_POLICY
    assert report["tolerance_us"] == 33_333
    assert report["auto_accepted_tokens"] == 3
    assert report["review_required_tokens"] == 1
    assert merged.lines[0].tokens[0].confidence >= 0.90
    assert merged.lines[0].tokens[1].confidence == 0.45


def test_dual_stem_consensus_rejects_low_probability_agreement() -> None:
    primary = parse_lrc("[00:00.00]Xin chào", duration_us=2_000_000)
    secondary = primary.model_copy(deep=True)
    for timeline in (primary, secondary):
        for token in timeline.lines[0].tokens:
            token.source = TimingSource.CTC
            token.confidence = 0.05

    merged, report = merge_ctc_consensus(primary, secondary)

    assert report["auto_accepted_tokens"] == 0
    assert report["review_required_tokens"] == 2
    assert all(token.confidence <= 0.05 for token in merged.lines[0].tokens)


def test_consensus_reconciles_mixed_candidate_word_boundaries() -> None:
    primary = parse_lrc("[00:00.00]Một hai", duration_us=2_000_000)
    secondary = primary.model_copy(deep=True)
    for timeline in (primary, secondary):
        for token in timeline.lines[0].tokens:
            token.source = TimingSource.CTC
    secondary.lines[0].tokens[0].end_us += 120_000
    secondary.lines[0].tokens[1].start_us += 120_000
    primary.lines[0].tokens[0].confidence = 0.9
    secondary.lines[0].tokens[0].confidence = 0.3
    primary.lines[0].tokens[1].confidence = 0.3
    secondary.lines[0].tokens[1].confidence = 0.9

    merged, _report = merge_ctc_consensus(primary, secondary)
    first, second = merged.lines[0].tokens

    assert first.end_us == second.start_us
    assert first.confidence <= 0.77
    assert second.confidence <= 0.77
