from __future__ import annotations

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.operations import OPERATIONS, plan_operation


def test_all_required_operations_are_registered_in_contract() -> None:
    assert OPERATIONS == {
        "video_understand_url",
        "video_import_urls",
        "video_process_one",
        "video_status",
        "video_doctor",
        "video_query",
        "video_reprocess",
        "video_attach_to_project",
    }


def test_understand_url_returns_private_plan_and_sanitized_receipt() -> None:
    result = plan_operation(
        "video_understand_url",
        {
            "url": "https://youtu.be/AbCdEf12345",
            "mode": "STANDARD",
            "project_hint": "Skeleton Architecture",
        },
    )
    private_plan = result["private_plan"]
    public = result["public_receipt"]
    assert private_plan["source"]["adapter"] == "youtube"
    assert private_plan["local_llm_policy"] == "REQUIRED_FOR_SYNTHESIS"
    assert private_plan["canonical_mutation"] == "MEMORY_GATEWAY_ONLY"
    assert private_plan["network_execution"] is False
    serialized_public = repr(public)
    assert "youtu" not in serialized_public.casefold()
    assert "AbCdEf12345" not in serialized_public
    assert "Skeleton Architecture" not in serialized_public


def test_targeted_mode_requires_question() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation(
            "video_understand_url",
            {"url": "https://youtu.be/AbCdEf12345", "mode": "TARGETED"},
        )
    assert exc.value.reason_code == "QUESTION_REQUIRED"


@pytest.mark.parametrize(
    "field",
    [
        "shell",
        "ffmpeg_args",
        "yt_dlp_args",
        "output_path",
        "model_path",
        "cookies",
        "api_key",
    ],
)
def test_private_runtime_control_fields_are_rejected(field: str) -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation(
            "video_understand_url",
            {"url": "https://youtu.be/AbCdEf12345", field: "private"},
        )
    assert exc.value.reason_code == "PRIVATE_CONTROL_FIELD_FORBIDDEN"


def test_unknown_fields_and_unbounded_batch_are_rejected() -> None:
    with pytest.raises(VideoUnderstandingError) as unknown:
        plan_operation("video_status", {"video_record_id": "vr_synthetic", "extra": 1})
    assert unknown.value.reason_code == "UNKNOWN_FIELDS"
    with pytest.raises(VideoUnderstandingError) as batch:
        plan_operation("video_import_urls", {"urls": ["https://vimeo.com/123456"] * 101})
    assert batch.value.reason_code == "INVALID_BATCH"


def test_doctor_checks_ollama_and_sona_without_running_them() -> None:
    result = plan_operation("video_doctor", {})
    checks = result["private_plan"]["checks"]
    assert "ollama" in checks
    assert "sona" in checks
    assert result["private_plan"]["network_execution"] is False
    assert result["private_plan"]["media_execution"] is False


def test_dios_compatibility_profile_is_supported() -> None:
    result = plan_operation("video_doctor", {"profile": "DIOS"})
    assert result["private_plan"]["profile"] == "DIOS"
