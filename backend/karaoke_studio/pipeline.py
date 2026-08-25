from __future__ import annotations

import json
from pathlib import Path

from .alignment import (
    LYRIC_CTC_SPEC,
    SPEECH_CTC_SPEC,
    VIETNAMESE_MODEL_ID,
    VIETNAMESE_MODEL_LICENSE,
    VIETNAMESE_MODEL_REVISION,
)
from .backgrounds import refresh_background_plan
from .db import Store
from .ensemble import align_timeline_ensemble, alignment_policy
from .final_audio import prepare_final_audio_project
from .media import extract_audio, make_proxy, probe, run, sha256_file, waveform_envelope
from .models import (
    AlignmentEvidenceV1,
    JobState,
    LineTiming,
    ProjectState,
    TimelineV1,
    TimingSource,
)
from .qa import motion_qa, run_final_qa
from .rendering import make_mp4_share_copy, render_video
from .separation import load_candidates, separate_candidates, separator_request_signature
from .settings import Settings
from .timeline_source import parse_timeline_source

INGEST_MANIFEST_VERSION = 3
ALIGNMENT_MANIFEST_VERSION = 18


class JobCancelled(RuntimeError):
    pass


class JobContext:
    def __init__(self, job_id: str, store: Store):
        self.job_id = job_id
        self.store = store
        self.log_path = store.settings.data_dir / "job-events" / f"{job_id}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, progress: float, message: str, level: str = "info") -> None:
        job = self.store.get_job(self.job_id)
        if not job or job.state == JobState.CANCELLED:
            raise JobCancelled("Job đã bị hủy.")
        progress = max(0.0, min(1.0, progress))
        updated = self.store.update_job(self.job_id, progress=progress, message=message)
        event = {
            "id": int(self.log_path.stat().st_size if self.log_path.exists() else 0),
            "job_id": self.job_id,
            "state": updated.state.value,
            "progress": progress,
            "message": message,
            "level": level,
            "updated_at": updated.updated_at,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def process_project(job_id: str, project_id: str, options: dict, settings: Settings) -> None:
    store = Store(settings)
    context = JobContext(job_id, store)
    project = store.get_project(project_id)
    if not project:
        raise KeyError(project_id)
    project_dir = store.project_dir(project_id)
    source = project_dir / "source" / project.source_name
    work = project_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    if not project.has_audio:
        raise RuntimeError("Video không có audio stream để tách giọng.")

    source_info = probe(source, settings)
    timeline = store.load_timeline(project_id)
    if timeline.duration_us != source_info.duration_us:
        context.emit(
            0.01,
            "Đang sửa timeline theo toàn bộ thời lượng audio của bài hát…",
        )
        lrc_text = (project_dir / "source" / project.lrc_name).read_text(encoding="utf-8")
        rebuilt = parse_timeline_source(
            lrc_text, source_info.duration_us, filename=project.lrc_name
        )
        timeline = _preserve_reviewed_timing(timeline, rebuilt)
        timeline = store.save_timeline(
            project_id, timeline, expected_revision=store.load_timeline(project_id).revision
        )
        project = store.update_project(project_id, duration_us=source_info.duration_us)

    context.emit(0.03, "Đang chuẩn hóa audio và tạo proxy CFR…")
    mix = work / "mix.wav"
    alignment_mix = work / "alignment.wav"
    proxy = work / "proxy.mp4"
    ingest_manifest = work / "ingest-manifest.json"
    valid_ingest = False
    if ingest_manifest.exists() and mix.exists() and alignment_mix.exists() and proxy.exists():
        payload = json.loads(ingest_manifest.read_text(encoding="utf-8"))
        valid_ingest = (
            payload.get("version") == INGEST_MANIFEST_VERSION
            and payload.get("source_sha256") == project.source_sha256
        )
    if not valid_ingest:
        mix, alignment_mix = extract_audio(source, work, settings)
        make_proxy(source, proxy, settings)
        ingest_manifest.write_text(
            json.dumps(
                {
                    "version": INGEST_MANIFEST_VERSION,
                    "source_sha256": project.source_sha256,
                    "proxy_audio": "aac-192k-48khz-stereo",
                    "presentation_duration_us": source_info.duration_us,
                    "video_duration_us": source_info.video_duration_us,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    quality = options.get("quality", "highest")
    candidates_manifest = work / "stems" / "manifest.json"
    previous_selected_sha256: str | None = None
    if candidates_manifest.exists() and project.selected_instrumental:
        try:
            previous_selected = next(
                (
                    candidate
                    for candidate in load_candidates(project_dir)
                    if candidate.id == project.selected_instrumental
                ),
                None,
            )
            if previous_selected and Path(previous_selected.instrumental).is_file():
                previous_selected_sha256 = sha256_file(Path(previous_selected.instrumental))
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            previous_selected_sha256 = None
    reusable_stems = False
    if candidates_manifest.exists():
        manifest_payload = json.loads(candidates_manifest.read_text(encoding="utf-8"))
        reusable_stems = manifest_payload.get("request") == separator_request_signature(quality)
    if reusable_stems:
        candidates = load_candidates(project_dir)
        reusable_stems = all(
            Path(candidate.instrumental).exists() and Path(candidate.vocals).exists()
            for candidate in candidates
        )
    if reusable_stems:
        context.emit(0.42, "Đã khôi phục stem candidate đúng engine từ lần chạy trước.")
    else:
        candidates = separate_candidates(mix, project_dir, settings, quality, context.emit)
    analysis_candidates = [candidate for candidate in candidates if candidate.analysis_eligible]
    if not analysis_candidates:
        raise RuntimeError("Không còn stem được phép dùng cho căn lời.")
    preferred = next(
        (candidate for candidate in analysis_candidates if candidate.production_grade),
        analysis_candidates[0],
    )
    selected_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.id == project.selected_instrumental
        ),
        None,
    )
    keep_selection = bool(
        selected_candidate
        and previous_selected_sha256
        and sha256_file(Path(selected_candidate.instrumental)) == previous_selected_sha256
    )
    selected_instrumental = project.selected_instrumental if keep_selection else preferred.id
    project = store.update_project(
        project_id,
        state=ProjectState.SEPARATED,
        selected_instrumental=selected_instrumental,
        selected_instrumental_sha256=(
            selected_candidate.pcm_sha256 if keep_selection and selected_candidate else None
        ),
        instrumental_confirmed=project.instrumental_confirmed if keep_selection else False,
        error=None,
    )

    alignment_manifest_path = work / "alignment-manifest.json"
    alignment_payload: dict[str, object] = {}
    if alignment_manifest_path.exists():
        alignment_payload = json.loads(alignment_manifest_path.read_text(encoding="utf-8"))
    previous_model = alignment_payload.get("model", {})
    previously_accepted = isinstance(previous_model, dict) and previous_model.get("accepted") is True
    accepted_license = bool(options.get("accept_vietnamese_model_license", False)) or previously_accepted
    alignment_profile = options.get("alignment_profile", "maximum")
    if alignment_profile not in {"maximum", "balanced", "fast"}:
        alignment_profile = "maximum"
    motion_profile = options.get("motion_profile", "vocal_hybrid")
    if motion_profile not in {"vocal_hybrid", "vocal_only", "linear"}:
        motion_profile = "vocal_hybrid"
    alignment_candidates = [preferred]
    if accepted_license and alignment_profile != "fast":
        alignment_candidates.extend(
            candidate
            for candidate in analysis_candidates
            if candidate.production_grade and candidate.id != preferred.id
        )
    alignment_candidates = alignment_candidates[:2]
    vocal_inputs: dict[str, Path] = {}
    for index, candidate in enumerate(alignment_candidates):
        vocal_alignment = (
            work / "alignment-vocal.wav"
            if index == 0
            else work / f"alignment-vocal-{candidate.id}.wav"
        )
        run(
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
                str(vocal_alignment),
            ]
        )
        vocal_inputs[candidate.id] = vocal_alignment
    timeline = store.load_timeline(project_id)
    vocal_inputs_sha256 = {
        candidate_id: sha256_file(path) for candidate_id, path in vocal_inputs.items()
    }
    vocal_sha256 = vocal_inputs_sha256[preferred.id]
    active_policy = alignment_policy(alignment_profile) if accepted_license else "energy-valley-v2"
    evidence_path = work / "alignment-evidence.json"
    report_path = work / "alignment-report.json"
    reusable_alignment = (
        alignment_payload.get("manifest_version") == ALIGNMENT_MANIFEST_VERSION
        and alignment_payload.get("timeline_revision") == timeline.revision
        and alignment_payload.get("vocal_sha256") == vocal_sha256
        and alignment_payload.get("vocal_inputs_sha256") == vocal_inputs_sha256
        and alignment_payload.get("alignment_policy") == active_policy
        and alignment_payload.get("alignment_profile") == alignment_profile
        and alignment_payload.get("motion_profile") == motion_profile
        and isinstance(previous_model, dict)
        and previous_model.get("accepted") is accepted_license
        and evidence_path.is_file()
        and report_path.is_file()
    )
    if reusable_alignment:
        context.emit(0.81, "Đã khôi phục alignment đúng revision và checksum từ lần chạy trước.")
        alignment_report = json.loads(report_path.read_text(encoding="utf-8"))
        alignment_evidence = AlignmentEvidenceV1.model_validate_json(
            evidence_path.read_text(encoding="utf-8")
        )
    else:
        lrc_text = (project_dir / "source" / project.lrc_name).read_text(encoding="utf-8")
        anchored_timeline = parse_timeline_source(
            lrc_text, timeline.duration_us, filename=project.lrc_name
        )
        base_timeline = _preserve_reviewed_timing(timeline, anchored_timeline)
        base_timeline.revision = timeline.revision
        timeline, alignment_evidence, alignment_report = align_timeline_ensemble(
            base_timeline,
            vocal_inputs,
            vocal_inputs_sha256,
            accepted_license,
            alignment_profile,
            context.emit,
            settings,
            work / "alignment-cache",
            motion_profile=motion_profile,
            rhythm_inputs={
                candidate.id: Path(candidate.instrumental)
                for candidate in alignment_candidates
                if Path(candidate.instrumental).is_file()
            },
        )
        timeline = store.save_timeline(project_id, timeline, expected_revision=timeline.revision)
        alignment_evidence.timeline_revision = timeline.revision
        alignment_report["timeline_revision"] = timeline.revision
        alignment_report["motion_qa"] = motion_qa(timeline)
        evidence_path.write_text(
            alignment_evidence.model_dump_json(indent=2), encoding="utf-8"
        )
        report_path.write_text(
            json.dumps(alignment_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    timing_sources = sorted({line.source.value for line in timeline.lines})
    model_snapshots: list[dict] = []
    for spec in (LYRIC_CTC_SPEC, SPEECH_CTC_SPEC):
        model_cache_manifest = settings.data_dir / "models" / f"{spec.cache_name}-manifest.json"
        if model_cache_manifest.exists():
            model_snapshots.append(json.loads(model_cache_manifest.read_text(encoding="utf-8")))
    alignment_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "manifest_version": ALIGNMENT_MANIFEST_VERSION,
                "timeline_revision": timeline.revision,
                "vocal_sha256": vocal_sha256,
                "vocal_inputs_sha256": vocal_inputs_sha256,
                "alignment_policy": active_policy,
                "alignment_profile": alignment_profile,
                "motion_profile": motion_profile,
                "report": alignment_report,
                "timing_sources": timing_sources,
                "model": {
                    "id": VIETNAMESE_MODEL_ID,
                    "revision": VIETNAMESE_MODEL_REVISION,
                    "license": VIETNAMESE_MODEL_LICENSE,
                    "accepted": accepted_license,
                    "used": "vietnamese_ctc" in timing_sources,
                    "snapshots": model_snapshots,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if project.background_mode == "custom":
        context.emit(0.83, "Đang đồng bộ chuyển cảnh nền theo timing lời đã căn…")
        refresh_background_plan(project, timeline, project_dir, settings)
    store.update_project(project_id, state=ProjectState.ALIGNED)

    context.emit(0.84, "Đang tạo waveform cho màn hình kiểm duyệt…")
    waveform_payload: dict[str, object] = {
        "mix": waveform_envelope(mix),
        "candidates": {},
    }
    for candidate in candidates:
        waveform_payload["candidates"][candidate.id] = {  # type: ignore[index]
            "label": candidate.label,
            "production_grade": candidate.production_grade,
            "warning": candidate.warning,
            "quality_profile": candidate.quality_profile,
            "analysis_eligible": candidate.analysis_eligible,
            "export_eligible": candidate.export_eligible,
            "pcm_sha256": candidate.pcm_sha256,
            "signal_path": candidate.signal_path,
            "audio_qa": candidate.audio_qa,
            "instrumental": waveform_envelope(Path(candidate.instrumental)),
            "vocals": waveform_envelope(Path(candidate.vocals)),
        }
    (work / "waveforms.json").write_text(
        json.dumps(waveform_payload, ensure_ascii=False), encoding="utf-8"
    )
    store.update_project(project_id, state=ProjectState.NEEDS_REVIEW)
    context.emit(
        1.0,
        "AI đã phân tích hết bài. Chỉ mở hàng kiểm duyệt cho các điểm còn thiếu tin cậy.",
    )


def _preserve_reviewed_timing(current: TimelineV1, rebuilt: TimelineV1) -> TimelineV1:
    """Keep user-owned style metadata and explicit timing decisions on rebuild."""
    rebuilt.language = current.language
    rebuilt.fps_numerator = current.fps_numerator
    rebuilt.fps_denominator = current.fps_denominator
    rebuilt.metadata = {**rebuilt.metadata, **current.metadata}
    current_by_key = {(line.id, line.text): line for line in current.lines}
    for index, fresh in enumerate(rebuilt.lines):
        previous = current_by_key.get((fresh.id, fresh.text))
        if previous is None or not _line_is_human_protected(previous):
            continue
        valid_line = 0 <= previous.start_us < previous.end_us <= rebuilt.duration_us
        valid_tokens = all(
            previous.start_us <= token.start_us < token.end_us <= previous.end_us
            for token in previous.tokens
        )
        if valid_line and valid_tokens:
            rebuilt.lines[index] = previous.model_copy(deep=True)
    return rebuilt


def _line_is_human_protected(line: LineTiming) -> bool:
    return bool(
        line.locked
        or line.verified
        or any(
            token.locked or token.verified or token.source == TimingSource.MANUAL
            for token in line.tokens
        )
    )


def prepare_final_audio_job(
    job_id: str, project_id: str, options: dict, settings: Settings
) -> None:
    del options
    store = Store(settings)
    context = JobContext(job_id, store)
    prepare_final_audio_project(job_id, project_id, settings, context.emit)


def render_project(job_id: str, project_id: str, options: dict, settings: Settings) -> None:
    store = Store(settings)
    context = JobContext(job_id, store)
    project = store.get_project(project_id)
    if not project:
        raise KeyError(project_id)
    timeline = store.load_timeline(project_id)
    expected_revision = options.get("expected_timeline_revision")
    if expected_revision is not None and timeline.revision != expected_revision:
        raise RuntimeError(
            "Timeline đã thay đổi sau khi xếp hàng render; hãy xuất lại revision mới nhất."
        )
    mode = options.get("mode", "draft")
    if mode == "final" and not project.instrumental_confirmed:
        raise RuntimeError(
            "Instrumental chưa được nghe và xác nhận; không thể xuất Final loại giọng."
        )
    expected_instrumental_id = options.get("expected_instrumental_id")
    expected_instrumental_sha256 = options.get("expected_instrumental_sha256")
    if mode == "final" and not expected_instrumental_id:
        raise RuntimeError(
            "Job Final thiếu khóa instrumental; hãy tạo lại lượt xuất từ Viewer hiện tại."
        )
    if mode == "final" and project.selected_instrumental != expected_instrumental_id:
        raise RuntimeError(
            "Instrumental đã đổi sau khi xếp hàng render; hãy nghe lại và xuất Final mới."
        )
    if mode == "final":
        candidates = {candidate.id: candidate for candidate in load_candidates(store.project_dir(project_id))}
        selected = candidates.get(str(expected_instrumental_id))
        if not selected or not Path(selected.instrumental).is_file():
            raise RuntimeError("Không tìm thấy PCM instrumental đã xác nhận.")
        actual_instrumental_sha256 = sha256_file(Path(selected.instrumental))
        if (
            not expected_instrumental_sha256
            or actual_instrumental_sha256 != expected_instrumental_sha256
            or project.selected_instrumental_sha256 != expected_instrumental_sha256
        ):
            raise RuntimeError(
                "PCM instrumental đã đổi sau khi xác nhận; hãy nghe A/B và xác nhận lại."
            )
    output = render_video(
        project,
        timeline,
        store.project_dir(project_id),
        settings,
        mode,
        options.get("preset", "1080p60"),
        bool(options.get("countdown", True)),
        context.emit,
    )
    report = run_final_qa(
        output,
        project,
        timeline,
        store.project_dir(project_id),
        settings,
        mode,
        expected_instrumental_id=(
            str(expected_instrumental_id) if mode == "final" else None
        ),
        expected_instrumental_sha256=(
            str(expected_instrumental_sha256) if mode == "final" else None
        ),
    )
    outputs = [output]
    if mode == "final" and "mp4_aac320" in options.get(
        "deliveries", ["mov_pcm24", "mp4_aac320"]
    ):
        context.emit(0.94, "Đang tạo MP4 chia sẻ; video được copy, chỉ audio mã hóa AAC 320k…")
        share = make_mp4_share_copy(output, settings)
        run_final_qa(
            share,
            project,
            timeline,
            store.project_dir(project_id),
            settings,
            mode,
            expected_instrumental_id=str(expected_instrumental_id),
            expected_instrumental_sha256=str(expected_instrumental_sha256),
            expected_video_source=output,
        )
        outputs.append(share)
    if project.state == ProjectState.RENDERED:
        next_state = ProjectState.RENDERED
    elif project.state == ProjectState.VERIFIED:
        next_state = ProjectState.RENDERED if mode == "final" else ProjectState.VERIFIED
    else:
        next_state = ProjectState.NEEDS_REVIEW
    store.update_project(project_id, state=next_state)
    context.emit(
        1.0,
        f"Xuất video và QA {report['status']}: " + " · ".join(path.name for path in outputs),
    )
