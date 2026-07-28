from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_install import (
    PILLOW_VERSION,
    VIBE_SHA256,
    VIBE_VERSION,
    YT_DLP_SHA256,
    YT_DLP_VERSION,
    _download_exact,
    _install_units,
    _runtime_config_payload,
    runtime_layout,
)


def test_runtime_layout_stays_under_user_private_roots(tmp_path: Path) -> None:
    layout = runtime_layout(tmp_path)
    assert layout.base.is_relative_to(tmp_path / ".local" / "share")
    assert layout.state.is_relative_to(tmp_path / ".local" / "state")
    assert layout.config_file.is_relative_to(tmp_path / ".config")
    assert layout.systemd_dir.is_relative_to(tmp_path / ".config")


def test_runtime_assets_are_exactly_versioned_and_hashed() -> None:
    assert YT_DLP_VERSION == "2026.06.09"
    assert VIBE_VERSION == "3.0.19"
    assert PILLOW_VERSION == "12.2.0"
    assert len(YT_DLP_SHA256) == len(VIBE_SHA256) == 64
    assert all(character in "0123456789abcdef" for character in YT_DLP_SHA256)
    assert all(character in "0123456789abcdef" for character in VIBE_SHA256)


def test_runtime_config_keeps_network_and_authority_bounded(tmp_path: Path) -> None:
    layout = runtime_layout(tmp_path)
    payload = _runtime_config_payload(
        layout,
        mandatory={
            "yt_dlp": "/private/bin/yt-dlp",
            "ffmpeg": "/usr/bin/ffmpeg",
            "ffprobe": "/usr/bin/ffprobe",
            "ocr": "/usr/bin/tesseract",
        },
        sona_executable="/private/bin/vibe",
        ollama_model="private-model",
    )
    assert payload["direct_media_allowed_hosts"] == []
    assert payload["ollama_endpoint"] == "http://127.0.0.1:11434"
    assert payload["sona_endpoint"] == "http://127.0.0.1:3022"
    assert payload["ollama_transport"] == "loopback"
    assert "database" not in payload
    assert "sqlite" not in json.dumps(payload).casefold()


def test_exact_download_rejects_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    payload = b"synthetic-asset"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            del size
            if payload_holder:
                return payload_holder.pop()
            return b""

    payload_holder = [payload]
    monkeypatch.setattr(
        "core.video_understanding.runtime_install.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )
    target = tmp_path / "asset"
    with pytest.raises(VideoUnderstandingError) as exc:
        _download_exact(
            "https://example.invalid/asset",
            target,
            "0" * 64,
            max_bytes=1024,
        )
    assert exc.value.reason_code == "RUNTIME_ASSET_HASH_MISMATCH"
    assert not target.exists()


def test_exact_download_promotes_verified_bytes(tmp_path: Path, monkeypatch) -> None:
    payload = b"synthetic-asset"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            del size
            if payload_holder:
                return payload_holder.pop()
            return b""

    payload_holder = [payload]
    monkeypatch.setattr(
        "core.video_understanding.runtime_install.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )
    target = tmp_path / "asset"
    _download_exact(
        "https://example.invalid/asset",
        target,
        hashlib.sha256(payload).hexdigest(),
        max_bytes=1024,
    )
    assert target.read_bytes() == payload


def test_units_use_fixed_worker_and_no_shell(tmp_path: Path) -> None:
    layout = runtime_layout(tmp_path)
    for path in (
        layout.venv / "bin",
        layout.systemd_dir,
        layout.config_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (layout.venv / "bin" / "python").write_text("", encoding="utf-8")
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    private_memory = tmp_path / "private-memory"
    private_memory.mkdir()
    layout.config_file.write_text(
        json.dumps({"executables": {"sona": "/private/bin/vibe"}}),
        encoding="utf-8",
    )
    _install_units(
        layout,
        release=release,
        private_memory_root=private_memory,
        source_sha="a" * 40,
        sona_enabled=True,
    )
    worker = (layout.systemd_dir / "skeleton-video-understanding-worker.service").read_text()
    sona = (layout.systemd_dir / "skeleton-video-understanding-sona.service").read_text()
    assert "--forever" in worker
    assert "hetzner-video-worker-1" in worker
    assert "SKELETON_PRIVATE_MEMORY_ROOT=" in worker
    assert "ExecStart=/bin/sh" not in worker
    assert "ExecStart=/bin/bash" not in worker
    assert " --server" in sona


def test_install_module_has_no_direct_sqlite_or_arbitrary_shell() -> None:
    source = Path("core/video_understanding/runtime_install.py").read_text(
        encoding="utf-8"
    ).casefold()
    assert "import sqlite3" not in source
    assert "shell=true" not in source
    assert "os.system" not in source
