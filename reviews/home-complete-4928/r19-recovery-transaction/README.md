# R19 transaction core — source review only

This completes a bounded transaction core, not the runtime adapter or automatic recovery activation. No network, threads, timers, or runtime startup are created by importing it.

The transaction admits two cache-starvation samples, prepares a detached exact-origin resolver job outside the mutation guard, stages load paused, validates preserved identity/position/track state before resuming, and requires both time and decoded-frame progress before a revision-conditional commit. Every mutation uses the reviewed expected_revision fence. Resolve failures consume the two-attempt session budget with a 60-second cooldown; admission is bounded to 128 sessions. Untrusted resolver MPV options are ignored; a reviewed adapter can supply only transport headers/referrer/user-agent.

Validation on 2026-11-18: 23 transaction tests, 102 retained backend tests, and 26 intent-fence tests pass in sequential bounded runs. These are synthetic source tests; no real HLS or phone acceptance is claimed.

Integration still required: authoritative fresh snapshot mapping; receiver session and mode lock; actual resolver timeout enforcement and transport mapping; revision-conditional persistence and source-id transition; existing watchdog invocation with no second executor. Process-local retry state requires review of restart behavior. Do not copy the rejected DE-PC admission prototype or replace runtime from old public main. Current runtime and APK are unchanged; full release gate remains blocked.
