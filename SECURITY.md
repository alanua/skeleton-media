# Security policy

Do not report credentials, tokens, private keys, OAuth secrets, private device topology, production personal data, signing material, or private runtime state in a public issue.

Skeleton Media uses configuration/state outside the repository. Public source must use environment/config references instead of embedding household IP addresses, machine IDs, account IDs, or user-specific absolute paths.

Production media caches, watch history, Trakt tokens, IPTV credentials, release signing state, APK signing keys, user documents, and Home Edge device registries are not repository content.

Security-sensitive deployment changes remain bounded by the Skeleton control plane and its approval/audit model.
