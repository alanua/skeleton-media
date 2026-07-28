#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_install import install_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the bounded private Skeleton Video Understanding runtime."
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install_runtime(
            ROOT,
            expected_sha=args.expected_sha,
            enable=args.enable,
        )
        print(json.dumps(result.public_dict(), sort_keys=True))
        return 0 if result.service_active or not args.enable else 2
    except VideoUnderstandingError as exc:
        print(
            json.dumps(
                {
                    "schema": "skeleton.video_understanding.install_receipt.v1",
                    "status": "BLOCKED",
                    "service_active": False,
                    "worker_count": 0,
                    "rollback_ready": True,
                    "stable_reason_codes": [exc.reason_code],
                },
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "skeleton.video_understanding.install_receipt.v1",
                    "status": "BLOCKED",
                    "service_active": False,
                    "worker_count": 0,
                    "rollback_ready": True,
                    "stable_reason_codes": [type(exc).__name__],
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
