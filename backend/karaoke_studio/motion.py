from __future__ import annotations

import bisect
import math
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont, features

from .fonts import resolve_karaoke_font
from .models import LineTiming, SweepCurveV1, SweepPointV1, TimelineV1
from .settings import Settings

PPM = 1_000_000
VIETNAMESE_VOWELS = frozenset("aeiouy")


@dataclass(frozen=True)
class GraphemeSpan:
    grapheme_index: int
    text: str
    start_us: int
    end_us: int
    confidence: float


@dataclass(frozen=True)
class RhythmGrid:
    times_us: tuple[int, ...] = ()
    strengths: tuple[float, ...] = ()

    def nearest(self, value_us: int, maximum_distance_us: int = 40_000) -> tuple[int, float] | None:
        if not self.times_us:
            return None
        index = bisect.bisect_left(self.times_us, value_us)
        candidates = [candidate for candidate in (index - 1, index) if 0 <= candidate < len(self.times_us)]
        if not candidates:
            return None
        selected = min(candidates, key=lambda item: abs(self.times_us[item] - value_us))
        if abs(self.times_us[selected] - value_us) > maximum_distance_us:
            return None
        strength = self.strengths[selected] if selected < len(self.strengths) else 0.0
        return self.times_us[selected], strength


def resolve_font(settings: Settings, font_id: str | None = None) -> Path:
    """Compatibility wrapper used by alignment and rendering code."""
    return resolve_karaoke_font(settings, font_id)


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if features.check("raqm"):
        font = ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.RAQM)
    else:
        font = ImageFont.truetype(str(path), size)
    with suppress(AttributeError, OSError):
        font.set_variation_by_name("Bold")
    return font


def split_graphemes(text: str) -> list[tuple[int, int, str]]:
    """Split enough of Unicode grapheme clusters for exact Vietnamese display text.

    Combining marks, variation selectors and joiner sequences remain attached to
    the preceding base character. The original string is never normalized here.
    """
    if not text:
        return []
    result: list[tuple[int, int, str]] = []
    start = 0
    join_next = False
    for index, character in enumerate(text):
        category = unicodedata.category(character)
        attaches = index > start and (
            bool(unicodedata.combining(character))
            or category in {"Mn", "Mc", "Me"}
            or ord(character) in range(0xFE00, 0xFE10)
            or join_next
            or character == "\u200d"
        )
        if not attaches and index > start:
            result.append((start, index, text[start:index]))
            start = index
        join_next = character == "\u200d"
    result.append((start, len(text), text[start:]))
    return result


def is_sung_grapheme(value: str) -> bool:
    normalized = unicodedata.normalize("NFD", value).casefold()
    return any(unicodedata.category(character)[0] in {"L", "N"} for character in normalized)


def is_vowel_grapheme(value: str) -> bool:
    normalized = unicodedata.normalize("NFD", value).casefold()
    return any(character in VIETNAMESE_VOWELS for character in normalized)


def token_display_ranges(line: LineTiming) -> list[tuple[int, int]]:
    """Return visual ranges whose starts coincide with vocal token onsets.

    A token owns its visible word and the whitespace before the next word. This
    makes the next word's first glyph begin exactly at the next acoustic onset,
    while spaces and punctuation still sweep continuously without a jump.
    """
    starts: list[int] = []
    cursor = 0
    for token in line.tokens:
        found = line.text.find(token.text, cursor)
        start = found if found >= 0 else cursor
        starts.append(max(cursor, start))
        cursor = min(len(line.text), starts[-1] + len(token.text))
    return [
        (start, starts[index + 1] if index + 1 < len(starts) else len(line.text))
        for index, start in enumerate(starts)
    ]


def canonical_progress_ppm(text: str, indexes: list[int], font_path: Path | None) -> list[int]:
    if not text:
        return [0 for _index in indexes]
    if font_path is None:
        total = max(1, len(text))
        return [max(0, min(PPM, round(index * PPM / total))) for index in indexes]
    font = load_font(font_path, 100)
    total_width = max(1e-9, float(font.getlength(text)))
    return [
        max(0, min(PPM, round(float(font.getlength(text[:index])) * PPM / total_width)))
        for index in indexes
    ]


def linear_sweep(
    line: LineTiming,
    token_index: int,
    source: str,
    confidence: float,
    verified: bool,
    font_path: Path | None = None,
) -> SweepCurveV1:
    token = line.tokens[token_index]
    start, end = token_display_ranges(line)[token_index]
    progress = canonical_progress_ppm(line.text, [start, end], font_path)
    return SweepCurveV1(
        source=source,
        confidence=max(0.0, min(1.0, confidence)),
        verified=verified,
        points=[
            SweepPointV1(time_us=token.start_us, line_progress_ppm=progress[0]),
            SweepPointV1(time_us=token.end_us, line_progress_ppm=progress[1]),
        ],
    )


def remap_timeline_sweep_font(timeline: TimelineV1, font_path: Path) -> TimelineV1:
    """Keep acoustic times but remap visual progress to a newly selected font.

    Sweep points are stored as line-width PPM values. Those values depend on
    glyph widths, so changing family must not leave word onsets tied to the old
    family's geometry.
    """
    result = timeline.model_copy(deep=True)
    for line in result.lines:
        display_ranges = token_display_ranges(line)
        for token_index, token in enumerate(line.tokens):
            if token.sweep is None or token_index >= len(display_ranges):
                continue
            display_start, display_end = display_ranges[token_index]
            display_indexes = [display_start]
            display_indexes.extend(
                display_start + end for _start, end, _text in split_graphemes(token.text)
            )
            if display_indexes[-1] != display_end:
                display_indexes.append(display_end)
            target = canonical_progress_ppm(line.text, display_indexes, font_path)
            points = token.sweep.points
            if len(points) == len(target):
                progress = target
            elif len(points) == 2:
                progress = [target[0], target[-1]]
            else:
                first = target[0]
                span = target[-1] - first
                progress = [
                    first + round(span * index / max(1, len(points) - 1))
                    for index in range(len(points))
                ]
            token.sweep = token.sweep.model_copy(
                update={
                    "points": [
                        point.model_copy(update={"line_progress_ppm": progress[index]})
                        for index, point in enumerate(points)
                    ]
                }
            )
    return result


def evaluate_sweep_ppm(curve: SweepCurveV1, now_us: int) -> int:
    points = curve.points
    if now_us <= points[0].time_us:
        return points[0].line_progress_ppm
    if now_us >= points[-1].time_us:
        return points[-1].line_progress_ppm
    times = [point.time_us for point in points]
    right = bisect.bisect_right(times, now_us)
    first = points[right - 1]
    second = points[right]
    elapsed = now_us - first.time_us
    duration = max(1, second.time_us - first.time_us)
    delta = second.line_progress_ppm - first.line_progress_ppm
    return first.line_progress_ppm + round(elapsed * delta / duration)


def evaluate_display_sweep_ppm(curve: SweepCurveV1, now_us: int) -> int:
    """Damp internal CTC velocity jumps without moving token boundaries."""
    points = curve.points
    if len(points) <= 2:
        return evaluate_sweep_ppm(curve, now_us)
    first = points[0]
    last = points[-1]
    if now_us <= first.time_us:
        return first.line_progress_ppm
    if now_us >= last.time_us:
        return last.line_progress_ppm
    duration = max(1, last.time_us - first.time_us)
    linear = first.line_progress_ppm + round(
        (now_us - first.time_us)
        * (last.line_progress_ppm - first.line_progress_ppm)
        / duration
    )
    acoustic = evaluate_sweep_ppm(curve, now_us)
    return round((acoustic + linear * 3) / 4)


def _monotone_tangents(points: list[tuple[int, int]]) -> list[float]:
    """Return PCHIP tangents for a monotone time/progress path."""
    if len(points) == 2:
        slope = (points[1][1] - points[0][1]) / max(1, points[1][0] - points[0][0])
        return [slope, slope]
    intervals = [points[index + 1][0] - points[index][0] for index in range(len(points) - 1)]
    slopes = [
        (points[index + 1][1] - points[index][1]) / max(1, intervals[index])
        for index in range(len(points) - 1)
    ]
    tangents = [0.0] * len(points)

    def endpoint_tangent(first_h: int, second_h: int, first_slope: float, second_slope: float) -> float:
        tangent = ((2 * first_h + second_h) * first_slope - first_h * second_slope) / (
            first_h + second_h
        )
        if tangent * first_slope <= 0:
            return 0.0
        if first_slope * second_slope < 0 and abs(tangent) > abs(3 * first_slope):
            return 3 * first_slope
        return tangent

    tangents[0] = endpoint_tangent(intervals[0], intervals[1], slopes[0], slopes[1])
    tangents[-1] = endpoint_tangent(
        intervals[-1], intervals[-2], slopes[-1], slopes[-2]
    )
    for index in range(1, len(points) - 1):
        before = slopes[index - 1]
        after = slopes[index]
        if before <= 0 or after <= 0:
            tangents[index] = 0.0
            continue
        before_h = intervals[index - 1]
        after_h = intervals[index]
        first_weight = 2 * after_h + before_h
        second_weight = after_h + 2 * before_h
        tangents[index] = (first_weight + second_weight) / (
            first_weight / before + second_weight / after
        )
    return tangents


def _evaluate_monotone_path_ppm(points: list[tuple[int, int]], now_us: int) -> int:
    if now_us <= points[0][0]:
        return points[0][1]
    if now_us >= points[-1][0]:
        return points[-1][1]
    times = [point[0] for point in points]
    right = bisect.bisect_right(times, now_us)
    left = right - 1
    start_time, start_progress = points[left]
    end_time, end_progress = points[right]
    duration = max(1, end_time - start_time)
    position = (now_us - start_time) / duration
    position_squared = position * position
    position_cubed = position_squared * position
    tangents = _monotone_tangents(points)
    progress = (
        (2 * position_cubed - 3 * position_squared + 1) * start_progress
        + (position_cubed - 2 * position_squared + position)
        * duration
        * tangents[left]
        + (-2 * position_cubed + 3 * position_squared) * end_progress
        + (position_cubed - position_squared) * duration * tangents[right]
    )
    return round(max(start_progress, min(end_progress, progress)))


def cinematic_line_progress_ppm(line: LineTiming, now_us: int) -> int | None:
    """Smooth the display across analyzed word boundaries without moving them."""
    if not line.tokens or any(token.sweep is None for token in line.tokens):
        return None
    points: list[tuple[int, int]] = []
    for token in line.tokens:
        assert token.sweep is not None
        for point in (token.sweep.points[0], token.sweep.points[-1]):
            candidate = (point.time_us, point.line_progress_ppm)
            if points and candidate[0] == points[-1][0]:
                if candidate[1] != points[-1][1]:
                    return None
                continue
            if points and (candidate[0] < points[-1][0] or candidate[1] < points[-1][1]):
                return None
            points.append(candidate)
    if len(points) < 2:
        return None
    return _evaluate_monotone_path_ppm(points, now_us)


def line_progress_ppm(line: LineTiming, now_us: int) -> int | None:
    if now_us <= line.start_us:
        return 0
    if now_us >= line.end_us:
        return PPM
    cinematic_progress = cinematic_line_progress_ppm(line, now_us)
    if cinematic_progress is not None:
        return cinematic_progress
    previous_progress = 0
    for token in line.tokens:
        if now_us < token.start_us:
            return previous_progress
        if now_us <= token.end_us:
            return (
                evaluate_display_sweep_ppm(token.sweep, now_us)
                if token.sweep is not None
                else None
            )
        if token.sweep is not None:
            previous_progress = token.sweep.points[-1].line_progress_ppm
    return PPM


def rescale_sweep(
    curve: SweepCurveV1 | None,
    old_start_us: int,
    old_end_us: int,
    new_start_us: int,
    new_end_us: int,
) -> SweepCurveV1 | None:
    if curve is None:
        return None
    old_duration = max(1, old_end_us - old_start_us)
    new_duration = max(1, new_end_us - new_start_us)
    points = [
        SweepPointV1(
            time_us=new_start_us
            + round((point.time_us - old_start_us) * new_duration / old_duration),
            line_progress_ppm=point.line_progress_ppm,
        )
        for point in curve.points
    ]
    points[0].time_us = new_start_us
    points[-1].time_us = new_end_us
    return regularize_sweep(
        SweepCurveV1(
            source="manual_rescaled",
            confidence=min(0.77, curve.confidence),
            verified=False,
            points=points,
        ),
        new_start_us,
        new_end_us,
    )


def regularize_sweep(
    curve: SweepCurveV1, start_us: int, end_us: int
) -> SweepCurveV1:
    if end_us <= start_us:
        raise ValueError("Sweep token phải có duration dương.")
    ordered = curve.points
    first_progress = ordered[0].line_progress_ppm
    last_progress = max(first_progress, ordered[-1].line_progress_ppm)
    intervals = max(1, len(ordered) - 1)
    minimum_step_us = min(16_667, max(1, (end_us - start_us) // intervals))
    result: list[SweepPointV1] = []
    for index, point in enumerate(ordered):
        minimum_time = (
            start_us if index == 0 else result[-1].time_us + minimum_step_us
        )
        remaining = len(ordered) - index - 1
        maximum_time = end_us - remaining * minimum_step_us
        time_us = max(minimum_time, min(maximum_time, point.time_us))
        progress = max(
            first_progress if not result else result[-1].line_progress_ppm,
            min(last_progress, point.line_progress_ppm),
        )
        result.append(SweepPointV1(time_us=time_us, line_progress_ppm=progress))
    result[0].time_us = start_us
    result[-1].time_us = end_us
    return curve.model_copy(update={"points": result})


def interpolate_missing_times(values: list[int | None], start_us: int, end_us: int) -> list[int]:
    if len(values) < 2:
        return [start_us, end_us]
    values[0] = start_us
    values[-1] = end_us
    known = [index for index, value in enumerate(values) if value is not None]
    for left_index, right_index in zip(known, known[1:], strict=False):
        left = int(values[left_index] or 0)
        right = int(values[right_index] or left)
        distance = right_index - left_index
        for index in range(left_index + 1, right_index):
            values[index] = left + round((right - left) * (index - left_index) / distance)
    return [int(value if value is not None else start_us) for value in values]


def weighted_median(values: list[int], weights: list[float]) -> int:
    if not values:
        raise ValueError("Không có giá trị cho weighted median.")
    ordered = sorted(zip(values, weights, strict=True), key=lambda item: item[0])
    threshold = sum(max(0.0, weight) for _value, weight in ordered) / 2
    total = 0.0
    for value, weight in ordered:
        total += max(0.0, weight)
        if total >= threshold:
            return value
    return ordered[-1][0]


def smooth_subdivision_grid(beat_times_us: list[int], beat_strengths: list[float]) -> RhythmGrid:
    if len(beat_times_us) < 2:
        return RhythmGrid(tuple(beat_times_us), tuple(beat_strengths))
    times: list[int] = []
    strengths: list[float] = []
    for index, (left, right) in enumerate(zip(beat_times_us, beat_times_us[1:], strict=False)):
        base_strength = beat_strengths[index] if index < len(beat_strengths) else 0.5
        for part in range(4):
            times.append(left + round((right - left) * part / 4))
            strengths.append(base_strength if part == 0 else base_strength * (0.72 if part == 2 else 0.52))
    times.append(beat_times_us[-1])
    strengths.append(beat_strengths[-1] if beat_strengths else 0.5)
    ordered = sorted(zip(times, strengths, strict=True))
    return RhythmGrid(tuple(value for value, _strength in ordered), tuple(strength for _value, strength in ordered))


def finite_number(value: float) -> float:
    return value if math.isfinite(value) else 0.0
