import json
import socket
import time
from lxml import html as lxml_html
import pytest
import resolver

ANITUBE_URLS = [
    "https://anitube.in.ua/2087-chervona-mezha",
    "https://anitube.in.ua/2087-chervona-mezha.html",
]


@pytest.fixture(autouse=True)
def stable_public_dns(monkeypatch):
    monkeypatch.setattr(
        resolver.site_registry.socket,
        "getaddrinfo",
        lambda _host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )

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


def test_normal_challenge_is_not_misclassified_as_cloudflare_hard_block():
    assert resolver._is_cloudflare_hard_block("<title>Just a moment...</title><div class='cf-chl'></div>") is False
    assert resolver._is_cloudflare_hard_block("Sorry, you have been blocked. Cloudflare Ray ID") is True


def _rendered_playlist() -> str:
    return '''<html><head><title>Я — космічний шахтар</title></head><body>
    <div class="playlists-videos">
      <li data-file="https://ashdi.vip/vod/536201" data-voice="Anime Classic">Серія 1</li>
      <li data-file="https://ashdi.vip/vod/536202" data-voice="Anime Classic">Серія 2</li>
    </div></body></html>'''


def _challenge(url: str) -> resolver.BrowserChallengeError:
    return resolver.BrowserChallengeError(
        url=url,
        stdout_length=512,
        challenge_detected=True,
        returncode=0,
        diagnostics=[],
    )


def test_normal_challenge_continues_to_public_mirror_without_cooldown(monkeypatch, tmp_path):
    cooldown = tmp_path / "anitube-origin-cooldown.json"
    calls = []
    monkeypatch.setattr(resolver, "ANITUBE_COOLDOWN_PATH", cooldown)

    def direct(url, **kwargs):
        calls.append(("direct", url, kwargs.get("allow_rendered_fallback")))
        raise RuntimeError("HTTP 403")

    def chrome(url, **_kwargs):
        calls.append(("chrome", url, None))
        raise _challenge(url)

    def mirror(url, **_kwargs):
        calls.append(("mirror", url, None))
        return _rendered_playlist()

    monkeypatch.setattr(resolver, "_curl_text", direct)
    monkeypatch.setattr(resolver, "_chrome_text", chrome)
    monkeypatch.setattr(resolver, "_public_html_mirror", mirror)

    targets, title, _poster = resolver.discover(ANITUBE_URLS[0])

    assert [item[0] for item in targets] == [
        "https://ashdi.vip/vod/536201",
        "https://ashdi.vip/vod/536202",
    ]
    assert title == "Я — космічний шахтар"
    assert [item[0] for item in calls] == ["direct", "chrome", "mirror"]
    assert calls[0][2] is False
    assert all(item[1].endswith(".html") for item in calls)
    assert not cooldown.exists()


def test_active_hard_block_cooldown_skips_origin_but_allows_public_mirror(monkeypatch, tmp_path):
    cooldown = tmp_path / "anitube-origin-cooldown.json"
    cooldown.write_text(
        json.dumps({"expires_at": time.time() + 1800}),
        encoding="utf-8",
    )
    monkeypatch.setattr(resolver, "ANITUBE_COOLDOWN_PATH", cooldown)
    monkeypatch.setattr(
        resolver,
        "_curl_text",
        lambda *_args, **_kwargs: pytest.fail("cooldown must suppress origin requests"),
    )
    monkeypatch.setattr(
        resolver,
        "_chrome_text",
        lambda *_args, **_kwargs: pytest.fail("cooldown must suppress origin browser requests"),
    )
    monkeypatch.setattr(resolver, "_public_html_mirror", lambda *_args, **_kwargs: _rendered_playlist())

    targets, _title, _poster = resolver.discover(ANITUBE_URLS[1])

    assert len(targets) == 2
    assert cooldown.exists()


def test_active_cooldown_never_reaches_origin_playlist_ajax(monkeypatch, tmp_path):
    cooldown = tmp_path / "anitube-origin-cooldown.json"
    cooldown.write_text(
        json.dumps({"expires_at": time.time() + 1800}),
        encoding="utf-8",
    )
    monkeypatch.setattr(resolver, "ANITUBE_COOLDOWN_PATH", cooldown)
    monkeypatch.setattr(
        resolver,
        "_curl_text",
        lambda *_args, **_kwargs: pytest.fail("cooldown must suppress origin AJAX"),
    )
    monkeypatch.setattr(
        resolver,
        "_chrome_text",
        lambda *_args, **_kwargs: pytest.fail("cooldown must suppress origin browser requests"),
    )
    monkeypatch.setattr(
        resolver,
        "_public_html_mirror",
        lambda *_args, **_kwargs: '''<html><body>
        <div class="playlists-ajax" data-news_id="5362" data-xfname="playlist"></div>
        </body></html>''',
    )

    with pytest.raises(resolver.OriginProtectedError):
        resolver.discover(ANITUBE_URLS[1])


def test_normal_challenge_without_fallback_does_not_create_cooldown(monkeypatch, tmp_path):
    cooldown = tmp_path / "anitube-origin-cooldown.json"
    monkeypatch.setattr(resolver, "ANITUBE_COOLDOWN_PATH", cooldown)
    monkeypatch.setattr(resolver, "_curl_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 403")))
    monkeypatch.setattr(resolver, "_chrome_text", lambda url, **_kwargs: (_ for _ in ()).throw(_challenge(url)))
    monkeypatch.setattr(resolver, "_public_html_mirror", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("mirror unavailable")))

    with pytest.raises(resolver.BrowserChallengeError):
        resolver.discover(ANITUBE_URLS[1])

    assert not cooldown.exists()
