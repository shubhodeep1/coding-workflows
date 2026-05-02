## Executive Summary

- **The highest-impact fix is already strongly indicated by the logs: lower implement reasoning for E2E smoke-canary tasks.** Two failed implement runs, **25243564804** and **25243569299**, used `MODEL_EDITOR=openai/gpt-5.3-codex` with `MODEL_REASONING_EFFORT=xhigh`, then bailed after **2 consecutive no-actionable-output attempts**. The merged PR text captured in run **25244043516** shows one smoke run burned **23,331 + 21,294 = 44,625 tokens** without making the required 3-line edit. **Estimated impact:** materially improves `test_and_mark_stable` pass rate and saves tens of thousands of tokens per failed smoke cycle. **Confidence:** high.

- **`test_and_mark_stable` is the dominant end-to-end bottleneck and reliability problem.** Family stats show **5 total runs, 4 failures, 0 successes, 80% failure rate**, with **p50 4,992s** and **p95 5,580s**. Failures cluster at `e2e-smoke-test / Phase 4b: Verify editor removed bait line` and `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`. **Estimated impact:** 45–90 min faster release validation per failed cycle once the waiting/polling path and smoke-model behavior are fixed. **Confidence:** high.

- **GH API usage is heavily concentrated in polling-heavy workflows.** In the sampled deep-dive logs, `review_autofix` runs **25215784558** and **25237552686** each logged **259 `gh api` invocations**; multiple implement failures logged **87** each; failed `test_and_mark_stable` runs also logged **66–87** each. **Estimated impact:** 40–70% reduction in API calls for these paths, with lower rate-limit exposure and modest latency gains. **Confidence:** high.

- **Review/autofix spends substantial time on queue/setup even when doing comment-only work.** The family has **p50 41s** but **p95 1,759s**; deep-dive runs **25215784558** and **25237552686** took **3,032s** and **2,938s**. Recent cancelled runs **25244025745** and **25244032300** still initialized Codex review jobs, used `xhigh` reviewer/editor settings, and emitted `memory helper script missing` warnings despite ending on comment-only/claude-branch review paths. **Estimated impact:** 5–40 min saved on affected review flows. **Confidence:** medium-high.

- **AI memory retrieval is mostly healthy when emitted, but coverage is spotty and some runs degrade silently.** Across unique telemetry events in sampled deep-dive logs, `retrieve` had a **72.7% hit rate**, **33.1 average estimated tokens**, and `keyword_method` was **plain`=8`, `none`=3`**; three retrieves returned zero records (**25237552686, 25215784558, 25212191835**). No `enabled:false` or `fail_open:true` entries were observed, but `memory helper script missing` appeared **21 times** in sampled logs, especially in `review_autofix`. **Estimated impact:** better post-failure learning and less operational blind spot. **Confidence:** medium.

- **Prompt-cache evidence is incomplete, but prompt variance and repeated instruction expansion are likely reducing reuse.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present in sampled orchestration/review logs, yet no prompt-cache create/read counters were emitted. At the same time, failed implement loops repeatedly carried large static instruction blocks before trivial edits. **Estimated impact:** token savings are likely meaningful but not yet measurable from this window. **Confidence:** medium-low.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Make E2E smoke-canary implement runs use low reasoning effort**
   - **Evidence:** Failed implement runs **25243564804** and **25243569299** both ran with `MODEL_REASONING_EFFORT: xhigh`. The merged PR description visible in **25244043516** states run **25243564804** consumed **23,331** and **21,294** tokens across two attempts and still never executed the edit; both failures were on the smoke-canary task.
   - **Root cause:** Over-reasoning on a trivial 3-line overwrite increases exploratory behavior and no-op retries.
   - **Exact change:** In `implement.yml`, override `MODEL_REASONING_EFFORT=low` when the issue title matches the existing smoke-test marker (`[E2E Smoke Test]`) before Codex config generation.
   - **Estimated time savings:** **10–20 minutes per failed smoke-canary branch** directly; **45–90 minutes end-to-end** when it unblocks `test_and_mark_stable`.
   - **Implementation risk:** **Low.** The PR text already proposes this exact change and limits it to smoke runs.

2. **Fail `test_and_mark_stable` earlier once the smoke edit is known to be impossible or stuck**
   - **Evidence:** `test_and_mark_stable` has **80% failure rate**, **p50 4,992s**, **p95 5,580s**. Failures occur late at `Phase 4b: Verify editor removed bait line` or after waiting for clarify→plan→implement in alt-model runs (**25212177682**, **25215477856**, **25223836137**, **25237291900**).
   - **Root cause:** Long poll/wait phases continue after the downstream implement loop has already entered a known bad state.
   - **Exact change:** Propagate implement terminal reasons (`codex_stuck_in_exploration.flag`, request-user-input rejection, or explicit “no actionable output” diagnostics) into the E2E waiter and abort immediately rather than continuing full phase polling.
   - **Estimated time savings:** **30–70 minutes** per failing release-validation run.
   - **Implementation risk:** **Low-medium.** Requires plumbing existing diagnostics into the waiter, not changing task semantics.

3. **Collapse polling in E2E waiters from broad scans to run-ID-based tracking**
   - **Evidence:** In **25212177682** `e2e-smoke-test`, the waiter calls endpoints like `actions/runs?per_page=50&created=>...`, repeatedly polls labels/comments, and checks workflow status in loops. Sampled failed `test_and_mark_stable` runs logged **66–87 `gh api`** calls each.
   - **Root cause:** Poll loops repeatedly enumerate recent runs instead of following the exact dispatched run IDs.
   - **Exact change:** Record dispatched workflow run IDs once, then poll `/actions/runs/{id}` directly; cache issue labels/comment snapshots per loop iteration; avoid repeated `per_page=50` scans.
   - **Estimated time savings:** **2–8 minutes** per long E2E run, plus API-rate headroom.
   - **Implementation risk:** **Low.**

4. **Skip full review job setup on comment-only / claude-branch review paths**
   - **Evidence:** Recent review runs **25244025745** and **25244032300** were cancelled after **41s** and **59s** but still initialized the Codex review job, logged `MODEL_EDITOR=openai/gpt-5.3-codex`, kept reviewer/editor reasoning at `xhigh`, and hit `memory helper script missing`. Slow review runs **25215784558** and **25237552686** exceeded **49 minutes** total.
   - **Root cause:** Heavy setup path is reused even when editor/commit/judge/merge are already known to be skipped.
   - **Exact change:** Move the comment-only / claude-branch gate ahead of Codex/Serena/bootstrap steps; short-circuit before tool setup when only reviewer-panel/comment behavior is needed.
   - **Estimated time savings:** **20–60s** on cancelled comment-only runs, **5–15 minutes** on long comment-only review jobs depending on queueing.
   - **Implementation risk:** **Low-medium.** Needs careful preservation of existing comments/status side effects.

5. **Remove duplicate support/bootstrap work from short orchestration paths**
   - **Evidence:** Successful clarify/plan runs around **26–80s** still show setup/cache/bootstrap dominating visible runtime; e.g. **25243569180** spent notable time in `Resolve workflow support ref`, while many follow-on clarify/plan/respond runs are skipped in **0–2s**.
   - **Root cause:** Light paths still pay checkout/bootstrap overhead.
   - **Exact change:** Push gating conditions earlier in reusable workflows so skipped clarify/plan/respond runs avoid repository/support checkout entirely.
   - **Estimated time savings:** **10–30s** on non-running paths; small per run, meaningful at volume given hundreds of skipped runs.
   - **Implementation risk:** **Low.**
   - **Critical-path note:** This is a micro-optimization; the first four items matter more.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Lower smoke-canary reasoning from `xhigh` to `low`**
   - **Evidence:** Run **25244043516** preserved exact prior-run evidence: **25243564804** spent **44,625 tokens** across two failed attempts; **25243569299** also failed after two no-op attempts. The task was only a 3-line overwrite.
   - **Root cause:** Model selection/reasoning level is mismatched to task complexity.
   - **Exact change:** Scope `MODEL_REASONING_EFFORT=low` to `[E2E Smoke Test]` issues only.
   - **Estimated savings:** **20k–45k tokens per failed smoke run**; much larger when it prevents the downstream `test_and_mark_stable` rerun.
   - **Quality-risk notes:** Very low quality risk for smoke canaries; keep `xhigh` for normal implement traffic.

2. **Add a “must-edit-by-attempt-1” guard for tiny-file tasks**
   - **Evidence:** PR summary in **25244043516** shows one failure produced only `serena.activate_project`, and another produced read-only `serena.search_for_pattern` calls with no write. The implement loop then aborted after two empty/no-change attempts.
   - **Root cause:** Repeated prompt/context expansion is being spent on exploration even when the task is mechanically tiny.
   - **Exact change:** For tasks matching “single-file overwrite / <N lines changed / exact file target known,” abort after the first no-change attempt and downgrade reasoning or reissue with a minimal prompt instead of re-running the full context.
   - **Estimated savings:** **30–60% token reduction** on trivial-task failures.
   - **Quality-risk notes:** Moderate if over-applied; keep scoped to smoke/selftest/single-file fixtures.

3. **Reduce repeated static instruction payload in implement retries**
   - **Evidence:** Failed implement logs contain large repeated instruction blocks; sampled summaries show retries with no new information still burning thousands to tens of thousands of tokens. Deep-dive summaries reference prior runs with **4,502 + 4,290**, **6,430 + 5,115**, and even **5,660 + 87,826** tokens before bailing.
   - **Root cause:** Retry attempts appear to resend large, mostly static context rather than a compressed retry delta.
   - **Exact change:** Freeze a stable base prompt and pass only retry-specific deltas on subsequent attempts; move volatile diagnostics and previous-attempt notes to a compact suffix.
   - **Estimated savings:** **10–35%** on retrying implement failures.
   - **Quality-risk notes:** Low if the base prompt is unchanged and only redundant retry text is removed.

4. **Use lighter review settings on comment-only review paths**
   - **Evidence:** Recent cancelled review runs **25244025745** and **25244032300** still set `REVIEWER_REASONING_EFFORT: xhigh` and `EDITOR_REASONING_EFFORT: xhigh` despite “editor/commit/judge/auto-merge skipped.”
   - **Root cause:** High-cost reasoning profile is applied before the path is known to be non-editing.
   - **Exact change:** After gate resolution, downgrade reviewer/editor reasoning or skip editor configuration entirely for comment-only flows.
   - **Estimated savings:** modest per run, but recurring across branch-review churn; likely **10–25%** on those paths.
   - **Quality-risk notes:** Low if restricted to comment-only paths.

5. **Avoid avoidable reruns by making selftest/smoke failures more diagnostic on first pass**
   - **Evidence:** `nightly_validation_selftest` **25242537588** failed with `fixtures=3 passed=1 failed=2`, then uploaded artifacts; `test_and_mark_stable` repeatedly fails on the same late phases.
   - **Root cause:** Reruns are being used to discover deterministic fixture/model problems.
   - **Exact change:** Promote fixture-level failure summaries and implement diagnostic comments into the deciding job so operators do not re-run for basic triage.
   - **Estimated savings:** indirect but high when it prevents repeated 80–90 minute release validations.
   - **Quality-risk notes:** None; purely diagnostic.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Land and verify the smoke-run reasoning override**
   - **Failure evidence:** `implement` failures **25243564804** and **25243569299** both ended in `Run Codex implementation`; downstream `test_and_mark_stable` runs failed at smoke and alt-model waits.
   - **Root cause category:** Model/task mismatch.
   - **Exact fix:** Apply `MODEL_REASONING_EFFORT=low` for smoke-canary issues and verify in a fresh `test-and-mark-stable` run that attempt 1 edits `tests/e2e_smoke_canary.txt`.
   - **Expected reliability impact:** Highest single reliability win in this sample; likely turns the current **0/5** `test_and_mark_stable` success rate into at least a debuggable baseline.
   - **Rollback / fail-open:** Safe rollback by restoring default `xhigh` if smoke quality regresses.

2. **Treat “announced edit without actual change” as a first-class terminal diagnosis**
   - **Failure evidence:** Implement loops explicitly abort on `empty-output and/or announced-edit-without-changes`; PR body in **25244043516** documents repeated “I’m applying via apply_patch” responses with no tool call.
   - **Root cause category:** Agent loop / tool-execution failure.
   - **Exact fix:** Preserve and surface this reason earlier to orchestrator and E2E waiters; route directly back to clarify or a narrowed retry profile instead of generic implement failure.
   - **Expected reliability impact:** Reduces duplicate failed implement reruns and makes operator recovery deterministic.
   - **Rollback / fail-open:** Fail-open by falling back to current implement-failed labeling if the reason cannot be classified.

3. **Fix `memory helper script missing` on review/autofix paths**
   - **Failure evidence:** Sampled deep-dive logs show **21** occurrences of `memory helper script missing`; recent review runs **25244025745** and **25244032300** skipped run-end failure event recording for that reason.
   - **Root cause category:** Missing dependency / observability gap.
   - **Exact fix:** Ensure the helper script is fetched or vendored consistently before review memory hooks run, or guard the hook behind a single earlier existence check rather than late warnings.
   - **Expected reliability impact:** Medium. Improves failure recording, debugging, and memory lineage consistency rather than direct job success.
   - **Rollback / fail-open:** Existing behavior already fail-opens; safe to stage.

4. **Stabilize `nightly_validation_selftest` by promoting fixture-level output into the failing summary**
   - **Failure evidence:** Run **25242537588** failed with `fixtures=3 passed=1 failed=2`.
   - **Root cause category:** Validation fixture regression.
   - **Exact fix:** Emit fixture names and first-failure reason into the primary job summary and annotate PR/issue comments for nightly failure.
   - **Expected reliability impact:** Medium; reduces mean-time-to-fix and rerun churn.
   - **Rollback / fail-open:** Safe; reporting-only.

5. **Add bounded retries to GitHub-script prepare/list-files calls**
   - **Failure evidence:** In **25244033334**, `actions/github-script@v8` “Prepare” uses `retries: 0` while it performs `pulls.get` and paginated `pulls.listFiles`.
   - **Root cause category:** No transient-failure protection.
   - **Exact fix:** Enable small bounded retries for 5xx/network classes while keeping 4xx exempt.
   - **Expected reliability impact:** Low-medium; protects review preparation from transient API flakiness.
   - **Rollback / fail-open:** Safe; keep retry count small.

## AI Memory Health

- **Observed telemetry coverage:** `AI_MEMORY_TELEMETRY` JSON lines were present in sampled deep-dive logs for `implement`, `review_autofix`, and `workflow_log_analysis`, but **not consistently present across many recent sampled runs**. Memory telemetry was therefore only partially observable in this collection window.

- **Retrieve health (unique telemetry events only):**
  - `retrieve` count: **11**
  - Hit rate (`records_selected > 0`): **72.7%**
  - Average `estimated_tokens`: **33.1**
  - `keyword_method` distribution:
    - `plain`: **8**
    - `none`: **3**
    - `llm`: **0 observed**
  - Zero-record retrieves: **3**
    - run **25237552686** (`review_autofix`)
    - run **25215784558** (`review_autofix`)
    - run **25212191835** (`workflow_log_analysis`)

- **Other observed operations:**
  - `record-run-event`: **25**
  - `processed-command-check`: **9**
  - `processed-command-claim`: **9**
  - `record-candidate`: **2**
  - `summarize_unselected_runs`: **4**

- **Healthy signals:**
  - No observed `enabled: false` telemetry entries.
  - No observed `fail_open: true` telemetry entries.
  - Most `record-run-event` pushes succeeded on **1 attempt**.

- **Flags:**
  - `phase_started` push attempts reached **2** in runs **25215763575** and **25243569299**. That is not severe, but it is worth watching.
  - `memory helper script missing` appeared **21 times** in sampled logs, overwhelmingly on `review_autofix`; this weakens lineage and failure recording even when jobs otherwise continue.

- **Recommendation:** keep telemetry emission mandatory on sampled failure/slow paths and add a simple sampled-run audit that fails soft if memory hooks are missing on workflows expected to emit them.

## GH API Call Audit

### High-volume patterns

1. **`review_autofix` polling and PR-state lookups**
   - **Evidence:** Runs **25215784558** and **25237552686** each logged **259 `gh api` invocations** in sampled deep dives.
   - **Pattern:** repeated PR state checks, branch/merge gate lookups, and post-merge dispatch logic inside the same run.
   - **Recommendation:** cache PR metadata, labels, and merge state once per phase; pass outputs between jobs instead of re-fetching.
   - **Estimated reduction:** **30–50%** fewer API calls on long review runs.

2. **`implement` repeated issue/workflow polling**
   - **Evidence:** Failed implement runs **25215763575**, **25224008847**, **25224028373**, **25237418726**, **25237690797**, **25237704374** each logged about **87 `gh api`** calls; recent failures **25243564804** and **25243569299** still logged about **30** each.
   - **Pattern:** repeated issue metadata fetches, status checks, and retry-loop supporting calls.
   - **Recommendation:** snapshot issue metadata once at start, persist to `$GITHUB_ENV`/artifact, and only re-read when a mutating call succeeded.
   - **Estimated reduction:** **20–40%** per implement run.

3. **`test_and_mark_stable` broad workflow-run scans**
   - **Evidence:** `e2e-smoke-test` in **25212177682** uses `actions/runs?per_page=50&created=>...`; sampled failed release-validation runs show **66–87 `gh api`** calls each.
   - **Pattern:** unbatched polling of workflow runs, issue labels, and comments while waiting through clarify/plan/implement phases.
   - **Recommendation:** record the exact downstream run IDs and poll `/actions/runs/{run_id}`; collapse per-loop calls into one issue snapshot + one run snapshot.
   - **Estimated reduction:** **40–70%** API calls on that workflow, plus lower secondary-rate-limit risk.

4. **`cancel_on_pr_close` unconditional rate-limit probe**
   - **Evidence:** Run **25244043526** calls `gh api -i /rate_limit` in `_rl_wait` even when no runs match.
   - **Pattern:** preemptive rate-limit inspection on a fast path.
   - **Recommendation:** only query `/rate_limit` after a failed API call or when a retry path is entered.
   - **Estimated reduction:** small per run, high-volume friendly.

5. **`copilot_pull_request_reviewer` prepare/artifact cleanup**
   - **Evidence:** Run **25244033334** uses `github.rest.pulls.get`, paginated `pulls.listFiles`, and artifact listing/cleanup; sampled count was **10 `gh api` + 4 GitHub REST calls**.
   - **Pattern:** prepare step uses no retries; cleanup lists artifacts even on small runs.
   - **Recommendation:** retain the single `listFiles` pagination but add bounded retries and skip artifact list/delete when the run did not create the target artifact set.
   - **Estimated reduction:** modest call reduction; moderate reliability gain.

### Repository-specific API hygiene alignment

- **Mandatory batching:** `issue_pr_status` is moving in the right direction by using GraphQL first in **25244043516**, but it still falls back to per-issue REST. Keep GraphQL as the default and expand batch fields before REST fallback.
- **Cycle-local caches:** Missing on review/implement/test waiters; add one in-memory snapshot per loop iteration and job outputs across phases.
- **Fail-open behavior:** Present and appropriate in `issue_pr_status` (`conservative fail-open`) and post-merge validate dispatch. Keep this behavior, but log fallback reason once rather than per item.

## MCP & Serena Efficiency

- **Observed pattern:** The most problematic Serena/Codex behavior in sampled failures was **not broad reading** but **read-only churn with no edit**.
  - In PR evidence captured by run **25244043516**, one failure did only `serena.activate_project`, another did two read-only `serena.search_for_pattern` calls, and neither produced an edit.
  - Recent workflow summaries also explicitly instruct skipping onboarding, which is good.

- **Efficiency findings:**
  1. **Tool sessions are sometimes started without an edit plan.**
     - Evidence: runs **25243564804** and **25243569299** eventually bailed for “no actionable output.”
     - Recommendation: for trivial tasks, require an edit or explicit no-op within the first small number of tool actions; otherwise downgrade to minimal retry path.

  2. **Comment-only review flows still incur Codex/Serena overhead.**
     - Evidence: review runs **25244025745** and **25244032300** still reached Codex review setup before cancellation/skip.
     - Recommendation: gate earlier and avoid Serena/Codex initialization for non-editing paths.

  3. **No strong evidence of repeated same-region reads was surfaced in sampled deep dives.**
     - Recommendation: keep current targeted Serena usage policy, but add lightweight per-attempt counters:
       - symbol lookups
       - pattern searches
       - edit calls
       - no-op exits after tool startup

- **Parallelization opportunities:**
  - Safe parallel reads exist in:
    - PR metadata + file list fetch in reviewer prepare
    - issue metadata + linked-issue classification in `issue_pr_status`
    - downstream workflow status checks in E2E waiters, once exact run IDs are known
  - Avoid parallelizing mutating issue/label operations.

## Prompt Cache & Memory System

- **Prompt cache behavior:** The current sample does **not** include prompt-cache create/read counters, so prompt-cache effectiveness cannot be measured directly here.
- **What is observable:**
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in sampled orchestration/review logs, so cache disablement is not the issue.
  - Setup/tool caches are healthy in sampled runs: across deep-dive folders I observed **167 cache-hit/restore mentions** and **43 save mentions**, especially in `implement`, `review_autofix`, and `workflow_log_analysis`.

- **Likely cache-fragmentation causes:**
  1. Large dynamic issue/comment bodies placed early in prompts.
  2. Retry prompts that prepend fresh diagnostics each attempt.
  3. Embedded long workflow instructions repeated across near-identical runs.

- **Concrete improvements:**
  1. **Stabilize prompt prefixes**
     - Put invariant policy/instructions first.
     - Move timestamps, run URLs, prior-attempt diagnostics, and live issue comments to the end.
  2. **Reuse a frozen base prompt across retries**
     - Retry with a compact delta block instead of rebuilding the full prompt.
  3. **Separate task class from task payload**
     - Smoke/selftest/fixture tasks should use a smaller, stable prompt template than general implement.

- **Estimated impact:**
  - **Tokens:** likely **10–35%** lower on retries and trivial tasks.
  - **Latency:** moderate reduction from less prompt serialization and lower model work.
  - **Reliability:** slight improvement by making retries more consistent.

- **Memory system recommendations:**
  - Increase `retrieve` usefulness on review flows where zero-record retrieves were observed.
  - Audit why some workflows emit no memory telemetry despite being important sampled paths.
  - Fix missing helper-script fetches so run-end failure events are not silently skipped.

## Orchestrator Health

- **Healthy behaviors observed:**
  - Gating is aggressive and cheap: `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all have many **0–2s skipped** runs, which keeps idle orchestration overhead low.
  - Label/idempotency checks are working in some implement runs:
    - **25243581598** and **25243574951** detected `ai:implementing` already present and skipped duplicate work.

- **Pain points:**
  1. **Failed implement runs create noisy follow-on skip traffic**
     - Evidence: many recent skipped plan/clarify/respond runs reference prior failed implement runs and diagnostic comments.
  2. **Auto-approval/auto-answer flows can still feed low-value downstream churn**
     - Evidence: summaries mention auto-approved-by-plan and orchestrator-managed auto-answer paths around the same issue set.
  3. **Observability is uneven**
     - Memory telemetry is partial, and helper-script warnings reduce confidence in lineage.

- **Smallest safe mitigations:**
  - Collapse downstream clarify/plan/respond fan-out when the upstream implement reason is terminal and already diagnostic.
  - Add a single “terminal implement failure reason” marker that downstream orchestrator jobs can honor without spinning.
  - Keep current fail-open behavior for dispatch/cleanup, but centralize the reason in one summary comment.

- **Indicators to track:**
  - `% implement failures with explicit terminal reason`
  - `skipped follow-on runs per failed implement`
  - `review_autofix comment-only runs that still initialize Codex`
  - `memory helper missing` count per 100 runs
  - `test_and_mark_stable` success rate and median duration

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Implement loop failures on smoke tasks**
   - Affects: `implement` → `test_and_mark_stable`
   - Type: compute + retry waste
   - Evidence: runs **25243564804**, **25243569299**, plus the 4/5 failed release validations.
   - Fix: lower reasoning + early terminal diagnosis.

2. **Polling-heavy release validation**
   - Affects: `clarify -> plan -> implement` waiters in `test_and_mark_stable`
   - Type: API + wait-loop overhead
   - Evidence: `actions/runs?per_page=50` polling and 66–87 API calls per failed run.
   - Fix: exact run-ID tracking and fail-fast on terminal implement diagnostics.

3. **Review/autofix queue/setup overhead**
   - Affects: `review/autofix -> validate`
   - Type: queueing + bootstrap
   - Evidence: family **p95 1,759s**; deep-dive runs near **3,000s**.
   - Fix: skip heavy setup on comment-only flows; reduce tool bootstrap before gate.

4. **Hosted-runner wait amplification across multi-job workflows**
   - Affects: `test_and_mark_stable`, `review_autofix`, `workflow_log_analysis`, some CI
   - Type: queueing
   - Evidence: repeated “Job is waiting for a hosted runner to come online” markers; sampled counts especially high in `test_and_mark_stable`.
   - Fix: reduce unnecessary job fan-out, especially for comment-only and skipped paths.

5. **Post-merge validate dispatch attempts with no eligible workflow**
   - Affects: `review_autofix -> validate`
   - Type: local overhead + API churn
   - Evidence: **25244043522** warns `No standalone validation workflow could be dispatched for merged PR #1933.`
   - Fix: precompute workflow availability once and short-circuit earlier.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `test_and_mark_stable` is the main release blocker: **5 runs, 0 success, 80% failure, p50 4,992s**.
  - `review_autofix` has severe long-tail latency: **p95 1,759s** with sampled deep dives at **2,938–3,032s**.
  - `ci` is stable but consistently long: **p50 615.5s**, **p95 650.45s**.

- **Top failure modes**
  - Smoke-canary implement loops with no edit despite high token spend.
  - Late E2E failure verifying bait-line removal.
  - Alt-model E2E wait path not completing clarify→plan→implement.
  - Validation self-test fixture failures (`passed=1 failed=2`).

- **Highest-cost drivers**
  - High-reasoning implement retries on trivial tasks.
  - Review/autofix setup on comment-only paths.
  - Poll-heavy GH API loops in test/review/implement families.
  - Long runner waits across multi-job workflows.

- **Top 3 prioritized actions**
  1. **Ship and validate smoke-run reasoning override (`xhigh` → `low`).**
  2. **Refactor `test_and_mark_stable` waiters to follow exact run IDs and fail fast on terminal implement diagnostics.**
  3. **Move review/comment-only gates ahead of Codex/Serena/bootstrap setup and fix missing memory helper fetches.**

## Metrics Appendix

### Overall repository window

| Repo | Total runs | Success | Failure | Cancelled | Other | Failure rate |
|---|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 273 | 14 | 38 | 675 | 1.4% |

### Duration summary by key workflow family

| Workflow family | Runs | Success | Failure | Cancelled | p50 duration (s) | p95 duration (s) | Avg duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| test_and_mark_stable | 5 | 0 | 4 | 1 | 4992.0 | 5580.2 | 4563.6 |
| review_autofix | 63 | 31 | 0 | 31 | 41.0 | 1759.3 | 360.8 |
| ci | 52 | 51 | 1 | 0 | 615.5 | 650.5 | 612.7 |
| implement | 180 | 18 | 8 | 5 | 1.0 | 231.2 | 33.1 |
| orchestrate | 5 | 5 | 0 | 0 | 257.0 | 283.2 | 253.4 |
| orchestrate_poll | 35 | 35 | 0 | 0 | 45.0 | 55.2 | 56.9 |
| clarify | 213 | 27 | 0 | 0 | 1.0 | 113.2 | 16.2 |
| plan | 181 | 22 | 0 | 0 | 1.0 | 163.0 | 17.7 |
| workflow_log_analysis | 5 | 4 | 0 | 1 | 4608.0 | 5183.2 | 4234.6 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 89.0 | 89.0 | 89.0 |

### Notable failed/slow runs

| Run ID | Workflow family | Conclusion | Duration (s) | Failure point |
|---|---|---|---:|---|
| 25215477856 | test_and_mark_stable | failure | 5609 | e2e-smoke-test / Verify editor removed bait line |
| 25237291900 | test_and_mark_stable | failure | 5465 | e2e-alt-model-test / Wait for clarify→plan→implement |
| 25212177682 | test_and_mark_stable | failure | 4992 | e2e-smoke-test / Verify editor removed bait line |
| 25223836137 | test_and_mark_stable | failure | 4758 | e2e-alt-model-test / Wait for clarify→plan→implement |
| 25215784558 | review_autofix | success | 3032 | n/a |
| 25237552686 | review_autofix | success | 2938 | n/a |
| 25243569299 | implement | failure | 210 | Run Codex implementation |
| 25243564804 | implement | failure | 190 | Run Codex implementation |
| 25242537588 | nightly_validation_selftest | failure | 89 | Run validation self-test matrix |

### Observed token-heavy implement failures from sampled logs

| Related run | Evidence source | Attempt token counts | Observed total |
|---|---|---|---:|
| 25243564804 | PR body captured in run 25244043516 | 23,331 + 21,294 | 44,625 |
| 25243569299 | recent run summaries | 6,430 + 5,115 | 11,545 |
| 25224028373 | workflow-log-analysis summary | 4,502 + 4,290 | 8,792 |
| 25215763575 | workflow-log-analysis summary | 5,660 + 87,826 | 93,486 |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Unique `retrieve` events | 11 |
| Retrieve hit rate | 72.7% |
| Avg `estimated_tokens` | 33.1 |
| `keyword_method=plain` | 8 |
| `keyword_method=none` | 3 |
| Zero-record retrieves | 3 |
| `enabled:false` observed | 0 |
| `fail_open:true` observed | 0 |

### Sampled GH API concentration (deep-dive folders only)

| Run ID | Workflow family | Observed `gh api` calls |
|---|---|---:|
| 25215784558 | review_autofix | 259 |
| 25237552686 | review_autofix | 259 |
| 25215763575 | implement | 87 |
| 25224008847 | implement | 87 |
| 25224028373 | implement | 87 |
| 25237418726 | implement | 87 |
| 25237690797 | implement | 87 |
| 25237704374 | implement | 87 |
| 25212177682 | test_and_mark_stable | 87 |
| 25215477856 | test_and_mark_stable | 87 |
| 25237291900 | test_and_mark_stable | 87 |
| 25223836137 | test_and_mark_stable | 66 |

### Sampled cache and helper-script signals

| Signal | Observed count in sampled deep-dive folders |
|---|---:|
| Cache hit / restored successfully mentions | 167 |
| Cache save mentions | 43 |
| `memory helper script missing` warnings | 21 |
| Runner-wait markers | 361 |

### Gaps in current window

| Area | Gap |
|---|---|
| Prompt cache | No prompt-cache create/read counters were emitted, only disable flags |
| Precise queue time | Runner-wait markers are frequent, but queue duration is not consistently measured numerically in the sampled logs |
| Full token accounting | Token totals are only available for a subset of runs via summaries/diagnostic comments, not globally across all workflows |

## Deep Audit — Workflows & Scripts (2026-05-02)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path** — `scripts/tg_helpers.sh:312-356,381-427`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — Both cleanup walkers page forward through issue comments and delete matching tracking comments during the same pagination pass. In `tg_cleanup_phase_msgs()` and `tg_cleanup_msgs()`, page `N` is fetched, matching comments are deleted, then `page=$((page + 1))` advances. Once page-1 comments are removed, later comments shift left into earlier pages, so some tracked comments can be skipped and their Telegram messages remain undeleted. This is a real TOCTOU/pagination bug whenever an issue has enough comments to span multiple pages.
- **Recommended fix** — Split discovery from deletion. Either:
  1. collect all matching comment IDs across all pages first, then delete them in a second pass; or
  2. repeatedly re-fetch `page=1` until no matching tracking comments remain.  
  Keep the Telegram delete + GitHub comment delete pairing, but make pagination immutable during discovery.

#### CONSIST-001
- **File path** — `.github/workflows/issue_pr_status.yml:235-251`
- **Severity** — Medium
- **Category tag** — `consistency`
- **Description** — The inline fallback `set_issue_phase_label_resilient()` is not behaviorally equivalent to the canonical helper in `scripts/label_helpers.sh:146-195`. If `label_helpers.sh` exists but does not export `set_issue_phase_label_resilient`, the fallback only POSTs the target label and never removes prior phase labels. That can leave contradictory phase state such as `ai:implementing` + `ai:merged` or `ai:ready-to-merge` + `ai:closed`. Because this workflow explicitly fetches support files across refs and falls back between refs, mixed-version execution is already an expected path.
- **Recommended fix** — Replace the inline fallback with the full canonical implementation from `scripts/label_helpers.sh`, or hard-fail with a clear “support ref too old” error when the symbol is missing. Do not keep a degraded add-only fallback for phase labels.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path** — `.github/workflows/review_autofix.yml:478-527`
- **Severity** — Medium
- **Category tag** — `api-batching`
- **Description** — The standalone-validate dispatch step already does one GraphQL fetch for `closingIssuesReferences`, but when that returns empty and the workflow falls back to regex-derived issue numbers, it then performs `gh issue view ... --json labels` inside a loop for each candidate issue. Current classification cost on the fallback path is **1 + N** calls (`1` PR fetch/body parse path + up to `N` per-issue label reads). This is exactly the per-iteration REST pattern CLAUDE.md §15 says to avoid.
- **Recommended fix** — After deriving fallback issue numbers, batch-fetch their labels in one aliased GraphQL request and drive the loop from that result. Projected classification cost becomes **2 total** calls instead of **1 + N**. Extend the existing batching style used in `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql` to return `{number, labels}` for an arbitrary issue set.

#### API-002
- **File path** — `.github/workflows/issue_pr_status.yml:253-350,501-512`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The main label-sync step already batches linked issues into `TRACKING_ISSUES` / `MANAGED_ISSUES` using GraphQL, but the later “Send PR merged Telegram alert” step re-fetches each linked issue body with `_safe_gh_jq "repos/.../issues/{n}"` to rediscover whether any issue is orchestrator-managed. That is a second pass over the same issue set with **N extra issue reads** in the same execution path even though the earlier step already classified those issues.
- **Recommended fix** — Export a single boolean such as `HAS_ORCHESTRATED_LINKED_ISSUE` (or the already-built managed/tracking sets) to `$GITHUB_ENV` in the label-sync step and reuse it in the alert step. Current extra cost is **N** calls; proposed extra cost is **0**. If a shared batch helper is preferred, mirror the `number → metadata` map shape used by `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path** — `.github/workflows/internal-cancel-on-pr-close.yml:1-15, .github/workflows/internal-clarify.yml:1-17, .github/workflows/internal-implement.yml:1-19, .github/workflows/internal-issue-pr-status.yml:1-14, .github/workflows/internal-memory-maintenance.yml:1-15, .github/workflows/internal-orchestrate-clarify-respond.yml:1-15, .github/workflows/internal-orchestrate-poll.yml:1-20, .github/workflows/internal-orchestrate.yml:1-24, .github/workflows/internal-plan.yml:1-18, .github/workflows/internal-validate.yml:1-35`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — The internal wrapper workflows are near-identical thin shims over reusable workflows on `@main`: same trigger skeleton, same secret forwarding, same `uses:` shape, and only minor input differences. This is >70% structural duplication across ten files, which raises drift risk whenever wrapper pinning, secret forwarding, or default inputs change.
- **Recommended fix** — Generate these wrappers from one shared module instead of hand-maintaining them. A concrete option is a generator script such as `scripts/render_internal_wrapper.py render_wrapper(workflow_name, target_workflow, inputs_map, secrets_mode)` and a checked-in manifest listing the wrappers. Callers updated: all `internal-*.yml` files above.

#### DUP-002
- **File path** — `.github/workflows/test-and-mark-stable.yml:396-415,521-540,714-733,1133-1155,1828-1847`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — `test-and-mark-stable.yml` defines the same local `gh_api_safe()` rate-limit wrapper five separate times. These copies are already drifting in comments and surrounding behavior, and any future bugfix to rate-limit handling, stderr preservation, or fallback semantics must be applied five times inside one workflow.
- **Recommended fix** — Extract the helper to a shared script, e.g. `scripts/test_mark_stable_helpers.sh::gh_api_safe(endpoint, ...)`, and source it from each phase step. Callers updated: create-issue, plan wait, PR wait, review wait, and orchestrator wait blocks in `test-and-mark-stable.yml`.

#### DUP-003
- **File path** — `.github/workflows/issue_pr_status.yml:41-172,466-499,555-593`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — `issue_pr_status.yml` contains three separate support-fetch implementations in one job: the initial memory/helper fetch, the merged-alert fetch for `tg_helpers.sh`, and the cleanup fetch for `tg_helpers.sh`. All three repeat support-ref resolution, staged clone directory setup, self-repo shortcuts, and copy logic.
- **Recommended fix** — Move support checkout into one reusable script, e.g. `scripts/fetch_support_helpers.sh fetch_support_helpers --workflow-source <repo> --ref <ref> --dest-root <dir> --files gh_helpers.sh tg_helpers.sh memory_helpers.sh ...`, then reuse it from all three steps. Callers updated: the three `issue_pr_status.yml` steps above.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The “wait for review/autofix” `run:` block contains `${{ }}` interpolations and is already approximately **16,626 characters**, leaving only about **4,374 characters of headroom** before GitHub’s hard **21,000-character** template-expression limit. This block has grown with live-log heuristics, bait-SHA handling, and early-exit diagnostics; it is below the hard cap today but is well into the warning zone and is exactly the kind of block that has previously broken this repo.
- **Recommended fix** — Extract the full wait loop to an external script under `scripts/` and pass the few dynamic values through env vars. Preferred target shape: `scripts/test_and_mark_stable_wait_review.sh wait_for_review(pr_number, test_repo, bait_sha, review_timeout, poll_interval)`. That matches the repo’s existing mitigation pattern in `scripts/orchestrate_poll_process.sh` and `scripts/review_commit_changes.sh`.

No audited workflow exceeded the 800 KB workflow-file warning threshold; the largest was `review_autofix.yml` at 268,567 characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001
- **File path** — `scripts/mark-stable.sh:1-14`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `scripts/mark-stable.sh` appears unreferenced by any checked-in workflow or repository script, while its behavior overlaps with the maintained release workflows (`.github/workflows/mark-stable.yml` and `.github/workflows/promote-main-to-stable.yml`). It performs direct tag mutation and push operations without the workflow-level checks, summaries, or consumer-dispatch safeguards. [NEEDS VERIFICATION]
- **Recommended fix** — If this script is no longer an operator entrypoint, remove it. If it is still intended for manual use, document that explicitly and move it behind a clearer manual-only path/name so it does not look like an active workflow dependency.

#### DEBT-001
- **File path** — `scripts/tg_helpers.sh:175-205,246-276,346-350,417-421`
- **Severity** — Low
- **Category tag** — `tech-debt`
- **Description** — `tg_helpers.sh` uses `curl_gh_api` for GitHub **reads**, but falls back to raw `curl https://api.github.com/...` for GitHub **writes** (comment create/patch/delete). Those write paths therefore bypass the repo’s standard retry, rate-limit backoff, alerting, and `GITHUB_API_URL` override handling. The result is one helper with two different API reliability contracts.
- **Recommended fix** — Route the GitHub write calls through a single shared helper as well, preferably `curl_gh_api` or a thin `gh_retry gh api` wrapper for issue-comment mutations. Keep one transport contract for all GitHub calls made by `tg_helpers.sh`.

#### SHELL-001
- **File path** — `.github/workflows/mark-stable.yml:450-489`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — The repository-dispatch loop iterates with `for REPO in $REPOS; do`, where `REPOS` is a newline-delimited `jq` result. This is the classic SC2086 word-splitting pattern: it depends on the current data shape staying shell-safe rather than iterating over a proper array. Today’s repo slugs happen to be simple, but the loop is still fragile shell style.
- **Recommended fix** — Read the JSON array into a bash array and iterate safely: `mapfile -t repos < <(jq -r '.[]' "$CONSUMER_FILE")` then `for REPO in "${repos[@]}"; do ...; done`. That removes the latent shell-splitting hazard without changing behavior.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 6 | BUG-001, CONSIST-001, API-001, API-002, DUP-002, EXPR-001 |
| Low | 5 | DUP-001, DUP-003, DEAD-001, DEBT-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2 | Medium |
| Code modularization | 3-12 | Medium |
| Expression size reduction | 1 | Medium |
| Medium/Low fixes | 4-6 | Medium |
