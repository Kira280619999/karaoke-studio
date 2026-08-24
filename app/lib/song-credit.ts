export const SONG_CREDIT_DURATION_US = 5_000_000;
export const SONG_CREDIT_FADE_OUT_US = 750_000;

export function songCreditOpacity(nowUs: number): number {
  if (!Number.isFinite(nowUs) || nowUs < 0 || nowUs >= SONG_CREDIT_DURATION_US) return 0;
  const fadeStartUs = SONG_CREDIT_DURATION_US - SONG_CREDIT_FADE_OUT_US;
  if (nowUs <= fadeStartUs) return 1;
  return (SONG_CREDIT_DURATION_US - nowUs) / SONG_CREDIT_FADE_OUT_US;
}
