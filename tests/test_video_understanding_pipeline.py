from __future__ import annotations

import os
from pathlib import Path

from skeleton_media.video_understanding.artifact_store import PrivateArtifactStore
from skeleton_media.video_understanding.intake_adapters import AcquiredSource, SourceMetadata
from skeleton_media.video_understanding.models import ProcessingMode
from skeleton_media.video_understanding.pipeline import VideoPipeline
from skeleton_media.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from skeleton_media.video_understanding.url_classifier import classify_local_reference


SECTIONS = {
    "ABOUT": {"summary": "Synthetic understanding"},
    "STRUCTURE": [],
    "METHOD": [],
    "ENTITIES": [],
    "CLAIMS": [],
    "VISUAL_EVIDENCE": [],
    "TIMESTAMPS": [],
    "ACTIONS": [],
    "CONFLICTS": [],
    "CONFIDENCE": {"status": "HIGH"},
}


def config(tmp_path: Path) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir(); source = local / "sample.mp4"; source.write_bytes(b"source")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "art",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "tmp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: f"/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="server-model",
        limits=RuntimeLimits(lease_seconds=30),
    )


class Adapter:
    def acquire(self, reference, workspace, mode):
        del mode
        classification = classify_local_reference(reference)
        subtitle = workspace / "subtitle.en.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:20.000\n" + "a" * 50 + "\n\n"
            "00:00:20.000 --> 00:00:40.000\n" + "b" * 50 + "\n\n"
            "00:00:40.000 --> 00:01:00.000\n" + "c" * 50 + "\n"
        )
        return AcquiredSource(
            classification,
            SourceMetadata("LOCAL_MEDIA", reference, "local_file", "Synthetic", 60, None, None),
            None,
            (subtitle,),
            None,
            False,
        )


def unlock(root: Path) -> None:
    for path in root.rglob("*"):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def test_pipeline_finalizes_artifacts_before_memory_and_projection_can_degrade(tmp_path: Path) -> None:
    cfg = config(tmp_path); store = PrivateArtifactStore(cfg); order = []

    def memory(envelope):
        order.append("memory")
        video_id = envelope["payload"]["value"]["video_record_id"]
        assert store.target_root(video_id, "r1").is_dir()
        return {"status": "DONE"}

    def bridge(packet):
        del packet
        order.append("ollama")
        return SECTIONS

    def projection(record):
        del record
        order.append("projection")
        raise RuntimeError("down")

    pipeline = VideoPipeline(
        cfg,
        processing_revision="r1",
        memory_executor=memory,
        projection_executor=projection,
        bridge_executor=bridge,
        local_adapter=Adapter(),
    )
    result = pipeline.process(source="local-media:abcdefghijklmnop", mode=ProcessingMode.STANDARD, approval_ref="operator.video.test")
    assert order == ["ollama", "memory", "projection"]
    assert result.public["canonical_mutation_status"] == "COMMITTED"
    assert result.public["projection_status"] == "DEGRADED"
    assert result.public["review_required"] is False
    assert (result.artifact.root / "transcript/original/subtitle-000.vtt").is_file()
    assert (result.artifact.root / "summary.md").is_file()
    assert (result.artifact.root / "scenes.jsonl").is_file()
    unlock(result.artifact.root)


def test_invalid_ollama_evidence_routes_to_review_but_keeps_canonical_record(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    bad = {
        **SECTIONS,
        "CLAIMS": [
            {
                "claim_id": "claim:1",
                "text": "invented",
                "support_type": "TRANSCRIPT_ONLY",
                "evidence_ids": ["missing"],
                "confidence": .9,
            }
        ],
    }
    pipeline = VideoPipeline(
        cfg,
        processing_revision="r2",
        memory_executor=lambda envelope: {"status": "DONE"},
        bridge_executor=lambda packet: bad,
        local_adapter=Adapter(),
    )
    result = pipeline.process(source="local-media:abcdefghijklmnop", mode="STANDARD", approval_ref="operator.video.test")
    assert result.public["review_required"] is True
    assert result.record.state.value == "REVIEW_REQUIRED"
    unlock(result.artifact.root)



def test_transcribe_only_never_calls_llm_memory_or_vision(tmp_path: Path) -> None:
    import json

    cfg = config(tmp_path)
    downstream = []

    def forbidden(name):
        def fail(*args, **kwargs):
            downstream.append(name)
            raise AssertionError(f"transcript-only must not call {name}")
        return fail

    pipeline = VideoPipeline(
        cfg,
        processing_revision="transcript-v1",
        memory_executor=forbidden("memory"),
        bridge_executor=forbidden("LLM"),
        projection_executor=forbidden("projection"),
        local_adapter=Adapter(),
    )
    pipeline._vision = forbidden("vision")
    result = pipeline.transcribe_only(
        source="local-media:abcdefghijklmnop",
        approval_ref="synthetic.operator",
    )
    assert result.public["status"] == "DONE_TRANSCRIPT"
    assert result.public["word_accuracy_status"] == "UNVERIFIED"
    assert result.public["canonical_mutation_status"] == "NOT_USED"
    assert result.public["llm_status"] == "NOT_RUN"
    assert result.public["vision_status"] == "NOT_RUN"
    assert downstream == []
    root = result.artifact.root
    assert (root / "transcript/readable.jsonl").is_file()
    assert (root / "transcript/normalized.jsonl").is_file()
    assert (root / "transcript/original/subtitle-000.vtt").is_file()
    assert (root / "transcript/verification.json").is_file()
    assert not (root / "summary.md").exists()
    assert not (root / "ocr.jsonl").exists()
    assert not (root / "llm_tasks.jsonl").exists()
    verification = json.loads((root / "transcript/verification.json").read_text())
    assert verification["independent_asr"] == "NOT_REQUESTED"
    unlock(root)


def test_transcribe_only_requested_asr_with_no_audio_returns_review(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    pipeline = VideoPipeline(
        cfg,
        processing_revision="transcript-asr-missing",
        memory_executor=lambda _payload: (_ for _ in ()).throw(AssertionError("NO_MEMORY")),
        bridge_executor=lambda _packet: (_ for _ in ()).throw(AssertionError("NO_LLM")),
        local_adapter=Adapter(),
    )
    result = pipeline.transcribe_only(
        source="local-media:abcdefghijklmnop",
        approval_ref="synthetic.operator",
        verify_with_independent_asr=True,
    )
    assert result.public["status"] == "REVIEW_REQUIRED"
    assert result.public["independent_asr"] == "AUDIO_UNAVAILABLE"
    unlock(result.artifact.root)


def test_transcribe_only_independent_asr_disagreement_preserves_both_sources(tmp_path: Path) -> None:
    import json

    from skeleton_media.video_understanding.sona_backend import SonaResult
    from skeleton_media.video_understanding.transcript import TranscriptSegment

    class MediaAdapter(Adapter):
        def acquire(self, reference, workspace, mode):
            original = super().acquire(reference, workspace, mode)
            media = workspace / "synthetic_source.mp4"
            media.write_bytes(b"synthetic media; no real decoding")
            return AcquiredSource(
                original.classification,
                original.metadata,
                media,
                original.subtitle_paths,
                original.source_sha256,
                True,
            )

    class InjectedFakeAsr:
        def extract_audio(self, runner, media_path, workspace):
            del runner, media_path
            audio = workspace / "audio.wav"
            audio.write_bytes(b"RIFF" + b"\x00" * 32)
            return audio

        def transcribe(self, audio_path):
            assert audio_path.is_file()
            return SonaResult(
                segments=(
                    TranscriptSegment(0, 20, "Незалежне ASR називає RTX 9999", "en", "sona"),
                    TranscriptSegment(20, 40, "Інший текст", "en", "sona"),
                    TranscriptSegment(40, 60, "Тут інші числа 123", "en", "sona"),
                ),
                language="en",
            )

    cfg = config(tmp_path)
    pipeline = VideoPipeline(
        cfg,
        processing_revision="transcript-asr-review",
        memory_executor=lambda _envelope: (_ for _ in ()).throw(AssertionError("NO_MEMORY")),
        bridge_executor=lambda _packet: (_ for _ in ()).throw(AssertionError("NO_LLM")),
        local_adapter=MediaAdapter(),
        sona=InjectedFakeAsr(),
    )
    result = pipeline.transcribe_only(
        source="local-media:abcdefghijklmnop",
        approval_ref="synthetic.operator",
        mode="STANDARD",
        verify_with_independent_asr=True,
    )
    assert result.public["status"] == "REVIEW_REQUIRED"
    assert result.public["independent_asr"] == "REVIEW_REQUIRED"
    root = result.artifact.root
    assert (root / "transcript/independent_asr.jsonl").is_file()
    assert (root / "transcript/original/subtitle-000.vtt").is_file()
    assert not (root / "audio.wav").exists()
    assert not (root / "synthetic_source.mp4").exists()
    check = json.loads((root / "transcript/verification.json").read_text())
    assert "TECHNICAL_MENTION_DISAGREEMENT" in check["review_reasons"]
    assert check["word_accuracy_status"] == "UNVERIFIED"
    unlock(root)


def test_transcribe_only_rejects_missing_operator_reference_and_archive(tmp_path: Path) -> None:
    import pytest
    from skeleton_media.video_understanding.models import VideoUnderstandingError

    cfg = config(tmp_path)
    pipeline = VideoPipeline(
        cfg,
        processing_revision="transcript-invalid",
        memory_executor=lambda _env: {"status": "DONE"},
        bridge_executor=lambda _packet: SECTIONS,
        local_adapter=Adapter(),
    )
    with pytest.raises(VideoUnderstandingError):
        pipeline.transcribe_only(source="local-media:abcdefghijklmnop", approval_ref="")
    with pytest.raises(VideoUnderstandingError):
        pipeline.transcribe_only(
            source="local-media:abcdefghijklmnop",
            approval_ref="synthetic.operator",
            mode="ARCHIVE",
        )



def test_transcribe_only_quick_asr_requests_media_without_changing_public_mode(tmp_path: Path) -> None:
    from skeleton_media.video_understanding.models import ProcessingMode

    modes = []

    class TrackingAdapter(Adapter):
        def acquire(self, reference, workspace, mode):
            modes.append(mode)
            return super().acquire(reference, workspace, mode)

    pipeline = VideoPipeline(
        config(tmp_path),
        processing_revision="transcript-quick-verified-acquisition",
        memory_executor=lambda _: (_ for _ in ()).throw(AssertionError("NO_MEMORY")),
        bridge_executor=lambda _: (_ for _ in ()).throw(AssertionError("NO_LLM")),
        local_adapter=TrackingAdapter(),
    )
    result = pipeline.transcribe_only(
        source="local-media:abcdefghijklmnop",
        approval_ref="synthetic.operator",
        mode="QUICK",
        verify_with_independent_asr=True,
    )
    # QUICK must not silently skip optional audio when explicitly requested.
    assert modes == [ProcessingMode.STANDARD]
    assert result.public["independent_asr"] == "AUDIO_UNAVAILABLE"
    assert result.public["review_required"] is True
    unlock(result.artifact.root)


def test_transcribe_only_offline_asr_is_review_and_audio_is_cleaned(tmp_path: Path) -> None:
    from skeleton_media.video_understanding.sona_backend import SonaBackend

    class MediaAdapter(Adapter):
        def acquire(self, reference, workspace, mode):
            source = super().acquire(reference, workspace, mode)
            media = workspace / "synthetic_video.mp4"
            media.write_bytes(b"not a real media file")
            return AcquiredSource(
                source.classification, source.metadata, media,
                source.subtitle_paths, source.source_sha256, True,
            )

    cfg = config(tmp_path)
    asr = SonaBackend(
        cfg,
        requester=lambda *args: (_ for _ in ()).throw(ConnectionRefusedError("offline")),
    )

    def synthetic_audio(runner, media_path, workspace):
        del runner, media_path
        audio = workspace / "audio.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 40)
        return audio

    asr.extract_audio = synthetic_audio
    pipeline = VideoPipeline(
        cfg,
        processing_revision="transcript-offline-reviewed",
        memory_executor=lambda _: (_ for _ in ()).throw(AssertionError("NO_MEMORY")),
        bridge_executor=lambda _: (_ for _ in ()).throw(AssertionError("NO_LLM")),
        local_adapter=MediaAdapter(),
        sona=asr,
    )
    result = pipeline.transcribe_only(
        source="local-media:abcdefghijklmnop",
        approval_ref="synthetic.operator",
        verify_with_independent_asr=True,
    )
    assert result.public["status"] == "REVIEW_REQUIRED"
    assert result.public["independent_asr"] == "FAILED"
    assert not (result.artifact.root / "audio.wav").exists()
    assert not (result.artifact.root / "synthetic_video.mp4").exists()
    assert "SONA_UNAVAILABLE" in (
        result.artifact.root / "transcript/verification.json"
    ).read_text()
    unlock(result.artifact.root)
