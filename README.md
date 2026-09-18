# Skeleton Media

Skeleton Media is the media subsystem extracted from alanua/Skeleton.

It provides the Home Edge media runtime, source resolution and search, MPV/TV-mode orchestration, IPTV support, Android remote/client source, adaptive remote controls, media state, release monitoring, and video-understanding components.

## Repository boundary

alanua/Skeleton remains the model-neutral control plane: approvals, registered executors, audit, memory routing, and generic Home Edge contracts.

alanua/skeleton-media owns media-domain source code and media-specific clients. It must not contain credentials, private device registries, user documents, production state, cached media, or signing keys.

## Current status

Source extraction is complete. Public-safe source import is complete. Git history is preserved for migrated Skeleton media paths. Production Home Edge cutover is pending canonical audited deployment.

The currently running Home Edge installation is intentionally not modified by this repository migration. Runtime cutover must use Skeleton's registered Home Edge execution path and independent post-deployment verification.

## Layout

- ops/skeleton_cast/ — Home Edge media service/deployment source and web UI.
- skeleton_media/home_edge/ — media-specific Home Edge modules.
- skeleton_media/video_understanding/ — video understanding pipeline/runtime.
- clients/android-home-native/ — current public-safe Android native client source.
- scripts/ — media and video-understanding entrypoints/install helpers.
- schemas/ — media/video receipt and request schemas.
- tests/ — regression coverage.
- docs/ — architecture and runtime documentation.

## Development

Python 3.11+ is supported.

Create a virtual environment and install this repository with the dev extra. Some integration tests also use Skeleton control-plane contracts; CI installs the pinned compatible Skeleton revision.

Host-specific values must be supplied through environment/configuration rather than committed topology. See MIGRATION.md and SECURITY.md before deploying.

## License

Apache License 2.0. See LICENSE.
