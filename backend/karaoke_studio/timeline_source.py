from __future__ import annotations

import re
from pathlib import Path

from .lrc import LINE_TIME, LRCError, RawLine, build_timeline, parse_lrc
from .models import TimelineV1

SUPPORTED_TIMELINE_EXTENSIONS = {".lrc", ".srt", ".vtt", ".txt"}

SUBTITLE_TIME = re.compile(
    r"^\s*(?P<start>(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?)"
    r"\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?)"
    r"(?:\s+(?:(?:align|line|position|size|vertical):\S+)\s*)*$"
)
INLINE_SUBTITLE = re.compile(
    r"^\s*(?P<start>(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?)"
    r"\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?)"
    r"\s+(?P<text>.+?)\s*$"
)
PLAIN_TIMESTAMP = re.compile(
    r"^\s*\[?(?P<time>(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?)\]?"
    r"\s*(?:[-–—|]\s*)?(?P<text>\S.*?)\s*$"
)


class TimelineSourceError(LRCError):
    pass


def parse_timeline_source(
    content: str,
    duration_us: int,
    filename: str | None = None,
    fps: int = 60,
) -> TimelineV1:
    """Parse a user-supplied LRC/SRT/VTT/plain timestamp timeline.

    The parser only reads timing syntax. It never transcribes, corrects, or
    replaces the visible lyric text supplied by the user.
    """
    content = content.removeprefix("\ufeff")
    suffix = Path(filename or "").suffix.casefold()
    if suffix and suffix not in SUPPORTED_TIMELINE_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_TIMELINE_EXTENSIONS))
        raise TimelineSourceError(f"Timeline phải có định dạng: {supported}.")

    detected = _detect_format(content, suffix)
    try:
        if detected == "lrc":
            timeline = parse_lrc(content, duration_us, fps=fps)
        elif detected in {"srt", "vtt"}:
            timeline = _parse_subtitle(content, duration_us, fps=fps)
        else:
            timeline = _parse_plain_timestamps(content, duration_us, fps=fps)
    except LRCError as exc:
        raise TimelineSourceError(str(exc)) from exc

    timeline.metadata["timeline_format"] = detected
    timeline.metadata["lyrics_provenance"] = "user_supplied"
    return timeline


def _detect_format(content: str, suffix: str) -> str:
    if suffix == ".lrc":
        return "lrc"
    if suffix == ".srt":
        return "srt"
    if suffix == ".vtt":
        return "vtt"
    if re.search(r"(?m)^\s*WEBVTT(?:\s|$)", content):
        return "vtt"
    if re.search(r"(?m)^\s*.+?\s*-->\s*.+?\s*$", content):
        return "srt"
    if LINE_TIME.search(content):
        return "lrc"
    return "timestamp"


def _parse_subtitle(content: str, duration_us: int, fps: int) -> TimelineV1:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw_lines: list[RawLine] = []
    index = 0
    while index < len(lines):
        timing = SUBTITLE_TIME.match(lines[index])
        if timing is None:
            inline = INLINE_SUBTITLE.match(lines[index])
            if inline:
                raw_lines.append(
                    RawLine(
                        start_us=_clock_us(inline.group("start")),
                        end_us=_clock_us(inline.group("end")),
                        text=inline.group("text").strip(),
                    )
                )
            index += 1
            continue

        start_us = _clock_us(timing.group("start"))
        end_us = _clock_us(timing.group("end"))
        index += 1
        lyric_lines: list[str] = []
        while index < len(lines):
            current = lines[index]
            if SUBTITLE_TIME.match(current):
                break
            if not current.strip():
                index += 1
                break
            # A numeric cue identifier belongs to the next timing block.
            if (
                current.strip().isdigit()
                and index + 1 < len(lines)
                and SUBTITLE_TIME.match(lines[index + 1])
            ):
                index += 1
                break
            lyric_lines.append(current.strip())
            index += 1

        # Karaoke Studio renders one cue per fixed lyric lane. Preserve every
        # supplied word while normalizing subtitle layout line breaks to spaces.
        lyric = " ".join(part for part in lyric_lines if part).strip()
        if lyric:
            raw_lines.append(RawLine(start_us=start_us, end_us=end_us, text=lyric))

    if not raw_lines:
        raise TimelineSourceError("Không tìm thấy cue SRT/VTT có timestamp và lời hợp lệ.")
    return build_timeline(raw_lines, duration_us, fps=fps)


def _parse_plain_timestamps(content: str, duration_us: int, fps: int) -> TimelineV1:
    raw_lines: list[RawLine] = []
    for source_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        source_line = source_line.strip()
        if not source_line:
            continue

        inline = INLINE_SUBTITLE.match(source_line)
        if inline:
            raw_lines.append(
                RawLine(
                    start_us=_clock_us(inline.group("start")),
                    end_us=_clock_us(inline.group("end")),
                    text=inline.group("text").strip(),
                )
            )
            continue

        match = PLAIN_TIMESTAMP.match(source_line)
        if match:
            raw_lines.append(
                RawLine(
                    start_us=_clock_us(match.group("time")),
                    text=match.group("text").strip(),
                )
            )

    if not raw_lines:
        raise TimelineSourceError(
            "Không tìm thấy timestamp hợp lệ. Dùng LRC, SRT/VTT hoặc dạng 00:12.500 Lời bài hát."
        )
    return build_timeline(raw_lines, duration_us, fps=fps)


def _clock_us(raw: str) -> int:
    normalized = raw.replace(",", ".")
    clock, dot, fraction = normalized.partition(".")
    parts = clock.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = (int(part) for part in parts)
    elif len(parts) == 3:
        hours, minutes, seconds = (int(part) for part in parts)
    else:
        raise TimelineSourceError(f"Timestamp không hợp lệ: {raw}")
    if seconds >= 60 or (len(parts) == 3 and minutes >= 60):
        raise TimelineSourceError(f"Timestamp không hợp lệ: {raw}")
    fraction_us = int((fraction if dot else "").ljust(6, "0")[:6] or "0")
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000_000 + fraction_us
