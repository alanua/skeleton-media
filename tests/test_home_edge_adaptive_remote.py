from __future__ import annotations

from pathlib import Path
import re

import pytest

from core.home_edge.adaptive_remote import (
    BROKER_MAPPING,
    BUTTON_ALLOWLIST,
    CONTROL_BUTTONS_BY_INTERFACE,
    ActiveHomeMode,
    ButtonEvent,
    ButtonPhase,
    ControlInterface,
    HomeInputBroker,
    HomeModeController,
    HomeRemoteContractError,
    MultimediaProfile,
    OrientationStrategy,
    RecordingBrokerSink,
    ReleaseReason,
    orientation_bridge_for,
    render_multimedia_remote,
    render_universal_gamepad,
    select_gamepad_controller,
    validate_contract_integrity,
)


ROOT = Path(__file__).resolve().parents[1]


def test_portrait_multimedia_remote_renders_at_reference_viewport() -> None:
    spec = render_multimedia_remote(390, 844, ActiveHomeMode.ANDROID_APP)

    assert spec.interface is ControlInterface.MULTIMEDIA_REMOTE
    assert (spec.width, spec.height) == (390, 844)
    assert spec.profile is MultimediaProfile.ANDROID_FAMILY
    assert spec.button_ids == CONTROL_BUTTONS_BY_INTERFACE[ControlInterface.MULTIMEDIA_REMOTE]
    assert spec.button_ids <= BUTTON_ALLOWLIST
    for button in spec.buttons:
        left, top, right, bottom = button.bounds()
        assert 0 <= left < right <= spec.width
        assert 0 <= top < bottom <= spec.height
    assert {button.shape for button in spec.buttons if button.button in {"dpad_up", "ok"}} == {
        "dpad_segment",
        "circle",
    }


def test_universal_gamepad_renders_at_reference_viewport() -> None:
    spec = render_universal_gamepad(844, 390)

    assert spec.interface is ControlInterface.GAMEPAD
    assert (spec.width, spec.height) == (844, 390)
    assert spec.profile is MultimediaProfile.GAME
    assert spec.button_ids == CONTROL_BUTTONS_BY_INTERFACE[ControlInterface.GAMEPAD]
    assert spec.button_ids <= BUTTON_ALLOWLIST
    for button in spec.buttons:
        left, top, right, bottom = button.bounds()
        assert 0 <= left < right <= spec.width
        assert 0 <= top < bottom <= spec.height


def test_ui_button_sets_equal_backend_allowlist_and_broker_mapping() -> None:
    validate_contract_integrity()

    rendered_buttons = (
        render_multimedia_remote(390, 844, ActiveHomeMode.LOCAL_PLAYER).button_ids
        | render_universal_gamepad(844, 390).button_ids
    )
    assert rendered_buttons == BUTTON_ALLOWLIST
    assert set(BROKER_MAPPING) == BUTTON_ALLOWLIST


def test_multimedia_profile_is_selected_from_active_mode() -> None:
    assert render_multimedia_remote(390, 844, ActiveHomeMode.ANDROID_TV).profile is MultimediaProfile.ANDROID_FAMILY
    assert render_multimedia_remote(390, 844, ActiveHomeMode.ANDROID_APP).profile is MultimediaProfile.ANDROID_FAMILY
    assert render_multimedia_remote(390, 844, ActiveHomeMode.LOCAL_PLAYER).profile is MultimediaProfile.LOCAL_PLAYER
    assert render_multimedia_remote(390, 844, ActiveHomeMode.BROWSER).profile is MultimediaProfile.BROWSER
    assert render_multimedia_remote(390, 844, ActiveHomeMode.GAME).profile is MultimediaProfile.GAME
    assert render_multimedia_remote(390, 844, ActiveHomeMode.OFF).profile is MultimediaProfile.INACTIVE
    assert render_multimedia_remote(390, 844, ActiveHomeMode.UNKNOWN).profile is MultimediaProfile.INACTIVE


def test_no_title_specific_or_game_specific_profile_strings_or_selectors() -> None:
    source = (ROOT / "core/home_edge/adaptive_remote.py").read_text(encoding="utf-8").lower()
    forbidden = ("ark" + "anoid", "pong", "tetris", "pac" + "man", "breakout")

    assert not any(value in source for value in forbidden)
    assert select_gamepad_controller("universal_gamepad") is ControlInterface.GAMEPAD
    with pytest.raises(HomeRemoteContractError, match="universal gamepad"):
        select_gamepad_controller("ark" + "anoid")
    with pytest.raises(HomeRemoteContractError, match="universal gamepad"):
        select_gamepad_controller("game:space-demo")


def test_same_mode_requests_are_idempotent() -> None:
    controller = HomeModeController(ActiveHomeMode.BROWSER)

    transition = controller.request_mode(ActiveHomeMode.BROWSER)

    assert transition.idempotent is True
    assert transition.actions == ()
    assert transition.resulting_mode is ActiveHomeMode.BROWSER
    assert transition.android_runtime_restarted is False


def test_android_family_warm_transitions_do_not_restart_runtime() -> None:
    controller = HomeModeController(ActiveHomeMode.ANDROID_TV)

    transition = controller.request_mode(ActiveHomeMode.ANDROID_APP)

    assert transition.idempotent is False
    assert transition.actions == ("android_family.warm_app_switch",)
    assert transition.resulting_mode is ActiveHomeMode.ANDROID_APP
    assert transition.android_runtime_restarted is False


def test_leaving_gamepad_does_not_change_tv_mode() -> None:
    controller = HomeModeController(ActiveHomeMode.GAME)

    transition = controller.leave_gamepad()

    assert transition.idempotent is True
    assert transition.actions == ()
    assert transition.resulting_mode is ActiveHomeMode.GAME


def test_closed_button_allowlist_phases_and_multitouch_holds() -> None:
    sink = RecordingBrokerSink()
    broker = HomeInputBroker(sink=sink)

    first, second = (
        ButtonEvent(ControlInterface.GAMEPAD, "a", ButtonPhase.DOWN, pointer_id="finger-1"),
        ButtonEvent(ControlInterface.GAMEPAD, "b", ButtonPhase.DOWN, pointer_id="finger-2"),
    )
    broker.dispatch(first, now=10)
    broker.dispatch(second, now=11)

    assert set(broker.active_holds) == {
        (ControlInterface.GAMEPAD, "a", "finger-1"),
        (ControlInterface.GAMEPAD, "b", "finger-2"),
    }
    with pytest.raises(HomeRemoteContractError, match="not allowed"):
        broker.dispatch(ButtonEvent(ControlInterface.GAMEPAD, "power", ButtonPhase.TAP), now=12)
    with pytest.raises(HomeRemoteContractError, match="not allowed"):
        broker.dispatch(ButtonEvent(ControlInterface.MULTIMEDIA_REMOTE, "a", ButtonPhase.TAP), now=12)


@pytest.mark.parametrize(
    "reason",
    [
        ReleaseReason.BLUR,
        ReleaseReason.VISIBILITY_LOSS,
        ReleaseReason.POINTER_CANCEL,
    ],
)
def test_safe_release_fires_on_browser_lifecycle_events(reason: ReleaseReason) -> None:
    sink = RecordingBrokerSink()
    broker = HomeInputBroker(sink=sink)
    broker.dispatch(ButtonEvent(ControlInterface.GAMEPAD, "dpad_left", ButtonPhase.DOWN, pointer_id="finger-1"), now=1)
    broker.dispatch(ButtonEvent(ControlInterface.GAMEPAD, "a", ButtonPhase.DOWN, pointer_id="finger-2"), now=1)

    released = broker.release_all(reason, now=2)

    assert [command.phase for command in released] == [ButtonPhase.UP, ButtonPhase.UP]
    assert broker.active_holds == {}
    assert [command.button for command in sink.commands[-2:]] == ["dpad_left", "a"]


def test_safe_release_fires_on_bounded_timeout() -> None:
    sink = RecordingBrokerSink()
    broker = HomeInputBroker(sink=sink, max_hold_seconds=8)
    broker.dispatch(ButtonEvent(ControlInterface.GAMEPAD, "r", ButtonPhase.DOWN, pointer_id="finger-1"), now=1)

    assert broker.release_expired(now=8.9) == ()
    released = broker.release_expired(now=9)

    assert len(released) == 1
    assert released[0].button == "r"
    assert released[0].phase is ButtonPhase.UP
    assert broker.active_holds == {}


def test_orientation_bridge_has_native_and_css_fallback_without_global_phone_rotation() -> None:
    native = orientation_bridge_for(ControlInterface.GAMEPAD, native_bridge_available=True)
    fallback = orientation_bridge_for(ControlInterface.MULTIMEDIA_REMOTE, native_bridge_available=False)

    assert native.strategy is OrientationStrategy.NATIVE_BRIDGE
    assert fallback.strategy is OrientationStrategy.CSS_ROTATION_FALLBACK
    assert native.changes_phone_global_orientation is False
    assert fallback.changes_phone_global_orientation is False


def test_offline_reference_does_not_touch_live_tv_or_send_input() -> None:
    sink = RecordingBrokerSink()
    broker = HomeInputBroker(sink=sink)
    broker.dispatch(ButtonEvent(ControlInterface.MULTIMEDIA_REMOTE, "mute", ButtonPhase.TAP), now=1)

    assert sink.commands[0].action == "home.remote.mute"
    assert not any(action.startswith(("ssh", "adb", "xdotool", "input keyevent")) for action in BROKER_MAPPING.values())


def test_functional_phone_ui_matches_contract_and_has_three_remote_tabs() -> None:
    source = (ROOT / "core/home_edge/static/adaptive_remote.html").read_text(encoding="utf-8")

    assert re.findall(r'data-view="([^"]+)"', source) == ["pult", "touchpad", "keyboard"]
    assert set(re.findall(r'data-gamepad="([^"]+)"', source)) == CONTROL_BUTTONS_BY_INTERFACE[ControlInterface.GAMEPAD]
    assert "pointercancel" in source
    assert "visibilitychange" in source
    assert "window.addEventListener('blur'" in source
    assert "setTimeout" in source
    assert "screen.orientation.lock('landscape')" in source
    assert "@media (orientation:portrait)" in source
    assert "/api/remote/control" in source
    assert not any(value in source.lower() for value in ("ark" + "anoid", "tetris", "breakout"))
