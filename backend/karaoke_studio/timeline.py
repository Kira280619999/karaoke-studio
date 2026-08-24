from __future__ import annotations

from .models import TimelineIssue, TimelineV1, TimingSource

LOW_CONFIDENCE = 0.78


def validate_timeline(timeline: TimelineV1, require_verified: bool = False) -> list[TimelineIssue]:
    issues: list[TimelineIssue] = []
    previous_line_start = -1
    for line in timeline.lines:
        if line.start_us < previous_line_start:
            issues.append(
                TimelineIssue(code="LINE_ORDER", message="Dòng lời bị đảo thứ tự.", line_id=line.id)
            )
        previous_line_start = line.start_us
        if line.end_us <= line.start_us:
            issues.append(
                TimelineIssue(
                    code="LINE_DURATION",
                    message="Dòng lời có duration không hợp lệ.",
                    line_id=line.id,
                )
            )
        if line.end_us > timeline.duration_us:
            issues.append(
                TimelineIssue(
                    code="LINE_OUT_OF_RANGE", message="Dòng lời vượt quá video.", line_id=line.id
                )
            )
        previous_token_end = line.start_us
        previous_sweep_progress = 0
        for token in line.tokens:
            if token.start_us < line.start_us or token.end_us > line.end_us:
                issues.append(
                    TimelineIssue(
                        code="TOKEN_OUT_OF_LINE",
                        message="Âm tiết nằm ngoài dòng.",
                        line_id=line.id,
                        token_id=token.id,
                    )
                )
            if token.start_us < previous_token_end:
                issues.append(
                    TimelineIssue(
                        code="TOKEN_OVERLAP",
                        message="Âm tiết bị overlap hoặc đảo thứ tự.",
                        line_id=line.id,
                        token_id=token.id,
                    )
                )
            if token.end_us <= token.start_us:
                issues.append(
                    TimelineIssue(
                        code="TOKEN_DURATION",
                        message="Âm tiết có duration không hợp lệ.",
                        line_id=line.id,
                        token_id=token.id,
                    )
                )
            if token.end_us - token.start_us < frame_duration_us(timeline):
                issues.append(
                    TimelineIssue(
                        code="TOKEN_UNDER_ONE_FRAME",
                        message="Âm tiết ngắn hơn một frame.",
                        line_id=line.id,
                        token_id=token.id,
                        severity="warning",
                    )
                )
            sweep = token.sweep
            if sweep is None:
                issues.append(
                    TimelineIssue(
                        code="SWEEP_MISSING",
                        message=(
                            "Chuyển động Karaoke chưa được phân tích; phải chạy lại hoặc duyệt trước Final."
                            if require_verified
                            else "Chuyển động Karaoke chưa được phân tích."
                        ),
                        line_id=line.id,
                        token_id=token.id,
                        severity="error" if require_verified else "warning",
                    )
                )
            else:
                points = sweep.points
                invalid_bounds = (
                    points[0].time_us != token.start_us
                    or points[-1].time_us != token.end_us
                )
                invalid_order = any(
                    current.time_us <= previous.time_us
                    or current.line_progress_ppm < previous.line_progress_ppm
                    for previous, current in zip(points, points[1:], strict=False)
                )
                if invalid_bounds or invalid_order:
                    issues.append(
                        TimelineIssue(
                            code="SWEEP_INVALID",
                            message="Đường quét có mốc thời gian hoặc tiến độ không hợp lệ.",
                            line_id=line.id,
                            token_id=token.id,
                        )
                    )
                if points[0].line_progress_ppm != previous_sweep_progress:
                    issues.append(
                        TimelineIssue(
                            code="SWEEP_DISCONTINUITY",
                            message="Đường quét bị nhảy hoặc chạy lùi tại ranh giới âm tiết.",
                            line_id=line.id,
                            token_id=token.id,
                        )
                    )
                previous_sweep_progress = points[-1].line_progress_ppm
                if not sweep.verified:
                    issues.append(
                        TimelineIssue(
                            code="SWEEP_UNVERIFIED",
                            message=(
                                "Chuyển động quét phải được nghe và xác nhận trước Final."
                                if require_verified
                                else "Chuyển động quét cần được nghe hoặc đạt đồng thuận AI."
                            ),
                            line_id=line.id,
                            token_id=token.id,
                            severity="error" if require_verified else "warning",
                        )
                    )
            if token.source == TimingSource.MANUAL and not token.verified:
                issues.append(
                    TimelineIssue(
                        code="MANUAL_REVIEW",
                        message=(
                            "Timing vừa sửa tay phải được nghe và xác nhận trước Final."
                            if require_verified
                            else "Timing vừa sửa tay cần được nghe và xác nhận."
                        ),
                        line_id=line.id,
                        token_id=token.id,
                        severity="error" if require_verified else "warning",
                    )
                )
            elif token.confidence < LOW_CONFIDENCE and not token.verified:
                issues.append(
                    TimelineIssue(
                        code="LOW_CONFIDENCE",
                        message=(
                            "Timing confidence thấp phải được nghe và xác nhận trước Final."
                            if require_verified
                            else "Timing confidence thấp cần được nghe và xác nhận."
                        ),
                        line_id=line.id,
                        token_id=token.id,
                        severity="error" if require_verified else "warning",
                    )
                )
            previous_token_end = token.end_us
        if (
            line.tokens
            and all(token.sweep is not None for token in line.tokens)
            and previous_sweep_progress != 1_000_000
        ):
            issues.append(
                TimelineIssue(
                    code="SWEEP_INCOMPLETE",
                    message="Đường quét không đi hết toàn bộ dòng lời.",
                    line_id=line.id,
                    token_id=line.tokens[-1].id,
                )
            )
    return issues


def frame_duration_us(timeline: TimelineV1) -> int:
    return round(1_000_000 * timeline.fps_denominator / timeline.fps_numerator)


def snap_to_frame(value_us: int, timeline: TimelineV1) -> int:
    frame = frame_duration_us(timeline)
    return max(0, round(value_us / frame) * frame)
