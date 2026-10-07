from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from test_skeleton_cast_media_target import FakePlayer, _auto_observe, _install_runtime, _source, cast_app


class ProjectorEndpointAdapter(cast_app.MediaEndpointAdapter):
    endpoint_id = "projector"
    device_id = "projector_01"
    label = "Projector"
    backend = "synthetic-projector"
    family = "projector"
    capabilities = ("handoff",)

    def __init__(self, status_path: Path) -> None:
        self.status_path = status_path
        self.started: list[dict] = []
        self.verified_before_pause = False

    def start_from_capture(self, capture: dict) -> dict:
        _job, source = cast_app._find_job_source(capture.get("job_id"), capture.get("source_id"))
        status = {
            "device_id": self.device_id,
            "source_id": source.get("source_id"),
            "job_id": capture.get("job_id"),
            "position_seconds": capture.get("position_seconds"),
            "playing": not capture.get("paused"),
            "backend": self.backend,
        }
        self.started.append(dict(status))
        cast_app._atomic(self.status_path, status)
        return status

    def verify_destination(self, capture: dict, started: dict) -> dict:
        observed = json.loads(self.status_path.read_text())
        self.verified_before_pause = True
        assert observed["source_id"] == capture["source_id"]
        return observed


class UnsupportedEndpointAdapter(cast_app.MediaEndpointAdapter):
    endpoint_id = "unsupported"
    device_id = "unsupported_01"
    label = "Unsupported"
    backend = "synthetic"
    family = "test"
    capabilities = ()


def _reset_extra_adapters() -> None:
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS.clear()


def test_capture_first_press_charges_source_without_stopping_and_selection_is_neutral(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False, position=77.0)
    _install_runtime(monkeypatch, tmp_path, fake)

    charged = cast_app._charge_media_capture("tv")
    selected = cast_app._select_media_target("samsung")

    assert charged["charged"] is True
    assert charged["charge"]["source_endpoint"] == "tv"
    assert charged["charge"]["source_id"] == "src-1080"
    assert charged["charge"]["position_seconds"] == 77.0
    assert fake.calls == []
    assert fake.status()["playing"] is True
    assert selected["target"] == "samsung"
    assert selected["charge"]["source_id"] == "src-1080"
    assert fake.calls == []


def test_second_press_handoff_verifies_destination_before_pausing_source_and_clears_charge(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False, position=81.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    projector = ProjectorEndpointAdapter(tmp_path / "projector-status.json")
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS["projector"] = projector

    cast_app._charge_media_capture("tv")
    result = cast_app._handoff_capture_to_endpoint("projector")

    assert result["target"] == "projector"
    assert result["charged"] is False
    assert result["charge"] is None
    assert projector.verified_before_pause is True
    assert projector.started[0]["source_id"] == "src-1080"
    assert fake.calls == [("control", "pause")]


def test_handoff_failure_keeps_source_playing_and_charge_authoritative(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False, position=25.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._charge_media_capture("tv")

    with pytest.raises(RuntimeError):
        cast_app._handoff_capture_to_endpoint("samsung")

    state = cast_app._media_target_state()
    assert state["target"] == "tv"
    assert state["charged"] is True
    assert state["charge"]["source_endpoint"] == "tv"
    assert fake.status()["playing"] is True
    assert fake.calls == []


def test_same_source_handoff_is_idempotent_and_does_not_clear_charge(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._charge_media_capture("tv")

    result = cast_app._handoff_capture_to_endpoint("tv")

    assert result["unchanged"] is True
    assert result["charged"] is True
    assert result["charge"]["source_endpoint"] == "tv"
    assert fake.calls == []


def test_capture_and_handoff_actions_are_available_via_target_and_handoff_apis(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(get_json=lambda silent=True: {"action": "capture", "source": "tv"}))
    captured = cast_app.media_target_set()
    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(get_json=lambda silent=True: {"target": "samsung"}))
    moved = cast_app.media_handoff_post()

    assert captured["status"] == "ok"
    assert captured["charged"] is True
    assert moved["status"] == "ok"
    assert moved["target"] == "samsung"
    assert moved["charged"] is False
    assert writes[0]["source_id"] == "src-720"


def test_dynamic_discovery_and_unsupported_capability_failure_are_endpoint_local(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS["unsupported"] = UnsupportedEndpointAdapter()
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    state = cast_app.media_target_get()
    cast_app._charge_media_capture("tv")

    with pytest.raises(RuntimeError):
        cast_app._handoff_capture_to_endpoint("unsupported")

    assert any(item["endpoint_id"] == "unsupported" for item in state["targets"])
    assert cast_app._media_target_state()["charged"] is True
    assert fake.status()["playing"] is True
