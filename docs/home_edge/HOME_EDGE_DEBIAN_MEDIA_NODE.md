# Home Edge Debian Media Node Bootstrap

`home_edge_01_debian_media_bootstrap_v1` is an exact allowlisted runtime-maintenance task for finishing the already installed Debian 13 media node on `home-edge-01`.

The task accepts only these non-empty runtime fields:

```text
Mode: RUNTIME_MAINTENANCE_TASK
Maintenance Task ID: home_edge_01_debian_media_bootstrap_v1
Repository: alanua/Skeleton
Expected Main SHA: <40 lowercase hex main SHA>
Operator Approval: EXPLICIT_FINISH_DEBIAN_MEDIA_NODE_20260805
Target: home-edge-01
```

Any other non-empty field that could affect command, package, path, user, host, service, timeout, lane, run identity, or script behavior is rejected before a Home Edge request is built.

The runner verifies the expected main SHA against the registered local `main` commit and `origin/main`, then sends a fresh signed `HomeEdgeExecRequest` through `core.home_edge.executor_gateway.execute_home_edge_request`. The mutation request is bound to `operator_approval_ref=EXPLICIT_FINISH_DEBIAN_MEDIA_NODE_20260805` and the repaired v2 idempotency key. The task module does not provide direct SSH, subprocess transport, passwords, or arbitrary command execution.

The source-owned script installs only the fixed package list, verifies the root device is not overlay/removable/USB-backed, validates only the four reviewed first-boot guard unit names before any package or config mutation, configures LightDM autologin for `oleksii`, prepares a blank Openbox desktop, mpv, Chromium policy, and enables/starts `ssh`, `lightdm`, and `avahi-daemon`. Openbox autostart does not launch PipeWire, pipewire-pulse, or WirePlumber; those user units are managed only through the live `/run/user/1000/bus` session with fixed `runuser` and `systemctl --user enable --now` argv. It never performs power, disk, bootloader, route, firewall, Tailscale, or private media mutations.

Before config mutation the script creates a retained private rollback bundle under `/var/lib/skeleton/home-edge-01/debian-media-bootstrap-v2` with `0700` directories and `0600` manifest/backup files. Rollback restores overwritten fixed config files, removes files created by this operation, and removes only packages proven newly added by this operation.

The fixed node script emits exactly one JSON receipt on handled success or failure paths, while apt, dpkg, systemctl, vainfo, and other command output goes only to private bounded logs or `/dev/null`. The public receipt reports aggregate counts, booleans, hashes, and stable status codes only. Its audit hash includes the HomeEdge mutation executor `receipt_hash` and final signed read-only postcheck `receipt_hash`. Physical audio and video remain `physical_pending` until an operator observes them directly.
