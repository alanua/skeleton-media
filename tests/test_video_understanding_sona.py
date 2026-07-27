from __future__ import annotations

from pathlib import Path

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from core.video_understanding.sona_backend import SonaBackend, SonaProcessManager


def config(tmp_path: Path) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir(); source = local / "x"; source.write_bytes(b"x")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "art",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "temp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: f"/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="m",
        sona_start_args=("--host", "127.0.0.1", "--port", "8080"),
        limits=RuntimeLimits(lease_seconds=30),
    )


def test_sona_parses_timestamped_segments(tmp_path: Path) -> None:
    cfg = config(tmp_path); audio = tmp_path / "audio.wav"; audio.write_bytes(b"RIFF" + b"x" * 100)
    backend = SonaBackend(cfg, requester=lambda *args: {"language": "uk", "segments": [{"start": 0, "end": 1, "text": "test", "confidence": .8}]})
    result = backend.transcribe(audio)
    assert result.language == "uk" and result.segments[0].text == "test"


class Proc:
    def __init__(self): self.stopped = False
    def poll(self): return None if not self.stopped else 0
    def terminate(self): self.stopped = True
    def kill(self): self.stopped = True
    def wait(self, timeout=None): self.stopped = True; return 0


def test_sona_manager_never_stops_external_process(tmp_path: Path) -> None:
    cfg = config(tmp_path); called = []
    manager = SonaProcessManager(cfg, process_factory=lambda *args, **kwargs: called.append(1), readiness_probe=lambda endpoint: True)
    assert manager.ensure_ready(start_owned=True) is False
    manager.stop()
    assert called == []


def test_sona_manager_stops_only_owned_process(tmp_path: Path) -> None:
    cfg = config(tmp_path); process = Proc(); probes = iter([False, True])
    manager = SonaProcessManager(cfg, process_factory=lambda *args, **kwargs: process, readiness_probe=lambda endpoint: next(probes), sleep=lambda _: None)
    assert manager.ensure_ready(start_owned=True, wait_seconds=1) is True
    assert manager.owns_process is True
    manager.stop(); assert process.stopped is True


def test_sona_invalid_response_fails_closed(tmp_path: Path) -> None:
    cfg = config(tmp_path); audio = tmp_path / "a.wav"; audio.write_bytes(b"RIFFxxxx")
    with pytest.raises(VideoUnderstandingError):
        SonaBackend(cfg, requester=lambda *args: {"segments": "bad"}).transcribe(audio)
