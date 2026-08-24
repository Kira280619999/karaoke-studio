export type ProjectState =
  | 'IMPORTED'
  | 'SEPARATED'
  | 'ALIGNED'
  | 'NEEDS_REVIEW'
  | 'VERIFIED'
  | 'RENDERED'
  | 'FAILED';

export interface Project {
  id: string;
  title: string;
  artist: string;
  state: ProjectState;
  created_at: string;
  updated_at: string;
  source_name: string;
  lrc_name: string;
  background_name: string | null;
  background_mode: 'original' | 'custom';
  source_sha256: string;
  duration_us: number;
  width: number;
  height: number;
  fps: string;
  has_audio: boolean;
  selected_instrumental: string | null;
  instrumental_confirmed: boolean;
  error: string | null;
}

export interface TokenTiming {
  id: string;
  text: string;
  normalized: string;
  start_us: number;
  end_us: number;
  confidence: number;
  source: string;
  verified: boolean;
  locked: boolean;
  sweep?: SweepCurveV1 | null;
}

export interface SweepPointV1 {
  time_us: number;
  line_progress_ppm: number;
}

export interface SweepCurveV1 {
  schema_version: '1.0';
  source: 'ensemble_ctc' | 'energy_linear' | 'manual_rescaled' | 'lrc_linear';
  confidence: number;
  verified: boolean;
  points: SweepPointV1[];
}

export interface LineTiming {
  id: string;
  text: string;
  start_us: number;
  end_us: number;
  confidence: number;
  source: string;
  verified: boolean;
  locked: boolean;
  tokens: TokenTiming[];
}

export interface Timeline {
  schema_version: '1.0' | '1.1';
  revision: number;
  language: string;
  duration_us: number;
  fps_numerator: number;
  fps_denominator: number;
  metadata: Record<string, string>;
  lines: LineTiming[];
}

export interface TimelineIssue {
  code: string;
  message: string;
  line_id: string | null;
  token_id: string | null;
  severity: 'warning' | 'error';
}

export interface TokenTimingSuggestion {
  token_id: string;
  text: string;
  start_us: number;
  end_us: number;
  confidence: number;
  source: string;
  delta_start_us: number;
  delta_end_us: number;
  consensus: boolean;
  reason_codes: string[];
  candidates: AlignmentCandidateEvidence[];
  sweep?: SweepCurveV1 | null;
}

export interface AlignmentCandidateEvidence {
  model_id: string;
  model_revision: string;
  stem_id: string;
  start_us: number;
  end_us: number;
  confidence: number;
}

export interface TimingSuggestion {
  line_id: string;
  source: string;
  confidence: number;
  used_vocal_stems: string[];
  license_accepted: boolean;
  license_required_for_ctc: boolean;
  alignment_profile: 'maximum' | 'balanced' | 'fast';
  motion_profile: 'vocal_hybrid' | 'vocal_only' | 'linear';
  tokens: TokenTimingSuggestion[];
}

export interface Artifact {
  id: string;
  label: string;
  kind: string;
  url: string;
  bytes: number;
}

export interface Job {
  id: string;
  project_id: string;
  kind: 'process' | 'render';
  state: 'PENDING' | 'RUNNING' | 'COMPLETE' | 'FAILED' | 'CANCELLED';
  progress: number;
  message: string;
  error: string | null;
}

export interface CandidateWaveform {
  label: string;
  production_grade: boolean;
  warning: string | null;
  instrumental: number[];
  vocals: number[];
}

export interface WaveformPayload {
  mix: number[];
  candidates: Record<string, CandidateWaveform>;
}

export interface Capabilities {
  ffmpeg: boolean;
  ffprobe: boolean;
  audio_separator: boolean;
  demucs: boolean;
  vietnamese_ctc: boolean;
  data_dir: string;
  vietnamese_model: { id: string; license: string; bundled: boolean };
  vietnamese_lyric_model: {
    id: string;
    revision: string;
    license: string;
    bundled: boolean;
    singing_specific: boolean;
  };
  karaoke_fonts: { id: string; label: string }[];
}
