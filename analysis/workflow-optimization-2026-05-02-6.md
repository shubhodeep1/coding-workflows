## Executive Summary

- **The highest-impact latency problem is the release-validation path, not CI.** `test_and_mark_stable` averaged **2,558s** with **0/5 successful runs** and a **p95 of 3,562s**; cancelled deep-dive run `25245004057` spent much of its time in repeated downstream polling and runner waits across sub-jobs. **Estimated impact:** 20–40 minutes saved on affected release cycles. **Confidence:** high.
- **The most urgent reliability issue is implement-stage no-op failure.** Multiple failed `implement` runs, including `25246727158`, died in `implement / Run Codex implementation` after **two consecutive “no actionable output” attempts**, despite spending ~3 minutes in the job. **Estimated impact:** cut implement failures by a large share of the current **4.86%** family failure rate and save ~2–3 minutes per failed attempt. **Confidence:** high.
- **`review_autofix` is the biggest sampled AI cost center and a major cancellation sink.** It has **36 cancellations out of 66 runs**, **p95 1,588s**, and deep-dive evidence from run `25237552686` shows broad multi-model review plus rate-limit pressure; the workflow-log-analysis run `25246650500` reports ~**259** `gh api` hits and a **58,177-token** consolidator pass for that review flow. **Estimated impact:** 30–60% token/cost reduction on low-complexity reviews, plus fewer superseded cancellations. **Confidence:** medium-high.
- **Workflow-log-analysis is itself expensive enough to warrant optimization.** Deep-dive run `25246650500` took **4,828s**, and its embedded report recorded a single summarization pass of **153,540 tokens**. **Estimated impact:** major cost reduction on analysis runs and lower turnaround for telemetry reporting. **Confidence:** high.
- **Runner wait and full-checkout overhead are recurring across otherwise-light workflows.** Recent runs `25247664026` (`orchestrate_poll`, 42s) and multiple CI runs around **606–637s** show visible hosted-runner wait; the poller also used `actions/checkout@v5` with `fetch-depth: 0` despite doing lightweight coordination work. **Estimated impact:** seconds for poller-class workflows, minutes from reduced queue churn across chains. **Confidence:** medium.
- **AI memory is functioning but underpowered, not broken.** Deep-dive logs contained **122** structured `AI_MEMORY_TELEMETRY` records; retrieve hit rate was **81.8% (18/22)** with very low average retrieval size (**36.9 estimated tokens**), but all sampled retrievals used `plain` or `none` keywording, and several zero-hit retrieves appeared in review/log-analysis flows. **Estimated impact:** modest speed/cost improvement and better consistency if retrieval quality improves. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Collapse `test_and_mark_stable` polling loops into fewer, run-ID-driven waits
- **Evidence:** Workflow family `test_and_mark_stable` averaged **2,558.2s**, `p50=1,999s`, `p95=3,562.2s`, with **0 successes in 5 runs**. Deep-dive cancelled run `25245004057` ran **3,629s** and showed repeated downstream status polling such as `gh api "repos/${TEST_REPO}/actions/runs/${NEW_ID}"` every ~15–20s, plus repeated `Waiting for a runner to pick up this job...` messages across child jobs.
- **Root cause:** The release-validation path serializes multiple long-lived downstream checks and pays both API polling cost and runner queue latency repeatedly.
- **Exact change:**  
  1. Replace tight fixed-interval polling with run-ID aware exponential backoff or phase-aware polling.  
  2. Stop polling once a downstream run reaches a terminal state instead of continuing through label/phase checks.  
  3. Add a stale-phase timeout for known blockers like `ai:implementing`/phase waits.
- **Estimated time savings:** **20–40 minutes** per affected stable-release cycle.
- **Implementation risk:** **Low-medium**; behavior stays backward-compatible if terminal-state detection remains fail-open.

### 2. Fail fast on first no-op Codex attempt in `implement`, then reroute with a smaller recovery prompt
- **Evidence:** Failed run `25246727158` in `implement / implement / Run Codex implementation` logged:  
  - attempt 1 warning that Codex announced edit/apply_patch but produced no changes,  
  - `serena.activate_project(...)`,  
  - same warning on attempt 2,  
  - `Codex produced no actionable output 2 attempts in a row`,  
  - `Codex bailed: 2 consecutive attempts with no actionable output`.  
  The prompt region was also visibly bloated with duplicated instruction content.
- **Root cause:** Expensive retries are repeating nearly the same large context after an exploratory/no-op turn instead of switching strategy.
- **Exact change:**  
  1. After the first no-op attempt, stop the normal retry path.  
  2. Re-issue a compact recovery prompt containing only task summary, changed-files target list, and “produce patch or explicitly say blocked.”  
  3. If still no-op, fail immediately with structured diagnostics rather than another full-context pass.
- **Estimated time savings:** **90–150s** per failed implement run; also shortens mean time to actionable failure.
- **Implementation risk:** **Low**; it only changes retry behavior after an already-bad first attempt.

### 3. Gate trivial `review_autofix` paths before full reviewer fanout
- **Evidence:** `review_autofix` has **66 runs**, **36 cancelled**, `p50=41s`, but `p95=1,588.25s`. Deep-dive success run `25237552686` lasted **2,938s** with broad reviewer model setup; recent run `25247341459` showed a docs-only skip decision (`skip=true reason=docs_only`) in 42s, proving early gating can work.
- **Root cause:** Some low-value or tiny diffs still enter expensive review preparation or are superseded after work has already begun.
- **Exact change:**  
  1. Run file-count/type gate before heavy environment prep and model fanout.  
  2. For docs-only, one-file, or formatting-only diffs, route to a single-pass lightweight review.  
  3. Only enable full reviewer ensemble when code-risk heuristics trigger.
- **Estimated time savings:** **5–25 minutes** on large review runs; **minutes** of avoided waste on cancelled runs.
- **Implementation risk:** **Low-medium**; depends on keeping the gate conservative.

### 4. Remove full-history checkout from lightweight orchestrator/poller flows
- **Evidence:** Recent `orchestrate_poll` success run `25247664026` finished in **42s** but used `actions/checkout@v5` with **`fetch-depth: 0`**, even though visible work was primarily auth/setup and polling.
- **Root cause:** Full fetch is overkill for a coordination workflow that appears not to require full git history.
- **Exact change:** Set `fetch-depth: 1` or skip checkout entirely if the workflow only reads API state and local scripts bundled with the workflow.
- **Estimated time savings:** **5–20s** per poller run and reduced network/runner load.
- **Implementation risk:** **Low**, provided no later step requires tags/history.

### 5. Reduce queue amplification in chained workflows
- **Evidence:** CI runs repeatedly show hosted-runner wait (`25247361216`, `25247341464`, `25247334589`), and stable/review chains multiply that wait across child jobs. Many `clarify`, `plan`, and `implement` runs also skip in **0–2s**, implying trigger fanout is high relative to actual executed work.
- **Root cause:** The pipeline creates many small workflows, some of which only evaluate guards, each paying orchestration overhead.
- **Exact change:** Coalesce guard-only workflows where possible, or move cheap eligibility checks earlier in the parent workflow before dispatching children.
- **Estimated time savings:** **Seconds per event**, compounding to meaningful queue reduction during busy periods.
- **Implementation risk:** **Medium**; requires careful workflow trigger hygiene.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Shrink `review_autofix` model breadth and reasoning depth on low-risk diffs
- **Evidence:** Deep-dive `review_autofix` run `25237552686` used `MODEL_EDITOR: openai/gpt-5.3-codex`, `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`, and a six-model `REVIEWER_MODELS` fanout with mostly `xhigh` reasoning. Deep-dive analysis run `25246650500` attributes **58,177 consolidator tokens** and ~**259** `gh api` log hits to that review flow.
- **Root cause:** The workflow applies high-end reasoning and broad model fanout too often, including on cases that can be classified cheaply.
- **Exact change:**  
  1. Use a single primary reviewer plus one fallback on small or deterministic diffs.  
  2. Downgrade reviewer/editor reasoning from `xhigh` to `medium/high` unless risk heuristics trigger.  
  3. Skip second-pass consolidation when the first pass finds no actionable comments.
- **Estimated savings:** **30–60%** tokens and noticeable runtime reduction on low/medium-risk reviews.
- **Quality-risk notes:** Keep full-depth review for security-sensitive, multi-language, or high-churn diffs.

### 2. Make implement retries surgical instead of replaying bloated prompts
- **Evidence:** Failed implement run `25246727158` repeated large instruction/context blocks before ending in no-op failure. The second attempt did not convert exploration into a patch.
- **Root cause:** Retry prompts preserve too much static and duplicate context, inflating token use without increasing actionability.
- **Exact change:** Build retry prompts from a stable prefix plus a tiny delta: prior attempt result, target files, and explicit action contract.
- **Estimated savings:** Likely **tens of thousands of prompt tokens** across repeated implement failures; also reduces runtime.
- **Quality-risk notes:** Low, if the compact retry retains explicit file/task anchors.

### 3. Cap unselected-run summarization in `workflow_log_analysis`
- **Evidence:** Deep-dive run `25246650500` recorded a single summarization pass costing **153,540 tokens**.
- **Root cause:** Unselected-run widening is valuable, but current summarization volume is large enough to dominate analysis cost.
- **Exact change:**  
  1. Cap summary generation by family and recency.  
  2. Skip summarization for runs with identical guard-only outcomes.  
  3. Reuse existing `log_summary` content instead of regenerating equivalent summaries.
- **Estimated savings:** **Very high** for analysis runs; likely the single biggest pure token reduction opportunity.
- **Quality-risk notes:** Moderate; preserve full analysis for failures, slow runs, and families with regressions.

### 4. Stabilize prompt prefixes to improve prompt-cache reuse
- **Evidence:** Sampled runs show repeated large agent/system blocks and dynamic contextual noise in implement/review paths; prompt cache was enabled in deep-dive review logs, but sampled raw logs did not surface hit-rate counters.
- **Root cause:** Dynamic headers, duplicated instruction bodies, and volatile metadata placement fragment cacheable prefixes.
- **Exact change:**  
  1. Keep invariant instructions first and byte-stable.  
  2. Move volatile fields (timestamps, IDs, comments) to the tail.  
  3. Deduplicate agent/AGENTS/system text before prompt assembly.
- **Estimated savings:** **Moderate** token and latency reduction on repeated workflows, especially `implement`, `review_autofix`, and `workflow_log_analysis`.
- **Quality-risk notes:** Low; this is packaging, not behavior.

### 5. Avoid optional heavy setup on skip/gate-only paths
- **Evidence:** Many `clarify`, `plan`, and `implement` runs skip in **0–2s**; some successful review/autofix runs still show environment prep and artifact work before discovering little to do.
- **Root cause:** Expensive prep can happen before the workflow knows it truly needs AI/model/tool execution.
- **Exact change:** Move AI tool setup, artifact download, and optional MCP initialization behind the same gate used for substantive execution.
- **Estimated savings:** Small per run, but high aggregate because guard-only runs are numerous.
- **Quality-risk notes:** Low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Add a first-class recovery path for implement no-op failures
- **Failure evidence:** Nine implement-family failures in the window; deep-dive failures including `25246727158`, `25243569299`, `25237690797`, `25237704374`, `25243564804`, `25245085089`, `25244127789`, `25245077011`, and `25244121942` all failed in `implement / implement / Run Codex implementation`.
- **Root cause category:** Agent/orchestrator recovery gap after non-actionable model output.
- **Exact fix:**  
  1. Detect “announced edit but no file change” as a typed failure mode.  
  2. Retry once with compact patch-only prompt.  
  3. If still no-op, fail with structured output and mark the issue for manual follow-up instead of consuming another full attempt.
- **Expected reliability impact:** Material reduction in the `implement` family’s **4.86%** failure rate and fewer ambiguous issue comments.
- **Rollback/fail-open considerations:** Safe to fail-open to current behavior behind a flag until confidence is established.

### 2. Fix or quarantine failing nightly validation self-test fixtures
- **Failure evidence:** Run `25242537588` failed in `validation-selftest / Run validation self-test matrix` with `fixtures=3 passed=1 failed=2`.
- **Root cause category:** Broken validation fixtures/test expectations.
- **Exact fix:** Split fixture outcomes into separate matrix jobs and quarantine the two failing fixtures until fixed, so one bad scenario does not fail the whole nightly signal path.
- **Expected reliability impact:** Restores nightly signal quality immediately and reduces false-alarm noise.
- **Rollback/fail-open considerations:** Low risk; re-enable quarantined fixtures individually after repair.

### 3. Add stale-heartbeat cleanup for long-lived release/test phases
- **Failure evidence:** `test_and_mark_stable` had **0 successes in 5 runs**; cancelled run `25245004057` spent long periods waiting on downstream state and labels.
- **Root cause category:** Stuck coordination / stale state in orchestration.
- **Exact fix:** Record phase heartbeat timestamps and automatically break/rewire waits when no phase transition occurs within a bounded interval.
- **Expected reliability impact:** Fewer indefinite waits and fewer operator cancellations.
- **Rollback/fail-open considerations:** Use conservative timeout defaults and emit warnings before forced transition.

### 4. Reduce GH API saturation in review flows
- **Failure evidence:** Deep-dive `workflow_log_analysis` run `25246650500` recorded HTTP 429/secondary-rate-limit handling for the heavy review path around run `25237552686`; recent `25247341459` also showed `gh api graphql`, `gh workflow run`, and `gh issue edit` in loop.
- **Root cause category:** API pressure from repeated per-item or per-phase calls.
- **Exact fix:** Batch PR/issue data fetches, memoize within a run, and back off polling intervals after the first few checks.
- **Expected reliability impact:** Lower rate-limit risk and fewer soft failures/retries in review/autofix.
- **Rollback/fail-open considerations:** Keep fail-open behavior on non-critical metadata fetches.

### 5. Persist cycle-local issue/PR metadata to avoid inconsistent refetches
- **Failure evidence:** In failing implement runs, issue precheck and later issue/comment metadata were fetched again in the same cycle.
- **Root cause category:** Redundant network fetches and race-prone state refresh.
- **Exact fix:** Fetch issue, PR, labels, and comments once early in the job and persist as cycle-local artifacts/JSON for later steps.
- **Expected reliability impact:** Lower probability of race conditions and lower API noise.
- **Rollback/fail-open considerations:** Refresh only on explicit mutation boundaries.

## AI Memory Health

Deep-dive logs did contain structured `AI_MEMORY_TELEMETRY:` entries, so memory telemetry was observed.

- **Total structured telemetry records observed:** **122**
- **Operation mix:**
  - `record-run-event`: **49**
  - `retrieve`: **22**
  - `processed-command-check`: **20**
  - `processed-command-claim`: **19**
  - `summarize_unselected_runs`: **8**
  - `record-candidate`: **4**

### Retrieve effectiveness
- **Retrieve count:** **22**
- **Hit rate:** **81.8%** (`18/22` had `records_selected > 0`)
- **Average `estimated_tokens`:** **36.9**
- **Estimated token budget comparison:** no explicit retrieve budget field was surfaced in sampled telemetry, so budget adherence cannot be measured directly.
- **`keyword_method` distribution:**
  - `plain`: **18**
  - `none`: **4**
  - `llm`: **0**

### Flags
- **Zero-hit retrieves:** **4**
  - one in `review_autofix` run `25237552686`
  - two in `workflow_log_analysis` run `25246650500`
  - one in `workflow_log_analysis` run `25246056978`
- **`fail_open: true` entries:** **0**
- **`enabled: false` entries:** **0**
- **High push retry counts (`push_attempts > 1`):** **3**
  - implement run `25246727158` on `phase_failed`
  - implement run `25244121942` on `phase_started`
  - implement run `25243569299` on `phase_started`

### Assessment
- The memory system is **enabled and mostly healthy**, with a good hit rate and low retrieval-token overhead.
- The main weakness is **retrieval sophistication**, not availability: all sampled retrievals were `plain`/`none`, with no `llm` keywording.
- Zero-hit retrievals cluster in high-cost analytic/review paths, where better recall would be most valuable.

### Recommendation
- Keep retrieval cheap by default, but add a second-stage richer retrieval only for high-cost flows (`review_autofix`, `workflow_log_analysis`) when initial `plain` retrieval returns zero records.
- Track two operational indicators going forward:
  1. zero-hit retrieve rate by workflow family,
  2. memory push retries per run.

## GH API Call Audit

### Highest-volume patterns

#### 1. `review_autofix` is the main GH API hotspot
- **Evidence:** Deep-dive analysis run `25246650500` attributed about **259** `gh api` log hits to heavy review run `25237552686`, with rate-limit handling observed. Recent success run `25247341459` also showed `gh api graphql` plus `gh workflow run` and `gh issue edit` invoked in a loop.
- **Problem pattern:** Repeated per-PR/per-issue calls and looped mutations during review completion/cleanup.
- **Recommendation:**  
  - Batch GraphQL fetches for PR metadata, closing issues, labels, and linked issue references into one request.  
  - Cache results within the workflow invocation.  
  - Collapse multiple `gh issue edit` operations into a single edit.
- **Estimated reduction:** **30–50%** fewer GH API calls in heavy review runs, plus lower secondary rate-limit exposure.

#### 2. `test_and_mark_stable` uses repetitive run-status polling
- **Evidence:** Run `25245004057` repeatedly queried downstream run state via `gh api "repos/${TEST_REPO}/actions/runs/${NEW_ID}"`.
- **Problem pattern:** Unbatched per-run polling in tight intervals.
- **Recommendation:**  
  - Increase poll interval over time.  
  - Cache terminal responses.  
  - If multiple downstream runs are active, fetch them in a batched listing call where feasible rather than one-by-one repeated gets.
- **Estimated reduction:** Large reduction in polling calls for long release validations.

#### 3. Artifact cleanup/retrieval is still single-purpose and repetitive in copilot review workflows
- **Evidence:** Runs `25247335697` and `25247330108` showed `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<id>/artifacts` as the visible hotspot.
- **Problem pattern:** Per-run artifact listing calls for cleanup/download.
- **Recommendation:** Reuse artifact metadata already returned by prior workflow steps instead of re-listing artifacts whenever possible.
- **Estimated reduction:** Small per run, but worthwhile because the workflow is frequent.

### Redundancy and missed cache opportunities
- **Implement flow duplication:** issue metadata is fetched early for precheck and later re-fetched with comments/context in the same run.
- **Review loop duplication:** repeated issue/PR mutation calls in loops can be merged.
- **Stable polling duplication:** repeated status checks continue even when phases are unchanged for long intervals.

### Repository-policy cross-check
No separate repo-local API hygiene spec was surfaced in the sampled logs beyond the prompt requirement to prefer batching, cycle-local caching, and fail-open behavior. The recommendations above align directly with those rules:
- **mandatory batching:** use GraphQL or list endpoints for grouped reads,
- **cycle-local caches:** persist issue/PR/run metadata within a workflow cycle,
- **fail-open:** metadata refresh failures should warn and continue when not correctness-critical.

## MCP & Serena Efficiency

### What the logs show
- **Implement flow:** failing run `25246727158` shows `serena.activate_project(...)`, which is correct and consistent with the required no-onboarding path.
- **Broad-read evidence:** recent successful implement run `25247284684` explicitly read broad context files: `issue_body.txt`, `issue_comments.json`, `implementation_context.txt`, `memory_context.txt`, `changed_files.txt`, `pr_metadata.json`.
- **Review flows:** sampled review logs did not show clear Serena/Git MCP usage; logs emphasized large context prep and GitHub CLI activity instead.

### Efficiency findings
1. **Tool use is still too broad before narrowing the edit target.**
   - Pulling large context blobs early increases token load and time-to-first-edit.
2. **Serena is being activated, but not obviously exploited for symbol-first narrowing in sampled implement failures.**
3. **Review flows appear to lean on shell/GH CLI orchestration more than targeted Git MCP fetches.**

### Recommendations
- **Prefer symbol-first reads in implement/review flows.**
  - Use `get_symbols_overview` and `find_symbol` before opening full files.
  - Use `find_referencing_symbols` for impact checks instead of reading entire related files.
- **Use `search_for_pattern` instead of repo-wide shell grep when Serena is available.**
  - This reduces both output volume and repeated scans.
- **Adopt Git MCP for review/edit fetches where available.**
  - Prefer `git_status`, `git_diff`, `git_show`, `git_log`, and `git_branch` over ad hoc shell/GitHub API mixtures.
- **Plan reads once and avoid duplicate region fetches.**
  - The no-op implement failures suggest context was expanded without converting to edits; targeted reads would lower this risk.
- **Increase safe parallelism for independent metadata reads.**
  - Issue metadata, PR metadata, and changed-files manifests can be fetched in parallel before model invocation.

### Expected impact
- **Latency:** moderate improvement in implement/review startup.
- **Token efficiency:** meaningful reduction from smaller context assembly.
- **Correctness risk:** low, because the recommendation narrows reads without changing decision logic.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed:** prompt cache was enabled in deep-dive review flow logs, but sampled raw logs did **not** expose cache creation/read counters or hit-rate metrics.
- **Implication:** cache is likely present, but current telemetry is insufficient to quantify hit/miss behavior from this sample.

### Likely cache-fragmentation causes
- **Repeated duplicated instruction blocks** in failed implement run `25246727158`.
- **Dynamic noise in prompt prefixes** such as run IDs, issue comments, and assembled context ordering.
- **Large multi-model review envelopes** where stable instructions are mixed with volatile metadata.

### Recommendations
1. **Stabilize the prompt prefix**
   - Put invariant system/agent/rubric content first.
   - Move run-specific metadata to a suffix block.
2. **Deduplicate instruction sources**
   - Remove repeated AGENTS/system text before prompt assembly.
3. **Normalize context file ordering**
   - Keep file list and context sections in deterministic order so semantically identical runs share the same prefix.
4. **Only expand memory context after first-pass need**
   - With average retrieval cost at **36.9 tokens**, memory is cheap; the bigger issue is surrounding prompt variance.

### Estimated impact
- **Tokens:** moderate savings on repeated runs.
- **Latency:** moderate improvement where cached prefixes can be reused.
- **Reliability:** slightly better consistency because the model sees cleaner, less repetitive instructions.

### Gaps
- Cache hit/miss counters were not surfaced in the sampled raw logs.  
- Next collection step: emit explicit prompt-cache create/read/hit lines per AI step so reuse can be measured by workflow family.

## Orchestrator Health

### Overall assessment
The orchestrator is **functional but noisy**:
- Many `clarify`, `plan`, and `orchestrate_clarify_respond` runs skip in **0–2s**, which suggests guards are working.
- However, heavy downstream phases (`implement`, `review_autofix`, `test_and_mark_stable`) still suffer from long waits, cancellations, and recovery gaps.
- `review_autofix` cancellations (**36/66 runs**) are the clearest sign of superseded or long-running work being overtaken by newer events.

### Recurring pain points
1. **No-op implement loops**
   - The orchestrator lacks a strong fallback path when the editor agent fails to emit edits.
2. **Long-lived downstream validation**
   - Stable/test phases hold onto workflow state for very long periods and then get cancelled.
3. **Superseded review work**
   - Review/autofix runs often outlive their usefulness.
4. **Guard-only workflow fanout**
   - Healthy individually, but collectively creates orchestration noise and queue pressure.

### Smallest safe mitigations
- Add **typed terminal reasons** for implement failures: `no_patch_emitted`, `blocked_missing_context`, `tool_error`.
- Add **supersession checks** before expensive review phases begin and between major passes.
- Add **phase heartbeat/staleness detection** for stable/test orchestration.
- Move cheap eligibility checks **ahead of child-workflow dispatch** where possible.

### Observable indicators to track
- `% implement runs ending in no actionable output`
- `review_autofix cancellation rate`
- `average poll iterations per stable/test run`
- `runner-wait seconds per workflow family`
- `guard-only workflow dispatch count per merged PR`
- `nightly_validation_selftest fixture pass rate`

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Validate / stable-release loop
- **Bottleneck type:** compute + queueing + polling overhead
- **Evidence:** `test_and_mark_stable` avg **2,558s**, p95 **3,562s**, 0 successes.
- **Why it dominates:** It sits late in the pipeline and blocks release completion.
- **Fix:** reduce polling, add stale-phase exits, trim child-job fanout.

### 2. Review/autofix loop
- **Bottleneck type:** compute + cancellation waste + API overhead
- **Evidence:** avg **294s**, p95 **1,588s**, **36 cancellations**; deep-dive run `25237552686` reached **2,938s**.
- **Why it dominates:** It consumes the most AI/model work and is frequently superseded.
- **Fix:** stronger early gating, lighter default reviewer set, pre-pass supersession checks.

### 3. Implement execution
- **Bottleneck type:** retry waste / non-productive compute
- **Evidence:** failed runs spend **143–210s** before dying at `Run Codex implementation`; `25246727158` failed after two non-actionable attempts.
- **Why it matters:** It is directly user-visible and blocks downstream review/validation.
- **Fix:** compact recovery prompt and early terminal failure on repeated no-op.

### 4. CI runner wait plus fixed ~10-minute compute floor
- **Bottleneck type:** queueing + steady compute
- **Evidence:** CI avg **609s**, p50 **614.5s**, p95 **650s**; recent runs show hosted-runner waits.
- **Why it matters:** It is stable but frequent, so small gains compound.
- **Fix:** reduce queue amplification from workflow chaining; review whether lint/test partitioning can expose earlier partial results without changing coverage.

### 5. Clarify/plan/orchestrate fanout
- **Bottleneck type:** orchestration overhead
- **Evidence:** many skip-only runs complete in **0–2s**.
- **Why it matters:** Individually cheap, collectively noisy.
- **Fix:** suppress dispatch of obviously ineligible child workflows earlier.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` release validation: avg **2,558s**, **0/5 successes**
- `review_autofix`: high-cost, high-cancellation path with **36/66 cancelled**
- `ci`: stable ~10-minute baseline with visible runner waits
- `implement`: repeated no-op failures in the core AI edit step

**Top failure modes**
- `implement / Run Codex implementation` producing no actionable output (`25246727158` and other failed implement runs)
- nightly validation self-test fixture failures (`25242537588`)
- long stable/test orchestration ending in cancellation rather than completion (`25245004057`)

**Highest-cost drivers**
- multi-model `review_autofix` with high reasoning settings
- `workflow_log_analysis` summarization volume, including a **153,540-token** summarization pass (`25246650500`)
- repeated downstream polling and runner wait across stable/test orchestration

**Top 3 prioritized actions**
1. **Fix implement no-op recovery first**
   - highest near-term reliability win with low change risk.
2. **Rework stable/test polling and stale-phase handling**
   - biggest end-to-end time reduction.
3. **Trim default review/autofix depth**
   - biggest recurring token and API savings.

## Metrics Appendix

### Overall run metrics

| Scope | Total Runs | Success | Failure | Cancelled | Other/Skipped | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 255 | 10 | 50 | 685 | 104.722 | 1.0 | 615.0 |

### Workflow family metrics

| Workflow Family | Total Runs | Success | Failure | Cancelled | Other | Avg (s) | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| implement | 185 | 17 | 9 | 6 | 153 | 28.22 | 1.0 | 201.2 | Core failure family; many skipped |
| review_autofix | 66 | 28 | 0 | 36 | 2 | 294.11 | 41.0 | 1588.25 | Major cost/cancel sink |
| test_and_mark_stable | 5 | 0 | 0 | 5 | 0 | 2558.2 | 1999.0 | 3562.2 | Biggest end-to-end bottleneck |
| workflow_log_analysis | 5 | 2 | 0 | 3 | 0 | 3382.0 | 3320.0 | 4577.6 | Expensive meta-analysis workflow |
| ci | 60 | 60 | 0 | 0 | 0 | 609.15 | 614.5 | 650.05 | Stable but queue-bound |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 89.0 | 89.0 | 89.0 | Single observed failure |
| orchestrate | 6 | 6 | 0 | 0 | 0 | 236.67 | 241.0 | 258.0 | Healthy but not cheap |
| orchestrate_poll | 11 | 11 | 0 | 0 | 0 | 42.82 | 42.0 | 46.5 | Lightweight, but full checkout observed |
| clarify | 223 | 32 | 0 | 0 | 191 | 19.52 | 1.0 | 126.2 | Mostly guard-only |
| plan | 185 | 26 | 0 | 0 | 159 | 14.64 | 1.0 | 147.6 | Mostly guard-only |

### Run-specific evidence table

| Run ID | Workflow Family | Outcome | Duration (s) | Key Evidence |
|---|---|---:|---:|---|
| 25246727158 | implement | failure | 184 | Two consecutive “no actionable output” attempts in `Run Codex implementation` |
| 25242537588 | nightly_validation_selftest | failure | 89 | `fixtures=3 passed=1 failed=2` |
| 25245004057 | test_and_mark_stable | cancelled | 3629 | repeated downstream polling + multiple runner waits |
| 25237552686 | review_autofix | success | 2938 | broad multi-model review, heavy prep, API pressure |
| 25246650500 | workflow_log_analysis | success | 4828 | embedded deep-dive report; 153,540-token summarization pass |
| 25247361216 | ci | success | 637 | `lint` dominated; hosted runner wait visible |
| 25247664026 | orchestrate_poll | success | 42 | `actions/checkout@v5` with `fetch-depth: 0` in lightweight poller |
| 25247284684 | implement | success | 291 | broad context reads: issue body/comments/memory/context/files/PR metadata |

### AI memory telemetry metrics

| Metric | Value |
|---|---:|
| Total telemetry records | 122 |
| Retrieve operations | 22 |
| Retrieve hit rate | 81.8% (18/22) |
| Avg retrieve `estimated_tokens` | 36.9 |
| `keyword_method=plain` | 18 |
| `keyword_method=none` | 4 |
| `keyword_method=llm` | 0 |
| Zero-hit retrieves | 4 |
| `fail_open:true` entries | 0 |
| `enabled:false` entries | 0 |
| Push retries >1 | 3 |

### Observed token / cache signals

| Source Run | Workflow | Signal | Value | Confidence |
|---|---|---|---:|---|
| 25246650500 | workflow_log_analysis | Summarization pass tokens | 153,540 | High |
| 25237552686 (via deep-dive analysis in 25246650500) | review_autofix | Consolidator tokens | 58,177 | Medium-high |
| Deep-dive telemetry sample | AI memory retrieve | Avg estimated tokens per retrieve | 36.9 | High |
| 25237552686 | review_autofix | Prompt cache enabled | Yes | High |
| Sampled raw logs | prompt cache read/hit counts | Not surfaced | — | High |

### GH API summary

| Workflow / Run | Observed Pattern | Approx Volume | Main Risk |
|---|---|---:|---|
| review_autofix / 25237552686 (via 25246650500) | repeated `gh api` / GraphQL / workflow dispatch/edit loops | ~259 log hits | 429 / secondary rate limits |
| test_and_mark_stable / 25245004057 | repeated downstream run polling | high, exact count not computed | wasted API + latency |
| copilot_pull_request_reviewer / 25247335697 | artifact listing via `gh api .../artifacts` | low | minor redundancy |
| implement failures | repeated issue/metadata fetches in same run | low-moderate | redundant calls, race exposure |

If you want, I can turn this into a shorter exec-ready memo or a prioritized implementation checklist for the workflow owners.

## Deep Audit — Workflows & Scripts (2026-05-02)

### Section 1: Bug & Correctness Sweep

- **BUG-001**
  - **File path:** `.github/workflows/review_autofix.yml:3740-3757,3859-3877,4603-4610`
  - **Severity:** High
  - **Category tag:** `bug`
  - **Description:** The fallback `set_issue_phase_label_resilient()` implementations in three late-stage review paths only do `POST /issues/{n}/labels` for the target phase label and never remove the prior phase labels. That diverges from the repository’s single-phase contract in `.github/ai/label_contract.v1.json`, where issue-phase labels are mutually exclusive. In the success path, `scripts/label_helpers.sh` uses a `PUT` replacement strategy that removes old phase labels first; these fallback copies do not. If the helper script is unavailable late in the job—as the surrounding comments explicitly anticipate—issues can end up carrying contradictory states like `ai:done` + `ai:ready-to-merge` or `ai:review-blocked` + `ai:ready-to-merge`, which can mislead the orchestrator and close/recovery sweeps.
  - **Recommended fix:** Delete the ad hoc fallback bodies and either (a) always restage/source `scripts/label_helpers.sh` before these late steps, or (b) copy the real `set_issue_phase_label_resilient` semantics from `scripts/label_helpers.sh:145-196`, including the `GET`→phase-set rewrite→`PUT` path. If a local fallback must exist, it should accept the same signature: `set_issue_phase_label_resilient <issue_number> <target_label> <repo>` and preserve phase exclusivity.

- **BUG-002**
  - **File path:** `.github/workflows/implement.yml:56-72`
  - **Severity:** Medium
  - **Category tag:** `bug`
  - **Description:** The very first implement gate uses a raw `gh api "repos/.../issues/${ISSUE_NUMBER}"` call with `set -euo pipefail` and no retry/backoff wrapper. Every later GitHub API path in this workflow intentionally sources `scripts/gh_helpers.sh` and uses `gh_retry`, but this precheck runs before support files are fetched and fails closed on any transient 403/429/5xx/network blip. That makes the cheapest gate in the workflow one of the least resilient.
  - **Recommended fix:** Mirror the inline early-boot retry pattern already used in `.github/workflows/orchestrate_poll.yml:61-101` or `cancel_on_pr_close.yml:29-80`: add a tiny local `gh_retry`/rate-limit-aware wrapper for this one precheck call, then write the successful payload into `ISSUE_META_FILE` or `$GITHUB_ENV` for later reuse.

### Section 2: GitHub API Call Redundancy Audit

- **API-001**
  - **File path:** `.github/workflows/implement.yml:56-72,547-572`
  - **Severity:** Medium
  - **Category tag:** `api-redundancy`
  - **Description:** The implement flow fetches the same issue payload twice on the common path: once in **Precheck approval phase label** and again in **Fetch issue metadata**. Current call count is **2 identical `GET /repos/{repo}/issues/{issue}` calls** before any mutation; proposed call count is **1**. The second fetch exists only because the first response is discarded instead of being persisted for downstream steps.
  - **Recommended fix:** Persist the first payload into `ISSUE_META_FILE` or a `GITHUB_ENV` heredoc, then let **Fetch issue metadata** become a pure parse/write step. This already matches the workflow’s own later cache-reuse pattern in **Validate approval phase label**, which prefers `ISSUE_META_FILE` before re-fetching. If you want a shared contract, model it after the cycle-local cache approach used in `scripts/orchestrate_poll_process.sh` (`ACTIVE_WORKFLOW_ISSUES`, `_candidate_details_json`).

- **BATCH-001**
  - **File path:** `.github/workflows/review_autofix.yml:498-530`
  - **Severity:** Medium
  - **Category tag:** `api-batching`
  - **Description:** In `post-merge-validate-dispatch`, the fallback path can degrade into a per-issue label lookup loop: after building `issue_nodes_json` from PR-body regexes, each linked issue with unknown labels triggers `gh issue view ... --json labels`. Current fetch shape is **1 PR metadata call + N `gh issue view` calls** for N linked issues; proposed fetch shape is **2 total calls** (1 PR/body discovery call + 1 batched GraphQL labels query for all issue numbers). This is exactly the “per-iteration API calls inside loops” pattern called out in `CLAUDE.md §15`.
  - **Recommended fix:** Add a batched helper in `scripts/gh_helpers.sh`, e.g. `fetch_issue_labels_graphql <repo> <numbers_json>`, using the same alias pattern as `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh:5914-6043`. Then populate `issue_nodes_json` with labels in one shot before the loop and leave the loop to handle only dispatch/edit decisions.

- **BATCH-002**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:2725-2774,2797-2832,2860-2897,2919-2956,3031-3055,3216-3239`
  - **Severity:** High
  - **Category tag:** `api-batching`
  - **Description:** Six separate “Dispatch & watch” blocks each reimplement the same three-call polling pattern for a different downstream workflow: (1) fetch latest run id before dispatch, (2) poll `actions/workflows/{wf}/runs` until a new id appears, and (3) poll `actions/runs/{id}` until completion. Current fixed cost is **at least 18 REST calls before steady-state waiting** (6 pre-dispatch snapshots + 6 discovery loops + 6 first status checks), then **6 independent status/discovery poll streams** with 5s/15s sleeps. Proposed steady-state call count is **1 list-runs poll per interval** after dispatching and tracking all child run ids centrally. [NEEDS VERIFICATION]
  - **Recommended fix:** Extract a shared watcher script, then extend the `scripts/gh_helpers.sh` list-runs filtering pattern already used by `autofix_retrigger_has_inflight_peer()` (`GET /repos/{repo}/actions/runs` + local jq filtering). A concrete target would be a helper like `dispatch_and_watch_many --repo "$TEST_REPO" --workflow workflow-log-analysis.yml ...` that dispatches all child workflows first, captures their run ids, and polls one repo-wide runs list instead of six independent endpoints.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**
  - **File path:** `.github/workflows/clarify.yml:161-250`, `.github/workflows/plan.yml:189-255`, `.github/workflows/orchestrate.yml:262-348`, `.github/workflows/orchestrate_clarify_respond.yml:204-292`, `.github/workflows/orchestrate_poll.yml:213-307`, `.github/workflows/validate.yml:185-275`
  - **Severity:** Medium
  - **Category tag:** `duplication`
  - **Description:** Six workflows independently implement near-identical “resolve support ref / checkout or clone coding-workflows / copy scripts, prompts, schemas, and instruction files” logic. The blocks differ only in file lists and minor fallback rules, but they duplicate ref selection, self-repo handling, fallback-to-main behavior, and copy/install mechanics. This has already drifted: `validate.yml` uses a bespoke clone-and-copy routine while the others use checkout-based staging, which increases the odds of subtle behavioral skew.
  - **Recommended fix:** Move this into one shared shell module, ideally `scripts/fetch_workflow_support.sh`, with a function signature like:
    ```bash
    stage_workflow_support \
      --consumer-repo "$GITHUB_REPOSITORY" \
      --self-sha "$GITHUB_SHA" \
      --script-ref stable \
      --require scripts/gh_helpers.sh \
      --require prompts/mode-orchestrate.txt \
      --optional ai-memory/schemas/*.json
    ```
    Update callers in `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `orchestrate_poll.yml`, and `validate.yml` to pass only their required asset lists.

- **DUP-002**
  - **File path:** `.github/workflows/issue_pr_status.yml:195-210`, `.github/workflows/review_autofix.yml:3763-3775`, `.github/workflows/review_autofix.yml:4618-4630`, `.github/workflows/review_autofix.yml:503-530`
  - **Severity:** Low
  - **Category tag:** `duplication`
  - **Description:** Linked-issue fallback discovery is implemented multiple times with hand-copied PR body/title regexes and then paired with custom label application loops. The regex bodies are close but not identical, and the duplicate logic now exists in at least three review/issue-status execution paths. That makes future fixes to closing-keyword parsing or false-positive handling easy to miss in one caller.
  - **Recommended fix:** Centralize the fallback into `scripts/gh_helpers.sh`, e.g. `linked_issue_numbers_from_pr_text <repo> <pr_text_or_meta_file>`, and pair it with the existing `set_issue_phase_label_resilient` helper from `scripts/label_helpers.sh`. Update callers in `issue_pr_status.yml` and both review_autofix label-update paths to consume the same helper output.

### Section 4: Expression Size Limit Risk Assessment

- **EXPR-001**
  - **File path:** `.github/workflows/test-and-mark-stable.yml:1118-1449`
  - **Severity:** Medium
  - **Category tag:** `expression-limit`
  - **Description:** The `Wait for review workflow` `run:` block contains `${{ }}` interpolations and is already large enough to be a realistic expression-limit regression risk. Estimated current interpolated block size is **16,626 characters**, leaving only **4,374 characters of headroom** before GitHub’s hard 21,000-character template-expression ceiling. This block also embeds a full polling helper plus retry/backoff logic, so routine maintenance is likely to grow it further.
  - **Recommended fix:** Extract the whole wait loop into `scripts/test_mark_stable_wait_review.sh` and pass only the small set of dynamic values through environment variables (`PR_NUMBER`, `REVIEW_TIMEOUT`, `BAIT_SHA`, `TEST_REPO`, `ISSUE_NUMBER`). Preferred over further inline editing because this repository has already hit the expression cap multiple times.
  
- No workflow file exceeds the **800 KB** early-warning threshold; the largest audited workflow is `.github/workflows/review_autofix.yml` at **268,926 bytes**, followed by `.github/workflows/test-and-mark-stable.yml` at **229,098 bytes**.

### Section 5: Cross-Cutting Concerns

- **DEAD-001**
  - **File path:** `scripts/review_run_reviewers.sh:119-123`
  - **Severity:** Low
  - **Category tag:** `dead-code`
  - **Description:** `probe_prompt` is declared in `run_cache_probe()` but never assigned or read. ShellCheck flags it as unused (`SC2034`). This is harmless at runtime, but it is dead state in a cost-sensitive script whose purpose is prompt-cache instrumentation.
  - **Recommended fix:** Remove `probe_prompt` from the local declaration list, or actually use it if the intent was to keep the assembled probe prompt in memory for telemetry/debug output.

- **SHELL-001**
  - **File path:** `scripts/validate_changed_files_syntax.sh:70-74`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** The secret-redaction `case` arm contains unreachable later patterns (`SC2221`/`SC2222`): `*.env*` already subsumes `*.envrc` and `.env*`, so the latter branches never match. The current behavior still redacts, but the dead patterns make the allow/deny surface harder to reason about and invite incorrect future edits.
  - **Recommended fix:** Collapse the redundant patterns into one explicit branch set, keeping the current broad-redaction behavior but removing unreachable alternatives. A single documented pattern group is easier to audit than an alternation with dead arms.

- **SHELL-002**
  - **File path:** `scripts/review_apply_fixes.sh:1002-1034`
  - **Severity:** Low
  - **Category tag:** `shellcheck`
  - **Description:** The fallback-summary path uses `ls -1 ... | sort -V | tail -n 1` twice to select the most recent retry artifact. ShellCheck flags both instances (`SC2012`). The current filenames are controlled, so this is unlikely to break today, but it is still brittle and couples correctness to `ls` formatting and pathname hygiene.
  - **Recommended fix:** Replace both selectors with shell glob arrays or `find ... -print0 | sort -zV | tail -z -n 1`. That keeps the “latest attempt wins” behavior without depending on `ls` parsing.

- No `TODO`, `FIXME`, `HACK`, or `XXX` markers were found in the audited workflow/script scope.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BATCH-002 |
| Medium | 5 | BUG-002, API-001, BATCH-001, DUP-001, EXPR-001 |
| Low | 4 | DUP-002, DEAD-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 7 | Large |
| Expression size reduction | 1-2 | Medium |
| Medium/Low fixes | 4 | Small |
