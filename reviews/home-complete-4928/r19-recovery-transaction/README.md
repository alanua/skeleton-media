# R19 bounded HLS recovery — integrated source review

When an owned VOD MPV session remains paused for cache across two observations, the existing progress monitor can resolve the saved origin, load the exact work/season/episode/voice at the retained position while paused, restore semantic audio/subtitle selections, then resume only under the unchanged user intent. Success requires both video-clock progress and independently observed decoded-image changes before publishing the new source/current/last-VOD state.

Applied to the reviewed production backend on 2026-11-19 through the registered idle-only Home Edge operation. Independent application-state checks confirmed unchanged paused YouTube identity/position, Chrome processes, selected mode and current record, plus exact installed hashes and a healthy new backend PID. Actual production-provider recovery and physical TV/Redmi/Pixel acceptance remain unverified. Do not deploy the rejected admission-only prototype or reconstruct the runtime from old public main.

## Runtime integration

- `mpv_recovery_runtime.py` supplies actual app/player adapters. It uses the existing tv-mode flock, rejects pending mode requests, binds MPV to boot ID/PID/start ticks/owner/executable, normalizes canonical episode labels, and fences both app and player job copies.
- `patch_runtime_wiring.py` installs one callback in the EXISTING progress monitor. It creates no worker, timer or daemon. Normal progress persistence still runs if recovery fails. Mode entry invalidates recovery before its first probe.
- Apply the existing playback-intent patch to the reviewed player and the optional resolver-injection patch to the reviewed app first. Wiring refuses any input except app SHA256 `2d6c9a00d057cb2e1bfaa7012735b7231697f8e0a5d2562c5fbb93129a7698fc` and intent-fenced player `c6197c05b3ebfcc5fc6b91efe7dc3326d9d7ab8804ed2a177bd36d1b40c82236`.
- Wired isolated app SHA256: `529cf5fea68699f4db4fec166d4a6a8185a82609df4039918bc3a88df3b76ffe`. Wired isolated player: `50752d8500a59ed40033b61c72c856e5aaa519e1251c0de76c1d25c1f0557099`.
- Snapshot, resolver worker, transaction, postload adapter, commit adapter, image collector, budget module and reviewed playback-intent module must be installed together. A missing commit gate is refused.
- The bounded resolver imports the existing sibling resolver, never the app or authentication files. Normal retries retain their default resolver and behavior.

## Safety and durability

Two attempts per stable playback session, at least 60 seconds apart, persist in private SQLite across engine reconstruction. Resolver failure consumes a reservation; SQLite errors do not fall back to memory. At 128 admitted sessions the budget fails closed without evicting prior sessions; any future cleanup requires proof those sessions are no longer active. Own source publication does not create a new session budget.

Resolve is bounded to 12 seconds, initial load readiness to 8 seconds, and adapter commands/observations to a 45-second total deadline. Missing asynchronous MPV properties are pending only during the owned initial load window; they never become synthetic ready state. Mode, process, record, selection, seek and revision changes cancel further recovery actions.

MPV numeric track IDs can change. Restoration requires one exact audio/subtitle descriptor; it excludes asynchronously completed video decoder metadata while retaining every selectable track ID/descriptor/selected flag. MPV's disabled-track boolean is normalized to `no`. Ambiguous or unavailable selections are not guessed.

Publication runs under the user intent gate and mode lock. It adds a source while retaining old rows, then updates current and last-VOD through private atomic writers with readback. It preserves session start/PID/content identity/poster/family metadata and does not call cold play, source-job cleanup or selection-changing UI helpers. Partial publication keeps a valid old source pointer; no rollback overwrites newer user intent.

Decoded-image evidence uses MPV screenshot-to-file with video-only pixels, excluding OSD/subtitles and PNG metadata. Counts mean observed image changes, never total decoded frames or time multiplied by FPS. Private temporary images are deleted. The allowlisted screenshot observation uses the existing raw IPC transport so it cannot increment the user-control revision.

## Validation — 2026-11-19

128 retained/backend/intent tests PASS after app/player wiring. 132 recovery tests PASS, including canonical adapter, actual monitor functions, durable files/budget, load-property waiting, user pause/seek/mode cancellation, semantic remapping and failure publication.

`verify_owned_hls_recovery.py` PASS with real MPV 0.40, generated HLS and an owned loopback HTTP fixture: actual cache starvation, paused load, restoration, independent decoded-image progress and durable commit, with PID/session preserved. It uses no external provider or production display. The earlier real MPV decoded-image contract also passed with a generated local file.

The real fixture exposed asynchronous property availability, disabled-track booleans and video metadata changes that synthetic tests missed. Those failures and the test harness corrections remain in private evidence. This is not physical TV, real provider, phone-cast or Android UI acceptance.

Reproduction uses the existing reviewed private app/player fixtures, baseline/context, retained tests and the playback-intent/source-refresh fixtures elsewhere in this PR. From the recovery review directory, run the explicit `test_*.py` recovery modules with the private `work/tests` on PYTHONPATH; run retained tests from `work` with `work/backend` on PYTHONPATH. The existing intent integration fixtures belong under `work/tests`, not this directory's parent. Run the owned HLS verifier separately under a finite deadline.

Full Home release remains blocked on unverified ledger outcomes, including R03/R22/R23 and phone/TV acceptance. The single signed 1.4.58/code174 APK remains private and uninstalled.
