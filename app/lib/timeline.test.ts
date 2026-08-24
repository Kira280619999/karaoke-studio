import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activeLineAt,
  applyTimingSuggestion,
  cinematicLineProgressPpm,
  deleteTimelineLine,
  deleteTimelineToken,
  editTimelineTokenText,
  evaluateDisplaySweepPpm,
  evaluateSweepPpm,
  highlightPercent,
  insertTimelineLine,
  insertTimelineToken,
  karaokeCountdownCue,
  karaokeDisplayRows,
  karaokeVisibleLineIndexes,
  KARAOKE_PREVIEW_FPS,
  KARAOKE_PREVIEW_HEIGHT,
  KARAOKE_PREVIEW_WIDTH,
  lyricFitScale,
  manualLineReplayRange,
  manualTransitionReplayRange,
  manualTokenLoopRange,
  moveLineBy,
  moveTokenBy,
  nearestMarkerWithin,
  pasteTimelineLineAt,
  rescaleSweep,
  previewFrameTimeUs,
  setBoundary,
  setPreviousLineEnd,
  setTransitionGap,
  setTransitionStart,
  smoothedPlaybackTimeUs,
  stepPreviewFrameTimeUs,
  timeUsToPixels,
  tokenDisplaySegments,
  trimTokenEdge,
  verifyTiming,
} from './timeline.ts';
import type { LineTiming, Timeline, TimingSuggestion } from './types.ts';

const line: LineTiming = {
  id: 'line-1',
  text: 'Xin chào',
  start_us: 1_000_000,
  end_us: 3_000_000,
  confidence: 0.8,
  source: 'energy_aware',
  verified: false,
  locked: false,
  tokens: [
    { id: 't1', text: 'Xin', normalized: 'xin', start_us: 1_000_000, end_us: 1_800_000, confidence: 0.8, source: 'energy_aware', verified: false, locked: false },
    { id: 't2', text: ' chào', normalized: 'chào', start_us: 1_800_000, end_us: 3_000_000, confidence: 0.8, source: 'energy_aware', verified: false, locked: false },
  ],
};

const timeline: Timeline = {
  schema_version: '1.0', revision: 1, language: 'vi', duration_us: 4_000_000,
  fps_numerator: 60, fps_denominator: 1, metadata: {}, lines: [line],
};

test('active line uses a half-open interval', () => {
  assert.equal(activeLineAt(timeline, 999_999), null);
  assert.equal(activeLineAt(timeline, 1_000_000), 0);
  assert.equal(activeLineAt(timeline, 3_000_000), null);
});

test('Karaoke countdown exposes 3-2-1 only in the final three seconds of a real gap', () => {
  const countdownLine = structuredClone(line);
  countdownLine.start_us += 4_000_000;
  countdownLine.end_us += 4_000_000;
  countdownLine.tokens.forEach((token) => {
    token.start_us += 4_000_000;
    token.end_us += 4_000_000;
  });
  const countdownTimeline: Timeline = {
    ...structuredClone(timeline),
    duration_us: 8_000_000,
    lines: [countdownLine],
  };

  assert.equal(karaokeCountdownCue(countdownTimeline, 1_900_000), null);
  assert.deepEqual(karaokeCountdownCue(countdownTimeline, 2_100_000), {
    lane: 1,
    nextLineId: 'line-1',
    number: 3,
  });
  assert.equal(karaokeCountdownCue(countdownTimeline, 3_100_000)?.number, 2);
  assert.equal(karaokeCountdownCue(countdownTimeline, 4_100_000)?.number, 1);
  assert.equal(karaokeCountdownCue(countdownTimeline, 5_000_000), null);
  assert.equal(karaokeCountdownCue(timeline, 500_000), null);
});

test('next lyric stays hidden through a long instrumental gap until its lead window', () => {
  const first = structuredClone(line);
  first.end_us = 3_000_000;
  first.tokens.at(-1)!.end_us = 3_000_000;
  const second = structuredClone(line);
  second.id = 'line-2';
  second.start_us = 10_000_000;
  second.end_us = 12_000_000;
  second.tokens.forEach((token) => {
    token.id = `line-2-${token.id}`;
    token.start_us += 9_000_000;
    token.end_us += 9_000_000;
  });
  const gapTimeline = {
    ...structuredClone(timeline),
    duration_us: 12_000_000,
    lines: [first, second],
  };

  assert.deepEqual(karaokeVisibleLineIndexes(gapTimeline, 2_000_000), [0]);
  assert.deepEqual(karaokeVisibleLineIndexes(gapTimeline, 5_000_000), []);
  assert.deepEqual(karaokeVisibleLineIndexes(gapTimeline, 5_500_000), []);
  assert.deepEqual(karaokeVisibleLineIndexes(gapTimeline, 8_500_000), [1]);
  assert.deepEqual(karaokeVisibleLineIndexes(gapTimeline, 10_500_000), [1]);
});

test('ordinary short transitions keep the next lyric ready while the current line sings', () => {
  const first = structuredClone(line);
  first.end_us = 3_000_000;
  const second = structuredClone(line);
  second.id = 'line-2';
  second.start_us = 3_500_000;
  second.end_us = 5_500_000;
  const shortTransition = {
    ...structuredClone(timeline),
    duration_us: 5_500_000,
    lines: [first, second],
  };

  assert.deepEqual(karaokeVisibleLineIndexes(shortTransition, 2_000_000), [0, 1]);
});

test('whole-song roll maps integer microseconds to monotonic pixels', () => {
  const points = [0, 500_000, 1_000_000, 3_000_000].map((timeUs) =>
    timeUsToPixels(timeUs, 72),
  );
  assert.deepEqual(points, [0, 36, 72, 216]);
  assert.ok(points.every((point, index) => index === 0 || point >= points[index - 1]));
});

test('each Karaoke display row keeps a safe horizontal fit fallback', () => {
  assert.equal(lyricFitScale(1_200, 900), 1);
  assert.equal(lyricFitScale(1_200, 2_400), 0.5);
  assert.ok(lyricFitScale(1_200, 4_800) > 0);
  assert.ok(lyricFitScale(1_200, 4_800) <= 1);
});

test('long Karaoke lyric wraps at a word boundary and preserves sweep order', () => {
  const text = 'Bão tố trong đời hồn chúng con luôn nương trong Ngài thôi';
  const words = text.split(' ');
  let cursorUs = 1_000_000;
  const longLine: LineTiming = {
    ...structuredClone(line),
    text,
    end_us: cursorUs + words.length * 500_000,
    tokens: words.map((word, index) => {
      const startUs = cursorUs;
      cursorUs += 500_000;
      return {
        ...structuredClone(line.tokens[0]),
        id: `long-${index}`,
        text: word,
        normalized: word.toLocaleLowerCase('vi'),
        start_us: startUs,
        end_us: cursorUs,
      };
    }),
  };

  const rows = karaokeDisplayRows(longLine);

  assert.equal(rows.length, 2);
  assert.equal(rows.map((row) => row.text).join(' '), text);
  assert.equal(rows[0].startProgressPpm, 0);
  assert.equal(rows[0].endProgressPpm, rows[1].startProgressPpm);
  assert.equal(rows[1].endProgressPpm, 1_000_000);
  assert.ok(rows.every((row) => Array.from(row.text).length < 48));
});

test('manual marker snap picks only the closest marker inside its threshold', () => {
  const markers = [1_000_000, 2_000_000, 3_000_000];

  assert.equal(nearestMarkerWithin(markers, 1_920_000, 100_000), 2_000_000);
  assert.equal(nearestMarkerWithin(markers, 1_500_000, 100_000), null);
  assert.equal(nearestMarkerWithin(markers, 2_950_000, -1), null);
});

test('whole-song roll moves a complete line without changing its internal rhythm', () => {
  const movable: Timeline = {
    ...structuredClone(timeline),
    duration_us: 8_000_000,
    lines: [
      structuredClone(line),
      {
        ...structuredClone(line),
        id: 'line-2',
        start_us: 5_000_000,
        end_us: 7_000_000,
        tokens: structuredClone(line.tokens).map((token) => ({
          ...token,
          id: `${token.id}-line-2`,
          start_us: token.start_us + 4_000_000,
          end_us: token.end_us + 4_000_000,
        })),
      },
    ],
  };

  const moved = moveLineBy(movable, 'line-1', 500_000);

  assert.equal(moved.lines[0].start_us, 1_500_000);
  assert.equal(moved.lines[0].end_us, 3_500_000);
  assert.equal(moved.lines[0].tokens[0].start_us, 1_500_000);
  assert.equal(moved.lines[0].tokens[1].end_us, 3_500_000);
  assert.equal(moved.lines[0].tokens[0].end_us - moved.lines[0].tokens[0].start_us, 800_000);
  assert.equal(moved.lines[0].source, 'manual');
  assert.equal(movable.lines[0].start_us, 1_000_000);
  assert.equal(moved.lines[1], movable.lines[1]);
});

test('whole-song roll moves a word by borrowing time from adjacent words', () => {
  const threeTokens: Timeline = {
    ...structuredClone(timeline),
    duration_us: 5_000_000,
    lines: [
      {
        ...structuredClone(line),
        end_us: 4_000_000,
        text: 'Một hai ba',
        tokens: [
          { ...structuredClone(line.tokens[0]), id: 'one', start_us: 1_000_000, end_us: 2_000_000 },
          { ...structuredClone(line.tokens[0]), id: 'two', start_us: 2_000_000, end_us: 3_000_000 },
          { ...structuredClone(line.tokens[0]), id: 'three', start_us: 3_000_000, end_us: 4_000_000 },
        ],
      },
      {
        ...structuredClone(line),
        id: 'line-untouched',
        text: 'Câu không đổi',
        start_us: 4_500_000,
        end_us: 4_900_000,
        tokens: [{
          ...structuredClone(line.tokens[0]),
          id: 'untouched',
          text: 'Câu không đổi',
          start_us: 4_500_000,
          end_us: 4_900_000,
        }],
      },
    ],
  };

  const moved = moveTokenBy(threeTokens, 'line-1', 'two', 200_000);

  assert.equal(moved.lines[0].tokens[0].end_us, 2_200_000);
  assert.equal(moved.lines[0].tokens[1].start_us, 2_200_000);
  assert.equal(moved.lines[0].tokens[1].end_us, 3_200_000);
  assert.equal(moved.lines[0].tokens[2].start_us, 3_200_000);
  assert.ok(moved.lines[0].tokens.every((token) => token.start_us < token.end_us));
  assert.equal(moved.lines[0].tokens[1].source, 'manual');
  assert.equal(moved.lines[1], threeTokens.lines[1]);
});

test('trim handle shortens the final word and moves the line end with its sweep', () => {
  const editable = structuredClone(timeline);
  editable.lines[0].verified = true;
  editable.lines[0].tokens[1].verified = true;
  editable.lines[0].tokens[1].sweep = {
    schema_version: '1.0',
    source: 'ensemble_ctc',
    confidence: 0.95,
    verified: true,
    points: [
      { time_us: 1_800_000, line_progress_ppm: 400_000 },
      { time_us: 2_400_000, line_progress_ppm: 760_000 },
      { time_us: 3_000_000, line_progress_ppm: 1_000_000 },
    ],
  };

  const changed = trimTokenEdge(editable, 'line-1', 't2', 'end', 2_300_000);
  const finalToken = changed.lines[0].tokens[1];

  assert.equal(finalToken.end_us, 2_300_000);
  assert.equal(changed.lines[0].end_us, 2_300_000);
  assert.equal(changed.lines[0].tokens[0].end_us, 1_800_000);
  assert.equal(finalToken.sweep?.points.at(-1)?.time_us, 2_300_000);
  assert.equal(finalToken.sweep?.verified, false);
  assert.equal(finalToken.source, 'manual');
  assert.equal(changed.lines[0].verified, false);
  assert.equal(editable.lines[0].end_us, 3_000_000);
});

test('final trim is clamped to one frame and cannot extend across the next line', () => {
  const editable = structuredClone(timeline);
  editable.duration_us = 6_000_000;
  editable.lines.push({
    ...structuredClone(line),
    id: 'line-2',
    text: 'Câu sau',
    start_us: 3_200_000,
    end_us: 5_000_000,
    tokens: [{
      ...structuredClone(line.tokens[0]),
      id: 'next',
      text: 'Câu sau',
      start_us: 3_200_000,
      end_us: 5_000_000,
    }],
  });

  const minimum = trimTokenEdge(editable, 'line-1', 't2', 'end', 1_800_000);
  const capped = trimTokenEdge(editable, 'line-1', 't2', 'end', 5_500_000);

  assert.equal(minimum.lines[0].tokens[1].end_us, 1_816_667);
  assert.equal(capped.lines[0].tokens[1].end_us, 3_200_000);
  assert.equal(capped.lines[0].end_us, 3_200_000);
  assert.equal(capped.lines[1].start_us, 3_200_000);
});

test('shared trim boundary resizes both words and respects an adjacent lock', () => {
  const changed = trimTokenEdge(timeline, 'line-1', 't2', 'start', 2_100_000);
  assert.equal(changed.lines[0].tokens[0].end_us, 2_100_000);
  assert.equal(changed.lines[0].tokens[1].start_us, 2_100_000);

  const locked = structuredClone(timeline);
  locked.lines[0].tokens[0].locked = true;
  assert.equal(trimTokenEdge(locked, 'line-1', 't2', 'start', 2_100_000), locked);
  assert.equal(trimTokenEdge(timeline, 'line-1', 't2', 'end', 3_000_000), timeline);
});

test('manual lyric editor changes a word and rebuilds continuous sweep progress', () => {
  const changed = editTimelineTokenText(timeline, 'line-1', 't2', 'Chúa');
  const changedLine = changed.lines[0];

  assert.equal(changedLine.text, 'Xin Chúa');
  assert.equal(changedLine.tokens[1].normalized, 'chúa');
  assert.equal(changedLine.tokens[0].sweep?.points[0].line_progress_ppm, 0);
  assert.equal(
    changedLine.tokens[0].sweep?.points.at(-1)?.line_progress_ppm,
    changedLine.tokens[1].sweep?.points[0].line_progress_ppm,
  );
  assert.equal(changedLine.tokens.at(-1)?.sweep?.points.at(-1)?.line_progress_ppm, 1_000_000);
  assert.equal(changedLine.verified, false);
  assert.equal(timeline.lines[0].text, 'Xin chào');
});

test('manual lyric editor inserts and removes a word without breaking token timing', () => {
  const inserted = insertTimelineToken(timeline, 'line-1', 't1', 'kính', 'after');
  const insertedLine = inserted.lines[0];
  const added = insertedLine.tokens[1];

  assert.equal(insertedLine.text, 'Xin kính chào');
  assert.equal(insertedLine.tokens.length, 3);
  assert.ok(insertedLine.tokens.every((token) => token.end_us > token.start_us));
  assert.ok(insertedLine.tokens.every((token, index) => (
    index === 0 || token.start_us >= insertedLine.tokens[index - 1].end_us
  )));
  assert.equal(added.source, 'manual');

  const removed = deleteTimelineToken(inserted, 'line-1', added.id);
  assert.equal(removed.lines[0].text, 'Xin chào');
  assert.equal(removed.lines[0].tokens.length, 2);
  assert.equal(removed.lines[0].tokens[0].start_us, 1_000_000);
  assert.equal(removed.lines[0].tokens.at(-1)?.end_us, 3_000_000);
});

test('manual lyric editor adds a review-required line at the preferred playhead', () => {
  const extended: Timeline = { ...structuredClone(timeline), duration_us: 8_000_000 };
  const changed = insertTimelineLine(
    extended,
    'line-1',
    'Ngài thật tuyệt vời',
    4_000_000,
  );
  const added = changed.lines[1];

  assert.equal(added.text, 'Ngài thật tuyệt vời');
  assert.equal(added.start_us, 4_000_000);
  assert.equal(added.tokens.length, 4);
  assert.ok(added.tokens.every((token) => token.source === 'manual' && !token.verified));
  assert.ok(added.tokens.every((token) => token.end_us - token.start_us >= 16_667));
  assert.equal(added.tokens.at(-1)?.sweep?.points.at(-1)?.line_progress_ppm, 1_000_000);
});

test('manual lyric editor deletes a line but never leaves an empty song', () => {
  const extended = insertTimelineLine(
    { ...structuredClone(timeline), duration_us: 8_000_000 },
    'line-1',
    'Câu mới',
    4_000_000,
  );
  const removed = deleteTimelineLine(extended, 'line-1');

  assert.equal(removed.lines.length, 1);
  assert.equal(removed.lines[0].text, 'Câu mới');
  assert.equal(deleteTimelineLine(timeline, 'line-1'), timeline);
});

test('copy and paste keeps every word and sweep offset on the exact same rhythm', () => {
  const source = structuredClone(line);
  source.tokens[0].sweep = {
    schema_version: '1.0',
    source: 'ensemble_ctc',
    confidence: 0.96,
    verified: true,
    points: [
      { time_us: 1_000_000, line_progress_ppm: 0 },
      { time_us: 1_350_000, line_progress_ppm: 220_000 },
      { time_us: 1_800_000, line_progress_ppm: 400_000 },
    ],
  };
  source.tokens[1].sweep = {
    schema_version: '1.0',
    source: 'ensemble_ctc',
    confidence: 0.94,
    verified: true,
    points: [
      { time_us: 1_800_000, line_progress_ppm: 400_000 },
      { time_us: 2_250_000, line_progress_ppm: 710_000 },
      { time_us: 3_000_000, line_progress_ppm: 1_000_000 },
    ],
  };
  const extended: Timeline = {
    ...structuredClone(timeline),
    duration_us: 10_000_000,
    lines: [source],
  };

  const changed = pasteTimelineLineAt(extended, source, 6_012_000);
  const pasted = changed.lines.find((candidate) => candidate.id !== source.id)!;
  const deltaUs = pasted.start_us - source.start_us;
  const frameUs = Math.round(1_000_000 / extended.fps_numerator);

  assert.equal(pasted.start_us % frameUs, 0);
  assert.equal(pasted.text, source.text);
  assert.equal(pasted.end_us - pasted.start_us, source.end_us - source.start_us);
  assert.deepEqual(
    pasted.tokens.map((token) => [
      token.text,
      token.start_us - pasted.start_us,
      token.end_us - pasted.start_us,
    ]),
    source.tokens.map((token) => [
      token.text,
      token.start_us - source.start_us,
      token.end_us - source.start_us,
    ]),
  );
  assert.deepEqual(
    pasted.tokens.map((token) => token.sweep?.points.map((point) => [
      point.time_us - pasted.start_us,
      point.line_progress_ppm,
    ])),
    source.tokens.map((token) => token.sweep?.points.map((point) => [
      point.time_us - source.start_us,
      point.line_progress_ppm,
    ])),
  );
  assert.ok(pasted.tokens.every((token) => token.source === 'manual' && !token.verified));
  assert.ok(pasted.tokens.every((token, index) => token.id !== source.tokens[index].id));
  assert.equal(pasted.tokens[0].start_us, source.tokens[0].start_us + deltaUs);
  assert.equal(source.tokens[0].sweep?.source, 'ensemble_ctc');
});

test('a pasted rhythm can be deleted without changing the original line', () => {
  const extended = { ...structuredClone(timeline), duration_us: 8_000_000 };
  const pastedTimeline = pasteTimelineLineAt(extended, extended.lines[0], 5_000_000);
  const pasted = pastedTimeline.lines.find((candidate) => candidate.id !== line.id)!;
  const removed = deleteTimelineLine(pastedTimeline, pasted.id);

  assert.equal(pastedTimeline.lines.length, 2);
  assert.deepEqual(removed.lines, extended.lines);
});

test('highlight moves monotonically across token text including its leading space', () => {
  const samples = [1_000_000, 1_400_000, 1_800_000, 2_400_000, 3_000_000].map((now) => highlightPercent(line, now));
  assert.equal(samples[0], 0);
  assert.equal(samples.at(-1), 100);
  assert.ok(samples.every((value, index) => index === 0 || value >= samples[index - 1]));
});

test('highlight sweeps original repeated whitespace instead of skipping it', () => {
  const spaced = structuredClone(line);
  spaced.text = 'Xin   chào';
  spaced.tokens[1].text = 'chào';
  const beforeSecond = highlightPercent(spaced, 1_800_000);
  const insideSecond = highlightPercent(spaced, 2_100_000);
  assert.ok(insideSecond > beforeSecond);
  assert.equal(highlightPercent(spaced, 3_000_000), 100);
});

test('manual boundary change is clamped and invalidates verification', () => {
  const changed = setBoundary(timeline, 'line-1', 1, 2_100_000);
  assert.equal(changed.lines[0].tokens[0].end_us, 2_100_000);
  assert.equal(changed.lines[0].tokens[1].start_us, 2_100_000);
  assert.equal(changed.lines[0].tokens[0].source, 'manual');
  assert.equal(changed.lines[0].verified, false);
  assert.equal(timeline.lines[0].tokens[0].end_us, 1_800_000);
});

test('cross-line start handle cannot overlap the previous lyric', () => {
  const previous: LineTiming = {
    ...structuredClone(line),
    id: 'line-previous',
    text: 'Câu trước',
    start_us: 0,
    end_us: 900_000,
    tokens: [
      { id: 'p1', text: 'Câu trước', normalized: 'cau truoc', start_us: 0, end_us: 900_000, confidence: 0.9, source: 'vietnamese_ctc', verified: true, locked: false },
    ],
  };
  const editable: Timeline = {
    ...structuredClone(timeline),
    lines: [previous, structuredClone(line)],
  };

  const changed = setTransitionStart(editable, 'line-1', 700_000);

  assert.equal(changed.lines[1].start_us, 900_000);
  assert.equal(changed.lines[1].tokens[0].start_us, 900_000);
  assert.equal(changed.lines[1].tokens[0].source, 'manual');
  assert.equal(changed.lines[0].end_us, 900_000);
});

test('large cross-line overlap is removed without moving the following boundary', () => {
  const previous: LineTiming = {
    ...structuredClone(line),
    id: 'line-previous',
    text: 'Câu trước',
    start_us: 0,
    end_us: 2_200_000,
    tokens: [
      { id: 'p1', text: 'Câu trước', normalized: 'cau truoc', start_us: 0, end_us: 2_200_000, confidence: 0.9, source: 'vietnamese_ctc', verified: true, locked: false },
    ],
  };
  const current: LineTiming = {
    ...structuredClone(line),
    start_us: 1_000_000,
    end_us: 4_000_000,
    tokens: [
      { ...structuredClone(line.tokens[0]), start_us: 1_000_000, end_us: 1_800_000 },
      { ...structuredClone(line.tokens[1]), start_us: 1_800_000, end_us: 4_000_000 },
    ],
  };
  const editable: Timeline = {
    ...structuredClone(timeline),
    duration_us: 6_000_000,
    lines: [previous, current],
  };

  const changed = setTransitionGap(editable, 'line-1', 250_000);

  assert.equal(changed.lines[1].start_us, 2_450_000);
  assert.equal(changed.lines[1].end_us, 4_000_000);
  assert.equal(changed.lines[1].tokens[0].start_us, 2_450_000);
  assert.equal(changed.lines[1].tokens.at(-1)?.end_us, 4_000_000);
  assert.ok(changed.lines[1].tokens.every((token) => token.start_us < token.end_us));
  assert.ok(changed.lines[1].tokens.every((token) => token.source === 'manual'));
});

test('previous-line end can be placed at the manual playhead', () => {
  const previous: LineTiming = {
    ...structuredClone(line),
    id: 'line-previous',
    text: 'Câu trước',
    start_us: 0,
    end_us: 900_000,
    tokens: [
      { id: 'p1', text: 'Câu trước', normalized: 'cau truoc', start_us: 0, end_us: 900_000, confidence: 0.9, source: 'vietnamese_ctc', verified: true, locked: false },
    ],
  };
  const editable: Timeline = {
    ...structuredClone(timeline),
    lines: [previous, structuredClone(line)],
  };

  const changed = setPreviousLineEnd(editable, 'line-1', 750_000);

  assert.equal(changed.lines[0].end_us, 750_000);
  assert.equal(changed.lines[0].tokens[0].end_us, 750_000);
  assert.equal(changed.lines[0].source, 'manual');
});

test('first word loops from the tail of the previous lyric without AI', () => {
  const previous: LineTiming = {
    ...structuredClone(line),
    id: 'line-previous',
    text: 'Câu trước',
    start_us: 0,
    end_us: 900_000,
    tokens: [
      { id: 'p1', text: 'Câu trước', normalized: 'cau truoc', start_us: 300_000, end_us: 900_000, confidence: 0.9, source: 'vietnamese_ctc', verified: true, locked: false },
    ],
  };
  const editable: Timeline = {
    ...structuredClone(timeline),
    lines: [previous, structuredClone(line)],
  };

  const range = manualTokenLoopRange(editable, 'line-1', 't1');

  assert.deepEqual(range, {
    startUs: 50_000,
    endUs: 2_300_000,
    tokenId: 't1',
    kind: 'transition',
    repeat: true,
  });
});

test('whole-line replay plays once from the exact line boundaries', () => {
  assert.deepEqual(manualLineReplayRange(timeline, 'line-1'), {
    startUs: 1_000_000,
    endUs: 3_000_000,
    tokenId: 't1',
    kind: 'line',
    repeat: false,
  });
});

test('transition replay plays both complete consecutive lines once', () => {
  const previous: LineTiming = {
    ...structuredClone(line),
    id: 'line-previous',
    text: 'Câu trước',
    start_us: 100_000,
    end_us: 900_000,
    tokens: [
      { id: 'p1', text: 'Câu trước', normalized: 'cau truoc', start_us: 100_000, end_us: 900_000, confidence: 0.9, source: 'vietnamese_ctc', verified: true, locked: false },
    ],
  };
  const editable: Timeline = {
    ...structuredClone(timeline),
    lines: [previous, structuredClone(line)],
  };

  assert.deepEqual(manualTransitionReplayRange(editable, 'line-previous', 'line-1'), {
    startUs: 100_000,
    endUs: 3_000_000,
    tokenId: 't1',
    kind: 'transition',
    repeat: false,
  });
  assert.equal(manualTransitionReplayRange(editable, 'line-1', 'line-previous'), null);
});

test('ordinary word audition keeps a lead-in so the first phoneme is not cut', () => {
  assert.deepEqual(manualTokenLoopRange(timeline, 'line-1', 't2'), {
    startUs: 1_150_000,
    endUs: 3_500_000,
    tokenId: 't2',
    kind: 'token',
    repeat: true,
  });
});

test('AI suggestion can be applied to one shared token boundary without mutating source', () => {
  const suggestion: TimingSuggestion = {
    line_id: 'line-1',
    source: 'vietnamese_ctc',
    confidence: 0.94,
    used_vocal_stems: ['mel', 'demucs'],
    license_accepted: true,
    license_required_for_ctc: false,
    alignment_profile: 'maximum',
    motion_profile: 'vocal_hybrid',
    tokens: [
      { token_id: 't1', text: 'Xin', start_us: 1_100_000, end_us: 1_700_000, confidence: 0.95, source: 'vietnamese_ctc', delta_start_us: 100_000, delta_end_us: -100_000, consensus: true, reason_codes: [], candidates: [] },
      { token_id: 't2', text: 'chào', start_us: 1_700_000, end_us: 2_800_000, confidence: 0.93, source: 'vietnamese_ctc', delta_start_us: -100_000, delta_end_us: -200_000, consensus: true, reason_codes: [], candidates: [] },
    ],
  };
  const changed = applyTimingSuggestion(timeline, 'line-1', suggestion, 't2');
  assert.equal(changed.lines[0].tokens[0].end_us, 1_700_000);
  assert.equal(changed.lines[0].tokens[1].start_us, 1_700_000);
  assert.equal(changed.lines[0].tokens[1].end_us, 2_800_000);
  assert.equal(changed.lines[0].source, 'vietnamese_ctc');
  assert.equal(timeline.lines[0].tokens[0].end_us, 1_800_000);
});

test('non-linear sweep follows acoustic control points with integer interpolation', () => {
  const curve = {
    schema_version: '1.0' as const,
    source: 'ensemble_ctc' as const,
    confidence: 0.96,
    verified: true,
    points: [
      { time_us: 1_000_000, line_progress_ppm: 0 },
      { time_us: 1_150_000, line_progress_ppm: 260_000 },
      { time_us: 2_850_000, line_progress_ppm: 880_000 },
      { time_us: 3_000_000, line_progress_ppm: 1_000_000 },
    ],
  };
  const samples = [1_000_000, 1_075_000, 1_150_000, 2_000_000, 2_850_000, 3_000_000]
    .map((timeUs) => evaluateSweepPpm(curve, timeUs));
  assert.deepEqual(samples, [0, 130_000, 260_000, 570_000, 880_000, 1_000_000]);
  assert.deepEqual(samples, [...samples].sort((a, b) => a - b));
});

test('display sweep damps internal velocity changes but preserves exact endpoints', () => {
  const curve = {
    schema_version: '1.0' as const,
    source: 'ensemble_ctc' as const,
    confidence: 0.96,
    verified: true,
    points: [
      { time_us: 1_000_000, line_progress_ppm: 0 },
      { time_us: 1_100_000, line_progress_ppm: 400_000 },
      { time_us: 2_900_000, line_progress_ppm: 600_000 },
      { time_us: 3_000_000, line_progress_ppm: 1_000_000 },
    ],
  };
  const display = [1_000_000, 1_100_000, 2_000_000, 2_900_000, 3_000_000]
    .map((timeUs) => evaluateDisplaySweepPpm(curve, timeUs));
  const acoustic = [1_000_000, 1_100_000, 2_000_000, 2_900_000, 3_000_000]
    .map((timeUs) => evaluateSweepPpm(curve, timeUs));

  assert.equal(display[0], acoustic[0]);
  assert.equal(display.at(-1), acoustic.at(-1));
  assert.ok(display[1] < acoustic[1]);
  assert.ok(display[3] > acoustic[3]);
  assert.deepEqual(display, [...display].sort((a, b) => a - b));
});

test('cinematic line path stays continuous through analyzed word boundaries', () => {
  const analyzed = structuredClone(line);
  const boundaries = [1_000_000, 1_600_000, 3_000_000];
  const progress = [0, 280_000, 1_000_000];
  analyzed.tokens.forEach((token, index) => {
    token.start_us = boundaries[index];
    token.end_us = boundaries[index + 1];
    token.sweep = {
      schema_version: '1.0',
      source: 'ensemble_ctc',
      confidence: 0.96,
      verified: true,
      points: [
        { time_us: boundaries[index], line_progress_ppm: progress[index] },
        {
          time_us: boundaries[index] + 20_000,
          line_progress_ppm: progress[index]
            + Math.round((progress[index + 1] - progress[index]) * 0.75),
        },
        { time_us: boundaries[index + 1], line_progress_ppm: progress[index + 1] },
      ],
    };
  });

  assert.deepEqual(
    boundaries.map((timeUs) => cinematicLineProgressPpm(analyzed, timeUs)),
    progress,
  );
  const samples = Array.from(
    { length: Math.floor((analyzed.end_us - analyzed.start_us) / 8_333) + 1 },
    (_, index) => cinematicLineProgressPpm(analyzed, analyzed.start_us + index * 8_333)!,
  );
  assert.deepEqual(samples, [...samples].sort((a, b) => a - b));
  const boundary = boundaries[1];
  const before = cinematicLineProgressPpm(analyzed, boundary - 1_000)!;
  const center = cinematicLineProgressPpm(analyzed, boundary)!;
  const after = cinematicLineProgressPpm(analyzed, boundary + 1_000)!;
  assert.ok(Math.abs((center - before) - (after - center)) <= 3);
});

test('display clock fills the repeated frames of a 30 fps source at 60 Hz', () => {
  const clock = { anchorMediaUs: null, anchorSampleMs: 0, playbackRate: 1 };
  const samples = Array.from({ length: 12 }, (_, index) => {
    const sampleMs = index * (1_000 / 60);
    const rawFrame = Math.floor(index / 2) * (1_000_000 / 30);
    return smoothedPlaybackTimeUs(clock, rawFrame, sampleMs, 1);
  });
  assert.ok(samples.every((value, index) => index === 0 || value > samples[index - 1]));
  assert.ok(samples.every((value, index) => Math.abs(value - index * (1_000_000 / 60)) <= 1));

  const seeked = smoothedPlaybackTimeUs(clock, 3_000_000, 250, 1, true);
  assert.equal(seeked, 3_000_000);
});

test('Preview defaults to the exact 1920x1080 120 fps output grid', () => {
  assert.deepEqual(
    [KARAOKE_PREVIEW_WIDTH, KARAOKE_PREVIEW_HEIGHT, KARAOKE_PREVIEW_FPS],
    [1_920, 1_080, 120],
  );
  assert.deepEqual(
    [0, 8_333, 16_666, 16_667, 1_000_000].map(previewFrameTimeUs),
    [0, 0, 8_333, 16_667, 1_000_000],
  );
  assert.equal(stepPreviewFrameTimeUs(0, 1), 8_333);
  assert.equal(stepPreviewFrameTimeUs(8_333, 1), 16_667);
  assert.equal(stepPreviewFrameTimeUs(16_667, -1), 8_333);
});

test('manual boundary edit rescales curve and invalidates motion verification', () => {
  const curve = {
    schema_version: '1.0' as const,
    source: 'ensemble_ctc' as const,
    confidence: 0.96,
    verified: true,
    points: [
      { time_us: 1_000_000, line_progress_ppm: 0 },
      { time_us: 1_400_000, line_progress_ppm: 150_000 },
      { time_us: 1_800_000, line_progress_ppm: 400_000 },
    ],
  };
  const changed = rescaleSweep(curve, 1_000_000, 1_800_000, 900_000, 2_100_000)!;
  assert.equal(changed.points[0].time_us, 900_000);
  assert.equal(changed.points.at(-1)!.time_us, 2_100_000);
  assert.equal(changed.source, 'manual_rescaled');
  assert.equal(changed.verified, false);
});

test('token display segments preserve exact spaces and punctuation', () => {
  const spaced = structuredClone(line);
  spaced.text = 'Xin   chào!';
  spaced.tokens[1].text = 'chào';

  const segments = tokenDisplaySegments(spaced);

  assert.equal(segments.map((item) => item.text).join(''), spaced.text);
  assert.deepEqual(segments.map((item) => item.text), ['Xin   ', 'chào!']);
});

test('inline token approval verifies its sweep without approving untouched token', () => {
  const editable = structuredClone(timeline);
  editable.lines[0].tokens[0].sweep = {
    schema_version: '1.0',
    source: 'manual_rescaled',
    confidence: 0.7,
    verified: false,
    points: [
      { time_us: 1_000_000, line_progress_ppm: 0 },
      { time_us: 1_800_000, line_progress_ppm: 400_000 },
    ],
  };

  const approved = verifyTiming(editable, 'line-1', 't1');

  assert.equal(approved.lines[0].tokens[0].verified, true);
  assert.equal(approved.lines[0].tokens[0].sweep?.verified, true);
  assert.equal(approved.lines[0].tokens[1].verified, false);
  assert.equal(approved.lines[0].verified, false);
});
