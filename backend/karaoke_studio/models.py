from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ProjectState(StrEnum):
    IMPORTED = "IMPORTED"
    SEPARATED = "SEPARATED"
    ALIGNED = "ALIGNED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    VERIFIED = "VERIFIED"
    RENDERED = "RENDERED"
    FAILED = "FAILED"


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TimingSource(StrEnum):
    LRC_LINE = "lrc_line"
    LRC_ENHANCED = "lrc_enhanced"
    ENERGY = "energy_aware"
    CTC = "vietnamese_ctc"
    MANUAL = "manual"


class SweepPointV1(BaseModel):
    time_us: int = Field(ge=0)
    line_progress_ppm: int = Field(ge=0, le=1_000_000)


class SweepCurveV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    source: Literal["ensemble_ctc", "energy_linear", "manual_rescaled", "lrc_linear"]
    confidence: float = Field(ge=0, le=1)
    verified: bool = False
    points: list[SweepPointV1] = Field(min_length=2)


class TokenTiming(BaseModel):
    id: str
    text: str
    normalized: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    source: TimingSource
    verified: bool = False
    locked: bool = False
    sweep: SweepCurveV1 | None = None


class LineTiming(BaseModel):
    id: str
    text: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    source: TimingSource
    verified: bool = False
    locked: bool = False
    tokens: list[TokenTiming]


class TimelineV1(BaseModel):
    schema_version: Literal["1.0", "1.1"] = "1.1"
    revision: int = Field(default=1, ge=1)
    language: str = "vi"
    duration_us: int = Field(gt=0)
    fps_numerator: int = Field(default=60, gt=0)
    fps_denominator: int = Field(default=1, gt=0)
    metadata: dict[str, str] = Field(default_factory=dict)
    lines: list[LineTiming]


class TimelineIssue(BaseModel):
    code: str
    message: str
    line_id: str | None = None
    token_id: str | None = None
    severity: Literal["warning", "error"] = "error"


class ProjectRecord(BaseModel):
    id: str
    title: str
    artist: str = ""
    state: ProjectState
    created_at: str
    updated_at: str
    source_name: str
    lrc_name: str
    background_name: str | None = None
    background_mode: Literal["original", "custom"] = "original"
    source_sha256: str
    duration_us: int
    width: int
    height: int
    fps: str
    has_audio: bool
    selected_instrumental: str | None = None
    instrumental_confirmed: bool = False
    error: str | None = None


class JobRecord(BaseModel):
    id: str
    project_id: str
    kind: Literal["process", "render"]
    state: JobState
    progress: float = Field(ge=0, le=1)
    message: str
    created_at: str
    updated_at: str
    pid: int | None = None
    error: str | None = None
    options: dict = Field(default_factory=dict)


class TimelinePatch(BaseModel):
    expected_revision: int = Field(ge=1)
    timeline: TimelineV1


class RenderRequest(BaseModel):
    mode: Literal["draft", "final"] = "draft"
    preset: Literal["1080p60", "1080p30", "source"] = "1080p60"
    countdown: bool = True


class ProcessRequest(BaseModel):
    quality: Literal["highest", "balanced", "fast"] = "highest"
    alignment_profile: Literal["maximum", "balanced", "fast"] = "maximum"
    motion_profile: Literal["vocal_hybrid", "vocal_only", "linear"] = "vocal_hybrid"
    accept_vietnamese_model_license: bool = False


class InstrumentalSelection(BaseModel):
    candidate_id: str
    confirmed: bool = True


class TimingSuggestionRequest(BaseModel):
    line_id: str
    alignment_profile: Literal["maximum", "balanced", "fast"] = "maximum"
    motion_profile: Literal["vocal_hybrid", "vocal_only", "linear"] = "vocal_hybrid"
    accept_vietnamese_model_license: bool = False


class AlignmentCandidateEvidence(BaseModel):
    model_id: str
    model_revision: str
    stem_id: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)


class GraphemeAlignmentCandidate(BaseModel):
    model_id: str
    model_revision: str
    stem_id: str
    grapheme_index: int = Field(ge=0)
    text: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)


class CriticCorrectionEvidence(BaseModel):
    pass_index: int = Field(ge=1, le=3)
    point_index: int = Field(ge=1)
    before_us: int = Field(ge=0)
    after_us: int = Field(ge=0)
    target_us: int = Field(ge=0)
    support: float = Field(ge=0, le=1)
    source: Literal["ctc_grapheme", "vowel_onset", "vowel_coda"]


class TokenAlignmentEvidence(BaseModel):
    line_id: str
    token_id: str
    text: str
    selected_start_us: int = Field(ge=0)
    selected_end_us: int = Field(gt=0)
    start_spread_us: int = Field(ge=0)
    end_spread_us: int = Field(ge=0)
    acoustic_support: float = Field(ge=0, le=1)
    consensus_count: int = Field(ge=0)
    auto_accepted: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    candidates: list[AlignmentCandidateEvidence] = Field(default_factory=list)
    sweep_spread_us: int = Field(default=0, ge=0)
    beat_support: float = Field(default=0.0, ge=0, le=1)
    selected_sweep: SweepCurveV1 | None = None
    grapheme_candidates: list[GraphemeAlignmentCandidate] = Field(default_factory=list)
    critic_iterations: int = Field(default=0, ge=0, le=3)
    critic_converged: bool = False
    critic_max_delta_us: int = Field(default=0, ge=0)
    critic_onset_support: float = Field(default=0.0, ge=0, le=1)
    critic_sustain_support: float = Field(default=0.0, ge=0, le=1)
    critic_selected_onset_us: int | None = Field(default=None, ge=0)
    critic_selected_sustain_us: int | None = Field(default=None, ge=0)
    critic_corrections: list[CriticCorrectionEvidence] = Field(default_factory=list)


class AlignmentEvidenceV1(BaseModel):
    schema_version: Literal["1.0", "1.1"] = "1.1"
    timeline_revision: int = Field(ge=1)
    alignment_profile: Literal["maximum", "balanced", "fast"] = "maximum"
    motion_profile: Literal["vocal_hybrid", "vocal_only", "linear"] = "vocal_hybrid"
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    vocal_inputs_sha256: dict[str, str] = Field(default_factory=dict)
    models: list[dict[str, str]] = Field(default_factory=list)
    tokens: list[TokenAlignmentEvidence] = Field(default_factory=list)


class TokenTimingSuggestion(BaseModel):
    token_id: str
    text: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    source: TimingSource
    delta_start_us: int
    delta_end_us: int
    consensus: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    candidates: list[AlignmentCandidateEvidence] = Field(default_factory=list)
    sweep: SweepCurveV1 | None = None


class TimingSuggestionResponse(BaseModel):
    line_id: str
    source: TimingSource
    confidence: float = Field(ge=0, le=1)
    used_vocal_stems: list[str]
    license_accepted: bool
    license_required_for_ctc: bool
    alignment_profile: Literal["maximum", "balanced", "fast"] = "maximum"
    motion_profile: Literal["vocal_hybrid", "vocal_only", "linear"] = "vocal_hybrid"
    tokens: list[TokenTimingSuggestion]


class Artifact(BaseModel):
    id: str
    label: str
    kind: str
    url: str
    bytes: int
