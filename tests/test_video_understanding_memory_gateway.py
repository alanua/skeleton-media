from __future__ import annotations

from pathlib import Path

import pytest

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
    VideoUnderstandingError,
)


MANIFEST_HASH = "a" * 64
APPROVAL_REF = "operator.video.phase_a"


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


def test_memory_gateway_envelope_matches_current_main_contract() -> None:
    envelope = build_private_mutation(_record(), approval_ref=APPROVAL_REF)
    assert envelope["schema"] == "skeleton.memory_gateway.request.v1"
    assert envelope["namespace"] == "skeleton"
    assert envelope["command"] == "skeleton.memory.private_mutate"
    payload = envelope["payload"]
    assert payload["schema"] == "skeleton.private_memory_gateway.mutation.v1"
    assert payload["operation"] == "put"
    assert payload["project_id"] == "skeleton"
    assert payload["dataset_id"] == "video_understanding"
    assert payload["fact_namespace"] == "video_understanding"
    assert payload["fact_id"].startswith("video:")
    assert payload["source_hash"] == MANIFEST_HASH
    assert payload["approval_ref"] == APPROVAL_REF
    assert payload["value"]["about"]["summary"] == "synthetic private summary"
    assert payload["value"]["state"] == "UNDERSTOOD"
    assert payload["value"]["mode"] == "STANDARD"


def test_identical_replay_has_identical_identity_and_revision_changes_it() -> None:
    first = build_private_mutation(_record("r1"), approval_ref=APPROVAL_REF)
    replay = build_private_mutation(_record("r1"), approval_ref=APPROVAL_REF)
    changed = build_private_mutation(_record("r2"), approval_ref=APPROVAL_REF)
    assert first["payload"]["idempotency_key"] == replay["payload"]["idempotency_key"]
    assert canonical_request_fingerprint(first) == canonical_request_fingerprint(replay)
    assert changed["payload"]["idempotency_key"] != first["payload"]["idempotency_key"]
    assert changed["payload"]["fact_id"] != first["payload"]["fact_id"]


def test_mutation_requires_explicit_safe_approval_reference() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        build_private_mutation(_record(), approval_ref="operator/unsafe")
    assert exc.value.reason_code == "INVALID_GATEWAY_TOKEN"


def test_expected_revision_rejects_boolean() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        build_private_mutation(_record(), approval_ref=APPROVAL_REF, expected_revision=True)
    assert exc.value.reason_code == "INVALID_EXPECTED_REVISION"


def test_adapter_contains_no_direct_sqlite_or_database_path() -> None:
    source = Path("core/video_understanding/memory_gateway_adapter.py").read_text()
    lowered = source.casefold()
    assert "import sqlite3" not in lowered
    assert ".sqlite" not in lowered
    assert "database_path" not in lowered
