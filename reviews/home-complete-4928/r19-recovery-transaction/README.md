# R19 transaction core — source review only

This completes a bounded transaction core, not the runtime adapter or automatic recovery activation. No network, threads, timers, or runtime startup are created by importing it.

The transaction admits two cache-starvation samples, prepares a detached exact-origin resolver job outside the mutation guard, stages load paused, validates preserved identity/position/track state before resuming, and requires both time and decoded-frame progress before a revision-conditional commit. Every mutation uses the reviewed expected_revision fence. Resolve failures consume the two-attempt session budget with a 60-second cooldown; admission is bounded to 128 sessions. Untrusted resolver MPV options are ignored; a reviewed adapter can supply only transport headers/referrer/user-agent.

Validation on 2026-11-18: 23 transaction tests, 102 retained backend tests, and 26 intent-fence tests pass in sequential bounded runs. These are synthetic source tests; no real HLS or phone acceptance is claimed.

Integration still required: authoritative fresh snapshot mapping; receiver session and mode lock; actual resolver timeout enforcement and transport mapping; revision-conditional persistence and source-id transition; existing watchdog invocation with no second executor. Process-local retry state requires review of restart behavior. Do not copy the rejected DE-PC admission prototype or replace runtime from old public main. Current runtime and APK are unchanged; full release gate remains blocked.

## Current selector and bounded resolver integration — 2026-11-18

The transaction now accepts the actual app's authoritative `job_id` (the old fixture's `id` is only a fallback). Four new integration cases reproduce the previous all-ineligible failure and then pass with the real current selector: successful load/verify/commit, unrelated catalog ID, wrong voice, and an operator pause during preparation.

`patch_refresh_resolver.py` changes only the current `_refresh_stale_source` signature and its resolver call. Without injection, normal retries are unchanged. With injection, `prepare_recovery_source` uses the same exact-origin/season/episode/voice selector with `persist=False`, backed by a resolver subprocess with a maximum 12-second deadline. It kills the owned process group on timeout, suppresses raw resolver error output, and imports only the sibling resolver, never the application/watchers. No account, API-key fallback, daemon, or second coding executor is involved.

Fresh validation: 11 bounded-resolver tests + 4 selector/transaction tests + 23 retained transaction tests pass together (38); all 102 backend tests pass on the patched isolated app. A real synthetic sleeping child and its descendant are killed in the timeout test; this does not claim real HLS acceptance. Initial test-fixture quoting, missing baseline/schema fixtures and a /proc reaper race were corrected and retained privately as failed attempts.

Reproduction requires the existing private current-source layout, never old public main:

1. Copy the reviewed current app (SHA256 `70af15e405e506d6bc4de3d92e545850fdf034aa31cc059bf5885de376106a38`) to `app.before.py` and `work/backend/app.py`.
2. Retain the existing monitor work/tests plus sibling `baseline/backend` and `context/schema.json`. Retain source-refresh review's `test_source_refresh_preservation.py.fixture` as `work/tests/test_source_refresh_preservation.py` and `refresh_helper_snapshot.json` beside it. These are already reviewed elsewhere in this PR.
3. Apply `python3 patch_refresh_resolver.py work/backend/app.py`.
4. With `PYTHONPATH=.:work/tests:work/backend`, run `python3 -m unittest test_selector_transaction test_hls_recovery_transaction test_bounded_hls_resolver -v`. Run the retained backend suite from `work` with its backend on PYTHONPATH.

Full automatic recovery is still disabled. Snapshot/selected-track remapping, source-ID transition, revision-conditional durable commit, independent real MPV progress evidence and existing watchdog invocation remain integration requirements. Neither the optional injection nor the transaction is deployed. Do not publish a partial APK as the completed package.
