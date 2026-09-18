import json
from lxml import html as lxml_html
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
