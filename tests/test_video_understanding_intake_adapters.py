from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.video_understanding.intake_adapters import DirectMediaAdapter, LocalFileAdapter, YtDlpAdapter
from core.video_understanding.models import ProcessingMode, VideoUnderstandingError
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from core.video_understanding.subprocess_tools import CommandResult
from core.video_understanding.url_classifier import classify_remote_url


def config(tmp_path: Path) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir()
    source_file = local / "sample.mp4"; source_file.write_bytes(b"private-media")
    return VideoRuntimeConfig(
        artifact_root=tmp_path / "artifacts",
        queue_root=tmp_path / "queue",
        temp_root=tmp_path / "temp",
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source_file},
        direct_media_allowed_hosts=("media.example.org",),
        executables={key: f"/{key}" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="synthetic",
        limits=RuntimeLimits(max_download_bytes=1024 * 1024, subprocess_timeout_seconds=10, subprocess_output_bytes=8192, lease_seconds=30),
    )


class Runner:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.requests = []

    def require_success(self, request, *, reason_code):
        del reason_code
        self.requests.append(request)
        if "--dump-single-json" in request.args:
            data = json.dumps({"title": "Synthetic", "duration": 60, "uploader": "Private", "webpage_url": "https://youtu.be/AbCdEf12345"}).encode()
            return CommandResult(("yt-dlp",), 0, data, b"", 0)
        if "--merge-output-format" in request.args:
            (self.workspace / "source.mkv").write_bytes(b"media")
        return CommandResult(("yt-dlp",), 0, b"", b"", 0)

    def run(self, request):
        self.requests.append(request)
        (self.workspace / "subtitle.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n")
        return CommandResult(("yt-dlp",), 0, b"", b"", 0)


def test_ytdlp_commands_are_fixed_and_user_arguments_cannot_enter(tmp_path: Path) -> None:
    cfg = config(tmp_path); workspace = tmp_path / "ws"; workspace.mkdir(); runner = Runner(workspace)
    source = classify_remote_url("https://youtu.be/AbCdEf12345")
    acquired = YtDlpAdapter(cfg, runner).acquire(source, workspace, ProcessingMode.STANDARD)
    assert acquired.media_path and acquired.subtitle_paths
    for request in runner.requests:
        assert "--ignore-config" in request.args
        assert request.args[-2] == "--"
        assert "cookie" not in " ".join(request.args).casefold()


def test_local_adapter_uses_opaque_registry_and_copies_immutably(tmp_path: Path) -> None:
    cfg = config(tmp_path); workspace = tmp_path / "ws"; workspace.mkdir()
    acquired = LocalFileAdapter(cfg).acquire("local-media:abcdefghijklmnop", workspace, ProcessingMode.STANDARD)
    assert acquired.media_path.read_bytes() == b"private-media"
    assert acquired.media_path != cfg.resolve_local_reference("local-media:abcdefghijklmnop")
    with pytest.raises(VideoUnderstandingError):
        LocalFileAdapter(cfg).acquire("/tmp/private.mp4", workspace, ProcessingMode.STANDARD)


def test_direct_media_requires_allowlisted_host_and_bounded_downloader(tmp_path: Path) -> None:
    cfg = config(tmp_path); workspace = tmp_path / "ws"; workspace.mkdir()

    def downloader(url, target, _config):
        assert url.startswith("https://media.example.org/")
        target.write_bytes(b"direct")
        return "video/mp4", 6

    acquired = DirectMediaAdapter(cfg, downloader=downloader).acquire("https://media.example.org/demo.mp4", workspace, ProcessingMode.STANDARD)
    assert acquired.source_sha256
    with pytest.raises(VideoUnderstandingError) as exc:
        DirectMediaAdapter(cfg, downloader=downloader).acquire("https://other.example.org/demo.mp4", workspace, ProcessingMode.STANDARD)
    assert exc.value.reason_code == "DIRECT_MEDIA_HOST_NOT_ALLOWED"
