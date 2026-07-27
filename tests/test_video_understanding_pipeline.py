from __future__ import annotations

import os
from pathlib import Path

from core.video_understanding.artifact_store import PrivateArtifactStore
from core.video_understanding.intake_adapters import AcquiredSource, SourceMetadata
from core.video_understanding.models import ProcessingMode
from core.video_understanding.pipeline import VideoPipeline
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from core.video_understanding.url_classifier import classify_local_reference


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
