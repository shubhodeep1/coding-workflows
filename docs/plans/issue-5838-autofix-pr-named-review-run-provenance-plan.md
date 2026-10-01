# review_autofix retrigger probes: trust a PR-named review run only from the default branch and its own wrapper

Source issue: shubhodeep1/coding-workflows#5838 (https://github.com/shubhodeep1/coding-workflows/issues/5838)
Base branch: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable
Security pass: skip (ai:security: automation-produced issue)

## Summary

`_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh` accepts any `workflow_dispatch` run whose title is `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]` and whose path ends in either wrapper's file name. It never checks the branch the run was dispatched on. A branch writer can dispatch a modified wrapper titled for another PR and so suppress that PR's follow-up review (the peer probe) or consume its editor-changes-lost retry (the budget probe). Count a PR-named run only when its `head_branch` is the repository's default branch and its path is exactly the wrapper that sets that title, the same rule #5094 applied to the poller.

## Context

- Security finding `autofix-retrigger-accepts-untrusted-review-runs` (high, confidence 9/10), `scripts/gh_helpers.sh:1244` on the base branch. The finding's own `Integration branch:` names the base branch.
- Issue #4898 moved `review_autofix.yml`'s two retrigger steps to dispatch from the default branch and added `_autofix_pr_named_review_runs` as the REST twin of the poller's `_pr_named_review_dispatch_runs`. Its two callers are `autofix_retrigger_has_inflight_peer` (fails open: a failure lets the dispatch go ahead) and `autofix_changes_lost_head_retry_consumed` (fails closed: a failure consumes the retry).
- Issue #5094 fixed the same hole in the poller. A PR-named run there counts only when `event == "workflow_dispatch"`, `headBranch == <default branch>`, and the title/path pair is exactly internal title + `.github/workflows/internal-review.yml` or consumer title + `.github/workflows/ai-review.yml`. Its changelog (`changelog.d/5094-verify-pr-named-review-run-provenance.md`) lists `_autofix_pr_named_review_runs` as outside that fix. This issue closes that gap.
- GitHub evaluates a `workflow_dispatch` run's `run-name` from the workflow file at the dispatched ref, so the title alone proves nothing. The default branch is the only ref whose workflow files are reviewed.
- Callers run in the `Re-trigger review via workflow_dispatch` and `Re-dispatch review on editor-changes-lost` steps (`scripts/review_autofix_step_post_commit_retrigger.sh`, `scripts/review_autofix_step_changes_lost_redispatch.sh`). Neither step knows the default branch today: they dispatch with no `--ref`.

## Goals

- `_autofix_pr_named_review_runs` returns a run only when all hold: `event` is `workflow_dispatch`; `head_branch` equals the resolved default branch; and either (title `Internal: AI Review & Autofix [pr:<N>]` and path exactly `.github/workflows/internal-review.yml`) or (title `AI Review [pr:<N>]` and path exactly `.github/workflows/ai-review.yml`).
- The default branch comes from GitHub-written data only, never a guess: the run's event payload (`$GITHUB_EVENT_PATH` → `.repository.default_branch`, used only when its `.repository.full_name` is `GITHUB_REPOSITORY`), else one `GET repos/<repo>` read. Never a `main` fallback.
- An unresolved default branch makes the helper return 1 with no listing call and one stderr diagnostic line (`AUTOFIX_PR_NAMED_REVIEW_PROVENANCE`). The callers keep their documented failure modes: the peer probe fails open, the budget probe fails closed.
- Output shape `[{id, status, conclusion, created_at, path}]`, return codes, arguments, and the `AUTOFIX_PEER_CHECK` / `AUTOFIX_CHANGES_LOST_BUDGET` lines are unchanged.
- In Actions the fix adds no GitHub API call: the event payload answers.

## Non-goals

- The other PR-named matchers #5094 also left out (`scripts/review_merge_train.sh`, `review_autofix_sweep.yml`, `.claude/scripts/check_in_status.py`). Each needs its own finding and its own fix (AD-2). `smoke_review_pr_named_runs` already checks the default branch.
- No change to `review_autofix.yml` or either step script.
- No change to the branch-scoped lookups in either probe: a run on the PR's own branch is the PR author's, which this finding is not about.

## Constraints

- §1: security first. Unverifiable provenance rejects the run; callers keep their existing fail-open/fail-closed contracts.
- §5: the smallest change. One new helper for the default branch, one stricter jq filter, the two callers untouched.
- §6: no identifier, output field, return code, or log key is renamed. New identifiers checked unique across the repo: `_autofix_pr_named_review_default_branch`, `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE`.
- §9: tabs in `scripts/gh_helpers.sh` and the Python tests.
- §15: no new call in Actions (the event payload already holds the value). The REST fallback is one `GET repos/<repo>`, issued only when the payload lacks the value or names another repository, and only on the PR-named path, which runs only after a caller's branch lookup found nothing. Audited: neither probe has an existing `repos/<repo>` read to extend.
- §20: a `changelog.d/` fragment (security fix).
- §27: `review_autofix.yml` is not touched (440,432 bytes today).

## Approach

Add `_autofix_pr_named_review_default_branch` next to `_autofix_pr_named_review_runs`. It reads `.repository.default_branch` from `$GITHUB_EVENT_PATH` when the file is readable and its `.repository.full_name` matches `GITHUB_REPOSITORY` (case-insensitive), else makes one `gh_retry gh api -X GET "repos/${GITHUB_REPOSITORY}" --jq '.default_branch'` read. It prints the name, or returns 1 when the value is empty or contains whitespace or control characters. It keeps no cache: the callers run the lookup in a command substitution, and each step calls it at most twice.

`_autofix_pr_named_review_runs` resolves the default branch before its listing call. On failure it logs `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE pr=<N> outcome=default_branch_unresolved` to stderr and returns 1. Its jq filter gains the `head_branch == $default_branch` check and pairs each title with its exact wrapper path, replacing the loose `(^|/)(internal-review|ai-review)\.ya?ml$` path regex.

Alternatives considered: always the REST read (AD-1 B: one more call per lookup for the same answer), and passing `github.event.repository.default_branch` through the two steps' `env:` (AD-1 C: edits the 440 KB workflow and both steps).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue names one defect in one helper.

1. **Phase 1 — provenance check in `_autofix_pr_named_review_runs`.**
   - Files: `scripts/gh_helpers.sh`, `tests/test_retrigger_default_branch_dispatch.py`, `tests/test_editor_changes_lost_redispatch_budget.py`, `tests/test_gh_helpers_list_runs_method.py`, `README.md`, `agents.md`, `changelog.d/5838-autofix-pr-named-review-run-provenance.md` [new].
   - Done when: a PR-named run dispatched on a non-default branch, or with a mismatched or non-exact title/path pair, is not counted by either probe; an unresolved default branch fails the peer probe open and the budget probe closed with no listing call; the event payload answers with no API call and the REST read is used only as fallback; all listed tests and the repo's shell and Python checks pass.
   - Rollback: revert the phase PR. The helper returns to name-only matching; nothing else depends on it.

## Implementation Steps

1. `scripts/gh_helpers.sh`: add `_autofix_pr_named_review_default_branch` with a contract comment (sources, output, API calls, failure). Update `_autofix_pr_named_review_runs`: resolve the default branch first (diagnostic line and return 1 on failure, no listing call), tighten the jq filter, and update its header (provenance rule, issue #5838, call budget). Update the "PR-named runs" paragraphs of both callers' headers in one line each.
2. Tests (below).
3. `README.md` (the peer-probe passage near line 541) and `agents.md` (the issue #4898 retrigger note near line 2084): say the PR-named match counts a run only from the default branch and the exact wrapper.
4. `changelog.d/5838-autofix-pr-named-review-run-provenance.md` (`security`).

## Files & Modules

- `scripts/gh_helpers.sh`
- `tests/test_retrigger_default_branch_dispatch.py`
- `tests/test_editor_changes_lost_redispatch_budget.py`
- `tests/test_gh_helpers_list_runs_method.py`
- `README.md`
- `agents.md`
- `changelog.d/5838-autofix-pr-named-review-run-provenance.md` [new]

## Tests

Unit (bash functions extracted and run against a `gh` stub), in `tests/test_retrigger_default_branch_dispatch.py`:
- Existing PR-named fixtures gain `head_branch: "main"` and the harness writes an event payload naming `owner/repo` with default branch `main`; every existing assertion (call counts included) holds.
- The peer probe ignores a queued PR-named run whose `head_branch` is another branch, is null, or is missing; a title/path pair swapped between the wrappers; a `.yaml` path; and a path with a prefix before `.github/workflows/`.
- The budget probe does not count a completed PR-named run from another branch, so the budget stays available.
- Default branch unresolved (no payload and a failing REST read): the helper makes no listing call and logs `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE … outcome=default_branch_unresolved`; the peer probe returns 1 with `reason=pr_named_api_error`; the budget probe returns 0 with `reason=pr_named_api_error`.
- The payload for another repository, or one without `default_branch`, falls back to exactly one `repos/owner/repo` read; with a valid payload no such read is made.
- A REST answer with whitespace (a malformed body) counts as unresolved.

`tests/test_editor_changes_lost_redispatch_budget.py` and `tests/test_gh_helpers_list_runs_method.py`: their harnesses also `eval` the new helper and set `GITHUB_EVENT_PATH`, so the existing assertions still hold.

Checks: `python3 -m pytest` on the three files, `bash tests/test_log_prefix_regressions.sh`, `bash -n scripts/gh_helpers.sh`, `shellcheck` on `scripts/gh_helpers.sh` if installed, and the repo's standard suite as CI runs it.

## Risks & Mitigations

- A legitimate PR-named run from the default branch is dropped because the payload is missing → the REST fallback answers; only a failed read drops it, and then the callers keep their pre-#4898 failure modes. ACCEPTED.
- The current run of a manual `workflow_dispatch` on a non-default branch is no longer in the PR-named list, so the budget probe's `unnamed_dispatch_run` check consumes its retry → fail closed is the intended direction for an unverifiable run. ACCEPTED.
- A consumer whose wrapper is named `ai-review.yaml` no longer matches → every consumer template ships `ai-review.yml`, and the poller already requires that exact path. ACCEPTED.

## Rollout

No flag. Ships to consumer repos with the next `@stable` sync of `scripts/gh_helpers.sh`. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-10-01] Where does the helper get the default branch? — Picked: A — the run's event payload (`.repository.default_branch`, only when `.repository.full_name` is `GITHUB_REPOSITORY`), else one `GET repos/<repo>` read; no `main` fallback. Alternatives: B — always one `GET repos/<repo>` read (the poller's #5094 source); C — pass `github.event.repository.default_branch` through both steps' `env:`. Why: GitHub writes both sources; A adds no API call in Actions (§15) and touches no workflow file, B costs a call per lookup for the same value, C edits the 440 KB `review_autofix.yml` and both steps (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Fix only this helper, or also the other PR-named matchers #5094 left out? — Picked: A — only `_autofix_pr_named_review_runs`. Alternatives: B — also `scripts/review_merge_train.sh`, `review_autofix_sweep.yml`, and `.claude/scripts/check_in_status.py`. Why: the issue names this helper; the others have different callers and failure modes and need their own findings (§5), and `check_in_status.py` is a protected path. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How strict is the path match? — Picked: A — exact `.github/workflows/internal-review.yml` for the internal title and `.github/workflows/ai-review.yml` for the consumer title. Alternatives: B — keep the `(^|/)…\.ya?ml$` regex and only pair titles with wrappers. Why: the issue asks for the exact workflow-path/run-title pair, the wrappers ship as `.yml`, and the poller already uses these exact paths. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] What happens when the default branch cannot be resolved? — Picked: A — the helper returns 1 before any listing call and logs one `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE` stderr line; the callers keep their existing reasons (`pr_named_api_error`) and fail-open / fail-closed contracts. Alternatives: B — return an empty list (rc 0); C — add a new caller reason value. Why: B would make the budget probe treat "unverifiable" as "no retry yet" and dispatch again; C changes pinned log output (§6) for no new behaviour. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The base branch's own final PR (#5702) is an open draft into `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch` (checked 2026-10-01).

## References

- Issue #5838 (this finding), issue #5094 and `changelog.d/5094-verify-pr-named-review-run-provenance.md` (the poller fix), issue #4898 (`docs/plans/issue-4898-retrigger-dispatch-default-branch-plan.md`), issue #5523 (push-time bound).
- `scripts/orchestrate_poll_process.sh` `_pr_named_review_dispatch_runs` / `_pr_named_review_default_branch` (the pattern this mirrors).
