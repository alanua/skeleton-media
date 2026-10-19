"""Shared added-work history stays independent of playback endpoint and loads quickly.

The list is all cataloged works except explicit user deletions; Samsung/TV
watch history is only a per-endpoint progress state and must not replace it.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

PAGE = Path(__file__).resolve().parents[1] / "ops" / "skeleton_cast" / "runtime" / "video.html"


def _html() -> str:
    return PAGE.read_text(encoding="utf-8")


def _loader() -> str:
    html = _html()
    return html.split("async function loadHistory(force=false){", 1)[1].split(
        "\nasync function openHistory(", 1
    )[0]


def test_one_shared_catalog_never_falls_back_to_per_device_watch_history() -> None:
    source = _html()
    fn = _loader()
    assert "jf('/api/video/history',{cache:'no-store'})" in fn
    assert "/api/media/endpoints/" not in fn
    assert "historyEndpointId()" not in source
    assert "loadHistory(true)" in source


def test_reopen_uses_memory_cache_and_deduplicates_concurrent_fetches() -> None:
    source = _html()
    fn = _loader()
    assert "if(!force&&historyLoadedAt){renderSharedHistoryUi();return true}" in fn
    assert "if(historyLoadPromise)return historyLoadPromise" in fn
    assert "historyLoadPromise=null" in fn
    assert "historyItems=loaded;historyLoadedAt=Date.now()" in fn
    assert "else if(!historyLoadedAt){historyItems=[]}" in fn
    assert "if(Date.now()-historyLoadedAt>30000)void loadHistory(true);" in source


def test_deleted_works_removed_from_global_list_without_endpoint_side_effects() -> None:
    source = _html()
    start = source.index("async function deleteHistory(")
    end = source.index("\nfunction renderSharedHistoryUi()", start)
    deletion = source[start:end]
    assert "jf('/api/video/history/'+encodeURIComponent(historyId),{method:'DELETE'})" in deletion
    assert "/api/media/endpoints/" not in deletion
    assert "historyItems=historyItems.filter(x=>x.history_id!==historyId)" in deletion
    assert "historyLoadedAt=Date.now()" in deletion


def test_fast_history_change_keeps_playback_controls_separate() -> None:
    source = _html()
    assert "async function openHistory(" in source
    assert "/api/video/history/open" in source
    assert "function renderSharedHistoryUi()" in source
    assert "Останні додані або переглянуті — першими" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_inline_javascript_syntax_is_valid(tmp_path: Path) -> None:
    source = _html()
    scripts = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", source, re.S | re.I)
    assert scripts
    path = tmp_path / "history-inline.js"
    path.write_text("\n".join(scripts), encoding="utf-8")
    run = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
