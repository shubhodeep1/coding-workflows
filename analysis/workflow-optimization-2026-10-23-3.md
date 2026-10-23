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
## Deep Audit — Workflows & Scripts (2026-10-23)

Scope note: this pass goes deep on a subset of files rather than skimming everything. Files read: `orchestrate_poll.yml`, `internal-orchestrate-poll.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `review_autofix_sweep.yml`, `test-and-mark-stable.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`, `pr6576-shard-followup.yml`, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/review_merge_train.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, and selected ranges of `scripts/orchestrate_poll_process.sh`.

Two findings are already in this report's Speed Optimizations section and are not repeated here: item 1 (no `concurrency:` in `ci.yml`) and item 3 (`fetch-depth: 0` in `heal-pr-reconcile`).

Byte counts and character counts could not be measured in this run. Every size figure below is a line count, or an estimate derived from one.

### Section 1: Bug & Correctness Sweep

- **BUG-001** — `.github/workflows/orchestrate_poll.yml:66-85`, `:1443-1445`; `.github/workflows/internal-orchestrate-poll.yml:3-5`
  - **Severity:** High
  - **Category:** `bug`
  - **Description:** The `poll` job has no `timeout-minutes`. The only `timeout-minutes:` in the file is at `:2088`, on `claude_pool_daily_usage`. GitHub's default job timeout of 360 minutes therefore applies.
    - The job's concurrency group is `ai-orchestrate-poll-${{ github.repository }}` with `cancel-in-progress: false` (`:68-73`).
    - The caller fires every 5 minutes (`cron: '*/5 * * * *'`).
    - The main step runs `bash scripts/orchestrate_poll_process.sh` (`:1445`) with no `timeout` wrapper.
    - The workflow shows no internal wall-clock budget: the only matches for one are the `REVIEW_RUN_MAX_RUNTIME_MINUTES` env lines.
    - Effect: one hung `gh`, `git` or judge call blocks every later tick for up to 6 hours. Only one run can be pending per concurrency group, so each newer pending tick cancels the previous one.
    - Inference: this is consistent with the 10 of 25 poller runs this report records as `cancelled_before_first_step` (Orchestrator Health).
  - **Recommended fix:** Add a job-level `timeout-minutes: ${{ fromJSON(vars.ORCH_POLL_TIMEOUT_MINUTES || '90') }}`. The new variable keeps a default, per §8. The 90-minute default is about 2.6× the observed successful-poll p95 of 2,025s. Optionally also wrap the long step in `timeout --signal=TERM`. This assumes the poller can resume on the next tick, since its state lives in issue comments [NEEDS VERIFICATION for mid-merge interruption safety].

- **BUG-002** — `.github/workflows/plan.yml:707-714`; consumers at `:889-894`, `:1286-1291`, `:1303-1324`, `:1404`
  - **Severity:** High
  - **Category:** `bug`
  - **Description:** "Fetch issue comments" runs `gh_retry gh api --paginate "repos/…/issues/${ISSUE_NUMBER}/comments?per_page=100" > "${ISSUE_COMMENTS_FILE}"` with no `--slurp` and no `jq -s`.
    - On an issue with more than 100 comments, the file holds one JSON array per page.
    - Every other caller of this endpoint already merges pages: `plan.yml:2664` and `implement.yml:2229` use `| jq -s 'add // []'`, and `implement.yml:871`, `clarify.yml:1015` and `orchestrate_clarify_respond.yml:726` use `--slurp`.
    - "Skip stale /answer comments" then evaluates `[ .[] | select(... /answer ...) ] | sort_by(.created_at) | last | .id // ""` once per page. `LATEST_ANSWER_COMMENT_ID` becomes multi-line (for example `"\n<id>"`), never equals `TRIGGER_COMMENT_ID` (`:906`), and the step writes `SKIP_PLAN=true` with `reason=stale_answer_comment`.
    - Result: planning is silently skipped on long-lived issues.
    - The answer extraction, question extraction and ALL COMMENTS context (`:1286`, `:1303`, `:1404`) are also computed per page.
    - The multi-array output matches how the repo handles `--paginate` everywhere else. That any real issue has crossed 100 comments is unconfirmed.
  - **Recommended fix:** Use the same pattern as `plan.yml:2664`:
    - `gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments?per_page=100" | jq -s 'add // []' > "${ISSUE_COMMENTS_FILE}"`
    - Keep the step's existing fail-closed behaviour.

- **BUG-003** — `.github/workflows/integration-pr-readiness.yml:25-79`; `scripts/check_integration_pr_readiness.py:117`, `:186`, `:221`; `.github/workflows/lint-pr-body-auto-close.yml:27-47`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** The readiness workflow runs on `labeled`, `unlabeled`, `synchronize`, `opened`, `reopened` and `ready_for_review`. It has no `concurrency:` group.
    - The PR labels come only from the event payload (`PR_LABELS_JSON: ${{ toJSON(github.event.pull_request.labels) }}`, `:58`). The script trusts that file (`:186`) for the override check (`:221`) and posts a commit status against the head SHA (`:117`).
    - The last run to finish wins. If an `unlabeled` run (override removed) finishes before an earlier `labeled` run (override present), the stale `success` stays on the SHA. That status is the merge gate described at `:20-22`.
    - Out-of-order runs are more likely under the queue delays recorded in this report (16–21 minutes). This is an inference.
    - `lint-pr-body-auto-close.yml` has the same race on `edited` events for the same SHA: an older body can be the last check reported.
  - **Recommended fix:**
    - Add `concurrency: { group: integration-pr-readiness-${{ github.event.pull_request.number }}, cancel-in-progress: true }`, and the matching `lint-pr-body-auto-close-<pr>` group.
    - Have `check_integration_pr_readiness.py` re-read live PR labels with one `GET repos/{repo}/issues/{n}/labels` call before deciding. Use the payload only as a fail-closed fallback.

- **BUG-004** — `.github/workflows/implement.yml:5406-5407`, `:5616-5617`, `:5630-5633`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** The "Handle no-op implementation" fall-through posts its comment with `gh_retry gh api "repos/…/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"`. There is no `|| true`, and the step runs under `set -euo pipefail`.
    - `gh_retry` makes exactly one attempt for a non-idempotent POST (`gh_helpers.sh:1067-1069`).
    - One transient failure therefore aborts the step before `ai:implementation-failed` is applied (`:5630-5633`). That label is the orchestrator's re-processing signal (`:5612`).
    - The two earlier guards in the same step tolerate failure: `:5452-5454` and `:5586-5588` both use `|| true`.
  - **Recommended fix:**
    - Apply the label transition first, then post the comment.
    - Append `>/dev/null 2>&1 || echo "::warning::no-op comment failed for #${ISSUE_NUMBER}"`.
    - Keep the step's failure semantics otherwise unchanged.

- **BUG-005** — `.github/workflows/review_autofix.yml:293-294`, `:749-751`, `:979-981`, `:1071-1073`, `:1484-1486`, `:1492-1505`; `.github/workflows/review_autofix_sweep.yml:109-110`; `.github/workflows/internal-cancel-on-pr-close.yml:19-27`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** These jobs have no `timeout-minutes`, so each defaults to 360 minutes:
    - `review_autofix.yml`: `gate`, `post-merge-validate-dispatch`, `post-merge-force-poll`, `deterministic-skip-merge`, `claude-fixer-auto-merge` and `fingerprint-cap-block`. Only `codex-agent` sets one (`:1831`).
    - `review_autofix_sweep.yml`: `sweep`, which runs on a 30-minute cron under a non-cancelling group (`:104-106`).
    - `internal-cancel-on-pr-close.yml`: `heal-pr-reconcile`.
    - A hung API call or git fetch in any of them holds a runner slot, and for the sweep the concurrency group, for up to 6 hours. That adds to the runner starvation this report measured.
  - **Recommended fix:** Add explicit job timeouts sized from observed run times plus a margin:
    - 15 minutes for `gate` and the `post-merge-*` jobs.
    - 30 minutes for `deterministic-skip-merge`, `fingerprint-cap-block` and `sweep`.
    - 10 minutes for `heal-pr-reconcile`.
    - [NEEDS VERIFICATION]: check each value against the jobs' p99 before landing.

- **BUG-006** — `scripts/review_collect_pr_metadata.sh:33-35`, `:69-74`; call sites `:138`, `:194`, `:231`, `:235`, `:240`, `:247`, `:280`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** After sourcing `gh_helpers.sh`, the script redefines `gh_retry` with a different signature, `gh_retry <outfile> <gh-args…>` (which calls `gh_retry_to_file`).
    - Several `gh_helpers.sh` functions call `gh_retry gh api …` internally: `gh_review_pr_state` (`:91`, `:107`), `_gh_pr_with_all_comments_rest` (`:1440-1447`) and the timeline helpers (`:1646`, `:1660`, `:1764`).
    - If this script ever calls one of them, the call would write to a file literally named `gh`, return empty stdout, and succeed.
    - It is latent today: the only `gh_helpers.sh` function the script calls is `extract_repo_scoped_issue_refs_from_text` (`:265`), which makes no API call.
    - It also breaks §10's rule that a new identifier must not shadow an existing one.
  - **Recommended fix:** Rename the local wrapper to a unique name, for example `_rcpm_gh_to_file()`, and update the 7 call sites. Do not shadow shared helpers.

- **BUG-007** — `.github/workflows/cancel_on_pr_close.yml:364-380`, `:18`
  - **Severity:** Low
  - **Category:** `bug`
  - **Description:** The cancel loop discards all output (`gh_retry gh api -X POST … >/dev/null 2>&1`). Every failure is logged as "could not be canceled (likely already completed)".
    - The token falls back to `secrets.GITHUB_TOKEN` when `GH_PAT` is absent (`:18`).
    - A 403 from a consumer caller that lacks `actions: write` is therefore misreported as a benign race. That is a silent failure.
  - **Recommended fix:** Capture stderr to a temp file and use `GH_API_RETRY_LAST_STATUS` through `gh_api_retry` (`gh_helpers.sh:910`). Print the status in the existing warning line; a 409 means already completed, while 403 or 404 should emit `::warning::`.

- **BUG-008** — `.github/workflows/cancel_on_pr_close.yml:14-16`; `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
  - **Severity:** Low
  - **Category:** `bug`
  - **Description:** `pull_request: closed` runs and 5-minute `schedule` runs share one job concurrency group (`cancel-on-pr-close-${{ github.repository }}`, `cancel-in-progress: false`).
    - Only one run can be pending per group. A schedule tick that queues behind an active run therefore cancels a pending PR-close run.
    - The PR-specific branch filter (`:317-345`) is then lost. The scheduled global scan (`:258-316`) skips runs with no PR linkage (`:261-285`).
    - Inference: this is consistent with the 3 cancelled `cancel_on_pr_close` runs this report records [NEEDS VERIFICATION].
  - **Recommended fix:** Key the group by event, for example `cancel-on-pr-close-${{ github.repository }}-${{ github.event_name }}`. Cancels and the merge-train release are idempotent, so allowing one close run and one schedule run at the same time is low risk.

- **SEC-001** — `.github/workflows/clarify.yml:69-75`; `.github/workflows/implement.yml:1958-1971`; `.github/workflows/plan.yml:2936`
  - **Severity:** Low
  - **Category:** `security`
  - **Description:** Several run bodies interpolate event data or step outputs directly instead of passing them through `env:`. None is exploitable today:
    - `clarify.yml:73`: `echo "Comment author: ${{ github.event.comment.user.login }}"`. GitHub logins are restricted to a safe character set.
    - `implement.yml:1964`, `:1969`, `:1971`: interpolate `steps.baseline_refctx.outputs.branch`. The value is regex-validated at `:1167` (`ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+`).
    - `plan.yml:2936`: builds JSON by string interpolation of `steps.parse_plan.outputs.*`.
    - Each is safe only because of an upstream invariant that is not visible at the point of use.
  - **Recommended fix:**
    - Move these values to step `env:`, using the pattern already applied at `test-and-mark-stable.yml:1851`: "Promoted to env so the run: script body contains no ${{ }}".
    - Build the `plan.yml:2936` JSON with `jq -cn --arg`.

### Section 2: GitHub API Call Redundancy Audit

- **API-001** — `scripts/review_collect_pr_metadata.sh:231-248`, `:276-292`
  - **Severity:** Medium
  - **Category:** `api-redundancy`
  - **Description:** Each review run makes these calls:
    - `GET pulls/{n}` (`:231`).
    - A paginated `GET issues/{n}/comments` (`:235`).
    - An optional paginated `GET pulls/{n}/reviews` (`:240`).
    - A paginated `GET pulls/{n}/comments` (`:247`).
    - A separate GraphQL `closingIssuesReferences` query (`:280-285`).

    That is at least 4 calls, or 5 with break-glass. The PR title, body, refs and head repo needed for `PR_META_FILE` (`:250-256`) could come from the GraphQL query that already runs. `gh_pr_with_all_comments` (`gh_helpers.sh:1491-1630`) already fetches the PR, issue comments and review comments in one GraphQL call with a REST fallback, but it returns a normalized shape.
  - **Calls:** current ≥4 → proposed 3, by folding the PR fields into the `:280` query. It could go to 2 if the comment consumers accept the normalized `gh_pr_with_all_comments` shape.
  - **Recommended fix:** Extend the `:284` query with `title body baseRefName headRefName headRepository{nameWithOwner} headRefOid`.
    - Keep the REST `GET pulls/{n}` only where `PR_PAYLOAD_FILE` needs the raw REST shape. `review_single_issue_security_pass.sh:320` reads it.
    - [NEEDS VERIFICATION]: which `PR_PAYLOAD_FILE` fields its consumers read.

- **BATCH-001** — `scripts/review_merge_train.sh:313-352` (`:340`); cache at `:215-228`; batch helper at `:487-534`
  - **Severity:** Low
  - **Category:** `api-batching`
  - **Description:** `_mt_blockers_for_into` makes one paginated `GET pulls/{n}/files` per older same-base PR inside its candidate loop. It is cached per process by `_MT_FILES_CACHE`.
    - On a first evaluation that is up to `MT_MAX_OLDER` REST calls. `MERGE_TRAIN_MAX_OLDER_PRS` defaults to `20` (`cancel_on_pr_close.yml:400`); the mapping between the two is an inference.
    - The release path (`:1379`) adds one more call per queued PR.
    - The same file already has an aliased GraphQL prefetch, `_mt_pr_meta_prefetch` (`:487`, `:526`).
  - **Calls:** current up to 21 per queued PR → proposed `ceil(N/25)` GraphQL calls, plus a REST fallback only for PRs whose `files.pageInfo.hasNextPage` is true.
  - **Recommended fix:** Add `files(first:100){nodes{path} pageInfo{hasNextPage}}` to the `_mt_pr_meta_prefetch` aliases. Populate `_MT_FILES_CACHE` from that response before the Phase A loop, and keep `_mt_pr_files_into` as the per-PR fallback.

- **BATCH-002** — `scripts/orchestrate_poll_process.sh:46-58`
  - **Severity:** Low
  - **Category:** `api-batching`
  - **Description:** `replay_failed_reclarify_commands` lists queued issues with `gh issue list --json number` (`:46-47`). It then makes one `GET issues/{n}` per issue (`:55`) only to check state and labels (`:56-57`), plus a paginated comments read per issue (`:58`). The header (`:37-41`) documents this as deliberate.
  - **Calls:** current `2 + 2N` (rate limit, list, then N issue reads and N or more comment reads) → proposed `2 + N`.
  - **Recommended fix:** Request `--json number,state,labels` in the existing list call and drop the per-issue GET. Keep the full comment pagination, because `:59` requires complete history. [NEEDS VERIFICATION]: whether the seconds-old list snapshot is acceptable in place of the "live issue read" contract.

- **BATCH-003** — `.github/workflows/implement.yml:5544-5562`
  - **Severity:** Low
  - **Category:** `api-batching`
  - **Description:** The ancestor-chain no-op cap makes two calls per hop: `GET issues/{n}` for the body and a paginated `GET issues/{parent}/comments`. With the default `IMPL_NOOP_ANCESTRY_THRESHOLD=2` (`:5405`), that is up to 4 calls.
  - **Calls:** current `2 × threshold` → proposed `threshold`.
  - **Recommended fix:** Use one GraphQL call per hop: `issue(number:$n){body comments(last:100){nodes{body}}}`. Fall back to the REST pair when `comments.totalCount > 100`.

- **API-002** — `scripts/workflow_failure_heal_pr_reconcile.sh:96-101`, `:239-247`
  - **Severity:** Low
  - **Category:** `api-redundancy`
  - **Description:** There is one `GET pulls?head=` per matching heal issue inside the loop (`:243`). Heal PRs are identified by base equal to the source head ref, or the source base after a merge (`:252`). The noop path (`:103-105`) still pays for the full-history checkout, as Speed Optimizations item 3 in this report already notes.
  - **Calls:** current `1 + N` → proposed `1 + 1`.
  - **Recommended fix:** Make one `GET pulls?state=open&base=${HEAD_REF}&per_page=100`, plus `base=${BASE_REF}` when `MERGED=true`. Filter heads matching `ai/issue-<heal_issue>` in jq.

- **API-003** — `.github/workflows/implement.yml:438-480`, `:741-782`, `:1322-1365`
  - **Severity:** Low
  - **Category:** `api-redundancy`
  - **Description:** Three "Resolve workflow support ref" steps in the same `implement` job (the job spans `:92-7035`) each recompute the same value. Each one may call `GET repos/…/branches/promote-candidate`; the identical regexes are at `:453`, `:758` and `:1337`.
  - **Calls:** current up to 3 per run on the promote-candidate path → proposed 1.
  - **Recommended fix:** Give the first resolver an `id` and a `script_ref` output. Have the later steps read `steps.<id>.outputs.script_ref`, or `SCRIPT_REF` from `GITHUB_ENV` once the earliest copy has written it. Fall back to their own resolution only when that value is empty, because the duplicate-PR and CI-triage steps are conditional.

- **API-004** — `.github/workflows/plan.yml:2664`; `scripts/orchestrate_poll_process.sh:44`, `:157`
  - **Severity:** Low
  - **Category:** `api-redundancy`
  - **Description:** Two duplicate reads:
    - `plan.yml:2664` fetches every issue comment again even though `ISSUE_COMMENTS_FILE` was fetched at `:714`. It may need comments posted since then, such as the plan comment [NEEDS VERIFICATION].
    - The poller reads `gh api rate_limit` twice per tick, in `replay_failed_reclarify_commands` (`:44`) and `detect_unrouted_blocked_comments` (`:157`). `/rate_limit` does not use core quota, so the cost is latency only.
  - **Calls:** 1 avoidable comments pagination per plan run (conditional on the above), and 1 `rate_limit` read per tick.
  - **Recommended fix:**
    - Re-fetch only comments newer than the cached file's newest `created_at`, using `since=`.
    - Cache the `rate_limit` reading in a tick-local variable shared by both functions.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — "Resolve workflow support ref" inline block: 21 copies across 13 workflows
  - **Severity:** Medium
  - **Category:** `duplication`
  - **Description:** The promote-candidate and pinned-SHA support-ref resolver is copied inline. Each copy emits `SUPPORT_REF source=promote_candidate_unprotected`. Locations:
    - `validate.yml:220`, `:487`
    - `cancel_on_pr_close.yml:51`
    - `orchestrate.yml:222`, `:396`
    - `implement.yml:464`, `:769`, `:1348`, `:7090`
    - `workflow_failure_heal.yml:126`
    - `check_failure_triage.yml:331`
    - `memory_maintenance.yml:38`
    - `orchestrate_clarify_respond.yml:413`
    - `orchestrate_poll.yml:143`, `:539`
    - `clarify.yml:401`, `:2369`
    - `review_autofix.yml:436`
    - `plan.yml:368`, `:3178`
    - `issue_pr_status.yml:1101`

    Each copy is about 40 lines (see `cancel_on_pr_close.yml:25-67`) and differs only in the workflow file name inside its regex. A security fix to the promote-candidate trust rule has to be repeated 21 times.
  - **Recommended fix:** Add `scripts/resolve_workflow_support_ref.sh <workflow_basename>`.
    - Inputs: `SUPPORT_REF_WORKFLOW_JOB_JSON`, `GITHUB_REPOSITORY` and `GITHUB_SHA`.
    - Output: `SCRIPT_REF=` appended to `GITHUB_ENV`, plus the same `SUPPORT_REF …` log lines so the contract in §10 holds.
    - Delivery: a SHA-pinned sparse checkout of the workflow commit, the same pattern as "Checkout head-gate helper" (`review_autofix.yml:566-612`; `agents.md:2557-2567`).
    - [NEEDS VERIFICATION]: that `job.workflow_sha` is populated for cross-repo `workflow_call`, and whether any contract test asserts the inline text.

- **DUP-002** — `scripts/gh_helpers.sh:1060-1138` (`gh_retry`), `:1149-1222` (`gh_retry_to_file`), `:1271-1335` (`gh_api_json_to_file`), `:910-1035` (`gh_api_retry`)
  - **Severity:** Medium
  - **Category:** `duplication`
  - **Description:** Three near-identical attempt loops, each with the same permanent-failure, rate-limit and exponential-backoff branches and their own temp-file handling, plus a fourth retry engine (`gh_api_retry`) with a different classifier.
    - Rate-limit handling has already drifted. The three `gh_retry`-family loops trip the breaker for a fixed `+60` seconds (`:1111`, `:1197`, `:1309`). `gh_api_retry` derives the wait from the reset header (`:997-1006`).
  - **Recommended fix:** Add an internal `_gh_retry_core <mode:stdout|file|json> <outfile|-> <cmd…>` that owns the loop. Keep all four public names and signatures as thin wrappers, per §10. Callers do not change.

- **DUP-003** — `.github/workflows/implement.yml:5441-5450`, `:5488-5495`, `:5575-5584`, `:5626-5633`, `:6506-6509`, `:6558-6561`, `:6735-6738`, `:6776-6779`; also `review_autofix.yml` (4 sites), `workflow-log-analysis.yml` (3) and `clarify.yml` (1)
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** 16 inline `gh label create … || true` calls, each followed by `gh issue edit --remove-label … --add-label … || gh issue edit --add-label …`. Meanwhile `ensure_label_exists` (`label_helpers.sh:163-197`) and `set_issue_phase_label_resilient` (`:205-269`) already implement catalog-backed creation and phase-label replacement. Color drift caused by this duplication is CONSIST-001.
  - **Recommended fix:** Add `transition_issue_labels <issue> <repo> <add_label> [remove_label…]` to `label_helpers.sh`, built on `ensure_label_exists` plus one PUT or POST, as in `set_issue_phase_label_resilient`. Replace the implement.yml sites first.

- **DUP-004** — inline GH_PAT budget snapshot: 19 copies
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** The inline `reading="$(gh api rate_limit --jq …)"` snapshot appears in `review_autofix.yml` (12 copies), `clarify.yml` (3, e.g. `:77-89`), `workflow-failure-heal-intake.yml` (2), `validation-improvements-intake.yml` (1) and `orchestrate_poll.yml:102-110` (1). It duplicates `gh_pat_budget` (`gh_helpers.sh:31-77`).
    - The semantics differ: the inline copies read the `/rate_limit` body, while the helper prefers the core `x-ratelimit-*` headers (`:50-60`).
  - **Recommended fix:** Where the helper is already staged, source it, as `review_autofix_sweep.yml:133-134` does. Keep the inline copy only for "start" snapshots that run before staging, and align its parsing with the helper's.

- **DUP-005** — `.github/workflows/review_autofix.yml:4155-4166`, `:4901-4909`, `:4960-4961`, `:5051-5052`, `:5204-5209`, `:5504-5517`; raw Telegram `curl` in `test-and-mark-stable.yml:7773`, `validation-refresh.yml:353`, `implement.yml:5511`, `check_failure_triage.yml:716`, `update_workflows.yml:1741`, `:1772` and `issue_pr_status.yml:969`
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** Six copies of the `PR_URL` / `RUN_URL` / `MSG` construction for Telegram, plus seven raw `curl` sends that bypass `tg_send_msg` (`tg_helpers.sh:108`). `implement.yml:5500-5501` documents why its raw send exists.
  - **Recommended fix:** Add `tg_send_pr_event <kind> <pr_number> [issue_number]` to `tg_helpers.sh`, built from `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID`. Keep raw `curl` only where the helper may already be cleaned up.

### Section 4: Expression Size Limit Risk Assessment

Method: a run body that contains any `${{ }}` is evaluated as one expression. Character counts are estimated as line count × the average line length sampled in each block (about 70–80 characters). Every workflow below currently loads, so every block is under 21,000 characters. [NEEDS VERIFICATION]: exact counts.

- **EXPR-001** — `.github/workflows/test-and-mark-stable.yml:954-1231` ("Phase 2: Wait for plan to complete", `run:` at `:962`)
  - **Severity:** High
  - **Category:** `expression-limit`
  - **Description:** About 270 lines with `${{ steps.create-issue.outputs.created_after }}` at `:980`, `:992` and `:1010`. Estimated 17,000–20,000 characters, leaving about 1,000–4,000 of headroom. The comments inside the block keep growing with each incident note (for example `:983-1005` and `:1028-1033`).
  - **Recommended fix:** Add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step `env:` and use `${CREATED_AFTER}` in the body. That removes the expression entirely, the same fix already used at `:1851` and `:2683`. Apply it to Phase 1 (`:848`) as well.

- **EXPR-002** — `.github/workflows/test-and-mark-stable.yml:1261-1551` ("Phase 3b", `run:` at `:1269`)
  - **Severity:** High
  - **Category:** `expression-limit`
  - **Description:** About 283 lines with one expression at `:1307` (`IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"`). Estimated 18,000–21,000 characters, leaving roughly 0–3,000 of headroom.
  - **Recommended fix:** Move the value to `env: IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` and delete the `:1307` assignment. The body then contains no expression.

- **EXPR-003** — `.github/workflows/implement.yml:5398-5634` ("Handle no-op implementation")
  - **Severity:** Medium
  - **Category:** `expression-limit`
  - **Description:** About 228 lines with long prose lines and `${{ github.* }}` at `:5411`, `:5413`, `:5616`, `:5624`, `:5626`, `:5630` and `:5632`. Estimated 14,000–17,000 characters, leaving about 4,000–7,000 of headroom.
  - **Recommended fix:**
    - Replace the expressions with the runner's default environment: `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"` and `REPO="${GITHUB_REPOSITORY}"`. That removes the expression.
    - Optionally also move the comment templates to `prompts/` or to a `scripts/implement_noop_handler.sh`.

- **EXPR-004** — `.github/workflows/plan.yml:2137-2342` ("Guard plans targeting template-owned paths")
  - **Severity:** Medium
  - **Category:** `expression-limit`
  - **Description:** About 200 lines, about 35 of them comment (`:2143-2180`), with `${{ github.repository }}`, `${{ github.server_url }}` and `${{ github.run_id }}` at `:2334`, `:2336` and `:2338`. Estimated 14,000–16,000 characters.
  - **Recommended fix:** Use `GITHUB_REPOSITORY`, `GITHUB_SERVER_URL` and `GITHUB_RUN_ID` instead. Optionally move the body to `scripts/plan_template_path_guard.sh`, which `plan.yml` already stages through `scripts/`.

- **EXPR-005** — `.github/workflows/test-and-mark-stable.yml:7541-7776` ("Send Telegram notification"); Phase 1 at `:800-939`
  - **Severity:** Low
  - **Category:** `expression-limit`
  - **Description:** The Telegram step is about 220 lines and contains more than 20 `needs.*.result` and `needs.*.outputs.*` expressions (`:7564-7584`, `:7613-7656`, `:7769`). Estimated 12,000–13,000 characters; the comment block at `:7713-7745` alone is about 2,500. Phase 1 is about 130 lines, roughly 9,000–11,000 characters. Both are below the 15,000 threshold today, but they grow with each incident comment.
  - **Recommended fix:** Move the `needs.*` values into step `env:`, for example `E2E_RESULT: ${{ needs.e2e-smoke-test.result }}`. Move the 30-line truncation comment to a script header.

Workflow file size: GitHub's measured hard limit for this repo is 512,000 bytes, not 1 MB (`agents.md:2506-2512`). The CI guard fails at 480,000 bytes (`tests/test_workflow_file_size_limit.py`), and `budget-watch` warns at 440,000. No workflow can be over 800 KB and still run. The largest by line count are `test-and-mark-stable.yml` (7,776 lines), `implement.yml` (7,136) and `review_autofix.yml` (5,749); their byte sizes were not measured in this run. CONSIST-002 also removes the `${{ }}` wrapping from many run bodies, which is the root cause of EXPR-003 and EXPR-004.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — `.github/workflows/pr6576-shard-followup.yml:14-23`, `:40-64`, `:66-113`; `docs/scripts-pending-removal.md:331-340`
  - **Severity:** Medium
  - **Category:** `dead-code`
  - **Description:** A single-use workflow for issue #7072 still triggers on every `push` to `main` (`:15-16`).
    - Each push queues the `self-test` job (`pip install` plus pytest, `:40-64`) and then the `locate` job (checkout, Python setup and a guard API call, `:92-97`). Both run even after the one-time marker has been posted.
    - That is 2 extra hosted-runner jobs per main push, in a window where this report measured about 84% of wall-clock as runner queue time.
  - **Recommended fix:**
    - Run the registry's removal preflight checks (`:337-339`).
    - If they pass, delete the workflow, `scripts/pr6576_shard_followup.py`, the test file and the registry entry in one PR, per §20.F.
    - Otherwise, add `paths:` to the `push` trigger, matching the `pull_request` paths at `:19-22`.
    - [NEEDS VERIFICATION]: whether the marker comment exists.

- **DEBT-001** — `scripts/orchestrate_poll_process.sh:1-21`
  - **Severity:** Low
  - **Category:** `tech-debt`
  - **Description:** The poller is now 30,821 lines. Its header (`:12-19`) cites about 27,700 lines when ShellCheck's extended analysis had to be disabled because it hung CI. The file keeps growing past the size where its own lint coverage was reduced.
    - No `TODO`, `FIXME` or `HACK` markers were found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.
    - `read_standalone_state_json` (`:16486`) is still definition-only. It is not re-raised here because it was rejected earlier (`analysis/recommendation-processing-report.md:1207`).
  - **Recommended fix:** Split self-contained prologue functions into sourced modules. `replay_failed_reclarify_commands` and `detect_unrouted_blocked_comments` (`:42-205`) are good candidates, for example `scripts/orchestrate_poll_reclarify.sh`, sourced the same way as `gh_helpers.sh` (`:27-30`). Then re-enable extended analysis on the extracted modules.

- **CONSIST-001** — `.github/workflows/implement.yml:5441-5443`, `:5488-5490`, `:6735-6737` vs `scripts/label_helpers.sh:48`, `:62`, `:115`
  - **Severity:** Low
  - **Category:** `consistency`
  - **Description:** Inline label creation uses colors and descriptions that differ from the catalog:
    - `ai:closed`: `6b7280` inline vs `6a737d` in the catalog.
    - `ai:needs-human`: `b60205` vs `e11d48`.
    - `ai:blocked`: "Blocked: human input required" vs "Orchestrator auto-answer loop blocked pending human intervention".
    - Whichever creator runs first in a consumer repo decides the label's metadata.
  - **Recommended fix:** Route these sites through `ensure_label_exists` (see DUP-003), so the catalog in `label_helpers.sh` is the single source.

- **CONSIST-002** — `${{ github.repository }}` and similar inside run bodies: about 70 sites in `implement.yml` (`:201`–`:7073`), about 50 in `plan.yml` (`:163`–`:3161`) and about 40 in `review_autofix.yml` (`:2122`–`:5517`)
  - **Severity:** Medium
  - **Category:** `consistency`
  - **Description:** Run bodies interpolate `${{ github.repository }}`, `${{ github.sha }}`, `${{ github.run_id }}`, `${{ github.server_url }}`, `${{ github.actor }}` and `${{ github.run_attempt }}`. The runner already exports each of these as `GITHUB_REPOSITORY`, `GITHUB_SHA`, `GITHUB_RUN_ID`, `GITHUB_SERVER_URL`, `GITHUB_ACTOR` and `GITHUB_RUN_ATTEMPT`. Each interpolation turns the whole body into an expression subject to the 21,000-character limit; this drives EXPR-003 and EXPR-004. Other steps in the same files already use the environment form, for example `orchestrate_poll.yml:89` and `lint-pr-body-auto-close.yml:85`.
  - **Recommended fix:** Mechanically replace them with the default environment variables, largest steps first. The values are identical in `workflow_call` because both read the caller's `github` context. `${{ job.status }}` and `${{ steps.* }}` have no default variable; move those to `env:`.

- **CONSIST-003** — `.github/workflows/plan.yml:688` vs `implement.yml:246`; `implement.yml:2229` vs `plan.yml:714`; comment-pagination variants
  - **Severity:** Low
  - **Category:** `consistency`
  - **Description:** The same endpoint is fetched in different ways:
    - Smoke-tracking issue lookup: raw `gh api` in `plan.yml:688`, but `gh_api_retry --optional` in `implement.yml:246`.
    - Issue comments: raw `gh api --paginate | jq -s` in `implement.yml:2229`, but `gh_retry` in `plan.yml:714`.
    - Comment pagination has three forms: `--slurp`, `| jq -s 'add // []'`, and raw output (BUG-002).
    - Release-supersede lookup: raw `gh api` at `test-and-mark-stable.yml:7645`.
  - **Recommended fix:**
    - Standardize on `gh_retry gh api --paginate "…" | jq -s 'add // []'` for list endpoints, and `gh_api_retry --optional` for best-effort single reads.
    - Add a CI grep guard for `--paginate` writes to a file without `--slurp`, `--jq` or `jq -s`.

- **CONSIST-004** — `.github/workflows/implement.yml:5408-5409`, `plan.yml:712-713`, `:2328-2329`, `cancel_on_pr_close.yml:147-148`; `scripts/workflow_failure_heal_pr_reconcile.sh:51-55`
  - **Severity:** Low
  - **Category:** `consistency`
  - **Description:** These sites run `source …/gh_helpers.sh 2>/dev/null || true` followed by `type gh_retry … || gh_retry() { "$@"; }`. If sourcing fails, retry, the POST-once safety and the rate-limit breaker are all silently disabled, and nothing is logged.
  - **Recommended fix:** Inside the fallback branch, emit one `::warning::gh_helpers.sh not loaded; gh_retry running without retry` line. Use plain `::warning::` with no new log prefix, so no `agents.d` registry entry is needed (§25).

- **SHELL-001** — `scripts/review_merge_train.sh:1323`
  - **Severity:** Low
  - **Category:** `shellcheck`
  - **Description:** `for wf in ${MERGE_TRAIN_DISPATCH_WORKFLOWS:-ai-review.yml internal-review.yml review_autofix.yml}; do` relies on unquoted word splitting (SC2086). A value containing a glob character would expand against the working directory.
  - **Recommended fix:** `read -ra _mt_dispatch_wfs <<< "${MERGE_TRAIN_DISPATCH_WORKFLOWS:-ai-review.yml internal-review.yml review_autofix.yml}"`, then `for wf in "${_mt_dispatch_wfs[@]}"`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 4 | BUG-001, BUG-002, EXPR-001, EXPR-002 |
| Medium | 11 | BUG-003, BUG-004, BUG-005, BUG-006, API-001, DUP-001, DUP-002, EXPR-003, EXPR-004, DEAD-001, CONSIST-002 |
| Low | 17 | BUG-007, BUG-008, SEC-001, BATCH-001, BATCH-002, BATCH-003, API-002, API-003, API-004, DUP-003, DUP-004, DUP-005, EXPR-005, DEBT-001, CONSIST-001, CONSIST-003, CONSIST-004 |

SHELL-001 (Low) brings the total to 33 findings.

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/plan.yml` | Small |
| API call optimization | `scripts/review_collect_pr_metadata.sh`, `scripts/review_merge_train.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/implement.yml`, `.github/workflows/plan.yml` | Medium |
| Code modularization | 13 workflows holding the support-ref block, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/tg_helpers.sh`, `.github/workflows/review_autofix.yml`, `.github/workflows/implement.yml` | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/plan.yml`, `.github/workflows/review_autofix.yml` | Medium |
| Medium/Low fixes | `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `scripts/check_integration_pr_readiness.py`, `.github/workflows/review_autofix.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/internal-cancel-on-pr-close.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/pr6576-shard-followup.yml`, `scripts/review_collect_pr_metadata.sh`, `scripts/review_merge_train.sh`, `.github/workflows/clarify.yml` | Medium |