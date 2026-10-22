## Executive Summary

- **Hosted-runner saturation is the main latency problem that is not compute.** When PRs #7874 and #7875 landed at about 10:45, jobs logged "All GitHub-hosted runners with label [ubuntu-latest] are busy". They waited 130 s of 138 s (run 39765926260), 151 s of 266 s (39765517214), about 3.5 min (poll 39765652831), 2 min 19 s (poll 39767503891) and 3 min 42 s (CI 39765518275). One cheap source of demand: every PR close runs a full-history checkout in `heal-pr-reconcile`, which takes about 70–80 s and then does nothing. *Impact: high (1–4 min per job during bursts). Confidence: high on the waits, medium on how much runner demand each fix removes.*
- **Every real failure in the window was the CI test wiring guard.** Two CI runs, 39746784127 and 39746749349, failed at `static-checks / CI test wiring guard`. That is 2 of 43 CI runs (4.7%) and 100% of the 2 failures among 1000 runs. Each run still used about 14 min (831–844 s). The guard (`tests/test_ci_wires_every_test_file.py:57-63`) only fails when a `tests/test_*` file is named in no workflow. A local pre-push check would catch it. *Impact: removes all observed failures. Confidence: high.*
- **Review/autofix accounts for all model spend.** It used 68.0M OpenRouter tokens over 80 calls, all in 8 runs. Of those, 13.93M were uncached prompt tokens, 49.63M cache reads and 3.92M cache writes, so `cache_hit_rate` = 0.735. Cache writes are almost constant at 467k–506k per run, about 49k per call, whatever the hit rate. That pattern points to a per-call prompt prefix being rewritten each time (inference). *Impact: about 10–15% of review cost-equivalent if the hit rate rises to 0.80 or more (pricing ratios assumed). Confidence: medium.*
- **Logs hide where the long runs spend their time.** The poll step logs were truncated by about 850–860k characters (39765652831, 39767503891). CI `tests-release-and-log-analysis` has a 9–10 min window that cannot be seen in three runs: 39765523289, 39765259182 and 39765265055. The collector already receives the jobs payload in `compute_run_metrics` (`scripts/collect_workflow_logs.py:1112-1119`), so it can record per-step durations and queue wait with **no additional API calls**. *Impact: makes the next round of speed fixes possible. Confidence: high.*
- **Semble is cheap and not noisy, but bootstrap time is real.** There were 10 queries returning 135,915 bytes from 63 sources, with `static_dup_bytes` 0 and 0 unused bootstraps. Bootstraps took 401 s in total, about 41.6 s per review run. Of the 42 `SEMBLE_FALLBACK` lines, 40 come from CI contract tests (fixtures, healthy). The other 2 are runtime fallbacks, both in implement runs (39722823089, 39752242564), and implement had 0 queries, so every implement Semble attempt fell back. *Impact: low cost, about 40 s of latency per review run. Confidence: medium; n=2 for implement.*
- **The 22 poller cancellations (36% of 61) are designed supersession, not failures.** All are `poll / poll` → `cancelled_before_first_step` under `concurrency: ai-orchestrate-poll-… cancel-in-progress: false` (`orchestrate_poll.yml:68-73`), with cron `*/5` (`internal-orchestrate-poll.yml:5`) against a poll p50 of 665 s. *Impact: noise in reliability dashboards. Confidence: high.*

The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not present in this sandbox. The evidence below comes from `analysis_context` (`cost_telemetry` parsed from full logs, plus gpt-6-luna `log_summary` fields) and the repository source. `insufficient_data` is false.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Reduce runner demand during merge bursts (critical path).**
   - **Evidence:** The queue waits listed above, all clustered at 10:45–10:51 after the PR #7874/#7875 events. In that minute, about 12 or more workflows started together: CI, review gate, lint, readiness, cancel-on-close, issue-status, post-merge dispatch, and the AI fan-out of clarify, plan, implement and clarify-respond.
   - **Root cause:** Bursty fan-out against the hosted-runner concurrency cap.
   - **Change:** Take the cheap demand off first. That is item 4 below, plus measuring whether the 553 skipped AI fan-out runs take a runner (see Pipeline Flow Bottlenecks). Do not change the triggers until that measurement exists.
   - **Savings:** Likely 1–3 min per job during bursts (inference).
   - **Risk:** Low.
2. **Find the hidden 9–10 min in CI `tests-release-and-log-analysis` (critical path for CI, p50 845 s).**
   - **Evidence:** Gaps between `SessionStart hook repo-slug contract test` (`ci.yml:1574`) and `Set up Node 22…` (`ci.yml:2046`): 10:49:01→10:59:49 in 39765523289, 10:46:17→10:56:29 in 39765259182, and 10:46:11→10:55:25 in 39765265055. The vitest suite itself took 433 ms.
   - **Change:**
     1. Have the collector record step durations (Reliability item 5).
     2. Move the dominant step(s) into a sibling job, alongside the existing six-job split, so they run in parallel.
   - **Savings:** Up to 5–8 min of CI wall clock, if one step dominates (inference).
   - **Risk:** Low. The `lint` gate job (`ci.yml:2527`) already requires every job to succeed.
3. **Take the Semble bootstrap off the review critical path.**
   - **Evidence:** 8 bootstraps took 332,715 ms (41.6 s average) in review runs that last 1,678–2,275 s. `SEMBLE_BOOTSTRAP_MODE: lazy` means the first query waits for the bootstrap.
   - **Change:** Start the bootstrap in the background when the review job starts, and keep the lazy path as the fallback.
   - **Savings:** About 35–45 s per full review run, roughly 2% of the run (micro-optimization).
   - **Risk:** Medium (needs care with concurrent index writes). Fail-open is unchanged.
4. **Avoid the full-history checkout in `heal-pr-reconcile` when it will do nothing.**
   - **Evidence:**
     - `internal-cancel-on-pr-close.yml:45-50` checks out with `fetch-depth: 0`.
     - Run 39768883357 fetched the whole v1.x tag list (563,474 truncated characters). The checkout ended at 11:14:08 and accounted for most of the 79 s, while the script ran for 0.6 s and logged `noop reason=no_heal_issue`.
     - Run 39765517213 took about 80 s the same way.
     - The script decides `no_heal_issue` from a single `gh api` issue list (`workflow_failure_heal_pr_reconcile.sh:96-106`) before any git operation. Its later fetches already use `--no-tags`.
   - **Change:** Check out with `fetch-depth: 1`. In the script, run `git fetch --no-tags --unshallow origin` only after `HEAL_ISSUES` is non-empty, just before `_retarget_heal`, so merges still have full history.
   - **Savings:** About 70 s of runner time per PR close (31 cancel_on_pr_close runs in the window), mostly during bursts.
   - **Risk:** Low. The retarget path keeps full branch history; only tags are dropped, and the script never uses them.
5. **Poller duration (p50 665 s, p95 1143 s) cannot be diagnosed yet.** The step log is truncated by about 850k characters. Add the phase-timing line described in Reliability item 5 before optimizing.

## Cost Optimizations

Ranked by expected token or dollar savings. Pricing ratios are an **assumption** (uncached 1.0×, cache write 1.25×, cache read 0.1×), because the context does not name the OpenRouter models.

1. **Review/autofix uncached prompt volume.**
   - **Evidence:** 13,928,923 uncached prompt tokens over 8 runs, about 1.74M per run and about 174k per call. Under the assumed ratios, that is about 58% of cost-equivalent units: 13.93M of about 23.8M, with cache writes 4.9M and cache reads 4.96M.
   - **Cache writes:** Writes stay in a tight 466,749–505,630 band per run, even though hit rates range from 0.549 (39729048063) to 0.842 (39730463796). That steady ~49k per call suggests each call writes its own prefix (inference: dynamic content placed early, or per-role prompt variants).
   - **Change:**
     1. Audit how the reviewer, consolidator and editor prompts are assembled.
     2. Keep the static system, rubric and repo-context block byte-identical and first.
     3. Put the PR diff, run IDs, timestamps and iteration counters after it.
   - **Savings:** Each +0.05 in `cache_hit_rate` moves about 3.4M tokens from uncached to read, about a 13% cut in cost-equivalent units for the window.
   - **Quality risk:** None if only the ordering changes.
2. **Avoidable CI reruns.**
   - **Evidence:** 2 failed CI runs × about 14 min of fan-out jobs, with no tokens involved.
   - **Change:** Shift the wiring guard left (Reliability item 1).
   - **Savings:** About 28 runner-minutes per window.
3. **Semble is not adding noise.**
   - **Evidence:** About 15.5 KB per review query, roughly 4k tokens, against about 1.74M uncached prompt tokens per run (about 0.2%). `static_dup_bytes_total` = 0 and `echo_lines_dropped` = 0. Review queries returned 7.6 sources on average (61/8).
   - **Poll:** Orchestrate-poll returned only **1 source per query** (2 queries, 11,806 bytes) after 34.4 s average bootstraps (68,724 ms over 2).
   - **Change:** Check whether poll-side Semble changes judge outcomes. If not, set `SEMBLE_ENABLED=false` for the poll path only.
   - **Savings:** About 34 s per poll cycle that queries Semble.
4. **Serena:** Disabled (`SERENA_ENABLED: false` in 39765265448, 39768883383 and others). All counters are 0. There is nothing to evaluate.
5. **Data gap:** Implement runs of 1,832 s (39722823089) and 1,746 s (39752242564) report 0 Codex and 0 OpenRouter tokens. The heal intake runs on `claude-opus-5-5 effort=high` (39769022575, 39767683973). The `claude_*` fields that `cost_audit.py` parses (`CLAUDE_COUNT_KEYS`) are not in this context, so Claude-engine cost cannot be assessed. **Next step:** include `claude_calls`, `claude_cost_usd` and `claude_models` in the analysis context rows.

There were no `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` lines (both counts 0), so prompt size is not under pressure.

## Reliability Improvements

1. **CI test wiring guard failures (2/2 failures, 4.7% of CI).**
   - **Evidence:** Runs 39746784127 (08:18:41) and 39746749349 (08:18:23), failing at `static-checks / CI test wiring guard` (`ci.yml:230-233`). Review/autofix runs 39746784171 and 39746749571 started at the same timestamps, so these were likely PR-head pushes that added a `tests/test_*` file without wiring it into CI (inference).
   - **Root cause:** A process gap. The guard runs only in CI, after the push.
   - **Fix:** Run `tests/test_ci_wires_every_test_file.py` locally whenever a `tests/test_*` file is added. Put it in the existing pre-push hook (`workflow-templates/.claude/hooks/prepush_affected_tests.py`, tested at `ci.yml:683-687`) and in the implement/autofix post-edit verification. The guard already prints every unwired path (`test_ci_wires_every_test_file.py:60-63`), so no extra logging is needed.
   - **Impact:** Eliminates all observed failures.
   - **Rollback:** Additive and fail-open. A local check failing never blocks CI from running.
2. **Implement Semble runtime fallbacks (2 of 2 attempts).**
   - **Evidence:** One `SEMBLE_FALLBACK` (runtime) each in 39722823089 and 39752242564. `semble_query_calls` = 0 for the implement family.
   - **Diagnosis:** Possibly a masked broken rollout rather than rare fail-open (n=2, low confidence). The 40 `context=contract-test` fallbacks (`cost_audit.py:381-382`), 8 per green CI run and 4 per failed run, are test fixtures and healthy.
   - **Fix:** Surface the per-target `reason=` for runtime fallbacks in the analysis context. If every implement fallback shares one reason, for example the index being unavailable, fix the implement bootstrap wiring.
   - **Rollback:** Fail-open is already in place.
3. **Validate npm cache never saves.**
   - **Evidence:** 39767325210: `Path Validation Error: Path(s) specified in the action for caching do(es) not exist, hence no cache is being saved.`
   - **Fix:** Point the cache `path` at the directory npm actually uses, or drop the step.
   - **Impact:** Small speed gain, and less warning noise.
4. **Reclassify superseded poller runs.**
   - **Evidence:** 22 runs are `cancelled_before_first_step` under a `cancel-in-progress: false` group, for example 39766790005 (346 s queued), 39765991966 (389 s) and 39765456373 (97 s).
   - **Fix:** In `extract_failure_point` / `_extract_cancelled_point`, tag runs that were cancelled before their first step and belong to a concurrency group as `superseded_pending`. This is an additive field; existing fields are unchanged.
   - **Impact:** Removes 22 false "cancelled" signals per window.
5. **Proposed diagnostic-logging additions.** All are additive; existing names are unchanged.
   - **(a) Collector step durations and queue wait.** In `compute_run_metrics`, add `queue_wait_seconds` (`job.started_at − job.created_at`) and `slowest_steps` (top 3 by `completed_at − started_at`). The function already receives `jobs`, so this needs 0 new API calls (§14). Note that `duration_seconds` (`collect_workflow_logs.py:1123-1127`) currently includes queue time.
   - **(b) Poller phase timing.** Emit one compact summary line per phase at the end of the poll step, for example `ORCH_POLL_PHASE_TIMING phase=<name> ms=<n>`, so the timing survives log truncation. The prefix is unused in `scripts/orchestrate_poll*.sh`. Register it through an `agents.d/` fragment under "Stable log prefixes (contractual)" (§25). `orchestrate_poll_process.sh` already has 33 timing call sites (`date +%s`/`SECONDS`) to build on.
   - **(c) `GH_PAT_BUDGET phase=start` where it is missing.** `used_in_job=unknown` appeared in 39769022575 and 39768883383. `gh_helpers.sh:68-72` computes usage only when a previous snapshot with the same reset window exists.

## AI Memory Health

- `AI_MEMORY_TELEMETRY` lines appear in only 2 runs, both `op=finalize-task`:
  - 39768883278: `ok:true, enabled:true, fail_open:true, reason:no_linked_issues`. A healthy noop.
  - 39765517247: `ok:true, did_push:true, push_attempts:1, final_state:merged, issue_number:7872`. There was a gap of about 125 s (10:46:29→10:48:34) before this output, which was 71% of that 175 s run. The cause is not visible; push contention or a runner stall is possible (inference).
- No `retrieve`, `record-candidate`, `promote`, `compact` or `processed-command-*` lines appear in the visible evidence. **Hit rate, `estimated_tokens` against budget, and the `keyword_method` mix cannot be computed.**
- **Recommendations:**
  - Verify that `retrieve` telemetry is emitted in the clarify, plan, implement and review prompt-assembly paths.
  - Have the collector count `AI_MEMORY_TELEMETRY` operations from full logs, as `cost_audit.py` already does for Semble, rather than relying on truncated summaries.
  - Time the step around `finalize-task` in issue-pr-status to explain the 125 s gap.

## GH API Call Audit

- **No rate-limit pressure:** No HTTP 429 or secondary rate-limit events in any summary.
- **Lowest `GH_PAT_BUDGET` remaining:** 3531 (sweep 39767354024, `used_in_job=17`) and 3818 (39765517214, `used_in_job=0`), both in reset window 1792666917. That is about 30% of a 5000 budget used in that window (inference about the budget size).
- **Per-job usage is low where it is measurable:**
  - Heal intake: 39 calls (39767683973).
  - Post-merge dispatch: 1 call (39768874489).
  - Lint: 1 paginated commits call per run (39765523210, 39765265095).
  - Heal reconcile: 1 paginated issue list per PR close (`workflow_failure_heal_pr_reconcile.sh:96`).
- **Blind spot:** The orchestrate poller is the likely largest consumer, but its API usage was not visible because the logs were truncated (39765652831, 39767503891). `PAT_BUDGET_LINE` (`collect_workflow_logs.py:109-113`) can already parse the end-phase line from full logs. **Next step:** aggregate `used_in_job` per family into `analysis_context`.
- **No unbatched loops observed**, but the poll internals cannot be seen. The batched GraphQL helpers named in §14 (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) are where any new poll data needs should go.
- **Gap:** In 2 of the visible budget lines, `used_in_job=unknown`. Fix: Reliability item 5(c).

## Prompt Cache & Memory System

| Run | cache_hit_rate | cache_read | cache_write | uncached prompt | wall (s) |
|---|---|---|---|---|---|
| 39730463796 | 0.842 | 11.16M | 0.503M | 1.59M | 2072 |
| 39746784171 | 0.807 | 9.56M | 0.493M | 1.79M | 2066 |
| 39737521638 | 0.749 | 6.14M | 0.506M | 1.55M | 1678 |
| 39743078516 | 0.729 | 6.27M | 0.491M | 1.84M | 2095 |
| 39733700109 | 0.700 | 6.04M | 0.487M | 2.11M | 2275 |
| 39753918214 | 0.660 | 4.49M | 0.490M | 1.83M | 1764 |
| 39739802391 | 0.618 | 3.66M | 0.486M | 1.78M | 1816 |
| 39729048063 | 0.549 | 2.32M | 0.467M | 1.43M | 2044 |

- **The hit-rate spread comes from cache-read volume (session length), not from misses.** Uncached prompt is roughly flat at 1.43–2.11M per run.
- **Fragmentation signal:** Each run writes about the same ~490k of new cache, which looks like a fixed set of about 10 per-call prefixes being re-created each run (inference). Fix: Cost item 1. Track `or_cache_write_tokens / or_calls` and aim for it to fall below 49k.
- Every call reported usage (`or_usage_unavailable_calls` = 0). There was no `CONTEXT_BUDGET_WARN` pressure.
- Memory-retrieval effectiveness cannot be assessed (see AI Memory Health).

## Orchestrator Health

- **Project #7869:**
  - Readiness warned twice: `1/2 sub-issues on #7869 still unchecked: **report-and-delete-0923**` (39765265010, 39765523217).
  - The sub-PR #7875 (issue #7872) merged, and finalize-task recorded it as `merged`.
  - Validate `[tracking:7869]` 39767325210 succeeded in 1026 s.
  - Integration PR #7874 uses `Refs #7869` and passed the auto-close lint (39765265095), so it complies with §21.
- **No clarification loops:** `orchestrate_clarify_respond` had 140 runs, all skipped, 0 active.
- **Poll cadence:** The cron fires every 5 min, but polls take 665 s at p50 and 1143 s at p95. In practice the cadence is one poll per run time (about 11–19 min), and wave-progression latency can reach about 2 poll durations (inference).
  - **Indicators to track:** poll p50/p95, the share of runs that are superseded pending runs, and per-phase timings once 5(b) exists.
- **Release path is healthy:**
  - Stable patch #7876 (v1.38.1) forward-merged on the first push attempt (39768883697).
  - The promote follower took the expected `frozen protected=true` path (39769181896).
  - Historical triage deduplicated against issue 7300 (39769183572).
- **Heal pipeline is quiet:** 49 heal runs, all skipped. Intake had 11 successes and 52 skips. Recovery scan returned `count=0` (39765926260).

## Pipeline Flow Bottlenecks

| Stage | Family p50 / p95 (s) | Dominant overhead |
|---|---|---|
| clarify / plan / implement / clarify-respond | 1 / 10 each, ~140 runs each | Fan-out: 553 skipped runs (55% of all runs) |
| implement (real work) | 1101–1832 (slow runs) | Compute; token telemetry missing |
| review/autofix | 123 / 2070; full-review wall p50 1721 | Compute (~10 model calls per run); Semble bootstrap ~42 s |
| CI | 845 / 996 | One job's hidden 9–10 min; queue up to 3m42s |
| validate | 777 / 1008 | Compute (hidden by truncation) |
| orchestrate_poll | 665 / 1143 | Compute (hidden), queue up to 3.5 min, superseded pending runs |
| PR close housekeeping | 12 / 139 | Full-history checkout (~70–80 s) |

**Fixes in order of end-to-end impact:**
1. Collector step timings and queue wait (needed to target everything else).
2. Cut runner demand during bursts (heal reconcile checkout, then fan-out measurement).
3. Split the CI job.
4. Improve review prompt-cache prefix stability.
5. Background Semble bootstrap.

**Fan-out check before changing triggers:** Some skipped runs lasted 10–12 s (for example clarify 39767286948 at 12 s, and heal intake 39767868799 at 127 s while skipped), which suggests a gate job sometimes takes a runner (inference). Use `slowest_steps` / `runner_name` from item 5(a) to count them.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** Hosted-runner queueing during merge bursts. Review/autofix runs of 28–38 min. CI's hidden 9–10 min step. Poller at 11–19 min per cycle.
- **Top failure modes:** The CI test wiring guard (2/2 failures). Implement Semble runtime fallbacks (2/2 attempts; low confidence). A validate cache-path warning.
- **Highest-cost drivers:** Review/autofix uncached prompt (13.93M of 68.0M tokens) and constant per-call cache writes (3.92M). Claude-engine implement and heal costs are not in this context.
- **Top 3 actions:**
  1. Add `queue_wait_seconds` and `slowest_steps` to `compute_run_metrics` (0 new API calls).
  2. Make `heal-pr-reconcile` check out shallow and unshallow only when heal issues exist.
  3. Run the wiring guard in the pre-push and implement verification paths when `tests/test_*` files are added.

## Metrics Appendix

**Runs**

| Metric | Value |
|---|---|
| Total runs | 1000 |
| Success / failure / cancelled / other (skipped) | 277 / 2 / 22 / 699 |
| Failure rate (all runs; of conclusive runs) | 0.2%; 0.66% (2/301) |
| p50 / p95 duration | 2 s / 875 s |
| Runs with log telemetry | 115 |
| Success sample rate | 0.07 |

**Tokens and cache (all from review/autofix, 8 runs)**

| Metric | Value |
|---|---|
| or_calls (usage available / unavailable) | 80 (80 / 0) |
| Uncached prompt / completion | 13,928,923 / 516,681 |
| Cache read / cache write | 49,634,574 / 3,923,108 |
| Total | 68,002,440 |
| cache_hit_rate | 0.735473 |
| codex_tokens_used / codex_calls | 0 / 0 |

**Review telemetry**

| Metric | Value |
|---|---|
| wall_clock_p50_ms / p99_ms (all) | 9,500 / 2,092,010 (n=114) |
| wall_clock_p50_ms / p99_ms (review/autofix) | 1,721,000 / 2,251,600 (n=14) |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**Semble and Serena**

| Metric | Value |
|---|---|
| Semble queries / logged bytes / sources | 10 / 135,915 / 63 (review 8 / 124,109 / 61; poll 2 / 11,806 / 2) |
| Semble bootstraps / failed / unused / total ms | 10 / 0 / 0 / 401,439 |
| Semble fallbacks: total / contract_test / runtime | 42 / 40 (CI) / 2 (implement) |
| Semble static_dup_bytes / echo_lines_dropped | 0 / 0 |
| Serena queries / response bytes / tool calls / fallbacks | 0 / 0 / 0 / 0 |

**MCP availability by target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (disabled) | 0 | 0 | 0 |

No per-target Semble probe rows were present. Other MCP servers observed: none.

**GH API (visible `GH_PAT_BUDGET phase=end` lines)**

| Run | Workflow / job | remaining | used_in_job |
|---|---|---|---|
| 39767354024 | review_autofix_sweep / sweep | 3531 | 17 |
| 39765517214 | review_autofix / post-merge-validate-dispatch | 3818 | 0 |
| 39769022575 | heal intake / intake | 4555 | unknown |
| 39768883383 | review_autofix / post-merge-validate-dispatch | 4664 | unknown |
| 39768874489 | review_autofix / post-merge-validate-dispatch | 4820 | 1 |
| 39767683973 | heal intake / intake | 4949 | 39 |

No HTTP 429 or secondary rate-limit events were seen.

**Data gaps**
- The run-logs directory was not mounted.
- Claude-engine cost fields are absent from the context.
- The poll, validate and CI middle sections are truncated in the summaries.
- There is no memory `retrieve` telemetry.
- Only 8 of 49 review/autofix runs carry token telemetry.