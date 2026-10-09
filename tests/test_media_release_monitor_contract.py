from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from skeleton_media.cast import media_discovery
from skeleton_media.cast import media_release_monitor as monitor


@pytest.fixture()
def synthetic_monitor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db = tmp_path / "media-catalog.sqlite3"
    monkeypatch.setattr(media_discovery, "DB", db)
    monkeypatch.setattr(monitor, "DB", db)
    monkeypatch.setattr(monitor.time, "time", lambda: 1_800_000_000)
    with sqlite3.connect(db) as con:
        con.execute(
            """
            CREATE TABLE media_items (
              media_uid TEXT PRIMARY KEY,
              source_url TEXT NOT NULL UNIQUE,
              provider TEXT NOT NULL,
              media_type TEXT NOT NULL,
              title TEXT NOT NULL,
              original_title TEXT,
              release_year INTEGER,
              tmdb_id INTEGER,
              imdb_id TEXT,
              poster TEXT,
              overview TEXT,
              metadata_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT INTO media_items(
              media_uid, source_url, provider, media_type, title, tmdb_id,
              metadata_json, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "media-tv-1",
                "https://catalog.example/tv/42",
                "synthetic",
                "tv",
                "Synthetic Series",
                42,
                "{}",
                "2026-10-09T00:00:00+00:00",
            ),
        )
    return db


def _job(*, sources: list[dict] | None = None) -> dict:
    return {
        "catalog": {
            "media_uid": "media-tv-1",
            "tmdb_id": 42,
            "media_type": "tv",
            "title": "Synthetic Series",
            "catalog_url": "https://catalog.example/tv/42",
        },
        "sources": sources or [{"season": 1, "episode": 1, "url": "https://media.example/s01e01"}],
    }


def _snap(status: str, release_key: str, season: int, episode: int) -> dict:
    return {
        "status": status,
        "release_key": release_key,
        "season": season,
        "episode": episode,
        "air_date": "2026-01-01",
        "episode_title": f"Episode {episode}",
    }


def test_effective_subscription_requires_explicit_on_and_series(
    synthetic_monitor: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "tmdb_release_snapshot", lambda tmdb_id: _snap("Returning Series", "S01E01", 1, 1))

    off_state = monitor.ensure_auto_for_job(_job(), reason="watched")
    assert off_state["enabled"] is False

    on_state = monitor.set_for_job(_job(), True)
    assert on_state["enabled"] is True

    monitor.set_for_job(_job(), False)
    assert monitor.ensure_auto_for_job(_job(), reason="watched")["enabled"] is False
    assert monitor.schedules()[0]["enabled"] is False


def test_tmdb_terminal_update_does_not_replace_already_waiting_localization(
    synthetic_monitor: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = iter(
        (
            _snap("Returning Series", "S01E01", 1, 1),
            _snap("Ended", "S01E03", 1, 3),
        )
    )
    monkeypatch.setattr(monitor, "tmdb_release_snapshot", lambda tmdb_id: next(snapshots))

    monitor.set_for_job(_job(), True)
    with sqlite3.connect(synthetic_monitor) as con:
        con.execute(
            """
            UPDATE media_release_monitors
            SET pending_release_key='S01E02',
                pending_season=1,
                pending_episode=2,
                pending_air_date='2026-01-01',
                pending_episode_title='Episode 2',
                state='WAITING_FOR_TRANSLATION',
                last_release_check=0
            WHERE media_uid='media-tv-1'
            """
        )

    result = monitor.tick("media-tv-1", "tick-1", lambda row, release: {"available": False})

    assert result["reason"] == "WAITING_FOR_TRANSLATION"
    row = monitor._get("media-tv-1")
    assert row["title_status"] == "Ended"
    assert row["pending_release_key"] == "S01E02"
    assert row["pending_episode"] == 2
    assert monitor.schedules()[0]["cron_expression"] == "17 */6 * * *"


def test_notification_intent_is_claimed_once_until_ack(
    synthetic_monitor: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "tmdb_release_snapshot", lambda tmdb_id: _snap("Returning Series", "S01E01", 1, 1))
    monitor.set_for_job(_job(), True)
    with sqlite3.connect(synthetic_monitor) as con:
        con.execute(
            """
            UPDATE media_release_monitors
            SET pending_release_key='S01E02',
                pending_season=1,
                pending_episode=2,
                pending_air_date='2026-01-01',
                pending_episode_title='Episode 2',
                state='WAITING_FOR_TRANSLATION',
                last_release_check=?
            WHERE media_uid='media-tv-1'
            """,
            (1_800_000_000,),
        )

    first = monitor.tick(
        "media-tv-1",
        "tick-available",
        lambda row, release: {"available": True, "capability": "uk_audio", "provider": "synthetic"},
    )
    second = monitor.tick(
        "media-tv-1",
        "tick-available-duplicate",
        lambda row, release: {"available": True, "capability": "uk_audio", "provider": "synthetic"},
    )

    assert first["reason"] == "LOCALIZATION_AVAILABLE"
    assert first["notification"]["release_key"] == "S01E02"
    assert second["reason"] == "ALERT_ALREADY_CLAIMED"
    assert second["notification"] is None

    ack = monitor.ack("media-tv-1", "S01E02", "tick-available", 123)
    assert ack == {"status": "ok", "media_uid": "media-tv-1", "release_key": "S01E02", "message_id": 123}
    assert monitor.tick(
        "media-tv-1",
        "tick-after-ack",
        lambda row, release: {"available": True},
    )["reason"] == "NOTIFIED"
