from __future__ import annotations

import json
import threading
import types
from pathlib import Path

from test_skeleton_cast_media_target import cast_app


def _item(key: str, *, position: float = 1.0, endpoint: str = "tv") -> dict:
    return {
        "schema": "skeleton.media.watch_item.v1",
        "history_key": key,
        "job_id": f"{key[:1] or 'a'}" * 16,
        "source_id": f"src-{key}",
        "title": f"Synthetic {endpoint} {key}",
        "position_seconds": position,
        "last_reason": f"{endpoint}_progress",
    }


def _store(*, tv_items: list[dict] | None = None, samsung_items: list[dict] | None = None) -> dict:
    return {
        "schema": cast_app.WATCH_HISTORY_ENDPOINT_SCHEMA,
        "version": 1,
        "updated_at": 1,
        "histories": {
            cast_app.HOME_EDGE_TV_HISTORY_ID: {"items": list(tv_items or [])},
            cast_app.SAMSUNG_DEVICE_ID: {"items": list(samsung_items or [])},
        },
    }


class CountingEndpointHistoryBackend:
    WATCH_HISTORY_ENDPOINTS: Path
    ENDPOINT_HISTORY_LOCK = threading.RLock()

    def __init__(self, path: Path) -> None:
        self.WATCH_HISTORY_ENDPOINTS = path
        self.load_calls = 0
        self.write_calls = 0

    def write_store(self, store: dict) -> None:
        self.WATCH_HISTORY_ENDPOINTS.parent.mkdir(parents=True, exist_ok=True)
        self.WATCH_HISTORY_ENDPOINTS.write_text(json.dumps(store, indent=2), encoding="utf-8")

    def _load_endpoint_watch_history(self) -> dict:
        self.load_calls += 1
        return json.loads(self.WATCH_HISTORY_ENDPOINTS.read_text(encoding="utf-8"))

    def _write_endpoint_watch_history(self, store: dict) -> None:
        self.write_calls += 1
        self.write_store(store)


def _install_counting_backend(monkeypatch, tmp_path: Path, store: dict) -> CountingEndpointHistoryBackend:
    backend = CountingEndpointHistoryBackend(tmp_path / "state" / "watch-history-endpoints.json")
    backend.write_store(store)
    monkeypatch.setattr(cast_app, "player", backend)
    monkeypatch.setattr(cast_app, "WATCH_HISTORY_ENDPOINTS", backend.WATCH_HISTORY_ENDPOINTS)
    cast_app._endpoint_history_cache_clear()
    return backend


def _install_route_stubs(monkeypatch, *, args: dict | None = None) -> None:
    monkeypatch.setattr(cast_app, "jsonify", lambda *items, **kwargs: items[0] if items else kwargs)
    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(args=args or {}, get_json=lambda silent=True: {}))


def test_media_history_route_caches_warm_store_but_keeps_auth_and_endpoint_boundaries(monkeypatch, tmp_path: Path) -> None:
    backend = _install_counting_backend(
        monkeypatch,
        tmp_path,
        _store(tv_items=[_item("tv-a", position=11.0)], samsung_items=[_item("sam-a", position=22.0, endpoint="samsung")]),
    )
    require_calls = 0

    def require() -> None:
        nonlocal require_calls
        require_calls += 1

    monkeypatch.setattr(cast_app, "_require", require)
    _install_route_stubs(monkeypatch, args={"endpoint": "tv"})

    first_tv = cast_app.media_history_get()
    second_tv = cast_app.media_history_get()
    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(args={"endpoint": "samsung"}, get_json=lambda silent=True: {}))
    samsung = cast_app.media_history_get()

    assert backend.load_calls == 1
    assert require_calls == 3
    assert [item["history_key"] for item in first_tv["items"]] == ["tv-a"]
    assert [item["history_key"] for item in second_tv["items"]] == ["tv-a"]
    assert [item["history_key"] for item in samsung["items"]] == ["sam-a"]


def test_media_history_cache_invalidates_after_save_update_delete_and_external_tombstone(monkeypatch, tmp_path: Path) -> None:
    backend = _install_counting_backend(monkeypatch, tmp_path, _store())
    monkeypatch.setattr(cast_app, "_require", lambda: None)
    _install_route_stubs(monkeypatch, args={"endpoint": "tv"})

    assert cast_app.media_history_get()["items"] == []
    assert cast_app.media_history_get()["items"] == []
    assert backend.load_calls == 1

    cast_app._record_history_progress(
        "tv",
        {"job_id": "a" * 16, "source_id": "src-tv", "position_seconds": 10.0},
        reason="save",
    )
    saved = cast_app.media_history_get()["items"]
    assert backend.load_calls == 2
    assert len(saved) == 1
    assert saved[0]["position_seconds"] == 10.0
    history_key = saved[0]["history_key"]

    cast_app._record_history_progress(
        "tv",
        {"job_id": "a" * 16, "source_id": "src-tv", "position_seconds": 88.0},
        reason="update",
    )
    updated = cast_app.media_history_get()["items"]
    assert backend.load_calls == 3
    assert len(updated) == 1
    assert updated[0]["history_key"] == history_key
    assert updated[0]["position_seconds"] == 88.0

    deleted = cast_app.media_history_item_delete(history_key)
    assert deleted["deleted"] == 1
    assert cast_app.media_history_get()["items"] == []
    assert backend.load_calls == 4

    backend.write_store(_store(tv_items=[_item("external-a", position=33.0)]))
    external = cast_app.media_history_get()["items"]
    assert backend.load_calls == 5
    assert [item["history_key"] for item in external] == ["external-a"]

    backend.write_store(_store())
    assert cast_app.media_history_get()["items"] == []
    assert backend.load_calls == 6


def test_authenticated_poster_route_still_requires_access_on_every_request(monkeypatch, tmp_path: Path) -> None:
    posters = tmp_path / "posters"
    posters.mkdir()
    (posters / "synthetic.jpg").write_bytes(b"synthetic-poster")
    monkeypatch.setattr(cast_app, "POSTERS", posters)
    require_calls = 0

    def require() -> None:
        nonlocal require_calls
        require_calls += 1

    monkeypatch.setattr(cast_app, "_require", require)
    monkeypatch.setattr(cast_app, "send_from_directory", lambda directory, filename: types.SimpleNamespace(data=(Path(directory) / filename).read_bytes(), headers={}))

    first = cast_app.poster_file("synthetic.jpg")
    second = cast_app.poster_file("synthetic.jpg")

    assert first.data == b"synthetic-poster"
    assert second.data == b"synthetic-poster"
    assert require_calls == 2
