## Executive Summary

- **`review_autofix` is the dominant bottleneck and the biggest avoidable waste source.** In `shubhodeep1/coding-workflows`, `review_autofix` ran 70 times, with `p50=701s`, `p95=3320.45s`, and **26.06 total runner-hours**. Ten of its 11 cancelled runs were launched within 30 seconds of a successful sibling run, wasting **12,331s (~3.4h, 13.2% of all `review_autofix` runtime)**. **Estimated impact:** 10-15% less review-phase runner time and lower queue pressure. **Confidence:** high.
- **The release smoke test is timing out healthy review runs.** `test_and_mark_stable` run **27456112610** failed in `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`; `step-006-e2e-smoke-test.log` lines **2136-2509** repeatedly showed review run **27456337761** as `status=in_progress`, then line **2510** errored with “Review phase stalled — no activity for 30 minutes.” That downstream review run actually **succeeded after 2564s**, finishing **249s later**. **Estimated impact:** eliminate false release failures and ~49-minute failed release cycles. **Confidence:** high.
- **CI instability is mostly from brittle tests, not platform issues.** Five CI failures (**27457327810, 27457379758, 27462446166, 27464751975, 27467267675**) all died in `lint / Validate harness RPC health-check unit tests`; `step-001-lint.log` line **852** shows `AssertionError: '"build_verification"' not found ...`. Another CI failure (**27468925858**) hit `lint / Clarify loop guard unit tests`; lines **886** and **2000** show `VALIDATION_DISCOVERY_FAILED ... looks_like_error_output` and `AssertionError: Failed to locate auto-answer Perl heredoc in plan.yml`. **Estimated impact:** materially cut the CI family’s **37.5% failure rate**. **Confidence:** high.
- **Prompt-size pressure is real, but prompt-cache telemetry is effectively missing.** Review run **27467613799** logged `CONTEXT_BUDGET_WARN` at **154,837** and **160,155** prompt tokens; parsed telemetry shows **6** context warnings repo-wide, but `cache_hit_rate` is **null** for all runs and `or_cache_read_tokens=or_cache_write_tokens=0` across **178** OpenRouter calls. **Estimated impact:** medium token/latency savings once prompt bloat is pruned and cache telemetry is fixed. **Confidence:** high on the gap, medium on savings.
- **AI memory retrieval is currently not helping review.** Across the 9 slow `review_autofix` deep dives, **9/9** `AI_MEMORY_TELEMETRY` `retrieve` events returned **0 records**, with `keyword_method=llm` every time and `estimated_tokens=0`. **Estimated impact:** small-to-medium improvement if empty retrievals are bypassed or the memory index/querying is fixed. **Confidence:** high.
- **Semble looks useful in runtime; Serena is effectively absent.** Repo aggregate telemetry shows **20 Semble queries / 248,039 logged bytes** and **15 Semble fallbacks**, but all 15 were **contract-test-only** (`context=contract-test`) and **runtime fallbacks were 0**. Serena query/fallback/probe totals were all **0**, and sampled review runs logged `SERENA_ENABLED: false` / `SERENA_AVAILABLE: false`. **Estimated impact:** keep Semble, don’t spend optimization time on Serena yet. **Confidence:** high.

## Speed Optimizations

1. **Prevent duplicate `review_autofix` siblings before heavy jobs start** *(critical path)*
   - **Evidence:** `review_autofix` had **11 cancelled runs**; **10** had a successful sibling created within 30 seconds. Examples:  
     - **27468925960** (`Internal: AI Review & Autofix`) cancelled after **2783s**; sibling **27468931448** (`Codex PR Self-Healing Semantic Agent`) succeeded after **2786s**.  
     - **27460808403** cancelled after **2358s**; sibling **27460813703** succeeded after **2358s**.  
     - Total cancelled `review_autofix` runtime = **12,347s**; sibling-paired waste = **12,331s**.
   - **Root cause:** duplicate workflow launches for the same PR/head SHA are being cancelled too late, after expensive review work has already started.
   - **Exact change:** add a top-level concurrency key on `repo + PR + head_sha` for all `review_autofix` variants, plus a very small preflight step that exits before `review_codex-agent` if a newer sibling already exists.
   - **Estimated time savings:** at least **3.4h** of runner time in this sample, plus indirect queue reduction for surviving runs.
   - **Implementation risk:** low, if keyed strictly to same PR/head and latest run wins.

2. **Replace the fixed 30-minute “stalled review” gate with heartbeat-aware waiting** *(critical path)*
   - **Evidence:** `test_and_mark_stable` run **27456112610** waited on review run **27456337761** until `step-006-e2e-smoke-test.log` line **2510** failed with a 30-minute stall message, but the downstream review run succeeded **249s later**. `review_autofix` family `p95` is **3320.45s**, already above the current 30-minute threshold.
   - **Root cause:** the smoke-test timeout policy is shorter than the observed healthy runtime distribution for real review runs.
   - **Exact change:** treat downstream review as healthy when its run is still updating or when a first heartbeat appears (for example, `phase_started`/log growth); only fail as stalled when neither status nor heartbeat advances. If full completion must stay required, set the timeout above observed `p95`.
   - **Estimated time savings:** removes entire **2952s** failed release cycles; on slow-but-healthy reviews, it avoids failing minutes before completion.
   - **Implementation risk:** medium; keep a fail-closed path for truly silent runs.

3. **Stop launching no-op child workflows that immediately skip** *(critical path)*
   - **Evidence:** of **757** skipped runs, **754** were just four families: `clarify` **198**, `orchestrate_clarify_respond` **194**, `plan` **182**, `implement` **180**. Recent examples **27470118320**, **27470118335**, **27470118332**, and **27470118316** all started and then evaluated `if` conditions to `false` in **1-9s**.
   - **Root cause:** fan-out happens before event/body/label predicates are fully resolved.
   - **Exact change:** move routing predicates into the parent dispatcher so child workflows are only dispatched when their trigger condition is already true; where practical, collapse these into jobs within one workflow.
   - **Estimated time savings:** small per run, but large cumulative scheduler/queue savings across **754** no-op launches.
   - **Implementation risk:** medium; workflow refactor, but behavior stays backward-compatible.

4. **Reduce poller and wait-loop overhead after first positive signal** *(medium impact)*
   - **Evidence:** `orchestrate_poll` ran **27** times with `p50=180s`, `p95=319.3s`; sampled run **27469694364** explicitly waited for a hosted runner. The release smoke test logged **185** repeated review-status checks for run **27456337761**.
   - **Root cause:** separate poll workflows and tight status loops keep waking up even after a run is clearly alive.
   - **Exact change:** back off aggressively after first heartbeat (for example, 5s → 15s → 30s → 60s), and avoid standalone poller launches when the parent workflow can hold the state cheaply.
   - **Estimated time savings:** **1-3 minutes** on poller-gated paths, and **>80% fewer** status checks on long waits.
   - **Implementation risk:** low.

5. **Trim Copilot review control-plane chatter** *(micro-optimization)*
   - **Evidence:** `copilot_pull_request_reviewer` run **27470006204** emitted **59** `Copilot API PUT /agents/sessions/...` calls and **1** progress `POST`; its log summary also showed `github-mcp-server` and `playwright` connected with **0 invocations**.
   - **Root cause:** high-frequency log shipping and unused sidecar startup.
   - **Exact change:** lower session-log flush frequency if configurable, and skip unused MCP bootstrap on code-review-only paths.
   - **Estimated time savings:** seconds to low tens of seconds per run.
   - **Implementation risk:** medium/low-confidence because some behavior may be platform-managed.

## Cost Optimizations

1. **Deduplicate `review_autofix` first; all measured AI spend is concentrated there**
   - **Evidence:** repo-wide measured AI usage was entirely in `review_autofix`: **18 Codex calls**, **36,468 Codex tokens**, **178 OpenRouter calls**, **20 Semble queries**. The same family also burned **12,331s** in cancelled sibling runs.
   - **Root cause:** duplicate review workflows are spending runner time—and likely unmeasured model time—before cancellation.
   - **Exact change:** same concurrency/preflight dedupe as above.
   - **Estimated savings:** measured lower bound is **13.2% of `review_autofix` runner time**; token savings are real but **not quantifiable** because cancelled runs do not have complete OR token telemetry.
   - **Quality-risk notes:** low risk; this removes duplicate work, not useful work.

2. **Prune non-Semble prompt bloat; Semble is probably not the cost problem**
   - **Evidence:** `review_autofix` run **27467613799** logged prompt sizes of **154,837** and **160,155** tokens. Parsed telemetry on **27468931448** added **4** more context-budget warnings, for **6** total repo-wide. By contrast, visible deep-dive `SEMBLE_QUERY` lines were all `target=reviewer-context`, averaged **14,768 bytes** and **516ms**, and repo aggregate was only **248,039 bytes across 20 queries**.
   - **Root cause:** large review prompts are being assembled from bulky per-PR context, not from Semble query payloads.
   - **Exact change:** dedupe repeated docs/context blocks, cap unchanged-file context, trim oversized diff/check-run/log-tail payloads, and keep high-variance sections later in the prompt.
   - **Estimated savings:** likely **tens of thousands of prompt tokens** on warned runs and minutes off slow reviews.
   - **Quality-risk notes:** medium. Keep Semble’s targeted reviewer-context fetches; prune the surrounding bulk first.
   - **Inference:** Semble appears to be replacing larger prompt expansion, not adding noisy low-value bytes.

3. **Fix OpenRouter/cache telemetry before trying model-downshift experiments**
   - **Evidence:** `recent/review_autofix/27470009517/step-001-gate.log` line **596** shows `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but repo aggregate still has `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `or_prompt_tokens=or_completion_tokens=or_total_tokens=0` across **178** OR calls. Per-run example: **27468931448** logged **24 OR calls** and **8 Semble queries**, yet OR token fields stayed zero.
   - **Root cause:** either OR token/cache telemetry is not emitted, or these calls bypass the prompt-cache accounting path.
   - **Exact change:** emit actual OR token totals and cache-hit/miss telemetry from review jobs before any model-selection tuning.
   - **Estimated savings:** not directly measurable today; this is the blocker to trustworthy dollar optimization.
   - **Quality-risk notes:** none.

4. **Fix CI rerun cost from brittle tests**
   - **Evidence:** the five repeated `build_verification` CI failures consumed **812s** total; adding **27468925858** brings observed failed CI time to **1,114s**.
   - **Root cause:** test/schema drift and brittle fixture parsing.
   - **Exact change:** update tests to current output schema and parse `plan.yml` structurally instead of via fragile heredoc matching.
   - **Estimated savings:** modest token savings, but meaningful runner-dollar savings and fewer reruns.
   - **Quality-risk notes:** low.

5. **Do not spend time on Serena cost tuning yet**
   - **Evidence:** `serena_query_calls=0`, `serena_query_response_bytes=0`, `serena_query_tool_calls=0`, `serena_fallbacks=0`, probes all zero. Sampled review runs logged `SERENA_ENABLED: false` / `SERENA_AVAILABLE: false`.
   - **Root cause:** Serena is effectively off in this window.
   - **Exact change:** none until Serena is actually enabled and emitting query/probe telemetry.
   - **Estimated savings:** none in current state.
   - **Quality-risk notes:** none.

**Model selection note:** the sampled control-plane paths already use smaller models where visible—e.g. `AGENTS_MD_MATERIALITY_MODEL: openai/gpt-5.4-mini` and `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini` in run **27470009517**. A broader model-mix audit is blocked by missing OR token telemetry.

## Reliability Improvements

1. **Fix the release smoke-test’s false “stalled review” detection**
   - **Failure evidence:** `test_and_mark_stable` run **27456112610**, `e2e-smoke-test / Phase 4`, failed at `step-006-e2e-smoke-test.log` line **2510** after long `in_progress` polling, while review run **27456337761** later succeeded.
   - **Root cause category:** timeout policy / liveness detection mismatch.
   - **Exact fix:** use downstream run heartbeat/log growth or `updatedAt` movement as liveness, and only fail when both status and heartbeat stop moving.
   - **Expected reliability impact:** removes a release-blocking false failure mode.
   - **Rollback / fail-open:** keep a hard fail when the review never starts or never produces a heartbeat.

2. **Repair the two brittle CI tests driving most observed failures**
   - **Failure evidence:**  
     - Runs **27457327810, 27457379758, 27462446166, 27464751975, 27467267675** all failed in `lint / Validate harness RPC health-check unit tests`; `step-001-lint.log` line **852** shows the `build_verification` assertion.  
     - Run **27468925858** failed in `lint / Clarify loop guard unit tests`; lines **886** and **2000** show discovery rejection noise plus the `plan.yml` heredoc assertion.
   - **Root cause category:** fixture/schema drift and brittle text matching.
   - **Exact fix:** align assertions with the current validation output and replace the heredoc locator with a YAML-aware lookup or stable marker.
   - **Expected reliability impact:** should eliminate most CI-family failures in this window.
   - **Rollback / fail-open:** low-risk; these are test-only changes.

3. **Keep Semble runtime fail-open, but fix the contract-test harness**
   - **Failure evidence:** parsed telemetry shows **15** `SEMBLE_FALLBACK` events: **5** in `test_and_mark_stable` run **27456112610** and **10** in CI run **27468925858**. Visible lines show `target=overflow ... reason=[Errno 2] No such file or directory: .../missing_semble ... context=contract-test`.
   - **Root cause category:** test harness/package setup, not production rollout.
   - **Exact fix:** make the contract tests inject a stub/mocked Semble binary or explicitly assert fail-open on missing binary without duplicating the fallback lines into parent logs.
   - **Expected reliability impact:** reduces noisy test failures without touching production behavior.
   - **Rollback / fail-open:** preserve today’s runtime fail-open behavior; **do not** disable Semble globally.

4. **Add stricter sibling-run suppression for `review_autofix`**
   - **Failure evidence:** **11** cancelled `review_autofix` runs, with **10** paired to nearby successful siblings.
   - **Root cause category:** duplicate dispatch / late cancellation.
   - **Exact fix:** gate all review variants behind the same PR/head concurrency group and check for inflight peers before starting `review_gate` and `review_codex-agent`.
   - **Expected reliability impact:** fewer confusing cancellations and less downstream ambiguity for release/test workflows.
   - **Rollback / fail-open:** latest run wins; if concurrency lookup fails, allow the run to proceed.

**Pressure signals:** `break_glass_count=0` repo-wide, so there is **no evidence of rubric/policy override pressure** in this window. `context_budget_warn_count=6` does indicate **prompt-size risk**: **2** warnings are visible in run **27467613799**, and parsed telemetry attributes the other **4** to review run **27468931448**.

**Semble/Serena rollout note:** all observed Semble fallbacks were contract-test-only; **runtime Semble fallbacks = 0**. Serena had **0 queries, 0 fallbacks, 0 probes**, so there is no sign of a broken Serena rollout—just a disabled one.

## AI Memory Health

- I found real `AI_MEMORY_TELEMETRY` only in the **9 slow `review_autofix` deep dives** under `slow/shubhodeep1_coding-workflows/review_autofix/*`. I did **not** find memory telemetry in other workflow families; if memory is supposed to run in plan/implement/orchestrate, verify emission there.
- **Retrieve effectiveness:** **9/9** unique `retrieve` events returned **0 records** (**0% hit rate**).  
  - Runs included **27455745833, 27457726245, 27462945755, 27465154508, 27467613799, 27468762901, 27468769932, 27468794477, 27468803713**.  
  - **Average `estimated_tokens`: 0**.  
  - **Budget comparison:** not possible; the retrieve telemetry did **not** emit a budget field.  
  - **`keyword_method` distribution:** `llm=100%`, `plain=0%`, `none=0%`.  
  - **Role distribution:** `reviewer=100%`.
- **Flags:** I found **no** `fail_open: true` retrieves and **no** `enabled: false` retrieves.
- **Push reliability:**  
  - `record-run-event`: **4/18** step-level events needed retries (`push_attempts=2` or `3`), including run **27455745833** (`phase_completed=3`), **27468769932** (`phase_completed=2`), **27468794477** (`phase_started=2`), and **27468803713** (`phase_completed=3`).  
  - `record-candidate`: **2/8** needed retries, in runs **27468762901** and **27468803713**.
- **Observed ops:** only `retrieve`, `record-run-event`, and `record-candidate` appeared. I saw **no** `finalize-task`, `promote`, `compact`, `processed-command-claim`, or `processed-command-complete` telemetry in the inspected logs.

**Recommendation:** short-circuit memory retrieval when the store is empty or prior hit rate is zero, add a budget field plus corpus-size/index-status to retrieve telemetry, and investigate the intermittent push retries before relying on memory as a latency or quality lever.

## GH API Call Audit

- **Repo policy cross-check:** this repo’s README (“H6 API Hygiene Inventory/Reporting Notes”) says API hygiene should prefer **batched GraphQL** and **cycle-local caches** before adding per-item GitHub calls. The observed hot paths do not fully follow that guidance.

1. **High-volume Actions run-status polling in the release smoke test**
   - **Evidence:** `test_and_mark_stable` run **27456112610**, `e2e-smoke-test`, logged **185** repeated `Review run #27456337761: status=in_progress` checks before failing.
   - **Pattern:** repeated status polling of the same downstream Actions run.
   - **Recommendation:** after first positive heartbeat, switch to slower backoff (for example, 30-60s), or stop polling until `updatedAt` changes.
   - **Estimated reduction:** likely **>80% fewer status checks** on long waits, with lower rate-limit risk.

2. **Redundant gate-step data fetching in `review_autofix`**
   - **Evidence:** `recent/review_autofix/27470009517/step-004-gate_Evaluate_review_gate.log` prints separate GH fetch branches for:
     - PR JSON (**line 41**),
     - GraphQL linked data (**line 83**),
     - commit metadata (**line 168**),
     - paginated PR files (**lines 250 and 307**).
   - **Pattern:** duplicated code paths for PR-file enumeration and multiple independent lookups that could reuse one cached blob.
   - **Recommendation:** fetch `pr_meta`, `commit_meta`, and `pr_files_json` once each per gate execution and reuse them across materiality/linkage branches.
   - **Estimated reduction:** **2-4 calls per gate execution**, plus simpler fail-open behavior.

3. **Conservative but chatty PR-head stability checking**
   - **Evidence:** the same smoke-test step uses two identical `GET /pulls/{n}` head-SHA reads per stability attempt at **lines 1418** and **1420**, with a 3-second delay between them.
   - **Pattern:** repeated lookup in a loop.
   - **Recommendation:** keep the safety check, but only escalate to the second read when the first read differs from cached state, or reuse ETag/last-seen SHA to avoid unnecessary repeats.
   - **Estimated reduction:** small, but worthwhile in the hottest control path.
   - **Note:** this one is intentionally defensive, so optimize carefully.

4. **Copilot control-plane chatter with no MCP reuse**
   - **Evidence:** `copilot_pull_request_reviewer` run **27470006204** emitted **59** `PUT /agents/sessions/...` calls and **1** progress `POST`; its `log_summary` says `"github-mcp-server"=connected/invocations=0` and `"playwright"=connected/invocations=0`.
   - **Pattern:** many control-plane writes without evidence of MCP batching/reuse.
   - **Recommendation:** reduce log flush frequency if configurable; otherwise treat this as platform overhead, not something the workflow should amplify further.
   - **Estimated reduction:** small on runner time, but large on endpoint noise.

- **Rate-limit signal:** I did **not** find an executed rate-limit/backoff event in the deep-dive logs. The repo has wrappers for it, but this window shows **potential hot spots without an actual rate-limit incident**.

## Prompt Cache & Memory System

- **Prompt cache is nominally enabled.** `review_autofix` run **27470009517** logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **Effective prompt-cache behavior is unmeasured.** Repo aggregate telemetry still shows:
  - `cache_hit_rate = null`
  - `or_cache_write_tokens = 0`
  - `or_cache_read_tokens = 0`
  - `or_prompt_tokens = or_completion_tokens = or_total_tokens = 0`
  despite **178** OR calls.
- **Context pressure is confirmed.** Run **27467613799** emitted `CONTEXT_BUDGET_WARN` at **154,837** and **160,155** prompt tokens; parsed telemetry adds **4** more warnings on **27468931448**.
- **Inference:** cache fragmentation is more likely coming from large, unstable per-PR payloads than from Semble. Recent gate summaries show very large diffs—e.g. run **27470009517** had **98 files / 1817 additions / 182 deletions**, and run **27468925960** had **97 / 1800 / 178**—while visible Semble payloads were small and targeted.
- **Memory retrieval is not helping prompt size today.** All **9** observed retrieve calls returned **0 records**, so memory is not offsetting prompt expansion.

**Concrete improvements**
1. Emit real `cache_hit_rate` and OR token totals from review jobs.
2. Freeze a stable prompt prefix; move diff/check-run/log-tail sections later.
3. Deterministically sort and dedupe file/context blocks so cache prefixes survive small PR churn.
4. Skip memory retrieval when no index/corpus is available or when recent hit rate is zero.
5. Keep Semble reviewer-context fetches; prune the much larger surrounding prompt assembly first.

**Expected impact:** medium token and latency savings on large review runs, plus better reliability from staying farther away from context limits.

## Orchestrator Health

- **Healthy signals**
  - `break_glass_count = 0`.
  - No Serena query/fallback/probe activity.
  - No runtime Semble fallbacks.
  - `orchestrate` itself was **2/2 success**; `orchestrate_poll` was **25 success / 2 cancelled**.

- **Recurring operational pain points**
  - **Fan-out skip storm:** `clarify`, `orchestrate_clarify_respond`, `plan`, and `implement` account for **754 skipped runs**.
  - **Queue sensitivity:** hosted-runner wait messages appeared in recent `review_autofix` gate/system logs (**27470009517**), Copilot review system logs (**27470006204**), and poller summaries (**27469694364**, **27468925960**).
  - **Review-stage churn:** duplicate/cancelled `review_autofix` runs create ambiguity for parent workflows and release gates.

- **Smallest safe mitigations**
  1. Filter no-op branches before child-workflow dispatch.
  2. Add sibling-run suppression for review/autofix variants.
  3. Track these indicators on every report:
     - `review_autofix` cancelled-with-nearby-success rate
     - time from review workflow creation to first heartbeat
     - `orchestrate_poll` duration
     - skipped-run ratio
     - `context_budget_warn_count`

- **Gap:** I did not see evidence of wave-progression bugs, conflict-heal retry storms, or bad terminal states in this window beyond the release smoke timeout. If those are current concerns, widen deep-dive sampling around `orchestrate_poll` and merge-conflict paths.

## Pipeline Flow Bottlenecks

1. **Clarify / respond dispatch**
   - **Bottleneck type:** scheduler/no-op overhead.
   - **Evidence:** `clarify` **198 skipped**, `orchestrate_clarify_respond` **194 skipped**.
   - **Fix:** dispatch only when command/body predicates already match.

2. **Plan**
   - **Bottleneck type:** moderate compute when active.
   - **Evidence:** successful active plan runs reached **537s** (**27468417469**) and **506s** (**27462539893**), though family `p50` is diluted by skips.
   - **Fix:** only optimize after review-stage issues are addressed.

3. **Implement**
   - **Bottleneck type:** moderate compute when active.
   - **Evidence:** successful implement runs reached **966s** (**27468550499**) and **785s** (**27462711594**).
   - **Fix:** secondary priority; not the main end-to-end limiter.

4. **Review / autofix**
   - **Bottleneck type:** dominant compute + queue + cancellation overhead.
   - **Evidence:** family `p50=701s`, `p95=3320.45s`, **26.06 total runner-hours**, duplicate cancels, and runner waits. Parsed AI-heavy review runs were **2786-3507s** each.
   - **Fix:** dedupe sibling runs, reduce prompt size, and make smoke-test gating heartbeat-aware.

5. **Validate / release gate**
   - **Bottleneck type:** retry/poll overhead and false failure.
   - **Evidence:** smoke-test run **27456112610** spent most of its life waiting on review completion and failed minutes before success.
   - **Fix:** rework wait logic around liveness instead of a blunt 30-minute inactivity rule.

6. **Orchestrate poller**
   - **Bottleneck type:** queue + control-plane wait.
   - **Evidence:** `orchestrate_poll` `p50=180s`, `p95=319.3s`; sampled run **27469694364** waited for a runner.
   - **Fix:** avoid separate poller launches when the parent workflow can cheaply hold state.

7. **Merge/conflict overhead**
   - **Evidence:** no direct conflict-heal spike was visible in this sample.
   - **Fix:** monitor, but do not prioritize until it appears in logs.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` runtime and cancellation churn
  - release smoke-test waiting on downstream review completion
  - poller/dispatcher overhead from many skipped child workflows

- **Top failure modes**
  - false `review_autofix` stall detection in `test_and_mark_stable` (**27456112610**)
  - repeated brittle CI assertions around `build_verification`
  - brittle `plan.yml` heredoc discovery in clarify-loop tests

- **Highest-cost drivers**
  - `review_autofix`: **70 runs**, **26.06 runner-hours**
  - cancelled sibling `review_autofix` runs: **12,331s** wasted
  - oversized review prompts: **6** context-budget warnings
  - Copilot review side path: **20 runs**, **1.55 runner-hours**

- **Top 3 prioritized actions**
  1. **Deduplicate `review_autofix` by PR/head SHA before heavy jobs start.**
  2. **Make release smoke-test waiting heartbeat-aware, not 30-minute inactivity-based.**
  3. **Fix the two brittle CI tests and the Semble contract-test harness.**

## Metrics Appendix

### Repo-level run metrics

| Repo | Total runs | Success | Failure | Cancelled | Skipped / other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) | Parsed telemetry runs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 220 | 7 | 16 | 757 *(all 757 are `skipped` in `workflow_log_report.json`)* | 0.7% | 147.908 | 2.0 | 720.05 | 116 |

### Key workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Skipped | Failure rate | Total runtime (h) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 70 | 57 | 0 | 11 | 2 | 0.0% | 26.06 | 701.0 | 3320.45 |
| ci | 16 | 9 | 6 | 1 | 0 | 37.5% | 5.05 | 1654.0 | 1752.75 |
| orchestrate_poll | 27 | 25 | 0 | 2 | 0 | 0.0% | 1.44 | 180.0 | 319.3 |
| copilot_pull_request_reviewer | 20 | 20 | 0 | 0 | 0 | 0.0% | 1.55 | 272.0 | 452.15 |
| plan | 195 | 13 | 0 | 0 | 182 | 0.0% | 1.30 | 1.0 | 204.0 |
| implement | 195 | 13 | 0 | 2 | 180 | 0.0% | 2.34 | 1.0 | 424.7 |
| clarify | 212 | 14 | 0 | 0 | 198 | 0.0% | 0.59 | 1.0 | 94.45 |
| orchestrate_clarify_respond | 195 | 1 | 0 | 0 | 194 | 0.0% | 0.17 | 1.0 | 10.0 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 100.0% | 0.82 | 2952.0 | 2952.0 |
| validate | 2 | 2 | 0 | 0 | 0 | 0.0% | 0.15 | 261.5 | 302.45 |

### Cost, cache, and wall-clock telemetry

| Scope | Parsed runs | Codex calls | Codex tokens | OR calls | OR prompt / completion / total | OR cache write / read | cache_hit_rate | wall_clock_p50_ms | wall_clock_p99_ms | context_budget_warn_count | break_glass_count |
|---|---:|---:|---:|---:|---|---|---|---:|---:|---:|---:|
| Repo aggregate | 116 | 18 | 36,468 | 178 | 0 / 0 / 0 | 0 / 0 | null | 7,000 | 3,504,600 | 6 | 0 |
| review_autofix family | 13 | 18 | 36,468 | 178 | 0 / 0 / 0 | 0 / 0 | null | 3,056,000 | 4,383,480 | 6 | 0 |
| ci family | 6 | 0 | 0 | 0 | 0 / 0 / 0 | 0 / 0 | null | 165,000 | 295,550 | 0 | 0 |

### Semble / Serena / MCP telemetry

| System / target | Query calls | Logged bytes / response bytes | Avg bytes per query | Fallbacks | Contract-test fallbacks | Runtime fallbacks | probe_ok | probe_failed | probe_skipped | Notes |
|---|---:|---:|---:|---:|---:|---:|---|---|---|---|
| Semble (repo aggregate) | 20 | 248,039 | 12,402 | 15 | 15 | 0 | n/a | n/a | n/a | All measured queries/fallbacks came from review/test paths |
| Semble `reviewer-context` *(visible deep-dive subset)* | 8 | 118,144 | 14,768 | 0 | 0 | 0 | n/a | n/a | n/a | 8 of 9 slow review runs; avg query time **516ms** |
| Serena (repo aggregate) | 0 | 0 | n/a | 0 | n/a | n/a | 0 | 0 | 0 | No Serena tool calls observed; `serena_query_tool_calls=0` |
| Other MCP servers observed | 0 telemetry | 0 | n/a | 0 | n/a | n/a | n/a | n/a | n/a | No `<NAME>_QUERY/FALLBACK/PROBE` beyond Semble/Serena. `27470006204` log summary showed `github-mcp-server` and `playwright` connected with **0 invocations** |

### AI memory telemetry

| Scope | Unique runs with `retrieve` telemetry | Retrieve hit rate | Avg `estimated_tokens` | Budget field emitted? | `keyword_method` distribution | `records_selected > 0` | `fail_open:true` retrieves | `enabled:false` retrieves | `record-run-event` pushes with retries | `record-candidate` pushes with retries |
|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|
| Slow `review_autofix` deep dives | 9 | 0/9 = 0% | 0 | No | `llm=100%` | 0 | 0 | 0 | 4/18 | 2/8 |

### GH API / control-plane hotspot summary

| Workflow / run | Job / step | Hotspot signal | Estimated issue | Recommended change |
|---|---|---|---|---|
| `test_and_mark_stable` / **27456112610** | `e2e-smoke-test / Phase 4` | **185** repeated downstream review-status checks before timeout | High-volume Actions polling | Back off after first heartbeat; poll on `updatedAt` changes |
| `review_autofix` / **27470009517** | `gate / Evaluate review gate` | Separate PR, GraphQL, commit, and dual PR-files fetch branches (lines 41, 83, 168, 250, 307) | Redundant per-gate lookups | Fetch once and reuse cached JSON |
| `copilot_pull_request_reviewer` / **27470006204** | `Processing Request Linux` | **59** session-log `PUT`s + **1** progress `POST` | Control-plane chatter | Lower log flush cadence if configurable |
| Repo-wide | n/a | No executed rate-limit/backoff event observed | Low current rate-limit risk, but hot-loop exposure exists | Fix hot loops before scale increases |

### Per-target MCP availability

| MCP server / target | probe_ok | probe_failed | probe_skipped | Query calls | Fallbacks | Availability note |
|---|---:|---:|---:|---:|---:|---|
| Serena | 0 | 0 | 0 | 0 | 0 | Disabled/unavailable in sampled review runs |
| Semble | n/a | n/a | n/a | 20 | 15 | No probe telemetry emitted; runtime fallbacks remained 0 |
| Other MCP servers | n/a | n/a | n/a | 0 telemetry | 0 telemetry | No `<NAME>_QUERY/FALLBACK/PROBE` emitted in this window |
