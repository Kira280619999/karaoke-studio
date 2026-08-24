import assert from 'node:assert/strict';
import test from 'node:test';

import {
  displayTitleFromFilename,
  mapWithConcurrency,
  normalizedFileStem,
  pairBatchFiles,
} from './batch-import.ts';

test('batch import matches video and timeline by normalized Vietnamese filename', () => {
  const videos = [{ name: 'Ân-Điển-Lạ-Lùng.mp4' }, { name: 'Thanh Linh.mov' }];
  const timelines = [{ name: 'thanh_linh.srt' }, { name: 'An Dien La Lung.lrc' }];

  const pairs = pairBatchFiles(videos, timelines);

  assert.equal(pairs[0].timeline?.name, 'An Dien La Lung.lrc');
  assert.equal(pairs[1].timeline?.name, 'thanh_linh.srt');
  assert.equal(pairs[0].suggestedTitle, 'Ân Điển Lạ Lùng');
  assert.equal(normalizedFileStem('Ân-Điển-Lạ-Lùng.mp4'), 'andienlalung');
});

test('batch import pairs remaining files by selection order only when counts agree', () => {
  const complete = pairBatchFiles(
    [{ name: 'Bài một.mp4' }, { name: 'Bài hai.mp4' }],
    [{ name: 'timeline-a.lrc' }, { name: 'timeline-b.lrc' }],
  );
  assert.deepEqual(complete.map((pair) => pair.timeline?.name), [
    'timeline-a.lrc',
    'timeline-b.lrc',
  ]);

  const incomplete = pairBatchFiles(
    [{ name: 'Bài một.mp4' }, { name: 'Bài hai.mp4' }],
    [{ name: 'timeline-a.lrc' }],
  );
  assert.deepEqual(incomplete.map((pair) => pair.timeline), [null, null]);
  assert.equal(displayTitleFromFilename('Bai__Hat---Moi.webm'), 'Bai Hat Moi');
});

test('batch runner never exceeds the requested concurrency and preserves result order', async () => {
  let active = 0;
  let peak = 0;
  const results = await mapWithConcurrency([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 2));
    active -= 1;
    if (value === 4) throw new Error('fixture failure');
    return value * 10;
  });

  assert.equal(peak, 2);
  assert.deepEqual(results.map((result) => result.status), [
    'fulfilled',
    'fulfilled',
    'fulfilled',
    'rejected',
    'fulfilled',
  ]);
  assert.equal(results[2].status === 'fulfilled' ? results[2].value : null, 30);
});
