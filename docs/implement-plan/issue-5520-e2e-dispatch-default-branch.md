# Implement-Plan Log — Dispatch the E2E smoke gate's review runs from the default branch, and bind their completion to the trusted reviewed head

- Plan: docs/plans/issue-5520-e2e-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#5520 (https://github.com/shubhodeep1/coding-workflows/issues/5520)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5520-e2e-dispatch-default-branch   Final PR: #5550 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (opened after this commit; its number is in the stage report and the checker instructions)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified locally (22 new tests, 548 tests over every module that references test-and-mark-stable.yml, actionlint, yamllint, shellcheck without new warnings); phase PR opened

## Phases
1. [ ] Phase 1 — default-branch E2E review dispatches with reviewed-head correlation   — PR open (waiting); review rounds: 0; interventions: 0
   - [x] Phase 3c and Phase 4b `gh workflow run "${REVIEW_WORKFLOW_FILE}"` calls carry no `--ref`
   - [x] Phase 4 matches default-branch dispatch runs (workflow-scoped listing, provenance, exact PR run name) and accepts a completed one only when its trusted reviewed head equals `PIN_SHA`
   - [x] Phase 4b adopts or registers matched dispatch runs and requires a reviewed head on a dispatched retry
   - [x] tests (new `tests/test_test_and_mark_stable_e2e_dispatch_default_branch.py`, updated Phase 4b contract), `ci.yml` step, agents.md, `changelog.d/5520-e2e-dispatch-default-branch.md`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which base branch does the project build on? — Picked: A — the issue's `Integration branch:`, `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch`. Alternatives: B — `main`. Why: the security pass that filed the finding runs against that branch, and its checker waits for this issue to close with `ai:merged`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the trusted reviewed head come from? — Picked: A — the existing `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` line in the `codex-agent` job log of the default-branch run. Alternatives: B — a new job or step in `review_autofix.yml` whose name carries the reviewed head; C — no SHA, correlate by run name and creation time only. Why: A is already emitted by reviewed code on `main` and `stable`, and needs no change to the consumer-facing reusable workflow, which is 440,432 bytes against the 480,000 guard. C ignores the issue's recommendation and accepts a run that checked out before the bait. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens with a `review_workflow_file` whose dispatch runs carry no PR name? — Picked: A — never matched: Phase 4 relies on the `synchronize` run, and Phase 4b fails before dispatching with `retry_dispatch_failed`. Alternatives: B — match unnamed dispatch runs by reviewed head alone. Why: both default wrappers carry the name, and B costs 2 calls for every unrelated dispatch run in a busy repo. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How do dispatch runs enter Phase 4's selection? — Picked: A — a separate block after the unchanged branch filter: a correlated completed run beats any branch run that has not finished, and an active run is used only when the branch selection is empty or cancelled. Alternatives: B — merge both listings into one list and run the existing tier filter over it. Why: A leaves the tested filter and tier order byte-for-byte intact (`tests/test_test_and_mark_stable_phantom_review_run_filter.py` extracts it). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Where do the helpers live? — Picked: A — inline in both step bodies, with a test asserting the copies are identical. Alternatives: B — `scripts/comprehensive_test_and_release_gh_api.sh`. Why: the job sources `scripts/` from its `ref: main` checkout, so a workflow run from `stable` or a branch could source a copy without the helpers. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What does Phase 4b require of a dispatched retry run's reviewed head? — Picked: A — it must exist, else `retry_workflow_failed`, and it is logged. Alternatives: B — also require it to be `RETRY_DISPATCH_SHA` or a descendant (one extra compare call). Why: the canary pytest right after is the real check. A missing head means the run never reviewed the PR, which would otherwise be blamed on the editor as `spec_mismatch`. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Are the `pull_request: synchronize` runs that the bait push starts in scope? — Picked: A — no, record them as out of scope. Alternatives: B — stop relying on them. Why: GitHub starts them for any push by a writer, the E2E job does not dispatch them, and the finding names the two dispatch sites. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-30] May Phase 4b adopt an active dispatch run instead of dispatching another? — Picked: A — yes, the oldest matched active run created at or after `BAIT_CREATED_AT`. Alternatives: B — never adopt dispatch runs. Why: it mirrors the existing branch adoption, and a duplicate run would only queue behind it in the `pr-autofix` concurrency group. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] When a shell step passes a GitHub run listing to jq, feed it on stdin, never through `--argjson`: a 100-run page can be several hundred KB, above Linux's 128 KB limit for one argument, and the step then fails with E2BIG. (files: .github/workflows/test-and-mark-stable.yml)
- [source:plan-deviation] A workflow step that adds a second source of candidate runs must also gate the loop's early-accept shortcuts (failed steps, log markers) on the new source, or a run whose correlation is only checked at completion can be accepted while it is still in flight. (files: .github/workflows/test-and-mark-stable.yml)

## Notes
- Issue mode: plan written by /implement-issue-claude for #5520; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned `skip: true` (`ai:security: created and labelled by the issue automation`), so step 9 is skipped.
- Issue base `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch` has not merged (its final PR #4923 is a draft into #4701's project branch).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5520#issuecomment-5906530022 (id 5906530022).
- Stale Routine sweep (2026-09-30): 8 ended Routines deleted.
- Phase 1: the plan's Goals did not name the Phase 4 in-progress shortcuts (the failed-step and editor-no-op early accepts). They are gated on `E2E_REVIEW_DISPATCH_WATCHING`, so a watched dispatch run is accepted only at completion with a matching reviewed head, as the Goals require. The `review_workflow_file` input description now says which wrappers can be matched. Phase 6's poller dispatch (`--ref "${POLLER_DISPATCH_REF}"`) is unchanged, per the plan's non-goals.
- Local test environment: `pytest`, `pyyaml`, `yamllint`, `actionlint-py`, `shellcheck-py` installed with pip, and `gawk` with apt (`tests/test_implement_post_codex_recovery.py` needs it; it failed with `gawk: command not found` before, unrelated to this change).
