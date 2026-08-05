from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Mapping

import pytest

from core.home_edge import debian_media_bootstrap as bootstrap
from core.home_edge.executor import HomeEdgeExecReceipt, HomeEdgeExecRequest, sign_request


SHA = "a" * 40
SECRET = "test-home-edge-secret"


def issue_body(**updates: str) -> str:
    fields = {
        "Mode": "RUNTIME_MAINTENANCE_TASK",
        "Maintenance Task ID": bootstrap.TASK_ID,
        "Repository": bootstrap.REPOSITORY,
        "Expected Main SHA": SHA,
        "Operator Approval": bootstrap.OPERATOR_APPROVAL,
        "Target": bootstrap.TARGET_NODE,
    }
    fields.update(updates)
    return "\n".join(f"{key}: {value}" for key, value in fields.items())


def public_receipt(**updates: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "maintenance_task_id": bootstrap.TASK_ID,
        "os_identity_status": "verified",
        "node_identity_status": "verified",
        "reboot_guard_status": "not_present",
        "reboot_performed": False,
        "packages_required_count": len(bootstrap.FIXED_PACKAGES),
        "packages_preexisting_count": 11,
        "packages_added_count": 9,
        "package_status": "installed",
        "display_manager_status": "service_active",
        "autologin_status": "configured",
        "pipewire_status": "pending_session",
        "vaapi_status": "physical_pending",
        "mpv_status": "configured",
        "chromium_status": "configured",
        "ssh_status": "service_active",
        "gateway_postcheck_status": "pending",
        "physical_audio_status": "physical_pending",
        "physical_video_status": "physical_pending",
        "rollback_ready": True,
        "rollback_applied": False,
        "mutation_executor_receipt_hash": "f" * 64,
        "final_postcheck_receipt_hash": "e" * 64,
        "audit_receipt_hash": "0" * 64,
        "stable_reason": "completed",
        "success_criteria": "met",
    }
    receipt.update(updates)
    return receipt


def executor_receipt(stdout: str, *, request_id: str = "req") -> HomeEdgeExecReceipt:
    now = datetime.now(UTC).isoformat()
    return HomeEdgeExecReceipt(
        status="ok",
        request_id=request_id,
        node_id=bootstrap.TARGET_NODE,
        execution_lane="privileged_mutation",
        exit_code=0,
        stdout=stdout,
        stderr="",
        started_at=now,
        finished_at=now,
        duration_seconds=0.01,
        idempotency="executed",
        receipt_hash="f" * 64,
    )


def test_exact_runtime_input_accepted_and_main_sha_checked() -> None:
    parsed = bootstrap.parse_runtime_input(issue_body())

    assert parsed.repository == bootstrap.REPOSITORY
    assert parsed.expected_main_sha == SHA
    bootstrap.validate_main_sha(
        SHA,
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
    )


@pytest.mark.parametrize(
    "body,reason",
    [
        (issue_body(**{"Expected Main SHA": "A" * 40}), "expected_main_sha_malformed"),
        (
            issue_body() + "\nExpected Main SHA: " + SHA,
            "duplicate_runtime_input_field",
        ),
        (issue_body() + "\nPackages: curl,evil", "unknown_runtime_input_field"),
        (issue_body() + "\nCommand: apt-get remove sudo", "unknown_runtime_input_field"),
        (issue_body() + "\nPath: /tmp/evil", "unknown_runtime_input_field"),
        (issue_body() + "\nUser: root", "unknown_runtime_input_field"),
        (issue_body() + "\nHost: other", "unknown_runtime_input_field"),
        (issue_body() + "\nUnits: ssh.service", "unknown_runtime_input_field"),
        (issue_body() + "\nTimeout: 1", "unknown_runtime_input_field"),
        (issue_body() + "\nLane: destructive", "unknown_runtime_input_field"),
        (issue_body() + "\nRun As: desktop-user", "unknown_runtime_input_field"),
    ],
)
def test_malformed_duplicate_and_behavior_changing_fields_rejected(
    body: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        bootstrap.parse_runtime_input(body)


def test_expected_sha_must_equal_registered_and_github_main() -> None:
    with pytest.raises(ValueError, match="registered_clean_main_sha_mismatch"):
        bootstrap.validate_main_sha(SHA, registered_clean_main_sha="b" * 40, github_main_sha=SHA)
    with pytest.raises(ValueError, match="github_main_sha_mismatch"):
        bootstrap.validate_main_sha(SHA, registered_clean_main_sha=SHA, github_main_sha="b" * 40)


def test_request_is_signed_fixed_and_dispatched_only_through_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Mapping[str, Any]] = []
    monkeypatch.setenv(bootstrap.EXEC_HMAC_SECRET_ENV, SECRET)

    def fake_execute(request: Mapping[str, Any]):
        calls.append(request)
        if len(calls) == 1:
            return executor_receipt(json.dumps(public_receipt()))
        return executor_receipt("", request_id="post")

    monkeypatch.setattr(bootstrap, "execute_home_edge_request", fake_execute)

    receipt = bootstrap.execute_debian_media_bootstrap_task(
        issue_body(),
        registered_clean_main_sha=SHA,
        github_main_sha=SHA,
    )

    assert receipt["success_criteria"] == "met"
    assert receipt["gateway_postcheck_status"] == "ok"
    assert len(calls) == 2
    first = HomeEdgeExecRequest.from_mapping(calls[0])
    assert first.signature == sign_request(first, SECRET)
    assert first.node_id == bootstrap.TARGET_NODE
    assert first.run_as.value == "root"
    assert first.execution_lane.value == "privileged_mutation"
    assert first.timeout_seconds == 900
    assert first.idempotency_key == bootstrap.IDEMPOTENCY_KEY
    assert first.idempotency_key.endswith("v2-review-repair-20260805")
    assert first.operator_approval_ref == bootstrap.OPERATOR_APPROVAL
    assert first.script == bootstrap.BOOTSTRAP_SCRIPT
    assert "apt-get install -y --no-install-recommends \"${PACKAGES[@]}\"" in first.script
    assert "openssh-server sudo pipewire" in first.script
    assert "ssh -" not in bootstrap.BOOTSTRAP_SCRIPT

    postcheck = HomeEdgeExecRequest.from_mapping(calls[1])
    assert postcheck.signature == sign_request(postcheck, SECRET)
    assert postcheck.argv == ("/usr/bin/true",)
    assert postcheck.execution_lane.value == "read_only"
    assert postcheck.run_as.value == "root"
    assert receipt["mutation_executor_receipt_hash"] == "f" * 64
    assert receipt["final_postcheck_receipt_hash"] == "f" * 64
    assert receipt["audit_receipt_hash"] == bootstrap._audit_hash(receipt)


def test_postcheck_failure_prevents_success_criteria() -> None:
    receipt = public_receipt(gateway_postcheck_status="blocked", success_criteria="met")
    assert bootstrap.success_criteria_met(receipt) is False
    receipt["gateway_postcheck_status"] = "ok"
    receipt["ssh_status"] = "pending_service"
    assert bootstrap.success_criteria_met(receipt) is False


@pytest.mark.parametrize(
    "marker",
    [
        "os_not_debian",
        "os_version_not_13",
        "hostname_mismatch",
        "user_missing",
        "uid_mismatch",
        "home_mismatch",
        "external_live_root_ambiguous",
        "dpkg_lock_active",
        "filesystem_read_only",
        "missing_root_execution",
        "free_space_low",
    ],
)
def test_fixed_preflight_blocks_before_apt(marker: str) -> None:
    assert f"block {marker}" in bootstrap.BOOTSTRAP_SCRIPT
    assert bootstrap.BOOTSTRAP_SCRIPT.index(f"block {marker}") < bootstrap.BOOTSTRAP_SCRIPT.index("apt-get update")


def test_first_boot_guard_allowlist_and_marker_are_strict() -> None:
    for unit in (
        "skeleton-home-edge-first-boot-guard.service",
        "skeleton-home-edge-first-boot-guard.timer",
        "home-edge-first-boot-guard.service",
        "home-edge-first-boot-guard.timer",
    ):
        assert unit in bootstrap.BOOTSTRAP_SCRIPT
    assert "skeleton.home_edge.debian13.first_boot_guard.v1" in bootstrap.BOOTSTRAP_SCRIPT
    assert "guard_status=\"unverified\"" in bootstrap.BOOTSTRAP_SCRIPT
    assert 'systemctl disable --now "$unit"' in bootstrap.BOOTSTRAP_SCRIPT
    assert "reboot_guard_unverified" in bootstrap.BOOTSTRAP_SCRIPT
    assert "stat -c '%u' \"$exec_path\"" in bootstrap.BOOTSTRAP_SCRIPT
    assert "sha256sum \"$exec_path\"" in bootstrap.BOOTSTRAP_SCRIPT
    assert "acceptance" not in bootstrap.BOOTSTRAP_SCRIPT.lower()


def test_no_boot_power_or_direct_transport_commands_exist_in_script() -> None:
    forbidden = ("systemctl reboot", "/sbin/reboot", "shutdown ", "poweroff", " tailscale ", " ufw ", "iptables", "parted", "mkfs", "ssh -")
    lowered = bootstrap.BOOTSTRAP_SCRIPT.lower()
    for token in forbidden:
        assert token not in lowered
    assert "systemctl --user -m" not in lowered


def test_marker_rerun_still_checks_drift_and_exact_rerun_avoids_apt_config_churn() -> None:
    assert 'if [ -f "$MARKER" ]; then' not in bootstrap.BOOTSTRAP_SCRIPT
    marker_reference = bootstrap.BOOTSTRAP_SCRIPT.index('MARKER="/var/lib/skeleton/home-edge-01/debian-media-bootstrap-v1.complete"')
    package_parity = bootstrap.BOOTSTRAP_SCRIPT.index('missing_packages=false')
    config_parity = bootstrap.BOOTSTRAP_SCRIPT.index('needs_config=false')
    apt_gate = bootstrap.BOOTSTRAP_SCRIPT.index('if [ "$missing_packages" = true ]; then')
    config_gate = bootstrap.BOOTSTRAP_SCRIPT.index('if [ "$needs_config" = true ]; then')
    assert marker_reference < package_parity < apt_gate
    assert marker_reference < config_parity < config_gate
    assert "stable_reason=\"already_complete\"" in bootstrap.BOOTSTRAP_SCRIPT


def test_partial_apt_failure_removes_only_proven_new_packages_and_restores_configs() -> None:
    script = bootstrap.BOOTSTRAP_SCRIPT
    assert 'grep -qx "$pkg" "$preexisting_file"' in script
    assert 'xargs -r apt-get remove -y --purge <"$added_file"' in script
    assert "restore_configs" in script
    assert "rollback_failed" in script
    assert "never remove" not in script


def test_receipt_generation_is_shell_only_and_command_output_is_private() -> None:
    script = bootstrap.BOOTSTRAP_SCRIPT
    assert "json_escape" not in script
    assert "python3 -c" not in script
    assert "jq" not in script.split("emit_receipt", 1)[1].split("block()", 1)[0]
    assert 'log="$(bounded_log "$label")"' in script
    assert '>"$log" 2>&1' in script
    for noisy in ("apt-get update", "apt-get install", "vainfo --display drm"):
        index = script.index(noisy)
        assert '>"$(bounded_log' in script[index : index + 220]
    assert "run_quiet systemctl_ssh systemctl enable --now ssh.service" in script
    assert "run_quiet systemctl_avahi systemctl enable --now avahi-daemon.service" in script
    assert "run_quiet systemctl_lightdm systemctl enable --now lightdm.service" in script


def test_rollback_manifest_is_durable_private_and_re_readable() -> None:
    script = bootstrap.BOOTSTRAP_SCRIPT
    assert 'STATE_ROOT="/var/lib/skeleton/home-edge-01/debian-media-bootstrap-v2"' in script
    assert 'install -d -m 0700 "$STATE_ROOT" "$ROLLBACK_ROOT" "$ROLLBACK_DIR"' in script
    assert 'chmod 0600 "$MANIFEST"' in script
    assert '[ -r "$MANIFEST" ]' in script
    assert 'rm -rf "$backup_dir"' not in script
    assert 'for backup in $(find' not in script


def test_openbox_does_not_launch_pipewire_and_user_units_use_session_bus() -> None:
    script = bootstrap.BOOTSTRAP_SCRIPT
    desired_openbox = script.split("desired_openbox='", 1)[1].split("'\n", 1)[0]
    assert "pipewire" not in desired_openbox.lower()
    assert "wireplumber" not in desired_openbox.lower()
    assert "[ -S /run/user/1000/bus ]" in script
    assert "runuser -u oleksii -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user enable --now pipewire.service pipewire-pulse.service wireplumber.service" in script
    assert "systemctl --user -M oleksii@" not in script


def test_mandatory_services_root_ancestry_and_vaapi_checks_are_strict() -> None:
    script = bootstrap.BOOTSTRAP_SCRIPT
    assert "root_device_safe || block external_live_root_ambiguous" in script
    assert 'lsblk -ndo TRAN "/dev/$root"' in script
    assert '[ "$tran" != "usb" ] || return 1' in script
    assert "systemctl enable --now ssh.service" in script
    assert "systemctl enable --now avahi-daemon.service" in script
    assert "systemctl enable --now lightdm.service" in script
    assert '[ "$ssh_status" != "service_active" ]' in script
    assert '[ "$display_status" != "service_active" ]' in script
    assert "timeout 10 vainfo --display drm --device /dev/dri/renderD128" in script
    assert 'vaapi_status="render_node_missing"' in script
    assert 'vaapi_status="driver_missing"' in script


def test_public_receipt_rejects_private_values_and_keeps_physical_pending() -> None:
    receipt = public_receipt(stable_reason="/home/skeleton/private")
    with pytest.raises(ValueError, match="receipt_field_not_public_safe"):
        bootstrap.sanitize_public_receipt(receipt)

    sanitized = bootstrap.sanitize_public_receipt(public_receipt())
    lines = "\n".join(bootstrap.receipt_status_lines(sanitized))
    assert "physical_audio_status=physical_pending" in lines
    assert "physical_video_status=physical_pending" in lines
    assert "/home" not in lines
    assert "secret" not in lines.lower()
    assert "oleksii" not in lines
