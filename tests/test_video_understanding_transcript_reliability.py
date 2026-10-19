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



# The bridge is never connected to a real network, device, or model in CI.
from pathlib import Path

import pytest

from skeleton_media.video_understanding.models import VideoUnderstandingError
from skeleton_media.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from skeleton_media.video_understanding.sona_backend import PrivateAsrBridgeRequester, SonaBackend
from skeleton_media.video_understanding.transcript import compare_independent_asr


def _synthetic_config(tmp_path: Path) -> VideoRuntimeConfig:
    local = tmp_path / "local"
    local.mkdir()
    source = local / "sample.mp4"
    source.write_bytes(b"synthetic")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "artifact",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "scratch",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: f"/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="synthetic",
        limits=RuntimeLimits(lease_seconds=30),
    )


def _synthetic_wav(path: Path, *, bytes_count: int = 1_048_576) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (bytes_count - 12))
    return path


def test_private_asr_bridge_is_bounded_and_injected(tmp_path: Path) -> None:
    config = _synthetic_config(tmp_path)
    audio = _synthetic_wav(config.temp_root / "audio.wav")
    calls = []

    def approved_executor(packet):
        calls.append(packet)
        return {
            "language": "uk",
            "segments": [{"start": 0.0, "end": 1.0, "text": "Синтетичний текст"}],
            "private_path": "/never/share",
        }

    requester = PrivateAsrBridgeRequester(
        approved_executor, approved_audio_root=config.temp_root, operator_approval_ref="synthetic.operator.v1"
    )
    result = SonaBackend(config, requester=requester).transcribe(audio)
    assert result.language == "uk" and result.segments[0].text == "Синтетичний текст"
    assert len(calls) == 1
    assert calls[0]["audio_wav"] == audio.read_bytes()
    assert calls[0]["operator_approval_ref"] == "synthetic.operator.v1"
    assert "private_path" not in requester(
        config.sona_endpoint, config.sona_model, audio,
        config.limits.subprocess_timeout_seconds, config.limits.subprocess_output_bytes
    )


def test_private_asr_bridge_rejects_unapproved_audio_and_malformed_data(tmp_path: Path) -> None:
    cfg = _synthetic_config(tmp_path)
    root = cfg.temp_root
    allowed = _synthetic_wav(root / "good.wav")
    outside = _synthetic_wav(tmp_path / "outside.wav")
    malformed = root / "bad.wav"
    malformed.write_bytes(b"NOT_WAVE" + b"x" * 1_048_576)
    no_calls = []

    def executor(packet):
        no_calls.append(packet)
        return {"language": "uk", "segments": []}

    bridge = PrivateAsrBridgeRequester(
        executor, approved_audio_root=root, operator_approval_ref="synthetic.operator.v1"
    )
    for path in (outside, malformed):
        with pytest.raises(VideoUnderstandingError):
            bridge(cfg.sona_endpoint, cfg.sona_model, path, 10, 4096)
    assert no_calls == []
    with pytest.raises(VideoUnderstandingError):
        PrivateAsrBridgeRequester(executor, approved_audio_root=root, operator_approval_ref="")
    with pytest.raises(VideoUnderstandingError):
        bridge("http://remote.example:8080", "default", allowed, 10, 4096)

    invalid = PrivateAsrBridgeRequester(
        lambda _: {"segments": "invalid"}, approved_audio_root=root,
        operator_approval_ref="synthetic.operator.v1",
    )
    with pytest.raises(VideoUnderstandingError):
        invalid(cfg.sona_endpoint, cfg.sona_model, allowed, 10, 4096)


def test_independent_asr_never_claims_ground_truth() -> None:
    subtitles = (
        cue(0, 2, "Цей ноутбук має RTX 3050"),
        cue(2, 4, "Також у нього 16 ГБ RAM"),
    )
    identical = (
        cue(0, 2, "Цей ноутбук має RTX 3050", "uk", "sona"),
        cue(2, 4, "Також у нього 16 ГБ RAM", "uk", "sona"),
    )
    same = compare_independent_asr(subtitles, identical)
    assert same.status == "AGREE_UNVERIFIED"
    assert same.reason_codes == ()
    assert same.token_sequence_similarity == 1.0

    disagreement = (
        cue(0, 2, "Цей ноутбук має ZX 3050", "uk", "sona"),
        cue(2, 4, "Також у нього 60 ГБ RAM", "uk", "sona"),
    )
    changed = compare_independent_asr(subtitles, disagreement)
    assert changed.status == "REVIEW_REQUIRED"
    assert "TECHNICAL_MENTION_DISAGREEMENT" in changed.reason_codes
    assert changed.token_sequence_similarity is not None
    assert compare_independent_asr(subtitles, ()).status == "REVIEW_REQUIRED"
    assert compare_independent_asr((), identical).status == "REVIEW_REQUIRED"


def test_asr_comparison_rejects_bad_bounds_and_language_mismatch() -> None:
    source = (cue(0, 2, "one two three", "en"),)
    asr = (cue(0, 2, "one two three", "de", "sona"),)
    changed = compare_independent_asr(source, asr)
    assert changed.status == "REVIEW_REQUIRED"
    assert "LANGUAGE_DISAGREEMENT" in changed.reason_codes
    with pytest.raises(ValueError):
        compare_independent_asr(source, asr, review_below_similarity=0)
    with pytest.raises(ValueError):
        compare_independent_asr(source, asr, max_tokens_per_source=3)



def test_private_bridge_requires_absolute_root_and_offline_failure_is_typed(tmp_path: Path) -> None:
    with pytest.raises(VideoUnderstandingError) as root_error:
        PrivateAsrBridgeRequester(
            lambda packet: {"language": "uk", "segments": []},
            approved_audio_root=Path("relative-user-audio"),
            operator_approval_ref="synthetic.operator",
        )
    assert root_error.value.reason_code == "ASR_BRIDGE_ROOT_INVALID"

    cfg = _synthetic_config(tmp_path)
    audio = _synthetic_wav(cfg.temp_root / "offline.wav")
    unavailable = SonaBackend(
        cfg,
        requester=lambda *args: (_ for _ in ()).throw(TimeoutError("private endpoint timed out")),
    )
    with pytest.raises(VideoUnderstandingError) as offline_error:
        unavailable.transcribe(audio)
    assert offline_error.value.reason_code == "SONA_UNAVAILABLE"
