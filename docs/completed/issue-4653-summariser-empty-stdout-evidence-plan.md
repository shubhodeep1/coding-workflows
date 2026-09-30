# Name the summariser's empty-stdout failure in reviewer heal evidence

Source issue: shubhodeep1/coding-workflows#4653 (https://github.com/shubhodeep1/coding-workflows/issues/4653)
Base branch: main (was ai/issue-4605, whose PR #4607 closed unmerged on 2026-09-27; rebuilt on main 2026-09-28 per the operator's Q1: D answer on #4653)
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

When the reviewer consensus summariser fails because OpenCode exits 0 with no final message, the heal evidence records only `reviewers_failed=true` and `dominant_rc=unknown`, so the heal diagnosis is inconclusive. This plan makes `reviewer-failure-evidence` recognise that diagnostic and uploads the summariser logs with the failure-log artifact, so the next occurrence can be diagnosed from its evidence.

## Context

- Run 36317817104 (`Internal: AI Review & Autofix` on PR #4607, head `4399da1`) failed with `reviewers_failed`. All six reviewer slots succeeded. The job log shows `summariser (pass1): attempt N produced empty stdout (OpenCode returned 0 but emitted no final message).` for all 10 attempts (about 7 s each, `openai/gpt-6-luna`, medium reasoning), then `##[error]summariser (pass1): all 10 attempts failed (last rc=0)`.
- `scripts/summarize_reviewer_consensus.sh` (retry loop, around lines 320–405) writes that empty-stdout line, each attempt's stderr tail, and the per-attempt `exited rc=N` lines to `${RUNTIME_DIR}/summariser_<prefix>.log` (prefixes `pass1` and `review`, from `scripts/review_run_reviewers.sh:4953-5067`). The `all N attempts failed` line goes to stderr only.
- `.github/workflows/review_autofix.yml` (`Post editor summary comment`, around line 4986) passes `PREVIOUS_REVIEWS_DIR/*.log` and `RUNTIME_DIR/summariser_*.log` to `workflow_failure_heal.py reviewer-failure-evidence`.
- `scripts/workflow_failure_heal.py::reviewer_failure_evidence` (line 1122) only knows `attempt N exited rc=N` and `all N attempts failed (last rc=N)` (`_SUMMARISER_EXIT_RE`, line 1114). The empty-stdout line matches neither, and the self-named-script helper pattern needs an underscore in the name, so the evidence for this run was `reviewers_failed=true` alone.
- The failure-log staging step of `review_autofix.yml` (around lines 7236–7334) copies a fixed list of `RUNTIME_DIR` files. `summariser_*.log` is not in it, so the stderr tails the issue asks to inspect were never uploaded (the run's artifact `codex-review-autofix-failure-logs-36317817104-1` has no summariser log).
- The summariser works on other PRs (run 36321174690 at 13:05 UTC succeeded on attempt 1), so the underlying OpenCode behaviour on PR #4607 is not identified yet.

## Goals

- `reviewer-failure-evidence` on a summariser log whose attempts produced empty stdout emits `summariser_exit rc=0`, `summariser_empty_stdout prefix=<prefix>`, and `dominant_rc=0` (instead of only `reviewers_failed=true`).
- The evidence stays stable across attempt counts (no attempt numbers, timestamps, or run ids), so the fingerprint repeats for repeated failures.
- A later `attempt N exited rc=M` line still wins for `summariser_exit` (the last attempt's rc, matching the script's `last_rc`).
- The failure-log artifact carries `summariser_pass1.log` and `summariser_review.log` when they exist.
- The summariser's nonempty-output requirement and fail-closed exit stay unchanged.

## Non-goals

- Changing the summariser model, retries, backoff, or OpenCode invocation. The issue forbids a retry or timeout fix, and the cause of the empty output is not known until the uploaded stderr tails show it.
- Changing `_REVIEWER_SLOT_EXIT_RE`, the fingerprint algorithm, or the heal intake.

## Constraints

- §5 minimal change set; §6 no identifier renamed or removed (`summariser_exit`, `dominant_rc`, `reviewer-failure-evidence` keep their meaning; the new `summariser_empty_stdout` key is additive and unique in `workflow_failure_heal.py`).
- §8 diagnostics first: the fix adds evidence and log capture, not a speculative behaviour change.
- §9 tabs in Python and shell; YAML stays 2-space.
- §27: `review_autofix.yml` is 451,394 bytes; the change adds under 100 bytes, well under the 480,000-byte guard.
- §20: changelog fragment `changelog.d/4653-summariser-empty-stdout-evidence.md`.
- Issue fix constraints: no retry/timeout fix; regression test fails on the reported head and passes with the fix.

## Approach

Add `_SUMMARISER_EMPTY_STDOUT_RE` next to `_SUMMARISER_EXIT_RE`. In `reviewer_failure_evidence`, a match sets the summariser code to `0` (last line wins, as for `exited rc=N`) and records the prefix; after the `summariser_exit` line, emit one `summariser_empty_stdout prefix=<prefix>` line per distinct prefix, sorted. Add `summariser_pass1.log` and `summariser_review.log` to the staged `RUNTIME_DIR` file list of the failure-log step. Update the README bullet that lists what the evidence contains.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes one defect, and the evidence change, its artifact capture, and its test are one reviewable unit.

1. **Phase 1 — summariser empty-stdout evidence and log upload.**
   - Files: `scripts/workflow_failure_heal.py`, `tests/test_workflow_failure_heal.py`, `.github/workflows/review_autofix.yml`, `tests/test_review_autofix_failure_log_upload.py`, `README.md`, `changelog.d/4653-summariser-empty-stdout-evidence.md`.
   - Done: the new regression test fails against the unmodified `workflow_failure_heal.py` and passes with the fix; the full `tests/test_workflow_failure_heal.py` and `tests/test_review_autofix_failure_log_upload.py` pass; `actionlint` (if available) and the workflow size test pass.
   - Rollback: revert the phase PR; the evidence falls back to the previous lines and the artifact to the previous file list.

## Implementation Steps

1. `scripts/workflow_failure_heal.py` ~line 1114: add `_SUMMARISER_EMPTY_STDOUT_RE = re.compile(r"summariser \((?P<prefix>[^)]*)\): attempt [0-9]+ produced empty stdout\b")`.
2. Same file, `reviewer_failure_evidence`: on a match set `summariser_code = "0"`, add the prefix to a set, `continue`; emit `summariser_empty_stdout prefix=<prefix>` lines after `summariser_exit`; extend the docstring.
3. `.github/workflows/review_autofix.yml` failure-log staging list: add `summariser_pass1.log summariser_review.log`, with a comment naming issue #4653.
4. `tests/test_workflow_failure_heal.py`: add a regression test with a summariser log of repeated exit-0, empty-stdout attempts (stderr-tail blocks included), asserting the new lines, stability across attempt counts, and that a later `exited rc=` line wins.
5. `tests/test_review_autofix_failure_log_upload.py`: assert the staging step copies both summariser logs.
6. `README.md` "Reviewer failures name the failing phase" bullet: mention the empty-stdout line and the uploaded summariser logs.
7. `changelog.d/4653-summariser-empty-stdout-evidence.md` (`fixed`).

## Files & Modules

- `scripts/workflow_failure_heal.py`
- `tests/test_workflow_failure_heal.py`
- `.github/workflows/review_autofix.yml`
- `tests/test_review_autofix_failure_log_upload.py`
- `README.md`
- `changelog.d/4653-summariser-empty-stdout-evidence.md` [new]

## Tests

- Unit: new `test_reviewer_failure_evidence_names_summariser_empty_stdout` in `tests/test_workflow_failure_heal.py`, run against the base-branch `workflow_failure_heal.py` (must fail) and the fixed one (must pass).
- Contract: extended assertion in `tests/test_review_autofix_failure_log_upload.py`.
- Regression: full `tests/test_workflow_failure_heal.py`, `tests/test_review_autofix_failure_log_upload.py`, and `tests/test_workflow_file_size_limit.py`.

## Risks & Mitigations

- The fingerprint of this failure class changes (evidence gains lines), so a recurrence starts a new fingerprint lineage. ACCEPTED — the heal intake also groups review/autofix failures by source pull request, and a diagnosable fingerprint is the point of the fix.
- Summariser logs may hold model stderr. Mitigation: the artifact is already private to the repo with 7-day retention and holds the reviewer `.log` files of the same kind.

## Rollout

Ships to the default branch with the final PR, then to consumers on the next `@stable` release. No flags, no migration. (Originally planned to ride `ai/issue-4605` → `orchestrator/project-4139`; that base closed unmerged, see AD-1.)

## Auto-decisions

- AD-1 [plan, 2026-09-27] Which branch should the fix build on? — Picked: A — `ai/issue-4605`, the issue's `Target branch:` (the failing PR #4607's head). Alternatives: B — the default branch. Why: `/implement-issue-claude` step 3 builds on the branch the issue names; the affected files are identical on both branches. Applied in: no code change. Status: changed to B (2026-09-28, operator answer Q1: D on #4653)
- AD-2 [plan, 2026-09-27] How far should the fix go? — Picked: A — recognise the empty-stdout line in the evidence and upload the summariser logs with the failure-log artifact. Alternatives: B — evidence parser only; C — also change the summariser's model or retry behaviour. Why: the issue asks to inspect the uploaded summariser log, which the artifact never carried, and forbids a retry fix; C guesses at an unidentified cause (§8). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What should the evidence say for an empty-stdout attempt? — Picked: A — `summariser_exit rc=0` plus one `summariser_empty_stdout prefix=<prefix>` line, no attempt counts. Alternatives: B — copy the raw diagnostic line with its attempt number; C — only `summariser_exit rc=0`. Why: A keeps the fingerprint stable and names the failure mode; B changes the fingerprint per attempt count; C loses the distinction from a clean exit. Applied in: phase 1 PR. Status: pending review

## References

- Issue #4653; source PR #4607; failed run https://github.com/shubhodeep1/coding-workflows/actions/runs/36317817104
- PR #4323 (reviewer failure evidence introduced)
