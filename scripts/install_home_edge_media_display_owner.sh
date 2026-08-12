#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODULE="$ROOT/core/home_edge/media_display_ownership.py"
CLI="$ROOT/scripts/home_edge_media_display_owner.py"
TARGET_LIB="$HOME/.local/lib/skeleton/home_edge"
TARGET_BIN="$HOME/.local/bin/home-edge-media-display-owner"
STATE="$HOME/.local/state/skeleton/home-edge-media-display-owner"
BACKUP="$STATE/rollback"

[[ -f "$MODULE" ]] || { echo 'reason=module_missing' >&2; exit 2; }
[[ -f "$CLI" ]] || { echo 'reason=cli_missing' >&2; exit 2; }
/usr/bin/python3 -m py_compile "$MODULE" "$CLI"

mkdir -p "$TARGET_LIB" "$(dirname "$TARGET_BIN")" "$BACKUP"
chmod 700 "$STATE" "$BACKUP"

if [[ ! -e "$BACKUP/captured" ]]; then
  if [[ -f "$TARGET_LIB/media_display_ownership.py" ]]; then
    cp -a "$TARGET_LIB/media_display_ownership.py" "$BACKUP/media_display_ownership.py.previous"
  fi
  if [[ -f "$TARGET_BIN" ]]; then
    cp -a "$TARGET_BIN" "$BACKUP/home-edge-media-display-owner.previous"
  fi
  : > "$BACKUP/captured"
fi

install -m 0644 "$MODULE" "$TARGET_LIB/media_display_ownership.py"
install -m 0755 "$CLI" "$TARGET_BIN"

printf 'install_status=APPLIED\n'
printf 'operation=home_edge_media_display_owner_install\n'
printf 'network_endpoint=loopback_only\n'
printf 'mutating_media_actions=false\n'
printf 'private_values_output=false\n'
