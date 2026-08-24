# Third-party notices

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
