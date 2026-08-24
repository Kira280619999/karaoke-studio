# TimelineV1

`TimelineV1` is the canonical Karaoke timing format. All time values are integer microseconds; floating point values are used only for transient UI calculations and confidence scores.

```json
{
  "schema_version": "1.0",
  "revision": 4,
  "language": "vi",
  "duration_us": 245000000,
  "fps_numerator": 60,
  "fps_denominator": 1,
  "metadata": { "ar": "Ca sĩ", "ti": "Bài hát" },
  "lines": [
    {
      "id": "line-0001",
      "text": "Ngài là ánh sáng",
      "start_us": 1200000,
      "end_us": 4300000,
      "confidence": 0.86,
      "source": "energy_aware",
      "verified": false,
      "locked": false,
      "tokens": []
    }
  ]
}
```

## Invariants

- `0 <= line.start_us < line.end_us <= duration_us`.
- Lines are monotonic and may not overlap.
- Every line contains at least one token.
- Every token stays inside its line, has `start_us < end_us`, and does not overlap or reverse the next token.
- A manual edit changes `source` to `manual` and clears verification for the affected line/tokens.
- The export API never treats review count as a render lock. A Verified Final still requires explicit verification for low-confidence/manual cues and current-revision alignment reason codes; an earlier instrumental export is labeled unverified in its filename and QA report. Maximum Accuracy auto-accepts only diverse model/stem consensus with acoustic support.
- Save uses `expected_revision`; a concurrent/stale editor receives HTTP 409 instead of overwriting newer timing.

## Timing sources

- `lrc_line`: the LRC supplied a line anchor; word timing is only an initial distribution.
- `lrc_enhanced`: enhanced word/segment timestamps were supplied and preserved after invariant checks.
- `energy_aware`: timing inferred from local vocal energy within the line anchor.
- `vietnamese_ctc`: timing proposed by the pinned non-commercial Vietnamese acoustic model.
- `manual`: an operator adjusted the boundary.

`AlignmentEvidenceV1` is stored separately so candidate model/stem boundaries and diagnostics do not change this canonical schema. Ngoài model/stem, grapheme, beat và acoustic evidence, mỗi token lưu số lượt/correction của Automatic Sweep Critic. Các lỗi onset, sustain hoặc đường quét không hội tụ dùng `CRITIC_ONSET_MISMATCH`, `CRITIC_SUSTAIN_MISMATCH` và `CRITIC_NOT_CONVERGED`; chúng luôn fail-closed sang hàng “Cần nghe”.

`POST /api/projects/{id}/timing-suggestions` remains a read-only diagnostics API, but Manual Precision never calls it. Human review is deliberately deterministic: listen to the original singer, drag boundaries, nudge by 10 ms/frame, replay the complete line or either cross-line transition, save a revision and approve. The first-token start is clamped at the previous line end so a manual transition cannot create overlap. Approval clears any audition loop synchronously and resumes playback before the revision request runs, so browser playback activation is preserved.

The renderer maps each output frame index to integer microseconds. Its gold sweep moves continuously through both glyphs and whitespace and never moves backward inside a line.
