import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyJobProgressEvent,
  JobMonitor,
  type JobMonitorOptions,
} from './job-monitor.ts';
import type { Job } from './types.ts';

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job_progress',
    project_id: 'proj_progress',
    kind: 'process',
    state: 'RUNNING',
    progress: 0,
    message: 'Đang xếp tác vụ…',
    error: null,
    ...overrides,
  };
}

class FakeEventSource {
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  closed = false;
  terminalListener: ((event: MessageEvent<string>) => void) | null = null;

  addEventListener(
    type: 'terminal',
    listener: (event: MessageEvent<string>) => void,
  ): void {
    if (type === 'terminal') this.terminalListener = listener;
  }

  close(): void {
    this.closed = true;
  }
}

test('progress frames cannot overwrite the durable job id', () => {
  const initial = job();
  const updated = applyJobProgressEvent(initial, {
    job_id: initial.id,
    state: 'RUNNING',
    progress: 0.39,
    message: 'Đang căn lời…',
    id: 912,
  } as never);

  assert.equal(updated.id, 'job_progress');
  assert.equal(updated.progress, 0.39);
  assert.equal(updated.message, 'Đang căn lời…');
  assert.equal(applyJobProgressEvent(initial, { job_id: 'job_khac', progress: 1 }), initial);
});

test('an SSE error keeps native reconnect alive and starts fallback reconciliation', async () => {
  const source = new FakeEventSource();
  const scheduled: Array<() => void> = [];
  const updates: Job[] = [];
  const options: JobMonitorOptions = {
    initial: job(),
    eventUrl: 'http://127.0.0.1:8000/api/jobs/job_progress/events',
    fetchJob: async () => job({ progress: 0.39, message: 'Đã tách giọng' }),
    onUpdate: (current) => updates.push(current),
    onTerminal: () => undefined,
    createEventSource: () => source,
    schedule: (callback) => {
      scheduled.push(callback);
      return scheduled.length as unknown as ReturnType<typeof setTimeout>;
    },
    cancelSchedule: () => undefined,
  };
  const monitor = new JobMonitor(options);
  monitor.start();

  source.onerror?.(new Event('error'));
  assert.equal(source.closed, false);
  assert.equal(scheduled.length, 1);

  await monitor.reconcile();
  assert.equal(updates.at(-1)?.progress, 0.39);
  assert.equal(updates.at(-1)?.message, 'Đã tách giọng');
  monitor.stop();
});

test('fallback reconciliation observes completion and refreshes exactly once', async () => {
  const source = new FakeEventSource();
  const updates: Job[] = [];
  const terminal: Job[] = [];
  const monitor = new JobMonitor({
    initial: job({ progress: 0.39 }),
    eventUrl: 'http://127.0.0.1:8000/api/jobs/job_progress/events',
    fetchJob: async () => job({
      state: 'COMPLETE',
      progress: 1,
      message: 'Hoàn tất phân tích',
    }),
    onUpdate: (current) => updates.push(current),
    onTerminal: (current) => {
      terminal.push(current);
    },
    createEventSource: () => source,
  });
  monitor.start();

  await monitor.reconcile();
  await Promise.resolve();

  assert.equal(source.closed, true);
  assert.equal(updates.at(-1)?.progress, 1);
  assert.equal(terminal.length, 1);
  assert.equal(terminal[0].state, 'COMPLETE');

  await monitor.reconcile();
  assert.equal(terminal.length, 1);
});
