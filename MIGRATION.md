# Extraction lineage

Skeleton Media was extracted from alanua/Skeleton on 2026-09-18.

## Public Git lineage

The extraction started from Skeleton main commit 1f3d2d8310c9b35b5be6402eee555e45634c0c2c.

Git history was filtered to media-specific paths before this repository became canonical for those sources. A later extraction commit reorganized reusable Python modules under skeleton_media/.

## Home Edge source reconciliation

A read-only reconciliation was performed against the running Home Edge skeleton-cast source tree.

Imported:
- current media modules such as player, resolver, discovery, IPTV, release monitor, media state and Trakt sync;
- public web UI/static source;
- public-safe Android native client source;
- live resolver regression tests.

Explicitly excluded:
- credentials, OAuth/token files, keys and device-registry contents;
- production state/databases, logs and caches;
- APK build artifacts and signing material;
- scan/downloaded user documents;
- backup/history copies;
- host-specific topology;
- production monolith portions that mix non-media domains.

The running system remains untouched until a separate audited cutover through Skeleton Home Edge.
