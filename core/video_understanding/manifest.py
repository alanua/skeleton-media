from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Iterable, Mapping

from core.video_understanding.models import ProcessingMode, VideoUnderstandingError


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactEntry:
    artifact_id: str
    relative_path: str
    sha256: str
    byte_size: int
    media_type: str
    producer: str
    processing_revision: str

    def __post_init__(self) -> None:
        normalized = validate_relative_artifact_path(self.relative_path)
        object.__setattr__(self, "relative_path", normalized)
        if _SHA256_RE.fullmatch(self.sha256) is None:
            raise VideoUnderstandingError("INVALID_ARTIFACT_HASH", "artifact hash is invalid")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size < 0:
            raise VideoUnderstandingError("INVALID_ARTIFACT_SIZE", "artifact size is invalid")


@dataclass(frozen=True)
class ArtifactManifest:
    schema: str
    video_record_id: str
    processing_revision: str
    mode: ProcessingMode
    entries: tuple[ArtifactEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ProcessingMode(self.mode))
        ids = [entry.artifact_id for entry in self.entries]
        paths = [entry.relative_path for entry in self.entries]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise VideoUnderstandingError(
                "DUPLICATE_ARTIFACT_IDENTITY", "manifest contains duplicate artifact identities"
            )

    @property
    def source_retention(self) -> str:
        return "RETAIN_SOURCE" if self.mode is ProcessingMode.ARCHIVE else "DELETE_TEMP_AFTER_VERIFY"

    def private_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "video_record_id": self.video_record_id,
            "processing_revision": self.processing_revision,
            "mode": self.mode.value,
            "source_retention": self.source_retention,
            "entries": [asdict(entry) for entry in sorted(self.entries, key=lambda item: item.artifact_id)],
        }

    def deterministic_hash(self) -> str:
        encoded = json.dumps(
            self.private_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_relative_artifact_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise VideoUnderstandingError("INVALID_ARTIFACT_PATH", "artifact path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VideoUnderstandingError("INVALID_ARTIFACT_PATH", "artifact path must be relative")
    normalized = path.as_posix()
    if normalized.startswith("/") or "//" in normalized:
        raise VideoUnderstandingError("INVALID_ARTIFACT_PATH", "artifact path is unsafe")
    return normalized


def verify_inventory(
    manifest: ArtifactManifest,
    inventory: Mapping[str, tuple[str, int]],
) -> dict[str, object]:
    missing = 0
    mismatched = 0
    for entry in manifest.entries:
        observed = inventory.get(entry.relative_path)
        if observed is None:
            missing += 1
            continue
        observed_hash, observed_size = observed
        if observed_hash != entry.sha256 or observed_size != entry.byte_size:
            mismatched += 1
    return {
        "verified": missing == 0 and mismatched == 0 and len(inventory) == len(manifest.entries),
        "entry_count": len(manifest.entries),
        "missing_count": missing,
        "mismatched_count": mismatched,
        "unexpected_count": max(0, len(inventory) - len(manifest.entries)),
    }


def build_manifest(
    *,
    video_record_id: str,
    processing_revision: str,
    mode: ProcessingMode | str,
    entries: Iterable[ArtifactEntry],
) -> ArtifactManifest:
    return ArtifactManifest(
        schema="skeleton.video_understanding.manifest.v1",
        video_record_id=video_record_id,
        processing_revision=processing_revision,
        mode=ProcessingMode(mode),
        entries=tuple(entries),
    )
