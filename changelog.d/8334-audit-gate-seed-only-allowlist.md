<!-- changelog: fixed -->
- **The `@stable` sync no longer wipes a consumer's npm audit allowlist or breaks ESM repos.** `security/dependency-audit-allowlist.json` is now seed-only, and the vendored `check-npm-audit.js` runs under both CommonJS and `"type": "module"`.

`.github/workflows/update_workflows.yml` used to overwrite both audit-gate files on every sync. It reset the consumer's allowlist to `[]` and put back a CommonJS script that crashes in ESM packages, so the consumer's npm Audit Gate failed after each release. In `shubhodeep1/multi-user-ai-agent` this happened about 9 times, each time followed by an AI fix that the next sync undid, until shubhodeep1/multi-user-ai-agent#316 blocked. The allowlist is now created only when missing and is never overwritten. The script loads core modules with `process.getBuiltinModule()` and keeps syncing as before.

| The numbers that matter | Value |
| --- | --- |
| Sync-and-restore cycles in multi-user-ai-agent | about 9 (2026-09 to 2026-10) |
| Minimum Node.js for `check-npm-audit.js` in an ESM package | 20.16 or 22.3 (CommonJS packages keep older Node.js) |
| New contract field | `managed_files[].seed_only` (boolean, default `false`) |

What this means for consumer repos: advisory entries you add to `security/dependency-audit-allowlist.json` survive every sync, and the audit gate runs in ESM packages. A repo whose allowlist was already reset still has to restore its entries once.

### For contributors

`scripts/build_sync_manifest.py` keeps seed-only targets in `allowed-paths` but drops them from `managed-paths` and `managed-digests`, so the security audit, heal intake and validation-owner classifiers treat the allowlist as consumer-owned.
