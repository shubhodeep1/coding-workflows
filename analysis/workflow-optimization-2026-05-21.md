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
