from __future__ import annotations

import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .backgrounds import BackgroundPlanV1, refresh_background_plan
from .models import LineTiming, ProjectRecord, ProjectState, TimelineV1
from .motion import line_progress_ppm, load_font, resolve_font
from .separation import load_candidates
from .settings import Settings
from .styles import karaoke_color_rgba

EventCallback = Callable[[float, str], None]
WHITE = (248, 247, 241, 255)
PREVIEW = (215, 222, 231, 235)
INK = (0, 0, 0, 225)
KARAOKE_SHADOW = (5, 16, 59, 245)


@dataclass(frozen=True)
class RenderPreset:
    width: int
    height: int
    fps_numerator: int
    fps_denominator: int

    @property
    def fps(self) -> float:
        return self.fps_numerator / self.fps_denominator

    @property
    def ffmpeg_fps(self) -> str:
        return f"{self.fps_numerator}/{self.fps_denominator}"


def resolve_preset(name: str, project: ProjectRecord) -> RenderPreset:
    if name == "1080p30":
        return RenderPreset(1920, 1080, 30, 1)
    if name == "source":
        try:
            fps = Fraction(project.fps)
        except (ValueError, ZeroDivisionError):
            fps = Fraction(30, 1)
        return RenderPreset(project.width, project.height, fps.numerator, fps.denominator)
    return RenderPreset(1920, 1080, 60, 1)


class LineAsset:
    def __init__(
        self,
        line: LineTiming,
        font: ImageFont.FreeTypeFont,
        y: int,
        width: int,
        overlay_height: int,
        active: bool,
        highlight_color: tuple[int, int, int, int],
    ):
        self.line = line
        self.font = font
        self.natural_text_width = max(1.0, float(font.getlength(line.text)))
        self.scale_x = min(1.0, (width * 0.88) / self.natural_text_width)
        self.text_width = self.natural_text_width * self.scale_x
        self.left = (width - self.text_width) / 2
        self.base = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
        stroke_width = max(4, round(font.size * 0.075))
        shadow_offset = max(3, round(font.size * 0.065))
        self._paste_text_layer(
            self.base,
            y + shadow_offset,
            KARAOKE_SHADOW,
            stroke_width=stroke_width,
            stroke_fill=KARAOKE_SHADOW,
        )
        self._paste_text_layer(
            self.base,
            y,
            WHITE if active else PREVIEW,
            stroke_width=stroke_width,
            stroke_fill=KARAOKE_SHADOW,
        )
        self.highlight = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
        self._paste_text_layer(
            self.highlight,
            y + shadow_offset,
            KARAOKE_SHADOW,
            stroke_width=stroke_width,
            stroke_fill=KARAOKE_SHADOW,
        )
        self._paste_text_layer(
            self.highlight,
            y,
            highlight_color,
            stroke_width=stroke_width,
            stroke_fill=WHITE,
        )
        color_layer = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
        self._paste_text_layer(color_layer, y, highlight_color)
        self.glow = color_layer.filter(
            ImageFilter.GaussianBlur(max(4, round(font.size * 0.09)))
        )

    def _paste_text_layer(
        self,
        destination: Image.Image,
        y: int,
        fill: tuple[int, int, int, int],
        stroke_width: int = 0,
        stroke_fill: tuple[int, int, int, int] | None = None,
    ) -> None:
        margin = max(10, stroke_width + 5)
        natural_layer_width = max(1, math.ceil(self.natural_text_width) + margin * 2)
        layer = Image.new("RGBA", (natural_layer_width, destination.height), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text(
            (natural_layer_width / 2, y),
            self.line.text,
            font=self.font,
            anchor="mm",
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        fitted_width = max(1, round(natural_layer_width * self.scale_x))
        if fitted_width != natural_layer_width:
            layer = layer.resize((fitted_width, destination.height), Image.Resampling.LANCZOS)
        destination.alpha_composite(layer, ((destination.width - fitted_width) // 2, 0))


class KaraokeRenderer:
    def __init__(
        self,
        timeline: TimelineV1,
        preset: RenderPreset,
        settings: Settings,
        draft: bool,
        countdown: bool,
    ):
        self.timeline = timeline
        self.preset = preset
        self.width = preset.width
        self.overlay_height = max(250, round(preset.height / 3))
        self.draft = draft
        self.countdown = countdown
        self.font_id = timeline.metadata.get("karaoke_font", "noto_sans")
        self.font_path = resolve_font(settings, self.font_id)
        self.highlight_color = karaoke_color_rgba(timeline.metadata.get("karaoke_color"))
        self.font = load_font(self.font_path, round(self.preset.height * 0.072))
        self.line_fonts: dict[str, ImageFont.FreeTypeFont] = {}
        self.line_scales: dict[str, float] = {}
        self.assets: dict[tuple[int, bool], LineAsset] = {}
        upper = round(self.overlay_height * 0.30)
        lower = round(self.overlay_height * 0.70)
        self.lane_y = (upper, lower)
        for index, line in enumerate(timeline.lines):
            y = self.lane_y[index % 2]
            self.line_fonts[line.id] = self.font
            self.assets[(index, True)] = LineAsset(
                line,
                self.font,
                y,
                self.width,
                self.overlay_height,
                True,
                self.highlight_color,
            )
            self.assets[(index, False)] = LineAsset(
                line,
                self.font,
                y,
                self.width,
                self.overlay_height,
                False,
                self.highlight_color,
            )
            self.line_scales[line.id] = self.assets[(index, True)].scale_x

    def frame(self, now_us: int) -> Image.Image:
        frame = Image.new("RGBA", (self.width, self.overlay_height), (0, 0, 0, 0))
        active = next(
            (
                index
                for index, line in enumerate(self.timeline.lines)
                if line.start_us <= now_us < line.end_us
            ),
            None,
        )
        rows: list[tuple[int, bool]] = []
        countdown: tuple[int, int] | None = None
        if active is not None:
            rows.append((active, True))
            if active + 1 < len(self.timeline.lines):
                rows.append((active + 1, False))
        else:
            upcoming = next(
                (index for index, line in enumerate(self.timeline.lines) if line.start_us > now_us),
                None,
            )
            if upcoming is not None:
                previous_ended = upcoming > 0 and now_us >= self.timeline.lines[upcoming - 1].end_us
                first_lead = upcoming == 0 and self.timeline.lines[0].start_us - now_us <= 4_500_000
                if previous_ended or first_lead:
                    rows.append((upcoming, False))
                    gap_start = 0 if upcoming == 0 else self.timeline.lines[upcoming - 1].end_us
                    until = self.timeline.lines[upcoming].start_us - now_us
                    if (
                        self.countdown
                        and self.timeline.lines[upcoming].start_us - gap_start >= 3_000_000
                        and 0 < until <= 3_000_000
                    ):
                        upcoming_lane = upcoming % 2
                        countdown = (
                            max(1, min(3, math.ceil(until / 1_000_000))),
                            self.lane_y[1 - upcoming_lane],
                        )
        for index, is_active in rows:
            self._composite_line(frame, index, is_active, now_us)
        if countdown:
            self._draw_countdown(frame, *countdown)
        if self.draft:
            self._draw_draft(frame)
        return frame

    def _composite_line(self, frame: Image.Image, index: int, active: bool, now_us: int) -> None:
        asset = self.assets[(index, active)]
        frame.alpha_composite(asset.base)
        if not active:
            return
        boundary = round(self._highlight_boundary(asset.line, asset.left, now_us))
        boundary = max(0, min(self.width, boundary))
        glow_right = min(self.width, boundary + round(asset.font.size * 0.35))
        if glow_right > 0:
            frame.alpha_composite(asset.glow.crop((0, 0, glow_right, self.overlay_height)), (0, 0))
        if boundary > 0:
            frame.alpha_composite(
                asset.highlight.crop((0, 0, boundary, self.overlay_height)), (0, 0)
            )

    def _highlight_boundary(self, line: LineTiming, left: float, now_us: int) -> float:
        font = self.line_fonts.get(line.id, self.font)
        scale_x = self.line_scales.get(line.id, 1.0)
        if now_us <= line.start_us:
            return left
        progress_ppm = line_progress_ppm(line, now_us)
        if progress_ppm is not None:
            return left + font.getlength(line.text) * scale_x * (progress_ppm / 1_000_000)
        cursor = 0
        for token in line.tokens:
            found = line.text.find(token.text, cursor)
            token_start = found if found >= 0 else cursor
            token_end = min(len(line.text), token_start + len(token.text))
            word_left = left + font.getlength(line.text[:cursor]) * scale_x
            word_right = left + font.getlength(line.text[:token_end]) * scale_x
            if now_us < token.start_us:
                return word_left
            if now_us <= token.end_us:
                progress = (now_us - token.start_us) / max(1, token.end_us - token.start_us)
                return word_left + (word_right - word_left) * max(0.0, min(1.0, progress))
            cursor = token_end
        return left + font.getlength(line.text) * scale_x

    def _draw_countdown(self, frame: Image.Image, number: int, y: int) -> None:
        draw = ImageDraw.Draw(frame)
        radius = round(self.font.size * 0.72)
        center = self.width // 2
        draw.ellipse(
            (center - radius, y - radius, center + radius, y + radius),
            fill=(2, 7, 16, 190),
            outline=self.highlight_color,
            width=3,
        )
        font = load_font(self.font_path, round(self.font.size * 0.92))
        draw.text(
            (center, y - 1),
            str(number),
            font=font,
            anchor="mm",
            fill=self.highlight_color,
            stroke_width=2,
            stroke_fill=INK,
        )

    def _draw_draft(self, frame: Image.Image) -> None:
        draw = ImageDraw.Draw(frame)
        font = load_font(self.font_path, max(16, round(self.font.size * 0.34)))
        text = "TIMING NOT VERIFIED"
        bbox = draw.textbbox((0, 0), text, font=font)
        x = self.width - (bbox[2] - bbox[0]) - 24
        draw.rounded_rectangle((x - 10, 10, self.width - 14, 42), radius=8, fill=(120, 22, 22, 185))
        draw.text((x, 17), text, font=font, fill=(255, 228, 228, 245))


def render_video(
    project: ProjectRecord,
    timeline: TimelineV1,
    project_dir: Path,
    settings: Settings,
    mode: str,
    preset_name: str,
    countdown: bool,
    event: EventCallback,
) -> Path:
    if mode == "draft":
        audio_input = project_dir / "work" / "mix.wav"
    else:
        candidates = {candidate.id: candidate for candidate in load_candidates(project_dir)}
        if not project.selected_instrumental or project.selected_instrumental not in candidates:
            raise RuntimeError("Chưa có instrumental để xuất bản loại giọng.")
        audio_input = Path(candidates[project.selected_instrumental].instrumental)
    if not audio_input.is_file():
        raise RuntimeError("Không tìm thấy audio dùng cho bản render.")
    source = project_dir / "source" / project.source_name
    preset = resolve_preset(preset_name, project)
    timeline = timeline.model_copy(deep=True)
    timeline.fps_numerator = preset.fps_numerator
    timeline.fps_denominator = preset.fps_denominator
    renderer = KaraokeRenderer(timeline, preset, settings, mode == "draft", countdown)
    export_dir = project_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_kind = mode
    if mode == "final" and project.state not in {ProjectState.VERIFIED, ProjectState.RENDERED}:
        export_kind = "unverified-final"
    output = export_dir / f"{_slug(project.title)}-karaoke-{export_kind}-{preset_name}.mp4"
    log_path = project_dir / "logs" / f"render-{mode}-{preset_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    background_args, background_filter, background_input_count = _background_render_pipeline(
        project,
        timeline,
        project_dir,
        source,
        preset,
        settings,
    )
    overlay_input_index = background_input_count
    audio_input_index = background_input_count + 1
    overlay_y = preset.height - renderer.overlay_height - round(preset.height * 0.025)
    filter_graph = (
        f"{background_filter};"
        f"[{overlay_input_index}:v]fps={preset.ffmpeg_fps},format=rgba[ov];"
        f"[bg][ov]overlay=0:{overlay_y}:shortest=1:format=auto[v]"
    )
    command = [
        settings.ffmpeg,
        "-hide_banner",
        "-y",
        *background_args,
        "-thread_queue_size",
        "1024",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{preset.width}x{renderer.overlay_height}",
        "-r",
        preset.ffmpeg_fps,
        "-i",
        "pipe:0",
        "-i",
        str(audio_input),
        "-filter_complex",
        filter_graph,
        "-map",
        "[v]",
        "-map",
        f"{audio_input_index}:a:0",
        "-af",
        "alimiter=limit=0.8913:level=false:latency=true",
        "-t",
        f"{timeline.duration_us / 1_000_000:.6f}",
        "-r",
        preset.ffmpeg_fps,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "17",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(output),
    ]
    total_frames = math.ceil(
        timeline.duration_us * preset.fps_numerator / (1_000_000 * preset.fps_denominator)
    )
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
        )
        assert process.stdin is not None
        try:
            for frame_index in range(total_frames):
                now_us = round(
                    frame_index * 1_000_000 * preset.fps_denominator / preset.fps_numerator
                )
                process.stdin.write(renderer.frame(now_us).tobytes())
                if frame_index % max(1, round(preset.fps * 2)) == 0:
                    event(
                        0.15 + 0.70 * frame_index / max(1, total_frames),
                        f"Đang render {100 * frame_index / max(1, total_frames):.1f}%…",
                    )
        finally:
            process.stdin.close()
        code = process.wait()
    if code:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"FFmpeg render thất bại.\n{tail}")
    event(0.86, "Render hoàn tất; đang chạy full decode và QA…")
    return output


def _background_render_pipeline(
    project: ProjectRecord,
    timeline: TimelineV1,
    project_dir: Path,
    source: Path,
    preset: RenderPreset,
    settings: Settings,
) -> tuple[list[str], str, int]:
    if project.background_mode != "custom":
        duration = timeline.duration_us / 1_000_000
        graph = (
            f"[0:v]fps={preset.ffmpeg_fps},"
            f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
            f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,tpad=stop_mode=clone:stop_duration={duration:.6f}[bg]"
        )
        return ["-i", str(source)], graph, 1

    plan = refresh_background_plan(project, timeline, project_dir, settings)
    if plan is None or not plan.segments:
        raise RuntimeError("Project chưa có lịch chuyển cảnh nền.")
    return _custom_background_pipeline(plan, project_dir, preset)


def _custom_background_pipeline(
    plan: BackgroundPlanV1,
    project_dir: Path,
    preset: RenderPreset,
) -> tuple[list[str], str, int]:
    assets_by_id = {asset.id: asset for asset in plan.assets}
    input_args: list[str] = []
    filters: list[str] = []
    for index, segment in enumerate(plan.segments):
        asset = assets_by_id.get(segment.asset_id)
        if asset is None:
            raise RuntimeError(f"Lịch nền tham chiếu asset không tồn tại: {segment.asset_id}")
        path = project_dir / "source" / asset.filename
        if not path.is_file():
            raise RuntimeError(f"Không tìm thấy nền: {asset.filename}")
        if asset.kind == "image":
            input_args.extend(["-loop", "1", "-framerate", preset.ffmpeg_fps, "-i", str(path)])
        else:
            input_args.extend(["-stream_loop", "-1", "-i", str(path)])

        outgoing_transition_us = (
            plan.segments[index + 1].transition_us
            if index + 1 < len(plan.segments)
            else 0
        )
        clip_duration_us = segment.end_us - segment.start_us + outgoing_transition_us
        filters.append(
            f"[{index}:v]fps={preset.ffmpeg_fps},"
            f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={preset.width}:{preset.height},setsar=1,settb=AVTB,"
            f"trim=duration={clip_duration_us / 1_000_000:.6f},"
            f"setpts=PTS-STARTPTS,format=yuv420p[bg{index}]"
        )

    chain_label = "bg0"
    for index, segment in enumerate(plan.segments[1:], start=1):
        output_label = f"bgmix{index}"
        if segment.transition_us > 0:
            filters.append(
                f"[{chain_label}][bg{index}]xfade=transition=fade:"
                f"duration={segment.transition_us / 1_000_000:.6f}:"
                f"offset={segment.start_us / 1_000_000:.6f}[{output_label}]"
            )
        else:
            filters.append(
                f"[{chain_label}][bg{index}]concat=n=2:v=1:a=0[{output_label}]"
            )
        chain_label = output_label
    filters.append(
        f"[{chain_label}]trim=duration={plan.duration_us / 1_000_000:.6f},"
        "setpts=PTS-STARTPTS[bg]"
    )
    return input_args, ";".join(filters), len(plan.segments)


def render_preview_png(
    timeline: TimelineV1, settings: Settings, now_us: int, draft: bool = True
) -> bytes:
    import io

    preset = RenderPreset(1920, 1080, timeline.fps_numerator, timeline.fps_denominator)
    image = KaraokeRenderer(timeline, preset, settings, draft, True).frame(now_us)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _slug(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in cleaned.split("-") if part)[:80] or "karaoke"
