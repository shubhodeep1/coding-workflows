# Dispatch the E2E smoke gate's review runs from the default branch, and bind their completion to the trusted reviewed head

Source issue: shubhodeep1/coding-workflows#5520 (https://github.com/shubhodeep1/coding-workflows/issues/5520)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

The E2E smoke job in `.github/workflows/test-and-mark-stable.yml` dispatches the review wrapper (`${REVIEW_WORKFLOW_FILE}`, default `internal-review.yml`) at the smoke PR's head branch in two places: the Phase 3c "Bug B" fallback after the bait commit, and the Phase 4b retry. A writer to that branch can replace the wrapper before either dispatch, and the altered workflow then runs with `${TEST_REPO}`'s inherited secrets and write permissions (security finding `e2e-dispatch-executes-unreviewed-workflow`, high). This plan dispatches both from the default branch and binds each run's completion to the PR head the run actually reviewed. That head comes from a line the default-branch `review_autofix.yml` already logs, not from the dispatch run's own `head_sha`.

## Context

- The issue was opened by `security-audit.yml` during the security pass of #4898's project and names that project's branch as its `Integration branch:` (AD-1). #4898 (AD-6) kept both E2E dispatches at `--ref "${BRANCH}"` and only documented the exposure. That choice is what this finding reverses.
- On the base branch:
  - Phase 3c, step `inject-bait`, lines 1366–1374: `gh workflow run "${REVIEW_WORKFLOW_FILE}" --repo "${TEST_REPO}" --ref "${BRANCH}" -f pr_number=…` (fail-soft).
  - Phase 4b, step `verify-bait-removed`, lines 2414–2423: the same call when no active run can be adopted. Registration then lists `actions/workflows/${REVIEW_WORKFLOW_FILE}/runs?branch=${BRANCH}` and takes the oldest run with `id > baseline`.
  - Phase 4, step `wait-review`, lines 1581–1585: lists `actions/runs?branch=ai/issue-<N>` and pins review runs to `head_sha == PIN_SHA` (initially `BAIT_SHA`), or to runs in flight at `BAIT_CREATED_AT` (leg b). A default-branch dispatch run has the default branch as `head_branch` and its tip as `head_sha`, so neither list nor pin can see it.
- Default-branch dispatch runs carry the PR in their run name: `Internal: AI Review & Autofix [pr:<N>]` (`internal-review.yml`, #4618) and `AI Review [pr:<N>]` (`workflow-templates/ai-review.yml`, #4701). A run name alone proves nothing: GitHub evaluates `run-name` from the workflow file at the dispatched ref, so any branch can name a run for any PR (issue #5094). A PR-named run counts only when its event is `workflow_dispatch`, its `head_branch` is the default branch, and its `path` is the wrapper that sets that name (agents.md, "Every review dispatch runs from the default branch").
- Both wrappers call `review_autofix.yml` pinned to `@main` / `@stable`. Its `codex-agent` job's "Checkout PR head branch" step prints `Captured INITIAL_HEAD_SHA=<40-hex> for stale-base detection.` right after it hard-resets to the PR head (base branch line 3587; present on `main` and on the `stable` tag). In a run dispatched from the default branch, that line comes from reviewed workflow code and names the exact head the reviewers and editor worked on. Phase 4 already reads this job's log (the editor-noop and reviewer-count shortcuts), selecting it by the name pattern `(^| / )codex-agent( \([^)]*\))?$`.
- `.github/workflows/test-and-mark-stable.yml` is 352,831 bytes (CLAUDE.md §27 guard: 480,000). The job's early checkout is `ref: main`, so every sourced `scripts/` file comes from `main` even when the workflow runs from `stable` (`auto-release-stable.yml`). New helpers therefore stay inline in the step bodies (AD-5).

## Goals

- Neither E2E `gh workflow run "${REVIEW_WORKFLOW_FILE}"` call passes `--ref`. Both keep `--repo "${TEST_REPO}"` and `-f pr_number="${PR_NUMBER}"`. `PR_NUMBER` is checked against `^[1-9][0-9]*$` before the Phase 4b dispatch; Phase 3c already fails soft.
- Phase 4 finds default-branch dispatch runs for the smoke PR. It lists `repos/${TEST_REPO}/actions/workflows/${REVIEW_WORKFLOW_FILE}/runs?event=workflow_dispatch&branch=<default>&per_page=100` and keeps runs whose `event`, `head_branch`, `path` (`.github/workflows/${REVIEW_WORKFLOW_FILE}`) and `display_title` (the exact PR name for that wrapper) all match.
- Phase 4 accepts a completed dispatch run only when its trusted reviewed head equals `PIN_SHA`. A matched run in flight that was created at or after `BAIT_CREATED_AT` is tracked for activity but can never be accepted until it completes. A completed run whose reviewed head differs, or has none, is never accepted. The pinned branch filter and its tier order stay byte-for-byte unchanged (AD-4).
- Phase 4b dispatches from the default branch, adopts an active matched dispatch run created at or after `BAIT_CREATED_AT` when no branch run can be adopted, and registers the oldest matched dispatch run with `id > baseline`. A dispatched retry run that completes with `success` but logged no reviewed head fails the step as `retry_workflow_failed`. The reviewed head is always logged.
- Each reviewed-head lookup costs 2 REST calls (jobs, log), is cached per run id, and is only made for completed runs. A lookup that fails is retried on the next poll and never counted as a match.
- A wrapper whose dispatch runs carry no PR name (a custom `review_workflow_file`) is never matched (AD-3). Phase 4 then relies on the `pull_request: synchronize` run. Phase 4b fails before dispatching with `retry_dispatch_failed` and an error naming the reason.
- Every correlation decision logs one structured line: `E2E_REVIEW_DISPATCH_CORRELATION`.
- Tests cover the dispatch sites, the run filter, the reviewed-head parser, the Phase 4 preference, and the Phase 4b registration. `changelog.d/5520-e2e-dispatch-default-branch.md` carries a `security` entry, and agents.md replaces the "E2E dispatches keep `--ref`" sentence.

## Non-goals

- No change to `review_autofix.yml`, `internal-review.yml`, or `workflow-templates/ai-review.yml`: the reviewed head is read from an existing log line (AD-2).
- No change to the `pull_request: synchronize` runs GitHub starts when the bait commit lands. GitHub runs the branch's own copy of the wrapper for those, and the E2E job does not dispatch them (AD-7).
- No change to Phase 6's poller dispatch (`--ref "${POLLER_DISPATCH_REF}"`). It dispatches the orchestrator poller, not the review wrapper, and is outside the finding.
- No change to the Phase 4 wait budgets, the retry budget, or the status values the aggregator reads.

## Constraints

- §1: security first. A run counts only through the listing filters above, never by name alone (#5094).
- §5: minimal change. The pinned Phase 4 filter, its tier order, and the legacy branch adoption in Phase 4b are untouched.
- §6: no identifier is renamed. The Phase 3c log message keeps its `(Bug B fallback)` suffix. New shell names (`E2E_REVIEW_DISPATCH_*`, `e2e_review_dispatch_*`) are checked to be unused. The `Captured INITIAL_HEAD_SHA=` line becomes a contract other code reads, so a test pins it.
- §8: one structured `E2E_REVIEW_DISPATCH_CORRELATION` line per decision.
- §9: YAML stays 2-space. The inline shell matches the step's existing space indentation.
- §15: audited calls. The branch listing cannot hold a default-branch run, and a repo-wide listing cannot be bounded to one page, so Phase 4 adds one workflow-scoped listing per poll (only while a bait is pinned and the wrapper has a PR name). It adds one `GET repos/${TEST_REPO}` per step for the default branch, and 2 cached calls per completed candidate run.
- §20: security fix, so the changelog fragment is required.
- §27: `test-and-mark-stable.yml` grows by about 8 KB and stays far below 480,000 bytes.

## Approach

1. **Dispatch.** Drop `--ref "${BRANCH}"` from both calls. `gh workflow run` then dispatches at `${TEST_REPO}`'s default branch, the protected copy of the wrapper (AD-2 of #4898 made the same choice).
2. **Find.** Each of the two steps defines the same inline helpers:
   - `e2e_review_dispatch_title <pr>` prints the PR run name for `${REVIEW_WORKFLOW_FILE}`: `internal-review.yml` → `Internal: AI Review & Autofix [pr:<pr>]`, `ai-review.yml` → `AI Review [pr:<pr>]`, anything else → empty.
   - `e2e_review_dispatch_default_branch` resolves `${TEST_REPO}`'s default branch with one call and caches it.
   - `e2e_review_dispatch_runs <pr>` makes the workflow-scoped listing above and prints the matched runs as a JSON array. It fails when the listing fails, and prints `[]` when the title or default branch is unknown.
3. **Correlate.** `e2e_review_dispatch_reviewed_head <run id>` reads the run's jobs, picks the `codex-agent` job with the Phase 4 name pattern, fetches its log to a temp file, and takes the first match of `Captured INITIAL_HEAD_SHA=([0-9a-f]{40}) for stale-base detection\.`. The step echoes its script source unexpanded (`${INITIAL_HEAD_SHA}`), which never matches. It prints the SHA, prints nothing when there is no such job or line, and returns 1 on an API failure. The answer is cached in `E2E_REVIEW_DISPATCH_HEADS[<run id>]`.
4. **Phase 4.** After the existing `REVIEW_RUN` selection, when `PIN_SHA` is set:
   - the newest completed, non-cancelled matched run whose reviewed head equals `PIN_SHA` replaces `REVIEW_RUN` unless the branch selection is itself completed and not cancelled (the existing tier-1 rule: a finished run beats an active sibling);
   - otherwise the oldest active matched run created at or after `BAIT_CREATED_AT` replaces `REVIEW_RUN` only when the branch selection is empty or cancelled;
   - everything after (status handling, job probes, pin advance) runs unchanged on whichever run was picked.
5. **Phase 4b.** Discovery also makes the dispatch listing. The baseline is the highest run id across both listings. Adoption is tried in order: the existing branch rule, then the oldest active matched dispatch run created at or after `BAIT_CREATED_AT`. The dispatch has no `--ref`. Registration re-reads the dispatch listing and takes the oldest run with `id > baseline` other than the prior run. When the run it polled came from the dispatch listing, the step reads the reviewed head after the conclusion check.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — default-branch E2E review dispatches with reviewed-head correlation.**
   - Files: `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/ci.yml`, `tests/test_test_and_mark_stable_e2e_dispatch_default_branch.py` [new], `tests/test_test_and_mark_stable_review_blocked_budget.py`, `agents.md`, `changelog.d/5520-e2e-dispatch-default-branch.md` [new].
   - Done when: both dispatches have no `--ref`, Phase 4 and Phase 4b correlate dispatch runs as in the Goals, the new and updated tests pass locally, every existing `test_test_and_mark_stable_*` test and `tests/test_workflow_file_size_limit.py` pass, and `actionlint` / YAML parsing of the workflow is clean.
   - Rollback: revert the phase PR. The dispatches return to the branch ref, and nothing else depends on the new helpers.

## Implementation Steps

1. `test-and-mark-stable.yml` Phase 3c (`inject-bait`): remove `--ref "${BRANCH}"`, and update the #4898 exposure comment and the success echo to say the dispatch runs from the default branch.
2. Phase 4 (`wait-review`): define the helpers and `declare -A E2E_REVIEW_DISPATCH_HEADS=()` after sourcing `comprehensive_test_and_release_gh_api.sh` (API function: `gh_api_safe_quiet_print`), and resolve the default branch once. Add the selection block after the `REVIEW_RUN` if/else, before the rate-limit guard, and update the comment that says the dispatch pins `head_sha` to `BAIT_SHA`.
3. Phase 4b (`verify-bait-removed`): add `BAIT_CREATED_AT` to `env:` and define the same helpers (API function: `gh_api_with_retry`). Extend discovery, the baseline, adoption, dispatch, registration, and the completion check as in the Approach, keep every existing status value, and update the header comment (lines 2025–2036) and the #4898 dispatch comment.
4. Tests: add `tests/test_test_and_mark_stable_e2e_dispatch_default_branch.py`. It executes the helpers extracted from the workflow text with stubbed API functions (reviewed-head parsing, provenance filter, cache, Phase 4 preference) and asserts the static contracts: no `--ref` at either site, both helper copies identical, the `review_autofix.yml` log line pinned. Update the Phase 4b assertions in `tests/test_test_and_mark_stable_review_blocked_budget.py` and add a `ci.yml` step for the new file.
5. Docs: rewrite the last sentence of agents.md's #4898 bullet (the E2E exposure) to describe the default-branch dispatch and the correlation, and add the changelog fragment.

## Files & Modules

- `.github/workflows/test-and-mark-stable.yml`
- `.github/workflows/ci.yml`
- `tests/test_test_and_mark_stable_e2e_dispatch_default_branch.py` [new]
- `tests/test_test_and_mark_stable_review_blocked_budget.py`
- `agents.md`
- `changelog.d/5520-e2e-dispatch-default-branch.md` [new]
- `docs/plans/issue-5520-e2e-dispatch-default-branch-plan.md` [new], `docs/implement-plan/issue-5520-e2e-dispatch-default-branch.md` [new]

## Tests

- Unit (bash under pytest, stubbed API): the reviewed-head parser takes the first expanded line and ignores the unexpanded script echo, a line with a short SHA, and a missing `codex-agent` job. It returns 1 on a failed call and caches. The run filter drops a wrong `path`, a wrong `head_branch`, a non-dispatch event, and another PR's name. The Phase 4 block prefers a completed correlated run over an active branch run, keeps a completed branch run, never accepts a completed run with a mismatched head, and tracks an active run only when the branch selection is empty.
- Contract: no `--ref` in either `gh workflow run "${REVIEW_WORKFLOW_FILE}"` call, identical helper copies, the updated Phase 4b discovery / adoption / registration text, and `review_autofix.yml` still printing the `Captured INITIAL_HEAD_SHA=` line.
- Existing: every `tests/test_test_and_mark_stable_*.py`, `tests/test_workflow_file_size_limit.py`, `tests/test_retrigger_default_branch_dispatch.py`, `tests/test_log_prefix_regressions.sh`.
- End to end: the next release gate run (`test-and-mark-stable.yml` on `main` after the chain lands) exercises Phase 3c / 4 / 4b. Runtime validation runs against the project branch.

## Risks & Mitigations

- The reviewed-head line is renamed or moved in `review_autofix.yml` → a contract test pins it, and a missing line never counts as a match, so Phase 4 falls back to the `synchronize` run and the retry fails loudly.
- An earlier step in the `codex-agent` job prints PR-controlled text that forges the line → ACCEPTED: the steps before the checkout print no PR body. The first match wins, and a forgery could only mislead the smoke gate's own correlation. It could not reach secrets, because the run itself is the default branch's.
- One more listing per Phase 4 poll → bounded to polls with a pinned bait and a PR-named wrapper, and the gate's `GH_PAT` budget is 5,000 calls per hour.
- A consumer `TEST_REPO` whose `ai-review.yml` predates the PR run name → its dispatch runs are not matched until its next workflow sync. Phase 4 relies on the `synchronize` run, and a retry fails as `retry_dispatch_failed` with the reason.
- The chain reaches `main` only after #4898's and #4701's projects merge → the change ships with them, and nothing here depends on code outside this repo.

## Rollout

No flag. Merges into #4898's project branch, then reaches `main` with #4898 → #4701 → `main`. The release gate runs it from `main` and, after promotion, from `stable`. Rollback is reverting the phase PR.

## References

- Issue #5520; security tracker #3576; #4898 (AD-6), #4701, #4618, #5094.
- `docs/plans/issue-4898-retrigger-dispatch-default-branch-plan.md`

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which base branch does the project build on? — Picked: A — the issue's `Integration branch:`, `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch`. Alternatives: B — `main`. Why: the security pass that filed the finding runs against that branch, and its checker waits for this issue to close with `ai:merged`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the trusted reviewed head come from? — Picked: A — the existing `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` line in the `codex-agent` job log of the default-branch run. Alternatives: B — a new job or step in `review_autofix.yml` whose name carries the reviewed head; C — no SHA, correlate by run name and creation time only. Why: A is already emitted by reviewed code on `main` and `stable`, and needs no change to the consumer-facing reusable workflow, which is 440,432 bytes against the 480,000 guard. C ignores the issue's recommendation and accepts a run that checked out before the bait. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens with a `review_workflow_file` whose dispatch runs carry no PR name? — Picked: A — never matched: Phase 4 relies on the `synchronize` run, and Phase 4b fails before dispatching with `retry_dispatch_failed`. Alternatives: B — match unnamed dispatch runs by reviewed head alone. Why: both default wrappers carry the name, and B costs 2 calls for every unrelated dispatch run in a busy repo. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How do dispatch runs enter Phase 4's selection? — Picked: A — a separate block after the unchanged branch filter: a correlated completed run beats any branch run that has not finished, and an active run is used only when the branch selection is empty or cancelled. Alternatives: B — merge both listings into one list and run the existing tier filter over it. Why: A leaves the tested filter and tier order byte-for-byte intact (`tests/test_test_and_mark_stable_phantom_review_run_filter.py` extracts it). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Where do the helpers live? — Picked: A — inline in both step bodies, with a test asserting the copies are identical. Alternatives: B — `scripts/comprehensive_test_and_release_gh_api.sh`. Why: the job sources `scripts/` from its `ref: main` checkout, so a workflow run from `stable` or a branch could source a copy without the helpers. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What does Phase 4b require of a dispatched retry run's reviewed head? — Picked: A — it must exist, else `retry_workflow_failed`, and it is logged. Alternatives: B — also require it to be `RETRY_DISPATCH_SHA` or a descendant (one extra compare call). Why: the canary pytest right after is the real check. A missing head means the run never reviewed the PR, which would otherwise be blamed on the editor as `spec_mismatch`. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Are the `pull_request: synchronize` runs that the bait push starts in scope? — Picked: A — no, record them as out of scope. Alternatives: B — stop relying on them. Why: GitHub starts them for any push by a writer, the E2E job does not dispatch them, and the finding names the two dispatch sites. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-30] May Phase 4b adopt an active dispatch run instead of dispatching another? — Picked: A — yes, the oldest matched active run created at or after `BAIT_CREATED_AT`. Alternatives: B — never adopt dispatch runs. Why: it mirrors the existing branch adoption, and a duplicate run would only queue behind it in the `pr-autofix` concurrency group. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
