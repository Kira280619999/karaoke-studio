from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from karaoke_studio.alignment import suggest_line_timing
from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import TimingSource


def test_energy_suggestion_is_line_scoped_and_monotonic(
    test_settings, tmp_path: Path
) -> None:
    sample_rate = 16_000
    audio = np.zeros(sample_rate * 2, dtype=np.float32)
    audio[3_200:28_800] = 0.3 * np.sin(
        2 * np.pi * 220 * np.arange(25_600) / sample_rate
    )
    vocal = tmp_path / "vocal.wav"
    sf.write(vocal, audio, sample_rate)
    line = parse_lrc("[00:00.00]Thiên nhiên vâng lời\n", 2_000_000).lines[0]

    suggestion = suggest_line_timing(
        line,
        [("fixture", vocal)],
        accept_noncommercial_license=False,
        settings=test_settings,
    )

    assert suggestion.line_id == line.id
    assert suggestion.source == TimingSource.ENERGY
    assert suggestion.used_vocal_stems == ["fixture"]
    assert suggestion.license_required_for_ctc is True
    assert [token.token_id for token in suggestion.tokens] == [
        token.id for token in line.tokens
    ]
    assert all(
        previous.end_us <= current.start_us
        for previous, current in zip(
            suggestion.tokens, suggestion.tokens[1:], strict=False
        )
    )
    assert all(line.start_us <= token.start_us < token.end_us <= line.end_us for token in suggestion.tokens)
