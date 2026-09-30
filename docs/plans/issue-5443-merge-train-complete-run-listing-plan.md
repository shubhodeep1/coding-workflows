# Merge-train release: match `@ref`-suffixed run paths and read every active run before releasing a queued PR

Source issue: shubhodeep1/coding-workflows#5443 (https://github.com/shubhodeep1/coding-workflows/issues/5443)
Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

The merge train's `release` subcommand decides whether a queued PR already has an active review run by reading one page of the newest 100 workflow runs and matching each run's `path` against `(^|/)(review_autofix|internal-review|ai-review)\.ya?ml$`. A path that ends in an `@<ref>` suffix (`…/ai-review.yml@refs/heads/main`) fails that match, a run older than the newest 100 is never seen, and a failed lookup releases anyway. In each case the train can release the PR and dispatch a second review beside a pending one (security finding `merge-train-drops-ref-suffixed-run-paths`, medium). This plan strips the `@<ref>` suffix before the match, lists every active run page by page, and leaves queued PRs queued when that listing is incomplete.

## Context

- Issue #5443 was filed by `.github/workflows/security-audit.yml` (tracker #3576) against `scripts/review_merge_train.sh:413` on the `claude/implement-plan-issue-4701-review-dispatch-default-branch` project branch. Its `Integration branch:` line names that branch, so this project is built on it.
- `_mt_inflight_review_branches` (`scripts/review_merge_train.sh:410-414`) makes one `GET actions/runs?per_page=100` call (every workflow, every status), keeps runs whose status is `queued`, `pending`, or `in_progress` and whose path matches the regex above, and prints each run's head branch plus `pr:<N>` for a `workflow_dispatch` run named `Internal: AI Review & Autofix [pr:<N>]` / `AI Review [pr:<N>]` (issue #4701).
- `_mt_release` (`scripts/review_merge_train.sh:441-530`) calls it once before its loop. On failure it logs `could not list active review runs; continuing without the dispatch-dedup guard` and evaluates every queued PR as if no review were active. That contradicts the file's own fail-open contract (lines 62-66): "any API failure … exits 0 WITHOUT releasing (release)".
- A run of a workflow defined outside the repository's own `.github/workflows/` directory (for example a ruleset-required workflow) reports a `path` carrying an `@<ref>` suffix; the finding reports such a path for `ai-review.yml`.
- In this repo `internal-review.yml` alone had 153 `workflow_dispatch` runs in 5 hours on 2026-09-29 (issue #4927), so the newest 100 runs of all workflows cover well under the 250-minute review-run budget. Issue #4927 fixed the same window problem for the poller's `_pr_named_review_dispatch_runs` and explicitly left the merge train out of scope (its AD-5); this plan is that remaining half.
- GitHub's filtered run listings (`status=…`) return `total_count` and serve at most 1,000 results per query.

## Goals

- A run whose `path` ends in `@<ref>` (`.github/workflows/ai-review.yml@refs/heads/main`, `owner/repo/.github/workflows/internal-review.yml@refs/heads/main`) matches the review-workflow regex exactly as the same path without the suffix does.
- The active-run listing reads every run in each active status (`pending`, `queued`, `in_progress`, the three the filter already keeps), page by page (100 per page) until the distinct runs read reach the listing's `total_count`, at most 10 pages per status.
- The listing is **incomplete** when a page fails after `gh_retry`, a page is malformed, a short page arrives before `total_count` is reached (the listing shifted), or more runs exist than 10 pages hold. Each case logs one structured line (§8): `MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=<page_failed|malformed_page|listing_shifted|truncated|filter_failed> status=<s> page=<p> read=<n> total=<n>`.
- On an incomplete listing `release` leaves every queued PR queued for that invocation (no marker retirement, no label removal, no dispatch), logs `MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=<N> action=leave_queued` per PR, and the next close event or poll tick retries.
- The listing is read only once per `release` invocation, and only when at least one `ai:merge-queued` PR passes the base filter. A tick with no queued PR makes no `actions/runs` call.
- Behaviour on a complete listing is unchanged: the same runs hold the same PRs (`MERGE_TRAIN_RELEASE_ACTIVE`), and every other release path is untouched.

## Non-goals

- The `gate` subcommand. It never dispatches and never reads workflow runs.
- The same path regex in `scripts/gh_helpers.sh` (`autofix_retrigger_has_inflight_peer`, `autofix_changes_lost_head_retry_consumed`). Those are branch-scoped listings in the review workflow's own retrigger path, not the merge train the finding names (AD-5). Recorded under Notes for a separate issue.
- Adding the `waiting` / `requested` statuses to what counts as active (AD-6).
- A cross-invocation cache of the listing; each release invocation reads live state.

## Constraints

- §5: only `_mt_inflight_review_branches`, its caller in `_mt_release`, the file header's API-budget and fail-open text, the tests, the README row that describes `release`, and one changelog fragment change.
- §6: no identifier is renamed or removed. `_mt_inflight_review_branches` keeps its name, takes no arguments, and still prints one sorted key per line (`<head branch>` or `pr:<N>`); it returns 1 on an incomplete listing, as it already did on a failed call. Existing log keys (`MERGE_TRAIN_RELEASE_ACTIVE`, `MERGE_TRAIN_RELEASED`, …) keep their names and fields. The new log keys (`MERGE_TRAIN_RUNS_LISTING`, `MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE`) and the new locals are checked against the file for collisions.
- §8: every incomplete listing is logged once with its reason, and each queued PR it holds back is logged.
- §15: one `GET actions/runs?status=<s>&per_page=100&page=<p>` per page per status: 3 calls per release invocation in the normal case (one page each), issued only when a queued PR is evaluated. Today every invocation makes 1 call whether or not anything is queued, so ticks with an empty queue drop from 1 call to 0. The function comment documents input, output, return codes, call budget, and failure behaviour. REST only.
- §9: tabs, opening braces on a new line for the new function body, matching the file's existing style.
- §20: one `changelog.d/` fragment (`security`).
- §12/§19: phase and fix PRs use `Refs #5443`; the base is not the default branch, so the final PR also uses `Refs #5443` and the final-merge stage closes the issue explicitly.

## Approach

`_mt_inflight_review_branches` reads the three active statuses in lifecycle order (`pending`, `queued`, `in_progress`), so a run that moves forward between two queries is seen in at least one of them:

```
gh_retry gh api -X GET "repos/${MT_REPO}/actions/runs?status=<s>&per_page=100&page=<p>" \
	--jq '{total_count: .total_count, workflow_runs: [(.workflow_runs // [])[]? | select(type == "object") | {id, status, event, head_branch, display_title, path}]}'
```

Pages accumulate through stdin (never `--argjson`) with `unique_by(.id)` and stop when the distinct runs read reach `total_count`; a short page before that, a page past 10, a failed or malformed page is incomplete and returns 1. The accumulated runs then go through the existing key filter, with the path compared after `sub("@.*$"; "")`.

`_mt_release` fetches the listing lazily, the first time a queued PR passes the base filter, and remembers whether it was complete. On an incomplete listing it logs a warning once and skips every queued PR before any write.

Alternatives considered (AD-1): paginating the unfiltered `actions/runs` listing has no natural end (every completed run in the repo's history); listing each review workflow per status (`actions/workflows/<wf>/runs?status=<s>`) costs 9 calls per invocation and would still miss a run whose workflow is reported under an `@<ref>` path. Status-filtered repo-wide listings are small (active runs only) and complete by `total_count`.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is one function, its caller, and their tests, and cannot be split into independently useful parts.

1. **Phase 1 — complete, suffix-tolerant active-run listing for the merge-train release.**
   - Files: `scripts/review_merge_train.sh`, `tests/test_review_merge_train.py`, `README.md`, `changelog.d/5443-merge-train-complete-run-listing.md` [new].
   - Done when: the new and existing tests in `tests/test_review_merge_train.py` and `tests/test_review_dispatch_default_branch.py` pass, `bash -n` and `shellcheck` (as CI runs it) are clean on the script, and each Goal above has a test.
   - Rollback: revert the phase PR; the previous single-page lookup returns with no data or state to clean up (the train keeps its labels and markers unchanged).

## Implementation Steps

Phase 1:
1. `scripts/review_merge_train.sh` — rewrite `_mt_inflight_review_branches` as described in Approach, with a comment block giving input, output, return codes, API calls, lifecycle order, and the incomplete reasons; strip `@<ref>` before the path regex.
2. `scripts/review_merge_train.sh` — in `_mt_release`, replace the eager call and its fail-open warning with a lazy fetch on the first queued PR that passes the base filter; on an incomplete listing log `::warning::` once plus `MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=<N> action=leave_queued` per PR and `continue` before any write.
3. `scripts/review_merge_train.sh` — update the header's release API budget (lines 51-60) and fail-open text so they describe the paged, lazy listing.
4. `tests/test_review_merge_train.py` — make the fake `gh` serve `actions/runs` filtered by the `status` query parameter with `total_count` and 100-run pages (existing fixtures keep working), add a `fail_runs_get` switch and a `total_count` override, and add tests for: an `@refs/heads/main` path holding its PR; an owner-prefixed `@ref` path; a failed listing leaving the PR queued with no label removal or dispatch; a truncated / shifted listing leaving it queued; a second page being read when `total_count` exceeds 100 and a run on page 2 holding its PR; each status being queried; no `actions/runs` call when nothing is queued.
5. `README.md` — the `release` row of the merge-train table (line 2312) says the active-run listing is complete and what an incomplete listing does.
6. `changelog.d/5443-merge-train-complete-run-listing.md` — security fragment per §20.

## Files & Modules

- `scripts/review_merge_train.sh`
- `tests/test_review_merge_train.py`
- `README.md`
- `changelog.d/5443-merge-train-complete-run-listing.md` [new]

## Tests

- Unit (bash via fake `gh`): `tests/test_review_merge_train.py`, new cases listed in step 4 plus every existing case.
- Contract: `tests/test_review_dispatch_default_branch.py` still finds the `pr:<N>` name capture in the script.
- Lint: `bash -n scripts/review_merge_train.sh`; `shellcheck` with the repo's CI settings.

## Risks & Mitigations

- A burst of more than 1,000 active runs in one status keeps the listing incomplete and holds every queued PR. — ACCEPTED: the train only ever delays a review (its documented contract), and the hold lifts on the first invocation whose listing completes.
- Runs change status while the three queries run. — Queried in lifecycle order so a forward move is seen at least once; a run created after the snapshot was already outside the previous design's snapshot too, and the label-removal claim still prevents two releases racing.
- Extra API calls on busy ticks. — Lazy fetch: 0 calls when nothing is queued (previously 1), 3 when something is.

## Rollout

Ships with the project branch's final PR into its base, then to consumer repos on the next `@stable` release through the reusable workflows' `SUPPORT_SCRIPTS_DIR` checkout. No flag: `MERGE_TRAIN_ENABLED=false` still disables the whole train. Rollback is reverting the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should the active-run listing become complete? — Picked: A — three status-filtered repo-wide listings (`pending`, `queued`, `in_progress`), paged to `total_count`, at most 10 pages each. Alternatives: B — paginate the unfiltered `actions/runs` listing; C — one listing per review workflow per status (9 calls). Why: active runs are few, `total_count` proves completeness, and it keeps the call count at 3. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What does `release` do when the listing is incomplete? — Picked: A — leave every queued PR queued this invocation and retry on the next close event or poll tick. Alternatives: B — keep releasing without the guard (today's behaviour). Why: the file's fail-open contract already says release does nothing on API failure, and releasing blind is the defect. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How is the `@<ref>` suffix handled? — Picked: A — strip it with `sub("@.*$"; "")` before the existing regex. Alternatives: B — extend the regex with `(@.*)?$`. Why: one transform keeps the regex identical to the other matchers in the repo. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] When is the listing fetched? — Picked: A — lazily, once, on the first queued PR that passes the base filter. Alternatives: B — eagerly before the loop, as today. Why: §15, the new listing costs 3 calls, and most ticks have no queued PR. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Fix the same path regex in `scripts/gh_helpers.sh` too? — Picked: A — no, record it for a separate issue. Alternatives: B — include both helpers in this PR. Why: §5; the finding names the merge train and those helpers belong to a different flow with their own tests. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Which statuses count as active? — Picked: A — keep `pending`, `queued`, `in_progress`, the three the filter already uses. Alternatives: B — also `waiting` and `requested` (5 calls). Why: §5, no semantic widening; the review workflows use no deployment environments that would hold a run in `waiting`. Applied in: phase 1. Status: pending review

## Notes

- Security pass skipped: `security_pass_skip.py` reported `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Candidate follow-up (AD-5): `scripts/gh_helpers.sh:1264` and `:1388` match `.path` with the same regex and would miss an `@<ref>`-suffixed path.

## References

- Issue #5443 (this finding), tracker #3576.
- Issue #4701 / final PR #4709 (the base project: default-branch review dispatches and `pr:<N>` run names).
- Issue #4927 and `docs/completed/issue-4927-paginate-review-dispatch-runs-plan.md` (the poller's paged listing, which left the merge train out of scope).
