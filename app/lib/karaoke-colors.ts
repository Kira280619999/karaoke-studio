export const DEFAULT_KARAOKE_COLOR_ID = 'yellow';

export const KARAOKE_COLORS = [
  { id: 'yellow', label: 'Vàng', hint: 'Sáng sân khấu', hex: '#ffd43b' },
  { id: 'red', label: 'Đỏ', hint: 'Đậm kiểu TV', hex: '#ff3657' },
  { id: 'pink', label: 'Hồng', hint: 'Tươi hiện đại', hex: '#ff4fa3' },
] as const;

export type KaraokeColorId = (typeof KARAOKE_COLORS)[number]['id'];

export function karaokeColorId(metadata: Record<string, string>): KaraokeColorId {
  const candidate = metadata.karaoke_color;
  return KARAOKE_COLORS.some((color) => color.id === candidate)
    ? candidate as KaraokeColorId
    : DEFAULT_KARAOKE_COLOR_ID;
}

export function karaokeColorHex(colorId: KaraokeColorId): string {
  return KARAOKE_COLORS.find((color) => color.id === colorId)?.hex ?? '#ffd43b';
}

export function karaokeColorLabel(colorId: KaraokeColorId): string {
  return KARAOKE_COLORS.find((color) => color.id === colorId)?.label ?? 'Vàng';
}
