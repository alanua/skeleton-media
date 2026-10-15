"""Static safety contract for terminal resolver failures in the Home video UI.

This guards against the 2026-10-15 regression where an origin_protected
failure stayed visually indistinguishable from an active stream search.
It deliberately does not contact media providers or affect playback.
"""

from pathlib import Path


VIDEO_HTML = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "skeleton_cast"
    / "runtime"
    / "video.html"
)


def _terminal_error_branch() -> str:
    page = VIDEO_HTML.read_text(encoding="utf-8")
    beginning = "if(['error','provider_error'].includes(j.status)){"
    assert page.count(beginning) == 1
    return page.split(beginning, 1)[1].split("\n    }", 1)[0]


def test_terminal_resolver_failure_stays_visible_after_toast_timeout() -> None:
    branch = _terminal_error_branch()
    assert "sources=[];" in branch
    assert "message(failure+guidance,true);" in branch
    # A 4.5-second toast used to disappear, exposing stale 'searching' UI.
    assert "clearTimeout(noticeTimer);" in branch
    assert "renderCatalog(j,failure+guidance);" in branch
    assert "pollTimer=setTimeout" not in branch
    assert "return;" in branch


def test_origin_protected_failure_has_bounded_retry_guidance() -> None:
    branch = _terminal_error_branch()
    assert "j.error_type==='origin_protected'" in branch
    assert "не раніше ніж за годину" in branch
    assert "оберіть інше джерело" in branch
    # Provider protection is surfaced, not bypassed or retried automatically.
    assert "fetch(" not in branch
    assert "setTimeout(()=>loadJob" not in branch
