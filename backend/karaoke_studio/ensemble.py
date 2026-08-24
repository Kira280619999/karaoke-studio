from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from .alignment import (
    ENERGY_POLICY,
    LYRIC_CTC_SPEC,
    SPEECH_CTC_SPEC,
    CTCModelSpec,
    EnergyAwareAligner,
    VietnameseCTCAligner,
    _line_is_protected,
    _regularize_line_sequence,
    _regularize_token_sequence,
)
from .critic import run_automatic_sweep_critic
from .media import sha256_file
from .models import (
    AlignmentCandidateEvidence,
    AlignmentEvidenceV1,
    GraphemeAlignmentCandidate,
    LineTiming,
    SweepCurveV1,
    SweepPointV1,
    TimelineIssue,
    TimelineV1,
    TimingSource,
    TokenAlignmentEvidence,
    TokenTiming,
)
from .motion import (
    GraphemeSpan,
    RhythmGrid,
    canonical_progress_ppm,
    interpolate_missing_times,
    is_sung_grapheme,
    is_vowel_grapheme,
    linear_sweep,
    regularize_sweep,
    resolve_font,
    smooth_subdivision_grid,
    split_graphemes,
    token_display_ranges,
    weighted_median,
)
from .settings import Settings

EventCallback = Callable[[float, str], None]
START_CONSENSUS_US = 33_000
END_CONSENSUS_US = 50_000
ANCHOR_DRIFT_US = 1_000_000
MIN_CTC_CONFIDENCE = 0.15
MAXIMUM_POLICY = "maximum-2-model-2-stem-global-sweep-critic-v6"
BALANCED_POLICY = "balanced-song-model-2-stem-global-sweep-critic-v6"
FAST_POLICY = "fast-song-model-1-stem-sweep-critic-v6"

REASON_MESSAGES = {
    "MODEL_DISAGREEMENT": "Các mô hình căn lời chưa đồng thuận.",
    "STEM_DISAGREEMENT": "Hai vocal stem cho timing khác nhau.",
    "FAST_PHRASE": "Câu hát nhanh cần nghe lại biên âm tiết.",
    "SUSTAIN_UNCERTAIN": "Điểm kết thúc nốt ngân chưa đủ chắc chắn.",
    "ANCHOR_DRIFT": "Âm thanh thật lệch xa mốc LRC.",
    "WEAK_VOCAL": "Tín hiệu giọng hát ở vùng này quá yếu.",
    "SWEEP_DISAGREEMENT": "Tốc độ quét bên trong âm tiết chưa đủ đồng thuận.",
    "VOWEL_SUSTAIN_UNCERTAIN": "Chuyển động qua nguyên âm ngân chưa đủ chắc chắn.",
    "BEAT_CONFLICT": "Nhịp nền và mốc phát âm đang xung đột.",
    "GRAPHEME_MAPPING_FAILED": "Không ánh xạ đủ mốc âm thanh vào chữ tiếng Việt gốc.",
    "CRITIC_ONSET_MISMATCH": "Vòng tự kiểm định thấy điểm bắt đầu chưa khớp vocal onset.",
    "CRITIC_SUSTAIN_MISMATCH": "Vòng tự kiểm định thấy chữ ngân kết thúc chưa khớp vocal offset.",
    "CRITIC_NOT_CONVERGED": "Đường quét chưa hội tụ sau vòng tự kiểm định.",
}


@dataclass
class AlignmentRun:
    spec: CTCModelSpec
    stem_id: str
    timeline: TimelineV1
    acoustic_support: dict[str, float]
    sustain_uncertain: set[str]
    graphemes: dict[str, list[GraphemeSpan]] = field(default_factory=dict)


def alignment_policy(profile: str) -> str:
    if profile == "balanced":
        return BALANCED_POLICY
    if profile == "fast":
        return FAST_POLICY
    return MAXIMUM_POLICY


def align_timeline_ensemble(
    timeline: TimelineV1,
    vocal_inputs: dict[str, Path],
    vocal_inputs_sha256: dict[str, str],
    accept_noncommercial_license: bool,
    profile: str,
    event: EventCallback,
    settings: Settings,
    cache_dir: Path,
    motion_profile: str = "vocal_hybrid",
    rhythm_inputs: dict[str, Path] | None = None,
) -> tuple[TimelineV1, AlignmentEvidenceV1, dict[str, object]]:
    """Align the entire ordered lyric contract and retain evidence per token.

    The audio is covered by overlapping, LRC-anchored windows. Candidate line
    positions are selected jointly across the whole song before token evidence
    is merged, which prevents an isolated repeated chorus from winning merely
    because it has a locally strong CTC score.
    """
    profile = profile if profile in {"maximum", "balanced", "fast"} else "maximum"
    motion_profile = (
        motion_profile
        if motion_profile in {"vocal_hybrid", "vocal_only", "linear"}
        else "vocal_hybrid"
    )
    stems = list(vocal_inputs.items())[: 1 if profile == "fast" else 2]
    if not stems:
        raise ValueError("Không có vocal stem cho alignment.")

    if not accept_noncommercial_license:
        return _energy_fallback(
            timeline,
            stems[0],
            vocal_inputs_sha256,
            profile,
            event,
            motion_profile,
            settings,
        )

    specs = [LYRIC_CTC_SPEC]
    if profile == "maximum":
        specs.append(SPEECH_CTC_SPEC)

    runs: list[AlignmentRun] = []
    failures: list[str] = []
    total_runs = max(1, len(specs) * len(stems))
    run_index = 0
    for spec in specs:
        aligner = VietnameseCTCAligner(settings, spec)
        if not aligner.available():
            failures.append(f"{spec.id}: dependencies unavailable")
            continue
        try:
            processor, model, device = aligner.runtime(event)
        except Exception as exc:
            failures.append(f"{spec.id}: {type(exc).__name__}")
            continue
        for stem_id, vocal_path in stems:
            run_index += 1
            event(
                0.61 + 0.17 * (run_index - 1) / total_runs,
                f"Đang căn toàn bài bằng {spec.label} · stem {stem_id}…",
            )
            try:
                run = _align_model_stem(
                    timeline,
                    stem_id,
                    vocal_path,
                    vocal_inputs_sha256.get(stem_id, ""),
                    aligner,
                    processor,
                    model,
                    device,
                    profile,
                    event,
                    cache_dir,
                    run_index,
                    total_runs,
                )
                runs.append(run)
            except Exception as exc:
                failures.append(f"{spec.id}/{stem_id}: {type(exc).__name__}")

    if not runs:
        result, evidence, report = _energy_fallback(
            timeline,
            stems[0],
            vocal_inputs_sha256,
            profile,
            event,
            motion_profile,
            settings,
        )
        evidence.degraded_reasons.extend(failures)
        report["runtime_failures"] = failures
        return result, evidence, report

    selected_centers = _select_global_line_centers(timeline, runs)
    result, token_evidence = _merge_runs(
        timeline,
        runs,
        selected_centers,
        profile,
        expected_models=len(specs),
        expected_stems=len(stems),
    )
    rhythm = (
        _rhythm_grid(rhythm_inputs or {}, cache_dir)
        if motion_profile == "vocal_hybrid"
        else RhythmGrid()
    )
    _apply_motion_curves(
        result,
        runs,
        token_evidence,
        profile,
        motion_profile,
        rhythm,
        resolve_font(settings, result.metadata.get("karaoke_font")),
    )
    event(0.805, "AI đang nghe lại và tự kiểm định tốc độ quét toàn bài…")
    critic_report = run_automatic_sweep_critic(
        result,
        token_evidence,
        dict(stems),
        profile,
    )
    reason_counts = Counter(
        reason for item in token_evidence for reason in item.reason_codes
    )
    auto_accepted = sum(item.auto_accepted for item in token_evidence)
    degraded = bool(failures) or len(runs) < len(specs) * len(stems)
    evidence = AlignmentEvidenceV1(
        timeline_revision=timeline.revision,
        alignment_profile=profile,
        motion_profile=motion_profile,
        degraded=degraded,
        degraded_reasons=failures,
        vocal_inputs_sha256=vocal_inputs_sha256,
        models=[
            {
                "id": spec.id,
                "revision": spec.revision,
                "license": spec.license,
                "role": "primary" if spec == LYRIC_CTC_SPEC else "secondary",
            }
            for spec in specs
        ],
        tokens=token_evidence,
    )
    report: dict[str, object] = {
        "schema_version": "1.2",
        "policy": alignment_policy(profile),
        "profile": profile,
        "motion_profile": motion_profile,
        "song_emission_chunk_seconds": 20,
        "song_emission_overlap_seconds": 2,
        "candidate_runs_expected": len(specs) * len(stems),
        "candidate_runs_completed": len(runs),
        "models": [
            {
                "id": spec.id,
                "revision": spec.revision,
                "role": "primary" if spec == LYRIC_CTC_SPEC else "secondary",
            }
            for spec in specs
        ],
        "rhythm_control_points": len(rhythm.times_us),
        "grapheme_candidate_count": sum(
            len(item.grapheme_candidates) for item in token_evidence
        ),
        "maximum_sweep_spread_us": max(
            (item.sweep_spread_us for item in token_evidence), default=0
        ),
        "beat_supported_tokens": sum(item.beat_support > 0 for item in token_evidence),
        "beat_support_strength_sum": round(
            sum(item.beat_support for item in token_evidence), 6
        ),
        "automatic_critic": critic_report,
        "auto_accepted_tokens": auto_accepted,
        "review_required_tokens": len(token_evidence) - auto_accepted,
        "reason_counts": dict(sorted(reason_counts.items())),
        "runtime_failures": failures,
        "degraded": degraded,
    }
    event(
        0.81,
        f"Ensemble hoàn tất: {auto_accepted} token tự động đạt, "
        f"{len(token_evidence) - auto_accepted} token cần nghe.",
    )
    return result, evidence, report


def _align_model_stem(
    timeline: TimelineV1,
    stem_id: str,
    vocal_path: Path,
    vocal_sha256: str,
    aligner: VietnameseCTCAligner,
    processor,
    model,
    device,
    profile: str,
    event: EventCallback,
    cache_dir: Path,
    run_index: int,
    total_runs: int,
) -> AlignmentRun:
    audio, sample_rate = sf.read(vocal_path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    duration_us = round(len(mono) * 1_000_000 / sample_rate)
    lines: list[LineTiming] = []
    support: dict[str, float] = {}
    uncertain: set[str] = set()
    graphemes: dict[str, list[GraphemeSpan]] = {}
    song_cache_key = hashlib.sha256(
        (
            f"{aligner.spec.id}@{aligner.spec.revision}:{aligner.spec.runtime_version}:"
            f"{vocal_sha256}:song-20s-overlap-2s"
        ).encode()
    ).hexdigest()
    song_emissions = aligner.song_emissions(
        mono,
        sample_rate,
        processor,
        model,
        device,
        cache_dir / aligner.spec.cache_name / stem_id / f"song-{song_cache_key}.npz",
    )
    for line_index, line in enumerate(timeline.lines):
        progress = 0.61 + 0.17 * (
            ((run_index - 1) + line_index / max(1, len(timeline.lines))) / total_runs
        )
        event(
            progress,
            f"{aligner.spec.label} · {stem_id}: câu {line_index + 1}/{len(timeline.lines)}…",
        )
        protected = line.source == TimingSource.LRC_ENHANCED or _line_is_protected(line)
        if protected:
            search_start_us = max(0, line.start_us - 300_000)
            search_end_us = min(duration_us, line.end_us + 300_000)
        else:
            search_start_us, search_end_us = _adaptive_search_window(
                line, duration_us, profile
            )
        try:
            aligned, line_graphemes = aligner._align_line_with_trace(
                line,
                mono,
                sample_rate,
                processor,
                model,
                device,
                search_start_us=search_start_us,
                search_end_us=search_end_us,
                song_emissions=song_emissions,
            )
            graphemes.update(line_graphemes)
            if protected:
                lines.append(line.model_copy(deep=True))
                support.update({token.id: 1.0 for token in line.tokens})
                continue
            next_start_us = (
                timeline.lines[line_index + 1].start_us
                if line_index + 1 < len(timeline.lines)
                else duration_us
            )
            aligned, line_support, sustain_uncertain = _refine_acoustic_boundaries(
                aligned,
                mono,
                sample_rate,
                next_start_us,
                round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
            )
            support.update(line_support)
            if sustain_uncertain:
                uncertain.add(line.tokens[-1].id)
            lines.append(aligned)
        except Exception:
            if protected:
                lines.append(line.model_copy(deep=True))
                support.update({token.id: 1.0 for token in line.tokens})
                continue
            fallback = EnergyAwareAligner()._align_line(line, mono, sample_rate)
            fallback.confidence = min(fallback.confidence, 0.60)
            for token in fallback.tokens:
                token.confidence = min(token.confidence, 0.60)
            lines.append(fallback)
            support.update({token.id: 0.0 for token in line.tokens})
    candidate = timeline.model_copy(deep=True)
    candidate.lines = _regularize_line_sequence(lines)
    return AlignmentRun(
        spec=aligner.spec,
        stem_id=stem_id,
        timeline=candidate,
        acoustic_support=support,
        sustain_uncertain=uncertain,
        graphemes=graphemes,
    )


def _adaptive_search_window(
    line: LineTiming, audio_duration_us: int, profile: str
) -> tuple[int, int]:
    maximum = 5_000_000 if profile == "maximum" else 2_000_000 if profile == "balanced" else 1_000_000
    line_duration = max(1, line.end_us - line.start_us)
    padding = min(maximum, max(900_000, line_duration))
    start_us = max(0, line.start_us - padding)
    end_us = min(audio_duration_us, line.end_us + padding)
    if end_us <= start_us:
        raise ValueError("Cửa sổ căn lời toàn bài không hợp lệ.")
    return start_us, end_us


def _refine_acoustic_boundaries(
    line: LineTiming,
    audio: np.ndarray,
    sample_rate: int,
    next_line_anchor_us: int,
    frame_us: int,
) -> tuple[LineTiming, dict[str, float], bool]:
    result = line.model_copy(deep=True)
    region_start_us = max(0, result.start_us - 200_000)
    region_end_us = min(
        round(len(audio) * 1_000_000 / sample_rate),
        max(result.end_us + 3_000_000, next_line_anchor_us),
    )
    start_sample = round(region_start_us * sample_rate / 1_000_000)
    end_sample = round(region_end_us * sample_rate / 1_000_000)
    segment = audio[start_sample:end_sample]
    rms, flux, hop_us = _acoustic_features(segment, sample_rate)
    if not len(rms):
        return result, {token.id: 0.0 for token in result.tokens}, True
    onset = _normalize(np.maximum(0.0, np.diff(rms, prepend=rms[0]))) * 0.45
    onset += _normalize(flux) * 0.55
    activity = _normalize(rms)
    support: dict[str, float] = {}

    median_duration = float(
        np.median([token.end_us - token.start_us for token in result.tokens])
    )
    radius_us = 55_000 if median_duration <= 180_000 else 90_000 if median_duration <= 500_000 else 130_000
    for token in result.tokens:
        center = round((token.start_us - region_start_us) / hop_us)
        radius = max(1, round(radius_us / hop_us))
        lower = max(0, center - radius)
        upper = min(len(onset), center + radius + 1)
        if upper <= lower:
            support[token.id] = 0.0
            continue
        local = onset[lower:upper]
        peak_index = lower + int(np.argmax(local))
        peak_support = float(onset[peak_index])
        support[token.id] = max(0.0, min(1.0, peak_support))
        if peak_support >= 0.24:
            proposed = region_start_us + peak_index * hop_us
            token.start_us = max(0, proposed)

    for previous, current in zip(result.tokens, result.tokens[1:], strict=False):
        boundary = max(previous.start_us + frame_us, current.start_us)
        current.start_us = boundary
        previous.end_us = boundary

    last = result.tokens[-1]
    sustain_end, sustain_support, sustain_uncertain = _find_sustain_offset(
        last,
        rms,
        activity,
        region_start_us,
        hop_us,
        region_end_us,
        frame_us,
    )
    last.end_us = max(last.start_us + frame_us, sustain_end)
    support[last.id] = max(support.get(last.id, 0.0), sustain_support)
    result.start_us = result.tokens[0].start_us
    result.end_us = result.tokens[-1].end_us
    result = _regularize_token_sequence(result, END_CONSENSUS_US)
    return result, support, sustain_uncertain


def _acoustic_features(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray, int]:
    window = max(32, round(sample_rate * 0.020))
    hop = max(16, round(sample_rate * 0.010))
    if len(audio) < window:
        return np.array([]), np.array([]), round(hop * 1_000_000 / sample_rate)
    positions = range(0, len(audio) - window + 1, hop)
    hann = np.hanning(window).astype(np.float32)
    rms_values: list[float] = []
    flux_values: list[float] = []
    previous_spectrum: np.ndarray | None = None
    for position in positions:
        frame = audio[position : position + window]
        rms_values.append(float(np.sqrt(np.mean(np.square(frame)) + 1e-12)))
        spectrum = np.abs(np.fft.rfft(frame * hann))
        if previous_spectrum is None:
            flux_values.append(0.0)
        else:
            flux_values.append(float(np.mean(np.maximum(0.0, spectrum - previous_spectrum))))
        previous_spectrum = spectrum
    return (
        np.asarray(rms_values, dtype=np.float64),
        np.asarray(flux_values, dtype=np.float64),
        round(hop * 1_000_000 / sample_rate),
    )


def _find_sustain_offset(
    token: TokenTiming,
    rms: np.ndarray,
    activity: np.ndarray,
    region_start_us: int,
    hop_us: int,
    limit_us: int,
    frame_us: int,
) -> tuple[int, float, bool]:
    start_bin = max(0, round((token.start_us - region_start_us) / hop_us))
    limit_bin = min(len(rms), max(start_bin + 1, round((limit_us - region_start_us) / hop_us)))
    if limit_bin <= start_bin:
        return token.end_us, 0.0, True
    local = rms[start_bin:limit_bin]
    floor = float(np.percentile(local, 20))
    ceiling = float(np.percentile(local, 90))
    threshold = floor + max(1e-7, ceiling - floor) * 0.18
    scan_bin = max(start_bin, round((token.end_us - region_start_us - 100_000) / hop_us))
    quiet_bins = max(3, round(80_000 / hop_us))
    active_seen = False
    for index in range(start_bin, max(start_bin, limit_bin - quiet_bins + 1)):
        if rms[index] >= threshold:
            active_seen = True
        if index < scan_bin or not active_seen:
            continue
        if np.all(rms[index : index + quiet_bins] < threshold):
            offset = region_start_us + index * hop_us
            strength = float(np.max(activity[start_bin : index + 1])) if index >= start_bin else 0.0
            return max(token.start_us + frame_us, offset), strength, False
    return max(token.start_us + frame_us, min(limit_us, token.end_us)), float(np.max(activity[start_bin:limit_bin])), True


def _normalize(values: np.ndarray) -> np.ndarray:
    if not len(values):
        return values
    low = float(np.percentile(values, 10))
    high = float(np.percentile(values, 95))
    return np.clip((values - low) / max(1e-9, high - low), 0.0, 1.0)


def _select_global_line_centers(
    base: TimelineV1, runs: list[AlignmentRun]
) -> dict[str, tuple[int, int]]:
    states: list[list[tuple[int, int, float]]] = []
    for line_index, base_line in enumerate(base.lines):
        candidates = [
            (
                run.timeline.lines[line_index].start_us,
                run.timeline.lines[line_index].end_us,
                run.timeline.lines[line_index].confidence,
            )
            for run in runs
            if run.timeline.lines[line_index].source == TimingSource.CTC
        ]
        if candidates:
            candidates.append(
                (
                    round(float(np.median([item[0] for item in candidates]))),
                    round(float(np.median([item[1] for item in candidates]))),
                    float(np.median([item[2] for item in candidates])),
                )
            )
        else:
            candidates = [(base_line.start_us, base_line.end_us, base_line.confidence)]
        states.append(candidates)

    costs: list[np.ndarray] = []
    links: list[np.ndarray] = []
    for line_index, candidates in enumerate(states):
        line = base.lines[line_index]
        node = np.asarray(
            [
                (1.0 - confidence) * 1.4
                + abs(start - line.start_us) / 5_000_000 * 0.22
                for start, _end, confidence in candidates
            ],
            dtype=np.float64,
        )
        if line_index == 0:
            costs.append(node)
            links.append(np.full(len(candidates), -1, dtype=np.int16))
            continue
        previous_states = states[line_index - 1]
        previous_costs = costs[-1]
        desired_delta = max(1, line.start_us - base.lines[line_index - 1].start_us)
        current_cost = np.full(len(candidates), np.inf, dtype=np.float64)
        current_link = np.full(len(candidates), -1, dtype=np.int16)
        for current_index, (start, _end, _confidence) in enumerate(candidates):
            for previous_index, (previous_start, previous_end, _previous_confidence) in enumerate(previous_states):
                if start <= previous_start or start < previous_end - 100_000:
                    continue
                actual_delta = start - previous_start
                transition = abs(actual_delta - desired_delta) / max(500_000, desired_delta) * 0.35
                score = previous_costs[previous_index] + node[current_index] + transition
                if score < current_cost[current_index]:
                    current_cost[current_index] = score
                    current_link[current_index] = previous_index
        if not np.isfinite(current_cost).any():
            best_previous = int(np.argmin(previous_costs))
            current_cost = node + previous_costs[best_previous] + 5.0
            current_link.fill(best_previous)
        costs.append(current_cost)
        links.append(current_link)

    selected: dict[str, tuple[int, int]] = {}
    state_index = int(np.argmin(costs[-1]))
    for line_index in range(len(states) - 1, -1, -1):
        start, end, _confidence = states[line_index][state_index]
        selected[base.lines[line_index].id] = (start, end)
        predecessor = int(links[line_index][state_index])
        if predecessor >= 0:
            state_index = predecessor
    return selected


def _merge_runs(
    base: TimelineV1,
    runs: list[AlignmentRun],
    selected_centers: dict[str, tuple[int, int]],
    profile: str,
    expected_models: int,
    expected_stems: int,
) -> tuple[TimelineV1, list[TokenAlignmentEvidence]]:
    result = base.model_copy(deep=True)
    evidence: list[TokenAlignmentEvidence] = []
    required = 3 if profile == "maximum" else 2 if profile == "balanced" else 1
    for line_index, line in enumerate(result.lines):
        base_line = base.lines[line_index]
        if _line_is_protected(base_line) or base_line.source == TimingSource.LRC_ENHANCED:
            for token in line.tokens:
                evidence.append(
                    TokenAlignmentEvidence(
                        line_id=line.id,
                        token_id=token.id,
                        text=token.text,
                        selected_start_us=token.start_us,
                        selected_end_us=token.end_us,
                        start_spread_us=0,
                        end_spread_us=0,
                        acoustic_support=1.0,
                        consensus_count=0,
                        auto_accepted=token.verified or token.locked,
                    )
                )
            continue

        center_start, _center_end = selected_centers[line.id]
        token_candidates: list[list[tuple[AlignmentRun, TokenTiming]]] = [
            [] for _token in line.tokens
        ]
        for run in runs:
            candidate_line = run.timeline.lines[line_index]
            if abs(candidate_line.start_us - center_start) > max(
                1_500_000, base_line.end_us - base_line.start_us
            ):
                continue
            for token_index, token in enumerate(candidate_line.tokens):
                if (
                    token.source == TimingSource.CTC
                    and token.confidence >= run.spec.min_confidence
                ):
                    token_candidates[token_index].append((run, token))

        merged_tokens: list[TokenTiming] = []
        pending: list[tuple[TokenAlignmentEvidence, bool]] = []
        for token_index, (base_token, candidates) in enumerate(
            zip(base_line.tokens, token_candidates, strict=True)
        ):
            if not candidates:
                fallback = max(
                    (run.timeline.lines[line_index].tokens[token_index] for run in runs),
                    key=lambda item: item.confidence,
                )
                selected = fallback.model_copy(
                    update={"confidence": min(0.60, fallback.confidence), "verified": False}
                )
                merged_tokens.append(selected)
                item = TokenAlignmentEvidence(
                    line_id=line.id,
                    token_id=base_token.id,
                    text=base_token.text,
                    selected_start_us=selected.start_us,
                    selected_end_us=selected.end_us,
                    start_spread_us=0,
                    end_spread_us=0,
                    acoustic_support=0.0,
                    consensus_count=0,
                    reason_codes=["MODEL_DISAGREEMENT", "WEAK_VOCAL"],
                )
                pending.append((item, False))
                continue

            starts = [token.start_us for _run, token in candidates]
            ends = [token.end_us for _run, token in candidates]
            weights = [run.spec.weight * max(MIN_CTC_CONFIDENCE, token.confidence) for run, token in candidates]
            selected_start = _weighted_median(starts, weights)
            selected_end = max(selected_start + 1, _weighted_median(ends, weights))
            agreeing = [
                (run, token)
                for run, token in candidates
                if abs(token.start_us - selected_start) <= START_CONSENSUS_US
                and abs(token.end_us - selected_end) <= END_CONSENSUS_US
            ]
            acoustic_support = float(
                np.median([run.acoustic_support.get(token.id, 0.0) for run, token in candidates])
            )
            reasons: list[str] = []
            model_groups: dict[str, list[int]] = defaultdict(list)
            stem_groups: dict[str, list[int]] = defaultdict(list)
            for run, token in candidates:
                model_groups[run.spec.id].append(token.start_us)
                stem_groups[run.stem_id].append(token.start_us)
            model_centers = [float(np.median(items)) for items in model_groups.values()]
            stem_centers = [float(np.median(items)) for items in stem_groups.values()]
            if len(model_groups) < expected_models or (
                len(model_centers) > 1 and max(model_centers) - min(model_centers) > END_CONSENSUS_US
            ):
                reasons.append("MODEL_DISAGREEMENT")
            if len(stem_groups) < expected_stems or (
                len(stem_centers) > 1 and max(stem_centers) - min(stem_centers) > END_CONSENSUS_US
            ):
                reasons.append("STEM_DISAGREEMENT")
            if abs(center_start - base_line.start_us) > ANCHOR_DRIFT_US:
                reasons.append("ANCHOR_DRIFT")
            if acoustic_support < 0.18:
                reasons.append("WEAK_VOCAL")
            duration_us = selected_end - selected_start
            if duration_us <= 150_000 and len(agreeing) < required:
                reasons.append("FAST_PHRASE")
            sustain_uncertain = token_index == len(line.tokens) - 1 and (
                len(agreeing) < required
                or sum(base_token.id in run.sustain_uncertain for run, _token in candidates)
                > len(candidates) // 2
            )
            if sustain_uncertain:
                reasons.append("SUSTAIN_UNCERTAIN")

            model_diversity_ok = len(model_groups) >= expected_models
            stem_diversity_ok = len(stem_groups) >= expected_stems
            blocking_reasons = [
                reason
                for reason in reasons
                if not (
                    reason == "ANCHOR_DRIFT"
                    and len(agreeing) >= required
                    and acoustic_support >= 0.18
                )
            ]
            auto_accept = (
                len(agreeing) >= required
                and model_diversity_ok
                and stem_diversity_ok
                and not blocking_reasons
            )
            confidence = (
                min(0.99, 0.90 + 0.02 * min(4, len(agreeing)) + 0.03 * acoustic_support)
                if auto_accept
                else min(0.77, max(token.confidence for _run, token in candidates))
            )
            selected = base_token.model_copy(
                update={
                    "start_us": selected_start,
                    "end_us": selected_end,
                    "confidence": confidence,
                    "source": TimingSource.CTC,
                    "verified": False,
                }
            )
            merged_tokens.append(selected)
            item = TokenAlignmentEvidence(
                line_id=line.id,
                token_id=base_token.id,
                text=base_token.text,
                selected_start_us=selected_start,
                selected_end_us=selected_end,
                start_spread_us=max(starts) - min(starts),
                end_spread_us=max(ends) - min(ends),
                acoustic_support=acoustic_support,
                consensus_count=len(agreeing),
                auto_accepted=auto_accept,
                reason_codes=list(dict.fromkeys(reasons)),
                candidates=[
                    AlignmentCandidateEvidence(
                        model_id=run.spec.id,
                        model_revision=run.spec.revision,
                        stem_id=run.stem_id,
                        start_us=token.start_us,
                        end_us=token.end_us,
                        confidence=token.confidence,
                    )
                    for run, token in candidates
                ],
            )
            pending.append((item, auto_accept))

        line.tokens = merged_tokens
        line.start_us = merged_tokens[0].start_us
        line.end_us = merged_tokens[-1].end_us
        line.source = TimingSource.CTC
        line.confidence = float(np.mean([token.confidence for token in merged_tokens]))
        line.verified = False
        line = _regularize_token_sequence(line, END_CONSENSUS_US)
        result.lines[line_index] = line
        for item, auto_accept in pending:
            token = next(token for token in line.tokens if token.id == item.token_id)
            item.selected_start_us = token.start_us
            item.selected_end_us = token.end_us
            if token.confidence < 0.78 and not (token.verified or token.locked):
                item.auto_accepted = False
            elif auto_accept:
                item.auto_accepted = True
            evidence.append(item)

    evidence_by_token = {item.token_id: item for item in evidence}
    frame_us = round(1_000_000 * result.fps_denominator / result.fps_numerator)
    for line in result.lines:
        if _line_is_protected(line):
            continue
        changed_ids = _ensure_minimum_token_frame(line, frame_us, result.duration_us)
        for token_id in changed_ids:
            item = evidence_by_token[token_id]
            item.auto_accepted = False
            if "FAST_PHRASE" not in item.reason_codes:
                item.reason_codes.append("FAST_PHRASE")
    result.lines = _regularize_line_sequence(result.lines)
    for line_index in range(1, len(result.lines)):
        previous = result.lines[line_index - 1]
        current = result.lines[line_index]
        if current.start_us >= previous.start_us:
            continue
        target_start = max(previous.start_us + frame_us, base.lines[line_index].start_us)
        delta_us = target_start - current.start_us
        if current.end_us + delta_us > result.duration_us:
            delta_us = result.duration_us - current.end_us
        current.start_us += delta_us
        current.end_us += delta_us
        current.confidence = min(current.confidence, 0.70)
        for token in current.tokens:
            token.start_us += delta_us
            token.end_us += delta_us
            token.confidence = min(token.confidence, 0.70)
            item = evidence_by_token[token.id]
            item.auto_accepted = False
            if "MODEL_DISAGREEMENT" not in item.reason_codes:
                item.reason_codes.append("MODEL_DISAGREEMENT")
    for line in result.lines:
        for token in line.tokens:
            item = evidence_by_token[token.id]
            item.selected_start_us = token.start_us
            item.selected_end_us = token.end_us
            if token.confidence < 0.78 and not (token.verified or token.locked):
                item.auto_accepted = False
    return result, evidence


def _ensure_minimum_token_frame(
    line: LineTiming, frame_us: int, timeline_duration_us: int
) -> set[str]:
    changed = {
        token.id for token in line.tokens if token.end_us - token.start_us < frame_us
    }
    if not changed:
        return changed
    boundaries = [line.tokens[0].start_us]
    boundaries.extend(token.start_us for token in line.tokens[1:])
    boundaries.append(line.tokens[-1].end_us)
    for index in range(1, len(boundaries)):
        boundaries[index] = max(boundaries[index], boundaries[index - 1] + frame_us)
    if boundaries[-1] > timeline_duration_us:
        boundaries[-1] = timeline_duration_us
        for index in range(len(boundaries) - 2, -1, -1):
            boundaries[index] = min(boundaries[index], boundaries[index + 1] - frame_us)
    if boundaries[0] < 0:
        return changed
    for index, token in enumerate(line.tokens):
        token.start_us = boundaries[index]
        token.end_us = boundaries[index + 1]
        if token.id in changed:
            token.confidence = min(token.confidence, 0.77)
    line.start_us = boundaries[0]
    line.end_us = boundaries[-1]
    line.confidence = float(np.mean([token.confidence for token in line.tokens]))
    return changed


def _weighted_median(values: list[int], weights: list[float]) -> int:
    ordered = sorted(zip(values, weights, strict=True), key=lambda item: item[0])
    threshold = sum(weights) / 2
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return int(value)
    return int(ordered[-1][0])


def _rhythm_grid(inputs: dict[str, Path], cache_dir: Path) -> RhythmGrid:
    path = next((candidate for candidate in inputs.values() if candidate.is_file()), None)
    if path is None:
        return RhythmGrid()
    checksum = sha256_file(path)
    cache_path = cache_dir / "rhythm" / f"{checksum}.json"
    if cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return RhythmGrid(
                tuple(int(value) for value in payload["times_us"]),
                tuple(float(value) for value in payload["strengths"]),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass

    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    beat_times: list[int] = []
    beat_strengths: list[float] = []
    try:
        import librosa

        analysis_rate = 22_050
        if sample_rate != analysis_rate:
            mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=analysis_rate)
            sample_rate = analysis_rate
        hop_length = 512
        onset = librosa.onset.onset_strength(y=mono, sr=sample_rate, hop_length=hop_length)
        _tempo, frames = librosa.beat.beat_track(
            onset_envelope=onset,
            sr=sample_rate,
            hop_length=hop_length,
        )
        normalized = _normalize(np.asarray(onset, dtype=np.float64))
        for frame in np.asarray(frames, dtype=np.int64).tolist():
            beat_times.append(round(frame * hop_length * 1_000_000 / sample_rate))
            beat_strengths.append(float(normalized[frame]) if frame < len(normalized) else 0.5)
        grid = smooth_subdivision_grid(beat_times, beat_strengths)
    except (ImportError, AttributeError, ValueError):
        rms, flux, hop_us = _acoustic_features(mono, sample_rate)
        onset = _normalize(np.maximum(0.0, np.diff(rms, prepend=rms[0]))) * 0.35
        onset += _normalize(flux) * 0.65
        if len(onset):
            threshold = float(np.percentile(onset, 82))
            minimum_bins = max(1, round(80_000 / hop_us))
            last = -minimum_bins
            for index in range(1, len(onset) - 1):
                if (
                    onset[index] >= threshold
                    and onset[index] >= onset[index - 1]
                    and onset[index] >= onset[index + 1]
                    and index - last >= minimum_bins
                ):
                    beat_times.append(index * hop_us)
                    beat_strengths.append(float(onset[index]))
                    last = index
        grid = RhythmGrid(tuple(beat_times), tuple(beat_strengths))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {"checksum": checksum, "times_us": grid.times_us, "strengths": grid.strengths},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return grid


def _apply_motion_curves(
    timeline: TimelineV1,
    runs: list[AlignmentRun],
    evidence: list[TokenAlignmentEvidence],
    profile: str,
    motion_profile: str,
    rhythm: RhythmGrid,
    font_path: Path,
) -> None:
    evidence_by_token = {item.token_id: item for item in evidence}
    required = 3 if profile == "maximum" else 2 if profile == "balanced" else 1
    for line in timeline.lines:
        for token_index, token in enumerate(line.tokens):
            item = evidence_by_token[token.id]
            human_curve = bool(
                token.sweep is not None
                and (
                    line.locked
                    or line.verified
                    or token.locked
                    or token.verified
                    or token.source == TimingSource.MANUAL
                    or token.sweep.source == "manual_rescaled"
                )
            )
            if human_curve:
                item.selected_sweep = token.sweep
                item.auto_accepted = bool(token.verified or token.locked)
                continue
            if motion_profile == "linear":
                token.sweep = linear_sweep(
                    line,
                    token_index,
                    "lrc_linear",
                    min(0.77, token.confidence),
                    token.verified,
                    font_path,
                )
                item.selected_sweep = token.sweep
                if not token.verified and "SWEEP_DISAGREEMENT" not in item.reason_codes:
                    item.reason_codes.append("SWEEP_DISAGREEMENT")
                    item.auto_accepted = False
                continue
            curve, candidates, spread_us, beat_support, reasons = _ensemble_token_sweep(
                line,
                token_index,
                runs,
                required,
                motion_profile,
                rhythm,
                font_path,
                "SUSTAIN_UNCERTAIN" in item.reason_codes,
                item.auto_accepted,
            )
            for reason in reasons:
                if reason not in item.reason_codes:
                    item.reason_codes.append(reason)
            curve.verified = item.auto_accepted and not reasons
            if not curve.verified:
                item.auto_accepted = False
                curve.confidence = min(0.77, curve.confidence)
            token.sweep = curve
            item.sweep_spread_us = spread_us
            item.beat_support = beat_support
            item.selected_sweep = curve
            item.grapheme_candidates = candidates
    timeline.schema_version = "1.1"


def _ensemble_token_sweep(
    line: LineTiming,
    token_index: int,
    runs: list[AlignmentRun],
    required: int,
    motion_profile: str,
    rhythm: RhythmGrid,
    font_path: Path,
    sustain_uncertain: bool,
    boundary_auto_accepted: bool,
) -> tuple[SweepCurveV1, list[GraphemeAlignmentCandidate], int, float, list[str]]:
    token = line.tokens[token_index]
    display_start, display_end = token_display_ranges(line)[token_index]
    token_graphemes = split_graphemes(token.text)
    display_indexes = [display_start]
    display_indexes.extend(display_start + end for _start, end, _text in token_graphemes)
    if display_indexes[-1] != display_end:
        display_indexes.append(display_end)
    progress = canonical_progress_ppm(line.text, display_indexes, font_path)
    boundary_times: list[int | None] = [None] * len(display_indexes)
    boundary_times[0] = token.start_us
    boundary_times[-1] = token.end_us
    evidence: list[GraphemeAlignmentCandidate] = []
    maximum_spread = 0
    beat_strengths: list[float] = []
    reasons: list[str] = []
    trace_by_grapheme: dict[int, list[tuple[AlignmentRun, GraphemeSpan]]] = defaultdict(list)
    for run in runs:
        run_token = next(
            (
                candidate
                for run_line in run.timeline.lines
                for candidate in run_line.tokens
                if candidate.id == token.id
            ),
            None,
        )
        boundary_tolerance = max(150_000, (token.end_us - token.start_us) // 2)
        boundary_inlier = run_token is not None and max(
            abs(run_token.start_us - token.start_us),
            abs(run_token.end_us - token.end_us),
        ) <= boundary_tolerance
        for span in run.graphemes.get(token.id, []):
            if boundary_inlier:
                trace_by_grapheme[span.grapheme_index].append((run, span))
            evidence.append(
                GraphemeAlignmentCandidate(
                    model_id=run.spec.id,
                    model_revision=run.spec.revision,
                    stem_id=run.stem_id,
                    grapheme_index=span.grapheme_index,
                    text=span.text,
                    start_us=span.start_us,
                    end_us=span.end_us,
                    confidence=span.confidence,
                )
            )

    missing_mapping = False
    for grapheme_index, (_start, _end, value) in enumerate(token_graphemes):
        if not is_sung_grapheme(value):
            continue
        candidates = trace_by_grapheme.get(grapheme_index, [])
        if not candidates:
            missing_mapping = True
            continue
        values = [span.end_us for _run, span in candidates]
        weights = [run.spec.weight * max(0.03, span.confidence) for run, span in candidates]
        selected = weighted_median(values, weights)
        spread = max(values) - min(values)
        maximum_spread = max(maximum_spread, spread)
        agreeing = sum(abs(value_us - selected) <= START_CONSENSUS_US for value_us in values)
        primary = [
            (run, span)
            for run, span in candidates
            if run.spec.id == LYRIC_CTC_SPEC.id
        ]
        primary_stem_consensus = (
            len({run.stem_id for run, _span in primary}) >= 2
            and max(span.end_us for _run, span in primary)
            - min(span.end_us for _run, span in primary)
            <= START_CONSENSUS_US
        )
        stable_boundary = agreeing >= required or (
            boundary_auto_accepted and primary_stem_consensus
        )
        if not stable_boundary:
            if motion_profile == "vocal_hybrid":
                nearby = rhythm.nearest(selected, 120_000)
                if nearby is not None:
                    beat_us, strength = nearby
                    if abs(beat_us - selected) <= 40_000 and strength >= 0.35:
                        selected = beat_us
                        beat_strengths.append(strength)
                    elif strength >= 0.55:
                        reasons.append("BEAT_CONFLICT")
            reasons.append("SWEEP_DISAGREEMENT")
        boundary_index = min(len(boundary_times) - 1, grapheme_index + 1)
        boundary_times[boundary_index] = max(token.start_us, min(token.end_us, selected))

    vowel_indexes = [
        index for index, (_start, _end, value) in enumerate(token_graphemes) if is_vowel_grapheme(value)
    ]
    if vowel_indexes:
        first_vowel = vowel_indexes[0]
        last_vowel = vowel_indexes[-1]
        vowel_candidates = trace_by_grapheme.get(first_vowel, [])
        if vowel_candidates:
            values = [span.start_us for _run, span in vowel_candidates]
            weights = [run.spec.weight * max(0.03, span.confidence) for run, span in vowel_candidates]
            boundary_times[first_vowel] = max(
                token.start_us, min(token.end_us, weighted_median(values, weights))
            )
        coda = next(
            (
                trace_by_grapheme[index]
                for index in range(last_vowel + 1, len(token_graphemes))
                if trace_by_grapheme.get(index)
            ),
            None,
        )
        if coda:
            values = [span.start_us for _run, span in coda]
            weights = [run.spec.weight * max(0.03, span.confidence) for run, span in coda]
            sustain_end = weighted_median(values, weights)
        else:
            sustain_end = token.end_us
        boundary_times[min(len(boundary_times) - 1, last_vowel + 1)] = max(
            token.start_us + 1, min(token.end_us, sustain_end)
        )
        if sustain_uncertain and token.end_us - token.start_us >= 500_000:
            reasons.append("VOWEL_SUSTAIN_UNCERTAIN")
    else:
        missing_mapping = True

    if missing_mapping:
        reasons.append("GRAPHEME_MAPPING_FAILED")
    times = interpolate_missing_times(boundary_times, token.start_us, token.end_us)
    points = [
        SweepPointV1(time_us=time_us, line_progress_ppm=progress_ppm)
        for time_us, progress_ppm in zip(times, progress, strict=True)
    ]
    unique_reasons = list(dict.fromkeys(reasons))
    confidence = (
        min(0.99, 0.90 + 0.02 * min(4, len(runs)))
        if not unique_reasons
        else min(0.77, token.confidence)
    )
    curve = regularize_sweep(
        SweepCurveV1(
            source="ensemble_ctc",
            confidence=confidence,
            verified=False,
            points=points,
        ),
        token.start_us,
        token.end_us,
    )
    return (
        curve,
        evidence,
        maximum_spread,
        float(np.mean(beat_strengths)) if beat_strengths else 0.0,
        unique_reasons,
    )


def _energy_fallback(
    timeline: TimelineV1,
    vocal_input: tuple[str, Path],
    vocal_inputs_sha256: dict[str, str],
    profile: str,
    event: EventCallback,
    motion_profile: str,
    settings: Settings | None,
) -> tuple[TimelineV1, AlignmentEvidenceV1, dict[str, object]]:
    stem_id, vocal_path = vocal_input
    result = EnergyAwareAligner().align(timeline.model_copy(deep=True), vocal_path, event)
    tokens: list[TokenAlignmentEvidence] = []
    font_path = (
        resolve_font(settings, result.metadata.get("karaoke_font"))
        if settings is not None
        else None
    )
    for line in result.lines:
        for token_index, token in enumerate(line.tokens):
            if _line_is_protected(line) or line.source == TimingSource.LRC_ENHANCED:
                reasons: list[str] = []
            else:
                token.confidence = min(0.76, token.confidence)
                reasons = ["MODEL_DISAGREEMENT", "SWEEP_DISAGREEMENT"]
            token.sweep = linear_sweep(
                line,
                token_index,
                "energy_linear",
                min(0.76, token.confidence),
                bool(token.verified or token.locked),
                font_path,
            )
            tokens.append(
                TokenAlignmentEvidence(
                    line_id=line.id,
                    token_id=token.id,
                    text=token.text,
                    selected_start_us=token.start_us,
                    selected_end_us=token.end_us,
                    start_spread_us=0,
                    end_spread_us=0,
                    acoustic_support=max(0.0, min(1.0, token.confidence)),
                    consensus_count=0,
                    auto_accepted=not reasons and (token.verified or token.locked),
                    reason_codes=reasons,
                    selected_sweep=token.sweep,
                )
            )
    evidence = AlignmentEvidenceV1(
        timeline_revision=timeline.revision,
        alignment_profile=profile,
        motion_profile=motion_profile,
        degraded=True,
        degraded_reasons=["Vietnamese noncommercial model pack was not enabled."],
        vocal_inputs_sha256=vocal_inputs_sha256,
        models=[],
        tokens=tokens,
    )
    report: dict[str, object] = {
        "schema_version": "1.1",
        "policy": ENERGY_POLICY,
        "profile": profile,
        "motion_profile": motion_profile,
        "candidate_runs_expected": 0,
        "candidate_runs_completed": 0,
        "auto_accepted_tokens": sum(item.auto_accepted for item in tokens),
        "review_required_tokens": sum(not item.auto_accepted for item in tokens),
        "reason_counts": {"MODEL_DISAGREEMENT": sum(bool(item.reason_codes) for item in tokens)},
        "runtime_failures": [],
        "degraded": True,
    }
    event(0.81, "Chưa bật model phi thương mại; đã tạo fallback và khóa Verified cho vùng chưa chắc chắn.")
    return result, evidence, report


def evidence_review_issues(
    timeline: TimelineV1,
    evidence: AlignmentEvidenceV1 | None,
    require_verified: bool = False,
) -> list[TimelineIssue]:
    if evidence is None or evidence.timeline_revision != timeline.revision:
        return []
    token_map = {token.id: token for line in timeline.lines for token in line.tokens}
    issues: list[TimelineIssue] = []
    for item in evidence.tokens:
        token = token_map.get(item.token_id)
        if token is None or token.verified or item.auto_accepted:
            continue
        for reason in item.reason_codes:
            issues.append(
                TimelineIssue(
                    code=reason,
                    message=(
                        f"{REASON_MESSAGES.get(reason, reason)} Phải nghe và xác nhận trước Final."
                        if require_verified
                        else REASON_MESSAGES.get(reason, reason)
                    ),
                    line_id=item.line_id,
                    token_id=item.token_id,
                    severity="error" if require_verified else "warning",
                )
            )
    return issues
