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
## Deep Audit — Workflows & Scripts (2026-10-22)

Scope and method: I read the full text of `internal-cancel-on-pr-close.yml`, `cancel_on_pr_close.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `tests/test_workflow_file_size_limit.py` and `internal-orchestrate-poll.yml`. I also read targeted line ranges of `orchestrate_poll.yml`, `test-and-mark-stable.yml`, `update_workflows.yml`, `implement.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `scripts/review_merge_train.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/stage_workflow_support.sh`, `scripts/apply_audit_gate_assets.py` and `docs/scripts-pending-removal.md`. The rest was surveyed by grep. Character counts for run bodies are estimates from line counts, because no shell was available.

Cross-references to the report above:
- The full-history checkout in `heal-pr-reconcile` is already covered by Speed Optimizations item 4 and is not repeated here.
- API-001 and DEBT-001 add more sources of runner demand to Speed Optimizations item 1.

### Section 1: Bug & Correctness Sweep

**BUG-001** — `scripts/orchestrate_poll_process.sh:29213-29252`
- **Severity:** High. **Category:** `bug`.
- **Description:** The judge auto-recovery revert path has four problems.
  1. **The first call always fails.** Lines 29221-29227 `POST repos/${GITHUB_REPOSITORY}/pulls` with `head="revert-${PR_TO_REVERT}-$(date +%s)"`. That branch is only created later, in the fallback (29232-29235), so the POST has no head branch and always fails. Each revert wastes one write call and always drops into the fallback.
  2. **A git failure may abort the poller.** The fallback runs inside `echo … | jq … | while read -r revert_issue` (29215). That is a pipeline subshell under `set -euo pipefail` (line 21). The unguarded `git checkout -b` (29233) and `git push -u origin` (29235) sit in the `{ … }` group after the final `||`, where errexit still applies. A failure there can end the loop for the remaining issues, and through `pipefail` may abort the poll tick. [NEEDS VERIFICATION: errexit is active in the enclosing function.]
  3. **No input check.** `revert_issue` comes from model output (`.issues_to_revert[]`) and is passed to `_issue_cross_ref_pr_number_last` without the `^[0-9]+$` check used elsewhere (for example at 4747 and 7925).
  4. **Possibly the wrong branch (inference).** Reverts target `DEFAULT_BRANCH`. Orchestrator sub-issue PRs merge into integration branches, so `git revert` of their `merge_commit_sha` against the default branch may fail every time.
- **Recommended fix:**
  - Delete the REST POST and keep only the git path.
  - Validate `revert_issue` against `^[1-9][0-9]*$`.
  - Iterate with `done < <(jq -r '.issues_to_revert[]?' <<< "${JUDGE_JSON}")`, so the loop runs in the parent shell.
  - Wrap each git operation as `if ! git …; then echo "::warning::…"; git checkout "${DEFAULT_BRANCH}" || true; continue; fi`.
  - Resolve the base from the project's integration branch in `STATE_FILE` before falling back to `DEFAULT_BRANCH`.
  - Run `scripts/lint_pr_body_auto_close.py` on the body before `gh pr create` (see SEC-001).

**SEC-001** — `scripts/orchestrate_poll_process.sh:29225-29227, 29240-29242` (also 9979-9984 and 27326-27333)
- **Severity:** Medium. **Category:** `security`.
- **Description:** §21 says every unattended phase must run `scripts/lint_pr_body_auto_close.py` on the composed body before `gh pr create`. This script never does: grep finds the name only in a comment at 18685, and nowhere before the three `gh pr create` sites.
  - The revert PR body contains model-written `${JUDGE_JUSTIFICATION}` and targets `DEFAULT_BRANCH`. A justification that quotes "Fixes #<tracking>" would close the tracking issue on merge.
  - `lint-pr-body-auto-close.yml:27-35` is only a check that runs after the PR exists (branches `[main, stable]`). It does not stop the PR from being created.
- **Recommended fix:**
  - Write the body to a temp file and run `PYTHONDONTWRITEBYTECODE=1 python3 scripts/lint_pr_body_auto_close.py` on it before each of the three `gh pr create` calls. On a non-zero exit, skip creation and post a tracking comment. The implement pre-flight step (`implement.yml:5835`, "Pre-flight — lint PR title/body…") is the pattern to mirror.
  - Strip `#<digits>` references from `JUDGE_JUSTIFICATION` before using it in the body.

**BUG-002** — `.github/workflows/orchestrate_poll.yml:301-306`
- **Severity:** Medium. **Category:** `bug`.
- **Description:** The tracking-issue listing uses `gh issue list … --label "ai:orchestrator-tracking" --state open --limit 20` and does not check whether the result was truncated. With more than 20 open tracking issues, the extras are never polled, and nothing is logged. `COUNT` (308) only reports what was returned. [NEEDS VERIFICATION: whether more than 20 concurrent projects is reachable under `.github/ai/concurrency_caps.yml`.]
- **Recommended fix:** Raise the limit to the 1000 used elsewhere in the poller (for example `orchestrate_poll_process.sh:47, 163`). Also emit `::warning::` when `COUNT` equals the limit.

**BUG-003** — `.github/workflows/orchestrate_poll.yml:513-555` (first resolution at 118-159)
- **Severity:** Low. **Category:** `bug`.
- **Description:** The same job resolves `SCRIPT_REF` twice. When pinned to `promote-candidate` (`internal-orchestrate-poll.yml:18`), each resolution reads the branch tip separately (137 and 533). If the branch moves between the two reads, `gh_helpers.sh` and `review_merge_train.sh` (staged at 208-237) come from commit A, while every other support file (605-897) comes from commit B. That is a TOCTOU mix of support code in one job.
- **Recommended fix:** In the second step, reuse the `SCRIPT_REF` already written to `GITHUB_ENV` at 146/158 (`if [ -n "${SCRIPT_REF:-}" ]; then echo "SUPPORT_REF reuse=${SCRIPT_REF}"; exit 0; fi`). Keep the re-checkout steps (557-603), since the repository checkout at 419 clears the workspace. Saves one API call per tick (API-002).

**BUG-004** — `.github/workflows/test-and-mark-stable.yml:1086-1117`; `scripts/comprehensive_test_and_release_gh_api.sh:151-154`
- **Severity:** Low. **Category:** `bug`.
- **Description:** On failure, `gh_api_safe` echoes `::error::gh api call failed: $*` to **stdout** (153), and `gh_api_safe_print` is called inside `$(...)`. A failed comments read therefore makes `COMMENTS_JSON` `"::error::…[]"`. Then `jq 'length'` fails, `COMMENT_COUNT=0`, `CUR_STATE` changes and `LAST_ACTIVITY` resets (1110-1117).
  - If reads alternate between success and failure, the inactivity timeout (`PLAN_PHASE_TIMEOUT`) never fires. Only the job's `timeout-minutes` bounds the step.
  - The workflow comment at 1000-1002 acknowledges the stdout echo.
- **Recommended fix:** Send the `::error::` line to stderr (`>&2`), as `_safe_gh_jq` does by contract (`gh_helpers.sh:1224-1255`). Alternatively, gate `COMMENTS_JSON` with `jq -e 'type == "array"'` and keep `PREV_STATE` unchanged on a failed read.

**SEC-002** — `.github/workflows/update_workflows.yml:1788-1806, 875`
- **Severity:** Low. **Category:** `security`.
- **Description:** Step outputs and repository variables are spliced directly into shell source:
  - `AUDIT_GATE_MESSAGE="${{ steps.audit_gate.outputs.message }}"` (1804)
  - `FAILURE_REASON_DETAIL="${{ steps.update.outputs.failure_reason_detail }}"` (1806)
  - `SELECTED_PROFILE="${{ vars.WORKFLOW_PROFILE … }}"` (875)

  `failure_reason_detail` embeds `${SELECTED_PROFILE}` and filesystem paths (`fail_with_reason` at 898-909, callers at 911-982). Only CR/LF are stripped (901-902). A `"`, backtick or `$(` in the profile name or in a path would break the step or execute. The inputs come from repository admins, so the risk is low. The pattern is still the expression-injection shape GitHub's hardening guide forbids.
- **Recommended fix:** Move these values into the step's `env:` block. The same step already does this for `VERIFY_OUTCOME`/`COMMIT_*` (1780-1784):

  ```yaml
  env:
    AUDIT_GATE_MESSAGE: ${{ steps.audit_gate.outputs.message }}
    FAILURE_REASON_DETAIL: ${{ steps.update.outputs.failure_reason_detail }}
  ```

  Then delete the shell assignments.

**DEAD-001** — `.github/workflows/test-and-mark-stable.yml:1082-1084, 1413-1416`
- **Severity:** Low. **Category:** `dead-code`.
- **Description:** `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]` can never be true. `RATE_LIMIT_BACKOFF` is only updated inside `gh_api_safe` (`comprehensive_test_and_release_gh_api.sh:109-132`), which runs in the `$(gh_api_safe_print …)` subshell, so the parent's value stays at its initial `0` (969, 1283). The Phase 2 branch also `continue`s without `sleep`, so it would spin if it ever became reachable.
- **Recommended fix:** Delete both guards. `gh_api_safe` already backs off internally. If a parent-visible signal is needed, call `gh_api_safe` (not `_print`) in the parent shell and read `GH_API_SAFE_OUTPUT`.

No unquoted-variable or missing-`set -e` defects were found in `workflow_failure_heal_pr_reconcile.sh`. It deliberately uses `set -uo pipefail` and every exit is 0, as its header contract states at lines 40-43. Every untrusted `github.event.*` text field I found (titles, bodies, comment bodies, head refs) goes through `env:`; grep found no direct use in any `run:` body.

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001** — `scripts/orchestrate_poll_process.sh:6984-7000, 27863-27879, 27723-27735`
- **Severity:** Medium. **Category:** `api-batching`.
- **Description:** These are per-iteration REST calls inside loops (review-blockers per §15):
  - Both post-Codex blocker loops call `gh_retry _safe_gh_jq "repos/…/issues/${blocker_issue}" --jq '.state'` once per blocker.
  - After review-blocked handling, the wave re-check calls `_safe_gh_jq "repos/…/issues/${inum}/labels"` once per wave issue (27728), with no cache lookup. The reissued loop at 27740-27748 does check `LABELS_JSON` first.
  - **Current:** N blockers → N calls (twice per tick when both paths run). M wave issues → M calls.
  - **After fix:** ceil(N/25) and ceil(M/25) GraphQL calls.
- **Recommended fix:** Add `_fetch_issue_state_labels_graphql <numbers_json> <out_file>`, modelled on `_fetch_blocked_issue_routing_graphql` (115-142). Use aliases `i${n}: issue(number: ${n}) { number state labels(first: 100) { nodes { name } } }`, batches of 25 and the same fail-open return 1. On helper failure, fall back to the current per-issue REST read. Document the batching contract as at 102-114.

**API-001** — `.github/workflows/cancel_on_pr_close.yml:391-427`; `.github/workflows/internal-cancel-on-pr-close.yml:6-7`; `.github/workflows/orchestrate_poll.yml:239-266`
- **Severity:** Medium. **Category:** `api-redundancy`.
- **Description:** The global merge-train `release` scan runs on two independent 5-minute crons:
  - the poller step "Release merge-queued PRs (merge train backstop)";
  - the scheduled `cancel` job. Its log line at 409-410 reads "Scheduled cleanup is running the global merge-train release scan".

  Each scan makes at least one paginated open-PR listing (`review_merge_train.sh:1343`). When queued PRs exist, it also lists active runs (1360) and, per queued PR, files and comments. The `_MT_FILES_CACHE` (215) lives per process, so the two scans cannot share it.

  The scheduled cancel tick also makes:
  - 2 paginated `actions/runs` listings (167-186);
  - ceil(N/50) GraphQL calls (223);
  - up to 3 support checkouts (69-115).

  That is 288 extra jobs per day, which feeds the burst runner saturation in Speed item 1.
  - **Current:** ≥2 release scans per 5 min.
  - **After fix:** 1.
- **Recommended fix:** Add `if: github.event_name != 'schedule'` (or an `EVENT_NAME` check) to the release step in `cancel_on_pr_close.yml`, but only where the full-profile poller is present. Line 394 says core-profile repos have no poll backstop, so gate on a profile variable rather than removing the step outright. Separately, consider dropping the cancel schedule to `*/15`. [NEEDS VERIFICATION: which consumer profiles rely on the scheduled release.]

**API-002** — `.github/workflows/orchestrate_poll.yml:137, 533`
- **Severity:** Low. **Category:** `api-redundancy`.
- **Description:** `GET repos/shubhodeep1/coding-workflows/branches/promote-candidate` runs twice per poll tick in this repository, because the wrapper pins `@promote-candidate`.
  - **Current:** 2 calls per tick, about 576 per day.
  - **After fix:** 1 call per tick, about 288 per day.
  - The same duplicated resolution block appears 4× in `implement.yml` and 2× each in `plan.yml`, `clarify.yml` and `orchestrate.yml` (grep count of `promote_candidate_support_ref="${{ github.sha }}"`). [NEEDS VERIFICATION: whether each pair is in the same job.]
- **Recommended fix:** Same as BUG-003. Reuse `SCRIPT_REF` from `GITHUB_ENV` when it is already set.

**API-003** — `scripts/orchestrate_poll_process.sh:42-58, 151-169, 210-211`
- **Severity:** Low. **Category:** `api-redundancy`.
- **Description:** Every cron tick runs `replay_failed_reclarify_commands` and then `detect_unrouted_blocked_comments` (210-211). Between them:
  - 2 `gh api rate_limit` reads (44, 157);
  - 2 `gh issue list` calls for different labels (46, 162);
  - per queued reclarify issue, 1 issue GET (55) plus a paginated comments read (58).

  The rate_limit endpoint does not use core quota, but the reads still add latency.
  - **Current:** 4 + 2Q calls per tick.
  - **After fix:** 2 + 2Q, by sharing one rate-limit read and one GraphQL listing with two label aliases.
  - The full comment history per queued issue is needed (line 59: "Incomplete or malformed comment history never authorizes replay"), so the per-issue comments read should stay. [NEEDS VERIFICATION]
- **Recommended fix:** Read `rate_limit` once in the caller, pass it to both functions, and fold the two listings into one aliased GraphQL query (`issues(labels: [...], states: OPEN, first: 100)` ×2).

**API-004** — `.github/workflows/test-and-mark-stable.yml:1409-1452`
- **Severity:** Low. **Category:** `api-batching`.
- **Description:** Each Phase 3b poll iteration makes up to 5 REST calls, every `POLL_INTERVAL`, for the whole implement phase:
  - open PRs by head (1409)
  - issue labels (1429)
  - runs list (1433)
  - run jobs (1446)
  - head commit (1451)
- **Recommended fix:** Fetch PR-by-head, issue labels and the branch head OID in one GraphQL query (`repository { issue(number:) { labels } ref(qualifiedName:) { target { oid } } pullRequests(headRefName:, states: OPEN, first: 1) { nodes { number } } }`). Keep the runs and jobs REST calls. Per iteration this goes from 5 calls to 3.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `scripts/orchestrate_poll_process.sh:6984-7000` vs `27863-27879`
- **Severity:** Medium. **Category:** `duplication`.
- **Description:** Two near-identical blocker-state loops. They share the same `case open|closed|*` arms and only rename the counters (`blocker_open_count` vs `IF_BLOCKER_OPEN_COUNT`).
- **Recommended fix:** Add one helper in `orchestrate_poll_process.sh`, backed by the BATCH-001 GraphQL helper, and update both callers:

  ```bash
  _blocker_issue_states_into()
  {
  	# $1 blockers_json  $2 open_count_var  $3 unknown_count_var  $4 summary_var
  	:
  }
  ```

**DUP-002** — `.github/workflows/orchestrate_poll.yml:613-895` (and the same idiom in `implement.yml`, `orchestrate.yml`, `clarify.yml`, `plan.yml`, `orchestrate_clarify_respond.yml`)
- **Severity:** Medium. **Category:** `duplication`.
- **Description:** The "use `.codex-workflow-src`, else `.codex-workflow-src-main`, else error/notice" staging block is copied about 12 times in this one step, and appears 23 times across 6 workflows (grep for the `.codex-workflow-src-main/scripts/${f}" ]; then` fallback). `scripts/stage_workflow_support.sh` already provides `stage_required_entry` and `stage_optional_preserve_entry` (678-729). `review_autofix.yml:2115` and `validate.yml:711` (`stage_workflow_support.sh validate --manifest`) use it; the other six workflows do not.
- **Recommended fix:** Add a `poll` mode (and later `implement`/`plan`/`clarify`) to `stage_workflow_support.sh`, with a manifest that lists required, optional and asset entries. Replace the inline loops with one `bash .codex-workflow-src/scripts/stage_workflow_support.sh poll --manifest …` call, and update the `REQUIRED_BOOTSTRAP_SCRIPTS`-style test registry. This also removes EXPR-003's growth risk.

**DUP-003** — "Resolve workflow support ref" steps: 16 copies across 9 workflows; inline `gh_api_retry` fallback shims: 26 copies across 13 workflows
- **Severity:** Low. **Category:** `duplication`.
- **Description:** These are security-sensitive (support-code pinning), byte-similar blocks that differ only in the workflow-file regex. Examples: `cancel_on_pr_close.yml:25-67` and `orchestrate_poll.yml:118-159` / `513-555`. Behaviour is pinned by `tests/test_workflow_support_ref_follows_pin.py` (for example 71-120 and 366-406), which limits the drift risk. The step has to run before support code is checked out, so it cannot simply move into `scripts/`.
- **Recommended fix:** Add a structural test that pulls every "Resolve workflow support ref*" `run:` body, swaps the workflow filename for a placeholder, and checks all copies are identical. If consolidation is wanted, use a composite action referenced by commit, as `test_shared_actions_are_referenced_by_commit` (199-214) already requires for shared actions.

**DUP-004** — `scripts/gh_helpers.sh:1060-1138, 1149-1222, 1271-1335`
- **Severity:** Low. **Category:** `duplication`.
- **Description:** `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` repeat the same parts: the attempt loop, the `_is_gh_rate_limit` → `_gh_ratelimit_tg_alert` / `_gh_rate_limit_trip_breaker` / `_gh_rate_limit_wait_for` chain, the `2 ** (attempt - 1)` backoff and the unsafe-POST single-attempt rule.
- **Recommended fix:** Extract `_gh_retry_core <stdout|file|json-file> <outfile|-> <cmd…>` and turn the three public functions into thin wrappers. Keep the public names unchanged (§10). Leave `comprehensive_test_and_release_gh_api.sh:gh_api_safe` separate; it is deliberately self-contained (93-96).

**DUP-005** — `.github/workflows/test-and-mark-stable.yml:1017-1026, 1050-1072` vs `1365-1401`
- **Severity:** Low. **Category:** `duplication`.
- **Description:** Phase 2 and Phase 3b each define their own `capture_run_id` (5-attempt retry, break on exit code 2) and `require_*_run_id` functions. Both steps already source `scripts/comprehensive_test_and_release_gh_api.sh` (977, 1296).
- **Recommended fix:** Move them into that script as `capture_scoped_run_id <repo> <created_after> <name_re> <title> <max_pages>` and `require_scoped_run_id <id> <phase>`. This also cuts both run bodies (EXPR-001, EXPR-002).

### Section 4: Expression Size Limit Risk Assessment

The repo already enforces this limit:
- `tests/test_workflow_file_size_limit.py:28-32, 66-98` fails CI for any `.github/workflows/*.yml` `run:` body that contains `${{` and reaches 18,000 characters.
- While CI is green, no block can be in the ≥18,000 "High" band.

The findings below are bodies estimated in the 15,000-18,000 Medium band. The estimates are line counts times average content width with YAML indentation removed [NEEDS VERIFICATION: exact `len(run)` from the test harness].

**EXPR-001** — `.github/workflows/test-and-mark-stable.yml:962-1229` ("Phase 2: Wait for plan to complete")
- **Severity:** Medium. **Category:** `expression-limit`.
- **Description:** About 267 body lines, estimated at 15,000-17,000 characters, leaving about 4,000-6,000 of headroom to 21,000. It contains `${{ steps.create-issue.outputs.created_after }}` three times (980, 992, 1010), which makes the whole body one expression. It has grown with each incident fix (#4723, #4730 comments).
- **Recommended fix:** Add `PLAN_CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step `env:` and use `${PLAN_CREATED_AFTER}` in the body. This is the same "Promoted to env so the run: script body contains no ${{ }}" pattern used at 1851 and 2683. The body then has no `${{` and is exempt. Then apply DUP-005.

**EXPR-002** — `.github/workflows/test-and-mark-stable.yml:1269-1529` ("Phase 3b: Wait for PR creation")
- **Severity:** Medium. **Category:** `expression-limit`.
- **Description:** About 260 body lines with several jq lines of 150-200 characters (1326-1334, 1435-1438). Estimated at 16,000-18,000 characters, leaving about 3,000-5,000 of headroom. A single interpolation at 1307 (`${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`) makes the whole body an expression.
- **Recommended fix:** Add `IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` to `env:` and delete line 1307.

**EXPR-003** — `.github/workflows/orchestrate_poll.yml:608-897` ("Stage workflow support files")
- **Severity:** Medium. **Category:** `expression-limit`.
- **Description:** About 289 body lines, including three list lines of roughly 600-1,000+ characters (614, 736, 780). Estimated at 15,000-16,500 characters, leaving about 4,500-6,000 of headroom. The interpolations are at 753, 754 and 875. The 614 list grows every time a support script is added, so this body grows structurally.
- **Recommended fix:**
  - Move the two `vars.*` values to `env:` as `STAGE_PROMPT_PRELUDE_REFACTOR_ENABLED` / `STAGE_UNATTENDED_IDENTITY_REINJECT_ENABLED`. `implement.yml:1420-1421` already uses exactly these names.
  - Replace `${{ github.repository }}` at 875 with `${GITHUB_REPOSITORY}`.
  - Longer term, apply DUP-002.

**EXPR-004** — `tests/test_workflow_file_size_limit.py:35-36, 66-98`
- **Severity:** Low. **Category:** `expression-limit`.
- **Description:** The guard scans only `.github/workflows/*.y*ml` job `steps[].run`. It skips:
  - composite actions (`.github/actions/*/action.yml` `runs.steps[].run`; all currently ≤166 lines, so no current risk);
  - large `if:` and `with:` expressions.
- **Recommended fix:** Extend `_workflow_files()` iteration to `.github/actions/*/action.yml` (`runs.steps`). Add a check that any single `${{ … }}` in `if:` or `with:` values stays under 18,000.

**File size:** The size limit that matters here is GitHub's 512,000-byte workflow file limit (`CLAUDE.md` §27; `agents.md` "Workflow file size limit"), not 1 MB. The CI guard fails at 480,000 bytes and `ci_budget_watch.py` warns at 440,000. No workflow can be over 800 KB while CI passes. The largest by line count are:
- `test-and-mark-stable.yml`: 7,776 lines
- `implement.yml`: 7,061 lines
- `review_autofix.yml`: 5,749 lines

Byte sizes were not measured here [NEEDS VERIFICATION: `wc -c` on these three against the 440,000-byte warning].

### Section 5: Cross-Cutting Concerns

**DEAD-002** — `docs/scripts-pending-removal.md:130-139`; `README.md:4055-4056`; `agents.md:546-555, 689-691`; `tests/test_workflow_checkout_integration_ref_audit.py:66`; `scripts/propagate_consumer_secrets.sh`; `ci.yml:1959`
- **Severity:** Medium. **Category:** `dead-code`.
- **Description:** `.github/workflows/propagate-consumer-secrets.yml` is not in `.github/workflows/`; the glob over the directory does not return it. It is still documented as a "supervisor" with removal trigger "permanent — review annually", is linked from README and agents.md, and is allow-listed in the checkout audit test.
  - The script and its 18-test suite (CI step at `ci.yml:1959`) remain, but nothing schedules the script.
  - This breaks §20.B/§20.C wiring: newly registered consumers in `.github/ai/consumer_repos.json` would not get secrets (inference).
  - [NEEDS VERIFICATION: whether the removal was intentional.]
- **Recommended fix:** Either restore the workflow (push on `.github/ai/consumer_repos.json` plus the weekly schedule described in the registry), or, in one PR, remove the script, its test and `ci.yml` step, the registry entry, the README/agents.md references (through an `agents.d/` correction fragment where required) and the audit-test dict entry.

**DEBT-001** — `.github/workflows/pr6576-shard-followup.yml:14-22, 40-64`
- **Severity:** Low. **Category:** `tech-debt`.
- **Description:** This single-use workflow (registry `docs/scripts-pending-removal.md:320-328`) still triggers on every push to `main`. Its `self-test` job installs pytest and runs a test suite each time, using a runner during the merge bursts described in Speed item 1. The marker comment makes `locate` a no-op, but `self-test` still runs.
- **Recommended fix:** Run the three registry preflight checks (`:326-328`). If they pass, delete the workflow, `scripts/pr6576_shard_followup.py`, the test and the registry entry in one PR.

**CONSIST-001** — `cancel_on_pr_close.yml:33-34`; `orchestrate_poll.yml:125-126, 288, 302, 521-522, 875`
- **Severity:** Low. **Category:** `consistency`.
- **Description:** These lines put `${{ github.repository }}` and `${{ github.sha }}` inline in shell, while sibling steps use `${GITHUB_REPOSITORY}`/`${REPOSITORY}` from `env:`. The values are safe, but every inline use turns the whole body into an expression and counts toward EXPR-00x.
- **Recommended fix:** Use `${GITHUB_REPOSITORY}` and `${GITHUB_SHA}`, which the runner always sets.

**CONSIST-002** — `.github/workflows/test-and-mark-stable.yml:1143-1144, 1498-1499`
- **Severity:** Low. **Category:** `consistency`.
- **Description:** Raw `gh api … 2>/dev/null || echo ""` sits next to `gh_api_safe_print` calls in the same loops. Those two reads get no transient or rate-limit retry, and on failure would print the error JSON to stdout, the exact case `_safe_gh_jq` documents at `gh_helpers.sh:1225-1236`.
- **Recommended fix:** Use `gh_api_safe_print … || echo ""`, consistent with 1078 and 1409.

**SHELL-001** — `scripts/orchestrate_poll_process.sh:25033, 27723, 27740, 28138`
- **Severity:** Low. **Category:** `shellcheck`.
- **Description:** These are SC2086 word-split loops: `for pw_inum in ${PRIOR_NON_TERMINAL}`, `for inum in ${ISSUE_NUMS}`, `for rnum in ${REISSUED_NUMS}`. The values come from jq and are numeric in practice. Glob expansion is still possible if jq ever emits `*`.
- **Recommended fix:** Use `mapfile -t _nums < <(…)` and `for inum in "${_nums[@]}"`, as `orchestrate_poll_process.sh:121` already does.

**SHELL-002** — `scripts/orchestrate_poll_process.sh:27721-27736`
- **Severity:** Low. **Category:** `shellcheck`.
- **Description:** `LABELS_JSON` is built by string concatenation (`LABELS_JSON+="\"${inum}\":${LABELS}"`) with no empty-value guard. The very next loop guards with `[ -z "${LABELS}" ] && LABELS='[]'` and merges through `jq` (27746-27747). An empty `LABELS` produces invalid JSON for `check-wave-status`.
- **Recommended fix:** Build it with `jq -c --arg key … --argjson labels … '. + {($key): $labels}'`, as at 27747. This is superseded once BATCH-001 lands.

TODO/FIXME/HACK markers: grep found none in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 10 | SEC-001, BUG-002, BATCH-001, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-003, DEAD-002 |
| Low | 16 | BUG-003, BUG-004, SEC-002, DEAD-001, API-002, API-003, API-004, DUP-003, DUP-004, DUP-005, EXPR-004, DEBT-001, CONSIST-001, CONSIST-002, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/test-and-mark-stable.yml` | Medium |
| Code modularization | `scripts/stage_workflow_support.sh`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate.yml`, `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/orchestrate_clarify_respond.yml`, `scripts/gh_helpers.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/orchestrate_poll_process.sh` | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/orchestrate_poll.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `.github/workflows/update_workflows.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/test-and-mark-stable.yml`, `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/pr6576-shard-followup.yml`, `docs/scripts-pending-removal.md`, `README.md`, `agents.d/` fragment | Medium |