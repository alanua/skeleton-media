import json
import os

import pytest

from skeleton_media.cast import media_state


def _point_media_state_at(monkeypatch, tmp_path):
    home = tmp_path / "home"
    state_dir = home / ".local/state/skeleton/media-state"
    monkeypatch.setattr(media_state, "HOME", home)
    monkeypatch.setattr(media_state, "DIR", state_dir)
    monkeypatch.setattr(media_state, "SNAPSHOT", state_dir / "current.json")
    monkeypatch.setattr(media_state, "EVENTS", state_dir / "events.jsonl")
    return state_dir


def test_atomic_json_uses_unique_tmp_for_interleaved_same_target_writes(monkeypatch, tmp_path):
    state_dir = _point_media_state_at(monkeypatch, tmp_path)
    target = state_dir / "current.json"
    real_replace = os.replace
    interleaved = False
    sources = []

    def replace_with_interleaving(src, dst):
        nonlocal interleaved
        sources.append(src)
        if not interleaved:
            interleaved = True
            media_state._atomic_json(target, {"schema": media_state.SCHEMA, "writer": "inner"})
        real_replace(src, dst)

    monkeypatch.setattr(media_state.os, "replace", replace_with_interleaving)

    media_state._atomic_json(target, {"schema": media_state.SCHEMA, "writer": "outer"})

    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert json.loads(target.read_text(encoding="utf-8"))["writer"] in {"inner", "outer"}
    assert list(state_dir.glob("*.tmp")) == []
    assert list(state_dir.glob(".*.tmp")) == []


def test_atomic_json_keeps_previous_snapshot_and_cleans_owned_tmp_on_replace_failure(
    monkeypatch, tmp_path
):
    state_dir = _point_media_state_at(monkeypatch, tmp_path)
    target = state_dir / "current.json"
    media_state._atomic_json(target, {"schema": media_state.SCHEMA, "player": {"title": "old"}})

    def fail_replace(src, dst):
        raise RuntimeError("synthetic replace failure")

    monkeypatch.setattr(media_state.os, "replace", fail_replace)

    with pytest.raises(RuntimeError, match="synthetic replace failure"):
        media_state._atomic_json(target, {"schema": media_state.SCHEMA, "player": {"title": "new"}})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema": media_state.SCHEMA,
        "player": {"title": "old"},
    }
    assert list(state_dir.glob("*.tmp")) == []
    assert list(state_dir.glob(".*.tmp")) == []


def test_publish_preserves_state_schema_changed_fields_and_event_semantics(monkeypatch, tmp_path):
    state_dir = _point_media_state_at(monkeypatch, tmp_path)

    first = media_state.publish(
        mode={"name": "cast"},
        player={"title": "Pilot"},
        volume={"level": 10},
        reason="initial",
    )
    second = media_state.publish(player={"title": "Pilot"}, reason="observe")
    third = media_state.publish(player={"title": "Finale"}, reason="play")

    assert first["schema"] == media_state.SCHEMA
    assert first["changed"] == ["mode", "player", "volume"]
    assert second["changed"] == []
    assert third["changed"] == ["player"]

    snapshot = json.loads(media_state.SNAPSHOT.read_text(encoding="utf-8"))
    assert snapshot["schema"] == media_state.SCHEMA
    assert snapshot["mode"] == {"name": "cast"}
    assert snapshot["volume"] == {"level": 10}
    assert snapshot["player"] == {"title": "Finale"}
    assert snapshot["changed"] == ["player"]

    events = [
        json.loads(line)
        for line in media_state.EVENTS.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["reason"] for event in events] == ["initial", "play"]
    assert [event["changed"] for event in events] == [
        ["mode", "player", "volume"],
        ["player"],
    ]
    assert events[1]["state"] == {"player": {"title": "Finale"}}
    assert list(state_dir.glob("*.tmp")) == []
    assert list(state_dir.glob(".*.tmp")) == []
