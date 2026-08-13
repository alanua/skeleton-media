from __future__ import annotations

import json
import subprocess

import pytest

from core.home_edge import media_display_ownership as ownership


def test_desktop_video_playing_is_owner() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "mpv"},
        player_payload={"status": "playing", "title": "PRIVATE TITLE", "url": "https://private.invalid/x"},
    )
    assert decision.state is ownership.Ownership.OWNER
    assert decision.exit_code == 0
    public = ownership.public_json(decision)
    assert "PRIVATE TITLE" not in public
    assert "private.invalid" not in public
    assert json.loads(public)["reason"] == "confirmed_video_playing"


@pytest.mark.parametrize("mode", ["kiosk", "chrome", "mpv"])
def test_desktop_video_running_unpaused_is_owner(mode: str) -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": mode},
        player_payload={"running": True, "pause": False},
    )
    assert decision.state is ownership.Ownership.OWNER
    assert decision.exit_code == 0
    assert decision.observations[-1].reason == "player_running_unpaused"


def test_desktop_video_running_paused_releases_to_clear() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "chrome"},
        player_payload={"running": True, "pause": True},
    )
    assert decision.state is ownership.Ownership.CLEAR
    assert decision.exit_code == 1
    assert decision.observations[-1].reason == "player_running_paused"


def test_desktop_video_not_running_releases_to_clear() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "kiosk"},
        player_payload={"running": False},
    )
    assert decision.state is ownership.Ownership.CLEAR
    assert decision.exit_code == 1
    assert decision.observations[-1].reason == "player_running_false"


def test_desktop_video_paused_releases_to_clear() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "chrome"},
        player_payload={"playback_status": "paused"},
    )
    assert decision.state is ownership.Ownership.CLEAR
    assert decision.exit_code == 1


def test_desktop_video_missing_or_malformed_player_fails_closed() -> None:
    for payload in (None, {}, {"status": "ok"}, {"playing": "yes"}):
        decision = ownership.decide_from_snapshots(mode_payload={"mode": "vlc"}, player_payload=payload)
        assert decision.state is ownership.Ownership.UNKNOWN
        assert decision.exit_code == 2


@pytest.mark.parametrize("payload", [{"running": True}, {"running": True, "pause": "false"}])
def test_desktop_video_running_true_without_explicit_pause_is_unknown(payload: dict[str, object]) -> None:
    decision = ownership.decide_from_snapshots(mode_payload={"mode": "mpv"}, player_payload=payload)
    assert decision.state is ownership.Ownership.UNKNOWN
    assert decision.exit_code == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"running": True, "status": "playing"},
        {"running": "true", "pause": False, "playing": True},
    ],
)
def test_unresolved_running_pause_falls_back_to_explicit_playing_marker(payload: dict[str, object]) -> None:
    decision = ownership.decide_from_snapshots(mode_payload={"mode": "vlc"}, player_payload=payload)
    assert decision.state is ownership.Ownership.OWNER
    assert decision.reason == "confirmed_video_playing"
    assert decision.observations[-1].reason == "player_explicit_playing"


def test_unresolved_running_pause_falls_back_to_explicit_clear_marker() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "chrome"},
        player_payload={"running": True, "pause": None, "playback_status": "paused"},
    )
    assert decision.state is ownership.Ownership.CLEAR
    assert decision.reason == "confirmed_video_not_playing"
    assert decision.observations[-1].reason == "player_explicit_not_playing"


def test_contradictory_explicit_markers_keep_existing_owner_precedence() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "kiosk"},
        player_payload={"player": {"playback_state": "paused"}, "playing": True},
    )
    assert decision.state is ownership.Ownership.OWNER
    assert decision.reason == "confirmed_video_playing"
    assert decision.observations[-1].reason == "player_explicit_playing"


def test_android_youtube_playing_is_owner() -> None:
    dump = "Sessions Stack\n  active=true\n  state=PlaybackState {state=3, position=123}\n"
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "youtube"},
        android_media_dump=dump,
    )
    assert decision.state is ownership.Ownership.OWNER
    assert decision.reason == "confirmed_android_video_playing"


def test_android_youtube_paused_releases_to_clear() -> None:
    dump = "active=true\nstate=PlaybackState {state=2, position=123}\n"
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "youtube"},
        android_media_dump=dump,
    )
    assert decision.state is ownership.Ownership.CLEAR


def test_android_generic_playing_does_not_falsely_claim_video_owner() -> None:
    dump = "active=true\nstate=PlaybackState {state=3, position=123}\n"
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "android"},
        android_media_dump=dump,
    )
    assert decision.state is ownership.Ownership.UNKNOWN
    assert decision.reason == "android_display_ownership_ambiguous"


def test_android_generic_clear_is_clear() -> None:
    decision = ownership.decide_from_snapshots(
        mode_payload={"mode": "android"},
        android_media_dump="active=true\nstate=PlaybackState {state=2}\n",
    )
    assert decision.state is ownership.Ownership.CLEAR


def test_foreground_games_mode_preempts_saver_without_media_mutation() -> None:
    decision = ownership.decide_from_snapshots(mode_payload={"mode": "games"})
    assert decision.state is ownership.Ownership.OWNER
    assert decision.reason == "confirmed_foreground_display_owner"


def test_off_mode_is_explicitly_clear() -> None:
    decision = ownership.decide_from_snapshots(mode_payload={"mode": "off"})
    assert decision.state is ownership.Ownership.CLEAR
    assert decision.reason == "display_mode_off"


def test_unknown_mode_fails_closed() -> None:
    for mode_payload in ({}, {"mode": "something-new"}, None):
        decision = ownership.decide_from_snapshots(mode_payload=mode_payload)
        assert decision.state is ownership.Ownership.UNKNOWN


def test_nested_explicit_playback_marker_is_supported_but_unrelated_status_is_ignored() -> None:
    owner = ownership.player_observation({"player": {"playback": {"playback_state": "PLAYING"}}, "status": "ok"})
    assert owner.state is ownership.Ownership.OWNER
    unknown = ownership.player_observation({"status": "ok", "health": "ready"})
    assert unknown.state is ownership.Ownership.UNKNOWN


def test_android_serial_is_bounded_and_command_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "state=PlaybackState {state=3}\n", "")

    assert ownership._read_android_media_session(serial="bad serial;rm -rf /", runner=fake_run) is None
    assert calls == []
    output = ownership._read_android_media_session(serial="192.0.2.7:5555", runner=fake_run)
    assert output is not None
    assert calls == [[ownership.ADB, "-s", "192.0.2.7:5555", "shell", "dumpsys", "media_session"]]


def test_live_decision_does_not_probe_android_for_desktop_player(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_json(path: str, *, timeout: float = 1.5) -> object:
        calls.append(path)
        return {"mode": "mpv"} if path == "/api/mode" else {"playing": True}

    monkeypatch.setattr(ownership, "_read_loopback_json", fake_json)
    monkeypatch.setattr(
        ownership,
        "_read_android_media_session",
        lambda: pytest.fail("android must not be probed for mpv mode"),
    )
    decision = ownership.live_decision()
    assert decision.state is ownership.Ownership.OWNER
    assert calls == ["/api/mode", "/api/player"]


def test_live_decision_mode_probe_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_path: str, *, timeout: float = 1.5) -> object:
        raise RuntimeError("private raw error must not escape")

    monkeypatch.setattr(ownership, "_read_loopback_json", fail)
    decision = ownership.live_decision()
    assert decision.state is ownership.Ownership.UNKNOWN
    assert "private raw error" not in ownership.public_json(decision)


def test_public_payload_has_only_bounded_state_and_reason_fields() -> None:
    decision = ownership.OwnershipDecision(
        ownership.Ownership.UNKNOWN,
        "android_video_state_unknown",
        (ownership.Observation("android_media_session", ownership.Ownership.UNKNOWN, "android_probe_unavailable"),),
    )
    payload = decision.public_payload()
    assert set(payload) == {"schema_version", "state", "reason", "observations"}
    assert set(payload["observations"][0]) == {"source", "state", "reason"}
