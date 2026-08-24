import type { Artifact, WaveformPayload } from './types';

export type PreviewAudioMode = 'instrumental' | 'original';

export function selectedInstrumentalArtifact(
  candidateId: string | null,
  artifacts: Artifact[],
): Artifact | null {
  if (!candidateId) return null;
  const suffix = `stems/${candidateId}/instrumental.wav`;
  return artifacts.find(
    (artifact) => artifact.kind === 'stem' && artifact.id.endsWith(suffix),
  ) ?? null;
}

export function preferredPreviewAudioMode(
  candidateId: string | null,
  artifacts: Artifact[],
): PreviewAudioMode {
  return selectedInstrumentalArtifact(candidateId, artifacts) ? 'instrumental' : 'original';
}

export function previewWaveformFor(
  mode: PreviewAudioMode,
  candidateId: string | null,
  waveform: WaveformPayload | null,
): number[] {
  if (mode === 'instrumental' && candidateId) {
    return waveform?.candidates[candidateId]?.instrumental ?? waveform?.mix ?? [];
  }
  return waveform?.mix ?? [];
}

export function previewAudioNeedsSync(
  videoTimeSeconds: number,
  audioTimeSeconds: number,
  toleranceSeconds = 0.02,
): boolean {
  if (!Number.isFinite(videoTimeSeconds) || videoTimeSeconds < 0) return false;
  if (!Number.isFinite(audioTimeSeconds) || audioTimeSeconds < 0) return true;
  return Math.abs(videoTimeSeconds - audioTimeSeconds) > toleranceSeconds;
}
