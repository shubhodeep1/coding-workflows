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