from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen


SCHEMA_VERSION = "skeleton.home_edge.media_display_ownership.v1"
CAST_BASE_URL = "http://127.0.0.1:8100"
ADB = "/usr/bin/adb"
ANDROID_SERIAL_ENV = "SKELETON_HOME_EDGE_ANDROID_SERIAL"


class Ownership(str, Enum):
    OWNER = "OWNER"
    CLEAR = "CLEAR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Observation:
    source: str
    state: Ownership
    reason: str


@dataclass(frozen=True)
class OwnershipDecision:
    state: Ownership
    reason: str
    observations: tuple[Observation, ...]

    @property
    def exit_code(self) -> int:
        if self.state is Ownership.OWNER:
            return 0
        if self.state is Ownership.CLEAR:
            return 1
        return 2

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "state": self.state.value,
            "reason": self.reason,
            "observations": [
                {"source": item.source, "state": item.state.value, "reason": item.reason}
                for item in self.observations
            ],
        }


MODE_OFF = frozenset({"off"})
MODE_FOREGROUND_OWNER = frozenset({"games"})
MODE_DESKTOP_VIDEO = frozenset({"mpv", "vlc", "chrome", "kiosk"})
MODE_ANDROID_VIDEO = frozenset({"youtube", "airscreen"})
MODE_ANDROID_AMBIGUOUS = frozenset({"android"})
KNOWN_MODES = MODE_OFF | MODE_FOREGROUND_OWNER | MODE_DESKTOP_VIDEO | MODE_ANDROID_VIDEO | MODE_ANDROID_AMBIGUOUS

_PLAYBACK_KEYS = frozenset(
    {
        "state",
        "status",
        "playback",
        "playback_state",
        "playback_status",
        "player_state",
        "play_state",
    }
)
_PLAYING_BOOL_KEYS = frozenset({"playing", "is_playing"})
_PLAYING_VALUES = frozenset({"playing", "play", "buffering"})
_CLEAR_VALUES = frozenset({"paused", "pause", "stopped", "stop", "ended", "idle", "none"})

_ANDROID_PLAYING_RE = re.compile(r"\bstate\s*=\s*(?:PlaybackState\s*\{\s*state\s*=\s*)?(3|6|8)\b", re.I)
_ANDROID_CLEAR_RE = re.compile(r"\bstate\s*=\s*(?:PlaybackState\s*\{\s*state\s*=\s*)?(0|1|2|7)\b", re.I)
_ANDROID_ACTIVE_RE = re.compile(r"\b(?:active|isActive)\s*=\s*true\b", re.I)
_SAFE_SERIAL_RE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")


def _normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _normalize_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "none"
    return str(value).strip().lower()


def _walk_playback_markers(value: Any, *, depth: int = 0) -> list[Ownership]:
    if depth > 4:
        return []
    markers: list[Ownership] = []
    if isinstance(value, Mapping):
        for raw_key, raw_value in value.items():
            key = _normalize_key(raw_key)
            if key in _PLAYING_BOOL_KEYS and isinstance(raw_value, bool):
                markers.append(Ownership.OWNER if raw_value else Ownership.CLEAR)
            elif key in _PLAYBACK_KEYS and not isinstance(raw_value, (Mapping, list, tuple)):
                scalar = _normalize_scalar(raw_value)
                if scalar in _PLAYING_VALUES:
                    markers.append(Ownership.OWNER)
                elif scalar in _CLEAR_VALUES:
                    markers.append(Ownership.CLEAR)
            if isinstance(raw_value, (Mapping, list, tuple)):
                markers.extend(_walk_playback_markers(raw_value, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value[:32]:
            markers.extend(_walk_playback_markers(item, depth=depth + 1))
    return markers


def _canonical_player_observation(payload: Mapping[object, object]) -> Observation:
    running = payload.get("running")
    if running is False:
        return Observation("skeleton_cast_player", Ownership.CLEAR, "player_running_false")
    if running is True:
        pause = payload.get("pause")
        if pause is False:
            return Observation("skeleton_cast_player", Ownership.OWNER, "player_running_unpaused")
        if pause is True:
            return Observation("skeleton_cast_player", Ownership.CLEAR, "player_running_paused")
        return Observation("skeleton_cast_player", Ownership.UNKNOWN, "player_pause_missing_or_invalid")
    return Observation("skeleton_cast_player", Ownership.UNKNOWN, "player_running_missing_or_invalid")


def player_observation(payload: Any) -> Observation:
    if not isinstance(payload, Mapping):
        return Observation("skeleton_cast_player", Ownership.UNKNOWN, "player_payload_invalid")
    canonical = _canonical_player_observation(payload)
    if canonical.state is not Ownership.UNKNOWN:
        return canonical
    markers = _walk_playback_markers(payload)
    if Ownership.OWNER in markers:
        return Observation("skeleton_cast_player", Ownership.OWNER, "player_explicit_playing")
    if markers and all(marker is Ownership.CLEAR for marker in markers):
        return Observation("skeleton_cast_player", Ownership.CLEAR, "player_explicit_not_playing")
    return Observation("skeleton_cast_player", Ownership.UNKNOWN, "player_state_unresolved")


def android_media_observation(output: str | None, *, video_mode_confirmed: bool) -> Observation:
    if output is None:
        return Observation("android_media_session", Ownership.UNKNOWN, "android_probe_unavailable")
    text = output[:2_000_000]
    if _ANDROID_PLAYING_RE.search(text):
        if video_mode_confirmed:
            return Observation("android_media_session", Ownership.OWNER, "android_video_playing")
        return Observation("android_media_session", Ownership.UNKNOWN, "android_playing_video_unconfirmed")
    if _ANDROID_CLEAR_RE.search(text) or not _ANDROID_ACTIVE_RE.search(text):
        return Observation("android_media_session", Ownership.CLEAR, "android_not_playing")
    return Observation("android_media_session", Ownership.UNKNOWN, "android_state_unresolved")


def mode_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("mode")
    if not isinstance(value, str):
        return None
    mode = value.strip().lower()
    return mode if mode in KNOWN_MODES else None


def decide_from_snapshots(
    *,
    mode_payload: Any,
    player_payload: Any | None = None,
    android_media_dump: str | None = None,
) -> OwnershipDecision:
    mode = mode_from_payload(mode_payload)
    observations: list[Observation] = []
    if mode is None:
        observations.append(Observation("skeleton_cast_mode", Ownership.UNKNOWN, "mode_unresolved"))
        return OwnershipDecision(Ownership.UNKNOWN, "authoritative_mode_unknown", tuple(observations))

    observations.append(Observation("skeleton_cast_mode", Ownership.CLEAR, f"mode_{mode}"))

    if mode in MODE_FOREGROUND_OWNER:
        observations.append(Observation("foreground_mode", Ownership.OWNER, "foreground_display_owner"))
        return OwnershipDecision(Ownership.OWNER, "confirmed_foreground_display_owner", tuple(observations))

    if mode in MODE_OFF:
        return OwnershipDecision(Ownership.CLEAR, "display_mode_off", tuple(observations))

    if mode in MODE_DESKTOP_VIDEO:
        player = player_observation(player_payload)
        observations.append(player)
        if player.state is Ownership.OWNER:
            return OwnershipDecision(Ownership.OWNER, "confirmed_video_playing", tuple(observations))
        if player.state is Ownership.CLEAR:
            return OwnershipDecision(Ownership.CLEAR, "confirmed_video_not_playing", tuple(observations))
        return OwnershipDecision(Ownership.UNKNOWN, "desktop_video_state_unknown", tuple(observations))

    if mode in MODE_ANDROID_VIDEO:
        android = android_media_observation(android_media_dump, video_mode_confirmed=True)
        observations.append(android)
        if android.state is Ownership.OWNER:
            return OwnershipDecision(Ownership.OWNER, "confirmed_android_video_playing", tuple(observations))
        if android.state is Ownership.CLEAR:
            return OwnershipDecision(Ownership.CLEAR, "confirmed_android_video_not_playing", tuple(observations))
        return OwnershipDecision(Ownership.UNKNOWN, "android_video_state_unknown", tuple(observations))

    if mode in MODE_ANDROID_AMBIGUOUS:
        android = android_media_observation(android_media_dump, video_mode_confirmed=False)
        observations.append(android)
        if android.state is Ownership.CLEAR:
            return OwnershipDecision(Ownership.CLEAR, "android_media_clear", tuple(observations))
        # A generic Android playback session may be audio-only. UNKNOWN intentionally
        # suppresses the visual saver without falsely declaring video ownership.
        return OwnershipDecision(Ownership.UNKNOWN, "android_display_ownership_ambiguous", tuple(observations))

    return OwnershipDecision(Ownership.UNKNOWN, "mode_unhandled", tuple(observations))


def _read_loopback_json(path: str, *, timeout: float = 1.5) -> Any:
    request = Request(CAST_BASE_URL + path, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError("non_200")
            body = response.read(256 * 1024)
    except (OSError, URLError, RuntimeError) as exc:
        raise RuntimeError("loopback_probe_failed") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("loopback_json_invalid") from exc


def _read_android_media_session(
    *,
    serial: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    value = (serial if serial is not None else os.environ.get(ANDROID_SERIAL_ENV, "")).strip()
    if not value or _SAFE_SERIAL_RE.fullmatch(value) is None:
        return None
    try:
        process = runner(
            [ADB, "-s", value, "shell", "dumpsys", "media_session"],
            text=True,
            capture_output=True,
            timeout=2.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    return process.stdout[:2_000_000]


def live_decision() -> OwnershipDecision:
    try:
        mode_payload = _read_loopback_json("/api/mode")
    except RuntimeError:
        return OwnershipDecision(
            Ownership.UNKNOWN,
            "skeleton_cast_mode_unavailable",
            (Observation("skeleton_cast_mode", Ownership.UNKNOWN, "mode_probe_unavailable"),),
        )

    mode = mode_from_payload(mode_payload)
    player_payload: Any | None = None
    android_dump: str | None = None

    if mode in MODE_DESKTOP_VIDEO:
        try:
            player_payload = _read_loopback_json("/api/player")
        except RuntimeError:
            player_payload = None
    elif mode in MODE_ANDROID_VIDEO or mode in MODE_ANDROID_AMBIGUOUS:
        android_dump = _read_android_media_session()

    return decide_from_snapshots(
        mode_payload=mode_payload,
        player_payload=player_payload,
        android_media_dump=android_dump,
    )


def public_json(decision: OwnershipDecision) -> str:
    return json.dumps(decision.public_payload(), sort_keys=True, separators=(",", ":"))
