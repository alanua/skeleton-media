from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skeleton_media.home_edge import media_source_snapshot as snapshot

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = ROOT / "scripts/home_edge_media_source_snapshot_signer_payload.py"
WRAPPER_PATH = ROOT / "scripts/home_edge_media_source_snapshot_signer"
INSTALLER_PATH = ROOT / "scripts/install_home_edge_media_source_snapshot_signer.sh"
CONTRACT_REPO_PATH = "skeleton_media/home_edge/media_source_snapshot.py"


def _load_payload():
    spec = importlib.util.spec_from_file_location("snapshot_static_signer", PAYLOAD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exact_reviewed_contract_source(tmp_path: Path) -> Path:
    source = tmp_path / "contract_source.py"
    source.write_bytes(
        subprocess.check_output(
            ["git", "show", f"HEAD:{CONTRACT_REPO_PATH}"],
            cwd=ROOT,
        )
    )
    source.chmod(0o644)
    return source


def test_static_payload_reconstructs_exact_reviewed_snapshot_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _load_payload()
    original_safe_regular = payload._safe_regular
    monkeypatch.setattr(payload, "CONTRACT_SOURCE", _exact_reviewed_contract_source(tmp_path))
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


def test_payload_signing_matches_executor_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _load_payload()
    original_safe_regular = payload._safe_regular
    monkeypatch.setattr(payload, "CONTRACT_SOURCE", _exact_reviewed_contract_source(tmp_path))
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


def test_installed_audit_precedes_commit_with_rollback_armed() -> None:
    text = INSTALLER_PATH.read_text()
    install = text.index('install -o root -g root -m 0440 "$BACKUP_DIR/sudoers.new" "$SUDOERS_PATH"')
    audit = text.index('validate_installed_file "$INSTALL_ROOT/signer_payload.py"')
    sudoers_audit = text.index('visudo -cf "$SUDOERS_PATH"')
    commit = text.index('COMMITTED=1')
    assert text.index('trap rollback EXIT') < install < audit < sudoers_audit < commit
    assert commit < text.index('trap - EXIT', commit)
    for call in (
        'validate_installed_file "$INSTALL_ROOT/signer_payload.py" 555 "$PAYLOAD_BLOB_SHA"',
        'validate_installed_file "$INSTALL_ROOT/contract_source.py" 444 "$CONTRACT_BLOB_SHA"',
        'validate_installed_file "$EXEC_ROOT/signer" 555 "$WRAPPER_BLOB_SHA"',
        'validate_installed_file "$SUDOERS_PATH" 440',
    ):
        assert call in text[audit:commit]


@pytest.mark.parametrize("target", ["payload", "contract", "wrapper", "sudoers"])
@pytest.mark.parametrize("fault", ["none", "content", "uid", "gid", "mode", "symlink", "directory", "missing", "stat", "hash", "visudo"])
def test_installed_audit_synthetic_rollback(
    tmp_path: Path, target: str, fault: str,
) -> None:
    # Execute only the audit and existing rollback in a temporary sandbox. Never
    # execute the installer entry point, sudo, chown, or privileged install.
    import hashlib
    import shlex

    text = INSTALLER_PATH.read_text()
    rollback = text.split('rollback() {', 1)[1].split('\ntrap rollback EXIT', 1)[0]
    audit = text.split('validate_installed_file() {', 1)[1].split('\nCOMMITTED=1', 1)[0]
    paths = {
        "payload": tmp_path / "runtime/signer_payload.py",
        "contract": tmp_path / "runtime/contract_source.py",
        "wrapper": tmp_path / "exec/signer",
        "sudoers": tmp_path / "sudoers",
    }
    backup = tmp_path / "backup"
    backup.mkdir()
    for directory in ("install-root", "exec-root"):
        (backup / directory).mkdir()
    old_paths = [backup / "install-root/signer_payload.py", backup / "install-root/contract_source.py", backup / "exec-root/signer", backup / "sudoers"]
    for old in old_paths:
        old.write_bytes(b"old working install\n")
        old.chmod(0o440 if old.name == "sudoers" else 0o555)
    blobs = {}
    for key, path in paths.items():
        path.parent.mkdir(exist_ok=True)
        data = (f'agent ALL=(root) NOPASSWD: {tmp_path}/exec/signer ""\n' if key == "sudoers" else key).encode()
        path.write_bytes(data)
        path.chmod(0o444 if key == "contract" else 0o440 if key == "sudoers" else 0o555)
        blobs[key] = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    selected = paths[target]
    if fault == "content":
        selected.chmod(0o644)
        selected.write_bytes(selected.read_bytes() + b"tampered\n")
        selected.chmod(0o444 if target == "contract" else 0o440 if target == "sudoers" else 0o555)
    elif fault in ("symlink", "directory", "nonempty_directory", "fifo", "missing"):
        selected.unlink()
        if fault == "symlink":
            selected.symlink_to(old_paths[0])
        elif fault in ("directory", "nonempty_directory"):
            selected.mkdir()
            if fault == "nonempty_directory":
                (selected / "unrelated").write_bytes(b"retain unrelated contents\n")
        elif fault == "fifo":
            import os

            os.mkfifo(selected)
    assignments = {
        "INSTALL_ROOT": tmp_path / "runtime", "EXEC_ROOT": tmp_path / "exec",
        "SUDOERS_PATH": paths["sudoers"], "BACKUP_DIR": backup,
        "STAGING_PARENT": tmp_path / "stage", "RUNNER_USER": "agent",
        "PAYLOAD_BLOB_SHA": blobs["payload"], "CONTRACT_BLOB_SHA": blobs["contract"],
        "WRAPPER_BLOB_SHA": blobs["wrapper"], "FAULT": fault, "TARGET": selected,
    }
    script = 'set -Eeuo pipefail\n' + '\n'.join(f'{k}={shlex.quote(str(v))}' for k, v in assignments.items())
    script += r'''
COMMITTED=0
ACTIVATION_STARTED=1
BACKUPS_READY=1
HAD_INSTALL_ROOT=1
HAD_EXEC_ROOT=1
HAD_SUDOERS=1
stat() {
  if [[ "${@: -1}" == "$TARGET" ]]; then
    case "$FAULT" in
      uid) printf '1:0:%s\n' "$(command stat -c '%a' -- "$TARGET")"; return;;
      gid) printf '0:1:%s\n' "$(command stat -c '%a' -- "$TARGET")"; return;;
      mode) echo '0:0:777'; return;;
      stat) return 1;;
    esac
  fi
  # Synthetic root ownership; preserve actual filesystem mode verification.
  printf '0:0:%s\n' "$(command stat -c '%a' -- "${@: -1}")"
}
reviewed_blob_sha() {
  if [[ "$FAULT" == hash && "$1" == "$TARGET" ]]; then return 1; fi
  /usr/bin/git hash-object --no-filters --stdin < "$1"
}
visudo() { [[ "$FAULT" != visudo ]]; }
install() {
  [[ "$1 $2 $3 $4 $5 $6" == '-o root -g root -m 0440' ]]
  command cp -- "$7" "$8"
  command chmod 0440 "$8"
}
'''
    script += '\nrollback() {' + rollback + '\ntrap rollback EXIT\n'
    script += '\nvalidate_installed_file() {' + audit + '\nCOMMITTED=1\n'
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    fails = fault != "none" and not (fault == "hash" and target == "sudoers")
    assert (result.returncode != 0) == fails, result.stderr
    if fault in ("nonempty_directory", "fifo"):
        assert result.returncode == 70
        assert result.stderr.count("FATAL:") == 1
        assert "backups retained" in result.stderr
        assert backup.is_dir()
        for old in old_paths:
            assert old.read_bytes() == b"old working install\n"
        if fault == "nonempty_directory":
            assert "must be empty" in result.stderr
            assert (selected / "unrelated").read_bytes() == b"retain unrelated contents\n"
        else:
            assert selected.is_fifo()
        return
    if fails:
        for path in paths.values():
            assert path.is_file() and not path.is_symlink()
            assert path.read_bytes() == b"old working install\n"
        assert paths["sudoers"].stat().st_mode & 0o7777 == 0o440
    else:
        assert paths["payload"].read_bytes() == b"payload"
    assert not backup.exists()


@pytest.mark.parametrize("fault", ["nonempty_directory", "fifo"])
def test_sudoers_rollback_refuses_destructive_cleanup(tmp_path: Path, fault: str) -> None:
    test_installed_audit_synthetic_rollback(tmp_path, "sudoers", fault)
