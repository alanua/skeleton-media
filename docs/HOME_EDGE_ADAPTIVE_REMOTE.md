# Home Edge Adaptive Phone Controls

`core.home_edge.adaptive_remote` is the repository-owned reference contract for
phone controls used by Home Edge. It is offline-only and does not connect to a
TV, Android runtime, browser, player, game, SSH route, or MCP service.

## Stable interfaces

The contract exposes two separate interfaces:

- `multimedia_remote`: portrait physical-style multimedia remote for Android
  family modes, local player, browser, game, off, and unknown.
- `gamepad`: one landscape universal gamepad for every game.

The multimedia profile is selected only from the active Home mode:

| Active mode | Multimedia profile |
| --- | --- |
| `android_tv`, `android_app` | `android_family` |
| `local_player` | `local_player` |
| `browser` | `browser` |
| `game` | `game` |
| `off`, `unknown` | `inactive` |

Same-mode requests are idempotent. Transitions within the Android family are
warm app switches and must not restart the Android runtime. Leaving the gamepad
does not change TV mode; mode changes require an explicit mode request.

## Button contract

The backend broker uses a closed button allowlist with `down`, `up`, and `tap`
phases. Multi-touch holds are tracked per interface, button, and pointer id.
Held buttons are released on blur, visibility loss, pointer cancellation, and a
bounded automatic timeout.

The multimedia remote includes power, mute, refresh, a circular D-pad, OK, back,
home, menu, transport controls, and volume. The universal gamepad includes
D-pad, A/B/X/Y, L/R, Start, Select, library, Android TV, and safe exit.

Only `universal_gamepad` is accepted as a gamepad selector. Title-specific,
game-specific, media-title-specific, and per-game controller branches are outside
the contract.

## Orientation

The reference contract supports a native orientation bridge when available and a
browser CSS rotation fallback when it is not. Both strategies leave the phone
global orientation setting unchanged.

## Validation

Run the offline tests with:

```bash
pytest tests/test_home_edge_adaptive_remote.py
```

These tests render the portrait remote at `390x844`, render the universal
gamepad at `844x390`, verify UI/backend allowlist parity, and assert that no
live TV mode change or input injection path is used.
