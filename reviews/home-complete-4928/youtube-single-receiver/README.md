# REJECTED for receiver deduplication — rolled back

The operator's 2026-11-18 19:52 phone screenshot still shows **Samsung SmartTV** and **YouTube on TV**, both marked as playing YouTube. At that time the proposed guard was active and its LR_AUTOCONNECT action absent. Therefore this adapter did not achieve the requested deduplication. **Do not deploy it as a fix.**

The exact original manager was restored, the opt-in manifest disabled and the native registrar restored through the registered rollback. Independent postflight preserved the manual screen, preflight pairing identities, same-source active playback, volume, mode and MPV record. No phone pairings, accounts or browser process were reset. Private failure/rollback evidence was persisted.

Follow-up read-only identity comparison shows the actual connected native MDX screen and device IDs equal their existing saved identities. One backend receiver identity is established; the Android picker identifiers for its two labels remain unavailable. Native MobileKey source can build the Samsung SmartTV label, but no initialized registered MobileKey service was found in the bounded existing-object check. This does not prove a replacement cause and does not justify disabling another service. Do not infer cache expiry or repeat the rejected change without new evidence.

---

## Historical experiment (not current runtime)

# Single native YouTube receiver review — Skeleton#4318 / #4928

The operator confirmed both native phone targets connect and requested keeping only **YouTube TV**.

The current manual MDX screen is named YouTube TV. The receiver also initializes a separate LR_AUTOCONNECT cloud handoff registration. Its association with the Samsung SmartTV picker label remains a hypothesis until observed on the phone.

The adapter cancels only that automatic registration and gates only its own service-instance registrar. It preserves the manual screen and existing pairing storage. It pins the current native cancel, registrar and initialize function fingerprints, refuses different native Cast endpoints, bounds cancellation acknowledgement to four seconds, and never retries an uncertain cancellation. Rollback restores the original property descriptor and requests a fresh native registration when one previously existed. A failed/uncertain cancellation retains its local guard and requires inspection or rollback.

The existing connection manager optionally enforces these exact approved bytes through a private opt-in manifest. Missing/disabled/invalid manifests or changed source bytes never execute the policy. The minimal patch preserves all ten pre-existing manager functions, IPC, identity handling and disabled custom DIAL. No second service or executor is created. Disable the manifest before native rollback so the next heartbeat does not reapply the guard.

Validation: 18 isolated JS behavior tests plus 8 manager tests pass. These use synthetic native services and do not establish Android picker acceptance.

Runtime evidence on 2026-11-18: cancellation acknowledged; current service remains unregistered and manager reports manual_only, connected, failures=0. Independent same-source video progressed 6.01 seconds and 181 decoded frames across later heartbeats. Manual screen, immediate preflight pairing identity, mode, paused MPV record and volume compared unchanged. Only the existing connection manager restarted; Chrome and playback did not restart. The fresh preflight contained one Redmi REMOTE_CONTROL entry, compared with two earlier; this operation changed neither count nor storage. Do not claim both earlier phone records were freshly verified.

Still required: refresh the actual native phone picker, observe whether Samsung disappears and YouTube TV remains, reconnect through YouTube TV and verify continuity across natural video transitions. No cold browser restart was forced during active viewing. Persistence is installed and source-verified; cold-start behavior and phone dedup are unverified. No APK publication or full-regression completion.
