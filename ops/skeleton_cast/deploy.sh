#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'MSG'
Direct production deploy is intentionally disabled in the extracted Skeleton Media repository.

Reason:
- the running Home Edge app currently contains non-media integration glue that has not yet been split into public media modules;
- overwriting it from this repository could remove unrelated Home functionality;
- production mutations must run through the registered Skeleton Home Edge executor, with backup, approval, audit receipt and independent post-condition verification.

Use this repository for source, tests and staging. Re-enable deployment only after the production route glue is decomposed and a canonical Skeleton operation is registered.
MSG
exit 2
