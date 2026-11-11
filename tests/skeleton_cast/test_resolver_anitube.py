import json
from lxml import html as lxml_html
import player
import resolver

ANITUBE_URLS = [
    "https://anitube.in.ua/2087-chervona-mezha",
    "https://anitube.in.ua/2087-chervona-mezha.html",
]

def test_browser_stderr_filtered():
    raw = """[123:456:0726/000606.261127:ERROR:services/network/mdns_responder.cc:897] mDNS responder manager failed to start.
[123:456:0726/000606.747313:ERROR:device/udev_linux/udev_watcher.cc:51] Failed to initialize a udev monitor.
[123:456:0726/000611.289648:ERROR:google_apis/gcm/engine/registration_request.cc:291] Registration response error message: PHONE_REGISTRATION_ERROR
[123:456:0726/000611.359082:ERROR:crypto/nss_util.cc:256] Error initializing NSS with a persistent database: NSS error code: -8126
ACTIONABLE: net::ERR_CONNECTION_REFUSED
"""
    filtered = resolver._filter_chrome_stderr(raw)
    assert filtered == ["ACTIONABLE: net::ERR_CONNECTION_REFUSED"]

def test_playlist_targets_detects_visible_rendered_dom_only():
    markup = '''<html><body><div class="playlists-videos">
    <li data-file="https://ashdi.vip/vod/123" data-voice="AniDub">Серія 1</li>
    <li style="display:none" data-file="https://ashdi.vip/vod/124" data-voice="Hidden">Серія 2</li>
    </div></body></html>'''
    doc = lxml_html.fromstring(markup)
    targets = resolver._playlist_targets(ANITUBE_URLS[0], markup, doc)
    assert targets == [("https://ashdi.vip/vod/123", "AniDub", "Серія 1", 0)]

def test_browser_challenge_error_structure():
    exc = resolver.BrowserChallengeError(
        url=ANITUBE_URLS[0], stdout_length=512, challenge_detected=True,
        returncode=0, diagnostics=["net::ERR_CONNECTION_REFUSED"],
    )
    assert exc.url == ANITUBE_URLS[0]
    assert exc.challenge_detected is True
    assert exc.diagnostics == ["net::ERR_CONNECTION_REFUSED"]

def test_anitube_js_challenge_keeps_mirror_fallback(monkeypatch):
    calls = []
    mirror = '''<html><head><title>AniTube Mirror</title></head><body>
    <div class="playlists-videos"><li data-file="https://ashdi.vip/vod/777" data-voice="AniUA">Серія 7</li></div>
    </body></html>'''

    monkeypatch.setattr(resolver, "_anitube_cooldown_remaining", lambda: 0)
    monkeypatch.setattr(resolver, "_curl_text", lambda *a, **k: "<html><title>challenge</title></html>")
    monkeypatch.setattr(
        resolver,
        "_chrome_text",
        lambda *a, **k: (_ for _ in ()).throw(
            resolver.BrowserChallengeError(
                url=ANITUBE_URLS[0],
                stdout_length=128,
                challenge_detected=True,
                returncode=0,
                diagnostics=[],
            )
        ),
    )
    monkeypatch.setattr(resolver, "_mark_anitube_origin_protected", lambda url: calls.append(url) or 3600)
    monkeypatch.setattr(resolver, "_public_html_mirror", lambda *a, **k: mirror)

    targets, title, _poster = resolver.discover(ANITUBE_URLS[0])

    assert title == "AniTube Mirror"
    assert targets == [("https://ashdi.vip/vod/777", "AniUA", "Серія 7", 0)]
    assert calls == []

def test_anitube_cooldown_still_allows_public_mirror(monkeypatch):
    mirror = '''<html><head><title>Cooldown Mirror</title></head><body>
    <div class="playlists-videos"><li data-file="https://ashdi.vip/vod/778" data-voice="AniUA">Серія 8</li></div>
    </body></html>'''

    monkeypatch.setattr(resolver, "_anitube_cooldown_remaining", lambda: 1200)
    monkeypatch.setattr(resolver, "_curl_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("origin fetch skipped")))
    monkeypatch.setattr(resolver, "_chrome_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser fetch skipped")))
    monkeypatch.setattr(resolver, "_public_html_mirror", lambda *a, **k: mirror)

    targets, title, _poster = resolver.discover(ANITUBE_URLS[1])

    assert title == "Cooldown Mirror"
    assert targets == [("https://ashdi.vip/vod/778", "AniUA", "Серія 8", 0)]

def test_chrome_browser_source_uses_browser_player_with_resolved_url(monkeypatch):
    calls = []

    monkeypatch.setattr(player, "stop", lambda: calls.append(("stop",)))
    monkeypatch.setattr(player, "status", lambda: {"running": True, "backend": "chrome-browser"})

    def fake_run(command, **_kwargs):
        calls.append(tuple(command))
        class Result:
            returncode = 0
            stdout = '{"opened": true}'
            stderr = ""
        return Result()

    monkeypatch.setattr(player.subprocess, "run", fake_run)

    result = player.play(
        {"job_id": "job1", "page_url": "https://anitube.in.ua/original.html"},
        {
            "source_id": "src1",
            "url": "https://anitube.in.ua/resolved.html",
            "backend": "chrome-browser",
            "browser_index": 2,
            "quality": "Chrome",
            "translation": "AniUA",
        },
    )

    assert result["backend"] == "chrome-browser"
    assert calls[0] == ("stop",)
    assert calls[1][0:3] == (player.BROWSER_MEDIA, "play", "default")
    assert calls[1][3] == "https://anitube.in.ua/resolved.html"
