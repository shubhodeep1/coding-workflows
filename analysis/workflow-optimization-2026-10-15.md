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
## Deep Audit — Workflows & Scripts (2026-10-15)

**Scope and method.** I read these files in full or in large part:
- Workflows: `review_autofix.yml` (gate, post-merge, deterministic-skip, fingerprint-cap and alert steps), `validate.yml`, `internal-review.yml`, `orchestrate_poll.yml`, `test-and-mark-stable.yml`, `issue_pr_status.yml`, `sync_ai_labels.yml`
- Scripts: `gh_helpers.sh`, `label_helpers.sh`, `review_collect_pr_metadata.sh`, `review_autofix_step_mark_ready_to_merge.sh`, `orchestrate_force_tick.sh`, `release_ci_gate.sh`, plus targeted regions of `orchestrate_poll_process.sh` and `review_rb_judge.sh`
- Context: CLAUDE.md §15 and §27, and the agents.md "Repo-specific batching helpers" and "Label-repair contradiction policy" sections

For every other workflow and script I used repo-wide pattern scans: untrusted `${{ }}` in `run:` bodies, duplicated helpers, `--input -` retries, and TODO markers. No shell was available, so I did not measure byte counts. The expression-size figures below are estimates from line counts and are marked [NEEDS VERIFICATION]. Where a finding overlaps the existing report, I cross-reference that report's section instead of repeating it.

### Section 1: Bug & Correctness Sweep

**Untrusted-input scan (no finding).** Every event-controlled field I found is passed through `env:` rather than interpolated into shell. Examples:
- `github.event.comment.body`: `orchestrate_clarify_respond.yml:122,612`, `plan.yml:659,1188`, `clarify.yml:1953`
- `github.event.pull_request.title/body`: `lint-pr-body-auto-close.yml:60-61`, `issue_pr_status.yml:204`
- `github.event.issue.title`: `orchestrate_clarify_respond.yml:75`
- `head.ref`: `cancel_on_pr_close.yml:21`, `integration-pr-readiness.yml:70`

I found no direct `${{ github.event.* }}` interpolation into a `run:` body.

---

- **ID:** BUG-001
- **File:** `scripts/gh_helpers.sh:185-188`, `scripts/gh_helpers.sh:1092-1117`, `scripts/label_helpers.sh:171-187`, `scripts/orchestrate_poll_process.sh:4029-4046`, `.github/workflows/review_autofix.yml:1113-1140`
- **Severity:** High
- **Category:** bug
- **Description:** `ensure_label_exists` runs `gh_retry gh label create …` and expects the "already exists" failure in the normal case.
  - `_is_gh_permanent_failure` only treats `HTTP 422` / `HTTP 404` / `'x' not found` as permanent. gh's message for an existing label is `label with name "X" already exists; use --force …`. The repo's own gh stub writes exactly that message (`tests/test_implement_post_codex_recovery.py:287-289`), and it contains none of those markers.
  - `_gh_cmd_is_unsafe_post` (`gh_helpers.sh:797-809`) does not list `label create`, so `gh_retry` treats the call as retryable. It makes 5 attempts with 1+2+4+8 = 15 s of sleep (`gh_helpers.sh:1109-1116`) before failing. `ensure_label_exists` then matches "already exists" in the captured stderr and returns 0.
  - Each call on an existing label therefore costs about 5 POSTs and about 15 s.
  - The inline `gh_retry` in deterministic-skip-merge (`review_autofix.yml:1113-1135`) has no permanent-failure check at all: 4 attempts and 2+4+8 = 14 s of sleep per call, called at 1356 and 1393.
  - `ensure_label_exists` has 156 call sites across 23 files.
  - `label_helpers.sh` has no per-process cache. The poller's copy caches per process only (`orchestrate_poll_process.sh:4001`), so every poll tick pays 15 s per distinct label it ensures.
  - Inference: this may be a measurable part of the 8–12.5 min poll step flagged in the existing report (Speed Optimizations item 1). [NEEDS VERIFICATION: confirm the stderr text from a live `gh label create` on an existing label.]
- **Recommended fix:** Two changes.
  1. Add the gh-specific message to the permanent-failure pattern:
     ```bash
     _is_gh_permanent_failure()
     {
     	printf '%s' "$1" | grep -qiE "['\"][^'\"]+['\"] not found|gh: Not Found|HTTP 404|404 Not Found|status code 404|HTTP 422|label with name \"[^\"]+\" already exists|Resource not accessible by (integration|personal access token)|Commenting is disabled on issues with more than [0-9]+ comments"
     }
     ```
  2. Add a per-process cache to `label_helpers.sh` `ensure_label_exists`, mirroring `_ENSURED_LABELS_CACHE` (`orchestrate_poll_process.sh:4001-4003`). Use a new, non-colliding name such as `_LABEL_HELPERS_ENSURED_CACHE` keyed by `repo|label`.

  The inline `gh_retry` at `review_autofix.yml:1113` should stop retrying when stderr matches the same pattern.

---

- **ID:** BUG-002
- **File:** `scripts/label_helpers.sh:147`, `scripts/label_helpers.sh:216-252`
- **Severity:** High
- **Category:** bug
- **Description:** `set_issue_phase_label_resilient` reads the issue's labels, computes `(. - $_AI_PHASE_LABELS) + [target]`, and writes the result with a full-replace `PUT /issues/{n}/labels`.
  - `_AI_PHASE_LABELS` (line 147) includes the terminal labels `ai:merged` and `ai:closed`. Setting `ai:ready-to-merge` therefore removes a terminal label that is already there.
  - Any label added between the GET and the PUT is also lost, because the PUT replaces the whole set.
  - Two writers race on the same linked issue:
    - `issue_pr_status.yml:627` sets `FINAL_LABEL`, i.e. `ai:merged`.
    - `review_autofix_step_mark_ready_to_merge.sh:80` sets `ai:ready-to-merge` right after auto-merge is enabled. Auto-merge can land immediately when there is no branch protection (`review_autofix.yml:1051-1052`).
  - This is the incident documented at `review_rb_judge.sh:968-975` (tele-funtoken-msg-scoring#4379: `ai:merged` was replaced by `ai:ready-to-merge`, and that failed project #3928). The fix was applied only to `_resilient_phase_swap` (`review_rb_judge.sh:977-1011`, with notes at 950-953). The shared helper still has the bug.
- **Recommended fix:** Port the guard from `_resilient_phase_swap` into `label_helpers.sh`:
  - Never replace an existing `ai:merged` / `ai:closed` with a non-terminal target.
  - For non-terminal targets, use POST-add plus a per-label DELETE, then a terminal-reconciliation GET, instead of PUT.
  - Document the new call count in the helper docstring (non-terminal: 3 + k calls, per `review_rb_judge.sh:952-953`).
  - Then make `_resilient_phase_swap` delegate to the shared helper (see DUP-003).

---

- **ID:** BUG-003
- **File:** `scripts/label_helpers.sh:247-249`, `scripts/review_rb_judge.sh:1014-1016`, `scripts/gh_helpers.sh:663-735`
- **Severity:** Medium
- **Category:** bug
- **Description:** Both sites run `printf '{"labels":%s}' … | gh_retry gh api -X PUT … --input -`.
  - `_gh_api_args_unsafe_post` treats an explicit PUT as safe (comment at line 667, returns at 715-717), so `gh_retry` may make up to 5 attempts.
  - Stdin is a pipe that attempt 1 consumes completely, so attempts 2–5 send an empty body.
  - Either the retries cannot succeed, or (unverified) an empty-body PUT changes the label set in an unintended way. [NEEDS VERIFICATION: what GitHub does with an empty PUT body on `/issues/{n}/labels`.]
- **Recommended fix:** Write the payload to a `mktemp` file and pass `--input "${payload_file}"`, so every attempt replays the same body. `review_autofix.yml:3346,3354` already uses this file-based `--input "${payload}"` pattern.

---

- **ID:** BUG-004
- **File:** `scripts/label_helpers.sh:189-190`, `.github/workflows/issue_pr_status.yml:209-210,622`, `scripts/review_autofix_step_mark_ready_to_merge.sh:7,76`, `.github/workflows/review_autofix.yml:4369,4448`
- **Severity:** Medium
- **Category:** bug
- **Description:** The `label_helpers.sh` version of `ensure_label_exists` returns 1 on any failure other than "already exists", for example rate-limit exhaustion or a 5xx after retries.
  - It is called as a bare command under `set -euo pipefail` at `issue_pr_status.yml:622`, `review_autofix_step_mark_ready_to_merge.sh:76` and `review_autofix.yml:4448`.
  - One label-create failure aborts the step in the middle of its loop. In `issue_pr_status.yml`, the remaining linked issues then get no terminal label, are not closed, and are left out of `LINEAGE_FINALIZE_ISSUE_NUMBERS`.
  - The poller's copy (`orchestrate_poll_process.sh:4048-4049`) returns 0, so the two copies disagree on the contract.
- **Recommended fix:** Make the `label_helpers.sh` copy fail open (`return 0` after the warning), matching the poller. The labelling call that follows already reports its own failure. Alternatively, append `|| true` at the three bare call sites.

---

- **ID:** BUG-005
- **File:** `.github/workflows/internal-review.yml:49-51`
- **Severity:** Medium
- **Category:** bug
- **Description:** For `pull_request` events, the concurrency group is `internal-review-pull_request-<head_ref>`. It has no PR number and no head repository.
  - Two PRs from different forks with the same branch name (for example `patch-1`) share one group.
  - With `cancel-in-progress: false`, GitHub cancels any older pending run when a newer run joins the group. One PR's queued review can therefore be silently dropped until the sweep finds it.
  - The existing report's duplicate-review finding (Executive Summary, first bullet; Speed Optimizations item 2) is about the split between the dispatch group and the pull_request group. This finding is a separate key-collision problem.
- **Recommended fix:** Key pull_request runs on the PR number:
  ```yaml
  concurrency:
    group: ${{ github.event_name == 'workflow_dispatch' && format('internal-review-dispatch-pr-{0}', github.event.inputs.pr_number) || format('internal-review-{0}-{1}', github.event_name, github.event.pull_request.number || github.head_ref || github.ref_name) }}
    cancel-in-progress: false
  ```

---

- **ID:** BUG-006
- **File:** `.github/workflows/review_autofix.yml:866-906`
- **Severity:** Low
- **Category:** bug
- **Description:** `validation_dispatched` stays `false` both when a dispatch failed and when no linked issue had `ai:orchestrator-validate-required` (all skipped at 884-886). Line 904-906 then always prints `::warning::No standalone validation workflow could be dispatched…`. That is a false-positive warning on most merged PRs.
- **Recommended fix:** Track a separate `post_merge_validate_candidate_seen=true` when an issue passes the label check, and warn only when it is true and nothing was dispatched.

---

- **ID:** BUG-007
- **File:** `.github/workflows/validate.yml:1554-1574`, `scripts/orchestrate_force_tick.sh:3,312-332,351`
- **Severity:** Low
- **Category:** bug
- **Description:** `Force orchestrate poll after validation finalization` has no `continue-on-error`. `orchestrate_force_tick.sh` runs under `set -euo pipefail` and makes unguarded `python3` calls (`payload_json=…` at 312, `_force_tick_now_utc` at 351). A runner-side python failure would mark a passing validate job as failed before `Enforce validation outcome` runs. The neighbouring memory steps use `continue-on-error: true` (1374, 1495). [NEEDS VERIFICATION]
- **Recommended fix:** Add `continue-on-error: true` to the step. The script already treats dispatch as best-effort ("cron remains the fallback", line 429).

---

- **ID:** BUG-008
- **File:** `scripts/orchestrate_poll_process.sh:4130-4133`, `scripts/orchestrate_poll_process.sh:4082-4095`
- **Severity:** Low
- **Category:** bug
- **Description:** `get_issue_labels_json` calls `repos/…/issues/{n}/labels` without `--paginate`, so it gets the default page of 30. `set_issue_phase_label` removes only the phase labels it sees, so a phase label beyond the first page is never removed. `label_helpers.sh:219` and `review_rb_judge.sh:959` both paginate.
- **Recommended fix:** Use `gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/labels" --jq '[.[].name]' | jq -cs 'add // []'`, with the same `|| echo '[]'` fail-open.

---

- **ID:** SEC-001
- **File:** `.github/workflows/validate.yml:129-136`, `.github/workflows/validate.yml:175-177`, `.github/workflows/implement.yml:145-146`, `.github/workflows/security-audit.yml:89-90`, `.github/workflows/orchestrate_clarify_respond.yml:99-100`, `.github/workflows/review_autofix.yml:343-344`
- **Severity:** Low
- **Category:** security
- **Description:** The base64 of `x-access-token:<GH_TOKEN>` is passed on the git argv as `-c http.extraHeader=…`. Nothing calls `::add-mask::` for that encoded form, and a grep for `add-mask` across workflows returns nothing. stderr goes to `/dev/null`, so exposure is limited to the process list or `GIT_TRACE`. [NEEDS VERIFICATION: whether the runner's secret masker already covers base64 of a prefixed secret.]
- **Recommended fix:** Emit `echo "::add-mask::${ghr_b64}"` before first use, or pass the header through the environment (`GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.extraHeader`, `GIT_CONFIG_VALUE_0=…`) so it never appears on argv.

---

- **Already covered in the existing report:** `validate.yml:1583-1601` prints only "Validation did not pass." See Reliability Improvements item 1 (`VALIDATE_OUTCOME`). I raise no duplicate finding.

### Section 2: GitHub API Call Redundancy Audit

- **ID:** API-001
- **File:** `scripts/label_helpers.sh:157-191`, `scripts/orchestrate_poll_process.sh:3995-4050`
- **Severity:** Medium
- **Category:** api-redundancy
- **Description:** This is the API-cost side of BUG-001. Every `ensure_label_exists` call on an existing label issues a POST `gh label create` that fails with 422.
  - **Current:** 5 POSTs per call through `gh_retry`, or 4 through the inline copy, with no cache in `label_helpers.sh`. The poller pays once per label per tick.
  - **Projected:** 1 POST per (repo, label) per process after the BUG-001 pattern fix, and 0 for repeat calls once the cache is in place.
  - With 156 call sites, many inside per-issue loops (for example `review_rb_judge.sh` with 12 calls), the saving grows with N.
- **Recommended fix:** Same as BUG-001: the permanent-failure pattern plus a per-process cache modelled on the poller's `_ENSURED_LABELS_CACHE`. No new API calls.

---

- **ID:** BATCH-001
- **File:** `scripts/orchestrate_poll_process.sh:5059-5070`, `scripts/orchestrate_poll_process.sh:5131-5219`
- **Severity:** Medium
- **Category:** api-batching
- **Description:** `close_merged_issues_sweep` runs on every tick and makes per-item API calls inside its loop.
  - It lists open `ai:merged` and `ai:ready-to-merge` issues (2 calls, up to 200 each).
  - For each issue it calls `_issue_timeline_with_cross_refs_json` (one GraphQL per issue, falling back to paginated REST when there are more than 100 items; `gh_helpers.sh:1745-1767`).
  - For each merged cross-referenced candidate it calls `_fetch_pr_json` (one REST call per candidate, line 5196).
  - Pending `ai:ready-to-merge` issues are fetched again on every tick, with no change detection.
  - **Current:** 2 + (1 default-branch call when uncached) + N + M calls per tick.
  - **Projected:** 2 + 1 + ⌈N/25⌉ calls, because the PR fields needed by `_pr_json_is_issue_implementation_pr` / `_pr_json_base_is_target_merge` (head ref, base ref, body, head repo, merged) can come from the same query.
  - This is a per-iteration call in a poller loop, which CLAUDE.md §15 treats as a review-blocker. Inference: it is a candidate contributor to the poll-duration bottleneck in the existing report (Speed Optimizations item 1).
- **Recommended fix:** Extend the `_fetch_linked_pr_status_graphql` pattern (`orchestrate_poll_process.sh:18083-18185`: batch size 25, aliased `issue(number:)`, fail-open per batch) with a new, uniquely named helper, `_fetch_close_sweep_crossref_prs_graphql`.
  - It should select `timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) { pageInfo { hasNextPage } nodes { ... on CrossReferencedEvent { source { ... on PullRequest { number merged baseRefName headRefName body headRepository { nameWithOwner } } } } } }`.
  - It should adapt the output to the REST field names the callers read (`.head.ref`, `.base.ref`, `.body`, `.head.repo.full_name`).
  - Issues with `hasNextPage=true` or missing from a failed batch fall back to the current per-issue path.
  - Document the batching contract in the docstring, as CLAUDE.md §15 requires.

  [NEEDS VERIFICATION: list every field `_pr_json_is_issue_implementation_pr` (line 18316) reads.]

---

- **ID:** API-002
- **File:** `scripts/review_collect_pr_metadata.sh:218-235,267-272`, `.github/workflows/review_autofix.yml:1036-1045`
- **Severity:** Low
- **Category:** api-redundancy
- **Description:** Each review run makes these calls:
  - `GET pulls/{n}` (line 218)
  - paginated issue comments (222)
  - paginated review comments (234)
  - optional reviews (227)
  - one GraphQL `closingIssuesReferences` call (267)

  The gate job already reads `/pulls/{n}` and exposes `head_ref`, `head_sha` and `retargeted_base_ref` as job outputs (`review_autofix.yml:1036-1045`).
  - **Current:** 4–5 calls.
  - **Projected:** 3–4, by folding `title body headRefName baseRefName headRefOid headRepository{nameWithOwner}` into the existing closing-references GraphQL.

  [NEEDS VERIFICATION: downstream steps may need the full REST shape of `PR_PAYLOAD_FILE`. If so, keep the REST read and drop this item.]
- **Recommended fix:** Extend the line-267 GraphQL query (an existing call, per CLAUDE.md §15 "extend it") and synthesize `PR_META_FILE` from it. Keep REST only where the raw payload shape is consumed.

---

- **ID:** BATCH-002
- **File:** `.github/workflows/review_autofix.yml:868-882`
- **Severity:** Low
- **Category:** api-batching
- **Description:** On the body/title fallback path, labels are unknown, and the loop calls `gh_retry gh issue view N --json labels` once per linked issue.
  - **Current:** N calls.
  - **Projected:** 1 call.
- **Recommended fix:** Build one aliased query (`i0: issue(number: N) { labels(first: 100) { nodes { name } } }`), following `issue_pr_status.yml:469-480` (`ORCH_ALIAS_FRAGMENT`). Fail open to the existing per-issue call.

---

- **ID:** API-003
- **File:** `.github/workflows/review_autofix.yml:776-798`, `.github/workflows/review_autofix.yml:1113-1135`
- **Severity:** Low
- **Category:** api-redundancy
- **Description:** The two inline `gh_retry` copies retry every non-zero exit, including permanent 404/422 failures and non-idempotent POST creates. They do not detect rate limits, and they make 4 attempts with 2/4/8 s backoff.
  - **Current:** up to 4 calls per permanent failure.
  - **Projected:** 1 call.
- **Recommended fix:** Source `${GH_API_RETRY_HELPERS}`. The bootstrap at `review_autofix.yml:331-361` exports it in the gate job; add the same bootstrap to these jobs, or reuse `.codex-freshness-src/scripts/gh_helpers.sh`, which the job already sparse-checks out at 1068-1071. Keep the inline copy only as a last-resort fallback that also checks `_is_gh_permanent_failure`-equivalent patterns.

---

- **ID:** API-004
- **File:** `scripts/orchestrate_poll_process.sh:5261-5263`, `scripts/gh_helpers.sh:804-808`
- **Severity:** Low
- **Category:** api-redundancy
- **Description:** `gh_retry gh issue close … -c "Closing: …"` is classified retryable, because `issue close` is not in the create list. If the comment was posted but the close step failed transiently, a retry posts the same "Closing: linked PR #N was merged" comment again. [NEEDS VERIFICATION: gh's comment-then-close ordering.]
- **Recommended fix:** Post the comment through a single-attempt create (`gh issue comment` is already treated as unsafe), then call `gh_retry gh issue close` without `-c`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001
- **File:** `.github/workflows/review_autofix.yml:776-798`, `.github/workflows/review_autofix.yml:1113-1135`, `scripts/review_collect_pr_metadata.sh:35,69-74`, plus pass-through fallbacks at `scripts/review_apply_fixes.sh:29`, `scripts/review_conflict_prepare.sh:42`, `scripts/review_rb_judge.sh:243-245`, `scripts/validate_process.sh:415`, `scripts/label_helpers.sh:19-21`, `scripts/orchestrate_poll_process.sh:783`, `scripts/review_run_reviewers.sh:14`, `scripts/release_ci_gate.sh:57-62`
- **Severity:** Medium
- **Category:** duplication
- **Description:** `gh_retry` is implemented 3 times outside `gh_helpers.sh:1053`, and there are 8 more pass-through stubs.
  - `review_collect_pr_metadata.sh` sources `gh_helpers.sh` (line 35) and then redefines `gh_retry()` with an incompatible signature (`<outfile> <gh args…>`, line 69). That shadows the shared function for the rest of the process.
  - No `gh_helpers.sh` function that calls `gh_retry` internally is reached from this script today (I checked the call list at lines 138, 218, 222, 227, 234 and 267), but any future call to one, such as `gh_pr_with_all_comments`, would break.
  - This conflicts with the CLAUDE.md §6 "New Identifier Uniqueness" rule.
- **Recommended fix:**
  - In `review_collect_pr_metadata.sh`, replace the five call sites with `gh_retry_to_file "${outfile}" gh api …` and delete the script-local shadow. It is script-internal and not an external contract. [NEEDS VERIFICATION: no test sources this script and relies on the shadow.]
  - Replace the two inline workflow copies as described in API-003.
  - Keep the `{ "$@"; }` stubs, which are legitimate fail-open fallbacks.

---

- **ID:** DUP-002
- **File:** `scripts/label_helpers.sh:157-191`, `scripts/orchestrate_poll_process.sh:3995-4050`, `.github/workflows/review_autofix.yml:1137-1140`, `.github/workflows/review_autofix.yml:1554-1560`, `.github/workflows/review_autofix.yml:4380-4391`, `scripts/review_autofix_step_mark_ready_to_merge.sh:15-29`
- **Severity:** Medium
- **Category:** duplication
- **Description:** There are six implementations of `ensure_label_exists`, and they differ in three ways:
  - **Colour source:** the poller reads `.github/ai/label_contract.v1.json` (4019-4024). `label_helpers.sh` uses a hard-coded catalog (23-145). The inline fallbacks hard-code `e11d48` or `1d76db`. The `review_autofix.yml:1555-1559` fallback applies the `ai:review-blocked` colour and description to whatever label it is given.
  - **Return contract:** 0 versus 1 (BUG-004).
  - **Caching:** only the poller caches.
- **Recommended fix:** Make `label_helpers.sh` the single owner:
  ```bash
  ensure_label_exists()
  {
  	# $1 label (required), $2 repo (default GITHUB_REPOSITORY)
  	# contract colour/description when .github/ai/label_contract.v1.json is readable,
  	# else the built-in catalog; per-process cache; always returns 0 (fail-open)
  }
  ```
  Then:
  - Have the poller source `label_helpers.sh` and drop its copy, keeping the function name per CLAUDE.md §6.
  - Reduce the inline fallbacks to a single `type ensure_label_exists || ensure_label_exists() { …; }` that has no colour logic.

---

- **ID:** DUP-003
- **File:** `scripts/label_helpers.sh:199-263`, `scripts/review_rb_judge.sh:954-1023`, `.github/workflows/review_autofix.yml:1561-1567`, `.github/workflows/review_autofix.yml:4392-4404`, `.github/workflows/review_autofix.yml:4407-4420`, `.github/workflows/issue_pr_status.yml:422-432`, `scripts/review_autofix_step_mark_ready_to_merge.sh:30-38,40-50`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:19-20`
- **Severity:** Medium
- **Category:** duplication
- **Description:** The phase-label swap logic exists as:
  - two full implementations, with different race semantics (BUG-002) and different phase lists (CONSIST-001);
  - seven POST-only inline fallbacks.

  `review_autofix_step_mark_ready_to_merge.sh` defines the same fallback twice (30-38 and 41-49).
- **Recommended fix:** After fixing BUG-002, make `set_issue_phase_label_resilient` in `label_helpers.sh` the only implementation. `_resilient_phase_swap` keeps its name and calls it with `"${REPOSITORY}"`. Collapse each caller's fallback to one guarded definition.

---

- **ID:** DUP-004
- **File:** `.github/workflows/review_autofix.yml:362-375,690-707,725-737,908-935,936-949,1000-1017,1074-1086,1409-1426,1477-1490,1737-1760,1913-1940,5666-5680`, `.github/workflows/orchestrate_poll.yml:97-112`
- **Severity:** Low
- **Category:** duplication
- **Description:** There are 12 inline GH_PAT budget blocks: a start and an end in each of 6 jobs. They read the `/rate_limit` body.
  - `gh_helpers.sh:31-77` `gh_pat_budget` prefers the response headers for the core resource. It is already used by `orchestrate_poll.yml:1809`, `review_autofix_sweep.yml:135,585`, `clarify.yml:2028` and both intake workflows.
  - `orchestrate_poll.yml` mixes the two in one job: an inline start (97-112) and a helper end (1809). Its start and end readings can therefore differ in method.
- **Recommended fix:** Where `GH_API_RETRY_HELPERS` is set, call `source "${GH_API_RETRY_HELPERS}" && gh_pat_budget start review_autofix <job> "${GH_PAT_BUDGET_FILE}"`, and keep the inline body only as an `else` fallback. That removes about 150 lines from `review_autofix.yml`, which helps keep it under the §27 480,000-byte guard.

---

- **ID:** DUP-005
- **File:** `.github/workflows/validate.yml:116-145`, `.github/workflows/implement.yml:145-146`, `.github/workflows/security-audit.yml:89-90`, `.github/workflows/orchestrate_clarify_respond.yml:99-100`, `.github/workflows/review_autofix.yml:331-361`
- **Severity:** Low
- **Category:** duplication
- **Description:** The same "Bootstrap GitHub API retry helper" step (a three-try git clone of `gh_helpers.sh` and `emit_event.sh`) is copied into 5 workflows.
- **Recommended fix:** Extract it into a composite action, `.github/actions/bootstrap-gh-api-retry/action.yml`. The repo already ships `.github/actions/run-soft-error-analyzer`. Inputs: `token`. Output: `GH_API_RETRY_HELPERS` written to `GITHUB_ENV`. [NEEDS VERIFICATION: how consumer repos pin composite actions under "Immutable consumer wrapper pins" (agents.md).]

---

- **ID:** DUP-006
- **File:** `.github/workflows/orchestrate_poll.yml:531-793`, `.github/workflows/review_autofix.yml:2061-2162`
- **Severity:** Low
- **Category:** duplication
- **Description:** In `orchestrate_poll.yml`, eleven near-identical loops (`for f in …; src=.codex-workflow-src/…; fall back to -main; install -m …`) differ only in path list, mode and required/optional message. `review_autofix.yml` has a similar staging block.
- **Recommended fix:** Create a new `scripts/stage_support_files.sh`, run from the verified `.codex-workflow-src`, with this function:
  ```bash
  stage_support_file()
  {
  	# $1 relative path, $2 mode (0755|0644), $3 required|optional, $4 message for a missing optional file
  }
  ```
  Callers: the orchestrate_poll "Stage workflow support files" step and the review_autofix staging step. This also removes most of EXPR-003's body.

---

- **ID:** DUP-007
- **File:** `scripts/orchestrate_poll_process.sh:964-975`, `scripts/orchestrate_poll_process.sh:22357-22362`, `scripts/apply_analysis_on_main.sh:170`, `scripts/auto_release_stable.sh:93`, `scripts/promote_main_cycle.sh:150`, `scripts/review_consolidate.sh:47`, `scripts/review_parse_consolidator.sh:102`, `scripts/workspace_init.sh:21`, `scripts/workspace_safety_check.sh:4`, `scripts/setup_serena.sh:131`, `scripts/validate_process.sh:432`, `scripts/orchestrate_force_tick.sh:12`, and 8 more (`*_truthy` helpers)
- **Severity:** Low
- **Category:** duplication
- **Description:** I counted 20 truthy-normalization helpers, all matching `1|true|yes|on`. `orchestrate_poll_process.sh` defines two equivalent ones (`is_truthy` and `_is_truthy`).
- **Recommended fix:** Low priority. Keep the existing names for compatibility (CLAUDE.md §6). New code should source a single `sh_env_is_truthy` from a new `scripts/shell_common.sh` (a unique name; there is no existing shell common module), and existing helpers can delegate to it opportunistically.

### Section 4: Expression Size Limit Risk Assessment

All counts below are estimates from block line counts multiplied by observed average line length after YAML indent stripping, because no byte-measurement tool was available. They are all [NEEDS VERIFICATION]. Blocks without `${{ }}` in the body are excluded, for example `review_autofix.yml:1098-1407` (about 310 lines, all values through `env:`) and `test-and-mark-stable.yml` Phase 4 / 4b (`1828-2523`, `2609-3263`), which the comments at 1823 and 2604 say were promoted to env.

---

- **ID:** EXPR-001
- **File:** `.github/workflows/test-and-mark-stable.yml:1240-1500`
- **Severity:** High
- **Category:** expression-limit
- **Description:** The "Phase 3b: Wait for PR creation" body is about 260 lines with one interpolation (line 1278: `${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`). That one interpolation makes the whole body a template expression. Estimated size: about 16,500–19,500 characters, leaving about 1.5–4.5 K of headroom. Many lines are long jq programs (1298-1305, 1406-1409). [NEEDS VERIFICATION]
- **Recommended fix:** Move the value to step `env:` and reference it in shell. This takes the block out of expression evaluation, as was done at 1823 and 2604:
  ```yaml
          env:
            IMPL_CREATED_AFTER_INPUT: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}
  ```
  Then use `IMPL_CREATED_AFTER="${IMPL_CREATED_AFTER_INPUT}"`.

---

- **ID:** EXPR-002
- **File:** `.github/workflows/test-and-mark-stable.yml:933-1203`
- **Severity:** High
- **Category:** expression-limit
- **Description:** The "Phase 2: Wait for plan to complete" body is about 270 lines, with `${{ steps.create-issue.outputs.created_after }}` at 951, 963 and 981. Estimated size: about 17,000–20,000 characters, leaving about 1–4 K of headroom. [NEEDS VERIFICATION]
- **Recommended fix:** Add `PLAN_CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step's `env:` and replace the three interpolations with `${PLAN_CREATED_AFTER}`.

---

- **ID:** EXPR-003
- **File:** `.github/workflows/orchestrate_poll.yml:525-795`
- **Severity:** Medium
- **Category:** expression-limit
- **Description:** The "Stage workflow support files" body is about 270 lines, with interpolations at 652, 653 (`vars.*`) and 773 (`github.repository`). Line 531 alone is about 1,000 characters. Estimated size: about 14,000–17,000 characters, leaving about 4–7 K of headroom. Every new support file grows this block. [NEEDS VERIFICATION]
- **Recommended fix:**
  - Move `PROMPT_PRELUDE_REFACTOR_ENABLED` / `UNATTENDED_IDENTITY_REINJECT_ENABLED` into step `env:` under new names, for example `PRELUDE_REFACTOR_VAR` and `IDENTITY_REINJECT_VAR`, and echo those.
  - Replace line 773's `${{ github.repository }}` with the runner default `${GITHUB_REPOSITORY}`.
  - Longer term, apply DUP-006.

---

- **ID:** EXPR-004
- **File:** `.github/workflows/test-and-mark-stable.yml:7363-7549`
- **Severity:** Low
- **Category:** expression-limit
- **Description:** The "Send Telegram notification" body is about 187 lines with about 20 interpolations (7371-7391, 7420-7431, 7542). Estimated size: about 11,000–14,000 characters, leaving about 7–10 K of headroom. Its long prose comments (for example 7529-7539) make it grow quickly.
- **Recommended fix:** Move the `needs.*.result` / `needs.*.outputs.*` values and `RUN_URL` into step `env:`.

---

- **ID:** EXPR-005
- **File:** `.github/workflows/review_autofix.yml:4995-5133`, `.github/workflows/review_autofix.yml:5148-5283`
- **Severity:** Low
- **Category:** expression-limit
- **Description:** Two alert steps have interpolated bodies of about 138 and 135 lines, using `github.server_url`, `github.repository` and `github.run_id` at 4999-5000, 5059, 5110, 5132, 5152-5157. Estimated size: about 10,000–13,000 characters each, leaving about 8–11 K of headroom. The same `PR_URL` / `RUN_URL` composition also appears at 4103-4104, 4849-4850 and 4908-4909.
- **Recommended fix:** Build `PR_URL` and `RUN_URL` from the default `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID` variables, and use `repos/${GITHUB_REPOSITORY}/…`. That removes every `${{ }}` from both bodies.

---

- **ID:** DEBT-001
- **File:** `.github/workflows/test-and-mark-stable.yml:507-4698`, `.github/workflows/review_autofix.yml` (about 5,680 lines)
- **Severity:** Medium
- **Category:** tech-debt
- **Description:** The prompt's 1 MB and 800 KB thresholds do not govern here. GitHub refuses workflow files over 512,000 bytes, and the repo guards at 480,000 bytes (`tests/test_workflow_file_size_limit.py:26-27`, CLAUDE.md §27). Two files are the largest:
  - `test-and-mark-stable.yml` is about 7,550 lines. Its `e2e-smoke-test` job alone runs from line 507 to 4698 (about 4,190 lines), with inline bodies of about 695 lines (Phase 4, 1828-2523) and about 655 lines (Phase 4b, 2609-3263).
  - `review_autofix.yml` is about 5,680 lines.

  I could not measure byte sizes. [NEEDS VERIFICATION: run `wc -c` on both.]
- **Recommended fix:** Following the §27 pattern, move the Phase 4 and Phase 4b bodies verbatim into a new `scripts/test_and_mark_stable_step_<slug>.sh`. Keep `name:`, `id:`, `if:` and `env:` in the YAML. Prioritize this if either file is above about 430,000 bytes.

### Section 5: Cross-Cutting Concerns

**TODO / FIXME / HACK / XXX markers:** none found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.

---

- **ID:** DEAD-001
- **File:** `scripts/review_autofix_step_mark_ready_to_merge.sh:40-50`, `.github/workflows/review_autofix.yml:4407-4420`
- **Severity:** Low
- **Category:** dead-code
- **Description:** Both branches before these blocks define `set_issue_phase_label_resilient`: `label_helpers.sh` defines it, and so does the else-fallback (30-38, 4392-4404). The second `if ! type …` block can only run if a staged `label_helpers.sh` predates the function. [NEEDS VERIFICATION: whether any supported support ref still lacks it.]
- **Recommended fix:** Remove the second definition once every pinned support ref ships the function. Until then, leave a comment that names the compatibility window.

---

- **ID:** DEAD-002
- **File:** `scripts/orchestrate_poll_process.sh:964-975`, `scripts/orchestrate_poll_process.sh:22355-22362`
- **Severity:** Low
- **Category:** dead-code
- **Description:** `is_truthy` and `_is_truthy` have identical semantics in the same file.
- **Recommended fix:** Make `_is_truthy` a one-line delegate (`_is_truthy() { is_truthy "$@"; }`) so there is one implementation and both names stay valid (CLAUDE.md §6).

---

- **ID:** CONSIST-001
- **File:** `scripts/label_helpers.sh:9-11,23-145,147`, `scripts/review_rb_judge.sh:957`, `.github/ai/label_contract.v1.json`
- **Severity:** Medium
- **Category:** consistency
- **Description:** The phase-label sets disagree:
  - `_AI_PHASE_LABELS` has 20 labels, including `ai:validating`, `ai:validation-*` and `ai:security-pass*`.
  - `_rps_phases` has 10.
  - The swap in `review_rb_judge.sh` therefore leaves `ai:validating` in place, while the shared helper removes it.

  The `label_helpers.sh` catalog says it "mirrors" `label_contract.v1.json`, but nothing checks the two stay in sync, whereas `sync_ai_labels.yml` and the poller read the contract.
- **Recommended fix:** Derive both phase lists from the contract's `phase_groups`, which the poller already reads (`orchestrate_poll_process.sh:5333-5344`), with the hard-coded list as a fallback only. Add a test that asserts `_AI_LABEL_COLORS` / `_AI_LABEL_DESCS` match the contract.

---

- **ID:** CONSIST-002
- **File:** `.github/workflows/review_autofix.yml:891,899`, `scripts/orchestrate_force_tick.sh:66,114,425`
- **Severity:** Low
- **Category:** consistency
- **Description:** Raw `gh workflow run`, `gh issue edit` and `gh api` calls sit next to `gh_retry` calls in the same blocks. For example, line 872 uses `gh_retry gh issue view`, but line 899 uses a bare `gh issue edit`.
- **Recommended fix:** Wrap the read and idempotent-edit calls in `gh_retry`, which `orchestrate_force_tick.sh` can source next to `memory_helpers.sh`. Leave `gh workflow run` single-attempt, since it is a create.

---

- **ID:** CONSIST-003
- **File:** `.github/workflows/sync_ai_labels.yml:107`
- **Severity:** Low
- **Category:** consistency
- **Description:** `if [ "${{ inputs.dry_run }}" = "true" ]` interpolates into the shell. The input is `type: boolean` (lines 7-11, 19-23), so this is safe, but it breaks the repo's env-first pattern. The same file uses `DRY_RUN: ${{ inputs.dry_run }}` at 152 and 241.
- **Recommended fix:** Add `DRY_RUN: ${{ inputs.dry_run }}` to the step `env:` and test `${DRY_RUN}`.

---

- **ID:** SHELL-001
- **File:** `.github/workflows/review_autofix.yml:5077-5078`
- **Severity:** Low
- **Category:** shellcheck
- **Description:** `ls -1 … | wc -l` and `ls -1 … | sort -V | tail -n 1` parse `ls` output (SC2012).
- **Recommended fix:** Use `find "${PREVIOUS_REVIEWS_DIR}" -maxdepth 1 -name 'editor_attempt_*.err' -print` with `sort -V`, or a `compgen -G` array.

---

- **ID:** SHELL-002
- **File:** `.github/workflows/review_autofix.yml:1525,1546,1549,1552`
- **Severity:** Low
- **Category:** shellcheck
- **Description:** The step uses `set -uo pipefail` without `-e`, plus `[ -n "${helper}" ] && source "${helper}"` (an SC2015-style `A && B` used as control flow). Any failing command after that line goes unnoticed, for example the `mkdir -p` at 1570.
- **Recommended fix:** If fail-open is intended, add a comment saying so. Otherwise use `set -euo pipefail` with `if [ -n "${helper}" ]; then source "${helper}" 2>/dev/null || true; fi`.

---

- **ID:** SHELL-003
- **File:** `scripts/orchestrate_poll_process.sh:5194`
- **Severity:** Low
- **Category:** shellcheck
- **Description:** `for _sweep_candidate_pr in ${merged_pr_candidates}` relies on unquoted word-splitting (SC2086). Each value is validated by regex at 5195, so this is safe today.
- **Recommended fix:** Use `while IFS= read -r _sweep_candidate_pr; do …; done <<< "${merged_pr_candidates}"`.

---

- **ID:** DEBT-002
- **File:** `scripts/orchestrate_poll_process.sh:5131-5187`, `scripts/orchestrate_poll_process.sh:4363-4364`, `scripts/orchestrate_poll_process.sh:18110-18111`
- **Severity:** Low
- **Category:** tech-debt
- **Description:** The loops start 1–6 `jq` processes per item. For example, the sweep calls `jq` per index at 5132, 5134, 5138, 5182 and 5184, and the batch builders call `jq -r ".[$i]"` per element. Inference: this adds CPU time to the poll step that the existing report flags (Speed Optimizations item 1).
- **Recommended fix:** Stream each item once with `jq -c '.[] | {number, origin, labels: [.labels[]?.name], body}'` into a `while read -r` loop. For the batch builders, emit all numbers once with `jq -r '.[]'`. Measure the effect with the proposed `ORCH_POLL_PHASE` timing.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 4 | BUG-001, BUG-002, EXPR-001, EXPR-002 |
| Medium | 11 | BUG-003, BUG-004, BUG-005, API-001, BATCH-001, DUP-001, DUP-002, DUP-003, EXPR-003, DEBT-001, CONSIST-001 |
| Low | 22 | BUG-006, BUG-007, BUG-008, SEC-001, API-002, API-003, API-004, BATCH-002, DUP-004, DUP-005, DUP-006, DUP-007, EXPR-004, EXPR-005, DEAD-001, DEAD-002, CONSIST-002, CONSIST-003, SHELL-001, SHELL-002, SHELL-003, DEBT-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/review_autofix.yml` (BUG-001, BUG-002) | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/label_helpers.sh`, `scripts/review_collect_pr_metadata.sh`, `.github/workflows/review_autofix.yml` (API-001…004, BATCH-001/002) | Medium |
| Code modularization | `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/review_autofix_step_mark_ready_to_merge.sh`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh`, new `scripts/stage_support_files.sh`, `.github/workflows/{review_autofix,issue_pr_status,orchestrate_poll,validate,implement,security-audit,orchestrate_clarify_respond}.yml`, new `.github/actions/bootstrap-gh-api-retry/` (DUP-001…007) | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/review_autofix.yml` (EXPR-001…005, DEBT-001) | Small |
| Medium/Low fixes | `.github/workflows/internal-review.yml`, `.github/workflows/validate.yml`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/orchestrate_force_tick.sh`, `scripts/label_helpers.sh` (BUG-003…008, SEC-001, CONSIST-*, SHELL-*, DEAD-*, DEBT-002) | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-15)

### Safety Tag Legend
- **`SAFE_TO_MERGE`** means every precondition was proved by reading the code. Both reads hit the same data with the same auth and no pagination. They sit in one function or step with no write between them. Error handling, retry behaviour, log keys and cache contracts stay the same. The implement stage may act on these directly.
- **`NEEDS_VERIFICATION`** means the saving is real, but at least one precondition can't be proved from the code alone. Run the listed verification steps before changing anything.
- **`RISKY_SKIP`** means at least one hard trigger applies: a retry or backoff loop, pagination, a stall-recovery judge, `orchestrate_poll_process.sh`, a race or freshness guard, or a change to log keys that other code greps. These findings are listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID:** MERGE-001
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `scripts/orchestrate_force_tick.sh:57-67` (`_force_tick_fetch_pull_request_json`)
  - `scripts/orchestrate_force_tick.sh:103-115` (`_force_tick_fetch_issue_body`)
  - `scripts/orchestrate_force_tick.sh:274-296` (the two lookup blocks)
  - Caller: `.github/workflows/implement.yml:5935-5940`
- **Current / proposed call count:** 2 → 1 per force tick when `--issue` is an issue number. Today that is a guaranteed 404 on `pulls/{issue}`, then `issues/{issue}`.
- **Endpoints:** `GET /repos/{o}/{r}/pulls/{n}`, then `GET /repos/{o}/{r}/issues/{n}`. Proposed: one `POST /graphql` with `repository.issueOrPullRequest(number:)`.
- **Evidence:** `implement.yml:5937` passes `--issue "${ISSUE_NUMBER}"`, which is an issue, and no `--tracking-issue`. The script always tries the PR endpoint first:
  ```bash
  if [ -z "${TRACKING_ISSUE}" ] && [ -n "${ISSUE_NUMBER}" ]; then
  	pr_json=""
  	if ! pr_json="$(_force_tick_fetch_pull_request_json "${REPOSITORY}" "${ISSUE_NUMBER}")"; then
  		pr_lookup_failed="true"
  ```
  For an issue number, `pulls/{n}` returns 404. The script then falls through to `issues/{n}` at line 288, so the first call is always wasted.
- **Proposed fix:** Replace the two lookups with one GraphQL helper. It selects `__typename`, plus `... on PullRequest { body headRefName baseRefName }` and `... on Issue { body }`. Feed the PR fields to `_force_tick_extract_tracking_issue_from_pull_request_json` (same field shape as the REST `head.ref`/`base.ref`/`body`). Feed the issue body to `_force_tick_extract_tracking_issue`. Follow the single-call, fail-open shape of `_fetch_linked_issue_bodies_graphql` (`scripts/review_collect_pr_metadata.sh:76-181`). Alternative with no API calls: have `implement.yml` pass `--tracking-issue` if the step already knows it.
- **Safety rationale:** The merge changes error handling. The `pr_lookup_failed` / `issue_lookup_failed` pair drives the warning at lines 299-300, a GraphQL error differs from a REST 404, and the test mock (`tests/test_orchestrate_force_tick.py:90-116`) emulates only REST endpoints.
- **Downstream signal (verify first):**
  1. Check whether the `implement.yml` "Force orchestrate poll after implementation PR" step (5928-5940) already has the tracking issue in env or a file. If so, prefer passing `--tracking-issue` (0 lookup calls).
  2. If GraphQL is used, make sure a total GraphQL failure still produces the "lookup failed" warning at lines 299-300.
  3. Extend the gh mock in `tests/test_orchestrate_force_tick.py` to answer `api graphql`. Re-run `test_force_tick_warns_when_tracking_issue_lookups_fail` and `test_force_tick_resolves_tracking_issue_from_pr_branch_refs`.

---

- **ID:** MERGE-002
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_enable_auto_merge.sh:154-170` (paginated PR labels read)
  - `scripts/review_enable_auto_merge.sh:221-228` (full PR read)
  - `scripts/review_enable_auto_merge.sh:91-109` (`reviewed_head_is_current_for_labels`, called at 112 and 158)
- **Current / proposed call count:**
  - Main path: 2 → 1.
  - e2e-smoke-test exit path: 2 (155 + 100) → 1.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}/labels?per_page=100` (`--paginate`) and `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** Both reads run at top level, one after the other. Nothing writes between them; the only step between is the `grep -qx 'e2e-smoke-test'` check. The `pulls/{n}` payload already carries `labels[]`, `head.sha`, `head.ref` and `base.ref`, which is everything read at 230, 244 and 264.
- **Proposed fix (manual review only):** Move the line-221 read above the label guard and get labels from `.labels[].name`. Reuse its `head.sha` in place of the separate freshness read at line 100.
- **Safety rationale:** Line 155 paginates (a hard trigger). It was added on purpose for PRs with more than 30 labels (comment at 144-153, Copilot review #3 on PR #1823), and it is a fail-closed race guard against the e2e gate (comment at 120-143). Merging also changes which `::warning::` text appears on failure.
- **Downstream signal (do not auto-implement):** A human must confirm that the `labels` array embedded in `GET /pulls/{n}` is never truncated below GitHub's 100-labels-per-issue cap. They must also keep both fail-closed exits and their warning texts.

---

- **ID:** MERGE-003
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/unblock_judge.sh:1075-1091` (`issues/{ITEM}` → `item.json`)
  - `scripts/unblock_judge.sh:1110-1117` (`pulls/{ITEM}` when `ITEM_KIND=pr`)
- **Current / proposed call count:** 2 → 1 for PR items only.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}` and `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** For a PR item, the issue read only sets `ITEM_KIND="pr"` through `.pull_request` (line 1087) and supplies labels and body. Then line 1113 fetches the same PR again for `head.sha`, `head.ref`, `user.login`, `author_association`, `head.repo.full_name` and `base.ref`.
- **Proposed fix (manual review only):** Fetch `pulls/{n}` once for PR items and build the `item.json` fields from it. The labels, body, state and `user` fields are shared.
- **Safety rationale:** `unblock_judge.sh` is a stall-recovery judge, which is a hard trigger. `item.json` is also passed whole to `check_triage_resolution.py` (line 336), so its REST issue shape is a contract. The fresh re-read at 1604-1612 is a deliberate race guard (#6557) and must stay.
- **Downstream signal (do not auto-implement):** This needs a human to review the `item.json` consumers (`check_triage_resolution.py`, the trust checks at 929-933) before any shape change. Low value: it saves one call only on PR-kind items.

### Redundant Re-Fetch (REUSE-###)

- **ID:** REUSE-001
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - Gate fetch: `scripts/review_autofix_step_evaluate_gate.sh:187-207` (call at 190-196; output at 1362)
  - Hand-off: `.github/workflows/review_autofix.yml:318`, `:723`
  - Re-fetch: `.github/workflows/review_autofix.yml:810-829` (call at 816-822)
- **Current / proposed call count:** 2 → 1 GraphQL calls for every merged PR whose closing references are empty but whose title or body is not. If the title and body are also empty, the job is already skipped through `post_merge_validate_context_definitely_empty` (line 716).
- **Endpoint:** `POST /graphql`, `pullRequest.closingIssuesReferences(first: 50) { nodes { number labels(first: 100) { nodes { name } } } }`. The query string and `--jq` are identical at both sites.
- **Evidence:**
  - The gate records whether its fetch succeeded (`post_merge_linked_issues_cache_known="true"`, line 198), but exports only the array.
  - Downstream, `[]` from a successful fetch looks exactly like `[]` from the default value (line 30). So the job repeats the same query:
    ```bash
    issue_nodes_json="$(normalize_post_merge_issue_nodes_json "${POST_MERGE_LINKED_ISSUES_JSON:-[]}")"
    ...
    if [ "${issue_nodes_json}" = "[]" ]; then
      if issue_nodes_json_live="$(gh_retry gh api graphql \
    ```
- **Proposed fix:**
  1. In the gate script, also print `post_merge_linked_issues_cache_known=${post_merge_linked_issues_cache_known:-false}` to `GITHUB_OUTPUT` next to line 1362. Initialise it to `false` before the `if` at 187, because the variable is currently set only inside it.
  2. Add the gate output at `review_autofix.yml:318`. In the job env, add a new non-colliding name, `POST_MERGE_LINKED_ISSUES_CACHE_KNOWN` (no existing `POST_MERGE_*` env uses it).
  3. Wrap 816-828 with `if [ "${POST_MERGE_LINKED_ISSUES_CACHE_KNOWN:-false}" != "true" ]`. The live `gh_retry` re-fetch stays the fallback when the gate's single unretried `gh api` failed.
- **Safety rationale:** The calls are in different jobs (`gate` → `post-merge-validate-dispatch`), so they are not in the same step. GitHub silently drops a job output that looks like a secret, so the fallback path must survive an empty or missing new output.
- **Downstream signal (verify first):**
  1. Confirm that a missing or dropped `post_merge_linked_issues_cache_known` output evaluates to not-`true`. The re-fetch must then still run (fail-open).
  2. Run `wc -c .github/workflows/review_autofix.yml` after the 2-line edit to check the 480,000-byte guard (§24).
  3. Re-run the review_autofix gate and post-merge contract tests.

---

- **ID:** REUSE-002
- **Safety tag:** `SAFE_TO_MERGE`
- **Files / lines:**
  - `scripts/orchestrate_force_tick.sh:274-284` (`pulls/{n}` read, kept in `pr_json`)
  - `scripts/orchestrate_force_tick.sh:286-296` (`issues/{n}` body re-read)
  - `scripts/orchestrate_force_tick.sh:69-101` and `:43-55` (the two extractors)
  - Callers passing a PR number with no `--tracking-issue`: `.github/workflows/review_autofix.yml:986-991`, `:4533-4538`, `:5404-5409`
- **Current / proposed call count:** 2 → 1 for every force tick on a PR whose head/base ref is not `orchestrator/project-N` and whose body has no `Tracking issue: #N` line (every non-orchestrated PR).
- **Endpoints:** `GET /repos/{o}/{r}/pulls/{n}` (kept). `GET /repos/{o}/{r}/issues/{n}` with `--jq '.body'` (removed on this path).
- **Evidence:**
  - A PR's issue body and PR body are the same stored field.
  - The PR extractor already searched that body with the same regex the issue extractor uses:
    ```python
    body_re = re.compile(r"(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)")   # line 85
    match = re.search(r"(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)", body, re.MULTILINE)   # line 51
    ```
    `re.MULTILINE` has no effect without `^`/`$`.
  - Whenever the `pulls/{n}` read succeeded and found no tracking issue, the second call at line 288 re-reads the same body and returns the same empty result.
- **Proposed fix:**
  - Initialise `pr_json=""` next to `pr_lookup_failed="false"` (line 227); this reuses the existing name and adds no new identifier.
  - Change the guard at line 286 to:
    ```bash
    if [ -z "${TRACKING_ISSUE}" ] && [ -n "${ISSUE_NUMBER}" ] && { [ "${pr_lookup_failed}" = "true" ] || [ -z "${pr_json}" ]; }; then
    ```
  - When `pulls/{n}` failed (the issue-number case, or a transient error), the `issues/{n}` fallback still runs exactly as today.
- **Safety rationale:** This meets the `SAFE_TO_MERGE` preconditions:
  - **Same data and scope:** the dropped call reads a strict subset (`.body`) of data already held in `pr_json`, with the same `GH_PAT` scope and no pagination.
  - **No intervening write:** both reads are consecutive top-level blocks of one script; only Python parsing runs between them.
  - **Same error handling and logs:** `issue_lookup_failed` only matters when `pr_lookup_failed` is also `true` (299-300), and that path is unchanged. The log text stays the same.
  - **No retry loop, no poller code, no cache contract:** `orchestrate_poll.yml:1582-1587` passes `--tracking-issue`, so it never reaches either lookup.
- **Downstream signal (implement directive):** In `scripts/orchestrate_force_tick.sh`, initialise `pr_json=""` at line 227 and skip the `_force_tick_fetch_issue_body` block (286-296) unless `pr_lookup_failed=true` or `pr_json` is empty. Then add a case to `tests/test_orchestrate_force_tick.py`: a PR mock with no tracking ref or body must produce no `issues/` call.

---

- **ID:** REUSE-003
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:** `scripts/review_rb_judge.sh` runs in the codex-agent job (`review_autofix.yml:4327`), after `review_collect_pr_metadata.sh` (`review_autofix.yml:2610`) has already written:
  - `PR_PAYLOAD_FILE` (`review_collect_pr_metadata.sh:218`)
  - `LINKED_ISSUES_RAW_FILE` (`:339-351`)
  - `PR_DIFF_FILE` (`:509`)

  The judge then re-reads the same data:
  - `review_rb_judge.sh:1071`: `pulls/{n}`, the same data as `PR_PAYLOAD_FILE`
  - `:1096-1100`: GraphQL `closingIssuesReferences` plus `baseRefName`, overlapping `LINKED_ISSUES_RAW_FILE` and `PR_META_FILE.baseRefName`
  - `:1120-1122`: a single-attempt `pulls/{n}` re-read that fires only when the `gh_retry` read at 1071 produced no title or body. In effect it is an extra retry, not a new source.
  - `:1236-1239`: `pulls/{n}` with `Accept: application/vnd.github.diff`, the same data as `PR_DIFF_FILE`
- **Current / proposed call count:** 4 → 1–2 per judge run (theoretical).
- **Endpoints:** `GET /pulls/{n}` (JSON and diff media types), `POST /graphql`.
- **Evidence:** All three artefacts are on disk in `RUNTIME_DIR` for the same job.
- **Safety rationale:** This is a stall-recovery judge (hard trigger). The editor or the conflict resolver may have pushed between metadata collection and the judge. The script also documents race-motivated fresh reads (`:1878-1888`, the merged-state re-fetch). The labels page size differs (`first:100` plus `pageInfo` here, `first:50` in the cached file).
- **Downstream signal (do not auto-implement):** A human must decide whether each re-read guards freshness. At minimum, the 1071 merged-state read and the 1888 guard must stay live. The 1120-1122 re-read could only be dropped after confirming that removing this extra attempt is acceptable when `gh_retry` is exhausted.

---

- **ID:** REUSE-004
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `.github/workflows/issue_pr_status.yml:356-361` (closing refs with `labels(first: 50)`)
  - `.github/workflows/issue_pr_status.yml:469-510` (`ORCH_ALIAS_FRAGMENT` batch with `labels(first: 50)`)
  - `.github/workflows/issue_pr_status.yml:627` → `scripts/label_helpers.sh:219-220` (per-issue paginated `GET /issues/{n}/labels`), then PUT at 247-249
- **Current / proposed call count:** N label GETs → 0 (theoretical). The labels are already in the batched payloads.
- **Endpoints:** `POST /graphql` (has labels), `GET /repos/{o}/{r}/issues/{n}/labels` (paginated), `PUT /issues/{n}/labels`.
- **Evidence:** Every issue that reaches line 627 was classified from a payload that already carried its labels.
- **Safety rationale:** The helper's GET is the read half of a read-modify-write full-replace PUT. Feeding it a batch snapshot taken seconds earlier widens the lost-update race that Deep Audit BUG-002 documents (`ai:merged` vs `ai:ready-to-merge`). The GET is also paginated (hard trigger).
- **Downstream signal (do not auto-implement):** Do not reuse the batch labels here. Revisit only after BUG-002 replaces the PUT with POST-add plus per-label DELETE; then the pre-read may become unnecessary.

---

- **ID:** REUSE-005
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/validate_process.sh:3504-3508` (tracking issue `.body`)
  - `scripts/validate_process.sh:4810-4820` (tracking issue `.state`, the "Tracker-open guard")
- **Current / proposed call count:** 2 → 1 (theoretical).
- **Endpoint:** `GET /repos/{o}/{r}/issues/{tracking}`, twice.
- **Evidence:** It is the same issue resource. The first read could also capture `.state`.
- **Safety rationale:** The two reads are separated by the whole LLM validation run (minutes). The second read is a deliberate fresh check so that no fix-up is opened against a tracker that closed during validation. Its error handling is also different: 3505 is fatal under `set -e`, while 4813 fails open.
- **Downstream signal (do not auto-implement):** Listed so that no later pass merges these reads. Keep both.

### Dead Calls (DEAD-API-###)

- **ID:** DEAD-API-001
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `scripts/review_autofix_step_mark_ready_to_merge.sh:58-63` (live `pulls/{n}` title/body fallback)
  - Producer: `scripts/review_collect_pr_metadata.sh:218,237-243`
  - Step gate: `.github/workflows/review_autofix.yml:4187-4209`
- **Current / proposed call count:** 1 → 0 (believed never to fire on this path today).
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}` with `--jq '.title + " " + (.body // "")'`.
- **Evidence:**
  - The call fires only when `PR_META_FILE` gives a blank title+body:
    ```bash
    PR_DATA="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" 2>/dev/null || echo "")"
    if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
      PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...
    ```
  - The step runs only on `success()`, excluding `CLAUDE_BRANCH_REVIEW_MODE` (line 4198).
  - "Collect PR metadata" (`review_autofix.yml:2603-2610`) has no `continue-on-error`. On the PR path it writes `PR_META_FILE` from a successful `pulls/{n}` REST payload. That payload's title is required to be non-blank.
  - `PR_META_FILE` lives under `/tmp` (`review_autofix_step_init_runtime_workspace.sh:9,45`), outside the workspace cleanup.
  - Its only other writer, `review_conflict_resolve.sh:2246-2249`, rewrites `.body` only.
  - The fallback in `review_autofix_step_mark_review_blocked_workflow_failure.sh:41-44` is **not** dead. That step runs on `failure()`, possibly before `PR_META_FILE` exists. Do not touch it.
- **Proposed fix:** Delete lines 61-63, the inner `if` block. Keep the `PR_META_FILE` read and the extraction.
- **Safety rationale:** Static reading shows the branch can't be reached. But the claim rests on two things outside this repo's code: GitHub's non-blank-title rule, and no future writer clearing `PR_META_FILE`. So the `SAFE_TO_MERGE` bar is not met.
- **Downstream signal (verify first):**
  1. Confirm the REST API rejects a blank PR title (422).
  2. Re-grep for writers of `PR_META_FILE` (`> "${PR_META_FILE}"`, `mv … "${PR_META_FILE}"`) and confirm only the two above exist.
  3. Confirm "Mark linked issues ready to merge" exists only in the codex-agent job, after "Collect PR metadata".

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION. I agree with the saving. The fix changes `_is_gh_permanent_failure`, which sets `gh_retry`'s retry behaviour for every caller, so the live `gh label create` stderr text must be confirmed first. Applying the matching cache change to the poller's copy (`orchestrate_poll_process.sh:3995-4050`) is RISKY_SKIP; limit auto-work to `label_helpers.sh`.
- BATCH-001: RISKY_SKIP. The work is inside `orchestrate_poll_process.sh` (`close_merged_issues_sweep`), and `_issue_timeline_with_cross_refs_json` has a paginated REST fallback (`gh_helpers.sh:1745-1767`). It needs a human design review with `ORCH_POLL_PHASE` timing data first.
- API-002: RISKY_SKIP (stricter than the Deep Audit's tag). Net-new detail:
  - The full REST shape of `PR_PAYLOAD_FILE` is read by at least 8 consumers:
    - `.state` in `review_run_reviewers.sh:460`
    - `.labels[].name` in `review_autofix_step_detect_smoke_test.sh:50`
    - `.head.sha` in `review_autofix.yml:2798`
    - `.head.ref` and `.body` in `review_rb_judge.sh:1210,1363`
    - the `head` object in `collect_pr_check_runs_context.py:120`
    - `review_conflict_resolve.sh:681,1398,2035`
    - `review_single_issue_security_pass.sh:320`
  - `review_autofix.yml:2798-2806` uses it for a head-moved race check.
  - Line 218 fails hard, while the line-267 GraphQL fails open, so folding them changes error handling.
- BATCH-002: NEEDS_VERIFICATION. It saves N−1 calls only on the body-fallback path. A batched query must keep the per-issue "state unknown → skip" behaviour (`review_autofix.yml:879-880`) when one alias fails.
- API-003: RISKY_SKIP. The fix rewrites inline retry and backoff loops (`review_autofix.yml:776-798`, `1113-1135`), a hard trigger. It needs manual review that sourcing `gh_helpers.sh` keeps the #5495 stdout-buffering behaviour.
- API-004: RISKY_SKIP. The call is in `orchestrate_poll_process.sh` and needs gh's comment-then-close ordering confirmed. Splitting it adds a call (1 → 2), so it is a correctness fix, not a consolidation.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | REUSE-002 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-001, DEAD-API-001 |
| RISKY_SKIP | 5 | MERGE-002, MERGE-003, REUSE-003, REUSE-004, REUSE-005 |

Cross-referenced Deep Audit items (not counted above):
- NEEDS_VERIFICATION: API-001, BATCH-002
- RISKY_SKIP: BATCH-001, API-002, API-003, API-004

### Implement-Stage Handoff
- **REUSE-002:** in `scripts/orchestrate_force_tick.sh`, skip the `issues/{n}` body re-read when the `pulls/{n}` read already succeeded. This removes one call from every force tick on a non-orchestrated PR, across the three `review_autofix.yml` force-tick steps. Add a no-`issues/`-call case to `tests/test_orchestrate_force_tick.py`.

Assumptions and gaps:
- This was a static-only audit; no shell, git or test runs were available.
- The "GitHub rejects blank PR titles" point (DEAD-API-001) and the dropped-secret-output behaviour (REUSE-001) come from platform knowledge, not from anything in this repo.
- Call savings are per invocation. Without phase-level telemetry I could not turn them into totals per hour.