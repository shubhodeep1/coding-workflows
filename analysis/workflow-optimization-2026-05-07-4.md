## Executive Summary

- **`test_and_mark_stable` is the dominant end-to-end bottleneck and reliability risk.** In `shubhodeep1/coding-workflows`, the family ran **5 times with 0 successes**, **avg 4,134s**, **p50 3,987s**, **p95 5,217.6s**; failures were at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` (run `25445414047`), `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` (run `25474642232`), and `orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project` (run `25477674617`). **Estimated impact:** save **20–35 min on affected release-test runs** and materially improve pass rate. **Confidence:** high.

- **`review_autofix` is the main long-running subworkflow feeding those failures.** The family had **85 runs**, **45 cancelled**, **avg 357.1s**, **p95 1,398.8s**. In run `25480623045`, the claude-branch-review path still spent about **229s** in `review / gate` + `codex-agent` even though editor/commit/judge/auto-merge were skipped. **Estimated impact:** **5–20 min** end-to-end savings for workflows waiting on review, plus fewer cancellation-induced reruns. **Confidence:** high.

- **`workflow_log_analysis` is the clearest token-cost hotspot and also fails operationally.** The family averaged **2,645.6s** with **1 failure** and **1 cancellation** out of 5 runs. Its `summarize_unselected_runs` step used **214,237 tokens** (run `25473131401`), **259,600 tokens** (run `25470798500`), and **232,690 tokens** (run `25474659590`); run `25477691662` logged `Workflow log analysis Codex pass failed after 3 attempts with exit code 2.` **Estimated impact:** cut analysis cost by **30–50%** and latency by **8–15 min/run** if scope is reduced safely. **Confidence:** high.

- **CI is stable but structurally slow because one `lint` job dominates nearly the entire run.** `ci` shows **63 runs**, **60 success**, **3 failures**, **avg 614.1s**, **p50 620s**, **p95 649.9s**. Recent run `25479539893` spent about **629s** in `lint`, and logs show repeated package installation/setup. **Estimated impact:** **1–3 min per CI run** with low-risk dependency caching and job partitioning. **Confidence:** high.

- **AI memory telemetry is present, but retrieval effectiveness is weak in sampled deep dives.** Across 6 sampled `retrieve` operations from failed/slow runs, only **1 returned records** (hit rate **16.7%**); five returned `records_selected: 0` across clarify/orchestrate/review paths. **Estimated impact:** moderate quality and token savings if retrieval is made useful before active phases; likely fewer redundant prompts and less repeated context expansion. **Confidence:** medium.

- **GH API hygiene is uneven: the main waste is redundant per-cycle lookups, not rate-limit failures.** `cancel_on_pr_close` repeatedly probes `/rate_limit` and cancellation endpoints even on no-op runs (e.g., run `25480772090` spent ~**9.8s of 13s** there), while `implement` logs show multiple issue/comments/PR lookups that repository rules already say should be GraphQL-batched/cached. **Estimated impact:** reduce API calls by **30–60% in hot steps** and lower latency variance. **Confidence:** high.

- **Prompt cache is enabled, but cache effectiveness is not observable enough and one file cache is misconfigured.** Logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but no prompt-cache hit/read/create counters were found in sampled deep dives; meanwhile validate run `25480631539` showed `Cache not found...` and `Path Validation Error... no cache is being saved.` **Estimated impact:** moderate token and latency reduction once instrumentation and stable cache keys are fixed. **Confidence:** medium.

## Speed Optimizations

Ranked by expected latency reduction.

### 1) Short-circuit `test_and_mark_stable` waits when downstream run lineage is broken
**Critical-path win**

- **Evidence:**  
  - `test_and_mark_stable` family: **avg 4,134.2s**, **p50 3,987s**, **p95 5,217.6s**, **0/5 success**.  
  - Run `25445414047` failed at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` after **3,987s**. Deep logs noted the review run pin advanced after cancellation but no successor run was found, and the workflow waited the full timeout.  
  - Run `25474642232` failed at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` after **5,395s**.  
- **Root cause:** Cancellation-sensitive watch logic continues waiting after a cancelled child run loses successor linkage.  
- **Exact change:**  
  1. In dispatch/watch helpers, treat “pinned run cancelled and no successor discovered within N polls” as a **terminal diagnostic state**, not a long wait.  
  2. Fail fast with a structured summary containing parent run ID, child workflow, last seen run ID, and cancellation reason.  
  3. Add a bounded successor-search window, e.g. 2–3 polling intervals, then exit.  
- **Estimated time savings:** **20–35 min on affected stable-test runs**; **10–20 min** on average for the failing class seen here.  
- **Implementation risk:** Low-medium. This is a watcher behavior change, not a workflow contract change.  
- **Rollback / fail-open:** Keep the old long-wait path behind a flag or only use fail-fast when the child run is already conclusively `cancelled`.

### 2) Reduce `review_autofix` wait time on comment-only / no-edit paths
**Critical-path win**

- **Evidence:**  
  - `review_autofix`: **85 runs**, **45 cancelled**, **p95 1,398.8s**.  
  - Run `25480623045` logged `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`, yet still took **251s**, with about **229s** dominated by `review / gate` and `codex-agent`.  
  - Multiple cancelled slow runs were in the **570–1,778s** range, including run `25468425312` cancelled at **1,778s**.  
- **Root cause:** Expensive reviewer path still runs nearly full-length even when deterministic gates already know no code-writing path will occur.  
- **Exact change:**  
  1. Split `review_autofix` into an early deterministic gate and a lighter “comment-only reviewer” path.  
  2. When `editor/commit/judge/auto-merge` are all skipped, cap reviewer runtime and reduce panel breadth before spawning long-running review work.  
  3. Propagate the gate result upstream so release tests wait for the lighter terminal condition, not the full heavy review path.  
- **Estimated time savings:** **3–10 min per affected review run**, with larger end-to-end benefit when `test_and_mark_stable` is blocked on it.  
- **Implementation risk:** Medium, because review quality must be preserved.  
- **Quality-risk notes:** Start by shrinking only the clearly comment-only branch; keep full path for risky diffs.

### 3) Narrow `workflow_log_analysis` deep-audit scope before invoking Codex retries
**Critical-path win**

- **Evidence:**  
  - `workflow_log_analysis`: **avg 2,645.6s**, **p50 3,345s**, **p95 3,538s**.  
  - Run `25477691662` succeeded at **3,345s**, but logged `Workflow log analysis Codex pass failed after 3 attempts with exit code 2.`  
  - `summarize_unselected_runs` token usage was **214,237** (`25473131401`), **259,600** (`25470798500`), **232,690** (`25474659590`).  
- **Root cause:** Large prompt scope plus retrying expensive analysis passes on already-broad run sets.  
- **Exact change:**  
  1. Pre-filter unselected runs more aggressively using deterministic heuristics already available in telemetry: same family + anomalous duration + failure/cancellation + recentness.  
  2. Lower the `summarize_unselected_runs` target when a deep-dive folder already exists for the repo/family.  
  3. On Codex exit code 2, retry only the failed sub-pass, not the full analysis batch.  
- **Estimated time savings:** **8–15 min/run** for `workflow_log_analysis`; also reduces blocking in `test_and_mark_stable` orphan-watch cases.  
- **Implementation risk:** Low-medium.  
- **Rollback / fail-open:** If prefiltering is too aggressive, fall back to current scope only when anomalies exceed a threshold.

### 4) Cache Python/actionlint dependencies and split CI hot tests from slower unit groups
**High-confidence, medium impact**

- **Evidence:**  
  - `ci`: **avg 614.1s**, **p50 620s**, **p95 649.9s**.  
  - Run `25479539893` spent ~**629s** in `lint`; runner wait and test execution dominated the full **632s** run.  
  - Logs show repeated installs such as `python3 -m pip install yamllint coverage pyyaml jsonschema jinja2` and repeated actionlint setup/download behavior.  
- **Root cause:** Monolithic `lint` job bundles environment setup, script validation, and unit tests every run.  
- **Exact change:**  
  1. Add pip cache keyed by Python version + dependency manifest.  
  2. Separate fast workflow-reference/lint checks from slower unit suites, so early failures return sooner.  
  3. Keep the current combined job as a nightly or merge-protection fallback if desired.  
- **Estimated time savings:** **1–3 min per CI run**; faster feedback on failures even when total wall clock changes less.  
- **Implementation risk:** Low.  
- **Rollback / fail-open:** Easy rollback; cache misses simply fall back to current behavior.

### 5) Fix the validate hints cache path so successful runs actually save cache
**Micro-optimization with low risk**

- **Evidence:** Run `25480631539` (`validate`) logged `Cache not found for input keys: validate-hints-v1-...` and `[warning]Path Validation Error: Path(s) specified in the action for caching do(es) not exist, hence no cache is being saved.`  
- **Root cause:** Cache key exists, but the path to save/restore does not.  
- **Exact change:** Ensure the hints directory is created before restore/save, or point the cache action to the correct existing path.  
- **Estimated time savings:** Likely **seconds to low tens of seconds** per validate run; small but essentially free.  
- **Implementation risk:** Very low.

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1) Cut `workflow_log_analysis` token scope before model invocation
- **Evidence:** `summarize_unselected_runs` consumed **214,237 tokens** in run `25473131401`, **259,600** in `25470798500`, and **232,690** in `25474659590`. The family itself is only **5 runs**, so this is concentrated spend.  
- **Root cause:** Large unselected-run coverage plus expensive retries on a workflow that already has deep-dive evidence folders.  
- **Exact change:**  
  1. Lower the unselected-run summary target below 100 when deep-dive folders already cover the failing/slow/recent edge cases.  
  2. Deduplicate runs from the same family/outcome window before summarization.  
  3. Skip full re-analysis on retries if the first pass already produced partial structured output.  
- **Estimated savings:** **30–50% token reduction** in `workflow_log_analysis` runs based on sampled 214k–260k token spans.  
- **Quality-risk notes:** Low if deep-dive-selected runs remain untouched; medium if unselected coverage becomes too narrow.

### 2) Gate multi-model review panels harder on deterministic skip/comment-only cases
- **Evidence:** Run `25480772095` exposed a broad reviewer set: `minimax/minimax-m2.5 moonshotai/kimi-k2.5 deepseek/deepseek-v4-pro z-ai/glm-5 qwen/qwen3.6-plus x-ai/grok-4.1-fast`; run `25480623045` still incurred long comment-only review time while edit/merge paths were skipped.  
- **Root cause:** Expensive reviewer breadth is available even when deterministic gates already indicate a narrow task.  
- **Exact change:**  
  1. Use a tiered reviewer policy: one primary reviewer for docs/small-diff/comment-only paths, full panel only for risky or conflicting diffs.  
  2. Reuse the existing deterministic skip logic (`doc_only`, `small_diff`, claude-branch-only) as the routing input.  
- **Estimated savings:** Potentially **large per heavy review run**, but not precisely quantifiable from current telemetry because token-by-model totals were not emitted.  
- **Quality-risk notes:** Medium. Start with small-diff/comment-only cases only.

### 3) Reduce avoidable rerun cost from cancelled `review_autofix` and failed stable-release tests
- **Evidence:** `review_autofix` had **45 cancelled / 85 total**. `test_and_mark_stable` had **3 failures + 2 cancellations / 5 total** and no successes.  
- **Root cause:** The pipeline burns compute and likely tokens on runs that later get invalidated by cancellation or downstream watch failures.  
- **Exact change:**  
  1. Promote earlier cancellation detection in release-test watchers.  
  2. Stop spawning expensive downstream analysis/review work once an upstream terminal condition is known.  
  3. Preserve and reuse intermediate structured summaries for reruns rather than recomputing them.  
- **Estimated savings:** High overall; exact token delta unavailable, but rerun avoidance is likely the single safest non-model cost win after `workflow_log_analysis`.  
- **Quality-risk notes:** Low if reuse is limited to deterministic metadata and previous structured summaries.

### 4) Trim repeated prompt/context expansion in implement/clarify/orchestrate active paths
- **Evidence:** Sampled memory retrieves in active failures mostly returned no prior records: clarify runs `25473125487`, `25473129175`, `25473129346`; orchestrate run `25473127144`; review run `25468425312`. That implies prompts likely carried context without memory-assisted compression.  
- **Root cause:** Low memory hit rate means repeated full context is more likely to be resent.  
- **Exact change:**  
  1. Improve retrieval usefulness before prompt construction.  
  2. Move dynamic per-run noise to suffix sections so stable task instructions can cache better.  
  3. Emit prompt-cache hit/read/create counters to verify savings.  
- **Estimated savings:** Medium, but currently inferential because direct prompt-cache counters were not present.  
- **Quality-risk notes:** Low if prompt semantics remain unchanged.

### 5) Lower reasoning/model intensity on low-risk maintenance steps
- **Evidence:** Validate run `25480631539` used `MODEL_EDITOR: openai/gpt-5.4`, `MODEL_REASONING_EFFORT: medium`, `MODEL_VERBOSITY: low`; some maintenance/status workflows complete quickly without clear need for heavy reasoning.  
- **Root cause:** Uniform model settings may be stronger than necessary on non-novel maintenance tasks.  
- **Exact change:** Audit maintenance/status paths and downgrade only those that are deterministic, short, and already backed by scripts.  
- **Estimated savings:** Small to medium.  
- **Quality-risk notes:** Medium; limit changes to steps with existing deterministic validation.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1) Harden downstream watch logic in `test_and_mark_stable`
- **Failure evidence:**  
  - Run `25445414047`: failed at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`.  
  - Run `25474642232`: failed at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis`, with downstream run `25474659590` ending `cancelled`.  
- **Root cause category:** Orchestration / child-run tracking.  
- **Exact fix:** Make watch steps resilient to cancelled child runs, missing successor discovery, and stale pins; emit terminal structured diagnostics instead of timing out.  
- **Expected reliability impact:** Highest. This addresses the failure mode present in **2 of 3** failed release-test runs directly.  
- **Rollback / fail-open:** Fall back to current behavior only when child-run state is ambiguous rather than conclusively terminal.

### 2) Fix missing tracking-issue lookup in orchestrate-decompose smoke
- **Failure evidence:** Run `25477674617` failed at `orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project`; deep logs included `##[error]Could not locate tracking issue for run 25477674617`.  
- **Root cause category:** State linkage / issue discovery.  
- **Exact fix:**  
  1. Persist tracking issue number/URL as an explicit workflow output at dispatch time.  
  2. When lookup fails, search by stored run ID marker before declaring failure.  
- **Expected reliability impact:** High for orchestration smoke coverage; should remove one concrete hard failure.  
- **Rollback / fail-open:** If explicit output is absent, retain current search path as fallback.

### 3) Turn `workflow_log_analysis` retry-after-exit-code-2 into partial retry
- **Failure evidence:** Run `25473131401` failed in `analyze-commit-notify / Run workflow log analysis`; run `25477691662` logged `Codex pass failed after 3 attempts with exit code 2.`  
- **Root cause category:** Tooling retry strategy.  
- **Exact fix:** Persist sub-phase artifacts and retry only the failed analysis pass instead of the whole pipeline.  
- **Expected reliability impact:** Medium-high; should reduce both outright failures and cancellation exposure.  
- **Rollback / fail-open:** If partial state is missing/corrupt, revert to full rerun.

### 4) Stabilize active clarify/orchestrate failures with better preflight and memory fallbacks
- **Failure evidence:**  
  - Clarify failures: runs `25473125487`, `25473129175`, `25473129346`, all at `clarify / Run Codex`.  
  - Orchestrate failure: run `25473127144` at `orchestrate / Run Codex (decomposer)`.  
  - Sampled retrieves for these runs returned `records_selected: 0`.  
- **Root cause category:** Active-phase context preparation / AI task readiness.  
- **Exact fix:**  
  1. Add a preflight summary step that verifies memory retrieval result, issue state, and required context before launching Codex.  
  2. When retrieval misses, inject a compact deterministic fallback context instead of proceeding with empty memory.  
- **Expected reliability impact:** Medium.  
- **Rollback / fail-open:** If preflight itself errors, continue with current behavior but mark the run as degraded.

### 5) Repair failing CI unit-test lanes before adding more coverage
- **Failure evidence:**  
  - Run `25469919488` failed at `lint / Orchestrate poll process unit tests`.  
  - Runs `25473514248` and `25473697143` failed at `lint / Implement post-Codex recovery unit tests`.  
  - `nightly_validation_selftest` run `25474243471` failed: `fixtures=3 passed=1 failed=2`.  
- **Root cause category:** Test instability / incomplete fixes.  
- **Exact fix:**  
  1. Quarantine newly failing unit groups behind a non-blocking lane until deterministic.  
  2. Keep existing stable lint/reference checks blocking.  
  3. Add targeted reproducer fixtures from the failed self-test cases.  
- **Expected reliability impact:** Medium; prevents noisy CI failures from obscuring orchestrator regressions.  
- **Rollback / fail-open:** Re-enable blocking once fixture pass rate is stable.

## AI Memory Health

- **Telemetry presence:** Present in deep-dive logs for `implement`, `clarify`, `orchestrate`, `review_autofix`, `validate`, `memory_maintenance`, and `issue_pr_status`. No evidence that telemetry is globally missing, but many recent runs only exposed partial/truncated lines.

- **Sampled retrieve hit rate:** In 6 sampled `retrieve` operations from deep-dive logs, only **1** selected any records.  
  - **Hit rate:** **16.7%**  
  - **Misses:** clarify `25473125487`, `25473129175`, `25473129346`; orchestrate `25473127144`; review `25468425312`  
  - **Hit:** implement `25470900024` with `records_selected: 2`

- **Average retrieval size vs budget:**  
  - **Average `estimated_tokens`: 9.3** across sampled retrieves  
  - **Observed range:** `0` to `56`  
  - **Budget gap:** sampled telemetry did **not** include an explicit retrieval token budget field, so budget utilization cannot be computed from the current window.

- **`keyword_method` distribution in sampled retrieves:**  
  - `plain`: **4/6** (**66.7%**)  
  - `llm`: **1/6** (**16.7%**)  
  - `none`: **1/6** (**16.7%**)

- **Flags:**  
  - **Zero-record retrieves are common:** 5 of 6 sampled.  
  - **`fail_open: true`:** not found in sampled telemetry.  
  - **`enabled: false`:** not found in sampled telemetry.  
  - **Push retries:** mostly `push_attempts: 1`; one sampled clarify failure event had **`push_attempts: 2`**, which is not alarming by itself but worth tracking.  
  - **Lifecycle ops present:**  
    - `processed-command-check` / `processed-command-claim` in implement `25470900024`  
    - `record-run-event` in implement/orchestrate/clarify/validate  
    - `compact` in memory maintenance run `25479744907`, compacting **2,914 candidates**  
    - `finalize-task` in issue/status flow `25479754989`

### Recommendations
1. **Improve retrieval recall before active AI phases.**  
   Use deterministic keys first: issue number, workflow family, command type, PR number, and branch. Current miss rate suggests the retrieval query is under-specific or disconnected from stored records.

2. **Emit retrieval budget and retrieval-source counts.**  
   Without budget, the “estimated tokens vs budget” health check is incomplete.

3. **Track these indicators weekly:**  
   - retrieve hit rate  
   - % retrieves with `records_selected = 0`  
   - median `estimated_tokens`  
   - % writes with `push_attempts > 1`  
   - count of `fail_open: true`

## GH API Call Audit

### Main hotspots

| Workflow / run evidence | Hot pattern | Evidence | Recommended change | Expected reduction |
|---|---|---|---|---|
| `cancel_on_pr_close` run `25480772090` | Rate-limit probe + cancel loop overhead | Step spent ~`9.8s` of `13s`; logs mention `_gh_retry gh api -X POST repos/${REPOSITORY}/actions/runs/${run_id}/cancel` and `gh api -i /rate_limit` in `_rl_wait()` even when no targets matched | Only probe `/rate_limit` after a 403/429 or after discovering actual runs to cancel; list candidate runs once, then cancel only if non-empty | On no-op runs, reduce from multiple GH calls to **0–1 effective calls**; lower latency and rate-limit noise |
| `implement` run `25470900024` | Repeated per-issue lookups | Logs show issue fetch, later label fetch again, `gh pr list --search "issue:${ISSUE_NUMBER}"`, paginated comments fetch, and failure comment post | Follow repo-local API hygiene rules already in prompts: use `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and cycle-local caches | Likely **30–60% fewer GH calls** in active implement cycles |
| `review_autofix` recent summaries | Repeated metadata / dispatch mutations | `gh api graphql`, `gh workflow run ...`, fallback `gh workflow run internal-validate.yml`, `gh issue edit ... --remove-label ai:orchestrator-validate-required` | Reuse issue/PR metadata already loaded earlier in the job; collapse label removal into the same decision path that determines dispatch | Save **2–3 calls per run** in the post-merge validate path |
| `copilot_pull_request_reviewer` run `25480625290` | Artifact cleanup lookup | `gh api /repos/shubhodeep1/coding-workflows/actions/runs/25480625290/artifacts` in cleanup | Skip artifact enumeration when no artifact-producing steps ran, or reuse artifact IDs from upload steps | Small reduction; low priority |

### Repository-specific API hygiene cross-check
The repository’s own instructions in `implement` already warn against ad hoc `gh api` expansion and prefer GraphQL helpers plus cycle-local caches. Current implement logs show the workflow is not fully benefiting from those rules yet.

### Additional observations
- No inspected logs showed GitHub **429** or secondary rate-limit failures.  
- The current issue is **redundancy and unnecessary probes**, not an active rate-limit outage.

## Prompt Cache & Memory System

### What the telemetry shows
- Prompt cache appears **enabled** in sampled AI workflows via `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- However, sampled deep-dive logs did **not** expose direct prompt-cache **create/read/hit** counters, so true prompt-cache hit rate cannot be measured from this window.
- File-based cache health is mixed:
  - `validate` run `25480631539` showed `Cache not found for input keys: validate-hints-v1-...`
  - Same run logged `[warning]Path Validation Error... no cache is being saved.`

### Likely fragmentation causes
- **Inference:** cache keys are probably being fragmented by dynamic prefixes such as expanded issue comments, orchestrator state blobs, run IDs, or timestamps appearing too early in prompts.
- The high skip volume in `clarify`, `plan`, and `implement` suggests many invocations are condition-driven and short-lived; if prompts differ run-to-run at the prefix level, cache reuse will be poor even when task type is identical.

### Recommendations
1. **Stabilize prompt prefixes.**  
   Keep long-lived instructions, repo rules, and tool contracts at the front; move dynamic issue comment bodies, timestamps, and run-specific state to suffix blocks.

2. **Emit prompt-cache metrics explicitly.**  
   Add per-step counters for `cache_create_tokens`, `cache_read_tokens`, hit/miss status, and fail-open state. Right now the system is effectively un-auditable.

3. **Fix the validate hints cache path.**  
   This is a straightforward reliability + latency improvement with almost no downside.

4. **Connect memory retrieval to prompt construction.**  
   When memory retrieves return 0 records, emit a compact fallback summary rather than silently proceeding with empty retrieval context.

### Estimated impact
- **Tokens:** moderate reduction, especially in repeated clarify/plan/implement/review paths.  
- **Latency:** low-to-moderate per run; larger impact in repeated or retried flows.  
- **Reliability:** medium, because better cache observability and fallback handling should reduce brittle active-phase launches.

## Orchestrator Health

### Current state
- **Gating looks healthy for skipped paths.**  
  `clarify` had **203 runs** but only **22 successes** and **178 skipped**; `plan` had **171 runs** with **153 skipped**; `implement` had **172 runs** with **145 skipped**. That pattern suggests event gating is preventing a lot of unnecessary work rather than causing failures.
- **Active-path orchestration has a few high-cost brittle points.**  
  - Run `25477674617`: `Plan phase stalled — no activity for 30 minutes` and missing tracking issue.  
  - `orchestrate_poll`: **27/27 success**, **avg 59.1s**, **p95 126.3s**, so polling itself is serviceable but not cheap.  
  - `review_autofix` cancellations feed back into orchestrator uncertainty.

### Recurring pain points
1. **Run lineage breaks after cancellation.**
2. **Tracking issue discovery is not robust enough.**
3. **Long waits are used where a degraded-but-terminal state would be safer.**
4. **Memory retrieval is too often empty when active AI phases start.**

### Smallest safe mitigations
- Persist child workflow run IDs and tracking issue IDs as explicit outputs/artifacts at dispatch time.
- Introduce a “degraded terminal” status for watcher steps: child cancelled, successor not found, stale pin, missing tracking issue.
- Add a short preflight check before launching active Codex phases:
  - context present
  - tracking issue present
  - memory retrieve result known
  - command not already claimed

### Indicators to track
- `% orchestrations with tracking issue found on first lookup`
- `% watcher steps ending in timeout vs degraded-terminal vs success`
- `% active AI phases launched with `records_selected = 0``
- `orchestrate_poll` p95 duration
- `% `review_autofix` runs cancelled before useful output`

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact across clarify → plan → implement → review/autofix → validate/orchestrate.

### 1) Retry/cancellation overhead in release validation
- **Evidence:** `test_and_mark_stable` failures at downstream watch steps (`25445414047`, `25474642232`, `25477674617`) after **3,737–5,395s**.  
- **Type:** Retry / orchestration overhead.  
- **Fix:** Fail fast on broken downstream lineage and cache partial child-run diagnostics.

### 2) Heavy compute in `review_autofix`
- **Evidence:** `review_autofix` p95 **1,398.8s**; run `25480623045` spent ~**229s** on review work despite comment-only path.  
- **Type:** Compute overhead.  
- **Fix:** Split comment-only/light review path from full reviewer/editor/judge path.

### 3) High-cost analysis in `workflow_log_analysis`
- **Evidence:** family avg **2,645.6s** and sampled token loads **214k–260k**.  
- **Type:** Compute + retry overhead.  
- **Fix:** Scope reduction, partial retries, and stronger deterministic filtering before model invocation.

### 4) Monolithic CI `lint` job
- **Evidence:** CI p50 **620s**; run `25479539893` spent ~**629s** in `lint`.  
- **Type:** Compute overhead.  
- **Fix:** Cache dependencies and split hot-path checks from slower unit suites.

### 5) Hosted runner queueing
- **Evidence:** multiple recent/slow logs mention `Job is waiting for a hosted runner to come online.` in `ci`, `validate`, `review_autofix`, and `promote_main_to_stable`.  
- **Type:** Queueing overhead.  
- **Fix:** Reduce unnecessary fan-out and avoid dispatching long child workflows unless preconditions are already satisfied.

### 6) API polling/probing on no-op paths
- **Evidence:** `cancel_on_pr_close` run `25480772090` spent ~**9.8s** of **13s** in cancellation/rate-limit logic despite no matching runs.  
- **Type:** API overhead.  
- **Fix:** Probe lazily and avoid no-op cancellation loops.

### 7) Merge/conflict overhead
- **Evidence:** limited. `forward_merge_stable_to_main` run `25480793747` was only **22s** and no-op; retry logic exists but is not a major bottleneck in the sampled window.  
- **Type:** Minor.  
- **Fix:** None urgent. Current focus should remain on orchestration/review/analysis.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` release validation chain: **avg 4,134.2s**, **0/5 success**
- `workflow_log_analysis`: **avg 2,645.6s**, token-heavy, retry-prone
- `review_autofix`: **45 cancellations / 85 runs**, long-tail p95 **1,398.8s**
- `ci` `lint` lane: steady **~10 min** wall clock

**Top failure modes**
- Downstream watch timeout/cancellation lineage break (`25445414047`, `25474642232`)
- Missing tracking issue in orchestrate-decompose (`25477674617`)
- Active Codex failures in clarify/orchestrate (`25473125487`, `25473129175`, `25473129346`, `25473127144`)
- New unit/self-test failures (`25469919488`, `25473514248`, `25473697143`, `25474243471`)

**Highest-cost drivers**
- `workflow_log_analysis` summarization tokens: **214,237**, **232,690**, **259,600** in sampled runs
- Broad reviewer model panel exposure in `review_autofix`
- Reruns/cancellations that invalidate expensive downstream work

**Top 3 prioritized actions**
1. **Fix watcher lineage in `test_and_mark_stable` and persist child-run/tracking identifiers.**
2. **Scope down `workflow_log_analysis` and switch to partial retries.**
3. **Create a lighter `review_autofix` path for comment-only / no-edit cases, then reuse that status upstream.**

## Metrics Appendix

### Overall run summary

| Scope | Total runs | Success | Failure | Cancelled | Skipped/other | Failure rate |
|---|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 282 | 13 | 54 | 651 | 1.3% overall |

### Major workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Skipped/other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 5 | 0 | 3 | 2 | 0 | 4134.2 | 3987.0 | 5217.6 |
| `workflow_log_analysis` | 5 | 3 | 1 | 1 | 0 | 2645.6 | 3345.0 | 3538.0 |
| `review_autofix` | 85 | 36 | 0 | 45 | 4 | 357.1 | 47.0 | 1398.8 |
| `ci` | 63 | 60 | 3 | 0 | 0 | 614.1 | 620.0 | 649.9 |
| `validate` | 6 | 6 | 0 | 0 | 0 | 115.3 | 103.5 | 169.3 |
| `orchestrate_poll` | 27 | 27 | 0 | 0 | 0 | 59.1 | 51.0 | 126.3 |
| `clarify` | 203 | 22 | 3 | 0 | 178 | 13.0 | 1.0 | 97.8 |
| `implement` | 172 | 20 | 1 | 6 | 145 | 25.6 | 1.0 | 226.3 |
| `plan` | 171 | 18 | 0 | 0 | 153 | 10.0 | 1.0 | 80.5 |

### Failing runs called out in this report

| Run ID | Workflow family | Job / step | Duration s | Failure signal |
|---|---|---|---:|---|
| `25445414047` | `test_and_mark_stable` | `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` | 3987 | Waited on review chain |
| `25474642232` | `test_and_mark_stable` | `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` | 5395 | Downstream analysis run cancelled |
| `25477674617` | `test_and_mark_stable` | `orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project` | 3737 | Tracking issue missing; plan stalled |
| `25473131401` | `workflow_log_analysis` | `analyze-commit-notify / Run workflow log analysis` | 318 | Analysis failure |
| `25473127144` | `orchestrate` | `orchestrate / Run Codex (decomposer)` | 95 | Active orchestration failure |
| `25473125487` | `clarify` | `clarify / Run Codex` | 115 | Active clarify failure |
| `25473514248` | `ci` | `lint / Implement post-Codex recovery unit tests` | 493 | Unit-test failure |
| `25473697143` | `ci` | `lint / Implement post-Codex recovery unit tests` | 542 | Unit-test failure |
| `25469919488` | `ci` | `lint / Orchestrate poll process unit tests` | 475 | Unit-test failure |
| `25474243471` | `nightly_validation_selftest` | `validation-selftest / Run validation self-test matrix` | 100 | 2/3 fixtures failed |

### Sampled `workflow_log_analysis` token telemetry

| Run ID | Sub-phase | Tokens used |
|---|---|---:|
| `25473131401` | `summarize_unselected_runs` | 214,237 |
| `25470798500` | `summarize_unselected_runs` | 259,600 |
| `25474659590` | `summarize_unselected_runs` | 232,690 |

### AI memory sampled retrieve metrics

| Metric | Value |
|---|---:|
| Sampled `retrieve` ops | 6 |
| Hits (`records_selected > 0`) | 1 |
| Misses (`records_selected = 0`) | 5 |
| Hit rate | 16.7% |
| Avg `estimated_tokens` | 9.3 |
| `plain` keyword method | 4 |
| `llm` keyword method | 1 |
| `none` keyword method | 1 |
| `fail_open: true` observed | 0 sampled |
| `enabled: false` observed | 0 sampled |
| Push retries >1 observed | 1 sampled event |

### Cache observations

| Run ID | Workflow | Cache signal | Observation |
|---|---|---|---|
| `25480631539` | `validate` | `validate-hints` cache | Restore miss and save-path misconfiguration; no cache saved |
| multiple sampled AI runs | `clarify` / `implement` / `orchestrate` | prompt cache env | `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but no direct hit/read/create counters emitted |

### GH API hotspot summary

| Workflow | Evidence run(s) | Hot pattern | Priority |
|---|---|---|---|
| `cancel_on_pr_close` | `25480772090` | `gh api -i /rate_limit` + cancel loop on no-op cases | High |
| `implement` | `25470900024` | repeated issue/comments/PR lookups | High |
| `review_autofix` | recent summaries | GraphQL + workflow dispatch + label mutation chain | Medium |
| `copilot_pull_request_reviewer` | `25480625290` | artifact enumeration in cleanup | Low |

If you want, I can turn this into a shorter leadership summary or a directly actionable backlog with owners, effort, and priority.

## Deep Audit — Workflows & Scripts (2026-05-07)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/review_autofix.yml:4578-4586`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The failure-path PR-state guard sources `gh_helpers.sh` but then bypasses it and runs raw `gh api "repos/.../pulls/${PR_NUMBER}" --jq '.state' ... || echo "open"`. On any transient GitHub API/auth failure, the step coerces the state to `open`, so the downstream failure handler can incorrectly treat an already closed/merged PR as active and proceed to mark linked issues review-blocked. This is directly coupled to the next mutation step, which is gated only on `env.PR_CLOSED != 'true'` at `.github/workflows/review_autofix.yml:4589-4636`.  
  **Recommended fix** — Replace the raw call with the repo-standard guarded pattern, e.g. `gh_retry _safe_gh_jq "repos/.../pulls/${PR_NUMBER}" --jq '.state'`, and treat unreadable state as `unknown`/skip-mutations rather than defaulting to `open`. Reuse the existing pattern already used in `scripts/review_run_reviewers.sh:49` and `scripts/review_apply_fixes.sh:999`.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/comprehensive-test-and-release.yml:127-188,384-444`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — Both dispatch-and-watch blocks discover the “new” workflow run by diffing the branch’s `workflow_dispatch` runs before and after dispatch, then hard-fail when more than one new run appears. The selector only filters by `event == workflow_dispatch` and `head_branch == $GITHUB_REF_NAME` (`:169-177`, `:418-426`), so a second dispatch on the same branch during the polling window makes `NEW_COUNT != 1` and aborts with `Ambiguous run selection`. This is a real TOCTOU race in the current selection logic. [NEEDS VERIFICATION]  
  **Recommended fix** — Add a unique correlation token to the dispatched workflow inputs and match on that token when resolving the child run. If input changes are not acceptable, at minimum narrow selection by `created_at >= COLLECTION_WINDOW_START`, actor, and workflow inputs to reduce collisions.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/review_autofix.yml:497-549`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The post-merge validate path already fetches linked issues and their labels in one GraphQL call at `:497-502`, but the loop re-fetches labels per issue with `gh issue view ... --json labels` whenever `labels_known != true` at `:522-528`. In the fallback path (`:504-515`), every recovered issue enters with `labels: null`, so the execution path becomes one GraphQL call plus one REST call per linked issue just to rediscover the same label state.  
  **Current call count** — `1 + N` calls for `N` linked issues.  
  **Proposed call count after fix** — `1` call total.  
  **Existing batching pattern to extend** — `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`, or the workflow’s own early GraphQL hydration pattern used here.  
  **Recommended fix** — When the fallback extracts issue numbers from PR text, immediately batch-fetch those issues’ labels in one GraphQL call and populate `issue_nodes_json` with the same shape as the primary path. Then delete the per-issue `gh issue view` branch entirely.

- **ID** — `API-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1387-1481,1523-1535`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — Once `ELAPSED >= 600`, the wait-review loop downloads the same job log twice per poll iteration: first into `LOG_FILE` for the editor-noop/reviewer-success shortcuts (`:1422-1481`), then again via `gh_api_safe .../actions/jobs/${JOB_ID_FOR_SIZE}/logs | wc -c` only to compute `LOG_SIZE` (`:1533-1535`). That duplicates the most expensive API payload in the loop.  
  **Current call count** — `2` log-download calls per eligible poll iteration.  
  **Proposed call count after fix** — `1` log-download call per eligible poll iteration.  
  **Existing batching pattern to extend** — The temp-file caching pattern already in the same block, or `gh_retry_to_file` from `scripts/gh_helpers.sh:449-514`.  
  **Recommended fix** — Reuse the first downloaded log file for size measurement (`wc -c < "$LOG_FILE"`) and carry the byte count forward, instead of issuing a second `/logs` request.

- **ID** — `BATCH-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:11305-11350`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — The standalone conflict sweep first lists open PRs once (`gh pr list` at `:11308-11312`), then iterates every candidate with `gh_retry _safe_gh_jq "repos/.../pulls/${S_PR}"` (`:11320-11340`) to recover state/head/mergeability context. Because this code runs inside the poller’s recurring sweep, it creates an `1 + N` GitHub-call pattern on every cycle, which is exactly the per-iteration API shape CLAUDE.md §15 forbids.  
  **Current call count** — `1 + N` calls per sweep for `N` open PRs.  
  **Proposed call count after fix** — `1` GraphQL call for up to 100 open PRs, or `ceil(N / batch_size)` if alias batching is used.  
  **Existing batching pattern to extend** — `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — Replace the `gh pr list` + per-PR REST hydration with one GraphQL query that returns `number`, `state`, `headRefName`, `baseRefName`, and `mergeable` for open PRs. Keep the current REST path only as a fail-open fallback when the batch query fails.

- **ID** — `BATCH-002`  
  **File path** — `scripts/review_rb_judge.sh:146-170,191-208`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — `review_rb_judge.sh` gets linked issue numbers in one GraphQL call (`:146-151`), then loops over those issues and fetches each issue body separately with `_safe_gh_jq "repos/.../issues/${issue_number}"` (`:161-166`). That turns linked-issue context hydration into `1 + N` calls even though the data shape is batchable.  
  **Current call count** — `1 + N` calls for linked-issue context.  
  **Proposed call count after fix** — `1` call total.  
  **Existing batching pattern to extend** — `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`, or the linked-issue GraphQL hydration already used in `.github/workflows/review_autofix.yml:1372-1385`.  
  **Recommended fix** — Expand the initial GraphQL query to return each linked issue’s `number`, `title`, and `body`, then consume that batched result instead of issuing per-issue REST reads.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/orchestrate_poll.yml:67-100; .github/workflows/mark-stable.yml:309-335,457-484`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — The repo carries multiple near-identical inline implementations of `_rl_wait` + `_gh_retry` that parse `/rate_limit`, sleep until reset, then retry with exponential backoff. The copies in `cancel_on_pr_close.yml` and `mark-stable.yml` are especially close. This duplicates retry policy and makes future fixes drift-prone.  
  **Recommended fix** — Centralize on `scripts/gh_helpers.sh` and remove inline copies. The shared module should own `gh_retry <command...>` and `_safe_gh_jq <endpoint...>`; callers to update are `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, and both retry blocks in `mark-stable.yml`.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/review_autofix.yml:3727-3766,4601-4619`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `review_autofix.yml` defines fallback `ensure_label_exists` / `set_issue_phase_label_resilient` logic twice in separate late-stage steps, even though `scripts/label_helpers.sh:102-197` already provides both functions. The duplicated bodies recreate label creation, POST fallback, and phase-label replacement logic.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner of these functions with the existing signatures `ensure_label_exists <label> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Update the “Mark linked issues ready to merge” and “Mark linked issues review-blocked” callers to source that helper only.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/comprehensive-test-and-release.yml:72-103,127-188,320-357,384-444`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — Phase 2 and phase 3 both define the same `gh_api_safe`, `list_dispatch_runs`, “before IDs” baseline logic, and run-resolution polling loop. The ambiguity bug in `BUG-002` would need to be fixed in both places.  
  **Recommended fix** — Extract a shared module, e.g. `scripts/dispatch_and_watch_workflow.sh`, with a function like `dispatch_and_resolve_run_id <workflow_file> <repo> <ref> [input key=value ...]`. Update both comprehensive-test-and-release dispatch phases to call the shared helper.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1201-1585`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The Phase 4 wait-review `run:` block contains `${{ }}` interpolations and is approximately **19,899 characters**, leaving only about **1,101 characters** of headroom before GitHub’s **21,000-character** expression ceiling. This is already within the requested >85% high-risk band and sits in the same workflow family that has previously hit expression-size failures.  
  **Recommended fix** — Extract the entire wait-review loop into an external script under `scripts/` (preferred), passing `TEST_REPO`, `PR_NUMBER`, `BAIT_SHA`, `REVIEW_TIMEOUT`, and `REVIEW_WORKFLOW_FILE` via env. This is the safest option because the block is already dense and still growing.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1671-2076`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The Phase 4b verify-bait-removed `run:` block contains `${{ }}` interpolations and is approximately **17,408 characters**, leaving about **3,592 characters** of headroom. That is above the requested 15,000-character medium-risk threshold and includes multiple inline helper functions plus retry logic, so future edits can push it over the limit quickly.  
  **Recommended fix** — Move the whole verify-bait phase into a dedicated script under `scripts/` or split it into smaller helper steps (install pytest, fetch canary, retry dispatch, verify retry result). Externalization is preferable because this block already carries several embedded shell functions.

No workflow file currently exceeds the 800 KB early-warning threshold. The largest audited workflow files are `review_autofix.yml` at `279,337` characters and `test-and-mark-stable.yml` at `261,375` characters.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/orchestrate_lib.py:988-1405`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `parse_phase_failure_markers`, `evaluate_phase_failure_resume`, `resolve_label_repair_evidence`, and `choose_most_advanced_conclusive_evidence` are implemented here, but the active repo documentation still describes them as “reserved and not yet wired into poller reconciliation” in `agents.md:111-118`. Repo-wide grep only finds self-references inside `scripts/orchestrate_lib.py` plus documentation/report mentions, not live workflow or shell callers. That leaves a substantial contradiction-resolution path dormant and free to drift from real poller behavior.  
  **Recommended fix** — Either wire these helpers into the active poller reconciliation path, or move them behind an explicitly tested CLI/library boundary with contract tests so they cannot silently diverge while unused.

- **ID** — `DEBT-001`  
  **File path** — `.github/workflows/workflow-log-analysis.yml:329-389,754-800,1070-1115`  
  **Severity** — Low  
  **Category tag** — `tech-debt`  
  **Description** — The workflow has three near-identical failure emitters (`emit_log_analysis_phase_failure`, `emit_deep_audit_failure`, `emit_api_redundancy_failure`) that all build the same `AI_PHASE_FAILURE_V1` payload, post a tracking issue comment, create `ai:log-analysis-failed`, and add the label. Only the step name/header text changes. This increases maintenance cost and invites behavioral skew.  
  **Recommended fix** — Extract one shared shell helper with a signature like `emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <summary> <heading>` and reuse it in all three jobs. If you want stronger reuse, move it into `scripts/` beside `gh_helpers.sh`.

- **ID** — `SHELL-001`  
  **File path** — `scripts/validate_process.sh:2043-2048,2706-2710`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Two Codex invocation loops use `cat "${..._PROMPT_FILE}" | codex ...`, which is the exact `SC2002` “useless cat” shape ShellCheck flags. Besides being unnecessary, the extra pipeline stage slightly widens the failure surface under `set -euo pipefail` and makes the prompt file handoff noisier than needed.  
  **Recommended fix** — Replace both pipelines with direct stdin redirection: `codex ... < "${DISCOVER_PROMPT_FILE}"` and `codex ... < "${DIAGNOSE_PROMPT_FILE}"`. That keeps behavior the same while removing the shellcheck violation.

No `TODO`, `FIXME`, or `HACK` markers were found in `.github/workflows/` or `scripts/` during this audit pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BATCH-001, EXPR-001 |
| Medium | 6 | BUG-001, BUG-002, API-001, API-002, BATCH-002, EXPR-002 |
| Low | 6 | DUP-001, DUP-002, DUP-003, DEAD-001, DEBT-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Large |
| API call optimization | 4 | Large |
| Code modularization | 5 | Medium |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-07)

### Safety Tag Legend
`SAFE_TO_MERGE` means the repository-local change is directly actionable without further review; `NEEDS_VERIFICATION` means the overlap is real but a human or follow-up pass must confirm semantics before changing it; `RISKY_SKIP` means the redundancy sits in a retry/poll/race-defense path and must not be auto-implemented even if it looks wasteful.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:188-193`, `.github/workflows/issue_pr_status.yml:280-349`  
  **Current call count** — `2` GraphQL calls on the happy path; `2 + N` if the second batch query fails and the per-issue REST fallback runs for `N` linked issues.  
  **Proposed call count** — `1` GraphQL call on the happy path; keep the existing per-issue REST fallback only if the richer GraphQL payload fails validation.  
  **Endpoint(s)** — GitHub GraphQL `repository.pullRequest(number){ closingIssuesReferences(first:50) }`; GitHub GraphQL `repository { issue(number: ...) }` aliases.  
  **Evidence** — The step first fetches linked issue numbers only, then immediately builds a second batch query to fetch labels/body for those same issues:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    ...
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
  ```
  ```bash
  ORCH_ALIAS_FRAGMENT+=" i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }"
  ...
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ```
  `closingIssuesReferences.nodes` are already issue nodes, so the first query can request `number`, `labels`, and `body` directly and eliminate the second batch request.  
  **Proposed fix** — Extend the first `closingIssuesReferences` GraphQL query in `.github/workflows/issue_pr_status.yml` to return `number`, `labels(first: 50) { nodes { name } }`, and `body`; derive `ISSUE_NUMBERS`, `TRACKING_ISSUES`, and `MANAGED_ISSUES` from that one payload; keep the existing per-issue REST fail-open path only for malformed/partial GraphQL responses.  
  **Safety rationale** — The two calls are in the same `run:` block and read the same PR-scoped issue set, but verification is still required to preserve the current fail-open behavior that protects tracking issues on partial GraphQL failure.  
  **Downstream signal** — Verify that a single enriched `closingIssuesReferences` query can preserve the current `ORCH_BATCH_FAILED` completeness checks and still fall back to the exact same per-issue REST classification path on malformed GraphQL responses.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:284-349`, `.github/workflows/issue_pr_status.yml:447-517`  
  **Current call count** — `N` REST issue fetches in the Telegram alert step for `N` linked issues, after earlier classification already fetched orchestrator-management evidence.  
  **Proposed call count** — `0` REST issue fetches in the alert step.  
  **Endpoint(s)** — Earlier: GitHub GraphQL `repository { issue(number: ...) }` aliases; later: GitHub REST `GET /repos/{repo}/issues/{issue_number}`.  
  **Evidence** — The first step already computes managed/tracking issue sets using labels and body:
  ```bash
  _managed_issues="$(printf '%s' "${ORCH_RESP}" | jq -r '
    .data.repository | to_entries[] | .value | select(. != null) |
    select(((.labels.nodes // []) | map(.name) | index("ai:orchestrator-tracking")) == null) |
    select(
      ((.labels.nodes // []) | map(.name) | index("ai:orchestrator-managed")) != null
      or
      ((.body // "") | contains("Managed by: AI Orchestrator"))
    ) | .number
  ' 2>/dev/null || echo '')"
  ```
  But the later merged-alert step re-fetches each issue body to rediscover the same fact:
  ```bash
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
      IS_ORCHESTRATED="true"
      break
    fi
  done <<< "${LINKED_ISSUE_NUMBERS}"
  ```
  **Proposed fix** — In `Update linked issue labels when PR closes`, export either `MANAGED_ISSUES` or a simple `HAS_ORCHESTRATED_LINKED_ISSUE=true/false` flag to `GITHUB_ENV`; in `Send PR merged Telegram alert`, consume that cached result instead of looping through `_safe_gh_jq` calls.  
  **Safety rationale** — The later step is re-fetching repository data already derived earlier in the same job, but verification is needed because the earlier step also mutates labels/closes issues and the alert step currently re-checks only the body marker.  
  **Downstream signal** — Verify that alert suppression only depends on preexisting orchestrator markers/body text and not on any post-classification mutation from the label/close loop before replacing the per-issue REST reads with an exported boolean or managed-issue list.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:63-86`, `.github/workflows/implement.yml:532-564`  
  **Current call count** — `2` issue fetches per non-skipped implement run.  
  **Proposed call count** — `1` issue fetch.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/issues/{ISSUE_NUMBER}`.  
  **Evidence** — The precheck step fetches issue state and labels:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ```
  Later, the workflow fetches the same issue again for metadata/body/title:
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ```
  The second call is avoidable if the first payload is persisted in a reusable file or step output.  
  **Proposed fix** — Move `Create runtime workspace` ahead of `Precheck approval phase label`, and have that precheck write the fetched issue JSON into `${ISSUE_META_FILE}` for `Fetch issue metadata`, `Validate approval phase label`, and the late failure handler to reuse.  
  **Safety rationale** — The endpoint and issue scope are identical with no intervening GitHub-side mutation, but verification is needed because the fix requires step reordering while preserving the current “skip before install” cost guard.  
  **Downstream signal** — Verify that moving runtime-dir creation ahead of the precheck does not change any skip/install semantics and that every later `ISSUE_META_FILE` consumer tolerates the precheck’s full issue payload format.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/internal-review.yml:98-109`, `.github/workflows/internal-review.yml:112-123`  
  **Current call count** — `2` API calls per `resolve-claude-branch-pr` evaluation.  
  **Proposed call count** — `1` call when an open PR already exists; `2` only on the `no_open_pr` branch.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/pulls?state=open&head={owner}:{ref}`; GitHub REST `GET /repos/{repo}`.  
  **Evidence** — The workflow fetches `default_branch` before it knows whether it will immediately exit:
  ```bash
  existing_pr="$(gh api \
    "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
    --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
  base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
  if [ -n "${existing_pr}" ]; then
    ...
    echo "proceed=false"
    ...
    exit 0
  fi
  ```
  The only downstream consumer is the push-only reusable-workflow call, which is gated on `proceed == 'true'`:
  ```yaml
  review-claude-branch-push:
    needs: resolve-claude-branch-pr
    if: ${{ github.event_name == 'push' && needs.resolve-claude-branch-pr.outputs.proceed == 'true' }}
  ```
  So `base_ref` fetched on the `existing_pr` path is never consumed.  
  **Proposed fix** — Move the `gh api "repos/${REPOSITORY}" --jq '.default_branch'` lookup below the `if [ -n "${existing_pr}" ]; then ... exit 0` branch so it runs only when `proceed=true`.  
  **Safety rationale** — This is the same step, with no intervening mutation or retry boundary, and the fetched `base_ref` is unused on the skip branch because the only downstream job is gated off.  
  **Downstream signal** — Move the default-branch lookup into the `no_open_pr` branch and leave the `existing_pr` early-exit path with only the open-PR lookup.

- **ID** — `DEAD-API-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/internal-issue-pr-status.yml:3-12`, `.github/workflows/issue_pr_status.yml:173-183`, `.github/workflows/issue_pr_status.yml:205-208`  
  **Current call count** — `0-1` extra PR fetch per run, guarded by the “blank PR_DATA” branch.  
  **Proposed call count** — `0`.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/pulls/{PR_NUMBER}`.  
  **Evidence** — The reusable workflow is only wired here from a `pull_request.closed` wrapper:
  ```yaml
  on:
    pull_request:
      types: [closed]
  ...
  jobs:
    sync-status:
      uses: shubhodeep1/coding-workflows/.github/workflows/issue_pr_status.yml@main
  ```
  And the reusable workflow already receives PR title/body from the event:
  ```bash
  PR_TITLE: ${{ github.event.pull_request.title }}
  PR_BODY: ${{ github.event.pull_request.body || '' }}
  ...
  PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
  fi
  ```
  On the in-repo call graph, `PR_TITLE` should already make `PR_DATA` non-blank, so the extra `/pulls/{PR_NUMBER}` read appears unreachable.  
  **Proposed fix** — Remove the `/pulls/${PR_NUMBER}` fallback from `issue_pr_status.yml`, or gate it behind an explicit “non-pull_request caller” condition instead of a blank-string test.  
  **Safety rationale** — The branch looks dead for this repository’s current wrapper path, but verification is required because `issue_pr_status.yml` is reusable and external callers may invoke it under a different event context.  
  **Downstream signal** — Verify that no in-repo or supported consumer caller invokes `issue_pr_status.yml` without a populated `github.event.pull_request.title/body`; only then remove or narrow the `/pulls/${PR_NUMBER}` fallback.

- **ID** — `DEAD-API-003`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/review_autofix.yml:1355-1369`, `.github/workflows/review_autofix.yml:3718-3778`, `.github/workflows/review_autofix.yml:3889-3900`, `.github/workflows/review_autofix.yml:4624-4633`  
  **Current call count** — Up to `3` extra PR fetches per `codex-agent` run, one in each late linked-issue fallback block.  
  **Proposed call count** — `0` extra PR fetches in those late fallback blocks.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/pulls/{PR_NUMBER}`.  
  **Evidence** — Early in the job, PR metadata is fetched once and normalized into `PR_META_FILE`:
  ```bash
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
  ...
  jq '{
    title: (.title // ""),
    body: (.body // ""),
    baseRefName: (.base.ref // ""),
    headRefName: (.head.ref // ""),
    headRepoFullName: (.head.repo.full_name // "")
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```
  But three later steps still carry the same fallback PR re-fetch pattern:
  ```bash
  PR_DATA="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" 2>/dev/null || echo "")"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
  fi
  ```
  Those steps are already gated out of the no-PR claude-branch-review mode, so on the normal path `PR_META_FILE` should already contain the needed title/body.  
  **Proposed fix** — In `review_autofix.yml`, centralize PR title/body fallback once near PR metadata collection, then delete the three late `/pulls/${PR_NUMBER}` fallbacks and treat unreadable `PR_META_FILE` as a local-data warning/hard failure according to the desired fail-open contract.  
  **Safety rationale** — The extra API reads appear dead on the successful normal PR path, but verification is required because the current code may be intentionally retaining a disk-corruption/partial-write fallback after long-running editor/reviewer stages.  
  **Downstream signal** — Verify that no later step truncates/removes `PR_META_FILE` and that the late labeling/failure paths do not intentionally rely on a remote `/pulls/${PR_NUMBER}` reread as recovery before deleting the three duplicate fallbacks.

### Cross-References to Deep Audit Section

- `API-001`: `NEEDS_VERIFICATION` — Agreed; the fallback path still turns one linked-issue batch into `1 + N` label lookups, but the fix must preserve the current fallback shape for recovered issue numbers.
- `API-002`: `RISKY_SKIP` — Agreed, but it sits inside the Phase 4 wait loop of the release gate, so changing call shape there needs manual review of watchdog/race-handling behavior.
- `BATCH-001`: `RISKY_SKIP` — Agreed; it is inside `scripts/orchestrate_poll_process.sh`’s recurring poll/sweep path, which the repo’s safety rules explicitly treat as race-defense code.
- `BATCH-002`: `NEEDS_VERIFICATION` — Agreed; linked-issue body hydration is batchable, but the judge’s current requirement-context fallback path must be preserved exactly before consolidating.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | DEAD-API-001 |
| NEEDS_VERIFICATION | 5 | MERGE-001, REUSE-001, REUSE-002, DEAD-API-002, DEAD-API-003 |
| RISKY_SKIP | 0 | — |

### Implement-Stage Handoff

- `DEAD-API-001`
