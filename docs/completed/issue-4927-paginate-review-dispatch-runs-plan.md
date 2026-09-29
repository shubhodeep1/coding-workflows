# Page through the review wrappers' dispatch runs before a review dispatch or an empty-commit push

Source issue: shubhodeep1/coding-workflows#4927 (https://github.com/shubhodeep1/coding-workflows/issues/4927)
Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

`_pr_named_review_dispatch_runs` in `scripts/orchestrate_poll_process.sh` finds the default-branch review runs named for a PR by reading a single page of the newest 100 `workflow_dispatch` runs from every workflow. When more than 100 newer dispatches exist, a still-active review run falls off that page. The helper then reports "no run", and the stall recovery pushes an empty commit onto the PR head, which throws away the in-flight review (security finding `review-run-global-window-exhaustion`, medium). This plan makes the helper page through the dispatch runs of the two review wrappers only, within the review-run window. The helper now tells its callers when the listing is incomplete. On an incomplete listing, every caller skips its dispatch or empty-commit push, and the next poll cycle retries.

## Context

- Issue #4927 was filed by `.github/workflows/security-audit.yml` (tracker #3576) against `scripts/orchestrate_poll_process.sh:17411` on the `claude/implement-plan-issue-4701-review-dispatch-default-branch` project branch. Its `Integration branch:` line names that branch, so this project is built on it.
- Issue #4701 added `_pr_named_review_dispatch_runs <pr>`. It makes one `gh run list --event workflow_dispatch --limit 100` call with no workflow filter and matches the wrapper run names `Internal: AI Review & Autofix [pr:<N>]` (`.github/workflows/internal-review.yml`) and `AI Review [pr:<N>]` (`workflow-templates/ai-review.yml`, the consumer wrapper). On a gh or jq failure it prints `[]` and fails open.
- The helper has three callers, all in `scripts/orchestrate_poll_process.sh`:
  1. `_has_active_autofix_run`: the "is a review already running for this PR" guard inside `_dispatch_review_for_conflicts` (about 15 dispatch paths) and the integration-heal lifetime-cap deferral. It uses the helper only when its head-branch lookups found nothing.
  2. `_direct_inflight_review_run_on_branch <branch> [pr]`: the last guard before the destructive empty-commit push in `execute_stall_recovery_action` (`retrigger_review`) and `run_standalone_stall_recovery`. It uses the helper only when its branch listing matched no fresh run.
  3. The failed-autofix redispatch lookup in `execute_stall_recovery_action` (`retrigger_review`). It reads the newest PR-named run and redispatches when that run failed and is newer than every completed head-branch run.
- Measured on 2026-09-29 in this repo: `internal-review.yml` had 153 `workflow_dispatch` runs in 5 hours and 701 in 24 hours, out of 747 `workflow_dispatch` runs in 24 hours. The single page of 100 therefore covers under 3.5 hours, less than the 250-minute review-run budget (`REVIEW_RUN_MAX_RUNTIME_MINUTES`). A burst of unrelated dispatches shrinks that window further. `ai-review.yml` answers 404 here; consumer repos have `ai-review.yml` and no `internal-review.yml`.
- GitHub's filtered workflow-run listings (`event`, `created`, …) return `total_count` and serve at most 1,000 results per query.

## Goals

- `_pr_named_review_dispatch_runs` lists only the `workflow_dispatch` runs of `internal-review.yml` and `ai-review.yml`, created within the last `REVIEW_RUN_MAX_RUNTIME_MINUTES`. It pages 100 runs at a time until it has read every run the listing reports (`total_count`), so unrelated dispatches can no longer push a review run out of view.
- The helper returns 0 when the listing is complete and 1 when it is not. Incomplete means a page failed (other than a 404 for an absent wrapper), a page was malformed, the cutoff time could not be computed, or fewer distinct runs were read than `total_count` reports (including past the 10-page / 1,000-result cap). Its stdout keeps the same JSON array shape.
- On an incomplete listing:
  - `_has_active_autofix_run` reports a possibly active run (return 0), so `_dispatch_review_for_conflicts` skips the dispatch (rc 2) and the integration heal defers one tick.
  - `_direct_inflight_review_run_on_branch` prints `listing-incomplete`, and both empty-commit push sites skip the push.
  - The failed-autofix redispatch lookup skips both the redispatch and the empty-commit push.
  Each case logs a searchable line, and the next poll cycle retries.
- The behaviour on a complete listing is unchanged for every caller.

## Non-goals

- The merge train's `_mt_inflight_review_branches` (`scripts/review_merge_train.sh`, one `actions/runs?per_page=100` page) and the sweep's snapshot in `.github/workflows/review_autofix_sweep.yml`. Neither pushes an empty commit, and the finding names the poller (AD-5).
- The head-branch lookups (`--branch <head_ref>` with `--limit 5` / `--limit 30`) and the cached `actions/runs` blob scans. A branch listing is scoped to the PR's own branch, so unrelated dispatches cannot crowd it.
- A cycle-local cache of the listing. Each call still reads live state, because a stale cache could hide a run that started a moment ago.

## Constraints

- §5: only the helper, its three call sites, their tests, and the docs that describe the helper change.
- §6: no identifier is renamed or removed. `_pr_named_review_dispatch_runs` keeps its name, arguments, and stdout shape, and gains a return code. `STALL_RECOVERY_EFFECTIVE_ACTION` gets no new value; the skip paths reuse `retrigger_review_skipped_inflight`.
- §8: every incomplete listing logs one structured line: `PR_NAMED_REVIEW_RUNS pr=<N> outcome=incomplete reason=<…> …` from the helper, and `STALL_INFLIGHT_DIRECT_CHECK … outcome=pr_named_listing_incomplete` from the direct guard.
- §15: the helper's call count goes from 1 to one call per wrapper plus one per extra page: 3 calls in this repo today (two `internal-review.yml` pages and one 404 for `ai-review.yml`). It is still issued only after the head-branch lookups miss. The docstring documents the input, output, return codes, call budget, and failure behaviour. REST only, no GraphQL.
- §9: tabs in the function bodies that already use tabs; the call sites keep their surrounding two-space style.
- §20: one `changelog.d/` fragment (security).

## Approach

Replace the helper's single global `gh run list` with a per-wrapper REST listing:

```
gh_retry gh api -X GET "repos/${GITHUB_REPOSITORY}/actions/workflows/<wrapper>/runs?event=workflow_dispatch&created=>=<cutoff>&per_page=100&page=<p>" --jq '{total_count, workflow_runs: [...]}'
```

for `<wrapper>` in `internal-review.yml` and `ai-review.yml`, where `<cutoff>` is now minus `REVIEW_RUN_MAX_RUNTIME_MINUTES` (default 250). Stderr is captured: a 404 means the wrapper does not exist in this repo, which is a complete, empty listing for that wrapper. Pages continue until the distinct runs read reach `total_count`, or a page comes back short. A short page before `total_count` is reached means the listing shifted underneath, so it counts as incomplete. At most 10 pages are read per wrapper. The REST fields are mapped to the names callers already read (`databaseId`, `event`, `status`, `conclusion`, `displayTitle`, `createdAt`, `startedAt`), and the same exact-name filter runs as before.

Alternatives considered (AD-1): a repo-wide `actions/runs?event=workflow_dispatch` listing with the same window is one page sequence, but unrelated dispatches still inflate it toward the 1,000 cap, so an attacker could keep it permanently incomplete. Raising `--limit` keeps a fixed window that a burst can exhaust.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — paginated, completeness-aware PR-named lookup.**
   - Files: `scripts/orchestrate_poll_process.sh`, `tests/test_review_dispatch_default_branch.py`, `tests/test_conflict_dispatch_active_run_visibility.py`, `tests/test_orchestrate_poll_process.py`, `agents.md`, `README.md`, `changelog.d/4927-paginate-review-dispatch-runs.md`.
   - Done when: the helper pages per wrapper within the window and returns 1 on an incomplete listing; each of the three callers skips its dispatch or push on 1 with a log line; all tests below pass; `bash -n` is clean on the poller.
   - Rollback: revert the phase PR. The helper goes back to the single-page, fail-open lookup.

## Implementation Steps

1. `scripts/orchestrate_poll_process.sh`, `_pr_named_review_dispatch_runs` (~L17374-17430): rewrite the body as in Approach, rewrite its docstring (input, output, return codes, API calls, 404 rule, window, cap), and log `PR_NAMED_REVIEW_RUNS … outcome=incomplete reason=<page_failed|malformed_page|cutoff_unavailable|truncated|listing_shifted>` on stderr for an incomplete listing.
2. `_has_active_autofix_run` (~L17351-17367): capture the helper's output and return code separately; on 1, log `Review dispatch run listing incomplete … Skipping dispatch; the next poll cycle retries.` and return 0.
3. `_direct_inflight_review_run_on_branch` (~L12564-12640): capture the helper's return code; on 1, log `STALL_INFLIGHT_DIRECT_CHECK … outcome=pr_named_listing_incomplete` and print `listing-incomplete`. Document the sentinel in its header comment.
4. `execute_stall_recovery_action` `retrigger_review` (~L13786-13803 and ~L13923): capture the helper's return code in the failed-autofix lookup, and on 1 log and return 1 with `STALL_RECOVERY_EFFECTIVE_ACTION=retrigger_review_skipped_inflight`. At the direct-guard call, handle `listing-incomplete` with its own log line before the existing skip.
5. `run_standalone_stall_recovery` (~L16573-16581): handle `listing-incomplete` with its own log line in the existing skip branch.
6. Tests (below), `agents.md` (the PR-named lookup paragraph), `README.md` (the failed-autofix redispatch paragraph), and the changelog fragment.

## Files & Modules

- `scripts/orchestrate_poll_process.sh`
- `tests/test_review_dispatch_default_branch.py`
- `tests/test_conflict_dispatch_active_run_visibility.py`
- `tests/test_orchestrate_poll_process.py`
- `agents.md`
- `README.md`
- `changelog.d/4927-paginate-review-dispatch-runs.md` [new]

## Tests

- Unit (`tests/test_review_dispatch_default_branch.py`, extracted shell functions with a stubbed `gh`):
  - one REST call per wrapper, each with `event=workflow_dispatch`, `per_page=100`, and a `created=>=` cutoff about `REVIEW_RUN_MAX_RUNTIME_MINUTES` ago;
  - exact-name matching on both wrappers, newest first;
  - pagination reads page 2 when `total_count` is above 100, and finds a matching run there;
  - a 404 wrapper counts as absent and complete;
  - a non-404 failure, a malformed page, a listing past the 10-page cap, and a short page before `total_count` all return 1;
  - an invalid PR number still prints `[]` with no call;
  - `_has_active_autofix_run` returns 0 with the incomplete-listing log line;
  - `_direct_inflight_review_run_on_branch` prints `listing-incomplete` with `outcome=pr_named_listing_incomplete`.
- Contract (`tests/test_conflict_dispatch_active_run_visibility.py`): update the static assertions to the new helper shape (per-wrapper REST listing, no global `--limit 100` page).
- Integration (`tests/test_orchestrate_poll_process.py`): serve `actions/workflows/<wrapper>/runs?event=workflow_dispatch` from `active_autofix_runs` in the `gh` mock. The four existing PR-named stall-recovery tests must keep passing, plus a new test: when the wrapper listing fails, `retrigger_review` pushes no empty commit and dispatches nothing.
- The CI steps that already run these three files (`ci.yml`) cover them; no wiring change.

## Risks & Mitigations

- A listing that stays incomplete (GitHub API down, or more than 1,000 review dispatches in 250 minutes) blocks stall-recovery pushes and conflict dispatches for as long as it lasts — ACCEPTED: that is the finding's recommendation. Skipping is the safe side of a destructive action, each skip is logged, and the next cycle retries.
- More API calls on the fallback path (3 instead of 1 here) — ACCEPTED: issued only after the head-branch lookups miss, documented per §15.
- A run older than 250 minutes is no longer visible to `_has_active_autofix_run` — ACCEPTED: the poller already treats such a review run as a zombie, and the old 100-run page covered less than that window in this repo.

## Rollout

Ships with the poller on the next `@stable` release; consumer repos pick it up with their wrapper sync. No flag, no new env var, no state migration. Rollback is reverting the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which listing should replace the global page? — Picked: A — per-wrapper REST listing of `internal-review.yml` and `ai-review.yml` `workflow_dispatch` runs, paginated. Alternatives: B — repo-wide `actions/runs?event=workflow_dispatch` paginated; C — keep one page and raise `--limit`. Why: only a per-wrapper listing cannot be crowded by unrelated dispatches, as the finding recommends. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How far back should the listing reach? — Picked: A — `REVIEW_RUN_MAX_RUNTIME_MINUTES` (default 250), the poller's own review-run freshness window. Alternatives: B — 24 hours (about 8 pages here, near the 1,000 cap); C — no window (unbounded pages). Why: runs older than the window are zombies by the poller's definition, and 250 minutes is about 2 pages here. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What counts as incomplete, and what do callers do? — Picked: A — any non-404 page failure, malformed page, cutoff failure, truncation, or shifted listing is incomplete, and every caller skips its dispatch or push this cycle. Alternatives: B — only truncation is incomplete, and API errors keep failing open. Why: a failed listing cannot prove no review is running, and the recommendation says to skip and retry. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is "incomplete" signalled? — Picked: A — the helper keeps its stdout contract and returns 1; the direct guard prints the `listing-incomplete` sentinel; the skip paths reuse `retrigger_review_skipped_inflight`. Alternatives: B — a second helper beside the old one. Why: §6 and §5, one listing and no new state values. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should the merge train's and the sweep's run snapshots change too? — Picked: A — no; poller only, recorded as a non-goal. Alternatives: B — also page the merge train's `_mt_inflight_review_branches`. Why: §5; the finding names the poller's empty-commit push, which neither of the others performs. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] How is a 404 for a wrapper treated? — Picked: A — the wrapper is absent, so its listing is complete and empty. Alternatives: B — incomplete. Why: every repo lacks one of the two wrappers, so B would block every dispatch and push forever. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1, 2026-09-29] Should a failed branch listing in `_direct_inflight_review_run_on_branch` also skip the empty-commit push? — Picked: A — no; keep its documented fail-open (`outcome=listing_unavailable`, covered by `test_empty_or_non_array_payload_fails_open`). Alternatives: B — return `listing-incomplete` there too; C — still run the PR-named lookup before failing open. Why: §5; the branch listing is scoped to the PR's own branch, so the finding's crowding vector cannot reach it. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #4927, security audit tracker #3576.
- Issue #4701 and its plan `docs/plans/issue-4701-review-dispatch-default-branch-plan.md` (PR #4725); issue #4618 (sweep).
- PR #3895 incident (duplicate dispatches), PR #3082 / issue #3081 (empty-commit push discarding a review).
