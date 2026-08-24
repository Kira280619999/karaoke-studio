from __future__ import annotations

from karaoke_studio.styles import (
    KARAOKE_COLORS,
    is_karaoke_color_id,
    karaoke_color_rgba,
    normalize_karaoke_color_id,
)


def test_requested_karaoke_colors_are_available() -> None:
    assert [spec.id for spec in KARAOKE_COLORS] == ["yellow", "red", "pink"]
    assert is_karaoke_color_id("red")
    assert karaoke_color_rgba("pink") == (255, 79, 163, 255)


def test_unknown_karaoke_color_falls_back_to_yellow() -> None:
    assert normalize_karaoke_color_id(None) == "yellow"
    assert normalize_karaoke_color_id("blue") == "yellow"
