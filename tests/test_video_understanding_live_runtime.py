from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.private_memory_stack import PrivateMemoryStack
from core.video_understanding.live_runtime import (
    build_live_runtime,
    doctor_live_runtime,
    resolve_existing_private_memory_root,
    synthetic_memory_roundtrip,
)
from core.video_understanding.models import VideoUnderstandingError


def _runtime_config(tmp_path: Path) -> Path:
    local = tmp_path / "local-media"
    local.mkdir()
    payload = {
        "artifact_root": str(tmp_path / "artifacts"),
        "queue_root": str(tmp_path / "queue"),
        "temp_root": str(tmp_path / "tmp"),
        "approved_local_roots": [str(local)],
        "local_media_registry": {},
        "direct_media_allowed_hosts": [],
        "executables": {
            "yt_dlp": "/usr/bin/true",
            "ffmpeg": "/usr/bin/true",
            "ffprobe": "/usr/bin/true",
            "sona": "/usr/bin/false",
            "ocr": "/usr/bin/true",
        },
        "ollama_transport": "loopback",
        "ollama_model": "synthetic-model",
        "ollama_endpoint": "http://127.0.0.1:11434",
        "sona_endpoint": "http://127.0.0.1:3022",
        "sona_model": "default",
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _memory_root(tmp_path: Path) -> Path:
    root = tmp_path / "private-memory"
    PrivateMemoryStack(root).init(import_manifest=False)
    return root


def test_existing_canonical_root_is_required_and_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(VideoUnderstandingError) as exc:
        resolve_existing_private_memory_root(
            {"SKELETON_PRIVATE_MEMORY_ROOT": str(missing)}
        )
    assert exc.value.reason_code in {
        "CANONICAL_MEMORY_NOT_FOUND",
        "FileNotFoundError",
    }
    assert not missing.exists()


def test_non_stack_connector_database_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "embodied-memory.sqlite3"
    db.write_bytes(b"not-a-stack")
    config = tmp_path / "private.json"
    config.write_text(
        json.dumps(
            {
                "schema": "skeleton.private_memory.config.v0",
                "database": {"path": str(db)},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(VideoUnderstandingError) as exc:
        resolve_existing_private_memory_root(
            {"SKELETON_PRIVATE_MEMORY_CONFIG": str(config)}
        )
    assert exc.value.reason_code == "CANONICAL_STACK_ROOT_REQUIRED"


def test_live_runtime_uses_existing_stack_and_memory_gateway(tmp_path: Path) -> None:
    root = _memory_root(tmp_path)
    runtime = build_live_runtime(
        _runtime_config(tmp_path),
        processing_revision="a" * 40,
        env={"SKELETON_PRIVATE_MEMORY_ROOT": str(root)},
    )
    assert runtime.private_memory_root == root.resolve()
    assert runtime.gateway is not None
    assert runtime.queue.counts()["pending"] == 0


def test_synthetic_memory_roundtrip_is_authoritative_and_idempotent(tmp_path: Path) -> None:
    root = _memory_root(tmp_path)
    runtime = build_live_runtime(
        _runtime_config(tmp_path),
        processing_revision="b" * 40,
        env={"SKELETON_PRIVATE_MEMORY_ROOT": str(root)},
    )
    first = synthetic_memory_roundtrip(
        runtime,
        approval_ref="operator.video.runtime.test",
    )
    second = synthetic_memory_roundtrip(
        runtime,
        approval_ref="operator.video.runtime.test",
    )
    assert first == second == {
        "schema": "skeleton.video_understanding.memory_roundtrip.v1",
        "status": "DONE",
        "mutation_committed": True,
        "exact_read_authoritative": True,
    }


def test_doctor_failure_is_aggregate_only(tmp_path: Path) -> None:
    report = doctor_live_runtime(
        _runtime_config(tmp_path),
        processing_revision="c" * 40,
        env={},
    )
    assert report["status"] == "BLOCKED"
    rendered = json.dumps(report)
    assert str(tmp_path) not in rendered
    assert "synthetic-model" not in rendered
    assert report["stable_reason_codes"] == ["CANONICAL_MEMORY_ROOT_REQUIRED"]


def test_live_runtime_module_has_no_direct_sqlite(tmp_path: Path) -> None:
    del tmp_path
    source = Path("core/video_understanding/live_runtime.py").read_text(
        encoding="utf-8"
    ).casefold()
    assert "import sqlite3" not in source
    assert ".sqlite3" not in source
