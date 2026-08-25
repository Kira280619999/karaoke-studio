'use client';

import {
  type CSSProperties,
  type FormEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { API_BASE, ApiError, api, assetUrl } from './lib/api';
import {
  BATCH_PROCESS_CONCURRENCY,
  mapWithConcurrency,
  MAX_BATCH_SONGS,
  pairBatchFiles,
} from './lib/batch-import';
import {
  backgroundMotionFrame,
  backgroundSceneProgress,
  legacyBackgroundPlan,
  smoothBackgroundTransition,
} from './lib/backgrounds';
import { JobMonitor } from './lib/job-monitor';
import {
  exportDeletePath,
  exportDownloadPath,
  videoExportArtifacts,
} from './lib/export-artifacts';
import {
  DEFAULT_EXPORT_PRESET,
  EXPORT_PRESET_OPTIONS,
  exportPresetLabel,
  type ExportPreset,
} from './lib/export-presets';
import {
  KARAOKE_COLORS,
  karaokeColorHex,
  karaokeColorId,
  karaokeColorLabel,
  type KaraokeColorId,
} from './lib/karaoke-colors';
import {
  KARAOKE_FONTS,
  karaokeFontFamily,
  karaokeFontId,
  type KaraokeFontId,
} from './lib/karaoke-fonts';
import {
  preferredPreviewAudioMode,
  previewAudioNeedsSync,
  previewWaveformFor,
  selectedInstrumentalArtifact,
  type PreviewAudioMode,
} from './lib/preview-audio';
import { songCreditOpacity } from './lib/song-credit';
import {
  activeLineAt,
  confidenceLabel,
  deleteTimelineLine,
  deleteTimelineToken,
  editTimelineTokenText,
  formatTime,
  highlightPercent,
  insertTimelineLine,
  insertTimelineToken,
  KARAOKE_PREVIEW_FPS,
  KARAOKE_PREVIEW_HEIGHT,
  KARAOKE_PREVIEW_WIDTH,
  karaokeCountdownCue,
  karaokeDisplayRows,
  karaokeVisibleLineIndexes,
  manualLineReplayRange,
  manualTransitionReplayRange,
  manualTokenLoopRange,
  moveLineBy,
  moveTokenBy,
  nearestMarkerWithin,
  lyricFitScale,
  pasteTimelineLineAt,
  previewFrameTimeUs,
  setPreviousLineEnd,
  trimTokenEdge,
  setTransitionGap,
  setTransitionStart,
  smoothedPlaybackTimeUs,
  stepPreviewFrameTimeUs,
  timeUsToPixels,
  verifyTiming,
  type ManualLoopRange,
  type SmoothPlaybackClock,
} from './lib/timeline';
import type {
  Artifact,
  BackgroundAssetV1,
  BackgroundPlanV1,
  Capabilities,
  Job,
  LineTiming,
  Project,
  Timeline,
  TimelineIssue,
  WaveformPayload,
} from './lib/types';

interface TimelineResponse {
  timeline: Timeline;
  issues: TimelineIssue[];
}

interface WorkspaceData {
  project: Project;
  timeline: Timeline;
  issues: TimelineIssue[];
  artifacts: Artifact[];
  backgroundPlan: BackgroundPlanV1 | null;
  waveform: WaveformPayload | null;
}

type ImportMode = 'single' | 'batch';
type BatchSongStatus =
  | 'ready'
  | 'uploading'
  | 'queued'
  | 'processing'
  | 'complete'
  | 'failed';

interface BatchSongDraft {
  id: string;
  video: File;
  timeline: File | null;
  title: string;
  artist: string;
  status: BatchSongStatus;
  progress: number;
  message: string;
  projectId?: string;
  error?: string;
}

type AutosaveStatus = 'saved' | 'pending' | 'saving' | 'error';

const STAGE_LABELS = ['Nhập nguồn', 'Tách giọng', 'Căn lời', 'Kiểm duyệt', 'Xuất video'];
const REVIEW_ISSUE_CODES = new Set([
  'LOW_CONFIDENCE',
  'MANUAL_REVIEW',
  'MODEL_DISAGREEMENT',
  'STEM_DISAGREEMENT',
  'FAST_PHRASE',
  'SUSTAIN_UNCERTAIN',
  'ANCHOR_DRIFT',
  'WEAK_VOCAL',
  'SWEEP_MISSING',
  'SWEEP_UNVERIFIED',
  'SWEEP_DISAGREEMENT',
  'VOWEL_SUSTAIN_UNCERTAIN',
  'BEAT_CONFLICT',
  'GRAPHEME_MAPPING_FAILED',
]);

export default function StudioApp() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceData | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [showCreate, setShowCreate] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [deleteCandidateId, setDeleteCandidateId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const jobMonitorRef = useRef<JobMonitor | null>(null);
  const editorMode = !showCreate && Boolean(
    workspace && !['IMPORTED', 'FAILED'].includes(workspace.project.state),
  );

  const refreshProjects = useCallback(async () => {
    const items = await api<Project[]>('/api/projects');
    setProjects(items);
    return items;
  }, []);

  const loadWorkspace = useCallback(async (projectId: string) => {
    const [project, timelineResponse, artifacts] = await Promise.all([
      api<Project>(`/api/projects/${projectId}`),
      api<TimelineResponse>(`/api/projects/${projectId}/timeline`),
      api<Artifact[]>(`/api/projects/${projectId}/artifacts`),
    ]);
    let waveform: WaveformPayload | null = null;
    let backgroundPlan: BackgroundPlanV1 | null = null;
    try {
      waveform = await api<WaveformPayload>(`/api/projects/${projectId}/waveform`);
    } catch {
      // Waveform is created during processing; IMPORTED projects do not have one yet.
    }
    if (project.background_mode === 'custom') {
      try {
        backgroundPlan = await api<BackgroundPlanV1>(`/api/projects/${projectId}/background-plan`);
      } catch {
        // Keep the user's selected replacement visible while an older local API
        // is still running. Never silently fall back to the source video's image.
        backgroundPlan = legacyBackgroundPlan(project);
      }
    }
    setWorkspace({
      project,
      timeline: timelineResponse.timeline,
      issues: timelineResponse.issues,
      artifacts,
      backgroundPlan,
      waveform,
    });
    setError(null);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const [items, caps] = await Promise.all([
          refreshProjects(),
          api<Capabilities>('/api/system/capabilities'),
        ]);
        setCapabilities(caps);
        if (items.length) {
          setSelectedId(items[0].id);
          setShowCreate(false);
          await loadWorkspace(items[0].id);
        }
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Không kết nối được local API.');
      } finally {
        setLoading(false);
      }
    })();
  }, [loadWorkspace, refreshProjects]);

  const watchJob = useCallback(
    (initial: Job) => {
      jobMonitorRef.current?.stop();
      setJob(initial);
      const monitor = new JobMonitor({
        initial,
        eventUrl: `${API_BASE}/api/jobs/${initial.id}/events`,
        fetchJob: () => api<Job>(`/api/jobs/${initial.id}`),
        onUpdate: setJob,
        onTerminal: async (terminal) => {
          if (jobMonitorRef.current === monitor) jobMonitorRef.current = null;
          const results = await Promise.allSettled([
            refreshProjects(),
            loadWorkspace(terminal.project_id),
          ]);
          const failure = results.find((result) => result.status === 'rejected');
          if (failure?.status === 'rejected') {
            setError(
              failure.reason instanceof Error
                ? failure.reason.message
                : 'Job đã xong nhưng chưa tải lại được workspace.',
            );
          }
        },
      });
      jobMonitorRef.current = monitor;
      monitor.start();
    },
    [loadWorkspace, refreshProjects],
  );

  useEffect(() => () => {
    jobMonitorRef.current?.stop();
    jobMonitorRef.current = null;
  }, []);

  const selectProject = (projectId: string) => {
    if (projectId !== selectedId) {
      jobMonitorRef.current?.stop();
      jobMonitorRef.current = null;
      setJob(null);
    }
    setDeleteCandidateId(null);
    setSelectedId(projectId);
    setShowCreate(false);
    setWorkspace(null);
    void loadWorkspace(projectId).catch((cause) =>
      setError(cause instanceof Error ? cause.message : 'Không tải được project.'),
    );
  };

  const deleteProject = async (project: Project) => {
    setDeletingId(project.id);
    setError(null);
    try {
      if (project.id === selectedId) {
        jobMonitorRef.current?.stop();
        jobMonitorRef.current = null;
      }
      await api<{ deleted: boolean; project_id: string }>(`/api/projects/${project.id}`, {
        method: 'DELETE',
      });
      await refreshProjects();
      if (project.id === selectedId) {
        setSelectedId(null);
        setWorkspace(null);
        setJob(null);
        setShowCreate(true);
      }
      setDeleteCandidateId(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Không xoá được dự án.');
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <main className={`app-shell ${editorMode ? 'is-editing' : 'is-create'} ${railCollapsed ? 'is-rail-collapsed' : ''}`}>
      <header className="app-header">
        <div className="header-brand-group">
          <span className="window-controls" aria-hidden="true"><i /><i /><i /></span>
          <button className="brand-button" type="button" onClick={() => setShowCreate(true)}>
            <span className="mini-mark" aria-hidden="true">KS</span>
            <span><small>LOCAL PRODUCTION SUITE</small>Karaoke Studio</span>
          </button>
        </div>
        <div className="header-center">
          <span className="local-dot" />
          <span className="header-project-name">
            {error
              ? 'API cần khởi động'
              : editorMode
              ? workspace?.project.title ?? 'Karaoke Edit'
              : 'Karaoke Studio'}
          </span>
          <small>{error ? 'OFFLINE' : 'LOCAL ENGINE'}</small>
        </div>
        <div className="header-actions">
          <span className="workspace-mode">EDIT</span>
          <button className="new-project-button" type="button" onClick={() => setShowCreate(true)}>
            ＋ Import
          </button>
        </div>
      </header>

      <div className="app-body">
        <aside className="project-rail">
          <div className="rail-heading">
            <p className="rail-label">LIBRARIES / DỰ ÁN</p>
            <button
              className="project-rail-toggle"
              type="button"
              aria-expanded={!railCollapsed}
              aria-label={railCollapsed ? 'Mở danh sách dự án' : 'Thu gọn danh sách dự án'}
              title={railCollapsed ? 'Mở danh sách dự án' : 'Thu gọn danh sách dự án'}
              onClick={() => setRailCollapsed((collapsed) => !collapsed)}
            >
              <span aria-hidden="true">{railCollapsed ? '›' : '‹'}</span>
            </button>
          </div>
          <button
            className={`rail-project new ${showCreate ? 'active' : ''}`}
            type="button"
            onClick={() => setShowCreate(true)}
          >
            <span>+</span><div><strong>Tạo mới</strong><small>VIDEO + TIMELINE</small></div>
          </button>
          <div className="project-list">
            {projects.map((project) => {
              const confirmingDelete = deleteCandidateId === project.id;
              const deleting = deletingId === project.id;
              return (
                <div className={`rail-project-item ${confirmingDelete ? 'confirming-delete' : ''}`} key={project.id}>
                  <button
                    className={`rail-project ${!showCreate && project.id === selectedId ? 'active' : ''}`}
                    type="button"
                    onClick={() => selectProject(project.id)}
                  >
                    <span>{project.title.slice(0, 1).toUpperCase()}</span>
                    <div><strong>{project.title}</strong><small>{stateLabel(project.state)}</small></div>
                  </button>
                  <button
                    className="rail-project-delete"
                    type="button"
                    aria-label={`Xoá dự án ${project.title}`}
                    title={`Xoá dự án ${project.title}`}
                    onClick={() => setDeleteCandidateId(project.id)}
                  >
                    Xoá
                  </button>
                  {confirmingDelete && (
                    <div className="rail-delete-confirm" role="alert">
                      <strong>Xoá dự án này?</strong>
                      <span>Video, stems và bản xuất local sẽ bị xoá.</span>
                      <div>
                        <button disabled={deleting} type="button" onClick={() => setDeleteCandidateId(null)}>Huỷ</button>
                        <button className="confirm-delete" disabled={deleting} type="button" onClick={() => void deleteProject(project)}>{deleting ? 'Đang xoá…' : 'Xoá tất cả'}</button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <div className="rail-footer">
            <span className={capabilities?.ffmpeg ? 'ok' : 'off'} />
            <div><strong>Local Engine</strong><small>{capabilities?.ffmpeg ? 'Sẵn sàng' : 'Chưa kết nối'}</small></div>
          </div>
        </aside>

        <section className="main-surface">
          {error && <div className="error-banner"><strong>Cần chú ý</strong><span>{error}</span></div>}
          {showCreate ? (
            <CreateProject
              capabilities={capabilities}
              busy={loading}
              onCreated={async (project, acceptModelLicense) => {
                await refreshProjects();
                setSelectedId(project.id);
                setShowCreate(false);
                await loadWorkspace(project.id);
                const response = await api<{ job: Job }>(`/api/projects/${project.id}/process`, {
                  method: 'POST',
                  body: JSON.stringify({
                    quality: 'highest',
                    alignment_profile: 'maximum',
                    motion_profile: 'vocal_hybrid',
                    accept_vietnamese_model_license: acceptModelLicense,
                  }),
                });
                watchJob(response.job);
              }}
              onProjectsChanged={async () => {
                await refreshProjects();
              }}
              onOpenProject={selectProject}
              onError={setError}
            />
          ) : !workspace ? (
            <LoadingStudio />
          ) : workspace ? (
            <ProjectWorkspace
              key={`${workspace.project.id}-${workspace.timeline.revision}`}
              data={workspace}
              capabilities={capabilities}
              job={job}
              watchJob={watchJob}
              onReload={() => loadWorkspace(workspace.project.id)}
              onError={setError}
            />
          ) : null}
        </section>
      </div>
    </main>
  );
}

function CreateProject({
  capabilities,
  busy,
  onCreated,
  onProjectsChanged,
  onOpenProject,
  onError,
}: {
  capabilities: Capabilities | null;
  busy: boolean;
  onCreated: (project: Project, acceptModelLicense: boolean) => Promise<void>;
  onProjectsChanged: () => Promise<void>;
  onOpenProject: (projectId: string) => void;
  onError: (message: string | null) => void;
}) {
  const [importMode, setImportMode] = useState<ImportMode>('single');
  const [submitting, setSubmitting] = useState(false);
  const [videoName, setVideoName] = useState('');
  const [timelineFileName, setTimelineFileName] = useState('');
  const [timelineText, setTimelineText] = useState('');
  const [batchVideos, setBatchVideos] = useState<File[]>([]);
  const [batchTimelines, setBatchTimelines] = useState<File[]>([]);
  const [batchSongs, setBatchSongs] = useState<BatchSongDraft[]>([]);
  const [backgroundMode, setBackgroundMode] = useState<'original' | 'custom'>('original');
  const [backgroundFiles, setBackgroundFiles] = useState<File[]>([]);
  const [karaokeFont, setKaraokeFont] = useState<KaraokeFontId>('noto_sans');
  const [karaokeColor, setKaraokeColor] = useState<KaraokeColorId>('yellow');
  const [acceptModelLicense, setAcceptModelLicense] = useState(true);

  const rebuildBatchSongs = (videos: File[], timelines: File[]) => {
    const previous = new Map(batchSongs.map((song) => [song.id, song]));
    const next = pairBatchFiles(videos, timelines).map((pair) => {
      const saved = previous.get(pair.id);
      return {
        id: pair.id,
        video: pair.video,
        timeline: pair.timeline,
        title: saved?.title ?? pair.suggestedTitle,
        artist: saved?.artist ?? '',
        status: 'ready' as const,
        progress: 0,
        message: pair.timeline ? 'Sẵn sàng' : 'Thiếu file timeline',
      };
    });
    setBatchSongs(next);
  };

  const selectBatchVideos = (files: File[]) => {
    if (files.length > MAX_BATCH_SONGS) {
      onError(`Mỗi lượt tối đa ${MAX_BATCH_SONGS} bài để máy chạy ổn định.`);
    } else {
      onError(null);
    }
    const accepted = files.slice(0, MAX_BATCH_SONGS);
    setBatchVideos(accepted);
    rebuildBatchSongs(accepted, batchTimelines);
  };

  const selectBatchTimelines = (files: File[]) => {
    if (files.length > MAX_BATCH_SONGS) {
      onError(`Mỗi lượt tối đa ${MAX_BATCH_SONGS} timeline để máy chạy ổn định.`);
    } else {
      onError(null);
    }
    const accepted = files.slice(0, MAX_BATCH_SONGS);
    setBatchTimelines(accepted);
    rebuildBatchSongs(batchVideos, accepted);
  };

  const updateBatchSong = (songId: string, patch: Partial<BatchSongDraft>) => {
    setBatchSongs((songs) => songs.map((song) => (
      song.id === songId ? { ...song, ...patch } : song
    )));
  };

  const waitForBatchJob = async (initial: Job, songId: string): Promise<Job> => {
    let current = initial;
    let temporaryFailures = 0;
    while (!['COMPLETE', 'FAILED', 'CANCELLED'].includes(current.state)) {
      updateBatchSong(songId, {
        status: 'processing',
        progress: current.progress,
        message: current.message,
      });
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      try {
        current = await api<Job>(`/api/jobs/${initial.id}`);
        temporaryFailures = 0;
      } catch (cause) {
        temporaryFailures += 1;
        if (temporaryFailures >= 20) throw cause;
      }
    }
    return current;
  };

  const submitBatch = async () => {
    if (!batchSongs.length) {
      onError('Hãy chọn ít nhất một video cho lô Karaoke.');
      return;
    }
    const incomplete = batchSongs.filter((song) => !song.timeline || !song.title.trim());
    if (incomplete.length) {
      onError('Mỗi video cần đúng một file timeline và một tên bài hát.');
      return;
    }
    setSubmitting(true);
    onError(null);
    const results = await mapWithConcurrency(
      batchSongs,
      BATCH_PROCESS_CONCURRENCY,
      async (song) => {
        try {
          updateBatchSong(song.id, {
            status: 'uploading',
            progress: 0.03,
            message: 'Đang nhập video và timeline…',
            error: undefined,
          });
          const payload = new FormData();
          payload.set('video', song.video, song.video.name);
          const timeline = song.timeline;
          if (!timeline) throw new Error('Thiếu file timeline.');
          payload.set('lrc', timeline, timeline.name);
          payload.set('title', song.title.trim());
          payload.set('artist', song.artist.trim());
          payload.set('background_mode', 'original');
          payload.set('karaoke_font', karaokeFont);
          payload.set('karaoke_color', karaokeColor);
          const project = await api<Project>('/api/projects', { method: 'POST', body: payload });
          updateBatchSong(song.id, {
            status: 'queued',
            progress: 0.06,
            message: 'Đã nhập · đang xếp hàng phân tích',
            projectId: project.id,
          });
          await onProjectsChanged();
          const response = await api<{ job: Job }>(`/api/projects/${project.id}/process`, {
            method: 'POST',
            body: JSON.stringify({
              quality: 'highest',
              alignment_profile: 'maximum',
              motion_profile: 'vocal_hybrid',
              accept_vietnamese_model_license: acceptModelLicense,
            }),
          });
          const terminal = await waitForBatchJob(response.job, song.id);
          if (terminal.state !== 'COMPLETE') {
            throw new Error(terminal.error || terminal.message || 'Phân tích không hoàn tất.');
          }
          updateBatchSong(song.id, {
            status: 'complete',
            progress: 1,
            message: 'Phân tích hoàn tất · sẵn sàng kiểm duyệt',
            projectId: project.id,
          });
          await onProjectsChanged();
          return project;
        } catch (cause) {
          const message = cause instanceof Error ? cause.message : 'Không thể xử lý bài này.';
          updateBatchSong(song.id, {
            status: 'failed',
            message: 'Cần kiểm tra lại',
            error: message,
          });
          throw cause;
        }
      },
    );
    const failedCount = results.filter((result) => result.status === 'rejected').length;
    if (failedCount) {
      onError(`${batchSongs.length - failedCount}/${batchSongs.length} bài đã hoàn tất; ${failedCount} bài cần kiểm tra lại.`);
    }
    setSubmitting(false);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (importMode === 'batch') {
      await submitBatch();
      return;
    }
    onError(null);
    if (!timelineFileName && !timelineText.trim()) {
      onError('Hãy chọn file timeline hoặc dán nội dung lời có timestamp.');
      return;
    }
    if (timelineFileName && timelineText.trim()) {
      onError('Chỉ dùng một nguồn timeline: file hoặc nội dung dán.');
      return;
    }
    setSubmitting(true);
    try {
      const payload = new FormData(event.currentTarget);
      if (timelineText.trim()) {
        payload.delete('lrc');
        payload.set('timeline_text', timelineText);
      } else {
        payload.delete('timeline_text');
      }
      payload.set('background_mode', backgroundMode);
      payload.set('karaoke_font', karaokeFont);
      payload.set('karaoke_color', karaokeColor);
      const project = await api<Project>('/api/projects', { method: 'POST', body: payload });
      await onCreated(project, acceptModelLicense);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'Không thể nhập project.');
    } finally {
      setSubmitting(false);
    }
  };

  const batchHasStarted = batchSongs.some((song) => song.status !== 'ready');
  const batchIsValid = batchSongs.length > 0 && batchSongs.every(
    (song) => Boolean(song.timeline && song.title.trim()),
  );

  return (
    <div className="create-view">
      <div className="create-copy">
        <div className="import-mode-switch" role="tablist" aria-label="Chọn số lượng bài Karaoke">
          <button className={importMode === 'single' ? 'active' : ''} disabled={submitting} type="button" role="tab" aria-selected={importMode === 'single'} onClick={() => setImportMode('single')}>Một bài</button>
          <button className={importMode === 'batch' ? 'active' : ''} disabled={submitting} type="button" role="tab" aria-selected={importMode === 'batch'} onClick={() => setImportMode('batch')}>Nhiều bài cùng lúc</button>
        </div>
        <span className="gold-chip">{importMode === 'batch' ? 'BATCH STUDIO · TỐI ĐA 12 BÀI' : 'DỰ ÁN MỚI · TIẾNG VIỆT'}</span>
        <h1>{importMode === 'batch' ? 'Làm nhiều bài Karaoke trong cùng một lượt.' : 'Biến video bài hát thành Karaoke chính xác đến từng frame.'}</h1>
        <p>
          {importMode === 'batch'
            ? 'Tự ghép video với timeline, chạy hai bài song song và xếp hàng phần còn lại để máy luôn ổn định.'
            : 'Tách giọng, căn từng âm tiết và đưa mọi timing chưa chắc chắn vào hàng kiểm duyệt trước khi xuất Final.'}
        </p>
        <div className="default-spec">
          <span>{importMode === 'batch' ? 'BATCH' : 'OUTPUT'}</span><strong>{importMode === 'batch' ? '2 bài xử lý song song' : '1080p · 60fps tương thích'}</strong>
          <small>{importMode === 'batch' ? 'Các bài còn lại tự xếp hàng · giữ video gốc' : 'Hai dòng cố định · quét liên tục · AAC 48 kHz'}</small>
        </div>
      </div>

      <form className={`create-form ${importMode === 'batch' ? 'is-batch' : ''}`} onSubmit={submit}>
        <div className="form-heading"><span>01</span><div><small>{importMode === 'batch' ? 'NHẬP HÀNG LOẠT' : 'NHẬP NGUỒN'}</small><h2>{importMode === 'batch' ? 'Chuẩn bị nhiều bản Karaoke' : 'Bắt đầu một bản Karaoke'}</h2></div></div>
        {importMode === 'single' ? (
          <>
            <label className={`file-drop ${videoName ? 'selected' : ''}`}>
              <input
                required
                type="file"
                name="video"
                accept="video/mp4,video/quicktime,video/x-matroska,video/webm"
                onChange={(event) => setVideoName(event.target.files?.[0]?.name ?? '')}
              />
              <span className="upload-symbol">↑</span>
              <strong>{videoName || 'Chọn hoặc thả video MP4'}</strong>
              <small>Nguồn được checksum và giữ nguyên, không rời khỏi máy.</small>
            </label>
            <section className="timeline-source" aria-labelledby="timeline-source-title">
              <div className="timeline-source-heading">
                <span>TXT</span>
                <div>
                  <strong id="timeline-source-title">Nguồn timeline lời bài hát</strong>
                  <small>LRC · SRT · VTT · TXT timestamp</small>
                </div>
                <b>CHỌN 1 NGUỒN</b>
              </div>
              <label className={`timeline-file ${timelineFileName ? 'selected' : ''}`}>
                <input
                  type="file"
                  name="lrc"
                  accept=".lrc,.srt,.vtt,.txt,text/plain,application/x-subrip,text/vtt"
                  onChange={(event) => setTimelineFileName(event.target.files?.[0]?.name ?? '')}
                />
                <span>{timelineFileName || 'Chọn hoặc thả file LRC / SRT / VTT / TXT'}</span>
                <b>{timelineFileName ? 'Đổi file' : 'Mở file'}</b>
              </label>
              <div className="timeline-source-divider"><span>HOẶC DÁN TRỰC TIẾP</span></div>
              <label className="timeline-paste">
                <span>Dán nguyên văn timestamp và lời bài hát</span>
                <textarea
                  name="timeline_text"
                  value={timelineText}
                  onChange={(event) => setTimelineText(event.target.value)}
                  placeholder={'[00:12.50] Lời bài hát\n[00:18.20] Dòng tiếp theo\n\nHoặc dán nguyên khối SRT / WebVTT'}
                  rows={5}
                  spellCheck={false}
                />
              </label>
              <p>Hệ thống giữ nguyên lời bạn nhập; AI chỉ dùng âm thanh để căn timing.</p>
            </section>
            <section className="song-identity" aria-label="Thông tin hiển thị trên video Karaoke">
              <div className="song-identity-heading">
                <div><strong>Thông tin bài hát</strong><small>Hiện trong 5 giây đầu video</small></div>
                <span>TỰ LẤY TỪ TÊN FILE NẾU ĐỂ TRỐNG</span>
              </div>
              <div className="form-row song-identity-fields">
                <label><span>Tên bài hát</span><input name="title" placeholder="Nhập tên bài hát" /></label>
                <label><span>Tên ca sĩ / nguồn</span><input name="artist" placeholder="Nhập tên ca sĩ hoặc nguồn phát hành" /></label>
              </div>
            </section>
          </>
        ) : (
          <>
            <div className="batch-pickers">
              <label className={`batch-file-picker ${batchVideos.length ? 'selected' : ''}`}>
                <input
                  multiple
                  type="file"
                  accept="video/mp4,video/quicktime,video/x-matroska,video/webm"
                  onChange={(event) => selectBatchVideos(Array.from(event.target.files ?? []))}
                />
                <span>MP4</span>
                <div><strong>{batchVideos.length ? `${batchVideos.length} video đã chọn` : 'Chọn nhiều video'}</strong><small>MP4 · MOV · MKV · WEBM</small></div>
                <b>{batchVideos.length ? 'Chọn lại' : 'Mở video'}</b>
              </label>
              <label className={`batch-file-picker ${batchTimelines.length ? 'selected' : ''}`}>
                <input
                  multiple
                  type="file"
                  accept=".lrc,.srt,.vtt,.txt,text/plain,application/x-subrip,text/vtt"
                  onChange={(event) => selectBatchTimelines(Array.from(event.target.files ?? []))}
                />
                <span>TXT</span>
                <div><strong>{batchTimelines.length ? `${batchTimelines.length} timeline đã chọn` : 'Chọn nhiều timeline'}</strong><small>LRC · SRT · VTT · TXT</small></div>
                <b>{batchTimelines.length ? 'Chọn lại' : 'Mở lời'}</b>
              </label>
            </div>
            {batchSongs.length ? (
              <section className="batch-song-list" aria-label="Danh sách bài Karaoke hàng loạt">
                <header><div><strong>{batchSongs.length} BÀI TRONG LÔ</strong><small>Tự ghép theo tên file; nếu tên khác nhau sẽ ghép theo thứ tự chọn.</small></div><b>{batchSongs.filter((song) => song.status === 'complete').length}/{batchSongs.length} XONG</b></header>
                {batchSongs.map((song, index) => (
                  <article className={`batch-song-row status-${song.status}`} key={song.id} style={{ '--batch-progress': `${Math.round(song.progress * 100)}%` } as CSSProperties}>
                    <div className="batch-song-index">{String(index + 1).padStart(2, '0')}</div>
                    <div className="batch-song-source">
                      <strong title={song.video.name}>{song.video.name}</strong>
                      <small className={song.timeline ? '' : 'missing'}>{song.timeline?.name ?? 'Chưa ghép được timeline'}</small>
                    </div>
                    <label><span>Tên bài hát</span><input aria-label={`Tên bài hát ${index + 1}`} disabled={submitting} type="text" value={song.title} onChange={(event) => updateBatchSong(song.id, { title: event.target.value })} /></label>
                    <label><span>Ca sĩ / nguồn</span><input aria-label={`Ca sĩ hoặc nguồn ${index + 1}`} disabled={submitting} type="text" value={song.artist} placeholder="Không bắt buộc" onChange={(event) => updateBatchSong(song.id, { artist: event.target.value })} /></label>
                    <div className="batch-song-state">
                      <strong>{batchStatusLabel(song.status)}</strong>
                      <small>{song.error ?? song.message}</small>
                      {song.projectId && ['complete', 'failed'].includes(song.status) && <button type="button" onClick={() => onOpenProject(song.projectId!)}>Mở dự án</button>}
                    </div>
                  </article>
                ))}
              </section>
            ) : (
              <p className="batch-empty-note">Chọn video và timeline; ứng dụng sẽ tạo một hàng cho từng bài để bạn kiểm tra trước khi chạy.</p>
            )}
          </>
        )}
        <KaraokeFontPicker value={karaokeFont} onChange={setKaraokeFont} />
        <KaraokeColorPicker value={karaokeColor} onChange={setKaraokeColor} />
        {importMode === 'single' ? (
          <>
            <fieldset className="background-choice">
              <legend>Hình ảnh đầu ra</legend>
              <label><input type="radio" checked={backgroundMode === 'original'} onChange={() => setBackgroundMode('original')} /><span><strong>Giữ video gốc</strong><small>Thay audio bằng instrumental</small></span></label>
              <label><input type="radio" checked={backgroundMode === 'custom'} onChange={() => setBackgroundMode('custom')} /><span><strong>Nền thay thế tự động</strong><small>Một hoặc nhiều ảnh/video</small></span></label>
            </fieldset>
            {backgroundMode === 'custom' && (
              <div className={`background-library ${backgroundFiles.length ? 'selected' : ''}`}>
                <label className="background-upload">
                  <span className="background-upload-icon">＋</span>
                  <span>
                    <strong>{backgroundFiles.length ? `${backgroundFiles.length} cảnh đã chọn` : 'Chọn nhiều ảnh hoặc video'}</strong>
                    <small>Giữ ⌘ hoặc Shift để chọn nhiều file cùng lúc · tối đa 64 cảnh</small>
                  </span>
                  <b>{backgroundFiles.length ? 'Chọn lại' : 'Mở thư viện'}</b>
                  <input
                    required
                    multiple
                    type="file"
                    name="background"
                    accept="video/*,image/jpeg,image/png,image/webp,image/bmp,image/tiff"
                    onChange={(event) => setBackgroundFiles(Array.from(event.target.files ?? []))}
                  />
                </label>
                {backgroundFiles.length > 0 && (
                  <div className="background-scene-list" aria-label="Danh sách cảnh nền đã chọn">
                    {backgroundFiles.map((file, index) => (
                      <span key={`${file.name}-${file.lastModified}-${index}`} title={file.name}>
                        <i>{String(index + 1).padStart(2, '0')}</i>
                        <b>{file.type.startsWith('video/') ? 'VIDEO' : 'ẢNH'}</b>
                        <em>{file.name}</em>
                      </span>
                    ))}
                  </div>
                )}
                <p className="background-auto-note"><i>✦</i><span><strong>CHUYỂN CẢNH ĐIỆN ẢNH</strong> Hệ thống ưu tiên khoảng nghỉ/ranh giới câu, dissolve mềm đến 1,8 giây và tạo chuyển động Ken Burns nhẹ để nền luôn sống nhưng không làm phân tâm lời Karaoke.</span></p>
              </div>
            )}
          </>
        ) : (
          <div className="batch-output-policy"><span>✓</span><div><strong>Mỗi bài giữ video gốc</strong><small>Audio sẽ được thay bằng instrumental sau khi bạn chọn bản tách giọng. Nền riêng có thể chỉnh trong từng project.</small></div></div>
        )}
        <label className="license-check"><input type="checkbox" checked={acceptModelLicense} onChange={(event) => setAcceptModelLicense(event.target.checked)} /><span><strong>Dùng Karaoke AI Maximum Accuracy</strong><small>Hai model × hai vocal stem, CC-BY-NC-4.0 cho mục đích phi thương mại. Nếu bỏ chọn, app vẫn chạy energy fallback nhưng không tự Verified.</small></span></label>
        <button className="primary-action" disabled={submitting || busy || !capabilities?.ffmpeg || (importMode === 'batch' && (!batchIsValid || batchHasStarted))} type="submit">
          {submitting
            ? importMode === 'batch' ? `Đang xử lý ${batchSongs.length} bài · tối đa 2 bài song song…` : 'Đang nhập và khởi động phân tích…'
            : importMode === 'batch' ? batchHasStarted ? 'Lô này đã chạy · chọn file mới để tạo lô khác' : `Tạo và xử lý ${batchSongs.length || 0} bài →`
              : 'Tạo project và tự phân tích hết bài →'}
        </button>
        <p className="privacy-note"><span /> Server chỉ lắng nghe tại 127.0.0.1 · không telemetry · không cloud upload</p>
      </form>
    </div>
  );
}

function ProjectWorkspace({
  data,
  capabilities,
  job,
  watchJob,
  onReload,
  onError,
}: {
  data: WorkspaceData;
  capabilities: Capabilities | null;
  job: Job | null;
  watchJob: (job: Job) => void;
  onReload: () => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const { project } = data;
  const [acceptLicense, setAcceptLicense] = useState(false);
  const processing = job && ['PENDING', 'RUNNING'].includes(job.state);

  const startProcess = async () => {
    try {
      onError(null);
      const response = await api<{ job: Job }>(`/api/projects/${project.id}/process`, {
        method: 'POST',
        body: JSON.stringify({
          quality: 'highest',
          alignment_profile: 'maximum',
          motion_profile: 'vocal_hybrid',
          accept_vietnamese_model_license: acceptLicense,
        }),
      });
      watchJob(response.job);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'Không khởi động được pipeline.');
    }
  };

  return (
    <div className={`workspace-view ${['IMPORTED', 'FAILED'].includes(project.state) ? 'analysis-workspace' : 'edit-workspace'}`}>
      <div className="editor-project-strip">
        <div className="project-titlebar">
          <div><span className="section-kicker">PROJECT / {project.id.slice(-6).toUpperCase()}</span><h1>{project.title}</h1><p>{project.artist || 'Chưa có tên ca sĩ'} · Nguồn {project.width}×{project.height} · {project.fps} fps</p></div>
          <div className="title-actions">
            {!['IMPORTED', 'FAILED'].includes(project.state) && (
              <>
                <label className="compact-license"><input type="checkbox" checked={acceptLicense} onChange={(event) => setAcceptLicense(event.target.checked)} /><span>Maximum Accuracy<br /><small>2 model × 2 stem</small></span></label>
                <button className="reprocess-button" disabled={Boolean(processing)} type="button" onClick={startProcess}>↻ Phân tích lại</button>
              </>
            )}
            <div className={`state-pill state-${project.state.toLowerCase()}`}><i />{stateLabel(project.state)}</div>
          </div>
        </div>
        <PipelineStatus project={project} job={job} />
      </div>

      {['IMPORTED', 'FAILED'].includes(project.state) ? (
        <section className="analysis-launch">
          <div className="launch-copy"><span className="section-kicker">02 / PHÂN TÍCH</span><h2>Tách giọng và căn lời tiếng Việt</h2><p>Chế độ chất lượng cao nhất sẽ thử mọi engine có trên máy. Nếu chưa có model AI, hệ thống vẫn tạo fallback nhưng bắt buộc nghe xác nhận.</p></div>
          <div className="engine-grid">
            <EngineStatus label="BS PolarFormer FP32" ready={Boolean(capabilities?.polarformer_fp32)} detail={capabilities?.polarformer_model_downloaded ? '201 MiB · đã xác minh local' : 'Tự tải 201 MiB lần đầu'} />
            <EngineStatus label="Mel-Band RoFormer" ready={Boolean(capabilities?.audio_separator)} detail="CoreML / Audio Separator" />
            <EngineStatus label="HTDemucs FT" ready={Boolean(capabilities?.demucs)} detail="Ứng viên A/B dự phòng" />
            <EngineStatus label="Vietnamese Lyric CTC" ready={Boolean(capabilities?.vietnamese_ctc)} detail="Model chuyên lời hát" />
            <EngineStatus label="Energy-aware" ready detail="Fallback luôn khả dụng" />
          </div>
          <label className="license-check"><input type="checkbox" checked={acceptLicense} onChange={(event) => setAcceptLicense(event.target.checked)} /><span><strong>Cho phép tải/dùng bộ Karaoke AI</strong><small>Model CC-BY-NC-4.0, chỉ dùng phi thương mại. Chấp nhận một lần sẽ được ghi nhớ cho project này.</small></span></label>
          {project.error && <div className="inline-error">{project.error}</div>}
          <button className="primary-action wide" disabled={Boolean(processing)} type="button" onClick={startProcess}>{processing ? 'Pipeline đang chạy…' : 'Chạy phân tích chất lượng cao →'}</button>
        </section>
      ) : (
        <ReviewWorkspace key={`${project.id}:${data.timeline.revision}`} data={data} onReload={onReload} onError={onError} watchJob={watchJob} job={job} />
      )}
    </div>
  );
}

function ReviewWorkspace({ data, onReload, onError, watchJob, job }: { data: WorkspaceData; onReload: () => Promise<void>; onError: (message: string | null) => void; watchJob: (job: Job) => void; job: Job | null }) {
  const selectedInstrumental = selectedInstrumentalArtifact(
    data.project.selected_instrumental,
    data.artifacts,
  );
  const [timeline, setTimeline] = useState(data.timeline);
  const [issues, setIssues] = useState(data.issues);
  const initialReviewLineId = data.issues.find((issue) => REVIEW_ISSUE_CODES.has(issue.code))?.line_id;
  const [activeLineId, setActiveLineId] = useState(initialReviewLineId ?? data.timeline.lines[0]?.id ?? '');
  const [reviewOnly, setReviewOnly] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [history, setHistory] = useState<Timeline[]>([]);
  const [future, setFuture] = useState<Timeline[]>([]);
  const [currentTimeUs, setCurrentTimeUs] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [viewerVolume, setViewerVolume] = useState(1);
  const [previewAudioMode, setPreviewAudioMode] = useState<PreviewAudioMode>(() => (
    preferredPreviewAudioMode()
  ));
  const [preset, setPreset] = useState<ExportPreset>(DEFAULT_EXPORT_PRESET);
  const [inspectorTab, setInspectorTab] = useState<'review' | 'audio' | 'text' | 'export'>('review');
  const [saving, setSaving] = useState(false);
  const [autosaveError, setAutosaveError] = useState<string | null>(null);
  const [selectedTokenId, setSelectedTokenId] = useState<string | null>(null);
  const [loopRange, setLoopRange] = useState<ManualLoopRange | null>(null);
  const loopRangeRef = useRef<ManualLoopRange | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const instrumentalAudioRef = useRef<HTMLAudioElement>(null);
  const timelineRef = useRef(data.timeline);
  const dirtyRef = useRef(false);
  const editVersionRef = useRef(0);
  const persistedRevisionRef = useRef(data.timeline.revision);
  const autosaveTimerRef = useRef<number | null>(null);
  const autosaveInFlightRef = useRef(false);
  const autosaveQueuedRef = useRef(false);
  const flushAutosaveRef = useRef<() => Promise<boolean>>(async () => true);
  const mountedRef = useRef(true);
  const activeLine = timeline.lines.find((line) => line.id === activeLineId) ?? timeline.lines[0];
  const proxy = data.artifacts.find((artifact) => artifact.id === 'work/proxy.mp4');
  const videoUrl = proxy ? assetUrl(proxy.url) : assetUrl(`/api/projects/${data.project.id}/files/source/${encodeURIComponent(data.project.source_name)}`);
  const instrumentalUrl = selectedInstrumental ? assetUrl(selectedInstrumental.url) : null;
  const usingInstrumentalPreview = previewAudioMode === 'instrumental' && Boolean(instrumentalUrl);
  const selectedInstrumentalName = data.project.selected_instrumental
    ? data.waveform?.candidates[data.project.selected_instrumental]?.label
      ?? data.project.selected_instrumental
    : 'Chưa có instrumental';
  const previewWaveform = previewWaveformFor(
    usingInstrumentalPreview ? 'instrumental' : 'original',
    data.project.selected_instrumental,
    data.waveform,
  );
  const activeLineIndex = timeline.lines.findIndex((line) => line.id === activeLine?.id);
  const previousLine = activeLineIndex > 0 ? timeline.lines[activeLineIndex - 1] : null;
  const nextLine = activeLineIndex >= 0 ? timeline.lines[activeLineIndex + 1] ?? null : null;
  const autosaveStatus: AutosaveStatus = autosaveError
    ? 'error'
    : saving
    ? 'saving'
    : dirty
    ? 'pending'
    : 'saved';

  const scheduleAutosave = useCallback((delayMs = 100) => {
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
    }
    autosaveTimerRef.current = window.setTimeout(() => {
      autosaveTimerRef.current = null;
      void flushAutosaveRef.current();
    }, delayMs);
  }, []);

  const stageTimeline = (next: Timeline, saveImmediately = true) => {
    if (next === timelineRef.current) return;
    timelineRef.current = next;
    editVersionRef.current += 1;
    dirtyRef.current = true;
    setTimeline(next);
    setDirty(true);
    setAutosaveError(null);
    if (saveImmediately) scheduleAutosave();
  };

  const flushAutosave = useCallback(async (): Promise<boolean> => {
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    if (autosaveInFlightRef.current) {
      autosaveQueuedRef.current = true;
      return false;
    }
    if (!dirtyRef.current) return true;

    autosaveInFlightRef.current = true;
    autosaveQueuedRef.current = false;
    const savedEditVersion = editVersionRef.current;
    const expectedRevision = persistedRevisionRef.current;
    const snapshot = structuredClone(timelineRef.current);
    snapshot.revision = expectedRevision;
    if (mountedRef.current) {
      setSaving(true);
      setAutosaveError(null);
    }

    let shouldSaveAgain = false;
    let saved = false;
    try {
      const response = await api<TimelineResponse>(`/api/projects/${data.project.id}/timeline`, {
        method: 'PATCH',
        body: JSON.stringify({ expected_revision: expectedRevision, timeline: snapshot }),
      });
      persistedRevisionRef.current = response.timeline.revision;
      if (editVersionRef.current === savedEditVersion) {
        timelineRef.current = response.timeline;
        dirtyRef.current = false;
        if (mountedRef.current) {
          setTimeline(response.timeline);
          setIssues(response.issues);
          setDirty(false);
          setAutosaveError(null);
          onError(null);
        }
        saved = true;
      } else {
        const latest = { ...timelineRef.current, revision: response.timeline.revision };
        timelineRef.current = latest;
        if (mountedRef.current) setTimeline(latest);
        shouldSaveAgain = true;
      }
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        try {
          const current = await api<TimelineResponse>(`/api/projects/${data.project.id}/timeline`);
          persistedRevisionRef.current = current.timeline.revision;
          const latest = { ...timelineRef.current, revision: current.timeline.revision };
          timelineRef.current = latest;
          if (mountedRef.current) setTimeline(latest);
          shouldSaveAgain = true;
        } catch (reloadCause) {
          const message = reloadCause instanceof Error
            ? reloadCause.message
            : 'Không đọc được revision mới nhất để tự lưu.';
          if (mountedRef.current) {
            setAutosaveError(message);
            onError(message);
          }
        }
      } else {
        const message = cause instanceof Error ? cause.message : 'Không tự lưu được timeline.';
        if (mountedRef.current) {
          setAutosaveError(message);
          onError(message);
        }
      }
    } finally {
      autosaveInFlightRef.current = false;
      if (mountedRef.current) setSaving(false);
      if (autosaveQueuedRef.current) shouldSaveAgain = true;
      autosaveQueuedRef.current = false;
      if (shouldSaveAgain && dirtyRef.current && mountedRef.current) scheduleAutosave(80);
    }
    return saved;
  }, [data.project.id, onError, scheduleAutosave]);

  useEffect(() => {
    flushAutosaveRef.current = flushAutosave;
  }, [flushAutosave]);

  const alignInstrumentalToVideo = useCallback((force = false) => {
    const video = videoRef.current;
    const audio = instrumentalAudioRef.current;
    if (!video || !audio) return;
    audio.playbackRate = video.playbackRate;
    if (force || previewAudioNeedsSync(video.currentTime, audio.currentTime)) {
      try {
        audio.currentTime = video.currentTime;
      } catch {
        // Metadata may still be loading; onLoadedMetadata aligns it again.
      }
    }
  }, []);

  const pauseViewer = useCallback(() => {
    videoRef.current?.pause();
    instrumentalAudioRef.current?.pause();
  }, []);

  const playViewer = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    document.querySelectorAll<HTMLAudioElement>('audio').forEach((audio) => {
      if (usingInstrumentalPreview && audio === instrumentalAudioRef.current) return;
      audio.pause();
    });
    video.volume = viewerVolume;
    if (!usingInstrumentalPreview) {
      instrumentalAudioRef.current?.pause();
      video.muted = false;
      void video.play().catch(() => undefined);
      return;
    }
    const audio = instrumentalAudioRef.current;
    if (!audio) return;
    video.muted = true;
    audio.volume = viewerVolume;
    alignInstrumentalToVideo(true);
    const audioPlayback = audio.play();
    const videoPlayback = video.play();
    void Promise.all([audioPlayback, videoPlayback]).catch(() => {
      audio.pause();
      video.pause();
    });
  }, [alignInstrumentalToVideo, usingInstrumentalPreview, viewerVolume]);

  const choosePreviewAudioMode = (mode: PreviewAudioMode) => {
    if (mode === 'instrumental' && !instrumentalUrl) return;
    setPreviewAudioMode(mode);
    const video = videoRef.current;
    const audio = instrumentalAudioRef.current;
    if (!video) return;
    if (mode === 'original') {
      audio?.pause();
      video.muted = false;
      video.volume = viewerVolume;
      return;
    }
    video.muted = true;
    if (!audio) return;
    audio.volume = viewerVolume;
    alignInstrumentalToVideo(true);
    if (!video.paused) {
      void audio.play().catch(() => video.pause());
    }
  };

  useEffect(() => {
    const video = videoRef.current;
    const audio = instrumentalAudioRef.current;
    if (!video) return;
    video.volume = viewerVolume;
    if (audio) audio.volume = viewerVolume;
    video.muted = usingInstrumentalPreview;
    if (!usingInstrumentalPreview) {
      audio?.pause();
      return;
    }
    alignInstrumentalToVideo(true);
    if (!video.paused && audio) {
      void audio.play().catch(() => video.pause());
    }
  }, [alignInstrumentalToVideo, instrumentalUrl, usingInstrumentalPreview, viewerVolume]);

  useEffect(() => {
    if (!usingInstrumentalPreview || !isPlaying) return;
    let frameId = 0;
    const keepAudioLocked = () => {
      alignInstrumentalToVideo();
      frameId = window.requestAnimationFrame(keepAudioLocked);
    };
    frameId = window.requestAnimationFrame(keepAudioLocked);
    return () => window.cancelAnimationFrame(frameId);
  }, [alignInstrumentalToVideo, isPlaying, usingInstrumentalPreview]);

  useEffect(() => () => {
    mountedRef.current = false;
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    if (dirtyRef.current && !autosaveInFlightRef.current) {
      void flushAutosaveRef.current();
    }
  }, []);

  const updateLoopRange = (range: ManualLoopRange | null) => {
    loopRangeRef.current = range;
    setLoopRange(range);
  };

  const startAudition = (range: ManualLoopRange) => {
    updateLoopRange(range);
    seek(range.startUs);
    playViewer();
  };

  const silenceManualDrag = () => {
    updateLoopRange(null);
    document.querySelectorAll<HTMLMediaElement>('audio, video').forEach((media) => {
      media.pause();
    });
  };

  const applyTimeline = (next: Timeline) => {
    setHistory((items) => [...items, timelineRef.current].slice(-50));
    setFuture([]);
    stageTimeline(next);
  };

  const selectKaraokeFont = (fontId: KaraokeFontId) => {
    if (karaokeFontId(timelineRef.current.metadata) === fontId) return;
    const next = structuredClone(timelineRef.current);
    next.metadata = { ...next.metadata, karaoke_font: fontId };
    applyTimeline(next);
  };

  const selectKaraokeColor = (colorId: KaraokeColorId) => {
    if (karaokeColorId(timelineRef.current.metadata) === colorId) return;
    const next = structuredClone(timelineRef.current);
    next.metadata = { ...next.metadata, karaoke_color: colorId };
    applyTimeline(next);
  };

  const verifyLine = () => {
    if (!activeLine) return;
    applyTimeline(verifyTiming(timeline, activeLine.id));
  };

  const toggleLock = () => {
    if (!activeLine) return;
    const next = structuredClone(timeline);
    const line = next.lines.find((candidate) => candidate.id === activeLine.id)!;
    line.locked = !line.locked;
    line.tokens.forEach((token) => { token.locked = line.locked; });
    applyTimeline(next);
  };

  const undo = () => {
    const previous = history.at(-1);
    if (!previous) return;
    setFuture((items) => [timelineRef.current, ...items].slice(0, 50));
    stageTimeline(previous);
    setHistory((items) => items.slice(0, -1));
  };

  const redo = () => {
    const next = future[0];
    if (!next) return;
    setHistory((items) => [...items, timelineRef.current].slice(-50));
    stageTimeline(next);
    setFuture((items) => items.slice(1));
  };

  const seek = (us: number) => {
    setCurrentTimeUs(us);
    if (videoRef.current) videoRef.current.currentTime = us / 1_000_000;
    if (usingInstrumentalPreview && instrumentalAudioRef.current) {
      try {
        instrumentalAudioRef.current.currentTime = us / 1_000_000;
      } catch {
        // Metadata may still be loading; video seek events align it again.
      }
    }
  };

  const togglePlayback = () => {
    const player = videoRef.current;
    if (!player) return;
    if (player.paused) playViewer();
    else pauseViewer();
  };

  const stepViewerFrame = (direction: -1 | 1) => {
    updateLoopRange(null);
    pauseViewer();
    seek(Math.min(
      timeline.duration_us,
      stepPreviewFrameTimeUs(currentTimeUs, direction),
    ));
  };

  const setVolume = (value: number) => {
    const next = Math.max(0, Math.min(1, value));
    setViewerVolume(next);
    if (videoRef.current) videoRef.current.volume = next;
    if (instrumentalAudioRef.current) instrumentalAudioRef.current.volume = next;
  };

  const selectToken = (tokenId: string, lineId = activeLine?.id) => {
    const line = timeline.lines.find((candidate) => candidate.id === lineId);
    if (!line) return;
    const token = line.tokens.find((candidate) => candidate.id === tokenId);
    if (!token) return;
    if (line.id !== activeLineId) {
      setActiveLineId(line.id);
    }
    setSelectedTokenId(tokenId);
    const range = manualTokenLoopRange(timeline, line.id, tokenId);
    if (!range) return;
    startAudition(range);
  };

  const replayActiveLine = () => {
    if (!activeLine) return;
    const range = manualLineReplayRange(timeline, activeLine.id);
    if (!range) return;
    startAudition(range);
  };

  const auditionPreviousTransition = () => {
    if (!activeLine || !previousLine) return;
    setSelectedTokenId(null);
    const range = manualTransitionReplayRange(timeline, previousLine.id, activeLine.id);
    if (range) startAudition(range);
  };

  const auditionNextTransition = () => {
    if (!activeLine || !nextLine) return;
    setSelectedTokenId(null);
    const range = manualTransitionReplayRange(timeline, activeLine.id, nextLine.id);
    if (range) startAudition(range);
  };

  const continueListening = () => {
    updateLoopRange(null);
    const player = videoRef.current;
    if (!player) return;
    setCurrentTimeUs(Math.round(player.currentTime * 1_000_000));
    playViewer();
  };

  const refreshTransitionLoop = (next: Timeline) => {
    const updatedLine = next.lines.find((line) => line.id === activeLine?.id);
    const firstToken = updatedLine?.tokens[0];
    if (!updatedLine || !firstToken) return;
    setSelectedTokenId(firstToken.id);
    const range = manualTokenLoopRange(next, updatedLine.id, firstToken.id);
    if (range) startAudition(range);
  };

  const applyTransitionGap = (gapUs: number) => {
    if (!activeLine || !previousLine) return;
    const next = setTransitionGap(timeline, activeLine.id, gapUs);
    applyTimeline(next);
    refreshTransitionLoop(next);
  };

  const placePreviousEndAtPlayhead = () => {
    if (!activeLine || !previousLine) return;
    const next = setPreviousLineEnd(timeline, activeLine.id, currentTimeUs);
    applyTimeline(next);
    refreshTransitionLoop(next);
  };

  const placeCurrentStartAtPlayhead = () => {
    if (!activeLine || !previousLine) return;
    const next = setTransitionStart(timeline, activeLine.id, currentTimeUs);
    applyTimeline(next);
    refreshTransitionLoop(next);
  };

  const markVerified = async () => {
    if (dirty || saving) {
      onError('Timeline đang tự lưu. Hãy chờ trạng thái “Đã tự lưu” rồi đánh dấu Verified.');
      return;
    }
    try {
      await api<Project>(`/api/projects/${data.project.id}/verify`, { method: 'POST', body: '{}' });
      await onReload();
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'Project chưa đủ điều kiện Verified.');
    }
  };

  const ensureTimelinePersisted = async (): Promise<number> => {
    const deadline = performance.now() + 5_000;
    while (dirtyRef.current || autosaveInFlightRef.current) {
      if (!autosaveInFlightRef.current && dirtyRef.current) {
        await flushAutosaveRef.current();
      } else {
        await new Promise((resolve) => window.setTimeout(resolve, 40));
      }
      if (performance.now() >= deadline) {
        throw new Error('Timeline chưa tự lưu xong. Hãy thử xuất lại sau vài giây.');
      }
    }
    return persistedRevisionRef.current;
  };

  const render = async (mode: 'draft' | 'final') => {
    try {
      onError(null);
      const expectedTimelineRevision = await ensureTimelinePersisted();
      const response = await api<{ job: Job }>(`/api/projects/${data.project.id}/renders`, {
        method: 'POST',
        body: JSON.stringify({
          mode,
          preset,
          countdown: true,
          expected_timeline_revision: expectedTimelineRevision,
          expected_instrumental_id: mode === 'final' ? data.project.selected_instrumental : null,
        }),
      });
      watchJob(response.job);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'Không khởi động được render.');
    }
  };

  const reviewIssues = issues.filter((issue) => REVIEW_ISSUE_CODES.has(issue.code));
  const lowConfidence = new Set(
    reviewIssues.map((issue) => issue.token_id ?? issue.line_id).filter(Boolean),
  ).size;
  const reviewLineIds = new Set(
    reviewIssues.map((issue) => issue.line_id).filter(Boolean),
  );
  const reviewLineCount = reviewLineIds.size;
  const visibleLines = reviewOnly
    ? timeline.lines.filter((line) => reviewLineIds.has(line.id))
    : timeline.lines;
  const rendering = job && ['PENDING', 'RUNNING'].includes(job.state);
  const transitionControls = activeLine ? (
    <div className="transition-inspector">
      <div className="transition-heading">
        <span>CHUYỂN TIẾP CÂU</span>
        <small>Hai nút ngoài luôn phát trọn hai câu, không phụ thuộc từ đang chọn</small>
      </div>
      <div className="transition-flow">
        <article className={!previousLine ? 'missing' : ''}>
          <small>CÂU TRƯỚC</small>
          <strong>{previousLine?.text ?? 'Đầu bài'}</strong>
        </article>
        <div className={`transition-gap ${previousLine && activeLine.start_us < previousLine.end_us ? 'overlap' : ''}`}>
          <b>{previousLine ? transitionGapLabel(activeLine.start_us - previousLine.end_us) : '—'}</b>
          <span>→</span>
        </div>
        <article className="current">
          <small>CÂU ĐANG CHỈNH</small>
          <strong>{activeLine.text}</strong>
        </article>
        <div className={`transition-gap ${nextLine && nextLine.start_us < activeLine.end_us ? 'overlap' : ''}`}>
          <b>{nextLine ? transitionGapLabel(nextLine.start_us - activeLine.end_us) : '—'}</b>
          <span>→</span>
        </div>
        <article className={!nextLine ? 'missing' : ''}>
          <small>CÂU SAU</small>
          <strong>{nextLine?.text ?? 'Cuối bài'}</strong>
        </article>
      </div>
      <div className="transition-actions">
        <button disabled={!previousLine} type="button" onClick={auditionPreviousTransition}>▶ Nghe TRỌN câu trước → câu này</button>
        <button className="replay-line" type="button" onClick={replayActiveLine}>↻ Nghe lại nguyên câu đang chỉnh</button>
        <button disabled={!nextLine} type="button" onClick={auditionNextTransition}>▶ Nghe TRỌN câu này → câu sau</button>
        <button className="continue-listening" type="button" onClick={continueListening}>▶ Tiếp tục nghe từ đây</button>
      </div>
      {previousLine && (
        <div className={`manual-gap-editor ${activeLine.start_us < previousLine.end_us ? 'overlap' : ''}`}>
          <div>
            <strong>{activeLine.start_us < previousLine.end_us
              ? `ĐANG CHỒNG ${Math.round((previousLine.end_us - activeLine.start_us) / 1_000)} ms`
              : `KHOẢNG NGHỈ ${Math.round((activeLine.start_us - previousLine.end_us) / 1_000)} ms`}</strong>
            <small>Chọn khoảng nghỉ nhanh, hoặc đang nghe thì đóng dấu chính xác bằng vị trí đầu phát.</small>
          </div>
          <div className="gap-presets">
            <button disabled={activeLine.locked} type="button" onClick={() => applyTransitionGap(0)}>Liền 0 ms</button>
            <button disabled={activeLine.locked} type="button" onClick={() => applyTransitionGap(100_000)}>Nghỉ 100 ms</button>
            <button disabled={activeLine.locked} type="button" onClick={() => applyTransitionGap(250_000)}>Nghỉ 250 ms</button>
            <button disabled={activeLine.locked} type="button" onClick={() => applyTransitionGap(500_000)}>Nghỉ 500 ms</button>
          </div>
          <div className="playhead-stamps">
            <button disabled={previousLine.locked} type="button" onClick={placePreviousEndAtPlayhead}>Đặt HẾT câu trên tại đầu phát</button>
            <button disabled={activeLine.locked} type="button" onClick={placeCurrentStartAtPlayhead}>Đặt ĐẦU câu dưới tại đầu phát</button>
          </div>
        </div>
      )}
    </div>
  ) : null;
  const songTimelineRoll = activeLine ? (
    <SongTimelineRoll
      projectId={data.project.id}
      timeline={timeline}
      nowUs={currentTimeUs}
      activeLineId={activeLine.id}
      selectedTokenId={selectedTokenId}
      transitionControls={transitionControls}
      autosaveStatus={autosaveStatus}
      autosaveRevision={timeline.revision}
      autosaveError={autosaveError}
      onRetryAutosave={() => scheduleAutosave(0)}
      canUndo={history.length > 0}
      canRedo={future.length > 0}
      lineLocked={activeLine.locked}
      lineVerified={activeLine.verified}
      onUndo={undo}
      onRedo={redo}
      onToggleLock={toggleLock}
      onVerifyLine={verifyLine}
      onSelectToken={(lineId, tokenId) => selectToken(tokenId, lineId)}
      onSilence={silenceManualDrag}
      onBeginEdit={() => {
        setHistory((items) => [...items, timelineRef.current].slice(-50));
        setFuture([]);
        silenceManualDrag();
      }}
      onCommitTimeline={(next, lineId, tokenId) => {
        stageTimeline(next);
        setActiveLineId(lineId);
        setSelectedTokenId(tokenId);
        silenceManualDrag();
      }}
      onStructureChange={(next, lineId, tokenId) => {
        applyTimeline(next);
        setActiveLineId(lineId);
        setSelectedTokenId(tokenId);
        silenceManualDrag();
      }}
      onSeek={(us) => {
        updateLoopRange(null);
        setSelectedTokenId(null);
        const lineAtTime = activeLineAt(timeline, us);
        const upcomingLine = timeline.lines.findIndex((line) => line.start_us > us);
        const nearestLine = lineAtTime ?? (
          upcomingLine < 0 ? timeline.lines.length - 1 : Math.max(0, upcomingLine - 1)
        );
        if (timeline.lines[nearestLine]) setActiveLineId(timeline.lines[nearestLine].id);
        seek(us);
      }}
    />
  ) : null;
  return (
    <section className="fcp-editor-workspace">
        <section className="monitor-panel">
          <div className="panel-toolbar">
            <div><span className="viewer-title">VIEWER</span><strong className="preview-quality-badge" aria-label="Preview mặc định 1920 nhân 1080, 120 fps">◆ 1080P · 120 FPS</strong><strong className={`mix-audio-badge ${usingInstrumentalPreview ? 'is-instrumental' : 'is-original'}`} aria-label={usingInstrumentalPreview ? `Karaoke ${data.project.instrumental_confirmed ? 'đã xác nhận' : 'chưa xác nhận'}` : 'Original Mix'} title={usingInstrumentalPreview ? `Đúng stem sẽ dùng khi xuất Karaoke: ${selectedInstrumentalName} · ${data.project.instrumental_confirmed ? 'đã xác nhận' : 'chưa xác nhận'}` : 'Âm thanh có ca sĩ từ video gốc'}>● {usingInstrumentalPreview ? `KARAOKE${data.project.instrumental_confirmed ? ' ✓' : ''}` : 'ORIGINAL MIX'}</strong>{data.backgroundPlan && <strong className="auto-scene-badge">✦ AUTO SCENE · {data.backgroundPlan.assets.length}</strong>}</div>
            <span className="viewer-timecode">{formatFrameTime(currentTimeUs, timeline, KARAOKE_PREVIEW_FPS, 1)} <i>/</i> {formatFrameTime(timeline.duration_us, timeline, KARAOKE_PREVIEW_FPS, 1)}</span>
            <div className="viewer-tools" aria-hidden="true"><span>⌗</span><span>▣</span><span>100%</span></div>
          </div>
          <div
            className="monitor-stage"
            data-preview-fps={KARAOKE_PREVIEW_FPS}
            data-preview-height={KARAOKE_PREVIEW_HEIGHT}
            data-preview-width={KARAOKE_PREVIEW_WIDTH}
            role="button"
            tabIndex={0}
            aria-label={isPlaying ? 'Tạm dừng video xem trước' : 'Phát video xem trước'}
            title={isPlaying ? 'Bấm để tạm dừng video' : 'Bấm để phát video'}
            onClick={togglePlayback}
            onKeyDown={(event) => {
              if (event.key !== ' ' && event.key !== 'Enter') return;
              event.preventDefault();
              togglePlayback();
            }}
          >
            {data.backgroundPlan && (
              <BackgroundPreview
                plan={data.backgroundPlan}
                nowUs={currentTimeUs}
                playing={isPlaying}
                mediaRef={videoRef}
              />
            )}
            <video className={data.backgroundPlan ? 'monitor-audio-source' : undefined} ref={videoRef} src={videoUrl} preload="metadata" muted={usingInstrumentalPreview} onPlay={(event) => {
              setCurrentTimeUs(Math.round(event.currentTarget.currentTime * 1_000_000));
              setIsPlaying(true);
              if (usingInstrumentalPreview) {
                alignInstrumentalToVideo(true);
                const audio = instrumentalAudioRef.current;
                if (audio) void audio.play().catch(() => event.currentTarget.pause());
              }
            }} onPause={(event) => {
              instrumentalAudioRef.current?.pause();
              setCurrentTimeUs(Math.round(event.currentTarget.currentTime * 1_000_000));
              setIsPlaying(false);
            }} onEnded={(event) => {
              instrumentalAudioRef.current?.pause();
              setCurrentTimeUs(Math.round(event.currentTarget.currentTime * 1_000_000));
              setIsPlaying(false);
            }} onSeeked={(event) => {
              if (usingInstrumentalPreview) alignInstrumentalToVideo(true);
              setCurrentTimeUs(Math.round(event.currentTarget.currentTime * 1_000_000));
            }} onTimeUpdate={(event) => {
              const nowUs = Math.round(event.currentTarget.currentTime * 1_000_000);
              if (usingInstrumentalPreview) alignInstrumentalToVideo();
              const audition = loopRangeRef.current;
              if (audition && nowUs >= audition.endUs) {
                if (!audition.repeat) {
                  updateLoopRange(null);
                  pauseViewer();
                  event.currentTarget.currentTime = audition.endUs / 1_000_000;
                  alignInstrumentalToVideo(true);
                  setCurrentTimeUs(audition.endUs);
                  return;
                }
                event.currentTarget.currentTime = audition.startUs / 1_000_000;
                alignInstrumentalToVideo(true);
                setCurrentTimeUs(audition.startUs);
                if (!event.currentTarget.paused) playViewer();
                return;
              }
              setCurrentTimeUs(nowUs);
            }} />
            {instrumentalUrl && (
              <audio
                className="viewer-instrumental-audio"
                key={instrumentalUrl}
                ref={instrumentalAudioRef}
                src={instrumentalUrl}
                preload="metadata"
                onLoadedMetadata={(event) => {
                  event.currentTarget.volume = viewerVolume;
                  if (!usingInstrumentalPreview) return;
                  alignInstrumentalToVideo(true);
                  if (videoRef.current && !videoRef.current.paused) {
                    void event.currentTarget.play().catch(() => videoRef.current?.pause());
                  }
                }}
                onError={() => {
                  setPreviewAudioMode('original');
                  onError('Không phát được instrumental đã chọn; Viewer đã trở về bản gốc.');
                }}
              />
            )}
            <SongCreditOverlay
              title={data.project.title}
              artist={data.project.artist}
              nowUs={currentTimeUs}
              mediaRef={videoRef}
              playing={isPlaying}
            />
            <KaraokeOverlay
              timeline={timeline}
              nowUs={currentTimeUs}
              mediaRef={videoRef}
              playing={isPlaying}
            />
          </div>
          <Waveform data={previewWaveform} nowUs={currentTimeUs} durationUs={timeline.duration_us} onSeek={seek} />
          <div className="viewer-transport-bar">
            <span>{loopRange ? `${loopRange.kind === 'transition' ? 'CHUYỂN CÂU' : loopRange.kind === 'line' ? 'NGUYÊN CÂU' : 'LOOP TỪ'} · ${activeLine?.tokens.find((token) => token.id === loopRange.tokenId)?.text ?? 'TIMING'}` : 'PREVIEW · 1920×1080 · 120 FPS'}</span>
            <div className="viewer-transport">
              <button title="Lùi một frame" type="button" onClick={() => stepViewerFrame(-1)}>‹│</button>
              <button title="Về đầu câu" type="button" onClick={() => seek(activeLine?.start_us ?? 0)}>│◀</button>
              <button className="viewer-play" title={isPlaying ? 'Tạm dừng' : 'Phát'} type="button" onClick={togglePlayback}>{isPlaying ? 'Ⅱ' : '▶'}</button>
              <button title="Tiến một frame" type="button" onClick={() => stepViewerFrame(1)}>│›</button>
              <button title="Nghe lại nguyên câu" type="button" onClick={replayActiveLine}>↻</button>
            </div>
            <div className="viewer-audio-controls">
              <div className="preview-audio-switch" role="group" aria-label="Nguồn âm thanh Viewer">
                <button className={usingInstrumentalPreview ? 'active' : ''} disabled={!instrumentalUrl} aria-pressed={usingInstrumentalPreview} title={instrumentalUrl ? `Nghe đúng stem Karaoke: ${selectedInstrumentalName}` : 'Chưa có instrumental'} type="button" onClick={() => choosePreviewAudioMode('instrumental')}>KARAOKE</button>
                <button className={!usingInstrumentalPreview ? 'active' : ''} aria-pressed={!usingInstrumentalPreview} title="Nghe lại âm thanh có ca sĩ từ video gốc" type="button" onClick={() => choosePreviewAudioMode('original')}>GỐC</button>
              </div>
              <span>VOL</span>
              <input aria-label="Âm lượng Viewer" type="range" min="0" max="1" step="0.05" value={viewerVolume} onChange={(event) => setVolume(Number(event.target.value))} />
              <button title="Toàn màn hình" type="button" onClick={() => void videoRef.current?.parentElement?.requestFullscreen()}>⛶</button>
            </div>
          </div>
        </section>

        <aside className="fcp-inspector" aria-label="Inspector dự án">
          <div className="fcp-inspector-tabs" role="tablist" aria-label="Công cụ kiểm tra">
            <button className={inspectorTab === 'review' ? 'active' : ''} type="button" role="tab" aria-selected={inspectorTab === 'review'} onClick={() => setInspectorTab('review')}>Timing <span>{reviewLineCount}</span></button>
            <button className={inspectorTab === 'audio' ? 'active' : ''} type="button" role="tab" aria-selected={inspectorTab === 'audio'} onClick={() => setInspectorTab('audio')}>Audio</button>
            <button className={inspectorTab === 'text' ? 'active' : ''} type="button" role="tab" aria-selected={inspectorTab === 'text'} onClick={() => setInspectorTab('text')}>Text</button>
            <button className={inspectorTab === 'export' ? 'active' : ''} type="button" role="tab" aria-selected={inspectorTab === 'export'} onClick={() => setInspectorTab('export')}>Share</button>
          </div>
          <div className="fcp-inspector-body">
            {inspectorTab === 'review' && (
              <section className="review-queue">
                <div className="inspector-selection">
                  <div><span>{String(Math.max(0, activeLineIndex) + 1).padStart(2, '0')}</span><strong>{activeLine?.text ?? 'Chưa chọn câu'}</strong></div>
                  <dl>
                    <div><dt>Bắt đầu</dt><dd>{formatTime(activeLine?.start_us ?? 0)}</dd></div>
                    <div><dt>Kết thúc</dt><dd>{formatTime(activeLine?.end_us ?? 0)}</dd></div>
                    <div><dt>Độ dài</dt><dd>{Math.round(((activeLine?.end_us ?? 0) - (activeLine?.start_us ?? 0)) / 1_000)} ms</dd></div>
                    <div><dt>Confidence</dt><dd>{Math.round((activeLine?.confidence ?? 0) * 100)}%</dd></div>
                  </dl>
                </div>
                <div className="queue-header"><div><span className="section-kicker">TIMING INSPECTOR</span><h2>{reviewLineCount ? `Còn ${reviewLineCount} câu cần nghe` : 'AI đã phân tích trọn bài'}</h2></div><span className="warning-count">{lowConfidence}</span></div>
                <button className="queue-filter" type="button" onClick={() => setReviewOnly((current) => !current)}>{reviewOnly ? `Đang lọc ${reviewLineCount} câu cần duyệt · Hiện tất cả` : `Đang hiện ${timeline.lines.length} câu · Chỉ hiện điểm yếu`}</button>
                <div className="line-list">
                  {!visibleLines.length && <div className="queue-empty"><strong>Không còn timing cần sửa</strong><small>AI đã tự động đạt toàn bộ cue; bạn chỉ cần hoàn tất cổng instrumental.</small></div>}
                  {visibleLines.map((line) => {
                    const index = timeline.lines.findIndex((candidate) => candidate.id === line.id);
                    const needsReview = reviewLineIds.has(line.id);
                    return (
                    <button className={`line-item ${line.id === activeLine?.id ? 'active' : ''} ${line.verified || !needsReview ? 'verified' : ''}`} type="button" key={line.id} onClick={() => { setActiveLineId(line.id); setSelectedTokenId(null); updateLoopRange(null); seek(Math.max(0, line.start_us - 250_000)); }}>
                      <span className="line-number">{String(index + 1).padStart(2, '0')}</span>
                      <div><strong>{line.text}</strong><small>{formatTime(line.start_us)} — {formatTime(line.end_us)} · {needsReview ? confidenceLabel(line.confidence) : 'AI tự động đạt'}</small></div>
                      <i>{line.verified ? '✓' : needsReview ? Math.round(line.confidence * 100) : 'AI'}</i>
                    </button>
                    );
                  })}
                </div>
              </section>
            )}
            {inspectorTab === 'audio' && <CandidateReview project={data.project} waveform={data.waveform} artifacts={data.artifacts} onReload={onReload} onError={onError} onSelected={() => setPreviewAudioMode('instrumental')} />}
            {inspectorTab === 'text' && (
              <section className="text-inspector">
                <div><span className="section-kicker">TEXT INSPECTOR</span><h2>Hai dòng Karaoke cố định</h2><p>Font được áp dụng đồng nhất cho Preview và video xuất. Mọi thay đổi đều tự lưu vào TimelineV1.</p></div>
                <KaraokeFontPicker compact value={karaokeFontId(timeline.metadata)} onChange={selectKaraokeFont} />
                <KaraokeColorPicker compact value={karaokeColorId(timeline.metadata)} onChange={selectKaraokeColor} />
                <div className="inspector-parameter-list">
                  <div><span>Bố cục</span><strong>2 lane cố định</strong></div>
                  <div><span>Chuyển động</span><strong>Quét màu theo giọng hát</strong></div>
                  <div><span>Kiểu chữ</span><strong>Viền trắng · bóng xanh</strong></div>
                  <div><span>Màu active</span><strong><i className="karaoke-color-swatch" style={{ background: karaokeColorHex(karaokeColorId(timeline.metadata)) }} /> {karaokeColorLabel(karaokeColorId(timeline.metadata))}</strong></div>
                  <div><span>Xuất hình</span><strong>{exportPresetLabel(preset)}</strong></div>
                </div>
              </section>
            )}
            {inspectorTab === 'export' && (
              <section className="export-panel">
                <div><span className="section-kicker">SHARE / XUẤT VIDEO</span><h2>Xuất đúng âm thanh đã nghe</h2><p>Điểm timing chỉ là cảnh báo. Draft mix gốc luôn khả dụng; Final loại giọng cần xác nhận đúng instrumental để không xuất nhầm.</p></div>
                <label>Preset<select value={preset} onChange={(event) => setPreset(event.target.value as ExportPreset)}>{EXPORT_PRESET_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>
                {preset === '1080p120' && <p className="export-preset-warning">120fps CFR thật · phát 1× trong QuickTime · clock từng frame rõ ràng. Thiết bị vẫn cần hỗ trợ giải mã H.264 1080p120.</p>}
                <div className={`export-audio-summary ${data.project.instrumental_confirmed ? 'confirmed' : 'needs-confirmation'}`}><span>Âm thanh Karaoke</span><strong>{selectedInstrumentalName}</strong><small>{data.project.instrumental_confirmed ? '✓ Đã nghe và xác nhận · Final dùng đúng stem này' : 'Cần nghe và xác nhận trước khi xuất Final'}</small>{!data.project.instrumental_confirmed && <button type="button" onClick={() => setInspectorTab('audio')}>Mở Audio để xác nhận</button>}</div>
                <div className="export-actions"><button disabled={Boolean(rendering) || dirty || saving} type="button" onClick={() => render('draft')}>Xuất bản có ca sĩ · mix gốc</button><button className="final-button" title={!data.project.selected_instrumental ? 'Chưa có instrumental để loại giọng' : !data.project.instrumental_confirmed ? 'Hãy nghe và xác nhận instrumental trong tab Audio' : `Final sẽ dùng ${selectedInstrumentalName}`} disabled={!data.project.selected_instrumental || !data.project.instrumental_confirmed || Boolean(rendering) || dirty || saving} type="button" onClick={() => render('final')}>{data.project.state === 'VERIFIED' || data.project.state === 'RENDERED' ? 'Final đã loại giọng' : 'Xuất Karaoke loại giọng'}</button></div>
                <button className="verification-gate" disabled={dirty || saving || data.project.state === 'VERIFIED' || data.project.state === 'RENDERED'} type="button" onClick={markVerified}><span>{data.project.instrumental_confirmed ? '✓' : '○'} Instrumental</span><span>{lowConfidence === 0 ? '✓' : '○'} {lowConfidence ? `${lowConfidence} điểm nên kiểm tra` : 'Timing AI đã đạt'}</span><strong>{data.project.state === 'VERIFIED' || data.project.state === 'RENDERED' ? 'ĐÃ VERIFIED' : 'Timeline Verified là tùy chọn · Final chỉ khóa khi stem chưa xác nhận'}</strong></button>
                <ExportList
                  projectId={data.project.id}
                  artifacts={data.artifacts}
                  rendering={Boolean(rendering)}
                  onReload={onReload}
                  onError={onError}
                />
              </section>
            )}
          </div>
        </aside>

      <section className="fcp-timeline-pane">
        <div className="fcp-timeline-heading"><span>TIMELINE</span><strong>{activeLine?.text}</strong><small>{timeline.lines.length} câu · {timeline.lines.reduce((count, line) => count + line.tokens.length, 0)} từ</small></div>
        {songTimelineRoll}
      </section>
    </section>
  );
}

function CandidateReview({ project, waveform, artifacts, onReload, onError, onSelected }: { project: Project; waveform: WaveformPayload | null; artifacts: Artifact[]; onReload: () => Promise<void>; onError: (message: string | null) => void; onSelected: (candidateId: string) => void }) {
  const finalCandidates = Object.entries(waveform?.candidates ?? {})
    .filter(([candidateId]) => ['bs_polarformer_fp32', 'bs_roformer_viperx_1297'].includes(candidateId))
    .sort(([left]) => left === 'bs_polarformer_fp32' ? -1 : 1)
    .slice(0, 1);
  const usingPolarformer = finalCandidates[0]?.[0] === 'bs_polarformer_fp32';
  const finalModelName = usingPolarformer ? 'BS PolarFormer 62 · FP32' : 'BS-RoFormer ViperX 1297';
  const select = async (candidateId: string) => {
    try {
      await api<Project>(`/api/projects/${project.id}/instrumental`, { method: 'POST', body: JSON.stringify({ candidate_id: candidateId, confirmed: true }) });
      await onReload();
      onSelected(candidateId);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'Không xác nhận được instrumental.');
    }
  };

  return (
    <section className="candidate-panel">
      <div><span className="section-kicker">ÂM THANH FINAL</span><h2>{finalModelName}</h2><p>{usingPolarformer ? 'PolarFormer 62-band FP32 là bản Karaoke chất lượng cao mặc định để Preview và xuất Final.' : 'ViperX 1297 đang được dùng làm fallback an toàn vì PolarFormer FP32 chưa tạo stem thành công.'}</p></div>
      <div className="candidate-list">
        {finalCandidates.map(([candidateId, candidate]) => {
          const artifact = selectedInstrumentalArtifact(candidateId, artifacts);
          const selected = project.selected_instrumental === candidateId;
          return (
            <article className={`candidate-card ${selected ? 'selected' : ''}`} key={candidateId}>
              <div><span>{candidate.production_grade ? 'AI' : 'FB'}</span><strong>{candidate.label}</strong><small>{candidate.production_grade ? 'Production candidate' : 'Fallback cần kiểm tra kỹ'}</small></div>
              {artifact && <audio controls preload="none" src={assetUrl(artifact.url)} onPlay={(event) => {
                document.querySelectorAll<HTMLMediaElement>('audio, video').forEach((media) => {
                  if (media !== event.currentTarget) media.pause();
                });
              }} />}
              {candidate.warning && <p>{candidate.warning}</p>}
              <button className={selected && project.instrumental_confirmed ? 'confirmed' : ''} type="button" onClick={() => select(candidateId)}>{selected && project.instrumental_confirmed ? 'Mặc định để xuất ✓' : `Đặt ${candidate.label} làm mặc định`}</button>
            </article>
          );
        })}
        {finalCandidates.length === 0 && <p>PolarFormer FP32 chưa sẵn sàng. Hãy bấm Phân tích lại để tải model và tạo stem mặc định.</p>}
      </div>
    </section>
  );
}

function PipelineStatus({ project, job }: { project: Project; job: Job | null }) {
  const activeIndex = stateIndex(project.state);
  return (
    <section className="pipeline-status">
      {STAGE_LABELS.map((label, index) => <div className={`${index < activeIndex ? 'done' : ''} ${index === activeIndex ? 'active' : ''}`} key={label}><span>{index < activeIndex ? '✓' : String(index + 1).padStart(2, '0')}</span><strong>{label}</strong></div>)}
      {job && ['PENDING', 'RUNNING'].includes(job.state) && <div className="job-progress"><span>{job.message}</span><b>{Math.round(job.progress * 100)}%</b><div><i style={{ width: `${job.progress * 100}%` }} /></div></div>}
      {job?.state === 'FAILED' && <div className="job-failed">{job.error || job.message}</div>}
    </section>
  );
}

function BackgroundPreview({
  plan,
  nowUs,
  playing,
  mediaRef,
}: {
  plan: BackgroundPlanV1;
  nowUs: number;
  playing: boolean;
  mediaRef: { readonly current: HTMLVideoElement | null };
}) {
  const sampledUs = usePreviewMediaClock(mediaRef, nowUs, playing);
  const assets = useMemo(
    () => new Map(plan.assets.map((asset) => [asset.id, asset])),
    [plan.assets],
  );
  const segmentIndex = Math.max(
    0,
    plan.segments.findLastIndex((segment) => sampledUs >= segment.start_us),
  );
  const segment = plan.segments[segmentIndex] ?? plan.segments[0];
  if (!segment) return null;
  const asset = assets.get(segment.asset_id);
  if (!asset?.url) return null;

  const rawTransitionProgress = segmentIndex > 0 && segment.transition_us > 0
    ? Math.max(0, Math.min(1, (sampledUs - segment.start_us) / segment.transition_us))
    : 1;
  const transitionProgress = smoothBackgroundTransition(rawTransitionProgress);
  const previousSegment = transitionProgress < 1 ? plan.segments[segmentIndex - 1] : null;
  const previousAsset = previousSegment ? assets.get(previousSegment.asset_id) : null;

  return (
    <div className="background-preview" aria-label={`Nền tự động cảnh ${segmentIndex + 1} trên ${plan.segments.length}`}>
      {previousSegment && previousAsset?.url && (
        <BackgroundSceneMedia
          asset={previousAsset}
          localUs={Math.max(0, sampledUs - previousSegment.start_us)}
          opacity={1 - transitionProgress}
          playing={playing}
          progress={backgroundSceneProgress(plan, segmentIndex - 1, sampledUs)}
          sceneIndex={segmentIndex - 1}
        />
      )}
      <BackgroundSceneMedia
        asset={asset}
        localUs={Math.max(0, sampledUs - segment.start_us)}
        opacity={transitionProgress}
        playing={playing}
        progress={backgroundSceneProgress(plan, segmentIndex, sampledUs)}
        sceneIndex={segmentIndex}
      />
      <span className="background-scene-status">SCENE {String(segmentIndex + 1).padStart(2, '0')} / {String(plan.segments.length).padStart(2, '0')} · {segment.anchor === 'lyric_gap' ? 'LYRIC GAP' : 'BALANCED'}{segment.transition_us > 0 ? ` · DISSOLVE ${(segment.transition_us / 1_000_000).toFixed(1)}S` : ''}</span>
    </div>
  );
}

function usePreviewMediaClock(
  mediaRef: { readonly current: HTMLVideoElement | null },
  fallbackNowUs: number,
  playing: boolean,
): number {
  const [sampledUs, setSampledUs] = useState(fallbackNowUs);

  useEffect(() => {
    if (!playing) return;
    let frameId = 0;
    let active = true;
    const clock: SmoothPlaybackClock = {
      anchorMediaUs: null,
      anchorSampleMs: 0,
      playbackRate: 1,
    };
    const tick = (sampleMs: number) => {
      if (!active) return;
      const media = mediaRef.current;
      if (media) {
        setSampledUs(previewFrameTimeUs(smoothedPlaybackTimeUs(
          clock,
          media.currentTime * 1_000_000,
          sampleMs,
          media.playbackRate,
          media.paused || media.seeking || media.readyState < HTMLMediaElement.HAVE_FUTURE_DATA,
        )));
      }
      frameId = window.requestAnimationFrame(tick);
    };
    frameId = window.requestAnimationFrame(tick);
    return () => {
      active = false;
      window.cancelAnimationFrame(frameId);
    };
  }, [mediaRef, playing]);

  return playing ? sampledUs : fallbackNowUs;
}

function BackgroundSceneMedia({
  asset,
  localUs,
  opacity,
  playing,
  progress,
  sceneIndex,
}: {
  asset: BackgroundAssetV1;
  localUs: number;
  opacity: number;
  playing: boolean;
  progress: number;
  sceneIndex: number;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const source = assetUrl(asset.url ?? '');
  const syncVideo = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    const sourceDurationUs = asset.source_duration_us ?? 0;
    const desiredUs = sourceDurationUs > 0 ? localUs % sourceDurationUs : localUs;
    const desiredSeconds = desiredUs / 1_000_000;
    if (Number.isFinite(video.duration) && Math.abs(video.currentTime - desiredSeconds) > 0.22) {
      video.currentTime = Math.min(desiredSeconds, Math.max(0, video.duration - 0.01));
    }
    if (playing) void video.play().catch(() => undefined);
    else video.pause();
  }, [asset.source_duration_us, localUs, playing]);

  useEffect(syncVideo, [syncVideo]);
  const motion = backgroundMotionFrame(sceneIndex, progress);
  const style = {
    opacity,
    transform: `translate3d(${motion.xPercent.toFixed(4)}%, ${motion.yPercent.toFixed(4)}%, 0) scale(${motion.scale.toFixed(6)})`,
    transition: 'none',
  } as CSSProperties;
  if (asset.kind === 'image') {
    // The source is a local project file selected at runtime; next/image cannot predeclare it.
    // eslint-disable-next-line @next/next/no-img-element
    return <img className="background-scene-media" src={source} alt="" style={style} />;
  }
  return (
    <video
      className="background-scene-media"
      ref={videoRef}
      src={source}
      muted
      loop
      playsInline
      preload="metadata"
      style={style}
      onLoadedMetadata={syncVideo}
    />
  );
}

function SongCreditOverlay({
  title,
  artist,
  nowUs,
  mediaRef,
  playing,
}: {
  title: string;
  artist: string;
  nowUs: number;
  mediaRef: { readonly current: HTMLVideoElement | null };
  playing: boolean;
}) {
  const sampledUs = usePreviewMediaClock(mediaRef, nowUs, playing);
  const opacity = songCreditOpacity(sampledUs);
  if (opacity <= 0) return null;
  return (
    <div
      className="song-credit-overlay"
      aria-label={artist ? `Tên bài hát ${title}, ca sĩ hoặc nguồn ${artist}` : `Tên bài hát ${title}`}
      style={{ opacity }}
    >
      <i aria-hidden="true" />
      <span>
        <strong>{title}</strong>
        {artist && <small>CA SĨ / NGUỒN · {artist}</small>}
      </span>
    </div>
  );
}

function KaraokeOverlay({
  timeline,
  nowUs,
  mediaRef,
  playing,
}: {
  timeline: Timeline;
  nowUs: number;
  mediaRef: { readonly current: HTMLVideoElement | null };
  playing: boolean;
}) {
  const structureNowUs = useKaraokeOverlayClock(timeline, mediaRef, nowUs, playing);
  const active = activeLineAt(timeline, structureNowUs);
  const countdown = karaokeCountdownCue(timeline, structureNowUs);
  const rows = karaokeVisibleLineIndexes(timeline, structureNowUs).map((index) => ({
    line: timeline.lines[index],
    active: index === active,
    lane: index % 2,
  }));
  const selectedFont = karaokeFontId(timeline.metadata);
  const selectedColor = karaokeColorId(timeline.metadata);
  return (
    <div
      className="karaoke-overlay"
      aria-label="Xem trước chuyển động Karaoke"
      style={{
        fontFamily: `'${karaokeFontFamily(selectedFont)}', sans-serif`,
        '--karaoke-highlight': karaokeColorHex(selectedColor),
      } as CSSProperties}
    >
      {rows.map(({ line, active: isActive, lane }) => (
        <div
          className={`preview-lyric lane-${lane} ${isActive ? 'is-active' : ''}`}
          key={line.id}
        >
          <FittedKaraokeLine
            line={line}
            active={isActive}
            nowUs={nowUs}
            mediaRef={mediaRef}
            playing={playing}
          />
        </div>
      ))}
      {countdown && (
        <div
          className={`karaoke-countdown lane-${countdown.lane}`}
          aria-label={`Đếm ${countdown.number} trước câu hát tiếp theo`}
          key={`${countdown.nextLineId}-${countdown.number}`}
        >
          <strong>{countdown.number}</strong>
        </div>
      )}
    </div>
  );
}

function karaokeOverlayStateKey(timeline: Timeline, nowUs: number): string {
  const active = activeLineAt(timeline, nowUs);
  const countdown = karaokeCountdownCue(timeline, nowUs);
  const visible = karaokeVisibleLineIndexes(timeline, nowUs)
    .map((index) => timeline.lines[index].id)
    .join(',');
  return [
    `active:${active === null ? 'none' : timeline.lines[active].id}`,
    `visible:${visible || 'none'}`,
    `countdown:${countdown ? `${countdown.nextLineId}:${countdown.number}` : 'none'}`,
  ].join('|');
}

function useKaraokeOverlayClock(
  timeline: Timeline,
  mediaRef: { readonly current: HTMLVideoElement | null },
  fallbackNowUs: number,
  playing: boolean,
): number {
  const [structureNowUs, setStructureNowUs] = useState(fallbackNowUs);
  const stateKeyRef = useRef(karaokeOverlayStateKey(timeline, fallbackNowUs));

  const syncStructure = useCallback((sampledUs: number) => {
    const nextKey = karaokeOverlayStateKey(timeline, sampledUs);
    if (nextKey === stateKeyRef.current) return;
    stateKeyRef.current = nextKey;
    setStructureNowUs(sampledUs);
  }, [timeline]);

  useEffect(() => {
    syncStructure(fallbackNowUs);
  }, [fallbackNowUs, syncStructure]);

  useEffect(() => {
    if (!playing) return;
    let frameId = 0;
    let active = true;
    const tick = () => {
      if (!active) return;
      const media = mediaRef.current;
      if (media) {
        syncStructure(Math.round(media.currentTime * 1_000_000));
      }
      frameId = window.requestAnimationFrame(tick);
    };
    frameId = window.requestAnimationFrame(tick);
    return () => {
      active = false;
      window.cancelAnimationFrame(frameId);
    };
  }, [mediaRef, playing, syncStructure]);

  return structureNowUs;
}

function FittedKaraokeLine({
  line,
  active,
  nowUs,
  mediaRef,
  playing,
}: {
  line: LineTiming;
  active: boolean;
  nowUs: number;
  mediaRef: { readonly current: HTMLVideoElement | null };
  playing: boolean;
}) {
  const stackRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Array<HTMLSpanElement | null>>([]);
  const highlightRefs = useRef<Array<HTMLElement | null>>([]);
  const fallbackNowUsRef = useRef(nowUs);
  const displayRows = useMemo(() => karaokeDisplayRows(line), [line]);

  const paintHighlight = useCallback((sampledUs: number) => {
    const progressPpm = Math.round(
      highlightPercent(line, previewFrameTimeUs(sampledUs)) * 10_000,
    );
    displayRows.forEach((row, index) => {
      const highlight = highlightRefs.current[index];
      if (!highlight) return;
      const rowProgress = progressPpm <= row.startProgressPpm
        ? 0
        : progressPpm >= row.endProgressPpm
          ? 1
          : (progressPpm - row.startProgressPpm)
            / Math.max(1, row.endProgressPpm - row.startProgressPpm);
      const nextValue = `${((1 - rowProgress) * 100).toFixed(5)}%`;
      if (highlight.style.getPropertyValue('--karaoke-clip-right') !== nextValue) {
        highlight.style.setProperty('--karaoke-clip-right', nextValue);
      }
    });
  }, [displayRows, line]);

  useLayoutEffect(() => {
    const stack = stackRef.current;
    const lane = stack?.parentElement;
    if (!stack || !lane) return;
    let cancelled = false;
    const fit = () => {
      if (cancelled) return;
      const horizontalSafeArea = Math.max(16, lane.clientWidth * 0.035);
      const availableWidth = Math.max(1, lane.clientWidth - horizontalSafeArea * 2);
      rowRefs.current.forEach((text) => {
        const row = text?.parentElement;
        if (!text || !row) return;
        row.style.setProperty('--lyric-fit-x', '1');
        row.style.setProperty(
          '--lyric-fit-x',
          String(lyricFitScale(availableWidth, text.scrollWidth)),
        );
      });
    };
    fit();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
    observer?.observe(lane);
    void document.fonts?.ready.then(fit);
    return () => {
      cancelled = true;
      observer?.disconnect();
    };
  }, [displayRows]);

  useLayoutEffect(() => {
    fallbackNowUsRef.current = nowUs;
    if (active) paintHighlight(nowUs);
  }, [active, nowUs, paintHighlight]);

  useEffect(() => {
    if (!active || !playing) return;
    let frameId = 0;
    let running = true;
    const clock: SmoothPlaybackClock = {
      anchorMediaUs: null,
      anchorSampleMs: 0,
      playbackRate: 1,
    };
    const paintFrame = (sampleMs: number) => {
      if (!running) return;
      const media = mediaRef.current;
      paintHighlight(
        media
          ? smoothedPlaybackTimeUs(
            clock,
            media.currentTime * 1_000_000,
            sampleMs,
            media.playbackRate,
            media.paused || media.seeking || media.readyState < HTMLMediaElement.HAVE_FUTURE_DATA,
          )
          : fallbackNowUsRef.current,
      );
      frameId = window.requestAnimationFrame(paintFrame);
    };
    frameId = window.requestAnimationFrame(paintFrame);
    return () => {
      running = false;
      window.cancelAnimationFrame(frameId);
    };
  }, [active, mediaRef, paintHighlight, playing]);

  return (
    <div className={`lyric-stack ${displayRows.length > 1 ? 'is-multiline' : ''}`} ref={stackRef}>
      {displayRows.map((row, index) => {
        const progressPpm = Math.round(
          highlightPercent(line, previewFrameTimeUs(nowUs)) * 10_000,
        );
        const rowProgress = progressPpm <= row.startProgressPpm
          ? 0
          : progressPpm >= row.endProgressPpm
            ? 1
            : (progressPpm - row.startProgressPpm)
              / Math.max(1, row.endProgressPpm - row.startProgressPpm);
        return (
          <div
            className="lyric-stack-row"
            key={`${line.id}-${index}`}
          >
            <span ref={(element) => { rowRefs.current[index] = element; }}>{row.text}</span>
            {active && (
              <b
                ref={(element) => { highlightRefs.current[index] = element; }}
                style={{ '--karaoke-clip-right': `${(1 - rowProgress) * 100}%` } as CSSProperties}
              >
                {row.text}
              </b>
            )}
          </div>
        );
      })}
    </div>
  );
}

function KaraokeFontPicker({
  value,
  onChange,
  compact = false,
}: {
  value: KaraokeFontId;
  onChange: (fontId: KaraokeFontId) => void;
  compact?: boolean;
}) {
  if (compact) {
    return (
      <label className="karaoke-font-picker compact">
        <span aria-hidden="true">Aa</span>
        <span>
          <small>KIỂU CHỮ KARAOKE</small>
          <select value={value} onChange={(event) => onChange(event.target.value as KaraokeFontId)} aria-label="Kiểu chữ Karaoke">
            {KARAOKE_FONTS.map((font) => <option key={font.id} value={font.id}>{font.label}</option>)}
          </select>
        </span>
        <em>TỰ LƯU</em>
      </label>
    );
  }
  return (
    <fieldset className="karaoke-font-picker">
      <legend>Kiểu chữ Karaoke</legend>
      <div>
        {KARAOKE_FONTS.map((font) => (
          <button
            className={value === font.id ? 'selected' : ''}
            key={font.id}
            type="button"
            onClick={() => onChange(font.id)}
            aria-pressed={value === font.id}
          >
            <strong style={{ fontFamily: `'${font.family}', sans-serif` }}>Thánh Linh</strong>
            <span>{font.label}</span>
            <small>{font.hint}</small>
          </button>
        ))}
      </div>
      <p>Chiều cao luôn cố định; câu dài chỉ được nén ngang để hai dòng không nhảy cỡ chữ.</p>
    </fieldset>
  );
}

function KaraokeColorPicker({
  value,
  onChange,
  compact = false,
}: {
  value: KaraokeColorId;
  onChange: (colorId: KaraokeColorId) => void;
  compact?: boolean;
}) {
  return (
    <fieldset className={`karaoke-color-picker ${compact ? 'compact' : ''}`}>
      <legend>{compact ? 'MÀU CHỮ CHẠY' : 'Màu chữ chuyển động'}</legend>
      <div>
        {KARAOKE_COLORS.map((color) => (
          <button
            className={value === color.id ? 'selected' : ''}
            key={color.id}
            type="button"
            onClick={() => onChange(color.id)}
            aria-label={`Chọn màu Karaoke ${color.label}`}
            aria-pressed={value === color.id}
            title={color.hint}
            style={{ '--choice-color': color.hex } as CSSProperties}
          >
            <i style={{ background: color.hex }} />
            <span>{color.label}</span>
            {!compact && <small>{color.hint}</small>}
          </button>
        ))}
      </div>
      {!compact && <p>Màu đã chọn sẽ quét mượt theo giọng hát trong Preview và video xuất.</p>}
    </fieldset>
  );
}

function Waveform({ data, nowUs, durationUs, onSeek }: { data: number[]; nowUs: number; durationUs: number; onSeek: (us: number) => void }) {
  const bars = useMemo(() => {
    if (!data.length) return Array.from({ length: 180 }, (_, index) => 0.14 + ((index * 17) % 60) / 100);
    const step = Math.max(1, Math.floor(data.length / 180));
    return data.filter((_, index) => index % step === 0).slice(0, 180);
  }, [data]);
  const percent = (nowUs / Math.max(1, durationUs)) * 100;
  return <button className="review-waveform" type="button" onClick={(event) => { const box = event.currentTarget.getBoundingClientRect(); onSeek(Math.round(((event.clientX - box.left) / box.width) * durationUs)); }} aria-label="Seek waveform"><span className="review-playhead" style={{ left: `${percent}%` }} />{bars.map((height, index) => <i className={index / bars.length * 100 <= percent ? 'played' : ''} style={{ height: `${Math.max(8, height * 100)}%` }} key={index} />)}</button>;
}

const SONG_ROLL_ZOOM = [36, 72, 140] as const;
const SONG_ROLL_TICKS = [0.5, 1, 2, 5, 10, 15, 30, 60] as const;

interface SongRollDrag {
  scope: 'token' | 'line' | 'trim-start' | 'trim-end';
  lineId: string;
  tokenId: string;
  lane: number;
  startX: number;
  originalStartUs: number;
  originalEndUs: number;
  deltaUs: number;
  snappedMarkerUs: number | null;
  started: boolean;
  baseTimeline: Timeline;
  latestTimeline: Timeline;
}

function SongTimelineRoll({
  projectId,
  timeline,
  nowUs,
  activeLineId,
  selectedTokenId,
  transitionControls,
  autosaveStatus,
  autosaveRevision,
  autosaveError,
  onRetryAutosave,
  canUndo,
  canRedo,
  lineLocked,
  lineVerified,
  onUndo,
  onRedo,
  onToggleLock,
  onVerifyLine,
  onSelectToken,
  onSilence,
  onBeginEdit,
  onCommitTimeline,
  onStructureChange,
  onSeek,
}: {
  projectId: string;
  timeline: Timeline;
  nowUs: number;
  activeLineId: string;
  selectedTokenId: string | null;
  transitionControls: ReactNode;
  autosaveStatus: AutosaveStatus;
  autosaveRevision: number;
  autosaveError: string | null;
  onRetryAutosave: () => void;
  canUndo: boolean;
  canRedo: boolean;
  lineLocked: boolean;
  lineVerified: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onToggleLock: () => void;
  onVerifyLine: () => void;
  onSelectToken: (lineId: string, tokenId: string) => void;
  onSilence: () => void;
  onBeginEdit: () => void;
  onCommitTimeline: (timeline: Timeline, lineId: string, tokenId: string) => void;
  onStructureChange: (timeline: Timeline, lineId: string, tokenId: string) => void;
  onSeek: (us: number) => void;
}) {
  const [zoomIndex, setZoomIndex] = useState(1);
  const [fitPixelsPerSecond, setFitPixelsPerSecond] = useState<number | null>(null);
  const [followPlayhead, setFollowPlayhead] = useState(true);
  const [dragMode, setDragMode] = useState<'token' | 'line'>('token');
  const [markers, setMarkers] = useState<number[]>([]);
  const [lyricDraft, setLyricDraft] = useState('');
  const [lyricNotice, setLyricNotice] = useState('Bấm một từ trên timeline để bắt đầu chỉnh lời.');
  const [copiedLine, setCopiedLine] = useState<LineTiming | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const dragGhostRef = useRef<HTMLSpanElement>(null);
  const dragGhostDeltaRef = useRef<HTMLElement>(null);
  const dragGhostSnapRef = useRef<HTMLElement>(null);
  const dragRef = useRef<SongRollDrag | null>(null);
  const dragElementsRef = useRef<{
    label: HTMLElement | null;
    tokens: Map<string, HTMLElement>;
  } | null>(null);
  const dragFrameRef = useRef<number | null>(null);
  const dragInputRef = useRef<{ clientX: number; shiftKey: boolean } | null>(null);
  const suppressClickRef = useRef(false);
  const skipMarkerSaveRef = useRef(true);
  const markerStorageKey = `karaoke-studio:manual-markers:${projectId}`;
  const pixelsPerSecond = fitPixelsPerSecond ?? SONG_ROLL_ZOOM[zoomIndex];
  const contentWidth = fitPixelsPerSecond === null
    ? Math.max(960, timeUsToPixels(timeline.duration_us, pixelsPerSecond) + 24)
    : Math.max(1, timeUsToPixels(timeline.duration_us, pixelsPerSecond) + 24);
  const playheadX = timeUsToPixels(nowUs, pixelsPerSecond);
  const tickSeconds = SONG_ROLL_TICKS.find((seconds) => seconds * pixelsPerSecond >= 90) ?? 60;
  const ticks = useMemo(
    () => Array.from(
      { length: Math.floor(timeline.duration_us / (tickSeconds * 1_000_000)) + 1 },
      (_, index) => index * tickSeconds,
    ),
    [tickSeconds, timeline.duration_us],
  );
  const structureLine = timeline.lines.find((line) => line.id === activeLineId) ?? null;
  const structureToken = structureLine?.tokens.find((token) => token.id === selectedTokenId) ?? null;
  const structureLocked = Boolean(
    structureLine?.locked || structureLine?.tokens.some((token) => token.locked),
  );

  useEffect(() => {
    if (!expanded) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setExpanded(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [expanded]);

  useEffect(() => () => {
    if (dragFrameRef.current !== null) window.cancelAnimationFrame(dragFrameRef.current);
  }, []);

  useEffect(() => {
    skipMarkerSaveRef.current = true;
    const loadTimer = window.setTimeout(() => {
      try {
        const saved = JSON.parse(localStorage.getItem(markerStorageKey) ?? '[]') as unknown;
        setMarkers(
          Array.isArray(saved)
            ? saved.filter((value): value is number => Number.isInteger(value) && value >= 0 && value <= timeline.duration_us).sort((a, b) => a - b)
            : [],
        );
      } catch {
        setMarkers([]);
      }
    }, 0);
    return () => window.clearTimeout(loadTimer);
  }, [markerStorageKey, timeline.duration_us]);

  useEffect(() => {
    if (skipMarkerSaveRef.current) {
      skipMarkerSaveRef.current = false;
      return;
    }
    try {
      localStorage.setItem(markerStorageKey, JSON.stringify(markers));
    } catch {
      // Marker là tiện ích local; timeline vẫn phải chỉnh được nếu storage bị khóa.
    }
  }, [markerStorageKey, markers]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !followPlayhead) return;
    const target = Math.max(0, playheadX - viewport.clientWidth * 0.42);
    viewport.scrollLeft = target;
  }, [followPlayhead, playheadX, pixelsPerSecond]);

  const handleWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    setFollowPlayhead(false);
    if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
      event.preventDefault();
      event.currentTarget.scrollLeft += event.deltaY;
    }
  };

  const seekCanvas = (event: ReactMouseEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button')) return;
    const box = event.currentTarget.getBoundingClientRect();
    const x = Math.max(0, event.clientX - box.left);
    setFollowPlayhead(true);
    onSeek(Math.min(timeline.duration_us, Math.round((x / pixelsPerSecond) * 1_000_000)));
  };

  const changeZoom = (nextIndex: number) => {
    setFitPixelsPerSecond(null);
    setZoomIndex(Math.max(0, Math.min(SONG_ROLL_ZOOM.length - 1, nextIndex)));
  };

  const fitTimeline = () => {
    const viewportWidth = viewportRef.current?.clientWidth ?? 960;
    const durationSeconds = Math.max(1, timeline.duration_us / 1_000_000);
    setFitPixelsPerSecond(Math.max(2, (viewportWidth - 24) / durationSeconds));
    setFollowPlayhead(false);
    if (viewportRef.current) viewportRef.current.scrollLeft = 0;
  };

  const addMarker = () => {
    const markerUs = Math.max(0, Math.min(timeline.duration_us, nowUs));
    setMarkers((current) => {
      if (current.some((value) => Math.abs(value - markerUs) <= 20_000)) return current;
      return [...current, markerUs].sort((a, b) => a - b).slice(0, 100);
    });
  };

  const paintDragLine = (source: Timeline, lineId: string) => {
    const elements = dragElementsRef.current;
    const line = source.lines.find((candidate) => candidate.id === lineId);
    if (!elements || !line) return;
    if (elements.label) {
      elements.label.style.left = `${timeUsToPixels(line.start_us, pixelsPerSecond)}px`;
    }
    line.tokens.forEach((token) => {
      const element = elements.tokens.get(token.id);
      if (!element) return;
      const left = timeUsToPixels(token.start_us, pixelsPerSecond);
      const width = Math.max(
        3,
        timeUsToPixels(token.end_us, pixelsPerSecond) - left,
      );
      element.style.left = `${left}px`;
      element.style.width = `${width}px`;
    });
  };

  const paintDragGhost = (drag: SongRollDrag) => {
    panelRef.current?.classList.add('dragging');
    panelRef.current?.classList.toggle(
      'trimming',
      drag.scope === 'trim-start' || drag.scope === 'trim-end',
    );
    const ghost = dragGhostRef.current;
    if (ghost) {
      ghost.hidden = false;
      ghost.className = `song-roll-drag-origin lane-${drag.lane}`;
      const left = timeUsToPixels(drag.originalStartUs, pixelsPerSecond);
      ghost.style.left = `${left}px`;
      ghost.style.width = `${Math.max(
        3,
        timeUsToPixels(drag.originalEndUs, pixelsPerSecond) - left,
      )}px`;
    }
    if (dragGhostDeltaRef.current) {
      const action = drag.scope === 'trim-start'
        ? 'ĐẦU'
        : drag.scope === 'trim-end'
        ? 'HẾT'
        : 'DỜI';
      dragGhostDeltaRef.current.textContent = `${action} ${drag.deltaUs >= 0 ? '+' : ''}${Math.round(drag.deltaUs / 1_000)} ms`;
    }
    if (dragGhostSnapRef.current) {
      dragGhostSnapRef.current.hidden = drag.snappedMarkerUs === null;
    }
  };

  const clearDragVisuals = (restore?: Timeline, lineId?: string) => {
    if (restore && lineId) paintDragLine(restore, lineId);
    panelRef.current?.classList.remove('dragging');
    panelRef.current?.classList.remove('trimming');
    if (dragGhostRef.current) dragGhostRef.current.hidden = true;
    if (dragGhostSnapRef.current) dragGhostSnapRef.current.hidden = true;
    dragElementsRef.current?.tokens.forEach((element) => {
      element.style.willChange = '';
    });
    if (dragElementsRef.current?.label) {
      dragElementsRef.current.label.style.willChange = '';
    }
  };

  const beginItemDrag = (
    event: ReactPointerEvent<HTMLElement>,
    line: LineTiming,
    tokenId: string,
    scope: SongRollDrag['scope'],
    lane: number,
  ) => {
    if (!event.isPrimary || event.button !== 0) return;
    const token = line.tokens.find((candidate) => candidate.id === tokenId);
    if (!token || line.locked || (scope !== 'line' && token.locked)) return;
    const tokenIndex = line.tokens.findIndex((candidate) => candidate.id === tokenId);
    const previousToken = tokenIndex > 0 ? line.tokens[tokenIndex - 1] : null;
    const nextToken = tokenIndex + 1 < line.tokens.length ? line.tokens[tokenIndex + 1] : null;
    if (
      (scope === 'trim-start' && previousToken?.locked)
      || (scope === 'trim-end' && nextToken?.locked)
      || (scope === 'token' && (previousToken?.locked || nextToken?.locked))
    ) return;
    onSilence();
    if (dragFrameRef.current !== null) {
      window.cancelAnimationFrame(dragFrameRef.current);
      dragFrameRef.current = null;
    }
    dragInputRef.current = null;
    const lineElements = Array.from(
      canvasRef.current?.querySelectorAll<HTMLElement>('[data-song-line-id]') ?? [],
    ).filter((element) => element.dataset.songLineId === line.id);
    const label = lineElements.find((element) => element.dataset.songKind === 'label') ?? null;
    const tokens = new Map<string, HTMLElement>();
    lineElements.forEach((element) => {
      const elementTokenId = element.dataset.songTokenId;
      if (element.dataset.songKind === 'token' && elementTokenId) {
        tokens.set(elementTokenId, element);
        element.style.willChange = 'left, width';
      }
    });
    if (label) label.style.willChange = 'left';
    dragElementsRef.current = { label, tokens };
    const startUs = scope === 'line' ? line.start_us : token.start_us;
    const endUs = scope === 'line' ? line.end_us : token.end_us;
    dragRef.current = {
      scope,
      lineId: line.id,
      tokenId,
      lane,
      startX: event.clientX,
      originalStartUs: startUs,
      originalEndUs: endUs,
      deltaUs: 0,
      snappedMarkerUs: null,
      started: false,
      baseTimeline: timeline,
      latestTimeline: timeline,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.stopPropagation();
  };

  const applyDragPosition = (clientX: number, shiftKey: boolean) => {
    const drag = dragRef.current;
    if (!drag) return;
    const movedPixels = clientX - drag.startX;
    const frameUs = Math.max(
      1,
      Math.round(1_000_000 * drag.baseTimeline.fps_denominator / drag.baseTimeline.fps_numerator),
    );
    const snapUs = shiftKey ? frameUs : 10_000;
    let requestedDeltaUs = Math.round(
      ((movedPixels / pixelsPerSecond) * 1_000_000) / snapUs,
    ) * snapUs;
    const originalEdgeUs = drag.scope === 'trim-end'
      ? drag.originalEndUs
      : drag.originalStartUs;
    const requestedEdgeUs = originalEdgeUs + requestedDeltaUs;
    const markerSnapThresholdUs = Math.min(
      250_000,
      Math.max(60_000, Math.round((8 / pixelsPerSecond) * 1_000_000)),
    );
    const nearestMarker = nearestMarkerWithin(
      markers,
      requestedEdgeUs,
      markerSnapThresholdUs,
    );
    if (nearestMarker !== null) requestedDeltaUs = nearestMarker - originalEdgeUs;
    const next = drag.scope === 'line'
      ? moveLineBy(drag.baseTimeline, drag.lineId, requestedDeltaUs)
      : drag.scope === 'token'
      ? moveTokenBy(drag.baseTimeline, drag.lineId, drag.tokenId, requestedDeltaUs)
      : trimTokenEdge(
        drag.baseTimeline,
        drag.lineId,
        drag.tokenId,
        drag.scope === 'trim-start' ? 'start' : 'end',
        originalEdgeUs + requestedDeltaUs,
      );
    const nextLine = next.lines.find((line) => line.id === drag.lineId);
    const nextToken = nextLine?.tokens.find((token) => token.id === drag.tokenId);
    const nextEdgeUs = drag.scope === 'line'
      ? nextLine?.start_us
      : drag.scope === 'trim-end'
      ? nextToken?.end_us
      : nextToken?.start_us;
    drag.deltaUs = (nextEdgeUs ?? originalEdgeUs) - originalEdgeUs;
    drag.snappedMarkerUs = nearestMarker !== null && nextEdgeUs === nearestMarker
      ? nearestMarker
      : null;
    drag.latestTimeline = next;
    paintDragLine(next, drag.lineId);
    paintDragGhost(drag);
  };

  const moveItemDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const movedPixels = event.clientX - drag.startX;
    if (!drag.started && Math.abs(movedPixels) < 3) return;
    if (!drag.started) {
      drag.started = true;
      setFollowPlayhead(false);
      onBeginEdit();
    }
    dragInputRef.current = { clientX: event.clientX, shiftKey: event.shiftKey };
    if (dragFrameRef.current === null) {
      dragFrameRef.current = window.requestAnimationFrame(() => {
        dragFrameRef.current = null;
        const input = dragInputRef.current;
        if (input) applyDragPosition(input.clientX, input.shiftKey);
      });
    }
    event.preventDefault();
    event.stopPropagation();
  };

  const endItemDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.started) {
      if (dragFrameRef.current !== null) {
        window.cancelAnimationFrame(dragFrameRef.current);
        dragFrameRef.current = null;
      }
      dragInputRef.current = { clientX: event.clientX, shiftKey: event.shiftKey };
      applyDragPosition(event.clientX, event.shiftKey);
      suppressClickRef.current = true;
      setTimeout(() => { suppressClickRef.current = false; }, 0);
      onCommitTimeline(drag.latestTimeline, drag.lineId, drag.tokenId);
      event.preventDefault();
      event.stopPropagation();
    }
    clearDragVisuals();
    dragRef.current = null;
    dragInputRef.current = null;
    dragElementsRef.current = null;
  };

  const cancelItemDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (dragFrameRef.current !== null) {
      window.cancelAnimationFrame(dragFrameRef.current);
      dragFrameRef.current = null;
    }
    clearDragVisuals(drag.baseTimeline, drag.lineId);
    dragRef.current = null;
    dragInputRef.current = null;
    dragElementsRef.current = null;
    event.preventDefault();
    event.stopPropagation();
  };

  const selectItem = (lineId: string, tokenId: string) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    const token = timeline.lines
      .find((line) => line.id === lineId)
      ?.tokens.find((candidate) => candidate.id === tokenId);
    if (token) {
      setLyricDraft(token.text);
      setLyricNotice(`Đang chọn “${token.text}”. Chỉnh nội dung hoặc chèn từ/câu quanh vị trí này.`);
    }
    setFollowPlayhead(true);
    onSelectToken(lineId, tokenId);
  };

  const copySelectedLine = () => {
    if (!structureLine) {
      setLyricNotice('Hãy chọn một câu trước khi sao chép.');
      return;
    }
    setCopiedLine(structuredClone(structureLine));
    setLyricNotice(`Đã copy nguyên câu “${structureLine.text}” cùng nhịp từng từ.`);
  };

  const pasteCopiedLine = () => {
    onSilence();
    if (!copiedLine) {
      setLyricNotice('Hãy copy một câu trước.');
      return;
    }
    const existingIds = new Set(timeline.lines.map((line) => line.id));
    const next = pasteTimelineLineAt(timeline, copiedLine, nowUs);
    const pastedLine = next.lines.find((line) => !existingIds.has(line.id));
    const pastedToken = pastedLine?.tokens[0];
    if (next === timeline || !pastedLine || !pastedToken) {
      setLyricNotice('Không còn đủ thời gian trong video để dán trọn câu đã copy.');
      return;
    }
    onStructureChange(next, pastedLine.id, pastedToken.id);
    setLyricDraft(pastedToken.text);
    setLyricNotice(
      `Đã dán “${pastedLine.text}” tại ${formatFrameTime(pastedLine.start_us, timeline)} · mọi nhịp và frame từng từ được giữ nguyên.`,
    );
  };

  const applyStructureAction = (
    action: 'rename' | 'insert-before' | 'insert-after' | 'delete-token' | 'insert-line' | 'delete-line',
  ) => {
    onSilence();
    const value = lyricDraft.normalize('NFC').trim();
    if (!structureLine) {
      setLyricNotice('Hãy chọn một câu trước.');
      return;
    }
    if (structureLocked && action !== 'insert-line') {
      setLyricNotice('Câu này đang khóa. Hãy mở khóa trước khi sửa nội dung.');
      return;
    }
    if (!value && !['delete-token', 'delete-line'].includes(action)) {
      setLyricNotice('Hãy nhập nội dung cần thêm hoặc sửa.');
      return;
    }
    if (
      ['rename', 'insert-before', 'insert-after', 'delete-token'].includes(action)
      && !structureToken
    ) {
      setLyricNotice('Hãy bấm đúng từ cần chỉnh trên timeline trước.');
      return;
    }

    let next = timeline;
    let nextLineId = structureLine.id;
    let nextTokenId = structureToken?.id ?? structureLine.tokens[0]?.id ?? '';
    if (action === 'rename' && structureToken) {
      next = editTimelineTokenText(timeline, structureLine.id, structureToken.id, value);
    } else if ((action === 'insert-before' || action === 'insert-after') && structureToken) {
      const oldIndex = structureLine.tokens.findIndex((token) => token.id === structureToken.id);
      next = insertTimelineToken(
        timeline,
        structureLine.id,
        structureToken.id,
        value,
        action === 'insert-before' ? 'before' : 'after',
      );
      const changedLine = next.lines.find((line) => line.id === structureLine.id);
      const insertedIndex = action === 'insert-before' ? oldIndex : oldIndex + 1;
      nextTokenId = changedLine?.tokens[insertedIndex]?.id ?? nextTokenId;
    } else if (action === 'delete-token' && structureToken) {
      const oldIndex = structureLine.tokens.findIndex((token) => token.id === structureToken.id);
      next = deleteTimelineToken(timeline, structureLine.id, structureToken.id);
      const changedLine = next.lines.find((line) => line.id === structureLine.id);
      nextTokenId = changedLine?.tokens[Math.min(oldIndex, (changedLine?.tokens.length ?? 1) - 1)]?.id
        ?? nextTokenId;
    } else if (action === 'insert-line') {
      const oldIndex = timeline.lines.findIndex((line) => line.id === structureLine.id);
      next = insertTimelineLine(
        timeline,
        structureLine.id,
        value,
        Math.max(nowUs, structureLine.end_us),
      );
      const insertedLine = next.lines[oldIndex + 1];
      nextLineId = insertedLine?.id ?? nextLineId;
      nextTokenId = insertedLine?.tokens[0]?.id ?? nextTokenId;
    } else if (action === 'delete-line') {
      const oldIndex = timeline.lines.findIndex((line) => line.id === structureLine.id);
      next = deleteTimelineLine(timeline, structureLine.id);
      const selectedLine = next.lines[Math.min(oldIndex, next.lines.length - 1)];
      nextLineId = selectedLine?.id ?? nextLineId;
      nextTokenId = selectedLine?.tokens[0]?.id ?? nextTokenId;
    }

    if (next === timeline || !nextLineId || !nextTokenId) {
      setLyricNotice(
        action === 'insert-line'
          ? 'Không còn đủ thời gian trong video để thêm câu tại vị trí này.'
          : action === 'delete-line'
          ? 'Bài phải còn ít nhất một câu và không thể xóa câu đang khóa.'
          : action === 'delete-token'
          ? 'Câu phải còn ít nhất một từ và không thể sửa phần đang khóa.'
          : 'Mỗi ô chỉ nhận một từ và từ hiện tại phải đủ ít nhất hai frame để chia timing.',
      );
      return;
    }
    const selectedLine = next.lines.find((line) => line.id === nextLineId);
    const selectedToken = selectedLine?.tokens.find((token) => token.id === nextTokenId);
    onStructureChange(next, nextLineId, nextTokenId);
    setLyricDraft(selectedToken?.text ?? '');
    setLyricNotice('Đã áp dụng bằng tay · timing mới cần nghe và duyệt · có thể Undo trước khi lưu.');
  };

  return (
    <section
      ref={panelRef}
      className={`song-roll-panel ${expanded ? 'expanded' : ''} ${showDetails || expanded ? 'details-open' : ''}`}
      role={expanded ? 'dialog' : undefined}
      aria-modal={expanded || undefined}
      aria-label={expanded ? 'Timeline Karaoke toàn màn hình' : undefined}
    >
      <div className="song-roll-header">
        <div className="song-roll-heading-copy">
          <div className="song-roll-title-row">
            <span>MAGNETIC LYRIC TIMELINE</span>
            <div className={`song-roll-autosave ${autosaveStatus}`} role="status" aria-live="polite" title={autosaveError ?? undefined}>
              <i />
              <strong>{autosaveStatus === 'error'
                ? 'Lỗi tự lưu'
                : autosaveStatus === 'saving'
                ? 'Đang lưu…'
                : autosaveStatus === 'pending'
                ? 'Chờ lưu…'
                : `Đã lưu · r${autosaveRevision} ✓`}</strong>
              {autosaveStatus === 'error' && <button type="button" onClick={onRetryAutosave}>Thử lại</button>}
            </div>
          </div>
          <small>Kéo để dời · kéo mép để co giãn · Shift = bắt frame</small>
        </div>
        <div className="song-roll-controls">
          <div className="song-roll-control-row primary">
            <div className="song-roll-edit-actions" role="group" aria-label="Hoàn tác, khóa và duyệt câu đang chọn">
              <button disabled={!canUndo} type="button" onClick={onUndo}>↶ Undo</button>
              <button disabled={!canRedo} type="button" onClick={onRedo}>↷ Redo</button>
              <button className={lineLocked ? 'active' : ''} type="button" onClick={onToggleLock}>{lineLocked ? 'Mở khóa' : 'Khóa câu'}</button>
              <button className={`approve ${lineVerified ? 'active' : ''}`} type="button" onClick={onVerifyLine}>{lineVerified ? 'Đã duyệt ✓' : 'Duyệt câu'}</button>
            </div>
            <div className="song-roll-copy-actions" role="group" aria-label="Sao chép, dán và xoá nguyên câu cùng nhịp">
              <button
                className={copiedLine?.id === structureLine?.id ? 'active' : ''}
                disabled={!structureLine}
                type="button"
                title="Copy nguyên lời, độ dài từng từ và đường quét"
                onClick={copySelectedLine}
              >{copiedLine?.id === structureLine?.id ? '✓ Đã copy câu + nhịp' : '⧉ Copy câu + nhịp'}</button>
              <button
                className="paste"
                disabled={!copiedLine}
                type="button"
                title={copiedLine ? `Dán “${copiedLine.text}” bắt đầu đúng tại frame đầu phát` : 'Hãy copy một câu trước'}
                onClick={pasteCopiedLine}
              >＋ Dán ở đầu phát</button>
              <button
                className="danger"
                disabled={!structureLine || structureLocked || timeline.lines.length === 1}
                type="button"
                title="Xoá câu đang chọn; có thể Undo"
                onClick={() => applyStructureAction('delete-line')}
              >Xoá câu ↶</button>
            </div>
          </div>
          <div className="song-roll-control-row secondary">
            <button
              className="song-roll-expand"
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((current) => !current)}
            >
              {expanded ? '↙ Thu nhỏ · Esc' : '⛶ Mở rộng'}
            </button>
            <button
              className={`song-roll-details-toggle ${showDetails ? 'active' : ''}`}
              type="button"
              aria-expanded={showDetails}
              onClick={() => setShowDetails((current) => !current)}
            >
              ◫ Chi tiết
            </button>
            <div className="song-roll-drag-mode" role="group" aria-label="Chọn kiểu kéo timing">
              <button className={dragMode === 'token' ? 'active' : ''} type="button" onClick={() => setDragMode('token')}>Kéo từ</button>
              <button className={dragMode === 'line' ? 'active' : ''} type="button" onClick={() => setDragMode('line')}>Kéo câu</button>
            </div>
            <button className="song-roll-add-marker" type="button" onClick={addMarker}>＋ Mốc {formatTime(nowUs)}</button>
            <div className="song-roll-zoom-actions" role="group" aria-label="Thu phóng và theo dõi Timeline">
              <button disabled={zoomIndex === 0} type="button" onClick={() => changeZoom(zoomIndex - 1)}>−</button>
              <b>{fitPixelsPerSecond === null ? `${pixelsPerSecond} px/s` : 'FIT'}</b>
              <button disabled={zoomIndex === SONG_ROLL_ZOOM.length - 1} type="button" onClick={() => changeZoom(zoomIndex + 1)}>+</button>
              <button className={fitPixelsPerSecond !== null ? 'active' : ''} title="Vừa toàn bài · Shift+Z" type="button" onClick={fitTimeline}>⇥ Fit</button>
              <button className={followPlayhead ? 'following' : ''} type="button" onClick={() => setFollowPlayhead((current) => !current)}>{followPlayhead ? '◎ Bám đầu phát' : '↔ Cuộn tự do'}</button>
            </div>
          </div>
        </div>
      </div>
      <div className="song-roll-editor-stage">
        <div className="song-roll-lane-labels" aria-hidden="true">
          <span className="lane-ruler">TC</span>
          <span className="lane-label lane-0"><i /> L1 <small>HIỆN TẠI</small></span>
          <span className="lane-label lane-1"><i /> L2 <small>KẾ TIẾP</small></span>
        </div>
        <div
          className="song-roll-viewport"
          ref={viewportRef}
          onWheel={handleWheel}
          onKeyDown={(event) => {
            if (event.shiftKey && event.key.toLowerCase() === 'z') {
              event.preventDefault();
              fitTimeline();
            }
          }}
          tabIndex={0}
          aria-label="Timeline cuộn ngang toàn bộ bài Karaoke"
        >
          <div
            ref={canvasRef}
            className="song-roll-canvas"
            style={{ width: `${contentWidth}px`, '--timeline-grid-px': `${Math.max(24, tickSeconds * pixelsPerSecond)}px` } as CSSProperties}
            onClick={seekCanvas}
          >
          <div className="song-roll-ruler">
            {ticks.map((seconds) => (
              <span style={{ left: `${seconds * pixelsPerSecond}px` }} key={seconds}>
                <i />{formatRollTime(seconds)}
              </span>
            ))}
          </div>
          {markers.map((markerUs) => (
            <button
              className="song-roll-marker"
              style={{ left: `${timeUsToPixels(markerUs, pixelsPerSecond)}px` }}
              type="button"
              title={`Mốc tay ${formatTime(markerUs)} · bấm để nghe`}
              aria-label={`Nghe tại mốc tay ${formatTime(markerUs)}`}
              key={markerUs}
              onClick={(event) => {
                event.stopPropagation();
                setFollowPlayhead(true);
                onSeek(markerUs);
              }}
            >
              <b>{formatTime(markerUs)}</b>
            </button>
          ))}
          {timeline.lines.flatMap((line, lineIndex) => {
            const lane = lineIndex % 2;
            const lineActive = line.id === activeLineId;
            const labelLeft = timeUsToPixels(line.start_us, pixelsPerSecond);
            return [
              <button
                className={`song-roll-line-number lane-${lane} ${lineActive ? 'active' : ''} ${line.locked ? 'locked' : ''}`}
                style={{ left: `${labelLeft}px` }}
                type="button"
                tabIndex={lineActive ? 0 : -1}
                title={`Kéo để dời nguyên câu ${lineIndex + 1}: ${line.text}`}
                aria-label={`Kéo nguyên câu ${lineIndex + 1}: ${line.text}`}
                data-song-line-id={line.id}
                data-song-kind="label"
                key={`${line.id}-label`}
                onPointerDown={(event) => beginItemDrag(event, line, line.tokens[0]?.id ?? line.id, 'line', lane)}
                onPointerMove={moveItemDrag}
                onPointerUp={endItemDrag}
                onPointerCancel={cancelItemDrag}
                onClick={() => line.tokens[0] && selectItem(line.id, line.tokens[0].id)}
              >
                {String(lineIndex + 1).padStart(2, '0')}
              </button>,
              ...line.tokens.map((token, tokenIndex) => {
                const left = timeUsToPixels(token.start_us, pixelsPerSecond);
                const width = Math.max(
                  3,
                  timeUsToPixels(token.end_us, pixelsPerSecond) - left,
                );
                const selected = lineActive && token.id === selectedTokenId;
                const canTrimStart = !line.locked
                  && !token.locked
                  && !line.tokens[tokenIndex - 1]?.locked;
                const canTrimEnd = !line.locked
                  && !token.locked
                  && !line.tokens[tokenIndex + 1]?.locked;
                return (
                  <button
                    className={`song-roll-token lane-${lane} ${width < 28 ? 'short-token' : ''} ${tokenIndex === 0 ? 'first-token' : ''} ${tokenIndex === line.tokens.length - 1 ? 'last-token' : ''} ${lineActive ? 'active-line' : ''} ${selected ? 'selected' : ''} ${token.verified ? 'verified' : 'review'}`}
                    style={{ left: `${left}px`, width: `${width}px` }}
                    type="button"
                    tabIndex={selected ? 0 : -1}
                    aria-pressed={selected}
                    title={`${line.text} · ${token.text} · ${formatTime(token.start_us)} — ${formatTime(token.end_us)} · kéo thân để dời, kéo mép để co giãn`}
                    aria-label={`Mở và nghe từ ${token.text} trong câu ${line.text}`}
                    data-song-line-id={line.id}
                    data-song-kind="token"
                    data-song-token-id={token.id}
                    key={`${line.id}-${token.id}`}
                    onPointerDown={(event) => beginItemDrag(event, line, token.id, dragMode, lane)}
                    onPointerMove={moveItemDrag}
                    onPointerUp={endItemDrag}
                    onPointerCancel={cancelItemDrag}
                    onClick={(event) => {
                      event.stopPropagation();
                      selectItem(line.id, token.id);
                    }}
                  >
                    <span className="song-roll-token-text">{width >= 22 ? token.text : ''}</span>
                    {selected && (canTrimStart || canTrimEnd) && (
                      <>
                        {canTrimStart && (
                          <span
                            className="song-roll-trim-handle start"
                            aria-hidden="true"
                            onPointerDown={(event) => beginItemDrag(event, line, token.id, 'trim-start', lane)}
                            onPointerMove={moveItemDrag}
                            onPointerUp={endItemDrag}
                            onPointerCancel={cancelItemDrag}
                          />
                        )}
                        {canTrimEnd && (
                          <span
                            className="song-roll-trim-handle end"
                            aria-hidden="true"
                            onPointerDown={(event) => beginItemDrag(event, line, token.id, 'trim-end', lane)}
                            onPointerMove={moveItemDrag}
                            onPointerUp={endItemDrag}
                            onPointerCancel={cancelItemDrag}
                          />
                        )}
                      </>
                    )}
                  </button>
                );
              }),
            ];
          })}
          <span ref={dragGhostRef} className="song-roll-drag-origin" hidden>
            <b ref={dragGhostDeltaRef}>0 ms</b>
            <em ref={dragGhostSnapRef} hidden>BẮT MỐC</em>
          </span>
            <span className="song-roll-playhead" style={{ left: `${playheadX}px` }}><b>{formatFrameTime(nowUs, timeline)}</b></span>
          </div>
        </div>
      </div>
      <div className="song-roll-legend"><span><i className="current" /> Câu đang chỉnh</span><span><i className="verified" /> Đã duyệt</span><span><i className="review" /> Cần nghe</span><span><i className="trim" /> Mép co giãn</span><span><i className="origin" /> Vị trí trước khi kéo</span><b>{timeline.lines.length} câu · {timeline.lines.reduce((count, line) => count + line.tokens.length, 0)} từ</b></div>
      {(showDetails || expanded) && (
        <div className="song-roll-detail-drawer">
          {markers.length > 0 && (
            <div className="song-roll-marker-list">
              <span>MỐC TAY</span>
              {markers.map((markerUs) => (
                <div key={markerUs}>
                  <button type="button" onClick={() => onSeek(markerUs)}>{formatTime(markerUs)}</button>
                  <button type="button" aria-label={`Xoá mốc ${formatTime(markerUs)}`} onClick={() => setMarkers((current) => current.filter((value) => value !== markerUs))}>×</button>
                </div>
              ))}
              <button className="clear-markers" type="button" onClick={() => setMarkers([])}>Xoá tất cả mốc</button>
            </div>
          )}
          {transitionControls && (
            <div className="song-roll-transition-slot">
              {transitionControls}
            </div>
          )}
          <div className="song-roll-lyrics-editor">
            <div className="song-roll-lyrics-copy">
              <span>CHỈNH LỜI TRỰC TIẾP</span>
              <strong>{structureToken
                ? `Từ “${structureToken.text}” · câu “${structureLine?.text}”`
                : structureLine
                ? `Câu “${structureLine.text}” · hãy bấm một từ`
                : 'Chưa chọn câu'}</strong>
              <small>{lyricNotice}</small>
            </div>
            <label>
              <span>NỘI DUNG DO BẠN NHẬP</span>
              <input
                value={lyricDraft}
                placeholder="Một từ để sửa/chèn · hoặc cả câu để thêm câu"
                onChange={(event) => setLyricDraft(event.target.value)}
                onFocus={onSilence}
              />
            </label>
            <div className="song-roll-word-actions">
              <button disabled={!structureToken || structureLocked} type="button" onClick={() => applyStructureAction('rename')}>Sửa từ</button>
              <button disabled={!structureToken || structureLocked} type="button" onClick={() => applyStructureAction('insert-before')}>＋ Trước</button>
              <button disabled={!structureToken || structureLocked} type="button" onClick={() => applyStructureAction('insert-after')}>＋ Sau</button>
              <button className="danger" disabled={!structureToken || structureLocked || structureLine?.tokens.length === 1} type="button" onClick={() => applyStructureAction('delete-token')}>Xoá từ ↶</button>
            </div>
            <div className="song-roll-line-actions">
              <button type="button" onClick={() => applyStructureAction('insert-line')}>＋ Thêm câu sau · ưu tiên đầu phát</button>
              <button className="danger" disabled={!structureLine || structureLocked || timeline.lines.length === 1} type="button" onClick={() => applyStructureAction('delete-line')}>Xoá câu ↶</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function EngineStatus({ label, ready, detail }: { label: string; ready: boolean; detail: string }) {
  return <div className={`engine-status ${ready ? 'ready' : ''}`}><span>{ready ? '✓' : '○'}</span><div><strong>{label}</strong><small>{ready ? detail : 'Chưa cài · sẽ dùng fallback'}</small></div></div>;
}

function ExportList({
  projectId,
  artifacts,
  rendering,
  onReload,
  onError,
}: {
  projectId: string;
  artifacts: Artifact[];
  rendering: boolean;
  onReload: () => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [deleteCandidateId, setDeleteCandidateId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const exports = videoExportArtifacts(artifacts);

  const downloadExport = (artifact: Artifact) => {
    const link = document.createElement('a');
    link.href = assetUrl(exportDownloadPath(artifact.url));
    link.download = artifact.label;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  const deleteExport = async (artifact: Artifact) => {
    setDeletingId(artifact.id);
    onError(null);
    try {
      await api<{ deleted: boolean }>(exportDeletePath(projectId, artifact.label), {
        method: 'DELETE',
      });
      setDeleteCandidateId(null);
      await onReload();
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : 'Không xoá được video xuất.');
    } finally {
      setDeletingId(null);
    }
  };

  if (rendering) {
    return (
      <p className="no-exports">
        Đang xuất và kiểm tra toàn bộ video… Nút tải xuống chỉ xuất hiện sau khi QA hoàn tất.
      </p>
    );
  }
  if (!exports.length) return <p className="no-exports">Chưa có video xuất.</p>;
  return (
    <div className="export-list">
      {exports.map((artifact) => {
        const confirming = deleteCandidateId === artifact.id;
        const deleting = deletingId === artifact.id;
        return (
          <article className={`export-item ${confirming ? 'is-confirming' : ''}`} key={artifact.id}>
            <div className="export-item-head">
              <span>MP4</span>
              <div><strong>{artifact.label}</strong><small>{formatBytes(artifact.bytes)}</small></div>
            </div>
            {confirming ? (
              <div className="export-delete-confirmation">
                <span>Xoá vĩnh viễn file này?</span>
                <button className="export-delete-confirm" disabled={deleting} type="button" onClick={() => void deleteExport(artifact)}>{deleting ? 'Đang xoá…' : 'Xoá ngay'}</button>
                <button disabled={deleting} type="button" onClick={() => setDeleteCandidateId(null)}>Huỷ</button>
              </div>
            ) : (
              <div className="export-file-actions">
                <button className="export-download-button" type="button" onClick={() => downloadExport(artifact)} aria-label={`Tải xuống ${artifact.label}`}><span aria-hidden="true">↓</span>Tải xuống</button>
                <button className="export-delete-button" disabled={Boolean(deletingId)} type="button" onClick={() => setDeleteCandidateId(artifact.id)}>Xoá</button>
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}

function LoadingStudio() {
  return <div className="loading-studio"><span /><strong>Đang mở project…</strong><small>Đọc timeline và artifact local</small></div>;
}

function formatFrameTime(
  us: number,
  timeline: Timeline,
  fpsNumerator = timeline.fps_numerator,
  fpsDenominator = timeline.fps_denominator,
): string {
  const fps = fpsNumerator / Math.max(1, fpsDenominator);
  const nominalFps = Math.max(1, Math.round(fps));
  const totalFrames = Math.max(
    0,
    Math.floor((us * fpsNumerator) / (1_000_000 * Math.max(1, fpsDenominator))),
  );
  const frames = totalFrames % nominalFps;
  const totalSeconds = Math.floor(totalFrames / nominalFps);
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3_600);
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}:${String(frames).padStart(2, '0')}`;
}

function stateIndex(state: Project['state']): number {
  if (state === 'IMPORTED' || state === 'FAILED') return 1;
  if (state === 'SEPARATED') return 2;
  if (state === 'ALIGNED' || state === 'NEEDS_REVIEW') return 3;
  if (state === 'VERIFIED') return 4;
  return 5;
}

function stateLabel(state: Project['state']): string {
  const labels: Record<Project['state'], string> = {
    IMPORTED: 'Đã nhập nguồn',
    SEPARATED: 'Đã tách giọng',
    ALIGNED: 'Đã căn lời',
    NEEDS_REVIEW: 'Cần kiểm duyệt',
    VERIFIED: 'Verified',
    RENDERED: 'Đã xuất video',
    FAILED: 'Cần xử lý lỗi',
  };
  return labels[state];
}

function batchStatusLabel(status: BatchSongStatus): string {
  const labels: Record<BatchSongStatus, string> = {
    ready: 'Sẵn sàng',
    uploading: 'Đang nhập',
    queued: 'Đang xếp hàng',
    processing: 'Đang phân tích',
    complete: 'Hoàn tất',
    failed: 'Có lỗi',
  };
  return labels[status];
}

function transitionGapLabel(deltaUs: number): string {
  const milliseconds = Math.round(deltaUs / 1_000);
  if (milliseconds < 0) return `CHỒNG ${Math.abs(milliseconds)} ms`;
  if (milliseconds === 0) return 'LIỀN 0 ms';
  return `NGHỈ ${milliseconds} ms`;
}

function formatRollTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.round(bytes / 1_000)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}
