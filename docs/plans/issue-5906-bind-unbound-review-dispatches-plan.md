# Pending-checks auto-merge: bind review dispatches to their PR and count their failures

Source issue: shubhodeep1/coding-workflows#5906 (https://github.com/shubhodeep1/coding-workflows/issues/5906)
Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
Security pass: skip (ai:security: automation-produced issue)

## Summary

The `claude-pr-catch-all` sweep's pending-checks pass (issues #4900, #5148) ignores a *finished* `review_autofix.yml`, `ai-review.yml`, or `review_rb_judge_dispatch.yml` dispatch, because those runs carry no PR binding and are only checked while active. A forced or judge review of the PR that fails after the pending-checks marker therefore never blocks the older marker from enabling auto-merge. This plan gives those three workflows a `[pr:<N>]` run name on `workflow_dispatch` (the binding `internal-review.yml` already has) and makes `check_review_runs()` treat a completed dispatch newer than the marker's run as a review of this PR when its title names the PR, and fail closed when its title names no PR and it did not succeed.

## Context

- Security audit finding `failed-unbound-review-dispatch-ignored` (high, confidence 8/10, `A01:2021-Broken Access Control`) at `scripts/claude_fixer_pending_checks.py:493`, filed by `.github/workflows/security-audit.yml` against the #4900 project branch (tracker #3576).
- `scripts/claude_fixer_pending_checks.py` `check_review_runs()` (lines 434-508): for each `UNBOUND_DISPATCH_REVIEW_WORKFLOWS` entry it reads the workflow_dispatch runs listing and only adds runs that are not `completed` to `active` (line 494). Completed ones never reach `bound_reviews` (line 499), so their conclusion is ignored.
- Binding precedent: `.github/workflows/internal-review.yml:8` sets `run-name: "${{ github.event_name == 'workflow_dispatch' && format('Internal: AI Review & Autofix [pr:{0}]', inputs.pr_number) || '' }}"`, and `.claude/scripts/check_in_status.py` binds such runs by `DISPATCHED_REVIEW_TITLE`.
- The three unbound workflows all take a required `pr_number` input on `workflow_dispatch`: `.github/workflows/review_autofix.yml:82` (name `Codex PR Self-Healing Semantic Agent`), `workflow-templates/ai-review.yml:13` (name `AI Review`, the consumer wrapper), `.github/workflows/review_rb_judge_dispatch.yml:25` (name `Internal: Review-Blocked Judge Dispatch`) and `workflow-templates/review_rb_judge_dispatch.yml:30` (name `AI Review-Blocked Judge Dispatch`). None has a `run-name`, so their runs' `display_title` is the workflow name.
- Other readers of these runs match on the workflow `name` / `workflowName` (`scripts/orchestrate_poll_process.sh`), not `display_title`, and `review_autofix_sweep.yml`'s stale-queue dedupe only parses `internal-review.yml` titles, so a run name changes nothing else. `review_autofix.yml`'s run name only applies to its own `workflow_dispatch` runs; a `workflow_call` run takes the caller's.
- Sibling findings filed with this one, out of scope here: #5904 (`failed-review-masked-by-skipped-run`: a gate-skipped successful dispatch newer than a failed review clears it under the "latest newer review" rule) and #5905 (base retarget race at merge time).

## Goals

- G1. Every `workflow_dispatch` run of `review_autofix.yml`, `ai-review.yml` (template), and `review_rb_judge_dispatch.yml` (both copies) is titled `<workflow name> [pr:<pr_number>]`.
- G2. In `check_review_runs()`, a completed dispatch of those workflows newer than the marker's run whose title ends in `[pr:<this PR>]` counts as a review of this PR, exactly like a titled `internal-review.yml` dispatch: when it is the latest newer review and did not conclude `success`, `evaluate()` returns `review_superseded`.
- G3. A completed dispatch of those workflows newer than the marker's run whose title carries no `[pr:<N>]` binding (a consumer wrapper before the `@stable` sync, or a dispatch from a ref without the run name) and did not conclude `success` returns `review_superseded` (fail closed). One titled for another PR is ignored.
- G4. Active dispatches keep blocking exactly as today (`review_active`), and the read budget is unchanged (no new API calls).
- G5. Tests reproduce the audit's scenario and every new path; a wiring test pins the run name in all four workflow files against the module's parser.
- G6. Docs (`README.md`, `agents.md`, `docs/INVENTORY.md`) and a `security` changelog fragment.

## Non-goals

- Any `.claude/**` change (`check_in_status.py` constants are reused read-only).
- The "latest newer review" rule itself and gate-skipped dispatches (#5904), and merge-time base revalidation (#5905).
- Re-triggering a review when the marker is blocked; the gate and the review sweep are unchanged.
- Narrowing which active dispatches block (AD-4).

## Constraints

- §1 / §3: fail closed on a run that cannot be bound.
- §5: the change is `check_review_runs()` plus one `run-name` line per workflow file.
- §6: no renames; `UNBOUND_DISPATCH_REVIEW_WORKFLOWS` keeps its name (its comment is updated). New identifiers `DISPATCH_TITLE_PR_RE` and `_dispatch_title_pr` were checked with a repo-wide grep and are unused.
- §9: tabs in Python; YAML stays 2-space.
- §14 / sync: `workflow-templates/ai-review.yml` and `workflow-templates/review_rb_judge_dispatch.yml` reach consumers on the next `@stable` sync; until then their dispatches stay untitled and fall under G3.
- §15: no new reads; the titles come from the listings `check_review_runs()` already reads.
- §18: no new script; runs inside the existing hourly `claude-pr-catch-all` job.
- §19: every PR uses `Refs #5906`; the final PR targets the #4900 project branch, so the final-merge stage closes the issue explicitly.
- §20: a `security` fragment.
- §27: `review_autofix.yml` is 460,568 bytes; one line keeps it under 480,000.

## Approach

1. Add `run-name: "${{ github.event_name == 'workflow_dispatch' && format('<workflow name> [pr:{0}]', inputs.pr_number) || '' }}"` to the four workflow files (the `internal-review.yml` pattern; an empty run name keeps the default title for other events).
2. In `scripts/claude_fixer_pending_checks.py`, add `DISPATCH_TITLE_PR_RE = re.compile(r"^.+ \[pr:([1-9][0-9]*)\]$")` and `_dispatch_title_pr(run) -> int | None`. In the unbound-workflow loop of `check_review_runs()`, for each completed run with an id above `marker_run_id`: title bound to this PR → append to a `titled_dispatches` list that joins `bound_reviews`; no binding and conclusion not `success` → append to `unbindable_failures`; bound to another PR → ignore. After the active check and the existing latest-newer test, a non-empty `unbindable_failures` returns `review_superseded` naming the runs.
3. Update the module docstring's fail-closed list and `check_review_runs()`'s docstring Output section.

Alternatives: reading each dispatch run's jobs or logs to find its PR costs calls per run (§15); blocking on every newer unsuccessful dispatch without a binding blocks every PR in the repo for good (AD-1).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — bind review dispatches to their PR and count their failures.** Files: see [Files & Modules](#files--modules). Done when the four workflow files carry the run name, `evaluate()` returns `review_superseded` without a merge call for a newer failed dispatch titled for the PR or carrying no PR binding, merges for one titled for another PR or older than the marker, the existing #4900 / #5147 / #5148 tests still pass (updated where they pinned the old fail-open case), the docs and changelog fragment are updated, and the repo's CI passes. Rollback: revert the PR; the pass then ignores finished dispatches again.

## Implementation Steps

Phase 1:
1. `.github/workflows/review_autofix.yml`, `workflow-templates/ai-review.yml`, `.github/workflows/review_rb_judge_dispatch.yml`, `workflow-templates/review_rb_judge_dispatch.yml`: add the `run-name` line after `name:`.
2. `scripts/claude_fixer_pending_checks.py`: `DISPATCH_TITLE_PR_RE`, `_dispatch_title_pr`, the loop change in `check_review_runs()`, docstrings.
3. `tests/test_claude_fixer_pending_checks.py`: move the two "finished unbound dispatch failed" cases from the settled list to the superseded list, add titled / other-PR / older / successful cases and the wiring test.
4. `README.md`, `agents.md`, `docs/INVENTORY.md`: one sentence each.
5. `changelog.d/5906-bind-review-dispatches-to-pr.md` (`<!-- changelog: security -->`).

## Files & Modules

- `scripts/claude_fixer_pending_checks.py`
- `.github/workflows/review_autofix.yml`, `.github/workflows/review_rb_judge_dispatch.yml`
- `workflow-templates/ai-review.yml`, `workflow-templates/review_rb_judge_dispatch.yml`
- `tests/test_claude_fixer_pending_checks.py`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/5906-bind-review-dispatches-to-pr.md` [new]

## Tests

- Audit scenario: marker live, checks green, a newer `review_autofix.yml` dispatch titled `[pr:42]` concluded `failure` → `review_superseded`, no merge call. The same for `ai-review.yml` and `review_rb_judge_dispatch.yml`.
- A newer untitled (legacy) dispatch that failed, was cancelled, or timed out → `review_superseded`.
- Still merges: a newer titled dispatch for this PR that succeeded; a failed one titled for another PR; an untitled one that succeeded; an untitled failed one older than the marker's run.
- A newer titled failed dispatch followed by a newer successful bound review → merges (the existing latest-newer rule, #5904's scope; pinned so the interaction is visible).
- Wiring: each of the four workflow files has exactly one `run-name` whose `format()` title, filled with a PR number, parses to that number with `DISPATCH_TITLE_PR_RE`, and names the file's own `name:`.
- Existing suites: `tests/test_claude_fixer_pending_checks.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_workflow_file_size_limit.py`, `tests/test_update_workflows_guardrails.py`.

## Risks & Mitigations

- A consumer whose wrapper predates the sync has untitled dispatches; one that fails blocks every pending-checks merge there whose marker is older, until a push or forced review posts a fresh marker. ACCEPTED: fail closed (§1); the daily sync titles new runs, and the PR still merges through a new review round.
- A blocked marker stays blocked while the gate keeps skipping dispatched re-runs on its head (unchanged behaviour shared with #5148's `review_superseded`). ACCEPTED: a push or the `force-review` label re-reviews it; changing the gate is #5904's territory.
- A run name forged by someone who can push a workflow file could bind a run to another PR. ACCEPTED: that actor already controls the workflows.

## Rollout

Ships when the #4900 project's final PR merges into `main` (the sweep runs from `main`); consumers get the wrapper run names on the next `@stable` sync. Kill switches unchanged: `CLAUDE_FIXER_ENABLED=false` and `ENABLE_AUTO_MERGE=false`. Rollback: revert.

## Auto-decisions

- AD-1 [plan, 2026-10-01] How are the unbound review dispatches bound to a PR? — Picked: A — a `run-name` of `<workflow name> [pr:<pr_number>]` on `workflow_dispatch` in `review_autofix.yml`, `ai-review.yml`, and both `review_rb_judge_dispatch.yml` copies, read from the listing's `display_title`. Alternatives: B — read each run's jobs or logs to find its PR (calls per run, §15); C — no binding, block on every newer unsuccessful dispatch (blocks every PR in the repo until its next review). Why: the `internal-review.yml` precedent, no new API calls. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What does a newer completed dispatch with no `[pr:<N>]` title do? — Picked: A — fail closed: if it did not conclude `success`, `review_superseded`. Alternatives: B — ignore it (today's behaviour, the finding). Why: §1; such runs only come from wrappers before the sync or refs without the run name. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How strictly is the title matched? — Picked: A — any title ending in ` [pr:<N>]` (`^.+ \[pr:([1-9][0-9]*)\]$`) in these workflows' listings. Alternatives: B — an exact title per workflow name. Why: the listing already fixes the workflow; a suffix survives a renamed workflow, and only a workflow author controls the run name. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Do active dispatches titled for another PR stop blocking? — Picked: A — no, every active dispatch of these workflows still blocks (`review_active`). Alternatives: B — block only on ones titled for this PR or untitled. Why: §5 (no relaxation in a security fix); the delay is one sweep tick. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] Also change the "latest newer review" rule so a later gate-skipped success cannot clear the failure? — Picked: A — no, that is #5904; titled dispatches join the bound set, so #5904's fix covers them too. Alternatives: B — fix it here. Why: one issue per project (`/implement-issue-claude` rules). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` verified the skip: `ai:security: created and labelled by the issue automation`.

## References

- Issue #5906; sibling findings #5904, #5905; security tracker #3576; issues #4900, #5147, #5148 and their plans in `docs/completed/`.
