## Executive Summary

- **The biggest end-to-end blocker is the release chain waiting on long child workflows, not core CI compute.** `test_and_mark_stable` has a **66.7% failure rate** in this window (2/3 runs), with failures at run **25200104592** after **5,361s** and run **25204168842** after **4,281s** while waiting on downstream analysis/review phases. The strongest fix is to replace long watch loops with explicit child phase/result signaling and split oversized audit passes. **Estimated impact:** 45–90 minutes faster failed release cycles, large rerun reduction. **Confidence:** high.

- **`review_autofix` is the dominant cost+latency hotspot, especially on Claude-branch comment-only runs.** Family metrics show **p95 1,902.7s** with **93 cancelled** runs out of **143** total. Successful runs still took **1,230s** (run **25206256213**) and many other slow runs between **1,418s–1,846s**, even when logs said `editor/commit/judge/auto-merge skipped`. **Estimated impact:** 40–70% token/cost reduction and 15–25 minutes saved on affected runs by reducing reviewer fanout/two-pass/xhigh use on comment-only paths. **Confidence:** high.

- **CI is slow and fails late for cheap deterministic errors.** `ci` has **p50 611s / p95 645.6s**, while failed runs **25200848815** and **25155077424** ended on a simple Ruff `E101` after ~10 minutes, and run **25156894907** failed on a deterministic checkout classification assertion after most tests had already run. **Estimated impact:** 6–9 minutes faster failure feedback for lint/self-test regressions. **Confidence:** high.

- **GitHub API usage is structurally redundant in watcher flows and PR-link lookups.** Deep-dive logs show repeated `actions/runs/<id>` polling every ~15s for over 30 minutes in `orphan-workflows-test`, plus repeated linked-issue GraphQL lookups and artifact enumerations across families. **Estimated impact:** large API-call reduction in release/watcher paths and lower rate-limit exposure. **Confidence:** high.

- **AI memory is on, but reviewer retrieval is mostly ineffective; prompt cache is enabled but not measurable.** Deep-audit logs report only **1 useful retrieve out of 6** (16.7% hit rate in that sample), with reviewer retrieves mostly `keyword_method="none"` and `records_selected=0`. Prompt-cache instrumentation is present (`OPENROUTER_PROMPT_CACHE_DISABLED=false`) but sampled runs do not emit usable hit/miss or token totals. **Estimated impact:** medium cost/latency savings once retrieval targeting and cache observability are fixed. **Confidence:** high.

- **Serena is helping, but adoption is still below target and bootstrap/reporting is inconsistent.** In review run **25206256213**, the Serena report showed **604 Serena calls vs 726 file-based fallback ops**, only **45% efficiency**, below the configured **50%** threshold. **Estimated impact:** medium token/turnaround savings from reducing fallback reads and delaying Serena startup on paths that short-circuit before code work. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Replace long child-workflow watch loops with explicit child status artifacts/outputs**
   - **Critical-path win**
   - **Evidence:** In `test_and_mark_stable` run **25204168842**, job `e2e-smoke-test`, step `Phase 4: Wait for review & autofix to complete`, the log shows a 30-minute inactivity window with repeated lines like `... idle 0s ... log: 0b` and `Review run #...` polling while no useful progress signal appeared. In the same run, `orphan-workflows-test` polled `workflow-log-analysis` every ~15s from `05:51:30Z` to `06:44:07Z` with `status=in_progress conclusion=`. Parent run then failed after **4,281s**. Similar parent/child failure happened in run **25200104592** after **5,361s**.
   - **Root cause:** Parent workflows infer child health by repeatedly polling run status instead of consuming explicit phase/result outputs. A child can be “in_progress” for a long time with no actionable state change.
   - **Exact change:**  
     - Have long child workflows (`review_autofix`, `workflow_log_analysis`) publish explicit compact outputs/artifacts such as `phase=current`, `result=ok|failed|soft_failed|stalled`, `last_progress_ts`, `reason`.
     - Make parent wait steps fail fast on these outputs instead of waiting for full workflow completion.
     - Increase polling interval after first successful registration and while status is stable.
   - **Estimated time savings:** **15–45 minutes** on stuck release chains; **45–90 minutes** on failure cases that currently idle until timeout.
   - **Implementation risk:** **Medium**. Safe if parent logic falls back to current polling when outputs are absent.

2. **Thin the Claude-branch comment-only `review_autofix` path**
   - **Critical-path win**
   - **Evidence:** `review_autofix` family p95 is **1,902.7s**. Slow successes include **25201255563 (1,846s)**, **25202909051 (1,696s)**, **25203362359 (1,466s)**, **25203375473 (1,430s)**, and **25201144027 (1,418s)**. Recent run **25206256213** still took **1,230s** while logs said `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.` The same run also logged `ENABLE_REVIEWER_TWO_PASS: true`, `REVIEWER_REASONING_EFFORT: xhigh`, and multi-pass summarization.
   - **Root cause:** The “comment-only” path still performs heavyweight reviewer orchestration designed for edit-capable flows.
   - **Exact change:** For `claude_branch_review=true` / comment-only paths:
     - run **one reviewer panel pass** instead of two;
     - reduce reviewer set to 1–2 models;
     - drop reviewer reasoning from `xhigh` to `high` unless diff-risk heuristics trigger escalation;
     - skip symbol-diff / Serena efficiency generation unless code-editing or multi-file review is required.
   - **Estimated time savings:** **10–25 minutes** per slow comment-only run.
   - **Implementation risk:** **Low to medium**. Safe if escalated mode remains for risky diffs.

3. **Split `workflow_log_analysis` `api-redundancy` into deterministic pre-pass + final synthesis**
   - **Critical-path win**
   - **Evidence:** `workflow_log_analysis` family averages **4,562.5s**. Slow run **25200117236** was cancelled after ~**5,264s**; successful run **25204185528** still took **3,861s**. Deep-audit logs explicitly note prior timeout increases (30→45→60→90 minutes) and still describe structural slowness.
   - **Root cause:** One monolithic Codex-heavy analysis stage is doing both discovery and writing over a wide repository/log surface.
   - **Exact change:**  
     - Stage 1: deterministic local extractor for candidate hotspots, endpoint counts, and repeated-call patterns.  
     - Stage 2: smaller model synthesis over the extracted summary only.  
     - Reserve highest reasoning only for final section generation when necessary.
   - **Estimated time savings:** **30–50 minutes** on the analysis child run; major release-cycle savings because the parent currently blocks on it.
   - **Implementation risk:** **Medium**. Keep markdown contract unchanged.

4. **Fail CI early on Ruff and workflow classification before the long unit-test matrix**
   - **Critical-path win for developer feedback**
   - **Evidence:** `ci` p50 is **611s**. Run **25200848815** failed on `Python lint (ruff)` with `E101 Indentation contains mixed spaces and tabs` only near the end of a ~**629s** run. Run **25156894907** failed after most tests with `AssertionError: Unclassified checkout@v5 workflows: ['promote-main-to-stable.yml']`.
   - **Root cause:** Cheap deterministic gates are scheduled after more expensive suites.
   - **Exact change:** Reorder CI so the first stage is:
     1. Ruff / shellcheck / YAML/actionlint
     2. workflow classification/self-test contracts
     3. broader Python/unit/coverage suites
   - **Estimated time savings:** **6–9 minutes** faster failure feedback on lint/contract regressions.
   - **Implementation risk:** **Low**.

5. **Delay Serena/bootstrap until after cheap gates confirm code work is needed**
   - **Local optimization**
   - **Evidence:** In run **25206256213**, steps before model work included workflow support checkout, dependency install, Serena setup, runtime workspace init, memory setup, diff prep, and reporting. Yet recent `review_autofix` run **25206770317** completed in **17s** after `No linked issues found for merged PR #1854`, with no code-inspection work needed. Deep audit also notes bootstrap overhead on runs that short-circuit.
   - **Root cause:** Code-analysis tooling starts before the workflow knows whether it needs to inspect or edit code.
   - **Exact change:** Move Serena install/setup and support-source staging behind the last cheap gate:
     - linked-issue presence,
     - deterministic docs-only skip,
     - comment-only/no-op branch.
   - **Estimated time savings:** **15–90s** on no-op/short-circuit runs; reduced queue contention.
   - **Implementation risk:** **Low**.

6. **Reduce broad Git fetches on short poller/merge workflows**
   - **Local optimization**
   - **Evidence:** `orchestrate_poll` run **25205953103** used `actions/checkout@v5` with `fetch-depth: 0`; log summary says checkout alone took about **9s** of a **47s** run. `promote_main_to_stable` and `forward_merge_stable_to_main` also show full-history/tag-oriented fetches on very short flows.
   - **Root cause:** Full-history checkout is used even when only branch-tip or tag delta checks are needed.
   - **Exact change:** Use shallow fetch by default, then targeted `git fetch origin stable main --depth=...` or tags-only fetch only where version/tag resolution requires it.
   - **Estimated time savings:** **5–10s** on poller/merge/promote flows.
   - **Implementation risk:** **Low**, if tag-resolution steps explicitly deepen when needed.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Downshift reviewer fanout, passes, and reasoning on comment-only review flows**
   - **Evidence:** Long comment-only `review_autofix` runs still show `ENABLE_REVIEWER_TWO_PASS=true`, `REVIEWER_REASONING_EFFORT: xhigh`, and reviewer summarization over multiple inputs in run **25206256213**. Family p95 is **1,902.7s** with many long successes.
   - **Root cause:** High-cost review settings are applied even when no edit/judge/merge actions will occur.
   - **Exact change:** For comment-only paths:
     - use 1 pass;
     - 1–2 reviewer models max;
     - `high` reasoning by default, escalate only on risk heuristics.
   - **Estimated savings:** **40–70%** token/dollar reduction on affected review runs.
   - **Quality-risk notes:** Low if escalation remains available for risky diffs or failed CI context.

2. **Stop re-expanding large prompt/context prefixes across retries and phase restarts**
   - **Evidence:** Deep-audit logs explicitly call out repeated prompt/cache instrumentation and repeated prompt file exports in retry-heavy flows; run **25206256213** includes `Pre-assemble static context cacheable across runs`, but token fields remain unmeasured and retry loops still rebuild large contexts.
   - **Root cause:** Stable prompt prefixes are not consistently separated from dynamic tail content.
   - **Exact change:**  
     - keep instructions/static repo context in a cached prefix file;
     - pass only diff/risk/retry delta in retry attempts;
     - avoid re-emitting unchanged support context once persisted locally.
   - **Estimated savings:** **Medium to high** on implement/review retries; likely significant but not precisely quantifiable from current telemetry.
   - **Quality-risk notes:** Low if retry prompts include a concise summary of prior failure cause.

3. **Make prompt-cache hit/miss measurable before further prompt-cache tuning**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears repeatedly, but run **25206256213** `openrouter usage` lines show `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. Deep-audit logs repeatedly state prompt cache is enabled but not auditable.
   - **Root cause:** Cache instrumentation is enabled but not emitting usable numeric telemetry.
   - **Exact change:** Emit per-LLM call:
     - prompt/completion/total tokens,
     - cache create/read tokens,
     - cache hit/miss/partial-hit state,
     - stable-prefix hash.
   - **Estimated savings:** Not directly quantifiable yet; prerequisite for reliable cost control.
   - **Quality-risk notes:** None; observability-only change.

4. **Reduce wasted spend from cancelled `review_autofix` runs**
   - **Evidence:** `review_autofix` shows **93 cancelled** runs in family metrics, and the deeper audit reports **106 cancelled** in a larger sampled set. Cancelled runs still burned substantial time: examples include **25206137008 (316s)** and several deep-audit examples in the **243s–417s** range.
   - **Root cause:** Cancellation happens after jobs have already queued, started, and sometimes reached Codex-agent work.
   - **Exact change:**  
     - strengthen concurrency groups and cancel-in-progress behavior at workflow start;
     - add an early superseded-run check before dependency install / Serena setup / prompt generation.
   - **Estimated savings:** **High runner + token savings** on active PRs.
   - **Quality-risk notes:** Low if the newest run is always preserved.

5. **Use smaller/cheaper discovery passes in `workflow_log_analysis`**
   - **Evidence:** Deep-audit logs show `WORKFLOW_EDITOR_MODEL: openai/gpt-5.4` with `THINKING_LEVEL_ANALYSIS: xhigh` for API-redundancy work that later times out or overruns.
   - **Root cause:** High-end reasoning is used too early in an analysis pipeline that first needs filtering and counting.
   - **Exact change:** Deterministic or cheap-model discovery first; reserve strongest model only for final narrative synthesis.
   - **Estimated savings:** **Medium to high** on long analysis runs.
   - **Quality-risk notes:** Low if final synthesis keeps the same output contract.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Convert long watcher polling into explicit fail-fast child-state propagation**
   - **Failure evidence:** `test_and_mark_stable` failed in run **25200104592** at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` after **5,361s**, and again in run **25204168842** at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` after **4,281s**.
   - **Root cause category:** Orchestration/watcher design.
   - **Exact fix:** Child workflows should write phase/result artifacts or outputs; parent should consume them and terminate early on `failed`, `stalled`, or `soft_failed`.
   - **Expected reliability impact:** **High** reduction in false timeouts and parent-child desynchronization failures.
   - **Rollback/fail-open considerations:** Fall back to current polling if child outputs are unavailable.

2. **Move deterministic CI/style/contract checks to the front**
   - **Failure evidence:** Run **25200848815** failed on Ruff `E101`; run **25156894907** failed on a deterministic self-test assertion; both failures surfaced late in ~10-minute CI runs.
   - **Root cause category:** Late surfacing of deterministic failures.
   - **Exact fix:** Reorder CI into cheap deterministic guardrails first, expensive test suites second.
   - **Expected reliability impact:** **Medium** failure-rate unchanged, but rerun waste and diagnosis time drop sharply.
   - **Rollback/fail-open considerations:** None; purely reordering.

3. **Fix validation self-test fixture drift before nightly publishing**
   - **Failure evidence:** `nightly_validation_selftest` run **25200681719** failed with `fixtures=3 passed=1 failed=2`.
   - **Root cause category:** Fixture/expected-output drift.
   - **Exact fix:** Gate commits to self-test fixtures through the same validation logic used in nightly; publish failing fixture names into the summary artifact for immediate diagnosis.
   - **Expected reliability impact:** **Medium** reduction in nightly failure noise.
   - **Rollback/fail-open considerations:** If fixture names cannot be emitted, at least preserve current summary JSON behavior.

4. **Harden workflow classification/self-test coverage for renamed or added workflows**
   - **Failure evidence:** Run **25156894907** failed with `AssertionError: Unclassified checkout@v5 workflows: ['promote-main-to-stable.yml']`.
   - **Root cause category:** Contract drift between workflow inventory and self-test expectations.
   - **Exact fix:** Generate the classification inventory from the workflow tree automatically, or fail PRs that add workflow files without updating the classification manifest.
   - **Expected reliability impact:** **Medium** reduction in avoidable CI failures.
   - **Rollback/fail-open considerations:** Keep a manual override list only for transitional renames.

5. **Harden support-source/bootstrap preflight before AI implementation/review phases**
   - **Failure evidence:** Deep-audit notes failed implement run **25151206656** had support-source checkout/staging issues and still progressed deep into the AI path.
   - **Root cause category:** Bootstrap dependency failure.
   - **Exact fix:** Validate support-source ref, prompts, and helper scripts before launching Codex/Serena/model steps; exit early with a bootstrap-specific error.
   - **Expected reliability impact:** **Medium** reduction in wasted failed AI runs.
   - **Rollback/fail-open considerations:** Fail closed for missing mandatory support assets; fail open only for nonessential reporting helpers.

6. **Strengthen cancellation-before-start for superseded `review_autofix` runs**
   - **Failure evidence:** Many `review_autofix` runs are cancelled after spending minutes in queue or execution; recent cancelled run **25206137008** still consumed **316s**.
   - **Root cause category:** Concurrency/cancellation race.
   - **Exact fix:** Early supersession check at workflow start plus tighter concurrency grouping by PR/head SHA before any expensive initialization.
   - **Expected reliability impact:** **Medium** reduction in reruns and noisy cancellations.
   - **Rollback/fail-open considerations:** Ensure newest run always survives; if concurrency metadata is missing, continue current behavior.

## AI Memory Health

- **Telemetry observed:** yes, in deep-dive logs from `review_autofix`, `implement`, and `memory_maintenance`.
- **Best evidence-grade aggregate:** the sampled `workflow_log_analysis` deep-audit for run **25204185528** reports **25 AI_MEMORY_TELEMETRY events** and **6 retrieve operations**, with **1 hit / 6 retrieves = 16.7% hit rate**.
- **Average `estimated_tokens`:** deep-audit reports **4.7** average; the one useful hit was **28 tokens**.
- **Keyword method distribution:** **`plain`: 1**, **`none`: 5**, **`llm`: 0** in the deep-audit sample.
- **Zero-record retrieves:** **5/6** in the deep-audit sample. Recent run **25206256213** also logged `{"op":"retrieve","role":"reviewer","records_selected":0,"estimated_tokens":0,"keyword_method":"none","enabled":true}`.
- **`fail_open: true` entries:** deep-audit reports **0** for sampled retrieve events.
- **`enabled: false` entries:** not observed on sampled retrieves.
- **High push retry counts:** not observed; sampled `record-run-event` / `record-candidate` telemetry consistently showed `push_attempts: 1`.
- **Compaction health:** `memory_maintenance` run **25205873529** succeeded with `AI_MEMORY_TELEMETRY: {"op":"compact","archived_candidates":2914,"did_push":true,"ok":true,"month":"2026-04"}`.

**Assessment:** memory is operational, but reviewer retrieval is usually irrelevant. Most retrievals return nothing because keyword generation is effectively absent (`keyword_method="none"`), so memory adds little value today.

**Recommendation:** limit memory retrieval to flows with clear issue/PR/problem signatures, and generate deterministic reviewer keywords from PR title, failing check names, touched paths, and issue labels before retrieval.

## GH API Call Audit

### High-volume / high-redundancy patterns

1. **Repeated `actions/runs/<id>` polling in watcher loops**
   - **Evidence:** In `test_and_mark_stable` run **25204168842**, `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` polled the child run roughly every **15s** from `05:51:30Z` through `06:44:07Z`, repeatedly printing `status=in_progress conclusion=`. The same release run also polled review/autofix status with long idle windows.
   - **Root cause:** Each watcher implements bespoke “dispatch + register + poll” behavior with no cycle-local cache and no adaptive backoff once state is stable.
   - **Concrete change:** Centralize into one helper that:
     - caches run metadata locally for the cycle,
     - uses fast registration polling only until the child is visible,
     - slows to wider intervals after sustained `in_progress`,
     - exits on explicit child result artifacts.
   - **Estimated call-count reduction:** likely **60–80%** in long watch loops.
   - **Rate-limit risk reduction:** high on release/test workflows.

2. **Repeated linked-issue / closing-issue GraphQL lookups across families**
   - **Evidence:** Deep-audit logs report `closingIssuesReferences` in **21 log files** across `review_autofix`, `issue_pr_status`, and implement-related flows. Recent runs **25206770317** and **25206770324** both use PR-linked issue lookups for small/no-op decisions.
   - **Root cause:** Similar linked-issue discovery logic is reimplemented in multiple workflows instead of being fetched once and reused.
   - **Concrete change:** Create one shared helper returning:
     - linked issue numbers,
     - closing issue references,
     - cached PR state for the workflow cycle.
   - **Estimated call-count reduction:** **2–5 calls → 1** per workflow invocation on common no-op paths.
   - **Rate-limit risk reduction:** medium.

3. **Artifact enumeration is repeated across reporting/cleanup flows**
   - **Evidence:** Deep-audit logs report `actions/runs/<id>/artifacts` access in **22 files / ~40 occurrences**, including `copilot_pull_request_reviewer`, `nightly_validation_selftest`, and release/reporting flows. Recent run **25206137963** also called `/actions/runs/25206137963/artifacts`.
   - **Root cause:** Cleanup and reporting steps enumerate artifacts independently instead of passing known artifact IDs/metadata downstream.
   - **Concrete change:** Persist artifact IDs as job outputs and reuse them; only enumerate when the ID is unknown.
   - **Estimated call-count reduction:** **1–2 calls per downstream step** that currently re-lists artifacts.
   - **Rate-limit risk reduction:** low to medium.

4. **No-op cancel/status workflows still invoke API logic after runner startup**
   - **Evidence:** `cancel_on_pr_close` runs **25206770321** and **25204430046** finished in **7s** and **6s**, but both still waited on runners and performed branch/run scans. `issue_pr_status` also starts a runner for short/no-op cases.
   - **Root cause:** Triggering is broad relative to actual work needed.
   - **Concrete change:** Add earlier event filters and minimal pre-run guards so workflows don’t start when branch/PR state makes work impossible.
   - **Estimated call-count reduction:** modest per run, but meaningful over high-frequency no-op traffic.
   - **Rate-limit risk reduction:** low, queue reduction more important.

### Hygiene against repo rules

- **Batching:** inconsistent. There are some GraphQL patterns, but watcher and linked-issue logic still duplicate calls.
- **Cycle-local caching:** weak in watcher paths, better in some PR metadata flows.
- **Fail-open behavior:** generally reasonable; recent `issue_pr_status` and memory-related paths already prefer conservative fail-open behavior.

## MCP & Serena Efficiency

- **Observed best evidence:** `review_autofix` run **25206256213**, step `Generate Serena efficiency report`, reported:
  - **Serena tool calls:** **604**
  - **File-based fallback ops:** **726**
  - **Serena efficiency:** **45%**
  - **Estimated tokens with Serena:** **~403,160**
  - **Estimated tokens without Serena:** **~734,800**
  - **Estimated savings:** **~331,640 tokens (45%)**
  - **Top tools:** `replace_symbol_body` (90), `insert_after_symbol` (90), `get_symbols_overview` (80), `find_symbol` (80), `find_referencing_symbols` (72)
  - Warning emitted: `Serena MCP adoption is below threshold (726 file-based fallback ops vs 604 Serena tool calls; 45% efficiency below 50% threshold).`

### Findings

1. **Serena is valuable, but fallback file reads still dominate**
   - **Evidence:** Same run above shows **726 file-based fallback ops** exceeding **604 Serena calls**.
   - **Issue:** Workflows still fall back to broad file operations often enough to keep Serena below target.
   - **Concrete change:** In review/edit flows, push more operations through symbol-aware paths first:
     - `get_symbols_overview`
     - `find_symbol`
     - `find_referencing_symbols`
     - `search_for_pattern`
   - **Expected impact:** medium token and turnaround improvement.

2. **Instrumentation is inconsistent between raw stats and synthesized report**
   - **Evidence:** In run **25206256213**, step `Log token usage and Serena stats` said `No Serena tool usage stats found`, while the next step generated a full Serena efficiency report with concrete counts.
   - **Issue:** Raw stats availability and summarized report availability are out of sync.
   - **Concrete change:** Ensure the report generator and stats logger read from the same canonical artifact path and fail consistently.
   - **Expected impact:** medium observability improvement, low implementation risk.

3. **Serena/bootstrap work starts before no-op paths are fully ruled out**
   - **Evidence:** Recent short `review_autofix` post-merge/no-linked-issue runs like **25206770317** completed in **17s** with no visible need for Serena, while longer code-review runs pay setup cost earlier.
   - **Issue:** MCP/tool overhead is incurred before cheap gates finish.
   - **Concrete change:** Gate Serena startup behind:
     - linked-issue discovery,
     - deterministic skip checks,
     - “comment-only/no-op” detection.
   - **Expected impact:** small per-run savings, good aggregate value.

4. **Parallelizable reads are underused in watcher/reporting flows**
   - **Evidence:** Watchers and analysis stages repeatedly perform sequential fetch/poll/read operations over long windows.
   - **Concrete change:** Where safe, parallelize independent metadata fetches (PR metadata, failing check summaries, linked issues) before LLM/tool invocation.
   - **Expected impact:** modest latency reduction with low correctness risk.

## Prompt Cache & Memory System

1. **Prompt cache is enabled, but not auditable**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears throughout recent and slow runs, including **25206256213**. However, the `openrouter usage` lines in `Run reviewer models` showed all key numeric fields as `na` (`prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`).
   - **Assessment:** The system is configured for cache-friendly behavior, but actual cache effectiveness cannot be measured from this window.

2. **Prompt-cache fragmentation risk remains high**
   - **Evidence:** Run **25206256213** includes many dynamic files and context-generation stages (`reviewer_prompt_body.txt`, `reviewer_prompt.txt`, diff context, symbol diff summary, runtime capture) before model execution. Deep-audit logs also call out repeated prompt/context expansion across retries.
   - **Likely fragmentation causes:**
     - dynamic metadata prepended too early,
     - retry attempts rebuilding full prompt bodies,
     - per-run environmental noise mixed into the prefix.
   - **Concrete change:** keep the prefix stable and move dynamic items to the tail:
     1. repo/workflow instructions
     2. static support prompts
     3. dynamic diff/risk/check context
     4. retry delta only

3. **Memory retrieval quality is too low to justify broad reviewer use**
   - **Evidence:** Deep-audit sample hit rate is only **16.7%**; recent reviewer retrieve in **25206256213** returned zero records.
   - **Concrete change:** only attempt reviewer memory retrieval when there is a usable deterministic query seed:
     - PR title,
     - failed check names,
     - touched file clusters,
     - linked issue title/labels.
   - **Expected impact:** medium cost reduction and slightly better reviewer relevance.

4. **Memory write path looks healthy**
   - **Evidence:** Recent run **25206256213** recorded `record-run-event` at phase start/completion and `record-candidate` with `push_attempts: 1`. Memory maintenance compaction also succeeded cleanly in run **25205873529**.
   - **Assessment:** Write-path reliability is better than read-path usefulness.

## Orchestrator Health

- **Clarify/plan/implement/respond orchestration is mostly healthy mechanically, but very noisy in triggers.**
  - `clarify`: **134 total runs**, mostly skipped/other.
  - `plan`: **119 total runs**, mostly skipped/other.
  - `implement`: **119 total runs**, mostly skipped/other.
  - `orchestrate_clarify_respond`: **119 total runs**, only **2 successes**.
  - Recent samples repeatedly exit in **0–2s** after `if` evaluation.

### Recurring pain points

1. **Too many no-op orchestrator invocations**
   - **Evidence:** Many recent runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` completed in **0–2s** solely because `if` conditions evaluated false.
   - **Smallest safe mitigation:** tighten trigger conditions at the workflow/event level so fewer workflows start only to skip.
   - **Verification indicators:** no-op run count per day; ratio of skipped-to-successful orchestrator runs.

2. **State handoff between phases is still fragile**
   - **Evidence:** In the `e2e-smoke-test` failure path of **25204168842**, implement/review waits saw long windows with little or no observable progress (`step: ?, reviews: 0, log: 0b`), implying weak intermediate status visibility.
   - **Smallest safe mitigation:** publish compact orchestrator state artifacts on each phase transition.
   - **Verification indicators:** count of waits with `log: 0b`, count of parent timeouts while child is still `in_progress`.

3. **Wave/decompose state can become inconsistent**
   - **Evidence:** recent log summaries around runs **25204405099** and **25204485279** reference conflicting canary/decompose status in `tests/e2e_smoke_canary_b.txt` (`pending` vs `ok` / `decompose-run: 25204168842`), indicating state propagation lag.
   - **Smallest safe mitigation:** validate state-file freshness before phase transitions and reject stale wave metadata early.
   - **Verification indicators:** number of stale-state skip events; frequency of reissued plan/implement waves due to mismatched canary state.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Review/autofix compute dominates the AI path**
   - **Stage:** implement → review/autofix
   - **Evidence:** `review_autofix` p95 **1,902.7s**; many long successes on comment-only paths.
   - **Type:** compute + orchestration overhead
   - **Fix:** reduce reviewer fanout/passes/reasoning on non-edit paths.

2. **Parent-child waiting dominates release workflows**
   - **Stage:** review/autofix/analysis → release marking
   - **Evidence:** `test_and_mark_stable` p50 **4,281s**, failures at **4,281s** and **5,361s**; `workflow_log_analysis` itself runs **3,861s–5,264s**.
   - **Type:** retry/poll overhead
   - **Fix:** explicit child outputs, adaptive polling, fail-fast terminal states.

3. **CI blocks merges for ~10 minutes even when the eventual failure is trivial**
   - **Stage:** validate/CI
   - **Evidence:** `ci` p50 **611s**, p95 **645.6s**, with simple Ruff/contract failures surfacing late.
   - **Type:** compute ordering inefficiency
   - **Fix:** front-load deterministic checks.

4. **Queueing materially affects short workflows**
   - **Stage:** no-op/status/merge helpers
   - **Evidence:** `forward_merge_stable_to_main`, `promote_main_to_stable`, `cancel_on_pr_close`, `issue_pr_status`, and `orchestrate_poll` all logged runner waits despite total durations from **6s** to **47s**.
   - **Type:** queue overhead
   - **Fix:** suppress avoidable no-op runs and shrink setup on helper workflows.

5. **Poller/setup overhead is nontrivial relative to useful work**
   - **Stage:** orchestrate poll / short review gates
   - **Evidence:** `orchestrate_poll` run **25205953103** spent ~**9s** in checkout in a **47s** run; `plan` run **25204439538** spent **170s** with repeated git cleanup/config and runner wait.
   - **Type:** setup overhead
   - **Fix:** shallow fetches, reuse metadata, defer expensive setup until after gates.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long-tail runtime and cancellation waste
  - `test_and_mark_stable` waiting on long child workflows
  - `ci` late failure detection despite deterministic failure causes

- **Top failure modes**
  - parent release jobs timing out while child analysis/review stays `in_progress`
  - CI deterministic failures found late (`ruff`, workflow classification)
  - nightly validation fixture drift

- **Highest-cost drivers**
  - comment-only review flows still using two-pass, xhigh, multi-model review
  - monolithic `workflow_log_analysis` analysis pass
  - repeated watcher/API polling with little incremental signal

- **Top 3 prioritized actions**
  1. **Thin comment-only `review_autofix`**: 1 pass, fewer reviewers, lower default reasoning.
  2. **Replace watcher polling with explicit child outputs/artifacts** in `test_and_mark_stable` and child workflows.
  3. **Reorder CI** so Ruff/classification/contract checks run before heavier suites.

## Metrics Appendix

### Repository Summary

| Repo | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 852 | 297 | 7 | 96 | 452 | 0.82% | 186.6 | 2.0 | 645.0 |

### Workflow Family Highlights

| Workflow Family | Total Runs | Success | Failure | Cancelled | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| ci | 89 | 85 | 4 | 0 | 611.0 | 645.6 | Slow, deterministic failures found late |
| review_autofix | 143 | 50 | 0 | 93 | 36.0 | 1902.7 | Biggest latency/cost hotspot |
| test_and_mark_stable | 3 | 1 | 2 | 0 | 4281.0 | 5253.0 | Structurally blocked by child waits |
| workflow_log_analysis | 2 | 1 | 0 | 1 | 4562.5 | 5193.9 | Monolithic analysis pass too long |
| plan | 119 | 10 | 0 | 0 | 1.0 | 164.6 | Mostly no-op/skipped |
| implement | 119 | 15 | 0 | 2 | 1.0 | 208.7 | Mostly no-op/skipped |
| clarify | 134 | 12 | 0 | 0 | 1.0 | 93.3 | Mostly no-op/skipped |
| orchestrate_poll | 35 | 35 | 0 | 0 | 46.0 | 195.5 | Setup/checkout noticeable |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 102.0 | 102.0 | Fixture drift failure |

### Key Run Evidence

| Run ID | Workflow Family | Conclusion | Duration (s) | Key Failure / Observation |
|---|---|---:|---:|---|
| 25204168842 | test_and_mark_stable | failure | 4281 | `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` |
| 25200104592 | test_and_mark_stable | failure | 5361 | `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` |
| 25206256213 | review_autofix | success | 1230 | Comment-only Claude branch review still expensive |
| 25206256145 | ci | success | 618 | CI long but green; lint dominates |
| 25200848815 | ci | failure | 629 | Ruff `E101` after long run |
| 25156894907 | ci | failure | 499 | workflow classification assertion failure |
| 25200681719 | nightly_validation_selftest | failure | 102 | `fixtures=3 passed=1 failed=2` |
| 25204185528 | workflow_log_analysis | success | 3861 | Deep audit confirms cache/memory/API issues |
| 25205953103 | orchestrate_poll | success | 47 | checkout dominates short poller |
| 25205873529 | memory_maintenance | success | 29 | memory compaction succeeded |

### AI Memory Metrics

| Metric | Value | Evidence |
|---|---:|---|
| Telemetry events (deep-audit sample) | 25 | `workflow_log_analysis` deep audit |
| Retrieve operations | 6 | deep-audit sample |
| Retrieve hit rate | 16.7% | 1/6 retrieves selected records |
| Avg estimated tokens per retrieve | 4.7 | deep-audit sample |
| Keyword method distribution | `plain`: 1, `none`: 5, `llm`: 0 | deep-audit sample |
| Zero-record retrieves | 5 | deep-audit sample |
| `fail_open: true` retrieves | 0 | deep-audit sample |
| High push retry counts | not observed | sampled telemetry showed `push_attempts: 1` |

### Prompt Cache Metrics

| Metric | Value | Status |
|---|---:|---|
| Prompt cache enabled flag observed | Yes | `OPENROUTER_PROMPT_CACHE_DISABLED=false` |
| Numeric prompt token totals | N/A | not emitted in sampled logs |
| Numeric completion token totals | N/A | not emitted in sampled logs |
| Cache creation token totals | N/A | not emitted in sampled logs |
| Cache read token totals | N/A | not emitted in sampled logs |
| Observable cache hit/miss rate | N/A | instrumentation incomplete |

### Serena / MCP Metrics

| Metric | Value | Source |
|---|---:|---|
| Serena tool calls | 604 | run **25206256213** Serena report |
| File-based fallback ops | 726 | run **25206256213** Serena report |
| Serena efficiency | 45% | below 50% threshold |
| Estimated tokens with Serena | ~403,160 | Serena report |
| Estimated tokens without Serena | ~734,800 | Serena report |
| Estimated token savings | ~331,640 | Serena report |
| Top Serena tools | `replace_symbol_body`, `insert_after_symbol`, `get_symbols_overview`, `find_symbol`, `find_referencing_symbols` | Serena report |

### GH API Summary

| Pattern | Observed Volume | Example |
|---|---:|---|
| Child workflow status polling | very high in watcher failures | run **25204168842** polled `actions/runs/<id>` every ~15s for ~53m |
| Linked-issue GraphQL lookups | seen in 21 log files (deep audit) | `review_autofix`, `issue_pr_status`, implement flows |
| Artifact enumeration | ~40 observed occurrences / 22 files (deep audit) | `/actions/runs/<id>/artifacts` |
| Actual 429 / secondary rate limit incidents | none surfaced as live events in sampled runs | retry scaffolding exists, but no confirmed event in this sample |

### Token Totals

| Category | Observable? | Notes |
|---|---|---|
| Per-run prompt tokens | No | OpenRouter usage lines emitted `na` |
| Per-run completion tokens | No | OpenRouter usage lines emitted `na` |
| Per-run total tokens | No | OpenRouter usage lines emitted `na` |
| Relative token savings estimate via Serena | Yes | available only through Serena synthesized report, not raw LLM telemetry |


## Deep Audit — Workflows & Scripts (2026-05-01)

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Child-workflow watchers can bind to the wrong run
- **File path** — `.github/workflows/test-and-mark-stable.yml:2725-2774; .github/workflows/test-and-mark-stable.yml:2797-2825; .github/workflows/test-and-mark-stable.yml:2860-2896; .github/workflows/test-and-mark-stable.yml:2919-2950; .github/workflows/test-and-mark-stable.yml:3025-3059`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — Each dispatch/watch block snapshots `PRE` from `actions/workflows/<file>/runs`, dispatches a child workflow, then selects the newest run with `.id > PRE`. There is no correlation on actor, inputs, parent run id, or a unique dispatch token. If another run of the same workflow is triggered after `PRE` but before this dispatch is indexed, `NEW_ID` can point at an unrelated execution. The smoke gate would then watch, pass, or fail based on the wrong child run, which is especially dangerous in release-gating paths.
- **Recommended fix** — Add a unique parent token to each child dispatch and match on that token when selecting the run. Concretely: pass `parent_run_id=${{ github.run_id }}` or a UUID input, have the child expose it in `run-name`/job summary/artifact, and have the watcher filter for that exact token instead of `id > PRE`. Reuse the cycle-local cache style already used in `scripts/orchestrate_poll_process.sh` for batched state reads instead of re-deriving identity from recency alone.

#### BUG-002 — Post-merge standalone validation clears labels for issues that were never individually dispatched
- **File path** — `.github/workflows/review_autofix.yml:498-530`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — The post-merge validate step sets `validation_dispatched="false"` and dispatches **at most one** standalone validate workflow with `tracking_issue=0` on the first linked issue carrying `ai:orchestrator-validate-required` (`lines 517-524`). The loop then continues and removes `ai:orchestrator-validate-required` from every other matching linked issue (`lines 527-529`) even though no issue-scoped validation run was started for them. For merged PRs linked to multiple validation-required issues, this can silently drop required validation state.
- **Recommended fix** — Dispatch validation per linked issue (`tracking_issue=<issue_number>`) and only remove `ai:orchestrator-validate-required` for issues whose dispatch succeeded. If batching is desired, add an explicit multi-issue input/artifact contract to `validate.yml` and make label clearing contingent on that batch being acknowledged, rather than clearing labels after a single `tracking_issue=0` run.

#### SEC-001 — Repository-check entry script executes caller input through `/bin/sh -c`
- **File path** — `scripts/run_validation_repo_checks.sh:14-23`
- **Severity** — High
- **Category tag** — `security`
- **Description** — The script replaces its default safe commands with raw positional arguments (`CHECK_COMMANDS=("$@")`) and then executes each entry via `timeout ... /bin/sh -c "${check_cmd}"`. That means shell metacharacters embedded in an argument are interpreted by a shell, not passed as literal argv. This is an arbitrary-command-execution surface in a repository script that is intentionally distributed by the repo’s validation bootstrap path (`scripts/validation_template_bootstrap.py:12-13`).
- **Recommended fix** — Remove shell-string execution entirely. Accept commands as argv arrays or as an allow-listed selector (`python`, `pytest`, `script`) plus arguments, then `exec` them directly without `/bin/sh -c`. If multiple commands must remain supported, accept a JSON array of argv arrays and run them with `python3 -c 'subprocess.run(...)'` or a bash array-based dispatcher.

#### SEC-002 — Several workflows still embed `GH_TOKEN` directly in clone URLs
- **File path** — `.github/workflows/issue_pr_status.yml:475-489; .github/workflows/validate.yml:201-233`
- **Severity** — Medium
- **Category tag** — `security`
- **Description** — These workflows build `WF_REMOTE_URL="https://x-access-token:${GH_TOKEN}@..."` and pass it to `git clone`. That puts the token into process arguments and increases the chance of accidental exposure through runner process listings or git diagnostic output. The same repository already uses a safer `http.extraHeader=Authorization: Basic ...` pattern in other workflows, such as the integration-ref resolver bootstrap in `validate.yml:94-97` and `implement.yml:239-242`.
- **Recommended fix** — Standardize all support-source clones on the existing `resolver_git()` pattern: build an auth header once, pass it through `git -c http.extraHeader=...`, and keep the remote URL token-free. Better yet, centralize support checkout in one shared helper so clone authentication semantics cannot drift across workflows.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — `review_autofix` still fan-outs PR context across five API calls instead of using the existing consolidated helper
- **File path** — `.github/workflows/review_autofix.yml:1351-1387`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The `Collect PR metadata` step currently performs 5 logical reads on the common path: `GET /pulls/{n}`, paginated issue comments, paginated reviews, paginated review comments, and a separate GraphQL `closingIssuesReferences` query. The repo already has `gh_pr_with_all_comments` in `scripts/gh_helpers.sh:733-899`, which is GraphQL-first and collapses PR metadata/comments/review comments into a single helper call, but this workflow reimplements the fetch path inline.
- **Recommended fix** — Current call count: **5**. Proposed call count after fix: **1-2** (or **1** if `gh_pr_with_all_comments` is extended to also include `closingIssuesReferences`). Extend the existing batching pattern in `scripts/gh_helpers.sh:733-899` rather than keeping a second in-workflow implementation. This also removes one of the largest `run:` blocks in the file.

#### API-002 — `issue_pr_status` re-fetches each linked issue body one-by-one after already doing a linked-issue GraphQL lookup
- **File path** — `.github/workflows/issue_pr_status.yml:192-193; .github/workflows/issue_pr_status.yml:504-512`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The workflow first gets linked issue numbers via `closingIssuesReferences(first: 50)`, then later loops over `LINKED_ISSUE_NUMBERS` and does one `_safe_gh_jq "repos/.../issues/<n>"` call per issue to inspect the body for `Managed by: AI Orchestrator`. That is an N+1 read pattern on a path that already has a GraphQL issue list in hand.
- **Recommended fix** — Current call count: **1 + N** reads. Proposed call count after fix: **1**. Extend the existing `closingIssuesReferences` GraphQL query to request the issue body (or labels if that is the intended classifier) and carry that data forward. The closest existing batching pattern to extend is `_fetch_issue_labels_batch_graphql` in `scripts/orchestrate_poll_process.sh:1227-1304`.

#### API-003 — Post-merge validate fallback in `review_autofix` does per-issue label lookups inside a loop
- **File path** — `.github/workflows/review_autofix.yml:478-530`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — When `closingIssuesReferences` comes back empty, the workflow falls back to regex-parsing issue numbers from the PR title/body, then does `gh issue view ... --json labels` inside a `while` loop for each discovered issue (`lines 500-510`). That creates a per-issue REST read path exactly where the repo’s API hygiene rules say to batch.
- **Recommended fix** — Current call count on fallback path: **2 + N** reads (1 GraphQL miss + 1 PR fetch + N issue-label reads). Proposed call count after fix: **2** reads (same GraphQL miss + same PR fetch + one batched GraphQL labels lookup). Extend the alias-based batching pattern in `scripts/orchestrate_poll_process.sh:1227-1304` so the fallback issue-number list is resolved in one query.

#### BATCH-001 — Smoke-gate child watchers poll run status at API-unfriendly cadence and scale
- **File path** — `.github/workflows/test-and-mark-stable.yml:2725-2774; .github/workflows/test-and-mark-stable.yml:2797-2825; .github/workflows/test-and-mark-stable.yml:2860-2896; .github/workflows/test-and-mark-stable.yml:2919-2950; .github/workflows/test-and-mark-stable.yml:3025-3059`
- **Severity** — High
- **Category tag** — `api-batching`
- **Description** — Each child-workflow watcher uses a tight “dispatch → poll run list every 5s until registered → poll `actions/runs/<id>` every 15–20s until complete” loop. For `workflow-log-analysis` alone, the coded worst case is roughly **1 pre-read + up to 2,400 registration polls + up to 800 status polls ≈ 3,201 calls**. Across the five blocks shown here, the coded upper bound is well over **5,000** run-status API calls in one smoke run. The same file repeats this pattern verbatim for multiple workflows rather than sharing a rate-limit-aware watcher with adaptive backoff.
- **Recommended fix** — Current call count: **thousands per smoke run** on long waits. Proposed call count after fix: **<100 total** by (a) polling quickly only until the child run is registered, (b) switching to exponential/adaptive backoff once the child is steadily `in_progress`, and ideally (c) consuming explicit child-state artifacts/outputs rather than raw run-status polling. Extend the cycle-local cache/batched-state pattern already used in `scripts/orchestrate_poll_process.sh` (for example `_candidate_details_json` and `_fetch_linked_pr_status_graphql`) and move this watcher logic into one shared helper in `scripts/gh_helpers.sh`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — `ensure_label_exists` has drift-prone reimplementations
- **File path** — `scripts/label_helpers.sh:110-195; scripts/orchestrate_poll_process.sh:1087-1142; scripts/validate_process.sh:496-530; scripts/review_rb_judge.sh:57-70; .github/workflows/review_autofix.yml:577-580; .github/workflows/review_autofix.yml:3704-3718`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The repo already has a canonical label helper in `scripts/label_helpers.sh`, but equivalent logic is duplicated in the poller, validate process, judge script fallback, and review workflow inline blocks. The copies do not behave the same: `orchestrate_poll_process.sh` adds an in-process cache, `validate_process.sh` emits Telegram on failure, and `review_autofix.yml` hardcodes color/description subsets. This is classic semantic drift territory for something central to label correctness.
- **Recommended fix** — Make `scripts/label_helpers.sh` the single owner with the existing signature `ensure_label_exists <label_name> <repo>`. Update the listed callers to source that helper; if caller-specific behavior is still needed, add small wrappers around the shared function instead of forking the implementation.

#### DUP-002 — Rate-limit retry wrappers are duplicated inline instead of sourced from `gh_helpers.sh`
- **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/review_autofix.yml:563-575; .github/workflows/review_autofix.yml:1269-1307; scripts/implement_diagnose_post_codex_failure.sh:41-48`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — There are multiple bespoke `gh_retry`/`_gh_retry` implementations with slightly different retry counts, backoff rules, temp-file handling, and rate-limit detection. The repository already ships a full helper module in `scripts/gh_helpers.sh:390-607`, but several workflows keep local forks. This increases both maintenance cost and behavioral inconsistency under GitHub throttling.
- **Recommended fix** — Use `scripts/gh_helpers.sh` as the only retry implementation. Keep the function signature `gh_retry <command...>` / `gh_retry_to_file <file> <command...>` and replace local wrappers with `source scripts/gh_helpers.sh`. For lightweight jobs that currently avoid the full helper, stage only `gh_helpers.sh` rather than copying its logic inline.

#### DUP-003 — Support-source staging logic is repeated across multiple workflows
- **File path** — `.github/workflows/clarify.yml:212-278; .github/workflows/plan.yml:240-305; .github/workflows/implement.yml:372-510; .github/workflows/review_autofix.yml:848-1045; .github/workflows/orchestrate.yml:312-405; .github/workflows/validate.yml:188-320`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — These workflows all perform near-identical “stage workflow support files” work: determine `script_ref`, clone/fallback to `main`, copy `scripts/`, `prompts/`, `ai-memory/schemas/`, and sometimes write `.gitignore` guards. The manifests differ slightly, but the control flow is mostly the same and has already grown into some of the repo’s largest `run:` blocks.
- **Recommended fix** — Extract the staging logic into one shared owner, preferably `scripts/stage_workflow_support.sh`, with a signature like `stage_workflow_support <manifest_file> <script_ref> <allow_main_fallback> <dest_root>`. Each workflow would pass its required-file manifest and consume outputs such as `resolved_script_ref`, `support_root`, and `main_fallback_used`. That reduces both duplication and expression-size risk.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Review-wait block in smoke gate is already above the High-risk threshold
- **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Description** — Estimated `run:` template body size is **19,696 characters**, leaving only **1,304 characters** before GitHub’s hard **21,000**-character expression limit. This block contains multiple `${{ }}` interpolations and a large embedded watcher implementation, so any future edits to logging, shortcuts, or inputs are likely to push it over the limit. `[NEEDS VERIFICATION]`
- **Recommended fix** — Extract this step to an external script such as `scripts/watch_review_autofix_run.sh` and pass the handful of needed env vars (`TEST_REPO`, `PR_NUMBER`, `BAIT_SHA`, timeout values). That is the same extraction strategy the repo already used successfully for `review_autofix` and `orchestrate_poll`.

#### EXPR-002 — `validate.yml` support-staging block is near the medium-risk ceiling
- **File path** — `.github/workflows/validate.yml:188-481`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — Estimated `run:` template body size is **16,529 characters**, leaving **4,471 characters** of headroom. The block mixes `${{ github.* }}` substitutions with large inline shell functions and support-copy logic, which makes it vulnerable to the same growth pattern that previously broke other workflows in this repo. `[NEEDS VERIFICATION]`
- **Recommended fix** — Move the entire support-staging routine into a reusable external script (`scripts/stage_workflow_support.sh`) or split it into multiple smaller steps: checkout resolution, file copy, schema copy, and prompt copy.

#### EXPR-003 — `review_autofix` PR-metadata collector is still a medium-risk oversized inline script
- **File path** — `.github/workflows/review_autofix.yml:1266-1588`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — Estimated `run:` template body size is **16,437 characters**, leaving **4,563 characters** of headroom. This block includes a full inline retry wrapper, the no-PR Claude-branch path, four PR fetches, linked-issue GraphQL, and context-file assembly, all inside one interpolated step. `[NEEDS VERIFICATION]`
- **Recommended fix** — Prefer the existing repo pattern: extract it to `scripts/review_collect_pr_metadata.sh`, or at minimum split “collect PR payload/comments” from “build linked issue context”. Reusing `scripts/gh_helpers.sh` would also shrink the inline shell substantially.

#### EXPR-004 — Orchestrator clarify-response posting block has limited headroom left
- **File path** — `.github/workflows/orchestrate_clarify_respond.yml:840-1123`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — Estimated `run:` template body size is **15,140 characters**, leaving **5,860 characters** of headroom. The block embeds answer parsing, memory-idempotency handling, loop protection, and GitHub posting logic in one interpolated shell body, so it is on the same growth path as other already-extracted workflow steps. `[NEEDS VERIFICATION]`
- **Recommended fix** — Extract the step to a dedicated script such as `scripts/orchestrate_post_clarify_answer.sh`. Keep only environment wiring in YAML and move idempotency/claim logic into the script.

- **Workflow file size note** — No workflow currently exceeds the **800 KB** warning threshold. The largest audited workflow is `review_autofix.yml` at roughly **267 KB**, so the immediate size risk is expression-template length, not the 1 MB file cap.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — `scripts/mark-stable.sh` appears to be dead release code
- **File path** — `scripts/mark-stable.sh:1-14`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — Repository-wide reference search found no workflow or repository script invoking `scripts/mark-stable.sh`; the live release logic is implemented directly in `.github/workflows/mark-stable.yml` instead. That leaves a dormant release-mutating script that can drift independently from the actual path used in production.
- **Recommended fix** — Either delete the script after confirming no external operational dependency exists, or make `mark-stable.yml` call it so stable-tagging has one owner. If you keep it, add a self-test or a comment in `README.md` clarifying whether it is operator-only.

#### CONSIST-001 — Integration-ref resolver bypasses the repo’s standard GitHub retry helper
- **File path** — `scripts/resolve_integration_ref.sh:38-56`
- **Severity** — Low
- **Category tag** — `consistency`
- **Description** — `resolve_integration_ref.sh` makes raw `gh api` calls for issue bodies and branch existence checks, while the rest of the repo standardizes on `scripts/gh_helpers.sh` for rate-limit/backoff behavior. Its callers in `validate.yml` and `implement.yml` treat any non-zero exit as “fall back to default branch”, so transient GitHub 403/5xx failures can silently discard integration-branch context instead of retrying once the API is healthy.
- **Recommended fix** — Source `scripts/gh_helpers.sh` when available and use `gh_retry` for the two `gh api` calls, or explicitly document that the raw-call fail-open behavior is intentional. Aligning it with the repo-standard helper would also simplify future audits.

#### SHELL-001 — Multiple scripts still rely on unquoted list expansion in loops
- **File path** — `scripts/setup_serena.sh:302-303,440-462; scripts/tg_helpers.sh:338-344,408-414; scripts/check_external_branch_advance.sh:180-182; scripts/orchestrate_poll_process.sh:2965-2966,4512-4514`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — Several shell scripts iterate with patterns like `for lang in ${ALL_LANGS}; do`, `for tg_id in ${id_list}; do`, and `for _f in ${refreshed_list}; do`. These trigger the usual SC2086/word-splitting hazards: values containing spaces, tabs, or glob characters will be split or expanded unexpectedly. Some inputs are constrained today, but the code relies on that implicit contract instead of encoding it safely.
- **Recommended fix** — Convert these to arrays or `while IFS= read -r` loops. For comma-delimited values like Telegram ids, use `IFS=',' read -r -a ids <<< "${id_list}"`; for generated path lists, write one path per line and iterate with `while IFS= read -r path`. `scripts/setup_serena.sh:454-462` already partially disables globbing for one path; apply the same rigor consistently.

- **TODO/FIXME/HACK scan** — No explicit `TODO`, `FIXME`, or `HACK` markers were found in the audited workflow and script files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 5 | BUG-001, BUG-002, SEC-001, BATCH-001, EXPR-001 |
| Medium | 10 | SEC-002, API-001, API-002, API-003, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004 |
| Low | 3 | DEAD-001, CONSIST-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 4-6 | Large |
| API call optimization | 4-5 | Large |
| Code modularization | 7-10 | Large |
| Expression size reduction | 4-8 | Medium |
| Medium/Low fixes | 5-7 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-05-01)

### Safety Tag Legend
`SAFE_TO_MERGE` means the redundancy can be removed with high confidence that endpoint/filter/retry/concurrency semantics stay intact. `NEEDS_VERIFICATION` means the overlap is real, but at least one SAFE precondition is not fully provable from static reading alone. `RISKY_SKIP` means the redundancy sits in a retry/poll/auth/race-defense path where auto-consolidation must not be done without manual design review.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — `test-and-mark-stable` creates an issue, then immediately re-fetches the same issue just to read `html_url`
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path(s)** — `.github/workflows/test-and-mark-stable.yml:371-377`
- **Current call count** — 2
- **Proposed call count** — 1
- **Endpoint(s)** — `POST /repos/{owner}/{repo}/issues`; `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The step already receives the full created-issue payload from the POST, but only keeps `.number`, then spends a second call to fetch `.html_url`.
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
- **Proposed fix** — In the `Create the issue` step, capture the POST response once (temp JSON or TSV), then derive both `ISSUE_NUMBER` and `ISSUE_URL` from that single payload.
- **Safety rationale** — The calls are adjacent in the same step with no intervening mutation, but `SAFE_TO_MERGE` is not proven because removing the second GET changes the current “POST succeeded but follow-up GET failed” failure mode.
- **Downstream signal** — Verify on the repository’s supported GitHub deployment(s) that `POST /issues` always returns `.html_url` and that no automation relies on the current second-call failure behavior; then replace the POST+GET pair with one captured POST payload.

#### MERGE-002 — `finalize_integration_merge_if_needed()` re-reads the same PR resource up to eight times at three checkpoints
- **Safety tag** — `RISKY_SKIP`
- **File path(s)** — `scripts/orchestrate_poll_process.sh:3411-3412`; `scripts/orchestrate_poll_process.sh:3466-3468`; `scripts/orchestrate_poll_process.sh:3517-3519`
- **Current call count** — Up to 8 identical `GET /pulls/{final_pr}` reads per function invocation
- **Proposed call count** — Up to 3 snapshots, one per decision checkpoint
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence** — The function fetches the same PR object repeatedly and extracts one field at a time, even though `_fetch_pr_json()` already exists.
  ```bash
  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ...
  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ...
  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
  Existing helper:
  ```bash
  _fetch_pr_json()
  {
  	local pr_number="$1"
  	gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" || echo '{}'
  }
  ```
- **Proposed fix** — In `finalize_integration_merge_if_needed()`, take one `_fetch_pr_json "${final_pr}"` snapshot at each checkpoint and parse `.state`, `.mergeable`, and `.merged_at` locally instead of issuing field-by-field GETs.
- **Safety rationale** — This sits inside `scripts/orchestrate_poll_process.sh`, an explicit poller/race-defense path, which is a mandatory `RISKY_SKIP` trigger even though the endpoint overlap is obvious.
- **Downstream signal** — Do not auto-implement inside `orchestrate_poll_process.sh`; manual review must confirm that snapshot reuse preserves poller race handling, fail-open behavior, and any log/output sequencing relied on by downstream watchdog logic.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — `review_rb_judge.sh` re-fetches linked-issue numbers and bodies that `review_autofix` already cached earlier in the same job
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path(s)** — `.github/workflows/review_autofix.yml:1381-1386`; `.github/workflows/review_autofix.yml:1409-1441`; `.github/workflows/review_autofix.yml:3619-3663`; `scripts/review_rb_judge.sh:146-169`
- **Current call count** — On the linked-issue path, 1 early GraphQL fetch + 1 judge GraphQL fetch + `N` judge REST issue-body reads = `2 + N`
- **Proposed call count** — 1 on cache-hit path; retain current `1 + N` judge fetches only as fail-open fallback when the cache is absent/invalid
- **Endpoint(s)** — GraphQL `pullRequest(number){closingIssuesReferences(first:50){nodes{number title body}}}`; GraphQL `pullRequest(number){closingIssuesReferences(first:50){nodes{number}}}`; `GET /repos/{owner}/{repo}/issues/{issue_number}`
- **Evidence** — The early metadata step already fetches linked issue details and materializes them into job-local artifacts/env, and the later cache step explicitly treats that early value as authoritative:
  ```bash
  if gh_retry "${_linked_tmp}" api graphql \
    ... \
    -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
    _linked_raw="$(cat "${_linked_tmp}" 2>/dev/null || echo '[]')"
  fi
  ...
  printf '%s' "${_linked_raw}" > "${_linked_json_file}"
  python3 - "${_linked_json_file}" "${LINKED_ISSUE_CONTEXT_FILE}" ...
  ```
  ```bash
  if [ -n "${LINKED_ISSUES_JSON+x}" ]; then
    echo "Linked issues already cached from early fetch."
  else
    if LINKED_ISSUES_JSON="$(gh_retry gh api graphql ... )"; then
      :
    fi
  fi
  ```
  But the judge redoes the lookup and then re-GETs issue bodies:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    ... closingIssuesReferences(first: 50) { nodes { number } } ...)"
  ...
  BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
  ```
- **Proposed fix** — Extend the early `Collect PR metadata` step to export a structured `LINKED_ISSUE_DETAILS_JSON` cache (or make `review_rb_judge.sh` consume `LINKED_ISSUE_CONTEXT_FILE` plus `LINKED_ISSUES_JSON`), and update `scripts/review_rb_judge.sh` to use that cache before falling back to its current GraphQL/REST path.
- **Safety rationale** — The overlap is real, but the calls are in different workflow steps, so the `SAFE_TO_MERGE` precondition of “no intervening mutation” is not fully provable from static reading alone.
- **Downstream signal** — Verify that the judge path always receives the early linked-issue cache (normal exhaustion, `force_rb_judge`, and conflict-resolution paths) and that no earlier step can mutate linked-issue title/body in a way the judge must see live before replacing the judge’s GraphQL+REST fetches.

#### REUSE-002 — `review_rb_judge.sh` re-fetches PR diff and comment context that `review_autofix` already snapshotted earlier in the same run
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path(s)** — `.github/workflows/review_autofix.yml:1351-1357`; `.github/workflows/review_autofix.yml:1559-1560`; `.github/workflows/review_autofix.yml:2124-2125`; `scripts/review_rb_judge.sh:191-208`
- **Current call count** — On helper-hit path, early snapshot cost is 5 logical reads and the judge adds 2 more (`diff` + `gh_pr_with_all_comments`) = 7 total; on helper REST fallback the judge adds 4 more instead of 2
- **Proposed call count** — Keep the early 5 logical reads; reduce the judge’s additional calls to 0 on cache-hit path
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{n}`; `GET /repos/{owner}/{repo}/issues/{n}/comments`; `GET /repos/{owner}/{repo}/pulls/{n}/reviews`; `GET /repos/{owner}/{repo}/pulls/{n}/comments`; PR diff fetch (`gh pr diff` / PR diff media type)
- **Evidence** — Early in the workflow, the job already snapshots the raw PR payload, issue comments, reviews, review comments, and diff:
  ```bash
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
  gh_retry /tmp/gh_issue_comments_raw.json api --paginate repos/${{ github.repository }}/issues/"${PR_NUMBER}"/comments
  gh_retry /tmp/gh_reviews_raw.json api --paginate repos/${{ github.repository }}/pulls/"${PR_NUMBER}"/reviews
  gh_retry /tmp/gh_review_comments_raw.json api --paginate repos/${{ github.repository }}/pulls/"${PR_NUMBER}"/comments
  ...
  if ! gh pr diff "${PR_NUMBER}" > "${PR_DIFF_FILE}"; then
  ```
  On the judge path, the later diff-regeneration step is skipped:
  ```yaml
  - name: Generate diff context
    if: steps.retrigger_guard.outputs.max_iterations_reached != 'true' && env.PR_CLOSED != 'true'
  ```
  But the judge still refetches live PR diff/comments:
  ```bash
  PR_DIFF="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    -H 'Accept: application/vnd.github.diff' 2>/dev/null || echo "(diff unavailable)")"
  ...
  PR_CONTEXT_JSON="$(gh_pr_with_all_comments "${REPOSITORY%%/*}" "${REPOSITORY##*/}" "${PR_NUMBER}" "${PRELOADED_PR_META}" || echo '{}')"
  ```
- **Proposed fix** — In `scripts/review_rb_judge.sh`, add an artifact-first loader that prefers `${PR_PAYLOAD_FILE}`, `${PR_META_FILE}`, `${PR_ISSUE_COMMENTS_FILE}`, `${PR_REVIEW_COMMENTS_FILE}`, and `${PR_DIFF_FILE}`; keep the current API path only as a freshness/missing-file fallback.
- **Safety rationale** — Static reading shows strong overlap, but `SAFE_TO_MERGE` is not provable because the judge runs later and live PR state/comments could have changed between the early snapshot and the judge invocation.
- **Downstream signal** — Before changing the judge, verify that the stored head SHA still matches the live PR at judge entry (or add an explicit freshness check), and confirm the artifact set fully covers the judge prompt fields now sourced from `gh_pr_with_all_comments` and the live diff fetch.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — `COMMITS_AFTER` is fetched but never consumed
- **Safety tag** — `SAFE_TO_MERGE`
- **File path(s)** — `.github/workflows/test-and-mark-stable.yml:1508-1512`
- **Current call count** — 1
- **Proposed call count** — 0
- **Endpoint(s)** — `GET /repos/{owner}/{repo}/commits?sha={branch}&per_page=20`
- **Evidence** — The value is assigned, then the step immediately uses a different API result (`PR_HEAD`) for the actual assertion; `COMMITS_AFTER` is never read afterward.
  ```bash
  COMMITS_AFTER=$(gh api "repos/${TEST_REPO}/commits?sha=${BRANCH}&per_page=20" \
    --jq "[.[] | select(.sha != \"${BAIT_SHA}\") | .sha] | length" 2>/dev/null || echo "0")
  # The PR head SHA should differ from the bait SHA.
  PR_HEAD=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
  ```
- **Proposed fix** — Delete the `COMMITS_AFTER=` API call and keep the existing `PR_HEAD != BAIT_SHA` check as the sole guard.
- **Safety rationale** — The result has no downstream consumer, the call is not paginated/retried in a special control path, and removing it does not change log keys or branch/PR filters.
- **Downstream signal** — Delete the `COMMITS_AFTER` `gh api` assignment and leave the `PR_HEAD` vs. `BAIT_SHA` assertion unchanged.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — Good batching target, but the helper extension must preserve current paginated comment/review coverage and fail-open retry behavior.
- API-002: NEEDS_VERIFICATION — Real N+1 pattern, but the fix crosses workflow-step boundaries and should preserve the current conservative orchestrator-classification behavior.
- API-003: NEEDS_VERIFICATION — The per-issue label lookups are batchable, but this sits in a merged-PR validation path whose label-clearing semantics already need careful human review.
- BATCH-001: RISKY_SKIP — The watcher loops are explicit poll/race-defense code in a release gate, so they should not be auto-consolidated.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | DEAD-API-001 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-001, REUSE-002 |
| RISKY_SKIP | 1 | MERGE-002 |

### Implement-Stage Handoff
- DEAD-API-001
