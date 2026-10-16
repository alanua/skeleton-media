from __future__ import annotations

import json
from pathlib import Path

import pytest

from skeleton_media.video_understanding.intake_adapters import DirectMediaAdapter, LocalFileAdapter, YtDlpAdapter
from skeleton_media.video_understanding.models import ProcessingMode, VideoUnderstandingError
from skeleton_media.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from skeleton_media.video_understanding.subprocess_tools import CommandResult
from skeleton_media.video_understanding.url_classifier import classify_remote_url


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


def _range_cfg(tmp_path: Path):
    from dataclasses import replace

    original = config(tmp_path)
    cfg = replace(original, limits=replace(original.limits, max_download_bytes=4 * 1024 * 1024))
    workspace = cfg.temp_root / "range-fixture"
    workspace.mkdir(parents=True)
    return cfg, workspace


def _public_video_metadata(*, url="https://r1---sn-abc.googlevideo.com/videoplayback?expire=1", public=True):
    return {
        "availability": "public" if public else "needs_auth",
        "is_live": False,
        "live_status": "not_live",
        "age_limit": 0,
        "formats": [{
            "format_id": "18", "protocol": "https", "ext": "mp4",
            "height": 360, "vcodec": "avc1", "acodec": "mp4a",
            "has_drm": False, "filesize_approx": 2 * 1024 * 1024,
            "url": url,
        }],
    }


def test_public_youtube_range_fallback_downloads_exact_bytes_atomically(tmp_path: Path) -> None:
    from skeleton_media.video_understanding.intake_adapters import (
        _RangeChunk, download_youtube_progressive_ranges,
    )

    cfg, workspace = _range_cfg(tmp_path)
    target = workspace / "source.mp4"
    requests = []

    def fetch(_url, start, end):
        requests.append((start, end))
        block = bytes([65 + len(requests)]) * (end - start + 1)
        return _RangeChunk(206, f"bytes {start}-{end}/{2 * 1024 * 1024}", "video/mp4", block)

    total = download_youtube_progressive_ranges(
        _public_video_metadata(), target, cfg, fetch_chunk=fetch,
    )
    assert total == 2 * 1024 * 1024
    assert requests == [(0, 1048575), (1048576, 2097151)]
    assert target.read_bytes() == b"B" * 1048576 + b"C" * 1048576
    assert not (workspace / "source.range-partial").exists()


@pytest.mark.parametrize(
    "malicious_url",
    [
        "https://localhost/videoplayback", "http://r1.googlevideo.com/stream",
        "https://r1.googlevideo.com.evil.test/stream",
        "https://user@r1.googlevideo.com/stream",
        "https://r1.googlevideo.com:444/stream",
        "https://r1.googlevideo.com/stream#secret",
    ],
)
def test_youtube_range_fallback_never_requests_untrusted_cdn(tmp_path: Path, malicious_url: str) -> None:
    from skeleton_media.video_understanding.intake_adapters import download_youtube_progressive_ranges

    cfg, workspace = _range_cfg(tmp_path)
    calls = []

    def unexpected(*args):
        calls.append(args)
        raise AssertionError("never invoke network for untrusted URL")

    with pytest.raises(VideoUnderstandingError) as error:
        download_youtube_progressive_ranges(
            _public_video_metadata(url=malicious_url),
            workspace / "source.mp4", cfg, fetch_chunk=unexpected,
        )
    assert error.value.reason_code == "SOURCE_RANGE_FORMAT_MISSING"
    assert calls == []
    assert not list(workspace.iterdir())


@pytest.mark.parametrize("private_change", [
    {"availability": "needs_auth"}, {"is_live": True}, {"live_status": "is_live"},
    {"age_limit": 18},
])
def test_youtube_range_fallback_denies_nonpublic_or_live_sources(tmp_path: Path, private_change) -> None:
    from skeleton_media.video_understanding.intake_adapters import download_youtube_progressive_ranges

    cfg, workspace = _range_cfg(tmp_path)
    payload = {**_public_video_metadata(), **private_change}
    with pytest.raises(VideoUnderstandingError) as error:
        download_youtube_progressive_ranges(payload, workspace / "source.mp4", cfg, fetch_chunk=lambda *_: None)
    assert error.value.reason_code == "SOURCE_RANGE_NOT_PUBLIC"
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("failure_case", [
    "wrong_status", "wrong_start", "wrong_total", "incomplete", "mime", "oversized",
])
def test_youtube_range_fallback_fails_closed_and_removes_partial(tmp_path: Path, failure_case: str) -> None:
    from skeleton_media.video_understanding.intake_adapters import (
        _RangeChunk, download_youtube_progressive_ranges,
    )

    cfg, workspace = _range_cfg(tmp_path)
    target = workspace / "source.mp4"
    counter = [0]

    def fetch(_url, start, end):
        counter[0] += 1
        if counter[0] == 1:
            return _RangeChunk(206, f"bytes {start}-{end}/{2 * 1048576}", "video/mp4", b"A" * 1048576)
        if failure_case == "wrong_status":
            return _RangeChunk(200, f"bytes {start}-{end}/{2 * 1048576}", "video/mp4", b"B" * 1048576)
        if failure_case == "wrong_start":
            return _RangeChunk(206, f"bytes {start+1}-{end}/{2 * 1048576}", "video/mp4", b"B" * 1048576)
        if failure_case == "wrong_total":
            return _RangeChunk(206, f"bytes {start}-{end}/{3 * 1048576}", "video/mp4", b"B" * 1048576)
        if failure_case == "incomplete":
            return _RangeChunk(206, f"bytes {start}-{end}/{2 * 1048576}", "video/mp4", b"B" * 10)
        if failure_case == "mime":
            return _RangeChunk(206, f"bytes {start}-{end}/{2 * 1048576}", "text/html", b"B" * 1048576)
        return _RangeChunk(206, f"bytes {start}-{end}/{5 * 1048576}", "video/mp4", b"B" * 1048576)

    with pytest.raises(VideoUnderstandingError):
        download_youtube_progressive_ranges(
            _public_video_metadata(), target, cfg, fetch_chunk=fetch,
        )
    assert not target.exists()
    assert not (workspace / "source.range-partial").exists()


def test_youtube_range_fallback_denies_workspace_escape_and_existing_target(tmp_path: Path) -> None:
    from skeleton_media.video_understanding.intake_adapters import download_youtube_progressive_ranges

    cfg, workspace = _range_cfg(tmp_path)
    with pytest.raises(VideoUnderstandingError) as outside:
        download_youtube_progressive_ranges(_public_video_metadata(), tmp_path / "source.mp4", cfg)
    assert outside.value.reason_code == "SOURCE_RANGE_WORKSPACE_DENIED"
    target = workspace / "source.mp4"
    target.write_bytes(b"owned-existing-data")
    with pytest.raises(VideoUnderstandingError) as collision:
        download_youtube_progressive_ranges(_public_video_metadata(), target, cfg)
    assert collision.value.reason_code == "SOURCE_RANGE_TARGET_UNSAFE"
    assert target.read_bytes() == b"owned-existing-data"


def test_youtube_adapter_uses_one_existing_pipeline_on_media_403(tmp_path: Path, monkeypatch) -> None:
    from skeleton_media.video_understanding import intake_adapters

    cfg, _workspace = _range_cfg(tmp_path)
    workspace = cfg.temp_root / "adapter"
    workspace.mkdir()
    source = classify_remote_url("https://youtu.be/AbCdEf12345")
    calls = []

    class Media403Runner:
        def require_success(self, request, *, reason_code):
            if "--dump-single-json" in request.args:
                metadata = {**_public_video_metadata(), "title": "Synthetic", "duration": 60}
                return CommandResult(("yt-dlp",), 0, json.dumps(metadata).encode(), b"", 0)
            assert "--merge-output-format" in request.args
            calls.append("yt_dlp_attempt")
            raise VideoUnderstandingError(reason_code, "synthetic stream 403")

        def run(self, request):
            (workspace / "subtitle.uk.vtt").write_text("WEBVTT\n", encoding="utf-8")
            return CommandResult(("yt-dlp",), 0, b"", b"", 0)

    def fake_download(metadata, target, config):
        assert target == workspace / "source.mp4"
        assert config is cfg and metadata["availability"] == "public"
        calls.append("range_fallback")
        target.write_bytes(b"synthetic-original-frame")
        return target.stat().st_size

    monkeypatch.setattr(intake_adapters, "download_youtube_progressive_ranges", fake_download)
    acquired = YtDlpAdapter(cfg, Media403Runner()).acquire(source, workspace, ProcessingMode.STANDARD)
    assert calls == ["yt_dlp_attempt", "range_fallback"]
    assert acquired.media_path == workspace / "source.mp4"
    assert len(acquired.subtitle_paths) == 1
    assert acquired.source_sha256


def test_youtube_adapter_does_not_fallback_for_other_errors_or_vimeo(tmp_path: Path, monkeypatch) -> None:
    from skeleton_media.video_understanding import intake_adapters
    cfg, _workspace = _range_cfg(tmp_path)
    workspace = cfg.temp_root / "adapter"; workspace.mkdir()
    monkeypatch.setattr(
        intake_adapters, "download_youtube_progressive_ranges",
        lambda *_args: pytest.fail("source fallback must not run"),
    )

    class RejectRunner:
        def require_success(self, request, *, reason_code):
            if "--dump-single-json" in request.args:
                metadata = {**_public_video_metadata(), "title": "Synthetic", "duration": 60}
                return CommandResult(("yt-dlp",), 0, json.dumps(metadata).encode(), b"", 0)
            raise VideoUnderstandingError("COMMAND_TIMEOUT", "synthetic timeout")

        def run(self, request):
            return CommandResult(("yt-dlp",), 0, b"", b"", 0)

    with pytest.raises(VideoUnderstandingError) as error:
        YtDlpAdapter(cfg, RejectRunner()).acquire(
            classify_remote_url("https://youtu.be/AbCdEf12345"), workspace, ProcessingMode.STANDARD,
        )
    assert error.value.reason_code == "COMMAND_TIMEOUT"

    class RejectVimeo(RejectRunner):
        def require_success(self, request, *, reason_code):
            if "--dump-single-json" in request.args:
                return super().require_success(request, reason_code=reason_code)
            raise VideoUnderstandingError("SOURCE_MEDIA_FAILED", "synthetic HTTP 403")

    with pytest.raises(VideoUnderstandingError) as vimeo:
        YtDlpAdapter(cfg, RejectVimeo()).acquire(
            classify_remote_url("https://vimeo.com/123456789"), workspace, ProcessingMode.STANDARD,
        )
    assert vimeo.value.reason_code == "SOURCE_MEDIA_FAILED"
