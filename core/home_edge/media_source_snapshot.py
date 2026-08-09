from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from core.home_edge.executor import HomeEdgeExecError, HomeEdgeExecRequest, sign_request
from core.home_edge.executor_gateway import EXEC_HMAC_SECRET_ENV, execute_home_edge_request


TASK_ID = "home_edge_01_media_source_snapshot_v1"
REPOSITORY = "alanua/Skeleton"
TARGET_NODE = "home-edge-01"
SOURCE_IDENTITY_TOKEN = "home_edge_01_skeleton_cast_app_py"
SOURCE_PATH = "/opt/skeleton/cast/app.py"
RUN_AS = "desktop-user"
EXECUTION_LANE = "read_only"
REQUEST_TIMEOUT_SECONDS = 30
MAX_SOURCE_BYTES = 700 * 1024
MAX_EXECUTOR_OUTPUT_BYTES = 1_000_000
RECEIPT_SCHEMA = "skeleton.home_edge.media_source_snapshot_receipt.v1"
IDEMPOTENCY_KEY_PREFIX = "home-edge-01-media-source-snapshot-v1"
PRIVATE_ARTIFACT_RELATIVE_PATH = (
    Path("home_edge") / "home_edge_01" / "media_source_snapshot" / "app.py.latest"
)
EXPECTED_MAIN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FIELD_RE = re.compile(r"^\s*(?P<field>[A-Za-z][A-Za-z0-9 _-]{0,80}):\s*(?P<value>.*?)\s*$")
PUBLIC_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:=-]+$")
VERSION_MARKER_RE = re.compile(r"(?i)\bv63\b")
SECRET_NAME_TOKENS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "BOT_TOKEN",
    "CLIENT_SECRET",
    "HMAC_SECRET",
    "SECRET_KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "TMDB_API_KEY",
    "TMDB_KEY",
    "OPENAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "SKELETON_SECRET",
    "BRAVE_KEY",
)
_SECRET_SUFFIXES = tuple(
    re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") for name in SECRET_NAME_TOKENS
)
_PLACEHOLDER_SECRET_RE = re.compile(
    r"(?i)^(?:|x+|test(?:ing)?(?:[-_ ]?(?:secret|token|key|password|value))?|"
    r"dummy(?:[-_ ]?(?:secret|token|key|password|value))?|"
    r"placeholder|example|sample|changeme|change_me|replace_me|not[-_ ]?set|"
    r"todo|none|null|fake[-_ ]?(?:secret|token|key|password|value)?|"
    r"sk-[x*]+)$"
)

RECEIPT_FIELDS = (
    "maintenance_task_id",
    "source_identity",
    "source_version_marker",
    "source_bytes",
    "source_sha256",
    "python_parse_status",
    "video_route_present",
    "health_route_present",
    "private_artifact_written",
    "private_artifact_hash_matches",
    "executor_receipt_hash",
    "stable_reason",
    "success_criteria",
)

ALLOWED_FIELDS = frozenset(
    {
        "Mode",
        "Maintenance Task ID",
        "Repository",
        "Expected Main SHA",
        "Target",
    }
)


@dataclass(frozen=True)
class RuntimeInput:
    repository: str
    expected_main_sha: str
    target: str


def execute_media_source_snapshot_task(
    body: str,
    *,
    registered_clean_main_sha: str,
    github_main_sha: str,
    environment: Mapping[str, str] | None = None,
    private_root: Path | None = None,
) -> dict[str, object]:
    runtime_input = parse_runtime_input(body)
    validate_main_sha(
        runtime_input.expected_main_sha,
        registered_clean_main_sha=registered_clean_main_sha,
        github_main_sha=github_main_sha,
    )
    artifact = private_artifact_path(environment=environment, private_root=private_root)
    existing = _existing_private_artifact_receipt(artifact)
    if existing is not None:
        return existing

    request = build_snapshot_request(environment=environment)
    try:
        executor_receipt = execute_home_edge_request(request.to_mapping())
    except (subprocess.TimeoutExpired, TimeoutError):
        return _blocked_receipt("executor_transport_timeout")
    except HomeEdgeExecError:
        return _blocked_receipt("executor_transport_failed")
    except Exception:
        return _blocked_receipt("executor_transport_exception")

    receipt = public_receipt_from_executor_stdout(executor_receipt.to_mapping())
    receipt["executor_receipt_hash"] = executor_receipt.receipt_hash
    if not _prepublication_success(receipt):
        return receipt

    source_b64 = _private_source_b64(executor_receipt.stdout)
    if source_b64 is None:
        return _blocked_receipt("executor_source_absent", executor_receipt_hash=executor_receipt.receipt_hash)
    try:
        source = base64.b64decode(source_b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return _blocked_receipt("executor_source_invalid", executor_receipt_hash=executor_receipt.receipt_hash)

    local = _validate_source_bytes(source)
    if local["source_identity"] != "verified":
        local["executor_receipt_hash"] = executor_receipt.receipt_hash
        return local
    if local["source_bytes"] != receipt["source_bytes"] or local["source_sha256"] != receipt["source_sha256"]:
        return _blocked_receipt("remote_local_source_mismatch", executor_receipt_hash=executor_receipt.receipt_hash)

    if not _write_private_artifact(source, artifact, str(receipt["source_sha256"]), int(receipt["source_bytes"])):
        return _blocked_receipt("private_artifact_hash_mismatch", executor_receipt_hash=executor_receipt.receipt_hash)

    receipt["private_artifact_written"] = True
    receipt["private_artifact_hash_matches"] = True
    receipt["stable_reason"] = "completed"
    receipt["success_criteria"] = "met"
    return receipt


def parse_runtime_input(body: str) -> RuntimeInput:
    fields: dict[str, str] = {}
    duplicates: set[str] = set()
    for line in _metadata_lines(body):
        match = FIELD_RE.match(line)
        if match is None:
            continue
        field = match.group("field").strip()
        value = match.group("value").strip()
        if not value:
            continue
        if field in fields:
            duplicates.add(field)
        fields[field] = value
    if duplicates:
        raise ValueError("duplicate_runtime_input_field")
    if sorted(set(fields) - ALLOWED_FIELDS):
        raise ValueError("unknown_runtime_input_field")
    if fields.get("Mode") != "RUNTIME_MAINTENANCE_TASK":
        raise ValueError("runtime_mode_mismatch")
    if fields.get("Maintenance Task ID") != TASK_ID:
        raise ValueError("maintenance_task_id_mismatch")
    runtime_input = RuntimeInput(
        repository=fields.get("Repository", ""),
        expected_main_sha=fields.get("Expected Main SHA", ""),
        target=fields.get("Target", ""),
    )
    if runtime_input.repository != REPOSITORY:
        raise ValueError("repository_mismatch")
    if EXPECTED_MAIN_SHA_RE.fullmatch(runtime_input.expected_main_sha) is None:
        raise ValueError("expected_main_sha_malformed")
    if runtime_input.target != TARGET_NODE:
        raise ValueError("target_mismatch")
    return runtime_input


def validate_main_sha(
    expected_main_sha: str,
    *,
    registered_clean_main_sha: str,
    github_main_sha: str,
) -> None:
    if EXPECTED_MAIN_SHA_RE.fullmatch(registered_clean_main_sha or "") is None:
        raise ValueError("registered_clean_main_sha_unavailable")
    if EXPECTED_MAIN_SHA_RE.fullmatch(github_main_sha or "") is None:
        raise ValueError("github_main_sha_unavailable")
    if expected_main_sha != registered_clean_main_sha:
        raise ValueError("registered_clean_main_sha_mismatch")
    if expected_main_sha != github_main_sha:
        raise ValueError("github_main_sha_mismatch")


def build_snapshot_request(*, environment: Mapping[str, str] | None = None) -> HomeEdgeExecRequest:
    env = os.environ if environment is None else environment
    secret = env.get(EXEC_HMAC_SECRET_ENV, "")
    if not secret:
        raise ValueError("home_edge_exec_hmac_secret_missing")
    request = HomeEdgeExecRequest.from_mapping(
        {
            "request_id": f"{TASK_ID}-{uuid4()}",
            "node_id": TARGET_NODE,
            "execution_lane": EXECUTION_LANE,
            "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "idempotency_key": f"{IDEMPOTENCY_KEY_PREFIX}-{uuid4()}",
            "run_as": RUN_AS,
            "mode": "script",
            "script": SNAPSHOT_SCRIPT,
            "script_interpreter": "python3",
            "timestamp": datetime.now(UTC).isoformat(),
            "nonce": f"{TASK_ID}-{uuid4()}",
            "max_output_bytes": MAX_EXECUTOR_OUTPUT_BYTES,
            "public": False,
        }
    )
    return HomeEdgeExecRequest.from_mapping(
        {**request.to_mapping(include_signature=False), "signature": sign_request(request, secret)}
    )


def public_receipt_from_executor_stdout(receipt: Mapping[str, Any]) -> dict[str, object]:
    if receipt.get("status") != "ok" or receipt.get("exit_code") != 0:
        return _blocked_receipt("executor_receipt_not_ok")
    decoded = _decode_stdout(receipt.get("stdout"))
    if decoded is None:
        return _blocked_receipt("executor_stdout_not_json")
    public = decoded.get("public")
    if not isinstance(public, Mapping):
        return _blocked_receipt("executor_public_receipt_missing")
    try:
        return sanitize_public_receipt(public)
    except ValueError:
        return _blocked_receipt("executor_public_receipt_unsafe")


def sanitize_public_receipt(receipt: Mapping[str, Any]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for field in RECEIPT_FIELDS:
        if field not in receipt:
            raise ValueError("receipt_field_missing")
        value = receipt[field]
        if isinstance(value, bool):
            sanitized[field] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            sanitized[field] = value
        elif isinstance(value, str) and PUBLIC_VALUE_RE.fullmatch(value):
            sanitized[field] = value
        else:
            raise ValueError("receipt_field_not_public_safe")
    if sanitized["maintenance_task_id"] != TASK_ID:
        raise ValueError("receipt_task_id_mismatch")
    if sanitized["source_identity"] not in {"verified", "blocked"}:
        raise ValueError("source_identity_invalid")
    if sanitized["source_version_marker"] not in {"v63", "unknown"}:
        raise ValueError("source_version_marker_invalid")
    return sanitized


def receipt_status_lines(receipt: Mapping[str, object]) -> list[str]:
    return [f"{field}={receipt[field]}" for field in RECEIPT_FIELDS]


def success_criteria_met(receipt: Mapping[str, object]) -> bool:
    return (
        receipt.get("success_criteria") == "met"
        and receipt.get("source_identity") == "verified"
        and receipt.get("python_parse_status") == "ok"
        and receipt.get("video_route_present") is True
        and receipt.get("health_route_present") is True
        and receipt.get("private_artifact_written") is True
        and receipt.get("private_artifact_hash_matches") is True
    )


def private_artifact_path(
    *,
    environment: Mapping[str, str] | None = None,
    private_root: Path | None = None,
) -> Path:
    if private_root is not None:
        root = private_root
    else:
        env = os.environ if environment is None else environment
        configured = env.get("SKELETON_RUNNER_PRIVATE_MEMORY_ROOT", "").strip()
        root = Path(configured) if configured else Path.home() / ".local/state/skeleton/runner-private"
    return root / PRIVATE_ARTIFACT_RELATIVE_PATH


def _validate_source_bytes(source: bytes) -> dict[str, object]:
    if len(source) > MAX_SOURCE_BYTES:
        return _blocked_receipt("source_oversize", source_bytes=len(source))
    source_hash = hashlib.sha256(source).hexdigest()
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return _blocked_receipt("source_not_utf8", source_bytes=len(source), source_sha256=source_hash)
    if credential_literal_names(text):
        return _blocked_receipt("suspicious_credential_literal", source_bytes=len(source), source_sha256=source_hash)
    try:
        tree = ast.parse(text, filename=SOURCE_PATH)
        compile(tree, SOURCE_PATH, "exec")
    except SyntaxError:
        return _blocked_receipt("python_parse_failed", source_bytes=len(source), source_sha256=source_hash)
    video_route, health_route = _route_markers(tree)
    identifiers = _identity_markers(text, tree)
    if not video_route:
        return _blocked_receipt("video_route_missing", source_bytes=len(source), source_sha256=source_hash)
    if not health_route:
        return _blocked_receipt("health_route_missing", source_bytes=len(source), source_sha256=source_hash)
    if not identifiers:
        return _blocked_receipt("skeleton_cast_markers_missing", source_bytes=len(source), source_sha256=source_hash)
    return {
        "maintenance_task_id": TASK_ID,
        "source_identity": "verified",
        "source_version_marker": "v63" if VERSION_MARKER_RE.search(text) else "unknown",
        "source_bytes": len(source),
        "source_sha256": source_hash,
        "python_parse_status": "ok",
        "video_route_present": True,
        "health_route_present": True,
        "private_artifact_written": False,
        "private_artifact_hash_matches": False,
        "executor_receipt_hash": "pending",
        "stable_reason": "validated",
        "success_criteria": "pending",
    }


def _existing_private_artifact_receipt(artifact: Path) -> dict[str, object] | None:
    try:
        artifact.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return _blocked_receipt("existing_artifact_unsafe")
    source = _read_existing_private_artifact_bytes(artifact)
    if source is None:
        return _blocked_receipt("existing_artifact_unsafe")
    local = _validate_source_bytes(source)
    if local["source_identity"] != "verified":
        return _blocked_receipt("existing_artifact_invalid")
    local["private_artifact_written"] = True
    local["private_artifact_hash_matches"] = True
    local["executor_receipt_hash"] = "not_required_existing_capture"
    local["stable_reason"] = "already_captured"
    local["success_criteria"] = "met"
    return local


def _read_existing_private_artifact_bytes(artifact: Path) -> bytes | None:
    try:
        st_l = artifact.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st_l.st_mode) or not stat.S_ISREG(st_l.st_mode):
        return None
    if stat.S_IMODE(st_l.st_mode) & 0o077:
        return None
    if hasattr(os, "getuid") and st_l.st_uid != os.getuid():
        return None
    try:
        parent_st = artifact.parent.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
        return None
    if stat.S_IMODE(parent_st.st_mode) & 0o077:
        return None
    if hasattr(os, "getuid") and parent_st.st_uid != os.getuid():
        return None
    if st_l.st_size > MAX_SOURCE_BYTES:
        return None
    try:
        with artifact.open("rb") as handle:
            source = handle.read(MAX_SOURCE_BYTES + 1)
        st_after = artifact.lstat()
    except OSError:
        return None
    if _file_id(st_l) != _file_id(st_after):
        return None
    if len(source) > MAX_SOURCE_BYTES:
        return None
    return source


def _file_id(st: os.stat_result) -> dict[str, int | None]:
    return {
        "mode": stat.S_IFMT(st.st_mode),
        "dev": getattr(st, "st_dev", None),
        "ino": getattr(st, "st_ino", None),
        "size": st.st_size,
        "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)),
    }


def credential_literal_names(text: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    leaked: list[str] = []
    for node in ast.walk(tree):
        name: str | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            value = node.value
            for target in node.targets:
                name = _assignment_name(target)
                if name and _secret_identifier(name) and _literal_is_live_secret(value):
                    leaked.append(name)
        elif isinstance(node, ast.AnnAssign):
            name = _assignment_name(node.target)
            value = node.value
            if name and _secret_identifier(name) and value is not None and _literal_is_live_secret(value):
                leaked.append(name)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if _dict_key_is_secret_identifier(key) and _literal_is_live_secret(value):
                    leaked.append("<dict>")
    return tuple(leaked)


def _assignment_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _secret_identifier(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    for suffix in _SECRET_SUFFIXES:
        if normalized == suffix or normalized.endswith("_" + suffix):
            return True
    return False


def _dict_key_is_secret_identifier(key: ast.AST | None) -> bool:
    return (
        isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and _secret_identifier(key.value)
    )


def _literal_is_live_secret(value: ast.AST) -> bool:
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return False
    literal = value.value.strip()
    if _PLACEHOLDER_SECRET_RE.fullmatch(literal):
        return False
    return len(literal) >= 8


def _route_markers(tree: ast.AST) -> tuple[bool, bool]:
    video = False
    health = False
    flask_apps = _flask_app_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            route = _route_path(decorator, flask_apps)
            if route == "/video":
                video = True
            if route in {"/health", "/healthz"}:
                health = True
    return video, health


def _flask_app_names(tree: ast.AST) -> frozenset[str]:
    flask_names = {"Flask"}
    app_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "flask":
            for alias in node.names:
                if alias.name == "Flask":
                    flask_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id in flask_names:
                app_names.update(name for target in node.targets if (name := _assignment_name(target)))
    return frozenset(app_names)


def _route_path(node: ast.AST, flask_apps: frozenset[str]) -> str | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr in {"get", "route"}
        and isinstance(func.value, ast.Name)
        and func.value.id in flask_apps
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return None
    return node.args[0].value


def _identity_markers(text: str, tree: ast.AST) -> bool:
    lowered = text.lower()
    has_flask = any(
        isinstance(node, ast.ImportFrom) and node.module == "flask"
        or isinstance(node, ast.Import) and any(alias.name == "flask" for alias in node.names)
        for node in ast.walk(tree)
    )
    return has_flask and "skeleton" in lowered and "cast" in lowered and "media" in lowered


def _write_private_artifact(source: bytes, artifact: Path, expected_hash: str, expected_size: int) -> bool:
    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".app.py.", dir=str(artifact.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        if tmp_path.stat().st_size != expected_size or _sha256_file(tmp_path) != expected_hash:
            tmp_path.unlink(missing_ok=True)
            return False
        try:
            os.link(tmp_path, artifact)
        except FileExistsError:
            tmp_path.unlink(missing_ok=True)
            return False
        os.chmod(artifact, 0o600)
        return artifact.stat().st_size == expected_size and _sha256_file(artifact) == expected_hash
    finally:
        tmp_path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepublication_success(receipt: Mapping[str, object]) -> bool:
    return (
        receipt.get("source_identity") == "verified"
        and receipt.get("python_parse_status") == "ok"
        and receipt.get("video_route_present") is True
        and receipt.get("health_route_present") is True
        and isinstance(receipt.get("source_bytes"), int)
        and isinstance(receipt.get("source_sha256"), str)
    )


def _private_source_b64(stdout: str) -> str | None:
    decoded = _decode_stdout(stdout)
    if decoded is None:
        return None
    source = decoded.get("private_source_b64")
    return source if isinstance(source, str) else None


def _decode_stdout(stdout: Any) -> dict[str, Any] | None:
    if not isinstance(stdout, str):
        return None
    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _metadata_lines(body: str) -> list[str]:
    metadata = (body or "").split("```task", 1)[0]
    return [line for line in metadata.splitlines() if not line.lstrip().startswith("#")]


def _blocked_receipt(
    reason: str,
    *,
    source_bytes: int = 0,
    source_sha256: str = "unavailable",
    executor_receipt_hash: str = "unavailable",
) -> dict[str, object]:
    return {
        "maintenance_task_id": TASK_ID,
        "source_identity": "blocked",
        "source_version_marker": "unknown",
        "source_bytes": source_bytes,
        "source_sha256": source_sha256,
        "python_parse_status": "blocked",
        "video_route_present": False,
        "health_route_present": False,
        "private_artifact_written": False,
        "private_artifact_hash_matches": False,
        "executor_receipt_hash": executor_receipt_hash,
        "stable_reason": reason,
        "success_criteria": "not_met",
    }


SNAPSHOT_SCRIPT = f'''from __future__ import annotations
import ast
import base64
import hashlib
import json
import os
import re
import stat

TASK_ID = {TASK_ID!r}
SOURCE_PATH = {SOURCE_PATH!r}
MAX_SOURCE_BYTES = {MAX_SOURCE_BYTES!r}
SECRET_SUFFIXES = {tuple(_SECRET_SUFFIXES)!r}

def blocked(reason, source=b""):
    sha = hashlib.sha256(source).hexdigest() if source else "unavailable"
    return {{
        "public": {{
            "maintenance_task_id": TASK_ID,
            "source_identity": "blocked",
            "source_version_marker": "unknown",
            "source_bytes": len(source),
            "source_sha256": sha,
            "python_parse_status": "blocked",
            "video_route_present": False,
            "health_route_present": False,
            "private_artifact_written": False,
            "private_artifact_hash_matches": False,
            "executor_receipt_hash": "pending",
            "stable_reason": reason,
            "success_criteria": "not_met",
        }}
    }}

def file_id(st):
    return {{
        "mode": stat.S_IFMT(st.st_mode),
        "dev": getattr(st, "st_dev", None),
        "ino": getattr(st, "st_ino", None),
        "size": st.st_size,
        "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1000000000)),
    }}

def same_file(before, after):
    return file_id(before) == file_id(after)

def assignment_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None

def secret_identifier(name):
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if any(normalized == suffix or normalized.endswith("_" + suffix) for suffix in SECRET_SUFFIXES):
        return True
    return False

def dict_key_is_secret_identifier(key):
    return isinstance(key, ast.Constant) and isinstance(key.value, str) and secret_identifier(key.value)

def placeholder(value):
    return re.fullmatch(
        r"(?i)^(?:|x+|test(?:ing)?(?:[-_ ]?(?:secret|token|key|password|value))?|dummy(?:[-_ ]?(?:secret|token|key|password|value))?|placeholder|example|sample|changeme|change_me|replace_me|not[-_ ]?set|todo|none|null|fake[-_ ]?(?:secret|token|key|password|value)?|sk-[x*]+)$",
        value.strip(),
    ) is not None

def literal_is_live_secret(value):
    return isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value.strip()) >= 8 and not placeholder(value.value)

def has_credential_literal(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = assignment_name(target)
                if name and secret_identifier(name) and literal_is_live_secret(node.value):
                    return True
        elif isinstance(node, ast.AnnAssign):
            name = assignment_name(node.target)
            if name and node.value is not None and secret_identifier(name) and literal_is_live_secret(node.value):
                return True
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if dict_key_is_secret_identifier(key) and literal_is_live_secret(value):
                    return True
    return False

def flask_app_names(tree):
    flask_names = {{"Flask"}}
    app_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "flask":
            for alias in node.names:
                if alias.name == "Flask":
                    flask_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id in flask_names:
                for target in node.targets:
                    name = assignment_name(target)
                    if name:
                        app_names.add(name)
    return app_names

def route_path(node, apps):
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr in ("get", "route")
        and isinstance(func.value, ast.Name)
        and func.value.id in apps
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return None
    return node.args[0].value

def route_markers(tree):
    video = False
    health = False
    apps = flask_app_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            route = route_path(decorator, apps)
            if route == "/video":
                video = True
            if route in ("/health", "/healthz"):
                health = True
    return video, health

def main():
    try:
        st_l = os.lstat(SOURCE_PATH)
    except OSError:
        print(json.dumps(blocked("source_lstat_failed"), sort_keys=True, separators=(",", ":")))
        return
    if stat.S_ISLNK(st_l.st_mode):
        print(json.dumps(blocked("source_symlink"), sort_keys=True, separators=(",", ":")))
        return
    if not stat.S_ISREG(st_l.st_mode):
        print(json.dumps(blocked("source_not_regular"), sort_keys=True, separators=(",", ":")))
        return
    if st_l.st_mode & stat.S_IWOTH:
        print(json.dumps(blocked("source_world_writable"), sort_keys=True, separators=(",", ":")))
        return
    if st_l.st_size > MAX_SOURCE_BYTES:
        print(json.dumps(blocked("source_oversize"), sort_keys=True, separators=(",", ":")))
        return
    if not os.access(SOURCE_PATH, os.R_OK):
        print(json.dumps(blocked("source_unreadable"), sort_keys=True, separators=(",", ":")))
        return
    with open(SOURCE_PATH, "rb") as handle:
        source = handle.read(MAX_SOURCE_BYTES + 1)
    try:
        st_after = os.lstat(SOURCE_PATH)
    except OSError:
        print(json.dumps(blocked("source_lstat_changed"), sort_keys=True, separators=(",", ":")))
        return
    if not same_file(st_l, st_after):
        print(json.dumps(blocked("source_changed_during_read"), sort_keys=True, separators=(",", ":")))
        return
    if len(source) > MAX_SOURCE_BYTES:
        print(json.dumps(blocked("source_oversize"), sort_keys=True, separators=(",", ":")))
        return
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        print(json.dumps(blocked("source_not_utf8", source), sort_keys=True, separators=(",", ":")))
        return
    try:
        tree = ast.parse(text, filename=SOURCE_PATH)
        compile(tree, SOURCE_PATH, "exec")
    except SyntaxError:
        print(json.dumps(blocked("python_parse_failed", source), sort_keys=True, separators=(",", ":")))
        return
    if has_credential_literal(tree):
        print(json.dumps(blocked("suspicious_credential_literal", source), sort_keys=True, separators=(",", ":")))
        return
    video, health = route_markers(tree)
    lowered = text.lower()
    has_flask = any(
        isinstance(node, ast.ImportFrom) and node.module == "flask"
        or isinstance(node, ast.Import) and any(alias.name == "flask" for alias in node.names)
        for node in ast.walk(tree)
    )
    if not video:
        print(json.dumps(blocked("video_route_missing", source), sort_keys=True, separators=(",", ":")))
        return
    if not health:
        print(json.dumps(blocked("health_route_missing", source), sort_keys=True, separators=(",", ":")))
        return
    if not (has_flask and "skeleton" in lowered and "cast" in lowered and "media" in lowered):
        print(json.dumps(blocked("skeleton_cast_markers_missing", source), sort_keys=True, separators=(",", ":")))
        return
    sha = hashlib.sha256(source).hexdigest()
    print(json.dumps({{
        "public": {{
            "maintenance_task_id": TASK_ID,
            "source_identity": "verified",
            "source_version_marker": "v63" if re.search(r"(?i)\\bv63\\b", text) else "unknown",
            "source_bytes": len(source),
            "source_sha256": sha,
            "python_parse_status": "ok",
            "video_route_present": True,
            "health_route_present": True,
            "private_artifact_written": False,
            "private_artifact_hash_matches": False,
            "executor_receipt_hash": "pending",
            "stable_reason": "validated",
            "success_criteria": "pending",
        }},
        "private_source_b64": base64.b64encode(source).decode("ascii"),
    }}, sort_keys=True, separators=(",", ":")))

main()
'''
