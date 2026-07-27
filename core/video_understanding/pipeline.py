from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from core.video_understanding.artifact_store import FinalizedArtifact, PrivateArtifactStore
from core.video_understanding.domain_router import DomainRoute, route_domain
from core.video_understanding.intake_adapters import (
    AcquiredSource,
    DirectMediaAdapter,
    LocalFileAdapter,
    SourceMetadata,
    YtDlpAdapter,
)
from core.video_understanding.local_llm import LocalLlmClient, UnderstandingInput, local_llm_policy
from core.video_understanding.memory_gateway_adapter import build_private_mutation
from core.video_understanding.models import (
    Claim,
    Domain,
    Entity,
    FrameEvidence,
    ProcessingMode,
    ProcessingState,
    ReviewDecision,
    SourceReference,
    SupportType,
    TimestampedEvidence,
    Topic,
    TranscriptArtifact,
    VideoRecord,
    VideoUnderstandingError,
    Workflow,
    public_receipt,
)
from core.video_understanding.runtime_config import VideoRuntimeConfig
from core.video_understanding.sona_backend import SonaBackend
from core.video_understanding.subprocess_tools import BoundedCommandRunner
from core.video_understanding.transcript import (
    TranscriptQuality,
    TranscriptSegment,
    assess_quality,
    parse_transcript,
    transcript_to_jsonl,
)
from core.video_understanding.url_classifier import classify_local_reference, classify_remote_url
from core.video_understanding.vision import (
    FrameArtifact,
    build_frame_artifacts,
    extract_frames,
    probe_media,
    select_frame_timestamps,
)

_REQUIRED_SECTIONS = (
    "ABOUT",
    "STRUCTURE",
    "METHOD",
    "ENTITIES",
    "CLAIMS",
    "VISUAL_EVIDENCE",
    "TIMESTAMPS",
    "ACTIONS",
    "CONFLICTS",
    "CONFIDENCE",
)


class UnderstandingProvider(Protocol):
    def understand(self, input_packet: UnderstandingInput) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PipelineResult:
    record: VideoRecord
    mutation_envelope: Mapping[str, Any]
    canonical_result: Mapping[str, Any]
    projection_status: str
    artifact: FinalizedArtifact
    public: Mapping[str, Any]


class PrivateBridgeOllama:
    """Injected narrow bridge to server-side Ollama; no endpoint or command is exposed."""

    def __init__(
        self,
        executor: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        model: str,
        max_input_chars: int = 120_000,
    ) -> None:
        self._executor = executor
        self.model = model
        self.max_input_chars = max_input_chars

    def understand(self, input_packet: UnderstandingInput) -> dict[str, Any]:
        packet = {
            "schema": "skeleton.video_understanding.ollama_bridge_request.v1",
            "model": self.model,
            "mode": ProcessingMode(input_packet.mode).value,
            "question": input_packet.question,
            "domain_hint": input_packet.domain_hint,
            "transcript_segments": list(input_packet.transcript_segments),
            "visual_evidence": list(input_packet.visual_evidence),
            "ocr_evidence": list(input_packet.ocr_evidence),
            "response_contract": list(_REQUIRED_SECTIONS),
        }
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if len(encoded) > self.max_input_chars:
            raise VideoUnderstandingError("LLM_INPUT_TOO_LARGE", "Ollama bridge packet exceeded limit")
        result = self._executor(packet)
        if not isinstance(result, Mapping):
            raise VideoUnderstandingError("OLLAMA_BRIDGE_INVALID", "Ollama bridge result must be an object")
        normalized = dict(result)
        _require_sections(normalized)
        return normalized


class VideoPipeline:
    def __init__(
        self,
        config: VideoRuntimeConfig,
        *,
        processing_revision: str,
        memory_executor: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        projection_executor: Callable[[VideoRecord], Mapping[str, Any]] | None = None,
        runner: BoundedCommandRunner | None = None,
        artifact_store: PrivateArtifactStore | None = None,
        sona: SonaBackend | None = None,
        loopback_llm: LocalLlmClient | None = None,
        bridge_executor: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        yt_adapter: YtDlpAdapter | None = None,
        direct_adapter: DirectMediaAdapter | None = None,
        local_adapter: LocalFileAdapter | None = None,
    ) -> None:
        if not processing_revision or len(processing_revision) > 128:
            raise VideoUnderstandingError("PROCESSING_REVISION_INVALID", "processing revision is invalid")
        self.config = config
        self.processing_revision = processing_revision
        self.memory_executor = memory_executor
        self.projection_executor = projection_executor
        self.runner = runner or BoundedCommandRunner(config)
        self.artifact_store = artifact_store or PrivateArtifactStore(config)
        self.sona = sona or SonaBackend(config)
        self.yt_adapter = yt_adapter or YtDlpAdapter(config, self.runner)
        self.direct_adapter = direct_adapter or DirectMediaAdapter(config)
        self.local_adapter = local_adapter or LocalFileAdapter(config)
        if config.ollama_transport == "loopback":
            if loopback_llm is None:
                raise VideoUnderstandingError("OLLAMA_CLIENT_REQUIRED", "loopback Ollama client is required")
            self.understanding: UnderstandingProvider = loopback_llm
        else:
            if bridge_executor is None:
                raise VideoUnderstandingError("OLLAMA_BRIDGE_REQUIRED", "private Ollama bridge is required")
            self.understanding = PrivateBridgeOllama(bridge_executor, model=config.ollama_model)

    def process(
        self,
        *,
        source: str,
        mode: ProcessingMode | str,
        approval_ref: str,
        question: str | None = None,
        project_hint: str | None = None,
        profile: Domain | str | None = None,
    ) -> PipelineResult:
        mode_value = ProcessingMode(mode)
        classification = classify_local_reference(source) if source.startswith("local-media:") else classify_remote_url(source)
        video_record_id = "vr_" + hashlib.sha256(
            classification.normalized_private_source.encode("utf-8")
        ).hexdigest()[:32]

        with self.artifact_store.workspace(video_record_id, self.processing_revision) as workspace:
            acquired = self._acquire(classification.adapter, source, classification, workspace, mode_value)
            _write_json(workspace / "source.json", _private_source(acquired))
            _write_json(workspace / "metadata.json", _metadata_dict(acquired.metadata))

            segments, quality = self._transcript(acquired, workspace)
            transcript_root = workspace / "transcript"
            transcript_root.mkdir(exist_ok=True)
            (transcript_root / "normalized.jsonl").write_text(transcript_to_jsonl(segments), encoding="utf-8")
            _write_json(transcript_root / "quality.json", quality.__dict__)
            original_root = transcript_root / "original"
            original_root.mkdir(exist_ok=True)
            for index, subtitle_path in enumerate(acquired.subtitle_paths):
                if subtitle_path.exists():
                    suffix = subtitle_path.suffix.lower() or ".txt"
                    target = original_root / f"subtitle-{index:03d}{suffix}"
                    if subtitle_path.resolve() != target.resolve(strict=False):
                        shutil.move(str(subtitle_path), target)

            frames, duration = self._vision(acquired, segments, workspace, mode_value)
            evidence = _build_evidence(segments, frames)
            _write_jsonl(workspace / "evidence.jsonl", [item.__dict__ for item in evidence])
            _write_jsonl(workspace / "ocr.jsonl", [frame.private_dict() for frame in frames])
            _write_jsonl(
                workspace / "scenes.jsonl",
                [
                    {
                        "scene_id": f"scene:{index:06d}",
                        "timestamp_seconds": frame.timestamp_seconds,
                        "frame_id": frame.frame_id,
                    }
                    for index, frame in enumerate(frames)
                ],
            )
            _write_json(
                workspace / "contact_sheets" / "index.json",
                {"status": "NOT_GENERATED", "frame_count": len(frames)},
            )

            route = route_domain(
                _routing_text(acquired.metadata, segments, frames, project_hint, question),
                explicit_profile=profile,
            )
            synthesis, review_reason = self._understand(
                mode_value,
                segments,
                frames,
                evidence,
                question=question,
                domain_hint=route.selected.value,
                duration=duration,
            )
            _write_json(workspace / "summary.json", synthesis)
            (workspace / "summary.md").write_text(
                _summary_markdown(synthesis, route.selected, review_reason), encoding="utf-8"
            )
            _write_json(workspace / "entities.json", synthesis.get("ENTITIES", []))
            _write_jsonl(workspace / "claims.jsonl", _mapping_list(synthesis.get("CLAIMS")))
            _write_json(workspace / "workflows.json", synthesis.get("METHOD", []))
            _write_jsonl(
                workspace / "llm_tasks.jsonl",
                [{"status": "COMPLETE" if review_reason is None else "REVIEW", "reason": review_reason}],
            )

            if acquired.media_path is not None and acquired.temporary_media and mode_value is not ProcessingMode.ARCHIVE:
                acquired.media_path.unlink(missing_ok=True)
            elif acquired.media_path is not None and mode_value is ProcessingMode.ARCHIVE:
                source_root = workspace / "source_media"
                source_root.mkdir(exist_ok=True)
                target = source_root / acquired.media_path.name
                if acquired.media_path != target:
                    shutil.move(str(acquired.media_path), target)

            finalized = self.artifact_store.finalize(
                workspace,
                video_record_id=video_record_id,
                processing_revision=self.processing_revision,
                mode=mode_value,
            )

        record = _build_record(
            video_record_id=video_record_id,
            revision=self.processing_revision,
            mode=mode_value,
            acquired=acquired,
            route=route,
            synthesis=synthesis,
            evidence=evidence,
            segments=segments,
            frames=frames,
            manifest_hash=finalized.manifest_hash,
            review_reason=review_reason,
            transcript_quality=quality,
        )
        mutation = build_private_mutation(record, approval_ref=approval_ref)
        canonical_result = self.memory_executor(mutation)
        canonical_status = str(canonical_result.get("status", "UNKNOWN"))
        if canonical_status not in {"DONE", "COMMITTED", "SUCCESS"}:
            raise VideoUnderstandingError("CANONICAL_MUTATION_FAILED", "MemoryGateway mutation did not commit")

        projection_status = "NOT_CONFIGURED"
        if self.projection_executor is not None:
            try:
                projection = self.projection_executor(record)
                projection_status = str(projection.get("status", "UNKNOWN"))
            except Exception:
                projection_status = "DEGRADED"
        public = public_receipt(
            operation="video_process_one",
            status="DONE" if review_reason is None else "REVIEW_REQUIRED",
            reason_code=review_reason or "VIDEO_UNDERSTOOD",
            mode=mode_value,
            detected_domain=route.selected,
            transcript_count=len(segments),
            frame_count=len(frames),
            ocr_count=sum(1 for frame in frames if frame.ocr_text),
            evidence_count=len(evidence),
            review_required=review_reason is not None,
            canonical_mutation_status="COMMITTED",
            projection_status=projection_status,
        )
        return PipelineResult(record, mutation, canonical_result, projection_status, finalized, public)

    def _acquire(
        self,
        adapter: str,
        raw_source: str,
        classification: Any,
        workspace: Path,
        mode: ProcessingMode,
    ) -> AcquiredSource:
        if adapter in {"youtube", "vimeo"}:
            return self.yt_adapter.acquire(classification, workspace, mode)
        if adapter == "direct_media":
            return self.direct_adapter.acquire(raw_source, workspace, mode)
        if adapter == "local_file":
            return self.local_adapter.acquire(raw_source, workspace, mode)
        raise VideoUnderstandingError("ADAPTER_UNSUPPORTED", "source adapter is unsupported")

    def _transcript(
        self,
        acquired: AcquiredSource,
        workspace: Path,
    ) -> tuple[tuple[TranscriptSegment, ...], TranscriptQuality]:
        candidates: list[TranscriptSegment] = []
        for path in acquired.subtitle_paths:
            try:
                candidates.extend(
                    parse_transcript(path, language=_language_from_name(path.name), provider="source_subtitle")
                )
            except VideoUnderstandingError:
                continue
        segments = tuple(candidates)
        quality = assess_quality(
            segments,
            media_duration_seconds=acquired.metadata.duration_seconds,
            max_chars=self.config.limits.max_transcript_chars,
        )
        if quality.usable or acquired.media_path is None:
            return segments, quality
        audio = self.sona.extract_audio(self.runner, acquired.media_path, workspace)
        try:
            sona_result = self.sona.transcribe(audio)
        finally:
            audio.unlink(missing_ok=True)
        quality = assess_quality(
            sona_result.segments,
            media_duration_seconds=acquired.metadata.duration_seconds,
            max_chars=self.config.limits.max_transcript_chars,
        )
        return sona_result.segments, quality

    def _vision(
        self,
        acquired: AcquiredSource,
        segments: Sequence[TranscriptSegment],
        workspace: Path,
        mode: ProcessingMode,
    ) -> tuple[tuple[FrameArtifact, ...], float | None]:
        if acquired.media_path is None or mode is ProcessingMode.QUICK:
            return (), acquired.metadata.duration_seconds
        probe = probe_media(self.runner, self.config, acquired.media_path, workspace)
        if not probe.has_video:
            return (), probe.duration_seconds
        cues = tuple((segment.start_seconds + segment.end_seconds) / 2 for segment in segments)
        timestamps = select_frame_timestamps(
            probe.duration_seconds,
            mode,
            cue_times=cues,
            max_frames=self.config.limits.max_frames,
        )
        paths = extract_frames(self.runner, self.config, acquired.media_path, workspace, timestamps)
        return build_frame_artifacts(self.runner, self.config, paths, timestamps), probe.duration_seconds

    def _understand(
        self,
        mode: ProcessingMode,
        segments: Sequence[TranscriptSegment],
        frames: Sequence[FrameArtifact],
        evidence: Sequence[TimestampedEvidence],
        *,
        question: str | None,
        domain_hint: str,
        duration: float | None,
    ) -> tuple[dict[str, Any], str | None]:
        policy = local_llm_policy(mode)
        if policy == "NOT_REQUIRED":
            return _deterministic_summary(segments, frames, question), None
        packet = UnderstandingInput(
            mode=mode,
            transcript_segments=tuple(segment.to_dict() for segment in segments),
            visual_evidence=tuple(
                {
                    "evidence_id": _frame_evidence_id(frame),
                    "timestamp_seconds": frame.timestamp_seconds,
                    "frame_id": frame.frame_id,
                }
                for frame in frames
            ),
            ocr_evidence=tuple(
                {
                    "evidence_id": _frame_evidence_id(frame),
                    "timestamp_seconds": frame.timestamp_seconds,
                    "text": frame.ocr_text,
                }
                for frame in frames
                if frame.ocr_text
            ),
            question=question,
            domain_hint=domain_hint,
        )
        try:
            result = self.understanding.understand(packet)
            _validate_synthesis(result, evidence, frames, duration)
            return result, None
        except VideoUnderstandingError as exc:
            fallback = _deterministic_summary(segments, frames, question)
            if policy == "OPTIONAL_WITH_DETERMINISTIC_FALLBACK":
                return fallback, None
            return fallback, exc.reason_code


def _build_record(
    *,
    video_record_id: str,
    revision: str,
    mode: ProcessingMode,
    acquired: AcquiredSource,
    route: DomainRoute,
    synthesis: Mapping[str, Any],
    evidence: Sequence[TimestampedEvidence],
    segments: Sequence[TranscriptSegment],
    frames: Sequence[FrameArtifact],
    manifest_hash: str,
    review_reason: str | None,
    transcript_quality: TranscriptQuality,
) -> VideoRecord:
    claims = tuple(_claim(item, index) for index, item in enumerate(_mapping_list(synthesis.get("CLAIMS"))))
    methods = tuple(_workflow(item, index) for index, item in enumerate(_mapping_list(synthesis.get("METHOD"))))
    entities = tuple(_entity(item) for item in _mapping_list(synthesis.get("ENTITIES")))
    topics = tuple(_topic(item) for item in _mapping_list(synthesis.get("STRUCTURE")))
    transcript_artifacts: tuple[TranscriptArtifact, ...] = ()
    if segments:
        transcript_artifacts = (
            TranscriptArtifact(
                "transcript:normalized",
                segments[0].language,
                segments[0].provider,
                transcript_quality.status,
                len(segments),
                hashlib.sha256(transcript_to_jsonl(segments).encode("utf-8")).hexdigest(),
            ),
        )
    frame_evidence = tuple(
        FrameEvidence(
            frame.frame_id,
            frame.timestamp_seconds,
            "FRAME_OCR" if frame.ocr_text else "FRAME",
            1.0,
            frame.ocr_text or None,
        )
        for frame in frames
    )
    return VideoRecord(
        schema="skeleton.video_understanding.record.v1",
        video_record_id=video_record_id,
        processing_revision=revision,
        state=ProcessingState.REVIEW_REQUIRED if review_reason else ProcessingState.UNDERSTOOD,
        mode=mode,
        source=SourceReference(
            acquired.metadata.source_type,
            acquired.metadata.source_identity,
            acquired.metadata.adapter,
            {"title": acquired.metadata.title, "duration_seconds": acquired.metadata.duration_seconds},
        ),
        detected_domain=route.selected,
        domain_candidates=tuple(candidate.to_dict() for candidate in route.candidates),
        about=dict(synthesis.get("ABOUT") or {}),
        structure=tuple(_mapping_list(synthesis.get("STRUCTURE"))),
        methods=methods,
        topics=topics,
        entities=entities,
        claims=claims,
        evidence=tuple(evidence),
        actions=tuple(_mapping_list(synthesis.get("ACTIONS"))),
        conflicts=tuple(_mapping_list(synthesis.get("CONFLICTS"))),
        project_links=(),
        review=ReviewDecision(
            "REVIEW_REQUIRED" if review_reason else "SYSTEM_UNDERSTOOD",
            "SYSTEM",
            (review_reason,) if review_reason else (),
        ),
        artifact_manifest_hash=manifest_hash,
        transcript_artifacts=transcript_artifacts,
        frame_evidence=frame_evidence,
    )


def _build_evidence(
    segments: Sequence[TranscriptSegment],
    frames: Sequence[FrameArtifact],
) -> tuple[TimestampedEvidence, ...]:
    items: list[TimestampedEvidence] = []
    for index, segment in enumerate(segments):
        items.append(
            TimestampedEvidence(
                f"transcript:{index:06d}",
                segment.start_seconds,
                segment.end_seconds,
                "transcript:normalized",
                "TRANSCRIPT",
                segment.confidence if segment.confidence is not None else 0.8,
                segment.text,
            )
        )
    for frame in frames:
        items.append(
            TimestampedEvidence(
                _frame_evidence_id(frame),
                frame.timestamp_seconds,
                frame.timestamp_seconds,
                frame.frame_id,
                "FRAME_OCR" if frame.ocr_text else "FRAME",
                1.0,
                frame.ocr_text or None,
            )
        )
    return tuple(items)


def _validate_synthesis(
    result: Mapping[str, Any],
    evidence: Sequence[TimestampedEvidence],
    frames: Sequence[FrameArtifact],
    duration: float | None,
) -> None:
    _require_sections(result)
    allowed = {item.evidence_id for item in evidence}
    visual = {_frame_evidence_id(frame) for frame in frames}

    def validate_ids(item: Mapping[str, Any], *, visual_required: bool = False) -> tuple[str, ...]:
        raw = item.get("evidence_ids", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
            raise VideoUnderstandingError("LLM_EVIDENCE_REFERENCE_INVALID", "evidence references are invalid")
        ids = tuple(raw)
        if any(value not in allowed for value in ids):
            raise VideoUnderstandingError("LLM_EVIDENCE_REFERENCE_INVALID", "result references unknown evidence")
        if visual_required and not any(value in visual for value in ids):
            raise VideoUnderstandingError("LLM_VISUAL_EVIDENCE_MISSING", "visual result has no frame evidence")
        return ids

    for claim in _mapping_list(result.get("CLAIMS")):
        support = str(claim.get("support_type", "INFERRED"))
        if support not in {member.value for member in SupportType}:
            raise VideoUnderstandingError("LLM_SUPPORT_TYPE_INVALID", "claim support type is invalid")
        ids = validate_ids(claim, visual_required=support in {"VISUAL_ONLY", "JOINT"})
        if support != "INFERRED" and not ids:
            raise VideoUnderstandingError("LLM_EVIDENCE_REFERENCE_INVALID", "supported claim has no evidence")
    for section in ("METHOD", "ENTITIES", "STRUCTURE", "ACTIONS", "CONFLICTS"):
        for item in _mapping_list(result.get(section)):
            validate_ids(item)
    for item in _mapping_list(result.get("VISUAL_EVIDENCE")):
        ids = list(item.get("evidence_ids", [])) if isinstance(item.get("evidence_ids", []), list) else []
        evidence_id = item.get("evidence_id")
        if evidence_id is not None:
            if not isinstance(evidence_id, str):
                raise VideoUnderstandingError("LLM_EVIDENCE_REFERENCE_INVALID", "visual evidence id is invalid")
            ids.append(evidence_id)
        if not ids or any(value not in visual for value in ids):
            raise VideoUnderstandingError("LLM_VISUAL_EVIDENCE_MISSING", "visual evidence references no frame")
    for item in _mapping_list(result.get("TIMESTAMPS")):
        start, end = item.get("start_seconds"), item.get("end_seconds")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < 0
            or end < start
            or (duration is not None and end > duration + 1)
        ):
            raise VideoUnderstandingError("LLM_TIMESTAMP_INVALID", "LLM timestamp is invalid")
        evidence_id = item.get("evidence_id")
        if evidence_id is not None and evidence_id not in allowed:
            raise VideoUnderstandingError("LLM_EVIDENCE_REFERENCE_INVALID", "timestamp references unknown evidence")


def _require_sections(result: Mapping[str, Any]) -> None:
    if any(section not in result for section in _REQUIRED_SECTIONS):
        raise VideoUnderstandingError("LLM_RESULT_INCOMPLETE", "understanding result is incomplete")


def _deterministic_summary(
    segments: Sequence[TranscriptSegment], frames: Sequence[FrameArtifact], question: str | None
) -> dict[str, Any]:
    excerpt = " ".join(segment.text for segment in segments)[:1200]
    return {
        "ABOUT": {"summary": excerpt, "question": question},
        "STRUCTURE": [],
        "METHOD": [],
        "ENTITIES": [],
        "CLAIMS": [],
        "VISUAL_EVIDENCE": [
            {"evidence_id": _frame_evidence_id(frame), "timestamp_seconds": frame.timestamp_seconds}
            for frame in frames
        ],
        "TIMESTAMPS": [],
        "ACTIONS": [],
        "CONFLICTS": [],
        "CONFIDENCE": {"status": "DETERMINISTIC_FALLBACK"},
    }


def _claim(item: Mapping[str, Any], index: int) -> Claim:
    try:
        support = SupportType(str(item.get("support_type", "INFERRED")))
    except ValueError:
        support = SupportType.INFERRED
    evidence_ids = tuple(str(value) for value in item.get("evidence_ids", []) if isinstance(value, str))
    if support is not SupportType.INFERRED and not evidence_ids:
        support = SupportType.INFERRED
    return Claim(
        str(item.get("claim_id") or f"claim:{index:06d}"),
        str(item.get("text") or "Unspecified claim"),
        support,
        evidence_ids,
        _confidence(item.get("confidence"), 0.5),
    )


def _workflow(item: Mapping[str, Any], index: int) -> Workflow:
    steps = tuple(str(value) for value in item.get("ordered_steps", item.get("steps", [])) if str(value).strip())
    return Workflow(
        str(item.get("workflow_id") or f"workflow:{index:06d}"),
        str(item.get("name") or "Workflow"),
        steps or ("Review workflow evidence",),
        tuple(str(value) for value in item.get("evidence_ids", []) if isinstance(value, str)),
        _confidence(item.get("confidence"), 0.5),
    )


def _entity(item: Mapping[str, Any]) -> Entity:
    return Entity(
        str(item.get("entity_type") or "unknown"),
        str(item.get("name") or "unknown"),
        _confidence(item.get("confidence"), 0.5),
        tuple(str(value) for value in item.get("evidence_ids", []) if isinstance(value, str)),
    )


def _topic(item: Mapping[str, Any]) -> Topic:
    return Topic(
        str(item.get("name") or item.get("title") or "section"),
        _confidence(item.get("confidence"), 0.5),
        tuple(str(value) for value in item.get("evidence_ids", []) if isinstance(value, str)),
    )


def _confidence(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _routing_text(
    metadata: SourceMetadata,
    segments: Sequence[TranscriptSegment],
    frames: Sequence[FrameArtifact],
    project_hint: str | None,
    question: str | None,
) -> str:
    parts = [metadata.title or "", project_hint or "", question or ""]
    parts.extend(segment.text for segment in segments[:1000])
    parts.extend(frame.ocr_text for frame in frames if frame.ocr_text)
    return "\n".join(parts)[:500_000]


def _private_source(acquired: AcquiredSource) -> dict[str, Any]:
    return {
        "source_type": acquired.metadata.source_type,
        "source_identity": acquired.metadata.source_identity,
        "adapter": acquired.metadata.adapter,
        "source_sha256": acquired.source_sha256,
        "temporary_media": acquired.temporary_media,
    }


def _metadata_dict(metadata: SourceMetadata) -> dict[str, Any]:
    return {
        "source_type": metadata.source_type,
        "source_identity": metadata.source_identity,
        "adapter": metadata.adapter,
        "title": metadata.title,
        "duration_seconds": metadata.duration_seconds,
        "uploader": metadata.uploader,
        "webpage_url": metadata.webpage_url,
        "extra": dict(metadata.extra),
    }


def _language_from_name(name: str) -> str:
    parts = name.split(".")
    return parts[-2][:16] if len(parts) >= 3 else "und"


def _frame_evidence_id(frame: FrameArtifact) -> str:
    return "evidence:" + frame.frame_id.removeprefix("frame:")


def _summary_markdown(
    synthesis: Mapping[str, Any], domain: Domain, review_reason: str | None
) -> str:
    about = synthesis.get("ABOUT")
    summary = str(about.get("summary") or about.get("subject") or "").strip() if isinstance(about, Mapping) else str(about or "").strip()
    lines = ["# Video Understanding", "", f"Domain: {domain.value}"]
    if review_reason:
        lines.append(f"Review: {review_reason}")
    if summary:
        lines.extend(("", summary[:4000]))
    lines.extend(
        (
            "",
            f"Claims: {len(_mapping_list(synthesis.get('CLAIMS')))}",
            f"Entities: {len(_mapping_list(synthesis.get('ENTITIES')))}",
            f"Workflows: {len(_mapping_list(synthesis.get('METHOD')))}",
        )
    )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )
