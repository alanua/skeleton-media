from __future__ import annotations

from types import SimpleNamespace

from skeleton_media.cast import player


def test_browser_play_prefers_resolved_source_url_over_protected_origin(monkeypatch):
    calls = []
    monkeypatch.setattr(player, "stop", lambda: None)
    monkeypatch.setattr(player, "status", lambda: {"running": True})

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"opened": true}', stderr="")

    monkeypatch.setattr(player.subprocess, "run", run)

    result = player.play_browser(
        {
            "job_id": "a" * 16,
            "page_url": "https://anitube.in.ua/5362-ya-kosmchniy-shahtar.html",
        },
        {
            "source_id": "episode-1",
            "backend": "chrome-browser",
            "browser_profile": "default",
            "browser_index": 1,
            "url": "https://moonanime.art/player/episode-1",
            "quality": "HTML5 · повний екран",
            "translation": "Anime Classic",
        },
    )

    assert calls[0][1:4] == [
        "play",
        "default",
        "https://moonanime.art/player/episode-1",
    ]
    assert result["backend"] == "chrome-browser"
