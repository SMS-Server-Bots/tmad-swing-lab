# TMAD v6.1.44 observability watchdog

This release adds a read-only watchdog around runtime publication freshness. It does not alter Scanner, SELECTIVE, CORE, GEX, Timing, FUSION or trade parameters.

The watchdog compares the last commit timestamps of `runtime-logs` and `runtime-data` against the newest local runtime writes. A stream is considered stale only when the branch is older than the configured threshold and the local runtime is newer than the branch snapshot. The result is persisted to `04_RUNTIME/observability/runtime_publication_status.json`.
