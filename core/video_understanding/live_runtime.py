from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from core.memory_gateway import MemoryGateway, capability_token
from core.memory_gateway_storage import PrivateMemoryGatewayStorage
from core.private_memory_stack import PrivateMemoryStack
from core.video_understanding.artifact_store import PrivateArtifactStore
from core.video_understanding.local_llm import LocalLlmClient, LocalLlmConfig
from core.video_understanding.models import (
    Domain,
    ProcessingMode,
    ProcessingState,
    ReviewDecision,
    SourceReference,
    VideoRecord,
    VideoUnderstandingError,
)
from core.video_understanding.pipeline import VideoPipeline
from core.video_understanding.queue import FileQueue
from core.video_understanding.runtime_config import VideoRuntimeConfig, load_runtime_config
from core.video_understanding.worker import VideoWorker


PRIVATE_MEMORY_ROOT_ENV = "SKELETON_PRIVATE_MEMORY_ROOT"
PRIVATE_MEMORY_CONFIG_ENV = "SKELETON_PRIVATE_MEMORY_CONFIG"
CANONICAL_DB_NAME = "canonical.sqlite"
_SAFE_REVISION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class LiveVideoRuntime:
    config: VideoRuntimeConfig
    private_memory_root: Path
    stack: PrivateMemoryStack
    gateway: MemoryGateway
    queue: FileQueue
    artifact_store: PrivateArtifactStore
    pipeline: VideoPipeline

    def worker(self, worker_id: str) -> VideoWorker:
        return VideoWorker(self.queue, self.pipeline, worker_id=worker_id)


def resolve_existing_private_memory_root(
    env: Mapping[str, str] | None = None,
) -> Path:
    values = env if env is not None else os.environ
    configured_root = str(values.get(PRIVATE_MEMORY_ROOT_ENV, "")).strip()
    if configured_root:
        try:
            root = Path(configured_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise VideoUnderstandingError(
                "CANONICAL_MEMORY_NOT_FOUND",
                "existing canonical private memory was not found",
            ) from exc
        _require_existing_canonical_stack(root)
        return root

    config_value = str(values.get(PRIVATE_MEMORY_CONFIG_ENV, "")).strip()
    if not config_value:
        raise VideoUnderstandingError(
            "CANONICAL_MEMORY_ROOT_REQUIRED",
            "an existing canonical private-memory root must be configured",
        )
    try:
        config_path = Path(config_value).expanduser().resolve(strict=True)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoUnderstandingError(
            "CANONICAL_MEMORY_CONFIG_INVALID",
            "private-memory configuration is unavailable",
        ) from exc
    if not isinstance(payload, Mapping):
        raise VideoUnderstandingError(
            "CANONICAL_MEMORY_CONFIG_INVALID",
            "private-memory configuration must be an object",
        )
    database = payload.get("database")
    raw_path = database.get("path") if isinstance(database, Mapping) else None
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise VideoUnderstandingError(
            "CANONICAL_STACK_ROOT_REQUIRED",
            "configured private memory is not a canonical stack root",
        )
    db_path = Path(raw_path).expanduser()
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path
    try:
        db_path = db_path.resolve(strict=True)
    except OSError as exc:
        raise VideoUnderstandingError(
            "CANONICAL_MEMORY_NOT_FOUND",
            "configured canonical private memory was not found",
        ) from exc
    if db_path.name != CANONICAL_DB_NAME:
        raise VideoUnderstandingError(
            "CANONICAL_STACK_ROOT_REQUIRED",
            "configured private memory is not the canonical stack database",
        )
    root = db_path.parent
    _require_existing_canonical_stack(root)
    return root


def build_live_runtime(
    config_path: Path,
    *,
    processing_revision: str,
    env: Mapping[str, str] | None = None,
) -> LiveVideoRuntime:
    if _SAFE_REVISION_RE.fullmatch(processing_revision) is None:
        raise VideoUnderstandingError(
            "PROCESSING_REVISION_INVALID",
            "processing revision is invalid",
        )
    config = load_runtime_config(config_path)
    private_root = resolve_existing_private_memory_root(env)
    stack = PrivateMemoryStack(private_root)
    status = stack.status()
    canonical = status.get("canonical_sqlite")
    if not isinstance(canonical, Mapping) or canonical.get("state") != "READY":
        raise VideoUnderstandingError(
            "CANONICAL_MEMORY_NOT_READY",
            "canonical private memory is not ready",
        )
    storage = PrivateMemoryGatewayStorage(stack)
    gateway = MemoryGateway(
        capability_token(namespaces=("skeleton",), public_mode=False),
        private_memory_storage=storage,
    )
    queue = FileQueue(config)
    artifact_store = PrivateArtifactStore(config)
    llm = LocalLlmClient(
        LocalLlmConfig(
            endpoint=config.ollama_endpoint or "",
            model=config.ollama_model,
            provider="ollama",
        )
    )
    pipeline = VideoPipeline(
        config,
        processing_revision=processing_revision,
        memory_executor=gateway.execute,
        artifact_store=artifact_store,
        loopback_llm=llm,
    )
    return LiveVideoRuntime(
        config=config,
        private_memory_root=private_root,
        stack=stack,
        gateway=gateway,
        queue=queue,
        artifact_store=artifact_store,
        pipeline=pipeline,
    )


def doctor_live_runtime(
    config_path: Path,
    *,
    processing_revision: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    reasons: list[str] = []
    try:
        runtime = build_live_runtime(
            config_path,
            processing_revision=processing_revision,
            env=env,
        )
    except VideoUnderstandingError as exc:
        return _doctor_blocked(exc.reason_code)

    mandatory_keys = ("yt_dlp", "ffmpeg", "ffprobe", "ocr")
    ready_mandatory = sum(
        1
        for key in mandatory_keys
        if _executable_ready(runtime.config.executables.get(key))
    )
    sona_executable_ready = _executable_ready(
        runtime.config.executables.get("sona")
    )
    if ready_mandatory != len(mandatory_keys):
        reasons.append("MANDATORY_PROVIDER_MISSING")

    ollama_status, model_count = _ollama_status(
        runtime.config.ollama_endpoint or "",
        runtime.config.ollama_model,
    )
    if ollama_status != "READY":
        reasons.append("OLLAMA_BLOCKED")
    sona_status = _local_api_status(runtime.config.sona_endpoint, "v1/models")
    if sona_status != "READY":
        reasons.append("ASR_FALLBACK_BLOCKED")

    memory_status = _gateway_status(runtime.gateway)
    if memory_status != "READY":
        reasons.append("MEMORY_GATEWAY_BLOCKED")
    queue_counts = runtime.queue.counts()
    queue_status = "READY"
    artifact_status = (
        "READY" if runtime.artifact_store.root.is_dir() else "BLOCKED"
    )
    if artifact_status != "READY":
        reasons.append("ARTIFACT_STORE_BLOCKED")

    mandatory_ready = (
        ready_mandatory == len(mandatory_keys)
        and ollama_status == "READY"
        and memory_status == "READY"
        and artifact_status == "READY"
    )
    return {
        "schema": "skeleton.video_understanding.live_doctor.v1",
        "status": "READY" if mandatory_ready else "BLOCKED",
        "runtime_config_status": "READY",
        "provider_ready_count": ready_mandatory,
        "provider_required_count": len(mandatory_keys),
        "sona_executable_status": (
            "READY" if sona_executable_ready else "BLOCKED"
        ),
        "ollama_status": ollama_status,
        "ollama_model_count": model_count,
        "sona_status": sona_status,
        "artifact_store_status": artifact_status,
        "queue_status": queue_status,
        "queue_counts": queue_counts,
        "memory_gateway_status": memory_status,
        "stable_reason_codes": sorted(set(reasons)),
    }


def synthetic_memory_roundtrip(
    runtime: LiveVideoRuntime,
    *,
    approval_ref: str,
) -> dict[str, object]:
    from core.video_understanding.memory_gateway_adapter import (
        build_private_mutation,
    )

    record = VideoRecord(
        schema="skeleton.video_understanding.record.v1",
        video_record_id="vr_runtime_healthcheck",
        processing_revision="runtime-health-v1",
        state=ProcessingState.UNDERSTOOD,
        mode=ProcessingMode.QUICK,
        source=SourceReference(
            source_type="SYNTHETIC",
            private_identity="synthetic:video-runtime-health",
            adapter="synthetic",
        ),
        detected_domain=Domain.SKELETON_ARCHITECTURE,
        domain_candidates=(),
        about={"status": "synthetic"},
        structure=(),
        methods=(),
        topics=(),
        entities=(),
        claims=(),
        evidence=(),
        actions=(),
        conflicts=(),
        project_links=(),
        review=ReviewDecision("SYSTEM_UNDERSTOOD", "SYSTEM"),
        artifact_manifest_hash="7" * 64,
    )
    envelope = build_private_mutation(record, approval_ref=approval_ref)
    mutation = runtime.gateway.execute(envelope)
    payload = envelope["payload"]
    read = runtime.gateway.execute(
        {
            "schema": "skeleton.memory_gateway.request.v1",
            "namespace": "skeleton",
            "command": "skeleton.memory.private_read_exact",
            "payload": {
                "project_id": "skeleton",
                "dataset_id": "video_understanding",
                "canonical_ref": (
                    f"{payload['fact_namespace']}:{payload['fact_id']}"
                ),
            },
        }
    )
    mutation_payload = mutation.get("payload")
    read_payload = read.get("payload")
    committed = (
        isinstance(mutation_payload, Mapping)
        and mutation_payload.get("status")
        in {"DONE", "COMMITTED", "SUCCESS", "DEGRADED"}
    )
    authoritative = (
        isinstance(read_payload, Mapping)
        and read_payload.get("authoritative") is True
    )
    return {
        "schema": "skeleton.video_understanding.memory_roundtrip.v1",
        "status": "DONE" if committed and authoritative else "BLOCKED",
        "mutation_committed": committed,
        "exact_read_authoritative": authoritative,
    }


def _require_existing_canonical_stack(root: Path) -> None:
    if not root.is_dir() or not (root / CANONICAL_DB_NAME).is_file():
        raise VideoUnderstandingError(
            "CANONICAL_MEMORY_NOT_FOUND",
            "existing canonical private memory was not found",
        )


def _executable_ready(value: str | None) -> bool:
    return bool(
        value
        and Path(value).is_file()
        and os.access(Path(value), os.X_OK)
    )


def _ollama_status(endpoint: str, selected_model: str) -> tuple[str, int]:
    try:
        payload = _local_json_get(endpoint, "api/tags")
    except VideoUnderstandingError:
        return "BLOCKED", 0
    models = payload.get("models")
    if not isinstance(models, list):
        return "BLOCKED", 0
    names = {
        str(item.get("name"))
        for item in models
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
    }
    return (
        "READY" if selected_model in names else "BLOCKED",
        len(names),
    )


def _local_api_status(endpoint: str, path: str) -> str:
    try:
        _local_json_get(endpoint, path)
    except VideoUnderstandingError:
        return "BLOCKED"
    return "READY"


def _local_json_get(endpoint: str, path: str) -> Mapping[str, Any]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise VideoUnderstandingError(
            "LOCAL_ENDPOINT_REQUIRED",
            "runtime endpoint must be loopback HTTP",
        )
    request = Request(
        urljoin(endpoint.rstrip("/") + "/", path),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=3) as response:
            raw = response.read(1_000_001)
    except Exception as exc:
        raise VideoUnderstandingError(
            "LOCAL_PROVIDER_UNAVAILABLE",
            "local provider is unavailable",
        ) from exc
    if len(raw) > 1_000_000:
        raise VideoUnderstandingError(
            "LOCAL_PROVIDER_OUTPUT_TOO_LARGE",
            "local provider output exceeded limit",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoUnderstandingError(
            "LOCAL_PROVIDER_INVALID_RESPONSE",
            "local provider response is invalid",
        ) from exc
    if not isinstance(payload, Mapping):
        raise VideoUnderstandingError(
            "LOCAL_PROVIDER_INVALID_RESPONSE",
            "local provider response must be an object",
        )
    return payload


def _gateway_status(gateway: MemoryGateway) -> str:
    try:
        response = gateway.execute(
            {
                "schema": "skeleton.memory_gateway.request.v1",
                "namespace": "skeleton",
                "command": "skeleton.memory.private_status",
                "payload": {
                    "project_id": "skeleton",
                    "dataset_id": "video_understanding",
                },
            }
        )
    except Exception:
        return "BLOCKED"
    return "READY" if isinstance(response.get("payload"), Mapping) else "BLOCKED"


def _doctor_blocked(reason_code: str) -> dict[str, object]:
    return {
        "schema": "skeleton.video_understanding.live_doctor.v1",
        "status": "BLOCKED",
        "runtime_config_status": "BLOCKED",
        "provider_ready_count": 0,
        "provider_required_count": 4,
        "sona_executable_status": "BLOCKED",
        "ollama_status": "BLOCKED",
        "ollama_model_count": 0,
        "sona_status": "BLOCKED",
        "artifact_store_status": "BLOCKED",
        "queue_status": "BLOCKED",
        "queue_counts": {},
        "memory_gateway_status": "BLOCKED",
        "stable_reason_codes": [reason_code],
    }
