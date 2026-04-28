## Executive Summary

*Scope note: direct access to `/home/runner/work/_temp/workflow-log-output` was not available in this environment, so this report is bounded to the supplied `summary`/run metadata and explicit failure points. Token, cache, GH API-count, Serena-trace, and deep-dive excerpt analysis are therefore limited to what was present in the prompt.*

- **The dominant end-to-end bottleneck is `review_autofix`, specifically `Run reviewer models`, with a severe long-tail and stall pattern.** Evidence: `review_autofix` p95 is **2543s**, while failed runs `25027333810`, `25026897459`, `25026882619`, and `25027318001` all died in `Run reviewer models` after **3580–3879s**; one success (`25027554119`) still took **4429s**. **Estimated impact:** cut worst-case review latency by **20–60 minutes/run**. **Confidence:** high.

- **The orchestrator is over-fanning and then skipping work, creating large control-plane churn.** Evidence: `clarify` has **112 total / 105 other**, `plan` **102 / 96 other**, `orchestrate_clarify_respond` **103 / 101 other**, `implement` **102 / 90 other**; recent runs show many same-minute skipped launches around **12:17–12:53 UTC on 2026-04-28**. **Estimated impact:** reduce no-op workflow launches by **50–80%**, improving responsiveness and reducing API/control overhead. **Confidence:** high.

- **`implement` has a bimodal behavior: many immediate skips, but real executions can run 9–60 minutes and fail in the core Codex step.** Evidence: failures `25030967036` (**3609s**), `25052297978` (**2143s**), `25052302522` (**581s**) all failed in `implement / Run Codex implementation`; successes `25052315757` (**569s**) and `25052329233` (**2107s**) show similar spread. **Estimated impact:** save **10–40 minutes** on stalled implementation runs and reduce reruns. **Confidence:** high.

- **CI has a stable ~10.5 minute floor, so it is not the biggest tail driver but is a consistent fixed cost on every meaningful change.** Evidence: `ci` p50 **610s**, p95 **638.4s**, with many successful runs clustered at **604–653s**. **Estimated impact:** a targeted split/conditionalization could save **1–3 minutes per qualifying CI run**. **Confidence:** medium.

- **The current telemetry window is strong for run timing but weak for token/cache/API/memory analysis.** Evidence: `sampled_success_runs` is **0** in the supplied summary; no explicit token totals, cache creation/read counts, GH API call counts, Serena traces, or `AI_MEMORY_TELEMETRY` lines were included. **Estimated impact:** better telemetry would not directly speed runs, but it would materially improve optimization precision and rollback safety. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Add a hard stall budget and fallback path to `Run reviewer models`
**Type:** Critical-path win

- **Evidence:**  
  - `review_autofix` p95 = **2543.3s**.  
  - Failures `25027333810` (**3879s**), `25026897459` (**3772s**), `25026882619` (**3720s**), `25027318001` (**3580s**) all failed at `Run reviewer models`.  
  - Even success `25027554119` took **4429s**.
- **Root cause:** reviewer-model execution appears to stall or run without an enforceable phase budget.
- **Exact change:**  
  - Put a step-level timeout budget on reviewer execution.  
  - Add heartbeat/progress detection; if no forward progress is observed for N minutes, terminate that reviewer branch and either:
    1. return partial review output, or  
    2. fall back to a lighter single-reviewer path.
- **Estimated time savings:** **20–60 minutes** on worst-case review runs; **10–25 minutes** at the family p95 tail.
- **Implementation risk:** low to medium. Main risk is fewer review comments on rare degraded runs; mitigate with fail-open partial output instead of hard job failure.

### 2. De-duplicate orchestrator fan-out before launching `clarify` / `plan` / `implement`
**Type:** Critical-path win

- **Evidence:**  
  - Same-minute clusters of skipped runs in recent activity around **2026-04-28 12:17–12:53 UTC**.  
  - Family mix is skip-heavy: `clarify` **112 total / 105 other**, `plan` **102 / 96 other**, `orchestrate_clarify_respond` **103 / 101 other**, `implement` **102 / 90 other**.
- **Root cause:** workflows are being dispatched before eligibility/work-needed checks are fully resolved, or duplicate events are not single-flighted.
- **Exact change:**  
  - Compute a cycle idempotency key at orchestrator entry: `(repo, issue/PR, head SHA, workflow family, wave)`.  
  - Refuse to dispatch a child workflow if an equivalent in-progress or terminal-same-input run already exists.  
  - Move “should this phase run?” checks ahead of child-workflow dispatch.
- **Estimated time savings:** modest on any one skipped run, but **substantial on wall-clock coordination** during busy bursts; likely **seconds to minutes per wave** and better queue behavior.
- **Implementation risk:** low, if implemented as fail-open dedupe with logging.

### 3. Put the same stall budget / checkpointing on `implement / Run Codex implementation`
**Type:** Critical-path win

- **Evidence:**  
  - Failures at `Run Codex implementation`: `25030967036` (**3609s**), `25052297978` (**2143s**), `25052302522` (**581s**), `25034870641` (**393s**).  
  - Successful implementations show similar long spans: `25052329233` (**2107s**), `25052315757` (**569s**).
- **Root cause:** long-running model execution without an early stuck-state detector or resumable checkpoint.
- **Exact change:**  
  - Enforce periodic progress checkpoints inside the implementation loop.  
  - Abort and emit a resumable state artifact when no meaningful progress occurs within a bounded window.  
  - Resume from last checkpoint rather than restarting the whole implementation.
- **Estimated time savings:** **10–40 minutes** on stalled runs; lower rerun cost.
- **Implementation risk:** medium, because checkpoint boundaries must preserve correctness.

### 4. Reduce polling latency in PR-creation waits
**Type:** Critical-path win

- **Evidence:** failed `test_and_mark_stable` run `25034760491` took **664s** and failed at `Phase 3b: Wait for PR creation (implement phase)`.
- **Root cause:** poll loop likely waits too long, polls too often, or does not reuse implementation outputs that already imply PR creation state.
- **Exact change:**  
  - Pass PR-number/branch metadata forward from the implementation phase when available.  
  - Use exponential backoff with an early-stop condition once the PR exists.  
  - Cap the wait budget and fail open into a diagnostic state instead of a long blind wait.
- **Estimated time savings:** **3–8 minutes** on affected stable-release runs.
- **Implementation risk:** low.

### 5. Attack the fixed ~10.5 minute CI floor with conditionalization or parallelization
**Type:** Local optimization with recurring value

- **Evidence:** `ci` p50 **610s**, p95 **638.4s**, dozens of successful runs clustered between **604s and 653s**.
- **Root cause:** CI likely has a stable serial segment or always-on heavy test subset.
- **Exact change:**  
  - Split always-on lint/static checks from heavier post-Codex recovery tests.  
  - Run non-dependent CI segments in parallel if they are currently serial.  
  - Add path-based conditions so recovery-specific tests run only when affected files change.
- **Estimated time savings:** **1–3 minutes/run** for qualifying CI runs.
- **Implementation risk:** medium until step-level timing confirms the heaviest CI segment.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Prevent repeated long-running reviewer executions that end in failure
- **Evidence:** multiple `review_autofix` failures all terminate in the same step after **3580–3879s**; successful review runs can exceed **2000–4400s**.
- **Root cause:** expensive reviewer-model sessions are allowed to run deep into the tail before failing.
- **Exact change:** add an execution budget, stall detector, and fallback reviewer path as described above.
- **Estimated savings:** likely the largest AI-cost lever in this dataset; saves the full inference cost of the worst stalled review runs.
- **Quality-risk notes:** low if fallback returns partial review rather than suppressing review entirely.

### 2. Stop launching skipped/no-op orchestration phases
- **Evidence:** 394 overall “other” runs across 723 total; especially heavy in `clarify`, `plan`, `orchestrate_clarify_respond`, and `implement`.
- **Root cause:** duplicate or premature workflow dispatches.
- **Exact change:** single-flight dedupe plus pre-dispatch eligibility checks.
- **Estimated savings:** small per run, but high in aggregate for runner startup, workflow dispatch overhead, and control-plane API churn.
- **Quality-risk notes:** low; the safe version only suppresses exact duplicates.

### 3. Right-size model/reasoning level by task size in `review_autofix` and `implement`
- **Evidence:** the same workflow family spans from **14–17s** successes to **>1 hour** tails. That bimodality strongly suggests some runs are trivial while others are expensive.
- **Root cause:** likely using a uniform review/implementation path for heterogeneous workloads.
- **Exact change:**  
  - Route trivial diffs to a lighter reviewer or lower reasoning level.  
  - Reserve the heaviest review path for larger diffs or risky file classes.  
  - Apply the same gating to implementation tasks with small isolated changes.
- **Estimated savings:** potentially high, but currently **unquantified** because token/model telemetry was not provided.
- **Quality-risk notes:** medium; should be guarded by file-count/diff-size thresholds and validated on a canary sample.

### 4. Eliminate avoidable reruns caused by stalled implementations
- **Evidence:** `implement` failures and immediate follow-on skipped/cancelled runs are clustered in recent activity around **12:17 UTC**.
- **Root cause:** orchestration seems to relaunch around unresolved terminal states.
- **Exact change:** mark stuck implementation states explicitly and suppress automatic relaunch until state is reconciled or input changes.
- **Estimated savings:** moderate AI-token and runner-minute savings from avoiding same-input re-execution.
- **Quality-risk notes:** low if suppressed only for identical input tuples.

### 5. Reduce prompt/context variance to improve prompt-cache locality
- **Evidence:** direct prompt-cache metrics were not supplied, but duplicate workflow launches and highly fragmented phase entry paths are typical cache-fragmentation drivers.
- **Root cause:** unstable prefixes, dynamic metadata inserted early, and repeated no-op launches likely reduce cache reuse.
- **Exact change:**  
  - Canonicalize system prompt prefixes.  
  - Move volatile metadata later in prompts.  
  - Standardize ordering of diff summaries, memory snippets, and orchestration metadata.
- **Estimated savings:** unknown until cache telemetry exists; likely medium in AI-heavy phases.
- **Quality-risk notes:** low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Fail open on reviewer-model degradation instead of failing the whole review job
- **Failure evidence:** `review_autofix` failure rate **5.2%** (6/115), all sampled failures hit `Run reviewer models`.
- **Root cause category:** external/model execution instability or long-tail timeout handling gap.
- **Exact fix:** if one reviewer branch stalls or fails, continue with available reviewer output and mark degraded mode in the result.
- **Expected reliability impact:** biggest single drop in review job failures; should reduce hard failures materially.
- **Rollback/fail-open considerations:** easy rollback; degraded reviews are safer than full workflow failures.

### 2. Add idempotency and duplicate-run suppression in the orchestrator
- **Failure evidence:** heavy same-minute skipped/cancelled churn across orchestrator child families; immediate `implement` cancellations (`25052313523` at **23s**, `25052306292` at **13s**) around longer implementation runs.
- **Root cause category:** orchestration/state-management duplication.
- **Exact fix:** enforce one active wave per `(repo, work item, SHA, phase)` and ignore duplicate events unless inputs changed.
- **Expected reliability impact:** lower rerun rate, fewer race-driven cancels/skips, cleaner terminal-state handling.
- **Rollback/fail-open considerations:** implement as advisory logging first, then enforce.

### 3. Replace blind PR-creation waits with bounded polling and explicit diagnostics
- **Failure evidence:** `test_and_mark_stable` run `25034760491` failed after **664s** waiting for PR creation.
- **Root cause category:** synchronization / external-state polling.
- **Exact fix:** store branch/PR metadata from implementation, back off polling, and surface explicit “PR not observed within budget” diagnostics.
- **Expected reliability impact:** fewer phase failures caused by delayed PR visibility.
- **Rollback/fail-open considerations:** fail-open into a reportable unstable state rather than hard failing the entire flow.

### 4. Investigate and isolate the `lint` step that is doing implementation work
- **Failure evidence:** CI failures `25031188981` and `25031041656` both failed in `lint` at `Implement post-Codex recovery unit tests`.
- **Root cause category:** step responsibility ambiguity / overloading.
- **Exact fix:** split “post-Codex recovery unit tests” out of `lint` into a separately named step or job with dedicated retries and diagnostics.
- **Expected reliability impact:** better failure classification, simpler retries, reduced false association with lint failures.
- **Rollback/fail-open considerations:** low; naming/splitting is backward compatible.

### 5. Add success-path deep sampling
- **Failure evidence:** summary reports `sampled_success_runs: 0`, which blocks root-cause analysis for the success path.
- **Root cause category:** observability gap.
- **Exact fix:** sample a small percentage of successful runs for the same token/cache/API/deep-log telemetry already captured for failures/slow runs.
- **Expected reliability impact:** indirect but important; enables safer tuning and regression detection.
- **Rollback/fail-open considerations:** low if sample rate stays small.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were present in the supplied context, so memory telemetry was **not observed** in this analysis window.

Because the deep-dive log excerpts were not available here, I could not compute:

- retrieve hit rate
- average `estimated_tokens` vs budget
- `keyword_method` distribution
- counts of `fail_open: true`
- counts of `enabled: false`
- push retry counts

**Recommendation:** verify that sampled runs emit `AI_MEMORY_TELEMETRY` lines for both success and failure paths, especially for `plan`, `implement`, and `review_autofix`. At minimum, capture `retrieve`, `record-candidate`, `record-run-event`, `finalize-task`, `promote`, `compact`, and processed-command idempotency events.

## GH API Call Audit

Direct GH API call counts and hotspot telemetry were **not included** in the supplied context, so this is a bounded audit based on workflow behavior.

### Likely high-volume patterns

1. **Polling-heavy orchestration**
   - **Evidence:** `orchestrate_poll` ran **34** times with p50 **39s** / p95 **47.7s**.
   - **Likely pattern:** repeated run/PR/status checks in a polling loop.
   - **Recommendation:** batch status fetches per cycle and cache results locally for the duration of a poll wave.
   - **Estimated call reduction:** **30–60%** in poll-heavy cycles.
   - **Rate-limit risk reduction:** medium.

2. **PR creation waiting in stable-release flow**
   - **Evidence:** `Phase 3b: Wait for PR creation (implement phase)` failed after **664s** in `25034760491`.
   - **Likely pattern:** unbatched repeated PR lookups against branch/head SHA.
   - **Recommendation:** reuse implementation outputs first, then poll with exponential backoff and bounded retries.
   - **Estimated call reduction:** **50%+** for that wait phase.
   - **Rate-limit risk reduction:** medium.

3. **Duplicate child workflow launches and status checks**
   - **Evidence:** same-minute skipped runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`.
   - **Likely pattern:** repeated dispatch, existence checks, and terminal-state checks for identical inputs.
   - **Recommendation:** enforce cycle-local caches and idempotency keys before dispatch; do not re-query the same run/PR state within the same orchestration cycle unless the state is expected to change.
   - **Estimated call reduction:** potentially large during bursts, though not directly countable from current data.
   - **Rate-limit risk reduction:** high during noisy event storms.

### Hygiene alignment

The recommended changes align with the repo-level hygiene themes you called out:
- **mandatory batching**
- **cycle-local caches**
- **fail-open behavior**

What is still missing is direct evidence of:
- top API endpoints by call count
- per-step retry counts
- actual rate-limit or abuse-limit events

Those should be added to the next telemetry sample.

## MCP & Serena Efficiency

No Serena or Git MCP traces were present in the supplied context, and no Serena tools were available in this environment to inspect the repo directly. I therefore cannot verify:

- symbol-first lookup usage vs raw file reads
- repeated reads of the same file region
- Git MCP usage vs shell/git fallback
- avoidable tool churn

### Best-effort efficiency recommendations

1. **Instrument tool summaries per AI-heavy step**
   - Track counts of symbol overviews, symbol lookups, pattern searches, raw file reads, Git status/diff/show calls.
   - This will make it possible to quantify broad-read vs targeted-read behavior.

2. **Enforce symbol-first retrieval in review/edit flows**
   - Prefer symbol overview / symbol lookup / reference lookup before raw file reads.
   - Add a guardrail that warns when the same file region is read twice in one task wave.

3. **Increase safe parallelism for independent reads**
   - For large reviews, fetch non-overlapping symbols or files in parallel rather than serial broad reads.
   - This should reduce turnaround time and token duplication without affecting correctness.

4. **Use Git-context fetches before code reads**
   - In review/autofix, obtain changed files and hunks first, then read only impacted symbols.
   - This is the safest way to cut both latency and context size.

Because no trace data was included, these are recommendations rather than confirmed findings.

## Prompt Cache & Memory System

Prompt-cache and memory-system telemetry were **not present** in the supplied context.

### What can still be inferred
- The workflow graph is highly fragmented by skipped/duplicate launches.
- That fragmentation commonly hurts cache locality because nearly identical prompts are re-assembled under slightly different metadata and timing contexts.
- The absence of success-path samples (`sampled_success_runs: 0`) means cache behavior on healthy runs is currently opaque.

### Recommendations

1. **Stabilize prompt prefixes**
   - Keep static instructions at the front.
   - Move volatile fields like timestamps, run IDs, and transient orchestration metadata later.
   - **Expected impact:** medium token savings in `review_autofix` and `implement` once prompt caching is active.

2. **Canonicalize prompt assembly across phases**
   - Use the same ordering for task summary, diff summary, memory retrievals, and constraints.
   - **Expected impact:** fewer cache misses caused by structurally equivalent but textually different prompts.

3. **Emit cache-create and cache-read metrics per step**
   - Capture counts and token volumes for:
     - prompt cache create
     - prompt cache read/hit
     - fail-open cache bypass
   - **Expected impact:** higher-confidence cost tuning and regression detection.

4. **Sample success-path memory retrievals**
   - Without success-path telemetry, retrieval effectiveness cannot be compared between healthy and failing runs.
   - **Expected impact:** better memory budget tuning and fewer silent memory regressions.

## Orchestrator Health

The orchestrator shows signs of **control-plane churn rather than outright instability**.

### Observed health issues

1. **Skip storms / over-fan-out**
   - `clarify`, `plan`, `orchestrate_clarify_respond`, and `implement` frequently launch and then skip within **1–2s**.
   - This is especially visible in recent bursts around **12:17–12:53 UTC on 2026-04-28**.

2. **Uneven wave progression**
   - Some waves proceed into long `implement` or `review_autofix` executions, while many sibling launches terminate immediately.
   - That pattern suggests orchestration decisions are being made after fan-out rather than before it.

3. **Bimodal execution**
   - `review_autofix` has a **17s p50** but **2543s p95**.
   - `implement` has many no-op skips but also real runs at **569s**, **2107s**, **2143s**, and **3609s**.
   - This makes capacity planning and timeout tuning difficult.

### Smallest safe mitigations

- Add a **single-flight key** for child phase dispatch.
- Emit a **wave-level state summary** before launching child workflows.
- Separate **“not needed”**, **“duplicate”**, and **“cancelled by newer wave”** into distinct conclusions/metrics instead of lumping them into `other`.

### Indicators to track

- skipped:successful ratio by workflow family
- duplicate launch count within 60s per work item
- implement cancellations under 30s
- reviewer-model stall count over budget
- median number of poll cycles per orchestrator wave

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Review/autofix model execution
- **Stage:** review/autofix
- **Evidence:** dominant long-tail family; worst success **4429s**, repeated failures in same step at **3580–3879s**.
- **Bottleneck type:** compute + stall handling.
- **Fix:** timeout budget, fallback reviewer path, diff-size routing.

### 2. Implementation step stalls
- **Stage:** implement
- **Evidence:** failures at **393s**, **581s**, **2143s**, **3609s** all in `Run Codex implementation`; successes also long.
- **Bottleneck type:** compute + retry/relaunch overhead.
- **Fix:** checkpointing, stuck-state detection, resumable implementation.

### 3. Orchestrator duplicate/skip overhead
- **Stage:** clarify → plan → implement dispatch
- **Evidence:** large skipped-run volumes and same-minute burst patterns.
- **Bottleneck type:** orchestration/control-plane overhead.
- **Fix:** pre-dispatch gating, idempotency keys, cycle-local caches.

### 4. Polling and synchronization waits
- **Stage:** orchestrate / stable-release wait loops
- **Evidence:** `orchestrate_poll` p50 **39s**, p95 **47.7s**; PR-creation wait failure at **664s**.
- **Bottleneck type:** queueing/synchronization/API polling.
- **Fix:** exponential backoff, shared state propagation, early-stop conditions.

### 5. Stable CI floor
- **Stage:** validate/CI
- **Evidence:** `ci` consistently around **10.5 minutes**.
- **Bottleneck type:** compute, likely serial.
- **Fix:** split always-on vs conditional work, parallelize independent jobs.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail reviewer execution (`Run reviewer models`)
- `implement / Run Codex implementation` stall/failure behavior
- orchestrator duplicate/skip churn
- stable ~10.5 minute CI floor

**Top failure modes**
- reviewer-model step failures in runs `25027333810`, `25026897459`, `25026882619`, `25027318001`, `25046910871`, `25045997555`
- implementation-step failures in `25030967036`, `25052297978`, `25052302522`, `25034870641`
- synchronization failure waiting for PR creation in `25034760491`
- CI/lint-step classification issue in `25031188981`, `25031041656`

**Highest-cost drivers**
- long reviewer-model runs, especially failed ones
- long implementation runs that do not checkpoint or degrade gracefully
- repeated orchestration launches with no work performed

**Top 3 prioritized actions**
1. Add hard budgets + fail-open fallback to reviewer and implementation model steps.
2. Add orchestrator single-flight/idempotency and pre-dispatch eligibility checks.
3. Replace polling-heavy waits with reused state and bounded exponential backoff.

## Metrics Appendix

### Overall repo metrics

| Repository | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 723 | 300 | 15 | 14 | 394 | 2.07% | 221.84 | 2.0 | 1387.6 |

**Interpretation note:** overall p50 is heavily compressed by skipped/no-op runs and is not representative of meaningful work.

### Selected workflow-family metrics

| Workflow Family | Total | Success | Failure | Cancelled | Other | Failure Rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 115 | 96 | 6 | 11 | 2 | 5.22% | 751.45 | 17.0 | 2543.3 |
| implement | 102 | 6 | 4 | 2 | 90 | 3.92% | 100.75 | 1.0 | 392.45 |
| ci | 73 | 71 | 2 | 0 | 0 | 2.74% | 603.45 | 610.0 | 638.4 |
| clarify | 112 | 7 | 0 | 0 | 105 | 0.00% | 12.34 | 1.0 | 93.8 |
| plan | 102 | 6 | 0 | 0 | 96 | 0.00% | 18.92 | 1.0 | 123.05 |
| orchestrate_clarify_respond | 103 | 2 | 0 | 0 | 101 | 0.00% | 1.46 | 1.0 | 2.0 |
| orchestrate_poll | 34 | 34 | 0 | 0 | 0 | 0.00% | 41.85 | 39.0 | 47.7 |
| test_and_mark_stable | 3 | 1 | 1 | 1 | 0 | 33.33% | 1116.33 | 1132.0 | 1510.9 |

### Notable failed runs

| Run ID | Family | Duration (s) | Conclusion | Failure Point |
|---|---|---:|---|---|
| 25027333810 | review_autofix | 3879 | failure | `review / codex-agent` → `Run reviewer models` |
| 25026897459 | review_autofix | 3772 | failure | `review / codex-agent` → `Run reviewer models` |
| 25026882619 | review_autofix | 3720 | failure | `review-claude-branch-push / codex-agent` → `Run reviewer models` |
| 25027318001 | review_autofix | 3580 | failure | `review-claude-branch-push / codex-agent` → `Run reviewer models` |
| 25030967036 | implement | 3609 | failure | `implement / implement` → `Run Codex implementation` |
| 25052297978 | implement | 2143 | failure | `implement / implement` → `Run Codex implementation` |
| 25052302522 | implement | 581 | failure | `implement / implement` → `Run Codex implementation` |
| 25034760491 | test_and_mark_stable | 664 | failure | `e2e-smoke-test` → `Phase 3b: Wait for PR creation (implement phase)` |
| 25031188981 | ci | 549 | failure | `lint` → `Implement post-Codex recovery unit tests` |
| 25031041656 | ci | 485 | failure | `lint` → `Implement post-Codex recovery unit tests` |

### Slow-run highlights

| Run ID | Family | Duration (s) | Conclusion |
|---|---|---:|---|
| 25027554119 | review_autofix | 4429 | success |
| 25027333810 | review_autofix | 3879 | failure |
| 25026897459 | review_autofix | 3772 | failure |
| 25026882619 | review_autofix | 3720 | failure |
| 25030967036 | implement | 3609 | failure |
| 25052143566 | orchestrate | 2274 | success |
| 25052297978 | implement | 2143 | failure |
| 25052329233 | implement | 2107 | success |

### Token / cache / API / memory telemetry availability

| Metric Area | Availability in Supplied Context | Notes |
|---|---|---|
| Token totals by run/step/model | Not available | No prompt/completion/total token fields supplied |
| Model usage breakdown | Not available | No model names or reasoning levels supplied |
| Prompt cache create/read metrics | Not available | No cache hit/miss/create telemetry supplied |
| GH API call counts/hotspots | Not available | Could only infer likely poll-heavy areas from workflow behavior |
| Rate-limit / retry telemetry | Not available | No direct evidence of rate-limit events |
| `AI_MEMORY_TELEMETRY` lines | Not observed | Could not compute hit rates or retrieval budgets |
| Serena / MCP traces | Not observed | No tool trace payloads supplied |

If you want, I can turn this into a shorter “top 10 fixes” version or a backlog-ready table with owner, priority, effort, and acceptance criteria.

## Deep Audit — Workflows & Scripts (2026-04-28)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path**: `.github/workflows/orchestrate.yml:3479-3498; scripts/gh_helpers.sh:2937-2949`
- **Severity**: High
- **Category**: `bug`
- **Description**: Integration-branch creation is a TOCTOU sequence: the workflow reads the default branch, checks whether `orchestrator/project-${TRACKING_ISSUE_NUMBER}` exists, and only then posts the new ref. If two orchestrator runs race on the same tracking issue, both can observe “missing” and one create call will fail with 422. Because `gh_retry` treats 422-class failures as permanent/non-retryable, the losing run does not converge on the branch that now exists. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/orchestrate.yml))
- **Recommended fix**: Make branch creation idempotent: POST the ref optimistically and treat `already exists`/422 as success, or re-read the ref after a failed create and continue if it now exists. The cleanest form is a shared helper such as `ensure_integration_branch <repo> <branch> <base_sha>` that normalizes the 422 path instead of leaving the check-then-create race inline. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/orchestrate.yml))

### Section 2: GitHub API Call Redundancy Audit

#### BATCH-001
- **File path**: `scripts/review_run_reviewers.sh:2294-2297,3641-3755; .github/workflows/review_autofix.yml:2400-2411`
- **Severity**: High
- **Category**: `api-batching`
- **Description**: The review job fans out six reviewer models, performs one preflight `pulls/{pr}` state read, and then each reviewer starts its own watchdog that polls the same PR-state endpoint roughly every nine watchdog ticks (`~90s` per the inline comment). That yields a current call pattern of `1 + 6×ceil(runtime/90s)` to the same endpoint for one boolean decision; a 30-minute run is about `1 + 6×20 = 121` REST calls. This is exactly the kind of per-iteration API loop CLAUDE.md §15 warns against. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/review_run_reviewers.sh))
- **Recommended fix**: Replace per-reviewer polling with one shared job-level PR-state poller that updates the existing `/tmp/pr_closed_sentinel_${PR_NUMBER}` file for all reviewers. That drops the pattern to `1 + ceil(runtime/90s)` calls and matches the repo’s existing “single loader, many consumers” cache style already used in `scripts/orchestrate_poll_process.sh`. **Current call count:** `1 + 6×ceil(runtime/90s)`. **Proposed call count:** `1 + ceil(runtime/90s)`. **Pattern to extend:** the cycle-local cached loader approach used by `scripts/orchestrate_poll_process.sh::_load_actions_runs_cached`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/review_run_reviewers.sh))

#### API-001
- **File path**: `.github/workflows/review_autofix.yml:3100-3113,3319-3328`
- **Severity**: Medium
- **Category**: `api-redundancy`
- **Description**: In the deterministic-skip path, `review_autofix.yml` queries `pullRequest.closingIssuesReferences` twice in the same execution path: first to fetch issue numbers plus labels for validate dispatch, then again to fetch issue numbers only for `ai:ready-to-merge` labeling. That is two GraphQL fetches for one logical dataset that is already in memory after the first call. **Current call count:** 2. **Proposed call count:** 1. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))
- **Recommended fix**: Keep the first `issue_nodes_json` payload, derive both `issue_numbers` and label presence from that one JSON blob with `jq`, and feed both downstream consumers from the derived variables. **Pattern to extend:** the repo’s existing fetch-once/reuse-many file-variable pattern used for issue metadata and cached Actions payloads. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))

#### API-002
- **File path**: `scripts/gh_helpers.sh:3303-3405`
- **Severity**: Medium
- **Category**: `api-redundancy`
- **Description**: `curl_gh_api` only distinguishes rate limits from “everything else,” so deterministic failures such as 401/403/404/422 fall into the generic exponential-retry branch and can consume up to five identical calls before surfacing. That is inconsistent with `gh_retry`/`gh_retry_to_file`, which already short-circuit known permanent failures. **Current call count on deterministic 4xx:** up to 5. **Proposed call count:** 1. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/gh_helpers.sh))
- **Recommended fix**: Add the same permanent-failure gate to `curl_gh_api` that the `gh`-based helpers use—either via direct HTTP-status classification or a shared helper that maps deterministic 4xx to “do not retry.” **Pattern to extend:** `_is_gh_permanent_failure` / `gh_retry`. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/gh_helpers.sh))

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path**: `.github/workflows/plan.yml:2798-2806,2857-2857; .github/workflows/implement.yml:3138-3146,3209-3221; scripts/gh_helpers.sh:3019-3116`
- **Severity**: Medium
- **Category**: `duplication`
- **Description**: Plan and implement both hand-build the same issue-context bootstrap: fetch issue metadata to a file, parse title/body/number/url out of that JSON, then fetch paginated issue comments. The two copies have already started to drift—`plan.yml` uses the shared helper stack, while `implement.yml` adds its own retry loop—which is a classic duplication smell. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/plan.yml))
- **Recommended fix**: Move this into a shared helper owned by `scripts/gh_helpers.sh` or a new `scripts/issue_context_helpers.sh`, with a signature such as `fetch_issue_context <repo> <issue_number> <meta_out> <comments_out>`. Update callers in `plan.yml` and `implement.yml` to consume that one function so parsing, pagination, retry, and JSON validation stay synchronized. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/plan.yml))

#### DUP-002
- **File path**: `.github/workflows/review_autofix.yml:3277-3285,3331-3341; scripts/label_helpers.sh:806-875`
- **Severity**: Low
- **Category**: `duplication`
- **Description**: The deterministic-skip branch in `review_autofix.yml` carries inline label-definition/helper behavior, and the workflow comments explicitly say those inline definitions must “stay in lockstep with the catalog.” That duplicates ownership that is already centralized in `scripts/label_helpers.sh`, increasing drift risk for label creation semantics, descriptions, and future label additions. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))
- **Recommended fix**: Make `scripts/label_helpers.sh` the single owner of label mutation and creation, with `ensure_label_exists <label> <repo>` and `set_issue_phase_label_resilient <issue> <label> <repo>` as the public interface. Update the review-skip path to call the shared helper instead of carrying its own parallel label logic. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/label_helpers.sh))

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path**: `.github/workflows/review_autofix.yml:3086-3349`
- **Severity**: Medium
- **Category**: `expression-limit`
- **Description**: `review_autofix.yml` is still the largest rendered workflow inspected at 252 KB, and this deterministic-skip / linked-issue post-processing step remains a large inline `run:` block in the same workflow that previously hit GitHub’s 21,000-character expression ceiling. Based on the block length and continued `${{ }}`-fed env interpolation around the step, this remains the likeliest regression surface. **Estimated current expanded size:** ~16–19 KB. **Estimated headroom:** ~2–5 KB. `[NEEDS VERIFICATION]` ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))
- **Recommended fix**: Extract the entire block to `scripts/` and pass only environment inputs, following the same mitigation pattern already used when `scripts/orchestrate_poll_process.sh` was split out to relieve expression-length pressure. ([github.com](https://github.com/shubhodeep1/coding-workflows/raw/refs/heads/main/scripts/orchestrate_poll_process.sh))

No rendered workflow inspected here crossed the 800 KB workflow-file threshold; the largest rendered workflow reviewed was `review_autofix.yml` at 252 KB. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/review_autofix.yml))

### Section 5: Cross-Cutting Concerns

#### CONSIST-001
- **File path**: `.github/workflows/implement.yml:3209-3215; .github/workflows/plan.yml:2857-2857; scripts/gh_helpers.sh:3019-3116`
- **Severity**: Medium
- **Category**: `consistency`
- **Description**: `implement.yml` fetches issue comments with a bespoke five-attempt loop and linear sleep, while `plan.yml` already uses the shared helper stack for the same paginated comments endpoint. That leaves the repo with two retry policies for one API surface, and the implement path skips helper-provided JSON validation and rate-limit-reset handling. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/implement.yml))
- **Recommended fix**: Replace the ad-hoc implement loop with `gh_api_json_to_file "${ISSUE_COMMENTS_FILE}" gh api --paginate ...` (or `gh_retry_to_file`) so plan and implement share one retry/backoff/validation contract. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/scripts/gh_helpers.sh))

#### SHELL-001
- **File path**: `.github/workflows/plan.yml:3220-3224; .github/workflows/review_autofix.yml:3333-3347`
- **Severity**: Low
- **Category**: `shellcheck`
- **Description**: Plan deletes clarification comments with `for cid in ${CLARIFY_IDS}; do`, which depends on unquoted shell word-splitting. The repo already uses the safer `while IFS= read -r ...` pattern in `review_autofix.yml` for similar newline-delimited iteration, so this is an avoidable SC2086-style inconsistency. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/plan.yml))
- **Recommended fix**: Rewrite the loop as `while IFS= read -r cid; do ... done <<< "${CLARIFY_IDS}"` so iteration is newline-safe and consistent with the safer pattern already present elsewhere in the repo. ([github.com](https://github.com/shubhodeep1/coding-workflows/blob/main/.github/workflows/plan.yml))

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BATCH-001 |
| Medium | 5 | API-001, API-002, DUP-001, EXPR-001, CONSIST-001 |
| Low | 2 | DUP-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 4 | Medium |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 3 | Small |
