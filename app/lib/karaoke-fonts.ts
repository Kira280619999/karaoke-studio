export const DEFAULT_KARAOKE_FONT_ID = 'noto_sans';

export const KARAOKE_FONTS = [
  { id: 'noto_sans', label: 'Studio rõ nét', hint: 'Cân bằng, dễ đọc', family: 'Karaoke Noto' },
  { id: 'be_vietnam_pro', label: 'Việt hiện đại', hint: 'Dấu tiếng Việt đẹp', family: 'Karaoke Be Vietnam Pro' },
  { id: 'lexend', label: 'Dễ đọc', hint: 'Rõ chữ từ xa', family: 'Karaoke Lexend' },
  { id: 'barlow_condensed', label: 'Câu dài gọn', hint: 'Ít phải nén ngang', family: 'Karaoke Barlow Condensed' },
  { id: 'baloo_2', label: 'Mềm và vui', hint: 'Phong cách trẻ', family: 'Karaoke Baloo 2' },
] as const;

export type KaraokeFontId = (typeof KARAOKE_FONTS)[number]['id'];

export function karaokeFontId(metadata: Record<string, string>): KaraokeFontId {
  const candidate = metadata.karaoke_font;
  return KARAOKE_FONTS.some((font) => font.id === candidate)
    ? candidate as KaraokeFontId
    : DEFAULT_KARAOKE_FONT_ID;
}

export function karaokeFontFamily(fontId: KaraokeFontId): string {
  return KARAOKE_FONTS.find((font) => font.id === fontId)?.family ?? 'Karaoke Noto';
}

const KARAOKE_FONT_WEIGHTS: Record<KaraokeFontId, string> = {
  noto_sans: '100 900',
  be_vietnam_pro: '700',
  lexend: '100 900',
  barlow_condensed: '700',
  baloo_2: '400 800',
};

export function karaokeFontFaceCss(apiBase: string): string {
  const normalizedBase = apiBase.replace(/\/+$/, '');
  return KARAOKE_FONTS.map((font) => {
    const url = `${normalizedBase}/api/assets/karaoke-font/${font.id}`;
    return [
      '@font-face {',
      `font-family: ${JSON.stringify(font.family)};`,
      `src: url(${JSON.stringify(url)}) format('truetype');`,
      'font-display: swap;',
      'font-style: normal;',
      `font-weight: ${KARAOKE_FONT_WEIGHTS[font.id]};`,
      '}',
    ].join('');
  }).join('\n');
}
