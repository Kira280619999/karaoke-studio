import type { BackgroundPlanV1, Project } from './types';

const BACKGROUND_VIDEO_EXTENSIONS = new Set(['mp4', 'mov', 'mkv', 'webm', 'm4v']);

export interface BackgroundMotionFrame {
  scale: number;
  xPercent: number;
  yPercent: number;
}

function clampUnit(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export function smoothBackgroundTransition(progress: number): number {
  const clamped = clampUnit(progress);
  return clamped * clamped * (3 - 2 * clamped);
}

export function backgroundSceneProgress(
  plan: BackgroundPlanV1,
  sceneIndex: number,
  nowUs: number,
): number {
  const segment = plan.segments[sceneIndex];
  if (!segment) return 0;
  const outgoingTransitionUs = plan.segments[sceneIndex + 1]?.transition_us ?? 0;
  const clipDurationUs = Math.max(
    1,
    segment.end_us - segment.start_us + outgoingTransitionUs,
  );
  return clampUnit((nowUs - segment.start_us) / clipDurationUs);
}

export function backgroundMotionFrame(
  sceneIndex: number,
  progress: number,
): BackgroundMotionFrame {
  const clamped = clampUnit(progress);
  const patterns = [
    { startScale: 1.035, endScale: 1.085, startX: 0.3, endX: -0.3, startY: -0.15, endY: 0.15 },
    { startScale: 1.085, endScale: 1.045, startX: -0.8, endX: 0.8, startY: 0.2, endY: -0.2 },
    { startScale: 1.045, endScale: 1.085, startX: 0.8, endX: -0.8, startY: -0.25, endY: 0.25 },
    { startScale: 1.09, endScale: 1.04, startX: -0.35, endX: 0.35, startY: -0.75, endY: 0.75 },
  ] as const;
  const pattern = patterns[Math.abs(sceneIndex) % patterns.length] ?? patterns[0];
  return {
    scale: pattern.startScale + (pattern.endScale - pattern.startScale) * clamped,
    xPercent: pattern.startX + (pattern.endX - pattern.startX) * clamped,
    yPercent: pattern.startY + (pattern.endY - pattern.startY) * clamped,
  };
}

export function legacyBackgroundPlan(project: Project): BackgroundPlanV1 | null {
  if (project.background_mode !== 'custom' || !project.background_name) {
    return null;
  }

  const durationUs = Math.max(1, project.duration_us);
  const extension = project.background_name.split('.').pop()?.toLocaleLowerCase() ?? '';
  const kind = BACKGROUND_VIDEO_EXTENSIONS.has(extension) ? 'video' : 'image';

  return {
    schema_version: '1.0',
    strategy: 'lyric_gap_balanced',
    duration_us: durationUs,
    assets: [
      {
        id: 'bg_legacy_001',
        filename: project.background_name,
        kind,
        sha256: 'legacy-project-background',
        width: Math.max(1, project.width),
        height: Math.max(1, project.height),
        source_duration_us: kind === 'video' ? durationUs : null,
        url: `/api/projects/${project.id}/files/source/${encodeURIComponent(project.background_name)}`,
      },
    ],
    segments: [
      {
        asset_id: 'bg_legacy_001',
        start_us: 0,
        end_us: durationUs,
        transition_us: 0,
        anchor: 'song_start',
      },
    ],
  };
}
