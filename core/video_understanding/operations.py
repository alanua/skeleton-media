from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from core.video_understanding.domain_router import route_domain
from core.video_understanding.local_llm import local_llm_policy
from core.video_understanding.models import (
    Domain,
    ProcessingMode,
    VideoRequest,
    VideoUnderstandingError,
    public_receipt,
    reject_unknown_fields,
)
from core.video_understanding.url_classifier import (
    classify_local_reference,
    classify_remote_url,
)


OPERATIONS = frozenset(
    {
        "video_understand_url",
        "video_import_urls",
        "video_process_one",
        "video_status",
        "video_doctor",
        "video_query",
        "video_reprocess",
        "video_attach_to_project",
    }
)

_FORBIDDEN_FIELDS = frozenset(
    {
        "shell",
        "command",
        "ffmpeg_args",
        "ffprobe_args",
        "yt_dlp_args",
        "output_path",
        "browser_selector",
        "executable_path",
        "model_path",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "api_key",
        "headers",
    }
)

_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "video_understand_url": frozenset({"url", "project_hint", "question", "mode", "depth", "profile"}),
    "video_import_urls": frozenset({"urls", "project_hint", "mode", "depth", "profile"}),
    "video_process_one": frozenset({"video_record_id", "mode", "question", "depth", "profile"}),
    "video_status": frozenset({"video_record_id"}),
    "video_doctor": frozenset({"profile"}),
    "video_query": frozenset({"video_record_id", "question", "depth"}),
    "video_reprocess": frozenset({"video_record_id", "mode", "question", "depth", "profile"}),
    "video_attach_to_project": frozenset({"video_record_id", "project_hint", "relation"}),
}


def plan_operation(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise VideoUnderstandingError("UNKNOWN_OPERATION", "video operation is not supported")
    if not isinstance(payload, Mapping):
        raise VideoUnderstandingError("INVALID_PAYLOAD", "operation payload must be an object")
    forbidden = set(payload) & _FORBIDDEN_FIELDS
    if forbidden:
        raise VideoUnderstandingError(
            "PRIVATE_CONTROL_FIELD_FORBIDDEN",
            f"private runtime control fields are forbidden: {','.join(sorted(forbidden))}",
        )
    reject_unknown_fields(payload, _ALLOWED_FIELDS[operation])

    if operation == "video_understand_url":
        return _plan_understand_url(payload)
    if operation == "video_import_urls":
        return _plan_import_urls(payload)
    if operation in {"video_process_one", "video_reprocess"}:
        return _plan_existing(operation, payload)
    if operation == "video_query":
        return _plan_query(payload)
    if operation == "video_attach_to_project":
        return _plan_attach(payload)
    if operation == "video_status":
        return _plan_identifier_only(operation, payload)
    return _plan_doctor(payload)


def _mode(payload: Mapping[str, Any], default: ProcessingMode = ProcessingMode.STANDARD) -> ProcessingMode:
    try:
        return ProcessingMode(payload.get("mode", default.value))
    except ValueError as exc:
        raise VideoUnderstandingError("INVALID_MODE", "processing mode is unsupported") from exc


def _profile(payload: Mapping[str, Any]) -> Domain | None:
    raw = payload.get("profile")
    if raw is None:
        return None
    try:
        return Domain(raw)
    except ValueError as exc:
        raise VideoUnderstandingError("UNKNOWN_PROFILE", "video profile is unsupported") from exc


def _depth(payload: Mapping[str, Any]) -> int:
    raw = payload.get("depth", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 3:
        raise VideoUnderstandingError("INVALID_DEPTH", "depth must be an integer from 1 to 3")
    return raw


def _opaque_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("video_record_id")
    if not isinstance(value, str) or not value.startswith("vr_") or not 8 <= len(value) <= 160:
        raise VideoUnderstandingError("INVALID_VIDEO_RECORD_ID", "video_record_id is invalid")
    return value


def _question(payload: Mapping[str, Any], *, required: bool = False) -> str | None:
    value = payload.get("question")
    if value is None:
        if required:
            raise VideoUnderstandingError("QUESTION_REQUIRED", "question is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise VideoUnderstandingError("INVALID_QUESTION", "question is invalid")
    return value.strip()


def _project_hint(payload: Mapping[str, Any], *, required: bool = False) -> str | None:
    value = payload.get("project_hint")
    if value is None:
        if required:
            raise VideoUnderstandingError("PROJECT_HINT_REQUIRED", "project hint is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise VideoUnderstandingError("INVALID_PROJECT_HINT", "project hint is invalid")
    return value.strip()


def _private_plan(operation: str, request: VideoRequest, **extra: Any) -> dict[str, Any]:
    plan = {
        "schema": "skeleton.video_understanding.operation_plan.v1",
        "operation": operation,
        "mode": request.mode.value,
        "depth": request.depth,
        "profile": request.profile.value if request.profile else None,
        "project_hint": request.project_hint,
        "question": request.question,
        "local_llm_policy": local_llm_policy(request.mode),
        "canonical_mutation": "MEMORY_GATEWAY_ONLY",
        "network_execution": False,
        "media_execution": False,
    }
    plan.update(extra)
    return plan


def _public_plan(operation: str, mode: ProcessingMode, reason: str, *, domain: Domain | None = None) -> dict[str, Any]:
    return public_receipt(
        operation=operation,
        status="PLANNED",
        reason_code=reason,
        mode=mode,
        detected_domain=domain,
        canonical_mutation_status="NOT_ATTEMPTED",
        projection_status="NOT_ATTEMPTED",
    )


def _plan_understand_url(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_url = payload.get("url")
    if not isinstance(raw_url, str):
        raise VideoUnderstandingError("URL_REQUIRED", "url is required")
    source = classify_remote_url(raw_url)
    mode = _mode(payload)
    question = _question(payload, required=mode is ProcessingMode.TARGETED)
    profile = _profile(payload)
    request = VideoRequest(
        operation="video_understand_url",
        mode=mode,
        source=source.normalized_private_source,
        project_hint=_project_hint(payload),
        question=question,
        depth=_depth(payload),
        profile=profile,
    )
    route = route_domain(
        " ".join(filter(None, (request.project_hint, request.question))),
        explicit_profile=profile,
    )
    return {
        "private_plan": _private_plan(
            request.operation,
            request,
            source=asdict(source),
            selected_domain=route.selected.value,
            domain_candidates=[candidate.to_dict() for candidate in route.candidates],
            override_applied=route.override_applied,
        ),
        "public_receipt": _public_plan(request.operation, mode, source.reason_code, domain=route.selected),
    }


def _plan_import_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    values = payload.get("urls")
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise VideoUnderstandingError("INVALID_BATCH", "urls must contain 1 to 100 items")
    classifications = [classify_remote_url(value) for value in values]
    if len({item.normalized_private_source for item in classifications}) != len(classifications):
        raise VideoUnderstandingError("DUPLICATE_SOURCE", "batch contains duplicate sources")
    mode = _mode(payload, ProcessingMode.QUICK)
    profile = _profile(payload)
    request = VideoRequest(
        operation="video_import_urls",
        mode=mode,
        project_hint=_project_hint(payload),
        depth=_depth(payload),
        profile=profile,
    )
    return {
        "private_plan": _private_plan(
            request.operation,
            request,
            sources=[asdict(item) for item in classifications],
            batch_count=len(classifications),
        ),
        "public_receipt": _public_plan(request.operation, mode, "BATCH_VALIDATED"),
    }


def _plan_existing(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    mode = _mode(payload)
    request = VideoRequest(
        operation=operation,
        mode=mode,
        source=_opaque_id(payload),
        question=_question(payload, required=mode is ProcessingMode.TARGETED),
        depth=_depth(payload),
        profile=_profile(payload),
    )
    return {
        "private_plan": _private_plan(operation, request, video_record_id=request.source),
        "public_receipt": _public_plan(operation, mode, "REQUEST_VALIDATED"),
    }


def _plan_query(payload: Mapping[str, Any]) -> dict[str, Any]:
    request = VideoRequest(
        operation="video_query",
        mode=ProcessingMode.TARGETED,
        source=_opaque_id(payload),
        question=_question(payload, required=True),
        depth=_depth(payload),
    )
    return {
        "private_plan": _private_plan(request.operation, request, video_record_id=request.source),
        "public_receipt": _public_plan(request.operation, request.mode, "QUERY_VALIDATED"),
    }


def _plan_attach(payload: Mapping[str, Any]) -> dict[str, Any]:
    relation = payload.get("relation", "related_to")
    if not isinstance(relation, str) or relation not in {"related_to", "supports", "implements", "contradicts"}:
        raise VideoUnderstandingError("INVALID_RELATION", "project relation is invalid")
    request = VideoRequest(
        operation="video_attach_to_project",
        mode=ProcessingMode.QUICK,
        source=_opaque_id(payload),
        project_hint=_project_hint(payload, required=True),
    )
    return {
        "private_plan": _private_plan(
            request.operation,
            request,
            video_record_id=request.source,
            relation=relation,
            human_review_required=True,
        ),
        "public_receipt": _public_plan(request.operation, request.mode, "ATTACHMENT_REVIEW_REQUIRED"),
    }


def _plan_identifier_only(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    video_record_id = _opaque_id(payload)
    request = VideoRequest(operation=operation, mode=ProcessingMode.QUICK, source=video_record_id)
    return {
        "private_plan": _private_plan(operation, request, video_record_id=video_record_id),
        "public_receipt": _public_plan(operation, request.mode, "REQUEST_VALIDATED"),
    }


def _plan_doctor(payload: Mapping[str, Any]) -> dict[str, Any]:
    profile = _profile(payload)
    request = VideoRequest(operation="video_doctor", mode=ProcessingMode.QUICK, profile=profile)
    return {
        "private_plan": _private_plan(
            request.operation,
            request,
            checks=(
                "artifact_store",
                "queue",
                "subtitle_provider",
                "sona",
                "ollama",
                "frame_tools",
                "ocr",
                "memory_gateway",
            ),
        ),
        "public_receipt": _public_plan(request.operation, request.mode, "DOCTOR_PLAN_VALIDATED"),
    }
