import assert from 'node:assert/strict';
import test from 'node:test';

import {
  KARAOKE_COLORS,
  karaokeColorHex,
  karaokeColorId,
  karaokeColorLabel,
} from './karaoke-colors.ts';

test('old and invalid timelines safely use the yellow Karaoke sweep', () => {
  assert.equal(karaokeColorId({}), 'yellow');
  assert.equal(karaokeColorId({ karaoke_color: 'blue' }), 'yellow');
});

test('the three requested Karaoke sweep colors are selectable', () => {
  assert.deepEqual(KARAOKE_COLORS.map((color) => color.id), ['yellow', 'red', 'pink']);
  assert.equal(karaokeColorId({ karaoke_color: 'red' }), 'red');
  assert.equal(karaokeColorHex('pink'), '#ff4fa3');
  assert.equal(karaokeColorLabel('yellow'), 'Vàng');
});
