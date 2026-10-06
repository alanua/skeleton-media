from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_skeleton_cast_media_target import (  # noqa: E402
    FakePlayer,
    _auto_observe,
    _install_runtime,
    _job,
    _source,
    cast_app,
)


def test_first_press_captures_snapshot_and_selection_is_playback_neutral(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, position=40.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)

    charged = cast_app._media_handoff_press("tv", owner_device_id="controller")
    fake.state["time-pos"] = 99.0
    selected = [cast_app._switch_media_target(target) for target in ("samsung", "tv", "samsung")]

    assert charged["status"] == "charged"
    assert charged["capture"]["position_seconds"] == 40.0
    assert fake.status()["playing"] is True
    assert fake.calls == []
    assert writes == []
    assert [item["target"] for item in selected] == ["samsung", "tv", "samsung"]
    assert cast_app._media_handoff_state()["charged"] is True


def test_second_press_discharges_only_to_final_selected_destination_with_captured_position(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, position=12.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)
    adapted: list[tuple[str, str]] = []
    original_adapt = cast_app._adapt_source_for_destination

    def adapt(destination: str, job: dict, source: dict) -> dict:
        adapted.append((destination, source["source_id"]))
        return original_adapt(destination, job, source)

    monkeypatch.setattr(cast_app, "_adapt_source_for_destination", adapt)

    cast_app._media_handoff_press("tv", owner_device_id="controller")
    fake.state["time-pos"] = 88.0
    for target in ("tv", "samsung", "tv", "samsung"):
        cast_app._switch_media_target(target)
    result = cast_app._media_handoff_press("samsung", owner_device_id="controller")

    assert result["status"] == "discharged"
    assert result["target"] == "samsung"
    assert writes[0]["source_id"] == "src-720"
    assert writes[0]["position_seconds"] == 12.0
    assert adapted == [("samsung", "src-1080")]
    assert fake.calls == [("control", "pause")]
    assert cast_app._media_handoff_state()["charged"] is False
    assert result["handoff"]["source_endpoint"] == "tv"
    assert result["handoff"]["destination_endpoint"] == "samsung"


def test_successful_discharge_retry_does_not_create_new_capture_or_rewrite_destination(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, position=15.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)

    cast_app._media_handoff_press("tv", owner_device_id="controller")
    cast_app._switch_media_target("samsung")
    first = cast_app._media_handoff_press("samsung", owner_device_id="controller")
    retry = cast_app._media_handoff_press("samsung", owner_device_id="controller")

    assert first["status"] == "discharged"
    assert retry["status"] == "discharged"
    assert retry["unchanged"] is True
    assert len(writes) == 1
    assert fake.calls == [("control", "pause")]


def test_destination_verification_precedes_source_pause(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, position=55.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    original_write = cast_app._write_samsung_desired
    seen_before_pause: list[list[tuple[str, object]]] = []

    def write_load(job: dict, source: dict, *, position: float) -> dict:
        desired = original_write(job, source, position=position)
        cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, {
            "device_id": cast_app.SAMSUNG_DEVICE_ID,
            "revision": desired["revision"],
            "mode": "video",
            "playing": True,
            "position_seconds": position,
            "source_id": desired["source_id"],
            "job_id": desired["job_id"],
        })
        return desired

    def wait_for_verified(desired: dict) -> dict:
        seen_before_pause.append(list(fake.calls))
        return cast_app._samsung_receiver_status()

    monkeypatch.setattr(cast_app, "_write_samsung_desired", write_load)
    monkeypatch.setattr(cast_app, "_wait_for_samsung_postcondition", wait_for_verified)

    cast_app._media_handoff_press("tv", owner_device_id="controller")
    cast_app._switch_media_target("samsung")
    cast_app._media_handoff_press("samsung", owner_device_id="controller")

    assert seen_before_pause == [[]]
    assert fake.calls == [("control", "pause")]


def test_failed_destination_start_preserves_source_and_pending_capture(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, position=90.0)
    _install_runtime(monkeypatch, tmp_path, fake)

    cast_app._media_handoff_press("tv", owner_device_id="controller")
    cast_app._switch_media_target("samsung")

    with pytest.raises(RuntimeError):
        cast_app._media_handoff_press("samsung", owner_device_id="controller")

    assert fake.status()["playing"] is True
    assert fake.calls == []
    assert cast_app._media_handoff_state()["charged"] is True


def test_explicit_clear_and_cross_controller_guard_are_playback_neutral(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, position=21.0)
    _install_runtime(monkeypatch, tmp_path, fake)

    cast_app._media_handoff_press("tv", owner_device_id="controller-a")
    with pytest.raises(RuntimeError):
        cast_app._media_handoff_press("samsung", owner_device_id="controller-b")

    cleared = cast_app._clear_media_handoff(owner_device_id="controller-a")

    assert cleared["charged"] is False
    assert fake.status()["playing"] is True
    assert fake.calls == []


def test_samsung_to_tv_failure_preserves_samsung_and_capture(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._set_media_target("samsung", reason="test")
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_DESIRED, {
        "schema": "skeleton.samsung.media_desired.v1",
        "revision": 8,
        "mode": "video",
        "url": "https://media.example/src-720.mp4",
        "position_seconds": 66.0,
        "action": "play",
        "action_id": "a1",
        "updated_at": 1,
        "job_id": "a" * 16,
        "source_id": "src-720",
    })
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, {
        "device_id": cast_app.SAMSUNG_DEVICE_ID,
        "revision": 8,
        "mode": "video",
        "position_seconds": 66.0,
        "playing": True,
        "source_id": "src-720",
        "job_id": "a" * 16,
    })
    monkeypatch.setattr(cast_app, "_wait_for_tv_postcondition", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("tv failed")))

    cast_app._media_handoff_press("samsung", owner_device_id="controller")
    cast_app._switch_media_target("tv")
    with pytest.raises(RuntimeError):
        cast_app._media_handoff_press("tv", owner_device_id="controller")

    assert cast_app._media_handoff_state()["charged"] is True
    assert json.loads(cast_app.SAMSUNG_RECEIVER_DESIRED.read_text())["action"] == "play"


def test_synthetic_third_endpoint_selection_proves_n_endpoint_shape(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    labels = dict(cast_app.TARGET_LABELS)
    labels["projector"] = "Projector"
    monkeypatch.setattr(cast_app, "TARGET_LABELS", labels)
    writes = _auto_observe(monkeypatch)

    cast_app._media_handoff_press("tv", owner_device_id="controller")
    result = cast_app._switch_media_target("projector")

    assert result["target"] == "projector"
    assert fake.calls == []
    assert writes == []
    assert cast_app._media_handoff_state()["charged"] is True
