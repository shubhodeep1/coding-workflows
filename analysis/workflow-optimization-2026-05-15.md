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

## Deep Audit — Workflows & Scripts (2026-05-15)

### Section 1: Bug & Correctness Sweep

Audit sweep notes: all `.github/workflows/*.yml` parsed cleanly with YAML loading, all `scripts/*.sh` passed `bash -n`, and all `scripts/*.py` passed `py_compile`.

- **BUG-001** — `.github/workflows/issue_pr_status.yml:501-518`
  - **Severity:** Low
  - **Category:** `bug`
  - **Description:** The merged-alert step recomputes orchestrator ownership by looping over `LINKED_ISSUE_NUMBERS` and fetching each issue body with `_safe_gh_jq`, then treating an empty/missing body as “not orchestrated.” Earlier in the same workflow, the classification step already builds `TRACKING_ISSUES` and `MANAGED_ISSUES` from batched issue metadata. On any later API miss, an orchestrator-managed issue can therefore fall through to the non-orchestrator Telegram path and emit a duplicate merge alert.
  - **Recommended fix:** Export `MANAGED_ISSUES` or a single `IS_ORCHESTRATED=true` flag into `$GITHUB_ENV` during the earlier classification step and reuse it here. If a re-check is still required, do one batched issue-details lookup instead of per-issue body scans.

- **BUG-002** — `scripts/orchestrate_poll_process.sh:3873-3983`
  - **Severity:** High
  - **Category:** `bug`
  - **Description:** The final-merge path reads the same PR from `repos/${GITHUB_REPOSITORY}/pulls/${final_pr}` eight times in one control path: twice at `3875-3876`, three times at `3930-3932`, and three more times at `3981-3983`. Because `state`, `mergeable`, and `merged_at` are coming from separate API snapshots, the script can make merge/heal/retry decisions on a mixed view of the PR if it changes between calls.
  - **Recommended fix:** Fetch the full PR JSON once per decision point, parse `state`, `mergeable`, and `merged_at` locally, and reuse that single snapshot through the branch logic. This should follow the same cache/reuse style already used elsewhere in the poller.

### Section 2: GitHub API Call Redundancy Audit

- **API-001** — `.github/workflows/review_autofix.yml:1590-1619`
  - **Severity:** Medium
  - **Category:** `api-batching`
  - **Description:** When `closingIssuesReferences` is empty, the fallback parses issue numbers from the PR body and then hydrates each issue with `gh api "repos/${{ github.repository }}/issues/${_fb_num}"` inside a loop. The repo already has an aliased GraphQL batching pattern for issue details, so this is an avoidable N-per-item REST path.
  - **Current call count:** Up to **20** REST calls per PR (`_FALLBACK_MAX_ISSUES=20`).
  - **Proposed call count:** **1** GraphQL call for up to 20 issues (or `ceil(N/25)` if kept at the poller’s batch size).
  - **Existing pattern to extend:** `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.
  - **Recommended fix:** Move a lightweight issue-details GraphQL helper into `scripts/gh_helpers.sh` (or another shared script) and have this fallback pass the parsed issue-number list into that helper instead of hydrating each issue individually.

- **API-002** — `.github/workflows/review_autofix.yml:533-565`
  - **Severity:** Medium
  - **Category:** `api-batching`
  - **Description:** In the standalone-validate dispatch loop, fallback-linked issues with `labels_known != 'true'` trigger `gh issue view ... --json labels` once per issue before the workflow decides whether to dispatch validate and remove `ai:orchestrator-validate-required`. That is another per-item REST lookup on data that can be batched.
  - **Current call count:** **N** REST calls for **N** fallback-linked issues.
  - **Proposed call count:** **1** batched GraphQL call for up to 25 issues (or `ceil(N/25)`).
  - **Existing pattern to extend:** `_fetch_candidate_issue_details_graphql` / the batched label-fetch pattern in `scripts/orchestrate_poll_process.sh`.
  - **Recommended fix:** After constructing fallback `issue_nodes_json`, batch-fetch labels once and enrich the local JSON before entering the loop so the loop can stay API-free except for the actual dispatch/edit operations.

- **API-003** — `.github/workflows/test-and-mark-stable.yml:1728-1750`
  - **Severity:** Medium
  - **Category:** `api-redundancy`
  - **Description:** `gh_api_with_retry` retries every `gh api` failure three times, but it does not classify permanent failures (`404`, `422`, token-scope errors) that `scripts/gh_helpers.sh` already treats as non-retryable. That wastes two extra API calls on deterministic failures inside the release gate.
  - **Current call count:** Up to **3** attempts for a permanent failure.
  - **Proposed call count:** **1** attempt for permanent failures; retries only for transient failures.
  - **Existing pattern to extend:** `gh_retry` plus `_is_gh_permanent_failure` in `scripts/gh_helpers.sh:56-75`.
  - **Recommended fix:** Source `scripts/gh_helpers.sh` in this step and switch to `gh_retry gh api` / `gh_retry_to_file`, or port the permanent-failure branch directly if the step must remain self-contained.

Additional API-impact cross-references:
- **BUG-002** — `scripts/orchestrate_poll_process.sh:3873-3983` — current `GET /pulls/{final_pr}` count is **8** on the hot finalize path; reusing one PR snapshot per decision point reduces that to **3**.
- **BUG-001** — `.github/workflows/issue_pr_status.yml:501-518` — current merged-alert path makes **N** extra `GET /issues/{n}` calls; exporting the earlier managed/tracking classification reduces that to **0**.

### Section 3: Code Duplication & Modularization Opportunities

I did not re-raise the repeated support-source checkout pattern here because the current report already covers it under the existing “Skip duplicate workflow-support checkouts on self-repo runs” optimization.

- **DUP-001** — `scripts/review_run_reviewers.sh:738-746; scripts/review_conflict_prepare.sh:448-456; scripts/review_apply_fixes.sh:291-299`
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** `append_semble_query_section()` is duplicated byte-for-byte in three review-side scripts. Any future change to truncation, newline handling, or empty-file behavior now requires three synchronized edits.
  - **Recommended fix:** Move it into `scripts/semble_helpers.sh` as `append_semble_query_section <label> <path> [max_bytes]`, then source that helper from all three callers.

- **CONSIST-001** — `scripts/label_helpers.sh:110-143; scripts/orchestrate_poll_process.sh:1365-1419`
  - **Severity:** Medium
  - **Category:** `consistency`
  - **Description:** The repo has two independent `ensure_label_exists()` implementations. The shared helper accepts an explicit repo, uses the full label catalog, and returns non-zero on create failure; the poller copy caches hits, has partial hardcoded fallbacks, and returns success even after a failed create. That is behavioral drift on a core label-management primitive.
  - **Recommended fix:** Consolidate on one helper in `scripts/label_helpers.sh`, then add an optional cache-aware wrapper there (for example `ensure_label_exists_cached <label> [repo]`) so the poller keeps its per-process memoization without redefining semantics.

- **DUP-002** — `.github/workflows/test-and-mark-stable.yml:455-563, 580-734, 766-921, 1203-1586, 1673-2077`
  - **Severity:** Medium
  - **Category:** `duplication`
  - **Description:** The release gate open-codes five large wait/verify shells with the same primitives: local `gh api` wrappers, inactivity timers, run-id capture, state-diff tracking, and GitHub run polling. This duplication is already drifting: later loops fetch `{status, conclusion}` once, while the cancel-on-close waiter still fetches them separately.
  - **Recommended fix:** Extract a shared helper such as `scripts/e2e_wait_phase.sh` (or Python equivalent) with a signature like `e2e_wait_phase --phase <clarify|plan|implement|review> --repo <owner/repo> --issue <n> --created-after <ts> [--pr <n>] [--head-sha <sha>]`, then keep workflow YAML limited to phase-specific inputs and success conditions.

### Section 4: Expression Size Limit Risk Assessment

Approximate counts below are from the checked-in `run:` scalar bodies that contain `${{ }}`. GitHub’s exact template-accounting is opaque, so these should be treated as conservative static estimates.

- **EXPR-001** — `.github/workflows/review_autofix.yml:1418-1807`
  - **Severity:** High
  - **Category:** `expression-limit`
  - **Description:** Approximate `run:` scalar size is **21,048** characters, leaving **-48** characters of headroom against the 21,000-character threshold under the repo’s prior failure mode. [NEEDS VERIFICATION] This “Collect PR metadata” block inlines a custom retry wrapper, no-PR synthetic metadata handling, diff capture, linked-issue fallback, and multiple file exports.
  - **Recommended fix:** Extract the whole step to `scripts/review_collect_pr_metadata.sh` and pass inputs via env/files.

- **EXPR-002** — `.github/workflows/test-and-mark-stable.yml:1203-1587`
  - **Severity:** High
  - **Category:** `expression-limit`
  - **Description:** Approximate `run:` scalar size is **23,499** characters, leaving **-2,499** characters of headroom. [NEEDS VERIFICATION] The Phase 4 wait-review poller is now materially past the historical failure threshold.
  - **Recommended fix:** Extract to `scripts/e2e_wait_review.sh`, or split into separate “discover run,” “poll run state,” and “inspect failed steps/logs” steps.

- **EXPR-003** — `.github/workflows/test-and-mark-stable.yml:1673-2078`
  - **Severity:** High
  - **Category:** `expression-limit`
  - **Description:** Approximate `run:` scalar size is **21,288** characters, leaving **-288** characters of headroom. [NEEDS VERIFICATION] The bait-verification step mixes pytest bootstrapping, GH API retry logic, canary fetch/parse, and retry-on-editor-miss handling in one inline block.
  - **Recommended fix:** Move the verifier to `scripts/e2e_verify_editor_bait.sh` and keep the workflow step to env setup plus one script invocation.

- **EXPR-004** — `.github/workflows/validate.yml:204-577`
  - **Severity:** High
  - **Category:** `expression-limit`
  - **Description:** Approximate `run:` scalar size is **20,816** characters, leaving only **184** characters of headroom. [NEEDS VERIFICATION] The “Fetch workflow support files” step is effectively at the cliff already.
  - **Recommended fix:** Extract the bootstrap to `scripts/fetch_workflow_support.sh` or split it into smaller ref-resolution / required-assets / optional-assets steps.

- **EXPR-005** — `.github/workflows/review_autofix.yml:893-1126`
  - **Severity:** Medium
  - **Category:** `expression-limit`
  - **Description:** Approximate `run:` scalar size is **15,230** characters, leaving **5,770** characters of headroom. [NEEDS VERIFICATION] The Stage workflow support files block already carries a large manifest and optional-copy logic, so normal feature growth can push it into the danger zone.
  - **Recommended fix:** Move the manifest/copy logic into a support bootstrap script and keep YAML as a short wrapper.

- **EXPR-006** — `.github/workflows/orchestrate_clarify_respond.yml:842-1125`
  - **Severity:** Medium
  - **Category:** `expression-limit`
  - **Description:** Approximate `run:` scalar size is **15,140** characters, leaving **5,860** characters of headroom. [NEEDS VERIFICATION] The auto-answer post-processing step combines claim/dedupe logic, comment posting, ledger writes, and env exports in one inline shell body.
  - **Recommended fix:** Extract to `scripts/orchestrate_clarify_respond_post_answer.sh` or split the step into claim/dedupe, post-comment, and ledger/update phases.

No workflow exceeded the 800 KB file-size warning threshold; the largest audited workflow was `.github/workflows/review_autofix.yml` at **323,040** characters.

### Section 5: Cross-Cutting Concerns

No `TODO` / `FIXME` / `HACK` markers were present in `.github/workflows/*.yml` or `scripts/*.{sh,py}`.

- **DEAD-001** — `scripts/orchestrate_poll_process.sh:10317-10347, 10758-10814`
  - **Severity:** Low
  - **Category:** `dead-code`
  - **Description:** `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are assigned multiple times but never read elsewhere in the script. They currently add state churn without affecting comments, persisted state, or control flow.
  - **Recommended fix:** Remove both variables and their assignments, or wire them into persisted state / telemetry if their values are still meant to matter.

- **DEAD-002** — `scripts/review_issue_ledger.sh:866-917`
  - **Severity:** Low
  - **Category:** `dead-code`
  - **Description:** `CURRENT_FLOOR` is declared and populated for each issue ID, but the associative array is never read later in the file. The floor-category bookkeeping is therefore dead state.
  - **Recommended fix:** Delete `CURRENT_FLOOR`, or consume it in collision resolution / final summary logic if floor data is intended to influence ledger persistence.

- **SHELL-001** — `scripts/validate_changed_files_syntax.sh:70-74`
  - **Severity:** Low
  - **Category:** `shellcheck`
  - **Description:** The denylist matches on the composite string `"${file},${basename_lc}"`, but the broad `*.env*|*.pem*|...` arm already shadows the later `*,*.envrc|*,.env*` patterns. ShellCheck flags this as SC2221/SC2222, and the basename-specific cases are unreachable.
  - **Recommended fix:** Split full-path and basename checks into separate `case` statements, or rewrite the existing arm so each pattern class is evaluated exactly once in a documented order.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 5 | BUG-002, EXPR-001, EXPR-002, EXPR-003, EXPR-004 |
| Medium | 7 | API-001, API-002, API-003, CONSIST-001, DUP-002, EXPR-005, EXPR-006 |
| Low | 5 | BUG-001, DUP-001, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1 | Medium |
| API call optimization | 5 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-15)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation can be implemented directly because the calls are in the same function/step, hit the same data source, and can preserve current failure behavior. `NEEDS_VERIFICATION` means the overlap is real but cross-step contracts, caller assumptions, or tested edge cases must be checked first. `RISKY_SKIP` means the redundancy is visible, but the call sits in a poller/race-defense path or other manual-review-only area and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **MERGE-001**
  - **Safety tag:** `NEEDS_VERIFICATION`
  - **Files:** `scripts/review_rb_judge.sh:235-245`, `scripts/review_rb_judge.sh:257-272`
  - **Current call count:** `1` GraphQL call + `1..N` REST issue GETs on the linked-issues path.
  - **Proposed call count:** `1` GraphQL call.
  - **Endpoint(s):** GraphQL `repository.pullRequest.closingIssuesReferences`; REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence:**
    ```sh
    ISSUE_NUMBERS="$(gh_retry gh api graphql ... closingIssuesReferences(first: 50) { nodes { number } } ...)"

    while IFS= read -r issue_number; do
      ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
      BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
      if [ -z "${FIRST_ISSUE}" ]; then
        FIRST_ISSUE="${issue_number}"
        FIRST_ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_META_JSON}" | jq -c '[(.labels // [])[]?.name]' 2>/dev/null || echo '[]')"
      fi
      if [ -z "${FIRST_ISSUE_BODY}" ]; then
        FIRST_ISSUE_BODY="${BODY}"
      fi
      [ -n "${FIRST_ISSUE}" ] && [ -n "${FIRST_ISSUE_BODY}" ] && break
    done <<< "${ISSUE_NUMBERS}"
    ```
  - **Proposed fix:** Extend the existing GraphQL query in `scripts/review_rb_judge.sh` to fetch `body` and `labels(first: 100) { nodes { name } }`, then shape the result like the existing batched helper pattern in `scripts/orchestrate_poll_process.sh` (`_fetch_candidate_issue_details_graphql`) so the loop reads local JSON instead of calling `_safe_gh_jq` per issue.
  - **Safety rationale:** This is not `SAFE_TO_MERGE` because the current loop has tested semantics: `FIRST_ISSUE_LABELS_JSON` must stay pinned to the first linked issue even when `FIRST_ISSUE_BODY` falls back to a later issue.
  - **Downstream signal:** Verify against `tests/test_review_rb_judge_label_propagation.py:90-107,561-595` that first-issue label pinning and later-issue-body fallback remain identical, and confirm whether `labels(first: 100)` is sufficient before deleting the REST loop.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001**
  - **Safety tag:** `SAFE_TO_MERGE`
  - **Files:** `scripts/review_rb_judge.sh:210-227`, `scripts/review_rb_judge.sh:242-245`
  - **Current call count:** `2` `GET /repos/{owner}/{repo}/pulls/{pull_number}` calls on the `ISSUE_NUMBERS == ""` path.
  - **Proposed call count:** `1` on that path.
  - **Endpoint(s):** REST `GET /repos/{owner}/{repo}/pulls/{pull_number}`
  - **Evidence:**
    ```sh
    _pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
    ...
    if [ -z "${ISSUE_NUMBERS}" ]; then
      PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    fi
    ```
  - **Proposed fix:** Keep `_pr_meta` (or a derived `_pr_title_body`) alive past line 227 and build `PR_DATA` from that cached PR snapshot; retain the existing late `_safe_gh_jq` call only when `_pr_meta` is `{}`/invalid.
  - **Safety rationale:** Same script scope, same endpoint/auth, no intervening mutation can change PR title/body between the two reads, and the existing late live fetch can remain as the error-path fallback.
  - **Downstream signal:** Reuse the initial `_pr_meta` snapshot for `PR_DATA`, and keep the current live `_safe_gh_jq` only as an invalid-snapshot fallback.

- **REUSE-002**
  - **Safety tag:** `NEEDS_VERIFICATION`
  - **Files:** `.github/workflows/orchestrate_clarify_respond.yml:62-84`, `.github/workflows/orchestrate_clarify_respond.yml:405-420`, `.github/workflows/orchestrate_clarify_respond.yml:989-990`
  - **Current call count:** `2` child-issue GETs per orchestrator-managed run, plus up to `2` tracking-issue GETs when `TRACKING_NUM` is present.
  - **Proposed call count:** `1` child-issue GET, plus up to `1` tracking-issue GET.
  - **Endpoint(s):** REST `GET /repos/{owner}/{repo}/issues/{issue_number}`
  - **Evidence:**
    ```sh
    ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
    ...
    TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' ...)"

    ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
    ...
    TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
    ```
    The first in-job issue mutation does not happen until `.github/workflows/orchestrate_clarify_respond.yml:989-990`.
  - **Proposed fix:** In `Check orchestrator metadata`, persist the full child-issue JSON and (when present) full tracking-issue JSON to a temp file or `$GITHUB_ENV`, then let `Prepare prompt context` reuse those snapshots for both title/body reads.
  - **Safety rationale:** This crosses workflow steps, so freshness assumptions must be verified even though the workflow itself does not mutate the issue before prompt assembly.
  - **Downstream signal:** Verify that no expected actor edits the child or tracking issue between `Check orchestrator metadata` and `Prepare prompt context` in a way the later prompt must see; if not, cache and reuse the earlier JSON.

- **REUSE-003**
  - **Safety tag:** `NEEDS_VERIFICATION`
  - **Files:** `.github/workflows/review_autofix.yml:1503-1517`, `.github/workflows/review_autofix.yml:4368-4425`, `.github/workflows/review_autofix.yml:4482-4546`, `.github/workflows/review_autofix.yml:5258-5300`
  - **Current call count:** `1` conditional late PR GET at each of the three sites; on the normal PR-backed path it is effectively dead because `PR_META_FILE` is already populated from the earlier `/pulls/{n}` fetch at `1503-1517`.
  - **Proposed call count:** `0` late PR GETs at those three sites.
  - **Endpoint(s):** REST `GET /repos/{owner}/{repo}/pulls/{pull_number}`
  - **Evidence:**
    ```sh
    gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
    ...
    jq '{ title: (.title // ""), body: (.body // "") ... }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
    ```
    ```sh
    PR_DATA="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" 2>/dev/null || echo "")"
    if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
      PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' ...)"
    fi
    ```
    The three consumers are all gated away from synthetic no-PR mode via `env.CLAUDE_BRANCH_REVIEW_MODE != 'true'` at `.github/workflows/review_autofix.yml:4368`, `.github/workflows/review_autofix.yml:4483`, and `.github/workflows/review_autofix.yml:5259`.
  - **Proposed fix:** In the three late linked-issue fallback steps, trust `PR_META_FILE` as the PR-backed title/body source and remove the extra live `/pulls/{n}` fetch; if a guard is still wanted, make it a local file-validity check rather than a second API read.
  - **Safety rationale:** Not `SAFE_TO_MERGE` because the reuse depends on an earlier workflow step’s cache contract, not a same-step call, and a checked-in contract test currently expects the live fallback fetch to exist.
  - **Downstream signal:** Verify `PR_META_FILE` is always present and non-empty on every PR-backed path reaching these three steps, then update `tests/test_review_autofix_phase_transition_contract.py:41-57` before removing the late `/pulls/{n}` fetches.

### Dead Calls (DEAD-API-###)

- **DEAD-API-001**
  - **Safety tag:** `RISKY_SKIP`
  - **Files:** `scripts/orchestrate_poll_process.sh:12157-12167`, `scripts/orchestrate_poll_process.sh:12169-12220`
  - **Current call count:** `1` `GET /repos/{owner}/{repo}` per standalone PR conflict sweep.
  - **Proposed call count:** `0`
  - **Endpoint(s):** REST `GET /repos/{owner}/{repo}`
  - **Evidence:**
    ```sh
    STANDALONE_PRS="$(gh_retry gh pr list ...)"
    ...
    DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"

    for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
      S_PR=...
      S_HEAD=...
      S_BASE=...
    ```
    In this block, the loop consumes `S_PR`, `S_HEAD`, and `S_BASE`; the fetched `DEFAULT_BRANCH` is not read afterward.
  - **Proposed fix:** Remove the `DEFAULT_BRANCH` fetch from the standalone PR conflict-sweep block after manual confirmation that no later code in that block consumes it.
  - **Safety rationale:** `RISKY_SKIP` is mandatory here because the dead call lives inside `scripts/orchestrate_poll_process.sh`, which this audit treats as manual-review-only even for apparently local simplifications.
  - **Downstream signal:** Do not auto-implement; manually inspect the standalone conflict-sweep path and add/refresh an integration test before deleting the fetch.

### Cross-References to Deep Audit Section

- API-001: `NEEDS_VERIFICATION` — the batching direction is right, but the fallback body-parser path and 20-issue cap need parity checks before replacing the per-issue REST hydration.
- API-002: `NEEDS_VERIFICATION` — batching label enrichment should reduce calls, but the validate-dispatch/remove-label sequencing must be revalidated.
- API-003: `NEEDS_VERIFICATION` — adopting `gh_retry`/permanent-failure classification is sensible, but it changes deterministic 4xx retry behavior inside the release gate.
- BUG-001: `NEEDS_VERIFICATION` — reusing earlier managed/tracking classification would cut extra issue GETs, but merged-alert routing depends on exact orchestrator-detection semantics.
- BUG-002: `RISKY_SKIP` — the duplicate PR reads are real, but the hot final-merge path lives in `scripts/orchestrate_poll_process.sh` and explicitly defends against upstream races.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 1 | DEAD-API-001 |

### Implement-Stage Handoff

- REUSE-001
