#!/usr/bin/env bash
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 BACKUP_DIR" >&2; exit 2; }
BASE=${SKELETON_CAST_BASE:-/home/skeleton/.local/lib/skeleton-cast}
for name in resolver.py app.py; do install -m 0755 "$1/$name" "$BASE/$name"; done
python3 -m py_compile "$BASE/resolver.py" "$BASE/app.py"
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
systemctl --user restart skeleton-cast.service
curl -fsS --retry 20 --retry-delay 1 http://127.0.0.1:8100/health >/dev/null
