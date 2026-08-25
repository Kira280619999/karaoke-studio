from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

import karaoke_studio.final_audio as final_audio
from karaoke_studio.db import Store, now_iso
from karaoke_studio.media import sha256_file
from karaoke_studio.models import ProjectRecord, ProjectState
from karaoke_studio.separation import StemCandidate, load_candidates


def _stereo_tone(frames: int, sample_rate: int, amplitude: float = 0.3) -> np.ndarray:
    time = np.arange(frames, dtype=np.float32) / sample_rate
    mono = amplitude * np.sin(2 * np.pi * 440 * time)
    return np.column_stack((mono, mono)).astype(np.float32)


def test_final_audio_generation_is_role_isolated_and_keeps_timing_bytes(
    monkeypatch, test_settings
) -> None:
    store = Store(test_settings)
    project_id = "proj_final_audio"
    project_dir = store.project_dir(project_id)
    work = project_dir / "work"
    stems = work / "stems"
    base_dir = stems / "mel_band_roformer"
    base_dir.mkdir(parents=True)
    mix = work / "mix.wav"
    sf.write(mix, _stereo_tone(48_000, 48_000), 48_000, subtype="PCM_24")
    sf.write(base_dir / "instrumental.wav", _stereo_tone(48_000, 48_000), 48_000)
    sf.write(base_dir / "vocals.wav", _stereo_tone(48_000, 48_000, 0.1), 48_000)
    stems.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "request": {"quality": "highest", "adapters": ["mel_band_roformer"]},
                "candidates": [
                    {
                        "id": "mel_band_roformer",
                        "label": "Mel-Band RoFormer",
                        "engine": "audio-separator",
                        "instrumental": "work/stems/mel_band_roformer/instrumental.wav",
                        "vocals": "work/stems/mel_band_roformer/vocals.wav",
                        "production_grade": True,
                    }
                ],
                "models": {},
                "failures": [],
            }
        ),
        encoding="utf-8",
    )
    work.joinpath("waveforms.json").write_text(
        json.dumps({"mix": [0.1], "candidates": {}}), encoding="utf-8"
    )
    protected_payloads = {
        "timeline.json": '{"timeline":"must-stay-identical"}',
        "work/alignment-manifest.json": '{"alignment":"manifest"}',
        "work/alignment-evidence.json": '{"alignment":"evidence"}',
        "work/alignment-report.json": '{"alignment":"report"}',
    }
    for relative, payload in protected_payloads.items():
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    protected_hashes = {
        relative: sha256_file(project_dir / relative) for relative in protected_payloads
    }
    store.add_project(
        ProjectRecord(
            id=project_id,
            title="Audio Final",
            artist="",
            state=ProjectState.NEEDS_REVIEW,
            created_at=now_iso(),
            updated_at=now_iso(),
            source_name="source.mp4",
            lrc_name="lyrics.lrc",
            source_sha256="source",
            duration_us=1_000_000,
            width=1920,
            height=1080,
            fps="30/1",
            has_audio=True,
        )
    )

    def fake_profile(profile, _input, _mix, _mix_hash, project_root, _settings):
        output_dir = project_root / "work" / "stems" / profile.id
        output_dir.mkdir(parents=True, exist_ok=True)
        instrumental = output_dir / "instrumental.wav"
        vocals = output_dir / "vocals.wav"
        sf.write(instrumental, _stereo_tone(48_000, 48_000), 48_000, subtype="PCM_24")
        sf.write(vocals, _stereo_tone(48_000, 48_000, 0.1), 48_000, subtype="PCM_24")
        return StemCandidate(
            id=profile.id,
            label=profile.label,
            engine=profile.preset,
            instrumental=str(instrumental),
            vocals=str(vocals),
            production_grade=True,
            quality_profile=profile.quality_profile,
            analysis_eligible=False,
            export_eligible=True,
            pcm_sha256=sha256_file(instrumental),
            signal_path="lossless-test",
            audio_qa={
                "sample_rate": 48000,
                "channels": 2,
                "frames": 48000,
                "peak": 0.3,
                "gain_db": 0.0,
                "status": "PASS",
            },
        )

    monkeypatch.setattr(final_audio, "_prepare_profile", fake_profile)
    events: list[str] = []
    generated = final_audio.prepare_final_audio_project(
        "job_test", project_id, test_settings, lambda _progress, message: events.append(message)
    )

    assert [candidate.id for candidate in generated] == [
        "final_full",
        "final_balanced",
        "final_clean",
    ]
    all_candidates = load_candidates(project_dir)
    assert [candidate.id for candidate in all_candidates if candidate.analysis_eligible] == [
        "mel_band_roformer"
    ]
    assert all(not candidate.analysis_eligible for candidate in all_candidates[1:])
    assert all(candidate.export_eligible for candidate in all_candidates)
    assert {
        relative: sha256_file(project_dir / relative) for relative in protected_payloads
    } == protected_hashes
    waveform = json.loads(work.joinpath("waveforms.json").read_text(encoding="utf-8"))
    assert waveform["candidates"]["final_balanced"]["audio_qa"]["status"] == "PASS"
    assert any("timing được giữ nguyên" in message for message in events)


def test_pcm_master_conversion_uses_only_fixed_gain_when_peak_would_clip(
    tmp_path: Path, test_settings
) -> None:
    source = tmp_path / "float.wav"
    output = tmp_path / "master.wav"
    sf.write(source, _stereo_tone(44_100, 44_100, 1.2), 44_100, subtype="FLOAT")

    report = final_audio._convert_to_master_pcm(
        source, output, 48_000, 48_000, test_settings
    )

    assert report["status"] == "PASS"
    assert report["sample_rate"] == 48_000
    assert report["frames"] == 48_000
    assert report["subtype"] == "PCM_24"
    assert report["gain_db"] < 0
    assert report["limiter"] is False
    assert report["loudness_normalization"] is False
    assert report["peak"] <= 1.0
