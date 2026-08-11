#!/usr/bin/python3
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

TASK_ID = "home_edge_01_media_source_snapshot_v1"
TARGET_NODE = "home-edge-01"
EXECUTION_LANE = "read_only"
RUN_AS = "desktop-user"
REQUEST_TIMEOUT_SECONDS = 30
MAX_EXECUTOR_OUTPUT_BYTES = 1_000_000
IDEMPOTENCY_KEY_PREFIX = "home-edge-01-media-source-snapshot-v1"
OPERATOR_APPROVAL_REF = "EXPLICIT_MINIMAL_HOME_EDGE_SNAPSHOT_ACCESS_REPAIR_2026_08_09"
EXEC_HMAC_SECRET_ENV = "SKELETON_HOME_EDGE_EXEC_HMAC_SECRET"
EXEC_HMAC_SECRET_CONFIG_DIR = Path("/etc/skeleton")
EXEC_HMAC_SECRET_PROFILE_METADATA_PATH = Path("/etc/skeleton/home-edge-01.env")
EXEC_HMAC_SECRET_CONFIG_PATH = Path("/etc/skeleton/home-edge-executor-controller.env")
CONTRACT_SOURCE = Path("/usr/local/lib/skeleton/home-edge/media-source-snapshot/contract_source.py")
CONTRACT_GIT_BLOB_SHA = "7ddfbfc54d55eccbbd95c73ad0016221e7222cd9"
MAX_EXEC_HMAC_SECRET_CONFIG_BYTES = 64 * 1024
MAX_CONTRACT_SOURCE_BYTES = 256 * 1024
SIGNER_STDIN_MAX_BYTES = 256 * 1024
CONFIG_ASSIGNMENT_RE = re.compile(r"^(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
AUTH_CONFIG_REASON_RE = re.compile(r"^executor_auth_config_(?:missing|unsafe|invalid)$")


def fail(reason: str = "snapshot_signer_rejected") -> None:
    if AUTH_CONFIG_REASON_RE.fullmatch(reason):
        print(json.dumps({"error": reason}, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)


def _file_id(st: os.stat_result) -> tuple[int, int | None, int | None, int | None, int | None, int, int]:
    return (
        st.st_mode,
        getattr(st, "st_dev", None),
        getattr(st, "st_ino", None),
        getattr(st, "st_uid", None),
        getattr(st, "st_gid", None),
        st.st_size,
        getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)),
    )


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _safe_regular(st: os.stat_result, *, max_bytes: int, require_root: bool = False) -> bool:
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    if st.st_size <= 0 or st.st_size > max_bytes:
        return False
    if stat.S_IMODE(st.st_mode) & 0o022:
        return False
    if require_root and getattr(st, "st_uid", None) != 0:
        return False
    return True


def _safe_config_boundary(directory_st: os.stat_result, profile_st: os.stat_result, controller_st: os.stat_result) -> bool:
    if stat.S_ISLNK(directory_st.st_mode) or not stat.S_ISDIR(directory_st.st_mode):
        return False
    if stat.S_IMODE(directory_st.st_mode) & 0o022:
        return False
    if not _safe_regular(profile_st, max_bytes=MAX_EXEC_HMAC_SECRET_CONFIG_BYTES):
        return False
    if not _safe_regular(controller_st, max_bytes=MAX_EXEC_HMAC_SECRET_CONFIG_BYTES):
        return False
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    trusted_owner = controller_st.st_uid == 0 or (current_uid is not None and controller_st.st_uid == current_uid)
    coherent_boundary = (
        directory_st.st_uid == profile_st.st_uid == controller_st.st_uid
        and directory_st.st_gid == profile_st.st_gid == controller_st.st_gid
    )
    return trusted_owner or coherent_boundary


def _parse_value(value: str) -> str:
    if "$" in value or "`" in value:
        fail("executor_auth_config_invalid")
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or not value.endswith(quote):
            fail("executor_auth_config_invalid")
        decoded = value[1:-1]
        if quote in decoded or "\\" in decoded:
            fail("executor_auth_config_invalid")
    elif "'" in value or '"' in value:
        fail("executor_auth_config_invalid")
    else:
        decoded = value
    if "\n" in decoded or "\r" in decoded or not decoded:
        fail("executor_auth_config_invalid")
    return decoded


def read_secret() -> str:
    try:
        directory_st = EXEC_HMAC_SECRET_CONFIG_DIR.lstat()
        profile_st = EXEC_HMAC_SECRET_PROFILE_METADATA_PATH.lstat()
        controller_st = EXEC_HMAC_SECRET_CONFIG_PATH.lstat()
    except FileNotFoundError:
        fail("executor_auth_config_missing")
    except OSError:
        fail("executor_auth_config_unsafe")
    if not _safe_config_boundary(directory_st, profile_st, controller_st):
        fail("executor_auth_config_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(EXEC_HMAC_SECRET_CONFIG_PATH, flags)
    except OSError:
        fail("executor_auth_config_unsafe")
    try:
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not _safe_regular(before, max_bytes=MAX_EXEC_HMAC_SECRET_CONFIG_BYTES):
                fail("executor_auth_config_unsafe")
            data = handle.read(MAX_EXEC_HMAC_SECRET_CONFIG_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError:
        fail("executor_auth_config_unsafe")
    if _file_id(controller_st) != _file_id(before) or _file_id(before) != _file_id(after):
        fail("executor_auth_config_unsafe")
    if len(data) > MAX_EXEC_HMAC_SECRET_CONFIG_BYTES:
        fail("executor_auth_config_unsafe")
    if b"\x00" in data:
        fail("executor_auth_config_invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fail("executor_auth_config_invalid")
    secret: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            fail("executor_auth_config_invalid")
        match = CONFIG_ASSIGNMENT_RE.fullmatch(line)
        if match is None:
            fail("executor_auth_config_invalid")
        if match.group("name") != EXEC_HMAC_SECRET_ENV:
            continue
        if secret is not None:
            fail("executor_auth_config_invalid")
        secret = _parse_value(match.group("value"))
    if not secret:
        fail("executor_auth_config_missing")
    return secret


def _normalize_secret_suffixes(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") for name in names)


def _safe_constant(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_safe_constant(item, values) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _safe_constant(node.left, values)
        right = _safe_constant(node.right, values)
        if isinstance(left, int) and isinstance(right, int):
            return left * right
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tuple"
        and len(node.args) == 1
        and not node.keywords
    ):
        return tuple(_safe_constant(node.args[0], values))
    raise ValueError("unsafe_contract_expression")


def _render_joined_string(node: ast.JoinedStr, values: dict[str, Any]) -> str:
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
            continue
        if not isinstance(part, ast.FormattedValue) or part.format_spec is not None:
            raise ValueError("unsafe_contract_fstring")
        value = _safe_constant(part.value, values)
        if part.conversion == ord("r"):
            parts.append(repr(value))
        elif part.conversion == -1:
            parts.append(str(value))
        else:
            raise ValueError("unsafe_contract_fstring")
    return "".join(parts)


def expected_snapshot_script() -> str:
    try:
        st_l = CONTRACT_SOURCE.lstat()
    except OSError:
        fail()
    if not _safe_regular(st_l, max_bytes=MAX_CONTRACT_SOURCE_BYTES, require_root=True):
        fail()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(CONTRACT_SOURCE, flags)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not _safe_regular(before, max_bytes=MAX_CONTRACT_SOURCE_BYTES, require_root=True):
                fail()
            data = handle.read(MAX_CONTRACT_SOURCE_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError:
        fail()
    if (
        len(data) > MAX_CONTRACT_SOURCE_BYTES
        or not _safe_regular(after, max_bytes=MAX_CONTRACT_SOURCE_BYTES, require_root=True)
        or _file_id(st_l) != _file_id(before)
        or _file_id(before) != _file_id(after)
        or _git_blob_sha1(data) != CONTRACT_GIT_BLOB_SHA
    ):
        fail()
    try:
        tree = ast.parse(data.decode("utf-8"), filename=str(CONTRACT_SOURCE))
    except (UnicodeDecodeError, SyntaxError):
        fail()
    values: dict[str, Any] = {}
    script_node: ast.JoinedStr | None = None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            continue
        name = statement.targets[0].id
        if name in {"TASK_ID", "SOURCE_PATH", "MAX_SOURCE_BYTES", "SECRET_NAME_TOKENS"}:
            try:
                values[name] = _safe_constant(statement.value, values)
            except ValueError:
                fail()
        elif name == "SNAPSHOT_SCRIPT" and isinstance(statement.value, ast.JoinedStr):
            script_node = statement.value
    names = values.get("SECRET_NAME_TOKENS")
    if not isinstance(names, tuple) or not all(isinstance(item, str) for item in names):
        fail()
    values["_SECRET_SUFFIXES"] = _normalize_secret_suffixes(names)
    if values.get("TASK_ID") != TASK_ID or script_node is None:
        fail()
    try:
        return _render_joined_string(script_node, values)
    except ValueError:
        fail()
    raise AssertionError("unreachable")


def validate_authority(request: dict[str, Any]) -> None:
    required = {
        "schema", "request_id", "node_id", "argv", "environment", "timeout_seconds",
        "execution_lane", "operator_approval_ref", "idempotency_key", "run_as", "mode",
        "script", "script_interpreter", "timestamp", "nonce", "max_output_bytes", "public",
    }
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
    if request.get("script_interpreter") != "python3" or request.get("script") != expected_snapshot_script():
        fail()
    if request.get("timeout_seconds") != REQUEST_TIMEOUT_SECONDS or request.get("max_output_bytes") != MAX_EXECUTOR_OUTPUT_BYTES:
        fail()
    if request.get("argv") != [] or request.get("environment") != {}:
        fail()
    if request.get("public") is not False:
        fail()
    if not isinstance(request.get("idempotency_key"), str) or not request["idempotency_key"].startswith(IDEMPOTENCY_KEY_PREFIX + "-"):
        fail()
    if not isinstance(request.get("timestamp"), str) or not request["timestamp"]:
        fail()
    if not isinstance(request.get("nonce"), str) or not request["nonce"].startswith(TASK_ID + "-"):
        fail()


def sign(request: dict[str, Any], secret: str) -> str:
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
    secret = read_secret()
    signed = dict(request)
    signed["signature"] = sign(request, secret)
    print(json.dumps(signed, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
