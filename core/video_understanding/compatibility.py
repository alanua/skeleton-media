from __future__ import annotations

from typing import Any, Mapping

from core.video_understanding.models import VideoUnderstandingError


DIOS_OPERATION_MAP = {
    "dios_video_doctor": "video_doctor",
    "dios_video_status": "video_status",
    "dios_video_process_one": "video_process_one",
}


def map_dios_operation(operation: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    target = DIOS_OPERATION_MAP.get(operation)
    if target is None:
        raise VideoUnderstandingError("DIOS_OPERATION_UNSUPPORTED", "DIOS operation is unsupported")
    if not isinstance(payload, Mapping):
        raise VideoUnderstandingError("INVALID_PAYLOAD", "DIOS payload must be an object")
    mapped = dict(payload)
    existing = mapped.get("profile")
    if existing not in (None, "DIOS"):
        raise VideoUnderstandingError("DIOS_PROFILE_CONFLICT", "DIOS compatibility profile conflicts")
    mapped["profile"] = "DIOS"
    return target, mapped
