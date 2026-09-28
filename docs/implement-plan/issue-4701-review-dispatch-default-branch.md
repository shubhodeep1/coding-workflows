# Implement-Plan Log — Dispatch the remaining review runs from the default branch, and find PR-named runs in the poller's lookups

- Plan: docs/plans/issue-4701-review-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#4701 (https://github.com/shubhodeep1/coding-workflows/issues/4701)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4618-sweep-dispatch-default-branch
- Project branch: claude/implement-plan-issue-4701-review-dispatch-default-branch   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened from the #4618 project branch (AD-1)

## Phases
1. [ ] Phase 1 — default-branch dispatch at the remaining sites, plus PR-named run lookups
   - [ ] `_dispatch_review_for_conflicts`, `_mt_dispatch_review`, and the forward-merge fallback dispatch without `--ref`, with a validated PR number
   - [ ] `workflow-templates/ai-review.yml` names dispatched runs `AI Review [pr:<N>]`
   - [ ] `_pr_named_review_dispatch_runs` helper; `_has_active_autofix_run` uses it
   - [ ] stall-judge `workflow_outcomes` matches PR-named runs from the cached blob
   - [ ] retrigger failed-autofix lookup: PR-named fallback (AD-6)
   - [ ] empty-commit push guards see PR-named runs (AD-7)
   - [ ] merge-train release sees `pr:<N>` keys (AD-9)
   - [ ] tests, `agents.md`, `README.md`, `changelog.d/4701-review-dispatch-default-branch.md`

## Conformance

## Security pass

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

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #4701; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned `skip: false` (no skip label), so the pass runs.
