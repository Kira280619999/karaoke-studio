# Karaoke Studio Architecture

## Boundary

Karaoke Studio is a local application. The web UI connects only to a FastAPI service on `127.0.0.1`; the API launches isolated worker subprocesses for long-running media tasks. The application has no telemetry or upload path.

```text
React/Vinext UI :3000
        │ HTTP + SSE
FastAPI API :8000 ── SQLite WAL + UUID project directories
        │ subprocess
Worker ─┬─ ffprobe / FFmpeg
        ├─ Separator adapters
        ├─ Aligner adapters
        └─ RGBA renderer + QA
```

## Durable project layout

```text
.karaoke-studio-data/                 # macOS/Linux default
%LOCALAPPDATA%\KaraokeStudio\         # Windows default
  karaoke-studio.sqlite3
  models/
  projects/proj_<uuid>/
    source/                 immutable user inputs
    timeline.json           current TimelineV1
    history/                previous revisions
    work/
      ingest-manifest.json
      alignment-manifest.json
      alignment-evidence.json
      alignment-report.json
      alignment-cache/       cached CTC emissions by model/stem/checksum/window
      proxy.mp4
      mix.wav
      stems/manifest.json
      waveforms.json
    exports/
    qa/<export-name>/QA_REPORT.json
    logs/
```

Every reusable stage validates its manifest or checksum before reuse. A repeated `/process` call reuses a live job or starts a new worker that continues from valid artifacts. Stale active jobs are marked failed before resume. Cancellation targets the worker process group on POSIX and the complete worker tree through `taskkill /T` on Windows, so FFmpeg children are also stopped.

## Platform shells

The product core is shared across platforms; there is no fork of the timeline,
database, renderer or verification policy.

- macOS/Linux use `scripts/dev.sh`, POSIX process groups and MPS when PyTorch exposes it.
- Windows 10/11 x64 uses `scripts/dev.ps1`, native PowerShell process ownership,
  Win32 read-only PID checks and a short `%LOCALAPPDATA%\KaraokeStudio` data path.
- Alignment device selection is `CUDA → MPS → CPU` in automatic mode. The
  `KARAOKE_STUDIO_TORCH_DEVICE` override accepts `auto`, `cuda`, `mps` or `cpu`
  and fails closed when an explicitly requested accelerator is unavailable.

## Adapters

- `Separator`: Audio Separator/Mel-Band RoFormer, HTDemucs FT and center-cancel fallback.
- `Aligner`: `EnsembleSongAligner` runs the pinned Vietnamese singing lyric CTC model plus an independent speech CTC model on up to two production vocal stems. Cached 20-second emissions with two-second overlap cover the full song; a global monotonic path rejects repeated-chorus outliers before robust token/grapheme consensus and onset/sustain refinement. `AutomaticSweepCritic` then re-listens for at most three bounded passes, corrects only diverse CTC control-point consensus, preserves human timing and fails closed when onset/sustain evidence does not converge. Energy-aware is the fail-closed fallback.
- `Renderer`: Pillow RGBA frames piped to FFmpeg. Timeline 1.1 stores an optional integer `SweepCurveV1` on every token. React preview and MP4 export share the same piecewise interpolation over absolute microseconds and `line_progress_ppm`, eliminating renderer/editor motion drift.

Timeline 1.0 remains readable and is upgraded on save. The adapter contracts stay independent from the canonical timeline, so another language, model or renderer can be added without changing project ownership or visible lyric text.

## Verification boundary

Automatic output is evidence, not approval. `AlignmentEvidenceV1` stores every model/stem boundary, selected boundary, acoustic support, disagreement and reason code outside canonical `TimelineV1`. `verify_project` rejects the transition unless the instrumental was explicitly confirmed and both timeline validation and current-revision alignment evidence contain no blocking issue. Rendering itself is operator-controlled: review warnings never block export. An instrumental export made before verification is named `unverified-final`, receives `PASS_WITH_WARNINGS` plus reason codes in `QA_REPORT.json`, and leaves the project in `NEEDS_REVIEW`; only a verified instrumental render advances it to `RENDERED`.

During review the proxy and singer-reference export use `work/mix.wav`, preserving the singer for accurate cue decisions. Separation is non-destructive and prepares candidates only. Once a selected instrumental exists, the operator may export it immediately; confirmation and timing review affect the quality label, not permission to render. Manual Precision intentionally does not call AI: selecting the first token loops the tail of the previous line through the new onset, and the operator controls start/end handles directly. The diagnostics suggestion endpoint remains read-only and detached from this manual workflow.

## Security

- Upload filenames are reduced to safe basenames and bounded length.
- Artifact serving resolves the requested path and rejects anything outside the project directory.
- LRC upload size is capped; unsupported source/background extensions fail closed.
- SQLite uses foreign keys, WAL, busy timeout and optimistic timeline revisions.
- Media, weights, caches, databases and output formats are excluded from Git.
