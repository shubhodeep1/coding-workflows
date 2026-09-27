# Implement-Plan Log — Dispatch the review sweep from the default branch only

- Plan: docs/plans/issue-4618-sweep-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#4618 (https://github.com/shubhodeep1/coding-workflows/issues/4618)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4618-sweep-dispatch-default-branch   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: project branch opened; implementing phase 1

## Phases
1. [ ] Phase 1 — default-branch sweep dispatch with PR-keyed dedupe
   - [ ] `internal-review.yml` names `workflow_dispatch` runs `Internal: AI Review & Autofix [pr:<N>]`
   - [ ] `review_autofix_sweep.yml` dispatches without `--ref`, validates `pr_number`, keys dispatch runs by `pr:<N>`
   - [ ] `_has_active_autofix_run` sees active PR-named dispatch runs
   - [ ] tests: `test_conflict_dispatch_active_run_visibility.py`, `test_review_autofix_sweep_stale_queued.py`
   - [ ] `agents.md` and `changelog.d/4618-sweep-dispatch-default-branch.md`

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which dispatch sites does the fix cover? — Picked: A — the sweep dispatch the finding names, plus PR-named run detection in the poller's `_has_active_autofix_run` so its dedupe keeps seeing sweep runs. Alternatives: B — also move `_dispatch_review_for_conflicts`, `review_merge_train.sh`, and the forward-merge fallback to the default branch; C — the sweep only, leaving the poller blind to sweep runs. Why: A closes the reported path without reintroducing the PR #3895 duplicate loop; B would also need the PR-named run in the consumer `ai-review.yml` template and a sync before its dedupe works. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] How are active runs deduplicated by PR? — Picked: A — a `workflow_dispatch` `run-name` of `Internal: AI Review & Autofix [pr:<N>]`, matched exactly together with `event == workflow_dispatch`. Alternatives: B — read each active run's jobs or inputs (one API call per run, §15); C — drop the active-run guard and rely on the concurrency groups (brings back the PR #3895 pending-run churn). Why: no new API calls in the sweep, and the repo already identifies release runs this way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Pass the head SHA as data as well as the PR number? — Picked: A — pass only the validated PR number; `review_autofix.yml` already resolves and pins the live PR head in its gate and checkout. Alternatives: B — add a `head_sha` input to `internal-review.yml` and thread it through `review_autofix.yml`. Why: the security fix is the ref the workflow file is loaded from; B changes the reusable workflow's interface (§5, §6) without closing more of the finding. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #4618; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
