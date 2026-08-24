export type ExportPreset = '1080p120' | '1080p60' | '1080p30' | 'source';

export const DEFAULT_EXPORT_PRESET: ExportPreset = '1080p60';

export const EXPORT_PRESET_OPTIONS: ReadonlyArray<{
  value: ExportPreset;
  label: string;
}> = [
  { value: '1080p60', label: '1080p · 60fps (khuyên dùng)' },
  { value: '1080p120', label: '1080p · 120fps (cần player hỗ trợ)' },
  { value: '1080p30', label: '1080p · 30fps' },
  { value: 'source', label: 'Theo video gốc' },
];

export function exportPresetLabel(preset: ExportPreset): string {
  return EXPORT_PRESET_OPTIONS.find((option) => option.value === preset)?.label ?? preset;
}
