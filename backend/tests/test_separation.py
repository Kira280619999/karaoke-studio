from __future__ import annotations

import json
from pathlib import Path

import karaoke_studio.separation as separation
from karaoke_studio.separation import (
    AudioSeparatorAdapter,
    BsRoformerAdapter,
    DemucsAdapter,
    StemCandidate,
    separate_candidates,
    separator_request_signature,
)


def test_manifest_signature_changes_when_quality_engine_becomes_available(monkeypatch) -> None:
    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: False)
    monkeypatch.setattr(DemucsAdapter, "available", lambda _self: False)
    fallback = separator_request_signature("highest")
    assert fallback == {"quality": "highest", "adapters": ["center_cancel"]}

    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: True)
    upgraded = separator_request_signature("highest")
    assert upgraded == {
        "quality": "highest",
        "adapters": ["mel_band_roformer", "bs_roformer_viperx_1297"],
    }
    assert upgraded != fallback


def test_bs_roformer_is_a_secondary_maximum_accuracy_candidate(monkeypatch) -> None:
    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: True)
    monkeypatch.setattr(DemucsAdapter, "available", lambda _self: True)

    assert separator_request_signature("highest") == {
        "quality": "highest",
        "adapters": [
            "mel_band_roformer",
            "bs_roformer_viperx_1297",
            "htdemucs_ft",
        ],
    }
    assert separator_request_signature("balanced") == {
        "quality": "balanced",
        "adapters": ["mel_band_roformer"],
    }
    assert BsRoformerAdapter.model == "model_bs_roformer_ep_317_sdr_12.9755.ckpt"


def test_new_adapter_reuses_existing_stems_and_only_runs_missing_models(
    monkeypatch, tmp_path: Path, test_settings
) -> None:
    project_dir = tmp_path / "project"
    stem_dir = project_dir / "work" / "stems" / "mel_band_roformer"
    stem_dir.mkdir(parents=True)
    instrumental = stem_dir / "instrumental.wav"
    vocals = stem_dir / "vocals.wav"
    instrumental.write_bytes(b"instrumental")
    vocals.write_bytes(b"vocals")
    manifest = project_dir / "work" / "stems" / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "request": {"quality": "highest", "adapters": ["mel_band_roformer"]},
                "candidates": [
                    {
                        "id": "mel_band_roformer",
                        "label": "Mel-Band RoFormer",
                        "engine": "mel_band_roformer",
                        "instrumental": "work/stems/mel_band_roformer/instrumental.wav",
                        "vocals": "work/stems/mel_band_roformer/vocals.wav",
                        "production_grade": True,
                        "warning": None,
                        "alignment_eligible": True,
                        "kind": "legacy-separator-metadata",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class ExistingAdapter:
        id = "mel_band_roformer"
        label = "Mel-Band RoFormer"

        def separate(self, *_args, **_kwargs) -> StemCandidate:
            raise AssertionError("existing stem must not be recomputed")

    monkeypatch.setattr(separation, "selected_adapters", lambda _quality: [ExistingAdapter()])
    result = separate_candidates(
        tmp_path / "mix.wav", project_dir, test_settings, "highest", lambda *_args: None
    )

    assert [candidate.id for candidate in result] == ["mel_band_roformer"]
    assert Path(result[0].instrumental).read_bytes() == b"instrumental"
