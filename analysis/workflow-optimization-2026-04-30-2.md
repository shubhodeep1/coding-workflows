## Executive Summary

- **`review_autofix` is the biggest critical-path delay.** Failed runs `25115530167` and `25127054791` spent **22-25 polling cycles at 20s each** waiting on sibling check-runs before doing useful work, adding roughly **7-8 minutes of pure latency** inside runs that still failed at **1,508s** and **1,614s**. **Estimated impact:** 5-8 min faster per affected review run. **Confidence:** high.
- **The pipeline is over-dispatching workflows that immediately skip or get cancelled.** In 1,000 sampled runs, `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all have **p50 = 1s** with very high skipped/other counts, while `review_autofix` shows **84 cancelled** runs in the family aggregate and recent runs include quick cancel/restart churn. **Estimated impact:** meaningful GH API/load reduction plus moderate end-to-end speedup. **Confidence:** high.
- **Deterministic failures are escaping into expensive lanes.** CI runs `25144143440` and `25144388441` both burned about **10.5 minutes** before failing `ruff` on six `E101` indentation errors in `scripts/mcp_handshake_probe.py:125-130`. **Estimated impact:** save ~10 min on bad AI-generated changes and reduce noisy reruns. **Confidence:** high.
- **Release smoke is failing late on known review/bait synchronization issues.** `test_and_mark_stable` failed in runs `25147636528` (**4,404s**) and `25126757724` (**4,270s**) at the bait-line verification path; the recent `issue_pr_status` log documents the stale-success/root-SHA mismatch failure mode and the need to pin wait-review to the bait SHA. **Estimated impact:** 30-70 min saved across repeated release-smoke failures. **Confidence:** high.
- **AI memory is operational but low-yield.** Across observed `AI_MEMORY_TELEMETRY`, retrieve hit rate was **43.8% (7/16)**, **9/16 retrieves returned 0 records**, and average selected context was only **12.2 estimated tokens**. No `fail_open: true` or `enabled: false` entries appeared. **Estimated impact:** modest quality/token improvement after tuning. **Confidence:** high.
- **Prompt-cache usage is enabled but not measurable.** Logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED=false`, yet sampled runs do **not** emit hit/miss or prompt/completion totals, so token cost cannot be audited precisely from this window. **Estimated impact:** medium cost savings once instrumented. **Confidence:** medium.

## Speed Optimizations

### 1. Shorten or bypass `review_autofix` check-run waiting
**Type:** critical-path win

- **Evidence:**  
  - Failed run `25127054791` logged **25** `Waiting for ... check-run(s)` messages from `18:39:19Z` to `18:47:08Z`.  
  - Failed run `25115530167` logged **22** such waits from `14:44:39Z` to `14:51:29Z`.  
  - Slow success `25143964823` also logged **25** waits before progressing.
- **Root cause:** `review_autofix` blocks on sibling check-runs for the same SHA, even when the later resolution still fails in conflict handling.
- **Exact change:**  
  - Lower the wait timeout from the current 1,200s path for review-context collection.  
  - Exit early once enough context is available, marking the snapshot as partial.  
  - Keep the current full wait only for cases that truly require all check-runs to settle.
- **Estimated time savings:** **5-8 minutes** per long-tail review run.
- **Implementation risk:** **low-medium**. Risk is acting on partial CI context; mitigate by explicitly labeling the collected context as partial and preserving fail-open behavior.

### 2. Stop dispatching workflows that are expected to skip
**Type:** critical-path win

- **Evidence:**  
  - Workflow-family metrics show extreme skip/other volumes: `clarify` **166**, `plan` **147**, `implement` **137**, `orchestrate_clarify_respond` **159**.  
  - All four families have **p50 duration = 1s**.  
  - Recent windows around `04:43-04:49 UTC` show repeated skip fanout across the same issue lifecycle.
- **Root cause:** routing/gating happens after dispatch instead of before dispatch.
- **Exact change:**  
  - Move gating decisions into a single dispatch router step.  
  - Only trigger `clarify`, `plan`, `implement`, or `orchestrate_clarify_respond` when state requires that phase.
- **Estimated time savings:** modest per run, but large aggregate reduction in queue churn and side-work.
- **Implementation risk:** **medium**. Must preserve current routing semantics.

### 3. Fail release smoke earlier on bait/review mismatch
**Type:** critical-path win

- **Evidence:**  
  - `test_and_mark_stable` run `25147636528` failed after **4,404s** with `Editor failed to remove bait line E2E_EDITOR_BAIT_25147636528`.  
  - In that run, bait commit `91d6077` was pushed at `04:44:39Z`, but the workflow matched review run `25147811755`, which completed successfully before the editor removed the bait.  
  - Recent `issue_pr_status` run `25150955243` documents the same root cause and the fix direction: pin wait-review to `BAIT_SHA`, widen `per_page`, emit `status=no_review_triggered`.
- **Root cause:** wait-review selected the wrong review run by branch/event recency instead of by bait commit SHA.
- **Exact change:**  
  - Make the bait SHA pinning logic the default for smoke verification when bait injection succeeds.  
  - Fail fast with a distinct `no_review_triggered` status instead of letting the run limp into Phase 4b.
- **Estimated time savings:** **30-70 minutes** across repeated release-smoke retries.
- **Implementation risk:** **low**. Recent logs already support this behavior.

### 4. Prevent duplicate implement starts before dispatch
**Type:** critical-path + queue reduction

- **Evidence:**  
  - In E2E smoke run `25147636528`, the implement watcher logged growth from **0 total** to active implement runs, and the repo’s own workflow-log analysis flags duplicate implement starts as a recurring issue.  
  - Workflow-family metrics show `implement` has many skipped runs plus **7 failures** and **3 cancelled**.
- **Root cause:** idempotency is enforced too late, after workflow start.
- **Exact change:**  
  - Add an atomic “active implement exists for issue N” pre-dispatch check.  
  - Reuse one cycle-local state snapshot instead of dispatching and then discovering a duplicate.
- **Estimated time savings:** seconds to minutes per affected issue, plus lower queue contention.
- **Implementation risk:** **medium**. Needs race-safe semantics.

### 5. Preflight lint AI-generated Python before opening the expensive CI lane
**Type:** local optimization with high bad-run savings

- **Evidence:**  
  - CI failures `25144143440` and `25144388441` both ran **631s** and **614s** before `ruff` failed on six `E101` errors.
- **Root cause:** style/syntax validation happens only in full CI, not inside `implement`/`review_autofix`.
- **Exact change:**  
  - Run `ruff check` on changed Python files before AI workflows commit/push.
- **Estimated time savings:** ~**10 minutes** on each bad PR.
- **Implementation risk:** **low**.

## Cost Optimizations

### 1. Lower default reasoning effort on non-escalated implement/review runs
- **Evidence:** Workflow-log-analysis run `25147648667` reports sampled implement/review paths using `xhigh` reasoning and an expensive reviewer/editor stack, while many runs end in deterministic failures, skips, or cancellations.
- **Root cause:** high-cost reasoning is being used as the default rather than as an escalation path.
- **Exact change:**  
  - Default implement/review to `medium` or `high`.  
  - Escalate to `xhigh` only for merge-conflict, fingerprint-regression, or retry-budget-exhaustion cases.
- **Estimated savings:** likely the largest token/dollar reduction in AI-heavy paths.
- **Quality-risk notes:** low-medium; protect quality by keeping escalation triggers.

### 2. Stop repeated full-context expansion across retry passes
- **Evidence:**  
  - Implement failure logs such as `25143249766` repeatedly restate the same long diagnose/retry context and fixed guidance.  
  - Review failure logs similarly re-emit large repeated blocks.
- **Root cause:** retries appear to re-inject immutable issue/context instead of referencing a stable base plus deltas.
- **Exact change:**  
  - Freeze a stable prompt prefix once.  
  - Pass only failure deltas, changed-file lists, and compact retry instructions on later attempts.
- **Estimated savings:** likely **15-25%** token reduction on multi-pass implement/review runs.
- **Quality-risk notes:** low if the first-pass frozen context remains available.

### 3. Reduce cancelled review runs before expensive AI work begins
- **Evidence:**  
  - Workflow-family aggregate shows `review_autofix` with **84 cancelled** runs out of **131** total.  
  - Recent runs show a **7s** cancel (`25150905581`), then a **105s** cancel (`25150906643`), then a **33s** success (`25150955222`) on the same morning.
- **Root cause:** AI work is starting before peer-run/gate collisions are fully resolved.
- **Exact change:**  
  - Resolve active-peer and deterministic-skip checks before starting reviewer/editor work.  
  - Reuse the existing `AUTOFIX_PEER_CHECK` style guard earlier in the dispatch path.
- **Estimated savings:** substantial AI token savings from avoiding wasted cancelled runs.
- **Quality-risk notes:** low.

### 4. Make reviewer fanout adaptive instead of fixed
- **Evidence:** slow and failed review runs spend large time in orchestration/wait states, while some paths are clearly deterministic or doc-only.
- **Root cause:** reviewer count/complexity appears fixed even when branch risk is low.
- **Exact change:**  
  - Use a smaller reviewer set for doc-only, lint-only, or obvious deterministic fixes.  
  - Reserve full reviewer fanout for risky code changes or conflict cases.
- **Estimated savings:** moderate token savings on low-risk PRs.
- **Quality-risk notes:** medium; keep full fanout for nontrivial diffs.

### 5. Avoid paying AI costs on deterministic repository-state failures
- **Evidence:**  
  - Implement run `25143249766` ended after repeated retries and deterministic integration/fingerprint failure handling.  
  - CI failures `25144143440`/`25144388441` were deterministic style errors.
- **Root cause:** the system spends model tokens before detecting conditions a local/static precheck could catch.
- **Exact change:**  
  - Add pre-dispatch/pre-commit guards for fingerprint regressions, no-op ancestry caps, and touched-file linting.  
  - Bail before invoking high-cost AI steps when failure is deterministic.
- **Estimated savings:** moderate token and rerun savings.
- **Quality-risk notes:** low.

### Cost gaps
- **Token totals, prompt/completion split, and prompt-cache hit/miss were not emitted in the sampled logs.** Savings above are directionally strong but not dollar-quantified from this window.

## Reliability Improvements

### 1. Add pre-commit lint/format guards inside AI workflows
- **Failure evidence:** CI runs `25144143440` and `25144388441` both failed at `Python lint (ruff)` after ~10 minutes because `scripts/mcp_handshake_probe.py:125-130` had six `E101` mixed-indentation errors.
- **Root cause category:** deterministic generated-code defect escaping to CI.
- **Exact fix:** run `ruff check` on changed Python files before commit in `implement` and `review_autofix`; optionally auto-format before pushing.
- **Expected reliability impact:** high reduction in avoidable CI failures and reruns.
- **Rollback/fail-open considerations:** fail open if lint tooling setup breaks, but log loudly and preserve the current path.

### 2. Prevent duplicate implement starts before dispatch
- **Failure evidence:** sampled runs and repo analysis indicate duplicate implement starts; `implement` family also shows high skip churn and repeated failure/cancel cases.
- **Root cause category:** orchestration race / idempotency gap.
- **Exact fix:** add a single cycle-local active-run check before dispatching implement.
- **Expected reliability impact:** lower duplicate execution, less branch-state conflict, fewer no-op/reissue loops.
- **Rollback/fail-open considerations:** if the guard cannot determine active state, fail open and dispatch as today.

### 3. Fail fast on impossible bait/review synchronization states
- **Failure evidence:** `test_and_mark_stable` runs `25147636528` and `25126757724` failed late at bait verification after 4,270-4,404s.
- **Root cause category:** stale review-run selection / missing synchronize trigger.
- **Exact fix:** require `head_sha == BAIT_SHA` for the matched review run after bait injection; emit `no_review_triggered` distinctly when absent.
- **Expected reliability impact:** high for release-smoke correctness.
- **Rollback/fail-open considerations:** if bait injection is skipped, preserve legacy branch-based matching.

### 4. Harden review conflict-resolution no-progress detection
- **Failure evidence:** failed review runs `25115530167` and `25127054791` both ended after repeated waiting and conflict-resolution retry exhaustion (`Conflict resolver failed after retries`).
- **Root cause category:** retry loop continuing without enough incremental progress.
- **Exact fix:**  
  - Detect unchanged/no-progress retry deltas earlier.  
  - Abort to a deterministic recovery path sooner when attempts reproduce the same failure signature.
- **Expected reliability impact:** medium reduction in long failed reviews.
- **Rollback/fail-open considerations:** keep existing retry budget as fallback if the new no-progress detector is inconclusive.

### 5. Catch deterministic integration fingerprint regressions before full retry exhaustion
- **Failure evidence:** implement run `25143249766` ended after multiple attempts with deterministic failure and post-Codex diagnose handling.
- **Root cause category:** integration-state/fingerprint contract violation.
- **Exact fix:** run fingerprint verification earlier and short-circuit to human/escalation flow instead of burning all Codex attempts.
- **Expected reliability impact:** medium reduction in long implement failures.
- **Rollback/fail-open considerations:** preserve current diagnose path on verifier plumbing errors.

## AI Memory Health

- **Observed telemetry:** `67` `AI_MEMORY_TELEMETRY` records across the sampled logs.
- **Operations seen:**  
  - `record-run-event`: `31`  
  - `retrieve`: `16`  
  - `processed-command-check`: `7`  
  - `processed-command-claim`: `7`  
  - `record-candidate`: `6`
- **Operations not observed in this sample:** `finalize-task`, `promote`, `compact`, `processed-command-complete`.

### Retrieve effectiveness
- **Hit rate:** **43.8%** (`7/16` retrieves selected at least one record).
- **Zero-result retrieves:** **9/16**.
- **Average `estimated_tokens`:** **12.2**.
- **Average budget:** emitted as **0.0** in the sample, so effective budget-vs-usage cannot be assessed.
- **`keyword_method` distribution:**  
  - `none`: `9`  
  - `plain`: `7`  
  - `llm`: `0`

### Health flags
- **`fail_open: true` entries:** none observed.
- **`enabled: false` entries:** none observed.
- **High push retry counts:** one record had `push_attempts: 2` (`review_autofix` run `25127054791`), everything else sampled was 1.

### Interpretation
- Memory is **operationally healthy** but **retrieval quality is weak**, especially for reviewer paths. Reviewer retrieves in failed review runs often returned `0` records with `keyword_method: none`.
- The system appears better at ledgering and event recording than at retrieving useful context.

### Recommendations
1. Tune retrieval for reviewer flows first; that is where the miss rate is most visible.
2. Add explicit budget telemetry for retrieves so “small context because good filter” can be separated from “small context because nothing useful found.”
3. Track hit rate by workflow family (`implement` vs `review_autofix`) and by role (`reviewer` vs `implementation`).

## GH API Call Audit

**Important limit:** exact executed call counts were not emitted; the findings below are based on log-observed polling/retry behavior and the repo’s own API-redundancy analysis run `25147648667`.

### High-volume patterns

1. **`review_autofix` check-run polling**
   - **Evidence:** review runs `25115530167`, `25127054791`, and `25143964823` each logged **22-25** repeated check-run wait messages.
   - **Pattern:** repeated polling of check-run status on the same SHA.
   - **Recommendation:** shorten polling window, cache one snapshot per cycle, and stop waiting once partial context is good enough.
   - **Estimated reduction:** roughly **10-20 GH API calls** per slow review run.
   - **Rate-limit impact:** moderate improvement on bursty review traffic.

2. **`implement` issue metadata refetching**
   - **Evidence:** the repo’s API audit in run `25147648667` flags `.github/workflows/implement.yml:53-65` plus `:511-543` as a same-issue re-fetch path.
   - **Pattern:** early label fetch, later full issue fetch for the same issue.
   - **Recommendation:** fetch full issue JSON once, persist it, and derive labels/body/title from the cached payload.
   - **Estimated reduction:** **1 GH API call** per non-skipped implement run.
   - **Rate-limit impact:** low individually, meaningful at scale.

3. **`cancel_on_pr_close` duplicate run-list queries**
   - **Evidence:** the repo’s API audit flags separate queued and in-progress run-list calls for the same close event.
   - **Pattern:** two list-runs calls merged locally.
   - **Recommendation:** use a single run-list query and filter client-side.
   - **Estimated reduction:** **2 → 1** list-runs call per event.
   - **Rate-limit impact:** low, but safe and easy.

4. **`issue_pr_status` batched classification followed by per-issue re-fetch**
   - **Evidence:** recent run `25150955243` shows a GraphQL batch plus fallback logic, and the API audit flags later per-issue re-fetch in the merged-alert path.
   - **Pattern:** classify once, then refetch linked issues to answer a question already known earlier.
   - **Recommendation:** export orchestrator-managed classification into outputs/env and reuse it downstream.
   - **Estimated reduction:** **1 batched query + N extra issue GETs → 1 batched query**.
   - **Rate-limit impact:** meaningful on PRs with multiple linked issues.

5. **Smoke-test child workflow dispatch/watch loops**
   - **Evidence:** in `test_and_mark_stable` run `25147636528`, the orphan-workflows phase separately dispatched and watched `memory_maintenance`, `workflow-log-analysis`, `validation-refresh`, and `update_workflows`.
   - **Pattern:** repeated “dispatch → find run → poll until completion” blocks.
   - **Recommendation:** consolidate on one shared helper with cached prior-run detection and backoff.
   - **Estimated reduction:** moderate call-count reduction and less duplicate retry logic.
   - **Rate-limit impact:** medium during release/smoke runs.

### API hygiene against repo rules
The repo’s own guidance in the sampled logs is clear:
- **Mandatory batching where safe**
- **Cycle-local cache reuse**
- **Fail open on cache miss / probe failure**

Observed compliance is mixed:
- **Batching:** present in some GraphQL paths, inconsistent overall.
- **Cycle-local caches:** weak in `implement` and review orchestration paths.
- **Fail-open behavior:** generally present and should be preserved.

## MCP & Serena Efficiency

### What the logs show
- Serena is being initialized in AI workflows:
  - `review_autofix` run `25127054791`: Serena warm cache succeeded after setup; `GIT_MCP_DISABLED=true`.
  - `review_autofix` run `25143964823`: same pattern.
  - `orchestrate_poll` run `25150278035`: Serena support files are staged, but this path is mostly orchestration, not code analysis.
- In sampled AI workflow logs, **Git MCP is disabled**, so the system cannot benefit from the user-preferred targeted Git MCP calls.
- Recent review-cancel run `25150906643` spent time in:
  - `Capture runtime context`
  - `Log token usage and Serena stats`
  - `Generate Serena efficiency report`
  even though the run was later cancelled.

### Efficiency assessment
- **Good:** Serena setup is standardized and cached at the uv layer.
- **Not good:** setup/report overhead still occurs on runs that are cancelled or quickly skipped.
- **Biggest gap:** this sample lacks detailed per-call Serena traces, so I cannot prove broad raw-file reads, repeated region reads, or symbol-vs-file inefficiency from the current window alone.

### Recommendations
1. **Move cancellation/peer-run gating ahead of Serena startup**
   - Prevent paying Serena boot/report overhead on runs that will cancel or skip.
   - **Impact:** small per run, large aggregate in `review_autofix`.

2. **Enable Git MCP in review/edit paths if operationally safe**
   - Current logs show `GIT_MCP_DISABLED=true`, which blocks the most targeted diff/status/history queries.
   - **Impact:** lower token usage and less file-context churn in edit/review flows.
   - **Risk:** medium; only if the Git MCP environment is stable.

3. **Skip Serena efficiency report generation on clearly non-productive runs**
   - Example: cancelled runs under a short threshold.
   - **Impact:** small latency and token savings, very low risk.

4. **Collect actual Serena call telemetry**
   - Without per-call traces, MCP efficiency recommendations stay partly inferential.
   - **Impact:** measurement improvement; enables precise optimization.

## Prompt Cache & Memory System

### Observed behavior
- `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears consistently in sampled implement/review/orchestrator logs.
- Prompt-cache **enablement** is visible; prompt-cache **effectiveness** is not.
- No sampled logs emitted:
  - prompt cache create/read counts
  - hit/miss percentages
  - prompt/completion token totals
- GitHub Actions cache behavior is mixed:
  - `workflow_log_analysis` run `25147648667` restored `codex-v0.114.0` successfully.
  - `review_autofix` run `25143964823` had a uv cache miss.
  - `implement` run `25143404687` hit a `setup-uv` cache key, but it was only about **1 KB**, suggesting low practical reuse.

### Assessment
- Prompt-cache fragmentation is likely being caused by:
  - repeated long dynamic issue bodies
  - run IDs, timestamps, URLs, and volatile diagnostics inserted high in the prompt
  - repeated retry preambles instead of stable-prefix + delta structure
- Memory retrieval is healthy enough operationally to keep enabled, but not strong enough yet to materially shrink prompt payloads.

### Recommendations
1. **Stabilize the prompt prefix**
   - Keep durable instructions and repo rules fixed at the front.
   - Push dynamic run-specific noise into a tail section.
   - **Impact:** likely better cache reuse and lower latency.

2. **Retry with deltas, not full reconstructions**
   - Reuse the initial prompt body; add only failure deltas for later attempts.
   - **Impact:** lower token spend and less cache fragmentation.

3. **Emit prompt-cache telemetry explicitly**
   - Add create/read/hit/miss counters and per-step prompt/completion totals.
   - **Impact:** enables exact cost tuning; current window cannot quantify savings.

4. **Track cache fragmentation by workflow family**
   - `implement` and `review_autofix` should be the first focus areas.
   - **Impact:** medium.

## Orchestrator Health

### Observed health signals
- **High skip churn:** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all have p50 around **1s** with very large skip/other counts.
- **Poller cadence is stable:** `orchestrate_poll` averages **44.0s**, p95 **55.2s**, and the recent run `25150278035` completed cleanly in **47s**.
- **Memory event logging is healthy:** `orchestrate_poll` emitted `poll_started` and `poll_completed` memory records successfully.
- **Duplicate/overlapping starts remain a pain point:** recent review runs show cancel/restart churn within minutes.

### Recurring pain points
1. Dispatch-first, decide-later orchestration.
2. Duplicate implement/review starts from race conditions.
3. Long waits in review/autofix before useful work.
4. Release smoke depending on brittle downstream synchronization.

### Smallest safe mitigations
- Add one canonical pre-dispatch gate that decides whether downstream workflows should run.
- Add one canonical active-run-exists guard for implement/review.
- Keep poller fail-open behavior, but reduce redundant child workflow dispatch/watch logic.

### Indicators to track next
- `review_autofix` wait-loop count per run
- skipped-dispatch ratio by workflow family
- average implement runs started per approved issue
- count of `no_review_triggered` smoke outcomes
- count of peer-run cancellations before Serena/model startup

## Pipeline Flow Bottlenecks

### 1. Review/autofix wait-loop overhead
- **Stage:** review/autofix
- **Type:** compute idle + polling overhead
- **Evidence:** 22-25 wait-loop cycles in long review runs.
- **Fix order:** first.

### 2. Workflow fanout that becomes immediate skips
- **Stage:** clarify → plan → implement → respond routing
- **Type:** queueing + orchestration waste
- **Evidence:** p50=1s and high skip counts across four workflow families.
- **Fix order:** second.

### 3. Duplicate implement starts during handoff
- **Stage:** plan → implement
- **Type:** retry/race overhead
- **Evidence:** repeated implement churn in sampled runs and repo analysis.
- **Fix order:** third.

### 4. Late deterministic CI failures
- **Stage:** implement/review → CI
- **Type:** compute waste
- **Evidence:** 10-minute CI failures on trivial lint defects.
- **Fix order:** fourth.

### 5. Merge/conflict and fingerprint regressions
- **Stage:** implement + review/autofix
- **Type:** retry overhead
- **Evidence:** implement run `25143249766` exhausted retries; failed review runs ended after conflict retry exhaustion.
- **Fix order:** fifth.

### 6. Release-smoke serialism
- **Stage:** validate/release smoke
- **Type:** serial orchestration overhead
- **Evidence:** `test_and_mark_stable` failures at 3,677-4,404s.
- **Fix order:** sixth, but high value because failures are so expensive.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail runtime from check-run polling and conflict-resolution retries.
- Heavy skip/cancel churn across AI phase workflows.
- Very slow `test_and_mark_stable` failures from bait/review orchestration issues.

**Top failure modes**
- Late CI lint failures (`25144143440`, `25144388441`).
- Late release-smoke bait verification failures (`25147636528`, `25126757724`).
- Implement failures after retry exhaustion and deterministic integration-state issues (`25143249766`).
- Review failures after long wait + conflict retry loops (`25115530167`, `25127054791`).

**Highest-cost drivers**
- High-reasoning AI passes on runs that cancel, skip, or fail deterministically.
- Repeated full-context retries in implement/review.
- Expensive review/autofix runs that spend minutes waiting before acting.

**Top 3 prioritized actions**
1. **Cut `review_autofix` wait-loop time** and allow partial-context execution earlier.
2. **Preflight lint changed Python files** inside implement/review before push.
3. **Move dispatch gating before downstream workflow fanout** and add active-run dedupe for implement/review.

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 286 | 15 | 88 | 611 | 1.5% | 151.56 | 1.0 | 632.0 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other/Skipped | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 81 | 79 | 2 | 0 | 0 | 605.5 | 609.0 | 639.0 |
| implement | 162 | 15 | 7 | 3 | 137 | 42.2 | 1.0 | 235.4 |
| plan | 162 | 15 | 0 | 0 | 147 | 30.4 | 1.0 | 179.8 |
| clarify | 184 | 18 | 0 | 0 | 166 | 13.3 | 1.0 | 94.9 |
| orchestrate_clarify_respond | 162 | 3 | 0 | 0 | 159 | 1.6 | 1.0 | 6.7 |
| review_autofix | 131 | 45 | 2 | 84 | 0 | 394.0 | 30.0 | 1843.5 |
| orchestrate_poll | 33 | 33 | 0 | 0 | 0 | 44.0 | 44.0 | 55.2 |
| orchestrate | 5 | 5 | 0 | 0 | 0 | 439.2 | 270.0 | 750.2 |
| test_and_mark_stable | 3 | 0 | 3 | 0 | 0 | 4117.0 | 4270.0 | 4390.6 |
| workflow_log_analysis | 3 | 2 | 0 | 1 | 0 | 3848.3 | 3916.0 | 3982.6 |

### Notable failed runs

| Run ID | Family | Duration (s) | Failure point |
|---|---|---:|---|
| 25147636528 | test_and_mark_stable | 4404 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25126757724 | test_and_mark_stable | 4270 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25115169454 | test_and_mark_stable | 3677 | orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project |
| 25127054791 | review_autofix | 1614 | review / codex-agent / Run Codex resolver, validate, stage, commit |
| 25115530167 | review_autofix | 1508 | review / codex-agent / Run Codex resolver, validate, stage, commit |
| 25143404687 | implement | 658 | implement / implement / Run Codex implementation |
| 25144143440 | ci | 631 | lint / Python lint (ruff) |
| 25144388441 | ci | 614 | lint / Python lint (ruff) |
| 25145624630 | nightly_validation_selftest | 94 | validation-selftest / Run validation self-test matrix |

### Sampled latency hotspot details

| Run ID | Family | Observed hotspot | Measured signal |
|---|---|---|---|
| 25127054791 | review_autofix | Check-run waiting | 25 wait-loop lines |
| 25115530167 | review_autofix | Check-run waiting | 22 wait-loop lines |
| 25143964823 | review_autofix | Check-run waiting | 25 wait-loop lines |
| 25147636528 | test_and_mark_stable | Bait verification mismatch | Failed after bait commit `91d6077` with bait still present |
| 25144143440 | ci | Late lint failure | 6 `E101` errors in `scripts/mcp_handshake_probe.py:125-130` |
| 25144388441 | ci | Late lint failure | 6 `E101` errors in `scripts/mcp_handshake_probe.py:125-130` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry records | 67 |
| Retrieve operations | 16 |
| Retrieve hit rate | 43.8% |
| Zero-result retrieves | 9 |
| Avg estimated tokens per retrieve | 12.2 |
| Avg budget tokens per retrieve | 0.0 emitted |
| `keyword_method=none` | 9 |
| `keyword_method=plain` | 7 |
| `keyword_method=llm` | 0 |
| `fail_open: true` entries | 0 |
| `enabled: false` entries | 0 |
| Records with `push_attempts > 1` | 1 |

### Prompt/cache/token telemetry availability

| Metric | Availability | Notes |
|---|---|---|
| Prompt token totals | Not observed | No prompt/completion/total counts in sampled logs |
| Prompt cache hit/miss | Not observed | Cache enabled flag present, but no usage counters |
| OpenRouter prompt cache enabled flag | Observed | `OPENROUTER_PROMPT_CACHE_DISABLED=false` in sampled AI runs |
| GitHub Actions cache hit | Observed | `codex-v0.114.0` restored in workflow-log-analysis run `25147648667` |
| uv cache miss | Observed | review run `25143964823` |
| uv cache hit but near-empty | Observed | implement run `25143404687` restored ~1 KB key |

### GH API hotspot summary

| Workflow/step | Evidence | Approx reduction if fixed |
|---|---|---:|
| `review_autofix` check-run polling | 22-25 repeated wait loops in long runs | 10-20 calls per slow run |
| `implement` issue metadata refetch | Same issue fetched early and later | 1 call per non-skipped implement |
| `cancel_on_pr_close` queued + in-progress list-runs split | Repo audit flags 2 list calls for one event | 1 call per PR-close event |
| `issue_pr_status` batch then per-issue refetch | Batch classification plus later N issue GETs | N calls per linked-issue PR |
| Release smoke dispatch/watch duplication | Four child workflow pollers in one phase | Moderate, especially in slow smoke runs |

If you want, I can turn this into a **prioritized implementation backlog** with estimated effort, owner type, and rollout sequence.

## Deep Audit — Workflows & Scripts (2026-04-30)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path**: `.github/workflows/implement.yml:53-65`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: The precheck gate decides whether to skip implementation by reading labels from `GET /issues/{ISSUE_NUMBER}`, but it does not mutate the issue atomically. A second `/approved` event can queue behind the same concurrency group and read the stale `ai:awaiting-approval` state before the first run reaches the later label swap at lines 626-628. Because `cancel-in-progress: false` is set for the job concurrency group at lines 42-44, the second run will still start later and execute expensive setup steps before it discovers the issue is already implementing or already has an open PR. This is a TOCTOU correctness gap rather than just a cost issue.  
- **Recommended fix**: Move the `ai:awaiting-approval` → `ai:implementing` transition into the earliest gate step and make it resilient/atomic using the same label-swap pattern already used elsewhere (`set_issue_phase_label_resilient` in `scripts/label_helpers.sh` or `_resilient_phase_swap` in `scripts/review_rb_judge.sh`). That lets queued follow-on runs observe terminal state immediately and skip before setup.

#### API-001
- **File path**: `.github/workflows/implement.yml:53-65, .github/workflows/implement.yml:511-543`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The workflow fetches the same issue twice on the normal execution path: first at line 58 to read labels, then again at line 520 to write the full issue payload into `ISSUE_META_FILE`. Both calls hit `repos/{repo}/issues/{ISSUE_NUMBER}`. Current call count is **2** for the same resource before any implementation work begins.  
- **Recommended fix**: Fetch the full issue JSON once in the precheck step, persist it to `${ISSUE_META_FILE}`, and derive labels/body/title from that cached file in later steps. Proposed call count: **2 → 1**. Reuse the repo’s existing “cache one JSON payload and fan out fields with jq” pattern from `review_autofix.yml:1351-1365`.

#### API-002
- **File path**: `.github/workflows/implement.yml:567-578`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: The approval-comment fetch loop calls `gh api --paginate "repos/.../issues/${ISSUE_NUMBER}/comments?per_page=100"` up to **5 times** while polling for the same comment body. This step refetches the full comment history each attempt instead of reusing data already available in the triggering event (`github.event.comment.id`) or narrowing the query. Current call count is **up to 5 REST list calls** for one issue in a single step.  
- **Recommended fix**: Cache the first successful comments payload and only retry on transport failure, or query the specific triggering comment once via `repos/{repo}/issues/comments/{comment_id}`. Proposed call count: **5 → 1** on the common path. If batching is still needed, extend the “single fetch then jq on cached JSON” pattern used in `gh_helpers.sh` / `review_autofix.yml`.

#### API-003
- **File path**: `.github/workflows/implement.yml:2243-2249`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: The ancestor no-op detector performs per-hop API calls inside a loop: one `GET /issues/{current}` plus one paginated `GET /issues/{parent}/comments` for each ancestor. With the default threshold of 2, current call count is already **up to 4 calls**; higher thresholds scale linearly. This is explicitly the kind of per-iteration GH API pattern called out by the repo’s API hygiene rules.  
- **Recommended fix**: Replace the loop with one batched GraphQL query over the ancestor issue numbers, or prefetch the necessary issue bodies/comments in one alias-based GraphQL request. Proposed call count after batching: **4 → 1** for threshold 2, or **2N → 1** generally. Extend the alias-batching pattern already used in `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql()`.

#### API-004
- **File path**: `.github/workflows/review_autofix.yml:500-530`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The post-merge validate-dispatch step already fetches linked issue numbers and labels in one GraphQL query at lines 478-483, but then falls back to `gh issue view --json labels` inside the loop whenever `labels_known != "true"` (lines 503-509). That creates per-issue REST refetches even though the step already has the issue list in `issue_nodes_json`, and the fallback path reconstructs `issue_nodes_json` without labels at line 495. Current call count is **1 GraphQL + N issue view calls** on fallback.  
- **Recommended fix**: When GraphQL fails, fetch issue numbers and labels together in one batched GraphQL alias query instead of emitting `labels: null` placeholders, or enrich the fallback payload once before the loop. Proposed call count: **1 + N → 1**. Reuse `gh_pr_with_all_comments`/GraphQL-first fallback strategy from `scripts/gh_helpers.sh`.

#### API-005
- **File path**: `.github/workflows/review_rb_judge_dispatch.yml` via callers `scripts/orchestrate_poll_process.sh:7468-7534, 7593-7599`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: The review-blocked recovery path first checks for active review runs by iterating candidate workflow names with `gh run list` (lines 7468-7476), then separately dispatches `review_rb_judge_dispatch.yml` (line 7593). This keeps run-list polling inside loops and splits existence detection across multiple CLI calls per candidate workflow. Current call count is **up to 3 `gh run list` calls + 1 dispatch** per recovery attempt.  
- **Recommended fix**: Collapse active-run detection into one cached run-list query for all relevant workflow files, then filter client-side before dispatch. Proposed call count: **4 → 2**. Extend the cached run-inventory pattern already used in `scripts/orchestrate_poll_process.sh` for branch/PR sweeps.

### Section 2: GitHub API Call Redundancy Audit

#### API-006
- **File path**: `.github/workflows/review_autofix.yml:1351-1386, .github/workflows/review_autofix.yml:2986-3001`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The “Collect PR metadata” step fetches linked issues via GraphQL and exports `LINKED_ISSUES_JSON` at lines 1381-1406, but a later “Cache linked issues references” step still exists in the same workflow path and is documented in comments as a now-redundant fetch target. Current logical call count on the legacy path is **2 linked-issue fetches** for the same PR; the earlier step already supersedes the later numbers-only fetch.  
- **Recommended fix**: Remove the later fetch path entirely and make downstream consumers read only the cached env/file output from the earlier step. Proposed call count: **2 → 1**. Existing batching helper to extend: the GraphQL linked-issue fetch already in `review_autofix.yml:1381-1386`.

#### API-007
- **File path**: `scripts/review_rb_judge.sh:146-207`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The judge first fetches linked issues via GraphQL (`closingIssuesReferences`) at lines 146-151, then performs one REST fetch per linked issue body in the loop at lines 161-169, and separately fetches PR diff/comments/review comments at lines 191-207. For a PR linked to N issues, current call count is **1 GraphQL + N issue GETs + 3 PR-context calls** before prompt construction. The first loop only preserves the first issue’s body for the prompt.  
- **Recommended fix**: Fold each linked issue’s title/body into the initial GraphQL query and eliminate the per-issue REST loop. Proposed call count: **(1 + N + 3) → 4**. Extend the existing alias-batching or enrich the `closingIssuesReferences` query the same way `review_autofix.yml:1381-1386` already fetches `number title body`.

#### API-008
- **File path**: `scripts/orchestrate_poll_process.sh:6328-6367`
- **Severity**: High
- **Category tag**: `api-batching`
- **Description**: `run_standalone_stall_recovery` still uses seven separate `gh issue list --label ...` calls in a loop (lines 6351-6354) to build `labeled_issues`, then separately calls `_fetch_standalone_marker_issues_graphql()` and `_fetch_candidate_issue_details_graphql()`. Current call count for label discovery alone is **7 REST list calls**, all over the same open-issue population, before any candidate details query runs.  
- **Recommended fix**: Replace the per-label loop with a single GraphQL search query using aliases for all pipeline labels, or one `gh issue list --json number,labels --limit 1000` call filtered locally. Proposed call count: **7 → 1** for label discovery. Existing batching pattern to extend: `_fetch_standalone_marker_issues_graphql()` and `_fetch_candidate_issue_details_graphql()` in the same script.

#### API-009
- **File path**: `.github/workflows/test-and-mark-stable.yml:2593-2823, .github/workflows/test-and-mark-stable.yml:3072-3115`
- **Severity**: High
- **Category tag**: `api-batching`
- **Description**: The smoke workflow repeats the same dispatch/watch pattern for `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, and `internal-validate`: each block does one “PRE” run-list call, one dispatch, one polling loop on `actions/workflows/{WF_FILE}/runs`, and one polling loop on `actions/runs/{NEW_ID}`. Current call count is roughly **4+ API calls per child workflow before polling repeats**, multiplied across at least 5 child workflows.  
- **Recommended fix**: Extract one shared dispatcher/watcher helper that accepts workflow name, expected conclusions, timeout, and inputs, then caches the latest run inventory per workflow family between attempts. Proposed call count per child: **~4 + polls → ~2 + polls**, plus lower duplication risk. Existing pattern to extend: the repo already centralizes GH retry logic in `scripts/gh_helpers.sh`; add a generic `dispatch_and_wait_workflow` helper there or in a dedicated script.

#### API-010
- **File path**: `.github/workflows/issue_pr_status.yml:295-338`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: The workflow batches orchestrator issue classification in one GraphQL query at lines 295-320, but if that query is incomplete or transiently fails it falls back to a per-issue REST loop (lines 327-338). Because the fallback is inside a `while read` over all linked issues, current call count becomes **1 GraphQL + N REST issue fetches** on a degraded path.  
- **Recommended fix**: Preserve the GraphQL alias approach on retry rather than downgrading all the way to per-issue REST, or batch the failed subset only. Proposed call count: **1 + N → 1 or 2**. Extend the alias-batching pattern already present in this step.

#### API-011
- **File path**: `scripts/gh_helpers.sh:760-899`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: `gh_pr_with_all_comments` uses GraphQL for the happy path, but any `hasNextPage` on PR comments, reviews, or nested review comments forces a full REST parity fallback at lines 846-898. On large PRs that means one GraphQL request followed by **2 or 3 paginated REST calls** for the same context. [NEEDS VERIFICATION] The fallback is safe, but it may be paying both query families on common large-review paths.  
- **Recommended fix**: Add GraphQL pagination support for comments/reviews before falling back to REST, or reuse already-fetched `preloaded_meta_json` and only REST-fetch the overflowing edge. Proposed call count for large PRs: **1 + 2/3 → 1/2**. Existing batching pattern to extend: this helper itself.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path**: `scripts/orchestrate_poll_process.sh:1087-1142`, `scripts/label_helpers.sh:108-170`, `scripts/review_rb_judge.sh:57-71`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: The repo maintains three separate `ensure_label_exists` implementations: the canonical helper in `scripts/label_helpers.sh`, an orchestrator-local variant with process-lifetime caching in `scripts/orchestrate_poll_process.sh`, and an inline fallback in `scripts/review_rb_judge.sh`. These copies already diverge in return semantics and label catalog sourcing.  
- **Recommended fix**: Move the caching behavior into `scripts/label_helpers.sh` and expose a single function signature like `ensure_label_exists <label_name> [repo]`. Update callers in `orchestrate_poll_process.sh` and `review_rb_judge.sh` to source the shared helper and drop their local copies/fallback bodies.

#### DUP-002
- **File path**: `.github/workflows/test-and-mark-stable.yml:2593-2823, .github/workflows/test-and-mark-stable.yml:3072-3115`
- **Severity**: High
- **Category tag**: `duplication`
- **Description**: The smoke-test workflow contains at least five near-identical inline shell blocks for “dispatch workflow, discover new run ID, poll status until completed.” The blocks differ mainly by workflow filename, timeout, and acceptable conclusions. This duplication has already produced large workflow size and API repetition.  
- **Recommended fix**: Extract a shared script, e.g. `scripts/dispatch_and_watch_workflow.sh`, with signature `dispatch_and_watch_workflow <repo> <workflow_file> <timeout_secs> <inputs_json> <allowed_conclusions_csv>`. Update each smoke step to call the shared script and keep only per-phase parameters in YAML.

#### DUP-003
- **File path**: `.github/workflows/mark-stable.yml:192-233`, `.github/workflows/cancel_on_pr_close.yml:21-47`, `.github/workflows/orchestrate_poll.yml:61-89`, `.github/workflows/review_autofix.yml:1269-1307`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: Multiple workflows define local retry wrappers around `gh api` despite the existence of `scripts/gh_helpers.sh`. The implementations all re-create rate-limit wait logic with slight differences in retry caps and error handling.  
- **Recommended fix**: Standardize on `scripts/gh_helpers.sh` for all rate-limit-aware API retries. Shared module owner should remain `scripts/gh_helpers.sh` with function signatures `gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`. Update the listed workflow steps to source the helper instead of carrying local shell implementations.

#### DUP-004
- **File path**: `scripts/review_conflict_prepare.sh:1-80`, `scripts/review_conflict_resolve.sh:42-80`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: The conflict-prepare and conflict-resolve scripts explicitly document that their integration-sync short-circuit rules must stay mirrored. Both scripts also carry parallel setup/disptach guard logic for orchestrator integration conflict handling.  
- **Recommended fix**: Factor the shared orchestration bits into a dedicated helper module such as `scripts/review_conflict_helpers.sh` with functions like `is_integration_sync`, `dispatch_integration_judge_now`, and `stage_conflict_runtime_dirs`. Update both conflict scripts to source the new module so the contract cannot drift.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path**: `.github/workflows/test-and-mark-stable.yml:930-1251`
- **Severity**: High
- **Category tag**: `expression-limit`
- **Description**: The `Phase 4: Wait for review & autofix to complete` run block is approximately **18,970 characters** and still contains `${{ }}` interpolations, leaving only about **2,030 characters** of headroom before the 21,000-character hard runner limit. This step already carries large inline polling, bait-SHA diagnostics, and fallback messaging, so normal maintenance can push it over the threshold quickly.  
- **Recommended fix**: Extract the wait-review loop into `scripts/wait_for_review_autofix.sh` and pass PR number, branch, bait SHA, and timeout via env. That is the preferred mitigation because the body is operational logic, not static data.

#### EXPR-002
- **File path**: `.github/workflows/review_autofix.yml:1266-1588`
- **Severity**: High
- **Category tag**: `expression-limit`
- **Description**: The `Collect PR metadata` run block is approximately **16,437 characters** with `${{ }}` interpolation, leaving about **4,563 characters** of headroom. It includes a custom retry wrapper, PR payload assembly, linked-issue GraphQL fetch, comment hydration, Python inline transformers, and diff collection in one step. This is below the hard limit but above the repo’s 15,000-char medium-risk threshold.  
- **Recommended fix**: Split the step into two or three steps or extract the whole body to a script under `scripts/` (preferred). Candidate decomposition: `collect_pr_metadata.sh`, `build_linked_issue_context.sh`, and `build_pr_comment_context.sh`.

#### EXPR-003
- **File path**: `.github/workflows/validate.yml:188-481`
- **Severity**: Medium
- **Category tag**: `expression-limit`
- **Description**: The `Fetch workflow support files` run block is approximately **16,529 characters** with `${{ }}` interpolation, leaving about **4,471 characters** of headroom. The block has grown into a full remote-file staging program with fallback logic, optional assets, and template bootstrap.  
- **Recommended fix**: Extract it to `scripts/fetch_validate_support.sh` and keep only the workflow inputs in YAML. That also improves auditability and testability.

#### EXPR-004
- **File path**: `.github/workflows/orchestrate_clarify_respond.yml:840-1123`
- **Severity**: High
- **Category tag**: `expression-limit`
- **Description**: The long prompt-assembly run block in `orchestrate_clarify_respond.yml` is approximately **15,140 characters**, leaving about **5,860 characters** of headroom. It is already close to the medium/high risk band and appears to embed large prompt-construction logic inline.  
- **Recommended fix**: Move prompt assembly to an external script under `scripts/` or move bulky static literals into `prompts/` files and read them at runtime. External script is preferred because the block is procedural.

### Section 5: Cross-Cutting Concerns

#### SHELL-001
- **File path**: `scripts/orchestrate_poll_process.sh:10683-10684`
- **Severity**: Medium
- **Category tag**: `shellcheck`
- **Description**: ShellCheck reports **SC2086** on `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"`. Because `${ISSUE_NUMS}` is unquoted, word splitting and glob expansion can occur before `printf`, which can corrupt the sorted issue set when the variable contains unexpected whitespace or shell metacharacters.  
- **Recommended fix**: Quote the expansion (`printf '%s\n' "${ISSUE_NUMS}"`) or iterate from a newline-delimited file/array. This is a direct shell-safety fix and should be addressed even if current inputs are numeric.

#### SHELL-002
- **File path**: `scripts/review_commit_changes.sh:455`, `scripts/review_conflict_resolve.sh:853`
- **Severity**: Medium
- **Category tag**: `shellcheck`
- **Description**: ShellCheck reports **SC2086** for unquoted token/repository interpolation in `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. While GitHub tokens and repo slugs are normally constrained, unquoted expansion in a URL assignment is still a shellcheck violation and can misbehave with unexpected characters.  
- **Recommended fix**: Quote the variable portions inside the URL or build the URL in a separate variable with safe quoting before passing it to `git remote set-url`.

#### SHELL-003
- **File path**: `scripts/check_external_branch_advance.sh:180-188`
- **Severity**: Low
- **Category tag**: `shellcheck`
- **Description**: The script iterates with `for sha in ${self_subject_shas}; do`, which relies on word splitting over a space-delimited accumulator. That is fragile and ShellCheck-style unsafe if the accumulator ever contains accidental extra whitespace or a malformed token.  
- **Recommended fix**: Convert `self_subject_shas` to an array and iterate with `for sha in "${self_subject_shas[@]}"`, or stream newline-delimited SHAs through `while IFS= read -r sha`.

#### SHELL-004
- **File path**: `scripts/implement_diagnose_post_codex_failure.sh:150`
- **Severity**: Low
- **Category tag**: `shellcheck`
- **Description**: ShellCheck reports **SC2015** on `[ "${_attempt}" -lt 3 ] && sleep 4 || true`. Because `A && B || C` is not an if/else, `true` can run even when `sleep` fails after a true condition, masking errors and making intent harder to verify.  
- **Recommended fix**: Replace the construct with an explicit `if [ "${_attempt}" -lt 3 ]; then sleep 4; fi` block.

#### DEBT-001
- **File path**: `scripts/gh_helpers.sh:760-899`, `.github/workflows/review_autofix.yml:1269-1307`, `.github/workflows/cancel_on_pr_close.yml:21-47`, `.github/workflows/mark-stable.yml:192-233`
- **Severity**: Medium
- **Category tag**: `tech-debt`
- **Description**: Rate-limit handling is partly centralized in `scripts/gh_helpers.sh` but several workflows still embed bespoke retry loops. This inconsistency increases maintenance cost, makes API behavior harder to reason about, and has already led to mixed fallback semantics across the repo.  
- **Recommended fix**: Make `scripts/gh_helpers.sh` the single owner of GH retry/backoff policy and refactor inline wrappers out of workflows. Keep workflow YAML limited to sourcing the helper and invoking shared functions.

#### CONSIST-001
- **File path**: `scripts/orchestrate_poll_process.sh:1087-1142`, `scripts/validate_process.sh:541-595`
- **Severity**: Medium
- **Category tag**: `consistency`
- **Description**: Label mutation is implemented differently across major scripts: `validate_process.sh` resolves phase transitions via `scripts/ai_labels.py resolve-phase` and issues one combined `gh issue edit`, while `orchestrate_poll_process.sh` keeps its own label catalog/cache and sometimes does ad hoc add/remove calls. This inconsistency increases the chance of drift between phase semantics and actual label mutations.  
- **Recommended fix**: Consolidate on one shared phase-label mutation helper, ideally backed by `scripts/ai_labels.py` plus `scripts/label_helpers.sh`, and update orchestrator callers to consume the same contract.

#### DEAD-001
- **File path**: `scripts/cost_audit.py:1-140`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `scripts/cost_audit.py` has no repository references outside itself, and no workflow in `.github/workflows/` invokes it. Within this audit scope it appears to be unused operational code.  
- **Recommended fix**: Either wire it into a workflow/documented operator path or explicitly mark it as a manual-only utility in README/agents docs. If it is truly obsolete, remove it in a separate change after verification.

#### CONSIST-002
- **File path**: `.github/workflows/orchestrate.yml:31-33`
- **Severity**: Medium
- **Category tag**: `consistency`
- **Description**: The orchestrate workflow uses `concurrency.group: ai-orchestrate-${{ github.repository }}-${{ github.run_id }}`, which is effectively unique per run and therefore provides no deduplication. That is inconsistent with the repository’s other issue/PR keyed concurrency groups and undermines the documented goal of avoiding overlapping orchestrator starts.  
- **Recommended fix**: Key orchestrator concurrency to the tracking issue or repo-wide orchestrator lane instead of `github.run_id`, e.g. `ai-orchestrate-${{ github.repository }}` or a project/tracking-specific group, while preserving any required parallelism intentionally.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | API-008, API-009, DUP-002, EXPR-001 |
| Medium | 15 | BUG-001, API-001, API-002, API-003, API-004, API-005, API-006, API-007, API-010, API-011, DUP-001, DUP-003, DUP-004, EXPR-002, CONSIST-001, CONSIST-002, DEBT-001, SHELL-001, SHELL-002 |
| Low | 4 | EXPR-003, SHELL-003, SHELL-004, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 5 | Medium |
| API call optimization | 8 | Large |
| Code modularization | 6 | Medium |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 9 | Small |
