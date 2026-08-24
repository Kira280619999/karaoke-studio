import type { BackgroundPlanV1, Project } from './types';

const BACKGROUND_VIDEO_EXTENSIONS = new Set(['mp4', 'mov', 'mkv', 'webm', 'm4v']);

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
