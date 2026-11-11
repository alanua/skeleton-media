from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

resolver_stub = types.ModuleType("skeleton_media.cast.resolver")
resolver_stub._cache_poster = lambda value: value
resolver_stub._cache_youtube_landscape_poster = lambda value: value
resolver_stub._cache_youtube_poster = lambda value: value
_previous_resolver = sys.modules.get("skeleton_media.cast.resolver")
sys.modules["skeleton_media.cast.resolver"] = resolver_stub
try:
    from skeleton_media.cast import player
finally:
    if _previous_resolver is None:
        sys.modules.pop("skeleton_media.cast.resolver", None)
    else:
        sys.modules["skeleton_media.cast.resolver"] = _previous_resolver


@pytest.fixture
def isolated_player(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(player, "CURRENT", tmp_path / "current.json")
    monkeypatch.setattr(player, "IPTV_TRANSITION", tmp_path / "tv-transition.json")
    monkeypatch.setattr(player, "_exit_gnome_overview", lambda: calls.append(("overview", False)))
    monkeypatch.setattr(player, "_set_display_refresh", lambda hz: calls.append(("refresh", hz)) or {"hz": hz})
    monkeypatch.setattr(player, "_named_process_running", lambda name: False)
    monkeypatch.setattr(player, "_player_active", lambda: False)
    monkeypatch.setattr(player, "suspend_current_vod", lambda reason: calls.append(("suspend", reason)) or {"suspended": True})
    monkeypatch.setattr(player, "stop", lambda: calls.append(("stop", None)))
    monkeypatch.setattr(player, "_start_user_unit", lambda unit: calls.append(("start", unit)))
    monkeypatch.setattr(player, "restore_last_vod", lambda: calls.append(("restore_last_vod", None)) or {"mode": "mpv", "restored": True})
    monkeypatch.setattr(player, "_activate_kiosk_in_place", lambda: calls.append(("activate_kiosk", None)) or {"focused": True})
    return calls


def test_player_switch_mode_does_not_own_tv_mode(isolated_player: list[tuple[str, object]]) -> None:
    calls = isolated_player

    with pytest.raises(ValueError):
        player.switch_mode("tv")

    assert ("start", "home-edge-tv-mode@tv.service") not in calls


@pytest.mark.parametrize("target", ["kiosk", "chrome"])
def test_leaving_vod_for_browser_saves_resume_then_stops_mpv_before_browser_unit(
    monkeypatch: pytest.MonkeyPatch,
    isolated_player: list[tuple[str, object]],
    target: str,
) -> None:
    calls = isolated_player
    monkeypatch.setattr(
        player,
        "mode_status",
        lambda *, include_transition=True: {"mode": target if include_transition else "mpv", "tv_mode": target},
    )

    result = player.switch_mode(target)

    assert result["mode"] == target
    assert result["saved_vod"] is True
    assert result["parked_vod"] is False
    assert calls.index(("suspend", "before_mode_switch")) < calls.index(("stop", None))
    assert calls.index(("stop", None)) < calls.index(("start", f"home-edge-tv-mode@{target}.service"))
    assert ("start", "home-edge-tv-mode@tv.service") not in calls


@pytest.mark.parametrize("target", ["games", "off"])
def test_leaving_vod_for_non_browser_still_stops_without_reporting_parked_vod(
    monkeypatch: pytest.MonkeyPatch,
    isolated_player: list[tuple[str, object]],
    target: str,
) -> None:
    calls = isolated_player
    monkeypatch.setattr(
        player,
        "mode_status",
        lambda *, include_transition=True: {"mode": target if include_transition else "mpv", "tv_mode": target},
    )

    result = player.switch_mode(target)

    assert result["mode"] == target
    assert result["saved_vod"] is False
    assert result["parked_vod"] is False
    assert calls.index(("suspend", "before_mode_switch")) < calls.index(("stop", None))
    assert ("start", f"home-edge-tv-mode@{target}.service") in calls
    assert ("start", "home-edge-tv-mode@tv.service") not in calls


@pytest.mark.parametrize("target", ["kiosk", "chrome"])
def test_browser_refresh_cleans_unexpected_active_mpv_before_browser_activation(
    monkeypatch: pytest.MonkeyPatch,
    isolated_player: list[tuple[str, object]],
    target: str,
) -> None:
    calls = isolated_player
    active = {"value": True}

    monkeypatch.setattr(player, "mode_status", lambda *, include_transition=True: {"mode": target, "tv_mode": target})
    monkeypatch.setattr(player, "_player_active", lambda: active["value"])

    def stop() -> None:
        calls.append(("stop", None))
        active["value"] = False

    monkeypatch.setattr(player, "stop", stop)
    monkeypatch.setattr(
        player,
        "_save_competing_vod_for_browser",
        lambda reason: calls.append(("save_competing", reason)) or {"suspended": True},
    )

    result = player.switch_mode(target)

    assert result["mode"] == target
    assert result["saved_vod"] is True
    assert calls.index(("save_competing", "before_browser_focus")) < calls.index(("stop", None))
    if target == "kiosk":
        assert calls.index(("stop", None)) < calls.index(("activate_kiosk", None))
    else:
        assert calls.index(("stop", None)) < calls.index(("start", "home-edge-tv-mode@chrome.service"))
    assert ("start", "home-edge-tv-mode@tv.service") not in calls


@pytest.mark.parametrize(("alias", "target"), [("youtube", "kiosk"), ("youtube_tv", "kiosk"), ("web", "chrome")])
def test_player_owned_mode_aliases_still_map_to_existing_browser_modes(
    monkeypatch: pytest.MonkeyPatch,
    isolated_player: list[tuple[str, object]],
    alias: str,
    target: str,
) -> None:
    calls = isolated_player
    monkeypatch.setattr(
        player,
        "mode_status",
        lambda *, include_transition=True: {"mode": target if include_transition else "off", "tv_mode": target},
    )

    result = player.switch_mode(alias)

    assert result["mode"] == target
    assert ("start", f"home-edge-tv-mode@{target}.service") in calls
    assert ("start", "home-edge-tv-mode@tv.service") not in calls


@pytest.mark.parametrize("alias", ["mpv", "media", "cast", "video"])
def test_video_intent_uses_last_vod_restore_without_intermediate_youtube(
    isolated_player: list[tuple[str, object]],
    alias: str,
) -> None:
    calls = isolated_player

    result = player.switch_mode(alias)

    assert result == {"mode": "mpv", "restored": True}
    assert calls[-1] == ("restore_last_vod", None)
    assert ("start", "home-edge-tv-mode@kiosk.service") not in calls
    assert ("start", "home-edge-tv-mode@tv.service") not in calls


def test_mode_status_does_not_treat_raw_tv_as_player_owned_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(player, "_active_tty", lambda: "tty2")
    monkeypatch.setattr(player, "_current_tv_mode", lambda: "tv")
    monkeypatch.setattr(player, "_active_backend", lambda: (None, None))
    monkeypatch.setattr(player, "_mode_transition_target", lambda: None)
    monkeypatch.setattr(player, "_iptv_transition_active", lambda: False)
    monkeypatch.setattr(player, "_player_active", lambda: False)
    monkeypatch.setattr(player, "_named_process_running", lambda name: False)

    assert player.mode_status()["mode"] == "unknown"
