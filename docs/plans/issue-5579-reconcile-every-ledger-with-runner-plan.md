# Claude-fixer review: reconcile every clean ledger with the reviewer runner

Source issue: shubhodeep1/coding-workflows#5579 (https://github.com/shubhodeep1/coding-workflows/issues/5579)
Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote
Security pass: skip (ai:security: automation-produced issue)

## Summary

In Claude-fixer mode a zero-finding ledger with no failed reviewer block is
marked clean from the ledger text alone, so a summariser ledger that leaves a
failed reviewer out, or shows it as clean, can enable auto-merge on green
checks. This plan runs the runner reconciliation that already guards the
failed-slot path (reviewer roster coverage, a `success` status per clean
block, no repeated block) on every zero-finding ledger, and fails closed on
any gap.

## Context

- Security audit finding #5579 (`A08:2021`, high, confidence 9/10) at
  `scripts/review_autofix_step_claude_fixer_handoff.sh:349`: when
  `claude_fixer_ledger_blocks` holds no `failed` record, the branch sets
  `claude_fixer_clean_ledger="true"` without reading `PREVIOUS_REVIEWS_DIR`.
  The ledger is model output (`scripts/summarize_reviewer_consensus.sh`), so
  it can drop a reviewer's block entirely or write `(No findings reported.)`
  for a slot the runner recorded as `failed`, and the round then takes the
  auto-merge path.
- The same base branch already reconciles failed-slot ledgers with the
  runner: failed lines against `review_<slug>.txt` (#4885), clean votes
  against the runner output and a strict verdict format (#5114, #5298), and
  roster coverage (#5297). Each of those projects scoped its check to the
  failed-slot path and left the no-failed-slot rule for review
  (#4835 AD-1, #5114 AD-1, #5297 AD-1, #5298 AD-4). This issue asks for the
  reconciliation on every ledger.
- The runner (`scripts/review_run_reviewers.sh`) writes
  `status_review_<slug>.txt` (`success`, `failed`, `skipped_budget`,
  `skipped_unmapped`, `skipped_open`, `pr_closed`) and `review_<slug>.txt` for
  every slot it runs. The summariser reads every non-empty `review_<slug>.txt`
  and is told to emit one block per input, so an honest ledger covers the
  whole roster.
- Measured on the `reviewer-logs-*` artifacts of the 20 most recent review
  runs (2026-09-30): 87 slots read `success`; only 14 of their outputs pass
  the #5298 strict verdict format, 16 fail it only on free-text citations or
  severity words, and 6 have no `NONE` line. Of the runs whose successful
  outputs report no finding, none would pass the strict format for every
  reviewer.

## Goals

- A zero-finding ledger with no failed block is clean only when every
  reviewer slot the runner wrote a `status_review_<slug>.txt` or
  `review_<slug>.txt` for has its own ledger block, every reviewer block's
  status file reads `success`, and no reviewer block repeats.
- `PREVIOUS_REVIEWS_DIR` unset or not a directory, an empty roster, a roster
  file with an unexpected slot name, an omitted slot, a status other than
  `success`, or a repeated block leaves the ledger not clean, with the
  existing `::warning::` line naming the reason; the round is handed off.
- A failed-slot ledger behaves exactly as today.
- A panel whose runner files match an honest all-clean ledger still
  auto-merges with a fresh ready check snapshot, with no minimum.

## Non-goals

- The strict runner-output verdict format (#5298) and
  `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` stay on the failed-slot path only (AD-1,
  AD-2).
- No change to the reviewer runner, the summariser, the ledger format, the
  hand-off comment text, or the GPT (non-fixer) path.
- No new variable, log key, or GitHub API call (AD-3).

## Constraints

- §1: fail closed; any doubt about the roster or a status keeps the round a
  hand-off.
- §5: the change stays in the clean-ledger block of the hand-off script, its
  comment blocks, the tests, and the docs that describe the rule.
- §6: no identifier renamed or repurposed; `claude_fixer_failed_slots_verified`
  keeps its name and now also covers no-failed-slot ledgers. The one new
  shell variable, `claude_fixer_ledger_has_failed_slot`, collides with
  nothing in the script (checked with grep).
- §9: the script keeps its 2-space bash style.
- §15: no GitHub API call; the roster is read from local runner files.
- §20: one `changelog.d/5579-…` fragment (security fix).
- §27: `review_autofix.yml` is not touched.

## Approach

In `scripts/review_autofix_step_claude_fixer_handoff.sh`, inside the existing
`if` that parses the zero-finding ledger:

1. Record whether the ledger has a failed block
   (`claude_fixer_ledger_has_failed_slot`) instead of branching on it.
2. Run the existing repeat check, per-block loop, and roster check for every
   ledger. In the loop, the `clean:success` case reads the runner output
   (`claude_fixer_runner_output_state`) only when the ledger has a failed
   block; without one, a `success` status is the clean vote (AD-1). Every
   other kind/status pair keeps its current handling, so a clean block over a
   `failed`, `skipped_*`, or `pr_closed` slot, or one with no status file,
   is not clean.
3. The verdict: with a failed block, today's rule (verified and at least the
   minimum, with the `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS` log line);
   without one, verified alone.

Alternatives are recorded under Auto-decisions.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — Reconcile every zero-finding ledger with the runner.**
   - Files: `scripts/review_autofix_step_claude_fixer_handoff.sh`,
     `tests/test_review_autofix_claude_fixer_mode.py`, `README.md`,
     `agents.md`, `changelog.d/5579-reconcile-every-ledger-with-runner.md`.
   - Done: the exploit tests (a no-failed-slot ledger that omits a failed
     reviewer, and one that shows a failed reviewer as clean) hand off; an
     honest all-clean panel still auto-merges; every other Claude-fixer test
     passes; the review_autofix step-script and workflow-size tests pass;
     shellcheck is clean.
   - Rollback: revert the phase PR on the project branch; the no-failed-slot
     path returns to the ledger-text rule.

## Implementation Steps

Phase 1:

1. `scripts/review_autofix_step_claude_fixer_handoff.sh` (≈ lines 20–31,
   129–156, 309–466): replace the no-failed-block short-circuit with the
   flag, gate the runner-output check in `clean:success` on the flag, run the
   repeat, loop, and roster checks for both cases, and split the final
   verdict; update the header and clean-ledger comments.
2. `tests/test_review_autofix_claude_fixer_mode.py`: give the zero-finding
   tests that expect the clean path (or test what follows it) a runner
   roster; replace `test_all_clean_ledger_without_failed_slots_keeps_todays_rule`
   with tests for the new rule; add the exploit tests.
3. `README.md` (`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` row) and `agents.md`
   (Claude-fixer mode paragraph): replace "Ledgers with no failed slot are
   unaffected" / "keeps the every-block-clean rule" with the new rule.
4. `changelog.d/5579-reconcile-every-ledger-with-runner.md` (security).

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `changelog.d/5579-reconcile-every-ledger-with-runner.md` [new]

## Tests

Unit (bash script executed with a stubbed `gh`, as the file already does):

- No failed block, the ledger omits a reviewer the runner recorded `failed`
  → hand-off, `ledger omits reviewer` warning.
- No failed block, the ledger shows a `failed` slot as clean → hand-off,
  status warning.
- No failed block, a clean block over a `skipped_open` slot → hand-off.
- No failed block, `PREVIOUS_REVIEWS_DIR` unset, and an empty roster →
  hand-off with the roster warnings.
- No failed block, a repeated clean block → hand-off.
- No failed block, full panel with `success` statuses and outputs that fail
  the strict format → still clean (the strict format stays on the
  failed-slot path), no `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS` line, no
  minimum with fewer than 5 reviewers.
- mawk and gawk both give the same answer for the exploit and the clean case.
- Existing: every failed-slot test unchanged; the zero-finding, stale-check,
  and failed-check tests run with a roster so they still cover what follows
  the clean check.

Also run `tests/test_review_autofix_*` (step-script registry and contract
tests), `tests/test_workflow_file_size_limit.py`, and `shellcheck` on the
script.

## Risks & Mitigations

- More hand-offs when the summariser drops or mislabels a slot on an
  otherwise clean review. ACCEPTED — that is the fail-closed behaviour the
  issue asks for; the warning names the slot, and honest ledgers cover the
  roster.
- A reviewer whose circuit breaker is open (`skipped_open`) can now turn a
  ledger that showed it as clean into a hand-off. ACCEPTED — the slot never
  voted; the status check is the reconciliation the issue requires.
- A completed reviewer's finding mislabelled clean by the summariser on a
  no-failed-slot ledger is still not caught. ACCEPTED — pending the human
  review of AD-1 (and #5114 AD-1 / #5298 AD-4); the stricter options would
  stall most clean reviews on current data.

## Rollout

Lands on the #4835 project branch and reaches `main` with that project's
final PR (#4847); consumers get it with the next `@stable` release, with no
wrapper change. Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which runner reconciliation must a zero-finding ledger with no failed block pass? — Picked: A — roster coverage, a `success` status for every reviewer block, and no repeated block. Alternatives: B — A plus the #5298 strict runner-output verdict format; C — A plus rejecting a clean block whose runner output carries a finding or task-gap field. Why: A closes the issue's exploit (an omitted or relabelled failed reviewer) with no false positives on honest ledgers; B would hand off nearly every clean review (14 of 87 successful outputs in the 20 latest runs pass the strict format), bringing back the #4835 stall; C flags the runner prompt's own `HARDENING_SUGGESTIONS` entries (`file`, `location` fields) as findings. Flagged for human review with #5114 AD-1 and #5298 AD-4. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Does `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` apply to a ledger with no failed block? — Picked: A — no. Alternatives: B — yes. Why: with full roster coverage and `success` statuses every reviewer that ran voted clean; B would stop panels of fewer than 5 reviewers (#4835 AD-1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Add a log key or hand-off line for a no-failed-slot ledger that fails reconciliation? — Picked: A — no; the existing `::warning::` lines name the reason. Alternatives: B — a new log key. Why: §5 and §6; the warnings are already searchable. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Security pass for this project? — Picked: A — skip, as `security_pass_skip.py` verified (`ai:security`, created and labelled by the issue automation). Alternatives: B — run. Why: the issue is itself an audit follow-up; the parent project's security pass re-audits the branch. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5579; parent project #4835 (final PR #4847); related #4885, #5114, #5297, #5298; audit tracker #3576.
