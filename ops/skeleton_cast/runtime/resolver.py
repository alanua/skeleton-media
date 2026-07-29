from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import tempfile
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
import site_registry
from lxml import html as lxml_html

YTDLP = "/home/skeleton/.local/bin/yt-dlp"
POSTER_DIR = Path("/home/skeleton/.local/state/skeleton-cast/posters")
ANITUBE_COOLDOWN_PATH = Path("/home/skeleton/.local/state/skeleton-cast/anitube-origin-cooldown.json")
ANITUBE_COOLDOWN_SECONDS = 3600
_ANITUBE_COOLDOWN_LOCK = threading.Lock()
UA = (
    "Mozilla/5.0 (Linux; Android 15; 23053RN02A) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Mobile Safari/537.36"
)
ASHDI = re.compile(r"https?:\\?/\\?/(?:www\.)?ashdi\.vip\\?/vod\\?/\d+", re.I)
DIRECT = re.compile(r"\.(?:m3u8|mpd|mp4)(?:$|[?#])", re.I)
EDIT_TIME = re.compile(r"var\s+dle_edittime\s*=\s*['\"]?(\d+)", re.I)


class OriginProtectedError(RuntimeError):
    """The public origin is protected and retries must be cooled down."""

    def __init__(self, *, url: str, cooldown_remaining_seconds: int) -> None:
        self.url = url
        self.cooldown_remaining_seconds = max(1, int(cooldown_remaining_seconds))
        super().__init__(f"Origin protected; retry after {self.cooldown_remaining_seconds}s")


def _anitube_cooldown_remaining(now: float | None = None) -> int:
    now = time.time() if now is None else now
    with _ANITUBE_COOLDOWN_LOCK:
        try:
            payload = json.loads(ANITUBE_COOLDOWN_PATH.read_text(encoding="utf-8"))
            expires_at = float(payload.get("expires_at", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0
        if expires_at <= now:
            try:
                ANITUBE_COOLDOWN_PATH.unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        return max(1, int(expires_at - now))


def _mark_anitube_origin_protected(url: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    expires_at = now + ANITUBE_COOLDOWN_SECONDS
    payload = {"schema": "skeleton-cast.anitube-cooldown.v1", "origin": "https://anitube.in.ua", "url": url, "created_at": int(now), "expires_at": int(expires_at)}
    with _ANITUBE_COOLDOWN_LOCK:
        ANITUBE_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ANITUBE_COOLDOWN_PATH.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, ANITUBE_COOLDOWN_PATH)
            os.chmod(ANITUBE_COOLDOWN_PATH, 0o600)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return ANITUBE_COOLDOWN_SECONDS


def _is_cloudflare_hard_block(text: str) -> bool:
    lowered = (text or "")[:100000].lower()
    return any(marker in lowered for marker in (
        "sorry, you have been blocked",
        "you are unable to access anitube.in.ua",
        "attention required! | cloudflare",
        "cf-error-details",
        "cloudflare ray id",
    ))


class BrowserChallengeError(RuntimeError):
    """Structured failure from the bounded rendered-page browser fallback."""

    def __init__(
        self,
        *,
        url: str,
        stdout_length: int,
        challenge_detected: bool,
        returncode: int,
        diagnostics: list[str],
    ) -> None:
        self.url = url
        self.stdout_length = stdout_length
        self.challenge_detected = challenge_detected
        self.returncode = returncode
        self.diagnostics = diagnostics
        super().__init__(
            f"Browser challenge: rc={returncode}, challenge={challenge_detected}, "
            f"stdout={stdout_length}b, diag_lines={len(diagnostics)}"
        )


def _filter_chrome_stderr(stderr: str) -> list[str]:
    """Keep actionable Chromium diagnostics and discard known platform noise."""
    noise_tokens = (
        "nss_initialize", "error initializing nss", "mdns responder", "udev monitor",
        "phone_registration_error", "deprecated_endpoint", "fontconfig", "error:bus",
        "error:viz", "warning:audio", "warning:bluez", "error:gl_surface",
        "warning:sandbox", "gcm/engine", "registration response error",
    )
    result: list[str] = []
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(token in lowered for token in noise_tokens):
            continue
        # Chromium prefixes all diagnostics with a volatile pid/timestamp tuple.
        line = re.sub(r"^\[\d+:\d+:\d+/[0-9.]+:(?:ERROR|WARNING):", "", line).strip()
        if line and line not in result:
            result.append(line)
    return result[-20:]


def _sid(url: str, label: str) -> str:
    return hashlib.sha256((url + "\n" + label).encode()).hexdigest()[:16]


def _quality(fmt: dict[str, Any]) -> str:
    h, w = fmt.get("height"), fmt.get("width")
    url = str(fmt.get("url") or "")
    named = re.search(r"/hls/(2160|1440|1080|720|480|360|240)/", url)
    label = named.group(1) if named else (str(h) if h else "")
    if label:
        return f"{label}p" + (f" · {w}×{h}" if w and h else "")
    return str(fmt.get("resolution") or fmt.get("format_note") or fmt.get("format_id") or "Авто")


def _browser_headers(page_url: str) -> dict[str, str]:
    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else page_url
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        "Referer": origin,
        "Cache-Control": "no-cache",
    }


def _chrome_text(url: str, *, timeout: int = 65) -> str:
    safe_url, _ = site_registry.validate_public_url(url)
    profile = tempfile.mkdtemp(prefix="skeleton-cast-chrome-", dir="/tmp")
    try:
        profile_path = Path(profile)
        (profile_path / "Default").mkdir(parents=True, exist_ok=True)
        (profile_path / "First Run").touch()
        command = [
            "/usr/bin/google-chrome",
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-breakpad",
            "--crash-dumps-dir=/dev/null",
            "--disable-features=MediaRouter,GlobalMediaControls,OptimizationHints,PushMessaging,NotificationTriggers,InterestFeedContentSuggestions",
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "--lang=uk-UA",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=60000",
            "--dump-dom",
            safe_url,
        ]
        chrome_env = os.environ.copy()
        chrome_env.update({
            "HOME": profile,
            "XDG_CONFIG_HOME": str(profile_path / "config"),
            "XDG_CACHE_HOME": str(profile_path / "cache"),
        })
        process = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, check=False,
            env=chrome_env,
        )
        text = process.stdout or ""
        lowered = text.lower()
        challenge = any(marker in lowered for marker in (
            "cf-chl", "just a moment", "трохи зачекайте", "checking your browser",
        ))
        if _is_cloudflare_hard_block(text):
            remaining = _mark_anitube_origin_protected(safe_url)
            raise OriginProtectedError(url=safe_url, cooldown_remaining_seconds=remaining)
        if process.returncode == 0 and len(text) > 1000 and not challenge:
            return text
        raise BrowserChallengeError(
            url=safe_url,
            stdout_length=len(text),
            challenge_detected=challenge,
            returncode=process.returncode,
            diagnostics=_filter_chrome_stderr(process.stderr or ""),
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)

def _public_html_mirror(url: str, *, timeout: int = 55) -> str:
    """Fetch rendered public HTML when the origin only returns a JS challenge.

    The mirror is used only for already validated public http(s) video pages and
    only for the initial HTML document. It is never used for credentials,
    private addresses, API calls, media streams, or POST data.
    """
    safe_url, _ = site_registry.validate_public_url(url)
    mirror_url = "https://r.jina.ai/" + safe_url
    command = [
        "/usr/bin/curl", "-4", "-fsSL",
        "--connect-timeout", "7", "--max-time", str(timeout),
        "--retry", "1", "--retry-all-errors", "--retry-delay", "1",
        "-H", "X-Return-Format: html",
        "-H", f"User-Agent: {UA}",
        mirror_url,
    ]
    process = subprocess.run(
        command, text=True, capture_output=True, timeout=timeout + 10, check=False,
    )
    text = process.stdout or ""
    lowered = text.lower()
    if _is_cloudflare_hard_block(text):
        remaining = _mark_anitube_origin_protected(safe_url)
        raise OriginProtectedError(url=safe_url, cooldown_remaining_seconds=remaining)
    if process.returncode == 0 and len(text) > 1000 and not any(
        marker in lowered for marker in ("cf-chl", "just a moment", "трохи зачекайте")
    ):
        return text
    raise RuntimeError("Захист сайту не дозволив отримати сторінку відео.")


def _clean_fetch_error(details: list[str]) -> str:
    joined = "\n".join(details).lower()
    if any(token in joined for token in (
        "cf-chl", "just a moment", "трохи зачекайте", "challenge",
        "mdns responder", "phone_registration_error", "deprecated_endpoint",
        "failed to initialize a udev monitor", "error initializing nss",
    )):
        return "Захист сайту не дозволив отримати сторінку відео. Спробуйте ще раз пізніше."
    for detail in reversed(details):
        clean = " ".join(detail.split())
        if clean:
            return clean[-500:]
    return "Сайт тимчасово не відповідає."


def _curl_text(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    timeout: int = 22,
) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    command = [
        "/usr/bin/curl",
        "-4",
        "-fsSL",
        "--connect-timeout",
        "6",
        "--max-time",
        str(timeout),
        "--retry",
        "1",
        "--retry-all-errors",
        "--retry-delay",
        "1",
    ]
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    if params:
        command.append("--get")
        for key, value in params.items():
            command.extend(["--data-urlencode", f"{key}={value}"])

    addresses: list[str] = []
    if host:
        try:
            for result in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
                address = result[4][0]
                if address not in addresses:
                    addresses.append(address)
        except OSError:
            pass

    # Cloudflare edge addresses can be intermittently unreachable from one ISP path.
    # Try every resolved IPv4 address independently instead of exhausting the whole
    # request timeout on the first edge address selected by curl.
    attempts: list[str | None] = addresses or [None]
    errors: list[str] = []
    for address in attempts:
        attempt = list(command)
        if address and host:
            attempt.extend(["--resolve", f"{host}:{port}:{address}"])
        attempt.append(url)
        process = subprocess.run(
            attempt,
            text=True,
            capture_output=True,
            timeout=timeout + 12,
            check=False,
        )
        if process.returncode == 0:
            return process.stdout
        detail = (process.stderr or "curl failed").strip()
        if detail and detail not in errors:
            errors.append(detail)

    # Final normal DNS attempt covers redirects to a different hostname.
    final = list(command)
    final.append(url)
    process = subprocess.run(
        final,
        text=True,
        capture_output=True,
        timeout=timeout + 12,
        check=False,
    )
    if process.returncode == 0:
        return process.stdout
    detail = (process.stderr or "curl failed").strip()
    if detail and detail not in errors:
        errors.append(detail)

    # Some public video pages use a JavaScript Cloudflare challenge. A real
    # browser can complete it and return the rendered DOM; curl and yt-dlp cannot.
    # Keep this fallback bounded and only use it for the initial HTML GET.
    if params is None and parsed.scheme in {"http", "https"}:
        try:
            return _chrome_text(url, timeout=max(65, timeout + 35))
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            chrome_detail = str(exc).strip()
            if chrome_detail and chrome_detail not in errors:
                errors.append(chrome_detail)
        try:
            return _public_html_mirror(url, timeout=max(55, timeout + 25))
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            mirror_detail = str(exc).strip()
            if mirror_detail and mirror_detail not in errors:
                errors.append(mirror_detail)

    raise RuntimeError(_clean_fetch_error(errors))


def _yt_info(url: str) -> dict[str, Any]:
    cmd = [
        YTDLP,
        "--ignore-config",
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--socket-timeout",
        "12",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--extractor-retries",
        "2",
        url,
    ]
    process = subprocess.run(cmd, text=True, capture_output=True, timeout=65, check=False)
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or "yt-dlp failed").strip()[-900:])
    return json.loads(process.stdout)


def _parse(
    info: dict[str, Any],
    voice: str,
    episode: str,
    order: int,
) -> list[dict[str, Any]]:
    title = str(info.get("title") or info.get("fulltitle") or "Відео")
    root_headers = dict(info.get("http_headers") or {})
    formats = list(info.get("formats") or [])
    if not formats and info.get("url"):
        formats = [info]

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    label = f"{voice} · {episode}"

    manifest = info.get("manifest_url")
    if isinstance(manifest, str) and manifest.startswith(("http://", "https://")):
        seen.add(manifest)
        output.append(
            {
                "source_id": _sid(manifest, label + " auto"),
                "url": manifest,
                "kind": "hls-auto" if ".m3u8" in manifest else "manifest",
                "group": voice,
                "translation": voice,
                "episode": episode,
                "quality": "Авто",
                "height": 0,
                "width": None,
                "tbr": None,
                "duration": info.get("duration"),
                "title": title,
                "headers": root_headers,
                "has_drm": bool(info.get("has_drm") or info.get("_has_drm")),
                "order": order,
            }
        )

    for fmt in formats:
        url = fmt.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
            continue
        if fmt.get("vcodec") == "none" and not fmt.get("height"):
            continue
        seen.add(url)
        quality = _quality(fmt)
        output.append(
            {
                "source_id": _sid(url, label + quality),
                "url": url,
                "kind": str(fmt.get("protocol") or ("hls" if ".m3u8" in url else "direct")),
                "group": voice,
                "translation": voice,
                "episode": episode,
                "quality": quality,
                "height": int(fmt.get("height") or 0),
                "width": fmt.get("width"),
                "tbr": fmt.get("tbr"),
                "duration": info.get("duration"),
                "title": title,
                "headers": dict(fmt.get("http_headers") or root_headers),
                "has_drm": bool(fmt.get("has_drm") or info.get("has_drm") or info.get("_has_drm")),
                "order": order,
            }
        )
    return output


def _clean(raw: str, base: str) -> str | None:
    value = html.unescape(raw).replace("\\/", "/").strip(" \t\r\n\"'")
    if value.startswith("//"):
        value = "https:" + value
    value = urljoin(base, value)
    parsed = urlparse(value)
    if (
        parsed.scheme in ("http", "https")
        and parsed.hostname
        and parsed.hostname.lower().endswith("ashdi.vip")
        and "/vod/" in parsed.path
    ):
        return value
    return None


def _generic_embed(raw: str, base: str, tag: str) -> str | None:
    value = html.unescape(raw).replace("\\/", "/").strip(" \t\r\n\"'")
    if value.startswith("//"):
        value = "https:" + value
    value = urljoin(base, value)
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    path = parsed.path.lower()
    media = bool(re.search(r"\.(?:m3u8|mpd|mp4)(?:$|[?#])", value, re.I))
    player = "/vod/" in path or parsed.hostname.lower() == "youtu.be" or parsed.hostname.lower().endswith(".youtube.com")
    if str(tag).lower() not in ("iframe", "video", "source") and not media and not player:
        return None
    try:
        safe, _ = site_registry.validate_public_url(value)
        return safe
    except ValueError:
        return None


def _page_title(document: Any) -> str:
    values = document.xpath("//meta[@property='og:title']/@content | //title/text()")
    for value in values:
        cleaned = " ".join(str(value).split())
        if cleaned:
            return cleaned
    return "Відео"


def _cache_poster(url: str, referer: str) -> str | None:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    existing = next(iter(POSTER_DIR.glob(digest + ".*")), None)
    if existing and existing.is_file() and existing.stat().st_size > 256:
        return f"/posters/{existing.name}"
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses: list[str] = []
    try:
        for row in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
            address = row[4][0]
            if address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    temp = POSTER_DIR / ("." + digest + ".tmp")
    for address in addresses or [None]:
        command = [
            "/usr/bin/curl", "-4", "-fsSL", "--connect-timeout", "6",
            "--max-time", "28", "--retry", "1", "--retry-all-errors",
            "-H", f"User-Agent: {UA}", "-H", f"Referer: {referer}",
            "-o", str(temp),
        ]
        if address:
            command.extend(["--resolve", f"{host}:{port}:{address}"])
        command.append(url)
        process = subprocess.run(command, text=True, capture_output=True, timeout=45, check=False)
        if process.returncode or not temp.exists() or temp.stat().st_size < 256:
            temp.unlink(missing_ok=True)
            continue
        head = temp.read_bytes()[:16]
        if head.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif head.startswith((b"GIF87a", b"GIF89a")):
            ext = ".gif"
        elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            ext = ".webp"
        elif b"ftypavif" in head or b"ftypavis" in head:
            ext = ".avif"
        else:
            temp.unlink(missing_ok=True)
            continue
        final = POSTER_DIR / (digest + ext)
        os.replace(temp, final)
        return f"/posters/{final.name}"
    return None


def _page_poster(document: Any, page_url: str) -> str | None:
    values = document.xpath(
        "//meta[@property='og:image']/@content | "
        "//meta[@name='twitter:image']/@content | "
        "//meta[@property='twitter:image']/@content | "
        "//link[@rel='image_src']/@href | "
        "//*[@itemprop='image']/@content | "
        "//*[@itemprop='image']/@src"
    )
    for raw in values:
        candidate = urljoin(page_url, html.unescape(str(raw)).strip())
        try:
            safe, _ = site_registry.validate_public_url(candidate)
            return _cache_poster(safe, page_url)
        except ValueError:
            continue
    return None


def _page_voice(document: Any) -> str:
    for item in document.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' table-info__item ')]"):
        text = " ".join(" ".join(item.itertext()).split())
        if text.lower().startswith("озвучення:"):
            value = text.split(":", 1)[1].strip()
            if value:
                return value
    values = document.xpath("//*[contains(normalize-space(.), 'Озвучення:')]/text()")
    for value in values:
        text = " ".join(str(value).split())
        if "Озвучення:" in text:
            voice = text.split("Озвучення:", 1)[1].strip()
            if voice:
                return voice
    return "Основне джерело"


def _playlist_targets(page_url: str, text: str, document: Any) -> list[tuple[str, str, str, int]]:
    edit_match = EDIT_TIME.search(text)
    edit_time = edit_match.group(1) if edit_match else "0"
    hash_match = re.search(r"var\s+dle_login_hash\s*=\s*['\"]([^'\"]+)", text, re.I)
    user_hash = hash_match.group(1) if hash_match else ""
    targets: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    order = 0

    # Rendered/cached HTML can already contain the complete playlist. Prefer
    # these direct data-file entries and avoid a second Cloudflare-protected
    # AJAX request when the player list is present.
    direct_items = document.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' playlists-videos ')]//li[@data-file]"
    )
    for item in direct_items:
        style = str(item.get("style") or "").replace(" ", "").lower()
        classes = {part.lower() for part in str(item.get("class") or "").split()}
        if "display:none" in style or item.get("hidden") is not None or str(item.get("aria-hidden") or "").lower() == "true" or classes.intersection({"hidden", "d-none"}):
            continue
        candidate = _clean(str(item.get("data-file") or ""), page_url)
        if not candidate or candidate in seen:
            continue
        voice = " ".join(str(item.get("data-voice") or item.get("data-translation") or "").split())
        if not voice:
            voice = _page_voice(document)
        episode = " ".join(" ".join(item.itertext()).split()) or "Фільм"
        seen.add(candidate)
        targets.append((candidate, voice, episode, order))
        order += 1
    if targets:
        return targets

    for element in document.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' playlists-ajax ')]"):
        news_id = str(element.get("data-news_id") or element.get("data-news-id") or "").strip()
        xfield = str(element.get("data-xfname") or "playlist").strip()
        if not news_id.isdigit() or not xfield:
            continue

        headers = _browser_headers(page_url)
        headers.update(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page_url,
            }
        )
        endpoint = urljoin(page_url, "/engine/ajax/playlists.php")
        try:
            payload = json.loads(
                _curl_text(
                    endpoint,
                    headers=headers,
                    params={
                        "news_id": news_id,
                        "xfield": xfield,
                        "user_hash": user_hash,
                        "time": edit_time,
                    },
                )
            )
        except (RuntimeError, json.JSONDecodeError):
            continue
        if not payload.get("success") or not isinstance(payload.get("response"), str):
            continue

        fragment = lxml_html.fromstring(payload["response"])
        for item in fragment.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' playlists-videos ')]//li[@data-file]"):
            style = str(item.get("style") or "").replace(" ", "").lower()
            classes = {part.lower() for part in str(item.get("class") or "").split()}
            if "display:none" in style or item.get("hidden") is not None or str(item.get("aria-hidden") or "").lower() == "true" or classes.intersection({"hidden", "d-none"}):
                continue
            candidate = _clean(str(item.get("data-file") or ""), page_url)
            if not candidate or candidate in seen:
                continue
            voice = " ".join(str(item.get("data-voice") or "Озвучення не вказано").split())
            episode = " ".join(" ".join(item.itertext()).split()) or "Фільм"
            seen.add(candidate)
            targets.append((candidate, voice, episode, order))
            order += 1
    return targets



def _generic_embed_scan(document: Any, page_url: str) -> list[tuple[str, str, str, int]]:
    found: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    page_voice = _page_voice(document)
    for element in document.iter():
        for attr in ("src", "href", "data-src", "data-url", "data-file", "value"):
            raw = element.get(attr)
            if not raw:
                continue
            candidate = _clean(raw, page_url) or _generic_embed(raw, page_url, str(element.tag))
            if not candidate or candidate in seen:
                continue
            context = " ".join(" ".join(element.itertext()).split())[:100]
            label = page_voice if page_voice != "Основне джерело" else (context or f"Джерело {len(found) + 1}")
            seen.add(candidate)
            found.append((candidate, label, "Фільм", len(found)))

    rendered = lxml_html.tostring(document, encoding="unicode")
    for raw in ASHDI.findall(rendered):
        candidate = _clean(raw, page_url)
        if candidate and candidate not in seen:
            seen.add(candidate)
            label = page_voice if page_voice != "Основне джерело" else f"Джерело {len(found) + 1}"
            found.append((candidate, label, "Фільм", len(found)))
    return found[:60]

def discover(page_url: str) -> tuple[list[tuple[str, str, str, int]], str, str | None]:
    page_url, host = site_registry.validate_public_url(page_url)
    host = host.lower()
    if host == "ashdi.vip" or host.endswith(".ashdi.vip"):
        return [(page_url, "Основне джерело", "Фільм", 0)], "Відео", None

    if host == "anitube.in.ua" or host.endswith(".anitube.in.ua"):
        remaining = _anitube_cooldown_remaining()
        if remaining:
            raise OriginProtectedError(url=page_url, cooldown_remaining_seconds=remaining)
        # AniTube's extensionless route currently serves a persistent challenge,
        # while the canonical DLE article route with .html exposes the same item.
        parsed_page = urlparse(page_url)
        if parsed_page.path and not parsed_page.path.lower().endswith(".html"):
            canonical_path = parsed_page.path.rstrip("/") + ".html"
            page_url = parsed_page._replace(path=canonical_path).geturl()
        title = "Відео"
        poster: str | None = None
        last_document: Any | None = None
        browser_error: BrowserChallengeError | None = None

        # Attempt 1: direct bounded fetch. It may return a challenge document,
        # so success is accepted only when real playlist targets are present.
        try:
            text = _curl_text(page_url, headers=_browser_headers(page_url))
            document = lxml_html.fromstring(text)
            last_document = document
            title = _page_title(document) or title
            poster = _page_poster(document, page_url)
            playlist = _playlist_targets(page_url, text, document)
            if playlist:
                return playlist, title, poster
        except OriginProtectedError:
            raise
        except BrowserChallengeError as exc:
            browser_error = exc
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            pass

        # Attempt 2: explicit rendered DOM. This is deliberately separate from
        # _curl_text so an AniTube challenge page cannot be mistaken for success.
        try:
            text = _chrome_text(page_url, timeout=65)
            document = lxml_html.fromstring(text)
            last_document = document
            title = _page_title(document) or title
            poster = poster or _page_poster(document, page_url)
            playlist = _playlist_targets(page_url, text, document)
            if playlist:
                return playlist, title, poster
        except OriginProtectedError:
            raise
        except BrowserChallengeError as exc:
            if exc.challenge_detected:
                remaining = _mark_anitube_origin_protected(page_url)
                raise OriginProtectedError(url=page_url, cooldown_remaining_seconds=remaining) from exc
            browser_error = exc
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass

        # Attempt 3: rendered public mirror, still subject to public URL and
        # SSRF validation. Useful when origin Chromium cannot complete CF.
        try:
            text = _public_html_mirror(page_url, timeout=55)
            document = lxml_html.fromstring(text)
            last_document = document
            title = _page_title(document) or title
            poster = poster or _page_poster(document, page_url)
            playlist = _playlist_targets(page_url, text, document)
            if playlist:
                return playlist, title, poster
        except OriginProtectedError:
            raise
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            pass

        if last_document is not None:
            # Do not report a page trailer as a successfully resolved AniTube title.
            # The article commonly exposes a YouTube trailer even when the protected
            # episode playlist is unavailable; returning it produced a false READY
            # job with a 37-second trailer instead of the requested movie/episodes.
            found = _generic_embed_scan(last_document, page_url)
            playable = [
                item for item in found
                if "трейлер" not in item[1].lower()
                and "trailer" not in item[1].lower()
            ]
            if playable:
                return playable, title, poster
        if browser_error is not None:
            raise browser_error
        raise RuntimeError("AniTube не повернув доступних відеопотоків.")

    text = _curl_text(page_url, headers=_browser_headers(page_url))
    document = lxml_html.fromstring(text)
    title = _page_title(document)
    poster = _page_poster(document, page_url)
    playlist = _playlist_targets(page_url, text, document)
    if playlist:
        return playlist, title, poster
    found = _generic_embed_scan(document, page_url)
    return (found or [(page_url, "Основне джерело", "Фільм", 0)]), title, poster

def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _canonical_youtube_url(page_url: str) -> str:
    parsed = urlparse(page_url)
    host = (parsed.hostname or "").lower()
    video_id = parsed.path.strip("/").split("/", 1)[0] if host == "youtu.be" else (parse_qs(parsed.query).get("v") or [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    return page_url


def _youtube_native_source(page_url: str, title: str, group: str, duration: Any = None) -> dict[str, Any]:
    clean = _canonical_youtube_url(page_url)
    return {
        "source_id": _sid(clean, "youtube-tv-native"), "url": clean,
        "kind": "youtube-native", "group": group, "translation": "YouTube TV",
        "episode": "Відео", "quality": "YouTube TV", "height": 0, "width": None,
        "tbr": None, "duration": duration, "title": title, "headers": {},
        "has_drm": False, "order": 999, "page_title": title,
    }


def _youtube_result(page_url: str) -> dict[str, Any]:
    clean = _canonical_youtube_url(page_url)
    info = _yt_info(clean)
    title = str(info.get("title") or "YouTube")
    group = str(info.get("uploader") or info.get("channel") or "YouTube")
    duration = info.get("duration")
    poster = str(info.get("thumbnail") or "").strip() or None
    if poster:
        try:
            poster, _ = site_registry.validate_public_url(poster)
            poster = _cache_poster(poster, clean)
        except ValueError:
            poster = None
    available = sorted({int(item.get("height")) for item in info.get("formats") or [] if item.get("height")}, reverse=True)
    choices = [height for height in [2160, 1440, 1080, 720, 480, 360] if height in available]
    base = {
        "url": clean, "kind": "youtube", "group": group, "translation": "MPV",
        "episode": "Відео", "duration": duration, "title": title, "headers": {},
        "has_drm": False, "order": 0, "page_title": title,
    }
    sources: list[dict[str, Any]] = []
    auto_rule = "bestvideo*[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/bestvideo*[vcodec^=avc1][height<=1080]+bestaudio/best[height<=1080]"
    sources.append({**base, "source_id": _sid(clean, auto_rule), "quality": "MPV · Auto (H.264)", "height": 0, "width": None, "tbr": None, "ytdl_format": auto_rule})
    for height in choices:
        rule = f"bestvideo*[vcodec^=avc1][height<={height}]+bestaudio[ext=m4a]/bestvideo*[vcodec^=avc1][height<={height}]+bestaudio/best[height<={height}]"
        sources.append({**base, "source_id": _sid(clean, rule), "quality": f"MPV · {height}p", "height": height, "width": None, "tbr": None, "ytdl_format": rule})
    return {"title": title, "poster": poster, "sources": sources, "errors": ["YouTube TV у Waydroid недоступний через збій Mesa EGL; використовується MPV."]}


def _decode_cinemar_payload(token: str) -> dict[str, Any]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    cleaned = "".join(ch for ch in token if ch in alphabet)
    # The wrapper may append an arbitrary number of noise characters. A final
    # one-character Base64 quantum is invalid; complete only the disposable
    # noisy tail, then add normal padding. The JSON is located independently
    # at bit level below.
    if len(cleaned) % 4 == 1:
        cleaned += "A"
    cleaned += "=" * ((4 - len(cleaned) % 4) % 4)
    decoded = base64.b64decode(cleaned, validate=False)

    # Cinemar prepends noise and shifts the JSON bitstream. Search for the
    # known JSON opening at bit level, then restore byte alignment.
    bits = "".join(f"{byte:08b}" for byte in decoded)
    marker = "".join(f"{byte:08b}" for byte in b'{\"id\"')
    position = bits.find(marker)
    if position < 0:
        raise RuntimeError("Cinemar payload marker not found")
    payload_bits = bits[position:]
    payload = bytes(
        int(payload_bits[index:index + 8], 2)
        for index in range(0, len(payload_bits) - 7, 8)
    ).decode("utf-8", "ignore")
    value, _ = json.JSONDecoder().raw_decode(payload)
    if not isinstance(value, dict):
        raise RuntimeError("Cinemar payload is not an object")
    return value


def _cinemar_sources(
    url: str, voice: str, episode: str, order: int, page_url: str
) -> list[dict[str, Any]]:
    parsed_page = urlparse(page_url)
    page_origin = (
        f"{parsed_page.scheme}://{parsed_page.netloc}/"
        if parsed_page.scheme and parsed_page.netloc
        else page_url
    )
    headers = _browser_headers(page_url)
    headers["Referer"] = page_origin
    text = _curl_text(url, headers=headers)
    match = re.search(r'"file":"([^"\n]+)"', text)
    if not match:
        raise RuntimeError("Cinemar media payload not found")
    payload = _decode_cinemar_payload(match.group(1))
    media_url = str(payload.get("file") or "").replace("\\/", "/").strip()
    media_url, _ = site_registry.validate_public_url(media_url)
    label = " ".join(str(payload.get("title") or voice).split()) or voice
    info = _yt_info(media_url)
    sources = _parse(info, label, episode, order)
    duration = payload.get("duration")
    for source in sources:
        source_headers = dict(source.get("headers") or {})
        source_headers.setdefault("User-Agent", UA)
        source_headers.setdefault("Referer", page_origin)
        source["headers"] = source_headers
        if not source.get("duration") and duration:
            source["duration"] = duration
        source["title"] = label
    return sources


def _resolve_one(
    target: tuple[str, str, str, int], page_url: str
) -> tuple[list[dict[str, Any]], str | None]:
    url, voice, episode, order = target
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()
    if host == "cinemar.cc" or host.endswith(".cinemar.cc"):
        try:
            return _cinemar_sources(url, voice, episode, order, page_url), None
        except Exception as exc:
            return [], f"{voice} · {episode}: {exc}"
    if host.startswith("cvt-") and "/iframe/" in path:
        return [], None
    error: Exception | None = None
    for attempt in range(2):
        try:
            return _parse(_yt_info(url), voice, episode, order), None
        except Exception as exc:
            error = exc
            if attempt == 0:
                time.sleep(0.7)

    if DIRECT.search(url):
        label = f"{voice} · {episode}"
        return (
            [
                {
                    "source_id": _sid(url, label),
                    "url": url,
                    "kind": "direct",
                    "group": voice,
                    "translation": voice,
                    "episode": episode,
                    "quality": "Авто",
                    "height": 0,
                    "width": None,
                    "tbr": None,
                    "duration": None,
                    "title": label,
                    "headers": {"User-Agent": UA},
                    "has_drm": False,
                    "order": order,
                }
            ],
            None,
        )
    return [], f"{voice} · {episode}: {error}"


def resolve_page(page_url: str) -> dict[str, Any]:
    if _is_youtube(page_url):
        return _youtube_result(page_url)
    targets, page_title, poster = discover(page_url)
    collected: list[dict[str, Any]] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        futures = [pool.submit(_resolve_one, target, page_url) for target in targets]
        for future in as_completed(futures):
            sources, error = future.result()
            collected.extend(sources)
            if error:
                errors.append(error)

    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in collected:
        key = (str(item.get("url")), str(item.get("group")), str(item.get("episode")))
        deduplicated[key] = item

    sources = list(deduplicated.values())
    sources.sort(
        key=lambda item: (
            int(item.get("order") or 0),
            0 if item.get("quality") == "Авто" else 1,
            -int(item.get("height") or 0),
            -float(item.get("tbr") or 0),
        )
    )
    if not sources:
        raise RuntimeError("; ".join(errors[:8]) or "Потоки не знайдені")

    for source in sources:
        source["page_title"] = page_title
    return {"title": page_title, "poster": poster, "sources": sources, "errors": errors}
