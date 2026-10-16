# Skeleton Video Understanding

`Skeleton Video Understanding` is the universal video-analysis core. DIOS is one profile, not the owner of the generic pipeline.

## Stable operations

- `video_understand_url`
- `video_import_urls`
- `video_process_one`
- `video_status`
- `video_doctor`
- `video_query`
- `video_reprocess`
- `video_attach_to_project`

Operation inputs contain semantic intent only. Executable paths, shell commands, `ffmpeg`/`yt-dlp` arguments, output paths, cookies, credentials and browser selectors remain private fixed runtime configuration.

## Processing modes

- `QUICK`: metadata and subtitles, with deterministic fallback when the local language model is unavailable.
- `STANDARD`: transcript, selected frames, OCR and local-LLM synthesis.
- `DEEP`: workflows, entities, claims, contradictions, evidence and project mapping.
- `TARGETED`: evidence-grounded answer to one question.
- `ARCHIVE`: verified source/evidence pack without requiring LLM synthesis.

## Local AI roles

```text
Sona
→ local timestamped speech transcription

Ollama
→ ABOUT / STRUCTURE / METHOD / ENTITIES / CLAIMS
→ VISUAL_EVIDENCE / TIMESTAMPS / ACTIONS / CONFLICTS / CONFIDENCE
```

The local LLM is used for synthesis only after deterministic processing has produced bounded evidence. It is required for `STANDARD`, `DEEP` and `TARGETED`, optional for `QUICK`, and not required for `ARCHIVE`.

Ollama can be reached in two private runtime topologies:

```text
loopback
worker and Ollama on the same node → native /api/chat

private_bridge
HomeEdge worker → Skeleton-controlled private executor → server Ollama
```

The private bridge accepts only the bounded normalized synthesis packet. It does not accept an endpoint, shell command, executable path, cookies, arbitrary headers or raw media. Protected bridge registration is a separate deployment task.

The LLM is not trusted to classify URL safety, choose commands, calculate hashes, verify artifacts, perform canonical writes or promote knowledge. Cloud fallback is forbidden. Inferred facts must remain explicitly inferred, and visual claims require linked frame evidence.

## Domain routing

The core ranks DIOS, Home Automation, Travel, Construction, Legal/Documents, Aviation, Skeleton Architecture and General Knowledge. An explicit user profile can select a profile, but original classifier candidates and evidence are retained.

## Private runtime order

```text
source acquisition
→ transcript/subtitle quality gate
→ optional Sona fallback
→ frames + OCR
→ Ollama understanding
→ artifact manifest/readback
→ MemoryGateway canonical mutation
→ optional projection
```

The provider layer uses fixed private configuration for `yt-dlp`, `ffmpeg`, `ffprobe`, Sona and OCR. Commands are argv-only with bounded timeout/output and owned process groups. YouTube/Vimeo use fixed yt-dlp profiles; direct media requires an allowlisted HTTPS host, pinned global DNS result, bounded redirects, content type and size; local files use opaque registry identities.

The file queue supports atomic enqueue/claim, leases, heartbeat, retry, quarantine and expired-lease recovery. It is operational state only, not a canonical database. The artifact store promotes immutable results only after manifest/hash/readback verification.

Projection failure never rolls back a successful canonical commit.

## Memory and review

Large media stays in private artifact storage. The adapter constructs the current private MemoryGateway request envelope:

```text
schema: skeleton.memory_gateway.request.v1
namespace: skeleton
command: skeleton.memory.private_mutate
payload.schema: skeleton.private_memory_gateway.mutation.v1
payload.operation: put
payload.dataset_id: video_understanding
```

Video modules do not import SQLite and do not create a second canonical database.

Knowledge transitions require explicit review:

```text
PROCESSED → UNDERSTOOD → PROJECT_LINKED → HUMAN_REVIEWED
→ ACCEPTED_REUSABLE → PROMOTED
```

Acceptance and promotion require separate human approvals.

## DIOS compatibility

The installed DIOS runtime, corpus and queue remain untouched until an operator-approved migration. Compatibility mapping is:

```text
dios_video_doctor      → video_doctor(profile=DIOS)
dios_video_status      → video_status(profile=DIOS)
dios_video_process_one → video_process_one(profile=DIOS)
```

## Current source boundary

The source implementation contains the universal core, local provider contracts, queue, worker, artifact storage, Sona/Ollama transports and synthetic tests. It does not register protected operations, deploy to HomeEdge, process a real URL, enable a worker, import the DIOS corpus or merge itself.


## Global Skeleton capability and ancillary/branch invariants

This repository **owns the single universal Video Understanding implementation**.
The Python entrypoint `skeleton_media.video_understanding.operations.shared_capability_inventory()`
publishes a deterministic public-safe **source contract** for the existing eight
typed operations. It advertises `ALL_AUTHORIZED_ANCILLARIES_AND_CHAT_BRANCHES`:
all authorized Skeleton ancillaries and chat branches use the same operations,
queue/worker contract and MemoryGateway canonical store, rather than copying the
video pipeline or maintaining per-ancillary indexes.

DIOS is a compatibility profile (`dios_video_*` aliases), not a second video
backend. Profiles influence evidence interpretation and project routing, but
not identity, Skeleton Gate rights or access to the canonical storage.

**Source contract is not deployment.** Until the canonical Skeleton Control
Gate explicitly enrolls and enables the external media provider, the manifest
returns `registration_status=CONTROL_PLANE_ATTESTATION_REQUIRED` and does **not** assert runtime activation.
A green source test is not evidence that operations are already callable from
other ChatGPT branches. Never change protected Skeleton registries, start
workers, or migrate the DIOS corpus while publishing this source-only contract.

Protected follow-up: enroll the eight operations once in the Skeleton Control
registry/MCP authority, bind authenticated calls to the one external
`skeleton-media` runtime, validate cross-ancillary scoped access, public-safe
receipts, one-worker concurrency and MemoryGateway readback on a disposable
synthetic fixture. Real-video Home Edge cutover and legacy DIOS preservation
require separate exact-SHA operator approval; the current media playback path
must remain untouched.
