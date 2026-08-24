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
KARAOKE_ROW_SPACING_EM = 1.32
KARAOKE_LOWER_ROW_GAP_EM = 0.06
KARAOKE_COUNTDOWN_US = 3_000_000
KARAOKE_NEXT_LINE_LEAD_US = 4_500_000
KARAOKE_INSTRUMENTAL_GAP_US = 1_500_000
KARAOKE_INSTRUMENTAL_LEAD_US = 1_500_000
KARAOKE_MAX_ROW_CHARACTERS = 48
SONG_CREDIT_ACCENT = (242, 184, 75, 255)
SONG_CREDIT_DURATION_SECONDS = 5.0
SONG_CREDIT_FADE_OUT_SECONDS = 0.75


def karaoke_countdown_number(gap_start_us: int, next_start_us: int, now_us: int) -> int | None:
    gap_us = next_start_us - gap_start_us
    until_us = next_start_us - now_us
    if (
        gap_us < KARAOKE_COUNTDOWN_US
        or now_us < gap_start_us
        or until_us <= 0
        or until_us > KARAOKE_COUNTDOWN_US
    ):
        return None
    return max(1, min(3, math.ceil(until_us / 1_000_000)))


def visible_karaoke_rows(timeline: TimelineV1, now_us: int) -> list[tuple[int, bool]]:
    def lead_us(next_index: int) -> int:
        if next_index <= 0:
            return KARAOKE_NEXT_LINE_LEAD_US
        gap_us = timeline.lines[next_index].start_us - timeline.lines[next_index - 1].end_us
        return (
            KARAOKE_INSTRUMENTAL_LEAD_US
            if gap_us >= KARAOKE_INSTRUMENTAL_GAP_US
            else KARAOKE_NEXT_LINE_LEAD_US
        )

    active = next(
        (
            index
            for index, line in enumerate(timeline.lines)
            if line.start_us <= now_us < line.end_us
        ),
        None,
    )
    if active is not None:
        rows = [(active, True)]
        if active + 1 < len(timeline.lines) and timeline.lines[
            active + 1
        ].start_us - now_us <= lead_us(active + 1):
            rows.append((active + 1, False))
        return rows
    upcoming = next(
        (index for index, line in enumerate(timeline.lines) if line.start_us > now_us),
        None,
    )
    if upcoming is not None and timeline.lines[upcoming].start_us - now_us <= lead_us(upcoming):
        return [(upcoming, False)]
    return []


@dataclass(frozen=True)
class LyricDisplayRow:
    text: str
    start_progress_ppm: int
    end_progress_ppm: int


def lyric_display_rows(
    line: LineTiming,
    font: ImageFont.FreeTypeFont | None = None,
    max_row_characters: int = KARAOKE_MAX_ROW_CHARACTERS,
) -> list[LyricDisplayRow]:
    """Split long display text without changing the authoritative lyric."""
    single = [LyricDisplayRow(line.text, 0, 1_000_000)]
    if len(line.text.strip()) <= max(1, max_row_characters) or len(line.tokens) < 2:
        return single

    starts: list[int] = []
    cursor = 0
    for token in line.tokens:
        found = line.text.find(token.text, cursor)
        start = found if found >= 0 else cursor
        starts.append(max(cursor, start))
        cursor = min(len(line.text), starts[-1] + len(token.text))
    segments = [
        line.text[
            0 if index == 0 else starts[index] : starts[index + 1]
            if index + 1 < len(starts)
            else len(line.text)
        ]
        for index in range(len(starts))
    ]

    best_index = -1
    best_score = math.inf
    for index in range(1, len(segments)):
        first = "".join(segments[:index]).rstrip()
        second = "".join(segments[index:]).lstrip()
        if not first or not second:
            continue
        first_length = len(first.strip())
        second_length = len(second.strip())
        score = max(first_length, second_length) * 2 + abs(first_length - second_length)
        if score < best_score:
            best_index = index
            best_score = score
    if best_index < 1:
        return single

    first_text = "".join(segments[:best_index]).rstrip()
    second_text = "".join(segments[best_index:]).lstrip()
    boundary_offset = sum(len(segment) for segment in segments[:best_index])
    previous_sweep = line.tokens[best_index - 1].sweep
    next_sweep = line.tokens[best_index].sweep
    if next_sweep and next_sweep.points:
        boundary_ppm = next_sweep.points[0].line_progress_ppm
    elif previous_sweep and previous_sweep.points:
        boundary_ppm = previous_sweep.points[-1].line_progress_ppm
    elif font is not None:
        total_width = max(1e-9, float(font.getlength(line.text)))
        boundary_ppm = round(
            float(font.getlength(line.text[:boundary_offset])) * 1_000_000 / total_width
        )
    else:
        boundary_ppm = round(boundary_offset * 1_000_000 / max(1, len(line.text)))
    boundary_ppm = max(1, min(999_999, boundary_ppm))
    return [
        LyricDisplayRow(first_text, 0, boundary_ppm),
        LyricDisplayRow(second_text, boundary_ppm, 1_000_000),
    ]


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
    if name == "1080p120":
        return RenderPreset(1920, 1080, 120, 1)
    if name == "1080p30":
        return RenderPreset(1920, 1080, 30, 1)
    if name == "source":
        try:
            fps = Fraction(project.fps)
        except (ValueError, ZeroDivisionError):
            fps = Fraction(30, 1)
        return RenderPreset(project.width, project.height, fps.numerator, fps.denominator)
    return RenderPreset(1920, 1080, 60, 1)


@dataclass
class RenderedLineRow:
    text: str
    start_progress_ppm: int
    end_progress_ppm: int
    natural_text_width: float
    scale_x: float
    text_width: float
    left: float
    base: Image.Image
    highlight: Image.Image
    glow: Image.Image


def song_credit_card(
    title: str,
    artist: str,
    preset: RenderPreset,
    font_path: Path,
) -> Image.Image:
    """Build the opening title/artist card burned into the first five seconds."""
    scale = max(0.45, preset.height / 1080)
    card_width = min(round(preset.width * 0.68), round(1080 * scale))
    card_height = round((142 if artist.strip() else 112) * scale)
    radius = max(6, round(15 * scale))
    padding_x = max(12, round(24 * scale))
    padding_y = max(9, round(17 * scale))
    accent_width = max(3, round(6 * scale))
    accent_gap = max(8, round(18 * scale))
    text_left = padding_x + accent_width + accent_gap
    available_width = max(1, card_width - text_left - padding_x)
    title_text = title.strip() or "Karaoke"
    artist_text = artist.strip()

    def fitted_font(text: str, initial_size: int, minimum_size: int) -> ImageFont.FreeTypeFont:
        size = max(minimum_size, initial_size)
        while size > minimum_size:
            candidate = load_font(font_path, size)
            if candidate.getlength(text) <= available_width:
                return candidate
            size -= 1
        return load_font(font_path, minimum_size)

    title_font = fitted_font(
        title_text,
        max(16, round(48 * scale)),
        max(11, round(24 * scale)),
    )
    artist_label = f"CA SĨ / NGUỒN · {artist_text}" if artist_text else ""
    artist_font = fitted_font(
        artist_label,
        max(10, round(20 * scale)),
        max(8, round(13 * scale)),
    )
    card = Image.new("RGBA", (card_width, card_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (0, 0, card_width - 1, card_height - 1),
        radius=radius,
        fill=(3, 8, 16, 188),
        outline=(255, 255, 255, 32),
        width=max(1, round(scale)),
    )
    draw.rounded_rectangle(
        (padding_x, padding_y, padding_x + accent_width, card_height - padding_y),
        radius=max(2, accent_width // 2),
        fill=SONG_CREDIT_ACCENT,
    )
    if artist_label:
        title_y = round(card_height * 0.39)
        artist_y = round(card_height * 0.72)
    else:
        title_y = card_height // 2
        artist_y = 0
    draw.text(
        (text_left, title_y),
        title_text,
        font=title_font,
        anchor="lm",
        fill=WHITE,
        stroke_width=max(1, round(scale)),
        stroke_fill=(0, 0, 0, 175),
    )
    if artist_label:
        draw.text(
            (text_left, artist_y),
            artist_label,
            font=artist_font,
            anchor="lm",
            fill=(248, 217, 146, 255),
            stroke_width=max(1, round(scale)),
            stroke_fill=(0, 0, 0, 165),
        )
    return card


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
        self.base = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
        self.highlight = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
        self.glow = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
        display_rows = lyric_display_rows(line, font)
        row_spacing = round(font.size * KARAOKE_ROW_SPACING_EM)
        row_y_values = (
            [y] if len(display_rows) == 1 else [y - row_spacing // 2, y + row_spacing // 2]
        )
        stroke_width = max(4, round(font.size * 0.075))
        shadow_offset = max(3, round(font.size * 0.065))
        self.rows: list[RenderedLineRow] = []
        for display_row, row_y in zip(display_rows, row_y_values, strict=True):
            natural_width = max(1.0, float(font.getlength(display_row.text)))
            scale_x = min(1.0, (width * 0.88) / natural_width)
            text_width = natural_width * scale_x
            left = (width - text_width) / 2
            base = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
            self._paste_text_layer(
                base,
                display_row.text,
                natural_width,
                scale_x,
                row_y + shadow_offset,
                KARAOKE_SHADOW,
                stroke_width=stroke_width,
                stroke_fill=KARAOKE_SHADOW,
            )
            self._paste_text_layer(
                base,
                display_row.text,
                natural_width,
                scale_x,
                row_y,
                WHITE if active else PREVIEW,
                stroke_width=stroke_width,
                stroke_fill=KARAOKE_SHADOW,
            )
            highlight = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
            self._paste_text_layer(
                highlight,
                display_row.text,
                natural_width,
                scale_x,
                row_y + shadow_offset,
                KARAOKE_SHADOW,
                stroke_width=stroke_width,
                stroke_fill=KARAOKE_SHADOW,
            )
            self._paste_text_layer(
                highlight,
                display_row.text,
                natural_width,
                scale_x,
                row_y,
                highlight_color,
                stroke_width=stroke_width,
                stroke_fill=WHITE,
            )
            color_layer = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 0))
            self._paste_text_layer(
                color_layer,
                display_row.text,
                natural_width,
                scale_x,
                row_y,
                highlight_color,
            )
            glow = color_layer.filter(ImageFilter.GaussianBlur(max(4, round(font.size * 0.09))))
            self.base.alpha_composite(base)
            self.highlight.alpha_composite(highlight)
            self.glow.alpha_composite(glow)
            self.rows.append(
                RenderedLineRow(
                    display_row.text,
                    display_row.start_progress_ppm,
                    display_row.end_progress_ppm,
                    natural_width,
                    scale_x,
                    text_width,
                    left,
                    base,
                    highlight,
                    glow,
                )
            )
        self.natural_text_width = max(row.natural_text_width for row in self.rows)
        self.scale_x = min(row.scale_x for row in self.rows)
        self.text_width = max(row.text_width for row in self.rows)
        self.left = (width - self.text_width) / 2

    def _paste_text_layer(
        self,
        destination: Image.Image,
        text: str,
        natural_text_width: float,
        scale_x: float,
        y: int,
        fill: tuple[int, int, int, int],
        stroke_width: int = 0,
        stroke_fill: tuple[int, int, int, int] | None = None,
    ) -> None:
        margin = max(10, stroke_width + 5)
        natural_layer_width = max(1, math.ceil(natural_text_width) + margin * 2)
        layer = Image.new("RGBA", (natural_layer_width, destination.height), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text(
            (natural_layer_width / 2, y),
            text,
            font=self.font,
            anchor="mm",
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        fitted_width = max(1, round(natural_layer_width * scale_x))
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
        self.font = load_font(self.font_path, round(self.preset.height * 0.076))
        self.wrapped_font = self.font
        self.line_fonts: dict[str, ImageFont.FreeTypeFont] = {}
        self.line_scales: dict[str, float] = {}
        self.assets: dict[tuple[int, bool], LineAsset] = {}
        lane_base = round(self.overlay_height * 0.75)
        wrapped_row_spacing = round(self.wrapped_font.size * KARAOKE_ROW_SPACING_EM)
        upper = lane_base - round(wrapped_row_spacing * 1.5)
        lower = lane_base + round(self.font.size * KARAOKE_LOWER_ROW_GAP_EM)
        self.lane_y = (upper, lower)
        for index, line in enumerate(timeline.lines):
            y = self.lane_y[index % 2]
            line_font = (
                self.wrapped_font if len(lyric_display_rows(line, self.font)) > 1 else self.font
            )
            self.line_fonts[line.id] = line_font
            self.assets[(index, True)] = LineAsset(
                line,
                line_font,
                y,
                self.width,
                self.overlay_height,
                True,
                self.highlight_color,
            )
            self.assets[(index, False)] = LineAsset(
                line,
                line_font,
                y,
                self.width,
                self.overlay_height,
                False,
                self.highlight_color,
            )
            self.line_scales[line.id] = self.assets[(index, True)].scale_x

    def frame(self, now_us: int) -> Image.Image:
        frame = Image.new("RGBA", (self.width, self.overlay_height), (0, 0, 0, 0))
        rows = visible_karaoke_rows(self.timeline, now_us)
        countdown: tuple[int, int] | None = None
        if not any(is_active for _index, is_active in rows):
            upcoming = next(
                (index for index, line in enumerate(self.timeline.lines) if line.start_us > now_us),
                None,
            )
            if upcoming is not None:
                gap_start = 0 if upcoming == 0 else self.timeline.lines[upcoming - 1].end_us
                countdown_number = karaoke_countdown_number(
                    gap_start,
                    self.timeline.lines[upcoming].start_us,
                    now_us,
                )
                if self.countdown and countdown_number is not None:
                    upcoming_lane = upcoming % 2
                    countdown = (
                        countdown_number,
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
        progress_ppm = self._line_progress_ppm(asset.line, now_us)
        for row in asset.rows:
            if progress_ppm <= row.start_progress_ppm:
                row_progress = 0.0
            elif progress_ppm >= row.end_progress_ppm:
                row_progress = 1.0
            else:
                row_progress = (progress_ppm - row.start_progress_ppm) / max(
                    1, row.end_progress_ppm - row.start_progress_ppm
                )
            boundary = round(row.left + row.text_width * row_progress)
            boundary = max(0, min(self.width, boundary))
            glow_right = min(self.width, boundary + round(asset.font.size * 0.35))
            if glow_right > 0:
                frame.alpha_composite(
                    row.glow.crop((0, 0, glow_right, self.overlay_height)), (0, 0)
                )
            if boundary > 0:
                frame.alpha_composite(
                    row.highlight.crop((0, 0, boundary, self.overlay_height)), (0, 0)
                )

    def _line_progress_ppm(self, line: LineTiming, now_us: int) -> int:
        if now_us <= line.start_us:
            return 0
        if now_us >= line.end_us:
            return 1_000_000
        progress_ppm = line_progress_ppm(line, now_us)
        if progress_ppm is not None:
            return max(0, min(1_000_000, progress_ppm))
        font = self.line_fonts.get(line.id, self.font)
        total_width = max(1e-9, float(font.getlength(line.text)))
        cursor = 0
        for token in line.tokens:
            found = line.text.find(token.text, cursor)
            token_start = found if found >= 0 else cursor
            token_end = min(len(line.text), token_start + len(token.text))
            word_left = float(font.getlength(line.text[:cursor]))
            word_right = float(font.getlength(line.text[:token_end]))
            if now_us < token.start_us:
                return round(word_left * 1_000_000 / total_width)
            if now_us <= token.end_us:
                progress = (now_us - token.start_us) / max(1, token.end_us - token.start_us)
                boundary = word_left + (word_right - word_left) * max(0.0, min(1.0, progress))
                return round(boundary * 1_000_000 / total_width)
            cursor = token_end
        return 1_000_000

    def _highlight_boundary(self, line: LineTiming, left: float, now_us: int) -> float:
        font = self.line_fonts.get(line.id, self.font)
        scale_x = self.line_scales.get(line.id, 1.0)
        return left + font.getlength(line.text) * scale_x * (
            self._line_progress_ppm(line, now_us) / 1_000_000
        )

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
        font = load_font(self.font_path, max(16, round(self.font.size * 0.3)))
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
    credit_path = project_dir / "work" / f"song-credit-{preset.width}x{preset.height}.png"
    credit_path.parent.mkdir(parents=True, exist_ok=True)
    song_credit_card(project.title, project.artist, preset, renderer.font_path).save(credit_path)
    credit_input_index = background_input_count
    overlay_input_index = background_input_count + 1
    audio_input_index = background_input_count + 2
    credit_x = round(preset.width * 0.04)
    credit_y = round(preset.height * 0.05)
    overlay_y = preset.height - renderer.overlay_height - round(preset.height * 0.025)
    credit_fade_start = SONG_CREDIT_DURATION_SECONDS - SONG_CREDIT_FADE_OUT_SECONDS
    filter_graph = (
        f"{background_filter};"
        f"[{credit_input_index}:v]format=rgba,"
        f"fade=t=out:st={credit_fade_start:.2f}:d={SONG_CREDIT_FADE_OUT_SECONDS:.2f}:alpha=1[credit];"
        f"[bg][credit]overlay={credit_x}:{credit_y}:eof_action=pass:format=auto:"
        f"enable='lt(t,{SONG_CREDIT_DURATION_SECONDS:.1f})'[credited];"
        f"[{overlay_input_index}:v]fps={preset.ffmpeg_fps},format=rgba[ov];"
        f"[credited][ov]overlay=0:{overlay_y}:shortest=1:format=auto[v]"
    )
    command = [
        settings.ffmpeg,
        "-hide_banner",
        "-y",
        *background_args,
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        f"{SONG_CREDIT_DURATION_SECONDS:.1f}",
        "-i",
        str(credit_path),
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
        "-fps_mode",
        "cfr",
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
            plan.segments[index + 1].transition_us if index + 1 < len(plan.segments) else 0
        )
        clip_duration_us = segment.end_us - segment.start_us + outgoing_transition_us
        motion_filter = _background_motion_filter(index, preset, clip_duration_us)
        filters.append(
            f"[{index}:v]fps={preset.ffmpeg_fps},"
            f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={preset.width}:{preset.height},setsar=1,"
            f"trim=duration={clip_duration_us / 1_000_000:.6f},"
            f"setpts=PTS-STARTPTS,{motion_filter},settb=AVTB,format=yuv420p[bg{index}]"
        )

    chain_label = "bg0"
    for index, segment in enumerate(plan.segments[1:], start=1):
        output_label = f"bgmix{index}"
        if segment.transition_us > 0:
            filters.append(
                f"[{chain_label}][bg{index}]xfade=transition=fadeslow:"
                f"duration={segment.transition_us / 1_000_000:.6f}:"
                f"offset={segment.start_us / 1_000_000:.6f}[{output_label}]"
            )
        else:
            filters.append(f"[{chain_label}][bg{index}]concat=n=2:v=1:a=0[{output_label}]")
        chain_label = output_label
    filters.append(
        f"[{chain_label}]trim=duration={plan.duration_us / 1_000_000:.6f},setpts=PTS-STARTPTS[bg]"
    )
    return input_args, ";".join(filters), len(plan.segments)


def _background_motion_filter(
    scene_index: int,
    preset: RenderPreset,
    clip_duration_us: int,
) -> str:
    """Return a subtle deterministic Ken Burns move for image and video scenes."""
    patterns = (
        (1.035, 1.085, -0.20, 0.20, 0.10, -0.10),
        (1.085, 1.045, 0.55, -0.55, -0.15, 0.15),
        (1.045, 1.085, -0.55, 0.55, 0.18, -0.18),
        (1.090, 1.040, 0.25, -0.25, 0.55, -0.55),
    )
    start_zoom, end_zoom, start_x, end_x, start_y, end_y = patterns[
        abs(scene_index) % len(patterns)
    ]
    total_frames = max(
        2,
        math.ceil(
            clip_duration_us
            * preset.fps_numerator
            / (1_000_000 * preset.fps_denominator)
        ),
    )
    denominator = total_frames - 1
    progress = f"on/{denominator}"
    zoom = f"{start_zoom:.6f}+({end_zoom - start_zoom:.6f})*{progress}"
    pan_x = f"{start_x:.6f}+({end_x - start_x:.6f})*{progress}"
    pan_y = f"{start_y:.6f}+({end_y - start_y:.6f})*{progress}"
    x = f"(iw-iw/zoom)/2*(1+({pan_x}))"
    y = f"(ih-ih/zoom)/2*(1+({pan_y}))"
    return (
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:"
        f"s={preset.width}x{preset.height}:fps={preset.ffmpeg_fps}"
    )


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
