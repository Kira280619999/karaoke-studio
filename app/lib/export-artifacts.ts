import type { Artifact } from './types';

export function videoExportArtifacts(artifacts: Artifact[]): Artifact[] {
  return artifacts.filter(
    (artifact) => artifact.kind === 'export' && artifact.label.toLowerCase().endsWith('.mp4'),
  );
}

export function exportDownloadPath(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}download=true`;
}

export function exportDeletePath(projectId: string, filename: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/exports/${encodeURIComponent(filename)}`;
}
