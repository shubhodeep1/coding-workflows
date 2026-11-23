# Keep PR-named sweep dispatch runs with a null head branch in the active-run snapshot

Source issue: shubhodeep1/coding-workflows#4928 (https://github.com/shubhodeep1/coding-workflows/issues/4928)
Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

The security audit found `sweep-discards-null-head-dispatch` (medium, STRIDE: Denial of Service) at `.github/workflows/review_autofix_sweep.yml:217`. The sweep's active-run snapshot drops every run whose `head_branch` is null or empty **before** it computes the `pr:<N>` dedupe key. A default-branch `workflow_dispatch` run named `Internal: AI Review & Autofix [pr:<N>]` is keyed by that name, not by its head branch. When GitHub reports its `head_branch` as null, the run is dropped, the PR looks idle, and the next sweep tick dispatches the same PR again. That new run replaces the pending review in the `review_autofix` concurrency group. This plan keeps such a run in the snapshot under its `pr:<N>` key, and still drops runs that have neither a head branch nor a verified PR key.

## Context

- `snapshot_active_review_runs` in `.github/workflows/review_autofix_sweep.yml` pipes the `queued`, `in_progress`, and `pending` run listings for `internal-review.yml` and `review_autofix.yml` into one `jq -c -s` program. That program:
  - defines `dispatch_pr`, which returns the PR number only for a `workflow_dispatch` run whose `display_title` exactly matches `^Internal: AI Review & Autofix \[pr:([1-9][0-9]*)\]$`;
  - defines `dedupe_key`, which is `pr:<N>` when `dispatch_pr` is set and `.head_branch` otherwise;
  - then filters `select((.head_branch // "") != "")` before either key is used, and reduces the rest into `active` counts and `stale` log entries.
- Issue #4618 added `dispatch_pr` / `dedupe_key` when the sweep moved to default-branch dispatch. Issue #4701's project (this project's base) extended the same PR-named matching to the poller and the merge train.
- `head_branch=null` on `workflow_dispatch` runs is a known GitHub behaviour in this repo. `README.md` lists it among the reasons the cached stall-recovery scan can miss a live run.
- The other PR-named lookups already handle a null head branch:
  - the three `scripts/orchestrate_poll_process.sh` jq filters OR the PR-name match with the head-branch match;
  - `_mt_inflight_review_branches` in `scripts/review_merge_train.sh` emits `.head_branch // empty` and the `pr:<N>` key independently.
  Only the sweep filters first (AD-1).
- `tests/test_review_autofix_sweep_stale_queued.py` extracts the jq program verbatim from the workflow and runs it. Its `test_runs_without_a_head_branch_are_ignored` pins the drop for non-dispatch runs, and `NamedDispatchRunKeyTests` covers the `pr:<N>` key.

## Goals

- A `workflow_dispatch` run whose title carries a valid PR key counts under `pr:<N>` in `active`, or in `stale` when it is wedged, whatever its `head_branch` is (null, missing, or empty).
- A run with no head branch and no verified PR key is still dropped, as the finding recommends.
- Keying is unchanged for every run that has a head branch.
- Tests pin the new behaviour. `agents.md` and a `changelog.d/` fragment record it.

## Non-goals

- No change to the poller, the merge train, or `check_in_status.py`. They already match PR-named runs without requiring a head branch (AD-1).
- No change to the dispatch itself, the run names, or the `SWEEP_STALE_QUEUED_MINUTES` cutoff.
- No key for a run that has neither a head branch nor a PR name, for example by `head_sha` (AD-2).

## Constraints

- §1 / §3: the change may only make the snapshot see **more** active runs. It never discounts a run that counts today.
- §5: one `select` expression plus its comment in the workflow. No new identifiers, inputs, env vars, or log keys (§6).
- §9: 2-space YAML; the tests keep the file's tab indentation.
- §15: no new API call. The fix works on the listing the sweep already fetches.
- §18: no new script; the sweep already runs on its cron.
- §20: one fragment, `changelog.d/4928-sweep-null-head-dispatch.md`, section `security` (AD-3).
- §27: `review_autofix_sweep.yml` is about 21.5 KB, far below the 480,000-byte guard.

## Approach

Replace the pre-filter

```jq
| select((.head_branch // "") != "")
```

with

```jq
| select((.head_branch // "") != "" or dispatch_pr != "")
```

`dispatch_pr` already requires `event == "workflow_dispatch"` and an exact, anchored title with a positive integer, so PR text cannot forge a key: the title of a dispatched run comes from the default branch's `run-name`. Every surviving run then has a non-empty `dedupe_key`: `pr:<N>` for a named dispatch, and the head branch otherwise. The `group_by(.id)` status merge, the `active` reduce, and the `stale` reduce are unchanged. Update the comment above the filter to say why a PR-keyed run survives without a head branch.

## Phases & Merge Strategy

This is a single-phase plan: issue mode (CLAUDE.md §28.A) authorises it, because `/implement-issue-claude` always turns one issue into one phase.

1. **Phase 1 — keep PR-keyed dispatch runs with a null head branch in the sweep snapshot.**
   - Files: `.github/workflows/review_autofix_sweep.yml`, `tests/test_review_autofix_sweep_stale_queued.py`, `agents.md`, `changelog.d/4928-sweep-null-head-dispatch.md` [new].
   - Done when:
     - the filter keeps a run with a verified PR key whatever its `head_branch` is, and drops a run that has neither;
     - the new and existing tests in `tests/test_review_autofix_sweep_stale_queued.py`, `tests/test_review_dispatch_default_branch.py`, and `tests/test_review_autofix_sweep_zero_candidate_fast_exit.py` pass;
     - `actionlint` (when available) and `yamllint -c .yamllint.yml` accept the workflow.
   - Rollback: revert the phase PR. The snapshot then drops null-head runs again, as it does today.

## Implementation Steps

1. `.github/workflows/review_autofix_sweep.yml`: change the pre-filter as above and extend its comment.
2. `tests/test_review_autofix_sweep_stale_queued.py`, in `NamedDispatchRunKeyTests`:
   - a named dispatch run with `head_branch: null`, one with the key missing, and one with `""` each count as `pr:<N>` in `active`;
   - a wedged named dispatch run with a null head branch is logged in `stale` under `pr:<N>`;
   - a `workflow_dispatch` run with a null head branch and an unnamed, malformed (`[pr:0]`, `[pr:12x]`), or non-dispatch title (`pull_request` event with the marker title) is still dropped.
3. `agents.md`: one sentence in the sweep dispatch paragraph saying the snapshot keeps a PR-named run whose `head_branch` is null (issue #4928).
4. `changelog.d/4928-sweep-null-head-dispatch.md` with a `security` entry.

## Files & Modules

- `.github/workflows/review_autofix_sweep.yml`
- `tests/test_review_autofix_sweep_stale_queued.py`
- `agents.md`
- `changelog.d/4928-sweep-null-head-dispatch.md` [new]

## Tests

- **Unit, extracted jq** (`tests/test_review_autofix_sweep_stale_queued.py`): the cases in step 2. The existing `test_runs_without_a_head_branch_are_ignored` stays unchanged, because its runs carry no event and no PR title.
- **Local verification**: `python3 tests/test_review_autofix_sweep_stale_queued.py`, `python3 -m unittest tests.test_review_dispatch_default_branch tests.test_review_autofix_sweep_zero_candidate_fast_exit`, `yamllint`, `actionlint` when installed, and `wc -c` on the workflow (§27).

## Risks & Mitigations

- A PR-keyed run that has a null head branch now suppresses a dispatch it did not suppress before. ACCEPTED: that is the fix. A wedged `queued` run still ages out through `SWEEP_STALE_QUEUED_MINUTES`, and an `in_progress` or `pending` run is bounded by the 240-minute job timeout, the same as for a run with a head branch.
- A forged key. Mitigated: `dispatch_pr` needs `event == "workflow_dispatch"` and the exact anchored title, which only the default branch's `internal-review.yml` `run-name` produces.

## Rollout

No flag: the change applies when the sweep next runs from the branch that carries it. This repo gets it when the project chain reaches `main`, through #4701's and #4618's final PRs. The sweep is not synced to consumer repos. Roll back by reverting the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] The finding names only the sweep; should the other PR-named run lookups change too? — Picked: A — sweep only. Alternatives: B — also rewrite the poller and merge-train lookups. Why: re-reading them shows they already match a PR-named run whatever its head branch (the three `orchestrate_poll_process.sh` filters OR the PR match, and `_mt_inflight_review_branches` emits the `pr:<N>` key on its own), so B changes nothing and breaks §5. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] How should a run with no head branch and no PR key be handled? — Picked: A — keep dropping it, as the finding recommends. Alternatives: B — key it by `head_sha`; C — count it under a shared placeholder key. Why: B needs a per-PR head SHA the snapshot does not have, and would add keys no PR check reads. C would suppress unrelated PRs. A matches the recommendation and today's behaviour for those runs. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which changelog section does the fragment use? — Picked: A — `security`. Alternatives: B — `fixed`. Why: the change closes a security-audit finding (STRIDE: Denial of Service), and §20.B lists `security` for exactly that. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The base branch is the head of #4701's draft final PR #4709, which targets #4618's project branch. Neither has merged as of 2026-09-29.
