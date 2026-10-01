# Merge train: trust a PR-named review run only from the default branch and its wrapper

Source issue: shubhodeep1/coding-workflows#5840 (https://github.com/shubhodeep1/coding-workflows/issues/5840)
Base branch: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable
Security pass: skip (ai:security: automation-produced issue)

## Summary

`_mt_inflight_review_branches` in `scripts/review_merge_train.sh` turns any active `workflow_dispatch` review run titled `AI Review [pr:<N>]` or `Internal: AI Review & Autofix [pr:<N>]` into the key `pr:<N>`, whatever branch the run was dispatched on. A run's name comes from the workflow file at the dispatched ref, so a branch writer can keep such a run active on their own branch and the merge train's `release` leaves PR N queued (`MERGE_TRAIN_RELEASE_ACTIVE … action=leave_queued`) for as long as they like. Emit the `pr:<N>` key only for a run dispatched on the repository's default branch, from the wrapper that sets that exact name.

## Context

- Security finding `merge-train-release-spoofed-by-branch-run` (issue #5840, medium, `scripts/review_merge_train.sh:413`), filed by the audit of this base branch.
- The `pr:<N>` key came with issue #4898's default-branch dispatch (merged into this base branch): `_mt_dispatch_review` now dispatches from the default branch, so the run's `head_branch` is the default branch and the merge train needs the run name to tie it to the PR.
- Issue #5094 fixed the same trust gap in `scripts/orchestrate_poll_process.sh`: a PR-named run counts only when `event == "workflow_dispatch"`, `head_branch` is the default branch, and `path` is `.github/workflows/internal-review.yml` (internal name) or `.github/workflows/ai-review.yml` (consumer name). Its changelog (`changelog.d/5094-verify-pr-named-review-run-provenance.md`) lists `scripts/review_merge_train.sh` as outside that fix. This plan applies the same rule there.
- The wrappers set the names: `.github/workflows/internal-review.yml` line 8 (`Internal: AI Review & Autofix [pr:{0}]`) and `workflow-templates/ai-review.yml` line 12 (`AI Review [pr:{0}]`).
- `_mt_release` already lists every open PR once (`_mt_list_open_prs ""`, REST `pulls?state=open`). Each PR object carries `base.repo.default_branch`, so the default branch is available without a new API call (§15).

## Goals

- A `workflow_dispatch` run named `AI Review [pr:<N>]` or `Internal: AI Review & Autofix [pr:<N>]` produces the `pr:<N>` key only when its `head_branch` equals the repository's default branch **and** its `path` is the wrapper that sets that name (`.github/workflows/ai-review.yml` for `AI Review …`, `.github/workflows/internal-review.yml` for `Internal: AI Review & Autofix …`).
- A run with the right name on any other branch, or from any other workflow path, never holds a queued PR back.
- A legitimate default-branch dispatch for PR N still holds PR N back (`MERGE_TRAIN_RELEASE_ACTIVE`), as in issue #4701.
- Head-branch keys are unchanged: an active review run on the PR's own head branch still holds it back.
- The release path issues no new API call (§15).
- When the default branch cannot be determined, no `pr:<N>` key is emitted and one structured log line says so.

## Non-goals

- The other PR-named matchers named in #5094's changelog (`_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, `review_autofix_sweep.yml`, `.claude/scripts/check_in_status.py`) are not touched. Issue #5840 names only the merge train (AD-3).
- No change to `_mt_dispatch_review`, the gate subcommand, labels, markers, or any env var.
- No change to which workflow files count for head-branch keys (`review_autofix`, `internal-review`, `ai-review`).

## Constraints

- §1: security first. The fix narrows what the train trusts; it never widens it.
- §5: minimal change. Only `_mt_list_open_prs`'s projection (one added field), `_mt_inflight_review_branches`, and its call in `_mt_release` change.
- §6: no rename. `_mt_inflight_review_branches` keeps its name; it gains an optional argument. The `_mt_list_open_prs` JSON lines gain a `default_branch` field; existing fields are unchanged, and every reader picks fields by name.
- §15: no new API call; the default branch comes from the open-PR listing the release already makes.
- §9: tabs in the shell script and the Python test.
- §20: a `changelog.d/` fragment (security fix).

## Approach

1. `_mt_list_open_prs` adds `default_branch: (.base.repo.default_branch // "")` to each JSON line.
2. `_mt_release` reads the first non-empty `default_branch` from that listing and passes it to `_mt_inflight_review_branches`. When none is found, it logs `MERGE_TRAIN_PR_NAMED_PROVENANCE repo=<repo> outcome=default_branch_unresolved pr_named_matching=disabled` and passes an empty value.
3. `_mt_inflight_review_branches [default_branch]` keeps printing each active review run's `head_branch`. It prints `pr:<N>` only when the run is a `workflow_dispatch`, the default branch is non-empty and equals `head_branch`, and (`display_title`, `path`) is exactly (`Internal: AI Review & Autofix [pr:<N>]`, `.github/workflows/internal-review.yml`) or (`AI Review [pr:<N>]`, `.github/workflows/ai-review.yml`), with `<N>` a positive integer without leading zeros.

Alternatives considered: a separate `GET repos/<repo>` for the default branch (one more API call, AD-1), and treating every queued PR as active when the default branch is unknown (it would stall the whole queue, AD-2).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is one function and its tests.

1. **Phase 1 — default-branch and wrapper provenance for the merge train's PR-named key.**
   - Files: `scripts/review_merge_train.sh`, `tests/test_review_merge_train.py`, `README.md`, `changelog.d/5840-merge-train-pr-named-run-provenance.md`.
   - Done when: the new tests pass (forged run on a non-default branch, wrong path, wrong path/title pairing, unresolved default branch), the existing #4701 test still holds the PR back for a genuine default-branch dispatch, and `python3 -m pytest tests/test_review_merge_train.py` plus `bash -n` and `shellcheck` (when available) on the script are clean.
   - Rollback: revert the PR. The previous behaviour (title-only keying) returns; nothing else depends on the change.

## Implementation Steps

1. `scripts/review_merge_train.sh` `_mt_list_open_prs`: add `default_branch: (.base.repo.default_branch // "")` to the `--jq` projection.
2. `scripts/review_merge_train.sh` `_mt_inflight_review_branches`: take the default branch as `$1` (default empty), pass it with `--arg`, and replace the title-only capture with the default-branch, event, path, and exact-title check. Update the comment above it to state the rule and cite issues #5840 and #5094.
3. `scripts/review_merge_train.sh` `_mt_release`: derive the default branch from `prs_json`, log the unresolved case, and pass it to `_mt_inflight_review_branches`. Update the release API-budget comment only if its wording becomes wrong.
4. `tests/test_review_merge_train.py`: give `_pr` a `base.repo.default_branch` (default `main`) and add the tests listed under Tests.
5. `README.md`: extend the merge train `release` row to say a PR-named run counts only from the default branch and its wrapper.
6. `changelog.d/5840-merge-train-pr-named-run-provenance.md` `[new]`: a `security` fragment per §20.

## Files & Modules

- `scripts/review_merge_train.sh`
- `tests/test_review_merge_train.py`
- `README.md`
- `changelog.d/5840-merge-train-pr-named-run-provenance.md` `[new]`

## Tests

Unit (fake `gh`, `tests/test_review_merge_train.py`):
- Existing `test_release_leaves_pr_named_dispatch_run_queued_without_dispatch` still passes (default-branch `AI Review [pr:4077]` from `ai-review.yml` holds PR 4077).
- New: an internal-name run from `.github/workflows/internal-review.yml` on the default branch holds the PR.
- New: the same `AI Review [pr:4077]` run with `head_branch` = an attacker branch does not hold the PR; it is released and dispatched.
- New: a default-branch run with the right title but a different path (for example `.github/workflows/evil.yml`, or the title of one wrapper with the other wrapper's path) does not hold the PR.
- New: when the PR listing carries no default branch, the PR-named key is disabled (`outcome=default_branch_unresolved` logged) while a head-branch run still holds its PR.

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_review_merge_train.py -q`, `bash -n scripts/review_merge_train.sh`, `shellcheck scripts/review_merge_train.sh` if installed, and the other suites that source or grep this script (`tests/test_review_dispatch_default_branch.py`, `tests/test_retrigger_default_branch_dispatch.py`).

## Risks & Mitigations

- A legitimate default-branch review run is not recognised when the default branch cannot be read, so the train may dispatch a second review for that PR. ACCEPTED: the open-PR listing that yields the default branch is the same listing that yields the queued PRs, so it is only empty when nothing is queued; the outcome is logged, and a duplicate dispatch only repeats a review (the train's fail-open contract).
- A consumer whose wrapper lives at a different path than `.github/workflows/ai-review.yml` loses the PR-named hold. ACCEPTED: the sync installs the wrapper at that path, and #5094 already depends on it in the poller.

## Rollout

Ships with the base branch's project and reaches consumer repos on the next `@stable` sync. No flags, no data migration. Rollback is a revert.

## References

- Issue #5840 (this finding), issue #5094 / `changelog.d/5094-verify-pr-named-review-run-provenance.md` (same rule in the poller), issues #4701, #4898 (default-branch dispatch), #4618.

## Auto-decisions

- AD-1 [plan, 2026-10-01] Where does the release path get the default branch? — Picked: A — from `base.repo.default_branch` in the open-PR listing it already makes. Alternatives: B — one extra `GET repos/<repo>`; C — a new env var set by the calling workflows. Why: no new API call (§15) and no workflow or env change (§5); every PR in that listing carries it. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What happens when the default branch cannot be determined? — Picked: A — emit no `pr:<N>` keys, log `MERGE_TRAIN_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved`, keep head-branch keys. Alternatives: B — hold every queued PR back; C — assume `main`. Why: A never trusts an unverified name (§1) and keeps the train's fail-open contract; B stalls the queue, C trusts a guess. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Fix the other PR-named matchers (`gh_helpers.sh`, `review_autofix_sweep.yml`, `check_in_status.py`) too? — Picked: A — no, only the merge train the finding names. Alternatives: B — fix all four in this PR. Why: the issue names one location; the others have their own callers and tests and are a separate change (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] How strict is the workflow path check? — Picked: A — exact `.github/workflows/internal-review.yml` / `.github/workflows/ai-review.yml`, each paired with its own run name, as in #5094. Alternatives: B — any path ending in the wrapper file name. Why: a suffix match would accept a lookalike path; the exact pairing matches the poller's rule. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
