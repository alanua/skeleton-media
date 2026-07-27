#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Mapping

from core.video_understanding.pipeline import VideoPipeline
from core.video_understanding.queue import FileQueue
from core.video_understanding.worker import VideoWorker


def run_once(
    queue: FileQueue,
    pipeline: VideoPipeline,
    *,
    worker_id: str,
) -> Mapping[str, object]:
    return VideoWorker(queue, pipeline, worker_id=worker_id).run_once()


def run_forever(
    queue: FileQueue,
    pipeline: VideoPipeline,
    *,
    worker_id: str,
    poll_seconds: float = 5.0,
) -> None:
    VideoWorker(queue, pipeline, worker_id=worker_id).run_forever(poll_seconds=poll_seconds)


def main() -> int:
    payload = {
        "schema": "skeleton.video_understanding.worker_bootstrap.v1",
        "status": "BLOCKED",
        "reason_code": "PROTECTED_EXECUTOR_WIRING_REQUIRED",
    }
    print(json.dumps(payload, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
