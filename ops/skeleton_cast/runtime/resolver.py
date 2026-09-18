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
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

HOME = Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
YTDLP = os.environ.get('SKELETON_MEDIA_YTDLP', str(HOME / '.local/bin/yt-dlp'))
POSTER_DIR = Path(os.environ.get('SKELETON_MEDIA_POSTER_DIR', str(HOME / '.local/state/skeleton-cast/posters'))).expanduser()
POSTER_CANONICAL_SIZE = (500, 750)
POSTER_CANONICAL_SUFFIX = ".poster.webp"
YOUTUBE_POSTER_SUFFIX = ".youtube.poster.webp"
YOUTUBE_POSTER_VERSION = "youtube-portrait-v1"
YOUTUBE_LANDSCAPE_SUFFIX = ".youtube.landscape.webp"
YOUTUBE_LANDSCAPE_VERSION = "youtube-landscape-v2"
ANITUBE_COOLDOWN_PATH = Path(os.environ.get('SKELETON_MEDIA_ANITUBE_COOLDOWN', str(HOME / '.local/state/skeleton-cast/anitube-origin-cooldown.json'))).expanduser()
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


def _format_audio_state(fmt: dict[str, Any]) -> bool | None:
    acodec = str(fmt.get("acodec") or "").strip().lower()
    if acodec and acodec != "none":
        return True
    if acodec == "none":
        return False
    audio_ext = str(fmt.get("audio_ext") or "").strip().lower()
    if audio_ext and audio_ext != "none":
        return True
    return None


def _real_video_format(fmt: dict[str, Any]) -> bool:
    vcodec = str(fmt.get("vcodec") or "").strip().lower()
    protocol = str(fmt.get("protocol") or "").strip().lower()
    ext = str(fmt.get("ext") or "").strip().lower()
    if vcodec == "none":
        return False
    if protocol == "mhtml" or ext == "mhtml":
        return False
    return bool(vcodec or fmt.get("height") or fmt.get("width"))


def _youtube_mpv_sources_from_info(
    info: dict[str, Any], voice: str, episode: str, order: int, title: str
) -> list[dict[str, Any]]:
    page_url = str(info.get("webpage_url") or info.get("original_url") or "").strip()
    if not page_url or not _is_youtube(page_url):
        return []
    clean = _canonical_youtube_url(page_url)
    available = sorted(
        {
            int(item.get("height"))
            for item in info.get("formats") or []
            if _real_video_format(item) and item.get("height")
        },
        reverse=True,
    )
    choices = [height for height in [2160, 1440, 1080, 720, 480, 360] if height in available]
    base = {
        "url": clean,
        "kind": "youtube",
        "group": voice,
        "translation": voice,
        "episode": episode,
        "duration": info.get("duration"),
        "title": title,
        "headers": {},
        "has_drm": bool(info.get("has_drm") or info.get("_has_drm")),
        "has_audio": True,
        "audio_strategy": "yt-dlp-merge",
        "audio_codec": "best available",
        "order": order,
    }
    sources: list[dict[str, Any]] = []
    auto_rule = "bestvideo*[height<=1080]+bestaudio/best[height<=1080]"
    sources.append({
        **base,
        "source_id": _sid(clean, f"{voice} {episode} {auto_rule}"),
        "quality": "Авто · зі звуком",
        "height": 0,
        "width": None,
        "tbr": None,
        "ytdl_format": auto_rule,
    })
    for height in choices:
        rule = f"bestvideo*[height<={height}]+bestaudio/best[height<={height}]"
        sources.append({
            **base,
            "source_id": _sid(clean, f"{voice} {episode} {rule}"),
            "quality": f"{height}p · зі звуком",
            "height": height,
            "width": None,
            "tbr": None,
            "ytdl_format": rule,
        })
    return sources


def _parse(
    info: dict[str, Any],
    voice: str,
    episode: str,
    order: int,
) -> list[dict[str, Any]]:
    title = str(info.get("title") or info.get("fulltitle") or "Відео")
    youtube_sources = _youtube_mpv_sources_from_info(info, voice, episode, order, title)
    if youtube_sources:
        return youtube_sources

    root_headers = dict(info.get("http_headers") or {})
    raw_formats = list(info.get("formats") or [])
    if not raw_formats and info.get("url"):
        raw_formats = [info]
    formats = [fmt for fmt in raw_formats if _real_video_format(fmt)]
    has_confirmed_audio = any(_format_audio_state(fmt) is True for fmt in formats)

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    label = f"{voice} · {episode}"

    manifest = info.get("manifest_url")
    manifest_audio = True if any(_format_audio_state(fmt) is True for fmt in formats) else None
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
                "quality": "Авто" + (" · зі звуком" if manifest_audio is True else ""),
                "height": 0,
                "width": None,
                "tbr": None,
                "duration": info.get("duration"),
                "title": title,
                "headers": root_headers,
                "has_drm": bool(info.get("has_drm") or info.get("_has_drm")),
                "has_audio": manifest_audio,
                "audio_strategy": "manifest",
                "audio_codec": None,
                "order": order,
            }
        )

    for fmt in formats:
        url = fmt.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
            continue
        audio_state = _format_audio_state(fmt)
        if has_confirmed_audio and audio_state is False:
            continue
        seen.add(url)
        quality = _quality(fmt)
        if audio_state is True and "зі звуком" not in quality:
            quality += " · зі звуком"
        elif audio_state is False:
            quality += " · без звуку"
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
                "has_audio": audio_state,
                "audio_strategy": "muxed" if audio_state is True else ("video-only" if audio_state is False else "unknown"),
                "audio_codec": fmt.get("acodec"),
                "video_codec": fmt.get("vcodec"),
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


def _canonical_poster_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.hostname or '').lower() == 'image.tmdb.org':
        path = re.sub(r'^/t/p/(?:original|w\d+)/', '/t/p/w500/', parsed.path)
        return parsed._replace(path=path).geturl()
    return url


def _normalize_poster(source: Path, digest: str) -> Path | None:
    final = POSTER_DIR / (digest + POSTER_CANONICAL_SUFFIX)
    if final.is_file() and final.stat().st_size > 512:
        return final
    temp = POSTER_DIR / ('.' + digest + '.poster.tmp.webp')
    try:
        with Image.open(source) as image:
            if getattr(image, 'is_animated', False):
                image.seek(0)
            if image.mode in {'RGBA', 'LA'}:
                background = Image.new('RGB', image.size, '#0a0d10')
                alpha = image.getchannel('A')
                background.paste(image.convert('RGB'), mask=alpha)
                image = background
            else:
                image = image.convert('RGB')
            image = ImageOps.fit(
                image, POSTER_CANONICAL_SIZE, method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            image.save(temp, 'WEBP', quality=88, method=6)
        os.replace(temp, final)
        os.chmod(final, 0o600)
        return final
    except (OSError, ValueError, UnidentifiedImageError):
        temp.unlink(missing_ok=True)
        return None


def _youtube_thumbnail_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    if (parsed.hostname or '').lower() not in {'i.ytimg.com', 'img.youtube.com'}:
        return []
    match = re.search(r'/vi(?:_webp)?/([^/]+)/', parsed.path)
    if not match:
        return [url]
    video_id = match.group(1)
    values = [
        f'https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg',
        f'https://i.ytimg.com/vi/{video_id}/sddefault.jpg',
        f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
        url,
    ]
    return list(dict.fromkeys(values))


def _trim_youtube_letterbox(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width < 2 or height < 2:
        return image
    mask = image.convert('L').point(lambda value: 255 if value > 14 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image
    top, bottom = bbox[1], bbox[3]
    top = max(0, min(top, int(height * 0.24)))
    bottom = min(height, max(bottom, int(height * 0.76)))
    visible = bottom - top
    if visible < int(height * 0.94) and visible >= int(height * 0.45):
        return image.crop((0, top, width, bottom))
    return image


def _normalize_youtube_poster(source: Path, digest: str) -> Path | None:
    final = POSTER_DIR / (digest + YOUTUBE_POSTER_SUFFIX)
    if final.is_file() and final.stat().st_size > 512:
        return final
    temp = POSTER_DIR / ('.' + digest + '.youtube.tmp.webp')
    try:
        with Image.open(source) as opened:
            if getattr(opened, 'is_animated', False):
                opened.seek(0)
            image = opened.convert('RGB')
        image = _trim_youtube_letterbox(image)
        background = ImageOps.fit(image, POSTER_CANONICAL_SIZE, method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=28))
        background = ImageEnhance.Brightness(background).enhance(0.38)
        background = ImageEnhance.Color(background).enhance(0.72)
        foreground = ImageOps.contain(image, (472, 670), method=Image.Resampling.LANCZOS)
        x = (500 - foreground.width) // 2
        y = (750 - foreground.height) // 2
        shadow = Image.new('RGB', (foreground.width + 16, foreground.height + 16), '#050607')
        background.paste(shadow, ((500 - shadow.width) // 2, (750 - shadow.height) // 2 + 4))
        background.paste(foreground, (x, y))
        background.save(temp, 'WEBP', quality=88, method=6)
        os.replace(temp, final)
        os.chmod(final, 0o600)
        return final
    except (OSError, ValueError, UnidentifiedImageError):
        temp.unlink(missing_ok=True)
        return None


def _cache_youtube_poster(url: str, referer: str) -> str | None:
    candidates = _youtube_thumbnail_candidates(url)
    if not candidates:
        return _cache_poster(url, referer)
    digest = hashlib.sha256((YOUTUBE_POSTER_VERSION + '|' + candidates[0]).encode()).hexdigest()[:24]
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    final = POSTER_DIR / (digest + YOUTUBE_POSTER_SUFFIX)
    if final.is_file() and final.stat().st_size > 512:
        return f'/posters/{final.name}'
    temp = POSTER_DIR / ('.' + digest + '.youtube.source')
    for candidate in candidates:
        try:
            response = requests.get(candidate, headers={'User-Agent': UA, 'Referer': referer}, timeout=(6, 28))
            response.raise_for_status()
            if len(response.content) < 1024:
                continue
            temp.write_bytes(response.content)
            with Image.open(temp) as probe:
                width, height = probe.size
            if width < 320 or height < 180:
                temp.unlink(missing_ok=True)
                continue
            result = _normalize_youtube_poster(temp, digest)
            temp.unlink(missing_ok=True)
            if result is not None:
                return f'/posters/{result.name}'
        except (OSError, ValueError, requests.RequestException, UnidentifiedImageError):
            temp.unlink(missing_ok=True)
    return None


def _cache_youtube_landscape_poster(url: str, referer: str) -> str | None:
    candidates = _youtube_thumbnail_candidates(url)
    if not candidates:
        return None
    digest = hashlib.sha256((YOUTUBE_LANDSCAPE_VERSION + '|' + candidates[0]).encode()).hexdigest()[:24]
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    final = POSTER_DIR / (digest + YOUTUBE_LANDSCAPE_SUFFIX)
    if final.is_file() and final.stat().st_size > 512:
        return f'/posters/{final.name}'
    temp_source = POSTER_DIR / ('.' + digest + '.youtube.landscape.source')
    temp_final = POSTER_DIR / ('.' + digest + '.youtube.landscape.tmp.webp')
    for candidate in candidates:
        try:
            response = requests.get(candidate, headers={'User-Agent': UA, 'Referer': referer}, timeout=(6, 28))
            response.raise_for_status()
            if len(response.content) < 1024:
                continue
            temp_source.write_bytes(response.content)
            with Image.open(temp_source) as opened:
                if getattr(opened, 'is_animated', False):
                    opened.seek(0)
                image = opened.convert('RGB')
            image = _trim_youtube_letterbox(image)
            if image.width < 320 or image.height < 180:
                temp_source.unlink(missing_ok=True)
                continue
            image = ImageOps.fit(image, (960, 540), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            image.save(temp_final, 'WEBP', quality=88, method=6)
            os.replace(temp_final, final)
            os.chmod(final, 0o600)
            temp_source.unlink(missing_ok=True)
            return f'/posters/{final.name}'
        except (OSError, ValueError, requests.RequestException, UnidentifiedImageError):
            temp_source.unlink(missing_ok=True)
            temp_final.unlink(missing_ok=True)
    return None


def _cache_poster(url: str, referer: str) -> str | None:
    url = _canonical_poster_url(url)
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    canonical = POSTER_DIR / (digest + POSTER_CANONICAL_SUFFIX)
    if canonical.is_file() and canonical.stat().st_size > 512:
        return f"/posters/{canonical.name}"
    existing = next((item for item in POSTER_DIR.glob(digest + ".*") if item.is_file() and not item.name.startswith('.') and item.stat().st_size > 256), None)
    if existing:
        normalized = _normalize_poster(existing, digest)
        return f"/posters/{(normalized or existing).name}"
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
        normalized = _normalize_poster(temp, digest)
        if normalized is not None:
            temp.unlink(missing_ok=True)
            return f"/posters/{normalized.name}"
        final = POSTER_DIR / (digest + ext)
        os.replace(temp, final)
        os.chmod(final, 0o600)
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


def _page_description(document: Any) -> str:
    values: list[str] = []
    values.extend(str(value) for value in document.xpath(
        "//meta[@property='og:description']/@content | "
        "//meta[@name='description']/@content | "
        "//meta[@name='twitter:description']/@content | "
        "//meta[@property='twitter:description']/@content | "
        "//*[@itemprop='description']/@content"
    ))
    for script in document.xpath("//script[@type='application/ld+json']/text()"):
        try:
            payload = json.loads(str(script))
        except Exception:
            continue
        queue = payload if isinstance(payload, list) else [payload]
        for item in queue:
            if isinstance(item, dict):
                if item.get('description'):
                    values.append(str(item.get('description')))
                graph = item.get('@graph')
                if isinstance(graph, list):
                    for node in graph:
                        if isinstance(node, dict) and node.get('description'):
                            values.append(str(node.get('description')))
    candidates: list[tuple[int, str]] = []
    for raw in values:
        text = html.unescape(str(raw))
        text = re.sub(r'<(?:script|style)[^>]*>.*?</(?:script|style)>', ' ', text, flags=re.I | re.S)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = ' '.join(text.split()).strip()
        if len(text) < 45:
            continue
        lowered = text.casefold()
        technical = ('#sidebar','padding:','display:','font-family','var(--','@media','function(','document.','background:','grid-template')
        if any(token in lowered for token in technical):
            continue
        if text.count('{') + text.count('}') >= 2 or text.count(';') >= 8:
            continue
        penalty = 0
        for marker in ('дивитися ', 'смотреть ', 'онлайн', 'без реєстрації', 'без регистрации', 'приємного перегляду', 'бесплатно'):
            if marker in lowered:
                penalty += 220
        score = min(len(text), 1800) - penalty
        candidates.append((score, text[:1800]))
    if not candidates:
        return ''
    candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return candidates[0][1]


def page_metadata(page_url: str, *, timeout: int = 45) -> dict[str, Any]:
    safe_url, _ = site_registry.validate_public_url(page_url)
    documents: list[Any] = []
    try:
        text = _curl_text(safe_url, headers=_browser_headers(safe_url), timeout=min(timeout, 24))
        if len(text) > 800:
            documents.append(lxml_html.fromstring(text))
    except Exception:
        pass
    best: dict[str, Any] = {'page_url': safe_url, 'title': '', 'poster': None, 'overview': '', 'original_title': '', 'year': None}

    def apply_structured(document: Any) -> None:
        best['title'] = _page_title(document) or best['title']
        best['poster'] = _page_poster(document, safe_url) or best['poster']
        best['overview'] = _page_description(document) or best['overview']
        original = str(document.xpath("string(//*[@itemprop='alternateName'][1])") or '').strip()
        if original:
            best['original_title'] = ' '.join(original.split())
        raw_year = str(document.xpath("string(//*[@itemprop='copyrightYear'][1])") or '').strip()
        match = re.search(r'\b(19\d{2}|20\d{2})\b', raw_year)
        if match:
            best['year'] = int(match.group(1))

    for document in documents:
        apply_structured(document)
    if best['poster'] and best['overview'] and (best['original_title'] or best['year']):
        return best
    try:
        text = _chrome_text(safe_url, timeout=timeout)
        document = lxml_html.fromstring(text)
        apply_structured(document)
    except Exception:
        pass
    return best

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

    if host == "lavakino.net" or host.endswith(".lavakino.net"):
        text = _curl_text(page_url, headers=_browser_headers(page_url), timeout=20)
        document = lxml_html.fromstring(text)
        title = _page_title(document)
        poster = _page_poster(document, page_url)
        targets: list[tuple[str, str, str, int]] = []
        seen: set[str] = set()
        for raw in document.xpath('//iframe/@src | //iframe/@data-src'):
            candidate = _generic_embed(str(raw), page_url, 'iframe')
            if not candidate or candidate in seen:
                continue
            player_host = (urlparse(candidate).hostname or '').lower()
            if player_host == 'api.zenithjs.ws' or player_host.endswith('.zenithjs.ws'):
                seen.add(candidate)
                targets.append((candidate, 'Lavakino · Zenith', 'Серіал', len(targets)))
        if targets:
            return targets, title, poster
        found = _generic_embed_scan(document, page_url)
        return (found or [(page_url, 'Основне джерело', 'Фільм', 0)]), title, poster

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
            # A trailer is still a valid preview of the canonical work.  It is
            # returned and classified upstream as trailer_only, never as a full release.
            found = _generic_embed_scan(last_document, page_url)
            if found:
                return found, title, poster
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
            poster = _cache_youtube_poster(poster, clean)
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
    return {"title": title, "poster": poster, "sources": sources, "errors": ["Для цього посилання використовується MPV з обмеженням якості до 720p."]}


def _decode_cinemar_playerjs_playlist(token: str) -> list[dict[str, Any]]:
    """Decode current Cinemar/PlayerJS #2 playlist format.

    Mirrors the public player implementation: custom slice -> atob ->
    escape/decodeURIComponent -> JSON.parse. No JavaScript execution is needed.
    """
    if not token.startswith('#2'):
        raise RuntimeError('Cinemar PlayerJS playlist prefix missing')
    value = token[2:]
    if len(value) < 4 or not value[:2].isdigit():
        raise RuntimeError('Cinemar PlayerJS separator missing')
    separator = chr(int(value[:2]))
    payload = value[2:]
    pieces: list[str] = []
    for piece in payload.split(separator):
        if not piece:
            pieces.append(piece)
            continue
        try:
            shift = int(piece[-1])
        except ValueError:
            pieces.append(piece)
            continue
        if len(piece) > 32:
            # JS: e.substr(2*t, e.length-3*t-1) + e.substr(0,t)
            length = len(piece) - 3 * shift - 1
            piece = piece[2 * shift:2 * shift + max(0, length)] + piece[:shift]
        pieces.append(piece)
    encoded = ''.join(pieces)
    encoded += '=' * ((4 - len(encoded) % 4) % 4)
    try:
        text = base64.b64decode(encoded, validate=False).decode('utf-8')
        result = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f'Cinemar PlayerJS playlist decode failed: {type(exc).__name__}') from exc
    if not isinstance(result, list):
        raise RuntimeError('Cinemar PlayerJS playlist is not a list')
    return result


def _decode_cinemar_payload(token: str) -> dict[str, Any]:
    """Legacy Cinemar payload decoder retained for older embeds."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    cleaned = "".join(ch for ch in token if ch in alphabet)
    if len(cleaned) % 4 == 1:
        cleaned += "A"
    cleaned += "=" * ((4 - len(cleaned) % 4) % 4)
    decoded = base64.b64decode(cleaned, validate=False)
    bits = "".join(f"{byte:08b}" for byte in decoded)
    marker = "".join(f"{byte:08b}" for byte in b'{"id"')
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


def _cinemar_playlist_files(value: Any, *, season: str = '', episode: str = '') -> list[tuple[dict[str, Any], str, str]]:
    output: list[tuple[dict[str, Any], str, str]] = []
    if isinstance(value, list):
        for item in value:
            output.extend(_cinemar_playlist_files(item, season=season, episode=episode))
        return output
    if not isinstance(value, dict):
        return output
    title = ' '.join(str(value.get('title') or '').split())
    next_season, next_episode = season, episode
    season_match = re.search(r'(?:сезон|season)\s*(\d+)', title, re.I)
    episode_match = re.search(r'(?:сер(?:ия|ія)|episode)\s*(\d+)', title, re.I)
    if season_match:
        next_season = season_match.group(1)
    if episode_match:
        next_episode = episode_match.group(1)
    folder = value.get('folder')
    if isinstance(folder, list):
        output.extend(_cinemar_playlist_files(folder, season=next_season, episode=next_episode))
    if value.get('file'):
        title2 = ' '.join(str(value.get('title2') or '').replace('\xa0', ' ').split())
        sm = re.search(r'(\d+)\s*(?:сезон|season)', title2, re.I)
        em = re.search(r'(\d+)\s*(?:сер(?:ия|ія)|episode)', title2, re.I)
        output.append((value, sm.group(1) if sm else next_season, em.group(1) if em else next_episode))
    return output


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
    token = html.unescape(match.group(1)).replace('\\/', '/').strip()
    if token.startswith('#2'):
        playlist = _decode_cinemar_playerjs_playlist(token)
        files = _cinemar_playlist_files(playlist)
        if not files:
            raise RuntimeError('Cinemar playlist is empty')
        sources: list[dict[str, Any]] = []
        for index, (item, season, item_episode) in enumerate(files):
            raw_url = str(item.get('file') or '').strip()
            if raw_url.startswith('//'):
                raw_url = 'https:' + raw_url
            try:
                media_url, _ = site_registry.validate_public_url(raw_url)
            except ValueError:
                continue
            raw_label = ' '.join(str(item.get('title') or voice or 'Озвучення').split()) or voice
            label = ' '.join(re.sub(r'<[^>]+>', ' ', raw_label).split()) or voice
            label_key = raw_label.casefold()
            if 'flags/ua.png' in label_key or 'украинск' in label_key or 'україн' in label_key:
                audio_language = 'uk'
            elif 'flags/us.png' in label_key or 'english' in label_key or 'original' in label_key:
                audio_language = 'en'
            elif (urlparse(page_url).hostname or '').lower().endswith('kinogo.online'):
                audio_language = 'ru'
            else:
                audio_language = 'unknown'
            episode_label = item_episode or episode or '1'
            source_headers = {'User-Agent': UA, 'Referer': page_origin}
            is_hls = '.m3u8' in media_url.lower()
            source = {
                'source_id': _sid(media_url, f'{label}|s{season}|e{episode_label}|cinemar'),
                'url': media_url,
                'kind': 'hls-auto' if is_hls else 'direct',
                'group': label,
                'translation': label,
                'season': season or '',
                'episode': episode_label,
                'quality': 'Авто',
                'height': 0,
                'width': None,
                'tbr': None,
                'duration': item.get('duration'),
                'title': ' '.join(str(item.get('title2') or label).replace('\xa0', ' ').split()),
                'headers': source_headers,
                'has_drm': False,
                'has_audio': None,
                'audio_strategy': 'manifest' if is_hls else 'direct',
                'audio_codec': None,
                'audio_language': audio_language,
                'order': order * 1000 + index,
            }
            subtitle = str(item.get('subtitle') or '').strip()
            if subtitle:
                source['subtitle'] = subtitle
            sources.append(source)
        if not sources:
            raise RuntimeError('Cinemar playlist has no public media URLs')
        return sources

    payload = _decode_cinemar_payload(token)
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


def _ashdi_sources(url: str, voice: str, episode: str, order: int) -> list[dict[str, Any]]:
    text = _curl_text(url, headers=_browser_headers(url), timeout=20)
    match = re.search(r"\bfile\s*:\s*['\"]([^'\"]+\.m3u8[^'\"]*)", text, re.I)
    if not match:
        raise RuntimeError('Ashdi player has no HLS manifest')
    manifest = html.unescape(match.group(1)).replace('\\/', '/').strip()
    manifest, _ = site_registry.validate_public_url(manifest)
    headers = {'User-Agent': UA, 'Referer': url}
    master = _curl_text(manifest, headers=headers, timeout=20)
    title = f'{voice} · {episode}'
    base = {
        'group': voice, 'translation': voice, 'episode': episode,
        'duration': None, 'title': title, 'headers': headers,
        'has_drm': False, 'has_audio': True, 'audio_strategy': 'manifest',
        'audio_codec': None, 'video_codec': None, 'order': order,
    }
    sources: list[dict[str, Any]] = [{
        **base, 'source_id': _sid(manifest, title + ' auto'), 'url': manifest,
        'kind': 'hls-auto', 'quality': 'Авто · зі звуком',
        'height': 0, 'width': None, 'tbr': None,
    }]
    lines = [line.strip() for line in master.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF:'):
            continue
        attrs = line.split(':', 1)[1]
        resolution = re.search(r'RESOLUTION=(\d+)x(\d+)', attrs, re.I)
        bandwidth = re.search(r'BANDWIDTH=(\d+)', attrs, re.I)
        variant = ''
        for candidate in lines[index + 1:]:
            if not candidate or candidate.startswith('#'):
                continue
            variant = candidate
            break
        if not variant:
            continue
        variant_url = urljoin(manifest, variant)
        variant_url, _ = site_registry.validate_public_url(variant_url)
        width = int(resolution.group(1)) if resolution else None
        height = int(resolution.group(2)) if resolution else 0
        tbr = (int(bandwidth.group(1)) / 1000.0) if bandwidth else None
        quality = f'{height}p' if height else 'HLS'
        if width and height:
            quality += f' · {width}×{height}'
        quality += ' · зі звуком'
        sources.append({
            **base, 'source_id': _sid(variant_url, title + quality), 'url': variant_url,
            'kind': 'm3u8_native', 'quality': quality,
            'height': height, 'width': width, 'tbr': tbr,
        })
    return sources



def _balanced_json_value(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] not in '[{':
        raise RuntimeError('JSON value start missing')
    opener = text[start]
    closer = ']' if opener == '[' else '}'
    depth = 0
    quote = ''
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if quote:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = ''
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise RuntimeError('Unterminated JSON value')


def _zenith_sources(url: str, voice: str, episode: str, order: int, page_url: str) -> list[dict[str, Any]]:
    headers = _browser_headers(url)
    headers['Referer'] = page_url
    text = _curl_text(url, headers=headers, timeout=20)
    match = re.search(r'\bseasons\s*:\s*\[', text, re.I)
    if not match:
        raise RuntimeError('Zenith player has no seasons playlist')
    raw = _balanced_json_value(text, match.end() - 1)
    try:
        seasons = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Zenith seasons playlist JSON invalid') from exc
    if not isinstance(seasons, list):
        raise RuntimeError('Zenith seasons playlist is not a list')
    output: list[dict[str, Any]] = []
    index = 0
    for season_item in seasons:
        if not isinstance(season_item, dict) or season_item.get('blocked') is True:
            continue
        season = str(season_item.get('season') or '').strip()
        episodes = season_item.get('episodes') if isinstance(season_item.get('episodes'), list) else []
        for item in episodes:
            if not isinstance(item, dict):
                continue
            manifest = html.unescape(str(item.get('hls') or '')).replace('\\/', '/').strip()
            if not manifest:
                continue
            try:
                manifest, _ = site_registry.validate_public_url(manifest)
            except ValueError:
                continue
            ep_num = str(item.get('episode') or '').strip()
            ep_label = f'Серія {ep_num}' if ep_num else (episode or 'Фільм')
            audio = item.get('audio') if isinstance(item.get('audio'), dict) else {}
            names = [' '.join(str(v).split()) for v in (audio.get('names') or []) if str(v).strip()]
            label = ' / '.join(names) or voice or 'Zenith'
            title = ' '.join(str(item.get('title') or f'{label} · {ep_label}').split())
            source_headers = {'User-Agent': UA, 'Referer': url}
            output.append({
                'source_id': _sid(manifest, f'zenith|s{season}|e{ep_num}|{label}'),
                'url': manifest,
                'kind': 'hls-auto',
                'group': label,
                'translation': label,
                'season': season,
                'episode': ep_label,
                'quality': 'HLS · Auto',
                'height': 0,
                'width': None,
                'tbr': None,
                'duration': item.get('duration'),
                'title': title,
                'headers': source_headers,
                'has_drm': False,
                'has_audio': True,
                'audio_strategy': 'manifest',
                'audio_codec': None,
                'video_codec': 'h264',
                'order': (int(season) if season.isdigit() else 999) * 10000 + (int(ep_num) if ep_num.isdigit() else index),
            })
            index += 1
    if not output:
        raise RuntimeError('Zenith playlist has no public HLS episodes')
    return output

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
    if host == "api.zenithjs.ws" or host.endswith(".zenithjs.ws"):
        try:
            return _zenith_sources(url, voice, episode, order, page_url), None
        except Exception as exc:
            return [], f"{voice} · {episode}: {exc}"
    if (host == "ashdi.vip" or host.endswith(".ashdi.vip")) and "/vod/" in path:
        try:
            return _ashdi_sources(url, voice, episode, order), None
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

    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
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
            0 if item.get("has_audio") is True else (2 if item.get("has_audio") is False else 1),
            -int(item.get("height") or 0),
            0 if str(item.get("quality") or "").startswith("Авто") else 1,
            int(item.get("order") or 0),
            -float(item.get("tbr") or 0),
        )
    )
    if not sources:
        raise RuntimeError("; ".join(errors[:8]) or "Потоки не знайдені")

    for source in sources:
        source["page_title"] = page_title
    return {"title": page_title, "poster": poster, "sources": sources, "errors": errors}
