from __future__ import annotations

import json
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from .media import probe, run, sha256_file
from .models import ProjectRecord, ProjectState, TimelineV1
from .motion import line_progress_ppm
from .settings import Settings
from .styles import normalize_karaoke_color_id
from .timeline import validate_timeline


def run_final_qa(
    output: Path,
    project: ProjectRecord,
    timeline: TimelineV1,
    project_dir: Path,
    settings: Settings,
    mode: str,
) -> dict:
    timing_verified = project.state in {ProjectState.VERIFIED, ProjectState.RENDERED}
    timeline_issues = validate_timeline(
        timeline,
        require_verified=mode == "final" and timing_verified,
    )
    output_info = probe(output, settings)
    duration_delta_us = abs(output_info.duration_us - timeline.duration_us)
    frame_us = round(1_000_000 / max(1.0, output_info.fps_float))
    av_duration_delta_us = (
        abs(output_info.duration_us - output_info.audio_duration_us)
        if output_info.audio_duration_us is not None
        else None
    )
    qa_timeline = timeline.model_copy(deep=True)
    try:
        output_fps = Fraction(output_info.fps)
        qa_timeline.fps_numerator = output_fps.numerator
        qa_timeline.fps_denominator = output_fps.denominator
    except (ValueError, ZeroDivisionError):
        pass
    motion_report = motion_qa(qa_timeline)
    run([settings.ffmpeg, "-v", "error", "-i", str(output), "-f", "null", "-"])
    qa_dir = project_dir / "qa" / output.stem
    qa_dir.mkdir(parents=True, exist_ok=True)
    representative = []
    if timeline.lines:
        indexes = sorted({0, len(timeline.lines) // 2, len(timeline.lines) - 1})
        for index in indexes:
            line = timeline.lines[index]
            timestamp = max(0, min(timeline.duration_us - 1, (line.start_us + line.end_us) // 2))
            screenshot = qa_dir / f"line-{index + 1:04d}.jpg"
            run(
                [
                    settings.ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-ss",
                    f"{timestamp / 1_000_000:.6f}",
                    "-i",
                    str(output),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(screenshot),
                ]
            )
            representative.append(str(screenshot.relative_to(project_dir)))
    blocking = [
        issue.model_dump(mode="json") for issue in timeline_issues if issue.severity == "error"
    ]
    alignment_manifest_path = project_dir / "work" / "alignment-manifest.json"
    stems_manifest_path = project_dir / "work" / "stems" / "manifest.json"
    technical_pass = (
        not blocking
        and motion_report["status"] == "PASS"
        and duration_delta_us <= frame_us
        and av_duration_delta_us is not None
        and av_duration_delta_us <= frame_us
    )
    advisory_reasons: list[str] = []
    if mode == "final" and not timing_verified:
        advisory_reasons.append("TIMING_NOT_VERIFIED")
    if mode == "final" and not project.instrumental_confirmed:
        advisory_reasons.append("INSTRUMENTAL_NOT_CONFIRMED")
    status = (
        "FAIL"
        if not technical_pass
        else "PASS_WITH_WARNINGS"
        if advisory_reasons
        else "PASS"
    )
    report = {
        "schema_version": "1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "status": status,
        "project_id": project.id,
        "source_sha256": project.source_sha256,
        "output": str(output.relative_to(project_dir)),
        "output_sha256": sha256_file(output),
        "full_decode": "PASS",
        "duration_us": output_info.duration_us,
        "expected_duration_us": timeline.duration_us,
        "duration_delta_us": duration_delta_us,
        "frame_tolerance_us": frame_us,
        "output_fps": output_info.fps_float,
        "output_fps_ratio": output_info.fps,
        "timeline_revision": timeline.revision,
        "karaoke_color": normalize_karaoke_color_id(
            str(timeline.metadata.get("karaoke_color", ""))
        ),
        "audio_duration_us": output_info.audio_duration_us,
        "av_duration_delta_us": av_duration_delta_us,
        "timeline_issues": [issue.model_dump(mode="json") for issue in timeline_issues],
        "motion_qa": motion_report,
        "audio_mode": (
            "source_mix_with_vocal"
            if mode == "draft"
            else "confirmed_instrumental"
            if project.instrumental_confirmed
            else "unconfirmed_instrumental"
        ),
        "instrumental_candidate": project.selected_instrumental,
        "instrumental_confirmed": project.instrumental_confirmed,
        "timing_verified_at_render": timing_verified,
        "advisory_reasons": advisory_reasons,
        "representative_frames": representative,
        "toolchain": {
            "karaoke_studio": "0.1.0",
            "renderer": "pillow-rgba-sweep-v2",
            "ffmpeg": run([settings.ffmpeg, "-version"]).stdout.splitlines()[0],
        },
        "alignment_manifest": json.loads(alignment_manifest_path.read_text(encoding="utf-8"))
        if alignment_manifest_path.exists()
        else None,
        "stems_manifest": json.loads(stems_manifest_path.read_text(encoding="utf-8"))
        if stems_manifest_path.exists()
        else None,
        "alignment_evidence": "work/alignment-evidence.json"
        if (project_dir / "work" / "alignment-evidence.json").is_file()
        else None,
    }
    report_path = qa_dir / "QA_REPORT.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["status"] == "FAIL":
        raise RuntimeError(f"Final QA thất bại; xem {report_path}.")
    return report


def motion_qa(timeline: TimelineV1) -> dict[str, int | str]:
    frame_us = round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator)
    backward_steps = 0
    missing_samples = 0
    maximum_step_ppm = 0
    sampled_frames = 0
    for line in timeline.lines:
        previous: int | None = None
        for now_us in range(line.start_us, line.end_us + 1, max(1, frame_us)):
            progress = line_progress_ppm(line, min(line.end_us, now_us))
            sampled_frames += 1
            if progress is None:
                missing_samples += 1
                previous = None
                continue
            if previous is not None:
                if progress < previous:
                    backward_steps += 1
                maximum_step_ppm = max(maximum_step_ppm, progress - previous)
            previous = progress
    return {
        "status": "PASS" if backward_steps == 0 and missing_samples == 0 else "FAIL",
        "sampled_frames": sampled_frames,
        "backward_steps": backward_steps,
        "missing_curve_samples": missing_samples,
        "maximum_step_ppm": maximum_step_ppm,
    }


# Backward-compatible internal name retained for existing integrations/tests.
_motion_qa = motion_qa
