from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests


DEFAULT_PROVIDER_TIMEOUT_SECONDS = 8.0
DEFAULT_CIRCUIT_BREAKER_FAILURES = 2
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 90.0
NO_RESPONSES_UK = "Джерела не відповіли"
NO_RELEASE_UK = "Реліз не знайдено"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


class CandidateKind(str, Enum):
    RELEASE = "release"
    TRAILER = "trailer"


@dataclass(frozen=True)
class MediaIdentity:
    original_title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    def release_query(self) -> str:
        parts = [" ".join(self.original_title.split())]
        if self.year:
            parts.append(str(self.year))
        if self.season is not None and self.episode is not None:
            parts.append(f"S{self.season:02d}E{self.episode:02d}")
        elif self.season is not None:
            parts.append(f"S{self.season:02d}")
        return " ".join(part for part in parts if part).strip()


@dataclass(frozen=True)
class MediaSearchRequest:
    identity: MediaIdentity
    include_trailers: bool = False
    release_tracking: bool = False


@dataclass(frozen=True)
class SourceCandidate:
    provider_id: str
    source_id: str
    kind: CandidateKind
    title: str
    quality: str | None = None
    translation: str | None = None
    audio_tracks: tuple[str, ...] = ()
    subtitles: tuple[str, ...] = ()
    season: int | None = None
    episode: int | None = None
    page_url: str | None = None
    source_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    height: int = 0
    bitrate: float = 0.0
    order: int = 0

    @property
    def playable(self) -> bool:
        return self.kind == CandidateKind.RELEASE and bool(self.source_url or self.page_url)


@dataclass(frozen=True)
class ProviderOutcome:
    provider_id: str
    status: str
    candidate_count: int = 0


@dataclass(frozen=True)
class SearchFacets:
    seasons: tuple[int, ...]
    episodes: tuple[int, ...]
    qualities: tuple[str, ...]
    translations: tuple[str, ...]
    audio_tracks: tuple[str, ...]
    subtitles: tuple[str, ...]


@dataclass(frozen=True)
class MediaSearchResult:
    status: str
    message: str | None
    query: str
    candidates: tuple[SourceCandidate, ...]
    facets: SearchFacets
    provider_outcomes: tuple[ProviderOutcome, ...]

    @property
    def playable_candidates(self) -> tuple[SourceCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.playable)


class MediaSearchProvider(Protocol):
    provider_id: str
    timeout_seconds: float

    def search(self, request: MediaSearchRequest) -> Iterable[SourceCandidate]:
        ...


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_CIRCUIT_BREAKER_FAILURES,
        cooldown_seconds: float = DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def allow(self, provider_id: str) -> bool:
        with self._lock:
            count, opened_at = self._failures.get(provider_id, (0, 0.0))
            if count < self.failure_threshold:
                return True
            if self.clock() - opened_at >= self.cooldown_seconds:
                self._failures.pop(provider_id, None)
                return True
            return False

    def record_success(self, provider_id: str) -> None:
        with self._lock:
            self._failures.pop(provider_id, None)

    def record_failure(self, provider_id: str) -> None:
        with self._lock:
            count, _ = self._failures.get(provider_id, (0, 0.0))
            self._failures[provider_id] = (count + 1, self.clock())


class ReleaseTrackingStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._availability: dict[str, CandidateKind] = {}

    def update(self, identity: MediaIdentity, candidates: Iterable[SourceCandidate]) -> None:
        key = _identity_key(identity)
        kinds = {candidate.kind for candidate in candidates}
        if not kinds:
            return
        with self._lock:
            if CandidateKind.RELEASE in kinds:
                self._availability[key] = CandidateKind.RELEASE
            elif self._availability.get(key) != CandidateKind.RELEASE:
                self._availability[key] = CandidateKind.TRAILER

    def availability(self, identity: MediaIdentity) -> CandidateKind | None:
        with self._lock:
            return self._availability.get(_identity_key(identity))


class JsonReleaseSearchProvider:
    """Configurable JSON adapter; endpoints are runtime config, not code constants."""

    def __init__(
        self,
        provider_id: str,
        endpoint_template: str,
        *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.provider_id = _clean_provider_id(provider_id)
        self.endpoint_template = endpoint_template
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def search(self, request: MediaSearchRequest) -> Iterable[SourceCandidate]:
        query = request.identity.release_query()
        url = self.endpoint_template.format(query=urlencode({"q": query})[2:])
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        return tuple(_candidate_from_mapping(self.provider_id, item) for item in _candidate_items(payload))


class HtmlReleaseSearchProvider:
    """Generic HTML adapter for configured release indexes.

    The adapter only extracts explicitly typed release metadata from a configured
    search result page. It does not infer playable/demo URLs from titles.
    """

    def __init__(
        self,
        provider_id: str,
        endpoint_template: str,
        *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.provider_id = _clean_provider_id(provider_id)
        self.endpoint_template = endpoint_template
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def search(self, request: MediaSearchRequest) -> Iterable[SourceCandidate]:
        query = request.identity.release_query()
        url = self.endpoint_template.format(query=urlencode({"q": query})[2:])
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return tuple(
            _candidate_from_mapping(self.provider_id, item)
            for item in _candidate_items_from_html(response.text, base_url=response.url)
        )


class PageUrlReleaseProvider:
    def __init__(
        self,
        resolve_page: Callable[[str], dict[str, Any]],
        *,
        provider_id: str = "shared-page-resolver",
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self.provider_id = provider_id
        self.timeout_seconds = timeout_seconds
        self.resolve_page = resolve_page

    def search(self, request: MediaSearchRequest) -> Iterable[SourceCandidate]:
        match = URL_RE.search(request.identity.original_title)
        if match is None:
            return ()
        resolved = self.resolve_page(match.group(0).rstrip(".,);]}>"))
        title = str(resolved.get("title") or request.identity.original_title)
        candidates = []
        for source in resolved.get("sources") or []:
            if isinstance(source, dict):
                candidates.append(
                    _candidate_from_mapping(
                        self.provider_id,
                        {
                            **source,
                            "kind": "release",
                            "title": str(source.get("title") or title),
                            "page_url": match.group(0),
                            "source_url": source.get("url"),
                        },
                    )
                )
        return tuple(candidates)


def providers_from_environment(
    *,
    resolve_page: Callable[[str], dict[str, Any]],
    environment: dict[str, str] | None = None,
) -> tuple[MediaSearchProvider, ...]:
    env = os.environ if environment is None else environment
    providers: list[MediaSearchProvider] = [PageUrlReleaseProvider(resolve_page)]
    raw = str(env.get("SKELETON_CAST_RELEASE_SEARCH_BACKENDS") or "").strip()
    if raw:
        for item in _configured_backend_items(raw):
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            endpoint_template = str(item.get("endpoint_template") or "").strip()
            if provider_id and endpoint_template:
                timeout_seconds = _positive_float(item.get("timeout_seconds"), DEFAULT_PROVIDER_TIMEOUT_SECONDS)
                backend_type = str(item.get("type") or item.get("kind") or "json").strip().lower()
                if backend_type == "html":
                    providers.append(
                        HtmlReleaseSearchProvider(
                            provider_id,
                            endpoint_template,
                            timeout_seconds=timeout_seconds,
                        )
                    )
                else:
                    providers.append(
                        JsonReleaseSearchProvider(
                            provider_id,
                            endpoint_template,
                            timeout_seconds=timeout_seconds,
                        )
                    )
    return tuple(providers)


def search_media_sources(
    request: MediaSearchRequest,
    providers: Iterable[MediaSearchProvider],
    *,
    circuit_breaker: CircuitBreaker | None = None,
    tracking_store: ReleaseTrackingStore | None = None,
) -> MediaSearchResult:
    breaker = circuit_breaker or CircuitBreaker()
    active = [provider for provider in providers if breaker.allow(provider.provider_id)]
    query = request.identity.release_query()
    if not active:
        return _result("error", NO_RESPONSES_UK, query, (), (ProviderOutcome("all", "circuit_open"),))

    collected: list[SourceCandidate] = []
    outcomes: list[ProviderOutcome] = []
    executor = ThreadPoolExecutor(max_workers=max(1, len(active)))
    try:
        futures = {executor.submit(provider.search, request): provider for provider in active}
        for future, provider in futures.items():
            try:
                items = tuple(future.result(timeout=max(0.1, provider.timeout_seconds)))
            except FutureTimeout:
                future.cancel()
                breaker.record_failure(provider.provider_id)
                outcomes.append(ProviderOutcome(provider.provider_id, "timeout"))
                continue
            except Exception:
                breaker.record_failure(provider.provider_id)
                outcomes.append(ProviderOutcome(provider.provider_id, "failed"))
                continue
            breaker.record_success(provider.provider_id)
            visible = [item for item in items if request.include_trailers or item.kind == CandidateKind.RELEASE]
            collected.extend(visible)
            outcomes.append(ProviderOutcome(provider.provider_id, "ok", len(visible)))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    normalized = normalize_candidates(collected)
    if tracking_store is not None and request.release_tracking:
        tracking_store.update(request.identity, normalized)
    playable = tuple(candidate for candidate in normalized if candidate.playable)
    if playable:
        return _result("ready", None, query, playable, tuple(outcomes))
    if outcomes and all(outcome.status in {"timeout", "failed", "circuit_open"} for outcome in outcomes):
        return _result("error", NO_RESPONSES_UK, query, (), tuple(outcomes))
    return _result("empty", NO_RELEASE_UK, query, (), tuple(outcomes))


def normalize_candidates(candidates: Iterable[SourceCandidate]) -> tuple[SourceCandidate, ...]:
    deduped: dict[tuple[str, str, str, str, int | None, int | None], SourceCandidate] = {}
    for candidate in candidates:
        if candidate.kind != CandidateKind.RELEASE:
            continue
        if not candidate.playable:
            continue
        key = (
            _norm_url(candidate.source_url or candidate.page_url or ""),
            _norm_text(candidate.quality),
            _norm_text(candidate.translation),
            _norm_text(candidate.title),
            candidate.season,
            candidate.episode,
        )
        previous = deduped.get(key)
        if previous is None or _candidate_rank(candidate) < _candidate_rank(previous):
            deduped[key] = candidate
    return tuple(sorted(deduped.values(), key=_candidate_rank))


def facets_for(candidates: Iterable[SourceCandidate]) -> SearchFacets:
    actual = tuple(candidate for candidate in candidates if candidate.kind == CandidateKind.RELEASE)
    return SearchFacets(
        seasons=tuple(sorted({candidate.season for candidate in actual if candidate.season is not None})),
        episodes=tuple(sorted({candidate.episode for candidate in actual if candidate.episode is not None})),
        qualities=_sorted_text({candidate.quality for candidate in actual if candidate.quality}),
        translations=_sorted_text({candidate.translation for candidate in actual if candidate.translation}),
        audio_tracks=_sorted_text(track for candidate in actual for track in candidate.audio_tracks if track),
        subtitles=_sorted_text(track for candidate in actual for track in candidate.subtitles if track),
    )


def public_result_payload(result: MediaSearchResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "message": result.message,
        "query": result.query,
        "facets": {
            "seasons": list(result.facets.seasons),
            "episodes": list(result.facets.episodes),
            "qualities": list(result.facets.qualities),
            "translations": list(result.facets.translations),
            "audio_tracks": list(result.facets.audio_tracks),
            "subtitles": list(result.facets.subtitles),
        },
        "sources": [_public_candidate(candidate) for candidate in result.candidates],
        "providers": [
            {"provider_id": outcome.provider_id, "status": outcome.status, "candidate_count": outcome.candidate_count}
            for outcome in result.provider_outcomes
        ],
    }


def source_to_job_source(candidate: SourceCandidate) -> dict[str, Any]:
    return {
        "source_id": candidate.source_id,
        "url": candidate.source_url or candidate.page_url,
        "kind": "search-release",
        "group": candidate.translation or "Реліз",
        "translation": candidate.translation or "Реліз",
        "episode": _episode_label(candidate),
        "quality": candidate.quality or "Авто",
        "height": candidate.height,
        "width": None,
        "tbr": candidate.bitrate,
        "duration": None,
        "title": candidate.title,
        "headers": candidate.headers,
        "has_drm": False,
        "order": candidate.order,
        "subtitles": list(candidate.subtitles),
        "audio_tracks": list(candidate.audio_tracks),
        "page_title": candidate.title,
    }


def _result(
    status: str,
    message: str | None,
    query: str,
    candidates: tuple[SourceCandidate, ...],
    outcomes: tuple[ProviderOutcome, ...],
) -> MediaSearchResult:
    return MediaSearchResult(status, message, query, candidates, facets_for(candidates), outcomes)


def _candidate_from_mapping(provider_id: str, item: dict[str, Any]) -> SourceCandidate:
    kind = _candidate_kind(item.get("kind"))
    source_url = _optional_str(item.get("source_url") or item.get("url"))
    page_url = _optional_str(item.get("page_url"))
    title = _optional_str(item.get("title")) or "Реліз"
    seed = "|".join([provider_id, kind.value, source_url or "", page_url or "", title])
    return SourceCandidate(
        provider_id=provider_id,
        source_id=_optional_str(item.get("source_id")) or hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
        kind=kind,
        title=title,
        quality=_optional_str(item.get("quality")),
        translation=_optional_str(item.get("translation") or item.get("voice") or item.get("group")),
        audio_tracks=tuple(str(value).strip() for value in item.get("audio_tracks") or () if str(value).strip()),
        subtitles=tuple(str(value).strip() for value in item.get("subtitles") or () if str(value).strip()),
        season=_optional_int(item.get("season")),
        episode=_optional_int(item.get("episode")),
        page_url=page_url,
        source_url=source_url,
        headers={str(k): str(v) for k, v in dict(item.get("headers") or {}).items() if str(k) and str(v)},
        height=_optional_int(item.get("height")) or 0,
        bitrate=float(item.get("tbr") or item.get("bitrate") or 0.0),
        order=_optional_int(item.get("order")) or 0,
    )


def _candidate_items(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("candidates") or payload.get("sources") or []
    else:
        items = payload
    return tuple(item for item in items if isinstance(item, dict))


def _candidate_items_from_html(text: str, *, base_url: str) -> Iterable[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    link_re = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>", re.I | re.S)
    for match in link_re.finditer(text or ""):
        attrs = _html_attrs(match.group("attrs"))
        href = _optional_str(attrs.get("data-source-url") or attrs.get("data-url") or attrs.get("href"))
        kind = _candidate_kind(attrs.get("data-kind") or attrs.get("data-type"))
        if kind != CandidateKind.RELEASE:
            continue
        if href is None:
            continue
        title = _strip_tags(attrs.get("data-title") or match.group("label")) or "Реліз"
        source_url = urljoin(base_url, href)
        items.append(
            {
                "kind": kind.value,
                "title": title,
                "source_url": source_url,
                "page_url": _optional_str(attrs.get("data-page-url")),
                "quality": attrs.get("data-quality"),
                "translation": attrs.get("data-translation") or attrs.get("data-voice"),
                "season": attrs.get("data-season"),
                "episode": attrs.get("data-episode"),
                "audio_tracks": _split_meta(attrs.get("data-audio") or attrs.get("data-audio-tracks")),
                "subtitles": _split_meta(attrs.get("data-subtitles")),
                "height": attrs.get("data-height"),
                "bitrate": attrs.get("data-bitrate") or attrs.get("data-tbr"),
                "order": attrs.get("data-order"),
            }
        )
    return tuple(items)


def _public_candidate(candidate: SourceCandidate) -> dict[str, Any]:
    return {
        "source_id": candidate.source_id,
        "kind": candidate.kind.value,
        "provider_id": candidate.provider_id,
        "title": candidate.title,
        "quality": candidate.quality,
        "translation": candidate.translation,
        "audio_tracks": list(candidate.audio_tracks),
        "subtitles": list(candidate.subtitles),
        "season": candidate.season,
        "episode": candidate.episode,
        "playable": candidate.playable,
    }


def _candidate_rank(candidate: SourceCandidate) -> tuple[int, int, float, str, str]:
    return (
        candidate.order,
        -candidate.height,
        -candidate.bitrate,
        candidate.provider_id,
        candidate.source_id,
    )


def _identity_key(identity: MediaIdentity) -> str:
    return "|".join(
        [
            _norm_text(identity.original_title),
            str(identity.year or ""),
            str(identity.season or ""),
            str(identity.episode or ""),
        ]
    )


def _clean_provider_id(provider_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", provider_id).strip("-")
    if not clean:
        raise ValueError("provider_id_required")
    return clean


def _norm_text(value: str | None) -> str:
    return " ".join(str(value or "").casefold().split())


def _norm_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return value.strip()
    query = urlencode(sorted(parse_qs(parsed.query, keep_blank_values=True).items()), doseq=True)
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), query=query, fragment="").geturl()


def _sorted_text(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({" ".join(str(value).split()) for value in values if str(value).strip()}, key=lambda item: item.casefold()))


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _configured_backend_items(raw: str) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, list):
        return ()
    return tuple(item for item in payload if isinstance(item, dict))


def _candidate_kind(value: Any) -> CandidateKind:
    try:
        return CandidateKind(str(value or "release").strip().lower())
    except ValueError:
        return CandidateKind.TRAILER


def _html_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"([:\w-]+)\s*=\s*(['\"])(.*?)\2", raw or "", re.S):
        attrs[match.group(1).lower()] = html_unescape(match.group(3))
    return attrs


def _strip_tags(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", html_unescape(str(value or ""))).strip()


def _split_meta(value: Any) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[,|]", str(value or "")) if part.strip())


def html_unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def _episode_label(candidate: SourceCandidate) -> str:
    if candidate.season is not None and candidate.episode is not None:
        return f"S{candidate.season:02d}E{candidate.episode:02d}"
    return "Відео"
