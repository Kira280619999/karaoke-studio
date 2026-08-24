from __future__ import annotations

import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor

from karaoke_studio.media import probe
from karaoke_studio.quicktime import (
    FULL_FRAME_RATE_PLAYBACK_INTENT_KEY,
    _adjust_chunk_offsets,
    inject_full_frame_rate_playback_intent,
    read_full_frame_rate_playback_intent,
)


def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def test_typed_hfr_intent_preserves_faststart_mp4_timing(
    synthetic_video,
    test_settings,
    tmp_path,
) -> None:
    output = tmp_path / "faststart-120.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(synthetic_video),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    before = probe(output, test_settings)
    before_size = output.stat().st_size

    inject_full_frame_rate_playback_intent(output)

    after = probe(output, test_settings)
    assert read_full_frame_rate_playback_intent(output) == 1
    assert output.stat().st_size == before_size + 150
    assert after.duration_us == before.duration_us
    assert after.audio_duration_us == before.audio_duration_us
    assert after.video_frames == before.video_frames
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
        check=True,
    )

    snapshot = tmp_path / "snapshot.mp4"
    shutil.copy2(output, snapshot)
    inject_full_frame_rate_playback_intent(output)
    assert output.read_bytes() == snapshot.read_bytes()


def test_hfr_intent_reader_requires_mdta_handler(
    synthetic_video,
    tmp_path,
) -> None:
    output = tmp_path / "wrong-handler.mp4"
    shutil.copy2(synthetic_video, output)
    inject_full_frame_rate_playback_intent(output)
    data = bytearray(output.read_bytes())
    key_position = data.index(FULL_FRAME_RATE_PLAYBACK_INTENT_KEY)
    key_namespace_position = key_position - 4
    handler_position = data.rfind(b"mdta", 0, key_namespace_position)
    assert handler_position >= 0
    data[handler_position : handler_position + 4] = b"mdir"
    output.write_bytes(data)

    assert read_full_frame_rate_playback_intent(output) is None


def test_chunk_offsets_shift_only_after_inserted_moov() -> None:
    stco = _atom(
        b"stco",
        b"\0\0\0\0" + struct.pack(">I", 2) + struct.pack(">II", 100, 1_000),
    )
    moov = bytearray(_atom(b"moov", _atom(b"trak", _atom(b"stbl", stco))))

    _adjust_chunk_offsets(moov, 8, len(moov), 150, 500)

    stco_position = moov.index(b"stco") - 4
    first_offset = stco_position + 16
    assert struct.unpack_from(">II", moov, first_offset) == (100, 1_150)


def test_concurrent_hfr_intent_injection_uses_isolated_temp_files(
    synthetic_video,
    tmp_path,
) -> None:
    output = tmp_path / "concurrent.mp4"
    shutil.copy2(synthetic_video, output)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(inject_full_frame_rate_playback_intent, output)
            for _ in range(2)
        ]
        for future in futures:
            future.result()

    assert read_full_frame_rate_playback_intent(output) == 1
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
        check=True,
    )
