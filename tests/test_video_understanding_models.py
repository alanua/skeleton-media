from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.video_understanding.models import (
    Claim,
    ProcessingMode,
    ProcessingState,
    SupportType,
    TimestampedEvidence,
    VideoRequest,
    VideoUnderstandingError,
    public_receipt,
    validate_transition,
)


def test_all_processing_modes_are_fixed() -> None:
    assert {mode.value for mode in ProcessingMode} == {
        "QUICK", "STANDARD", "DEEP", "TARGETED", "ARCHIVE"
    }


def test_targeted_request_requires_question() -> None:
    with pytest.raises(VideoUnderstandingError, match="question") as exc:
        VideoRequest(operation="video_query", mode=ProcessingMode.TARGETED)
    assert exc.value.reason_code == "QUESTION_REQUIRED"


def test_reusable_and_canon_transitions_require_explicit_approval() -> None:
    with pytest.raises(VideoUnderstandingError) as reusable:
        validate_transition(ProcessingState.HUMAN_REVIEWED, ProcessingState.ACCEPTED_REUSABLE)
    assert reusable.value.reason_code == "HUMAN_REVIEW_REQUIRED"
    validate_transition(
        ProcessingState.HUMAN_REVIEWED,
        ProcessingState.ACCEPTED_REUSABLE,
        human_approved=True,
    )
    with pytest.raises(VideoUnderstandingError) as promoted:
        validate_transition(ProcessingState.ACCEPTED_REUSABLE, ProcessingState.PROMOTED)
    assert promoted.value.reason_code == "CANON_APPROVAL_REQUIRED"


def test_non_inferred_claim_requires_evidence() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        Claim("claim-1", "A visible value exists", SupportType.VISUAL_ONLY, (), 0.8)
    assert exc.value.reason_code == "EVIDENCE_REQUIRED"
    inferred = Claim("claim-2", "Possible explanation", SupportType.INFERRED, (), 0.4)
    assert inferred.support_type is SupportType.INFERRED


def test_timestamp_evidence_rejects_reverse_range() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        TimestampedEvidence("ev", 10, 9, "frame-1", "FRAME", 0.8)
    assert exc.value.reason_code == "INVALID_TIMESTAMP"


def test_public_receipt_has_no_private_values() -> None:
    receipt = public_receipt(
        operation="video_understand_url",
        status="PLANNED",
        reason_code="SUPPORTED_YOUTUBE",
        mode="STANDARD",
        transcript_count=1,
        frame_count=2,
        ocr_count=1,
        evidence_count=3,
    )
    serialized = repr(receipt)
    for private_value in (
        "https://youtu.be/AbCdEf12345",
        "AbCdEf12345",
        "/home/private/video.mp4",
        "private transcript text",
        "project-secret",
        "a" * 64,
    ):
        assert private_value not in serialized


def test_all_video_schemas_parse_and_reject_root_unknown_fields() -> None:
    names = (
        "video_understanding_request.schema.json",
        "video_understanding_record.schema.json",
        "video_understanding_receipt.schema.json",
    )
    for name in names:
        schema = json.loads((Path("schemas") / name).read_text())
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    request = json.loads((Path("schemas") / names[0]).read_text())
    assert request["properties"]["payload"]["additionalProperties"] is False
