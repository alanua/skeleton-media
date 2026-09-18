# Contributing

Keep changes scoped to the media domain. Generic approval, executor, memory, registry, and audit mechanisms belong in alanua/Skeleton.

Before opening a pull request:
1. keep secrets and private topology out of the patch;
2. add or update focused regression tests;
3. run pytest;
4. state whether runtime/deployment behavior changes;
5. state any required Skeleton control-plane revision;
6. preserve rollback and independent verification for Home Edge changes.

Do not commit generated APKs, caches, media files, private configuration, runtime state, or production user data.
