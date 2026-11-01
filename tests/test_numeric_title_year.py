"""Synthetic source-only tests; this helper is not wired into live history."""

import pytest

from skeleton_media.cast.numeric_title_year import (
    Candidate, Reason, candidate_match, year_evidence,
)


@pytest.mark.parametrize(('title', 'year'), [
    ('2001: Космічна Одіссея', 1968), ('2001: A Space Odyssey', 1968),
    ('1917', 2019), ('2012', 2009), ('1984', 1984), ('2046', 2004),
])
def test_numeric_titles_search_without_year_and_match(title, year):
    evidence = year_evidence(title, catalog_year=int(title[:4]))
    assert evidence.title == title
    assert evidence.search_years == (None,)
    assert evidence.constraint is None
    assert evidence.reason == Reason.NUMERIC_TITLE
    assert candidate_match(evidence, 'movie', Candidate(title, 'movie', year)).accepted


@pytest.mark.parametrize(('title', 'clean', 'year'), [
    ('Fargo (2014)', 'Fargo', 2014),
    ('Some Title (2023)', 'Some Title', 2023),
    ('Some Title [2023]', 'Some Title', 2023),
    ('2001: A Space Odyssey (1968)', '2001: A Space Odyssey', 1968),
])
def test_bracket_conventions(title, clean, year):
    evidence = year_evidence(title)
    assert evidence.title == clean
    assert evidence.constraint == year
    assert evidence.search_years == (year, None)
    assert evidence.reason == Reason.BRACKET_YEAR


@pytest.mark.parametrize('title', ['Some Title 2023', '2001', '1917', '2012'])
def test_unbracketed_numbers_are_preserved(title):
    evidence = year_evidence(title)
    assert evidence.title == title
    assert evidence.search_years == (None,)


@pytest.mark.parametrize('kwargs', [
    {'explicit_year': 2022}, {'external_id_year': 2022},
    {'explicit_year': 2022, 'external_id_year': 2022},
])
def test_trusted_year_survives_numeric_title_and_stale_catalog(kwargs):
    evidence = year_evidence('2001: A Space Odyssey', catalog_year=2001, **kwargs)
    assert evidence.constraint == 2022
    assert evidence.search_years == (2022, None)
    assert evidence.reason == Reason.TRUSTED_YEAR
    assert candidate_match(evidence, 'movie', Candidate(evidence.title, 'movie', 2022)).accepted
    assert not candidate_match(evidence, 'movie', Candidate(evidence.title, 'movie', 1968)).accepted


def test_trusted_date_outranks_bracket_hint():
    assert year_evidence('Title (2023)', explicit_year=2022).constraint == 2022


def test_conflicting_trusted_evidence_fails_closed():
    evidence = year_evidence('1917', explicit_year=2019, external_id_year=2020)
    assert evidence.search_years == ()
    result = candidate_match(evidence, 'movie', Candidate('1917', 'movie', 2019))
    assert not result.accepted
    assert result.reason == Reason.CONFLICTING_YEARS


def test_catalog_year_alone_is_not_authoritative():
    evidence = year_evidence('A Film', catalog_year=2001)
    assert evidence.reason == Reason.UNTRUSTED_CATALOG_YEAR
    assert evidence.search_years == (None,)
    assert candidate_match(evidence, 'movie', Candidate('A Film', 'movie', 1968)).accepted


def test_no_year():
    assert year_evidence('A Film').reason == Reason.NO_YEAR


@pytest.mark.parametrize('title', ['Title (2023]', 'Title [2023)', 'Title (0000)', '(2023)'])
def test_non_year_brackets_preserved(title):
    evidence = year_evidence(title)
    assert evidence.title == title
    assert evidence.constraint is None


@pytest.mark.parametrize('candidate_year', [2022, None])
def test_unfiltered_retry_cannot_discard_constraint(candidate_year):
    evidence = year_evidence('Some Title (2023)')
    assert evidence.search_years == (2023, None)
    result = candidate_match(evidence, 'movie', Candidate('Some Title', 'movie', candidate_year))
    assert not result.accepted
    assert result.reason == Reason.YEAR_MISMATCH


@pytest.mark.parametrize('title', ['Random Movie', '2001: Another Story', '2002: A Space Odyssey'])
def test_same_year_never_establishes_identity(title):
    result = candidate_match(year_evidence('2001: A Space Odyssey', explicit_year=1968),
                             'movie', Candidate(title, 'movie', 1968))
    assert not result.accepted
    assert result.reason == Reason.TITLE_MISMATCH


def test_type_mismatch():
    result = candidate_match(year_evidence('Fargo (2014)'), 'series', Candidate('Fargo', 'movie', 2014))
    assert not result.accepted
    assert result.reason == Reason.TYPE_MISMATCH


def test_series_match():
    assert candidate_match(year_evidence('Fargo (2014)'), 'series', Candidate('Fargo', 'series', 2014)).accepted


@pytest.mark.parametrize(('selected_type', 'candidate_type'), [
    ('tv', 'tv'), ('series', 'tv'), ('tv', 'series'), ('movie', 'movie'),
])
def test_canonical_and_legacy_media_types(selected_type, candidate_type):
    result = candidate_match(year_evidence('Synthetic Title (2014)'), selected_type,
                             Candidate('Synthetic Title', candidate_type, 2014))
    assert result.accepted
    assert result.reason == Reason.STRONG_IDENTITY
    assert result.title_score == 1.0


@pytest.mark.parametrize(('selected_type', 'candidate_type'), [
    ('tv', 'movie'), ('movie', 'tv'),
    ('series', 'movie'), ('movie', 'series'),
    ('anime', 'movie'), ('tv', 'anime'), ('anime', 'anime'),
])
def test_movie_and_series_types_remain_distinct(selected_type, candidate_type):
    # An anime movie still requires movie type, regardless of its title or year.
    result = candidate_match(year_evidence('Synthetic Anime (2014)'), selected_type,
                             Candidate('Synthetic Anime', candidate_type, 2014))
    assert not result.accepted
    assert result.reason == Reason.TYPE_MISMATCH
    assert result.title_score == 0.0


def test_stale_numeric_catalog_year_requires_verified_title_alias():
    local = '2001: Космічна Одіссея'
    evidence = year_evidence(local, catalog_year=2001)
    assert evidence.search_years == (None,)
    assert evidence.constraint is None
    assert evidence.reason == Reason.NUMERIC_TITLE
    # Synthetic trusted alias metadata is explicitly supplied by the caller.
    candidate = Candidate('2001: A Space Odyssey', 'movie', 1968, aliases=(local,))
    result = candidate_match(evidence, 'movie', candidate)
    assert result.accepted
    assert result.reason == Reason.STRONG_IDENTITY
    assert result.title_score == 1.0
    for title in ('2001: A Space Odyssey', '2001: Another Story'):
        result = candidate_match(evidence, 'movie', Candidate(title, 'movie', 1968))
        assert not result.accepted
        assert result.reason == Reason.TITLE_MISMATCH


def test_1917_catalog_ambiguity_and_trusted_explicit_year():
    candidate = Candidate(' １９１７! ', 'movie', 2019)
    ambiguous = year_evidence('1917', catalog_year=1917)
    assert ambiguous.reason == Reason.NUMERIC_TITLE
    assert ambiguous.constraint is None
    assert ambiguous.search_years == (None,)
    result = candidate_match(ambiguous, 'movie', candidate)
    assert result.accepted
    assert result.reason == Reason.STRONG_IDENTITY
    assert result.title_score == 1.0

    trusted = year_evidence('1917', catalog_year=1917, explicit_year=1917)
    assert trusted.reason == Reason.TRUSTED_YEAR
    assert trusted.constraint == 1917
    assert trusted.search_years == (1917, None)
    result = candidate_match(trusted, 'movie', candidate)
    assert not result.accepted
    assert result.reason == Reason.YEAR_MISMATCH
    assert result.title_score == 1.0


@pytest.mark.parametrize('candidate', ['  2001 — A SPACE ODYSSEY! ', '２００１: A Space Odyssey'])
def test_normalized_title(candidate):
    result = candidate_match(year_evidence('2001: A Space Odyssey'), 'movie', Candidate(candidate, 'movie', 1968))
    assert result.accepted
    assert result.title_score == 1.0


@pytest.mark.parametrize('request_alias', [True, False])
def test_explicit_translation_alias(request_alias):
    local, english = '2001: Космічна Одіссея', '2001: A Space Odyssey'
    candidate = Candidate(english, 'movie', 1968, () if request_alias else (local,))
    assert candidate_match(year_evidence(local), 'movie', candidate,
                           title_aliases=(english,) if request_alias else ()).accepted


def test_translation_not_guessed():
    assert not candidate_match(year_evidence('2001: Космічна Одіссея'), 'movie',
                               Candidate('2001: A Space Odyssey', 'movie', 1968)).accepted


def test_high_similarity_is_insufficient():
    result = candidate_match(year_evidence('2001: A Space Odyssey'), 'movie',
                             Candidate('2001: A Space Odysseys', 'movie', 1968))
    assert .95 < result.title_score < 1
    assert not result.accepted


@pytest.mark.parametrize('title', ['', '---', '   '])
def test_empty_normalization_never_accepts(title):
    assert not candidate_match(year_evidence(title), 'movie', Candidate(title, 'movie', 2023)).accepted


@pytest.mark.parametrize('year', [True, '2023', 999, 10000])
def test_invalid_years_rejected(year):
    with pytest.raises(ValueError):
        year_evidence('Title', explicit_year=year)
    with pytest.raises(ValueError):
        candidate_match(year_evidence('Title'), 'movie', Candidate('Title', 'movie', year))
