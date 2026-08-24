from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KaraokeColorSpec:
    id: str
    label: str
    rgba: tuple[int, int, int, int]


KARAOKE_COLORS = (
    KaraokeColorSpec("yellow", "Vàng", (255, 212, 59, 255)),
    KaraokeColorSpec("red", "Đỏ", (255, 54, 87, 255)),
    KaraokeColorSpec("pink", "Hồng", (255, 79, 163, 255)),
)
DEFAULT_KARAOKE_COLOR_ID = "yellow"
_COLOR_BY_ID = {spec.id: spec for spec in KARAOKE_COLORS}


def is_karaoke_color_id(value: str | None) -> bool:
    return value in _COLOR_BY_ID


def normalize_karaoke_color_id(value: str | None) -> str:
    return value if value in _COLOR_BY_ID else DEFAULT_KARAOKE_COLOR_ID


def karaoke_color_rgba(value: str | None) -> tuple[int, int, int, int]:
    return _COLOR_BY_ID[normalize_karaoke_color_id(value)].rgba
