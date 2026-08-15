from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


RELEASE_SCHEMA = "skeleton.home.native_release.v1"
UPDATE_SCHEMA = "skeleton.home.native_app_update.v1"
_VERSIONED_APK_RE = re.compile(r"^Home-\d+\.\d+\.\d+\.apk$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class NativeAppUpdateUnavailable(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_update_manifest(static_dir: Path, release_record: Path) -> dict[str, object]:
    home_apk = static_dir / "Home.apk"
    download_apk = static_dir / "SkeletonTV.apk"
    if not home_apk.is_file() or not download_apk.is_file() or not release_record.is_file():
        raise NativeAppUpdateUnavailable("Home release is not published")
    try:
        release = json.loads(release_record.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise NativeAppUpdateUnavailable("Home release record is invalid") from exc
    if not isinstance(release, dict) or release.get("schema") != RELEASE_SCHEMA:
        raise NativeAppUpdateUnavailable("Home release record schema is invalid")
    try:
        version_code = int(release.get("version_code") or 0)
        expected_bytes = int(release.get("bytes") or 0)
        published_at = int(release.get("published_at") or 0)
    except (TypeError, ValueError) as exc:
        raise NativeAppUpdateUnavailable("Home release numeric metadata is invalid") from exc
    version_name = str(release.get("version_name") or "").strip()
    expected_sha = str(release.get("sha256") or "").strip().lower()
    versioned_name = Path(str(release.get("apk") or "")).name
    signer_sha = str(release.get("signer_sha256") or "").strip().lower()
    if version_code <= 0 or expected_bytes <= 0 or not version_name or not _SHA_RE.fullmatch(expected_sha):
        raise NativeAppUpdateUnavailable("Home release metadata is incomplete")
    if signer_sha and not _SHA_RE.fullmatch(signer_sha):
        raise NativeAppUpdateUnavailable("Home release signer metadata is invalid")
    if not _VERSIONED_APK_RE.fullmatch(versioned_name):
        raise NativeAppUpdateUnavailable("Home release is not a production versioned APK")
    versioned_apk = static_dir / versioned_name
    if not versioned_apk.is_file():
        raise NativeAppUpdateUnavailable("Home versioned APK is missing")
    for candidate in (home_apk, download_apk, versioned_apk):
        if candidate.stat().st_size != expected_bytes or _sha256(candidate) != expected_sha:
            raise NativeAppUpdateUnavailable("Home published APK aliases do not match the release record")
    return {
        "schema": UPDATE_SCHEMA,
        "version_code": version_code,
        "version_name": version_name,
        "sha256": expected_sha,
        "bytes": expected_bytes,
        "apk_path": "/download/SkeletonTV.apk",
        "published_at": published_at,
    }
