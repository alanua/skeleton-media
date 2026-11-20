"""Synthetic fixtures for the provisioned Flask and browser checks."""
from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "ops" / "skeleton_cast" / "runtime"
SCHEMA = "skeleton.home_edge.media_display_ownership.v1"
CLEAR = {"schema_version": SCHEMA, "state": "CLEAR", "reason": "display_mode_off"}
PAUSED = {
    "schema_version": SCHEMA, "state": "CLEAR", "reason": "confirmed_video_not_playing",
    "observations": [{"source": "skeleton_cast_player", "state": "CLEAR", "reason": "player_running_paused"}],
}
ORIGIN = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}


def atlas_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Fixture:
    def __init__(self, module, root):
        self.module = module
        self.app = module.app
        self.root = root
        self.history = root / "watch-history-endpoints.json"
        self.legacy = root / "watch-history.json"
        self.ownership = CLEAR
        self.real_owner_probe = module._atlas_owner_probe
        self.current_time = 10.0
        self.items = []

    def clock(self):
        return self.current_time

    def owner_probe(self):
        if isinstance(self.ownership, Exception):
            raise self.ownership
        return self.ownership

    def seed(self, key, digit, title, year, genres, **flags):
        job_id = digit * 16
        source = {"source_id": "synthetic-source", "episode": "Фільм", "quality": "1080p",
                  "height": 1080, "duration": 7200, "group": "Українська",
                  "url": "https://stream.invalid/synthetic", "headers": {"Referer": "https://private.invalid/"}}
        job = {
            "job_id": job_id, "title": title, "status": "ready", "sources": [source],
            "catalog": {"year": year, "genres": genres, "media_type": "movie", "overview": "Private synthetic details"},
        }
        (self.root / "jobs" / f"{job_id}.json").write_text(json.dumps(job), encoding="utf-8")
        self.items.append({
            "history_key": key, "job_id": job_id, "title": title, "source_id": source["source_id"],
            "position_seconds": 123, "url": "https://private.invalid/history", **flags,
        })
        return job_id

    def write_history(self):
        self.history.write_text(json.dumps({
            "schema": self.module.WATCH_HISTORY_ENDPOINT_SCHEMA,
            "histories": {self.module.HOME_EDGE_TV_HISTORY_ID: {"items": self.items}},
        }), encoding="utf-8")


@pytest.fixture
def atlas_fixture(monkeypatch, tmp_path):
    import flask
    import bs4  # noqa: F401
    monkeypatch.setenv("SKELETON_MEDIA_HOME", str(tmp_path))
    monkeypatch.setenv("SKELETON_MEDIA_SITE_REGISTRY", str(tmp_path / "sites.yaml"))
    import app as cast

    assert callable(getattr(flask.Flask, "test_client", None)), "Real Flask is required"
    fixture = Fixture(cast, tmp_path)
    (tmp_path / "jobs").mkdir()
    (tmp_path / "posters").mkdir()
    monkeypatch.setattr(cast, "BASE", RUNTIME)
    monkeypatch.setattr(cast, "STATIC", RUNTIME / "static")
    monkeypatch.setattr(cast.app, "static_folder", str(RUNTIME / "static"))
    monkeypatch.setattr(cast, "STATE", tmp_path)
    monkeypatch.setattr(cast, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(cast, "POSTERS", tmp_path / "posters")
    monkeypatch.setattr(cast, "WATCH_HISTORY_LEGACY", fixture.legacy)
    monkeypatch.setattr(cast, "WATCH_HISTORY_ENDPOINTS", fixture.history)
    monkeypatch.setattr(cast, "player", types.SimpleNamespace(status=lambda: {"running": False}))
    monkeypatch.setattr(cast, "_neighbor_mac", lambda _ip: None)
    monkeypatch.setattr(cast, "_client_identities", lambda: [])
    monkeypatch.setattr(cast, "_atlas_owner_probe", fixture.owner_probe)
    monkeypatch.setattr(cast, "_MOVIE_ATLAS", cast.movie_atlas.AtlasStore(fixture.clock))
    return fixture
