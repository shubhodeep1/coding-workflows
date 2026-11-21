# Dispatch the remaining review runs from the default branch, and find PR-named runs in the poller's lookups

Source issue: shubhodeep1/coding-workflows#4701 (https://github.com/shubhodeep1/coding-workflows/issues/4701)
Base branch: main (was claude/implement-plan-issue-4618-sweep-dispatch-default-branch, which merged into main as #4634 on 2026-09-29)
Security pass: run

## Summary

Issue #4618 (`review-dispatches-unmerged-workflow`, critical) is being fixed for `review_autofix_sweep.yml` only. Three more sites still dispatch the review workflow at a pull request's head ref, so they run that branch's unmerged copy of the workflow file with `secrets: inherit` and write permissions. This plan moves all three to the default branch with a validated PR number, and teaches the poller's head-branch-keyed run lookups to also find the PR-named dispatch runs that default-branch dispatches produce.

## Context

- #4618's project (`claude/implement-plan-issue-4618-sweep-dispatch-default-branch`, final PR #4634, still a draft into `main`) added:
  - `run-name: Internal: AI Review & Autofix [pr:<N>]` for `workflow_dispatch` runs of `.github/workflows/internal-review.yml`;
  - a `pr:<N>` dedupe key in the sweep's active-run snapshot;
  - one PR-named `gh run list --workflow internal-review.yml --event workflow_dispatch --limit 100` lookup in `_has_active_autofix_run` (`scripts/orchestrate_poll_process.sh`), issued only when the head-branch lookups found nothing.
  Its AD-1 scoped the other sites out. This issue covers them, and it depends on that `run-name`, so the project builds on #4618's project branch (AD-1).
- Remaining head-ref dispatch sites:
  1. `_dispatch_review_for_conflicts` (`scripts/orchestrate_poll_process.sh`): tries `ai-review.yml`, `internal-review.yml`, then `review_autofix.yml`, each with `--ref "${head_ref}"`. It is called from about 15 poller paths (conflicts, stall recovery, review-blocked unstick, integration heal).
  2. `_mt_dispatch_review` (`scripts/review_merge_train.sh`): the same three workflows with `--ref "${head}"`. `_mt_inflight_review_branches`, the release dedupe, keys active runs by `head_branch` only.
  3. The fallback-PR step "Dispatch AI review for the fallback PR" in `.github/workflows/forward-merge-stable-to-main.yml`: `--ref "${HEAD_BRANCH}"`, a branch the workflow cut itself.
- Consumer repos run the same poller and merge-train scripts, and they dispatch `ai-review.yml` (`workflow-templates/ai-review.yml`, name `AI Review`). It has no `run-name` yet, so a default-branch dispatch there would be invisible to every lookup (AD-3).
- Poller lookups keyed by head branch that miss a default-branch dispatch run (`head_branch` = the default branch, `head_sha` = its tip):
  4. Stall-judge `workflow_outcomes` in `invoke_stall_judge`: filters the cached `_load_actions_runs_cached` blob by `head_branch` / `head_sha`.
  5. The retrigger failed-autofix lookup in `execute_stall_recovery_action` (`retrigger_review`): `gh run list --workflow <wf> --branch <head_ref> --limit 1` per review workflow.
  6. The empty-commit push guards: the cached in-flight scans in `execute_stall_recovery_action` and `run_standalone_stall_recovery`, and their fallback `_direct_inflight_review_run_on_branch`. Missing a live review run here makes the stall recovery push an empty commit under it, which discards that run's editor pass (AD-7).
- The PR #3895 incident (2026-08-29) is why a dispatched run must stay visible to the active-run guards: without it the poller re-dispatched every cycle.

## Goals

- `_dispatch_review_for_conflicts`, `_mt_dispatch_review`, and the forward-merge fallback dispatch never pass `--ref`; each passes only a PR number it has validated as `^[1-9][0-9]*$`, plus `allow_workflow_edits` as before.
- A `workflow_dispatch` run of the consumer `ai-review.yml` is named `AI Review [pr:<N>]`; every other event keeps GitHub's default name.
- One shared helper, `_pr_named_review_dispatch_runs <pr_number>`, returns the active and recent `workflow_dispatch` runs named `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]`, with one `gh run list` call. It fails open to `[]`.
- `_has_active_autofix_run` uses the helper, so it sees PR-named runs in this repo and in consumer repos.
- The stall-judge `workflow_outcomes` also lists PR-named dispatch runs for the target PR from the cached blob, with no new API call.
- The retrigger failed-autofix lookup treats the newest PR-named dispatch run's failure like a head-branch failure. It issues its one helper call only when the head-branch lookups found no failed run, and a PR-named failure counts only when it is newer than every completed head-branch run it saw.
- The empty-commit push guards also see a live PR-named dispatch run: the cached scans at no API cost, and `_direct_inflight_review_run_on_branch` with one helper call only when its branch listing matched nothing.
- The merge-train release sees an active PR-named dispatch run for a queued PR from the listing it already makes, and leaves that PR queued.
- Tests cover every site, and `changelog.d/4701-review-dispatch-default-branch.md` carries a `security` entry.

## Non-goals

- `build_active_issue_set` and `cancel_zombie_runs_for_issue` keep mapping runs to issues by head-branch pattern (AD-8). A PR-named run carries a PR number, not an issue number, and the destructive recovery paths they feed are covered by the guards above.
- No `run-name` for `review_autofix.yml`. It stays the last dispatch candidate, reached only when neither wrapper can be dispatched (AD-10).
- No new workflow inputs and no `head_sha` input (same reasoning as #4618 AD-3).
- `review_autofix_sweep.yml` is unchanged; #4618 owns it.

## Constraints

- §1 / §3: security first; every site must keep the PR #3895 dedupe working.
- §5: minimal change set. Existing function names, log keys (`MERGE_TRAIN_DISPATCHED`, `STALL_INFLIGHT_DIRECT_CHECK`, `[conflict-dispatch]`), and return codes are unchanged. The `ref=` field of `MERGE_TRAIN_DISPATCHED` now prints the default-branch marker instead of the head branch (§6: the key is kept).
- §6: `_direct_inflight_review_run_on_branch` gains an optional second argument; one-argument callers keep today's behaviour.
- §9: tabs in shell and Python, 2-space YAML.
- §14: `workflow-templates/ai-review.yml` reaches consumer repos with the next `@stable` sync; no new consumer.
- §15: at most one new API call per lookup, and only on the miss path; the stall judge and the merge train add none.
- §18: no new scripts; everything lives in existing scripts and workflows.
- §20: one `changelog.d/` fragment, section `security`.
- §27: no workflow file grows by more than a few hundred bytes (`review_autofix.yml` is untouched).

## Approach

- **Dispatch sites.** Drop `--ref` at all three sites and validate the PR number first. The workflow file then always comes from the default branch, while `review_autofix.yml` still checks out the PR head from the PR's metadata, as it does for the #4618 sweep. The forward-merge branch is cut by the workflow itself, so it is less exposed than the other two. It moves anyway (AD-2): any writer can push to it between the fallback push and the dispatch, and the default branch's `internal-review.yml` is the copy that carries the PR `run-name`.
- **Run names.** `internal-review.yml` already names dispatched runs (#4618). `workflow-templates/ai-review.yml` gets the same expression with its own name: `${{ github.event_name == 'workflow_dispatch' && format('AI Review [pr:{0}]', inputs.pr_number) || '' }}`.
- **Shared helper.** `_pr_named_review_dispatch_runs` makes one `gh run list --repo … --event workflow_dispatch --limit 100 --json databaseId,status,conclusion,displayTitle,createdAt,startedAt` call and keeps the runs whose `displayTitle` exactly equals one of the two names for the PR, newest first. It has no `--workflow` filter, so one call covers both repo kinds (AD-4). The title of a `workflow_dispatch` run comes from the dispatched ref's workflow file, which is now always the default branch, so the exact-title match cannot be spoofed by PR text. #4618's `internal-review.yml`-only call in `_has_active_autofix_run` is replaced by the helper.
- **Stall judge.** Add a second `select` branch to the `workflow_outcomes` jq: `event == "workflow_dispatch"` and `display_title` equal to either name for `target_pr`. No new call (AD-5).
- **Retrigger lookup.** The head-branch loop also records each workflow's newest completed `createdAt`. When no failure is found, call the helper once. Take its newest completed run, and when that run failed, was cancelled, or timed out and is newer than every completed head-branch run, redispatch exactly as for a head-branch failure (AD-6).
- **Push guards.** Both cached scans accept a run that matches the head branch or head SHA as today, or is a `workflow_dispatch` run named for the PR. `_direct_inflight_review_run_on_branch <branch> [pr_number]` runs the helper when its branch listing matched nothing and a valid PR number was given, with the same freshness window (AD-7).
- **Merge train.** `_mt_inflight_review_branches` also prints `pr:<N>` for each active `workflow_dispatch` review run whose `display_title` ends in ` [pr:<N>]`, from the same listing. The release loop checks `pr:${num}` as well as the head branch.

## Phases & Merge Strategy

This is a single-phase plan: issue mode (CLAUDE.md §28.A) authorises it, because `/implement-issue-claude` always turns one issue into one phase.

1. **Phase 1 — default-branch dispatch at the remaining sites, plus PR-named run lookups.**
   - Files: `scripts/orchestrate_poll_process.sh`, `scripts/review_merge_train.sh`, `.github/workflows/forward-merge-stable-to-main.yml`, `workflow-templates/ai-review.yml`, the tests below, `agents.md`, `README.md`, `changelog.d/4701-review-dispatch-default-branch.md`.
   - Done when:
     - no remaining review dispatch in these files passes `--ref`;
     - every lookup listed in Goals finds a PR-named run in its tests;
     - the new tests, the updated existing tests, ShellCheck on both scripts, and `actionlint` on the changed workflows pass.
   - Rollback: revert the phase PR. The sites return to head-ref dispatch and the lookups to head-branch keying, which is #4618's state.

## Implementation Steps

1. `scripts/orchestrate_poll_process.sh`: add `_pr_named_review_dispatch_runs` next to `_has_active_autofix_run`, with a docstring giving its input, output, one API call, and fail-open contract (§15). Replace the PR-named block in `_has_active_autofix_run` with a call to it.
2. `scripts/orchestrate_poll_process.sh` `_dispatch_review_for_conflicts`: validate `pr_number` (warn and return 1 when invalid), drop `--ref "${head_ref}"`, and update the helper comment and the dispatch log line (the head ref stays in logs and in the `_has_active_autofix_run` call).
3. `scripts/orchestrate_poll_process.sh` `invoke_stall_judge`: extend the `workflow_outcomes` jq with a `--arg pr` PR-named branch.
4. `scripts/orchestrate_poll_process.sh` `execute_stall_recovery_action` retrigger lookup: record the newest completed `createdAt`, then the one-call PR-named fallback per AD-6.
5. `scripts/orchestrate_poll_process.sh` push guards: extend both cached in-flight scans with the PR-named branch, and add the optional PR argument to `_direct_inflight_review_run_on_branch` and to both of its call sites.
6. `scripts/review_merge_train.sh`: validate `pr` and drop `--ref` in `_mt_dispatch_review`; emit `pr:<N>` keys in `_mt_inflight_review_branches`; check them in `_mt_release`.
7. `.github/workflows/forward-merge-stable-to-main.yml`: dispatch without `--ref`, validate `PR_NUMBER`, and rewrite the two comments that explain the head-ref dispatch.
8. `workflow-templates/ai-review.yml`: add the `run-name`.
9. Tests (below), `agents.md` (the stale-queued / dispatch-ref paragraph), `README.md` (the failed-autofix redispatch paragraph), and the changelog fragment.

## Files & Modules

- `scripts/orchestrate_poll_process.sh`
- `scripts/review_merge_train.sh`
- `.github/workflows/forward-merge-stable-to-main.yml`
- `workflow-templates/ai-review.yml`
- `tests/test_review_dispatch_default_branch.py` [new]
- `tests/test_conflict_dispatch_active_run_visibility.py`
- `tests/test_orchestrate_poll_process.py`
- `tests/test_review_merge_train.py`
- `tests/test_retrigger_inflight_direct_fallback.py` (only if a changed contract needs it)
- `agents.md`, `README.md`
- `changelog.d/4701-review-dispatch-default-branch.md` [new]

## Tests

- **Unit, extracted shell functions with a stubbed `gh`** (`tests/test_review_dispatch_default_branch.py`):
  - `_dispatch_review_for_conflicts` dispatches with no `--ref` and only `pr_number` / `allow_workflow_edits`, and refuses an invalid PR number without calling `gh`;
  - `_pr_named_review_dispatch_runs` keeps exact titles for both names, drops other PRs, prefixes (`[pr:12]` vs `[pr:1]`), and non-dispatch events, and returns `[]` on a `gh` failure;
  - `_has_active_autofix_run` finds an active `AI Review [pr:<N>]` run;
  - `_direct_inflight_review_run_on_branch` with a PR number finds a fresh PR-named run the branch listing missed, and makes no extra call without one;
  - the stall-judge `workflow_outcomes` jq, run against a sample blob, includes a PR-named dispatch run and excludes another PR's.
- **Poller harness** (`tests/test_orchestrate_poll_process.py`): extend the fake `gh run list` to honour `--event` and return `displayTitle` / `createdAt`. Then:
  - a PR-named failed run with no head-branch history redispatches;
  - an older PR-named failure behind a newer head-branch success does not;
  - update the existing assertion that a conflict dispatch carries `ref == "claude/issue-10"`.
- **Merge train** (`tests/test_review_merge_train.py`): the dispatch has no `--ref`, and a queued PR with an active PR-named dispatch run stays queued.
- **Text contracts**: forward-merge dispatch has no `--ref`; `workflow-templates/ai-review.yml` carries the `run-name`; update `test_conflict_dispatch_active_run_visibility.py`'s forward-merge and `--workflow internal-review.yml` assertions.
- **Local verification**: the changed test modules, ShellCheck (`-S warning`) on both scripts, `actionlint` on the changed workflows, and `wc -c` on the changed workflows (§27).

## Risks & Mitigations

- A consumer runs the new poller before its `ai-review.yml` sync adds the `run-name`: its default-branch dispatch runs are invisible to the PR-named lookups for up to a day, so the PR #3895 duplicate dispatch can recur there. ACCEPTED — bounded by the cycle-local `_CONFLICT_DISPATCH_TRACKER`, the `pr-autofix-<N>` concurrency group, and the `@stable` `repository_dispatch` sync that usually lands within minutes. Documented in the changelog.
- The 100 newest `workflow_dispatch` runs across all workflows cover about 4 hours in this repo (96 of 100 were `internal-review.yml` on 2026-09-28), close to the 240-minute review job budget. ACCEPTED — same window as #4618's per-workflow call; a miss costs a duplicate dispatch, not a security gap.
- The retrigger fallback now compares timestamps it did not read before. Mitigation: a missing `createdAt` counts as older, so the new path can only add a redispatch when a PR-named failure is definitely the newest run.
- The base branch (#4618's project) merges while this project is in flight. Mitigation: the issue-mode base-move rule retargets the final PR to `main` and switches it to `Fixes #4701`.

## Rollout

No flag: the change applies when the phase merges. This repo picks it up once the final PR reaches `main` (through #4618's final PR or directly after a base move). Consumer repos get the poller and merge-train change and the `ai-review.yml` `run-name` on the next `@stable` release and its sync. Roll back by reverting the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Which base branch does the project build on? — Picked: A — #4618's project branch `claude/implement-plan-issue-4618-sweep-dispatch-default-branch`. Alternatives: B — `main` (the default when the issue names no `Integration branch:` line); C — stop until #4618 merges. Why: the issue says to build on that branch or wait, the `run-name` and the `_has_active_autofix_run` pattern exist only there, and the issue-mode base-move rule retargets the project to `main` when #4618 merges. B would break the PR #3895 dedupe until #4618 lands, and C stalls an unattended project. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Is the forward-merge fallback dispatch exposed, and does it move? — Picked: A — move it to the default branch too. Alternatives: B — keep `--ref` with a comment saying the branch is workflow-cut. Why: the branch holds only `stable` / `main` content, but any writer can push to it between the fallback push and the dispatch, and only the default branch's `internal-review.yml` carries the PR `run-name`. The dispatch costs nothing extra. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Consumer repos dispatch `ai-review.yml`; how do their dispatched runs stay visible? — Picked: A — add `run-name: AI Review [pr:<N>]` to `workflow-templates/ai-review.yml` and match both names in every lookup. Alternatives: B — keep head-ref dispatch for `ai-review.yml` (leaves the finding open in consumer repos); C — dispatch from the default branch with no name (consumer dedupe blind, the PR #3895 loop). Why: A closes the finding everywhere, and the only gap is the sync window. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] How do the poller lookups find PR-named runs with one call in both repo kinds? — Picked: A — one shared `_pr_named_review_dispatch_runs` helper: `gh run list --event workflow_dispatch --limit 100` with no `--workflow`, exact-title match on both names. Alternatives: B — one `--workflow` call per wrapper (two calls, one a 404 in each repo kind); C — resolve and cache the wrapper file once per cycle (new cycle state). Why: one call (§15), no new state, and the same 100-run window #4618 accepted. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] How does the stall-judge lookup find PR-named runs? — Picked: A — extend its jq over the cached runs blob, which already carries `event` and `display_title`. Alternatives: B — one helper call when the blob matched nothing. Why: zero API calls (§15 prefers the existing cache), and the blob already holds the active runs the judge needs. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] When does the retrigger lookup issue its PR-named call, and which run decides? — Picked: A — only when the head-branch loop found no failed run; the newest completed PR-named run decides, and only when it is newer than every completed head-branch run. Alternatives: B — only when the head-branch loop found no completed run at all; C — any PR-named failure, regardless of age. Why: B misses the common case (a conflicted PR that has earlier `pull_request` runs), and C redispatches on a failure a later run already superseded. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-28] The empty-commit push guards also key by head branch; include them? — Picked: A — yes: cached scans at zero cost, and the direct fallback with one helper call only when its branch listing matched nothing. Alternatives: B — leave them (the issue lists only the stall-judge and retrigger lookups). Why: moving conflict dispatches to the default branch would otherwise let stall recovery push an empty commit under a live review run and discard its editor pass. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-28] Should `build_active_issue_set` map PR-named runs to issues? — Picked: A — no, leave it. Alternatives: B — resolve each PR-named run's PR to its linked issue (new per-run lookups). Why: B costs a PR-to-issue read per run (§15), and the paths that act on a stalled issue are all covered by `_has_active_autofix_run` and the push guards. Applied in: no code change. Status: pending review
- AD-9 [plan, 2026-09-28] How does the merge-train release see PR-named runs? — Picked: A — emit `pr:<N>` keys from the listing `_mt_inflight_review_branches` already makes. Alternatives: B — a per-PR helper call in the release loop. Why: zero new calls, and a per-item call inside that loop is a §15 review-blocker. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-28] `review_autofix.yml` is the last dispatch candidate and has no PR `run-name`; add one? — Picked: A — no; keep it as the last candidate, dispatched from the default branch. Alternatives: B — add a `run-name` to `review_autofix.yml`. Why: it is reached only when neither wrapper can be dispatched, and `review_autofix.yml` is the 451 KB reusable workflow every review runs through (§5, §27). Applied in: no code change. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #4701; issue #4618 and its project (final PR #4634, phase PR #4638).
- PR #3895 incident (duplicate conflict dispatches, 2026-08-29).
- `docs/plans/issue-4618-sweep-dispatch-default-branch-plan.md` on the base branch.
