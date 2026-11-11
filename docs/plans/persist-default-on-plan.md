# Turn the persistence switches (P1–P11) on by default for every repository

## Summary

Change the default of the 11 `PERSIST_*_ENABLED` repo variables from `false` to `true` in the reusable workflows and the scripts they call, so every consumer gets the persistent-unattended-workflows features (Refs #8427) on its next `@stable` sync without setting anything. Each switch stays, so a repository opts out by setting the variable to `false`. The same PR fixes two behaviours the audit found would hit consumers that never opted in. (1) P6 stops counting time while an issue waits on a person. (2) P9 waits less when a repository has no CI check runs at all.

## Context

- Project #8427 (PR #8430, squash `0480649`) shipped P1–P11, each behind a default-off repo variable (`docs/plans/persistent-unattended-workflows-plan.md`, D1).
- On 2026-11-09 all 11 were set to `true` in `shubhodeep1/coding-workflows`. Activation log: `docs/deploy-activation/issue-8427.md` (Status LIVE). Poll run 42586255223 succeeded with the switches on.
- Reusable workflows read `vars.*` from the **calling** repository. Today every consumer in `.github/ai/consumer_repos.json` runs with all 11 off.
- The plan's own rollout (`persistent-unattended-workflows-plan.md:216, :723`) flips one phase per PR after a clean week. The operator chose instead to flip all 11 in one PR, after one proving release with them on (decisions below).
- A read-only audit of `origin/main` found every phase fails open when a prerequisite is missing (a plan with no `ACCEPTANCE:` lines, a stale consumer hook, a GitHub API failure), with two exceptions this plan fixes:
  - **P6:** `scripts/persist_budget.py` `decide()` escalates on `max_hours` (48 h from the first journal entry) whatever stage the issue is in. The standalone skip list in `scripts/orchestrate_poll_process.sh` (the `case` before `persist_budget_govern_issue ... standalone`) does not skip `ai:awaiting-approval`, `ai:clarification`, `ai:ready-to-merge` or `ai:review-blocked`. A plan left unapproved for 48 h gets `ai:needs-human`.
  - **P9:** `no_checks` (no check runs on the integration PR head) holds the wave for the full `PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES` (90) at every wave boundary. Repositories with no CI wait 90 minutes per wave.

### Decisions (operator, 2026-11-09)

| ID | Decision |
| --- | --- |
| Q1: A | Ship after one release cycle in coding-workflows passes with all 11 on. This issue is opened only after that release is tagged. |
| Q2: A | One PR for all 11 switches. |
| Q3: A | Every consumer gets default-on. Opt out per repository with `false`. |
| Q4: A | Keep all 11 switches; change only the default. |
| Q5: A | Keep every existing cap default. |
| Q7: A | Fix the P6 human-wait escalation in this PR. |
| Q8: A | Add a shorter wait for the "no check runs at all" case. |
| Q10: A | Keep P5's reopen-on-recurrence behaviour as designed. |
| Q11: A | Correct all docs that say "default `false`"; add one changelog fragment. |

## Goals

- G1. With no repo variable set, all 11 phases run in this repo and in every consumer.
- G2. Setting any switch to `false` (exact string) turns that phase off, byte-for-byte the same as today's default-off behaviour.
- G3. P6 never escalates an issue for time it spent waiting on a person.
- G4. P9 moves on after at most 15 minutes (default) when the integration PR head has no check runs at all.
- G5. Docs, tests and the changelog say "default `true`".

## Non-goals

- Changing any cap default (`PERSIST_MAX_*`, `PERSIST_REPAIR_MAX_TURNS`, `PERSIST_EVIDENCE_*_MAX_*`, P5 window, P8 windows, P10 cap), except adding the new P9 no-checks wait.
- Changing P5's `recurred` behaviour (Q10).
- Removing or renaming any switch (§6).
- Setting variables in any consumer repository.

## Constraints

- §6: no identifier renamed or removed. New identifiers (all verified unused on `origin/main`): `PERSIST_ORCH_WAVE_CHECK_NO_CHECKS_WAIT_MINUTES`, issue-state field `persist_budget_resume_at`, decision reason `human_wait`, `persist_budget.py decide` option `--window-start`.
- §4: the new variable gets a default (`15`, clamped 1..`PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES`).
- §15: no new GitHub API call. P6 reads the issue's labels already passed to `decide --labels`. P9 reuses its existing check-runs read.
- §27: `orchestrate_poll.yml` and `implement.yml` stay under 480,000 bytes; check with `wc -c`.
- §14: the change is in reusable workflows and `scripts/` only (no `workflow-templates/` file references these switches), so it reaches consumers on the next `@stable`. Nothing to add to `.github/ai/consumer_repos.json`.
- Opt-out must keep working: `${{ vars.X || 'true' }}` yields `'false'` when the variable is `false`, because a non-empty string is truthy.

## Approach

1. **Flip every default site** from `false` to `true`:
   - workflow `${{ vars.X || 'false' }}`;
   - workflow and script shell `${X:-false}`;
   - Python `os.environ.get("X", "false")` and `_flag(...)` fallbacks.

   Script fallbacks must match the workflow default so a script run outside the workflow behaves the same.
2. **Fix the one direct comparison.** `issue_pr_status.yml:853` `vars.PERSIST_POSTMERGE_VERIFY_ENABLED == 'true'` becomes `vars.PERSIST_POSTMERGE_VERIFY_ENABLED != 'false'`. Otherwise P5 would stay off by default while its helper fetch (`:185`) turns on.
3. **P6 human-wait (Q7):**
   - `decide()` returns `ignore` with reason `human_wait` when the labels include `ai:awaiting-approval`, `ai:clarification`, `ai:ready-to-merge` or `ai:review-blocked`. Put the set in one module-level constant.
   - New optional `decide --window-start TS`: the `max_hours` window starts at the later of the existing start and `TS`.
   - In `persist_budget_govern_issue`:
     - when a tick sees a human-wait label, it sets `persist_budget_resume_at` to the literal `waiting`;
     - on the first later tick without such a label, when the field is `waiting`, it replaces it with that tick's UTC time;
     - from then on it passes it as `--window-start`.

     Time spent waiting therefore never counts, and a resumed issue gets a fresh 48 h. The state update follows the existing `persist_budget_basis` pattern in the same function.
4. **P9 no-checks wait (Q8):**
   - New env `PERSIST_ORCH_WAVE_CHECK_NO_CHECKS_WAIT_MINUTES` in `orchestrate_poll.yml`, next to `PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES`: `${{ vars.PERSIST_ORCH_WAVE_CHECK_NO_CHECKS_WAIT_MINUTES || '15' }}`.
   - In `scripts/persist_orchestrator.py`'s wave-check decision, the `no_checks` outcome uses this shorter limit, then takes today's skip (`no_checks_timeout`).
   - `pending` and `red` keep the 90-minute limit and their current behaviour.

## Phases & Merge Strategy

One phase, one PR (Q2: A). It is independently mergeable and production-safe at merge:

- every phase already fails open;
- opt-out is one repo variable per phase;
- rollback is a revert of this PR, or setting the variables to `false` in the affected repositories.

## Implementation Steps

1. Flip the workflow `|| 'false'` defaults:
   - `implement.yml:21,23,25,29`
   - `clarify.yml:21,23`
   - `plan.yml:20,22,24,27`
   - `review_autofix.yml:121,123`
   - `unblock_judge.yml:37`
   - `orchestrate.yml:73,75`
   - `orchestrate_poll.yml:36,43,47,52,54,58,61`
   - `issue_pr_status.yml:53,864,866`
2. Flip the workflow shell `${X:-false}` fallbacks:
   - `implement.yml:2303,3215,3222,6188,6192,6263`
   - `clarify.yml:1700`
   - `issue_pr_status.yml:185`
   - `orchestrate.yml:508,864`
   - `plan.yml:2847,2885,2889`
3. Change `issue_pr_status.yml:853` to `!= 'false'`.
4. Flip the script fallbacks:
   - `run_plan_codex.sh:305,339,357,416,630`
   - `implement_step_prepush_test_gate.sh:285,450,737`
   - `review_autofix_step_prepush_test_gate.sh:346,436,733,854,884`
   - `review_conflict_resolve.sh:285,1127`
   - `review_commit_changes.sh:685`
   - `review_apply_fixes.sh:1315,1457`
   - `unblock_judge.sh:2570`
   - `orchestrate_poll_process.sh:20266,20477,20504,20515,20570,20819,21070,21102,21306,21440,24569,26993,29512,29843,32470,33723`
   - `persist_orchestrator.py:266,271,750,1015`
   - `evidence_pack.py:121`
   - `evidence_broker.py:170`
   - `attempt_journal.py:96`
   - `persist_budget.py:115-116`
   - `persist_postmerge_verify.py:129-130`

   Line numbers are from `origin/main` 7014d5a5. Re-grep `PERSIST_[A-Z_]*_ENABLED` for any site added since.
5. Update the "default false" docstrings:
   - `acceptance_check.py:26`
   - `persist_orchestrator.py:4,45,80,125`
   - `evidence_pack.py:22`
   - `evidence_broker.py:34`
   - `attempt_journal.py:19`
   - `persist_postmerge_verify.py:32,38`
   - `persist_budget.py:45`
6. P6 human-wait: `persist_budget.py` (constant, `human_wait` reason, `--window-start`) and `persist_budget_govern_issue` in `orchestrate_poll_process.sh` (`persist_budget_resume_at`).
7. P9 no-checks wait: `orchestrate_poll.yml` env line, `persist_orchestrator.py` wave-check decision, and the wave-check caller in `orchestrate_poll_process.sh`, if it passes the limits.
8. Docs and changelog:
   - correct "default `false`" in the `agents.md` P1–P11 sections (§30.B: direct edit to correct existing text);
   - document `human_wait` and the new P9 variable in the P6 and P9 sections;
   - update `docs/INVENTORY.md`;
   - add `changelog.d/<issue>-persist-default-on.md` (`<!-- changelog: changed -->`), including a "What this means for consumer repos" paragraph that explains opt-out.
9. Run `wc -c .github/workflows/*.yml` (§27).

## Files & Modules

- Workflows: `.github/workflows/{implement,clarify,plan,review_autofix,unblock_judge,orchestrate,orchestrate_poll,issue_pr_status}.yml`
- Scripts:
  - `scripts/{run_plan_codex,implement_step_prepush_test_gate,review_autofix_step_prepush_test_gate,review_conflict_resolve,review_commit_changes,review_apply_fixes,unblock_judge,orchestrate_poll_process}.sh`
  - `scripts/{persist_orchestrator,evidence_pack,evidence_broker,attempt_journal,persist_budget,persist_postmerge_verify,acceptance_check}.py`
- Docs: `agents.md`, `docs/INVENTORY.md`, `changelog.d/<issue>-persist-default-on.md`
- Tests: listed below.

## Data Model / Index Changes

None. No MongoDB collection or contract is touched. The only new state is the issue-state JSON field `persist_budget_resume_at` in the poller's existing per-issue state.

## Tests

- Update the assertions that pin the `'false'` default to `'true'`:
  - `tests/test_acceptance_check.py:331,339-342,359,363,371`
  - `tests/test_implement_prepush_test_gate.py:525,673,676`
  - `tests/test_workflow_untrusted_input_contract.py`: the PERSIST default lines (`:539`–`:1082`)
  - `tests/test_orchestrate_poll_workflow_contract.py:237`–`:308`
  - `tests/test_persist_postmerge_verify.py:645-653`
  - `tests/test_persist_orchestrator.py:370,612,852,1261`
  - `tests/test_orchestrate_poll_process.py:29810,30110,30195,30369,30441,30535,30544,30578,30767,31122`
  - `tests/test_evidence_broker.py:379,488`
  - `tests/test_attempt_journal.py:378`
  - `tests/test_unblock_judge.py:5142`
  - `tests/test_review_conflict_resolve_sandbox_only.py:1562`
- Change the unset-means-off tests to unset-means-on, and add an explicit `false`-means-off case:
  - `tests/test_persist_budget.py:243-247`
  - `tests/test_persist_postmerge_verify.py:618-619`
  - `tests/test_persist_orchestrator.py:104`
  - `tests/test_attempt_journal.py:280-290`
- New:
  - P6 `decide()` returns `ignore human_wait` for each of the four labels;
  - `--window-start` moves the `max_hours` start;
  - `persist_budget_govern_issue` sets `persist_budget_resume_at` on the first tick after the label drops, and an issue waiting 72 h then resumed is not escalated.
  - P9 `no_checks` times out at the new limit (default 15, clamped); `pending` still uses 90.
  - Every flipped workflow default, and `issue_pr_status.yml:853`, still yields off when the variable is exactly `false`.
- Run the full suite. Some tests may assume unset means off; fix those that fail because of this change.

## Risks & Mitigations

| Risk | Mitigation |
| --- | --- |
| A consumer hits behaviour it did not expect (extra comments, P5 reopening a merged issue, P3's extra repair turns) | Changelog fragment with opt-out instructions; one variable per phase turns it off. |
| P5 reopens a merged issue after an unrelated post-merge failure | Only when the plan named a post-merge workflow; capped at `PERSIST_MAX_APPROACHES` (3). Kept as designed (Q10). |
| More AI usage in consumers | Up to 2 extra repair turns per failing gate (P3); one extra planner run per evidence request (P11); one judge call per red/timeout wave check (P9) or revise (P10, cap 2). Caps unchanged. |
| A missed `false` fallback leaves a phase off by default somewhere | Step 4's re-grep, plus a test that greps `.github/workflows` and `scripts` for `PERSIST_[A-Z_]*_ENABLED` with a `false` fallback and fails if any remains. |
| P6 resume tracking misses a label drop between ticks | The field is set on the first tick without a human-wait label, so the worst case is one tick of counted time. |

## Rollout

1. Precondition (Q1: A): the issue is opened only after a `stable` release tagged after 2026-11-09T08:16Z (when all 11 switches were on here) has passed its promote cycle (smoke gate, proving and verifying apply-analysis runs). This holds the flip back from the release that proves the current code.
2. The PR merges to `main` through the normal pipeline.
3. The next daily promote cycle proves `main` with the new defaults (its proving and verifying runs exercise them here) and tags `stable`.
4. Consumers pick it up on their sync (`update_workflows.yml`, daily 04:00 UTC, plus the `@stable` `repository_dispatch`).
5. Rollback: revert the PR, or set the variable(s) to `false` in the affected repository.

## Automation (§18.E)

- **Scripts:** no new script. Existing scripts and workflows are modified only.
- **Scheduler / entry points:**
  - unchanged: `internal-orchestrate-poll.yml` (cron `*/5 * * * *`) → `orchestrate_poll.yml`;
  - issue/PR events → `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `issue_pr_status.yml`, `unblock_judge.yml`;
  - releases go through the existing `promote-main-to-stable.yml` daily cycle (cron `0 0 * * *`).
- **Supervisor:** none needed. The poller is the existing long-running driver.
- **DB work:** none.
- **Removal registry:** no new single-use or long-running script, so no `docs/scripts-pending-removal.md` entry.

## References

- Refs #8427, PR #8430
- `docs/plans/persistent-unattended-workflows-plan.md` (D1, Rollout)
- `docs/deploy-activation/issue-8427.md`
- `scripts/persist_budget.py` `decide()`; `scripts/orchestrate_poll_process.sh` `persist_budget_govern_issue` and the standalone skip `case`
- `scripts/persist_orchestrator.py` wave-check (`no_checks`, `no_checks_timeout`)
