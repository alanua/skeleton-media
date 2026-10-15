"""Regression for wrong media attached to a correctly identified TV catalog entry.

The Bubblegum Crisis (1987) discovery previously attached unrelated
"Полураспад" streaming links simply because both pages were TV series.
"""

from skeleton_media.cast import media_discovery


META = {
    "title": "Щоденна криза",
    "original_title": "バブルガムクライシス",
    "search_aliases": ["Bubblegum Crisis", "Кризис каждый день"],
    "year": 1987,
    "media_type": "tv",
    "tmdb_id": 42882,
}


def _row(title: str, url: str, year: int = 0) -> dict:
    return {
        "title": title,
        "page_url": url,
        "year": year,
        "media_type": "tv",
    }


def test_reject_1987_anime_wrong_show_with_working_player() -> None:
    false_match = _row(
        "Полураспад (1 сезон) смотреть онлайн бесплатно",
        "https://kinogo.online/serialy/118290-poluraspad.html",
    )
    assert not media_discovery._candidate_content_compatible(META, false_match)


def test_reject_unrelated_series_with_similar_search_ranking() -> None:
    false_match = _row(
        "Почти настоящий детектив (1 сезон) смотреть онлайн бесплатно",
        "https://kinogo.online/serialy/112827-detektiv.html",
    )
    assert not media_discovery._candidate_content_compatible(META, false_match)


def test_accept_localized_correct_1987_anime() -> None:
    correct = _row(
        "Щоденна криза (8 серій) українською",
        "https://anitube.in.ua/4556-schodenna-kriza.html",
        1987,
    )
    assert media_discovery._candidate_content_compatible(META, correct)


def test_accept_english_title_as_verified_alias() -> None:
    correct = _row(
        "Bubblegum Crisis (1987) Season 1",
        "https://example.org/bubblegum-crisis-1987-season-1",
        1987,
    )
    assert media_discovery._candidate_content_compatible(META, correct)


def test_do_not_confuse_franchise_spinoff_with_1987_original() -> None:
    spinoff = _row(
        "Bubblegum Crisis Tokyo 2040",
        "https://example.org/bubblegum-crisis-tokyo-2040",
        1998,
    )
    assert not media_discovery._candidate_content_compatible(META, spinoff)
