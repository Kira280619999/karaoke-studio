from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import karaoke_studio.pipeline as pipeline_module
from karaoke_studio.api import create_app
from karaoke_studio.db import Store, now_iso
from karaoke_studio.models import JobRecord, JobState, ProjectState
from karaoke_studio.pipeline import process_project, render_project
from karaoke_studio.separation import AudioSeparatorAdapter, BsPolarformerAdapter

LRC = """[ar:Fixture]
[ti:Ánh sáng]
[00:00.50]Ngài là ánh sáng
[00:02.10]Dẫn con đi
"""


def _import_project(client: TestClient, synthetic_video: Path) -> dict:
    with synthetic_video.open("rb") as video:
        response = client.post(
            "/api/projects",
            data={
                "title": "Ánh sáng",
                "artist": "Fixture",
                "background_mode": "original",
                "karaoke_font": "be_vietnam_pro",
                "karaoke_color": "red",
            },
            files={
                "video": ("fixture.mp4", video, "video/mp4"),
                "lrc": ("fixture.lrc", LRC.encode(), "text/plain"),
            },
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_import_api_and_path_traversal_guard(test_settings, synthetic_video: Path) -> None:
    client = TestClient(create_app(test_settings))
    project = _import_project(client, synthetic_video)
    assert project["state"] == "IMPORTED"
    assert project["has_audio"] is True
    timeline = client.get(f"/api/projects/{project['id']}/timeline").json()["timeline"]
    assert timeline["schema_version"] == "1.1"
    assert timeline["metadata"]["karaoke_font"] == "be_vietnam_pro"
    assert timeline["metadata"]["karaoke_color"] == "red"
    suggestion = client.post(
        f"/api/projects/{project['id']}/timing-suggestions",
        json={"line_id": timeline["lines"][0]["id"]},
    )
    assert suggestion.status_code == 409
    escaped = client.get(f"/api/projects/{project['id']}/files/%2e%2e/%2e%2e/README.md")
    assert escaped.status_code in {404, 422}


def test_artifacts_hide_in_progress_render_staging(
    test_settings, synthetic_video: Path
) -> None:
    client = TestClient(create_app(test_settings))
    project = _import_project(client, synthetic_video)
    project_dir = Store(test_settings).project_dir(project["id"])
    staged = project_dir / "work" / "render-staging" / "job_active" / "partial.mp4"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"partial")
    completed = project_dir / "exports" / "completed.mp4"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_bytes(b"complete")

    response = client.get(f"/api/projects/{project['id']}/artifacts")
    assert response.status_code == 200
    artifacts = response.json()
    assert any(item["label"] == "completed.mp4" for item in artifacts)
    assert all(item["label"] != "partial.mp4" for item in artifacts)


def test_render_project_atomically_publishes_after_qa(
    test_settings, synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(test_settings))
    project_payload = _import_project(client, synthetic_video)
    project_id = project_payload["id"]
    store = Store(test_settings)
    project_dir = store.project_dir(project_id)
    published_output = project_dir / "exports" / "atomic-render.mp4"
    published_output.parent.mkdir(parents=True, exist_ok=True)
    published_output.write_bytes(b"previous-valid-export")
    job = JobRecord(
        id="job_atomic_publish_test",
        project_id=project_id,
        kind="render",
        state=JobState.RUNNING,
        progress=0,
        message="test",
        created_at=now_iso(),
        updated_at=now_iso(),
        options={"mode": "draft", "preset": "source", "countdown": True},
    )
    store.add_job(job)
    qa_observed = False

    def fake_render_video(*args, output_dir: Path | None = None, **kwargs) -> Path:
        assert output_dir is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        staged_output = output_dir / published_output.name
        staged_output.write_bytes(b"new-fully-rendered-export")
        return staged_output

    def fake_run_final_qa(
        output: Path, *args, published_output: Path | None = None, **kwargs
    ) -> dict:
        nonlocal qa_observed
        qa_observed = True
        assert output.read_bytes() == b"new-fully-rendered-export"
        assert published_output is not None
        assert published_output.read_bytes() == b"previous-valid-export"
        return {"status": "PASS"}

    monkeypatch.setattr(pipeline_module, "render_video", fake_render_video)
    monkeypatch.setattr(pipeline_module, "run_final_qa", fake_run_final_qa)

    render_project(job.id, project_id, job.options, test_settings)

    assert qa_observed is True
    assert published_output.read_bytes() == b"new-fully-rendered-export"
    assert not (project_dir / "work" / "render-staging" / job.id).exists()


def test_import_accepts_srt_file_and_pasted_timestamp_text(
    test_settings, synthetic_video: Path
) -> None:
    client = TestClient(create_app(test_settings))
    srt = """1
00:00:00,250 --> 00:00:01,750
Ngài là ánh sáng
"""
    with synthetic_video.open("rb") as video:
        srt_response = client.post(
            "/api/projects",
            files={
                "video": ("fixture.mp4", video, "video/mp4"),
                "lrc": ("fixture.srt", srt.encode(), "application/x-subrip"),
            },
        )
    assert srt_response.status_code == 200, srt_response.text
    srt_project = srt_response.json()
    assert srt_project["lrc_name"] == "fixture.srt"
    srt_timeline = client.get(
        f"/api/projects/{srt_project['id']}/timeline"
    ).json()["timeline"]
    assert srt_timeline["metadata"]["timeline_format"] == "srt"
    assert srt_timeline["lines"][0]["text"] == "Ngài là ánh sáng"

    with synthetic_video.open("rb") as video:
        pasted_response = client.post(
            "/api/projects",
            data={"timeline_text": "00:00.50 Lời dán trực tiếp"},
            files={"video": ("fixture.mp4", video, "video/mp4")},
        )
    assert pasted_response.status_code == 200, pasted_response.text
    pasted_project = pasted_response.json()
    assert pasted_project["lrc_name"] == "pasted-timeline.txt"
    pasted_timeline = client.get(
        f"/api/projects/{pasted_project['id']}/timeline"
    ).json()["timeline"]
    assert pasted_timeline["metadata"]["timeline_format"] == "timestamp"
    assert pasted_timeline["lines"][0]["text"] == "Lời dán trực tiếp"


def test_font_assets_and_invalid_import_font(test_settings, synthetic_video: Path) -> None:
    client = TestClient(create_app(test_settings))
    for font_id in ("noto_sans", "be_vietnam_pro", "lexend", "barlow_condensed", "baloo_2"):
        response = client.get(f"/api/assets/karaoke-font/{font_id}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("font/ttf")

    with synthetic_video.open("rb") as video:
        invalid = client.post(
            "/api/projects",
            data={"background_mode": "original", "karaoke_font": "system-font"},
            files={
                "video": ("fixture.mp4", video, "video/mp4"),
                "lrc": ("fixture.lrc", LRC.encode(), "text/plain"),
            },
        )
    assert invalid.status_code == 400

    with synthetic_video.open("rb") as video:
        invalid_color = client.post(
            "/api/projects",
            data={"background_mode": "original", "karaoke_color": "blue"},
            files={
                "video": ("fixture.mp4", video, "video/mp4"),
                "lrc": ("fixture.lrc", LRC.encode(), "text/plain"),
            },
        )
    assert invalid_color.status_code == 400
    assert "karaoke_color" in invalid_color.text


def test_timeline_patch_persists_revision_and_rejects_stale_autosave(
    test_settings, synthetic_video: Path
) -> None:
    client = TestClient(create_app(test_settings))
    project = _import_project(client, synthetic_video)
    project_id = project["id"]
    timeline = client.get(f"/api/projects/{project_id}/timeline").json()["timeline"]
    first_revision = timeline["revision"]
    timeline["lines"][0]["locked"] = True
    for token in timeline["lines"][0]["tokens"]:
        token["locked"] = True

    saved = client.patch(
        f"/api/projects/{project_id}/timeline",
        json={"expected_revision": first_revision, "timeline": timeline},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["timeline"]["revision"] == first_revision + 1
    persisted = client.get(f"/api/projects/{project_id}/timeline").json()["timeline"]
    assert persisted["revision"] == first_revision + 1
    assert persisted["lines"][0]["locked"] is True

    stale = client.patch(
        f"/api/projects/{project_id}/timeline",
        json={"expected_revision": first_revision, "timeline": timeline},
    )
    assert stale.status_code == 409


def test_delete_project_removes_database_record_and_local_media(
    test_settings, synthetic_video: Path
) -> None:
    client = TestClient(create_app(test_settings))
    project = _import_project(client, synthetic_video)
    project_id = project["id"]
    project_dir = Store(test_settings).project_dir(project_id)
    assert project_dir.is_dir()

    deleted = client.delete(f"/api/projects/{project_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "project_id": project_id}
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert all(item["id"] != project_id for item in client.get("/api/projects").json())
    assert not project_dir.exists()
    assert client.delete(f"/api/projects/{project_id}").status_code == 404


def test_export_download_and_scoped_delete(
    test_settings, synthetic_video: Path
) -> None:
    client = TestClient(create_app(test_settings))
    project = _import_project(client, synthetic_video)
    project_id = project["id"]
    project_dir = Store(test_settings).project_dir(project_id)
    export_dir = project_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    filename = "ân-điển-test.mp4"
    payload = b"local-export-fixture"
    export_path = export_dir / filename
    export_path.write_bytes(payload)
    legacy_hfr = export_dir / "ân-điển-r00001-1080p120.mp4"
    compatible_hfr = export_dir / "ân-điển-r00001-hfr-realtime-v1-1080p120.mp4"
    legacy_hfr.write_bytes(b"legacy-quicktime-hfr")
    compatible_hfr.write_bytes(b"compatible-quicktime-hfr")

    artifacts = client.get(f"/api/projects/{project_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_labels = {item["label"] for item in artifacts.json()}
    assert legacy_hfr.name not in artifact_labels
    assert compatible_hfr.name in artifact_labels
    artifact = next(item for item in artifacts.json() if item["label"] == filename)
    assert artifact["kind"] == "export"
    assert artifact["bytes"] == len(payload)

    download = client.get(f"{artifact['url']}?download=true")
    assert download.status_code == 200
    assert download.content == payload
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["cache-control"] == "no-store, max-age=0"

    protected_path = project_dir / "work" / "protected.mp4"
    protected_path.parent.mkdir(parents=True, exist_ok=True)
    protected_path.write_bytes(b"keep")
    traversal = quote("../work/protected.mp4", safe="")
    refused = client.delete(f"/api/projects/{project_id}/exports/{traversal}")
    assert refused.status_code in {404, 422}
    assert protected_path.read_bytes() == b"keep"

    deleted = client.delete(
        f"/api/projects/{project_id}/exports/{quote(filename, safe='')}"
    )
    assert deleted.status_code == 200
    assert deleted.json() == {
        "deleted": True,
        "filename": filename,
        "bytes": len(payload),
    }
    assert not export_path.exists()
    assert client.delete(
        f"/api/projects/{project_id}/exports/{quote(filename, safe='')}"
    ).status_code == 404


def test_malformed_lrc_and_missing_audio_fail_closed(
    test_settings, synthetic_video: Path, silent_video: Path
) -> None:
    client = TestClient(create_app(test_settings))
    with synthetic_video.open("rb") as video:
        malformed = client.post(
            "/api/projects",
            files={
                "video": ("fixture.mp4", video, "video/mp4"),
                "lrc": ("fixture.lrc", b"khong co timestamp", "text/plain"),
            },
        )
    assert malformed.status_code == 400

    with silent_video.open("rb") as video:
        imported = client.post(
            "/api/projects",
            files={
                "video": ("silent.mp4", video, "video/mp4"),
                "lrc": ("fixture.lrc", b"[00:00.10]Xin chao", "text/plain"),
            },
        )
    assert imported.status_code == 200
    project = imported.json()
    assert project["has_audio"] is False
    rejected = client.post(
        f"/api/projects/{project['id']}/process",
        json={"quality": "fast", "accept_vietnamese_model_license": False},
    )
    assert rejected.status_code == 409


def test_synthetic_process_render_and_qa(
    test_settings, synthetic_video: Path, monkeypatch
) -> None:
    # Keep the synthetic end-to-end test independent of optional local AI models.
    # Production still uses PolarFormer when its optional runtime is installed.
    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: False)
    monkeypatch.setattr(BsPolarformerAdapter, "available", lambda _self: False)
    app = create_app(test_settings)
    client = TestClient(app)
    project_payload = _import_project(client, synthetic_video)
    project_id = project_payload["id"]
    store = Store(test_settings)

    process_job = JobRecord(
        id="job_process_test",
        project_id=project_id,
        kind="process",
        state=JobState.RUNNING,
        progress=0,
        message="test",
        created_at=now_iso(),
        updated_at=now_iso(),
        options={"quality": "fast", "accept_vietnamese_model_license": False},
    )
    store.add_job(process_job)
    process_project(process_job.id, project_id, process_job.options, test_settings)
    project = store.get_project(project_id)
    assert project is not None
    assert project.state == ProjectState.NEEDS_REVIEW
    assert project.selected_instrumental == "center_cancel"

    timestamp = now_iso()
    monkeypatch.setattr(
        app.state.jobs,
        "start",
        lambda requested_project_id, kind, options: JobRecord(
            id="job_unverified_api_test",
            project_id=requested_project_id,
            kind=kind,
            state=JobState.PENDING,
            progress=0,
            message="test",
            created_at=timestamp,
            updated_at=timestamp,
            options=options,
        ),
    )
    unconfirmed_api_render = client.post(
        f"/api/projects/{project_id}/renders",
        json={"mode": "final", "preset": "source", "countdown": True},
    )
    assert unconfirmed_api_render.status_code == 409
    assert "xác nhận instrumental" in unconfirmed_api_render.text

    unconfirmed_render_job = JobRecord(
        id="job_unconfirmed_render_test",
        project_id=project_id,
        kind="render",
        state=JobState.RUNNING,
        progress=0,
        message="test",
        created_at=now_iso(),
        updated_at=now_iso(),
        options={"mode": "final", "preset": "source", "countdown": True},
    )
    store.add_job(unconfirmed_render_job)
    with pytest.raises(RuntimeError, match="chưa được nghe và xác nhận"):
        render_project(
            unconfirmed_render_job.id,
            project_id,
            unconfirmed_render_job.options,
            test_settings,
        )

    confirmed_instrumental = client.post(
        f"/api/projects/{project_id}/instrumental",
        json={"candidate_id": "center_cancel", "confirmed": True},
    )
    assert confirmed_instrumental.status_code == 200
    assert confirmed_instrumental.json()["instrumental_confirmed"] is True

    early_api_render = client.post(
        f"/api/projects/{project_id}/renders",
        json={
            "mode": "final",
            "preset": "source",
            "countdown": True,
            "expected_instrumental_id": "center_cancel",
        },
    )
    assert early_api_render.status_code == 200, early_api_render.text
    assert early_api_render.json()["job"]["options"]["mode"] == "final"
    assert (
        early_api_render.json()["job"]["options"]["expected_instrumental_id"]
        == "center_cancel"
    )
    stale_instrumental_render = client.post(
        f"/api/projects/{project_id}/renders",
        json={
            "mode": "final",
            "preset": "source",
            "expected_instrumental_id": "candidate_from_stale_viewer",
        },
    )
    assert stale_instrumental_render.status_code == 409
    assert "Instrumental đã đổi" in stale_instrumental_render.text

    current_revision = store.load_timeline(project_id).revision
    stale_api_render = client.post(
        f"/api/projects/{project_id}/renders",
        json={
            "mode": "draft",
            "preset": "1080p120",
            "expected_timeline_revision": current_revision - 1,
        },
    )
    assert stale_api_render.status_code == 409
    revision_bound_render = client.post(
        f"/api/projects/{project_id}/renders",
        json={
            "mode": "draft",
            "preset": "1080p120",
            "expected_timeline_revision": current_revision,
        },
    )
    assert revision_bound_render.status_code == 200, revision_bound_render.text
    assert (
        revision_bound_render.json()["job"]["options"]["expected_timeline_revision"]
        == current_revision
    )

    evidence_response = client.get(f"/api/projects/{project_id}/alignment-evidence")
    assert evidence_response.status_code == 200
    evidence = evidence_response.json()
    assert evidence["alignment_profile"] == "maximum"
    assert evidence["degraded"] is True
    assert all(item["reason_codes"] for item in evidence["tokens"])
    filtered = client.get(
        f"/api/projects/{project_id}/alignment-evidence",
        params={"line_id": evidence["tokens"][0]["line_id"]},
    )
    assert filtered.status_code == 200
    assert {item["line_id"] for item in filtered.json()["tokens"]} == {
        evidence["tokens"][0]["line_id"]
    }

    early_render_job = JobRecord(
        id="job_unverified_render_test",
        project_id=project_id,
        kind="render",
        state=JobState.RUNNING,
        progress=0,
        message="test",
        created_at=now_iso(),
        updated_at=now_iso(),
        options={
            "mode": "final",
            "preset": "source",
            "countdown": True,
            "expected_instrumental_id": "center_cancel",
        },
    )
    store.add_job(early_render_job)
    store.update_project(
        project_id,
        selected_instrumental="candidate_changed_after_queue",
        instrumental_confirmed=True,
    )
    with pytest.raises(RuntimeError, match="đã đổi sau khi xếp hàng render"):
        render_project(
            early_render_job.id,
            project_id,
            early_render_job.options,
            test_settings,
        )
    store.update_project(
        project_id,
        selected_instrumental="center_cancel",
        instrumental_confirmed=True,
    )
    render_project(
        early_render_job.id,
        project_id,
        early_render_job.options,
        test_settings,
    )
    project_after_early_export = store.get_project(project_id)
    assert project_after_early_export is not None
    assert project_after_early_export.state == ProjectState.NEEDS_REVIEW
    early_report_path = next(
        (store.project_dir(project_id) / "qa").glob(
            "*-karaoke-unverified-final-r*-source/QA_REPORT.json"
        )
    )
    early_report = json.loads(early_report_path.read_text(encoding="utf-8"))
    assert early_report["status"] == "PASS_WITH_WARNINGS"
    assert early_report["timing_verified_at_render"] is False
    assert early_report["render_bound_instrumental_candidate"] == "center_cancel"
    assert "TIMING_NOT_VERIFIED" in early_report["advisory_reasons"]

    timeline = store.load_timeline(project_id)
    for line in timeline.lines:
        line.verified = True
        for token in line.tokens:
            token.verified = True
    store.save_timeline(project_id, timeline, expected_revision=timeline.revision)
    project = store.update_project(
        project_id,
        selected_instrumental="center_cancel",
        instrumental_confirmed=True,
        state=ProjectState.VERIFIED,
    )

    render_revision = store.load_timeline(project_id).revision
    render_job = JobRecord(
        id="job_render_test",
        project_id=project_id,
        kind="render",
        state=JobState.RUNNING,
        progress=0,
        message="test",
        created_at=now_iso(),
        updated_at=now_iso(),
        options={
            "mode": "draft",
            "preset": "1080p120",
            "countdown": True,
            "expected_timeline_revision": render_revision,
        },
    )
    store.add_job(render_job)
    render_project(render_job.id, project_id, render_job.options, test_settings)
    exports = list((store.project_dir(project_id) / "exports").glob("*.mp4"))
    reports = list((store.project_dir(project_id) / "qa").rglob("QA_REPORT.json"))
    assert len(exports) == 2
    assert len(reports) == 2
    report_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in reports]
    draft_report = next(report for report in report_payloads if report["mode"] == "draft")
    assert draft_report["audio_mode"] == "source_mix_with_vocal"
    assert draft_report["status"] == "PASS"
    assert draft_report["output_fps"] == 120.0
    assert draft_report["output_fps_ratio"] == "120/1"
    assert (
        f"-r{render_revision:05d}-hfr-realtime-v1-1080p120.mp4"
        in draft_report["output"]
    )
    assert draft_report["playback_timing"]["status"] == "PASS"
    assert draft_report["playback_timing"]["packet_count"] == 480
    assert draft_report["playback_timing"]["pts_equals_dts"] is True
    assert draft_report["playback_timing"]["packet_duration_ticks"] == 1000
    assert draft_report["playback_timing"]["expected_packet_duration_ticks"] == 1000
    assert draft_report["playback_timing"]["starts_at_zero"] is True
    assert draft_report["playback_timing"]["quicktime_realtime_intent_required"] is True
    assert draft_report["playback_timing"]["quicktime_realtime_intent_uint8"] == 1
    assert draft_report["karaoke_color"] == "red"
