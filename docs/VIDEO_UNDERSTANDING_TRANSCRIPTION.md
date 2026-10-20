# Reliable Video Transcription — source-only v1 (DRAFT)

Owner: `alanua/skeleton-media` existing `video_understanding` package.
Operator-selected priority: **transcription first**. No new service, database, worker, globally registered capability, production changes, or MemoryGateway mutation in this change.

## Scope

- Existing `parse_transcript` and `assess_quality` remain source-of-truth for timed subtitle cues and structural/time coverage checks.
- The pipeline continues storing `transcript/normalized.jsonl` and a copy of original subtitles as private immutable evidence.
- A new derived `transcript/readable.jsonl` removes only exact, nearby multiword suffix/prefix overlaps caused by sliding-window subtitle generation, without replacing model identifiers, digits, or phrases with guessed alternatives.
- `transcript/verification.json` explicitly states `word_accuracy_status=UNVERIFIED` and `independent_asr=NOT_RUN`. The status `GOOD` in `quality.json` concerns structural/temporal coverage; it **never** attests that transcribed words match the audio.
- `readable.jsonl` retains timestamp and original language/provider references; original cues are **never** modified.
- Conservative deduplication resets at a language/provider change or a gap greater than 3 seconds. Single-word overlaps are deliberately retained rather than risking deletion of intentional repetition.

## Next acceptance gates (NOT implemented by this PR)

1. Add bounded independent local Sona/Whisper ASR when audio, hardware and an approved local service are actually available. Only audio evidence may justify corrections of names, model numbers or dates. Disagreement produces a review marker, not a hallucinated correction.
2. Audit out-of-media duration subtitle cues and preservation of original time provenance in adjacent media issue #62.
3. Add a transcript-only externally invoked operation that does not require downstream LLM, vision, canonical MemoryGateway writes or global registration, with exact approved Gate and runtime integration.
4. Validate on operator-approved **private** fixtures on the Home Edge executor. Publish **aggregate status only** to GitHub.
5. Independently test deployment and post-test unchanged TV playback and DIOS queue before operator-approved promotion.

## Private/permission boundary

User URL, title, raw captions, generated readable text, local model data, video, and filesystem paths stay private; only synthetic regression text is committed. No service, Worker, Runner, Gate, Windows Ollama, Remote Desktop Commander, or deployment change in this PR. Do not merge or deploy from green unit tests alone.

Known hardware (private audit; do not expose host/IP identifiers in public source): the currently reachable Windows fallback node has a local text LLM installation but its API is inactive and the checked environment had no working Whisper/Sona command. **A text LLM is not an ASR model.** Do not block transcript extraction on that LLM or count it as audio verification.

## Private independent ASR bridge (stacked source-only draft)

An existing `SonaBackend` can already accept a requester callable. The optional
`PrivateAsrBridgeRequester` implements that callable contract with strict local
WAV file containment, a byte cap, output cap, nonempty operator approval
reference, and a trusted injected executor. This is a *transport protocol*,
not a live service, installer, task runner, remote SSH login or capability
registration. The executor itself MUST enforce the canonical Skeleton Gate,
request identity, privacy and all platform admission checks; a mere
`operator_approval_ref` string is NOT an authorization bypass.

Request shape (PRIVATE only): `skeleton.video_understanding.private_asr_bridge_request.v1`,
`operator_approval_ref`, `model`, `audio_wav` bytes, `timeout_seconds`,
`max_output_bytes`. Response shape: `language`, `segments` (timed speech
text); extraneous executor fields are discarded. Never log, stringify,
publish or copy the private packet into source control. The old loopback Sona
requester remains the default; explicit trusted runtime injection is required.

`compare_independent_asr` checks two segment streams and records
`AGREE_UNVERIFIED` on agreement or `REVIEW_REQUIRED` on mismatches,
especially technical numeric mentions, text differences and language
mismatches. A ratio between subtitle and ASR token sequences is NOT a
word-error rate, speech accuracy metric, or ground truth. It must never
automatically replace subtitle content, and model-name/date/price
corrections require independent evidence.

Pending separate operator-reviewed runtime steps: authorize the DE-PC executor
through the established Gate, make SonaBridge injection part of one shared
media worker, add a transcript-only operation that does not call vision,
semantic LLM or MemoryGateway, test offline workstation and timeout behavior,
and validate private audio scratch cleanup. **No activation from this draft.**

## Transcript-only pipeline path (further stacked source-only draft)

VideoPipeline.transcribe_only(source, approval_ref, mode=QUICK,
verify_with_independent_asr=False) uses the SAME existing intake,
transcript parser, SonaBackend, and PrivateArtifactStore. It does not create
a second queue, worker, DB or external executor. It intentionally does not
call frame extraction/OCR, local LLM, canonical MemoryGateway, or projections.
QUICK and STANDARD only; ARCHIVE and other modes fail closed.

Private result files: original source cues, normalized.jsonl, readable.jsonl,
quality.json, verification.json, and (when audio+independent ASR available)
independent_asr.jsonl and independent_comparison.json. Temporary media/audio
must not survive finalization. Replay uses the existing immutable, SHA-256
readback-verified PrivateArtifactStore, with a distinct tr_ namespace and
a mode/verification-specific identity to avoid full-understanding collisions.

Public-safe receipt contains only counts, structural coverage/verification
statuses and NOT_USED markers for MemoryGateway, LLM and vision; never source
URL, title, transcript content, audio, hashes or private paths. Status
DONE_TRANSCRIPT means only that a structurally usable transcript was safely
recorded. It DOES NOT mean its words were independently accurate.

An outer Gate approval is mandatory for a live invocation. The approval_ref
parameter validates presence but does NOT authorize itself. Live runtime
registration and any changes to deployed services remain prohibited until
separate explicit human approval and tested canonical Gate wiring.

## Typed transcript-only ingress proposal (source-only, NOT REGISTERED)

`plan_operation("video_transcribe_url", {"url": ..., "mode": "QUICK",
"verify_with_independent_asr": false})` is a synthetic-safe plan for
calling the already-merged `VideoPipeline.transcribe_only()` method.
It does not run the video URL, download media, run ASR, alter the queue,
or configure a new capability. Modes QUICK and STANDARD only.

This plan deliberately states `operator_gate_required=true`,
`runtime_registered=false`, `executor_policy=EXISTING_SHARED_MEDIA_WORKER_ONLY`,
`canonical_mutation=NOT_USED`, `local_llm_policy=NOT_USED`,
`network_execution=false`, and `media_execution=false`. The public
receipt is sanitized and contains no URL, content or private path.
The payload does not accept an operator-approval reference, arbitrary
backend command, credential or output path: a trusted Gate must inject
caller identity and enforce authorization independently.

**This source contract DOES NOT publish a ninth operation in Skeleton Core**
or make the module usable in ChatGPT. Protected Skeleton #4788 must
explicitly decide whether to expose this ninth typed action in the
same global provider as the existing eight full video operations;
it must not be treated as a separate service, queue or MemoryGateway.
Before runtime enablement, verify exactly one authorized executor,
private payload transport/response, Home Edge non-playback interference,
offline-ASR fail closed, queue authority, provider pin freshness,
and no private media or transcript values in public receipts.
