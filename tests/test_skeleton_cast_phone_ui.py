from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
PHONE_HTML = ROOT / "ops" / "skeleton_cast" / "runtime" / "phone.html"


def _phone_source() -> str:
    return PHONE_HTML.read_text(encoding="utf-8")


def test_phone_player_removes_generic_transport_row_but_keeps_timeline_volume_and_youtube_mute() -> None:
    source = _phone_source()

    assert 'class="transport"' not in source
    for removed_id in ('id="seekBack"', 'id="playToggle"', 'id="seekForward"', 'id="mute"', 'id="toggleIcon"', 'id="muteIcon"'):
        assert removed_id not in source
    assert 'id="playerTimeRow"' in source
    assert 'id="seek"' in source
    assert 'id="volume"' in source
    assert 'onclick="stepVolume(-5)"' in source
    assert 'onclick="stepVolume(5)"' in source
    assert 'id="youtubeNavPanel"' in source
    assert 'id="youtubeMute"' in source
    assert 'id="youtubeMuteIcon"' in source


def test_phone_player_js_has_no_direct_references_to_removed_transport_controls() -> None:
    source = _phone_source()

    assert "$('seekBack')" not in source
    assert "$('seekForward')" not in source
    assert "$('playToggle')" not in source
    assert "setIcon('toggleIcon'" not in source
    assert "document.querySelector('.transport')" not in source


def test_phone_mode_switch_renders_authoritative_get_mode_after_post() -> None:
    source = _phone_source()
    set_mode_body = source[source.index("async function setMode(") : source.index("function requestModeFromButton")]

    assert "normalizeModeTarget(mode)" in set_mode_body
    assert "await jf('/api/mode/'+mode,{method:'POST'" in set_mode_body
    assert "await jf('/api/mode',{cache:'no-store'})" in set_mode_body
    assert "actual!==target" in set_mode_body
    assert "renderMode(finalMode)" in set_mode_body
    assert "renderMode(j)" not in set_mode_body


def test_phone_mode_buttons_use_user_facing_video_semantics_for_internal_mpv() -> None:
    source = _phone_source()

    assert 'data-mode-target="mpv"' in source
    assert "<span>Відео</span>" in source
    assert "mpv:'Відео'" in source
    assert "<span>Cast</span>" not in source
    assert "<span>Video</span>" not in source
    assert "mpv:'Video'" not in source


def _mode_button_targets(source: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for match in re.finditer(r'<button class="mode-btn"[^>]*data-mode-target="([^"]+)"[^>]*>.*?<span>([^<]+)</span></button>', source):
        target, label = match.groups()
        targets[label] = target
    return targets


@pytest.mark.parametrize(
    ("origin", "destination", "post_target", "final_mode"),
    [
        ("TV", "YouTube", "kiosk", "kiosk"),
        ("YouTube", "TV", "tv", "tv"),
        ("TV", "Відео", "mpv", "mpv"),
        ("Відео", "TV", "tv", "tv"),
        ("YouTube", "Відео", "mpv", "mpv"),
        ("Відео", "YouTube", "kiosk", "kiosk"),
    ],
)
def test_phone_ui_contract_for_tv_youtube_video_intents_requires_post_target_and_authoritative_get(
    origin: str,
    destination: str,
    post_target: str,
    final_mode: str,
) -> None:
    source = _phone_source()
    set_mode_body = source[source.index("async function setMode(") : source.index("function requestModeFromButton")]
    targets = _mode_button_targets(source)

    assert origin in targets
    assert targets[destination] == post_target
    assert "await jf('/api/mode/'+mode,{method:'POST'" in set_mode_body
    assert "const target=normalizeModeTarget(mode)" in set_mode_body
    assert f"{post_target!r}" in source
    assert "const[modeR,finalMode]=await jf('/api/mode',{cache:'no-store'});" in set_mode_body
    assert "const actual=normalizeModeTarget(finalMode.mode||finalMode.tv_mode||'unknown');" in set_mode_body
    assert "if(actual!==target)" in set_mode_body
    assert final_mode in {"tv", "kiosk", "mpv"}
