"""Catalog search title identity: strip display labels, never real title suffixes."""
from skeleton_media.cast import media_discovery as discovery

META = {
    "title": "Щоденна криза",
    "original_title": "バブルガムクライシス",
    "search_aliases": ["Bubblegum Crisis", "Кризис каждый день"],
    "year": 1987,
    "media_type": "tv",
    "tmdb_id": 42882,
}

def item(title, year=1987):
    return {
        "title": title,
        "page_url": "https://anitube.in.ua/4556-schodenna-kriza.html",
        "media_type": "tv",
        "year": year,
    }

def test_anitube_localized_8_episodes_label_not_false_spinoff():
    assert discovery._candidate_content_compatible(
        META, item("Щоденна криза (8 серій) українською")
    )

def test_anitube_online_title_not_false_spinoff():
    assert discovery._candidate_content_compatible(
        META, item("Щоденна криза українською онлайн")
    )

def test_search_index_title_with_season_and_provider_suffix():
    assert discovery._candidate_content_compatible(
        META, item("Аніме Щоденна криза (1 сезон) українською онлайн – UAserial")
    )

def test_different_show_still_rejected():
    assert not discovery._candidate_content_compatible(
        META, item("Полураспад (1 сезон) смотреть онлайн бесплатно")
    )

def test_actual_spin_off_suffix_still_rejected():
    assert not discovery._candidate_content_compatible(
        META, item("Bubblegum Crisis Tokyo 2040", year=1998)
    )

def test_simple_canonical_and_english_alias_still_accepted():
    assert discovery._candidate_content_compatible(META, item("Щоденна криза"))
    assert discovery._candidate_content_compatible(META, item("Bubblegum Crisis"))
