from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import time
import shutil
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from skeleton_media.video_understanding.models import ProcessingMode, VideoUnderstandingError
from skeleton_media.video_understanding.runtime_config import VideoRuntimeConfig
from skeleton_media.video_understanding.subprocess_tools import BoundedCommandRunner, CommandRequest
from skeleton_media.video_understanding.url_classifier import SourceClassification, classify_local_reference, classify_remote_url


_MEDIA_CONTENT_PREFIXES = ("video/", "audio/")
_REDIRECT_CODES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class SourceMetadata:
    source_type: str
    source_identity: str
    adapter: str
    title: str | None
    duration_seconds: float | None
    uploader: str | None
    webpage_url: str | None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcquiredSource:
    classification: SourceClassification
    metadata: SourceMetadata
    media_path: Path | None
    subtitle_paths: tuple[Path, ...]
    source_sha256: str | None
    temporary_media: bool


class YtDlpAdapter:
    def __init__(self, config: VideoRuntimeConfig, runner: BoundedCommandRunner) -> None:
        self.config = config
        self.runner = runner

    def metadata_command(self, source: SourceClassification, workspace: Path) -> CommandRequest:
        if source.adapter not in {"youtube", "vimeo"}:
            raise VideoUnderstandingError("ADAPTER_MISMATCH", "yt-dlp adapter cannot process source")
        return CommandRequest(
            "yt_dlp",
            (
                "--ignore-config",
                "--no-playlist",
                "--no-warnings",
                "--dump-single-json",
                "--skip-download",
                "--",
                source.normalized_private_source,
            ),
            workspace,
            timeout_seconds=min(180, self.config.limits.subprocess_timeout_seconds),
        )

    def android_metadata_command(self, source: SourceClassification, workspace: Path) -> CommandRequest:
        """Fixed public YouTube metadata profile used only after media 403.

        Never accepts client names or extractor arguments from a chat request.
        """
        if source.adapter != "youtube":
            raise VideoUnderstandingError("ADAPTER_MISMATCH", "Android metadata is YouTube-only")
        return CommandRequest(
            "yt_dlp",
            (
                "--ignore-config",
                "--no-playlist",
                "--no-warnings",
                "--extractor-args",
                "youtube:player_client=android",
                "--dump-single-json",
                "--skip-download",
                "--",
                source.normalized_private_source,
            ),
            workspace,
            timeout_seconds=min(180, self.config.limits.subprocess_timeout_seconds),
        )

    def subtitle_command(self, source: SourceClassification, workspace: Path) -> CommandRequest:
        languages = ",".join(self.config.subtitle_languages)
        return CommandRequest(
            "yt_dlp",
            (
                "--ignore-config",
                "--no-playlist",
                "--no-warnings",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-format",
                "vtt",
                "--sub-langs",
                languages,
                "--paths",
                str(workspace),
                "--output",
                "subtitle.%(language)s.%(ext)s",
                "--",
                source.normalized_private_source,
            ),
            workspace,
            timeout_seconds=min(300, self.config.limits.subprocess_timeout_seconds),
        )

    def media_command(self, source: SourceClassification, workspace: Path) -> CommandRequest:
        return CommandRequest(
            "yt_dlp",
            (
                "--ignore-config",
                "--no-playlist",
                "--no-warnings",
                "--format",
                "bestvideo*+bestaudio/best",
                "--merge-output-format",
                "mkv",
                "--max-filesize",
                str(self.config.limits.max_download_bytes),
                "--paths",
                str(workspace),
                "--output",
                "source.%(ext)s",
                "--",
                source.normalized_private_source,
            ),
            workspace,
            timeout_seconds=self.config.limits.subprocess_timeout_seconds,
        )

    def acquire(
        self,
        source: SourceClassification,
        workspace: Path,
        mode: ProcessingMode,
    ) -> AcquiredSource:
        metadata_result = self.runner.require_success(
            self.metadata_command(source, workspace), reason_code="SOURCE_METADATA_FAILED"
        )
        metadata = self._parse_metadata(metadata_result.stdout_text(), source)

        subtitle_result = self.runner.run(self.subtitle_command(source, workspace))
        subtitle_paths = tuple(sorted(workspace.glob("subtitle.*.vtt"))) if subtitle_result.returncode == 0 else ()

        needs_media = mode is not ProcessingMode.QUICK or not subtitle_paths
        media_path: Path | None = None
        digest: str | None = None
        if needs_media:
            try:
                self.runner.require_success(self.media_command(source, workspace), reason_code="SOURCE_MEDIA_FAILED")
            except VideoUnderstandingError as exc:
                if exc.reason_code != "SOURCE_MEDIA_FAILED" or source.adapter != "youtube":
                    raise
                # The normal web-client progressive URL may return HTTP 403
                # after a few megabytes. The fixed public Android profile is
                # only a second metadata source; it supplies no credentials
                # and is never selectable through a public operation.
                alternative_result = self.runner.require_success(
                    self.android_metadata_command(source, workspace),
                    reason_code="SOURCE_ANDROID_METADATA_FAILED",
                )
                try:
                    original = json.loads(metadata_result.stdout_text())
                    alternative = json.loads(alternative_result.stdout_text())
                except (ValueError, TypeError) as parse_error:
                    raise VideoUnderstandingError(
                        "SOURCE_ANDROID_METADATA_INVALID", "source metadata is invalid"
                    ) from parse_error
                if not isinstance(original, dict) or not isinstance(alternative, dict):
                    raise VideoUnderstandingError(
                        "SOURCE_ANDROID_METADATA_INVALID", "source metadata is invalid"
                    )
                original_id, alternative_id = original.get("id"), alternative.get("id")
                if (
                    not isinstance(original_id, str)
                    or not original_id
                    or not isinstance(alternative_id, str)
                    or alternative_id != original_id
                ):
                    raise VideoUnderstandingError(
                        "SOURCE_ANDROID_IDENTITY_MISMATCH", "source identity changed"
                    )
                download_youtube_progressive_ranges(
                    alternative,
                    workspace / "source.mp4",
                    self.config,
                )
            candidates = [path for path in workspace.glob("source.*") if path.is_file()]
            if len(candidates) != 1:
                raise VideoUnderstandingError("SOURCE_MEDIA_AMBIGUOUS", "yt-dlp did not produce exactly one media file")
            media_path = candidates[0]
            _validate_file_size(media_path, self.config.limits.max_download_bytes)
            digest = sha256_file(media_path)
        return AcquiredSource(source, metadata, media_path, subtitle_paths, digest, True)

    def _parse_metadata(self, text: str, source: SourceClassification) -> SourceMetadata:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VideoUnderstandingError("SOURCE_METADATA_INVALID", "yt-dlp metadata is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise VideoUnderstandingError("SOURCE_METADATA_INVALID", "yt-dlp metadata must be an object")
        duration_raw = payload.get("duration")
        duration: float | None = None
        if duration_raw is not None:
            if isinstance(duration_raw, bool) or not isinstance(duration_raw, (int, float)):
                raise VideoUnderstandingError("SOURCE_DURATION_INVALID", "source duration is invalid")
            duration = float(duration_raw)
            if duration <= 0 or duration > self.config.limits.max_duration_seconds:
                raise VideoUnderstandingError("SOURCE_DURATION_OUT_OF_BOUNDS", "source duration exceeded limit")
        return SourceMetadata(
            source_type=source.source_type,
            source_identity=source.normalized_private_source,
            adapter=source.adapter,
            title=_bounded_optional_text(payload.get("title"), 2000),
            duration_seconds=duration,
            uploader=_bounded_optional_text(payload.get("uploader") or payload.get("channel"), 1000),
            webpage_url=_bounded_optional_text(payload.get("webpage_url"), 4096),
            extra={
                "extractor": _bounded_optional_text(payload.get("extractor_key"), 128),
                "live_status": _bounded_optional_text(payload.get("live_status"), 64),
            },
        )


# Conservative, site-specific transport fallback. It is not a second video
# pipeline and cannot be requested through any public Skeleton operation.
_YOUTUBE_RANGE_BYTES = 1024 * 1024
_YOUTUBE_RANGE_MAX_BYTES = 256 * 1024 * 1024
_YOUTUBE_RANGE_MAX_SECONDS = 180
_YOUTUBE_RANGE_HEADER = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")


@dataclass(frozen=True)
class _RangeChunk:
    http_status: int
    content_range: str
    content_type: str
    data: bytes


def download_youtube_progressive_ranges(
    metadata: Mapping[str, Any],
    target: Path,
    config: VideoRuntimeConfig,
    *,
    fetch_chunk: Callable[[str, int, int], _RangeChunk] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Bounded progressive MP4 transfer from yt-dlp's public YouTube metadata.

    HTTPS is pinned to globally routed Googlevideo addresses; credentials,
    arbitrary domains, redirects and configurable headers are prohibited.
    A failed/uncertain partial is removed, never promoted as usable media.
    """
    if metadata.get("availability") != "public" or metadata.get("is_live") is not False:
        raise VideoUnderstandingError("SOURCE_RANGE_NOT_PUBLIC", "range fallback requires a non-live public source")
    if metadata.get("live_status") not in (None, "not_live") or metadata.get("age_limit", 0) not in (0, None):
        raise VideoUnderstandingError("SOURCE_RANGE_NOT_PUBLIC", "range fallback excludes live or restricted media")
    if not isinstance(metadata.get("formats"), list):
        raise VideoUnderstandingError("SOURCE_RANGE_FORMAT_MISSING", "source format list is missing")
    maximum = min(config.limits.max_download_bytes, _YOUTUBE_RANGE_MAX_BYTES)
    candidates: list[tuple[int, int, str]] = []
    for format_info in metadata["formats"]:
        if not isinstance(format_info, Mapping):
            continue
        height = format_info.get("height")
        if (
            format_info.get("protocol") != "https"
            or format_info.get("ext") != "mp4"
            or format_info.get("has_drm") is not False
            or not isinstance(height, int)
            or not 144 <= height <= 480
            or format_info.get("vcodec") in (None, "none")
            or format_info.get("acodec") in (None, "none")
        ):
            continue
        approximate = format_info.get("filesize") or format_info.get("filesize_approx")
        if isinstance(approximate, bool) or not isinstance(approximate, int) or not 1024 <= approximate <= maximum:
            continue
        raw_url = format_info.get("url")
        if not isinstance(raw_url, str):
            continue
        try:
            _validate_youtube_cdn_url(raw_url)
        except VideoUnderstandingError:
            continue
        candidates.append((height, approximate, raw_url))
    if not candidates:
        raise VideoUnderstandingError("SOURCE_RANGE_FORMAT_MISSING", "no bounded progressive public MP4 format")
    # Prefer 360p where available; preserve sound for ASR. Candidate is
    # derived from provider metadata, never from the user's request body.
    candidates.sort(key=lambda row: (abs(row[0] - 360), row[1]))
    _, _, url = candidates[0]
    workspace = target.parent.resolve(strict=True)
    approved = config.temp_root.resolve(strict=False)
    if workspace != approved and approved not in workspace.parents:
        raise VideoUnderstandingError("SOURCE_RANGE_WORKSPACE_DENIED", "range transfer must stay in private temporary root")
    if target.name != "source.mp4" or target.exists() or target.is_symlink():
        raise VideoUnderstandingError("SOURCE_RANGE_TARGET_UNSAFE", "range transfer target already exists or is unsafe")
    temporary = workspace / "source.range-partial"
    if temporary.exists() or temporary.is_symlink():
        raise VideoUnderstandingError("SOURCE_RANGE_TARGET_UNSAFE", "range partial target already exists")
    fetch = fetch_chunk or _read_youtube_googlevideo_range
    started = monotonic()
    deadline = min(float(config.limits.subprocess_timeout_seconds), _YOUTUBE_RANGE_MAX_SECONDS)
    total: int | None = None
    offset = 0
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            while total is None or offset < total:
                if monotonic() - started > deadline:
                    raise VideoUnderstandingError("SOURCE_RANGE_TIMEOUT", "bounded range transfer exceeded its deadline")
                end = offset + _YOUTUBE_RANGE_BYTES - 1
                if total is not None:
                    end = min(end, total - 1)
                chunk = fetch(url, offset, end)
                if chunk.http_status != 206:
                    raise VideoUnderstandingError("SOURCE_RANGE_HTTP_FAILED", "media server did not support strict byte ranges")
                match = _YOUTUBE_RANGE_HEADER.fullmatch(chunk.content_range)
                if match is None:
                    raise VideoUnderstandingError("SOURCE_RANGE_INVALID_RESPONSE", "invalid Content-Range")
                first, last, declared = map(int, match.groups())
                if first != offset or last != end or not offset < declared <= maximum or last >= declared:
                    raise VideoUnderstandingError("SOURCE_RANGE_INVALID_RESPONSE", "range offset or total size mismatch")
                if total is not None and total != declared:
                    raise VideoUnderstandingError("SOURCE_RANGE_INVALID_RESPONSE", "media size changed during transfer")
                if chunk.content_type.split(";", 1)[0].strip().lower() not in {"video/mp4", "application/octet-stream"}:
                    raise VideoUnderstandingError("SOURCE_RANGE_INVALID_RESPONSE", "media type is unsupported")
                if not isinstance(chunk.data, bytes) or len(chunk.data) != end - offset + 1:
                    raise VideoUnderstandingError("SOURCE_RANGE_INVALID_RESPONSE", "incomplete range response")
                total = declared
                offset += len(chunk.data)
                stream.write(chunk.data)
            stream.flush()
            os.fsync(stream.fileno())
        if total is None or temporary.stat().st_size != total:
            raise VideoUnderstandingError("SOURCE_RANGE_INVALID_RESPONSE", "transfer did not complete")
        os.replace(temporary, target)
        return total
    finally:
        temporary.unlink(missing_ok=True)


def _validate_youtube_cdn_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise VideoUnderstandingError("SOURCE_RANGE_URL_UNSAFE", "invalid CDN URL") from exc
    if (
        len(url) > 8192 or parsed.scheme != "https" or not host
        or not (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
        or port not in (None, 443) or parsed.username is not None
        or parsed.password is not None or parsed.fragment
    ):
        raise VideoUnderstandingError("SOURCE_RANGE_URL_UNSAFE", "range requests require Googlevideo HTTPS")


def _read_youtube_googlevideo_range(url: str, start: int, end: int) -> _RangeChunk:
    _validate_youtube_cdn_url(url)
    if start < 0 or end < start or end - start + 1 > _YOUTUBE_RANGE_BYTES:
        raise VideoUnderstandingError("SOURCE_RANGE_INVALID_REQUEST", "range request is outside fixed bounds")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    ips = _resolve_global_addresses(host, 443)
    connection = _PinnedHTTPSConnection(host, ips[0], 443, timeout=18)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        connection.request("GET", path, headers={
            "Range": f"bytes={start}-{end}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "video/mp4,application/octet-stream",
            "Accept-Encoding": "identity",
        })
        if connection.sock is None or not ipaddress.ip_address(connection.sock.getpeername()[0]).is_global:
            raise VideoUnderstandingError("SOURCE_RANGE_PEER_UNSAFE", "non-global CDN peer")
        response = connection.getresponse()
        try:
            if response.status != 206:
                raise VideoUnderstandingError("SOURCE_RANGE_HTTP_FAILED", "range endpoint did not return 206")
            body = response.read(_YOUTUBE_RANGE_BYTES + 1)
            return _RangeChunk(
                http_status=response.status,
                content_range=response.getheader("Content-Range") or "",
                content_type=response.getheader("Content-Type") or "",
                data=body,
            )
        finally:
            response.close()
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise VideoUnderstandingError("SOURCE_RANGE_NETWORK_FAILED", "range request failed") from exc
    finally:
        connection.close()


class LocalFileAdapter:
    def __init__(self, config: VideoRuntimeConfig) -> None:
        self.config = config

    def acquire(self, reference: str, workspace: Path, mode: ProcessingMode) -> AcquiredSource:
        del mode
        classification = classify_local_reference(reference)
        source_path = self.config.resolve_local_reference(reference)
        _validate_file_size(source_path, self.config.limits.max_download_bytes)
        suffix = source_path.suffix.lower() or ".bin"
        target = workspace / f"source{suffix}"
        _copy_immutable(source_path, target)
        digest = sha256_file(target)
        guessed, _ = mimetypes.guess_type(source_path.name)
        metadata = SourceMetadata(
            source_type="LOCAL_MEDIA",
            source_identity=classification.normalized_private_source,
            adapter="local_file",
            title=None,
            duration_seconds=None,
            uploader=None,
            webpage_url=None,
            extra={"media_type": guessed or "application/octet-stream"},
        )
        return AcquiredSource(classification, metadata, target, (), digest, True)


class DirectMediaAdapter:
    def __init__(
        self,
        config: VideoRuntimeConfig,
        *,
        downloader: Callable[[str, Path, VideoRuntimeConfig], tuple[str, int]] | None = None,
    ) -> None:
        self.config = config
        self._downloader = downloader or download_direct_media

    def acquire(self, raw_url: str, workspace: Path, mode: ProcessingMode) -> AcquiredSource:
        del mode
        classification = classify_remote_url(raw_url)
        if classification.adapter != "direct_media":
            raise VideoUnderstandingError("ADAPTER_MISMATCH", "direct media adapter cannot process source")
        host = (urlsplit(classification.normalized_private_source).hostname or "").casefold()
        if host not in self.config.direct_media_allowed_hosts:
            raise VideoUnderstandingError("DIRECT_MEDIA_HOST_NOT_ALLOWED", "direct media host is not allowlisted")
        suffix = Path(urlsplit(classification.normalized_private_source).path).suffix.lower() or ".bin"
        target = workspace / f"source{suffix}"
        media_type, byte_count = self._downloader(classification.normalized_private_source, target, self.config)
        _validate_file_size(target, self.config.limits.max_download_bytes)
        digest = sha256_file(target)
        metadata = SourceMetadata(
            source_type="REMOTE_MEDIA",
            source_identity=classification.normalized_private_source,
            adapter="direct_media",
            title=None,
            duration_seconds=None,
            uploader=None,
            webpage_url=classification.normalized_private_source,
            extra={"media_type": media_type, "byte_count": byte_count},
        )
        return AcquiredSource(classification, metadata, target, (), digest, True)


def download_direct_media(url: str, target: Path, config: VideoRuntimeConfig) -> tuple[str, int]:
    current = url
    for _ in range(config.limits.max_redirects + 1):
        parsed = urlsplit(current)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise VideoUnderstandingError("DIRECT_MEDIA_URL_UNSAFE", "direct media URL must remain safe HTTPS")
        try:
            port = parsed.port or 443
        except ValueError as exc:
            raise VideoUnderstandingError("DIRECT_MEDIA_URL_UNSAFE", "direct media port is invalid") from exc
        if port != 443:
            raise VideoUnderstandingError("DIRECT_MEDIA_URL_UNSAFE", "direct media port is not allowed")
        host = parsed.hostname.casefold().rstrip(".")
        if host not in config.direct_media_allowed_hosts:
            raise VideoUnderstandingError("DIRECT_MEDIA_HOST_NOT_ALLOWED", "redirect host is not allowlisted")
        addresses = _resolve_global_addresses(host, port)
        response = _request_pinned_https(parsed, addresses[0], timeout=60)
        try:
            if response.status in _REDIRECT_CODES:
                location = response.getheader("Location")
                if not location:
                    raise VideoUnderstandingError("DIRECT_MEDIA_REDIRECT_INVALID", "redirect is missing location")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise VideoUnderstandingError("DIRECT_MEDIA_HTTP_FAILED", "direct media request failed")
            media_type = (response.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().casefold()
            suffix = Path(parsed.path).suffix.lower()
            if not media_type.startswith(_MEDIA_CONTENT_PREFIXES) and not (
                media_type == "application/octet-stream" and suffix
            ):
                raise VideoUnderstandingError("DIRECT_MEDIA_TYPE_REJECTED", "direct media content type is rejected")
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise VideoUnderstandingError("DIRECT_MEDIA_SIZE_INVALID", "content length is invalid") from exc
                if declared_size < 0 or declared_size > config.limits.max_download_bytes:
                    raise VideoUnderstandingError("DIRECT_MEDIA_TOO_LARGE", "direct media exceeded size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            total = 0
            try:
                with temporary.open("xb") as handle:
                    while True:
                        chunk = response.read(65_536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > config.limits.max_download_bytes:
                            raise VideoUnderstandingError("DIRECT_MEDIA_TOO_LARGE", "direct media exceeded size limit")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return media_type, total
        finally:
            response.close()
            connection = getattr(response, "_skeleton_connection", None)
            if connection is not None:
                connection.close()
    raise VideoUnderstandingError("DIRECT_MEDIA_REDIRECT_LIMIT", "direct media redirect limit exceeded")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, *, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _request_pinned_https(parsed: Any, ip: str, *, timeout: float) -> http.client.HTTPResponse:
    port = parsed.port or 443
    connection = _PinnedHTTPSConnection(parsed.hostname, ip, port, timeout=timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    connection.request("GET", path, headers={"User-Agent": "Skeleton-Video-Understanding/1", "Accept": "video/*,audio/*,application/octet-stream"})
    response = connection.getresponse()
    peer = ipaddress.ip_address(connection.sock.getpeername()[0]) if connection.sock else None
    if peer is None or not peer.is_global:
        response.close()
        connection.close()
        raise VideoUnderstandingError("DIRECT_MEDIA_PEER_UNSAFE", "direct media peer is not global")
    setattr(response, "_skeleton_connection", connection)
    return response


def _resolve_global_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise VideoUnderstandingError("DIRECT_MEDIA_DNS_FAILED", "direct media DNS resolution failed") from exc
    addresses: list[str] = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise VideoUnderstandingError("DIRECT_MEDIA_DNS_UNSAFE", "direct media DNS returned unsafe address")
        addresses.append(str(address))
    if not addresses:
        raise VideoUnderstandingError("DIRECT_MEDIA_DNS_EMPTY", "direct media DNS returned no addresses")
    return tuple(dict.fromkeys(addresses))


def _copy_immutable(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_file_size(path: Path, maximum: int) -> None:
    size = path.stat().st_size
    if size <= 0:
        raise VideoUnderstandingError("SOURCE_EMPTY", "source media is empty")
    if size > maximum:
        raise VideoUnderstandingError("SOURCE_TOO_LARGE", "source media exceeded size limit")


def _bounded_optional_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:maximum]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
