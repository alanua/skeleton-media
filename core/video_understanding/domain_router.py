from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.video_understanding.models import Domain, VideoUnderstandingError


_KEYWORDS: dict[Domain, tuple[str, ...]] = {
    Domain.DIOS: (
        "cad", "bim", "drawing", "blueprint", "technical drawing", "autocad",
        "revit", "dimension", "креслення", "чертеж",
    ),
    Domain.HOME_AUTOMATION: (
        "home assistant", "mqtt", "zigbee", "z-wave", "esp32", "esphome",
        "node-red", "automation", "автоматизац",
    ),
    Domain.TRAVEL: (
        "hotel", "flight", "destination", "itinerary", "route", "booking",
        "подорож", "готел", "маршрут",
    ),
    Domain.CONSTRUCTION: (
        "construction", "building site", "installation", "concrete", "roof",
        "electrical installation", "будівниц", "монтаж",
    ),
    Domain.LEGAL_DOCUMENTS: (
        "law", "legal", "contract", "court", "claim", "document", "regulation",
        "gesetz", "bescheid", "догов", "суд",
    ),
    Domain.AVIATION: (
        "aircraft", "flight training", "pilot", "runway", "aviation", "ultralight",
        "літак", "авіац",
    ),
    Domain.SKELETON_ARCHITECTURE: (
        "skeleton", "github", "docker", "agent", "memory gateway", "runner",
        "local llm", "server", "ollama", "api",
    ),
}


@dataclass(frozen=True)
class DomainCandidate:
    domain: Domain
    confidence: float
    evidence_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain.value,
            "confidence": self.confidence,
            "evidence_terms": list(self.evidence_terms),
        }


@dataclass(frozen=True)
class DomainRoute:
    selected: Domain
    candidates: tuple[DomainCandidate, ...]
    override_applied: bool
    original_selected: Domain


def route_domain(
    content: str | Iterable[str],
    *,
    explicit_profile: Domain | str | None = None,
) -> DomainRoute:
    if isinstance(content, str):
        combined = content.casefold()
    else:
        combined = "\n".join(str(item) for item in content).casefold()
    candidates: list[DomainCandidate] = []
    for domain, terms in _KEYWORDS.items():
        matched = tuple(term for term in terms if term in combined)
        if matched:
            confidence = min(0.99, 0.45 + 0.12 * len(matched))
            candidates.append(DomainCandidate(domain, confidence, matched))
    candidates.sort(key=lambda item: (-item.confidence, item.domain.value))

    if not candidates:
        candidates = [DomainCandidate(Domain.GENERAL_KNOWLEDGE, 0.35, ())]
    elif len(candidates) > 1 and abs(candidates[0].confidence - candidates[1].confidence) < 0.1:
        candidates.insert(0, DomainCandidate(Domain.GENERAL_KNOWLEDGE, 0.5, ("mixed",)))

    original = candidates[0].domain
    selected = original
    override_applied = False
    if explicit_profile is not None:
        try:
            selected = Domain(explicit_profile)
        except ValueError as exc:
            raise VideoUnderstandingError("UNKNOWN_PROFILE", "profile is not supported") from exc
        override_applied = selected is not original

    return DomainRoute(
        selected=selected,
        candidates=tuple(candidates),
        override_applied=override_applied,
        original_selected=original,
    )
