I couldn't read the run-logs directory: the path `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox, so there were no `summary.json`, `errors/`, `slow/` or `recent/` folders. This report rests on the supplied `analysis_context.json` (including the `log_summary` fields), checked against the repo files `.github/workflows/ci.yml`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/internal-cancel-on-pr-close.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/collect_workflow_logs.py` and `scripts/cost_audit.py`. `insufficient_data` is `false`.

## Executive Summary

- **Most of the observed time is spent waiting for a hosted runner, not doing work.** 16 runs have a `log_summary` with timestamps. They waited about 7,900s out of 9,355s total (about 84%) on "Waiting for a runner to pick up this job…". The actual work in lint, readiness, gate and cleanup jobs took 3–7s.
  - Review gate jobs waited 16.5 min (39930736846) and 21 min (39930789235).
  - Several runs logged "All GitHub-hosted runners with label [ubuntu-latest] are busy" (39930411043, 39930411084).
  - CI is the likely main source of runner demand. It has no workflow-level `concurrency:` group and fans out into 8+ jobs per run. Its p50 is 3,500s even though no job's `timeout-minutes` exceeds 20. That gap is an inference that CI wall-clock is mostly queue time.
  - Impact: high; queue time is the largest end-to-end latency driver. Confidence: high for the 16 runs, medium as a generalisation.
- **The stable release gate did not complete.** `Test & Mark Stable Release` run 39914231938 was cancelled after 10,069s in step "Phase 4: Wait for review & autofix to complete". The follower then reported `PROMOTE_CANDIDATE_FOLLOW outcome=frozen protected=true` (39930412245). The current logging cannot tell runner starvation of the review run apart from a real review stall. Impact: high, since promotion is blocked. Confidence: medium.
- **Reliability is otherwise strong.** There was 1 failure in 1,000 runs and 0 retries. The failure is CI 39912672392 at `static-checks` → "Validation self-test unit tests" (1,287s run against a 15-minute job timeout). Its root cause cannot be determined without the run's logs. Confidence: high on the rate, low on the cause.
- **Cost telemetry covers almost none of the expensive runs.**
  - 99.8% of the 17,563,499 tracked tokens come from one review run (39916467872).
  - The long plan and implement runs (2,064–2,924s, e.g. 39920559746 and 39925252703) carry no token telemetry at all.
  - `success_sample_rate=0.07` gave only 4 sampled successes, and 698 of the 1,000 rows in the window are 1–11s skipped runs.
  - Claude usage fields (`claude_*`) are missing from every per-family block.
  - Impact: high for planning, since cost decisions are currently blind. Confidence: high.
- **Semble is healthy.** All 40 `SEMBLE_FALLBACK` lines are contract-test fixtures from CI and test-and-mark-stable runs; there are 0 runtime fallbacks. The one real query (15,604 bytes, 8 sources, 0 `static_dup_bytes`, 46,218 ms bootstrap) is negligible next to the 1.79M uncached prompt tokens in the same run. Serena is disabled (`SERENA_ENABLED: false`), so its counters are all zero, as expected. Confidence: high.
- **Two cheap fixes found in the code.**
  - The `heal-pr-reconcile` job in `internal-cancel-on-pr-close.yml:45-50` checks out `main` with `fetch-depth: 0`. That took about 2m10s and printed hundreds of `[new tag]` lines (log truncated by about 567k chars, run 39929758805). The script then took 0.5s to report `noop reason=no_heal_issue`.
  - Memory lineage is stale: implement run 39920354664 completed successfully but is still recorded as `in_progress` when issue #7942 was finalized (39929758814).
  - Impact: medium. Confidence: high for the checkout, medium for the lineage.

## Speed Optimizations

Ranked by expected latency reduction. Items 1–3 are on the critical path; items 4–5 are smaller wins.

1. **Cancel superseded pull-request CI runs.** Critical path.
   - **Evidence:**
     - CI ran 40 times with p50 3,500s, p95 6,410s and max 7,218s (39919044690). That is about 147,000 run-seconds in a window of roughly 4 hours (05:51–09:40Z).
     - `ci.yml` has no top-level `concurrency:` key. It runs 7 named jobs plus an `orchestrate-poll` matrix (`ci.yml:22`, `:506`, `:945`, `:1260`, `:1532`, `:2064`, `:2495`, `:2533`), and every job's `timeout-minutes` is ≤ 20.
     - Several CI runs started within seconds of each other: 39916109516, 39916105042, 39916059357 and 39916054371 all within 32s at 07:10Z.
   - **Root cause (inference):** every PR push queues 8+ runner jobs, and earlier pushes to the same PR keep holding runner slots.
   - **Change:** add a workflow-level `concurrency` group that cancels only for `pull_request` events. Push CI on `main`, `stable` and `orchestrator/project-*` keeps running, because check-failure triage needs a base-branch result.
     ```yaml
     concurrency:
       group: ci-${{ github.event.pull_request.number && format('pr-{0}', github.event.pull_request.number) || github.ref }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
   - **Savings:** each superseded run cancelled frees 8+ runner jobs. The supersede rate is unknown; collect it first (see Diagnostic 1). Even a 20% supersede rate removes about 64+ jobs from a ~4-hour window.
   - **Risk:** low to medium. Confirm that check-failure triage and the merge train handle a `cancelled` CI conclusion on a superseded SHA. It is easy to revert.

2. **Make the Phase 4 wait in the stable gate distinguish "queued" from "running".** Critical path for promotion.
   - **Evidence:** run 39914231938 was cancelled at 10,069s in Phase 4. The job has `timeout-minutes: 300` (`test-and-mark-stable.yml:543`) and `REVIEW_TIMEOUT` defaults to 60. Review gate jobs elsewhere waited 16.5–21 min just to get a runner. The retry path already logs `Retry review run queued for ${RETRY_QUEUED_SECS}s` (`:3287`), but the main wait loop starting at `:1856` has no equivalent queued-time line.
   - **Change:** add one heartbeat line per poll, using a new stable prefix: `E2E_WAIT_REVIEW run_id=<id> status=<queued|in_progress|completed> queued_secs=<n> elapsed_secs=<n> wall_budget_secs=<n>`. Register the prefix through an `agents.d/<id>-e2e-wait-review.md` fragment (§25).
   - **Savings:** none directly. It turns a 2.8-hour cancellation into an attributable cause, so the next fix is chosen correctly.
   - **Risk:** very low; the change only adds log output.

3. **Stop the noop `heal-pr-reconcile` path from fetching full history.** Smaller win.
   - **Evidence:** in 39929758805, checkout took about 2m10s with hundreds of tag lines, and the script then took 0.5s to report `noop reason=no_heal_issue`. The same tag noise appears in 39930411084.
   - **Root cause:** `fetch-depth: 0` (`internal-cancel-on-pr-close.yml:50`). The script does need history for its merges (`_retarget_heal`, `workflow_failure_heal_pr_reconcile.sh:181-195`), but it fetches the specific refs it needs itself with `--no-tags` (`:159`, `:165`).
   - **Change (smallest):** add `filter: blob:none` to the checkout. The commit graph stays complete for merge-base, and blobs load on demand.
   - **Follow-up option:** use `fetch-depth: 1` and run `git fetch --no-tags --filter=blob:none --unshallow origin` inside `_retarget_heal` only when there is a heal PR.
   - **Savings:** about 1–2 min of runner time per PR close, plus much smaller logs.
   - **Risk:** low with `blob:none`; medium for the unshallow variant.

4. **Measure the `*/5` cancel-sweep cron before changing it.** Smaller win.
   - **Evidence:** the scheduled sweep 39931544728 queued 6m38s to do 7s of work and found "no cancellable queued/in-progress pull_request workflow runs" (`MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0`). The schedule `cron: "*/5 * * * *"` (`internal-cancel-on-pr-close.yml:7`) asks for 12 runners per hour, and a 6.6-minute queue means ticks overlap.
   - **Change:** after Diagnostic 1 lands, consider `*/10`.
   - **Trade-off:** merge-train release latency could grow by up to 5 minutes.
   - **Savings:** 6 runner jobs per hour.
   - **Risk:** medium, because the merge train depends on this sweep. Do not change it without data.

5. **Look into the 2m10s lineage finalize step.** Smaller win.
   - **Evidence:** "Finalize linked issue lineage state" ran 09:14:09–09:16:19 in 39929758814 with a single push attempt (`push_attempts=1`).
   - **Change:** add timing fields for fetch, commit and push (Diagnostic 5) before optimising.
   - **Savings:** up to about 2 min per PR close.
   - **Risk:** none for the logging step.

## Cost Optimizations

Ranked by expected savings. The biggest item is visibility, because about 99.8% of observed token spend comes from a single run.

1. **Close the token-telemetry blind spot.** This is the highest-leverage step because every other cost decision depends on it.
   - **Evidence:**
     - Plan runs 39925252703 (2,216s), 39925069802 (2,216s) and 39924906900 (2,064s), implement runs 39920559746 (2,924s) and 39928773941 (2,096s), and clarify run 39917214376 (1,518s) all have no `cost_telemetry`.
     - Only 115 of 1,000 runs carry log telemetry, and most of those are skipped runs.
     - `CLAUDE_COUNT_KEYS` exist in `cost_audit.py:1041`/`:1079`, but no `claude_*` field appears in any per-family block of the analysis context.
   - **Change:**
     - Have the collector always deep-parse completed runs of the AI families (clarify, plan, implement, review_autofix, validate, orchestrate) regardless of `success_sample_rate`.
     - Carry the `claude_calls`, `claude_*_tokens` and `claude_cost_usd` aggregates into `per_workflow_family.cost_telemetry`.
   - **Savings:** none directly; it makes cost attribution possible.
   - **Quality risk:** none.

2. **Review prompt size in the large review run 39916467872.**
   - **Evidence:**
     - 10 OpenRouter calls: 1,787,653 uncached prompt tokens, 15,039,530 cache-read, 626,102 cache-write and 75,407 completion tokens.
     - That averages about 1.75M input tokens per call, against about 7.5k output tokens per call.
     - Cache hit rate is 0.8617. The calls are cache-efficient, but the context is very large.
   - **Root cause:** unknown without logs. A likely candidate (inference) is agentic multi-turn sessions re-reading a large context.
   - **Change:** break `or_phases` down per reviewer, aggregator and editor for this run so the call or calls driving most of the 1.79M uncached tokens can be found. Then cap context for that phase.
   - **Savings:** 10–30% of uncached prompt tokens if one phase dominates (estimate).
   - **Quality risk:** medium; do not trim reviewer context blindly.

3. **Stop paying cache writes that are never read in the e2e smoke test.**
   - **Evidence:** run 39914231938 made 12 calls with `or_cache_write_tokens=28,462`, `or_cache_read_tokens=0` and `cache_hit_rate=0.0`.
   - **Root cause (inference):** each call is small and has a different prefix, or the gaps between calls are longer than the cache lifetime.
   - **Change:** skip cache-control on these short e2e prompts, or put the stable instructions first.
   - **Savings:** small, about 7k-token equivalents per run.
   - **Quality risk:** none.

4. **Semble and Serena: no action.**
   - The one Semble query (target recorded in `semble_targets`, 15,604 bytes, 8 sources, about 1,950 bytes per source, 0 `static_dup_bytes`) is about 0.2% of that run's uncached prompt. The 46s lazy bootstrap is about 0.9% of the 5,054s run.
   - Semble adds no noisy context and makes no material change to prompt size either way.
   - Serena is disabled, so it neither replaces tool or model work nor adds noise.

5. **No avoidable reruns observed.** Every run has `retries=0`, and there are 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` lines, so prompt-size pressure is not visible.

## Reliability Improvements

1. **Stable gate cancellation (39914231938).**
   - **Category:** wait-loop timeout or starvation.
   - **Evidence:** 10,069s, `e2e-smoke-test` / "Phase 4: Wait for review & autofix to complete", with `test_and_mark_stable` showing success 0 of 1. The follower is frozen (`PROMOTE_CYCLE_FREEZE_MAX_SECS: 172800`, 39930412245).
   - **Fix:** Speed item 2 (heartbeat with queued time). Then gate the inactivity clock on `status=in_progress` only, so time spent queued does not count as stall time.
   - **Expected impact:** fewer false-stall cancellations of the release gate.
   - **Fail-open:** the change only logs and reclassifies; keep the existing wall-clock budget as the hard cap.

2. **CI static-checks failure (39912672392).**
   - **Category:** unknown; test failure or timeout.
   - **Evidence:** the 1,287s run duration includes queue time, and `static-checks` has `timeout-minutes: 15` (`ci.yml:40`). The comment at `ci.yml:35-38` records an earlier 15m18s overrun (run 38562929820).
   - **Fix:** check the job conclusion first. If it timed out, the budget-watch warning at 75% should already have fired, and the next step is moving a step out per the existing pattern. If tests failed, make the step print a short failure summary so triage needs no log download.
   - **Expected impact:** this was the only failure in the window; the gain is faster diagnosis.

3. **Stale queued runs are kept indefinitely.**
   - **Evidence:** the 39931544728 cleanup says it "preserved 6 active run(s) without pull-request linkage". These include `id=34748435167` and `id=34748435180` (created 2026-09-13) and `id=32215723558` (2026-08-19, `ai/issue-3768`).
   - **Category:** orphaned queue state.
   - **Fix:** log an age field for preserved runs, e.g. `CANCEL_SWEEP_PRESERVED id=<id> age_hours=<n> workflow=<file>`. Cancelling by age is possible later behind a default-off var (`CANCEL_STALE_QUEUED_MAX_AGE_HOURS`, default `0` meaning disabled).
   - **Expected impact:** clearer queue state, and possibly freed concurrency.
   - **Rollback:** the var defaults to off.

4. **Semble fallbacks are not a masked broken rollout.** All 40 are `semble_contract_test_fallbacks`: 9 CI runs with 4 each, plus the e2e run with 4. There are 0 runtime fallbacks, 0 failed bootstraps and 0 unused bootstraps. Report the runtime-fallback count as the headline number so the 40 fixture lines don't trigger false alarms. There are no `SERENA_PROBE` lines because Serena is disabled.

5. **`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`:** 0 in every family. There is no visible policy or prompt-size pressure.

## AI Memory Health

- **Telemetry found:** two `AI_MEMORY_TELEMETRY` lines, both quoted in `log_summary` fields.
  - 39930411043: `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues","source":"issue_pr_status.yml"}`. This is a correct no-op; the `fail_open: true` flag is benign here.
  - 39929758814: `{"did_push": true, "final_state": "closed", "issue_number": 7942, "ok": true, "op": "finalize-task"}`, with `push_attempts=1` and commit `ab8ac14f79c74dcd5cc18bda9d81b0595b663143`.
- **Retrieve hit rate:** cannot be computed. No `retrieve` operations are visible, so `records_selected`, `estimated_tokens` and `keyword_method` are all unavailable. This is the same coverage gap as Cost item 1. Verify that plan and implement runs emit `retrieve` lines by deep-parsing one of 39920559746 or 39925252703.
- **Stale lineage (medium confidence):** when #7942 was finalized, the lineage still showed implement run 39920354664 as `in_progress` at 08:18:34Z. That run actually finished successfully after 1,938s (it is in the slow-runs list). Likely cause: no `record-run-event` is written when the implement run completes.
  - Smallest fix: in finalize-task, log `stale_run_entries=<n>` and set entries whose GitHub run is `completed` to their real conclusion.
- **No push-retry pressure:** `push_attempts=1`.

## GH API Call Audit

- **Visible volume is low.** The lint workflow makes one paginated `gh api repos/…/pulls/<n>/commits?per_page=100` call per PR: 14 commits for #7922 (39930789065), 14 for #7934 (39930736723) and 3 for #7948 (39929763058). No 429s, secondary rate limits or retries were seen in any summarized run.
- **Possible duplicate PR fetches per event (inference).** Lint and integration-readiness fire as separate workflows on the same PR event: 39930789062 and 39930789065 at 09:17:28 for #7922, and 39930736713 and 39930736723 at 09:17:02 for #7934. Each reads PR data independently. The saving would be 1 call per PR event, so the value is mainly fewer runner requests, not API quota. Do not merge them unless branch protection does not depend on the separate check names.
- **Skipped-run event fan-out.** Clarify, plan, implement and orchestrate-clarify-respond fire together and skip within 1–11s (e.g., 09:40:30 → 39933574981, 39933574910, 39933574818, 39933574824). Heal, heal-intake and check-failure-triage do the same (e.g., 09:38:51–53). That is 698 skipped runs in the window. They cost little runner time, but they fill the collector's 1,000-run cap (see Diagnostic 2).
- **Cancel sweep:** cancellations go through `gh_retry gh api -X POST`. There were 0 cancellations in the observed sweep and no rate-limit events.
- **Hygiene check (§14):** none of the visible calls are per-item loops; the existing GraphQL batch helpers are unaffected.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.8587 overall = 15,039,530 read ÷ (15,039,530 read + 654,564 write + 1,820,119 uncached). It is 0.8617 for review_autofix and 0.0 for test_and_mark_stable.
- **Review cache works well.** Cache writes are about 4% of input. The remaining cost is uncached input (1.79M tokens), which comes from context size, not fragmentation.
- **Fragmentation.** The e2e smoke run has 12 calls with 28,462 tokens written and 0 read. Either the prefixes vary or the calls are too small to benefit; see Cost item 3.
- **`CONTEXT_BUDGET_WARN`:** 0, so there is no evidence that prompt growth is eroding cache value.
- **Memory retrieval:** not measurable (see AI Memory Health). Next step: deep-parse one plan run and one implement run per window.

## Orchestrator Health

- **The poller is starved.** `orchestrate_poll` had 25 runs: 15 succeeded and 10 were cancelled (40%). The sampled cancellations are all `poll / poll` → `cancelled_before_first_step` after 157s (39932764525), 344s (39933053810), 735s (39931278216) and 1,316s (39928316033) in the queue.
  - Inference: newer ticks replaced pending ones because of concurrency, which means poll cadence is effectively limited by runner throughput.
  - Successful polls have p50 925s and p95 2,025s.
  - Indicator to track: poll cancellations where the run never started, ÷ poll runs.
- **Wave progress.** The readiness checker reports #7913 at 8 of 10 sub-issues unchecked (PR #7922) and #7925 at 2 of 5 unchecked (PR #7934). Neither has the `ai:override-incomplete-merge` label, which is correct gating.
- **Clarification loops are idle.** `orchestrate_clarify_respond` ran 141 times: 140 skipped, 1 succeeded. Clarify succeeded 9 times, with the slowest at 1,518s (39917214376).
- **Auto-close discipline works.** #7922 and #7934 use `Refs #<tracking>`. #7948 uses `Closes …/issues/7942`, which points at a sub-issue, so the lint correctly let it pass (§21).
- **Promotion is frozen.** The promote follower reports `outcome=frozen` while the stable gate (39914231938) did not complete. Indicator: hours frozen against `PROMOTE_CYCLE_FREEZE_MAX_SECS=172800`.

## Pipeline Flow Bottlenecks

| Stage | Successful runs | Representative slow runs (s) | Dominant overhead |
|---|---|---|---|
| clarify | 9 | 1518, 1396, 1237 | unknown (no telemetry) |
| plan | 6 | 2216, 2216, 2064, 2063 (a wave of 4 between 08:28 and 08:31Z) | unknown |
| implement | 5 (+34 cancelled) | 2924, 2096, 1938, 1820, 1668 | unknown |
| review_autofix | 46 / 51 | p50 1298, p95 4200, max 5054 | queue (gate waits of 16–21 min) plus model work |
| CI | 39 / 40 | p50 3500, p95 6410 | **queue (inferred: wall-clock is about 3× the longest job timeout)** |
| readiness / lint | 64 / 64 | 174–1208 | queue (≥ 84%, exec ≈ 4–6s) |
| stable gate | 0 / 1 | 10069 | waiting on review (cause not attributable) |

The collector's `duration_seconds` runs from `run_started_at` to `updated_at` (`collect_workflow_logs.py:1123-1127`), so every number above includes job queue time.

Fixes, ordered by end-to-end impact:
1. Cancel superseded PR CI runs (Speed item 1).
2. Add job-level queue metrics so the split between queueing and compute is measured, not inferred (Diagnostic 1).
3. Add the stable-gate wait heartbeat (Speed item 2).
4. Slim the `heal-pr-reconcile` checkout (Speed item 3).

Merge and conflict overhead: none visible. The merge train examined 0 PRs in both observed sweeps.

**Proposed diagnostic logging (all additive, fail-open):**
1. **Queue split per run.** In `compute_run_metrics` (`scripts/collect_workflow_logs.py:1112`), add `queue_seconds_max` and `queue_seconds_sum` (from `job.started_at − job.created_at`) and `exec_seconds_max` next to `duration_seconds`.
2. **Window coverage.** Report `skipped_count` separately, and exclude `skipped` runs from the 1,000-run cap so the window covers more hours and more successful AI runs.
3. **Stable-gate heartbeat.** The `E2E_WAIT_REVIEW` heartbeat in `test-and-mark-stable.yml` Phase 4 (registered via an `agents.d` fragment).
4. **Preserved-run age.** `CANCEL_SWEEP_PRESERVED … age_hours=` in the cancel sweep.
5. **Lineage finalize detail.** In lineage finalize, add `stale_run_entries`, `fetch_ms`, `commit_ms` and `push_ms` to the `finalize-task` telemetry JSON.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Hosted-runner queue: about 84% of wall-clock in the 16 summarized runs.
  - CI p50 3,500s.
  - Review gate waits of 16–21 min.
- **Top failure modes:**
  - Stable gate cancelled in Phase 4 (39914231938).
  - Poller ticks cancelled while queued (10 of 25).
  - One CI `static-checks` failure (39912672392).
  - Stale lineage run status for #7942.
- **Highest cost driver:** review_autofix run 39916467872, 17.53M tokens (99.8% of tracked). Tracking does not cover plan, implement or Claude spend.
- **Top 3 actions:**
  1. Add PR-scoped `concurrency` with `cancel-in-progress` to `ci.yml`, together with the queue-split collector fields.
  2. Add the Phase 4 heartbeat and exclude queued time from the stall clock.
  3. Make the collector always deep-parse AI-family runs and carry `claude_*` fields through.

## Metrics Appendix

**Run outcomes (window ≈ 05:51–09:40Z, 2026-10-23; 1,000 runs)**

| Metric | Value |
|---|---|
| Total / success / failure / cancelled / other (skipped) | 1000 / 248 / 1 / 53 / 698 |
| Failure rate | 0.1% |
| p50 / p95 duration | 2s / 2,600s |
| Sampled success runs (`success_sample_rate` 0.07) | 4 |
| Runs with log telemetry | 115 |

**Key families**

| Family | Runs | Success | Cancelled | Skipped | p50 (s) | p95 (s) |
|---|---|---|---|---|---|---|
| ci | 40 | 39 | 0 | 0 | 3500.5 | 6410 |
| review_autofix | 51 | 46 | 2 | 3 | 1298 | 4199.5 |
| implement | 138 | 5 | 34 | 99 | 3 | 364 |
| plan | 141 | 6 | 0 | 135 | 1 | 266 |
| clarify | 152 | 9 | 0 | 143 | 1 | 775 |
| orchestrate_poll | 25 | 15 | 10 | 0 | 925 | 2025 |
| integration_pr_readiness | 35 | 35 | 0 | 0 | 414 | 1024 |
| lint_pr_body_auto_close | 29 | 29 | 0 | 0 | 500 | 955 |
| cancel_on_pr_close | 21 | 18 | 3 | 0 | 516 | 1554 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 10069 | 10069 |

**Tokens and cache**

| Metric | Overall | review_autofix | test_and_mark_stable |
|---|---|---|---|
| or_calls | 22 | 10 | 12 |
| or_prompt_tokens (uncached) | 1,820,119 | 1,787,653 | 32,466 |
| or_completion_tokens | 77,748 | 75,407 | 2,341 |
| or_cache_read / write | 15,039,530 / 654,564 | 15,039,530 / 626,102 | 0 / 28,462 |
| or_total_tokens | 17,563,499 | 17,528,692 | 34,807 |
| cache_hit_rate | 0.8587 | 0.8617 | 0.0 |
| codex_calls / tokens | 0 / 0 | 0 / 0 | 0 / 0 |
| claude_* | not present in context (data gap) | — | — |

**Review telemetry**

| Metric | Overall | review_autofix | ci |
|---|---|---|---|
| wall_clock_p50_ms | 2,000 | 1,265,000 | 5,513,000 |
| wall_clock_p99_ms | 7,128,820 | 4,970,680 | 7,167,040 |
| wall_clock_sample_count | 115 | 5 | 9 |
| break_glass_count | 0 | 0 | 0 |
| context_budget_warn_count | 0 | 0 | 0 |

**Runner queue (from `log_summary`)**

| Run | Family | Duration (s) | Queue (≈ s) | Exec (≈ s) |
|---|---|---|---|---|
| 39930789235 | review_autofix (gate) | 1265 | 1249 | — |
| 39930736846 | review_autofix (gate) | 1008 | 990 | — |
| 39929763058 | lint_pr_body_auto_close | 922 | 913 | 5 |
| 39930412424 | check_failure_triage_historical | 670 | 660 | 3 |
| 39930446205 | integration_pr_readiness | 659 | 650 | 4 |
| 39930411084 | cancel_on_pr_close | 815 | 616 | — |
| 39931544728 | cancel_on_pr_close (schedule) | 423 | 398 | 7 |
| Sum of 16 summarized runs | — | 9,355 | ≈ 7,904 (84%) | — |

**GH API**

| Signal | Value |
|---|---|
| Visible REST calls (lint) | 1 paginated call per PR (#7922, #7934, #7948) |
| 429 / secondary rate-limit / retry events | 0 visible |
| Cancel-sweep cancellations | 0 (6 unlinked runs preserved) |

**Semble**

| Metric | Value |
|---|---|
| query_calls / bytes / sources / static_dup_bytes | 1 / 15,604 / 8 / 0 |
| bootstraps / failed / unused / bootstrap_ms_total | 1 / 0 / 0 / 46,218 |
| fallbacks total / contract-test / runtime | 40 / 40 / 0 |

**Serena and per-target MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | fallbacks |
|---|---|---|---|---|---|---|
| serena (all targets; `SERENA_ENABLED: false`) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps**
- The run-logs directory was not mounted.
- There are no deep-dive excerpts for 39912672392 or 39914231938.
- `claude_*` aggregates are missing from the context.
- There are no `retrieve` memory operations.
- The collector does not split queue time from execution time.