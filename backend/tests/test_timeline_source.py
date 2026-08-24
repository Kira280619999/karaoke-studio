from __future__ import annotations

import pytest

from karaoke_studio.timeline_source import TimelineSourceError, parse_timeline_source


def test_srt_preserves_supplied_lyrics_and_explicit_cue_end() -> None:
    timeline = parse_timeline_source(
        """1
00:00:00,500 --> 00:00:02,250
Ngài là ánh sáng

2
00:00:02,500 --> 00:00:04,000
Dẫn con đi
""",
        duration_us=5_000_000,
        filename="lyrics.srt",
    )

    assert timeline.metadata["timeline_format"] == "srt"
    assert timeline.metadata["lyrics_provenance"] == "user_supplied"
    assert [line.text for line in timeline.lines] == ["Ngài là ánh sáng", "Dẫn con đi"]
    assert timeline.lines[0].start_us == 500_000
    assert timeline.lines[0].end_us == 2_250_000


def test_webvtt_and_multiline_cue_are_accepted() -> None:
    timeline = parse_timeline_source(
        """WEBVTT

intro
00:00.250 --> 00:02.000 align:center
Thánh thay
Thánh thay
""",
        duration_us=3_000_000,
        filename="lyrics.vtt",
    )

    assert timeline.metadata["timeline_format"] == "vtt"
    assert timeline.lines[0].text == "Thánh thay Thánh thay"
    assert timeline.lines[0].end_us == 2_000_000


def test_plain_pasted_timestamps_are_auto_detected() -> None:
    timeline = parse_timeline_source(
        "00:00.50 Lời đầu tiên\n00:02.750 - Lời thứ hai",
        duration_us=4_000_000,
        filename="pasted-timeline.txt",
    )

    assert timeline.metadata["timeline_format"] == "timestamp"
    assert [line.start_us for line in timeline.lines] == [500_000, 2_750_000]
    assert [line.text for line in timeline.lines] == ["Lời đầu tiên", "Lời thứ hai"]


def test_txt_auto_detects_lrc_and_inline_subtitle() -> None:
    lrc = parse_timeline_source(
        "[00:00.10]Giữ nguyên lời",
        duration_us=2_000_000,
        filename="copied.txt",
    )
    inline = parse_timeline_source(
        "00:00.200 --> 00:01.500 Dòng SRT cùng hàng",
        duration_us=2_000_000,
        filename="copied.txt",
    )

    assert lrc.metadata["timeline_format"] == "lrc"
    assert lrc.lines[0].text == "Giữ nguyên lời"
    assert inline.lines[0].start_us == 200_000
    assert inline.lines[0].end_us == 1_500_000


@pytest.mark.parametrize("content", ["Chỉ có lời không có giờ", "00:99.00 Sai giây"])
def test_invalid_pasted_timeline_fails_closed(content: str) -> None:
    with pytest.raises(TimelineSourceError):
        parse_timeline_source(content, duration_us=3_000_000, filename="copied.txt")


def test_unsupported_timeline_extension_is_rejected() -> None:
    with pytest.raises(TimelineSourceError, match="định dạng"):
        parse_timeline_source(
            "[00:00.10]Không nhận file này",
            duration_us=2_000_000,
            filename="lyrics.docx",
        )
