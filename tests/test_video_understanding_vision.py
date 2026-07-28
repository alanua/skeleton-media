from __future__ import annotations

from pathlib import Path

import pytest

from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from core.video_understanding.subprocess_tools import CommandResult
from core.video_understanding.vision import average_hash, build_frame_artifacts, hamming_distance, select_frame_timestamps


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
        limits=RuntimeLimits(max_frames=10, subprocess_output_bytes=8192, lease_seconds=30),
    )


class Runner:
    def run(self, request):
        del request
        return CommandResult(("ocr",), 0, b"VISIBLE TEXT", b"", 0)


def test_frame_selection_is_deterministic_and_bounded() -> None:
    first = select_frame_timestamps(600, "DEEP", scene_times=(10, 20), cue_times=(30,), max_frames=10)
    assert first == select_frame_timestamps(600, "DEEP", scene_times=(10, 20), cue_times=(30,), max_frames=10)
    assert len(first) <= 10 and 10 in first


def test_perceptual_dedup_and_ocr_evidence(tmp_path: Path) -> None:
    Image = pytest.importorskip("PIL.Image")
    cfg = config(tmp_path); workspace = tmp_path / "ws"; frames = workspace / "frames"; frames.mkdir(parents=True)
    first = frames / "a.png"; second = frames / "b.png"; third = frames / "c.png"
    Image.new("RGB", (8, 8), (10, 10, 10)).save(first)
    Image.new("RGB", (8, 8), (10, 10, 10)).save(second)
    patterned = Image.new("RGB", (8, 8), (0, 0, 0))
    for x in range(4):
        for y in range(8):
            patterned.putpixel((x, y), (255, 255, 255))
    patterned.save(third)
    result = build_frame_artifacts(Runner(), cfg, (first, second, third), (0, 1, 2), hamming_threshold=0)
    assert len(result) == 2
    assert result[0].ocr_text == "VISIBLE TEXT"
    assert hamming_distance(average_hash(first), average_hash(second)) == 0
