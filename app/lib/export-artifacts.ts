import type { Artifact } from './types';

function isLegacyQuickTimeHfrExport(label: string): boolean {
  const normalized = label.toLowerCase();
  return normalized.endsWith('-1080p120.mp4') && !normalized.includes('-hfr-realtime-v1-');
}

export function videoExportArtifacts(artifacts: Artifact[]): Artifact[] {
  return artifacts.filter(
    (artifact) => artifact.kind === 'export'
      && ['.mp4', '.mov'].some((suffix) => artifact.label.toLowerCase().endsWith(suffix))
      && !isLegacyQuickTimeHfrExport(artifact.label),
  );
}

export function exportDownloadPath(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}download=true`;
}

export function exportDeletePath(projectId: string, filename: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/exports/${encodeURIComponent(filename)}`;
}
