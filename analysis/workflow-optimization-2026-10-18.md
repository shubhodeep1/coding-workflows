## Executive Summary

Analysis window: about 12 hours, 2026-10-17 12:32Z to 2026-10-18 00:41Z, inferred from run timestamps. It covers 1,000 runs in one repo, `shubhodeep1/coding-workflows`. Results: 348 success, **0 failures**, 9 cancelled, 643 other (almost all skipped). `insufficient_data` is false.

The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox. Every log-level claim below therefore comes from the `log_summary` fields, with spot checks against the repo source.

- **The orchestrate poller cannot keep up with its own schedule (high confidence).**
  - `internal-orchestrate-poll.yml` fires every 5 minutes (`*/5 * * * *`). Each poll takes p50 572s and p95 964s, and the job-level `concurrency` group in `orchestrate_poll.yml:68-73` has `cancel-in-progress: false`.
  - Result: 79 poll runs in about 12.1h, one every ~9.2 min, against ~145 scheduled ticks. The poller is busy about 89% of the time (73 × ~534s).
  - The "waiting for runner" time seen in poll runs is the run waiting behind the previous poll. Pickup times match the previous run's end almost to the second: 39072882176 picked up at 21:56:48, and 39072324248's poll ended at 21:56:47.
  - Impact: orchestrator reaction latency is ~9–12 min instead of 5, plus 94–191s of hidden wait per tick.
- **Poll runs are a telemetry blind spot (high confidence).** In every sampled poll run about 822,800–822,975 characters are cut from the middle of the log. No token, API, memory, Semble or Serena signal is visible for the family that holds the CPU-time majority.
  - Impact: the top speed and API questions can't be answered yet.
  - Fix: collect per-step timings (no extra API calls), and have the poller print a summary line at the end of the run, where truncation can't remove it.
- **Review & Autofix accounts for 99.8% of OpenRouter tokens (high confidence).** It used 42.93M of 43.00M total tokens, in just 4 runs and 36 calls, at `cache_hit_rate` 0.747.
  - Run 39035504863 (`[pr:7570]`) hit only **0.41**; the others ranged 0.73–0.87.
  - Est. savings: about 4.4M uncached input tokens per similar window if the hit rate reaches 0.85.
- **The Codex token count is probably wrong (medium confidence).** Implement runs 39050040144 and 39033114795, four hours apart, each report exactly **11 calls and 1,322,139 tokens**.
  - `CODEX_TOKENS_RE` in `scripts/cost_audit.py:62-63` matches any `tokens used` text in the log, so a static echoed block could produce identical totals.
  - Impact: Codex spend (2.64M tokens) can't be trusted until this is fixed.
- **Some Semble work is wasted, and implement fell back at runtime (medium confidence).**
  - Workflow Log Analysis 39033601113 ran 3 Semble bootstraps (119,974 ms) and used none of them.
  - Two implement runs, 39050040144 and 39033114795, logged `semble_runtime_fallbacks=1` each with 0 bootstraps.
  - Review's use is efficient: 4 queries, 67,066 bytes, `static_dup_bytes=0`.
- **Reliability is clean, apart from one deprecation risk (high confidence).** Every sampled poll run warns `Node.js 20 is deprecated`, with `actions/cache/restore@v4` forced onto Node 24. There were 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` events.

## Speed Optimizations

**Critical path**

1. **Stop the poller serializing behind itself.**
   - Evidence: five measured pairs where a run's pickup equals the previous run's end:

     | Waiting run | Pickup | Previous run | Previous end | Wait |
     |---|---|---|---|---|
     | 39072882176 | 21:56:48 | 39072324248 | 21:56:47 | 137s |
     | 39075926650 | 22:40:33 | 39075309419 | 22:40:29 | 94s |
     | 39064648619 | 19:59:36 | 39064070967 | ~19:59:31 | 143s |
     | 39060353890 | 19:00:39 | 39059772593 | 19:00:34 | 191s |
     | 39077050216 | 22:59:18 | 39076491631 | 22:59:14 | 183s |

     There are also 6 cancelled poll runs. My inference is that these are pending ticks replaced by newer ones in the concurrency group.
   - Root cause: a fixed ~6.5–10 min per tick, even when idle. Evening runs 39073888039 (393s) and 39081053844 (403s) had nothing visible to dispatch, yet the cron is 5 min.
   - Change:
     - First, measure. See the diagnostic additions under Reliability: per-step timings in the collector, and a `POLL_TICK_SUMMARY` line.
     - Then gate the heavy install steps on whether `Find active tracking issues` (`orchestrate_poll.yml:247`) found work. Candidates are `Install Codex CLI` (L427), `Install OpenCode CLI…` (L437), `Install Claude Code CLI` (L1089) and `Install semble` / `Build semble index` (L869/L892). Keep the current default behaviour behind a repo variable that defaults to running them.
   - Est. savings: 94–191s of hidden wait per tick, and roughly halving the effective tick interval if idle ticks drop under 5 min. Exact numbers wait on step timings.
   - Risk: medium. The gating must not skip standalone stall recovery or PR sweeps (L1392–L1564), which run when no projects are active.

2. **Review & Autofix runs are 15–43 min and run alongside CI (14–25 min).**
   - Evidence: review 39035508230 (2613s) and CI 39035508117 (1209s) were both created at 13:10:59.
   - Other slow review runs: 39034952244 2168s, 39037270431 1955s, 39035504863 1901s, 39034705425 1834s. Review p95 is 1909s; CI p50 948s and p95 1394s.
   - Root cause: review takes the longest of the two, at about 10 model sessions per run (`or_calls`=10), with Semble bootstrap 37.6–43.6s per run.
   - Change: start the Semble bootstrap at job start alongside checkout and tool installs, if it isn't already. Then focus on the per-call token load (see Cost #1).
   - Est. savings: about 40s per review run on the critical path if the bootstrap is currently serial (inference).
   - Risk: low.

3. **Implement runs take 23–44 min.**
   - Evidence: 39050040144 2665s, 39033114795 1951s, 39035201898 1818s, 39035279129 1789s, 39034348950 1762s.
   - Root cause: unknown. There's no codex cache telemetry and no step timings.
   - Change: add step timings (collector) before tuning.
   - Risk: none (diagnostic only).

**Micro-optimizations**

4. **Unused Semble bootstraps in Workflow Log Analysis.** Run 39033601113 spent 119,974 ms on 3 bootstraps with `semble_bootstraps_unused=3`, 5.1% of its 2335s. Change: don't bootstrap when this workflow has no Semble query consumer. Est. savings: about 120s per run. Risk: low.
5. **Skipped-run fan-out.** 640 of 1,000 runs are skip-only: clarify 126, plan 115, implement 110, clarify_respond 121, failure_heal 95, heal_intake 39, check_failure_triage 34. One event at 00:04:02 started 4 workflows: 39081384493, 39081384553, 39081384559, 39081384649. Most finish in about 1s, but implement 39081384559 took 12s while skipped. Change: move the event filter into the caller's job-level `if:` so the skip happens before a runner is assigned. Est.: about 10s of runner time per skipped implement. Risk: low.

## Cost Optimizations

1. **Review & Autofix uncached input and cache fragmentation.**
   - Evidence: family totals are 31,833,087 cache-read, 8,999,502 uncached prompt, 1,755,230 cache-write and 340,434 completion tokens. Completion is only 0.8%, so cost is driven by input.
   - Per run:

     | Run | Total tokens | Calls | Hit rate |
     |---|---|---|---|
     | 39034952244 | 15.81M | 10 | 0.736 |
     | 39037270431 | 14.30M | 10 | 0.731 |
     | 39034705425 | 10.23M | 6 | 0.873 |
     | 39035504863 `[pr:7570]` | 2.59M | 10 | **0.412** |

     Run 39035504863 had 1.15M uncached and 0.36M write against 1.06M read.
   - Root cause (inference): the shared prompt prefix varies from call to call. Changing content (diff, findings, timestamps) probably sits before stable content, or calls are spaced past the cache lifetime.
   - Change: put rubric, system text and repo context first and PR-specific content last. Add a diagnostic `PROMPT_PREFIX_HASH` line (see below) to prove which prefix is changing.
   - Est. savings: raising the family hit rate from 0.747 to 0.85 moves about 4.4M of 42.6M input tokens from uncached or write to cache-read.
   - Quality risk: none if only the order changes.

2. **Codex token count likely over- or mis-counted.**
   - Evidence: two implement runs report identical `codex_calls=11` and `codex_tokens_used=1322139`.
   - Root cause: `CODEX_TOKENS_RE` (`cost_audit.py:62-63`, applied at L690-693) matches any `tokens used` followed by digits anywhere in the log, including echoed script or prompt text.
   - Change: emit one structured `CODEX_USAGE` line per Codex call and count only that, keeping the old regex as a fallback when no `CODEX_USAGE` lines are present.
   - Savings: accurate cost data rather than tokens. It is the prerequisite for any Codex model or reasoning-level decision.
   - Risk: low.

3. **test_and_mark_stable never reads its cache.** Run 39033435276 made 12 calls with 68,248 cache-write, **0** cache-read and 70,383 prompt tokens (`cache_hit_rate` 0.0) over 5367s. That pays the cache-write premium with no return. My inference is that the calls are spaced past the cache lifetime in an 89-minute run. Change: skip cache markers on this path, or group the calls. Savings: small (about 68K write-premium tokens per release run). Risk: none.

4. **Semble and Serena value.**
   - Semble in review: 4 queries, 67,066 bytes (~16.8 KB, roughly 4K tokens each), 19 sources, `static_dup_bytes_total=0`, no fallbacks. That is about 0.04% of the ~10.7M tokens per review run, so it adds little noise. The telemetry can't show whether it reduces downstream expansion, because there's no with/without comparison.
   - The cost to watch is bootstrap time (~42s per run), not bytes.
   - Serena: every counter is zero, including the probes, so there's nothing to assess.
   - Action: none for Semble bytes. Confirm whether Serena is meant to be enabled.

5. **No model, reasoning-level or context-budget pressure observed.** `context_budget_warn_count`=0 and `break_glass_count`=0. The poll runs show `MODEL_REASONING_EFFORT_JUDGE: high` (e.g. 39080552241), but judge token usage isn't visible because of truncation. Don't change reasoning levels until poll token lines are visible.

## Reliability Improvements

Failure rate is 0/1000, so these items reduce risk and blind spots rather than an existing failure rate.

1. **Node 20 deprecation on `actions/cache/restore@v4`.**
   - Evidence: the warning appears in every sampled poll `log_summary` (e.g. 39081053844, 39079475785, 39078863808, 39055155480).
   - Category: dependency end-of-life.
   - Fix: move to an `actions/cache` release that targets Node 24 once its tag is checked in the action's own repo.
   - Impact: avoids a poller-wide failure when Node 20 is removed.
   - Rollback: revert the pin.

2. **Cancellations.** 9 runs: 6 orchestrate_poll (inferred to be replaced pending ticks; harmless, but they confirm Speed #1) and 3 implement (cause not visible). Fix: the collector already records `_extract_cancelled_point` (`collect_workflow_logs.py:1053`); surface it in the analysis rows so implement cancellations can be told apart (concurrency versus timeout versus manual).

3. **Semble fallbacks.**
   - Contract-test fallbacks: 8 in total, 4 in CI 39047759102 and 4 in test_and_mark_stable 39033435276. These are healthy, deliberate test-path fallbacks.
   - Runtime fallbacks: 2, in implement 39050040144 and 39033114795, with **0 bootstraps** in the implement family. These are 2 of the 4 implement runs with parsed telemetry, which looks like Semble simply not being available in implement and failing open, rather than a rare fallback.
   - Fix: make the implement fallback line carry a `reason=` value, then either provision Semble in implement or turn it off there explicitly so this stops counting as a fallback.
   - Rollback: the existing fail-open behaviour stays.

4. **Orphan-run warning without identifiers.** `cancel_on_pr_close` 39080835884 warned "Scheduled close cleanup preserved 3 active run(s) without pull-request linkage." Fix: add the run IDs and workflow names to that warning so orphans can be traced. It's a log-only change.

5. **Proposed diagnostic logging additions.** New prefixes need an `agents.d/` fragment under "Stable log prefixes (contractual)" (§25). New env flags default to on and fail open.

   ```text
   # collect_workflow_logs.py — no new API calls: jobs[].steps[] is already fetched (L1020-L1060)
   run_row["step_timings"] = [{"job": j.name, "step": s.name, "ms": completed_at - started_at, "conclusion": s.conclusion}]

   # orchestrate_poll_process.sh — one line per top-level phase, plus a tail summary in the "Write run summary" step (orchestrate_poll.yml:1877)
   POLL_PHASE_TIMING phase=<tracking_scan|process_issue|stall_recovery|pr_sweeps|snapshot> ms=<n> items=<n> issue=<n|none>
   POLL_TICK_SUMMARY ms_total=<n> issues=<n> dispatched=<n> judges=<n> gh_calls=<n|unknown> idle=<true|false>

   # Codex wrapper — replaces free-text regex matching in cost_audit.py
   CODEX_USAGE phase=<phase> call=<i> tokens=<n> run_key=<run_id-attempt>

   # OpenRouter caller — cache fragmentation diagnosis (hash only, never prompt content)
   PROMPT_PREFIX_HASH phase=<phase> call=<i> prefix_bytes=<n> sha256_16=<hex>
   ```

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary` in this window. That includes the poll runs, whose summaries list it as not visible because of truncation, and the one memory_maintenance run, 39035746363 (1281s, no summary available).
- So there is no retrieve hit rate, `estimated_tokens` versus budget, `keyword_method` mix, or `fail_open`/`enabled:false` data for this window.
- Likely cause: the poll log loses its middle 822K characters, and memory retrieves happen mid-run (inference).
- Action: add memory-op counts to `POLL_TICK_SUMMARY` (e.g. `mem_retrieve=<n> mem_zero_hits=<n> mem_fail_open=<n>`), so a tail-retained line carries them. Also have the collector scan full archives for `AI_MEMORY_TELEMETRY:` the way it already scans for `GH_PAT_BUDGET phase=end` (`collect_workflow_logs.py:1702`).

## GH API Call Audit

| Source | Evidence | Assessment |
|---|---|---|
| review_autofix_sweep | `used_in_job` = 2, 4, 4, 4, 4, 8, 9, 10, 11, 13, 13 across sweeps (e.g. 39081203469=11, 39079277228=2, 39077316069=13, 39058717808=10) | Low and healthy |
| workflow-failure-heal-intake | 39071706352 `used_in_job=57` | Moderate, one-off dispatch |
| Overall PAT drain | Same reset window, :00 sweep vs :30 sweep: 4644→3928 (716), 4616→3863 (753), 4642→3821 (821), 4654→3952 (702). Heal intake at 21:37 saw 3593 (228 more in ~7 min) | ~1.4–1.6K calls/hour, about 30% of the 5,000/hour budget; no 429s seen anywhere |
| orchestrate_poll | Emits `GH_PAT_BUDGET phase=end` (`orchestrate_poll.yml:1974-1984`), but the value isn't in the analysis rows | **Gap.** Probably the main consumer (inference: it is the only near-continuous workflow) |
| cancel_on_pr_close (scheduled) | ~68 runs, every one `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` | Each scan costs at least a listing call; volume can't be measured because ~33.5K characters are truncated per run |

Recommendations:
1. Surface the poller's `GH_PAT_BUDGET phase=end used_in_job` in each poll row of `analysis_context.json`. The collector already parses these lines, so this needs no new API calls.
2. Add `gh_calls=` to `POLL_TICK_SUMMARY` so API use is tied to individual phases.
3. Once measured, check per-issue loops in `Process each tracking issue` (`orchestrate_poll.yml:1189`) against the existing batched lookups (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) and the cycle-local caches required by §14.
4. In scheduled `cancel_on_pr_close`, consider skipping the merge-train release scan when the poller's merge-train backstop (`orchestrate_poll.yml:218`) has already run in the same 10-minute slot.

Estimated reduction can't be computed until the poller's budget is visible. Rate-limit risk is currently low.

## Prompt Cache & Memory System

- **Overall `cache_hit_rate` is 0.745.** Review & Autofix is 0.747 and test_and_mark_stable is 0.0. Codex families report no cache data.
- **Fragmentation signals:**
  - In run 39035504863 (`[pr:7570]`), the cache-write share (0.36M) is about a third of the reads (1.06M), which suggests each session starts with a fresh prefix. That could be a first review on this PR, or a prefix that changes between calls.
  - Run 39034705425 reached 0.873 with 6 calls. Fewer, longer sessions reuse the prefix better.
  - test_and_mark_stable writes 68,248 tokens to the cache and never reads them.
- **Context-budget pressure:** none (`context_budget_warn_count`=0).
- **Fail-open:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` (poll env, 39080552241). `or_usage_unavailable_calls`=0, so all 48 OpenRouter calls returned usage data.
- **Memory retrieval:** no telemetry (see AI Memory Health).
- **Recommendations, in order:**
  1. `PROMPT_PREFIX_HASH` per call, to measure prefix churn per phase.
  2. Order review prompts with stable content first and PR-specific content last.
  3. Skip cache markers for test_and_mark_stable.

  Estimated impact: about 4.4M fewer uncached or write tokens per similar window, and somewhat lower review latency from cached prefill (inference).

## Orchestrator Health

- **Tick cadence is degraded.** The effective interval is about 9.2 min against a 5-minute cron, the poller is busy ~89% of the time, and 6 pending ticks were replaced. This is the top operational pain point. It doesn't cause failures; it adds 4–7 min to every hand-off between phases.
- **Idle ticks are still expensive.** After 17:40Z, clarify, plan and implement were all skipped (e.g. 39081384493, 39081384553, 39081384559). Polls still ran 393–757s (39073888039 393s, 39060353890 757s including 191s of wait).
- **Clarification loops.** orchestrate_clarify_respond: 124 runs, 121 skipped, 3 success. Two of the successes were long: 39039507032 at 917s and 39041311337 at 674s. Loop count can't be measured without per-issue telemetry.
- **Validate naming anomaly.** Run 39033597276 is named `Internal: AI Validate [tracking:0]` (744s). The `0` may mean the tracking-issue number wasn't passed through (inference). Check the validate dispatch inputs.
- **Wave progression:** integration_pr_readiness 25/25 success (p50 33s, p95 511s), forward_merge_stable_to_main 2/2, auto_release_stable 2/2 (39058864432 `AUTO_RELEASE_SKIPPED reason=up_to_date`). No conflict-heal retries or stuck states are visible.
- **Indicators to track:**

  | Indicator | Target |
  |---|---|
  | Poll run count per hour | ≥ 10 |
  | `POLL_TICK_SUMMARY ms_total` p50 | < 240s |
  | Wait-behind-previous-poll (pickup minus previous end) | ≈ 0 |
  | Replaced (cancelled) poll ticks per day | 0 |
  | `idle=true` share | — |
  | Poller `GH_PAT_BUDGET used_in_job` | — |

## Pipeline Flow Bottlenecks

| Stage | Evidence (successful runs) | Overhead type |
|---|---|---|
| clarify | p95 360.5s; 10 success / 136 | Compute (small) |
| plan | Successful runs 659–885s (39041751790 885s, 39040173120 830s, 39049159075 799s) | Compute |
| implement | 1363–2665s | Compute (largest single stage) |
| CI | p50 948s, p95 1394s; max 1527s (39034665320) | Compute, alongside review |
| review/autofix | 690–2613s full runs | Compute + Semble bootstrap (~42s) + model sessions |
| validate | 744s (39033597276) | Compute |
| orchestrate hand-off | ~9.2 min tick + 94–191s wait behind previous poll | **Queueing** |
| retries | `retries=0` across every row | None |
| merge/conflict | Merge-train `examined=0` on every scheduled scan | None observed |

Estimated end-to-end path for one issue: clarify (~6 min) → **poll hand-off (~10 min)** → plan (~13 min) → **poll hand-off** → implement (~30 min) → review alongside CI (~25–40 min) → **poll hand-off** → validate (~12 min). That is three or more poll hand-offs at ~10 min each, about 30 min of pure queueing (inference based on how phases are dispatched).

Fixes by end-to-end impact:
1. Shorten poll ticks below 5 min (Speed #1). Saves about 15 min per issue.
2. Reduce per-call review input (Cost #1).
3. Run Semble bootstrap in parallel or skip it when unused (Speed #2 and #4).
4. Profile implement with step timings.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - The poller is saturated: p50 572s on a 5-minute cron, with 94–191s of waiting behind the previous poll.
  - Implement takes 23–44 min.
  - Review & Autofix takes 15–43 min.
- **Top failure modes:** no failures (0/1000). Risks:
  - The Node 20 deprecation in every poll run.
  - 3 implement cancellations with no recorded cause.
  - Semble runtime fallbacks in 2 of the 4 implement runs with telemetry.
  - Poll logs truncated so heavily that no mid-run telemetry is visible.
- **Highest-cost drivers:**
  - Review & Autofix OpenRouter: 42.93M tokens, 9.0M of them uncached.
  - Implement Codex: 2.64M tokens, a count I suspect is inflated or wrong.
  - Poller judge tokens: not visible.
- **Top 3 actions:**
  1. Ship the poller diagnostics: collector `step_timings` plus `POLL_TICK_SUMMARY` and `POLL_PHASE_TIMING`. Then gate the heavy installs on active work.
  2. Fix Codex token counting with a structured `CODEX_USAGE` line and add `PROMPT_PREFIX_HASH`. Then reorder review prompts to lift the cache hit rate from 0.747 toward 0.85.
  3. Move `actions/cache/restore` to a Node 24-targeting release, and skip Semble bootstrap where it goes unused (Workflow Log Analysis) or is unavailable (implement).

## Metrics Appendix

**Run outcomes by family (window ≈12.1h)**

| Family | Runs | Success | Cancelled | Other/skipped | p50 s | p95 s |
|---|---|---|---|---|---|---|
| ALL | 1000 | 348 | 9 | 643 | 1 | 947 |
| orchestrate_poll | 79 | 73 | 6 | 0 | 572 | 964 |
| review_autofix | 58 | 56 | 0 | 2 | 157 | 1909 |
| ci | 32 | 32 | 0 | 0 | 948.5 | 1393.6 |
| implement | 123 | 10 | 3 | 110 | 1 | 1711 |
| plan | 124 | 9 | 0 | 115 | 1 | 655 |
| clarify | 136 | 10 | 0 | 126 | 1 | 360.5 |
| orchestrate_clarify_respond | 124 | 3 | 0 | 121 | 1 | 10 |
| cancel_on_pr_close | 68 | 68 | 0 | 0 | 11 | 121.7 |
| workflow_failure_heal | 95 | 0 | 0 | 95 | 1 | 8 |
| workflow_failure_heal_intake | 53 | 14 | 0 | 39 | 7 | 24.4 |
| check_failure_triage | 34 | 0 | 0 | 34 | 1 | 9 |
| integration_pr_readiness | 25 | 25 | 0 | 0 | 33 | 511 |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 5367 | 5367 |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 2335 | 2335 |
| validation_refresh | 1 | 1 | 0 | 0 | 1879 | 1879 |
| memory_maintenance | 1 | 1 | 0 | 0 | 1281 | 1281 |
| validate | 1 | 1 | 0 | 0 | 744 | 744 |

Failure rate is 0.0% in every family.

**Tokens and cache**

| Scope | Total tokens | Prompt (uncached) | Cache read | Cache write | Completion | Calls | `cache_hit_rate` |
|---|---|---|---|---|---|---|---|
| All OpenRouter | 43,001,534 | 9,069,885 | 31,833,087 | 1,823,478 | 343,332 | 48 | 0.745 |
| review_autofix | 42,928,253 | 8,999,502 | 31,833,087 | 1,755,230 | 340,434 | 36 | 0.747 |
| test_and_mark_stable | 73,281 | 70,383 | 0 | 68,248 | 2,898 | 12 | 0.0 |
| Codex (implement) | 2,644,278 (suspect) | — | — | — | — | 22 | n/a |

**Review-telemetry metrics**

| Metric | Value |
|---|---|
| `wall_clock_p50_ms` | 14,000 overall; review_autofix 15,500; implement 981,500 |
| `wall_clock_p99_ms` | 2,658,240 overall; review_autofix 2,537,350; implement 2,643,580 |
| Wall-clock samples | 114 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |
| Runs with log telemetry | 115 / 1000 |

**GH API (from `GH_PAT_BUDGET` lines in summaries)**

| Workflow / job | `used_in_job` samples | Remaining range |
|---|---|---|
| review_autofix_sweep / sweep | 2–13 (n=11 quoted) | 3,779–4,722 |
| workflow-failure-heal-intake / intake | 57 (39071706352) | 3,593 |
| orchestrate_poll / poll | not surfaced (gap) | — |
| HTTP 429 / secondary rate limit | 0 observed | — |

**Semble**

| Family | Queries | Bytes | Sources | Bootstraps (ms) | Unused | Failed | Fallbacks (contract-test / runtime) | static_dup bytes |
|---|---|---|---|---|---|---|---|---|
| review_autofix | 4 | 67,066 | 19 | 4 (166,786) | 0 | 0 | 0 / 0 | 0 |
| workflow_log_analysis | 0 | 0 | 0 | 3 (119,974) | 3 | 0 | 0 / 0 | 0 |
| implement | 0 | 0 | 0 | 0 | 0 | 0 | 0 / 2 | 0 |
| ci | 0 | 0 | 0 | 0 | 0 | 0 | 4 / 0 | 0 |
| test_and_mark_stable | 0 | 0 | 0 | 0 | 0 | 0 | 4 / 0 | 0 |
| **Total** | 4 | 67,066 | 19 | 7 (286,760) | 3 | 0 | 8 / 2 | 0 |

Echo lines dropped: 0.

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | tool_calls | fallbacks |
|---|---|---|---|---|---|---|---|
| Serena (all families) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Semble (poller) | n/a: `SEMBLE_AVAILABLE=false` set at `orchestrate_poll.yml:96` | — | — | 0 | — | — | 0 |

No other MCP servers were observed.

**Data gaps**
- The run-logs directory was not mounted.
- Poll logs are truncated by about 822.8K characters per run.
- The poller's `GH_PAT_BUDGET` isn't surfaced in the analysis rows.
- There is no `AI_MEMORY_TELEMETRY`, no Codex cache telemetry, and no per-step timings.
- Merged-PR count isn't in the context.

Next collection steps: add `step_timings`, `POLL_TICK_SUMMARY`, `CODEX_USAGE` and `PROMPT_PREFIX_HASH` as described under Reliability Improvements, and add poll `GH_PAT_BUDGET` to the analysis rows.

---
**What I did:** worked from the supplied telemetry and `log_summary` fields, and checked `orchestrate_poll.yml` (cron, concurrency, step list), `scripts/collect_workflow_logs.py` (jobs/steps fetch, PAT-budget scan) and `scripts/cost_audit.py` (Codex regex). No files were changed.

**Assumptions:**
- The 6 cancelled poll runs are pending ticks replaced in the concurrency group.
- The poller is the main PAT consumer.
- Prompt-prefix churn is behind review's cache misses.

All three are inferences, labelled as such where they appear above.

**Missing context:** the run-logs directory at `/home/runner/work/_temp/workflow-log-output` doesn't exist in this sandbox, so none of the deep-dive log claims could be checked against full logs.
## Deep Audit — Workflows & Scripts (2026-10-18)

### Section 1: Bug & Correctness Sweep

Scope: I read in depth `orchestrate_poll.yml` (all of it), `cancel_on_pr_close.yml` (all of it), selected steps of `implement.yml`, `plan.yml` and `test-and-mark-stable.yml`, and these scripts: `scripts/orchestrate_poll_process.sh` (main loop, label and default-branch helpers, judge loops, standalone sweep), `scripts/gh_helpers.sh` (the retry family and `curl_gh_api`), `scripts/review_collect_pr_metadata.sh`, `scripts/label_helpers.sh`, `scripts/memory_helpers.sh`, `scripts/review_merge_train.sh` and `scripts/lint_pr_body_auto_close.py`. The other workflows and scripts got targeted searches only.

Already covered by the existing report and not raised again:
- gating idle-tick installs (Speed #1);
- the `CODEX_TOKENS_RE` miscount (Cost #2);
- missing run IDs in the orphan-run warning at `cancel_on_pr_close.yml:240-243` (Reliability #4);
- the merge-train double scan (GH API Call Audit, recommendation 4).

I checked and cleared two candidates:
- **`steps.baseline_refctx.outputs.branch` interpolated into shell (`implement.yml:1895-1908`).** The branch is held to `ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+` (`implement.yml:1125`) and then checked against GitHub, so it cannot carry an injection.
- **`ISSUE_BODY` (`implement.yml:972`).** It is passed through `env:`, not interpolated.

**BUG-001** — `.github/workflows/orchestrate_poll.yml:1598-1599`, `:1762-1777` — Severity: Medium — Category: `bug`
- Description:
  - On idle ticks, "Stage workflow support files" (`id: stage_support`, L563-566) is `continue-on-error: ${{ steps.find_tracking.outputs.has_work != 'true' }}`. That step is what installs `worktree_gc.sh` and `build_state_snapshot.py` (they are in its L572 list).
  - Two later steps call those files with no guard:
    - "Run worktree registry GC" (`run: bash scripts/worktree_gc.sh`) has no `if:` and no `continue-on-error`.
    - "Build state snapshot" (`always()`) runs `python3 scripts/build_state_snapshot.py`, also without `continue-on-error`.
  - In a consumer repo, `scripts/` holds only what staging installed. A staging failure that the job deliberately tolerated therefore turns into a hard job failure (exit 127 or 2), and "Notify Telegram on job failure" (L1922-1972) sends an ERROR page.
  - Two neighbouring steps already guard against exactly this: "Run standalone PR sweeps without active projects" (L1591-1595) and "Replay failed trusted reclarify commands" (L858-861).
  - In this repository the root checkout (L398-402) provides `scripts/`, so the impact is consumer-only. That is an inference.
- Recommended fix: Start both bodies with the same guard as L1591-1595, for example `[ -f scripts/worktree_gc.sh ] || { echo "::warning::worktree_gc.sh not staged; skipping registry GC."; exit 0; }`. Use the equivalent check for `scripts/build_state_snapshot.py`. The step names and `if:` conditions stay unchanged (§10).

**BUG-002** — `.github/workflows/implement.yml:5416-5433` — Severity: Medium — Category: `bug`
- Description:
  - In "Handle no-op implementation", the fall-through path posts the issue comment (`gh_retry gh api ".../comments" -f body=...`, L5416-5417) under `set -euo pipefail` with no `|| ...`. Only after that does it apply `ai:implementation-failed` (L5430-5433).
  - `gh_retry` gives a field-bearing POST exactly one attempt (`gh_helpers.sh:1067-1069`; `_gh_api_args_unsafe_post` at L678-742 returns "unsafe" for any non-GraphQL request with fields).
  - So one transient 5xx on the comment aborts the step before the label lands. The comment text itself says the label is what triggers "orchestrator re-processing" (L5412), so the issue is left without the state transition the orchestrator expects.
- Recommended fix: Apply the label edits (L5422-5433) before the comment. Make the comment best-effort: `... -f body="${noop_body}" >/dev/null || echo "::warning::no-op comment post failed for #${ISSUE_NUMBER}"`. This matches the `|| true` already used for the comment posts in Guard 0 and Guard 2 (L5253-5254, L5387-5388).

**BUG-003** — `.github/workflows/orchestrate_poll.yml:280-292` — Severity: Low — Category: `bug`
- Description: "Find active tracking issues" lists tracking issues with `--limit 20` and never checks whether the result hit that cap. A 21st open `ai:orchestrator-tracking` project is silently skipped on every tick, and nothing in the log says so. The Claude-engine listing in the same workflow does detect its cap (L1040-1042, `length >= 1000` → `standalone_listing_capped`).
- Recommended fix: After L287, add `if [ "${COUNT}" -ge 20 ]; then echo "::warning::TRACKING_LISTING_CAPPED limit=20 ..."; fi`. Alternatively, raise `--limit` and keep the warning. Either way, keep the listing as a single call.

**BUG-004** — `scripts/memory_helpers.sh:151` — Severity: Low — Category: `bug`
- Description: `git ls-remote --heads origin "${branch}" | grep -q "${branch}"` is a substring match. A remote branch named, say, `feature/ai-memory-x` makes the default `ai-memory` look present, so the orphan-branch bootstrap (L155-188) is skipped and later `--branch ai-memory` clones fail. Those failures are fail-open, so memory silently stays disabled.
- Recommended fix: Use an exact ref test: `git ls-remote --exit-code --heads origin "refs/heads/${branch}" >/dev/null 2>&1`.

**SEC-001** — `.github/workflows/orchestrate_poll.yml:404-413`, `:1824` — Severity: Low — Category: `security` [NEEDS VERIFICATION]
- Description:
  - "Configure git auth for memory helper clones" writes the PAT into the remote URL (`git remote set-url origin "https://x-access-token:${GH_TOKEN}@..."`). The token therefore sits in plain text in `.git/config` for the rest of the job.
  - "Publish state snapshot branch" then copies it into a second repository (`git remote add origin "$(git remote get-url origin)"`, L1824).
  - `actions/checkout` already persists an auth header, so the extra exposure is mostly defence-in-depth. The real question is whether any judge or Codex subprocess in this job can read the workspace `.git/config`; I did not verify that.
- Recommended fix: Leave the URL clean and pass credentials per command, for example `git -c http.extraheader="AUTHORIZATION: basic ..."` or a credential helper scoped to the memory clone commands. Alternatively, confirm and document that the isolated judges (`codex_isolated_exec.sh`) cannot see `GITHUB_WORKSPACE/.git`.

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `scripts/orchestrate_poll_process.sh:3829, 5114, 5554, 14743, 22978, 23150, 23441, 23488, 23533, 23939, 24009, 24086, 28164, 28509, 29271` — Severity: Medium — Category: `api-redundancy`
- Description:
  - The repository default branch is re-fetched with `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch'` at 15 separate sites.
  - The same file already has a per-process memo, `_review_run_default_branch` (L22138-22190, which documents "at most one `GET repos/<repo>` per poller process (§14)").
  - Lines 22978–28509 sit inside the per-tracking-issue loop that opens at L22760. That is an inference: the loop body appears to run until the standalone sweep header at L29255. If so, these are the per-iteration calls §14 treats as review blockers.
- Current: up to 15 REST GETs per tick, of which up to about 11 are per tracking issue. Proposed: 1 per poller process.
- Recommended fix: Route every site through the existing memo while keeping each caller's fallback, for example `DEFAULT_BRANCH_TRACKING="$(_review_run_default_branch || echo main)"`. Pattern to extend: `_review_run_default_branch` (orchestrate_poll_process.sh) or `_autofix_review_default_branch_prime` (gh_helpers.sh:1898-1919).
  - Caveat: the memo also caches a failure, so after one failed read every site would use its fallback for the rest of the tick instead of retrying. See CONSIST-005.

**API-002** — `scripts/orchestrate_poll_process.sh:22940`; `.github/workflows/orchestrate_poll.yml:280-285` — Severity: Low — Category: `api-redundancy` [NEEDS VERIFICATION]
- Description: At the top of each tracking-issue iteration the poller fetches `TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"` (REST `issues/N/labels`, L4130-4133). The same tick already listed those labels via `gh issue list --json number,title,labels` into `tracking_issues.json`.
- Current: 1 REST call per tracking issue per tick, before any mutation. Proposed: 0 for the first read; read `.[tidx].labels[].name` from the cached file. Keep the existing refetches that follow label mutations (L22975, L23336, L23602).
- Risk: the listing can be minutes old by the time issue N is processed. Only use the cache for issues processed within a bounded window (for example, the first iteration), or accept the staleness explicitly in a comment.

**API-003** — `scripts/orchestrate_poll_process.sh:9234-9264`, `:28754-28782` — Severity: Low — Category: `api-batching`
- Description: Both judge new-issue loops (`create_judge_fixup_issues_from_verdict` and the `in_progress)` arm) call `get_issue_labels_json "${EXISTING_NUM}"` once per `new_issues[]` entry that already has a mapped issue.
- Current: N REST calls, where N is the judge entries with an existing mapping. Proposed: ceil(N/25) aliased GraphQL calls.
- Recommended fix: Collect the `EXISTING_NUM` values first and fetch their labels in one batch. Model a light, labels-only helper on `_fetch_candidate_issue_details_graphql` (L18015 onward, batch_size 25) rather than reusing it, because it also fetches `comments(last: 100)` and timeline items. Fall back to the per-issue REST call on a batch miss (§14 fail-open).

**BATCH-001** — `scripts/orchestrate_poll_process.sh:14647-14650`, `:14683-14685`, `:14708-14711`, `:14847-14848` — Severity: Low — Category: `api-batching`
- Description: Each validation outcome applies its label transition as two or three separate `gh issue edit` calls: `set_tracking_phase_label` (which builds its own `edit_args` at L4100-4108), then a separate `--remove-label "ai:validate-failed"`, then `--remove-label "ai:harness-broken"`.
- Current: 3 calls per transition. Proposed: 1.
- Recommended fix: Add an optional list of extra labels to remove to `set_issue_phase_label` (new parameter, existing callers unchanged), appended to `edit_args`. Keep the existing tolerance for missing labels (L4111-4114).

**API-004** — `scripts/orchestrate_poll_process.sh:29303-29318`, `:29373-29404` — Severity: Medium — Category: `api-redundancy` [NEEDS VERIFICATION]
- Description:
  - The standalone conflict sweep already fetches `mergeStateStatus` for every candidate PR in 30-PR GraphQL batches (L29303), but only uses it to skip `CLEAN` PRs (L29373-29375).
  - Every other PR still gets a full REST `pulls/N` read (L29380) — including the common `BLOCKED`, `BEHIND` and `UNSTABLE` states — even though only `mergeable_state == "dirty"` leads to any action (L29402-29404).
- Current: ceil(N/30) GraphQL calls plus 1 REST call per non-CLEAN open PR (up to `--limit 3000`, L29265). Proposed: the same GraphQL calls plus REST only for PRs whose batched state is `DIRTY` or `UNKNOWN`, or that are missing from the batch.
- Recommended fix:
  - Record the batched state in a new associative array next to `_STANDALONE_CLEAN_PRS` (L29278). Use a name such as `_STANDALONE_BATCH_MERGE_STATE` after checking it is unique.
  - Skip the REST read when the batch shows the same `headRefOid` and a state that is neither `DIRTY` nor `UNKNOWN`.
  - Keep the fresh REST read for DIRTY candidates; it supplies the `expected_head_sha` used at L29414-29416.

**API-005** — `scripts/gh_helpers.sh:1365-1398` (`curl_gh_api`); callers at `scripts/tg_helpers.sh:207, 214, 398, 467` — Severity: Medium — Category: `api-redundancy`
- Description: `curl_gh_api` treats every non-2xx, non-rate-limit response as transient. A 404, 401 or 422 is retried up to `GH_RETRY_MAX_ATTEMPTS` (default 5) times, with 1+2+4+8 seconds of sleep (L1389-1395). `gh_retry` short-circuits these through `_is_gh_permanent_failure` (L192-195, used at L1099). The Telegram cleanup paths use `curl_gh_api` to page through issue comments; a deleted issue or comment costs 5 calls and about 15 seconds each.
- Current: 5 calls per permanent error. Proposed: 1.
- Recommended fix: Inside the loop, return 1 immediately when `http_code` is 400, 401, 404 or 422 (mirroring `_is_gh_permanent_failure`). Keep retries for `000`, 5xx and rate-limit responses.

**API-006** — `.github/workflows/cancel_on_pr_close.yml:322-338` — Severity: Low — Category: `api-redundancy`
- Description: `gh_retry gh api -X POST ".../actions/runs/${run_id}/cancel"` gets a single attempt because `_gh_api_args_unsafe_post` (gh_helpers.sh:721-741) classifies every non-GraphQL POST as non-idempotent. Cancel is idempotent (it is a no-op or 409 once the run has completed), so one transient 5xx leaves the run uncancelled until the next scheduled sweep. There is no batch cancel endpoint, so the per-run loop itself is unavoidable.
- Current: 1 attempt per run, with no retry. Proposed: up to `GH_RETRY_MAX_ATTEMPTS` attempts using `gh_retry --idempotent gh api -X POST ...` (supported at gh_helpers.sh:1065).

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `.github/workflows/orchestrate_poll.yml:118-185` and `:492-561`; `cancel_on_pr_close.yml:25-94`; `implement.yml:438-494`, `:1280-1350`, `:6723-6757`; `plan.yml:343-410`; plus 9 more copies (16 in total across 10 workflows, matched by `support_ref_workflow_ref="$(jq -r '.workflow_ref`) — Severity: Medium — Category: `duplication`
- Description:
  - The block "Resolve workflow support ref → Checkout support source → fallback → Ensure → main snapshot" is copied almost verbatim 16 times.
  - In `orchestrate_poll.yml` it runs twice in the same job. The second copy appears to exist because "Checkout repository" (L398-402) runs between the two copies and clears the workspace contents. That is an inference [NEEDS VERIFICATION]: I believe `actions/checkout` removes the existing contents when the target directory isn't a matching clone.
  - Each poll tick therefore does two extra shallow clones and stages `gh_helpers.sh` twice (L187-201, L572).
- Recommended fix:
  - Within `orchestrate_poll.yml`: move "Checkout repository" ahead of L118. That is safe because no step after `find_tracking` exits the job early. Then delete the second quartet (L492-561).
  - Across workflows: add a commit-pinned composite action (for example `shubhodeep1/coding-workflows/.github/actions/resolve-support-ref@<sha>`). The repo already pins its own composite actions this way (`install-codex@91b30f6…`, L431). Inputs: `workflow_file`, `token`. Outputs: `script_ref`. Callers: the 10 workflows above.

**DUP-002** — `.github/workflows/orchestrate_poll.yml:1191-1378` vs `:1398-1512` — Severity: Medium — Category: `duplication`
- Description: "Process each tracking issue" and "Run standalone stall recovery without active projects" carry the same ~110-key `env:` block. Only `STANDALONE_RECOVERY_SWEEP_ONLY` (L1512) and some comments differ. Equality is enforced by `tests/test_orchestrate_poll_workflow_contract.py` (cited at L1388), but the duplication costs about 9–10 KB of workflow bytes and means every change has to be made twice.
- Recommended fix: Move the shared `vars.*`-only keys to the job-level `env:` (L80-84). Keep these at step level:
  - the secrets (`GH_TOKEN`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`), to limit their exposure;
  - `CLAUDE_POOL_REASON` (L1204, L1410), because job-level `env` cannot read the `steps` context;
  - `ALERT_MSG_LEVEL`, because it reads `env.ALERT_MSG_LEVEL`, which is set at runtime via `GITHUB_ENV` (L395).
  Then update the contract test to assert the reduced step-level sets match.

**DUP-003** — `scripts/label_helpers.sh:161-195`, `scripts/orchestrate_poll_process.sh:3995-4029+`, `scripts/validate_process.sh:1222-1256` — Severity: Medium — Category: `duplication`
- Description: There are three different `ensure_label_exists` implementations:
  - `label_helpers.sh` takes `(<name> [repo])`, reads colours from a hard-coded catalog, and has no cache.
  - The poller takes `(<name>)`, reads `.github/ai/label_contract.v1.json`, has special cases (`ai:closed`, `ai:ready-to-merge`), and caches in `_ENSURED_LABELS_CACHE`.
  - The validate copy reads the contract, has no cache, and sends a `tg_notify` WARNING on failure.

  So the same label can be created with a different colour or description depending on which path runs first.
- Recommended fix: Make `label_helpers.sh::ensure_label_exists <name> [repo]` the single owner. It should read `label_contract.v1.json` first, fall back to its catalog, and add the process-local cache. The other two become thin wrappers that preserve their current names and side effects, for example the validate copy's `tg_notify`.

**DUP-004** — `scripts/gh_helpers.sh:1898-1919`, `scripts/orchestrate_poll_process.sh:22138-22190`, `scripts/review_merge_train.sh:1137-1149` — Severity: Low — Category: `duplication`
- Description: There are three memoized default-branch helpers (`_autofix_review_default_branch_prime`, `_review_run_default_branch`, `_mt_resolve_default_branch`), each with its own validation regex and failure semantics. They sit alongside the 15 uncached sites in API-001.
- Recommended fix: Add `gh_default_branch <varname>` to `gh_helpers.sh` (shell-local memo plus a run-scoped memo file, matching L22151's subshell-safe pattern; fail-closed return 1). Re-implement the three helpers as wrappers and update the API-001 sites.

**DUP-005** — `scripts/orchestrate_poll_process.sh:9231-9279` vs `:28749-28799` — Severity: Low — Category: `duplication`
- Description: The judge "fix-up" loop and the `in_progress` "new issue" loop are near-identical: untrusted-entry guard, dedup via `issue_number_map`, label re-check, metadata footer, `ensure_label_exists` ×2, `gh issue create`. Only the type string differs (`judge-fix-up` vs `judge-addition`). L28759-28761 acknowledges it is the "Same guard as create_judge_fixup_issues_from_verdict".
- Recommended fix: Extract `create_judge_issues_from_verdict <type_label> <log_noun>` and call it from both sites. This also gives API-003 a single place to do its batching.

**DUP-006** — `.github/workflows/test-and-mark-stable.yml:774-914`, `:928-1203`, `:1235-1503`, `:1795-2526`, `:3397-3820` — Severity: Low — Category: `duplication`
- Description: Five phase wait loops each re-implement the same scaffold: `INACTIVITY_LIMIT`, `LAST_ACTIVITY`, `PREV_STATE`, `RATE_LIMIT_BACKOFF`, the state-change reset, and the `[ $IDLE -ge $INACTIVITY_LIMIT ]` timeout block. That scaffold is a large share of the body sizes in EXPR-001 and EXPR-002.
- Recommended fix: Add `wait_activity_tick <state_string>` and `wait_check_timeout <phase_label>` to `scripts/comprehensive_test_and_release_gh_api.sh`, which these steps already source (L951, L1270).

### Section 4: Expression Size Limit Risk Assessment

Method: character counts are estimates. I took each block's line count and per-line length distribution from search results and subtracted the 10-column YAML block indentation. No `wc -c` was run. Every row is [NEEDS VERIFICATION] until it is measured. Only `run:` bodies that contain at least one `${{ }}` are counted.

| ID | Block | Est. chars | % of 21,000 | Headroom | `${{ }}` in body | Risk |
|---|---|---|---|---|---|---|
| EXPR-001 | `test-and-mark-stable.yml:1243-1503` "Phase 3b: Wait for PR creation" | 17,500–19,500 | 83–93% | ~1,500–3,500 | 1 (L1281) | High |
| EXPR-002 | `test-and-mark-stable.yml:936-1203` "Phase 2: Wait for plan to complete" | 16,000–17,500 | 76–83% | ~3,500–5,000 | 3 (L954, L966, L984) | Medium |
| EXPR-003 | `implement.yml:5206-5433` "Handle no-op implementation" | 13,000–14,500 | 62–69% | ~6,500 | 7 (L5211, L5213, L5416, L5424, L5426, L5430, L5432) | Low |
| EXPR-004 | `orchestrate_poll.yml:566-837` "Stage workflow support files" | 12,800–14,000 | 61–67% | ~7,000 | 3 (L693, L694, L815) | Low |
| EXPR-005 | `plan.yml:2134-2334` "Guard plans targeting template-owned paths" | 12,500–14,000 | 60–67% | ~7,000 | 3 (L2327, L2329, L2331) | Low |

**EXPR-001** — `.github/workflows/test-and-mark-stable.yml:1243-1503` — Severity: High — Category: `expression-limit` [NEEDS VERIFICATION]
- Description: The body is about 260 dense lines with many jq filters of 150+ characters (for example L1305-1306, L1409-1412, L1372). It contains exactly one interpolation, `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` at L1281, and that one line makes the whole body count toward the 21,000-character limit.
- Recommended fix: Move it to the step's `env:` as `IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` and delete L1281. The body then contains no `${{ }}`. Phase 4 already uses this pattern ("Promoted to env so the run: script body contains no ${{ }}", L1825).

**EXPR-002** — `.github/workflows/test-and-mark-stable.yml:936-1203` — Severity: Medium — Category: `expression-limit` [NEEDS VERIFICATION]
- Description: Three inline `${{ steps.create-issue.outputs.created_after }}` references sit in helper functions (L954, L966, L984) of a comment-heavy body of about 267 lines.
- Recommended fix: Add `PLAN_CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step's `env:`. The name is not used anywhere else in this file; only `IMPL_CREATED_AFTER` exists. Replace the three references with `${PLAN_CREATED_AFTER}`.

**EXPR-003 / EXPR-004 / EXPR-005** — files and lines as in the table — Severity: Low — Category: `expression-limit`
- Description: Each body is pulled under the limit by only a few small references. They are growth risks: EXPR-003 and EXPR-005 carry long inline prose templates (for example the `printf '%s\n'` message bodies at implement.yml:5233-5240 and 5273-5287, and `REJECTION_BODY` at plan.yml:2330).
- Recommended fix:
  - Replace `${{ github.repository }}`, `${{ github.server_url }}` and `${{ github.run_id }}` with the runner's default `${GITHUB_REPOSITORY}`, `${GITHUB_SERVER_URL}` and `${GITHUB_RUN_ID}`.
  - Move `${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED || 'false' }}` and `${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED || 'false' }}` (orchestrate_poll.yml:693-694) into the step's `env:` and echo the env values.
  - This removes every interpolation from all three bodies, with no change in behaviour.

**EXPR-006** — `.github/workflows/test-and-mark-stable.yml` (whole file) — Severity: Medium — Category: `expression-limit` [NEEDS VERIFICATION]
- Description:
  - From the line-length distribution (7,187 non-empty lines; 3,665 of 60+ characters; 280 of 120+), I estimate the file at about 420,000–435,000 bytes.
  - That is close to `CI_BUDGET_WORKFLOW_WARN_BYTES` (440,000, agents.md:2351-2356) and within about 45–60 KB of the 480,000-byte guard (`tests/test_workflow_file_size_limit.py`, agents.md:2376-2379). GitHub's hard limit is 512,000 bytes (agents.md:2362).
  - No workflow is near the 800 KB / 1 MB thresholds in the prompt; the repo's real limit is 512,000 bytes. My other estimates are `implement.yml` about 394K and `review_autofix.yml` about 357K.
- Recommended fix: Run `wc -c .github/workflows/test-and-mark-stable.yml`. Then, following §24 and the agents.md split rule, move the largest bodies that already have no `${{ }}` into `scripts/`: "Phase 4" (L1795-2526), "Phase 4b" (L2596-3266) and "Phase 6" (L3397-3820). Keep each step's `name:`, `id:`, `if:` and `env:`.

`if:` conditions: the longest one sampled, `implement.yml:5199`, is about 330 characters. None approaches the limit.

### Section 5: Cross-Cutting Concerns

TODO/FIXME/HACK markers: none in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`. A comment-scoped `\b(TODO|FIXME|HACK)\b` search returned no matches; the raw-substring hits were `mktemp` `XXXXXX` templates.

**DEAD-001** — `.github/workflows/test-and-mark-stable.yml:789/861`, `:943/1056`, `:1257/1387`, `:1877/2140`, `:3556/3706` — Severity: Low — Category: `dead-code`
- Description: `RATE_LIMIT_BACKOFF` is only ever set to `0`; the one other match, L2274, is a comment. Every `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && ...` guard is unreachable. If it were ever enabled, the L1056-1058 and L861 versions would busy-loop against the API, because they `continue` without a `sleep` (the L1387-1389 version does sleep).
- Recommended fix: Remove the variable and the five guards, since rate limits already back off inside `gh_api_safe` (comment at L1006-1007). If kept, implement it in the shared helper from DUP-006, with a mandatory sleep.

**CONSIST-001** — `.github/workflows/implement.yml:5302-5316`; also `test-and-mark-stable.yml:7582`, `update_workflows.yml:1754`, `validation-refresh.yml:353` — Severity: Low — Category: `consistency`
- Description: Seven raw `curl .../bot${TG_BOT_SECRET}/sendMessage` sites exist across six workflows. Some are deliberate fallbacks (`update_workflows.yml:1721-1723`, `issue_pr_status.yml:822-824`, `check_failure_triage.yml:693-697`). The implement pathspec alert only checks that the secret and chat ID are set and never consults `ALERT_MSG_LEVEL`, so it ignores `SILENT` and level filtering. `validation-refresh.yml:345-350` re-implements that filtering by hand.
- Recommended fix: Use `tg_send_msg "${TG_MSG}" "CRITICAL"` when `tg_helpers.sh` is present. Keep the raw curl only as an `else` fallback that applies the same SILENT check as `validation-refresh.yml:345-350`.

**CONSIST-002** — `scripts/review_collect_pr_metadata.sh:35`, `:69-74` — Severity: Low — Category: `consistency`
- Description: The script sources `gh_helpers.sh` (L35) and then redefines `gh_retry` with an incompatible signature (`gh_retry <outfile> <gh args…>`). Any `gh_helpers.sh` function that calls `gh_retry gh api …` internally — for example `gh_pr_with_all_comments` (L1440-1447) or `gh_review_pr_state` (L91, L107) — would treat the literal `gh` as its output file if it were called from this script. It isn't called today, so this is latent, but it is the identifier collision §10 forbids.
- Recommended fix: Rename the local wrapper to a unique name (for example `_rcpm_gh_to_file`) and update its 7 call sites (L138, L194, L231, L235, L240, L247, L280).

**CONSIST-003** — `.github/workflows/implement.yml:2166, 2392, 2411-2412, 2434`; `test-and-mark-stable.yml:1117, 1472`; `plan.yml:667, 1419`; `orchestrate_poll.yml:1955-1958` — Severity: Low — Category: `consistency`
- Description:
  - These sites use raw `gh api` or `curl -H "Authorization: token …"` in code paths where the same file uses `gh_retry` / `gh_api_safe_print` for the same endpoints. For example, `test-and-mark-stable.yml:1117` re-reads the issue labels with raw `gh api` one line after the `gh_api_safe_print` read at L1052.
  - As a result they get no rate-limit handling and no `GH_API_RETRY` telemetry.
- Recommended fix: Wrap each in the wrapper the step already uses: `gh_retry` (or `gh_retry_to_file` for `--paginate`) in implement and plan, and `gh_api_safe_print` in test-and-mark-stable. For the failure-alert step, use `gh_retry _safe_gh_jq ".../attempts/${GITHUB_RUN_ATTEMPT}/jobs"`.

**CONSIST-004** — `.github/workflows/orchestrate_poll.yml:437-442` vs `:304-307`, `:1004-1012` — Severity: Low — Category: `consistency`
- Description: The OpenCode install is gated on `vars.ENABLE_STANDALONE_STALL_RECOVERY != 'false' && vars.ENABLE_STALL_JUDGE != 'false'`. The steps that actually consume those flags normalise them case-insensitively (`1|true|yes|on`). A value like `False`, `off` or `0` disables recovery but still installs OpenCode on every idle tick.
- Recommended fix: Use `steps.find_tracking.outputs.has_work == 'true' || steps.find_tracking.outputs.standalone_recovery == 'true'` (as at L416), with the same normalised check for `ENABLE_STALL_JUDGE`.

**CONSIST-005** — `scripts/orchestrate_poll_process.sh:14743, 22978, 23150, 23441, 23488, 23533, 23939, 24009, 28164, 28509, 29271` — Severity: Medium — Category: `consistency` [NEEDS VERIFICATION]
- Description: On a failed lookup these sites fall back to `|| echo "main"`. `review_merge_train.sh:1133-1134` ("no fallback, so a failed read is never mistaken for 'main'") and `_review_run_default_branch` (L22125-22126) deliberately fail closed for the same value. In a consumer repo whose default branch isn't `main`, a transient failure can steer default-branch logic to the wrong base; L14743 feeds `finalize_integration_merge_if_needed` (L14769). I did not check which downstream calls act on the fallback value.
- Recommended fix: When doing API-001, have merge/finalize paths (L14743, L23533, L28509) defer the tick on failure rather than use `main`. Keep the fallback only for read-only and display uses.

**SHELL-001** — `.github/workflows/test-and-mark-stable.yml:902, 1194, 1494, 2466, 3806` — Severity: Low — Category: `shellcheck`
- Description: `if [ $IDLE -ge $INACTIVITY_LIMIT ]` triggers SC2086. Both values are computed integers, so this is harmless today, but it is inconsistent with the quoted tests elsewhere in the same steps (for example L1446).
- Recommended fix: Quote both operands, or fold this into the DUP-006 helper.

**SHELL-002** — `scripts/orchestrate_poll_process.sh:14349, 14354, 14629` (with `set -euo pipefail` at L21) — Severity: Low — Category: `shellcheck` [NEEDS VERIFICATION]
- Description: `printf '%s' "${reason}" | grep -q…` under `pipefail`. If `reason` (validation-failure text) exceeds the 64 KiB pipe buffer and `grep -q` exits on an early match, `printf` gets SIGPIPE and the pipeline returns 141, so a matched `harness_error` or `deterministic_*` class reads as "no match". `gh_helpers.sh:753-754` already avoids pipelines for this reason. I did not check how large `reason` can get.
- Recommended fix: Use `grep -qE '…' <<< "${reason}"`, or `[[ ${reason} =~ … ]]`.

**SHELL-003** — `scripts/orchestrate_poll_process.sh:9234`, `:28754` — Severity: Low — Category: `shellcheck`
- Description: `echo "${JUDGE_JSON}" | jq -c … | while read -r …` runs the loop in a pipeline subshell. `_ENSURED_LABELS_CACHE` entries added by `ensure_label_exists` (L4001) inside the loop are lost when it ends, so later calls in the tick repeat `gh label create`. `read -r` without `IFS=` also trims surrounding whitespace.
- Recommended fix: `while IFS= read -r fix_issue; do …; done < <(printf '%s' "${JUDGE_JSON}" | jq -c '…')`. This is the process-substitution form already used at L5774-5819.

**SHELL-004** — `.github/workflows/validation-refresh.yml:353-356` — Severity: Low — Category: `shellcheck`
- Description: `curl -sS -X POST …` under `set -euo pipefail`, without `-f` and without `|| …`. An HTTP 4xx from Telegram is silently treated as success, and a network error fails the (already-failure) notification step.
- Recommended fix: `curl -fsS … >/dev/null || echo "::warning::Telegram send failed"`, the same form as `test-and-mark-stable.yml:7582-7585`.

**DEBT-001** — `.github/workflows/plan.yml:2219-2258` — Severity: Medium — Category: `tech-debt`
- Description: The template-path guard finds implement's fetched-helper manifest by text-parsing `implement.yml`. It looks for the literal step header `"      - name: Stage workflow support files\n"` (L2226) and for `for f in …; do` loops containing `_fetched_scripts+=("${f}")` (L2231-2238). If that step body moves into `scripts/` under the §24 split rule, or the loop form changes, the parse fails and the guard falls back to rejecting every `scripts/` path (L2253-2257). Plans that touch legitimate consumer scripts would then be blocked.
- Recommended fix: Generate the helper list once into a checked-in manifest (for example a `scripts/` text file listing runtime-fetched helpers). Have both implement's staging loop and this guard read it, and add a contract test that the two stay aligned.

**DEBT-002** — `scripts/orchestrate_poll_process.sh:12-19` — Severity: Low — Category: `tech-debt`
- Description: The header says the poller is "~27,700-line", but the file has at least 29,271 lines (line 29271 was read) and 304 top-level function definitions. ShellCheck's extended analysis is switched off for it (L12) because it timed out in CI. Its size is the main reason the poll log loses ~822K characters in the middle (see the existing report's "Poll runs are a telemetry blind spot").
- Recommended fix: Split the self-contained sweeps into sourced modules, starting with the standalone PR conflict and no-op sweeps from L29255 onward. Follow the existing optional-module pattern (`orchestrate_stall_retired_conflict.sh`, staged at orchestrate_poll.yml:606-619) and add each module to the "Stage workflow support files" list.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 12 | BUG-001, BUG-002, API-001, API-004, API-005, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-006, CONSIST-005, DEBT-001 |
| Low | 23 | BUG-003, BUG-004, SEC-001, API-002, API-003, API-006, BATCH-001, DUP-004, DUP-005, DUP-006, EXPR-003, EXPR-004, EXPR-005, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, SHELL-003, SHELL-004, DEBT-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/test-and-mark-stable.yml` (EXPR-001: one `env:` move) | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `.github/workflows/cancel_on_pr_close.yml` | Medium |
| Code modularization | 10 workflows carrying the support-ref block, a new composite action under `.github/actions/`, `.github/workflows/orchestrate_poll.yml`, `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_merge_train.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `tests/test_orchestrate_poll_workflow_contract.py` | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/plan.yml`, new `scripts/` step bodies for EXPR-006 | Medium |
| Medium/Low fixes | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`, `.github/workflows/plan.yml`, `.github/workflows/validation-refresh.yml`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/update_workflows.yml`, `scripts/memory_helpers.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/orchestrate_poll_process.sh` | Medium |