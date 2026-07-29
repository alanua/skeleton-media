# Skeleton Cast canonical runtime

`runtime/app.py` and `runtime/resolver.py` are the repository-owned sources deployed to Home Edge.

Deploy: `ops/skeleton_cast/deploy.sh`
Rollback: `ops/skeleton_cast/rollback.sh <backup-directory>`

Issue #2096 adds explicit `origin_protected`, a one-hour restart-safe AniTube cooldown, `.html` request canonicalization and fast-fail before Chromium/public mirror retries. It does not bypass Cloudflare.
