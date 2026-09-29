# Implement-Plan Log — Dispatch review_autofix.yml's post-commit retrigger from the default branch, and let its peer check see PR-named runs

- Plan: docs/plans/issue-4898-retrigger-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#4898 (https://github.com/shubhodeep1/coding-workflows/issues/4898)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from #4701's project branch; phase 1 starting

## Phases
1. [ ] Phase 1 — default-branch retrigger dispatch plus PR-named probes
   - [ ] both retrigger step bodies dispatch without `--ref`, with a validated PR number, wrappers first and `review_autofix.yml` last (AD-2, AD-3)
   - [ ] `_autofix_pr_named_review_runs` helper (AD-5); `autofix_retrigger_has_inflight_peer` sees PR-named in-flight runs (fail open)
   - [ ] `autofix_changes_lost_head_retry_consumed` counts PR-named completed runs since the head's push-time bound (fail closed, AD-4)
   - [ ] both step bodies moved to `scripts/review_autofix_step_{post_commit_retrigger,changes_lost_redispatch}.sh`, registered (AD-8)
   - [ ] E2E exposure comments in `test-and-mark-stable.yml` (AD-6)
   - [ ] tests, `README.md`, `agents.md`, `docs/INVENTORY.md`, `changelog.d/4898-retrigger-dispatch-default-branch.md`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which base branch does the project build on? — Picked: A — #4701's project branch `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Alternatives: B — `main`; C — stop until #4618 and #4701 reach `main`. Why: the issue's Ordering section names it, and the PR run names and PR-named lookup exist only there. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] In what order does the retrigger try workflows? — Picked: A — the caller when it is a PR-named wrapper, then `ai-review.yml`, `internal-review.yml`, a differently named caller, `review_autofix.yml` last. Alternatives: B — #4701's fixed order without the caller fallback; C — keep `review_autofix.yml` first. Why: only the wrappers' runs are visible to the probes; the caller-first order saves a failed call; the custom caller keeps today's fallback. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Is the editor-changes-lost re-dispatch in scope? — Picked: A — yes, with the same chain. Alternatives: B — leave it and file a follow-up issue. Why: same finding in the same file, and the contract test requires the two chains to stay identical. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How does the changes-lost loop bound see a default-branch retry? — Picked: A — count completed, non-cancelled PR-named runs created at or after the earlier of the head commit time and the head's first branch run; one extra call only when the branch lookup counted nothing; fail closed. Alternatives: B — no extension; C — count every completed PR-named run in the page. Why: B reopens an unbounded loop; C blocks the retry after any earlier review. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Where does the PR-named match live for the workflow probes? — Picked: A — new REST helper `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`. Alternatives: B — move the poller helper into `gh_helpers.sh`; C — inline the query in each probe. Why: the workflow never sources the poller; B refactors merged code (§5); C duplicates the match. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] What happens to the E2E dispatches in `test-and-mark-stable.yml`? — Picked: A — keep `--ref "${BRANCH}"` and document the exposure. Alternatives: B — dispatch from the default branch and re-key Phase 4 / 4b on PR-named runs. Why: the issue asks for a comment; B rewrites the E2E run matching. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Does the dispatch still pass `allow_workflow_edits`? — Picked: A — yes, normalised to `true` / `false`. Alternatives: B — pass only `pr_number`. Why: it is the run's own input, not PR data; dropping it changes consumer behaviour. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] How does `review_autofix.yml` stay under §27 as the bodies grow? — Picked: A — move both retrigger step bodies whole into `scripts/review_autofix_step_<slug>.sh`. Alternatives: B — keep them inline; C — move only the shared dispatch chain. Why: the issue asks for the move; the registry keeps contract tests reading the same text. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #4898; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned `skip: false` (`no skip label`), so the pass runs.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4898#issuecomment-5882065747 (id 5882065747).
- The invoking session started without `gh` and without the GitHub MCP tools; `gh` was installed with `.claude/hooks/session-start.sh`, and GitHub writes go through `gh api` (REST) via the session proxy.
