from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from skeleton_media.video_understanding.models import VideoUnderstandingError
from skeleton_media.video_understanding.queue import FileQueue
from skeleton_media.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from skeleton_media.video_understanding.worker import VideoWorker


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



class TranscriptPipeline(Pipeline):
    def __init__(self):
        super().__init__()
        self.transcript_calls = []

    def process(self, **kwargs):
        raise AssertionError("transcript-only must not dispatch full video processing")

    def transcribe_only(self, **kwargs):
        self.transcript_calls.append(kwargs)
        return SimpleNamespace(public={
            "status": "DONE_TRANSCRIPT",
            "review_required": False,
            "canonical_mutation_status": "NOT_USED",
            "transcript_count": 3,
            "readable_segment_count": 2,
            "word_accuracy_status": "UNVERIFIED",
            "independent_asr": "NOT_REQUESTED",
            "llm_status": "NOT_RUN",
            "vision_status": "NOT_RUN",
        })


def test_transcript_only_worker_dispatch_is_bounded_and_public_safe(tmp_path: Path) -> None:
    queue = FileQueue(config(tmp_path))
    request = {
        "source": "local-media:abcdefghijklmnop",
        "approval_ref": "synthetic.operator",
        "mode": "QUICK",
        "verify_with_independent_asr": False,
    }
    queue.enqueue(operation="video_transcribe_url", payload=request, idempotency_key="synthetic-transcription")
    pipeline = TranscriptPipeline()
    receipt = VideoWorker(queue, pipeline, worker_id="worker-1").run_once()
    assert queue.counts()["done"] == 1
    assert receipt["status"] == "DONE"
    assert receipt["reason_code"] == "TRANSCRIPT_PROCESSED"
    assert receipt["processing_status"] == "DONE_TRANSCRIPT"
    assert receipt["canonical_mutation_status"] == "NOT_USED"
    assert receipt["word_accuracy_status"] == "UNVERIFIED"
    assert receipt["llm_status"] == "NOT_RUN"
    assert receipt["vision_status"] == "NOT_RUN"
    assert receipt["transcript_count"] == 3
    assert "source" not in receipt and "approval_ref" not in receipt
    assert "frame_count" not in receipt and "projection_status" not in receipt
    assert pipeline.transcript_calls[0]["verify_with_independent_asr"] is False
    assert pipeline.transcript_calls[0]["mode"].value == "QUICK"


def test_transcript_only_rejects_untrusted_queue_payload_and_unknown_operation(tmp_path: Path) -> None:
    for index, payload in enumerate((
        {"source": "local-media:abcdefghijklmnop", "approval_ref": "synthetic.operator", "mode": "QUICK", "verify_with_independent_asr": "true"},
        {"source": "local-media:abcdefghijklmnop", "approval_ref": "synthetic.operator", "mode": "ARCHIVE", "verify_with_independent_asr": False},
        {"source": "local-media:abcdefghijklmnop", "approval_ref": "synthetic.operator", "mode": "QUICK", "verify_with_independent_asr": False, "shell": "forbidden"},
        {"source": "local-media:abcdefghijklmnop", "approval_ref": "", "mode": "QUICK", "verify_with_independent_asr": False},
    )):
        queue = FileQueue(config(tmp_path / f"case-{index}"))
        queue.enqueue(operation="video_transcribe_url", payload=payload, idempotency_key=f"bad-{index}")
        pipeline = TranscriptPipeline()
        receipt = VideoWorker(queue, pipeline, worker_id="worker-1").run_once()
        assert receipt["status"] == "QUARANTINED"
        assert receipt["reason_code"].startswith("INVALID_")
        assert pipeline.transcript_calls == []
    queue = FileQueue(config(tmp_path / "unknown"))
    queue.enqueue(operation="video_worker_bypass", payload=payload(), idempotency_key="unexpected")
    receipt = VideoWorker(queue, TranscriptPipeline(), worker_id="worker-1").run_once()
    assert receipt["status"] == "QUARANTINED"
    assert receipt["reason_code"] == "INVALID_QUEUE_OPERATION"


def test_cli_enqueues_transcript_only_without_colliding_asr_identity(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    from scripts import video_understanding as cli

    cfg = config(tmp_path)
    monkeypatch.setattr(cli, "load_runtime_config", lambda path: cfg)
    monkeypatch.setenv("SKELETON_VIDEO_APPROVAL_REF", "synthetic.operator")
    url = "https://youtu.be/AbCdEf12345"
    for enabled in (False, True):
        monkeypatch.setattr(cli, "_read_request", lambda enabled=enabled: {
            "operation": "video_transcribe_url",
            "payload": {
                "url": url,
                "mode": "QUICK",
                "verify_with_independent_asr": enabled,
            },
        })
        assert cli.main(["enqueue", "--config", "synthetic.json"]) == 0
        output = capsys.readouterr().out
        assert "QUEUED" in output
        assert url not in output
        assert "synthetic.operator" not in output
    queue = FileQueue(cfg)
    assert queue.counts()["pending"] == 2
    first = queue.claim("worker-1")
    second = queue.claim("worker-2")
    assert first is not None and second is not None
    assert first.operation == second.operation == "video_transcribe_url"
    assert first.record_id != second.record_id
    assert {first.payload["verify_with_independent_asr"], second.payload["verify_with_independent_asr"]} == {True, False}
