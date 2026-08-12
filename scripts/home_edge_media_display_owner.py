#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


LOCAL_LIB = Path.home() / ".local/lib/skeleton/home_edge"
if LOCAL_LIB.is_dir():
    sys.path.insert(0, str(LOCAL_LIB))
else:
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))

try:
    from media_display_ownership import live_decision, public_json  # type: ignore[import-not-found]
except ImportError:
    from core.home_edge.media_display_ownership import live_decision, public_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Home Edge media display ownership guard")
    parser.add_argument("command", choices=("status",))
    args = parser.parse_args()
    if args.command != "status":
        return 2
    decision = live_decision()
    print(public_json(decision))
    return decision.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
