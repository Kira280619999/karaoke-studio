import assert from 'node:assert/strict';
import test from 'node:test';

import { legacyBackgroundPlan } from './backgrounds.ts';
import type { Project } from './types.ts';

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj_test',
    title: 'Bài hát',
    artist: '',
    state: 'NEEDS_REVIEW',
    created_at: '2026-08-24T00:00:00Z',
    updated_at: '2026-08-24T00:00:00Z',
    source_name: 'source.mp4',
    lrc_name: 'lyrics.lrc',
    background_name: 'nền mới.png',
    background_mode: 'custom',
    source_sha256: 'source',
    duration_us: 12_500_000,
    width: 1920,
    height: 1080,
    fps: '60/1',
    has_audio: true,
    selected_instrumental: null,
    instrumental_confirmed: false,
    error: null,
    ...overrides,
  };
}

test('custom projects keep showing their selected image when a legacy API has no plan route', () => {
  const plan = legacyBackgroundPlan(project());

  assert.ok(plan);
  assert.equal(plan.assets[0]?.kind, 'image');
  assert.equal(
    plan.assets[0]?.url,
    '/api/projects/proj_test/files/source/n%E1%BB%81n%20m%E1%BB%9Bi.png',
  );
  assert.deepEqual(plan.segments[0], {
    asset_id: 'bg_legacy_001',
    start_us: 0,
    end_us: 12_500_000,
    transition_us: 0,
    anchor: 'song_start',
  });
});

test('legacy fallback recognizes video backgrounds and ignores original-mode projects', () => {
  const videoPlan = legacyBackgroundPlan(project({ background_name: 'loop.MOV' }));
  assert.equal(videoPlan?.assets[0]?.kind, 'video');
  assert.equal(videoPlan?.assets[0]?.source_duration_us, 12_500_000);

  assert.equal(legacyBackgroundPlan(project({ background_mode: 'original' })), null);
});
