# Skeleton Video Understanding

Skeleton Video Understanding is a universal private-first capability. DIOS is one domain profile, not the core.

## Purpose

The capability turns a supported URL or approved local-media identity into timestamped, reviewable knowledge:

```text
source
→ metadata
→ subtitles or local ASR
→ frames/scenes/OCR
→ transcript-frame alignment
→ local multimodal understanding
→ domain routing
→ private artifact manifest
→ MemoryGateway mutation
→ human review
→ optional reusable-knowledge promotion
```

Automatic canon promotion is forbidden.

## Modes

- `QUICK`: metadata, subtitles and deterministic short summary. Local LLM is optional.
- `STANDARD`: transcript, scene frames, OCR and local-LLM synthesis.
- `DEEP`: workflows, entities, claims, conflicts, timestamped evidence and project mapping.
- `TARGETED`: answer one bounded user question from video evidence.
- `ARCHIVE`: preserve the verified source/evidence pack; local LLM is not required.

## Local AI roles

The runtime uses separate local providers:

```text
Sona
→ local timestamped speech transcription

Ollama
→ ABOUT / STRUCTURE / METHOD / ENTITIES / CLAIMS
→ VISUAL_EVIDENCE / TIMESTAMPS / ACTIONS / CONFLICTS / CONFIDENCE
```

Ollama is accessed only through a fixed loopback HTTP endpoint from private runtime configuration. The default provider contract uses the native non-streaming `/api/chat` endpoint with JSON output. No cloud endpoint or fallback is permitted.

The local LLM is not authoritative. It cannot:

- download media;
- choose arbitrary commands, executable paths or arguments;
- write directly to SQLite;
- bypass MemoryGateway;
- mark inferred claims as visually confirmed;
- promote knowledge to reusable or canon state;
- override human review.

Deterministic code remains responsible for source safety, URL classification, hashes, artifact verification, timestamps, evidence identity, queue state, idempotency and canonical mutation envelopes.

## Operations

```text
video_understand_url
video_import_urls
video_process_one
video_status
video_doctor
video_query
video_reprocess
video_attach_to_project
```

Operation payloads accept semantic inputs only. Shell commands, ffmpeg/yt-dlp arguments, output paths, selectors, model paths, cookies and credentials are rejected.

## Domain profiles

- DIOS
- Home Automation
- Travel
- Construction
- Legal/Documents
- Aviation
- Skeleton Architecture
- General Knowledge

A user-selected profile is recorded as an override. Original classifier candidates and evidence remain in the private record.

## Private record

A complete private `VideoRecord` contains source identity, processing revision, domain candidates, ABOUT, STRUCTURE, workflows, topics, entities, claims, timestamped evidence, actions, conflicts, project links, review state, transcript artifacts, frame evidence and the verified artifact-manifest hash.

Large media, audio and frame files remain in private artifact storage. MemoryGateway stores structured identity, relations and searchable knowledge.

## Memory authority

The only normal mutation contract is:

```text
command: skeleton.memory.private_mutate
operation: put
private_mode: true
dataset: video_understanding
```

The fact key is stable for `video_record_id + processing_revision`. The idempotency key is stable for `source identity + manifest hash + processing revision`.

The video module does not import `sqlite3`, accept a database path or create a second canonical database. Projection failures are recorded separately from the canonical mutation status.

## Artifact manifest

Manifest entries use relative identities and record private SHA-256, byte size, media type, producer and processing revision. Absolute paths, traversal, backslashes and duplicate identities are rejected. The manifest hash is deterministic.

`ARCHIVE` retains verified source media. Other modes delete temporary source media only after artifact finalization and readback verification.

## Public output

Public receipts contain only operation, aggregate status/reason, mode, allowed domain class, artifact/evidence counts, review-required status and canonical/projection status categories.

They do not contain URL, video ID, title, channel, transcript, evidence text, project identity, local path, frame identity or raw hash.

## DIOS compatibility

The existing DIOS runtime remains intact until a separately reviewed migration:

```text
dios_video_doctor
→ video_doctor(profile=DIOS)

dios_video_status
→ video_status(profile=DIOS)

dios_video_process_one
→ video_process_one(profile=DIOS)
```

No existing private DIOS queue, corpus or artifact is changed by Phase A.

## Phase A boundary

This source phase defines and tests models, safety classification, routing, manifests, MemoryGateway envelopes, local Ollama synthesis contracts and operation plans. It performs no real network/media/model execution, live MemoryGateway mutation, HomeEdge installation or service activation.
