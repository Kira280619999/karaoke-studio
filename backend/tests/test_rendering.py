from __future__ import annotations

from karaoke_studio.lrc import parse_lrc
from karaoke_studio.models import SweepCurveV1, SweepPointV1
from karaoke_studio.motion import evaluate_sweep_ppm
from karaoke_studio.rendering import KaraokeRenderer, RenderPreset


def test_continuous_highlight_never_moves_backward(test_settings) -> None:
    timeline = parse_lrc("[00:00.00]Ngài là ánh sáng", duration_us=3_000_000)
    renderer = KaraokeRenderer(timeline, RenderPreset(1280, 720, 60, 1), test_settings, True, True)
    line = timeline.lines[0]
    left = renderer.assets[(0, True)].left
    boundaries = [
        renderer._highlight_boundary(line, left, timestamp)
        for timestamp in range(0, 3_000_000, 16_667)
    ]
    assert boundaries == sorted(boundaries)
    assert boundaries[-1] > boundaries[0]


def test_highlight_sweeps_original_multiple_spaces(test_settings) -> None:
    timeline = parse_lrc("[00:00.00]Ngài   là ánh sáng", duration_us=3_000_000)
    renderer = KaraokeRenderer(timeline, RenderPreset(1280, 720, 60, 1), test_settings, True, True)
    line = timeline.lines[0]
    asset = renderer.assets[(0, True)]
    left = asset.left
    samples = [
        renderer._highlight_boundary(line, left, timestamp)
        for timestamp in range(0, 3_000_000, 16_667)
    ]
    assert samples == sorted(samples)
    assert samples[-1] > left + asset.font.getlength("Ngài") * asset.scale_x


def test_preview_frame_has_native_dimensions(test_settings) -> None:
    timeline = parse_lrc("[00:00.00]Ngài là ánh sáng", duration_us=2_000_000)
    renderer = KaraokeRenderer(timeline, RenderPreset(1920, 1080, 60, 1), test_settings, True, True)
    assert renderer.frame(1_000_000).size == (1920, 360)


def test_long_vietnamese_line_is_fitted_and_draft_mark_is_visible(test_settings) -> None:
    timeline = parse_lrc(
        "[00:00.00]Nguyện tình yêu thương soi sáng mọi con đường chúng ta đang bước",
        duration_us=3_000_000,
    )
    renderer = KaraokeRenderer(timeline, RenderPreset(1920, 1080, 60, 1), test_settings, True, True)
    asset = renderer.assets[(0, True)]
    assert asset.text_width <= 1920 * 0.88
    image = renderer.frame(1_500_000)
    assert image.getbbox() is not None


def test_each_fixed_lane_fits_its_own_text_without_wrapping(test_settings) -> None:
    timeline = parse_lrc(
        "[00:00.00]Ngắn\n"
        "[00:02.00]Lửa chiếu sáng mắt Ngài. Huyết của Chúa chữa lành "
        "và Ngài tỏ bày ra vinh quang diệu kì",
        duration_us=5_000_000,
    )
    renderer = KaraokeRenderer(timeline, RenderPreset(1280, 720, 60, 1), test_settings, True, True)
    short_asset = renderer.assets[(0, True)]
    long_asset = renderer.assets[(1, True)]
    max_width = 1280 * 0.88
    assert short_asset.text_width <= max_width
    assert long_asset.text_width <= max_width
    assert long_asset.font.size == short_asset.font.size
    assert short_asset.scale_x == 1
    assert long_asset.scale_x < 1


def test_renderer_uses_exact_same_integer_sweep_progress(test_settings) -> None:
    timeline = parse_lrc("[00:01.00]Ngân", duration_us=3_000_000)
    token = timeline.lines[0].tokens[0]
    token.sweep = SweepCurveV1(
        source="ensemble_ctc",
        confidence=0.96,
        verified=True,
        points=[
            SweepPointV1(time_us=token.start_us, line_progress_ppm=0),
            SweepPointV1(time_us=1_250_000, line_progress_ppm=300_000),
            SweepPointV1(time_us=token.end_us, line_progress_ppm=1_000_000),
        ],
    )
    renderer = KaraokeRenderer(
        timeline, RenderPreset(1280, 720, 60, 1), test_settings, True, True
    )
    line = timeline.lines[0]
    asset = renderer.assets[(0, True)]
    left = asset.left
    now_us = 1_700_000
    boundary = renderer._highlight_boundary(line, left, now_us)
    expected = left + asset.font.getlength(line.text) * asset.scale_x * (
        evaluate_sweep_ppm(token.sweep, now_us) / 1_000_000
    )
    assert abs(boundary - expected) < 1e-9


def test_renderer_uses_timeline_font_without_changing_fixed_size(test_settings) -> None:
    timeline = parse_lrc(
        "[00:00.00]Xin Đức Thánh Linh đưa dắt chúng con vững tin nơi Ngài\n"
        "[00:03.00]Nguyện Chúa nắm tay bước qua hiểm nguy",
        duration_us=6_000_000,
    )
    timeline.metadata["karaoke_font"] = "be_vietnam_pro"
    renderer = KaraokeRenderer(
        timeline, RenderPreset(1920, 1080, 60, 1), test_settings, True, True
    )

    assert renderer.font_path.name == "BeVietnamPro-Bold.ttf"
    assert {asset.font.size for asset in renderer.assets.values()} == {63}
