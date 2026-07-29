#!/usr/bin/env bash
set -euo pipefail
BASE=${SKELETON_CAST_BASE:-/home/skeleton/.local/lib/skeleton-cast}
STATE=${SKELETON_CAST_STATE:-/home/skeleton/.local/state/skeleton-cast}
SRC=$(cd "$(dirname "$0")/runtime" && pwd)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$STATE/backups/issue-2096-$STAMP"
mkdir -p "$BACKUP" "$STATE"
for name in resolver.py app.py; do
  install -m 0600 "$BASE/$name" "$BACKUP/$name"
  install -m 0755 "$SRC/$name" "$BASE/$name"
done
python3 -m py_compile "$BASE/resolver.py" "$BASE/app.py"
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
systemctl --user restart skeleton-cast.service
curl -fsS --retry 20 --retry-delay 1 http://127.0.0.1:8100/health >/dev/null
printf '%s\n' "$BACKUP"
