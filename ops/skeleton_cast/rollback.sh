#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'MSG'
Direct rollback is intentionally disabled in the extracted Skeleton Media repository.

Production rollback remains owned by the registered Skeleton Home Edge operation and its audited backup/receipt chain. Do not mutate the live Home Edge installation from this standalone repository.
MSG
exit 2
