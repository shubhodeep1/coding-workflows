## Executive Summary

- **`review_autofix` is the main end-to-end latency and cost sink.** It ran **71 times**, with only **27 successes**, **4 hard failures**, and **40 cancellations**; its **p95 duration is 2,057s (~34m)**, and successful slow runs reached **1,761–2,987s** (`25371973926`, `25353743396`). The common failure signature is `MERGE_CONFLICT=true` with `CONFLICT_RESOLVED=false`, often discovered only after the full reviewer panel ran. **Estimated impact:** cut review-path latency by **10–20m/run** and reduce wasted AI work by **30–50%** on cancelled/failed runs. **Confidence:** high.

- **`test_and_mark_stable` is fully blocked by Phase 4b retry handling.** All **3/3** sampled release-test runs failed (`25324103531`, `25347776357`, `25369768571`), each taking **3,139–3,359s**. In `25369768571`, pytest already proved the canary was still bait-corrupted, then the workflow re-dispatched `review_autofix` and eventually failed with **`retry_timeout`**, even though corresponding review runs had already failed. **Estimated impact:** recover release pipeline reliability from **0% to materially usable**, and save **15–25m** per failing release attempt. **Confidence:** high.

- **CI is stable but slow because a single `lint` job serializes most work.** `ci` has **51/51 successes** but sits at **p50 611s / p95 643.5s**, with repeated runs dominated by `lint` for **~9–11 minutes** (`25370047593`, `25371218961`, `25375054896`). **Estimated impact:** **2–5m** wall-clock reduction per CI run by parallelizing the long test blocks. **Confidence:** high.

- **Prompt cache is enabled but not measurable, and memory retrieval is mostly ineffective.** In sampled deep-dive logs, review runs report `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but cache probes emitted **`prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`** (`25370115370`, `25371432937`, `25353743396`). Memory retrieval telemetry shows only **1 hit in 13 retrieves (7.7%)**, with **12/13 returning 0 records** and `keyword_method="none"` in **12/13** cases. **Estimated impact:** unlock **10–20% token savings** on repeated review paths once instrumented and stabilized. **Confidence:** high.

- **Workflow-log analysis itself is expensive enough to warrant trimming.** In `workflow_log_analysis` run `25369803376`, `summarize_unselected_runs` used **229,172 tokens** to summarize **92 runs**. That is useful, but expensive for a support workflow with no direct customer-facing latency benefit. **Estimated impact:** save **70k–120k tokens/run** with delta summarization or lower sampling. **Confidence:** medium.

- **GH API usage is not rate-limited today, but there are clear high-volume polling and N+1 patterns.** The biggest offender is the E2E Phase 4b poll loop, which polls PR state and candidate review runs every **10s** for up to **30 minutes**; at configured limits that is roughly **180 iterations** and **hundreds of API reads** per failing run. `copilot_pull_request_reviewer` also paginates PR files and separately lists artifacts each run. **Estimated impact:** cut API read volume by **50–80%** on failing E2E runs and lower retry/rate-limit risk elsewhere. **Confidence:** high.

## Speed Optimizations

Ranked by expected latency reduction.

### 1. Short-circuit `review_autofix` before full reviewer fan-out when merge-precheck is already broken
**Critical-path win**

- **Evidence**
  - `review_autofix` p95 is **2,057s**; slow successful runs include `25353743396` at **2,987s** and `25371973926` at **1,761s**.
  - All sampled hard failures in `review_autofix` failed at **`review / codex-agent / Run Codex resolver, validate, stage, commit`** (`25324565713`, `25370025320`, `25370115370`, `25371432937`).
  - Failed runs ended with `MERGE_CONFLICT: true` and `CONFLICT_RESOLVED: false`; `25370115370` also logged:
    - `Merge precheck failed ... HEAD (...) and origin/main (...) have no common ancestor`
    - then marked the run failed only after reviewer work had already completed.

- **Root cause**
  - The expensive reviewer/editor path is running before the workflow proves the branch is mergeable or even shares ancestry with `main`.

- **Exact change**
  - Move a lightweight merge ancestry + simulated merge precheck to the very start of `review_autofix`, before launching the 6-reviewer panel.
  - If the branch has:
    - no common ancestor,
    - merge precheck exit 128,
    - or known unrecoverable merge-state corruption,
    then:
    - emit the dedicated failure classification immediately,
    - upload the same failure bundle,
    - skip reviewer/editor execution entirely.

- **Estimated time savings**
  - **10–20 minutes per failing/cancelled review run**.
  - Also reduces secondary knock-on delay in E2E smoke and orchestrator flows.

- **Implementation risk**
  - **Low-medium.**
  - Safe if this is only an early-exit for states already known to fail later.

---

### 2. Fix Phase 4b retry-run detection so `test_and_mark_stable` stops waiting out the full timeout
**Critical-path win**

- **Evidence**
  - `test_and_mark_stable` is **3/3 failed**, with durations **3,139s**, **3,303s**, **3,359s**.
  - In `25369768571`, Phase 4b logged:
    - `Phase 4b first attempt failed — re-dispatching review_autofix and retrying once`
    - pytest failures:
      - `canary still contains the bait marker`
      - `canary content does not match the issue's required 3-line spec`
    - final result:
      - `Editor Bait. FAILED (retry_timeout)`
  - In the same time window, related `review_autofix` runs for PR `#2112` failed (`25370025320`, `25370115370`, `25371432937`), implying the E2E harness did not correctly identify or consume the retry outcome.

- **Root cause**
  - The retry poll loop appears to over-pin on head SHA / candidate filtering and misses the actual re-dispatched run, so it burns the timeout budget instead of exiting on the observed failure.

- **Exact change**
  - Persist the retry workflow run identity more deterministically:
    - capture dispatch timestamp and branch,
    - match by `workflow_family=review_autofix + PR number + created_at >= dispatch_time`,
    - use head SHA as preferred filter, not the only filter,
    - if a matching run reaches `failure`, stop Phase 4b immediately.
  - Add a fallback: if no exact SHA match appears within a short window, use the newest review_autofix run for the same PR/branch after dispatch time.

- **Estimated time savings**
  - **15–25 minutes** on failing release-test runs.
  - Biggest single improvement for release throughput.

- **Implementation risk**
  - **Low.**
  - This tightens observability and timeout handling without weakening validation.

---

### 3. Parallelize the two long CI test blocks inside `ci`
**Critical-path win**

- **Evidence**
  - `ci` runs are consistently **~9–11 minutes**:
    - `25370047593` = **666s**
    - `25371218961` = **655s**
    - `25375054896` = **644s**
  - `lint` dominates almost the entire run.
  - Recent runs show both:
    - `25 passed, 0 failed`
    - `81 passed, 0 failed`
    being executed in the same long path.

- **Root cause**
  - Independent test blocks appear serialized inside one job, so wall time equals the sum of both suites plus setup.

- **Exact change**
  - Split `lint` into two required jobs:
    - workflow/script checks + smaller pytest block
    - orchestrate-lib unit tests
  - Keep identical pass/fail semantics; only parallelize execution.

- **Estimated time savings**
  - **2–5 minutes per CI run**.

- **Implementation risk**
  - **Medium-low.**
  - Slightly higher runner-minute usage, but low behavior risk.

---

### 4. Trim `orchestrate_poll` checkout/fetch work when `has_work=false`
**Micro-optimization with high frequency**

- **Evidence**
  - `orchestrate_poll` p50 is **49s** across **34 runs**.
  - Recent runs (`25373922220`, `25375155647`) show:
    - `actions/checkout@v5` with `fetch-depth: 0`
    - a second `git fetch ... origin/main`
    - `has_work: false`
    - `push_attempts: 1`
  - The checkout/fetch section alone costs about **10s** in a **51–56s** poll cycle.

- **Root cause**
  - Full-history checkout and repeated `origin/main` fetches are being paid even when the cycle immediately concludes “no work”.

- **Exact change**
  - For the no-work path:
    - switch repository checkout to shallow/no-tags unless full history is explicitly needed,
    - avoid the second `origin/main` fetch when the first checkout already materialized the needed ref,
    - short-circuit support-source staging once tracking-issue scan returns empty.

- **Estimated time savings**
  - **8–15s per poll run**.
  - At 34 runs in the sample, that is **4.5–8.5 minutes** of aggregate latency eliminated.

- **Implementation risk**
  - **Low-medium.**
  - Must verify any downstream branch/ledger logic does not require tags or full history in the no-work case.

---

### 5. Collapse multi-job runner waits in `copilot_pull_request_reviewer`
**Micro-optimization**

- **Evidence**
  - `copilot_pull_request_reviewer` recent/slow runs show repeated runner waits across `Prepare`, `Agent`, `Upload results`, and `Cleanup artifacts` (`25374689917`, `25371774588`, `25370026928`).
  - Runtime is often dominated by job-to-job handoff and final artifact cleanup, not only by analysis.

- **Root cause**
  - Multiple short jobs each pay hosted-runner queue/start overhead.

- **Exact change**
  - Merge `Cleanup artifacts` into the same job that already has artifact IDs available, or defer cleanup to a later scheduled maintenance path.
  - Keep artifact upload separate only if required by workflow isolation.

- **Estimated time savings**
  - **30–90s per run**, sometimes more under runner contention.

- **Implementation risk**
  - **Medium.**
  - Needs validation against current artifact retention/cleanup guarantees.

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1. Replace fixed 6-model reviewer fan-out with staged fan-out on comment-only review paths
- **Evidence**
  - `review_autofix` uses six reviewer models (`minimax`, `kimi`, `deepseek`, `glm`, `qwen`, `grok`) plus `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`.
  - Slow comment-only runs:
    - `25353743396` = **2,987s**
    - `25371973926` = **1,761s**
  - Cancelled comment-only runs still burned significant time:
    - `25371772684` = **312s**
    - `25374688099` = **526s**
    - `25375055227` = **898s**
  - Many of these were explicitly on the `claude-branch-review` / comment-only path where editor/commit/judge were skipped.

- **Root cause**
  - Full reviewer fan-out is always paid even when the workflow is only producing a comment and not acting on code.

- **Exact change**
  - For comment-only / Claude-branch-review mode:
    - start with 2–3 diverse reviewers,
    - expand to all 6 only if initial reviewers disagree or confidence is low,
    - skip second-pass summarization when reviewer consensus is already strong and unchanged from prior run.

- **Estimated savings**
  - **30–50% token savings** on comment-only review runs.
  - Also cuts large amounts of compute time.

- **Quality-risk notes**
  - **Medium.**
  - Keep full 6-model fan-out available for merge-affecting or auto-fix paths; reduce only on non-mutating review mode.

---

### 2. Stop paying for re-dispatched `review_autofix` runs that E2E later ignores
- **Evidence**
  - In `25369768571`, Phase 4b re-dispatched review, then still ended as `retry_timeout`.
  - The same window includes multiple failed `review_autofix` runs for the same smoke-test PR (`25370025320`, `25370115370`, `25371432937`).

- **Root cause**
  - The pipeline is spending model budget on retry review runs without reliably consuming the result.

- **Exact change**
  - Fix retry-run correlation first; then suppress additional redispatch if a qualifying retry run already exists after bait injection.
  - Add dedupe guard keyed by PR + bait SHA + dispatch window.

- **Estimated savings**
  - Eliminates one whole extra `review_autofix` cycle per failing smoke-test attempt.
  - Savings likely **very high** in both tokens and runner minutes.

- **Quality-risk notes**
  - **Low.**
  - This removes duplicate work, not capability.

---

### 3. Reduce `workflow_log_analysis` summarization volume
- **Evidence**
  - `workflow_log_analysis` family averages **2,880s**.
  - In `25369803376`, telemetry reported:
    - `op: summarize_unselected_runs`
    - `summarized: 92`
    - `targeted: 100`
    - `tokens_used: 229172`

- **Root cause**
  - The analyzer is spending substantial token budget summarizing unselected runs every time, even though many runs are repetitive low-signal successes/skips.

- **Exact change**
  - Summarize only:
    - new workflow families not already covered in the current window,
    - runs whose status changed relative to prior windows,
    - or capped samples per family.
  - Keep full deep-dive collection for failures/slow outliers.

- **Estimated savings**
  - **70k–120k tokens per analysis run**.
  - Also likely shaves several minutes off analyzer runtime.

- **Quality-risk notes**
  - **Low-medium.**
  - Needs careful sampling rules so regressions in low-volume families are still surfaced.

---

### 4. Make prompt cache measurable, then stabilize prompt prefixes
- **Evidence**
  - Review runs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - But cache probes in `25370115370`, `25371432937`, and `25353743396` logged all key token fields as `na`.

- **Root cause**
  - Cache is nominally enabled, but telemetry does not reveal creation/read behavior, preventing any tuning.
  - Dynamic run-specific noise likely fragments cacheability.

- **Exact change**
  - First: emit real values for `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.
  - Then:
    - keep invariant system instructions/model config at the top,
    - move volatile fields (run IDs, timestamps, branch SHA, live check-run dumps) after the cache breakpoint,
    - avoid rewriting equivalent prompt prefixes across retries.

- **Estimated savings**
  - **10–20% token reduction** on repeated review paths once working.
  - Small latency improvement from cache hits as a side effect.

- **Quality-risk notes**
  - **Low.**
  - This is observability + prompt ordering, not behavioral logic change.

---

### 5. Cut no-op poll cost by removing full-history checkout in `orchestrate_poll`
- **Evidence**
  - Observed poll cycles frequently end with `has_work: false`, yet still perform full checkout and repeated fetches.
  - `orchestrate_poll` ran **34 times** in the sample.

- **Root cause**
  - The workflow pays the same git/context cost whether there is work or not.

- **Exact change**
  - Use a minimal checkout path for the no-work poll cycle, and only escalate to full context when a tracking issue is found.

- **Estimated savings**
  - Mostly runner-minute savings rather than token savings; still meaningful because of frequency.

- **Quality-risk notes**
  - **Low-medium.**
  - Validate that memory ledger writes still have the git context they need.

## Reliability Improvements

Ranked by expected failure-rate / rerun-rate reduction.

### 1. Fix `review_autofix` merge-precheck / ancestry failures before reviewer execution
- **Failure evidence**
  - Hard failures in `25324565713`, `25370025320`, `25370115370`, `25371432937`.
  - `25370115370` explicitly logged:
    - `Merge precheck failed ... have no common ancestor`
  - Failed runs consistently ended with:
    - `MERGE_CONFLICT: true`
    - `CONFLICT_RESOLVED: false`

- **Root cause category**
  - Branch-state / mergeability precondition failure.

- **Exact fix**
  - Add an initial guard that validates:
    - branch shares ancestry with base,
    - merge precheck can enter a valid merge state,
    - test/smoke branches are created from current `main`.
  - Fail fast with a dedicated status and no reviewer/editor work if the precondition is false.

- **Expected reliability impact**
  - Prevents repeated expensive review failures on branches that were never repairable in-workflow.
  - Should materially reduce `review_autofix` hard failures and some cancellations.

- **Rollback / fail-open**
  - Safe rollback: keep current behavior behind a flag.
  - Fail-open option: for ambiguous states, emit comment-only diagnostics but skip mutation.

---

### 2. Repair E2E Phase 4b retry matching and stop classifying known failed retries as timeouts
- **Failure evidence**
  - `test_and_mark_stable` failure rate is **100% (3/3)**.
  - `25369768571` logged real pytest assertion failures, then ended as `retry_timeout`.

- **Root cause category**
  - Workflow orchestration / retry correlation bug.

- **Exact fix**
  - Record the retry dispatch context and correlate on PR + branch + dispatch time.
  - Stop waiting when the retry run has already concluded `failure`.
  - Distinguish:
    - retry workflow failed,
    - retry run never appeared,
    - retry run succeeded but canary still wrong.

- **Expected reliability impact**
  - Highest-impact fix in the release pipeline; likely the difference between blocked and usable release testing.

- **Rollback / fail-open**
  - Low-risk.
  - If matching is inconclusive, keep current timeout path as fallback.

---

### 3. Add bounded retries to transient GitHub reads in `copilot_pull_request_reviewer`
- **Failure evidence**
  - `actions/github-script@v8` in recent `copilot_pull_request_reviewer` logs showed `retries: 0`.
  - The same path depends on paginated PR file reads and artifact listing/deletion.

- **Root cause category**
  - Transient API-read fragility.

- **Exact fix**
  - Set small bounded retries for:
    - PR file listing in `Prepare`
    - artifact listing in `Cleanup artifacts`
  - Preserve existing exempt status codes.

- **Expected reliability impact**
  - Medium reduction in sporadic failures from GitHub API hiccups and runner/network blips.

- **Rollback / fail-open**
  - Very safe; retries can be disabled without changing correctness.

---

### 4. Improve nightly self-test failure surfacing
- **Failure evidence**
  - `nightly_validation_selftest` run `25356037835` failed with:
    - `fixtures=3 passed=1 failed=2`
  - But the step log does not surface which fixtures failed before exit 1.

- **Root cause category**
  - Diagnostic gap / slow MTTR.

- **Exact fix**
  - Print failing fixture names and failing stages to stdout and step summary before exiting non-zero.

- **Expected reliability impact**
  - Does not directly reduce failure rate, but should sharply reduce time-to-fix and repeated blind reruns.

- **Rollback / fail-open**
  - No behavioral risk.

---

### 5. Retry label removal / validate dispatch in post-merge validate handoff
- **Failure evidence**
  - Run `25370047742` logged:
    - `No standalone validation workflow could be dispatched`
    - `Standalone validation dispatched ... but failed to remove ai:orchestrator-validate-required`
    - continued without failing the workflow.

- **Root cause category**
  - Post-merge handoff hygiene / fail-open drift.

- **Exact fix**
  - Add bounded retry for:
    - validate workflow dispatch
    - label removal
  - Emit a structured warning counter so stale `ai:orchestrator-validate-required` labels can be tracked.

- **Expected reliability impact**
  - Medium-low; reduces stuck validation-required states.

- **Rollback / fail-open**
  - Keep current fail-open behavior after retries exhaust.

## AI Memory Health

- **Telemetry coverage**
  - Parsed **133** `AI_MEMORY_TELEMETRY`-tagged entries across deep-dive logs.
  - Of those, **59** were usable structured entries; many other matches were documentation text or malformed JSON inside analyzer output.

- **Retrieve effectiveness**
  - **13** `retrieve` operations observed.
  - **Hit rate:** **1 / 13 = 7.7%**
  - **Zero-record retrieves:** **12 / 13 = 92.3%**
  - **Average `estimated_tokens`:** **2.15**
  - **`keyword_method` distribution:**
    - `none`: **12**
    - `plain`: **1**
    - `llm`: **0**
  - **`enabled: false` entries:** **0**
  - **`fail_open: true` entries:** **0**
  - Most review-path retrieves were reviewer lookups returning no records:
    - `25370025320`
    - `25370115370`
    - `25371432937`
    - `25353743396`
    - `25355461484`
    - `25356794150`
    - `25368082752`

- **What is working**
  - Memory writes are healthy:
    - `record-run-event` observed **33** times, generally with `push_attempts: 1`
    - `record-candidate` observed **8** times, also with `push_attempts: 1`
  - `memory_maintenance` run `25372104796` compacted memory successfully and archived **2,914 candidate records**, with `did_push: true` and `push_attempts: 1` per the run summary.

- **What is not working**
  - Retrieval is effectively not contributing useful context on the review path.
  - The only hit was a `workflow_log_analysis` implementation retrieve with:
    - `records_selected: 1`
    - `estimated_tokens: 28`
    - `keyword_method: plain`
  - That strongly suggests the current reviewer retrieval path is under-keyed, not that memory is empty.

- **Telemetry gaps**
  - No deep-dive entries showed `fail_open: true`, yet some steps are explicitly labeled fail-open in filenames or comments.
  - This indicates an **emission gap**: the fail-open branch may exist operationally but is not serialized into the runtime JSON payload.

- **Recommendation**
  - Seed retrieve queries with deterministic plain keywords first:
    - issue number
    - PR number
    - touched file paths
    - workflow family
  - Then optionally layer LLM keywording on top.
  - Also verify every fail-open path emits `fail_open: true` in structured telemetry.

## GH API Call Audit

### Highest-volume / highest-redundancy patterns

1. **E2E Phase 4b poll loop (`test_and_mark_stable`)**
   - **Evidence**
     - `25369768571` runs with `REVIEW_TIMEOUT=30` and `POLL_INTERVAL=10`.
     - Phase 4b code repeatedly calls API helpers for:
       - PR state
       - canary contents
       - candidate review runs
     - The loop re-dispatches review and still times out.
   - **Why it matters**
     - At configured limits, this is roughly **180 poll iterations** per failing run; with 2+ read endpoints per iteration, that is **hundreds of API reads** before retries.
   - **Concrete change**
     - Correlate directly to the retry run and stop polling once it reaches a terminal state.
     - Reduce polling to exponential backoff after the first few minutes.
   - **Estimated reduction**
     - **50–80% fewer API reads** on failing E2E runs.
   - **Rate-limit risk reduction**
     - High.

2. **Per-issue fallback lookups in post-merge validate dispatch**
   - **Evidence**
     - `25375705018`:
       - GraphQL query for `closingIssuesReferences`
       - fallback PR body/title fetch if empty
       - `gh issue view` per issue when labels are not already known
       - `gh workflow run`
       - `gh issue edit`
   - **Why it matters**
     - This becomes an **N+1** pattern when multiple linked issues exist.
   - **Concrete change**
     - If GraphQL fallback is needed, fetch issue labels in the same GraphQL call for all discovered issues rather than `gh issue view` per issue.
   - **Estimated reduction**
     - **1–N fewer API calls** per merged PR with linked issues.
   - **Rate-limit risk reduction**
     - Medium.

3. **Repeated artifact listing in `copilot_pull_request_reviewer`**
   - **Evidence**
     - `25374689917` cleanup uses `gh api /repos/.../actions/runs/<run_id>/artifacts`.
     - `Prepare` separately paginates PR files via `github.paginate(github.rest.pulls.listFiles, per_page: 100)`.
   - **Why it matters**
     - Not a crisis, but it is repeated every run and split across jobs.
   - **Concrete change**
     - Reuse artifact IDs and PR metadata across jobs via outputs/artifacts instead of refetching where practical.
   - **Estimated reduction**
     - Small per run, moderate in aggregate.
   - **Rate-limit risk reduction**
     - Low-medium.

4. **Retry wrappers that consult `/rate_limit`**
   - **Evidence**
     - `orchestrate_poll` and `cancel_on_pr_close` explicitly call `gh api -i /rate_limit`.
   - **Why it matters**
     - This is appropriate under actual rate-pressure, but can become overhead if invoked too eagerly.
   - **Concrete change**
     - Keep it only on actual retry paths, not on first-attempt success paths.
   - **Estimated reduction**
     - Small but safe.
   - **Rate-limit risk reduction**
     - Neutral-to-positive.

### Repository API hygiene alignment

The repository’s own review instructions explicitly say GitHub REST/GraphQL budget is shared and data should be batched/reused where possible. The main opportunities that violate the spirit of that rule are:

- Phase 4b repeated poll reads instead of directly following the dispatched retry run
- per-issue fallback fetches after an initial GraphQL issue query
- repeated artifact/PR metadata fetches across separate jobs when the same run already has the data

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed state**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` in review and poll workflows.
  - Review workflows emit `openrouter usage phase=review_autofix_cache_probe`.
- **Problem**
  - The probe output is not actionable:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - Seen in:
    - `25370115370`
    - `25371432937`
    - `25353743396`
- **Interpretation**
  - Cache may be enabled, but current telemetry cannot distinguish hit vs miss, creation vs read, or measure any savings.

### Cache-fragmentation risks
- Review runs clearly include highly dynamic context:
  - PR number
  - branch name / SHA
  - linked issue context
  - live check-run context
  - last-run diff stats
  - canary run IDs in smoke-test paths
- Those volatile inputs likely destabilize prompt prefixes if they appear before the cache breakpoint.

### Memory retrieval effectiveness
- Review-path memory retrieval is largely ineffective:
  - **92.3%** zero-hit rate
  - mostly `keyword_method: none`
- By contrast, the one successful retrieve used `plain` keywords.

### Concrete improvements
1. **Emit real cache accounting fields**
   - Without this, cache work is blind.

2. **Stabilize prompt prefixes**
   - Put invariant system instructions, model policy, and role scaffolding first.
   - Push volatile runtime fields after the breakpoint.

3. **Default memory retrieval to deterministic plain keys**
   - issue/PR number
   - touched paths
   - workflow family
   - prior record category

4. **Emit structured fail-open telemetry**
   - especially on explicitly fail-open retrieval/helper steps.

### Expected impact
- **Tokens:** likely **10–20% savings** on repeated review paths once cache metrics and stable prefixes are in place.
- **Latency:** modest improvement from cache hits and smaller effective prompts.
- **Reliability:** better observability of cache misses, fail-open behavior, and retrieval quality.

## Orchestrator Health

### What looks healthy
- `orchestrate_poll` is operationally stable:
  - **34/34 successes**
  - p50 **49s**
  - sampled runs show `has_work: false` and `push_attempts: 1`
- Clarify/plan/respond gates are fast and usually skip cleanly:
  - `clarify` p50 **1s**
  - `plan` p50 **1s**
  - `orchestrate_clarify_respond` p50 **1s**

### Pain points
1. **Very high “triggered but no work” volume**
   - Large counts of skipped/other runs in:
     - `clarify` (**101 other** of 116)
     - `plan` (**86 other** of 98)
     - `implement` (**83 other** of 98)
     - `orchestrate_clarify_respond` (**95 other** of 98)
   - This is operationally okay, but it indicates a chatty event fan-out.

2. **Conflict-heal / review-blocked pain surfaces late**
   - Repeated `review_autofix` failures end only after expensive reviewer work.
   - Merge conflict state is known but not acted on early enough.

3. **Fail-open behaviors are present, but not always observable**
   - Recent logs include fail-open-labeled steps, but structured telemetry does not preserve `fail_open: true`.

4. **Post-merge validate handoff is permissive**
   - Good for uptime, but easy to miss stale validation-required labels or undispatched validation jobs.

### Smallest safe mitigations
- Track and alert on:
  - `review_autofix` cancelled share
  - `MERGE_CONFLICT=true` + `CONFLICT_RESOLVED=false`
  - `retry_timeout` outcomes in E2E
  - stale `ai:orchestrator-validate-required` labels after merged PRs
  - trigger-to-work ratio for clarify/plan/respond/implement

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact across the pipeline.

### 1. Review/autofix compute dominates the pipeline
- **Where**
  - `review_autofix`
- **Symptoms**
  - p95 **2,057s**
  - slow runs **1,761–2,987s**
  - 40 cancellations out of 71 runs
- **Bottleneck type**
  - Compute + cancellation waste
- **Fix**
  - Early mergeability gating
  - staged reviewer fan-out

### 2. Retry overhead in release smoke testing
- **Where**
  - `test_and_mark_stable` → Phase 4b
- **Symptoms**
  - all three release-test runs failed after **52–56 minutes**
  - timeout spent after the underlying review path had already failed
- **Bottleneck type**
  - Retry / poll-loop overhead
- **Fix**
  - deterministic retry-run correlation and early terminal-state exit

### 3. CI serial execution
- **Where**
  - `ci`
- **Symptoms**
  - stable but **~10 minute** runtime floor
- **Bottleneck type**
  - Compute serialization
- **Fix**
  - parallelize independent test blocks

### 4. Queueing overhead across multi-job workflows
- **Where**
  - `copilot_pull_request_reviewer`, `ci`, `review_autofix`, `orchestrate_poll`
- **Symptoms**
  - repeated `Waiting for a runner to pick up this job...`
  - repeated job-to-job startup delays
- **Bottleneck type**
  - Queueing / runner handoff
- **Fix**
  - collapse low-value extra jobs where safe

### 5. Polling and no-op orchestration overhead
- **Where**
  - `orchestrate_poll`, clarify/plan/respond/implement fan-out
- **Symptoms**
  - many no-work cycles
  - full checkout/fetch even when `has_work=false`
- **Bottleneck type**
  - Queueing + compute overhead on no-op paths
- **Fix**
  - lighter no-work path and better event prefiltering

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-running reviewer path and conflict failure mode
- `test_and_mark_stable` Phase 4b timeout/retry correlation bug
- `ci` serialized `lint` runtime floor around 10 minutes

**Top failure modes**
- `review / codex-agent / Run Codex resolver, validate, stage, commit`
- `e2e-smoke-test / Phase 4b: Verify editor removed bait line`
- `e2e-smoke-test / Phase 4b: Verify editor restored canary (pytest + retry)`

**Highest-cost drivers**
- 6-model reviewer fan-out on comment-only review paths
- repeated retry/re-dispatch in E2E smoke
- workflow-log-analysis summarization (`229,172` tokens in `25369803376`)

**Top 3 prioritized actions**
1. **Move merge-precheck ahead of reviewer fan-out in `review_autofix`.**
2. **Fix Phase 4b retry-run matching and stop waiting out known failures.**
3. **Parallelize the two long CI test blocks and trim no-work orchestrate poll checkout.**

## Metrics Appendix

### Overall repository metrics

| Repo | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 632 | 213 | 8 | 43 | 368 | 1.27% | 169.3 | 2.0 | 641.4 |

### Key workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Other/skipped | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 51 | 51 | 0 | 0 | 0 | 0.0% | 611.4 | 611.0 | 643.5 |
| review_autofix | 71 | 27 | 4 | 40 | 0 | 5.63% | 610.5 | 52.0 | 2057.0 |
| test_and_mark_stable | 3 | 0 | 3 | 0 | 0 | 100.0% | 3267.0 | 3303.0 | 3353.4 |
| orchestrate_poll | 34 | 34 | 0 | 0 | 0 | 0.0% | 48.1 | 49.0 | 54.3 |
| copilot_pull_request_reviewer | 21 | 21 | 0 | 0 | 0 | 0.0% | 228.9 | 222.0 | 373.0 |
| workflow_log_analysis | 3 | 3 | 0 | 0 | 0 | 0.0% | 2880.3 | 2922.0 | 2967.0 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.0% | 100.0 | 100.0 | 100.0 |

### Notable run samples used in analysis

| Run ID | Workflow family | Conclusion | Duration (s) | Key evidence |
|---|---|---|---:|---|
| 25370115370 | review_autofix | failure | 1836 | merge precheck failure, `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false` |
| 25370025320 | review_autofix | failure | 1295 | failed after reviewer work; `LEDGER_ONLY_COMMIT=true`; unresolved merge conflict |
| 25371432937 | review_autofix | failure | 637 | same codex-agent failure signature; unresolved merge conflict |
| 25353743396 | review_autofix | success | 2987 | slow comment-only reviewer path; 6 reviewers; cache probe metrics `na` |
| 25371973926 | review_autofix | success | 1761 | comment-only Claude branch review; `REVIEWERS_SUCCESSFUL: 6` |
| 25369768571 | test_and_mark_stable | failure | 3359 | Phase 4b assertions + retry timeout |
| 25356037835 | nightly_validation_selftest | failure | 100 | `fixtures=3 passed=1 failed=2` |
| 25375054896 | ci | success | 644 | `lint` dominates runtime |
| 25375155647 | orchestrate_poll | success | 56 | `has_work=false`; duplicate fetch/checkout pattern |
| 25369803376 | workflow_log_analysis | success | 2972 | `summarize_unselected_runs` used `229172` tokens |

### Known token telemetry

| Run ID | Workflow family | Telemetry op | Known token usage |
|---|---|---|---:|
| 25369803376 | workflow_log_analysis | `summarize_unselected_runs` | 229,172 |

### Prompt cache telemetry status

| Workflow / run samples | Cache enabled | Cache metrics usable? | Notes |
|---|---|---|---|
| review_autofix (`25370115370`, `25371432937`, `25353743396`) | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) | No | `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` all `na` |

### AI memory retrieval metrics

| Metric | Value |
|---|---:|
| Parsed `retrieve` ops | 13 |
| Retrieves with `records_selected > 0` | 1 |
| Retrieve hit rate | 7.7% |
| Zero-record retrieves | 12 |
| Avg `estimated_tokens` | 2.15 |
| `keyword_method=none` | 12 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| `enabled=false` entries | 0 |
| `fail_open=true` entries | 0 |
| High push retry counts observed | None; sampled pushes were `push_attempts: 1` |

### GH API hotspot summary

| Workflow / step | Pattern | Observed issue | Recommended reduction |
|---|---|---|---|
| `test_and_mark_stable` / Phase 4b | repeated poll reads every 10s for up to 30m | high redundant read volume on failures | 50–80% on failing runs |
| `review_autofix` / post-merge validate dispatch | GraphQL + fallback REST + per-issue `gh issue view` | N+1 issue lookups | batch issue labels in one query |
| `copilot_pull_request_reviewer` / Prepare | `github.paginate(github.rest.pulls.listFiles, per_page: 100)` | repeated full-file listing each run | reuse outputs where possible |
| `copilot_pull_request_reviewer` / Cleanup artifacts | `gh api /actions/runs/<id>/artifacts` | extra fetch in separate job | reuse artifact IDs / consolidate jobs |
| `orchestrate_poll` / retry wrapper | `gh api -i /rate_limit` | small overhead on retry path | keep only on actual retries |

## Deep Audit — Workflows & Scripts (2026-05-05)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/internal-review.yml:91-103`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The `resolve-claude-branch-pr` gate uses raw `gh api` calls under `set -euo pipefail`, but both calls are softened with `|| echo ""` / `|| echo 'main'`. On any transient GitHub API failure, `existing_pr` collapses to empty and the step emits `proceed=true`, which dispatches the no-PR claude-branch review path even when an open PR already exists. Because this path is meant to suppress duplicate reviewer runs, the current fail-open behavior can double-fire review/autofix work and post duplicate review output.  
  **Recommended fix** — Source `scripts/gh_helpers.sh` (or an equivalent early bootstrap helper) and make the PR lookup retryable. If lookup remains inconclusive after retries, fail closed for this gate (`proceed=false`) and log a warning rather than assuming “no open PR”.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:59-77`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The first step in `orchestrate_clarify_respond` calls `gh api` directly for the issue payload and optional tracking title, before any retry helper is sourced. Under `set -euo pipefail`, a transient 403/429/5xx aborts the whole workflow before it can determine whether the issue is orchestrator-managed. Later in the same workflow, the file already uses `gh_retry gh api` for equivalent GitHub reads (`.github/workflows/orchestrate_clarify_respond.yml:397-420`), so the opening gate is the outlier.  
  **Recommended fix** — Reuse the same `gh_retry` pattern already used later in this workflow, or inline a minimal retry wrapper in the first step. On repeated failure, fail open to `is_orchestrator=false`/skip instead of hard-failing the unattended clarify-response path.

- **ID** — `BUG-003`  
  **File path** — `scripts/orchestrate_poll_process.sh:6794-6838`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The standalone stall-recovery `retrigger_review` branch treats the action as successful even when no retrigger actually happened. `git commit --allow-empty` and `git push origin "HEAD:${head_ref}"` are both allowed to fail with `|| true`, but the code still emits the “re-triggered review flow” notification, sets `STALL_RECOVERY_SHOULD_INCREMENT=true`, and marks `took_action=true`. A rejected push, checkout failure, or empty-commit failure therefore consumes recovery budget without producing a `pull_request.synchronize` event.  
  **Recommended fix** — Capture fetch/checkout/commit/push return codes explicitly and only set `took_action=true` after a confirmed push. If the git path fails, fall back to an explicit `gh workflow run review_autofix.yml` dispatch for the linked PR branch, or leave the state unchanged so the next poll can retry honestly.

- **ID** — `SEC-001`  
  **File path** — `.github/workflows/validate.yml:160-169`  
  **Severity** — Medium  
  **Category tag** — `security`  
  **Description** — `Configure git auth for memory helper clones` rewrites `origin` to `https://x-access-token:${GH_TOKEN}@...`, which persists the PAT inside `.git/config`. This is weaker than the safer header-based pattern already used later in the same workflow for support-file fetches (`.github/workflows/validate.yml:196-218`). Any later `git remote -v`, verbose git failure, or accidental workspace capture can expose the token-bearing URL.  
  **Recommended fix** — Replace the remote rewrite with per-command auth, e.g. `git -c http.extraHeader=Authorization: Basic ...`, or use a credential helper scoped to the job. The header-based clone pattern already in this file is the repo-local precedent to copy.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:3407-3519`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The final-merge path re-fetches the same PR repeatedly via separate field-specific calls. In the common “final PR already exists, merge attempt fails or needs re-check” path it does: 2 calls for `state`/`merged_at` (`3411-3412`), then 3 more for `state`/`mergeable`/`merged_at` (`3466-3468`), then another 3 after merge (`3517-3519`). **Current call count:** up to 8 reads against `repos/{repo}/pulls/{final_pr}` in one execution path.  
  **Recommended fix** — Reuse the existing `_fetch_pr_json` + `_jq_field` pattern already defined in this file (`scripts/orchestrate_poll_process.sh:688-718`). Cache one JSON blob per decision point and read all needed fields from it. **Proposed call count after fix:** 3 PR reads (pre-check, pre-merge, post-merge). **Batching/helper pattern to extend:** `_fetch_pr_json` / `_jq_field`.

- **ID** — `API-002`  
  **File path** — `scripts/orchestrate_poll_process.sh:6346-6353`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The standalone-stall candidate sweep enumerates open issues by running `gh issue list` once per label (`ai:clarification`, `ai:planning`, `ai:awaiting-approval`, `ai:implementing`, `ai:done`, `ai:ready-to-merge`, `ai:review-blocked`) and then unions the results in jq. **Current call count:** 7 REST list calls per poll cycle before any per-issue processing. This is precisely the kind of multi-item label discovery that CLAUDE.md §15 says to batch.  
  **Recommended fix** — Extend a GraphQL batch helper so one request returns all open issues carrying any pipeline label, ideally with labels/comments in the same payload. **Proposed call count after fix:** 1 GraphQL query per poll cycle. **Batching/helper pattern to extend:** `_fetch_candidate_issue_details_graphql` or `_fetch_standalone_marker_issues_graphql`.

- **ID** — `API-003`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1739-1745,1913-1949`  
  **Severity** — High  
  **Category tag** — `api-redundancy`  
  **Description** — The Phase 4b retry poll loop keeps polling both PR state and workflow runs every 15 seconds until a 25-minute deadline. That is one `fetch_pr_state` call plus one `actions/workflows/.../runs` call per iteration, even after the retry run becomes identifiable. **Current call count:** about 200 REST reads for a full 25-minute retry window (≈100 iterations × 2 calls), excluding any retries inside `gh_api_with_retry`. This overlaps the in-progress report’s Phase 4b hotspot, but the source code here pins the exact call sites.  
  **Recommended fix** — Capture the retry run once (dispatch timestamp + branch, or run ID once it appears), then poll only `actions/runs/{id}` thereafter; drop the repeated PR-state probe after the run is known unless the PR-close guard is still needed. **Proposed call count after fix:** about 101 reads (1 run-discovery query + ≈100 single-run status reads), or lower if replaced with a dedicated watcher helper. **Batching/helper pattern to extend:** the cycle-local cache pattern already used elsewhere in `orchestrate_poll_process.sh` (`_fetch_pr_json`, `STALL_MANAGED_LINKED_PR_CACHE`).

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `scripts/label_helpers.sh:121-197; scripts/validate_process.sh:500-595; scripts/orchestrate_poll_process.sh:1114-1215; scripts/review_rb_judge.sh:78-110`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo contains at least four near-identical implementations of “ensure label exists” and “replace the issue’s phase label set safely” logic. They all do the same GET-labels / compute replacement / PUT-labels or POST fallback dance, but with slightly different error handling and logging. This is already diverging: `label_helpers.sh` has `set_issue_phase_label_resilient`, while `validate_process.sh` and `orchestrate_poll_process.sh` still carry local copies.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the single owner. Reuse the existing `ensure_label_exists <label_name> [repo]` and promote one canonical phase-swap helper, e.g. `set_issue_phase_label_resilient <issue_number> <target_label> [repo]`. Update callers in `validate_process.sh`, `orchestrate_poll_process.sh`, and `review_rb_judge.sh` to source that module instead of maintaining forked copies.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:27-52; .github/workflows/mark-stable.yml:309-335,458-483; .github/workflows/orchestrate_poll.yml:66-96; .github/workflows/review_autofix.yml:1258-1291; .github/workflows/test-and-mark-stable.yml:453-470,578-595,771-788,1205-1224,1659-1681,4282-4307; .github/workflows/comprehensive-test-and-release.yml:72-98,315-340`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Rate-limit and retry wrappers are duplicated across many workflows (`_rl_wait`, `_gh_retry`, `gh_api_safe`, `gh_api_with_retry`) with different retry counts, backoff rules, and stderr handling. This is already causing policy drift: some wrappers retry non-transient failures, some silently return empty strings, and some consult `/rate_limit` on every rate-limit path.  
  **Recommended fix** — Consolidate on `scripts/gh_helpers.sh` as the canonical implementation for checked-out jobs, and add one bootstrap wrapper module/composite action for pre-checkout jobs. Target signatures should stay aligned with the existing helper surface: `gh_retry gh api ...`, `gh_retry_to_file <path> gh api ...`, `gh_api_json_to_file <path> ...`. Update the listed workflows to call the shared helper instead of maintaining inline variants.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3197-3235,3269-3293,3332-3360,3688-3720,4097-4107`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `test-and-mark-stable.yml` repeats the same “snapshot latest run → dispatch workflow → poll for new run ID → poll status/conclusion” shell block for multiple workflows (`workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-validate`, alt-model capture). The blocks differ only in workflow name, timeout, and accepted conclusions.  
  **Recommended fix** — Extract a shared helper, preferably `scripts/watch_workflow_dispatch.sh`, with a signature like `watch_workflow_dispatch <repo> <workflow_file> <timeout_secs> [accepted_conclusions_csv] [branch_filter]`. Update the repeated callers in `test-and-mark-stable.yml` to pass parameters instead of duplicating the watch loop.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1187-1518`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `wait-review` `run:` block contains `${{ }}` interpolation and is already large enough to be a template-expression risk. Estimated current expanded body size is **~16.6 KB**, leaving only **~4.4 KB** before the 21 KB hard failure. The block mixes helper definitions, retry logic, live-log probing, and inactivity handling in one expression-bearing step, so routine edits can push it over the limit.  
  **Recommended fix** — Extract the whole loop to an external script such as `scripts/e2e_wait_review.sh`, or split helper-definition / wait-loop / diagnostics into multiple smaller steps. External script extraction is the safer option because it removes the run-body from expression accounting entirely.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1604-2009`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The Phase 4b “verify canary + retry review” `run:` block is another interpolation-bearing block near the limit. Estimated current expanded body size is **~17.4 KB**, leaving only **~3.6 KB** of headroom. It combines package install fallback, multiple helper functions, pytest classification, retry dispatch, and polling in one template expression.  
  **Recommended fix** — Move this logic into a dedicated script such as `scripts/e2e_verify_canary_retry.sh`, or split it into separate steps (`install pytest`, `attempt 1`, `retry dispatch/watch`, `attempt 2`). Moving it to `scripts/` is the preferred fix because this block is already a known growth surface.

No workflow file currently exceeds the 800 KB early-warning threshold. The largest audited workflow files are `review_autofix.yml` (**277,694 chars**) and `test-and-mark-stable.yml` (**251,001 chars**), both well below the 1 MB hard cap.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:4765-4771`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is defined but has no in-repo callers. `rg` only finds its definition, while the live standalone-state path uses `_extract_standalone_state_json_from_comments` directly and passes cached `state_comment_id` into `write_standalone_state_json`.  
  **Recommended fix** — Remove the unused wrapper, or switch one caller to it if a comments-fetching abstraction is still desired. As written, it increases maintenance surface without providing a unique behavior.

- **ID** — `SHELL-001`  
  **File path** — `scripts/memory_helpers.sh:56-57`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `memory_ensure_branch()` declares `local token="${GH_TOKEN:-}"`, but never uses it. ShellCheck reports this as `SC2034`, and the surrounding comment says “Resolve authenticated origin URL,” which suggests the token was intended to influence auth but currently does not. The helper therefore silently depends on `origin` already being authenticated.  
  **Recommended fix** — Either remove the dead variable, or actually use it to apply header-based auth when creating/pushing the temporary repo (matching the safer auth patterns already used elsewhere in this repo). That resolves both the ShellCheck warning and the auth-contract ambiguity.

No `TODO`, `FIXME`, or `HACK` markers were found in the audited `.github/workflows/*.yml` and `scripts/*.{sh,py}` scope.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-003, API-003 |
| Medium | 9 | BUG-001, BUG-002, SEC-001, API-001, API-002, DUP-001, DUP-002, EXPR-001, EXPR-002 |
| Low | 3 | DUP-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 2 | Medium |
| Code modularization | 7 | Large |
| Expression size reduction | 1 | Medium |
| Medium/Low fixes | 6 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-05-05)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is repo-local, same-scope, and behavior-preserving from static review alone. `NEEDS_VERIFICATION` means the overlap is real, but a human or follow-up analysis must confirm caller contracts, fallback semantics, or mutation timing before changing it. `RISKY_SKIP` means the redundancy is visible, but it sits in a retry/poll/pagination/race-defense path where this pass is not authorizing auto-implementation.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:188-193`, `.github/workflows/issue_pr_status.yml:220-228`, `.github/workflows/issue_pr_status.yml:295-330`  
  **Current call count** — 2 GitHub GraphQL calls on the common path, plus up to `N` REST fallbacks if the second batch probe fails.  
  **Proposed call count** — 1 GraphQL call on the common path; keep a second lookup only when `BRANCH_ISSUE_NUMBER` adds an issue not already present in `closingIssuesReferences`.  
  **Endpoint(s)** — GitHub GraphQL `repository.pullRequest(number).closingIssuesReferences` and GraphQL `repository.issue(number)` aliases.  
  **Evidence** — The step first fetches linked issue numbers, then later re-queries the same linked issue set for labels/body to classify tracking vs managed issues.
  ```yaml
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    ... \
    -f query='query(...) { repository(...) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
  ```
  ```yaml
  ORCH_QUERY="query { repository(owner: \"${REPOSITORY%/*}\", name: \"${REPOSITORY#*/}\") {${ORCH_ALIAS_FRAGMENT} } }"
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ```
  The second query is only needed because the first query drops `labels`/`body`, even though those fields are what the classifier consumes downstream.  
  **Proposed fix** — Extend the first `closingIssuesReferences` GraphQL query to include `labels(first: 50) { nodes { name } }` and `body`, build the `TRACKING_ISSUES` / `MANAGED_ISSUES` sets directly from that payload, and issue a tiny follow-up alias query only if `BRANCH_ISSUE_NUMBER` introduces a number not already in the first result set. Use the same batched-GraphQL style already established by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Safety rationale** — `NEEDS_VERIFICATION` because the branch-derived issue augmentation changes the final issue set, so the merged query must be proven to preserve classification for three cases: closing-links only, branch-only linkage, and mixed linkage.  
  **Downstream signal** — Verify on PRs covering `closingIssuesReferences`-only, branch-derived-only, and mixed linkage that the merged query produces byte-identical `TRACKING_ISSUES` / `MANAGED_ISSUES` sets before removing the second GraphQL pass.

- **ID** — `MERGE-002`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `.github/workflows/clarify.yml:367-372`  
  **Current call count** — 2 calls to the issue-comments endpoint when `SEMANTIC_CACHE_BACKEND != none`.  
  **Proposed call count** — 1 call in the cache-enabled branch, deriving both outputs from one fetched payload.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/issues/{issue_number}/comments`.  
  **Evidence** — The same issue comments are fetched twice back-to-back: once as a bounded prompt snapshot, then again as a paginated full-thread history.
  ```yaml
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=50" > "${ISSUE_COMMENTS_FILE}"

  if [ "${SEMANTIC_CACHE_BACKEND}" != "none" ]; then
    if ! gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?sort=created&direction=asc&per_page=100" \
  ```
  **Proposed fix** — Manual-review option only: in the semantic-cache-enabled branch, fetch the full paginated comment set once to a temp JSON file, derive `THREAD_HISTORY_FILE` from the full payload, and derive the bounded `ISSUE_COMMENTS_FILE` from the same cached data; leave the cache-disabled branch unchanged.  
  **Safety rationale** — `RISKY_SKIP` because one of the involved calls uses `--paginate`, and consolidating it changes page-boundary/truncation behavior in a prompt-construction path.  
  **Downstream signal** — Do not auto-implement; manual review must prove that the derived `ISSUE_COMMENTS_FILE` stays byte-compatible with the current 50-comment snapshot and that large-thread memory/runtime behavior does not regress.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:175-207`, `.github/workflows/internal-issue-pr-status.yml:1-12`  
  **Current call count** — 1 conditional REST refetch of the PR payload when `PR_DATA` is blank.  
  **Proposed call count** — 0 conditional refetches.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/pulls/{pull_number}`.  
  **Evidence** — The reusable workflow already receives `PR_TITLE` and `PR_BODY` from the closed-PR event payload, then conditionally re-reads the same fields from REST only if that concatenated string is blank.
  ```yaml
  env:
    PR_TITLE: ${{ github.event.pull_request.title }}
    PR_BODY: ${{ github.event.pull_request.body || '' }}
  ...
  PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
  fi
  ```
  The only in-repo caller is the closed-PR wrapper:
  ```yaml
  on:
    pull_request:
      types: [closed]
  ```
  **Proposed fix** — Remove the conditional `GET /pulls/{PR_NUMBER}` fallback from `issue_pr_status.yml` and rely on `${PR_TITLE} ${PR_BODY}` from the `pull_request.closed` event payload already passed into the job.  
  **Safety rationale** — `SAFE_TO_MERGE` because, within this repository, the reusable workflow is only invoked from a `pull_request.closed` wrapper, so the PR title is already available in the same job and no extra API read is needed to recover it.  
  **Downstream signal** — Delete the `gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"` fallback in `issue_pr_status.yml` and keep the existing event-payload string as the sole PR title/body source.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:53-76`, `.github/workflows/implement.yml:528-538`  
  **Current call count** — 2 reads of `GET /repos/{repo}/issues/{ISSUE_NUMBER}` in the same job.  
  **Proposed call count** — 1 issue read, if approval-time snapshot semantics are acceptable.  
  **Endpoint(s)** — GitHub REST `GET /repos/{repo}/issues/{issue_number}`.  
  **Evidence** — The job first fetches state+labels for precheck, then later fetches the same issue again to populate `ISSUE_META_FILE`.
  ```yaml
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ```
  ```yaml
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ISSUE_URL_JSON="$(jq -r '.html_url' "${ISSUE_META_FILE}")"
  ```
  **Proposed fix** — Have `Precheck approval phase label` fetch the full issue JSON once into `${ISSUE_META_FILE}` (or a temp file later promoted to it), parse state/labels from that file for the precheck gate, and let `Fetch issue metadata` reuse the same file unless a product decision explicitly requires “latest issue body/title after queueing.”  
  **Safety rationale** — `NEEDS_VERIFICATION` because there are many intervening setup steps, and a human may intentionally edit the issue body/title/labels between the approval trigger and the later metadata-read step.  
  **Downstream signal** — Verify whether implement should honor mid-run issue edits; if approval-time snapshot semantics are acceptable, persist the first issue payload and remove the second `GET /issues/{ISSUE_NUMBER}` call.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- `API-001`: `RISKY_SKIP` — valid redundancy, but it lives in `scripts/orchestrate_poll_process.sh` final-merge logic, which this pass must not auto-consolidate because that path explicitly defends against upstream races.
- `API-002`: `RISKY_SKIP` — valid batching opportunity, but it is inside the poller/stall-recovery codepath in `scripts/orchestrate_poll_process.sh`, so manual review is required before changing discovery semantics.
- `API-003`: `RISKY_SKIP` — valid hotspot, but it sits inside an explicit retry/poll loop in `test-and-mark-stable.yml`, so this pass must not auto-merge calls there.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 2 | MERGE-001, REUSE-002 |
| RISKY_SKIP | 1 | MERGE-002 |

### Implement-Stage Handoff
- REUSE-001
