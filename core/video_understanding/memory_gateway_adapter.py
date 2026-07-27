from __future__ import annotations

import hashlib
import json
from typing import Any

from core.video_understanding.models import VideoRecord, VideoUnderstandingError


DATASET = "video_understanding"
COMMAND = "skeleton.memory.private_mutate"


def _stable_digest(*parts: str) -> str:
    encoded = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_private_mutation(
    record: VideoRecord,
    *,
    manifest_hash: str | None = None,
) -> dict[str, Any]:
    effective_manifest_hash = manifest_hash or record.artifact_manifest_hash
    if effective_manifest_hash != record.artifact_manifest_hash:
        raise VideoUnderstandingError(
            "MANIFEST_HASH_MISMATCH",
            "record and mutation manifest hashes differ",
        )
    fact_key = f"video:{record.video_record_id}:revision:{record.processing_revision}"
    idempotency_key = _stable_digest(
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
    return {
        "command": COMMAND,
        "operation": "put",
        "private_mode": True,
        "dataset": DATASET,
        "fact_key": fact_key,
        "idempotency_key": idempotency_key,
        "value": value,
        "projection": {
            "canonical_status": "PENDING",
            "derived_status": "NOT_ATTEMPTED",
        },
    }


def canonical_request_fingerprint(envelope: dict[str, Any]) -> str:
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
