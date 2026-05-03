## Executive Summary

- **`test_and_mark_stable` is the highest-impact issue by far.** In `shubhodeep1/coding-workflows`, the family had **6 runs, 0 successes, 4 failures, 2 cancellations**, with **p50 5,253s** and every sampled failure ending at `e2e-smoke-test → Phase 4b: Verify editor removed bait line` (`25247210528`, `25249170035`, `25252918179`, `25254380200`). Impact: recovering this flow removes the biggest release blocker and can save **75–105 minutes of failed/cancelled release-cycle time per run**. **Confidence: high.**

- **The root cause of the smoke-test failure is concrete and low-risk to address/validate.** Recent run `25265907936` records the RCA: `last_run_diff.patch` wrongly included the injected bait commit, which tripped the editor prompt’s OSCILLATION GUARD and led to empty editor output in review run `25254574828`. The repo now contains a matching fix in `.github/workflows/review_autofix.yml:2263-2285`. Impact: likely converts the failing release test family from **0% to normal pass behavior** once validated on `stable`. **Confidence: high.**

- **`review_autofix` is the biggest recurring cost sink.** Family stats are **60 runs, 34 success, 25 cancelled, p50 47s, p95 2,024s**. Cancelled run `25265763764` spent **533s** and logged **1,781,558 total tokens** on `claude-sonnet-4-20250514` before delivering no completed value. Impact: gating/capping long comment-only review paths can save **hundreds of seconds and ~1.8M tokens on bad runs**. **Confidence: high.**

- **Fast-control workflows are over-fetching Git state.** `orchestrate_poll` is healthy but expensive for no-work cycles: **44/44 success**, yet sampled runs like `25265861566` still take **47s** with `has_work:false`, `fetch-depth: 0`, and broad ref/tag fetches. Impact: trimming fetch scope should save **5–20s per no-work poll**, improving both latency and hosted-runner load. **Confidence: high.**

- **Prompt cache is enabled, but its effectiveness is not measurable in sampled logs.** Review and poll runs show `OPENROUTER_PROMPT_CACHE_DISABLED: false` and `review_autofix` explicitly has a `Pre-assemble static context (cacheable across runs)` step, but sampled telemetry lacks usable `cache_creation_input_tokens` / `cache_read_input_tokens` values. Impact: instrumentation first, then likely **moderate token and retry-latency savings**. **Confidence: medium.**

- **AI memory is partially healthy but uneven by workflow.** In sampled deep dives, reviewer retrievals in `review_autofix` returned **0 records in 2/2 cases**, while implementation retrieval in failed run `25246727158` returned **2 records / 56 estimated tokens**. Impact: improving reviewer retrieval relevance should reduce repeated context expansion and improve consistency, though likely with smaller gains than fixing release/review loops. **Confidence: medium.**

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Validate and lock in the `LAST_RUN_DIFF` fix for smoke-test editor runs**
   - **Evidence:** All failing `test_and_mark_stable` runs (`25247210528`, `25249170035`, `25252918179`, `25254380200`) failed in `Phase 4b`. In `25254380200`, the canary re-fetch shows `::error::Editor failed to remove bait line ...` and `status=bait_remained`. The same run also spent repeated idle windows in `Apply fixes with editor model`. Recent run `25265907936` documents the RCA and points to `.github/workflows/review_autofix.yml:2263-2285`.
   - **Root cause:** `LAST_RUN_DIFF` was polluted by the smoke-test bait commit, so the editor prompt’s OSCILLATION GUARD in `scripts/review_apply_fixes.sh:441-462` suppressed the needed change.
   - **Exact change:** Keep the current first-parent autofix-only diff logic in `.github/workflows/review_autofix.yml:2263-2285`, backport it to the branch that drives release testing if needed, and immediately run a forced `test_and_mark_stable` validation cycle on `stable`.
   - **Estimated time savings:** **45–90 minutes** per failed release cycle, because the current family runs for 4,457–6,478s before failing.
   - **Implementation risk:** **Low.** The fix narrows `LAST_RUN_DIFF` to actual prior autofix commits and preserves existing semantics for normal iteration-2+ cases.

2. **Short-circuit long `review_autofix` comment-only paths before heavy setup**
   - **Evidence:** `review_autofix` family p95 is **2,024s** vs p50 **47s**. Cancelled runs `25265631645` and `25265763764` were both comment-only Claude-branch-review paths, yet still executed setup-heavy steps including `Install uv for Serena`, `Setup Serena MCP server`, memory retrieval, PR metadata collection, and disk cleanup before cancellation.
   - **Root cause:** The workflow pays most of the expensive bootstrap cost before it knows the path will remain comment-only / non-editing.
   - **Exact change:** Move gate evaluation earlier and skip Serena install, reviewer-memory retrieval, static-context assembly, and free-disk steps when `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` resolves to reviewer-panel/comment-only mode.
   - **Estimated time savings:** **3–10 minutes** on affected review runs; more on cancelled runs.
   - **Implementation risk:** **Low to medium.** Safe if the gate decision is already deterministic and available early.

3. **Reduce no-work `orchestrate_poll` Git fetch scope**
   - **Evidence:** `orchestrate_poll` averages **44.3s** with all-success outcomes, but sampled no-work run `25265861566` still uses `actions/checkout@v5` with `fetch-depth: 0`; checkout logs show many refs/tags fetched before the run records `has_work:false`.
   - **Root cause:** Full-history fetch is overkill for a control loop that usually finds no work.
   - **Exact change:** Default poll cycles to `fetch-depth: 1` and fetch only the refs required for tracking-issue evaluation; reserve full-history fetch for branches/paths that truly need merge ancestry or tag walks.
   - **Estimated time savings:** **5–20s per no-work poll**.
   - **Implementation risk:** **Low**, if tag/history-dependent logic is explicitly isolated.

4. **Keep `workflow_log_analysis` deep-audit at `high`, not `xhigh`, and apply the same rule consistently**
   - **Evidence:** In slow run `25254390226`, `step-002-deep-audit.log` explicitly says prior runs hit timeout pressure with `xhigh` and that `high gives ~30–50% per-pass latency cut with negligible impact on audit depth`.
   - **Root cause:** Excessive reasoning level on wide-surface analysis work.
   - **Exact change:** Standardize deep-audit and similar broad summarization passes on `high`; reserve `xhigh` only for narrow, high-stakes root-cause substeps.
   - **Estimated time savings:** **30–50%** on deep-audit passes; on a 5,641s run, that is roughly **28–47 minutes**.
   - **Implementation risk:** **Low**, because the workflow already documents this tradeoff.

5. **Add earlier smoke-test failure exits once editor inactivity crosses a threshold**
   - **Evidence:** In `25254380200`, the smoke test repeatedly polls a review run sitting at `Apply fixes with editor model`, emitting idle lines every ~13s while no progress occurs.
   - **Root cause:** Long poll loops wait for an outcome that is already effectively stuck.
   - **Exact change:** If the editor step remains unchanged for N polls and no file-modifying action appears, fail Phase 4 earlier with a distinct diagnostic status.
   - **Estimated time savings:** **10–25 minutes** on stuck smoke-test runs.
   - **Implementation risk:** **Medium.** Needs careful thresholding to avoid cutting off legitimately long edits.

**Critical-path wins:** items 1, 2, 3.  
**Local/micro-optimizations:** item 5, plus smaller cleanup reductions in review setup.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Cap spend on cancelled comment-only review runs**
   - **Evidence:** Cancelled run `25265763764` used **1,781,558 total tokens** (`input_tokens=1,747,212`, `output_tokens=34,346`) on `claude-sonnet-4-20250514` and then ended cancelled after **533s**. Family-wide, `review_autofix` has **25 cancellations out of 60 runs**.
   - **Root cause:** Expensive review execution starts before the workflow can confidently determine whether a long model run is warranted.
   - **Exact change:** For comment-only / reviewer-panel-only paths, switch to a smaller review envelope: early gate, reduced prompt surface, and a hard timeout/token budget before any editor or multi-pass review escalation.
   - **Estimated savings:** **Up to ~1.8M tokens on individual cancelled runs**; largest total dollar reduction in the dataset.
   - **Quality-risk notes:** Low if full review remains available only for paths that can commit code or block merge.

2. **Lower reasoning effort on non-editing review flows**
   - **Evidence:** Sampled review runs log `REVIEWER_REASONING_EFFORT: xhigh` and `EDITOR_REASONING_EFFORT: xhigh`. In `25265631645` and `25265763764`, these settings appear even on comment-only Claude-branch-review paths that never reach edit/commit.
   - **Root cause:** Uniform `xhigh` reasoning is being applied to flows with low ambiguity and limited output value.
   - **Exact change:** Use `medium` or `high` reasoning for comment-only reviews and only escalate to `xhigh` for merge-blocking code-edit paths or explicit retry diagnoses.
   - **Estimated savings:** Likely **double-digit percentage token reduction** on review_autofix’s long tail.
   - **Quality-risk notes:** Medium; keep escalation-on-demand rather than permanently downgrading all review paths.

3. **Reduce avoidable rerun spend from the smoke-test release loop**
   - **Evidence:** `test_and_mark_stable` has **0 successes in 6 runs** and burns **4,457–6,478s** per attempt before failing/cancelling.
   - **Root cause:** A known deterministic failure in Phase 4b forces expensive full reruns of the release validation path.
   - **Exact change:** Treat the `LAST_RUN_DIFF` bait fix as a release blocker; do not continue iterating on lower-value optimizations until this family passes at least one full cycle.
   - **Estimated savings:** Saves the full downstream compute/token cost of repeated failed release-test runs.
   - **Quality-risk notes:** None; this is corrective, not reductive.

4. **Trim `workflow_log_analysis` summarization scope or delta-process unselected runs**
   - **Evidence:** In `25254390226`, `summarize_unselected_runs` used **226,933 tokens** with model `openai/gpt-5.4-mini` to summarize **83 of 100 targeted** runs. The family average duration is **4,961s**.
   - **Root cause:** Broad re-summarization of many unselected runs on each analysis pass.
   - **Exact change:** Delta-process only newly observed runs or cap unselected-run summarization to the highest-information changes since the last analysis artifact.
   - **Estimated savings:** **100k–200k+ tokens per analysis run**.
   - **Quality-risk notes:** Low if unchanged runs keep their prior summaries.

5. **Stabilize prompt-cache prefixes and expose cache counters**
   - **Evidence:** Review and poll runs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, and `review_autofix` has a `Pre-assemble static context (cacheable across runs)` step, but sampled logs do not expose usable cache read/create counters. Slow analysis logs explicitly note `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`.
   - **Root cause:** Cache exists logically, but measurement and likely prefix discipline are insufficient.
   - **Exact change:** Keep volatile fields (timestamps, run IDs, dynamic warnings) after the cacheable prefix and emit provider cache counters in the job summary/logs.
   - **Estimated savings:** **Moderate recurring savings** on retry-heavy review paths; not yet quantifiable from current telemetry.
   - **Quality-risk notes:** Low.

6. **Do not assume model-env presence equals actual model spend**
   - **Evidence:** `orchestrate_poll` logs show `MODEL_EDITOR: openai/gpt-5.4` and `MODEL_REASONING_EFFORT_JUDGE: xhigh`, but sampled poll runs do not show token usage.
   - **Root cause:** Environment configuration is broader than actual invocation.
   - **Exact change:** Instrument per-step token usage before changing poller model selection.
   - **Estimated savings:** Unknown until measured.
   - **Quality-risk notes:** High risk of over-optimizing the wrong thing without usage counters.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Make the smoke-test editor-bait fix the top release reliability priority**
   - **Failure evidence:** All sampled failures in `test_and_mark_stable` terminate at `Phase 4b: Verify editor removed bait line`, with explicit `status=bait_remained` in `25254380200`.
   - **Root cause category:** Workflow logic / prompt-context contamination.
   - **Exact fix:** Validate the current `.github/workflows/review_autofix.yml:2263-2285` fix on the release branch, and add a regression test that asserts `LAST_RUN_DIFF` never includes `[E2E Smoke Test] inject editor bait line`.
   - **Expected reliability impact:** Largest single improvement; should restore release-test passability if RCA is complete.
   - **Rollback/fail-open considerations:** Safe rollback path is to disable the bait assertion temporarily only if release is blocked, but that should be time-boxed because it weakens signal.

2. **Add retry/fallback for `actionlint` installation**
   - **Failure evidence:** CI run `25249161547` failed in `lint → Install actionlint` with `curl: (22) The requested URL returned error: 502`.
   - **Root cause category:** External dependency download flake.
   - **Exact fix:** Add bounded retries with checksum verification and fallback to a pinned asset/cache path for the same version.
   - **Expected reliability impact:** Removes the only sampled CI failure in a family otherwise at **63/64 success**.
   - **Rollback/fail-open considerations:** Retry-only change is fail-safe; if retries exhaust, current behavior can remain.

3. **Preserve smoke-test labels before deterministic auto-merge can race them**
   - **Failure evidence:** The smoke-test script itself contains explicit comments and error handling for the case where the PR is already closed/merged before bait injection; multiple failing logs show repeated label and PR recheck calls around that race window.
   - **Root cause category:** Cross-workflow race / control-plane ordering.
   - **Exact fix:** Apply or verify `force-review` / smoke-test-required labels before opening the auto-merge window, or hard-block deterministic skip-merge while the e2e smoke test is active.
   - **Expected reliability impact:** Prevents false negatives where the editor never had a chance to run.
   - **Rollback/fail-open considerations:** Prefer fail-closed only for the smoke-test label path; keep normal deterministic merge behavior for non-smoke PRs.

4. **Strengthen fail-open behavior when memory helper scripts are missing**
   - **Failure evidence:** Recent cancelled review run `25265616289` logged `memory helper script missing; skipping run-end failure event`.
   - **Root cause category:** Observability / cleanup robustness.
   - **Exact fix:** Keep the workflow succeeding when memory helpers are unavailable, but emit a structured warning counter and a summary note so missing ledger events are visible.
   - **Expected reliability impact:** Small direct failure reduction, moderate debugging improvement.
   - **Rollback/fail-open considerations:** Should remain fail-open.

5. **Add “no actionable output” diagnosis to implement failures before second wasted attempt**
   - **Failure evidence:** Failed implement run `25246727158` logged memory retrieval and run events, then ended with `phase_failed` and `push_attempts: 2`.
   - **Root cause category:** Agent retry strategy / insufficient early diagnosis.
   - **Exact fix:** After first no-actionable-output attempt, run a lightweight diagnosis branch that checks tool stats, diff availability, and prompt-scope size before re-running the full implementation path.
   - **Expected reliability impact:** Moderate reduction in failed implement reruns and token waste.
   - **Rollback/fail-open considerations:** Keep second attempt available if the diagnosis finds a transient condition.

## AI Memory Health

- **Telemetry was observed** in sampled deep-dive logs. Confirmed operations include:
  - `retrieve`
  - `record-run-event`
  - `processed-command-check`
  - `processed-command-claim`
  - `compact`
  - `summarize_unselected_runs`

### Observed retrieve metrics from directly inspected deep-dive logs
| Workflow / run | Role | records_selected | estimated_tokens | keyword_method | Notes |
|---|---:|---:|---:|---|---|
| `review_autofix` `25265763764` | reviewer | 0 | 0 | none | fail-open retrieve returned no context |
| `review_autofix` `25265631645` | reviewer | 0 | 0 | none | same pattern |
| `implement` `25246727158` | implementation | 2 | 56 | plain | non-zero hit |

### Aggregate from those observed retrieves
- **Retrieve hit rate:** **33.3%** (1 of 3 had `records_selected > 0`)
- **Average estimated tokens:** **18.7**
- **Keyword method distribution:** `none=2`, `plain=1`, `llm=0`

### Health assessment
- **Reviewer memory is underperforming in sampled runs.** Both sampled `review_autofix` retrieves returned zero records, so memory is not helping the highest-cost review flow.
- **Implementation memory is working better.** In `25246727158`, implementation memory retrieved 2 records for only 56 estimated tokens, which is a healthy budget/usefulness ratio.
- **No sampled `fail_open: true` retrieve records were observed.**
- **No sampled `enabled:false` entries were observed.**
- **High push retries were limited but real.** `implement` run `25246727158` logged `record-run-event` with `push_attempts: 2` on `phase_failed`.
- **Compaction looks healthy.** A sampled memory-maintenance summary reported `compact` archiving **2,914 candidates** with successful push and no removals.

### Recommendations
1. Improve reviewer retrieval query formation so `review_autofix` stops returning empty context on the most expensive path.
2. Keep implementation retrieval settings close to current behavior; they appear efficient.
3. Add a simple workflow-level KPI: reviewer retrieve hit rate by family, tracked weekly.
4. Emit a summary warning when a high-cost review run starts with `records_selected=0`.

## GH API Call Audit

### Highest-volume / highest-redundancy patterns

1. **`test_and_mark_stable` repeatedly polls Actions runs and re-fetches PR/issue state**
   - **Evidence:** In `25254380200` and `25252918179`, `e2e-smoke-test` repeatedly calls:
     - `gh api repos/.../actions/runs/{RID}`
     - `gh api repos/.../actions/runs?per_page=50&created=>...`
     - `gh api repos/.../issues/{ISSUE_NUMBER}`
     - `gh api repos/.../issues/{ISSUE_NUMBER}/comments`
     - `gh api repos/.../pulls/${PR_NUMBER}`
   - **Problem:** Run status, issue labels, PR head SHA, and comments are re-read in loops instead of cached cycle-locally.
   - **Concrete change:** Cache PR metadata, issue labels, and latest run listings per smoke-test phase; only refresh when a state transition is required.
   - **Estimated call-count reduction:** **Double-digit REST-call reduction per smoke-test run**.
   - **Rate-limit risk reduction:** **High** in the workflow most likely to churn API calls.

2. **Missed batching in linked-issue / PR metadata lookups**
   - **Evidence:** Recent `review_autofix` run `25265907936` uses GraphQL for `closingIssuesReferences`, then separately fetches `pulls/${PR_NUMBER}` for title/body context.
   - **Problem:** Mixed GraphQL + REST fetches for related state increase call count and fragmentation.
   - **Concrete change:** Fold needed PR fields into the existing GraphQL request where possible and persist them in `$GITHUB_ENV`/job outputs for downstream steps.
   - **Estimated call-count reduction:** **2–4 calls per review run**.
   - **Rate-limit risk reduction:** Moderate.

3. **Copilot PR reviewer duplicates metadata and artifact API work**
   - **Evidence:** In `25265632302`, `Prepare/Get pull request details` uses `github.rest.pulls.get`, while the workflow also paginates `pulls.listFiles`; cleanup later calls `gh api /repos/shubhodeep1/coding-workflows/actions/runs/25265632302/artifacts`.
   - **Problem:** Metadata is fetched in separate stages without obvious reuse, and artifact cleanup always performs additional API work.
   - **Concrete change:** Reuse the initial PR payload and file list across prepare/upload phases; skip artifact-list calls when artifact naming or retention metadata is already known locally.
   - **Estimated call-count reduction:** Small to moderate per run; worthwhile because the workflow is already artifact-heavy.
   - **Rate-limit risk reduction:** Low to moderate.

4. **Rate-limit-aware wrappers exist, but hot paths still over-call**
   - **Evidence:** Smoke-test logs show explicit wrappers for 403/rate-limit backoff; cancel-on-close uses `_rl_wait` with `/rate_limit` checks.
   - **Problem:** Good retry logic is compensating for avoidable call volume.
   - **Concrete change:** Keep current fail-open/retry wrappers, but apply repo hygiene rules more consistently:
     - batch where GraphQL can answer the whole question
     - cache within the workflow cycle
     - avoid per-item REST loops inside polling logic
   - **Estimated call-count reduction:** Best gains come from smoke-test and review flows, not from already-fast maintenance flows.

### Cross-reference to repo API hygiene rules
The repository already points toward the right patterns—**mandatory batching, cycle-local caches, fail-open behavior**—but the sampled hot paths are not consistently following them. The main gap is execution discipline, not lack of helper patterns.

## MCP & Serena Efficiency

- **Observed state in sampled review runs:** Serena is provisioned, but `review_autofix` runs `25265631645` and `25265763764` both log **`No Serena tool usage stats found.`**
- **Observed setup overhead:** `.github/workflows/review_autofix.yml` always includes:
  - `Install uv for Serena`
  - `Setup Serena MCP server`
  - `Pre-assemble static context (cacheable across runs)`
  - `Retrieve reviewer memory context (fail-open)`
- **Observed issue:** This setup happens even on runs that never reach editing and later cancel.

### Findings
1. **Tool bootstrap is not clearly gated by need**
   - Comment-only review paths still pay Serena startup overhead.
   - Recommendation: gate Serena bootstrap behind paths that will actually inspect/edit code.

2. **Stats persistence is weak**
   - Because sampled runs cancel, Serena usage reporting is often absent.
   - Recommendation: write tool-usage stats incrementally during execution, not only at the end.

3. **Use targeted symbol/search flows when Serena is active**
   - For future optimization, prefer:
     - `activate_project`
     - `get_symbols_overview`
     - `find_symbol`
     - `find_referencing_symbols`
     - `search_for_pattern`
   - Avoid broad raw file reads and shell grep churn when Serena is available.

4. **Safe parallelization opportunity**
   - PR metadata collection, check-run failure collection, and cacheable static-context prep are mostly independent and can be parallelized before model invocation.
   - Recommendation: parallelize read-only prep, then serialize only the steps that mutate state or depend on previous outputs.

### Net recommendation
Treat Serena as a **conditional accelerator**, not mandatory baseline overhead. In the sampled window, the biggest gain is from **not starting it unnecessarily**, not from more aggressive use.

## Prompt Cache & Memory System

### Prompt cache
- **Enabled:** Yes, sampled runs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **Cache-oriented workflow design exists:** `review_autofix` has `Pre-assemble static context (cacheable across runs)`.
- **Measurement gap:** Sampled logs do not expose reliable `cache_creation_input_tokens` or `cache_read_input_tokens`; slow analysis logs explicitly note `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`.

### Memory retrieval effectiveness
- Reviewer retrievals are ineffective in sampled `review_autofix` runs.
- Implementation retrieval is lightweight and useful in sampled `implement`.

### Likely cache-fragmentation causes
1. Dynamic run-specific text appearing too early in prompts.
2. Retry flows rebuilding large static prefixes instead of reusing a stable prefix artifact.
3. Mixing warnings/noise/support-ref messages into the prompt prefix.

### Recommendations
1. **Emit cache counters for every AI step**
   - Include cache create/read input tokens in logs and job summaries.
   - **Impact:** enables real optimization; improves cost and latency attribution.

2. **Keep dynamic noise after the cacheable prefix**
   - Move run IDs, timestamps, transient warnings, and fetched diagnostics after the stable instruction/context block.
   - **Impact:** moderate token and latency savings on repeated review/editor invocations.

3. **Separate reviewer and editor cache surfaces**
   - Reviewer and editor have different volatility patterns; separate prefixes prevent one noisy surface from invalidating the other.
   - **Impact:** moderate.

4. **Promote successful implement-memory patterns into review retrieval**
   - The implementation path already retrieves relevant context cheaply.
   - **Impact:** modest token savings and potentially better consistency.

## Orchestrator Health

### What looks healthy
- `clarify`, `plan`, and `orchestrate_clarify_respond` show many **intentional skips**; this appears to be normal slash-command gating rather than failure.
- `orchestrate_poll` is operationally stable: **44/44 success** in the window.

### What hurts
1. **No-work poll cycles are expensive**
   - `orchestrate_poll` typically takes **39–49s** even when `has_work:false`.
2. **Review loop cancellation is high**
   - `review_autofix` has **25 cancellations in 60 runs**, which destabilizes downstream orchestration.
3. **Runner wait is common**
   - Present across CI, review, copilot-reviewer, and poll workflows. Since new infrastructure is off-limits, the safest mitigation is fewer unnecessary runs and less work per run.

### Smallest safe mitigations
- Reduce no-work poll fetch scope.
- Gate comment-only review paths earlier.
- Track and alert on `review_autofix` cancellation rate.
- Validate the smoke-test RCA fix before further tuning.

### Observable indicators to track
- `test_and_mark_stable` success rate
- `review_autofix` cancellation rate
- `orchestrate_poll` p50/p95 duration
- `% of poll cycles with has_work:false`
- reviewer memory retrieve hit rate
- prompt-cache read/create counters once emitted

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan
- Mostly healthy.
- Many runs are skipped quickly, suggesting gating is doing its job.
- Bottleneck type: **minimal**.

### 2. Implement
- Normal successful implement runs are in the **263–297s** range in the sample.
- Failure run `25246727158` shows that when implementation goes wrong, it still spends meaningful AI/runtime budget before failing.
- Bottleneck type: **compute + retry waste**.

### 3. Review / Autofix
- This is the dominant compute bottleneck.
- Evidence:
  - Family average **420s**
  - p95 **2,024s**
  - long successes/cancellations: `25254525024` (**1,314s**), `25265631645` (**487s cancelled**), `25265763764` (**533s cancelled**)
- Bottleneck type: **compute + setup overhead + cancellation waste**.

### 4. Validate / Smoke-test loop
- Biggest end-to-end bottleneck when release validation is involved.
- Evidence:
  - `test_and_mark_stable` average **4,518.7s**
  - p95 **6,422.25s**
  - repeated Phase 4b failures
- Bottleneck type: **retry/rerun amplification + review/editor wait loop**.

### 5. Poll / Orchestrate loop
- Operationally healthy but inefficient for idle cycles.
- Bottleneck type: **queue + checkout/fetch overhead**.

### Ordered bottleneck fixes by end-to-end impact
1. Fix/validate smoke-test bait-removal path.
2. Short-circuit comment-only `review_autofix` before heavy setup/model spend.
3. Reduce no-work poll checkout/fetch scope.
4. Add early diagnosis in implement no-actionable-output failures.
5. Tighten cycle-local GH API caching in smoke tests and review flows.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` release validation loop: **0/6 success**, **p50 5,253s**
- `review_autofix` long-tail compute/cancellation: **25 cancelled / 60 total**, **p95 2,024s**
- `orchestrate_poll` idle overhead: **44.3s avg** despite frequent `has_work:false`
- `ci` is slow but stable: **p50 608s**, **63/64 success**

**Top failure modes**
- Phase 4b bait-removal failure in smoke tests
- transient external install failure (`actionlint` 502)
- implement no-actionable-output / repeated failed attempts
- occasional missing memory-helper cleanup event

**Highest-cost drivers**
- cancelled/long-running `review_autofix`
- repeated failed `test_and_mark_stable` release runs
- `workflow_log_analysis` summarization breadth
- unnecessary full-history fetches in control workflows

**Top 3 prioritized actions**
1. **Validate the existing `LAST_RUN_DIFF` smoke-test RCA fix on `stable` immediately** and do not deprioritize until `test_and_mark_stable` records a clean pass.
2. **Reorder/gate `review_autofix`** so comment-only paths skip Serena setup, reviewer memory retrieval, and high-cost model execution unless they can actually edit or block merge.
3. **Cut idle poll overhead** by switching no-work `orchestrate_poll` cycles away from `fetch-depth: 0` / broad ref fetches.

## Metrics Appendix

### Overall repository window
| Repo | Total runs | Success | Failure | Cancelled | Other | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 318 | 6 | 33 | 643 | 0.6% | 1s | 615s |

### Key workflow-family metrics
| Workflow family | Total | Success | Failure | Cancelled | Avg duration | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 6 | 0 | 4 | 2 | 4518.7s | 5253s | 6422.25s |
| `review_autofix` | 60 | 34 | 0 | 25 | 420.0s | 47s | 2024s |
| `ci` | 64 | 63 | 1 | 0 | 596.9s | 608s | 651.5s |
| `implement` | 173 | 23 | 1 | 6 | 33.1s | 1s | 225.2s |
| `orchestrate_poll` | 44 | 44 | 0 | 0 | 44.3s | 45s | 49s |
| `workflow_log_analysis` | 6 | 6 | 0 | 0 | 4961.2s | 5234.5s | 6025s |

### Key failing runs
| Run ID | Workflow family | Conclusion | Duration | Failure point |
|---|---|---|---:|---|
| `25254380200` | `test_and_mark_stable` | failure | 6049s | `e2e-smoke-test → Phase 4b: Verify editor removed bait line` |
| `25252918179` | `test_and_mark_stable` | failure | 4457s | `e2e-smoke-test → Phase 4b: Verify editor removed bait line` |
| `25249170035` | `test_and_mark_stable` | failure | 6255s | `e2e-smoke-test → Phase 4b: Verify editor removed bait line` |
| `25247210528` | `test_and_mark_stable` | failure | 6478s | `e2e-smoke-test → Phase 4b: Verify editor removed bait line` |
| `25246727158` | `implement` | failure | 184s | `implement / Run Codex implementation` |
| `25249161547` | `ci` | failure | 13s | `lint / Install actionlint` |

### Observed token-heavy runs
| Run ID | Workflow family | Conclusion | Model | Observed tokens |
|---|---|---|---|---:|
| `25265763764` | `review_autofix` | cancelled | `claude-sonnet-4-20250514` | 1,781,558 |
| `25254390226` | `workflow_log_analysis` (`summarize_unselected_runs` op) | success | `openai/gpt-5.4-mini` | 226,933 |

### Observed memory telemetry summary
| Metric | Value |
|---|---:|
| Directly inspected retrieve ops | 3 |
| Retrieve hit rate | 33.3% |
| Avg estimated retrieve tokens | 18.7 |
| `keyword_method=none` | 2 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| Observed zero-record retrieves | 2 |
| Observed `enabled:false` retrieves | 0 |
| Observed `fail_open:true` retrieves | 0 |
| Observed high push retry sample | `implement` `25246727158` with `push_attempts: 2` |

### GH API hotspot summary
| Workflow / run | Pattern | Evidence |
|---|---|---|
| `test_and_mark_stable` / `25254380200`, `25252918179`, `25247210528` | repeated run-status polling | repeated `gh api repos/.../actions/runs/{RID}` |
| same | repeated run-list scans | `actions/runs?per_page=50&created=>...` |
| same | repeated PR/issue refreshes | issue, comments, labels, and PR head refetches |
| `review_autofix` / `25265907936` | mixed GraphQL + REST for related PR data | `closingIssuesReferences` GraphQL plus separate PR REST fetch |
| `copilot_pull_request_reviewer` / `25265632302` | duplicated PR/artifact metadata calls | `github.rest.pulls.get`, paginated file listing, artifact API lookup |

If you want, I can turn this into a shorter exec-ready action plan with owners, priority, and “change first / measure next” sequencing.

## Deep Audit — Workflows & Scripts (2026-05-03)

### Section 1: Bug & Correctness Sweep

Audited scope: 34 workflow files under `.github/workflows/` and 61 repository scripts under `scripts/`. Findings below are the material defects and high-value correctness/security risks.

#### BUG-001
- **File path** — `.github/workflows/review_autofix.yml:848-915,3237-3244`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — The `EDITOR_CHANGES_LOST` false-positive downgrade path is wired to a helper that is never bootstrapped for consumer-repo runs. The workflow stages support scripts into `${SUPPORT_SCRIPTS_DIR}` and explicitly enumerates `REQUIRED_BOOTSTRAP_SCRIPTS`, but that list does **not** include `detect_editor_changes_lost.sh`. Later, the guard hardcodes `recheck_script="${GITHUB_WORKSPACE}/scripts/detect_editor_changes_lost.sh"` and silently skips the downgrade when the file is not executable. In this repo the script exists locally, but in the consumer-repo execution path the bootstrap destination is the temp support dir, not `GITHUB_WORKSPACE/scripts`, so the guard is effectively disabled and false-positive “editor claimed changes but no commit was produced” failures can reappear.
- **Recommended fix** — Add `detect_editor_changes_lost.sh` to the bootstrapped support-script set and resolve it through the existing support-root convention (`${SUPPORT_SCRIPTS_DIR}/detect_editor_changes_lost.sh`), not `GITHUB_WORKSPACE`. That matches how `post_review_comment.sh`, `review_apply_fixes.sh`, and other review helpers are already sourced.

#### SEC-001
- **File path** — `scripts/memory_helpers.sh:47-95`; representative authenticated-origin setup sites include `.github/workflows/clarify.yml:145-148`, `.github/workflows/plan.yml:180-182`, `.github/workflows/orchestrate.yml:71-73`, `.github/workflows/review_autofix.yml:790-796`, `.github/workflows/validate.yml:160-169`
- **Severity** — High
- **Category tag** — `security`
- **Description** — `memory_ensure_branch` reads `origin` with `git remote get-url origin`, then reuses that URL in a temp repo and runs `git push origin "${branch}" 2>&1`. Multiple workflows first rewrite `origin` to an authenticated URL of the form `https://x-access-token:${GH_TOKEN}@...`. If ai-memory branch bootstrap fails, git stderr can include the authenticated remote, which risks credential exposure in Actions logs. The helper is fail-open, so this path is specifically exercised under failure conditions rather than being impossible. [NEEDS VERIFICATION]
- **Recommended fix** — Strip credentials from `origin_url` before reusing it, and pass auth separately via the existing workflow credential state or an explicit `http.extraheader`/credential helper. Also avoid emitting raw `git push` stderr when the remote may contain embedded credentials. Centralizing remote construction behind the existing `SERVER_HOST="${GITHUB_SERVER_URL#https://}"` pattern would keep auth handling consistent.

#### BATCH-001
- **File path** — `scripts/orchestrate_poll_process.sh:6327-6411`
- **Severity** — High
- **Category tag** — `api-batching`
- **Description** — The standalone stall scan still performs per-tracking-issue and per-label API walks inside the main poll tick. Specifically, it fetches tracking-issue comments one issue at a time at lines 6329-6344, then issues seven separate `gh issue list --label ...` calls at lines 6356-6359 before iterating candidates. That is exactly the kind of per-iteration API fan-out CLAUDE.md §15 forbids, and it sits next to existing GraphQL batching helpers (`_fetch_standalone_marker_issues_graphql`, `_fetch_candidate_issue_details_graphql`) that already solve the same shape elsewhere.
- **Recommended fix** — Batch both prefetches. Extend `_fetch_candidate_issue_details_graphql` with a tracking-issue mode (or add `_fetch_tracking_issue_state_graphql`) so tracking comments/state are fetched in aliased GraphQL batches, and replace the seven label-specific `gh issue list` calls with one batched GraphQL/search helper that returns all open candidate issue numbers by phase label.
  - **Current call count** — `T + 7 + M` logical calls per poll tick, where `T` = tracking issues and `M` = fallback cache misses.
  - **Proposed call count** — `ceil(T / 25) + 1 + M`.
  - **Existing batching pattern to extend** — `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql` and `::_fetch_standalone_marker_issues_graphql`.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path** — `.github/workflows/issue_pr_status.yml:322-349,503-512`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The workflow classifies linked issues into `TRACKING_ISSUES` / `MANAGED_ISSUES` earlier in the same execution path, but the later Telegram-alert gate re-fetches each linked issue body one-by-one via `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""'` just to detect orchestrator ownership. That is a straight cache miss: the workflow already had the data needed to know whether the issue is orchestrator-managed.
- **Recommended fix** — Persist the earlier classification result and reuse it in the alert step instead of re-downloading issue bodies. The simplest fix is to consult `MANAGED_ISSUES`/`TRACKING_ISSUES`; the more general fix is to persist the earlier `_orch_meta` JSON into a temp file for downstream consumers.
  - **Current call count** — `N` extra `GET /issues/{n}` calls per merged-PR execution path.
  - **Proposed call count** — `0`.
  - **Existing batching pattern to extend** — Reuse the existing `ORCH_RESP` GraphQL batch in this workflow, following the same cache-first shape used by `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql`.

#### API-002
- **File path** — `scripts/check_external_branch_advance.sh:175-185`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The branch-advance detector makes one `gh api repos/.../commits/{sha}` call per advancing commit in `self_subject_shas`. The script comments that the set is “usually tiny,” but this is still an API call inside a loop in a review hot path, and the result shape is narrow: only `author.login` and `committer.login` are needed.
- **Recommended fix** — Batch commit attribution lookups when more than one SHA is present. A small GraphQL alias query over the advancing SHAs would preserve the current fail-open semantics while removing the per-SHA looped REST pattern.
  - **Current call count** — `K` commit API calls, where `K` = number of advancing SHAs.
  - **Proposed call count** — `1` when `K > 1`, otherwise keep the single-call fast path.
  - **Existing batching pattern to extend** — The aliased-GraphQL batching style already used in `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql`. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path** — `.github/workflows/clarify.yml:161-235`; `.github/workflows/plan.yml:189-255`; `.github/workflows/orchestrate.yml:132-205,262-325`; `.github/workflows/orchestrate_clarify_respond.yml:204-275`; `.github/workflows/orchestrate_poll.yml:213-285`; `.github/workflows/review_autofix.yml:798-915`; `.github/workflows/validate.yml:185-282`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The repo has at least three near-identical implementations of “resolve support ref → checkout `.codex-workflow-src` → fallback to `main` → stage scripts/prompts into runtime locations.” The same bootstrap logic is duplicated across most primary workflows, with only the required file lists changing. This is now large enough that any support-fetch bug, auth change, or path-resolution fix must be patched in many places, and the repo has already drifted into checkout-based, `git clone`-based, and `copy_from_ref_or_local` variants.
- **Recommended fix** — Move the bootstrap into one owner module, preferably a new composite action (e.g. `.github/actions/stage-workflow-support`) or a new shell helper (e.g. `scripts/fetch_workflow_support.sh`).
  - **Suggested signature** — `fetch_workflow_support <required_scripts_csv> <optional_scripts_csv> <required_prompts_csv> <dest_root> [script_ref]`
  - **Callers to update** — `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `orchestrate_poll.yml`, `review_autofix.yml`, `validate.yml`, and any remaining support-staging jobs in `issue_pr_status.yml` / `validation-improvements-intake.yml`.

#### DUP-002
- **File path** — `.github/workflows/mark-stable.yml:456-483`; `.github/workflows/review_autofix.yml:1289-1327`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — Two workflows re-implement local rate-limit-aware `gh` retry wrappers even though the repo already maintains `scripts/gh_helpers.sh` as the canonical implementation. The inline versions have already drifted: they do not share the helper’s permanent-failure short-circuiting, Telegram cooldown handling, breaker-file semantics, or temp-file hygiene.
- **Recommended fix** — Remove the inline wrappers and source the canonical helper instead.
  - **Suggested owner** — `scripts/gh_helpers.sh`
  - **Suggested function signature** — existing `gh_retry "$@"` and `gh_retry_to_file <outfile> "$@"`
  - **Callers to update** — `mark-stable.yml` “Dispatch stable release event” and `review_autofix.yml` “Collect PR metadata”.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1448`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block is already large enough to be expression-limit sensitive. The interpolated block is about **16,626 characters**, leaving only about **4,374 characters** of headroom before GitHub’s hard **21,000-character** template-expression limit. This block is a known growth magnet: it contains the rate-limit wrapper, run polling, live-log probing, reviewer-count shortcut, editor-noop shortcut, and inactivity diagnostics in one interpolated shell body.
- **Recommended fix** — Extract the wait loop into an external script (preferred: `scripts/wait_for_review_run.sh`) and pass the few dynamic values through `env:`. If keeping it inline, split the live-log shortcut logic and the activity-probe logic into separate steps so future diagnostics do not push the block over the limit.
  - **Estimated current size** — ~16,626 chars
  - **Headroom remaining** — ~4,374 chars

No workflow file exceeded the 800 KB early-warning threshold; the largest audited workflow was `review_autofix.yml` at 271,414 characters.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001
- **File path** — `.github/workflows/implement.yml:2514-2524`; `.github/workflows/review_autofix.yml:4090-4098`; `scripts/review_commit_changes.sh:448-456`; `scripts/review_conflict_resolve.sh:852-854`; `scripts/review_rb_judge.sh:584-592`; `scripts/orchestrate_poll_process.sh:9739-9748`
- **Severity** — Medium
- **Category tag** — `consistency`
- **Description** — Several push/update paths hardcode authenticated remotes against `github.com`, while other workflows correctly derive the host from `GITHUB_SERVER_URL` and `SERVER_HOST`. That makes the repo internally inconsistent and breaks portability for GitHub Enterprise / non-`github.com` deployments in exactly the write paths that matter most: implement push, autofix push, conflict resolution, judge fixes, and orchestrator follow-up fixes.
- **Recommended fix** — Centralize authenticated-origin setup behind one shared helper (for example `set_authenticated_origin <token> <repo> [server_host]` in `scripts/gh_helpers.sh`) and replace the hardcoded `github.com` literals with the existing `SERVER_HOST="${GITHUB_SERVER_URL#https://}"` pattern already used in `clarify.yml`, `plan.yml`, `orchestrate.yml`, and `validate.yml`.

#### DEAD-001
- **File path** — `scripts/review_issue_ledger.sh:866-918`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `CURRENT_FLOOR` is declared and populated for every parsed current issue, but there is no read of that associative array later in the file. A full-text scan only finds the declaration and assignment, and ShellCheck surfaces this as unused state. In an already-complex ledger script, dead per-issue state makes the control flow harder to reason about and suggests abandoned classification logic.
- **Recommended fix** — Either remove `CURRENT_FLOOR` entirely or thread it into the final ledger/rendering logic if floor-category output is still intended.

No in-scope `TODO` / `FIXME` / `HACK` markers were found in the audited workflow/script set.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, SEC-001, BATCH-001 |
| Medium | 5 | API-001, API-002, DUP-001, DUP-002, EXPR-001, CONSIST-001 |
| Low | 1 | DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 8 | Large |
| Expression size reduction | 2 | Small |
| Medium/Low fixes | 4 | Small |
