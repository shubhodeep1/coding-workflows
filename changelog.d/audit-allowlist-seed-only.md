<!-- changelog: fixed -->
- **A malformed `seed_only` flag in the audit-gate contract now stops the sync manifest build instead of being read as `false`.** This extends the seed-only allowlist fix described in `changelog.d/8334-audit-gate-seed-only-allowlist.md` (Refs shubhodeep1/multi-user-ai-agent#322).

`scripts/build_sync_manifest.py` (`allowed-paths`, `managed-paths` and `managed-digests`) now fails when a `managed_files` entry has a `seed_only` value that is not a boolean, as `scripts/apply_audit_gate_assets.py` already did. Before, a value such as the string `"true"` would have made the npm audit allowlist a managed file again, so the sync would overwrite it. `scripts/workflow_failure_heal.py` no longer maps an upstream source to a seed-only target, so a security finding on a consumer's allowlist is not filed as a coding-workflows fix. When the existing seed-only target is a symlink or a directory, the "Apply canonical audit-gate assets" step now prints a warning and still leaves it untouched.

What this means for consumer repos: nothing changes for a correct contract. The allowlist stays consumer-owned once it exists.
