import type { LineTiming, SweepCurveV1, Timeline, TimingSuggestion } from './types';

export interface TokenDisplaySegment {
  tokenId: string;
  text: string;
}

export interface ManualLoopRange {
  startUs: number;
  endUs: number;
  tokenId: string;
  kind: 'token' | 'transition' | 'line';
  repeat: boolean;
}

export interface KaraokeCountdownCue {
  lane: 0 | 1;
  nextLineId: string;
  number: 1 | 2 | 3;
}

export interface KaraokeDisplayRow {
  text: string;
  startProgressPpm: number;
  endProgressPpm: number;
}

export interface SmoothPlaybackClock {
  anchorMediaUs: number | null;
  anchorSampleMs: number;
  playbackRate: number;
}

const KARAOKE_COUNTDOWN_US = 3_000_000;
const PLAYBACK_CLOCK_REANCHOR_US = 90_000;
const PLAYBACK_CLOCK_SOFT_DRIFT_US = 25_000;
export const KARAOKE_PREVIEW_WIDTH = 1_920;
export const KARAOKE_PREVIEW_HEIGHT = 1_080;
export const KARAOKE_PREVIEW_FPS = 120;

/** Snap a playback timestamp to the exact frame grid used by 1080p120 export. */
export function previewFrameTimeUs(nowUs: number): number {
  const frameIndex = Math.floor(Math.max(0, nowUs) * KARAOKE_PREVIEW_FPS / 1_000_000);
  return Math.round(frameIndex * 1_000_000 / KARAOKE_PREVIEW_FPS);
}

/** Move by one native Preview frame without accumulating 8,333 µs rounding drift. */
export function stepPreviewFrameTimeUs(nowUs: number, direction: -1 | 1): number {
  const currentFrame = Math.round(Math.max(0, nowUs) * KARAOKE_PREVIEW_FPS / 1_000_000);
  const nextFrame = Math.max(0, currentFrame + direction);
  return Math.round(nextFrame * 1_000_000 / KARAOKE_PREVIEW_FPS);
}

/**
 * Extrapolate a display clock between decoded source-video frames. A 30 fps
 * source can otherwise make a 60/120 Hz Karaoke sweep visibly repeat frames.
 */
export function smoothedPlaybackTimeUs(
  clock: SmoothPlaybackClock,
  rawMediaUs: number,
  sampleMs: number,
  playbackRate: number,
  reset = false,
): number {
  const safeRate = Number.isFinite(playbackRate) && playbackRate > 0 ? playbackRate : 1;
  const reanchor = () => {
    clock.anchorMediaUs = rawMediaUs;
    clock.anchorSampleMs = sampleMs;
    clock.playbackRate = safeRate;
    return Math.max(0, Math.round(rawMediaUs));
  };
  if (
    reset
    || clock.anchorMediaUs === null
    || Math.abs(clock.playbackRate - safeRate) > 0.0001
  ) return reanchor();
  let predictedUs = clock.anchorMediaUs
    + (sampleMs - clock.anchorSampleMs) * 1_000 * safeRate;
  const driftUs = rawMediaUs - predictedUs;
  if (Math.abs(driftUs) > PLAYBACK_CLOCK_REANCHOR_US) return reanchor();
  if (Math.abs(driftUs) > PLAYBACK_CLOCK_SOFT_DRIFT_US) {
    const correctionUs = driftUs * 0.04;
    clock.anchorMediaUs += correctionUs;
    predictedUs += correctionUs;
  }
  return Math.max(0, Math.round(predictedUs));
}

export function karaokeCountdownCue(
  timeline: Timeline,
  nowUs: number,
): KaraokeCountdownCue | null {
  const upcomingIndex = timeline.lines.findIndex((line) => line.start_us > nowUs);
  if (upcomingIndex < 0) return null;
  const upcoming = timeline.lines[upcomingIndex];
  const gapStartUs = upcomingIndex === 0
    ? 0
    : timeline.lines[upcomingIndex - 1].end_us;
  const gapUs = upcoming.start_us - gapStartUs;
  const untilUs = upcoming.start_us - nowUs;
  if (
    gapUs < KARAOKE_COUNTDOWN_US
    || nowUs < gapStartUs
    || untilUs <= 0
    || untilUs > KARAOKE_COUNTDOWN_US
  ) return null;
  return {
    lane: (1 - (upcomingIndex % 2)) as 0 | 1,
    nextLineId: upcoming.id,
    number: Math.max(1, Math.min(3, Math.ceil(untilUs / 1_000_000))) as 1 | 2 | 3,
  };
}

export function timeUsToPixels(timeUs: number, pixelsPerSecond: number): number {
  return Math.round((Math.max(0, timeUs) * Math.max(1, pixelsPerSecond)) / 1_000_000);
}

export function lyricFitScale(availableWidth: number, naturalWidth: number): number {
  const available = Math.max(1, availableWidth);
  const natural = Math.max(1, naturalWidth);
  return Math.min(1, available / natural);
}

export function nearestMarkerWithin(
  markers: number[],
  targetUs: number,
  thresholdUs: number,
): number | null {
  const maximumDistanceUs = Math.max(0, thresholdUs);
  let nearest: number | null = null;
  for (const markerUs of markers) {
    if (!Number.isInteger(markerUs)) continue;
    const distanceUs = Math.abs(markerUs - targetUs);
    if (distanceUs > maximumDistanceUs) continue;
    if (nearest === null || distanceUs < Math.abs(nearest - targetUs)) {
      nearest = markerUs;
    }
  }
  return nearest;
}

let manualIdSequence = 0;

function nextManualId(kind: 'line' | 'token'): string {
  manualIdSequence += 1;
  return `manual-${kind}-${Date.now().toString(36)}-${manualIdSequence.toString(36)}`;
}

function manualWords(value: string): string[] {
  return value
    .normalize('NFC')
    .trim()
    .split(/\s+/u)
    .filter(Boolean);
}

function normalizeManualToken(value: string): string {
  return value
    .normalize('NFC')
    .toLocaleLowerCase('vi')
    .replace(/[^\p{L}\p{N}_]+/gu, '');
}

function rebuildManualLine(line: LineTiming): void {
  const displayTokens = line.tokens.map((token) => token.text.normalize('NFC').trim());
  line.text = displayTokens.join(' ');
  const totalCharacters = Math.max(1, line.text.length);
  let characterCursor = 0;
  let previousProgressPpm = 0;
  line.tokens.forEach((token, index) => {
    token.text = displayTokens[index];
    token.normalized = normalizeManualToken(token.text);
    const tokenEndCharacter = index === line.tokens.length - 1
      ? totalCharacters
      : characterCursor + token.text.length + 1;
    const endProgressPpm = index === line.tokens.length - 1
      ? 1_000_000
      : Math.max(
        previousProgressPpm,
        Math.round((tokenEndCharacter * 1_000_000) / totalCharacters),
      );
    token.source = 'manual';
    token.confidence = Math.min(0.6, token.confidence);
    token.verified = false;
    token.locked = false;
    token.sweep = {
      schema_version: '1.0',
      source: 'manual_rescaled',
      confidence: Math.min(0.6, token.sweep?.confidence ?? token.confidence),
      verified: false,
      points: [
        { time_us: token.start_us, line_progress_ppm: previousProgressPpm },
        { time_us: token.end_us, line_progress_ppm: endProgressPpm },
      ],
    };
    previousProgressPpm = endProgressPpm;
    characterCursor = tokenEndCharacter;
  });
  line.start_us = line.tokens[0].start_us;
  line.end_us = line.tokens.at(-1)!.end_us;
  line.source = 'manual';
  line.confidence = line.tokens.reduce((sum, token) => sum + token.confidence, 0)
    / line.tokens.length;
  line.verified = false;
  line.locked = false;
}

export function editTimelineTokenText(
  timeline: Timeline,
  lineId: string,
  tokenId: string,
  value: string,
): Timeline {
  const words = manualWords(value);
  const line = timeline.lines.find((candidate) => candidate.id === lineId);
  const token = line?.tokens.find((candidate) => candidate.id === tokenId);
  if (
    words.length !== 1
    || !line
    || !token
    || line.locked
    || line.tokens.some((candidate) => candidate.locked)
  ) return timeline;
  const copy = structuredClone(timeline);
  const changedLine = copy.lines.find((candidate) => candidate.id === lineId)!;
  changedLine.tokens.find((candidate) => candidate.id === tokenId)!.text = words[0];
  rebuildManualLine(changedLine);
  return copy;
}

export function insertTimelineToken(
  timeline: Timeline,
  lineId: string,
  tokenId: string,
  value: string,
  position: 'before' | 'after',
): Timeline {
  const words = manualWords(value);
  const line = timeline.lines.find((candidate) => candidate.id === lineId);
  const tokenIndex = line?.tokens.findIndex((candidate) => candidate.id === tokenId) ?? -1;
  const token = line?.tokens[tokenIndex];
  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  if (
    words.length !== 1
    || !line
    || !token
    || line.locked
    || line.tokens.some((candidate) => candidate.locked)
    || token.end_us - token.start_us < frameUs * 2
  ) return timeline;

  const copy = structuredClone(timeline);
  const changedLine = copy.lines.find((candidate) => candidate.id === lineId)!;
  const changedToken = changedLine.tokens[tokenIndex];
  const splitUs = Math.max(
    changedToken.start_us + frameUs,
    Math.min(
      changedToken.end_us - frameUs,
      Math.round((changedToken.start_us + changedToken.end_us) / 2),
    ),
  );
  const inserted = {
    ...structuredClone(changedToken),
    id: nextManualId('token'),
    text: words[0],
    normalized: normalizeManualToken(words[0]),
    confidence: 0.5,
    source: 'manual',
    verified: false,
    locked: false,
  };
  if (position === 'before') {
    inserted.start_us = changedToken.start_us;
    inserted.end_us = splitUs;
    changedToken.start_us = splitUs;
    changedLine.tokens.splice(tokenIndex, 0, inserted);
  } else {
    inserted.start_us = splitUs;
    inserted.end_us = changedToken.end_us;
    changedToken.end_us = splitUs;
    changedLine.tokens.splice(tokenIndex + 1, 0, inserted);
  }
  rebuildManualLine(changedLine);
  return copy;
}

export function deleteTimelineToken(
  timeline: Timeline,
  lineId: string,
  tokenId: string,
): Timeline {
  const line = timeline.lines.find((candidate) => candidate.id === lineId);
  const tokenIndex = line?.tokens.findIndex((candidate) => candidate.id === tokenId) ?? -1;
  if (
    !line
    || tokenIndex < 0
    || line.tokens.length <= 1
    || line.locked
    || line.tokens.some((candidate) => candidate.locked)
  ) return timeline;
  const copy = structuredClone(timeline);
  const changedLine = copy.lines.find((candidate) => candidate.id === lineId)!;
  const removed = changedLine.tokens[tokenIndex];
  if (tokenIndex === 0) {
    changedLine.tokens[1].start_us = removed.start_us;
  } else {
    changedLine.tokens[tokenIndex - 1].end_us = removed.end_us;
  }
  changedLine.tokens.splice(tokenIndex, 1);
  rebuildManualLine(changedLine);
  return copy;
}

export function insertTimelineLine(
  timeline: Timeline,
  afterLineId: string,
  value: string,
  preferredStartUs?: number,
): Timeline {
  const words = manualWords(value);
  const lineIndex = timeline.lines.findIndex((line) => line.id === afterLineId);
  if (!words.length || lineIndex < 0) return timeline;
  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  const minimumDurationUs = frameUs * words.length;
  const previous = timeline.lines[lineIndex];
  const next = timeline.lines[lineIndex + 1];
  const latestOrderedStartUs = next?.start_us ?? timeline.duration_us - minimumDurationUs;
  let startUs = Math.max(
    previous.start_us,
    Math.min(
      latestOrderedStartUs,
      preferredStartUs ?? previous.end_us,
    ),
  );
  if (timeline.duration_us - startUs < minimumDurationUs) {
    startUs = Math.max(previous.start_us, timeline.duration_us - minimumDurationUs);
  }
  if (timeline.duration_us - startUs < minimumDurationUs) return timeline;

  const desiredDurationUs = Math.max(minimumDurationUs, Math.min(4_000_000, words.length * 450_000));
  const gapEndUs = next?.start_us ?? timeline.duration_us;
  const gapDurationUs = gapEndUs - startUs;
  const durationUs = gapDurationUs >= minimumDurationUs
    ? Math.min(desiredDurationUs, gapDurationUs)
    : Math.min(desiredDurationUs, timeline.duration_us - startUs);
  if (durationUs < minimumDurationUs) return timeline;
  const endUs = startUs + durationUs;
  const weights = words.map((word) => Math.max(1, normalizeManualToken(word).length));
  const totalWeight = weights.reduce((sum, weight) => sum + weight, 0);
  const boundaries = [startUs];
  let consumedWeight = 0;
  for (let index = 0; index < words.length - 1; index += 1) {
    consumedWeight += weights[index];
    const remainingTokens = words.length - index - 1;
    const proposed = startUs + Math.round((durationUs * consumedWeight) / totalWeight);
    boundaries.push(Math.max(
      boundaries.at(-1)! + frameUs,
      Math.min(endUs - remainingTokens * frameUs, proposed),
    ));
  }
  boundaries.push(endUs);
  const newLine: LineTiming = {
    id: nextManualId('line'),
    text: words.join(' '),
    start_us: startUs,
    end_us: endUs,
    confidence: 0.5,
    source: 'manual',
    verified: false,
    locked: false,
    tokens: words.map((word, index) => ({
      id: nextManualId('token'),
      text: word,
      normalized: normalizeManualToken(word),
      start_us: boundaries[index],
      end_us: boundaries[index + 1],
      confidence: 0.5,
      source: 'manual',
      verified: false,
      locked: false,
      sweep: null,
    })),
  };
  rebuildManualLine(newLine);
  const copy = structuredClone(timeline);
  copy.lines.splice(lineIndex + 1, 0, newLine);
  return copy;
}

/**
 * Paste a copied lyric line at a new frame without rescaling its rhythm.
 * Every token duration, gap, and sweep control-point offset stays identical;
 * only the absolute timestamps and ids change.
 */
export function pasteTimelineLineAt(
  timeline: Timeline,
  copiedLine: LineTiming,
  preferredStartUs: number,
): Timeline {
  const durationUs = copiedLine.end_us - copiedLine.start_us;
  if (
    durationUs <= 0
    || durationUs > timeline.duration_us
    || copiedLine.tokens.length === 0
  ) return timeline;

  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  const requestedStartUs = Number.isFinite(preferredStartUs)
    ? Math.max(0, preferredStartUs)
    : copiedLine.start_us;
  const snappedStartUs = Math.round(requestedStartUs / frameUs) * frameUs;
  const startUs = Math.min(timeline.duration_us - durationUs, snappedStartUs);
  const deltaUs = startUs - copiedLine.start_us;
  const pastedLine = structuredClone(copiedLine);
  pastedLine.id = nextManualId('line');
  pastedLine.start_us += deltaUs;
  pastedLine.end_us += deltaUs;
  pastedLine.source = 'manual';
  pastedLine.confidence = Math.min(0.77, pastedLine.confidence);
  pastedLine.verified = false;
  pastedLine.locked = false;
  pastedLine.tokens.forEach((token) => {
    token.id = nextManualId('token');
    token.start_us += deltaUs;
    token.end_us += deltaUs;
    token.source = 'manual';
    token.confidence = Math.min(0.77, token.confidence);
    token.verified = false;
    token.locked = false;
    if (token.sweep) {
      token.sweep.source = 'manual_rescaled';
      token.sweep.confidence = Math.min(0.77, token.sweep.confidence);
      token.sweep.verified = false;
      token.sweep.points.forEach((point) => { point.time_us += deltaUs; });
    }
  });

  const copy = structuredClone(timeline);
  const insertIndex = copy.lines.findIndex((line) => line.start_us > pastedLine.start_us);
  copy.lines.splice(insertIndex < 0 ? copy.lines.length : insertIndex, 0, pastedLine);
  return copy;
}

export function deleteTimelineLine(timeline: Timeline, lineId: string): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  const line = timeline.lines[lineIndex];
  if (
    !line
    || timeline.lines.length <= 1
    || line.locked
    || line.tokens.some((token) => token.locked)
  ) return timeline;
  const copy = structuredClone(timeline);
  copy.lines.splice(lineIndex, 1);
  return copy;
}

export function moveLineBy(
  timeline: Timeline,
  lineId: string,
  deltaUs: number,
): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  const sourceLine = timeline.lines[lineIndex];
  if (!sourceLine || sourceLine.locked || sourceLine.tokens.some((token) => token.locked)) {
    return timeline;
  }
  const previousStartUs = lineIndex > 0 ? timeline.lines[lineIndex - 1].start_us : 0;
  const nextStartUs = lineIndex + 1 < timeline.lines.length
    ? timeline.lines[lineIndex + 1].start_us
    : timeline.duration_us;
  const minimumDeltaUs = Math.max(
    -sourceLine.start_us,
    previousStartUs - sourceLine.start_us,
  );
  const maximumDeltaUs = Math.min(
    timeline.duration_us - sourceLine.end_us,
    nextStartUs - sourceLine.start_us,
  );
  const appliedDeltaUs = Math.max(minimumDeltaUs, Math.min(maximumDeltaUs, deltaUs));
  if (!appliedDeltaUs) return timeline;
  const line = structuredClone(sourceLine);
  line.start_us += appliedDeltaUs;
  line.end_us += appliedDeltaUs;
  line.source = 'manual';
  line.verified = false;
  line.tokens.forEach((token) => {
    token.start_us += appliedDeltaUs;
    token.end_us += appliedDeltaUs;
    if (token.sweep) {
      token.sweep.points.forEach((point) => { point.time_us += appliedDeltaUs; });
      token.sweep.source = 'manual_rescaled';
      token.sweep.confidence = Math.min(0.77, token.sweep.confidence);
      token.sweep.verified = false;
    }
    token.source = 'manual';
    token.verified = false;
  });
  const lines = [...timeline.lines];
  lines[lineIndex] = line;
  return { ...timeline, lines };
}

export function moveTokenBy(
  timeline: Timeline,
  lineId: string,
  tokenId: string,
  deltaUs: number,
): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  const sourceLine = timeline.lines[lineIndex];
  const tokenIndex = sourceLine?.tokens.findIndex((token) => token.id === tokenId) ?? -1;
  const sourceToken = sourceLine?.tokens[tokenIndex];
  if (!sourceLine || !sourceToken || sourceLine.locked || sourceToken.locked) {
    return timeline;
  }
  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  const durationUs = sourceToken.end_us - sourceToken.start_us;
  const sourcePrevious = tokenIndex > 0 ? sourceLine.tokens[tokenIndex - 1] : null;
  const sourceNext = tokenIndex + 1 < sourceLine.tokens.length
    ? sourceLine.tokens[tokenIndex + 1]
    : null;
  if (sourcePrevious?.locked || sourceNext?.locked) return timeline;
  const previousLineStartUs = lineIndex > 0 ? timeline.lines[lineIndex - 1].start_us : 0;
  const nextLineStartUs = lineIndex + 1 < timeline.lines.length
    ? timeline.lines[lineIndex + 1].start_us
    : timeline.duration_us;
  const minimumStartUs = sourcePrevious
    ? sourcePrevious.start_us + frameUs
    : previousLineStartUs;
  let maximumStartUs = sourceNext
    ? sourceNext.end_us - frameUs - durationUs
    : timeline.duration_us - durationUs;
  if (tokenIndex === 0) {
    maximumStartUs = Math.min(maximumStartUs, nextLineStartUs);
  }
  const newStartUs = Math.max(
    minimumStartUs,
    Math.min(maximumStartUs, sourceToken.start_us + deltaUs),
  );
  const newEndUs = newStartUs + durationUs;
  if (newStartUs === sourceToken.start_us) return timeline;

  const line = structuredClone(sourceLine);
  const token = line.tokens[tokenIndex];
  const previous = tokenIndex > 0 ? line.tokens[tokenIndex - 1] : null;
  const next = tokenIndex + 1 < line.tokens.length ? line.tokens[tokenIndex + 1] : null;

  const rescale = (
    affected: typeof token,
    startUs: number,
    endUs: number,
  ) => {
    affected.sweep = rescaleSweep(
      affected.sweep,
      affected.start_us,
      affected.end_us,
      startUs,
      endUs,
    );
    affected.start_us = startUs;
    affected.end_us = endUs;
    affected.source = 'manual';
    affected.verified = false;
    if (affected.sweep) affected.sweep.verified = false;
  };

  if (previous) rescale(previous, previous.start_us, newStartUs);
  rescale(token, newStartUs, newEndUs);
  if (next) rescale(next, newEndUs, next.end_us);
  if (tokenIndex === 0) line.start_us = newStartUs;
  if (tokenIndex === line.tokens.length - 1) line.end_us = newEndUs;
  line.source = 'manual';
  line.verified = false;
  const lines = [...timeline.lines];
  lines[lineIndex] = line;
  return { ...timeline, lines };
}

export function activeLineAt(timeline: Timeline, nowUs: number): number | null {
  const index = timeline.lines.findIndex(
    (line) => line.start_us <= nowUs && nowUs < line.end_us,
  );
  return index >= 0 ? index : null;
}

export const KARAOKE_NEXT_LINE_LEAD_US = 4_500_000;
export const KARAOKE_INSTRUMENTAL_GAP_US = 1_500_000;
export const KARAOKE_INSTRUMENTAL_LEAD_US = 1_500_000;

function karaokeNextLineLeadUs(timeline: Timeline, nextIndex: number): number {
  if (nextIndex <= 0) return KARAOKE_NEXT_LINE_LEAD_US;
  const gapUs = timeline.lines[nextIndex].start_us - timeline.lines[nextIndex - 1].end_us;
  return gapUs >= KARAOKE_INSTRUMENTAL_GAP_US
    ? KARAOKE_INSTRUMENTAL_LEAD_US
    : KARAOKE_NEXT_LINE_LEAD_US;
}

export function karaokeVisibleLineIndexes(timeline: Timeline, nowUs: number): number[] {
  const active = activeLineAt(timeline, nowUs);
  if (active !== null) {
    const result = [active];
    const next = timeline.lines[active + 1];
    if (
      next
      && next.start_us - nowUs <= karaokeNextLineLeadUs(timeline, active + 1)
    ) {
      result.push(active + 1);
    }
    return result;
  }
  const upcoming = timeline.lines.findIndex((line) => line.start_us > nowUs);
  if (
    upcoming >= 0
    && timeline.lines[upcoming].start_us - nowUs <= karaokeNextLineLeadUs(timeline, upcoming)
  ) {
    return [upcoming];
  }
  return [];
}

export function highlightPercent(line: LineTiming, nowUs: number): number {
  if (nowUs <= line.start_us) return 0;
  if (nowUs >= line.end_us) return 100;
  const cinematicProgress = cinematicLineProgressPpm(line, nowUs);
  if (cinematicProgress !== null) return cinematicProgress / 10_000;
  const curve = line.tokens.find(
    (token) => token.sweep && token.start_us <= nowUs && nowUs <= token.end_us,
  )?.sweep;
  if (curve) return evaluateDisplaySweepPpm(curve, nowUs) / 10_000;
  const total = Math.max(1, line.text.length);
  let cursor = 0;
  for (let index = 0; index < line.tokens.length; index += 1) {
    const token = line.tokens[index];
    const found = line.text.indexOf(token.text, cursor);
    const tokenStart = found >= 0 ? found : cursor;
    const tokenEnd = Math.min(total, tokenStart + token.text.length);
    if (nowUs < token.start_us) return (cursor / total) * 100;
    if (nowUs <= token.end_us) {
      const progress =
        (nowUs - token.start_us) / Math.max(1, token.end_us - token.start_us);
      const boundary = cursor + (tokenEnd - cursor) * Math.max(0, Math.min(1, progress));
      return (boundary / total) * 100;
    }
    cursor = tokenEnd;
  }
  return 100;
}

export function evaluateSweepPpm(curve: SweepCurveV1, nowUs: number): number {
  const points = curve.points;
  if (!points.length) return 0;
  if (nowUs <= points[0].time_us) return points[0].line_progress_ppm;
  if (nowUs >= points.at(-1)!.time_us) return points.at(-1)!.line_progress_ppm;
  let right = 1;
  while (right < points.length && points[right].time_us <= nowUs) right += 1;
  const first = points[right - 1];
  const second = points[right];
  const elapsed = nowUs - first.time_us;
  const duration = Math.max(1, second.time_us - first.time_us);
  return first.line_progress_ppm
    + Math.round((elapsed * (second.line_progress_ppm - first.line_progress_ppm)) / duration);
}

/**
 * Preserve every acoustic token boundary while damping abrupt velocity changes
 * between internal CTC control points. A mostly-linear display path feels like
 * a hardware Karaoke sweep; the acoustic path still contributes subtle vowel
 * and consonant timing without creating visible stalls.
 */
export function evaluateDisplaySweepPpm(curve: SweepCurveV1, nowUs: number): number {
  const points = curve.points;
  if (points.length <= 2) return evaluateSweepPpm(curve, nowUs);
  const first = points[0];
  const last = points.at(-1)!;
  if (nowUs <= first.time_us) return first.line_progress_ppm;
  if (nowUs >= last.time_us) return last.line_progress_ppm;
  const linear = first.line_progress_ppm + Math.round(
    ((nowUs - first.time_us) * (last.line_progress_ppm - first.line_progress_ppm))
      / Math.max(1, last.time_us - first.time_us),
  );
  const acoustic = evaluateSweepPpm(curve, nowUs);
  return Math.round((acoustic + linear * 3) / 4);
}

function monotoneTangents(points: Array<[number, number]>): number[] {
  if (points.length === 2) {
    const slope = (points[1][1] - points[0][1]) / Math.max(1, points[1][0] - points[0][0]);
    return [slope, slope];
  }
  const intervals = points.slice(1).map((point, index) => point[0] - points[index][0]);
  const slopes = intervals.map((interval, index) => (
    (points[index + 1][1] - points[index][1]) / Math.max(1, interval)
  ));
  const tangents = Array<number>(points.length).fill(0);
  const endpointTangent = (
    firstInterval: number,
    secondInterval: number,
    firstSlope: number,
    secondSlope: number,
  ) => {
    const tangent = (
      (2 * firstInterval + secondInterval) * firstSlope - firstInterval * secondSlope
    ) / (firstInterval + secondInterval);
    if (tangent * firstSlope <= 0) return 0;
    if (firstSlope * secondSlope < 0 && Math.abs(tangent) > Math.abs(3 * firstSlope)) {
      return 3 * firstSlope;
    }
    return tangent;
  };
  tangents[0] = endpointTangent(intervals[0], intervals[1], slopes[0], slopes[1]);
  tangents[tangents.length - 1] = endpointTangent(
    intervals.at(-1)!, intervals.at(-2)!, slopes.at(-1)!, slopes.at(-2)!,
  );
  for (let index = 1; index < points.length - 1; index += 1) {
    const before = slopes[index - 1];
    const after = slopes[index];
    if (before <= 0 || after <= 0) continue;
    const beforeInterval = intervals[index - 1];
    const afterInterval = intervals[index];
    const firstWeight = 2 * afterInterval + beforeInterval;
    const secondWeight = afterInterval + 2 * beforeInterval;
    tangents[index] = (firstWeight + secondWeight)
      / (firstWeight / before + secondWeight / after);
  }
  return tangents;
}

function evaluateMonotonePathPpm(points: Array<[number, number]>, nowUs: number): number {
  if (nowUs <= points[0][0]) return points[0][1];
  if (nowUs >= points.at(-1)![0]) return points.at(-1)![1];
  let right = 1;
  while (right < points.length && points[right][0] <= nowUs) right += 1;
  const left = right - 1;
  const [startTime, startProgress] = points[left];
  const [endTime, endProgress] = points[right];
  const duration = Math.max(1, endTime - startTime);
  const position = (nowUs - startTime) / duration;
  const squared = position * position;
  const cubed = squared * position;
  const tangents = monotoneTangents(points);
  const progress = (
    (2 * cubed - 3 * squared + 1) * startProgress
    + (cubed - 2 * squared + position) * duration * tangents[left]
    + (-2 * cubed + 3 * squared) * endProgress
    + (cubed - squared) * duration * tangents[right]
  );
  return Math.round(Math.max(startProgress, Math.min(endProgress, progress)));
}

/**
 * Build one continuous professional display path from analyzed word endpoints.
 * Internal CTC micro-points may be jagged after re-analysis; token boundaries
 * stay exact while monotone cubic interpolation removes the visible speed jumps.
 */
export function cinematicLineProgressPpm(line: LineTiming, nowUs: number): number | null {
  if (!line.tokens.length || line.tokens.some((token) => !token.sweep)) return null;
  const points: Array<[number, number]> = [];
  for (const token of line.tokens) {
    const sweep = token.sweep!;
    for (const point of [sweep.points[0], sweep.points.at(-1)!]) {
      const previous = points.at(-1);
      if (previous?.[0] === point.time_us) {
        if (previous[1] !== point.line_progress_ppm) return null;
        continue;
      }
      if (
        previous
        && (point.time_us < previous[0] || point.line_progress_ppm < previous[1])
      ) return null;
      points.push([point.time_us, point.line_progress_ppm]);
    }
  }
  if (points.length < 2) return null;
  return evaluateMonotonePathPpm(points, nowUs);
}

export function tokenDisplaySegments(line: LineTiming): TokenDisplaySegment[] {
  if (!line.tokens.length) return [];
  const starts: number[] = [];
  let cursor = 0;
  for (const token of line.tokens) {
    const found = line.text.indexOf(token.text, cursor);
    const start = found >= 0 ? found : cursor;
    starts.push(Math.max(cursor, start));
    cursor = Math.min(line.text.length, starts.at(-1)! + token.text.length);
  }
  return line.tokens.map((token, index) => ({
    tokenId: token.id,
    text: line.text.slice(
      index === 0 ? 0 : starts[index],
      index + 1 < starts.length ? starts[index + 1] : line.text.length,
    ),
  }));
}

const KARAOKE_MAX_ROW_CHARACTERS = 48;

function visibleCharacterCount(value: string): number {
  return Array.from(value.trim()).length;
}

/**
 * Split an unusually long lyric at a real token boundary. The source lyric is
 * never rewritten; these rows are display-only and carry their original sweep
 * ranges so highlighting finishes row one before it starts row two.
 */
export function karaokeDisplayRows(
  line: LineTiming,
  maxRowCharacters = KARAOKE_MAX_ROW_CHARACTERS,
): KaraokeDisplayRow[] {
  const singleRow = [{
    text: line.text,
    startProgressPpm: 0,
    endProgressPpm: 1_000_000,
  }];
  const segments = tokenDisplaySegments(line);
  if (
    visibleCharacterCount(line.text) <= Math.max(1, maxRowCharacters)
    || segments.length < 2
  ) {
    return singleRow;
  }

  let bestIndex = -1;
  let bestScore = Number.POSITIVE_INFINITY;
  for (let index = 1; index < segments.length; index += 1) {
    const first = segments.slice(0, index).map((segment) => segment.text).join('').trimEnd();
    const second = segments.slice(index).map((segment) => segment.text).join('').trimStart();
    if (!first || !second) continue;
    const firstLength = visibleCharacterCount(first);
    const secondLength = visibleCharacterCount(second);
    const score = Math.max(firstLength, secondLength) * 2 + Math.abs(firstLength - secondLength);
    if (score < bestScore) {
      bestIndex = index;
      bestScore = score;
    }
  }
  if (bestIndex < 1) return singleRow;

  const firstText = segments.slice(0, bestIndex).map((segment) => segment.text).join('').trimEnd();
  const secondText = segments.slice(bestIndex).map((segment) => segment.text).join('').trimStart();
  const boundaryOffset = segments
    .slice(0, bestIndex)
    .reduce((total, segment) => total + segment.text.length, 0);
  const previousSweep = line.tokens[bestIndex - 1]?.sweep?.points.at(-1);
  const nextSweep = line.tokens[bestIndex]?.sweep?.points[0];
  const fallbackBoundary = Math.round(
    (boundaryOffset * 1_000_000) / Math.max(1, line.text.length),
  );
  const boundaryPpm = Math.max(
    1,
    Math.min(
      999_999,
      nextSweep?.line_progress_ppm
        ?? previousSweep?.line_progress_ppm
        ?? fallbackBoundary,
    ),
  );
  return [
    { text: firstText, startProgressPpm: 0, endProgressPpm: boundaryPpm },
    { text: secondText, startProgressPpm: boundaryPpm, endProgressPpm: 1_000_000 },
  ];
}

export function manualTokenLoopRange(
  timeline: Timeline,
  lineId: string,
  tokenId: string,
): ManualLoopRange | null {
  const auditionLeadUs = 650_000;
  const auditionTailUs = 500_000;
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  if (lineIndex < 0) return null;
  const line = timeline.lines[lineIndex];
  const tokenIndex = line.tokens.findIndex((token) => token.id === tokenId);
  const token = line.tokens[tokenIndex];
  if (!token) return null;
  const transition = tokenIndex === 0 && lineIndex > 0;
  const previous = transition ? timeline.lines[lineIndex - 1] : null;
  const previousTailStart = previous
    ? Math.max(
      previous.start_us,
      Math.min(
        (previous.tokens.at(-1)?.start_us ?? previous.end_us) - 250_000,
        line.start_us - auditionLeadUs,
      ),
    )
    : line.start_us;
  return {
    startUs: transition ? previousTailStart : Math.max(line.start_us, token.start_us - auditionLeadUs),
    endUs: Math.min(timeline.duration_us, token.end_us + auditionTailUs),
    tokenId,
    kind: transition ? 'transition' : 'token',
    repeat: true,
  };
}

export function manualLineReplayRange(
  timeline: Timeline,
  lineId: string,
): ManualLoopRange | null {
  const line = timeline.lines.find((candidate) => candidate.id === lineId);
  if (!line) return null;
  return {
    startUs: line.start_us,
    endUs: line.end_us,
    tokenId: line.tokens[0]?.id ?? line.id,
    kind: 'line',
    repeat: false,
  };
}

export function manualTransitionReplayRange(
  timeline: Timeline,
  fromLineId: string,
  toLineId: string,
): ManualLoopRange | null {
  const fromIndex = timeline.lines.findIndex((line) => line.id === fromLineId);
  const toIndex = timeline.lines.findIndex((line) => line.id === toLineId);
  if (fromIndex < 0 || toIndex !== fromIndex + 1) return null;
  const fromLine = timeline.lines[fromIndex];
  const toLine = timeline.lines[toIndex];
  return {
    startUs: fromLine.start_us,
    endUs: toLine.end_us,
    tokenId: toLine.tokens[0]?.id ?? toLine.id,
    kind: 'transition',
    repeat: false,
  };
}

export function verifyTiming(
  timeline: Timeline,
  lineId: string,
  tokenId?: string,
): Timeline {
  const copy = structuredClone(timeline);
  const line = copy.lines.find((candidate) => candidate.id === lineId);
  if (!line) return copy;
  const selected = tokenId
    ? line.tokens.filter((token) => token.id === tokenId)
    : line.tokens;
  if (!selected.length) return copy;
  selected.forEach((token) => {
    token.verified = true;
    if (token.sweep) token.sweep.verified = true;
  });
  line.verified = tokenId ? line.tokens.every((token) => token.verified) : true;
  return copy;
}

export function rescaleSweep(
  curve: SweepCurveV1 | null | undefined,
  oldStartUs: number,
  oldEndUs: number,
  newStartUs: number,
  newEndUs: number,
  manual = true,
): SweepCurveV1 | null {
  if (!curve) return null;
  const oldDuration = Math.max(1, oldEndUs - oldStartUs);
  const newDuration = Math.max(1, newEndUs - newStartUs);
  const points = curve.points.map((point) => ({
    ...point,
    time_us: newStartUs
      + Math.round(((point.time_us - oldStartUs) * newDuration) / oldDuration),
  }));
  points[0].time_us = newStartUs;
  points.at(-1)!.time_us = newEndUs;
  const intervals = Math.max(1, points.length - 1);
  const minimumStepUs = Math.min(16_667, Math.max(1, Math.floor(newDuration / intervals)));
  for (let index = 1; index < points.length; index += 1) {
    const remaining = points.length - index - 1;
    points[index].time_us = Math.max(
      points[index - 1].time_us + minimumStepUs,
      Math.min(newEndUs - remaining * minimumStepUs, points[index].time_us),
    );
    points[index].line_progress_ppm = Math.max(
      points[index - 1].line_progress_ppm,
      points[index].line_progress_ppm,
    );
  }
  return {
    ...curve,
    source: manual ? 'manual_rescaled' : curve.source,
    confidence: manual ? Math.min(0.77, curve.confidence) : curve.confidence,
    verified: manual ? false : curve.verified,
    points,
  };
}

export function setBoundary(
  timeline: Timeline,
  lineId: string,
  boundaryIndex: number,
  nextUs: number,
): Timeline {
  const copy = structuredClone(timeline);
  const line = copy.lines.find((candidate) => candidate.id === lineId);
  if (!line || !line.tokens.length || line.locked) return copy;
  const tokens = line.tokens;
  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * copy.fps_denominator / copy.fps_numerator),
  );
  const affected = new Set<number>();
  if (boundaryIndex <= 0) {
    const oldStart = tokens[0].start_us;
    const oldEnd = tokens[0].end_us;
    const value = Math.max(0, Math.min(tokens[0].end_us - frameUs, nextUs));
    line.start_us = value;
    tokens[0].start_us = value;
    tokens[0].sweep = rescaleSweep(tokens[0].sweep, oldStart, oldEnd, value, oldEnd);
    affected.add(0);
  } else if (boundaryIndex >= tokens.length) {
    const lastIndex = tokens.length - 1;
    const oldStart = tokens[lastIndex].start_us;
    const oldEnd = tokens[lastIndex].end_us;
    const value = Math.max(
      tokens.at(-1)!.start_us + frameUs,
      Math.min(copy.duration_us, nextUs),
    );
    line.end_us = value;
    tokens.at(-1)!.end_us = value;
    tokens[lastIndex].sweep = rescaleSweep(tokens[lastIndex].sweep, oldStart, oldEnd, oldStart, value);
    affected.add(lastIndex);
  } else {
    const previous = tokens[boundaryIndex - 1];
    const next = tokens[boundaryIndex];
    const previousStart = previous.start_us;
    const previousEnd = previous.end_us;
    const nextStart = next.start_us;
    const nextEnd = next.end_us;
    const combinedDuration = next.end_us - previous.start_us;
    const minimumDuration = combinedDuration >= frameUs * 2 ? frameUs : 1;
    const value = Math.max(
      previous.start_us + minimumDuration,
      Math.min(next.end_us - minimumDuration, nextUs),
    );
    previous.end_us = value;
    next.start_us = value;
    previous.sweep = rescaleSweep(previous.sweep, previousStart, previousEnd, previousStart, value);
    next.sweep = rescaleSweep(next.sweep, nextStart, nextEnd, value, nextEnd);
    affected.add(boundaryIndex - 1);
    affected.add(boundaryIndex);
  }
  line.verified = false;
  tokens.forEach((token, index) => {
    if (!affected.has(index)) return;
    token.verified = false;
    token.source = 'manual';
    if (token.sweep) token.sweep.verified = false;
  });
  line.source = 'manual';
  return copy;
}

export function trimTokenEdge(
  timeline: Timeline,
  lineId: string,
  tokenId: string,
  edge: 'start' | 'end',
  nextUs: number,
): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  const line = timeline.lines[lineIndex];
  const tokenIndex = line?.tokens.findIndex((token) => token.id === tokenId) ?? -1;
  const token = line?.tokens[tokenIndex];
  if (!line || !token || line.locked || token.locked) return timeline;

  const boundaryIndex = edge === 'start' ? tokenIndex : tokenIndex + 1;
  const adjacentIndex = edge === 'start' ? tokenIndex - 1 : tokenIndex + 1;
  if (adjacentIndex >= 0 && adjacentIndex < line.tokens.length) {
    if (line.tokens[adjacentIndex].locked) return timeline;
  }

  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  let value = Math.round(nextUs);

  if (boundaryIndex === 0) {
    const maximum = token.end_us - frameUs;
    const previousLineEnd = lineIndex > 0 ? timeline.lines[lineIndex - 1].end_us : 0;
    const minimum = Math.min(maximum, previousLineEnd);
    value = Math.max(minimum, Math.min(maximum, value));
  } else if (boundaryIndex === line.tokens.length) {
    const minimum = token.start_us + frameUs;
    const nextLineStart = lineIndex + 1 < timeline.lines.length
      ? timeline.lines[lineIndex + 1].start_us
      : timeline.duration_us;
    const maximum = Math.max(minimum, Math.min(timeline.duration_us, nextLineStart));
    value = Math.max(minimum, Math.min(maximum, value));
  }

  const currentBoundary = edge === 'start' ? token.start_us : token.end_us;
  if (value === currentBoundary) return timeline;

  return setBoundary(timeline, lineId, boundaryIndex, value);
}

export function setTransitionStart(
  timeline: Timeline,
  lineId: string,
  nextUs: number,
): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  if (lineIndex < 0) return structuredClone(timeline);
  const line = timeline.lines[lineIndex];
  if (!line.tokens.length || line.locked) return structuredClone(timeline);
  const previousEndUs = lineIndex > 0 ? timeline.lines[lineIndex - 1].end_us : 0;
  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  const latestStartUs = line.end_us - frameUs * line.tokens.length;
  const value = Math.min(latestStartUs, Math.max(previousEndUs, nextUs));

  // A small onset correction only stretches the first token. A large overlap can
  // place the requested onset beyond that token's old end; in that case rescale
  // the complete line inside its existing end boundary so the following cue is
  // not shifted and every token still owns at least one frame.
  if (value <= line.tokens[0].end_us - frameUs) {
    return setBoundary(timeline, lineId, 0, value);
  }

  const copy = structuredClone(timeline);
  const shifted = copy.lines[lineIndex];
  const oldLineStartUs = shifted.start_us;
  const oldLineEndUs = shifted.end_us;
  const oldDurationUs = Math.max(1, oldLineEndUs - oldLineStartUs);
  const newDurationUs = Math.max(frameUs * shifted.tokens.length, oldLineEndUs - value);
  const boundaries = [value];
  for (let index = 1; index < shifted.tokens.length; index += 1) {
    const oldBoundaryUs = shifted.tokens[index].start_us;
    boundaries.push(
      value + Math.round(
        ((oldBoundaryUs - oldLineStartUs) * newDurationUs) / oldDurationUs,
      ),
    );
  }
  boundaries.push(oldLineEndUs);
  for (let index = 1; index < boundaries.length - 1; index += 1) {
    const remaining = boundaries.length - index - 1;
    boundaries[index] = Math.max(
      boundaries[index - 1] + frameUs,
      Math.min(oldLineEndUs - remaining * frameUs, boundaries[index]),
    );
  }
  shifted.tokens.forEach((token, index) => {
    const oldStartUs = token.start_us;
    const oldEndUs = token.end_us;
    token.start_us = boundaries[index];
    token.end_us = boundaries[index + 1];
    token.sweep = rescaleSweep(
      token.sweep,
      oldStartUs,
      oldEndUs,
      token.start_us,
      token.end_us,
    );
    token.source = 'manual';
    token.verified = false;
    if (token.sweep) token.sweep.verified = false;
  });
  shifted.start_us = value;
  shifted.source = 'manual';
  shifted.verified = false;
  return copy;
}

export function setTransitionGap(
  timeline: Timeline,
  lineId: string,
  gapUs: number,
): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  if (lineIndex <= 0) return structuredClone(timeline);
  return setTransitionStart(
    timeline,
    lineId,
    timeline.lines[lineIndex - 1].end_us + Math.max(0, gapUs),
  );
}

export function setPreviousLineEnd(
  timeline: Timeline,
  lineId: string,
  nextUs: number,
): Timeline {
  const lineIndex = timeline.lines.findIndex((line) => line.id === lineId);
  if (lineIndex <= 0) return structuredClone(timeline);
  const current = timeline.lines[lineIndex];
  const previous = timeline.lines[lineIndex - 1];
  const frameUs = Math.max(
    1,
    Math.round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator),
  );
  return setBoundary(
    timeline,
    previous.id,
    previous.tokens.length,
    Math.min(current.end_us - frameUs, nextUs),
  );
}

export function applyTimingSuggestion(
  timeline: Timeline,
  lineId: string,
  suggestion: TimingSuggestion,
  tokenId?: string,
): Timeline {
  const copy = structuredClone(timeline);
  const line = copy.lines.find((candidate) => candidate.id === lineId);
  if (!line || line.locked || suggestion.line_id !== lineId) return copy;
  const proposals = new Map(suggestion.tokens.map((token) => [token.token_id, token]));
  if (line.tokens.some((token) => !proposals.has(token.id))) return copy;

  if (!tokenId) {
    line.tokens = line.tokens.map((token) => {
      const proposal = proposals.get(token.id)!;
      const startUs = Math.max(line.start_us, Math.min(line.end_us - 1, proposal.start_us));
      const endUs = Math.max(startUs + 1, Math.min(line.end_us, proposal.end_us));
      return {
        ...token,
        start_us: startUs,
        end_us: endUs,
        confidence: proposal.confidence,
        source: proposal.source,
        verified: false,
        sweep: rescaleSweep(
          proposal.sweep,
          proposal.start_us,
          proposal.end_us,
          startUs,
          endUs,
          false,
        ),
      };
    });
  } else {
    const index = line.tokens.findIndex((token) => token.id === tokenId);
    const proposal = proposals.get(tokenId);
    if (index < 0 || !proposal) return copy;
    const previous = line.tokens[index - 1];
    const token = line.tokens[index];
    const next = line.tokens[index + 1];
    const minimumStart = previous ? previous.start_us + 1 : line.start_us;
    const maximumStart = token.end_us - 1;
    const startUs = Math.max(minimumStart, Math.min(maximumStart, proposal.start_us));
    const minimumEnd = startUs + 1;
    const maximumEnd = next ? next.end_us - 1 : line.end_us;
    const endUs = Math.max(minimumEnd, Math.min(maximumEnd, proposal.end_us));
    if (previous) {
      const oldStart = previous.start_us;
      const oldEnd = previous.end_us;
      previous.end_us = startUs;
      previous.sweep = rescaleSweep(previous.sweep, oldStart, oldEnd, oldStart, startUs);
      previous.source = proposal.source;
      previous.confidence = Math.min(previous.confidence, proposal.confidence);
      previous.verified = false;
    }
    token.start_us = startUs;
    token.end_us = endUs;
    token.sweep = rescaleSweep(
      proposal.sweep,
      proposal.start_us,
      proposal.end_us,
      startUs,
      endUs,
      false,
    );
    token.source = proposal.source;
    token.confidence = proposal.confidence;
    token.verified = false;
    if (next) {
      const oldStart = next.start_us;
      const oldEnd = next.end_us;
      next.start_us = endUs;
      next.sweep = rescaleSweep(next.sweep, oldStart, oldEnd, endUs, oldEnd);
      next.source = proposal.source;
      next.confidence = Math.min(next.confidence, proposal.confidence);
      next.verified = false;
    }
  }
  line.source = suggestion.source;
  line.confidence = line.tokens.reduce((sum, token) => sum + token.confidence, 0) / line.tokens.length;
  line.verified = false;
  return copy;
}

export function formatTime(us: number): string {
  const totalMs = Math.max(0, Math.round(us / 1_000));
  const minutes = Math.floor(totalMs / 60_000);
  const seconds = Math.floor((totalMs % 60_000) / 1_000);
  const milliseconds = totalMs % 1_000;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(milliseconds).padStart(3, '0')}`;
}

export function confidenceLabel(value: number): string {
  if (value >= 0.9) return 'Cao';
  if (value >= 0.78) return 'Khá';
  return 'Cần duyệt';
}
