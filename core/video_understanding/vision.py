from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from core.video_understanding.models import ProcessingMode, VideoUnderstandingError
from core.video_understanding.runtime_config import VideoRuntimeConfig
from core.video_understanding.subprocess_tools import BoundedCommandRunner, CommandRequest


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool


@dataclass(frozen=True)
class FrameArtifact:
    frame_id: str
    path: Path
    timestamp_seconds: float
    sha256: str
    perceptual_hash: int
    ocr_text: str
    ocr_provider: str

    def private_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "timestamp_seconds": self.timestamp_seconds,
            "sha256": self.sha256,
            "ocr_text": self.ocr_text,
            "ocr_provider": self.ocr_provider,
            "relative_path": self.path.name,
        }


def probe_media(
    runner: BoundedCommandRunner,
    config: VideoRuntimeConfig,
    media_path: Path,
    workspace: Path,
) -> MediaProbe:
    result = runner.require_success(
        CommandRequest(
            "ffprobe",
            (
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of",
                "json",
                str(media_path),
            ),
            workspace,
            timeout_seconds=min(120, config.limits.subprocess_timeout_seconds),
        ),
        reason_code="MEDIA_PROBE_FAILED",
    )
    try:
        payload = json.loads(result.stdout_text())
        duration = float(payload["format"]["duration"])
        streams = payload.get("streams", [])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoUnderstandingError("MEDIA_PROBE_INVALID", "ffprobe response is invalid") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > config.limits.max_duration_seconds:
        raise VideoUnderstandingError("MEDIA_DURATION_OUT_OF_BOUNDS", "media duration is outside configured bound")
    has_video = False
    has_audio = False
    width: int | None = None
    height: int | None = None
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") == "video":
            has_video = True
            raw_width, raw_height = stream.get("width"), stream.get("height")
            if isinstance(raw_width, int) and isinstance(raw_height, int):
                width, height = raw_width, raw_height
        if stream.get("codec_type") == "audio":
            has_audio = True
    return MediaProbe(duration, width, height, has_video, has_audio)


def select_frame_timestamps(
    duration_seconds: float,
    mode: ProcessingMode | str,
    *,
    scene_times: Iterable[float] = (),
    cue_times: Iterable[float] = (),
    max_frames: int,
) -> tuple[float, ...]:
    mode_value = ProcessingMode(mode)
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise VideoUnderstandingError("MEDIA_DURATION_INVALID", "duration must be positive")
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
        raise VideoUnderstandingError("FRAME_LIMIT_INVALID", "frame limit is invalid")
    if mode_value is ProcessingMode.QUICK:
        return ()
    target_count = {
        ProcessingMode.STANDARD: min(max_frames, max(6, round(duration_seconds / 90))),
        ProcessingMode.DEEP: min(max_frames, max(12, round(duration_seconds / 45))),
        ProcessingMode.TARGETED: min(max_frames, max(12, round(duration_seconds / 45))),
        ProcessingMode.ARCHIVE: min(max_frames, max(8, round(duration_seconds / 60))),
    }[mode_value]
    candidates: set[float] = set()
    if target_count == 1:
        candidates.add(duration_seconds / 2)
    else:
        for index in range(target_count):
            candidates.add(duration_seconds * index / max(1, target_count - 1))
    for value in (*tuple(scene_times), *tuple(cue_times)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        timestamp = float(value)
        if math.isfinite(timestamp) and 0 <= timestamp <= duration_seconds:
            candidates.add(timestamp)
    ordered = sorted(round(min(duration_seconds, max(0.0, value)), 3) for value in candidates)
    if len(ordered) <= max_frames:
        return tuple(ordered)
    priority = set(round(float(value), 3) for value in (*tuple(scene_times), *tuple(cue_times)) if isinstance(value, (int, float)) and not isinstance(value, bool))
    selected = [value for value in ordered if value in priority][:max_frames]
    if len(selected) < max_frames:
        remaining = [value for value in ordered if value not in selected]
        step = len(remaining) / max(1, max_frames - len(selected))
        selected.extend(remaining[min(len(remaining) - 1, int(index * step))] for index in range(max_frames - len(selected)))
    return tuple(sorted(set(selected))[:max_frames])


def extract_frames(
    runner: BoundedCommandRunner,
    config: VideoRuntimeConfig,
    media_path: Path,
    workspace: Path,
    timestamps: Sequence[float],
) -> tuple[Path, ...]:
    frame_dir = workspace / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for index, timestamp in enumerate(timestamps):
        output = frame_dir / f"frame-{index:04d}-{int(round(timestamp * 1000)):010d}.jpg"
        runner.require_success(
            CommandRequest(
                "ffmpeg",
                (
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(media_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1600:-2:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "3",
                    "-y",
                    str(output),
                ),
                workspace,
                timeout_seconds=min(120, config.limits.subprocess_timeout_seconds),
            ),
            reason_code="FRAME_EXTRACTION_FAILED",
        )
        if not output.is_file() or output.stat().st_size <= 0:
            raise VideoUnderstandingError("FRAME_EXTRACTION_EMPTY", "frame extraction produced no image")
        outputs.append(output)
    return tuple(outputs)


def build_frame_artifacts(
    runner: BoundedCommandRunner,
    config: VideoRuntimeConfig,
    paths: Sequence[Path],
    timestamps: Sequence[float],
    *,
    hamming_threshold: int = 5,
) -> tuple[FrameArtifact, ...]:
    if len(paths) != len(timestamps):
        raise VideoUnderstandingError("FRAME_TIMESTAMP_MISMATCH", "frame paths and timestamps differ")
    accepted: list[FrameArtifact] = []
    for path, timestamp in zip(paths, timestamps, strict=True):
        digest = sha256_file(path)
        perceptual = average_hash(path)
        if any(hamming_distance(perceptual, previous.perceptual_hash) <= hamming_threshold for previous in accepted):
            continue
        ocr_text = run_ocr(runner, config, path, path.parent.parent)
        frame_id = "frame:" + hashlib.sha256(f"{timestamp:.3f}:{digest}".encode("utf-8")).hexdigest()[:32]
        accepted.append(FrameArtifact(frame_id, path, float(timestamp), digest, perceptual, ocr_text, "local_ocr"))
    return tuple(accepted)


def run_ocr(
    runner: BoundedCommandRunner,
    config: VideoRuntimeConfig,
    image_path: Path,
    workspace: Path,
) -> str:
    languages = "+".join(config.ocr_languages)
    result = runner.run(
        CommandRequest(
            "ocr",
            (str(image_path), "stdout", "-l", languages, "--psm", "6"),
            workspace,
            timeout_seconds=min(120, config.limits.subprocess_timeout_seconds),
            max_output_bytes=min(
                config.limits.subprocess_output_bytes,
                max(4096, config.limits.max_ocr_chars_per_frame * 4),
            ),
        )
    )
    if result.returncode != 0:
        return ""
    text = result.stdout_text().strip()
    return text[: config.limits.max_ocr_chars_per_frame]


def average_hash(path: Path, *, size: int = 8) -> int:
    try:
        from PIL import Image
    except ImportError as exc:
        raise VideoUnderstandingError(
            "VISION_DEPENDENCY_MISSING",
            "video vision requires the video-understanding optional dependency",
        ) from exc
    try:
        with Image.open(path) as image:
            grayscale = image.convert("L").resize((size, size))
            getter = getattr(grayscale, "get_flattened_data", grayscale.getdata)
            values = list(getter())
    except (OSError, ValueError) as exc:
        raise VideoUnderstandingError("FRAME_DECODE_FAILED", "frame image could not be decoded") from exc
    average = sum(values) / len(values)
    result = 0
    for value in values:
        result = (result << 1) | int(value >= average)
    return result


def hamming_distance(first: int, second: int) -> int:
    return (first ^ second).bit_count()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
