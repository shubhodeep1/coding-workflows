## Executive Summary

- **Fix the Codex CLI invocation mismatch first.** Four `clarify`/`orchestrate` failures and at least one `implement` regression cluster were caused by `codex exec` rejecting `--ask-for-approval` (`orchestrate` run `25473127144`, job `orchestrate / orchestrate`, step `Run Codex (decomposer)`; `clarify` run `25473125487`, job `clarify / clarify`, step `Run Codex`). This is a **high-impact reliability fix** that should remove a whole failure class and avoid wasted retry/backoff time. **Estimated impact:** eliminate most of the current AI-phase hard failures; save ~30–90s per failed attempt chain. **Confidence:** high.

- **`review_autofix` is the largest recurring latency sink and likely the largest avoidable AI spend.** Family metrics show `98` runs, `60` cancelled, `p95=1498.2s`; slow run `25468425312` ran with `ENABLE_REVIEWER_TWO_PASS: true`, `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`, `CHECK_RUNS_POLL_INTERVAL_SECS: 20`, and logged `Two-pass review enabled.` plus `summariser (pass1): 6 input(s); ... prompt_bytes=23101`. Many recent runs were comment-only/branch-review paths yet still long or cancelled (`25474017571`, `25473834179`). **Estimated impact:** cut affected review latency by ~4–12 minutes/run and reduce review token spend materially. **Confidence:** high.

- **`test_and_mark_stable` has the worst end-to-end bottleneck.** Family metrics are `avg=3812s`, `p50=3987s`, `p95=4404.4s`, `failure_rate=20%`. Failed run `25445414047` died in job `e2e-smoke-test`, step `Phase 4: Wait for review & autofix to complete`, with repeated 10-second polling and a 30-minute review timeout. **Estimated impact:** save ~20–30 minutes on bad-path runs and reduce release-validation reruns. **Confidence:** high.

- **`ci` is stable but consistently expensive in wall-clock time.** `ci` ran `77` times with `avg=611.0s`, `p50=615s`, `p95=649.2s`. Recent successes (`25474240463`, `25473834116`, `25473420508`) all show `lint` dominating ~`605–612s`, while smaller test subsets finish quickly (`81 passed`, `25 passed`). **Estimated impact:** 2–4 minutes/run from splitting or parallelizing the slow lane. **Confidence:** medium.

- **Workflow-log analysis is the clearest quantified token-cost hotspot.** `workflow_log_analysis` run `25473131401` emitted `AI_MEMORY_TELEMETRY` with `summarized=85`, `targeted=100`, `tokens_used=214237` on `openai/gpt-5.4-mini`; similar historical runs used `225273`, `240605`, and `241574` tokens. **Estimated impact:** 40–70% token reduction for that workflow with bounded sampling. **Confidence:** high.

- **AI memory retrieval is underperforming.** Across deep-dive logs there were `28` `retrieve` events with only `8` hits (`28.6%` hit rate), `20` zero-record retrieves, and most review/clarify retrieves returned nothing (for example `review_autofix` run `25474017571`, reviewer retrieve; `clarify` run `25473125487`). **Estimated impact:** modest direct latency savings, but meaningful quality/reliability gains if retrieval precision improves. **Confidence:** medium.

## Speed Optimizations

### 1) Fail fast and repin faster in `test_and_mark_stable` Phase 4
**Critical-path win**

- **Evidence:** `test_and_mark_stable` family: `avg=3812s`, `p50=3987s`, `p95=4404.4s`, `failure_rate=20%`. Failed run `25445414047` stopped at job `e2e-smoke-test`, step `Phase 4: Wait for review & autofix to complete`. The log shows `PHASE_TIMEOUT: 30`, `REVIEW_TIMEOUT: 30`, `POLL_INTERVAL: 10`, repeated `gh api` polling on `actions/runs`, `actions/runs/{id}`, and `actions/runs/{id}/jobs`, plus explicit handling for cancelled review runs before eventually timing out.
- **Root cause:** The release smoke test spends long idle time polling downstream review workflow state, especially when a pinned review run is cancelled and a successor run is slow or absent.
- **Exact change:**  
  1. In Phase 4, when the pinned review run becomes `cancelled`, repin once to the newest matching run immediately instead of continuing to poll the dead run path.  
  2. If no successor appears within a short bounded window (for example 2–3 poll cycles), fail the phase early with a specific diagnostic instead of waiting the full `REVIEW_TIMEOUT`.  
  3. Switch from flat `10s` polling to progressive backoff after initial discovery.
- **Estimated time savings:** ~20–30 minutes on bad-path runs like `25445414047`; ~2–5 minutes on slower but eventually successful review waits.
- **Implementation risk:** low-medium. Behavior is backward-compatible if the first few quick polls are preserved before fail-fast.

### 2) Turn off two-pass review for comment-only / branch-review paths
**Critical-path win**

- **Evidence:** `review_autofix` family has `98` runs, `60` cancelled, `p95=1498.2s`. Slow run `25468425312` logged `ENABLE_REVIEWER_TWO_PASS: true`, `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`, `CHECK_RUNS_POLL_INTERVAL_SECS: 20`, `Two-pass review enabled.`, and `summariser (pass1): 6 input(s); target_lines=1080; prompt_bytes=23101`. Recent cancelled run `25474017571` shows a comment-only branch-review path (`AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... comment-only path; editor/commit/judge/auto-merge skipped`) still consuming several minutes before cancellation.
- **Root cause:** Expensive reviewer behavior is applied even when the run is structurally limited to comment-only output and cannot reach edit/merge paths.
- **Exact change:** Gate `ENABLE_REVIEWER_TWO_PASS` behind conditions that require substantive review output, such as non-comment-only mode, larger diff, or explicit failure context. For `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` comment-only runs, force single-pass summarization and skip long check-run waiting where the review result cannot change behavior.
- **Estimated time savings:** ~4–12 minutes on affected `review_autofix` runs; biggest gains on cancelled/comment-only runs.
- **Implementation risk:** low, if guarded only for already-comment-only or deterministic-skip paths.

### 3) Split the `ci` slow lane so `lint` stops owning the entire critical path
**Critical-path win**

- **Evidence:** `ci` family ran `77` times with `avg=611.0s`, `p50=615s`, `p95=649.2s`. Recent successful runs `25474240463`, `25473834116`, `25473420508`, `25472319676` all report `lint` dominating ~`605–612s`. Within those same runs, `lint/Orchestrate lib unit tests` finished quickly (`81 passed`) and other checks (`25 passed`, workflow-ref check) completed successfully.
- **Root cause:** A single aggregated `lint` job serializes several checks behind one long runner allocation and one long wall-clock path.
- **Exact change:** Split the current `lint` lane into separate jobs for:
  - orchestrate-lib unit tests,
  - recovery/integration unit tests,
  - workflow script reference validation,
  - any remaining style/static checks.
  Preserve the same required checks but run them in parallel.
- **Estimated time savings:** **Inference:** ~2–4 minutes off `ci` wall time if the current long lane can be parallelized cleanly.
- **Implementation risk:** medium. Requires job graph changes and artifact/cache reuse review, but no behavior change.

### 4) Stop full-history/tag fetches in `orchestrate_poll` unless work is actually present
**Micro-optimization with high frequency**

- **Evidence:** Recent `orchestrate_poll` run `25474192072` logged `fetch-depth: 0` and a broad fetch command: `git fetch --prune ... +refs/heads/* +refs/tags/*`, with checkout/fetch dominating the run from `03:20:12` to `03:20:21`. The run completed in `45s` and had `has_work=false`.
- **Root cause:** The poller pays full-history and full-tag fetch cost before it knows whether any active tracking work exists.
- **Exact change:** Reorder the flow so issue discovery happens before the broad checkout, then:
  - use a shallow/no-tags checkout when there is no active work,
  - only escalate to broader fetch depth if a work item requires git history/tag inspection.
- **Estimated time savings:** ~5–15s per no-work poll cycle.
- **Implementation risk:** low.

### 5) Reduce duplicate polling calls inside E2E phase wait loops
**Micro-optimization**

- **Evidence:** In `test_and_mark_stable` run `25445414047`, the same step repeatedly calls:
  - `actions/runs?per_page=50|100`,
  - `actions/runs/{id}`,
  - `actions/runs/{id}/jobs?per_page=10`,
  - PR and issue metadata endpoints,
  often in adjacent loop iterations.
- **Root cause:** Phase state, run state, and job state are fetched independently on every poll tick.
- **Exact change:** Cache one poll snapshot per iteration, derive all state transitions from that snapshot, and only refetch subordinate endpoints when the run ID or head SHA changes.
- **Estimated time savings:** ~30–90s on long E2E runs, plus lower GitHub API pressure.
- **Implementation risk:** low.

## Cost Optimizations

### 1) Cut `workflow_log_analysis` unselected-run summarization volume
- **Evidence:** Failed `workflow_log_analysis` run `25473131401`, step `Summarize unselected runs gpt-5.4-mini`, emitted `AI_MEMORY_TELEMETRY` with `summarized=85`, `targeted=100`, `tokens_used=214237`. Additional deep-dive historical runs used `225273`, `240605`, and `241574` tokens for the same operation.
- **Root cause:** The workflow is summarizing too many non-deep-dive runs per analysis window.
- **Exact change:** Lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS` from `100` to a smaller cap such as `30–50`, and stop early once each repo/workflow family has representative coverage. Keep deep-dive `errors/slow/recent` as the primary evidence path.
- **Estimated savings:** ~85k–150k tokens per analysis run (40–70% reduction).
- **Quality-risk notes:** medium. Coverage narrows slightly, so preserve diversity quotas across workflow families when sampling.

### 2) Disable reviewer pass 2 for low-leverage review modes
- **Evidence:** Slow `review_autofix` run `25468425312` used two-pass review with a large summarizer prompt (`prompt_bytes=23101`). Recent runs like `25474017571` and `25473834179` were comment-only branch-review paths where edit/judge/auto-merge were skipped.
- **Root cause:** Expensive summarization/review passes are used where the workflow outcome is capped at comments.
- **Exact change:** For comment-only, branch-review, and deterministic low-risk paths, force single-pass review and bypass second-pass prompt construction.
- **Estimated savings:** token savings are **not directly quantified in the logs**, but likely material for `review_autofix`; latency evidence suggests the cost reduction is also material.
- **Quality-risk notes:** low if limited to paths that already skip editing and merge actions.

### 3) Remove avoidable reruns from the Codex CLI flag regression
- **Evidence:** `orchestrate` run `25473127144` and `clarify` run `25473125487` each retried 3 times against the same parser error: `error: unexpected argument '--ask-for-approval' found`. Review logs for PR `2210` also cited the same regression across runs `25473127144`, `25473129175`, `25473125487`, and `25473129346`.
- **Root cause:** A broken invocation path triggers repeated doomed attempts and downstream remediation activity.
- **Exact change:** Replace the deprecated flags everywhere and add one CI smoke test that runs the exact `codex exec` command shape used in workflows.
- **Estimated savings:** mostly compute/rerun savings rather than model-token savings; removes repeated backoff sleeps and failed workflow churn.
- **Quality-risk notes:** low.

### 4) Fix ineffective cache save/load paths in validation flows
- **Evidence:** Recent validation evidence includes `Cache not found for input keys: validate-hints-v1-...` and a cache warning that the configured path did not exist, so no cache was saved. Across many AI workflow logs, `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears, but no concrete cache-read/cache-write token counters were emitted.
- **Root cause:** Cache is nominally enabled, but some action-level caches are not being populated, and provider-level cache observability is weak.
- **Exact change:** Fix the missing path in the validation cache action and emit explicit cache-hit/miss counters into logs for AI calls.
- **Estimated savings:** unquantified, but likely recurring on validation and repeated prompt-prefix workloads.
- **Quality-risk notes:** low.

### 5) Reduce repeated prompt/context expansion in stable-prefix workflows
- **Evidence:** `clarify` prompt construction explicitly says the static prefix (`pre_assembled_static.txt`) is kept identical to enable provider prompt-prefix caching, which is good. However, many runs still inject dynamic runtime paths, temp file paths, and other per-run noise around AI steps.
- **Root cause:** Prompt variance may be fragmenting cache keys outside the intentionally stable prefix. This is an **inference** because cache-hit counters are missing.
- **Exact change:** Keep all run-specific temp paths, timestamps, and transient metadata out of the prompt body unless required for task semantics; append dynamic content after the stable prefix.
- **Estimated savings:** likely modest per run, but compounded across high-volume `clarify`/`implement`/`review_autofix`.
- **Quality-risk notes:** low.

## Reliability Improvements

### 1) Fix the `codex exec` contract drift everywhere
- **Failure evidence:**  
  - `orchestrate` run `25473127144`, step `Run Codex (decomposer)`: `error: unexpected argument '--ask-for-approval' found`, followed by `Codex orchestrate failed after 3 attempts`.  
  - `clarify` run `25473125487`, step `Run Codex`: same parser error across retries.  
  - Recent PR-review summaries tied the same issue to runs `25473129175` and `25473129346`.
- **Root cause category:** tool/CLI compatibility drift.
- **Exact fix:** Remove deprecated flags from `codex exec` invocations, or add a tiny capability probe that selects the supported argument shape once per run. Also add a CI contract test for the exact workflow invocation form.
- **Expected reliability impact:** high; should remove the dominant AI-phase hard-failure cluster immediately.
- **Rollback / fail-open considerations:** if capability probing is added, default to the simpler supported invocation and surface a warning rather than retrying the same invalid command.

### 2) Stabilize the new recovery / review integration tests in `ci`
- **Failure evidence:** `ci` runs `25473514248` and `25473697143`, job `lint`, step `Implement post-Codex recovery unit tests`, failed with:
  - `FAIL  test_review_pipeline_integration_chain_module_runs_clean`
  - `FAIL  test_chain_happy_path_with_mocked_consolidator`
  - `42 passed, 1 failed, 43 total`
  Another `ci` failure `25469919488` failed in `Orchestrate poll process unit tests`.
- **Root cause category:** test fragility/regression in newly added orchestration/recovery coverage.
- **Exact fix:** Isolate shared state in those test modules, freeze mocked consolidator inputs/ordering, and ensure poll/review chain tests do not depend on ambient repo or timing state.
- **Expected reliability impact:** medium-high; could bring `ci` failure rate down from `3/77` to near-zero if this is the main active flake cluster.
- **Rollback / fail-open considerations:** if immediate stabilization is not possible, temporarily move the flaky subset to a non-blocking lane while preserving signal.

### 3) Make nightly validation self-test failures diagnosable from the main log
- **Failure evidence:** `nightly_validation_selftest` run `25474243471`, step `Run validation self-test matrix`, ended with `validation-selftest: fixtures=3 passed=1 failed=2`.
- **Root cause category:** insufficient failure observability.
- **Exact fix:** Print failing fixture names and first failing assertion/validator summary directly into the main step log and workflow summary, not only into artifacts.
- **Expected reliability impact:** medium; reduces mean time to diagnose and rerun churn on nightly failures.
- **Rollback / fail-open considerations:** safe; additive observability only.

### 4) Convert review-wait false negatives into explicit successor-run errors
- **Failure evidence:** In `test_and_mark_stable` run `25445414047`, the wait logic detected cancelled review runs and continued polling until timeout. The step comments explicitly document the stale-pin/cancelled-run problem.
- **Root cause category:** orchestrator state-transition handling.
- **Exact fix:** After a cancelled review run, require a successor run with a matching updated head SHA within a bounded window; if absent, fail immediately with a targeted message and dump the last observed run list.
- **Expected reliability impact:** medium; fewer misleading “stalled” failures and fewer wasted reruns.
- **Rollback / fail-open considerations:** keep current long-timeout path behind a feature flag if needed.

### 5) Fail once on deterministic parser errors instead of retrying 3 times
- **Failure evidence:** `clarify` and `orchestrate` retried the same exit-code-2 parser error with exponential backoff despite zero chance of success.
- **Root cause category:** retry policy misclassification.
- **Exact fix:** Treat known parser/usage errors from `codex exec` as non-retryable.
- **Expected reliability impact:** low-medium direct failure-rate impact, but high operational clarity.
- **Rollback / fail-open considerations:** safe if matched on explicit usage-error strings only.

## AI Memory Health

- **Telemetry presence:** Present. Across the deep-dive logs, I found `112` `AI_MEMORY_TELEMETRY` events.
- **Operation mix:**  
  - `record-run-event`: `58`  
  - `retrieve`: `28`  
  - `processed-command-check`: `8`  
  - `processed-command-claim`: `8`  
  - `summarize_unselected_runs`: `8`  
  - `record-candidate`: `2`

### Retrieval effectiveness
- **Retrieve hit rate:** `8/28 = 28.6%`
- **Zero-record retrieves:** `20/28`
- **Average `estimated_tokens`:** `16.0`
- **Max `estimated_tokens`:** `56`
- **Estimated tokens vs budget:** no explicit retrieval budget field was emitted in the telemetry; only `estimated_tokens` was present, so budget adherence cannot be directly assessed from current logs.
- **`keyword_method` distribution:**  
  - `plain`: `16`  
  - `llm`: `2`  
  - `none`: `10`

### Notable examples
- **Useful hit:** `implement` run `25470900024` recorded a retrieve with `records_selected=2`, `estimated_tokens=56`, `keyword_method=plain`, role `implementation`.
- **Misses in important paths:**  
  - `orchestrate` run `25473127144` retrieve used `keyword_method=llm` but returned `records_selected=0`.  
  - `clarify` run `25473125487` retrieve returned `records_selected=0`.  
  - `review_autofix` run `25474017571` reviewer retrieve returned `records_selected=0`, `keyword_method=none`.

### Health flags
- **`fail_open: true` entries:** none observed.
- **`enabled: false` entries:** none observed.
- **High push retry counts:** `8` telemetry entries had `push_attempts > 1`; examples include `clarify` phase run-event writes and `workflow_log_analysis` run-start events with `push_attempts: 2`.
- **Assessment:** Memory write plumbing is functioning, but retrieval precision/recall is low in the phases where context would matter most. The system is healthy operationally, but underperforming functionally.

### Recommendation
- Tune retrieval queries by phase:
  - use stronger plain-keyword extraction for `clarify`,
  - use SHA/PR-linked retrieval in `review_autofix`,
  - keep `implementation` retrieval behavior as the current best-performing pattern.
- Also log retrieval candidate counts before selection so misses can be separated into “nothing found” vs “found but filtered out.”

## GH API Call Audit

> No separate repo-specific GH API hygiene document surfaced in the collected logs. The recommendations below align with the repository’s own observed wrappers (`gh_retry`, rate-limit-aware helpers, `/rate_limit` checks).

### 1) `review_autofix` repeatedly polls check runs and PR metadata
- **Evidence:** In `review_autofix` runs `25468425312` and `25474017571`, logs show repeated:
  - `gh api -i /rate_limit`
  - paginated PR comments/reviews fetches
  - `gh api --paginate --slurp "repos/.../commits/${HEAD_SHA}/check-runs?per_page=100"`
- **Pattern:** high-volume repeated lookups inside loops, especially `Collect PR check-run failures`.
- **Recommendation:** Cache PR metadata and comments once per PR/head SHA, and only refresh check-runs when the head SHA or in-flight status changes.
- **Estimated call-count reduction:** **Inference:** 50–80% reduction in check-run polling calls on long review runs.
- **Rate-limit risk reduction:** high.

### 2) `test_and_mark_stable` has polling-heavy phase loops
- **Evidence:** Failed run `25445414047` repeatedly called:
  - `repos/${TEST_REPO}/actions/runs?...`
  - `actions/runs/${RID}`
  - `actions/runs/${RID}/jobs?per_page=10`
  - PR and issue endpoints
  every `10s` across clarify, plan, implement, and review waits.
- **Pattern:** repeated list-then-detail polling across multiple phases.
- **Recommendation:** Use one snapshot per loop iteration, cache discovered run IDs, and increase polling interval over time.
- **Estimated call-count reduction:** hundreds of API calls on the longest E2E runs.
- **Rate-limit risk reduction:** high.

### 3) `orchestrate_poll` does unnecessary repo fetch work around light API activity
- **Evidence:** `orchestrate_poll` run `25474192072` used `_gh_retry gh issue list`, `gh api -i /rate_limit`, and also paid for `fetch-depth: 0` with broad ref fetches despite `has_work=false`.
- **Pattern:** API volume itself is moderate, but the workflow does heavyweight git and API prep before knowing whether there is actionable work.
- **Recommendation:** Query active tracking issues first, then skip broad checkout/fetch if no work is found.
- **Estimated call-count reduction:** modest API savings, moderate network/latency savings.
- **Rate-limit risk reduction:** low-medium.

### 4) Repeated artifact and follow-up metadata fetches in auxiliary workflows
- **Evidence:** `copilot_pull_request_reviewer` run `25473835287` calls `gh api /repos/shubhodeep1/coding-workflows/actions/runs/25473835287/artifacts`. Post-merge validation dispatch uses `gh api graphql`, `gh workflow run`, and issue label edits in short succession.
- **Pattern:** mostly acceptable, but some workflows fetch metadata that could be passed forward as outputs/artifacts instead.
- **Recommendation:** Where a prior job already knows run IDs, artifact IDs, or linked issue IDs, pass them as outputs rather than re-querying.
- **Estimated call-count reduction:** small per run, worthwhile in aggregate.
- **Rate-limit risk reduction:** low.

### 5) Rate-limit handling exists but is compensating for redundancy
- **Evidence:** `review_autofix` logs explicitly include handlers for `rate limit|abuse detection|secondary rate|HTTP 429`, and `/rate_limit` probing is embedded in retry helpers.
- **Pattern:** good defensive code, but the presence of sophisticated rate-limit handling suggests the workflows already expect bursts.
- **Recommendation:** Keep the retry wrappers, but reduce loop redundancy first; retry code should be the safety net, not the throughput plan.
- **Estimated call-count reduction:** indirect.
- **Rate-limit risk reduction:** medium.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed state:** Cache is usually enabled in AI workflows: many logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false` (`clarify`, `orchestrate_poll`, `review_autofix`).
- **Positive design evidence:** `clarify` prompt assembly explicitly says the static prefix is kept identical across runs to enable provider prompt-prefix caching.
- **Observability gap:** I did **not** find concrete provider-side cache read/write token counters in the collected logs.
- **Negative cache evidence:** Validation logs reported `Cache not found for input keys: validate-hints-v1-...`, and one cache save path did not exist, so no cache artifact was written.

### Likely cache-fragmentation causes
- **Inference:** Prefix caching may still be under-realized because runtime-specific noise appears frequently around AI steps:
  - temp file paths,
  - per-run directories,
  - timestamps,
  - dynamic environment dumps.
- The stable-prefix strategy is good, but cache effectiveness cannot be validated without hit/miss counters.

### Memory retrieval effectiveness
- Retrieval exists and is safe operationally, but only `28.6%` of retrieves selected any records.
- Reviewer/clarify retrieves frequently return `0` records, which limits the value of the memory system even when enabled.

### Concrete improvements
1. **Emit provider cache metrics** for every AI call:
   - cache hit/miss,
   - cache read tokens,
   - cache write tokens,
   - prompt token totals.
2. **Keep all dynamic noise after the stable prefix** and exclude temp paths unless semantically necessary.
3. **Fix validation cache save paths** so action cache warnings disappear.
4. **Phase-tune memory retrieval**:
   - `clarify`: plain-keyword + issue title/body emphasis
   - `review_autofix`: PR/head-SHA keyed retrieval
   - `orchestrate`: preserve `llm` keywording only if it outperforms plain mode in a tracked A/B sample
5. **Log retrieval candidate counts and rejection reasons** to diagnose why 20 retrieves returned 0 selected records.

### Estimated impact
- **Tokens:** potentially meaningful but currently unquantified due missing cache counters.
- **Latency:** low-moderate per run, potentially high in aggregate for `clarify`/`review_autofix`.
- **Reliability:** moderate, because better retrieval should reduce redundant reasoning and inconsistent phase behavior.

## Orchestrator Health

### Overall health
- **Good:** The orchestrator control plane is mostly functioning. `orchestrate_poll` is healthy (`30/30` successful family runs, `avg=60.8s`) and records `poll_started`/`poll_completed` ledger events cleanly in run `25474192072`.
- **Concerning:** Execution phases are currently vulnerable to command-contract drift and long downstream wait loops.

### Recurring pain points
1. **Non-actionable retries on deterministic failures**
   - `clarify` and `orchestrate` retried parser errors three times even though the command shape was invalid.
2. **High cancellation churn in review**
   - `review_autofix` had `60` cancelled runs out of `98`.
3. **Long downstream waits dominate end-to-end orchestration**
   - Especially in `test_and_mark_stable` review waiting.
4. **Many no-op/skipped sub-workflows**
   - `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have very low `p50` durations (`1s`) and large `other_count` values, indicating lots of intentional skipped dispatches. This is not a correctness issue, but it does add orchestration noise.

### Smallest safe mitigations
- Stop retrying known non-retryable `codex exec` usage errors.
- Add a “comment-only fast path” in `review_autofix`.
- Promote run-state repinning and exponential backoff in E2E wait loops.
- Track and alert on memory write retries (`push_attempts > 1`).
- Surface failing validation fixture names in-line.

### Observable indicators teams should track
- `% of AI phase failures caused by command/CLI usage errors`
- `review_autofix cancellation ratio`
- `time to first matching downstream run` in E2E phase waits
- `% of review waits ending on cancelled pinned runs`
- `AI memory retrieve hit rate`
- `count of telemetry entries with push_attempts > 1`

## Pipeline Flow Bottlenecks

### Clarify → Plan
- **Observed bottleneck:** mostly not compute, but incorrect invocation when it does run. Many runs are intentionally skipped in ~`1s`; the costly bad path is the broken `codex exec` retry chain (`25473125487`).
- **Fix order:** command-contract fix first.

### Plan → Implement
- **Observed bottleneck:** light in normal cases; `implement` family `avg=25.4s`, but one failure (`25470900024`) aligns with the same Codex invocation instability and low-value retry behavior.
- **Fix order:** same CLI fix, then test stabilization.

### Implement → Review/Autofix
- **Observed bottleneck:** this is the dominant orchestration choke point. `review_autofix` has extreme spread (`p50=60.5s`, `p95=1498.2s`) and heavy cancellation churn.
- **Bottleneck type:** compute + polling + cancellation overhead.
- **Fix order:** comment-only fast path, single-pass review gating, reduced check-run polling.

### Review/Autofix → Validate
- **Observed bottleneck:** in E2E/release flows, the pipeline spends more time waiting for review completion than doing validation itself. In ordinary CI, `lint` remains the main compute bottleneck.
- **Bottleneck type:** downstream dependency wait + serialized CI compute.
- **Fix order:** repin/fail-fast in release E2E, split CI slow lane.

### Orchestrate loops / Polling
- **Observed bottleneck:** poller itself is acceptable, but it performs full-history git work even when no work exists (`25474192072`).
- **Bottleneck type:** unnecessary prep/network overhead.
- **Fix order:** work-detection before full checkout.

### Queueing overhead
- **Observed bottleneck:** many CI and auxiliary runs note hosted-runner wait (`25474240463`, `25473834116`, `25472319676`).
- **Bottleneck type:** queueing.
- **Recommendation:** since adding infrastructure is out of scope, reduce concurrent redundant jobs and shorten long-running lanes so runners free up sooner.

### Merge/conflict overhead
- **Observed evidence:** limited in this window. Forward-merge and promote flows show retry wrappers and full-history fetches, but no strong merge-conflict hotspot emerged from the collected logs.
- **Recommendation:** keep this secondary to review/E2E/CI fixes.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` cancellation-heavy long tail (`98` runs, `60` cancelled, `p95=1498.2s`)
- `test_and_mark_stable` release smoke waits (`avg=3812s`, `p50=3987s`)
- `ci` slow steady-state runtime (`avg=611.0s`, `p50=615s`)

**Top failure modes**
- Codex CLI parser failures in `clarify`/`orchestrate`/`implement` (`--ask-for-approval` rejected)
- Failing integration/recovery tests in `ci` (`25473514248`, `25473697143`, `25469919488`)
- Nightly self-test fixture failures (`25474243471`, `fixtures=3 passed=1 failed=2`)

**Highest-cost drivers**
- Unselected-run summarization in `workflow_log_analysis` (`214237` tokens in run `25473131401`)
- Two-pass reviewer behavior in `review_autofix`
- Repeated long E2E poll loops in `test_and_mark_stable`

**Top 3 prioritized actions**
1. **Replace deprecated `codex exec` flags and add an invocation contract test.**
2. **Turn off two-pass review for comment-only / branch-review paths and reduce check-run polling.**
3. **Refactor `test_and_mark_stable` Phase 4 to repin once, back off polling, and fail fast when no successor review run appears.**

## Metrics Appendix

### Repo-level summary

| Repo | Total Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 298 | 12 | 68 | 622 | 1.2% | 132.7 | 2.0 | 632.0 |

### Key workflow-family metrics

| Workflow Family | Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 77 | 74 | 3 | 0 | 0 | 3.9% | 611.0 | 615.0 | 649.2 |
| `review_autofix` | 98 | 36 | 0 | 60 | 2 | 0.0% | 357.6 | 60.5 | 1498.2 |
| `test_and_mark_stable` | 5 | 2 | 1 | 2 | 0 | 20.0% | 3812.0 | 3987.0 | 4404.4 |
| `workflow_log_analysis` | 5 | 4 | 1 | 0 | 0 | 20.0% | 2410.6 | 2573.0 | 3462.2 |
| `clarify` | 193 | 19 | 4 | 0 | 170 | 2.1% | 13.3 | 1.0 | 99.4 |
| `implement` | 164 | 19 | 1 | 6 | 138 | 0.6% | 25.4 | 1.0 | 199.9 |
| `orchestrate` | 5 | 4 | 1 | 0 | 0 | 20.0% | 147.6 | 156.0 | 173.0 |
| `orchestrate_poll` | 30 | 30 | 0 | 0 | 0 | 0.0% | 60.8 | 51.0 | 147.1 |
| `validation_refresh` | 6 | 6 | 0 | 0 | 0 | 0.0% | 214.0 | 214.5 | 227.8 |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 0 | 100.0% | 100.0 | 100.0 | 100.0 |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events observed | 112 |
| `retrieve` events | 28 |
| Retrieval hit count (`records_selected > 0`) | 8 |
| Retrieval hit rate | 28.6% |
| Zero-record retrieves | 20 |
| Avg `estimated_tokens` per retrieve | 16.0 |
| Max `estimated_tokens` | 56 |
| `keyword_method=plain` | 16 |
| `keyword_method=llm` | 2 |
| `keyword_method=none` | 10 |
| `enabled=false` retrieves | 0 |
| `fail_open=true` retrieves | 0 observed |
| Telemetry entries with `push_attempts > 1` | 8 |

### Workflow-log-analysis token usage

| Run ID | Workflow | Model | Targeted Runs | Summarized Runs | Tokens Used |
|---|---|---|---:|---:|---:|
| `25473131401` | `workflow_log_analysis` | `openai/gpt-5.4-mini` | 100 | 85 | 214,237 |
| historical deep-dive | `workflow_log_analysis` | `openai/gpt-5.4-mini` | n/a | 95 | 225,273 |
| historical deep-dive | `workflow_log_analysis` | `openai/gpt-5.4-mini` | n/a | 99 | 240,605 |
| historical deep-dive | `workflow_log_analysis` | `openai/gpt-5.4-mini` | n/a | 100 | 241,574 |

### Prompt-cache / cache evidence summary

| Signal | Evidence |
|---|---|
| Provider prompt cache enabled | Repeated `OPENROUTER_PROMPT_CACHE_DISABLED: false` in `clarify`, `review_autofix`, `orchestrate_poll` |
| Stable prefix strategy present | `clarify` prompt assembly notes identical static prefix for prompt-prefix caching |
| Concrete provider cache hit counters | Not observed in collected logs |
| Validation cache miss | `Cache not found for input keys: validate-hints-v1-...` |
| Validation cache save problem | Cache path missing, so nothing was saved |

### GH API hotspot summary

| Area | Representative Runs | Observed Pattern | Main Opportunity |
|---|---|---|---|
| `review_autofix` | `25468425312`, `25474017571` | repeated `check-runs`, PR comments/reviews, `/rate_limit` polling | cache by PR/head SHA; refresh only on state change |
| `test_and_mark_stable` | `25445414047` | repeated `actions/runs`, `runs/{id}`, `runs/{id}/jobs`, PR/issue polling every `10s` | consolidate snapshot per loop, exponential backoff |
| `orchestrate_poll` | `25474192072` | `gh issue list` + `/rate_limit` + full-history git fetch on no-work cycle | detect work before broad checkout |
| auxiliary review flows | `25473835287`, recent post-merge validate dispatch runs | artifact and metadata re-queries | pass outputs forward instead of refetching |

### Notable run-specific failure evidence

| Run ID | Workflow Family | Failure Point | Key Evidence |
|---|---|---|---|
| `25473127144` | `orchestrate` | `Run Codex (decomposer)` | `unexpected argument '--ask-for-approval'` |
| `25473125487` | `clarify` | `Run Codex` | same parser error across retries |
| `25470900024` | `implement` | `Run Codex implementation` | retrieval worked, but run failed in AI implementation path |
| `25473514248` | `ci` | `Implement post-Codex recovery unit tests` | `42 passed, 1 failed, 43 total` |
| `25473697143` | `ci` | `Implement post-Codex recovery unit tests` | same failure cluster |
| `25469919488` | `ci` | `Orchestrate poll process unit tests` | separate CI failure cluster |
| `25474243471` | `nightly_validation_selftest` | `Run validation self-test matrix` | `fixtures=3 passed=1 failed=2` |
| `25445414047` | `test_and_mark_stable` | `Phase 4: Wait for review & autofix to complete` | 30-minute review timeout with repeated polling |
