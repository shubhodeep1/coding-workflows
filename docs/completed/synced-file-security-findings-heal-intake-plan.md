# Report synced-file security findings to coding-workflows through heal intake

## Summary

Consumer security audits find issues in files coding-workflows owns: the synced wrapper workflows, `.claude/` assets, `CLAUDE.md`, the audit-gate files and retired files. Today those findings are only listed in the consumer's tracker comment, so nobody upstream acts on them. This plan adds a heal-intake report kind, `synced_security_finding`, that turns each high or critical finding on an unmodified synced file into one coding-workflows heal issue. The issue is shared across consumers and fixed by the normal heal flow.

## Context

- `scripts/security_audit.sh` (lines ~537-549 and ~2168-2330) already separates these findings. A finding on a path whose consumer copy is byte-identical to the release's managed digest (`upstream_managed_status() == "unmodified"`) is not filed in the consumer. The consumer could never fix it, because the next sync overwrites the fix. It goes into `upstream_managed_findings` and is logged as `SECURITY_AUDIT_UPSTREAM_MANAGED outcome=suppressed ...`. It is summarised in the payload (`upstream_managed`, ~line 3011) and in the tracker comment ("fix them in coding-workflows", ~line 3218). Nothing reaches coding-workflows itself.
- On 2026-10-17, 22 stranded consumer security-audit issues had to be closed by hand. Nine guard-file findings were filed upstream by hand as #7555.
- Heal intake (`.github/workflows/workflow-failure-heal-intake.yml`, `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`) already takes `repository_dispatch` reports from consumers. It validates them (`SOURCE_KINDS`, `validate-payload`) and binds the reporter's identity (`REPORT_IDENTITY_WORKFLOW_BY_KIND`, OIDC). It de-duplicates by fingerprint, records repeats as occurrence comments (`compose_occurrence_comment`), applies the open and per-day budgets (`WORKFLOW_HEAL_MAX_OPEN_ISSUES` = 10, `WORKFLOW_HEAL_MAX_ISSUES_PER_DAY` = 20) and opens `ai:workflow-heal` issues.
- `engine_fallback` is the existing precedent for a kind that carries no run log. It has its own fingerprint branch in the intake (~line 938) and skips log fetching and the diagnosis model (~lines 1630, 1659, 1886).
- coding-workflows is a **public** repository.

### Owner decisions (2026-10-18)

| ID | Decision |
| --- | --- |
| Q231 | A new heal-intake report kind for synced-file security findings. |
| Q245 | The upstream issue carries only the file, line, OWASP/STRIDE category, severity, confidence, finding id and consumer repository. The exploit scenario and the recommendation stay in the consumer's tracker comment and never reach the public repository. |
| Q246 | One issue per *path + category + line area*, shared by all consumers. Each further consumer or run is an occurrence comment on that issue. |
| Q247 | Only `high` and `critical` severities are reported. |
| Q248 | The normal heal flow: the issue is planned and implemented by the pipeline. Edits to protected paths still go through the existing scope guard and its approvals. |
| Q258 | This plan is handed to the orchestrator with the Claude engine label. |

## Goals

1. A consumer security audit run that suppresses at least one `high` or `critical` finding on an unmodified synced file sends exactly one `workflow-failure-heal` dispatch to coding-workflows. The dispatch's `source_kind` is `synced_security_finding` and it lists those findings with the allowed fields only.
2. The intake accepts the report only when all of these hold:
   - the reporter's OIDC identity is the coding-workflows `security-audit.yml` reusable workflow;
   - every finding's `file` is a path the release's managed-digests list marks as upstream-managed;
   - every field passes validation.
   Anything else is rejected with a logged reason and opens nothing.
3. For each finding, the intake opens one `ai:workflow-heal` issue, or adds an occurrence comment to the open issue with the same fingerprint. The fingerprint is `sha256(path | category | line // LINE_BUCKET)`, with `LINE_BUCKET` = 25 by default. The open and per-day budgets apply.
4. The issue body and comments contain no exploit scenario, recommendation or other free text from the consumer audit.
5. Both sides can be turned off without a code change: `SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED` on the consumer side and `HEAL_SYNCED_SECURITY_FINDING_ENABLED` on the intake side. Both default to `true`.

## Non-goals

- Changing which findings the consumer files locally, or the `upstream_managed_status` rules.
- Reporting `medium`, `low` or `info` findings.
- Publishing the exploit text anywhere in coding-workflows, including issue comments, logs and job summaries.
- Closing the consumer tracker entries automatically.
- Findings in coding-workflows' own security audit. Those are filed in-repo as today.

## Constraints

- **§6:** all existing identifiers stay unchanged. New names: the `synced_security_finding` kind, the env vars `SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED`, `SECURITY_AUDIT_UPSTREAM_MANAGED_EXPORT`, `HEAL_SYNCED_SECURITY_FINDING_ENABLED` and `HEAL_SYNCED_SECURITY_LINE_BUCKET`, and the log prefix `SECURITY_AUDIT_UPSTREAM_REPORT`. Check each for collisions before adding it.
- **§4:** every new env var has a default.
- **§15:** one dispatch per audit run, no matter how many findings, and no new read calls on the consumer side. The intake reuses its existing issue-list read for de-duplication and budgets, and makes no per-finding API reads.
- **Security:**
  - Treat every payload field as untrusted.
  - `file` must match a managed path exactly: no `..`, no absolute paths, no control characters.
  - `line` is an integer from 1 to 10^6.
  - `severity` and `category` come from the audit's own enums.
  - `finding_id` matches `^[A-Za-z0-9._:-]{1,80}$`.
  - The intake never passes these fields to a model prompt unescaped. The issue body is built from validated fields only.
- **§27:** check `wc -c` on any workflow file that grows.
- **§20/§30:** each phase ships one changelog fragment. Agents documentation goes in `agents.d/`.

## Approach

**Sender (consumer side, `security-audit.yml`).** A new step runs after the audit, from the trusted support checkout, in the same pattern as `Report refused AI engine fallbacks`: `if: always()`, `continue-on-error: true`, `GH_TOKEN: secrets.GH_PAT`, and the OIDC token for report identity. It runs `scripts/security_audit_upstream_report.sh` [new]. The script:
1. reads `${RUNNER_TEMP}/security-audit-upstream-managed.json`, a copy of the `upstream_managed` list that `security_audit.sh` now also writes there (its filter summary lives in a `mktemp` runtime directory that a later step cannot find);
2. keeps the `high` and `critical` entries;
3. drops `exploit_scenario` and `recommendation`;
4. caps the list at 50 entries;
5. builds a `workflow_failure_heal.v1` payload with `source_kind: synced_security_finding`, `source_repo`, the run URL, the reporter run id, the consumer's pinned release SHA and the findings;
6. sends it with one `repository_dispatch`, event type `workflow-failure-heal`.

Each outcome logs one `SECURITY_AUDIT_UPSTREAM_REPORT outcome=... findings=N` line. The script never fails the job.

**Intake (coding-workflows side).**
- `workflow_failure_heal.py`:
  - adds the kind to `SOURCE_KINDS`;
  - maps it to `("security-audit.yml",)` in `REPORT_IDENTITY_WORKFLOW_BY_KIND`;
  - validates the payload (`validate-payload`);
  - checks each `file` against the managed-digests list for the payload's release SHA. That is the same list `build_sync_manifest.py managed-digests` produces and `security_audit.sh` reads. Locally, the intake builds it from the release commit it already fetches.
  - adds `synced-security-fingerprint` and composes the issue body and occurrence comment.
- `workflow_failure_heal_intake.sh`: for this kind, skips run-log fetching and the diagnosis model like `engine_fallback` does. It then loops over the validated findings and runs the existing dedupe, budget and open path once per finding fingerprint.
- The issue body:
  - names the path, line, category, severity, confidence, the reporting consumers and the run link;
  - states that the details stay in the consumer's tracker comment, and that the fix belongs to the synced template under `workflow-templates/` (or the matching source path).

**Alternative rejected:** a separate intake workflow. It would duplicate the identity, dedupe and budget code that heal intake already has.

## Phases & Merge Strategy

The phases are independently mergeable and production-safe in either order:
- If Phase 1 merges first, the intake accepts a kind nobody sends yet.
- If Phase 2 merges first, the existing intake rejects the unknown kind with `source_kind must be one of`, logs it, and opens nothing. The consumer step is `continue-on-error`.

1. **Phase 1: intake accepts and files `synced_security_finding` reports.**
   - Files: `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`, `.github/workflows/workflow-failure-heal-intake.yml` (env default only), tests, a changelog fragment and an agents fragment.
   - Done when the new unit tests and the intake tests pass. A fixture payload with two findings in the same line bucket from two consumers opens one issue with one occurrence comment, and a payload with a non-managed path is rejected.
   - Rollback: revert the PR, or set `HEAL_SYNCED_SECURITY_FINDING_ENABLED=false`.
2. **Phase 2: consumer audits send the report.**
   - Files: `scripts/security_audit.sh` (export only), `scripts/security_audit_upstream_report.sh` [new], `.github/workflows/security-audit.yml` (one step), tests, a changelog fragment and an agents fragment.
   - Done when the script tests show:
     - one dispatch for several qualifying findings;
     - no dispatch when none qualify, or when the flag is `false`;
     - the free-text fields absent from the payload;
     - exit code 0 on dispatch errors.
   - Rollback: revert the PR, or set `SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED=false` on a consumer.

## Implementation Steps

### Phase 1
1. `scripts/workflow_failure_heal.py`:
   - add `synced_security_finding` to `SOURCE_KINDS` (~line 84) and to `REPORT_IDENTITY_WORKFLOW_BY_KIND` (~line 266);
   - add `SYNCED_SECURITY_SEVERITIES = ("high", "critical")`, a validator for the findings list (fields, enums, bounds; max 50 entries; reject unknown keys) and a `synced-security-fingerprint` subcommand: `sha256(path|category|line//bucket)`, bucket from `HEAL_SYNCED_SECURITY_LINE_BUCKET`, default 25, minimum 1;
   - add a managed-path check that takes the managed-digests JSON;
   - compose the issue body and the occurrence comment from validated fields only.
2. `scripts/workflow_failure_heal_intake.sh`:
   - add a gate that skips with `skip reason=synced_security_finding_disabled` when the flag is `false`;
   - skip log fetching and diagnosis for the new kind (mirror `engine_fallback`);
   - build the managed-digests list for the payload's release SHA from the release checkout the intake already has. Reject the report with `reason=release_unavailable` if that is not possible;
   - for each finding, compute the fingerprint and run the existing dedupe, occurrence, budget and open path;
   - log `received kind=synced_security_finding findings=N opened=X occurrences=Y skipped=Z`.
3. `.github/workflows/workflow-failure-heal-intake.yml`: add `HEAL_SYNCED_SECURITY_FINDING_ENABLED: ${{ vars.HEAL_SYNCED_SECURITY_FINDING_ENABLED || 'true' }}` and `HEAL_SYNCED_SECURITY_LINE_BUCKET` (default `25`) to the intake job env.
4. Tests, the changelog fragment, the agents fragment, and README env-var table rows.

### Phase 2
0. `scripts/security_audit.sh`: next to the existing `filter-summary.json` write (~line 2339), also write the `upstream_managed` list to `${SECURITY_AUDIT_UPSTREAM_MANAGED_EXPORT:-${RUNNER_TEMP:-${SECURITY_AUDIT_RUNTIME_DIR}}/security-audit-upstream-managed.json}`, atomically (temp file plus rename). Write it only when the list is non-empty. Remove a stale copy at the start of each run.
1. `scripts/security_audit_upstream_report.sh` [new]:
   - inputs: the exported `security-audit-upstream-managed.json` path, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `REPORT_WRAPPER_SHA` and the OIDC token request env;
   - filter to high/critical, strip the free-text fields, cap at 50, build the payload and send one dispatch with the existing retry helper (`gh_retry`);
   - log one `SECURITY_AUDIT_UPSTREAM_REPORT` line and always exit 0.
2. `.github/workflows/security-audit.yml`: a new `Report synced-file security findings upstream` step after the audit step, in the same shape as `Report refused AI engine fallbacks`, with `SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED: ${{ vars.SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED || 'true' }}`. It runs only the support-checkout copy of the script, never a PR-head script.
3. Register the new script wherever this repo lists required support scripts for the security-audit job, so a consumer checkout stages it. Register the log prefix in the agents file's stable log prefix registry.
4. Tests, the changelog fragment, the agents fragment and the README.

## Files & Modules

- `scripts/workflow_failure_heal.py`
- `scripts/workflow_failure_heal_intake.sh`
- `.github/workflows/workflow-failure-heal-intake.yml`
- `scripts/security_audit.sh` (export of the upstream-managed list)
- `scripts/security_audit_upstream_report.sh` [new]
- `.github/workflows/security-audit.yml`
- `tests/test_workflow_failure_heal_synced_security.py` [new]
- `tests/test_security_audit_upstream_report.py` [new]
- the existing intake and security-audit wiring tests that pin step lists or env, if any
- `README.md` (env-var table)
- `changelog.d/<phase PR>-*.md`, `agents.d/<phase PR>-*.md`

## Data Model / Index Changes

None. No MongoDB collections are involved. State lives in GitHub issues, as with every heal kind.

## Tests

- **Unit (Phase 1):**
  - payload validation, accept and reject for each field;
  - managed-path check (exact match, traversal, absolute path, unknown path);
  - fingerprint stability and bucket edges (lines 24/25/26 with bucket 25);
  - the issue body and occurrence comment contain no free text;
  - the flag off.
- **Intake (Phase 1):** a fixture-driven run of the intake shell with stubbed `gh`, in the style of the existing heal intake tests:
  - two consumers, same bucket: one issue plus one occurrence;
  - budget exhausted: skip with the existing reason;
  - wrong reporter identity: rejected.
- **Sender (Phase 2):** a stubbed-`gh` run of the script:
  - one dispatch for three qualifying findings, and the payload contains no `exploit_scenario` or `recommendation` keys;
  - medium-only findings: no dispatch;
  - flag `false`: no dispatch;
  - dispatch error: exit 0 with an `outcome=failed` log.
- **End to end:** after both phases are on stable, the next consumer security audit that suppresses a high or critical synced-file finding produces a heal issue. Confirm with the `SECURITY_AUDIT_UPSTREAM_REPORT` and intake log lines.

## Risks & Mitigations

- **Public disclosure of a real vulnerability in a synced file.** Mitigated by Q245: only location, category and severity are published; the exploit text stays in the consumer. ACCEPTED: the location alone is visible, as the owner decided.
- **Forged reports from a consumer.** Mitigated by the OIDC workflow binding, the managed-path check against the release's own digests list, and strict field validation.
- **Issue floods.** Mitigated by line buckets, cross-consumer de-duplication, the existing open and per-day budgets, and the 50-finding cap.
- **Over-merging.** Two findings in the same bucket and category share one issue. ACCEPTED: the occurrence comments list each line, and the fix pass reads the whole file.
- **The release SHA's managed-digests list is unavailable at intake time.** The report is rejected with a logged reason, and the consumer's tracker comment still lists the findings.

## Rollout

Both flags default to `true`, consistent with the repo's default-on policy. Phase 1 can land first and is inert until consumers send. Phase 2 reaches consumers on the next `@stable` sync. Rollback is per phase, by revert or by its flag.

## References

- Issue #7555 (the nine guard-file findings filed by hand)
- `scripts/security_audit.sh` upstream-managed filter (Q224 D)
- `scripts/ai_engine_fallback_report.sh` and the `engine_fallback` intake path (the sender and intake precedent)
- CLAUDE.md §4, §6, §15, §18, §20, §27, §30
