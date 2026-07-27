from __future__ import annotations

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


def test_public_receipt_has_no_private_fields() -> None:
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
    serialized = repr(receipt).casefold()
    for marker in ("url", "title", "transcript", "path", "sha256", "video_id", "project_id"):
        assert marker not in serialized
