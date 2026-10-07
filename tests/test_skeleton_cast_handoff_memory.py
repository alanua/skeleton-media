from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from test_skeleton_cast_media_target import FakePlayer, _auto_observe, _install_runtime, _job, _source, cast_app


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


def test_same_endpoint_second_press_releases_capture_without_mutating_playback(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._charge_media_capture("tv")

    result = cast_app._handoff_capture_to_endpoint("tv")

    assert result["discharged"] is True
    assert result["returned_to_source"] is True
    assert result["charged"] is False
    assert result["charge"] is None
    assert cast_app._media_target_state()["charged"] is False
    assert fake.calls == []


def test_tv_to_samsung_youtube_normalizes_tv_url_and_accepts_revision_mode_ack(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    video_id = "abcDEF12345"
    fake = FakePlayer(paused=False, source_id="youtube-tv", position=95.4)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._save(
        _job(
            [
                _source(
                    "youtube-tv",
                    height=720,
                    quality="720p",
                    kind="youtube",
                    url=f"https://www.youtube.com/tv#/watch?v={video_id}",
                )
            ]
        )
    )
    original_load = cast_app._write_samsung_desired
    writes: list[dict] = []

    def write_load(job: dict, source: dict, *, position: float) -> dict:
        desired = original_load(job, source, position=position)
        cast_app._atomic(
            cast_app.SAMSUNG_RECEIVER_STATUS,
            {
                "schema": "skeleton.samsung.media_status.v1",
                "device_id": cast_app.SAMSUNG_DEVICE_ID,
                "revision": desired["revision"],
                "mode": desired["mode"],
                "app": "Samsung Media Receiver",
            },
        )
        writes.append(dict(desired))
        return desired

    monkeypatch.setattr(cast_app, "_write_samsung_desired", write_load)

    cast_app._charge_media_capture("tv")
    result = cast_app._handoff_capture_to_endpoint("samsung")

    assert result["charged"] is False
    assert writes[0]["mode"] == "youtube"
    assert writes[0]["video_id"] == video_id
    assert writes[0]["url"] == f"https://www.youtube.com/watch?v={video_id}&t=95s"
    assert fake.calls == [("control", "pause")]


def test_samsung_to_tv_youtube_capture_uses_desired_video_id_and_position_fallback(monkeypatch, tmp_path: Path) -> None:
    _reset_extra_adapters()
    video_id = "ZYXwvUT9876"
    fake = FakePlayer(paused=True, source_id="src-1080", position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._save(
        _job(
            [
                _source(
                    "youtube-tv",
                    height=720,
                    quality="720p",
                    kind="youtube",
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    video_id=video_id,
                )
            ]
        )
    )
    cast_app._set_media_target("samsung", reason="test")
    cast_app._atomic(
        cast_app.SAMSUNG_RECEIVER_DESIRED,
        {
            "schema": "skeleton.samsung.media_desired.v1",
            "revision": 7,
            "mode": "youtube",
            "url": f"https://www.youtube.com/watch?v={video_id}&t=44s",
            "video_id": video_id,
            "position_seconds": 44.0,
            "action": "pause",
            "job_id": "a" * 16,
            "source_id": "youtube-tv",
        },
    )
    cast_app._atomic(
        cast_app.SAMSUNG_RECEIVER_STATUS,
        {
            "schema": "skeleton.samsung.media_status.v1",
            "device_id": cast_app.SAMSUNG_DEVICE_ID,
            "revision": 7,
            "mode": "youtube",
            "app": "SmartTube",
        },
    )

    result = cast_app._switch_media_target("tv")

    assert result["target"] == "tv"
    assert fake.calls == [("play", "youtube-tv"), ("seek", 44.0), ("control", "pause")]
    assert fake.status()["video_id"] == video_id
    assert fake.status()["pause"] is True


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
