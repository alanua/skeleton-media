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
CONFIG_SECRET = "synthetic-config-signing-key"
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


def _write_file(path: Path, content: bytes | str, *, mode: int = 0o600) -> None:
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    os.chmod(path, mode)


def _stat_with(st: os.stat_result, **updates: int) -> os.stat_result:
    values = list(st)
    index = {
        "st_mode": 0,
        "st_ino": 1,
        "st_dev": 2,
        "st_uid": 4,
        "st_gid": 5,
        "st_size": 6,
    }
    for name, value in updates.items():
        values[index[name]] = value
    return os.stat_result(values)


class FixedHmacFs:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        controller_content: bytes | str = f"{snapshot.EXEC_HMAC_SECRET_ENV}={CONFIG_SECRET}\n",
        directory_kind: str = "dir",
        profile_kind: str = "file",
        controller_kind: str = "file",
        directory_mode: int = 0o700,
        profile_mode: int = 0o600,
        controller_mode: int = 0o600,
        uid: int | None = None,
        gid: int | None = None,
        directory_uid: int | None = None,
        profile_uid: int | None = None,
        controller_uid: int | None = None,
        directory_gid: int | None = None,
        profile_gid: int | None = None,
        controller_gid: int | None = None,
        profile_size: int | None = None,
        controller_size: int | None = None,
        controller_missing: bool = False,
        deny_controller_open: bool = False,
        replacement_race: bool = False,
    ) -> None:
        self.open_calls: list[Path] = []
        self.lstat_calls: list[Path] = []
        self._open_fds: dict[int, int] = {}
        self._deny_controller_open = deny_controller_open
        self._replacement_race = replacement_race
        self._real_lstat = Path.lstat
        self._real_open = os.open
        self._real_fstat = os.fstat

        self.root = tmp_path / "fixed-hmac"
        self.root.mkdir()
        self.directory = self.root / "etc-skeleton"
        self.profile = self.root / "home-edge-01.env"
        self.controller = self.root / "home-edge-executor-controller.env"

        if directory_kind == "dir":
            self.directory.mkdir()
            os.chmod(self.directory, directory_mode)
        elif directory_kind == "file":
            _write_file(self.directory, "not a directory", mode=directory_mode)
        elif directory_kind == "symlink":
            target = self.root / "directory-target"
            target.mkdir()
            os.symlink(target, self.directory)

        profile_target = self.root / "profile-target"
        _write_file(profile_target, "profile content must stay unread\n")
        if profile_kind == "file":
            _write_file(self.profile, "profile content must stay unread\n", mode=profile_mode)
        elif profile_kind == "dir":
            self.profile.mkdir()
            os.chmod(self.profile, profile_mode)
        elif profile_kind == "symlink":
            os.symlink(profile_target, self.profile)

        controller_target = self.root / "controller-target"
        _write_file(controller_target, controller_content, mode=controller_mode)
        if not controller_missing:
            if controller_kind == "file":
                _write_file(self.controller, controller_content, mode=controller_mode)
            elif controller_kind == "dir":
                self.controller.mkdir()
                os.chmod(self.controller, controller_mode)
            elif controller_kind == "symlink":
                os.symlink(controller_target, self.controller)

        current_uid = os.getuid() if hasattr(os, "getuid") else 1
        base_uid = current_uid if uid is None else uid
        base_gid = os.getgid() if gid is None and hasattr(os, "getgid") else (gid or 1)
        self._overrides = {
            snapshot.EXEC_HMAC_SECRET_CONFIG_DIR: {
                "st_uid": base_uid if directory_uid is None else directory_uid,
                "st_gid": base_gid if directory_gid is None else directory_gid,
            },
            snapshot.EXEC_HMAC_SECRET_PROFILE_METADATA_PATH: {
                "st_uid": base_uid if profile_uid is None else profile_uid,
                "st_gid": base_gid if profile_gid is None else profile_gid,
            },
            snapshot.EXEC_HMAC_SECRET_CONFIG_PATH: {
                "st_uid": base_uid if controller_uid is None else controller_uid,
                "st_gid": base_gid if controller_gid is None else controller_gid,
            },
        }
        if profile_size is not None:
            self._overrides[snapshot.EXEC_HMAC_SECRET_PROFILE_METADATA_PATH]["st_size"] = profile_size
        if controller_size is not None:
            self._overrides[snapshot.EXEC_HMAC_SECRET_CONFIG_PATH]["st_size"] = controller_size
        self._path_map = {
            snapshot.EXEC_HMAC_SECRET_CONFIG_DIR: self.directory,
            snapshot.EXEC_HMAC_SECRET_PROFILE_METADATA_PATH: self.profile,
            snapshot.EXEC_HMAC_SECRET_CONFIG_PATH: self.controller,
        }

        monkeypatch.setattr(Path, "lstat", lambda path: self._fake_lstat(path))
        monkeypatch.setattr(os, "open", self._fake_open)
        monkeypatch.setattr(os, "fstat", self._fake_fstat)

    def _fake_lstat(self, path: Path) -> os.stat_result:
        canonical = Path(path)
        self.lstat_calls.append(canonical)
        if canonical not in self._path_map:
            return self._real_lstat(path)
        st = self._real_lstat(self._path_map[canonical])
        return _stat_with(st, **self._overrides[canonical])

    def _fake_open(
        self,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        canonical = Path(path)
        self.open_calls.append(canonical)
        if canonical == snapshot.EXEC_HMAC_SECRET_PROFILE_METADATA_PATH:
            raise AssertionError("profile metadata file content must not be opened")
        if canonical == Path("/etc/skeleton/home-edge-01/env"):
            raise AssertionError("legacy nested env path must not be consulted")
        if canonical != snapshot.EXEC_HMAC_SECRET_CONFIG_PATH:
            return self._real_open(path, flags, mode, dir_fd=dir_fd)
        if self._deny_controller_open:
            raise PermissionError("denied")
        fd = self._real_open(self.controller, flags, mode)
        self._open_fds[fd] = 0
        return fd

    def _fake_fstat(self, fd: int) -> os.stat_result:
        if fd not in self._open_fds:
            return self._real_fstat(fd)
        self._open_fds[fd] += 1
        st = self._real_fstat(fd)
        overrides = dict(self._overrides[snapshot.EXEC_HMAC_SECRET_CONFIG_PATH])
        if self._replacement_race and self._open_fds[fd] >= 2:
            overrides["st_ino"] = st.st_ino + 1
        return _stat_with(st, **overrides)


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


def test_environment_secret_takes_precedence_without_config_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: pytest.fail("filesystem metadata must not be read"),
    )
    monkeypatch.setattr(
        os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("filesystem content must not be read"),
    )
    monkeypatch.setattr(
        snapshot,
        "_read_exec_hmac_secret_config",
        lambda: pytest.fail("config reader must not be called"),
    )

    request = snapshot.build_snapshot_request(environment={snapshot.EXEC_HMAC_SECRET_ENV: SECRET})

    assert request.signature == sign_request(request, SECRET)


def test_safe_fixed_controller_config_secret_signs_request_and_stays_private(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fs = FixedHmacFs(
        monkeypatch,
        tmp_path,
        controller_content=f"# unrelated\nexport OTHER_VALUE=ignored\n"
        f"{snapshot.EXEC_HMAC_SECRET_ENV}='{CONFIG_SECRET}'\n",
    )

    request = snapshot.build_snapshot_request(environment={})
    serialized = json.dumps(request.to_mapping(), sort_keys=True)

    assert request.signature == sign_request(request, CONFIG_SECRET)
    assert CONFIG_SECRET not in serialized
    assert snapshot.EXEC_HMAC_SECRET_ENV not in serialized
    assert fs.open_calls == [snapshot.EXEC_HMAC_SECRET_CONFIG_PATH]
    assert snapshot.EXEC_HMAC_SECRET_CONFIG_PATH == Path("/etc/skeleton/home-edge-executor-controller.env")
    assert snapshot.EXEC_HMAC_SECRET_PROFILE_METADATA_PATH == Path("/etc/skeleton/home-edge-01.env")
    assert snapshot.EXEC_HMAC_SECRET_CONFIG_DIR == Path("/etc/skeleton")


def test_unrelated_config_variables_and_comments_do_not_affect_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FixedHmacFs(
        monkeypatch,
        tmp_path,
        controller_content=(
            "\n# comment\nUNRELATED=value\nexport ALSO_UNRELATED='literal'\n"
            f'{snapshot.EXEC_HMAC_SECRET_ENV}="{CONFIG_SECRET}"\n'
        ),
    )

    request = snapshot.build_snapshot_request(environment={})

    assert request.signature == sign_request(request, CONFIG_SECRET)


def test_legacy_nested_env_is_never_consulted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "etc" / "skeleton" / "home-edge-01" / "env"
    legacy.parent.mkdir(parents=True)
    _write_file(legacy, f"{snapshot.EXEC_HMAC_SECRET_ENV}=wrong-secret\n")
    fs = FixedHmacFs(monkeypatch, tmp_path)

    request = snapshot.build_snapshot_request(environment={})

    assert request.signature == sign_request(request, CONFIG_SECRET)
    assert fs.open_calls == [snapshot.EXEC_HMAC_SECRET_CONFIG_PATH]
    assert Path("/etc/skeleton/home-edge-01/env") not in fs.lstat_calls


def test_profile_env_content_is_never_read_for_coherent_owner_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current_uid = os.getuid() if hasattr(os, "getuid") else 1
    fs = FixedHmacFs(
        monkeypatch,
        tmp_path,
        uid=current_uid + 1000,
        gid=43210,
    )

    request = snapshot.build_snapshot_request(environment={})

    assert request.signature == sign_request(request, CONFIG_SECRET)
    assert fs.open_calls == [snapshot.EXEC_HMAC_SECRET_CONFIG_PATH]


def test_root_or_current_process_owner_controller_remains_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current_uid = os.getuid() if hasattr(os, "getuid") else 1
    FixedHmacFs(
        monkeypatch,
        tmp_path,
        directory_uid=current_uid + 10,
        profile_uid=current_uid + 11,
        controller_uid=current_uid,
    )

    request = snapshot.build_snapshot_request(environment={})

    assert request.signature == sign_request(request, CONFIG_SECRET)


def test_coherent_fixed_path_owner_group_case_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current_uid = os.getuid() if hasattr(os, "getuid") else 1
    FixedHmacFs(monkeypatch, tmp_path, uid=current_uid + 1000, gid=54321)

    request = snapshot.build_snapshot_request(environment={})

    assert request.signature == sign_request(request, CONFIG_SECRET)


@pytest.mark.parametrize(
    "overrides",
    [
        {"directory_uid": 2001, "profile_uid": 2001, "controller_uid": 2002},
        {"directory_gid": 3001, "profile_gid": 3002, "controller_gid": 3001},
    ],
)
def test_owner_or_group_mismatch_between_fixed_paths_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, int],
) -> None:
    current_uid = os.getuid() if hasattr(os, "getuid") else 1
    defaults = {
        "directory_uid": current_uid + 1000,
        "profile_uid": current_uid + 1000,
        "controller_uid": current_uid + 1000,
        "directory_gid": 4001,
        "profile_gid": 4001,
        "controller_gid": 4001,
    }
    defaults.update(overrides)
    FixedHmacFs(monkeypatch, tmp_path, **defaults)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"


@pytest.mark.parametrize(
    "kind,content,mode,reason",
    [
        (
            "duplicate",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}={CONFIG_SECRET}\n"
            f"export {snapshot.EXEC_HMAC_SECRET_ENV}={CONFIG_SECRET}\n",
            0o600,
            "executor_auth_config_invalid",
        ),
        (
            "group_writable",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}={CONFIG_SECRET}\n",
            0o620,
            "executor_auth_config_unsafe",
        ),
        (
            "world_writable",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}={CONFIG_SECRET}\n",
            0o602,
            "executor_auth_config_unsafe",
        ),
        (
            "oversize",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}={CONFIG_SECRET}\n".encode()
            + (b"x" * snapshot.MAX_EXEC_HMAC_SECRET_CONFIG_BYTES),
            0o600,
            "executor_auth_config_unsafe",
        ),
        (
            "malformed_quote",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}='unterminated\n",
            0o600,
            "executor_auth_config_invalid",
        ),
        (
            "command_substitution",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}=$(printf value)\n",
            0o600,
            "executor_auth_config_invalid",
        ),
        (
            "backtick",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}=`printf value`\n",
            0o600,
            "executor_auth_config_invalid",
        ),
        (
            "variable_reference",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}=${{OTHER_VALUE}}\n",
            0o600,
            "executor_auth_config_invalid",
        ),
        (
            "continuation",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}=continued\\\n",
            0o600,
            "executor_auth_config_invalid",
        ),
        (
            "nul",
            f"{snapshot.EXEC_HMAC_SECRET_ENV}=bad".encode() + b"\x00\n",
            0o600,
            "executor_auth_config_invalid",
        ),
    ],
)
def test_invalid_fixed_config_blocks_before_executor_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    content: bytes | str,
    mode: int,
    reason: str,
) -> None:
    calls = 0
    FixedHmacFs(monkeypatch, tmp_path, controller_content=content, controller_mode=mode)

    def fake_execute(_request: Mapping[str, Any]) -> HomeEdgeExecReceipt:
        nonlocal calls
        calls += 1
        raise AssertionError(f"executor must not be called for {kind}")

    monkeypatch.setattr(snapshot, "execute_home_edge_request", fake_execute)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )
    public = json.dumps(receipt, sort_keys=True)

    assert calls == 0
    assert receipt["stable_reason"] == reason
    assert CONFIG_SECRET not in public
    assert str(snapshot.EXEC_HMAC_SECRET_CONFIG_PATH) not in public


@pytest.mark.parametrize(
    "kind,fs_kwargs",
    [
        ("directory", {"directory_kind": "symlink"}),
        ("profile", {"profile_kind": "symlink"}),
        ("controller", {"controller_kind": "symlink"}),
    ],
)
def test_symlink_fixed_boundary_blocks_before_executor_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    fs_kwargs: dict[str, str],
) -> None:
    FixedHmacFs(monkeypatch, tmp_path, **fs_kwargs)
    monkeypatch.setattr(
        snapshot,
        "execute_home_edge_request",
        lambda _request: pytest.fail(f"executor must not be called for {kind}"),
    )

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"


@pytest.mark.parametrize(
    "fs_kwargs",
    [
        {"directory_kind": "file"},
        {"profile_kind": "dir"},
        {"controller_kind": "dir"},
    ],
)
def test_non_directory_or_non_regular_fixed_boundary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fs_kwargs: dict[str, str],
) -> None:
    FixedHmacFs(monkeypatch, tmp_path, **fs_kwargs)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"


@pytest.mark.parametrize(
    "fs_kwargs",
    [
        {"directory_mode": 0o720},
        {"directory_mode": 0o702},
        {"profile_mode": 0o620},
        {"profile_mode": 0o602},
        {"controller_mode": 0o620},
        {"controller_mode": 0o602},
    ],
)
def test_group_or_world_writable_fixed_boundary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fs_kwargs: dict[str, int],
) -> None:
    FixedHmacFs(monkeypatch, tmp_path, **fs_kwargs)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"


@pytest.mark.parametrize(
    "fs_kwargs",
    [
        {"profile_size": snapshot.MAX_EXEC_HMAC_SECRET_CONFIG_BYTES + 1},
        {"controller_size": snapshot.MAX_EXEC_HMAC_SECRET_CONFIG_BYTES + 1},
    ],
)
def test_oversize_profile_or_controller_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fs_kwargs: dict[str, int],
) -> None:
    FixedHmacFs(monkeypatch, tmp_path, **fs_kwargs)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"


def test_controller_file_replacement_identity_race_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FixedHmacFs(monkeypatch, tmp_path, replacement_race=True)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"


def test_permission_denial_opening_controller_env_fails_closed_without_alternate_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fs = FixedHmacFs(monkeypatch, tmp_path, deny_controller_open=True)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"
    assert fs.open_calls == [snapshot.EXEC_HMAC_SECRET_CONFIG_PATH]


def test_auth_config_failure_receipt_excludes_secret_path_and_owner_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current_uid = os.getuid() if hasattr(os, "getuid") else 1
    private_uid = current_uid + 9000
    private_gid = 65432
    FixedHmacFs(
        monkeypatch,
        tmp_path,
        directory_uid=private_uid,
        profile_uid=private_uid,
        controller_uid=private_uid + 1,
        directory_gid=private_gid,
        profile_gid=private_gid,
        controller_gid=private_gid,
    )

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )
    public = json.dumps(receipt, sort_keys=True)

    assert receipt["stable_reason"] == "executor_auth_config_unsafe"
    assert CONFIG_SECRET not in public
    assert snapshot.EXEC_HMAC_SECRET_ENV not in public
    assert str(snapshot.EXEC_HMAC_SECRET_CONFIG_PATH) not in public
    assert str(snapshot.EXEC_HMAC_SECRET_PROFILE_METADATA_PATH) not in public
    assert str(private_uid) not in public
    assert str(private_gid) not in public


@pytest.mark.parametrize(
    "content,reason",
    [
        ("UNRELATED=value\n", "executor_auth_config_missing"),
        (None, "executor_auth_config_missing"),
    ],
)
def test_missing_fixed_config_or_target_variable_is_public_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str | None,
    reason: str,
) -> None:
    calls = 0
    FixedHmacFs(
        monkeypatch,
        tmp_path,
        controller_content=content or "",
        controller_missing=content is None,
    )

    def fake_execute(_request: Mapping[str, Any]) -> HomeEdgeExecReceipt:
        nonlocal calls
        calls += 1
        raise AssertionError("executor must not be called")

    monkeypatch.setattr(snapshot, "execute_home_edge_request", fake_execute)

    receipt = snapshot.execute_media_source_snapshot_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
        private_root=tmp_path / "private",
        environment={},
    )
    public = json.dumps(receipt, sort_keys=True)

    assert calls == 0
    assert receipt["stable_reason"] == reason
    assert str(snapshot.EXEC_HMAC_SECRET_CONFIG_PATH) not in public
    assert snapshot.EXEC_HMAC_SECRET_ENV not in public


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
