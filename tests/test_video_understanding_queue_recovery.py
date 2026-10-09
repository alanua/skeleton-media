from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from skeleton_media.video_understanding.models import VideoUnderstandingError
from skeleton_media.video_understanding.queue import FileQueue
from skeleton_media.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from skeleton_media.video_understanding.worker import VideoWorker


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def config(tmp_path: Path, *, lease_seconds: int = 30, attempts: int = 3) -> VideoRuntimeConfig:
    local = tmp_path / "private-customer-media"
    local.mkdir(parents=True)
    source = local / "customer-secret-video.mp4"
    source.write_bytes(b"synthetic offline video placeholder")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "private-artifacts",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "tmp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: f"/offline/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="offline-test-model",
        limits=RuntimeLimits(lease_seconds=lease_seconds, max_attempts=attempts),
    )


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "local-media:abcdefghijklmnop",
        "approval_ref": "operator.video.test",
        "mode": "STANDARD",
    }
    payload.update(overrides)
    return payload


class OfflinePipeline:
    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.calls: list[dict[str, object]] = []

    def process(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        marker = self.artifact_root / "finalized-summary.json"
        marker.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            public={
                "status": "DONE",
                "review_required": False,
                "canonical_mutation_status": "COMMITTED",
                "projection_status": "NOT_CONFIGURED",
                "transcript_count": 0,
                "frame_count": 0,
                "ocr_count": 0,
                "evidence_count": 1,
            }
        )


def assert_public_safe(value: object, *private_needles: str) -> None:
    rendered = repr(value)
    for needle in private_needles:
        assert needle not in rendered


def test_stale_lease_is_recovered_and_claimed_by_next_worker(tmp_path: Path) -> None:
    clock = Clock()
    queue = FileQueue(config(tmp_path), clock=clock)
    record = queue.enqueue(
        operation="video_process_one",
        payload=valid_payload(),
        idempotency_key="customer-request-1",
    )

    claimed = queue.claim("worker-before-crash")
    assert claimed is not None
    assert claimed.attempts == 1
    clock.advance(31)

    assert queue.recover_expired() == 1
    recovered = queue.claim("worker-after-crash")
    assert recovered is not None
    assert recovered.record_id == record.record_id
    assert recovered.attempts == 2

    with pytest.raises(VideoUnderstandingError, match="worker does not own queue record"):
        queue.complete(record.record_id, "worker-before-crash")


def test_duplicate_request_idempotency_keeps_original_payload(tmp_path: Path) -> None:
    queue = FileQueue(config(tmp_path))
    first = queue.enqueue(
        operation="video_process_one",
        payload=valid_payload(question="first question"),
        idempotency_key="same-customer-request",
    )
    replay = queue.enqueue(
        operation="video_process_one",
        payload=valid_payload(question="different replay body"),
        idempotency_key="same-customer-request",
    )

    assert replay.record_id == first.record_id
    assert queue.counts()["pending"] == 1
    assert queue.get(first.record_id).payload["question"] == "first question"


def test_crash_after_artifact_finalization_recovers_and_acknowledges(tmp_path: Path) -> None:
    clock = Clock()
    cfg = config(tmp_path)
    queue = FileQueue(cfg, clock=clock)
    record = queue.enqueue(
        operation="video_process_one",
        payload=valid_payload(),
        idempotency_key="crash-after-finalize",
    )
    pipeline = OfflinePipeline(cfg.artifact_root)

    claimed = queue.claim("worker-that-crashes")
    assert claimed is not None
    VideoWorker(queue, pipeline, worker_id="worker-that-crashes")._execute(claimed)
    assert (cfg.artifact_root / "finalized-summary.json").exists()
    assert queue.counts()["processing"] == 1

    clock.advance(31)
    result = VideoWorker(queue, pipeline, worker_id="worker-that-recovers").run_once()

    assert result["status"] == "DONE"
    assert result["recovered_lease_count"] == 1
    assert queue.get(record.record_id).state == "done"
    assert len(pipeline.calls) == 2


def test_malformed_request_payload_is_quarantined(tmp_path: Path) -> None:
    queue = FileQueue(config(tmp_path))
    queue.enqueue(
        operation="video_process_one",
        payload={"source": "local-media:abcdefghijklmnop", "mode": "STANDARD"},
        idempotency_key="missing-approval-ref",
    )

    result = VideoWorker(queue, OfflinePipeline(tmp_path / "artifacts"), worker_id="worker-1").run_once()

    assert result["status"] == "QUARANTINED"
    assert result["reason_code"] == "INVALID_QUEUE_PAYLOAD"
    assert result["queue_counts"]["quarantined"] == 1


def test_public_statuses_do_not_expose_private_source_or_artifact_paths(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    queue = FileQueue(cfg)
    queue.enqueue(
        operation="video_process_one",
        payload=valid_payload(project_hint="customer private project"),
        idempotency_key="public-safe-status",
    )

    result = VideoWorker(queue, OfflinePipeline(cfg.artifact_root), worker_id="worker-1").run_once()

    assert result["status"] == "DONE"
    assert_public_safe(
        result,
        "customer-secret-video.mp4",
        "private-customer-media",
        "private-artifacts",
        "customer private project",
        "local-media:abcdefghijklmnop",
    )
