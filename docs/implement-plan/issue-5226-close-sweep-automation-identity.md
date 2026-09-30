# Implement-Plan Log — Require an automation-owned PR before closing an issue on a non-default branch merge

- Plan: docs/completed/issue-5226-close-sweep-automation-identity-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5226
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5226-close-sweep-automation-identity   Final PR: #5261 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges; the change goes live with #4813's project)
- Waiting on: the completion PR, then final PR #5261
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_014nbczUm84YMzWVtDy43KCv (project checker, reused)   safety net and hand-back: see the completion stage report
- Last updated: 2026-09-30
- Last note: completion stage (session_01CBpgWQgND4jw2P7A6bfm1i): Q1: A answered on #5226 (standing decision Q17), so runtime validation is skipped (AD-7); plan moved to docs/completed/. Next: final-merge 1/1 marks #5261 ready, and after it merges into the #4813 branch the issue is closed and labelled ai:merged.

## Phases
1. [x] Phase 1 — require an automation-owned PR for non-default target merges (sweep + issue_pr_status.yml + gh_helpers.sh helper + tests + README + changelog)   — PR #5272 merged 2026-09-29 (merge commit 96864ec); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-validation; security pass skipped). Every acceptance criterion maps to merged PR #5272 on the project branch (96864ec); no correctness defects (stage session session_016VcL9wKFp3ajoDpHQv9Stc).

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation
- Skipped (covered by #4813's project validation). `validate.yml` step `Authorize explicit validation target` binds `target_ref` only to an open PR into the default branch, and final PR #5261 targets the #4813 project branch. Nothing was dispatched. Q1: A on #5226 (standing decision Q17, operator-confirmed Q18: A); see AD-7. Long-term fix: #4734.

## Completion
- Completion PR (branch claude/implement-plan-issue-5226-close-sweep-automation-identity-complete) open — doc moved to docs/completed/issue-5226-close-sweep-automation-identity-plan.md
- Final PR #5261 draft (into claude/implement-plan-issue-4813-close-sweep-target-branch-merges)

## Activation
- n/a: the base is #4813's project branch, so the change goes live with that project's own final merge and activation (issue mode). Steps 12–13 do not run.

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which paths get the identity rule? — Picked: A — both the sweep and `issue_pr_status.yml`, through one helper in `scripts/gh_helpers.sh`. Alternatives: B — the sweep only; C — change `_pr_json_is_issue_implementation_pr` for every caller. Why: B leaves the workflow closing labelled managed children on the same PR text; C changes callers that close nothing. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What proves a PR is the issue's assigned fix on a non-default base? — Picked: A — an automation head branch for that issue in this repository. Alternatives: B — trust the closing keyword from collaborator-authored issues; C — a new issue-to-PR marker comment. Why: A needs no API call and no new state; a same-repo branch requires write access. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which head names count as automation branches? — Picked: A — `ai/issue-<n>`, `ai/issue-<n>-…` / `ai/issue-<n>/…`, `fix/<n>-followup-<digits>`. Alternatives: B — exact `ai/issue-<n>` only. Why: the workflow already maps the suffixed shape to the issue, and the orchestrator judge's follow-up PR closes its issue on the integration branch. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does a default-branch merge also need the identity? — Picked: A — no. Alternatives: B — require it everywhere. Why: GitHub closes the issue on that merge by itself. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should the editable `Integration branch:` line itself be authorized? — Picked: A — no separate check. Alternatives: B — trust the line only on automation-authored issues. Why: with AD-2 a changed line can only point at a branch where the issue's own automation PR merged. Applied in: no code change. Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-09-29] Round 1 left no valid finding, but no dedicated verdict bot is configured (`CLAUDE_FIXER_VERDICT_BOT_LOGIN`), so no verdict can be posted. How should the PR move on? — Picked: A — push a real change (the issue base → project branch sync merged into the PR head, plus this log update) so the workflow reviews a new head; a clean review then auto-merges. Alternatives: B — stop BLOCKED and ask for the verdict or a hand merge. Why: both changes are needed anyway (step 2 sync, and the log has to record the PR, round, and check-in ids), and it matches issue-4707 and issue-4622. Nothing is skipped, and the new head gets a full reviewer panel. Applied in: PR #5272 (merge + log commit). Status: pending review
- AD-7 [validation 1/3, 2026-09-30] How should this project handle runtime validation, given `validate.yml` cannot authorize a final PR into another project's branch? — Picked: A — skip validation and record `Validation: skipped (covered by #4813's project validation)`; #4813's chain validates a project branch that contains this fix before anything reaches `main`. Alternatives: B — hold until the base merges into `main`, then retarget #5261 and validate (deadlocks if #4813's security-pass checker waits for this issue to close); C — widen `validate.yml`'s target binding (a trust-boundary change that belongs to #4734). Why: answered `Q1: A` on #5226 by the master session under standing decision Q17 (operator-confirmed Q18: A), the same answer as siblings #4955 and #4956. Applied in: no code change. Status: pending review

## Lessons

## Notes
- 2026-09-29 phase 1/1 review round 1 (PR #5272, head 344d43e, run 36637535318), session session_01Spr6xLRE1wavAcuVafZxZe: the ledger has 2 entries, both the same NIT task gap from `minimax_minimax-m3` (confidence 5). The other 5 reviewers found nothing. Rejected: the entry's "evidence of absence" lists every deliverable as present (`scripts/gh_helpers.sh` helper, the sweep split, the workflow gate and stub, `ci.yml` registration, README, changelog, tests), and the diff confirms it. Checks run on 344d43e: `bash -n` on both scripts, YAML parse of `issue_pr_status.yml` / `ci.yml`, 54 passed (helper, target-branch gate, payload fallback, linked-PR guard, integration-branch helper, workflow size), 27 passed (`tests/test_orchestrate_poll_process.py -k "close_merged or sweep"`). No verdict bot, so no verdict was posted (AD-6); the round's push is the issue base → project branch sync (c1ad075: #5244 `docs/operations/master-session.md`) merged as `[claude-merge-resolve]`, plus this log update.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5226#issuecomment-5899545241
- Security pass: skip (`security_pass_skip.py`: ai:security, created and labelled by the issue automation).
- Base branch is another project's branch (#4813, final PR #4826, open draft); per Issue Mode this project ends after its final merge (`Activation: n/a`) and the final-merge stage closes #5226 with `ai:merged`.
- 2026-09-30 validation 1/3 (session session_016VcL9wKFp3ajoDpHQv9Stc) stopped BLOCKED on #5226 (Q1, comment 5901773456) before dispatching anything; the committed log lagged at `phase 1/1 — review round`. The owner answered `Q1: A` and `/reclarify`, and the dispatcher resumed in session session_01CBpgWQgND4jw2P7A6bfm1i, which read the blocker comment's resume state (Status: BLOCKED) over the lagging log's IN_PROGRESS.
