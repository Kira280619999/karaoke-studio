from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .models import LineTiming, TimelineV1, TimingSource, TokenTiming

LINE_TIME = re.compile(r"\[(\d{1,3}):(\d{2})(?:[\.:](\d{1,3}))?\]")
WORD_TIME = re.compile(r"<(\d{1,3}):(\d{2})(?:[\.:](\d{1,3}))?>")
METADATA = re.compile(r"^\[([A-Za-z]+):(.*)\]\s*$")
PUNCTUATION = re.compile(r"[^\w\u00C0-\u024F\u1E00-\u1EFF]+", re.UNICODE)


@dataclass
class RawLine:
    start_us: int
    text: str
    enhanced: list[tuple[int, str]] = field(default_factory=list)


class LRCError(ValueError):
    pass


def _fraction_us(raw: str | None) -> int:
    if not raw:
        return 0
    return int(raw.ljust(3, "0")[:3]) * 1_000


def _time_us(match: re.Match[str]) -> int:
    return (int(match.group(1)) * 60 + int(match.group(2))) * 1_000_000 + _fraction_us(
        match.group(3)
    )


def normalize_token(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text).casefold()
    return PUNCTUATION.sub("", normalized)


def parse_lrc(content: str, duration_us: int, fps: int = 60) -> TimelineV1:
    if duration_us <= 0:
        raise LRCError("Video phải có duration lớn hơn 0.")
    content = content.removeprefix("\ufeff")
    metadata: dict[str, str] = {}
    raw_lines: list[RawLine] = []
    offset_ms = 0

    for source_line in content.splitlines():
        source_line = source_line.strip()
        if not source_line:
            continue
        meta = METADATA.match(source_line)
        if meta and not LINE_TIME.match(source_line):
            key, value = meta.group(1).lower(), meta.group(2).strip()
            metadata[key] = value
            if key == "offset":
                try:
                    offset_ms = int(value)
                except ValueError as exc:
                    raise LRCError("[offset] phải là số mili-giây.") from exc
            continue
        stamps = list(LINE_TIME.finditer(source_line))
        if not stamps:
            continue
        lyric = source_line[stamps[-1].end() :].strip()
        enhanced: list[tuple[int, str]] = []
        word_stamps = list(WORD_TIME.finditer(lyric))
        if word_stamps:
            display_parts: list[str] = []
            for index, stamp in enumerate(word_stamps):
                end = word_stamps[index + 1].start() if index + 1 < len(word_stamps) else len(lyric)
                part = lyric[stamp.end() : end]
                display_parts.append(part)
                enhanced.append((_time_us(stamp) + offset_ms * 1_000, part))
            lyric = "".join(display_parts).strip()
        if not lyric:
            continue
        for stamp in stamps:
            raw_lines.append(
                RawLine(
                    start_us=max(0, _time_us(stamp) + offset_ms * 1_000),
                    text=lyric,
                    enhanced=enhanced.copy(),
                )
            )

    raw_lines.sort(key=lambda item: item.start_us)
    if not raw_lines:
        raise LRCError("Không tìm thấy dòng lời có timestamp hợp lệ.")
    if raw_lines[0].start_us >= duration_us:
        raise LRCError("Timestamp đầu tiên nằm ngoài media.")
    if raw_lines[-1].start_us >= duration_us:
        raise LRCError("LRC có câu bắt đầu sau khi audio/video đã kết thúc.")

    lines: list[LineTiming] = []
    for index, raw in enumerate(raw_lines):
        next_start = raw_lines[index + 1].start_us if index + 1 < len(raw_lines) else duration_us
        line_end = min(duration_us, max(raw.start_us + 1, next_start))
        source = TimingSource.LRC_ENHANCED if raw.enhanced else TimingSource.LRC_LINE
        confidence = 0.97 if raw.enhanced else 0.62
        tokens = _tokens_for_line(raw, line_end, index, source, confidence)
        lines.append(
            LineTiming(
                id=f"line-{index + 1:04d}",
                text=raw.text,
                start_us=raw.start_us,
                end_us=line_end,
                confidence=confidence,
                source=source,
                verified=False,
                tokens=tokens,
            )
        )

    return TimelineV1(
        revision=1,
        duration_us=duration_us,
        fps_numerator=fps,
        metadata=metadata,
        lines=lines,
    )


def _tokens_for_line(
    raw: RawLine, line_end: int, line_index: int, source: TimingSource, confidence: float
) -> list[TokenTiming]:
    if raw.enhanced:
        result: list[TokenTiming] = []
        token_index = 0
        for segment_index, (segment_start, segment) in enumerate(raw.enhanced):
            segment_start = max(raw.start_us, min(line_end - 1, segment_start))
            segment_end = (
                raw.enhanced[segment_index + 1][0]
                if segment_index + 1 < len(raw.enhanced)
                else line_end
            )
            segment_end = max(segment_start + 1, min(line_end, segment_end))
            segment_tokens = segment.split()
            if not segment_tokens:
                continue
            weights = [max(1, len(normalize_token(text))) for text in segment_tokens]
            total = sum(weights)
            consumed = 0
            cursor = segment_start
            for local_index, (text, weight) in enumerate(zip(segment_tokens, weights, strict=True)):
                consumed += weight
                token_end = (
                    segment_end
                    if local_index == len(segment_tokens) - 1
                    else segment_start + round((segment_end - segment_start) * consumed / total)
                )
                token_end = max(cursor + 1, min(segment_end, token_end))
                result.append(
                    _token(
                        line_index,
                        token_index,
                        text,
                        cursor,
                        token_end,
                        source,
                        confidence,
                    )
                )
                token_index += 1
                cursor = token_end
        if result:
            return result

    display_tokens = raw.text.split()
    if not display_tokens:
        display_tokens = [raw.text]
    weights = [max(1, len(normalize_token(token))) for token in display_tokens]
    total_weight = sum(weights)
    duration = max(len(display_tokens), line_end - raw.start_us)
    result = []
    cursor = raw.start_us
    consumed = 0
    for token_index, (text, weight) in enumerate(zip(display_tokens, weights, strict=True)):
        consumed += weight
        end_us = (
            line_end
            if token_index == len(display_tokens) - 1
            else raw.start_us + round(duration * consumed / total_weight)
        )
        end_us = max(cursor + 1, min(line_end, end_us))
        result.append(_token(line_index, token_index, text, cursor, end_us, source, confidence))
        cursor = end_us
    return result


def _token(
    line_index: int,
    token_index: int,
    text: str,
    start_us: int,
    end_us: int,
    source: TimingSource,
    confidence: float,
) -> TokenTiming:
    return TokenTiming(
        id=f"line-{line_index + 1:04d}-token-{token_index + 1:03d}",
        text=text,
        normalized=normalize_token(text),
        start_us=start_us,
        end_us=end_us,
        confidence=confidence,
        source=source,
        verified=False,
    )
