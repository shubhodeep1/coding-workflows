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