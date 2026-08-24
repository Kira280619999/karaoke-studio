from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from karaoke_studio.api import create_app
from karaoke_studio.backgrounds import (
    BackgroundAssetV1,
    build_background_plan,
    refresh_background_plan,
    save_background_plan,
    schedule_backgrounds,
)
from karaoke_studio.db import now_iso
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import ProjectRecord, ProjectState
from karaoke_studio.rendering import (
    RenderPreset,
    _background_motion_filter,
    _custom_background_pipeline,
    render_video,
)


def _asset(index: int) -> BackgroundAssetV1:
    return BackgroundAssetV1(
        id=f"bg_{index:03d}",
        filename=f"scene-{index}.png",
        kind="image",
        sha256=f"sha-{index}",
        width=1920,
        height=1080,
    )


def test_background_schedule_prefers_lyric_gaps_and_stays_monotonic() -> None:
    timeline = parse_lrc(
        "[00:00.00]Câu một\n[00:04.00]Câu hai\n[00:08.00]Câu ba",
        duration_us=12_000_000,
    )
    timeline.lines[0].end_us = 3_000_000
    timeline.lines[1].end_us = 7_000_000

    plan = schedule_backgrounds([_asset(1), _asset(2), _asset(3)], timeline)

    assert [segment.asset_id for segment in plan.segments] == ["bg_001", "bg_002", "bg_003"]
    assert plan.segments[0].start_us == 0
    assert plan.segments[-1].end_us == timeline.duration_us
    assert all(
        left.end_us == right.start_us
        for left, right in zip(plan.segments, plan.segments[1:], strict=False)
    )
    assert all(segment.start_us < segment.end_us for segment in plan.segments)
    assert all(segment.anchor == "lyric_gap" for segment in plan.segments[1:])
    assert all(650_000 < segment.transition_us <= 1_800_000 for segment in plan.segments[1:])


def test_long_scenes_receive_full_professional_dissolve() -> None:
    timeline = parse_lrc(
        "[00:00.00]Câu một\n[00:42.00]Câu hai\n[01:22.00]Câu ba",
        duration_us=120_000_000,
    )
    timeline.lines[0].end_us = 38_000_000
    timeline.lines[1].end_us = 78_000_000

    plan = schedule_backgrounds([_asset(1), _asset(2), _asset(3)], timeline)

    assert [segment.transition_us for segment in plan.segments] == [0, 1_800_000, 1_800_000]


def test_multiple_background_upload_builds_manifest_and_safe_urls(
    test_settings, synthetic_video: Path, tmp_path: Path
) -> None:
    first = tmp_path / "ảnh thứ nhất.png"
    second = tmp_path / "ảnh thứ hai.jpg"
    Image.new("RGB", (640, 360), "navy").save(first)
    Image.new("RGB", (360, 640), "gold").save(second)
    client = TestClient(create_app(test_settings))

    response = client.post(
        "/api/projects",
        data={"background_mode": "custom", "title": "Nhiều cảnh"},
        files=[
            ("video", ("fixture.mp4", synthetic_video.read_bytes(), "video/mp4")),
            ("lrc", ("fixture.lrc", b"[00:00.10]Xin chao\n[00:02.00]Karaoke", "text/plain")),
            ("background", (first.name, first.read_bytes(), "image/png")),
            ("background", (second.name, second.read_bytes(), "image/jpeg")),
        ],
    )

    assert response.status_code == 200, response.text
    project = response.json()
    assert project["background_name"].startswith("background-01-")
    plan_response = client.get(f"/api/projects/{project['id']}/background-plan")
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    assert len(plan["assets"]) == len(plan["segments"]) == 2
    assert [asset["kind"] for asset in plan["assets"]] == ["image", "image"]
    assert all(asset["url"].startswith(f"/api/projects/{project['id']}/files/source/") for asset in plan["assets"])
    assert plan["segments"][0]["start_us"] == 0
    assert plan["segments"][-1]["end_us"] == project["duration_us"]


def test_refresh_reschedules_existing_assets_after_alignment(
    test_settings, tmp_path: Path
) -> None:
    project_dir = tmp_path / "project"
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True)
    paths = [source_dir / "one.png", source_dir / "two.png"]
    Image.new("RGB", (320, 180), "black").save(paths[0])
    Image.new("RGB", (320, 180), "white").save(paths[1])
    original = parse_lrc("[00:00.00]Một\n[00:02.00]Hai", duration_us=4_000_000)
    save_background_plan(
        project_dir,
        build_background_plan(paths, original, test_settings),
    )
    project = ProjectRecord(
        id="proj_background",
        title="Background",
        state=ProjectState.IMPORTED,
        created_at=now_iso(),
        updated_at=now_iso(),
        source_name="source.mp4",
        lrc_name="lyrics.lrc",
        background_name="one.png",
        background_mode="custom",
        source_sha256="sha",
        duration_us=6_000_000,
        width=640,
        height=360,
        fps="30/1",
        has_audio=True,
    )
    aligned = parse_lrc("[00:00.00]Một\n[00:04.00]Hai", duration_us=6_000_000)

    refreshed = refresh_background_plan(project, aligned, project_dir, test_settings)

    assert refreshed is not None
    assert refreshed.duration_us == 6_000_000
    assert [asset.sha256 for asset in refreshed.assets] == [
        asset.sha256 for asset in build_background_plan(paths, aligned, test_settings).assets
    ]
    assert refreshed.segments[-1].end_us == 6_000_000


def test_custom_background_filter_graph_decodes_crossfade(
    test_settings, tmp_path: Path
) -> None:
    project_dir = tmp_path / "project"
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True)
    paths = [source_dir / "wide.png", source_dir / "portrait.png"]
    Image.new("RGB", (640, 360), "#112244").save(paths[0])
    Image.new("RGB", (240, 420), "#aa7722").save(paths[1])
    timeline = parse_lrc("[00:00.00]Một\n[00:03.00]Hai", duration_us=6_000_000)
    plan = build_background_plan(paths, timeline, test_settings)
    input_args, graph, count = _custom_background_pipeline(
        plan,
        project_dir,
        RenderPreset(320, 180, 10, 1),
    )

    assert count == 2
    assert "xfade=transition=fadeslow" in graph
    assert graph.count("perspective=") == 2
    assert graph.count("eval=frame:interpolation=cubic") == 2
    assert "force_original_aspect_ratio=increase" in graph
    subprocess.run(
        [
            test_settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-filter_complex",
            graph,
            "-map",
            "[bg]",
            "-t",
            "6",
            "-f",
            "null",
            "-",
        ],
        check=True,
        timeout=30,
    )


def test_ken_burns_motion_matches_preview_curves_at_frame_time() -> None:
    preset = RenderPreset(1920, 1080, 60, 1)

    first = _background_motion_filter(0, preset, 10_000_000)
    second = _background_motion_filter(1, preset, 10_000_000)

    assert "1.035000+(0.050000)*in*1000000/600000000" in first
    assert "0.300000+(-0.600000)*in*1000000/600000000" in first
    assert "-0.150000+(0.300000)*in*1000000/600000000" in first
    assert "1.085000+(-0.040000)*in*1000000/600000000" in second
    assert "-0.800000+(1.600000)*in*1000000/600000000" in second
    assert "0.200000+(-0.400000)*in*1000000/600000000" in second
    assert "sense=source:eval=frame:interpolation=cubic" in first


def test_120fps_still_background_motion_changes_every_frame(
    test_settings, tmp_path: Path
) -> None:
    project_dir = tmp_path / "smooth-motion-project"
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True)
    background = source_dir / "detail.png"
    Image.effect_noise((640, 360), 96).convert("RGB").save(background)
    timeline = parse_lrc("[00:00.00]Chuyển động mượt", duration_us=2_000_000)
    plan = build_background_plan([background], timeline, test_settings)

    input_args, graph, _count = _custom_background_pipeline(
        plan,
        project_dir,
        RenderPreset(320, 180, 120, 1),
    )
    result = subprocess.run(
        [
            test_settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-filter_complex",
            graph,
            "-map",
            "[bg]",
            "-frames:v",
            "240",
            "-pix_fmt",
            "rgb24",
            "-f",
            "framemd5",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    hashes = [
        row.rsplit(", ", 1)[-1]
        for row in result.stdout.splitlines()
        if row and not row.startswith("#")
    ]

    assert len(hashes) == 240
    assert all(left != right for left, right in zip(hashes, hashes[1:], strict=False))


def test_full_draft_render_uses_multi_scene_background(
    test_settings, synthetic_video: Path, tmp_path: Path
) -> None:
    project_dir = tmp_path / "render-project"
    source_dir = project_dir / "source"
    work_dir = project_dir / "work"
    source_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    source = source_dir / "source.mp4"
    shutil.copy2(synthetic_video, source)
    backgrounds = [source_dir / "wide.png", source_dir / "portrait.png"]
    Image.new("RGB", (640, 360), "#18304a").save(backgrounds[0])
    Image.new("RGB", (260, 460), "#9a5b26").save(backgrounds[1])
    subprocess.run(
        [
            test_settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(work_dir / "mix.wav"),
        ],
        check=True,
        timeout=30,
    )
    timeline = parse_lrc(
        "[00:00.10]Xin chào\n[00:02.00]Karaoke",
        duration_us=4_000_000,
    )
    save_background_plan(
        project_dir,
        build_background_plan(backgrounds, timeline, test_settings),
    )
    project = ProjectRecord(
        id="proj_render_background",
        title="Nhiều cảnh",
        state=ProjectState.IMPORTED,
        created_at=now_iso(),
        updated_at=now_iso(),
        source_name=source.name,
        lrc_name="lyrics.lrc",
        background_name=backgrounds[0].name,
        background_mode="custom",
        source_sha256="sha",
        duration_us=4_000_000,
        width=640,
        height=360,
        fps="30/1",
        has_audio=True,
    )

    output = render_video(
        project,
        timeline,
        project_dir,
        test_settings,
        mode="draft",
        preset_name="source",
        countdown=False,
        event=lambda _progress, _message: None,
    )

    assert output.is_file()
    subprocess.run(
        [
            test_settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output),
            "-f",
            "null",
            "-",
        ],
        check=True,
        timeout=30,
    )


def test_exported_song_credit_fades_out_and_is_gone_after_five_seconds(
    test_settings, tmp_path: Path
) -> None:
    project_dir = tmp_path / "credit-timeout-project"
    source_dir = project_dir / "source"
    work_dir = project_dir / "work"
    source_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    source = source_dir / "source.mp4"
    subprocess.run(
        [
            test_settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x101820:s=320x180:r=30:d=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            test_settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(work_dir / "mix.wav"),
        ],
        check=True,
        timeout=30,
    )
    timeline = parse_lrc("[00:01.00]Lời thử", duration_us=6_000_000)
    project = ProjectRecord(
        id="proj_credit_timeout",
        title="Diệu Thay Là Chúa Ta",
        artist="Isaac Thái",
        state=ProjectState.IMPORTED,
        created_at=now_iso(),
        updated_at=now_iso(),
        source_name=source.name,
        lrc_name="lyrics.lrc",
        source_sha256="sha",
        duration_us=6_000_000,
        width=320,
        height=180,
        fps="30/1",
        has_audio=True,
    )

    output = render_video(
        project,
        timeline,
        project_dir,
        test_settings,
        mode="draft",
        preset_name="source",
        countdown=False,
        event=lambda _progress, _message: None,
    )
    before_path = tmp_path / "credit-before.png"
    after_path = tmp_path / "credit-after.png"
    for timestamp, frame_path in (("1.0", before_path), ("5.5", after_path)):
        subprocess.run(
            [
                test_settings.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                timestamp,
                "-i",
                str(output),
                "-frames:v",
                "1",
                str(frame_path),
            ],
            check=True,
            timeout=30,
        )

    before_pixel = Image.open(before_path).convert("RGB").getpixel((26, 32))
    after_pixel = Image.open(after_path).convert("RGB").getpixel((26, 32))
    assert before_pixel[0] > 150 and before_pixel[1] > 100 and before_pixel[2] < 130
    assert (
        max(
            abs(channel - target)
            for channel, target in zip(after_pixel, (16, 24, 32), strict=True)
        )
        < 20
    )
