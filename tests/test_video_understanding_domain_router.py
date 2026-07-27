from __future__ import annotations

from core.video_understanding.domain_router import route_domain
from core.video_understanding.models import Domain


def test_home_automation_and_skeleton_domains_route() -> None:
    home = route_domain("Home Assistant MQTT Zigbee ESP32 automation")
    assert home.selected is Domain.HOME_AUTOMATION
    skeleton = route_domain("Skeleton Runner GitHub Memory Gateway Docker")
    assert skeleton.selected is Domain.SKELETON_ARCHITECTURE


def test_mixed_content_falls_back_without_losing_candidates() -> None:
    route = route_domain("CAD drawing and Home Assistant MQTT")
    assert route.selected is Domain.GENERAL_KNOWLEDGE
    assert {candidate.domain for candidate in route.candidates} >= {
        Domain.DIOS,
        Domain.HOME_AUTOMATION,
    }


def test_explicit_profile_preserves_original_evidence() -> None:
    route = route_domain("aircraft pilot runway", explicit_profile=Domain.DIOS)
    assert route.selected is Domain.DIOS
    assert route.original_selected is Domain.AVIATION
    assert route.override_applied is True
    assert any(candidate.domain is Domain.AVIATION for candidate in route.candidates)


def test_unknown_content_routes_to_general_knowledge() -> None:
    route = route_domain("an unrelated synthetic subject")
    assert route.selected is Domain.GENERAL_KNOWLEDGE
