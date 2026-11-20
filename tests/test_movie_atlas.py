from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import movie_atlas

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    value = 100.0

    def __call__(self) -> float:
        return self.value


def catalog() -> list[dict]:
    return [
        {"history_key": "known-1", "title": "Synthetic One", "year": 2024, "genre": "Drama",
         "poster": "/posters/synthetic.webp", "url": "https://private.invalid/stream", "overview": "private"},
        {"history_key": "known-1", "title": "Duplicate"},
        {"history_key": "hidden", "title": "Hidden", "hidden": True},
        {"history_key": "deleted", "title": "Deleted", "deleted": True},
        {"history_key": "deleted", "title": "Stale version of deleted film"},
    ]


def test_pair_late_join_revision_conflict_and_role_separation() -> None:
    store = movie_atlas.AtlasStore(Clock())
    created = store.create(catalog())
    code, token = created["code"], created["controller_token"]
    changed = store.update(code, token, 1, {"query": "one", "selected_id": "known-1"})
    assert changed["revision"] == 2
    joined = store.join(code)
    assert joined["state"]["selected_id"] == "known-1"
    assert joined["catalog"] == [{"id": "known-1", "title": "Synthetic One", "year": "2024",
                                  "genres": ["Drama"], "poster": "/posters/synthetic.webp"}]
    assert "private" not in json.dumps(joined)
    assert "controller_token" not in joined
    display = joined["display_token"]
    assert store.update(code, token, 2, {"genre": "Drama", "year_min": 2020, "year_max": 2024})["revision"] == 3
    assert store.update(code, token, 3, {"camera": {"yaw": -33, "pitch": 12, "zoom": 1.3}})["revision"] == 4
    assert store.update(code, token, 4, {"selected_id": None})["revision"] == 5
    assert store.read(code, display, "display")["state"]["selected_id"] is None
    with pytest.raises(movie_atlas.AtlasError) as conflict:
        store.update(code, token, 4, {"sort": "title"})
    assert (conflict.value.status, conflict.value.code) == (409, "revision_conflict")
    for action in (
        lambda: store.update(code, display, 5, {"sort": "title"}),
        lambda: store.read(code, display, "controller"),
        lambda: store.close(code, display),
    ):
        with pytest.raises(movie_atlas.AtlasError) as forbidden:
            action()
        assert forbidden.value.status == 403


@pytest.mark.parametrize("poster", [
    "/posters/../leak.jpg", "/posters/sub/a.jpg", "/posters/%2e%2e.jpg",
    "/posters/a.svg", "/posters/a.jpg?secret=1", "/posters/a.jpg#hidden",
    "/posters/\\evil.jpg", "//elsewhere.invalid/posters/a.jpg",
])
def test_unsafe_poster_paths_are_dropped(poster) -> None:
    assert movie_atlas.public_catalog([{"id": "item", "title": "Example", "poster": poster}]) == [
        {"id": "item", "title": "Example"},
    ]


@pytest.mark.parametrize("revision, change", [
    (True, {"query": "one"}), (1.0, {"query": "one"}), ("1", {"query": "one"}),
    (2**100, {"query": "one"}), (1, {"sort": []}), (1, {"query": {}}),
    (1, {"query": "a" * 81}), (1, {"genre": "Unknown"}), (1, {"year_min": True}),
    (1, {"year_min": 2024, "year_max": 2020}), (1, {"selected_id": []}),
    (1, {"selected_id": "unknown"}), (1, {"selected_id": "hidden"}), (1, {"selected_id": "deleted"}),
    (1, {"camera": {"yaw": True, "pitch": 0, "zoom": 1}}),
    (1, {"camera": {"yaw": 0, "pitch": float("nan"), "zoom": 1}}),
    (1, {"camera": {"yaw": 0, "pitch": 0, "zoom": float("inf")}}),
    (1, {"camera": {"yaw": 0, "pitch": 0, "zoom": 4}}),
    (1, {"camera": {"zoom": 1}}), (1, {"poster": "/posters/a.jpg"}),
])
def test_invalid_updates_leave_revision_unchanged(revision, change) -> None:
    store = movie_atlas.AtlasStore(Clock())
    created = store.create(catalog())
    with pytest.raises(movie_atlas.AtlasError) as invalid:
        store.update(created["code"], created["controller_token"], revision, change)
    assert invalid.value.status == 400
    assert store.read(created["code"], created["controller_token"], "controller")["revision"] == 1


def test_expiry_empty_catalog_disconnection_and_rate_limits() -> None:
    clock = Clock()
    store = movie_atlas.AtlasStore(clock)
    created = store.create([])
    code, token = created["code"], created["controller_token"]
    assert created["catalog"] == []
    display = store.join(code)["display_token"]
    for revision in range(1, 21):
        store.update(code, token, revision, {"query": str(revision)})
    with pytest.raises(movie_atlas.AtlasError) as limited:
        store.update(code, token, 21, {"query": "next"})
    assert limited.value.status == 429
    clock.value += movie_atlas.DISCONNECTED_SECONDS + 1
    assert not store.read(code, token, "controller")["display_connected"]
    assert store.read(code, display, "display")["display_connected"]
    assert store.update(code, token, 21, {"query": "next"})["revision"] == 22
    clock.value += movie_atlas.SESSION_SECONDS
    with pytest.raises(movie_atlas.AtlasError) as expired:
        store.read(code, token, "controller")
    assert (expired.value.status, expired.value.code) == (410, "expired")


def test_executable_js_state_and_transport_contract() -> None:
    subprocess.run(["node", "--test", "tests/movie_atlas/state.test.js"], cwd=ROOT, check=True)

def test_join_attempts_are_bounded_before_the_ownership_probe() -> None:
    clock = Clock()
    store = movie_atlas.AtlasStore(clock)
    created = store.create([])
    bogus_code = "AAAAAA" if created["code"] != "AAAAAA" else "BBBBBB"
    calls = []
    for _ in range(24):
        with pytest.raises(movie_atlas.AtlasError) as invalid:
            store.join(bogus_code, require_clear=lambda: calls.append(True))
        assert invalid.value.status == 410
    with pytest.raises(movie_atlas.AtlasError) as limited:
        store.join(created["code"], require_clear=lambda: calls.append(True))
    assert limited.value.status == 429
    assert calls == []
    clock.value += 10
    assert store.join(created["code"], require_clear=lambda: calls.append(True))["display_connected"]
    assert calls == [True]
