from __future__ import annotations

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAST_RUNTIME = ROOT / "ops" / "skeleton_cast" / "runtime"
sys.path.insert(0, str(CAST_RUNTIME))

import media_search  # noqa: E402


class FakeResponse:
    def __init__(self, *, url: str, payload=None, text: str = "") -> None:
        self.url = url
        self.payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        return self.response


class StaticProvider:
    def __init__(self, provider_id: str, candidates: tuple[media_search.SourceCandidate, ...], timeout_seconds: float = 0.1) -> None:
        self.provider_id = provider_id
        self.candidates = candidates
        self.timeout_seconds = timeout_seconds
        self.queries: list[str] = []

    def search(self, request: media_search.MediaSearchRequest):
        self.queries.append(request.identity.release_query())
        return self.candidates


class SlowProvider:
    provider_id = "provider-a"
    timeout_seconds = 0.01

    def search(self, request: media_search.MediaSearchRequest):
        del request
        time.sleep(0.2)
        return ()


def _release(
    provider_id: str = "provider-b",
    source_id: str = "release-1",
    *,
    source_url: str = "https://media.example/release.m3u8",
    quality: str = "1080p",
    translation: str = "Українська",
    audio_tracks: tuple[str, ...] = ("uk",),
    subtitles: tuple[str, ...] = ("uk",),
    order: int = 0,
) -> media_search.SourceCandidate:
    return media_search.SourceCandidate(
        provider_id=provider_id,
        source_id=source_id,
        kind=media_search.CandidateKind.RELEASE,
        title="Known Show",
        source_url=source_url,
        quality=quality,
        translation=translation,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
        season=2,
        episode=5,
        height=1080,
        order=order,
    )


def test_provider_timeout_does_not_abort_aggregate_release_search() -> None:
    request = media_search.MediaSearchRequest(
        media_search.MediaIdentity("Known Show", year=2024, season=2, episode=5)
    )
    provider_b = StaticProvider("provider-b", (_release(),))

    result = media_search.search_media_sources(request, (SlowProvider(), provider_b))

    assert result.status == "ready"
    assert [candidate.source_id for candidate in result.candidates] == ["release-1"]
    assert {outcome.provider_id: outcome.status for outcome in result.provider_outcomes} == {
        "provider-a": "timeout",
        "provider-b": "ok",
    }


def test_all_provider_timeouts_return_bounded_ukrainian_state_without_raw_exception() -> None:
    request = media_search.MediaSearchRequest(media_search.MediaIdentity("Known Show", year=2024))

    result = media_search.search_media_sources(request, (SlowProvider(),))

    assert result.status == "error"
    assert result.message == "Джерела не відповіли"
    assert "timeout" not in result.message.lower()
    assert result.candidates == ()


def test_trailer_only_candidate_is_not_playable_release() -> None:
    trailer = media_search.SourceCandidate(
        provider_id="trailers",
        source_id="trailer-1",
        kind=media_search.CandidateKind.TRAILER,
        title="Known Show trailer",
        source_url="https://video.example/trailer",
        quality="1080p",
    )
    request = media_search.MediaSearchRequest(media_search.MediaIdentity("Known Show", 2024))

    result = media_search.search_media_sources(request, (StaticProvider("trailers", (trailer,)),))

    assert result.status == "empty"
    assert result.message == "Реліз не знайдено"
    assert result.playable_candidates == ()


def test_release_tracking_replaces_trailer_availability_when_release_appears() -> None:
    identity = media_search.MediaIdentity("Known Show", 2024)
    store = media_search.ReleaseTrackingStore()
    trailer = media_search.SourceCandidate(
        provider_id="trailers",
        source_id="trailer-1",
        kind=media_search.CandidateKind.TRAILER,
        title="Known Show trailer",
        source_url="https://video.example/trailer",
    )

    store.update(identity, (trailer,))
    assert store.availability(identity) == media_search.CandidateKind.TRAILER

    store.update(identity, (_release(),))
    assert store.availability(identity) == media_search.CandidateKind.RELEASE


def test_episodic_release_query_includes_original_title_year_and_se_identity() -> None:
    provider = StaticProvider("provider-b", (_release(),))
    request = media_search.MediaSearchRequest(
        media_search.MediaIdentity("Original Known Show", year=2024, season=2, episode=5)
    )

    media_search.search_media_sources(request, (provider,))

    assert provider.queries == ["Original Known Show 2024 S02E05"]


def test_dedupe_and_metadata_facets_are_from_actual_release_candidates() -> None:
    duplicate_worse = _release("provider-c", "dupe-worse", order=5)
    duplicate_better = _release("provider-b", "dupe-better", order=0)
    other = _release(
        "provider-d",
        "release-720",
        source_url="https://media.example/release-720.m3u8",
        quality="720p",
        translation="Оригінал",
        audio_tracks=("en",),
        subtitles=("en", "uk"),
        order=2,
    )
    request = media_search.MediaSearchRequest(media_search.MediaIdentity("Known Show", 2024, 2, 5))

    result = media_search.search_media_sources(
        request,
        (
            StaticProvider("provider-b", (duplicate_worse,)),
            StaticProvider("provider-c", (duplicate_better, other)),
        ),
    )

    assert [candidate.source_id for candidate in result.candidates] == ["dupe-better", "release-720"]
    assert result.facets.seasons == (2,)
    assert result.facets.episodes == (5,)
    assert result.facets.qualities == ("1080p", "720p")
    assert result.facets.translations == ("Оригінал", "Українська")
    assert result.facets.audio_tracks == ("en", "uk")
    assert result.facets.subtitles == ("en", "uk")


def test_json_and_html_release_backends_can_contribute_through_canonical_abstraction() -> None:
    json_provider = media_search.JsonReleaseSearchProvider(
        "json-index",
        "https://search.example/json?q={query}",
        timeout_seconds=0.2,
        session=FakeSession(
            FakeResponse(
                url="https://search.example/json",
                payload={
                    "candidates": [
                        {
                            "kind": "release",
                            "title": "Known Show",
                            "url": "https://cdn.example/show.m3u8?b=2&a=1",
                            "quality": "1080p",
                            "translation": "Українська",
                            "season": 2,
                            "episode": 5,
                        }
                    ]
                },
            )
        ),
    )
    html_provider = media_search.HtmlReleaseSearchProvider(
        "html-index",
        "https://search.example/html?q={query}",
        timeout_seconds=0.2,
        session=FakeSession(
            FakeResponse(
                url="https://search.example/html",
                text="""
                    <a data-kind="trailer" data-source-url="/trailer.mp4" data-quality="1080p">Trailer</a>
                    <a data-kind="release" data-source-url="/show-720.m3u8" data-quality="720p"
                       data-translation="Оригінал" data-audio="en|uk" data-subtitles="uk,en"
                       data-season="2" data-episode="5">Known Show</a>
                """,
            )
        ),
    )
    request = media_search.MediaSearchRequest(media_search.MediaIdentity("Known Show", 2024, 2, 5))

    result = media_search.search_media_sources(request, (json_provider, html_provider))

    assert result.status == "ready"
    assert {candidate.provider_id for candidate in result.candidates} == {"json-index", "html-index"}
    assert result.facets.qualities == ("1080p", "720p")
    assert result.facets.translations == ("Оригінал", "Українська")
    assert result.facets.audio_tracks == ("en", "uk")
    assert result.facets.subtitles == ("en", "uk")
    assert all(candidate.kind == media_search.CandidateKind.RELEASE for candidate in result.candidates)


def test_environment_provider_config_supports_independent_json_and_html_backends() -> None:
    providers = media_search.providers_from_environment(
        resolve_page=lambda url: {"title": url, "sources": []},
        environment={
            "SKELETON_CAST_RELEASE_SEARCH_BACKENDS": """
            [
              {"id": "json-index", "type": "json", "endpoint_template": "https://search.example/json?q={query}"},
              {"id": "html-index", "type": "html", "endpoint_template": "https://search.example/html?q={query}"}
            ]
            """
        },
    )

    assert [provider.provider_id for provider in providers] == [
        "shared-page-resolver",
        "json-index",
        "html-index",
    ]
    assert isinstance(providers[1], media_search.JsonReleaseSearchProvider)
    assert isinstance(providers[2], media_search.HtmlReleaseSearchProvider)


def test_dedupe_normalizes_equivalent_release_urls_deterministically() -> None:
    request = media_search.MediaSearchRequest(media_search.MediaIdentity("Known Show", 2024, 2, 5))

    result = media_search.search_media_sources(
        request,
        (
            StaticProvider("provider-a", (_release("provider-a", "slower", source_url="https://CDN.example/show.m3u8?b=2&a=1", order=4),)),
            StaticProvider("provider-b", (_release("provider-b", "preferred", source_url="https://cdn.example/show.m3u8?a=1&b=2", order=0),)),
        ),
    )

    assert [candidate.source_id for candidate in result.candidates] == ["preferred"]
