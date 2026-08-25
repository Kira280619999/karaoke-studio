import assert from 'node:assert/strict';
import test from 'node:test';

import {
  backgroundMotionFrame,
  backgroundSceneProgress,
  legacyBackgroundPlan,
  smoothBackgroundTransition,
} from './backgrounds.ts';
import type { BackgroundPlanV1, Project } from './types.ts';

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
    selected_instrumental_sha256: null,
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

test('professional dissolve easing is smooth, bounded, and monotonic', () => {
  const samples = [-1, 0, 0.1, 0.25, 0.5, 0.75, 0.9, 1, 2]
    .map(smoothBackgroundTransition);

  assert.equal(samples[0], 0);
  assert.equal(samples[1], 0);
  assert.equal(samples[4], 0.5);
  assert.equal(samples.at(-1), 1);
  assert.deepEqual(samples, samples.toSorted((left, right) => left - right));
});

test('scene motion stays subtle and alternates its cinematic direction', () => {
  const firstStart = backgroundMotionFrame(0, 0);
  const firstEnd = backgroundMotionFrame(0, 1);
  const secondStart = backgroundMotionFrame(1, 0);
  const secondEnd = backgroundMotionFrame(1, 1);

  assert.ok(firstEnd.scale > firstStart.scale);
  assert.ok(secondEnd.scale < secondStart.scale);
  assert.ok(firstStart.xPercent > firstEnd.xPercent);
  assert.ok(secondStart.xPercent < secondEnd.xPercent);
  for (const frame of [firstStart, firstEnd, secondStart, secondEnd]) {
    assert.ok(frame.scale >= 1.03 && frame.scale <= 1.1);
    assert.ok(Math.abs(frame.xPercent) <= 1);
    assert.ok(Math.abs(frame.yPercent) <= 1);
  }
});

test('scene motion continues through its outgoing dissolve without jumping', () => {
  const plan: BackgroundPlanV1 = {
    schema_version: '1.0',
    strategy: 'lyric_gap_balanced',
    duration_us: 20_000_000,
    assets: [],
    segments: [
      { asset_id: 'one', start_us: 0, end_us: 10_000_000, transition_us: 0, anchor: 'song_start' },
      { asset_id: 'two', start_us: 10_000_000, end_us: 20_000_000, transition_us: 1_800_000, anchor: 'lyric_gap' },
    ],
  };

  const atTransitionStart = backgroundSceneProgress(plan, 0, 10_000_000);
  const atTransitionEnd = backgroundSceneProgress(plan, 0, 11_800_000);

  assert.ok(atTransitionStart > 0.8 && atTransitionStart < 1);
  assert.equal(atTransitionEnd, 1);
});
