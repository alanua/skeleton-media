#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.operations import plan_operation
from core.video_understanding.queue import FileQueue
from core.video_understanding.runtime_config import load_runtime_config


_MAX_STDIN_BYTES = 1_000_000


def _read_request() -> Mapping[str, Any]:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        raise VideoUnderstandingError("REQUEST_TOO_LARGE", "request exceeded limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoUnderstandingError("REQUEST_INVALID_JSON", "request must be JSON") from exc
    if not isinstance(payload, Mapping):
        raise VideoUnderstandingError("REQUEST_INVALID", "request must be an object")
    return payload


def _safe_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="video-understanding")
    parser.add_argument("action", choices=("plan", "enqueue", "status", "doctor"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            request = _read_request()
            operation = request.get("operation")
            payload = request.get("payload")
            if not isinstance(operation, str) or not isinstance(payload, Mapping):
                raise VideoUnderstandingError("REQUEST_INVALID", "operation and payload are required")
            _safe_print(plan_operation(operation, payload)["public_receipt"])
            return 0

        if args.config is None:
            raise VideoUnderstandingError("RUNTIME_CONFIG_REQUIRED", "private runtime config is required")
        config = load_runtime_config(args.config)
        queue = FileQueue(config)
        if args.action == "status":
            _safe_print(
                {
                    "schema": "skeleton.video_understanding.status.v1",
                    "status": "OK",
                    "queue_counts": queue.counts(),
                }
            )
            return 0
        if args.action == "doctor":
            executable_ready = sum(Path(value).is_file() for value in config.executables.values())
            _safe_print(
                {
                    **config.public_summary(),
                    "status": "READY" if executable_ready == len(config.executables) else "BLOCKED",
                    "executable_ready_count": executable_ready,
                    "executable_required_count": len(config.executables),
                    "queue_counts": queue.counts(),
                }
            )
            return 0

        request = _read_request()
        operation = request.get("operation")
        payload = request.get("payload")
        if not isinstance(operation, str) or not isinstance(payload, Mapping):
            raise VideoUnderstandingError("REQUEST_INVALID", "operation and payload are required")
        plan = plan_operation(operation, payload)
        private = plan["private_plan"]
        source_info = private.get("source")
        source = source_info.get("normalized_private_source") if isinstance(source_info, Mapping) else private.get("video_record_id")
        if operation == "video_import_urls":
            raise VideoUnderstandingError("BATCH_ENQUEUE_NOT_IMPLEMENTED", "batch enqueue requires explicit import transaction")
        if not isinstance(source, str):
            raise VideoUnderstandingError("SOURCE_REQUIRED", "operation does not contain a processable source")
        approval_ref = os.environ.get("SKELETON_VIDEO_APPROVAL_REF", "")
        if not approval_ref:
            raise VideoUnderstandingError("APPROVAL_REF_REQUIRED", "private operator approval reference is required")
        queued = queue.enqueue(
            operation="video_process_one",
            payload={
                "source": source,
                "approval_ref": approval_ref,
                "mode": private["mode"],
                "question": private.get("question"),
                "project_hint": private.get("project_hint"),
                "profile": private.get("profile"),
            },
            idempotency_key=f"{operation}:{source}:{private['mode']}:{private.get('question') or ''}",
        )
        _safe_print(
            {
                "schema": "skeleton.video_understanding.enqueue_receipt.v1",
                "status": "QUEUED",
                "queue_counts": queue.counts(),
                "attempts": queued.attempts,
            }
        )
        return 0
    except VideoUnderstandingError as exc:
        _safe_print(
            {
                "schema": "skeleton.video_understanding.cli_error.v1",
                "status": "BLOCKED",
                "reason_code": exc.reason_code,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
