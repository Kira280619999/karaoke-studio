import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  KARAOKE_FONTS,
  karaokeFontFaceCss,
  karaokeFontFamily,
  karaokeFontId,
} from './karaoke-fonts.ts';

test('old and invalid timelines safely use the default Karaoke font', () => {
  assert.equal(karaokeFontId({}), 'noto_sans');
  assert.equal(karaokeFontId({ karaoke_font: 'không-tồn-tại' }), 'noto_sans');
});

test('all selectable fonts expose a unique browser family', () => {
  assert.equal(new Set(KARAOKE_FONTS.map((font) => font.id)).size, KARAOKE_FONTS.length);
  assert.equal(karaokeFontId({ karaoke_font: 'lexend' }), 'lexend');
  assert.equal(karaokeFontFamily('lexend'), 'Karaoke Lexend');
});

test('font faces follow the runtime-selected local API port', () => {
  const css = karaokeFontFaceCss('http://127.0.0.1:8123/');

  assert.match(css, /http:\/\/127\.0\.0\.1:8123\/api\/assets\/karaoke-font\/noto_sans/);
  assert.equal((css.match(/@font-face/g) ?? []).length, KARAOKE_FONTS.length);
  assert.doesNotMatch(css, /:8000/);
});

test('global styles do not retain a fixed API port for bundled fonts', () => {
  const globalCss = readFileSync(new URL('../globals.css', import.meta.url), 'utf8');

  assert.doesNotMatch(globalCss, /127\.0\.0\.1:8000\/api\/assets\/karaoke-font/);
});
