# Verify default-branch provenance before the review sweep and the retrigger helpers trust a PR-named review run

Source issue: shubhodeep1/coding-workflows#5522 (https://github.com/shubhodeep1/coding-workflows/issues/5522)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

The review sweep (`.github/workflows/review_autofix_sweep.yml`) counts a `workflow_dispatch` run titled `Internal: AI Review & Autofix [pr:<N>]` as PR `<N>`'s active review, whatever branch the run came from, even when its head branch is null. Anyone who can push a branch can dispatch an altered wrapper from that branch with that title, and the sweep then skips PR `<N>`'s scheduled review while the forged run stays active (security finding `sweep-accepts-spoofed-pr-review-run`, STRIDE Spoofing, medium). This plan makes the sweep, and the shared retrigger lookup `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, count a PR-named run only when its event is `workflow_dispatch`, its head branch is the repository's default branch, and its workflow path is the wrapper that sets that name, which is the check #5094 added to the orchestrator poller.

## Context

- Issues #4618, #4701, and #4898 moved every review dispatch to the default branch. A default-branch run's `head_branch` is the default branch, so the guards find it by the run name the wrappers set (`run-name` in `.github/workflows/internal-review.yml:8` and `workflow-templates/ai-review.yml:12`).
- GitHub evaluates `run-name` from the workflow file **at the dispatched ref**. A writer can push a branch whose `internal-review.yml` (or another workflow file) sets another PR's title and dispatch it at that branch. #5094 closed this in `scripts/orchestrate_poll_process.sh`: a PR-named run counts only with `event == workflow_dispatch`, `head_branch ==` the default branch (`_pr_named_review_default_branch`), and a matching (title, `path`) pair. Its plan listed the remaining matchers under Notes (`docs/completed/issue-5094-verify-pr-named-review-run-provenance-plan.md`).
- The sweep (`review_autofix_sweep.yml`, `snapshot_active_review_runs`, lines ~180–242 on the base branch) pipes the `queued` / `in_progress` / `pending` listings of `internal-review.yml` and `review_autofix.yml` into one `jq -c -s --argjson cutoff …` program:
  - `dispatch_pr` (line ~205) returns `<N>` for any `workflow_dispatch` run whose `display_title` matches the internal name. It checks neither `head_branch` nor `path`.
  - The pre-filter at line 222 keeps a run that has a head branch **or** a `dispatch_pr` key. Issue #4928 added the second half so a named run with a null `head_branch` stays in the snapshot. That is the "null head branch" path the finding names.
  - The per-PR preflight then skips the dispatch when `active_review_runs["<wf>:pr:<N>"]` is non-zero.
- `_autofix_pr_named_review_runs <pr> [status]` (`scripts/gh_helpers.sh:1208`, one `GET actions/runs?event=workflow_dispatch&per_page=100`) checks `event` and that `path` is one of the wrappers, but not `head_branch`, and it accepts either title from either wrapper. Its two callers run inside `review_autofix.yml`'s retrigger steps:
  - `autofix_retrigger_has_inflight_peer`: a forged in-flight run makes the post-commit retrigger and the changes-lost re-dispatch skip their dispatch.
  - `autofix_changes_lost_head_retry_consumed`: a forged completed run consumes the per-head changes-lost retry budget.
- Sibling findings from the same audit are separate issues on the same base branch, each with its own project: #5524 (`merge-train-accepts-spoofed-pr-review-run`, `scripts/review_merge_train.sh:413`), #5521 (`check-in-misses-active-review-beyond-global-page`, `.claude/scripts/check_in_status.py:313`, the same function whose PR-named count lacks the head-branch check), and #5523 (`retry-budget-uses-author-controlled-commit-time`, `scripts/gh_helpers.sh:1575`, a different function in the same file). See AD-1.

## Goals

- G1: In the sweep, `dispatch_pr` returns a PR number only for a run with `event == "workflow_dispatch"`, `head_branch ==` the resolved default branch, `path` (with any `@<ref>` suffix removed) `== ".github/workflows/internal-review.yml"`, and the exact anchored internal title. Any other run is keyed by its head branch, as before, and a run with neither key is dropped. A named run with a null, missing, or empty `head_branch` no longer counts (AD-2).
- G2: The sweep resolves the default branch from the open-PR listing it already fetches (`.base.repo.default_branch`), with no new API call. When no single value is found, PR-named keys are disabled for that tick and one `AUTOFIX_SWEEP_PROVENANCE … outcome=default_branch_unresolved` warning is logged (AD-3).
- G3: `_autofix_pr_named_review_runs` returns only runs with `event == "workflow_dispatch"`, `head_branch ==` the default branch, and a matching (title, `path`) pair: `Internal: AI Review & Autofix [pr:<N>]` with `.github/workflows/internal-review.yml`, or `AI Review [pr:<N>]` with `.github/workflows/ai-review.yml`. Its output shape (`[{id, status, conclusion, created_at, path}]`, API order) and its callers' log lines are unchanged.
- G4: `scripts/gh_helpers.sh` resolves the default branch with a new `_autofix_pr_named_review_default_branch`: at most one `GET repos/<repo>` per process, cached in globals. When it cannot be resolved, `_autofix_pr_named_review_runs` returns 1 without its listing call and logs `AUTOFIX_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved` on stderr, so each caller applies its existing failure rule (the peer check proceeds, the budget check fails closed) (AD-4).
- G5: Tests prove that a forged run (non-default head branch, null head branch, wrong path, title/path swap, non-dispatch event) is rejected at both sites, and that genuine default-branch runs still match.

## Non-goals

- `scripts/review_merge_train.sh` (#5524) and `.claude/scripts/check_in_status.py` (#5521): their own issues and projects fix them (AD-1).
- The poller (`scripts/orchestrate_poll_process.sh`), already fixed by #5094.
- How dispatches are made, the run names, or any other workflow file.
- A key for a run with no head branch, for example by `head_sha` (AD-2).
- The retry-budget time bound in `autofix_changes_lost_head_retry_consumed` (#5523).

## Constraints

- §1: security first. A spoofed run name must not suppress a review.
- §3: fail-safe directions. The sweep without a default branch behaves as before #4618 (a possible duplicate dispatch, absorbed by `review_autofix.yml`'s `cancel-in-progress: false` concurrency group). The helper failure path is each caller's existing rule.
- §5: only the PR-named clauses change. The branch-keyed path, the stale-queued cutoff, the status merge, and the callers' logic stay as they are.
- §6: no identifier is renamed or removed. New identifiers `_autofix_pr_named_review_default_branch`, `_AUTOFIX_PR_NAMED_REVIEW_DEFAULT_BRANCH_CACHE`, `_AUTOFIX_PR_NAMED_REVIEW_DEFAULT_BRANCH_READY`, the jq `$default_branch` argument, the shell variable `sweep_default_branch`, and the log keys `AUTOFIX_SWEEP_PROVENANCE` / `AUTOFIX_PR_NAMED_PROVENANCE` are checked for collisions in `scripts/`, `.github/`, and `tests/` before use.
- §9: 2-space YAML; tabs in `gh_helpers.sh` and the Python tests.
- §15: the sweep adds no API call. The helper adds one `GET repos/<repo>` per process, only on the PR-named fallback path, and caches it. That call is audited in its docstring: no existing call in `review_autofix.yml`'s retrigger steps returns the default branch.
- §18: no new script; the sweep and the retrigger steps already run automatically.
- §20: one fragment, `changelog.d/5522-sweep-verify-pr-named-run-provenance.md`, section `security`.
- §27: `review_autofix_sweep.yml` is ~22 KB, far under the 480,000-byte guard.

## Approach

**Sweep.**
- In the PR listing's `jq` map, add `default_branch: (.base.repo.default_branch // "")` to each PR object. After the zero-candidate early exit, set `sweep_default_branch` to the single distinct non-empty value, or empty (and log the warning) when there is none or more than one.
- Pass `--arg default_branch "${sweep_default_branch}"` to the snapshot `jq`, and make `dispatch_pr` require `$default_branch != ""`, `event == "workflow_dispatch"`, `head_branch == $default_branch`, `path | sub("@.*$"; "") == ".github/workflows/internal-review.yml"`, and the anchored title. Removing an `@<ref>` suffix only drops the ref part, so no other file can match.
- Leave the line-222 pre-filter as it is. With the stricter `dispatch_pr`, a run with no head branch never has a PR key, so it is dropped. Update the comments at `dispatch_pr`, the pre-filter, and the dispatch block to explain provenance and cite this issue.
- A forged run dispatched from another branch keeps counting under its own head branch, so it can only hold back a PR whose head is that branch, the attacker's own. That is the same as any branch-keyed run.

**`_autofix_pr_named_review_runs`.**
- Resolve the default branch first with `_autofix_pr_named_review_default_branch` (one `gh_retry gh api -X GET "/repos/${GITHUB_REPOSITORY}" --jq '.default_branch'`, cached per process).
- When the branch is empty, log and return 1 before the listing call. Otherwise filter with event, `head_branch`, and the two (title, `path`) pairs.
- The listing call and output shape are unchanged. The server-side `branch=` filter #5094 used is not added (§5; paging is #5521's and #5442's topic).

**Rejected alternatives.**
- Keeping a null-head named run (AD-2): the finding names that path, and a run's head branch cannot be verified when it is missing.
- `github.event.repository.default_branch` for the sweep: a `schedule` event payload carries no repository object.
- Reading the event payload in `gh_helpers.sh`: it varies by caller event, and the REST read is authoritative and matches #5094.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: one issue, one coherent change to the two unowned PR-named matchers plus their tests and docs.

1. **Phase 1 — verify default-branch provenance and wrapper identity for PR-named runs in the sweep and in `_autofix_pr_named_review_runs`.**
   - Files: `.github/workflows/review_autofix_sweep.yml`, `scripts/gh_helpers.sh`, `tests/test_review_autofix_sweep_stale_queued.py`, `tests/test_retrigger_default_branch_dispatch.py`, `tests/test_editor_changes_lost_redispatch_budget.py`, `tests/test_gh_helpers_list_runs_method.py` (only where their `gh` stubs or call counts need the default-branch read), `agents.md`, `changelog.d/5522-sweep-verify-pr-named-run-provenance.md` [new].
   - Done when:
     - G1–G5 hold;
     - the tests above, `tests/test_review_dispatch_default_branch.py`, and `tests/test_review_autofix_sweep_zero_candidate_fast_exit.py` pass;
     - `bash -n scripts/gh_helpers.sh` is clean;
     - `yamllint -c .yamllint.yml` and `actionlint` (when available) accept the workflow.
   - Rollback: revert the phase PR. Both matchers go back to name-only matching, with no state to undo.

## Implementation Steps

1. `.github/workflows/review_autofix_sweep.yml`:
   - add `default_branch` to the PR map;
   - compute `sweep_default_branch` after the zero-candidate exit, logging `::warning::AUTOFIX_SWEEP_PROVENANCE repo=<repo> outcome=default_branch_unresolved pr_named_matching=disabled` when it is empty;
   - pass `--arg default_branch` to the snapshot `jq` and tighten `dispatch_pr`;
   - refresh the header's "Dispatch ref" note and the inline comments.
2. `scripts/gh_helpers.sh`:
   - add the two cache globals and `_autofix_pr_named_review_default_branch`, with a docstring giving input, output, API calls, and fail-closed behaviour (§15);
   - in `_autofix_pr_named_review_runs`, resolve the branch first, and extend the `jq` filter with `head_branch` and the (title, `path`) pairs;
   - update the header comments of the helper and of its two callers.
3. `tests/test_review_autofix_sweep_stale_queued.py`:
   - extend the `JQ_BLOCK` regex and `run_sweep_reduce` to pass `default_branch` (default `"main"`);
   - give the `dispatch_run` fixture `path: .github/workflows/internal-review.yml`;
   - replace the #4928 null-head cases with rejection cases (null, missing, and empty head branch are dropped; wedged null-head runs are not logged under a PR);
   - add rejection cases for a non-default head branch (keyed by that branch), wrong path, a `review_autofix.yml` path, an `@ref` suffix (accepted), and an empty default branch (no PR keys);
   - add contract assertions for the listing field and the warning line.
4. gh_helpers tests: extend the `gh` stubs to answer `repos/owner/repo --jq .default_branch`, and extract the new function. Adjust call-count assertions (+1 read on the fallback path). Add cases:
   - rejected: non-default `head_branch`, a null `head_branch`, title/path swap, `push` event;
   - the unresolved default branch: no listing call, return 1, the peer check proceeds with `reason=pr_named_api_error`, the budget fails closed;
   - one default-branch read across two helper calls in one process.
5. `agents.md`: update the sweep sentence (issue #4928's null-head note) and the `_autofix_pr_named_review_runs` sentence to describe the provenance checks and the unresolved-branch behaviour.
6. `changelog.d/5522-sweep-verify-pr-named-run-provenance.md` (`<!-- changelog: security -->`), following §20.D.

## Files & Modules

- `.github/workflows/review_autofix_sweep.yml`
- `scripts/gh_helpers.sh`
- `tests/test_review_autofix_sweep_stale_queued.py`
- `tests/test_retrigger_default_branch_dispatch.py`, `tests/test_editor_changes_lost_redispatch_budget.py`, `tests/test_gh_helpers_list_runs_method.py` (as needed)
- `agents.md`
- `changelog.d/5522-sweep-verify-pr-named-run-provenance.md` [new]

## Tests

- The sweep `jq` is run as extracted from the workflow (existing harness), with the cases in step 4.
- The gh_helpers probes run with a stubbed `gh` (existing harness), with the cases in step 5.
- Regression: the full files listed in the phase's "Done when", plus `bash -n`.

## Risks & Mitigations

- **A GitHub run with a null `head_branch` that really came from the default branch** is no longer seen by the sweep (AD-2). The next tick then dispatches that PR again. The new run lands in the same `review_autofix` concurrency group (`cancel-in-progress: false`) and replaces only the pending one, so the PR is still reviewed. ACCEPTED, and logged by the existing `AUTOFIX_SWEEP_DISPATCH` line.
- **A tag named like the default branch.** A `workflow_dispatch` at a tag reports the tag name as `head_branch`. Creating a tag needs the same write access as pushing a branch, and a tag named after the default branch makes `--ref <default>` ambiguous for every dispatch. RESIDUAL: noted, not fixed here; the orchestrator's #5094 check has the same limit.
- **Default-branch read failure in `gh_helpers.sh`:** each caller applies its existing failure rule. The peer check dispatches (a duplicate absorbed by the concurrency group), and the budget check fails closed (no automated changes-lost retry that run). ACCEPTED.
- **Consumer repos** get the change on the next `@stable` sync. Their `ai-review.yml` runs are matched with the consumer (title, path) pair, and their default branch is resolved, never assumed `main`.

## Rollout

Ships with this project's final PR into its base branch (#4898's project branch), then reaches `main` with that project's final PR #4923. Consumer repos receive it on the next `@stable` sync. No flag and no migration.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Q1: Which PR-named run consumers does this issue fix? — Picked: A — the sweep (the reported line) and `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, the one remaining consumer no other open issue covers. Alternatives: B — the sweep only; C — also `scripts/review_merge_train.sh` and `.claude/scripts/check_in_status.py`. Why: the recommendation says to apply the check to the other run-name consumers. The merge train (#5524) and the check-in script (#5521) have their own issues and projects on the same base, so editing them here would race those projects, and the check-in script is a protected path (§28.C). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Q2: What happens to a PR-named run whose `head_branch` is null, missing, or empty (the #4928 case)? — Picked: A — it no longer counts under `pr:<N>` and is dropped. Alternatives: B — keep counting it by name (the #4928 behaviour); C — keep it only when its `head_sha` is in the default branch's recent history (one extra commits read per tick). Why: the finding names the null-head path. A missing head branch cannot prove provenance (§1), and the cost of dropping one is a duplicate dispatch that the concurrency group absorbs. C adds an API call and a new trust rule for a rare GitHub quirk (§5, §15). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Q3: Where does the sweep get the default branch? — Picked: A — the `.base.repo.default_branch` of the open-PR listing it already fetches, disabling PR-named keys (with a warning) when there is no single value. Alternatives: B — `github.event.repository.default_branch`; C — one extra `GET repos/<repo>` per tick. Why: A costs no API call (§15). B is absent on `schedule` events, the sweep's main trigger. C duplicates data the listing already returns. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Q4: How does `_autofix_pr_named_review_runs` get the default branch, and what happens when it cannot? — Picked: A — one cached `GET repos/<repo>` per process on the PR-named path. When it fails, the helper returns 1 without listing, so the callers' existing failure rules apply. Alternatives: B — read `repository.default_branch` from `GITHUB_EVENT_PATH`; C — assume `main`. Why: A is authoritative in every caller context and matches #5094's `_pr_named_review_default_branch`. B depends on the caller's event payload. C would let a guessed branch vouch for a run in consumer repos whose default is not `main`. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Residual same-pattern matchers owned elsewhere (AD-1):
  - `scripts/review_merge_train.sh` `_mt_inflight_review_branches` → #5524;
  - `.claude/scripts/check_in_status.py` `_active_run_count`, whose PR-named count checks the (path, title) pair but not `head_branch` (`_is_pr_dispatched_review_run` already checks it) → #5521's project should carry the same predicate.

## References

- Issue #5522 (this finding), audit tracker #3576
- Issues #4618, #4701, #4898, #4928, #5094 (default-branch dispatch, PR-named runs, and the poller's provenance fix)
- Sibling findings #5521, #5523, #5524
