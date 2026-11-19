# R22 persistent opt-in cadence policy — REVIEW

This source implements a bounded, serial cadence transaction with private persistent opt-in configuration. It defaults OFF. No timer, daemon, executor, display command or gateway credential access is introduced.

For an explicitly opted-in, settled, exact owned MPV HLS VOD with at least 30 seconds of independently observed buffered headroom, a bounded second-source packet probe reads real video presentation timestamps. Constant 25 fps selects the EXISTING registered 50 Hz operation; constant 30/60 selects the EXISTING 60 Hz operation. Variable, unknown, unsupported or ambiguous/reversibly unrepresentable display modes leave the display unchanged. No time-times-FPS substitute is accepted.

Private source URLs live in a sealed anonymous descriptor, not process arguments or logs. The child is bounded by elapsed timeout, CPU, memory and output size. Auth/custom headers fail closed; there is no credential fallback. Probes are limited across session changes and reconstruction to one per 60 seconds, including failures and already-matched outcomes. Two mutation attempts per session survive intervening sessions/restarts, with a non-evicting capacity of 128. No probing occurs while disabled, without the canonical action gateway, while paused/buffering/seeking, during transition, on other endpoints/modes, or with uncertain identity/headroom.

Every mutation and rollback requires a separately admitted canonical registered operation and receipt. The same mode flock and exact session/process/revision/source/path/policy generation fence are checked at each boundary. Acknowledgement is insufficient: independent Mutter mode/layout/resolution and actual MPV position advancement must agree. Rollback is refused after newer user/session/display intent.

46 behavioral tests PASS: transaction, real private SQLite/config reconstruction, rate/retry bounds, concurrent tick refusal, config/source change, rollback, user intent, gateway absence and packet/Mutter adapters. The initial integration test found a private-state parent directory permission error; fixed with explicit owned 0700 root creation and all tests rerun successfully.

A real owned loopback HLS/ffprobe contract test PASSED: 32 actual packet PTS from a fixed 25 fps stream classified 25; 50 packet PTS from variable-frame video were rejected. No production source, TV output or refresh mode was touched. The earlier real MPV 0.40 video-pts property observation FAILED because the property is unavailable; that sampler is rejected and is not a runtime dependency.

## Remaining integration boundary

The product integration must supply the existing approved Home Edge gateway object with admission and execution methods; the adapter has NO direct display-helper/subprocess fallback. Installed public wrapper is node-side server-only; the internal client requires signing authority. No credential file was read, no privilege grant was added, and no unsigned route was invented. Until a canonical scoped service admission route is bound, automatic deployment remains BLOCKED, not enabled. Source tests use a synthetic gateway and are not runtime or physical acceptance.

No production policy install, enablement, display change or phone/subjective judder acceptance is claimed. Keep the complete release gate blocked. This work remains REVIEW, not CANON.
