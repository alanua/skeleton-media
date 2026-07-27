from __future__ import annotations

import pytest

from core.video_understanding.manifest import ArtifactEntry, build_manifest, verify_inventory
from core.video_understanding.models import VideoUnderstandingError


HASH_A = "a" * 64
HASH_B = "b" * 64


def _entry(artifact_id: str, path: str, digest: str = HASH_A) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_id=artifact_id,
        relative_path=path,
        sha256=digest,
        byte_size=12,
        media_type="application/json",
        producer="synthetic-test",
        processing_revision="r1",
    )


def test_manifest_hash_is_deterministic_independent_of_input_order() -> None:
    first = build_manifest(
        video_record_id="vr_example",
        processing_revision="r1",
        mode="STANDARD",
        entries=[_entry("b", "summary.json", HASH_B), _entry("a", "metadata.json")],
    )
    second = build_manifest(
        video_record_id="vr_example",
        processing_revision="r1",
        mode="STANDARD",
        entries=[_entry("a", "metadata.json"), _entry("b", "summary.json", HASH_B)],
    )
    assert first.deterministic_hash() == second.deterministic_hash()


def test_archive_mode_retains_source_and_standard_cleans_temp() -> None:
    archive = build_manifest(
        video_record_id="vr_example",
        processing_revision="r1",
        mode="ARCHIVE",
        entries=[_entry("a", "source.json")],
    )
    standard = build_manifest(
        video_record_id="vr_example",
        processing_revision="r1",
        mode="STANDARD",
        entries=[_entry("a", "source.json")],
    )
    assert archive.source_retention == "RETAIN_SOURCE"
    assert standard.source_retention == "DELETE_TEMP_AFTER_VERIFY"


@pytest.mark.parametrize(
    "path",
    ["/absolute/file", "../escape", "frames/../../escape", r"frames\\x.png", "frames//x.png"],
)
def test_manifest_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(VideoUnderstandingError):
        _entry("a", path)


def test_manifest_rejects_duplicate_identity() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        build_manifest(
            video_record_id="vr_example",
            processing_revision="r1",
            mode="STANDARD",
            entries=[_entry("a", "metadata.json"), _entry("a", "summary.json")],
        )
    assert exc.value.reason_code == "DUPLICATE_ARTIFACT_IDENTITY"


def test_inventory_verification_returns_counts_only() -> None:
    manifest = build_manifest(
        video_record_id="vr_example",
        processing_revision="r1",
        mode="STANDARD",
        entries=[_entry("a", "metadata.json")],
    )
    result = verify_inventory(manifest, {"metadata.json": (HASH_A, 12)})
    assert result == {
        "verified": True,
        "entry_count": 1,
        "missing_count": 0,
        "mismatched_count": 0,
        "unexpected_count": 0,
    }
    assert HASH_A not in repr(result)


def test_inventory_counts_missing_and_unexpected_independently() -> None:
    manifest = build_manifest(
        video_record_id="vr_example",
        processing_revision="r1",
        mode="STANDARD",
        entries=[_entry("a", "metadata.json")],
    )
    result = verify_inventory(manifest, {"unexpected.json": (HASH_B, 12)})
    assert result["verified"] is False
    assert result["missing_count"] == 1
    assert result["unexpected_count"] == 1
