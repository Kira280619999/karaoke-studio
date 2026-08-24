from __future__ import annotations

from karaoke_studio.fonts import (
    KARAOKE_FONTS,
    normalize_karaoke_font_id,
    resolve_karaoke_font,
)
from karaoke_studio.motion import load_font


def test_all_bundled_karaoke_fonts_render_vietnamese(test_settings) -> None:
    sample = "Xin Đức Thánh Linh đưa dắt chúng con"
    for spec in KARAOKE_FONTS:
        path = resolve_karaoke_font(test_settings, spec.id)
        font = load_font(path, 58)
        assert path.is_file()
        assert font.getlength(sample) > 0


def test_unknown_font_falls_back_for_old_timeline_metadata() -> None:
    assert normalize_karaoke_font_id("unknown") == "noto_sans"
    assert normalize_karaoke_font_id(None) == "noto_sans"
