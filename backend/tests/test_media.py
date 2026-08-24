from __future__ import annotations

import json
import subprocess

import numpy as np
import soundfile as sf

from karaoke_studio import media


def test_probe_detects_vfr_rotation_and_independent_audio_duration(
    test_settings, tmp_path, monkeypatch
) -> None:
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"fixture")
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "duration": "2.000000",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "24000/1001",
                "r_frame_rate": "30/1",
                "side_data_list": [{"rotation": 90}],
            },
            {"codec_type": "audio", "duration": "1.990000"},
        ],
        "format": {"duration": "2.000000"},
    }
    completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(media, "run", lambda _command: completed)

    info = media.probe(source, test_settings)
    assert info.variable_frame_rate is True
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_duration_us == 2_000_000
    assert info.audio_duration_us == 1_990_000


def test_probe_uses_complete_audio_when_video_track_ends_early(
    test_settings, tmp_path, monkeypatch
) -> None:
    source = tmp_path / "truncated-video.mp4"
    source.write_bytes(b"fixture")
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "duration": "196.000000",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "24/1",
                "r_frame_rate": "24/1",
            },
            {"codec_type": "audio", "duration": "247.872000"},
        ],
        "format": {"duration": "247.872000"},
    }
    completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(media, "run", lambda _command: completed)

    info = media.probe(source, test_settings)

    assert info.video_duration_us == 196_000_000
    assert info.audio_duration_us == 247_872_000
    assert info.duration_us == 247_872_000


def test_safe_filename_drops_directories_and_unicode_punctuation() -> None:
    assert media.safe_filename("../../Bài hát (final).mp4", "input.mp4") == "B-i-h-t-final-.mp4"


def test_proxy_keeps_original_audio_for_singer_review(
    test_settings, synthetic_video, tmp_path
) -> None:
    proxy = tmp_path / "proxy.mp4"

    media.make_proxy(synthetic_video, proxy, test_settings)

    info = media.probe(proxy, test_settings)
    assert info.has_audio is True
    assert info.audio_duration_us is not None
    assert abs(info.audio_duration_us - info.duration_us) <= 50_000


def test_proxy_freezes_last_video_frame_until_longer_song_audio_ends(
    test_settings, tmp_path
) -> None:
    source = tmp_path / "short-video-long-audio.mp4"
    proxy = tmp_path / "proxy.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=24:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )

    media.make_proxy(source, proxy, test_settings)

    info = media.probe(proxy, test_settings)
    assert abs(info.duration_us - 4_000_000) <= 50_000
    assert abs(info.video_duration_us - info.duration_us) <= 50_000
    assert info.audio_duration_us is not None
    assert abs(info.audio_duration_us - info.duration_us) <= 50_000


def test_waveform_envelope_supports_demucs_float32_wav(tmp_path) -> None:
    source = tmp_path / "demucs-float.wav"
    time = np.linspace(0, 1, 48_000, endpoint=False, dtype=np.float32)
    stereo = np.column_stack(
        [np.sin(2 * np.pi * 220 * time), np.sin(2 * np.pi * 330 * time)]
    ).astype(np.float32)
    sf.write(source, stereo, 48_000, subtype="FLOAT")

    envelope = media.waveform_envelope(source, points=96)

    assert len(envelope) == 96
    assert max(envelope) == 1.0
    assert all(0.0 <= value <= 1.0 for value in envelope)
