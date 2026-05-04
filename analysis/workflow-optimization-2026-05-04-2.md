## Executive Summary

- **`review_autofix` is the biggest latency and likely token-cost driver.** Long runs cluster between **1,358s and 1,730s** (`25304556994`, `25300219172`, `25303629616`) and are dominated by `review / codex-agent (claude-branch-review)` even on comment-only paths where editor/commit/judge are skipped; one successful run still reported **`Reviewers_SUCCESSFUL=6`**. **Estimated impact:** cut affected review latency by **5–12 minutes/run**. **Confidence:** high.

- **`test_and_mark_stable` is the worst reliability bottleneck and blocks release flow end-to-end.** All **3/3 runs failed** and each failed late: **2,424s**, **3,235s**, **3,461s** (`25300046587`, `25273372573`, `25281876234`). Failures were deterministic enough to catch much earlier: editor did not remove bait line, push failed, and one step ran outside a git repo. **Estimated impact:** reduce failed-release waste by **25–45 minutes/run** and materially improve release success rate. **Confidence:** high.

- **`implement` failures are wasting retries on agent exploration/no-op loops.** Failed runs (`25293966619`, `25293932552`, `25293940145`, `25294005792`) ended with messages like “**Codex produced no actionable output 2 attempts in a row**” and “**Codex implement failed after 5 attempts**.” **Estimated impact:** cut failed implement retries and associated model spend by **40–80%** on ambiguous tasks. **Confidence:** high.

- **`ci` is stable but consistently expensive in wall-clock time.** `ci` p50 is **606s** and recent successful runs sit around **582–665s** (`25304556923`, `25303629554`, `25302038658`), almost always dominated by `lint`. This is an optimization opportunity, but it is lower leverage than fixing review/release. **Estimated impact:** **2–4 minutes** wall-clock reduction if job fan-out is introduced carefully. **Confidence:** medium.

- **GH API usage is heaviest in release and review flows, with clear polling/redundancy patterns.** Deep-dive `gh api` mention counts were led by **`test_and_mark_stable` 632**, **`workflow_log_analysis` 353**, **`review_autofix` 199**, **`implement` 72**. Repeated Actions polling and repeated PR metadata fetches are the main waste patterns. **Estimated impact:** **30–70%** fewer API calls in targeted workflows, with lower secondary rate-limit risk. **Confidence:** high.

- **AI memory retrieval is functioning but weakly effective for review workloads.** Across deep-dive logs there were **18 retrieves**, only **8 hits** (**44.4% hit rate**), with **10 zero-result retrieves** and **0 `llm` keyword-method uses**; reviewer runs often retrieved nothing while implement runs repeatedly retrieved a single small record. **Estimated impact:** moderate quality and cost improvement if retrieval relevance and helper availability are fixed. **Confidence:** medium-high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Reduce reviewer fan-out and disable two-pass review on comment-only / claude-branch-review paths
- **Critical-path win**
- **Evidence:** `review_autofix` runs `25303629616` (**1730s**), `25302038719` (**1668s**), `25302806251` (**1646s**), and `25304556994` (**1358s**) were dominated by `review / codex-agent (claude-branch-review)`. In `25304556994`, the run still took 22.6 minutes despite `editor/commit/judge/auto-merge skipped` and `Reviewers_SUCCESSFUL=6`. Failed run `25300219172` also showed `REVIEWER_MODELS` with 6 models and `ENABLE_REVIEWER_TWO_PASS: true`.
- **Root cause:** Over-provisioned reviewer breadth and second-pass analysis are being applied to low-risk, comment-only review paths.
- **Exact change:** Gate reviewer breadth by path type:
  - For `claude-branch-review` or comment-only paths, run a **reduced panel** (for example 2–3 reviewers instead of 6).
  - Disable `ENABLE_REVIEWER_TWO_PASS` unless the first pass detects disagreement, safety-risk files, or merge conflicts.
- **Estimated time savings:** **300–720s/run** on affected `review_autofix` runs.
- **Implementation risk:** **Medium.**
- **Quality-risk notes:** Slight risk of lower reviewer diversity; mitigate by escalating back to full panel only when first-pass disagreement or risky file patterns are present.

### 2. Stop waiting up to 20 minutes for check-runs on paths that cannot merge code anyway
- **Critical-path win**
- **Evidence:** `review_autofix` logs showed `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200` and `CHECK_RUNS_POLL_INTERVAL_SECS: 20`. Long review runs (`25300167876` at **1160s**, `25303629616` at **1730s**) included visible wait/setup overhead before meaningful AI work.
- **Root cause:** A long check-run wait budget is applied even to branches/paths where the workflow is effectively comment-only or where editor/merge steps are already skipped.
- **Exact change:** Add a path-mode guard:
  - If workflow is in comment-only / branch-review mode, skip or sharply cap check-run waiting.
  - Use a much shorter timeout for non-merging review paths.
- **Estimated time savings:** **120–480s/run** on affected review runs; larger in pathological waits.
- **Implementation risk:** **Low-Medium.**
- **Rollback/fail-open:** Safe to fail open by posting review results without waiting on checks when no merge/commit action is planned.

### 3. Add fail-fast preflight checks to `test_and_mark_stable`
- **Critical-path win**
- **Evidence:** All release runs failed late:
  - `25300046587` failed at **2424s** in `e2e-smoke-test / Phase 4b: Verify editor removed bait line`
  - `25273372573` failed at **3235s** in `release / Tag version and update stable pointer`
  - `25281876234` failed at **3461s** in `sync-to-main / Dispatch forward-merge-stable-to-main`
- **Root cause:** Deterministic precondition errors are discovered after long test/poll/orchestration work.
- **Exact change:** Before the long release/test chain:
  - verify git repo context and working directory,
  - dry-run push/tag permissions,
  - verify the editor path can actually produce and push a fix commit in the smoke fixture.
- **Estimated time savings:** **1,500–2,700s** on failing runs of this workflow.
- **Implementation risk:** **Low.**
- **Notes:** This is both a speed and reliability improvement.

### 4. Short-circuit no-work `orchestrate_poll` cycles before full checkout/support staging
- **Critical-path win for poll loops; micro in single-run terms**
- **Evidence:** `orchestrate_poll` run `25305233480` succeeded in **45s** with `has_work=false`, yet the visible hotspot was `poll / Checkout repository`; it used `actions/checkout@v5` with `fetch-depth: 0`. Similar recent poll cycles reported no work but still paid runner/setup overhead.
- **Root cause:** The workflow performs repository checkout and support staging before confirming there is any work to do.
- **Exact change:** Move the “has work?” query ahead of checkout, and exit before checkout when no PR/issue state changed.
- **Estimated time savings:** **5–15s/poll cycle** plus fewer runner-minutes.
- **Implementation risk:** **Low.**
- **Why it matters:** Per-run gain is small, but it compounds across frequent poll cycles.

### 5. Split `ci` wall-clock work into parallel jobs, keeping coverage on one leg only
- **Micro-optimization relative to review/release**
- **Evidence:** `ci` p50 is **606s** and recent runs cluster around **590–665s** (`25304556923`, `25303629554`, `25300184390`, `25302038658`). Logs repeatedly show `lint` dominating runtime while both `25 passed` and `81 passed` suites complete in the same workflow.
- **Root cause:** Multiple test/check groups appear serialized in one long-running job.
- **Exact change:** Split `ci` into at least two parallel jobs:
  - lint/script-ref/unit set A
  - orchestrate lib unit tests / set B
  - keep coverage/report merge only on one leg if possible
- **Estimated time savings:** **120–240s** wall-clock on `ci`.
- **Implementation risk:** **Medium.**
- **Caution:** This may raise total runner-minutes slightly if not paired with removal of duplicate setup/coverage work.

## Cost Optimizations

Ranked by expected model/token or runner-minute savings.

### 1. Shrink `review_autofix` model breadth on low-risk paths
- **Evidence:** `25304556994` ran **1358s** with `Reviewers_SUCCESSFUL=6` despite comment-only behavior; `25300219172` showed 6 configured reviewer models plus two-pass review.
- **Root cause:** The workflow spends premium model time on broad reviewer consensus even when no commit/judge/merge path is active.
- **Exact change:** Use a tiered reviewer policy:
  - 2–3 reviewers, single pass for comment-only or branch-review paths
  - full 6-reviewer, two-pass panel only when changing executable code, conflict resolution, or release logic
- **Estimated savings:** **30–60% AI-review token cost** and corresponding runner-time reduction on affected `review_autofix` runs.
- **Quality-risk notes:** **Medium**; preserve quality by escalating only when first-pass disagreement or risky file scope is detected.

### 2. Terminate `implement` no-op loops earlier and request clarification explicitly
- **Evidence:** Failed runs `25293966619`, `25293932552`, `25293940145`, `25294005792` spent **144–331s** and ended with no-actionable-output loops or up to **5 attempts**. One run explicitly said, “I need one of these to continue,” indicating a hidden clarification need.
- **Root cause:** The implement stage keeps retrying even after the agent has signaled ambiguity or produced announced-edit-without-changes behavior.
- **Exact change:** After:
  - 1 empty-output + 1 no-diff output, or
  - explicit clarification-needed language,
  immediately route to clarify/fail-open instead of continuing implement retries.
- **Estimated savings:** **40–80%** of failed-implement model usage and retry time.
- **Quality-risk notes:** **Low** if the workflow clearly posts the clarification request and preserves the issue state.

### 3. Eliminate avoidable reruns from deterministic release failures
- **Evidence:** `test_and_mark_stable` consumed **2,424–3,461s** before failing, and all three observed runs failed.
- **Root cause:** Long release/test chains are retried or re-invoked despite known deterministic breakpoints.
- **Exact change:** Add early exit guards for:
  - missing git repo state,
  - push/tag permission failures,
  - smoke-test editor non-write behavior,
  and mark them as preflight failures with explicit summaries.
- **Estimated savings:** Save nearly the **entire run cost** on these failure classes.
- **Quality-risk notes:** **Low.** Preflight checks reduce waste without changing release semantics.

### 4. Make prompt-cache effectiveness measurable before further tuning
- **Evidence:** `review_autofix` deep-dive runs (`25300219172`, `25303629616`, `25304556994`) showed cache probes with `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`, even though `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
- **Root cause:** Cache is enabled, but telemetry is not surfacing actual creation/read counters.
- **Exact change:** Emit provider-returned token/cache counters into workflow logs and run summaries for every AI step.
- **Estimated savings:** **Unquantified today**; likely prerequisite to meaningful cache optimization.
- **Quality-risk notes:** **Low.**
- **Bounded statement:** Cache savings cannot be verified from the current dataset.

### 5. Reduce GH API polling loops that burn runner-minutes without adding model quality
- **Evidence:** `gh api` mentions: `test_and_mark_stable` **632**, `review_autofix` **199**, `implement` **72**, `cancel_on_pr_close` **16**.
- **Root cause:** Poll-heavy loops and repeated metadata fetches extend runner occupancy.
- **Exact change:** Batch workflow/run status fetches and cache PR metadata within the run.
- **Estimated savings:** Mostly **runner-minute** savings; likely **meaningful but secondary** compared with model breadth reductions.
- **Quality-risk notes:** **Low.**

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Fix `sync-to-main` to always run inside a checked-out git repository
- **Failure evidence:** `test_and_mark_stable` run `25281876234` failed in `sync-to-main / Dispatch forward-merge-stable-to-main` with `fatal: not a git repository (or any of the parent directories): .git`.
- **Root cause category:** Workflow/environment precondition bug.
- **Exact fix:** Add an explicit checkout or enforce the correct working directory before any `git` call in `sync-to-main`.
- **Expected reliability impact:** High for release flow; this is a deterministic failure.
- **Rollback/fail-open:** Safe; if checkout fails, abort immediately with a clear summary instead of reaching the dispatch step.

### 2. Fix the editor/resolver no-op regression exposed by the smoke test
- **Failure evidence:** `25300046587` failed in `e2e-smoke-test / Phase 4b: Verify editor removed bait line` with `Editor failed to remove bait line E2E_EDITOR_BAIT_25300046587` and `PR head is still the bait commit — editor did not push a fix commit`. Related `review_autofix` failure `25300219172` failed in `Run Codex resolver, validate, stage, commit`.
- **Root cause category:** AI execution / resolver integration bug.
- **Exact fix:** Add a post-edit invariant in the resolver path:
  - if the model claims an edit but git diff is empty, abort immediately and mark the attempt as no-op,
  - if the target file still contains the bait marker after edit, stop and surface a resolver failure before downstream verification.
- **Expected reliability impact:** High for smoke coverage and review-autofix trustworthiness.
- **Rollback/fail-open:** Fail open by posting diagnostic artifacts instead of pushing partial/no-op commits.

### 3. Harden release push/tagging with a dry-run and clearer retry boundaries
- **Failure evidence:** `25273372573` failed in `release / Tag version and update stable pointer` with `failed to push some refs`.
- **Root cause category:** Git push/tag race or branch protection/precondition failure.
- **Exact fix:** Before mutating refs:
  - fetch/prune and verify remote state,
  - perform a dry-run push or equivalent eligibility check,
  - if stale remote state is detected, rebase/fetch once and re-evaluate instead of proceeding deep into the release job.
- **Expected reliability impact:** Medium-high for release success.
- **Rollback/fail-open:** Safe to stop before tagging if remote state is inconsistent.

### 4. Route ambiguous implement tasks to clarification instead of repeated retries
- **Failure evidence:** `implement` failures `25293932552`, `25293966619`, `25293940145`, `25294005792` showed repeated no-op/empty-output loops and at least one explicit clarification need.
- **Root cause category:** Orchestration / task classification.
- **Exact fix:** Add a classifier on implement failure text:
  - ambiguity/clarification-needed → hand off to clarify
  - empty diff/no-op loop → stop after second occurrence
- **Expected reliability impact:** Medium; should reduce failed implement runs and manual reruns.
- **Rollback/fail-open:** Safe; clarification is preferable to five failing attempts.

### 5. Surface fixture-level failures in nightly self-test output
- **Failure evidence:** `nightly_validation_selftest` run `25299383150` failed with `fixtures=3 passed=1 failed=2`, but the excerpt did not name the failing fixtures.
- **Root cause category:** Observability gap.
- **Exact fix:** Print fixture names and first failing assertion in the job summary and artifact manifest.
- **Expected reliability impact:** Medium for MTTR, low for raw failure rate.
- **Rollback/fail-open:** No behavioral risk.

## AI Memory Health

### Observed telemetry
Across deep-dive logs, the following `AI_MEMORY_TELEMETRY` operations were observed:

| Operation | Count |
|---|---:|
| `record-run-event` | 39 |
| `retrieve` | 18 |
| `record-candidate` | 8 |
| `processed-command-check` | 8 |
| `processed-command-claim` | 8 |
| `summarize_unselected_runs` | 3 |
| `finalize-task` | 2 |

### Retrieval effectiveness
- **Total retrieves:** 18
- **Retrieves with hits (`records_selected > 0`):** 8
- **Hit rate:** **44.4%**
- **Zero-result retrieves:** **10**
- **Average `estimated_tokens`:** **12.4**
- **Max `estimated_tokens`:** **28**
- **`keyword_method` distribution:**
  - `none`: **10**
  - `plain`: **8**
  - `llm`: **0**
- **`fail_open: true` retrieve count:** **0**
- **`enabled: false` retrieve count:** **0**

### What this means
- Memory retrieval is **enabled and cheap**, but not very effective for review flows.
- Reviewer runs frequently retrieved nothing. Zero-hit examples include `review_autofix` runs:
  - `25300219172`
  - `25303629616`
  - `25279043495`
  - `25278175531`
  - `25276795302`
  - `25304556994`
- Implement runs were the opposite: `25293966619`, `25293932552`, `25294005792`, `25293940145` each retrieved **1 record** with `estimated_tokens: 28` and `keyword_method: plain`.

### Health flags
- **No `llm` keyword-method usage** was observed. Retrieval is relying on `plain` or `none`, which likely limits recall for semantically similar failures.
- **Memory helper availability is inconsistent.**
  - `review_autofix` `25300219172` logged warnings such as:
    - `memory helper script missing; skipping run-start event`
    - `memory helper script missing; writing fallback reviewer memory context`
    - `memory helper script missing; skipping consensus candidate record`
  - `orchestrate_poll` `25305233480` reported helper-not-found warnings while still emitting poll telemetry.
- **Push retries are present but not yet severe.**
  - Max `push_attempts`: **2**
  - Entries with `push_attempts > 1`: **2**

### Recommendations
1. **Fix helper-script availability first.** Missing helpers degrade both retrieval and memory write-back quality.
2. **Enable semantic retrieval escalation for zero-hit review runs.** Start with `plain`, then escalate to an `llm`-keyword path only when `records_selected=0`.
3. **Log retrieval miss reasons.** Distinguish “no candidate records,” “budget cap,” and “filter mismatch.”
4. **Track two KPIs:** review retrieval hit rate and percent of runs with helper-missing warnings.

## GH API Call Audit

### Call-volume summary from deep-dive logs

| Workflow family | `gh api` mentions |
|---|---:|
| `test_and_mark_stable` | 632 |
| `workflow_log_analysis` | 353 |
| `review_autofix` | 199 |
| `implement` | 72 |
| `issue_pr_status` | 20 |
| `cancel_on_pr_close` | 16 |
| `copilot_pull_request_reviewer` | 10 |
| `orchestrate_poll` | 2 |

### Highest-volume runs

| Run ID | Workflow family | `gh api` mentions | Primary pattern |
|---|---|---:|---|
| `25281876234` | `test_and_mark_stable` | 230 | downstream Actions polling |
| `25273372573` | `test_and_mark_stable` | 228 | downstream Actions polling |
| `25281892914` | `workflow_log_analysis` | 178 | artifact/run log collection |
| `25300046587` | `test_and_mark_stable` | 174 | downstream Actions polling |
| `25300219172` | `review_autofix` | 82 | PR metadata/comments/reviews fetches |

### High-redundancy patterns and fixes

### 1. `test_and_mark_stable` repeatedly polls downstream workflows one-by-one
- **Evidence:** The family accounted for **632** `gh api` mentions across just 3 failed runs. The loop pattern repeatedly queried workflow runs and then queried individual run IDs.
- **Observed hotspot:** validate, orchestrate, workflow-log-analysis, validation-refresh, update-workflows, memory-maintenance, and test variants were all polled repeatedly.
- **Concrete change:** Replace per-workflow/per-run loops with a batched poll cycle:
  - one list query per cycle,
  - local map of workflow → latest run id/status,
  - query individual runs only when status changes or data is missing.
- **Estimated call reduction:** **50–70%** in this workflow family.
- **Rate-limit risk reduction:** High.

### 2. `review_autofix` re-fetches PR state in separate REST calls
- **Evidence:** `25300219172` used separate calls for PR payload, issue comments, reviews, review comments, and linked issues; family total was **199** mentions.
- **Concrete change:** Build one cycle-local PR context bundle at workflow start and share it across steps/sub-jobs. Prefer a single GraphQL fetch for PR + comments/reviews metadata where practical.
- **Estimated call reduction:** **30–50%** for `review_autofix`.
- **Rate-limit risk reduction:** Medium-high.
- **Repo-hygiene alignment:** This matches the repo’s apparent preference for batched GraphQL and cycle-local caches.

### 3. `cancel_on_pr_close` checks `/rate_limit` in no-op cancellation paths
- **Evidence:** Recent run `25305519674` was a no-op but still used `_rl_wait()` with `gh api -i /rate_limit`; recent run `25304794288` also had a no-cancellation success path.
- **Concrete change:** Only call `/rate_limit` after a real API failure or 403/429 response, not pre-emptively in no-op loops.
- **Estimated call reduction:** **1–2 calls/no-op run**, which matters because these runs are frequent and short.
- **Rate-limit risk reduction:** Medium.

### 4. `issue_pr_status` likely repeats issue-link lookups inside a single run
- **Evidence:** Family total **20** mentions; recent runs used GraphQL lookups and Telegram cleanup for linked PR/issue pairs.
- **Concrete change:** Cache linked issue IDs and cleanup targets in environment/output variables once per run.
- **Estimated call reduction:** **25–40%** in this family.
- **Rate-limit risk reduction:** Low-Medium.

### 5. `copilot_pull_request_reviewer` artifact lookups should be reused
- **Evidence:** `25304557192` called `gh api /repos/.../actions/runs/25304557192/artifacts`; family total **10** mentions.
- **Concrete change:** Resolve artifacts once, persist artifact ID/name mapping for downstream steps.
- **Estimated call reduction:** Small but safe.
- **Rate-limit risk reduction:** Low.

## Prompt Cache & Memory System

### Prompt cache status
- Prompt cache appears **configured/enabled** in review flows (`OPENROUTER_PROMPT_CACHE_DISABLED: false`).
- However, the available deep-dive logs did **not** include usable cache counters:
  - `prompt_tokens=na`
  - `completion_tokens=na`
  - `total_tokens=na`
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`

### Assessment
- **Current cache behavior cannot be quantified.**
- That means the dataset does **not** support claims about cache hit rate, creation/read volume, or actual token savings.
- Because long `review_autofix` runs recur on similar paths, cache should be a meaningful lever, but the current logging does not prove whether reuse is happening.

### Likely cache-fragmentation causes
These are **inferences** from the observed workflow shape, not directly measured cache misses:
1. **Dynamic run-specific data may be too early in the prompt prefix**  
   Examples in logs include PR numbers, branch refs, callback payloads, linked issue details, timestamps, and review-thread context.
2. **Reviewer-panel prompts likely vary per model and per pass**  
   Six-model review plus two-pass mode naturally reduces shared stable prefixes.
3. **Fallback memory context differs when helper scripts are missing**  
   That variance can fragment otherwise reusable prompt prefixes.

### Recommendations
1. **Emit real provider cache counters** into step logs and summaries.
2. **Stabilize prompt prefixes** by placing static policies/instructions before run-specific metadata.
3. **Move volatile context later** in the prompt where the provider’s cache strategy benefits most.
4. **Normalize fallback memory blocks** so helper-missing behavior does not create a separate prompt shape.
5. **Track three metrics per AI step:** prompt tokens, cache-read tokens, cache-create tokens.

### Expected impact
- **Tokens:** unquantified until counters are emitted.
- **Latency:** likely moderate for repeated review paths.
- **Reliability:** moderate, because helper-missing behavior currently changes context shape and can reduce consistency.

## Orchestrator Health

### What looks healthy
- Clarify/respond/plan skip logic is fast:
  - `clarify`, `plan`, and `orchestrate_clarify_respond` frequently skip in **0–2s** when conditions evaluate false.
- `orchestrate_poll` is operational and records completion telemetry:
  - example `25305233480` reported `poll_completed` with `has_work=false`.

### What looks unhealthy
### 1. Too many review runs are cancelled before meaningful work
- **Evidence:** `review_autofix` has **38 cancelled** runs out of **77** total. Recent examples include `25304555548`, `25303628509`, `25302805121`, `25302037708`, `25301945966`, `25301294320`, many while waiting for a hosted runner.
- **Impact:** High orchestration noise, wasted queue time, and duplicated trigger overhead.

### 2. Implement stage can get stuck in exploration instead of handing off
- **Evidence:** `implement` failures repeatedly looped through no-op/empty-output attempts instead of escalating to clarify.
- **Impact:** Preventable retries and delayed user-facing progress.

### 3. No-work poll cycles still execute checkout/setup
- **Evidence:** `25305233480` had `has_work=false` but still checked out the repository with `fetch-depth: 0`.
- **Impact:** Low per cycle, moderate in aggregate.

### 4. Memory-helper availability is inconsistent across orchestration paths
- **Evidence:** helper-missing warnings in both `review_autofix` and `orchestrate_poll`.
- **Impact:** Reduced memory continuity and noisier decision-making.

### Smallest safe mitigations
1. **Deduplicate `review_autofix` triggers by PR head SHA** before scheduling expensive work.
2. **Add an “implement exhausted / clarify needed” terminal state** after repeated no-op attempts.
3. **Front-load no-work detection** in poller workflows.
4. **Treat memory-helper absence as a tracked degraded-mode state**, not a silent warning.

### Observable indicators to track
- `% of `review_autofix` runs cancelled before first AI output`
- `% of `orchestrate_poll` cycles with `has_work=false` but full checkout performed`
- `implement average attempts per successful diff`
- `% of runs with memory-helper-missing warnings`
- `test_and_mark_stable` median stage reached before failure`

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact across the pipeline.

### 1. Review/autofix compute dominates the PR path
- **Stage:** review/autofix
- **Evidence:** `review_autofix` p95 is **1715.6s**; long runs from **908s** to **1730s** are common. `review / codex-agent (claude-branch-review)` dominates successful and failed runs alike.
- **Bottleneck type:** Compute + runner wait + review fan-out.
- **Recommendation:** Reduce reviewer breadth/two-pass on comment-only paths first.

### 2. Release/test-mark-stable failures occur far too late
- **Stage:** validate/orchestrate/release
- **Evidence:** all 3 `test_and_mark_stable` runs failed after **40–57 minutes**.
- **Bottleneck type:** Retry/poll overhead + missing preflight checks.
- **Recommendation:** Add preflight checks so deterministic failures happen in the first few minutes.

### 3. `ci` is a broad but stable wall-clock drag
- **Stage:** validate
- **Evidence:** `ci` p50 **606s**, p95 **653.4s**, with `lint` dominating nearly every recent run.
- **Bottleneck type:** Compute serialization.
- **Recommendation:** Parallelize stable test/check groups after high-priority review/release fixes.

### 4. Implement stage loses time in unproductive retry loops
- **Stage:** implement
- **Evidence:** failed implement runs spent **144–331s** without producing actionable changes.
- **Bottleneck type:** Retry/orchestration.
- **Recommendation:** Hand ambiguous tasks to clarify after early no-op detection.

### 5. Poll/queue overhead exists even in no-op paths
- **Stage:** orchestrate/poll
- **Evidence:** no-work poll cycles still run checkout; multiple review/cancel jobs wait for hosted runners even when they later skip or cancel.
- **Bottleneck type:** Queueing + setup overhead.
- **Recommendation:** Short-circuit before checkout and dedupe triggers.

### Flow map summary
- **Clarify:** mostly healthy, often skipped quickly.
- **Plan:** mostly healthy, often skipped quickly; not a major bottleneck.
- **Implement:** reliability/cost issue due to retry loops.
- **Review/autofix:** largest combined speed/cost bottleneck.
- **Validate/CI:** consistent but second-order optimization target.
- **Release/orchestrate:** highest-severity reliability bottleneck because failures land late and block downstream release flow.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail latency: avg **486.4s**, p95 **1715.6s**, many cancellations.
- `ci` steady **~10 minute** runtime dominated by `lint`.
- `test_and_mark_stable` catastrophic failure profile: **3 runs, 3 failures, avg 3040s**.

**Top failure modes**
- Release path preconditions caught too late:
  - missing `.git` context (`25281876234`)
  - push failure (`25273372573`)
  - editor smoke no-op (`25300046587`)
- Implement-stage no-op/empty-output loops:
  - `25293966619`, `25293932552`, `25293940145`, `25294005792`
- Nightly self-test lacks actionable failure detail:
  - `25299383150`

**Highest-cost drivers**
- Multi-model, two-pass `review_autofix` reviewer panel on low-risk/comment-only paths.
- Long-running `ci` `lint` job repeated across many PRs.
- Poll-heavy release/orchestration workflows with high GH API churn.

**Top 3 prioritized actions**
1. **Right-size `review_autofix`** for comment-only and branch-review paths: fewer reviewers, no two-pass by default.
2. **Add `test_and_mark_stable` preflight checks** for git context, push eligibility, and smoke editor-write behavior.
3. **Terminate `implement` no-op loops early** and route ambiguity to clarification instead of repeated retries.

## Metrics Appendix

### Overall repository metrics

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 896 | 264 | 10 | 42 | 580 | 1.12% | 116.82 | 1.0 | 626.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 77 | 35 | 1 | 38 | 3 | 486.38 | 49.0 | 1715.6 |
| `ci` | 55 | 54 | 1 | 0 | 0 | 604.24 | 606.0 | 653.4 |
| `test_and_mark_stable` | 3 | 0 | 3 | 0 | 0 | 3040.0 | 3235.0 | 3438.4 |
| `implement` | 152 | 13 | 4 | 4 | 131 | 23.34 | 1.0 | 187.4 |
| `copilot_pull_request_reviewer` | 22 | 22 | 0 | 0 | 0 | 169.23 | 161.0 | 306.8 |
| `orchestrate_poll` | 49 | 49 | 0 | 0 | 0 | 52.96 | 45.0 | 116.6 |
| `plan` | 152 | 17 | 0 | 0 | 135 | 16.96 | 1.0 | 146.7 |
| `clarify` | 178 | 20 | 0 | 0 | 158 | 12.28 | 1.0 | 89.15 |

### Key run evidence

| Run ID | Workflow family | Conclusion | Duration (s) | Failure point / hotspot |
|---|---|---|---:|---|
| `25303629616` | `review_autofix` | success | 1730 | `review / codex-agent (claude-branch-review)` dominated |
| `25304556994` | `review_autofix` | success | 1358 | comment-only path still used 6 successful reviewers |
| `25300219172` | `review_autofix` | failure | 1628 | `review / codex-agent` → `Run Codex resolver, validate, stage, commit` |
| `25300046587` | `test_and_mark_stable` | failure | 2424 | `e2e-smoke-test` → bait line not removed |
| `25273372573` | `test_and_mark_stable` | failure | 3235 | `release` → push refs failed |
| `25281876234` | `test_and_mark_stable` | failure | 3461 | `sync-to-main` → not a git repository |
| `25293966619` | `implement` | failure | 331 | `Run Codex implementation` → no actionable output loop |
| `25293932552` | `implement` | failure | 152 | `Run Codex implementation` → stuck exploration |
| `25272902365` | `ci` | failure | 555 | `lint` → `Implement post-Codex recovery unit tests` |
| `25299383150` | `nightly_validation_selftest` | failure | 95 | `validation-selftest` → fixtures=3 passed=1 failed=2 |

### AI memory metrics

| Metric | Value |
|---|---:|
| Retrieve operations | 18 |
| Retrieve hits | 8 |
| Retrieve hit rate | 44.4% |
| Zero-result retrieves | 10 |
| Avg estimated retrieval tokens | 12.4 |
| Max estimated retrieval tokens | 28 |
| `keyword_method=plain` | 8 |
| `keyword_method=none` | 10 |
| `keyword_method=llm` | 0 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Max `push_attempts` | 2 |
| Entries with `push_attempts > 1` | 2 |

### GH API call summary

| Workflow family | `gh api` mentions | Primary issue |
|---|---:|---|
| `test_and_mark_stable` | 632 | repeated downstream workflow polling |
| `workflow_log_analysis` | 353 | repeated run/artifact collection |
| `review_autofix` | 199 | repeated PR/comments/reviews metadata fetch |
| `implement` | 72 | repeated state/context fetch during retries |
| `issue_pr_status` | 20 | repeated issue/PR link resolution |
| `cancel_on_pr_close` | 16 | rate-limit checks in no-op cancellation paths |
| `copilot_pull_request_reviewer` | 10 | artifact endpoint reuse opportunity |
| `orchestrate_poll` | 2 | minimal |

### Token and cache metrics availability

| Metric | Availability | Notes |
|---|---|---|
| Prompt tokens | Not reliably available | review cache probes reported `na` |
| Completion tokens | Not reliably available | review cache probes reported `na` |
| Total tokens | Not reliably available | review cache probes reported `na` |
| Cache creation tokens | Not reliably available | review cache probes reported `na` |
| Cache read tokens | Not reliably available | review cache probes reported `na` |
| Memory retrieval estimated tokens | Available partially | retrieved from `AI_MEMORY_TELEMETRY` only |

### Data gaps to close next collection window
- Emit provider token/cache counters for every AI step.
- Include fixture names in nightly self-test failure summaries.
- Add per-step queue time to run summaries for review and poll workflows.
- Log first-AI-output timestamp for `review_autofix` and `implement` to separate queue/setup vs model time.

## Deep Audit — Workflows & Scripts (2026-05-04)

### Section 1: Bug & Correctness Sweep

- **ID**: BUG-001  
  **File path**: `.github/workflows/review_autofix.yml:478-528`  
  **Severity**: High  
  **Category tag**: `bug`  
  **Description**: The post-merge standalone-validate path dispatches validation only once (`validation_dispatched` flips to `true` at lines 517-521), but it removes `ai:orchestrator-validate-required` from every later linked issue in the loop at lines 527-528. On a PR linked to multiple validation-required issues, only the first issue actually causes a `gh workflow run`, while the remaining issues lose their retry signal without ever being validated.  
  **Recommended fix**: Either dispatch once per linked issue and pass the concrete issue number into the validate workflow, or only remove `ai:orchestrator-validate-required` from the issue that actually triggered the dispatch. The safer pattern is “consume label only after successful per-issue dispatch,” mirroring the repo’s other label-transition gates in `scripts/orchestrate_poll_process.sh`.

- **ID**: BUG-002  
  **File path**: `.github/workflows/implement.yml:116-130,2665-2673`  
  **Severity**: Medium  
  **Category tag**: `bug`  
  **Description**: Implement still uses `gh pr list --search "issue:${ISSUE_NUMBER}"` to decide whether a PR already exists. That search is text-based and can match unrelated PRs that merely mention the issue number. The repo already documents this exact failure mode and replaced it in plan with timeline cross-reference logic at `.github/workflows/plan.yml:404-418`; implement has not been updated to match. A false positive here can skip implementation entirely or bind later steps to the wrong PR URL.  
  **Recommended fix**: Replace both search-based checks with the same timeline/cross-reference approach used in `plan.yml`, ideally extracted into a shared helper in `scripts/gh_helpers.sh` so plan and implement cannot drift again.

### Section 2: GitHub API Call Redundancy Audit

- **ID**: API-001  
  **File path**: `.github/workflows/review_autofix.yml:1336-1342`  
  **Severity**: Medium  
  **Category tag**: `api-redundancy`  
  **Description**: The “Collect PR metadata” step fetches the same PR context as four separate logical API calls in one straight-line path: PR payload, issue comments, reviews, and review comments. This duplicates functionality already provided by the consolidated `gh_pr_with_all_comments` helper in `scripts/gh_helpers.sh`, which was built specifically to return this bundle in one GraphQL-first call with REST parity fallback.  
  **Current call count**: `4` logical calls per run path, plus pagination/retry overhead.  
  **Proposed call count**: `1` logical call on the primary path, with helper-managed fallback only when pagination or GraphQL limits require it.  
  **Batching pattern to extend**: `scripts/gh_helpers.sh` consolidated PR-context pattern (`gh_pr_with_all_comments`).  
  **Recommended fix**: Replace the four inline fetches with a single helper call that writes `meta/comments/review_comments` once and reuses that JSON for `PR_PAYLOAD_FILE`, `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, and `PR_REVIEW_COMMENTS_FILE`.

- **ID**: API-002  
  **File path**: `scripts/orchestrate_poll_process.sh:3407-3519`  
  **Severity**: Medium  
  **Category tag**: `api-redundancy`  
  **Description**: `finalize_integration_merge_if_needed()` re-fetches `repos/${GITHUB_REPOSITORY}/pulls/${final_pr}` eight times on the hot path when a final PR already exists and the function reaches merge evaluation: two calls at lines 3411-3412, three more at 3466-3468, and three more at 3517-3519. All are reading fields from the same resource (`state`, `mergeable`, `merged_at`) within one function invocation.  
  **Current call count**: `8` PR-fetch calls on the hot path.  
  **Proposed call count**: `3` snapshots max: one early-state read, one pre-merge read, one post-merge read.  
  **Batching pattern to extend**: The cycle-local cache pattern already used in `scripts/orchestrate_poll_process.sh` (`_fetch_linked_pr_status_graphql`, `ACTIVE_WORKFLOW_ISSUES`).  
  **Recommended fix**: Fetch the full PR JSON once per decision phase into a temp file or shell variable, derive all three fields from that cached object, and only refresh after a state-changing operation such as `gh pr merge`.

- **ID**: BATCH-001  
  **File path**: `.github/workflows/review_autofix.yml:478-528`  
  **Severity**: Medium  
  **Category tag**: `api-batching`  
  **Description**: When `closingIssuesReferences` does not yield label data, the fallback path parses issue numbers from PR text and then performs `gh issue view ... --json labels` inside the loop for each issue at lines 503-505. This is the per-item REST pattern that CLAUDE.md §15 calls out as a review-blocker in loops.  
  **Current call count**: `2 + N` logical calls on the fallback path (`1` GraphQL call, `1` PR-body fetch, then `N` issue-label calls).  
  **Proposed call count**: `2` logical calls total on the fallback path (`1` PR-body fetch, `1` batched GraphQL issue-label query for all parsed issue numbers).  
  **Batching pattern to extend**: `scripts/orchestrate_poll_process.sh` aliased GraphQL issue batching (`_fetch_candidate_issue_details_graphql`).  
  **Recommended fix**: After parsing fallback issue numbers, batch their labels with one aliased GraphQL query and feed the loop from that local map instead of calling `gh issue view` per issue.

### Section 3: Code Duplication & Modularization Opportunities

- **ID**: DUP-001  
  **File path**: `.github/workflows/clarify.yml:51-120; .github/workflows/plan.yml:80-152; .github/workflows/implement.yml:224-293; .github/workflows/orchestrate_clarify_respond.yml:88-157; .github/workflows/orchestrate_poll.yml:167-200; .github/workflows/validate.yml:183-476`  
  **Severity**: Low  
  **Category tag**: `duplication`  
  **Description**: The repository repeatedly inlines large “support source checkout / copy scripts / copy prompts / fallback to main” shell blocks across multiple workflows. The validate variant is the largest, but the same control flow and failure semantics recur across clarify, plan, implement, orchestrate-clarify-respond, orchestrate-poll, and validate. This duplication is already causing drift: some workflows source central helpers, others reimplement retry/bootstrap logic inline.  
  **Recommended fix**: Extract a shared module such as `scripts/bootstrap_support_source.sh` with a signature like `bootstrap_support_source <workflow_source_repo> <resolved_ref> <mode> <path...>`. Update the caller workflows to pass only their required file lists and receive normalized outputs (`SUPPORT_ROOT`, `SCRIPT_REF`, copied-path manifest).

- **ID**: DUP-002  
  **File path**: `.github/workflows/implement.yml:2344-2362; scripts/orchestrate_poll_process.sh:4666-4690`  
  **Severity**: Low  
  **Category tag**: `duplication`  
  **Description**: The “ancestor no-op chain” walk is implemented twice: once inline in implement’s no-op handler and once as `count_noop_ancestors()` in the poller. Both traverse `Re-issued from #N` markers and query ancestor comments for the same `produced no repository changes` marker. The logic is already behaviorally coupled by comments and thresholds, but the code paths can now diverge independently.  
  **Recommended fix**: Move the shared traversal into one shell helper, e.g. `count_noop_ancestors <repo> <issue_number> <max_depth>`, and call it from both implement and the poller. `scripts/gh_helpers.sh` is the best existing shared home because both callers already depend on GitHub API helpers there.

### Section 4: Expression Size Limit Risk Assessment

- **ID**: EXPR-001  
  **File path**: `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity**: High  
  **Category tag**: `expression-limit`  
  **Description**: The inline “wait for review workflow” shell block is already close to the 21,000-character expression ceiling for interpolated `run:` bodies. It contains polling logic, live-log heuristics, timeout handling, and multiple `${{ }}` substitutions in one step. This file has already hit expression-length failures historically, so this block is a high-risk regrowth point.  
  **Estimated expression size**: `~19,696 chars`  
  **Headroom remaining**: `~1,304 chars`  
  **Recommended fix**: Extract the full wait/poll logic into `scripts/wait_for_review_run.sh` and pass only a small set of environment variables from YAML. If extraction is deferred, split live-log shortcuts and inactivity detection into separate steps.

- **ID**: EXPR-002  
  **File path**: `.github/workflows/validate.yml:183-476`  
  **Severity**: Medium  
  **Category tag**: `expression-limit`  
  **Description**: The validate support-bootstrap step inlines repo/ref resolution, remote checkout, selective copying, template staging, schema staging, and fallback logic in a single interpolated `run:` block. The block is below the hard limit today but already well into the medium-risk range.  
  **Estimated expression size**: `~16,485 chars`  
  **Headroom remaining**: `~4,515 chars`  
  **Recommended fix**: Move the bootstrap logic into an external script under `scripts/` (ideally the same shared bootstrap module from DUP-001), leaving YAML to set inputs only.

- **ID**: EXPR-003  
  **File path**: `.github/workflows/review_autofix.yml:1251-1573`  
  **Severity**: Medium  
  **Category tag**: `expression-limit`  
  **Description**: The “Collect PR metadata” step combines an inline rate-limit wrapper, no-PR synthetic metadata path, four REST fetches, and linked-issue caching logic in one interpolated block. Review-autofix has already hit expression-limit failures before, so keeping large helper logic inline here is a recurring risk.  
  **Estimated expression size**: `~16,437 chars`  
  **Headroom remaining**: `~4,563 chars`  
  **Recommended fix**: Extract this step to a dedicated helper script, e.g. `scripts/review_fetch_pr_context.sh`, and call central GitHub helpers from there instead of embedding the retry wrapper in YAML.

- **ID**: EXPR-004  
  **File path**: `.github/workflows/orchestrate_clarify_respond.yml:813-1096`  
  **Severity**: Medium  
  **Category tag**: `expression-limit`  
  **Description**: The auto-answer posting step embeds memory-claim checks, loop-guard logic, escalation comment construction, Telegram notification, and processed-command completion in one interpolated `run:` block. It is above the 15,000-character threshold already, so small future additions can push it into the same failure mode the repo has previously seen.  
  **Estimated expression size**: `~15,140 chars`  
  **Headroom remaining**: `~5,860 chars`  
  **Recommended fix**: Split this into an external script or at minimum separate the memory/loop-guard phase from the comment-posting/escalation phase.

- No workflow file exceeded the 800 KB warning threshold; the largest workflow reviewed was `review_autofix.yml` at `268,059` bytes.

### Section 5: Cross-Cutting Concerns

- **ID**: SHELL-001  
  **File path**: `scripts/orchestrate_poll_process.sh:10675-10676`  
  **Severity**: Medium  
  **Category tag**: `shellcheck`  
  **Description**: ShellCheck flags SC2086 here: `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"`. The unquoted expansion allows word-splitting and glob expansion before sorting. In a poller that intentionally builds composite issue lists, that can silently change the iteration set or order.  
  **Recommended fix**: Normalize `ISSUE_NUMS` to one issue number per line before sorting, e.g. build it as a newline-delimited string or array and iterate with `while IFS= read -r inum; do ...; done`. Do not rely on implicit shell splitting.

- **ID**: SHELL-002  
  **File path**: `scripts/review_commit_changes.sh:455-455; scripts/review_conflict_resolve.sh:936-936`  
  **Severity**: Medium  
  **Category tag**: `shellcheck`  
  **Description**: Both scripts set the remote URL with unquoted credential-bearing expansions: `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`. ShellCheck flags SC2086 in both locations. Even if the current token format is usually safe, this is fragile shell and risks malformed commands if either component ever contains shell-significant characters.  
  **Recommended fix**: Build the URL in a quoted variable and pass it as one argument, e.g. `remote_url="https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"` then `git remote set-url origin "${remote_url}"`.

- **ID**: DEAD-001  
  **File path**: `scripts/validate_changed_files_syntax.sh:71-73`  
  **Severity**: Low  
  **Category tag**: `dead-code`  
  **Description**: The redaction case list contains unreachable patterns. `*.env*` on line 71 already matches the later `*.envrc`/`.env` cases on line 73, which ShellCheck reports as SC2221/SC2222. That makes the later arms dead and obscures the intended redaction policy.  
  **Recommended fix**: Remove the unreachable arms or reorder the patterns from most specific to least specific, then keep a single comment describing the unified over-redaction policy.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 9 | BUG-002, API-001, API-002, BATCH-001, EXPR-002, EXPR-003, EXPR-004, SHELL-001, SHELL-002 |
| Low | 3 | DUP-001, DUP-002, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 7 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-04)

### Safety Tag Legend
`SAFE_TO_MERGE` means the reduction is statically provable and does not cross a race-sensitive, retry-sensitive, pagination-sensitive, or concurrency-sensitive boundary. `NEEDS_VERIFICATION` means the overlap looks real, but at least one safe-merge precondition could not be fully proven from static reading alone. `RISKY_SKIP` means the call is in a path where consolidation/removal could change retry, polling, race-defense, or observability behavior and therefore must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Create-issue response already contains the URL later re-fetched
- **Safety tag**: `NEEDS_VERIFICATION`
- **File path and line ranges**: `.github/workflows/test-and-mark-stable.yml:372-377`
- **Current call count**: `2`
- **Proposed call count**: `1`
- **Endpoint(s)**:
  - `POST /repos/{repo}/issues`
  - `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**:
  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```
  The second call only extracts `html_url` for the issue that was just created by the first call in the same step. The create response from GitHub issue creation normally includes both `number` and `html_url`, so this is overlapping data from the same newly-created resource.
- **Proposed fix**: Capture the full create response once into a temp JSON file or shell variable, then derive both `ISSUE_NUMBER` and `ISSUE_URL` from that single response instead of issuing the follow-up `GET`. Example shape: extend the existing create call to store the full JSON, then read `.number` and `.html_url`.
- **Safety rationale**: This is in the same workflow step with no intervening mutation, but it merges a `POST` create response with a later `GET` read, so the strict `SAFE_TO_MERGE` same-endpoint precondition is not fully met from static review alone.
- **Downstream signal**: Verify in a dry run that `gh api POST repos/${TEST_REPO}/issues` always returns `html_url` under this token/workflow context; if confirmed, collapse lines 372-377 to a single captured create-response parse.

#### MERGE-002 — `review_rb_judge.sh` can batch linked-issue body fetch into the existing GraphQL query
- **Safety tag**: `NEEDS_VERIFICATION`
- **File path and line ranges**: `scripts/review_rb_judge.sh:146-151`, `scripts/review_rb_judge.sh:161-168`
- **Current call count**: `1 + up to N`
- **Proposed call count**: `1`
- **Endpoint(s)**:
  - `POST /graphql` for `repository.pullRequest(number).closingIssuesReferences`
  - `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    -f owner="${REPOSITORY%/*}" \
    -f name="${REPOSITORY#*/}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
  ...
  BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
  if [ -z "${FIRST_ISSUE_BODY}" ]; then
    FIRST_ISSUE_BODY="${BODY}"
  fi
  ```
  The script first asks GraphQL for linked issue numbers, then re-fetches issue bodies one-by-one via REST to populate `FIRST_ISSUE_BODY`, even though the GraphQL selection can be extended to include `body` directly on each linked issue node.
- **Proposed fix**: Extend the existing GraphQL query in `scripts/review_rb_judge.sh` to request `nodes { number body title }`, then populate `FIRST_ISSUE`/`FIRST_ISSUE_BODY` from that payload and delete the per-issue REST body lookups. This follows the repository’s batched GraphQL pattern without introducing a new helper.
- **Safety rationale**: The merge target is nearby and structurally compatible, but the current code’s fallback granularity changes from per-issue REST to all-in-one GraphQL fields, so `SAFE_TO_MERGE` error-semantic equivalence is not fully proven without runtime verification.
- **Downstream signal**: Verify the expanded GraphQL payload against PRs with 0, 1, and multiple linked issues, and confirm the judge prompt still receives the same first linked issue body when GraphQL succeeds and still degrades acceptably when it fails.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — `implement.yml` re-fetches the same issue after precheck already loaded it
- **Safety tag**: `NEEDS_VERIFICATION`
- **File path and line ranges**: `.github/workflows/implement.yml:64-66`, `.github/workflows/implement.yml:532-538`, `.github/workflows/implement.yml:629-635`
- **Current call count**: `2` on the happy path, `3` on the degraded path
- **Proposed call count**: `1` on the happy path, preserving fallback only if the cached payload is missing/corrupt
- **Endpoint(s)**:
  - `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ```
  later:
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  ```
  and the later label-validation fallback can call the same endpoint again:
  ```bash
  if [ -z "${ISSUE_LABELS_JSON}" ]; then
    ISSUE_LABELS_JSON="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]')"
  fi
  ```
  The precheck already proves the same issue exists and reads state/labels; the later metadata step then fetches the full issue again, and the label-validation fallback can fetch it a third time if the file is absent or invalid.
- **Proposed fix**: Expand the precheck fetch to capture the full issue JSON once, persist it into `${ISSUE_META_FILE}` during runtime setup (or a temp env file), and have both “Fetch issue metadata” and “Validate approval phase label” consume that cached payload first. Keep the current fallback re-fetch only when the cached file is missing or unparsable.
- **Safety rationale**: This is the same endpoint in the same job, but the reuse spans multiple workflow steps and there is no static proof that the workflow intentionally does not want a fresher issue snapshot after the precheck.
- **Downstream signal**: Verify on a live run that no step between precheck and metadata fetch intentionally relies on a refreshed issue body/labels, then reuse the initial full payload and keep the current parse-failure fallback.

#### REUSE-002 — `orchestrate_clarify_respond.yml` fetches the child issue and tracking issue twice
- **Safety tag**: `NEEDS_VERIFICATION`
- **File path and line ranges**: `.github/workflows/orchestrate_clarify_respond.yml:61-77`, `.github/workflows/orchestrate_clarify_respond.yml:400-411`
- **Current call count**: `4`
- **Proposed call count**: `2`
- **Endpoint(s)**:
  - `GET /repos/{repo}/issues/{ISSUE_NUMBER}`
  - `GET /repos/{repo}/issues/{TRACKING_NUM}`
- **Evidence**:
  Early step:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ```
  Later step:
  ```bash
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```
  The first step already has the full child issue payload and enough data to derive `TRACKING_NUM`; the tracking issue call can also return both `title` and `body` at once if cached.
- **Proposed fix**: In `Check orchestrator metadata`, write the full child-issue JSON and the full tracking-issue JSON to runtime files or exported heredoc env vars, then reuse them in the later “Fetch child issue metadata” step. Only fall back to re-fetch if the cached JSON is missing or invalid.
- **Safety rationale**: The duplicate calls are obvious, but they cross a step boundary and the later prompt-assembly phase may intentionally prefer a fresh snapshot, which static reading alone does not disprove.
- **Downstream signal**: Verify that no issue/title/body mutation is expected between the metadata gate and prompt assembly; if not, persist both JSON payloads from the early step and reuse them later.

#### REUSE-003 — `issue_pr_status.yml` re-fetches issue bodies after already classifying orchestrator-managed issues
- **Safety tag**: `NEEDS_VERIFICATION`
- **File path and line ranges**: `.github/workflows/issue_pr_status.yml:297-349`, `.github/workflows/issue_pr_status.yml:503-508`
- **Current call count**: `1` batched classification call plus `up to N` later body probes on the success path
- **Proposed call count**: `1` on the success path
- **Endpoint(s)**:
  - `POST /graphql` for aliased issue classification
  - `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**:
  Earlier classification step already determines managed/tracking status:
  ```bash
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ...
  _managed_issues="$(printf '%s' "${ORCH_RESP}" | jq -r '
    .data.repository | to_entries[] | .value | select(. != null) |
    select(
      ((.labels.nodes // []) | map(.name) | index("ai:orchestrator-managed")) != null
      or
      ((.body // "") | contains("Managed by: AI Orchestrator"))
    ) | .number
  ' 2>/dev/null || echo '')"
  ```
  But the later Telegram step probes issue bodies again:
  ```bash
  BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
  if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
    IS_ORCHESTRATED="true"
  fi
  ```
  The second probe is redundant when the earlier step already knows whether any linked issue is managed.
- **Proposed fix**: At the end of “Update linked issue labels when PR closes”, export a boolean such as `ANY_MANAGED_LINKED_ISSUE=true|false` derived from `MANAGED_ISSUES`, and consume that in the Telegram alert step instead of re-querying each issue body. Preserve the current fallback behavior only if the earlier classifier itself had to fail open.
- **Safety rationale**: The data is already available, but the earlier step intentionally treats some REST-classification failures conservatively, so alert suppression reuse must be verified not to change fail-open alert behavior.
- **Downstream signal**: Verify behavior across three cases—GraphQL classification success, GraphQL failure with REST fallback success, and REST fallback failure treated as tracking—before replacing the later per-issue body probes with an exported boolean.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — `COMMITS_AFTER` commits listing is fetched but never consumed
- **Safety tag**: `SAFE_TO_MERGE`
- **File path and line ranges**: `.github/workflows/test-and-mark-stable.yml:1508-1511`
- **Current call count**: `1`
- **Proposed call count**: `0`
- **Endpoint(s)**:
  - `GET /repos/{repo}/commits?sha={branch}&per_page=20`
- **Evidence**:
  ```bash
  COMMITS_AFTER=$(gh api "repos/${TEST_REPO}/commits?sha=${BRANCH}&per_page=20" \
    --jq "[.[] | select(.sha != \"${BAIT_SHA}\") | .sha] | length" 2>/dev/null || echo "0")
  # The PR head SHA should differ from the bait SHA.
  PR_HEAD=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
  ```
  `COMMITS_AFTER` is assigned and never read afterward; the actual guard uses `PR_HEAD` vs `BAIT_SHA`. Static scan of this file found no later `${COMMITS_AFTER}` expansion.
- **Proposed fix**: Delete the `COMMITS_AFTER` assignment entirely and keep the `PR_HEAD != BAIT_SHA` check as the sole post-review commit signal.
- **Safety rationale**: The result is never consumed, and the call is not in a retry loop, auth probe, pagination loop, stall-recovery path, or log-key contract path.
- **Downstream signal**: Remove the `COMMITS_AFTER` `gh api /commits` call and leave the existing `PR_HEAD` comparison unchanged.

### Cross-References to Deep Audit Section
- API-001: `NEEDS_VERIFICATION` — agreed, but the implement step must preserve `PR_REVIEWS_FILE` semantics because `.github/workflows/review_autofix.yml:1433-1464` still consumes the reviews array, not just issue/review comments.
- API-002: `RISKY_SKIP` — agreed on redundancy, but this sits inside `scripts/orchestrate_poll_process.sh` final-merge race-defense logic, which the policy explicitly excludes from auto-merge treatment.
- BATCH-001: `NEEDS_VERIFICATION` — agreed; batched GraphQL is the right shape, but the fallback path must preserve the current conservative handling of parsed issue numbers whose labels cannot be resolved.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | DEAD-API-001 |
| NEEDS_VERIFICATION | 5 | MERGE-001, MERGE-002, REUSE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 0 | — |

### Implement-Stage Handoff
- DEAD-API-001
