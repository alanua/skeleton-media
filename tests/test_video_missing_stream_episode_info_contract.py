"""The Home video detail card must not look empty when metadata exists but no streams."""
from pathlib import Path

VIDEO = Path(__file__).resolve().parents[1] / "ops" / "skeleton_cast" / "runtime" / "video.html"


def _catalog_render() -> str:
    source = VIDEO.read_text(encoding="utf-8")
    return source.split("function renderCatalog(j={},statusText=''){", 1)[1].split(
        "\nasync function loadJob(", 1
    )[0]


def test_known_episode_count_shown_without_fabricated_streams() -> None:
    code = _catalog_render()
    assert "const catalogSeasons=Array.isArray(c.seasons)?c.seasons:[];" in code
    assert "const knownEpisodes=catalogSeasons.reduce" in code
    assert "unavailable&&knownEpisodes" in code
    assert "джерела недоступні" in code
    assert "j?.status" in code
    assert "j?.sources.push(" not in code
    assert "j?.sources=" not in code


def test_unavailable_state_is_not_hidden_by_overview_text() -> None:
    code = _catalog_render()
    assert "const description=String(c.overview||j?.overview||j?.description||'').trim();" in code
    assert "unavailable ? [fallback,description].filter(Boolean).join(' — ')" in code
    assert "$('catalogOverview').textContent=overview.slice(0,420)" in code
