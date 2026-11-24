"""Review contracts for exact candidate Android paging and fast no-source exit.

These Python tests exercise the source transformation and guards.
Actual Kotlin/JVM + device acceptance is a separate release gate.
"""
from __future__ import annotations

import hashlib
import pytest

from review.home_android_history_paging_20261124 import (
    ANDROID_SHA, OLD, NEW, OLD_QUERY, OLD_COUNTER, patch_candidate,
)


def candidate_fixture():
    old_open='        val open=api.post("/api/video/history/open",savedHistoryOpenBody(endpoint.endpointId,item,seasonHint))'
    return "\n".join((
        "package com.skeleton.home",
        "import org.json.JSONObject",
        OLD,
        "private fun historyDialog() {",
        OLD_QUERY,
        "    "+OLD_COUNTER,
        "}",
        "suspend fun openSavedHistoryItem(item:JSONObject,seasonHint:String?=null):Boolean{",
        old_open,
        '        val preferred=endpointSelectionFromHistoryOpen(endpoint.endpointId,item,open)',
        "        return true",
        "}",
    ))


def transformed():
    source=candidate_fixture()
    return patch_candidate(source,expected_sha=hashlib.sha256(source.encode()).hexdigest())


def test_baseline_hash_is_pinned():
    assert len(ANDROID_SHA)==64
    assert not any(x not in "0123456789abcdef" for x in ANDROID_SHA)


def test_unreviewed_source_is_rejected():
    with pytest.raises(ValueError,match="ANDROID_SOURCE_PARITY_FAILED"):
        patch_candidate(candidate_fixture())


def test_exact_five_edit_anchors():
    output=transformed()
    assert "nextSavedVideoHistoryOffset" in output
    assert "savedVideoHistoryItemsPaged" in output
    assert "val totalCount=items.size" in output
    assert "val visibleCount=items.count" in output
    assert "Повне відеоджерело ще не знайдено" in output


def test_old_one_page_reader_is_replaced():
    output=transformed()
    assert "runCatching{savedVideoHistoryItemsPaged(api)}" in output
    assert "runCatching{api.get(savedVideoHistoryPath()).optJSONArray" not in output


def test_legacy_server_fallback_exists():
    assert 'reply.optJSONObject("page") ?: return null' in transformed()


def test_page_reader_bound_to_hundred():
    output=transformed()
    assert "while(true)" in output
    assert 'count>100' in output
    assert "repeat(100)" not in output
    assert "next>10_000" not in output
    assert "restart_required" in output
    assert "revision=$revision" in output
    assert "canonical.size!=total" in output


def test_page_offset_monotonic_and_actual_reply_checked():
    output=transformed()
    assert "actualOffset!=expectedOffset" in output
    assert "count<=0" in output
    assert "offset=next" in output


def test_later_pages_are_requested_from_same_api_endpoint():
    output=transformed()
    assert '?offset=$offset&limit=100' in output
    assert "savedVideoHistoryPath()" in output


def test_coroutine_cancellation_is_observed_between_pages():
    output=transformed()
    assert output.count("currentCoroutineContext().ensureActive()")==2
    assert "kotlinx.coroutines.CancellationException" in output


def test_history_is_canonical_deduplicated():
    output=transformed()
    assert "val canonical=canonicalHistoryItems(collected)" in output
    assert "canonical.size!=total" in output
    assert "return canonical" in output


def test_hidden_count_is_not_search_count():
    output=transformed()
    assert "val totalCount=items.size" in output
    assert 'items.count{!it.optBoolean("hidden",false)}' in output
    assert '$totalCount/$visibleCount' in output


def test_existing_dialog_filters_are_retained():
    output=transformed()
    assert 'val q=query.trim().lowercase(Locale.ROOT)' in output
    assert "Text(" in output


def test_no_source_open_has_fast_error_before_load_job():
    output=transformed()
    error=output.index('if(open.optString("status") in setOf')
    load=output.index("val preferred=endpointSelectionFromHistoryOpen")
    assert error<load
    assert all(status in output for status in ("discovering","no_verified_sources","searching_sources"))


def test_no_auto_play_or_mode_switch_added():
    output=transformed()
    for danger in ('player.play(', 'skeleton-cast-control', 'systemctl', 'deleteAll'):
        assert danger not in output


def test_modifying_one_anchor_fails_closed():
    src=candidate_fixture().replace("historyDialog()", "historyDialogTwo()").replace(OLD_QUERY,"    val q=query.trim()")
    with pytest.raises(ValueError,match="ANDROID_(PATCH_ANCHOR_MISMATCH|HISTORY_EDITOR_QUERY_NOT_FOUND)"):
        patch_candidate(src,expected_sha=hashlib.sha256(src.encode()).hexdigest())


def test_patch_idempotence_is_rejected_by_baseline_sha():
    result=transformed()
    with pytest.raises(ValueError,match="ANDROID_SOURCE_PARITY_FAILED"):
        patch_candidate(result)
