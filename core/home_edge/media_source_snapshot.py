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
OPERATOR_APPROVAL_REF = "EXPLICIT_MINIMAL_HOME_EDGE_SNAPSHOT_ACCESS_REPAIR_2026_08_09"
PRIVATE_ARTIFACT_RELATIVE_PATH = (
    Path("home_edge") / "home_edge_01" / "media_source_snapshot" / "app.py.latest"
)
INSTALLED_SIGNER_EXECUTABLE = Path("/usr/local/libexec/skeleton/home-edge/media-source-snapshot/signer")
INSTALLED_SIGNER_PAYLOAD = Path("/usr/local/lib/skeleton/home-edge/media-source-snapshot/signer_payload.py")
SIGNER_SUDO_ARGV = ("sudo", "--non-interactive", str(INSTALLED_SIGNER_EXECUTABLE))
SIGNER_STDIN_MAX_BYTES = 256 * 1024
SIGNER_TIMEOUT_SECONDS = 10
TEST_RUNNER_HMAC_OVERRIDE_ENV = "SKELETON_HOME_EDGE_TEST_ALLOW_RUNNER_HMAC"
EXEC_HMAC_SECRET_CONFIG_DIR = Path("/etc/skeleton")
EXEC_HMAC_SECRET_PROFILE_METADATA_PATH = Path("/etc/skeleton/home-edge-01.env")
EXEC_HMAC_SECRET_CONFIG_PATH = Path("/etc/skeleton/home-edge-executor-controller.env")
MAX_EXEC_HMAC_SECRET_CONFIG_BYTES = 64 * 1024
EXPECTED_MAIN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FIELD_RE = re.compile(r"^\s*(?P<field>[A-Za-z][A-Za-z0-9 _-]{0,80}):\s*(?P<value>.*?)\s*$")
PUBLIC_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:=-]+$")
AUTH_CONFIG_REASON_RE = re.compile(
    r"^executor_auth_config_(?:missing|unsafe|invalid)$"
)
CONFIG_ASSIGNMENT_RE = re.compile(
    r"^(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)
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

    try:
        unsigned = build_snapshot_request(environment=environment)
        request = sign_snapshot_request(unsigned, environment=environment)
        _validate_signed_snapshot_request_for_transport(
            request,
            expected_unsigned=unsigned.to_mapping(include_signature=False),
        )
    except ValueError as exc:
        reason = exc.args[0] if exc.args else ""
        if isinstance(reason, str) and (
            AUTH_CONFIG_REASON_RE.fullmatch(reason) or reason.startswith("snapshot_signer_")
        ):
            return _blocked_receipt(reason)
        raise
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
    return HomeEdgeExecRequest.from_mapping(
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
            "operator_approval_ref": OPERATOR_APPROVAL_REF,
        }
    )


def sign_snapshot_request(
    unsigned_request: HomeEdgeExecRequest,
    *,
    environment: Mapping[str, str] | None = None,
) -> HomeEdgeExecRequest:
    unsigned = unsigned_request.to_mapping(include_signature=False)
    _validate_unsigned_snapshot_request_for_signing(unsigned)
    if _runner_hmac_override_allowed(environment=environment):
        secret = _resolve_exec_hmac_secret(environment=environment)
        return HomeEdgeExecRequest.from_mapping(
            {**unsigned, "signature": sign_request(unsigned_request, secret)}
        )
    return _sign_snapshot_request_with_installed_signer(unsigned)


def _runner_hmac_override_allowed(*, environment: Mapping[str, str] | None = None) -> bool:
    return bool(
        environment is not None
        and environment.get(TEST_RUNNER_HMAC_OVERRIDE_ENV) == "1"
        and environment.get(EXEC_HMAC_SECRET_ENV)
    )


def _sign_snapshot_request_with_installed_signer(unsigned: Mapping[str, Any]) -> HomeEdgeExecRequest:
    try:
        completed = subprocess.run(
            list(SIGNER_SUDO_ARGV),
            input=json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=SIGNER_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("snapshot_signer_unavailable") from None
    if completed.returncode != 0:
        reason = _installed_signer_error_reason(completed.stdout)
        if reason is not None:
            raise ValueError(reason)
        raise ValueError("snapshot_signer_rejected")
    if len(completed.stdout) > SIGNER_STDIN_MAX_BYTES:
        raise ValueError("snapshot_signer_rejected")
    try:
        signed = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("snapshot_signer_invalid_output") from None
    if not isinstance(signed, Mapping):
        raise ValueError("snapshot_signer_invalid_output")
    request = HomeEdgeExecRequest.from_mapping(signed)
    _validate_signed_snapshot_request_for_transport(request, expected_unsigned=unsigned)
    return request


def _installed_signer_error_reason(stdout: bytes) -> str | None:
    if not stdout or len(stdout) > 4096:
        return None
    try:
        decoded = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    reason = decoded.get("error")
    return reason if isinstance(reason, str) and AUTH_CONFIG_REASON_RE.fullmatch(reason) else None


def _validate_unsigned_snapshot_request_for_signing(request: Mapping[str, Any]) -> None:
    if request.get("operator_approval_ref") != OPERATOR_APPROVAL_REF:
        raise ValueError("snapshot_signer_operator_approval_mismatch")
    _validate_snapshot_authority(request, include_signature=False)


def _validate_signed_snapshot_request_for_transport(
    request: HomeEdgeExecRequest,
    *,
    expected_unsigned: Mapping[str, Any],
) -> None:
    signed = request.to_mapping(include_signature=True)
    unsigned = request.to_mapping(include_signature=False)
    if unsigned != dict(expected_unsigned):
        raise ValueError("snapshot_signer_signed_authority_mismatch")
    if signed.get("operator_approval_ref") != OPERATOR_APPROVAL_REF:
        raise ValueError("snapshot_signer_operator_approval_mismatch")
    if not request.signature:
        raise ValueError("snapshot_signer_missing_signature")
    _validate_snapshot_authority(signed, include_signature=True)


def _validate_snapshot_authority(request: Mapping[str, Any], *, include_signature: bool) -> None:
    required = {
        "schema",
        "request_id",
        "node_id",
        "argv",
        "environment",
        "timeout_seconds",
        "execution_lane",
        "operator_approval_ref",
        "idempotency_key",
        "run_as",
        "mode",
        "script",
        "script_interpreter",
        "timestamp",
        "nonce",
        "max_output_bytes",
        "public",
    }
    if include_signature:
        required.add("signature")
    if set(request) != required:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["schema"] != "skeleton.home_edge.exec_request.v1":
        raise ValueError("snapshot_signer_authority_mismatch")
    if not isinstance(request["request_id"], str) or not request["request_id"].startswith(TASK_ID + "-"):
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["node_id"] != TARGET_NODE:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["execution_lane"] != EXECUTION_LANE:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["operator_approval_ref"] != OPERATOR_APPROVAL_REF:
        raise ValueError("snapshot_signer_operator_approval_mismatch")
    if request["run_as"] != RUN_AS:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["mode"] != "script" or request["script"] != SNAPSHOT_SCRIPT:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["script_interpreter"] != "python3":
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["timeout_seconds"] != REQUEST_TIMEOUT_SECONDS:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["max_output_bytes"] != MAX_EXECUTOR_OUTPUT_BYTES:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["argv"] != [] or "cwd" in request or request["environment"] != {}:
        raise ValueError("snapshot_signer_authority_mismatch")
    if "stdin_text" in request or "stdin_base64" in request:
        raise ValueError("snapshot_signer_authority_mismatch")
    if request["public"] is not False:
        raise ValueError("snapshot_signer_authority_mismatch")
    if not isinstance(request["idempotency_key"], str) or not request["idempotency_key"].startswith(
        IDEMPOTENCY_KEY_PREFIX + "-"
    ):
        raise ValueError("snapshot_signer_authority_mismatch")
    if not isinstance(request["timestamp"], str) or not request["timestamp"]:
        raise ValueError("snapshot_signer_authority_mismatch")
    if not isinstance(request["nonce"], str) or not request["nonce"].startswith(TASK_ID + "-"):
        raise ValueError("snapshot_signer_authority_mismatch")
    if include_signature and (
        not isinstance(request["signature"], str)
        or not request["signature"].startswith("sha256=")
        or len(request["signature"]) != len("sha256=") + 64
    ):
        raise ValueError("snapshot_signer_authority_mismatch")


def _resolve_exec_hmac_secret(*, environment: Mapping[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    secret = env.get(EXEC_HMAC_SECRET_ENV, "")
    if secret:
        return secret
    return _read_exec_hmac_secret_config()


def _read_exec_hmac_secret_config() -> str:
    try:
        controller_st = _validate_exec_hmac_secret_config_path()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(EXEC_HMAC_SECRET_CONFIG_PATH, flags)
    except FileNotFoundError:
        raise ValueError("executor_auth_config_missing") from None
    except OSError:
        raise ValueError("executor_auth_config_unsafe") from None

    try:
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not _safe_exec_hmac_secret_config_file_metadata(before):
                raise ValueError("executor_auth_config_unsafe")
            data = handle.read(MAX_EXEC_HMAC_SECRET_CONFIG_BYTES + 1)
            after = os.fstat(handle.fileno())
    except ValueError:
        raise
    except OSError:
        raise ValueError("executor_auth_config_unsafe") from None

    if (
        _file_id(controller_st) != _file_id(before)
        or _file_id(before) != _file_id(after)
        or len(data) > MAX_EXEC_HMAC_SECRET_CONFIG_BYTES
    ):
        raise ValueError("executor_auth_config_unsafe")
    return _parse_exec_hmac_secret_config(data)


def _validate_exec_hmac_secret_config_path() -> os.stat_result:
    try:
        directory_st = EXEC_HMAC_SECRET_CONFIG_DIR.lstat()
        profile_st = EXEC_HMAC_SECRET_PROFILE_METADATA_PATH.lstat()
        controller_st = EXEC_HMAC_SECRET_CONFIG_PATH.lstat()
    except FileNotFoundError:
        raise ValueError("executor_auth_config_missing") from None
    except OSError:
        raise ValueError("executor_auth_config_unsafe") from None
    if not _safe_exec_hmac_secret_config_boundary(
        directory_st=directory_st,
        profile_st=profile_st,
        controller_st=controller_st,
    ):
        raise ValueError("executor_auth_config_unsafe")
    return controller_st


def _safe_exec_hmac_secret_config_boundary(
    *,
    directory_st: os.stat_result,
    profile_st: os.stat_result,
    controller_st: os.stat_result,
) -> bool:
    if stat.S_ISLNK(directory_st.st_mode) or not stat.S_ISDIR(directory_st.st_mode):
        return False
    if not _safe_exec_hmac_secret_config_file_metadata(profile_st):
        return False
    if not _safe_exec_hmac_secret_config_file_metadata(controller_st):
        return False
    if stat.S_IMODE(directory_st.st_mode) & 0o022:
        return False

    current_uid = os.getuid() if hasattr(os, "getuid") else None
    controller_owned_by_trusted_process = (
        controller_st.st_uid == 0
        or (current_uid is not None and controller_st.st_uid == current_uid)
    )
    coherent_private_controller = (
        directory_st.st_uid == profile_st.st_uid == controller_st.st_uid
        and directory_st.st_gid == profile_st.st_gid == controller_st.st_gid
    )
    return controller_owned_by_trusted_process or coherent_private_controller


def _safe_exec_hmac_secret_config_file_metadata(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    if st.st_size > MAX_EXEC_HMAC_SECRET_CONFIG_BYTES:
        return False
    if stat.S_IMODE(st.st_mode) & 0o022:
        return False
    return True


def _safe_exec_hmac_secret_config_stat(st: os.stat_result) -> bool:
    if not _safe_exec_hmac_secret_config_file_metadata(st):
        return False
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    return st.st_uid == 0 or (current_uid is not None and st.st_uid == current_uid)


def _parse_exec_hmac_secret_config(data: bytes) -> str:
    if b"\x00" in data:
        raise ValueError("executor_auth_config_invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("executor_auth_config_invalid") from None
    secret: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            raise ValueError("executor_auth_config_invalid")
        match = CONFIG_ASSIGNMENT_RE.fullmatch(line)
        if match is None:
            raise ValueError("executor_auth_config_invalid")
        if match.group("name") != EXEC_HMAC_SECRET_ENV:
            continue
        if secret is not None:
            raise ValueError("executor_auth_config_invalid")
        secret = _parse_exec_hmac_secret_config_value(match.group("value"))
    if not secret:
        raise ValueError("executor_auth_config_missing")
    return secret


def _parse_exec_hmac_secret_config_value(value: str) -> str:
    if "$" in value or "`" in value:
        raise ValueError("executor_auth_config_invalid")
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or not value.endswith(quote):
            raise ValueError("executor_auth_config_invalid")
        decoded = value[1:-1]
        if quote in decoded or "\\" in decoded:
            raise ValueError("executor_auth_config_invalid")
    elif "'" in value or '"' in value:
        raise ValueError("executor_auth_config_invalid")
    else:
        decoded = value
    if "\n" in decoded or "\r" in decoded or not decoded:
        raise ValueError("executor_auth_config_invalid")
    return decoded


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
        "uid": getattr(st, "st_uid", None),
        "gid": getattr(st, "st_gid", None),
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


def installed_signer_payload_source() -> str:
    return f'''#!/usr/bin/python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path

TASK_ID = {TASK_ID!r}
TARGET_NODE = {TARGET_NODE!r}
EXECUTION_LANE = {EXECUTION_LANE!r}
RUN_AS = {RUN_AS!r}
REQUEST_TIMEOUT_SECONDS = {REQUEST_TIMEOUT_SECONDS!r}
MAX_EXECUTOR_OUTPUT_BYTES = {MAX_EXECUTOR_OUTPUT_BYTES!r}
IDEMPOTENCY_KEY_PREFIX = {IDEMPOTENCY_KEY_PREFIX!r}
OPERATOR_APPROVAL_REF = {OPERATOR_APPROVAL_REF!r}
SNAPSHOT_SCRIPT = {SNAPSHOT_SCRIPT!r}
EXEC_HMAC_SECRET_ENV = {EXEC_HMAC_SECRET_ENV!r}
EXEC_HMAC_SECRET_CONFIG_DIR = Path({str(EXEC_HMAC_SECRET_CONFIG_DIR)!r})
EXEC_HMAC_SECRET_PROFILE_METADATA_PATH = Path({str(EXEC_HMAC_SECRET_PROFILE_METADATA_PATH)!r})
EXEC_HMAC_SECRET_CONFIG_PATH = Path({str(EXEC_HMAC_SECRET_CONFIG_PATH)!r})
MAX_EXEC_HMAC_SECRET_CONFIG_BYTES = {MAX_EXEC_HMAC_SECRET_CONFIG_BYTES!r}
SIGNER_STDIN_MAX_BYTES = {SIGNER_STDIN_MAX_BYTES!r}
CONFIG_ASSIGNMENT_RE = re.compile(r"^(?:export[ \\t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def fail(reason: str = "snapshot_signer_rejected") -> None:
    if reason.startswith("executor_auth_config_"):
        print(json.dumps({{"error": reason}}, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)


def file_id(st):
    return {{
        "mode": stat.S_IFMT(st.st_mode),
        "dev": getattr(st, "st_dev", None),
        "ino": getattr(st, "st_ino", None),
        "uid": getattr(st, "st_uid", None),
        "gid": getattr(st, "st_gid", None),
        "size": st.st_size,
        "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1000000000)),
    }}


def safe_config_file_metadata(st) -> bool:
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    if st.st_size > MAX_EXEC_HMAC_SECRET_CONFIG_BYTES:
        return False
    if stat.S_IMODE(st.st_mode) & 0o022:
        return False
    return True


def safe_config_boundary(directory_st, profile_st, controller_st) -> bool:
    if stat.S_ISLNK(directory_st.st_mode) or not stat.S_ISDIR(directory_st.st_mode):
        return False
    if not safe_config_file_metadata(profile_st) or not safe_config_file_metadata(controller_st):
        return False
    if stat.S_IMODE(directory_st.st_mode) & 0o022:
        return False
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    controller_owned_by_trusted_process = (
        controller_st.st_uid == 0
        or (current_uid is not None and controller_st.st_uid == current_uid)
    )
    coherent_private_controller = (
        directory_st.st_uid == profile_st.st_uid == controller_st.st_uid
        and directory_st.st_gid == profile_st.st_gid == controller_st.st_gid
    )
    return controller_owned_by_trusted_process or coherent_private_controller


def read_secret() -> str:
    try:
        directory_st = EXEC_HMAC_SECRET_CONFIG_DIR.lstat()
        profile_st = EXEC_HMAC_SECRET_PROFILE_METADATA_PATH.lstat()
        controller_st = EXEC_HMAC_SECRET_CONFIG_PATH.lstat()
    except FileNotFoundError:
        fail("executor_auth_config_missing")
    except OSError:
        fail("executor_auth_config_unsafe")
    if not safe_config_boundary(directory_st, profile_st, controller_st):
        fail("executor_auth_config_unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(EXEC_HMAC_SECRET_CONFIG_PATH, flags)
    except OSError:
        fail("executor_auth_config_unsafe")
    try:
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not safe_config_file_metadata(before):
                fail("executor_auth_config_unsafe")
            data = handle.read(MAX_EXEC_HMAC_SECRET_CONFIG_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError:
        fail("executor_auth_config_unsafe")
    if file_id(controller_st) != file_id(before) or file_id(before) != file_id(after):
        fail("executor_auth_config_unsafe")
    if len(data) > MAX_EXEC_HMAC_SECRET_CONFIG_BYTES:
        fail("executor_auth_config_unsafe")
    if b"\\x00" in data:
        fail("executor_auth_config_invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fail("executor_auth_config_invalid")
    secret = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\\\"):
            fail("executor_auth_config_invalid")
        match = CONFIG_ASSIGNMENT_RE.fullmatch(line)
        if match is None:
            fail("executor_auth_config_invalid")
        if match.group("name") != EXEC_HMAC_SECRET_ENV:
            continue
        if secret is not None:
            fail("executor_auth_config_invalid")
        secret = parse_value(match.group("value"))
    if not secret:
        fail("executor_auth_config_missing")
    return secret


def parse_value(value: str) -> str:
    if "$" in value or "`" in value:
        fail("executor_auth_config_invalid")
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or not value.endswith(quote):
            fail("executor_auth_config_invalid")
        decoded = value[1:-1]
        if quote in decoded or "\\\\" in decoded:
            fail("executor_auth_config_invalid")
    elif "'" in value or '"' in value:
        fail("executor_auth_config_invalid")
    else:
        decoded = value
    if "\\n" in decoded or "\\r" in decoded or not decoded:
        fail("executor_auth_config_invalid")
    return decoded


def validate_authority(request: dict) -> None:
    required = {{
        "schema",
        "request_id",
        "node_id",
        "argv",
        "environment",
        "timeout_seconds",
        "execution_lane",
        "operator_approval_ref",
        "idempotency_key",
        "run_as",
        "mode",
        "script",
        "script_interpreter",
        "timestamp",
        "nonce",
        "max_output_bytes",
        "public",
    }}
    if set(request) != required:
        fail()
    if request.get("operator_approval_ref") != OPERATOR_APPROVAL_REF:
        fail()
    if request.get("schema") != "skeleton.home_edge.exec_request.v1":
        fail()
    if not isinstance(request.get("request_id"), str) or not request["request_id"].startswith(TASK_ID + "-"):
        fail()
    if request.get("node_id") != TARGET_NODE or request.get("execution_lane") != EXECUTION_LANE:
        fail()
    if request.get("run_as") != RUN_AS or request.get("mode") != "script":
        fail()
    if request.get("script") != SNAPSHOT_SCRIPT or request.get("script_interpreter") != "python3":
        fail()
    if request.get("timeout_seconds") != REQUEST_TIMEOUT_SECONDS:
        fail()
    if request.get("max_output_bytes") != MAX_EXECUTOR_OUTPUT_BYTES:
        fail()
    if request.get("argv") != [] or "cwd" in request or request.get("environment") != {{}}:
        fail()
    if request.get("public") is not False:
        fail()
    if not isinstance(request.get("idempotency_key"), str) or not request["idempotency_key"].startswith(IDEMPOTENCY_KEY_PREFIX + "-"):
        fail()
    if not isinstance(request.get("timestamp"), str) or not request["timestamp"]:
        fail()
    if not isinstance(request.get("nonce"), str) or not request["nonce"].startswith(TASK_ID + "-"):
        fail()


def sign(request: dict, secret: str) -> str:
    canonical = dict(request)
    canonical.pop("public", None)
    message = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def main() -> None:
    if len(sys.argv) != 1:
        fail()
    data = sys.stdin.buffer.read(SIGNER_STDIN_MAX_BYTES + 1)
    if not data or len(data) > SIGNER_STDIN_MAX_BYTES:
        fail()
    try:
        request = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail()
    if not isinstance(request, dict):
        fail()
    validate_authority(request)
    signed = dict(request)
    signed["signature"] = sign(request, read_secret())
    print(json.dumps(signed, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''


def installed_signer_wrapper_source() -> str:
    return f'''#!/bin/sh
set -efu
if [ "$#" -ne 0 ]; then
  exit 2
fi
exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /usr/bin/python3 {INSTALLED_SIGNER_PAYLOAD}
'''
