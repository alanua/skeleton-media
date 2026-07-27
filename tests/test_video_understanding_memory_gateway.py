from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory_gateway import MemoryGateway, capability_token
from core.memory_gateway_storage import PrivateMemoryGatewayStorage
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


def test_payload_passes_current_storage_normalization(tmp_path: Path) -> None:
    stack = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path, db=tmp_path / "canonical.sqlite3")
    )
    storage = PrivateMemoryGatewayStorage(stack)
    envelope = build_private_mutation(_record(), approval_ref=APPROVAL_REF)
    normalized = storage._normalize_payload(envelope["payload"])
    assert normalized["operation"] == "put"
    assert normalized["project_id"] == "skeleton"
    assert normalized["dataset_id"] == "video_understanding"
    assert normalized["canonical_ref"].startswith("video_understanding:video:")
    assert normalized["source_hash"] == MANIFEST_HASH


def test_envelope_passes_current_memory_gateway_dispatch() -> None:
    captured = {}

    class Storage:
        def execute_mutation(self, payload):
            captured.update(payload)
            return {"status": "DONE", "aggregate_counts": {"mutation_count": 1}}

    gateway = MemoryGateway(
        capability_token(namespaces=("skeleton",), public_mode=False),
        private_memory_storage=Storage(),
    )
    response = gateway.execute(
        build_private_mutation(_record(), approval_ref=APPROVAL_REF)
    )
    assert captured["schema"] == "skeleton.private_memory_gateway.mutation.v1"
    assert captured["dataset_id"] == "video_understanding"
    assert response["command"] == "skeleton.memory.private_mutate"


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
