export const MAX_BATCH_SONGS = 12;
export const BATCH_PROCESS_CONCURRENCY = 2;

export type NamedFile = { name: string };

export interface BatchFilePair<TFile extends NamedFile> {
  id: string;
  video: TFile;
  timeline: TFile | null;
  suggestedTitle: string;
}

const extensionPattern = /\.[^.]+$/;

export function displayTitleFromFilename(filename: string): string {
  const stem = filename.replace(extensionPattern, '');
  return stem.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim() || 'Karaoke';
}

export function normalizedFileStem(filename: string): string {
  return displayTitleFromFilename(filename)
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[đĐ]/g, 'd')
    .toLocaleLowerCase('vi')
    .replace(/[^a-z0-9]+/g, '');
}

export function pairBatchFiles<TFile extends NamedFile>(
  videos: readonly TFile[],
  timelines: readonly TFile[],
): BatchFilePair<TFile>[] {
  const remainingTimelineIndexes = new Set(timelines.map((_file, index) => index));
  const pairs = videos.map((video, videoIndex) => {
    const videoStem = normalizedFileStem(video.name);
    const exactMatches = [...remainingTimelineIndexes].filter(
      (timelineIndex) => normalizedFileStem(timelines[timelineIndex].name) === videoStem,
    );
    const timelineIndex = exactMatches.length === 1 ? exactMatches[0] : null;
    if (timelineIndex !== null) remainingTimelineIndexes.delete(timelineIndex);
    return {
      id: `${video.name}:${videoIndex}`,
      video,
      timeline: timelineIndex === null ? null : timelines[timelineIndex],
      suggestedTitle: displayTitleFromFilename(video.name),
    };
  });

  const unmatchedPairs = pairs.filter((pair) => pair.timeline === null);
  const remainingTimelines = [...remainingTimelineIndexes].map((index) => timelines[index]);
  if (unmatchedPairs.length === remainingTimelines.length) {
    unmatchedPairs.forEach((pair, index) => {
      pair.timeline = remainingTimelines[index];
    });
  }
  return pairs;
}

export async function mapWithConcurrency<TInput, TOutput>(
  items: readonly TInput[],
  concurrency: number,
  worker: (item: TInput, index: number) => Promise<TOutput>,
): Promise<Array<PromiseSettledResult<TOutput>>> {
  const results: Array<PromiseSettledResult<TOutput>> = new Array(items.length);
  let cursor = 0;
  const workerCount = Math.min(items.length, Math.max(1, Math.floor(concurrency)));
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      try {
        results[index] = { status: 'fulfilled', value: await worker(items[index], index) };
      } catch (reason) {
        results[index] = { status: 'rejected', reason };
      }
    }
  }));
  return results;
}
