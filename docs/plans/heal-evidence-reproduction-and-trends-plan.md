# Give the workflow-failure heal evidence, reproduction and trend awareness

## Summary

From 2026-10-27, release gates failed six times in a row, while the workflow-failure heal filed partial fixes or held its plans for a human. An interactive session found and fixed the cause within hours (#8382). It used evidence and checks the heal pipeline cannot reach: child-run step timelines, `main` vs `stable` source, run-time trends, and a re-run of the failing component in its real sandbox.

This plan gives the heal pipeline the same capabilities in seven independently mergeable phases. Each phase sits behind its own default-off repo variable.

## Context

### The incident (2026-10-27 to 2026-10-29)

- **The regression.** #8192 (merged 2026-10-27) wired the pre-push affected-test gate into implement and review. Run times then roughly doubled:

  | Run type | Median, 10-18 to 10-27 | Median after |
  | --- | --- | --- |
  | `internal-implement.yml` success | 20 to 29 min | 52 to 53 min (10-28, 10-29) |
  | `internal-review.yml` | 16 to 25 min | 81 min (10-29) |

  The release gate's smoke test allows 30 minutes without visible progress, so release runs 40608957733, 40831286935, 40876853022 and 40932430968 failed `Phase 3b: Wait for PR creation (implement phase)`.
- **The causes**, all found in an interactive session:
  1. A slow test whose baseline re-run hit the gate deadline was blamed on the change. That triggered a repair turn and a second 15-minute gate run. Fixed in #8382.
  2. The gate container's `/tmp` is noexec, and none of its three images installs `jq`. Shell-script tests fail or hang there on both trees, which also drives review loops such as PR #8371. A fix is in progress.
  3. The release gate watches only implement runs created after its own `/approved`, but the plan's auto-approval starts the working run earlier. This is #8391, building on #8376's port of #8243 and #8259.
- **What the heal pipeline did instead:**
  - #8287, #8327, #8347, #8376 and #8391 each fixed part of the problem or held for a human.
  - #8376 and #8391 failed on the same step with the same error but got different fingerprints, so they were filed as two heals.
  - Several plans ended `HEAL_COUNTERFACTUAL: UNVERIFIED`. One said "`main`'s source (#8243/#8259) is unreadable in the planning workspace".

### What the heal pipeline gets today

All of this is from `main` at e8afd87f.

- **Intake** (`scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`, `.github/workflows/workflow-failure-heal-intake.yml`):
  - **Logs:** at most `WORKFLOW_HEAL_MAX_RUNS=3` runs and `WORKFLOW_HEAL_MAX_FAILED_JOBS=3` jobs. Each job log is filtered by `filter_log` (last 400 lines plus error lines) and capped at 60,000 bytes.
  - **Linked runs** (#8248): at most 4 same-repo runs whose URL appears in the logs. They come with a step timeline (`render_linked_run_timeline`, line 837), but are evidence only and never feed scope. Their header shows no `head_sha` or `head_branch`.
  - **The failed run's own steps** are used only to name the failing step. No timeline of them is rendered.
  - **History:** none of previous runs, durations or trends. The checkout is `fetch-depth: 1`.
  - **Diagnosis:** the `WORKFLOW_HEAL` LLM role runs in `codex_isolated_exec.sh run --mode read-only` with worktrees at the failing SHA and the branch tip.
- **Fingerprint:** `fingerprint()` (`workflow_failure_heal.py:3163`) hashes the workflow name, the failing step and `error_signature()`, which is up to 5 normalised error lines. A different extra error line gives a different heal. `root=` is the lineage's first fingerprint, not a cause.
- **Target branch:** `WORKFLOW_HEAL_TARGET_BRANCH`, default `stable`, or the failed run's `head_branch` (`promote-candidate` maps to the default branch). Nothing records which commit or branch a child run's code came from.
- **Scope:**
  - The `ai:workflow-heal-scope:v1` marker holds at most 20 paths plus `tests/**` and `changelog.d/*.md`. It is frozen before any untrusted evidence and immutable once filed.
  - The plan gate `heal_scope_plan_out_of_scope` holds anything outside it.
  - `docs/plans/heal-scope-linked-runs-script-closure-plan.md` (#8290) proposes widening it but is not implemented.
- **Clarify and plan:**
  - Both run `scripts/clarify_isolated_run.sh`: `--network none`, read-only source, no `.git`, only the single checked-out ref, and Claude tools `Read,Grep,Glob` (+ WebSearch).
  - The only run evidence is `scripts/workflow_failure_heal_evidence.sh`: at most 3 runs, 3 jobs, 6,000 bytes per job and 24,000 bytes in total, with no timelines.
  - The clarify-respond worker gets no heal evidence at all.
  - `scripts/clarify_data_provision_guard.py` swaps any "provide a log" answer for a "proceed without data" fallback.
- **Counterfactual gate:**
  - `scripts/partial_delivery.py` `parse_heal_counterfactual` (line 294), applied in `plan.yml:3001-3064`.
  - Only `PASS`, or `PARTIAL` with every job deferred, auto-approves. `UNVERIFIED` and `FAIL` hold for a human. No check runs anything.
- **Sandboxes:**
  - `scripts/prepush_test_gate_run.sh` already runs tests on a PR tree and on the merge-base in a credential-free container, and `classify_baseline` separates caused from inherited failures.
  - The heal route runs the gate in the clarify image with no `--venv` (`heal_isolated_implement.sh:292`), so pytest files come out `unverified pytest_unavailable`.
- **After merge:** nothing checks that a merged heal fixed the failure. A recurrence is noticed only if the identical fingerprint repeats (lineage `gen+1`). `docs/plans/heal-issues-self-resolve-plan.md` (#8200, not implemented) closes heals whose *source* resolved. It does not verify the next run.

### Constraints this plan works within (CLAUDE.md)

- **§6:** existing identifiers stay as they are. These include env vars, the `WORKFLOW_HEAL` and `PREPUSH_TEST_GATE` log keys, the `HEAL_COUNTERFACTUAL` values, and the marker formats. New ones are added alongside.
- **§14:** `workflow-templates/.claude/hooks/prepush_affected_tests.py` ships to consumer repos, so changes to it are additive only.
- **§15:** every new GitHub API call is listed per phase with a budget and fail-open behaviour.
- **§18:** no manual scripts. Everything runs from the existing intake, recovery-scan, plan and implement entry points.
- **§20 and §30:** one `changelog.d/` and one `agents.d/` fragment per phase.
- **§27:** `.github/workflows/implement.yml` is 425,273 bytes and `plan.yml` 203,288 bytes. New logic goes into `scripts/`; no phase may add more than about 2 KB to `implement.yml`.
- **Security:** heal inputs are untrusted (logs, comments, consumer code). New evidence stays labelled untrusted in prompts. Reproduction runs only in the existing credential-free, `--network none`, read-only-root container pattern. No new token reaches any model sandbox.

## Goals

- **P1:** every heal issue body carries an `## Evidence pack` built from GitHub data. Clarify, plan and clarify-respond all see it, so the evidence gaps named in tonight's `UNVERIFIED` lines are covered:
  - a step timeline of the failed run's own jobs;
  - child and linked runs with their `head_sha` and `head_branch`;
  - runs started from issues the failed job names;
  - commits on `main` that are not on `stable` and touch the scope paths.
- **P2:** a heal whose failing tests can be run gets them run in the gate sandbox at the failing SHA and at the last green SHA during intake, and again on the heal PR's tree before the PR opens. A fix that turns them green can auto-approve without a human.
- **P3:** a timeout or stall failure carries a run-time trend table and the commits merged since the failing workflow last passed. A plan that only raises a timeout or budget while the trend shows a regression is held.
- **P4:** files changed by the suspected regressing commits can enter the heal scope, capped, alongside #8290's scope widening.
- **P5:** a second failure with the same workflow, failing step and error class within the grouping window becomes an occurrence on the open heal instead of a new issue.
- **P6:** when the failing code ran from another branch than the failed run, the heal targets that branch, and a `stable` fix that `main` lacks gets a port issue.
- **P7:** within 24 hours of a heal PR merging, the next completed run of the same workflow is checked. A recurrence at the same step reopens the heal with the new run, and a pass is logged.
- Every phase is behind its own repo variable, which defaults to off. With it off, behaviour is byte-identical to today.

## Non-goals

- Raising the pipelines' trust in model output: provenance, scope-marker immutability and the credential-free sandboxes stay as they are.
- The scope widening that #8290 already plans (linked-run workflows, script closure, plan-hold re-scope). P4 adds a separate input to it and does not duplicate it.
- Closing heals whose source resolved (#8200's plan).
- Applying these capabilities to standalone and orchestrator issues. That is being discussed separately.
- Changing the gate's test selection (for example making script-style files honour function names). That's separate work.

## Approach

**Evidence first, then experiments, then judgement.**
- The intake already has a credential-bearing step that reads the GitHub API and freezes scope before any untrusted evidence. P1 and P3 extend that step with batched reads and write the results into the issue body, which every later stage reads.
- No model sandbox gets a token or network. The models get better input instead of more privileges.
- P2 reuses the gate container and `classify_baseline` instead of building a new runner. "Did the fix work?" becomes an experiment with a recorded result.

**Alternatives considered:**
- **Give the planner a read-only GitHub token or MCP server.** Rejected: it hands credentials to a model running on untrusted input, and §23 keeps tokens out of model sandboxes. Prefetching at intake costs a little more API budget but keeps the boundary.
- **Raise the evidence byte caps.** Rejected as the main lever: the missing evidence was never in the logs at all, for example the working implement run and the commits on `main`.
- **Replace the counterfactual gate with reproduction.** Rejected: many failures, such as release stalls, have no runnable unit. P2 adds a reproduction path next to the reasoning path.

**Decisions taken as the recommended defaults.** The reviewer can override any of them on this PR.

- **D1: every phase is default-off.** Rollout turns phases on one at a time, and a follow-up flips the defaults once each has run cleanly for a week.
- **D2: a new counterfactual value.** `HEAL_COUNTERFACTUAL: REPRODUCIBLE: <test ids>` auto-approves only when P2 is enabled. The heal PR opens only if those tests fail at the failing SHA and pass on the PR tree. Otherwise implement holds with `ai:needs-human` and the reproduction result.
- **D3: the grouping key** is `(workflow name, failing step, error class)`. The error class comes from a fixed list: `stall`, `timeout`, `test_failure`, `crash`, `api_error`, `other`. The window is 3 days.
- **D4: porting a `stable` fix to `main`.** After a heal PR merges into `stable`, a follow-up heal issue is opened on `main` if the same patch still applies there. The heal pipeline does not open PRs on two branches itself.

## Phases & Merge Strategy

Each phase is one PR, independently mergeable, and safe in production with its switch off. No phase needs another to have merged. Where a phase can use another's output, it reads it only when present and works without it.

1. **P1: Evidence pack.**
   - Switch: `WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED` (default `false`).
   - Scope: an intake-built `## Evidence pack` section in the heal issue body. It holds:
     - the failed run's step timeline;
     - `head_sha` and `head_branch` for linked runs;
     - issue-linked runs;
     - divergence of the scope paths between `main` and `stable`.

     The pack is also given to clarify-respond.
   - Done when: with the switch on, a heal filed from a recorded release-gate payload includes a pack listing the working implement run and its timeline. With the switch off, the issue body is byte-identical.
   - Rollback: switch off, or revert.
2. **P2: Reproduction runner.**
   - Switch: `WORKFLOW_HEAL_REPRODUCE_ENABLED` (default `false`).
   - Scope:
     - an intake check that runs the failing tests at the failing SHA and the last green SHA;
     - `--venv` for the heal gate;
     - the `REPRODUCIBLE` counterfactual (D2) and a check before the PR opens.
   - Done when: a recorded CI failure of a test file reproduces at intake, and a fix that turns it green auto-approves and opens its PR. A fix that doesn't is held with the result.
   - Rollback: switch off. `REPRODUCIBLE` then parses as a hold, same as `UNVERIFIED`.
3. **P3: Trends and the timeout rule.**
   - Switch: `WORKFLOW_HEAL_TREND_ENABLED` (default `false`).
   - Scope:
     - for stall and timeout failures, a run-time trend table;
     - the last green run and the commits merged since;
     - a `HEAL_REGRESSION:` line the plan must include;
     - a plan-gate hold for plans that only change timeouts or budgets.
   - Done when: replaying release run 40876853022's payload yields a trend showing implement at about 23 min then about 53 min, with #8192 among the suspect commits. A plan that only raises `PHASE_TIMEOUT` is held.
   - Rollback: switch off.
4. **P4: Suspect-commit scope.**
   - Switch: `WORKFLOW_HEAL_SUSPECT_SCOPE_ENABLED` (default `false`).
   - Scope: pipeline files changed by suspect commits are added to the frozen scope inputs:
     - at most 8 paths;
     - only from `scripts/`, `.github/workflows/`, `workflow-templates/` and `prompts/`;
     - never `.github/ai/` or `.claude/`.
   - The suspect commits come from the same GitHub compare data P3 uses. P4 computes them itself when P3 is off.
   - Done when: the replayed payload's marker includes `scripts/implement_step_prepush_test_gate.sh` and `workflow-templates/.claude/hooks/prepush_affected_tests.py`, and the marker still verifies.
   - Rollback: switch off. Already-filed markers are unaffected.
5. **P5: Root-cause grouping.**
   - Switch: `WORKFLOW_HEAL_CAUSE_GROUPING_ENABLED` (default `false`); window `WORKFLOW_HEAL_CAUSE_GROUPING_DAYS` (default `3`).
   - Scope: a secondary `cause=` key on heal issues. Intake records an occurrence on an open heal with the same cause instead of filing a new one.
   - Done when: replaying #8391's payload while #8376 is open posts an occurrence on #8376 instead of a new issue.
   - Rollback: switch off. Existing `cause=` markers are ignored.
6. **P6: Branch awareness.**
   - Switch: `WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED` (default `false`).
   - Scope: target the branch the failing code ran from, using the P1-style child-run `head_sha`/`head_branch` or the `SCRIPT_REF` echoed in child logs. Add a port issue to `main` after a `stable` merge (D4).
   - Done when: the replayed payload targets `main` and the issue states the code ref. A merged `stable` heal whose patch applies to `main` produces one port issue.
   - Rollback: switch off.
7. **P7: Post-merge verification.**
   - Switch: `WORKFLOW_HEAL_VERIFY_ENABLED` (default `false`).
   - Scope: the hourly `recovery-scan` job checks heals merged in the last 7 days. It reopens a heal with a recurrence comment, and adds `ai:workflow-heal-escalated` on a second recurrence. A pass is logged.
   - Done when: a fixture heal whose next run failed at the same step is reopened, one whose next run passed logs `outcome=held`, and nothing changes with the switch off.
   - Rollback: switch off.

Suggested order (not required): P1, P2, then P3, P5, P7, then P4, P6.

## Implementation Steps

### P1: Evidence pack

1. `scripts/workflow_failure_heal.py`:
   - Add `render_failed_run_timeline(run_json, jobs_json)`. It reuses `render_linked_run_timeline`'s step formatting and `[LONG]` marking, applies the same caps (8 KiB, 20 jobs, 40 steps), and takes the jobs JSON the intake already saved (`${LOG_DIR}/run-<id>-jobs.json`). No new API call.
   - Extend `render_linked_run_timeline`'s header with `head_sha` (12 characters) and `head_branch`, taken from the run JSON it already receives. Keep the existing header line and add a second line, so existing parsers keep working (§6).
2. `scripts/workflow_failure_heal.py`: add `issue_linked_runs` for a `workflow_run` report.
   - From the failed job logs, collect same-repo issue references: `issues/<n>` URLs, `issue #<n>` and `on issue #<n>`. Cap: 2 issues.
   - For each issue, call `GET repos/<repo>/actions/runs?created=<job start>..<job end>&per_page=100`. One call per issue, at most 2 per report, both counted against `GH_PAT_BUDGET`.
   - Keep runs whose `display_title` equals the issue's title. Fetch the issue title with one `GET issues/<n>` per issue, unless it already appears in the log.
   - Render each run as `id status/conclusion created run_started_at updated head_sha head_branch`.
   - Fail open: on any error, log `WORKFLOW_HEAL evidence_pack outcome=partial reason=<r>`.
3. `scripts/workflow_failure_heal.py`: add `branch_divergence`, which is one `GET repos/<repo>/compare/<target>...<default>` call. List the commits on the default branch that touch any scope-marker path (subject, short SHA, PR number), at most 10. Skip it when the target branch is the default branch.
4. `scripts/workflow_failure_heal_intake.sh`:
   - Behind `WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED`, build the pack after the scope freeze (lines around 2501-2533), so it never feeds the marker.
   - Insert it into the issue body as `## Evidence pack` with an untrusted-data banner, capped at `WORKFLOW_HEAL_EVIDENCE_PACK_MAX_BYTES` (default 12000).
   - Log `WORKFLOW_HEAL evidence_pack outcome=ok|partial|skipped bytes=<n>`.
5. `scripts/workflow_failure_heal_evidence.sh` (the plan and clarify bundle): when the issue body has an `## Evidence pack`, include it in full before the per-job tails, inside the existing 24,000-byte limit, with priority over the tails.
6. `.github/workflows/orchestrate_clarify_respond.yml` and `scripts/clarify_github_facts.py`: when the issue is a heal and the pack exists, pass it in the respond prompt under `=== HEAL EVIDENCE PACK (UNTRUSTED) ===`.
7. `prompts/mode-workflow-failure-heal.txt`, `prompts/mode-plan-heal-counterfactual.txt` and their `_templates/` copies: tell the model to use the pack before declaring evidence missing.
8. `.github/workflows/workflow-failure-heal-intake.yml`: add `WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED: ${{ vars.WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED || 'false' }}` and `WORKFLOW_HEAL_EVIDENCE_PACK_MAX_BYTES` to the intake and recovery-scan job env.
9. Fragments:
   - `agents.d/<n>-heal-evidence-pack.md`: README-level description, plus the `WORKFLOW_HEAL evidence_pack` log key under the existing `WORKFLOW_HEAL` prefix entry.
   - `changelog.d/<n>-heal-evidence-pack.md`.

### P2: Reproduction runner

1. `workflow-templates/.claude/hooks/prepush_affected_tests.py`: add `run --tests-file <file>`, an explicit list of test files with optional `::name` entries that bypasses `select_tests`. It is additive (§14), and the existing `run` arguments are unchanged.
2. `scripts/prepush_test_gate_run.sh`: add `--tests-file`, passed through to the module and mounted read-only. Existing callers are unchanged.
3. `scripts/heal_reproduce.sh` [new], called from the intake and from `heal_isolated_implement.sh`.
   - Inputs: a test list from `extract_failing_tests`, a SHA to test, and a baseline SHA.
   - It exports both trees with `git archive`, prepares deps with `codex_isolated_exec.sh prepare --deps` to get a venv, and runs `prepush_test_gate_run.sh` with `--tests-file`.
   - Output: `heal_repro.v1` JSON `{status: reproduced|not_reproduced|unverified, failing_at, passing_at, tests[]}`.
   - Wall time: `WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES` (default 10).
   - Log prefix: `HEAL_REPRODUCE`.
4. `scripts/workflow_failure_heal_intake.sh`: behind the switch, when failing tests exist, run `heal_reproduce.sh` at the failing SHA, with the last green SHA of the same workflow on the same branch as baseline. Finding the last green SHA takes one `actions/workflows/<id>/runs?status=success&branch=<b>&per_page=1` call. Add a `## Reproduction` section to the body.
   - "Fails at failing SHA, passes at last green" means a deterministic regression. Feed this into classification: never `transient`.
   - "Fails at both" means pre-existing.
5. `scripts/heal_isolated_implement.sh`: pass `--venv` from the prepared deps to the heal gate. This fixes `pytest_unavailable`, independently of the switch.
6. `scripts/partial_delivery.py`: accept `HEAL_COUNTERFACTUAL: REPRODUCIBLE: <test ids>`.
   - Switch on: decision `approve_pending_repro`. Switch off: decision `hold` with reason `reproduction_disabled`.
   - `PASS`, `PARTIAL`, `FAIL` and `UNVERIFIED` are unchanged.
7. `.github/workflows/plan.yml` (about 1 KB): treat `approve_pending_repro` like approve, but post `/approved [auto-approved-by-plan] [repro-required]`.
8. `scripts/heal_isolated_implement.sh`: when the approving comment carries `[repro-required]`, run `heal_reproduce.sh` on the edited tree against the failing SHA before handing back to implement.
   - Tests must fail at the failing SHA and pass on the edited tree.
   - On a mismatch, exit with a new code that `implement.yml` (about 0.5 KB) maps to `implement_heal_preflight.sh refuse repro_not_fixed`. That comments the result and adds `ai:needs-human`.
9. `prompts/mode-plan-heal-counterfactual.txt` and its template copy: tell the planner when to emit `REPRODUCIBLE` (a `## Reproduction` section shows `reproduced`).
10. Fragments: `HEAL_REPRODUCE` log prefix and README section in `agents.d/`; changelog entry.

### P3: Trends and the timeout rule

1. `scripts/workflow_failure_heal.py` `classify_failure_shape`: return `stall` or `timeout` when the failed job concluded `timed_out`, or the signature matches `stalled|no activity for|timed out|exceeded .* budget|timeout`. Otherwise return `other`. The function is pure and adds no API calls.
2. `scripts/workflow_failure_heal.py` `trend_table`: for the failing workflow and the workflows of linked and issue-linked runs (at most 3), call `actions/workflows/<file>/runs?status=success&created=>=<now-7d>&per_page=100`, one call each. Render the per-day `n`, median and p90 duration.
3. Mark `regression=true` when the latest day's median is at least `WORKFLOW_HEAL_TREND_REGRESSION_RATIO` (default 1.5) times the median of the earlier days.
4. `scripts/workflow_failure_heal.py` `suspect_commits`:
   - Find the last successful run of the failing workflow on its branch (shared with P2 step 4).
   - Call `compare/<last_green_sha>...<failing_sha>` once.
   - List the commits (subject, PR number, short SHA, pipeline files touched), at most 15.
   - If `regression=true`, also list the default-branch commits merged on the first slow day: one `commits?since=&until=&per_page=50` call.
5. `scripts/workflow_failure_heal_intake.sh`: behind `WORKFLOW_HEAL_TREND_ENABLED`, for `stall` or `timeout` shapes, add `## Run-time trend` and `## Suspect commits` sections.
6. `scripts/partial_delivery.py`: add `parse_heal_regression`. When the issue body says `regression=true`, the plan must contain one `HEAL_REGRESSION: <file or commit> | not a regression: <reason>` line.
   - Hold reason `regression_line_missing`.
   - Also hold with `budget_only_plan` when the plan's `## Files likely to change` only names the failing workflow and its text only touches `timeout-minutes`, `*_TIMEOUT*`, `PHASE_TIMEOUT` or `*_BUDGET*`.
7. `.github/workflows/plan.yml`: call the new check next to the counterfactual gate. This is a few lines; the body lives in the script.
8. `prompts/mode-workflow-failure-heal.txt` and the counterfactual prompt: add the regression rule.
9. Fragments.

### P4: Suspect-commit scope

1. `scripts/workflow_failure_heal_intake.sh`: behind the switch, before the freeze, add P3's suspect-commit files to `SCOPE_INPUTS_FROZEN` as a new input.
   - The suspect commits come from GitHub API data, not from logs, so they are trusted metadata.
   - Only files under `scripts/`, `.github/workflows/`, `workflow-templates/` and `prompts/` are kept; never `.github/ai/` or `.claude/` (the existing `_safe_heal_path` rules); at most 8.
   - When P3 is off, compute the compare call here.
2. `scripts/workflow_failure_heal.py` `render_heal_scope_marker`: add a `suspect_files` parameter, ranked after workflow paths and before `changed_files`. The existing 20-path cap is unchanged. Without the parameter, output is identical.
3. Fragments. Cross-reference #8290's plan in the `agents.d/` text, so its P1 and P2 inputs stack with this one.

### P5: Root-cause grouping

1. `scripts/workflow_failure_heal.py` `cause_key(workflow, failing_step, shape)`: sha256 of the workflow name (with the cycle suffix stripped, as in `fingerprint()`), the failing step and the shape from P3 step 1. That step is pure and lands in this phase if P3 has not merged.
2. `render` adds `<!-- workflow-failure-heal:cause=<64hex> -->` to new heal issue bodies, next to the existing `fp=` marker (§6: additive).
3. `budget` decision: behind the switch, a new report with no `fp` match but a `cause` match on an open heal created within `WORKFLOW_HEAL_CAUSE_GROUPING_DAYS` becomes an occurrence on that heal.
   - The comment includes the new run link, its signature and the P1 pack if enabled.
   - It logs `WORKFLOW_HEAL duplicate reason=cause_match`.
4. Fragments.

### P6: Branch awareness

1. `scripts/workflow_failure_heal.py` `code_ref`:
   - From the P1 data (or one `GET actions/runs/<id>` per linked or issue-linked run when P1 is off, at most 3), take the `head_sha` and `head_branch` of the run whose step failed or stalled.
   - Also parse `SCRIPT_REF: <sha>` from the evidence logs.
   - If that branch differs from the failed run's `head_branch` and the scope paths include its workflow, set the target branch to it.
   - Write `- **Code ran from:** <branch>@<sha>` in the body.
2. `scripts/heal_port_to_default.sh` [new], called from `.github/workflows/issue_pr_status.yml`'s merged-PR path next to `handle_partial_delivery_merge` (defined at line 259, called around line 761), and wired in the same PR (§18.B).
   - When a heal PR merges into `stable`, it checks whether the merged diff applies to the default branch with `git apply --check` on a fresh checkout of it.
   - If it does, it files a heal issue on the default branch with the patch summary and `Refs #<heal>`.
   - It logs `WORKFLOW_HEAL port outcome=filed|not_needed|not_applicable`.
3. Fragments.

### P7: Post-merge verification

1. `scripts/workflow_failure_heal_recovery_scan.sh`: behind `WORKFLOW_HEAL_VERIFY_ENABLED`, list heal issues closed in the last 7 days whose linked heal PR merged. That comes from the list the scan already fetches, with no new call.
2. For each, at most `WORKFLOW_HEAL_VERIFY_MAX_PER_TICK` (default 3) per tick:
   - Make one `actions/workflows/<file>/runs?branch=<target>&created=><merged_at>&per_page=5` call.
   - Take the first completed run whose `head_sha` contains the merge commit. Ancestry is checked with one `compare` call only when the SHAs differ.
   - If it failed with the same failing step, reopen the heal with a `## Fix did not hold` comment (run link, signature) and log `WORKFLOW_HEAL_VERIFY outcome=recurred`. On a second recurrence, add `ai:workflow-heal-escalated` and send a CRITICAL alert.
   - If it succeeded, log `outcome=held` and record a marker comment `<!-- workflow-failure-heal:verified:v1 run=<id> -->`, so the heal is checked only once.
3. `.github/workflows/workflow-failure-heal-intake.yml`: add the two env vars to the `recovery-scan` job.
4. Fragments: the `WORKFLOW_HEAL_VERIFY` log prefix.

## Files & Modules

- `scripts/workflow_failure_heal.py` (P1, P3, P4, P5, P6)
- `scripts/workflow_failure_heal_intake.sh` (P1, P2, P3, P4)
- `scripts/workflow_failure_heal_evidence.sh` (P1)
- `scripts/workflow_failure_heal_recovery_scan.sh` (P7)
- `scripts/partial_delivery.py` (P2, P3)
- `scripts/heal_reproduce.sh` [new] (P2)
- `scripts/heal_isolated_implement.sh` (P2)
- `scripts/prepush_test_gate_run.sh` (P2)
- `workflow-templates/.claude/hooks/prepush_affected_tests.py` (P2, additive; ships to consumers per §14)
- `scripts/clarify_github_facts.py`, `.github/workflows/orchestrate_clarify_respond.yml` (P1)
- `.github/workflows/workflow-failure-heal-intake.yml` (P1, P2, P3, P4, P5, P6, P7: env only)
- `.github/workflows/plan.yml` (P2, P3: a few lines each)
- `.github/workflows/implement.yml` (P2: about 0.5 KB exit-code mapping)
- `scripts/implement_heal_preflight.sh` (P2: refusal reason `repro_not_fixed`)
- `scripts/heal_port_to_default.sh` [new] and `.github/workflows/issue_pr_status.yml` (P6: wiring only)
- `prompts/mode-workflow-failure-heal.txt`, `prompts/mode-plan-heal-counterfactual.txt` and their `_templates/` copies (P1, P2, P3)
- `docs/scripts-pending-removal.md`: no entry, because `heal_reproduce.sh` is a permanent library script called from automation, not single-use (§18.F).
- Tests: `tests/test_workflow_failure_heal.py`, `tests/test_workflow_failure_heal_scope_tests.py`, `tests/test_partial_delivery.py` (or the existing counterfactual test module), `tests/test_prepush_affected_tests.py`, `tests/test_heal_isolated_editor.py`, `tests/test_workflow_failure_heal_recovery_scan.py`, plus new `tests/test_heal_reproduce.py`.
- One `agents.d/` and one `changelog.d/` fragment per phase.

## Data Model / Index Changes

None. No MongoDB collections are involved.

## Tests

- **Unit (per phase):**
  - Renderers: timeline, issue-linked runs, divergence, trend, suspect commits, cause key and code ref. Use recorded JSON fixtures from release runs 40876853022 and 40932430968 (runs, jobs, compare) under `tests/fixtures/heal/`.
  - Parsers: `REPRODUCIBLE` and `HEAL_REGRESSION`, including the switch-off cases.
  - Scope marker rendering with `suspect_files`: the cap, the allowed roots and the forbidden paths.
- **Switch-off byte-identity:** for each phase, a test that renders a heal body (or plan decision) with the switch off and compares it to the current output fixture.
- **Intake replay:** the existing intake replay tests (`test_intake_replays_*` in `tests/test_workflow_failure_heal_scope_tests.py`) get a release-gate stall replay. It asserts the evidence pack (P1), the trend and suspect commits (P3), the scope addition (P4), cause grouping against an open fixture heal (P5) and the code-ref target (P6).
- **Reproduction:**
  - `tests/test_heal_reproduce.py` with a fake docker and fake gate result, covering the reproduced, not-reproduced and unverified paths.
  - One docker end-to-end test, skipped when docker is unavailable, following `tests/test_codex_isolated_exec_docker_e2e.py`.
- **API budget:** each new call path asserts its call count against a fake `gh`. The P1 budget is at most 5 extra calls per report, P3 at most 5, P7 at most 2 per heal per tick.

## Risks & Mitigations

- **Larger issue bodies push out other evidence.** The pack is capped at 12,000 bytes and has its own section. The plan bundle gives it priority but keeps the 24,000-byte total.
- **Issue-linked runs match the wrong runs.** Matching needs the exact title and the job's time window, and the pack labels them "runs started for issue #N". The planner treats them as evidence, not as fact.
- **Reproduction cost: runner minutes.** It runs only for failures with extracted failing tests and is capped by `WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES`. The image and deps reuse existing caches.
- **Reproduction runs consumer code.** It uses the same container rules as the pre-push gate: `--network none`, read-only root, all capabilities dropped, no credentials, results copied back as JSON only.
- **`REPRODUCIBLE` auto-approves a wrong fix that happens to turn the tests green.** The normal review pipeline and CI still run on the heal PR. With D2, the tests must fail before the fix and pass after it, which is stronger than today's reasoning-only `PASS`.
- **Cause grouping merges two different causes.** The occurrence comment carries the new run's signature and evidence. The heal's planner sees both, and the window is short (3 days).
- **The timeout rule blocks a legitimate budget increase.** `HEAL_REGRESSION: not a regression: <reason>` lets the plan explain one.
- **API budget.** At most about 12 extra REST calls per heal report across all phases, and 2 per verified heal per hourly tick. All are fail-open and logged under `GH_PAT_BUDGET`.
- **Workflow size (§27).** Only `plan.yml` and `implement.yml` grow, by a few hundred bytes each; everything else goes into scripts.

## Rollout

1. Merge the phases in any order, all with switches off.
2. Turn on P1, then watch the next 3 heal issues' packs for size and accuracy.
3. Turn on P3 and P5, then P2 (it costs runner minutes), then P7, then P4 and P6.
4. After a week without problems for each phase, open one follow-up PR that flips that phase's default to `true`, keeping the switch.
5. Rollback for any phase: set its repo variable to `false`. No data migration is involved, and markers added by a phase are ignored when it is off.
6. Consumer repos (§14): only P2's additive gate option reaches them via `workflow-templates/.claude/hooks/prepush_affected_tests.py`. Their pipelines are unchanged unless they call `--tests-file`.

## Automation and wiring (§18.E)

- **New scripts:**
  - `scripts/heal_reproduce.sh` (P2), called from `scripts/workflow_failure_heal_intake.sh` and `scripts/heal_isolated_implement.sh`.
  - `scripts/heal_port_to_default.sh` (P6), wired into `.github/workflows/issue_pr_status.yml`'s existing merged-PR path in the same PR.
  - Everything else extends existing scripts.
- **Entry points (no new triggers):**
  - `.github/workflows/workflow-failure-heal-intake.yml`: the intake job (`workflow_run`, `repository_dispatch`, `workflow_dispatch`) and the hourly `recovery-scan` job.
  - `.github/workflows/plan.yml`: the heal counterfactual gate step.
  - `.github/workflows/implement.yml`: the heal isolated editor step.
  - `.github/workflows/orchestrate_clarify_respond.yml`.
  - `.github/workflows/issue_pr_status.yml`: the merged-PR handler.
- **Long-running supervisor:** none. P7 uses the existing hourly `recovery-scan`.
- **DB work:** none.
- **`docs/scripts-pending-removal.md`:** no entries. The new scripts are permanent library steps called by automation, not single-use, long-running or supervisor scripts.

## References

- Incident: release runs 40608957733, 40831286935, 40876853022 and 40932430968; implement runs 40882033359 and 40936238289.
- Heal issues: #8287, #8327, #8347, #8352, #8376, #8391, #8393, #8397. PRs #8382 (gate baseline fix), #8404 (live-copy sync), #8371 (review loop).
- Prior plans: `docs/plans/heal-scope-linked-runs-script-closure-plan.md` (#8290), `docs/plans/heal-issues-self-resolve-plan.md` (#8200), `docs/plans/release-blocker-heal-reports-plan.md`, `docs/plans/prepush-affected-test-gate-plan.md`.
- Related issue: #8405 (live-copy sync bug).
