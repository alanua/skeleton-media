from __future__ import annotations

import hashlib
import re
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
    monkeypatch.setattr(
        snapshot,
        "_resolve_exec_hmac_secret",
        lambda **_kwargs: pytest.fail("production-like environment must not read Runner HMAC directly"),
    )

    signed = snapshot.sign_snapshot_request(
        unsigned,
        environment={snapshot.EXEC_HMAC_SECRET_ENV: "must-not-be-used"},
    )

    assert calls == [unsigned.to_mapping(include_signature=False)]
    assert signed.signature == "sha256=" + "a" * 64


def test_test_sentinel_allows_bounded_direct_hmac_override() -> None:
    secret = "synthetic-test-only-signing-secret"
    unsigned = snapshot.build_snapshot_request()
    environment = {
        snapshot.EXEC_HMAC_SECRET_ENV: secret,
        snapshot.TEST_RUNNER_HMAC_OVERRIDE_ENV: "1",
    }

    signed = snapshot.sign_snapshot_request(unsigned, environment=environment)

    assert signed.signature == sign_request(signed, secret)


def test_test_sentinel_without_explicit_synthetic_secret_cannot_enable_override() -> None:
    assert snapshot._runner_hmac_override_allowed(
        environment={snapshot.TEST_RUNNER_HMAC_OVERRIDE_ENV: "1"}
    ) is False


@pytest.mark.parametrize("value", ["", "0", "true", "TRUE", "yes", "2"])
def test_wrong_test_sentinel_value_cannot_enable_override_even_with_secret(value: str) -> None:
    assert snapshot._runner_hmac_override_allowed(
        environment={
            snapshot.TEST_RUNNER_HMAC_OVERRIDE_ENV: value,
            snapshot.EXEC_HMAC_SECRET_ENV: "synthetic-test-only-signing-secret",
        }
    ) is False


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
