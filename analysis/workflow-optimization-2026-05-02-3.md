## Executive Summary

- **The largest end-to-end latency loss is in `test_and_mark_stable` failure handling, not raw model time.** Run `25237291900` failed after **5,465s** at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`, while the downstream `implement` failures that likely caused it were only **179–240s** long and failed at `Run Codex implementation`. The waiter is burning most of the hour by timing out instead of terminating on downstream failure. **Estimated impact:** save **60–88 minutes** per failed stable-release run. **Confidence:** high.

- **`review_autofix` is over-spending on comment-only / non-merge paths.** Cancelled runs `25244790668` (**734s**) and `25244601237` (**707s**) still ran the reviewer panel with `MODEL_EDITOR=openai/gpt-5.3-codex`, six reviewer models, and `xhigh` reasoning even though logs say `editor/commit/judge/auto-merge skipped` and the path was `comment-only`. **Estimated impact:** save **8–12 minutes and substantial multi-model tokens per affected run**. **Confidence:** high.

- **Implement reliability is concentrated in one repeatable failure mode: “announced edit, no diff.”** Failed implement run `25244127789` spent two attempts, used **4,439 + 4,334 = 8,773 tokens**, and ended with `Codex produced no actionable output 2 attempts in a row` / `agent loop stuck in exploration`. This same failure point appears repeatedly across `implement` failures, where the family failure rate is **5.46% (10/183)**. **Estimated impact:** cut implement reruns materially and remove a major source of downstream timeouts. **Confidence:** high.

- **GH API hotspots are concentrated and fixable with batching/cycle-local caching.** In the deep-dive sample, `workflow_log_analysis` logged **779** `gh api` calls, `implement` **612**, `test_and_mark_stable` **578**, and `review_autofix` **560`. Existing analysis logs already identify safe reductions such as review wait-loop caching (**~6 calls/iteration → ~3**) and batching linked-issue label/body fetches. **Estimated impact:** **40–60% API-call reduction** in hotspot loops, plus lower rate-limit risk. **Confidence:** high.

- **Serena usage is strong in `implement`, but observability is inconsistent in `review`.** Failed implement runs `25244127789` and `25244121942` show **269 Serena tool calls**, only **14–16 file-based fallback ops**, and estimated savings of **~154,750 tokens (85–86%)** versus file-heavy operation. But review run `25244790668` logged `No Serena tool usage stats found.` **Estimated impact:** preserve current token savings and improve diagnosability by fixing stats emission and enabling Git MCP where safe. **Confidence:** medium-high.

- **AI memory is useful in `implement` but weak in `review`.** Across observed JSON telemetry, `retrieve` hit rate was **80% (16/20)** with average `estimated_tokens=37.8`; however, reviewer retrievals in `25237552686`, `25215784558`, and `25244790668` returned **0 records** with `keyword_method: none`. **Estimated impact:** modest latency/token gains, better reviewer consistency. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fail fast in stable-release E2E when downstream implement fails
- **Evidence:**  
  - `test_and_mark_stable` run `25237291900` failed after **5,465s** at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`.  
  - In `step-009-e2e-alt-model-test.log`, the loop polls labels every **20s**, reaches `ai:awaiting-approval`, then `ai:implementing`, and finally exits with `::error::Alt-model run timed out before reaching review stage`.  
  - Separate `implement` failures in the same telemetry window (`25244127789`, `25224008847`, `25237418726`, etc.) fail in **179–240s** at `Run Codex implementation`.
- **Root cause:** The E2E waiter is keyed mainly to label progression / review-stage appearance, not to definitive downstream failure signals.
- **Exact change:**  
  - In the E2E wait step, watch the spawned `implement` run conclusion and/or explicit failure labels/comments/artifacts.  
  - Abort immediately when `implement` reaches `failure`, or when `ai:implement-failed` / Codex failure diagnostics are posted, instead of waiting for review-stage emergence.
- **Estimated time savings:** **60–88 minutes per failed `test_and_mark_stable` run**.
- **Implementation risk:** **Low.** This is a fail-faster change; success path behavior stays unchanged.
- **Priority:** **Critical-path win.**

### 2. Short-circuit `review_autofix` comment-only paths before multi-model review fan-out
- **Evidence:**  
  - Cancelled runs `25244790668` (**734s**) and `25244601237` (**707s**) both logged `running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`  
  - Recent successful run `25244990766` also shows six `REVIEWER_MODELS`, `MODEL_EDITOR: openai/gpt-5.3-codex`, and `xhigh` reasoning in a path that mostly dispatches/edits post-merge metadata.
- **Root cause:** The gate decides the run is informational/comment-only, but the expensive reviewer panel still executes.
- **Exact change:**  
  - Add a gate branch: if `comment-only` or `claude-branch-review` with no editor/judge/automerge path, use either:
    1. a single fast reviewer, or  
    2. a deterministic rules-only comment path.  
  - Skip the full multi-model reviewer panel unless the result can change merge/editor behavior.
- **Estimated time savings:** **8–12 minutes per affected run**; additional queue relief from fewer long-lived reviewers.
- **Implementation risk:** **Medium.** Slight reduction in informational review depth; no merge-safety change if only applied to comment-only paths.
- **Priority:** **Critical-path win** for review-heavy PR flows.

### 3. Suppress non-command `issue_comment` fan-out to reduce queue pressure
- **Evidence:**  
  - Around `2026-05-02T04:51–04:54Z`, many `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` runs completed in **0–2s** because comment bodies like `<!-- tg_cleanup:... -->`, failure notifications, or wave summaries did not match `/answer`, `/approved`, or `/reclarify`.  
  - Families affected include `clarify` (**215 runs**, p50 **1s**) and `plan` (**183 runs**, p50 **1s**), with most runs falling into `other/skipped`.
- **Root cause:** Broad workflow triggers are creating many no-op workflow invocations that still consume orchestration, runner scheduling, and GitHub Actions control-plane capacity.
- **Exact change:**  
  - Tighten top-level `if:` / event filters so only command-prefixed comments dispatch these workflows.  
  - Prefer one shared comment router over separate `clarify`/`plan`/`implement`/`respond` no-op runs.
- **Estimated time savings:** Small per run, but meaningful cumulative queue reduction across hundreds of skipped runs.
- **Implementation risk:** **Low** if implemented as a stricter prefix filter with existing fall-open behavior preserved.
- **Priority:** **System-level throughput win**, not a single-run critical-path win.

### 4. Stop full-ref fetches in pollers that only need branch state
- **Evidence:**  
  - `orchestrate_poll` run `25244636945` spent about **9s** in `poll/Checkout repository`.  
  - `step-017-poll_poll_Checkout_repository.log` shows `fetch-depth: 0` and a full fetch of `+refs/heads/*` and `+refs/tags/*`, followed by hundreds of `* [new tag] ...` lines.
- **Root cause:** Poll jobs fetch full history and all tags/branches even when they only need current orchestration state.
- **Exact change:**  
  - For poll-only jobs, use shallow branch fetch and `fetch-tags: false` unless a step explicitly needs tags/history.  
  - Split “version-aware” and “poll-only” checkout profiles.
- **Estimated time savings:** **5–10s per poll run**.
- **Implementation risk:** **Low**, as long as version/tag-dependent jobs keep full-history checkout.
- **Priority:** **Local micro-optimization**.

### 5. Reduce runner-allocation fan-out inside `test_and_mark_stable`
- **Evidence:**  
  - Failed stable-release runs show many sub-jobs (`resolve-version`, `e2e-smoke-test`, `orchestrate-decompose-test`, `validate-standalone-test`, `orphan-workflows-test`, etc.), and their corresponding `*_system.log` files repeatedly show `Waiting for a runner to pick up this job...` / `Job is waiting for a hosted runner to come online.`  
  - Family stats: `test_and_mark_stable` average **4,224.2s**, p50 **4,758s**, p95 **5,580.2s**.
- **Root cause:** Sequential/loosely-coupled sub-jobs each pay runner queue/startup overhead.
- **Exact change:**  
  - Merge ultra-short sequential setup/verification jobs where outputs are local and no cross-runner isolation is needed.  
  - Keep true parallel test branches separate; collapse orchestration-only glue jobs.
- **Estimated time savings:** **1–5 minutes per stable-release run**, depending on queue conditions.
- **Implementation risk:** **Medium**, because job boundaries may encode artifact/output contracts.
- **Priority:** **Secondary critical-path win**.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Downshift reviewer model count and reasoning on comment-only review paths
- **Evidence:**  
  - `review_autofix` runs `25244790668` and `25244601237` were comment-only / non-editor paths but still configured:  
    - `MODEL_EDITOR: openai/gpt-5.3-codex`  
    - `REVIEWER_MODELS: minimax/minimax-m2.5 moonshotai/kimi-k2.5 deepseek/deepseek-v4-pro z-ai/glm-5 qwen/qwen3.6-plus x-ai/grok-4.1-fast`  
    - `REVIEWER_REASONING_EFFORT: xhigh` and `EDITOR_REASONING_EFFORT: xhigh`
  - Family stats: `review_autofix` has **31 cancelled runs out of 60**.
- **Root cause:** High-cost review configuration is applied even when no code-edit or merge decision can occur.
- **Exact change:**  
  - Introduce a low-cost mode for `comment-only`, `PR closed`, and `claude-branch-review` informational paths:
    - one fast reviewer or deterministic rules,
    - `medium` or `low` reasoning,
    - no editor model unless a patch is possible.
- **Estimated savings:** Largest model-cost reduction in the current window; likely the majority of review-token spend on cancelled/informational runs.
- **Quality-risk notes:** **Low-medium.** Apply only where the result cannot change merge/editor behavior.

### 2. Use low/medium reasoning for deterministic smoke/canary implement tasks
- **Evidence:**  
  - Failed implement run `25244127789` used **8,773 tokens** across two attempts (`4,439`, `4,334`) and never produced a diff.  
  - The run script itself contains comments that `xhigh` is unnecessary for the smoke fixture and that over-reasoning causes trivial overwrite failures.  
  - Yet the run still logged `MODEL_REASONING_EFFORT: xhigh`.
- **Root cause:** Deterministic file-overwrite tasks are paying for deep reasoning and exploration behavior.
- **Exact change:**  
  - For E2E/smoke issues identified by fixture path/title, force:
    - one fast editor model,
    - `low` or `medium` reasoning,
    - a deterministic patch-first prompt variant.
- **Estimated savings:** **8k–20k tokens per failed smoke-task implement**, plus secondary savings from avoided `test_and_mark_stable` timeout cascades.
- **Quality-risk notes:** **Low.** These tasks are intentionally narrow and test-oriented.

### 3. Stop avoidable reruns caused by “announced edit, no diff” loops
- **Evidence:**  
  - `25244127789` stopped after two no-diff attempts; follow-on diagnostics in the provided telemetry echoed the same failure mode to later `clarify`/`respond` runs.  
  - `implement` family failure rate is **5.46%**.
- **Root cause:** The pipeline retries even when the model behavior pattern already indicates a low-probability recovery path.
- **Exact change:**  
  - After the first no-diff “announced edit” on deterministic issues, switch to a stricter repair prompt or scripted patch fallback instead of running another equivalent attempt.  
  - Keep the current 2-attempt bail for general issues if needed.
- **Estimated savings:** Moderate per incident; high aggregate savings because failed implement runs also trigger follow-on orchestration noise and human reruns.
- **Quality-risk notes:** **Low** if scoped to deterministic/smoke issues first.

### 4. Remove token-free but expensive no-op workflow invocations
- **Evidence:**  
  - Large volumes of skipped `clarify`/`plan`/`implement`/`respond` runs are created by non-command comments and cleanup comments.
- **Root cause:** Trigger fan-out is too broad.
- **Exact change:** Filter comment triggers earlier.
- **Estimated savings:** Mainly runner/control-plane cost; small direct token savings because most skip before model invocation.
- **Quality-risk notes:** **Low.**

### 5. Stabilize prompt prefixes to improve prompt-cache reuse
- **Evidence:**  
  - Implement/review logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED: false` and `## OpenRouter Prompt Cache Instrumentation`, so cache machinery is active.  
  - However, the sampled deep-dive logs do **not** expose cache creation/read token counters, so actual hit rate is not measurable here.  
  - Implement retries prepend dynamic diagnostics, attempt recaps, and run-specific metadata before model invocation.
- **Root cause:** Dynamic, run-specific noise likely fragments cache keys and reduces reuse of shared policy/system prefixes.
- **Exact change:**  
  - Keep the longest stable system/policy prefix fixed.  
  - Move highly dynamic failure diagnostics, attempt tables, issue/run IDs, and transient telemetry to the end of the prompt or to referenced files.
- **Estimated savings:** Likely modest-to-medium token and latency savings on retries and similar issue classes.
- **Quality-risk notes:** **Low**, but verify with emitted cache-read/create counters after rollout.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Add deterministic fallback after first no-diff implement attempt on smoke/e2e tasks
- **Failure evidence:**  
  - `implement` failures `25244127789`, `25224008847`, `25237418726`, `25215763575`, `25244121942`, `25243564804`, `25243569299`, `25237690797`, `25237704374`, `25224028373` all fail at `Run Codex implementation`.  
  - `25244127789` shows:
    - attempt 1: `4,439` tokens, announced edit, no file changes  
    - attempt 2: `4,334` tokens, announced edit, no file changes  
    - final error: `agent loop stuck in exploration`
- **Root cause category:** Model/task mismatch; retry policy still too generous for deterministic fixture edits.
- **Exact fix:**  
  - On smoke/e2e issues, after the first announced-edit/no-diff event:
    - swap to a patch-only repair prompt, or
    - apply a scripted file rewrite fallback for the target canary file.
- **Expected reliability impact:** Should reduce the dominant repeated implement failure mode and downstream timeout cascades.
- **Rollback/fail-open:** Keep fallback scoped to known deterministic fixtures; default to existing behavior for general issues.

### 2. Make E2E waiters terminate on explicit downstream failure, not only missing progress
- **Failure evidence:**  
  - `25237291900` timed out waiting for alt-model progress.  
  - Separate implement runs in the same window fail much earlier.
- **Root cause category:** Orchestration/watcher logic.
- **Exact fix:**  
  - Watch downstream run conclusion, failure labels, and failure comments/artifacts.  
  - Emit a terminal failed state to the stable-release workflow as soon as the child run is known-bad.
- **Expected reliability impact:** Lowers false “timeout” failures, improves triage quality, and reduces manual reruns.
- **Rollback/fail-open:** If child-run data is unavailable, fall back to current timeout behavior.

### 3. Preserve Serena and memory observability through cancellation/cleanup
- **Failure evidence:**  
  - Review run `25244588726` logged `::warning::memory helper script missing; skipping run-end failure event`.  
  - Review run `25244790668` logged `No Serena tool usage stats found.`  
  - The same run summary notes cleanup removed `.serena` and related support files.
- **Root cause category:** Cleanup/teardown ordering.
- **Exact fix:**  
  - Move stats emission and run-end memory event recording earlier than cleanup.  
  - Keep cleanup fail-open.
- **Expected reliability impact:** Better postmortems, less blind debugging on cancelled/failed review runs.
- **Rollback/fail-open:** If stats generation fails, keep current behavior and warn.

### 4. Fix editor-bait reliability before using it as a hard stable gate
- **Failure evidence:**  
  - Stable-release run `25215477856` failed at `Phase 4b: Verify editor removed bait line`.  
  - `step-006-e2e-smoke-test.log` shows the canary still contained:
    - `status: ok`
    - `run_id: 25215477856`
    - `updated-by: ai-pipeline`
    - `# E2E_EDITOR_BAIT_25215477856: this line should be removed by the editor (smoke gate)`
- **Root cause category:** Review/edit behavior mismatch in the editor-bait exercise.
- **Exact fix:**  
  - Before failing the whole stable-release run, add a narrow fallback:
    - confirm whether the review path intentionally skipped editability,
    - distinguish “reviewer chose comment-only/no-fix” from “editor attempted and failed”.
  - Only keep hard failure for the second case.
- **Expected reliability impact:** Fewer false-negative stable-release failures.
- **Rollback/fail-open:** Keep the current hard gate if the fallback signal is ambiguous.

### 5. Triage and stabilize nightly validation self-test fixtures before trusting status rollups
- **Failure evidence:**  
  - `nightly_validation_selftest` run `25242537588` reported `fixtures=3 passed=1 failed=2` and `overall_status=fail`.
- **Root cause category:** Test fixture instability or new regression in self-test matrix.
- **Exact fix:**  
  - Inspect uploaded artifact `validation-selftest-25242537588-1` to identify the two failing fixtures.  
  - Fix or quarantine those fixtures before using the status trend operationally.
- **Expected reliability impact:** Prevents noisy nightly failures and misleading validation health signals.
- **Rollback/fail-open:** Keep artifact upload and summary generation even if fixture fixes take time.

## AI Memory Health

- **Observed telemetry:** Present in `implement` and `review` deep-dive logs; absent in sampled `ci` runs and many skip-only runs.
- **Retrieve hit rate:** **80.0%** (`16/20` retrieves returned `records_selected > 0`).
- **Average `estimated_tokens` vs budget:** Average **37.8** estimated tokens per retrieve; observed range **0–56**.
- **`keyword_method` distribution:**  
  - `plain`: **16**  
  - `none`: **4**  
  - `llm`: **0 observed**
- **Zero-record retrieves flagged:**  
  - `review_autofix` run `25237552686`, reviewer retrieve: `records_selected: 0`, `keyword_method: none`  
  - `review_autofix` run `25215784558`, reviewer retrieve: `records_selected: 0`, `keyword_method: none`  
  - `review_autofix` run `25244790668`, reviewer retrieve: `records_selected: 0`, `keyword_method: none`
- **`fail_open: true` entries:** **0 observed**
- **`enabled: false` entries:** **0 observed**
- **High push retry counts:** **6** telemetry events had `push_attempts > 1`; max observed `push_attempts` was **2**.
- **Interpretation:**  
  - Memory is healthy in `implement`: retrievals are usually small and usually hit.  
  - Reviewer memory is not contributing yet; all observed reviewer retrieves missed with `keyword_method: none`.
- **Recommendation:**  
  - Improve reviewer retrieval keys/search terms so review jobs stop defaulting to empty `none` retrievals.  
  - Emit memory telemetry consistently in more workflow families so health can be measured beyond implement/review.

## GH API Call Audit

### High-volume patterns

| Workflow family | Observed `gh api` calls in deep-dive sample | Main hotspot |
|---|---:|---|
| `workflow_log_analysis` | 779 | `step-001-api-redundancy.log` |
| `implement` | 612 | `step-001-implement_implement.log` |
| `test_and_mark_stable` | 578 | E2E polling / verification steps |
| `review_autofix` | 560 | `step-001-review_codex-agent.log` |

### 1. Cache review wait-loop data per iteration
- **Evidence:** `workflow_log_analysis` run `25237305050` explicitly documents a review wait loop doing **~6 API calls per active iteration**, with a recommended reduction to **~3** by reusing run/job/log data.
- **Root cause:** Re-fetching overlapping run, job, log, PR, and comment data inside the same polling cycle.
- **Exact change:**  
  - Cache the log blob once per iteration.  
  - Reuse the same `runs` / `jobs` payload for grep + byte-count checks.  
  - Refresh PR metadata/comment count only on state change or a lower-frequency cadence.
- **Estimated call reduction:** About **50% per active iteration**.
- **Rate-limit risk reduction:** High, because this is a repeated polling loop.
- **API hygiene alignment:** Matches the repo’s existing `actions_runs_cache.v1` / cycle-local cache guidance.

### 2. Batch linked-issue label fetches in post-merge validate dispatch
- **Evidence:** `workflow_log_analysis` run `25237305050` identifies a path that currently does **2 + N** discovery calls and can be reduced to **3 total** via one batched GraphQL label fetch.
- **Root cause:** Per-issue label lookups in a loop after fallback issue-number extraction.
- **Exact change:** Use one GraphQL alias query to fetch labels for all candidate issues, then consume the local map in-loop.
- **Estimated call reduction:** From **2 + N** to **3** on affected paths.
- **Rate-limit risk reduction:** Medium-high on large linked-issue PRs.
- **API hygiene alignment:** Directly extends the repo’s existing GraphQL batch helper pattern.

### 3. Batch linked-issue body retrieval in review-blocked judge paths
- **Evidence:** The same API-redundancy pass identifies a `1 + N` issue-body pattern that can be reduced to a single alias-based GraphQL query.
- **Root cause:** One REST fetch per linked issue to get `.body`.
- **Exact change:** Replace per-issue GETs with one GraphQL alias query returning `{ number, body }` for all linked issues.
- **Estimated call reduction:** From **1 + N** to **1**.
- **Rate-limit risk reduction:** Medium.
- **API hygiene alignment:** Satisfies mandatory batching for per-item loops.

### 4. Reduce duplicate Actions list calls in `cancel_on_pr_close`
- **Evidence:** Run `25244990735` calls:
  - `/rate_limit`
  - `GET repos/.../actions/runs` for queued runs
  - `GET repos/.../actions/runs` for in-progress runs
  - per-run cancel endpoint if needed
- **Root cause:** Defensive duplication plus a proactive rate-limit call on every run.
- **Exact change:**  
  - Query workflow runs once, filter queued/in-progress client-side.  
  - Only call `/rate_limit` after an actual retryable failure, not pre-emptively on the success path.
- **Estimated call reduction:** **2–3 calls → 1 call** on no-op runs like `25244990735`.
- **Rate-limit risk reduction:** Small per run, meaningful because this workflow is frequent and highly repetitive.
- **API hygiene alignment:** Preserves fail-open behavior while reducing redundant control-plane traffic.

### 5. Reduce pre-anchor workflow-run lookups in alt-model E2E setup
- **Evidence:** `step-009-e2e-alt-model-test.log` loops over `clarify`, `plan`, `implement`, and `review_autofix`, calling `actions/workflows/{wf}.yml/runs?per_page=1` once for each before issue creation.
- **Root cause:** Four separate latest-run lookups for anchor IDs.
- **Exact change:** Batch these with a single GraphQL or broader Actions query if practical; otherwise cache once per E2E run and reuse across phases.
- **Estimated call reduction:** Small per run (**4 → 1–2**).
- **Rate-limit risk reduction:** Low-medium.
- **API hygiene alignment:** Good candidate, but lower priority than loop batching.

## MCP & Serena Efficiency

- **Strong point:** `implement` runs are already using Serena effectively.  
  - Run `25244127789`:  
    - **269 Serena tool calls**  
    - **16 file-based fallback ops**  
    - **94% Serena efficiency**  
    - estimated tokens **~26,350 with Serena** vs **~181,100 without**  
    - estimated savings **~154,750 tokens (85%)**
  - Run `25244121942` is nearly identical, with **95% efficiency** and **~154,750 token savings (86%)**.
- **Tool-use quality:** Top tools were exactly the right semantic ones: `replace_symbol_body`, `insert_after_symbol`, `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`.
- **Gap:** `review` observability is weaker. In recent review run `25244790668`, `step-038-review_codex-agent_claude-branch-review_Log_token_usage_and_Serena_stats.log` ended with `No Serena tool usage stats found.`
- **Observed inefficiency:** Implement setup logs show `GIT_MCP_DISABLED: true`, so review/edit flows are not yet benefiting from targeted Git MCP context even though that is the intended optimized path.
- **Concrete recommendations:**
  1. **Preserve Serena stats through teardown** so review cancellations still emit usage metrics.
  2. **Enable Git MCP fail-open in review/edit paths** to replace broad git context with targeted `status/diff/show/log/branch` queries.
  3. **Parallelize independent read-only symbol lookups** after project activation—for example, file structure lookup plus reference discovery on different files—where correctness does not depend on sequencing.
  4. **Keep the current Serena-first policy** in implement; it is working and should not be relaxed.
- **Expected impact:** Better turnaround and token efficiency in review flows; minimal risk because the repo already hardens Serena as `required=false`.

## Prompt Cache & Memory System

- **Prompt cache state:**  
  - Many implement/review logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, so prompt caching is generally enabled in the sampled runs.  
  - Repeated `## OpenRouter Prompt Cache Instrumentation` sections are present in failed implement runs.
- **Gap:** The sampled logs do **not** expose concrete prompt-cache read/create token counters, so cache hit/miss effectiveness cannot be quantified from this window.
- **Non-prompt cache behavior:** UV/setup caches are healthy. Recent plan/clarify runs (`25244144177`, `25244143994`, `25244128715`, `25244128476`) show cache hits for the `setup-uv` key and no save needed.
- **Likely cache-fragmentation causes:**  
  - dynamic attempt recaps,  
  - per-run issue/run IDs,  
  - injected diagnostics tables,  
  - failure-specific telemetry added before invocation.
- **Concrete recommendations:**
  1. **Stabilize prompt prefixes:** keep policy/system instructions fixed; move transient diagnostics to the end.
  2. **Emit cache-read/cache-create token metrics** in logs so real hit rate can be measured.
  3. **Reuse deterministic prompt templates for smoke/e2e tasks** instead of assembling bespoke prompt variants on each retry.
  4. **Keep fail-open semantics** exactly as-is; don’t block runs on cache instrumentation failure.
- **Estimated impact:** Modest-to-medium token and latency reduction on retries and repeated issue classes; confidence improves once read/create counters are logged.

## Orchestrator Health

- **Overall health:** Core orchestrator families are stable in this window:
  - `orchestrate`: **5/5 success**
  - `orchestrate_poll`: **33/33 success**
  - `orchestrate_clarify_respond`: no failures, but many skips
- **Observed pain points:**
  1. **Command-routing noise:** many `clarify` / `plan` / `respond` / `implement` runs are dispatched on comments that are clearly not actionable commands.
  2. **Failure handoff lag:** successful plan runs such as `25244144177` and `25244143994` still logged `PLAN_FAILURE_CONTEXT: Planning run did not complete.`, showing orchestration is carrying failure context forward correctly, but later comment-triggered workflows still fan out around those failures.
  3. **Review-path cancellations:** comment-only review flows stay alive long after the gate decides there will be no edit/judge/automerge action.
- **Smallest safe mitigations:**
  - Tighten command routing at trigger time.
  - End comment-only review flows immediately after posting the intended comment or status.
  - Promote child-run terminal-state awareness into parent orchestration loops.
- **Track these indicators:**
  - `% of issue_comment-triggered runs that skip in <2s`
  - median time spent in `ai:planning` and `ai:implementing`
  - count of implement failures with `stuck in exploration`
  - count/duration of cancelled `review_autofix` comment-only runs
  - stable-release timeout count vs explicit-failure count

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Implement bottleneck: downstream failure is discovered too late
- **Queueing:** modest
- **Compute:** implement attempts are short
- **Retry overhead:** high downstream impact
- **Evidence:** `25237291900` waits ~91 minutes; actual implement failures take ~3 minutes.
- **Fix:** Fail fast on child-run failure signals.

### 2. Implement → Review bottleneck: expensive review runs even when no patch/merge path exists
- **Queueing:** moderate
- **Compute:** high
- **Retry overhead:** cancellation waste
- **Evidence:** `review_autofix` cancelled runs at `707–734s` on comment-only paths.
- **Fix:** low-cost reviewer mode or deterministic comment-only short-circuit.

### 3. Comment-trigger fan-out bottleneck: no-op workflows consume orchestration capacity
- **Queueing:** high aggregate impact
- **Compute:** low per run
- **Retry overhead:** none
- **Evidence:** dozens of 0–2s skip runs in recent telemetry.
- **Fix:** route only command-prefixed comments.

### 4. Review/autofix merge/conflict overhead: editor-bait race and post-merge edge cases
- **Queueing:** low
- **Compute:** moderate
- **Merge/conflict overhead:** real
- **Evidence:** `step-006-e2e-smoke-test.log` includes explicit guards and prior-race commentary around PR already closed / bait injection, and `25215477856` still failed the bait-removal check.
- **Fix:** distinguish test-setup failure from editor failure; gate stable-release failure accordingly.

### 5. Poller/checkout overhead: full fetches where narrow state would do
- **Queueing:** low
- **Compute:** low-medium
- **Evidence:** `orchestrate_poll` checkout spent ~9s fetching all branches/tags.
- **Fix:** shallow, branch-scoped fetch profiles for pollers.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` end-to-end timeout behavior: avg **4,224.2s**, p50 **4,758s**, p95 **5,580.2s**
- `review_autofix` long/cancelled comment-only review paths: p95 **1,819.35s**, **31 cancelled / 60 total**
- `ci` runner/setup-heavy jobs: avg **611.6s**, p50 **615.5s**
- `workflow_log_analysis` is itself a long-running consumer: avg **3,959.6s**

**Top failure modes**
- Implement failures at `Run Codex implementation` with exploration/no-diff loops
- Stable-release failures due to timeout waiting for phase progression
- Stable-release bait-removal regression (`bait_remained`)
- Nightly validation self-test fixture failures (`passed=1 failed=2`)

**Highest-cost drivers**
- Multi-model, `xhigh` reasoning review runs on comment-only paths
- Repeated failed implement attempts on trivial deterministic tasks
- Long-lived stable-release wait loops
- GH API-heavy audit/review polling loops

**Top 3 prioritized actions**
1. **Make stable-release waiters consume downstream implement/review failure signals immediately.**
2. **Introduce a low-cost `review_autofix` mode for comment-only / non-editor paths.**
3. **Add deterministic fallback for smoke/e2e implement tasks after the first no-diff attempt.**

## Metrics Appendix

### Repository summary

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 261 | 14 | 40 | 685 | 1.4% | 115.0 | 1.0 | 610.0 |

### Workflow-family hotspots

| Workflow family | Total runs | Failures | Cancelled | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 5 | 3 | 2 | 4224.2 | 4758.0 | 5580.2 |
| `workflow_log_analysis` | 5 | 0 | 2 | 3959.6 | 4329.0 | 5183.2 |
| `review_autofix` | 60 | 0 | 31 | 365.8 | 41.0 | 1819.35 |
| `ci` | 52 | 0 | 0 | 611.6 | 615.5 | 650.45 |
| `implement` | 183 | 10 | 5 | 28.9 | 1.0 | 218.1 |
| `orchestrate` | 5 | 0 | 0 | 261.8 | 257.0 | 283.2 |
| `orchestrate_poll` | 33 | 0 | 0 | 57.7 | 45.0 | 55.6 |

### Observed token totals in sampled deep dives

| Run ID | Workflow family | Evidence | Observed tokens |
|---|---|---|---:|
| `25244127789` | `implement` | attempt 1 `4,439`, attempt 2 `4,334` | **8,773** |
| `25244121942` | `implement` | provided follow-on diagnostics in telemetry | **32,938** |
| `25244157035` / `25244172221` | `clarify` / `respond` follow-ons | echoed failed implement diagnostics | mirrors failed implement totals |

> Repo-wide token totals were **not** present in `summary.json`; only per-run observations were available in deep-dive logs and supplied telemetry.

### AI memory telemetry

| Metric | Value |
|---|---:|
| JSON telemetry ops observed | 110 |
| `retrieve` ops | 20 |
| Retrieve hit rate | 80.0% (16/20) |
| Avg estimated tokens per retrieve | 37.8 |
| Min / Max estimated tokens | 0 / 56 |
| `keyword_method=plain` | 16 |
| `keyword_method=none` | 4 |
| `keyword_method=llm` | 0 |
| `fail_open=true` | 0 |
| `enabled=false` | 0 |
| Push events with `push_attempts > 1` | 6 |
| Max push attempts | 2 |

### Prompt/cache signals

| Cache type | Observed behavior | Notes |
|---|---|---|
| UV/setup cache | Frequent hits | Seen in recent `plan`/`clarify` runs |
| OpenRouter prompt cache | Enabled in sampled implement/review runs (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) | Hit-rate metrics not emitted in sampled logs |
| Prompt-cache instrumentation | Present | `## OpenRouter Prompt Cache Instrumentation` sections found |
| Prompt read/create token metrics | Not available | Recommend explicit emission |

### GH API summary from deep-dive sample

| Workflow family | Observed `gh api` calls | Primary hotspot |
|---|---:|---|
| `workflow_log_analysis` | 779 | API redundancy + deep audit passes |
| `implement` | 612 | main implement step |
| `test_and_mark_stable` | 578 | E2E polling and verification |
| `review_autofix` | 560 | codex-agent / review wait logic |
| `copilot_pull_request_reviewer` | 10 | artifact lookup |
| `issue_pr_status` | 10 | sync/cleanup helpers |
| `cancel_on_pr_close` | 8 | rate-limit + runs lookup/cancel |
| `orchestrate_poll` | 2 | lightweight polling |

### Serena efficiency snapshots

| Run ID | Workflow family | Serena calls | File fallback ops | Efficiency | Est. tokens with Serena | Est. tokens without Serena | Est. savings |
|---|---|---:|---:|---:|---:|---:|---:|
| `25244127789` | `implement` | 269 | 16 | 94% | ~26,350 | ~181,100 | ~154,750 (85%) |
| `25244121942` | `implement` | 269 | 14 | 95% | ~25,350 | ~180,100 | ~154,750 (86%) |
| `25244790668` | `review_autofix` | n/a | n/a | n/a | n/a | n/a | Serena stats missing |

If you want, I can turn this into a prioritized implementation checklist mapped to specific workflow files and likely edit points.

## Deep Audit — Workflows & Scripts (2026-05-02)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `scripts/memory_helpers.sh:216-233`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — `memory_finalize_task()` and `memory_promote()` call `python3 ... ai_memory.py` directly and do not preserve the helper library’s documented fail-open contract. Under `set -euo pipefail`, their callers in `.github/workflows/implement.yml:2949-2969` and `.github/workflows/issue_pr_status.yml:428-445` will fail the whole workflow on any transient memory write error, even though `README.md` says memory run events are fail-open.  
  **Recommended fix** — Wrap both functions the same way `memory_record_run_event()`, `memory_record_candidate()`, and `memory_processed_command_complete()` already do: catch non-zero exits, emit `_memory_warn` + `_memory_telemetry`, and return `0`. Reuse the existing fail-open pattern already implemented elsewhere in `scripts/memory_helpers.sh`.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/internal-review.yml:98-118`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The claude-branch push path treats GitHub API lookup failure as “no open PR exists.” `existing_pr="$(gh api ... || echo "")"` and `base_ref="$(gh api ... || echo 'main')"` both fail open; if the PR lookup hits a transient 5xx/rate limit/auth blip, the job emits `proceed=true` and dispatches `review_autofix.yml` even when the synchronized PR review path is already running. That creates duplicate expensive review runs instead of suppressing them.  
  **Recommended fix** — Make PR lookup failure a soft skip, not a proceed path: use `gh_retry`/`scripts/gh_helpers.sh`, and only set `proceed=true` after a successful API response that definitively shows no open PR. If lookup fails, emit `proceed=false` with a warning so the next push/retry re-evaluates cleanly.

- **ID** — `SEC-001`  
  **File path** — `scripts/setup_serena.sh:173-177`  
  **Severity** — High  
  **Category tag** — `security`  
  **Description** — The script installs `uv` via `curl -LsSf https://astral.sh/uv/install.sh | sh`. That executes remote network content immediately on the runner with no checksum, signature, or pinned artifact verification. Because this helper runs in multiple privileged workflows, compromise of the installer path would become a supply-chain compromise of the Actions pipeline.  
  **Recommended fix** — Replace the pipe-to-shell install with a pinned, verifiable path: download a versioned release artifact, verify checksum/signature, then execute it; or install `uv` from a trusted package source already pinned elsewhere in the repo. Keep the current fail-open wrapper (`warn_and_exit`) around the safer installer.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/review_autofix.yml:478-530`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The post-merge validate-dispatch step already does one GraphQL fetch for `closingIssuesReferences`, but on the body/title fallback path it rebuilds `issue_nodes_json` with `labels: null` and then performs `gh issue view ... --json labels` once per linked issue inside the `while` loop. Current call count on the fallback path is `2 + N` (`1` PR fetch + `1` initial discovery + `N` per-issue label fetches). Proposed call count is `2`: one PR/body fallback fetch plus one alias-based GraphQL labels fetch for all issue numbers.  
  **Recommended fix** — Extend the alias-fragment batching pattern already used in `.github/workflows/issue_pr_status.yml:286-336` so the fallback issue-number list is rehydrated with labels in one GraphQL call before the loop. That preserves loop logic while eliminating the per-issue REST calls.

- **ID** — `API-002`  
  **File path** — `.github/workflows/clarify.yml:385-390`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — When semantic cache is enabled, the step fetches the same issue comments twice: once bounded to `per_page=50` into `ISSUE_COMMENTS_FILE`, then again with `--paginate --slurp per_page=100` into `THREAD_HISTORY_FILE`. Current call count is `2` for the same resource in the same execution path; proposed call count is `1`.  
  **Recommended fix** — Fetch the paginated/slurped comments once, write that raw JSON to a temp file, derive the bounded prompt context and the full thread-history projection from the same local payload. Mirror the repo’s existing “fetch once, reuse many” snapshot pattern already used for issue metadata caches in `implement.yml`.

- **ID** — `API-003`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:27-103`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The workflow always performs a proactive `/rate_limit` header fetch inside `_rl_wait`, then separately lists queued runs and in-progress runs from the same endpoint and merges them client-side. On the common no-op path, current call count is effectively `3` (`1` `/rate_limit` helper path available + `2` Actions run listings) before any cancellation attempt. Proposed call count is `1`: list branch-scoped PR runs once and filter `queued|in_progress` locally; only hit `/rate_limit` if an actual retryable error occurs.  
  **Recommended fix** — Replace the two `actions/runs` scans with one paginated branch/event-scoped snapshot, filtered client-side for both statuses. Then source `scripts/gh_helpers.sh` and use its retry logic instead of the inline `_rl_wait` path so `/rate_limit` is only consulted on demand.

- **ID** — `API-004`  
  **File path** — `.github/workflows/internal-review.yml:98-101`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The claude-branch resolver does two independent `gh api` requests every push: one for open PRs on the branch and one for repository default branch. Current call count is `2`; proposed call count is `1` via a single GraphQL query that returns both `defaultBranchRef.name` and the branch’s open PR nodes.  
  **Recommended fix** — Add a small GraphQL-first helper in `scripts/gh_helpers.sh`, modeled after the repo’s existing GraphQL-first helpers, that returns `{ default_branch, existing_pr_number }` in one request. This also makes it easy to add `gh_retry` and eliminate the current fail-open dispatch bug.

- **ID** — `BATCH-001`  
  **File path** — `scripts/check_external_branch_advance.sh:175-199`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — For every advancing self-like commit, the script calls `gh api repos/{repo}/commits/{sha}` to resolve GitHub-attributed author/committer identities. Current call count is `1 + N` on the API side after local `git rev-list` (`N` = advancing commits). Proposed call count is `1` GraphQL alias query for all candidate SHAs. The current comment says commit sets are “usually tiny,” but this code runs in the hot review/autofix path and can still amplify retries during stacked autofix or force-push scenarios. [NEEDS VERIFICATION]  
  **Recommended fix** — Extend the alias-based GraphQL batching style already used in `.github/workflows/issue_pr_status.yml` to fetch author/committer logins for all `self_subject_shas` at once. If GraphQL commit-by-SHA resolution proves unreliable for detached SHAs, cache the per-SHA REST results within the script so repeated checks in the same run do not re-fetch them.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/clarify.yml:60-119`; `.github/workflows/plan.yml:89-140`; `.github/workflows/implement.yml:237-290`; `.github/workflows/validate.yml:84-132`; `.github/workflows/orchestrate_clarify_respond.yml:97-150`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The “canonical integration resolver” bootstrap is copied almost verbatim across five workflows: stage temp dirs, clone `shubhodeep1/coding-workflows`, fetch `stable` or `${{ github.sha }}`, sanitize clone logs, locate `scripts/resolve_integration_ref.sh`, and invoke it with `REPO/ISSUE/GH_TOKEN`. This is drift-prone; any auth, fallback, or log-redaction fix must be replicated in five places.  
  **Recommended fix** — Move the bootstrap into a shared script, e.g. `scripts/resolve_integration_ref_bootstrap.sh`, with a function signature like `resolve_integration_ref_bootstrap <repository> <issue_number> <resolver_repo> <resolver_ref>`. Update callers in `clarify.yml`, `plan.yml`, `implement.yml`, `validate.yml`, and `orchestrate_clarify_respond.yml` to call the shared helper and consume a single `ref=` output contract.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:27-52`; `.github/workflows/mark-stable.yml:308-334`; `.github/workflows/orchestrate_poll.yml:66-97`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Three workflows carry their own inline `_rl_wait`/`_gh_retry` implementations even though `scripts/gh_helpers.sh` already owns the canonical retry and rate-limit behavior. The bodies are near-identical but not fully identical, which means retry policy changes can drift across control-plane workflows.  
  **Recommended fix** — Standardize on `scripts/gh_helpers.sh` as the owner module. Either check out/support-copy it earlier in each workflow, or add a tiny bootstrapping helper with the signature `source_gh_retry_or_inline_fallback`. Update `cancel_on_pr_close.yml`, `mark-stable.yml`, and `orchestrate_poll.yml` to use the shared implementation instead of maintaining inline copies.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/workflow-log-analysis.yml:460-510`; `.github/workflows/workflow-log-analysis.yml:831-878`; `.github/workflows/workflow-log-analysis.yml:1159-1205`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The workflow contains three near-identical Codex-pass runners: base analysis, deep audit, and API-redundancy. Each block assembles a prompt file, validates `MAX_CODEX_ATTEMPTS`, retries `codex exec`, handles empty output, and writes an output section. The only real variation is prompt template, output heading, and failure emitter.  
  **Recommended fix** — Extract this into a shared script such as `scripts/run_codex_report_pass.sh` with a signature like `run_codex_report_pass <prompt_template> <context_file> <output_file> <log_file> <max_attempts> <heading_regex>`. Update the three workflow jobs to pass their prompt and heading requirements instead of inlining the whole control loop.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Phase 4b: Wait for review & autofix completion` `run:` block is approximately **16,626 characters** and contains GitHub expressions, so it is already at about **79.2%** of the 21,000-character expression ceiling. Estimated headroom is only **4,374 characters**. This is the same workflow family that has already hit the expression limit multiple times, and this block contains extensive inline helper functions, polling logic, and commentary that are likely to keep growing.  
  **Recommended fix** — Extract the wait-loop into a dedicated script under `scripts/` and keep the workflow step limited to argument/env wiring. That is the same mitigation already used elsewhere in the repo (`scripts/orchestrate_poll_process.sh`, `scripts/render_prompt.sh`) to avoid expression blowups.

No `if:` expression in the audited workflows crossed the 15,000-character threshold, and no workflow file exceeds the 800 KB early-warning threshold. The largest audited workflow files are still below the 1 MB hard limit.

### Section 5: Cross-Cutting Concerns

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/internal-clarify.yml:3-16`; `.github/workflows/internal-orchestrate-clarify-respond.yml:3-13`; comparison baselines `.github/workflows/internal-plan.yml:13-16`, `.github/workflows/internal-implement.yml:13-17`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — Wrapper comment routing is inconsistent. `internal-plan.yml` and `internal-implement.yml` gate on `/answer` and `/approved`, but `internal-clarify.yml` and `internal-orchestrate-clarify-respond.yml` trigger on every created issue comment and delegate skip logic downstream. That inconsistency is a direct source of the repo’s skip-only workflow fan-out and queue churn.  
  **Recommended fix** — Normalize wrapper behavior around command-prefixed gating at the wrapper boundary. Either add the same lightweight `if:` command filters to `internal-clarify.yml` / `internal-orchestrate-clarify-respond.yml`, or centralize comment routing in one shared dispatcher workflow so only actionable comments invoke reusable workflows.

- **ID** — `DEAD-001`  
  **File path** — `scripts/memory_helpers.sh:226-233`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `memory_promote()` is defined but has no callers in any audited workflow or script. That makes it dead wrapper code, and because it also lacks fail-open handling, it is dead code with divergent behavior from the rest of the helper library.  
  **Recommended fix** — Either remove `memory_promote()` if it is obsolete, or wire it into a real call site and align it with the library’s fail-open wrapper pattern before use.

- **ID** — `DEAD-002`  
  **File path** — `scripts/orchestrate_poll_process.sh:9754-9784`; `scripts/orchestrate_poll_process.sh:10003-10057`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are assigned but never read. Search across the repository only finds their definitions/assignments, not any consumer logic. That leaves dead state transitions in one of the repo’s most complex scripts and makes later maintenance harder because the variables look semantically important but currently do nothing.  
  **Recommended fix** — Remove the unused variables, or add the missing consumer logic if they were intended for diagnostics/state reporting. Either way, document the intended lifecycle in the surrounding comment block so future changes do not resurrect more inert state.

- **ID** — `SHELL-001`  
  **File path** — `scripts/validate_process.sh:197-205`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `tg_notify()` uses `local msg="$1$(_tg_link_suffix)"`, which triggers ShellCheck `SC2155` (“declare and assign separately to avoid masking return values”). In a `set -e` script, this pattern can hide failures inside `_tg_link_suffix` instead of surfacing them cleanly.  
  **Recommended fix** — Split declaration from assignment: `local msg` on one line, then `msg="$1$(_tg_link_suffix)"` on the next. Apply the same cleanup to other SC2155 sites such as `scripts/orchestrate_poll_process.sh:2684`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, SEC-001 |
| Medium | 10 | BUG-002, API-001, API-002, API-003, API-004, DUP-001, DUP-002, DUP-003, EXPR-001, CONSIST-001 |
| Low | 4 | BATCH-001, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 5 | Medium |
| Code modularization | 9 | Large |
| Expression size reduction | 1 | Small |
| Medium/Low fixes | 5 | Small |
