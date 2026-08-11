#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT=""
RUNNER_USER="agent"
RUNNER_SERVICE="skeleton-runner-poll.service"
PROTECTED_INSTALLER_PATH="/usr/local/libexec/skeleton/home-edge/media-source-snapshot-installer/install_home_edge_media_source_snapshot_signer.sh"
INSTALL_ROOT="/usr/local/lib/skeleton/home-edge/media-source-snapshot"
EXEC_ROOT="/usr/local/libexec/skeleton/home-edge/media-source-snapshot"
SUDOERS_PATH="/etc/sudoers.d/skeleton-home-edge-media-source-snapshot-signer"
PAYLOAD_REL="scripts/home_edge_media_source_snapshot_signer_payload.py"
WRAPPER_REL="scripts/home_edge_media_source_snapshot_signer"
CONTRACT_REL="core/home_edge/media_source_snapshot.py"
PAYLOAD_BLOB_SHA="9d1d69216116f42f195e2cad4c0f385754d8e6e0"
WRAPPER_BLOB_SHA="24620d9e9fe4f62c055113e6aeefb2d0984be2d5"
CONTRACT_BLOB_SHA="7ddfbfc54d55eccbbd95c73ad0016221e7222cd9"
COMMITTED=0
BACKUP_DIR=""
STAGING_PARENT=""
HAD_INSTALL_ROOT=0
HAD_EXEC_ROOT=0
HAD_SUDOERS=0
BACKUPS_READY=0
ACTIVATION_STARTED=0

usage() {
  cat <<'EOF'
Usage: sudo scripts/install_home_edge_media_source_snapshot_signer.sh [--repo-root PATH]

Copies exact reviewed signer files as inert data into a root-owned immutable runtime.
The existing Runner maintenance fabric must first copy/hash-verify this
installer as inert data at:
  /usr/local/libexec/skeleton/home-edge/media-source-snapshot-installer/install_home_edge_media_source_snapshot_signer.sh
Root execution is accepted only from that protected installed copy. Sudo access
is bound only to the canonical Skeleton Runner service account: agent.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT="${2:?missing value for --repo-root}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$(dirname -- "$(dirname -- "${BASH_SOURCE[0]}")")"
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  printf 'BLOCKED: installer must run as root\n' >&2
  exit 2
fi
if [[ "$(readlink -f -- "$0")" != "$PROTECTED_INSTALLER_PATH" ]]; then
  printf 'BLOCKED: root must execute only protected installed signer installer copy\n' >&2
  exit 2
fi
if [[ -L "$PROTECTED_INSTALLER_PATH" || ! -f "$PROTECTED_INSTALLER_PATH" ]]; then
  printf 'BLOCKED: protected signer installer copy is unsafe\n' >&2
  exit 2
fi
protected_uid="$(stat -c '%u' -- "$PROTECTED_INSTALLER_PATH")"
protected_gid="$(stat -c '%g' -- "$PROTECTED_INSTALLER_PATH")"
protected_mode="$(stat -c '%a' -- "$PROTECTED_INSTALLER_PATH")"
if [[ "$protected_uid" != "0" || "$protected_gid" != "0" || $((8#$protected_mode & 8#022)) -ne 0 ]]; then
  printf 'BLOCKED: protected signer installer copy ownership or mode is unsafe\n' >&2
  exit 2
fi
if ! getent passwd "$RUNNER_USER" >/dev/null; then
  printf 'BLOCKED: canonical runner user is unavailable\n' >&2
  exit 2
fi
actual_runner_user="$(/usr/bin/systemctl show --property=User --value "$RUNNER_SERVICE" 2>/dev/null || true)"
if [[ "$actual_runner_user" != "$RUNNER_USER" ]]; then
  printf 'BLOCKED: live Runner service user does not match canonical agent account\n' >&2
  exit 2
fi

PAYLOAD_SRC="$REPO_ROOT/$PAYLOAD_REL"
WRAPPER_SRC="$REPO_ROOT/$WRAPPER_REL"
CONTRACT_SRC="$REPO_ROOT/$CONTRACT_REL"

reviewed_blob_sha() {
  /usr/bin/git hash-object --no-filters --stdin < "$1"
}

validate_source_file() {
  local path="$1" max_bytes="$2" expected_blob="$3" size mode actual_blob
  if [[ -L "$path" || ! -f "$path" || ! -r "$path" ]]; then
    printf 'BLOCKED: reviewed signer source is not a readable regular file\n' >&2
    exit 2
  fi
  size="$(stat -c '%s' -- "$path")"
  mode="$(stat -c '%a' -- "$path")"
  if (( size <= 0 || size > max_bytes )); then
    printf 'BLOCKED: reviewed signer source size is unsafe\n' >&2
    exit 2
  fi
  if (( (8#$mode & 8#022) != 0 )); then
    printf 'BLOCKED: reviewed signer source is group/world writable\n' >&2
    exit 2
  fi
  actual_blob="$(reviewed_blob_sha "$path")"
  if [[ "$actual_blob" != "$expected_blob" ]]; then
    printf 'BLOCKED: reviewed signer source bytes do not match approved blob\n' >&2
    exit 2
  fi
}

validate_source_file "$PAYLOAD_SRC" $((128 * 1024)) "$PAYLOAD_BLOB_SHA"
validate_source_file "$WRAPPER_SRC" $((16 * 1024)) "$WRAPPER_BLOB_SHA"
validate_source_file "$CONTRACT_SRC" $((256 * 1024)) "$CONTRACT_BLOB_SHA"
for path in /etc/skeleton /etc/skeleton/home-edge-01.env /etc/skeleton/home-edge-executor-controller.env; do
  if [[ -L "$path" || ! -e "$path" ]]; then
    printf 'BLOCKED: controller boundary metadata is unavailable\n' >&2
    exit 2
  fi
done

if [[ -L "$INSTALL_ROOT" || ( -e "$INSTALL_ROOT" && ! -d "$INSTALL_ROOT" ) ]]; then
  printf 'BLOCKED: existing signer install root is unsafe\n' >&2
  exit 2
fi
if [[ -L "$EXEC_ROOT" || ( -e "$EXEC_ROOT" && ! -d "$EXEC_ROOT" ) ]]; then
  printf 'BLOCKED: existing signer executable root is unsafe\n' >&2
  exit 2
fi
if [[ -L "$SUDOERS_PATH" || ( -e "$SUDOERS_PATH" && ! -f "$SUDOERS_PATH" ) ]]; then
  printf 'BLOCKED: existing signer sudoers entry is unsafe\n' >&2
  exit 2
fi
[[ -e "$INSTALL_ROOT" ]] && HAD_INSTALL_ROOT=1
[[ -e "$EXEC_ROOT" ]] && HAD_EXEC_ROOT=1
[[ -e "$SUDOERS_PATH" ]] && HAD_SUDOERS=1

BACKUP_DIR="$(mktemp -d /tmp/skeleton-home-edge-snapshot-signer.XXXXXX)"
STAGING_PARENT="$(mktemp -d /tmp/skeleton-home-edge-snapshot-signer-stage.XXXXXX)"
rollback() {
  local rc=$?
  trap - EXIT
  rm -rf "$STAGING_PARENT" "$INSTALL_ROOT.new" "$EXEC_ROOT.new"
  if [[ $COMMITTED -eq 0 && $ACTIVATION_STARTED -eq 1 ]]; then
    if [[ $BACKUPS_READY -ne 1 ]]; then
      printf 'FATAL: signer activation started without complete backups\n' >&2
      exit 70
    fi
    if [[ $HAD_INSTALL_ROOT -eq 1 ]]; then
      rm -rf "$INSTALL_ROOT"
      mkdir -p "$(dirname "$INSTALL_ROOT")"
      cp -a "$BACKUP_DIR/install-root" "$INSTALL_ROOT"
    else
      rm -rf "$INSTALL_ROOT"
    fi
    if [[ $HAD_EXEC_ROOT -eq 1 ]]; then
      rm -rf "$EXEC_ROOT"
      mkdir -p "$(dirname "$EXEC_ROOT")"
      cp -a "$BACKUP_DIR/exec-root" "$EXEC_ROOT"
    else
      rm -rf "$EXEC_ROOT"
    fi
    if [[ $HAD_SUDOERS -eq 1 ]]; then
      install -o root -g root -m 0440 "$BACKUP_DIR/sudoers" "$SUDOERS_PATH"
    else
      rm -f "$SUDOERS_PATH"
    fi
  fi
  rm -rf "$BACKUP_DIR"
  exit "$rc"
}
trap rollback EXIT

if [[ $HAD_INSTALL_ROOT -eq 1 ]]; then cp -a "$INSTALL_ROOT" "$BACKUP_DIR/install-root"; fi
if [[ $HAD_EXEC_ROOT -eq 1 ]]; then cp -a "$EXEC_ROOT" "$BACKUP_DIR/exec-root"; fi
if [[ $HAD_SUDOERS -eq 1 ]]; then cp -a "$SUDOERS_PATH" "$BACKUP_DIR/sudoers"; fi
BACKUPS_READY=1
mkdir -p "$STAGING_PARENT/install" "$STAGING_PARENT/exec"

copy_stable_source() {
  local source="$1" destination="$2" mode="$3" expected_blob="$4" before after staged_blob
  before="$(stat -c '%d:%i:%s:%Y:%Z:%a:%u:%g' -- "$source")"
  cp --no-dereference -- "$source" "$destination"
  if [[ -L "$destination" || ! -f "$destination" ]]; then
    printf 'BLOCKED: inert copy did not produce a regular file\n' >&2
    exit 2
  fi
  chown root:root "$destination"
  chmod "$mode" "$destination"
  after="$(stat -c '%d:%i:%s:%Y:%Z:%a:%u:%g' -- "$source")"
  staged_blob="$(reviewed_blob_sha "$destination")"
  if [[ "$before" != "$after" || "$staged_blob" != "$expected_blob" ]]; then
    printf 'BLOCKED: reviewed signer source changed during inert copy\n' >&2
    exit 2
  fi
}

copy_stable_source "$PAYLOAD_SRC" "$STAGING_PARENT/install/signer_payload.py" 0555 "$PAYLOAD_BLOB_SHA"
copy_stable_source "$CONTRACT_SRC" "$STAGING_PARENT/install/contract_source.py" 0444 "$CONTRACT_BLOB_SHA"
copy_stable_source "$WRAPPER_SRC" "$STAGING_PARENT/exec/signer" 0555 "$WRAPPER_BLOB_SHA"

install -d -o root -g root -m 0755 "$(dirname "$INSTALL_ROOT")" "$(dirname "$EXEC_ROOT")"
rm -rf "$INSTALL_ROOT.new" "$EXEC_ROOT.new"
install -d -o root -g root -m 0755 "$INSTALL_ROOT.new" "$EXEC_ROOT.new"
install -o root -g root -m 0555 "$STAGING_PARENT/install/signer_payload.py" "$INSTALL_ROOT.new/signer_payload.py"
install -o root -g root -m 0444 "$STAGING_PARENT/install/contract_source.py" "$INSTALL_ROOT.new/contract_source.py"
install -o root -g root -m 0555 "$STAGING_PARENT/exec/signer" "$EXEC_ROOT.new/signer"

for installed in "$INSTALL_ROOT.new/signer_payload.py" "$INSTALL_ROOT.new/contract_source.py" "$EXEC_ROOT.new/signer"; do
  if [[ -L "$installed" || ! -f "$installed" ]]; then
    printf 'BLOCKED: immutable signer staging is unsafe\n' >&2
    exit 2
  fi
  mode="$(stat -c '%a' -- "$installed")"
  if (( (8#$mode & 8#022) != 0 )); then
    printf 'BLOCKED: immutable signer staging is writable by group/world\n' >&2
    exit 2
  fi
done
if [[ "$(reviewed_blob_sha "$INSTALL_ROOT.new/signer_payload.py")" != "$PAYLOAD_BLOB_SHA" \
   || "$(reviewed_blob_sha "$INSTALL_ROOT.new/contract_source.py")" != "$CONTRACT_BLOB_SHA" \
   || "$(reviewed_blob_sha "$EXEC_ROOT.new/signer")" != "$WRAPPER_BLOB_SHA" ]]; then
  printf 'BLOCKED: immutable signer staging bytes changed\n' >&2
  exit 2
fi
if [[ $BACKUPS_READY -ne 1 ]]; then
  printf 'BLOCKED: signer activation backups are incomplete\n' >&2
  exit 2
fi

# Disable any previous Runner entry before replacing the installed runtime. This
# makes a reinstall fail closed during the short activation window.
ACTIVATION_STARTED=1
rm -f "$SUDOERS_PATH"
if [[ $HAD_INSTALL_ROOT -eq 1 ]]; then rm -rf "$INSTALL_ROOT"; fi
if [[ $HAD_EXEC_ROOT -eq 1 ]]; then rm -rf "$EXEC_ROOT"; fi
mv -T "$INSTALL_ROOT.new" "$INSTALL_ROOT"
mv -T "$EXEC_ROOT.new" "$EXEC_ROOT"

cat > "$BACKUP_DIR/sudoers.new" <<EOF
$RUNNER_USER ALL=(root) NOPASSWD: $EXEC_ROOT/signer ""
EOF
chmod 0440 "$BACKUP_DIR/sudoers.new"
visudo -cf "$BACKUP_DIR/sudoers.new" >/dev/null
install -o root -g root -m 0440 "$BACKUP_DIR/sudoers.new" "$SUDOERS_PATH"

COMMITTED=1
rm -rf "$STAGING_PARENT" "$BACKUP_DIR"
trap - EXIT
printf 'DONE: Home Edge media source snapshot signer installed\n'
printf 'signer=%s\n' "$EXEC_ROOT/signer"
printf 'payload=%s\n' "$INSTALL_ROOT/signer_payload.py"
printf 'contract=%s\n' "$INSTALL_ROOT/contract_source.py"
printf 'sudoers=%s\n' "$SUDOERS_PATH"
