"""Source contract: Home video refreshes a selected job after new episodes arrive.

No playback action may be triggered by the passive refresh callback.
"""
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "ops" / "skeleton_cast" / "runtime" / "video.html"


def _refresh_code():
    text = PAGE.read_text(encoding="utf-8")
    return text.split("let catalogRefreshBusy=false;",1)[1].split("async function boot(){",1)[0]


def test_background_job_refresh_uses_cache_bypass_and_stable_job_identity():
    code = _refresh_code()
    assert "document.hidden||!currentJobId||!jobData||catalogRefreshBusy||restoring" in code
    assert "'/api/jobs/'+encodeURIComponent(jobId)" in code
    assert "{cache:'no-store'}" in code
    assert "catalogRefreshStamp(latest)===oldStamp" in code
    assert "currentJobId!==jobId" in code


def test_passive_refresh_preserves_selection_without_starting_player():
    code = _refresh_code()
    for field in ("season", "episode", "quality", "voice"):
        assert f"{field}:$('"+field+"').value" in code
    assert "source_id:source?.source_id||''" in code
    assert "await loadJob(preferred)" in code
    assert "playSelected(" not in code
    assert "'/api/play'" not in code
    assert "mode" not in code


def test_resume_and_bounded_periodic_refresh():
    code = _refresh_code()
    assert "'visibilitychange'" in code
    assert "'pageshow'" in code
    assert "setInterval(()=>refreshVisibleVideoCatalog(),90000)" in code
    assert "finally{catalogRefreshBusy=false}" in code
