import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SONG_CREDIT_DURATION_US,
  songCreditOpacity,
} from './song-credit.ts';

test('song title and artist disappear completely after five seconds', () => {
  assert.equal(songCreditOpacity(0), 1);
  assert.equal(songCreditOpacity(4_250_000), 1);
  assert.ok(songCreditOpacity(4_750_000) > 0);
  assert.ok(songCreditOpacity(4_750_000) < 1);
  assert.equal(songCreditOpacity(SONG_CREDIT_DURATION_US), 0);
  assert.equal(songCreditOpacity(8_000_000), 0);
});
