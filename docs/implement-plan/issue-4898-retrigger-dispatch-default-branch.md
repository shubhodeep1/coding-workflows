# Implement-Plan Log — Dispatch review_autofix.yml's post-commit retrigger from the default branch, and let its peer check see PR-named runs

- Plan: docs/plans/issue-4898-retrigger-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#4898 (https://github.com/shubhodeep1/coding-workflows/issues/4898)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch   Final PR: #4923 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR (branch claude/implement-plan-issue-4898-retrigger-dispatch-default-branch-conformance-fix-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01GWp9PnRZTCBZkQhCbRnyNw (project checker, reused)
- Last updated: 2026-09-29
- Last note: conformance run 1 INCOMPLETE (1 EVIDENCE-BASED BLOCKER, 2 CONCERNs); fix PR opened against the project branch

## Phases
1. [x] Phase 1 — default-branch retrigger dispatch plus PR-named probes   — PR #4982 merged 2026-09-29
   - [x] both retrigger step bodies dispatch without `--ref`, with a validated PR number, wrappers first and `review_autofix.yml` last (AD-2, AD-3)
   - [x] `_autofix_pr_named_review_runs` helper (AD-5); `autofix_retrigger_has_inflight_peer` sees PR-named in-flight runs (fail open)
   - [x] `autofix_changes_lost_head_retry_consumed` counts PR-named completed runs since the head's push-time bound (fail closed, AD-4)
   - [x] both step bodies moved to `scripts/review_autofix_step_{post_commit_retrigger,changes_lost_redispatch}.sh`, registered (AD-8)
   - [x] E2E exposure comments in `test-and-mark-stable.yml` (AD-6)
   - [x] tests, `README.md`, `agents.md`, `docs/INVENTORY.md`, `changelog.d/4898-retrigger-dispatch-default-branch.md`

## Conformance
- Run 1 — 2026-09-29: INCOMPLETE (Step 4 FAIL) — fix PR from `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch-conformance-fix-1` (pre-security). BLOCKER: the changes-lost budget could not see a retry dispatched under a name the PR-named match misses (renamed caller, `review_autofix.yml`, or a pre-#4701 `ai-review.yml`), so with the head's pull_request twin cancelled the retry looped without bound (fixed per AD-9). CONCERNs: stale `$1 pr_number` / dispatch comments in `scripts/gh_helpers.sh`; changelog size row no longer true after the base grew `review_autofix.yml` (fixed).

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
- AD-9 [conformance 1/3, 2026-09-29] How does the changes-lost budget stay bounded when a retry runs under a name the PR-named match cannot see (a renamed caller, `review_autofix.yml`, or an `ai-review.yml` that predates the `[pr:<N>]` run name)? — Picked: A — fail closed when the current run is a `workflow_dispatch` run not among the PR-named runs (`reason=unnamed_dispatch_run`), reading the PR-named page without a status filter (same one call). Alternatives: B — restrict the changes-lost re-dispatch to the PR-named wrappers (a pre-#4701 `ai-review.yml` still loops, and the two chains stop being identical); C — leave it until consumer wrappers sync. Why: the only option that bounds every fallback path without an extra API call; an unnamed run loses one automated retry and falls back to today's terminal comment. Applied in: conformance fix PR. Status: pending review

## Lessons
- [source:plan-deviation] Moving a dispatch off the head ref also blinds any head-SHA loop bound keyed on that dispatch's runs; extend the bound (here, PR-named runs since the push) in the same change, or the retry loop loses its limit. (files: scripts/gh_helpers.sh, scripts/review_autofix_step_changes_lost_redispatch.sh)
- [source:plan-deviation] `tests/test_log_prefix_regressions.sh` pins the exact `AUTOFIX_PEER_CHECK` line, so a new field on a pinned log line is a breaking change; keep the line and surface new detail elsewhere. (files: scripts/gh_helpers.sh, tests/test_log_prefix_regressions.sh)
- [source:conformance] A loop bound that recognises its own retry by run name must fail closed when the current run carries no such name: a retry dispatched under a fallback name is invisible to the next run, and the bound silently disappears. (files: scripts/gh_helpers.sh, scripts/review_autofix_step_changes_lost_redispatch.sh)

## Notes
- Conformance 1/3 (2026-09-29): project branch synced with the issue base (`claude/implement-plan-issue-4701-review-dispatch-default-branch`, not merged; final PR #4709 open into `main`) as merge `eb1fb86`; zombie checkers archived: 0.
- Issue mode: plan written by /implement-issue-claude for #4898; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned `skip: false` (`no skip label`), so the pass runs.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4898#issuecomment-5882065747 (id 5882065747).
- Phase 1: `AUTOFIX_PEER_CHECK` keeps its exact field set (pinned by `tests/test_log_prefix_regressions.sh`), so the plan's `peer_source=` field was dropped; a PR-named peer shows in `peer_run` / `peer_path`. `tests/test_gh_helpers_list_runs_method.py` was not run by CI before; phase 1 adds it, with the new module, to the "Editor-changes-lost re-dispatch budget tests" step.
- The invoking session started without `gh` and without the GitHub MCP tools; `gh` was installed with `.claude/hooks/session-start.sh`, and GitHub writes go through `gh api` (REST) via the session proxy.
