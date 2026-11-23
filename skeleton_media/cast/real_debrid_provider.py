"""Real-Debrid read-only, transport-injected provider contract.

This module has no HTTP client, persistent storage or playback side effects.
The caller must supply a trusted transport and an in-memory token supplier.
Device authorization is only a model until a separately reviewed Home Secrets
session broker and Bitwarden credential-write boundary are available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit
import re
import time


_ORIGIN = "https://api.real-debrid.com"
_READ_PATHS = frozenset({
    "/rest/1.0/user",
    "/rest/1.0/torrents",
    "/rest/1.0/downloads",
    "/oauth/v2/device/code",
    "/oauth/v2/device/credentials",
})
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ProviderError(RuntimeError):
    """Stable redacted error. Never contains provider payloads or credentials."""

    def __init__(self, reason_code: str, *, retry_after: int | None = None) -> None:
        self.reason_code = reason_code
        self.retry_after = retry_after
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    status_code: int
    payload: object
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> ProviderResponse: ...


@dataclass(frozen=True, slots=True, repr=False)
class DeviceFlow:
    """Device code stays in the trusted backend; public_view is operator-only."""

    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: float

    def __repr__(self) -> str:
        return "DeviceFlow(<redacted>)"

    def public_view(self, *, now: float) -> dict[str, object]:
        return {
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "interval": self.interval,
            "expires_in": max(0, int(self.expires_at - now)),
        }


@dataclass(frozen=True, slots=True, repr=False)
class DeviceCredentials:
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return "DeviceCredentials(<redacted>)"


@dataclass(frozen=True, slots=True)
class PollResult:
    status: str
    credentials: DeviceCredentials | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class InventoryItem:
    provider_id: str
    kind: str
    filename: str
    size_bytes: int | None
    status: str
    verified_playable: bool = False


@dataclass(frozen=True, slots=True)
class AccountStatus:
    plan: str
    premium_expires_at: int | None


class RealDebridProvider:
    """No default network transport; no API write endpoints are implemented.

    A production transport must additionally disable redirects, validate TLS,
    disallow proxies/host overrides and redact all request/response logging.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        client_id: str,
        access_token: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 8.0,
    ) -> None:
        if not _ID_PATTERN.fullmatch(client_id):
            raise ValueError("INVALID_CLIENT_ID")
        if not (1 <= timeout <= 20):
            raise ValueError("INVALID_TIMEOUT")
        self._transport = transport
        self._client_id = client_id
        self._access_token = access_token
        self._clock = clock
        self._timeout = float(timeout)

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        authenticated: bool = False,
        pending_is_403: bool = False,
    ) -> object:
        if path not in _READ_PATHS:
            raise ProviderError("ENDPOINT_FORBIDDEN")
        headers: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            try:
                token = self._access_token() if self._access_token else None
            except Exception:
                raise ProviderError("CREDENTIAL_UNAVAILABLE") from None
            if not isinstance(token, str) or not token.strip():
                raise ProviderError("CREDENTIAL_UNAVAILABLE")
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._transport.request(
                "GET",
                _ORIGIN + path,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
        except Exception:
            raise ProviderError("TRANSPORT_UNAVAILABLE") from None
        if not isinstance(response, ProviderResponse):
            raise ProviderError("INVALID_RESPONSE")
        status = response.status_code
        if pending_is_403 and status == 403:
            raise ProviderError("AUTH_PENDING")
        if status == 401:
            raise ProviderError("AUTH_REQUIRED")
        if status == 403:
            raise ProviderError("PERMISSION_DENIED")
        if status == 429:
            raw = response.headers.get("Retry-After", "")
            try:
                retry_after = min(120, max(1, int(raw)))
            except (TypeError, ValueError):
                retry_after = 30
            raise ProviderError("RATE_LIMITED", retry_after=retry_after)
        if isinstance(status, int) and status >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE")
        if status != 200:
            raise ProviderError("PROVIDER_REJECTED")
        content_type = response.headers.get("Content-Type", "application/json")
        if not content_type.lower().split(";", 1)[0].strip().endswith("/json"):
            raise ProviderError("UNEXPECTED_CONTENT_TYPE")
        if not isinstance(response.payload, (Mapping, list)):
            raise ProviderError("INVALID_RESPONSE")
        return response.payload

    def begin_device_flow(self) -> DeviceFlow:
        payload = self._get(
            "/oauth/v2/device/code",
            params={"client_id": self._client_id, "new_credentials": "yes"},
        )
        if not isinstance(payload, Mapping):
            raise ProviderError("INVALID_RESPONSE")
        code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_url = payload.get("verification_url")
        try:
            interval = int(payload["interval"])
            expires_in = int(payload["expires_in"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError("INVALID_RESPONSE") from None
        if not all(isinstance(x, str) and 3 <= len(x) <= 256 for x in (code, user_code)):
            raise ProviderError("INVALID_RESPONSE")
        if not isinstance(verification_url, str):
            raise ProviderError("INVALID_RESPONSE")
        parts = urlsplit(verification_url)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"real-debrid.com", "www.real-debrid.com"}
            or parts.port not in {None, 443}
            or parts.path != "/device"
            or parts.username is not None
            or parts.password is not None
        ):
            raise ProviderError("INVALID_VERIFICATION_URL")
        if not (2 <= interval <= 120 and 60 <= expires_in <= 3600):
            raise ProviderError("INVALID_RESPONSE")
        return DeviceFlow(
            device_code=code,
            user_code=user_code,
            verification_url=verification_url,
            interval=interval,
            expires_at=self._clock() + expires_in,
        )

    def poll_device_flow(self, flow: DeviceFlow) -> PollResult:
        if not isinstance(flow, DeviceFlow):
            raise ProviderError("INVALID_FLOW")
        if self._clock() >= flow.expires_at:
            return PollResult("EXPIRED")
        try:
            payload = self._get(
                "/oauth/v2/device/credentials",
                params={"client_id": self._client_id, "code": flow.device_code},
                pending_is_403=True,
            )
        except ProviderError as exc:
            if exc.reason_code == "AUTH_PENDING":
                return PollResult("PENDING")
            raise
        if not isinstance(payload, Mapping):
            raise ProviderError("INVALID_RESPONSE")
        client_id, secret = payload.get("client_id"), payload.get("client_secret")
        if not isinstance(client_id, str) or not _ID_PATTERN.fullmatch(client_id):
            raise ProviderError("INVALID_RESPONSE")
        if not isinstance(secret, str) or len(secret) < 8:
            raise ProviderError("INVALID_RESPONSE")
        return PollResult("AUTHORIZED", DeviceCredentials(client_id, secret))

    def account_status(self) -> AccountStatus:
        data = self._get("/rest/1.0/user", authenticated=True)
        if not isinstance(data, Mapping):
            raise ProviderError("INVALID_RESPONSE")
        kind = str(data.get("type") or "unknown").lower()
        plan = kind if kind in {"premium", "free"} else "unknown"
        timestamp = data.get("premium")
        expires = int(timestamp) if isinstance(timestamp, (int, float)) and timestamp > 0 else None
        return AccountStatus(plan, expires)

    @staticmethod
    def _items(payload: object, kind: str) -> tuple[InventoryItem, ...]:
        if not isinstance(payload, list):
            raise ProviderError("INVALID_RESPONSE")
        result: list[InventoryItem] = []
        seen: set[str] = set()
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            identifier = entry.get("id")
            if not isinstance(identifier, str) or not _ID_PATTERN.fullmatch(identifier):
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            name = entry.get("filename")
            name = name[:512] if isinstance(name, str) else ""
            raw_size = entry.get("bytes") if kind == "torrent" else entry.get("filesize")
            size = raw_size if type(raw_size) is int and raw_size >= 0 else None
            state = str(entry.get("status") or "unknown")[:48] if kind == "torrent" else "listed"
            # Never propagate "download", "link", "links", user identity or signed URLs.
            result.append(InventoryItem(identifier, kind, name, size, state))
        return tuple(result)

    def list_torrents(self, *, page: int = 1, limit: int = 100) -> tuple[InventoryItem, ...]:
        self._validate_page(page, limit)
        response = self._get(
            "/rest/1.0/torrents",
            authenticated=True,
            params={"page": str(page), "limit": str(limit)},
        )
        return self._items(response, "torrent")

    def list_downloads(self, *, page: int = 1, limit: int = 100) -> tuple[InventoryItem, ...]:
        self._validate_page(page, limit)
        response = self._get(
            "/rest/1.0/downloads",
            authenticated=True,
            params={"page": str(page), "limit": str(limit)},
        )
        return self._items(response, "download")

    @staticmethod
    def _validate_page(page: int, limit: int) -> None:
        if type(page) is not int or type(limit) is not int or not (1 <= page <= 100_000 and 1 <= limit <= 5000):
            raise ProviderError("INVALID_PAGINATION")
