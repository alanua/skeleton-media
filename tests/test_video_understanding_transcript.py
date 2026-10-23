from __future__ import annotations

from pathlib import Path

import pytest

from skeleton_media.video_understanding.models import VideoUnderstandingError

from skeleton_media.video_understanding.transcript import TranscriptSegment, assess_quality, normalize_segments, parse_transcript


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


def test_non_monotonic_source_cues_fail_closed_without_silent_sorting(tmp_path: Path) -> None:
    cues = (
        TranscriptSegment(2, 3, "later first", "en", "synthetic"),
        TranscriptSegment(1, 1.5, "earlier second", "en", "synthetic"),
    )
    with pytest.raises(VideoUnderstandingError) as error:
        normalize_segments(cues)
    assert error.value.reason_code == "TRANSCRIPT_NOT_MONOTONIC"

    vtt = tmp_path / "synthetic.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:02.000 --> 00:00:03.000\nlater first\n\n"
        "00:00:01.000 --> 00:00:01.500\nearlier second\n",
        encoding="utf-8",
    )
    with pytest.raises(VideoUnderstandingError) as parsed_error:
        parse_transcript(vtt, language="en", provider="synthetic")
    assert parsed_error.value.reason_code == "TRANSCRIPT_NOT_MONOTONIC"


def test_overlapping_identical_words_from_different_sources_are_preserved() -> None:
    cues = (
        TranscriptSegment(0, 2, "same words", "en", "manual", provenance="manual"),
        TranscriptSegment(1, 3, "same words", "uk", "manual", provenance="manual"),
        TranscriptSegment(2, 4, "same words", "uk", "youtube_auto", provenance="subtitle"),
        TranscriptSegment(3, 5, "same words", "uk", "youtube_auto", provenance="local_asr"),
    )
    assert normalize_segments(cues) == cues


def test_identical_overlapping_same_source_cues_still_collapse() -> None:
    cues = (
        TranscriptSegment(0, 2, "same words", "uk", "youtube_auto", provenance="subtitle"),
        TranscriptSegment(1, 3, "same words", "uk", "youtube_auto", provenance="subtitle"),
    )
    result = normalize_segments(cues)
    assert len(result) == 1
    assert (result[0].start_seconds, result[0].end_seconds) == (0, 3)
