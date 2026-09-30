# Claude-fixer review: a failed-slot ledger must cover every reviewer the runner ran

Source issue: shubhodeep1/coding-workflows#5297 (https://github.com/shubhodeep1/coding-workflows/issues/5297)
Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote
Security pass: skip (ai:security: automation-produced issue)

## Summary

On the Claude-fixer failed-slot path, the reviewer consensus ledger can leave a reviewer out entirely, and the hand-off step never notices, so a review that reported a finding can be auto-merged. This plan makes the clean check compare the ledger's reviewer blocks with the reviewer roster the runner itself wrote and fail closed when a slot is missing.

## Context

- #4835 (the base branch's project) made a failed reviewer slot a missing vote: a ledger with a verified failed block is clean when no block is a finding and at least `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` (default 5) clean votes remain. #4885 binds each failed block to the runner's exact failure line, and #5114 binds each clean block to the runner's own `review_<slug>.txt`.
- The security audit of the project branch (issue #5297, tracker #3576) found the remaining gap in `scripts/review_autofix_step_claude_fixer_handoff.sh:355`. The check only inspects the blocks the ledger contains. The ledger is model output (`scripts/summarize_reviewer_consensus.sh`, one OpenCode call), so it can omit a reviewer's block. With six reviewers and the minimum set to 4, four clean blocks plus one verified failed block pass while the sixth reviewer's finding never reaches the check.
- The runner (`scripts/review_run_reviewers.sh`, `run_reviewer_pass`) writes `status_review_<safe_name>.txt` for every slot it launches or skips, and `review_<safe_name>.txt` for its output. The summariser reads exactly the `review_*.txt` files in the same `PREVIOUS_REVIEWS_DIR`, which is fresh per run (`${RUNTIME_DIR}/previous_reviews`). These runner files are the roster.
- CLAUDE.md §1 (security first), §5 (minimal change), §6 (no renames), §7 (docs), §20 (changelog fragment).

## Goals

- On the failed-slot path, every reviewer slot named by a runner file in `PREVIOUS_REVIEWS_DIR` (`status_review_<slug>.txt` or `review_<slug>.txt`) must have its own block in the ledger. A slot with no block makes the ledger not clean, with a `::warning::` naming the slot.
- A roster that is empty, unreadable, or holds a slug outside `^[A-Za-z0-9_-]+$` makes the ledger not clean.
- Every successful reviewer is therefore checked: its block must be a clean block backed by a clean runner output (the existing #5114 rule), or the round is handed off.
- The six-reviewer exploit from the issue (4 clean, 1 verified failed, 1 omitted finding, minimum 4) hands off instead of auto-merging.

## Non-goals

- Ledgers with no failed block keep today's every-block-clean rule (AD-1), as #4835 AD-1 and #5114 AD-1 decided.
- No change to the runner, the summariser, the `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` default, or the `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS` log line (AD-3).

## Constraints

- §6: no identifier renamed. New shell variables use the `claude_fixer_roster_` prefix, which no existing name in the script uses.
- §5: the change stays inside the failed-slot branch of the clean-ledger check, plus its comment block and the docs that describe the rule.
- §15: no new GitHub API call; the roster is read from local runner files.
- The check must fail closed: any doubt about the roster leaves the ledger not clean.

## Approach

Inside the existing `else` branch (at least one `failed` record), after the per-block loop and before the quorum test at line 355:

1. List the ledger's reviewer slugs from `claude_fixer_ledger_blocks` (the same field split the duplicate check uses).
2. Build the roster from `"${PREVIOUS_REVIEWS_DIR}"/status_review_*.txt` and `"${PREVIOUS_REVIEWS_DIR}"/review_*.txt`, stripping the prefix and `.txt` (AD-2). Skip non-files (an unmatched glob). An unset or missing directory, an empty roster, or a slug that fails `^[A-Za-z0-9_-]+$` sets `claude_fixer_failed_slots_verified=false` with a warning.
3. For each distinct roster slug, test membership in the ledger slug list with a literal newline-delimited pattern match (no `printf | grep -q`, per the pipefail note already in the script). A missing slug sets `claude_fixer_failed_slots_verified=false` with `::warning::Claude-fixer ledger omits reviewer '<slug>' that the reviewer runner ran; the ledger is not clean.`

The quorum test at line 355 is unchanged. A ledger block whose slug is not in the roster already fails, because its status file is missing.

Alternatives: have the runner write an explicit roster file of the active models (AD-2 B). That changes the runner and the workflow wiring for a gap the existing runner files already close, and a slot the runner never recorded produced no output the ledger could hide.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — Ledger must cover the runner's reviewer roster.**
   - Files: `scripts/review_autofix_step_claude_fixer_handoff.sh`, `tests/test_review_autofix_claude_fixer_mode.py`, `README.md`, `agents.md`, `changelog.d/5297-ledger-must-cover-reviewer-roster.md`.
   - Done: the exploit test hands off; the existing Claude-fixer tests pass unchanged; the workflow size test and the review_autofix contract tests pass; shellcheck is clean.
   - Rollback: revert the phase PR on the project branch; the check returns to the #5114 behaviour.

## Implementation Steps

Phase 1:
1. `scripts/review_autofix_step_claude_fixer_handoff.sh`, clean-ledger comment (lines ~125–148) and header (lines ~20–28): add that the ledger must carry a block for every slot the runner wrote a `status_review_<slug>.txt` or `review_<slug>.txt` for (issue #5297).
2. Same file, failed-slot branch (after the `while` loop, before line 355): the roster check from Approach.
3. `tests/test_review_autofix_claude_fixer_mode.py`: add tests (below).
4. `README.md` (`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` row) and `agents.md` (Claude-fixer paragraph): one sentence each on the roster rule.
5. `changelog.d/5297-ledger-must-cover-reviewer-roster.md` with `<!-- changelog: security -->`.

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `changelog.d/5297-ledger-must-cover-reviewer-roster.md` [new]

## Tests

Unit tests through `_run_handoff`, which writes the runner files:
- The issue's exploit: six-reviewer panel, one verified failed slot, one reviewer with a finding output whose block is left out of the ledger, minimum 4 → hand-off, warning names the omitted slug, no `CLAUDE_FIXER_ZERO_FINDINGS`.
- A slot omitted from the ledger whose runner output is clean still hands off (the rule is coverage, not only findings).
- A slot with only a `review_<slug>.txt` (no status file) omitted from the ledger hands off.
- A roster slug outside the allowed characters hands off.
- The existing five-clean-one-failed test still auto-merges (full coverage), and the no-failed-slot test still does not read runner files.
Run the whole Claude-fixer test file, `tests/test_workflow_file_size_limit.py`, the review_autofix contract tests, `shellcheck` on the script, and the tests under both `mawk` and `gawk` where the file already does.

## Risks & Mitigations

- A clean run where the summariser drops a reviewer's block now hands off instead of merging → ACCEPTED — that is the fail-closed behaviour the finding asks for; the Claude session judges the round as usual.
- A skipped slot (`skipped_open`, `skipped_budget`) is in the roster → its block already fails the classifier today (#4835 AD-3), so no new stall.

## Rollout

No flag. It ships with the #4835 project's final PR into `main`, then to consumer repos on the next `@stable` release; the step script is in the support bundle, so no wrapper change. Rollback: revert the phase PR on the project branch.

## References

- Issue #5297, audit tracker #3576
- #4835 / #4885 / #5114 plans on the base branch

## Auto-decisions

- AD-1 [plan, 2026-09-29] Should the roster check also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited line are the failed-slot quorum; the every-block-clean rule is main's pre-existing behaviour shared with the GPT path, which #4835 AD-1 and #5114 AD-1 kept unchanged (§5, §12.D). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What is the runner-generated roster? — Picked: A — every slug with a `status_review_<slug>.txt` or `review_<slug>.txt` in `PREVIOUS_REVIEWS_DIR`. Alternatives: B — a new roster file written by the runner from the active model list; C — the status files only. Why: A covers exactly the files the summariser reads plus every slot the runner recorded, with no runner change; C misses an output with no status file; B widens the change to the runner and workflow. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Add the roster size to the `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS` log line? — Picked: A — no; the omission is reported by its own `::warning::` line. Alternatives: B — append `roster=<n>`. Why: §5 and §6; the log key's documented shape stays as is. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Security pass for this project? — Picked: A — skip, as `security_pass_skip.py` verified (`ai:security` created and labelled by the issue automation). Alternatives: B — run. Why: the issue is itself an audit follow-up; its own audit could open follow-ups of follow-ups, and the parent project's security pass re-audits the branch. Applied in: no code change. Status: pending review
