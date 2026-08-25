import assert from 'node:assert/strict';
import test from 'node:test';

import {
  preferredPreviewAudioMode,
  previewAudioNeedsSync,
  previewWaveformFor,
  selectedInstrumentalArtifact,
} from './preview-audio.ts';
import type { Artifact } from './types.ts';

const artifacts: Artifact[] = [
  {
    id: 'work/stems/center_cancel/vocals.wav',
    label: 'vocals.wav',
    kind: 'stem',
    url: '/vocals',
    bytes: 12,
  },
  {
    id: 'work/stems/center_cancel/instrumental.wav',
    label: 'instrumental.wav',
    kind: 'stem',
    url: '/instrumental',
    bytes: 34,
  },
  {
    id: 'exports/instrumental.wav',
    label: 'instrumental.wav',
    kind: 'export',
    url: '/wrong-kind',
    bytes: 56,
  },
];

test('selected instrumental preview resolves the exact selected stem', () => {
  assert.equal(
    selectedInstrumentalArtifact('center_cancel', artifacts)?.url,
    '/instrumental',
  );
  assert.equal(selectedInstrumentalArtifact('missing', artifacts), null);
  assert.equal(selectedInstrumentalArtifact(null, artifacts), null);
});

test('viewer always defaults to the original mix', () => {
  assert.equal(preferredPreviewAudioMode(), 'original');
});

test('waveform follows the audio source that the viewer is actually playing', () => {
  const waveform = {
    mix: [0.1, 0.2],
    candidates: {
      center_cancel: {
        label: 'Center Cancel',
        production_grade: false,
        warning: null,
        instrumental: [0.3, 0.4],
        vocals: [0.5, 0.6],
      },
    },
  };
  assert.deepEqual(previewWaveformFor('instrumental', 'center_cancel', waveform), [0.3, 0.4]);
  assert.deepEqual(previewWaveformFor('original', 'center_cancel', waveform), [0.1, 0.2]);
  assert.deepEqual(previewWaveformFor('instrumental', 'missing', waveform), [0.1, 0.2]);
});

test('instrumental is resynchronized only after meaningful playback drift', () => {
  assert.equal(previewAudioNeedsSync(12, 12.019), false);
  assert.equal(previewAudioNeedsSync(12, 12.021), true);
  assert.equal(previewAudioNeedsSync(12, Number.NaN), true);
  assert.equal(previewAudioNeedsSync(Number.NaN, 12), false);
});
