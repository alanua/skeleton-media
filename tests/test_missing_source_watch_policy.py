from __future__ import annotations

from dataclasses import replace
import pytest

from skeleton_media.cast.missing_source_watch_policy import (
    WEEK_SECONDS,
    WatchWork,
    VisibilityDecision,
    plan_rechecks,
    next_due_utc,
    retry_backoff_seconds,
    reconcile_visibility,
)

NOW = 1_800_000_000
K = "tmdb:movie:542178"


def work(**kwargs):
    return replace(WatchWork(work_key=K, last_checked_utc=NOW), **kwargs)


def test_seven_day_cadence_not_hourly_or_daily():
    w = work()
    assert next_due_utc(w) == NOW + 7 * 24 * 60 * 60
    assert plan_rechecks([w], now_utc=NOW + WEEK_SECONDS - 1).work_keys == ()
    assert plan_rechecks([w], now_utc=NOW + WEEK_SECONDS).work_keys == (K,)


def test_first_unchecked_pending_work_due_immediately():
    w = work(last_checked_utc=None)
    assert next_due_utc(w) == 0
    assert plan_rechecks([w], now_utc=NOW).work_keys == (K,)


def test_verified_full_needs_no_missing_source_recheck():
    assert next_due_utc(work(verified_full=True)) is None


def test_deleted_work_is_never_scheduled():
    assert next_due_utc(work(deleted=True)) is None


def test_manually_hidden_is_not_scheduled():
    assert next_due_utc(work(manual_hidden=True)) is None


def test_per_work_off_disables_recheck():
    assert next_due_utc(work(watch_enabled=False)) is None


def test_global_off_disables_all_rechecks():
    plan = plan_rechecks([work(last_checked_utc=None)], now_utc=NOW, global_enabled=False)
    assert plan.work_keys == ()
    assert plan.provider_call_budget == 0


def test_exact_due_order_uses_timestamp_then_canonical_key():
    rows = [
        WatchWork("tmdb:movie:4", last_checked_utc=NOW-10),
        WatchWork("tmdb:movie:2", last_checked_utc=NOW-20),
        WatchWork("tmdb:movie:3", last_checked_utc=NOW-10),
    ]
    result = plan_rechecks(rows[::-1], now_utc=NOW+WEEK_SECONDS)
    assert result.work_keys == ("tmdb:movie:2", "tmdb:movie:3", "tmdb:movie:4")
    assert plan_rechecks(rows, now_utc=NOW+WEEK_SECONDS).work_keys == result.work_keys


def test_bounded_work_and_provider_budgets():
    rows = [
        WatchWork(f"tmdb:movie:{i+1}", last_checked_utc=None)
        for i in range(1000)
    ]
    result = plan_rechecks(rows, now_utc=NOW, max_works=12, max_provider_calls=5)
    assert len(result.work_keys) == 5
    assert result.provider_call_budget == 5
    assert result.eligible_count == 1000
    assert result.deferred_count == 995


def test_bounded_zero_budget_fails_closed():
    rows = [work(last_checked_utc=None)]
    assert plan_rechecks(rows, now_utc=NOW, max_works=0).work_keys == ()
    assert plan_rechecks(rows, now_utc=NOW, max_provider_calls=0).work_keys == ()


@pytest.mark.parametrize("bad", [
    -1, True, 1.5, 17, "24",
])
def test_invalid_work_budget_rejected(bad):
    with pytest.raises(ValueError, match="INVALID_PLAN_LIMITS"):
        plan_rechecks([work()], now_utc=NOW, max_works=bad)


@pytest.mark.parametrize("bad", [
    -1, True, 33, "2",
])
def test_invalid_provider_budget_rejected(bad):
    with pytest.raises(ValueError, match="INVALID_PLAN_LIMITS"):
        plan_rechecks([work()], now_utc=NOW, max_provider_calls=bad)


@pytest.mark.parametrize("bad", [-1, True, "123", 2**54])
def test_invalid_time_fails_closed(bad):
    with pytest.raises(ValueError, match="INVALID_PLAN_LIMITS"):
        plan_rechecks([work()], now_utc=bad)


def test_duplicate_tmdb_works_all_quarantined():
    a = work(last_checked_utc=None)
    b = work(last_checked_utc=NOW - WEEK_SECONDS)
    result = plan_rechecks([a, b], now_utc=NOW)
    assert result.work_keys == ()
    assert result.eligible_count == 0


def test_bad_identity_rejected_not_canonical():
    rows = [
        WatchWork("tv/291507", last_checked_utc=None),
        WatchWork("tmdb:movie:0", last_checked_utc=None),
        WatchWork("imdb:tt123", last_checked_utc=None),
        WatchWork("tmdb:tv:291507", last_checked_utc=None),
    ]
    assert plan_rechecks(rows, now_utc=NOW).work_keys == ("tmdb:tv:291507",)


def test_invalid_negative_check_time_excluded():
    assert plan_rechecks([work(last_checked_utc=-30)], now_utc=NOW).work_keys == ()


def test_int_boolean_time_not_accepted():
    assert next_due_utc(work(last_checked_utc=True)) is None


def test_403_has_six_hour_minimum_backoff():
    assert retry_backoff_seconds("cloudflare_403", 1) == 6 * 3600


def test_timeout_has_one_hour_minimum_backoff():
    assert retry_backoff_seconds("timeout", 1) == 3600


def test_retry_backoff_is_capped_at_one_week():
    assert retry_backoff_seconds("cloudflare_403", 500) == WEEK_SECONDS
    assert retry_backoff_seconds("timeout", 500) == WEEK_SECONDS


@pytest.mark.parametrize("kind,count", [("bad", 1), ("timeout", 0), ("timeout", -1), ("timeout", True), ("timeout", 1001)])
def test_invalid_retry_config_rejected(kind, count):
    with pytest.raises(ValueError, match="INVALID_RETRY_POLICY"):
        retry_backoff_seconds(kind, count)


def test_rate_limit_can_delay_a_due_item():
    record = work(
        last_checked_utc=NOW - WEEK_SECONDS,
        last_failed_utc=NOW - 3600,
        consecutive_failures=2,
        failure_kind="rate_limited",
    )
    assert plan_rechecks([record], now_utc=NOW).work_keys == ()
    assert next_due_utc(record) == NOW - 3600 + 12 * 3600


def test_missing_failure_details_fails_closed():
    record = work(last_checked_utc=None, last_failed_utc=NOW, consecutive_failures=1)
    assert next_due_utc(record) is None


def test_future_eligible_count_and_next_due_timestamp():
    record = work()
    result = plan_rechecks([record], now_utc=NOW)
    assert result.eligible_count == 1
    assert result.deferred_count == 0
    assert result.next_due_utc == NOW + WEEK_SECONDS


def test_visibility_auto_hide_when_verified_missing():
    result = reconcile_visibility(work(auto_suppressed=False), outcome="missing_verified")
    assert result == VisibilityDecision(True, True, False, "full_source_missing")


def test_verified_full_auto_reveals_only_auto_hidden():
    result = reconcile_visibility(work(auto_suppressed=True), outcome="full_verified")
    assert result == VisibilityDecision(False, False, True, "verified_full")


def test_verified_full_does_not_claim_restored_when_already_visible():
    result = reconcile_visibility(work(auto_suppressed=False), outcome="full_verified")
    assert result == VisibilityDecision(False, False, False, "verified_full")


def test_manually_hidden_cannot_be_auto_revealed():
    result = reconcile_visibility(work(manual_hidden=True), outcome="full_verified")
    assert result.hidden is True
    assert result.restored_from_auto_suppression is False
    assert result.reason == "manual_hidden"


def test_deleted_tombstone_can_never_be_resurrected():
    result = reconcile_visibility(work(deleted=True), outcome="full_verified")
    assert result.hidden is True
    assert result.restored_from_auto_suppression is False
    assert result.reason == "deleted"


def test_unknown_or_transient_scan_preserves_prior_hidden():
    assert reconcile_visibility(work(auto_suppressed=True), outcome="indeterminate").hidden is True
    assert reconcile_visibility(work(auto_suppressed=False), outcome="indeterminate").hidden is False


def test_invalid_outcome_is_not_treated_as_full():
    with pytest.raises(ValueError, match="INVALID_VERIFICATION"):
        reconcile_visibility(work(), outcome="trailer_claimed_full")


def test_no_provider_network_or_playback_mutation():
    current_playback = {"mode": "YouTube", "playing": True, "position": 123}
    record = work(last_checked_utc=None)
    before = (current_playback.copy(), record)
    plan_rechecks([record], now_utc=NOW)
    reconcile_visibility(record, outcome="indeterminate")
    assert (current_playback, record) == before


def test_repeat_scheduling_idempotent_without_external_write():
    record = work(last_checked_utc=None)
    one = plan_rechecks([record], now_utc=NOW)
    two = plan_rechecks([record], now_utc=NOW)
    assert one == two


def test_pending_suppression_is_not_playback_verification():
    record = work(auto_suppressed=True, verified_full=False)
    assert reconcile_visibility(record, outcome="indeterminate").hidden is True
    assert record.verified_full is False


def test_large_work_input_budget_failure():
    records = [WatchWork(f"tmdb:tv:{i+1}", last_checked_utc=None) for i in range(20_001)]
    with pytest.raises(ValueError, match="WORK_BUDGET_EXCEEDED"):
        plan_rechecks(records, now_utc=NOW)
