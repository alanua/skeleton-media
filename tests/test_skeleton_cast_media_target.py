from __future__ import annotations

import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAST_RUNTIME = ROOT / "ops" / "skeleton_cast" / "runtime"
sys.path.insert(0, str(CAST_RUNTIME))

try:  # pragma: no cover - exercised only in minimal runner Python images.
    import flask  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    flask_stub = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, *_args, **_kwargs) -> None:
            self.config = {}

        def get(self, *_args, **_kwargs):
            return lambda func: func

        def post(self, *_args, **_kwargs):
            return lambda func: func

        def after_request(self, func):
            return func

        def run(self, *_args, **_kwargs) -> None:
            return None

    class _Abort(Exception):
        pass

    def _abort(code: int, description: str | None = None):
        raise _Abort(f"{code}: {description or ''}")

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = dict
    flask_stub.abort = _abort
    flask_stub.jsonify = lambda *args, **kwargs: args[0] if args else kwargs
    flask_stub.request = types.SimpleNamespace(remote_addr="127.0.0.1", args={}, get_json=lambda silent=True: {})
    flask_stub.send_from_directory = lambda *args, **kwargs: {}
    sys.modules["flask"] = flask_stub

player_stub = types.ModuleType("player")
player_stub.status = lambda: {"running": False}
player_stub.play = lambda *_args, **_kwargs: {}
player_stub.seek_absolute = lambda *_args, **_kwargs: {}
player_stub.control = lambda *_args, **_kwargs: {}
player_stub.mode_status = lambda *args, **kwargs: {"mode": "mpv"}
player_stub.switch_mode = lambda mode: {"mode": mode}
player_stub.command = lambda *_args, **_kwargs: {"error": "success"}
sys.modules.setdefault("player", player_stub)

resolver_stub = types.ModuleType("resolver")

class _ResolverError(Exception):
    pass

resolver_stub.BrowserChallengeError = _ResolverError
resolver_stub.OriginProtectedError = _ResolverError
resolver_stub.resolve_page = lambda url: {"title": url, "sources": []}
sys.modules.setdefault("resolver", resolver_stub)

import app as cast_app  # noqa: E402


JOB_ID = "a" * 16


class FakePlayer:
    def __init__(self, *, paused: bool = False, source_id: str = "src-1080", position: float = 123.0) -> None:
        self.calls: list[tuple[str, object]] = []
        self.state = {
            "running": True,
            "job_id": JOB_ID,
            "source_id": source_id,
            "time-pos": position,
            "pause": paused,
            "playing": not paused,
        }

    def status(self) -> dict:
        return dict(self.state)

    def play(self, job: dict, source: dict, subtitles: str = "off") -> dict:
        self.calls.append(("play", source.get("source_id")))
        self.state.update(
            {
                "running": True,
                "job_id": job.get("job_id"),
                "source_id": source.get("source_id"),
                "time-pos": 0.0,
                "pause": False,
                "playing": True,
            }
        )
        return {"accepted": True}

    def seek_absolute(self, position: float) -> dict:
        self.calls.append(("seek", position))
        self.state["time-pos"] = float(position)
        return {"player": self.status()}

    def control(self, action: str) -> dict:
        self.calls.append(("control", action))
        paused = action == "pause"
        self.state["pause"] = paused
        self.state["playing"] = not paused
        return {"player": self.status()}


def _job(sources: list[dict] | None = None) -> dict:
    return {
        "job_id": JOB_ID,
        "status": "ready",
        "title": "Known Show",
        "sources": sources
        or [
            _source("src-1080", height=1080, quality="1080p"),
            _source("src-720", height=720, quality="720p"),
            _source("src-480", height=480, quality="480p"),
            _source("youtube-hop", height=720, quality="720p", kind="youtube"),
        ],
    }


def _source(
    source_id: str,
    *,
    height: int,
    quality: str,
    translation: str = "Ukrainian",
    episode: str = "5",
    kind: str = "search-release",
    video_codec: str = "h264",
    audio_codec: str = "aac",
) -> dict:
    return {
        "source_id": source_id,
        "url": f"https://media.example/{source_id}.mp4",
        "kind": kind,
        "quality": quality,
        "height": height,
        "translation": translation,
        "group": translation,
        "season": "2",
        "episode": episode,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "headers": {"Referer": "https://site.example/show"},
    }


def _install_runtime(monkeypatch, tmp_path: Path, fake_player: FakePlayer) -> None:
    state = tmp_path / "state"
    jobs = state / "jobs"
    jobs.mkdir(parents=True)
    monkeypatch.setattr(cast_app, "STATE", state)
    monkeypatch.setattr(cast_app, "JOBS", jobs)
    monkeypatch.setattr(cast_app, "MEDIA_TARGET_STATE", state / "media-target.json")
    monkeypatch.setattr(cast_app, "SAMSUNG_RECEIVER_DESIRED", state / "samsung-receiver-desired.json")
    monkeypatch.setattr(cast_app, "SAMSUNG_RECEIVER_STATUS", state / "samsung-receiver-status.json")
    monkeypatch.setattr(cast_app, "MEDIA_TARGET_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(cast_app, "_require", lambda: None)
    monkeypatch.setattr(cast_app, "player", fake_player)
    cast_app._save(_job())


def _auto_ack(monkeypatch, *, playing: bool | None = None, paused: bool | None = None) -> list[dict]:
    original = cast_app._write_samsung_desired
    writes: list[dict] = []

    def wrapped(job: dict, source: dict, *, position: float, paused: bool) -> dict:
        desired = original(job, source, position=position, paused=paused)
        media = dict(desired["media"])
        media["position_seconds"] = position + 2.0
        media["pause"] = paused if paused is not None else bool(media.get("paused"))
        media["playing"] = (not paused) if playing is None else playing
        if paused is not None:
            media["paused"] = paused
        cast_app._atomic(
            cast_app.SAMSUNG_RECEIVER_STATUS,
            {
                "device_id": cast_app.SAMSUNG_DEVICE_ID,
                "ack_revision": desired["revision"],
                "media": media,
            },
        )
        writes.append(desired)
        return desired

    monkeypatch.setattr(cast_app, "_write_samsung_desired", wrapped)
    return writes


def test_playing_tv_to_samsung_waits_for_receiver_ack_and_uses_720p_same_voice(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_ack(monkeypatch)

    result = cast_app._switch_media_target("samsung")

    assert result["target"] == "samsung"
    assert result["device_id"] == "samsung_kiosk"
    assert result["label"] == "Samsung Kiosk"
    assert writes[0]["media"]["source_id"] == "src-720"
    assert writes[0]["media"]["height"] == 720
    assert writes[0]["media"]["translation"] == "Ukrainian"
    assert writes[0]["media"]["paused"] is False


def test_paused_tv_to_samsung_requires_observable_paused_destination_before_active_target(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, position=222.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    _auto_ack(monkeypatch)

    result = cast_app._switch_media_target("samsung")

    assert result["target"] == "samsung"
    desired = json.loads(cast_app.SAMSUNG_RECEIVER_DESIRED.read_text())
    assert desired["media"]["paused"] is True
    assert desired["media"]["playback_state"] == "paused"
    assert abs(desired["media"]["position_seconds"] - 222.0) <= 0.01


def test_playing_samsung_to_tv_transfers_identity_source_timecode_and_play_state(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, source_id="src-1080", position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._set_media_target("samsung", reason="test")
    cast_app._atomic(
        cast_app.SAMSUNG_RECEIVER_STATUS,
        {
            "device_id": "samsung_kiosk",
            "ack_revision": 5,
            "media": {"job_id": JOB_ID, "source_id": "src-720", "position_seconds": 321.0, "playing": True, "pause": False},
        },
    )

    result = cast_app._switch_media_target("tv")

    assert result["target"] == "tv"
    assert fake.calls == [("play", "src-720"), ("seek", 321.0), ("control", "play")]
    assert fake.status()["playing"] is True
    assert abs(fake.status()["time-pos"] - 321.0) <= 0.01


def test_paused_samsung_to_tv_keeps_destination_paused_before_active_target(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, source_id="src-1080", position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._set_media_target("samsung", reason="test")
    cast_app._atomic(
        cast_app.SAMSUNG_RECEIVER_STATUS,
        {
            "device_id": "samsung_kiosk",
            "ack_revision": 5,
            "media": {"job_id": JOB_ID, "source_id": "src-720", "position_seconds": 44.0, "playing": False, "pause": True},
        },
    )

    result = cast_app._switch_media_target("tv")

    assert result["target"] == "tv"
    assert fake.calls[-1] == ("control", "pause")
    assert fake.status()["pause"] is True


def test_destination_failure_leaves_previous_session_recoverable_and_target_truthful(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, position=90.0)
    _install_runtime(monkeypatch, tmp_path, fake)

    try:
        cast_app._switch_media_target("samsung")
    except RuntimeError:
        pass
    else:
        raise AssertionError("handoff should fail without receiver ACK")

    assert cast_app._media_target_state()["target"] == "tv"
    assert fake.status()["source_id"] == "src-1080"
    assert fake.status()["pause"] is True


def test_duplicate_target_selection_is_idempotent_and_does_not_rewrite_receiver_revision(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_ack(monkeypatch)

    first = cast_app._switch_media_target("samsung")
    second = cast_app._switch_media_target("samsung")

    assert first["target"] == "samsung"
    assert second["unchanged"] is True
    assert len(writes) == 1
    assert json.loads(cast_app.SAMSUNG_RECEIVER_DESIRED.read_text())["revision"] == writes[0]["revision"]


def test_no_youtube_hop_for_non_youtube_media_and_no_volume_reset(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._save(_job([_source("src-1080", height=1080, quality="1080p"), _source("youtube-hop", height=720, quality="720p", kind="youtube")]))
    monkeypatch.setattr(cast_app, "_set_volume", lambda level: (_ for _ in ()).throw(AssertionError("volume changed")))

    try:
        cast_app._switch_media_target("samsung")
    except RuntimeError as exc:
        assert "сумісного потоку" in str(exc)
    else:
        raise AssertionError("non-YouTube media must not use a YouTube intermediary")
    assert cast_app._media_target_state()["target"] == "tv"


def test_wired_capability_is_not_degraded_by_wifi_policy(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.STATE / "samsung-tablet-capabilities.json", {"connection": "wired", "wifi_max_height": 480})
    writes = _auto_ack(monkeypatch)

    cast_app._switch_media_target("samsung")

    assert writes[0]["media"]["source_id"] == "src-720"


def test_receiver_status_endpoint_uses_stable_device_id_not_ip(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)

    monkeypatch.setattr(
        cast_app,
        "request",
        types.SimpleNamespace(get_json=lambda silent=True: {"device_id": "192.0.2.44", "ack_revision": 1}),
    )
    rejected = cast_app.samsung_status_post()
    monkeypatch.setattr(
        cast_app,
        "request",
        types.SimpleNamespace(get_json=lambda silent=True: {"device_id": "samsung_kiosk", "ack_revision": 2}),
    )
    accepted = cast_app.samsung_status_post()

    assert rejected[1] == 404
    assert accepted["status"] == "ok"
    assert json.loads(cast_app.SAMSUNG_RECEIVER_STATUS.read_text())["device_id"] == "samsung_kiosk"
