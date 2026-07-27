from __future__ import annotations

from pathlib import Path

from core.video_understanding.memory_gateway_adapter import (
    build_private_mutation,
    canonical_request_fingerprint,
)
from core.video_understanding.models import (
    Domain,
    ProcessingMode,
    ProcessingState,
    ReviewDecision,
    SourceReference,
    VideoRecord,
)


MANIFEST_HASH = "a" * 64


def _record(revision: str = "r1") -> VideoRecord:
    return VideoRecord(
        schema="skeleton.video_understanding.record.v1",
        video_record_id="vr_synthetic",
        processing_revision=revision,
        state=ProcessingState.UNDERSTOOD,
        mode=ProcessingMode.STANDARD,
        source=SourceReference(
            source_type="REMOTE_VIDEO",
            private_identity="https://example.invalid/private-video",
            adapter="synthetic",
        ),
        detected_domain=Domain.GENERAL_KNOWLEDGE,
        domain_candidates=(),
        about={"summary": "synthetic private summary"},
        structure=(),
        methods=(),
        topics=(),
        entities=(),
        claims=(),
        evidence=(),
        actions=(),
        conflicts=(),
        project_links=(),
        review=ReviewDecision("REVIEW_REQUIRED", "SYSTEM"),
        artifact_manifest_hash=MANIFEST_HASH,
    )


def test_memory_gateway_envelope_is_exact_and_private() -> None:
    envelope = build_private_mutation(_record())
    assert envelope["command"] == "skeleton.memory.private_mutate"
    assert envelope["operation"] == "put"
    assert envelope["private_mode"] is True
    assert envelope["dataset"] == "video_understanding"
    assert envelope["fact_key"] == "video:vr_synthetic:revision:r1"
    assert envelope["value"]["about"]["summary"] == "synthetic private summary"
    assert envelope["projection"] == {
        "canonical_status": "PENDING",
        "derived_status": "NOT_ATTEMPTED",
    }


def test_identical_replay_has_identical_identity_and_revision_changes_it() -> None:
    first = build_private_mutation(_record("r1"))
    replay = build_private_mutation(_record("r1"))
    changed = build_private_mutation(_record("r2"))
    assert first["idempotency_key"] == replay["idempotency_key"]
    assert canonical_request_fingerprint(first) == canonical_request_fingerprint(replay)
    assert changed["idempotency_key"] != first["idempotency_key"]
    assert changed["fact_key"] != first["fact_key"]


def test_adapter_contains_no_direct_sqlite_or_database_path() -> None:
    source = Path("core/video_understanding/memory_gateway_adapter.py").read_text()
    lowered = source.casefold()
    assert "import sqlite3" not in lowered
    assert ".sqlite" not in lowered
    assert "database_path" not in lowered
