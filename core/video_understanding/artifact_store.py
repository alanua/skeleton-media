from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from core.video_understanding.manifest import ArtifactEntry, build_manifest, verify_inventory
from core.video_understanding.models import ProcessingMode, VideoUnderstandingError
from core.video_understanding.runtime_config import VideoRuntimeConfig


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


@dataclass(frozen=True)
class FinalizedArtifact:
    video_record_id: str
    processing_revision: str
    root: Path
    manifest_hash: str
    entry_count: int
    replay: bool

    def public_receipt(self) -> dict[str, object]:
        return {
            "schema": "skeleton.video_understanding.artifact_receipt.v1",
            "status": "REPLAY" if self.replay else "FINALIZED",
            "entry_count": self.entry_count,
            "readback_verified": True,
        }


class PrivateArtifactStore:
    def __init__(self, config: VideoRuntimeConfig) -> None:
        self.config = config
        self.root = config.artifact_root
        self.staging_root = self.root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def workspace(self, video_record_id: str, processing_revision: str) -> Iterator[Path]:
        _safe_id(video_record_id, "video_record_id")
        _safe_id(processing_revision, "processing_revision")
        path = Path(
            tempfile.mkdtemp(
                prefix=f"{video_record_id}-{processing_revision}-",
                dir=self.staging_root,
            )
        )
        os.chmod(path, 0o700)
        try:
            yield path
        finally:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    def target_root(self, video_record_id: str, processing_revision: str) -> Path:
        _safe_id(video_record_id, "video_record_id")
        _safe_id(processing_revision, "processing_revision")
        target = self.root / video_record_id / processing_revision
        resolved = target.resolve(strict=False)
        if self.root not in resolved.parents:
            raise VideoUnderstandingError("ARTIFACT_PATH_ESCAPE", "artifact target escaped root")
        return resolved

    def finalize(
        self,
        workspace: Path,
        *,
        video_record_id: str,
        processing_revision: str,
        mode: ProcessingMode | str,
        producer: str = "skeleton-video-understanding",
    ) -> FinalizedArtifact:
        workspace = workspace.resolve(strict=True)
        if self.staging_root not in workspace.parents:
            raise VideoUnderstandingError("ARTIFACT_WORKSPACE_INVALID", "workspace is outside staging root")
        entries = self._inventory(workspace, processing_revision, producer)
        if not entries:
            raise VideoUnderstandingError("ARTIFACT_SET_EMPTY", "no artifacts were produced")
        manifest = build_manifest(
            video_record_id=video_record_id,
            processing_revision=processing_revision,
            mode=mode,
            entries=entries,
        )
        manifest_hash = manifest.deterministic_hash()
        manifest_payload = {
            "manifest": manifest.private_dict(),
            "manifest_hash": manifest_hash,
        }
        _atomic_json_write(workspace / "manifest.json", manifest_payload)
        _fsync_tree(workspace)

        target = self.target_root(video_record_id, processing_revision)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            existing = self._read_manifest(target)
            if existing.get("manifest_hash") == manifest_hash:
                self._verify_target(target, manifest)
                return FinalizedArtifact(
                    video_record_id,
                    processing_revision,
                    target,
                    manifest_hash,
                    len(entries),
                    True,
                )
            raise VideoUnderstandingError(
                "ARTIFACT_REVISION_CONFLICT",
                "artifact revision already exists with different content",
            )
        try:
            os.replace(workspace, target)
        except OSError as exc:
            raise VideoUnderstandingError("ARTIFACT_PROMOTION_FAILED", "artifact promotion failed") from exc
        _fsync_directory(target.parent)
        self._verify_target(target, manifest)
        _make_read_only(target)
        return FinalizedArtifact(
            video_record_id,
            processing_revision,
            target,
            manifest_hash,
            len(entries),
            False,
        )

    def _inventory(
        self,
        workspace: Path,
        processing_revision: str,
        producer: str,
    ) -> tuple[ArtifactEntry, ...]:
        entries: list[ArtifactEntry] = []
        for path in sorted(workspace.rglob("*")):
            if path.is_symlink():
                raise VideoUnderstandingError("ARTIFACT_SYMLINK_REJECTED", "artifact symlink is forbidden")
            if not path.is_file() or path.name == "manifest.json" or path.name.endswith(".part"):
                continue
            relative = path.relative_to(workspace).as_posix()
            entries.append(
                ArtifactEntry(
                    artifact_id="artifact:" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32],
                    relative_path=relative,
                    sha256=sha256_file(path),
                    byte_size=path.stat().st_size,
                    media_type=_media_type(path),
                    producer=producer,
                    processing_revision=processing_revision,
                )
            )
        return tuple(entries)

    def _verify_target(self, target: Path, manifest: object) -> None:
        inventory: dict[str, tuple[str, int]] = {}
        for path in sorted(target.rglob("*")):
            if path.is_symlink():
                raise VideoUnderstandingError("ARTIFACT_SYMLINK_REJECTED", "artifact symlink is forbidden")
            if not path.is_file() or path.name == "manifest.json":
                continue
            resolved = path.resolve(strict=True)
            if target not in resolved.parents:
                raise VideoUnderstandingError("ARTIFACT_READBACK_ESCAPE", "artifact escaped target")
            relative = resolved.relative_to(target).as_posix()
            inventory[relative] = (sha256_file(resolved), resolved.stat().st_size)
        verification = verify_inventory(manifest, inventory)  # type: ignore[arg-type]
        if verification.get("verified") is not True:
            raise VideoUnderstandingError("ARTIFACT_READBACK_FAILED", "artifact readback verification failed")
        payload = self._read_manifest(target)
        if payload.get("manifest_hash") != manifest.deterministic_hash():  # type: ignore[attr-defined]
            raise VideoUnderstandingError("MANIFEST_READBACK_FAILED", "manifest hash did not read back")

    @staticmethod
    def _read_manifest(target: Path) -> dict[str, object]:
        try:
            payload = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VideoUnderstandingError("MANIFEST_READ_FAILED", "artifact manifest is unreadable") from exc
        if not isinstance(payload, dict):
            raise VideoUnderstandingError("MANIFEST_READ_FAILED", "artifact manifest is invalid")
        return payload


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None or ".." in value:
        raise VideoUnderstandingError("ARTIFACT_ID_INVALID", f"{field_name} is invalid")
    return value


def _atomic_json_write(path: Path, payload: object) -> None:
    temporary = path.with_name(path.name + ".part")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o400)
        elif path.is_dir():
            os.chmod(path, 0o500)
    os.chmod(root, 0o500)


def _media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".vtt": "text/vtt",
        ".srt": "application/x-subrip",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".wav": "audio/wav",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
    }.get(suffix, "application/octet-stream")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
