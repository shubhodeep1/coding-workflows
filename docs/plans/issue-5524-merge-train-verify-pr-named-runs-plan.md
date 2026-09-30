# Merge train: count a PR-named review run as active only when it came from the default branch and its wrapper

Source issue: shubhodeep1/coding-workflows#5524 (https://github.com/shubhodeep1/coding-workflows/issues/5524)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/review_merge_train.sh release` keys every active `workflow_dispatch` review run whose title reads `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]` as `pr:<N>` and leaves PR `<N>` queued. A run title is evaluated from the workflow file at the dispatched ref, so a run dispatched from an altered branch can name any queued PR and hold it in the queue (security finding `merge-train-accepts-spoofed-pr-review-run`, #5524). This plan derives `pr:<N>` keys only from the exact wrapper path / run-name pairs dispatched on the default branch, and ignores unverified runs for release deduplication.

## Context

- `_mt_inflight_review_branches` (`scripts/review_merge_train.sh:402-416` on the base branch) lists the newest 100 Actions runs once per release tick and prints, for each queued / pending / in-progress run whose path ends in `review_autofix.yml`, `internal-review.yml`, or `ai-review.yml`:
  - its `head_branch`, and
  - for a `workflow_dispatch` run, `pr:<N>` when its `display_title` matches `^(Internal: AI Review & Autofix|AI Review) \[pr:<N>\]$`, whatever its head branch or path.
- `_mt_release` (`scripts/review_merge_train.sh:441-458`) skips a queued PR whose `pr:<N>` or head branch is in that list (`MERGE_TRAIN_RELEASE_ACTIVE … action=leave_queued`).
- #4701 introduced the `pr:<N>` key because default-branch dispatches (#4618, #4701) have the default branch as `head_branch`. #5094 already closed the same spoofing gap in the poller (`scripts/orchestrate_poll_process.sh`): a PR-named run counts only when its event is `workflow_dispatch`, its `head_branch` is the default branch, and its `path` is the wrapper that sets that name. Its changelog fragment (`changelog.d/5094-verify-pr-named-review-run-provenance.md`) lists `scripts/review_merge_train.sh` as outside that fix; this issue is that remaining site.
- Wrapper names: `.github/workflows/internal-review.yml:8` sets `Internal: AI Review & Autofix [pr:<N>]`; `workflow-templates/ai-review.yml:12` (synced to consumers as `.github/workflows/ai-review.yml`) sets `AI Review [pr:<N>]`. `review_autofix.yml` has no PR run name.
- `_mt_list_open_prs` (`scripts/review_merge_train.sh:184-192`) already calls `GET repos/<repo>/pulls?state=open…`, and every PR in that response carries `base.repo.default_branch` (verified live 2026-09-30).

## Goals

- A `pr:<N>` key is produced only for a `workflow_dispatch` run whose `head_branch` equals the repository's default branch and whose (`path`, `display_title`) is exactly (`.github/workflows/internal-review.yml`, `Internal: AI Review & Autofix [pr:<N>]`) or (`.github/workflows/ai-review.yml`, `AI Review [pr:<N>]`).
- A run named for PR `<N>` from any other branch, from any other workflow path, or with the other wrapper's name no longer holds PR `<N>` in the queue.
- Legitimate default-branch wrapper dispatches still hold their PR queued (the #4701 dedup guard keeps working).
- Head-branch keys are unchanged.
- No new GitHub API call (§15).

## Non-goals

- The other PR-named matchers the #5094 fragment lists as outside that fix: `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, `.github/workflows/review_autofix_sweep.yml`, and `.claude/scripts/check_in_status.py` (AD-3). One issue, one phase.
- The `gate` subcommand, which does not read Actions runs.
- The head-branch key and its path regex.

## Constraints

- §5 minimal change: only `_mt_inflight_review_branches`, the one line in `_mt_list_open_prs` that adds a field, and the call site in `_mt_release`.
- §6: no identifier is renamed or removed. `_mt_inflight_review_branches` keeps its name and output shape (one key per line); it gains an optional first argument. `MERGE_TRAIN_RELEASE_ACTIVE` keeps its fields. The open-PR row gains an additive `default_branch` field.
- §8: the unresolved-default-branch path logs a structured `MERGE_TRAIN_PR_NAMED_PROVENANCE` warning.
- §15: the default branch comes from the open-PR listing the release already makes; the recent-runs call is unchanged.
- §20: security fix, so one `changelog.d/` fragment.
- Fail-open contract of the script (header): an unresolved default branch never blocks the release.

## Approach

1. `_mt_list_open_prs` adds `default_branch: .base.repo.default_branch` to each row (AD-1).
2. `_mt_release` reads the first non-empty `default_branch` from its `prs_json` and passes it to `_mt_inflight_review_branches`. When it is empty and the list is non-empty, it logs `MERGE_TRAIN_PR_NAMED_PROVENANCE repo=<repo> outcome=default_branch_unresolved pr_named_matching=disabled` as a `::warning::` and continues (AD-2).
3. `_mt_inflight_review_branches [default_branch]` keeps the same `gh api` call but has `--jq` return the active review-path runs as a JSON array of `{head_branch, event, display_title, path}`; a local `jq --arg default_branch` then prints the head branch of each run, plus `pr:<N>` only when the run is a `workflow_dispatch` run on `$default_branch` and its path/title pair is one of the two exact pairs (AD-4). An empty `default_branch` produces no `pr:` key. A failed listing still returns non-zero so the caller's existing "continuing without the dispatch-dedup guard" path runs.

Alternatives: one extra `GET repos/<repo>` like the poller's `_pr_named_review_default_branch` (costs a call; AD-1 B); trusting a caller env var such as `DEFAULT_BRANCH` (rejected: callers set it with a `main` fallback, and a guessed branch must never vouch for a run).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is one function and its call site, and splitting it would ship an untested half.

1. **Phase 1 — verify PR-named run provenance in the merge-train release.**
   - Files: `scripts/review_merge_train.sh`, `tests/test_review_merge_train.py`, `tests/test_review_dispatch_default_branch.py`, `agents.md`, `README.md`, `changelog.d/5524-merge-train-verify-pr-named-runs.md` [new].
   - Done: the Goals hold, proven by the tests below; `tests/test_review_merge_train.py` and `tests/test_review_dispatch_default_branch.py` pass; `bash -n` and `shellcheck` (when installed) are clean on the script.
   - Rollback: revert the phase PR; the previous name-only matching returns.

## Implementation Steps

Phase 1:
1. `scripts/review_merge_train.sh` `_mt_list_open_prs`: add `default_branch: .base.repo.default_branch` to the `--jq` object.
2. `scripts/review_merge_train.sh` `_mt_inflight_review_branches`: take `$1` = default branch; split the listing (`gh api … --jq` array of the four fields) from the key derivation (local `jq -r --arg default_branch`), with the exact pair checks; update its header comment (issue #5524, provenance, identity).
3. `scripts/review_merge_train.sh` `_mt_release`: derive the default branch from `prs_json`, log `MERGE_TRAIN_PR_NAMED_PROVENANCE` when it is unresolved and the list is non-empty, and pass it to `_mt_inflight_review_branches`.
4. `scripts/review_merge_train.sh` header: note the default-branch source in the release API budget line.
5. Tests (below).
6. Docs: `agents.md` (the sentence on the merge-train release's `pr:<N>` keys) and `README.md` (the `release` row of the merge-train table).
7. `changelog.d/5524-merge-train-verify-pr-named-runs.md` with `<!-- changelog: security -->`.

## Files & Modules

- `scripts/review_merge_train.sh`
- `tests/test_review_merge_train.py`
- `tests/test_review_dispatch_default_branch.py`
- `agents.md`
- `README.md`
- `changelog.d/5524-merge-train-verify-pr-named-runs.md` [new]

## Tests

Unit (fake `gh`, `tests/test_review_merge_train.py`; the `_pr` fixture gains `base.repo.default_branch`):
- existing `test_release_leaves_pr_named_dispatch_run_queued_without_dispatch` still holds (default-branch `ai-review.yml` run named for the PR).
- new: an `internal-review.yml` default-branch run named `Internal: AI Review & Autofix [pr:<N>]` holds the PR.
- new: a PR-named `workflow_dispatch` run from a non-default branch does not hold the PR (the finding's exploit).
- new: a PR-named run whose path is not the wrapper that sets that name (swapped pair, `review_autofix.yml`, another workflow) does not hold the PR.
- new: when the open-PR list carries no default branch, PR-named runs do not hold the PR, the release proceeds, and `MERGE_TRAIN_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved` is logged; head-branch runs still hold their PR.
- existing tests for other PRs / events, empty head, claim, and dispatch keep passing.

Static (`tests/test_review_dispatch_default_branch.py::test_lookups_use_the_names_the_wrappers_set`): assert the merge train carries the two per-wrapper name patterns and the wrapper paths instead of the combined alternation.

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_review_merge_train.py tests/test_review_dispatch_default_branch.py`, plus the `ci.yml` step that covers them.

## Risks & Mitigations

- A default-branch review run named for a PR is not seen when the open-PR list lacks `base.repo.default_branch` → the release may dispatch beside it. ACCEPTED: the same fail-open contract the script already applies when the runs listing fails; `review_autofix.yml`'s concurrency group serialises the two runs, and the warning is logged.
- A consumer whose `ai-review.yml` predates the `[pr:<N>]` run name produces no `pr:` key → unchanged from today.
- Two-stage `jq` changes the fake-`gh` interaction → covered by the behavioural tests, which drive the real `--jq` filter through `jq`.

## Rollout

Ships with the base project's final PR. The script is staged from `shubhodeep1/coding-workflows` by `cancel_on_pr_close.yml` and `orchestrate_poll.yml`, so consumers pick it up on the next `@stable` release; no repo variable or wrapper change is needed. Rollback: revert the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where does the release get the default branch it checks PR-named runs against? — Picked: A — `base.repo.default_branch` from the open-PR listing `_mt_release` already makes. Alternatives: B — one extra `GET repos/<repo>` per release like the poller's `_pr_named_review_default_branch`; C — a caller-supplied env var. Why: no new API call (§15), and GitHub reports the field on every PR; C can carry a guessed `main`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens when the default branch cannot be resolved? — Picked: A — ignore PR-named runs for that release, log `MERGE_TRAIN_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved`, keep head-branch keys, continue. Alternatives: B — leave every queued PR queued for that tick. Why: matches the finding's recommendation ("ignore unverified runs for release deduplication") and the script's existing fail-open for an unreadable runs listing; B would recreate the denial of service on an API hiccup. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Fix the other PR-named matchers (`scripts/gh_helpers.sh`, `review_autofix_sweep.yml`, `check_in_status.py`) too? — Picked: A — no, only the merge train named by #5524. Alternatives: B — fix every matcher in this project. Why: one issue, one phase (`/implement-issue-claude` rules); the others are separate call sites with their own tests. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] Which (path, title) pairs are approved? — Picked: A — exactly (`.github/workflows/internal-review.yml`, `Internal: AI Review & Autofix [pr:<N>]`) and (`.github/workflows/ai-review.yml`, `AI Review [pr:<N>]`), as in the poller (#5094). Alternatives: B — also accept `review_autofix.yml` with either name. Why: `review_autofix.yml` sets no PR run name, so a PR-named run at that path is not one the wrappers made. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` (2026-09-30): `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- #5524 (this finding), #5094 (poller fix, `changelog.d/5094-verify-pr-named-review-run-provenance.md`), #4701 (`pr:<N>` keys), #4618, #4898 (base project), #3576 (audit tracker).
