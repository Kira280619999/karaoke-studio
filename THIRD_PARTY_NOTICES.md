# Third-party notices

## BS PolarFormer FP32 ONNX

Karaoke Studio downloads the FP32 `bs_polarformer.onnx` conversion from
[`bgkb/bs_polarformer`](https://huggingface.co/bgkb/bs_polarformer) at the
pinned revision recorded in source. The repository declares the conversion
and model card under the MIT License and credits the original
[`ZFTurbo/Music-Source-Separation-Training`](https://github.com/ZFTurbo/Music-Source-Separation-Training)
implementation. The weight is SHA-256 verified, stored only in the user's local
data directory and is not redistributed in this repository or GitHub Release.

## Audio Separator and fallback stem models

Karaoke Studio invokes [Audio Separator](https://github.com/nomadkaraoke/python-audio-separator),
which is licensed under the MIT License, to run Mel-Band RoFormer for analysis
and BS-RoFormer ViperX 1297 as a fallback when PolarFormer cannot run locally.

The ViperX checkpoint `model_bs_roformer_ep_317_sdr_12.9755.ckpt` and its
configuration are downloaded by Audio Separator on first use. Karaoke Studio
records their hashes for reproducibility but does not redistribute those model
files in this repository or its GitHub Release. Model authors and upstream
download hosts retain their own terms; users must review those terms for their
intended use.

## Vietnamese lyric-alignment model adapter

`backend/karaoke_studio/lyric_model.py` implements the checkpoint-compatible
Wav2Vec2 feature-transform architecture described by Nguyen Vu Le Binh's
[`lyric-alignment`](https://github.com/nguyenvulebinh/lyric-alignment) project.
The upstream source repository is licensed under Apache License 2.0.

The separately downloaded model weights at
[`nguyenvulebinh/lyric-alignment`](https://huggingface.co/nguyenvulebinh/lyric-alignment)
are licensed CC BY-NC 4.0 and are not distributed with Karaoke Studio.

## Bundled Karaoke fonts

Karaoke Studio redistributes the following unmodified font binaries under the
SIL Open Font License 1.1. The corresponding complete license text is stored
next to each binary in `backend/karaoke_studio/assets/`.

- Noto Sans — `NotoSans-Variable.ttf`, license `OFL-NotoSans.txt`.
- Be Vietnam Pro — `BeVietnamPro-Bold.ttf`, license `OFL-BeVietnamPro.txt`.
- Lexend — `Lexend-Variable.ttf`, license `OFL-Lexend.txt`.
- Barlow Condensed — `BarlowCondensed-Bold.ttf`, license `OFL-BarlowCondensed.txt`.
- Baloo 2 — `Baloo2-Variable.ttf`, license `OFL-Baloo2.txt`.

The application uses the original family names and does not modify the font
binaries. Generated Karaoke videos are documents produced with the fonts; they
do not contain or redistribute standalone font files.
