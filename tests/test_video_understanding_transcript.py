from __future__ import annotations

from pathlib import Path

from core.video_understanding.transcript import TranscriptSegment, assess_quality, normalize_segments, parse_transcript


def test_vtt_and_srt_normalize_with_monotonic_timestamps(tmp_path: Path) -> None:
    vtt = tmp_path / "x.vtt"
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello\n\n00:00:01.900 --> 00:00:03.000\nHello\n")
    segments = parse_transcript(vtt, language="en", provider="subtitle")
    assert len(segments) == 1
    assert segments[0].start_seconds == 0
    assert segments[0].end_seconds == 3


def test_repeated_speech_at_different_times_is_preserved() -> None:
    values = (
        TranscriptSegment(0, 1, "repeat", "en", "test", 1.0, "synthetic"),
        TranscriptSegment(10, 11, "repeat", "en", "test", 1.0, "synthetic"),
    )
    assert len(normalize_segments(values)) == 2


def test_quality_assessment_distinguishes_usable_and_low() -> None:
    good = (
        TranscriptSegment(0, 20, "a" * 100, "en", "test", .9, "synthetic"),
        TranscriptSegment(20, 40, "b" * 100, "en", "test", .9, "synthetic"),
        TranscriptSegment(40, 60, "c" * 100, "en", "test", .9, "synthetic"),
    )
    assert assess_quality(good, media_duration_seconds=60, max_chars=1000).usable is True
    assert assess_quality((), media_duration_seconds=60, max_chars=1000).usable is False
