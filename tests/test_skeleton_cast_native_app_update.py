from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAST_RUNTIME = ROOT / "ops" / "skeleton_cast" / "runtime"
sys.path.insert(0, str(CAST_RUNTIME))

import native_app_update_manifest as update_manifest  # noqa: E402


def _publish(tmp_path: Path, *, content: bytes = b"production-home-apk", name: str = "Home-1.3.17.apk"):
    static = tmp_path / "static"
    static.mkdir()
    sha = hashlib.sha256(content).hexdigest()
    for filename in ("Home.apk", "SkeletonTV.apk", name):
        (static / filename).write_bytes(content)
    release = tmp_path / "home-native-release.json"
    release.write_text(json.dumps({
        "schema": "skeleton.home.native_release.v1",
        "version_code": 31,
        "version_name": "1.3.17",
        "sha256": sha,
        "bytes": len(content),
        "apk": name,
        "signer_sha256": "a" * 64,
        "published_at": 123,
    }), encoding="utf-8")
    return static, release, sha


def test_exact_production_aliases_return_update_manifest(tmp_path: Path) -> None:
    static, release, sha = _publish(tmp_path)
    payload = update_manifest.build_update_manifest(static, release)
    assert payload == {
        "schema": "skeleton.home.native_app_update.v1",
        "version_code": 31,
        "version_name": "1.3.17",
        "sha256": sha,
        "bytes": len(b"production-home-apk"),
        "apk_path": "/download/SkeletonTV.apk",
        "published_at": 123,
    }


def test_download_alias_mismatch_fails_closed(tmp_path: Path) -> None:
    static, release, _ = _publish(tmp_path)
    (static / "SkeletonTV.apk").write_bytes(b"different")
    with pytest.raises(update_manifest.NativeAppUpdateUnavailable):
        update_manifest.build_update_manifest(static, release)


def test_preview_named_release_cannot_be_advertised(tmp_path: Path) -> None:
    static, release, _ = _publish(tmp_path, name="Home-preview.apk")
    with pytest.raises(update_manifest.NativeAppUpdateUnavailable):
        update_manifest.build_update_manifest(static, release)


def test_release_metadata_size_or_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    static, release, _ = _publish(tmp_path)
    data = json.loads(release.read_text(encoding="utf-8"))
    data["bytes"] += 1
    release.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(update_manifest.NativeAppUpdateUnavailable):
        update_manifest.build_update_manifest(static, release)
