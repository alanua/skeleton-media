from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_home_edge_media_source_snapshot_signer.sh"
RUNNER_SETUP = ROOT / "scripts/README_RUNNER_SETUP.md"
RUNNER_SERVICE = ROOT / "scripts/skeleton-runner-poll.service"


def test_snapshot_signer_sudoers_is_bound_only_to_canonical_runner_service_user() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    setup = RUNNER_SETUP.read_text(encoding="utf-8")
    service = RUNNER_SERVICE.read_text(encoding="utf-8")

    assert "The service runs as user `agent`" in setup
    assert "User=agent" in service
    assert 'RUNNER_USER="agent"' in installer
    assert 'RUNNER_SERVICE="skeleton-runner-poll.service"' in installer
    assert "/usr/bin/systemctl show --property=User --value \"$RUNNER_SERVICE\"" in installer
    assert '[[ "$actual_runner_user" != "$RUNNER_USER" ]]' in installer
    assert "live Runner service user does not match canonical agent account" in installer
    assert "SUDO_USER" not in installer
    assert "--runner-user" not in installer
    assert '$RUNNER_USER ALL=(root) NOPASSWD: $EXEC_ROOT/signer ""' in installer
    assert "canonical runner user is unavailable" in installer
