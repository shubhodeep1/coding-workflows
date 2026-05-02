## Executive Summary

- **The highest-latency bottleneck is the long-polling E2E stable test path, not model runtime.** `test_and_mark_stable` averages 3,276s, had 0/5 successful runs in this window, and run `25237291900` failed after `5,465s` while the alt-model path sat in `ai:implementing` for ~70+ minutes with repeated 20s polls. **Estimated impact:** 30–70 minutes saved per affected E2E run. **Confidence:** high.

- **`implement` reliability is dominated by “no actionable output” Codex bailouts, not hard validation failures.** Ten `implement` failures occurred; run `25246727158` aborted after two retries with `Codex produced no actionable output 2 attempts in a row`, while companion summaries reported token burn of `4,466` then `14,134` before bailout. **Estimated impact:** cut implement-family failure rate materially from current `5.43%`, plus ~18.6k tokens saved per avoided bailout. **Confidence:** high.

- **GH API pressure is concentrated in a few workflows and is already brushing rate limits.** Deep-dive runs show `review_autofix` run `25237552686` with ~`259` `gh api` log lines plus `HTTP 429`/secondary-rate-limit handling, and `workflow_log_analysis` run `25246056978` with ~`204` `gh api` log lines and `18` secondary-rate-limit mentions. **Estimated impact:** 20–50% API-call reduction on hot paths and lower rate-limit/cancellation risk. **Confidence:** high.

- **Review/autofix is the biggest AI-cost center per successful run.** `review_autofix` p95 is `1,457s`; run `25237552686` ran a two-pass review flow, used six external reviewer models plus editor/model summarization, and the consolidator alone logged `58,177` tokens on a trivial smoke-test PR. **Estimated impact:** substantial token and wall-time reduction on small/simple PRs without behavior changes. **Confidence:** medium-high.

- **Runner queueing is a recurring but secondary drag across successful runs.** CI runs repeatedly sit around `605–615s`; sampled summaries explicitly call out hosted-runner wait in `ci`, `clarify`, `plan`, `implement`, and `review_autofix`. Example: implement run `25246733470` spent ~`163s` waiting for a runner before doing mostly gate checks. **Estimated impact:** 1–3 minutes saved on many active runs if unnecessary dispatches are suppressed early. **Confidence:** medium.

- **AI memory retrieval is generally healthy, but reviewer memory usefulness is weak.** Across deep-dive telemetry, `retrieve` hit rate was `87.5%` (`21/24`), average `estimated_tokens` was `42`, and keywording was mostly `plain` (`21/24`), but all three zero-hit retrieves were reviewer/analysis-side rather than implementation-side. **Estimated impact:** modest token/quality gains by tightening reviewer memory criteria. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Replace long issue-label polling in `test_and_mark_stable` with run-ID-driven phase waits plus bounded stale-label escape hatches**
   - **Evidence:** Run `25237291900` failed after `5,465s` at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`. Its log shows repeated 20s polling and the issue staying `ai:implementing` for well over an hour. Run `25246048096` still logged `130` `gh api` lines across `32` files while waiting through phase steps like `Phase 1 Wait for clarify`, `Phase 2 Wait for plan`, `Phase 3b Wait for PR creation`, and `Phase 4 Wait for review_autofix`.
   - **Root cause:** E2E orchestration watches issue labels/comments on a fixed cadence even after concrete phase run IDs are known, so stuck label state dominates total runtime.
   - **Exact change:** After phase discovery, pivot waits to the actual run IDs for clarify/plan/implement/review where possible; add a bounded “stale label” guard that fails fast when `ai:implementing` persists without branch/PR/job progress for N polls.
   - **Estimated time savings:** 30–70 minutes on stuck E2E runs; 5–15 minutes on slow-but-successful ones.
   - **Implementation risk:** low-medium. This is a control-flow tightening, not a behavior change to production workflows.

2. **Fail `implement` earlier on exploration loops instead of paying for two full high-reasoning attempts**
   - **Evidence:** Run `25246727158` ended with `Codex produced no actionable output 2 attempts in a row`. Companion summaries for the same failure chain reported attempt token usage of `4,466` and `14,134`. The same failure signature appears across the error set (`25237418726`, `25237690797`, `25237704374`, `25243564804`, `25243569299`, `25244121942`, `25244127789`, `25245077011`, `25245085089`, `25246727158`).
   - **Root cause:** Retry logic allows a second expensive attempt even when the first attempt already shows “announced edit without changes” / empty-output exploration behavior.
   - **Exact change:** Introduce an early retry downgrade path: after first no-op attempt, switch to a short “surgical retry” prompt with capped context and lower reasoning effort; abort before a second full-context xhigh attempt if no changed files or no meaningful tool output is observed.
   - **Estimated time savings:** ~1.5–3 minutes per failed implement run; more importantly reduces repeated failed cycles.
   - **Implementation risk:** medium. Needs careful guardrails to avoid cutting off recoverable first-attempt misses.

3. **Short-circuit trivial/single-file review PRs before the full two-pass reviewer panel**
   - **Evidence:** `review_autofix` has p50 `41s` but p95 `1,457s`. Run `25237552686` took `2,938s` and executed `PASS 1: Broad sweep` then `PASS 2: Deep review`, despite converging on one trivial canary-file issue. Run `25246765078` shows `ENABLE_REVIEWER_TWO_PASS: true`, six reviewer models, and `REVIEWER_REASONING_EFFORT: xhigh`.
   - **Root cause:** Heavy review topology is applied even when diffs are tiny and reviewer consensus is obvious.
   - **Exact change:** Add a deterministic “small diff / exact-file-class” shortcut before pass 2: if pass 1 consensus is high and diff scope is below strict thresholds, skip deep review and go directly to consolidator/editor or comment-only result.
   - **Estimated time savings:** 5–20 minutes per trivial review_autofix run.
   - **Implementation risk:** medium. Must preserve safety for risky one-file changes.

4. **Suppress dispatch of downstream clarify/plan/respond workflows earlier to avoid queueing overhead on obvious false conditions**
   - **Evidence:** Of 1,000 total runs, `682` are in “other” states, mostly instant skips; many recent runs are 0–2s skipped children. Even when fast, they still contribute scheduling churn. Sampled summaries repeatedly show `plan.if`, `clarify.if`, `implement.if`, and `respond.if` evaluating false immediately.
   - **Root cause:** Fan-out dispatch happens before enough preconditions are known, so the platform still has to create and evaluate numerous child runs.
   - **Exact change:** Move command/body/phase gating up into the parent/orchestrator dispatch decision so obviously ineligible child workflows are not triggered at all.
   - **Estimated time savings:** small per run, but 30–120s aggregate queue-pressure reduction during bursty issue activity.
   - **Implementation risk:** low.

5. **Reduce CI runner waste on cache/dependency scans**
   - **Evidence:** CI p50 is `613.5s`, p95 `644.9s`. Sampled CI runs `25246765065` and `25246802550` both finished around `605–615s` and note runner waiting before start. Recent plan logs also show broad dependency glob scans for `**/*requirements*.txt`, `**/pyproject.toml`, `**/uv.lock`, `**/*.py.lock`.
   - **Root cause:** Even with cache hits, broad dependency scanning and full lint/test matrix execution are constant-cost.
   - **Exact change:** Narrow cache-dependency globs to repo-actual paths and split ultra-stable validation jobs from change-sensitive ones when path filters permit.
   - **Estimated time savings:** 30–90s on CI/plan jobs.
   - **Implementation risk:** low.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Reduce review/autofix model breadth and second-pass depth for low-complexity PRs**
   - **Evidence:** Run `25246765078` used six external reviewer models plus `MODEL_EDITOR: openai/gpt-5.3-codex` and `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`, all with high reasoning settings. In run `25237552686`, the consolidator alone logged `58,177` tokens on a trivial canary-file issue.
   - **Root cause:** Full reviewer ensemble and two-pass review are applied broadly rather than selectively.
   - **Exact change:** For tiny diffs and deterministic file classes, reduce pass-1 reviewer count, keep only highest-signal models, and skip pass 2 unless disagreement or high-risk file patterns appear.
   - **Estimated savings:** largest per-run token/dollar reduction in the window; likely 30–60% lower review tokens on trivial PRs.
   - **Quality-risk notes:** medium. Keep full ensemble for security-sensitive files, workflow files, orchestrator scripts, and multi-file diffs.

2. **Downgrade second implement retry reasoning/context after a no-op first attempt**
   - **Evidence:** The failed implement chain around run `25246727158` spent `18,600` tokens across two attempts and still bailed. Environment shows `MODEL_EDITOR: openai/gpt-5.3-codex` with `MODEL_REASONING_EFFORT: xhigh`.
   - **Root cause:** Second attempt repeats a large, high-reasoning context even when first-attempt telemetry already indicates stuck exploration.
   - **Exact change:** On no-op retry, switch from full xhigh context to a constrained “diff-only + target file + last failure summary” prompt, ideally at lower reasoning.
   - **Estimated savings:** ~25–60% token reduction on this failure class; ~18k tokens saved per fully avoided duplicate bailout.
   - **Quality-risk notes:** low-medium if restricted to the specific no-change/no-op signature.

3. **Stabilize prompt prefixes to improve prompt-cache reuse**
   - **Evidence:** OpenRouter prompt cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) in sampled runs, but observed cache probe lines in `review_autofix` report `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`, so effectiveness is not measurable. Deep-dive implement/review logs repeatedly reprint long static Serena/API-hygiene instruction blocks on each retry/pass.
   - **Root cause:** Large static instructions are likely mixed with dynamic run-specific noise, fragmenting cacheability and forcing repeated transmission.
   - **Exact change:** Move dynamic metadata (issue IDs, run IDs, timestamps, branch names, one-off warnings) after a stable static prefix; deduplicate repeated Serena/API-hygiene blocks across retries.
   - **Estimated savings:** medium but broad; likely noticeable latency and prompt-token reductions on implement/review/analysis jobs.
   - **Quality-risk notes:** low. This is formatting, not content removal.

4. **Stop paying AI-analysis cost for unselected-run summaries when the report already has enough signal**
   - **Evidence:** `workflow_log_analysis` run `25246056978` emitted `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` with `summarized: 68`, `targeted: 100`, `tokens_used: 153540`.
   - **Root cause:** The analysis pipeline spends substantial token budget widening coverage even when the deep-dive set already identifies dominant bottlenecks.
   - **Exact change:** Gate unselected-run summarization by novelty budget: skip or cap when recent windows are homogeneous, or summarize only families lacking deep-dive evidence.
   - **Estimated savings:** very high on workflow-log-analysis runs; up to ~150k tokens per report cycle in similar windows.
   - **Quality-risk notes:** medium. Keep the feature, but make it adaptive rather than always broad.

5. **Avoid duplicate AI work on skip-path noise**
   - **Evidence:** Some companion recent run summaries around the failed implement chain surface token usage and MCP startup signals even when the child workflow itself was ultimately skipped. At minimum, this creates analysis noise; at worst, it indicates context leakage or duplicated logging work.
   - **Root cause:** Skip-path orchestration and summary generation are not cleanly separated from neighboring failed attempt telemetry.
   - **Exact change:** Ensure skip-path jobs never initialize AI/MCP setup and isolate summary generation to the active run only.
   - **Estimated savings:** small-to-medium, plus cleaner observability.
   - **Quality-risk notes:** low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Add a deterministic “stuck exploration” fail-open path from `implement` back to clarify or narrow-plan**
   - **Failure evidence:** Ten `implement` failures in the window; run `25246727158` failed specifically at `Run Codex implementation` with `2 consecutive attempts with no actionable output`.
   - **Root cause category:** agent/orchestrator recovery gap.
   - **Exact fix:** When no-op exploration is detected, post a machine-readable failure reason that routes the issue back to clarify or requests a narrower implementation slice instead of retrying identical full-auto execution.
   - **Expected reliability impact:** biggest reduction in implement-family reruns and false-starts.
   - **Rollback/fail-open considerations:** fail open to current behavior behind a feature flag; only enable reroute on the exact no-change signature.

2. **Break `ai:implementing` deadlocks with heartbeat-based timeout logic**
   - **Failure evidence:** `test_and_mark_stable` run `25237291900` waited until failure while labels remained `ai:implementing`; family stats show `1` failure, `4` cancellations, `0` successes out of `5`.
   - **Root cause category:** stale phase-state / polling deadlock.
   - **Exact fix:** Record a lightweight implement heartbeat or last-progress timestamp; if label state doesn’t advance and no PR/check/run movement is observed for a bounded interval, mark the phase stale and fail the E2E scenario early with diagnostics.
   - **Expected reliability impact:** high for stable-test workflows; avoids indefinite waiting and ambiguous cancellations.
   - **Rollback/fail-open considerations:** conservative threshold and smoke/E2E-only rollout first.

3. **Fix nightly validation self-test fixture instability before treating it as gating health**
   - **Failure evidence:** `nightly_validation_selftest` had `1/1` failures; run `25242537588` reported `fixtures=3 passed=1 failed=2`.
   - **Root cause category:** test-fixture or harness drift.
   - **Exact fix:** Split the self-test summary artifact into per-fixture outcomes and quarantine the two failing fixtures until green again; keep the runner job green only if failures are known and non-regressive.
   - **Expected reliability impact:** removes a permanent red signal and makes regressions actionable.
   - **Rollback/fail-open considerations:** fail-open only for known-bad fixtures with explicit allowlist and expiration.

4. **Reduce review/autofix rate-limit-related instability**
   - **Failure evidence:** `review_autofix` run `25237552686` logged `HTTP 429` and secondary-rate-limit handling while making ~259 API log-line hits.
   - **Root cause category:** external API saturation.
   - **Exact fix:** batch reusable PR/issue/check-run reads, reuse fetched metadata across reviewer/editor/consolidator steps, and avoid `/rate_limit` probes except on retryable 403/429 responses.
   - **Expected reliability impact:** medium-high; fewer transient review cancellations/timeouts.
   - **Rollback/fail-open considerations:** preserve current single-call fallbacks on cache miss or parse failure.

5. **Persist intermediate issue/job metadata to remove race-prone re-fetches**
   - **Failure evidence:** In `implement` run `25246727158`, precheck fetched issue state/labels, later steps re-fetched issue metadata/comments, and deep-audit output proposes seeding later steps from earlier results.
   - **Root cause category:** duplicated state fetches and race windows.
   - **Exact fix:** Persist full issue JSON from precheck plus failed-step/job snapshots into `${RUNNER_TEMP}`/`${RUNTIME_DIR}` and read them downstream before any fresh API call.
   - **Expected reliability impact:** medium; fewer races during issue closure/failure diagnosis.
   - **Rollback/fail-open considerations:** keep live API fallback when cache file missing or invalid.

## AI Memory Health

- **Telemetry coverage:** observed. Across deep-dive logs there were `127` structured `AI_MEMORY_TELEMETRY` entries.
- **Retrieve hit rate:** `21/24` retrieves selected at least one record = **87.5%**.
- **Average estimated tokens:** **42** tokens per retrieve on average, max `56`.  
  - **Budget comparison:** not available in sampled telemetry; no explicit retrieve budget field was observed.
- **Keyword method distribution:**
  - `plain`: `21`
  - `none`: `3`
  - `llm`: `0` observed in sampled deep-dive runs

### Notable findings

- **Implementation memory is working well.**
  - Example: run `25246727158` logged `AI_MEMORY_TELEMETRY: {"op":"retrieve","role":"implementation","records_selected":2,"estimated_tokens":56,"keyword_method":"plain"}`.
  - Similar successful implementation retrieves appear in other failed implement runs such as `25245077011`.

- **Reviewer memory usefulness is weak.**
  - All zero-hit retrieves were reviewer-side:
    - `review_autofix` run `25237552686`
    - `workflow_log_analysis` run `25246056978` (twice)
  - Example: `25237552686` logged `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: "none"`.

- **No silent-disable pattern was observed.**
  - `enabled: false` entries: **0**
  - `fail_open: true` entries in structured telemetry: **0**

- **Push retries are present but not severe.**
  - Entries with `push_attempts > 1`: **4**
  - Maximum observed `push_attempts`: **2**
  - That is acceptable, but worth watching for write-path degradation.

### Recommendations

1. **Tune reviewer/analysis memory retrieval keywords separately from implementation retrieval**
   - Implementation retrieval is already effective; reviewer retrieval should bias toward PR-specific entities, file paths, and recent fix patterns instead of generic `none` retrievals.

2. **Emit explicit retrieve budget fields**
   - Current telemetry includes `estimated_tokens` but not target budget, which prevents proper “tokens vs budget” analysis.

3. **Track zero-hit retrieve rate by workflow family**
   - Target alert: reviewer or analysis zero-hit rate >20% over a rolling window.

## GH API Call Audit

### Highest-volume patterns

1. **`review_autofix` hot path**
   - **Evidence:** Slow run `25237552686` showed ~`259` `gh api` log-line hits across two step logs and also encountered `HTTP 429` and secondary-rate-limit logic.
   - **Pattern:** repeated PR state, linked issue, check-run, and rate-limit probing across review phases.

2. **`workflow_log_analysis` hot path**
   - **Evidence:** Slow run `25246056978` showed ~`204` `gh api` log-line hits across six files; run `25245013179` had similar volume plus `HTTP 429`; run `25237305050` logged `12` HTTP 429 mentions and `31` secondary-rate-limit mentions.
   - **Pattern:** multi-pass analysis jobs re-fetch run/report data and perform rate-limit-sensitive scans.

3. **`test_and_mark_stable` orchestration loops**
   - **Evidence:** Run `25246048096` showed ~`130` `gh api` hits across `32` files. Run `25237291900` repeatedly polled issue labels and searched workflow runs for `clarify`, `plan`, `implement`, and `review_autofix`.
   - **Pattern:** unbatched polling loops around issue state and workflow-run discovery.

4. **`implement` duplicate issue metadata fetches**
   - **Evidence:** Run `25246727158` performs:
     - precheck issue lookup (`repos/.../issues/${ISSUE_NUMBER}`)
     - issue metadata fetch
     - paginated issue comments fetch
     - post-failure job/issue fetches
   - **Pattern:** same issue scope fetched multiple times across steps.

### Concrete batching/reuse changes

1. **Seed downstream implement steps from the precheck issue payload**
   - **Current evidence:** `Precheck approval phase label` already fetches state+labels; later steps fetch full issue metadata again.
   - **Change:** persist the full issue JSON once, then hydrate downstream steps from disk first.
   - **Estimated reduction:** 1–2 REST calls per implement run; larger savings under retries.
   - **Rate-limit benefit:** low-medium, but high-confidence and low-risk.

2. **Batch linked-issue and PR metadata in review/autofix**
   - **Current evidence:** deep-audit proposals inside `workflow_log_analysis` explicitly call out duplicated linked-issue/body fetches and recommend caching managed/tracking classification.
   - **Change:** extend early GraphQL fetches to carry title/body/labels needed by later judge/alert steps, then reuse cached results.
   - **Estimated reduction:** 20–40% fewer review-path API calls on healthy runs.
   - **Rate-limit benefit:** high.

3. **Stop unconditional `/rate_limit` probing**
   - **Current evidence:** rate-limit helpers are present, and analysis notes recommend probing only on retryable 403/429 paths.
   - **Change:** read `/rate_limit` only after actual rate-limit errors, not proactively on every path.
   - **Estimated reduction:** 5–10% fewer API calls on hot loops.
   - **Rate-limit benefit:** medium.

4. **Replace per-poll REST workflow-run discovery with cycle-local cached run sets**
   - **Current evidence:** `test_and_mark_stable` repeatedly calls workflow-run endpoints and issue endpoints during phase waits.
   - **Change:** fetch recent run sets once per poll cycle, store locally, then evaluate clarify/plan/implement/review transitions from the cached payload.
   - **Estimated reduction:** dozens of calls per long E2E run.
   - **Rate-limit benefit:** high.

5. **Persist failed-step/job snapshots for post-Codex diagnosis**
   - **Current evidence:** deep-audit recommendations in `workflow_log_analysis` propose caching `FAILED_STEP_NAME` / jobs JSON for reuse.
   - **Change:** write jobs JSON once during failure capture; downstream diagnosis reads cache first.
   - **Estimated reduction:** small per failure, but removes race-prone duplicate calls.
   - **Rate-limit benefit:** low-medium.

## MCP & Serena Efficiency

### Observed issues

- **Repeated Serena/MCP startup across retries**
  - Run `25246727158` started `context7`, `git`, and `serena`, activated the project, then did it again on the second attempt before failing.
  - The run also repeated long Serena instruction blocks between attempts.

- **High MCP churn in review/autofix**
  - Sampled stats show `review_autofix` run `25237552686` with `10` Serena startup events across the review flow.

- **Instruction duplication is large**
  - Implement and review logs repeatedly print the same Serena usage guide, API hygiene rules, and fallback notes. This increases token overhead and makes retries slower.

### Recommendations

1. **Reuse one Serena/MCP session per job across retries/passes**
   - Keep a single activated project/session alive within the job instead of reinitializing on each attempt.
   - Expected gain: 10–30s per retry-heavy run, plus lower token churn.

2. **De-duplicate Serena instruction injection**
   - Load the Serena/tooling instruction block once into a stable shared prompt prefix, then append only attempt-specific guidance.
   - Expected gain: lower prompt tokens and less cache fragmentation.

3. **Avoid starting optional MCP servers on skip or gate-only paths**
   - Recent skip-path summaries show MCP startup signals appearing near runs that ultimately do no useful work.
   - Only initialize Serena/context7 after all cheap branch/comment/label gates have passed.

4. **Prefer cached symbol/use reports between review passes**
   - Review/autofix multi-pass flows should persist symbol overviews/search results from pass 1 for pass 2/editor reuse when the tree hasn’t changed.
   - Expected gain: modest latency and token savings without correctness risk.

5. **Increase safe parallelism for read-only prep**
   - Repository checkout integrity checks, issue metadata hydration, and memory retrieval are separable from Serena startup; overlap them where possible.
   - Expected gain: small but safe critical-path compression.

## Prompt Cache & Memory System

### Prompt-cache behavior

- **Cache is enabled**, not disabled:
  - Sampled runs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **But prompt-cache observability is incomplete**
  - In `review_autofix` cache probe lines, `cache_creation_input_tokens` and `cache_read_input_tokens` were `na`, so real prompt-cache hit/miss effectiveness cannot be quantified from this sample.
- **Non-AI dependency caches are healthy**
  - `setup-uv` cache hits appear repeatedly in `plan`, `clarify`, `implement`, and `ci`.
- **Likely cache fragmentation**
  - Large static instruction blocks are re-emitted inside retry loops and multipass flows with dynamic run-specific noise interleaved.

### Memory retrieval effectiveness

- Strong for implementation; weak for reviewer/analysis, as covered above.
- No evidence of disabled memory or silent fail-open in structured telemetry.

### Recommendations

1. **Make the prompt prefix more stable**
   - Put static policy/tool instructions first, and move dynamic metadata to a short suffix.
   - Expected impact: better cache reuse, lower prompt tokens, slightly faster first-token latency.

2. **Instrument real prompt-cache reads/creates**
   - Require non-`na` `cache_creation_input_tokens` / `cache_read_input_tokens` in logs for cache-enabled runs.
   - Expected impact: better optimization loop rather than direct latency gain.

3. **Split reviewer and implement memory policies**
   - Implementation retrieval is already useful; reviewer retrieval should use a narrower candidate class and recency window.
   - Expected impact: fewer zero-hit retrieves and better signal density.

4. **Avoid retry-prefix drift**
   - Retry prompts should reference the same static prefix and only append a compact retry delta.
   - Expected impact: lower token cost on failure loops.

## Orchestrator Health

### Observed health signals

- **Normal behavior:** most clarify/plan/respond runs are fast no-ops because conditions correctly evaluate false.
- **Pain point 1:** `implement` failures can strand issues in `ai:implementing`, which then blocks E2E and companion workflows.
- **Pain point 2:** auto-approval/child-workflow fan-out creates many skipped follow-on runs, adding operational noise.
- **Pain point 3:** review flows still spend heavy effort on tiny diffs.
- **Pain point 4:** runner wait obscures whether time is spent queueing vs computing.

### Smallest safe mitigations

1. **Track and alert on phase dwell time**
   - Especially `ai:implementing` and `ai:planning`.
   - Use median and p95 dwell by phase.

2. **Promote “no actionable output” to a first-class orchestrator state**
   - Distinguish it from validation failure.
   - This enables safer reroute to clarify/narrow-plan.

3. **Suppress child-workflow dispatch when the parent already knows the answer**
   - Particularly for false `clarify.if` / `plan.if` / `respond.if` paths.

4. **Separate queue time from compute time in run summaries**
   - Teams need both to know whether to optimize workflows or dispatch behavior.

### Observable indicators to track

- `implement` no-actionable-output count per day
- `ai:implementing` dwell p50/p95
- runner-wait share of total duration
- per-run `gh api` count by workflow family
- reviewer zero-hit memory retrieve rate
- review_autofix second-pass skip rate

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Compute + polling overhead in `implement`/`review`/E2E loops**
   - The dominant bottleneck across the clarify → plan → implement → review chain is not clarify/plan runtime itself, but long implement/review waits and E2E polling around them.

2. **Queueing overhead**
   - Hosted-runner wait is repeatedly visible in `ci`, `clarify`, `plan`, `implement`, and `review_autofix`.
   - This is especially wasteful for runs that later skip or only perform gate checks.

3. **Retry overhead**
   - `implement` no-op retries and `review_autofix` rate-limit retries add minutes and tokens without advancing state.

4. **API saturation overhead**
   - Review and analysis jobs make enough GitHub calls to trigger rate-limit handling, which compounds latency.

5. **Merge/conflict/check-run overhead**
   - Present but secondary in sampled evidence.
   - `review_autofix` includes explicit check-run wait logic with long timeouts; however, sampled failures were more about rate limits/cancellation than merge conflicts.

### Bottleneck fixes

1. Replace issue polling with run-aware waits.
2. Downgrade or abort implement retries after first no-op attempt.
3. Short-circuit trivial review flows before full panel/two-pass execution.
4. Cache and batch GH API reads across workflow phases.
5. Stop dispatching obviously ineligible child workflows.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` and `workflow_log_analysis` are the longest families by far.
- `review_autofix` is the highest-cost successful AI path.
- CI is consistently ~10 minutes, mostly stable but not cheap.
- Many orchestrator child runs are intentional skips, which keeps p50 at `1s` but adds noise/queue churn.

**Top failure modes**
- `implement` no-actionable-output bailouts (`10` failures in family, `5.43%` family failure rate).
- `test_and_mark_stable` stalled/waiting behavior (`0` successes in `5` runs this window).
- `nightly_validation_selftest` fixture failures (`1/1` failed).

**Highest-cost drivers**
- `workflow_log_analysis` summarization (`153,540` tokens in one summarization pass).
- `review_autofix` two-pass multi-model review (`58,177` consolidator tokens in one sampled run, plus multiple reviewer models).
- duplicate high-reasoning retries in failed implement runs.

**Top 3 prioritized actions**
1. Replace stable-test polling with run-ID-based waits and stale-phase timeouts.
2. Add a first-class recovery path for implement no-op exploration failures.
3. Reduce review/autofix model breadth and pass count for tiny deterministic diffs.

## Metrics Appendix

### Repo-level overview

| Repository | Total Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 257 | 12 | 49 | 682 | 1.2% | 105.3 | 1.0 | 610.0 |

### Key workflow-family metrics

| Workflow Family | Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| implement | 184 | 16 | 10 | 6 | 152 | 5.43% | 28.8 | 1.0 | 205.9 |
| plan | 185 | 26 | 0 | 0 | 159 | 0.0% | 14.6 | 1.0 | 148.4 |
| clarify | 222 | 32 | 0 | 0 | 190 | 0.0% | 19.1 | 1.0 | 126.8 |
| review_autofix | 61 | 24 | 0 | 36 | 1 | 0.0% | 271.2 | 41.0 | 1457.0 |
| ci | 58 | 58 | 0 | 0 | 0 | 0.0% | 606.5 | 613.5 | 644.9 |
| test_and_mark_stable | 5 | 0 | 1 | 4 | 0 | 20.0% | 3276.4 | 3295.0 | 5097.8 |
| workflow_log_analysis | 5 | 2 | 0 | 3 | 0 | 0.0% | 3430.8 | 3320.0 | 4772.8 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.0% | 89.0 | 89.0 | 89.0 |

### Notable run samples

| Run ID | Workflow Family | Conclusion | Duration (s) | Key Signal |
|---|---|---|---:|---|
| 25237291900 | test_and_mark_stable | failure | 5465 | Alt-model wait step failed after very long polling |
| 25246727158 | implement | failure | 184 | Codex bailed after 2 no-actionable-output attempts |
| 25237552686 | review_autofix | success | 2938 | Two-pass multi-model review; 429/rate-limit handling; 58,177 consolidator tokens |
| 25246056978 | workflow_log_analysis | success | 3320 | 204 GH API log-line hits; secondary rate-limit pressure; 153,540 summary tokens |
| 25246802550 | ci | success | 615 | Stable ~10m CI; runner wait noted |
| 25242537588 | nightly_validation_selftest | failure | 89 | `fixtures=3 passed=1 failed=2` |

### AI memory metrics from deep-dive logs

| Metric | Value |
|---|---:|
| Structured telemetry entries | 127 |
| Retrieve operations | 24 |
| Retrieve hit rate | 87.5% |
| Avg estimated retrieve tokens | 42 |
| Max estimated retrieve tokens | 56 |
| Keyword method `plain` | 21 |
| Keyword method `none` | 3 |
| Keyword method `llm` | 0 observed |
| Zero-hit retrieves | 3 |
| `fail_open: true` entries | 0 |
| `enabled: false` entries | 0 |
| Entries with `push_attempts > 1` | 4 |
| Max `push_attempts` | 2 |

### Sample GH API hot spots

| Run ID | Workflow Family | Approx. `gh api` Log Hits | Files Involved | Rate-Limit Signals |
|---|---|---:|---:|---|
| 25237552686 | review_autofix | 259 | 2 | HTTP 429 + secondary-rate handling |
| 25246056978 | workflow_log_analysis | 204 | 6 | 18 secondary-rate mentions |
| 25245013179 | workflow_log_analysis | 204 | 4 | HTTP 429 + secondary-rate mentions |
| 25246048096 | test_and_mark_stable | 130 | 32 | none observed, but very high polling volume |
| 25246727158 | implement | 87 | 1+ | duplicated issue/comment fetches |
| 25237291900 | test_and_mark_stable | 87 | 6 | long polling / repeated workflow-run lookups |

### Cache metrics observed

| Cache Type | Observed Behavior | Notes |
|---|---|---|
| `setup-uv` / dependency cache | Frequent hits | Seen repeatedly in plan/clarify/implement/ci |
| Prompt cache | Enabled | `OPENROUTER_PROMPT_CACHE_DISABLED: false` |
| Prompt-cache read/create token metrics | Not usable | Observed as `na` in sampled review cache-probe logs |
| Review ledger cache | Hit in sampled review run | Very small cache object (~348 B) |

### Token observations

| Context | Observed Tokens |
|---|---:|
| Failed implement chain around `25246727158` | 4,466 + 14,134 before bailout |
| `review_autofix` consolidator in `25237552686` | 58,177 |
| `workflow_log_analysis` summarize-unselected-runs in `25246056978` | 153,540 |


## Deep Audit — Workflows & Scripts (2026-05-02)

### Section 1: Bug & Correctness Sweep

No additional material correctness defects were identified in the thin wrapper/internal `workflow_call` workflows beyond the findings below.

- **ID** — BUG-001  
  **File path** — `scripts/review_run_reviewers.sh:42-47,896-903,941-942`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The reviewer-close sentinel is stored at a global path, `/tmp/pr_closed_sentinel_${PR_NUMBER}`. The preflight path creates it on close detection, and each reviewer watchdog later aborts immediately if that file exists. Because the filename is keyed only by PR number and is never namespaced to `RUN_ID`/`RUN_ATTEMPT` or cleaned at job start, a stale sentinel can incorrectly short-circuit later runs on the same PR when `/tmp` persists across jobs or on self-hosted runners. This is a real cross-run state leak in otherwise stateless review logic. [NEEDS VERIFICATION]  
  **Recommended fix** — Move the sentinel into `${RUNNER_TEMP}` or another job-scoped directory and include `${GITHUB_RUN_ID}` in the filename. Also delete any job-local sentinel at script start before spawning reviewer watchdogs. Reuse the repo’s existing job-scoped temp pattern (`RUNTIME_DIR` / `${RUNNER_TEMP}` used throughout `clarify.yml`, `implement.yml`, and `orchestrate.yml`).

- **ID** — SEC-001  
  **File path** — `scripts/review_commit_changes.sh:448-455; scripts/review_conflict_resolve.sh:852-853; .github/workflows/issue_pr_status.yml:475-490,564-579`  
  **Severity** — High  
  **Category tag** — `security`  
  **Description** — Multiple paths persist `GH_PAT` inside authenticated remote URLs (`https://x-access-token:${GH_PAT}@...`) before push/clone operations. In the scripts this is done via `git remote set-url origin ...`; in `issue_pr_status.yml` it is embedded into `WF_REMOTE_URL` for helper-script clones. That leaves the token in process arguments and repository config/clone command strings, increasing exposure through command echo, debug output, crash dumps, or later `git remote -v` inspection. The two shell-script call sites also omit quoting around the interpolated variables, which ShellCheck flags.  
  **Recommended fix** — Replace URL-embedded credentials with the safer `http.extraHeader=Authorization: Basic ...` pattern already used in the integration-ref resolver blocks (`clarify.yml` / `implement.yml`) and in `orchestrate.yml`’s authenticated git setup. For pushes, prefer `git -c http.extraHeader=... push ...`; for clones/fetches, wrap `git` as the resolver blocks already do instead of rewriting `origin`.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `scripts/review_run_reviewers.sh:32-49,930-947`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The script does one PR-state preflight call (`GET /pulls/{pr}`) before fan-out, then each reviewer watchdog independently polls the same PR-state endpoint every ninth watchdog cycle. With the default six-reviewer panel, each ~90-second window costs 6 identical PR-state reads after the initial preflight. Current call count: `1 + (reviewer_count × poll_windows)`; with six reviewers, each poll window costs 6 calls. Proposed call count: `1 + poll_windows` by using one shared job-level PR-state watcher that writes a single sentinel/cache file for all reviewers. This is a classic per-iteration API call inside parallel loops.  
  **Recommended fix** — Add a single background PR-state watcher in `review_run_reviewers.sh` (or `gh_helpers.sh`) that updates a shared `${RUNNER_TEMP}` state file/sentinel, and have reviewer watchdogs only read that local file. Extend the repo’s existing cycle-local cache pattern from `scripts/orchestrate_poll_process.sh` rather than keeping one `gh api` loop per reviewer.

- **ID** — BATCH-001  
  **File path** — `scripts/orchestrate_poll_process.sh:6353-6359`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — Standalone-stall candidate discovery performs 7 separate `gh issue list --label ...` calls every poll cycle, one for each pipeline label (`ai:clarification`, `ai:planning`, `ai:awaiting-approval`, `ai:implementing`, `ai:done`, `ai:ready-to-merge`, `ai:review-blocked`). Current call count: 7 REST calls per sweep before candidate-detail hydration. Proposed call count: 1 batched query for the label sweep. This is exactly the kind of repeated N-call pattern CLAUDE.md §15 says to batch.  
  **Recommended fix** — Replace the 7-label loop with one aliased GraphQL helper or a single search query that unions the label predicates, then feed the deduped issue numbers into the existing `_fetch_candidate_issue_details_graphql` path. The closest existing batching pattern to extend is `_fetch_standalone_marker_issues_graphql` / `_fetch_candidate_issue_details_graphql` in the same file.

- **ID** — API-002  
  **File path** — `.github/workflows/issue_pr_status.yml:503-512`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The merged-alert step iterates `LINKED_ISSUE_NUMBERS` and issues one `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""'` call per linked issue just to determine whether any linked issue is orchestrator-managed. Current call count: `N` issue-body reads per merged PR, where `N` is the number of linked issues. Proposed call count: 1 batched issue-body fetch for all linked issues, then local evaluation.  
  **Recommended fix** — Add a small batched GraphQL helper in `gh_helpers.sh` that accepts issue numbers and returns `{number, body}` records, then reuse it here. Model it after `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`; this path only needs `body`, so it can be cheaper than the full candidate-details query.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/clarify.yml:47-120; .github/workflows/plan.yml:76-152; .github/workflows/implement.yml:223-297; .github/workflows/orchestrate_clarify_respond.yml:83-157; .github/workflows/validate.yml:67-141`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The “Resolve integration ref” shell block is near-identical in five reusable workflows: it validates `ISSUE_NUMBER`, stages a checkout of `shubhodeep1/coding-workflows`, constructs an authenticated git wrapper, tries `stable`/`main` fallback logic, then executes `scripts/resolve_integration_ref.sh`. This duplication is already large enough that any auth/fallback fix now requires five synchronized edits.  
  **Recommended fix** — Extract the wrapper logic into a shared executable, e.g. `scripts/run_integration_ref_resolver.sh <repo> <issue_number>`, with env `GH_TOKEN`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`. Callers to update: `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, and `validate.yml`. Keep `scripts/resolve_integration_ref.sh` as the inner resolver and make the new helper responsible only for authenticated staging/fallback.

- **ID** — DUP-002  
  **File path** — `.github/workflows/clarify.yml:211-277; .github/workflows/plan.yml:239-305; .github/workflows/implement.yml:383-542; .github/workflows/orchestrate.yml:312-408; .github/workflows/orchestrate_clarify_respond.yml:257-360; .github/workflows/orchestrate_poll.yml:266-363; .github/workflows/review_autofix.yml:848-1060`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The “Stage workflow support files” block is duplicated across the main AI workflows. Each version creates `scripts/`, copies a curated allowlist from `.codex-workflow-src` with `main` fallback, conditionally stages schemas/prompts/context files, and sometimes writes a `scripts/.gitignore`. The blocks are structurally >70% identical and are now one of the main maintenance hotspots in the workflow layer.  
  **Recommended fix** — Introduce a shared script such as `scripts/stage_workflow_support.sh <profile>` where `profile` selects the needed support set (`clarify`, `plan`, `implement`, `review`, `orchestrate`, `poll`). Signature should accept `SCRIPT_REF`, `wf_source`, and booleans for prompts/schema/context staging. Update the seven workflows above to call that script instead of inlining the copy/fallback logic.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Wait for review workflow on PR` `run:` block contains `${{ }}` interpolations and currently measures about **16,626 characters**. That is below GitHub Actions’ hard 21,000-character expression ceiling, but above the requested 15,000-character medium-risk threshold. Estimated headroom remaining: **4,374 characters**. This block already embeds a large polling helper (`gh_api_safe`), review-run discovery, job-status inspection, log scraping, PR/head/comment probes, and timeout reporting in one interpolated shell body, so future edits are likely to push it over the limit.  
  **Recommended fix** — Extract the entire wait loop into a dedicated script such as `scripts/wait_for_review_run.sh`, passing `PR_NUMBER`, `TEST_REPO`, `BAIT_SHA`, `POLL_INTERVAL`, and `REVIEW_TIMEOUT` via env. If a full extraction is too invasive, split live-log probing and timeout/reporting into a second step so the first `run:` block shrinks materially.

No workflow exceeds the 800 KB early-warning threshold; the largest audited workflow is `review_autofix.yml` at 268,569 bytes.

### Section 5: Cross-Cutting Concerns

- **ID** — DEAD-001  
  **File path** — `scripts/orchestrate_poll_process.sh:4770-4776`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is defined but has no callers in the repository. The surrounding write path uses `get_standalone_state_comment_id()` and cached `comments_json` directly instead. Keeping an unreferenced helper around this close to hot poller code increases audit surface without buying behavior.  
  **Recommended fix** — Remove the unused helper, or wire a real caller to it and delete the duplicate direct parsing path. If retained intentionally for future use, add a comment stating that it is reserved and why.

- **ID** — DEAD-002  
  **File path** — `scripts/review_issue_ledger.sh:10-15,866-917`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — Two pieces of dead scaffolding remain in the ledger parser: the shell `trim()` helper at lines 10-15 is not referenced by the shell logic, and `CURRENT_FLOOR` is declared/populated but never read after assignment. Repository search shows parsing work is handled by embedded `awk`-side `trim()` functions instead, and no later ledger transition logic consumes `CURRENT_FLOOR`.  
  **Recommended fix** — Delete the unused shell `trim()` helper and remove `CURRENT_FLOOR` unless a follow-on feature is about to consume it. If floor-category persistence is intended, thread it into the final-state emission so the variable is not write-only.

- **ID** — SHELL-001  
  **File path** — `scripts/self_heal_validation.sh:155-155`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — The self-heal Codex invocation uses `cat "${SELF_HEAL_PROMPT_FILE}" | codex exec ...`, which ShellCheck flags as SC2002. It is not a correctness failure today, but it adds an unnecessary process and makes stdin ownership less clear in an already failure-sensitive recovery path.  
  **Recommended fix** — Replace the pipeline with input redirection: `codex exec --model "${MODEL_EDITOR}" --full-auto < "${SELF_HEAL_PROMPT_FILE}" > "${SELF_HEAL_OUTPUT_FILE}" 2>> "${SELF_HEAL_LOG_FILE}"`. That preserves behavior while simplifying error attribution.

No `TODO`, `FIXME`, or `HACK` markers were found in the audited workflow/script set.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | SEC-001, BATCH-001 |
| Medium | 6 | BUG-001, API-001, API-002, DUP-001, DUP-002, EXPR-001 |
| Low | 3 | DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 4 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 7 | Large |
| Expression size reduction | 2 | Small |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-02)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap/deadness is statically clear enough that the implement stage can act directly without changing observable behavior. `NEEDS_VERIFICATION` means the optimization is plausible but not fully provable from static reading alone, so a human or follow-on verification pass must confirm freshness/error-handling semantics first. `RISKY_SKIP` means the overlap sits in retry/pagination/race-sensitive logic and must stay manual-only even if it looks redundant.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Duplicate child/tracking issue reads in `orchestrate_clarify_respond`
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:61-76` and `.github/workflows/orchestrate_clarify_respond.yml:418-429`
- **Current call count** — `4` calls when a tracking issue exists (`child issue` twice, `tracking issue` twice); `2` calls when no tracking issue exists
- **Proposed call count** — `2` calls when a tracking issue exists; `1` call when no tracking issue exists
- **Endpoint(s)** — `GET /repos/{repo}/issues/{ISSUE_NUMBER}`, `GET /repos/{repo}/issues/{TRACKING_NUM}`
- **Evidence**
  ```sh
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ```
  ```sh
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  The same child issue is fetched in both steps, and the same tracking issue is split into one title-only read and one body-only read.
- **Proposed fix** — Consolidate the reads by persisting the step-1 child issue JSON and one full tracking issue JSON (title + body) into `${RUNNER_TEMP}` or step outputs, then have `Fetch issue and tracking context` reuse those payloads. If keeping the current step structure, fetch the tracking issue once with both fields and reuse its title for the smoke-test alert suppression check.
- **Safety rationale** — This crosses workflow steps and also changes a non-retried preflight read into reused data for a later retried context-build step, so freshness and failure semantics are not fully provable from static reading alone.
- **Downstream signal** — Verify whether edits to the child issue body or tracking issue title/body between lines `61-76` and `418-429` must be visible to the model; if not, persist the JSON from the first step and reuse it in the second.

#### MERGE-002 — Clarify fetches overlapping issue-comment data twice when semantic cache is enabled
- **Safety tag** — `RISKY_SKIP`
- **File path and line ranges** — `.github/workflows/clarify.yml:375-390`
- **Current call count** — `2` comment reads per run when `SEMANTIC_CACHE_BACKEND != none`
- **Proposed call count** — `1` comment read per run when `SEMANTIC_CACHE_BACKEND != none`
- **Endpoint(s)** — `GET /repos/{repo}/issues/{ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=...`
- **Evidence**
  ```sh
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"
  ...
  if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
    if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
  ```
  The second call is a superset of the first call's data on semantic-cache-enabled runs.
- **Proposed fix** — If this path is ever manually optimized, replace the pair with one `gh api --paginate --slurp` call, materialize the full comment array once, derive `ISSUE_COMMENTS_FILE` with a local `.[0:50]` slice, and derive `THREAD_HISTORY_FILE` from the same cached payload.
- **Safety rationale** — One of the calls uses `--paginate`, which is an explicit `RISKY_SKIP` trigger because consolidating it changes page-boundary and fail-open behavior.
- **Downstream signal** — Do not auto-implement; a human must compare bounded prompt context, thread-history ordering, and failure behavior on issues with `>100` comments before collapsing these two reads.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Implement failure diagnosis re-reads the same jobs payload the previous step already fetched
- **Safety tag** — `RISKY_SKIP`
- **File path and line ranges** — `.github/workflows/implement.yml:3087-3137` and `scripts/implement_diagnose_post_codex_failure.sh:124-153`
- **Current call count** — `2` logical reads of the same jobs endpoint per failing implement run, with up to `3` attempts in each loop (`<=6` underlying attempts)
- **Proposed call count** — `1` logical read on cache hit; keep the second read only as a cache-miss fallback
- **Endpoint(s)** — `GET /repos/{repo}/actions/runs/{run_id}/jobs?per_page=100`
- **Evidence**
  ```sh
  for _attempt in 1 2 3; do
    RUN_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}/actions/runs/${{ github.run_id }}/jobs?per_page=100" || true)"
    ...
    FAILED_STEP_NAME="$(printf '%s' "${RUN_JOBS_JSON}" | jq -r '[.jobs[].steps[] | select(.conclusion == "failure")] | first | .name // ""' 2>/dev/null || true)"
  done
  echo "failed_step_name=${FAILED_STEP_NAME}" >> "$GITHUB_OUTPUT"
  ```
  ```sh
  for _attempt in 1 2 3; do
    FAILED_STEP_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/jobs?per_page=100" || true)"
    ...
    FAILED_STEP_NAME="$(printf '%s' "${FAILED_STEP_JOBS_JSON}" | jq -r '
      [.jobs[].steps[]
        | select(
            .conclusion == "failure"
  ```
  The first step already computes `FAILED_STEP_NAME`, then the diagnose script re-fetches the same endpoint to compute it again.
- **Proposed fix** — Persist `FAILED_STEP_NAME` and optionally `RUN_JOBS_JSON` from `Capture post-Codex validation errors` into `${RUNTIME_DIR}`; update `scripts/implement_diagnose_post_codex_failure.sh` to read that cache first and only hit `/actions/runs/{run_id}/jobs` when the cache is missing or invalid.
- **Safety rationale** — This is inside a retrying failure-diagnosis path that explicitly compensates for GitHub job-finalization races, which is an explicit `RISKY_SKIP` class.
- **Downstream signal** — Do not auto-implement; a human must prove that a cached failed-step snapshot preserves `failure`, `cancelled`, and delayed-step-finalization behavior before removing the second jobs read.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — `review_rb_judge.sh` fetches every linked issue body even though only the first one is ever used
- **Safety tag** — `SAFE_TO_MERGE`
- **File path and line ranges** — `scripts/review_rb_judge.sh:159-170` and `scripts/review_rb_judge.sh:241-244`
- **Current call count** — `N` issue-body reads, where `N` is the number of linked issues
- **Proposed call count** — `1` issue-body read
- **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
  ```sh
  FIRST_ISSUE=""
  FIRST_ISSUE_BODY=""
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    if [ -z "${FIRST_ISSUE}" ]; then
      FIRST_ISSUE="${issue_number}"
    fi
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if [ -z "${FIRST_ISSUE_BODY}" ]; then
      FIRST_ISSUE_BODY="${BODY}"
    fi
  done <<< "${ISSUE_NUMBERS}"
  ```
  ```sh
  if [ -n "${FIRST_ISSUE}" ]; then
    echo "=== ISSUE #${FIRST_ISSUE} (original requirement) ==="
    echo "${FIRST_ISSUE_BODY}"
  fi
  ```
  `BODY` is fetched on every iteration, but only `FIRST_ISSUE_BODY` is ever consumed.
- **Proposed fix** — In `scripts/review_rb_judge.sh`, move the issue-body fetch inside the `if [ -z "${FIRST_ISSUE_BODY}" ]` block so only the first linked issue triggers the REST read.
- **Safety rationale** — The later loop iterations' body reads have no downstream consumers, no side effects, and no special retry/pagination behavior, so removing them does not change observable behavior.
- **Downstream signal** — In `scripts/review_rb_judge.sh`, guard the issue-body fetch with `if [ -z "${FIRST_ISSUE_BODY}" ]` so only the first linked issue is read.

#### DEAD-API-002 — `issue_pr_status.yml` has an unreachable PR title/body refetch branch
- **Safety tag** — `SAFE_TO_MERGE`
- **File path and line ranges** — `.github/workflows/issue_pr_status.yml:205-208` (supporting event payload setup at `.github/workflows/issue_pr_status.yml:181-182`)
- **Current call count** — up to `1` extra PR read on the fallback branch
- **Proposed call count** — `0`
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{PR_NUMBER}`
- **Evidence**
  ```sh
  PR_TITLE: ${{ github.event.pull_request.title }}
  PR_BODY: ${{ github.event.pull_request.body || '' }}
  ```
  ```sh
  PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
  fi
  ```
  On `pull_request.closed`, the workflow already has title/body from the event payload; this fallback only fires if both collapse to all-whitespace.
- **Proposed fix** — Remove the REST fallback and run the closing-keyword regex directly against `${PR_TITLE} ${PR_BODY}`.
- **Safety rationale** — This workflow only runs on a pull-request close event, and the same title/body are already present in the event payload; removing the fallback does not remove any reachable success path under GitHub's required-title contract.
- **Downstream signal** — In `.github/workflows/issue_pr_status.yml`, delete the blank-payload `gh api pulls/{PR_NUMBER}` fallback and use the event payload string directly for the fallback regex path.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — Consolidation crosses parallel reviewer watchdog processes and must preserve shared-sentinel timing plus per-reviewer abort semantics.
- BATCH-001: RISKY_SKIP — The seven-label sweep lives in `scripts/orchestrate_poll_process.sh`, a poller/race-defense path the policy explicitly excludes from auto-implementation.
- API-002: NEEDS_VERIFICATION — The overlap is real, but replacing per-issue body reads with cached/batched data changes source shape across steps and needs a freshness/alert-behavior check first.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 2 | DEAD-API-001, DEAD-API-002 |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 2 | MERGE-002, REUSE-001 |

### Implement-Stage Handoff
- DEAD-API-001
- DEAD-API-002
