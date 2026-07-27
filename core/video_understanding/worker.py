from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from core.video_understanding.models import Domain, ProcessingMode, VideoUnderstandingError
from core.video_understanding.pipeline import VideoPipeline
from core.video_understanding.queue import FileQueue, QueueRecord


_PERMANENT_PREFIXES = (
    "INVALID_",
    "UNSAFE_",
    "UNSUPPORTED_",
    "URL_",
    "DIRECT_MEDIA_HOST_NOT_ALLOWED",
    "LOCAL_MEDIA_NOT_REGISTERED",
    "ARTIFACT_PATH_ESCAPE",
)


class VideoWorker:
    def __init__(
        self,
        queue: FileQueue,
        pipeline: VideoPipeline,
        *,
        worker_id: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.queue = queue
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.sleep = sleep
        self.lock_path = queue.root / ".worker.lock"
        self.lock_path.touch(mode=0o600, exist_ok=True)

    def run_once(self) -> dict[str, object]:
        with self.single_instance():
            recovered = self.queue.recover_expired()
            record = self.queue.claim(self.worker_id)
            if record is None:
                return self._public("IDLE", "QUEUE_EMPTY", recovered=recovered)
            with _LeaseHeartbeat(self.queue, record, self.worker_id):
                try:
                    result = self._execute(record)
                except VideoUnderstandingError as exc:
                    permanent = exc.reason_code.startswith(_PERMANENT_PREFIXES)
                    failed = self.queue.fail(
                        record.record_id,
                        self.worker_id,
                        reason_code=exc.reason_code,
                        permanent=permanent,
                    )
                    return self._public(
                        "QUARANTINED" if failed.state == "quarantined" else "RETRY",
                        exc.reason_code,
                        recovered=recovered,
                    )
                except Exception:
                    failed = self.queue.fail(
                        record.record_id,
                        self.worker_id,
                        reason_code="UNEXPECTED_WORKER_FAILURE",
                        permanent=False,
                    )
                    return self._public(
                        "QUARANTINED" if failed.state == "quarantined" else "RETRY",
                        "UNEXPECTED_WORKER_FAILURE",
                        recovered=recovered,
                    )
            self.queue.complete(record.record_id, self.worker_id)
            return {
                **self._public("DONE", "VIDEO_PROCESSED", recovered=recovered),
                "processing_status": result.public["status"],
                "review_required": result.public["review_required"],
                "canonical_mutation_status": result.public["canonical_mutation_status"],
                "projection_status": result.public["projection_status"],
                "transcript_count": result.public["transcript_count"],
                "frame_count": result.public["frame_count"],
                "ocr_count": result.public["ocr_count"],
                "evidence_count": result.public["evidence_count"],
            }

    def run_forever(
        self,
        *,
        poll_seconds: float = 5.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        event = stop_event or threading.Event()
        with self.single_instance():
            while not event.is_set():
                recovered = self.queue.recover_expired()
                record = self.queue.claim(self.worker_id)
                if record is None:
                    event.wait(poll_seconds)
                    continue
                with _LeaseHeartbeat(self.queue, record, self.worker_id):
                    try:
                        self._execute(record)
                    except VideoUnderstandingError as exc:
                        self.queue.fail(
                            record.record_id,
                            self.worker_id,
                            reason_code=exc.reason_code,
                            permanent=exc.reason_code.startswith(_PERMANENT_PREFIXES),
                        )
                    except Exception:
                        self.queue.fail(
                            record.record_id,
                            self.worker_id,
                            reason_code="UNEXPECTED_WORKER_FAILURE",
                            permanent=False,
                        )
                    else:
                        self.queue.complete(record.record_id, self.worker_id)
                del recovered

    def _execute(self, record: QueueRecord):
        payload = record.payload
        source = payload.get("source")
        approval_ref = payload.get("approval_ref")
        if not isinstance(source, str) or not isinstance(approval_ref, str):
            raise VideoUnderstandingError("INVALID_QUEUE_PAYLOAD", "queue payload is incomplete")
        try:
            mode = ProcessingMode(payload.get("mode", "STANDARD"))
        except (TypeError, ValueError) as exc:
            raise VideoUnderstandingError("INVALID_MODE", "queue mode is invalid") from exc
        profile_raw = payload.get("profile")
        try:
            profile = Domain(profile_raw) if profile_raw is not None else None
        except (TypeError, ValueError) as exc:
            raise VideoUnderstandingError("INVALID_PROFILE", "queue profile is invalid") from exc
        question = payload.get("question")
        project_hint = payload.get("project_hint")
        if question is not None and not isinstance(question, str):
            raise VideoUnderstandingError("INVALID_QUESTION", "queue question is invalid")
        if project_hint is not None and not isinstance(project_hint, str):
            raise VideoUnderstandingError("INVALID_PROJECT_HINT", "queue project hint is invalid")
        return self.pipeline.process(
            source=source,
            mode=mode,
            approval_ref=approval_ref,
            question=question,
            project_hint=project_hint,
            profile=profile,
        )

    @contextmanager
    def single_instance(self) -> Iterator[None]:
        with self.lock_path.open("r+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise VideoUnderstandingError("WORKER_ALREADY_RUNNING", "video worker is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _public(self, status: str, reason_code: str, *, recovered: int) -> dict[str, object]:
        counts = self.queue.counts()
        return {
            "schema": "skeleton.video_understanding.worker_receipt.v1",
            "status": status,
            "reason_code": reason_code,
            "recovered_lease_count": recovered,
            "queue_counts": counts,
        }


class _LeaseHeartbeat:
    def __init__(self, queue: FileQueue, record: QueueRecord, worker_id: str) -> None:
        self.queue = queue
        self.record = record
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LeaseHeartbeat":
        interval = max(5.0, self.queue.config.limits.lease_seconds / 3)

        def beat() -> None:
            while not self._stop.wait(interval):
                try:
                    self.queue.heartbeat(self.record.record_id, self.worker_id)
                except VideoUnderstandingError:
                    return

        self._thread = threading.Thread(target=beat, name="video-lease-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
