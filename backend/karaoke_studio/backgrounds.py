from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PIL import Image
from pydantic import BaseModel, Field

from .media import MediaError, probe, sha256_file
from .models import TimelineV1
from .settings import Settings

if TYPE_CHECKING:
    from .models import ProjectRecord


BACKGROUND_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
BACKGROUND_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}
BACKGROUND_EXTENSIONS = BACKGROUND_VIDEO_EXTENSIONS | BACKGROUND_IMAGE_EXTENSIONS
MAX_BACKGROUND_ASSETS = 64
DEFAULT_TRANSITION_US = 650_000


class BackgroundAssetV1(BaseModel):
    id: str
    filename: str
    kind: Literal["image", "video"]
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    source_duration_us: int | None = Field(default=None, gt=0)
    url: str | None = None


class BackgroundSegmentV1(BaseModel):
    asset_id: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    transition_us: int = Field(ge=0)
    anchor: Literal["song_start", "lyric_gap", "balanced"]


class BackgroundPlanV1(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    strategy: Literal["lyric_gap_balanced"] = "lyric_gap_balanced"
    duration_us: int = Field(gt=0)
    assets: list[BackgroundAssetV1]
    segments: list[BackgroundSegmentV1]


def inspect_background(path: Path, index: int, settings: Settings) -> BackgroundAssetV1:
    suffix = path.suffix.casefold()
    if suffix in BACKGROUND_VIDEO_EXTENSIONS:
        info = probe(path, settings)
        width, height = info.width, info.height
        kind: Literal["image", "video"] = "video"
        duration_us: int | None = info.video_duration_us
    elif suffix in BACKGROUND_IMAGE_EXTENSIONS:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, ValueError) as exc:
            raise MediaError(f"Không đọc được ảnh nền: {path.name}") from exc
        kind = "image"
        duration_us = None
    else:
        raise MediaError(f"Định dạng nền không được hỗ trợ: {path.suffix or path.name}")
    if width <= 0 or height <= 0:
        raise MediaError(f"Nền không có kích thước hợp lệ: {path.name}")
    return BackgroundAssetV1(
        id=f"bg_{index + 1:03d}",
        filename=path.name,
        kind=kind,
        sha256=sha256_file(path),
        width=width,
        height=height,
        source_duration_us=duration_us,
    )


def schedule_backgrounds(
    assets: list[BackgroundAssetV1], timeline: TimelineV1
) -> BackgroundPlanV1:
    if not assets:
        raise ValueError("Cần ít nhất một ảnh hoặc video nền.")
    duration_us = max(1, timeline.duration_us)
    asset_count = len(assets)
    if asset_count == 1:
        return BackgroundPlanV1(
            duration_us=duration_us,
            assets=assets,
            segments=[
                BackgroundSegmentV1(
                    asset_id=assets[0].id,
                    start_us=0,
                    end_us=duration_us,
                    transition_us=0,
                    anchor="song_start",
                )
            ],
        )

    candidates: list[tuple[int, int]] = []
    ordered_lines = sorted(timeline.lines, key=lambda line: (line.start_us, line.end_us))
    for previous, current in zip(ordered_lines, ordered_lines[1:], strict=False):
        gap_us = max(0, current.start_us - previous.end_us)
        boundary_us = (
            previous.end_us + gap_us // 2 if gap_us >= 250_000 else current.start_us
        )
        if 0 < boundary_us < duration_us:
            candidates.append((boundary_us, gap_us))

    # A dense or malformed lyric timeline may not expose enough quiet gaps.
    # Line starts remain useful secondary anchors before falling back to equal spacing.
    candidates.extend(
        (line.start_us, 0)
        for line in ordered_lines[1:]
        if 0 < line.start_us < duration_us
    )
    candidates = sorted(set(candidates))

    minimum_segment_us = min(
        2_000_000,
        max(250_000, duration_us // max(1, asset_count * 3)),
    )
    boundaries = [0]
    anchors: list[Literal["song_start", "lyric_gap", "balanced"]] = ["song_start"]
    for index in range(1, asset_count):
        target_us = round(duration_us * index / asset_count)
        remaining = asset_count - index
        lower_us = boundaries[-1] + minimum_segment_us
        upper_us = duration_us - remaining * minimum_segment_us
        if lower_us > upper_us:
            lower_us = boundaries[-1] + 1
            upper_us = max(lower_us, duration_us - remaining)

        eligible = [
            candidate
            for candidate in candidates
            if lower_us <= candidate[0] <= upper_us
        ]
        if eligible:
            # Prefer a nearby boundary, then reward a genuine vocal/lyric pause.
            boundary_us, gap_us = min(
                eligible,
                key=lambda candidate: (
                    abs(candidate[0] - target_us)
                    - (min(candidate[1], 2_000_000) * 3) // 4,
                    abs(candidate[0] - target_us),
                ),
            )
            anchor: Literal["lyric_gap", "balanced"] = (
                "lyric_gap" if gap_us >= 250_000 else "balanced"
            )
        else:
            boundary_us = min(max(target_us, lower_us), upper_us)
            anchor = "balanced"
        boundaries.append(boundary_us)
        anchors.append(anchor)
    boundaries.append(duration_us)

    segments: list[BackgroundSegmentV1] = []
    for index, asset in enumerate(assets):
        start_us, end_us = boundaries[index], boundaries[index + 1]
        if index == 0:
            transition_us = 0
        else:
            previous_duration = boundaries[index] - boundaries[index - 1]
            current_duration = end_us - start_us
            transition_us = min(
                DEFAULT_TRANSITION_US,
                max(0, previous_duration // 3),
                max(0, current_duration // 3),
            )
            if transition_us < 100_000:
                transition_us = 0
        segments.append(
            BackgroundSegmentV1(
                asset_id=asset.id,
                start_us=start_us,
                end_us=end_us,
                transition_us=transition_us,
                anchor=anchors[index],
            )
        )
    return BackgroundPlanV1(duration_us=duration_us, assets=assets, segments=segments)


def build_background_plan(
    paths: list[Path], timeline: TimelineV1, settings: Settings
) -> BackgroundPlanV1:
    if len(paths) > MAX_BACKGROUND_ASSETS:
        raise MediaError(f"Tối đa {MAX_BACKGROUND_ASSETS} ảnh/video nền cho một bài.")
    assets = [inspect_background(path, index, settings) for index, path in enumerate(paths)]
    return schedule_backgrounds(assets, timeline)


def background_plan_path(project_dir: Path) -> Path:
    return project_dir / "source" / "background-plan.json"


def save_background_plan(project_dir: Path, plan: BackgroundPlanV1) -> Path:
    destination = background_plan_path(project_dir)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_background_plan(project_dir: Path) -> BackgroundPlanV1 | None:
    path = background_plan_path(project_dir)
    if not path.is_file():
        return None
    try:
        return BackgroundPlanV1.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def refresh_background_plan(
    project: ProjectRecord,
    timeline: TimelineV1,
    project_dir: Path,
    settings: Settings,
) -> BackgroundPlanV1 | None:
    if project.background_mode != "custom":
        return None
    existing = load_background_plan(project_dir)
    filenames = [asset.filename for asset in existing.assets] if existing else []
    if not filenames and project.background_name:
        filenames = [project.background_name]
    paths = [project_dir / "source" / filename for filename in filenames]
    paths = [path for path in paths if path.is_file()]
    if not paths:
        raise MediaError("Không tìm thấy ảnh/video nền của project.")

    if existing and len(paths) == len(existing.assets) and all(
        asset.sha256 == sha256_file(project_dir / "source" / asset.filename)
        for asset in existing.assets
    ):
        plan = schedule_backgrounds(existing.assets, timeline)
    else:
        plan = build_background_plan(paths, timeline, settings)
    save_background_plan(project_dir, plan)
    return plan
