## Executive Summary

**Coverage note:** the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted for this run, so no `errors/`, `slow/` or `recent/` excerpts could be read. All findings come from `analysis_context.json` (metrics plus `log_summary` fields) and from reading the repo's workflow and script sources. The window is 1,000 runs (the collector cap) covering about 10 hours, from 2026-10-14T14:18Z to 2026-10-15T00:30Z, for one repo. `insufficient_data=false`.

- **Two review/autofix runs seem to review the same PR at the same time, possibly doubling LLM spend on those PRs.** About 10 pairs of review/autofix runs start 12–15s apart: one pull_request run (unlabeled) and one `workflow_dispatch` run (`[pr:N]`). Both run the full reviewer and editor cycle. In the two pairs that logged token data, the Semble query bytes are identical within each pair, which points to the same diff: 38618340834 and 38618374661 both logged 17,139 bytes, and 38602731407 and 38602766010 both logged 16,675 bytes. Those two pairs used 4.57M and 9.02M tokens. The probable cause: `internal-review.yml:50` puts pull_request runs and dispatch runs in different concurrency groups, and the "terminal same-head skip" (`review_autofix.yml:597`) only covers dispatch runs whose head has already reached a terminal state. **Impact (inference): about 5.9–7.7M of the 18.0M logged OpenRouter tokens (33–43%) could be avoided, plus 1–1.8 h of runner time per pair. Confidence: medium.** It needs a head-SHA check, so I propose a `REVIEW_RUN_ORIGIN` log line below.
- **The orchestrator poller runs back to back; its "runner wait" is really concurrency queueing.** Each poll's start time matches the previous poll's end time to within 2–6s. For example, 38654243861 started at 23:35:59, and 38653979130 ended at about 23:35:53. Polls actually start every 8.2–13.3 minutes, not every 5 as the cron schedule says (`internal-orchestrate-poll.yml:5`). 16 of 68 poll runs (23.5%) were waiting runs that a newer run replaced. Run 38655264748 waited 519s and was then cancelled before its first step. **Impact: every minute cut from the 8–12.5 min poll step shortens orchestrator reaction time by the same minute. Confidence: high.** No timing per poll phase exists to show where the time goes.
- **All 3 validate runs for tracking #7355 failed, and their logs don't say why.** Runs 38646893482, 38649799512 and 38652432980 all failed at `Enforce validation outcome`, using 1,591s of runner time in total. `Collect validation status` writes `status`, `raw_status` and `failure_summary` only to `$GITHUB_OUTPUT` (`validate.yml:1334-1341`). The enforce step prints only "Validation did not pass." (`validate.yml:1600`). Log analysis therefore can't tell a harness error from an application failure. **Impact: high value for diagnosis, at near-zero risk. Confidence: high.**
- **CI test jobs are at their 20-minute limit.** Three CI runs were cancelled at 1,225–1,259s total duration while inside 20-minute jobs (`ci.yml:1046`, `ci.yml:1397`). Two of them stopped at the same step, `Workflow failure heal contract and driver tests`, and one of those was a rerun (38602731355, attempt 2). CI p50 is 924s and p95 is 1,396s. **Inference:** these are timeouts, since `ci.yml` has no workflow-level concurrency. **Confidence: medium.**
- **Several kinds of telemetry are missing from this analysis.**
  - Claude usage is absent from the per-run and per-family cost fields, because `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS` (`cost_audit.py:162-232`) contain no `claude_*` keys.
  - 7 of the 12 review/autofix runs with log telemetry show `or_calls=0`.
  - Plan run 38658009649 reports `codex_calls=4` with `codex_tokens_used=2`, which looks like a parser error.
  - No `AI_MEMORY_TELEMETRY` lines are visible anywhere: the poll step logs lose about 810k characters each to truncation, plan loses 484k and clarify 146k.

  **Confidence: high.**
- **65.6% of runs (656 of 1,000) are skipped fan-out runs.** Each event appears to start 4 workflows at once (clarify, plan, implement, orchestrate_clarify_respond; e.g. runs 38660244566, 38660244572, 38660244623 and 38660244614 at 00:28:44). They use almost no compute, but they shrink the collector's 1,000-run window to about 10 hours. **Confidence: high for the count; the effect on the window is an inference.**

## Speed Optimizations

Ranked by expected latency reduction.

1. **Find out where the poller's 8–12.5 minutes go.** Critical path.
   - **Evidence:** poll steps ran 23:27:51–23:35:53 (38653979130), 23:36:01–23:46:32 (38654243861), 23:57:21–00:10:31 (38656928712) and 00:10:38–00:23:16 (38658223057). orchestrate_poll p50 is 696.5s and p95 is 1,110.65s. The following poll waits in the concurrency group (`orchestrate_poll.yml:68-73`, `cancel-in-progress: false`) for exactly that long.
   - **Root cause:** unknown. No phase timings exist, and the poll step's log is cut by about 810k characters in every run.
   - **Change:** add one timing line per top-level phase of the poll step, and tackle the slowest phase first.
     ```bash
     # in scripts/orchestrate_poll_process.sh, around each top-level phase
     _phase_t0=$(date +%s%3N)
     # ... phase body ...
     echo "ORCH_POLL_PHASE name=<phase> ms=$(( $(date +%s%3N) - _phase_t0 )) items=<n>"
     ```
     Register the new prefix in an `agents.d/<issue>-orch-poll-phase.md` fragment under "Stable log prefixes (contractual)" (§25).
   - **Estimated savings:** unknown until measured. Halving the dominant phase would cut cadence from about 11 minutes to about 6.
   - **Risk:** low; the change only adds logging.
2. **Stop duplicate review/autofix runs on the same head.** Critical path for PR throughput.
   - **Evidence:** about 10 pairs in `slow_runs`, for example 38618340834 (6,413s) with 38618374661 (5,310s), and 38602731407 (5,479s) with 38602766010 (4,290s).
   - **Root cause (inference):** a pull_request synchronize run and a dispatch run go to separate concurrency groups (`internal-review.yml:50`).
   - **Change:** first add the diagnostic line below. Once it confirms the same `head_sha`, extend the existing same-head skip (`review_autofix.yml:597`) so a dispatch run is also skipped when an in-flight pull_request run already has the same head SHA. Keep the skip fail-open: if the lookup fails, run the review.
     ```bash
     echo "REVIEW_RUN_ORIGIN pr=${PR_NUMBER} event=${GITHUB_EVENT_NAME} head_sha=${PR_HEAD_SHA:-unknown} run_attempt=${GITHUB_RUN_ATTEMPT}"
     ```
   - **Estimated savings:** one 15–100 min run per pair, and less runner contention.
   - **Risk:** medium. The comment at `internal-review.yml:40-43` says autofix continuation dispatch must not be lost, so only skip when the other run is in flight on the same SHA.
3. **Fix CI jobs that are at their timeout.**
   - **Evidence:** 38599514093 (1,259s) and 38602731355 (1,228s, attempt 2) stopped at `Workflow failure heal contract and driver tests`. 38615507907 (1,225s) stopped at `AI label sync dry-run smoke`. The longest successful CI run took 1,531s (38579723870).
   - **Change:** add `--durations=15` to the pytest calls in those steps and in `Single-issue security pass tests (port P1)` (`ci.yml:661`). Then move the slowest tests to another job, following the #4707 job-split pattern described in `ci.yml:23-37`. Do not raise the timeout.
   - **Estimated savings:** removes timeout reruns (each about 20 minutes).
   - **Risk:** low.
4. **Probe the Claude pool in parallel.** Micro-optimization.
   - **Evidence:** the unblock-judge run 38660357610 spent about 9s probing 4 accounts one after another (00:30:29–00:30:38), while the judge itself ran about 1.3s and returned `outcome=skip reason=fixup_escalated`.
   - **Change:** probe the accounts in parallel. **Savings:** about 6s per run. **Risk:** low.
5. **Sandbox image pull of about 18s** in run 38656952345. This is a micro-optimization. Keep the existing `publish_sandbox_images` pre-seed and do nothing further for now.

## Cost Optimizations

1. **Remove duplicate same-head reviews.**
   - **Evidence:** the two pairs with identical Semble bytes used 2,137,740 + 2,436,590 and 3,761,552 + 5,255,468 OpenRouter tokens.
   - **Estimated savings:** at least 5.9M tokens across just these two pairs (the smaller run of each pair), which is 33% of all logged OpenRouter tokens.
   - **Change:** as in Speed item 2.
   - **Quality risk:** none if the head SHA really is the same. The surviving run still reviews the full diff.
2. **Surface Claude spend in the analysis.**
   - **Evidence:** the unblock judge (run 38656952345) used `claude-opus-5-5` for `total_cost_usd 0.5262172`, while `UNBLOCK_JUDGE_MODEL: openai/gpt-6-sol` was configured. 7 slow review/autofix runs report `log_parsed=true` but `or_calls=0` (for example 38579386101, 38625077273, 38586070354, 38630765165 and 38591814227). Their spend is likely Claude, which this analysis can't see.
   - **Change:** add `CLAUDE_COUNT_KEYS` and `claude_cost_usd` to `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS` in `cost_audit.py`. This is additive and renames nothing.
   - **Estimated savings:** none directly; it unblocks cost decisions for about 58% of review runs.
3. **Close the cache-hit gap.**
   - **Evidence:** hit rate per run ranges from 0.595 (38625109890: 10 calls, 1,373,316 uncached prompt tokens, 71,422 completion tokens) to 0.859 (38602766010). Cache writes total 1,701,625 tokens, 9.4% of the total.
   - **Change:** log one usage line per call (role, prompt, cached and written tokens) so it's clear which reviewer role misses the cache. If the 0.595 runs reached the 0.707 mean, about 0.5M uncached prompt tokens would be saved per run.
   - **Quality risk:** none.
4. **Semble is not adding noise.**
   - **Evidence:** 5 queries, 81,215 bytes (about 16 KB, or about 4k tokens, per run) against 417k–1.37M uncached prompt tokens per run (under 1%). 27 sources, `static_dup_bytes_total=0`, `echo_lines_dropped=0`.
   - **Conclusion:** the context it adds is small and not duplicated. Whether it reduces prompt growth can't be shown without a control run. **Change:** none.
5. **Serena** has zero queries, probes and fallbacks across all 1,000 runs. Either it is not deployed or it emits nothing (`validate.yml:1343` "Emit Serena stats" is `continue-on-error`). There is no cost to assess. Confirm it is intentionally off.

## Reliability Improvements

1. **Log validate outcomes so failures can be classified.** Failure-rate visibility.
   - **Evidence:** validate failed 3 of 3 times (38646893482, 38649799512, 38652432980), all at `Enforce validation outcome`.
   - **Root cause category:** an observability gap. Validate's own failure cause can't be determined from these inputs.
   - **Fix:** add one line at the top of the enforce step's `run:` (`validate.yml:1588`).
     ```yaml
             run: |
               set -euo pipefail
               echo "VALIDATE_OUTCOME status=${VALIDATION_STATUS:-unknown} raw_status=${VALIDATION_RAW_STATUS:-unknown}"
     ```
     Register the prefix in an `agents.d/` fragment (§25).
   - **Impact:** each validate failure becomes `harness_error` (exit 2) or application failure, and the three runs about 25 minutes apart can be checked for whether the same head was validated again.
   - **Rollback:** delete the echo line.
2. **Fix the CI timeouts.**
   - **Evidence:** 3 cancellations at 1,225–1,259s, including a rerun that stopped at the same step (38602731355, `retries=1`).
   - **Root cause (inference):** the 20-minute job timeout. `cancel_on_pr_close` cancelling the run is less likely, because the same step was hit twice.
   - **Fix:** as in Speed item 3.
   - **Impact:** removes 3 of 45 CI non-successes (6.7%).
   - **Fail-open:** not applicable.
3. **CI test failure in 38586073562.**
   - **Evidence:** job `tests-hooks-and-orchestrator`, step `Single-issue security pass tests (port P1)` (`ci.yml:658-663`, covering `tests/test_single_issue_security_pass.py` and `tests/test_review_security_hold_sweep.py`).
   - **Root cause:** unknown, because no error log was available.
   - **Fix:** add `--durations=15 -rf` so the failing test IDs show up in summaries.
   - **Impact:** it was a single failure (2.2% of CI runs), so the main value is faster triage.
4. **Risk from two review runs on one PR at once (inference).**
   - **Evidence:** both runs in each pair succeeded after 70–107 minutes running side by side, so two editors may push to the same branch.
   - **Fix:** the `REVIEW_RUN_ORIGIN` line, then the same-head skip.
   - **Impact:** prevents push races and superseded work.
5. **Healthy signals.**
   - **Semble:** all 8 Semble fallbacks are `context=contract-test` (CI runs 38586073562 and 38602731355, 4 each). `semble_runtime_fallbacks=0`, `semble_bootstraps_failed=0`, `semble_bootstraps_unused=0`. This is normal fail-open behaviour, not a hidden broken rollout.
   - **Poller:** it ran with `SEMBLE_AVAILABLE=false` and an empty `SEMBLE_BIN` (lazy mode) and logged no fallbacks. That is acceptable if the poller never queries Semble.
   - **Other:** `BREAK_GLASS` = 0 and `CONTEXT_BUDGET_WARN` = 0 in every run.

## AI Memory Health

- **No `AI_MEMORY_TELEMETRY:` lines were found** in any `log_summary`. The summaries for clarify 38657896601, plan 38658009649, cancel run 38655429313 and unblock judge 38653890634 explicitly say none were visible.
- **Cause: truncation.** The plan step lost about 484k characters, clarify about 146k and the poll step about 810k, so the lines could be in the dropped part. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, and `fail_open` / `enabled:false` counts can't be calculated.
- **Recommendation:** the collector already streams the full log zips for `GH_PAT_BUDGET phase=end` (`collect_workflow_logs.py:1680-1685`). Reuse that scan to count `AI_MEMORY_TELEMETRY:`, `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` lines before truncation. Then confirm that clarify and plan emit `retrieve` events.

## GH API Call Audit

- **Observed budgets:**
  - review_autofix_sweep: `used_in_job=5` with `remaining=4131` (38657579144), and `used_in_job=10` with `remaining=3136` (38654512680).
  - The PR body lint made 1 paginated `pulls/7363/commits` call (38657947726).
  - cancel_on_pr_close made 0 cancel calls (38657274220, 38656379373).
  - No HTTP 429s or secondary rate limits appear in any summary.
- **Gap:** the poller's own usage is invisible. `orchestrate_poll.yml:111` and `:1809-1811` emit `GH_PAT_BUDGET` start and end lines, and the collector parses them, but only through `--pat-budget-day` (`collect_workflow_logs.py:1884-1887`). They are not in this analysis context. The poller runs back to back (about 6 runs per hour), so it is probably the largest PAT consumer (inference).
- **Recommendations:**
  1. Include the `GH_PAT_BUDGET phase=end` totals per workflow in the standard analysis context. This adds no API calls; it parses logs already downloaded.
  2. Once phase timings exist, check whether any slow poll phase loops over per-item `gh` calls and could read the `_candidate_details_json` or `STALL_MANAGED_LINKED_PR_CACHE` batch caches instead (§14).
  3. The 656 skipped fan-out runs add to every `gh run list` page that the sweep or poller reads (inference). Moving cheap event filters into job-level `if:` conditions keeps the run list shorter. Validate this against the sweep's active-run snapshot logic before changing anything.

## Prompt Cache & Memory System

- **`cache_hit_rate` = 0.707** for review_autofix: 12,651,669 tokens read from cache, 1,701,625 written, 3,531,730 uncached prompt tokens. This is the only family with OpenRouter usage, and all 34 calls had usage data (`or_usage_unavailable_calls=0`).
- **Spread between runs:** 0.595 (38625109890, 10 calls) and 0.605 (38618340834) against 0.859 (38602766010).
  - **Inference:** runs with more calls (10 against 6) and higher completion volume (71k) add more prompt content that is new on each call.
  - **Recommendation:** check that dynamic content (diff, findings) comes after the static rubric and system prefix in every reviewer and editor prompt. Log a prefix hash per call to detect prefix changes.
  - **Estimated impact:** raising the low runs to 0.75 saves about 0.3–0.5M uncached tokens per run.
- **Claude caching (judge run 38656952345):** 236,026 tokens read from cache against 40,908 written. That is healthy, but it isn't aggregated anywhere (see Cost item 2).
- **CONTEXT_BUDGET_WARN:** 0. There is no sign of prompt-size pressure.

## Orchestrator Health

- **Project #7355 flow:** validation failed 3 times between 22:19 and 23:10. The unblock judge then acted at 23:55 (`stop=validation-failed fingerprint=cb0a8929ecce verdict=operator_step round=1 outcome=acted`, run 38656952345). Clarify on #7392 followed at 00:04 (38657896601, 263s), then plan from `/answer` at 00:05 on `orchestrator/project-7355` (38658009649, 866s). The loop recovers, but it took about 1h45m from the first validation failure to a new plan. Two judge-related fields still need checking:
  - `op=run_log outcome=omitted reason=run_unbound`
  - the engine used (`claude-opus-5-5`) differs from the configured `UNBLOCK_JUDGE_MODEL`. Log the engine and model actually used next to the configured ones.
- **Item #7258:** it was judged twice with `outcome=skip reason=fixup_escalated` (38653890634 at 23:24 and 38660357610 at 00:29), using about 100s of runner time to make no change. **Mitigation:** check the escalation state before dispatch, so items with `fixup_escalated` are not dispatched again within `UNBLOCK_JUDGE_FIXUP_WAIT_HOURS=72`.
- **Poller saturation:** 100% duty cycle and 16 superseded waiting runs. This is expected under `cancel-in-progress: false`, but it means the 5-minute cron schedule is never the limit.
- **Indicators to track:**
  - `ORCH_POLL_PHASE` p95 per phase
  - the gap between consecutive poll starts
  - superseded waiting polls per hour
  - judge `outcome=skip` re-dispatches per item
  - validate cycles per tracking issue
- **Not observed:** clarification loops beyond one round and conflict-heal retries. The truncated poll logs hide wave progression.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix (ordered by end-to-end impact) |
|---|---|---|---|
| review/autofix | Compute plus duplicate runs | p95 4,063s; pairs run in parallel (e.g. 6,413s and 5,310s) | 1. Same-head dedupe (Speed 2) |
| orchestrate poll | Compute plus queueing behind the previous poll | 8–12.5 min step; 2.5–8 min wait in the concurrency group | 2. Phase timing, then optimize the top phase |
| CI | Compute near the timeout; retries | p50 924s; 3 cancellations near 20m; 1 rerun | 3. `--durations`, then job split |
| validate | Retry loop with unclear cause | 3 runs × 397–628s, all failed | 4. `VALIDATE_OUTCOME` line; same-head guard if repeated |
| plan / clarify | Compute | plan 866s (38658009649), clarify 263s | No change; tokens not visible |
| unblock judge | Setup | 46–54s runs, about 1.3s of judge work on skips | 5. Pre-dispatch skip check; parallel probes |
| Fan-out | Skipped-run noise | 656 of 1,000 runs skipped | Low priority: tighten trigger filters |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - the poller runs continuously at 8–12.5 min per cycle
  - review/autofix p95 is 4,063s, made worse by about 10 same-PR run pairs
  - CI p50 is 924s, with jobs at their 20-minute limit
- **Top failure modes:**
  - validate 3/3 failures with no classification in the logs (#7355)
  - CI timeouts at `Workflow failure heal contract and driver tests` (2×) and `AI label sync dry-run smoke`
  - CI test failure in `Single-issue security pass tests (port P1)`
- **Highest cost drivers:** review/autofix used 18.03M OpenRouter tokens over 5 logged runs (2.1–5.3M each). Claude spend can't be measured: the one visible data point is $0.526 per acted judge run.
- **Top 3 actions:**
  1. Add the `REVIEW_RUN_ORIGIN` line, then a fail-open same-head dedupe for `workflow_dispatch` runs.
  2. Add the `VALIDATE_OUTCOME` and `ORCH_POLL_PHASE` diagnostic lines, plus pytest `--durations=15` on the timed-out CI steps.
  3. Add `claude_*` to the `cost_audit.py` telemetry fields, and scan full log zips for memory, break-glass and context-budget markers.

## Metrics Appendix

**Runs by family (1,000-run window, about 10h)**

| Family | Runs | Success | Fail | Cancelled | Skipped/other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| All | 1000 | 320 | 4 (0.4%) | 20 | 656 | 6.0 | 1041.1 |
| review_autofix | 67 | 66 | 0 | 1 | 0 | 157.0 | 4063.5 |
| ci | 45 | 41 | 1 (2.2%) | 3 | 0 | 924.0 | 1396.2 |
| orchestrate_poll | 68 | 52 | 0 | 16 | 0 | 696.5 | 1110.65 |
| validate | 3 | 0 | 3 (100%) | 0 | 0 | 566.0 | 621.8 |
| unblock_judge_dispatch | 11 | 11 | 0 | 0 | 0 | 46.0 | 104.0 |
| clarify / plan / implement / clarify_respond | 133 / 128 / 128 / 128 | 3 / 3 / 2 / 0 | 0 | 0 | 130 / 125 / 126 / 128 | 1.0 | 10–11 |
| check_failure_triage / heal / heal_intake | 51 / 46 / 62 | 2 / 0 / 10 | 0 | 0 | 49 / 46 / 52 | 1–2 | 8–11 |

**Tokens and cache (review_autofix only; Claude not aggregated)**

| Run | Calls | Total tokens | Uncached prompt | Completion | Cache read | Cache write | Hit rate | Duration s |
|---|---|---|---|---|---|---|---|---|
| 38618340834 | 6 | 2,137,740 | 512,270 | 20,431 | 1,281,283 | 323,756 | 0.605 | 6413 |
| 38602731407 | 6 | 3,761,552 | 702,136 | 21,663 | 2,694,685 | 343,093 | 0.721 | 5479 |
| 38618374661 [pr:7388] | 6 | 2,436,590 | 526,262 | 18,908 | 1,574,599 | 316,821 | 0.651 | 5310 |
| 38602766010 [pr:7388] | 6 | 5,255,468 | 417,746 | 15,301 | 4,503,021 | 319,400 | 0.859 | 4290 |
| 38625109890 [pr:7389] | 10 | 4,441,374 | 1,373,316 | 71,422 | 2,598,081 | 398,555 | 0.595 | 2569 |
| **Total** | **34** | **18,032,724** | **3,531,730** | **147,725** | **12,651,669** | **1,701,625** | **0.707** | — |

**Review-telemetry metrics:** `cache_hit_rate` 0.707389. `wall_clock_p50_ms` is 1,000 across all runs and 3,254,000 for review_autofix. `wall_clock_p99_ms` is 5,457,030 across all runs and 6,310,260 for review_autofix (12 samples). `break_glass_count` = 0 and `context_budget_warn_count` = 0. Codex: 4 calls / 2 tokens (likely a parser error, plan run 38658009649).

**Semble**

| Target / workflow | query_calls | logged_bytes | bytes per query | sources | static_dup_bytes | bootstraps (ms) | fallbacks (contract-test / runtime) |
|---|---|---|---|---|---|---|---|
| review_autofix | 5 | 81,215 | 16,243 | 27 | 0 | 5 (191,344; avg 38,269) | 0 / 0 |
| ci | 0 | 0 | — | 0 | 0 | 0 | 8 / 0 |

**Serena:** 0 query calls, 0 response bytes, 0 tool calls, 0 fallbacks.

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all targets) | 0 | 0 | 0 |

**Other MCP servers observed:** none.

**GH API**

| Workflow / run | used_in_job | remaining | Rate-limit events |
|---|---|---|---|
| review_autofix_sweep 38657579144 | 5 | 4131 | 0 |
| review_autofix_sweep 38654512680 | 10 | 3136 | 0 |
| lint_pr_body_auto_close 38657947726 | 1 call (paginated) | — | 0 |
| orchestrate_poll | not in context (emitted at `orchestrate_poll.yml:1809`) | — | — |

**Data gaps and assumptions**
- The run-logs directory was absent, so every log-level claim comes from `log_summary` fields.
- `success_sample_rate` is 0.07 and `sampled_success_runs` is 0.
- The duplicate-review finding rests on identical Semble bytes and start times 12–15s apart, not on confirmed head SHAs.
- Treating the CI cancellations as timeouts is an inference from step position and the 20-minute limits.
- Poll queue time is put down to concurrency because each poll's start lines up with the previous poll's end.

**Next collection steps**
- Mount the run-logs directory.
- Add the `REVIEW_RUN_ORIGIN`, `VALIDATE_OUTCOME` and `ORCH_POLL_PHASE` lines.
- Add the `claude_*` fields to the `cost_audit.py` telemetry tuples.
- Raise the collector's 1,000-run cap, or filter skipped runs, so the window covers at least 24 hours.