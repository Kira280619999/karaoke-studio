import assert from 'node:assert/strict';
import test from 'node:test';

import {
  exportDeletePath,
  exportDownloadPath,
  videoExportArtifacts,
} from './export-artifacts.ts';

test('only rendered MP4 artifacts appear in the export action list', () => {
  const artifacts = videoExportArtifacts([
    { id: 'exports/final.mp4', label: 'final.mp4', kind: 'export', url: '/final', bytes: 12 },
    { id: 'exports/qa.json', label: 'qa.json', kind: 'export', url: '/qa', bytes: 8 },
    { id: 'work/proxy.mp4', label: 'proxy.mp4', kind: 'proxy', url: '/proxy', bytes: 6 },
  ]);

  assert.deepEqual(artifacts.map((artifact) => artifact.label), ['final.mp4']);
});

test('download and delete paths preserve Vietnamese filenames safely', () => {
  assert.equal(
    exportDownloadPath('/api/projects/proj/files/exports/%C3%A2n.mp4'),
    '/api/projects/proj/files/exports/%C3%A2n.mp4?download=true',
  );
  assert.equal(
    exportDeletePath('proj test', 'ân điển.mp4'),
    '/api/projects/proj%20test/exports/%C3%A2n%20%C4%91i%E1%BB%83n.mp4',
  );
});
