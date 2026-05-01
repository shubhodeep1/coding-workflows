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

