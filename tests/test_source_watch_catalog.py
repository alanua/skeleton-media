from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from skeleton_media.cast.source_watch_catalog import (
    WatchStateError,
    SourceGuard,
    ClaimedWork,
    ensure_schema,
    enroll_verified_missing,
    plan_from_catalog,
    claim_due,
    complete_claim,
)
from skeleton_media.cast.missing_source_watch_policy import WEEK_SECONDS

NOW = 1_800_000_000
UID = "MED-TEST-A"
UID2 = "MED-TEST-B"
LEASE = "synthetic_lease_token_a_123456789012345"
OTHER_LEASE = "synthetic_lease_token_b_987654321012345"
GUARD = SourceGuard(manual_hidden=False, deleted=False, verified_full=False)


def database(path=":memory:", *, foreign_keys=True, watched=True):
    c = sqlite3.connect(path, timeout=2)
    if foreign_keys:
        c.execute("PRAGMA foreign_keys=ON")
    c.execute("""
        CREATE TABLE IF NOT EXISTS media_items(
            media_uid TEXT PRIMARY KEY,
            source_url TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            original_title TEXT,
            release_year INTEGER,
            tmdb_id INTEGER,
            imdb_id TEXT,
            poster TEXT,
            overview TEXT,
            metadata_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS discoveries(
            discovery_id TEXT PRIMARY KEY,
            details_json TEXT NOT NULL
        )
    """)
    c.commit()
    if watched:
        ensure_schema(c)
    return c


def add(c, uid=UID, kind="movie", ident=542178):
    c.execute("""
        INSERT INTO media_items(
            media_uid,source_url,provider,media_type,title,metadata_json,updated_at
            ,tmdb_id
        ) VALUES(?,?,?,?,?,?,?,?)
    """, (uid, f"https://example.invalid/tmdb/{uid}", "tmdb", kind,
          "Synthetic title", '{"example":"no source media"}',
          "2026-11-23T00:00:00Z", ident))
    c.commit()


def register(c, uid=UID, kind="movie", ident=542178, **kw):
    return enroll_verified_missing(
        c, media_uid=uid, media_type=kind, tmdb_id=ident,
        now_utc=NOW, independently_verified_missing=True, **kw,
    )


def guards(**updates):
    return {UID: replace(GUARD, **updates)}


def plan(c, *, supplied=None, now=NOW, **opts):
    return plan_from_catalog(
        c, guards=guards() if supplied is None else supplied,
        now_utc=now, **opts,
    )


def claim(c, *, supplied=None, now=NOW, lease=LEASE, **kwargs):
    return claim_due(
        c, guards=guards() if supplied is None else supplied, now_utc=now,
        lease_id=lease, **kwargs,
    )


def test_schema_in_canonical_connection_and_no_new_database_file():
    c = database()
    names = [x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "media_items" in names and "media_source_watches" in names
    assert "discoveries" in names
    assert len([n for n in names if n == "media_source_watches"]) == 1


def test_schema_migration_idempotent():
    c = database()
    ensure_schema(c)
    ensure_schema(c)
    assert c.execute("SELECT count(*) FROM media_source_watches").fetchone()[0] == 0


def test_schema_requires_foreign_key_boundary():
    c = database(foreign_keys=False, watched=False)
    with pytest.raises(WatchStateError, match="CANONICAL_DATABASE_REQUIRED"):
        ensure_schema(c)


def test_schema_refuses_missing_media_items_schema():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(WatchStateError, match="CANONICAL_DATABASE_REQUIRED"):
        ensure_schema(c)


def test_enroll_exact_canonical_id_and_missing_verified_gate():
    c = database()
    add(c)
    assert register(c) is True
    row = c.execute("""
        SELECT media_uid,tmdb_id,media_type,watch_enabled,auto_suppressed,
               last_checked_utc
        FROM media_source_watches
    """).fetchone()
    assert row == (UID, 542178, "movie", 1, 1, None)


def test_enroll_does_not_modify_existing_catalog_or_discoveries():
    c = database()
    add(c)
    c.execute("INSERT INTO discoveries VALUES ('synthetic','{\"data\":1}')")
    c.commit()
    original = c.execute("SELECT * FROM media_items").fetchall()
    discovered = c.execute("SELECT * FROM discoveries").fetchall()
    register(c)
    assert c.execute("SELECT * FROM media_items").fetchall() == original
    assert c.execute("SELECT * FROM discoveries").fetchall() == discovered


def test_missing_verification_is_required_not_inferred_from_metadata():
    c = database()
    add(c)
    with pytest.raises(WatchStateError, match="ENROLLMENT_UNVERIFIED"):
        enroll_verified_missing(
            c, media_uid=UID, media_type="movie", tmdb_id=542178,
            now_utc=NOW, independently_verified_missing=False,
        )
    assert c.execute("SELECT count(*) FROM media_source_watches").fetchone()[0] == 0


@pytest.mark.parametrize("uid,kind,ident", [
    (UID, "tv", 542178),
    (UID, "movie", 99999),
    (UID2, "movie", 542178),
    (UID, "movie", 0),
    (UID, "movie", True),
    ("../../escape", "movie", 542178),
])
def test_invalid_or_mismatched_enrollment_fails_closed(uid, kind, ident):
    c = database()
    add(c)
    with pytest.raises(WatchStateError):
        register(c, uid=uid, kind=kind, ident=ident)
    assert c.execute("SELECT count(*) FROM media_source_watches").fetchone()[0] == 0


def test_colliding_tmdb_identity_blocks_enrollment():
    c = database()
    add(c)
    add(c, uid=UID2, ident=542178)
    with pytest.raises(WatchStateError, match="CANONICAL_IDENTITY_AMBIGUOUS"):
        register(c)


def test_enroll_is_idempotent_and_retains_existing_checked_state():
    c = database()
    add(c)
    assert register(c) is True
    c.execute("UPDATE media_source_watches SET last_checked_utc=?", (NOW,))
    c.commit()
    assert register(c) is False
    assert c.execute("SELECT last_checked_utc FROM media_source_watches").fetchone()[0] == NOW


def test_unwatched_catalog_items_do_not_become_due_automatically():
    c = database()
    add(c)
    assert plan(c).work_keys == ()


def test_missing_guards_quarantine_work():
    c = database()
    add(c)
    register(c)
    assert plan(c, supplied={}).work_keys == ()


def test_new_registered_work_is_due_with_exact_tmdb_key():
    c = database()
    add(c)
    register(c)
    result = plan(c)
    assert result.work_keys == ("tmdb:movie:542178",)
    assert result.eligible_count == 1
    assert result.provider_call_budget == 1


@pytest.mark.parametrize("guard", [
    SourceGuard(True, False, False),
    SourceGuard(False, True, False),
    SourceGuard(False, False, True),
])
def test_manual_hidden_deleted_or_verified_full_not_rechecked(guard):
    c = database()
    add(c)
    register(c)
    assert plan(c, supplied={UID: guard}).work_keys == ()


def test_global_off_preserves_rows_and_disables_plan():
    c = database()
    add(c)
    register(c)
    assert plan(c, global_enabled=False).work_keys == ()
    assert c.execute("SELECT count(*) FROM media_source_watches").fetchone()[0] == 1


def test_per_work_disable_preserves_record_and_blocks_plan():
    c = database()
    add(c)
    register(c)
    c.execute("UPDATE media_source_watches SET watch_enabled=0")
    c.commit()
    assert plan(c).work_keys == ()


def test_pending_claim_has_lease_and_stops_duplicate_execution():
    c = database()
    add(c)
    register(c)
    first = claim(c)
    assert len(first) == 1
    assert first[0].work_key == "tmdb:movie:542178"
    assert first[0].revision == 1
    assert claim(c, lease=OTHER_LEASE) == ()
    assert c.execute("SELECT revision FROM media_source_watches").fetchone()[0] == 1


def test_atomic_lease_blocks_other_connection(tmp_path):
    path = str(tmp_path / "existing_canonical.sqlite3")
    c1 = database(path)
    add(c1)
    register(c1)
    c2 = sqlite3.connect(path, timeout=2)
    c2.execute("PRAGMA foreign_keys=ON")
    first = claim(c1)
    assert len(first) == 1
    assert claim(c2, lease=OTHER_LEASE) == ()
    c2.close()
    c1.close()


def test_lease_expiry_allows_reclaim_but_old_receipt_is_rejected():
    c = database()
    add(c)
    register(c)
    a = claim(c, lease_seconds=5)[0]
    b = claim(c, now=NOW+6, lease=OTHER_LEASE)[0]
    assert b.revision == a.revision+1
    with pytest.raises(WatchStateError, match="COMPLETION_ABORTED"):
        complete_claim(c, claimed=a, lease_id=LEASE,
                       now_utc=NOW+6, outcome="missing_verified")


def test_successful_missing_record_advances_due_by_seven_days():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    complete_claim(c, claimed=receipt, lease_id=LEASE,
                   now_utc=NOW+10, outcome="missing_verified")
    assert plan(c, now=NOW+WEEK_SECONDS).work_keys == ()
    assert plan(c, now=NOW+WEEK_SECONDS+10).work_keys == ("tmdb:movie:542178",)
    assert c.execute("""
        SELECT last_checked_utc,auto_suppressed,revision,lease_hash
        FROM media_source_watches
    """).fetchone() == (NOW+10, 1, 2, None)


def test_success_replay_rejected():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    complete_claim(c, claimed=receipt, lease_id=LEASE,
                   now_utc=NOW+10, outcome="missing_verified")
    with pytest.raises(WatchStateError, match="COMPLETION_ABORTED"):
        complete_claim(c, claimed=receipt, lease_id=LEASE,
                       now_utc=NOW+11, outcome="missing_verified")


def test_winning_claim_does_not_change_video_playback_or_visibility_prefs():
    playback = {"mode": "YouTube", "position": 120.5}
    prefs = {"manual_hidden": True, "deleted": False}
    c = database()
    add(c)
    register(c)
    claim(c, supplied={UID: SourceGuard(True, False, False)})
    assert playback == {"mode": "YouTube", "position": 120.5}
    assert prefs == {"manual_hidden": True, "deleted": False}


def test_wrong_lease_token_cannot_complete():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    with pytest.raises(WatchStateError, match="COMPLETION_ABORTED"):
        complete_claim(c, claimed=receipt, lease_id=OTHER_LEASE,
                       now_utc=NOW+1, outcome="missing_verified")


def test_expired_lease_cannot_complete():
    c = database()
    add(c)
    register(c)
    receipt = claim(c, lease_seconds=2)[0]
    with pytest.raises(WatchStateError, match="COMPLETION_ABORTED"):
        complete_claim(c, claimed=receipt, lease_id=LEASE,
                       now_utc=NOW+3, outcome="missing_verified")


def test_full_verified_outcome_prohibited_before_av_gate():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    with pytest.raises(WatchStateError, match="INVALID_CLAIM_RESULT"):
        complete_claim(c, claimed=receipt, lease_id=LEASE,
                       now_utc=NOW+1, outcome="full_verified")


@pytest.mark.parametrize("failure", [
    "cloudflare_403", "timeout", "rate_limited", "other_transient"
])
def test_failed_provider_attempt_has_bounded_retry_and_no_source_change(failure):
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    complete_claim(c, claimed=receipt, lease_id=LEASE, now_utc=NOW+1,
                   outcome="transient_failure", failure_kind=failure)
    state = c.execute("""
        SELECT last_checked_utc,last_failed_utc,failure_kind,
               consecutive_failures,auto_suppressed
        FROM media_source_watches
    """).fetchone()
    assert state == (None, NOW+1, failure, 1, 1)
    assert plan(c, now=NOW+1).work_keys == ()


def test_failure_does_not_advance_full_seven_day_successful_check():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    complete_claim(c, claimed=receipt, lease_id=LEASE, now_utc=NOW+1,
                   outcome="transient_failure", failure_kind="timeout")
    assert c.execute("SELECT last_checked_utc FROM media_source_watches").fetchone()[0] is None
    assert plan(c, now=NOW+3602).work_keys == ("tmdb:movie:542178",)


def test_failure_recorded_without_error_payload_or_signed_link():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    complete_claim(c, claimed=receipt, lease_id=LEASE, now_utc=NOW+1,
                   outcome="transient_failure", failure_kind="cloudflare_403")
    cols=[r[1] for r in c.execute("PRAGMA table_info(media_source_watches)")]
    for forbidden in ("source_url","token","password","account_email","access_token"):
        assert forbidden not in cols


def test_missing_failure_kind_for_transient_rejected():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    with pytest.raises(WatchStateError, match="INVALID_CLAIM_RESULT"):
        complete_claim(c, claimed=receipt, lease_id=LEASE, now_utc=NOW+1,
                       outcome="transient_failure")


def test_missing_outcome_with_error_kind_rejected():
    c = database()
    add(c)
    register(c)
    receipt = claim(c)[0]
    with pytest.raises(WatchStateError, match="INVALID_CLAIM_RESULT"):
        complete_claim(c, claimed=receipt, lease_id=LEASE, now_utc=NOW+1,
                       outcome="missing_verified", failure_kind="timeout")


def test_delete_media_item_cascades_watch_but_does_not_restore_tombstone():
    c = database()
    add(c)
    register(c)
    c.execute("DELETE FROM media_items WHERE media_uid=?", (UID,))
    c.commit()
    assert c.execute("SELECT count(*) FROM media_source_watches").fetchone()[0] == 0
    assert plan(c, supplied={UID: SourceGuard(False, True, False)}).work_keys == ()


def test_changed_canonical_identity_quarantined():
    c = database()
    add(c)
    register(c)
    c.execute("UPDATE media_items SET tmdb_id=12345 WHERE media_uid=?", (UID,))
    c.commit()
    assert plan(c).work_keys == ()
    assert claim(c) == ()


def test_duplicate_canonical_id_after_enroll_quarantined():
    c = database()
    add(c)
    register(c)
    add(c, uid=UID2, ident=542178)
    assert plan(c).work_keys == ()


def test_watch_table_not_yet_migrated_fails_closed():
    c = database(watched=False)
    add(c)
    with pytest.raises(WatchStateError, match="WATCH_READ_FAILED"):
        plan(c)


def test_large_queued_catalog_obeys_max_provider_calls():
    c = database()
    for i in range(1, 121):
        add(c, uid=f"MED-{i}", ident=i)
        register(c, uid=f"MED-{i}", ident=i)
    g = {f"MED-{i}": GUARD for i in range(1, 121)}
    p = plan(c, supplied=g, max_provider_calls=4, max_works=15)
    assert len(p.work_keys) == 4 and p.deferred_count == 116


def test_duplicate_claim_with_same_token_is_still_not_reentrant():
    c = database()
    add(c)
    register(c)
    assert len(claim(c)) == 1
    assert claim(c) == ()


def test_strict_lease_boundaries():
    c = database()
    add(c)
    register(c)
    for kw in ({ "lease_seconds": 0}, {"lease_seconds": 601},
               {"lease_seconds": True}, {"lease": "too-short"}):
        with pytest.raises(WatchStateError, match="INVALID_LEASE"):
            claim(c, **kw)


def test_no_external_active_transaction_committed_by_claim():
    c = database()
    add(c)
    register(c)
    c.execute("UPDATE media_items SET original_title='existing transaction'")
    with pytest.raises(WatchStateError, match="INVALID_LEASE"):
        claim(c)
    c.rollback()


def test_global_watch_off_cannot_acquire_a_lease_even_when_due():
    c = database()
    add(c)
    register(c)
    before = c.execute(
        "SELECT revision,lease_hash FROM media_source_watches"
    ).fetchone()
    assert claim(c, global_enabled=False) == ()
    assert c.execute(
        "SELECT revision,lease_hash FROM media_source_watches"
    ).fetchone() == before


def test_invalid_global_watch_setting_fails_closed():
    c = database()
    add(c)
    register(c)
    with pytest.raises(WatchStateError, match="INVALID_LEASE"):
        claim(c, global_enabled="false")


def test_enrollment_refuses_to_commit_unrelated_caller_transaction():
    c = database()
    add(c)
    c.execute(
        "UPDATE media_items SET title='inflight-uncommitted' WHERE media_uid=?",
        (UID,),
    )
    with pytest.raises(WatchStateError, match="EXTERNAL_TRANSACTION_ACTIVE"):
        register(c)
    assert c.in_transaction is True
    c.rollback()
    assert c.execute(
        "SELECT title FROM media_items WHERE media_uid=?",
        (UID,),
    ).fetchone()[0] == "Synthetic title"
    assert c.execute(
        "SELECT count(*) FROM media_source_watches"
    ).fetchone()[0] == 0
