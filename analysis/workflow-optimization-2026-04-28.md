## Executive Summary

- **`review_autofix` is the dominant end-to-end bottleneck and failure source.** It accounts for **6 of 13 failures (46.2%)**, has **p95 duration 2,617s**, and multiple runs on **April 28, 2026** stretched to **1,388-4,429s**; all sampled failures hit **“Run reviewer models.”** **Estimated impact:** cut affected review runs by **15-30 minutes** and reduce repo-wide failures materially. **Confidence:** high.
- **The orchestration layer is generating substantial control-plane noise.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` are overwhelmingly in **“other”** states rather than clean success paths: **73/76, 69/72, 67/72, and 71/72** respectively, with many recent runs ending as **`skipped` or `action_required` in 0-2s**. **Estimated impact:** reduce unnecessary workflow starts/API chatter by **50%+** in these families. **Confidence:** high.
- **CI is not the worst bottleneck, but it is a steady floor of ~10 minutes.** `ci` runs have **p50 610s** and repeated successful durations clustered at **598-651s**, so any PR that reaches CI inherits a stable ~10 minute validation tax. **Estimated impact:** modest per-run savings unless test parallelism/selectivity is improved. **Confidence:** high.
- **Observability is incomplete in exactly the places needed for root-cause analysis.** Four runs returned **GitHub API 404** on log archive fetch (`actions/runs/{id}/logs`) for run IDs **25051823019, 25051823011, 25051823002, 25051822983**, and the supplied window contained **no token/model/cache/API-call/MCP telemetry excerpts**. **Estimated impact:** faster RCA and better optimization prioritization; little direct runtime change. **Confidence:** high.
- **A small number of waiters/download steps are causing disproportionate reliability pain.** Examples: **PR creation wait** failure in `test_and_mark_stable` at **664s** (run **25034760491**) and **“Download ccrcli”** failure at **240s** (run **25000347887**). **Estimated impact:** save **4-11 minutes** per incident and improve release/review reliability. **Confidence:** medium.
- **Cost optimization is currently constrained by missing token telemetry, but wasted expensive work is still visible through runtime proxies.** Long failed/cancelled `review_autofix` runs and long failed `implement` runs strongly suggest avoidable high-cost model calls without fail-fast guards. **Estimated impact:** likely largest token savings come from reviewer-model gating and fail-open behavior. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Stage `review_autofix` into fast triage -> selective deep review**
   - **Evidence:** `review_autofix` has **p50 18s** but **p95 2,617s** and many slow runs at **1,273s, 1,388s, 1,401s, 1,451s, 1,660s, 1,847s, 1,988s, 2,025s, 2,046s, 2,073s, 2,099s, 3,699s, and 4,429s**. Failures in runs **25027333810, 25026897459, 25026882619, 25027318001, 25046910871, 25045997555** all stop at **“Run reviewer models.”**
   - **Root cause:** A bimodal review path: many runs are trivial/no-op, while a minority escalate into very long reviewer-model execution with no fast fail.
   - **Exact change:** Add a cheap pre-review gate before launching full reviewer models:
     - skip deep review when diff is empty/trivial/already handled,
     - route small low-risk diffs to a single cheaper reviewer pass,
     - only escalate to the full reviewer set for large or risky changes.
   - **Estimated time savings:** **15-30 minutes** on affected long-tail review runs; **5-10 minutes** end-to-end on PRs that currently hit the heavy review path unnecessarily.
   - **Implementation risk:** **Low-medium.**
   - **Critical-path or local:** **Critical-path win.**

2. **Add hard per-model deadlines and partial-success completion for `Run reviewer models`**
   - **Evidence:** Failed reviewer runs lasted **668s, 697s, 3,580s, 3,720s, 3,772s, and 3,879s** before failing.
   - **Root cause:** Long waits are being allowed to consume most of the workflow budget before the step fails.
   - **Exact change:** Put a strict timeout around each reviewer invocation and allow the step to finish with partial results if at least one reviewer returns. Mark the step degraded instead of failed when enough evidence exists to continue.
   - **Estimated time savings:** **10-60 minutes** per failed run; improves tail latency immediately.
   - **Implementation risk:** **Low.**
   - **Critical-path or local:** **Critical-path win.**

3. **Suppress no-op downstream orchestration launches**
   - **Evidence:** `clarify` has **73/76 other**, `plan` **69/72 other**, `implement` **67/72 other**, and `orchestrate_clarify_respond` **71/72 other**. Recent runs at **09:18, 10:00, 10:40, 10:55, 12:06 UTC** show repeated **skipped/action_required** outcomes in **0-2s** across these families.
   - **Root cause:** The orchestrator appears to fan out workflows before prerequisites are satisfied, then exits almost immediately.
   - **Exact change:** Move prerequisite checks into the dispatch decision so `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` are only started when they can actually execute work.
   - **Estimated time savings:** Small per run (**1-10s**), but meaningful control-plane reduction across dozens of invocations per day.
   - **Implementation risk:** **Low.**
   - **Critical-path or local:** **Mostly local**, but improves overall system responsiveness.

4. **Shorten the PR-creation waiter in stable-release testing**
   - **Evidence:** `test_and_mark_stable` run **25034760491** failed after **664s** at **“Phase 3b: Wait for PR creation (implement phase)”**. The family has only **2 runs**, with **1 success / 1 failure**.
   - **Root cause:** A long blind wait on PR appearance rather than a bounded existence check with explicit fallback.
   - **Exact change:** Poll fewer times with tighter exit criteria, and query by deterministic branch/head once before each sleep instead of waiting the full window.
   - **Estimated time savings:** **5-11 minutes** per incident.
   - **Implementation risk:** **Low.**
   - **Critical-path or local:** **Critical-path win** for release validation.

5. **Cap runaway `implement` executions with earlier preflight validation**
   - **Evidence:** `implement` failures occurred at **393s** (run **25034870641**) and **3,609s** (run **25030967036**) at **“Run Codex implementation.”** Yet family p95 is only **173.25s**, indicating a sharp long-tail outlier pattern.
   - **Root cause:** Some implementation runs likely enter a bad path after startup instead of failing quickly.
   - **Exact change:** Add preflight checks before the implementation model starts: repo cleanliness, issue/PR context completeness, changed-file budget, and prompt-size sanity.
   - **Estimated time savings:** **5-55 minutes** on failed outliers.
   - **Implementation risk:** **Low-medium.**
   - **Critical-path or local:** **Critical-path win.**

6. **Optimize CI only after AI-stage guardrails land**
   - **Evidence:** `ci` is very consistent: **p50 610s**, **p95 638s**, with many successful runs clustered between **598s and 651s**.
   - **Root cause:** CI is a fixed compute block, not the biggest variability source.
   - **Exact change:** Once AI-stage failures are under control, look at test sharding/selective execution for unchanged areas.
   - **Estimated time savings:** likely **1-3 minutes** per PR unless there are easy selective-test wins.
   - **Implementation risk:** **Medium** because validation coverage can regress.
   - **Critical-path or local:** **Critical-path**, but lower priority than review/implement fixes.

## Cost Optimizations

Ranked by expected token and/or dollar savings.  
**Important limitation:** the supplied window contains **no token totals, model mix, cache creation/read metrics, or per-step billing telemetry**, so savings are estimated using runtime/failure proxies rather than direct token spend.

1. **Gate expensive reviewer-model usage by diff risk**
   - **Evidence:** The most expensive-looking path is `review_autofix`: **p95 2,617s**, multiple **20-74 minute** runs, and all sampled failures occur in **“Run reviewer models.”**
   - **Root cause:** High-cost review seems to run too often or too long before proving value.
   - **Exact change:** Use a cheap deterministic risk gate plus one lightweight reviewer for low-risk diffs; escalate only when the diff size, touched files, or policy triggers warrant it.
   - **Estimated savings:** Likely the **largest token savings in the pipeline**; runtime proxy suggests avoiding **10-60 minutes** of model time on failed or low-value review runs.
   - **Quality-risk notes:** **Low-medium.** Keep escalation for risky diffs to preserve review quality.

2. **Lower reasoning depth for routing/no-op orchestration paths**
   - **Evidence:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` frequently terminate in **`skipped`/`action_required`** within **0-2s** in recent runs.
   - **Root cause:** If these paths are invoking LLM logic before deciding there is no work, the pipeline is paying for decisions that could be made deterministically.
   - **Exact change:** Put pure-rule gating first; if a model is still required, use the lowest-cost routing model and minimum reasoning level for precondition evaluation.
   - **Estimated savings:** Potentially substantial in aggregate because these families ran **72-76 times each** in the sample window.
   - **Quality-risk notes:** **Low**, provided only routing logic is downgraded.

3. **Reduce avoidable rerun-equivalent spend from long cancellations/failures**
   - **Evidence:** `review_autofix` had **11 cancelled runs (9.65%)** and **6 failed runs (5.26%)** out of **114 total**. Cancelled examples include **235s, 279s, 431s, 1,290s, 1,730s, and 1,802s**.
   - **Root cause:** Expensive work is continuing after it is likely superseded or doomed.
   - **Exact change:** Apply tighter concurrency cancellation at the start of expensive jobs and checkpoint before launching each costly reviewer/model wave.
   - **Estimated savings:** Removes a meaningful amount of wasted model/runtime spend from superseded work; exact token savings unavailable.
   - **Quality-risk notes:** **Low** if cancellation only targets superseded runs on the same PR/head.

4. **Stabilize prompt prefixes for shared review/implement prompts**
   - **Evidence:** Direct cache metrics are missing, but long repeated `review_autofix` runs on the same day suggest repeated context assembly over similar repo state.
   - **Root cause:** Prompt cache fragmentation is commonly caused by volatile prefixes such as timestamps, run IDs, step banners, and changing metadata placement.
   - **Exact change:** Keep stable system/instruction prefixes identical across runs; move run-specific metadata to the tail; normalize file ordering and diff summaries.
   - **Estimated savings:** Unquantified due missing cache telemetry, but this is the safest cache-efficiency improvement available.
   - **Quality-risk notes:** **Low.**

5. **Preflight implementation runs to avoid paying for doomed long contexts**
   - **Evidence:** `implement` run **25030967036** consumed **3,609s** before failing in the main model step.
   - **Root cause:** Context/model work starts before validating whether inputs are complete and actionable.
   - **Exact change:** Validate issue state, branch state, diff availability, and prompt/context size before starting the implementation model.
   - **Estimated savings:** Large on failed outliers; limited on healthy runs.
   - **Quality-risk notes:** **Low.**

**Model selection / reasoning-level evaluation:**  
Direct model telemetry was **not supplied**, so I cannot verify whether model choice is currently oversized. The strongest evidence-based action is to **reserve high-reasoning/high-cost reviewers for the minority of diffs that justify them**, and keep routing/orchestration on minimal-cost logic.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Make reviewer-model failures fail-open instead of hard-fail**
   - **Failure evidence:** `review_autofix` contributed **6 of 13 repo failures**, all at **“Run reviewer models.”**
   - **Root cause category:** External/model-step instability and over-strict terminal behavior.
   - **Exact fix:** Treat individual reviewer failures/timeouts as degraded signals; continue if at least one reviewer returns or if fallback checks succeed.
   - **Expected reliability impact:** Best single reliability improvement; likely removes the largest recurring failure class.
   - **Rollback/fail-open considerations:** Keep a strict mode behind a flag for debugging.

2. **Add one bounded retry for transient downloader/network steps**
   - **Failure evidence:** `copilot_pull_request_reviewer` run **25000347887** failed at **“Download ccrcli”** after **240s**.
   - **Root cause category:** Transient dependency/download failure.
   - **Exact fix:** Add a single retry with short backoff and checksum validation for artifact/tool downloads.
   - **Expected reliability impact:** Small absolute impact, but very low-risk and cheap.
   - **Rollback/fail-open considerations:** Fail closed only after retry; keep logs of both attempts.

3. **Replace long PR wait loops with deterministic existence checks**
   - **Failure evidence:** `test_and_mark_stable` run **25034760491** failed in **Phase 3b: Wait for PR creation** after **664s**; family success rate is only **50%** in the sample (**1/2**).
   - **Root cause category:** Async orchestration timeout / waiter fragility.
   - **Exact fix:** Query PR existence by expected head branch and issue linkage; shorten sleep cadence; emit explicit reason when exiting.
   - **Expected reliability impact:** High for the release-stability flow.
   - **Rollback/fail-open considerations:** If PR lookup fails, surface a precise diagnostic instead of generic timeout.

4. **Add watchdogs/checkpoints to `Run Codex implementation`**
   - **Failure evidence:** `implement` failures at **393s** and **3,609s** on the main implementation step.
   - **Root cause category:** Long-running model execution without enough intermediate liveness checks.
   - **Exact fix:** Emit heartbeats, checkpoint after prompt assembly, after diff fetch, and after patch generation; abort early on no-progress.
   - **Expected reliability impact:** Medium; likely converts long failures into quick failures and eases RCA.
   - **Rollback/fail-open considerations:** Watchdog should fail open into a diagnostic artifact where possible.

5. **Fix missing-log observability failures**
   - **Failure evidence:** Four runs returned **404** for `actions/runs/{id}/logs`: **25051823019, 25051823011, 25051823002, 25051822983**.
   - **Root cause category:** Telemetry/collection failure.
   - **Exact fix:** Upload step logs/artifacts before teardown and make the collector retry 404s briefly before marking partial data.
   - **Expected reliability impact:** No direct runtime gain, but much higher RCA reliability.
   - **Rollback/fail-open considerations:** Collector should mark missing logs as partial, not failed.

6. **Add one rerun-once policy for the flaky CI test step**
   - **Failure evidence:** `ci` failures **25031188981** and **25031041656** both hit **“Implement post-Codex recovery unit tests”** at **549s** and **485s**.
   - **Root cause category:** Potential flaky test or environment sensitivity.
   - **Exact fix:** Retry that step once, and separately emit the failing test names into the artifact/summary.
   - **Expected reliability impact:** Medium if this is flake-driven; low if it is deterministic.
   - **Rollback/fail-open considerations:** Keep the rerun scoped to that step only.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were present in the supplied analysis window or excerpts.

- **Observed gap:** I could not compute:
  - retrieve hit rate,
  - average `estimated_tokens` vs budget,
  - `keyword_method` distribution,
  - push retry counts,
  - fail-open or disabled-memory rates.
- **Risk:** Memory may be working, degraded, or fully disabled, but this sample provides no direct signal.
- **Recommendation:** Verify that sampled failed/slow runs emit `AI_MEMORY_TELEMETRY` lines for at least:
  - `retrieve`
  - `record-candidate`
  - `record-run-event`
  - `finalize-task`
  - `promote`
  - `compact`
  - `processed-command-claim`
  - `processed-command-complete`

Until that telemetry appears, any memory-health conclusion would be speculative.

## GH API Call Audit

**Important limitation:** the sample did **not** include per-step GH API call counts, endpoint histograms, rate-limit counters, or retry telemetry. Findings below are based on workflow behavior and the explicit 404 evidence.

1. **Log archive retrieval is an observable API failure hotspot**
   - **Evidence:** 404s on `repos/.../actions/runs/{id}/logs` for runs **25051823019, 25051823011, 25051823002, 25051822983**.
   - **Likely issue:** collector is querying log archives before availability, after expiry, or against runs with no retained archives.
   - **Concrete change:** add short retry/backoff for 404 on log archive fetch; if still missing, fall back to per-job artifacts/step summaries.
   - **Estimated call-count reduction:** None directly, but reduces wasted repeated manual/automated debugging calls.
   - **Rate-limit risk reduction:** Low direct effect; high debugging efficiency gain.

2. **Status/poller workflows are likely the highest API-volume control-plane consumers**
   - **Evidence:** frequent lightweight API-oriented families:
     - `issue_pr_status`: **21 runs**, avg **25.1s**
     - `cancel_on_pr_close`: **21 runs**, avg **8.0s**
     - `orchestrate_poll`: **34 runs**, avg **41.7s**
   - **Likely issue:** repeated status lookups on the same PR/issue within short windows.
   - **Concrete change:** introduce cycle-local caches inside each orchestration/poller cycle and share fetched issue/PR state across steps instead of re-querying per step/workflow.
   - **Estimated call-count reduction:** Directionally **20-50%** in these control-plane flows if duplicate reads exist.
   - **Rate-limit risk reduction:** Medium.

3. **No-op workflow fan-out likely causes redundant API reads**
   - **Evidence:** many `clarify/plan/implement/orchestrate_clarify_respond` runs immediately end as skipped/action_required.
   - **Likely issue:** downstream workflows may each re-fetch issue/PR state before discovering they should not run.
   - **Concrete change:** centralize precondition evaluation in the parent orchestrator and pass a compact state payload to the one child workflow that should execute.
   - **Estimated call-count reduction:** Potentially **3-4 workflow-start equivalents** and associated GH reads per no-op decision cycle.
   - **Rate-limit risk reduction:** Medium-high.

4. **No retry telemetry was observed for GH API operations**
   - **Evidence:** sampled failures show **`retries: 0`** at run level, and no per-endpoint retry data was included.
   - **Likely issue:** transient GH API/read failures may be failing directly or being hidden.
   - **Concrete change:** instrument per-endpoint retries and distinguish fail-open vs fail-closed API behavior in step summaries.
   - **Estimated call-count reduction:** Not a reduction by itself; improves auditability and safe retry targeting.
   - **Rate-limit risk reduction:** Medium because retry policy can then be tuned instead of guessed.

**Repository-specific API hygiene note:** No repo-specific batching/cache rules file was included in the sample. I therefore recommend the standard baseline: **batch list reads where possible, maintain cycle-local caches, and fail open on non-critical metadata fetches.**

## MCP & Serena Efficiency

**Data gap:** no MCP/Serena execution traces were included in the supplied window, and no per-step tool-use audit was available. I therefore cannot determine:
- broad reads vs symbol-level reads,
- repeated reads of the same file region,
- avoidable tool churn,
- parallelizable read opportunities already missed.

Best evidence-based recommendations for the pipeline team:

1. **Instrument tool counts per run**
   - Capture counts for:
     - Serena symbol queries,
     - pattern searches,
     - raw file reads,
     - repeated reads of the same path/range,
     - Git MCP diff/log/status calls.
   - **Why:** Without this, token-efficiency regressions are invisible.

2. **Prefer targeted code retrieval in review/implement flows**
   - If Serena is available in the pipeline, use symbol/pattern lookups before raw file reads.
   - **Expected benefit:** lower prompt size and faster turnaround on large repos.
   - **Risk:** low.

3. **Avoid repeated raw reads in multi-wave review**
   - The long-tail `review_autofix` runs are prime candidates for redundant context loading.
   - Cache diff summaries and symbol hits across reviewer waves.
   - **Expected benefit:** lower token load and less review latency.
   - **Risk:** low.

4. **Parallelize independent metadata/code lookups safely**
   - In slow AI phases, independent read-only repo lookups can be parallelized.
   - **Expected benefit:** moderate latency reduction on context assembly.
   - **Risk:** low if reads are truly independent.

Because there is no actual tool telemetry in this sample, treat this section as **instrumentation-first**, not as a verdict on current MCP/Serena quality.

## Prompt Cache & Memory System

**Data gap:** no prompt-cache creation/read metrics, no cache hit/miss counters, and no model token telemetry were supplied.

What can be concluded safely:

1. **Prompt/cache effectiveness cannot be verified from this sample**
   - No evidence was provided for:
     - cache creates,
     - cache reads,
     - hit rate,
     - fail-open behavior,
     - prompt-token reuse.

2. **The long-tail reviewer behavior is where cache hygiene matters most**
   - `review_autofix` is repeatedly executing long runs on the same repository in the same day.
   - If prompt prefixes vary per run due to timestamps/run IDs/dynamic banners, cache fragmentation is likely.

3. **Concrete cache improvements**
   - Keep the instruction/system prefix stable across `review_autofix` and `implement`.
   - Normalize ordering of changed files and repo metadata.
   - Move volatile fields—run ID, timestamps, ephemeral branch notes—to the suffix.
   - Persist compact repo-state summaries between adjacent orchestrator phases instead of rebuilding them.

4. **Expected impact**
   - **Tokens:** likely meaningful in long review/implement paths, but not quantifiable here.
   - **Latency:** modest to moderate, especially on repeated same-repo runs.
   - **Reliability:** improved if fail-open cache behavior prevents hard failures when cache services miss.

5. **Next telemetry to collect**
   - per-run prompt tokens and completion tokens,
   - cache create/read counts,
   - cache hit rate by workflow family,
   - prompt-prefix hash stability across adjacent runs.

## Orchestrator Health

The orchestrator control flow appears **functional but noisy**.

1. **Strong signal of over-dispatch / premature fan-out**
   - `clarify`: **76 total**, **73 other**, **3 success**
   - `plan`: **72 total**, **69 other**, **3 success**
   - `implement`: **72 total**, **67 other**, **3 success**, **2 failure**
   - `orchestrate_clarify_respond`: **72 total**, **71 other**, **1 success**
   - Recent examples on **April 28, 2026** show repeated batches of `skipped` or `action_required` runs finishing in **0-2s**.

2. **What this likely means**
   - The orchestrator is frequently scheduling workflows before the state machine is ready for them, then relying on each child to self-cancel or request action.
   - That is safe, but inefficient and noisy.

3. **Smallest safe mitigations**
   - Add a single pre-dispatch state resolver in the poller/orchestrator.
   - Only launch the next workflow family when prerequisites are satisfied.
   - Attach an idempotency key so the same state transition cannot enqueue duplicate children.
   - Treat `action_required` as a terminal orchestration state until new user/PR input arrives.

4. **Observable indicators to track**
   - `% of orchestrator child workflows ending in skipped/action_required within 5s`
   - `average child workflows launched per user-visible task`
   - `poll cycles per completed task`
   - `duplicate state transition attempts`
   - `cancelled superseded review_autofix runs`

5. **Conflict-heal / stuck-state visibility**
   - No explicit conflict-heal retry telemetry or stuck-state counters were included.
   - Add those counters before changing orchestration logic aggressively.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact across `clarify -> plan -> implement -> review/autofix -> validate/orchestrate`.

1. **Review/autofix compute bottleneck**
   - Dominant latency source.
   - Evidence: `review_autofix` **avg 757.9s**, **p95 2,617.3s**, many runs above **1,400s**, and the slowest successful run at **4,429s**.
   - Fix first: staged review, strict reviewer deadlines, fail-open partial completion.

2. **CI fixed compute floor**
   - Second-largest predictable latency block.
   - Evidence: `ci` **p50 610s**, **p95 638s**.
   - Fix second: selective tests/sharding only after AI-stage waste is reduced.

3. **Implement long-tail failures**
   - Less frequent than review issues, but very expensive when they happen.
   - Evidence: one failed `implement` run at **3,609s**.
   - Fix: preflight checks and watchdogs.

4. **Control-plane orchestration overhead**
   - Not expensive individually, but frequent.
   - Evidence: dozens of `clarify/plan/implement/orchestrate_clarify_respond` runs that exit in **0-2s**.
   - Fix: centralize readiness checks and reduce child fan-out.

5. **Wait-loop overhead in stable-release testing**
   - Rare but acute.
   - Evidence: `test_and_mark_stable` PR-wait failure at **664s**.
   - Fix: deterministic PR detection and shorter bounded waits.

6. **Retry overhead**
   - **Not strongly observed** in this sample; sampled failures show **`retries: 0`**.
   - Recommendation: add scoped retries only for known-transient operations.

7. **Queueing and merge/conflict overhead**
   - **No queue/wait metrics or merge-conflict telemetry were included**, so these cannot be ranked from current evidence.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long tail: **p95 2,617s**, repeated **1,388-4,429s** runs.
  - `ci` stable compute floor: **p50 610s**.
  - Orchestrator child workflow fan-out with many **0-2s skipped/action_required** runs.

- **Top failure modes**
  - Reviewer-model step failures in `review_autofix`:
    - **25027333810**
    - **25026897459**
    - **25026882619**
    - **25027318001**
    - **25046910871**
    - **25045997555**
  - `implement` main-step failures:
    - **25030967036**
    - **25034870641**
  - CI test-step failures:
    - **25031188981**
    - **25031041656**
  - Release waiter failure:
    - **25034760491**
  - Download/tooling failure:
    - **25000347887**

- **Highest-cost drivers**
  - Long reviewer-model runs and cancellations in `review_autofix`.
  - Repeated orchestration no-op launches.
  - Fixed ~10 minute CI validation after AI stages.

- **Top 3 prioritized actions**
  1. **Refactor `review_autofix` into gated triage + fail-open reviewer execution.**
  2. **Stop dispatching no-op child workflows from the orchestrator.**
  3. **Add preflight/watchdog logic to `implement` and deterministic checks to PR wait/download steps.**

## Metrics Appendix

### Repo Summary

| Repository | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 586 | 280 | 13 | 11 | 282 | 2.22% | 250.4 | 7 | 1401 |

### Workflow Family Summary

| Workflow Family | Total | Success | Failure | Cancelled | Other | Failure Rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 114 | 95 | 6 | 11 | 2 | 5.26% | 757.9 | 18 | 2617.3 |
| ci | 70 | 68 | 2 | 0 | 0 | 2.86% | 603.6 | 610 | 638 |
| implement | 72 | 3 | 2 | 0 | 67 | 2.78% | 66.7 | 1 | 173.3 |
| plan | 72 | 3 | 0 | 0 | 69 | 0.00% | 14.8 | 1 | 10 |
| clarify | 76 | 3 | 0 | 0 | 73 | 0.00% | 10.3 | 1 | 7.75 |
| orchestrate_clarify_respond | 72 | 1 | 0 | 0 | 71 | 0.00% | 1.5 | 1 | 2 |
| orchestrate_poll | 34 | 34 | 0 | 0 | 0 | 0.00% | 41.7 | 38.5 | 47.7 |
| issue_pr_status | 21 | 21 | 0 | 0 | 0 | 0.00% | 25.1 | 13 | 66 |
| cancel_on_pr_close | 21 | 21 | 0 | 0 | 0 | 0.00% | 8.0 | 8 | 10 |
| copilot_pull_request_reviewer | 27 | 26 | 1 | 0 | 0 | 3.70% | 208.2 | 195 | 336 |
| test_and_mark_stable | 2 | 1 | 1 | 0 | 0 | 50.00% | 1108.5 | 1108.5 | 1508.6 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.00% | 88.0 | 88 | 88 |

### Representative Failed Runs

| Run ID | Workflow Family | Duration (s) | Failure Point |
|---|---|---:|---|
| 25027333810 | review_autofix | 3879 | review / codex-agent -> Run reviewer models |
| 25026897459 | review_autofix | 3772 | review / codex-agent -> Run reviewer models |
| 25026882619 | review_autofix | 3720 | review-claude-branch-push / codex-agent -> Run reviewer models |
| 25027318001 | review_autofix | 3580 | review-claude-branch-push / codex-agent -> Run reviewer models |
| 25030967036 | implement | 3609 | implement / implement -> Run Codex implementation |
| 25046910871 | review_autofix | 697 | review / codex-agent (claude-branch-review) -> Run reviewer models |
| 25045997555 | review_autofix | 668 | review / codex-agent (claude-branch-review) -> Run reviewer models |
| 25034760491 | test_and_mark_stable | 664 | e2e-smoke-test -> Phase 3b: Wait for PR creation (implement phase) |
| 25031188981 | ci | 549 | lint -> Implement post-Codex recovery unit tests |
| 25031041656 | ci | 485 | lint -> Implement post-Codex recovery unit tests |
| 25034870641 | implement | 393 | implement / implement -> Run Codex implementation |
| 25000347887 | copilot_pull_request_reviewer | 240 | Upload results -> Download ccrcli |
| 25032083242 | nightly_validation_selftest | 88 | validation-selftest -> Run validation self-test matrix |

### Representative Slow Runs

| Run ID | Workflow Family | Conclusion | Duration (s) |
|---|---|---|---:|
| 25027554119 | review_autofix | success | 4429 |
| 25027333810 | review_autofix | failure | 3879 |
| 25026897459 | review_autofix | failure | 3772 |
| 25026882619 | review_autofix | failure | 3720 |
| 25027021627 | review_autofix | success | 3699 |
| 25030967036 | implement | failure | 3609 |
| 25027318001 | review_autofix | failure | 3580 |
| 25029392749 | review_autofix | success | 2099 |
| 25032016590 | review_autofix | success | 2073 |
| 25037222624 | review_autofix | success | 2046 |

### Observability / Telemetry Availability

| Telemetry Category | Present in Sample? | Notes |
|---|---|---|
| Run/job/step durations & outcomes | Yes | Sufficient for bottleneck/failure analysis |
| Token/model usage | No | No prompt/completion/cache token metrics provided |
| Prompt cache create/read metrics | No | Cannot compute hit rate or miss cost |
| GH API call counts/hotspots | No | Only explicit 404 log-archive failures observed |
| MCP/Serena traces | No | No tool-efficiency audit possible |
| AI memory telemetry | No | No `AI_MEMORY_TELEMETRY` lines observed |
| Queue/wait-time metrics | No | Queueing bottlenecks cannot be quantified |

### Explicit GH API Retrieval Errors

| Repository | Run ID | Scope | Error |
|---|---:|---|---|
| shubhodeep1/coding-workflows | 25051823019 | logs | `gh: Not Found (HTTP 404)` |
| shubhodeep1/coding-workflows | 25051823011 | logs | `gh: Not Found (HTTP 404)` |
| shubhodeep1/coding-workflows | 25051823002 | logs | `gh: Not Found (HTTP 404)` |
| shubhodeep1/coding-workflows | 25051822983 | logs | `gh: Not Found (HTTP 404)` |

If you want, I can turn this into a **ranked remediation checklist** with owner/team suggestions and a **1-week implementation plan**.

## Deep Audit — Workflows & Scripts (2026-04-28)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `.github/workflows/review_autofix.yml:2600-2626`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The review gate does a direct `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"` lookup, but if that call yields no data, `pr_state` stays empty and the gate sets `SHOULD_RUN="false"` with `SKIP_REASON="pr_state_unknown"`. Because this fetch is not wrapped in `gh_retry` and the failure path is “skip the workflow” rather than “run without deterministic metadata,” a transient GitHub API failure can suppress the entire review/autofix pass for a valid PR. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))  
  **Recommended fix** — Route the PR metadata fetch through `gh_retry` / `_safe_gh_jq` from `scripts/gh_helpers.sh`, and on exhausted failure fail open into the full review path while disabling only the deterministic-skip branches that depend on labels/additions/deletions. Do not let metadata lookup failure zero out the whole review run. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))

- **ID** — BUG-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3171-3282`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/test-and-mark-stable.yml))  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The step named **“Phase 3b: Wait for PR creation (implement phase)”** waits for “PR creation,” but the implementation shown here establishes a grace timer and then defines `capture_run_id()` around `GET /actions/runs?per_page=100&created=>...`, i.e. it infers success from workflow-run discovery rather than from the PR object itself. That makes the waiter sensitive to Actions indexing lag and workflow-name matching instead of the deterministic head/base PR state the phase is actually waiting on. [NEEDS VERIFICATION] ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/test-and-mark-stable.yml))  
  **Recommended fix** — Make PR existence the primary readiness signal: poll a targeted PR lookup (`gh pr list --head ... --json number,state,url` or a GraphQL PR query) once per loop, and only inspect workflow-run state after the PR is known to exist. Keep the workflow-run search as a secondary diagnostic, not the gate. [NEEDS VERIFICATION] ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/test-and-mark-stable.yml))

- **ID** — BUG-003  
  **File path** — `scripts/review_run_reviewers.sh:3730-3745`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/review_run_reviewers.sh))  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The reviewer watchdog resolves PR state with `gh_retry gh api ... --jq '.state' | grep ... || echo "open"`. Any API failure, parse mismatch, or unexpected state therefore collapses to `"open"`, which means the watchdog keeps the reviewer process running even when PR state is actually unknown or already closed. In this code path, transport failure is indistinguishable from a healthy open PR. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/review_run_reviewers.sh))  
  **Recommended fix** — Split “open,” “closed,” and “unknown” into distinct states. On API/parsing failure, mark the PR state unknown and terminate or degrade the reviewer pass explicitly instead of defaulting to open. If a fail-open policy is required, log a separate sentinel so stale reviewer runs are still auditable. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/review_run_reviewers.sh))

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `.github/workflows/implement.yml:3209-3230`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/implement.yml))  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — This step already has `APPROVAL_COMMENT_ID`, but it still retries `gh api --paginate "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?per_page=100"` up to 5 times and scans the full issue-comment array with `jq any(...)`. **Current call count:** up to 5 paginated list sequences per run, and each sequence can expand to multiple REST calls on long issues. **Proposed call count:** 1 direct `GET /repos/{repo}/issues/comments/{APPROVAL_COMMENT_ID}` call. This is unnecessary list-fetching on a code path that already knows the exact record ID. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/implement.yml))  
  **Recommended fix** — Add a tiny direct-comment helper to `scripts/gh_helpers.sh`, e.g. `fetch_issue_comment_by_id <repo> <comment_id>`, implemented with the existing `gh_retry` wrapper. Update this step to validate the returned single comment instead of paginating the whole thread. **Batching pattern to extend:** none needed; this should be a direct-ID fetch layered on top of `gh_retry`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/implement.yml))

- **ID** — API-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3232-3282`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/test-and-mark-stable.yml))  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — `capture_run_id()` performs a 5-attempt burst against `GET /actions/runs?per_page=100&created=>...`, sleeping 2 seconds between attempts. **Current call count:** 5 list calls per `capture_run_id()` invocation before the outer waiter advances. **Proposed call count:** 1 targeted PR lookup per loop cycle, cached for the rest of the cycle. This is a review-blocker-style polling pattern because it repeatedly lists recent workflow runs to infer a PR-side state change that should be queried directly. [NEEDS VERIFICATION] ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/test-and-mark-stable.yml))  
  **Recommended fix** — Replace the burst polling with a cached PR lookup helper in `scripts/gh_helpers.sh`. If the phase truly needs multi-entity state, follow CLAUDE.md §15’s documented batched/cached GraphQL pattern (`_fetch_linked_pr_status_graphql`) rather than repeatedly listing recent Actions runs. **Batching pattern to extend:** the aliased GraphQL + cycle-local cache guidance called out in `unattended_system_instructions.md` §14. [NEEDS VERIFICATION] ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/test-and-mark-stable.yml))

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/issue_pr_status.yml:1768-1800`; `.github/workflows/review_autofix.yml:3100-3119`; `.github/workflows/review_autofix.yml:3319-3350`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Linked-issue resolution is implemented at least three times with diverging semantics: `issue_pr_status.yml` does `closingIssuesReferences` and then a closing-keyword/body fallback; `review_autofix.yml`’s post-merge validate dispatch also falls back to body/title parsing; `review_autofix.yml`’s deterministic-skip path intentionally forbids regex fallback and stays GraphQL-only. The duplication has already drifted into different contracts, so future bug fixes to issue-link resolution will be easy to apply inconsistently. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Recommended fix** — Move this into one shared helper module, preferably `scripts/gh_helpers.sh` or `scripts/label_helpers.sh`, with a signature like `resolve_linked_issue_numbers <repo> <pr_number> <mode>`, where `<mode>` is `graphql_only` or `graphql_or_keyword_fallback`. Update callers in `issue_pr_status.yml`, `review_autofix.yml` post-merge validate dispatch, and `review_autofix.yml` deterministic-skip-merge to use that single helper. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))

- **ID** — DUP-002  
  **File path** — `.github/workflows/issue_pr_status.yml:1764-1766`; `.github/workflows/implement.yml:3252-3254`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Both workflows silently do `source scripts/gh_helpers.sh 2>/dev/null || true` and then define a local no-op `gh_retry() { "$@"; }` fallback if sourcing fails. That duplicates bootstrap logic and creates contract drift: code that appears to be using the repo’s rate-limit-aware retry helper may actually run raw `gh` calls with no helper semantics at all. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Recommended fix** — Centralize this into a single bootstrap helper, e.g. `scripts/require_gh_helpers.sh` or `ensure_gh_retry`, and stop shadowing `gh_retry` inside workflows. For workflows that require retry semantics for correctness, hard-fail if the helper cannot be sourced; for fail-open workflows, emit an explicit degraded-mode marker instead of silently substituting a no-op. Callers to update: at least `implement.yml` and `issue_pr_status.yml`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))

### Section 4: Expression Size Limit Risk Assessment

- No new Medium/High-risk `${{ }}` blocks were confirmable from the current hot-path excerpts without local measurement. The largest inspected workflow files remain below GitHub’s 1 MB workflow-file ceiling: `.github/workflows/review_autofix.yml` is 252 KB and `.github/workflows/test-and-mark-stable.yml` is 152 KB. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))
- The in-progress report already records the historical expression-limit hotspots in `review_autofix.yml` and `orchestrate_poll_process.sh`; from the current excerpts, those prior hotspots appear to have been moved out of the worst inline positions rather than left untouched.

### Section 5: Cross-Cutting Concerns

- **ID** — CONSIST-001  
  **File path** — `.github/workflows/review_autofix.yml:2764-2768`; `agents.md:796-798`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — The self-triggered-autofix comment in `review_autofix.yml` says the orchestrator stall cron re-kicks autofix “every 30 min” with a 30-minute worst-case window, but `agents.md` documents `internal-orchestrate-poll.yml` at `*/5 * * * *` with a 5-minute worst-case recovery window. That leaves operators with materially different expectations depending on whether they read the workflow or the repo instructions. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))  
  **Recommended fix** — Update the workflow comment to match the current 5-minute cadence, or better, point both docs and comments at one source-of-truth constant/documented schedule so cron changes cannot drift operational guidance again. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 6 | BUG-002, BUG-003, API-001, API-002, DUP-001, DUP-002 |
| Low | 1 | CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Small |
| API call optimization | 2-3 | Small |
| Code modularization | 3 | Medium |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 4-5 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-04-28)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is statically proven safe within the same step/function and preserves filters, pagination, retries, and failure semantics. `NEEDS_VERIFICATION` means the overlap is real but at least one of those safety conditions is not fully provable from static reading alone. `RISKY_SKIP` means the inefficiency is visible, but the call lives in a retry/race-defense/pagination-sensitive path that must not be auto-implemented without manual review.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:1768-1777` and `.github/workflows/issue_pr_status.yml:1948-1967`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Current call count** — 2 GraphQL calls on the path where `ISSUE_NUMBERS` is non-empty.  
  **Proposed call count** — 1 GraphQL call.  
  **Endpoint(s)** — GitHub GraphQL `POST /graphql`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Evidence** — the step first fetches only linked issue numbers from `closingIssuesReferences`, then immediately builds a second GraphQL query to refetch those same issues by number for `labels` and `body`:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    -f owner="${REPOSITORY%/*}" \
    -f name="${REPOSITORY#*/}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
  ...
  ORCH_ALIAS_FRAGMENT+=" i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }"
  ORCH_QUERY="query { repository(owner: \"${REPOSITORY%/*}\", name: \"${REPOSITORY#*/}\") {${ORCH_ALIAS_FRAGMENT} } }"
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ```
  The second call is enrichment-only for the exact issues already discovered by the first call, so the first query is an overlap candidate for direct field expansion. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Proposed fix** — extend the initial `pullRequest(number:$number)` query in `Update linked issue labels when PR closes` to request `closingIssuesReferences(first: 50) { nodes { number body labels(first: 50) { nodes { name } } } }`, parse that response into the existing tracking/managed-issue classifier, and delete the `ORCH_ALIAS_FRAGMENT` / `ORCH_QUERY` round-trip on the GraphQL-success path. If maintainers want to keep an explicit batching helper style, use the aliased-GraphQL batching contract referenced in `unattended_system_instructions.md` §14 (`_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql`). ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Safety rationale** — both calls are in the same workflow step and hit the same GraphQL endpoint with the same auth scope, but static reading here does not fully prove that replacing the alias-shaped `ORCH_RESP` payload preserves the current `ORCH_BATCH_FAILED` fail-open behavior for empty or partial GraphQL responses. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/issue_pr_status.yml))  
  **Downstream signal** — Verify that `closingIssuesReferences.nodes` in this repository’s GraphQL schema accepts `body` and `labels(first:50)`, then run one merged-PR case with multiple linked issues and one regex-fallback-only case to confirm `TRACKING_ISSUES` / `MANAGED_ISSUES` outcomes remain identical before removing `ORCH_QUERY`.

### Redundant Re-Fetch (REUSE-###)
No findings.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- `API-001`: `NEEDS_VERIFICATION` — the direct comment-by-ID fetch is directionally correct, but the implementer must preserve current behavior when the comment is missing or inaccessible because switching from a paginated list read to `GET /issues/comments/{id}` changes the failure shape from “not found in array” to a hard API error/404. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/analysis/workflow-optimization-2026-04-28.md))
- `API-002`: `RISKY_SKIP` — this waiter explicitly rides out Actions indexing lag with local rate-limit backoff before trying to infer readiness, so replacing it changes a race-defense polling path and must not be auto-implemented without manual equivalence testing against real delayed-index scenarios. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/analysis/workflow-optimization-2026-04-28.md))

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | MERGE-001, API-001 |
| RISKY_SKIP | 1 | API-002 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
