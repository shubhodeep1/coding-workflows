# Dispatch the E2E smoke gate's review runs from the default branch, and correlate them with the bait commit

Source issue: shubhodeep1/coding-workflows#5093 (https://github.com/shubhodeep1/coding-workflows/issues/5093)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

The E2E smoke job in `.github/workflows/test-and-mark-stable.yml` dispatches `${REVIEW_WORKFLOW_FILE}` at the smoke PR's branch (`--ref "${BRANCH}"`, `BRANCH=ai/issue-<N>`) in two places: the Phase 3c "Bug B" fallback after the bait commit, and the Phase 4b editor retry. Each dispatched run executes that branch's unmerged copy of the wrapper with `TEST_REPO`'s inherited secrets and write permissions (finding `smoke-review-dispatches-unmerged-workflow`). This plan dispatches both from `TEST_REPO`'s default branch. It then finds the PR-named run that the dispatch starts, and accepts that run only after the run's own log shows it checked out the bait commit.

## Context

- Finding (issue #5093, A08:2021, high, confidence 9/10): `test-and-mark-stable.yml:1368` (Phase 3c) and the retry at line 2416 (Phase 4b) both run `gh workflow run "${REVIEW_WORKFLOW_FILE}" --repo "${TEST_REPO}" --ref "${BRANCH}" -f pr_number=…`. A writer can change the smoke branch's review workflow before the dispatch. Recommendation: dispatch the reviewed default-branch wrapper, and correlate its PR-numbered run with the checked-out bait SHA using verified runtime metadata.
- #4898 AD-6 (this project's base branch) kept both sites at `--ref "${BRANCH}"` and documented the exposure. The alternative it rejected is this plan: *dispatch from the default branch and re-key Phase 4 / 4b on PR-named runs*. The reason for keeping the ref was that Phase 4 pins review runs to `head_sha == BAIT_SHA`, and only a branch dispatch produces that SHA.
- A run dispatched from the default branch is still attributable to the PR:
  - **Run name.** `internal-review.yml` names dispatch runs `Internal: AI Review & Autofix [pr:<N>]` (#4618), and `workflow-templates/ai-review.yml` names them `AI Review [pr:<N>]` (#4701). The run's `head_branch` / `head_sha` are the default branch's.
  - **Checked-out commit.** `review_autofix.yml` checks out the PR head from the PR's metadata at runtime. Its trusted "Checkout PR head branch" step in the `codex-agent` job logs `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` (`review_autofix.yml` ~line 3587). That step runs before any reviewer or editor touches PR content, so the first line-anchored match in that job's log is the commit the run reviewed. Phase 3c's own comments already cite this line as the proof of which tree a run reviewed.
- Phase 4 (`wait-review`) lists `actions/runs?branch=ai/issue-<N>&per_page=100` and selects runs with leg (a) `head_sha == PIN_SHA` or leg (b), in flight at bait time. A default-branch run appears in neither leg. Phase 4 already reads the `codex-agent` job's log through `actions/jobs/<id>/logs` (the editor-noop and reviewer-count shortcuts).
- Phase 4b retry: it adopts an active branch run whose `head_sha` is `BAIT_SHA` or the pre-dispatch head (`RETRY_DISPATCH_SHA`); otherwise it dispatches, registers the oldest new branch run after a baseline id (90 s), and polls that one id. Registration explicitly tolerates the branch advancing after the snapshot.
- The job's "Validate prerequisites" step already calls `gh api "repos/${TEST_REPO}" --jq '.full_name'`; `default_branch` comes from the same response (§15).
- The early checkout step checks out `ref: main`, and Phase 4 sources `./scripts/comprehensive_test_and_release_gh_api.sh` from it. A sourced helper script therefore reaches the stable gate once it is on `main`, which always happens before `stable` carries a workflow that sources it.
- `tests/test_test_and_mark_stable_phantom_review_run_filter.py` extracts Phase 4's pinned `--jq` filter by regex. `tests/test_test_and_mark_stable_review_blocked_budget.py` pins the Phase 4b retry's discovery, dispatch, and registration text. Both run in `ci.yml`.

## Goals

- Neither Phase 3c nor Phase 4b passes `--ref "${BRANCH}"` to `gh workflow run`. Both dispatch `${REVIEW_WORKFLOW_FILE}` at `--ref "${REVIEW_DISPATCH_REF}"`: `TEST_REPO`'s default branch, validated as `^[A-Za-z0-9._/-]+$`, output by "Validate prerequisites" (new `id: prereqs`, output `test_repo_default_branch`) from its existing `repos/${TEST_REPO}` call.
- A new sourced helper, `scripts/smoke_review_dispatch.sh`, provides:
  - `smoke_review_pr_named_runs <repo> <workflow_file> <dispatch_ref> <pr_number>` — one `GET repos/<repo>/actions/workflows/<file>/runs` with `event=workflow_dispatch`, `branch=<dispatch_ref>`, `per_page=100`. Prints the runs whose `event` is `workflow_dispatch`, whose `head_branch` is the dispatch ref, whose `name` is not their `path`, and whose `display_title` ends with ` [pr:<N>]`, as `[{id, status, conclusion, created_at, updated_at, head_sha, name, path, event, display_title}]`. Returns 1 on invalid input, an API failure, or a malformed payload.
  - `smoke_review_checked_out_sha <repo> <run_id>` — one jobs read (`per_page=100`) and at most one job-log read. Prints the SHA from the first line-anchored `Captured INITIAL_HEAD_SHA=<40 hex> for stale-base detection.` in the `codex-agent` job's log. Return codes: 0 found; 1 API failure (retryable); 2 the log was read but has no such line; 3 the run has no `codex-agent` job.
  - `smoke_review_sha_descends_from <repo> <base_sha> <head_sha>` — one `GET repos/<repo>/compare/<base>...<head>`. Returns 0 when the status is `identical` or `ahead`, 1 when `behind` or `diverged`, and 2 on an API failure or bad input.
- **Phase 3c** takes a baseline (the highest id from `smoke_review_pr_named_runs`), dispatches at the default branch, and registers the oldest PR-named run with an id above the baseline. It polls every 5 s for at most 90 s and outputs `bug_b_run_id`. It stays fail-soft: when the baseline read fails it skips the dispatch with a warning, and when registration times out it leaves `bug_b_run_id` empty with a warning. Either way `pull_request: synchronize` stays the primary trigger, as today.
- **Phase 4** keeps legs (a) and (b) unchanged and adds leg (c): the registered `bug_b_run_id` run, read by id (one extra REST call per poll, only while a bait SHA and a run id are set and the run has not been rejected). While the run is active it is a candidate. Once it is `completed`, it stays a candidate only after `smoke_review_checked_out_sha` returns `PIN_SHA` or `BAIT_SHA`. Return codes 2 or 3, or a different SHA, reject it for the rest of the step with a warning. Return code 1 leaves it out for that poll and retries on the next one. The tier preference order is unchanged, and so are the phantom, Copilot, and name filters on legs (a) and (b).
- **Phase 4b** adopts, in addition to today's active branch runs, active PR-named runs that are not the prior run (`smoke_review_pr_named_runs`, one call). When there is no active run, it dispatches at the default branch and registers the oldest new PR-named run after the PR-named baseline, with the same 90 s window. Once the pinned run completes, a run that came from the PR-named listing must pass correlation before attempt 2. Its checked-out SHA (`smoke_review_checked_out_sha`) must be `BAIT_SHA` or `RETRY_DISPATCH_SHA`, or descend from `BAIT_SHA` (`smoke_review_sha_descends_from`, called only when the SHA equals neither). A transient read (return code 1 or 2 from the compare) retries within the existing retry deadline. A definite miss fails the step with the new status `retry_run_unverified`, which the aggregator hard-fails like every status except `success` / `success_after_retry`. Adopted branch runs keep their existing `head_sha` correlation.
- Tests cover the helper functionally, both dispatch sites, the Phase 4 candidate set with leg (c), and the Phase 4b adoption, registration, and correlation. `changelog.d/5093-smoke-review-dispatch-default-branch.md` carries a `security` entry.

## Non-goals

- No change to `review_autofix.yml`, `internal-review.yml`, `workflow-templates/ai-review.yml`, the sweep, the poller, or `scripts/gh_helpers.sh` (#4618 / #4701 / #4898).
- No change to Phase 4 legs (a) and (b), the pin-advance-on-cancel path, the inactivity and wall-clock budgets, the live-log shortcuts, or Phase 4b's pytest attempts and the rest of its statuses.
- No change to the Phase 6 poller-wrapper dispatch at `--ref "${POLLER_DISPATCH_REF}"` (`ai/issue-<N>`). It is the same class of exposure, but it is not this finding and it tests the poller, not the review wrapper (AD-8).
- No change to how the `pull_request: synchronize` review is triggered.

## Constraints

- §1: security first. No review run in the smoke job is dispatched at the smoke branch.
- §5: minimal change. Phases 3c / 4 / 4b change only where this plan says.
- §6: no identifier, output, or status is renamed. Existing Phase 4b statuses and the `review_run_id`, `bait_sha`, `bait_created_at`, and `status` outputs keep their meaning. New identifiers are checked unique in the repo: `prereqs` (step id), `test_repo_default_branch`, `REVIEW_DISPATCH_REF`, `bug_b_run_id`, `BUG_B_RUN_ID`, `retry_run_unverified`, `smoke_review_pr_named_runs`, `smoke_review_checked_out_sha`, `smoke_review_sha_descends_from`, and `scripts/smoke_review_dispatch.sh`.
- §9: tabs in shell and Python, 2-space YAML.
- §15: no new call for the default branch; it extends the existing `repos/${TEST_REPO}` read. Phase 3c adds at most 1 + 18 listing calls, and only when it dispatches. Phase 4 adds one run read per poll only while leg (c) is live, plus at most one jobs read and one log read per verification attempt. Phase 4b adds one listing call before adoption, a listing call per registration poll (replacing today's branch listing for registration), and at most three reads for correlation. A comment above each new call names the call it audited.
- §18: the helper is sourced by existing steps; nothing runs by hand, and there is no single-use or long-running script to register.
- §20: one `changelog.d/` fragment.
- §27: `test-and-mark-stable.yml` is 352,059 bytes and stays well under 480,000.
- The 21,000-character expression limit: new values enter `run:` bodies through `env:`, never as `${{ }}` inside a script body.

## Approach

1. **Default branch.** "Validate prerequisites" gets `id: prereqs`. Its `gh api "repos/${TEST_REPO}"` call reads `[.full_name, .default_branch] | @tsv` in place of `.full_name`, keeps the same error message on failure, validates the branch name, prints the full name as before, and writes `test_repo_default_branch=<branch>` to `$GITHUB_OUTPUT`. Phase 3c and Phase 4b read it as `REVIEW_DISPATCH_REF` through `env:`.
2. **Helper script.** Add `scripts/smoke_review_dispatch.sh` with the three functions above. It calls `gh api` directly with `-X GET` and query fields, and leaves retry policy to its callers. Callers already loop, and the functions report failure through their return codes. Its header documents inputs, outputs, calls, and return codes. The job-name regex is the one Phase 4 already uses, `(^| / )codex-agent( \([^)]*\))?$`. The log line is matched as `^<timestamp> Captured INITIAL_HEAD_SHA=([0-9a-f]{40}) for stale-base detection\.\r?$`, and the first match wins, because the checkout step runs before any PR content is reviewed.
3. **Phase 3c.** Source the helper. Replace the dispatch block with: baseline → dispatch at `REVIEW_DISPATCH_REF` → register. Output `bug_b_run_id`. Rewrite the #4898 exposure comment to describe the new behaviour. Phase 3c's other outputs stay as they are.
4. **Phase 4.** Add `BUG_B_RUN_ID` to `env:` and validate it (empty or a positive integer). Read the branch run list once as raw JSON (the same URL), and apply today's pinned filter with `jq --argjson extra` so leg (c) joins `$m` as `([ …legs a/b… ] + $extra) as $m`. The legacy unpinned path is unchanged. Update the Bug B comment next to the filter.
5. **Phase 4b.** Add `REVIEW_DISPATCH_REF` and `BAIT_SHA` validation. Adoption covers active branch runs (unchanged) plus active PR-named runs. The PR-named baseline replaces the branch baseline for registration. Registration polls `smoke_review_pr_named_runs`. After completion, correlate as in Goals. Add `retry_run_unverified` to the status list comment.
6. **Docs.** Update `agents.md` (the #4898 note's last sentence, plus a short note for this change), `docs/INVENTORY.md` (new script), and the changelog fragment.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1: default-branch smoke review dispatch with checked-out-SHA correlation.** Files: see Files & Modules. Done when:
   - neither dispatch site passes `--ref "${BRANCH}"` and both use `REVIEW_DISPATCH_REF`;
   - Phase 3c registers `bug_b_run_id`, Phase 4 accepts that run only after verification, and Phase 4b adopts, registers, and correlates PR-named runs with the call budgets above;
   - the tests below pass locally, together with `tests/test_workflow_file_size_limit.py` and `scripts/check_workflow_script_refs.py`.

   Rollback: revert the phase PR; nothing persists state.

## Implementation Steps

Phase 1:
1. `scripts/smoke_review_dispatch.sh` [new]: the three functions with a contract header; mode `0755`.
2. `.github/workflows/test-and-mark-stable.yml`:
   - "Validate prerequisites": `id: prereqs`, the default-branch read, and its output.
   - Phase 3c: `REVIEW_DISPATCH_REF` env, source the helper, and the baseline / dispatch / register block with the `bug_b_run_id` output.
   - Phase 4: `BUG_B_RUN_ID` env and validation, raw branch list plus leg (c) through `--argjson extra`, and the verification state.
   - Phase 4b: `REVIEW_DISPATCH_REF` env, PR-named adoption, dispatch, registration, and correlation, plus the status list comment and the header comment.
3. Tests (below), and their registration in `ci.yml` next to the phantom-filter step.
4. `agents.md`, `docs/INVENTORY.md`, `changelog.d/5093-smoke-review-dispatch-default-branch.md`.

## Files & Modules

- `.github/workflows/test-and-mark-stable.yml`
- `.github/workflows/ci.yml` (one test step)
- `scripts/smoke_review_dispatch.sh` [new]
- `tests/test_smoke_review_dispatch.py` [new]
- `tests/test_test_and_mark_stable_review_blocked_budget.py`, `tests/test_test_and_mark_stable_phantom_review_run_filter.py` (updated where they pin the old dispatch, discovery, or filter text)
- `agents.md`, `docs/INVENTORY.md`
- `changelog.d/5093-smoke-review-dispatch-default-branch.md` [new]

## Tests

- New `tests/test_smoke_review_dispatch.py` (pytest, and runnable as a script):
  - Functional, with a stub `gh` on `PATH` that serves per-URL fixtures and records calls:
    - `smoke_review_pr_named_runs` keeps only exact ` [pr:<N>]` suffixes on `workflow_dispatch` runs of the dispatch ref (`[pr:12]` does not match PR 2 or 112), drops phantom runs, sends `-X GET` with the three query fields, and rejects bad input without calling `gh`.
    - `smoke_review_checked_out_sha` returns the first anchored line: a later forged line and a mid-line mention are ignored. It returns 2 when the line is missing, 3 when there is no `codex-agent` job, and 1 on an API failure.
    - `smoke_review_sha_descends_from` maps `identical` / `ahead` to 0, `behind` / `diverged` to 1, and a failure to 2.
  - Contract:
    - Neither dispatch site in the E2E job passes `--ref "${BRANCH}"`; both pass `--ref "${REVIEW_DISPATCH_REF}"`.
    - `prereqs` outputs `test_repo_default_branch` from the same `repos/${TEST_REPO}` call.
    - Phase 3c outputs `bug_b_run_id` and skips the dispatch when the baseline read fails.
    - Phase 4's leg (c) enters `$m` only through `$extra`, and a completed run joins only after verification.
    - Phase 4b's registration uses the PR-named listing, and `retry_run_unverified` is emitted on a definite correlation miss.
  - Functional jq: Phase 4's pinned filter, bash-expanded as the step expands it, picks a verified leg (c) run when it is the only completed one, and still prefers a completed leg (a) run.
- Updated `tests/test_test_and_mark_stable_review_blocked_budget.py` (retry discovery, dispatch, and registration assertions) and `tests/test_test_and_mark_stable_phantom_review_run_filter.py` (extracts the new filter and runs it with `--argjson extra '[]'`).
- Local run: the new and updated modules, `tests/test_workflow_file_size_limit.py`, `python3 scripts/check_workflow_script_refs.py` where it has a CLI, `bash -n` on the helper and on the changed step bodies extracted from YAML, `shellcheck` when installed, and a YAML parse of both workflows.
- No live E2E run: the stable gate runs this job after the project reaches `main`.

## Risks & Mitigations

- A consumer wrapper whose dispatch runs are not named `… [pr:<N>]` (a renamed wrapper without a PR `run-name`, or an `ai-review.yml` from before #4701). Phase 3c then cannot register its dispatch; it warns, and the synchronize run stays the path. Phase 4b fails with `retry_dispatch_failed` instead of a silent mismatch. ACCEPTED: the same gap #4898 accepted; the next `@stable` sync adds the run name.
- A review run that stops at the gate (no `codex-agent` job) is no longer accepted through leg (c). Mitigation: Phase 3b1 applies `force-review` to the smoke PR, so the gate reviews it, and the synchronize run (leg a) is unaffected.
- A forged log line from reviewed PR content. Mitigation: the first line-anchored match wins, and the checkout step logs before any reviewer or editor runs. A miss only makes the smoke gate fail; no secret or write is exposed through it.
- One extra REST read per Phase 4 poll while leg (c) is live. Mitigation: it stops once the run is rejected or the step ends, and it is skipped when no bait was injected or registration failed.
- The helper comes from the `main` checkout while the workflow comes from the dispatched ref. Mitigation: `main` always carries the helper before `stable` carries a workflow that sources it, and the helper's interface is new, so there is no older version to skew against.

## Rollout

Ships with the #4898 / #4701 / #4618 projects to `main`, then runs in the next stable gate. There is no flag. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] What runtime metadata correlates a default-branch dispatch run with the bait commit? — Picked: A — the first line-anchored `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` in the run's `codex-agent` job log, which the trusted checkout step writes before any PR content is reviewed. Alternatives: B — timing only (the run was created after the bait landed); C — add a head-SHA artifact or run name to `review_autofix.yml`. Why: A is written by default-branch code, needs no change to the review workflow (§5), and is the proof Phase 3c's comments already cite; B verifies nothing; C changes a workflow outside this finding. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How are the dispatched runs found? — Picked: A — the per-workflow listing `actions/workflows/<REVIEW_WORKFLOW_FILE>/runs?event=workflow_dispatch&branch=<default>&per_page=100`, keeping runs whose `display_title` ends with ` [pr:<N>]`. Alternatives: B — match only the two exact names, as `_autofix_pr_named_review_runs` does; C — source `scripts/gh_helpers.sh` and reuse `_autofix_pr_named_review_runs`. Why: the listing is already scoped to the wrapper the job dispatches, so the suffix also covers a renamed consumer wrapper that kept the run-name pattern; C pulls the review helper library into the smoke job and lists every workflow. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] When is Phase 3c's dispatch run registered? — Picked: A — in Phase 3c, 90 s, fail-soft, with no dispatch at all when the baseline read fails. Alternatives: B — Phase 4 discovers it lazily with a listing call on every poll; C — dispatch even without a baseline. Why: A mirrors Phase 4b's registration and costs one read per poll in Phase 4; C starts a run nothing can correlate. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] When does Phase 4 accept the registered run? — Picked: A — while active it is a candidate; once completed, only when its checked-out SHA equals `PIN_SHA` or `BAIT_SHA`; a definite miss rejects it for the step, and a transient read retries on the next poll. Alternatives: B — accept any SHA that descends from the bait; C — accept it unverified. Why: A mirrors leg (a)'s exact pin; B could accept a run that reviewed the editor's fix instead of the bait; C ignores the finding's correlation requirement. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] How is Phase 4b's retry run correlated? — Picked: A — the checked-out SHA is `BAIT_SHA` or `RETRY_DISPATCH_SHA`, or descends from `BAIT_SHA` (one compare call only when it equals neither); a definite miss is the new status `retry_run_unverified`. Alternatives: B — exact `BAIT_SHA` / `RETRY_DISPATCH_SHA` only; C — no correlation for the retry. Why: registration already tolerates the branch advancing after the snapshot, so B would fail runs that are correct today; C leaves the retry uncorrelated. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Does Phase 4b also adopt active PR-named runs? — Picked: A — yes, active PR-named runs that are not the prior run, alongside today's branch runs. Alternatives: B — branch runs only. Why: Phase 3c's own dispatch now shows up only in the PR-named listing, and B would queue a duplicate behind it in `pr-autofix-<N>`, which adoption exists to avoid. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Where does the dispatch ref come from? — Picked: A — `TEST_REPO`'s `default_branch`, from the existing "Validate prerequisites" read, as the step output `test_repo_default_branch`. Alternatives: B — a new `repos/${TEST_REPO}` read in each step; C — hardcode `main`. Why: §15 (extend the existing call); C breaks consumer repos with another default branch. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Is the Phase 6 poller-wrapper dispatch at `ai/issue-<N>` in scope? — Picked: A — no; record it in the plan and the report. Alternatives: B — move it to the default branch too. Why: the finding names only the two review dispatches, and Phase 6 exercises the poller wrapper with its own registration contract, so moving it is a separate change (§5). Applied in: no code change. Status: pending review
- AD-9 [plan, 2026-09-29] Where do the new helpers live? — Picked: A — a sourced `scripts/smoke_review_dispatch.sh`. Alternatives: B — inline in the three step bodies. Why: one tested implementation for three call sites, less growth in a 352 KB workflow, and the same sourcing pattern as `comprehensive_test_and_release_gh_api.sh`. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `.claude/scripts/security_pass_skip.py` returned `skip: true` (`ai:security: created and labelled by the issue automation`).
- This session started with no `gh` and no GitHub MCP tools: `gh` was installed with `.claude/hooks/session-start.sh`, and GitHub writes go through `gh api` via the session proxy.

## References

- Issue #5093; finding `smoke-review-dispatches-unmerged-workflow`; audit tracker #3576.
- #4898 (base project, AD-6), #4701, #4618.
- `docs/plans/issue-4898-retrigger-dispatch-default-branch-plan.md`.
- CLAUDE.md §15, §27; `agents.md` "Workflow file size limit".
