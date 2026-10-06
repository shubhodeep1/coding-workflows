# Unattended pipeline: remove stuck states

## Summary

Remove the stuck states found in the 2026-10-06 pipeline audit. When work stalls, the pipeline must retry with an LLM, change approach, and only then hand off to a person. A person is needed only when an LLM has failed three distinct times. In that case the item stays open, gets one line in a single standing "needs human" issue, and triggers one Telegram alert.

## Automation wiring (§18.E)

| Question | Answer |
|---|---|
| New script / extended script / code-only? | One new script, `scripts/dependency_wait_resume.py` (Phase 8). It is the script `docs/plans/confine-human-steps-to-activation-plan.md` already designs. Every other phase extends existing scripts, workflows and prompts. |
| Scheduler / PR-push entry points | Poller tick: `.github/workflows/orchestrate_poll.yml` (cron) → `scripts/orchestrate_poll_process.sh`. Phases 3, 6, 7, 8 and 10 wire in there. Daily promote cycle: `.github/workflows/promote-main-to-stable.yml` → `scripts/promote_main_cycle.sh` (Phase 4). Review runs: `.github/workflows/review_autofix.yml` → `scripts/review_merge_train.sh` and `scripts/review_single_issue_security_pass.sh` (Phases 1, 5). Release gate: `.github/workflows/test-and-mark-stable.yml` (Phase 4). Sweep cron: `.github/workflows/review_autofix_sweep.yml` (Phase 11b). Label sync: `.github/workflows/sync_ai_labels.yml`, newly called on a schedule from this repo (Phase 11a). |
| New long-running supervisor (§18.C)? | No. Everything rides the existing poller, sweep, promote and review crons. |
| DB work (§18.D)? | None. No MongoDB collections, indexes or contracts are touched (§10 N/A). |
| `docs/scripts-pending-removal.md` (§18.F) | No new entries. `scripts/dependency_wait_resume.py` runs every poller tick permanently, so it is not single-use, long-running or a supervisor. Phase 4 updates the two existing release-reporter entries only if it implements `release-blocker-heal-reports-plan.md` Phase 2, which already specifies that update. |

## Context

On 2026-10-06 the audit covered 112 open items. 15 were stuck and needed a person, 43 were waiting behind a single bottleneck, and 16 were obsolete. Root causes, with evidence:

1. **Merge train serializes everything.** `.ai/.workspace_source_manifest.txt` is gitignored (`.gitignore:27`) but tracked, and `git add -u` (`scripts/implement_commit_changes.sh:315-316`) commits it in every PR. The train's overlap test is a raw path intersection (`_mt_intersect`, `scripts/review_merge_train.sh:186-188`) with no notion of "git can auto-merge this", no head-age limit and no priority lane (`_mt_blockers_for_into`, `:222-236`). 18 PRs on `main` waited behind #6135, which was queued for 32.5 h and then cycled for about 36 h. #6464, the fix for the failing release gate, was queued 18th solely on that file. The manifest itself is fixed by #6508 (PR #6515, adds `MERGE_TRAIN_IGNORE_PATHS`); the rest is not.
2. **Workflow YAML and helper scripts come from different refs.**
   - `plan.yml` runs from `main`. Its "Stage workflow support files" step (`plan.yml:280-300`) copies a hand-kept list of scripts from `.codex-workflow-src` (SCRIPT_REF) into the checkout of the target branch. Every unlisted script comes from the target branch.
   - Heal issues target `stable`. Main's `ai_engine.sh:284-285` passes `--engine` to `stable`'s `codex_stall_guard.sh`, which predates the flag (added in #6179).
   - Every heal plan therefore crashes (`codex_stall_guard.sh: unknown option: --engine`, run 37406961564).
   - The same copy-list pattern is in `clarify.yml:224`, `implement.yml:1027` and `orchestrate_clarify_respond.yml:333`.
   - `review_autofix.yml` already avoids this. It stages the whole support tree with `scripts/stage_workflow_support.sh` into `${SUPPORT_SCRIPTS_DIR}` (`review_autofix.yml:2603-2620`, 137 call sites) and never runs helpers from the target tree.
   - PR #6433 adds the one missing file to the lists, which fixes this instance only.
3. **Stall recovery never escalates a deterministic failure.**
   - The standalone poller zeroes `stall_recovery_count` on every phase change (`orchestrate_poll_process.sh:16186-16190`).
   - A failing plan flips the issue from `ai:planning` back to `ai:clarification` (`plan.yml:~2300-2323`) with no failure signature. The counter therefore never reaches `STALL_JUDGE_TRIGGER_COUNT`.
   - At `MAX_STALL_RECOVERIES_PER_ISSUE` the `skip` action closes the issue as `ai:closed` (`:16990-16996`) instead of diagnosing it.
   - `escalate_human` is downgraded when `ENABLE_STALL_HUMAN_TERMINALIZATION=false` (`orchestrate_lib.py:2221-2224`).
4. **`stable` froze for 3+ days, unnoticed.**
   - 8 of the last 15 `test-and-mark-stable` gates failed, almost all in `e2e-smoke-test`. Phase 4b (`test-and-mark-stable.yml:2273+`) is the most common failure: its 25-minute retry budget runs while the adopted review run is still `queued` (`:2788`, `:2904`).
   - `promote_main_cycle.sh:302-306` skips (`cycle_in_flight`) while any `ai:orchestrator-tracking` + `ai:comprehensive-test-pending` issue is open, even one parked on `ai:harness-broken`. #6031 held the cycle on 2026-10-04 and 2026-10-05.
   - `docs/plans/release-blocker-heal-reports-plan.md` (staleness and skip reporters) is merged as a design but none of it is implemented.
5. **The per-PR security pass does not converge.**
   - `scripts/review_single_issue_security_pass.sh` re-audits `merge-base..head` every cycle (`security-audit.yml:216-224` sets only `SECURITY_AUDIT_DIFF_BASE`/`HEAD`). It has no `SECURITY_AUDIT_DIFF_SINCE` and no `SECURITY_AUDIT_PRIOR_FINDINGS`; the latter is only valid in `findings-json` mode (`security_audit.sh:362-365`).
   - It files one follow-up issue per finding. Each fix becomes new attack surface for the next audit.
   - #6135 got findings on `review_autofix.yml:2551` in 4 of 5 cycles. #6187 and #6209 had new findings on every head.
   - #6377 (merged) bounds the churn but does not make it converge.
6. **Human-only latches.**
   - In flight: PR #6204 (replace-claude-sessions Phase 7) adds an unblock judge for most block labels, with at most 2 rounds per item (`unblock_ledger.py:140`, hardcoded). On exhaustion it closes the item.
   - Gaps that remain after #6204:
     - Re-running validation after a `harness-broken` project's heal fix merges (#6031).
     - The scope guard rejecting a required `changelog.d/` fragment and test fixtures (#4664; `files_touched_scope_guard.py:191-198` auto-allows only lockfiles).
     - Generic "Depends on: #N" waits (#6038).
     - Security-dependency holds that wait forever when the predecessor stalls or closes unmerged (`orchestrate_poll_process.sh:16011-16100`, `security_dependency.py:75-91`).
7. **Clarify blocks on a defect in its own read-only snapshot.** `scripts/clarify_isolated_run.sh:57` keeps only 18 file suffixes and omits `.j2`, `.patch`, `.tmpl`, `.sol` and `.jsonl`. The agent never saw `workflow-templates/validation-harness/**`, wrote `BLOCKED:` (#6194), and `clarify.yml:1152-1270` parked the issue in `ai:blocked` with no retry.
8. **Operator steps for automated work.**
   - `prompts/mode-activation-verify.txt:25` classifies "tag a release" as an `operator` step, so #6211 accumulated "release and sync" steps that the daily promote cycle performs anyway.
   - Nothing ever ticks or closes an operator step (`scripts/operator_step_issue.py` only upserts).
9. **Orphans and zombies.**
   - Retiring a subsystem deletes its labels (`scripts/ai_labels.py`, `retired_labels` at `label_contract.v1.json:267`) but never closes items that carry them. The label sync is never called in this repo, and 10 queue issues were orphaned.
   - 8 Actions runs have sat `queued` since August/September. A normal cancel returns 409, and nothing reaps them (`review_autofix_sweep.yml` only ignores them via `SWEEP_STALE_QUEUED_MINUTES`).
10. **Abandoned projects.** #3965 (contract-list union) and #4139 (security-pass convergence) were closed on 2026-10-06 as not planned, with branches 44 and 39 conflicting files behind `main`. Their unshipped designs are re-planned here, fresh off `main`:
    - `docs/plans/orchestrator-sync-contract-list-union-plan.md` Phase 1
    - `docs/plans/security-pass-convergence-plan.md` Phases A and B (D1 line ownership, D4 rebinding)

## Goals

- G1. Two `ai/issue-*` PRs that change the same file only queue when `git merge-tree` reports a real conflict. A train head older than the cap stops blocking. Heal, release-blocker and security fixes jump the queue.
- G2. No reusable workflow among clarify, plan, implement and orchestrate-clarify-respond runs a pipeline helper from the target-branch checkout. A CI test fails if any of them reintroduces a per-file copy list.
- G3. Two consecutive identical failure signatures in the same issue route to the unblock judge, regardless of phase flips. Stall recovery never closes an issue on exhaustion.
- G4. `stable` older than `RELEASE_BLOCKER_STABLE_STALE_SECS` (default 86400, as designed in `release-blocker-heal-reports-plan.md`) produces one Telegram alert and one heal issue per 24 h throttle window. A tracking issue parked on a human-latch label cannot hold the promote cycle longer than `PROMOTE_CYCLE_HOLD_MAX_HOURS`. Phase 4b's retry budget starts only when the adopted review run starts.
- G5. The per-PR security pass audits only what changed since the last audited head and carries prior findings forward. Each cycle files at most one consolidated follow-up issue. High and critical findings on lines the PR wrote still block.
- G6. Every human-only stop gets `UNBLOCK_MAX_ROUNDS_PER_ITEM` (default 3) distinct LLM rounds. After that, the item stays open, gets one entry in the needs-human digest, and sends one CRITICAL Telegram alert.
- G7. When a heal issue for a `harness-broken` project merges, the project is re-validated automatically. Required `changelog.d/` fragments and `tests/fixtures/` paths never trip the scope guard.
- G8. "Depends on: #N" waits resume automatically for every issue, and escalate after `DEPENDENCY_WAIT_ESCALATE_DAYS`.
- G9. Clarify's snapshot contains every tracked text file. A `BLOCKED:` that cites a path present in git is retried, not parked.
- G10. Activation verify never emits an operator step for work automation performs. Operator steps whose condition is met are ticked automatically, and the issue closes when none remain.
- G11. Retiring a label closes the open items that carry it. Runs queued longer than `ZOMBIE_RUN_MAX_QUEUED_HOURS` are force-cancelled.
- G12. The contract-list union pre-resolver and project-pass line ownership / rebinding ship as designed in their existing plans.

## Non-goals

- Re-implementing PR #6204, #6433, #6464 or #6508/#6515. They are prerequisites (Q7: A). This plan only fills gaps they leave.
- Weakening any security check. Nothing here lets a high or critical finding on PR-owned lines merge unaudited. Line ownership applies only to the orchestrator project pass (Q17: A).
- Changing the merge train's lowest-PR-first policy for PRs that genuinely conflict.
- Phases 1, 2, 3 and 5 of `confine-human-steps-to-activation-plan.md`. Only its Phases 4a/4b are pulled in, as Phase 8 here.
- Making `stable` promotion skip the release gate. The gate stays mandatory; this plan only makes its failures visible and shorter-lived.
- Runner capacity (the queue backlog of ~30 queued runs). This plan records it as a risk only.

## Constraints

- **§4 / §6:** every new env var has a default, and every new identifier was checked for collisions against `origin/main` on 2026-10-06 (none found). Reused identifiers keep their existing meaning:
  - `MERGE_TRAIN_IGNORE_PATHS` (#6515)
  - `ai:plan-failed`, `ai:needs-human`, `ai:operator-step`
  - `DEPENDENCY_WAIT_ESCALATE_DAYS`, `ai:waiting-on-dependency`, `AI_DEPENDENCY_WAIT_V1`, `DEPENDENCY_WAIT_RESUME` (designed in `confine-human-steps-to-activation-plan.md`)
  - `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED`, `SECURITY_AUDIT_LINE_OWNERSHIP`, `SECURITY_PASS_LINE_OWNERSHIP`, `SECURITY_PASS_REBOUND` (designed in their plans)

  No existing identifier is renamed or removed.
- **§14:** reusable workflows and scripts reach consumers through the normal `@stable` sync. Phase 11a adds a home-repo caller only; consumers already receive `workflow-templates/ai-sync-labels.yml`. No change to `.github/ai/consumer_repos.json`.
- **§15:**
  - Each phase states its API budget in its steps.
  - New per-item reads must come from existing per-tick prefetches (`_candidate_details_json`, `ACTIVE_WORKFLOW_ISSUES`) or be one batched REST call per tick.
  - GraphQL is not added (the web proxy rejects it).
- **§19:** no PR body may use auto-close keywords against `ai:orchestrator-tracking` issues.
- **§20:** each phase ships exactly one `changelog.d/<issue>-<slug>.md` fragment.
- **§27:** `review_autofix.yml` is 439,318 bytes and `test-and-mark-stable.yml` 376,514 bytes (limit 480,000). Phases touching them must move any new run body over about 40 lines into `scripts/` and check `wc -c`.
- **Security first (§1):**
  - Phase 2 must keep helper execution read-only with respect to the editor's workspace.
  - Phase 5 must never drop a high or critical finding on PR-owned lines.
  - Phase 7's scope allowance is limited to the two path patterns and never covers `.github/`, `.claude/`, `workflow-templates/` or `scripts/`.

## Approach

Each stuck class gets a mechanism that removes it entirely, not a patch for the instance.

- **Bottlenecks:** the merge train gains real-conflict detection and bounded head age (P1). Release health gains visibility and bounded holds (P4).
- **Loops:** stall recovery gains a failure-signature breaker that survives phase flips (P3). The per-PR security pass converges by delta re-audit (P5).
- **Human latches:** PR #6204's unblock judge is the single escalation funnel. This plan widens what reaches it (P3, P7, P8) and changes its end state from "close" to "park open in a digest" (P6).
- **Mismatched inputs:** support scripts come from one ref (P2), and clarify's snapshot contains every text file (P9).
- **Noise:** automated steps stop being reported as human steps (P10). Retired subsystems and zombie runs are cleaned up (P11).
- **Abandoned designs:** shipped from their existing plans, fresh off `main` (P12, P13).

Alternatives considered:
- A bigger `MERGE_TRAIN_MAX_OLDER_PRS`. Rejected: it does not stop false overlaps.
- Disabling the per-PR security pass on exhaustion. Rejected under §1.
- Keeping #6204's close-on-exhaustion. Rejected by Q8: closing loses work, and the user wants a human only after the LLM fails.

## Decisions (operator answers, 2026-10-06)

| Q | Decision |
|---|---|
| Q5 | A: #3965, #4139, #4142, #4664, #4090 and #4091 closed. The unshipped pieces are folded in here (P12, P13). |
| Q6 | A: one plan with independent phases, implemented via `/implement-plan-ai`. |
| Q7 | A: PRs #6204, #6433 and #6464 are prerequisites outside the plan. The orchestrator starts this plan after they merge. |
| Q8 | A: after the LLM gives up, the item stays open, gets one entry in the existing `ai:operator-step` issue, and sends a Telegram alert. |
| Q9 | A: 3 distinct LLM rounds per item. |
| Q10 | A: run helpers from the support checkout; remove the per-file copy lists. |
| Q11 | A: real-conflict overlap, ignore list, head-age cap, priority lane. The manifest untracking ships separately (#6508). |
| Q12 | A: #6508 filed and in flight as PR #6515. |
| Q13 | A: per-PR pass gets delta re-audit, prior-finding carry-forward and one consolidated follow-up per cycle. High/critical still block. |
| Q14 | A: Phase 4b fix, bounded promote-cycle hold, stale-`stable` alert and heal, release-blocker reporters. |
| Q15 | A+B+C+D+E: all smaller fixes (P7–P11). |
| Q16 | A: each phase behind a new default-on kill switch, shipped through the normal `@stable` sync. |
| Q17 | A: line ownership (D1) only in the orchestrator project pass. |
| Q18 | A: one-time merge-train bypass for #6508's PR, performed by the operator's session. |

## Phases & Merge Strategy

Every phase is one PR to `main` and is independently mergeable. No phase assumes another phase has merged. Each is complete and production-safe on merge, behind a default-on kill switch (Q16: A), and reverts on its own. The only external precondition is Q7. Phases 3, 6, 7 and 8 hand items to the unblock judge from PR #6204. Until #6204 is on `main`, they degrade to applying the same block label, which a human can still clear today. That degradation is safe and is noted per phase.

1. **P1 — Merge train: real conflicts, bounded head, priority lane.**
   - Files: `scripts/review_merge_train.sh`, `tests/test_review_merge_train.py`, `.github/workflows/review_autofix.yml` (env rows only), README "Merge train", `agents.md`, changelog.
   - Done when the tests show:
     - Same-path-but-clean-merging PRs do not queue.
     - Conflicting PRs do.
     - A head older than the cap stops blocking.
     - A priority-labelled PR is never queued behind non-priority PRs.
     - All three switches off reproduce today's behaviour.
   - Rollback: revert, or set `MERGE_TRAIN_CONFLICT_CHECK_ENABLED=false`, `MERGE_TRAIN_HEAD_MAX_AGE_HOURS=0` and `MERGE_TRAIN_PRIORITY_LABELS=''`.
2. **P2a — Single support-script source: clarify, plan, orchestrate-clarify-respond.**
   - Files: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/orchestrate_clarify_respond.yml`, `scripts/ai_engine.sh` (resolve sibling helpers via `${SUPPORT_SCRIPTS_DIR}` when set), `tests/test_support_script_single_source.py` [new], changelog.
   - Done when a heal-issue plan targeting a `stable`-like fixture branch that lacks `--engine` support runs the support tree's guard, and the contract test passes.
   - Rollback: revert, or set `SUPPORT_SCRIPTS_UNIFIED_ENABLED=false`, which restores the copy lists.
3. **P2b — Single support-script source: implement.**
   - Files: `.github/workflows/implement.yml`, `scripts/implement_*.sh` call sites, `tests/test_support_script_single_source.py` (extend), changelog.
   - Done when implement runs every helper from `${SUPPORT_SCRIPTS_DIR}`, the editor workspace is never written by staging, and commit exclusions for consumer repos (`implement_commit_changes.sh:318-341`) still pass their tests.
   - Rollback: same switch.
4. **P3 — Failure-signature circuit breaker for stall recovery.**
   - Files: `scripts/orchestrate_poll_process.sh` (standalone and managed loops), `scripts/orchestrate_lib.py`, `.github/workflows/plan.yml` (failure handler), `.github/workflows/clarify.yml` (failure handler), `tests/test_orchestrate_poll_process.py`, `tests/test_orchestrate_lib.py`, changelog.
   - Done when the tests show:
     - Two identical plan-failure signatures across a planning↔clarification flip apply `ai:plan-failed` with a `AI_FAILURE_FINGERPRINT_V1` marker.
     - The `skip` exhaustion path no longer closes the issue.
     - The switch off restores the current behaviour.
   - Rollback: `STALL_FINGERPRINT_BREAKER_ENABLED=false`.
5. **P4 — Release health.**
   - Files: `.github/workflows/test-and-mark-stable.yml` (Phase 4b timing, body moved into `scripts/e2e_phase4b_retry.sh` [new helper sourced by the step, §27]), `scripts/promote_main_cycle.sh`, `.github/workflows/promote-main-to-stable.yml` (env rows), the scope of `docs/plans/release-blocker-heal-reports-plan.md` Phases 1–3, tests, README, `agents.md`, `docs/scripts-pending-removal.md` (existing entries only), changelog.
   - Done when the tests show:
     - Phase 4b's budget starts at the adopted run's `run_started_at`.
     - A latched tracking issue older than the cap no longer holds the cycle and is reported.
     - Stale `stable` produces one heal report and alert per throttle window (the release-blocker plan's `stable_release_stale` reason).
     - The release-blocker plan's own done conditions hold.
   - This is a large phase. It may ship as up to five PRs (4b timing, cycle hold, release-blocker Phases 1, 2 and 3), each independently mergeable; the release-blocker plan already guarantees this for its own phases.
6. **P5 — Per-PR security pass convergence.**
   - Files: `scripts/review_single_issue_security_pass.sh`, `.github/workflows/security-audit.yml` (pass `SECURITY_AUDIT_DIFF_SINCE` and `SECURITY_AUDIT_PRIOR_FINDINGS` when dispatched with `pr_number`), `scripts/security_audit.sh` (only if the findings-json path needs a PR-scoped writer), `tests/test_review_single_issue_security_pass.py`, `tests/test_security_audit_workflow_contract.py`, README, `agents.md`, changelog.
   - Done when the tests show:
     - Cycle n+1 audits only `last_audited_head..head` plus prior findings.
     - A prior finding not re-reported and not touched is still listed.
     - One consolidated follow-up issue is filed per cycle.
     - A high or critical finding still holds the merge.
     - Switches off restore today's behaviour.
7. **P6 — Escalation policy on top of the unblock judge.**
   - Files: `scripts/unblock_ledger.py`, `scripts/unblock_actions.py`, `scripts/operator_step_issue.py` (new `needs-human` entry kind), `.github/workflows/orchestrate_poll.yml` (env rows), tests, README, `agents.md`, changelog.
   - Done when the tests show:
     - An item gets 3 rounds with distinct verdict/fingerprint.
     - The terminal outcome leaves it open with an `ai:needs-human` label, one digest entry and one CRITICAL alert.
     - Setting `NEEDS_HUMAN_DIGEST_ENABLED=false` restores #6204's close behaviour.
   - Precondition: #6204 merged (Q7). If the files are absent at implementation time, the phase stops and reports instead of recreating them.
8. **P7 — Latch gaps: harness-broken auto-revalidate; scope-guard auto-allow.**
   - Files: `scripts/orchestrate_poll_process.sh`, `scripts/files_touched_scope_guard.py`, `tests/test_files_touched_scope_guard.py`, `tests/test_orchestrate_poll_process.py`, README, `agents.md`, changelog.
   - Done when the tests show:
     - A merged heal issue whose `workflow-failure-heal:source` names a `harness-broken` tracker causes one trusted `/revalidate` on it.
     - `changelog.d/*.md` and `tests/fixtures/**` pass the scope guard.
     - `.github/**`, `.claude/**`, `scripts/**` and `workflow-templates/**` never match the allowance.
9. **P8 — Dependency waits (implements `confine-human-steps-to-activation-plan.md` Phases 4a/4b, plus security holds).**
   - Files: as listed in that plan for 4a/4b, plus `scripts/security_dependency.py` and the hold branch at `orchestrate_poll_process.sh:16058-16100`.
   - Done when that plan's 4a/4b done conditions hold. Additionally:
     - A security hold older than `DEPENDENCY_WAIT_ESCALATE_DAYS` is labelled `ai:needs-human` for the unblock judge.
     - A predecessor closed without `ai:merged` releases the hold with a `/reclarify`. The follow-up must then re-establish whether its finding still applies.
10. **P9 — Clarify snapshot completeness and BLOCKED verification.**
    - Files: `scripts/clarify_isolated_run.sh`, `.github/workflows/clarify.yml` (BLOCKED branch `:1227-1270`, body moved to `scripts/clarify_blocked_verify.sh` [new helper] if it grows), `tests/test_clarify_isolated_run.py`, `tests/test_clarify_blocked_verify.py` [new], changelog.
    - Done when the tests show:
      - A `.j2` file is in the snapshot.
      - Binary files and files over the existing size cap are excluded and counted in `CLARIFY_SNAPSHOT_EXCLUDED`.
      - A `BLOCKED:` citing a path that `git ls-files` lists triggers one automatic re-clarify with the path listing appended, not `ai:blocked`.
11. **P10 — Operator steps only for real human work; auto-resolve.**
    - Files: `prompts/mode-activation-verify.txt` and its `prompts/_templates/` mirror, `scripts/activation_verify.sh`, `scripts/operator_step_issue.py` (new `resolve` subcommand), `.github/workflows/orchestrate_poll.yml` (one step), tests, README, `agents.md`, changelog.
    - Done when the tests show:
      - Activation verify classifies "promote to stable / consumer sync" as `code` with `dormant_until` naming the release.
      - `resolve` ticks an entry whose release condition is met (`compare/<sha>...stable` shows the merge contained).
      - The issue closes when no unticked boxes remain.
12. **P11a — Retired-label drain.**
    - Files: `scripts/ai_labels.py`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/internal-sync-ai-labels.yml` [new caller, weekly cron plus `workflow_dispatch`], `tests/test_ai_labels_retired.py`, changelog.
    - Done when, before deleting a retired label, open issues and PRs carrying it are closed as not planned with one comment each, and the caller runs here.
13. **P11b — Zombie run reaper.**
    - Files: `.github/workflows/review_autofix_sweep.yml` (extend the existing stale-queued listing), `scripts/review_autofix_sweep_reaper.sh` [new helper sourced by the step], tests, changelog.
    - Done when runs queued longer than `ZOMBIE_RUN_MAX_QUEUED_HOURS` get `cancel`, then `force-cancel` on 409, with an audit line per run, and the run list is not fetched a second time.
14. **P12 — Contract-list union pre-resolver.** Implements `docs/plans/orchestrator-sync-contract-list-union-plan.md` Phase 1 exactly as written, fresh off `main`. `scripts/sync_contract_list_union.py` may be ported from the closed branch `orchestrator/project-3965`. Done per that plan.
15. **P13 — Project security pass line ownership and rebinding.** Implements `docs/plans/security-pass-convergence-plan.md` Phase A and Phase B exactly as written (two PRs, any order), fresh off `main`. Project pass only (Q17: A). Done per that plan.

## Implementation Steps

### P1 — Merge train
1. `scripts/review_merge_train.sh`: add `MERGE_TRAIN_CONFLICT_CHECK_ENABLED` (default `true`).
   - When a path overlap exists with a blocker, fetch both heads (`git fetch --depth=200 origin <blocker_head> <pr_head>`).
   - Run `git merge-tree --write-tree --name-only <pr_head> <blocker_head>`.
   - Queue only on exit status 1, a real conflict. Log `MERGE_TRAIN_GATE ... overlap=paths conflict=none` and continue for clean merges.
   - On any git error, fall back to today's path rule (fail-closed to queueing, `conflict=unknown`).
   - API cost: zero new REST calls.
2. Add `MERGE_TRAIN_HEAD_MAX_AGE_HOURS` (default `24`). In `_mt_blockers_for_into` (`:222-236`), a blocker PR is "stale" when all of the following hold:
   - It is not itself queued (no `ai:merge-queued` label, so it is the one under review).
   - It carries `ai:security-pass-failed` or a label in the unblock judge's `BLOCK_LABELS`.
   - It has been under review longer than the cap. Review start is the last time `ai:merge-queued` was removed, read from one `GET issues/<n>/events?per_page=100`; it falls back to `created_at` when the PR was never queued.

   Stale blockers are skipped. Log `MERGE_TRAIN_GATE ... skipped_stale_head=<n>`.
   - The events read happens only for blockers that already carry such a label, so usually 0–1 per gate run. Labels come from the open-PR listing the gate already fetched (§15).
   - Followers still get the existing feature-sweep `update-branch`.
3. Add `MERGE_TRAIN_PRIORITY_LABELS` (default `ai:workflow-heal,ai:security`; release-blocker reports arrive as `ai:workflow-heal` issues through the heal intake). A PR carrying any of these labels on its linked issue is never queued behind PRs without them. Among priority PRs, lowest number first.
   - Read issue labels from the PR body's `Closes #N` link already parsed by `review_collect_pr_metadata.sh`. If not available, use one `GET issues/<n>` per gate run.
4. Tests in `tests/test_review_merge_train.py` for each rule, plus the all-off parity test.
5. README "Merge train" table, env var table, `agents.md`, changelog `changelog.d/<issue>-merge-train-conflict-aware.md`.

### P2a / P2b — Single support-script source
1. In each of `clarify.yml`, `plan.yml` and `orchestrate_clarify_respond.yml` (P2a) and `implement.yml` (P2b), replace the "Stage workflow support files" copy loop with the `review_autofix.yml:2603-2620` pattern:
   - Verify `.codex-workflow-src` HEAD equals `SCRIPT_REF`.
   - Run `.codex-workflow-src/scripts/stage_workflow_support.sh`.
   - Export `SUPPORT_ROOT_DIR` / `SUPPORT_SCRIPTS_DIR` / `SUPPORT_PROMPTS_DIR` to `$GITHUB_ENV`.
2. Rewrite each `scripts/<helper>` invocation in those workflows to `"${SUPPORT_SCRIPTS_DIR}/<helper>"`.
   - Keep the original copy loop behind `SUPPORT_SCRIPTS_UNIFIED_ENABLED != 'true'` (default `true`) so the kill switch restores it.
   - If the workflow grows past §27 headroom, move the conditional into `scripts/stage_support_for_phase.sh` [new helper].
3. `scripts/ai_engine.sh`: resolve `codex_stall_guard.sh`, `claude_engine.py` and other sibling helpers relative to its own directory (`$(dirname "${BASH_SOURCE[0]}")`), not `scripts/`. Do the same for any other support script that calls a sibling by `scripts/` path (grep `scripts/` inside `scripts/*.sh` called from these four workflows).
4. `tests/test_support_script_single_source.py` [new]:
   - Parse each of the four workflows and fail on any `for f in ... ; do ... install ... scripts/` copy list or bare `bash scripts/<support helper>` call outside the kill-switch branch.
   - Execute a fixture where the target tree's `codex_stall_guard.sh` rejects `--engine` and assert the support copy is used.
5. Consumer impact (§14): consumers already set `SCRIPT_REF=stable` for both sides. The change is behaviour-neutral there and reaches them on the next `@stable`.
6. Changelog per sub-phase.

### P3 — Failure-signature breaker
1. `plan.yml` and `clarify.yml` failure handlers (`plan.yml:~2300-2341`):
   - Compute `fingerprint = sha256(phase + failed_step_name + first ::error:: line normalized)[:12]`.
   - Append `<!-- AI_FAILURE_FINGERPRINT_V1 phase=<p> fp=<fp> run=<id> -->` to the existing failure comment (no new comment).
2. `orchestrate_poll_process.sh` standalone loop (`:16180-16195`) and managed loop:
   - Track `failure_fingerprints` (last 5) in the existing `AI_STANDALONE_STALL_STATE_V1` state. They are not reset on phase change.
   - When the same fingerprint appears `STALL_FINGERPRINT_REPEAT_MAX` (default `2`) times consecutively, apply the phase's existing failure label (`ai:plan-failed` for plan, `ai:clarify-failed` for clarify, `ai:needs-human` for any other phase) and stop re-triggering. All three are in the unblock judge's `BLOCK_LABELS` (`unblock_ledger.py:88-108` on #6204).
   - Read the fingerprints from the comments already in `_candidate_details_json`. No new API calls.
3. Replace the `skip` exhaustion action (`:16990-16996`) when `STALL_FINGERPRINT_BREAKER_ENABLED=true`. It applies `ai:needs-human` with the recovery history in the comment instead of closing, and `tg_notify_issue` stays.
4. Tests:
   - Planning↔clarification flapping with the same fingerprint escalates on the 2nd repeat.
   - Different fingerprints keep retrying.
   - Exhaustion leaves the issue open.
5. README stall section, `agents.md`, changelog.

### P4 — Release health
1. Phase 4b (`test-and-mark-stable.yml:2273+`):
   - Move the retry body into `scripts/e2e_phase4b_retry.sh`.
   - Start `EDITOR_RETRY_BUDGET_MINUTES` when the adopted or dispatched run reports `run_started_at`, not at adoption.
   - Add `E2E_RETRY_QUEUED_GRACE_MINUTES` (default `30`), a separate cap for queued time that fails with a distinct reason `retry_queued_timeout`.
   - Reuse the run GET the poll loop already issues.
2. `scripts/promote_main_cycle.sh:302-306`: add `PROMOTE_CYCLE_HOLD_MAX_HOURS` (default `24`).
   - A held tracking issue counts as holding only if it carries no human-latch label (`ai:harness-broken`, `ai:validation-failed`, `ai:security-pass-failed`, `ai:blocked`, `ai:needs-human`) or its latch is younger than the cap.
   - An over-cap held issue logs `PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=<n> reason=<label>`, sends a WARNING, and the cycle proceeds.
   - Use the labels already in the issue listing; the latch age needs one `GET issues/<n>/events` per held issue (0–1 issues in practice).
3. Staleness is not re-designed here. The `stable_release_stale` reporter (`RELEASE_BLOCKER_STABLE_STALE_SECS`, in `auto_release_stable.sh`) and the `cycle_stale` reporter (`RELEASE_BLOCKER_CYCLE_STALE_SECS`, in `promote_main_cycle.sh`) from `release-blocker-heal-reports-plan.md` Phase 2 cover it, so do not add a competing setting (§5).
4. Implement `release-blocker-heal-reports-plan.md` Phases 1–3 as written. Those phases already carry their own files, done conditions and rollback.
5. Tests, README, `agents.md`, changelog per PR.

### P5 — Per-PR security pass convergence
1. `review_single_issue_security_pass.sh` `report` mode: persist `last_audited_head` and the audited findings JSON digest in the existing status marker. Add fields to `ai:single-issue-security-pass:v1`; old readers ignore unknown fields.
2. When dispatching cycle n+1 (`gate`, `SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED`, default `true`), pass `audit_diff_since=<last_audited_head>` and `prior_findings=<artifact or comment ref>` to `security-audit.yml`.
3. `security-audit.yml`, when `pr_number` is set:
   - Run the audit in `findings-json` mode with `SECURITY_AUDIT_DIFF_SINCE` and `SECURITY_AUDIT_PRIOR_FINDINGS` (`security_audit.sh:243-365` already support both).
   - Then file issues from the JSON in a separate step.
4. `SINGLE_ISSUE_SECURITY_PASS_CONSOLIDATE_ENABLED` (default `true`):
   - File one follow-up issue per cycle listing all findings, with the same `Depends on`/target-branch metadata as today. Do not file one per finding.
   - Severity gating is unchanged. High, critical and unrated findings block.
5. Tests:
   - A delta range is passed on cycle ≥2.
   - Carried findings persist.
   - One issue per cycle.
   - A high/critical finding holds the merge.
   - Switches off produce byte-identical dispatch inputs to today.
6. README security pass section, `agents.md`, changelog.

### P6 — Escalation policy
1. `scripts/unblock_ledger.py`: replace the constant `MAX_ROUNDS_PER_ITEM = 2` with `UNBLOCK_MAX_ROUNDS_PER_ITEM` env (default `3`, min 1, max 5). Keep the existing "never repeat a verdict for the same stop + fingerprint" rule so each round is distinct.
2. `scripts/unblock_actions.py` terminal `close` path, when `NEEDS_HUMAN_DIGEST_ENABLED=true` (default):
   - Do not close.
   - Apply `ai:needs-human` and record `parked=true` with the parking time in the item's existing ledger entry. `ai:needs-human` is itself in `BLOCK_LABELS`, so the scan must skip items whose ledger says `parked`.
   - A parked item becomes eligible again only on a new trigger: a comment from a trusted human, a label change by a human, or a new head SHA. That trigger resets the item's round count to 0.
   - Call `scripts/operator_step_issue.py upsert` with key `needs-human-<kind>-<n>` and a one-paragraph summary of the rounds tried.
   - Send one CRITICAL Telegram.
   - Security items already stay open; they get the digest entry too.
3. `operator_step_issue.py`: add entry kind `needs-human`, rendered under a `## Needs human` heading in the same standing issue, plus a `remove` subcommand. The poller calls `remove` when the item closes or loses `ai:needs-human`, using the existing per-tick candidate listing.
4. Tests, README, `agents.md`, changelog.

### P7 — Latch gaps
1. Harness auto-revalidate (`HARNESS_HEAL_AUTO_REVALIDATE_ENABLED`, default `true`):
   - In the poller's tracking-issue loop, for a tracker carrying `ai:harness-broken`, look for its heal issue via the `workflow-failure-heal:outcome` comment already in the tracker's comments.
   - If that heal issue is closed with `ai:merged`, post one trusted `/revalidate` with marker `AI_HARNESS_HEAL_REVALIDATE_V1 heal=<n>`, at most once per heal issue.
   - Cost: 1 `GET issues/<heal>` per harness-broken tracker per tick.
2. Scope guard (`SCOPE_GUARD_AUTO_ALLOW_PATHS`, default `changelog.d/*.md,tests/fixtures/**`): in `files_touched_scope_guard.py` `path_in_scope` (`:191-198`), auto-allow paths matching these globs, alongside lockfiles.
   - Hard-deny prefixes `.github/`, `.claude/`, `scripts/` and `workflow-templates/` even if a configured glob would match them.
   - Log `SCOPE_GUARD_AUTO_ALLOWED path=<p>`.
3. Tests, README, `agents.md`, changelog.

### P8 — Dependency waits
1. Implement `confine-human-steps-to-activation-plan.md` Phase 4a and 4b steps verbatim (identifiers as listed in that plan's §6 inventory).
2. Extend the security-dependency hold branch (`orchestrate_poll_process.sh:16058-16100`):
   - Record `held_since` in the issue's existing stall state.
   - At `DEPENDENCY_WAIT_ESCALATE_DAYS` (that plan's default), apply `ai:needs-human` with the dependency chain in the comment.
   - When the verdict reason is `dependency closed without ai:merged`, post a trusted `/reclarify` with marker `AI_SECURITY_DEPENDENCY_RELEASED_V1 reason=predecessor_unmerged` so clarify re-checks whether the finding still applies.
   - Cost: none beyond the existing verdict read.
3. Tests, README, `agents.md`, changelog. Add a note to `confine-human-steps-to-activation-plan.md` that its Phases 4a/4b were delivered by this plan.

### P9 — Clarify snapshot and BLOCKED verification
1. `scripts/clarify_isolated_run.sh:46-100` (`CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED`, default `true`):
   - Include every `git ls-files` path whose first 8 KiB has no NUL byte and whose size is within the existing per-file cap. The suffix list remains the fallback when the switch is off.
   - Log `CLARIFY_SNAPSHOT_EXCLUDED count=<n> sample=<first 5>` and pass the excluded list into the prompt.
   - Keep the existing exclusions of secrets and sensitive paths unchanged.
2. `clarify.yml` BLOCKED branch (`CLARIFY_BLOCKED_VERIFY_ENABLED`, default `true`), before applying `ai:blocked`:
   - Extract repo-relative paths from the BLOCKED reason.
   - If any path exists in `git ls-files` at the checked-out ref, re-run clarify once in the same job with the path listing appended, marker `AI_CLARIFY_BLOCKED_RETRY_V1`, max 1.
   - Otherwise keep today's behaviour.
3. Tests and changelog.

### P10 — Operator steps
1. Prompt: in `prompts/mode-activation-verify.txt:25` and its template mirror, state that promotion to `stable` and consumer wrapper sync are automated (`promote-main-to-stable.yml`, `update_workflows.yml`) and must be emitted as `code` with `dormant_until: "@stable contains <sha>"`, never as `operator`. Secrets, repo settings and external accounts remain `operator`.
2. `operator_step_issue.py resolve` (`OPERATOR_STEP_AUTO_RESOLVE_ENABLED`, default `true`):
   - For each unticked entry `pr-<n>` whose recorded condition is a release condition, check `GET compare/<merge_sha>...stable`; `status` `behind` or `identical` means contained.
   - Tick it, and close the issue when no unticked boxes remain.
   - Called once per poller tick only when an open `ai:operator-step` issue exists. Cost: 1 compare per unticked release entry, capped at 10 per tick.
3. Tests, README, `agents.md`, changelog.

### P11a — Retired-label drain
1. `scripts/ai_labels.py` (`:672-740` retire path, `RETIRED_LABEL_DRAIN_ENABLED`, default `true`), before deleting a retired label:
   - List open issues and PRs with it (`GET issues?labels=<l>&state=open`, paged).
   - Comment once on each, close it as `not_planned`, then delete the label.
2. `.github/workflows/internal-sync-ai-labels.yml` [new]: weekly cron plus `workflow_dispatch`, calling `sync_ai_labels.yml` for this repo.
3. Tests and changelog.

### P11b — Zombie reaper
1. `review_autofix_sweep.yml`'s existing stale-queued listing (`~:43-60`) is extended via `scripts/review_autofix_sweep_reaper.sh` (`ZOMBIE_RUN_REAPER_ENABLED`, default `true`; `ZOMBIE_RUN_MAX_QUEUED_HOURS`, default `24`):
   - For each run queued longer than the cap: `POST actions/runs/<id>/cancel`, then `POST .../force-cancel` on 409.
   - Log `ZOMBIE_RUN_REAPED run=<id> workflow=<path> age_h=<n> result=<cancel|force_cancel|failed>`.
   - Cap 10 per sweep, and reuse the listing (§15).
2. Tests and changelog.

### P12 / P13
Follow the referenced plans' Implementation Steps verbatim. Each PR must include its `changelog.d/` fragment and test fixtures in its `files_touched` block, so the scope guard never rejects it (the #4664 failure mode).

## Files & Modules

- `scripts/review_merge_train.sh`, `tests/test_review_merge_train.py` (P1)
- `.github/workflows/clarify.yml`, `plan.yml`, `orchestrate_clarify_respond.yml`, `implement.yml`; `scripts/ai_engine.sh`; `scripts/stage_support_for_phase.sh` [new, only if needed for §27]; `tests/test_support_script_single_source.py` [new] (P2a/P2b)
- `scripts/orchestrate_poll_process.sh`, `scripts/orchestrate_lib.py`, `tests/test_orchestrate_poll_process.py`, `tests/test_orchestrate_lib.py` (P3, P7, P8)
- `.github/workflows/test-and-mark-stable.yml`, `scripts/e2e_phase4b_retry.sh` [new], `scripts/promote_main_cycle.sh`, `.github/workflows/promote-main-to-stable.yml`, plus the files listed in `release-blocker-heal-reports-plan.md` (P4)
- `scripts/review_single_issue_security_pass.sh`, `.github/workflows/security-audit.yml`, `scripts/security_audit.sh`, related tests (P5)
- `scripts/unblock_ledger.py`, `scripts/unblock_actions.py`, `scripts/operator_step_issue.py` (P6, P10)
- `scripts/files_touched_scope_guard.py`, `tests/test_files_touched_scope_guard.py` (P7)
- `scripts/security_dependency.py`, `scripts/dependency_wait_resume.py` [new, per the confine plan], plus that plan's 4a/4b files (P8)
- `scripts/clarify_isolated_run.sh`, `scripts/clarify_blocked_verify.sh` [new, only if needed], tests (P9)
- `prompts/mode-activation-verify.txt`, `prompts/_templates/mode-activation-verify.txt`, `scripts/activation_verify.sh` (P10)
- `scripts/ai_labels.py`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/internal-sync-ai-labels.yml` [new] (P11a)
- `.github/workflows/review_autofix_sweep.yml`, `scripts/review_autofix_sweep_reaper.sh` [new] (P11b)
- Files per `orchestrator-sync-contract-list-union-plan.md` (P12) and `security-pass-convergence-plan.md` (P13)
- `README.md`, `agents.md`, `changelog.d/*.md` (every phase)

## Data Model / Index Changes

None (§10 N/A).

## Tests

- Unit and contract tests per phase as listed. Each phase includes a "switch off reproduces today's behaviour" parity test.
- CI runs each new test file through the existing `ci.yml` pytest steps. Add a step only if the file is not covered by an existing glob.
- End to end:
  - The release gate (`test-and-mark-stable.yml`) exercises P2a (heal plans), P4 (Phase 4b) and P1 (smoke PRs bypass the train via `IS_SMOKE_TEST`).
  - The P4 staleness alert is verified by a mocked-`gh` harness test, not a real stale tag.
- Success metrics, re-audited about 7 days after the last phase merges, with the same audit method as 2026-10-06:
  - No item stuck without a human-needed entry in the digest.
  - No `ai:merge-queued` PR older than `MERGE_TRAIN_HEAD_MAX_AGE_HOURS` behind a non-conflicting head.
  - `stable` never stale beyond `RELEASE_BLOCKER_STABLE_STALE_SECS` without an alert and heal issue.
  - No standalone issue with more than 2 identical failure fingerprints in its comments.

## Risks & Mitigations

- **`git merge-tree` false "clean" when a semantic conflict exists.** ACCEPTED: the train never guarded semantics, only textual conflicts. Review still runs on the updated branch after `update-branch`.
- **P2 changes how every reusable workflow finds its helpers.** Mitigations:
  - The kill switch restores the copy lists.
  - P2a (read-only phases) ships before P2b (implement).
  - The contract test prevents regressions.
- **P2b: an editor run edits `scripts/` in this repo while helpers run from the support tree.** Intended: helpers come from the verified SCRIPT_REF and the editor's edits land in the workspace only, matching `review_autofix.yml` today.
- **P3 fingerprint collisions merge distinct failures.** Normalization keeps the first error line, so the risk is low. An escalation then goes to the unblock judge, which reads the full logs.
- **P4 releasing a hold while a project is genuinely mid-proving.** Only human-latched holders older than the cap are released, and every release is logged and alerted.
- **P5 delta re-audit misses an interaction between old and new code.** Mitigations:
  - Prior findings are carried forward.
  - The final exhausted-head audit stays a full `merge-base..head` audit (`SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS` path unchanged).
  - High/critical findings still block.
- **P6 parks items open forever.** The digest is a single standing issue with one line per item and a CRITICAL alert at entry. ACCEPTED: the operator chose open-and-digest over close (Q8: A).
- **P7 scope auto-allow becomes a bypass vector.** The allowance is limited to two globs with a hard-deny prefix list. Fixtures are data, not executed by pipeline code paths with credentials.
- **P8 duplicates the confine plan if someone implements it separately.** P8 adds a "delivered by" note to that plan.
- **P11b force-cancel on a run that is merely slow to start.** The 24 h cap is far above any legitimate queue time observed (max ~2 h).
- **P13 / P12 port from abandoned branches.** The plans are re-implemented fresh off `main`. The old branches serve as reference only.
- **Runner capacity (~30 queued runs at peak) lengthens every phase's latency.** ACCEPTED: out of scope. P4's alerting surfaces it if it blocks releases.
- **Prerequisite PRs #6204, #6433 or #6464 do not merge.** ACCEPTED — pending discovery: P6 stops and reports if #6204's files are absent. P2a supersedes #6433's instance fix. P4's Phase 4b work is independent of #6464.

## Rollout

- Each phase ships default-on behind its kill switch (Q16: A) and reaches consumer repos on the next `@stable` release through `update_workflows.yml`.
- New repo variables, all default-on or defaulted:
  - `MERGE_TRAIN_CONFLICT_CHECK_ENABLED`, `MERGE_TRAIN_HEAD_MAX_AGE_HOURS`, `MERGE_TRAIN_PRIORITY_LABELS`
  - `SUPPORT_SCRIPTS_UNIFIED_ENABLED`
  - `STALL_FINGERPRINT_BREAKER_ENABLED`, `STALL_FINGERPRINT_REPEAT_MAX`
  - `E2E_RETRY_QUEUED_GRACE_MINUTES`, `PROMOTE_CYCLE_HOLD_MAX_HOURS`
  - `SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED`, `SINGLE_ISSUE_SECURITY_PASS_CONSOLIDATE_ENABLED`
  - `UNBLOCK_MAX_ROUNDS_PER_ITEM`, `NEEDS_HUMAN_DIGEST_ENABLED`
  - `HARNESS_HEAL_AUTO_REVALIDATE_ENABLED`, `SCOPE_GUARD_AUTO_ALLOW_PATHS`
  - `CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED`, `CLARIFY_BLOCKED_VERIFY_ENABLED`
  - `OPERATOR_STEP_AUTO_RESOLVE_ENABLED`
  - `RETIRED_LABEL_DRAIN_ENABLED`, `ZOMBIE_RUN_REAPER_ENABLED`, `ZOMBIE_RUN_MAX_QUEUED_HOURS`
- Plus those named by the referenced plans.
- Suggested order for throughput (not required): P1 → P2a → P3 → P4 → P5 → P6 → the rest. Any order is safe.
- Rollback per phase: flip its switch, or revert its PR.

## References

- Audit and actions, 2026-10-06:
  - Closed obsolete items: #6163–#6171, #6180, #4487, #5983.
  - Closed abandoned projects: #3965, #4139, #4142, #4664, #4090, #4091.
  - Filed #6508, the manifest untracking (PR #6515).
- Prerequisite PRs: #6204 (unblock judge), #6433 (staging instance fix, phase-failure heal reports), #6464 (Phase 4 bait coverage).
- Related merged work: #6377 (autofix cap, security exhaustion judge), #6201 (single-issue security pass), #6176 (session automation retired).
- Plans: `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`, `docs/plans/confine-human-steps-to-activation-plan.md`, `docs/plans/release-blocker-heal-reports-plan.md`, `docs/plans/security-pass-convergence-plan.md`, `docs/plans/orchestrator-sync-contract-list-union-plan.md`.
- Evidence runs: 37406961564 (heal plan crash), 37395055737 / 37395952357 (failed promote cycle and gate), 37194789519 (clarify for #6194).
