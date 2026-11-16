# Workflow optimization report: shubhodeep1/coding-workflows, 2026-11-15 21:16Z to 2026-11-16 04:05Z

The deep-dive log folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted for this run. Everything below comes from `analysis_context.json` (run rows and the `log_summary` fields) plus a read of the workflow and script source in the repo. `insufficient_data` is `false`. However, the 1,000-run sample covers only about 6h49m, because 772 of the 1,000 runs (77%) are `skipped`/"other".

## Executive Summary

- **The pool of GitHub-hosted `ubuntu-24.04`/`ubuntu-latest` runners is saturated, and that is where most of the time goes.**
  - Impact: very high. Confidence: high.
  - The lint and readiness jobs have `timeout-minutes: 5` (`.github/workflows/lint-pr-body-auto-close.yml:47`, `integration-pr-readiness.yml:44`), yet they succeed after 10,173s p50 and 17,849s max (run 43775010315). A job timeout only counts execution time, so nearly all of those durations are waiting in the queue.
  - Four sweep `log_summary` entries quote "All GitHub-hosted runners with label [ubuntu-24.04] are busy". Run 43795529699 waited about 59 minutes and then executed for 57s.
- **CI fans out to roughly 39 jobs per run and has no `concurrency:` guard**, so superseded PR commits keep their jobs queued.
  - Impact: high (likely the largest runner consumer; inference). Confidence: medium.
  - Of 10 CI runs, all 10 were cancelled and none succeeded. For example, run 43764204116 was cancelled after 22,576s at `tests-heal-plan-and-validation` / `cancelled_before_first_step`.
  - Count: 24 `system-python-shard` + 5 `poll-group` + 3 `sandbox-family` + 7 single jobs (`ci.yml:2771, 3556, 3714`; no `concurrency` key anywhere in `ci.yml`).
- **The orchestrator poller is effectively stalled.**
  - Impact: high (wave progression blocked). Confidence: medium.
  - 37 of 38 poll runs were cancelled before their first step. The only success, 43763083431, finished around 23:57Z; after that, every tick from 00:01Z onward was superseded.
  - The `poll` job has no `timeout-minutes` (`orchestrate_poll.yml:95-102`; the only one in the file is `:2239`, on the daily-usage job), so GitHub's 360-minute default applies.
  - Inference: either a long active poll held `ai-orchestrate-poll-<repo>`, or the job sat waiting for a runner.
- **The AI pipeline did almost no work in this window, and token telemetry would not show Claude work anyway.**
  - Impact: observability. Confidence: high.
  - Implement had 0 successes in 159 runs, plan 1 in 163, clarify 4 in 171. Every token counter is 0 and `cache_hit_rate` is null.
  - `RUN_COST_TELEMETRY_FIELDS` (`scripts/cost_audit.py:209`) and `AGGREGATABLE_COST_FIELDS` (`:246`) leave out `CLAUDE_COUNT_KEYS` (`:128`) and `claude_cost_usd`, so Claude-engine usage never reaches this context.
- **`promote-candidate` probably lags `main`.**
  - Impact: medium (correctness drift). Confidence: medium.
  - 10 of 11 `Promote candidate follower` runs were cancelled (for example 43792011382 after 3,605s), and the one success, 43768568268, took 10,912s.
  - The internal wrappers (for example `internal-orchestrate-poll.yml:18`) call `@promote-candidate`. Inference: pipeline runs may be using stale support code.
- **About 15 runs were cancelled in one burst at about 03:41Z without ever starting.** They are trios of lint, readiness and review-gate runs created between 02:12Z and 03:41Z:
  - 43790693538, 43790693682, 43790693891 (02:12:03Z, cancelled after about 5,387s)
  - 43790790618, 43790790622 (02:12:59Z)
  - 43795271347, 43795271381, 43795271476 (02:57:53Z)
  - 43799571683, 43799571706 (03:40:19Z)
  - 43799680321, 43799680410 (03:41:30Z)

  Each one's `created_at + duration` lands at 03:41:00–03:41:53Z. These are PR checks that queued for up to 89 minutes and never ran before the PR (presumably) closed.
  - Impact: diagnostic. Confidence: high on the timing, low on the cause.

## Speed Optimizations

All of the items below are on the critical path; the window shows no micro-optimizations worth listing.

1. **Cancel superseded PR CI runs.**
   - Evidence: 10 of 10 CI runs cancelled. Job-level `cancelled_before_first_step` waits of 22,576s (43764204116), 19,017s (43771105041, `static-checks`), 17,526s (43773002936) and 9,591s (43765558368, `tests-sandbox-python (codex-isolated-codex)`).
   - Root cause: `ci.yml` triggers on `pull_request: ["**"]` and on `push` to `main`/`stable`/`orchestrator/project-*` with no concurrency group. Every autofix push queues another ~39 jobs while the old SHA's jobs are still pending.
   - Change: add a workflow-level block after `on:` in `ci.yml`. It applies only to `pull_request`, so base-branch push CI, which check-failure triage needs (`ci.yml:5-12`), is never cancelled:
     ```yaml
     concurrency:
       group: ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
   - Estimated saving: removes the ~39-job backlog for every superseded PR push. It should cut queue wait for every other workflow by a large fraction of the hours currently observed (inference, not measured).
   - Risk: low. Cancelled checks land only on stale SHAs, which cannot be merged.
2. **Give `lint-pr-body-auto-close.yml` and `integration-pr-readiness.yml` per-PR concurrency.**
   - Evidence: 22 successes each, p50 10,239.5s / 10,173s, p95 16,862s / 16,943s, against a 5-minute execution budget.
   - Root cause: every `edited`, `synchronize`, `labeled` or `unlabeled` event queues its own job, and none is ever superseded.
   - Change: add `concurrency: { group: <workflow-slug>-pr-${{ github.event.pull_request.number }}, cancel-in-progress: true }` to each file.
   - Risk: low. The newest event re-evaluates the newest title, body, labels and head SHA, and readiness posts a commit status for that SHA.
   - Saving: these are tiny jobs, so they free little runner time, but they stop older events from piling up in the queue.
3. **Sweep-type workflows: no change; the delay is entirely runner wait.** Queue time versus execution time:

   | Run | Queue wait | Execution |
   |---|---|---|
   | 43795529699 | ~3,569s | 57s |
   | 43801483983 | ~192s | ~30s |
   | 43789562661 | ~152s | not given |
   | 43786748921 | ~208s | not given |
   | 43795977490 (Drift Audit) | ~757s | ~42s |

   Fixes 1 and 2 address the cause.

## Cost Optimizations

1. **Runner minutes, not tokens, are the measurable cost this window.**
   - CI fan-out of about 39 jobs per PR push is the top driver, and the Speed fix 1 concurrency block is also the top cost fix.
   - The saving is proportional to how often a PR is pushed again within one CI duration. Autofix-driven re-pushes make that common (inference).
   - Quality risk: none, because only stale SHAs are cancelled.
2. **Token, model and cache cost cannot be evaluated.**
   - Every `codex_*`, `or_*`, Semble and Serena counter is 0 across all 115 runs with log telemetry.
   - The AI phases were mostly `skipped`: implement 159/159, plan 162/163, clarify 167/171, clarify-respond 165/165. That is plausibly a downstream effect of the stalled poller (inference).
   - Even when the Claude engine runs, its usage is dropped from per-run `cost_telemetry`, because `build_run_cost_telemetry` (`scripts/cost_audit.py:609-613`) copies only `RUN_COST_TELEMETRY_FIELDS`.
   - Change (additive, backward compatible): append the five `CLAUDE_COUNT_KEYS` and `claude_cost_usd` to `RUN_COST_TELEMETRY_FIELDS`. Also append the five count keys to `AGGREGATABLE_COST_FIELDS`, and sum `claude_cost_usd` as a float in `aggregate_run_cost_telemetry`.
3. **Semble and Serena.** No `SEMBLE_*` or `SERENA_*` lines appear in any run or `log_summary`, so there is no evidence either way on prompt-expansion reduction or noisy bytes.
4. **Wasted log fetches in Drift Audit.**
   - `scripts/drift_audit.sh:816-818` still calls `_fetch_run_log` for runs whose conclusion is in `UNSCANNABLE_CONCLUSIONS`. In run 43795977490 that was 9 of 25 fetches (36%), at about 2–5s each.
   - Change: skip the fetch when `conclusion == "skipped"`. Keep fetching for `cancelled`, because runs cancelled mid-execution do have logs.
   - Small saving: about 20–45s and up to 9 REST calls per audit.

## Reliability Improvements

1. **Poller starvation** (category: concurrency or hang).
   - Evidence: 37 of 38 `orchestrate_poll` runs `cancelled_before_first_step` (for example 43801386220 after 1,020s, 43777910055 after 2,357s). The only success is 43763083431 (9,422s). The cron is `*/5` (`internal-orchestrate-poll.yml:5`), and the job-level group uses `cancel-in-progress: false`, so GitHub keeps only one pending run and drops the older one.
   - Fix, in order:
     1. Add the heartbeat logging under Diagnostic-logging additions to confirm whether an active run is holding the group.
     2. Then set an explicit `timeout-minutes` on `poll` (`orchestrate_poll.yml:95`), sized to the measured execution p99 plus headroom. That must stay well under the implicit 360 minutes.
   - Impact: restores poll ticks and wave progression.
   - Rollback consideration: a timeout can kill a run in the middle of conflict resolution, which the comment at `:98-100` warns about. Measure before you cap.
2. **Promote-candidate follower superseded or queued** (category: runner starvation).
   - 10 of 11 cancelled. The fixes under Speed Optimizations relieve this indirectly.
   - Add an indicator: compare the `promote-candidate` SHA with the `main` SHA at the end of each poll and log the lag.
3. **Cancel-on-close sweeps cannot run.**
   - 36 of 40 runs were `cancelled_before_first_step` (for example 43801901279 after 878s, 43795013154 after 1,196s).
   - Under starvation, the scheduled sweep that cancels runs for closed PRs (`cancel_on_pr_close.yml:301-323`) is itself queued, so it cannot clear the backlog. This loop should ease on its own once Speed fix 1 lands.
4. **There are no failures:** `failure_count` is 0 in every family. The cancellation rate is 147 of 228 completed non-skipped runs (64.5%).
5. **`BREAK_GLASS`, `CONTEXT_BUDGET_WARN`, Semble/Serena fallbacks and probes:** none observed.

### Diagnostic-logging additions (SRE asks)

Each new log prefix needs an `agents.d/<id>-<slug>.md` fragment under "Stable log prefixes (contractual)", per §25.

| # | Where | Line to add | Extra API calls |
|---|---|---|---|
| D1 | `scripts/collect_workflow_logs.py`, next to `extract_failure_point` (`:1069`) | Per-run `queue_wait_seconds` (first job's `started_at − created_at`) and `exec_seconds`, taken from the jobs payload it already fetches | 0 |
| D2 | `cancel_on_pr_close.yml:372` loop | `CANCEL_ON_PR_CLOSE_TARGET run_id= workflow_path= status= created_at= linked_prs=` from `active_runs_file`, which already holds these fields | 0 |
| D3 | `orchestrate_poll.yml` poll job, first and last step | `POLL_TICK_START run_id=` and `POLL_TICK_END run_id= elapsed_s=` | 0 |
| D4 | `scripts/cost_audit.py:209/246` | Claude keys (see Cost item 2) | 0 |
| D5 | Review autofix sweep | The bare `reason=other_conclusion` line (seen in 43795529699, 43792697346, 43789562661, 43786748921) should carry `pr=`, `run_id=` and `conclusion=` | 0 |

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary`. Several summaries say explicitly that none were visible, and the logs were truncated by about 89–96k characters. The deep-dive logs were unavailable, so retrieve hit rate, token-versus-budget, `keyword_method` mix and `fail_open` counts cannot be computed.

Recommendation: once the poller runs again, check that its runs emit `retrieve` events. If they do not, make sure `AI_MEMORY_TELEMETRY` lines go to step stdout outside the truncated middle section, for example by echoing a one-line summary at the end of the step.

## GH API Call Audit

- No GH API call-count, 429, secondary-rate-limit or retry signals appear in any visible summary.
- `scripts/drift_audit.sh:818`: per-run log fetch inside a loop, including runs that cannot be scanned. Skipping `skipped` runs saves up to 36% of that audit's fetches (9 of 25 in run 43795977490).
- `cancel_on_pr_close.yml:377`: one POST per cancelled run. There is no batch endpoint for this. The active-run listing is already a single prefetch (`_cpr_fetch_active_runs`), so it complies with §14.
- Two scheduled `*/5` workflows (poller and cancel sweep) plus the `*/30` review sweep make API-heavy ticks. With 90%+ of those runs superseded, actual API use is lower than the schedule implies. No action needed.
- D1 adds queue-wait observability without new calls, because it reuses the jobs payload.

## Prompt Cache & Memory System

`cache_hit_rate` is null in every family, and cache read/write tokens are 0. There is no evidence to judge prefix stability or fragmentation in this window. `CONTEXT_BUDGET_WARN` count is 0.

The first prerequisite is D4, the Claude key propagation. Claude cache creation and read tokens are parsed (`CLAUDE_USAGE_FIELDS`, `cost_audit.py:122-127`) but never surfaced per run, so any Claude-engine cache-hit analysis is impossible today.

## Orchestrator Health

**Stuck state.** The poller completed one tick in about 6.8 hours, at about 23:57Z (43763083431). Every tick after that was superseded (37 runs).

Indicators to track:
- `POLL_TICK_END` count per hour (D3)
- poll `queue_wait_seconds` (D1)
- the `promote-candidate`–`main` SHA lag

**Downstream symptoms.**
- Implement had 0 successes. One `Unblock judge #9238` run (43762912636) succeeded after 8,467s, and one `Internal: AI Validate [tracking:0]` run (43762909983) succeeded after 9,034s.
- Clarify successes took up to 10,860s (43771053738). Whether that was queue or execution is unknown without D1.

**Conflict-heal and clarification loops.** Not observable here; the clarify and plan runs were skipped by their filters.

**Smallest safe mitigation.** Speed fix 1 frees runners, and D3 confirms whether there is a hang before adding the timeout.

## Pipeline Flow Bottlenecks

1. **Queueing dominates everything.**
   - PR checks: lint and readiness p50 about 10,200s.
   - CI: jobs pending up to 22,576s.
   - Sweeps: 40s to 59 minutes of queue against 30–57s of execution.
   - Drift audit: 757s of queue against 42s of execution.
2. **Retries contribute nothing:** `retries` is 0 and `run_attempt` is 1 on every row.
3. **Compute is small** wherever it was visible (30–57s sweeps, 42s audit). CI compute could not be measured.
4. **Merge and conflict:**
   - The 03:41Z burst cancelled about 15 never-started PR checks at once.
   - At 02:13:25Z a second burst cancelled 43790775820, 43790786288, 43790786301, 43790786392 and 43790790579 within about 30 seconds of creation.
   - D2 will attribute these cancellations to a workflow and PR.

**Order of fixes by end-to-end impact:**
1. CI concurrency (Speed fix 1)
2. PR-check concurrency (Speed fix 2)
3. Poller heartbeat, then timeout
4. Queue-wait metrics in the collector

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottleneck:** saturation of the hosted-runner queue. 1,000 runs in about 6.8 hours, with an estimated ~39 jobs per CI run.
- **Top failure modes:**
  - poller superseded: 37 of 38
  - cancel-on-close sweep superseded: 36 of 40
  - promote-candidate follower cancelled: 10 of 11
  - CI never started: 10 of 10
- **Highest cost driver:** CI runner minutes. Token cost cannot be measured (all zeros, and Claude keys are not surfaced).
- **Top 3 actions:**
  1. PR-only `cancel-in-progress` concurrency in `ci.yml`.
  2. Per-PR concurrency for the lint and readiness workflows.
  3. D1, D3 and D4 telemetry, followed by an evidence-sized poll `timeout-minutes`.

## Metrics Appendix

**Window and coverage:** 1,000 runs (77.2% skipped), from 21:16:44Z (43762784001) to 04:05:06Z (43801901279). 115 runs have parsed log telemetry, the success sample rate is 0.07 (5 sampled successes), and the deep-dive log directory was absent.

| Family | Total | Success | Cancelled | Other | p50 s | p95 s |
|---|---|---|---|---|---|---|
| ci | 10 | 0 | 10 | 0 | 1,901.5 | 20,974 |
| integration_pr_readiness | 32 | 22 | 10 | 0 | 10,173 | 16,943 |
| lint_pr_body_auto_close | 32 | 22 | 10 | 0 | 10,239.5 | 16,863 |
| orchestrate_poll | 38 | 1 | 37 | 0 | 539.5 | 1,395 |
| cancel_on_pr_close | 40 | 4 | 36 | 0 | 640 | 13,425 |
| review_autofix | 44 | 14 | 26 | 4 | 1,001.5 | 8,417 |
| promote_candidate_follow | 11 | 1 | 10 | 0 | 2,602 | 7,399 |
| security_audit | 7 | 5 | 2 | 0 | 1,409 | 2,261 |
| clarify / plan / implement | 171 / 163 / 159 | 4 / 1 / 0 | 0 | 167 / 162 / 159 | 1 | ≤10.5 |
| orchestrate_clarify_respond | 165 | 0 | 0 | 165 | 1 | 10 |
| check_failure_triage / workflow_failure_heal / heal_intake | 41 / 29 / 47 | 0 / 0 / 2 | 0 | 41 / 29 / 45 | 1 | ≤11 |
| **All** | **1,000** | **81** | **147** | **772** | **1** | **9,315** |

| Telemetry | Value |
|---|---|
| failure_count / failure_rate | 0 / 0.0 |
| codex_tokens_used, or_total_tokens, or cache read/write tokens | 0 |
| claude_* | not present in the context (D4 gap) |
| cache_hit_rate | null |
| wall_clock_p50_ms / wall_clock_p99_ms | 251,000 / 18,907,220 (112 samples; run duration including queue) |
| break_glass_count / context_budget_warn_count | 0 / 0 |
| GH API calls / 429s | not instrumented / none visible |
| Semble query/fallback/bootstrap; logged bytes | 0 / 0 / 0; 0 bytes |
| Serena query/fallback; response bytes; tool calls | 0 / 0; 0; 0 |
| Drift audit coverage (43795977490) | scanned 25, fetched 16, unscannable 9 (36%), missing 0 |

| MCP target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| semble | 0 | 0 | 0 |
| serena | 0 | 0 | 0 |

**Other MCP servers observed:** none.

**Data gaps:**
- The run-logs directory was unavailable.
- Sweep logs were truncated by 89–96k characters.
- The 1,000-run cap covered under 7 hours because of skipped runs. Excluding `skipped` from the cap in the collector would widen the window.
- Queue time and execution time are not separated (D1).

---
- **Actions taken:** read-only analysis of the analysis context plus `ci.yml`, `orchestrate_poll.yml`, `internal-orchestrate-poll.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `promote-candidate-follow.yml`, `review_autofix_sweep.yml`, `scripts/cost_audit.py`, `scripts/collect_workflow_logs.py` and `scripts/drift_audit.sh`. No files were changed.
- **Assumptions:** a 5-minute `timeout-minutes` paired with a multi-hour duration means queue wait. The 03:41Z cancellation cluster was worked out from `created_at + duration_seconds`.
- **Missing context:** the deep-dive log directory, per-job timestamps, Claude-engine usage, and GH API counters.