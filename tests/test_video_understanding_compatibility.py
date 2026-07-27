from __future__ import annotations

import pytest

from core.video_understanding.compatibility import map_dios_operation
from core.video_understanding.models import VideoUnderstandingError


def test_existing_dios_operations_map_to_universal_profile() -> None:
    assert map_dios_operation("dios_video_doctor", {}) == ("video_doctor", {"profile": "DIOS"})
    target, payload = map_dios_operation("dios_video_process_one", {"video_record_id": "vr_synthetic"})
    assert target == "video_process_one" and payload["profile"] == "DIOS"


def test_dios_profile_conflict_and_unknown_operation_fail() -> None:
    with pytest.raises(VideoUnderstandingError):
        map_dios_operation("dios_video_status", {"profile": "TRAVEL"})
    with pytest.raises(VideoUnderstandingError):
        map_dios_operation("other", {})
