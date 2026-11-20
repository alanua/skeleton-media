from __future__ import annotations

import json
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from conftest import CLEAR, ORIGIN, PAUSED, SCHEMA, atlas_id

BASE = "/api/movie-atlas"
SESSIONS = BASE + "/sessions"


def headers(token):
    return {**ORIGIN, "X-Atlas-Token": token}


def test_trusted_auth_and_write_origin_on_all_methods(atlas_fixture):
    client = atlas_fixture.app.test_client()
    outside = {"REMOTE_ADDR": "192.0.2.40"}
    assert client.get("/movie-atlas", environ_overrides=outside).status_code == 403
    assert client.post(SESSIONS, headers=ORIGIN, environ_overrides=outside).status_code == 403
    created = client.post(SESSIONS, headers=ORIGIN).get_json()
    path = SESSIONS + "/" + created["code"]
    token = created["controller_token"]
    assert client.get(path, headers=headers(token), environ_overrides=outside).status_code == 403
    for invalid in ({}, {"Origin": "null"}, {"Origin": "https://elsewhere.invalid"},
                    {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"},
                    {"Origin": "http://localhost", "Sec-Fetch-Site": "same-site"}):
        invalid = {"X-Atlas-Token": token, **invalid}
        assert client.post(SESSIONS, headers=invalid).status_code == 403
        assert client.post(BASE + "/join", json={"code": created["code"]}, headers=invalid).status_code == 403
        assert client.put(path, json={"revision": 1, "state": {"query": "x"}}, headers=invalid).status_code == 403
        assert client.post(path + "/handoff", headers=invalid).status_code == 403
        assert client.delete(path, headers=invalid).status_code == 403


def test_flask_projection_latest_revision_and_handoff_visibility(atlas_fixture):
    f = atlas_fixture
    Image.new("RGB", (40, 60), "#667799").save(f.root / "posters" / "synthetic.png")
    Image.new("RGB", (970, 60), "#667799").save(f.root / "posters" / "large.png")
    job = f.seed("first", "a", "Synthetic Alpha", 2020, ["Drama"], poster="/posters/synthetic.png")
    f.items.append(dict(f.items[0]))
    f.seed("hidden", "b", "Hidden Fixture", 2024, ["Drama"], hidden=True)
    f.seed("deleted", "c", "Deleted Fixture", 2021, ["Drama"], deleted_at=5)
    f.seed("unsafe", "d", "Synthetic Unsafe Poster", 2024, ["Comedy"], poster="/posters/../unsafe.png")
    f.seed("large", "e", "Synthetic Large Poster", 2024, ["Comedy"], poster="/posters/large.png")
    f.seed("missing", "f", "Missing Job Fixture", 2024, ["Drama"])
    (f.root / "jobs" / ("f" * 16 + ".json")).unlink()
    f.legacy.write_text(json.dumps({"schema": f.module.WATCH_HISTORY_LEGACY_SCHEMA, "items": f.items}), encoding="utf-8")
    before = f.legacy.read_bytes()
    phone = f.app.test_client()
    tv = f.app.test_client()
    response = phone.post(SESSIONS, headers=ORIGIN)
    assert response.status_code == 201
    assert not f.history.exists()
    assert f.legacy.read_bytes() == before
    created = response.get_json()
    assert [entry["title"] for entry in created["catalog"]] == ["Synthetic Alpha", "Synthetic Unsafe Poster", "Synthetic Large Poster"]
    assert [entry.get("poster") for entry in created["catalog"]] == ["/posters/synthetic.png", None, None]
    code, token = created["code"], created["controller_token"]
    path = SESSIONS + "/" + code
    movie_id = atlas_id("first")

    for revision, patch in enumerate([
        {"selected_id": movie_id}, {"query": "Synthetic", "genre": "Drama", "year_min": 2020, "year_max": 2023},
        {"sort": "year", "camera": {"yaw": 22, "pitch": 12, "zoom": 1.4}},
    ], start=1):
        result = phone.put(path, json={"revision": revision, "state": patch}, headers=headers(token))
        assert result.status_code == 200
        assert result.get_json()["revision"] == revision + 1
        assert "controller_token" not in result.get_json()
    joined = tv.post(BASE + "/join", json={"code": code}, headers=ORIGIN).get_json()
    assert joined["revision"] == 4
    assert joined["state"]["camera"]["yaw"] == 22
    assert "controller_token" not in joined
    for private in ("private.invalid", "stream.invalid", "position_seconds", "source_id", "overview", "job_id"):
        assert private not in json.dumps(joined)
    display = joined["display_token"]
    assert tv.get(path, headers=headers(display)).get_json()["state"]["selected_id"] == movie_id
    assert tv.get(path + "?role=controller", headers=headers(display)).status_code == 403
    assert tv.put(path, json={"revision": 4, "state": {"query": ""}}, headers=headers(display)).status_code == 403
    assert tv.delete(path, headers=headers(display)).status_code == 403
    assert tv.post(path + "/handoff", headers=headers(display)).status_code == 403
    assert phone.put(path, json={"revision": 1, "state": {"query": ""}}, headers=headers(token)).status_code == 409
    for bad_id in (atlas_id("hidden"), atlas_id("deleted"), atlas_id("missing"), "unknown"):
        assert phone.put(path, json={"revision": 4, "state": {"selected_id": bad_id}}, headers=headers(token)).status_code == 400

    result = phone.post(path + "/handoff", headers=headers(token)).get_json()
    selection = parse_qs(urlsplit(result["selection_url"]).query)["atlas_history"][0]
    assert phone.get(BASE + "/selection/" + selection).get_json() == {"job_id": job, "source_id": "synthetic-source"}
    f.items = [entry for entry in f.items if entry["history_key"] != "first"]
    f.write_history()
    assert phone.post(path + "/handoff", headers=headers(token)).status_code == 409
    assert phone.get(BASE + "/selection/" + selection).status_code == 404
    assert phone.put(path, json={"revision": 4, "state": {"selected_id": movie_id}}, headers=headers(token)).status_code == 400
    assert phone.delete(path, headers=headers(token)).status_code == 200
    assert tv.get(path, headers=headers(display)).status_code == 410


@pytest.mark.parametrize("denied", [PAUSED, {"state": "OWNER"}, {"state": "UNKNOWN"}, {}, OSError("unavailable")])
def test_display_preemption_and_recovery(atlas_fixture, denied):
    f = atlas_fixture
    client = f.app.test_client()
    created = client.post(SESSIONS, headers=ORIGIN).get_json()
    body = {"code": created["code"]}
    joined = client.post(BASE + "/join", json=body, headers=ORIGIN).get_json()
    path = SESSIONS + "/" + created["code"]
    f.ownership = denied
    assert client.post(SESSIONS, headers=ORIGIN).get_json()["code"] == "display_busy"
    assert client.post(BASE + "/join", json=body, headers=ORIGIN).status_code == 409
    assert client.get(path, headers=headers(joined["display_token"])).status_code == 409
    assert client.get(path + "?role=controller", headers=headers(created["controller_token"])).status_code == 200
    f.ownership = CLEAR
    assert client.get(path, headers=headers(joined["display_token"])).status_code == 200


def test_canonical_command_contract(atlas_fixture, monkeypatch):
    cast = atlas_fixture.module
    monkeypatch.setattr(cast, "HOME", atlas_fixture.root)
    monkeypatch.setattr(cast, "_atlas_owner_probe", atlas_fixture.real_owner_probe)
    calls = []
    probe = {"status": 1, "payload": CLEAR}

    def run(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, probe["status"], json.dumps(probe["payload"]), "")

    monkeypatch.setattr(cast.subprocess, "run", run)
    client = cast.app.test_client()
    route = str(atlas_fixture.root / ".local/bin/home-edge-media-display-owner")
    assert client.post(SESSIONS, headers=ORIGIN).status_code == 201
    assert calls == [([route, "status"], {"capture_output": True, "text": True, "timeout": 6, "check": False})]
    for status, report in [(1, PAUSED), (2, CLEAR), (0, CLEAR), (1, {**CLEAR, "schema_version": "wrong"}),
                           (1, {**CLEAR, "reason": "confirmed_android_video_not_playing"})]:
        probe.update(status=status, payload=report)
        assert client.post(SESSIONS, headers=ORIGIN).status_code == 409


def test_payload_rate_capacity_disconnection_and_expiration(atlas_fixture):
    f = atlas_fixture
    client = f.app.test_client()
    assert client.post(SESSIONS, headers=ORIGIN, data="a" * 3000).status_code == 413
    created = client.post(SESSIONS, headers=ORIGIN).get_json()
    assert created["catalog"] == []
    path = SESSIONS + "/" + created["code"]
    token = headers(created["controller_token"])
    joined = client.post(BASE + "/join", json={"code": created["code"]}, headers=ORIGIN).get_json()

    assert client.put(path, json={"revision": 1, "state": {"query": "a" * 2048}}, headers=token).status_code == 413
    assert client.put(path, json={"revision": True, "state": {"query": "x"}}, headers=token).status_code == 400
    assert client.put(path, data='{"revision":1,"state":{"camera":{"yaw":0,"pitch":0,"zoom":NaN}}}',
                      content_type="application/json", headers=token).status_code == 400
    for revision in range(1, 19):
        assert client.put(path, headers=token, json={"revision": revision, "state": {"query": str(revision)}}).status_code == 200
    assert client.put(path, headers=token, json={"revision": 19, "state": {"query": "rate"}}).status_code == 429

    for _ in range(f.module.movie_atlas.MAX_SESSIONS - 1):
        assert client.post(SESSIONS, headers=ORIGIN).status_code == 201
    assert client.post(SESSIONS, headers=ORIGIN).status_code == 429
    f.current_time += f.module.movie_atlas.DISCONNECTED_SECONDS + 1
    assert not client.get(path + "?role=controller", headers=token).get_json()["display_connected"]
    display = headers(joined["display_token"])
    assert client.get(path, headers=display).get_json()["display_connected"]
    assert client.get(path + "?role=controller", headers=token).get_json()["display_connected"]
    f.current_time += f.module.movie_atlas.SESSION_SECONDS
    assert client.get(path, headers=display).status_code == 410
    assert client.post(BASE + "/join", json={"code": created["code"]}, headers=ORIGIN).status_code == 410
    assert client.post(SESSIONS, headers=ORIGIN).status_code == 201
