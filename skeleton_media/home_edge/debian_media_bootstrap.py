from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

from core.home_edge.executor import HomeEdgeExecRequest, sign_request
from core.home_edge.executor_gateway import EXEC_HMAC_SECRET_ENV, execute_home_edge_request


TASK_ID = "home_edge_01_debian_media_bootstrap_v1"
REPOSITORY = "alanua/skeleton-media"
TARGET_NODE = "home-edge-01"
OPERATOR_APPROVAL = "EXPLICIT_FINISH_DEBIAN_MEDIA_NODE_20260805"
APPROVAL_REF = OPERATOR_APPROVAL
IDEMPOTENCY_KEY = "home-edge-01-debian-media-bootstrap-v2-review-repair-20260805"
REQUEST_TIMEOUT_SECONDS = 900
RECEIPT_SCHEMA = "skeleton.home_edge.debian_media_bootstrap_receipt.v1"
EXPECTED_MAIN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FIXED_PACKAGES = (
    "openssh-server",
    "sudo",
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "alsa-utils",
    "mpv",
    "yt-dlp",
    "intel-media-va-driver",
    "vainfo",
    "chromium",
    "xserver-xorg",
    "lightdm",
    "openbox",
    "dbus-x11",
    "avahi-daemon",
    "curl",
    "jq",
    "git",
    "python3",
)

RECEIPT_FIELDS = (
    "maintenance_task_id",
    "os_identity_status",
    "node_identity_status",
    "reboot_guard_status",
    "reboot_performed",
    "packages_required_count",
    "packages_preexisting_count",
    "packages_added_count",
    "package_status",
    "display_manager_status",
    "autologin_status",
    "pipewire_status",
    "vaapi_status",
    "mpv_status",
    "chromium_status",
    "ssh_status",
    "gateway_postcheck_status",
    "physical_audio_status",
    "physical_video_status",
    "rollback_ready",
    "rollback_applied",
    "mutation_executor_receipt_hash",
    "final_postcheck_receipt_hash",
    "audit_receipt_hash",
    "stable_reason",
    "success_criteria",
)

_ALLOWED_FIELDS = frozenset(
    {
        "Mode",
        "Maintenance Task ID",
        "Repository",
        "Expected Main SHA",
        "Operator Approval",
        "Target",
    }
)
_FIELD_RE = re.compile(r"^\s*(?P<field>[A-Za-z][A-Za-z0-9 _-]{0,80}):\s*(?P<value>.*?)\s*$")
_PUBLIC_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:=-]+$")


@dataclass(frozen=True)
class RuntimeInput:
    repository: str
    expected_main_sha: str
    operator_approval: str
    target: str


def execute_debian_media_bootstrap_task(
    body: str,
    *,
    registered_clean_main_sha: str,
    github_main_sha: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    runtime_input = parse_runtime_input(body)
    validate_main_sha(
        runtime_input.expected_main_sha,
        registered_clean_main_sha=registered_clean_main_sha,
        github_main_sha=github_main_sha,
    )
    request = build_bootstrap_request(environment=environment)
    executor_receipt = execute_home_edge_request(request.to_mapping())
    public = public_receipt_from_executor_stdout(executor_receipt.to_mapping())
    public["mutation_executor_receipt_hash"] = executor_receipt.receipt_hash
    postcheck_status, postcheck_hash = _gateway_postcheck_status(environment=environment)
    public["gateway_postcheck_status"] = postcheck_status
    public["final_postcheck_receipt_hash"] = postcheck_hash
    if postcheck_status != "ok":
        public["success_criteria"] = "not_met"
    public["audit_receipt_hash"] = _audit_hash(public)
    return public


def parse_runtime_input(body: str) -> RuntimeInput:
    fields: dict[str, str] = {}
    duplicates: set[str] = set()
    for line in _metadata_lines(body):
        match = _FIELD_RE.match(line)
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
    unknown = sorted(set(fields) - _ALLOWED_FIELDS)
    if unknown:
        raise ValueError("unknown_runtime_input_field")
    if fields.get("Mode") != "RUNTIME_MAINTENANCE_TASK":
        raise ValueError("runtime_mode_mismatch")
    if fields.get("Maintenance Task ID") != TASK_ID:
        raise ValueError("maintenance_task_id_mismatch")
    runtime_input = RuntimeInput(
        repository=fields.get("Repository", ""),
        expected_main_sha=fields.get("Expected Main SHA", ""),
        operator_approval=fields.get("Operator Approval", ""),
        target=fields.get("Target", ""),
    )
    if runtime_input.repository != REPOSITORY:
        raise ValueError("repository_mismatch")
    if EXPECTED_MAIN_SHA_RE.fullmatch(runtime_input.expected_main_sha) is None:
        raise ValueError("expected_main_sha_malformed")
    if runtime_input.operator_approval != OPERATOR_APPROVAL:
        raise ValueError("operator_approval_mismatch")
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


def build_bootstrap_request(
    *, environment: Mapping[str, str] | None = None
) -> HomeEdgeExecRequest:
    env = os.environ if environment is None else environment
    secret = env.get(EXEC_HMAC_SECRET_ENV, "")
    if not secret:
        raise ValueError("home_edge_exec_hmac_secret_missing")
    request = HomeEdgeExecRequest.from_mapping(
        {
            "request_id": f"{TASK_ID}-{uuid4()}",
            "node_id": TARGET_NODE,
            "execution_lane": "privileged_mutation",
            "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "operator_approval_ref": APPROVAL_REF,
            "idempotency_key": IDEMPOTENCY_KEY,
            "run_as": "root",
            "mode": "script",
            "script": BOOTSTRAP_SCRIPT,
            "script_interpreter": "bash",
            "timestamp": datetime.now(UTC).isoformat(),
            "nonce": f"{TASK_ID}-{uuid4()}",
            "max_output_bytes": 65536,
        }
    )
    signature = sign_request(request, secret)
    return HomeEdgeExecRequest.from_mapping({**request.to_mapping(include_signature=False), "signature": signature})


def public_receipt_from_executor_stdout(receipt: Mapping[str, Any]) -> dict[str, object]:
    if receipt.get("status") != "ok" or receipt.get("exit_code") != 0:
        return _blocked_receipt("executor_receipt_not_ok")
    stdout = receipt.get("stdout")
    if not isinstance(stdout, str):
        return _blocked_receipt("executor_stdout_missing")
    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError:
        return _blocked_receipt("executor_stdout_not_json")
    if not isinstance(decoded, dict):
        return _blocked_receipt("executor_stdout_not_object")
    return sanitize_public_receipt(decoded)


def sanitize_public_receipt(receipt: Mapping[str, Any]) -> dict[str, object]:
    sanitized = _blocked_receipt("invalid_public_receipt")
    for field in RECEIPT_FIELDS:
        if field not in receipt:
            raise ValueError("receipt_field_missing")
    sanitized.clear()
    for field in RECEIPT_FIELDS:
        value = receipt[field]
        if isinstance(value, bool):
            sanitized[field] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            sanitized[field] = value
        elif isinstance(value, str) and _PUBLIC_VALUE_RE.fullmatch(value):
            sanitized[field] = value
        else:
            raise ValueError("receipt_field_not_public_safe")
    if sanitized["maintenance_task_id"] != TASK_ID:
        raise ValueError("receipt_task_id_mismatch")
    return sanitized


def receipt_status_lines(receipt: Mapping[str, object]) -> list[str]:
    return [f"{field}={receipt[field]}" for field in RECEIPT_FIELDS]


def success_criteria_met(receipt: Mapping[str, object]) -> bool:
    return (
        receipt.get("success_criteria") == "met"
        and receipt.get("package_status") == "installed"
        and receipt.get("autologin_status") == "configured"
        and receipt.get("display_manager_status") == "service_active"
        and receipt.get("ssh_status") == "service_active"
        and receipt.get("reboot_performed") is False
        and receipt.get("physical_audio_status") == "physical_pending"
        and receipt.get("physical_video_status") == "physical_pending"
        and receipt.get("gateway_postcheck_status") == "ok"
    )


def _gateway_postcheck_status(
    *, environment: Mapping[str, str] | None = None
) -> tuple[str, str]:
    env = os.environ if environment is None else environment
    secret = env.get(EXEC_HMAC_SECRET_ENV, "")
    if not secret:
        return "missing_secret", "unavailable"
    request = HomeEdgeExecRequest.from_mapping(
        {
            "request_id": f"{TASK_ID}-postcheck-{uuid4()}",
            "node_id": TARGET_NODE,
            "execution_lane": "read_only",
            "argv": ["/usr/bin/true"],
            "timeout_seconds": 30,
            "operator_approval_ref": "root-read-only:home-edge-01-debian-media-bootstrap-postcheck-v1",
            "idempotency_key": f"{IDEMPOTENCY_KEY}-postcheck-{uuid4()}",
            "run_as": "root",
            "mode": "argv",
            "timestamp": datetime.now(UTC).isoformat(),
            "nonce": f"{TASK_ID}-postcheck-{uuid4()}",
            "max_output_bytes": 1024,
        }
    )
    signed = HomeEdgeExecRequest.from_mapping(
        {
            **request.to_mapping(include_signature=False),
            "signature": sign_request(request, secret),
        }
    )
    try:
        receipt = execute_home_edge_request(signed.to_mapping())
    except Exception:  # noqa: BLE001 - public report must be stable.
        return "blocked", "unavailable"
    return (
        "ok" if receipt.status == "ok" and receipt.exit_code == 0 else "blocked",
        receipt.receipt_hash,
    )


def _metadata_lines(body: str) -> list[str]:
    metadata = (body or "").split("```task", 1)[0]
    return [line for line in metadata.splitlines() if not line.lstrip().startswith("#")]


def _blocked_receipt(reason: str) -> dict[str, object]:
    receipt: dict[str, object] = {
        "maintenance_task_id": TASK_ID,
        "os_identity_status": "unverified",
        "node_identity_status": "unverified",
        "reboot_guard_status": "unverified",
        "reboot_performed": False,
        "packages_required_count": len(FIXED_PACKAGES),
        "packages_preexisting_count": 0,
        "packages_added_count": 0,
        "package_status": "blocked",
        "display_manager_status": "blocked",
        "autologin_status": "blocked",
        "pipewire_status": "blocked",
        "vaapi_status": "blocked",
        "mpv_status": "blocked",
        "chromium_status": "blocked",
        "ssh_status": "blocked",
        "gateway_postcheck_status": "blocked",
        "physical_audio_status": "physical_pending",
        "physical_video_status": "physical_pending",
        "rollback_ready": True,
        "rollback_applied": False,
        "mutation_executor_receipt_hash": "unavailable",
        "final_postcheck_receipt_hash": "unavailable",
        "audit_receipt_hash": "pending",
        "stable_reason": reason,
        "success_criteria": "not_met",
    }
    receipt["audit_receipt_hash"] = _audit_hash(receipt)
    return receipt


def _audit_hash(receipt: Mapping[str, object]) -> str:
    payload = {
        key: value
        for key, value in receipt.items()
        if key in RECEIPT_FIELDS and key != "audit_receipt_hash"
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


BOOTSTRAP_SCRIPT = r'''set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
DESKTOP_USER="${SKELETON_MEDIA_DESKTOP_USER:-skeleton}"
DESKTOP_UID="${SKELETON_MEDIA_DESKTOP_UID:-1000}"
DESKTOP_HOME="${SKELETON_MEDIA_DESKTOP_HOME:-/home/$DESKTOP_USER}"
DESKTOP_GROUP="$(id -gn "$DESKTOP_USER" 2>/dev/null || true)"
[ -n "$DESKTOP_GROUP" ] || DESKTOP_GROUP="$DESKTOP_USER"
TASK_ID="home_edge_01_debian_media_bootstrap_v1"
MARKER="/var/lib/skeleton/home-edge-01/debian-media-bootstrap-v1.complete"
STATE_ROOT="/var/lib/skeleton/home-edge-01/debian-media-bootstrap-v2"
ROLLBACK_ROOT="$STATE_ROOT/rollback"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
ROLLBACK_DIR="$ROLLBACK_ROOT/$RUN_ID"
MANIFEST="$ROLLBACK_DIR/manifest.tsv"
LOG_DIR=""
PACKAGES=(openssh-server sudo pipewire pipewire-pulse wireplumber alsa-utils mpv yt-dlp intel-media-va-driver vainfo chromium xserver-xorg lightdm openbox dbus-x11 avahi-daemon curl jq git python3)
GUARDS=(skeleton-home-edge-first-boot-guard.service skeleton-home-edge-first-boot-guard.timer home-edge-first-boot-guard.service home-edge-first-boot-guard.timer)
CONFIG_PATHS=(/etc/lightdm/lightdm.conf.d/50-skeleton-home-edge-autologin.conf $DESKTOP_HOME/.config/openbox/autostart $DESKTOP_HOME/.config/mpv/mpv.conf /etc/chromium/policies/managed/skeleton-home-edge-media.json)
GUARD_HASH_skeleton_home_edge_first_boot_guard_service="7f3c0a92a97a350b2b07254c39290bc391531309e7c7f06d97680fc8d4a9507c"
GUARD_HASH_skeleton_home_edge_first_boot_guard_timer="7f3c0a92a97a350b2b07254c39290bc391531309e7c7f06d97680fc8d4a9507c"
GUARD_HASH_home_edge_first_boot_guard_service="7f3c0a92a97a350b2b07254c39290bc391531309e7c7f06d97680fc8d4a9507c"
GUARD_HASH_home_edge_first_boot_guard_timer="7f3c0a92a97a350b2b07254c39290bc391531309e7c7f06d97680fc8d4a9507c"
guard_status="not_present"
rollback_applied=false
rollback_ready=false
packages_preexisting=0
packages_added=0
boot_id_before="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
stable_reason="completed"
receipt_emitted=false
bounded_log() {
  local name="$1"
  if [ -n "$LOG_DIR" ]; then
    printf '%s/%s.log' "$LOG_DIR" "$name"
  else
    printf '/dev/null'
  fi
}
emit_receipt() {
  if [ "$receipt_emitted" = true ]; then exit 70; fi
  receipt_emitted=true
  local success="$1"
  local package_status="$2"
  local display_status="$3"
  local autologin_status="$4"
  local pipewire_status="$5"
  local vaapi_status="$6"
  local mpv_status="$7"
  local chromium_status="$8"
  local ssh_status="$9"
  local criteria="${10}"
  local hash
  hash="$(printf '%s:%s:%s:%s:%s:%s' "$TASK_ID" "$package_status" "$display_status" "$autologin_status" "$pipewire_status" "$stable_reason" | sha256sum | awk '{print $1}')"
  printf '{"maintenance_task_id":"%s",' "$TASK_ID"
  printf '"os_identity_status":"%s",' "$success"
  printf '"node_identity_status":"%s",' "$success"
  printf '"reboot_guard_status":"%s",' "$guard_status"
  printf '"reboot_performed":false,'
  printf '"packages_required_count":%s,' "${#PACKAGES[@]}"
  printf '"packages_preexisting_count":%s,' "$packages_preexisting"
  printf '"packages_added_count":%s,' "$packages_added"
  printf '"package_status":"%s",' "$package_status"
  printf '"display_manager_status":"%s",' "$display_status"
  printf '"autologin_status":"%s",' "$autologin_status"
  printf '"pipewire_status":"%s",' "$pipewire_status"
  printf '"vaapi_status":"%s",' "$vaapi_status"
  printf '"mpv_status":"%s",' "$mpv_status"
  printf '"chromium_status":"%s",' "$chromium_status"
  printf '"ssh_status":"%s",' "$ssh_status"
  printf '"gateway_postcheck_status":"pending",'
  printf '"physical_audio_status":"physical_pending",'
  printf '"physical_video_status":"physical_pending",'
  printf '"rollback_ready":%s,' "$rollback_ready"
  printf '"rollback_applied":%s,' "$rollback_applied"
  printf '"mutation_executor_receipt_hash":"pending",'
  printf '"final_postcheck_receipt_hash":"pending",'
  printf '"audit_receipt_hash":"%s",' "$hash"
  printf '"stable_reason":"%s",' "$stable_reason"
  printf '"success_criteria":"%s"' "$criteria"
  printf '}\n'
}
block() { stable_reason="$1"; emit_receipt blocked blocked blocked blocked blocked blocked blocked blocked blocked not_met; exit 10; }
run_quiet() {
  local label="$1"
  shift
  local log
  log="$(bounded_log "$label")"
  "$@" >"$log" 2>&1
}
ensure_private_state() {
  install -d -m 0700 "$STATE_ROOT" "$ROLLBACK_ROOT" "$ROLLBACK_DIR" || return 1
  LOG_DIR="$ROLLBACK_DIR/logs"
  install -d -m 0700 "$LOG_DIR" || return 1
  : >"$MANIFEST" || return 1
  chmod 0600 "$MANIFEST" || return 1
}
root_device_safe() {
  local src pk root tran rm type
  src="$(findmnt -n -o SOURCE / 2>/dev/null)" || return 1
  [ -n "$src" ] || return 1
  case "$src" in overlay|tmpfs|aufs|none) return 1 ;; esac
  root="$(lsblk -ndo PKNAME "$src" 2>/dev/null | head -n1)"
  [ -n "$root" ] || root="$(lsblk -ndo NAME "$src" 2>/dev/null | head -n1)"
  [ -n "$root" ] || return 1
  while :; do
    pk="$(lsblk -ndo PKNAME "/dev/$root" 2>/dev/null | head -n1)"
    [ -n "$pk" ] || break
    root="$pk"
  done
  tran="$(lsblk -ndo TRAN "/dev/$root" 2>/dev/null | tr -d ' ' | head -n1)"
  rm="$(lsblk -ndo RM "/dev/$root" 2>/dev/null | tr -d ' ' | head -n1)"
  type="$(lsblk -ndo TYPE "/dev/$root" 2>/dev/null | tr -d ' ' | head -n1)"
  [ "$rm" = "0" ] || return 1
  [ "$tran" != "usb" ] || return 1
  [ "$type" = "disk" ] || [ "$type" = "crypt" ] || [ "$type" = "lvm" ] || return 1
}
record_config_state() {
  mkdir -p "$(dirname "$MANIFEST")" || return 1
  : >"$MANIFEST" || return 1
  local path rel backup mode uid gid
  for path in "${CONFIG_PATHS[@]}"; do
    rel="${path#/}"
    backup="$ROLLBACK_DIR/files/$rel"
    if [ -e "$path" ]; then
      install -d -m 0700 "$(dirname "$backup")" || return 1
      cp -a "$path" "$backup" || return 1
      chmod 0600 "$backup" || return 1
      mode="$(stat -c '%a' "$path" 2>/dev/null)" || return 1
      uid="$(stat -c '%u' "$path" 2>/dev/null)" || return 1
      gid="$(stat -c '%g' "$path" 2>/dev/null)" || return 1
      printf '%s\texisted\t%s\t%s\t%s\t%s\n' "$path" "$mode" "$uid" "$gid" "$backup" >>"$MANIFEST" || return 1
    else
      printf '%s\tcreated\t0\t0\t0\t-\n' "$path" >>"$MANIFEST" || return 1
    fi
  done
  chmod 0600 "$MANIFEST" || return 1
  rollback_ready=true
  [ -r "$MANIFEST" ] || return 1
}
restore_configs() {
  local path state mode uid gid backup failed=false
  while IFS="$(printf '\t')" read -r path state mode uid gid backup; do
    case "$path" in
      /etc/lightdm/lightdm.conf.d/50-skeleton-home-edge-autologin.conf|$DESKTOP_HOME/.config/openbox/autostart|$DESKTOP_HOME/.config/mpv/mpv.conf|/etc/chromium/policies/managed/skeleton-home-edge-media.json) ;;
      *) failed=true; continue ;;
    esac
    if [ "$state" = "existed" ]; then
      install -d -m 0755 "$(dirname "$path")" || { failed=true; continue; }
      cp -a "$backup" "$path" || { failed=true; continue; }
      chown "$uid:$gid" "$path" 2>/dev/null || true
      chmod "$mode" "$path" 2>/dev/null || true
    elif [ "$state" = "created" ]; then
      rm -f -- "$path" || failed=true
    else
      failed=true
    fi
  done <"$MANIFEST"
  [ "$failed" = false ]
}
rollback_new_packages() {
  if [ -s "$added_file" ]; then
    if xargs -r apt-get remove -y --purge <"$added_file" >"$(bounded_log apt_remove)" 2>&1; then
      rollback_applied=true
    else
      return 1
    fi
  fi
}
fail_after_mutation() {
  local reason="$1"
  rollback_new_packages && restore_configs || { stable_reason="rollback_failed"; emit_receipt verified failed blocked blocked blocked blocked blocked blocked blocked not_met; exit 60; }
  stable_reason="$reason"
  emit_receipt verified failed blocked blocked blocked blocked blocked blocked blocked not_met
  exit 50
}
[ "$(id -u)" = "0" ] || block missing_root_execution
[ -r /etc/os-release ] || block os_release_missing
. /etc/os-release
[ "${ID:-}" = "debian" ] || block os_not_debian
case "${VERSION_ID:-}" in 13*) ;; *) block os_version_not_13 ;; esac
[ "$(hostname)" = "home-edge-01" ] || block hostname_mismatch
desktop_entry="$(getent passwd "$DESKTOP_USER" || true)"
[ -n "$desktop_entry" ] || block user_missing
IFS=: read -r user_name _ user_uid _ _ user_home _ <<EOF_USER
$desktop_entry
EOF_USER
[ "$user_name" = "$DESKTOP_USER" ] || block user_mismatch
[ "$user_uid" = "1000" ] || block uid_mismatch
[ "$user_home" = "$DESKTOP_HOME" ] || block home_mismatch
[ -w / ] || block root_not_writable
free_kib="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}')"
[ "${free_kib:-0}" -ge 8388608 ] || block free_space_low
[ "$(findmnt -n -o FSTYPE / 2>/dev/null || true)" != "overlay" ] || block external_live_root_ambiguous
root_device_safe || block external_live_root_ambiguous
[ ! -e /var/lib/dpkg/lock-frontend ] || ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || block dpkg_lock_active
[ ! -e /var/lib/dpkg/lock ] || ! fuser /var/lib/dpkg/lock >/dev/null 2>&1 || block dpkg_lock_active
[ "$(findmnt -n -o OPTIONS / | grep -c '(^|,)ro(,|$)' || true)" = "0" ] || block filesystem_read_only
ensure_private_state || block rollback_manifest_unavailable
for unit in "${GUARDS[@]}"; do
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | awk '{print $1}' | grep -qx "$unit"; then
    exec_start="$(systemctl show -p ExecStart --value "$unit" 2>/dev/null || true)"
    exec_path="$(printf '%s\n' "$exec_start" | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -n1)"
    [ -n "$exec_path" ] || { guard_status="unverified"; block reboot_guard_unverified; }
    [ -f "$exec_path" ] || { guard_status="unverified"; block reboot_guard_unverified; }
    [ "$(stat -c '%u' "$exec_path" 2>/dev/null)" = "0" ] || { guard_status="unverified"; block reboot_guard_unverified; }
    mode="$(stat -c '%a' "$exec_path" 2>/dev/null)" || { guard_status="unverified"; block reboot_guard_unverified; }
    [ $((8#$mode & 022)) -eq 0 ] || { guard_status="unverified"; block reboot_guard_unverified; }
    grep -q 'skeleton.home_edge.debian13.first_boot_guard.v1' "$exec_path" || { guard_status="unverified"; block reboot_guard_unverified; }
    guard_key="$(printf '%s' "$unit" | tr '.-' '__')"
    expected_var="GUARD_HASH_${guard_key}"
    expected_hash="${!expected_var:-}"
    actual_hash="$(sha256sum "$exec_path" | awk '{print $1}')"
    [ -n "$expected_hash" ] && [ "$actual_hash" = "$expected_hash" ] || { guard_status="unverified"; block reboot_guard_unverified; }
    systemctl disable --now "$unit" >"$(bounded_log guard_disable)" 2>&1 || { guard_status="unverified"; block reboot_guard_unverified; }
    guard_status="disabled_verified"
  fi
done
preexisting_file="$(mktemp)"
added_file="$(mktemp)"
cleanup() { rm -f "$preexisting_file" "$added_file"; }
trap cleanup EXIT
for pkg in "${PACKAGES[@]}"; do
  if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -qx "install ok installed"; then
    printf '%s\n' "$pkg" >>"$preexisting_file"
  fi
done
packages_preexisting="$(wc -l <"$preexisting_file" | tr -d ' ')"
missing_packages=false
for pkg in "${PACKAGES[@]}"; do
  grep -qx "$pkg" "$preexisting_file" || missing_packages=true
done
desired_lightdm='[Seat:*]
autologin-user=$DESKTOP_USER
autologin-user-timeout=0
user-session=openbox
'
desired_openbox='xsetroot -solid black
'
desired_mpv='hwdec=auto-safe
ao=pipewire
'
desired_chromium='{"HardwareAccelerationModeEnabled":true}
'
needs_config=false
printf '%s' "$desired_lightdm" | cmp -s - /etc/lightdm/lightdm.conf.d/50-skeleton-home-edge-autologin.conf || needs_config=true
printf '%s' "$desired_openbox" | cmp -s - $DESKTOP_HOME/.config/openbox/autostart || needs_config=true
printf '%s' "$desired_mpv" | cmp -s - $DESKTOP_HOME/.config/mpv/mpv.conf || needs_config=true
printf '%s' "$desired_chromium" | cmp -s - /etc/chromium/policies/managed/skeleton-home-edge-media.json || needs_config=true
if [ "$missing_packages" = true ]; then
  if ! apt-get update >"$(bounded_log apt_update)" 2>&1; then stable_reason="apt_update_failed"; emit_receipt verified blocked blocked blocked blocked blocked blocked blocked blocked not_met; exit 20; fi
  if ! apt-get install -y --no-install-recommends "${PACKAGES[@]}" >"$(bounded_log apt_install)" 2>&1; then
    for pkg in "${PACKAGES[@]}"; do
      if ! grep -qx "$pkg" "$preexisting_file" && dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -qx "install ok installed"; then printf '%s\n' "$pkg" >>"$added_file"; fi
    done
    fail_after_mutation apt_install_failed
  fi
  for pkg in "${PACKAGES[@]}"; do
    if ! grep -qx "$pkg" "$preexisting_file" && dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -qx "install ok installed"; then printf '%s\n' "$pkg" >>"$added_file"; fi
  done
fi
packages_added="$(sort -u "$added_file" | wc -l | tr -d ' ')"
if [ "$needs_config" = true ]; then
  record_config_state || block rollback_manifest_unavailable
  install -d -m 0755 /etc/lightdm/lightdm.conf.d /etc/chromium/policies/managed || fail_after_mutation config_prepare_failed
  install -d -m 0755 -o "$DESKTOP_UID" -g "$DESKTOP_GROUP" "$DESKTOP_HOME/.config/openbox" "$DESKTOP_HOME/.config/mpv" || fail_after_mutation config_prepare_failed
  printf '%s' "$desired_lightdm" >/etc/lightdm/lightdm.conf.d/50-skeleton-home-edge-autologin.conf || fail_after_mutation config_write_failed
  chmod 0644 /etc/lightdm/lightdm.conf.d/50-skeleton-home-edge-autologin.conf || fail_after_mutation config_write_failed
  printf '%s' "$desired_openbox" >$DESKTOP_HOME/.config/openbox/autostart || fail_after_mutation config_write_failed
  chown "$DESKTOP_USER:$DESKTOP_GROUP" "$DESKTOP_HOME/.config/openbox/autostart" || fail_after_mutation config_write_failed
  chmod 0755 $DESKTOP_HOME/.config/openbox/autostart || fail_after_mutation config_write_failed
  printf '%s' "$desired_mpv" >$DESKTOP_HOME/.config/mpv/mpv.conf || fail_after_mutation config_write_failed
  chown "$DESKTOP_USER:$DESKTOP_GROUP" "$DESKTOP_HOME/.config/mpv/mpv.conf" || fail_after_mutation config_write_failed
  chmod 0644 $DESKTOP_HOME/.config/mpv/mpv.conf || fail_after_mutation config_write_failed
  printf '%s' "$desired_chromium" >/etc/chromium/policies/managed/skeleton-home-edge-media.json || fail_after_mutation config_write_failed
  chmod 0644 /etc/chromium/policies/managed/skeleton-home-edge-media.json || fail_after_mutation config_write_failed
else
  record_config_state || block rollback_manifest_unavailable
fi
for group in audio video render input; do getent group "$group" >/dev/null 2>&1 && usermod -aG "$group" "$DESKTOP_USER" >"$(bounded_log usermod_$group)" 2>&1; done
run_quiet systemctl_ssh systemctl enable --now ssh.service || true
run_quiet systemctl_avahi systemctl enable --now avahi-daemon.service || true
pipewire_status="pending_session"
if [ -S "/run/user/$DESKTOP_UID/bus" ]; then
  if runuser -u "$DESKTOP_USER" -- env XDG_RUNTIME_DIR="/run/user/$DESKTOP_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DESKTOP_UID/bus" systemctl --user enable --now pipewire.service pipewire-pulse.service wireplumber.service >"$(bounded_log user_units)" 2>&1; then
    pipewire_status="session_ready"
  fi
fi
package_status="installed"
for pkg in "${PACKAGES[@]}"; do dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -qx "install ok installed" || package_status="failed"; done
if [ "$package_status" != "installed" ]; then fail_after_mutation package_parity_failed; fi
run_quiet systemctl_lightdm systemctl enable --now lightdm.service || true
ssh_status="$(systemctl is-active --quiet ssh.service >/dev/null 2>&1 && echo service_active || echo pending_service)"
display_status="$(systemctl is-active --quiet lightdm.service >/dev/null 2>&1 && echo service_active || echo pending_service)"
autologin_status="$(printf '%s' "$desired_lightdm" | cmp -s - /etc/lightdm/lightdm.conf.d/50-skeleton-home-edge-autologin.conf && echo configured || echo blocked)"
mpv_status="$(mpv --version >/dev/null 2>&1 && echo configured || echo blocked)"
chromium_status="$(chromium --version >/dev/null 2>&1 && echo configured || echo blocked)"
if [ ! -e /dev/dri/renderD128 ]; then
  vaapi_status="render_node_missing"
elif timeout 10 vainfo --display drm --device /dev/dri/renderD128 >"$(bounded_log vainfo)" 2>&1; then
  vaapi_status="configured"
else
  vaapi_status="driver_missing"
fi
if [ -S "/run/user/$DESKTOP_UID/bus" ]; then
  runuser -u "$DESKTOP_USER" -- env XDG_RUNTIME_DIR="/run/user/$DESKTOP_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DESKTOP_UID/bus" pactl list short sinks >"$(bounded_log pactl_sinks)" 2>&1 || true
fi
if [ "$boot_id_before" != "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)" ]; then
  fail_after_mutation boot_id_changed
fi
if [ "$ssh_status" != "service_active" ] || [ "$display_status" != "service_active" ] || [ "$autologin_status" != "configured" ] || [ "$mpv_status" = "blocked" ] || [ "$chromium_status" = "blocked" ]; then
  rollback_new_packages && restore_configs || { stable_reason="rollback_failed"; emit_receipt verified "$package_status" "$display_status" "$autologin_status" "$pipewire_status" "$vaapi_status" "$mpv_status" "$chromium_status" "$ssh_status" not_met; exit 60; }
  stable_reason="verification_failed"
  emit_receipt verified "$package_status" "$display_status" "$autologin_status" "$pipewire_status" "$vaapi_status" "$mpv_status" "$chromium_status" "$ssh_status" not_met
  exit 50
fi
install -d -m 0700 /var/lib/skeleton/home-edge-01
printf 'skeleton.home_edge.debian_media_bootstrap.v1\n' >"$MARKER"
chmod 0600 "$MARKER"
if [ -f "$MARKER" ] && [ "$missing_packages" = false ] && [ "$needs_config" = false ] && [ "$packages_added" = "0" ]; then
  stable_reason="already_complete"
fi
emit_receipt verified "$package_status" "$display_status" "$autologin_status" "$pipewire_status" "$vaapi_status" "$mpv_status" "$chromium_status" "$ssh_status" met
'''
