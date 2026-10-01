# Count a PR-named review run as active only when it ran from the default branch

Source issue: shubhodeep1/coding-workflows#5839 (https://github.com/shubhodeep1/coding-workflows/issues/5839)
Base branch: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable
Security pass: skip (ai:security: automation-produced issue)

## Summary

`.claude/scripts/check_in_status.py` counts any active `workflow_dispatch` run whose workflow path and run name match a PR-named review wrapper (`internal-review.yml` / `Internal: AI Review & Autofix [pr:<N>]`, `ai-review.yml` / `AI Review [pr:<N>]`) as an active review of PR `<N>`, whatever branch it ran from. A run dispatched from any other branch with that title therefore holds a conflicted, failing, or handed-off PR in `open` / `wait` instead of letting it become `stuck`, `conflict`, `review-round`, or `ci-failed`. This plan adds the default-branch provenance check the hand-off review-run path already applies.

## Context

- Security audit finding `status-checker-accepts-spoofed-active-review` (issue #5839, `STRIDE: Spoofing / Denial of Service`, medium, confidence 9/10, `.claude/scripts/check_in_status.py:316`), filed under tracker #3576 with `Integration branch: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable`.
- `_active_run_count` (`.claude/scripts/check_in_status.py:294-317`) reads `PR_NAMED_REVIEW_RUNS_PATH` (the newest 100 `workflow_dispatch` runs of the repository) when the head-branch reads find nothing, and counts runs with an active status for which `_is_pr_named_review_pair(run, pr_number)` is true. That predicate checks only `(path, display_title)`. `display_title` comes from the wrapper's `run-name`, so a run of a branch-local copy of `ai-review.yml` (or `internal-review.yml`) dispatched with `--ref <any branch>` produces the same pair.
- The same file already binds a PR-named run to the default branch where it accepts a hand-off's review run: `_is_pr_dispatched_review_run` (`:338-354`) requires `event == "workflow_dispatch"`, `head_branch ==` the PR base repository's default branch (`_pr_default_branch(pr)`, `:330-335`), and the exact pair, and fails closed when the default branch is unknown. Issue #5094 made the orchestrator poller (`scripts/orchestrate_poll_process.sh`) apply the same rule to its PR-named matches. `_active_run_count` is the remaining site without it.
- Callers that pass `pr_number` (all reach the repo-wide listing): `check_pr` (`:288`), `_check_claude_fixer_pr` conflict and hand-off paths (`:480`, `:486`), and `check_pr_hand_back` (`:662`), which `scripts/claude_pr_sweep.py` also calls. Each already has the PR object or a `default_branch` argument in hand.
- `workflow-templates/.claude/scripts/check_in_status.py` is the byte-identical consumer twin (`tests/test_check_in_status_hand_back.py:489-490` enforces parity).
- CLAUDE.md §28.C interim twin-first default: `.claude/**` is protected, so the phase edits only the `workflow-templates/.claude/` twin; the `.claude/` copy is synced from it by a `[claude-twin-sync]` commit before the phase PR can merge.

## Goals

- A run in the repo-wide `workflow_dispatch` listing counts toward `_active_run_count` only when its status is active, its `event` is `workflow_dispatch`, its `head_branch` equals the PR base repository's default branch, and its `(path, display_title)` is a `PR_NAMED_REVIEW_DISPATCHES` pair for the PR.
- When the default branch is unknown, no PR-named run counts and the listing is not read (fail closed, no wasted call, §15).
- Every caller that passes `pr_number` also passes the PR's default branch; the head-branch reads and their counts are unchanged.
- The twin and `.claude/scripts/check_in_status.py` stay byte-identical after the twin sync.
- Tests prove: a same-title run from a non-default branch does not hold a conflict, hand-off, stuck, or `ci-failed` verdict; a default-branch run still does; an unknown default branch skips the listing.

## Non-goals

- The hand-off review-run acceptance (`_is_pr_dispatched_review_run`), which already checks provenance.
- The orchestrator poller and `review_autofix.yml` retrigger peer probe (fixed or tracked elsewhere, issue #5094 / #4898).
- Changing `PR_NAMED_REVIEW_DISPATCHES`, `PR_NAMED_REVIEW_RUNS_PATH`, the 100-run window, or any verdict field.

## Constraints

- §6: no rename or removal; `_active_run_count` gains one keyword-only-by-convention argument `default_branch: str | None = None` (new name, checked unique in the module). `_is_pr_named_review_pair` and `DISPATCHED_REVIEW_*` stay.
- §15: no new API call; the listing read is skipped when it cannot match anything.
- §5: only the predicate, its callers, the docstrings, tests, the agents.md bullet, and a changelog fragment.
- §9: tabs in Python.
- §20: security fix → one `changelog.d/5839-…` fragment.
- §28.C: protected path → twin-first; hold claim and twin-sync blocker after the phase PR opens.

## Approach

In `_active_run_count`, replace the `_is_pr_named_review_pair(run, pr_number)` test with `_is_pr_dispatched_review_run(run, pr_number, default_branch)`, the predicate that already encodes event + default-branch + pair, and return the head-branch count without reading the listing when `default_branch` is None. Pass `default_branch=_pr_default_branch(pr)` from `check_pr` and `check_pr_hand_back`, and the existing `default_branch` argument from `_check_claude_fixer_pr`.

Alternatives: an inline `head_branch` check next to the pair test (duplicates the existing predicate); an extra `GET repos/<repo>` read for the default branch (a new call for data the PR object already carries, §15).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue.

1. **Phase 1 — bind active PR-named review runs to the default branch** — protected paths: `.claude/scripts/check_in_status.py` (twin-first: edit `workflow-templates/.claude/scripts/check_in_status.py`; `.claude/` synced by `[claude-twin-sync]`).
   - Files: `workflow-templates/.claude/scripts/check_in_status.py`, `.claude/scripts/check_in_status.py` (twin sync only), `tests/test_check_in_status_hand_back.py`, `tests/test_check_in_status.py`, `agents.md`, `changelog.d/5839-checker-pr-named-run-default-branch.md`.
   - Done: the goals above hold in the twin; the new tests pass against the twin; the full check-in, hand-back, sweep, and session-targeting suites pass once the twin is synced; both copies are identical.
   - Rollback: revert the phase PR; the previous predicate returns with no data or config change.

## Implementation Steps

1. Twin `check_in_status.py`: add `default_branch: str | None = None` to `_active_run_count`; skip the listing when it is None; count with `_is_pr_dispatched_review_run`; update its docstring.
2. Pass the default branch at the four call sites (`check_pr`, two in `_check_claude_fixer_pr`, `check_pr_hand_back`).
3. Tests: give existing dispatched-run fixtures `event: workflow_dispatch` and `head_branch: main`, and the PRs they pair with `base=DEFAULT_BASE`, so they pass against both copies; add twin-loaded tests for the spoofed-branch, default-branch, and unknown-default-branch cases on the conflict, hand-off, stuck (`check_pr`), and `ci-failed` (`--hand-back`) paths.
4. `agents.md` verdict-helper bullet: an active PR-named run holds a hand-off back only when it ran from the default branch.
5. `changelog.d/5839-checker-pr-named-run-default-branch.md` (`<!-- changelog: security -->`).

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py`
- `.claude/scripts/check_in_status.py` (twin sync)
- `tests/test_check_in_status_hand_back.py`
- `tests/test_check_in_status.py`
- `agents.md`
- `changelog.d/5839-checker-pr-named-run-default-branch.md` [new]

## Tests

- Unit (pytest): `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_session_targeting.py` (the `ci.yml` steps that cover this script).
- Before the twin sync the twin-loaded tests pass and the template-parity test fails by design; after it everything passes.

## Risks & Mitigations

- A legitimate review dispatched from a non-default branch no longer holds the checker back → ACCEPTED — every documented dispatcher (sweep, poller, merge train, retrigger) dispatches from the default branch (issues #4618, #4701, #4898, #4926).
- PR object without `base.repo.default_branch` → no PR-named run counts; GitHub always returns it, and the result is the same fail-closed rule `_is_pr_dispatched_review_run` already uses.

## Rollout

Ships with the base project's chain, then to consumers on the next `@stable` sync of `workflow-templates/.claude/`. No flag, no data migration.

## References

- #5839 (this finding), #3576 (security audit tracker), #4618, #4701, #4926, #5094, #4898, #4785 (twin sync automation).

## Auto-decisions

- AD-1 [plan, 2026-10-01] How should `_active_run_count` bind a PR-named run to its PR? — Picked: A — reuse `_is_pr_dispatched_review_run` (event + default-branch head + exact pair). Alternatives: B — add an inline `head_branch` check beside `_is_pr_named_review_pair`; C — read the repository's default branch with an extra API call. Why: one predicate for both sites, no new call (§15), matches #5094. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What happens when the PR's default branch is unknown? — Picked: A — count no PR-named run and skip the listing read. Alternatives: B — fall back to the old path + title match. Why: fail closed against the spoof, same rule as the hand-off path, saves a call. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How do the tests cover a protected-path change under twin-first? — Picked: A — new tests load the twin; existing fixtures gain `event`/`head_branch`/`base` so they pass against both copies. Alternatives: B — parametrize every test over both copies (red until the sync). Why: the phase is verifiable before the sync, and the parity test guarantees both copies match after it. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
