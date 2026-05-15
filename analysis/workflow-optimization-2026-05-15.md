## Executive Summary

- **`review_autofix` is the dominant latency and cost bottleneck.** It ran **76** times with **636.3s avg / 2231.8s p95**. Tail runs were extreme: **25856876392** took **4101s** (`review_codex-agent` **2048.5s** plus `review_codex-agent_system` **2003.3s**), and **25858191744** spent **2143.6s** in `review_codex-agent_system` before only **154.6s** of compute. **Estimated impact:** very high; biggest p95 win is here. **Confidence:** high.

- **PR-less `claude/**` review runs are an avoidable hotspot.** Recent no-PR runs **25898098684 (810s)**, **25898052209 (766s)**, and **25897663527 (690s)** all logged `AUTOFIX_GATE_NO_PR_FALLBACK` and `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR ... reviewer panel + commit-comment path because no PR exists`; two explicitly logged `REVIEWERS_SUCCESSFUL: 6`. **Estimated impact:** save roughly **8-13 min per no-PR run** and reduce queue pressure. **Confidence:** high.

- **CI is the next largest critical-path bucket and is serialized today.** The `ci` family ran **12** times with **678.2s avg / 800.0s p95**; recent successful runs **25897697319 (827s)**, **25898115155 (778s)**, **25898131465 (766s)**, and **25898082421 (718s)** were all dominated by the single `lint` job. **Estimated impact:** **4-6 min** lower wall-clock per CI run by sharding the job (inference). **Confidence:** medium.

- **The only sampled infrastructure failure was transient external download failure, not repo code.** CI run **25897677337** failed in **`lint / Install actionlint`** because `curl` returned **HTTP 504**. **Estimated impact:** materially reduce CI reruns/failures with retry+cache. **Confidence:** high.

- **The validate “failure” was a healthy fix-loop signal, not workflow breakage.** Run **25863792212** failed at **`validate / validate / Enforce validation outcome`**, but the log says **`Validation needs fixes`** and **`Runtime validation failed with 1 failing test(s). A single consolidated fix-up issue was created for 1 root cause(s).`** The diagnosed failing test was `test_renderer_deterministic_output_for_same_manifest`. **Estimated impact:** medium triage-noise reduction if this is tracked separately from infra failure. **Confidence:** high.

- **Semble looks useful; AI memory retrieval does not.** Sampled deep-dive logs contained **10 `SEMBLE_QUERY`** calls totaling **89,401 bytes** at **536ms avg** with **0 `SEMBLE_FALLBACK`** lines, but **8/8** structured memory `retrieve` ops returned **0 records**, **0 estimated tokens**, and `keyword_method=none`. **Estimated impact:** medium cost/relevance gain by fixing or gating memory retrieval; little reason to disable Semble. **Confidence:** high.

## Speed Optimizations

### 1. Shrink the no-PR `claude/**` review path
**Priority:** critical path

- **Evidence:** `review_autofix` no-PR runs **25898098684 (810s)**, **25898052209 (766s)**, and **25897663527 (690s)** all went through `review-claude-branch-push` and logged:
  - `AUTOFIX_GATE_NO_PR_FALLBACK ... reason=force_claude_branch_review_push`
  - `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR pr=none ... reviewer panel + commit-comment path because no PR exists.`
  - Runs **25898098684** and **25898052209** also logged `REVIEWERS_SUCCESSFUL: 6`.
- **Root cause:** the PR-less branch path still fans out to a full reviewer panel even though there is no PR thread, merge gate, or normal autofix loop to justify PR-grade review cost.
- **Exact change:** in the existing no-PR lightweight path, also reduce `REVIEWER_MODELS` to **1-2 fast reviewers** and skip consensus summarization when only one reviewer is used. Keep full multi-reviewer behavior only after a PR exists or when explicitly forced.
- **Estimated time savings:** about **8-13 minutes per no-PR run**; indirect queue relief should also help other workflows.
- **Implementation risk:** **low-medium**. Main risk is missing a marginal review finding on a pre-PR branch; mitigate by restoring the full panel once a PR opens.

### 2. Split the CI `lint` job into parallel shards
**Priority:** critical path

- **Evidence:** the `ci` family averaged **678.2s** over **12** runs. Recent runs **25897697319**, **25898115155**, **25898131465**, and **25898082421** all report that the single `lint` job dominated end-to-end runtime.
- **Root cause:** `.github/workflows/ci.yml` runs static checks, contract tests, coverage gates, lint, shell checks, and schema/prompt validation in one long serial job.
- **Exact change:** split into at least:
  1. **fast static gate** (`yamllint`, `actionlint`, script-ref checks, syntax),
  2. **Python/contract tests**,
  3. **coverage gates / heavier test bundles**.
  Keep the fast static gate required so failures surface quickly.
- **Estimated time savings:** **4-6 minutes per CI run** (inference from current 718-827s wall times and the large number of independent steps).
- **Implementation risk:** **medium**. Coverage-step refactoring is the main coordination cost.

### 3. Skip duplicate workflow-support checkouts on self-repo runs
**Priority:** micro-optimization

- **Evidence:** successful clarify run **25898379788** took **79s**, and its slowest visible step was **`clarify/Checkout workflow support source`** at roughly **31s**. The same multi-checkout support-source pattern exists in `clarify.yml`, `plan.yml`, `implement.yml`, and `orchestrate.yml`.
- **Root cause:** self-repo runs are checking out workflow-support sources even when the workspace already contains the same repo/ref.
- **Exact change:** when `github.repository == shubhodeep1/coding-workflows` and the support ref resolves to the current SHA, reuse the workspace copy of `scripts/`, `prompts/`, and schemas instead of doing the extra `.codex-workflow-src` checkouts.
- **Estimated time savings:** roughly **20-40s per active clarify/plan/implement/orchestrate run**.
- **Implementation risk:** **low** if guarded to self-repo + same-SHA only.

## Cost Optimizations

### 1. Downshift the PR-less review panel
- **Evidence:** the sampled no-PR review runs **25898098684**, **25898052209**, and **25897663527** averaged about **755s**, and two explicitly showed **6 successful reviewers** with no PR present.
- **Root cause:** repeated prompt/context expansion is being multiplied across a multi-reviewer panel for a branch state that cannot take the full PR autofix path.
- **Exact change:** for `CLAUDE_BRANCH_REVIEW_MODE=true` with empty `PR_NUMBER`, use **1-2 cheaper/faster reviewers only**, skip cross-reviewer summarization when there is only one reviewer, and keep the commit-comment output.
- **Estimated savings:** **~60-80% of LLM spend on that path** (directional; token telemetry was not emitted).
- **Quality-risk notes:** **low-medium**. Keep the full reviewer panel for real PRs and for explicit force-review cases.

### 2. Turn prompt-cache measurement on before changing model tiers broadly
- **Evidence:** sampled runs **25898224963** (`orchestrate`) and **25898131499 / 25898082447** (`review_autofix`) logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but sampled deep-dive logs did **not** emit `INFO: openrouter usage`, `cache_creation_input_tokens`, or `cache_read_input_tokens`.
- **Root cause:** prompt cache is enabled, but effectiveness is unobservable in the sampled runs. The repo already has a cache-probe path in `scripts/review_run_reviewers.sh`, but it was not enabled.
- **Exact change:** enable the existing cache probe on a **low-frequency canary** (nightly/self-test or sampled production slice), and keep volatile GH metadata/comment noise at the end of prompts so the static prefix is more stable (inference).
- **Estimated savings:** **not quantifiable from current logs**; if prefix stability improves, repeated reviewer/editor calls likely have **double-digit input-token savings** (inference).
- **Quality-risk notes:** **low**. This is mostly observability + prompt hygiene.

### 3. Stop paying for zero-hit memory retrieval
- **Evidence:** all **8** structured `AI_MEMORY_TELEMETRY` `retrieve` ops in sampled deep dives returned:
  - `records_selected=0`
  - `estimated_tokens=0`
  - `keyword_method=none`
- **Root cause:** the retrieval step is executing without useful keywords or retrievable promoted records.
- **Exact change:** either:
  - short-circuit retrieval when keyword extraction would be `none`, or
  - add a plain-keyword fallback from PR title / issue title / changed file names before invoking memory.
- **Estimated savings:** **small** direct savings per run, but meaningful cleanup of dead work and better prompt relevance once hits start appearing.
- **Quality-risk notes:** **low**.

**Semble note:** I would **not** cut Semble for cost right now. The sampled production footprint was only **10 queries / 89,401 bytes total / 536ms avg / 0 fallbacks**, which looks like targeted context selection rather than noisy bulk prompt expansion.

## Reliability Improvements

### 1. Add retry/backoff (and optionally a version-keyed cache) to `Install actionlint`
- **Failure evidence:** CI run **25897677337** failed in **`lint / Install actionlint`** with `curl: (22) The requested URL returned error: 504`.
- **Root cause category:** external dependency fetch flake.
- **Exact fix:** retry the tarball download with exponential backoff, preserve checksum verification, and optionally cache the versioned tarball or extracted binary by `ACTIONLINT_VERSION`.
- **Expected reliability impact:** high for this repo’s CI, because the sampled `ci` family had **1 failure in 12 runs (8.3%)**, and this was that failure.
- **Rollback / fail-open:** fail **closed** on checksum mismatch; only retry network errors.

### 2. Separate `needs_fixes` validation outcomes from infrastructure failures
- **Failure evidence:** validate run **25863792212** failed at **`Enforce validation outcome`**, but the same log recorded:
  - `SUMMARY_VALUE: Validation needs fixes`
  - `FAILURE_SUMMARY_VALUE: Runtime validation failed with 1 failing test(s). A single consolidated fix-up issue was created for 1 root cause(s).`
- **Root cause category:** expected product/repo validation miss being counted as a workflow failure.
- **Exact fix:** first, teach analysis/reporting to classify `raw_status=needs_fixes` separately from infra errors. If downstream automation allows it, consider making the workflow conclude successfully after creating the fix-up issue while preserving a strong summary/status output.
- **Expected reliability impact:** medium. This reduces false incident triage and pointless reruns.
- **Rollback / fail-open:** safest path is **analytics-only reclassification first**; keep GitHub’s red outcome until downstream dependencies are verified.

### 3. Remove Node 20 deprecation debt before it becomes a hard break
- **Failure evidence:** recent runs **25898224963** (`orchestrate`) and **25891755197** (`orchestrate_poll`) logged `Node.js 20 is deprecated` warnings tied to `astral-sh/setup-uv@v3`.
- **Root cause category:** action runtime deprecation.
- **Exact fix:** upgrade/pin the affected action to a Node 24-compatible release and remove the warning from recurring orchestrator paths.
- **Expected reliability impact:** low-medium today, higher later if GitHub hard-disables the legacy runtime.
- **Rollback / fail-open:** low risk; simple version rollback if needed.

**Semble fallback status:** no production `SEMBLE_FALLBACK` lines were found in sampled deep-dive logs or evidence-grade run summaries, so this does **not** look like a masked broken rollout. Current fail-open behavior appears healthy.

## AI Memory Health

- Sampled deep-dive logs contained **32 structured `AI_MEMORY_TELEMETRY` JSON entries**:
  - **18** `record-run-event`
  - **6** `record-candidate`
  - **8** `retrieve`

- **Retrieve hit rate is 0%.** All **8/8** `retrieve` ops in the sampled `review_autofix` deep dives (**25854955044**, **25855086361**, **25855490686**, **25856755453**, **25856876392**, **25856885764**, **25858191744**, **25858201334**) had:
  - `records_selected=0`
  - `estimated_tokens=0`
  - `keyword_method=none`

- **Average estimated tokens vs budget:** average `estimated_tokens` was **0.0**. A retrieval budget field was **not emitted** in the sampled telemetry, so budget adherence cannot be quantified from this window.

- **Keyword extraction distribution:** `keyword_method` was **`none` in 8/8 retrieves**; no sampled retrieve used `llm` or `plain`.

- **Fail-open / disabled flags:** no sampled retrieve had `fail_open: true` or `enabled: false`.

- **Write-path health:** mostly healthy. Validate run **25863792212** emitted `validation_started`, later a `record-candidate`, and then `validation_completed`. The only anomaly was `push_attempts=2` on `validation_completed`, which is worth tracking but not yet a systemic problem.

- **Missing ops:** no sampled deep-dive telemetry showed `processed-command-claim`, `processed-command-complete`, `finalize-task`, `promote`, or `compact`.

**Recommendation:** add an alert on **retrieve hit rate <5%** and either fix keyword extraction or skip retrieval entirely when it would be `keyword_method=none`.

## GH API Call Audit

**What already looks good**
- No actual runtime **HTTP 429** or **secondary rate-limit** events were observed in sampled deep-dive logs.
- The repo already follows good API hygiene in the poller:
  - `scripts/orchestrate_poll_process.sh` caches `/actions/runs` with **TTL + ETag**
  - the same script batches issue-label lookups in **GraphQL batches of 25**
  These are the right patterns; the poller is not the first thing I would optimize.

### 1. Repeated linked-issue GraphQL lookups in `review_autofix`
- **Evidence:** recent runs **25898131499** and **25898082447** both logged `gh api graphql` `closingIssuesReferences` work even though they finished in **10-20s** and found no linked issues. The workflow file still contains multiple `closingIssuesReferences` call sites across post-merge dispatch, deterministic skip, early PR metadata collection, and late cache refresh.
- **Missed reuse opportunity:** linked-issue resolution is still duplicated across jobs/phases.
- **Exact change:** resolve linked issues **once per workflow run**, persist the JSON as a job output/artifact, and honor cached `[]` across later jobs instead of re-querying.
- **Estimated call reduction:** **2-3 GraphQL calls per merged-PR review run**.
- **Rate-limit risk reduction:** modest but real; also simplifies logic.

### 2. Artifact listing during Copilot review cleanup
- **Evidence:** runs **25898628308**, **25898117080**, **25898066328**, and **25897680887** all logged artifact listing via `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<run>/artifacts`; **25898117080** explicitly called cleanup the runtime hotspot.
- **Missed reuse opportunity:** artifact IDs are already known earlier in the flow, but cleanup still does an extra list call.
- **Exact change:** carry the artifact ID/name from the download/upload step into cleanup, or skip cleanup when retention policy is acceptable.
- **Estimated call reduction:** about **1 REST call per Copilot review run**.
- **Rate-limit risk reduction:** low, but this should shave some cleanup latency too.

### 3. `claude/**` push path still burns API calls before deciding to review
- **Evidence:** `internal-review.yml` always resolves whether an open PR exists for a `claude/**` push before dispatching the no-PR review path; recent no-PR review runs **25898098684**, **25898052209**, and **25897663527** show that this path is live.
- **Missed reuse opportunity:** if the no-PR path is reduced or removed, the preflight lookup and its downstream review-side API work go away too.
- **Exact change:** best fix is to shrink the no-PR path itself; second-best is to debounce by head SHA/branch quiet period before dispatch.
- **Estimated call reduction:** **1 REST lookup per `claude/**` push**, plus downstream review-side savings.

## Prompt Cache & Memory System

- **Prompt cache is configured on, but not measurable from this sample.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` was visible in **25898224963** (`orchestrate`) and **25898131499 / 25898082447** (`review_autofix`), but sampled deep-dive logs did not emit cache read/write token counters. That means I cannot quantify hit rate or dollar savings yet.

- **Semble looks healthy and targeted.** Across sampled deep dives:
  - **10** `SEMBLE_QUERY` lines
  - **89,401** total logged bytes
  - **536ms** average latency
  - targets: **`reviewer-context` 7**, **`overflow` 2**, **`validate-diagnose-context` 1**
  - **0** `SEMBLE_FALLBACK`
  
  **Inference:** this looks more like targeted context retrieval than noisy prompt bloat. I would keep it enabled.

- **Overflow usage is present but not alarming.** The only sampled overflow lookups were in **25856876392** and **25856885764**, both for `tests/test_implement_post_codex_recovery.py`. That is too small and too specific to call noisy today.

- **Memory retrieval is the weak link.** The cache/memory system is paying retrieval overhead but returning no records. Until hit rate improves, the memory side is not offsetting prompt volume.

**Concrete improvements**
1. Enable the existing reviewer cache probe on a sampled canary path.
2. Stabilize prompt prefixes by placing volatile metadata/comments/status late in the assembled prompt (inference).
3. Gate memory retrieval on non-empty keywords.
4. Keep Semble on; add alerts only when overflow frequency or bytes spike.

**Expected impact**
- **Tokens:** unquantified until cache metrics are emitted.
- **Latency:** small direct win from skipping dead memory retrieves; potentially larger if prompt-cache hit rate turns out to be low and fixable.
- **Reliability:** improved observability and less chance of making a blind model-tier change.

## Orchestrator Health

- **Core orchestration looks functionally healthy in this window.**
  - `orchestrate`: **2/2 success**, avg **588.5s**
  - `orchestrate_poll`: **31/31 success**, avg **120.7s**
  - `validation_refresh`: **1/1 success**, **234s**
  - `nightly_validation_selftest`: **1/1 success**, **121s**

- **Clarification flow appears to be working.** Clarify run **25898379788** succeeded in **79s** and recorded phase completion as `Clarification completed: auto_answered_by_orchestrator`.

- **The validate fix-loop is healthy.** Run **25863792212** diagnosed the issue, created a consolidated fix-up issue, and emitted memory telemetry; this is the right kind of fail-closed behavior for content defects.

- **Main operational pain points are control-plane churn and queueing, not state-machine breakage.**
  - **425** skipped runs came from `clarify` / `plan` / `implement` / `orchestrate_clarify_respond`
  - the repo-level top-line **p50 = 1s** is therefore misleading
  - many short workflows still logged hosted-runner wait messages

- **Observable indicators to track**
  1. `review_codex-agent_system` or equivalent system-step overhead **>600s**
  2. count of `AUTOFIX_GATE_NO_PR_FALLBACK` runs per day
  3. active-run p95 duration (not overall p95)
  4. `orchestrate_poll` runtime as a fraction of its **300s** cron interval
  5. memory retrieve hit rate
  6. `validate needs_fixes` count vs true infra-failure count

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Bottleneck type | Best next fix |
|---|---|---|---|---|
| Clarify | Startup/support checkout overhead on actual runs; lots of skipped wrappers overall | `clarify` has **122** runs but **113** `other`; successful run **25898379788** spent ~**31s** in support-source checkout | queueing + startup | Reuse workspace support files on self-repo runs; keep skipped-wrapper metrics separate |
| Plan | Real runs are much slower than top-line family stats imply | `plan` has **110** runs but **101** skipped; successful runs include **25898402748 (282s)** and **25898402333 (377s)** | startup + cleanup | Same support-source reuse; track active-run SLOs |
| Implement | Actual runs are long and runner-waited | `implement` successful runs include **25898528281 (307s)** and **25854672048 (698s)** | queueing + compute | Reduce overall runner contention first |
| Review / Autofix | Biggest end-to-end bottleneck by far | family avg **636.3s**, p95 **2231.8s**; outliers **2463-4101s** | compute + queueing | Shrink no-PR branch review path first |
| Validate | Mostly healthy, but “needs_fixes” is counted as failure | **25863792212** = `Validation needs fixes`; **25869406356** succeeded in **209s** | classification + compute | Separate product validation misses from infra failures |
| Orchestrate / Poll | Background load is steady; poller runtime is non-trivial | `orchestrate_poll` avg **120.7s** on a **5-minute cron**; recent runs **111-198s** | background compute + queueing | Watch duty cycle; avoid adding more background work until review latency is down |
| CI | Merge-gate long pole outside orchestrator | `ci` avg **678.2s**, recent **718-827s** all dominated by one serial job | compute | Parallelize CI |

**Notable non-bottlenecks in the sample**
- Retry overhead was minimal in sampled production runs.
- Repeated merge/conflict-heal retries did **not** show up as a dominant cost in sampled deep dives.
- Semble fallbacks were **not** a source of instability.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` tail latency and queueing
- serialized `ci` job
- background runner churn / misleading skipped-run volume

**Top failure modes**
- transient upstream download failure in `CI / Install actionlint` (**25897677337**)
- repo-content validation mismatch in `Internal: AI Validate` (**25863792212**), not infrastructure

**Highest-cost drivers**
- no-PR `claude/**` reviewer panel runs
- full reviewer-context expansion across multiple reviewers
- prompt-cache effectiveness currently unmeasured

**Top 3 prioritized actions**
1. **Shrink the no-PR `claude/**` review path** to 1-2 fast reviewers and no consensus summarizer unless needed.
2. **Split CI into parallel shards** and add retry/cache to `Install actionlint`.
3. **Turn on prompt-cache measurement and fix/gate zero-hit memory retrieval** before changing broader model tiers.

## Metrics Appendix

### Repo-level run metrics

| Scope | Total runs | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 619 | 187 | 2 | 4 | 426 | 124.4 | 1.0 | 766.0 |
| Active runs only (derived from `summary.json`, excluding `other`) | 193 | 187 | 2 | 4 | 0 | 395.8 | 116.0 | 1730.6 |

### Workflow family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Other | Failure rate | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 12 | 11 | 1 | 0 | 0 | 8.3% | 678.2 | 732.5 | 800.0 |
| review_autofix | 76 | 71 | 0 | 4 | 1 | 0.0% | 636.3 | 38.5 | 2231.8 |
| orchestrate | 2 | 2 | 0 | 0 | 0 | 0.0% | 588.5 | 588.5 | 788.8 |
| validate | 2 | 1 | 1 | 0 | 0 | 50.0% | 334.0 | 334.0 | 446.5 |
| copilot_pull_request_reviewer | 16 | 16 | 0 | 0 | 0 | 0.0% | 240.6 | 218.0 | 457.0 |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 0.0% | 234.0 | 234.0 | 234.0 |
| nightly_validation_selftest | 1 | 1 | 0 | 0 | 0 | 0.0% | 121.0 | 121.0 | 121.0 |
| orchestrate_poll | 31 | 31 | 0 | 0 | 0 | 0.0% | 120.7 | 116.0 | 187.0 |
| issue_pr_status | 12 | 12 | 0 | 0 | 0 | 0.0% | 49.1 | 63.5 | 71.8 |
| implement | 109 | 8 | 0 | 0 | 101 | 0.0% | 42.5 | 1.0 | 303.4 |
| plan | 110 | 9 | 0 | 0 | 101 | 0.0% | 38.9 | 1.0 | 386.3 |
| promote_main_to_stable | 1 | 1 | 0 | 0 | 0 | 0.0% | 22.0 | 22.0 | 22.0 |
| forward_merge_stable_to_main | 2 | 2 | 0 | 0 | 0 | 0.0% | 18.0 | 18.0 | 18.9 |
| cancel_on_pr_close | 12 | 12 | 0 | 0 | 0 | 0.0% | 9.6 | 8.0 | 14.4 |
| clarify | 122 | 9 | 0 | 0 | 113 | 0.0% | 7.5 | 1.0 | 80.9 |
| orchestrate_clarify_respond | 110 | 0 | 0 | 0 | 110 | 0.0% | 1.3 | 1.0 | 2.0 |

### Long-tail latency evidence

| Run ID | Family | Total s | Dominant step | Dominant step s | System/queue s | Note |
|---|---|---:|---|---:|---:|---|
| 25856876392 | review_autofix | 4101 | `review_codex-agent` | 2048.5 | 2003.3 | compute and scheduling both very large |
| 25855086361 | review_autofix | 2651 | `review_codex-agent` | 2639.3 | 0.0 | compute dominated |
| 25854955044 | review_autofix | 2463 | `review_codex-agent` | 2438.3 | 0.3 | compute dominated |
| 25858191744 | review_autofix | 2336 | `review_codex-agent_system` | 2143.6 | 2143.6 | scheduling dominated |

### Semble metrics

| Target | Queries | Total bytes | Avg ms | Example run |
|---|---:|---:|---:|---|
| reviewer-context | 7 | 75,776 | 520.0 | 25856885764 |
| overflow | 2 | 9,267 | 542.0 | 25856885764 |
| validate-diagnose-context | 1 | 4,358 | 640.0 | 25863792212 |

| Aggregate Semble metric | Value |
|---|---:|
| Total `SEMBLE_QUERY` lines | 10 |
| Total logged bytes | 89,401 |
| Average latency | 536.4 ms |
| `SEMBLE_FALLBACK` lines in sampled production deep dives | 0 |

### AI memory telemetry metrics

| Metric | Value |
|---|---:|
| Structured telemetry entries | 32 |
| `record-run-event` ops | 18 |
| `record-candidate` ops | 6 |
| `retrieve` ops | 8 |
| Retrieve hit rate | 0/8 |
| Avg retrieve `estimated_tokens` | 0.0 |
| Retrieve `keyword_method=none` | 8 |
| Retrieve `fail_open=true` | 0 |
| Retrieve `enabled=false` | 0 |
| Telemetry entries with `push_attempts>1` | 1 |

### Token and prompt-cache metrics

| Metric | Value | Notes |
|---|---|---|
| Prompt tokens | Not emitted in sampled deep-dive logs | No runtime usage lines were present |
| Completion tokens | Not emitted in sampled deep-dive logs | Same |
| Total tokens | Not emitted in sampled deep-dive logs | Same |
| Cache creation input tokens | Not emitted in sampled deep-dive logs | Cache probe not observed |
| Cache read input tokens | Not emitted in sampled deep-dive logs | Cache probe not observed |
| Prompt-cache hit rate | Not measurable from this window | `OPENROUTER_PROMPT_CACHE_DISABLED=false` was observed, but usage counters were absent |

### GH API call summary

| Pattern | Sampled runtime evidence | Estimated reducible calls | Notes |
|---|---|---:|---|
| `review_autofix` linked-issue `closingIssuesReferences` GraphQL | Runs 25898131499, 25898082447 | 2-3/run | Multiple call sites remain across jobs/phases |
| Copilot reviewer artifact list/cleanup | Runs 25898628308, 25898117080, 25898066328, 25897680887 | ~1/run | Cleanup/listing shows up as a hotspot |
| Actual 429 / secondary-rate-limit events | None observed | — | Poller already uses ETag/TTL and batching |
| `claude/**` open-PR existence lookup | Live code path; no-PR review runs 25898098684, 25898052209, 25897663527 show path is active | ~1 per push | Best removed by shrinking the no-PR review path itself |
