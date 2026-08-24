from __future__ import annotations

import pytest

from karaoke_studio.lrc import LRCError, normalize_token, parse_lrc
from karaoke_studio.models import TimingSource


def test_standard_lrc_metadata_offset_and_repeated_timestamps() -> None:
    timeline = parse_lrc(
        """[ar:Ca sĩ]
[ti:Bài hát]
[offset:100]
[00:01.00][00:03.00]Xin chào Việt Nam
[00:04.00]Một ngày mới
""",
        duration_us=6_000_000,
    )
    assert timeline.metadata["ar"] == "Ca sĩ"
    assert [line.start_us for line in timeline.lines] == [1_100_000, 3_100_000, 4_100_000]
    assert timeline.lines[0].tokens[2].text == "Việt"
    assert timeline.lines[0].tokens[-1].end_us == timeline.lines[0].end_us
    assert all(line.source == TimingSource.LRC_LINE for line in timeline.lines)


def test_enhanced_lrc_preserves_display_and_word_stamps() -> None:
    timeline = parse_lrc(
        "[00:01.00]<00:01.00>Ngài <00:01.50>là <00:02.10>ánh sáng\n[00:03.00]Dẫn con đi",
        duration_us=5_000_000,
    )
    line = timeline.lines[0]
    assert line.text == "Ngài là ánh sáng"
    assert [token.start_us for token in line.tokens] == [
        1_000_000,
        1_500_000,
        2_100_000,
        2_485_714,
    ]
    assert line.source == TimingSource.LRC_ENHANCED


def test_unicode_normalization_keeps_vietnamese_letters() -> None:
    assert normalize_token("Đấng!") == "đấng"
    assert normalize_token("NGÀI,") == "ngài"


@pytest.mark.parametrize("content", ["không có timestamp", "[offset:x]\n[00:01.00]Xin chào"])
def test_invalid_lrc_fails_closed(content: str) -> None:
    with pytest.raises(LRCError):
        parse_lrc(content, duration_us=2_000_000)


def test_lrc_rejects_late_lines_outside_media_duration() -> None:
    with pytest.raises(LRCError, match="audio/video"):
        parse_lrc("[00:00.50]Xin chào\n[00:04.00]Ngoài media", duration_us=3_000_000)
