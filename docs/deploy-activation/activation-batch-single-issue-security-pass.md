# Deploy-Activation Log — opt coding-workflows in to the single-issue security pass

- Reference: shared step "Opt in to the single-issue security pass" from operator-steps issue #6211 (https://github.com/shubhodeep1/coding-workflows/issues/6211), covering entries pr-6562, pr-6462, pr-6610, pr-7213 (PRs #6562, #6462, #6610, #7213, all merged)
- Deploy target: shubhodeep1/coding-workflows (repo variable only; no consumers, per operator decision Q1: A on 2026-10-25)
- How it runs: pull_request (review_autofix.yml gate, scripts/review_single_issue_security_pass.sh:331) + cron '*/30 * * * *' from default branch (.github/workflows/review_autofix_sweep.yml:80, step gated at :601 on vars.SINGLE_ISSUE_SECURITY_PASS_ENABLED == 'true')
- Status: IN_PROGRESS
- Last updated: 2026-10-25
- Last note: Step 1 done; Step 2 (read current variable value) emitted.

## Runbook
1. [x] Prereqs: Homebrew, gh, `gh auth login` (no clone needed; every command uses `-R`)   — done 2026-10-25: operator confirmed `done`
2. [ ] Read current SINGLE_ISSUE_SECURITY_PASS_ENABLED value in shubhodeep1/coding-workflows (default `false`)
3. [ ] Set repo-var SINGLE_ISSUE_SECURITY_PASS_ENABLED=true in shubhodeep1/coding-workflows
4. [ ] Verify LIVE: variable reads `true` and a scheduled review_autofix_sweep run executes "Re-dispatch stale security holds" (not skipped)

## Notes
- Scope decision (2026-10-25): Q1: A — enable in coding-workflows only, to observe OpenRouter spend before widening; the 13 consumers in .github/ai/consumer_repos.json stay at the `false` default. Consumer-wrapper `pr_number` check therefore not needed.
- Value is lowercase `true`: the workflow conditions compare `== 'true'` exactly (review_autofix_sweep.yml:601, workflow-templates/ai-review.yml:106).
- Out of scope (handled in their own sessions): GH_PAT / OPENROUTER_API_KEY secrets, TG alert destination, @stable release + consumer sync.
- Repo variables are not readable from the web session (agent proxy returns 403 on Actions variable paths), so reads run on the operator's Mac.
- Operator authorized (2026-10-25) ticking this shared step in issue #6211 for entries pr-6562 ("Per-PR audits require explicit opt-in"), pr-6462 ("Opt in to the security pass"), pr-6610 ("Opt in to standalone-PR security passes if wanted"), pr-7213 ("Enable the single-issue security pass") once Step 4 verifies the variable is `true`. Ticks: none yet.
