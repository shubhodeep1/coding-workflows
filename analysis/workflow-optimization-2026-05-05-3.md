## Executive Summary

- **`test_and_mark_stable` is the highest-impact bottleneck and reliability problem.** All 4 observed runs failed (`run_ids`: 25324103531, 25347776357, 25369768571, 25375729485) and each consumed **~50–56 minutes** (`2999–3359s`) before failing in Phase 4b or Phase 7. The biggest win is to collapse the retry/poll logic and split or short-circuit late verification paths. **Estimated impact:** save **15–25 minutes per failed release validation** and materially reduce release-blocking false reds. **Confidence:** high.

- **`review_autofix` failures are dominated by unresolved merge conflicts after expensive editor work has already run.** The 3 deep-dive failures in this path (`25370025320`, `25370115370`, `25371432937`) all ended with `MERGE_CONFLICT=true` and `CONFLICT_RESOLVED=false`; `25371432937` explicitly shows `CONFLICT (content): Merge conflict in tests/e2e_smoke_canary.txt`. The family’s **p95 is 2054s**, and successful outliers still took **2263–2987s** (`25355461484`, `25353743396`). **Estimated impact:** cut conflicting-review runtime by **10–20 minutes** and reduce reruns/failures. **Confidence:** high.

- **GH API efficiency is acceptable at current scale, but there are clear high-redundancy patterns.** The worst path is Phase 4b retry polling in `test_and_mark_stable`; the repo’s own workflow-log analysis run (`25375766109`) flags this as a **50–80% reducible** read-heavy loop. Additional N+1/fallback patterns exist in `review_autofix` post-merge validate dispatch (`25378601655`) and `issue_pr_status` (`25378601664`). **Estimated impact:** reduce worst-case API reads by **50–80%** on failing release tests and cut rate-limit exposure. **Confidence:** high.

- **Prompt cache is enabled everywhere sampled, but its value is currently not measurable, and AI memory retrieval is mostly ineffective.** Deep-dive logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, yet no cache create/read counters are emitted. AI memory retrieval hit only **1 of 11 retrieves (9.1%)**, with **10 zero-record retrieves** and `keyword_method` almost always `none`. **Estimated impact:** medium token/latency savings and better memory usefulness once instrumentation and retrieval tuning are added. **Confidence:** medium.

- **The orchestrator is operationally healthy in the sense that it mostly fail-opens and records state, but it produces substantial no-op churn.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` generated large volumes of skipped/other runs (for example, `orchestrate_clarify_respond`: **127 of 131** were non-success no-op paths), while `review_autofix` saw **45 cancellations out of 82 runs**. **Estimated impact:** reduce queue noise and wasted runner starts with earlier routing/deduplication. **Confidence:** high.

## Speed Optimizations

### 1. Collapse `test_and_mark_stable` Phase 4b/7 polling and short-circuit late failure paths
**Priority:** Critical-path win

- **Evidence**
  - `test_and_mark_stable` has **4 total runs, 4 failures, 0 successes**, with `avg_duration_seconds=3200`, `p50=3221`, `p95=3350.6`.
  - Failures:
    - `25324103531` failed at **Phase 4b: Verify editor removed bait line** after **3303s**.
    - `25369768571` failed at **Phase 4b: Verify editor restored canary (pytest + retry)** after **3359s**.
    - `25375729485` failed at **Phase 7: Close PR and verify cancel_on_pr_close fires** after **2999s**.
  - In `25375729485`, the log shows repeated review-run polling for the same retry run from **12:24:00Z through 12:33:08Z** before completion.
  - The repo’s own workflow-log analysis run `25375766109` explicitly identifies the Phase 4b retry poll loop as polling both **PR state** and **workflow runs** every **15 seconds** until a long timeout, with a **50–80% API reduction opportunity**.

- **Root cause**
  - Phase 4b repeatedly re-fetches overlapping state during retry detection and validation.
  - Phase 7 verification remains inside the same long-running E2E job even after earlier phases already established failure.
  - The job mixes editor validation, retry orchestration, and cancel-on-close verification in one serial path.

- **Exact change**
  - When retry dispatch succeeds, persist the **retry run ID** once it appears and then poll only `actions/runs/{id}`.
  - Stop PR-state polling once the retry run is identified; re-check PR state only for the specific “closed during retry” branch.
  - Move Phase 7 `cancel_on_pr_close` verification into a separate follow-up job or mark it **idempotent-pass** when the PR is already closed and the cancel workflow is already observed.
  - Hard-stop downstream phases once Phase 4b reaches terminal bug classes like `spec_mismatch`, `retry_timeout`, or `retry_workflow_failed`.

- **Estimated time savings**
  - **900–1500s per failing release validation**.
  - Also reduces operator diagnosis time because the first meaningful failure becomes the terminal failure.

- **Implementation risk**
  - **Low to medium.**
  - The behavior is already logically segmented by status labels (`success_after_retry`, `retry_timeout`, etc.); this is mostly control-flow tightening, not semantic change.

---

### 2. Preflight merge conflicts before the expensive `review_autofix` editor loop
**Priority:** Critical-path win

- **Evidence**
  - `review_autofix` family metrics: **82 runs**, **33 success**, **4 failure**, **45 cancelled**, **p50=53s**, **p95=2054s**.
  - Deep-dive failures:
    - `25370025320`: ended with `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`, duration **1295s**.
    - `25370115370`: ended with `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`, duration **1836s**.
    - `25371432937`: ended with `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`, duration **637s**.
  - `25371432937` contains:
    - `CONFLICT (content): Merge conflict in tests/e2e_smoke_canary.txt`
    - `Conflict resolver: 1 unmerged path(s) to resolve in this run.`
    - `Conflict resolver retry 2/3`
    - final `MERGE_CONFLICT: true` / `CONFLICT_RESOLVED: false`
  - Successful but slow runs show the same family can consume **2263s** (`25355461484`) and **2987s** (`25353743396`) even without failing.

- **Root cause**
  - The pipeline spends substantial time on reviewer/editor work before discovering that the branch is not cleanly mergeable or that the narrow conflict resolver cannot resolve the hot file.
  - Conflict handling happens too late in the path.

- **Exact change**
  - Add an early mergeability precheck immediately after checkout, before reviewer/editor prompt generation.
  - If a deterministic merge conflict exists, route directly to:
    1. a narrow conflict-resolver step with the existing changed-file allowlist, or
    2. an immediate `ai:review-blocked` exit if the conflict is outside the safe resolver scope.
  - Skip full reviewer/editor execution until the branch is mergeable.

- **Estimated time savings**
  - **600–1200s on conflicting PRs**.
  - Also lowers queue pressure by reducing long tail runs.

- **Implementation risk**
  - **Low.**
  - This is an earlier use of logic the workflow already has.

---

### 3. Split claude-branch comment-only review mode into a lightweight workflow
**Priority:** Secondary critical-path win

- **Evidence**
  - Recent cancelled runs:
    - `25378337612` ran **296s** and was cancelled during `review / codex-agent (claude-branch-review)`.
    - `25378563105` ran **109s** and was cancelled while in the same comment-only branch-review mode.
  - The gate step logs explicitly note that in this path, **editor/commit/judge/auto-merge are skipped**, and that avoiding the full path cuts about **50%** of follow-up latency and skips about **7 LLM calls**.
  - Despite that, the workflow still enters the heavy codex-agent review job.

- **Root cause**
  - The claude-branch review mode is still implemented inside the main `review_autofix` job shape instead of dispatching a reduced reviewer-only path.

- **Exact change**
  - After gate evaluation, dispatch a dedicated lightweight reviewer-only workflow when `claude_branch_review == true`.
  - Reuse existing gate outputs and comment-only artifacts, but do not schedule the full editor-capable reusable workflow.

- **Estimated time savings**
  - **60–300s per follow-up/cancelled claude-branch run**.

- **Implementation risk**
  - **Medium.**
  - Requires workflow factoring, but semantics are already distinct in current logs.

---

### 4. Consolidate Copilot artifact cleanup into the producer path
**Priority:** Micro-optimization

- **Evidence**
  - Recent Copilot run `25378341064` completed in **187s**; the provided summary called out **Cleanup artifacts** as the longest visible step and logged `gh api /repos/.../actions/runs/25378341064/artifacts`.
  - Similar artifact-listing behavior appears in other sampled Copilot runs.

- **Root cause**
  - Artifact listing/cleanup is done in a separate later stage that no longer has the producer context in memory.

- **Exact change**
  - Reuse artifact IDs from the producer job via outputs/artifacts and perform cleanup in the same job, or defer cleanup to a low-priority maintenance path.

- **Estimated time savings**
  - **30–90s per Copilot review run**.

- **Implementation risk**
  - **Low.**

## Cost Optimizations

### 1. Stop paying for long `review_autofix` runs that fail after post-editor merge conflict handling
- **Evidence**
  - The failing `review_autofix` runs (`25370025320`, `25370115370`, `25371432937`) all reached post-editor state with `EDITOR_SUMMARY_POSTED=true`, then failed on unresolved merge conflict.
  - Slow successful runs in the same family still took **2263–2987s**.
  - Review jobs load a six-model reviewer panel (`minimax`, `moonshot`, `deepseek`, `glm`, `qwen`, `grok`) plus `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`.

- **Root cause**
  - Token-expensive reviewer/editor work is executed before mergeability is conclusively established.

- **Exact change**
  - Make mergeability an early gate.
  - On known conflict cases, either resolve first or fail fast before reviewer/editor prompting.

- **Estimated savings**
  - Highest single cost-saving change in this window.
  - Saves one full reviewer/editor cycle per conflicting PR; exact token savings are not logged, but compute/runtime savings are **10–20 minutes per occurrence**.

- **Quality-risk notes**
  - **Low risk** if the fast-fail path is limited to deterministic merge conflicts.

---

### 2. Right-size claude-branch comment-only review mode
- **Evidence**
  - Recent claude-branch review runs still carry the full reviewer-model configuration while explicitly skipping editor/commit/judge.
  - Gate logs say this mode avoids roughly **7 LLM calls** and about **50%** of follow-up latency, which implies the residual reviewer panel is still a major cost center.

- **Root cause**
  - Comment-only validation uses the same reviewer breadth as normal review.

- **Exact change**
  - In `claude_branch_review` mode:
    - reduce reviewer count,
    - or reuse prior consensus when there is no code delta,
    - or switch to one primary reviewer + summarizer.

- **Estimated savings**
  - **40–70% token reduction** on comment-only/follow-up runs.

- **Quality-risk notes**
  - **Medium risk** if over-trimmed.
  - Keep the full panel behind a manual override or changed-files threshold.

---

### 3. Make prompt-cache behavior measurable and stabilize prompt prefixes
- **Evidence**
  - Sampled deep-dive logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - No sampled deep-dive logs emitted cache-create/cache-read counters.
  - The repo’s own `workflow_log_analysis` run `25375766109` explicitly noted that prompt cache was enabled but not measurable.
  - Review failure logs show highly dynamic runtime paths and large dynamic content blocks injected during prompt construction, including very long inline consolidator stderr content.

- **Root cause**
  - Cache is on, but:
    - there is no measurement,
    - stable vs dynamic prompt regions are not clearly separated,
    - dynamic noise likely fragments cache prefixes.

- **Exact change**
  - Render static system/tool/model instructions into a fixed prefix file.
  - Append PR-specific paths, comments, and runtime context only after a stable delimiter.
  - Emit provider-level cache read/create counters in every LLM step summary.

- **Estimated savings**
  - **Medium**, but not quantifiable from current telemetry.
  - Likely affects both token cost and latency.

- **Quality-risk notes**
  - **Low risk** if only prompt assembly order changes.

---

### 4. Coalesce idle AI-memory writes in `orchestrate_poll`
- **Evidence**
  - Recent `orchestrate_poll` run `25378536620` emitted and pushed both:
    - `poll_started`
    - `poll_completed`
  - Both telemetry events had `did_push: true`.

- **Root cause**
  - Idle poll cycles perform two git-backed memory writes even when `has_work=false`.

- **Exact change**
  - For idle cycles, record only `poll_completed`, or batch `poll_started` + `poll_completed` into one commit/push.

- **Estimated savings**
  - **Small per run**, but persistent across **36 poller runs** in the current window.

- **Quality-risk notes**
  - **Low risk** if detailed start timestamps are not operationally required for idle polls.

---

### 5. Tune or skip low-yield reviewer memory retrieval
- **Evidence**
  - AI memory retrieve hit rate was **1/11 (9.1%)**.
  - **10 retrieves returned 0 records**.
  - `keyword_method` was `none` for **10 of 11** retrieves.
  - Average `estimated_tokens` was only **2.5**, indicating little payload even when enabled.

- **Root cause**
  - Retrieval is being invoked on reviewer paths without enough context to produce matches.

- **Exact change**
  - Add a cheap precondition: only retrieve when issue/PR context includes linked issues, prior candidate records, or enough changed-file/comment signal.
  - Default to plain keyword extraction before retrieval instead of `keyword_method=none`.

- **Estimated savings**
  - **Small to moderate** latency and token savings.
  - More importantly, reduces low-value memory work.

- **Quality-risk notes**
  - **Low risk** with fail-open behavior retained.

## Reliability Improvements

### 1. Fix unresolved merge-conflict handling in `review_autofix`
- **Failure evidence**
  - `25370025320`, `25370115370`, and `25371432937` all failed in `review / codex-agent / Run Codex resolver, validate, stage, commit`.
  - Each ended with `MERGE_CONFLICT=true` and `CONFLICT_RESOLVED=false`.
  - `25371432937` explicitly failed on merge conflict in `tests/e2e_smoke_canary.txt`.

- **Root cause category**
  - Merge/conflict-resolution failure after expensive editor work.

- **Exact fix**
  - Add pre-editor mergeability detection.
  - If conflict exists:
    - run conflict resolver first on the exact conflict set,
    - or fail fast to blocked state without reviewer/editor spend.
  - On unresolved conflict after one bounded resolver pass, stop continuation and preserve artifacts.

- **Expected reliability impact**
  - Most likely improvement to `review_autofix` family failure rate and rerun rate.
  - Should also lower cancellation churn by shortening dead-end runs.

- **Rollback / fail-open considerations**
  - If early conflict routing proves noisy, keep current behavior behind a feature flag and only enable for known hot files like `tests/e2e_smoke_canary.txt`.

---

### 2. Repair `test_and_mark_stable` Phase 4b and Phase 7 harness semantics
- **Failure evidence**
  - All 4 sampled release-test runs failed.
  - `25324103531` logged `##[error]Editor failed to remove bait line ...` and ended `bait_remained`.
  - `25369768571` failed at Phase 4b retry verification.
  - `25375729485` failed at Phase 7 despite cleanup comments already stating Phase 7 may have already closed the PR.

- **Root cause category**
  - E2E harness sequencing / retry-state tracking / idempotency bug.

- **Exact fix**
  - Persist retry run identity deterministically.
  - Use a single source of truth for Phase 4b state.
  - Make Phase 7 idempotent: if the PR is already closed and `cancel_on_pr_close` already ran successfully, treat that as pass rather than failure.
  - Stop later phases after a terminal Phase 4b failure.

- **Expected reliability impact**
  - Highest impact release-quality fix in the window.
  - Could move release-test family from **100% failure** to mostly green if harness, not product logic, is the main issue.

- **Rollback / fail-open considerations**
  - Keep current strict assertions available behind a debug flag for one-off validation.

---

### 3. Make nightly validation self-test diagnosable before enforcing it as hard-red
- **Failure evidence**
  - `nightly_validation_selftest` has **1 run, 1 failure**.
  - `25356037835` reported `fixtures=3 passed=1 failed=2`.

- **Root cause category**
  - Test fixture failures with insufficient surfaced detail in the main job log.

- **Exact fix**
  - Print failing fixture names and stage statuses directly into the main job summary, not only the uploaded artifact.
  - Consider temporary per-fixture `continue-on-error` plus aggregate nightly summary until the two red fixtures are identified and fixed.

- **Expected reliability impact**
  - Reduces false-red operational burden and shortens time-to-diagnosis.

- **Rollback / fail-open considerations**
  - Keep the workflow red overall if you need strict enforcement, but still surface failing fixture names inline.

---

### 4. Stabilize `issue_pr_status` helper fetch and Telegram cleanup path
- **Failure evidence**
  - Recent `issue_pr_status` run `25378601664` logged:
    - `Support checkout ref ... is unavailable; using main`
    - `Could not fetch tg_helpers.sh; skipping TG cleanup`
    - `tg_helpers.sh is empty; skipping TG cleanup`
    - `tg_cleanup_msgs helper missing; skipping TG cleanup`
    - `Telegram send failed`

- **Root cause category**
  - Support-script fetch fallback and duplicated helper acquisition logic.

- **Exact fix**
  - Resolve support root once per run and source `tg_helpers.sh` from that single location.
  - Remove the second independent fetch path.
  - If helper acquisition fails, emit one stable warning and skip all downstream TG work cleanly.

- **Expected reliability impact**
  - Moderate improvement for notifications/cleanup consistency.

- **Rollback / fail-open considerations**
  - Safe to fail open; TG cleanup is already treated as non-blocking.

## AI Memory Health

- **Telemetry found:** yes.
- **Total `AI_MEMORY_TELEMETRY` entries in deep-dive logs:** **55**
- **Operation distribution:**
  - `record-run-event`: **31**
  - `retrieve`: **11**
  - `record-candidate`: **6**
  - `summarize_unselected_runs`: **5**
  - `processed-command-check`: **1**
  - `processed-command-claim`: **1**

### Retrieval effectiveness
- **Retrieve hit rate:** **9.1%** (**1/11** had `records_selected > 0`)
- **Average `estimated_tokens`:** **2.5**
- **Average budget vs actual:** **budget not emitted** in sampled retrieve telemetry, so budget adherence cannot be assessed
- **`keyword_method` distribution:**
  - `none`: **10**
  - `plain`: **1**
  - `llm`: **0**

### Flags
- **Zero-record retrieves:** **10**
  - Seen in:
    - `review_autofix` failures: `25324565713`, `25370025320`, `25370115370`, `25371432937`
    - slow `review_autofix`: `25353743396`, `25355461484`
    - recent `review_autofix`: `25378337612`
    - `workflow_log_analysis`: `25375766109`
- **`fail_open: true` retrieves:** **0 observed**
- **`enabled: false` retrieves:** **0 observed**
- **High push retry counts:** one observed event with `push_attempts: 2` in `workflow_log_analysis` run `25324145530` during `processed-command-claim`/`record-run-event` activity; all others sampled were `push_attempts: 1`

### Write-path observations
- `review_autofix` consistently records `phase_started` and `phase_failed`/`phase_completed` events and often records a `patterns` candidate.
- `orchestrate_poll` recent run `25378536620` recorded and pushed both `poll_started` and `poll_completed`.
- Evidence-grade recent summary for `memory_maintenance` run `25377994087` showed a successful `compact` op with:
  - `archived_candidates: 2914`
  - `did_push: true`
  - `push_attempts: 1`

### Assessment
- The memory write path is healthy.
- The memory retrieve path is **not currently healthy/useful** for reviewer flows: almost every retrieve returned nothing, with almost no keyword extraction.
- Retrieval tuning should be prioritized before any broader memory expansion.

## GH API Call Audit

### Highest-volume / highest-redundancy patterns

#### 1. `test_and_mark_stable` Phase 4b retry poll loop
- **Evidence**
  - Workflow-log analysis run `25375766109` explicitly identifies this path as:
    - polling PR state and workflow runs every **15s**
    - reducible by **50–80%**
  - E2E log `25375729485` shows long repeated `Review run #25376134966: status=in_progress` polling before completion.
- **Pattern**
  - Unbatched repeated reads of:
    - PR state
    - workflow-run inventory
    - run status
- **Concrete change**
  - Persist retry run ID once and poll only `actions/runs/{id}`.
  - Re-check PR state only for close-event ambiguity.
- **Estimated call-count reduction**
  - **50–80%** on failing/slow Phase 4b paths.
- **Rate-limit risk reduction**
  - **High.**

#### 2. `review_autofix` post-merge validate dispatch does GraphQL + extra REST + per-issue actions
- **Evidence**
  - `25378601655`:
    - `gh api graphql` for `closingIssuesReferences` with labels
    - separate `gh api repos/.../pulls/{PR_NUMBER}` for `title + body`
    - `gh workflow run`
    - `gh issue edit`
- **Pattern**
  - GraphQL already returns linked issue labels, but the path still performs an extra PR REST fetch and then per-issue workflow/label operations.
- **Concrete change**
  - Make the initial GraphQL query authoritative for linked-issue routing.
  - Only fetch PR REST data if GraphQL returns empty or malformed output.
  - Batch all discovered issue metadata in that first GraphQL call.
- **Estimated call-count reduction**
  - **1–N fewer calls per merged PR** with linked issues.
- **Rate-limit risk reduction**
  - **Medium.**

#### 3. `issue_pr_status` falls back from batch GraphQL to per-issue REST
- **Evidence**
  - `25378601664`:
    - batch `gh api graphql`
    - warning on fallback
    - then per-issue `gh api repos/.../issues/{n}` calls
- **Pattern**
  - Missed batching/reuse opportunity after a batch query has already started the work.
- **Concrete change**
  - Expand the GraphQL batch query to include all needed label/body fields and treat it as primary.
  - Only per-issue fallback on true parse or transport failure.
- **Estimated call-count reduction**
  - **N fewer calls** in fallback-heavy runs.
- **Rate-limit risk reduction**
  - **Medium.**

#### 4. `cancel_on_pr_close` and `orchestrate_poll` probe `/rate_limit` preemptively
- **Evidence**
  - `cancel_on_pr_close` `25378601683`: `_reset_ts=$(gh api -i /rate_limit ...)`
  - `orchestrate_poll` `25378536620`: same pattern
- **Pattern**
  - A rate-limit lookup is paid even on green runs with no retries.
- **Concrete change**
  - Call `/rate_limit` only after an actual 403/429/secondary-rate-limit response, or on retry attempt 2+.
- **Estimated call-count reduction**
  - **1 call per run** for these families.
- **Rate-limit risk reduction**
  - Small direct savings; better hygiene.

#### 5. `copilot_pull_request_reviewer` cleanup refetches artifact metadata
- **Evidence**
  - `25378341064` and other recent Copilot summaries show `gh api /repos/.../actions/runs/<run_id>/artifacts`.
- **Pattern**
  - Artifact IDs are reacquired later instead of reused.
- **Concrete change**
  - Pass artifact IDs or names from producer to cleanup step/job.
- **Estimated call-count reduction**
  - **1 call per Copilot run**, plus less cleanup orchestration.
- **Rate-limit risk reduction**
  - Low to medium.

### Repo-policy cross-check
The repository’s own workflow-log analysis prompt explicitly references **GitHub API Call Hygiene** and instructs batching/reuse. The main observed violations of that policy are:
- long polling loops re-reading overlapping state,
- GraphQL followed by per-item REST fallback,
- separate cleanup/listing calls when IDs are already known earlier.

## Prompt Cache & Memory System

### What is working
- Prompt cache is **enabled** in every sampled AI-heavy workflow path (`review_autofix`, `orchestrate_poll`, `issue_pr_status`, etc.), via `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- Memory write operations are frequent and mostly successful; nearly all sampled telemetry pushes succeeded on the first attempt.

### What is not working
- **Prompt cache value is not measurable.**
  - No sampled deep-dive logs emitted cache read/create counters.
  - That means there is no evidence today that cache is delivering savings, even though it is turned on.
- **Memory retrieval is mostly ineffective.**
  - Hit rate is **9.1%**.
  - Most retrieves use `keyword_method: none`.
- **Prompt prefixes likely fragment.**  
  This is an inference from the logs:
  - review runs emit many highly variable runtime paths (`/tmp/codex-pr-...`)
  - large dynamic comment/context blobs appear in review execution
  - support-source/bootstrap context is repeatedly injected

### Likely cache-fragmentation causes
1. Dynamic PR/runtime noise too early in prompt assembly.
2. Large environment/runtime dumps changing from run to run.
3. Reviewer/editor prompts likely include unstable file paths and context ordering.
4. Error-path prompts include long dynamic stderr payloads.

### Concrete improvements
1. **Create a stable prompt prefix**
   - Put system instructions, tool contract, model catalog, and reusable guardrails in one rendered static prefix file.
   - Append PR metadata, diff stats, and comment context after a delimiter.

2. **Emit cache metrics per LLM call**
   - Add step-summary logging for:
     - prompt tokens
     - completion tokens
     - total tokens
     - cache create tokens
     - cache read tokens
   - Without this, cost work remains guesswork.

3. **Suppress low-value reviewer retrieval**
   - Skip retrieve when there is no linked issue, no prior memory records, and no usable keyword seed.

4. **Coalesce idle memory writes**
   - For `orchestrate_poll` cycles with `has_work=false`, write only one completion event.

### Estimated impact
- **Tokens:** medium savings, currently unquantified
- **Latency:** low-to-medium savings per run, larger cumulative savings on frequent review/poll paths
- **Reliability:** medium improvement via fewer low-value moving parts and better observability

## Orchestrator Health

### Observable health
- `orchestrate_poll` looks functionally healthy:
  - family success rate: **36/36**
  - recent sampled runs finished in **51s** and **132s**
  - telemetry shows clean `poll_started` and `poll_completed` writes
- There is no sampled evidence of stuck terminal states in the poller itself.

### Operational pain points
1. **High no-op fan-out**
   - `clarify`: **156 total**, **20 success**, **136 other**
   - `plan`: **131 total**, **16 success**, **115 other**
   - `implement`: **131 total**, **16 success**, **111 other**, **4 cancelled**
   - `orchestrate_clarify_respond`: **131 total**, **4 success**, **127 other**
2. **High cancellation churn in review**
   - `review_autofix`: **45 cancelled of 82 total** (**54.9% cancelled**)
3. **Idle poll write amplification**
   - Idle poll cycles still perform start/end memory writes and pushes.

### Smallest safe mitigations
- Add a single early router check for comment-command prefixes before dispatching four sibling workflows.
- Dedupe `review_autofix` by head SHA / comment hash so superseded runs do not fully start.
- Coalesce idle poll memory writes.

### Track these indicators
- `review_autofix` cancellation ratio
- `% skipped/other` in `clarify`, `plan`, `implement`, `orchestrate_clarify_respond`
- `review_autofix` p95 duration
- `test_and_mark_stable` failure rate
- AI memory retrieve hit rate
- prompt cache read/create counters once emitted

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Respond fan-out
- **Bottleneck type:** orchestration/no-op overhead
- **Evidence:** huge volumes of skipped/non-success no-op runs, especially `orchestrate_clarify_respond` (**127/131**) and `plan` (**115/131**).
- **Impact:** low per run, medium cumulative noise/queue pressure.
- **Fix:** upstream router/dedup before dispatch.

### 2. Implement queueing inside E2E release validation
- **Bottleneck type:** queueing
- **Evidence:** in `25375729485` and `25324103531`, implement polling sat in `pending`/`queued` states for long stretches before meaningful work started.
- **Impact:** extends already-long E2E validation windows.
- **Fix:** do not serially block Phase 4b/7 on slow upstream phase polling when earlier terminal failure is already known.

### 3. Review/autofix compute + merge-conflict overhead
- **Bottleneck type:** compute + retry + merge/conflict
- **Evidence:** `review_autofix` p95 **2054s**; multiple failures after unresolved merge conflict; successful outliers above **2200s**.
- **Impact:** biggest sustained AI/runtime bottleneck outside release E2E.
- **Fix:** preflight mergeability, shorten comment-only paths, stop continuation after bounded unresolved conflict.

### 4. CI lint/test wall time
- **Bottleneck type:** compute + some queueing
- **Evidence:** `ci` family **60/60 success**, but `avg_duration_seconds=613.6`, `p50=614`, `p95=653.05`; logs repeatedly say `lint` dominates and runner wait precedes execution.
- **Impact:** frequent merge-path latency tax.
- **Fix:** second-order optimization only after release/review fixes; investigate safe test splitting or setup trimming.

### 5. Workflow-log analysis itself
- **Bottleneck type:** compute
- **Evidence:** `workflow_log_analysis` runs take **2527–2972s**.
- **Impact:** not product-critical, but expensive as an auxiliary workflow.
- **Fix:** only optimize after operational paths are fixed; reuse report context and trim redundant deep-audit passes.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` E2E release validation: **100% failure**, **~50–56 minute** runs
- `review_autofix` long tail: **p95 2054s**, many cancellations, merge-conflict failures
- `ci` steady **~10 minute** lint/test runtime

**Top failure modes**
- Unresolved merge conflicts in `review_autofix`
- Phase 4b/Phase 7 harness issues in release validation
- Nightly validation self-test fixture failures
- Helper-fetch fallbacks in `issue_pr_status`

**Highest-cost drivers**
- Long `review_autofix` reviewer/editor runs
- Failed `test_and_mark_stable` retry/poll loops
- Unmeasured prompt-cache behavior with repeated AI-heavy runs
- Frequent no-op orchestrator fan-out and cancelled review runs

**Top 3 prioritized actions**
1. **Fix `test_and_mark_stable` Phase 4b/7 control flow and polling**
2. **Add early mergeability/conflict routing to `review_autofix`**
3. **Batch/trim GH API polling and fallback lookups, especially in release validation and post-merge validation dispatch**

## Metrics Appendix

### Overall repository metrics

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 812 | 261 | 9 | 49 | 493 | 1.1% | 152.4 | 2 | 639 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | Avg dur (s) | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| test_and_mark_stable | 4 | 0 | 4 | 0 | 0 | 3200.0 | 3221 | 3350.6 | Highest-risk family; all failed |
| review_autofix | 82 | 33 | 4 | 45 | 0 | 555.9 | 53 | 2054 | High cancel ratio, long tail |
| ci | 60 | 60 | 0 | 0 | 0 | 613.6 | 614 | 653.1 | Stable but consistently ~10 min |
| workflow_log_analysis | 4 | 4 | 0 | 0 | 0 | 2792.0 | 2834.5 | 2964.5 | Slow auxiliary workflow |
| orchestrate_poll | 36 | 36 | 0 | 0 | 0 | 50.9 | 49 | 57 | Functionally healthy |
| clarify | 156 | 20 | 0 | 0 | 136 | 13.7 | 1 | 100.8 | Heavy no-op volume |
| plan | 131 | 16 | 0 | 0 | 115 | 10.7 | 1 | 116 | Heavy no-op volume |
| implement | 131 | 16 | 0 | 4 | 111 | 24.3 | 1 | 195.5 | Large no-op/cancel share |
| orchestrate_clarify_respond | 131 | 4 | 0 | 0 | 127 | 1.6 | 1 | 7 | Mostly skipped |
| copilot_pull_request_reviewer | 25 | 25 | 0 | 0 | 0 | 216.2 | 212 | 372.8 | Cleanup/artifact path visible |
| issue_pr_status | 10 | 10 | 0 | 0 | 0 | 44.7 | 57.5 | 76.1 | Helper-fetch warnings |
| cancel_on_pr_close | 10 | 10 | 0 | 0 | 0 | 11.4 | 13 | 15 | API hygiene opportunity only |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.0 | 100 | 100 | 2 of 3 fixtures failed |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry entries | 55 |
| `record-run-event` entries | 31 |
| `retrieve` entries | 11 |
| `record-candidate` entries | 6 |
| Retrieve hit rate | 9.1% (1/11) |
| Zero-record retrieves | 10 |
| Avg `estimated_tokens` on retrieve | 2.5 |
| `keyword_method=none` | 10 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Push attempts >1 observed | 1 event |

### Prompt/cache metrics availability

| Metric | Availability | Notes |
|---|---|---|
| Prompt tokens | Not reliably emitted | No repo-wide deep-dive totals available |
| Completion tokens | Not reliably emitted | Same gap |
| Total tokens | Not reliably emitted | Same gap |
| Cache create/read counters | Not emitted in sampled deep-dive logs | Cache enabled but not measurable |
| OpenRouter cache enabled flag | Present | `OPENROUTER_PROMPT_CACHE_DISABLED: false` repeatedly observed |

### GH API hotspot summary

| Workflow / step | Observed pattern | Evidence run(s) | Exact call count available? | Safe reduction estimate |
|---|---|---|---|---|
| `test_and_mark_stable` / Phase 4b retry loop | Repeated PR-state + workflow-run polling | 25375729485, 25375766109 | No | 50–80% |
| `review_autofix` / post-merge validate dispatch | GraphQL + extra PR REST + per-issue actions | 25378601655 | No | 1–N calls per merged PR |
| `issue_pr_status` / orchestrator fallback | Batch GraphQL then per-issue REST fallback | 25378601664 | No | N calls in fallback cases |
| `orchestrate_poll` / retry wrapper | Preemptive `/rate_limit` probe | 25378536620 | Yes, 1 visible probe per run | 1 call per run |
| `cancel_on_pr_close` / retry wrapper | Preemptive `/rate_limit` probe | 25378601683 | Yes, 1 visible probe per run | 1 call per run |
| `copilot_pull_request_reviewer` / artifact cleanup | Artifact list re-fetch | 25378341064 | No | 1 call per run |

### Sampled failure evidence table

| Run ID | Workflow | Duration (s) | Failure point | Key evidence |
|---|---|---:|---|---|
| 25370115370 | review_autofix | 1836 | `review / codex-agent` | `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false` |
| 25370025320 | review_autofix | 1295 | `review / codex-agent` | `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false` |
| 25371432937 | review_autofix | 637 | `review / codex-agent` | conflict in `tests/e2e_smoke_canary.txt`; resolver retries; unresolved |
| 25375729485 | test_and_mark_stable | 2999 | Phase 7 close/cancel verify | long review polling, late harness failure |
| 25369768571 | test_and_mark_stable | 3359 | Phase 4b pytest+retry | long retry validation path |
| 25324103531 | test_and_mark_stable | 3303 | Phase 4b bait removal verify | `Editor failed to remove bait line ...`; `bait_remained` |
| 25356037835 | nightly_validation_selftest | 100 | self-test matrix | fixtures=3, passed=1, failed=2 |

If you want, I can turn this into a shorter exec-ready action list with owners, estimated effort, and rollout order.

## Deep Audit — Workflows & Scripts (2026-05-05)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `.github/workflows/issue_pr_status.yml:240-249`; `.github/workflows/review_autofix.yml:3728-3746, 3848-3867, 4592-4599`; `scripts/label_helpers.sh:146-197`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — Several workflow-local fallback implementations of `set_issue_phase_label_resilient` only do `POST /labels` for the target label, while the canonical helper in `scripts/label_helpers.sh` first reads current labels, removes conflicting phase labels, and then `PUT`s the reconciled set. If `label_helpers.sh` cannot be sourced, these reduced fallbacks can leave mutually exclusive phase labels on the same issue, e.g. `ai:review-blocked` and `ai:ready-to-merge` together.  
  **Recommended fix** — Remove the inline POST-only fallbacks and source `scripts/label_helpers.sh` earlier in the job so the canonical `set_issue_phase_label_resilient <issue_number> <target_label> <repo>` is always available. If a fallback must remain, copy the full GET/compute/PUT implementation from `scripts/label_helpers.sh` rather than the simplified POST-only variant.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `.github/workflows/review_autofix.yml:1248-1378`; `scripts/gh_helpers.sh:735-899`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The `Collect PR metadata` step manually hydrates PR context with four separate logical GitHub calls in the common path: 1× PR payload, 1× issue comments pagination, 1× reviews pagination, and 1× review comments pagination, then separately performs a GraphQL linked-issues fetch. The repo already has `gh_pr_with_all_comments` in `scripts/gh_helpers.sh`, which batches PR metadata, issue comments, and review comments behind one GraphQL-first helper with REST fail-open fallback. Current logical call count in this path is **5**; proposed count is **2** (**1** `gh_pr_with_all_comments` + **1** linked-issues GraphQL).  
  **Recommended fix** — Replace the hand-rolled hydration in `.github/workflows/review_autofix.yml` with `gh_pr_with_all_comments "${owner}" "${repo}" "${PR_NUMBER}" "${PRELOADED_PR_META}"`, then keep the existing linked-issues GraphQL call. Reuse the helper’s output to populate `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, and `PR_REVIEW_COMMENTS_FILE`.

- **ID** — BATCH-001  
  **File path** — `.github/workflows/issue_pr_status.yml:188-193, 297-330, 503-512`  
  **Severity** — High  
  **Category tag** — `api-batching`  
  **Description** — `issue_pr_status.yml` fetches linked issues more than once in the same execution path, then falls back to per-issue REST inside loops. It does: **1** GraphQL call to get linked issue numbers, **1** second GraphQL call to classify orchestrator tracking/managed issues, then on GraphQL failure it performs up to **N** `GET /issues/{n}` REST lookups, and later the Telegram alert gate performs up to **N** more `_safe_gh_jq` body fetches to rediscover whether any linked issue is orchestrated. Current logical call count is **2 + up to 2N**; proposed count is **1** batched GraphQL query reused across close/label/alert logic, with **0** extra per-issue calls on the happy path.  
  **Recommended fix** — Extend one batched GraphQL query to return `number`, `labels`, and `body` for all linked issues, cache that result in a JSON variable/file, and reuse it for both orchestrator classification and alert suppression. Follow the existing batching patterns in `scripts/orchestrate_poll_process.sh`, especially `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`.

- **ID** — API-002  
  **File path** — `scripts/review_rb_judge.sh:191-201`; `.github/workflows/review_autofix.yml:1104-1105, 1544-1548, 2232-2239`  
  **Severity** — Low  
  **Category tag** — `api-redundancy`  
  **Description** — `review_rb_judge.sh` always refetches the PR diff with `gh api ... -H 'Accept: application/vnd.github.diff'` even though `review_autofix.yml` already materializes `PR_DIFF_FILE` and `ORIGINAL_PR_DIFF_FILE` earlier in the same run. Current logical call count is **1** extra diff fetch per judge invocation; proposed count is **0** extra calls when the cached diff files exist.  
  **Recommended fix** — Teach `scripts/review_rb_judge.sh` to prefer `${PR_DIFF_FILE}` or `${ORIGINAL_PR_DIFF_FILE}` when present, and only fall back to the diff API if neither file exists. Reuse the existing runtime-file cache contract already established by `review_autofix.yml`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/validate.yml:199-277`; `.github/workflows/issue_pr_status.yml:59-125`; `.github/workflows/validation-improvements-intake.yml:66-128`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The support-repo bootstrap logic is duplicated across workflows: `WF_REMOTE_URL` construction, `checkout_support_ref`, and `copy_from_ref_or_local` are implemented repeatedly with only small variations. This duplication increases drift risk for fallback semantics, remote/local precedence, and warning behavior.  
  **Recommended fix** — Extract a shared helper such as `scripts/fetch_workflow_support.sh` with a signature like `fetch_workflow_support <wf_source> <preferred_ref> <workspace_root> [--require-remote-canonical]`. Update `validate.yml`, `issue_pr_status.yml`, and `validation-improvements-intake.yml` to call the shared script.

- **ID** — DUP-002  
  **File path** — `.github/workflows/review_autofix.yml:3713-3747, 3836-3868, 4591-4599`; `.github/workflows/issue_pr_status.yml:240-249`; `scripts/label_helpers.sh:146-197`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `set_issue_phase_label_resilient` is redefined inline multiple times in `review_autofix.yml` and once in `issue_pr_status.yml`, despite already existing as a reusable canonical function in `scripts/label_helpers.sh`. The inline copies are not semantically equivalent to the shared implementation, so this is both duplication and behavior drift.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the single owner of `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Update the callers in `review_autofix.yml` and `issue_pr_status.yml` to fail over to a copied-once helper artifact instead of redefining local versions.

- **ID** — DUP-003  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:27-48`; `.github/workflows/orchestrate_poll.yml:66-88`; `.github/workflows/review_autofix.yml:1258-1292`; `.github/workflows/test-and-mark-stable.yml:1202-1224, 4350-4371`; `scripts/gh_helpers.sh:391-716`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Rate-limit/backoff wrappers are duplicated in several workflows (`_rl_wait`, `_gh_retry`, `gh_api_safe`, local `gh_retry`) even though `scripts/gh_helpers.sh` already owns the canonical retry, JSON-validation, and curl wrappers. These copies diverge in retry counts, stderr handling, and JSON safety.  
  **Recommended fix** — Standardize on `scripts/gh_helpers.sh` as the shared module. Use existing signatures `gh_retry "$@"`, `gh_retry_to_file <outfile> ...`, `gh_api_json_to_file <outfile> ...`, and `curl_gh_api ...`. Update callers in `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, `review_autofix.yml`, `test-and-mark-stable.yml`, and `mark-stable.yml` to source the helper instead of embedding local retry code.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1187-1518`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Phase 3d/Wait for review workflow` run block contains `${{ }}` interpolations and is approximately **19,701** characters long, leaving only about **1,299** characters of headroom before GitHub’s 21,000-character template-expression limit. This is already inside the repo’s documented high-risk zone.  
  **Recommended fix** — Extract this polling logic to an external script under `scripts/` (preferred), e.g. `scripts/e2e_wait_review.sh`, and pass only the small set of required environment variables from YAML.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1604-2009`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The `Phase 4b: Verify editor restored canary (pytest + retry)` run block contains `${{ }}` interpolations and is approximately **21,289** characters long, which is about **289 characters over** the hard 21,000-character ceiling by raw block size estimate. Because GitHub counts the compiled template expression rather than the YAML source verbatim, this is an overflow-risk finding rather than a confirmed parser failure. [NEEDS VERIFICATION]  
  **Recommended fix** — Split the step immediately. The safest path is to move the retry-dispatch/poll logic into a dedicated script such as `scripts/e2e_verify_retry_canary.sh`, or split the step into separate “prepare”, “retry dispatch/poll”, and “pytest classify” steps.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/review_autofix.yml:1251-1573`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Collect PR metadata` run block contains `${{ }}` interpolations and is approximately **16,438** characters long, leaving about **4,562** characters of headroom. The block also embeds two Python heredocs, which makes future edits likely to push it over the threshold.  
  **Recommended fix** — Move the metadata assembly and context rendering into external scripts, e.g. `scripts/build_pr_context.py` and/or a thin shell wrapper that sources `scripts/gh_helpers.sh`, then keep YAML limited to env wiring.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/validate.yml:183-476`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Fetch workflow support files` run block contains `${{ }}` interpolations and is approximately **16,486** characters long, leaving about **4,514** characters of headroom. Its large inline file list and support-bootstrap functions make it a growth hotspot.  
  **Recommended fix** — Extract the whole support-fetch/bootstrap routine to `scripts/fetch_workflow_support.sh` and keep the YAML step as a short `bash scripts/fetch_workflow_support.sh` invocation.

- **ID** — EXPR-005  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:813-1096`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Parse and post answer` run block contains `${{ }}` interpolations and is approximately **15,141** characters long, leaving about **5,859** characters of headroom. The inline memory-loop-guard and comment-posting logic is already large enough that a few more branches could move it into the danger zone.  
  **Recommended fix** — Extract the answer parsing / loop-guard / posting path into a dedicated script such as `scripts/orchestrate_parse_and_post_answer.sh`, or split the current step into smaller “parse”, “loop guard”, and “post answer / escalate” steps.

**Workflow file size note:** no audited workflow exceeds the 800 KB early-warning threshold; the largest observed files are `review_autofix.yml` (~278 KB) and `test-and-mark-stable.yml` (~255 KB).

### Section 5: Cross-Cutting Concerns

- **ID** — SHELL-001  
  **File path** — `scripts/review_commit_changes.sh:455`; `scripts/review_conflict_resolve.sh:994`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Two scripts set the authenticated remote URL without quoting the full argument: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. Elsewhere in the repo the same pattern is quoted. This is a classic SC2086-style inconsistency on a secret-bearing argument and makes the command more fragile if token/repo formatting ever changes.  
  **Recommended fix** — Quote the URL exactly as the workflows already do, e.g. `git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"`, and standardize on one quoting pattern across scripts and workflows.

No additional dead-code blocks or `TODO`/`FIXME`/`HACK` markers were found in the audited workflow/script set.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BATCH-001, EXPR-001, EXPR-002 |
| Medium | 8 | BUG-001, API-001, DUP-001, DUP-002, DUP-003, EXPR-003, EXPR-004, EXPR-005 |
| Low | 2 | API-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 4 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 2 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-05)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap/deadness is proven locally enough that implement can act without changing retry, pagination, auth, or concurrency semantics. `NEEDS_VERIFICATION` means the optimization looks real, but a human or follow-up audit must confirm a cross-step/file contract before changing it. `RISKY_SKIP` means the redundancy may be real, but it lives in a retry/race-defense/poller path that this pass must not auto-implement.

### Consolidation Candidates (MERGE-###)
No findings.

### Redundant Re-Fetch (REUSE-###)

- **ID** — REUSE-001  
  **Safety tag** — NEEDS_VERIFICATION  
  **File path and line ranges** — `scripts/review_rb_judge.sh:146-156`; `.github/workflows/review_autofix.yml:1102-1105, 1336-1350`  
  **Current call count** — 1 extra REST PR fetch on the “GraphQL returned no linked issues” fallback path  
  **Proposed call count** — 0 extra REST PR fetches on that path  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/pulls/{pull_number}`  
  **Evidence** — The judge fallback re-fetches PR title/body even though the caller already materializes both `PR_PAYLOAD_FILE` and `PR_META_FILE` earlier in the same workflow run.

  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql ... )"
  
  if [ -z "${ISSUE_NUMBERS}" ]; then
    PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
    ...
  fi
  ```

  ```bash
  echo "PR_PAYLOAD_FILE=${RUNTIME_DIR}/pr_payload.json"
  echo "PR_META_FILE=${RUNTIME_DIR}/pr_meta.json"
  ...
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
  ...
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```
  **Proposed fix** — In `scripts/review_rb_judge.sh`, build `PR_DATA` from `PR_PAYLOAD_FILE` first, then `PR_META_FILE`, and keep the existing `gh api repos/.../pulls/${PR_NUMBER}` call only as a last-resort fallback when both files are missing/empty.  
  **Safety rationale** — NEEDS_VERIFICATION because the reuse crosses the workflow→script boundary; this pass cannot prove from static reading alone that every in-repo `review_rb_judge.sh` invocation always seeds valid `PR_PAYLOAD_FILE`/`PR_META_FILE`.  
  **Downstream signal** — Verify every in-repo invocation of `review_rb_judge.sh` exports readable `PR_PAYLOAD_FILE` or `PR_META_FILE` before execution; if true, switch the fallback `PR_DATA` assembly to file-first and keep the current `gh api` call as the last-resort fallback.

### Dead Calls (DEAD-API-###)

- **ID** — DEAD-API-001  
  **Safety tag** — SAFE_TO_MERGE  
  **File path and line ranges** — `scripts/review_rb_judge.sh:161-170`; `scripts/review_rb_judge.sh:241-249, 645-651`  
  **Current call count** — `N` issue-body fetches for `N` linked issues  
  **Proposed call count** — `1` issue-body fetch total  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}/issues/{issue_number}`  
  **Evidence** — The loop fetches every linked issue body, but downstream only uses the first linked issue’s number/body.

  ```bash
  FIRST_ISSUE=""
  FIRST_ISSUE_BODY=""
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    if [ -z "${FIRST_ISSUE}" ]; then
      FIRST_ISSUE="${issue_number}"
    fi
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if [ -z "${FIRST_ISSUE_BODY}" ]; then
      FIRST_ISSUE_BODY="${BODY}"
    fi
  done <<< "${ISSUE_NUMBERS}"
  ```

  Later reads are only against the first issue/body:

  ```bash
  echo "=== ISSUE #${FIRST_ISSUE} (original requirement) ==="
  echo "${FIRST_ISSUE_BODY}"
  ```

  ```bash
  - Replaces: ${FIRST_ISSUE:+#${FIRST_ISSUE} }(PR #${PR_NUMBER} closed — approach rework)
  ```
  **Proposed fix** — In `scripts/review_rb_judge.sh`, fetch the issue body only when `FIRST_ISSUE_BODY` is still empty, then stop issuing `repos/.../issues/{n}` calls for later linked issues.  
  **Safety rationale** — SAFE_TO_MERGE because the extra calls are in the same function, no downstream branch consumes later issue bodies, and removing them does not alter pagination, retry shape, auth scope, or concurrency boundaries.  
  **Downstream signal** — In `scripts/review_rb_judge.sh`, fetch issue body only for the first linked issue and skip `repos/.../issues/{n}` calls for later linked issues.

- **ID** — DEAD-API-002  
  **Safety tag** — RISKY_SKIP  
  **File path and line ranges** — `scripts/orchestrate_poll_process.sh:11379-11415`  
  **Current call count** — 1  
  **Proposed call count** — 0  
  **Endpoint(s)** — `GET /repos/{owner}/{repo}`  
  **Evidence** — The standalone conflict sweep fetches `default_branch` into `DEFAULT_BRANCH`, but this assignment is not consumed anywhere in the sweep body that follows.

  ```bash
  STANDALONE_PRS="$(gh_retry gh pr list ...)"
  STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
  echo "Found ${STANDALONE_COUNT} open PR(s) to scan."
  
  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  
  for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
    ...
    S_PR_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${S_PR}" || echo '{}')"
    ...
  done
  ```
  **Proposed fix** — Remove the `DEFAULT_BRANCH` fetch from this sweep, or prove a hidden contract requires refreshing it here before touching the code.  
  **Safety rationale** — RISKY_SKIP because the dead call sits inside `scripts/orchestrate_poll_process.sh`, which the audit contract explicitly treats as a race-defense path that must not be auto-implemented.  
  **Downstream signal** — Do not auto-delete this call; manually review the standalone conflict-sweep path in `orchestrate_poll_process.sh` and confirm no hidden sourcing/logging contract depends on `DEFAULT_BRANCH` being refreshed here.

### Cross-References to Deep Audit Section

- API-001: NEEDS_VERIFICATION — Directionally correct helper consolidation, but comment/review pagination parity and exact env/file output shape should be checked before swapping out the hand-rolled hydration.
- BATCH-001: NEEDS_VERIFICATION — The single cached GraphQL payload should cover close/label/alert logic, but the conservative tracking-vs-managed fallback behavior needs parity verification before removing per-issue reads.
- API-002: NEEDS_VERIFICATION — Reusing `PR_DIFF_FILE`/`ORIGINAL_PR_DIFF_FILE` is the right direction, but implement should first confirm every `review_rb_judge.sh` invocation is seeded with those files.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | DEAD-API-001 |
| NEEDS_VERIFICATION | 1 | REUSE-001 |
| RISKY_SKIP | 1 | DEAD-API-002 |

### Implement-Stage Handoff

- DEAD-API-001
