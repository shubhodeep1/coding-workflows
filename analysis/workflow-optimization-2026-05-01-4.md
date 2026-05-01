## Executive Summary

- **`review_autofix` is the dominant end-to-end bottleneck and likely the biggest avoidable AI spend.** Family p95 is **1,634.6s** across **102** runs, and deep-dive runs **25207020260 (2,555s)**, **25201255563 (1,846s)**, and log-summary run **25206256213 (1,230s)** all show long reviewer-panel execution even on Claude-branch/comment-only paths where editor/commit/judge are skipped. **Estimated impact:** save **15–30 min** on affected runs and materially reduce reviewer-token spend. **Confidence:** high.

- **The E2E smoke gate is losing reliability and time in the watcher phase, not the model phase.** Failed smoke run **25204168842** ended after **4,281s** with `No review_autofix run with head_sha=... ever appeared within 30m`, and its own logs show the PR could already be merged/closed before bait injection. **Estimated impact:** cut **30–60 min** from failed release-validation cycles and reduce thousands of status/API polls. **Confidence:** high.

- **Implement failures are burning tokens on futile retries after exploration loops stall.** Failure run **25208345846** bailed with `2 consecutive attempts with no actionable output`; downstream evidence reports attempts of **8,045 + 92,035 + 10,025 = 110,105 tokens** before abort. Similar failures occurred in **25206967321** and **25206976031**. **Estimated impact:** save **50k–100k+ tokens per stuck run** and reduce implement failure rate. **Confidence:** high.

- **CI reliability regressed due to assertion drift in the new post-Codex recovery tests, not broad lint instability.** Seven CI failures clustered between **08:26Z–08:38Z** on May 1, 2026 in step `Implement post-Codex recovery unit tests`, and deep-dive run **25208295433** shows only **2 failing tests out of 36**. **Estimated impact:** restore most of the **12.5% CI family failure rate** quickly with a low-risk fix. **Confidence:** high.

- **AI memory is operational but only moderately effective in sampled deep dives.** Across observed telemetry, retrieve hit rate was **50% (6/12)**, average retrieval size was only **14 estimated tokens**, and reviewer retrieves often returned **0 records** with `keyword_method: "none"`. **Estimated impact:** modest latency/cost gains and better reviewer consistency if reviewer retrieval quality improves. **Confidence:** medium-high.

- **Prompt cache is enabled but not auditable in current telemetry.** In slow review run **25207020260**, `OPENROUTER` usage lines reported `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`, so cache efficiency cannot be verified despite cache being enabled. **Estimated impact:** medium cost savings once visibility and prefix stability are fixed. **Confidence:** high.

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fix smoke-gate watcher identity/ordering so `test_and_mark_stable` stops waiting 30 minutes for runs that never arrive
- **Critical-path win**
- **Evidence:** Failed release-validation run **25204168842** took **4,281s** and ended with `No review_autofix run with head_sha=808a6370... ever appeared within 30m`. The same log shows the PR could already be closed before bait injection: `PR ... is already ... merged/closed before bait could be injected`, and the watcher loops poll `actions/runs/<id>` status every few seconds during earlier phases.
- **Root cause:** The smoke test infers downstream run existence indirectly from branch head state after injecting bait, but deterministic skip/merge timing can close the PR before the bait-triggered `pull_request synchronize` event fans out.
- **Exact change:**  
  1. Add an explicit parent dispatch token (`parent_run_id` or UUID) to the smoke test’s bait/trigger path.  
  2. Make `review_autofix` expose that token in a predictable output/artifact/comment.  
  3. Have Phase 4 watch for that token, not only `head_sha > PRE`.  
  4. Fail fast before the 30-minute watcher if the PR closes before triggerability is confirmed.
- **Estimated time savings:** **30–60 min** on failed smoke runs; also shortens successful smoke runs by reducing idle wait.
- **Implementation risk:** **Low-medium**. Backward-compatible if token matching is additive and falls back to current behavior during rollout.

### 2. Add a fast path for Claude-branch/comment-only `review_autofix` runs
- **Critical-path win**
- **Evidence:** `review_autofix` family p95 is **1,634.65s**. Slow runs **25207020260 (2,555s)**, **25201255563 (1,846s)**, and **25206256213 (1,230s)** still spent 20–40 minutes in review even when logs said `editor/commit/judge/auto-merge skipped`. Recent shorter runs **25208295519 (55s)** and **25208384364 (40s)** show the gate can already skip work deterministically for docs-only or small changes.
- **Root cause:** Claude-branch comment-only paths still execute a heavy reviewer panel even when no editing or merge action will occur.
- **Exact change:**  
  - Introduce a `CLAUDE_BRANCH_REVIEW_FAST_PATH` for comment-only runs: one fast reviewer + summariser, or skip the full panel when diff is under existing deterministic thresholds and no merge-affecting action is possible.  
  - Reuse the existing deterministic-skip gate machinery instead of a separate branch path.
- **Estimated time savings:** **15–25 min** per affected Claude-branch run.
- **Implementation risk:** **Medium**. Keep the current full panel as fallback behind a repo variable until precision is validated.

### 3. Cut implement retry-loop wall time after the first “exploration-only” failure
- **Critical-path win**
- **Evidence:** Failed implement run **25208345846** spent **427s** before bailing on `2 consecutive attempts with no actionable output`. Similar failures occurred in **25206967321 (504s)** and **25206976031 (641s)**. Logs show repeated Serena startup/activation across attempts in the same job.
- **Root cause:** The retry loop allows a second expensive attempt even after the first attempt has already produced “announced edit without changes” / empty-output behavior characteristic of a stuck exploration loop.
- **Exact change:**  
  - If attempt 1 produces no file changes **and** no concrete edit primitive (`apply_patch`, file write, diff) appears, switch immediately to a compact fallback prompt or route to clarify/diagnostics instead of re-running the same full implementation prompt.  
  - Persist and inspect a small “attempt capability summary” rather than replaying the full exploration recap into another xhigh run.
- **Estimated time savings:** **3–7 min** per stuck implement run.
- **Implementation risk:** **Low-medium**. Keep the current 2-attempt behavior only for cases with actual file-edit evidence.

### 4. Reduce full-history checkout in the poller
- **Local optimization**
- **Evidence:** Orchestrate poll run **25208298328 (47s)** logged `actions/checkout@v5` with `fetch-depth: 0`; the workflow file `.github/workflows/orchestrate_poll.yml` confirms full-history checkout in the poll job.
- **Root cause:** The poller pays for full-history checkout even though most poll cycles appear to perform status/orchestration work, not deep history analysis.
- **Exact change:** Change the default checkout in `orchestrate_poll` to shallow (`fetch-depth: 1` or a small bounded depth), and deepen only in the specific steps that actually require history walks.
- **Estimated time savings:** **5–15s** per poll run.
- **Implementation risk:** **Low**, provided targeted deepening remains in the rare history-dependent step.

### 5. Trim repeated Serena boot/activation inside single jobs
- **Local optimization**
- **Evidence:** Implement failure **25208345846** shows Serena starting/ready three times and `serena.activate_project(...)` three times in one run. Slow review run **25207020260** shows `serena.activate_project(...)` at least eight times across passes.
- **Root cause:** Serena is being reactivated per attempt/pass instead of reused as a job-scoped session where possible.
- **Exact change:** Keep one Serena activation per job workspace and reuse it across retries/passes unless the working directory actually changes.
- **Estimated time savings:** **10–60s** per long implement/review job, plus token savings from less repeated tool chatter.
- **Implementation risk:** **Low**, assuming session reuse is guarded by workspace identity.

---

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Lower reasoning level on comment-only / deterministic-review paths
- **Evidence:** `.github/workflows/review_autofix.yml` defaults `REVIEWER_REASONING_EFFORT` to **`xhigh`** and `XPOLL_SUMMARISER_MODEL` to **`openai/gpt-5.4-mini`**; logs from **25208384364** and **25207020260** show these settings on paths that often skip editor/judge and only post comments.
- **Root cause:** Premium reasoning is applied even when the workflow outcome cannot commit, merge, or modify code.
- **Exact change:**  
  - Use `medium` or `high` reasoning on comment-only Claude-branch review paths.  
  - Keep `xhigh` only for merge-blocking judge paths, conflict resolution, or multi-reviewer disagreement cases.
- **Estimated savings:** **20–50%** reviewer-model cost on comment-only runs.
- **Quality-risk notes:** Low if gated to non-editing paths; keep a one-click override to force full reasoning on risky diffs.

### 2. Stop paying for repeated implement retries after a no-op exploration signal
- **Evidence:** Log-summary evidence around failed implement **25208345846** reports attempts consuming **8,045**, **92,035**, and **10,025** tokens before aborting with “no actionable output.” Deep-dive logs confirm the bail reason.
- **Root cause:** The second and third prompts inherit large repeated context while the agent remains stuck in exploration.
- **Exact change:**  
  - After the first no-change/no-edit attempt, either:  
    1. route to clarify/diagnose, or  
    2. use a shortened “edit now or stop” fallback prompt with a tight token budget.  
  - Cap retry prompt expansion; do not restate the entire policy/context block on later attempts.
- **Estimated savings:** **50k–100k+ tokens per failed implement run**.
- **Quality-risk notes:** Very low if the aggressive cutoff only applies to empty-output / announced-edit-without-changes cases.

### 3. Reduce `workflow_log_analysis` summarization spend on unselected runs
- **Evidence:** Deep-dive telemetry in **25204185528** and **25206805901** shows `summarize_unselected_runs` using **304,169** and **186,487** tokens respectively to summarize **83/100** and **86/100** targeted runs.
- **Root cause:** The summarizer processes many unselected runs even when most are short skipped runs with nearly identical gating outcomes.
- **Exact change:**  
  - Collapse identical skipped-run templates before model summarization.  
  - Skip summarization for repeated 1-second gate-false runs once one canonical sample per workflow family is captured.  
  - Prefer deterministic summarization for single-line skipped patterns.
- **Estimated savings:** **35–60%** of unselected-run summarization tokens.
- **Quality-risk notes:** Low if at least one canonical example per gate pattern remains in the report.

### 4. Simplify reviewer-panel width on low-entropy diffs
- **Evidence:** Slow review run **25207020260** finished with `REVIEWERS_SUCCESSFUL: 6`, while recent docs/small-diff runs already deterministically skip much of the path. Multiple recent runs show only one-file, one- or two-line diffs.
- **Root cause:** The same reviewer breadth is used for trivial and complex diffs.
- **Exact change:** Scale reviewer count by diff size/risk: e.g., 1 reviewer for docs/single-file canary changes, 2–3 for normal diffs, full panel only for large or conflict-prone diffs.
- **Estimated savings:** **30–70%** reviewer token spend on trivial diffs.
- **Quality-risk notes:** Medium. Keep full-panel escalation when reviewers disagree or when protected files are touched.

### 5. Stabilize prompt prefixes to make prompt caching actually useful
- **Evidence:** Prompt cache is enabled, but slow review run **25207020260** reports all `OPENROUTER` usage fields as `na`, so cache benefits are unmeasurable. Failed implement retries also appear to prepend dynamic retry material repeatedly, which likely changes the prompt head on each attempt.
- **Root cause:** Dynamic noise near the start of prompts fragments cache keys and hides effectiveness.
- **Exact change:** Keep the first prompt segment stable and static; append volatile retry context, timestamps, and attempt recaps near the end.
- **Estimated savings:** **Unknown today** because cache telemetry is missing, but likely meaningful on repeated review/implement prompts.
- **Quality-risk notes:** Low. This is structure-only, not behavior-changing.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Repair the post-Codex recovery test expectations in CI
- **Failure evidence:** CI family failure rate is **12.5% (9/72)**. Seven failures between **08:26Z–08:38Z** on May 1, 2026 all failed at `lint / Implement post-Codex recovery unit tests` (runs **25208029193**, **25208058169**, **25208121455**, **25208128841**, **25208142230**, **25208295433**, **25208312323**, **25208317081**). Deep-dive run **25208295433** shows only 2 failing tests:  
  - `test_codex_empty_output_streak_bail_and_flag`  
  - `test_failure_diagnostics_posted_to_source_issue`
- **Root cause category:** Test/spec drift.
- **Exact fix:** Align tests and workflow diagnostics around the new empty-streak bail reason so both tests assert the current intended wording and branch-specific distinction.
- **Expected reliability impact:** Restore most of the CI family failure spike immediately.
- **Rollback/fail-open considerations:** Very safe; if behavior is correct, fix the tests. If behavior is not correct, keep the tests and revert the workflow message change explicitly.

### 2. Make smoke-gate review detection explicit and fail fast when bait cannot trigger
- **Failure evidence:** Run **25204168842** failed after **71 minutes** total workflow time with the downstream review run never appearing; the same log explicitly notes the PR may already be merged/closed before bait injection.
- **Root cause category:** Cross-workflow orchestration race / watcher identity ambiguity.
- **Exact fix:**  
  - Add explicit parent-child trigger identity.  
  - Refuse to enter the long review wait if bait injection or triggerability is not confirmed.  
  - Gate auto-merge/deterministic skip until smoke labels are visible, or exempt smoke-test PRs from that fast-merge path.
- **Expected reliability impact:** Major reduction in false smoke failures and reruns.
- **Rollback/fail-open considerations:** Fail-open by keeping current polling as a fallback if explicit token discovery fails.

### 3. Route exec-mode ambiguity to clarify immediately instead of retrying implement
- **Failure evidence:** Failed implement run **25208345846** logged both `Codex bailed: request_user_input rejected in exec mode (issue/plan ambiguity — route back to clarify)` and later `2 consecutive attempts with no actionable output`. Similar failure pattern appears in **25206967321** / **25206976031**.
- **Root cause category:** Orchestrator/agent mode mismatch.
- **Exact fix:** When the agent identifies a need for user input in exec mode, stop the implement loop immediately, post a deterministic clarify/diagnostic result, and mark the run as redirected rather than “implementation failure.”
- **Expected reliability impact:** Fewer false implement failures; lower rerun churn.
- **Rollback/fail-open considerations:** Low risk if the current retry loop remains as a fallback only when a real edit attempt occurred.

### 4. Add a missing `processed-command-complete` emission for stronger idempotency auditability
- **Failure evidence:** Sampled memory telemetry includes **6** `processed-command-check` and **6** `processed-command-claim` events but **0** `processed-command-complete` events.
- **Root cause category:** Observability gap / partial idempotency ledger.
- **Exact fix:** Emit completion telemetry once command handling finishes, even on fail-open paths.
- **Expected reliability impact:** Medium; reduces duplicate-command ambiguity and makes replay bugs easier to prove.
- **Rollback/fail-open considerations:** Safe, telemetry-only.

### 5. Remove runner-queue time from success/failure diagnosis dashboards
- **Failure evidence:** Many successful and cancelled runs include `Job is waiting for a hosted runner to come online`, including **25208681716**, **25208668798**, **25208298328**, **25208401210**, and many CI/review runs.
- **Root cause category:** Operational visibility noise.
- **Exact fix:** Split runner-queue delay from in-job execution time in telemetry dashboards and alerting thresholds.
- **Expected reliability impact:** Medium for operations; prevents chasing infra noise as workflow regressions.
- **Rollback/fail-open considerations:** No behavior risk.

---

## AI Memory Health

- **Observed telemetry volume:** **57** structured `AI_MEMORY_TELEMETRY` JSON events across sampled deep-dive `errors/`, `slow/`, and `recent/` logs.
- **Operation mix:**  
  - `record-run-event`: **25**  
  - `retrieve`: **12**  
  - `processed-command-check`: **6**  
  - `processed-command-claim`: **6**  
  - `record-candidate`: **6**  
  - `summarize_unselected_runs`: **2**

### Retrieval effectiveness
- **Retrieve hit rate:** **50%** (**6/12** had `records_selected > 0`)
- **Average `estimated_tokens`:** **14.0**
- **Keyword method distribution:**  
  - `plain`: **6**  
  - `none`: **6**  
  - `llm`: **0 observed**

### What worked
- Implement runs **25206967321**, **25206976031**, and **25208345846** all showed successful memory retrievals for implementation with `records_selected: 1`, `keyword_method: "plain"`, `estimated_tokens: 28`.
- Deep-dive review run **25207020260** successfully recorded `phase_started`, `record-candidate`, and `phase_completed` events.
- Memory maintenance appears healthy in sampled report evidence: run **25205873529** compacted **2,914** archived candidates with `did_push: true` and `push_attempts: 1`.

### Flags
- **Reviewer retrievals are weak.** In slow review run **25207020260**, reviewer retrieval returned `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: "none"`.
- **Zero-record retrievals:** **6/12** retrieves returned nothing.
- **No `fail_open: true` JSON entries observed** in sampled telemetry.
- **No `enabled: false` JSON entries observed** in sampled telemetry.
- **Push retry counts are mostly healthy**, but one implement start event in **25206967321** needed `push_attempts: 2`, so write-path retries do occur.
- **Idempotency observability is incomplete:** `processed-command-complete` was not observed in sampled JSON telemetry.

### Recommendation
Prioritize reviewer-side retrieval quality before adding more memory volume:
1. derive plain keywords from PR title/files/linked issue body for reviewer role,
2. emit `processed-command-complete`,
3. track per-role retrieval hit rate as a first-class metric.

---

## GH API Call Audit

### 1. Watcher polling in smoke/release flows is the highest-volume GH API pattern
- **Evidence:**  
  - Failed smoke run **25204168842** repeatedly polls `actions/runs/<id>` status during clarify/plan waits and eventually fails after a 30-minute review wait.  
  - Deep-audit evidence in slow workflow-log-analysis run **25206805901** calls out watcher polling as “**thousands per smoke run**” and repeated `actions/runs/<id>` status polling over 30+ minutes.
- **Pattern:** Unbatched status polling in loops.
- **Recommendation:**  
  - Poll quickly only until child-run discovery, then switch to adaptive backoff.  
  - Cache run metadata per loop iteration and avoid re-reading unchanged state.  
  - Prefer child artifact/output heartbeat over raw status polling.
- **Estimated call-count reduction:** **Order-of-magnitude** on smoke/release flows.
- **Rate-limit risk reduction:** **High**.

### 2. `issue_pr_status` still shows N+1 linked-issue lookups
- **Evidence:** Recent runs **25208681714** and **25208401210** show a `gh api graphql` linked-issue fetch plus later issue-label/body handling; deep-audit evidence from **25206805901** explicitly identifies `issue_pr_status` as re-fetching linked issue metadata one-by-one after GraphQL lookup.
- **Pattern:** One batched GraphQL read followed by per-issue REST reads in the same workflow.
- **Recommendation:** Extend the existing GraphQL query to carry body/labels needed for downstream orchestration classification; persist that data instead of re-fetching each issue.
- **Estimated call-count reduction:** From **1 + N** reads to **1** on the common path.
- **Rate-limit risk reduction:** **Medium**.

### 3. `review_autofix` PR metadata collection is broader and more repetitive than necessary
- **Evidence:** Deep-audit analysis in **25206805901** highlights that the workflow collects PR metadata with multiple logical reads (PR, comments, reviews, review comments, linked issues) even though the repo already has a GraphQL-first helper in `scripts/gh_helpers.sh`.
- **Pattern:** Reimplemented multi-call PR metadata assembly.
- **Recommendation:** Reuse/extend the existing shared helper and collapse the current 5-read path into **1–2** batched calls.
- **Estimated call-count reduction:** **~60–80%** for that step.
- **Rate-limit risk reduction:** **Medium**.

### 4. `cancel_on_pr_close` does extra rate-limit probing for low-volume work
- **Evidence:** Recent runs **25208681716**, **25208401214**, and **25208317269** all log `_rl_wait()` using `gh api -i /rate_limit`, even when no matching runs exist and the job exits quickly.
- **Pattern:** Defensive rate-limit check on a low-volume path.
- **Recommendation:** Keep the wrapper, but skip `/rate_limit` probing unless a retry is actually needed or more than one cancellation target exists.
- **Estimated call-count reduction:** Small per run, but easy and safe.
- **Rate-limit risk reduction:** Low; mostly a cleanup win.

### 5. Repository API hygiene rules are good; enforcement should move from docs to metrics
- **Evidence:** Deep-audit logs in **25206805901** repeatedly restate repo rules: mandatory batching, cycle-local caches (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, `_candidate_details_json`), and fail-open resolver behavior.
- **Recommendation:** Track three repo-local counters per run family:  
  1. per-iteration `gh api` inside loops,  
  2. batched GraphQL helper reuse count,  
  3. fail-open GH API fallbacks triggered.
- **Estimated call-count reduction:** Indirect but durable.
- **Rate-limit risk reduction:** Medium.

---

## MCP & Serena Efficiency

### What the logs show
- **Good:** No broad raw file-read spam was visible in sampled deep dives; Serena usage mostly appears as targeted activation and pattern search.
- **Bad:** Serena startup/activation is repeated unnecessarily.
  - Failed implement **25208345846**: Serena started and `activate_project` ran **3 times**.
  - Slow review **25207020260**: `activate_project` ran at least **8 times**.
- **Low-value tool churn:** In review/implement examples, Serena is sometimes used to search a single canary file repeatedly (`tests/e2e_smoke_canary.txt`) rather than reusing prior results.

### Recommendations

1. **Reuse one Serena session per job**
   - **Evidence:** repeated `mcp: serena starting`, `mcp: serena ready`, and `serena.activate_project(...)`.
   - **Change:** activate once per workspace and reuse across retries/passes.
   - **Impact:** lower turnaround time and less tool overhead.
   - **Risk:** low.

2. **Skip Serena entirely on trivial single-file checks**
   - **Evidence:** implement/review logs use Serena to inspect the smoke canary file only.
   - **Change:** if the task is a known single-file existence/content check, use the already-produced local diff/context artifact instead of booting Serena.
   - **Impact:** seconds saved per run; fewer model/tool round trips.
   - **Risk:** low.

3. **Persist symbol/search results across retries**
   - **Evidence:** failed implement retries re-enter the same project and repeat early discovery steps before bailing.
   - **Change:** cache Serena outputs per attempt group (symbols overview, matched patterns, changed-file map) in temp files and reuse on retry.
   - **Impact:** better token efficiency on stuck runs.
   - **Risk:** low-medium.

4. **Increase safe parallel reads before reviewer/editor execution**
   - **Evidence:** review runs already assemble many independent context files (`PR_DIFF`, `LINKED_ISSUE_CONTEXT`, `MEMORY_CONTEXT`, symbol summaries).
   - **Change:** fetch PR diff, linked issue context, memory retrieve, and symbol diff in parallel before the first reviewer prompt.
   - **Impact:** modest latency win on long review runs.
   - **Risk:** low if outputs remain read-only.

5. **Measure “tool churn per successful edit”**
   - **Evidence:** slow review **25207020260** ended with `DID_COMMIT: false`, `EDITOR_SUMMARY_POSTED: true`, `EDITOR_NOOP_SUSPICIOUS: true` after substantial work.
   - **Change:** add a simple metric: Serena activations + searches per run, divided by files actually changed/committed.
   - **Impact:** makes low-yield tool usage visible.
   - **Risk:** none.

---

## Prompt Cache & Memory System

### Prompt cache behavior
- **Enabled but not measurable.** In slow review run **25207020260**, cache-related usage lines exist but all key numeric fields are `na`: `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.
- **Inference:** Prompt cache may be on, but current telemetry is insufficient to verify hit/miss rates or cost benefit.
- **Observed cache wins elsewhere:** `setup-uv` cache restored successfully in plan runs **25208262958** and **25208263353**, showing general workflow caching is healthy even if prompt cache metrics are not.

### Likely cache-fragmentation causes
- Repeated retry prompts in implement appear to prepend dynamic attempt-specific context early in the prompt.
- Review/comment-only paths still include large stable policy blocks plus dynamic branch/run metadata, likely varying too early in the prompt for cache reuse.
- Long in-workflow `run:` blocks make it harder to keep prompt assembly stable across families.

### Recommendations

1. **Emit numeric prompt/cache usage on every AI call**
   - **Evidence:** current `na` values make the prompt cache unauditable.
   - **Impact:** unlocks real cache optimization; improves cost attribution.
   - **Risk:** low.

2. **Stabilize prompt prefixes**
   - **Evidence:** retry paths likely vary prompt heads across attempts.
   - **Change:** move volatile sections (attempt recap, dynamic diagnostics, timestamps) to the end of prompts.
   - **Impact:** likely improves cache read rate and lowers latency.
   - **Risk:** low.

3. **Deduplicate repeated policy/instruction blocks across retries**
   - **Evidence:** failed implement and review logs repeatedly print the same long instruction material.
   - **Change:** reference a shared static prompt body and append only delta context.
   - **Impact:** token savings on retries and multi-pass review.
   - **Risk:** low.

4. **Improve reviewer memory retrieval inputs**
   - **Evidence:** reviewer retrievals in deep-dive review **25207020260** returned zero records with `keyword_method: "none"`.
   - **Change:** seed reviewer retrieval with PR title, changed paths, linked issue title/body, and branch role.
   - **Impact:** better memory hit rate and potentially shorter reviewer prompts.
   - **Risk:** low.

5. **Track cache effectiveness per workflow family**
   - **Evidence:** cache visibility differs sharply between workflow caches (`uv`, Codex binary) and prompt cache.
   - **Change:** add family-level metrics: prompt cache read hit %, average cached input tokens, and cache-disabled/fallback count.
   - **Impact:** medium cost and latency benefit over time.
   - **Risk:** none.

---

## Orchestrator Health

### Observed health signals
- The system is **heavily gate-driven**, with many ultra-short skipped runs:
  - `clarify`: **193 total**, **172** “other/skipped”
  - `plan`: **168 total**, **151** “other/skipped”
  - `orchestrate_clarify_respond`: **169 total**, **165** “other/skipped”
  - `implement`: **170 total**, **139** “other/skipped”
- Recent skipped runs consistently show comment-body guards failing on non-command comments or automation comments, e.g. `/answer` and `/approved` checks evaluating false.
- Plan runs **25208262958** and **25208263353** show `PROCESSED_ANSWER_CLAIMED: false` / `PROCESSED_ANSWER_COMPLETED: false`, which is useful but should be trended.

### Pain points
1. **Too many triggered-but-skipped runs** create queue noise and operational confusion.
2. **Mode mismatches** still occur: implement can reach exec mode and then discover it needs clarification.
3. **Long child workflows lack compact machine-readable phase outputs**, so parent watchers resort to polling and heuristics.

### Smallest safe mitigations
- Tighten upstream event filters so plan/clarify/respond do not start for obvious non-command comments.
- Publish compact phase outputs/artifacts on every phase transition (`phase`, `status`, `last_progress_ts`, `reason`).
- Add a separate terminal status for “redirected to clarify” so implement failures don’t inflate failure metrics.

### Indicators to track
- **Skipped-trigger ratio** per workflow family
- **Implement redirected-to-clarify count**
- **Processed-command claim-to-complete ratio**
- **Parent watcher timeout count**
- **Child workflow discovery latency** from trigger to first observable run/output

---

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Review/autofix compute dominates the pipeline
- **Evidence:** `review_autofix` p95 **1,634.6s**; deep-dive runs up to **2,555s**.
- **Type:** Compute + model/tool overhead
- **Fix:** Fast path comment-only/Claude-branch runs; smaller reviewer panel; lower reasoning on non-editing paths.

### 2. Smoke/release watcher loops add extreme queue/retry overhead
- **Evidence:** `test_and_mark_stable` average **3,873s**, p50 **4,390.5s**, failure rate **50%** in the sample; run **25204168842** failed after a 30-minute wait for a child run that never appeared.
- **Type:** Retry/poll overhead
- **Fix:** Explicit trigger identity, adaptive polling, early triggerability checks.

### 3. Implement loops waste compute on stuck exploration
- **Evidence:** failed implement runs **25208345846**, **25206967321**, **25206976031**; no-action retries and repeated Serena activation.
- **Type:** Compute + token waste
- **Fix:** early stop after no-op exploration, redirect to clarify, shorter retry prompts.

### 4. CI is a stable 10-minute floor with localized failure spikes
- **Evidence:** CI p50 **609.5s**, p95 **643s**; many successful runs cluster around 10 minutes, while failures are concentrated in one test step.
- **Type:** Compute bottleneck
- **Fix:** restore failing tests first; then consider splitting the newest recovery-test block into a separate fast-fail job if queue visibility matters.

### 5. Queueing is visible across many short workflows
- **Evidence:** multiple recent runs log `Job is waiting for a hosted runner to come online`, including short admin/status jobs.
- **Type:** Queueing overhead
- **Fix:** separate queue time from execution metrics; don’t optimize workflow logic based on queue inflation.

### 6. Clarify/plan/respond generate lots of micro-runs with little productive work
- **Evidence:** most recent runs in these families are 1–2 seconds and skipped by conditions.
- **Type:** Trigger/gating overhead
- **Fix:** tighten event predicates before workflow start, not inside the job.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail runtime: p95 **1,634.6s**, deep-dive max **2,555s**
- `test_and_mark_stable` watcher/coordination overhead: average **3,873s**, failure rate **50%**
- CI baseline runtime around **10 minutes** even when healthy

**Top failure modes**
- Post-Codex recovery test expectation drift in CI
- Implement retries stuck in exploration / no-action output
- Smoke-gate review detection race when PR closes before bait-triggered downstream review appears

**Highest-cost drivers**
- Full reviewer-panel execution on comment-only Claude-branch paths
- Implement retries after empty/noop attempts
- Unselected-run summarization in workflow-log analysis (**304,169** and **186,487** tokens in sampled runs)

**Top 3 prioritized actions**
1. **Add fast path for Claude-branch/comment-only `review_autofix`** and reduce reviewer reasoning there.
2. **Fix smoke-gate child-run identity/trigger sequencing** to eliminate 30-minute watcher misses.
3. **Short-circuit implement after first no-action exploration attempt** and route ambiguity to clarify instead of retrying.

---

## Metrics Appendix

### Overall sampled run window

| Metric | Value |
|---|---:|
| Total runs | 1,000 |
| Success count | 293 |
| Failure count | 15 |
| Cancelled count | 64 |
| Other/skipped count | 628 |
| Overall avg duration | 127.8s |
| Overall p50 duration | 1.0s |
| Overall p95 duration | 617.0s |

### Key workflow family metrics

| Workflow family | Runs | Failure rate | Avg duration | p50 | p95 |
|---|---:|---:|---:|---:|---:|
| `review_autofix` | 102 | 0.0% | 325.0s | 28.5s | 1,634.6s |
| `ci` | 72 | 12.5% | 603.3s | 609.5s | 643.0s |
| `implement` | 170 | 1.8% | 38.4s | 1.0s | 240.3s |
| `test_and_mark_stable` | 4 | 50.0% | 3,873.0s | 4,390.5s | 5,231.9s |
| `workflow_log_analysis` | 3 | 0.0% | 4,531.3s | 4,469.0s | 5,184.5s |
| `orchestrate_poll` | 29 | 0.0% | 58.2s | 46.0s | 56.2s |
| `plan` | 168 | 0.0% | 18.6s | 1.0s | 170.7s |
| `clarify` | 193 | 0.0% | 15.0s | 1.0s | 112.2s |

### Representative slow/failing runs

| Run ID | Family | Conclusion | Duration | Key issue |
|---|---|---|---:|---|
| 25204168842 | `test_and_mark_stable` | failure | 4,281s | Waited 30m for `review_autofix` run that never appeared |
| 25200104592 | `test_and_mark_stable` | failure | 5,361s | Failed in `Dispatch & watch — workflow-log-analysis` |
| 25207020260 | `review_autofix` | success | 2,555s | Long reviewer-panel execution |
| 25208345846 | `implement` | failure | 427s | Bailed after 2 no-action attempts |
| 25206976031 | `implement` | failure | 641s | Same stuck-implement pattern |
| 25208295433 | `ci` | failure | 594s | 2 failing recovery tests in `lint` job |

### Observed token usage from sampled evidence

| Context | Runs | Observed tokens |
|---|---|---:|
| Failed implement retries for run 25208345846 (quoted in downstream log summary) | 1 run | **110,105** |
| `workflow_log_analysis` unselected-run summarization | 25204185528 | **304,169** |
| `workflow_log_analysis` unselected-run summarization | 25206805901 | **186,487** |
| **Observed subtotal** | 3 sampled cases | **600,761** |

> Note: global token totals for the full 1,000-run window were not present in the provided aggregate context, so the table above is limited to explicit token figures observed in deep-dive/log-summary evidence.

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Structured telemetry events observed | 57 |
| Retrieve operations | 12 |
| Retrieve hit rate | 50% |
| Avg retrieve estimated tokens | 14.0 |
| `keyword_method=plain` | 6 |
| `keyword_method=none` | 6 |
| `keyword_method=llm` | 0 |
| `fail_open: true` retrieves | 0 observed |
| `enabled: false` retrieves | 0 observed |
| Highest observed push retry count | 2 |

### Cache metrics observed

| Cache surface | Evidence | Status |
|---|---|---|
| `setup-uv` cache | plan runs 25208262958 / 25208263353 | Healthy cache hits |
| Codex binary cache | workflow-log-analysis deep dive | Healthy cache hit (`codex-v0.114.0`) |
| Prompt cache enabled flag | multiple review runs | Enabled |
| Prompt/cache numeric usage | slow review 25207020260 | **Unavailable (`na`)** |
| Cache observability quality | sampled prompt-cache telemetry | **Insufficient** |

### GH API call summary

| Pattern | Evidence runs | Observed shape | Est. reduction opportunity |
|---|---|---|---|
| Child-run status polling | 25204168842, 25206805901 | repeated `actions/runs/<id>` polling in watchers | Very high |
| Linked-issue GraphQL + per-issue REST | 25208681714, 25208401210, 25206805901 | 1 batched query followed by N re-fetches | Medium-high |
| Multi-read PR metadata assembly in review | 25206805901 | separate PR/comments/reviews/review-comments calls | Medium |
| `/rate_limit` probing in cancel flow | 25208681716, 25208401214, 25208317269 | defensive call even with no cancellation work | Low |

If you want, I can turn this into a shorter exec-ready action list with owners, priorities, and a one-week rollout plan.
