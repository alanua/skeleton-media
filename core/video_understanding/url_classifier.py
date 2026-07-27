from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from core.video_understanding.models import VideoUnderstandingError


_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_VIMEO_HOSTS = {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}
_DIRECT_MEDIA_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".mp3", ".m4a", ".wav", ".flac"}
_FORBIDDEN_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}
_FORBIDDEN_SUFFIXES = (".local", ".internal", ".localhost", ".home", ".lan")
_OBFUSCATED_IP_RE = re.compile(r"^(?:0x[0-9a-f]+|[0-9a-fx.]+)$", re.IGNORECASE)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
_VIMEO_ID_RE = re.compile(r"^[0-9]{3,20}$")


@dataclass(frozen=True)
class SourceClassification:
    source_type: str
    adapter: str
    reason_code: str
    normalized_private_source: str
    source_token: str


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _reject_host(host: str) -> None:
    normalized = host.casefold().rstrip(".")
    if normalized in _FORBIDDEN_HOSTS or normalized.endswith(_FORBIDDEN_SUFFIXES):
        raise VideoUnderstandingError("UNSAFE_TARGET", "local or private host is not allowed")
    try:
        address = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        if _OBFUSCATED_IP_RE.fullmatch(normalized) and any(character.isdigit() for character in normalized):
            raise VideoUnderstandingError("UNSAFE_TARGET", "obfuscated numeric host is not allowed")
        return
    if not address.is_global:
        raise VideoUnderstandingError("UNSAFE_TARGET", "non-global IP target is not allowed")


def classify_remote_url(raw_url: str) -> SourceClassification:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise VideoUnderstandingError("INVALID_URL", "URL must be non-empty")
    if len(raw_url) > 4096:
        raise VideoUnderstandingError("URL_TOO_LARGE", "URL is too large")
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme.casefold() != "https":
        raise VideoUnderstandingError("UNSUPPORTED_SCHEME", "remote video URLs must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise VideoUnderstandingError("URL_USERINFO_FORBIDDEN", "URL userinfo is forbidden")
    if parsed.fragment:
        raise VideoUnderstandingError("URL_FRAGMENT_FORBIDDEN", "URL fragments are forbidden")
    try:
        port = parsed.port
    except ValueError as exc:
        raise VideoUnderstandingError("INVALID_PORT", "URL port is invalid") from exc
    if port not in (None, 443):
        raise VideoUnderstandingError("UNSUPPORTED_PORT", "only the HTTPS default port is allowed")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        raise VideoUnderstandingError("INVALID_HOST", "URL host is missing")
    _reject_host(host)

    if host in _YOUTUBE_HOSTS:
        normalized = _normalize_youtube(parsed)
        return SourceClassification(
            source_type="REMOTE_VIDEO",
            adapter="youtube",
            reason_code="SUPPORTED_YOUTUBE",
            normalized_private_source=normalized,
            source_token=_token(normalized),
        )
    if host in _VIMEO_HOSTS:
        normalized = _normalize_vimeo(parsed)
        return SourceClassification(
            source_type="REMOTE_VIDEO",
            adapter="vimeo",
            reason_code="SUPPORTED_VIMEO",
            normalized_private_source=normalized,
            source_token=_token(normalized),
        )

    suffix = PurePosixPath(parsed.path).suffix.casefold()
    if suffix in _DIRECT_MEDIA_SUFFIXES:
        normalized = urlunsplit(("https", host, parsed.path, parsed.query, ""))
        return SourceClassification(
            source_type="REMOTE_MEDIA",
            adapter="direct_media",
            reason_code="SUPPORTED_DIRECT_MEDIA",
            normalized_private_source=normalized,
            source_token=_token(normalized),
        )
    raise VideoUnderstandingError("UNSUPPORTED_URL", "no supported video adapter matches the URL")


def classify_local_reference(reference: str) -> SourceClassification:
    if not isinstance(reference, str) or not reference.startswith("local-media:"):
        raise VideoUnderstandingError(
            "INVALID_LOCAL_REFERENCE",
            "local media must use an opaque local-media reference",
        )
    opaque_id = reference.removeprefix("local-media:")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", opaque_id) is None:
        raise VideoUnderstandingError("INVALID_LOCAL_REFERENCE", "local reference is malformed")
    return SourceClassification(
        source_type="LOCAL_MEDIA",
        adapter="local_file",
        reason_code="SUPPORTED_LOCAL_REFERENCE",
        normalized_private_source=reference,
        source_token=_token(reference),
    )


def _normalize_youtube(parsed) -> str:
    host = (parsed.hostname or "").casefold().rstrip(".")
    video_id: str | None = None
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    else:
        query = parse_qs(parsed.query, keep_blank_values=False)
        candidates = query.get("v", [])
        if candidates:
            video_id = candidates[0]
        elif parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            parts = parsed.path.strip("/").split("/")
            video_id = parts[1] if len(parts) > 1 else None
    if video_id is None or _YOUTUBE_ID_RE.fullmatch(video_id) is None:
        raise VideoUnderstandingError("INVALID_YOUTUBE_ID", "YouTube video identity is invalid")
    return urlunsplit(("https", "www.youtube.com", "/watch", urlencode({"v": video_id}), ""))


def _normalize_vimeo(parsed) -> str:
    parts = [part for part in parsed.path.split("/") if part]
    video_id = next((part for part in reversed(parts) if _VIMEO_ID_RE.fullmatch(part)), None)
    if video_id is None:
        raise VideoUnderstandingError("INVALID_VIMEO_ID", "Vimeo video identity is invalid")
    return urlunsplit(("https", "vimeo.com", f"/{video_id}", "", ""))
