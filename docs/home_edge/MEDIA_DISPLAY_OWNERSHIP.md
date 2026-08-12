# Home Edge media display ownership guard

`home-edge-media-display-owner status` is the canonical read-only integration boundary between Home Edge foreground media/display state and visual screensavers such as Lavalamp.

It has no media mutation capability and no persistent state store. It observes only fixed local sources:

- Skeleton Cast mode at `http://127.0.0.1:8100/api/mode`;
- Skeleton Cast player state at the fixed loopback `/api/player` endpoint for desktop video modes;
- Android target `dumpsys media_session` through fixed `adb` argv when `SKELETON_HOME_EDGE_ANDROID_SERIAL` has been privately resolved by deployment from canonical device identity.

The Android serial is never printed. Invalid or absent serial configuration is `UNKNOWN`, not an inferred device.

## Exit contract

- exit `0`: `OWNER` — a confirmed video/foreground display owner must preempt the visual saver;
- exit `1`: `CLEAR` — the relevant authoritative source explicitly reports no active display-owning playback;
- exit `2`: `UNKNOWN` — stale, missing, malformed or ambiguous state; visual saver must fail closed and stay hidden.

Stdout is one public-safe JSON object with schema `skeleton.home_edge.media_display_ownership.v1`. It contains only state/reason classes and source class names. Titles, URLs, account identifiers, device serials, session identifiers and raw probe output are never emitted.

## Normalization

Current Skeleton Cast modes are normalized without a second authority:

- `mpv`, `vlc`, `chrome`, `kiosk`: require an explicit bounded playback marker from `/api/player`;
- `youtube`, `airscreen`: require the Android media-session observer; PLAYING/BUFFERING/CONNECTING owns the display, PAUSED/STOPPED/ENDED clears it;
- generic `android`: explicit non-playing is clear, but playing is `UNKNOWN` unless video ownership is independently confirmed, so audio-only playback is never falsely declared a video owner;
- `games`: foreground display owner;
- `off`: clear;
- unknown/new mode: unknown.

A confirmed owner always wins over saver ownership. Ambiguity never causes the visual saver to cover a potentially active video.

## Install and rollback

Source-only helpers:

```sh
scripts/install_home_edge_media_display_owner.sh
scripts/rollback_home_edge_media_display_owner.sh
```

The installer copies the module to `~/.local/lib/skeleton/home_edge/` and the fixed CLI to `~/.local/bin/home-edge-media-display-owner`. It does not use sudo, change playback, change volume/source/mode, install packages, edit networking, or expose private values.

Actual Home Edge activation remains a separate audited canonical `home_edge_exec` operation. Deployment must privately resolve the Android target identity before setting `SKELETON_HOME_EDGE_ANDROID_SERIAL`; do not infer it from an IP alone.
