from __future__ import annotations

from karaoke_studio.separation import (
    AudioSeparatorAdapter,
    BsPolarformerAdapter,
    BsRoformerAdapter,
    DemucsAdapter,
    separator_request_signature,
)


def test_manifest_signature_changes_when_quality_engine_becomes_available(monkeypatch) -> None:
    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: False)
    monkeypatch.setattr(DemucsAdapter, "available", lambda _self: False)
    monkeypatch.setattr(BsPolarformerAdapter, "available", lambda _self: False)
    fallback = separator_request_signature("highest")
    assert fallback == {"quality": "highest", "adapters": ["center_cancel"]}

    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: True)
    monkeypatch.setattr(BsPolarformerAdapter, "available", lambda _self: True)
    upgraded = separator_request_signature("highest")
    assert upgraded == {
        "quality": "highest",
        "adapters": ["mel_band_roformer", "center_cancel", "bs_polarformer_fp32"],
    }
    assert upgraded != fallback


def test_fast_profile_keeps_original_fallback_and_adds_polarformer_for_export(monkeypatch) -> None:
    monkeypatch.setattr(AudioSeparatorAdapter, "available", lambda _self: True)
    monkeypatch.setattr(BsRoformerAdapter, "available", lambda _self: True)
    monkeypatch.setattr(BsPolarformerAdapter, "available", lambda _self: True)
    monkeypatch.setattr(DemucsAdapter, "available", lambda _self: True)

    assert separator_request_signature("fast") == {
        "quality": "fast",
        "adapters": ["center_cancel", "bs_polarformer_fp32"],
    }
