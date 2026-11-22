# Pipeline human-wait elimination

## Summary

Remove seven recurring reasons the unattended pipeline stops and waits for a person. Six showed up together while clearing the 2026-11-17 human-wait backlog, and a seventh (R7) the next day, where about 20 items needed a human, several of them for days. Each fix is a deterministic check or a routing change behind its own default-on `_ENABLED` switch. None adds a new model call.

## Context

On 2026-11-17 an interactive session worked through every project and PR waiting on a person in `shubhodeep1/coding-workflows`. Each wait traced back to one of six root causes (R1–R6). R7 was found on 2026-11-18 while following project #8291's wave 19:

| # | Root cause | Incidents |
|---|---|---|
| R1 | PR CI quick does not run `tests/inventory_parity.py`, so INVENTORY gaps merge and turn `main` red. Heal intake then opens one issue per failing `main` head, and every long-lived integration branch fixes the gap on its own, which later conflicts. | #9535/#9563/#9566 merged the gap. Heals #9577, #9578, #9596. Check-triage heal #9584 was routed to PR branch `ai/issue-9503`, which lacked the failing files (`out_of_heal_scope`). INVENTORY conflicts on #8307 and #9124. |
| R2a | The conflict resolver can write content neither side had. | `[ai-merge-resolve]` `df30f80a` on `orchestrator/project-8291` flipped `ZOMBIE_RUN_REAPER_ENABLED` to `'true'` (citing the project plan's Q16) and kept duplicate INVENTORY entries. Fixed by hand in `8a1664a5`. |
| R2b | A modify/delete conflict on a non-fragment path is given to the model, which leaves it untouched. `check_resolver_diff.sh` then fails after the retry loop, the failure cap trips, and the PR is labelled `ai:review-blocked`. | PR #9452: `.github/workflows/check-failure-triage-historical.yml (index stages: 1 2)`, 8 identical failures, heal #9574. Resolved by hand in `dd171afc`. |
| R3 | Pipeline sandboxes cannot create files under `.github/ai/`, even when the trusted plan grant names the exact path. | #9401 (`.github/ai/alert_inbox.json`) stalled project #9394 for 5 days. #9449 (`.github/ai/chatgpt_pool.json`). |
| R4 | Holds wait on conditions that cannot become true. | #9242: `pr_on_branch` waited for #9240's commit on `stable`, but hotfix #9339 carried the same fix as a different commit. #9238/#9244: `placeholder_retired` waited for a repo variable after the fix had shipped. #8822: a counterfactual hold sat unanswered for 13 days. #9212: the stall judge linked plan-doc PR #9478 instead of implementation PR #9262. |
| R5 | "Planning blocked: work is already on the planning ref; nothing to implement" blocks for a human, even though the work is done. | #9472, #9583, #9238, #9244, #9447 |
| R6 | `ai:operator-step` entries without a source SHA are never re-checked, so finished steps stay unticked. | #6211 had 3 unticked steps that pipeline-visible signals showed were satisfied. |
| R7 | The final-CI merge-ref freshness guard (`scripts/ci_final_merge_ref_fresh.sh`, #9652) fails every job when the PR's merge ref's base is not the base branch's current tip. Final-CI jobs wait about an hour for runners, and an integration branch syncs `main` more often than that, so every run tests a base that has already moved and stops as `stale` before any test runs. | PR #9602 (wave 19 of project #8291), 2026-11-17 to 2026-11-18. Merge ref on `ac704952` while the tip was `1a943640`, then on `3851da5a` while the tip was `289c7eb5` after the 01:42 UTC sync. 24, then 36 jobs failed with `CI_FINAL_MERGE_REF ... outcome=stale reason=not_refreshed`, with the review head gate green throughout. |

The decisions were clarified in the authoring session (Q1–Q8 all `A`):
- All six root causes, one phase each. R2 and R4 are split into two phases because their halves touch different files.
- The approved-PR "update from main repeatedly" merge-loop logic is out of scope; another session owns it.
- Every new behaviour ships behind a default-on `_ENABLED` switch.

## Decisions

### D1 — Scope: all six root causes, one phase each
- **Chosen:** R1–R6, with R2 and R4 each split into two phases by file (Q1 A).
- **Alternatives considered:** only the project-stalling causes R1–R3; R1–R5 without the operator-step pass.
- **Why:** every root cause produced at least one multi-day human wait in the 2026-11-17 backlog, and each fix is small and independent.

### D2 — R1: quick-CI parity plus red-main dedupe
- **Chosen:** run `tests/inventory_parity.py` in CI quick `static-checks`, and dedupe red-main push-CI heals by workflow and failing job across head SHAs (Q2 A).
- **Alternatives considered:** dedupe only; the quick-CI check only; turning on `WORKFLOW_HEAL_CAUSE_GROUPING_ENABLED`.
- **Why:** the check stops the gap at its source, and the dedupe bounds the blast radius of any other red-main cause. Cause grouping applies far beyond red main.

### D3 — R2a: provenance check that retries
- **Chosen:** a diff3-based check in the resolver's soft-validation loop, with one retry naming the foreign hunks, then escalation (Q3 A).
- **Alternatives considered:** auto-revert the foreign hunks to the base side; log a warning only.
- **Why:** an auto-revert can silently drop a correct union, and a warning would not have stopped `df30f80a`.

### D4 — R2b: deterministic take-base for incidental modify/delete
- **Chosen:** take the base's deletion when a merged base PR deleted the file and the PR's change to it is a small cross-file sweep, recording the dropped edit. Otherwise give the model both versions and the stage context (Q4 A).
- **Alternatives considered:** always hand the path to the model; always take the deletion.
- **Why:** the #9452 shape is mechanical and needs no model. A blanket take-deletion could drop real work.

### D5 — R3: exact plan-granted `.github/ai/` paths only
- **Chosen:** admit creating or editing `.github/ai/<file>` only when that literal path is in the trusted plan grant (Q5 A).
- **Alternatives considered:** admit any new `.github/ai/*.json`; keep the exclusion and file an operator step with the content.
- **Why:** it unblocks plan-specified config files without widening the sandbox for anything the plan did not name.

### D6 — R4: holds end on evidence, or expire
- **Chosen:**
  - patch-equivalence for `pr_on_branch`;
  - a no-recurrence check for `placeholder_retired` on heals;
  - a 3-day counterfactual-hold expiry with one Telegram escalation, then close as not planned;
  - implementation-first stall PR resolution.

  (Q6 A)
- **Alternatives considered:** auto-approve expired counterfactual holds; patch-equivalence only.
- **Why:** each incident waited on something that could never happen. Closing is reversible, whereas auto-approving an unverified heal is not.

### D7 — R5: verify, then close
- **Chosen:** run the plan's acceptance tests (or tests the issue names) on the planning ref, and close through the existing `ai:implement-success-noop` path on a pass (Q7 A).
- **Alternatives considered:** close with no check; keep blocking and page once.
- **Why:** a deterministic check keeps the close honest. Paging still needs a person.

### D8 — R6: daily observable tick
- **Chosen:** extend `operator_step_issue.py tick` with observable conditions, and run it daily from `workflow-log-analysis.yml` (Q8 A).
- **Alternatives considered:** only stop the judge writing steps for observable settings.
- **Why:** existing stale entries also need clearing, and the tick already exists.

### D9 — Leave the approved-PR merge loop alone
- **Chosen:** no change to the `ai:merge-base-sync:v1` repeated update-from-main logic (Q9, assumed A).
- **Alternatives considered:** include it here.
- **Why:** another session is investigating it.

### D10 — R7: accept a stale merge ref when only default-branch syncs and resolver commits moved the base
- **Chosen:** the freshness guard accepts a stale merge ref only when all of these hold. Otherwise it fails as today (Q17 A, added 2026-11-18).
  - The PR's base is not the default branch, so only integration-branch PRs qualify. A stale default-branch-based PR still fails.
  - The tested base is an ancestor of the current tip.
  - Every commit between them that is not on the default branch is an integration-sync merge, whose second parent is a default-branch commit, or an `[ai-merge-resolve]` commit.
  - None of the files changed between them, counting the old path of a rename, is a file the PR changes.
- **Alternatives considered:** pause main → integration syncs while an approved PR's final CI run is queued (Q17 B); both (Q17 C).
- **Why:** a deterministic check in the guard itself keeps a narrower guarantee: the tested base differs from the tip only by main syncs and `[ai-merge-resolve]` resolver commits, none of which touches the PR's files. Resolver commits include non-merge, model-authored commits such as `[ai-merge-resolve] repair post-sync test failures` (`scripts/review_conflict_resolve.sh`), so their content is accepted on the same no-overlap rule. Runner queue time then cannot livelock a PR. That is not the full "never test an old base" guarantee, and Risks (R7) records the residue. Pausing syncs would change orchestrator poller timing next to the approved-PR merge loop, which another session owns (D9).

## Goals

- A PR that would make inventory parity fail on `main` fails CI quick on its own push.
- Repeated red-`main` push-CI failures with the same workflow and failing job become occurrence comments on one open heal, not new issues.
- A check-triage heal is never routed to a branch that lacks the files it names.
- A resolved file whose changed region matches neither side fails resolver validation and gets one retry. The retry prompt names the offending hunks.
- A non-fragment modify/delete conflict is resolved deterministically when the base deleted the file through a merged PR and the PR's change to it is incidental. Otherwise the model receives both versions and the stage information. Either way, the resolve never ends with an untouched unmerged path.
- Implement, review-editor and heal sandboxes can create or edit a `.github/ai/<file>` path when, and only when, that exact path is in the trusted plan grant's `files_touched`.
- A `pr_on_branch` hold counts as met when the PR's patch is already on the branch (patch-id equivalence).
- A `placeholder_retired` hold on an `ai:workflow-heal` item ends when the source failure has not recurred.
- A counterfactual hold expires after `HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS` (default 3), with one Telegram escalation, and closes as not planned unless someone has answered.
- The stall judge's single-PR resolution prefers the issue's implementation PR over a cross-reference.
- A "nothing to implement" planning block closes the issue through the existing verify-and-close path when a deterministic acceptance check passes. Only a failing check leaves it blocked.
- A daily pass ticks operator steps whose condition is observable and closes the tracker when nothing is left.
- A final CI run whose merge ref is behind the base only by integration-sync merges and `[ai-merge-resolve]` commits that touch none of the PR's files runs its tests instead of stopping as `stale`.

## Non-goals

- The approved-PR "update from main repeatedly" merge loop (`ai:merge-base-sync:v1`). Another session owns it.
- Dispatching, re-running or cancelling `test-and-mark-stable.yml` or `promote-main-to-stable.yml`.
- Admitting `.github/ai/**` globs, or any `.github/ai/` path not literally in the plan grant (Q5).
- Changing the counterfactual gate's verdict logic. Only the hold's lifetime changes.
- The `orchestrate-poll (3)` shard runtime and pre-push gate timeouts seen in the same session. Those are separate budget items.

## Constraints

- **§6:** no renames or removals. Every new env var, marker and log key is new and unique; each was checked against `main` at `b699a07c`. Existing identifiers keep their meaning. That includes `WORKFLOW_HEAL_CAUSE_GROUPING_ENABLED`, which stays default `false`.
- **§4:** every new env var has a default.
- **§15:**
  - New GitHub calls reuse fetched data where it exists.
  - Patch equivalence uses local git (`git cherry`) after one `git fetch` of the target branch, with no extra API calls.
  - The recurrence check reuses `heal_stale_decision` and its run caches (`HEAL_STALE_RUNS_CACHE_JSON`).
  - The operator-step pass reads the tracker issue's comments once per run.
- **§18:** no manual scripts. Everything runs from existing scheduled or PR-triggered workflows (see Automation below).
- **§19:** PRs reference tracking issues with `Refs #N` only.
- **§20 and §30:** each phase ships one `changelog.d/` fragment and, where behaviour changes, one `agents.d/` fragment. Neither `CHANGELOG.md` nor `agents.md` is edited directly.
- **§27:** `ci.yml` and `review_autofix.yml` are large. Each phase that touches a workflow checks the file stays under 480,000 bytes with `tests/test_workflow_file_size_limit.py`. New step bodies go into `scripts/`.
- **§33:** no phase adds a job, a workflow or a matrix leg. Phase 1 adds a step to the existing `static-checks` job, Phase 8a a step in `plan.yml`'s existing job, and Phase 9 a daily step in the existing `collect-logs` job. Jobs per triggering event are unchanged before and after.
- **§14:**
  - Phases 3, 4 and 6 change reusable workflows and scripts that consumers receive at the next `@stable` sync. Consumers in `.github/ai/consumer_repos.json` get them automatically, with no wrapper changes.
  - Phase 1's `ci.yml` change applies only to this repo's own CI.

## Automation (§18.E)

- **Scripts:** no new standalone scripts. Phases extend existing scripts and add helpers inside them. Phase 8's daily step calls the existing `scripts/operator_step_issue.py tick`.
- **Entry points:**

  | Phase | Wired into | Trigger |
  |---|---|---|
  | 1 | `.github/workflows/ci.yml` `static-checks` | `ci_pr_quick.yml` on every PR push |
  | 2 | `scripts/workflow_failure_heal_intake.sh` and `scripts/check_failure_triage.sh` | the existing heal intake and check-triage workflows |
  | 3, 4 | `scripts/review_conflict_prepare.sh` and `scripts/review_conflict_resolve.sh` | the existing review_autofix.yml resolver steps |
  | 5 | implement.yml, review_autofix.yml and heal runs | the existing runs of those workflows |
  | 6 | `scripts/unblock_judge.sh` | the unblock judge's existing schedule |
  | 7 | `scripts/orchestrate_poll_process.sh` | the poller's existing schedule |
  | 8a | `plan.yml` | plan runs, for new blocks |
  | 8b | `scripts/unblock_judge.sh` | the unblock judge's existing schedule, for existing blocks |
  | 9 | `.github/workflows/workflow-log-analysis.yml` `collect-logs` | the existing daily `0 6 * * *` schedule |
  | 10 | `scripts/ci_final_merge_ref_fresh.sh`, already the step after each final-CI checkout in `ci.yml`; the ten step `env:` blocks gain the new switch | final-CI `workflow_dispatch` runs (no new step or job) |

- **Supervisor:** none. Every phase rides an existing scheduler.
- **DB work:** none.
- **`docs/scripts-pending-removal.md`:** no entries. Nothing single-use or long-running is added.

## Approach

Every fix extends an existing mechanism instead of adding a parallel one:
- **R1:** a quick-CI step, a dedupe rule in `budget_decision`, and a guard in check-triage routing.
- **R2a:** a soft validation next to `_verify_fingerprints_soft` inside the resolver retry loop, so it can retry.
- **R2b:** a deterministic pre-step next to the folded-fragment keep rule in `review_conflict_prepare.sh`, plus stage-aware context for the model.
- **R3:** thread the existing automation grant (exact literal paths only, already produced by `build_automation_grant`) into the two sandbox gates that ignore it today.
- **R4:** new `met`/`impossible` branches in `unblock_hold_condition`, an expiry around the existing `_heal_counterfactual_hold_notify`, and a resolution-order fix in the stall PR lookup.
- **R5:** route the planner block into the existing `unblock_nothing_to_implement_close` path, with a deterministic test run added first.
- **R6:** broaden the existing `tick` and call it daily.

Alternatives considered:
- **Turn on `WORKFLOW_HEAL_CAUSE_GROUPING_ENABLED` instead of adding a red-main key.** Rejected. That flag groups across all branches and signature shapes. Turning it on changes behaviour well beyond red `main`.
- **Make the provenance check a hard invariant in `check_resolver_diff.sh`.** Rejected. It runs after the loop, so a failure there gets no retry, which Q3 requires.

## Phases & Merge Strategy

There are ten phases and eleven PRs (Phase 8 ships as 8a and 8b). Phase 10 was added on 2026-11-18, after project #9650 had already split Phases 1–9, so it is filed as its own `ai:engine-claude` issue rather than as a wave of #9650. Each PR is independently mergeable in any order, and production-safe at merge because its behaviour sits behind a default-on switch that can be turned off without a code change. No phase reads another phase's switch or output.

1. **Inventory parity in CI quick (R1a).** Runs the parity check on every PR push.
   - **Files:** `ci.yml` `static-checks`; `tests/test_ci_pr_quick.py`.
   - **Done:** a PR that breaks parity fails `quick / static-checks`.
   - **Rollback:** revert the PR. No switch is needed: the step is read-only and takes under 2 seconds.
2. **Red-main heal dedupe and check-triage target guard (R1b, R1c).**
   - **Files:** `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`, `scripts/check_failure_triage.sh`, tests.
   - **Done:** a second red-main failure posts an occurrence comment, and triage falls back to the default branch when the PR branch lacks the files.
   - **Rollback:** `WORKFLOW_HEAL_RED_MAIN_GROUPING_ENABLED=false` and `CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED=false`.
3. **Resolver provenance check (R2a).**
   - **Files:** `scripts/review_conflict_resolve.sh`, a new retry prelude under `prompts/`, tests.
   - **Done:** a resolved region matching neither side fails soft validation, retries once naming the hunks, and escalates on a second failure.
   - **Rollback:** `RESOLVER_PROVENANCE_CHECK_ENABLED=false`.
4. **Modify/delete conflict handling (R2b).**
   - **Files:** `scripts/review_conflict_prepare.sh`, `scripts/review_conflict_resolve.sh`, the `prompts/conflict-resolver.txt` context section, tests.
   - **Done:** the #9452 shape resolves without a model call. Other modify/delete paths reach the model with stage context, and an untouched unmerged path is caught inside the loop.
   - **Rollback:** `CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED=false`.
5. **Plan-granted `.github/ai/` writes (R3).**
   - **Files:** `scripts/implement_commit_changes.sh`, `scripts/review_untrusted_workspace.py`, `scripts/heal_isolated_implement.sh`, tests.
   - **Done:** an exact granted `.github/ai/x.json` is created and committed by the implement, editor and heal paths, while an ungranted one is still refused.
   - **Rollback:** `GITHUB_AI_PLAN_GRANT_WRITE_ENABLED=false`.
6. **Unblock hold re-evaluation (R4a).**
   - **Files:** `scripts/unblock_judge.sh`, `scripts/workflow_failure_heal.py` (CLI reuse only), tests.
   - **Done:** a patch-equivalent hotfix meets `pr_on_branch`, and a non-recurring heal ends `placeholder_retired`.
   - **Rollback:** `UNBLOCK_HOLD_PATCH_EQUIVALENCE_ENABLED=false` and `UNBLOCK_PLACEHOLDER_RECURRENCE_CHECK_ENABLED=false`.
7. **Counterfactual hold expiry and stall PR resolution (R4b).**
   - **Files:** `scripts/orchestrate_poll_process.sh`, tests.
   - **Done:** a hold older than 3 days escalates once and then closes as not planned, and the stall judge picks the `ai/issue-N` PR.
   - **Rollback:** `HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED=false` and `STALL_PR_RESOLVE_IMPLEMENTATION_FIRST_ENABLED=false`.
8. **Nothing-to-implement verify and close (R5), split by file.**
   - **8a (plan.yml):** a block whose acceptance check passes is closed `ai:closed` with the no-op marker, and a failing check still blocks. **Files:** `.github/workflows/plan.yml` and a new step script under `scripts/`.
   - **8b (unblock judge):** existing blocks close on the judge's next run without a fix-up. **Files:** `scripts/unblock_judge.sh`, tests.
   - **Rollback:** `PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED=false` (covers both halves).
9. **Daily operator-step tick (R6).**
   - **Files:** `scripts/operator_step_issue.py`, `.github/workflows/workflow-log-analysis.yml`, tests.
   - **Done:** observable steps are ticked daily, and an empty tracker is closed.
   - **Rollback:** `OPERATOR_STEP_DAILY_TICK_ENABLED=false`.
10. **Stale merge ref accepted when only syncs and resolver commits moved the base (R7).**
    - **Files:** `scripts/ci_final_merge_ref_fresh.sh`, `.github/workflows/ci.yml` (the ten final-CI check steps' `env:` blocks), `tests/test_ci_final_merge_ref_fresh.py`, README env var table, fragments.
    - **Done:** the #9602 shape logs `outcome=stale_accepted`, and the job runs its tests. That shape is an integration-branch PR whose merge ref is behind only by `chore: sync <default branch> into <base>` merges and `[ai-merge-resolve]` commits (merge or not), none of which touches the PR's files. Any other stale merge ref still fails, including every stale merge ref of a PR based on the default branch.
    - **Rollback:** `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED=false`.

Phases 3 and 4 both edit `scripts/review_conflict_resolve.sh`, in different functions: the soft-validation block at about :4208 and the targeted-paths block at about :3423. They can conflict textually but not logically, and either can merge first. Phases 6 and 8b both edit `scripts/unblock_judge.sh`, again in different functions (`unblock_hold_condition` and `unblock_nothing_to_implement_close`).

## Implementation Steps

### Phase 1: inventory parity in CI quick

1. In `.github/workflows/ci.yml` job `static-checks`, add an `Inventory parity (quick)` step after `CI test wiring guard` (about :337) running `PYTHONDONTWRITEBYTECODE=1 python3 tests/inventory_parity.py`.
   - Keep the existing `Inventory parity` step in `tests-release-and-log-analysis` (:2691) unchanged, so `tests/test_ci_inventory_parity_order_contract.py` still holds.
2. In `tests/test_ci_pr_quick.py`, add a test asserting that `static-checks` contains a step whose `run` invokes `tests/inventory_parity.py`.
3. Add `changelog.d/<pr>-ci-quick-inventory-parity.md`.

### Phase 2: red-main heal dedupe and triage target guard

1. In `scripts/workflow_failure_heal.py`, add `red_main_group_key(workflow_name, failing_job, branch)`. It returns a key only for `push` CI runs on the default branch, and `""` otherwise. Its docstring states the input shape, the output, and that it makes no API calls.
2. In `budget_decision` (about :5937), when `WORKFLOW_HEAL_RED_MAIN_GROUPING_ENABLED` is `true` (the default), first look for an open heal carrying the marker `<!-- workflow-failure-heal:red-main-group=<key> -->`.
   - If one is found, return `action=duplicate`, so the existing occurrence-comment path (`compose_occurrence_comment`, :8045) records the new head and run.
   - Otherwise, continue to today's fingerprint logic.
   - New heals for red-main runs carry that marker in their body.
3. In `scripts/workflow_failure_heal_intake.sh` near the fingerprint call (:1996), pass `failing_job` and the branch into the decision, and add `WORKFLOW_HEAL_RED_MAIN_GROUPING_ENABLED` (default `true`) next to the existing flags (:312/:361). Log `WORKFLOW_HEAL red_main_group key=<k> outcome=duplicate|new|skipped reason=<token>`.
4. In `scripts/check_failure_triage.sh`, add a guard after routing sets `ROUTING_TARGET_BRANCH="${HEAD_REF}"` (:1239), behind `CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED` (default `true`):
   - **Check:** read the failing files the triage extracted, using the existing metadata. Check them against `HEAD_REF` with the local `git cat-file -e` (the checkout already has the ref; no API call).
   - **Fall back:** when none exist on `HEAD_REF` but they exist on the default branch, route to the default branch.
   - **Log:** `routing outcome=fallback reason=target_branch_lacks_files target_branch=<default>`.
5. Tests:
   - `tests/test_workflow_failure_heal.py`: grouping hit, miss, non-main branch, and switch off.
   - A new `tests/test_check_failure_triage_target_files_guard.py`: stubbed git, fallback, no fallback, and switch off. Wire it into `ci.yml` per `tests/test_ci_wires_every_test_file.py`.
6. Fragments: `changelog.d/` and `agents.d/` (section "Workflow architecture", plus the log-prefix registry if a new prefix is added; this phase reuses `WORKFLOW_HEAL`). README env var table rows for both switches.

### Phase 3: resolver provenance check

1. In `scripts/review_conflict_resolve.sh`, add `_verify_resolution_provenance` next to `_scan_residual_markers` (:2583). For each path in the allowlist with stages 2 and 3:
   - Run `git merge-file -p --diff3` on the `:1:`, `:2:` and `:3:` blobs (taken with `git show :N:path` before the model runs and cached in the attempt runtime dir).
   - Compare each changed line range of the resolved file, from `git diff --no-index`, against the union of lines present in ours or theirs.
   - Report any region whose lines appear in neither, and write the offending hunks (path, line range, text capped at 40 lines) to `RESOLVER_PROVENANCE_REPORT_FILE`.
   - Skip binary files. Skip paths handled deterministically by prepare (fragments, manifests, host-only, `.ai/`), since prepare records them.
2. Call it in the soft-validation block (about :4208-4216) when `RESOLVER_PROVENANCE_CHECK_ENABLED` is `true` (the default):
   - **Failure:** set `_prev_attempt_failure_kind="provenance"`.
   - **Retry:** `_build_retry_prompt` (:2504) appends the new `prompts/conflict-resolver-retry-provenance-prelude.txt` with the report. Allow at most one provenance retry per run (`RESOLVER_PROVENANCE_RETRY_MAX`, default `1`).
   - **Escalation:** a second provenance failure ends the loop with `RESOLVER_TERMINAL_EXIT_CLASS="provenance"`, which goes through the existing failure comment and heal path.
3. Log `Resolver provenance: path=<p> outcome=ok|foreign_hunks hunks=<n>`.
4. Tests: a new `tests/test_review_conflict_resolve_provenance.py`, run as a real temporary git repo with three-way blobs. Cases: an ours/theirs union passes; the `df30f80a` shape (a new value in neither side) fails; whitespace-only changes pass; a binary file is skipped; the switch is off. Wire it into `ci.yml`.
5. Fragments plus a README env var row. Update `prompts/` validation if it pins the prelude file list.

### Phase 4: modify/delete conflict handling

1. In `scripts/review_conflict_prepare.sh`, after the folded-fragment block (about :586), add a non-fragment modify/delete rule behind `CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED` (default `true`). For each unmerged path with stages `1 2` (PR modified, base deleted) that is not a fragment:
   - **Locate the deleting PR:** find the base commit that deleted the path, using `git log --diff-filter=D -1 --format=%H <merge-base>..<base> -- <path>`, and its PR number from the squash subject's `(#N)` suffix.
   - **Containment test:** the PR's own diff to the path (`git diff <merge-base> <pr-head> -- <path>`) touches at most `CONFLICT_MODIFY_DELETE_MAX_CHANGED_LINES` lines (default 20). Every changed line also matches a pattern the PR applies to other surviving files in the same commit range. Concretely: each changed line's normalised text, with leading whitespace and the file path removed, occurs as a changed line in at least one other file of the PR. That catches mechanical sweeps such as a `runs-on` pin.
   - **When both hold:** `git rm` the path and record it in `RESOLVER_DROPPED_EDITS_FILE`. Log `Modify/delete take-base: path=<p> deleted_by=#<n> pr_lines=<k>`.
   - **Otherwise:** leave the path for the model.
2. Report dropped edits on the PR: the existing resolver success summary lists each path with its deleting PR and a 10-line excerpt of the dropped diff. That reuses the existing comment; there is no new comment.
3. In `scripts/review_conflict_resolve.sh`, where targeted paths are assembled (:3423-3465), add per-path stage context for modify/delete paths left to the model: "deleted on base by #N / modified on base", the surviving side's content path, and the instruction to either restore the file (keep) or `git rm` it (accept the deletion). Then move the untouched-unmerged-path check (today `check_resolver_diff.sh:220-237`) into the soft-validation block as failure kind `unmerged_left`, so it retries with a prompt naming the path. The post-loop check stays as a backstop.
4. Tests: a new `tests/test_review_conflict_modify_delete.py` using real temporary git repos. Cases:
   - the #9452 shape (a `runs-on` pin applied across workflows, file deleted on base) takes base without a model run;
   - a substantive PR edit is left to the model;
   - the deleting commit has no `(#N)`, so the file is left to the model;
   - the switch is off;
   - the in-loop `unmerged_left` retry fires.

   Wire into `ci.yml`.
5. Fragments plus a README env var row.

### Phase 5: plan-granted `.github/ai/` writes

1. In `scripts/implement_commit_changes.sh:349`, keep `':!.github/ai'` in `add_o_excludes`. After the main untracked-file add, when `GITHUB_AI_PLAN_GRANT_WRITE_ENABLED` is `true` (the default), `git add --` each exact `.github/ai/<path>` from the existing automation grant (`AUTOMATION_PATH_GRANT_FILE`, built by `build_automation_grant`, literal paths only) that exists in the worktree.
   - Log `IMPLEMENT_GITHUB_AI_GRANT path=<p> outcome=staged|absent`.
   - The deletion guard at :401/:436 is unchanged.
2. In `scripts/review_untrusted_workspace.py`, accept an optional `--granted-paths-file` (a JSON list of literal paths) in snapshot, enumerate and transfer.
   - `allowed()` (:138-165) admits a `.github/ai/<file>` path only when it is in that list.
   - The heal-scope denial for `.github/ai/` (:491) makes the same exception.
   - Entries containing `*`, `?`, `[`, `..` or a trailing `/` are rejected when the file is read.
   - `.claude/` handling is unchanged.
3. Pass the grant file from the review editor sandbox invocation and from `scripts/heal_isolated_implement.sh` (:86, :309, :833, :860), only when the switch is `true` and the file exists. Review and heal runs build the grant from the trusted plan grant comment with the existing `build_automation_grant`, so no new parser is needed.
4. Tests:
   - extend `tests/test_files_touched_scope_guard.py` to show globs never reach the grant;
   - add a new `tests/test_github_ai_plan_grant_write.py` covering: a granted exact path is staged and transferred; an ungranted path is refused; a glob entry is refused; a symlink under `.github/ai/` is refused; the switch is off.

   Wire into `ci.yml`.
5. Fragments plus a README env var row. Update the `review_editor_sandbox_path_excluded` remedy text (`workflow_failure_heal.py:5726`) to say that a plan-granted `.github/ai/` path is now admitted.

### Phase 6: unblock hold re-evaluation

1. In `scripts/unblock_judge.sh` `unblock_hold_condition`, `pr_on_branch` branch (:411-436): when the compare result is `ahead|diverged` and `UNBLOCK_HOLD_PATCH_EQUIVALENCE_ENABLED` is `true` (the default):
   - `git fetch --no-tags origin <branch>` once per judge run (cached per branch in the run dir).
   - Run `git cherry origin/<branch> <merge_commit_sha>` and print `met` when every line starts with `-`.
   - Log `op=hold hold=pr_on_branch outcome=met reason=patch_equivalent`.
   - A fetch failure keeps today's `unmet`.
2. In the `placeholder_retired` branch (:439-472): before printing `unmet`, if the item has `ai:workflow-heal` and a `workflow-failure-heal:source=` marker, and `UNBLOCK_PLACEHOLDER_RECURRENCE_CHECK_ENABLED` is `true` (the default):
   - Call `python3 scripts/workflow_failure_heal.py heal-stale-decision` with the stages `target`, `resolve` and `decide`, the same CLI `_heal_stale_close_check` uses, with the existing cache env.
   - On a `close` decision, print `impossible`. Then post one comment, ending `<!-- ai:unblock-wait:v1 item=<n> hold=placeholder_retired ref=<NAME> done -->`, that says the failure has not recurred and gives the green-run count, and close the item as `completed`.
   - For `#<n>`-sourced heals with no run source, check that the source issue is closed and no new occurrence comment is newer than `HEAL_STALE_MIN_AGE_HOURS`.
   - Log `op=hold hold=placeholder_retired outcome=impossible reason=no_recurrence green_runs=<k>`.
3. Tests: `tests/test_unblock_judge.py` cases for patch-equivalent met, cherry `+` unmet, fetch failure unmet, no recurrence closes, recurrence stays unmet, and each switch off.
4. Fragments plus README env var rows.

### Phase 7: counterfactual hold expiry and stall PR resolution

1. In `scripts/orchestrate_poll_process.sh`, inside the `heal_counterfactual_hold` branch (:20102-20212), after `_heal_stale_close_check` and before `_heal_counterfactual_hold_notify`, add `_heal_counterfactual_hold_expiry_check`. Behind `HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED` (default `true`) and `HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS` (default `3`, clamped 1–30):
   - **Age:** read the trusted `ai:heal-counterfactual-hold:v1` marker comment's `created_at` from the tracking-comment data already fetched for the gate (no new API call).
   - **Escalate:** past half the limit, post one escalation comment and send one Telegram WARNING. The comment is marked `<!-- ai:heal-counterfactual-hold-escalated:v1 issue=<n> -->` and deduplicated by that marker.
   - **Close:** past the limit, with no trusted `/approved`, `/reclarify` or `/answer` after the hold, post `<!-- ai:heal-counterfactual-hold-expired:v1 issue=<n> days=<d> -->`. The comment says how to reopen. Then close the issue as `not_planned`.
   - **Log:** `HEAL_COUNTERFACTUAL_HOLD_EXPIRY issue=<n> outcome=escalated|expired|kept|skipped reason=<token> age_days=<d>`, with the prefix added to the registry.
2. In the stall judge's single-PR resolution, where `_issue_cross_ref_pr_number_last` (about :4940) chooses "the" PR, behind `STALL_PR_RESOLVE_IMPLEMENTATION_FIRST_ENABLED` (default `true`):
   - prefer `_resolve_linked_pr_fresh_by_branch` (`ai/issue-N`, :5105);
   - then a cross-referenced PR that passes `_linked_pr_is_issue_implementation` (:5051);
   - fall back to the last cross-reference only when neither exists;
   - skip a candidate whose changed files are all under `docs/plans/`, using the file list from the PR data already fetched, or one `pulls/<n>/files` call cached in `STALL_MANAGED_LINKED_PR_CACHE`.
3. Tests: extend `tests/test_stall_recovery_pr_lookup.py` with the #9212 shape (plan-doc PR cross-referenced last, implementation PR on `ai/issue-9212`). Add `tests/test_heal_counterfactual_hold_expiry.py` covering kept, escalated once, expired, an answered hold kept, and the switch off. Wire into `ci.yml`.
4. Fragments: the `agents.d/` block plus log-prefix registry lines. README env var rows.

### Phase 8: nothing-to-implement verify and close

1. **(8a)** In `.github/workflows/plan.yml`, before posting the "Planning blocked: human input required." comment (:2398), when `BLOCKED_REASON` equals the nothing-to-implement reason and `PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED` is `true` (the default), run the new `scripts/plan_nothing_to_implement_verify.sh` (§27: the body lives in `scripts/`). It works as follows:
   1. Collect test paths from the plan's `ACCEPTANCE:` lines, using `scripts/acceptance_check.py`'s `parse_acceptance_lines`.
   2. If there are none, add `tests/...` paths named in the issue body, through a new `extract_issue_test_paths` in `acceptance_check.py` (only paths that exist on the planning ref, at most 10).
   3. Run them with `pytest -q -p no:cacheprovider` in the credential-free test environment the pre-push gate already uses, with a 600 s cap.
   4. **All pass (at least one test):** post "Already implemented — verified" with the test list and `<!-- ai:implement-success-noop:v1 issue=<n> -->`, swap labels to `ai:closed` exactly as `implement.yml:5572-5590` does, and close as completed.
   5. **No tests found, a failure, or a timeout:** post today's blocked comment with an extra line giving the verify outcome.
   6. **Log:** `PLAN_NOTHING_TO_IMPLEMENT issue=<n> outcome=closed|blocked reason=passed|no_tests|failed|timeout tests=<k>`.
2. **(8b)** In `scripts/unblock_judge.sh` `unblock_nothing_to_implement_close` (:570-640), allow the close without a finished fix-up when a trusted `ai:implement-success-noop:v1` or `PLAN_NOTHING_TO_IMPLEMENT ... outcome=closed`-equivalent marker exists, under the same switch. This clears blocks that existed before 8a merged.
3. Tests:
   - a new `tests/test_plan_nothing_to_implement_verify.py`: passing tests close; a failing test keeps the block; no tests keeps the block; the switch is off; tests named only in the issue body are used;
   - `tests/test_unblock_judge.py` cases for 8b.

   Wire into `ci.yml`.
4. Fragments plus README env var rows.

### Phase 9: daily operator-step tick

1. In `scripts/operator_step_issue.py` `tick` (:422), add observable conditions for entries without a source SHA, under `OPERATOR_STEP_DAILY_TICK_ENABLED` (default `true`). Each is checked with data the run already has or one cached call:
   - **`allow_auto_merge`:** the step text names Allow auto-merge. Read `repos/<r>` once and check `allow_auto_merge=true`.
   - **Credentials:** the step text names `GH_PAT` / `OPENROUTER_API_KEY`. A successful `review_autofix.yml` or `security-audit.yml` run in the last 7 days counts, from one `actions/runs?status=success&per_page=1` call per workflow.
   - **Stable release:** the step text says the stable tag must contain merge `#N`. Check that the PR's merge commit is an ancestor of the `stable` tag, using the existing `_is_ancestor`.
   - **Conditional steps:** a step whose text says it applies only when a command is invoked is ticked with reason `conditional_not_a_gate`.

   Each tick posts the existing `_render_ticked` comment with `Done: <reason>`. When every entry in a tracker is done, the pass comments once and closes the tracker as completed. Log `OPERATOR_STEP_TICK key=<k> outcome=ticked reason=<condition>` and `OPERATOR_STEP_TICK tracker=<n> outcome=closed`.
2. Add a `--trusted-login` argument to `tick`, defaulting to today's `gh api user` lookup. The daily step passes `github-actions[bot]` because `/user` fails with `github.token`.
3. In `.github/workflows/workflow-log-analysis.yml` job `collect-logs`, add the step `Tick observable operator steps`. It uses the daily condition `github.event_name == 'schedule' && github.event.schedule == '0 6 * * *'` and is `continue-on-error: true`. It runs `git fetch --no-tags origin refs/tags/stable:refs/tags/stable` then the tick, with its body in a script under `scripts/` (§27).
4. Tests: extend `tests/test_operator_step_issue.py` with each condition, the tracker close, the switch off, and the trusted-login override. Add a workflow contract assertion that the step is daily-only.
5. Fragments plus a README env var row.

### Phase 10: stale merge ref accepted when only syncs and resolver commits moved the base

1. In `scripts/ci_final_merge_ref_fresh.sh`, add `ci_final_merge_ref_sync_only`. Call it in the main flow where the refresh attempts have run out and the merge ref is still stale (today's `outcome=stale` exit at about :181).
   - **Switch:** it runs only when `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED` is `true` (default `true` in the script itself; same true/1/yes/on parsing as `CI_FINAL_MERGE_REF_FRESH_ENABLED`). In this switch, in `ci_final_merge_ref_sync_only` and in the `sync_only_no_overlap` reason, "sync only" is shorthand for "only default-branch syncs and `[ai-merge-resolve]` commits". These names are final as written here, because renaming them after Phase 10 ships would be a breaking change.
   - **Inputs:** the PR number, `tested_base`, `tip` (the live base tip), the base ref, the default branch and the dispatched head (`GITHUB_SHA`).
   - **`tested_base`:** the first parent of the commit actually checked out. The loop's `first` is not the right value: the refresh loop reassigns it from each re-fetched ref (about :159), and when no refresh succeeds the job still has the original checkout. Capture it once before the refresh loop, from the raw commit object (`ci_final_merge_ref_parents HEAD`, since a shallow clone hides it from `rev-parse`), into its own variable.
   - **Default branch:** read with `git ls-remote --symref origin HEAD`, with no API call, like the tip read. Its symref line is `ref: refs/heads/<name>`, a tab, then `HEAD`. Keep only `<name>`, the short name, as `scripts/load_workflow_overlay.py` does (`ref: refs/heads/(.+)\tHEAD`). Condition 0 compares that short name with the short `base.ref` from `pulls/<n>`, and condition 2 builds the sync subject from it. An unreadable or empty name fails as `detail=api_error`.

   It accepts only when all of these hold, checked in this order and stopping at the first failure:
   0. **Integration base only:** the base ref is not the default branch. For a default-branch-based PR every commit in range is a default-branch commit, so the commit-type check below would pass vacuously.
   1. **Ancestry and range:** `GET compare/<tested_base>...<tip>` reports `status=ahead`, and the listing is complete: `total_commits` is at most 250, the `commits` array holds exactly `total_commits` entries (the check `scripts/workflow_failure_heal.py` uses, `total_commits > len(commits)`, means a cut page), and `files` is under GitHub's 300-file cap. The same `commits` count check applies to the second compare in (2). Either shortfall fails as `detail=truncated`, so commits are never classified from a partial page.
   2. **Commit types:** each listed commit is a default-branch commit, an integration-sync merge, or an `[ai-merge-resolve]` commit, judged as follows.
      - **Default-branch commits:** `GET compare/<tested_base>...<default branch>` gives the set of default-branch commits in range. Any commit in that set is fine. This listing must be complete too (`total_commits` at most 250); a truncated one fails as `detail=truncated` rather than classifying commits against a partial set.
      - **Remaining commits:** each must be either a two-parent merge whose subject (the first line of the compare API's `commits[].commit.message`) is exactly `chore: sync <default branch> into <base ref>` (the subject `scripts/orchestrate_poll_process.sh` writes, with `<default branch>` from the `ls-remote --symref` read above, not a literal `main`) and whose second parent is in that default-branch set, or a commit whose subject starts with `[ai-merge-resolve]`, merge or not (D10 records that this admits resolver-authored content). The subject and parent count are author-controlled, so the second-parent check keeps a crafted merge from passing as a sync.
   3. **No overlap:** none of the files from (1) is in the PR's changed-file list (`GET pulls/<n>/files`, paginated, at most 3000 files; a larger PR fails). That listing returns no total, so the total is `changed_files` from `pulls/<n>`, read in the guard's existing `pulls/<n>` call by widening its jq filter (no extra call). `changed_files` above 3000 fails as `detail=too_many_files` before any listing page is fetched. A listing whose entry count differs from `changed_files` fails as `detail=truncated`, so the overlap check never runs on a cut list. On both sides, the compared set holds each entry's `filename` and, for `status=renamed`, its `previous_filename`. A sync that renames a file the PR touches therefore counts as an overlap.

   When all hold:
   - **Checkout:** keep the checked-out merge, which still merges the dispatched head.
   - **Log:** `CI_FINAL_MERGE_REF ... outcome=stale_accepted reason=sync_only_no_overlap tested_base=<sha> tip=<sha> commits=<k> files=<m>` and exit 0.
   - **Job summary:** write one line to `$GITHUB_STEP_SUMMARY` saying the run tested base `<tested_base>`, and that the sync commits up to `<tip>` touch none of the PR's files.

   On any failed condition or unreadable response, keep today's behaviour: `::error::` and exit 1 with `outcome=stale`, adding `detail=<first failed condition>` (`default_base`, `not_ahead`, `truncated`, `foreign_commit`, `file_overlap`, `too_many_files`, `api_error`).
2. **API budget (§15):** document it in the header, and only on the already-failing stale path, never on the fresh path.
   - **Count:** two compares, plus the paginated PR files listing at 100 per page, at most 30 pages for the 3000-file cap. That is up to 32 GETs per final-CI job.
   - **Cost per run:** each of the ten final-CI jobs that reaches the stale exit pays it.
   - **What keeps it low:** the cheap checks run first. Condition 0 needs no call, and a failed compare check stops before the files listing. A typical PR's listing is one page.
   - **Auth:** the reads reuse the guard's own `pulls/<n>` auth path, `GH_TOKEN` or unauthenticated, plus `CI_FINAL_MERGE_REF_API_URL` for tests. On an unauthenticated job, a rate-limit refusal fails closed as `detail=api_error`, which is today's `stale` outcome.
3. **Tests:** extend `tests/test_ci_final_merge_ref_fresh.py`. The file has no HTTP server.
   - **Stubbing:** it uses the real local origin and the fake `gh` (`FAKE_GH`), which today answers `pulls/7` reads by jq filter. Extend `FAKE_GH` to answer the `compare/...` and `pulls/7/files` endpoints from JSON fixtures selected by the endpoint path argument. For the token-less path, serve the PR read and the two compares as files under the `file://` `CI_FINAL_MERGE_REF_API_URL` directory. The existing token-less test keeps `pulls/7` as a file (the script reads exactly `.../pulls/7`), so that tree cannot also hold `pulls/7/files`. The token-less case therefore expects the files listing to fail closed as `detail=api_error`, and the accept path is tested through `FAKE_GH`, which uses the same checks.
   - **The origin's `HEAD`:** point it at the default branch, so `ls-remote --symref` resolves.
   - **Cases:**
     - the #9602 shape (integration base, sync-only range, disjoint files) is accepted;
     - a PR based on the default branch fails with `detail=default_base`;
     - a sync merge that touches a PR file fails with `detail=file_overlap`;
     - a sync that renames a PR file fails with `detail=file_overlap`;
     - a non-sync, non-default-branch commit in range fails with `detail=foreign_commit`;
     - a merge with the sync subject whose second parent is not a default-branch commit also fails with `detail=foreign_commit`;
     - `status=diverged` fails with `detail=not_ahead`;
     - a truncated first compare (`<tested_base>...<tip>`: `total_commits` over 250, fewer listed commits than `total_commits`, or 300 files) fails with `detail=truncated`;
     - a truncated second compare (`<tested_base>...<default branch>`) fails with `detail=truncated`;
     - a sync merge in an origin whose default branch is not `main` (subject `chore: sync <that branch> into <base>`, branch read via `ls-remote --symref`) is accepted;
     - a non-merge `[ai-merge-resolve]` commit in range that touches none of the PR's files is accepted;
     - a PR whose `changed_files` is over 3000 fails with `detail=too_many_files` and fetches no listing page;
     - a files listing with fewer entries than `changed_files` fails with `detail=truncated`;
     - the token-less tree from Stubbing fails closed with `detail=api_error` at the files listing;
     - an API error fails closed as `stale`;
     - the switch off keeps today's `stale` exit;
     - `tested_base` is the checked-out commit's first parent even when a re-fetch moved the merge ref without a checkout;
     - the fresh path makes no extra calls.
   - **ci.yml wiring:** in each of the ten final-CI "Verify the final-CI merge ref is current" step `env:` blocks in `.github/workflows/ci.yml`, next to `CI_FINAL_MERGE_REF_FRESH_ENABLED`, add `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED: ${{ vars.CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED || 'true' }}`. The script reads the switch only from its environment, so without this the repository variable would not reach it and could not roll the change back. Extend `test_every_final_ci_checkout_is_followed_by_the_check` to assert the new entry on all ten steps. Check `wc -c` against the §27 size guard; the change is about ten lines.
   - **Other contract tests:** keep `tests/test_ci_job_split_contract.py` and `tests/test_ci_shallow_checkout_contract.py` unchanged, since no step or job is added.
4. **Fragments and docs:**
   - `changelog.d/<issue>-final-ci-sync-only-merge-ref.md` (`fixed`);
   - an `agents.d/<issue>-final-ci-sync-only-merge-ref.md` block under "Workflow architecture" for the new behaviour. Do not add a second `CI_FINAL_MERGE_REF` registry bullet or `LOG_PREFIX.name=` line: the prefix is already registered.
   - **Registry entry:** correct the existing `CI_FINAL_MERGE_REF` bullet in place (§25 allows correcting existing text). It is in `agents.d/9602-final-ci-base-sync.md` while that fragment is unfolded, or in the agents file's "Stable log prefixes (contractual)" section once it has been folded.
     - add `stale_accepted` to the outcomes and `sync_only_no_overlap` to the reasons;
     - add the new fields `detail=default_base|not_ahead|truncated|foreign_commit|file_overlap|too_many_files|api_error`, `commits=` and `files=`;
   - a README env var row for `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED`.

## Files & Modules

- **Phase 1:**
  - `.github/workflows/ci.yml`
  - `tests/test_ci_pr_quick.py`
  - `changelog.d/<pr>-ci-quick-inventory-parity.md` `[new]`
- **Phase 2:**
  - `scripts/workflow_failure_heal.py`
  - `scripts/workflow_failure_heal_intake.sh`
  - `scripts/check_failure_triage.sh`
  - `tests/test_workflow_failure_heal.py`
  - `tests/test_check_failure_triage_target_files_guard.py` `[new]`
  - `.github/workflows/ci.yml`
  - `README.md`
  - `agents.d/<pr>-red-main-heal-dedupe.md` `[new]`
  - `changelog.d/<pr>-red-main-heal-dedupe.md` `[new]`
- **Phase 3:**
  - `scripts/review_conflict_resolve.sh`
  - `prompts/conflict-resolver-retry-provenance-prelude.txt` `[new]`
  - `tests/test_review_conflict_resolve_provenance.py` `[new]`
  - `.github/workflows/ci.yml`
  - `README.md`
  - `agents.d/<pr>-resolver-provenance.md` `[new]`
  - `changelog.d/<pr>-resolver-provenance.md` `[new]`
- **Phase 4:**
  - `scripts/review_conflict_prepare.sh`
  - `scripts/review_conflict_resolve.sh`
  - `prompts/conflict-resolver.txt`
  - `tests/test_review_conflict_modify_delete.py` `[new]`
  - `.github/workflows/ci.yml`
  - `README.md`
  - `agents.d/<pr>-modify-delete-take-base.md` `[new]`
  - `changelog.d/<pr>-modify-delete-take-base.md` `[new]`
- **Phase 5:**
  - `scripts/implement_commit_changes.sh`
  - `scripts/review_untrusted_workspace.py`
  - `scripts/heal_isolated_implement.sh`
  - `scripts/workflow_failure_heal.py` (remedy text)
  - `tests/test_files_touched_scope_guard.py`
  - `tests/test_github_ai_plan_grant_write.py` `[new]`
  - `.github/workflows/ci.yml`
  - `README.md`
  - `agents.d/<pr>-github-ai-plan-grant-write.md` `[new]`
  - `changelog.d/<pr>-github-ai-plan-grant-write.md` `[new]`
- **Phase 6:**
  - `scripts/unblock_judge.sh`
  - `tests/test_unblock_judge.py`
  - `README.md`
  - `agents.d/<pr>-unblock-hold-reevaluation.md` `[new]`
  - `changelog.d/<pr>-unblock-hold-reevaluation.md` `[new]`
- **Phase 7:**
  - `scripts/orchestrate_poll_process.sh`
  - `tests/test_stall_recovery_pr_lookup.py`
  - `tests/test_heal_counterfactual_hold_expiry.py` `[new]`
  - `.github/workflows/ci.yml`
  - `README.md`
  - `agents.d/<pr>-counterfactual-hold-expiry.md` `[new]`
  - `changelog.d/<pr>-counterfactual-hold-expiry.md` `[new]`
- **Phase 8a:**
  - `.github/workflows/plan.yml`
  - `scripts/plan_nothing_to_implement_verify.sh` `[new]`
  - `scripts/acceptance_check.py`
  - `tests/test_plan_nothing_to_implement_verify.py` `[new]`
  - `.github/workflows/ci.yml`
  - `README.md`
  - `docs/INVENTORY.md`
  - fragments `[new]`
- **Phase 8b:**
  - `scripts/unblock_judge.sh`
  - `tests/test_unblock_judge.py`
  - fragments `[new]`
- **Phase 9:**
  - `scripts/operator_step_issue.py`
  - `scripts/operator_step_daily_tick.sh` `[new]`
  - `.github/workflows/workflow-log-analysis.yml`
  - `tests/test_operator_step_issue.py`
  - `README.md`
  - `docs/INVENTORY.md`
  - fragments `[new]`

- **Phase 10:**
  - `scripts/ci_final_merge_ref_fresh.sh`
  - `.github/workflows/ci.yml` (one `env:` entry in each of the ten final-CI check steps)
  - `tests/test_ci_final_merge_ref_fresh.py`
  - `README.md`
  - `agents.d/<issue>-final-ci-sync-only-merge-ref.md` `[new]`
  - the existing `CI_FINAL_MERGE_REF` registry bullet (`agents.d/9602-final-ci-base-sync.md`, or the agents file once folded), corrected in place
  - `changelog.d/<issue>-final-ci-sync-only-merge-ref.md` `[new]`

## Data Model / Index Changes

None. No MongoDB collections are touched.

## Tests

- **Unit / contract:** every phase adds or extends pytest files listed above. Git-level behaviour (provenance, modify/delete, patch equivalence) is tested with real temporary repositories, not mocks of git output. `gh` calls are stubbed the way the existing suites do.
- **Wiring:** each new test file is wired into `ci.yml` (`tests/test_ci_wires_every_test_file.py` enforces it).
- **Incident replays:** each phase includes a test built from its incident's shape:
  - the #9452 modify/delete;
  - the `df30f80a` foreign hunk;
  - the #9242 hotfix-vs-squash;
  - the #9212 plan-doc PR;
  - the #9472 nothing-to-implement;
  - the #6211 unticked steps;
  - the #9602 stale merge ref (integration base, sync-only range, disjoint files).
- **Size guard:** `tests/test_workflow_file_size_limit.py` for every phase touching a workflow.

## Risks & Mitigations

- **R2a false positives.** A legitimate resolution may combine both sides into a line that matches neither verbatim, such as merging two lists into one sorted line. Mitigation: compare lines after normalising whitespace, treat a resolved line as provenant when it equals a line of ours or theirs, and allow one retry. Escalation goes through the existing path, so the worst case is today's human wait.
- **R2b dropping a meaningful edit.** Mitigation: a strict containment test (small diff that repeats a cross-file sweep), the deleting PR must be identifiable, and the dropped diff is excerpted in the PR comment so a reviewer sees it.
- **R3 widening the sandbox.** Mitigation: only exact literal paths from the trusted grant that `build_automation_grant` already produces; globs are rejected at read; symlinks are refused; deletion guards are unchanged; tests cover each refusal.
- **R4 closing a heal whose failure is merely quiet.** Mitigation: reuse `heal_stale_decision`'s thresholds (`HEAL_STALE_GREEN_RUNS` green runs after the anchor, at least `HEAL_STALE_MIN_AGE_HOURS`). The close comment says how to reopen.
- **R4 an expired counterfactual hold losing a real fix.** ACCEPTED: the user chose close-not-planned in Q6 A. The escalation at half the limit gives a person a window, and the close comment links the plan for re-filing.
- **R5 tests passing although the work is partly missing.** Mitigation: a close needs at least one test, and the tests come from the plan's own `ACCEPTANCE:` lines first. The wave-completion judge's existing reopen path still applies, as it does for `ai:implement-success-noop` today.
- **R6 ticking a step that is not really done.** Mitigation: tick only on the listed observable conditions. Each tick comment records its reason, and an unrecognised step text is never ticked.
- **R7 testing a base that differs in ways that matter.** A sync merge can change files the PR does not touch but that its tests read, such as a shared helper. Mitigation: only integration-branch PRs qualify, only sync merges (with a default-branch second parent) and resolver commits are accepted, and any overlap with the PR's own files, renames included, fails. The full suite still runs on the integration branch's own push CI and again on the final integration PR, so a base-only break is caught there. The reverse case is also possible: the old base was already broken and a later sync merge fixed it. Then the accepted run fails on the base, not the PR (a false red). The PR goes back through `final-ci-failed`, and the sweep's base sync moves the head as it does today, so this is no worse than the current behaviour. A fix landed by hand on the integration branch (such as `8a1664a5`, see R2a) is not a sync or resolver commit, so the guard refuses that range as `foreign_commit` and no false red arises from it. ACCEPTED, as a narrower risk than a PR that can never run CI.
- **Textual conflicts between phases 3/4 and 6/8b.** ACCEPTED: they touch different functions; the merge-conflict resolver (now with Phase 3/4 safeguards) or a rebase handles them.

## Rollout

- Each phase ships default-on. It takes effect in this repo on merge to `main` (the internal wrappers call `@main`), and in consumers at the next `@stable` release and sync, with no wrapper changes.
- **Rollback:** set the phase's `_ENABLED` repository variable to `false` (no code change), or revert the phase's PR.
- **Watch after each merge, through the stable log prefixes:**
  - Phase 2: `WORKFLOW_HEAL red_main_group`
  - Phase 3: `Resolver provenance:`
  - Phase 4: `Modify/delete take-base:`
  - Phase 5: `IMPLEMENT_GITHUB_AI_GRANT`
  - Phases 6 and 8b: `UNBLOCK_JUDGE ... reason=patch_equivalent|no_recurrence`
  - Phase 7: `HEAL_COUNTERFACTUAL_HOLD_EXPIRY`
  - Phase 8a: `PLAN_NOTHING_TO_IMPLEMENT`
  - Phase 9: `OPERATOR_STEP_TICK`
  - Phase 10: `CI_FINAL_MERGE_REF ... outcome=stale_accepted`, and `outcome=stale ... detail=` for refusals

## References

- Incidents: #9452, #9574, #8307, #9124, #9401, #9394, #9449, #9242, #9339, #9240, #9238, #9244, #8822, #9212, #9478, #9262, #9472, #9583, #9447, #6211, #9577, #9578, #9596, #9584, #9602, #9652 (the freshness guard), #9650 (the project that split Phases 1–9)
- Commits: `df30f80a`, `8a1664a5`, `dd171afc`
- Related plans:
  - `docs/plans/remove-unattended-pipeline-stuck-states-plan.md`
  - `docs/plans/stuck-backlog-prevention-plan.md`
  - `docs/plans/sandbox-aware-unblock-and-implements-marker-plan.md`
- Existing mechanisms extended:
  - `agents.d/8808-heal-stale-auto-close.md` (heal stale close)
  - `agents.d/9484-unblock-placeholder-hold-escalation.md`
  - the folded-fragment keep rule in `scripts/review_conflict_prepare.sh`
