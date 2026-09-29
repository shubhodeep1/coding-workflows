# Verify workflow identity and default-branch provenance before the poller trusts a PR-named review run

Source issue: shubhodeep1/coding-workflows#5094 (https://github.com/shubhodeep1/coding-workflows/issues/5094)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

The orchestrator poller treats any `workflow_dispatch` run whose title is `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]` as PR `<N>`'s review run, without checking which workflow file produced it or which ref it ran from. This plan makes every PR-named match in `scripts/orchestrate_poll_process.sh` also require the matching wrapper file (`.github/workflows/internal-review.yml` / `.github/workflows/ai-review.yml`) and a head branch equal to the repository's default branch.

## Context

- Issues #4618, #4701, and #4898 moved every review dispatch to the default branch. A default-branch run's `head_branch` is the default branch, so the poller finds it by the run name the wrappers set (`run-name` in `.github/workflows/internal-review.yml:8` and `workflow-templates/ai-review.yml:12`).
- A `workflow_dispatch` run's name is evaluated from the workflow file **at the dispatched ref**. Anyone who can push a branch can dispatch a workflow at that branch, whose copy sets `run-name` to another PR's review title. A different workflow file can do the same. Security audit finding `poller-trusts-unverified-review-run-name` (issue #5094, STRIDE Spoofing, medium, `scripts/orchestrate_poll_process.sh:17423`): the poller then mistakes that run for the PR's review and skips recovery. `_has_active_autofix_run` suppresses the conflict dispatch, `_direct_inflight_review_run_on_branch` suppresses the empty-commit push, and stall recovery reads a forged failure as the PR's own.
- The REST run object carries `path` (the workflow file, which is the workflow's identity) and `head_branch` (the dispatched ref). Observed on 2026-09-29 (`GET actions/runs?event=workflow_dispatch`): every default-branch review dispatch has `path: .github/workflows/internal-review.yml` and `head_branch: main`. The few PR-named runs dispatched on a PR branch (for example run 36546798531 on `claude/implement-plan-issue-4957-…`) have that PR's head branch, which the poller's head-branch lookups already match. So requiring default-branch provenance on the *PR-named* path drops no legitimate run.
- Match sites in `scripts/orchestrate_poll_process.sh` on the base branch:
  1. `_pr_named_review_dispatch_runs` (lines ~17373–17427): one `gh run list --event workflow_dispatch --limit 100` call. `gh run list --json` has no `path` field. Used by `_has_active_autofix_run` (~17361), `_direct_inflight_review_run_on_branch` (~12625), and the stall-recovery failed-autofix redispatch (~13789).
  2. The `retrigger_review` empty-commit push guard's cached-blob scan (~13884–13889).
  3. The stall judge's `workflow_outcomes` (~14334–14339).
  4. The standalone empty-commit push guard's cached-blob scan (~16538–16542).
  Sites 2–4 read the cached REST `actions/runs` blob (`_load_actions_runs_cached`), which keeps full run objects, including `path` and `head_branch`.
- Documentation: `agents.md` line ~2006 describes `_pr_named_review_dispatch_runs` and the cached-blob scans.

## Goals

- G1: `_pr_named_review_dispatch_runs <pr>` returns only runs with `event == workflow_dispatch`, `head_branch ==` the default branch, and a (title, `path`) pair of either `Internal: AI Review & Autofix [pr:<N>]` + `.github/workflows/internal-review.yml` or `AI Review [pr:<N>]` + `.github/workflows/ai-review.yml`. Its output shape (`[{databaseId, event, status, conclusion, displayTitle, createdAt, startedAt}]`, newest first) and its one-API-call budget are unchanged.
- G2: The three cached-blob PR-named clauses (sites 2–4) apply the same identity and provenance predicate. They add no API call beyond the once-per-run default-branch resolution (G3).
- G3: The default branch is resolved at most once per poller run with one `GET repos/<repo>` read cached in a global. `DEFAULT_BRANCH` is never reused, because the poller sets it with a `main` fallback and a guessed branch must not vouch for a run. When it cannot be resolved, PR-named matching yields nothing, the pre-#4701 behaviour, and a `PR_NAMED_REVIEW_PROVENANCE` warning line is logged (§8).
- G4: Tests prove that a spoofed run (wrong `path`, non-default `head_branch`, non-`workflow_dispatch` event, or title/path mismatch) is rejected at every site, and that genuine default-branch runs still match.

## Non-goals

- The other PR-named matchers outside the poller: `_autofix_pr_named_review_runs` (`scripts/gh_helpers.sh` ~1245), `review_merge_train.sh` (~413; it already checks `path`), `review_autofix_sweep.yml` (~208), and `.claude/scripts/check_in_status.py` (~135). See AD-1. They are listed under Notes so a follow-up audit can pick them up.
- Changing how dispatches are made, the run names, or any workflow file.
- Any change to head-branch matching.

## Constraints

- §1: security first. This closes a spoofing path in recovery logic.
- §5: minimal change set. Only the PR-named clauses change. Head-branch lookups, status and freshness filters, and caller logic stay as they are.
- §6: no identifier is renamed or removed. `_pr_named_review_dispatch_runs` keeps its name, arguments, and output shape. The new identifiers (`_pr_named_review_default_branch`, `_PR_NAMED_REVIEW_DEFAULT_BRANCH_CACHE`, `_PR_NAMED_REVIEW_DEFAULT_BRANCH_READY`) were checked for collisions across `scripts/` and `tests/` (none).
- §9: tabs in shell, and each edited block matches its surrounding indentation.
- §15: the helper stays one call (`gh run list` becomes a single REST `actions/runs?event=workflow_dispatch&branch=<default>&per_page=100`). The default-branch read happens once per run and fails open.
- §20: a `changelog.d/5094-verify-pr-named-review-run-provenance.md` fragment (section `security`).
- §7: `agents.md` is updated where it documents the helper and the cached scans.
- §27: no workflow file grows.

## Approach

- **Identity:** the REST `path` field names the workflow file, and the workflow's id is bound to that path. A title only counts together with its own wrapper's path.
- **Provenance:** `head_branch ==` the default branch. The default branch is the only ref whose workflow files went through review, and every legitimate PR-named dispatch runs there (#4618, #4701, #4898). A branch-dispatched run that names itself for its own PR is still found by the head-branch lookups, exactly as before.
- **Transport for the helper:** switch from `gh run list` (no `path`) to one `gh api "repos/<repo>/actions/runs?event=workflow_dispatch&branch=<uri-encoded default>&per_page=100"` through `gh_retry`, and map the result to the existing camelCase output shape. The server-side `branch` filter also makes the 100-run page cover only default-branch dispatches, which stretches the window. See AD-2.
- **Default branch:** new `_pr_named_review_default_branch`, which prints the cached value and resolves it on first use. It is called once in the main shell before project processing begins, so subshells inherit the cache. See AD-3.
- **Alternatives rejected:** matching `workflowName` from `gh run list` (a name is not an identity); an extra `actions/workflows` call to map workflow ids (more calls for the same guarantee `path` gives).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes the scope, and the change is one coherent edit to one script plus its tests and docs.

1. **Phase 1 — verify identity and provenance for every PR-named match in the poller.**
   - Files: `scripts/orchestrate_poll_process.sh`, `tests/test_review_dispatch_default_branch.py`, `tests/test_conflict_dispatch_active_run_visibility.py` (only if its source assertions name the old transport), `agents.md`, `changelog.d/5094-verify-pr-named-review-run-provenance.md` [new].
   - Done: G1–G4 hold; `tests/test_review_dispatch_default_branch.py`, `tests/test_conflict_dispatch_active_run_visibility.py`, and the `test_orchestrate_poll_process.py` selections CI runs pass; `bash -n scripts/orchestrate_poll_process.sh` is clean.
   - Rollback: revert the phase PR. The helper returns to name-only matching, with no data or state to undo.

## Implementation Steps

Phase 1:
1. `scripts/orchestrate_poll_process.sh`, next to `_pr_named_review_dispatch_runs`: add globals `_PR_NAMED_REVIEW_DEFAULT_BRANCH_CACHE=''` / `_PR_NAMED_REVIEW_DEFAULT_BRANCH_READY='false'` and the function `_pr_named_review_default_branch`. It returns the cache, or resolves it with one `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch'`, stores it (empty on failure), and logs `PR_NAMED_REVIEW_PROVENANCE outcome=default_branch_unresolved` on stderr when empty. The docstring states the input and output shape, the calls issued, and the fail-open behaviour (§15).
2. Rewrite `_pr_named_review_dispatch_runs` to resolve the default branch (empty → print `[]` with no runs call), call `gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs?event=workflow_dispatch&branch=<@uri>&per_page=100"`, and filter with `event`, `head_branch`, and title+`path` pairs. Map to `{databaseId: .id, event, status, conclusion, displayTitle: .display_title, createdAt: .created_at, startedAt: .run_started_at}`, sorted newest first. Invalid PR numbers and gh or jq failures still print `[]`. Update its header comment (provenance, identity, one call).
3. Sites 2–4: pass `--arg default_branch "<resolved>"` and replace each `display_title`-only clause with the full predicate (event, non-empty `$default_branch`, `head_branch`, title+`path` pairs). The existing title literals stay in place, so the source assertions in the current tests still find them.
4. Main flow: call `_pr_named_review_default_branch >/dev/null` once in the main shell before the per-project loop and the standalone sweep, so the cache is populated for every subshell. This is the only added call.
5. Tests: in `tests/test_review_dispatch_default_branch.py`, extend the `gh` stub to answer `gh api repos/<repo>/actions/runs?event=workflow_dispatch…` with `$GH_EVENT_LIST` (REST shape) and `repos/<repo>` with the default branch. Add cases: a genuine internal run and a genuine consumer run match; wrong path, non-default `head_branch`, title/path swap, `push` event, and unresolved default branch are all rejected; exactly one runs call is made; the `branch=` query is present. Also add cached-blob cases for the stall-judge `workflow_outcomes` and source assertions that both empty-commit guards carry the `path` and `head_branch` checks. Adjust existing cases to the REST shape.
6. `agents.md` ~2006: describe the REST call, the identity and provenance checks, and the unresolved-default-branch behaviour.
7. `changelog.d/5094-verify-pr-named-review-run-provenance.md` (`<!-- changelog: security -->`) following §20.D.

## Files & Modules

- `scripts/orchestrate_poll_process.sh`
- `tests/test_review_dispatch_default_branch.py`
- `tests/test_conflict_dispatch_active_run_visibility.py` (only if needed)
- `agents.md`
- `changelog.d/5094-verify-pr-named-review-run-provenance.md` [new]

## Tests

- Unit (shell functions extracted and sourced with a stubbed `gh`): the helper cases in step 5; `_has_active_autofix_run` and `_direct_inflight_review_run_on_branch` cases re-run with the REST-shaped stub.
- Source assertions: each cached-blob site carries `(.path // "") == ".github/workflows/internal-review.yml"`, `(.path // "") == ".github/workflows/ai-review.yml"`, and `(.head_branch // "") == $default_branch`.
- Regression: the CI-selected `tests/test_orchestrate_poll_process.py` tests, `tests/test_conflict_dispatch_active_run_visibility.py`, and `bash -n`.

## Risks & Mitigations

- A consumer repo whose default branch is not `main`: provenance compares against the resolved default branch, never a literal.
- A consumer that renamed its wrapper file: its runs stop matching by name and fall back to pre-#4701 behaviour. ACCEPTED: `workflow-templates/profiles/*.txt` install it as `ai-review.yml`, and `.claude/scripts/check_in_status.py` already assumes that exact path.
- Default-branch lookup failure: PR-named matching yields nothing (pre-#4701 behaviour, a possible duplicate dispatch, which `review_autofix.yml`'s concurrency group absorbs) and is logged. ACCEPTED: fail-open is the helper's existing contract.
- Cached blob built from `status=in_progress|queued|completed` pages: it already carries `path` and `head_branch`, so there is no gap.

## Rollout

Ships with the project branch's final PR into its base, then reaches `main` with that base's own project. Consumer repos receive it on the next `@stable` sync. No flag and no migration.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Q1: Which PR-named matchers does this issue fix? — Picked: A — every PR-named match in `scripts/orchestrate_poll_process.sh` (the helper at line 17423 and the three cached-blob copies of the same trust decision). Alternatives: B — only `_pr_named_review_dispatch_runs`; C — also `gh_helpers.sh`, `review_merge_train.sh`, `review_autofix_sweep.yml`, and `.claude/scripts/check_in_status.py`. Why: the finding is "the poller trusts an unverified run name" and §1 puts security first, so the same decision must not stay spoofable three lines away. C widens into other flows and a protected path (`.claude/**`, §28.C), so those are listed under Notes instead. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Q2: How does the helper get the workflow identity? — Picked: A — one REST `actions/runs?event=workflow_dispatch&branch=<default>&per_page=100` call exposing `path` and `head_branch`, mapped to the existing output shape. Alternatives: B — keep `gh run list` and match `workflowName`; C — keep `gh run list` plus an extra `actions/workflows` call for workflow ids. Why: `path` is the workflow's identity at no extra call (§15); a name is not an identity. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Q3: What happens when the default branch cannot be resolved? — Picked: A — PR-named matching yields nothing (pre-#4701 behaviour) with a warning log line. Alternatives: B — assume `main`; C — skip the provenance check. Why: B and C would re-open the spoofing path; A fails in the direction the helper already documents. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Residual same-pattern matchers outside this scope (AD-1): `scripts/gh_helpers.sh` `_autofix_pr_named_review_runs` (~1245, no `path` or `head_branch` check); `scripts/review_merge_train.sh` ~413 (checks `path`, not `head_branch`); `.github/workflows/review_autofix_sweep.yml` ~208; `.claude/scripts/check_in_status.py` ~135.

## References

- Issue #5094 (this finding), audit tracker #3576
- Issues #4618, #4701, #4898 (default-branch dispatch and PR-named runs)
- `scripts/orchestrate_poll_process.sh` `_pr_named_review_dispatch_runs`
