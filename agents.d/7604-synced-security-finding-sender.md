<!-- agents: section="Workflow architecture" -->
Synced-file security findings sender (issue #7623, project #7604 Phase 2 of
`docs/plans/synced-file-security-findings-heal-intake-plan.md`). After its
filter, `scripts/security_audit.sh` writes the summary's `upstream_managed`
list to `SECURITY_AUDIT_UPSTREAM_MANAGED_EXPORT` (default
`${RUNNER_TEMP}/security-audit-upstream-managed.json`; only when non-empty,
a stale copy removed at the start of each audit, a write failure logs
`::warning::SECURITY_AUDIT_UPSTREAM_MANAGED outcome=export_failed` and
changes nothing else). In consumer repositories only, the `security-audit.yml`
step "Report synced-file security findings upstream" (`continue-on-error`,
skipped on `CLAUDE_CAPACITY_WAIT=true`) runs
`scripts/security_audit_upstream_report.sh` from the trusted support checkout
(`${GITHUB_WORKSPACE}`, symlinks refused). The script skips, always exiting 0,
when `SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED` is off (default on: unset, empty
or `true`; `false` off; other values off with a warning),
`WORKFLOW_HEAL_ENABLED=false`, the repository is the upstream, the export is
missing, a symlink or over 1 MiB, `GH_PAT` is empty, the job's `workflow_ref`
does not end in `@<support_sha>` (the intake would reject it as
`release_mismatch`), the helper is missing, or no OIDC token is available.
It keeps `high` / `critical` entries with a safe relative path, line 1 to
1,000,000, confidence 1 to 10, an `[A-Za-z0-9._:-]{1,80}` finding id and a
category the support module's `normalize_synced_security_category` accepts;
copies only the six fields the intake allows (never `exploit_scenario` or
`recommendation`); de-duplicates within the run by (file, line, category)
and by finding id; caps at 50; pre-validates with `validate-payload` when the
module knows the kind; and sends one `repository_dispatch` per run with
`report_identity` attached. Repeats across runs and consumers become
occurrence comments through the Phase 1 intake fingerprint; the sender keeps
no state and makes no reads (one dispatch call). Log lines carry fixed
tokens and counts only. Tests: `tests/test_security_audit_upstream_report.py`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `SECURITY_AUDIT_UPSTREAM_REPORT` (`scripts/security_audit_upstream_report.sh` and the `security-audit.yml` step "Report synced-file security findings upstream": `outcome=dispatched|none findings= dropped= deduped= capped= below_high=`, `outcome=failed reason=dispatch_denied findings= upstream= detail=`, `prevalidate=unavailable findings=`, `skip reason=disabled|invalid_switch|workflow_heal_disabled|capacity_wait|missing_context|self_repo|no_summary|export_symlink|export_oversized|export_unreadable|token_missing|wrapper_not_pinned|helper_missing|tmpdir_unavailable|payload_invalid|identity_unavailable|dispatch_envelope_failed|support_missing`)

LOG_PREFIX.name=SECURITY_AUDIT_UPSTREAM_REPORT
