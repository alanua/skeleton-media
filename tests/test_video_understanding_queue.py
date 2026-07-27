from __future__ import annotations

from pathlib import Path

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.queue import FileQueue, QueueRecord
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig


class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value
    def advance(self, value): self.value += value


def config(tmp_path: Path, attempts: int = 3) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir(); source = local / "x"; source.write_bytes(b"x")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "art",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "tmp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: f"/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="m",
        limits=RuntimeLimits(lease_seconds=30, max_attempts=attempts),
    )


def test_enqueue_claim_heartbeat_complete_and_replay(tmp_path: Path) -> None:
    clock = Clock(); queue = FileQueue(config(tmp_path), clock=clock)
    record = queue.enqueue(operation="video_process_one", payload={"source": "local-media:abcdefghijklmnop"}, idempotency_key="private-id")
    assert queue.enqueue(operation="video_process_one", payload={}, idempotency_key="private-id").record_id == record.record_id
    claimed = queue.claim("worker-1"); assert claimed and claimed.state == "processing" and claimed.attempts == 1
    clock.advance(5); assert queue.heartbeat(record.record_id, "worker-1").lease_until == clock.value + 30
    assert queue.complete(record.record_id, "worker-1").state == "done"


def test_expired_lease_recovers_and_retry_quarantines(tmp_path: Path) -> None:
    clock = Clock(); queue = FileQueue(config(tmp_path, attempts=2), clock=clock)
    record = queue.enqueue(operation="video_process_one", payload={}, idempotency_key="a")
    queue.claim("worker-1"); clock.advance(31)
    assert queue.recover_expired() == 1
    claimed = queue.claim("worker-1"); assert claimed and claimed.attempts == 2
    failed = queue.fail(record.record_id, "worker-1", reason_code="TRANSIENT_FAILURE")
    assert failed.state == "quarantined"


def test_queue_rejects_non_json_payload_reason_and_boolean_attempts(tmp_path: Path) -> None:
    queue = FileQueue(config(tmp_path))
    with pytest.raises(VideoUnderstandingError):
        queue.enqueue(operation="video_process_one", payload={"bad": float("nan")}, idempotency_key="x")
    record = queue.enqueue(operation="video_process_one", payload={}, idempotency_key="y")
    queue.claim("worker-1")
    with pytest.raises(VideoUnderstandingError):
        queue.fail(record.record_id, "worker-1", reason_code="private/path")
    payload = {
        "schema": "skeleton.video_understanding.queue_record.v1",
        "record_id": "job_" + "a" * 32,
        "idempotency_key": "x",
        "operation": "video_process_one",
        "payload": {},
        "state": "pending",
        "attempts": True,
        "available_at": 1,
        "lease_until": None,
        "worker_id": None,
        "reason_code": None,
        "created_at": 1,
        "updated_at": 1,
    }
    with pytest.raises(VideoUnderstandingError):
        QueueRecord.from_dict(payload)
