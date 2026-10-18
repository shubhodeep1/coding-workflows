<!-- agents: section="Workflow architecture" -->
Synced-file security findings in the heal intake (project #7604, Phase 1 of
`docs/plans/synced-file-security-findings-heal-intake-plan.md`). The kind
`synced_security_finding` (`SOURCE_KINDS`; identity workflow
`security-audit.yml` in `REPORT_IDENTITY_WORKFLOW_BY_KIND`) carries
`findings` (1-50 entries of exactly `file`, `line` 1..10^6, `category`,
`severity` `high`/`critical`, `confidence` 1..10, `finding_id`
`^[A-Za-z0-9._:-]{1,80}$`), `wrapper_sha` (the consumer's release SHA),
`reporter_run_url`, an optional digits-only `reporter_run_id` (never
published) and an empty `run_refs`; any other top-level or finding key
rejects the report, because coding-workflows is public (Q245). The category is
mapped onto a fixed OWASP 2021 (`A0x:2021-<name>`) / STRIDE token, and anything
else is rejected. `validate_payload` forces the title, workflow name and
excerpts. In `scripts/workflow_failure_heal_intake.sh`:

- `HEAL_SYNCED_SECURITY_FINDING_ENABLED` (default on) is checked on the raw payload, before validation (`skip reason=synced_security_finding_disabled`).
- A verified identity is required whatever `WORKFLOW_HEAL_REQUIRE_REPORT_AUTH` says (`identity_required`), and the `@<sha>` of its `job_workflow_ref` must equal `wrapper_sha` (`release_mismatch`).
- Reports from this repository are skipped (`skip reason=synced_security_self_repo`).
- After binding, `_synced_security_finding_intake` runs and the script exits, so no run log is read and no diagnosis model runs. It fetches the release commit and checks `compare/<sha>...stable` (`ahead` or `identical`, else `release_not_on_stable`; one call per report). It builds `build_sync_manifest.py managed-digests` from this trusted checkout under `env -i` / `python3 -I -B` (`release_unavailable` on failure), and requires every finding's file to be a key of that list (`unmanaged_path`).
- `synced-security-groups` groups findings by `sha256(path|category|line//bucket)` (`HEAL_SYNCED_SECURITY_LINE_BUCKET`, default 25, minimum 1). Each group runs the unchanged `budget` decision against this repository's heal issues. A duplicate gets `compose-synced-security-occurrence`, an escalation labels the prior issue, and an open renders a heal scope from the mapped `workflow-templates/` source paths and the reporter run (`skip reason=scope_unresolved` when none exists at HEAD). The open then passes the unchanged scope guard (`skip reason=scope_guard_disabled` while it is off) before `gh issue create`. Each opened issue is added to the in-memory list, so later groups see it.
- The run logs `received kind=synced_security_finding findings=N opened=X occurrences=Y skipped=Z`: N counts findings, the other three count fingerprint groups. It writes the same to the step summary and sends one Telegram message (WARNING when a group was skipped for budget, lineage or scope, else DEBUG); a compose or create failure ends the run red.
- `_heal_fetch_issue_list` and `_heal_intake_scope_guard_is_on` are shared helpers, and `_git_fetch_diagnosis_ref` and the scope-guard functions are now defined ahead of this branch.

Nothing sends these reports until Phase 2 (`security_audit_upstream_report.sh`).
