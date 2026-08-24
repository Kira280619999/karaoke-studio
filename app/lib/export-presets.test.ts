import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_EXPORT_PRESET,
  EXPORT_PRESET_OPTIONS,
  exportPresetLabel,
} from './export-presets.ts';

test('1080p60 is the compatible default while 120fps remains available', () => {
  assert.equal(DEFAULT_EXPORT_PRESET, '1080p60');
  assert.deepEqual(
    EXPORT_PRESET_OPTIONS.map((option) => option.value),
    ['1080p60', '1080p120', '1080p30', 'source'],
  );
  assert.match(exportPresetLabel('1080p120'), /CFR/);
});
