<!-- changelog: fixed -->
- **The workflow sync no longer empties a consumer's npm audit allowlist.** `security/dependency-audit-allowlist.json` is now written only when a consumer repo does not have one yet, and is never overwritten after that.

Until now `update_workflows.yml` treated the allowlist like the audit script: whenever the consumer's copy differed from upstream's empty `[]`, the "Apply canonical audit-gate assets" step replaced it. Every justified entry a consumer added was wiped on the next `@stable` sync, and the consumer's `npm Audit Gate` then failed on every pull request. In `shubhodeep1/multi-user-ai-agent` this happened five times in a row: each AI fix restored 18 entries and the next sync removed them (Refs shubhodeep1/multi-user-ai-agent#322). The audit-gate contract (`workflow-templates/audit-gate/contract.json`) now marks the allowlist `"seed_only": true`, and `scripts/apply_audit_gate_assets.py` skips a seed-only target that already exists. `scripts/security/check-npm-audit.js` is still refreshed on every sync.

| The numbers that matter | Value |
| --- | --- |
| Allowlist wipes in multi-user-ai-agent before this fix | 5 (multi-user-ai-agent sync PRs 284, 289, 293, 297, 302) |
| Findings the live audit reports there today | 35 |

What this means for consumer repos: an allowlist you restore or edit now survives the next sync. A repo whose allowlist was already emptied still has to restore it once, from a live `npm run audit:ci -- --write` run.

### For contributors

`seed_only` is an optional boolean on a `managed_files` entry; the applier rejects any other type. Seed-only targets stay in `build_sync_manifest.py allowed-paths` (the updater may still create them) but are left out of `managed-paths` and `managed-digests`, because the consumer owns them. For the same reason `workflow_failure_heal.py` maps no upstream source to a seed-only target.
