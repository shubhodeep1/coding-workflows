# Claude-fixer review: prove a failed reviewer slot from the runner's own output line

Source issue: shubhodeep1/coding-workflows#4885 (https://github.com/shubhodeep1/coding-workflows/issues/4885)
Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote
Security pass: skip (ai:security: automation-produced issue)

## Summary

The #4835 failed-slot rule counts a ledger block as a failed slot (a missing
vote) when its single line is one of the reviewer runner's three
retry-exhaustion lines for the block's own model and the runner's
`status_review_<slug>.txt` reads `failed`. The runner writes `failed` into
that status file for non-retryable errors too, so the status file does not
prove the failure class. The ledger line is summariser output over reviewer
output, so author-controlled review text could lead the summariser to write a
retry-exhaustion line for a slot that actually failed on a non-retryable error.
With five clean votes and green checks that enables auto-merge. This plan binds
each failed block to the runner-written `review_<slug>.txt`, which holds the
exact terminal line the runner produced, and fails closed on any mismatch.

## Context

- Security audit finding (issue #4885, tracker #3576): A08:2021, severity high,
  location `scripts/review_autofix_step_claude_fixer_handoff.sh:207` on the
  #4835 project branch.
- `scripts/review_autofix_step_claude_fixer_handoff.sh` lines 150–220: the awk
  classifier prints `failed <slug>` for a block whose line matches
  `^Reviewer [^ ]+ failed after (reaching the slot retryable-failure limit \([0-9]+\)|retryable failure recovery was exhausted|[0-9]+ attempts)\.$`
  and names the block's own model. The bash loop then accepts it when
  `${PREVIOUS_REVIEWS_DIR}/status_review_<slug>.txt` reads `failed` (line 207).
- `scripts/review_run_reviewers.sh` writes, for the final pass (`pass_prefix`
  `review`, lines 5040/5056), the slot's output to
  `${PREVIOUS_REVIEWS_DIR}/review_<safe_name>.txt` and its status to
  `status_review_<safe_name>.txt` (lines 4791–4794):
  - retry exhaustion: `printf '%s\n' "${REVIEWER_RETRY_PLAN_MESSAGE}"` (the slot
    retryable-failure limit or recovery-exhausted line, lines 4441–4447,
    4698) and `failed`;
  - attempts exhausted: `Reviewer ${model} failed after ${reviewer_max_attempts} attempts.`
    (lines 4728–4730) and `failed`;
  - non-retryable error: `Reviewer ${model} failed after non-retryable error on <attempt>.`
    (lines 4710–4712) and `failed` — the case the finding names.
  No later step rewrites a terminally failed slot's output file.
- The same job exports `PREVIOUS_REVIEWS_DIR` through `GITHUB_ENV`, so the
  hand-off step already reads that directory (no workflow change).

## Goals

- A block counts as a failed slot only when, in addition to today's checks,
  the runner-written `${PREVIOUS_REVIEWS_DIR}/review_<slug>.txt` exists and its
  content is exactly the block's line.
- A missing, unreadable, or different runner file (a non-retryable failure, a
  paraphrase, another model's line, extra lines) makes the ledger not clean:
  the round is handed off with a warning naming the slot.
- No change for ledgers without a failed block, for the minimum
  (`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS`), or for the log key and hand-off text.

## Non-goals

- Changing the reviewer runner, the summariser, or the status-file values (AD-1).
- Cross-checking clean blocks against the runner output (AD-3).
- The #4835 project's other stages.

## Constraints

- §1: security first; every new mismatch path fails closed.
- §5: one check in the existing loop, plus the minimal plumbing to carry the
  block's line out of awk.
- §6: no identifier is renamed; the new locals
  (`claude_fixer_block_line`, `claude_fixer_block_rest`,
  `claude_fixer_block_runner_line`) collide with nothing in the script.
- §9: the step script keeps its 2-space bash style.
- §15: no GitHub API call; the runner file is local.
- §20: one new fragment, `changelog.d/4885-failed-slot-runner-line-check.md` (AD-2).
- §27: `review_autofix.yml` is not edited.

## Approach

1. The awk classifier prints `failed <slug> <line>` for a failed block (the
   line is the block's single line, already regex-checked and bound to the
   slug; a failed slug never contains a space because the regex's model is
   `[^ ]+`). `clean <slug>` lines are unchanged.
2. The duplicate-slug check extracts the slug per kind (`$2` for `failed`,
   everything after the kind for `clean`), so a repeated slug is still caught.
3. The bash loop splits a `failed` record into slug and line. In the
   `failed:failed` case it reads `${PREVIOUS_REVIEWS_DIR}/review_<slug>.txt`
   (the slug already passed `^[A-Za-z0-9_-]+$`) and accepts the slot only when
   the file's content equals the line; otherwise it warns
   (`… failure line does not match the reviewer runner's output …`) and marks
   the failed slots unverified, so the ledger is not clean.

Alternatives: new runner failure-class metadata (a status value or file in
`review_run_reviewers.sh`) — rejected for this fix, the runner already writes
the exact class-bearing line and changing its status values would reach every
reader of `status_review_*.txt` (AD-1).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
issue fixes the scope, and the change is one block of one step script, its
tests, docs, and a changelog fragment.

1. **Phase 1 — bind failed slots to the runner's output line.** Files: see
   Files & Modules. Done when the new tests (non-retryable runner line under a
   forged retry-exhaustion ledger line, missing runner file, extra runner
   lines) fail closed, the existing Claude-fixer tests stay green with runner
   files provided, and the docs describe the extra check. Rollback: revert the
   PR; the #4835 status-file-only check returns.

## Implementation Steps

1. `scripts/review_autofix_step_claude_fixer_handoff.sh`: awk prints the
   failed line; the duplicate check extracts slugs per kind; the loop reads
   `review_<slug>.txt` for failed blocks and requires an exact match; update
   the header's input list and the clean-ledger comment.
2. `tests/test_review_autofix_claude_fixer_mode.py`: `_run_handoff` gains an
   optional `outputs` map written as `review_<slug>.txt`; `_panel` returns the
   runner lines for failed slots; existing failed-slot tests pass them. New
   tests: non-retryable runner line with a retry-exhaustion ledger line → hand
   off with the mismatch warning; missing runner file → hand off; runner file
   with an extra line → hand off; runner line for a different retry-exhaustion
   variant than the ledger → hand off.
3. `README.md` (`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` row) and `agents.md`
   (Claude-fixer paragraph): name the runner output-file match.
4. `changelog.d/4885-failed-slot-runner-line-check.md` (`security`).

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `changelog.d/4885-failed-slot-runner-line-check.md` [new]

## Testing

`python3 -m pytest tests/test_review_autofix_claude_fixer_mode.py tests/test_workflow_file_size_limit.py`
plus the review_autofix contract tests that expand step scripts
(`tests/test_review_autofix_*.py`), `bash -n` and `shellcheck` on the script,
under both mawk and gawk where available.

## Risks

- A same-head resume reuses cached status and output files; a failed slot is
  never reused (only `success` slots are, `reviewer_resume_should_reuse_success_slot`),
  so its output file is the one this run wrote.
- A runner that one day rewords its failure lines makes failed slots stop
  matching: the round hands off, which is the pre-#4835 behaviour (fail closed).

## Rollout

Rides the #4835 project branch and reaches `main` with that project's final
PR #4847, then consumers with the next `@stable` sync. No wrapper or variable
change.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How is a failed slot's failure class proven? — Picked: A — require the runner-written `review_<slug>.txt` to equal the ledger block's line exactly. Alternatives: B — add runner-produced failure-class metadata (new status value or file) in `review_run_reviewers.sh`. Why: the runner already writes the exact class-bearing line for every terminal failure, so A closes the gap with no runner change and no change for other readers of `status_review_*.txt` (§5); the issue lists it first. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the changelog entry go, given #4835 has not reached `main` yet? — Picked: A — a new fragment `changelog.d/4885-failed-slot-runner-line-check.md` under `security`. Alternatives: B — edit `changelog.d/4835-failed-reviewer-slot-missing-vote.md`. Why: §20.B is one fragment per PR and never another PR's file. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Also cross-check clean blocks against the runner output? — Picked: A — no, failed blocks only. Alternatives: B — also require a clean block's runner output to be an empty review. Why: the finding and its recommendation are about failed blocks; clean votes already need a runner `success` status, and reviewer output formats vary (§5). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` reported `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
