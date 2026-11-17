# Release monitor identity and capability / R18

Exact patches for current private app.py and media_release_monitor.py. They do not replace live source with old public main. The scan predicate previously let unrelated tmdb_id0 works match and let matching UID ignore media type. Red/green fixtures reproduce both on the baseline; the patched scan requires TV identity and tolerates malformed history IDs.

Server returns capability/reason and distinct409/504/500 responses. Unsupported provider series do not become fake TMDB works or implicitly opt in. Existing explicit opt-in, FK ownership, TMDB episode flow and notification claim/ack path are tested; notification claim is conditional, future dates cannot produce playable alerts, and lookup failures retain state for retry.

Validation:19 focused Python tests pass independently on DE-PC and Edge. Android companion compiled with80 JVM tests. Existing signed174 and live monitor files are unchanged. Full R18 is source_partial: an official provider episode metadata producer plus verified stable provider identity contract remains missing; no new schema pretending to supply it is accepted.

Reproduction fixture: in a private isolated test directory arrange work/backend/{app.py,media_release_monitor.py}, baseline/backend/app.py (original snapshot), context/schema.json (sanitized DDL fixture), work/tests/test_release_monitor.py (copy the provided .py.fixture and rename to .py). Apply patch_monitor.py to work/backend, then run python3 -m unittest discover -s work/tests -p test_release_monitor.py. All imports/network/device state are stubbed or temporary SQLite. Do not point fixtures at production.

No daemon, new schedule, external provider API, device operation or publication is added. Source-only review for Skeleton#4821/#4928.
