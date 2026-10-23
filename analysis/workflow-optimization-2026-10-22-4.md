## Executive Summary

**Data caveat:** The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox, so no `errors/`, `slow/` or `recent/` deep-dive logs could be read. The evidence comes from `analysis_context.json` (aggregates plus the `log_summary` on each run) and from the repository source. The window holds 1,000 runs from 2026-10-22 09:14Z to 18:41Z, only about 9.5 hours, because 646 of the 1,000 runs (64.6%) are `skipped`/other. `insufficient_data=false`.

- **One release-path failure has no diagnosis, and the cause is a logging gap.** Promote run 39774429376 failed at `promote / Fast-forward stable to main` after 158s. In `.github/workflows/promote-main-to-stable.yml` (lines 271 and 277), `git merge --ff-only` and `git push origin "HEAD:refs/heads/stable"` are the only exits in that step without an `::error::` annotation, and the step emits no structured outcome line. Adding a single `PROMOTE_STABLE_FF outcome=… reason=…` line makes every future failure diagnosable from the summary alone. Impact: the promote failure rate is 1/1 in this window. Confidence: high on the gap, low on which branch actually failed.
- **The two CI failures were a real regression, and the check-failure triage loop fixed it.** Runs 39800256248 (14:57Z) and 39807709623 (15:44Z) both failed at `tests-release-and-log-analysis / Promote cycle and blocked-verdict unit tests` on PR #7889. Triage run 39809895946 deduplicated onto issue #7890 (`skip reason=duplicate_open_issue issue=7890`). The fix PR #7891 merged into `ai/issue-7888` around 15:56Z, and the next CI run, 39809580977, passed. No infrastructure change is needed. Confidence: medium (the sequence is inferred from run timestamps and lines in the summaries).
- **Hosted-runner queueing is the largest avoidable latency on the critical path.** In review 39807709608, `codex-agent` waited about 11m42s (15:47:08 to 15:58:50) for a runner, during the same minutes that `deterministic-skip-merge` (39807273046) held a runner for about 14.7 minutes polling CI and two CI fan-outs (39807272915, 39807709623) were running. A smaller signal: promote-follow 39814723779 ran 581s for about 4.5s of actual work. Estimated saving is 5–12 minutes per contended PR cycle. Confidence: medium (the correlation is inferred).
- **Review/autofix accounts for 99.5% of logged model tokens: 20.9M of 21.0M.** The prompt-cache hit rate is 0.678 overall, but it varies by run, from 0.600 in 39816960064 to 0.709 in 39809581147. `test_and_mark_stable` (39768897865) wrote 91,618 cache tokens across 12 calls and read none back (hit rate 0.0), so it paid the cache-write premium for nothing. Raising the worst review run to the median would avoid about 0.5M uncached tokens per run. Confidence: medium.
- **The orchestrator poller cannot be observed in this telemetry.** All 13 poll runs with summaries had about 852–853k characters truncated from the middle of the log, and no poll-cycle summary line exists in `scripts/orchestrate_poll_process.sh` (searched, no match). Poll p50 is 655s, 9 of 60 runs were cancelled, and wave progression, GH API spend and AI-memory telemetry are all invisible. Confidence: high.
- **Model spend for non-review phases does not appear.** Implement 39805819469 resolved `AI_ENGINE_RESOLVED_*: claude` and ran 1,142s, yet its row shows zero tokens, and the run rows here carry no `claude_*` fields even though `scripts/cost_audit.py` aggregates `CLAUDE_COUNT_KEYS`. Implement, plan and clarify spend is therefore unmeasured. Confidence: high.
- **Semble works when it runs, but a third of the bootstraps were wasted.** The 3 review queries returned 42,840 bytes from 25 sources with 0 static-duplicate bytes, which is clean. Workflow Log Analysis run 39769243471 bootstrapped Semble 3 times without ever using it, costing 119,317 ms (46.7% of all bootstrap time). Confidence: high.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Stop `deterministic-skip-merge` from holding a runner while it polls CI. Critical path.**
   - **Evidence:** In run 39807273046, `deterministic-skip-merge` logged from 15:42:18 to about 15:57:02 (about 14.7 minutes) with `AUTO_MERGE_CHECKS_WAIT_MINUTES: 45` and `AUTO_MERGE_CHECKS_POLL_SECONDS: 60`, waiting on CI run 39807272915 (816s). In the same window, review 39807709608's `codex-agent` waited about 11.7 minutes for a runner.
   - **Root cause:** A runner sits idle in a polling loop, which adds to runner contention (inference).
   - **Change:** When `ENABLE_AUTO_MERGE=true` and branch protection requires `CI`, call `gh pr merge --auto` and exit, keeping the current polling path as the fallback. Gate it behind a new defaulted variable, for example `DET_SKIP_NATIVE_AUTO_MERGE_ENABLED` (default `false`, then flip it).
   - **Savings:** About 14 runner-minutes per small-diff merge, and possibly 5–12 minutes of queue time removed for concurrent review jobs.
   - **Risk:** Medium. The merge-train and freshness-sync logic (`MERGE_BASE_FRESHNESS_MAX_SYNCS: 2`) must still run, so it stays behind the flag.
2. **Measure runner queue time per job before tuning concurrency. Diagnostic, critical path.**
   - **Evidence:** Waits of 11m42s (39807709608), 2m25s (39826214109), 74s (39809349590), and an unexplained gap of about 576s in 39814723779.
   - **Root cause:** Queue wait is only visible as free text in the summaries.
   - **Change:** In `scripts/collect_workflow_logs.py`, use the jobs payload it already fetches to derive `failure_point` and emit `queue_seconds = started_at - created_at` for each job. No new API call should be needed; confirm that before merging (§14).
   - **Savings:** Indirect. This lets the next report rank contention sources.
   - **Risk:** Low.
3. **Drop the full-history tag fetch from `heal-pr-reconcile`. Off the critical path.**
   - **Evidence:** In runs 39809570090 and 39820953086, the reconcile work took about 1s, but checkout produced about 564k truncated characters of `[new tag]` lines and about 2 minutes of the 132–142s runs. `.github/workflows/internal-cancel-on-pr-close.yml:50` sets `fetch-depth: 0`. The script already fetches the refs it needs with `--no-tags` (`scripts/workflow_failure_heal_pr_reconcile.sh:159,165`).
   - **Change:** The smallest safe change is to add a cheap pre-step that exits early when the closed PR has no heal PR (the common `noop reason=no_heal_issue` case) before the full checkout. Reducing the depth outright is riskier, because the rebase path may need full history.
   - **Savings:** About 2 minutes per PR close.
   - **Risk:** Low with the early-exit pre-step.
4. **Skip the eager Semble bootstrap in Workflow Log Analysis. Micro.**
   - **Evidence:** Run 39769243471 had 3 bootstraps, all unused, costing 119,317 ms.
   - **Change:** Switch that workflow to `SEMBLE_BOOTSTRAP_MODE=lazy`, which the poll and review runs already use.
   - **Savings:** About 2 minutes per analysis run.
   - **Risk:** Low.
5. **Considered and deferred: CI `cancel-in-progress`.** `ci.yml` has no `concurrency:` block. Run 39807709623 on head `ce39845c` kept running for about 2 minutes after head `801a40e5` was pushed. However, `ci_cancelled_rerun.py` and the auto-merge wait both depend on how CI conclusions behave today, so adding cancellation risks a rerun loop. Not recommended this cycle.

## Cost Optimizations

1. **Stop paying for cache writes that are never read in `test_and_mark_stable`.**
   - **Evidence:** Run 39768897865 made 12 calls with `or_prompt_tokens=94,735`, `or_cache_write_tokens=91,618` and `or_cache_read_tokens=0`.
   - **Root cause:** Either each call has a unique prefix or the calls are one-shot, so the cache-write premium is pure overhead.
   - **Change:** Either move the shared static instructions to the front so the prefix is identical across the 12 calls, or remove cache-control breakpoints for these one-shot calls.
   - **Savings:** Roughly 91k tokens per release billed at write rates instead of base rates (inference: the cache-write premium is usually about 1.25× the input rate).
   - **Quality risk:** None.
2. **Reduce variance in the review/autofix cache hit rate.**
   - **Evidence:** Hit rates were 0.600 in 39816960064 (1,757,060 uncached prompt tokens), 0.704 in 39800256892 and 0.709 in 39809581147. Each run makes about 11 calls averaging about 509k input tokens per call.
   - **Root cause:** Unknown. Prefix fragmentation is likely, for example PR-specific diffs or timestamps placed before static rubric text (inference).
   - **Change:** Use the per-phase `or_phases` breakdown that `cost_audit.py` already aggregates to find which phase misses. Then move dynamic blocks after the static system and rubric prefix.
   - **Savings:** Bringing the 0.600 run up to 0.70 moves about 0.5–0.6M tokens per run from uncached to cached reads.
   - **Quality risk:** None, since only ordering changes.
3. **Make non-review AI spend visible. This is a prerequisite.**
   - **Evidence:** The implement, plan and clarify families show 0 `or_*` and 0 `codex_*` tokens, but 39805819469 ran on the claude engine for 1,142s. Run rows carry no `claude_calls` or `claude_cost_usd`.
   - **Change:** Have the collector copy the `CLAUDE_COUNT_KEYS` and `claude_cost_usd` fields that `cost_audit.py` already computes into each run's `cost_telemetry`. This is an additive JSON field (§10).
   - **Savings:** Not direct. It unlocks cost analysis for roughly 8–10 implement runs per window at 1,100–1,400s each.
4. **Semble:** Keep it. The 3 queries (`target` not exposed in the aggregates) added 14.3 KB on average with 8.3 sources each and 0 static-duplicate bytes, so it is not adding noise. The only waste is the unused bootstrap above. **Serena:** disabled (`SERENA_ENABLED: false`), with no queries or probes, so there is nothing to assess.
5. **Reasoning level:** The configuration shows `MODEL_REASONING_EFFORT_JUDGE: high` and `MODEL_EDITOR: openai/gpt-6-sol`. There is no per-model token split in this window, so no change is recommended until item 3 lands.

## Reliability Improvements

1. **Annotate the two unannotated exits in the promote fast-forward step and add a structured outcome line.**
   - **Failure evidence:** Run 39774429376 failed at `promote / Fast-forward stable to main` with no excerpt available. The step has 9 annotated `::error::` exits (`promote-main-to-stable.yml` lines 229–313). `git merge --ff-only "${TARGET}"` (line 271) and `git push origin "HEAD:refs/heads/stable"` (line 277) fail silently under `set -e`.
   - **Root cause category:** Observability gap. The underlying cause is unknown. One possibility (inference only) is a push race, since `test_and_mark_stable` 39768897865 was running from 11:12Z to about 12:50Z, overlapping the promote.
   - **Fix:** See the code below. The new prefix `PROMOTE_STABLE_FF` has 0 existing matches in the repo. Register it through a fragment at `agents.d/<pr>-promote-stable-ff-log.md` under `Stable log prefixes (contractual)` (§25).
   - **Impact:** Every future promote failure becomes triageable from one line.
   - **Rollback:** Logging only, with exit codes unchanged.
   ```bash
   if ! git merge --ff-only "${TARGET}" 2>/tmp/_ff_err; then
     echo "PROMOTE_STABLE_FF outcome=fail reason=merge_ff_only before=${BEFORE} target=${TARGET}"
     echo "::error::git merge --ff-only ${TARGET} failed: $(tail -n 3 /tmp/_ff_err | tr '\n' ' ')"
     exit 1
   fi
   if ! git push origin "HEAD:refs/heads/stable" 2>/tmp/_push_err; then
     echo "PROMOTE_STABLE_FF outcome=fail reason=push before=${BEFORE} target=${TARGET} remote_now=$(git ls-remote origin refs/heads/stable | awk '{print $1}')"
     echo "::error::push to refs/heads/stable failed: $(tail -n 3 /tmp/_push_err | tr '\n' ' ')"
     exit 1
   fi
   ```
   Each existing `::error::` exit should also print a matching `PROMOTE_STABLE_FF outcome=fail reason=<no_stable_branch|ls_remote|bad_target_sha|target_not_on_main|stable_not_ancestor|post_verify>` line.
2. **CI regression in the promote-cycle tests.** Two failures on PR #7889, about 47 minutes apart, both in the same step. The category is a real test regression. The existing loop of triage, dedupe onto #7890, and fix PR #7891 resolved it, and the step already prints per-shard tails (`ci.yml:1751–1754`). No change is needed. The only follow-up is to confirm the triage issue closed when #7891 merged.
3. **Stale queued runs that can never be cancelled.** Every scheduled `cancel_on_pr_close` run (for example 39832749613, 39829522760 and 39826672335) warns `preserved 3 active run(s) without pull-request linkage` about runs 34748435167, 34748435180 (queued since 2026-09-13) and 32215723558 (queued since 2026-08-19).
   - **Fix:** Attempt a cancel on unlinked runs queued for more than N days, using a new defaulted variable (for example `CANCEL_UNLINKED_QUEUED_MAX_AGE_DAYS=14`), and log the outcome per run with the HTTP status. If GitHub rejects the cancel, the log line records that the run is a ghost.
   - **Impact:** Removes recurring warning noise. Risk is low, and the change can fail open.
4. **`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`:** zero in all 113 runs with telemetry, so there is no rubric or prompt-size pressure. **Semble fallbacks:** 25 in total. 24 are `contract_test` fallbacks (4 per CI run across 5 CI runs, and 4 in 39768897865), which is healthy test-fixture behaviour. 1 is a `runtime` fallback in implement run 39805819469, which is rare and fails open. The poller logs `SEMBLE_ENABLED: true` with `SEMBLE_AVAILABLE: false` in runs 39830964272, 39827396578 and 39826214109. With `lazy` mode and no `SEMBLE_FALLBACK` lines this is probably by design, but confirm the poller never needs Semble (low confidence). **Serena probes:** ok, failed and skipped are all 0.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were found. Every `log_summary` that mentions the field reports it absent, and the deep-dive logs were not mounted. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` entries, and push retries cannot be assessed.

Most likely the lines fall in the truncated middle of the poll logs (about 853k characters) and the `codex-agent` logs (1.1–1.7M characters), which is an inference. Two collection steps would close the gap:
1. Have the collector grep the untruncated logs for `AI_MEMORY_TELEMETRY:` before truncating, and add counts per `op` to `cost_telemetry`, as it already does for `SEMBLE_*`.
2. Verify emission with one manual review of the raw `codex-agent` log from 39816960064.

## GH API Call Audit

| Signal | Evidence | Assessment |
|---|---|---|
| PAT headroom | `remaining` ranged from 3,859 (39827596371, 18:00Z) to 4,954 (39807709608) | No 429s or secondary limits anywhere |
| Heaviest measured job | 39814995656 `codex-agent` `used_in_job=185` | Reasonable for a full review |
| Sweep | 39833189931 `used_in_job=2` | Efficient |
| `used_in_job=unknown` | 39827596371, 39816960064, 39807709608, 39807273046, 39809570600 | Usage cannot be measured |
| Expected 404 | `promote-cycle-frozen` tag lookup in every promote-follow run (39832070231, 39822472264, 39814723779) | One call per run; harmless but logged as `GH_API_RETRY outcome=permanent` |
| Poller | Not visible (truncated) | Largest unknown |

- **The `unknown` usage comes from the hourly window rolling over or from a missing start snapshot.** `scripts/gh_helpers.sh:68–71` computes `used` only when `reset` matches the start snapshot. Jobs that cross an hourly reset (39807709608 ran 15:47 to 16:11) therefore report `unknown`.
  - **Diagnostic fix:** Emit a separate line, `GH_PAT_BUDGET_DETAIL job=… used_reason=window_rollover|no_start_snapshot start_remaining=… start_reset=…` (0 existing matches). Do not append a field to the existing line: `scripts/collect_workflow_logs.py:112` anchors `used_in_job=([0-9]+|unknown)$`, so an extra trailing field would break parsing (§10).
  - **Better estimate:** On rollover, report `used_in_job≈(start_remaining) + (limit - remaining)` and label it as an estimate.
- **Poller API spend:** No batching recommendations are possible without visibility. The cycle summary below should include `gh_calls_used`, so the existing `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` caches can be checked for regressions. This also serves the §14 hygiene rules.
- **Scheduled `cancel_on_pr_close`:** It runs about every 10–20 minutes (14 sampled runs) and lists active runs each time while finding nothing to cancel. That is acceptable at the current headroom. No change.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.67162 across the repo and 0.677684 for `review_autofix`. Reads were 13,987,235 tokens, writes 1,556,981 and uncached prompt 5,281,891. The figure matches read / (read + prompt + write).
- **Fragmentation:**
  - The review spread from 0.600 to 0.709 suggests the cached prefix varies between runs (inference).
  - Each run writes about 485–493k new cache tokens across 11 calls. That is roughly 44k per call, consistent with PR-specific context being cached and never reused.
  - Recommendation: put the static system prompt, rubric and repo guidance first and the per-PR diff, comments and Semble output last, then compare `or_phases` hit rates before and after.
  - Expected impact: a hit rate of 0.70–0.72 across all review runs, and about 0.3–0.6M fewer uncached tokens per run.
- **`test_and_mark_stable`:** hit rate 0.0 with 91,618 tokens written (see Cost item 1).
- **Context budget:** `context_budget_warn_count=0`, so prompt growth is not eroding cache value.
- **Fail-open:** no cache-failure signals were observed. All 45 calls reported usage (`or_usage_unavailable_calls=0`).
- **Memory retrieval:** no telemetry (see AI Memory Health).

## Orchestrator Health

- **Poller cadence:** 60 runs, 51 successful and 9 cancelled. Durations: p50 655s, p95 1,038s. Sampled starts are 8–20 minutes apart (for example 17:16:37, 17:30:43, 17:42:17, 17:50:44, 17:59:04), so the poller holds a runner for about 70–90% of wall time (inference). Run 39769473400 was cancelled at `cancelled_before_first_step` after 855s, meaning it was superseded while still queued.
- **Observability gap, highest priority:** There is no cycle summary line. Add one final line in `scripts/orchestrate_poll_process.sh`, mirrored to `$GITHUB_STEP_SUMMARY`, so it survives head and tail truncation:
  ```bash
  echo "ORCHESTRATE_POLL_CYCLE_SUMMARY tracking_examined=${n_tracking} managed_examined=${n_managed} dispatched=${n_dispatched} deferred=${n_deferred} stalled=${n_stalled} waves_advanced=${n_waves} clarify_pending=${n_clarify} gh_calls_used=${gh_used:-unknown} elapsed_s=${SECONDS}"
  ```
  Register the prefix through an `agents.d/` fragment. Default every counter to `0` or `unknown` so the line is fail-open.
- **Indicators to track:** `dispatched/cycle`, `stalled`, `waves_advanced`, poll `elapsed_s` p95, cancelled poll count (9 per 60 now), and the `claude_pool_daily_due` output already set in each run.
- **Clarify, plan and implement fan-out:**
  - Most runs in these families are skipped (other): clarify 128/137, plan 120/127, implement 118/127, `orchestrate_clarify_respond` 124/126. Each issue event fires four workflows at once (for example 39821591431, 39821591558, 39821592946 and 39821591581 at 17:19:22Z).
  - Several skipped runs lasted a long time: 39820931391 took 4,688s, 39797226940 took 3,977s and 39803825978 took 1,982s. They were likely queued behind a workflow-level concurrency group (inference).
  - The cost is mainly that they use up about 65% of the collector's 1,000-run window.
- **Terminal and stuck states:** None observed. PR #7889 (issue #7888) went from its first CI failure at 14:57Z to merge at about 17:14Z (from 39820953086, `merged=true`), with 3 or more full review runs. That is about 2h17m end to end, including one triage and fix sub-cycle.

## Pipeline Flow Bottlenecks

| Stage | Compute | Queue | Retry/rework | Merge/conflict |
|---|---|---|---|---|
| clarify/plan | Real runs 893–1,368s (39796710495, 39776716880, 39803826801) | Skipped runs wait up to 1,982s | none seen | — |
| implement | 1,074–1,388s (39798273748, 39770818951, 39805819469) | — | 1 Semble runtime fallback | — |
| review/autofix | p50 272s, p95 2,756s; `codex-agent` up to 29.5 min (39816960064) | **11.7 min** (39807709608) | about 6 review runs for PR #7889 | det-skip merge polls 14.7 min (39807273046) |
| CI | p50 848.5s; test jobs take about 13–14 min before the `lint` gate | jobs queued in parallel | 2 real failures, fixed | — |
| release | `test_and_mark_stable` 5,864s; `validation_refresh` 2,455s | — | promote 1/1 failed | — |

Fixes in order of end-to-end impact:
1. Free runners held by det-skip polling and measure queue time (Speed 1 and 2).
2. Add the promote fast-forward diagnostics (Reliability 1), because the release path is blocked on an unknown cause.
3. Add the poller cycle summary.
4. Get per-job CI durations from the collector to find which of the six test jobs sets the roughly 13-minute critical path. The timeout is `timeout-minutes: 20` (`ci.yml:1531`).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** Hosted-runner queueing (up to 11.7 minutes), CI test jobs of about 13–14 minutes, review `codex-agent` sessions of 12–29.5 minutes, and poller cycles of about 11 minutes.
- **Top failure modes:**
  - The promote fast-forward failure (39774429376), with no diagnosis.
  - A promote-cycle unit-test regression (39800256248, 39807709623), now fixed through #7890 and #7891.
- **Highest-cost drivers:** Review/autofix, at 20.9M tokens and a 0.678 hit rate. Claude-engine phases are unmeasured.
- **Top 3 actions:**
  1. Add `PROMOTE_STABLE_FF` outcome lines and annotate the `git merge --ff-only` and `git push` exits.
  2. Add `ORCHESTRATE_POLL_CYCLE_SUMMARY` and the `GH_PAT_BUDGET_DETAIL` line.
  3. Move det-skip merges to native auto-merge behind a defaulted flag, and add `queue_seconds` for each job to the collector.

## Metrics Appendix

**Run outcomes (window 09:14Z–18:41Z, 1,000 runs, success sample rate 0.07)**

| Metric | Value |
|---|---|
| success / failure / cancelled / other | 340 / 3 / 11 / 646 |
| failure rate | 0.3% |
| p50 / p95 duration | 8s / 933s |
| runs with log telemetry | 113 |

**Key families**

| Family | Runs | Success | Fail | Cancelled | Other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| ci | 40 | 38 | 2 | 0 | 0 | 848.5 | 1,061.7 |
| review_autofix | 58 | 55 | 0 | 1 | 2 | 272 | 2,756 |
| orchestrate_poll | 60 | 51 | 0 | 9 | 0 | 655 | 1,038 |
| implement | 127 | 8 | 0 | 1 | 118 | 1 | 1,129.7 |
| plan | 127 | 7 | 0 | 0 | 120 | 1 | 561.5 |
| clarify | 137 | 9 | 0 | 0 | 128 | 1 | 543.6 |
| orchestrate_clarify_respond | 126 | 2 | 0 | 0 | 124 | 1 | 421.75 |
| cancel_on_pr_close | 47 | 47 | 0 | 0 | 0 | 16 | 528.7 |
| promote_main_to_stable | 1 | 0 | 1 | 0 | 0 | 158 | 158 |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 0 | 5,864 | 5,864 |

**Tokens and cache**

| Scope | Calls | Prompt | Completion | Cache read | Cache write | Total | cache_hit_rate |
|---|---|---|---|---|---|---|---|
| repo | 45 | 5,281,891 | 269,223 | 13,987,235 | 1,556,981 | 21,003,712 | 0.67162 |
| review_autofix | 33 | 5,187,156 | 265,136 | 13,987,235 | 1,465,363 | 20,904,890 | 0.677684 |
| test_and_mark_stable | 12 | 94,735 | 4,087 | 0 | 91,618 | 98,822 | 0.0 |
| run 39809581147 | 11 | 1,783,688 | 82,169 | 5,544,547 | 486,995 | 7,897,399 | 0.709454 |
| run 39800256892 | 11 | 1,646,408 | 93,988 | 5,084,453 | 493,329 | 7,318,178 | 0.703809 |
| run 39816960064 | 11 | 1,757,060 | 88,979 | 3,358,235 | 485,039 | 5,689,313 | 0.599649 |

Codex calls and tokens are 0. Claude fields are absent from the run rows (data gap).

**Review telemetry**

| Metric | Value |
|---|---|
| wall_clock_p50_ms (repo) | 16,000 |
| wall_clock_p99_ms (repo) | 4,609,760 |
| wall_clock_p50 / p99 ms (review_autofix) | 261,000 / 3,374,420 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**GH API (PAT budget lines)**

| Run | Job | remaining | used_in_job |
|---|---|---|---|
| 39814995656 | codex-agent | 4,472 | 185 |
| 39833189931 | sweep | 4,364 | 2 |
| 39823754979 | sweep | 4,513 | 2 |
| 39820953013 | post-merge-validate-dispatch | 4,776 | 0 |
| 39827596371 | sweep | 3,859 | unknown |
| 39807709608 / 39816960064 / 39807273046 / 39809570600 | various | 4,954 / 4,787 / 4,848 / 3,901 | unknown |

There were no HTTP 429 or secondary rate-limit events.

**Semble**

| Metric | Value |
|---|---|
| query calls / bytes | 3 / 42,840 (about 14.3 KB per query) |
| sources total / static_dup_bytes | 25 / 0 |
| bootstraps / failed / unused | 6 / 0 / 3 |
| bootstrap ms total | 255,225 (review 135,908; workflow_log_analysis 119,317, all unused) |
| fallbacks total / contract_test / runtime | 25 / 24 / 1 (implement 39805819469) |
| echo lines dropped | 0 |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response bytes | fallbacks |
|---|---|---|---|---|---|---|
| serena (all workflows) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps**
- The deep-dive logs directory was not mounted.
- Poll and `codex-agent` logs are truncated by 0.85–1.7M characters.
- No `AI_MEMORY_TELEMETRY` was found.
- Claude-engine token fields are missing from the run rows.
- The 1,000-run cap covers only about 9.5 hours because 64.6% of runs are skipped.
- There is no per-job queue or duration data.
## Deep Audit — Workflows & Scripts (2026-10-22)

Scope and method: this pass reads the workflows and scripts in depth instead of skimming all of them. It covers `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh` (the wave loop, the superseded check and the GraphQL batch helpers), `scripts/review_merge_train.sh`, `scripts/promote_candidate_follow.sh`, `scripts/promote_main_cycle.sh` (the pin and freeze path), `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/check_failure_triage.sh` (the routing helpers), `scripts/label_helpers.sh`, the `review_autofix_step_*` failure-marker scripts, and these workflows: `promote-main-to-stable.yml`, `promote-candidate-follow.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `review_rb_judge_dispatch.yml`, the Telegram and failure steps of `review_autofix.yml`, and the support-ref and integration-ref steps of `implement.yml`.

The sandbox has no shell. Character and byte counts in Section 4 are therefore estimates from line ranges and sampled line widths, and they are marked as such. Findings already in this report's earlier sections are cross-referenced, not repeated: the promote fast-forward annotations (Reliability 1), the heal-reconcile full checkout (Speed 3), and the expected 404 on `promote-cycle-frozen` (GH API Call Audit).

### Section 1: Bug & Correctness Sweep

**BUG-001** — `.github/workflows/review_autofix.yml:4967-4982`, `.github/workflows/review_autofix.yml:5058-5073`, `scripts/review_autofix_step_post_editor_summary.sh:101-117` — Severity: Medium — Category: `bug`
- **Description:**
  - All three inline copies of the "review-autofix-failure:v1 marker" block say they use "Same evidence files and reason precedence as 'Assemble failure evidence', so every failure comment of one run carries one fingerprint" (`review_autofix.yml:4963-4966`).
  - That is not true. `scripts/review_autofix_step_assemble_failure_evidence.sh:63-75` passes `--evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt"` (line 69) and the conditional `"${provider_outage_reason_args[@]}"` (line 74). Both `review_autofix.yml` copies omit both of them.
  - `review_autofix_step_post_editor_summary.sh:103-113` also omits the resolver file. It passes `--failure-reason` instead.
  - `_cmd_autofix_failure_fingerprint` in `scripts/workflow_failure_heal.py:5354-5363` hashes the joined evidence text (`read_failure_evidence(args.evidence_file)`). So the same failure produces different `fp=` values depending on which step posted the comment. The gate's identical-failure fingerprint cap can then undercount streaks.
  - [NEEDS VERIFICATION] Whether `resolver_stage_stderr.txt` exists when the editor-changes-lost or no-op steps run. The resolver step at `review_autofix.yml:3951` runs earlier in the same job, so it is plausible.
- **Recommended fix:**
  - Make the evidence list identical in all four call sites. At minimum, add the missing `--evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt"` to the three inline copies.
  - The root-cause fix is DUP-005: one shared helper, so the list cannot drift again.

**BUG-002** — `.github/workflows/cancel_on_pr_close.yml:391-427` (with `:143-189`) — Severity: Medium — Category: `bug`
- **Description:**
  - The "Release merge-queued PRs (merge train)" step has no `if:`, so it runs only when the previous step succeeded.
  - The previous step runs under `set -euo pipefail` (line 146). It calls `_cpr_fetch_active_runs` (lines 167-186, `gh_retry gh api --paginate … | jq -s …`) outside any `if` or `||` guard, at lines 260 and 325.
  - A transient listing failure therefore fails the cancel step and skips the merge-train release.
  - The step's own comment (lines 392-394) says "core-profile repos have no poll backstop". In those consumers, a PR queued behind the closed PR stays queued until some later PR event.
  - In this repository the `*/5` schedule (`internal-cancel-on-pr-close.yml:7`) eventually recovers it (inference).
- **Recommended fix:**
  - Add `if: ${{ !cancelled() }}` to the release step. `review_merge_train.sh release` is already fail-open (`scripts/review_merge_train.sh:103-111`).
  - Optionally wrap the two `_cpr_fetch_active_runs` calls in `if ! … ; then echo "::warning::…"; exit 0; fi` so the cancel step itself fails open.

**BUG-003** — `scripts/promote_candidate_follow.sh:168-179` and `:231-243` vs `scripts/promote_main_cycle.sh:632-655` and `:904-905` — Severity: Medium — Category: `bug`
- **Description:**
  - The follower reads the `promote-cycle-frozen` tag ref once, at line 168. It reads the main tip at line 181, and only then PATCHes `refs/heads/promote-candidate` (line 237, `force=false`).
  - `pin_freeze` in the cycle job creates the tag and then PATCHes the branch to its tip (lines 638-655). The follower runs in a different concurrency group (`promote-candidate-follow.yml:23-25`) and fires on every push to `main`.
  - If the freeze lands between the follower's tag read and its PATCH, the follower fast-forwards the frozen branch past the candidate.
  - The cycle catches this only after the smoke gate starts: `fail_cycle pin_head_mismatch` at `promote_main_cycle.sh:904-905`. That uses up one of the `PROMOTE_CYCLE_MAX_ATTEMPTS` attempts (default `3`, `promote-main-to-stable.yml:143`).
  - The window is a few seconds wide (inference), but every push to `main` opens it.
- **Recommended fix:**
  - In `promote_candidate_follow.sh`, re-read `git/ref/tags/promote-cycle-frozen` immediately before the step-4 PATCH. If it is present now, emit `outcome=frozen`.
  - This adds 1 GET, only on the advance path. Update the §14 budget comment at line 22.

**BUG-004** — `.github/workflows/cancel_on_pr_close.yml:40-53`, `.github/workflows/orchestrate_poll.yml:132-145` and the same resolver block in `orchestrate.yml`, `clarify.yml`, `plan.yml`, `workflow_failure_heal.yml`, `check_failure_triage.yml`, `orchestrate_clarify_respond.yml` and `implement.yml` (16 occurrences of `promote_candidate_support_ref="${{ github.sha }}"` across 9 workflows) — Severity: Medium — Category: `bug`
- **Description:**
  - When the workflow is called `@refs/heads/promote-candidate`, the step reads `repos/shubhodeep1/coding-workflows/branches/promote-candidate` with `gh_api_retry … 2>/dev/null || true`.
  - Any failure (a transient 5xx, or a rate limit) ends at the same `else` branch as a real unprotected branch. It logs `SUPPORT_REF source=promote_candidate_unprotected` and keeps `github.sha`.
  - The step sources `${GH_API_RETRY_HELPERS:-/dev/null}`. That variable is written only by bootstrap steps in `validate.yml:167`, `implement.yml:178`, `security-audit.yml:122`, `orchestrate_clarify_respond.yml:132` and `review_autofix.yml:376`. So in `cancel_on_pr_close.yml:43`, `orchestrate_poll.yml:135`, `clarify.yml:393`, `plan.yml:360`, `orchestrate.yml:214`, `workflow_failure_heal.yml:118`, `check_failure_triage.yml:323` and `review_autofix_sweep.yml:219`, the passthrough `gh api` runs with no retry.
  - During a frozen promote cycle, one blip runs support scripts from the caller's `github.sha` instead of the frozen candidate (inference). This defeats the pin, and the log hides the cause.
- **Recommended fix:**
  - Log a distinct token when the read fails, for example `SUPPORT_REF source=promote_candidate_read_failed`. This is a new value of an existing line and needs an `agents.d/` fragment if the prefix is contractual.
  - Add a bounded inline retry: 3 attempts with 2s and 4s sleeps, matching the backoff pattern at `promote-main-to-stable.yml:288-311`.
  - Consider failing closed when the workflow ref is exactly `@refs/heads/promote-candidate`. See also DUP-001.

**BUG-005** — `scripts/review_merge_train.sh:1316-1331` — Severity: Low — Category: `bug`
- **Description:**
  - `_mt_dispatch_review` tries `ai-review.yml`, then `internal-review.yml`, then `review_autofix.yml`. It moves to the next name after any `gh workflow run` failure.
  - `gh workflow run` is a single-attempt create (`scripts/gh_helpers.sh:813`). A timeout after GitHub accepted the dispatch therefore also dispatches the next workflow name, giving two review runs for one PR.
  - In this repository, the fallback is `review_autofix.yml` itself, which skips the `@promote-candidate` wrapper.
  - [NEEDS VERIFICATION] The duplicate is probably bounded by the review concurrency group mentioned at lines 1215-1216.
- **Recommended fix:** Move to the next workflow name only when stderr shows the workflow is missing (`HTTP 404` / `could not find any workflows`). On any other failure, return 1 so the existing label-restore path at lines 1417-1433 runs.

**BUG-006** — `.github/workflows/promote-main-to-stable.yml:437-446` — Severity: Low — Category: `bug`
- **Description:**
  - The release dispatch is a raw `gh workflow run`, with no helper and no structured outcome line. It runs after `stable` was already pushed (line 277).
  - A transient failure leaves `stable` advanced with no release dispatched. A manual or poller re-run recovers it, because lines 264-269 make the fast-forward step a no-op and the dispatch then runs (inference).
  - This complements the report's Reliability item 1, which covers only the fast-forward step.
- **Recommended fix:** Do not auto-retry, because the dispatch is not idempotent. Wrap it in `if ! gh workflow run …; then echo "PROMOTE_STABLE_FF outcome=fail reason=dispatch …"; echo "::error::…stable moved to ${RESOLVED_VERSION} target but release dispatch failed; re-run this workflow"; exit 1; fi`, reusing the `PROMOTE_STABLE_FF` prefix proposed in Reliability 1.

**SEC-001** — `scripts/gh_helpers.sh:1100`, `:1117`, `:1130`, `:1132` (and the matching lines in `gh_retry_to_file` from `:1185`) — Severity: Medium — Category: `security`
- **Description:**
  - `gh_retry` writes the full command line unescaped into workflow-command annotations: `echo "::warning::gh command failed … : $*"` and `echo "::error::gh command failed after ${max_attempts} attempts: $*"`.
  - `$*` includes `-f body=<comment text>` and `-f query=<GraphQL document>` values. Examples: `review_autofix.yml:5028`, `:5111`, `:5162`, `:5184` and `scripts/review_merge_train.sh:886`.
  - A multi-line body becomes several log lines. Any line in it that starts with `::` is then parsed as a workflow command (for example `::stop-commands::` or `::add-mask::`), and comment bodies can quote untrusted PR or issue text.
  - The file already has `_gh_actions_escape` (lines 157-164) and uses it for stderr (line 1102) and for `gh_api_retry` endpoints (line 907), so this is an oversight.
  - [NEEDS VERIFICATION] Whether the runner parses workflow commands on stderr as well as stdout.
- **Recommended fix:**
  - Add a helper `_gh_retry_cmd_summary "$@"` that prints only `gh <subcommand> <endpoint>`, reusing `_gh_api_endpoint` (lines 630-643) and dropping field values.
  - Log `$(_gh_actions_escape "$(_gh_retry_cmd_summary "$@")")` at all four sites. The `::error::` prefix text stays unchanged (§10).

**SEC-002** — `scripts/orchestrate_poll_process.sh:27020-27055` (path set at `:26836`) — Severity: Low — Category: `security`
- **Description:**
  - The review-blocked judge parser inlines a shell variable into Python source: `raw = open('${RB_JUDGE_OUTPUT_FILE}', 'r').read()`.
  - `RB_JUDGE_OUTPUT_FILE="${RUNTIME_DIR}/rb_judge_output_${rb_issue}.txt"`. A `'` in `RUNTIME_DIR` breaks the parser or injects Python.
  - The sibling `_robust_parse_json_file` (lines 16615-16622) already passes the path safely via `sys.argv[1]`.
- **Recommended fix:** Pass the path via argv (`python3 -I -B -c '…open(sys.argv[1])…' "${RB_JUDGE_OUTPUT_FILE}"`), or reuse the extractor from DUP-004.

**SEC-003** — `.github/workflows/clarify.yml:68-75` — Severity: Low — Category: `security`
- **Description:**
  - The "Log trigger context" step interpolates `${{ github.event.issue.number }}`, `${{ github.event.action }}` and `${{ github.event.comment.user.login }}` directly into `run:`.
  - It is not exploitable today, because logins are restricted to `[A-Za-z0-9-]` plus `[bot]`. But it breaks the env-only pattern used for the same value at `clarify.yml:98` (`HOLD_COMMENT_LOGIN`) and `:680`.
- **Recommended fix:** Move the three values into the step's `env:` and reference them as `"${…}"`.

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001** — `scripts/orchestrate_poll_process.sh:25551-25603` (helpers at `:4451-4466`, `:2220-2224`; batch at `:25522`, `:18428-18557`) — Severity: High — Category: `api-batching`
- **Description:**
  - In the per-wave reconcile loop, each issue gets `_issue_cross_ref_pr_numbers_unique "${inum}"`. That is a paginated REST timeline read (`gh_retry gh api --paginate …/issues/N/timeline`, line 4448).
  - Each cross-referenced PR candidate then gets `_fetch_pr_json` (`pulls/N`).
  - Both happen right after `_fetch_candidate_issue_details_graphql "${_wave_issue_nums_json}"` (line 25522). The same issues' GraphQL query already selects `timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT])` with PR `number state merged mergedAt headRefName baseRefName headRefOid` (lines 18466-18487).
  - The §14 comment at lines 25573-25574 explains the re-fetch: the existing call "omits PR body/head". `head` (`headRefName`) is already fetched. Only `body` is missing; `_pr_json_is_issue_implementation_pr` (lines 18925-18942) needs it.
  - This is a per-iteration call inside a loop that already has a prefetched cache, which §14 lists as a review blocker.
  - The same per-issue timeline pattern repeats through `_issue_cross_ref_pr_number_last` in other loops: lines 25839 (RTM), 25985 (in-progress), 26131 (review-blocked) and 28721. [NEEDS VERIFICATION] Those loops want the *last* cross-reference, while the batch's `linked_pr` keeps the last `willCloseTarget` PR.
- **Call count:**
  - Current: per tracking issue per cycle, N timeline calls (1 or more pages each) plus Σ candidates `pulls/N` calls. For example, 6 issues × (1 + 2) = 18 REST calls.
  - Proposed: 0 extra calls on a cache hit, because the data rides on the existing `ceil(N/25)` GraphQL calls. The REST path stays only as the fail-open fallback for issues missing from the cache.
- **Recommended fix:**
  - Extend `_fetch_candidate_issue_details_graphql`. Add `body` and `isCrossRepository` to the `... on PullRequest` fragment.
  - Project a new additive field `cross_ref_prs: [{number,state,merged,merged_at,head_ref,body}]` alongside the unchanged `linked_pr` (§10).
  - In the loop, build the REST-shaped `{head:{ref},body,state,merged_at}` object from that field and pass it to `_pr_json_is_issue_implementation_pr`.
  - [NEEDS VERIFICATION] `last: 50` against the full REST pagination: keep the REST fallback when `timelineItems.pageInfo.hasPreviousPage` is true.

**BATCH-002** — `scripts/orchestrate_poll_process.sh:9229-9307` (`evaluate_sync_superseded_by_main`, called at `:12998`, `:13042`) — Severity: Medium — Category: `api-batching`
- **Description:** The function makes three kinds of call:
  - For each project issue, a paginated timeline REST call (line 9262).
  - For each discovered PR, `_fetch_pr_json` (line 9280).
  - For each PR, a paginated `pulls/N/files` call (lines 9293-9294).
- **Call count:**
  - Current: N + 2M calls or more (for example, 8 issues and 8 PRs gives 24 or more).
  - Proposed: `ceil(N/25)` aliased GraphQL calls, `issue(number:){timelineItems(itemTypes:[CROSS_REFERENCED_EVENT]){nodes{... on CrossReferencedEvent{source{... on PullRequest{number state merged files(first:100){nodes{path} pageInfo{hasNextPage}}}}}}}}`, plus one REST files call only for PRs with `hasNextPage`.
- **Recommended fix:** Add a sibling helper modelled on `_fetch_linked_pr_status_graphql` (lines 18587-18620): batch size 25, merged into one keyed object, with failed batches omitted. Document the batching contract per §14. When a batch is missing, set `SYNC_SUPERSEDED_CONFIDENT=false`, as the current loop does on fetch failure.

**API-001** — `scripts/label_helpers.sh:163-197` — Severity: Medium — Category: `api-redundancy`
- **Description:**
  - `ensure_label_exists` always issues a `gh label create` POST and treats "already exists" as success. There is no per-process memo, so every call costs one API request, even for labels that have existed for months.
  - The poller's own copy already memoizes this with `_ENSURED_LABELS_CACHE` (`scripts/orchestrate_poll_process.sh:4008-4016`).
- **Call count:**
  - Current: 1 call per invocation.
  - Proposed: 1 call per label per process.
- **Recommended fix:** Add a `declare -gA _LABEL_HELPERS_ENSURED=()` memo in `label_helpers.sh`, set on success or "already exists" and checked first, mirroring lines 4011-4016. Then converge the other copies on it (DUP-003).

**API-002** — `.github/workflows/implement.yml:5627-5630` — Severity: Low — Category: `api-redundancy`
- **Description:**
  - `gh issue edit --remove-label 'ai:done' --add-label 'ai:implementation-failed'` fails whenever `ai:done` is absent. That "not found" error is classified as permanent at `gh_helpers.sh:194`.
  - The step then repeats the edit with only `--add-label`, so the common path costs 2 calls, one of them a guaranteed failure.
- **Call count:** Current 2 calls, 1 of them failed. Proposed 2 successful calls (`POST …/labels`, `DELETE …/labels/ai%3Adone` with 404 ignored), or 1 call via `set_issue_phase_label_resilient` (`scripts/label_helpers.sh:199`) if it fits the semantics.
- **Recommended fix:** Use the `label_helpers.sh` helper, which this step already sources at line 5620.

**BATCH-003** — `scripts/workflow_failure_heal_pr_reconcile.sh:239-275` — Severity: Low — Category: `api-batching`
- **Description:**
  - For each matching heal issue, the script calls `GET pulls?state=open&head=<owner>:ai/issue-<n>` (line 243). The header (lines 29-32) documents this.
  - Every acceptable heal PR has `base == SOURCE_HEAD_REF`, or `base == BASE_REF` after a merge (line 252). So one or two `pulls?state=open&base=<ref>` listings, filtered locally by `head.ref == ai/issue-<n>`, cover all heal issues.
- **Call count:** Current H calls (H = matching heal issues, usually 1). Proposed 1 call (unmerged), or 2 (merged).
- **Recommended fix:** Make the change only if H > 1 is seen in logs, since the gain is small. Speed item 3 in this report covers the larger cost on this job.

Checked with no finding: the 10 status queries in `scripts/review_merge_train.sh:1166` are an intentional second pass, documented at lines 83-90 and read lazily, once, only when a queued PR passes the base filter (lines 1336-1367). The per-run cancel POSTs at `cancel_on_pr_close.yml:364-380` have no batch endpoint. `/rate_limit` reads in the PAT-budget steps do not count against the core allowance (`gh_helpers.sh:29-30`).

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — Support-ref resolver block: 16 copies across 9 workflows (for example `cancel_on_pr_close.yml:30-67`, `orchestrate_poll.yml:122-159`, `implement.yml:446-447`, `:751-752`, `:1330-1331`, `:7004-7005`) — Severity: Medium — Category: `duplication`
- **Description:**
  - About 35 lines are repeated in each copy. Only the workflow filename in two regexes changes.
  - The fallback `gh_api_retry() { while …; do shift; done; gh api "$@"; }` appears 26 times in 13 workflows.
  - The block cannot move to `scripts/`, because it runs before the support checkout exists. The copies are kept in sync by hand.
- **Recommended fix:**
  - Keep the copies inline, but add `tests/test_support_ref_resolver_parity.py`. It would extract each `Resolve workflow support ref*` step and assert that its body, after normalizing `<file>\.yml`, equals one canonical text.
  - Apply BUG-004's fix in that one canonical text so it reaches all 16 copies together.

**DUP-002** — Inline GH_PAT budget logic: 19 occurrences of `reading="$(gh api rate_limit --jq …)"` across 5 workflows (`review_autofix.yml` ×12, for example `:730-747` and `:1461-1478`; `clarify.yml` ×3; `workflow-failure-heal-intake.yml` ×2; `orchestrate_poll.yml:102-116`; `validation-improvements-intake.yml` ×1) — Severity: Medium — Category: `duplication`
- **Description:**
  - These copies re-implement `gh_pat_budget` from `scripts/gh_helpers.sh:31-77`, and they have already drifted. The helper overrides the reading with the `x-ratelimit-resource: core` headers (lines 50-60); the inline copies read only the body.
  - The prior section's `used_in_job=unknown` diagnosis (GH API Call Audit) applies to every copy.
- **Recommended fix:**
  - For `phase=end` steps, which run after support staging, replace the body with `source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" && gh_pat_budget end <workflow> <job> "${GH_PAT_BUDGET_FILE}"`. That updates 6 end steps in `review_autofix.yml` plus the end steps in the other workflows.
  - Keep the inline text only for `phase=start` steps that run before staging.
  - The `GH_PAT_BUDGET …` line format must stay identical (§10; the collector regex is cited in GH API Call Audit).

**DUP-003** — `ensure_label_exists` defined 6 times, plus a seventh label-create helper — Severity: Medium — Category: `duplication`
- **Description:**
  - Definitions:
    - `scripts/label_helpers.sh:163`
    - `scripts/orchestrate_poll_process.sh:4008`
    - `scripts/validate_process.sh:1222`
    - `scripts/review_rb_judge.sh:924`
    - `scripts/review_autofix_step_mark_ready_to_merge.sh:15`
    - `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`
  - There is also a separate create-with-retry helper in `scripts/implement_handle_guard_block.sh:33-60`, and 44 raw `gh label create` sites across 14 files.
  - Colour and description sources disagree. `label_helpers.sh` uses the hard-coded `_AI_LABEL_COLORS` / `_AI_LABEL_DESCS` catalogue (lines 150-167). `validate_process.sh:1229-1232` and the poller read `.github/ai/label_contract.v1.json`.
  - A label created by one path can get a different colour or description than the same label created by another.
- **Recommended fix:**
  - Make `scripts/label_helpers.sh::ensure_label_exists <label> [repo]` the owner. It should read the contract file first, fall back to the catalogue, and memoize (API-001).
  - Have the five other definers source it behind their existing `type ensure_label_exists` guards, as `scripts/consumer_health_check.sh:75` already does.

**DUP-004** — `scripts/orchestrate_poll_process.sh:875-920`, `:16615-16660`, `:27020-27055` — Severity: Low — Category: `duplication`
- **Description:** Three embedded Python copies of the same "strip code fences, then scan for balanced braces and `json.loads`" extractor. Their failure behaviour and argv handling differ (see SEC-002).
- **Recommended fix:** Add `extract-json <file> [--require-status]` to `scripts/orchestrate_lib.py`, which the poller already calls (line 25609), and replace the three heredocs.

**DUP-005** — `.github/workflows/review_autofix.yml:4967-4982`, `:5058-5073`, `scripts/review_autofix_step_post_editor_summary.sh:101-117`, variant at `scripts/review_autofix_step_assemble_failure_evidence.sh:63-75` — Severity: Low — Category: `duplication`
- **Description:** Four hand-maintained copies of the fingerprint invocation. They have already drifted (BUG-001).
- **Recommended fix:** Add a new `scripts/review_autofix_failure_marker.sh` with `review_autofix_failure_marker_suffix [failure_reason]`, which prints `$'\n\n'marker=…` or nothing. It holds the one evidence-file list. Source it from the three comment sites, and have the assemble step use the same list array. Register it in `REQUIRED_BOOTSTRAP_SCRIPTS` per the §24 pattern.

**DUP-006** — `.github/workflows/review_autofix.yml:4155`, `:4901`, `:4960`, `:5051`, `:5204`, `:5504` and the 81 `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }` fallbacks in 43 files — Severity: Low — Category: `duplication`
- **Description:** Every Telegram or failure step rebuilds `PR_URL` and `RUN_URL` from `${{ }}`. The silent no-retry `gh_retry` fallback appears in nearly every script.
- **Recommended fix:**
  - Export `PR_URL` and `RUN_URL` once into `$GITHUB_ENV` in an early `codex-agent` step, which also helps EXPR-001.
  - Have the fallback print one `::notice::gh_helpers.sh unavailable; gh_retry is a passthrough` line, so missing helpers are visible.

**DUP-007** — Branch-name validators — Severity: Low — Category: `duplication`
- **Description:** Two different regexes guard branch names, each with different extra rules:
  - `^[A-Za-z0-9._/-]{1,200}$`: `scripts/workflow_failure_heal_pr_reconcile.sh:69` (rejects `*.lock|*/.*|.*`), `scripts/orchestrate_stall_retired_conflict.sh:25` (plus `git check-ref-format`), `scripts/review_head_gate.sh:133`, `scripts/check_failure_triage.sh:740`.
  - `^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$`: `scripts/check_failure_triage.sh:122` and `:176`, `scripts/unblock_judge.sh:1003`, `scripts/workflow_failure_heal.py:353`, `scripts/unblock_actions.py:135`.
  - Within `check_failure_triage.sh`, `triage_verify_pr_head_route` (lines 173-189) re-implements `triage_routing_ref_is_safe` (lines 119-132) line for line.
- **Recommended fix:**
  - Add a new `scripts/ref_helpers.sh` with `ref_helpers_branch_is_safe <branch>`: the stricter leading-alphanumeric regex, the union of the case filters, then `git check-ref-format --branch`.
  - Call it from the four bash sites. Inside `check_failure_triage.sh`, make lines 176-189 call `triage_routing_ref_is_safe`.

### Section 4: Expression Size Limit Risk Assessment

Existing control: `tests/test_workflow_file_size_limit.py:28-32` and `:66-98` fail CI when any `run:` containing `${{` reaches 18,000 characters. Lines `:44-59` fail it at 480,000 bytes per file. No `actions/github-script` `script:` inputs exist (searched, 0 matches), so the run-only check covers every interpolated template. The largest `if:` seen is about 400 characters (`review_autofix.yml:5188`), with no risk.

The very large run bodies have no `${{ }}` in the ranges checked, so the 21,000-character limit does not apply to them; they count only toward file size. Examples: `test-and-mark-stable.yml` Phase 4 (lines 1821-2603) and `implement.yml` "Run Codex implementation" (lines 2795-4103).

**EXPR-001** — `.github/workflows/review_autofix.yml:5200-5349` ("Telegram review-blocked judge decision") — Severity: Medium — Category: `expression-limit`
- **Description:**
  - The step is a 150-line `run:` with `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` at lines 5204, 5205 and 5209.
  - Estimated length is about 13,500-15,000 characters: about 30 `MSG="$(printf …)"` lines of 150-250 characters plus about 120 shorter lines. That leaves about 6,000-7,500 characters of headroom to 21,000, and puts the step near the 15,000 Medium line. [NEEDS VERIFICATION] with `wc -c`.
  - The `case` table gains a branch for every new judge reason (lines 5223-5343), so this step will keep growing.
- **Recommended fix:**
  - Move the three `${{ }}` values into the step's `env:` (`PR_URL_BASE`, `RUN_URL`). That makes the body expression-free, so the limit no longer applies.
  - Better, extract the body verbatim to `scripts/review_autofix_step_telegram_rb_judge_decision.sh`, keeping `name:`, `if:` and `env:` in the YAML (§24).

**EXPR-002** — `.github/workflows/review_autofix.yml:5047-5185` ("Telegram editor-noop-suspicious warning") — Severity: Low — Category: `expression-limit`
- **Description:** About 138 lines with `${{ }}` at lines 5051, 5052, 5111, 5162 and 5184. Estimated 11,000-12,500 characters, leaving about 8,500-10,000 of headroom. It embeds three long comment templates that tend to grow.
- **Recommended fix:** Same as EXPR-001: move the values to `env:`, or move the body to `scripts/review_autofix_step_telegram_editor_noop.sh`.

**EXPR-003** — `.github/workflows/implement.yml:573-735` ("Resolve integration ref") — Severity: Low — Category: `expression-limit`
- **Description:** About 163 lines with `${{ github.repository }}` and `${{ github.sha }}` at lines 592, 593, 677 and 702. Estimated 11,000-13,000 characters, leaving about 8,000-10,000 of headroom.
- **Recommended fix:** Add `REPOSITORY: ${{ github.repository }}` and `DISPATCH_SHA: ${{ github.sha }}` to the step's existing `env:` (lines 566-572) and reference them as variables. This keeps the body expression-free as it grows.

**EXPR-004** — `.github/workflows/test-and-mark-stable.yml` (7,776 lines), `implement.yml` (7,068), `review_autofix.yml` (5,749), `ci.yml` (2,560), `update_workflows.yml` (2,458) — Severity: Medium — Category: `expression-limit`
- **Description:**
  - This sandbox cannot measure byte sizes. At 60-65 bytes per line, the two largest files would be about 425,000-505,000 bytes, which is at or near the 480,000-byte guard. [NEEDS VERIFICATION] with `wc -c`.
  - CI passed in this window (run 39809580977 in the Executive Summary), so every file is currently below 480,000 bytes (inference). No workflow can be near the 800 KB / 1 MB figure in the audit brief.
  - The binding limit is GitHub's documented 512,000 bytes (§24), not 1 MB.
  - The guard prints only the largest file (`tests/test_workflow_file_size_limit.py:105-109`), so each file's headroom is invisible until it breaks.
- **Recommended fix:**
  - Have `main()` print every workflow's size and headroom, logging only, with no threshold change.
  - Pre-emptively extract the largest bodies into `scripts/`, keeping `name:`, `id:`, `if:` and `env:` (§24):
    - `test-and-mark-stable.yml` Phase 4 (lines 1821-2603, about 780 lines).
    - `test-and-mark-stable.yml` Phase 4b (lines 2673-3343, about 670 lines).
    - `implement.yml` "Run Codex implementation" (lines 2795-4103, about 1,300 lines).

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `scripts/gh_helpers.sh:336-341` — Severity: Low — Category: `dead-code`
- **Description:** `gh_rate_limit_breaker_tripped` has no caller in `scripts/` or `.github/workflows/` (searched). The comment at lines 315-318 says its only consumer was removed. `gh_rate_limit_breaker_active` (lines 894-902) replaced it.
- **Recommended fix:** Keep the function, since removing it could break consumers (§10). Mark it deprecated in its comment, pointing to `gh_rate_limit_breaker_active`, and add it to an alias note.

**CONSIST-001** — `.github/workflows/review_rb_judge_dispatch.yml:46`, `.github/workflows/unblock_judge_dispatch.yml:32` vs `scripts/promote_main_cycle.sh:517-530` — Severity: Medium — Category: `consistency`
- **Description:**
  - These two dispatch wrappers call `review_autofix.yml@main` and `unblock_judge.yml@main`. The 13 `internal-*.yml` wrappers call `@promote-candidate`.
  - `pin_preflight` globs only `.github/workflows/internal-*.yml` (line 520), so these two never trip `wrappers_not_pinned`.
  - During a frozen cycle, judge runs dispatched by the poller run main-tip workflow code while every other phase runs the candidate (inference).
- **Recommended fix:** Pin both to `@promote-candidate`, and widen the preflight glob to `internal-*.yml *_dispatch.yml`. Or document the exemption in `docs/plans/pin-promote-cycle-to-candidate-plan.md` if the mismatch is intentional.

**CONSIST-002** — `.github/workflows/implement.yml:592-594` vs `:446-447`, `:751-752`, `:1330-1331` — Severity: Low — Category: `consistency`
- **Description:** In the source repository, the integration-ref resolver always uses `resolver_ref="${{ github.sha }}"`. The other support-ref resolvers in the same file switch to the protected `promote-candidate` tip when called `@refs/heads/promote-candidate`.
- **Recommended fix:** Reuse the resolved `SCRIPT_REF` from the earlier support-ref step (it is written to `$GITHUB_ENV`), or apply the same promote-candidate branch read here.

**CONSIST-003** — `scripts/promote_candidate_follow.sh:116-119` and `:168-175` — Severity: Low — Category: `consistency`
- **Description:** The expected-absent freeze tag produces a `GH_API_RETRY outcome=permanent … status=404` line on every follower run (see GH API Call Audit, "Expected 404"). `gh_api_retry` has no expected-404 mode, so log consumers see a "permanent failure" on the healthy path.
- **Recommended fix:** Add an opt-in `--expect-404` flag to `gh_api_retry` (`scripts/gh_helpers.sh:910-919`) that returns 2 without the `permanent` log line when the status is exactly 404. The existing outcome vocabulary stays unchanged (§10).

**DEBT-001** — `scripts/orchestrate_poll_process.sh` (30,821 lines) — Severity: Medium — Category: `tech-debt`
- **Description:**
  - `scripts/orchestrate_stall_retired_conflict.sh:10-12` records that "ShellCheck's CI run on the ~27,000-line poller did not finish". `ci.yml:422-426` pins ShellCheck 0.10 and relies on `extended-analysis=false` to cope. The file has since grown by about 3,800 lines.
  - The size also hides BATCH-001 and BATCH-002, and the poll-cycle observability gap noted in Orchestrator Health.
- **Recommended fix:** Keep extracting cohesive loops into sourced modules, following the `orchestrate_stall_retired_conflict.sh` pattern: sourced, with fail-closed stubs and its own log prefix. Start with the wave reconcile loop (lines 25536-25603) and `evaluate_sync_superseded_by_main` (lines 9229-9320).

**SHELL-001** — `.github/workflows/review_autofix.yml:5129-5130` — Severity: Low — Category: `shellcheck`
- **Description:** `ls -1 "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err | wc -l` and `ls -1 … | sort -V | tail -n 1` (SC2012) parse `ls` output.
- **Recommended fix:** Use `shopt -s nullglob; editor_err_files=("${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err); editor_attempts="${#editor_err_files[@]}"`, then `printf '%s\n' "${editor_err_files[@]}" | sort -V | tail -n 1`.

**SHELL-002** — `.github/workflows/review_autofix.yml:4956-4959`, `:5047-5050`, `:5200-5202` — Severity: Low — Category: `shellcheck`
- **Description:**
  - These multi-line steps run without `set -euo pipefail`, so only the runner's default `bash -e` applies.
  - `tg_helpers.sh` is sourced with no guard, while `gh_helpers.sh` on the next line is guarded. A missing `tg_helpers.sh` fails an alert-only step, and with it the job.
- **Recommended fix:** Add `set -uo pipefail` and source `tg_helpers.sh` with `2>/dev/null || true`, plus `type tg_send_tracked >/dev/null 2>&1 || tg_send_tracked() { :; }`. This matches the fail-open stance of these notification steps.

TODO/FIXME/HACK/XXX markers: none found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml` (searched).

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BATCH-001 |
| Medium | 14 | BUG-001, BUG-002, BUG-003, BUG-004, SEC-001, BATCH-002, API-001, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-004, CONSIST-001, DEBT-001 |
| Low | 17 | BUG-005, BUG-006, SEC-002, SEC-003, API-002, BATCH-003, DUP-004, DUP-005, DUP-006, DUP-007, EXPR-002, EXPR-003, DEAD-001, CONSIST-002, CONSIST-003, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` (BATCH-001; tests in `tests/test_orchestrate_poll_process.py`) | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/label_helpers.sh`, `.github/workflows/implement.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh` | Medium |
| Code modularization | 9 workflows with the support-ref block plus a new parity test, 5 workflows with PAT-budget copies, `scripts/label_helpers.sh` plus 5 definers, new `scripts/review_autofix_failure_marker.sh`, new `scripts/ref_helpers.sh`, `scripts/orchestrate_lib.py` | Large |
| Expression size reduction | `.github/workflows/review_autofix.yml`, `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml`, new `scripts/review_autofix_step_*.sh`, `tests/test_workflow_file_size_limit.py` (reporting only) | Medium |
| Medium/Low fixes | `scripts/gh_helpers.sh`, `.github/workflows/cancel_on_pr_close.yml`, `scripts/promote_candidate_follow.sh`, `scripts/review_merge_train.sh`, `.github/workflows/promote-main-to-stable.yml`, `.github/workflows/clarify.yml`, `.github/workflows/review_rb_judge_dispatch.yml`, `.github/workflows/unblock_judge_dispatch.yml`, `scripts/promote_main_cycle.sh`, `.github/workflows/review_autofix.yml` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-22)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: every precondition is proven from the source. The calls hit the same endpoint or a strict subset of fields, with identical pagination, filters and auth. They are in the same function with no mutation in between. Error and retry behaviour is unchanged, no concurrency boundary is crossed, and the cache contract holds. The implement stage may apply these without further review.
- **`NEEDS_VERIFICATION`**: the change looks correct, but at least one precondition can't be proven by reading the code. The listed checks must pass before anyone implements it.
- **`RISKY_SKIP`**: listed only so it is visible. At least one hard trigger applies: a retry or backoff path, pagination, a rate-limit-sensitive path, the poller or stall-recovery code, race-defensive logic, or a change to log keys other code reads. It must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001** — `NEEDS_VERIFICATION`
- **Files:**
  - `scripts/promote_candidate_follow.sh:168-179` reads the freeze tag ref.
  - `scripts/promote_candidate_follow.sh:181-184` reads the main ref.
  - `scripts/promote_candidate_follow.sh:208-211` reads the annotated tag object.
  - The budget comment at `:22-25`.
- **Call count:** 3 reads per run on the common no-tag path (branch, tag ref, main), or 4 when an annotated freeze tag exists. Proposed: 2, the REST branch read at `:148` plus one GraphQL query.
- **Endpoints:**
  - `GET repos/{repo}/git/ref/tags/promote-cycle-frozen`
  - `GET repos/{repo}/git/ref/heads/main`
  - `GET repos/{repo}/git/tags/{sha}`
  - Proposed: `POST /graphql`, `repository{ main: ref(qualifiedName:"refs/heads/main"){target{oid}} freeze: ref(qualifiedName:"refs/tags/promote-cycle-frozen"){target{__typename oid ... on Tag{message}}} }`
- **Evidence:** all three are plain, non-paginated `follow_api` reads in straight-line code. Nothing is written between `:168` and `:211`.
  ```bash
  follow_api "${FOLLOW_TMP}/tag.json" "repos/${REPO}/git/ref/tags/${FOLLOW_TAG}"
  ...
  follow_api "${FOLLOW_TMP}/main.json" "repos/${REPO}/git/ref/heads/main"
  ```
  In GraphQL, a missing tag comes back as `freeze: null` instead of HTTP 404. That would also remove the healthy-path `GH_API_RETRY outcome=permanent … status=404` line, which is an alternate fix for the Deep Audit's CONSIST-003.
- **Proposed fix:**
  - Add a `follow_read_refs` helper in `promote_candidate_follow.sh` that issues one `gh_api_retry graphql` call.
  - Map `freeze == null` to `TAG_EXISTS=false`. Map `__typename == "Tag"` to `TAG_TYPE=tag`, taking `TAG_MESSAGE` from the same response. Keep `MAIN_SHA` from `main.target.oid`.
  - Leave the REST branch read alone. It supplies `protected`, and GraphQL `branchProtectionRule` may not reflect rulesets.
  - Update the §14 budget comment at `:22-25`.
- **Safety rationale:**
  - The error behaviour is not identical. Today a non-404 tag-read failure fails closed (`tag_read_failed`, `:173-174`).
  - In GraphQL, a partial `errors[]` alongside `freeze: null` must also fail closed, not read as "no tag".
  - The change also conflicts with BUG-003, which wants the tag re-read just before the PATCH.
- **Downstream signal:** before implementing, check three things:
  1. A GraphQL response with `errors` set and `data.repository.freeze == null` maps to `follow_fail tag_read_failed`.
  2. `ref(qualifiedName:)` resolves both lightweight and annotated tags, and returns `message` for `Tag` targets.
  3. Nothing greps the `GH_API_RETRY … status=404` follower line. The report's GH API Call Audit table cites it.

  Decide this together with BUG-003. If the pre-PATCH tag re-read lands, keep the REST tag read as that second read.

**MERGE-002** — `RISKY_SKIP`
- **Files:** `scripts/check_failure_triage.sh:567-575`.
- **Call count:** 1 on the success and 404 paths. On a transient error it is 1 plus up to `GH_RETRY_MAX_ATTEMPTS`. Proposed: 1 retried call.
- **Endpoint:** `GET repos/{repo}/git/ref/heads/{ref}`, called twice.
- **Evidence:** a raw single attempt runs first so its stderr can be checked for a 404. On any other error, the same endpoint is called again under `gh_retry`.
  ```bash
  if gh api "repos/${REPO}/git/ref/heads/${integration_ref_encoded}" >/dev/null 2>"${integration_ref_err_file}"; then
  ...
  elif gh_retry gh api "repos/${REPO}/git/ref/heads/${integration_ref_encoded}" >/dev/null 2>&1; then
  ```
  `gh_api_retry` (`scripts/gh_helpers.sh:910-992`) already classifies a 404 as permanent (returns 2) and exposes `GH_API_RETRY_LAST_STATUS`. `scripts/promote_candidate_follow.sh:108-119` relies on exactly that.
- **Proposed fix:** a single `gh_api_retry "repos/${REPO}/git/ref/heads/${integration_ref_encoded}"`. Branch on `rc == 0` for `added`, `GH_API_RETRY_LAST_STATUS == 404` for `branch_missing`, and anything else for `lookup_failed`.
- **Safety rationale:** this replaces a hand-written retry fallback with a different retry engine. It would also add a new `GH_API_RETRY outcome=permanent` stderr line on the 404 path, a change in observable log output. And the script only stubs `gh_retry` (`:206`), not `gh_api_retry`.
- **Downstream signal:** do not implement automatically. It saves calls only on transient errors. A human must confirm that `gh_api_retry` is always defined when `check_failure_triage.sh` runs, and that the extra `GH_API_RETRY` line is acceptable in triage logs.

**MERGE-003** — `RISKY_SKIP`
- **Files:**
  - `scripts/review_merge_train.sh:1393` (`_mt_find_marker_comment_id "${num}" "${MT_MARKER}"`)
  - `:1413` (`_mt_upsert_comment` with 3 arguments, which calls `_mt_find_marker_comment_id` again at `:875`)
  - The listing helper at `:800-806`.
- **Call count:** 2 paginated listings of the same PR's comments per released PR, plus 1 `GET` at `:879`. Proposed: 1 paginated listing that returns both markers.
- **Endpoint:** `GET repos/{repo}/issues/{pr}/comments?per_page=100` with `--paginate`, twice.
- **Evidence:** both listings walk the same comments endpoint for the same `num` and differ only in the jq marker filter (`MT_MARKER` vs `MT_RELEASED_MARKER`). Three mutations sit between them:
  - the marker PATCH at `:1398`;
  - the claim DELETE of the label at `:1406`;
  - the dispatch at `:1410`.
- **Proposed fix:** a `_mt_find_marker_comments <pr> <marker>...` variant that returns `marker id created_at` for every requested marker in one paginated pass. Pass the `MT_RELEASED_MARKER` id into `_mt_upsert_comment` as its 4th argument.
- **Safety rationale:**
  - Both calls paginate.
  - Mutations happen between them.
  - The release path defends against races on purpose. The comment at `:1403-1405` says "Concurrent release invocations cannot both claim it", and a stale snapshot of the released marker could post a duplicate comment.
- **Downstream signal:** manual review only. A reviewer must show that no concurrent release run can create a `MT_RELEASED_MARKER` comment between `:1393` and `:1413`, or accept a possible duplicate "released" comment.

**MERGE-004** — `RISKY_SKIP`
- **Files:** `.github/workflows/cancel_on_pr_close.yml:155-189` (`_cpr_fetch_active_runs`), called at `:260` and `:325`.
- **Call count:** 2 or more paginated listings per invocation. Proposed: none; do not merge.
- **Endpoint:** `GET repos/{repo}/actions/runs?status=queued|in_progress&event=pull_request` with `--paginate`.
- **Evidence:** the in-code comment already records why the two calls stay separate:
  ```bash
  # Keep the two status-filtered calls: dropping the filters can
  # paginate through an unbounded completed-run history.
  ```
- **Proposed fix:** none. Recorded so later passes don't raise it again.
- **Safety rationale:** both calls paginate, and merging them means dropping the `status` filter, which changes the page boundaries and bounds.
- **Downstream signal:** do not implement. Any future proposal needs a single-filter "active" listing proven bounded at the REST layer.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** — `NEEDS_VERIFICATION`
- **Files:**
  - `scripts/check_failure_triage.sh:747-755`
  - `.github/workflows/internal-check-failure-triage.yml:46-61`
  - `.github/workflows/check_failure_triage.yml:32-36` (the `details_url` input) and `:595-599` / `:619-623` (the `CHECK_TRIAGE_*` env).
- **Call count:** 2 per `workflow_run` triage that reaches the base gate. Proposed: 1 when the caller passes the workflow id, with the existing call kept as a fallback.
- **Endpoints:**
  - `GET repos/{repo}/actions/runs/{run_id}`, used only for `.workflow_id`.
  - `GET repos/{repo}/actions/workflows/{workflow_id}/runs?branch=…&event=push&per_page=1`
- **Evidence:** the script recovers the run id by regex-parsing `details_url` and then calls the API just to read one field:
  ```bash
  base_gate_workflow_id="$(gh_retry gh api "repos/${REPO}/actions/runs/${base_gate_run_id}" --jq '.workflow_id // ""' 2>/dev/null || true)"
  ```
  The caller job `triage-workflow-run` already holds `github.event.workflow_run` in its event payload; that object carries `workflow_id`. The wrapper passes only `html_url` (`:61`).
- **Proposed fix:**
  - Add an optional input to `check_failure_triage.yml`, for example `workflow_id` (type string, default `""`).
  - Map it to a new env var, `CHECK_TRIAGE_WORKFLOW_ID`, in both steps.
  - At `check_failure_triage.sh:749-751`, use it when it matches `^[0-9]+$`, otherwise fall back to the existing call.
  - Pass `workflow_id: ${{ github.event.workflow_run.workflow_id }}` in `internal-check-failure-triage.yml:55-61`.
- **Safety rationale:** the data crosses a reusable-workflow boundary. The wrapper calls `check_failure_triage.yml@promote-candidate` (`:54`), so a wrapper that passes the new input before `promote-candidate` declares it will fail. This is a skew risk, not a same-function merge.
- **Downstream signal:** before implementing, check:
  1. The input and env names are unused (grep `workflow_id` and `CHECK_TRIAGE_WORKFLOW_ID`).
  2. The input lands in `check_failure_triage.yml` and reaches `promote-candidate` before the wrapper passes it. Ship it in two PRs, or confirm GitHub's behaviour for undeclared `with:` keys.
  3. Consumer templates under `workflow-templates/` that call the triage workflow still work without the input, since the fallback is kept.
  4. The `html_url` run id always equals `github.event.workflow_run.id`.

**REUSE-002** — `NEEDS_VERIFICATION`
- **Files:**
  - `scripts/promote_candidate_follow.sh:181-184`
  - `.github/workflows/promote-candidate-follow.yml:35-40` ("Checkout main", `ref: main`, `fetch-depth: 1`) and `:52`.
- **Call count:** 1 per run. Proposed: 0, read from the local checkout.
- **Endpoint:** `GET repos/{repo}/git/ref/heads/main`.
- **Evidence:** the job checks out `main` seconds before the script reads the main tip over the API. The script runs from that checkout (`bash scripts/promote_candidate_follow.sh`). The budget comment at `:22-25` says no existing call supplies the data, but the checkout already resolved the same ref.
- **Proposed fix:** set `MAIN_SHA="$(git rev-parse HEAD)"` when `GITHUB_REF == refs/heads/main` and the value passes `follow_valid_sha`, otherwise fall back to the existing `follow_api` read.
- **Safety rationale:** the checkout SHA can be older than the API read. If `promote-candidate` is already ahead of it (for example `pin_freeze` moved it after an unfreeze race), the `force=false` PATCH at `:237-239` is rejected and the run exits `error reason=patch_rejected`. Today's read narrows that window but doesn't close it.
- **Downstream signal:** before implementing:
  1. Confirm from job logs that the checkout SHA always matches the `main.json` read across push, schedule and dispatch triggers.
  2. Add an ancestor check (skip the PATCH when `MAIN_SHA` is an ancestor of `BRANCH_SHA`) so a stale checkout gives `current`, not `error`.

  This is mutually exclusive with MERGE-001 and DEAD-API-001 for the same call; pick one.

**REUSE-003** — `RISKY_SKIP`
- **Files:**
  - `scripts/review_merge_train.sh:958` (gate listing via `_mt_find_marker_comment`)
  - `:993-999` (passes `queued_comment_id` as the 4th argument to `_mt_upsert_comment`)
  - `:879` (re-fetches that comment's body)
  - The listing helper at `:800-806`.
- **Call count:** 1 paginated listing plus 1 `GET` per queued PR that already has a marker. Proposed: the listing alone.
- **Endpoints:**
  - `GET repos/{repo}/issues/{pr}/comments?per_page=100` with `--paginate`.
  - `GET repos/{repo}/issues/comments/{id}`.
- **Evidence:** the listing's jq filter already reads `.body` (`startswith("${marker}")`) and outputs only `id created_at`. `:879` then fetches the same comment's body again, only to skip an identical rewrite:
  ```bash
  existing_body="$(gh_retry gh api "repos/${MT_REPO}/issues/comments/${existing_id}" --jq '.body' 2>/dev/null || true)"
  ```
- **Proposed fix:** have a sibling listing helper also emit `(.body | @base64)`, and compare it in `_mt_upsert_comment` when the caller supplies it.
- **Safety rationale:**
  - The listing paginates.
  - The output is parsed line by line (`sort -k2,2r … | sed -n '1p'`, `:805`), so adding a multi-line body breaks the parse unless it is encoded.
  - The marker state feeds the race-defensive bypass and claim logic (`:965-976`, `:1403-1405`).
- **Downstream signal:** manual review. It saves at most one GET per gate re-queue. A human must confirm that the encoded-body output keeps `_mt_find_marker_comment`'s `id created_at` contract for its existing callers (`:811`, `:939`, `:958`, `:1393`).

### Dead Calls (DEAD-API-###)

**DEAD-API-001** — `NEEDS_VERIFICATION`
- **Files:** `scripts/promote_candidate_follow.sh:181-184` (main read), `:201-229` (freeze handling), `:233` (step-4 guard) and `:255` (final emit).
- **Call count:** 1 per run while a freeze is active, which covers the whole promote cycle, every hourly cron run and every push to main. Proposed: 0 on the `frozen` outcome.
- **Endpoint:** `GET repos/{repo}/git/ref/heads/main`.
- **Evidence:**
  - `MAIN_SHA` is read for every run, but it is used only in step 2 (branch missing, `:187-198`) and step 4 (`:233-243`).
  - When the tag is fresh and pinning is on, `ACTION=frozen` (`:226`), step 4 is skipped by `if [ "${ACTION}" != "frozen" ]`, and the final `follow_emit "${ACTION}" ${FROM_TO}` has an empty `FROM_TO`.
  - The step-5 unprotected alert (`:246-251`) does not use `MAIN_SHA` either. So on frozen runs the call's result is validated and then never used.
- **Proposed fix:** move `:181-184` into a lazy `follow_read_main` helper, called at the top of step 2 and inside the `ACTION != frozen` branch of step 4.
- **Safety rationale:** this changes the error behaviour. Today a failed main read during a freeze exits 1 (`main_read_failed`). After the change, the same run exits 0 with `outcome=frozen`. The observable `PROMOTE_CANDIDATE_FOLLOW outcome=error reason=main_read_failed` line disappears on that path.
- **Downstream signal:** before implementing:
  1. Grep for consumers of `PROMOTE_CANDIDATE_FOLLOW outcome=error` or `reason=main_read_failed` (collector, alerts, `promote_main_cycle.sh`) and confirm none rely on it firing while frozen.
  2. Coordinate with BUG-003's pre-PATCH tag re-read so that both changes touch step 4 in one PR.

### Cross-References to Deep Audit Section
- BATCH-001: `RISKY_SKIP` — this is the poller's wave reconcile loop, and the REST fallback walks a paginated timeline. Before any manual review, check:
  - that adding `body` to the 25-issue × `timelineItems(last: 50)` batch stays within GraphQL cost (inspect `rateLimit{cost}`);
  - that the "last cross-reference" semantics of `_issue_cross_ref_pr_number_last` survive.
- BATCH-002: `RISKY_SKIP` — in `orchestrate_poll_process.sh`, it replaces two paginated REST walks (timeline, `pulls/N/files`). Nested `files(first:100)` under timeline nodes × 25 aliases multiplies query cost, so it needs a manual cost measurement and a `hasNextPage` fallback test.
- API-001: `NEEDS_VERIFICATION` — a per-process memo is sound. Confirm no caller of `label_helpers.sh` deletes and re-ensures the same label within one process, because the memo would then skip the re-create.
- API-002: `NEEDS_VERIFICATION` — I agree with the direction, but the premise is unproven: `gh issue edit --remove-label` may fail only when `ai:done` is missing from the repository, not merely from the issue. Check the gh CLI behaviour before counting the first edit at `implement.yml:5627-5628` as a "guaranteed failure".
- BATCH-003: `RISKY_SKIP` — today's per-head call (`scripts/workflow_failure_heal_pr_reconcile.sh:243`) is a single non-paginated page (`per_page=10`). A `base=<ref>` listing on a busy base such as `main` would need `--paginate`, which could cost more than the H calls it replaces and changes page semantics.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 4 | MERGE-001, REUSE-001, REUSE-002, DEAD-API-001 |
| RISKY_SKIP | 4 | MERGE-002, MERGE-003, MERGE-004, REUSE-003 |

The cross-referenced Deep Audit items are tagged in the section above and not counted here: BATCH-001, BATCH-002 and BATCH-003 are `RISKY_SKIP`; API-001 and API-002 are `NEEDS_VERIFICATION`.

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.