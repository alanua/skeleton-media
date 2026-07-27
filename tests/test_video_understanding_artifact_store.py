from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.video_understanding.artifact_store import PrivateArtifactStore
from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig


def config(tmp_path: Path) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir(); source = local / "x"; source.write_bytes(b"x")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "art",
        queue_root=tmp_path / "q",
        temp_root=tmp_path / "tmp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: f"/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="m",
        limits=RuntimeLimits(lease_seconds=30),
    )


def unlock(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def test_atomic_finalize_readback_and_replay(tmp_path: Path) -> None:
    store = PrivateArtifactStore(config(tmp_path))
    with store.workspace("vr_synthetic", "r1") as workspace:
        (workspace / "summary.json").write_text('{"ok":true}')
        first = store.finalize(workspace, video_record_id="vr_synthetic", processing_revision="r1", mode="STANDARD")
    assert first.replay is False and (first.root / "manifest.json").is_file()
    manifest = json.loads((first.root / "manifest.json").read_text())
    unlock(first.root)
    with store.workspace("vr_synthetic", "r1") as workspace:
        (workspace / "summary.json").write_text('{"ok":true}')
        replay = store.finalize(workspace, video_record_id="vr_synthetic", processing_revision="r1", mode="STANDARD")
    assert replay.replay is True and replay.manifest_hash == manifest["manifest_hash"]
    unlock(first.root)


def test_conflict_never_overwrites_existing_revision(tmp_path: Path) -> None:
    store = PrivateArtifactStore(config(tmp_path))
    with store.workspace("vr_synthetic", "r1") as workspace:
        (workspace / "a.txt").write_text("one")
        final = store.finalize(workspace, video_record_id="vr_synthetic", processing_revision="r1", mode="STANDARD")
    unlock(final.root)
    with store.workspace("vr_synthetic", "r1") as workspace:
        (workspace / "a.txt").write_text("two")
        with pytest.raises(VideoUnderstandingError) as exc:
            store.finalize(workspace, video_record_id="vr_synthetic", processing_revision="r1", mode="STANDARD")
    assert exc.value.reason_code == "ARTIFACT_REVISION_CONFLICT"
    assert (final.root / "a.txt").read_text() == "one"
    unlock(final.root)


def test_readback_detects_unexpected_file(tmp_path: Path) -> None:
    store = PrivateArtifactStore(config(tmp_path))
    with store.workspace("vr_synthetic", "r1") as workspace:
        (workspace / "a.txt").write_text("one")
        final = store.finalize(workspace, video_record_id="vr_synthetic", processing_revision="r1", mode="STANDARD")
    unlock(final.root)
    (final.root / "unexpected.txt").write_text("bad")
    with store.workspace("vr_synthetic", "r1") as workspace:
        (workspace / "a.txt").write_text("one")
        with pytest.raises(VideoUnderstandingError) as exc:
            store.finalize(workspace, video_record_id="vr_synthetic", processing_revision="r1", mode="STANDARD")
    assert exc.value.reason_code == "ARTIFACT_READBACK_FAILED"
    unlock(final.root)
