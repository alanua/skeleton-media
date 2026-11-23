"""Existing media-catalog.sqlite3 adapter for the #86 seven-day source watch.

Source-only stage: no worker, HTTP client, scheduler or media-control actions.
The caller owns an existing SQLite connection. This adapter adds ONE table to
that same database after schema review; it never creates another DB or changes
media_items, discoveries, playback history, hidden preferences or tombstones.

Visibility and verified-full evidence MUST come from trusted live canonical
sources supplied by the caller, never inferred from catalog metadata.
There is intentionally no auto-reveal or full_verified completion here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
import sqlite3

from .missing_source_watch_policy import (
    RecheckPlan, WatchWork, plan_rechecks,
)

_UID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_LEASE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_KINDS = frozenset({"movie", "tv"})
_ERRORS = frozenset({"timeout", "cloudflare_403", "rate_limited", "other_transient"})
_MAX_LEASE = 600
_TABLE = "media_source_watches"


class WatchStateError(RuntimeError):
    """Intentionally exposes no SQL details, media names or source links."""


@dataclass(frozen=True, slots=True)
class SourceGuard:
    """Authoritative, fresh caller snapshot; never cached by this adapter."""
    manual_hidden: bool
    deleted: bool
    verified_full: bool


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    work_key: str
    media_uid: str
    revision: int
    expires_at_utc: int


def _utc(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**53


def _check_database(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection):
        raise WatchStateError("INVALID_DATABASE")
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        fields = {v[1] for v in conn.execute("PRAGMA table_info(media_items)")}
    except sqlite3.Error:
        raise WatchStateError("DATABASE_UNAVAILABLE") from None
    if not fk or fk[0] != 1 or not {
        "media_uid", "media_type", "tmdb_id", "source_url", "metadata_json",
    }.issubset(fields):
        raise WatchStateError("CANONICAL_DATABASE_REQUIRED")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add one state table to existing canonical DB; never run against a file path.

    This schema migration is for reviewed, approved deployment only; calling
    ensure_schema on the real Home DB is forbidden before separate approval.
    """
    _check_database(conn)
    if conn.in_transaction:
        raise WatchStateError("EXTERNAL_TRANSACTION_ACTIVE")
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS media_source_watches (
                    media_uid TEXT PRIMARY KEY NOT NULL
                      REFERENCES media_items(media_uid) ON DELETE CASCADE,
                    media_type TEXT NOT NULL CHECK(media_type IN ('movie','tv')),
                    tmdb_id INTEGER NOT NULL CHECK(tmdb_id > 0),
                    watch_enabled INTEGER NOT NULL DEFAULT 1 CHECK(watch_enabled IN (0,1)),
                    auto_suppressed INTEGER NOT NULL DEFAULT 1
                      CHECK(auto_suppressed IN (0,1)),
                    last_checked_utc INTEGER,
                    last_failed_utc INTEGER,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    failure_kind TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    lease_hash TEXT,
                    lease_expires_utc INTEGER,
                    created_at_utc INTEGER NOT NULL,
                    updated_at_utc INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS media_source_watches_due_idx
                ON media_source_watches(watch_enabled,last_checked_utc,lease_expires_utc)
            """)
    except sqlite3.Error:
        raise WatchStateError("SCHEMA_MIGRATION_FAILED") from None


def enroll_verified_missing(
    conn: sqlite3.Connection,
    *,
    media_uid: str,
    media_type: str,
    tmdb_id: int,
    now_utc: int,
    independently_verified_missing: bool,
) -> bool:
    """Idempotently enroll exact canonical item after trusted missing AV evidence.

    Does NOT set hidden preferences, perform source checks or infer missing
    streams from missing metadata. Existing schedules/visibility stay intact.
    """
    _check_database(conn)
    if (
        not isinstance(media_uid, str) or not _UID.fullmatch(media_uid)
        or media_type not in _KINDS
        or type(tmdb_id) is not int or not 1 <= tmdb_id <= 999_999_999_999
        or not _utc(now_utc)
        or independently_verified_missing is not True
    ):
        raise WatchStateError("ENROLLMENT_UNVERIFIED")

    try:
        exact = conn.execute(
            "SELECT COUNT(*) FROM media_items "
            "WHERE media_uid=? AND media_type=? AND tmdb_id=?",
            (media_uid, media_type, tmdb_id),
        ).fetchone()[0]
        identity_count = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE media_type=? AND tmdb_id=?",
            (media_type, tmdb_id),
        ).fetchone()[0]
        if exact != 1 or identity_count != 1:
            raise WatchStateError("CANONICAL_IDENTITY_AMBIGUOUS")
        with conn:
            result = conn.execute("""
                INSERT INTO media_source_watches
                    (media_uid,media_type,tmdb_id,watch_enabled,auto_suppressed,
                     created_at_utc,updated_at_utc)
                VALUES(?,?,?,1,1,?,?)
                ON CONFLICT(media_uid) DO NOTHING
            """, (media_uid, media_type, tmdb_id, now_utc, now_utc))
        return result.rowcount == 1
    except sqlite3.Error:
        raise WatchStateError("WATCH_ENROLLMENT_FAILED") from None


def _candidates(
    conn: sqlite3.Connection,
    *,
    guards: Mapping[str, SourceGuard],
    now_utc: int,
) -> tuple[list[WatchWork], dict[str, str]]:
    if not isinstance(guards, Mapping) or not _utc(now_utc):
        raise WatchStateError("INVALID_GUARDS")
    try:
        # All identities are taken from the existing canonical media_items table.
        identities = conn.execute("""
            SELECT media_type,tmdb_id,COUNT(*) FROM media_items
            WHERE tmdb_id IS NOT NULL GROUP BY media_type,tmdb_id
        """).fetchall()
        distinct = {(str(k), int(i)): int(n) for k, i, n in identities}
        rows = conn.execute("""
            SELECT w.media_uid,w.media_type,w.tmdb_id,w.watch_enabled,
                   w.auto_suppressed,w.last_checked_utc,w.last_failed_utc,
                   w.consecutive_failures,w.failure_kind,w.lease_expires_utc,
                   m.media_uid,m.media_type,m.tmdb_id
            FROM media_source_watches w
            LEFT JOIN media_items m ON m.media_uid=w.media_uid
            ORDER BY w.media_type,w.tmdb_id,w.media_uid
            LIMIT 20001
        """).fetchall()
    except sqlite3.Error:
        raise WatchStateError("WATCH_READ_FAILED") from None
    if len(rows) > 20_000:
        raise WatchStateError("WATCH_READ_BUDGET_EXCEEDED")

    candidates: list[WatchWork] = []
    lookup: dict[str, str] = {}
    for row in rows:
        (uid, kind, tmdb_id, enabled, auto_hidden, last_check, last_failed,
         failures, failure_kind, lease_until, canonical_uid, canonical_kind, canonical_tmdb) = row
        guard = guards.get(uid)
        if (
            not isinstance(uid, str) or not _UID.fullmatch(uid)
            or kind not in _KINDS
            or type(tmdb_id) is not int or tmdb_id <= 0
            or canonical_uid != uid or canonical_kind != kind or canonical_tmdb != tmdb_id
            or distinct.get((kind, tmdb_id)) != 1
            or not isinstance(guard, SourceGuard)
            or not all(type(getattr(guard, x)) is bool for x in
                       ("manual_hidden","deleted","verified_full"))
            or (lease_until is not None and (type(lease_until) is not int or lease_until > now_utc))
            or enabled not in (0, 1) or auto_hidden not in (0, 1)
        ):
            continue
        work_key = f"tmdb:{kind}:{tmdb_id}"
        candidates.append(WatchWork(
            work_key=work_key,
            last_checked_utc=last_check,
            last_failed_utc=last_failed,
            consecutive_failures=failures,
            failure_kind=failure_kind,
            watch_enabled=bool(enabled),
            manual_hidden=guard.manual_hidden,
            deleted=guard.deleted,
            verified_full=guard.verified_full,
            auto_suppressed=bool(auto_hidden),
        ))
        lookup[work_key] = uid
    return candidates, lookup


def plan_from_catalog(
    conn: sqlite3.Connection,
    *,
    guards: Mapping[str, SourceGuard],
    now_utc: int,
    global_enabled: bool = True,
    max_works: int = 12,
    max_provider_calls: int = 24,
) -> RecheckPlan:
    """Read-only. Absent source guards make that work ineligible, not visible."""
    _check_database(conn)
    candidates, _ = _candidates(conn, guards=guards, now_utc=now_utc)
    return plan_rechecks(
        candidates, now_utc=now_utc, global_enabled=global_enabled,
        max_works=max_works, max_provider_calls=max_provider_calls,
    )


def claim_due(
    conn: sqlite3.Connection,
    *,
    guards: Mapping[str, SourceGuard],
    now_utc: int,
    lease_id: str,
    lease_seconds: int = 300,
    max_works: int = 12,
    max_provider_calls: int = 24,
) -> tuple[ClaimedWork, ...]:
    """Atomically reserve bounded due candidates in the SAME canonical DB.

    This is a protocol for a later approved Skeleton executor. No scan runs.
    A lease ID must be independently generated by the trusted caller and kept
    out of logs. This operation never changes video history or hidden flags.
    """
    _check_database(conn)
    if (
        conn.in_transaction
        or not _utc(now_utc)
        or not isinstance(lease_id, str) or not _LEASE.fullmatch(lease_id)
        or type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE
    ):
        raise WatchStateError("INVALID_LEASE")
    lease_hash = hashlib.sha256(lease_id.encode()).hexdigest()
    claimed: list[ClaimedWork] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidates, lookup = _candidates(conn, guards=guards, now_utc=now_utc)
        plan = plan_rechecks(
            candidates, now_utc=now_utc, max_works=max_works,
            max_provider_calls=max_provider_calls,
        )
        for key in plan.work_keys:
            uid = lookup[key]
            result = conn.execute("""
                UPDATE media_source_watches
                SET lease_hash=?,lease_expires_utc=?,revision=revision+1,
                    updated_at_utc=?
                WHERE media_uid=? AND
                      (lease_expires_utc IS NULL OR lease_expires_utc<=?)
            """, (lease_hash, now_utc+lease_seconds, now_utc, uid, now_utc))
            if result.rowcount != 1:
                raise WatchStateError("LEASE_CONFLICT")
            revision = conn.execute(
                "SELECT revision FROM media_source_watches WHERE media_uid=?",
                (uid,),
            ).fetchone()[0]
            claimed.append(ClaimedWork(key, uid, revision, now_utc+lease_seconds))
        conn.commit()
    except (sqlite3.Error, WatchStateError):
        conn.rollback()
        raise WatchStateError("CLAIM_ABORTED") from None
    return tuple(claimed)


def complete_claim(
    conn: sqlite3.Connection,
    *,
    claimed: ClaimedWork,
    lease_id: str,
    now_utc: int,
    outcome: str,
    failure_kind: str | None = None,
) -> None:
    """Record missing/full-source-negative or transient failure only.

    NO full_verified result: actual playable video requires #85 independent
    AV/rights/part verification and a separately reviewed admission action.
    NO hidden preference writes, source URL storage or automatic unhide.
    """
    _check_database(conn)
    if (
        conn.in_transaction or not isinstance(claimed, ClaimedWork)
        or not isinstance(lease_id, str) or not _LEASE.fullmatch(lease_id)
        or not _utc(now_utc) or type(claimed.revision) is not int
        or (outcome == "missing_verified" and failure_kind is not None)
        or (outcome == "transient_failure" and failure_kind not in _ERRORS)
        or outcome not in {"missing_verified", "transient_failure"}
    ):
        raise WatchStateError("INVALID_CLAIM_RESULT")

    lease_hash = hashlib.sha256(lease_id.encode()).hexdigest()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if outcome == "missing_verified":
            sql = """
                UPDATE media_source_watches
                SET last_checked_utc=?,last_failed_utc=NULL,
                    consecutive_failures=0,failure_kind=NULL,
                    auto_suppressed=1,lease_hash=NULL,lease_expires_utc=NULL,
                    revision=revision+1,updated_at_utc=?
                WHERE media_uid=? AND revision=? AND lease_hash=?
                    AND lease_expires_utc>=?
            """
            params = (now_utc, now_utc, claimed.media_uid,
                      claimed.revision, lease_hash, now_utc)
        else:
            sql = """
                UPDATE media_source_watches
                SET last_failed_utc=?,
                    consecutive_failures=MIN(1000,consecutive_failures+1),
                    failure_kind=?,lease_hash=NULL,lease_expires_utc=NULL,
                    revision=revision+1,updated_at_utc=?
                WHERE media_uid=? AND revision=? AND lease_hash=?
                    AND lease_expires_utc>=?
            """
            params = (now_utc, failure_kind, now_utc, claimed.media_uid,
                      claimed.revision, lease_hash, now_utc)
        result = conn.execute(sql, params)
        if result.rowcount != 1:
            raise WatchStateError("STALE_OR_EXPIRED_CLAIM")
        conn.commit()
    except (sqlite3.Error, WatchStateError):
        conn.rollback()
        raise WatchStateError("COMPLETION_ABORTED") from None
