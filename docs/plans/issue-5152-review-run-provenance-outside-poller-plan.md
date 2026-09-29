# Verify workflow identity and default-branch provenance in the PR-named review-run matchers outside the poller

Source issue: shubhodeep1/coding-workflows#5152 (https://github.com/shubhodeep1/coding-workflows/issues/5152)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: run

## Summary

Issue #5094 (phase PR #5109) made the orchestrator poller trust a PR-named review run only when its event, workflow file, and head branch prove where it came from. Four other matchers still trust a run's title with a loose path match or none at all. This plan applies the same identity and provenance check to all four, so a run dispatched from a pushed branch copy of a review wrapper can no longer suppress a review retrigger, use up the editor-changes-lost retry budget, hold a merge-train release, or keep a Claude check-in waiting.

## Context

- A `workflow_dispatch` run's name comes from the workflow file **at the dispatched ref**. Anyone who can push a branch can dispatch `internal-review.yml` (or any file) from that branch with a `run-name` naming another PR. The run's REST object still reports the truth in two fields: `path` (the workflow file) and `head_branch` (the dispatched ref).
- Issues #4618, #4701, and #4898 moved every review dispatch to the default branch and taught the guards to find those runs by name: `Internal: AI Review & Autofix [pr:<N>]` (`internal-review.yml`) and `AI Review [pr:<N>]` (`workflow-templates/ai-review.yml`). #5094 then hardened the poller's four matchers on its project branch. Its plan listed the four matchers below as non-goals (its AD-1), and the reviewer panel on PR #5109 raised two of them again. This issue is that follow-up.
- The four matchers, as they stand on the base branch:
  1. `scripts/gh_helpers.sh` `_autofix_pr_named_review_runs` (~line 1208). One REST `actions/runs?event=workflow_dispatch&per_page=100` call. It requires `event == workflow_dispatch`, a loose path regex `(^|/)(internal-review|ai-review)\.ya?ml$`, and either title, **unpaired** with its path, and no head-branch check. Callers in the same file: `autofix_retrigger_has_inflight_peer` (a spoofed in-flight run suppresses the post-commit retrigger dispatch) and `autofix_changes_lost_head_retry_consumed` (a spoofed completed run consumes the one editor-changes-lost retry). Both run inside `review_autofix.yml` steps (`scripts/review_autofix_step_post_commit_retrigger.sh`, `scripts/review_autofix_step_changes_lost_redispatch.sh`).
  2. `scripts/review_merge_train.sh` `_mt_inflight_review_branches` (~line 410). Its one listing checks a loose path regex that also admits `review_autofix.yml`, and emits `pr:<N>` for any `workflow_dispatch` run with either title, with no head-branch check and no title/path pairing. A spoofed active run makes `_mt_release` leave that PR queued (`MERGE_TRAIN_RELEASE_ACTIVE`).
  3. `.github/workflows/review_autofix_sweep.yml` `dispatch_pr` (~line 202). It snapshots `internal-review.yml` and `review_autofix.yml` runs through one jq program and keys a `workflow_dispatch` run titled `Internal: AI Review & Autofix [pr:<N>]` as `pr:<N>` with no path or head-branch check, so even a `review_autofix.yml` run can claim the key. A spoofed active run makes the sweep skip that PR (`AUTOFIX_SWEEP_SKIP reason=active_run`).
  4. `.claude/scripts/check_in_status.py` `_active_run_count` (~line 282). Its PR-named fallback reads `actions/workflows/internal-review.yml/runs?event=workflow_dispatch` and counts runs by title alone. A spoofed active run keeps a PR in `open` instead of `stuck`, so the §26 checker and the §26.H sweep keep waiting. The same file's `_is_pr_dispatched_review_run` (~line 321) already applies the full rule and is the model for this fix.
- The four files are byte-identical on this base branch and on #5094's project branch, and #5094 changed none of them, so the two projects do not overlap.
- GitHub can report a null `head_branch` on a `workflow_dispatch` run. Issue #4928 fixed the sweep dropping such a PR-named run, which let the next tick dispatch the PR again and replace its pending review. `agents.md` documents that carve-out.
- Where each matcher can read the default branch without a new call: the Actions event payload (`$GITHUB_EVENT_PATH`, `.repository.default_branch`) in the `review_autofix.yml` steps and in `cancel_on_pr_close.yml` (which runs the merge train); the PR object (`base.repo.default_branch`, already read by `_pr_default_branch`) in `check_in_status.py`. The sweep runs on `schedule` and `workflow_dispatch`; it reads the payload first and falls back to one `GET repos/<repo>` per run.
- `.claude/scripts/check_in_status.py` is a protected path (CLAUDE.md §28.C). Its twin `workflow-templates/.claude/scripts/check_in_status.py` is byte-identical today.

## Goals

- G1: `_autofix_pr_named_review_runs <pr> [status]` returns only runs where `event == workflow_dispatch`, the title and `path` are a pair (`Internal: AI Review & Autofix [pr:<N>]` with `.github/workflows/internal-review.yml`, or `AI Review [pr:<N>]` with `.github/workflows/ai-review.yml`), and `head_branch` is the default branch or absent (AD-2). Its output shape `[{id, status, conclusion, created_at, path}]`, return codes, and one-call budget are unchanged.
- G2: `_mt_inflight_review_branches` emits `pr:<N>` only for a run that passes the same predicate. Its head-branch output and single listing call are unchanged.
- G3: the sweep's `dispatch_pr` returns a PR number only for an `internal-review.yml` run that passes the same predicate. Any other run keeps its head-branch key, and a run with neither key is still dropped (#4928 unchanged).
- G4: `_active_run_count`'s PR-named fallback counts only runs that pass the same predicate for `internal-review.yml`. Its head-branch reads are unchanged.
- G5: the default branch is never guessed. No matcher uses a `main` fallback. When it cannot be resolved, every PR-named match finds nothing, a `REVIEW_RUN_PROVENANCE ... outcome=default_branch_unresolved` warning is logged, and each caller behaves as it did before PR-named runs existed (AD-3).
- G6: spoof-rejection tests for each matcher (a same-name run on a non-default branch, a wrong path, a wrong event, and a title paired with the other wrapper's path), plus tests that genuine default-branch runs and null-head runs still match, plus the existing tests for each caller.

## Non-goals

- The poller (`scripts/orchestrate_poll_process.sh`): #5094 owns it.
- Adding the consumer name `AI Review [pr:<N>]` to `check_in_status.py` or the sweep, which read only `internal-review.yml` today (AD-4).
- Changing how dispatches are made, the run names, or any workflow's triggers or permissions.
- Head-branch lookups (runs on the PR's own branch), status filters, freshness windows, and the stale-queued cutoff.

## Constraints

- §1: security first. This closes a spoofing path in four availability guards.
- §5: minimal change set. Only the PR-named clauses change, plus the default-branch resolution they need.
- §6: no identifier is renamed or removed. `_autofix_pr_named_review_runs`, `_mt_inflight_review_branches`, `dispatch_pr`, `dedupe_key`, and `_active_run_count` keep their names, arguments, and output shapes. `_active_run_count` gains an optional keyword argument `default_branch` (default `None`). New identifiers, checked for collisions on this base branch, #5094's branch, and `main` (none): `_autofix_review_default_branch`, `_AUTOFIX_REVIEW_DEFAULT_BRANCH_CACHE`, `_AUTOFIX_REVIEW_DEFAULT_BRANCH_READY`, `sweep_default_branch`, `_is_active_pr_named_review_run`, and the log key `REVIEW_RUN_PROVENANCE`. The sweep step's env reuses the `EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}` idiom `internal-review.yml` already uses, in its own step scope. Existing log lines (`AUTOFIX_PEER_CHECK`, `AUTOFIX_CHANGES_LOST_BUDGET`, `MERGE_TRAIN_RELEASE_ACTIVE`, `AUTOFIX_SWEEP_SKIP`) keep their field sets.
- §9: tabs in shell and Python, 2-space YAML.
- §15: no new per-item call. The gh_helpers and merge-train matchers read the default branch from the event payload and fall back to one `GET repos/<repo>` per process, cached. `check_in_status.py` uses the PR object it already has. The sweep reads the payload and falls back to one `GET repos/<repo>` per run.
- §20: one fragment, `changelog.d/5152-review-run-provenance-outside-poller.md`, section `security`.
- §27: `review_autofix_sweep.yml` is about 21.9 KB; the change adds well under 2 KB.
- §28.C: `.claude/scripts/check_in_status.py` is a protected path. The phase edits only the twin `workflow-templates/.claude/scripts/check_in_status.py` (the interim twin-first default, until #4785), and the root copy follows through the `[claude-twin-sync]` step.

## Approach

- **One predicate everywhere.** A PR-named run counts only when all hold:
  - `event == "workflow_dispatch"`;
  - identity: the title and `path` are a pair. `path` is compared exactly (`.github/workflows/internal-review.yml` or `.github/workflows/ai-review.yml`), after dropping any `@<ref>` suffix, as `_is_pr_dispatched_review_run` already does;
  - provenance: `head_branch` equals the resolved default branch, or GitHub reported none (null or empty) (AD-2). Any other branch is rejected, which is exactly the forged case: a branch-dispatched run always reports the branch it ran from.
- **Default branch.** A new `_autofix_review_default_branch` in `scripts/gh_helpers.sh` prints the cached value and resolves it on first use: `.repository.default_branch` from `$GITHUB_EVENT_PATH` when that file is readable, otherwise one `gh api repos/<repo> --jq .default_branch` through `gh_retry`. The merge train already sources `gh_helpers.sh`, so it reuses it. `check_in_status.py` threads `_pr_default_branch(pr)` into `_active_run_count`. The sweep sets `sweep_default_branch` once per run from `${{ github.event.repository.default_branch }}` or, when that is empty, one REST read, and passes it to the jq program with `--arg`. #5094's `_pr_named_review_default_branch` lives in the poller, which none of these four processes source, so it cannot be reused (AD-3).
- **Unresolved default branch.** PR-named matching finds nothing and a `REVIEW_RUN_PROVENANCE repo=<repo> source=<gh_helpers|merge_train|sweep|check_in_status> outcome=default_branch_unresolved pr_named_matching=disabled` warning goes to stderr (§8). Each caller then acts as it did before PR-named runs existed: the peer check finds no PR-named peer (it dispatches, fail-open); the changes-lost budget cannot see the current run among PR-named runs and consumes the budget on a dispatch run (fail-closed); the merge train and the sweep key runs by head branch only; `check_in_status.py` counts no PR-named run.
- **Transport stays as is.** `_autofix_pr_named_review_runs` keeps its unfiltered `event=workflow_dispatch&per_page=100` call and filters locally, because a server-side `branch=` filter would drop the null-head runs AD-2 keeps.
- **Alternatives rejected:** a strict `head_branch ==` default match (re-opens #4928's duplicate dispatch for null-head runs); reusing `DEFAULT_BRANCH` or a `main` fallback (a guess must never vouch for a run); an extra `actions/workflows` call to map workflow ids (`path` already is the identity).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes the scope, and the four matchers share one predicate, one default-branch rule, and one set of spoof tests.

1. **Phase 1 — apply the identity and provenance check to the four PR-named matchers outside the poller.** `protected paths: .claude/scripts/check_in_status.py` (twin-first: only `workflow-templates/.claude/scripts/check_in_status.py` is edited in the phase PR).
   - Files: `scripts/gh_helpers.sh`, `scripts/review_merge_train.sh`, `.github/workflows/review_autofix_sweep.yml`, `workflow-templates/.claude/scripts/check_in_status.py`, the tests below, `agents.md`, `changelog.d/5152-review-run-provenance-outside-poller.md`.
   - Done when: every goal G1–G6 is met with test evidence, the repo's standard checks pass, and after the `[claude-twin-sync]` copy `.claude/scripts/check_in_status.py` is byte-identical to its twin.
   - Rollback: revert the phase PR. No data, index, or workflow-input change is involved.

## Implementation Steps

1. `scripts/gh_helpers.sh`: add `_autofix_review_default_branch` (with the two cache globals and a docstring stating input, output, API calls, and fail behaviour, per §15) above `_autofix_pr_named_review_runs`. In `_autofix_pr_named_review_runs`, resolve the default branch first; when empty, log the warning and print `[]` with status 0, before any call. Replace the jq filter with the paired predicate. Update its docstring.
2. `scripts/review_merge_train.sh`: in `_mt_inflight_review_branches`, resolve the default branch through `_autofix_review_default_branch` (guarded with `type` in case `gh_helpers.sh` failed to source; unresolved then disables PR-named keys) and pass it to the jq with `--arg`. Emit `pr:<N>` only for a run that passes the predicate. Head-branch output is unchanged. Update the comment block.
3. `.github/workflows/review_autofix_sweep.yml`: add `EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}` to the step env, resolve `sweep_default_branch` once before the snapshot loop (event value, else one REST read, else empty plus the warning), pass `--arg default_branch "${sweep_default_branch}"` to the jq program, and make `dispatch_pr` apply the predicate for the internal name and `internal-review.yml`.
4. `workflow-templates/.claude/scripts/check_in_status.py`: give `_active_run_count` an optional `default_branch: str | None = None`, apply the predicate in its PR-named fallback through a new `_is_active_pr_named_review_run` (count nothing, with a stderr warning and no listing read, when it is `None`), and pass the default branch from its four call sites (`_pr_default_branch(pr)` where the PR object is in scope, the `default_branch` argument inside `_check_claude_fixer_pr`). Keep the root copy untouched in the phase PR.
5. Tests (see below), `agents.md` (the paragraph that describes the PR-named matchers), and the changelog fragment.

## Files & Modules

| File | Change |
| --- | --- |
| `scripts/gh_helpers.sh` | new `_autofix_review_default_branch`; paired, provenance-checked filter in `_autofix_pr_named_review_runs` |
| `scripts/review_merge_train.sh` | provenance-checked `pr:<N>` key in `_mt_inflight_review_branches` |
| `.github/workflows/review_autofix_sweep.yml` | `sweep_default_branch`; provenance-checked `dispatch_pr` |
| `workflow-templates/.claude/scripts/check_in_status.py` | provenance-checked PR-named fallback in `_active_run_count` |
| `.claude/scripts/check_in_status.py` | same, through the `[claude-twin-sync]` copy only |
| `tests/test_retrigger_default_branch_dispatch.py`, `tests/test_review_merge_train.py`, `tests/test_review_autofix_sweep_stale_queued.py`, `tests/test_check_in_status_hand_back.py` | spoof-rejection and acceptance tests |
| `agents.md`, `changelog.d/5152-review-run-provenance-outside-poller.md` | documentation (§7, §20) |

## Tests

- `_autofix_pr_named_review_runs`: rejects a same-name run on a non-default branch, a wrong path, a title paired with the other wrapper's path, and a non-dispatch event; keeps a genuine default-branch run and a null-head run; prints `[]` with no API call when the default branch is unresolved; reads the default branch from the event payload with no REST call when the payload has it.
- The two callers keep their existing tests (`tests/test_retrigger_default_branch_dispatch.py`, `tests/test_editor_changes_lost_redispatch_budget.py`, `tests/test_gh_helpers_list_runs_method.py`), plus one case each proving a spoofed run neither suppresses the retrigger nor consumes the budget.
- `_mt_inflight_review_branches`: the same spoof cases emit no `pr:<N>` key; a genuine run does; an unresolved default branch emits head branches only.
- The sweep's jq program (extracted from the workflow, as the existing test does): the same spoof cases get no `pr:<N>` key; the #4928 null-head case still does; a `review_autofix.yml` run with the internal title does not.
- `check_in_status.py` (twin): `_active_run_count` ignores spoofed runs and counts a genuine one; `default_branch=None` counts no PR-named run. New-behaviour tests load the `workflow-templates/.claude/scripts/` twin; the existing fixtures gain the fields every real PR and run object carries (`base.repo.default_branch`, `event`, `path`, `head_branch`), so they pass against both copies, and the whole suite is also run against a copy of the tree with the twin synced into `.claude/scripts/`.
- The test harnesses pin `GITHUB_EVENT_PATH` to a fixture payload and never inherit the CI run's own.
- The repo's standard suites: `python3 -m pytest tests/` (or the `ci.yml` steps that cover these files), `bash -n` on the edited scripts, and `actionlint`/`yamllint` on the workflow when available.

## Risks & Mitigations

- **A legitimate run is rejected** (for example, one dispatched on a PR's own branch). Such a run is found by each caller's head-branch lookup, as #5094 notes, so it is not lost. A null-head run is kept on purpose (AD-2).
- **The event payload lacks `repository.default_branch`** (for example, a `schedule` event). The REST fallback covers it, at most once per process.
- **Template parity tests are red until the twin sync.** Expected under the interim twin-first rule; the phase stays on hold until the `[claude-twin-sync]` commit lands.
- **Stacked base.** This project's final PR targets the #4898 project branch, two levels below `main`. `validate.yml` authorizes a `target_ref` only for a PR into the default branch, so the validation stage is expected to stop as #5094's did (its Q2).

## Rollout

Ships with the base chain (#4898 → #4701 → `main`) and reaches consumer repos on the next `@stable` sync. `check_in_status.py` reaches consumers through the `.claude/` sync. No flag: each change only narrows what counts as a PR-named run.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which branch does this project build on? The issue names no `Integration branch:` or `Target branch:` line, and the rule's default (`main`) lacks `_autofix_pr_named_review_runs`, which the issue's first item targets (it exists only on the unmerged #4898 → #4701 stack). — Picked: A — `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch`, the shallowest branch that carries all four matchers, the same base #5094 uses. Alternatives: B — #5094's project branch (adds a dependency on a project that is `ai:claude-blocked`, and none of the four matchers can reach its poller cache); C — `main` (the gh_helpers matcher does not exist there, and the other edits would conflict with the stack). Why: every matcher the issue names exists there, and it keeps this project independent of #5094's blockers. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is a PR-named run with a null or empty `head_branch` treated? — Picked: A — accept it (the default branch or none); reject any other branch. Alternatives: B — strict `head_branch ==` default, as the poller does since #5094; C — keep the carve-out in the sweep only. Why: a forged run always reports the branch it was dispatched from, so A blocks the issue's attack, and B would re-open #4928 (a null-head review dropped, then a duplicate dispatch replacing the pending run) in all four guards. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Where does each matcher get the default branch, and what happens when it is unknown? — Picked: A — the event payload or the PR object first, then at most one `GET repos/<repo>` per process; when unresolved, PR-named matching finds nothing and a `REVIEW_RUN_PROVENANCE` warning is logged. Alternatives: B — fall back to `main`; C — skip the provenance check when unknown. Why: B and C let a guess or nothing vouch for a run; A adds no per-item call (§15). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should `check_in_status.py` and the sweep also start matching the consumer name `AI Review [pr:<N>]`? — Picked: A — no; they keep matching only `internal-review.yml`, now with identity and provenance. Alternatives: B — add `ai-review.yml` to both. Why: §5; the issue asks to harden the matchers, not widen them. Applied in: no code change. Status: pending review

## Notes

- Found while verifying PR #5109 (https://github.com/shubhodeep1/coding-workflows/pull/5109#issuecomment-5889831514). Refs #5094.
- `security_pass_skip.py` result: `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #5152; issue #5094 and its plan `docs/plans/issue-5094-verify-pr-named-review-run-provenance-plan.md` (on its project branch); issues #4618, #4701, #4898, #4928.
- CLAUDE.md §1, §5, §6, §8, §9, §15, §20, §27, §28.
