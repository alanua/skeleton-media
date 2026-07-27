from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class VideoUnderstandingError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ProcessingMode(str, Enum):
    QUICK = "QUICK"
    STANDARD = "STANDARD"
    DEEP = "DEEP"
    TARGETED = "TARGETED"
    ARCHIVE = "ARCHIVE"


class ProcessingState(str, Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    UNDERSTOOD = "UNDERSTOOD"
    PROJECT_LINKED = "PROJECT_LINKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"
    ACCEPTED_REUSABLE = "ACCEPTED_REUSABLE"
    PROMOTED = "PROMOTED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class SupportType(str, Enum):
    TRANSCRIPT_ONLY = "TRANSCRIPT_ONLY"
    VISUAL_ONLY = "VISUAL_ONLY"
    JOINT = "JOINT"
    EXTERNAL_REFERENCE = "EXTERNAL_REFERENCE"
    INFERRED = "INFERRED"


class Domain(str, Enum):
    DIOS = "DIOS"
    HOME_AUTOMATION = "HOME_AUTOMATION"
    TRAVEL = "TRAVEL"
    CONSTRUCTION = "CONSTRUCTION"
    LEGAL_DOCUMENTS = "LEGAL_DOCUMENTS"
    AVIATION = "AVIATION"
    SKELETON_ARCHITECTURE = "SKELETON_ARCHITECTURE"
    GENERAL_KNOWLEDGE = "GENERAL_KNOWLEDGE"


_ALLOWED_TRANSITIONS: dict[ProcessingState, frozenset[ProcessingState]] = {
    ProcessingState.QUEUED: frozenset(
        {ProcessingState.PROCESSING, ProcessingState.FAILED, ProcessingState.QUARANTINED}
    ),
    ProcessingState.PROCESSING: frozenset(
        {
            ProcessingState.PROCESSED,
            ProcessingState.REVIEW_REQUIRED,
            ProcessingState.FAILED,
            ProcessingState.QUARANTINED,
        }
    ),
    ProcessingState.PROCESSED: frozenset(
        {ProcessingState.UNDERSTOOD, ProcessingState.REVIEW_REQUIRED, ProcessingState.FAILED}
    ),
    ProcessingState.UNDERSTOOD: frozenset(
        {ProcessingState.PROJECT_LINKED, ProcessingState.REVIEW_REQUIRED, ProcessingState.HUMAN_REVIEWED}
    ),
    ProcessingState.PROJECT_LINKED: frozenset(
        {ProcessingState.REVIEW_REQUIRED, ProcessingState.HUMAN_REVIEWED}
    ),
    ProcessingState.REVIEW_REQUIRED: frozenset(
        {ProcessingState.HUMAN_REVIEWED, ProcessingState.FAILED, ProcessingState.QUARANTINED}
    ),
    ProcessingState.HUMAN_REVIEWED: frozenset(
        {ProcessingState.ACCEPTED_REUSABLE, ProcessingState.REVIEW_REQUIRED}
    ),
    ProcessingState.ACCEPTED_REUSABLE: frozenset({ProcessingState.PROMOTED}),
    ProcessingState.PROMOTED: frozenset(),
    ProcessingState.FAILED: frozenset({ProcessingState.QUEUED, ProcessingState.QUARANTINED}),
    ProcessingState.QUARANTINED: frozenset({ProcessingState.QUEUED}),
}


def validate_transition(
    current: ProcessingState | str,
    target: ProcessingState | str,
    *,
    human_approved: bool = False,
    canon_approved: bool = False,
) -> None:
    current_state = ProcessingState(current)
    target_state = ProcessingState(target)
    if target_state not in _ALLOWED_TRANSITIONS[current_state]:
        raise VideoUnderstandingError(
            "INVALID_STATE_TRANSITION",
            f"transition {current_state.value}->{target_state.value} is not allowed",
        )
    if target_state is ProcessingState.ACCEPTED_REUSABLE and not human_approved:
        raise VideoUnderstandingError(
            "HUMAN_REVIEW_REQUIRED",
            "reusable knowledge requires explicit human approval",
        )
    if target_state is ProcessingState.PROMOTED and not canon_approved:
        raise VideoUnderstandingError(
            "CANON_APPROVAL_REQUIRED",
            "canon promotion requires explicit approval",
        )


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoUnderstandingError("INVALID_CONFIDENCE", "confidence must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise VideoUnderstandingError("INVALID_CONFIDENCE", "confidence must be between 0 and 1")
    return normalized


def _nonempty(value: str, field_name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VideoUnderstandingError("INVALID_FIELD", f"{field_name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise VideoUnderstandingError("FIELD_TOO_LARGE", f"{field_name} is too large")
    return cleaned


def _timestamp_range(start_seconds: float, end_seconds: float) -> tuple[float, float]:
    values = (start_seconds, end_seconds)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        raise VideoUnderstandingError("INVALID_TIMESTAMP", "timestamps must be numeric")
    start, end = float(start_seconds), float(end_seconds)
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        raise VideoUnderstandingError("INVALID_TIMESTAMP", "timestamp range is invalid")
    return start, end


@dataclass(frozen=True)
class SourceReference:
    source_type: str
    private_identity: str
    adapter: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", _nonempty(self.source_type, "source_type", max_length=64))
        object.__setattr__(self, "private_identity", _nonempty(self.private_identity, "private_identity"))
        object.__setattr__(self, "adapter", _nonempty(self.adapter, "adapter", max_length=64))


@dataclass(frozen=True)
class TranscriptArtifact:
    artifact_id: str
    language: str
    provider: str
    quality_status: str
    segment_count: int
    text_sha256: str

    def __post_init__(self) -> None:
        if self.segment_count < 0:
            raise VideoUnderstandingError("INVALID_COUNT", "segment_count must be non-negative")


@dataclass(frozen=True)
class FrameEvidence:
    artifact_id: str
    timestamp_seconds: float
    evidence_kind: str
    confidence: float
    ocr_text: str | None = None

    def __post_init__(self) -> None:
        start, _ = _timestamp_range(self.timestamp_seconds, self.timestamp_seconds)
        object.__setattr__(self, "timestamp_seconds", start)
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class Topic:
    name: str
    confidence: float
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "topic", max_length=256))
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class Entity:
    entity_type: str
    name: str
    confidence: float
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_type", _nonempty(self.entity_type, "entity_type", max_length=64))
        object.__setattr__(self, "name", _nonempty(self.name, "entity_name", max_length=512))
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    support_type: SupportType
    evidence_ids: tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _nonempty(self.text, "claim_text"))
        object.__setattr__(self, "support_type", SupportType(self.support_type))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        if self.support_type is not SupportType.INFERRED and not self.evidence_ids:
            raise VideoUnderstandingError(
                "EVIDENCE_REQUIRED", "non-inferred claims require evidence"
            )


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    name: str
    ordered_steps: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if not self.ordered_steps:
            raise VideoUnderstandingError("WORKFLOW_STEPS_REQUIRED", "workflow requires steps")
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class TimestampedEvidence:
    evidence_id: str
    start_seconds: float
    end_seconds: float
    artifact_id: str
    evidence_kind: str
    confidence: float
    private_excerpt: str | None = None

    def __post_init__(self) -> None:
        start, end = _timestamp_range(self.start_seconds, self.end_seconds)
        object.__setattr__(self, "start_seconds", start)
        object.__setattr__(self, "end_seconds", end)
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class ProjectLink:
    project_id: str
    relation: str
    confidence: float
    operator_selected: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class ReviewDecision:
    status: str
    reviewer_type: str
    reason_codes: tuple[str, ...] = ()
    accepted_reusable: bool = False
    promoted: bool = False

    def __post_init__(self) -> None:
        if self.promoted and not self.accepted_reusable:
            raise VideoUnderstandingError(
                "INVALID_REVIEW_DECISION",
                "promoted knowledge must first be accepted as reusable",
            )


@dataclass(frozen=True)
class VideoRequest:
    operation: str
    mode: ProcessingMode
    source: str | None = None
    project_hint: str | None = None
    question: str | None = None
    depth: int = 1
    profile: Domain | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ProcessingMode(self.mode))
        if self.profile is not None:
            object.__setattr__(self, "profile", Domain(self.profile))
        if not 1 <= self.depth <= 3:
            raise VideoUnderstandingError("INVALID_DEPTH", "depth must be between 1 and 3")
        if self.question is not None:
            object.__setattr__(self, "question", _nonempty(self.question, "question", max_length=4000))
        if self.mode is ProcessingMode.TARGETED and self.question is None:
            raise VideoUnderstandingError(
                "QUESTION_REQUIRED", "TARGETED mode requires a question"
            )


@dataclass(frozen=True)
class VideoRecord:
    schema: str
    video_record_id: str
    processing_revision: str
    state: ProcessingState
    mode: ProcessingMode
    source: SourceReference
    detected_domain: Domain
    domain_candidates: tuple[Mapping[str, Any], ...]
    about: Mapping[str, Any]
    structure: tuple[Mapping[str, Any], ...]
    methods: tuple[Workflow, ...]
    topics: tuple[Topic, ...]
    entities: tuple[Entity, ...]
    claims: tuple[Claim, ...]
    evidence: tuple[TimestampedEvidence, ...]
    actions: tuple[Mapping[str, Any], ...]
    conflicts: tuple[Mapping[str, Any], ...]
    project_links: tuple[ProjectLink, ...]
    review: ReviewDecision
    artifact_manifest_hash: str
    transcript_artifacts: tuple[TranscriptArtifact, ...] = ()
    frame_evidence: tuple[FrameEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ProcessingState(self.state))
        object.__setattr__(self, "mode", ProcessingMode(self.mode))
        object.__setattr__(self, "detected_domain", Domain(self.detected_domain))
        if self.review.promoted and self.state is not ProcessingState.PROMOTED:
            raise VideoUnderstandingError(
                "STATE_REVIEW_MISMATCH", "promoted review requires PROMOTED state"
            )

    def to_private_value(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        value["mode"] = self.mode.value
        value["detected_domain"] = self.detected_domain.value
        for claim in value["claims"]:
            claim["support_type"] = claim["support_type"].value
        return value


def public_receipt(
    *,
    operation: str,
    status: str,
    reason_code: str,
    mode: ProcessingMode | str,
    detected_domain: Domain | str | None = None,
    transcript_count: int = 0,
    frame_count: int = 0,
    ocr_count: int = 0,
    evidence_count: int = 0,
    review_required: bool = False,
    canonical_mutation_status: str = "NOT_ATTEMPTED",
    projection_status: str = "NOT_ATTEMPTED",
) -> dict[str, Any]:
    values = (transcript_count, frame_count, ocr_count, evidence_count)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        raise VideoUnderstandingError("INVALID_COUNT", "receipt counts must be non-negative integers")
    return {
        "schema": "skeleton.video_understanding.receipt.v1",
        "operation": _nonempty(operation, "operation", max_length=64),
        "status": _nonempty(status, "status", max_length=64),
        "reason_code": _nonempty(reason_code, "reason_code", max_length=128),
        "mode": ProcessingMode(mode).value,
        "detected_domain": Domain(detected_domain).value if detected_domain else None,
        "transcript_count": transcript_count,
        "frame_count": frame_count,
        "ocr_count": ocr_count,
        "evidence_count": evidence_count,
        "review_required": bool(review_required),
        "canonical_mutation_status": canonical_mutation_status,
        "projection_status": projection_status,
    }


def reject_unknown_fields(payload: Mapping[str, Any], allowed: Sequence[str]) -> None:
    unknown = set(payload) - set(allowed)
    if unknown:
        raise VideoUnderstandingError(
            "UNKNOWN_FIELDS", f"unsupported fields: {','.join(sorted(unknown))}"
        )
