import type { Job } from './types';

export const JOB_POLL_INTERVAL_MS = 1_500;

type JobProgressEvent = {
  job_id?: unknown;
  state?: unknown;
  progress?: unknown;
  message?: unknown;
};

type EventSourceLike = {
  onerror: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onopen: ((event: Event) => void) | null;
  addEventListener: (
    type: 'terminal',
    listener: (event: MessageEvent<string>) => void,
  ) => void;
  close: () => void;
};

type TimerHandle = ReturnType<typeof setTimeout>;

export type JobMonitorOptions = {
  initial: Job;
  eventUrl: string;
  fetchJob: () => Promise<Job>;
  onUpdate: (job: Job) => void;
  onTerminal: (job: Job) => void | Promise<void>;
  createEventSource?: (url: string) => EventSourceLike;
  pollIntervalMs?: number;
  schedule?: (callback: () => void, delayMs: number) => TimerHandle;
  cancelSchedule?: (handle: TimerHandle) => void;
};

const JOB_STATES = new Set<Job['state']>([
  'PENDING',
  'RUNNING',
  'COMPLETE',
  'FAILED',
  'CANCELLED',
]);

export function isTerminalJob(job: Job): boolean {
  return ['COMPLETE', 'FAILED', 'CANCELLED'].includes(job.state);
}

export function applyJobProgressEvent(current: Job, payload: JobProgressEvent): Job {
  if (payload.job_id !== current.id) return current;

  const next = { ...current };
  if (typeof payload.state === 'string' && JOB_STATES.has(payload.state as Job['state'])) {
    next.state = payload.state as Job['state'];
  }
  if (typeof payload.progress === 'number' && Number.isFinite(payload.progress)) {
    next.progress = Math.max(0, Math.min(1, payload.progress));
  }
  if (typeof payload.message === 'string') next.message = payload.message;
  return next;
}

export class JobMonitor {
  private readonly options: JobMonitorOptions;
  private current: Job;
  private source: EventSourceLike | null = null;
  private pollTimer: TimerHandle | null = null;
  private fallbackPolling = false;
  private stopped = false;

  private readonly createEventSource: (url: string) => EventSourceLike;
  private readonly pollIntervalMs: number;
  private readonly schedule: (callback: () => void, delayMs: number) => TimerHandle;
  private readonly cancelSchedule: (handle: TimerHandle) => void;

  constructor(options: JobMonitorOptions) {
    this.options = options;
    this.current = options.initial;
    this.createEventSource = options.createEventSource
      ?? ((url) => new EventSource(url) as EventSourceLike);
    this.pollIntervalMs = options.pollIntervalMs ?? JOB_POLL_INTERVAL_MS;
    this.schedule = options.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
    this.cancelSchedule = options.cancelSchedule ?? ((handle) => clearTimeout(handle));
  }

  start(): void {
    if (this.stopped || this.source) return;
    if (isTerminalJob(this.current)) {
      this.finish(this.current);
      return;
    }

    const source = this.createEventSource(this.options.eventUrl);
    this.source = source;
    source.onmessage = (event) => {
      if (this.stopped || this.source !== source) return;
      try {
        const next = applyJobProgressEvent(
          this.current,
          JSON.parse(event.data) as JobProgressEvent,
        );
        if (next !== this.current) {
          this.current = next;
          this.options.onUpdate(next);
        }
      } catch {
        // Ignore a malformed progress frame. The next SSE frame or fallback poll
        // still has a chance to reconcile the durable backend state.
      }
    };
    source.addEventListener('terminal', (event) => {
      if (this.stopped || this.source !== source) return;
      try {
        const terminal = JSON.parse(event.data) as Job;
        if (terminal.id === this.current.id && isTerminalJob(terminal)) {
          this.finish(terminal);
        }
      } catch {
        this.enableFallbackPolling();
      }
    });
    source.onopen = () => {
      if (this.stopped || this.source !== source) return;
      this.disableFallbackPolling();
    };
    source.onerror = () => {
      if (this.stopped || this.source !== source) return;
      // Do not close the EventSource: browsers reconnect it automatically and
      // preserve Last-Event-ID. Poll the durable job record until SSE reopens.
      this.enableFallbackPolling();
    };
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.fallbackPolling = false;
    if (this.pollTimer !== null) {
      this.cancelSchedule(this.pollTimer);
      this.pollTimer = null;
    }
    this.source?.close();
    this.source = null;
  }

  async reconcile(): Promise<void> {
    if (this.stopped) return;
    try {
      const current = await this.options.fetchJob();
      if (this.stopped || current.id !== this.current.id) return;
      if (isTerminalJob(current)) {
        this.finish(current);
        return;
      }
      this.current = current;
      this.options.onUpdate(current);
    } catch {
      // A temporary API outage is expected while the local server reconnects.
    }
    if (this.fallbackPolling) this.schedulePoll(this.pollIntervalMs);
  }

  private enableFallbackPolling(): void {
    if (this.stopped) return;
    this.fallbackPolling = true;
    this.schedulePoll(0);
  }

  private disableFallbackPolling(): void {
    this.fallbackPolling = false;
    if (this.pollTimer !== null) {
      this.cancelSchedule(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private schedulePoll(delayMs: number): void {
    if (this.stopped || !this.fallbackPolling || this.pollTimer !== null) return;
    this.pollTimer = this.schedule(() => {
      this.pollTimer = null;
      void this.reconcile();
    }, delayMs);
  }

  private finish(terminal: Job): void {
    if (this.stopped) return;
    this.current = terminal;
    this.options.onUpdate(terminal);
    this.stop();
    void Promise.resolve(this.options.onTerminal(terminal)).catch(() => undefined);
  }
}
