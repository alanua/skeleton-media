R19 prerequisite: preserve operator intent while a recovery resolves or loads.

The in-process revision is invalidated at play/stop/control entry and for every mutation, before waiting on the existing MPV wire. Reads keep their existing independent transport and never wait behind the mutation fence. A recovery mutation requires the captured revision and nonbusy state; stale/busy work never transmits. An operator command arriving during a recovery command invalidates any subsequent recovery step and runs after that wire command, so a stale recovery cannot unpause the user's pause.

26 tests pass, including actual current player command/control function extraction and mocked Unix-socket I/O. Current socket wire body is unchanged. All unrelated player functions and the operation bodies are preserved; play/stop/control gain the intent decorator. Exact source patch is idempotent and rejects drift.

SOURCE ONLY. This prerequisite does not implement HLS re-resolution, load, cooldown, source/track preservation or physical progress verification, and is not deployed. Current runtime/app/APK remain unchanged. Next: bind real HLS recovery preparation and every commit/unpause to this revision plus existing mode/session/seek/source identity. The full R19 recovery remains open.

MPV loadfile is asynchronous, so its acknowledgement is not playback verification: https://mpv.io/manual/stable/#command-interface-loadfile
