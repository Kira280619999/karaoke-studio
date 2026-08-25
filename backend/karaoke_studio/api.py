from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import __version__
from .alignment import (
    LYRIC_MODEL_ID,
    LYRIC_MODEL_LICENSE,
    LYRIC_MODEL_REVISION,
    suggest_line_timing,
)
from .backgrounds import (
    BACKGROUND_EXTENSIONS,
    MAX_BACKGROUND_ASSETS,
    BackgroundPlanV1,
    build_background_plan,
    refresh_background_plan,
    save_background_plan,
)
from .db import RevisionConflict, Store, now_iso
from .ensemble import evidence_review_issues
from .fonts import KARAOKE_FONTS, is_karaoke_font_id, normalize_karaoke_font_id
from .jobs import JobManager
from .media import MediaError, probe, safe_filename, sha256_file, tool_capabilities
from .media import run as run_media
from .models import (
    AlignmentEvidenceV1,
    Artifact,
    InstrumentalSelection,
    JobState,
    ProcessRequest,
    ProjectRecord,
    ProjectState,
    RenderRequest,
    TimelinePatch,
    TimingSuggestionRequest,
    TimingSuggestionResponse,
)
from .motion import remap_timeline_sweep_font, resolve_font
from .polarformer import polarformer_dependencies_available, polarformer_model_path
from .quicktime import is_legacy_quicktime_hfr_export
from .rendering import render_preview_png
from .separation import load_candidates
from .settings import Settings
from .styles import (
    KARAOKE_COLORS,
    is_karaoke_color_id,
    normalize_karaoke_color_id,
)
from .timeline import validate_timeline
from .timeline_source import (
    SUPPORTED_TIMELINE_EXTENSIONS,
    TimelineSourceError,
    parse_timeline_source,
)

MAX_TIMELINE_BYTES = 10 * 1024 * 1024
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def create_app(custom_settings: Settings | None = None) -> FastAPI:
    settings = custom_settings or Settings.load()
    settings.ensure()
    store = Store(settings)
    jobs = JobManager(store, settings)
    app = FastAPI(title="Karaoke Studio API", version=__version__)
    app.state.settings = settings
    app.state.store = store
    app.state.jobs = jobs
    origins = {settings.frontend_origin, "http://localhost:3000", "http://127.0.0.1:3000"}
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "local_only": True, "version": __version__}

    @app.get("/api/assets/karaoke-font")
    def karaoke_font() -> FileResponse:
        return FileResponse(resolve_font(settings), media_type="font/ttf")

    @app.get("/api/assets/karaoke-font/{font_id}")
    def selected_karaoke_font(font_id: str) -> FileResponse:
        if not is_karaoke_font_id(font_id):
            raise HTTPException(404, "Font Karaoke không tồn tại.")
        return FileResponse(resolve_font(settings, font_id), media_type="font/ttf")

    @app.get("/api/system/capabilities")
    def capabilities() -> dict:
        return {
            **tool_capabilities(settings),
            "polarformer_fp32": polarformer_dependencies_available(),
            "polarformer_model_downloaded": polarformer_model_path(settings.data_dir).is_file(),
            "data_dir": str(settings.data_dir),
            "vietnamese_model": {
                "id": "nguyenvulebinh/wav2vec2-base-vietnamese-250h",
                "license": "CC-BY-NC-4.0",
                "bundled": False,
            },
            "vietnamese_lyric_model": {
                "id": LYRIC_MODEL_ID,
                "revision": LYRIC_MODEL_REVISION,
                "license": LYRIC_MODEL_LICENSE,
                "bundled": False,
                "singing_specific": True,
            },
            "karaoke_fonts": [
                {"id": spec.id, "label": spec.label} for spec in KARAOKE_FONTS
            ],
            "karaoke_colors": [
                {"id": spec.id, "label": spec.label} for spec in KARAOKE_COLORS
            ],
        }

    @app.get("/api/projects", response_model=list[ProjectRecord])
    def list_projects() -> list[ProjectRecord]:
        return store.list_projects()

    @app.post("/api/projects", response_model=ProjectRecord)
    async def create_project(
        video: UploadFile = File(...),
        lrc: UploadFile | None = File(None),
        background: list[UploadFile] | None = File(None),
        timeline_text: str = Form(""),
        title: str = Form(""),
        artist: str = Form(""),
        background_mode: str = Form("original"),
        karaoke_font: str = Form("noto_sans"),
        karaoke_color: str = Form("yellow"),
    ) -> ProjectRecord:
        video_suffix = Path(video.filename or "").suffix.casefold()
        if video_suffix not in MEDIA_EXTENSIONS:
            raise HTTPException(400, "Video phải là MP4/MOV/MKV/WEBM/M4V.")
        has_pasted_timeline = bool(timeline_text.strip())
        if lrc is not None and has_pasted_timeline:
            raise HTTPException(400, "Chỉ chọn một nguồn timeline: file hoặc nội dung dán.")
        if lrc is None and not has_pasted_timeline:
            raise HTTPException(400, "Hãy chọn file timeline hoặc dán nội dung có timestamp.")
        if lrc is not None:
            timeline_suffix = Path(lrc.filename or "").suffix.casefold()
            if timeline_suffix not in SUPPORTED_TIMELINE_EXTENSIONS:
                raise HTTPException(400, "Timeline hỗ trợ LRC, SRT, VTT hoặc TXT.")
        if background_mode not in {"original", "custom"}:
            raise HTTPException(400, "background_mode không hợp lệ.")
        if not is_karaoke_font_id(karaoke_font):
            raise HTTPException(400, "karaoke_font không hợp lệ.")
        if not is_karaoke_color_id(karaoke_color):
            raise HTTPException(400, "karaoke_color không hợp lệ.")
        background_uploads = background or []
        if background_mode == "custom" and not background_uploads:
            raise HTTPException(400, "Chế độ custom yêu cầu ít nhất một ảnh hoặc video nền.")
        if len(background_uploads) > MAX_BACKGROUND_ASSETS:
            raise HTTPException(400, f"Tối đa {MAX_BACKGROUND_ASSETS} ảnh/video nền.")
        if any(
            Path(upload.filename or "").suffix.casefold() not in BACKGROUND_EXTENSIONS
            for upload in background_uploads
        ):
            raise HTTPException(400, "Có background không được hỗ trợ.")

        project_id = f"proj_{uuid4().hex[:16]}"
        project_dir = store.project_dir(project_id)
        source_dir = project_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=False)
        source_name = safe_filename(video.filename or "input.mp4", f"input{video_suffix}")
        lrc_name = (
            safe_filename(lrc.filename or "lyrics.lrc", "lyrics.lrc")
            if lrc is not None
            else "pasted-timeline.txt"
        )
        source_path = source_dir / source_name
        lrc_path = source_dir / lrc_name
        background_name: str | None = None
        background_paths: list[Path] = []
        try:
            await _stream_upload(video, source_path)
            if lrc is not None:
                timeline_bytes = await lrc.read(MAX_TIMELINE_BYTES + 1)
                if len(timeline_bytes) > MAX_TIMELINE_BYTES:
                    raise HTTPException(413, "File timeline vượt quá 10 MB.")
                lrc_text = timeline_bytes.decode("utf-8-sig")
            else:
                timeline_bytes = timeline_text.encode("utf-8")
                if len(timeline_bytes) > MAX_TIMELINE_BYTES:
                    raise HTTPException(413, "Nội dung timeline vượt quá 10 MB.")
                lrc_text = timeline_text
            lrc_path.write_text(lrc_text, encoding="utf-8")
            for index, upload in enumerate(background_uploads):
                suffix = Path(upload.filename or "").suffix.casefold()
                original_name = safe_filename(
                    upload.filename or f"background{suffix}", f"background{suffix}"
                )
                stored_name = safe_filename(
                    f"background-{index + 1:02d}-{original_name}",
                    f"background-{index + 1:02d}{suffix}",
                )
                destination = source_dir / stored_name
                await _stream_upload(upload, destination)
                background_paths.append(destination)
            if background_paths:
                background_name = background_paths[0].name
            info = probe(source_path, settings)
            timeline = parse_timeline_source(lrc_text, info.duration_us, filename=lrc_name)
            timeline.metadata["karaoke_font"] = normalize_karaoke_font_id(karaoke_font)
            timeline.metadata["karaoke_color"] = normalize_karaoke_color_id(karaoke_color)
            if background_paths:
                plan = build_background_plan(background_paths, timeline, settings)
                save_background_plan(project_dir, plan)
            timestamp = now_iso()
            record = ProjectRecord(
                id=project_id,
                title=title.strip() or Path(source_name).stem,
                artist=artist.strip(),
                state=ProjectState.IMPORTED,
                created_at=timestamp,
                updated_at=timestamp,
                source_name=source_name,
                lrc_name=lrc_name,
                background_name=background_name,
                background_mode=background_mode,
                source_sha256=sha256_file(source_path),
                duration_us=info.duration_us,
                width=info.width,
                height=info.height,
                fps=info.fps,
                has_audio=info.has_audio,
            )
            store.add_project(record)
            store.save_timeline(project_id, timeline)
            return record
        except HTTPException:
            shutil.rmtree(project_dir, ignore_errors=True)
            raise
        except (UnicodeDecodeError, TimelineSourceError, MediaError) as exc:
            shutil.rmtree(project_dir, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}", response_model=ProjectRecord)
    def get_project(project_id: str) -> ProjectRecord:
        return _require_project(store, project_id)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, str | bool]:
        _require_project(store, project_id)
        active_job = store.get_active_job(project_id)
        if active_job is not None:
            jobs.cancel(active_job.id)

        job_ids = store.job_ids_for_project(project_id)
        project_dir = store.project_dir(project_id)
        deleting_dir = settings.data_dir / ".deleting" / f"{project_id}-{uuid4().hex[:8]}"
        staged = False
        if project_dir.exists():
            deleting_dir.parent.mkdir(exist_ok=True)
            project_dir.replace(deleting_dir)
            staged = True
        try:
            if not store.delete_project(project_id):
                raise HTTPException(404, "Không tìm thấy project.")
        except Exception:
            if staged and deleting_dir.exists() and not project_dir.exists():
                deleting_dir.replace(project_dir)
            raise

        if staged:
            shutil.rmtree(deleting_dir, ignore_errors=True)
        for job_id in job_ids:
            (settings.data_dir / "job-events" / f"{job_id}.jsonl").unlink(missing_ok=True)
            (settings.data_dir / "worker-logs" / f"{job_id}.log").unlink(missing_ok=True)
        return {"deleted": True, "project_id": project_id}

    @app.post("/api/projects/{project_id}/process")
    def start_process(project_id: str, request: ProcessRequest) -> dict:
        project = _require_project(store, project_id)
        if not project.has_audio:
            raise HTTPException(409, "Video không có audio stream.")
        try:
            job = jobs.start(project_id, "process", request.model_dump())
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job": job}

    @app.get("/api/projects/{project_id}/timeline")
    def get_timeline(project_id: str) -> dict:
        _require_project(store, project_id)
        timeline = store.load_timeline(project_id)
        evidence = _load_alignment_evidence(store.project_dir(project_id))
        return {
            "timeline": timeline,
            "issues": validate_timeline(timeline) + evidence_review_issues(timeline, evidence),
        }

    @app.patch("/api/projects/{project_id}/timeline")
    def patch_timeline(project_id: str, patch: TimelinePatch) -> dict:
        project = _require_project(store, project_id)
        current_timeline = store.load_timeline(project_id)
        current_font = normalize_karaoke_font_id(
            current_timeline.metadata.get("karaoke_font")
        )
        selected_font = normalize_karaoke_font_id(
            patch.timeline.metadata.get("karaoke_font")
        )
        patch.timeline.metadata["karaoke_font"] = selected_font
        patch.timeline.metadata["karaoke_color"] = normalize_karaoke_color_id(
            patch.timeline.metadata.get("karaoke_color")
        )
        if selected_font != current_font:
            patch.timeline = remap_timeline_sweep_font(
                patch.timeline, resolve_font(settings, selected_font)
            )
        errors = [issue for issue in validate_timeline(patch.timeline) if issue.severity == "error"]
        if errors:
            raise HTTPException(422, detail=[issue.model_dump(mode="json") for issue in errors])
        try:
            timeline = store.save_timeline(
                project_id, patch.timeline, expected_revision=patch.expected_revision
            )
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        evidence = _load_alignment_evidence(store.project_dir(project_id))
        verification_errors = [
            issue
            for issue in (
                validate_timeline(timeline, require_verified=True)
                + evidence_review_issues(timeline, evidence, require_verified=True)
            )
            if issue.severity == "error"
        ]
        if project.instrumental_confirmed and not verification_errors:
            store.update_project(project_id, state=ProjectState.VERIFIED)
        elif project.state in {ProjectState.VERIFIED, ProjectState.RENDERED}:
            store.update_project(project_id, state=ProjectState.NEEDS_REVIEW)
        return {
            "timeline": timeline,
            "issues": validate_timeline(timeline) + evidence_review_issues(timeline, evidence),
        }

    @app.get(
        "/api/projects/{project_id}/alignment-evidence",
        response_model=AlignmentEvidenceV1,
    )
    def alignment_evidence(
        project_id: str, line_id: str | None = None
    ) -> AlignmentEvidenceV1:
        _require_project(store, project_id)
        evidence = _load_alignment_evidence(store.project_dir(project_id))
        if evidence is None:
            raise HTTPException(404, "Alignment evidence chưa được tạo.")
        if line_id is None:
            return evidence
        selected = [item for item in evidence.tokens if item.line_id == line_id]
        if not selected:
            raise HTTPException(404, "Không tìm thấy evidence của câu lyric.")
        return evidence.model_copy(update={"tokens": selected})

    @app.post(
        "/api/projects/{project_id}/timing-suggestions",
        response_model=TimingSuggestionResponse,
    )
    def timing_suggestions(
        project_id: str, request: TimingSuggestionRequest
    ) -> TimingSuggestionResponse:
        _require_project(store, project_id)
        project_dir = store.project_dir(project_id)
        timeline = store.load_timeline(project_id)
        line = next((item for item in timeline.lines if item.id == request.line_id), None)
        if line is None:
            raise HTTPException(404, "Không tìm thấy câu lyric cần gợi ý.")
        license_accepted = request.accept_vietnamese_model_license or _ctc_was_accepted(
            project_dir
        )
        try:
            vocal_inputs = _smart_timing_vocals(
                project_dir, settings, include_secondary=license_accepted
            )
            return suggest_line_timing(
                line,
                vocal_inputs,
                license_accepted,
                settings,
                alignment_profile=request.alignment_profile,
                motion_profile=request.motion_profile,
                karaoke_font=timeline.metadata.get("karaoke_font"),
            )
        except (FileNotFoundError, ValueError, MediaError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/projects/{project_id}/instrumental", response_model=ProjectRecord)
    def select_instrumental(project_id: str, selection: InstrumentalSelection) -> ProjectRecord:
        _require_project(store, project_id)
        candidates = {candidate.id for candidate in load_candidates(store.project_dir(project_id))}
        if selection.candidate_id not in candidates:
            raise HTTPException(404, "Không tìm thấy stem candidate.")
        timeline = store.load_timeline(project_id)
        evidence = _load_alignment_evidence(store.project_dir(project_id))
        verification_errors = [
            issue
            for issue in (
                validate_timeline(timeline, require_verified=True)
                + evidence_review_issues(timeline, evidence, require_verified=True)
            )
            if issue.severity == "error"
        ]
        return store.update_project(
            project_id,
            selected_instrumental=selection.candidate_id,
            instrumental_confirmed=selection.confirmed,
            state=(
                ProjectState.VERIFIED
                if selection.confirmed and not verification_errors
                else ProjectState.NEEDS_REVIEW
            ),
        )

    @app.post("/api/projects/{project_id}/verify", response_model=ProjectRecord)
    def verify_project(project_id: str) -> ProjectRecord:
        project = _require_project(store, project_id)
        if not project.instrumental_confirmed:
            raise HTTPException(409, "Bạn phải nghe A/B và xác nhận instrumental.")
        timeline = store.load_timeline(project_id)
        evidence = _load_alignment_evidence(store.project_dir(project_id))
        issues = [
            issue
            for issue in (
                validate_timeline(timeline, require_verified=True)
                + evidence_review_issues(timeline, evidence, require_verified=True)
            )
            if issue.severity == "error"
        ]
        if issues:
            raise HTTPException(409, detail=[issue.model_dump(mode="json") for issue in issues])
        return store.update_project(project_id, state=ProjectState.VERIFIED)

    @app.post("/api/projects/{project_id}/renders")
    def start_render(project_id: str, request: RenderRequest) -> dict:
        project = _require_project(store, project_id)
        timeline = store.load_timeline(project_id)
        if (
            request.expected_timeline_revision is not None
            and request.expected_timeline_revision != timeline.revision
        ):
            raise HTTPException(
                409,
                "Timeline vừa thay đổi. Hãy chờ tự lưu hoàn tất rồi xuất lại.",
            )
        if request.mode == "final" and not project.selected_instrumental:
            raise HTTPException(409, "Chưa có instrumental để xuất bản loại giọng.")
        if request.mode == "final" and not project.instrumental_confirmed:
            raise HTTPException(
                409,
                "Hãy nghe và xác nhận instrumental trong tab Audio trước khi xuất Final.",
            )
        render_options = request.model_dump()
        if request.mode == "final":
            if (
                request.expected_instrumental_id is not None
                and request.expected_instrumental_id != project.selected_instrumental
            ):
                raise HTTPException(
                    409,
                    "Instrumental đã đổi sau khi Viewer tải dữ liệu. Hãy nghe lại rồi xuất Final.",
                )
            # Bind the worker to the exact confirmed stem that exists at queue time.
            # Older clients may omit the field, so the server remains authoritative.
            render_options["expected_instrumental_id"] = project.selected_instrumental
        try:
            job = jobs.start(project_id, "render", render_options)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job": job}

    @app.get("/api/projects/{project_id}/artifacts", response_model=list[Artifact])
    def artifacts(project_id: str) -> list[Artifact]:
        _require_project(store, project_id)
        project_dir = store.project_dir(project_id)
        result: list[Artifact] = []
        roots = [
            ("proxy", project_dir / "work"),
            ("stem", project_dir / "work" / "stems"),
            ("export", project_dir / "exports"),
            ("qa", project_dir / "qa"),
        ]
        for kind, root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                relative_parts = path.relative_to(root).parts
                if kind == "proxy" and (
                    "stems" in relative_parts or "render-staging" in relative_parts
                ):
                    continue
                if path.is_file() and path.suffix.casefold() in {
                    ".mp4",
                    ".wav",
                    ".json",
                    ".jpg",
                    ".png",
                }:
                    if kind == "export" and is_legacy_quicktime_hfr_export(path.name):
                        continue
                    relative = path.relative_to(project_dir).as_posix()
                    result.append(
                        Artifact(
                            id=relative,
                            label=path.name,
                            kind=kind,
                            url=f"/api/projects/{project_id}/files/{quote(relative)}",
                            bytes=path.stat().st_size,
                        )
                    )
        return result

    @app.get("/api/projects/{project_id}/waveform")
    def waveform(project_id: str) -> JSONResponse:
        _require_project(store, project_id)
        path = store.project_dir(project_id) / "work" / "waveforms.json"
        if not path.exists():
            raise HTTPException(404, "Waveform chưa được tạo.")
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/projects/{project_id}/preview")
    def preview(project_id: str, time_us: int = 0) -> Response:
        _require_project(store, project_id)
        timeline = store.load_timeline(project_id)
        time_us = max(0, min(timeline.duration_us - 1, time_us))
        return Response(render_preview_png(timeline, settings, time_us), media_type="image/png")

    @app.get(
        "/api/projects/{project_id}/background-plan",
        response_model=BackgroundPlanV1,
    )
    def background_plan(project_id: str) -> BackgroundPlanV1:
        project = _require_project(store, project_id)
        if project.background_mode != "custom":
            raise HTTPException(404, "Project đang dùng video gốc.")
        try:
            plan = refresh_background_plan(
                project,
                store.load_timeline(project_id),
                store.project_dir(project_id),
                settings,
            )
        except MediaError as exc:
            raise HTTPException(409, str(exc)) from exc
        if plan is None:
            raise HTTPException(404, "Project chưa có lịch nền thay thế.")
        assets = [
            asset.model_copy(
                update={
                    "url": (
                        f"/api/projects/{project_id}/files/source/"
                        f"{quote(asset.filename)}"
                    )
                }
            )
            for asset in plan.assets
        ]
        return plan.model_copy(update={"assets": assets})

    @app.get("/api/projects/{project_id}/files/{relative_path:path}")
    def project_file(
        project_id: str,
        relative_path: str,
        download: bool = False,
    ) -> FileResponse:
        _require_project(store, project_id)
        project_dir = store.project_dir(project_id).resolve()
        path = (project_dir / relative_path).resolve()
        if project_dir not in path.parents or not path.is_file():
            raise HTTPException(404, "Artifact không tồn tại.")
        if download:
            return FileResponse(
                path,
                filename=path.name,
                content_disposition_type="attachment",
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        return FileResponse(path)

    @app.delete("/api/projects/{project_id}/exports/{filename:path}")
    def delete_export(project_id: str, filename: str) -> dict:
        _require_project(store, project_id)
        project_dir = store.project_dir(project_id).resolve()
        export_dir = (project_dir / "exports").resolve()
        path = (export_dir / filename).resolve()
        if (
            not filename
            or Path(filename).name != filename
            or path.parent != export_dir
            or path.suffix.casefold() != ".mp4"
            or not path.is_file()
        ):
            raise HTTPException(404, "Video xuất không tồn tại.")
        size = path.stat().st_size
        path.unlink()
        return {"deleted": True, "filename": filename, "bytes": size}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "Không tìm thấy job.")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            return jobs.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Không tìm thấy job.") from exc

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        if not store.get_job(job_id):
            raise HTTPException(404, "Không tìm thấy job.")
        last_id = int(request.headers.get("last-event-id", "-1"))

        async def stream() -> AsyncIterator[str]:
            sequence = last_id
            while True:
                for event in jobs.read_events(job_id, sequence):
                    sequence = event["sequence"]
                    yield f"id: {sequence}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                job = store.get_job(job_id)
                if not job:
                    return
                if job.state in {JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED}:
                    yield f"event: terminal\ndata: {job.model_dump_json()}\n\n"
                    return
                if await request.is_disconnected():
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.75)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


async def _stream_upload(upload: UploadFile, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".upload")
    with temporary.open("wb") as handle:
        while chunk := await upload.read(4 * 1024 * 1024):
            handle.write(chunk)
    temporary.replace(destination)


def _require_project(store: Store, project_id: str) -> ProjectRecord:
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(404, "Không tìm thấy project.")
    return project


def _ctc_was_accepted(project_dir: Path) -> bool:
    manifest = project_dir / "work" / "alignment-manifest.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    model = payload.get("model")
    return isinstance(model, dict) and model.get("accepted") is True


def _load_alignment_evidence(project_dir: Path) -> AlignmentEvidenceV1 | None:
    path = project_dir / "work" / "alignment-evidence.json"
    if not path.is_file():
        return None
    try:
        return AlignmentEvidenceV1.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _smart_timing_vocals(
    project_dir: Path, settings: Settings, include_secondary: bool
) -> list[tuple[str, Path]]:
    try:
        candidates = load_candidates(project_dir)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("Project chưa có vocal stem; hãy chạy phân tích trước.") from exc
    production = [candidate for candidate in candidates if candidate.production_grade]
    selected = (production or candidates)[: 2 if include_secondary else 1]
    if not selected:
        raise FileNotFoundError("Project chưa có vocal stem để nghe và căn lời.")

    work = project_dir / "work"
    results: list[tuple[str, Path]] = []
    for index, candidate in enumerate(selected):
        target = (
            work / "alignment-vocal.wav"
            if index == 0
            else work / f"alignment-vocal-{candidate.id}.wav"
        )
        if not target.is_file():
            run_media(
                [
                    settings.ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-i",
                    str(Path(candidate.vocals)),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(target),
                ]
            )
        results.append((candidate.id, target))
    return results


app = create_app()


def run() -> None:
    import uvicorn

    settings = Settings.load()
    uvicorn.run("karaoke_studio.api:app", host=settings.host, port=settings.port, reload=False)
