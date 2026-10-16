from __future__ import annotations

from skeleton_media.video_understanding.transcript import (
    TranscriptSegment,
    assess_quality,
    compact_rolling_captions,
    transcript_to_jsonl,
)


def cue(start: float, end: float, text: str, lang: str = "uk", provider: str = "youtube_auto") -> TranscriptSegment:
    return TranscriptSegment(start, end, text, lang, provider, None, "synthetic_auto_caption")


def test_rolling_auto_cues_become_readable_without_rewriting_the_source() -> None:
    source = (
        cue(0, 1, "Дешеві ноутбуки з RTX 3050"),
        cue(0.01, 2, "Дешеві ноутбуки з RTX 3050 коштують мало"),
        cue(1.8, 2.01, "RTX 3050 коштують мало"),
        cue(2.01, 3, "RTX 3050 коштують мало сьогодні."),
    )
    result = compact_rolling_captions(source)
    assert " ".join(s.text for s in result) == "Дешеві ноутбуки з RTX 3050 коштують мало сьогодні."
    assert result[0].start_seconds == 0
    assert result[-1].start_seconds == 2.01
    assert len(result) == 3
    assert source[1].text == "Дешеві ноутбуки з RTX 3050 коштують мало"
    assert result[1].provenance.endswith(":rolling_overlap_removed")


def test_compaction_handles_case_and_punctuation_without_guessing_model_numbers() -> None:
    source = (
        cue(0, 1, "Model RTX 3050, costs"),
        cue(0.5, 2, "RTX 3050 costs 200 euros."),
    )
    result = compact_rolling_captions(source)
    assert " ".join(s.text for s in result) == "Model RTX 3050, costs 200 euros."
    assert "3050" in transcript_to_jsonl(result)


def test_same_words_repeated_after_gap_remain_a_second_utterance() -> None:
    source = (cue(0, 1, "Use fresh paste"), cue(20, 21, "Use fresh paste"))
    assert tuple(s.text for s in compact_rolling_captions(source)) == (
        "Use fresh paste", "Use fresh paste"
    )


def test_single_word_repetition_is_never_silently_erased() -> None:
    source = (cue(0, 1, "No"), cue(0.5, 1.5, "No"))
    assert len(compact_rolling_captions(source)) == 2


def test_cross_provider_or_language_cues_are_not_fused() -> None:
    source = (
        cue(0, 1, "two words here", "en", "youtube_auto"),
        cue(0.5, 2, "two words here again", "de", "youtube_auto"),
        cue(1.2, 3, "two words here again", "de", "manual"),
    )
    assert len(compact_rolling_captions(source)) == 3


def test_coverage_does_not_mean_word_accuracy() -> None:
    source = (
        cue(0, 20, "spoken words might still have incorrect brand names"),
        cue(20, 40, "auto generated subtitles can contain technical errors"),
        cue(40, 60, "independent audio verification has not happened here"),
    )
    quality = assess_quality(source, media_duration_seconds=60, max_chars=2000)
    assert quality.status == "GOOD"
    assert quality.coverage_ratio == 1.0
    # Even a perfect coverage ratio is intentionally not an ASR accuracy score.
    assert all(segment.confidence is None for segment in source)


def test_rerun_is_deterministic_and_has_no_side_effects() -> None:
    source = (
        cue(0, 1, "Words can be repeated"),
        cue(0.1, 2, "Words can be repeated by captions"),
    )
    first = compact_rolling_captions(source)
    second = compact_rolling_captions(source)
    assert transcript_to_jsonl(first) == transcript_to_jsonl(second)
    assert first[0].text == source[0].text


def test_invalid_compaction_bounds_fail_closed() -> None:
    source = (cue(0, 1, "hello there"),)
    for gap, lookback in ((-1, 128), (31, 128), (3, 1), (3, 513)):
        try:
            compact_rolling_captions(source, max_gap_seconds=gap, max_lookback_words=lookback)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid compaction limits must be rejected")
