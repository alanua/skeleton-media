from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from core.video_understanding.models import VideoRecord, VideoUnderstandingError


GATEWAY_REQUEST_SCHEMA = "skeleton.memory_gateway.request.v1"
PRIVATE_MUTATION_SCHEMA = "skeleton.private_memory_gateway.mutation.v1"
NAMESPACE = "skeleton"
DATASET_ID = "video_understanding"
FACT_NAMESPACE = "video_understanding"
COMMAND = "skeleton.memory.private_mutate"
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _stable_digest(*parts: str) -> str:
    encoded = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_token(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN_RE.fullmatch(value) is None:
        raise VideoUnderstandingError("INVALID_GATEWAY_TOKEN", f"{field_name} is invalid")
    lowered = value.casefold()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "/", "\\")):
        raise VideoUnderstandingError("INVALID_GATEWAY_TOKEN", f"{field_name} contains a private marker")
    return value


def build_private_mutation(
    record: VideoRecord,
    *,
    approval_ref: str,
    actor_ref: str = "skeleton.video_understanding",
    reason_code: str = "video_understanding_record_commit",
    expected_revision: int | None = None,
    manifest_hash: str | None = None,
) -> dict[str, Any]:
    effective_manifest_hash = manifest_hash or record.artifact_manifest_hash
    if effective_manifest_hash != record.artifact_manifest_hash:
        raise VideoUnderstandingError(
            "MANIFEST_HASH_MISMATCH",
            "record and mutation manifest hashes differ",
        )
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise VideoUnderstandingError(
            "INVALID_EXPECTED_REVISION",
            "expected revision must be a non-negative integer",
        )

    fact_digest = _stable_digest(record.video_record_id, record.processing_revision)
    fact_id = f"video:{fact_digest[:48]}"
    idempotency_key = "video:" + _stable_digest(
        record.source.private_identity,
        effective_manifest_hash,
        record.processing_revision,
    )
    value = record.to_private_value()
    value["relations"] = {
        "project_links": [link.project_id for link in record.project_links],
        "review_status": record.review.status,
        "source_identity": record.source.private_identity,
    }
    payload = {
        "schema": PRIVATE_MUTATION_SCHEMA,
        "operation": "put",
        "project_id": NAMESPACE,
        "dataset_id": DATASET_ID,
        "expected_revision": expected_revision,
        "actor_ref": _safe_token(actor_ref, "actor_ref"),
        "reason_code": _safe_token(reason_code, "reason_code"),
        "approval_ref": _safe_token(approval_ref, "approval_ref"),
        "fact_namespace": FACT_NAMESPACE,
        "fact_id": fact_id,
        "value": value,
        "source_hash": effective_manifest_hash,
        "idempotency_key": idempotency_key,
    }
    return {
        "schema": GATEWAY_REQUEST_SCHEMA,
        "namespace": NAMESPACE,
        "command": COMMAND,
        "payload": payload,
    }


def canonical_request_fingerprint(envelope: dict[str, Any]) -> str:
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
