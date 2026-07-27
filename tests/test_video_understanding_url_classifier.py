from __future__ import annotations

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.url_classifier import classify_local_reference, classify_remote_url


def test_youtube_variants_normalize_without_network() -> None:
    first = classify_remote_url("https://youtu.be/AbCdEf12345")
    second = classify_remote_url("https://www.youtube.com/watch?v=AbCdEf12345&utm_source=test")
    assert first.adapter == second.adapter == "youtube"
    assert first.normalized_private_source == second.normalized_private_source
    assert first.source_token == second.source_token


def test_vimeo_and_direct_media_are_supported() -> None:
    assert classify_remote_url("https://vimeo.com/123456789").adapter == "vimeo"
    assert classify_remote_url("https://media.example.org/training/demo.mp4").adapter == "direct_media"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/video.mp4",
        "https://localhost/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/video.mp4",
        "https://2130706433/video.mp4",
        "https://0x7f000001/video.mp4",
        "https://0177.0.0.1/video.mp4",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://user:pass@example.org/video.mp4",
        "https://example.org/video.mp4#fragment",
        "https://example.org:8443/video.mp4",
    ],
)
def test_unsafe_remote_targets_are_rejected(url: str) -> None:
    with pytest.raises(VideoUnderstandingError):
        classify_remote_url(url)


def test_local_files_use_opaque_reference_not_path() -> None:
    result = classify_local_reference("local-media:abcdefghijklmnop")
    assert result.adapter == "local_file"
    with pytest.raises(VideoUnderstandingError):
        classify_local_reference("/home/private/video.mp4")
