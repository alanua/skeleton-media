"""Pure seven-day recheck policy for missing full-playable media sources.

This is NOT a worker, timer, provider search client or secret store.
The caller must use the existing Skeleton scheduler and canonical media DB,
provide authenticated operator settings, and independently verify full audio,
video, rights, content identity, episodes and playable duration.

No I/O and no playback, media or visibility state mutations occur here.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import re

WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_WORKS_PER_RUN = 16
MAX_PROVIDER_CALLS_PER_RUN = 32
MAX_ACCEPTED_RECORDS = 20_000
_WORK_KEY = re.compile(r"^tmdb:(?:movie|tv):[1-9][0-9]{0,11}$")
_OUTCOMES = frozenset({"full_verified", "missing_verified", "indeterminate"})
_BACKOFF_BASE = {
    "timeout": 60 * 60,
    "cloudflare_403": 6 * 60 * 60,
    "rate_limited": 6 * 60 * 60,
    "other_transient": 2 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class WatchWork:
    work_key: str
    last_checked_utc: int | None = None
    last_failed_utc: int | None = None
    consecutive_failures: int = 0
    failure_kind: str | None = None
    watch_enabled: bool = True
    manual_hidden: bool = False
    deleted: bool = False
    verified_full: bool = False
    auto_suppressed: bool = True


@dataclass(frozen=True, slots=True)
class RecheckPlan:
    work_keys: tuple[str, ...]
    eligible_count: int
    deferred_count: int
    next_due_utc: int | None
    provider_call_budget: int


@dataclass(frozen=True, slots=True)
class VisibilityDecision:
    hidden: bool
    auto_suppressed: bool
    restored_from_auto_suppression: bool
    reason: str


def _valid_time(value: object) -> bool:
    return value is None or (type(value) is int and 0 <= value <= 2**53)


def _valid_work(work: object) -> bool:
    return (
        isinstance(work, WatchWork)
        and isinstance(work.work_key, str)
        and bool(_WORK_KEY.fullmatch(work.work_key))
        and _valid_time(work.last_checked_utc)
        and _valid_time(work.last_failed_utc)
        and type(work.consecutive_failures) is int
        and 0 <= work.consecutive_failures <= 1_000
        and (work.failure_kind is None or work.failure_kind in _BACKOFF_BASE)
        and (work.consecutive_failures == 0 or work.last_failed_utc is not None)
        and all(type(getattr(work, name)) is bool for name in (
            "watch_enabled", "manual_hidden", "deleted",
            "verified_full", "auto_suppressed",
        ))
    )


def retry_backoff_seconds(kind: str, failures: int) -> int:
    """Bound transient retry backoff, never start a busy retry loop."""
    if kind not in _BACKOFF_BASE or type(failures) is not int or not 1 <= failures <= 1_000:
        raise ValueError("INVALID_RETRY_POLICY")
    return min(WEEK_SECONDS, _BACKOFF_BASE[kind] * (1 << min(failures - 1, 12)))


def next_due_utc(work: WatchWork) -> int | None:
    """Source rechecks are independent of the TV episode-release monitor."""
    if not _valid_work(work):
        return None
    if work.deleted or work.manual_hidden or not work.watch_enabled or work.verified_full:
        return None

    due = (work.last_checked_utc + WEEK_SECONDS) if work.last_checked_utc is not None else 0
    if work.consecutive_failures:
        if work.failure_kind is None:
            return None
        due = max(
            due,
            work.last_failed_utc + retry_backoff_seconds(
                work.failure_kind, work.consecutive_failures
            ),
        )
    return due


def plan_rechecks(
    works: Iterable[WatchWork],
    *,
    now_utc: int,
    global_enabled: bool = True,
    max_works: int = 12,
    max_provider_calls: int = 24,
) -> RecheckPlan:
    """Plan only, never execute; ambiguous duplicate identities fail closed.

    The eventual executor must enforce time/HTTP/cost budgets, independent
    API authorization, locks and durable leases or audit receipts.
    """
    if (
        type(now_utc) is not int or not 0 <= now_utc <= 2**53
        or type(global_enabled) is not bool
        or type(max_works) is not int or not 0 <= max_works <= MAX_WORKS_PER_RUN
        or type(max_provider_calls) is not int
        or not 0 <= max_provider_calls <= MAX_PROVIDER_CALLS_PER_RUN
    ):
        raise ValueError("INVALID_PLAN_LIMITS")
    if not global_enabled or max_works == 0 or max_provider_calls == 0:
        return RecheckPlan((), 0, 0, None, 0)

    seen: dict[str, WatchWork] = {}
    duplicated: set[str] = set()
    for index, work in enumerate(works):
        if index >= MAX_ACCEPTED_RECORDS:
            raise ValueError("WORK_BUDGET_EXCEEDED")
        if not _valid_work(work):
            continue
        if work.work_key in seen:
            duplicated.add(work.work_key)
        else:
            seen[work.work_key] = work

    due: list[tuple[int, str]] = []
    future: list[int] = []
    for key, work in seen.items():
        if key in duplicated:
            continue
        at = next_due_utc(work)
        if at is None:
            continue
        if at <= now_utc:
            due.append((at, key))
        else:
            future.append(at)
    due.sort(key=lambda item: (item[0], item[1]))
    selected = tuple(key for _, key in due[: min(max_works, max_provider_calls)])
    deferred_count = len(due) - len(selected)
    next_due = min(future) if future else None
    return RecheckPlan(
        selected, len(due) + len(future), deferred_count,
        next_due, min(max_provider_calls, len(selected)),
    )


def reconcile_visibility(work: WatchWork, *, outcome: str) -> VisibilityDecision:
    """Pure policy; never treat incoming provider data as verified directly.

    full_verified is admissible only after external #85 full-media AV,
    duration, rights and exact TMDB/season/episode checks. Unknown results
    preserve current suppression. Manual hide/deletion always wins.
    """
    if not _valid_work(work) or outcome not in _OUTCOMES:
        raise ValueError("INVALID_VERIFICATION")
    if work.deleted:
        return VisibilityDecision(True, work.auto_suppressed, False, "deleted")
    if work.manual_hidden:
        return VisibilityDecision(True, work.auto_suppressed, False, "manual_hidden")
    if outcome == "full_verified":
        return VisibilityDecision(False, False, work.auto_suppressed, "verified_full")
    if outcome == "missing_verified":
        return VisibilityDecision(True, True, False, "full_source_missing")
    return VisibilityDecision(
        work.auto_suppressed, work.auto_suppressed, False, "indeterminate",
    )
