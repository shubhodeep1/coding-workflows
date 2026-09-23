<!-- changelog: fixed -->

- **Review/autofix now classifies workspace-guard startup failures instead of reporting an empty editor no-op.** Editor guard artifacts use a private runner-temp runtime, legacy `/tmp/codex-pr-*` workflow runtimes cannot collide with `PrivateTmp`, and a failed snapshot stops before model launch with a bounded sanitized diagnostic and fail-closed partial finalize.
