from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.home_edge import media_source_snapshot as snapshot

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = ROOT / "scripts/home_edge_media_source_snapshot_signer_payload.py"
WRAPPER_PATH = ROOT / "scripts/home_edge_media_source_snapshot_signer"
INSTALLER_PATH = ROOT / "scripts/install_home_edge_media_source_snapshot_signer.sh"


def _load_payload():
    spec = importlib.util.spec_from_file_location("snapshot_static_signer", PAYLOAD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_static_payload_reconstructs_exact_reviewed_snapshot_script(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _load_payload()
    original_safe_regular = payload._safe_regular
    monkeypatch.setattr(payload, "CONTRACT_SOURCE", Path(snapshot.__file__))
    monkeypatch.setattr(
        payload,
        "_safe_regular",
        lambda st, *, max_bytes, require_root=False: original_safe_regular(
            st, max_bytes=max_bytes, require_root=False
        ),
    )

    assert payload.expected_snapshot_script() == snapshot.SNAPSHOT_SCRIPT


def test_static_payload_is_repo_import_independent() -> None:
    text = PAYLOAD_PATH.read_text(encoding="utf-8")
    wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

    assert "from core." not in text
    assert "import core." not in text
    assert "/home/agent/" not in text + wrapper
    assert "PYTHONPATH" not in text + wrapper
    assert str(snapshot.INSTALLED_SIGNER_PAYLOAD) in wrapper


def test_installer_never_executes_checkout_python() -> None:
    text = INSTALLER_PATH.read_text(encoding="utf-8")

    forbidden = (
        "PYTHONPATH=",
        "/usr/bin/python3 -",
        "python3 -c",
        "python -c",
        "python3 -m py_compile",
        "from core.home_edge",
    )
    for token in forbidden:
        assert token not in text
    assert "/home/agent/" not in text
    assert "copy_stable_source" in text
    assert "contract_source.py" in text
    assert "visudo -cf" in text
    assert 'NOPASSWD: $EXEC_ROOT/signer ""' in text
    assert 'PROTECTED_INSTALLER_PATH="/usr/local/libexec/skeleton/home-edge/media-source-snapshot-installer/install_home_edge_media_source_snapshot_signer.sh"' in text
    assert 'readlink -f -- "$0"' in text
    assert "root must execute only protected installed signer installer copy" in text
    assert "protected signer installer copy ownership or mode is unsafe" in text


def test_installer_rollback_restores_exact_preinstall_presence() -> None:
    text = INSTALLER_PATH.read_text(encoding="utf-8")

    for marker in (
        "HAD_INSTALL_ROOT=0",
        "HAD_EXEC_ROOT=0",
        "HAD_SUDOERS=0",
        "BACKUPS_READY=0",
        "ACTIVATION_STARTED=0",
    ):
        assert marker in text
    assert 'rm -rf "$STAGING_PARENT" "$INSTALL_ROOT.new" "$EXEC_ROOT.new"' in text
    assert 'if [[ $COMMITTED -eq 0 && $ACTIVATION_STARTED -eq 1 ]]; then' in text
    assert 'if [[ $BACKUPS_READY -ne 1 ]]; then' in text
    assert 'if [[ $HAD_INSTALL_ROOT -eq 1 ]]; then' in text
    assert 'if [[ $HAD_EXEC_ROOT -eq 1 ]]; then' in text
    assert 'if [[ $HAD_SUDOERS -eq 1 ]]; then' in text
    assert 'rm -rf "$INSTALL_ROOT"' in text
    assert 'rm -rf "$EXEC_ROOT"' in text
    assert 'rm -f "$SUDOERS_PATH"' in text
    assert "existing signer install root is unsafe" in text
    assert "existing signer executable root is unsafe" in text
    assert "existing signer sudoers entry is unsafe" in text


def test_installer_activation_waits_for_complete_backups_and_fails_closed_during_reinstall() -> None:
    text = INSTALLER_PATH.read_text(encoding="utf-8")

    backups_ready = text.index("BACKUPS_READY=1")
    activation_started = text.index("ACTIVATION_STARTED=1")
    disable_sudoers = text.index('rm -f "$SUDOERS_PATH"', activation_started)
    remove_install = text.index('rm -rf "$INSTALL_ROOT"', activation_started)
    remove_exec = text.index('rm -rf "$EXEC_ROOT"', activation_started)
    move_install = text.index('mv -T "$INSTALL_ROOT.new" "$INSTALL_ROOT"')
    move_exec = text.index('mv -T "$EXEC_ROOT.new" "$EXEC_ROOT"')
    new_sudoers = text.index('install -o root -g root -m 0440 "$BACKUP_DIR/sudoers.new" "$SUDOERS_PATH"')

    assert backups_ready < activation_started
    assert activation_started < disable_sudoers
    assert disable_sudoers < remove_install < move_install
    assert disable_sudoers < remove_exec < move_exec
    assert move_install < new_sudoers
    assert move_exec < new_sudoers
    assert 'if [[ $HAD_INSTALL_ROOT -eq 1 ]]; then rm -rf "$INSTALL_ROOT"; fi' in text
    assert 'if [[ $HAD_EXEC_ROOT -eq 1 ]]; then rm -rf "$EXEC_ROOT"; fi' in text
    assert "FATAL: signer activation started without complete backups" in text


def test_payload_and_wrapper_syntax() -> None:
    subprocess.run([sys.executable, "-m", "py_compile", str(PAYLOAD_PATH)], check=True)
    subprocess.run(["/bin/sh", "-n", str(WRAPPER_PATH)], check=True)
    subprocess.run(["/bin/bash", "-n", str(INSTALLER_PATH)], check=True)


def test_wrong_approval_fails_before_secret_read(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _load_payload()
    unsigned = snapshot.build_snapshot_request().to_mapping(include_signature=False)
    unsigned["operator_approval_ref"] = "WRONG_APPROVAL"
    monkeypatch.setattr(payload, "read_secret", lambda: pytest.fail("credential must not be read"))

    with pytest.raises(SystemExit) as exc:
        payload.validate_authority(unsigned)

    assert exc.value.code == 2


def test_payload_signing_matches_executor_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _load_payload()
    original_safe_regular = payload._safe_regular
    monkeypatch.setattr(payload, "CONTRACT_SOURCE", Path(snapshot.__file__))
    monkeypatch.setattr(
        payload,
        "_safe_regular",
        lambda st, *, max_bytes, require_root=False: original_safe_regular(
            st, max_bytes=max_bytes, require_root=False
        ),
    )
    unsigned = snapshot.build_snapshot_request().to_mapping(include_signature=False)
    payload.validate_authority(unsigned)
    secret = "synthetic-static-signer-key"
    signature = payload.sign(unsigned, secret)

    from core.home_edge.executor import HomeEdgeExecRequest, sign_request

    signed = HomeEdgeExecRequest.from_mapping({**unsigned, "signature": signature})
    assert signature == sign_request(signed, secret)


def test_installed_contract_requires_root_owned_regular_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _load_payload()
    source = tmp_path / "contract_source.py"
    source.write_text(Path(snapshot.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    source.chmod(0o644)
    monkeypatch.setattr(payload, "CONTRACT_SOURCE", source)
    if os.getuid() != 0:
        with pytest.raises(SystemExit):
            payload.expected_snapshot_script()
