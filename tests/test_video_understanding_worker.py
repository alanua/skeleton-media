from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.queue import FileQueue
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from core.video_understanding.worker import VideoWorker


def config(tmp_path: Path, attempts: int = 2) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir(parents=True); source = local / "x"; source.write_bytes(b"x")
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


class Pipeline:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def process(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise VideoUnderstandingError(self.error, "failed")
        return SimpleNamespace(
            public={
                "status": "DONE",
                "review_required": False,
                "canonical_mutation_status": "COMMITTED",
                "projection_status": "NOT_CONFIGURED",
                "transcript_count": 1,
                "frame_count": 0,
                "ocr_count": 0,
                "evidence_count": 1,
            }
        )


def payload(profile=None):
    value = {"source": "local-media:abcdefghijklmnop", "approval_ref": "operator.video.test", "mode": "STANDARD"}
    if profile is not None:
        value["profile"] = profile
    return value


def test_worker_processes_one_and_returns_aggregate_status(tmp_path: Path) -> None:
    queue = FileQueue(config(tmp_path)); queue.enqueue(operation="video_process_one", payload=payload(), idempotency_key="x")
    result = VideoWorker(queue, Pipeline(), worker_id="worker-1").run_once()
    assert result["status"] == "DONE" and result["canonical_mutation_status"] == "COMMITTED"
    assert queue.counts()["done"] == 1


def test_worker_retries_transient_and_quarantines_invalid_profile(tmp_path: Path) -> None:
    queue = FileQueue(config(tmp_path)); queue.enqueue(operation="video_process_one", payload=payload(), idempotency_key="x")
    result = VideoWorker(queue, Pipeline("LOCAL_LLM_UNAVAILABLE"), worker_id="worker-1").run_once()
    assert result["status"] == "RETRY"
    queue2 = FileQueue(config(tmp_path / "second")); queue2.enqueue(operation="video_process_one", payload=payload("BAD"), idempotency_key="y")
    result2 = VideoWorker(queue2, Pipeline(), worker_id="worker-1").run_once()
    assert result2["status"] == "QUARANTINED" and result2["reason_code"] == "INVALID_PROFILE"
