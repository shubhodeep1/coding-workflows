# Implement-Plan Log — Require an automation-owned PR before closing an issue on a non-default branch merge

- Plan: docs/plans/issue-5226-close-sweep-automation-identity-plan.md
- Source issue: shubhodeep1/coding-workflows#5226
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5226-close-sweep-automation-identity   Final PR: #5261 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5272
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_014nbczUm84YMzWVtDy43KCv   safety net trig_01EVBCrAuA6qqbEePG4quTEd   hand-back trig_01HsRgUM9exhaZX1sMowHQDd
- Last updated: 2026-09-29
- Last note: PR #5272 review round 1 (head 344d43e): the only ledger entry, a NIT task gap, was rejected because its own evidence lists every deliverable as present. No verdict bot is configured, so no verdict was posted. This round pushes the issue base → project branch sync plus this log update, so the next round reviews a new head.

## Phases
1. [ ] Phase 1 — require an automation-owned PR for non-default target merges (sweep + issue_pr_status.yml + gh_helpers.sh helper + tests + README + changelog)   — PR #5272 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which paths get the identity rule? — Picked: A — both the sweep and `issue_pr_status.yml`, through one helper in `scripts/gh_helpers.sh`. Alternatives: B — the sweep only; C — change `_pr_json_is_issue_implementation_pr` for every caller. Why: B leaves the workflow closing labelled managed children on the same PR text; C changes callers that close nothing. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What proves a PR is the issue's assigned fix on a non-default base? — Picked: A — an automation head branch for that issue in this repository. Alternatives: B — trust the closing keyword from collaborator-authored issues; C — a new issue-to-PR marker comment. Why: A needs no API call and no new state; a same-repo branch requires write access. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which head names count as automation branches? — Picked: A — `ai/issue-<n>`, `ai/issue-<n>-…` / `ai/issue-<n>/…`, `fix/<n>-followup-<digits>`. Alternatives: B — exact `ai/issue-<n>` only. Why: the workflow already maps the suffixed shape to the issue, and the orchestrator judge's follow-up PR closes its issue on the integration branch. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does a default-branch merge also need the identity? — Picked: A — no. Alternatives: B — require it everywhere. Why: GitHub closes the issue on that merge by itself. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should the editable `Integration branch:` line itself be authorized? — Picked: A — no separate check. Alternatives: B — trust the line only on automation-authored issues. Why: with AD-2 a changed line can only point at a branch where the issue's own automation PR merged. Applied in: no code change. Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-09-29] Round 1 left no valid finding, but no dedicated verdict bot is configured (`CLAUDE_FIXER_VERDICT_BOT_LOGIN`), so no verdict can be posted. How should the PR move on? — Picked: A — push a real change (the issue base → project branch sync merged into the PR head, plus this log update) so the workflow reviews a new head; a clean review then auto-merges. Alternatives: B — stop BLOCKED and ask for the verdict or a hand merge. Why: both changes are needed anyway (step 2 sync, and the log has to record the PR, round, and check-in ids), and it matches issue-4707 and issue-4622. Nothing is skipped, and the new head gets a full reviewer panel. Applied in: PR #5272 (merge + log commit). Status: pending review

## Lessons

## Notes
- 2026-09-29 phase 1/1 review round 1 (PR #5272, head 344d43e, run 36637535318), session session_01Spr6xLRE1wavAcuVafZxZe: the ledger has 2 entries, both the same NIT task gap from `minimax_minimax-m3` (confidence 5). The other 5 reviewers found nothing. Rejected: the entry's "evidence of absence" lists every deliverable as present (`scripts/gh_helpers.sh` helper, the sweep split, the workflow gate and stub, `ci.yml` registration, README, changelog, tests), and the diff confirms it. Checks run on 344d43e: `bash -n` on both scripts, YAML parse of `issue_pr_status.yml` / `ci.yml`, 54 passed (helper, target-branch gate, payload fallback, linked-PR guard, integration-branch helper, workflow size), 27 passed (`tests/test_orchestrate_poll_process.py -k "close_merged or sweep"`). No verdict bot, so no verdict was posted (AD-6); the round's push is the issue base → project branch sync (c1ad075: #5244 `docs/operations/master-session.md`) merged as `[claude-merge-resolve]`, plus this log update.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5226#issuecomment-5899545241
- Security pass: skip (`security_pass_skip.py`: ai:security, created and labelled by the issue automation).
- Base branch is another project's branch (#4813, final PR #4826, open draft); per Issue Mode this project ends after its final merge (`Activation: n/a`) and the final-merge stage closes #5226 with `ai:merged`.
