from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.video_understanding.models import VideoUnderstandingError


_TIMESTAMP_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    language: str
    provider: str
    confidence: float | None = None
    provenance: str = "subtitle"

    def __post_init__(self) -> None:
        start = _number(self.start_seconds, "start_seconds")
        end = _number(self.end_seconds, "end_seconds")
        if start < 0 or end < start:
            raise VideoUnderstandingError("TRANSCRIPT_TIMESTAMP_INVALID", "transcript timestamp is invalid")
        cleaned = normalize_text(self.text)
        if not cleaned:
            raise VideoUnderstandingError("TRANSCRIPT_TEXT_EMPTY", "transcript segment is empty")
        if len(cleaned) > 20_000:
            raise VideoUnderstandingError("TRANSCRIPT_SEGMENT_TOO_LARGE", "transcript segment is too large")
        if not isinstance(self.language, str) or not self.language or len(self.language) > 32:
            raise VideoUnderstandingError("TRANSCRIPT_LANGUAGE_INVALID", "transcript language is invalid")
        if not isinstance(self.provider, str) or not self.provider or len(self.provider) > 128:
            raise VideoUnderstandingError("TRANSCRIPT_PROVIDER_INVALID", "transcript provider is invalid")
        if self.confidence is not None:
            confidence = _number(self.confidence, "confidence")
            if not 0 <= confidence <= 1:
                raise VideoUnderstandingError("TRANSCRIPT_CONFIDENCE_INVALID", "transcript confidence is invalid")
            object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "start_seconds", start)
        object.__setattr__(self, "end_seconds", end)
        object.__setattr__(self, "text", cleaned)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptQuality:
    status: str
    reason_codes: tuple[str, ...]
    segment_count: int
    character_count: int
    duration_seconds: float
    covered_seconds: float
    coverage_ratio: float

    @property
    def usable(self) -> bool:
        return self.status == "GOOD"


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise VideoUnderstandingError("TRANSCRIPT_TEXT_INVALID", "transcript text must be a string")
    cleaned = html.unescape(_TAG_RE.sub(" ", value)).replace("\u200b", " ")
    return _SPACE_RE.sub(" ", cleaned).strip()


def parse_transcript(path: Path, *, language: str, provider: str) -> tuple[TranscriptSegment, ...]:
    suffix = path.suffix.casefold()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise VideoUnderstandingError("TRANSCRIPT_READ_FAILED", "transcript could not be read") from exc
    if suffix == ".vtt":
        segments = parse_vtt(text, language=language, provider=provider)
    elif suffix == ".srt":
        segments = parse_srt(text, language=language, provider=provider)
    elif suffix in {".json", ".jsonl"}:
        segments = parse_json_transcript(text, language=language, provider=provider)
    else:
        raise VideoUnderstandingError("TRANSCRIPT_FORMAT_UNSUPPORTED", "transcript format is unsupported")
    return normalize_segments(segments)


def parse_vtt(text: str, *, language: str, provider: str) -> tuple[TranscriptSegment, ...]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[TranscriptSegment] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start_raw, end_raw = [part.strip().split(" ", 1)[0] for part in line.split("-->", 1)]
        start = parse_timestamp(start_raw)
        end = parse_timestamp(end_raw)
        index += 1
        payload: list[str] = []
        while index < len(lines) and lines[index].strip():
            payload.append(lines[index])
            index += 1
        cleaned = normalize_text(" ".join(payload))
        if cleaned:
            segments.append(TranscriptSegment(start, end, cleaned, language, provider))
        index += 1
    return tuple(segments)


def parse_srt(text: str, *, language: str, provider: str) -> tuple[TranscriptSegment, ...]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start_raw, end_raw = [part.strip().split(" ", 1)[0] for part in lines[timing_index].split("-->", 1)]
        cleaned = normalize_text(" ".join(lines[timing_index + 1 :]))
        if cleaned:
            segments.append(
                TranscriptSegment(
                    parse_timestamp(start_raw),
                    parse_timestamp(end_raw),
                    cleaned,
                    language,
                    provider,
                )
            )
    return tuple(segments)


def parse_json_transcript(text: str, *, language: str, provider: str) -> tuple[TranscriptSegment, ...]:
    try:
        if text.lstrip().startswith("["):
            values = json.loads(text)
        else:
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise VideoUnderstandingError("TRANSCRIPT_JSON_INVALID", "transcript JSON is invalid") from exc
    if not isinstance(values, list):
        raise VideoUnderstandingError("TRANSCRIPT_JSON_INVALID", "transcript JSON must be a list")
    segments: list[TranscriptSegment] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise VideoUnderstandingError("TRANSCRIPT_JSON_INVALID", "transcript segment must be an object")
        segments.append(
            TranscriptSegment(
                item.get("start", item.get("start_seconds")),
                item.get("end", item.get("end_seconds")),
                item.get("text", ""),
                str(item.get("language", language)),
                str(item.get("provider", provider)),
                item.get("confidence"),
                str(item.get("provenance", "local_asr")),
            )
        )
    return tuple(segments)


def parse_timestamp(value: str) -> float:
    match = _TIMESTAMP_RE.fullmatch(value.strip())
    if match is None:
        raise VideoUnderstandingError("TRANSCRIPT_TIMESTAMP_INVALID", "timestamp format is invalid")
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    milliseconds = int(match.group("ms"))
    if minutes >= 60 or seconds >= 60:
        raise VideoUnderstandingError("TRANSCRIPT_TIMESTAMP_INVALID", "timestamp value is invalid")
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def normalize_segments(segments: Iterable[TranscriptSegment]) -> tuple[TranscriptSegment, ...]:
    ordered = sorted(tuple(segments), key=lambda item: (item.start_seconds, item.end_seconds, item.text))
    result: list[TranscriptSegment] = []
    for segment in ordered:
        if result and segment.start_seconds < result[-1].start_seconds:
            raise VideoUnderstandingError("TRANSCRIPT_NOT_MONOTONIC", "transcript order is invalid")
        if (
            result
            and segment.text.casefold() == result[-1].text.casefold()
            and segment.start_seconds <= result[-1].end_seconds
        ):
            previous = result[-1]
            result[-1] = TranscriptSegment(
                previous.start_seconds,
                max(previous.end_seconds, segment.end_seconds),
                previous.text,
                previous.language,
                previous.provider,
                previous.confidence,
                previous.provenance,
            )
            continue
        result.append(segment)
    return tuple(result)


def assess_quality(
    segments: Sequence[TranscriptSegment],
    *,
    media_duration_seconds: float | None,
    max_chars: int,
) -> TranscriptQuality:
    if not segments:
        return TranscriptQuality("MISSING", ("NO_SEGMENTS",), 0, 0, media_duration_seconds or 0.0, 0.0, 0.0)
    character_count = sum(len(segment.text) for segment in segments)
    if character_count > max_chars:
        raise VideoUnderstandingError("TRANSCRIPT_TOO_LARGE", "transcript exceeded configured size")
    covered = _union_duration((segment.start_seconds, segment.end_seconds) for segment in segments)
    duration = media_duration_seconds if media_duration_seconds and media_duration_seconds > 0 else max(
        segment.end_seconds for segment in segments
    )
    ratio = min(1.0, covered / duration) if duration > 0 else 0.0
    reasons: list[str] = []
    if len(segments) < 3:
        reasons.append("TOO_FEW_SEGMENTS")
    if character_count < 120:
        reasons.append("TOO_FEW_CHARACTERS")
    if duration >= 60 and ratio < 0.25:
        reasons.append("LOW_TIME_COVERAGE")
    status = "GOOD" if not reasons else "LOW"
    return TranscriptQuality(status, tuple(reasons), len(segments), character_count, duration, covered, ratio)


def transcript_to_jsonl(segments: Sequence[TranscriptSegment]) -> str:
    return "".join(json.dumps(segment.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for segment in segments)


def _union_duration(ranges: Iterable[tuple[float, float]]) -> float:
    ordered = sorted(ranges)
    if not ordered:
        return 0.0
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += max(0.0, current_end - current_start)
            current_start, current_end = start, end
    return total + max(0.0, current_end - current_start)


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoUnderstandingError("TRANSCRIPT_NUMBER_INVALID", f"{field_name} must be numeric")
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        raise VideoUnderstandingError("TRANSCRIPT_NUMBER_INVALID", f"{field_name} must be finite")
    return number
