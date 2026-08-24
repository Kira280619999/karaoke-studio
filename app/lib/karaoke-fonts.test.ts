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

test('Karaoke lyrics keep the font natural spacing instead of squeezing spaces', () => {
  const globalCss = readFileSync(new URL('../globals.css', import.meta.url), 'utf8');
  const rule = globalCss.match(/\.preview-lyric \{[^}]+\}/)?.[0] ?? '';

  assert.match(rule, /letter-spacing:\s*0/);
  assert.match(rule, /word-spacing:\s*0/);
  assert.match(rule, /align-items:\s*unsafe center/);
  assert.doesNotMatch(rule, /letter-spacing:\s*-/);
});

test('a horizontally fitted Karaoke row remains anchored to the lane center', () => {
  const globalCss = readFileSync(new URL('../globals.css', import.meta.url), 'utf8');
  const rule = globalCss.match(/\.lyric-stack-row \{[^}]+\}/)?.[0] ?? '';

  assert.match(rule, /width:\s*100%/);
  const textRule = globalCss.match(/\.lyric-stack-row > span, \.lyric-stack-row > b \{[^}]+\}/)?.[0] ?? '';
  assert.match(textRule, /left:\s*50%/);
  assert.match(textRule, /translate:\s*-50% 0/);
  assert.match(textRule, /scale:\s*var\(--lyric-fit-x\) 1/);
});

test('wrapped Karaoke rows keep balanced font-relative vertical spacing', () => {
  const globalCss = readFileSync(new URL('../globals.css', import.meta.url), 'utf8');
  const upperLane = globalCss.match(/\.preview-lyric\.lane-0 \{[^}]+\}/)?.[0] ?? '';
  const lowerLane = globalCss.match(/\.preview-lyric\.lane-1 \{[^}]+\}/)?.[0] ?? '';
  const stack = globalCss.match(/\.lyric-stack \{[^}]+\}/)?.[0] ?? '';
  const multiline = globalCss.match(/\.lyric-stack\.is-multiline \{[^}]+\}/)?.[0] ?? '';

  assert.match(upperLane, /bottom:\s*calc\(6% \+ 1\.53em\)/);
  assert.match(lowerLane, /bottom:\s*calc\(6% - \.06em\)/);
  assert.match(stack, /position:\s*absolute/);
  assert.match(stack, /top:\s*50%/);
  assert.match(stack, /translate:\s*0 -50%/);
  assert.match(multiline, /row-gap:\s*\.02em/);
  assert.match(multiline, /font-size:\s*1em/);
});
