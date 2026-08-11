from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from core.home_edge import media_source_snapshot as snapshot
from core.home_edge.executor import HomeEdgeExecRequest, sign_request

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_home_edge_media_source_snapshot_signer.sh"


def _synthetic_installed_signature(unsigned: Mapping[str, Any]) -> HomeEdgeExecRequest:
    return HomeEdgeExecRequest.from_mapping(
        {**dict(unsigned), "signature": "sha256=" + "a" * 64}
    )


def test_environment_without_test_sentinel_cannot_bypass_installed_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsigned = snapshot.build_snapshot_request()
    calls: list[dict[str, Any]] = []

    def installed_signer(request: Mapping[str, Any]) -> HomeEdgeExecRequest:
        calls.append(dict(request))
        return _synthetic_installed_signature(request)

    monkeypatch.setattr(snapshot, "_sign_snapshot_request_with_installed_signer", installed_signer)
    assert not hasattr(snapshot, "_resolve_exec_hmac_secret")

    signed = snapshot.sign_snapshot_request(
        unsigned,
        environment={snapshot.EXEC_HMAC_SECRET_ENV: "must-not-be-used"},
    )

    assert calls == [unsigned.to_mapping(include_signature=False)]
    assert signed.signature == "sha256=" + "a" * 64


def test_signer_uses_exact_absolute_sudo_invocation() -> None:
    assert snapshot.SIGNER_SUDO_ARGV == (
        "/usr/bin/sudo",
        "-n",
        str(snapshot.INSTALLED_SIGNER_EXECUTABLE),
    )


def test_no_environment_combination_enables_runner_hmac_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-test-only-signing-secret"
    unsigned = snapshot.build_snapshot_request()
    environment = {
        snapshot.EXEC_HMAC_SECRET_ENV: secret,
        "SKELETON_HOME_EDGE_TEST_ALLOW_RUNNER_HMAC": "1",
    }
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        input: bytes,
        stdout: int,
        stderr: int,
        timeout: int,
        check: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert argv == list(snapshot.SIGNER_SUDO_ARGV)
        assert env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        request = HomeEdgeExecRequest.from_mapping(json.loads(input.decode("utf-8")))
        signed = {
            **request.to_mapping(include_signature=False),
            "signature": sign_request(request, secret),
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(signed).encode("utf-8"), b"")

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)

    signed = snapshot.sign_snapshot_request(unsigned, environment=environment)

    assert calls == [list(snapshot.SIGNER_SUDO_ARGV)]
    assert signed.signature == sign_request(signed, secret)
    assert not hasattr(snapshot, "TEST_RUNNER_HMAC_OVERRIDE_ENV")
    assert not hasattr(snapshot, "_runner_hmac_override_allowed")
    assert not hasattr(snapshot, "_resolve_exec_hmac_secret")


def test_installer_contract_blob_pin_matches_current_contract_source() -> None:
    data = Path(snapshot.__file__).read_bytes()
    git_blob = hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()
    installer = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r'^CONTRACT_BLOB_SHA="([0-9a-f]{40})"$', installer, re.MULTILINE)

    assert match is not None
    assert match.group(1) == git_blob
