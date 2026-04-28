## Executive Summary

- **The largest preventable latency and failure driver is the implement phase’s repeated unsupported MCP/tool calls.** In failed implement runs `25055428237` and `25057072163`, the `Run Codex implementation` step spent ~64 and ~68 minutes respectively before ending with `Codex implement failed after 5 attempts`, while repeatedly logging `unsupported call: activate_project` and once `unsupported call: git_status`. **Estimated impact:** save ~25–45 minutes on affected runs and recover most of the `implement` family’s 6.7% failure rate. **Confidence:** high.
- **Review/autofix reliability is being hurt by invalid GH API inputs and rate-limit-sensitive retry loops.** In run `25026882619`, the review path retried the same malformed GraphQL request 5 times (`Variable $number of type Int! was provided invalid value`) before failing; in `25045997555`, the no-PR review path also logged `head_ref_override and head_sha_override must be supplied by the caller` and emitted GitHub rate-limit handling. **Estimated impact:** cut 30–90 seconds of wasted retry time per bad run and materially reduce `review_autofix` failures/cancellations. **Confidence:** high.
- **Plan and review critical paths are dominated by oversized, repetitive prompt/context assembly rather than runner setup.** Slow plan run `25052390881` lasted 4,448s, and cancelled run `25068290028` lasted 7,243s; the sampled successful plan run shows a single Codex attempt running for ~71 minutes after large inlined workflow guidance and repeated Serena/GH API instructions were assembled. **Estimated impact:** 15–30% token reduction and 10–30 minutes saved on worst-case plan/review runs. **Confidence:** medium.
- **The orchestrator is producing high control-plane churn: many sibling workflows are dispatched only to skip immediately.** Repository-wide there were 506 `other` outcomes out of 843 runs; on April 28, many `clarify`/`plan`/`implement`/`orchestrate_clarify_respond` runs launched at the same timestamp and skipped in 1–2s. **Estimated impact:** lower queue pressure, cleaner state transitions, and fewer false negatives in polling. **Confidence:** high.
- **Queue starvation is causing avoidable poller failures.** `orchestrate_poll` failures `25058629488` and `25061570578` both failed at exactly 903s without running business logic; logs show only repeated “Waiting for a runner to pick up this job...”. **Estimated impact:** eliminate ~15-minute false failures and improve orchestration reliability without touching core logic. **Confidence:** high.
- **Memory telemetry is present and generally healthy, but reviewer memory retrieval is weak.** Across sampled logs there were 20 `retrieve` operations with a 70% hit rate and low average estimated payload (30.2 tokens), but all 6 zero-hit retrieves came from `review_autofix` runs. **Estimated impact:** modest cost savings and better reviewer consistency if reviewer retrieval is tuned. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fail fast on unsupported MCP tool calls in `implement` and immediately downgrade to fallback mode
- **Evidence:** Failed implement runs `25057072163` (4,053s) and `25055428237` (3,818s) repeatedly logged `ERROR codex_core::tools::router: error=unsupported call: activate_project`, then ended with `Codex implement failed after 5 attempts.` Run `25057072163` also logged `unsupported call: git_status`.
- **Root cause:** The prompt/tool contract expected Serena/Git MCP calls that were not available in the runtime router, but the workflow kept retrying full editor attempts instead of switching modes after the first capability miss.
- **Exact change:** Add a one-time capability probe before editor execution. If Serena/Git MCP capabilities are unavailable, set a session flag and re-render the prompt without MCP-specific instructions for the remaining attempts. Do not let the same unsupported call repeat across attempts.
- **Estimated time savings:** **25–45 minutes per affected implement run**; also prevents wasting all 5 attempts on a deterministic mismatch.
- **Implementation risk:** **Low.** This is a fail-open fallback, not a behavioral change to successful MCP-capable runs.
- **Type:** **Critical-path win**

### 2. Shrink plan prompts and lower reasoning on fully specified planning tasks
- **Evidence:** Slow plan run `25052390881` lasted 4,448s and the cancelled plan run `25068290028` lasted 7,243s. In `25052390881`, a single planning attempt started around `12:21:13Z` and succeeded only at `13:32:47Z`, after large inlined guidance blocks, repeated Serena instructions, repeated GH API hygiene guidance, and broad workflow-reference material were assembled.
- **Root cause:** The plan phase is over-contextualizing and appears to run high-reasoning planning even when the output is short and deterministic.
- **Exact change:**  
  1. Move stable workflow/operator guidance into a single reusable system prefix rather than inlining it multiple times.  
  2. Keep dynamic issue state/comments in a suffix.  
  3. Route obviously fully specified/no-ambiguity tasks to a cheaper reasoning setting than the current default.
- **Estimated time savings:** **10–30 minutes on worst-case plan runs**; likely smaller but still meaningful on successful long plans.
- **Implementation risk:** **Medium.** Requires prompt refactoring and careful regression checks on plan quality.
- **Type:** **Critical-path win**

### 3. Expand deterministic skip/gate logic so more review runs take the fast path
- **Evidence:** `review_autofix` has a wide runtime spread: p50 325s, p95 2,543.3s, with many successes between 1,100s and 4,429s. But recent run `25074079707` completed successfully in **16s**, proving a very fast path already exists.
- **Root cause:** The current gate catches some non-actionable review runs, but many low-value runs still enter the expensive reviewer/editor chain.
- **Exact change:** Reuse existing gate patterns more aggressively for doc-only, metadata-only, already-autofixed, or externally-advanced branches before launching reviewer models.
- **Estimated time savings:** **5–20 minutes on each newly gated trivial review run**.
- **Implementation risk:** **Low to medium.** Safe if fail-open and limited to deterministic skip categories.
- **Type:** **Critical-path win**

### 4. Stop treating poller queue starvation as work failure
- **Evidence:** `orchestrate_poll` failures `25058629488` and `25061570578` both consumed 903s and show only repeated queue wait messages with no poll logic execution.
- **Root cause:** A control-plane workflow is failing because it cannot acquire a runner inside its timeout budget.
- **Exact change:** For the poller, either increase tolerance for runner acquisition or mark queue-starved attempts as neutral/deferred and let the next tick continue. Also reduce unnecessary sibling workflow dispatches so pollers are less likely to compete with skipped jobs.
- **Estimated time savings:** **~15 minutes per queue-starved poll failure**, plus lower downstream rework.
- **Implementation risk:** **Low.**
- **Type:** **Critical-path win**

### 5. Parallelize read-only GH fetches in plan/review setup
- **Evidence:** In plan run `25052390881`, issue metadata, issue comments, linked PR count, and progress-comment lookups were executed sequentially. In review run `25027554119`, PR payload, issue comments, reviews, review comments, and linked-issue lookups were all fetched before model work.
- **Root cause:** Setup steps serialize independent read-only API operations.
- **Exact change:** Fetch independent read-only resources in parallel into temp files, then join before prompt rendering.
- **Estimated time savings:** **10–30 seconds per plan/review run**.
- **Implementation risk:** **Low.**
- **Type:** **Local micro-optimization**

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Remove duplicated prompt guidance blocks to improve prompt-cache reuse and reduce prompt tokens
- **Evidence:** In plan run `25052390881`, Serena/GH API guidance appears multiple times in the assembled prompt/log output; similar repeated blocks appear in `review_autofix` slow run `25027554119`. The workflow explicitly says it is inlining large amounts of content so “the model doesn't waste tool calls reading files.”
- **Root cause:** Stable instructions are being re-expanded per run and, in some cases, duplicated within a single prompt assembly.
- **Exact change:** Deduplicate stable policy text into one canonical prefix; append only run-specific context afterwards. Keep volatile fields like timestamps, run IDs, and progress markers out of the cacheable prefix.
- **Estimated savings:** **15–30% prompt-token reduction** on long plan/review runs; likely the highest token-saving lever in the current sample.
- **Quality-risk notes:** **Low** if the same content remains present once.

### 2. Reduce reasoning level for deterministic plan/review subpaths
- **Evidence:** The sampled plan run produced a short result after ~71 minutes of model time; logs also state defaults such as `THINKING_LEVEL_* = xhigh`. Review already demonstrates that a fast skip path exists (`25074079707`, 16s).
- **Root cause:** High reasoning is likely being spent on tasks with narrow solution spaces or where the workflow has already resolved ambiguity.
- **Exact change:** Add routing rules: fully specified plan tasks and trivial review gates should use `medium`/`low` reasoning; reserve highest reasoning for unresolved, multi-file, or conflict-heavy tasks.
- **Estimated savings:** **20–40% AI cost** on the subset of deterministic runs.
- **Quality-risk notes:** **Medium.** Must be constrained to cases with strong determinism signals.

### 3. Eliminate avoidable full-run reruns caused by deterministic workflow bugs
- **Evidence:** Sampled long failures include roughly **6.5 runner-hours** in `implement` failures and **4.5 runner-hours** in `review_autofix` failures on April 28 alone, before counting token spend.
- **Root cause:** Deterministic failures (unsupported MCP calls, malformed GraphQL, missing required overrides) trigger expensive model and orchestration retries.
- **Exact change:** Convert deterministic preflight failures into immediate fail-open/fail-fast branches before launching model-heavy steps.
- **Estimated savings:** **High compute-dollar savings**; token savings are likely substantial but **cannot be quantified from current logs** because prompt/completion totals were not emitted.
- **Quality-risk notes:** **Low.** This removes known-bad attempts rather than reducing successful work.

### 4. Collapse duplicate cache probes in review/autofix
- **Evidence:** Across sampled logs there were 12 OpenRouter cache-probe lines, always in pairs (`call=1`, `call=2`) for `review_autofix_cache_probe`, model `minimax/minimax-m2.5`. All token/cache fields were `na`.
- **Root cause:** The workflow appears to run two probe calls per sampled review run without yielding observable accounting value.
- **Exact change:** If the second probe is only a health check, keep a single probe per run or cache the probe result for the job.
- **Estimated savings:** **Small per run, but very low risk**.
- **Quality-risk notes:** **Low**, assuming one probe is enough to verify the path.

### 5. Reduce prompt variance from status/progress comment churn
- **Evidence:** Plan run `25052390881` looked up, updated, retried, deleted, and reposted progress comments during one execution.
- **Root cause:** Frequent state/comment mutations encourage dynamic prompt scaffolding and can fragment cache keys if those values leak into prompt headers.
- **Exact change:** Keep comment body transitions minimal and ensure live status text is not embedded in the cacheable prompt prefix.
- **Estimated savings:** **Modest token and latency improvement**, especially on repeated attempts.
- **Quality-risk notes:** **Low.**

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Add MCP capability negotiation and fallback before editor execution
- **Failure evidence:** `implement` failures `25055428237`, `25057072163`, and similar failures across the family all terminate in `Run Codex implementation`. The sampled logs show repeated unsupported tool calls across attempts.
- **Root cause category:** **Tool/runtime contract mismatch**
- **Exact fix:** At job start, detect available tool names once. If `activate_project`/Git MCP methods are unavailable, render a non-MCP prompt and suppress further MCP invocation attempts for that run.
- **Expected reliability impact:** Should recover or at least fail fast on the largest sampled implement failure mode; likely the biggest single reduction in `implement` reruns.
- **Rollback/fail-open considerations:** Safe to fail open into raw file/git workflow if capability detection is uncertain.

### 2. Validate review inputs before any GH GraphQL call in no-PR paths
- **Failure evidence:** Run `25026882619` retried a malformed GraphQL request 5 times with `Variable $number of type Int! was provided invalid value`. Run `25045997555` logged `head_ref_override and head_sha_override must be supplied by the caller`.
- **Root cause category:** **Input validation / control-plane bug**
- **Exact fix:** If `PR_NUMBER` is empty, never execute PR-number GraphQL. Require `head_ref_override` and `head_sha_override` up front and short-circuit to a neutral fallback path when absent.
- **Expected reliability impact:** Removes a deterministic class of review failures and unnecessary retries.
- **Rollback/fail-open considerations:** Fail open by skipping linked-issue enrichment rather than failing the whole review run.

### 3. Treat runner-acquisition failures in `orchestrate_poll` as deferrals, not hard failures
- **Failure evidence:** Poller runs `25058629488` and `25061570578` failed after 903s without entering business logic.
- **Root cause category:** **Infrastructure queueing / scheduler pressure**
- **Exact fix:** Either make queue timeout non-fatal for pollers or let the next scheduled tick supersede the missed attempt.
- **Expected reliability impact:** Eliminates false-negative poller failures and reduces orchestration turbulence.
- **Rollback/fail-open considerations:** Safe; polling is idempotent by nature.

### 4. Fix the CI regression in `test_capture_step_fails_open_on_invalid_jobs_payload`
- **Failure evidence:** CI run `25031188981` failed with `26 passed, 1 failed, 27 total`, specifically `FAIL test_capture_step_fails_open_on_invalid_jobs_payload`.
- **Root cause category:** **Regression in fail-open behavior**
- **Exact fix:** Restore the expected fail-open contract for invalid jobs payloads and add a targeted regression assertion around the selector fallback path.
- **Expected reliability impact:** Restores CI trust and prevents shipping broken error-capture logic.
- **Rollback/fail-open considerations:** Prefer fail-open behavior, matching the test’s stated contract.

### 5. Guard release/E2E workflows on missing secrets and repo context
- **Failure evidence:** `test_and_mark_stable` run `25034760491` logged `GH_PAT secret is required for E2E tests`, then later `All 1 implement workflow run(s) completed but no PR was created`; cleanup also logged `fatal: not a git repository`.
- **Root cause category:** **Environment/configuration guard weakness**
- **Exact fix:** Gate the E2E job early with a neutral/skip outcome when `GH_PAT` or required repo context is unavailable; do not let the workflow run into downstream false failures.
- **Expected reliability impact:** Reduces release-gate noise and makes failures more actionable.
- **Rollback/fail-open considerations:** Fail open to “skipped due to missing prereq” rather than “workflow failed.”

### 6. Repair or quarantine failing nightly validation self-test fixtures
- **Failure evidence:** `nightly_validation_selftest` run `25032083242` failed with `fixtures=3 passed=1 failed=2`; family failure rate is 100% in this sample window.
- **Root cause category:** **Test fixture instability**
- **Exact fix:** Identify the two failing fixtures and either fix them or temporarily quarantine them behind an allowlist until repaired.
- **Expected reliability impact:** Prevents a nightly signal from becoming background noise.
- **Rollback/fail-open considerations:** If quarantined, clearly annotate reduced coverage.

## AI Memory Health

- **Memory telemetry was observed** in sampled deep-dive logs: **93** `AI_MEMORY_TELEMETRY` events total.
- **Operation mix:** `record-run-event` 40, `retrieve` 20, `processed-command-check` 14, `processed-command-claim` 14, `processed-command-complete` 3, `record-candidate` 2.
- **Retrieve hit rate:** **70.0%** (`14/20` retrieves selected at least one record).
- **Average retrieve payload:** **30.2 estimated tokens**.
- **Keyword method distribution for retrieves:** `plain` **11** (55%), `none` **6** (30%), `llm` **3** (15%).
- **Zero-record retrieves:** **6**, all from sampled `review_autofix` runs:  
  `25045997555`, `25046910871`, `25026882619`, `25027333810`, `25026897459`, `25027554119`.
- **Fail-open / disabled flags:** No sampled `fail_open: true` or `enabled: false` entries were observed.
- **Push retry anomalies:** One sampled event had elevated push retries: implement run `25052302522` logged `record-run-event` with `push_attempts: 2`.

**Assessment**
- Memory is **on and lightweight**, which is good for latency.
- The weak spot is **reviewer retrieval quality**: all observed zero-hit retrieves were in `review_autofix`.
- The system is not silently failing in the sample, but the reviewer role appears under-seeded.

**Recommended changes**
1. Seed reviewer retrieval with PR title + failed checks + linked issue title instead of bare reviewer context.
2. Prefer `plain` keyword retrieval before `none` in review mode when PR metadata exists.
3. Track reviewer-memory hit rate as a workflow metric; current sampled hit rate for review mode is materially worse than planning.

## GH API Call Audit

### High-volume / high-redundancy patterns

#### 1. Review/autofix prefetches many PR resources before any model work
- **Evidence:** In run `25027554119`, the workflow fetched: PR payload, paginated issue comments, paginated reviews, paginated review comments, and linked-issue context before reviewers ran.
- **Redundancy pattern:** Multiple per-PR REST fetches plus GraphQL enrichment, then later check-run polling.
- **Concrete change:** Consolidate PR state/head/base/linked issue metadata into one GraphQL fetch where possible, and reuse a single normalized PR-context artifact across all reviewer/editor steps.
- **Estimated reduction:** **3–5 API calls per review iteration**, more on multi-iteration autofix runs.
- **Rate-limit risk reduction:** Moderate; this is one of the few sampled areas with explicit rate-limit handling.

#### 2. Malformed GraphQL requests are retried 5 times instead of failing open
- **Evidence:** Run `25026882619` retried the same invalid GraphQL request 5 times before logging `gh call failed after 5 attempts`.
- **Redundancy pattern:** Repeated bad input rather than transient retry.
- **Concrete change:** Distinguish validation errors from retryable API errors. Do **not** retry 4xx schema/input failures.
- **Estimated reduction:** **5 wasted calls per failing run**.
- **Rate-limit risk reduction:** High on bad-input paths.

#### 3. Plan progress-comment management is API-chatty
- **Evidence:** In `25052390881`, plan fetched issue metadata, paginated issue comments, looked up existing progress comments, updated progress comments, retried updates, deleted stale comments, edited the issue, and posted the final result.
- **Redundancy pattern:** Multiple issue comment mutations within one plan cycle.
- **Concrete change:** Cache the progress comment ID locally for the run and only patch on state transitions. Avoid delete+repost when update is enough.
- **Estimated reduction:** **2–4 API calls per plan run**.
- **Rate-limit risk reduction:** Low to moderate.

#### 4. Issue-status sync mixes batched and unbatched patterns
- **Evidence:** Recent successful run `25074079699` used GraphQL to gather issue numbers, but still fetched PR text via `repos/${REPOSITORY}/pulls/${PR_NUMBER}` and later fetched orchestrator issue metadata separately.
- **Redundancy pattern:** Partial batching only.
- **Concrete change:** Extend the initial GraphQL query to include PR title/body and orchestrator metadata needed for label decisions.
- **Estimated reduction:** **1–2 API calls per PR-close event**.
- **Rate-limit risk reduction:** Low, but easy win.

### Cross-reference against API hygiene rules
The workflow guidance itself emphasizes **mandatory batching, cycle-local caches, and fail-open behavior**. In the sample:
- **Good:** `cancel_on_pr_close` uses a rate-limit-aware retry helper and a small, bounded API surface.
- **Needs work:** `review_autofix` violates the spirit of fail-open on bad inputs by retrying malformed GraphQL 5 times.
- **Needs work:** `plan` uses repeated comment mutation patterns that should be cycle-local.

## MCP & Serena Efficiency

### 1. Unsupported Serena/Git MCP calls are the biggest MCP inefficiency
- **Evidence:** Implement failures `25055428237` and `25057072163` repeatedly logged `unsupported call: activate_project`; `25057072163` also logged `unsupported call: git_status`.
- **Issue:** The workflow is paying full retry costs after a known tool-capability miss.
- **Recommendation:** Capability-probe once, then downgrade the run to non-MCP mode immediately.
- **Impact:** **Large** latency and reliability gain; biggest MCP fix in the sample.

### 2. Serena startup cost is acceptable, but should be gated behind cheap preflight checks
- **Evidence:** `setup_serena.sh` ran in both plan and review samples, with `.serena/project.yml` creation around 7–8 seconds after startup.
- **Issue:** Runs that are going to fail on missing metadata or skip immediately still pay Serena startup overhead.
- **Recommendation:** Validate PR/issue metadata and fast-skip conditions first; only start Serena for runs that will actually analyze/edit code.
- **Impact:** **7–10 seconds saved per skipped/invalid run**.

### 3. Serena guidance is being repeated, which hurts token efficiency even when Serena itself works
- **Evidence:** Slow plan/review logs contain repeated Serena operating instructions and tool lists.
- **Issue:** Token cost is being paid for policy repetition rather than code reasoning.
- **Recommendation:** Keep one Serena instruction block in the stable prefix and remove duplicated copies from phase-specific prompt bodies.
- **Impact:** **Moderate** token and latency savings.

### 4. Serena efficiency reporting is failing often enough to be noisy
- **Evidence:** Both implement and review failure samples logged `Serena efficiency report generation failed.`
- **Issue:** Non-critical telemetry work is adding warning noise during already-failing runs.
- **Recommendation:** Generate the report only when the main step succeeds, or downgrade report generation to a separate best-effort post-step without warnings unless explicitly debugging.
- **Impact:** Small direct speed gain; moderate observability cleanup.

### 5. Parallelizable reads are underused
- **Evidence:** Plan and review both serialize independent metadata fetches.
- **Recommendation:** Parallelize read-only API/file fetches before prompt assembly; keep writes serialized.
- **Impact:** Small but safe latency win.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed data:** 12 OpenRouter usage lines were found, all for `review_autofix_cache_probe`, all with `cache_enabled=true`, model `minimax/minimax-m2.5`.
- **Gap:** `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` were all `na`, so **actual cache hit/miss performance cannot be verified** from the sampled window.
- **Assessment:** Cache plumbing exists, but telemetry is not sufficient to measure value.

### Cache-fragmentation causes
- Repeated stable guidance blocks in prompts.
- Dynamic run-specific noise likely mixed too early into prompt construction.
- Frequent progress-comment/status mutations in plan may encourage unstable prompt envelopes if mirrored into prompt scaffolding.

### Concrete improvements
1. **Stabilize the prompt prefix**  
   Keep policy/Serena/GH-hygiene text in a constant prefix; move run-specific issue/PR state into a suffix.
2. **Make cache metrics mandatory in workflow summaries**  
   Export cache create/read token counts to job summary or JSON artifact so hit rates can be measured.
3. **Stop double-probing when one probe is enough**  
   The current pairwise cache probes per sampled review run should be justified or removed.
4. **Preserve memory retrieval quality in review mode**  
   Reviewer memory had all observed zero-hit retrieves; seed retrieval with stronger keywords and PR-linked signals.

### Estimated impact
- **Tokens:** likely **15–30% reduction** on long prompt-heavy runs if duplication is removed.
- **Latency:** modest to meaningful on long plan/review runs.
- **Reliability:** better cache observability and fewer “mystery misses.”

## Orchestrator Health

### What looks healthy
- Successful `orchestrate_poll` runs are short: recent successful polls `25073222869`, `25071575991`, `25069789185`, `25068238411`, `25066269586`, `25064119642` all completed in **71–95s**.
- Clarify/plan/implement gates are capable of no-op exits in **1–2s**.

### What looks unhealthy
- **Skip storm / dispatch fan-out:** many same-timestamp runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` skip almost immediately.
- **Superseded long work is not being cancelled early enough:** plan run `25068290028` ran for **7,243s** before cancellation.
- **Poller failures are infrastructure-induced:** both sampled poller failures were queue-only.

### Smallest safe mitigations
1. **Centralize dispatch gating earlier** so sibling workflows are not launched unless their preconditions are already met.
2. **Strengthen concurrency keys / cancel-in-progress** for plan and review families so superseded long runs terminate sooner.
3. **Coalesce poll ticks** when the prior poll is still queued or in progress.
4. **Track control-plane waste directly**, not just failure rate.

### Observable indicators to track
- Same-minute multi-phase dispatch count per issue.
- Skipped sibling workflows per orchestrated issue.
- Runner queue wait before first user step.
- Cancelled long-run count by workflow family.
- Median phase-transition latency: clarify → plan → implement → review.

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan
- **Dominant bottleneck:** not clarify itself; it is usually fast or skipped.
- **Problem:** too many plan runs are launched despite heavy skip/no-op behavior upstream.
- **Fix:** tighter dispatch gating before plan launch.

### 2. Plan
- **Dominant bottleneck:** **compute/prompt expansion**
- **Evidence:** `25052390881` and `25068290028`.
- **Fix:** prompt dedupe, lower reasoning for deterministic tasks, earlier cancellation of superseded runs.

### 3. Implement
- **Dominant bottleneck:** **retry overhead from deterministic MCP/tool mismatch**
- **Evidence:** `25055428237`, `25057072163`, `25069841009`.
- **Fix:** capability probe + fallback mode after first unsupported tool call.

### 4. Review / Autofix
- **Dominant bottlenecks:** **compute + GH API retry/backoff**
- **Evidence:** long successful runs across the family; failures `25026882619`, `25026897459`, `25027333810`, `25045997555`, `25046910871`; rate-limit handling and malformed GraphQL retries present.
- **Fix:** stronger gating, batched prefetch, distinguish bad input from transient retry.

### 5. Validate / Orchestrate
- **Dominant bottlenecks:** **queueing and environment prereqs**
- **Evidence:** poll failures at 903s from queue starvation; E2E stable run `25034760491` required `GH_PAT` and later failed with no PR creation.
- **Fix:** neutral-skip missing-prereq runs, reduce control-plane fan-out, make queue-starved pollers non-fatal.

### Queueing vs compute vs retry vs merge/conflict overhead
- **Queueing:** clear in `orchestrate_poll` failures.
- **Compute:** dominant in long `plan` and `review_autofix` runs.
- **Retry:** dominant in `implement` unsupported-tool loops and review malformed GraphQL/rate-limit loops.
- **Merge/conflict overhead:** visible as protective git deepen/fallback logic in review logs, but **not the primary sampled failure mode** in this window.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Long-tail `plan` compute outliers (`25052390881` at 4,448s; `25068290028` cancelled at 7,243s).
- `implement` retry loops on unsupported MCP calls (`25055428237`, `25057072163`, `25069841009`).
- `review_autofix` GH API/input instability and long reviewer/editor runs.

**Top failure modes**
- Unsupported MCP/tool calls in implement.
- Invalid or missing PR metadata in review no-PR/claude-branch paths.
- Queue starvation in `orchestrate_poll`.
- One CI regression (`test_capture_step_fails_open_on_invalid_jobs_payload`).
- Missing prereqs in `test_and_mark_stable` (`GH_PAT`).

**Highest-cost drivers**
- Prompt-heavy plan/review executions.
- Full-run retries after deterministic control-plane bugs.
- High skip/other volume creating scheduler noise without user value.

**Top 3 prioritized actions**
1. **Implement MCP capability negotiation + fallback in `implement`.**
2. **Fix review no-PR/GraphQL input validation and stop retrying non-retryable 4xx errors.**
3. **Reduce prompt duplication and lower reasoning on deterministic plan/review subpaths.**

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate |
|---|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 843 | 293 | 21 | 23 | 506 | 2.49% |

| Repository | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|
| shubhodeep1/coding-workflows | 245.0 | 2.0 | 1406.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| implement | 134 | 6 | 9 | 3 | 116 | 6.72% | 240.9 | 1.0 | 2119.6 |
| review_autofix | 115 | 92 | 6 | 17 | 0 | 5.22% | 796.5 | 325.0 | 2543.3 |
| plan | 133 | 9 | 0 | 1 | 123 | 0.00% | 107.7 | 1.0 | 137.6 |
| orchestrate_poll | 32 | 30 | 2 | 0 | 0 | 6.25% | 101.3 | 41.5 | 458.6 |
| ci | 72 | 70 | 2 | 0 | 0 | 2.78% | 603.4 | 609.0 | 645.0 |
| test_and_mark_stable | 3 | 0 | 1 | 2 | 0 | 33.33% | 1843.3 | 1132.0 | 3473.8 |

### Notable sampled outliers

| Run ID | Workflow family | Conclusion | Duration (s) | Notable evidence |
|---|---|---:|---:|---|
| 25068290028 | plan | cancelled | 7243 | Long cancelled plan run; likely superseded late |
| 25052390881 | plan | success | 4448 | Single plan attempt ran ~71 min after large prompt assembly |
| 25057072163 | implement | failure | 4053 | Repeated `unsupported call: activate_project`; failed after 5 attempts |
| 25055428237 | implement | failure | 3818 | Same unsupported MCP call pattern |
| 25027554119 | review_autofix | success | 4429 | Long reviewer/editor path with rate-limit-aware API wrapper |
| 25026882619 | review_autofix | failure | 3720 | Malformed GraphQL retried 5 times |
| 25058629488 | orchestrate_poll | failure | 903 | Queue-only failure; no business logic executed |
| 25061570578 | orchestrate_poll | failure | 903 | Queue-only failure; no business logic executed |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events observed | 93 |
| `retrieve` operations | 20 |
| Retrieve hit rate | 70.0% |
| Avg retrieve estimated tokens | 30.2 |
| `keyword_method=plain` | 11 |
| `keyword_method=none` | 6 |
| `keyword_method=llm` | 3 |
| Zero-record retrieves | 6 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Events with `push_attempts > 1` | 1 |

### Prompt-cache / token telemetry summary

| Metric | Value |
|---|---|
| OpenRouter usage lines observed | 12 |
| Workflow/phase observed | `review_autofix_cache_probe` |
| Model observed | `minimax/minimax-m2.5` |
| Cache enabled | Yes |
| Prompt tokens | Unavailable (`na`) |
| Completion tokens | Unavailable (`na`) |
| Total tokens | Unavailable (`na`) |
| Cache creation tokens | Unavailable (`na`) |
| Cache read tokens | Unavailable (`na`) |

### GH API audit summary from sampled logs

| Workflow / run | Observed pattern | Reliability note |
|---|---|---|
| plan / `25052390881` | Issue GET, paginated comments, linked-PR query, issue edits, progress-comment lookup/update/delete/post | Comment churn is reducible |
| review_autofix / `25027554119` | PR GET, issue comments paginate, reviews paginate, review comments paginate, linked-issue GraphQL, check-run polling | Rate-limit-aware wrapper present, but call surface is large |
| review_autofix / `25026882619` | Same malformed GraphQL retried 5x | Should not retry validation errors |
| issue_pr_status / `25074079699` | 2 GraphQL calls + PR GET + labels POST + issue GET | Partial batching; can batch further |
| cancel_on_pr_close / `25074079667` | Small bounded API surface with rate-limit-aware retry | Good pattern to emulate |

**Data gaps**
- `summary.json` did not provide aggregate token totals.
- Sampled OpenRouter cache logs exposed probe events but not usable token/cache accounting.
- `sampled_success_runs` in the collector summary was `0`, even though success logs were present in `slow/` and `recent/`; token-cost analysis is therefore bounded to sampled log content rather than full-window accounting.
