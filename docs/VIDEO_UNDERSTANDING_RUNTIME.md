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

## Public YouTube 403 bounded-range recovery (source-only)

The **existing** `YtDlpAdapter` can attempt an alternate media transfer
only after the standard provider command fails with `SOURCE_MEDIA_FAILED`,
and only for a publicly available, non-live, non-DRM YouTube video.
This does not register a new Skeleton operation, worker, queue, or database.
It never accepts downloader URLs, custom HTTP headers, cookies, filenames,
external scripts, or bypass settings as chat inputs.

The fallback reads an already advertised progressive MP4 format with both
audio and video at 144–480p. Every request is HTTPS to a validated
`googlevideo.com` subdomain, resolved to public/global addresses with
IP-pinned TLS and no redirects. Each chunk must return HTTP 206 and exact
consistent `Content-Range`, size and media type. The default read size is
1 MiB; the entire response is capped at 256 MiB (and the configured
smaller download limit), with a 180-second wall clock ceiling (and the
configured smaller subprocess timeout). The private workspace uses exclusive
creation and atomic promotion; failure removes the incomplete file.
Credential-protected, age-restricted and live content are never routed to
this fallback.

This is a best-effort transfer for normal publicly accessible bytes,
not DRM or access-control circumvention. HTTP 403 can still be returned.
A PASS requires **complete verified bytes** followed by normal
`ffprobe`/frame/OCR/transcript and manifest checks on disposable media.
Fetching just a 206 range is **not proof** of a successful download.
The earlier operator-approved source fixture completed timed subtitles and
320x180 storyboard OCR; its high-resolution stream remained blocked.
Real Home Edge rollout, MemoryGateway writes and global MCP/Gate activation
require their separate approvals.

### Fixed public Android metadata fallback

The operator-approved real-video scratch pilot showed the standard web client
URL served initial ranges but then returned 403. The same public, non-DRM,
non-live clip transferred fully through the **fixed** yt-dlp Android metadata
profile and the existing strict range downloader: 60,482,449 bytes, H.264+AAC,
640x360, 41 actual decoded frames, 31 distinct evidence frames, 868 timed
Ukrainian caption cues (97.26% coverage), and 28 OCR-nonempty evidence frames.
Original video and scratch source code were removed after SHA manifest
verification. This does **not** assert a deployed global operation.

On YouTube `SOURCE_MEDIA_FAILED`, `YtDlpAdapter` now fetches one additional
metadata document using fixed `youtube:player_client=android` and compares
the opaque source video IDs with the original metadata before bounded
Googlevideo download. It does not accept arbitrary extractor options or user
credentials; identity mismatch is a hard block. Public operation semantics,
legacy DIOS runtime, MemoryGateway storage, Gate/Runner and production TV
remain unchanged. This new source path must receive exact-head CI and
operator-controlled deploy review before Home Edge activation.
