from __future__ import annotations

import http.client
import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import VideoRuntimeConfig
from core.video_understanding.subprocess_tools import BoundedCommandRunner, CommandRequest
from core.video_understanding.transcript import TranscriptSegment, normalize_segments


@dataclass(frozen=True)
class SonaResult:
    segments: tuple[TranscriptSegment, ...]
    language: str
    provider: str = "sona"


class SonaBackend:
    def __init__(
        self,
        config: VideoRuntimeConfig,
        *,
        requester: Callable[[str, str, Path, int, int], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self._requester = requester or _request_sona

    def extract_audio(
        self,
        runner: BoundedCommandRunner,
        media_path: Path,
        workspace: Path,
    ) -> Path:
        target = workspace / "audio.wav"
        request = CommandRequest(
            "ffmpeg",
            (
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(target),
            ),
            workspace,
            timeout_seconds=self.config.limits.subprocess_timeout_seconds,
        )
        runner.require_success(request, reason_code="AUDIO_EXTRACTION_FAILED")
        if not target.is_file() or target.stat().st_size <= 44:
            raise VideoUnderstandingError("AUDIO_EXTRACTION_EMPTY", "audio extraction produced no audio")
        if target.stat().st_size > self.config.limits.max_download_bytes:
            raise VideoUnderstandingError("AUDIO_TOO_LARGE", "audio exceeded configured size")
        return target

    def transcribe(self, audio_path: Path) -> SonaResult:
        payload = self._requester(
            self.config.sona_endpoint,
            self.config.sona_model,
            audio_path,
            self.config.limits.subprocess_timeout_seconds,
            self.config.limits.subprocess_output_bytes,
        )
        language = str(payload.get("language") or "und")[:32]
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list):
            raise VideoUnderstandingError("SONA_RESPONSE_INVALID", "Sona response has no segments")
        segments: list[TranscriptSegment] = []
        for item in raw_segments:
            if not isinstance(item, Mapping):
                raise VideoUnderstandingError("SONA_RESPONSE_INVALID", "Sona segment is invalid")
            segments.append(
                TranscriptSegment(
                    item.get("start"),
                    item.get("end"),
                    str(item.get("text", "")),
                    language,
                    "sona",
                    item.get("confidence"),
                    "local_asr",
                )
            )
        return SonaResult(normalize_segments(segments), language)


class SonaProcessManager:
    """Owns only a process it started; existing loopback Sona is never terminated."""

    def __init__(
        self,
        config: VideoRuntimeConfig,
        *,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        readiness_probe: Callable[[str], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._process_factory = process_factory
        self._readiness_probe = readiness_probe or _probe_sona
        self._sleep = sleep
        self._owned: subprocess.Popen[bytes] | None = None

    def ensure_ready(self, *, start_owned: bool, wait_seconds: float = 30.0) -> bool:
        if self._readiness_probe(self.config.sona_endpoint):
            return False
        if not start_owned:
            raise VideoUnderstandingError("SONA_UNAVAILABLE", "Sona is not ready")
        parsed = urlsplit(self.config.sona_endpoint)
        if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise VideoUnderstandingError("SONA_ENDPOINT_INVALID", "Sona endpoint is not loopback")
        executable = self.config.executables["sona"]
        if not self.config.sona_start_args:
            raise VideoUnderstandingError(
                "SONA_START_CONFIGURATION_REQUIRED",
                "owned Sona startup requires fixed private runtime args",
            )
        try:
            self._owned = self._process_factory(
                [executable, *self.config.sona_start_args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "NO_COLOR": "1",
                },
            )
        except OSError as exc:
            raise VideoUnderstandingError("SONA_START_FAILED", "owned Sona process could not start") from exc
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self._owned.poll() is not None:
                self.stop()
                raise VideoUnderstandingError("SONA_START_FAILED", "owned Sona process exited early")
            if self._readiness_probe(self.config.sona_endpoint):
                return True
            self._sleep(0.2)
        self.stop()
        raise VideoUnderstandingError("SONA_READY_TIMEOUT", "owned Sona process did not become ready")

    def stop(self) -> None:
        process = self._owned
        self._owned = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @property
    def owns_process(self) -> bool:
        return self._owned is not None


def _request_sona(
    endpoint: str,
    model: str,
    audio_path: Path,
    timeout_seconds: int,
    max_output_bytes: int,
) -> Mapping[str, Any]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise VideoUnderstandingError("SONA_ENDPOINT_INVALID", "Sona endpoint must be loopback HTTP")
    boundary = "skeleton-" + secrets.token_hex(12)
    filename = "audio.wav"
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode("utf-8")
    closing = f"\r\n--{boundary}--\r\n".encode("ascii")
    size = audio_path.stat().st_size
    content_length = len(preamble) + size + len(closing)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout_seconds)
    path = (parsed.path.rstrip("/") if parsed.path else "") + "/v1/audio/transcriptions"
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        connection.send(preamble)
        with audio_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65_536), b""):
                connection.send(chunk)
        connection.send(closing)
        response = connection.getresponse()
        raw = response.read(max_output_bytes + 1)
        if response.status != 200:
            raise VideoUnderstandingError("SONA_REQUEST_FAILED", "Sona transcription request failed")
        if len(raw) > max_output_bytes:
            raise VideoUnderstandingError("SONA_OUTPUT_TOO_LARGE", "Sona response exceeded limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VideoUnderstandingError("SONA_RESPONSE_INVALID", "Sona response is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise VideoUnderstandingError("SONA_RESPONSE_INVALID", "Sona response must be an object")
        return payload
    finally:
        connection.close()


def _probe_sona(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    try:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=1)
        path = (parsed.path.rstrip("/") if parsed.path else "") + "/v1/models"
        connection.request("GET", path)
        response = connection.getresponse()
        response.read(1024)
        return response.status == 200
    except OSError:
        return False
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
