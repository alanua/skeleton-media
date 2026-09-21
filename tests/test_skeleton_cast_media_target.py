from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


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
            self._rules = []
            self.url_map = types.SimpleNamespace(iter_rules=lambda: iter(self._rules))

        def _route(self, rule, method):
            self._rules.append(types.SimpleNamespace(rule=rule, methods={method}))
            return lambda func: func

        def get(self, rule, *_args, **_kwargs):
            return self._route(rule, "GET")

        def post(self, rule, *_args, **_kwargs):
            return self._route(rule, "POST")

        def put(self, rule, *_args, **_kwargs):
            return self._route(rule, "PUT")

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

resolver_stub = types.ModuleType("resolver")


class _ResolverError(Exception):
    pass


resolver_stub.BrowserChallengeError = _ResolverError
resolver_stub.OriginProtectedError = _ResolverError
resolver_stub.resolve_page = lambda url: {"title": url, "sources": []}

_MISSING = object()
_prior_player = sys.modules.get("player", _MISSING)
_prior_resolver = sys.modules.get("resolver", _MISSING)
sys.modules["player"] = player_stub
sys.modules["resolver"] = resolver_stub
try:
    import app as cast_app  # noqa: E402
finally:
    for _name, _prior in (("player", _prior_player), ("resolver", _prior_resolver)):
        if _prior is _MISSING:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _prior


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
    monkeypatch.setattr(cast_app, "SAMSUNG_MEDIA_DESIRED", state / "samsung-media-desired.json")
    monkeypatch.setattr(cast_app, "SAMSUNG_MEDIA_STATUS", state / "samsung-media-status.json")
    monkeypatch.setattr(cast_app, "SAMSUNG_MEDIA_MODE", state / "samsung-media-mode.json")
    monkeypatch.setattr(cast_app, "SAMSUNG_RECEIVER_DESIRED", state / "samsung-media-desired.json")
    monkeypatch.setattr(cast_app, "SAMSUNG_RECEIVER_STATUS", state / "samsung-media-status.json")
    monkeypatch.setattr(cast_app, "MEDIA_TARGET_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(cast_app, "_require", lambda: None)
    monkeypatch.setattr(cast_app, "_request_device_id", lambda: cast_app.SAMSUNG_DEVICE_ID)
    monkeypatch.setattr(cast_app, "player", fake_player)
    cast_app._save(_job())


def _install_identity_registry(monkeypatch, tmp_path: Path) -> None:
    registry = tmp_path / "confirmed.yaml"
    registry.write_text(
        """
devices:
  samsung_kiosk:
    operator_confirmed: true
    role: tablet_kiosk
    identifiers:
      ipv4: 192.0.2.20
      tailscale_ipv4: 100.64.0.20
      mac: aa:bb:cc:dd:ee:20
    lan_identities:
      wired:
        ipv4: 192.0.2.21
        mac: aa:bb:cc:dd:ee:21
  family_phone_01:
    operator_confirmed: true
    role: android_phone
    identifiers:
      ipv4: 192.0.2.44
      tailscale_ipv4: 100.64.0.44
      mac: aa:bb:cc:dd:ee:44
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cast_app, "REGISTRY", registry)
    monkeypatch.setattr(cast_app, "TRUSTED_CLIENT_IDS", ("family_phone_01",))
    monkeypatch.setattr(cast_app, "_neighbor_mac", lambda _ip: None)


def _install_samsung_status_state(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(cast_app, "SAMSUNG_RECEIVER_STATUS", state / "samsung-media-status.json")


def _status_for(desired: dict, *, playing: bool, position: float | None = None) -> dict:
    return {
        "schema": "skeleton.samsung.media_status.v1",
        "device_id": cast_app.SAMSUNG_DEVICE_ID,
        "mode": desired.get("mode") or "video",
        "revision": desired["revision"],
        "playing": playing,
        "position_seconds": desired.get("position_seconds") if position is None else position,
        "duration_seconds": 1800.0,
        "app": "Samsung Media Receiver",
        "source_id": desired.get("source_id"),
        "job_id": desired.get("job_id"),
    }


def _auto_observe(monkeypatch) -> list[dict]:
    original_load = cast_app._write_samsung_desired
    original_action = cast_app._write_samsung_action
    writes: list[dict] = []

    def write_load(job: dict, source: dict, *, position: float) -> dict:
        desired = original_load(job, source, position=position)
        cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, _status_for(desired, playing=True, position=position + 2.0))
        writes.append(dict(desired))
        return desired

    def write_action(action: str) -> dict:
        desired = original_action(action)
        cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, _status_for(desired, playing=action != "pause"))
        writes.append(dict(desired))
        return desired

    monkeypatch.setattr(cast_app, "_write_samsung_desired", write_load)
    monkeypatch.setattr(cast_app, "_write_samsung_action", write_action)
    return writes


def test_canonical_samsung_routes_and_put_media_target_are_registered() -> None:
    rules = {(rule.rule, tuple(sorted(rule.methods))) for rule in cast_app.app.url_map.iter_rules()}

    assert any(rule == "/api/samsung/media/desired" and "GET" in methods for rule, methods in rules)
    assert any(rule == "/api/samsung/media/status" and "GET" in methods for rule, methods in rules)
    assert any(rule == "/api/samsung/media/status" and "POST" in methods for rule, methods in rules)
    assert any(rule == "/api/samsung/media/mode/<mode>" and "POST" in methods for rule, methods in rules)
    assert any(rule == "/api/samsung/media/control/<action>" and "POST" in methods for rule, methods in rules)
    assert any(rule == "/api/media/target" and "PUT" in methods for rule, methods in rules)
    assert not any(rule in {"/api/samsung/desired", "/api/samsung/status"} for rule, _methods in rules)


def test_samsung_registry_ip_resolves_to_samsung_device_id(monkeypatch, tmp_path: Path) -> None:
    _install_identity_registry(monkeypatch, tmp_path)

    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(remote_addr="192.0.2.20"))
    assert cast_app._request_device_id() == "samsung_kiosk"

    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(remote_addr="192.0.2.21"))
    assert cast_app._request_device_id() == "samsung_kiosk"


def test_different_trusted_phone_resolves_to_own_id_and_cannot_post_samsung_status(monkeypatch, tmp_path: Path) -> None:
    _install_identity_registry(monkeypatch, tmp_path)
    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(remote_addr="192.0.2.44"))
    assert cast_app._request_device_id() == "family_phone_01"

    monkeypatch.setattr(
        cast_app,
        "request",
        types.SimpleNamespace(
            remote_addr="192.0.2.44",
            get_json=lambda silent=True: {"device_id": "samsung_kiosk", "revision": 3, "playing": True},
        ),
    )

    with pytest.raises(Exception) as exc:
        cast_app.samsung_status_post()
    assert getattr(exc.value, "code", None) == 403 or "403" in str(exc.value)


def test_client_supplied_device_id_cannot_override_resolved_samsung_identity(monkeypatch, tmp_path: Path) -> None:
    _install_identity_registry(monkeypatch, tmp_path)
    _install_samsung_status_state(monkeypatch, tmp_path)
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)
    monkeypatch.setattr(
        cast_app,
        "request",
        types.SimpleNamespace(
            remote_addr="192.0.2.20",
            get_json=lambda silent=True: {"device_id": "family_phone_01", "revision": 7, "mode": "video", "playing": True, "position_seconds": 3.0, "app": "receiver"},
        ),
    )

    accepted = cast_app.samsung_status_post()

    assert accepted["status"] == "ok"
    stored = json.loads(cast_app.SAMSUNG_RECEIVER_STATUS.read_text())
    assert stored["device_id"] == "samsung_kiosk"
    assert stored["revision"] == 7


def test_playing_tv_to_samsung_sends_flat_720p_same_voice_and_then_pauses_tv(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)

    result = cast_app._switch_media_target("samsung")

    desired = writes[0]
    assert result["target"] == "samsung"
    assert result["device_id"] == "samsung_kiosk"
    assert desired["schema"] == "skeleton.samsung.media_desired.v1"
    assert "media" not in desired
    assert desired["revision"] == 1
    assert desired["action"] == "play"
    assert desired["source_id"] == "src-720"
    assert desired["quality"] == "720p"
    assert desired["translation"] == "Ukrainian"
    assert desired["episode"] == "5"
    assert fake.calls == [("control", "pause")]


def test_paused_tv_to_samsung_loads_then_same_revision_pause_before_target_mutation(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, position=222.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)

    result = cast_app._switch_media_target("samsung")

    assert result["target"] == "samsung"
    assert [item["action"] for item in writes] == ["play", "pause"]
    assert writes[0]["revision"] == writes[1]["revision"]
    assert writes[0]["action_id"] != writes[1]["action_id"]
    assert abs(writes[0]["position_seconds"] - 222.0) <= 0.01
    assert fake.calls == []
    observed = result["observed"]
    assert observed["playing"] is False
    assert observed["revision"] == writes[1]["revision"]


def test_action_only_pause_play_keeps_revision_stable(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    desired = cast_app._write_samsung_desired(_job(), _source("src-720", height=720, quality="720p"), position=11.0)

    paused = cast_app._write_samsung_action("pause")
    played = cast_app._write_samsung_action("play")

    assert paused["revision"] == desired["revision"]
    assert played["revision"] == desired["revision"]
    assert len({desired["action_id"], paused["action_id"], played["action_id"]}) == 3


def test_playing_samsung_to_tv_transfers_state_then_pauses_samsung_same_revision(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, source_id="src-1080", position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._set_media_target("samsung", reason="test")
    cast_app._atomic(
        cast_app.SAMSUNG_RECEIVER_DESIRED,
        {
            "schema": "skeleton.samsung.media_desired.v1",
            "revision": 5,
            "mode": "video",
            "url": "https://media.example/src-720.mp4",
            "position_seconds": 321.0,
            "action": "play",
            "action_id": "a1",
            "updated_at": 1,
            "job_id": JOB_ID,
            "source_id": "src-720",
        },
    )
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, {"device_id": "samsung_kiosk", "revision": 5, "mode": "video", "position_seconds": 321.0, "playing": True, "app": "receiver"})
    actions = _auto_observe(monkeypatch)

    result = cast_app._switch_media_target("tv")

    assert result["target"] == "tv"
    assert fake.calls == [("play", "src-720"), ("seek", 321.0), ("control", "play")]
    assert actions[-1]["action"] == "pause"
    assert actions[-1]["revision"] == 5
    assert fake.status()["playing"] is True


def test_paused_samsung_to_tv_keeps_destination_paused_without_pausing_samsung_again(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False, source_id="src-1080", position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._set_media_target("samsung", reason="test")
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_DESIRED, {"revision": 5, "mode": "video", "job_id": JOB_ID, "source_id": "src-720", "action": "pause", "action_id": "p1"})
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, {"device_id": "samsung_kiosk", "revision": 5, "mode": "video", "position_seconds": 44.0, "playing": False, "app": "receiver"})
    actions = _auto_observe(monkeypatch)

    result = cast_app._switch_media_target("tv")

    assert result["target"] == "tv"
    assert fake.calls == [("play", "src-720"), ("seek", 44.0), ("control", "pause")]
    assert actions == []
    assert fake.status()["pause"] is True


def test_stale_previous_revision_playback_cannot_satisfy_new_media_postcondition(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, {"device_id": "samsung_kiosk", "revision": 1, "mode": "video", "playing": True, "position_seconds": 10.0, "app": "receiver"})
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)
    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(get_json=lambda silent=True: {"revision": 2, "mode": "video", "app": "receiver"}))

    accepted = cast_app.samsung_status_post()

    stored = json.loads(cast_app.SAMSUNG_RECEIVER_STATUS.read_text())
    assert accepted["status"] == "ok"
    assert "playing" not in stored
    assert "position_seconds" not in stored
    assert cast_app._destination_verified(stored, revision=2, source_id="src-720", position=10.0, paused=False) is False


def test_status_endpoint_uses_revision_and_trusted_device_not_client_json_or_ip(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(cast_app, "_request_device_id", lambda: cast_app.SAMSUNG_DEVICE_ID)
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)
    monkeypatch.setattr(
        cast_app,
        "request",
        types.SimpleNamespace(
            remote_addr="192.0.2.44",
            get_json=lambda silent=True: {"device_id": "client-lie", "ack_revision": 99, "revision": 7, "mode": "video", "playing": True, "position_seconds": 3.0, "app": "receiver"},
        ),
    )

    accepted = cast_app.samsung_status_post()

    assert accepted["status"] == "ok"
    stored = json.loads(cast_app.SAMSUNG_RECEIVER_STATUS.read_text())
    assert stored["device_id"] == "samsung_kiosk"
    assert stored["revision"] == 7
    assert "ack_revision" not in stored


def test_destination_failure_leaves_previous_session_recoverable_and_target_truthful(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=True, position=90.0)
    _install_runtime(monkeypatch, tmp_path, fake)

    try:
        cast_app._switch_media_target("samsung")
    except RuntimeError:
        pass
    else:
        raise AssertionError("handoff should fail without receiver heartbeat")

    assert cast_app._media_target_state()["target"] == "tv"
    assert fake.status()["source_id"] == "src-1080"
    assert fake.status()["pause"] is True


def test_duplicate_target_selection_is_idempotent_and_does_not_rewrite_receiver_revision(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(paused=False)
    _install_runtime(monkeypatch, tmp_path, fake)
    writes = _auto_observe(monkeypatch)

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
    writes = _auto_observe(monkeypatch)

    cast_app._switch_media_target("samsung")

    assert writes[0]["source_id"] == "src-720"


def _install_controller(monkeypatch, responses: dict[tuple[str, ...], dict], calls: list[tuple[str, ...]]) -> None:
    controller = Path("/tmp/test-skeleton-samsung-media-controller")
    monkeypatch.setattr(cast_app, "SAMSUNG_MEDIA_CONTROLLER", controller)

    def run(cmd, **_kwargs):
        assert cmd[0] == str(controller)
        assert "/usr/bin/adb" not in cmd
        args = tuple(cmd[1:])
        calls.append(args)
        payload = responses.get(args, {})
        return types.SimpleNamespace(returncode=int(payload.pop("_returncode", 0)) if "_returncode" in payload else 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cast_app.subprocess, "run", run)
    monkeypatch.setattr(cast_app, "_run_adb_key", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("generic adb key routing used")))


def test_samsung_mode_youtube_is_requested_then_applied_only_after_smarttube_foreground_and_clears_stale_video_error(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_MEDIA_MODE, {"applied_mode": "video", "requested_mode": "video", "video_error": "stale"})
    cast_app._atomic(cast_app.SAMSUNG_RECEIVER_STATUS, {"device_id": "samsung_kiosk", "revision": 3, "mode": "video", "video_error": "stale"})
    calls: list[tuple[str, ...]] = []
    _install_controller(
        monkeypatch,
        {
            ("mode", "youtube"): {"accepted": True},
            ("youtube-status",): {"active": True, "foreground_package": "org.smarttube.stable"},
        },
        calls,
    )
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    body, code = cast_app.samsung_media_mode_post("youtube")

    assert code == 200
    assert body["requested_mode"] == "youtube"
    assert body["applied_mode"] == "youtube"
    assert body["transition"] == "verified"
    assert calls == [("mode", "youtube"), ("youtube-status",)]
    assert "video_error" not in json.loads(cast_app.SAMSUNG_MEDIA_MODE.read_text())
    assert "video_error" not in json.loads(cast_app.SAMSUNG_RECEIVER_STATUS.read_text())


def test_samsung_mode_video_from_youtube_requires_receiver_current_focus(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_MEDIA_MODE, {"applied_mode": "youtube", "requested_mode": "youtube", "transition": "verified"})
    calls: list[tuple[str, ...]] = []
    _install_controller(
        monkeypatch,
        {
            ("mode", "video"): {"accepted": True},
            ("status",): {"current_focus": "Window{42 ua.homeedge.mediareceiver/.MainActivity}"},
        },
        calls,
    )
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    body, code = cast_app.samsung_media_mode_post("video")

    assert code == 200
    assert body["requested_mode"] == "video"
    assert body["applied_mode"] == "video"
    assert calls == [("mode", "video"), ("status",)]


def test_samsung_mode_tv_from_youtube_requires_receiver_current_focus(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_MEDIA_MODE, {"applied_mode": "youtube", "requested_mode": "youtube", "transition": "verified"})
    calls: list[tuple[str, ...]] = []
    _install_controller(
        monkeypatch,
        {
            ("mode", "tv"): {"accepted": True},
            ("status",): {"current_focus": "mCurrentFocus=Window{ua.homeedge.mediareceiver/Player}"},
        },
        calls,
    )
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    body, code = cast_app.samsung_media_mode_post("tv")

    assert code == 200
    assert body["requested_mode"] == "tv"
    assert body["applied_mode"] == "tv"
    assert calls == [("mode", "tv"), ("status",)]


def test_samsung_mode_deferred_without_smarttube_foreground_preserves_previous_verified_mode(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_MEDIA_MODE, {"applied_mode": "video", "requested_mode": "video", "transition": "verified"})
    calls: list[tuple[str, ...]] = []
    _install_controller(
        monkeypatch,
        {
            ("mode", "youtube"): {"accepted": True},
            ("youtube-status",): {"active": False, "foreground_package": "com.samsung.launcher"},
        },
        calls,
    )
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    body, code = cast_app.samsung_media_mode_post("youtube")

    assert code == 409
    assert body["requested_mode"] == "youtube"
    assert body["applied_mode"] == "video"
    assert body["mode"] == "video"
    assert body["transition"] == "failed"


def test_samsung_mode_failed_receiver_verification_has_no_no_evidence_fallback(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_MEDIA_MODE, {"applied_mode": "youtube", "requested_mode": "youtube", "transition": "verified"})
    calls: list[tuple[str, ...]] = []
    _install_controller(
        monkeypatch,
        {
            ("mode", "video"): {"accepted": True},
            ("status",): {"app": "Samsung Media Receiver", "package": "ua.homeedge.mediareceiver"},
        },
        calls,
    )
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    body, code = cast_app.samsung_media_mode_post("video")

    assert code == 409
    assert body["requested_mode"] == "video"
    assert body["applied_mode"] == "youtube"
    assert body["transition"] == "failed"


def test_samsung_mode_without_previous_state_preserves_explicit_idle_on_failure(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    calls: list[tuple[str, ...]] = []
    _install_controller(
        monkeypatch,
        {
            ("mode", "youtube"): {"accepted": True},
            ("youtube-status",): {"active": False, "foreground_package": "com.samsung.launcher"},
        },
        calls,
    )

    state = cast_app._set_samsung_media_mode("youtube")

    assert state["requested_mode"] == "youtube"
    assert state["applied_mode"] == "idle"
    assert state["transition"] == "failed"


def test_samsung_status_get_refreshes_requested_mode_for_home_ui(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    cast_app._atomic(cast_app.SAMSUNG_MEDIA_MODE, {"requested_mode": "youtube", "applied_mode": "idle", "transition": "pending"})
    calls: list[tuple[str, ...]] = []
    _install_controller(monkeypatch, {("youtube-status",): {"active": True, "foreground_package": "org.smarttube.stable"}}, calls)
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    body = cast_app.samsung_media_status_get()

    assert body["requested_mode"] == "youtube"
    assert body["applied_mode"] == "youtube"
    assert body["packages"]["youtube"] == "org.smarttube.stable"
    assert body["packages"]["receiver"] == "ua.homeedge.mediareceiver"
    assert calls == [("youtube-status",)]


def test_samsung_control_endpoint_uses_controller_slugs_not_select_or_generic_adb(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer()
    _install_runtime(monkeypatch, tmp_path, fake)
    calls: list[tuple[str, ...]] = []
    _install_controller(monkeypatch, {("control", "play_pause"): {"sent": True}, ("control", "ok"): {"sent": True}}, calls)
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)

    play = cast_app.samsung_media_control_post("playpause")
    ok = cast_app.samsung_media_control_post("ok")

    assert play["action"] == "play_pause"
    assert ok["action"] == "ok"
    assert ("control", "select") not in calls
    assert calls == [("control", "play_pause"), ("control", "ok")]


def test_samsung_package_identities_are_strict_for_controller_verification() -> None:
    assert cast_app.SAMSUNG_SMARTTUBE_PACKAGE == "org.smarttube.stable"
    assert cast_app.SAMSUNG_RECEIVER_PACKAGE == "ua.homeedge.mediareceiver"
    assert cast_app._samsung_youtube_verified({"active": True, "foreground_package": "org.smarttube.beta"}) is False
    assert cast_app._samsung_youtube_verified({"active": True, "foreground_package": "org.smarttube.stable"}) is True
    assert cast_app._samsung_receiver_verified({"current_focus": "Window{ua.homeedge.mediareceiver/.MainActivity}"}) is True
    assert cast_app._samsung_receiver_verified({"package": "ua.homeedge.mediareceiver", "app": "Samsung Media Receiver"}) is False
