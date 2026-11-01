"""NOT-YET-WIRED metadata decisions; no runtime imports, network or file I/O.

Live /api/video/history remains unchanged until approved source parity and
integration into the canonical runtime. Never copy the deployed Home Edge app.py.
Callers must supply trusted years only from separate explicit date metadata or
verified external-ID evidence, never from title parsing or catalog.year alone.
Aliases must likewise come from trusted translated/original title metadata.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
import re
import unicodedata
from typing import Literal

MediaType = Literal["movie", "tv", "series"]


class Reason(str, Enum):
    TRUSTED_YEAR = "trusted_year"
    BRACKET_YEAR = "bracket_year"
    CONFLICTING_YEARS = "conflicting_years"
    NUMERIC_TITLE = "numeric_title"
    UNTRUSTED_CATALOG_YEAR = "untrusted_catalog_year"
    NO_YEAR = "no_year"
    TYPE_MISMATCH = "type_mismatch"
    TITLE_MISMATCH = "title_mismatch"
    YEAR_MISMATCH = "year_mismatch"
    STRONG_IDENTITY = "strong_identity"


@dataclass(frozen=True)
class YearEvidence:
    title: str
    constraint: int | None
    search_years: tuple[int | None, ...]
    reason: Reason


@dataclass(frozen=True)
class Candidate:
    title: str
    media_type: MediaType
    year: int | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchDecision:
    accepted: bool
    reason: Reason
    title_score: float


_BRACKET_YEAR = re.compile(r"\s*(?:\(([1-9][0-9]{3})\)|\[([1-9][0-9]{3})\])\s*$")


def _valid_year(value: int | None) -> int | None:
    # Fixed bounds keep decisions deterministic and reject bool (an int subclass).
    if value is not None and (type(value) is not int or not 1000 <= value <= 9999):
        raise ValueError("year must be an integer in 1000..9999 or None")
    return value


def _split_title(title: str) -> tuple[str, int | None]:
    match = _BRACKET_YEAR.search(title)
    if match and title[:match.start()].strip():
        return title[:match.start()].strip(), int(match[1] or match[2])
    return title.strip(), None


def year_evidence(
    title: str,
    *,
    catalog_year: int | None = None,
    explicit_year: int | None = None,
    external_id_year: int | None = None,
) -> YearEvidence:
    """Return ordered search attempts; None means search without a year.

    Unproven catalog years never constrain identity, even when they look plausible.
    Explicit date/external-ID years outrank title conventions. Conflicting trusted
    years require resolution: no searches or candidate acceptance are permitted.
    A constrained search may retry without a year, but validation keeps the year.
    Unbracketed suffixes are preserved because they may belong to the title.
    """
    clean, bracket = _split_title(title)
    catalog_year = _valid_year(catalog_year)
    trusted = {_valid_year(y) for y in (explicit_year, external_id_year) if y is not None}
    if len(trusted) > 1:
        return YearEvidence(clean, None, (), Reason.CONFLICTING_YEARS)
    if trusted:
        year = trusted.pop()
        return YearEvidence(clean, year, (year, None), Reason.TRUSTED_YEAR)
    if bracket is not None:
        return YearEvidence(clean, bracket, (bracket, None), Reason.BRACKET_YEAR)
    reason = (Reason.NUMERIC_TITLE if any(c.isdigit() for c in clean)
              else Reason.UNTRUSTED_CATALOG_YEAR if catalog_year is not None
              else Reason.NO_YEAR)
    return YearEvidence(clean, None, (None,), reason)


def _normalize(title: str) -> str:
    title = unicodedata.normalize("NFKC", title).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in title).split())


def candidate_match(
    evidence: YearEvidence,
    media_type: MediaType,
    candidate: Candidate,
    *,
    title_aliases: tuple[str, ...] = (),
) -> MatchDecision:
    """Require exact normalized title/alias and known matching media type.

    Canonical tv and legacy series denote the same type; movies remain distinct.
    Similarity is diagnostic only: even a high fuzzy score or matching year cannot
    establish identity. Translation is supplied as metadata, never guessed. These
    are candidate decisions, not a unique selection among same-title remakes.
    """
    _valid_year(candidate.year)
    if evidence.reason == Reason.CONFLICTING_YEARS:
        return MatchDecision(False, Reason.CONFLICTING_YEARS, 0.0)
    types = {"movie": "movie", "tv": "series", "series": "series"}
    if media_type not in types or types.get(candidate.media_type) != types[media_type]:
        return MatchDecision(False, Reason.TYPE_MISMATCH, 0.0)
    requested = [_normalize(t) for t in (evidence.title, *title_aliases)]
    offered = [_normalize(_split_title(t)[0]) for t in (candidate.title, *candidate.aliases)]
    score = max((SequenceMatcher(None, a, b, autojunk=False).ratio()
                 for a in requested for b in offered if a and b), default=0.0)
    if score != 1.0:
        return MatchDecision(False, Reason.TITLE_MISMATCH, score)
    if evidence.constraint is not None and candidate.year != evidence.constraint:
        return MatchDecision(False, Reason.YEAR_MISMATCH, score)
    return MatchDecision(True, Reason.STRONG_IDENTITY, score)
