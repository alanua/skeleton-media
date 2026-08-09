from __future__ import annotations

import base64
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import pytest

from core.home_edge import media_source_snapshot as snapshot
from core.home_edge.executor import HomeEdgeExecReceipt, HomeEdgeExecRequest, sign_request


SHA = "a" * 40
SECRET = "test-home-edge-secret"
PRIVATE_MARKER = "10.44.55.66"


def issue_body(**updates: str) -> str:
    fields = {
        "Mode": "RUNTIME_MAINTENANCE_TASK",
        "Maintenance Task ID": snapshot.TASK_ID,
        "Repository": snapshot.REPOSITORY,
        "Expected Main SHA": SHA,
        "Target": snapshot.TARGET_NODE,
    }
    fields.update(updates)
    return "\n".join(f"{key}: {value}" for key, value in fields.items())


def valid_source(*, route_style: str = "get", marker: str = PRIVATE_MARKER, padding: int = 0) -> bytes:
    route = "get" if route_style == "get" else "route"
    return f'''from flask import Flask

app = Flask("skeleton-cast-media")
SKELETON_CAST_VERSION = "v63"
MEDIA_PRIVATE_RUNTIME = "{marker}"
PAD = "{'x' * padding}"

@app.{route}("/video")
def video():
    return "skeleton cast media"

@app.{route}("/health")
def health():
    return {{"service": "skeleton-cast", "status": "ok"}}
'''.encode()


def executor_stdout(source: bytes) -> str:
    local = snapshot._validate_source_bytes(source)
    assert local["source_identity"] == "verified"
    public = dict(local)
    public["executor_receipt_hash"] = "pending"
    return json.dumps(
        {
            "public": public,
            "private_source_b64": base64.b64encode(source).decode("ascii"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def executor_receipt(stdout: str) -> HomeEdgeExecReceipt:
    now = datetime.now(UTC).isoformat()
    return HomeEdgeExecReceipt(
        status="ok",
        request_id="req",
        node_id=snapshot.TARGET_NODE,
        execution_lane=snapshot.EXECUTION_LANE,
        exit_code=0,
        stdout=stdout,
        stderr="",
        started_at=now,
        finished_at=now,
        duration_seconds=0.01,
        idempotency="executed",
        receipt_hash="f" * 64,
    )


def test_exact_fixed_task_path_node_lane_run_as_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)

    first = snapshot.build_snapshot_request()
    second = snapshot.build_snapshot_request()

    assert snapshot.TASK_ID == "home_edge_01_media_source_snapshot_v1"
    assert snapshot.SOURCE_PATH == "/opt/skeleton/cast/app.py"
    assert snapshot.MAX_SOURCE_BYTES == 700 * 1024
    assert snapshot.SOURCE_PATH in first.script
    assert first.node_id == "home-edge-01"
    assert first.execution_lane.value == "read_only"
    assert first.run_as.value == "desktop-user"
    assert first.timeout_seconds == 30
    assert first.argv == ()
    assert first.mode.value == "script"
    assert first.script_interpreter == "python3"
    assert first.max_output_bytes == 1_000_000
    assert first.idempotency_key.startswith(snapshot.IDEMPOTENCY_KEY_PREFIX + "-")
    assert second.idempotency_key.startswith(snapshot.IDEMPOTENCY_KEY_PREFIX + "-")
    assert first.idempotency_key != second.idempotency_key
    assert first.request_id != second.request_id
    assert first.nonce != second.nonce
    assert first.signature == sign_request(first, SECRET)
    assert second.signature == sign_request(second, SECRET)


@pytest.mark.parametrize(
    "body,reason",
    [
        (issue_body(**{"Expected Main SHA": "A" * 40}), "expected_main_sha_malformed"),
        (issue_body() + "\nExpected Main SHA: " + SHA, "duplicate_runtime_input_field"),
        (issue_body() + "\nPath: /tmp/evil.py", "unknown_runtime_input_field"),
        (issue_body() + "\nScript: print(1)", "unknown_runtime_input_field"),
        (issue_body() + "\nOutput Path: /tmp/out", "unknown_runtime_input_field"),
        (issue_body(Target="home-edge-02"), "target_mismatch"),
        (issue_body() + "\nTimeout: 1", "unknown_runtime_input_field"),
        (issue_body() + "\nLane: destructive", "unknown_runtime_input_field"),
        (issue_body() + "\nRun As: root", "unknown_runtime_input_field"),
    ],
)
def test_malformed_unknown_issue_fields_cannot_change_behavior(body: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        snapshot.parse_runtime_input(body)


def test_current_style_app_get_routes_validate() -> None:
    receipt = snapshot._validate_source_bytes(valid_source(route_style="get"))

    assert receipt["source_identity"] == "verified"
    assert receipt["source_version_marker"] == "v63"
    assert receipt["video_route_present"] is True
    assert receipt["health_route_present"] is True


def test_app_route_remains_accepted_if_valid() -> None:
    receipt = snapshot._validate_source_bytes(valid_source(route_style="route"))

    assert receipt["source_identity"] == "verified"


@pytest.mark.parametrize(
    "source,reason",
    [
        (b"def nope(:\n", "python_parse_failed"),
        (b"print('not skeleton cast media')\n", "video_route_missing"),
        (
            b"from flask import Flask\napp=Flask('skeleton-cast-media')\n@app.get('/video')\ndef video(): return 'x'\n",
            "health_route_missing",
        ),
        (
            b"from flask import Flask\napi=Flask('skeleton-cast-media')\n@app.get('/video')\ndef video(): return 'x'\n@app.get('/health')\ndef health(): return 'ok'\n",
            "video_route_missing",
        ),
        (
            b"from flask import Flask\nclass Fake:\n def get(self, path): return lambda fn: fn\napp=Fake()\n@app.get('/video')\ndef video(): return 'skeleton cast media'\n@app.get('/health')\ndef health(): return 'ok'\n",
            "video_route_missing",
        ),
        (
            b"from flask import Flask\napp=Flask('x')\n@app.get('/video')\ndef video(): return 'x'\n@app.get('/health')\ndef health(): return 'ok'\n",
            "skeleton_cast_markers_missing",
        ),
    ],
)
def test_invalid_sources_block_before_artifact_publication(source: bytes, reason: str) -> None:
    receipt = snapshot._validate_source_bytes(source)

    assert receipt["stable_reason"] == reason
    assert receipt["private_artifact_written"] is False


@pytest.mark.parametrize(
    "name",
    [
        "TMDB_API_KEY",
        "BRAVE_API_KEY",
        "SOME_API_KEY",
        "ACCESS_TOKEN",
        "AUTH_TOKEN",
        "BOT_TOKEN",
        "HMAC_SECRET",
        "SECRET_KEY",
        "SERVICE_SECRET",
        "PASSWD",
        "MY_CREDENTIAL",
        "TMDB_KEY",
        "BRAVE_KEY",
        "MY_TMDB_KEY",
        "SERVICE_BRAVE_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "SKELETON_SECRET",
        "CLIENT_SECRET",
        "PASSWORD",
        "PRIVATE_KEY",
        "MY_TMDB_API_KEY",
        "service_openai_api_key",
        "payment_client_secret",
        "database_password",
        "ssh_private_key",
    ],
)
def test_prefixed_quoted_credential_literals_block_without_public_leak(name: str) -> None:
    literal = "live-secret-value-12345"
    source = valid_source() + f"\n{name} = {literal!r}\n".encode()

    receipt = snapshot._validate_source_bytes(source)
    public = json.dumps(receipt, sort_keys=True)

    assert receipt["stable_reason"] == "suspicious_credential_literal"
    assert literal not in public
    assert name not in public


def test_sensitive_dict_entry_blocks_without_public_identifier_or_value() -> None:
    literal = "live-secret-value-12345"
    source = valid_source() + f"\nCONFIG = {{'api_key': {literal!r}}}\n".encode()

    receipt = snapshot._validate_source_bytes(source)
    public = json.dumps(receipt, sort_keys=True)

    assert receipt["stable_reason"] == "suspicious_credential_literal"
    assert literal not in public
    assert "api_key" not in public


def test_remote_scanner_blocks_generic_identifiers_and_sensitive_dict_entries() -> None:
    namespace: dict[str, Any] = {}
    exec(snapshot.SNAPSHOT_SCRIPT.rsplit("\nmain()", 1)[0], namespace)

    tree = snapshot.ast.parse(
        "BRAVE_API_KEY = 'live-secret-value-12345'\n"
        "CONFIG = {'api_key': 'live-secret-value-12345'}\n"
    )

    assert namespace["has_credential_literal"](tree) is True


def test_environment_reference_credentials_and_placeholders_do_not_false_positive() -> None:
    source = (
        valid_source()
        + b"\nimport os\n"
        + b"TMDB_API_KEY = os.environ['TMDB_API_KEY']\n"
        + b"BRAVE_API_KEY = os.getenv('BRAVE_API_KEY')\n"
        + b"CONFIG = {'api_key': os.getenv('SERVICE_API_KEY')}\n"
        + b"OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')\n"
        + b"TELEGRAM_BOT_TOKEN = 'test-token'\n"
        + b"SKELETON_SECRET = ''\n"
        + b"CLIENT_SECRET = 'placeholder'\n"
        + b"PASSWORD = 'dummy-password'\n"
        + b"PRIVATE_KEY = 'replace_me'\n"
    )

    receipt = snapshot._validate_source_bytes(source)

    assert receipt["source_identity"] == "verified"


def test_source_size_bound_fits_base64_json_executor_cap_and_one_byte_over_blocks() -> None:
    source = valid_source(padding=1)
    base_len = len(source) - 1
    max_source = valid_source(padding=snapshot.MAX_SOURCE_BYTES - base_len)
    assert len(max_source) == snapshot.MAX_SOURCE_BYTES

    stdout = executor_stdout(max_source)
    assert len(stdout.encode("utf-8")) < snapshot.MAX_EXECUTOR_OUTPUT_BYTES
    assert snapshot._validate_source_bytes(max_source)["source_identity"] == "verified"
    assert snapshot._validate_source_bytes(max_source + b"x")["stable_reason"] == "source_oversize"


def test_execute_uses_only_signed_executor_gateway_and_writes_private_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Mapping[str, Any]] = []
    source = valid_source()
    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)

    def fake_execute(request: Mapping[str, Any]) -> HomeEdgeExecReceipt:
        calls.append(request)
        return executor_receipt(executor_stdout(source))

    monkeypatch.setattr(snapshot, "execute_home_edge_request", fake_execute)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )

    assert snapshot.success_criteria_met(receipt)
    assert len(calls) == 1
    request = HomeEdgeExecRequest.from_mapping(calls[0])
    assert request.signature == sign_request(request, SECRET)
    assert request.node_id == snapshot.TARGET_NODE
    assert request.execution_lane.value == snapshot.EXECUTION_LANE
    assert request.run_as.value == snapshot.RUN_AS
    assert request.idempotency_key.startswith(snapshot.IDEMPOTENCY_KEY_PREFIX + "-")
    artifact = snapshot.private_artifact_path(private_root=tmp_path)
    assert artifact.read_bytes() == source
    assert artifact.stat().st_size == len(source)
    assert snapshot._sha256_file(artifact) == receipt["source_sha256"]
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700


def test_second_execute_uses_existing_artifact_without_home_edge_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Mapping[str, Any]] = []
    source = valid_source()
    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)

    def fake_execute(request: Mapping[str, Any]) -> HomeEdgeExecReceipt:
        calls.append(request)
        return executor_receipt(executor_stdout(source))

    monkeypatch.setattr(snapshot, "execute_home_edge_request", fake_execute)

    first = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )
    second = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )

    assert snapshot.success_criteria_met(first)
    assert snapshot.success_criteria_met(second)
    assert len(calls) == 1
    assert second["stable_reason"] == "already_captured"
    assert second["executor_receipt_hash"] == "not_required_existing_capture"
    assert second["source_sha256"] == first["source_sha256"]
    assert second["source_bytes"] == first["source_bytes"]


@pytest.mark.parametrize(
    "kind",
    ["invalid", "oversize", "symlink", "mode"],
)
def test_suspicious_existing_artifact_fails_closed_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    artifact = snapshot.private_artifact_path(private_root=tmp_path)
    artifact.parent.mkdir(mode=0o700, parents=True)
    if kind == "invalid":
        artifact.write_bytes(b"print('not skeleton cast media')\n")
        os.chmod(artifact, 0o600)
    elif kind == "oversize":
        artifact.write_bytes(b"x" * (snapshot.MAX_SOURCE_BYTES + 1))
        os.chmod(artifact, 0o600)
    elif kind == "symlink":
        target = tmp_path / "target.py"
        target.write_bytes(valid_source())
        os.symlink(target, artifact)
    else:
        artifact.write_bytes(valid_source())
        os.chmod(artifact, 0o644)

    def fake_execute(_request: Mapping[str, Any]) -> HomeEdgeExecReceipt:
        raise AssertionError("executor must not be called")

    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)
    monkeypatch.setattr(snapshot, "execute_home_edge_request", fake_execute)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )

    assert receipt["success_criteria"] == "not_met"
    assert receipt["private_artifact_written"] is False
    if kind == "mode":
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o644
    elif kind != "symlink":
        assert artifact.read_bytes() != valid_source()


def test_ambiguous_transport_failure_does_not_write_artifact_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)

    def fake_execute(_request: Mapping[str, Any]) -> HomeEdgeExecReceipt:
        nonlocal calls
        calls += 1
        raise TimeoutError("ambiguous")

    monkeypatch.setattr(snapshot, "execute_home_edge_request", fake_execute)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )

    assert calls == 1
    assert receipt["stable_reason"] == "executor_transport_timeout"
    assert not snapshot.private_artifact_path(private_root=tmp_path).exists()


def test_remote_script_checks_file_safety_before_export() -> None:
    script = snapshot.SNAPSHOT_SCRIPT

    assert "os.lstat(SOURCE_PATH)" in script
    assert "same_file(st_l, st_after)" in script
    assert "source_changed_during_read" in script
    assert "stat.S_ISLNK" in script
    assert "stat.S_ISREG" in script
    assert "stat.S_IWOTH" in script
    assert "os.access(SOURCE_PATH, os.R_OK)" in script
    assert "has_credential_literal(tree)" in script
    assert "base64.b64encode(source)" in script
    assert "private_source_b64" in script
    assert "ssh " not in script.lower()
    assert "sudo" not in script.lower()


def test_simulated_file_change_between_pre_post_metadata_blocks() -> None:
    namespace: dict[str, Any] = {}
    exec(snapshot.SNAPSHOT_SCRIPT.rsplit("\nmain()", 1)[0], namespace)

    class St:
        st_mode = stat.S_IFREG | 0o644
        st_dev = 1
        st_ino = 2
        st_size = 10
        st_mtime_ns = 100
        st_mtime = 0.0

    before = St()
    after = St()
    after.st_size = 11

    assert namespace["same_file"](before, before) is True
    assert namespace["same_file"](before, after) is False


def test_artifact_hash_size_mismatch_blocks_fresh_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = snapshot.private_artifact_path(private_root=tmp_path)
    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)
    monkeypatch.setattr(snapshot, "_sha256_file", lambda _path: "0" * 64)
    monkeypatch.setattr(
        snapshot,
        "execute_home_edge_request",
        lambda _request: executor_receipt(executor_stdout(valid_source())),
    )

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )

    assert receipt["stable_reason"] == "private_artifact_hash_mismatch"
    assert receipt["private_artifact_written"] is False
    assert not artifact.exists()


def test_public_receipt_and_status_lines_exclude_source_text_private_markers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = valid_source(marker=PRIVATE_MARKER)
    monkeypatch.setenv(snapshot.EXEC_HMAC_SECRET_ENV, SECRET)
    monkeypatch.setattr(
        snapshot,
        "execute_home_edge_request",
        lambda _request: executor_receipt(executor_stdout(source)),
    )

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path,
    )
    public = json.dumps(receipt, sort_keys=True) + "\n".join(snapshot.receipt_status_lines(receipt))

    assert "private_source_b64" not in public
    assert "Flask" not in public
    assert "skeleton-cast-media" not in public
    assert PRIVATE_MARKER not in public
    assert snapshot.SOURCE_PATH not in public
    assert snapshot.SOURCE_IDENTITY_TOKEN not in public
    assert "source_sha256" in public
    assert "source_bytes" in public
