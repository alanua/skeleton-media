# Skeleton Video Understanding live runtime

## Authority

The first live runtime is installed on the registered Hetzner Skeleton node.

```text
VideoWorker
→ VideoPipeline
→ local providers
→ Ollama loopback
→ immutable artifact store
→ MemoryGateway
→ canonical private SQLite
```

The runtime refuses to create a canonical database. It requires an already configured `PrivateMemoryStack` containing `canonical.sqlite`. A connector configuration that points at another database filename is not silently converted and does not create a second authority.

HomeEdge is not a canonical-memory host. It may later become a bounded media-execution adapter, but structured results must return to the server MemoryGateway.

## Runtime installation

The installer:

1. verifies a clean exact Git SHA;
2. resolves the existing canonical private-memory root;
3. copies the reviewed source into an immutable private release directory;
4. creates a private Python virtual environment;
5. installs the pinned Pillow dependency;
6. installs the pinned, hash-verified yt-dlp executable;
7. optionally installs the pinned, hash-verified Vibe/Sona package;
8. writes a private runtime configuration with an empty direct-media host allowlist;
9. verifies fixed ffmpeg, ffprobe, OCR and Ollama providers;
10. runs a synthetic MemoryGateway mutation and exact readback;
11. installs and activates exactly one user worker service;
12. restores the previous release and service definitions on failure.

The public installation receipt contains status categories and counts only. It excludes runtime paths, model names, source identities, URLs, transcripts, OCR, frames, hashes, canonical refs and database rows.

## Local providers

Mandatory providers:

- yt-dlp;
- ffmpeg;
- ffprobe;
- local OCR;
- Ollama loopback;
- canonical MemoryGateway storage.

Sona is an optional fallback during initial activation. When its local API is unavailable, the runtime reports `ASR_FALLBACK_BLOCKED` but can process sources with usable subtitles. No cloud ASR fallback is allowed.

## Services

- `skeleton-video-understanding-worker.service`
- `skeleton-video-understanding-sona.service` when the verified sidecar is available

The worker uses a single-instance file lock, durable queue leases, heartbeat, retry and quarantine. Automatic reusable-knowledge or canon promotion is forbidden.

## Protected launch workflow

`.github/workflows/video-understanding-runtime-launch.yml` runs only on exact `main` changes to the runtime implementation or by explicit workflow dispatch. It uses the registered self-hosted Hetzner label, validates the full video test set, restores a clean checkout, installs the exact commit, verifies MemoryGateway readback and confirms one active worker.

## Rollback

Before changing the current release or user units, the installer records the prior release target and service definitions. Any installation, doctor, MemoryGateway or activation failure disables the new services, restores the prior release and units, reloads the user service manager and restarts the prior units when they existed.
