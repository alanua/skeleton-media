from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from core.video_understanding.models import VideoUnderstandingError


_EXECUTABLE_KEYS = frozenset({"yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr"})
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALLOWED_TRANSPORTS = frozenset({"loopback", "private_bridge"})


@dataclass(frozen=True)
class RuntimeLimits:
    max_duration_seconds: int = 21_600
    max_download_bytes: int = 8 * 1024 * 1024 * 1024
    max_transcript_chars: int = 2_000_000
    max_frames: int = 80
    max_ocr_chars_per_frame: int = 20_000
    max_redirects: int = 5
    subprocess_timeout_seconds: int = 900
    subprocess_output_bytes: int = 4 * 1024 * 1024
    lease_seconds: int = 900
    max_attempts: int = 4

    def __post_init__(self) -> None:
        integer_bounds = {
            "max_duration_seconds": (1, 86_400),
            "max_download_bytes": (1_048_576, 64 * 1024 * 1024 * 1024),
            "max_transcript_chars": (1_000, 20_000_000),
            "max_frames": (1, 500),
            "max_ocr_chars_per_frame": (100, 200_000),
            "max_redirects": (0, 10),
            "subprocess_timeout_seconds": (1, 7_200),
            "subprocess_output_bytes": (1_024, 64 * 1024 * 1024),
            "lease_seconds": (30, 86_400),
            "max_attempts": (1, 20),
        }
        for name, (minimum, maximum) in integer_bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise VideoUnderstandingError("INVALID_RUNTIME_LIMIT", f"{name} is outside its bound")


@dataclass(frozen=True)
class VideoRuntimeConfig:
    artifact_root: Path
    queue_root: Path
    temp_root: Path
    approved_local_roots: tuple[Path, ...]
    local_media_registry: Mapping[str, Path]
    direct_media_allowed_hosts: tuple[str, ...]
    executables: Mapping[str, str]
    ollama_transport: str
    ollama_model: str
    ollama_endpoint: str | None = None
    sona_endpoint: str = "http://127.0.0.1:8080"
    sona_model: str = "default"
    sona_start_args: tuple[str, ...] = ()
    subtitle_languages: tuple[str, ...] = ("uk", "de", "en", "ru")
    ocr_languages: tuple[str, ...] = ("ukr", "deu", "eng", "rus")
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)

    def __post_init__(self) -> None:
        for name in ("artifact_root", "queue_root", "temp_root"):
            value = Path(getattr(self, name)).expanduser()
            if not value.is_absolute():
                raise VideoUnderstandingError("RUNTIME_PATH_NOT_ABSOLUTE", f"{name} must be absolute")
            object.__setattr__(self, name, value.resolve(strict=False))
        roots = tuple(Path(root).expanduser().resolve(strict=False) for root in self.approved_local_roots)
        if not roots:
            raise VideoUnderstandingError("LOCAL_ROOT_REQUIRED", "at least one approved local root is required")
        object.__setattr__(self, "approved_local_roots", roots)

        registry: dict[str, Path] = {}
        for opaque_id, raw_path in self.local_media_registry.items():
            if _LOCAL_ID_RE.fullmatch(str(opaque_id)) is None:
                raise VideoUnderstandingError("INVALID_LOCAL_MEDIA_ID", "local media identity is invalid")
            resolved = Path(raw_path).expanduser().resolve(strict=False)
            if not any(resolved == root or root in resolved.parents for root in roots):
                raise VideoUnderstandingError("LOCAL_MEDIA_OUTSIDE_ROOT", "local media path is outside approved roots")
            registry[str(opaque_id)] = resolved
        object.__setattr__(self, "local_media_registry", registry)

        hosts: list[str] = []
        for host in self.direct_media_allowed_hosts:
            normalized = str(host).casefold().rstrip(".")
            if _HOST_RE.fullmatch(normalized) is None or normalized in {"localhost", "metadata"}:
                raise VideoUnderstandingError("INVALID_DIRECT_MEDIA_HOST", "direct media host is invalid")
            hosts.append(normalized)
        object.__setattr__(self, "direct_media_allowed_hosts", tuple(sorted(set(hosts))))

        if set(self.executables) != _EXECUTABLE_KEYS:
            raise VideoUnderstandingError("EXECUTABLE_SET_MISMATCH", "runtime executable keys are not exact")
        executable_map: dict[str, str] = {}
        for key, value in self.executables.items():
            if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
                raise VideoUnderstandingError("INVALID_EXECUTABLE", f"{key} executable is invalid")
            executable_path = Path(value).expanduser()
            if not executable_path.is_absolute():
                raise VideoUnderstandingError("INVALID_EXECUTABLE", f"{key} executable must be absolute")
            executable_map[key] = str(executable_path)
        object.__setattr__(self, "executables", executable_map)

        if self.ollama_transport not in _ALLOWED_TRANSPORTS:
            raise VideoUnderstandingError("INVALID_OLLAMA_TRANSPORT", "Ollama transport is unsupported")
        if not self.ollama_model.strip() or len(self.ollama_model) > 256:
            raise VideoUnderstandingError("INVALID_OLLAMA_MODEL", "Ollama model is invalid")
        if self.ollama_transport == "loopback":
            if self.ollama_endpoint is None:
                raise VideoUnderstandingError("OLLAMA_ENDPOINT_REQUIRED", "loopback Ollama endpoint is required")
            _validate_loopback_http(self.ollama_endpoint, "OLLAMA_ENDPOINT_INVALID")
        elif self.ollama_endpoint is not None:
            raise VideoUnderstandingError(
                "OLLAMA_ENDPOINT_FORBIDDEN",
                "private bridge transport must not carry an endpoint",
            )
        _validate_loopback_http(self.sona_endpoint, "SONA_ENDPOINT_INVALID")
        if not self.sona_model.strip() or len(self.sona_model) > 256:
            raise VideoUnderstandingError("INVALID_SONA_MODEL", "Sona model is invalid")
        if not isinstance(self.sona_start_args, tuple) or len(self.sona_start_args) > 32:
            raise VideoUnderstandingError("INVALID_SONA_START_ARGS", "Sona start args are invalid")
        normalized_sona_args: list[str] = []
        for value in self.sona_start_args:
            if not isinstance(value, str) or not value or "\x00" in value or len(value) > 1024:
                raise VideoUnderstandingError("INVALID_SONA_START_ARGS", "Sona start arg is invalid")
            normalized_sona_args.append(value)
        object.__setattr__(self, "sona_start_args", tuple(normalized_sona_args))
        object.__setattr__(self, "subtitle_languages", _validate_languages(self.subtitle_languages, "subtitle"))
        object.__setattr__(self, "ocr_languages", _validate_languages(self.ocr_languages, "ocr"))

    def resolve_local_reference(self, reference: str) -> Path:
        if not isinstance(reference, str) or not reference.startswith("local-media:"):
            raise VideoUnderstandingError("INVALID_LOCAL_REFERENCE", "local reference is invalid")
        opaque_id = reference.removeprefix("local-media:")
        path = self.local_media_registry.get(opaque_id)
        if path is None:
            raise VideoUnderstandingError("LOCAL_MEDIA_NOT_REGISTERED", "local media identity is not registered")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise VideoUnderstandingError("LOCAL_MEDIA_UNAVAILABLE", "local media is unavailable")
        if not any(resolved == root or root in resolved.parents for root in self.approved_local_roots):
            raise VideoUnderstandingError("LOCAL_MEDIA_OUTSIDE_ROOT", "local media escaped approved roots")
        return resolved

    def public_summary(self) -> dict[str, object]:
        return {
            "schema": "skeleton.video_understanding.runtime_summary.v1",
            "ollama_transport": self.ollama_transport,
            "direct_media_host_count": len(self.direct_media_allowed_hosts),
            "local_media_count": len(self.local_media_registry),
            "subtitle_language_count": len(self.subtitle_languages),
            "ocr_language_count": len(self.ocr_languages),
            "max_frames": self.limits.max_frames,
            "max_attempts": self.limits.max_attempts,
        }


def _validate_loopback_http(value: str, reason: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VideoUnderstandingError(reason, "endpoint must be fixed loopback HTTP")


def _validate_languages(values: tuple[str, ...], kind: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not 1 <= len(values) <= 16:
        raise VideoUnderstandingError("INVALID_LANGUAGE_SET", f"{kind} languages are invalid")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{2,16}", value) is None:
            raise VideoUnderstandingError("INVALID_LANGUAGE", f"{kind} language is invalid")
        normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def config_from_mapping(payload: Mapping[str, Any]) -> VideoRuntimeConfig:
    allowed = {
        "artifact_root",
        "queue_root",
        "temp_root",
        "approved_local_roots",
        "local_media_registry",
        "direct_media_allowed_hosts",
        "executables",
        "ollama_transport",
        "ollama_model",
        "ollama_endpoint",
        "sona_endpoint",
        "sona_model",
        "sona_start_args",
        "subtitle_languages",
        "ocr_languages",
        "limits",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise VideoUnderstandingError("UNKNOWN_RUNTIME_CONFIG_FIELD", "runtime config contains unknown fields")
    limits_payload = payload.get("limits", {})
    if not isinstance(limits_payload, Mapping):
        raise VideoUnderstandingError("INVALID_RUNTIME_LIMITS", "limits must be an object")
    try:
        limits = RuntimeLimits(**dict(limits_payload))
        return VideoRuntimeConfig(
            artifact_root=Path(str(payload["artifact_root"])),
            queue_root=Path(str(payload["queue_root"])),
            temp_root=Path(str(payload["temp_root"])),
            approved_local_roots=tuple(Path(str(value)) for value in payload["approved_local_roots"]),
            local_media_registry={
                str(key): Path(str(value)) for key, value in dict(payload["local_media_registry"]).items()
            },
            direct_media_allowed_hosts=tuple(str(value) for value in payload.get("direct_media_allowed_hosts", ())),
            executables={str(key): str(value) for key, value in dict(payload["executables"]).items()},
            ollama_transport=str(payload["ollama_transport"]),
            ollama_model=str(payload["ollama_model"]),
            ollama_endpoint=(str(payload["ollama_endpoint"]) if payload.get("ollama_endpoint") is not None else None),
            sona_endpoint=str(payload.get("sona_endpoint", "http://127.0.0.1:8080")),
            sona_model=str(payload.get("sona_model", "default")),
            sona_start_args=tuple(str(value) for value in payload.get("sona_start_args", ())),
            subtitle_languages=tuple(str(value) for value in payload.get("subtitle_languages", ("uk", "de", "en", "ru"))),
            ocr_languages=tuple(str(value) for value in payload.get("ocr_languages", ("ukr", "deu", "eng", "rus"))),
            limits=limits,
        )
    except KeyError as exc:
        raise VideoUnderstandingError("RUNTIME_CONFIG_FIELD_REQUIRED", "runtime config is incomplete") from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, VideoUnderstandingError):
            raise
        raise VideoUnderstandingError("INVALID_RUNTIME_CONFIG", "runtime config is invalid") from exc


def load_runtime_config(path: Path) -> VideoRuntimeConfig:
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise VideoUnderstandingError("RUNTIME_CONFIG_UNAVAILABLE", "runtime config is unavailable")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoUnderstandingError("RUNTIME_CONFIG_INVALID_JSON", "runtime config is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise VideoUnderstandingError("INVALID_RUNTIME_CONFIG", "runtime config must be an object")
    return config_from_mapping(payload)
