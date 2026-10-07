from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from test_skeleton_cast_handoff_memory import ProjectorEndpointAdapter
from test_skeleton_cast_media_target import FakePlayer, _install_runtime, cast_app

_player_resolver_stub = types.ModuleType("skeleton_media.cast.resolver")
_player_resolver_stub._cache_poster = lambda *args, **kwargs: None
_player_resolver_stub._cache_youtube_landscape_poster = lambda *args, **kwargs: None
_player_resolver_stub._cache_youtube_poster = lambda *args, **kwargs: None
_player_trakt_stub = types.ModuleType("skeleton_media.cast.trakt_sync")
_player_trakt_stub.enqueue_progress = lambda *args, **kwargs: None
sys.modules.setdefault("skeleton_media.cast.resolver", _player_resolver_stub)
sys.modules.setdefault("skeleton_media.cast.trakt_sync", _player_trakt_stub)
from skeleton_media.cast import player as player_impl


def _install_history(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cast_app, "STATE", state)
    monkeypatch.setattr(cast_app, "WATCH_HISTORY_LEGACY", state / "watch-history.json")
    monkeypatch.setattr(cast_app, "WATCH_HISTORY_ENDPOINTS", state / "watch-history-endpoints.json")


def _legacy_item(index: int, *, samsung: bool = False) -> dict:
    return {
        "job_id": f"job-{index:03d}",
        "source_id": f"src-{index:03d}",
        "title": f"Synthetic Title {index:03d}",
        "position_seconds": float(index),
        "last_reason": "samsung_periodic" if samsung else "tv_periodic",
    }


def _write_legacy(path: Path, *, total: int = 123, samsung_count: int = 17) -> list[dict]:
    items = [_legacy_item(index, samsung=index < samsung_count) for index in range(total)]
    path.write_text(
        json.dumps({"schema": cast_app.WATCH_HISTORY_LEGACY_SCHEMA, "items": items}, indent=2),
        encoding="utf-8",
    )
    return items


def _history_key(endpoint: str) -> str:
    items = cast_app._history_public_items(endpoint)["items"]
    assert len(items) == 1
    return items[0]["history_key"]


def test_legacy_migration_copies_all_to_tv_and_explicit_samsung_subset_idempotently(monkeypatch, tmp_path: Path) -> None:
    _install_history(monkeypatch, tmp_path)
    legacy = _write_legacy(cast_app.WATCH_HISTORY_LEGACY, total=123, samsung_count=17)
    before = cast_app.WATCH_HISTORY_LEGACY.read_text(encoding="utf-8")

    first_tv = cast_app._history_public_items("tv")
    first_samsung = cast_app._history_public_items("samsung")
    second_tv = cast_app._history_public_items("home_edge_tv")
    second_samsung = cast_app._history_public_items("samsung_kiosk")

    assert len(first_tv["items"]) == 123
    assert len(second_tv["items"]) == 123
    assert len(first_samsung["items"]) == 17
    assert len(second_samsung["items"]) == 17
    assert first_tv["items"][0]["title"] == legacy[0]["title"]
    assert cast_app.WATCH_HISTORY_LEGACY.read_text(encoding="utf-8") == before
    assert first_tv["migration"]["schema"] == cast_app.WATCH_HISTORY_LEGACY_SCHEMA
    assert first_tv["migration"]["source_counts"]["legacy_total"] == 123
    assert first_tv["migration"]["source_counts"]["home_edge_tv"] == 123
    assert first_tv["migration"]["source_counts"]["samsung_kiosk"] == 17


def test_tv_and_samsung_progress_are_independent_for_same_content(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    _install_history(monkeypatch, tmp_path)

    cast_app._record_tv_history_from_status(
        {"running": True, "job_id": "a" * 16, "source_id": "src-1080", "time-pos": 15.0, "playing": True},
        reason="tv_progress",
    )
    cast_app._record_history_progress(
        "samsung",
        {"job_id": "a" * 16, "source_id": "src-1080", "position_seconds": 240.0, "playing": True},
        reason="samsung_periodic",
    )

    tv = cast_app._history_public_items("tv")["items"]
    samsung = cast_app._history_public_items("samsung")["items"]
    assert len(tv) == 1
    assert len(samsung) == 1
    assert tv[0]["position_seconds"] == 15.0
    assert tv[0]["last_reason"] == "tv_progress"
    assert samsung[0]["position_seconds"] == 240.0
    assert samsung[0]["last_reason"] == "samsung_periodic"


def test_selector_capture_and_successful_handoff_do_not_mutate_source_history(monkeypatch, tmp_path: Path) -> None:
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS.clear()
    fake = FakePlayer(paused=False, position=33.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    _install_history(monkeypatch, tmp_path)
    projector = ProjectorEndpointAdapter(tmp_path / "projector-status.json")
    projector.history_id = "projector_room"
    projector.capabilities = (*projector.capabilities, "history")
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS["projector"] = projector

    cast_app._record_tv_history_from_status(
        {"running": True, "job_id": "a" * 16, "source_id": "src-1080", "time-pos": 11.0, "playing": True},
        reason="tv_progress",
    )
    before = cast_app._history_public_items("tv")["items"][0].copy()

    cast_app._select_media_target("samsung")
    cast_app._charge_media_capture("tv")
    result = cast_app._handoff_capture_to_endpoint("projector")

    after = cast_app._history_public_items("tv")["items"][0]
    assert result["target"] == "projector"
    assert after == before
    assert cast_app._history_public_items("samsung")["items"] == []
    assert cast_app._history_public_items("projector_room")["items"] == []


def test_synthetic_third_endpoint_uses_adapter_history_namespace_without_core_branch(monkeypatch, tmp_path: Path) -> None:
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS.clear()
    _install_history(monkeypatch, tmp_path)
    projector = ProjectorEndpointAdapter(tmp_path / "projector-status.json")
    projector.history_id = "projector_room"
    projector.capabilities = (*projector.capabilities, "history")
    cast_app._EXTRA_MEDIA_ENDPOINT_ADAPTERS["projector"] = projector

    cast_app._record_history_progress(
        "projector",
        {"job_id": "p" * 16, "source_id": "src-projector", "position_seconds": 88.0},
        reason="projector_periodic",
    )

    assert cast_app._history_public_items("projector")["history_id"] == "projector_room"
    assert cast_app._history_public_items("projector_room")["items"][0]["position_seconds"] == 88.0
    assert cast_app._history_public_items("tv")["items"] == []
    assert cast_app._history_public_items("samsung")["items"] == []


def test_endpoint_scoped_list_open_delete_and_resume_default_to_home_edge_tv(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    _install_history(monkeypatch, tmp_path)
    cast_app._record_history_progress(
        "tv",
        {"job_id": "a" * 16, "source_id": "src-1080", "position_seconds": 42.0},
        reason="tv_progress",
    )
    cast_app._record_history_progress(
        "samsung",
        {"job_id": "a" * 16, "source_id": "src-1080", "position_seconds": 142.0},
        reason="samsung_periodic",
    )
    tv_key = _history_key("tv")
    samsung_key = _history_key("samsung")
    monkeypatch.setattr(cast_app, "jsonify", lambda *args, **kwargs: args[0] if args else kwargs)
    monkeypatch.setattr(cast_app, "_require", lambda: None)

    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(args={}, get_json=lambda silent=True: {"history_key": tv_key}))
    resumed = cast_app.media_history_resume_post()
    opened = cast_app.media_history_item_get(tv_key)

    assert resumed["history_id"] == "home_edge_tv"
    assert opened["item"]["position_seconds"] == 42.0
    assert fake.calls == [("play", "src-1080"), ("seek", 42.0), ("control", "play")]

    monkeypatch.setattr(cast_app, "request", types.SimpleNamespace(args={"endpoint": "samsung"}, get_json=lambda silent=True: {}))
    deleted = cast_app.media_history_item_delete(samsung_key)

    assert deleted["history_id"] == "samsung_kiosk"
    assert deleted["deleted"] == 1
    assert len(cast_app._history_public_items("tv")["items"]) == 1
    assert cast_app._history_public_items("samsung")["items"] == []


def test_legacy_dict_items_migrate_losslessly_and_preserve_keys(monkeypatch, tmp_path: Path) -> None:
    _install_history(monkeypatch, tmp_path)
    items = {
        f"content-{index:03d}": _legacy_item(index, samsung=index < 3)
        for index in range(123)
    }
    cast_app.WATCH_HISTORY_LEGACY.write_text(
        json.dumps({"schema": cast_app.WATCH_HISTORY_LEGACY_SCHEMA, "items": items}, indent=2),
        encoding="utf-8",
    )
    before = cast_app.WATCH_HISTORY_LEGACY.read_text(encoding="utf-8")

    tv = cast_app._history_public_items("tv")
    samsung = cast_app._history_public_items("samsung")

    assert len(tv["items"]) == 123
    assert len(samsung["items"]) == 3
    assert {item["content_key"] for item in tv["items"]} == set(items)
    assert cast_app.WATCH_HISTORY_LEGACY.read_text(encoding="utf-8") == before


def test_package_player_writes_endpoint_history_and_never_mutates_legacy(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / "player-state"
    state.mkdir()
    legacy = state / "watch-history.json"
    endpoints = state / "watch-history-endpoints.json"
    legacy_payload = {
        "schema": player_impl.WATCH_SCHEMA,
        "items": {
            "legacy-content": {
                "content_key": "legacy-content",
                "last_reason": "periodic",
                "position_seconds": 19.0,
            }
        },
    }
    legacy.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")
    before = legacy.read_text(encoding="utf-8")
    monkeypatch.setattr(player_impl, "WATCH_HISTORY", legacy)
    monkeypatch.setattr(player_impl, "WATCH_HISTORY_ENDPOINTS", endpoints)

    job = {"job_id": "b" * 16, "title": "Synthetic Package Title", "sources": []}
    source = {
        "source_id": "pkg-source",
        "title": "Synthetic Package Title",
        "duration": 300.0,
        "season": "1",
        "episode": "2",
    }
    entry = player_impl._save_progress_snapshot(job, source, 42.0, 300.0, False, "periodic")
    resume = player_impl._history_resume(job, source)
    store = json.loads(endpoints.read_text(encoding="utf-8"))

    assert legacy.read_text(encoding="utf-8") == before
    assert store["schema"] == player_impl.WATCH_ENDPOINT_SCHEMA
    assert len(store["histories"][player_impl.HOME_EDGE_TV_HISTORY_ID]["items"]) == 2
    assert entry["position_seconds"] == 42.0
    assert resume["history_found"] is True
    assert resume["resume_position"] == 42.0


def test_legacy_v2_last_job_source_aliases_resume_from_endpoint_history(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlayer(position=0.0)
    _install_runtime(monkeypatch, tmp_path, fake)
    _install_history(monkeypatch, tmp_path)
    legacy_key = "legacy-real-shape"
    cast_app.WATCH_HISTORY_LEGACY.write_text(
        json.dumps(
            {
                "schema": cast_app.WATCH_HISTORY_LEGACY_SCHEMA,
                "items": {
                    legacy_key: {
                        "content_key": legacy_key,
                        "display_title": "Synthetic Legacy Resume",
                        "position_seconds": 52.0,
                        "last_job_id": "a" * 16,
                        "last_source_id": "src-1080",
                        "last_reason": "periodic",
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    migrated = cast_app._history_public_items("tv")
    item = migrated["items"][0]

    assert item["job_id"] == "a" * 16
    assert item["source_id"] == "src-1080"
    assert item["title"] == "Synthetic Legacy Resume"
    result = cast_app._history_resume("tv", item["history_key"])
    assert result["history_id"] == "home_edge_tv"
    assert result["endpoint_id"] == "tv"
    assert fake.calls == [("play", "src-1080"), ("seek", 52.0), ("control", "play")]
