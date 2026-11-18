# Dispatch the review sweep from the default branch only

Source issue: shubhodeep1/coding-workflows#4618 (https://github.com/shubhodeep1/coding-workflows/issues/4618)
Base branch: main
Security pass: skip (ai:security: automation-produced issue)

## Summary

`review_autofix_sweep.yml` dispatches `internal-review.yml` with `--ref <PR head branch>` for every same-repository PR, so the scheduled sweep runs whatever `internal-review.yml` the unmerged branch carries, with `secrets: inherit` and the sweep's `GH_PAT`. This plan fixes security finding `review-dispatches-unmerged-workflow` (A08:2021, critical): the sweep dispatches from the protected default branch only, passes a validated PR number as data, and deduplicates active runs by PR instead of by workflow ref.

## Context

- Finding: issue #4618, `.github/workflows/review_autofix_sweep.yml:299` (`dispatch_args+=(--ref "${head_ref}")`). Refs #3576 (security-audit follow-up tracker).
- The head-ref dispatch was added for the PR #3895 incident (2026-08-29): a default-branch dispatch has `head_branch = main`, so the sweep snapshot and the poller's `_has_active_autofix_run` (both keyed by head branch) could not see it and re-dispatched every cycle. `tests/test_conflict_dispatch_active_run_visibility.py` pins that contract, and `agents.md` ("Workflow file size limit" neighbourhood, the stale-queued paragraph) documents it.
- Dispatching from `main` is already supported: `review_autofix.yml`'s "Checkout PR head branch" step switches to the PR head from the PR's metadata, fork PRs have always been dispatched from `main`, and both concurrency groups (`internal-review-dispatch-pr-<N>`, `pr-autofix-<N>`) are keyed by PR number, not by ref.
- Precedent for identifying a run by its name: `test-and-mark-stable.yml` sets `run-name: … [cycle:<id>]` and `scripts/promote_main_cycle.sh` matches `display_title` exactly.
- GitHub docs: "If `run-name` is omitted or is only whitespace, then the run name is set to event-specific information for the workflow run", and `run-name` may read the `github` and `inputs` contexts.

## Goals

- The sweep never passes `--ref` to `gh workflow run internal-review.yml`; every sweep dispatch runs the default branch's workflow file.
- The sweep refuses a PR number that is not a positive integer (`AUTOFIX_SWEEP_SKIP … reason=invalid_pr_number`).
- A `workflow_dispatch` run of `internal-review.yml` is named `Internal: AI Review & Autofix [pr:<N>]`; runs from every other event keep their default names.
- The sweep's active-run snapshot counts a dispatch run under its PR (`pr:<N>`) and every other run under its head branch, so a sweep-dispatched run still suppresses the next tick's duplicate for that PR, and a `main`-headed PR is not suppressed by unrelated dispatch runs.
- The poller's `_has_active_autofix_run` also sees active PR-named dispatch runs, so it does not duplicate-dispatch against a sweep run (the PR #3895 class).

## Non-goals

- The poller's own `_dispatch_review_for_conflicts` (`--ref <head_ref>`), `scripts/review_merge_train.sh` (`--ref "${head}"`), and `forward-merge-stable-to-main.yml` keep their dispatch refs (AD-1). The forward-merge branch is cut from `stable` by the workflow itself, so it carries no contributor-edited workflow file.
- No new `head_sha` workflow input (AD-3).
- No change to `review_autofix.yml` or consumer templates (`ai-review.yml`); the sweep is internal to this repo.

## Constraints

- §1: security first; §5: minimal change set; §6: log keys (`AUTOFIX_SWEEP_*`, `head_ref=`) and workflow input names stay; the new `reason=invalid_pr_number` value and the run name are additions.
- §9: YAML stays 2-space; the shell in `orchestrate_poll_process.sh` keeps tabs.
- §15: the sweep issues no new API calls. The poller adds at most one `gh run list` call per dispatch attempt, and only when the three head-branch lookups found nothing (audited: those lookups filter by `--branch <head_ref>` and cannot return a `main`-headed run).
- §20: security fix, so a `changelog.d/` fragment.
- §27: `internal-review.yml` (6,846 bytes) and `review_autofix_sweep.yml` stay far below 480,000 bytes.

## Approach

1. `internal-review.yml` gains `run-name: ${{ github.event_name == 'workflow_dispatch' && format('Internal: AI Review & Autofix [pr:{0}]', inputs.pr_number) || '' }}`. The empty string falls back to GitHub's event-specific default, so `pull_request` and `push` runs are unchanged.
2. The sweep drops the `--ref "${head_ref}"` branch of `dispatch_args` and validates `pr_number` before dispatching.
3. The sweep's jq reduce keys each active run by `pr:<N>` when `event == "workflow_dispatch"` and `display_title` is exactly `Internal: AI Review & Autofix [pr:<N>]`, else by `head_branch`. The per-PR preflight checks both `<wf>:<head_ref>` and `<wf>:pr:<N>`. The `event` requirement keeps a PR author from suppressing another PR by titling their own PR with the marker. Git refs cannot contain `:`, so `pr:<N>` never collides with a branch key.
4. `_has_active_autofix_run` gains one `gh run list --workflow internal-review.yml --event workflow_dispatch --limit 100 --json status,displayTitle` lookup that counts active runs whose `displayTitle` is exactly the PR's name. It fails open to `0` like the existing lookups (a consumer repo without `internal-review.yml` gets a non-retryable error, not a retry loop).

Alternatives are recorded under Auto-decisions.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the run name, the sweep dispatch, and both dedupe guards have to change together, because a sweep that dispatches from `main` without PR-keyed dedupe brings back the PR #3895 duplicate-dispatch loop.

1. **Phase 1: default-branch sweep dispatch with PR-keyed dedupe.**
   - Files: `.github/workflows/internal-review.yml`, `.github/workflows/review_autofix_sweep.yml`, `scripts/orchestrate_poll_process.sh`, `tests/test_conflict_dispatch_active_run_visibility.py`, `tests/test_review_autofix_sweep_stale_queued.py`, `agents.md`, `changelog.d/4618-sweep-dispatch-default-branch.md` [new].
   - Done: the sweep has no `--ref` on its dispatch; the tests below pass; `ci.yml`'s steps for the three sweep/dispatch test files pass; YAML parses.
   - Rollback: revert the PR. The previous head-ref dispatch comes back, with the finding.

## Implementation Steps

1. `internal-review.yml`: add the `run-name` line under `name:` with a comment naming issue #4618 and the two consumers of the name.
2. `review_autofix_sweep.yml`:
   - header comment: replace the head-ref rationale with the default-branch rule and the PR-keyed dedupe;
   - `snapshot_active_review_runs` jq: add a `dedupe_key` def and use it for `active` and `stale`;
   - per-PR loop: validate `pr_number` (`^[1-9][0-9]*$`), check `<wf>:pr:<N>` next to `<wf>:<head_ref>`;
   - dispatch: remove the `--ref` branch and rewrite the comment.
3. `scripts/orchestrate_poll_process.sh` `_has_active_autofix_run`: after the head-branch loop, the PR-named lookup for `internal-review.yml`, guarded by a numeric check on `pr_number`; update the header comment.
4. Tests:
   - `tests/test_conflict_dispatch_active_run_visibility.py`: replace `SweepDispatchRefContract` (head-ref) with a contract that the sweep never passes `--ref`, validates the PR number, and checks the PR key; add a contract for the `run-name` and for the poller's PR-named lookup;
   - `tests/test_review_autofix_sweep_stale_queued.py`: add behavioural jq cases (dispatch run with the marker → `pr:<N>`; marker on a `pull_request` run → branch key; dispatch run without the marker → branch key; stale PR-keyed run logged with its key).
5. `agents.md`: rewrite the head-ref sentence of the stale-queued paragraph.
6. `changelog.d/4618-sweep-dispatch-default-branch.md` with `<!-- changelog: security -->`.

## Files & Modules

- `.github/workflows/internal-review.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `scripts/orchestrate_poll_process.sh`
- `tests/test_conflict_dispatch_active_run_visibility.py`
- `tests/test_review_autofix_sweep_stale_queued.py`
- `agents.md`
- `changelog.d/4618-sweep-dispatch-default-branch.md` [new]

## Testing

- `python3 tests/test_conflict_dispatch_active_run_visibility.py`, `python3 tests/test_review_autofix_sweep_stale_queued.py`, `python3 -m pytest tests/test_review_autofix_sweep_zero_candidate_fast_exit.py`.
- `bash -n scripts/orchestrate_poll_process.sh`; YAML parse of both workflows; `actionlint` when available.
- Any other test that greps `review_autofix_sweep.yml`, `internal-review.yml`, or `_has_active_autofix_run` (found with `grep -rl`).

## Risks

- A sweep-dispatched run older than the 100 most recent `internal-review.yml` dispatch runs is invisible to the poller's PR-named lookup. The cost is one duplicate dispatch that queues behind the running peer in the PR-keyed concurrency group; it cannot cancel the running peer.
- Dispatch runs from before this change have no `[pr:<N>]` name and stay branch-keyed. They age out within one run.
- The poller, `review_merge_train.sh`, and the forward-merge fallback still dispatch at a head ref (AD-1). They are the same class as this finding and are recorded as a lesson for the security audit's next pass.

## Rollout

Ships with the final PR into `main`. The sweep and `internal-review.yml` run from `main`, so the change takes effect on the first sweep tick after the merge. Nothing is synced to consumer repos.

## Auto-decisions

- AD-1 [plan, 2026-09-27] Which dispatch sites does the fix cover? — Picked: A — the sweep dispatch the finding names, plus PR-named run detection in the poller's `_has_active_autofix_run` so its dedupe keeps seeing sweep runs. Alternatives: B — also move `_dispatch_review_for_conflicts`, `review_merge_train.sh`, and the forward-merge fallback to the default branch; C — the sweep only, leaving the poller blind to sweep runs. Why: A closes the reported path without reintroducing the PR #3895 duplicate loop; B would also need the PR-named run in the consumer `ai-review.yml` template and a sync before its dedupe works. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] How are active runs deduplicated by PR? — Picked: A — a `workflow_dispatch` `run-name` of `Internal: AI Review & Autofix [pr:<N>]`, matched exactly together with `event == workflow_dispatch`. Alternatives: B — read each active run's jobs or inputs (one API call per run, §15); C — drop the active-run guard and rely on the concurrency groups (brings back the PR #3895 pending-run churn). Why: no new API calls in the sweep, and the repo already identifies release runs this way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Pass the head SHA as data as well as the PR number? — Picked: A — pass only the validated PR number; `review_autofix.yml` already resolves and pins the live PR head in its gate and checkout. Alternatives: B — add a `head_sha` input to `internal-review.yml` and thread it through `review_autofix.yml`. Why: the security fix is the ref the workflow file is loaded from; B changes the reusable workflow's interface (§5, §6) without closing more of the finding. Applied in: phase 1 PR. Status: pending review
