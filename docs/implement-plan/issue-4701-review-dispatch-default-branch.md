# Implement-Plan Log — Dispatch the remaining review runs from the default branch, and find PR-named runs in the poller's lookups

- Plan: docs/plans/issue-4701-review-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#4701 (https://github.com/shubhodeep1/coding-workflows/issues/4701)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main (was claude/implement-plan-issue-4618-sweep-dispatch-default-branch until #4634 merged, 2026-09-29)
- Project branch: claude/implement-plan-issue-4701-review-dispatch-default-branch   Final PR: #4709 draft (retargeted to main 2026-09-29)
- Status: IN_PROGRESS
- Stage: security-pass cycle 2/5 (waiting on cycle 1 follow-ups)
- Activation: not started
- Waiting on: issues #4926, #4927, #4928
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01QCiBCQixd1i5mfLmmr5NUQ   safety net / hand-back: in the stage report
- Last updated: 2026-09-29
- Last note: security cycle 1 (run 36509277736, success) opened 3 medium follow-ups (#4926, #4927, #4928) that the Claude issue implementer builds on this branch; issue base #4634 merged into main, so the project moved onto main

## Phases
1. [x] Phase 1 — default-branch dispatch at the remaining sites, plus PR-named run lookups   — PR #4725 merged 2026-09-28 (8d36c31); review rounds: 1; interventions: 0
   - [x] `_dispatch_review_for_conflicts`, `_mt_dispatch_review`, and the forward-merge fallback dispatch without `--ref`, with a validated PR number
   - [x] `workflow-templates/ai-review.yml` names dispatched runs `AI Review [pr:<N>]`
   - [x] `_pr_named_review_dispatch_runs` helper; `_has_active_autofix_run` uses it
   - [x] stall-judge `workflow_outcomes` matches PR-named runs from the cached blob
   - [x] retrigger failed-autofix lookup: PR-named fallback (AD-6)
   - [x] empty-commit push guards see PR-named runs (AD-7)
   - [x] merge-train release sees `pr:<N>` keys (AD-9)
   - [x] tests, `agents.md`, `README.md`, `changelog.d/4701-review-dispatch-default-branch.md`

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). CONCERN (EVIDENCE-BASED, sibling path, auto-decided AD-11 → #4898): `review_autofix.yml`'s post-commit retrigger dispatches at the head ref and its peer check `autofix_retrigger_has_inflight_peer` is head-branch-only. Note: the retrigger PR-named fallback acts only when the newest PR-named run is completed (plan text: newest completed run) — stricter and safe, no change. Checks: 61 + 450 tests pass, actionlint clean on changed workflows, ShellCheck clean on the 7 changed poller functions and `review_merge_train.sh`.

## Security pass
- Cycle 1 — run 36509277736 2026-09-29 (ref: project branch, audited 073cce8..6f201fc, conclusion success): follow-ups #4926 (`consumer-review-handoff-unrecognized`, check_in_status.py; its project is blocked on the protected-path question, answered D twin-first), #4927 (`review-run-global-window-exhaustion`, poller), #4928 (`sweep-discards-null-head-dispatch`, sweep) — waiting

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Which base branch does the project build on? — Picked: A — #4618's project branch `claude/implement-plan-issue-4618-sweep-dispatch-default-branch`. Alternatives: B — `main`; C — stop until #4618 merges. Why: the issue says to build on it or wait, and the `run-name` and `_has_active_autofix_run` pattern exist only there. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Is the forward-merge fallback dispatch exposed, and does it move? — Picked: A — move it to the default branch too. Alternatives: B — keep `--ref` with a comment. Why: a writer can push to the workflow-cut branch before the dispatch, and only the default branch's `internal-review.yml` carries the PR `run-name`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] How do consumer `ai-review.yml` dispatch runs stay visible? — Picked: A — `run-name: AI Review [pr:<N>]` in the template, matched by every lookup. Alternatives: B — keep head-ref dispatch for `ai-review.yml`; C — no name. Why: closes the finding in consumer repos too. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] How do the poller lookups find PR-named runs with one call? — Picked: A — shared `_pr_named_review_dispatch_runs`, one `gh run list --event workflow_dispatch --limit 100`, exact-title match. Alternatives: B — one call per wrapper; C — per-cycle wrapper resolution. Why: one call (§15), no new state. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] How does the stall-judge lookup find PR-named runs? — Picked: A — extend its jq over the cached runs blob. Alternatives: B — one helper call on a miss. Why: zero API calls. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] When does the retrigger lookup issue its PR-named call, and which run decides? — Picked: A — only when no head-branch failure was found; the newest completed PR-named run decides when newer than every completed head-branch run. Alternatives: B — only when no head-branch run exists; C — any PR-named failure. Why: B misses the common case, and C acts on superseded failures. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-28] Include the empty-commit push guards? — Picked: A — yes. Alternatives: B — leave them. Why: otherwise stall recovery can push an empty commit under a live default-branch dispatch run. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-28] Map PR-named runs to issues in `build_active_issue_set`? — Picked: A — no. Alternatives: B — per-run PR-to-issue lookups. Why: §15 cost; the acting paths are guarded. Applied in: no code change. Status: pending review
- AD-9 [plan, 2026-09-28] How does the merge-train release see PR-named runs? — Picked: A — `pr:<N>` keys from its existing listing. Alternatives: B — per-PR call in the loop. Why: zero new calls. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-28] Add a PR `run-name` to `review_autofix.yml`? — Picked: A — no. Alternatives: B — yes. Why: it is the last-resort candidate, and the file is the 451 KB reusable workflow (§5, §27). Applied in: no code change. Status: pending review
- AD-11 [conformance 1/3, 2026-09-29] `review_autofix.yml`'s post-commit retrigger (review_autofix.yml:6337-6375) still dispatches at the PR head ref, and its peer check `autofix_retrigger_has_inflight_peer` (scripts/gh_helpers.sh) keys by head branch only; fix it in this project? — Picked: A — no code change here; follow-up issue #4898. Alternatives: B — fix it in a conformance fix PR; C — record only. Why: the plan keeps review_autofix.yml untouched (AD-10, §27), and the fix needs a dispatch-target choice that affects the PR #3895 dedupe; #4898 gets its own security pass. Applied in: no code change (#4898). Status: pending review

## Lessons
- [source:plan-deviation] Moving a workflow dispatch off a head ref makes every head_branch-keyed run lookup blind to it; audit the empty-commit push guards and the stall judge along with the active-run dedupe, not only the lookups a finding names. (files: scripts/orchestrate_poll_process.sh, scripts/review_merge_train.sh)
- [source:intervention] Never pass a possibly-empty value to `grep -Fx -e`: an empty pattern matches a blank line of the haystack; add it with `${var:+-e "${var}"}`. (files: scripts/review_merge_train.sh)
- [source:conformance] When moving a workflow dispatch off a head ref, grep every `gh workflow run … --ref` for the same workflows, including the reusable workflow's own retrigger step and its peer check, before scoping the issue. (files: .github/workflows/review_autofix.yml, scripts/gh_helpers.sh)
- [source:security] A run lookup that finds default-branch dispatches by name must cover every wrapper's name (internal and consumer), keep runs whose head_branch is null, and treat one global `gh run list --limit 100` page as possibly incomplete before a destructive action such as an empty-commit push. (files: .claude/scripts/check_in_status.py, scripts/orchestrate_poll_process.sh, .github/workflows/review_autofix_sweep.yml)

## Notes
- Issue mode: plan written by /implement-issue-claude for #4701; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned `skip: false` (no skip label), so the pass runs.
- Local verification: this container lacks `gawk`; 40 tests in 5 unrelated modules (`test_review_issue_ledger`, `test_review_reject_verify`, `test_review_parse_consolidator`, `test_review_pipeline_integration`, `test_implement_post_codex_recovery`) fail here identically with and without this change. ShellCheck on the whole 1 MB poller is killed for memory here, so the changed functions were checked extracted.
- 2026-09-29 stage-start sync (conformance 1/3) merged the then-base claude/implement-plan-issue-4618-sweep-dispatch-default-branch into the project branch (6f201fc).
- 2026-09-29 base move: #4634 (head claude/implement-plan-issue-4618-sweep-dispatch-default-branch) merged into main (a0b5be8, squash). The project branch took the 4618 final tip (9ea7299) first, then main; main's squash re-applied 4618 hunks that phase 1 edits, so the leftover conflicts (agents.md, the poller, test_conflict_dispatch_active_run_visibility.py) were re-merged with 9ea7299 as base, keeping main's #4869 draft-claude/* skip. Net diff against main equals the phase 1 diff. Final PR #4709 retargeted to main; its body now carries Fixes #4701 and steps 12–13 run.
