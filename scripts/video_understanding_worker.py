#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.video_understanding.live_runtime import (
    build_live_runtime,
    doctor_live_runtime,
    synthetic_memory_roundtrip,
)
from core.video_understanding.models import VideoUnderstandingError
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
    VideoWorker(queue, pipeline, worker_id=worker_id).run_forever(
        poll_seconds=poll_seconds
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the private Skeleton Video Understanding worker."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--processing-revision", required=True)
    parser.add_argument("--worker-id", default="hetzner-video-worker-1")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--doctor", action="store_true")
    mode.add_argument("--memory-roundtrip", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--forever", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.doctor:
            _print_public(
                doctor_live_runtime(
                    args.config,
                    processing_revision=args.processing_revision,
                )
            )
            return 0
        runtime = build_live_runtime(
            args.config,
            processing_revision=args.processing_revision,
        )
        if args.memory_roundtrip:
            receipt = synthetic_memory_roundtrip(
                runtime,
                approval_ref="operator.video.runtime.launch",
            )
            _print_public(receipt)
            return 0 if receipt.get("status") == "DONE" else 2
        if args.once:
            _print_public(
                run_once(runtime.queue, runtime.pipeline, worker_id=args.worker_id)
            )
            return 0
        if not 0.5 <= args.poll_seconds <= 300:
            raise VideoUnderstandingError(
                "POLL_INTERVAL_INVALID",
                "worker poll interval is invalid",
            )
        _print_public(
            {
                "schema": "skeleton.video_understanding.worker_start.v1",
                "status": "RUNNING",
                "worker_count": 1,
                "queue_counts": runtime.queue.counts(),
            }
        )
        run_forever(
            runtime.queue,
            runtime.pipeline,
            worker_id=args.worker_id,
            poll_seconds=args.poll_seconds,
        )
        return 0
    except VideoUnderstandingError as exc:
        _print_public(
            {
                "schema": "skeleton.video_understanding.worker_error.v1",
                "status": "BLOCKED",
                "reason_code": exc.reason_code,
            }
        )
        return 2
    except Exception as exc:
        _print_public(
            {
                "schema": "skeleton.video_understanding.worker_error.v1",
                "status": "BLOCKED",
                "reason_code": type(exc).__name__,
            }
        )
        return 2


def _print_public(payload: Mapping[str, object]) -> None:
    print(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
