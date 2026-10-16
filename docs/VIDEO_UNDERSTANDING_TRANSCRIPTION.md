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
