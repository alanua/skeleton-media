from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import VideoRuntimeConfig


_STATES = ("pending", "processing", "done", "failed", "quarantined")
_ID_RE = re.compile(r"^job_[0-9a-f]{32}$")
_WORKER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class QueueRecord:
    schema: str
    record_id: str
    idempotency_key: str
    operation: str
    payload: Mapping[str, Any]
    state: str
    attempts: int
    available_at: float
    lease_until: float | None
    worker_id: str | None
    reason_code: str | None
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if self.schema != "skeleton.video_understanding.queue_record.v1":
            raise VideoUnderstandingError("QUEUE_SCHEMA_INVALID", "queue record schema is invalid")
        if _ID_RE.fullmatch(self.record_id) is None:
            raise VideoUnderstandingError("QUEUE_RECORD_ID_INVALID", "queue record id is invalid")
        if self.state not in _STATES:
            raise VideoUnderstandingError("QUEUE_STATE_INVALID", "queue state is invalid")
        if not isinstance(self.payload, Mapping):
            raise VideoUnderstandingError("QUEUE_PAYLOAD_INVALID", "queue payload must be an object")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 0:
            raise VideoUnderstandingError("QUEUE_ATTEMPTS_INVALID", "queue attempts are invalid")
        for value in (self.available_at, self.created_at, self.updated_at):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise VideoUnderstandingError("QUEUE_TIME_INVALID", "queue timestamp is invalid")
        if self.lease_until is not None and (
            isinstance(self.lease_until, bool) or not isinstance(self.lease_until, (int, float))
        ):
            raise VideoUnderstandingError("QUEUE_TIME_INVALID", "queue lease is invalid")
        if self.worker_id is not None and _WORKER_RE.fullmatch(self.worker_id) is None:
            raise VideoUnderstandingError("QUEUE_WORKER_INVALID", "queue worker id is invalid")
        if _TOKEN_RE.fullmatch(self.operation) is None:
            raise VideoUnderstandingError("QUEUE_OPERATION_INVALID", "queue operation is invalid")
        if self.reason_code is not None and _TOKEN_RE.fullmatch(self.reason_code) is None:
            raise VideoUnderstandingError("QUEUE_REASON_INVALID", "queue reason is invalid")
        try:
            json.dumps(self.payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise VideoUnderstandingError("QUEUE_PAYLOAD_INVALID", "queue payload must be strict JSON") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QueueRecord":
        if isinstance(value.get("attempts"), bool):
            raise VideoUnderstandingError("QUEUE_RECORD_INVALID", "queue attempts cannot be boolean")
        for key in ("available_at", "created_at", "updated_at", "lease_until"):
            if isinstance(value.get(key), bool):
                raise VideoUnderstandingError("QUEUE_RECORD_INVALID", "queue timestamps cannot be boolean")
        try:
            return cls(
                schema=str(value["schema"]),
                record_id=str(value["record_id"]),
                idempotency_key=str(value["idempotency_key"]),
                operation=str(value["operation"]),
                payload=dict(value["payload"]),
                state=str(value["state"]),
                attempts=int(value["attempts"]),
                available_at=float(value["available_at"]),
                lease_until=(float(value["lease_until"]) if value.get("lease_until") is not None else None),
                worker_id=(str(value["worker_id"]) if value.get("worker_id") is not None else None),
                reason_code=(str(value["reason_code"]) if value.get("reason_code") is not None else None),
                created_at=float(value["created_at"]),
                updated_at=float(value["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VideoUnderstandingError("QUEUE_RECORD_INVALID", "queue record is invalid") from exc


class FileQueue:
    def __init__(
        self,
        config: VideoRuntimeConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.root = config.queue_root
        self.clock = clock
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for state in _STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.root / ".queue.lock"
        self.lock_path.touch(mode=0o600, exist_ok=True)

    def enqueue(
        self,
        *,
        operation: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        available_at: float | None = None,
    ) -> QueueRecord:
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 4096:
            raise VideoUnderstandingError("QUEUE_IDEMPOTENCY_INVALID", "queue idempotency key is invalid")
        if not isinstance(operation, str) or _TOKEN_RE.fullmatch(operation) is None:
            raise VideoUnderstandingError("QUEUE_OPERATION_INVALID", "queue operation is invalid")
        if not isinstance(payload, Mapping):
            raise VideoUnderstandingError("QUEUE_PAYLOAD_INVALID", "queue payload must be an object")
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise VideoUnderstandingError("QUEUE_PAYLOAD_INVALID", "queue payload must be strict JSON") from exc
        record_id = "job_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        now = self.clock()
        with self._locked():
            existing = self._find(record_id)
            if existing is not None:
                return existing
            record = QueueRecord(
                schema="skeleton.video_understanding.queue_record.v1",
                record_id=record_id,
                idempotency_key=idempotency_key,
                operation=operation,
                payload=dict(payload),
                state="pending",
                attempts=0,
                available_at=float(available_at if available_at is not None else now),
                lease_until=None,
                worker_id=None,
                reason_code=None,
                created_at=now,
                updated_at=now,
            )
            self._write(self._path("pending", record_id), record)
            return record

    def claim(self, worker_id: str, *, lease_seconds: int | None = None) -> QueueRecord | None:
        if _WORKER_RE.fullmatch(worker_id) is None:
            raise VideoUnderstandingError("QUEUE_WORKER_INVALID", "worker id is invalid")
        lease = lease_seconds or self.config.limits.lease_seconds
        now = self.clock()
        with self._locked():
            self._recover_expired_locked(now)
            for path in sorted((self.root / "pending").glob("job_*.json")):
                record = self._read(path)
                if record.available_at > now:
                    continue
                claimed = replace(
                    record,
                    state="processing",
                    attempts=record.attempts + 1,
                    lease_until=now + lease,
                    worker_id=worker_id,
                    reason_code=None,
                    updated_at=now,
                )
                target = self._path("processing", record.record_id)
                self._write(target, claimed)
                path.unlink()
                _fsync_directory(path.parent)
                return claimed
        return None

    def heartbeat(self, record_id: str, worker_id: str, *, lease_seconds: int | None = None) -> QueueRecord:
        now = self.clock()
        lease = lease_seconds or self.config.limits.lease_seconds
        with self._locked():
            path = self._path("processing", record_id)
            record = self._read(path)
            self._require_owner(record, worker_id)
            updated = replace(record, lease_until=now + lease, updated_at=now)
            self._write(path, updated)
            return updated

    def complete(self, record_id: str, worker_id: str) -> QueueRecord:
        now = self.clock()
        with self._locked():
            source = self._path("processing", record_id)
            record = self._read(source)
            self._require_owner(record, worker_id)
            completed = replace(
                record,
                state="done",
                lease_until=None,
                worker_id=None,
                reason_code=None,
                updated_at=now,
            )
            target = self._path("done", record_id)
            self._write(target, completed)
            source.unlink()
            _fsync_directory(source.parent)
            return completed

    def fail(
        self,
        record_id: str,
        worker_id: str,
        *,
        reason_code: str,
        permanent: bool = False,
    ) -> QueueRecord:
        if not isinstance(reason_code, str) or _TOKEN_RE.fullmatch(reason_code) is None:
            raise VideoUnderstandingError("QUEUE_REASON_INVALID", "queue reason is invalid")
        now = self.clock()
        with self._locked():
            source = self._path("processing", record_id)
            record = self._read(source)
            self._require_owner(record, worker_id)
            quarantine = permanent or record.attempts >= self.config.limits.max_attempts
            state = "quarantined" if quarantine else "pending"
            delay = 0.0 if quarantine else min(3600.0, 30.0 * (2 ** max(0, record.attempts - 1)))
            failed = replace(
                record,
                state=state,
                available_at=now + delay,
                lease_until=None,
                worker_id=None,
                reason_code=reason_code,
                updated_at=now,
            )
            target = self._path(state, record_id)
            self._write(target, failed)
            source.unlink()
            _fsync_directory(source.parent)
            return failed

    def recover_expired(self) -> int:
        with self._locked():
            return self._recover_expired_locked(self.clock())

    def get(self, record_id: str) -> QueueRecord:
        with self._locked():
            record = self._find(record_id)
            if record is None:
                raise VideoUnderstandingError("QUEUE_RECORD_NOT_FOUND", "queue record was not found")
            return record

    def counts(self) -> dict[str, int]:
        with self._locked():
            return {state: len(tuple((self.root / state).glob("job_*.json"))) for state in _STATES}

    def _recover_expired_locked(self, now: float) -> int:
        recovered = 0
        for path in sorted((self.root / "processing").glob("job_*.json")):
            record = self._read(path)
            if record.lease_until is None or record.lease_until > now:
                continue
            pending = replace(
                record,
                state="pending",
                available_at=now,
                lease_until=None,
                worker_id=None,
                reason_code="LEASE_EXPIRED",
                updated_at=now,
            )
            target = self._path("pending", record.record_id)
            self._write(target, pending)
            path.unlink()
            recovered += 1
        if recovered:
            _fsync_directory(self.root / "processing")
        return recovered

    @staticmethod
    def _require_owner(record: QueueRecord, worker_id: str) -> None:
        if record.state != "processing" or record.worker_id != worker_id:
            raise VideoUnderstandingError("QUEUE_LEASE_OWNER_MISMATCH", "worker does not own queue record")

    def _find(self, record_id: str) -> QueueRecord | None:
        if _ID_RE.fullmatch(record_id) is None:
            raise VideoUnderstandingError("QUEUE_RECORD_ID_INVALID", "queue record id is invalid")
        found: list[QueueRecord] = []
        for state in _STATES:
            path = self._path(state, record_id)
            if path.exists():
                found.append(self._read(path))
        if len(found) > 1:
            raise VideoUnderstandingError("QUEUE_DUPLICATE_STATE", "queue record exists in multiple states")
        return found[0] if found else None

    def _path(self, state: str, record_id: str) -> Path:
        if state not in _STATES or _ID_RE.fullmatch(record_id) is None:
            raise VideoUnderstandingError("QUEUE_PATH_INVALID", "queue path identity is invalid")
        return self.root / state / f"{record_id}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_path.open("r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> QueueRecord:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise VideoUnderstandingError("QUEUE_RECORD_NOT_FOUND", "queue record was not found") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VideoUnderstandingError("QUEUE_RECORD_UNREADABLE", "queue record could not be read") from exc
        if not isinstance(value, Mapping):
            raise VideoUnderstandingError("QUEUE_RECORD_INVALID", "queue record must be an object")
        return QueueRecord.from_dict(value)

    @staticmethod
    def _write(path: Path, record: QueueRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".part")
        encoded = json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
