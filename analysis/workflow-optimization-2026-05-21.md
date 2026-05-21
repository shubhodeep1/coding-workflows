## Executive Summary

- `review_autofix` is the dominant bottleneck: 150 runs consumed 175,031s (48.6h), or 65.2% of all observed runtime; sampled slow runs show `codex-agent` alone taking 3,053-3,784s in runs `26160440710`, `26162540427`, and `26154599231`. **Estimated impact:** high (largest latency/cost lever). **Confidence:** high.
- Cancellation waste is concentrated in `review_autofix`: 40 of 42 cancelled runs (95.2%) came from that family, burning 29,254s (8.13h); 12 cancelled review runs lasted more than 30 minutes before cancellation. **Estimated impact:** high if stale/superseded runs are killed earlier. **Confidence:** medium.
- `ci` is the next steady-state long pole: 64 runs consumed 60,338s (16.8h), 22.5% of total runtime, with p50 948.5s; failures `26157790206` and `26202338589` surfaced only after 842-895s because the single `lint` job is long and sequential. **Estimated impact:** medium-high via fail-fast ordering. **Confidence:** high.
- The `test_and_mark_stable` false-negative Plan polling failure was real in run `26177257854` (`e2e-smoke-test / Phase 2: Wait for plan to complete`, 5,468s), but the workflow file is now fixed; the remaining reliability issue is stale regression coverage, shown by CI failure `26202338589`. **Estimated impact:** high on failure/rerun reduction. **Confidence:** high.
- Semble is net-positive but still has low-value repetition: operational deep-dive logs show 24 `SEMBLE_QUERY` events totaling 215,032 bytes, mostly targeted `reviewer-context` and overflow lookups, but run `26160440710` queried `scripts/orchestrate_poll_process.sh` twice in the same `codex-agent` step. **Estimated impact:** medium on token/context efficiency, low on wall-clock. **Confidence:** high.
- AI memory is instrumented but not yet useful for retrieval: 28 structured telemetry events were observed, but all 7 `retrieve` operations selected 0 records, average `estimated_tokens` was 0, and 5 review runs emitted malformed glued JSON+telemetry lines. **Estimated impact:** medium on future cost/quality gains, low immediate runtime gain. **Confidence:** high.

## Speed Optimizations

### Critical-path wins

1. **Add a middle review tier for small diffs in `review_autofix`**
   - **Evidence:** `review_autofix` accounts for 65.2% of all runtime. Slow runs `26154599231` (`codex-agent` ~3,784s), `26162540427` (~3,306s), and `26170059805` (~3,381s) were all dominated by the `codex-agent` step. Sampled gate/codex logs for run `26154599231` show a full six-model reviewer panel (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`, `qwen/qwen3.6-plus`, `x-ai/grok-4.20`) plus `MODEL_EDITOR: openai/gpt-5.4` and `REVIEWER_REASONING_EFFORT: xhigh`.
   - **Root cause:** The current gate is effectively binary: skip or full-strength review. Even small diffs still pay for the full reviewer ensemble. Recent run summaries show examples like `AUTOFIX_GATE_DET_SKIP_EVAL pr=2841 files=1 additions=17 ... skip=false` in `26202338652` and `files=2 additions=19 ... skip=false` in `26205019445`.
   - **Exact change:** Keep the current skip logic, but add a middle tier: for low-risk diffs (for example, ≤2 files, small additions, and no workflow/shell/prompt changes), run 2-3 reviewers at lower reasoning effort; keep the full six-reviewer + `xhigh` path for workflow, shell, prompt, and cross-file behavioral changes.
   - **Estimated time savings:** **Inference:** 20-40% on eligible `review_autofix` runs, roughly 8-20 minutes on the current 30-60 minute outliers.
   - **Implementation risk:** Medium; mitigate by auto-escalating to the full panel on reviewer disagreement or risky file patterns.

2. **Kill stale/superseded review runs before `codex-agent` starts**
   - **Evidence:** `review_autofix` produced 40 cancelled runs totaling 29,254s; long cancelled examples include run `26170076339` (3,379s), `26204109354` (1,868s), and `26202338652` (1,811s). `review_autofix.yml` already has pre-dispatch dedupe logic at lines 5282-5298, but sampled cancellations show redundant work still survives long enough to burn expensive agent time.
   - **Root cause:** Successor detection happens too early in the dispatch path and not late enough before the expensive agent step.
   - **Exact change:** Immediately before `review.codex-agent`, re-fetch PR head SHA / branch state and exit 0 if the run is superseded, the PR is closed, or a newer queued/running sibling exists. Repeat the same check just before continuation dispatch.
   - **Estimated time savings:** Up to **29,254s per 1,000-run window** in avoided cancelled work, plus indirect queue relief.
   - **Implementation risk:** Low-medium if implemented fail-open (proceed if the recheck fails).

3. **Front-load the regression-prone CI tests instead of discovering them 14-15 minutes in**
   - **Evidence:** CI failures `26157790206` and `26202338589` both failed in `lint`, after 842s and 895s respectively. The actual failing assertions were:
     - `26157790206`: `FAIL test_prompt_budget_helpers_fail_closed_on_non_numeric_counters`
     - `26202338589`: `tests/test_test_and_mark_stable_plan_polling_guard.py` assertion failure
     Current `ci.yml` runs a very long sequence of static checks and contract tests inside one `lint` job.
   - **Root cause:** Failure-prone contract tests run late in a long serial job.
   - **Exact change:** Move `tests/test_review_rb_judge_label_propagation.py` / prompt-budget helper coverage and `tests/test_test_and_mark_stable_plan_polling_guard.py` to the top of the job, right after Python setup and syntax checks. Defer coverage gates and long tail contract suites until after those fail-fast guards.
   - **Estimated time savings:** Roughly **12-15 minutes faster failure feedback** on regressions like the two observed CI failures.
   - **Implementation risk:** Low.

### Micro-optimizations

4. **Remove the duplicate `test_orchestrate_integration_ahead_by_gate.py` invocation from `test-and-mark-stable`**
   - **Evidence:** `.github/workflows/test-and-mark-stable.yml` runs `tests/test_orchestrate_integration_ahead_by_gate.py` in both the Phase 5 block (line 2164) and the later Unit tests block (line 3276).
   - **Root cause:** Duplicate test invocation inside the same workflow.
   - **Exact change:** Keep a single invocation and let the dedicated `ci.yml` job remain the canonical regression gate.
   - **Estimated time savings:** Small (seconds to low minutes per `test_and_mark_stable` run).
   - **Implementation risk:** Low.

## Cost Optimizations

Direct dollar estimates are limited because the deep-dive logs did **not** emit operational `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` counters. The clearest hard token number in this window is from `workflow_log_analysis` run `26177484217`, where `summarize_unselected_runs` used **263,076 tokens**.

1. **Right-size the reviewer panel and reasoning level for low-risk diffs**
   - **Evidence:** Sampled `review_autofix` runs log a six-model reviewer panel plus `MODEL_EDITOR: openai/gpt-5.4`; run `26154599231` also logged `REVIEWER_REASONING_EFFORT: xhigh`. The family consumed 175,031s across 150 runs.
   - **Root cause:** Full reviewer fan-out appears to be the default for all non-skipped review runs.
   - **Exact change:** Add a cost tier: small, non-workflow/non-shell diffs get a reduced reviewer set and lower reasoning effort; risky diffs keep the current full panel.
   - **Estimated savings:** **Inference:** roughly halve reviewer-call spend on eligible small-diff runs.
   - **Quality-risk notes:** Medium. Guard with automatic escalation on disagreement, high-risk path matches, or failed downstream validation.

2. **Stop summarizing low-value skipped control-plane runs in `workflow_log_analysis`**
   - **Evidence:** `workflow_log_analysis` run `26177484217` spent 263,076 tokens summarizing 96 of 100 targeted unselected runs. In the main window, `clarify` + `plan` + `implement` + `orchestrate_clarify_respond` accounted for **597 runs** but only **7,803s** total runtime; 375 of those runs were `<=1s` “other/skipped-like” executions.
   - **Root cause:** The analysis workflow is spending model budget on control-plane noise.
   - **Exact change:** Skip summarization for `duration_seconds <= 2` runs in control-plane families unless they contain warnings, retries, or MCP/memory telemetry; alternatively, lower the unselected-run target when failure_count is low.
   - **Estimated savings:** **Inference:** 35-60% of analysis-workflow token use in similar windows.
   - **Quality-risk notes:** Low if failure/slow/recent deep dives remain unchanged.

3. **Memoize Semble overflow queries within a run**
   - **Evidence:** Deep-dive operational logs show 24 `SEMBLE_QUERY` events totaling 215,032 bytes and 11,518ms. Overflow accounted for 13 queries / 82,360 bytes. Run `26160440710` queried `scripts/orchestrate_poll_process.sh` twice in the same `codex-agent` step (12:28:03, 6,877 bytes; 12:34:53, 7,153 bytes).
   - **Root cause:** Reviewer/editor/conflict-resolver phases rediscover the same large files independently.
   - **Exact change:** Cache Semble responses per run by `(target,file,chunks)` in the runtime directory and reuse them across phases.
   - **Estimated savings:** 10-20% of Semble bytes and a few seconds on heavy review runs.
   - **Quality-risk notes:** Low.
   - **Semble assessment:** **Inference:** Semble is reducing prompt expansion overall, because the logged context is targeted by file/role instead of dumping full files into prompts.
   - **Serena assessment:** No operational `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines were observed in sampled primary logs, so Serena is not currently replacing downstream tool/model work in this window.

4. **Eliminate avoidable rerun/cancel spend**
   - **Evidence:** 40 cancelled `review_autofix` runs burned 29,254s, and the false-negative `test_and_mark_stable` failure `26177257854` burned 5,468s before failing.
   - **Root cause:** Late supersession detection plus stale regression coverage after the Plan-poll fix.
   - **Exact change:** Implement the reliability fixes below; they pay for themselves in both runner time and model time.
   - **Estimated savings:** Medium-high in aggregate window cost.
   - **Quality-risk notes:** Low.

## Reliability Improvements

1. **Finish the Plan-polling fix rollout by updating the stale regression test**
   - **Failure evidence:** Failed smoke run `26177257854` (`test_and_mark_stable`, `e2e-smoke-test / Phase 2: Wait for plan to complete`) used the old `actions/runs?per_page=50` logic and hard-failed with `status=plan_failed`; later evidence in run summary `26204109354` says the real Plan run was the 51st item and finished about 10 seconds later. Current repo state shows the workflow has been fixed in `.github/workflows/test-and-mark-stable.yml` lines 680-760, but CI run `26202338589` still failed because `tests/test_test_and_mark_stable_plan_polling_guard.py` asserted the old string.
   - **Root cause category:** Workflow/test drift after a bug fix.
   - **Exact fix:** Update `tests/test_test_and_mark_stable_plan_polling_guard.py` to assert the new `fetch_plan_runs_json`/shape-validation behavior, and ensure the fixed workflow is the version used by `stable`/reusable refs.
   - **Expected reliability impact:** Removes **1 of the 3 observed failures** in this window and prevents another 91-minute false-negative smoke failure.
   - **Rollback / fail-open:** Prefer the current “retry/wait” behavior over any fallback that silently rewrites invalid payloads to empty success-like JSON.

2. **Fix or confirm the prompt-budget numeric-coercion regression in `review_rb_judge` fallback helpers**
   - **Failure evidence:** CI run `26157790206` was labeled broadly as `Review-blocked judge label propagation contract test`, but the actual failing test in `lint` was `test_prompt_budget_helpers_fail_closed_on_non_numeric_counters`.
   - **Root cause category:** Shell fallback helper contract drift.
   - **Exact fix:** Ensure fallback prompt-budget counters are coerced to `0` before integer comparisons in `_embed_input_file()` and related helper paths; keep the existing unit test, but move it earlier in CI.
   - **Expected reliability impact:** Addresses the remaining distinct CI failure mode seen in this window.
   - **Rollback / fail-open:** Default malformed counters to `0` rather than aborting on arithmetic comparison errors.

3. **Fix malformed `AI_MEMORY_TELEMETRY` emission**
   - **Failure evidence:** Five sampled `review_autofix` runs (`26154599231`, `26159879998`, `26160440710`, `26170059805`, `26201815742`) emitted a raw JSON blob glued directly onto the prefixed telemetry line in `codex-agent`, making line-based parsing brittle.
   - **Root cause category:** Telemetry formatting / observability defect.
   - **Exact fix:** Emit raw JSON and `AI_MEMORY_TELEMETRY:` as separate writes with a newline boundary, or send one of them to a separate stream.
   - **Expected reliability impact:** Prevents parser breakage and makes memory-health dashboards trustworthy.
   - **Rollback / fail-open:** Telemetry emission should stay non-blocking; formatting failures must not fail the workflow.

4. **Keep Semble’s fail-open behavior, but stop alerting on the test-only fallbacks**
   - **Failure evidence:** There were 15 operational `SEMBLE_FALLBACK` lines in deep-dive logs, all `target=overflow`, all `reason=[Errno 2] ... missing_semble`, and all confined to test runs: CI `26157790206`, CI `26202338589`, and `test_and_mark_stable` `26177257854`.
   - **Root cause category:** Healthy fail-open test coverage, not production breakage.
   - **Exact fix:** Filter `missing_semble` test cases out of production fallback alerts/reports, or tag them as test-only.
   - **Expected reliability impact:** Lower false-positive operational noise without changing runtime behavior.
   - **Rollback / fail-open:** No runtime rollback needed; keep the current fail-open behavior.
   - **Serena note:** No operational `SERENA_FALLBACK` or `SERENA_PROBE` events were observed in this window, so there is no evidence of a masked broken Serena rollout here.

## AI Memory Health

- **Observed telemetry:** 28 structured `AI_MEMORY_TELEMETRY` events across sampled deep-dive primary logs: 14 `record-run-event`, 7 `record-candidate`, and 7 `retrieve`.
- **Retrieve hit rate:** **0/7 = 0%**. Every sampled retrieve was in `review_autofix` reviewer context (for example, run `26154599231`, `codex-agent`, line 3333: `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`).
- **Average estimated tokens vs budget:** Average `estimated_tokens` was **0**. No explicit budget field was emitted in the sampled retrieve events, so budget utilization cannot be computed.
- **Keyword method distribution:** `none` = **7/7**; `llm` = 0; `plain` = 0.
- **Flags:** No sampled retrieve had `enabled:false`; none had `fail_open:true`.
- **Push retries:** One retry anomaly was observed: run `26170059805` logged `record-run-event` with `push_attempts: 2`.
- **Missing lifecycle ops:** No `promote`, `finalize-task`, `compact`, `processed-command-claim`, or `processed-command-complete` ops were observed in the sampled deep-dive logs.
- **Assessment:** Memory is recording events/candidates, but it is not producing usable retrievals yet.
- **Recommendation:** Verify that successful review runs actually promote/finalize records before the next retrieve, and add a non-`none` keyword path. Fix the malformed emission first; otherwise, memory-health analytics will stay untrustworthy.

## GH API Call Audit

1. **Keep the new scoped Plan polling logic; backport it anywhere old polling still exists**
   - **Evidence:** Failed smoke run `26177257854` repeatedly used the old broad `actions/runs?per_page=50&created=>...` list polling in `e2e-smoke-test`, then misclassified the Plan phase as failed. The current repo workflow now scopes Plan lookup and validates payload shape in `.github/workflows/test-and-mark-stable.yml` lines 680-760.
   - **High-volume/redundancy pattern:** Repeated list polling over Actions runs and issue labels inside a wait loop.
   - **Concrete batching/reuse change:** Treat the new scoped helper as canonical; remove/backport any older copies on other refs.
   - **Estimated reduction:** Moderate Actions API reduction in smoke loops, but the bigger win is rate-limit and false-negative risk reduction.

2. **Collapse `internal-review`’s two REST lookups into one GraphQL query**
   - **Evidence:** `.github/workflows/internal-review.yml` lines 98-101 make two separate calls in `resolve-claude-branch-pr`: one to list open PRs for the head branch, and one to fetch `default_branch`.
   - **High-volume/redundancy pattern:** Fixed two-call pattern for every push-triggered branch-resolution path.
   - **Concrete batching/reuse change:** Replace with one GraphQL query returning both open PR match and repository default branch.
   - **Estimated reduction:** **50% call reduction for that step** (2 → 1), lower rate-limit surface, low risk.

3. **Extend the existing conditional-fetch discipline to queued/completed Actions lists**
   - **Evidence:** `scripts/orchestrate_poll_process.sh` lines 5408-5464 already use ETag/`If-None-Match` for the in-progress Actions runs fetch, which is good API hygiene. On cache miss, the helper still always fetches queued (`per_page=50`) and completed (`per_page=20`) runs afterward.
   - **High-volume/redundancy pattern:** Conditional fetch for one status, unconditional fetches for the other two.
   - **Concrete batching/reuse change:** Cache queued/completed snapshots briefly inside the same poll loop, or fetch completed runs only when stall diagnostics actually need conclusions.
   - **Estimated reduction:** Up to **1-2 GH API calls per refresh cycle** on cache misses; medium rate-limit improvement, medium implementation risk.

4. **Preserve the good GraphQL batching already present in `issue_pr_status`**
   - **Evidence:** Recent run `26206096275` (`issue_pr_status / sync-status`) resolved linked issues in 2 GraphQL calls: one PR-linked-issue query and one `ORCH_QUERY` alias bundle.
   - **Audit outcome:** This is good hygiene, not a hotspot.
   - **Recommendation:** Reuse this GraphQL batching pattern when collapsing other multi-call REST lookups.
   - **Rate-limit evidence:** No 429 or backoff/retry hotspot was visible in sampled deep-dive logs.

## Prompt Cache & Memory System

- **Prompt-cache observability gap:** No operational `cache_creation_input_tokens` or `cache_read_input_tokens` lines were emitted in the sampled deep-dive logs, so true prompt-cache hit/miss behavior is unmeasurable in this window.
  - **Improvement:** Emit those counters from reviewer/editor wrappers before making prompt-cache policy changes.
  - **Impact:** High measurement value; token/latency impact not yet quantifiable.

- **Semble is acting as a context compressor, but with some fragmentation**
  - **Evidence:** 24 operational `SEMBLE_QUERY` events across 7 sampled review runs; by target: `reviewer-context` 7 / 103,584 bytes, `overflow` 13 / 82,360 bytes, `conflict-resolver-context` 4 / 29,088 bytes.
  - **Assessment:** **Inference:** This is probably reducing prompt expansion versus whole-file inclusion, because the logged bytes are bounded and targeted.
  - **Improvement:** Prefetch once per run after reviewer consensus, then reuse across editor/conflict-resolver phases; memoize overflow queries by file.
  - **Estimated impact:** Small-moderate token/context reduction, low runtime risk.

- **The GitHub Actions review-ledger cache works, but keys are fragmented per run**
  - **Evidence:** Across 8 sampled slow `review_autofix` runs, there were 7 restore attempts, 6 restore hits, 1 miss, and 7 saves. `review_autofix.yml` uses a run-specific key at lines 3401-3403 and 3617, relying on a PR-scoped restore prefix to recover older state.
  - **Assessment:** The cache is helpful, but every run creates a new save key even when state may be unchanged.
  - **Improvement:** Save by `PR + ledger content hash`, or skip save when the ledger/runtime state is unchanged.
  - **Estimated impact:** Small latency win, moderate cache-churn reduction, low-medium risk.

- **Memory retrieval is not yet helping prompts**
  - **Evidence:** 0/7 memory retrieves hit, all with `keyword_method=none`, and no promote/finalize lifecycle ops were observed.
  - **Improvement:** Verify promotion/finalization first, then tune retrieval. Do not spend time on aggressive memory-context injection until hit rate is above zero.
  - **Estimated impact:** Medium future token/quality upside; low immediate gain.

## Orchestrator Health

- The control plane is **noisy but cheap**: `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` produced 597 of 1,000 runs (59.7%) but only 7,803s total (2.9% of runtime). Recent examples `26206110909`, `26206110922`, `26206110927`, and `26206110923` all skipped in 1-9s based on guard conditions.
- `orchestrate_poll` looks healthy in this window: 21/21 success, average 110.4s, p95 148s.
- The main orchestration defect observed was not a clarify loop or stuck wave; it was the false `plan_failed` classification in `test_and_mark_stable` run `26177257854`.
- `review_autofix` already has continuation and dedupe logic (`review_autofix.yml` lines 5282-5298), but long cancelled runs show the supersession check still happens too late for some branches.
- **Smallest safe mitigations:**
  - Add a pre-`codex-agent` supersession recheck.
  - Track `cancelled_duration_seconds` for `review_autofix`.
  - Track `plan_failed` events that later show a successful/labelled Plan outcome.
  - Track the control-plane skipped-run ratio separately from user-visible failures.
- **Observable indicators to track weekly:** `review_autofix` cancel count and cancel duration, `orchestrate_poll` p95, false `plan_failed` count, forward-merge conflict count, AI memory retrieve hit rate.

## Pipeline Flow Bottlenecks

| Stage | Bottleneck type | Evidence | Priority fix |
|---|---:|---|---|
| Clarify → Plan → Implement → Clarify Respond | Control-plane fanout / UI noise, not compute | 597 runs but only 7,803s total; recent runs mostly 1s skips | Keep guards; don’t over-optimize runtime here |
| Review / Autofix | **Compute** | `review_autofix` = 175,031s, 65.2% of all runtime; `codex-agent` dominates sampled slow runs | Add small-diff middle tier and lower-cost reviewer configuration |
| Review / Autofix | **Retry / supersession waste** | 40 cancelled runs, 29,254s wasted; 12 cancelled after >30m | Revalidate head SHA / newer sibling before `codex-agent` and before continuation dispatch |
| Validate / CI | **Compute + fail-late** | `ci` = 60,338s, 22.5% of runtime; failures `26157790206` and `26202338589` surfaced after 842-895s | Front-load brittle regression tests; only consider parallel job split after queue telemetry is measured |
| Cross-cutting | **Queueing** | 16 of 27 sampled deep-dive runs logged hosted-runner wait messages, including `review_autofix`, `ci`, and utility workflows | Reduce needless starts/cancels before adding more parallelism |
| Stable → Main propagation | **Merge/conflict overhead** | Forward-merge run `26206096243` hit `STATUS="conflict"`, `AHEAD="10"`, and opened fallback PR `#2844`; conflict list included 15 files | Resolve forward-merge conflicts quickly; keep stable/main drift small |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix`: 150 runs, 175,031s total, p95 3,057.2s.
  - `ci`: 64 runs, 60,338s total, p95 1,034.65s.
  - Merge/conflict overhead on `forward_merge_stable_to_main` when `stable` drifts from `main`.

- **Top failure modes**
  - False-negative Plan polling in `test_and_mark_stable` (`26177257854`).
  - Stale regression test after the Plan-poll fix (`26202338589`).
  - Prompt-budget helper numeric-coercion contract failure (`26157790206`).

- **Highest-cost drivers**
  - Full six-model reviewer panel + GPT-5.4 editor in `review_autofix`.
  - Long cancelled `review_autofix` runs.
  - `workflow_log_analysis` summarization spending 263,076 tokens on unselected runs.
  - Repeated Semble overflow lookups on the same files within a run.

- **Top 3 prioritized actions**
  1. Add a middle review tier plus pre-`codex-agent` supersession checks in `review_autofix`.
  2. Update `tests/test_test_and_mark_stable_plan_polling_guard.py`, verify the fixed Plan-poll logic is the active one on all reused refs, and move that test earlier in CI.
  3. Fix AI memory telemetry formatting, emit prompt-cache counters, and don’t invest further in memory-based prompt reduction until retrieval hit rate rises above zero.

## Metrics Appendix

### Run-volume and duration summary

| Scope | Runs | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo overall | 1000 | 375 | 3 | 42 | 580 | 268.3 | 2.0 | 1942.35 |
| review_autofix | 150 | 109 | 0 | 40 | 1 | 1166.9 | 1103.5 | 3057.2 |
| ci | 64 | 62 | 2 | 0 | 0 | 942.8 | 948.5 | 1034.65 |
| control-plane aggregate (`clarify`+`plan`+`implement`+`orchestrate_clarify_respond`) | 597 | 17 | 0 | 2 | 578 | 13.1 | — | — |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 5468.0 | 5468.0 | 5468.0 |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 0 | 5156.0 | 5156.0 | 5156.0 |

### `review_autofix` outcome detail

| Outcome | Runs | Total duration s | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|
| Success | 109 | 144,845 | 1328.9 | 1676.0 | 3201.2 |
| Cancelled | 40 | 29,254 | 731.4 | 24.0 | 2873.4 |

### AI / token metrics

| Metric | Value | Evidence |
|---|---:|---|
| Operational prompt/completion token counters emitted | No | No deep-dive operational `prompt_tokens` / `completion_tokens` / `total_tokens` lines found |
| Prompt-cache counters emitted | No | No operational `cache_creation_input_tokens` / `cache_read_input_tokens` lines found |
| `workflow_log_analysis` summarizer tokens | 263,076 | Run `26177484217`, `analyze-commit-notify`, `summarize_unselected_runs` |
| Unselected runs targeted / summarized | 100 / 96 | Same telemetry event as above |
| Reviewer panel size in sampled `review_autofix` runs | 6 reviewer models + GPT-5.4 editor | Sampled gate/codex logs, e.g. `26154599231` |

### Semble / Serena / MCP telemetry  
_Deduped across `errors/slow/recent`, excluding `workflow_log_analysis` self-analysis logs._

| Server | Queries | Query bytes | Query ms | Fallbacks | Response bytes | Notes |
|---|---:|---:|---:|---:|---:|---|
| Semble | 24 | 215,032 | 11,518 | 15 | n/a | All fallbacks were test-only `missing_semble` fail-open cases |
| Serena | 0 operational | 0 | 0 | 0 operational | 0 | No operational `SERENA_QUERY/FALLBACK/PROBE` lines observed |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | None operationally observed in this window |

### Semble target breakdown

| Semble target | Queries | Bytes | ms |
|---|---:|---:|---:|
| reviewer-context | 7 | 103,584 | 3,444 |
| overflow | 13 | 82,360 | 6,182 |
| conflict-resolver-context | 4 | 29,088 | 1,892 |

### Semble overflow file hotspots

| File | Queries | Bytes |
|---|---:|---:|
| `.github/workflows/test-and-mark-stable.yml` | 5 | 32,388 |
| `scripts/orchestrate_poll_process.sh` | 4 | 22,567 |
| `tests/test_orchestrate_integration_ahead_by_gate.py` | 2 | 13,538 |
| `tests/test_orchestrate_lib.py` | 1 | 7,505 |
| `scripts/review_rb_judge.sh` | 1 | 6,362 |

### AI memory telemetry

| Metric | Value |
|---|---:|
| Structured `AI_MEMORY_TELEMETRY` events | 28 |
| `record-run-event` | 14 |
| `record-candidate` | 7 |
| `retrieve` | 7 |
| Retrieve hit rate | 0 / 7 = 0% |
| Avg `estimated_tokens` on retrieve | 0 |
| `keyword_method=none` | 7 / 7 |
| `enabled:false` retrieves | 0 |
| `fail_open:true` retrieves | 0 |
| `push_attempts > 1` events | 1 (run `26170059805`) |
| Malformed glued telemetry lines | 5 |

### Cache metrics

| Cache/system | Sampled behavior | Result |
|---|---|---|
| Review-ledger GitHub Actions cache | 7 restore attempts across 8 slow `review_autofix` runs | 6 hits, 1 miss, 7 saves |
| Review-ledger restore hit rate | `6 / 7` | 85.7% |
| Review-ledger key shape | `review-ledger-{repo}-pr-{PR}-{run_id}-{run_attempt}` with PR-scoped restore prefix | Works, but fragments per run |
| Prompt cache | No operational counters emitted | Hit rate unknown |

### GH API summary

| Workflow / step | Observed pattern | Approx call shape | Recommendation |
|---|---|---|---|
| `internal-review` / `resolve-claude-branch-pr` | Separate open-PR lookup and default-branch lookup | 2 REST calls per push-triggered resolve step | Collapse to 1 GraphQL query |
| `test_and_mark_stable` / `e2e-smoke-test` (old failing logic) | Repeated Actions-run and issue-label polling | Looping list calls; old path used `per_page=50` | Keep/backport current scoped helper |
| `orchestrate_poll_process.sh` / `_load_actions_runs_cached` | Conditional in-progress fetch, unconditional queued+completed on miss | 1 conditional + 2 follow-up Actions calls on miss | TTL-cache queued/completed or fetch only when diagnostics need them |
| `issue_pr_status` / `sync-status` | Good GraphQL batching | 2 GraphQL queries in sampled run `26206096275` | Keep pattern |

### MCP availability rows

| Server / target | probe_ok | probe_failed | probe_skipped | Explicit disabled runs | Notes |
|---|---:|---:|---:|---:|---|
| Serena / `review-autofix-editor` | 0 | 0 | 0 | 8 | No `SERENA_PROBE` telemetry emitted; 8 sampled slow `review_autofix` runs logged `SERENA_ENABLED:false`, 7 also logged `SERENA_AVAILABLE:false` |

## Deep Audit — Workflows & Scripts (2026-05-21)

### Section 1: Bug & Correctness Sweep

- **BUG-001**
  - **File path** — `.github/workflows/test-and-mark-stable.yml:875-905,3486-3493,3501-3516,3635-3662,3696-3727,3759-3786,3871-3895,4108-4139`
  - **Severity** — High
  - **Category tag** — `bug`
  - **Description** — The release gate still discovers dispatched runs by “latest run with `id > PRE`” and, in the implement waiter, by only the first 50 global runs (`actions/runs?per_page=50` at lines 899-905). The orphan-workflow/orchestrate watchers are even narrower (`per_page=10`) and do not apply any smoke-specific discriminator before accepting `NEW_ID`. That leaves two correctness holes: high run volume can push the intended run out of the page window, and a different run of the same workflow that starts after `PRE` can be mis-associated with the smoke test. The same file already contains a safer scoped pattern in Phase 2 (`fetch_plan_runs_json` / `latest_scoped_run_field` at lines 624-636), but the later watchers do not reuse it.
  - **Recommended fix** — Factor the Phase 2 scoped watcher into a reusable helper and use it for implement + dispatch waits. Query `per_page=100`, constrain by `event=workflow_dispatch` where possible, and add a smoke-only discriminator (reuse an existing unique input like `branch_name`, or add a `dispatch_nonce` input surfaced in the callee workflow `run-name`) before declaring `dispatch did not register` or consuming a run id.

### Section 2: GitHub API Call Redundancy Audit

- **API-001**
  - **File path** — `.github/workflows/issue_pr_status.yml:188-349,503-512`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — The label-sync step already classifies linked issues into `TRACKING_ISSUES` and `MANAGED_ISSUES` with one aliased GraphQL bundle, then the Telegram-alert step re-fetches each linked issue body one-by-one just to rediscover whether any linked issue is orchestrator-managed. **Current call count:** `1` GraphQL classification call + `N` extra `GET /repos/{repo}/issues/{n}` calls for `N` linked issues. **Proposed call count:** `1` GraphQL call + `0` extra issue GETs by exporting the classification result to later steps. **Pattern to extend:** the existing aliased GraphQL bundle here already matches the batching style used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.
  - **Recommended fix** — After computing `TRACKING_ISSUES` / `MANAGED_ISSUES`, write a boolean like `HAS_ORCHESTRATED_LINKED_ISSUE=true` (or persist the lists) into `$GITHUB_ENV`, and have the alert step read that instead of looping over `_safe_gh_jq`.

- **API-002**
  - **File path** — `.github/workflows/test-and-mark-stable.yml:3453-3516,3629-3662,3692-3727,3751-3786,3863-3895,4099-4139`
  - **Severity** — Medium
  - **Category tag** — `api-redundancy`
  - **Description** — Each dispatch watcher first polls `GET /actions/workflows/{wf}/runs` until a new run appears, then switches to `GET /actions/runs/{id}` to read `status` and `conclusion`, even though the workflow-runs list payload already contains those fields. **Current call count per watcher:** `1 + R + P` (`1` baseline `per_page=1` lookup, `R` registration polls, `P` status polls). **Proposed call count per watcher:** `1 + max(R, P)` by reusing one cached `actions/workflows/{wf}/runs?per_page=100` payload for both discovery and status tracking. Across the six watchers here, that is `6 + Σ(R+P)` today versus `6 + Σmax(R,P)` after consolidation. **Pattern to extend:** `_load_actions_runs_cached` in `scripts/orchestrate_poll_process.sh`, or a new cached Actions-run helper in `scripts/gh_helpers.sh`.
  - **Recommended fix** — Add a shared `watch_workflow_dispatch_run` helper that caches one workflow-runs response per poll cycle, extracts `NEW_ID`, `status`, and `conclusion` from the same JSON, and returns once the matched run completes.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**
  - **File path** — `.github/workflows/test-and-mark-stable.yml:475-489,610-622,829-841,1284-1303,1799-1819,2458-2469; .github/workflows/comprehensive-test-and-release.yml:72-98,315-341`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — The release workflows carry seven inline variants of the same GitHub-API retry/backoff helper (`gh_api_safe` / `gh_api_with_retry`). The copies have already drifted: some print stderr on non-rate-limit failures, some silently return empty output, and only one version does bounded multi-attempt retries for 5xx/429 errors. That makes release-gate behavior step-dependent and raises maintenance cost whenever rate-limit handling changes.
  - **Recommended fix** — Move this logic into a shared module, preferably `scripts/gh_helpers.sh`, with a signature like `gh_api_safe_backoff <endpoint> [gh api args...]` plus `watch_workflow_run <workflow_file> <selector> <deadline_secs>`. Update callers in `test-and-mark-stable.yml` and `comprehensive-test-and-release.yml` to source the shared helper instead of redefining local wrappers.

- **DUP-002**
  - **File path** — `.github/workflows/issue_pr_status.yml:47-130; .github/workflows/validation-improvements-intake.yml:54-134; .github/workflows/validate.yml:223-304`
  - **Severity** — Medium
  - **Category tag** — `duplication`
  - **Description** — Three workflows re-implement near-identical support-checkout helpers (`checkout_support_ref` plus `fetch_from_ref_or_local` / `copy_from_ref_or_local`) for staging scripts from `shubhodeep1/coding-workflows`. The bodies differ mostly in manifests and temp-root names, so fallback-policy fixes or copy-semantic changes have to be repeated manually. This duplication also feeds directly into the large interpolated `run:` blocks in `validate.yml`.
  - **Recommended fix** — Extract the checkout/copy logic into a shared script such as `scripts/bootstrap_support_files.sh` with a narrow interface like `bootstrap_support_files.sh --source-repo <owner/repo> --ref <ref> --dest-root <dir> --manifest <manifest.json> [--require-remote] [--allow-main-fallback]`. Update `issue_pr_status.yml`, `validation-improvements-intake.yml`, and `validate.yml` first; `review_autofix.yml` can adopt the same helper later with a richer manifest.

### Section 4: Expression Size Limit Risk Assessment

- **EXPR-001**
  - **File path** — `.github/workflows/review_autofix.yml:928-1205`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — `Stage workflow support files` is approximately **15,479** characters in its interpolated `run:` body, leaving only **5,521** characters of headroom before GitHub’s 21,000-character expression ceiling. This step is already above the 15,000-character warning threshold and continues to grow as new scripts/prompts are staged.
  - **Recommended fix** — Extract the entire staging step to an external script such as `scripts/stage_review_support_files.sh`, or split script/prompt/schema staging into separate steps driven by environment variables.

- **EXPR-002**
  - **File path** — `.github/workflows/review_autofix.yml:1497-1886`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — `Collect PR metadata` is approximately **17,408** characters, leaving only **3,592** characters of headroom. It combines retry helpers, GraphQL fallback logic, linked-issue context assembly, comment collation, and diff collection in one interpolated block, so even a small future addition can cross the hard limit.
  - **Recommended fix** — Extract this step into `scripts/collect_review_pr_metadata.sh`, or split it into separate steps for PR payload/comments, linked-issue context, and diff/check-run collection.

- **EXPR-003**
  - **File path** — `.github/workflows/validate.yml:210-583`
  - **Severity** — Medium
  - **Category tag** — `expression-limit`
  - **Description** — `Fetch workflow support files` is approximately **17,416** characters, leaving only **3,584** characters of headroom. It is both large and frequently edited, making it one of the likeliest places to re-hit the 21,000-character ceiling during future support-asset rollouts.
  - **Recommended fix** — Move the bootstrap logic into a shared script (`scripts/bootstrap_support_files.sh`) and keep the workflow step to environment setup plus a single script invocation.

No workflow file exceeded the 800 KB pre-warning threshold in this pass; the largest is `review_autofix.yml` at **359,044** bytes. No `if:` expression was large enough to flag.

### Section 5: Cross-Cutting Concerns

- **DEAD-001**
  - **File path** — `scripts/orchestrate_lib.py:1096-1369`
  - **Severity** — Low
  - **Category tag** — `dead-code`
  - **Description** — `evaluate_phase_failure_resume()` and `resolve_label_repair_evidence()` implement a contradiction-evidence path that a repo-wide search does not find any workflow/script caller for, and the current branch docs explicitly describe that path as “not yet wired” (`README.md:1137`, `agents.md:211-212`). This leaves a sizable decision stack inactive and at risk of drifting from the live reconciliation code in `scripts/orchestrate_poll_process.sh`.
  - **Recommended fix** — Either wire these helpers into the poller’s active label-repair / phase-failure reconciliation path and add regression tests around the real caller, or remove the reserved implementation until rollout resumes.

- **SHELL-001**
  - **File path** — `scripts/validate_changed_files_syntax.sh:70-75`
  - **Severity** — Low
  - **Category tag** — `shellcheck`
  - **Description** — The denylist case arm ends with `*,*.envrc|*,.env*`, but `*.env*` already appears earlier in the same alternation. ShellCheck flags this as SC2221/SC2222 because the later patterns can never match. Runtime behavior is currently benign, but the unreachable arms make the redaction policy harder to review.
  - **Recommended fix** — Remove the redundant `*,*.envrc|*,.env*` suffixes, or narrow the earlier `*.env*` pattern if `.envrc` genuinely needs special handling.

- **SHELL-002**
  - **File path** — `scripts/review_commit_changes.sh:489-489; scripts/review_conflict_resolve.sh:1522-1523`
  - **Severity** — Low
  - **Category tag** — `shellcheck`
  - **Description** — Both helper scripts pass a token-bearing remote URL to `git remote set-url` without quoting `${GH_PAT}` / `${GITHUB_REPOSITORY}`. ShellCheck flags SC2086 here. Today’s token/repo formats make breakage unlikely, but quoting is the safer contract for secret-bearing command arguments and avoids accidental shell expansion if the token alphabet changes.
  - **Recommended fix** — Quote the full URL (or each expansion) in both helpers, e.g. `git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"`.

No `TODO` / `FIXME` / `HACK` markers were present under `.github/workflows` or `scripts` in this pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 7 | API-001, API-002, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-003 |
| Low | 3 | DEAD-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1-7 workflows | Medium |
| API call optimization | 2 workflows + 1 helper module | Medium |
| Code modularization | 5 workflows + 1-2 helper scripts | Large |
| Expression size reduction | 2 workflows + 2 helper scripts | Medium |
| Medium/Low fixes | 4 scripts + 1 library module | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-21)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is statically proven safe to collapse without changing retry/error/concurrency behavior; `NEEDS_VERIFICATION` means the overlap is real but a human must confirm semantics before changing it; `RISKY_SKIP` means the duplication is in a retry/poll/race-defense path and must not be auto-implemented even if it looks mergeable.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — RISKY_SKIP
- **Files** — `.github/workflows/test-and-mark-stable.yml:2869-2878`
- **Current / proposed call count** — `2 -> 1` per poll iteration in the pre-existing `cancel-on-close` run wait loop.
- **Endpoint(s)** — `GET /repos/{repo}/actions/runs/{run_id}`
- **Evidence**
```bash
while [ "${EXISTING_STATUS}" != "completed" ] && [ "$(date +%s)" -lt "${WAIT_DEADLINE}" ]; do
  sleep 5
  EXISTING_STATUS=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
    --jq '.status // ""' 2>/dev/null || echo "")
  EXISTING_CONCLUSION=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
    --jq '.conclusion // ""' 2>/dev/null || echo "")
done
```
- **Proposed fix** — In `verify-cancel-on-close`, fetch the run JSON once per loop iteration, then derive both `.status` and `.conclusion` locally with `jq`.
- **Safety rationale** — This sits inside a bounded poll loop that defends against event-propagation races, so it matches the audit’s `RISKY_SKIP` trigger even though the endpoint overlap is obvious.
- **Downstream signal** — Do not auto-implement; manually test both `verify-cancel-on-close` branches (already-closed PR and close-during-test PR) and diff timeout/progress logging before changing the polling shape.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — NEEDS_VERIFICATION
- **Files** — `.github/workflows/internal-review.yml:98-101`
- **Current / proposed call count** — `2 -> 1` on the `push`-only `resolve-claude-branch-pr` step.
- **Endpoint(s)** — `GET /repos/{repo}/pulls?state=open&head={owner}:{ref}`; `GET /repos/{repo}`
- **Evidence**
```bash
existing_pr="$(gh api \
  "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
  --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
```
This step is gated to `push` at `.github/workflows/internal-review.yml:76-76`.
- **Proposed fix** — Drop the `GET /repos/{repo}` call and source `base_ref` from the push payload (`github.event.repository.default_branch`, with the same `'main'` fallback).
- **Safety rationale** — The reuse is local and non-concurrent, but it replaces a live repo read with event payload data, so payload availability/freshness needs one verification pass first.
- **Downstream signal** — On a real `claude/**` push, log both `github.event.repository.default_branch` and `gh api "repos/${REPOSITORY}" --jq '.default_branch'`; only remove the API call if they match and the step remains `push`-only.

#### REUSE-002 — NEEDS_VERIFICATION
- **Files** — `.github/workflows/implement.yml:72-82`, `.github/workflows/implement.yml:340-406`, `.github/workflows/implement.yml:1046-1057`
- **Current / proposed call count** — Happy path: `2 -> 1` mandatory `GET /repos/{repo}/issues/{issue_number}` calls; line `1056` is already a cache-miss fallback and can stay fallback-only.
- **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
```bash
ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" \
  --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
```

```bash
if ! issue_meta_json="$(gh_api_with_retry gh api \
  "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"; then
  ...
fi
printf '%s\n' "${issue_meta_json}" > "${ISSUE_META_FILE}"
```

```bash
if [ ! -s "${ISSUE_META_FILE}" ]; then
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
fi
```
- **Proposed fix** — Stage the first issue fetch into a temp/runtime JSON file, parse state/labels from that same payload in `Guard against closed or already-implementing issue`, and let `Resolve checkout ref` plus `Fetch issue metadata` reuse it.
- **Safety rationale** — The overlap is real, but the later checkout step has bespoke retry/fail-open behavior, so collapsing to a shared cache needs verification that fallback semantics stay identical.
- **Downstream signal** — Validate both a normal open-issue run and a forced `Resolve checkout ref` API/JSON-parse failure; confirm the checkout step still falls back to `${{ github.event.repository.default_branch }}` and `Fetch issue metadata` still re-fetches on cache miss.

#### REUSE-003 — NEEDS_VERIFICATION
- **Files** — `.github/workflows/orchestrate_clarify_respond.yml:62-88`, `.github/workflows/orchestrate_clarify_respond.yml:394-425`
- **Current / proposed call count** — On the orchestrator-managed path: `2 -> 1` child-issue GETs, plus `0-2 -> 0-1` tracking-issue GETs depending on whether `TRACKING_NUM` is present.
- **Endpoint(s)** — `GET /repos/{repo}/issues/{ISSUE_NUMBER}`; `GET /repos/{repo}/issues/{TRACKING_NUM}`
- **Evidence**
```bash
ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
...
TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" \
  --jq '.title // ""' 2>/dev/null || echo "")"
```

```bash
ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
...
TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" \
  --jq '.body // ""')"
```
- **Proposed fix** — Cache the child-issue payload (and, when present, the tracking-issue payload) from `Check orchestrator metadata` into `$RUNNER_TEMP` or an equivalent per-job file, then have `Fetch issue and tracking context` consume the cached JSON instead of re-fetching both issues.
- **Safety rationale** — This spans steps and transports full issue bodies, so output/file-size assumptions and “fresh enough after trigger” behavior need verification before collapsing the calls.
- **Downstream signal** — Verify on a real orchestrator-managed comment event that the chosen cache transport survives across steps, handles large issue bodies, and still behaves acceptably if the issue body is edited after queueing but before the job runs.

#### REUSE-004 — NEEDS_VERIFICATION
- **Files** — `scripts/review_rb_judge.sh:353-370`, `scripts/review_rb_judge.sh:378-389`
- **Current / proposed call count** — `2 -> 1` on the `closingIssuesReferences`-empty fallback path.
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr_number}`; `GraphQL repository.pullRequest(number){ closingIssuesReferences }`
- **Evidence**
```bash
_pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
  2>/dev/null || echo '{}')"
...
unset _pr_meta _pr_state _pr_merged
...
if [ -z "${ISSUE_NUMBERS}" ]; then
  PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    --jq '.title + " " + (.body // "")' || echo "")"
fi
```
- **Proposed fix** — Retain `_pr_meta` (or derive `PR_DATA` from it before `unset`) and reuse that cached title/body for the grep fallback; only issue a second `GET /pulls/{n}` if the first fetch failed.
- **Safety rationale** — The duplicate GETs are adjacent and unmutated, but the first call is retried and the second is single-attempt, so merging them changes second-chance semantics unless the failure path is preserved explicitly.
- **Downstream signal** — Test two cases with `closingIssuesReferences` empty: initial PR GET succeeds, and initial PR GET fails; confirm the merged-PR guard, issue-number fallback, and log output stay equivalent before removing the second GET.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — agreed; the later Telegram alert path should reuse the earlier issue classification, but exporting that state across steps still needs fail-open verification.
- API-002: RISKY_SKIP — agreed; those watcher loops are explicit dispatch-race defenses, so consolidation must stay manual-only.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 4 | REUSE-001, REUSE-002, REUSE-003, REUSE-004 |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
