# Deploy-Activation Log — Heal issue creation gate (shared operator step, batch)

- Reference: https://github.com/shubhodeep1/coding-workflows/issues/6211, shared step "Enable heal issue creation" (set HEAL_INTAKE_SCOPE_GUARD_ENABLED to true; leave WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED at its true default)   (+ entries pr-6682, pr-6874, pr-7065, pr-7194, pr-7195, all PRs in shubhodeep1/coding-workflows)
- Deploy target: shubhodeep1/coding-workflows
- How it runs: repository_dispatch + workflow_run + schedule on `.github/workflows/workflow-failure-heal-intake.yml` (default branch); the gate is read by `scripts/workflow_failure_heal_intake.sh`
- Status: LIVE
- Last updated: 2026-10-26
- Last note: Gate already on by code default and confirmed in live run 40210066224; all five batch checkboxes ticked on #6211. LIVE.
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/6211 — keys pr-6682, pr-6874, pr-7065, pr-7194, pr-7195

## Runbook
1. [x] Confirm HEAL_INTAKE_SCOPE_GUARD_ENABLED resolves to true and WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED stays at its true default   — covers pr-6682 «Enable heal issue creation», pr-6874 «Enable guarded heal issue creation», pr-7065 «Enable heal-issue creation», pr-7194 «Enable the required intake scope guard», pr-7195 «Automatic creation of new heal issues is off»   — done 2026-10-26: already configured, verified (see Notes); no variable change needed
2. [x] Tick the shared step in each of the 5 entry comments on #6211   — done 2026-10-26: each saved body re-read and byte-compared to the expected one-line change
3. [x] Verify LIVE   — done 2026-10-26: repository_dispatch intake run 40210066224 (2026-10-25 16:06 UTC, success) resolves HEAL_INTAKE_SCOPE_GUARD_ENABLED: true in every step

## Notes
- The entries' evidence lines predate the default flip. On main (e1a7621) the gate defaults ON:
  - `.github/workflows/workflow-failure-heal-intake.yml:120` and `:396`: `HEAL_INTAKE_SCOPE_GUARD_ENABLED: ${{ vars.HEAL_INTAKE_SCOPE_GUARD_ENABLED || 'true' }}`
  - `scripts/workflow_failure_heal_intake.sh:219`: `SCOPE_GUARD_ENABLED="${HEAL_INTAKE_SCOPE_GUARD_ENABLED:-true}"`; `:611-614` treats unset, empty or "true" (any case) as on.
- So the only way the gate is off is a repo variable set to a non-true value. Direct reads of repo variables are refused by the web-session proxy (HTTP 403), so the live run log was used instead: run 40210066224 shows `HEAL_INTAKE_SCOPE_GUARD_ENABLED: true` in all 15 step env blocks. That means the variable is either unset or "true"; both are on.
- WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED is not passed from any repo variable by the workflow, so the script default `true` (`scripts/workflow_failure_heal_intake.sh:216`) always applies. Nothing to leave alone beyond not adding it.
- Only this shared step is in scope; the entries' other steps (pr-6682 consumer release, pr-7194 heal-scope recovery, pr-7195 reauthorize refused heal) are handled in their own sessions and were left unticked.
- Ticks on #6211 (2026-10-26), shared step only:
  - pr-6682 (comment 6113664576): Enable heal issue creation
  - pr-6874 (comment 6111267295): Enable guarded heal issue creation
  - pr-7065 (comment 6134192193): Enable heal-issue creation
  - pr-7194 (comment 6143951593): Enable the required intake scope guard
  - pr-7195 (comment 6115886224): Automatic creation of new heal issues is off
