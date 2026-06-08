## Executive Summary

- **Fix the CI regression first.** `shubhodeep1/coding-workflows` had **21/21 CI failures** in this window; **20/21** failed at job `lint`, step `Orchestrate poll process unit tests`, and **14/14 deep-dive CI error runs** contained `FAIL test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` (for example run **27107200428** ended `284 passed, 1 failed, 285 total`; run **27110411091** ended `283 passed, 2 failed, 285 total`). **Estimated impact:** recover roughly **1,300-1,725s** of dead-end CI time per attempt and remove about **9.1 runner-hours** of wasted CI time across these 21 runs. **Confidence:** high.

- **`review_autofix` is the dominant speed and cost problem.** Collector-measured Codex usage is **164,794,978 tokens** in `review_autofix`, or **78.7%** of the repo’s measured total (**209,374,964**). Tail runs are extreme: run **27110905305** took **3,940s**, used **115,346,152 tokens** across **60 Codex calls**, and emitted **12 parsed `CONTEXT_BUDGET_WARN`** events; run **27101252005** took **2,946s** and used **49,430,592 tokens** with **6** context warnings. **Estimated impact:** a conditional fan-out / reasoning reduction on oversized prompts could save roughly **41M-66M tokens per 1,000-run window** and cut **10-25 minutes** from worst-case review runs. **Confidence:** high.

- **`implement` is the second major token/latency driver, and the current default looks too expensive for the common case.** Run **27099766039** (`Internal: AI Implement`) took **3,937s**, used **44,577,959 tokens** across **34 Codex calls**, and repeatedly logged `MODEL_EDITOR: openai/gpt-5.4` with `MODEL_REASONING_EFFORT: xhigh`. **Estimated impact:** gating `xhigh` behind retries / large diffs could save roughly **9M-16M tokens** per window and several minutes on heavy implement runs. **Confidence:** medium.

- **AI memory persistence is hurting reliability more than memory retrieval is helping.** Plan run **27102733145** failed in job `plan / plan`, step `Check and claim /answer command` with `AI_MEMORY_ERROR: Failed to push memory branch after 5 attempts` and `! [rejected] HEAD -> ai-memory (fetch first)`. Meanwhile, sampled reviewer-memory retrievals were mostly misses: **8/8** sampled `review_autofix` retrieves selected **0 records**, while implement run **27099766039** retrieved **1 record** with estimated **28 tokens**. **Estimated impact:** hardening memory push semantics can remove at least the observed plan failure mode and reduce hidden retry noise. **Confidence:** high.

- **Prompt-cache behavior is effectively unmeasured right now.** Across **124** runs with parsed telemetry, repo-level `cache_hit_rate` is **null**, and all `or_*` counters are **0**, even though trusted runtime logs in review runs **27110905305** and **27105568462** show `OPENROUTER_PROMPT_CACHE_DISABLED: false`. **Estimated impact:** unknown until instrumentation is fixed, but this is the biggest observability gap blocking safe cache tuning. **Confidence:** high on the gap, low on the current true cache hit rate.

- **Semble is mostly helping; Serena is effectively absent.** Trusted runtime Semble query lines show useful, cheap `reviewer-context` lookups in `review_autofix` (deduped deep-dive sample: **7 queries**, **108,225 bytes**, **~529ms avg**), while implement run **27099766039** used **7 `overflow` queries** against historical analysis docs (**49,385 bytes**, **~507ms avg**) that look lower-value. Serena had **0 queries, 0 fallbacks, 0 probes** in collector telemetry. **Estimated impact:** keep Semble for targeted reviewer context, trim noisy overflow fetches, and do not spend effort tuning Serena until it emits traffic. **Confidence:** medium.

## Speed Optimizations

Ranked by expected latency reduction.

### Critical-path wins

1. **Restore green CI by fixing the orchestrate-poll unit-test regression**
   - **Evidence:** `ci` had **21 runs / 21 failures**, completed-run **p50 1,650s**, **p95 1,725s**. **20/21** failures ended at job `lint`, step `Orchestrate poll process unit tests`. In deep-dive logs:
     - Run **27107200428** (`CI`) logged `FAIL test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` and ended `284 passed, 1 failed, 285 total`.
     - Run **27110411091** logged that same failure plus `FAIL test_resolver_tooling_refresh_allowlist_includes_both_retry_preludes`, ending `283 passed, 2 failed, 285 total`.
   - **Root cause:** test regression / contract drift in orchestrate-poll behavior, plus a secondary contract mismatch around timeout-prelude staging.
   - **Exact change:** repair the implementation that `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` is exercising, and align `review_autofix.yml`’s timeout-prelude staging with the expected `install -m 0644 ... integration-sync-conflict-resolver-retry-timeout-prelude.txt` signature.
   - **Estimated time savings:** removes **~22-29 minutes** of dead-end wait per CI attempt and unblocks the whole merge path.
   - **Implementation risk:** low. This is a contract fix, not a product-behavior expansion.

2. **Collapse `review_autofix` fan-out when prompt-size warnings fire**
   - **Evidence:** 
     - Run **27110905305** (`Internal: AI Review & Autofix`) took **3,940s**, used **115,346,152 tokens** across **60 Codex calls**, and emitted `CONTEXT_BUDGET_WARN` with `prompt_tokens=216970` and later `220663`; it also logged `REVIEWERS_SUCCESSFUL: 5`, `MODEL_EDITOR: openai/gpt-5.4`, `REVIEWER_REASONING_EFFORT: xhigh`, and `EDITOR_REASONING_EFFORT: xhigh`.
     - Run **27101252005** took **2,946s**, used **49,430,592 tokens**, and emitted **6** context warnings with `prompt_tokens=214681` and `216296`.
     - Family-level `review_autofix` completed-run **p95** is **2,887s**; all **18** repo `context_budget_warn_count` events are in this family.
   - **Root cause:** **Inference:** repeated reviewer/editor passes over largely identical, oversized prompt context; the warning-triggered prompt sizes are already at or above practical context pressure thresholds before fan-out completes.
   - **Exact change:** on the first `CONTEXT_BUDGET_WARN`, switch to a reduced path:
     1. generate one compressed PR/check-run summary,
     2. run only the top **2 reviewers + editor**,
     3. lower secondary reviewer/editor reasoning from `xhigh` to `high`,
     4. skip third-attempt retries for low-priority reviewer slots if quorum is already met.
   - **Estimated time savings:** **600-1,500s** on affected tail runs.
   - **Implementation risk:** medium. Quality risk is manageable if one deep reviewer path remains.

3. **Shorten the pre-review check-run wait budget**
   - **Evidence:** Run **27105568462** (`review_autofix`, **2,897s**) logged `CHECK_RUNS_WAIT_TIMEOUT_SECS: 300` and later `CHECK_RUNS_WAIT_TIMEOUT reached after 300s ... proceeding with snapshot.` This is a pure wait cost on the review critical path.
   - **Root cause:** the workflow waits too long for queued/in-progress check-runs before snapshotting.
   - **Exact change:** reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` from **300** to **60-120** for normal runs, or snapshot after one unchanged poll cycle and fail open with current data.
   - **Estimated time savings:** up to **180-300s** on runs that hit the timeout path.
   - **Implementation risk:** low-medium. Keep the existing fail-open sentinel behavior so missing check-run data is explicit, not silent.

4. **Gate `implement` `xhigh` reasoning instead of using it as the broad default**
   - **Evidence:** Run **27099766039** (`Internal: AI Implement`) took **3,937s**, used **44,577,959 tokens**, made **34 Codex calls**, and repeatedly logged `MODEL_REASONING_EFFORT: xhigh`. Completed `implement` runs had **p50 622s** and **p95 2,621s**.
   - **Root cause:** expensive reasoning level is applied before the workflow knows whether the task actually needs it.
   - **Exact change:** make `high` the default for first-pass implementation; escalate to `xhigh` only for:
     - retry passes,
     - large multi-file diffs,
     - conflict/merge-heavy tasks,
     - or explicit repo override.
   - **Estimated time savings:** roughly **300-1,200s** on the heaviest implement runs.
   - **Implementation risk:** medium. Keep an escalation path for complex tasks.

### Micro-optimizations

5. **Reduce queue-heavy `orchestrate_poll` starts**
   - **Evidence:** `orchestrate_poll` completed-run **p50** is **162.5s** and **p95** is **532.25s**. Recent log summaries show queue/startup dominating:
     - Run **27110432396**: **722s**, `Job is waiting for a hosted runner to come online`.
     - Run **27110943707**: **181s**, runner queued and `poll` dominated runtime.
     - Run **27112574469**: **164s**, runner wait consumed noticeable startup time.
   - **Root cause:** short poll jobs are paying runner acquisition overhead too often.
   - **Exact change:** modestly coalesce adjacent poll invocations, or add a very early no-op exit when no orchestrator-managed work changed since the previous tick.
   - **Estimated time savings:** **60-300s** per lightly loaded poller run.
   - **Implementation risk:** low-medium. The tradeoff is slightly slower pickup of new work.

6. **Fix validate cache-save drift before it turns into a speed and reliability problem**
   - **Evidence:** Validate run **27110728522** succeeded in **278s** but its `log_summary` reported `Path Validation Error: Path(s) specified in the action for caching do(es) not exist` and a Node.js 20 deprecation warning for `actions/cache/restore@v4`.
   - **Root cause:** cache path drift plus action runtime deprecation.
   - **Exact change:** correct the cache-save path and move to a Node 24-compatible cache action revision.
   - **Estimated time savings:** small per run today, but prevents future cold-cache regressions and a likely hard break after the Node 24 enforcement date.
   - **Implementation risk:** low.

## Cost Optimizations

Ranked by expected token / dollar savings.

1. **Reduce `review_autofix` reviewer fan-out and reasoning level on oversized prompts**
   - **Evidence:** `review_autofix` accounts for **164,794,978 / 209,374,964 = 78.7%** of collector-measured Codex tokens. Two parsed outliers dominate that spend:
     - Run **27110905305**: **115,346,152 tokens**, **60 Codex calls**, **12 parsed context warnings**.
     - Run **27101252005**: **49,430,592 tokens**, **24 Codex calls**, **6 context warnings**.
     Together they account for **164,776,744 tokens**, or essentially **all parsed `review_autofix` token usage** in this window.
   - **Root cause:** repeated prompt/context expansion across multiple reviewer slots, combined with `xhigh` reasoning under context pressure.
   - **Exact change:** when `CONTEXT_BUDGET_WARN` fires, reduce reviewer count, compress shared context once, and step down secondary passes to `high`.
   - **Estimated savings:** **25-40%** of `review_autofix` spend, or about **41M-66M tokens** per 1,000-run window.
   - **Quality-risk notes:** medium. Keep one deep reviewer + editor path so defect-finding depth does not collapse.

2. **Downshift `implement` from `xhigh` by default**
   - **Evidence:** `implement` consumed **44,577,959 tokens** in the window. The single slow parsed outlier, run **27099766039**, consumed that entire measured family spend while running `xhigh`.
   - **Root cause:** high reasoning spend is front-loaded into first-pass implementation.
   - **Exact change:** use `high` first-pass implement reasoning and reserve `xhigh` for retries, conflict-heavy edits, or explicit task overrides.
   - **Estimated savings:** **20-35%** of measured `implement` spend, or roughly **9M-16M tokens** per window.
   - **Quality-risk notes:** medium. Use automatic escalation instead of removing `xhigh`.

3. **Keep Semble where it compresses context; trim noisy `overflow` fetches**
   - **Evidence:**
     - In `review_autofix`, deduped trusted runtime Semble queries were mostly targeted and cheap: **7 `target=reviewer-context` queries**, **108,225 bytes**, **~528.6ms average**; plus **1 `conflict-resolver-context`** query (**10,613 bytes**, **475ms**). Example: run **27110905305**, step `Run reviewer models`, logged `SEMBLE_QUERY target=reviewer-context ... bytes=15735 ms=587`.
     - In `implement`, run **27099766039** logged **7 `target=overflow`** queries to historical `analysis/workflow-optimization-*.md` files, **49,385 bytes total**, **~507ms average**.
   - **Root cause:** Semble is doing two different jobs:
     - in review, it appears to reduce prompt expansion with targeted reviewer-context retrieval;
     - in implement, it is sometimes bringing in lower-value historical analysis docs.
   - **Exact change:** keep `reviewer-context` and conflict-resolver Semble paths enabled; restrict `implement` `overflow` queries to task-local docs / changed files / exact referenced artifacts, not broad historical analysis notes.
   - **Estimated savings:** small direct token savings, plus about **3.5s** latency saved on the sampled implement outlier and lower noise in prompts.
   - **Quality-risk notes:** low if current fail-open fallback remains in place.
   - **Serena note:** there is **no evidence** that Serena is replacing downstream tool/model work in this window; collector telemetry shows **0 queries / 0 fallbacks / 0 probes**.

4. **Fix prompt-cache observability before trying to tune cache policy**
   - **Evidence:** repo summary shows `cache_hit_rate: null`, `or_calls: 0`, `or_total_tokens: 0`, `or_cache_write_tokens: 0`, `or_cache_read_tokens: 0` across **124** telemetry-covered runs, yet trusted runtime logs in runs **27110905305** and **27105568462** show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
   - **Root cause:** cache instrumentation or parser coverage gap.
   - **Exact change:** emit and parse stable prompt-cache metrics per call/run before changing prompt shapes or cache policy.
   - **Estimated savings:** not quantifiable yet; this is a prerequisite optimization.
   - **Quality-risk notes:** none. This is observability, not behavior change.

5. **Stop paying for avoidable reruns and no-op workflow work**
   - **Evidence:** the **21** failed CI runs spent **32,744s** total (**9.1 hours**) while using **0 AI tokens**. Recent review sweep runs **27112579805** and **27111036521** each did GH/API work only to skip because the candidate PR already had an active run.
   - **Root cause:** preventable CI dead ends and no-op orchestration/sweep work.
   - **Exact change:** fix the CI blocker, and debounce sweep dispatch/snapshot logic when an active run is already present.
   - **Estimated savings:** primarily GitHub Actions minutes rather than model spend.
   - **Quality-risk notes:** low.

## Reliability Improvements

Ranked by expected failure-rate / rerun-rate reduction.

1. **Repair the broken CI contracts**
   - **Failure evidence:** 
     - Window result: **0/21 CI successes**.
     - **20/21** CI failures ended at `lint` → `Orchestrate poll process unit tests`.
     - Deep-dive error logs: **14/14** contain `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs`.
     - Run **27110411091** also failed `test_resolver_tooling_refresh_allowlist_includes_both_retry_preludes` because `review_autofix.yml` did not stage the timeout-prelude template via the expected `install -m 0644 ... integration-sync-conflict-resolver-retry-timeout-prelude.txt` signature.
   - **Root cause category:** test regression / workflow-contract drift.
   - **Exact fix:** align orchestrate-poll dependency-gate reissue behavior with test expectations, and restore the timeout-prelude staging contract.
   - **Expected reliability impact:** highest in the window; it should directly remove the repo’s dominant failure mode.
   - **Rollback / fail-open:** do **not** fail open here. Keep the tests strict; fix the implementation/contract.

2. **Harden `ai-memory` branch writes, but fail open only for non-critical writes**
   - **Failure evidence:** 
     - Plan run **27102733145** failed with `AI_MEMORY_ERROR: Failed to push memory branch after 5 attempts` and `HEAD -> ai-memory (fetch first)`.
     - Review runs **27101252005**, **27105568462**, and **27103488076** needed **2**, **2**, and **3** push attempts respectively just to record `phase_started`.
     - Implement run **27099766039** logged `memory force-tick-get failed ... (fail-open)` and `memory force-tick-put failed ... (fail-open)` while still succeeding overall.
   - **Root cause category:** shared-branch write contention / optimistic concurrency failure.
   - **Exact fix:** add a fresh fetch/rebase before each retry plus jittered backoff; keep command-claim semantics strict, but downgrade non-critical event writes (`record-run-event`, `finalize-task`, force-tick bookkeeping) to soft-fail warnings.
   - **Expected reliability impact:** removes the observed hard plan failure and reduces hidden contention noise in review/orchestrator paths.
   - **Rollback / fail-open:** keep duplicate-claim prevention strict; only fail open for best-effort memory writes.

3. **Treat `CONTEXT_BUDGET_WARN` as a reliability alarm, not just a cost alarm**
   - **Failure evidence:** repo `break_glass_count` is **0**, but `context_budget_warn_count` is **18**, all in `review_autofix`. Trusted runtime warnings were confirmed in:
     - Run **27101252005**: **6** warnings with `prompt_tokens=214681` and `216296`.
     - Run **27110905305**: parsed count **12**; trusted runtime lines show **6 unique warnings** across two checkpoints with `prompt_tokens=216970` and `220663`.
   - **Interpretation:** this is **prompt-size risk**, not policy/rubric pressure. No `BREAK_GLASS` was observed anywhere.
   - **Root cause category:** prompt growth / repeated context expansion.
   - **Exact fix:** trigger early compression and reviewer reduction when the first warning is emitted; keep current warning telemetry and add a “compressed_after_warn=true” marker.
   - **Expected reliability impact:** lower risk of empty outputs, excessive retries, or context-window edge failures in long review runs.
   - **Rollback / fail-open:** thresholds can be tuned back upward if quality drops.

4. **Tighten reviewer-slot retry policy after repeated retryable rate-limit failures**
   - **Failure evidence:** In review run **27110905305**, reviewer slot `minimax/minimax-m2.5` failed on attempts **1**, **2**, and **3**, each classified as retryable `rate_limit`. Slot `qwen/qwen3.6-plus` also needed a retry before succeeding. The overall run still succeeded with `REVIEWERS_SUCCESSFUL: 5`.
   - **Root cause category:** external model/transient provider failures with an overly generous per-slot retry budget.
   - **Exact fix:** after **2** retryable failures for an unmapped slot, either:
     - fail open for that slot, or
     - fail over immediately to a mapped backup slot / cheaper reasoning level.
   - **Expected reliability impact:** fewer tail stalls and less chance that one flaky slot turns a healthy run into a timeout.
   - **Rollback / fail-open:** preserve the current run-level fail-open behavior; only tighten per-slot retry loops.

5. **Separate healthy Semble fail-open test coverage from real runtime incidents**
   - **Failure evidence:** collector summary reports **75 `semble_fallbacks`**, but trusted deep-dive runtime lines show these are CI contract-test fixtures pointing to `/missing_semble` paths (for example run **27107200428**, `SEMBLE_FALLBACK target=overflow ... reason=... missing_semble`; run **27110411091` duplicated 5 of these lines in step `Targeted file context contract tests`). Dedupe across trusted runtime lines yields **70 unique fallback events**, all in CI tests. Serena fallbacks/probes remain **0**.
   - **Interpretation:** this is **healthy fail-open test coverage**, not a masked Semble rollout outage.
   - **Root cause category:** telemetry aggregation noise.
   - **Exact fix:** tag test-only MCP fallback lines or exclude contract-test logs from operational fallback dashboards.
   - **Expected reliability impact:** reduces false alarms so real MCP regressions are easier to spot.
   - **Rollback / fail-open:** keep raw fallback lines in logs; only adjust aggregation/alerting.

6. **Fix validate cache drift before the Node 24 cutover**
   - **Failure evidence:** validate run **27110728522** succeeded but logged `Path Validation Error ... no cache is being saved` and a deprecation warning that `actions/cache/restore@v4` will be forced to Node 24 on **2026-06-16**.
   - **Root cause category:** maintenance/config drift.
   - **Exact fix:** fix the cache-save path and upgrade to a Node 24-compatible cache action revision.
   - **Expected reliability impact:** prevents a predictable near-term validate break and restores future cache saves.
   - **Rollback / fail-open:** low risk; standard action maintenance.

## AI Memory Health

Trusted deep-dive logs contained **37 unique `AI_MEMORY_TELEMETRY` events**.

| Metric | Value | Notes |
|---|---:|---|
| Unique `retrieve` ops | 9 | Deep-dive runs only |
| Retrieve hit rate | **11.1%** (1/9) | 1 hit, 8 misses |
| Avg `estimated_tokens` | **3.1** | Budget field was **not emitted**, so budget comparison is unavailable |
| `keyword_method` distribution | `llm`: **8**, `plain`: **1** | No `none` observed |
| Zero-record retrieves | **8/9** | All 8 misses were reviewer retrieves in `review_autofix` |
| Retrieve `fail_open: true` | **0** | None on retrieve ops |
| Retrieve `enabled: false` | **0** | None observed |
| Push retries >1 | 3 runs | `review_autofix` runs **27101252005** (2), **27105568462** (2), **27103488076** (3) |
| Hard push failure | 1 run | Plan run **27102733145** failed after **5** attempts |
| Force-tick fail-open | observed | Implement run **27099766039** logged `force-tick-get` / `force-tick-put` fail-open warnings |

Key findings:

- **Reviewer memory retrieval is mostly not working as a useful recall mechanism.** Sampled `review_autofix` retrieves in runs **27088151566**, **27091099675**, **27094428767**, **27101252005**, **27103364266**, **27103488076**, **27105568462**, and **27110905305** all selected **0 records**, all with `keyword_method: "llm"`.

- **Implementation memory can be useful.** Run **27099766039** retrieved **1** implementation record with `estimated_tokens: 28` and `keyword_method: "plain"`.

- **The immediate risk is persistence contention, not retrieval budget.** The only hard memory failure in the sample was the plan-phase push collision in run **27102733145**. Retrieval payloads were tiny.

- **Recommended next actions:**
  1. harden memory branch writes first,
  2. then improve reviewer retrieval selection (likely simpler keys / more directly relevant reviewer summaries),
  3. add a retrieval `budget` field so “estimated_tokens vs budget” can actually be audited.

## GH API Call Audit

**Current read:** trusted logs show **redundancy risk**, not an active GitHub rate-limit incident. I did **not** find a trustworthy GH `429` / secondary-rate-limit event in the sampled runtime logs. That matches the repo’s own API hygiene rule in **`CLAUDE.md` §15**, which says to prefer batched GraphQL and cycle-local caches over repeated per-item `gh api` calls.

1. **Plan step is re-reading issue state and churning comments**
   - **Evidence:** plan run **27102733145**, step `Check and claim /answer command`, showed:
     - issue fetch: `gh_retry gh api "repos/.../issues/${ISSUE_NUMBER}"`  
     - paginated issue comments fetch  
     - paginated linked PR count lookup  
     - progress comment delete  
     - later another issue fetch for labels (`--jq '[.labels[].name]'`)  
     - multiple `gh issue edit` invocations  
     - final comment post
   - **Root cause:** same issue scope is being reloaded instead of reused from `ISSUE_META_FILE` / `ISSUE_COMMENTS_FILE`.
   - **Concrete change:** reuse the first issue JSON for labels/state, fold linked PR status into the first prefetch if possible, and update an existing progress comment instead of delete+recreate when payload is unchanged.
   - **Estimated call-count reduction:** roughly **2-4 logical API calls** per plan execution, plus lower contention risk.

2. **`review_autofix` preflight still fans out separate PR/comments/reviews/check-run reads**
   - **Evidence:** run **27094428767**, step `review_codex-agent`, logged:
     - PR payload fetch,
     - paginated issue comments fetch,
     - paginated PR reviews fetch,
     - check-run snapshot fetch for `commits/${HEAD_SHA}/check-runs?per_page=100`.
   - **Root cause:** data is collected per concern rather than as one reusable preflight bundle.
   - **Concrete change:** build one per-PR/head-SHA prefetch bundle, store it once on disk, and reuse it across reviewer/editor/resolver steps.
   - **Estimated call-count reduction:** about **2-3 logical calls** per review run; larger benefit when comments/reviews paginate.

3. **Review sweep does API work even when it immediately skips because a run is already active**
   - **Evidence:** `log_summary` for runs **27112579805** and **27111036521** shows a pull-snapshot API call followed by `AUTOFIX_SWEEP_SKIP ... reason=active_run`.
   - **Root cause:** active-run detection happens after enough state has already been fetched to decide not to dispatch.
   - **Concrete change:** cache active-run state first within the sweep, or narrow the candidate pull query before taking a broader snapshot.
   - **Estimated call-count reduction:** small, but essentially free.

4. **Do not optimize GH rate-limit backoff first**
   - **Evidence:** no trustworthy GH rate-limit hits were surfaced in the sampled runtime logs.
   - **Recommendation:** prioritize **deduplication and reuse** over more elaborate retry logic. The repo already has wrappers; the bigger issue is unnecessary call shape.

## Prompt Cache & Memory System

1. **Prompt-cache telemetry is effectively blind**
   - **Evidence:** repo summary over **124** telemetry-covered runs shows:
     - `cache_hit_rate = null`
     - `or_calls = 0`
     - `or_prompt_tokens = 0`
     - `or_completion_tokens = 0`
     - `or_total_tokens = 0`
     - `or_cache_write_tokens = 0`
     - `or_cache_read_tokens = 0`
   - **But:** trusted runtime logs in review runs **27110905305** and **27105568462** show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
   - **Implication:** prompt cache may be on, but the collector cannot currently prove hits, misses, writes, or reads.
   - **Recommendation:** fix telemetry emission/parsing before changing prompt shapes for cache reasons.

2. **Prompt growth is likely eroding cache value**
   - **Evidence:** `review_autofix` is the only family with `CONTEXT_BUDGET_WARN` events (**18 total**), and sampled warnings were emitted at **214,681-220,663 prompt tokens** in runs **27101252005** and **27110905305**.
   - **Inference:** with prompts already this large and volatile, stable prefix reuse is probably poor even if caching is functioning.
   - **Recommendation:** stabilize the static prefix:
     - keep rubric/system instructions fixed,
     - move volatile PR comments/check-run deltas later,
     - compress once and reuse across reviewer slots,
     - avoid repeating large shared context per reviewer.
   - **Estimated impact:** medium-high on both latency and token cost once telemetry is visible.

3. **Memory retrieval is low-yield for reviewer flows**
   - **Evidence:** sampled `review_autofix` reviewer retrieves were **8/8 misses**, all with `keyword_method: llm`; implement retrieve in run **27099766039** was a hit with `keyword_method: plain`.
   - **Recommendation:** use simpler, more deterministic retrieval keys for reviewer memory, and record reviewer-relevant summaries rather than generic task artifacts.

4. **Fail-open behavior is mostly healthy, but it needs one consolidated health counter**
   - **Evidence:** 
     - Implement run **27099766039** succeeded despite force-tick memory fail-open warnings.
     - Orchestrate poll run **27112574469** succeeded with `SEMBLE_ENABLED: true` but `SEMBLE_AVAILABLE: false`.
     - Repo `break_glass_count` is **0**.
   - **Recommendation:** keep fail-open semantics, but add a single per-run `fail_open_count` / `subsystem_health` summary so optional-system degradation is visible without turning into a hard blocker.

**Important note:** the observed `setup-uv` cache hits in runs like **27099766039**, **27110432396**, and **27110728522** are **dependency/infrastructure caches**, not prompt-cache hits.

## Orchestrator Health

1. **The system is failing open in the right places, but persistence health is noisy**
   - **Evidence:** implement run **27099766039** completed successfully despite `force-tick-get` / `force-tick-put` memory failures; orchestrate poll run **27112574469** completed with `SEMBLE_AVAILABLE: false`.
   - **Assessment:** availability-first behavior is working.
   - **Smallest safe mitigation:** keep fail-open behavior, but alert only on sustained non-zero fail-open counts.

2. **Active-run de-duplication looks healthy**
   - **Evidence:** review sweep runs **27112579805** and **27111036521** skipped dispatch because PR `#3191` already had an active run.
   - **Assessment:** this is good orchestration hygiene, not a stuck state.
   - **Smallest safe mitigation:** retain it, but avoid paying for broader prefetch work before the skip decision.

3. **Poller health is acceptable, but queue delay is a recurring operational tax**
   - **Evidence:** `orchestrate_poll` had **44 successes**, **2 cancellations**, completed-run **p50 162.5s**, and multiple recent runs where runner wait dominated startup.
   - **Assessment:** the poller is not failing; it is paying avoidable scheduling overhead.
   - **Smallest safe mitigation:** track queue wait separately from poll work and reduce unnecessary poll starts.

4. **Phase visibility is weaker than phase health**
   - **Evidence:** the collector classifies many runs as `other`:
     - `clarify`: **188/198**
     - `plan`: **177/188**
     - `implement`: **176/188**
     - `orchestrate_clarify_respond`: **189/189**
   - **Assessment:** **Inference:** many of these are healthy skips/gates, but current telemetry cannot distinguish “intentionally skipped” from “did not meaningfully progress.”
   - **Smallest safe mitigation:** emit explicit `skip_reason` / `gated_by` telemetry so operators can tell healthy deferrals from missed work.

**Indicators to track weekly**
- CI success rate by family
- `context_budget_warn_count` by run/family
- reviewer retryable-failure count by model slot
- AI memory `push_attempts` and hard failures
- force-tick fail-open count
- poller queue/startup seconds
- active-run sweep skips

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact across the pipeline.

1. **CI gate after upstream AI work**
   - **Where:** review/implement outputs eventually rely on CI.
   - **Evidence:** **21/21** CI failures; **20/21** in `Orchestrate poll process unit tests`.
   - **Type:** retry / validation bottleneck.
   - **Fix first:** repair the two broken contracts before optimizing anything else.

2. **`review_autofix` compute tail**
   - **Where:** review / autofix stage.
   - **Evidence:** family completed-run **p95 2,887s**; run **27110905305** at **3,940s / 115.3M tokens** and run **27101252005** at **2,946s / 49.4M tokens**.
   - **Type:** compute + context expansion bottleneck.
   - **Fix:** conditional fan-out reduction, reasoning step-down, prompt compression after first context warning.

3. **`implement` compute tail**
   - **Where:** implement stage.
   - **Evidence:** run **27099766039** at **3,937s / 44.6M tokens / 34 calls** with `xhigh` reasoning.
   - **Type:** compute bottleneck.
   - **Fix:** gate `xhigh` and escalate only when needed.

4. **Review-stage wait/retry overhead**
   - **Where:** review/autofix before and during reviewer execution.
   - **Evidence:** 
     - run **27105568462** hit a **300s** check-run wait timeout,
     - run **27110905305** retried `minimax/minimax-m2.5` three times and `qwen/qwen3.6-plus` twice before full success.
   - **Type:** wait + retry bottleneck.
   - **Fix:** shorten wait budget; tighten per-slot retry budget.

5. **Orchestrator queue/startup overhead**
   - **Where:** orchestrate poll stage.
   - **Evidence:** recent poll runs show queueing dominating short runs.
   - **Type:** queueing bottleneck.
   - **Fix:** coalesce polls / early no-op exit.

6. **Validation maintenance drag**
   - **Where:** validate stage.
   - **Evidence:** validate run **27110728522** shows cache-save drift and pending cache action deprecation.
   - **Type:** setup/config bottleneck.
   - **Fix:** repair cache path and action version before it becomes a hard stop.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Broken CI gate: **21/21 failures**, mostly `Orchestrate poll process unit tests`.
- `review_autofix` tail latency and token blowups: **164.8M tokens**, completed-run **p95 2,887s**.
- `implement` heavy outlier behavior: run **27099766039** at **3,937s / 44.6M tokens**.
- `orchestrate_poll` queue/startup overhead on otherwise short runs.

**Top failure modes**
- Recurring CI regression in orchestrate-poll dependency-gate behavior.
- AI memory branch contention (`ai-memory` push conflicts) causing at least one hard plan failure.
- Retry-heavy reviewer slots under provider rate limits.

**Highest-cost drivers**
- `review_autofix`: **164,794,978 tokens**, **93 Codex calls**, **18 context warnings**.
- `implement`: **44,577,959 tokens**, **34 Codex calls**.
- Semble cost is comparatively small and mostly justified in review; Serena is inactive.

**Top 3 prioritized actions**
1. **Fix the CI regressions** (`test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` and timeout-prelude staging contract).
2. **Add an oversized-context fallback mode to `review_autofix`**: fewer reviewers, compressed shared context, lower reasoning on secondary passes.
3. **Harden AI memory writes and GH API reuse together**: memory push backoff/jitter + reuse plan/review prefetch data instead of reloading issue/PR state.

## Metrics Appendix

### Repo window summary

| Repo | Total runs | Success | Failure | Cancelled | Other | Overall p50 s | Overall p95 s | Completed-run p50 s | Completed-run p95 s | Codex tokens | Codex calls | `cache_hit_rate` | `break_glass_count` | `context_budget_warn_count` | Reported `wall_clock_p50_ms` | Reported `wall_clock_p99_ms` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 234 | 24 | 12 | 730 | 1.0 | 1680.0 | 140.5 | 2730.1 | 209,374,964 | 130 | null | 0 | 18 | 1,000 | 3,877,150 |

### Workflow family summary

| Family | Runs | S / F / C / O | Completed-run p50 / p95 s | Telemetry runs | Codex tokens | Codex calls | Semble q / bytes / fb | `context_budget_warn_count` | `break_glass_count` | Median telemetry `wall_clock_p50_ms` / `wall_clock_p99_ms` |
|---|---:|---|---|---:|---:|---:|---|---:|---:|---|
| `ci` | 21 | 0 / 21 / 0 / 0 | 1650 / 1725 | 14 | 0 | 0 | 0 / 0 / 75 | 0 | 0 | 1,651,500 / 1,651,500 |
| `review_autofix` | 103 | 93 / 0 / 10 / 0 | 71 / 2887 | 12 | 164,794,978 | 93 | 11 / 149,536 / 0 | 18 | 0 | 2,867,000 / 2,867,000 |
| `implement` | 188 | 10 / 2 / 0 / 176 | 622 / 2621 | 22 | 44,577,959 | 34 | 7 / 49,385 / 0 | 0 | 0 | 3,937,000 / 3,937,000* |
| `orchestrate_poll` | 46 | 44 / 0 / 2 / 0 | 162.5 / 532.3 | 4 | 2,026 | 1 | 1 / 4,143 / 0 | 0 | 0 | 179,000 / 179,000 |
| `plan` | 188 | 10 / 1 / 0 / 177 | 482 / 578 | 22 | 1 | 2 | 0 / 0 / 0 | 0 | 0 | 103,000 / 103,000* |
| `validate` | 1 | 1 / 0 / 0 / 0 | 278 / 278 | 1 | 0 | 0 | 0 / 0 / 0 | 0 | 0 | 278,000 / 278,000 |

\* telemetry coverage is sparse here; the wall-clock median is effectively driven by a single parsed outlier.

### Prompt cache / OpenRouter telemetry

| Scope | Telemetry-covered runs | `cache_hit_rate` | `or_calls` | `or_total_tokens` | `or_cache_write_tokens` | `or_cache_read_tokens` |
|---|---:|---|---:|---:|---:|---:|
| Repo total | 124 | null | 0 | 0 | 0 | 0 |
| `review_autofix` | 12 | null | 0 | 0 | 0 | 0 |
| `implement` | 22 | null | 0 | 0 | 0 | 0 |
| `plan` | 22 | null | 0 | 0 | 0 | 0 |
| `orchestrate_poll` | 4 | null | 0 | 0 | 0 | 0 |
| `validate` | 1 | null | 0 | 0 | 0 | 0 |

### GH API call summaries

Counts below are **observed logical calls in trusted logs**, not exact underlying HTTP request counts; `--paginate` may expand them.

| Run | Workflow / step | Observed GH API work | Hotspot / redundancy |
|---|---|---|---|
| **27102733145** | `plan` / `Check and claim /answer command` | issue GET, paginated issue-comments GET, paginated linked-PR lookup, progress-comment DELETE, later issue GET for labels, final comment POST, plus 3 `gh issue edit` calls | Re-reads issue scope instead of reusing `ISSUE_META_FILE`; comment churn could be reduced |
| **27094428767** | `review_autofix` / `review_codex-agent` | PR payload GET, paginated issue-comments GET, paginated reviews GET, check-runs snapshot GET | Good candidate for one reusable per-PR prefetch bundle |
| **27112579805** | `review_autofix` sweep | paginated pulls snapshot, then active-run skip | Snapshot work done before no-op skip |
| **27111036521** | `review_autofix` sweep | pull/run dispatch state check before active-run skip | Small but easy no-op reduction |

### MCP / Semble / Serena summary

| Scope | Semble queries | Semble bytes | Avg bytes/query | Semble fallbacks | Serena queries | Serena response bytes | Serena tool calls | Serena fallbacks | Serena probes ok / failed / skipped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Repo total | 19 | 203,064 | 10,688 | 75 | 0 | 0 | 0 | 0 | 0 / 0 / 0 |
| `review_autofix` | 11 | 149,536 | 13,594 | 0 | 0 | 0 | 0 | 0 | 0 / 0 / 0 |
| `implement` | 7 | 49,385 | 7,055 | 0 | 0 | 0 | 0 | 0 | 0 / 0 / 0 |
| `orchestrate_poll` | 1 | 4,143 | 4,143 | 0 | 0 | 0 | 0 | 0 | 0 / 0 / 0 |
| `ci` | 0 | 0 | — | 75 | 0 | 0 | 0 | 0 | 0 / 0 / 0 |

**Collector vs trusted-log note:** collector totals include duplicate step-log emissions. In trusted deep-dive runtime dedupe, I saw **17 unique `SEMBLE_QUERY`** lines and **70 unique `SEMBLE_FALLBACK`** lines; the delta comes from duplicated logging in runs such as **27110905305** and **27110411091**.

### Trusted deep-dive Semble target breakdown

| Family / target | Unique runtime queries | Logged bytes | Avg ms | Reading |
|---|---:|---:|---:|---|
| `review_autofix` / `reviewer-context` | 7 | 108,225 | 528.6 | Looks useful and cheap |
| `review_autofix` / `overflow` | 2 | 14,963 | 511.5 | Mixed value |
| `review_autofix` / `conflict-resolver-context` | 1 | 10,613 | 475.0 | Likely useful |
| `implement` / `overflow` | 7 | 49,385 | 507.3 | Likely noisy in current form |

### MCP availability rows

| System | Target | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---|---:|---:|---:|
| Serena | none observed | 0 | 0 | 0 |

**Other MCP servers observed:** none.

### AI memory summary

| Metric | Value |
|---|---:|
| Unique trusted `AI_MEMORY_TELEMETRY` events | 37 |
| Unique `retrieve` events | 9 |
| Retrieve hit rate | 11.1% |
| Avg retrieve `estimated_tokens` | 3.1 |
| Reviewer retrieve misses | 8 / 8 sampled review retrieves |
| Push retries >1 | 3 runs |
| Hard memory-push failure | 1 run (`27102733145`) |
| Force-tick fail-open observed | yes (`27099766039`) |

