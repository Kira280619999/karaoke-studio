from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .models import (
    CriticCorrectionEvidence,
    LineTiming,
    TimelineV1,
    TimingSource,
    TokenAlignmentEvidence,
    TokenTiming,
)
from .motion import is_sung_grapheme, is_vowel_grapheme, regularize_sweep, split_graphemes

MAX_CRITIC_PASSES = 3
CONTROL_POINT_TOLERANCE_US = 33_000
ACOUSTIC_BOUNDARY_TOLERANCE_US = 50_000
MAX_CORRECTION_PER_PASS_US = 50_000


@dataclass(frozen=True)
class AcousticTrack:
    hop_us: int
    rms: np.ndarray
    onset: np.ndarray
    activity: np.ndarray
    voicing: np.ndarray

    def prominent_onset(self, target_us: int, radius_us: int = 90_000) -> tuple[int, float] | None:
        center = round(target_us / self.hop_us)
        radius = max(1, round(radius_us / self.hop_us))
        lower = max(0, center - radius)
        upper = min(len(self.onset), center + radius + 1)
        if upper <= lower:
            return None
        index = lower + int(np.argmax(self.onset[lower:upper]))
        support = float(self.onset[index])
        return (index * self.hop_us, support) if support >= 0.25 else None

    def active_offset(self, start_us: int, limit_us: int) -> tuple[int, float] | None:
        lower = max(0, round(start_us / self.hop_us))
        upper = min(len(self.activity), max(lower + 1, round(limit_us / self.hop_us)))
        if upper <= lower:
            return None
        local = self.activity[lower:upper]
        local_voicing = self.voicing[lower:upper]
        floor = float(np.percentile(local, 20))
        ceiling = float(np.percentile(local, 90))
        threshold = max(0.32, floor + (ceiling - floor) * 0.25)
        voiced = (local >= threshold) & (local_voicing >= 0.30)
        stable = np.convolve(voiced.astype(np.int8), np.ones(3, dtype=np.int8), mode="same") >= 3
        active = np.flatnonzero(stable)
        if not len(active):
            return None
        index = lower + int(active[-1]) + 1
        support = float(np.percentile(local[active] * local_voicing[active], 70))
        return min(limit_us, index * self.hop_us), max(0.0, min(1.0, support))


def _normalize(values: np.ndarray) -> np.ndarray:
    if not len(values):
        return values
    low = float(np.percentile(values, 10))
    high = float(np.percentile(values, 95))
    return np.clip((values - low) / max(1e-9, high - low), 0.0, 1.0)


def load_acoustic_track(path: Path) -> AcousticTrack:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1).astype(np.float64, copy=False)
    window = max(32, round(sample_rate * 0.020))
    hop = max(16, round(sample_rate * 0.010))
    if len(mono) < window:
        empty = np.asarray([], dtype=np.float64)
        return AcousticTrack(
            round(hop * 1_000_000 / sample_rate), empty, empty, empty, empty
        )
    starts = np.arange(0, len(mono) - window + 1, hop, dtype=np.int64)
    squared = np.square(mono)
    cumulative = np.concatenate((np.zeros(1, dtype=np.float64), np.cumsum(squared)))
    energy = cumulative[starts + window] - cumulative[starts]
    rms = np.sqrt(np.maximum(0.0, energy / window))
    activity = _normalize(rms)
    rise = np.maximum(0.0, np.diff(rms, prepend=rms[0]))
    onset = _normalize(rise) * 0.65
    # A short positive activity slope is robust to separator phase differences.
    onset += _normalize(np.maximum(0.0, np.diff(activity, prepend=activity[0]))) * 0.35
    voicing = np.zeros(len(starts), dtype=np.float64)
    sample_indexes = np.arange(window, dtype=np.int64)
    hann = np.hanning(window).astype(np.float64)
    fft_size = 1 << max(1, (window * 2 - 1).bit_length())
    minimum_lag = max(1, round(sample_rate / 800))
    maximum_lag = min(window - 2, round(sample_rate / 70))
    block_size = 2_048
    for block_start in range(0, len(starts), block_size):
        block_end = min(len(starts), block_start + block_size)
        indexes = starts[block_start:block_end, None] + sample_indexes[None, :]
        frames = mono[indexes] * hann[None, :]
        spectrum = np.fft.rfft(frames, n=fft_size, axis=1)
        autocorrelation = np.fft.irfft(np.square(np.abs(spectrum)), n=fft_size, axis=1)
        denominator = np.maximum(1e-12, autocorrelation[:, 0])
        periodic = np.max(
            autocorrelation[:, minimum_lag : maximum_lag + 1], axis=1
        ) / denominator
        voicing[block_start:block_end] = np.clip(periodic, 0.0, 1.0)
    return AcousticTrack(
        hop_us=round(hop * 1_000_000 / sample_rate),
        rms=rms,
        onset=np.clip(onset, 0.0, 1.0),
        activity=activity,
        voicing=voicing,
    )


def _human_protected(line: LineTiming, token: TokenTiming) -> bool:
    return bool(
        line.locked
        or line.verified
        or token.locked
        or token.verified
        or token.source == TimingSource.MANUAL
        or (token.sweep is not None and token.sweep.source == "manual_rescaled")
    )


def _weighted_median(values: list[int], weights: list[float]) -> int:
    ordered = sorted(zip(values, weights, strict=True), key=lambda item: item[0])
    threshold = sum(weights) / 2
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _candidate_consensus(
    item: TokenAlignmentEvidence,
    grapheme_index: int,
    boundary: str,
    required: int,
) -> tuple[int, float] | None:
    candidates = [
        candidate
        for candidate in item.grapheme_candidates
        if candidate.grapheme_index == grapheme_index
    ]
    if len(candidates) < required:
        return None
    values = [
        candidate.start_us if boundary == "start" else candidate.end_us
        for candidate in candidates
    ]
    weights = [
        (1.2 if "lyric-alignment" in candidate.model_id else 1.0)
        * max(0.03, candidate.confidence)
        for candidate in candidates
    ]
    selected = _weighted_median(values, weights)
    agreeing = [
        candidate
        for candidate, value in zip(candidates, values, strict=True)
        if abs(value - selected) <= CONTROL_POINT_TOLERANCE_US
    ]
    model_count = len({candidate.model_id for candidate in agreeing})
    stem_count = len({candidate.stem_id for candidate in agreeing})
    diversity_ok = required < 3 or (model_count >= 2 and stem_count >= 2)
    if len(agreeing) < required or not diversity_ok:
        return None
    support = min(
        1.0,
        len(agreeing) / max(1, len(candidates)) * 0.65
        + float(np.mean([candidate.confidence for candidate in agreeing])) * 0.35,
    )
    return selected, support


def _consensus_acoustic(
    values: list[tuple[int, float]],
    required: int,
    tolerance_us: int = 40_000,
) -> tuple[int, float] | None:
    strong = [(value, support) for value, support in values if support >= 0.35]
    minimum = 2 if required >= 2 else 1
    if len(strong) < minimum:
        return None
    selected = int(np.median([value for value, _support in strong]))
    agreeing = [item for item in strong if abs(item[0] - selected) <= tolerance_us]
    if len(agreeing) < minimum:
        return None
    return selected, float(np.mean([support for _value, support in agreeing]))


def _curve_targets(
    token: TokenTiming,
    item: TokenAlignmentEvidence,
    required: int,
) -> list[tuple[int, int, float, str]]:
    if token.sweep is None:
        return []
    graphemes = split_graphemes(token.text)
    targets: dict[int, tuple[int, float, str]] = {}
    for grapheme_index, (_start, _end, value) in enumerate(graphemes):
        point_index = grapheme_index + 1
        if point_index >= len(token.sweep.points) - 1 or not is_sung_grapheme(value):
            continue
        consensus = _candidate_consensus(item, grapheme_index, "end", required)
        if consensus is not None:
            targets[point_index] = (consensus[0], consensus[1], "ctc_grapheme")

    vowel_indexes = [
        index for index, (_start, _end, value) in enumerate(graphemes) if is_vowel_grapheme(value)
    ]
    if vowel_indexes:
        first_vowel = vowel_indexes[0]
        if 0 < first_vowel < len(token.sweep.points) - 1:
            consensus = _candidate_consensus(item, first_vowel, "start", required)
            if consensus is not None:
                targets[first_vowel] = (consensus[0], consensus[1], "vowel_onset")
        coda_index = next(
            (
                index
                for index in range(vowel_indexes[-1] + 1, len(graphemes))
                if is_sung_grapheme(graphemes[index][2])
            ),
            None,
        )
        if coda_index is not None and 0 < coda_index < len(token.sweep.points) - 1:
            consensus = _candidate_consensus(item, coda_index, "start", required)
            if consensus is not None:
                targets[coda_index] = (consensus[0], consensus[1], "vowel_coda")
    return [
        (point_index, target, support, source)
        for point_index, (target, support, source) in sorted(targets.items())
    ]


def _correction_step(current_us: int, target_us: int) -> int:
    delta = max(
        -MAX_CORRECTION_PER_PASS_US,
        min(MAX_CORRECTION_PER_PASS_US, target_us - current_us),
    )
    return current_us + delta


def run_automatic_sweep_critic(
    timeline: TimelineV1,
    evidence: list[TokenAlignmentEvidence],
    vocal_inputs: dict[str, Path],
    profile: str,
) -> dict[str, object]:
    """Re-listen to every AI curve and fail closed when motion is ambiguous.

    The critic never rewrites lyric text or a human-reviewed boundary. It only
    nudges internal sweep control points toward diverse CTC consensus, then
    checks the result against independent acoustic onset/sustain evidence.
    """
    tracks = [load_acoustic_track(path) for path in vocal_inputs.values() if path.is_file()]
    tracks = [track for track in tracks if len(track.rms)]
    required = 3 if profile == "maximum" else 2 if profile == "balanced" else 1
    evidence_by_token = {item.token_id: item for item in evidence}
    frame_us = round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator)
    corrections = 0
    examined = 0
    pass_maxima: list[int] = []
    token_deltas: list[int] = []

    for pass_index in range(1, MAX_CRITIC_PASSES + 1):
        maximum_delta = 0
        pass_corrections = 0
        for line in timeline.lines:
            for token in line.tokens:
                item = evidence_by_token[token.id]
                if token.sweep is None or _human_protected(line, token):
                    item.critic_converged = True
                    item.critic_iterations = max(item.critic_iterations, pass_index - 1)
                    continue
                if pass_index == 1:
                    examined += 1
                before = token.sweep
                points = [point.model_copy() for point in before.points]
                targets = _curve_targets(token, item, required)
                proposals: dict[int, tuple[int, float, str]] = {}
                for point_index, target_us, support, source in targets:
                    if point_index <= 0 or point_index >= len(points) - 1:
                        continue
                    old_us = points[point_index].time_us
                    new_us = _correction_step(old_us, target_us)
                    if abs(new_us - old_us) <= frame_us:
                        continue
                    points[point_index].time_us = new_us
                    proposals[point_index] = (target_us, support, source)
                candidate = regularize_sweep(
                    before.model_copy(update={"points": points}),
                    token.start_us,
                    token.end_us,
                )
                for point_index, (target_us, support, source) in proposals.items():
                    old_us = before.points[point_index].time_us
                    new_us = candidate.points[point_index].time_us
                    if abs(new_us - old_us) <= frame_us:
                        continue
                    item.critic_corrections.append(
                        CriticCorrectionEvidence(
                            pass_index=pass_index,
                            point_index=point_index,
                            before_us=old_us,
                            after_us=new_us,
                            target_us=target_us,
                            support=support,
                            source=source,
                        )
                    )
                delta = max(
                    (
                        abs(after.time_us - previous.time_us)
                        for after, previous in zip(candidate.points, before.points, strict=True)
                    ),
                    default=0,
                )
                item.critic_iterations = pass_index
                item.critic_max_delta_us = max(item.critic_max_delta_us, delta)
                if delta > frame_us:
                    token.sweep = candidate
                    item.selected_sweep = candidate
                    maximum_delta = max(maximum_delta, delta)
                    token_deltas.append(delta)
                    corrections += 1
                    pass_corrections += 1
        pass_maxima.append(maximum_delta)
        if pass_corrections == 0 or maximum_delta <= frame_us:
            break

    converged = 0
    review = 0
    for line_index, line in enumerate(timeline.lines):
        next_line_start = (
            timeline.lines[line_index + 1].start_us
            if line_index + 1 < len(timeline.lines)
            else timeline.duration_us
        )
        for token_index, token in enumerate(line.tokens):
            item = evidence_by_token[token.id]
            if token.sweep is None:
                item.critic_converged = False
                if "CRITIC_NOT_CONVERGED" not in item.reason_codes:
                    item.reason_codes.append("CRITIC_NOT_CONVERGED")
                review += 1
                continue
            if _human_protected(line, token):
                item.critic_converged = True
                converged += 1
                continue

            onset_values = [
                candidate
                for track in tracks
                if (candidate := track.prominent_onset(token.start_us)) is not None
            ]
            onset = _consensus_acoustic(onset_values, min(required, len(tracks)))
            onset_mismatch = False
            if onset is not None:
                item.critic_selected_onset_us = onset[0]
                item.critic_onset_support = onset[1]
                onset_mismatch = (
                    onset[1] >= 0.50
                    and abs(onset[0] - token.start_us) > ACOUSTIC_BOUNDARY_TOLERANCE_US
                )

            sustain_mismatch = False
            if token_index == len(line.tokens) - 1 and tracks:
                limit_us = min(timeline.duration_us, next_line_start, token.end_us + 500_000)
                sustain_values = [
                    candidate
                    for track in tracks
                    if (
                        candidate := track.active_offset(
                            max(token.start_us, token.end_us - 500_000), limit_us
                        )
                    )
                    is not None
                ]
                sustain = _consensus_acoustic(sustain_values, min(required, len(tracks)), 50_000)
                if sustain is not None:
                    item.critic_selected_sustain_us = sustain[0]
                    item.critic_sustain_support = sustain[1]
                    sustain_mismatch = (
                        sustain[1] >= 0.45
                        and abs(sustain[0] - token.end_us) > ACOUSTIC_BOUNDARY_TOLERANCE_US
                    )

            targets = _curve_targets(token, item, required)
            residual = max(
                (
                    abs(token.sweep.points[index].time_us - target_us)
                    for index, target_us, _support, _source in targets
                ),
                default=0,
            )
            mapping_failed = "GRAPHEME_MAPPING_FAILED" in item.reason_codes
            curve_converged = (
                residual <= ACOUSTIC_BOUNDARY_TOLERANCE_US
                and not onset_mismatch
                and not sustain_mismatch
                and not mapping_failed
            )
            item.critic_converged = curve_converged
            if onset_mismatch and "CRITIC_ONSET_MISMATCH" not in item.reason_codes:
                item.reason_codes.append("CRITIC_ONSET_MISMATCH")
            if sustain_mismatch and "CRITIC_SUSTAIN_MISMATCH" not in item.reason_codes:
                item.reason_codes.append("CRITIC_SUSTAIN_MISMATCH")
            if not curve_converged:
                if "CRITIC_NOT_CONVERGED" not in item.reason_codes:
                    item.reason_codes.append("CRITIC_NOT_CONVERGED")
                item.auto_accepted = False
                token.sweep.verified = False
                token.sweep.confidence = min(0.77, token.sweep.confidence)
                review += 1
                continue

            converged += 1
            item.reason_codes = [
                reason
                for reason in item.reason_codes
                if reason
                not in {
                    "SWEEP_DISAGREEMENT",
                    "VOWEL_SUSTAIN_UNCERTAIN",
                    "CRITIC_ONSET_MISMATCH",
                    "CRITIC_SUSTAIN_MISMATCH",
                    "CRITIC_NOT_CONVERGED",
                }
            ]
            blocking = [
                reason for reason in item.reason_codes if reason != "ANCHOR_DRIFT"
            ]
            model_count = len({candidate.model_id for candidate in item.candidates})
            stem_count = len({candidate.stem_id for candidate in item.candidates})
            boundary_consensus = (
                item.consensus_count >= required
                and (required < 3 or (model_count >= 2 and stem_count >= 2))
                and item.acoustic_support >= 0.18
            )
            if boundary_consensus and not blocking:
                item.auto_accepted = True
                token.sweep.verified = True
                token.sweep.confidence = max(token.sweep.confidence, 0.90)
            item.selected_sweep = token.sweep

    return {
        "policy": "automatic-sweep-critic-v1",
        "maximum_passes": MAX_CRITIC_PASSES,
        "passes_executed": len(pass_maxima),
        "pass_max_delta_us": pass_maxima,
        "tokens_examined": examined,
        "corrections_applied": corrections,
        "tokens_converged": converged,
        "tokens_requiring_review": review,
        "median_correction_us": int(np.median(token_deltas)) if token_deltas else 0,
        "maximum_correction_us": max(token_deltas, default=0),
        "acoustic_stems": len(tracks),
    }
