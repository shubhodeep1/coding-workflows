# Dispatch review_autofix.yml's post-commit retrigger from the default branch, and let its peer check see PR-named runs

Source issue: shubhodeep1/coding-workflows#4898 (https://github.com/shubhodeep1/coding-workflows/issues/4898)
Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
Security pass: run

## Summary

`review_autofix.yml` still dispatches its follow-up review run at the pull request's head ref. It does this in two places: right after the editor pushed its own `[ai-autofix]` commit, and when an editor-changes-lost run re-dispatches itself. So the next run executes the PR branch's unmerged copy of the review workflow, with `secrets: inherit` and write permissions (finding `review-dispatches-unmerged-workflow`, #4618 / #4701). This plan moves both dispatches to the default branch with a validated PR number, preferring the PR-named review wrappers. It also teaches the two run probes those steps use to see the PR-named `workflow_dispatch` runs that default-branch dispatches produce.

## Context

- #4618 (sweep) and #4701 (poller `_dispatch_review_for_conflicts`, merge train `_mt_dispatch_review`, forward-merge fallback) moved every other review dispatch to the default branch. Their projects are still in flight: #4701's project branch holds both (final PR #4709 → #4618's branch, final PR #4634 → `main`). The issue's Ordering section says to build on #4701's project branch (AD-1).
- On that branch:
  - `.github/workflows/internal-review.yml` names `workflow_dispatch` runs `Internal: AI Review & Autofix [pr:<N>]` (#4618); `workflow-templates/ai-review.yml` names them `AI Review [pr:<N>]` (#4701).
  - `_pr_named_review_dispatch_runs` in `scripts/orchestrate_poll_process.sh` matches those exact names with one `gh run list --event workflow_dispatch --limit 100` call (#4701 AD-4). It lives in the poller, which `review_autofix.yml` does not source.
- Remaining head-ref dispatch sites in `.github/workflows/review_autofix.yml`, where `TARGET_BRANCH` is the PR's `HEAD_REF` (line 3534):
  1. **"Re-trigger review via workflow_dispatch"** (lines 6185–6384): `gh workflow run review_autofix.yml --ref "${TARGET_BRANCH}"`, then the caller workflow (`${{ github.workflow_ref }}`), or `ai-review.yml` / `internal-review.yml`, all with the same `--ref`. It runs right after the editor pushed its commit, and `allow_workflow_edits` defaults to true, so the dispatched run can execute workflow files that commit changed.
  2. **"Re-dispatch review on editor-changes-lost"** (lines 6479–6599): the identical chain with the same `--ref`. The issue does not list it, but `tests/test_review_autofix_review_pipeline_contract.py::test_editor_changes_lost_redispatch_matches_post_commit_fallback_chain` pins the two chains as identical, and the finding is the same (AD-3).
- Their probes in `scripts/gh_helpers.sh` list runs with `branch=<head branch>` only, so neither can see a default-branch dispatch run (`head_branch` and `head_sha` are the default branch's):
  - `autofix_retrigger_has_inflight_peer` (line 1219), used by both steps. It misses a live poller or sweep dispatch and queues a duplicate review run.
  - `autofix_changes_lost_head_retry_consumed` (line 1338), the per-head-SHA loop bound of the changes-lost re-dispatch. It counts completed review runs whose `head_sha` is the PR head. Once the retry itself is dispatched from the default branch, it can no longer see that retry, and a repeated changes-lost failure would re-dispatch without bound (AD-4).
- E2E sites the issue asks about: `.github/workflows/test-and-mark-stable.yml` lines 1352 and 2393 dispatch `${REVIEW_WORKFLOW_FILE}` with `--ref "${BRANCH}"` in `${TEST_REPO}` (default: this repository). `BRANCH` is `ai/issue-<N>` for the smoke issue the E2E job opened, which carries the E2E implement run's commits and the job's own bait commit. Phase 4 pins review runs to `head_sha == BAIT_SHA`, which only a dispatch at that branch produces (AD-6).
- `.github/workflows/review_autofix.yml` is 451,394 bytes. CLAUDE.md §27 splits at 480,000, and the issue asks that any body that grows move into `scripts/review_autofix_step_<slug>.sh` (AD-8).

## Goals

- Neither retrigger step passes `--ref` to `gh workflow run`. Each validates `PR_NUMBER` as `^[1-9][0-9]*$` before dispatching, and passes only that number plus `allow_workflow_edits` normalised to `true` / `false`.
- Both steps try the PR-named wrappers first: the caller workflow when it is `internal-review.yml` or `ai-review.yml`, then `ai-review.yml`, then `internal-review.yml`; then a differently named caller workflow; and `review_autofix.yml` last, because it has no PR run name (#4701 AD-10). A comment says why.
- `autofix_retrigger_has_inflight_peer` also counts queued or in-progress `workflow_dispatch` runs named for the PR, excluding the current run. It makes this one extra REST call only when the branch lookup found no peer and the PR number is valid, and it fails open as before.
- `autofix_changes_lost_head_retry_consumed` also counts completed, non-cancelled `workflow_dispatch` runs named for the PR that were created at or after the head's push-time bound. It makes this one extra REST call only when the branch lookup counted nothing, and it fails closed as before.
- Both E2E dispatch sites keep `--ref "${BRANCH}"` and carry a comment on whether they are exposed and why the ref stays.
- Both step bodies live in `scripts/review_autofix_step_post_commit_retrigger.sh` and `scripts/review_autofix_step_changes_lost_redispatch.sh`, so `review_autofix.yml` shrinks rather than grows.
- Tests cover the dispatch chains, both probes, and the step-script move. `changelog.d/4898-retrigger-dispatch-default-branch.md` carries a `security` entry.

## Non-goals

- No change to `_pr_named_review_dispatch_runs` or any other poller, sweep, or merge-train code (#4618 / #4701).
- No `run-name` for `review_autofix.yml` (#4701 AD-10).
- No change to the E2E harness's dispatch ref or its Phase 4 / 4b run matching (AD-6).
- No change to the rest of either step: the continuation settle delay, the peer-check bypass for continuation dispatches, the self-triggered skip, the Telegram and terminal-comment paths.

## Constraints

- §1: security first; the dispatched run must execute the default branch's workflow file.
- §5: minimal change; the moved bodies change only where this plan says.
- §6: no identifier, log prefix, or env var is renamed. `AUTOFIX_PEER_CHECK` and `AUTOFIX_CHANGES_LOST_BUDGET` keep every field and gain one appended field each (`peer_source=`, `pr_named_completed=`). New identifiers were checked unique: `_autofix_pr_named_review_runs`, `REVIEW_AUTOFIX_CALLER_WORKFLOW_REF`, `REVIEWED_HEAD_COMMIT_EPOCH`, `retrigger_candidates`, `retrigger_allow_workflow_edits`.
- §9: tabs in shell and Python, 2-space YAML.
- §15: each probe adds at most one REST call, only on the path where the branch lookup found nothing. A comment above each call names the call it audited.
- §18: no new script is run by hand. The two step scripts are sourced by the existing steps.
- §20: one `changelog.d/` fragment.
- §27: `review_autofix.yml` must end smaller than it started.

## Approach

1. **Dispatch chain.** In both step bodies, replace the three-stage `--ref "${TARGET_BRANCH}"` chain with one loop over `retrigger_candidates`, built as in Goals, calling `gh workflow run "<file>" -f pr_number=… -f allow_workflow_edits=…` with no `--ref`, so GitHub runs the default branch's workflow file. `review_autofix.yml` checks out the PR head from the PR's metadata either way, and its `pr-autofix-<N>` concurrency group is keyed by PR number, not by ref. The caller file name is read from `REVIEW_AUTOFIX_CALLER_WORKFLOW_REF` (step `env:` = `${{ github.workflow_ref }}`), reduced with `basename "${ref%%@*}"`, and used only when it matches `^[A-Za-z0-9._-]+\.ya?ml$`. The chain is written once and is byte-identical in both scripts, which the existing contract test checks.
2. **Shared PR-named lookup.** Add `_autofix_pr_named_review_runs <pr_number> [status]` to `scripts/gh_helpers.sh`: one `gh_retry gh api -X GET /repos/<repo>/actions/runs -f event=workflow_dispatch -f per_page=100 [-f status=<status>]`, filtered with jq to runs whose `path` is `internal-review.yml` / `ai-review.yml` and whose `display_title` is exactly `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]`. It prints `[{id, status, conclusion, created_at, path}]` and returns 1 on an invalid PR number, an API error, or bad JSON. It uses REST like its two callers; the poller's `gh run list` helper is not reachable from the workflow step (AD-5).
3. **Peer check.** After the branch lookup, when `peer_count` is 0 and the PR number is valid, call the helper (no status filter), count runs with status `queued`, `in_progress`, `pending`, `waiting`, or `requested` whose id is not the current run, and report `peer_source=branch|pr_named|none`. A failed call prints `AUTOFIX_PEER_QUERY_FAILED … reason=pr_named_api_error` on stderr and keeps returning 1 (fail open).
4. **Changes-lost budget.** Add an optional fifth argument, the head commit's epoch seconds. When the branch count is 0, compute the bound as the earlier of that epoch and the earliest `created_at` of any branch run on the head SHA (the push's `pull_request` run, cancelled twins included). Using the earlier value keeps a future-dated commit from moving the bound past the push. With no bound, an invalid PR number, or a failed call, it returns 0 (consumed, fail closed) with a reason. Otherwise it calls the helper with `status=completed` and adds the runs that are not cancelled, not the current run, and created at or after the bound. The step passes `REVIEWED_HEAD_COMMIT_EPOCH="$(git log -1 --format=%ct HEAD)"`.
5. **Step scripts.** Move both `run:` bodies verbatim, with the edits above, into the two new scripts. Follow the documented pattern: header comment, mode `0755`, no `${{ }}`, `error` wrapper, `REQUIRED_BOOTSTRAP_SCRIPTS`, the `tests/review_autofix_step_scripts.py` registry, `docs/INVENTORY.md`. The steps keep `name:`, `if:`, and `env:`, and gain `REVIEW_AUTOFIX_CALLER_WORKFLOW_REF`.
6. **E2E comments.** Above both `test-and-mark-stable.yml` dispatches, state that the site is exposed in the same way: the dispatched wrapper is `BRANCH`'s copy, run with `TEST_REPO`'s secrets. Say what writes that branch (the E2E implement run for the job's own smoke issue, and the bait commit). Say why the ref stays: the Phase 4 / 4b `head_sha == BAIT_SHA` pin, which a default-branch dispatch can never match.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — default-branch retrigger dispatch plus PR-named probes.** Files: see Files & Modules. Done when both retrigger scripts dispatch without `--ref` in the order above, both probes see PR-named runs with the call budgets above, both E2E sites carry the exposure comment, `review_autofix.yml` is smaller than 451,394 bytes, and the tests below pass. Rollback: revert the phase PR; nothing persists state.

## Implementation Steps

Phase 1:
1. `scripts/gh_helpers.sh`: add `_autofix_pr_named_review_runs` with a contract comment (input, output, one call, fail behaviour). Extend `autofix_retrigger_has_inflight_peer` (step 3) and `autofix_changes_lost_head_retry_consumed` (step 4), updating their header comments (inputs, return, API calls, §15 audit).
2. `scripts/review_autofix_step_post_commit_retrigger.sh` [new]: the "Re-trigger review via workflow_dispatch" body with the new chain.
3. `scripts/review_autofix_step_changes_lost_redispatch.sh` [new]: the "Re-dispatch review on editor-changes-lost" body with the new chain and the fifth budget argument.
4. `.github/workflows/review_autofix.yml`: replace both `run:` bodies with the resolving wrapper; add `REVIEW_AUTOFIX_CALLER_WORKFLOW_REF: ${{ github.workflow_ref }}` to both steps' `env:`.
5. `scripts/stage_workflow_support.sh` (`REQUIRED_BOOTSTRAP_SCRIPTS`), `tests/review_autofix_step_scripts.py` (registry), `docs/INVENTORY.md`: register both scripts.
6. `.github/workflows/test-and-mark-stable.yml`: exposure comments at both dispatch sites; no code change.
7. Tests (below); `README.md` "Autofix retrigger dedup" paragraph; `agents.md` note next to the #4701 dispatch notes; `changelog.d/4898-retrigger-dispatch-default-branch.md`.

## Files & Modules

- `.github/workflows/review_autofix.yml`
- `.github/workflows/test-and-mark-stable.yml` (comments only)
- `scripts/gh_helpers.sh`
- `scripts/review_autofix_step_post_commit_retrigger.sh` [new]
- `scripts/review_autofix_step_changes_lost_redispatch.sh` [new]
- `scripts/stage_workflow_support.sh`
- `tests/review_autofix_step_scripts.py`
- `tests/test_retrigger_default_branch_dispatch.py` [new]
- `tests/test_gh_helpers_list_runs_method.py`, `tests/test_editor_changes_lost_redispatch_budget.py`, `tests/test_review_autofix_review_pipeline_contract.py` (updated where they assert the old chain or read the raw workflow)
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/4898-retrigger-dispatch-default-branch.md` [new]

## Tests

- New `tests/test_retrigger_default_branch_dispatch.py`:
  - Neither step, expanded, contains `--ref` on a `gh workflow run` line. Both chains are identical. `review_autofix.yml` has no `${{ github.workflow_ref }}` left inside a moved body.
  - Functional: each script is sourced with a stub `gh` that records calls. It checks candidate order for an `internal-review.yml`, `ai-review.yml`, custom, or `review_autofix.yml` caller; no `--ref`; the first success stops the loop; an invalid PR number makes no dispatch call; an `ALLOW_WORKFLOW_EDITS` value other than `true` is sent as `false`.
  - Peer check against a `gh api` stub serving per-call fixtures: a PR-named in-flight run is found only when the branch lookup found none; no second call when the branch lookup found a peer; the current run and other PRs' runs are ignored; `ai-review.yml` names match; a second-call failure fails open.
  - Budget: a PR-named completed run after the bound consumes the budget; one before the bound, a cancelled one, or the current run does not; a future-dated head commit is bounded by the branch run's `created_at`; a missing bound or a failed call fails closed; no second call when the branch lookup already counted a run.
  - The helper pins `-X GET`.
- Updated existing tests keep their assertions, reading the expanded workflow text where they read the raw file.
- Local run: the new and updated modules, `tests/test_review_autofix_step_scripts_contract.py`, `tests/test_workflow_file_size_limit.py`, `tests/test_log_prefix_regressions.sh`, and `bash -n` / `shellcheck` (when installed) on the changed scripts.

## Risks & Mitigations

- A consumer repo without `ai-review.yml` or `internal-review.yml` falls through to a custom caller or `review_autofix.yml`, whose runs are not PR-named, so the probes cannot see them. ACCEPTED: the same last-resort gap as #4701 AD-10, and no worse than today.
- The changes-lost bound could count a PR-named run that reviewed an older head (a commit dated before a later push). Mitigation: that only consumes the budget early (fail closed), which ends in today's terminal comment, never a loop.
- An extra failed dispatch call when `ai-review.yml` is absent. Mitigation: the caller wrapper is tried first, so this repo and consumer repos dispatch on the first call.
- A missing step script fails the step. Mitigation: `REQUIRED_BOOTSTRAP_SCRIPTS` stages it, and the verified `.codex-workflow-src` checkout is the fallback, exactly as for the six scripts already moved.

## Rollout

Ships with the #4701 / #4618 projects to `main`, then to consumer repos with the next `@stable` sync. There is no flag. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which base branch does the project build on? — Picked: A — #4701's project branch `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Alternatives: B — `main` (the body has no `Integration branch:` line); C — stop until #4618 and #4701 reach `main`. Why: the issue's Ordering section names this branch, and the PR run names and PR-named lookup exist only there. The chain's base-move rule follows it to `main`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] In what order does the retrigger try workflows? — Picked: A — the caller when it is a PR-named wrapper, then `ai-review.yml`, `internal-review.yml`, a differently named caller, and `review_autofix.yml` last. Alternatives: B — #4701's fixed order `ai-review.yml`, `internal-review.yml`, `review_autofix.yml`, dropping the caller fallback; C — keep `review_autofix.yml` first. Why: the wrappers' runs are the only ones the probes can see; trying the caller first saves a failed call in this repo; keeping the custom caller preserves today's fallback for renamed consumer wrappers. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Is the editor-changes-lost re-dispatch in scope? — Picked: A — yes, with the same chain. Alternatives: B — leave it and file a follow-up issue. Why: it is the same finding in the same file, and the contract test requires the two chains to stay identical. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How does the changes-lost loop bound see a default-branch retry? — Picked: A — count completed, non-cancelled PR-named runs created at or after the earlier of the head commit time and the head's first branch run, with one extra call only when the branch lookup counted nothing; fail closed. Alternatives: B — no extension; C — count every completed PR-named run in the page. Why: B reopens an unbounded dispatch loop; C would block the retry after any earlier review of the PR. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Where does the PR-named match live for the workflow probes? — Picked: A — a new REST helper `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, matching the same exact names as `_pr_named_review_dispatch_runs`. Alternatives: B — move the poller helper into `gh_helpers.sh`; C — inline the query in each probe. Why: the workflow never sources the poller; B refactors #4701's merged code (§5); C duplicates the match. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] What happens to the E2E dispatches in `test-and-mark-stable.yml`? — Picked: A — keep `--ref "${BRANCH}"` and document the exposure in a comment. Alternatives: B — dispatch from the default branch and re-key Phase 4 / 4b on PR-named runs. Why: the issue asks for a comment, and B rewrites the E2E harness's run matching. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Does the dispatch still pass `allow_workflow_edits`? — Picked: A — yes, normalised to `true` / `false`. Alternatives: B — pass only `pr_number`. Why: it is the run's own input, not PR data. Dropping it would change consumer behaviour, because `ai-review.yml` defaults it to false. #4701 passes it too. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] How does `review_autofix.yml` stay under §27 as the bodies grow? — Picked: A — move both retrigger step bodies whole into `scripts/review_autofix_step_<slug>.sh` per the documented pattern. Alternatives: B — keep them inline (still under 480,000 bytes); C — move only the shared dispatch chain into one sourced file. Why: the issue asks for the move; the registry keeps contract tests reading the same text; C invents a new pattern. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `.claude/scripts/security_pass_skip.py` returned `skip: false` (`no skip label`), so the pass runs.
- This session had no `gh` and no GitHub MCP tools at start: `gh` was installed with `.claude/hooks/session-start.sh`, and GitHub writes go through `gh api` / REST via the session proxy.

## References

- Issue #4898; #4618 (final PR #4634); #4701 (final PR #4709, phase PR #4725).
- `docs/plans/issue-4701-review-dispatch-default-branch-plan.md` (AD-4, AD-10).
- CLAUDE.md §15, §27; `agents.md` "Workflow file size limit".
