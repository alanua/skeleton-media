from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Mapping, Protocol


class HomeRemoteContractError(ValueError):
    """Raised when a phone remote request is outside the closed contract."""


class ActiveHomeMode(StrEnum):
    ANDROID_TV = "android_tv"
    ANDROID_APP = "android_app"
    LOCAL_PLAYER = "local_player"
    BROWSER = "browser"
    GAME = "game"
    OFF = "off"
    UNKNOWN = "unknown"


class MultimediaProfile(StrEnum):
    ANDROID_FAMILY = "android_family"
    LOCAL_PLAYER = "local_player"
    BROWSER = "browser"
    GAME = "game"
    INACTIVE = "inactive"


class ControlInterface(StrEnum):
    MULTIMEDIA_REMOTE = "multimedia_remote"
    GAMEPAD = "gamepad"


class ButtonPhase(StrEnum):
    DOWN = "down"
    UP = "up"
    TAP = "tap"


class OrientationMode(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class OrientationStrategy(StrEnum):
    NATIVE_BRIDGE = "native_bridge"
    CSS_ROTATION_FALLBACK = "css_rotation_fallback"


class ReleaseReason(StrEnum):
    BLUR = "blur"
    VISIBILITY_LOSS = "visibility_loss"
    POINTER_CANCEL = "pointer_cancel"
    TIMEOUT = "timeout"


ANDROID_FAMILY_MODES = frozenset(
    {
        ActiveHomeMode.ANDROID_TV,
        ActiveHomeMode.ANDROID_APP,
    }
)

REMOTE_BUTTONS = frozenset(
    {
        "power",
        "mute",
        "refresh",
        "dpad_up",
        "dpad_down",
        "dpad_left",
        "dpad_right",
        "ok",
        "back",
        "home",
        "menu",
        "play_pause",
        "rewind",
        "fast_forward",
        "previous",
        "next",
        "volume_up",
        "volume_down",
    }
)

GAMEPAD_BUTTONS = frozenset(
    {
        "dpad_up",
        "dpad_down",
        "dpad_left",
        "dpad_right",
        "a",
        "b",
        "x",
        "y",
        "l",
        "r",
        "start",
        "select",
        "library",
        "android_tv",
        "safe_exit",
    }
)

CONTROL_BUTTONS_BY_INTERFACE = {
    ControlInterface.MULTIMEDIA_REMOTE: REMOTE_BUTTONS,
    ControlInterface.GAMEPAD: GAMEPAD_BUTTONS,
}

BUTTON_ALLOWLIST = frozenset(REMOTE_BUTTONS | GAMEPAD_BUTTONS)
PHASE_ALLOWLIST = frozenset(ButtonPhase)
MAX_HOLD_SECONDS = 8.0

BROKER_MAPPING: Mapping[str, str] = {
    "power": "home.remote.power",
    "mute": "home.remote.mute",
    "refresh": "home.remote.refresh",
    "dpad_up": "home.nav.up",
    "dpad_down": "home.nav.down",
    "dpad_left": "home.nav.left",
    "dpad_right": "home.nav.right",
    "ok": "home.nav.ok",
    "back": "home.nav.back",
    "home": "home.nav.home",
    "menu": "home.nav.menu",
    "play_pause": "home.transport.play_pause",
    "rewind": "home.transport.rewind",
    "fast_forward": "home.transport.fast_forward",
    "previous": "home.transport.previous",
    "next": "home.transport.next",
    "volume_up": "home.volume.up",
    "volume_down": "home.volume.down",
    "a": "home.gamepad.a",
    "b": "home.gamepad.b",
    "x": "home.gamepad.x",
    "y": "home.gamepad.y",
    "l": "home.gamepad.l",
    "r": "home.gamepad.r",
    "start": "home.gamepad.start",
    "select": "home.gamepad.select",
    "library": "home.gamepad.library",
    "android_tv": "home.gamepad.android_tv",
    "safe_exit": "home.gamepad.safe_exit",
}


@dataclass(frozen=True)
class ButtonEvent:
    interface: ControlInterface
    button: str
    phase: ButtonPhase
    pointer_id: str = "primary"


@dataclass(frozen=True)
class BrokerCommand:
    action: str
    phase: ButtonPhase
    button: str
    interface: ControlInterface
    pointer_id: str


class BrokerSink(Protocol):
    def send(self, command: BrokerCommand) -> None: ...


@dataclass
class RecordingBrokerSink:
    commands: list[BrokerCommand] = field(default_factory=list)

    def send(self, command: BrokerCommand) -> None:
        self.commands.append(command)


@dataclass
class HomeInputBroker:
    sink: BrokerSink
    mapping: Mapping[str, str] = field(default_factory=lambda: dict(BROKER_MAPPING))
    active_holds: dict[tuple[ControlInterface, str, str], float] = field(default_factory=dict)
    max_hold_seconds: float = MAX_HOLD_SECONDS

    def dispatch(self, event: ButtonEvent, *, now: float | None = None) -> tuple[BrokerCommand, ...]:
        self._validate_event(event)
        timestamp = monotonic() if now is None else now
        command = BrokerCommand(
            action=self.mapping[event.button],
            phase=event.phase,
            button=event.button,
            interface=event.interface,
            pointer_id=event.pointer_id,
        )
        if event.phase is ButtonPhase.DOWN:
            self.active_holds[(event.interface, event.button, event.pointer_id)] = timestamp
        elif event.phase in {ButtonPhase.UP, ButtonPhase.TAP}:
            self.active_holds.pop((event.interface, event.button, event.pointer_id), None)
        self.sink.send(command)
        return (command,)

    def release_all(self, reason: ReleaseReason, *, now: float | None = None) -> tuple[BrokerCommand, ...]:
        del reason
        timestamp = monotonic() if now is None else now
        return self._release_matching(lambda _key, _started: True, timestamp)

    def release_expired(self, *, now: float | None = None) -> tuple[BrokerCommand, ...]:
        timestamp = monotonic() if now is None else now
        return self._release_matching(
            lambda _key, started: timestamp - started >= self.max_hold_seconds,
            timestamp,
        )

    def _release_matching(self, predicate, now: float) -> tuple[BrokerCommand, ...]:
        del now
        commands: list[BrokerCommand] = []
        for key, started in tuple(self.active_holds.items()):
            if not predicate(key, started):
                continue
            interface, button, pointer_id = key
            self.active_holds.pop(key, None)
            command = BrokerCommand(
                action=self.mapping[button],
                phase=ButtonPhase.UP,
                button=button,
                interface=interface,
                pointer_id=pointer_id,
            )
            self.sink.send(command)
            commands.append(command)
        return tuple(commands)

    def _validate_event(self, event: ButtonEvent) -> None:
        allowed = CONTROL_BUTTONS_BY_INTERFACE[event.interface]
        if event.button not in allowed:
            raise HomeRemoteContractError("button is not allowed for this stable interface")
        if event.button not in self.mapping:
            raise HomeRemoteContractError("button has no bounded broker mapping")
        if event.phase not in PHASE_ALLOWLIST:
            raise HomeRemoteContractError("button phase is not allowlisted")
        if not event.pointer_id.strip():
            raise HomeRemoteContractError("pointer_id must be non-empty")


@dataclass(frozen=True)
class ModeTransition:
    previous_mode: ActiveHomeMode
    requested_mode: ActiveHomeMode
    resulting_mode: ActiveHomeMode
    idempotent: bool
    android_runtime_restarted: bool
    actions: tuple[str, ...]


@dataclass
class HomeModeController:
    active_mode: ActiveHomeMode = ActiveHomeMode.UNKNOWN

    def request_mode(self, requested_mode: ActiveHomeMode) -> ModeTransition:
        previous = self.active_mode
        if requested_mode is previous:
            return ModeTransition(previous, requested_mode, previous, True, False, ())
        warm_android_transition = previous in ANDROID_FAMILY_MODES and requested_mode in ANDROID_FAMILY_MODES
        action = "android_family.warm_app_switch" if warm_android_transition else f"mode.select.{requested_mode.value}"
        self.active_mode = requested_mode
        return ModeTransition(previous, requested_mode, requested_mode, False, False, (action,))

    def leave_gamepad(self) -> ModeTransition:
        return ModeTransition(self.active_mode, self.active_mode, self.active_mode, True, False, ())


def multimedia_profile_for_mode(mode: ActiveHomeMode) -> MultimediaProfile:
    if mode in ANDROID_FAMILY_MODES:
        return MultimediaProfile.ANDROID_FAMILY
    if mode is ActiveHomeMode.LOCAL_PLAYER:
        return MultimediaProfile.LOCAL_PLAYER
    if mode is ActiveHomeMode.BROWSER:
        return MultimediaProfile.BROWSER
    if mode is ActiveHomeMode.GAME:
        return MultimediaProfile.GAME
    return MultimediaProfile.INACTIVE


@dataclass(frozen=True)
class ButtonLayout:
    button: str
    x: int
    y: int
    width: int
    height: int
    shape: str = "rounded_rect"

    def bounds(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


@dataclass(frozen=True)
class RemoteRenderSpec:
    interface: ControlInterface
    width: int
    height: int
    orientation: OrientationMode
    profile: MultimediaProfile
    buttons: tuple[ButtonLayout, ...]

    @property
    def button_ids(self) -> frozenset[str]:
        return frozenset(button.button for button in self.buttons)


@dataclass(frozen=True)
class OrientationBridgeSpec:
    desired: OrientationMode
    strategy: OrientationStrategy
    changes_phone_global_orientation: bool = False


def orientation_bridge_for(
    interface: ControlInterface, *, native_bridge_available: bool
) -> OrientationBridgeSpec:
    desired = OrientationMode.LANDSCAPE if interface is ControlInterface.GAMEPAD else OrientationMode.PORTRAIT
    strategy = (
        OrientationStrategy.NATIVE_BRIDGE
        if native_bridge_available
        else OrientationStrategy.CSS_ROTATION_FALLBACK
    )
    return OrientationBridgeSpec(desired=desired, strategy=strategy)


def render_multimedia_remote(width: int, height: int, mode: ActiveHomeMode) -> RemoteRenderSpec:
    if (width, height) != (390, 844):
        raise HomeRemoteContractError("portrait multimedia remote reference viewport must be 390x844")
    return RemoteRenderSpec(
        interface=ControlInterface.MULTIMEDIA_REMOTE,
        width=width,
        height=height,
        orientation=OrientationMode.PORTRAIT,
        profile=multimedia_profile_for_mode(mode),
        buttons=(
            ButtonLayout("power", 24, 28, 58, 58, "circle"),
            ButtonLayout("mute", 166, 28, 58, 58, "circle"),
            ButtonLayout("refresh", 308, 28, 58, 58, "circle"),
            ButtonLayout("dpad_up", 155, 154, 80, 70, "dpad_segment"),
            ButtonLayout("dpad_left", 86, 223, 70, 80, "dpad_segment"),
            ButtonLayout("ok", 158, 226, 74, 74, "circle"),
            ButtonLayout("dpad_right", 234, 223, 70, 80, "dpad_segment"),
            ButtonLayout("dpad_down", 155, 303, 80, 70, "dpad_segment"),
            ButtonLayout("back", 38, 414, 78, 54),
            ButtonLayout("home", 156, 414, 78, 54),
            ButtonLayout("menu", 274, 414, 78, 54),
            ButtonLayout("previous", 38, 526, 64, 52),
            ButtonLayout("rewind", 120, 526, 64, 52),
            ButtonLayout("play_pause", 203, 526, 64, 52),
            ButtonLayout("fast_forward", 286, 526, 64, 52),
            ButtonLayout("next", 38, 606, 64, 52),
            ButtonLayout("volume_down", 120, 688, 96, 78),
            ButtonLayout("volume_up", 236, 688, 96, 78),
        ),
    )


def render_universal_gamepad(width: int, height: int) -> RemoteRenderSpec:
    if (width, height) != (844, 390):
        raise HomeRemoteContractError("landscape gamepad reference viewport must be 844x390")
    return RemoteRenderSpec(
        interface=ControlInterface.GAMEPAD,
        width=width,
        height=height,
        orientation=OrientationMode.LANDSCAPE,
        profile=MultimediaProfile.GAME,
        buttons=(
            ButtonLayout("library", 24, 18, 56, 44),
            ButtonLayout("android_tv", 96, 18, 56, 44),
            ButtonLayout("select", 336, 24, 70, 42),
            ButtonLayout("start", 438, 24, 70, 42),
            ButtonLayout("safe_exit", 764, 18, 56, 44),
            ButtonLayout("l", 166, 84, 84, 46),
            ButtonLayout("r", 594, 84, 84, 46),
            ButtonLayout("dpad_up", 118, 150, 68, 58, "dpad_segment"),
            ButtonLayout("dpad_left", 58, 207, 58, 68, "dpad_segment"),
            ButtonLayout("dpad_right", 188, 207, 58, 68, "dpad_segment"),
            ButtonLayout("dpad_down", 118, 278, 68, 58, "dpad_segment"),
            ButtonLayout("y", 665, 148, 58, 58, "circle"),
            ButtonLayout("x", 604, 209, 58, 58, "circle"),
            ButtonLayout("b", 726, 209, 58, 58, "circle"),
            ButtonLayout("a", 665, 270, 58, 58, "circle"),
        ),
    )


def select_gamepad_controller(selector: str) -> ControlInterface:
    if selector != "universal_gamepad":
        raise HomeRemoteContractError("only the universal gamepad controller is supported")
    return ControlInterface.GAMEPAD


def validate_contract_integrity() -> None:
    if set(BROKER_MAPPING) != set(BUTTON_ALLOWLIST):
        raise HomeRemoteContractError("broker mapping must exactly equal the button allowlist")
    for interface, buttons in CONTROL_BUTTONS_BY_INTERFACE.items():
        if not buttons <= BUTTON_ALLOWLIST:
            raise HomeRemoteContractError(f"{interface.value} buttons escape the allowlist")
