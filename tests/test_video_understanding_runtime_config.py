from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig, load_runtime_config


def make_config(tmp_path: Path, *, transport: str = "private_bridge") -> VideoRuntimeConfig:
    local = tmp_path / "local"
    local.mkdir()
    media = local / "sample.mp4"
    media.write_bytes(b"media")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "artifacts",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "temp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": media},
        direct_media_allowed_hosts=("media.example.org",),
        executables={key: "/usr/bin/python3" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport=transport,
        ollama_model="synthetic",
        ollama_endpoint="http://127.0.0.1:11434" if transport == "loopback" else None,
        sona_endpoint="http://127.0.0.1:8080",
        limits=RuntimeLimits(subprocess_timeout_seconds=3, subprocess_output_bytes=8192, lease_seconds=30),
    )


def test_private_bridge_has_no_endpoint_and_public_summary_is_aggregate(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    summary = config.public_summary()
    assert summary["ollama_transport"] == "private_bridge"
    assert summary["local_media_count"] == 1
    assert "path" not in repr(summary).casefold()


def test_loopback_ollama_must_be_loopback(tmp_path: Path) -> None:
    config = make_config(tmp_path, transport="loopback")
    assert config.ollama_endpoint == "http://127.0.0.1:11434"
    with pytest.raises(VideoUnderstandingError):
        VideoRuntimeConfig(**{**config.__dict__, "ollama_endpoint": "http://example.org:11434"})


def test_local_reference_cannot_escape_approved_root(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert config.resolve_local_reference("local-media:abcdefghijklmnop").is_file()
    with pytest.raises(VideoUnderstandingError) as exc:
        config.resolve_local_reference("local-media:zzzzzzzzzzzzzzzz")
    assert exc.value.reason_code == "LOCAL_MEDIA_NOT_REGISTERED"


def test_runtime_config_json_load_and_unknown_field_rejection(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    payload = {
        "artifact_root": str(config.artifact_root),
        "queue_root": str(config.queue_root),
        "temp_root": str(config.temp_root),
        "approved_local_roots": [str(value) for value in config.approved_local_roots],
        "local_media_registry": {key: str(value) for key, value in config.local_media_registry.items()},
        "direct_media_allowed_hosts": list(config.direct_media_allowed_hosts),
        "executables": dict(config.executables),
        "ollama_transport": "private_bridge",
        "ollama_model": "synthetic",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    assert load_runtime_config(path).ollama_transport == "private_bridge"
    payload["unexpected"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(VideoUnderstandingError) as exc:
        load_runtime_config(path)
    assert exc.value.reason_code == "UNKNOWN_RUNTIME_CONFIG_FIELD"


def test_video_understanding_extra_pins_pillow() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["optional-dependencies"]["video-understanding"] == [
        "pillow==12.2.0"
    ]
