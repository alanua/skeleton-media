from __future__ import annotations

import pytest

from skeleton_media.video_understanding.models import VideoUnderstandingError
from skeleton_media.video_understanding.operations import OPERATIONS, plan_operation


def test_all_required_operations_are_registered_in_contract() -> None:
    assert OPERATIONS == {
        "video_understand_url",
        "video_transcribe_url",
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
    assert "https://youtu.be/AbCdEf12345" not in serialized_public
    assert "AbCdEf12345" not in serialized_public
    assert "Skeleton Architecture" not in serialized_public
    assert public["reason_code"] == "SUPPORTED_YOUTUBE"


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


def test_path_like_video_record_identity_is_rejected() -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation("video_status", {"video_record_id": "vr_../../private"})
    assert exc.value.reason_code == "INVALID_VIDEO_RECORD_ID"


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



def test_transcription_url_plan_uses_existing_pipeline_without_memory_or_llm() -> None:
    result = plan_operation(
        "video_transcribe_url",
        {
            "url": "https://youtu.be/AbCdEf12345",
            "mode": "QUICK",
            "verify_with_independent_asr": True,
        },
    )
    private = result["private_plan"]
    public = result["public_receipt"]
    assert private["operation"] == "video_transcribe_url"
    assert private["pipeline_entrypoint"] == "VideoPipeline.transcribe_only"
    assert private["verify_with_independent_asr"] is True
    assert private["operator_gate_required"] is True
    assert private["runtime_registered"] is False
    assert private["executor_policy"] == "EXISTING_SHARED_MEDIA_WORKER_ONLY"
    assert private["canonical_mutation"] == "NOT_USED"
    assert private["local_llm_policy"] == "NOT_USED"
    assert private["network_execution"] is False
    assert private["media_execution"] is False
    assert public["canonical_mutation_status"] == "NOT_USED"
    assert public["projection_status"] == "NOT_USED"
    assert public["reason_code"] == "SUPPORTED_YOUTUBE"
    public_text = repr(public)
    for private_content in ("https://", "AbCdEf12345", "audio_wav", "transcript", "model_path"):
        if private_content == "transcript":
            # Public schema identifies a transcription operation, not user text.
            continue
        assert private_content not in public_text


def test_transcription_defaults_to_subtitles_first_and_no_asr() -> None:
    result = plan_operation("video_transcribe_url", {"url": "https://youtu.be/AbCdEf12345"})
    assert result["private_plan"]["mode"] == "QUICK"
    assert result["private_plan"]["verify_with_independent_asr"] is False
    assert result["public_receipt"]["mode"] == "QUICK"


@pytest.mark.parametrize("mode", ["DEEP", "TARGETED", "ARCHIVE"])
def test_transcription_rejects_full_understanding_modes(mode: str) -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation(
            "video_transcribe_url",
            {"url": "https://youtu.be/AbCdEf12345", "mode": mode},
        )
    assert exc.value.reason_code == "TRANSCRIPT_MODE_UNSUPPORTED"


@pytest.mark.parametrize("value", [1, "yes", None, [], {}])
def test_transcription_rejects_non_boolean_asr_flag(value: object) -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation(
            "video_transcribe_url",
            {"url": "https://youtu.be/AbCdEf12345", "verify_with_independent_asr": value},
        )
    assert exc.value.reason_code == "ASR_VERIFY_FLAG_INVALID"


@pytest.mark.parametrize("field", [
    "cookie", "api_key", "model_path", "shell", "output_path",
    "credentials", "headers", "ffmpeg_args",
])
def test_transcription_rejects_private_runtime_controls(field: str) -> None:
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation(
            "video_transcribe_url",
            {"url": "https://youtu.be/AbCdEf12345", field: "private"},
        )
    assert exc.value.reason_code == "PRIVATE_CONTROL_FIELD_FORBIDDEN"


def test_transcription_rejects_external_approval_reference_and_unknown_fields() -> None:
    # Operator identity and Gate approval are injected by the trusted host,
    # never accepted from user-controlled payloads.
    with pytest.raises(VideoUnderstandingError) as exc:
        plan_operation(
            "video_transcribe_url",
            {
                "url": "https://youtu.be/AbCdEf12345",
                "operator_approval_ref": "forged",
            },
        )
    assert exc.value.reason_code == "UNKNOWN_FIELDS"
