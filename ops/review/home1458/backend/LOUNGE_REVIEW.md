# Incoming YouTube media-load observation (R03 / Skeleton#4972)

Classification: REVIEW / PUBLIC_SAFE. This diagnostic is not an enabled automatic mode switch.

The deployed receiver's heartbeat and DIAL pairing route do not establish a new phone-origin media load. Passive observation found the existing YouTube Lounge receive XHR: HTTPS YouTube `/api/lounge/bc/bind`, explicit `LOUNGE_SCREEN`, `RID=rpc`, a session identifier and numeric sequence watermark. Decimal-length framed records were decoded on the actual receiver. The initial snapshot contained historical commands, including `setPlaylist`; none was accepted as a fresh phone-cast event.

`youtube_lounge_events.py` validates bounded framing, receiver role and monotonic sequence. `setPlaylist`/`setVideo` require a validated media identity. Initial history, partial priming frames, replay, status, pairing and sender requests cannot trigger. Reports contain fixed command counters and known-field types, with no raw URLs, cookies, tokens, session identifiers or media identities.

`youtube_lounge_observer.py` observes newly opened Network requests. Existing long-lived requests require `youtube_lounge_xhr_observer.py`: fixed read functions apply verified native XHR getters, without page hooks, reload, network requests or media input. Handles and debugging connections are released. Discovery and polling are bounded; new requests in an already primed session retain sequence deduplication.

`youtube_lounge_capture.py` runs one 180-second observation and records only a private sanitized report. Its launcher has a single-job lock, process-identity check and independent 200-second timeout. These are diagnostic helpers invoked by the canonical audited Home Edge executor, not a new execution service. They do not alter the physical mode or publish an Android mode event.

Validation: 38 Python tests pass locally and on Home Edge; 6 synthetic native-getter JavaScript tests pass locally. Actual existing-XHR observation decoded 19 records; fresh media load was not observed. Edge receipts: source/tests `72b7ba908e12f0f13a85c9785a3a22cd17ab10a65c4c9183b876c9e73167662a`; actual receiver read `ce0a9edfc507c7f37eb36184e261be3a4e7619168b1acc56f3c14b8f4c03a9cf`.

Remaining: verify a deliberately fresh phone-origin load, then review a handoff that preserves the incoming clip. The normal warm YouTube restore may restore an old URL/position, so it must not be invoked blindly for an incoming load. Verify concurrent explicit mode selection, repeated same-video loads, endpoint separation and actual foreground/UI state. R03 remains unaccepted. Candidate 174 stays private and the shared update channel is unchanged.
