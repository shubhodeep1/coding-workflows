## Executive Summary

- **`review_autofix` is the dominant cost and latency hotspot.** In `shubhodeep1/coding-workflows`, the family has **160 runs**, **106 cancelled**, **p50 39s**, but **p95 1,849s (~30.8m)**. Multiple successful runs on May 1, 2026 still took **1,418s, 1,430s, 1,466s, 1,696s, and 1,846s** on the Claude-branch comment-only path, even though editor/commit/judge/auto-merge were skipped. **Impact:** very high latency and token savings if the comment-only path is thinned. **Confidence:** high.

- **`workflow_log_analysis` and its parent `test_and_mark_stable` are timing out structurally, not incidentally.** Run **25200117236** was cancelled after about **60m** in `api-redundancy`; parent run **25200104592** then failed after **5,361s** while watching that child. Another parent run **25150961704** failed after **4,788s** waiting for `review_autofix`. Recent PR text in logs explicitly notes this was the **same failure mode after a prior 30→60 minute bump**. **Impact:** very high reliability and release-cycle latency improvement if the long audit/review stages are decomposed instead of just extending caps. **Confidence:** high.

- **CI is consistently slow and fails late for cheap-to-detect problems.** `ci` runs have **p50 612s** and **p95 651s**. Failures include a simple Ruff issue (`E101 Indentation contains mixed spaces and tabs`, run **25200848815**) and a workflow self-test classification failure (`Unclassified checkout@v5 workflows: ['forward-merge-stable-to-main.yml']`, run **25151563308**) that both surfaced after most of the 10-minute job elapsed. **Impact:** high feedback-speed gain, moderate reliability gain, low implementation risk. **Confidence:** high.

- **AI memory is enabled but mostly not helping reviewer runs.** Across sampled deep-dive logs there were **25 AI_MEMORY_TELEMETRY events** and **6 retrieves**; only **1/6 retrieves hit** (**16.7% hit rate**). Reviewer retrieves were mostly `keyword_method="none"` with `records_selected=0`; only the implement run had a useful hit (`estimated_tokens=28`, run **25151206656**). **Impact:** medium cost/quality gain if reviewer retrieval is made relevant; current memory overhead is low but effectiveness is weak. **Confidence:** high.

- **Prompt cache instrumentation is present, but cache effectiveness is not observable.** Logs show `OPENROUTER_PROMPT_CACHE_DISABLED=false` and repeated “OpenRouter Prompt Cache Instrumentation” blocks, but sampled runs do **not** emit actual hit/miss or prompt/completion totals. That blocks precise cost control and hides fragmentation from retries/context restatement. **Impact:** medium cost and latency gain once measured and stabilized. **Confidence:** high.

- **GitHub Actions runner queue time is a recurring tax on short workflows and pollers.** Recent `forward_merge_stable_to_main`, `promote_main_to_stable`, `issue_pr_status`, `cancel_on_pr_close`, and `orchestrate_poll` runs all logged hosted-runner wait despite total durations of only **6–42s**. **Impact:** medium end-to-end improvement if no-op/skipped workflows are filtered earlier, plus lower queue contention for critical jobs. **Confidence:** medium.

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Decompose `workflow_log_analysis`’s `api-redundancy` stage instead of extending timeouts**
   - **Type:** Critical-path win
   - **Evidence:** Child run **25200117236** (`workflow_log_analysis`) was cancelled after about **60m** while still in `api-redundancy`; parent `test_and_mark_stable` run **25200104592** failed after **5,361s** watching it. The PR body echoed in recent `issue_pr_status` logs states the same structural failure had already survived a **30→60 minute** cap increase and that the audit input size (~**51 KB**) was not the root cause.
   - **Root cause:** A single long-lived Codex audit pass is doing both discovery and final synthesis under `xhigh` reasoning, so one slow tail blocks the whole release chain.
   - **Exact change:** Split `api-redundancy` into:
     1. a deterministic pre-pass that extracts candidate hotspots/call counts locally,
     2. a smaller LLM synthesis pass over only the condensed findings.
     Keep `xhigh` only on the synthesis pass if needed.
   - **Estimated time savings:** **30–50 minutes** on the slow audit job; **45–90 minutes** on parent release workflows that currently wait on it.
   - **Implementation risk:** **Medium.** Behavior stays backward-compatible if the deterministic pre-pass is advisory and the final markdown contract remains unchanged.

2. **Thin the Claude-branch comment-only `review_autofix` path**
   - **Type:** Critical-path win
   - **Evidence:** Slow successful `review_autofix` runs took **1,418s** (run **25201144027**), **1,430s** (**25203375473**), **1,466s** (**25203362359**), **1,696s** (**25202909051**), and **1,846s** (**25201255563**). Logs show `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.` Environment also shows **6 reviewer models**, `ENABLE_REVIEWER_TWO_PASS=true`, and `REVIEWER_REASONING_EFFORT=xhigh`.
   - **Root cause:** The “comment-only” path still pays for a near-full multi-model review stack.
   - **Exact change:** For `claude/*` review-only runs, add a fast path:
     - if diff is tiny/doc-only or no linked issues are found, run only a single summariser/reviewer,
     - otherwise reduce the panel size or disable second-pass review unless a risk heuristic fires.
   - **Estimated time savings:** **10–25 minutes** per long Claude-branch review run.
   - **Implementation risk:** **Medium.** Mitigate with a `force-review` escape hatch and preserve full panel for larger/riskier diffs.

3. **Split CI into fast-fail and slower validation jobs**
   - **Type:** Critical-path win for failures; local speedup for successes if parallelized
   - **Evidence:** `ci` family p50 is **612s** and p95 **651s**. Run **25200848815** failed on Ruff with `E101` near the end of a **629s** run. Run **25151563308** failed because a classification self-test still considered `forward-merge-stable-to-main.yml` unclassified, also after most of a **567s** run.
   - **Root cause:** Cheap lint/classification failures are buried inside a long serial `lint` job.
   - **Exact change:** Split into at least:
     - `lint-fast`: Ruff + syntax + workflow-script classification checks,
     - `tests`: orchestrate/unit/self-tests,
     - optional coverage/reporting tail.
     Fail fast before starting the long suite.
   - **Estimated time savings:** **8–10 minutes** on failing PRs; **2–4 minutes** on successful PRs if the slower parts run in parallel.
   - **Implementation risk:** **Low.**

4. **Tighten trigger-level filtering for clarify/plan/implement/respond no-ops**
   - **Type:** Local micro-optimization with queue relief
   - **Evidence:** `clarify` had **133 total runs** with only **11 success** and **122 skipped/other**; `plan` had **119 total** with **9 success** and **110 skipped/other**; `implement` had **119 total** with **13 success** and **103 skipped/other**; `orchestrate_clarify_respond` had **119 total** with only **2 success**. Recent samples repeatedly completed in **0–1s** after `if` evaluation.
   - **Root cause:** Workflows are being invoked for many events that are immediately gated off at job runtime.
   - **Exact change:** Move as much filtering as possible from job `if:` to event filters / dispatch conditions / reusable workflow call guards.
   - **Estimated time savings:** **5–20s** per no-op invocation plus reduced queue contention for real work.
   - **Implementation risk:** **Low**, provided current trigger semantics are mirrored carefully.

5. **Skip Serena/bootstrap setup on flows that cannot reach code-editing**
   - **Type:** Local micro-optimization
   - **Evidence:** Failed implement run **25151206656** invoked `setup_serena.sh`, warmed Serena cache successfully, created `.serena/project.yml`, and resolved the Serena binary. That setup is useful in editing flows, but recent `review_autofix` post-merge/no-linked-issue runs completed in **10–28s** without any visible Serena activity.
   - **Root cause:** Tool bootstrap cost is paid even when gates may short-circuit to no-op/comment-only logic.
   - **Exact change:** Run gate/linked-issue checks first, and only bootstrap Serena after a path actually requires code inspection/editing.
   - **Estimated time savings:** **5–10s** on short-circuit AI runs.
   - **Implementation risk:** **Low.**

---

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Reduce reviewer-panel breadth on Claude-branch comment-only runs**
   - **Evidence:** Long `review_autofix` runs on the comment-only path still load `REVIEWER_MODELS` across six providers, `ENABLE_REVIEWER_TWO_PASS=true`, and `REVIEWER_REASONING_EFFORT=xhigh`. This happens in runs like **25201255563**, **25202909051**, and **25203362359**.
   - **Root cause:** Model selection is oversized for a path that explicitly skips editing and merge actions.
   - **Exact change:** On `claude/*` comment-only runs, use:
     - fewer reviewer models,
     - single-pass review,
     - lower reasoning effort (`high` instead of `xhigh`) unless a risk condition is met.
   - **Estimated savings:** **40–70%** token/dollar reduction on long comment-only review runs.
   - **Quality-risk notes:** **Medium.** Keep full-panel fallback for large diffs, integration-sensitive files, or explicit override labels.

2. **Stop repeating full prompt/context across retries and phase restarts**
   - **Evidence:** Failed implement run **25151206656** repeatedly re-emitted prompt/cache instrumentation and long instruction blocks, with multiple prompt file exports and repeated “OpenRouter Prompt Cache Instrumentation” sections. The run also explicitly documents “prompt-prefix caching motivation,” implying the prefix is intended to be stable.
   - **Root cause:** Retry attempts restate large static context instead of isolating deltas.
   - **Exact change:** Persist a stable prefix once and append only:
     - failure deltas,
     - changed-file lists,
     - compact retry guidance.
   - **Estimated savings:** **Medium to high** token reduction on implement and retry-heavy flows.
   - **Quality-risk notes:** **Low** if the stable prefix is versioned and immutable within a run.

3. **Instrument real prompt-cache hit/miss usage and optimize for it**
   - **Evidence:** Logs show `OPENROUTER_PROMPT_CACHE_DISABLED=false` and explicit prompt-cache instrumentation, but no usable counts for `prompt_tokens`, `completion_tokens`, `cache_creation_input_tokens`, or `cache_read_input_tokens` are emitted in sampled successful/slow runs.
   - **Root cause:** Cache is enabled but not observable, so fragmentation cannot be quantified or corrected.
   - **Exact change:** Emit per-step counters for:
     - prompt/completion/total tokens,
     - cache create/read tokens,
     - request count by model.
     Then enforce a stable prompt prefix ordering.
   - **Estimated savings:** **Medium**, currently unquantifiable from this window.
   - **Quality-risk notes:** **Low.** This is observability-first and fail-open.

4. **Cut avoidable reruns/cancellations in `review_autofix`**
   - **Evidence:** `review_autofix` had **160 total runs**, **54 success**, **106 cancelled**, **0 failures**. Several cancelled runs still consumed real time: **243s** (run **25201063457**), **304s** (**25203253448**), **313s** (**25201143852**), **367s** (**25203242500**), **380s** (**25202771319**), **417s** (**25201998070**).
   - **Root cause:** Superseded or redundant runs often start far enough to burn queue/compute before cancellation lands.
   - **Exact change:** Apply stronger concurrency cancellation and pre-gate checks before expensive agent startup.
   - **Estimated savings:** **High runner and token savings** on busy PRs; especially valuable because some cancelled runs already reached Codex-agent execution.
   - **Quality-risk notes:** **Low** if cancellation keys are scoped to PR/head-ref and never suppress manual reruns.

5. **Use lighter models for discovery, not for final judgment**
   - **Evidence:** The slowest analysis/review paths combine `openai/gpt-5.4` or reviewer panels with `xhigh` reasoning. The release watcher evidence shows that simply increasing timeout ceilings has not solved the underlying latency/cost profile.
   - **Root cause:** High-reasoning models are being used too early in pipelines that first need filtering/condensation.
   - **Exact change:** Keep the strongest model for final synthesis or adjudication only; use deterministic scripts or a cheaper summariser for discovery/pre-triage.
   - **Estimated savings:** **Medium to high** on `workflow_log_analysis` and long review runs.
   - **Quality-risk notes:** **Low to medium.** Safe if the lighter stage cannot finalize decisions on its own.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Replace timeout inflation with phase decomposition and explicit child status contracts**
   - **Failure evidence:** `test_and_mark_stable` failed **2 of 3 runs** in the sample (`failure_rate=66.7%`). Run **25150961704** failed in `Phase 4: Wait for review & autofix to complete`; run **25200104592** failed watching `workflow-log-analysis`.
   - **Root cause category:** Timeout/stall in downstream child workflows.
   - **Exact fix:** Have long child workflows publish explicit phase/result status artifacts or outputs, and let parents fail on those states rather than idle polling until outer timeout.
   - **Expected reliability impact:** **High** reduction in false parent failures and reruns.
   - **Rollback/fail-open considerations:** Keep existing timeout as a final safety net while adopting child status contracts.

2. **Fix the workflow classification/self-test drift immediately**
   - **Failure evidence:** CI run **25151563308** ended with `AssertionError: Unclassified checkout@v5 workflows: ['forward-merge-stable-to-main.yml']`.
   - **Root cause category:** Validation fixture/rule drift after workflow changes.
   - **Exact fix:** Update the classifier/allowlist so new workflow files are recognized as soon as they are added; make this check part of `lint-fast`.
   - **Expected reliability impact:** **High** reduction in CI false failures.
   - **Rollback/fail-open considerations:** If classifier uncertainty remains, downgrade unknown-but-safe workflows to warning until the rule is updated.

3. **Promote Ruff/format errors to an immediate pre-test gate**
   - **Failure evidence:** CI run **25200848815** failed on `E101 Indentation contains mixed spaces and tabs` after a **629s** job.
   - **Root cause category:** Late surfacing of deterministic style errors.
   - **Exact fix:** Run Ruff first, and optionally add local/pre-commit enforcement so malformed files never enter the slow CI path.
   - **Expected reliability impact:** **Medium** reduction in failed CI cycles and wasted reruns.
   - **Rollback/fail-open considerations:** None needed; this is a stricter early gate for an already-failing condition.

4. **Harden implement support-source staging before Codex starts**
   - **Failure evidence:** Implement run **25151206656** logged `Canonical integration resolver failed; falling back to default branch`, then `Failed to checkout workflow support source`, plus missing support scripts/prompts during staging.
   - **Root cause category:** Bootstrap/source-resolution failure.
   - **Exact fix:** Validate the support source manifest before launching the implementation phase; if staging fails, stop with a short bootstrap error instead of entering the full Codex flow.
   - **Expected reliability impact:** **Medium** reduction in implement failures and wasted token attempts.
   - **Rollback/fail-open considerations:** Preserve current default-branch fallback, but fail fast if required files are still absent.

5. **Repair the nightly validation self-test matrix before treating it as a hard gate**
   - **Failure evidence:** Nightly validation self-test run **25200681719** reported `fixtures=3 passed=1 failed=2` and exited non-zero.
   - **Root cause category:** Broken test fixtures / unstable self-test coverage.
   - **Exact fix:** Quarantine failing fixtures or mark expected-fail until repaired; emit per-fixture identities into the summary for targeted triage.
   - **Expected reliability impact:** **Medium** improvement in signal quality from the nightly safety net.
   - **Rollback/fail-open considerations:** If temporary quarantine is used, keep status tracking so green runs still require explicit re-enablement.

6. **Make reviewer memory helpers consistently available**
   - **Failure evidence:** Slow `review_autofix` runs **25163198735** and **25165138809** repeatedly logged `memory helper script missing` for run-start, fallback context, candidate record, and run-end completion.
   - **Root cause category:** Incomplete support packaging / optional dependency drift.
   - **Exact fix:** Bundle `memory_helpers.sh` and related support files in the same validated bootstrap manifest as other required review assets.
   - **Expected reliability impact:** **Low to medium** improvement in observability and post-run learning continuity.
   - **Rollback/fail-open considerations:** Keep current fail-open behavior if helpers are unavailable.

---

## AI Memory Health

- **Telemetry observed:** yes, in deep-dive logs from `implement`, `review_autofix`, and `memory_maintenance`.
- **Total sampled `AI_MEMORY_TELEMETRY` events:** **25**
- **Operation mix:**
  - `record-run-event`: **12**
  - `retrieve`: **6**
  - `record-candidate`: **5**
  - `processed-command-check`: **1**
  - `processed-command-claim`: **1**

### Retrieve effectiveness
- **Retrieve hit rate:** **16.7%** (**1 hit / 6 retrieves**)
- **Average `estimated_tokens`:** **4.7**
- **Keyword method distribution:**
  - `plain`: **1**
  - `none`: **5**
  - `llm`: **0**
- **Zero-record retrieves:** **5**
- **`fail_open: true` entries:** **0**
- **`enabled: false` entries:** **0**

### Evidence by workflow
- **Implement run 25151206656:** one useful retrieve hit:
  - `records_selected=1`
  - `estimated_tokens=28`
  - `keyword_method="plain"`
- **Reviewer runs 25163198735 and 25165138809:** retrieves were effectively empty:
  - `records_selected=0`
  - `estimated_tokens=0`
  - `keyword_method="none"`

### Push/write health
- **Max `push_attempts`:** **1**
- **Entries with `push_attempts > 1`:** **0**
- **High push retry counts:** not observed in this sample.

### Memory health findings
1. **Reviewer memory retrieval is mostly ineffective**
   - Evidence: 5 of 6 retrieves returned zero records; reviewer runs used `keyword_method="none"`.
   - Recommendation: improve reviewer retrieval query construction so it emits actual keywords or issue/PR anchors, not empty/no-keyword lookups.
   - Expected impact: better continuity with minimal latency cost.

2. **Memory helper packaging is inconsistent**
   - Evidence: `review_autofix` logs repeatedly warned `memory helper script missing` while still emitting some telemetry.
   - Recommendation: make helper availability part of bootstrap validation, or log a single consolidated degraded-mode marker instead of repeated warnings.
   - Expected impact: cleaner observability and more complete run lineage.

3. **Compaction looks healthy**
   - Evidence: `memory_maintenance` run **25202051337** compacted month `2026-04` successfully, archiving **2,914 candidates** and **6,567 ledger entries**, with `did_push=true`.
   - Recommendation: keep current compaction cadence; no action needed beyond ongoing monitoring.

---

## GH API Call Audit

### High-volume / high-redundancy patterns

1. **Release smoke and orphan workflow watchers are API-heavy and duplicated**
   - **Evidence:** `test_and_mark_stable` failure logs show repeated dispatch/watch logic with long polling loops. In run **25200104592**, `orphan-workflows-test` polled `repos/${TEST_REPO}/actions/runs/${NEW_ID}` every ~15s for over **30 minutes** while the child stayed `in_progress`.
   - **Root cause:** Repeated bespoke “dispatch + register + poll” blocks for child workflows.
   - **Concrete change:** Extract a shared `dispatch_and_wait_workflow` helper with:
     - cycle-local cached run inventory,
     - registration polling with short backoff,
     - status polling with slower backoff after stable `in_progress`,
     - shared fail-open handling.
   - **Estimated call-count reduction:** roughly **~2x** per watched child workflow on common paths, plus lower rate-limit risk.

2. **`issue_pr_status` has a batched GraphQL path, but degraded fallback reverts to per-issue REST**
   - **Evidence:** Recent run **25204133638** logged `GraphQL batch failed — fall back to per-issue REST detection`, then looped over `repos/${REPOSITORY}/issues/${_orch_num}` one issue at a time.
   - **Root cause:** Fallback path does not preserve batching discipline.
   - **Concrete change:** Retry GraphQL on only the failed subset, or issue one alias-based GraphQL batch for the unresolved issue numbers instead of N REST calls.
   - **Estimated call-count reduction:** degraded path **1 + N → 1 or 2**.
   - **Rate-limit risk reduction:** meaningful on PRs linked to many orchestrator issues.

3. **`cancel_on_pr_close` does per-run cancellation POSTs and rate-limit probing even when nothing needs cancelling**
   - **Evidence:** Recent runs **25204119994**, **25204133650**, and **25204147910** all completed with no matching runs, but logs still show `gh api -i /rate_limit` and loop-shaped cancel logic.
   - **Root cause:** Cancellation workflow always initializes the rate-limit-aware cancellation path.
   - **Concrete change:** First fetch candidate runs once; if the set is empty, skip rate-limit probing and cancellation setup entirely.
   - **Estimated call-count reduction:** common no-op path **2+ calls → 1**.
   - **Rate-limit risk reduction:** small per run, but high aggregate because these jobs are frequent and short.

4. **Linked-issue resolution is fetched in multiple places with similar GraphQL queries**
   - **Evidence:** `closingIssuesReferences` appeared in **21 log files** across recent `review_autofix`, `issue_pr_status`, and implement logs; recent review runs **25204120002** and **25204147954** both used `gh api graphql` just to determine no linked issues existed.
   - **Root cause:** Similar PR-linked-issue lookups are reimplemented across workflow families.
   - **Concrete change:** Centralize linked-issue resolution into one helper that returns:
     - linked issue numbers,
     - labels,
     - title/body fragments when needed,
     - cached within a workflow cycle.
   - **Estimated call-count reduction:** moderate, especially in post-merge and status-sync paths.
   - **Rate-limit risk reduction:** moderate.

5. **Artifact enumeration is repeated across review/reporting workflows**
   - **Evidence:** `actions/runs/<id>/artifacts` access showed up in **22 files** / about **40 observed occurrences**, including `copilot_pull_request_reviewer`, `nightly_validation_selftest`, and `test_and_mark_stable`.
   - **Root cause:** Cleanup/reporting steps independently enumerate run artifacts.
   - **Concrete change:** When multiple later steps need artifact metadata, fetch artifact inventory once and fan out IDs/URLs via outputs or a temp file.
   - **Estimated call-count reduction:** low to moderate.
   - **Rate-limit risk reduction:** low.

### Repo-specific API hygiene assessment

- **Mandatory batching:** partially followed. `issue_pr_status` and some review paths already use GraphQL batching, but degraded paths still fall back to per-item REST loops.
- **Cycle-local caches:** inconsistent. Good in some PR-state fetches; weak in child workflow watchers and fallback issue classification.
- **Fail-open behavior:** generally good. Recent `issue_pr_status` explicitly uses conservative fail-open behavior to avoid reintroducing orchestrator issue corruption.

---

## MCP & Serena Efficiency

1. **No evidence of Serena onboarding misuse**
   - **Evidence:** Sampled logs show `setup_serena.sh` usage and Serena cache warmup in implement run **25151206656**, but no onboarding calls or repeated project activation misuse.
   - **Recommendation:** Keep current “one bootstrap per editing run” behavior.

2. **Serena setup overhead is paid even when the workflow may short-circuit**
   - **Evidence:** Implement run **25151206656** spent time warming Serena cache and creating `.serena/project.yml`; many recent AI runs ended quickly after gate decisions or “no linked issues found.”
   - **Recommendation:** Move Serena bootstrap after the last cheap gate that can still short-circuit execution.
   - **Expected impact:** modest latency savings (**5–10s/run**) and less tool churn.

3. **Sampled logs do not expose actual Serena call patterns, so efficiency cannot be audited deeply**
   - **Evidence:** Recent runs often report no Serena tool patterns; one cancelled `review_autofix` run recorded `SERENA_REPORT_FILE`, but the report content was not surfaced in sampled logs.
   - **Recommendation:** Emit compact Serena counters into logs:
     - symbol lookups,
     - raw file reads,
     - repeated region reads,
     - search operations,
     - elapsed setup vs active-use time.
   - **Expected impact:** enables real MCP efficiency tuning next cycle.

4. **Comment-only review paths should avoid code-tool initialization unless a code read is guaranteed**
   - **Evidence:** `review_autofix` recent runs with no linked issues completed in **10–28s** and were dominated by gating/GraphQL checks, not editing.
   - **Recommendation:** Gate MCP/Serena startup behind the same conditions that decide whether codex-agent will actually inspect code.
   - **Expected impact:** small but safe speed/cost win.

5. **Parallelizable safe reads are underused in non-editing audit/report flows**
   - **Evidence:** Workflow-log analysis and artifact/report collectors appear serialized across independent preparation steps.
   - **Recommendation:** Parallelize independent artifact metadata reads / summary-file generation where outputs do not depend on each other.
   - **Expected impact:** low to moderate latency improvement on reporting flows.

---

## Prompt Cache & Memory System

1. **Prompt cache is enabled but effectively unmeasured**
   - **Evidence:** Logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED=false`, and implement logs contain repeated “OpenRouter Prompt Cache Instrumentation” sections, but sampled runs do not emit usable cache hit/miss or prompt/completion totals.
   - **Assessment:** You cannot currently tell whether cache-friendly prompt shaping is actually saving money.
   - **Recommendation:** Emit step-level:
     - request counts,
     - prompt/completion/total tokens,
     - `cache_creation_input_tokens`,
     - `cache_read_input_tokens`,
     - cache hit ratio by workflow family.
   - **Estimated impact:** medium cost visibility; likely medium token savings once optimized.

2. **Prompt fragmentation is likely caused by retry restatement and dynamic noise placement**
   - **Evidence:** Failed implement run **25151206656** repeated prompt-related exports and instrumentation many times. The logs also explicitly reference “prompt-prefix caching motivation.”
   - **Assessment:** Static instructions appear to be reintroduced alongside dynamic state during retries.
   - **Recommendation:** Keep:
     - stable system instructions,
     - stable repo/workflow instructions,
     - stable support docs
     in a fixed prefix, and append only deltas/errors after that.
   - **Estimated impact:** lower token spend and improved cache reuse.
   - **Reliability impact:** positive, because retries become easier to reason about.

3. **Memory retrieval works for implement, not for reviewer**
   - **Evidence:** Implement retrieve in run **25151206656** returned 1 record; reviewer retrieves in slow runs returned 0 records with `keyword_method="none"`.
   - **Assessment:** The memory system is available, but reviewer retrieval inputs are too weak to select relevant records.
   - **Recommendation:** Populate reviewer retrieval with stable keys such as:
     - PR number,
     - changed-file stems,
     - workflow family,
     - issue references,
     - prior reviewer consensus fingerprint.
   - **Estimated impact:** medium quality improvement with low token overhead.

4. **Memory helper degraded mode is too noisy**
   - **Evidence:** Slow review runs repeatedly logged `memory helper script missing` for multiple lifecycle events.
   - **Assessment:** Fail-open behavior is correct, but repeated warnings clutter logs and suggest partial wiring.
   - **Recommendation:** Emit one degraded-mode marker per run and summarize skipped memory operations once.
   - **Estimated impact:** better observability, easier debugging.

5. **Non-prompt cache hygiene is mixed**
   - **Evidence:** Implement bootstrap in run **25151206656** warned that no dependency files matched the uv cache dependency glob, producing a `...pruned-no-dependency-glob` cache key that “will never get invalidated.” Other review runs showed good uv cache hits on a stable key.
   - **Assessment:** Runtime dependency caching is not consistently keyed.
   - **Recommendation:** Ensure cache dependency globs always match the checked-out working tree before cache restore/save.
   - **Estimated impact:** small latency/reliability gain; avoids stale tool cache surprises.

---

## Orchestrator Health

1. **Event gating is logically healthy but operationally noisy**
   - **Evidence:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have extremely high skip/other counts:
     - `clarify`: **133 total**, **11 success**, **122 other/skipped**
     - `plan`: **119 total**, **9 success**, **110 other/skipped**
     - `implement`: **119 total**, **13 success**, **103 other/skipped**
     - `orchestrate_clarify_respond`: **119 total**, **2 success**, **117 other/skipped**
   - **Interpretation:** The decision logic is working, but too many workflows are still being instantiated before they short-circuit.
   - **Smallest safe mitigation:** push more filtering to the trigger layer.

2. **Poller health is acceptable, but queue wait dominates**
   - **Evidence:** `orchestrate_poll` average duration is **55.2s**, p95 **97.2s**; recent run **25202569766** succeeded in **42s** with runner wait being the main visible delay.
   - **Interpretation:** The poller itself is not obviously stuck, but short operational jobs are paying hosted-runner startup tax.
   - **Smallest safe mitigation:** reduce no-op workflow volume to keep queue capacity for pollers and critical child workflows.

3. **Parent/child flow health is weakest around long AI phases**
   - **Evidence:** `test_and_mark_stable` failures came from downstream watch phases, not immediate syntax/config errors.
   - **Interpretation:** Orchestrator control flow is most fragile when it depends on long-lived child workflows without explicit heartbeats or intermediate status outputs.
   - **Smallest safe mitigation:** standardize child status contracts and stop relying on long idle polling.

### Indicators to track next
- `review_autofix` cancellation ratio
- `test_and_mark_stable` child-watch timeout count
- `workflow_log_analysis` per-job timeout/cancel count
- skip ratio by workflow family
- short workflow queue wait vs execution time
- memory retrieve hit rate by role (`implementation` vs `reviewer`)

---

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Review/autofix compute dominates the pipeline**
   - **Stage:** implement/review loop
   - **Evidence:** `review_autofix` p95 **1,849s**; many successful runs landed between **24–31 minutes**.
   - **Type:** compute
   - **Fix:** thin comment-only path, reduce reviewer breadth, reserve expensive reasoning for high-risk diffs.

2. **Long child workflow watches amplify downstream slowness into parent failures**
   - **Stage:** review/autofix → release/test orchestration
   - **Evidence:** `test_and_mark_stable` failures at **4,788s** and **5,361s** were both watch-phase failures.
   - **Type:** retry/watch overhead + compute dependency
   - **Fix:** decompose long children and replace blind polling with explicit progress states.

3. **Workflow-log analysis audit stage is oversized**
   - **Stage:** validate/report/analyze
   - **Evidence:** `workflow_log_analysis` success **4,328s**, cancellation **5,264s**; parent failures depend on it.
   - **Type:** compute
   - **Fix:** deterministic pre-pass + smaller synthesis prompt.

4. **CI serializes cheap and expensive checks**
   - **Stage:** validate
   - **Evidence:** `ci` p50 **612s** and p95 **651s**, even for deterministic lint/self-test failures.
   - **Type:** compute
   - **Fix:** split and parallelize.

5. **Queueing hurts the short-control-plane workflows**
   - **Stage:** clarify/plan/respond/poll/status/promote/merge
   - **Evidence:** many **6–28s** workflows log hosted-runner wait as a dominant component.
   - **Type:** queueing
   - **Fix:** reduce no-op workflow invocations; keep runners free for critical work.

6. **Superseded/cancelled review runs waste partial work**
   - **Stage:** review/autofix
   - **Evidence:** **106 cancelled** `review_autofix` runs, including many that had already consumed several minutes.
   - **Type:** retry/cancellation overhead
   - **Fix:** stronger concurrency preemption and earlier skip/cancel decisions.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail runtime and cancellation churn
- `workflow_log_analysis` / `test_and_mark_stable` watch-timeouts
- `ci` 10-minute serial lint/test job

**Top failure modes**
- Parent workflow watch failures on long child workflows (`25150961704`, `25200104592`)
- Late CI failures from simple lint/classification issues (`25200848815`, `25151563308`)
- Implement bootstrap/source-staging failures (`25151206656`)
- Nightly validation self-test instability (`25200681719`)

**Highest-cost drivers**
- Multi-model, two-pass, `xhigh` reviewer panel on comment-only review paths
- Oversized `api-redundancy` Codex pass in workflow log analysis
- Repeated retries/context expansion without measurable prompt-cache effectiveness

**Top 3 prioritized actions**
1. **Refactor `review_autofix` comment-only path** to use a smaller reviewer stack and lower reasoning unless risk heuristics trigger.
2. **Break `workflow_log_analysis/api-redundancy` into deterministic discovery + smaller synthesis** and stop solving failures with timeout inflation.
3. **Split CI into fast-fail lint/classification + parallel slower tests** so deterministic failures surface in under 2 minutes.

---

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 874 | 302 | 10 | 109 | 453 | 1.14% | 191.0 | 2.0 | 650.0 |

### Key workflow family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 94 | 88 | 6 | 0 | 0 | 6.38% | 606.2 | 612.0 | 651.1 |
| review_autofix | 160 | 54 | 0 | 106 | 0 | 0.00% | 424.2 | 39.0 | 1849.1 |
| test_and_mark_stable | 3 | 1 | 2 | 0 | 0 | 66.67% | 4335.0 | 4788.0 | 5303.7 |
| workflow_log_analysis | 2 | 1 | 0 | 1 | 0 | 0.00% | 4796.0 | 4796.0 | 5217.2 |
| clarify | 133 | 11 | 0 | 0 | 122 | 0.00% | 12.9 | 1.0 | 95.0 |
| plan | 119 | 9 | 0 | 0 | 110 | 0.00% | 14.2 | 1.0 | 149.4 |
| implement | 119 | 13 | 1 | 2 | 103 | 0.84% | 27.9 | 1.0 | 207.1 |
| orchestrate_clarify_respond | 119 | 2 | 0 | 0 | 117 | 0.00% | 1.3 | 1.0 | 2.0 |
| orchestrate_poll | 35 | 35 | 0 | 0 | 0 | 0.00% | 55.2 | 46.0 | 97.2 |
| issue_pr_status | 17 | 17 | 0 | 0 | 0 | 0.00% | 27.8 | 15.0 | 69.2 |
| cancel_on_pr_close | 17 | 17 | 0 | 0 | 0 | 0.00% | 10.3 | 11.0 | 15.0 |

### Notable failing runs

| Run ID | Date (UTC) | Family | Duration (s) | Failure point |
|---|---|---|---:|---|
| 25200104592 | 2026-05-01 02:57 | test_and_mark_stable | 5361 | orphan-workflows-test / Dispatch & watch — workflow-log-analysis |
| 25150961704 | 2026-04-30 06:30 | test_and_mark_stable | 4788 | e2e-smoke-test / Phase 4: Wait for review & autofix to complete |
| 25200848815 | 2026-05-01 03:28 | ci | 629 | lint / Python lint (ruff) |
| 25151563308 | 2026-04-30 06:48 | ci | 567 | lint / Validation self-test unit tests |
| 25151206656 | 2026-04-30 06:37 | implement | 476 | implement / Run Codex implementation |
| 25200681719 | 2026-05-01 03:21 | nightly_validation_selftest | 102 | validation-selftest / Run validation self-test matrix |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events | 25 |
| Retrieve events | 6 |
| Retrieve hit rate | 16.7% |
| Avg estimated retrieve tokens | 4.7 |
| `keyword_method=plain` | 1 |
| `keyword_method=none` | 5 |
| `keyword_method=llm` | 0 |
| Zero-record retrieves | 5 |
| `fail_open=true` retrieves | 0 |
| `enabled=false` retrieves | 0 |
| Max push attempts observed | 1 |

### Prompt/cache telemetry availability

| Metric | Status | Notes |
|---|---|---|
| Prompt/completion token totals | Not observed in sampled deep-dive logs | Model env vars visible, but no per-run totals |
| Prompt cache enabled flag | Observed | `OPENROUTER_PROMPT_CACHE_DISABLED=false` |
| Prompt cache hit/miss | Not observed | No usable hit-rate counters emitted |
| `cache_creation_input_tokens` | Mentioned in instrumentation/docs | No sampled numeric values surfaced for audit |
| `cache_read_input_tokens` | Mentioned in instrumentation/docs | No sampled numeric values surfaced for audit |
| AI memory telemetry | Observed | Mostly implement/review/memory-maintenance runs |
| uv cache hits | Observed | Seen in slow review runs |
| uv cache key hygiene issue | Observed | Implement run restored `...no-dependency-glob` cache after no matches |

### GH API observed signal summary

| Signal | Observed count* | Where it showed up | Main concern |
|---|---:|---|---|
| `gh api` REST command occurrences | 871 | release tests, implement, status sync, analysis | High control-plane chatter, repeated polling |
| `gh api graphql` occurrences | 31 | issue/status/review linked-issue lookups | Repeated linked-issue resolution |
| `closingIssuesReferences` occurrences | 78 | review/status/implement flows | Similar GraphQL queries repeated across workflows |
| `/actions/runs/<id>/artifacts` occurrences | 40 | reviewer, selftest, release/reporting flows | Repeated artifact inventory fetches |
| Rate-limit related occurrences | 87 | cancel flow, some review logs | Mostly defensive probes, not actual 429 incidents |
| PR head lookup occurrences | 15 | review/release watcher paths | Open-PR lookup loops |

\*These are log-observed command/signal occurrences in the sampled workflow-log corpus, not authoritative API billing counters.

### Token/model usage observations

| Workflow family | Models observed in logs | Notes |
|---|---|---|
| review_autofix | `openai/gpt-5.3-codex`, `openai/gpt-5.4-mini`, reviewer panel models from 6 providers | `xhigh` reviewer reasoning observed |
| implement | `openai/gpt-5.3-codex` | `MODEL_REASONING_EFFORT=xhigh` |
| workflow_log_analysis | `openai/gpt-5.4` mentioned in downstream PR body/log context | Longest audit path |
| issue_pr_status / cancel / CI | No usable token totals observed | Mostly control-plane work |

If you want, I can turn this into a **prioritized implementation checklist** mapped to specific workflow files and likely edit locations next.
