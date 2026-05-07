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
