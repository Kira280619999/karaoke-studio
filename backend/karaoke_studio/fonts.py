from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .settings import Settings

DEFAULT_KARAOKE_FONT_ID = "noto_sans"


@dataclass(frozen=True)
class KaraokeFontSpec:
    id: str
    label: str
    filename: str


KARAOKE_FONTS: tuple[KaraokeFontSpec, ...] = (
    KaraokeFontSpec("noto_sans", "Studio rõ nét", "NotoSans-Variable.ttf"),
    KaraokeFontSpec("be_vietnam_pro", "Việt hiện đại", "BeVietnamPro-Bold.ttf"),
    KaraokeFontSpec("lexend", "Dễ đọc", "Lexend-Variable.ttf"),
    KaraokeFontSpec("barlow_condensed", "Câu dài gọn", "BarlowCondensed-Bold.ttf"),
    KaraokeFontSpec("baloo_2", "Mềm và vui", "Baloo2-Variable.ttf"),
)

_FONT_BY_ID = {spec.id: spec for spec in KARAOKE_FONTS}


def normalize_karaoke_font_id(value: str | None) -> str:
    candidate = (value or "").strip().casefold()
    return candidate if candidate in _FONT_BY_ID else DEFAULT_KARAOKE_FONT_ID


def is_karaoke_font_id(value: str | None) -> bool:
    return bool(value and value.strip().casefold() in _FONT_BY_ID)


def karaoke_font_spec(value: str | None) -> KaraokeFontSpec:
    return _FONT_BY_ID[normalize_karaoke_font_id(value)]


def resolve_karaoke_font(settings: Settings, value: str | None = None) -> Path:
    spec = karaoke_font_spec(value)
    bundled = settings.root / "backend" / "karaoke_studio" / "assets" / spec.filename
    if bundled.is_file():
        return bundled
    if spec.id != DEFAULT_KARAOKE_FONT_ID:
        return resolve_karaoke_font(settings, DEFAULT_KARAOKE_FONT_ID)
    raise RuntimeError("Không tìm thấy font Karaoke Unicode được đóng gói.")
