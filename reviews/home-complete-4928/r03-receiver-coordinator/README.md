# R03 serialized receiver handoff — integrated source review

A fresh decoded receiver media-load event can select YouTube directly from any Home mode. The handler checks the actual current receiver clip, persists a replay reservation, invokes only the canonical incoming-preserving controller, then independently verifies mode, Chrome foreground ownership, hidden MPV surface and exact clip before publishing YouTube. It never chooses Video as an intermediate state.

## Event and intent boundaries

All initial native XHR streams are priming data, including multiple historical requests in one batch. Appends to a previously primed stream are distinct from a first-seen request; the latter additionally requires a fresh matching Network request/HTTP200 response proof without disk/service-worker cache. Role, session, sequence, target page, Chrome process start/owner and endpoint are checked. Tokens/raw payloads are never persisted.

A mode generation, user-control revision, receiver epoch, pending transition or stale observation gap resets priming instead of replaying an ambiguously ordered event. The private SQLite replay ledger is reserved before actuation, survives reconstruction and fails closed on errors/capacity; failed handoffs are not retried automatically from the same event. Noops, pairing, warm playback and remembered titles do not trigger it.

The existing native reader can observe Network metadata with post-data capture disabled. It installs no page hooks. Timing -1 is accepted as opaque metadata only, never a seek command; numeric/string playlist indexes retain exact bounds and video selection. This compatibility choice is an inference informed by [the independent receiver implementation](https://github.com/aykevl/plaincast/blob/master/apps/youtube/youtube.go), not a claim of fresh live-phone acceptance.

## Canonical controller and runtime

The incoming controller uses the EXISTING tv-mode flock and validates the original mode generation before each stage. Its wrapper neither writes nor removes another pending mode request. VOD pause failure aborts without killing preserved work; only confirmed live TV is stopped. The existing pause-preserving MPV park helper is reused under the incoming context token. Explicit failure propagation is necessary because Bash errexit is suppressed in functions called through an OR-list.

The receiver's current video is retained. No cold Chrome start, previous-video restore, navigation, playback input, account/pairing edit, volume normalization or HyperHDR change is performed. Existing Chrome is made visible/fullscreen and its receiver page is brought to the front. A successful command acknowledgement alone cannot satisfy the foreground/postcondition check.

The source patch hooks the EXISTING autoplay poll monitor, including when autoplay is disabled or no VOD is active. No additional worker, timer, daemon or executor is created. Receiver failure uses bounded backoff and sanitized/deduplicated private diagnostics. HLS recovery remains in its separate existing progress monitor; accepted incoming handoff invalidates prepared HLS intent.

Exact app input is activated R19 SHA256 `529cf5fea68699f4db4fec166d4a6a8185a82609df4039918bc3a88df3b76ffe`; output is `14244d9c5cc2ad081a980d136a2894d3390e13f3a4dbd71cc9847bb04cb56dc8`. The R19 player remains `50752d8500a59ed40033b61c72c856e5aaa519e1251c0de76c1d25c1f0557099`. Controller/wrapper/module hashes and rollback copies are retained privately; unrelated current source is not replaced from public main.

## Evidence — 2026-11-19

107 receiver/coordinator/controller/runtime/monitor/process/permission tests PASS, including an owned fake controller process, real private SQLite/files, isolated shell helpers, replay/restart, malformed/bootstrap/mode-change cases and failure after each critical boundary. 128 retained backend/intent tests PASS after app wiring. Existing R19 helper/selector bytes are protected.

A 15.27-second NON-ACTUATING real receiver probe decoded the actual native Lounge stream; fresh loads observed: 0. Existing paused YouTube identity/position was preserved. Audit receipt `5c8ca0ad8666873e042fa9c7a91c6440e132c50ca3fe8d0fdc9205254dc8b1b3`. Its synthetic revision0 was observation-only and could not authorize a handoff. It proves the live read contract, not a fresh phone transition.

R03 v1 installed successfully but live observer startup FAILED with connect/PermissionError. The premature no_fresh_load startup statement was withdrawn. Registered rollback restored the working activated R19 app SHA256 `529cf5fea68699f4db4fec166d4a6a8185a82609df4039918bc3a88df3b76ffe`, preserving paused playback; rollback receipt `7c157a7455ca6e4758e1357df06b4b8e29d5dcd6206555c839462c8490c02525`. R03 v1 is inactive; v2 activation is recorded below.

The reviewed v2 source replaces unnecessary ptrace-gated executable resolution with canonical unit PID plus owned comm/boot/start identity. The exact v1 denial site was not captured, so this is a permitted-path repair candidate, not a proven root-cause claim. It adds sanitized code-site diagnostics and stops PermissionError retries until conditions change. No capability, sandbox, root or account change is proposed. 107 receiver tests and 128 retained tests pass on regenerated app `14244d9c5cc2ad081a980d136a2894d3390e13f3a4dbd71cc9847bb04cb56dc8`.

V2 was applied through the registered idle-only operation. The actual existing backend observer independently reports no_fresh_load without the permission error, and the apply operation required that fresh startup postcondition before reporting success. Exact installed app SHA256 is 14244d9c5cc2ad081a980d136a2894d3390e13f3a4dbd71cc9847bb04cb56dc8; player remains 50752d8500a59ed40033b61c72c856e5aaa519e1251c0de76c1d25c1f0557099. Paused Chrome identity/position/processes/mode/current record are preserved. Apply receipt: `945f4440b5c9b9e4dda1b05df3a5bd297803047fce35c7851c03f3b9ae8760c4`. No elevation or sandbox change was made. Any future permission denial halts retries. Actual fresh phone handoff/physical TV/installed Android acceptance and the Samsung SmartTV picker duplicate remain unverified. Shared 1.4.58/code174 stays private/uninstalled and the full release gate remains blocked.
