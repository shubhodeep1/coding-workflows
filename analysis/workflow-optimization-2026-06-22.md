## Executive Summary

- **Review/autofix is the dominant latency problem.** In the `review_autofix` family, the headline p50 of 17s is misleading because it includes 25 fast sweep runs; the real PR-review paths are much slower: `.github/workflows/internal-review.yml` has p50 **2,653s** / p95 **3,293s**, and `.github/workflows/review_autofix.yml` has p50 **2,426s** / p95 **3,005s**. Slow runs **27911188250** (3,957s) and **27912850883** (3,499s) spent 10–11 minute chunks stalled in reviewer/editor phases. **Estimated impact:** save **15–25 minutes** on p95 active review runs. **Confidence:** **high**.

- **CI failures are deterministic test drift, not flaky infra.** All **7/7** `CI` runs failed in this window: **4** from stale implement-workflow assertions, **2** from checkout-audit allowlist drift, **1** from a Semble contract-test harness failure. **Estimated impact:** recover nearly all CI reliability for the window. **Confidence:** **high**.

- **One implement run drove almost all model spend.** `Internal: AI Implement` run **27921743869** used **2,652,382 tokens across 26 Codex calls** in **444s**; repo-wide total was **2,668,592 tokens across 38 calls**. **Estimated impact:** save **0.8M–1.3M tokens** per similar run by trimming repeated context and downshifting follow-up calls. **Confidence:** **medium**.

- **Orchestrator routing is noisy.** `clarify` had **160/167** other/skipped runs, `plan` **152/159**, `implement` **152/159**, and `orchestrate_clarify_respond` **159/159**. Recent runs **27922854924**, **27922854929**, **27922854922**, and **27922854915** were triggered by `ORCHESTRATOR_STATE_V2` comments and then skipped by `/answer` / `/approved` gating. **Estimated impact:** cut hundreds of short runner starts and reduce queue noise. **Confidence:** **high**.

- **Prompt/cache observability is too weak to tune confidently.** Repo telemetry shows **98 `or_calls`** and **117** runs with log telemetry, but `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_*`, and `cache_hit_rate` are all **0/null**; sampled AI memory retrieval hit rate was **0/8**. **Estimated impact:** medium; this blocks precise cost tuning more than it directly causes spend. **Confidence:** **high**.

## Speed Optimizations

1. **Critical path — cap reviewer stalls and parallelize review post-processing**
   - **Evidence:** Slow review runs **27911188250** and **27912850883** dominated active latency. Run **27911188250** had intra-step gaps of **668.2s**, **666.8s**, **411.5s**, **255.3s**, and **252.4s** inside `review / codex-agent`. Run **27912850883** showed the same pattern: **642.7s**, **638.1s**, and **258.8s** gaps.
   - **Root cause:** reviewer slots and editor/summarizer phases are spending long silent periods on the critical path.
   - **Exact change:** enforce per-reviewer slot time budgets, mark slow reviewer slots partial after timeout instead of blocking the whole run, start summarization as reviewers finish instead of after all slots complete, and preflight missing reviewer overlay files before launch.
   - **Estimated time savings:** **15–22 minutes** on active `internal-review.yml` / `review_autofix.yml` runs; more on worst-case outliers.
   - **Implementation risk:** **medium**. Keep partial-review fail-open behavior so one slow model does not suppress the full review.

2. **Critical path — remove the 300s synchronous check-run settle wait**
   - **Evidence:** Run **27911188250** spent **300.3s** waiting on `Waiting for 1 in-progress/queued check-run(s) ... deadline in 299s`. Workflow knobs in `.github/workflows/review_autofix.yml` are `CHECK_RUNS_WAIT_TIMEOUT_SECS=300` and `CHECK_RUNS_POLL_INTERVAL_SECS=20` (lines **181–182**).
   - **Root cause:** the review runner stays alive polling external check-run state instead of handing unresolved work back to the existing async path.
   - **Exact change:** reduce the synchronous wait ceiling to **60–120s**, switch to exponential backoff, and hand off unresolved cases to the existing re-dispatch/poller path instead of holding the runner for the full 300s.
   - **Estimated time savings:** **3–5 minutes** on blocked review runs.
   - **Implementation risk:** **low-medium**. Safe if unresolved waits fail open to the already-existing re-trigger flow.

3. **Critical path — trim poller setup/bookkeeping around tiny workloads**
   - **Evidence:** `orchestrate_poll` is reliable (**41/41 success**) but slow for the work done: p50 **173s**, p95 **427s**. Recent run **27922794411** found **1 active tracking issue** in **0.8s**, then spent about **21.2s** in `Checkout repository`, **10.1s** in `Install Semble`, **26.0s** in `Record poll run start`, **69.5s** in `Process each tracking issue`, and **25.1s** in `Record poll run end`. The workflow also has multiple checkouts and a `fetch-depth: 0` checkout at line **193** of `.github/workflows/orchestrate_poll.yml`.
   - **Root cause:** setup and memory bookkeeping dominate a poll cycle even when the workload is just one no-op issue.
   - **Exact change:** keep poll-only checkouts shallow unless state publication truly needs full history, collapse repeated support-source checkouts, and combine/skip memory start+end writes for no-op polls.
   - **Estimated time savings:** **40–70s** per poll run.
   - **Implementation risk:** **low-medium**.

4. **Micro-optimization — gate comment-triggered workflows before runner startup**
   - **Evidence:** recent skipped runs **27922854924** (`clarify`), **27922854929** (`plan`), **27922854922** (`implement`), and **27922854915** (`respond`) all expanded `github.event.comment.body` to an `<!-- ORCHESTRATOR_STATE_V2 ... -->` payload, then evaluated `startsWith(..., '/answer')`, `startsWith(..., '/approved')`, or similar and returned `Result: false`.
   - **Root cause:** broad `issue_comment` triggers are firing multiple workflows, and job-level `if:` logic is skipping them only after dispatch. This is an inference from the recent skip logs.
   - **Exact change:** move command-prefix routing into a single lightweight entrypoint, or at minimum add a first-line command precheck before dispatching the family workflows.
   - **Estimated time savings:** **1–10s** per skipped run, plus lower queue contention across **623** other/skipped runs.
   - **Implementation risk:** **low**.

5. **Micro-optimization — reduce redundant checkout cleanup**
   - **Evidence:** `Running Copilot Code Review` run **27921902068** spent about **73s** in `Complete job` cleanup, and **8** deep-dive `review_autofix` logs emitted `fatal: /usr/lib/git-core/git-submodule cannot be used without a working tree.` Review/autofix also uses several `actions/checkout` steps.
   - **Root cause:** repeated checkout post-steps are adding cleanup overhead and noisy submodule checks.
   - **Exact change:** consolidate checkouts where possible and skip submodule cleanup paths when the repo has no working-tree submodules.
   - **Estimated time savings:** **30–70s** on long-lived runs.
   - **Implementation risk:** **low**.

## Cost Optimizations

1. **Highest savings — shrink repeated implement context and downshift follow-up calls**
   - **Evidence:** run **27921743869** (`Internal: AI Implement`) consumed **2,652,382 tokens / 26 Codex calls / 444s**; that is effectively the whole window’s Codex spend. Average load was about **102k tokens per call**.
   - **Root cause:** repeated large-context resend across a multi-call implement loop is the most likely driver. This is an inference from the token profile.
   - **Exact change:** keep the first implementation call on the current model, but for follow-up/repair calls send only changed files, current diff, and the live task ledger; avoid re-inlining full repo context each time. If possible, apply the same targeted retrieval pattern used in review/autofix before expanding full file contents.
   - **Estimated savings:** **30–50%** on similar implement runs, or roughly **0.8M–1.3M tokens** each.
   - **Quality-risk notes:** **medium**. Do not downscope the first call aggressively; start with follow-up calls only.

2. **High leverage — make prompt caching measurable, then stabilize the cacheable prefix**
   - **Evidence:** repo-wide telemetry shows **98 `or_calls`**, but `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_write_tokens`, `or_cache_read_tokens`, and `cache_hit_rate` are all **0/null**. There were **0** runs with non-null `cache_hit_rate`.
   - **Root cause:** either cache telemetry is not wired, prompt prefixes are too unstable to cache, or both.
   - **Exact change:** emit cache prefix hashes and `cache_hit_rate`; keep invariant instructions stable; move timestamps, manifests, comment blobs, and other dynamic state to a suffix or file attachment path so only the stable prefix is cache-eligible.
   - **Estimated savings:** **10–25%** token reduction and **5–15%** latency reduction on multi-call review/implement workflows once cache behavior is real and measurable. This estimate is an inference.
   - **Quality-risk notes:** **low** if content stays identical and only ordering/stability changes.

3. **Medium savings — right-size plan reasoning effort**
   - **Evidence:** sampled plan run **27921624585** took **305s**, logged `MODEL_EDITOR: openai/gpt-5.4` with `MODEL_REASONING_EFFORT: xhigh`, and used only **4,054 tokens across 6 Codex calls**. `plan` has **159** total runs, but **152** are skipped, so the active ones matter more than family p50/p95.
   - **Root cause:** reasoning depth appears over-provisioned relative to actual token volume and work done.
   - **Exact change:** use `xhigh` only for large/multi-file planning cases; default simpler plans to `high` or `medium`.
   - **Estimated savings:** modest token savings, but likely **10–20%** per active plan run if billing scales with deeper reasoning; latency savings are more likely than dollar savings here.
   - **Quality-risk notes:** **medium**. A/B on plan acceptance/redo rate.

4. **Low savings, keep the feature — retain Semble reviewer-context queries, only dedupe overflow pulls**
   - **Evidence:** repo-wide Semble volume is small: **16 query calls / 163,561 bytes**. Deep-dive explicit event lines show **8** `reviewer-context` queries totaling **114,547 bytes** and **6** `overflow` queries totaling **38,736 bytes**. Runtime Semble fallbacks were **0**; the only **5** fallbacks were in CI contract tests.
   - **Root cause:** Semble is not the spend problem; overflow queries are reacting to prompt spill, not creating major byte noise.
   - **Exact change:** keep `reviewer-context` Semble enabled, but dedupe repeated overflow requests for the same file within a run and reuse previously fetched snippets.
   - **Estimated savings:** small; a few query calls and tens of KB on heavy review runs.
   - **Quality-risk notes:** **low**. Removing reviewer-context Semble would likely increase downstream prompt expansion cost.

5. **Runner-minute savings — stop late-skip fan-out**
   - **Evidence:** `plan`, `implement`, `clarify`, and `respond` are mostly skipped: **95–100%** of their runs in this window. Sample skipped runs carried **0** model-token telemetry, but they still started runners and executed workflow condition evaluation.
   - **Root cause:** broad comment-triggered dispatch.
   - **Exact change:** early route commands before spawning family workflows.
   - **Estimated savings:** low model-token savings in the sampled skips, but likely meaningful runner-minute savings across the **623** other/skipped runs.
   - **Quality-risk notes:** **low**.

**Semble vs. Serena note:** Semble appears to be reducing prompt expansion rather than adding noisy context. Serena is not a factor in this window: `serena_query_calls=0`, `serena_query_response_bytes=0`, `serena_query_tool_calls=0`, `serena_fallbacks=0`, and `serena_probe_* = 0`.

## Reliability Improvements

1. **Fix brittle implement-workflow tests first**
   - **Failure evidence:** CI runs **27905907603**, **27910538668**, **27917624531**, and **27921426217** all failed `Orchestrate lib unit tests` on:
     - `test_implement_workflow_delegates_stall_guard_launch_to_thread_reuse_helper`
     - `test_implement_workflow_uses_shared_stall_guard_path`
     - with expected text `'observed|killed)'` missing from `.github/workflows/implement.yml`
   - **Root cause category:** brittle text/regex assertions after workflow refactor.
   - **Exact fix:** update these tests to assert helper-path behavior and stall-guard semantics, not an exact regex fragment in the workflow YAML.
   - **Expected reliability impact:** removes **4/7 CI failures** in this window (**57%**).
   - **Rollback / fail-open:** keep one end-to-end stall-guard assertion so the test does not become too weak.

2. **Repair checkout-audit allowlist drift**
   - **Failure evidence:** CI runs **27895911008** and **27899673615** failed `Validation self-test unit tests` with `AssertionError: Unclassified checkout@v5 workflows`, first for `sync_ai_labels.yml`, then for `audit_consumer_drift.yml` plus `sync_ai_labels.yml`.
   - **Root cause category:** governance/audit fixture drift.
   - **Exact fix:** update `tests/test_workflow_checkout_integration_ref_audit.py` to classify newly added workflow files automatically or keep a single authoritative allowlist that is updated by the workflow generator.
   - **Expected reliability impact:** removes **2/7 CI failures** (**29%**).
   - **Rollback / fail-open:** keep the audit strict, but generate actionable diffs instead of failing on every new file.

3. **Fix the Semble contract-test harness without changing production fail-open behavior**
   - **Failure evidence:** CI run **27892222021** failed `Review autofix review-pipeline plumbing contract test` and emitted **5** `SEMBLE_FALLBACK` lines, all `target=overflow`, `context=contract-test`, with missing-binary errors.
   - **Root cause category:** test harness dependency injection failure.
   - **Exact fix:** provide a stub Semble binary/path in the contract fixture, or make the test explicitly assert fallback behavior when the binary is absent.
   - **Expected reliability impact:** removes the remaining **1/7 CI failure** (**14%**).
   - **Rollback / fail-open:** production should stay fail-open on missing Semble; only the test harness should change.

4. **Treat Semble fallbacks as healthy fail-open in runtime, but alert on runtime recurrence**
   - **Failure evidence:** repo telemetry shows **5** Semble fallbacks total, all `contract-test` and all in run **27892222021**; `semble_runtime_fallbacks=0`.
   - **Root cause category:** test-only rollout gap, not runtime instability.
   - **Exact fix:** keep the current runtime fail-open path, but add a simple alert threshold if runtime fallbacks ever become non-zero.
   - **Expected reliability impact:** avoids overreacting to a healthy test-only fallback while still catching a broken rollout early.
   - **Rollback / fail-open:** preserve current runtime fallback behavior.

**Pressure signals:** `break_glass_count=0` and `context_budget_warn_count=0` repo-wide. There is no evidence in this window of policy/rubric pressure or hard prompt-budget emergencies.  
**Serena health:** no `SERENA_FALLBACK` or `SERENA_PROBE` events were observed, so there is no rollout failure to mitigate there.

## AI Memory Health

Deep-dive logs did contain `AI_MEMORY_TELEMETRY`.

| Metric | Value |
|---|---:|
| Total AI memory telemetry events | 36 |
| `record-run-event` | 20 |
| `record-candidate` | 8 |
| `retrieve` | 8 |
| Retrieve hit rate (`records_selected > 0`) | **0/8 = 0%** |
| Avg `estimated_tokens` on retrieve | **0.0** |
| `keyword_method` distribution | `llm`: 8, `plain`: 0, `none`: 0 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Push retries `>1` | 0 |
| Retrieve budget field emitted | not present in sampled telemetry |

- **Runs with retrieve attempts:** **27892142848**, **27896448097**, **27900323122**, **27901454796**, **27906809873**, **27909309905**, **27911188250**, **27912850883**.
- **Observed pattern:** all retrieves were `enabled: true` and `ok: true`, but every one returned `records_selected: 0` and `estimated_tokens: 0`.
- **Interpretation:** write-side memory activity exists (`record-candidate` is present), but retrieval is currently ineffective. Likely causes include namespace mismatch, poor query formulation, or missing index coverage. This is an inference.
- **Recent non-review example:** `orchestrate_poll` run **27922794411** only emitted bookkeeping-style memory events (`poll_started` / `poll_completed`), not useful retrievals.

**Recommended next changes**
1. Add a `miss_reason` field to `retrieve` telemetry.
2. On a 0-hit `llm` retrieval, immediately retry with a cheap `plain` keyword path before returning empty.
3. Emit retrieval budget information so `estimated_tokens vs budget` can be tracked.
4. Verify that `record-candidate` and `retrieve` are writing/reading the same namespace and repo scope.

## GH API Call Audit

No 429s, secondary rate-limit events, or retry storms were visible in sampled logs. The main issue is redundancy, not rate limiting.

| Pattern | Evidence | Exact change | Est. call reduction | Risk |
|---|---|---|---:|---|
| Duplicate paginated PR file fetch | `.github/workflows/review_autofix.yml` lines **498** and **555** both call `gh api --paginate repos/.../pulls/{PR}/files` | Fetch once, write to a temp JSON file, reuse downstream | **1 full paginated scan per review run** | Low |
| Repeated PR state lookups | `scripts/review_run_reviewers.sh` lines **131** and **2789**; `scripts/review_apply_fixes.sh` line **1577** all fetch PR state | Resolve PR state once and export it to child stages | **2–3 calls per review run** | Low |
| Long polling of check-run settle state | `.github/workflows/review_autofix.yml` lines **181–182** set 20s polling for up to 300s; run **27911188250** spent the full **300.3s** waiting | Use exponential backoff and exit to async re-dispatch sooner | If 1 API call/poll, cut from ~15 polls to ~5–6 | Low-medium |
| Large API surface concentrated in one workflow | `.github/workflows/review_autofix.yml` contains **29** `gh api` call sites; `internal-review.yml` has **2**; `scripts/review_run_reviewers.sh` has **2**; `scripts/review_apply_fixes.sh` has **1** | Centralize shared data fetches and pass them through env/files | Depends on path reuse | Low |
| Poller issue discovery already batched well | `.github/workflows/orchestrate_poll.yml` line **155** uses `gh issue list --json number,title --limit 20`; run **27922794411** found 1 issue in **0.8s** | Keep as-is; optimize setup around it instead | Minimal API savings available here | Low |

**Repository-specific API hygiene observations**
- Existing `gh_retry` wrappers are a good pattern; keep them.
- API batching is already good in `orchestrate_poll`.
- The repo should focus on **reusing fetched PR metadata** inside review/autofix rather than redesigning retries.

## Prompt Cache & Memory System

- **Prompt cache is effectively unobservable right now.** Repo-wide telemetry shows:
  - `cache_hit_rate = null`
  - `or_cache_write_tokens = 0`
  - `or_cache_read_tokens = 0`
  - `or_prompt_tokens = 0`
  - `or_completion_tokens = 0`
  - `or_total_tokens = 0`
  - despite **98 `or_calls`**
- **Interpretation:** either the OR token/cache collector is not wired, or the prompts are too unstable to produce hits. Without this, the largest prompt-heavy loops cannot be tuned safely.
- **Semble is the one context-control mechanism that looks useful.** Deep-dive logs show:
  - `reviewer-context`: **8 queries**, **114,547 bytes**
  - `overflow`: **6 queries**, **38,736 bytes**
  - no runtime fallbacks
  - only CI contract-test fallbacks
  This is consistent with Semble reducing prompt expansion rather than adding noisy bytes.
- **No hard budget warnings surfaced.** `context_budget_warn_count=0` and `break_glass_count=0`, but overflow Semble pulls in runs **27909309905**, **27912850883**, and **27901454796** still suggest file-level spill pressure before any formal warning triggers.
- **AI memory retrieval is not helping yet.** Hit rate is **0%** in sampled review runs.

**Concrete improvements**
1. Emit real prompt-cache telemetry (`cache_hit_rate`, read/write tokens, stable prefix hash).
2. Keep the reusable prompt prefix stable; move run-specific noise to a suffix/file path.
3. Reuse overflow retrieval results within a run instead of refetching.
4. Fix AI memory retrieval miss behavior before relying on it for prompt compression.

**Expected impact:** once observable, **10–25%** token savings and **5–15%** latency savings on multi-call runs is a reasonable first target. This is an inference, not a measured result from the current window.

## Orchestrator Health

- **Poller reliability is good, but efficiency is poor.** `orchestrate_poll` went **41/41** successful, yet p50 was **173s** and p95 **427s**. In run **27922794411**, the actual issue-discovery call took **0.8s** and the run ended `Dispatched: 0, force-merged: 0, blocked: 0`, so setup/bookkeeping is the main cost.
- **Late-skip fan-out is the clearest orchestration issue.** Recent comment-triggered runs **27922854924**, **27922854929**, **27922854922**, and **27922854915** all evaluated large `ORCHESTRATOR_STATE_V2` comment bodies and then skipped. This strongly suggests trigger fan-out followed by late routing.
- **Metrics are distorted by reusable-workflow caller/callee pairs.** `.github/workflows/internal-review.yml` lines **55** and **129** both `uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@main`, and `.github/workflows/internal-orchestrate-poll.yml` line **16** similarly wraps `orchestrate_poll.yml`. Count logical operations separately from raw workflow-run counts.
- **No sampled evidence of stuck terminal states or conflict-heal retry storms.** The visible pain point is routing noise and long review/autofix active loops, not orchestration deadlock.

**Track these indicators**
- Late-skip count by workflow family
- Active-run / skipped-run ratio by family
- Poll no-op rate
- Caller/callee pair count per logical review
- AI memory retrieve hit rate
- Runtime Semble fallback rate

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck | Evidence | Bottleneck type | Fix order |
|---|---|---|---|---|
| Clarify | Most runs are skipped | **160/167** `clarify` runs are other/skipped; run **27922854924** skipped after evaluating `ORCHESTRATOR_STATE_V2` | Routing / queue noise | 4 |
| Plan | Most runs are skipped; active plans are over-reasoned | **152/159** skipped; active run **27921624585** took **305s** with `MODEL_REASONING_EFFORT=xhigh` | Routing + compute | 3 |
| Implement | Most runs are skipped; active implement is extremely token-heavy | **152/159** skipped; active run **27921743869** took **444s** and **2.65M tokens** | Compute / prompt expansion | 2 |
| Review & autofix | Dominant end-to-end latency on active work | Active PR review paths are p50 **2,426–2,653s**; slow runs hit **3,499–3,957s** with multi-minute reviewer/editor stalls | Compute / merge-wait | 1 |
| Validate / CI | Deterministic failures stop flow early | **7/7 CI** runs failed, all in tests | Validation / rerun overhead | 1 |
| Orchestrate loop | Poller setup dominates tiny workloads | `orchestrate_poll` p50 **173s**; run **27922794411** spent far more time in setup/bookkeeping than in issue discovery | Setup / bookkeeping | 3 |

**Queueing vs compute vs retry vs merge/conflict**
- **Queueing:** visible but not dominant in sampled runs.
- **Compute:** dominated by `review_autofix` and the single active `implement` run.
- **Retry overhead:** limited; no broad retry storms observed.
- **Merge/conflict overhead:** the largest explicit case was the **300s** check-run settle wait in review/autofix.
- **Conflict-heal / terminal-state overhead:** not prominent in the sampled evidence.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - Active `review_autofix` runs are 40–55 minutes long and dominated by reviewer/editor stalls.
  - `orchestrate_poll` spends ~3 minutes per run even when only one issue is active and nothing is dispatched.
  - Orchestrator comment routing triggers many late-skipped runs.

- **Top failure modes**
  - 4 stale implement-workflow tests
  - 2 checkout-audit allowlist failures
  - 1 Semble contract-test harness failure

- **Highest-cost drivers**
  - `Internal: AI Implement` run **27921743869**: **2,652,382 tokens / 26 Codex calls / 444s**
  - Repo-wide prompt/cache observability gap: **98 `or_calls`** with no usable token/cache breakdown
  - Review/autofix prompt-control work is using Semble lightly and likely effectively, but AI memory retrieval is not contributing

- **Top 3 prioritized actions**
  1. Fix the 3 CI test-drift buckets so validation stops blocking the pipeline.
  2. Shorten active review/autofix runs by capping reviewer stalls and removing the 300s synchronous wait.
  3. Reduce implement prompt size and make prompt-cache behavior measurable before changing more models.

## Metrics Appendix

### Repo window overview

| Repo | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 dur (s) | p95 dur (s) | Avg dur (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 822 | 176 | 7 | 16 | 623 | 0.85% | 2.0 | 568.95 | 148.19 |

### Workflow-family overview

| Workflow family | Total | Success | Failure | Cancelled | Other/skipped | p50 dur (s) | p95 dur (s) | Avg dur (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 7 | 0 | 7 | 0 | 0 | 102 | 275.2 | 155.0 |
| `review_autofix` | 78 | 62 | 0 | 16 | 0 | 17.0 | 3190.6 | 1178.8 |
| `orchestrate_poll` | 41 | 41 | 0 | 0 | 0 | 173 | 427.0 | 203.0 |
| `copilot_pull_request_reviewer` | 18 | 18 | 0 | 0 | 0 | 366.5 | 458.1 | 366.1 |
| `plan` | 159 | 7 | 0 | 0 | 152 | 1 | 11.0 | 23.5 |
| `implement` | 159 | 7 | 0 | 0 | 152 | 1 | 11.0 | 40.6 |
| `clarify` | 167 | 7 | 0 | 0 | 160 | 1 | 11.0 | 7.6 |
| `orchestrate_clarify_respond` | 159 | 0 | 0 | 0 | 159 | 1 | 10.0 | 2.8 |
| `orchestrate` | 1 | 1 | 0 | 0 | 0 | 397 | 397 | 397.0 |

### Review/autofix split by workflow path

| Workflow | Path | Total | Success | Cancelled | p50 dur (s) | p95 dur (s) | Avg dur (s) |
|---|---|---:|---:|---:|---:|---:|---:|
| Internal: AI Review Autofix Sweep | `.github/workflows/review_autofix_sweep.yml` | 25 | 25 | 0 | 7 | 9.0 | 7.2 |
| Internal: AI Review & Autofix | `.github/workflows/internal-review.yml` | 36 | 28 | 8 | 2653.0 | 3293.25 | 1852.8 |
| Codex PR Self-Healing Semantic Agent | `.github/workflows/review_autofix.yml` | 17 | 9 | 8 | 2426 | 3005.4 | 1474.2 |

### Repo-wide cost / telemetry totals

| Metric | Value |
|---|---:|
| `codex_tokens_used` | 2,668,592 |
| `codex_calls` | 38 |
| `or_calls` | 98 |
| `or_prompt_tokens` | 0 |
| `or_completion_tokens` | 0 |
| `or_total_tokens` | 0 |
| `or_cache_write_tokens` | 0 |
| `or_cache_read_tokens` | 0 |
| `cache_hit_rate` | unavailable (`null`) |
| Runs with parsed log telemetry | 117 |
| `wall_clock_p50_ms` | 7,000 |
| `wall_clock_p99_ms` | 3,497,500 |
| `wall_clock_sample_count` | 116 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

### Notable run outliers

| Run ID | Workflow family | Workflow | Outcome | Duration (s) | Key evidence |
|---|---|---|---|---:|---|
| 27911188250 | `review_autofix` | Internal: AI Review & Autofix | success | 3957 | 668.2s + 666.8s reviewer stalls; 411.5s editor gap; 300.3s check-run wait |
| 27912850883 | `review_autofix` | Codex PR Self-Healing Semantic Agent | success | 3499 | 642.7s + 638.1s reviewer stalls; 258.8s editor gap |
| 27921743869 | `implement` | Internal: AI Implement | success | 444 | 2,652,382 tokens / 26 Codex calls; implement step dominated runtime |
| 27921624585 | `plan` | Internal: AI Plan | success | 305 | `MODEL_REASONING_EFFORT=xhigh`; 4,054 tokens / 6 Codex calls |
| 27922794411 | `orchestrate_poll` | Internal: AI Orchestrate Poller | success | 199 | 1 active issue; ~0.8s issue discovery vs ~150s setup/bookkeeping |
| 27892222021 | `ci` | CI | failure | 307 | 5 Semble contract-test fallbacks; failed review-pipeline plumbing contract test |

### MCP telemetry

| Server | Target | Query calls | Logged bytes | Fallbacks | Probe ok | Probe failed | Probe skipped | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | `reviewer-context` | 8 | 114,547 | 0 | n/a | n/a | n/a | Deep-dive explicit event lines |
| Semble | `overflow` | 6 | 38,736 | 5 | n/a | n/a | n/a | All 5 fallbacks were `context=contract-test` in CI run 27892222021 |
| Serena | all targets | 0 | 0 response bytes | 0 | 0 | 0 | 0 | No event-grade Serena usage observed |
| Other MCP servers | none | 0 | 0 | 0 | 0 | 0 | 0 | No event-grade non-Semble/Serena MCP activity observed |

**Note:** repo-level telemetry reports **16** Semble calls / **163,561** bytes. Deep-dive explicit event lines account for **14** calls / **153,283** bytes; the remaining **2** calls / **10,278** bytes were present in run-level telemetry but not surfaced in excerpted event lines.

### AI memory retrieve metrics

| Metric | Value |
|---|---:|
| Total AI memory events | 36 |
| Retrieve ops | 8 |
| Retrieve hit rate | 0% |
| Avg `estimated_tokens` | 0.0 |
| Budget field emitted | No |
| `keyword_method=llm` | 8 |
| `keyword_method=plain` | 0 |
| `keyword_method=none` | 0 |
| `fail_open: true` | 0 |
| `enabled: false` | 0 |
| Push retries `>1` | 0 |

### GH API hotspot summary

| File | API call sites observed | Main issue |
|---|---:|---|
| `.github/workflows/review_autofix.yml` | 29 `gh api` sites | duplicate PR-file fetches; repeated PR metadata/comment/label calls; long polling loop |
| `.github/workflows/internal-review.yml` | 2 `gh api` sites | wrapper around reusable review workflow |
| `scripts/review_run_reviewers.sh` | 2 `gh api` sites | repeated PR state lookup |
| `scripts/review_apply_fixes.sh` | 1 `gh api` site | repeated PR state lookup |
| `.github/workflows/orchestrate_poll.yml` | 1 `gh issue list` site | API usage already batched; setup dominates instead |

