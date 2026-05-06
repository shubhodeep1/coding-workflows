## Executive Summary

- **`review_autofix` is the biggest practical latency sink, especially on comment-only `claude/*` reviews that still run the heavy reviewer path.** Deep-dive runs `25413999630` (2,160s), `25415476868` (1,524s, from repo deep-audit), and cancelled runs `25411605236` (1,690s, from repo deep-audit), plus recent cancellations `25426794858` (1,042s), `25427857784` (815s), and `25426295569` (694s), all spent most of their time in `review / codex-agent (claude-branch-review)` while logs explicitly said `editor/commit/judge/auto-merge skipped`. **Estimated impact:** save **8–20 minutes** on affected runs. **Confidence:** high.

- **Release validation is over-polling and misclassifying terminal failures as long timeouts.** `test_and_mark_stable` has only 4 runs in-window with **2 failures / 2 successes**, average **3,560s**, and failures `25416934394` (4,579s) and `25375729485` (2,999s) stalled in `e2e-alt-model-test` / Phase 7 polling. The repo’s own `workflow_log_analysis` runs `25375766109`, `25378679803`, and `25416954546` identify Phase 4b/Phase 7 as the largest GH API hotspot with **50–80% fewer API reads** possible. **Estimated impact:** save **5–25 minutes** on failing/slow release-validation runs and materially reduce false-red release blocks. **Confidence:** high.

- **The current `review_autofix` hard failures are mostly deterministic, not flaky.** Failed runs `25370025320`, `25370115370`, and `25371432937` all died in `review / codex-agent` → `Run Codex resolver, validate, stage, commit` after repeated merge conflicts in `tests/e2e_smoke_canary.txt`; `25371432937` shows unresolved `<<<<<<< HEAD` / `>>>>>>> origin/main` markers and `Conflict resolver failed after retries.` **Estimated impact:** remove most of the current **3/99 hard failures** in `review_autofix` and unblock downstream release tests. **Confidence:** high.

- **Implement failures are wasting tokens on a known “announced edit / no-op” model behavior.** Failed runs `25417040196` and `25417030055` hit `implement / implement` → `Run Codex implementation`; surrounding summaries show bailout after **2 consecutive no-actionable-output attempts**, with example token spends of **5,429 + 4,890** and **12,627 + 16,803** before failing. **Estimated impact:** save roughly **10k–30k tokens per failed implement chain** and reduce pointless retries. **Confidence:** medium-high.

- **AI memory retrieval is active but largely ineffective for reviewer flows.** Across deep-dive logs there were **15 `retrieve` operations**, but only **2 hits** (**13.3% hit rate**); reviewer retrievals were **13/13 zero-hit**, all with `keyword_method="none"` and `estimated_tokens=0`. By contrast, both implementation retrieves hit (`records_selected=2`, `estimated_tokens=56`, `keyword_method="plain"`). **Estimated impact:** better retrieval quality could reduce repeated context reconstruction and reviewer latency, but current telemetry suggests reviewer memory is not yet helping. **Confidence:** high.

- **Prompt cache is enabled but not observable enough to prove value.** `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in recent runs like `25428440267` and `25426585510`, and there are **29** `review_autofix_cache_probe` log lines with `cache_enabled=true`, but every sampled probe reports `prompt_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. **Estimated impact:** unknown until counters are emitted; likely medium token and latency savings if prompt prefixes are stabilized. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Critical-path: Short-circuit comment-only `review_autofix` before full reviewer execution

- **Evidence:**  
  - `review_autofix` family: **99 total runs**, **p95 1,703.4s**, **63 cancelled**, average **446.7s**.  
  - Recent cancelled runs `25426794858` (1,042s), `25427857784` (815s), `25426295569` (694s), `25425830515` (669s), and `25425264833` (814s) all logged `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`  
  - Repo deep-audit `workflow_log_analysis/25416954546/step-002-deep-audit.log` states `review_autofix` is the largest end-to-end latency hotspot and estimates **8–20 minutes saved per affected run**.
- **Root cause:** Comment-only branch reviews still execute the expensive reviewer panel and associated orchestration even when mutate/commit/judge paths are pre-skipped.
- **Exact change:** Add an early gate that routes `claude/*` comment-only reviews to a lightweight single-pass comment path before `codex-agent` reviewer fan-out starts. Reuse existing detection already printed in logs (`AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW`) but make it a true short-circuit.
- **Estimated time savings:** **8–20 minutes** on affected `review_autofix` runs; likely the single biggest latency win in the window.
- **Implementation risk:** Low-medium. Safe if the lightweight path preserves final comment behavior and only skips already-unused edit/judge stages.

### 2. Critical-path: Fix `test_and_mark_stable` Phase 4b / Phase 7 retry matching and stop polling terminal failures

- **Evidence:**  
  - `test_and_mark_stable` averages **3,560s** with **50% failure rate** (2/4).  
  - Failure `25416934394` ran **4,579s** and failed in `e2e-alt-model-test` → `Wait for clarify→plan→implement (alt-model)` after repeated `ai:implementing` label polling from about `04:48:58` through `06:00:12`, ending with `Alt-model run timed out before reaching review stage`.  
  - Failure `25375729485` ran **2,999s** and logged `Phase 4b first attempt failed — re-dispatching review_autofix and retrying once` and later `Retry review run did not complete within 25 minutes`.  
  - Repo deep-audit `25375766109` and `25378679803` explicitly identify Phase 4b polling as a **50–80% API reduction opportunity** and a **15–25 minute** reliability/speed opportunity on failures.
- **Root cause:** The harness keeps re-fetching PR/workflow state on long intervals even after failure is already knowable, and retry-run correlation is weak enough that known failed retries are reclassified as timeouts.
- **Exact change:** Persist the dispatched retry run ID, poll that run directly, widen backoff after the run is identified, and hard-stop downstream phases when a retry reaches terminal outcomes like failure, `retry_timeout`, or `retry_workflow_failed`.
- **Estimated time savings:** **5–25 minutes** on failing/slow release validation runs; biggest release-path win.
- **Implementation risk:** Medium. Needs careful preservation of release-test assertions, but the change is internal and backward-compatible.

### 3. Critical-path: Cancel stale `review_autofix` runs before heavy AI work begins

- **Evidence:**  
  - `review_autofix` has **63 cancelled** runs out of **99** total.  
  - Recent cancelled runs `25426794858`, `25427857784`, and `25425264833` all consumed **11–17 minutes** before cancellation.  
  - `25426794858` logs show lengthy post-job orphan termination after cancellation, meaning work was already well underway.
- **Root cause:** Concurrency/cancellation happens too late; stale runs still acquire a runner and start long review work.
- **Exact change:** Tighten concurrency at workflow or job level so superseded PR-head runs are cancelled before `review / codex-agent` starts; add an early freshness check against the current PR head SHA before reviewer execution.
- **Estimated time savings:** **10–17 minutes** per stale run avoided; also frees runner capacity.
- **Implementation risk:** Low. Fail-open fallback can be “continue if freshness check unavailable.”

### 4. High-impact: Split or parallelize the monolithic CI `lint` job

- **Evidence:**  
  - `ci` family: **74 runs**, average **607.3s**, **p50 615s**, **p95 654.35s**.  
  - Recent successful CI runs `25426794728` (608s), `25427555118` (638s), `25427857574` (575s), `25426295506` (547s) all report that `lint` dominates nearly the full runtime.  
  - The job contains dozens of independently logged steps, including unit/contract suites for orchestrate poll, review autofix, validation, prompt files, shellcheck, ruff, and coverage gates.
- **Root cause:** A large serial job couples unrelated checks into one long critical path.
- **Exact change:** Split `lint` into at least 3 parallel jobs:  
  1. Python/shell/static checks,  
  2. orchestration/review/implement unit+contract tests,  
  3. schema/prompt/workflow-reference checks.  
  Keep the current commands unchanged; only rebalance job graph.
- **Estimated time savings:** Likely **2–5 minutes** from wall-clock CI duration, depending on runner queueing.
- **Implementation risk:** Low-medium. Main risk is duplicated environment setup; keep artifacts and caches unchanged.

### 5. Medium-impact: Convert pure runner-starvation `orchestrate_poll` failures into reschedules/no-op exits

- **Evidence:**  
  - `orchestrate_poll` family: **37 runs**, **3 failures**, **8.1% failure rate**, **p95 903s**.  
  - Failed runs `25424218738`, `25383797907`, and `25381014761` each lasted **903s** with only system logs like `Waiting for a runner to pick up this job...`; no workflow body executed.
- **Root cause:** The workflow hard-fails when GitHub never schedules a runner within the timeout window.
- **Exact change:** For the outer poller workflow, treat “never started body execution” as a retryable reschedule or neutral exit rather than a failure; optionally shorten the timeout and re-dispatch.
- **Estimated time savings:** Not a per-run compute win, but removes **15-minute dead failures** and reduces queue-induced red runs.
- **Implementation risk:** Low, if the fail-open condition is limited to “body never ran.”

### 6. Micro-optimization: Remove repeated post-job orphan cleanup in cancelled review runs

- **Evidence:**  
  - `25426794858` explicitly spent extra time terminating orphan processes after cancellation.
- **Root cause:** Heavy child process trees survive into cancellation cleanup.
- **Exact change:** Ensure long-lived subprocesses are trapped and terminated on cancellation within the review step itself.
- **Estimated time savings:** **10–60s** per cancelled review run.
- **Implementation risk:** Low.

## Cost Optimizations

Ranked by expected token/dollar savings. Overall token visibility is incomplete in this window, so estimates below are bounded to observed evidence.

### 1. Stop two-attempt implement bailouts on known no-op model output

- **Evidence:**  
  - Failed implement runs `25417040196` and `25417030055` both died in `implement / implement` → `Run Codex implementation`.  
  - Deep-dive logs show implementation memory retrieved useful context (`records_selected=2`, `estimated_tokens=56`).  
  - Surrounding summaries report `Codex bailed: 2 consecutive attempts with no actionable output...` with token examples of **5,429 + 4,890** and **12,627 + 16,803** before failing.
- **Root cause:** The workflow retries a model behavior it already knows is non-actionable for some prompts/tasks.
- **Exact change:** After one explicit “announced edit / no-op” response, either fail fast with a clearer classification or switch to a smaller recovery prompt/model path instead of rerunning the same expensive attempt.
- **Estimated savings:** **10k–30k tokens per failed implement chain** in the observed pattern.
- **Quality-risk notes:** Low if the fast-fail only triggers on the existing known signature; medium if generalized too broadly.

### 2. Reduce reviewer breadth / pass count on comment-only `review_autofix`

- **Evidence:**  
  - Comment-only `review_autofix` runs are the dominant long-duration path.  
  - Repo deep-audit `25416954546` notes cost/latency concentration in comment-only branch reviews and references repeated `REVIEWERS_SUCCESSFUL: 6`.
- **Root cause:** Multi-reviewer panels are being spent on flows that never edit, commit, or judge.
- **Exact change:** For comment-only mode, reduce to a smaller reviewer set or a single-pass summarizer/reviewer combination.
- **Estimated savings:** Potentially the largest recurring AI cost reduction in this repo, but exact token counts are missing. Based on runtime concentration, this is likely a **high** spend reducer.
- **Quality-risk notes:** Medium. Keep full reviewer breadth for merge-blocking or edit-capable paths; reduce only for comment-only flows.

### 3. Reuse analysis context in `workflow_log_analysis` to cut summarization spend

- **Evidence:**  
  - Across four deep-dive `workflow_log_analysis` runs, `summarize_unselected_runs` telemetry used **255,754**, **213,986**, **201,326**, and **247,109** tokens, totaling **918,175 tokens**.
- **Root cause:** The analysis workflow is re-summarizing many unselected runs each time instead of reusing prior condensed artifacts/context where possible.
- **Exact change:** Cache or persist first-pass run summaries between the API-redundancy and deep-audit steps within the same workflow, and avoid regenerating unchanged summaries when inputs are identical.
- **Estimated savings:** Potentially **hundreds of thousands of tokens per analysis cycle**; bounded by the observed **918,175-token** subtotal across 4 runs.
- **Quality-risk notes:** Low if reuse is keyed by run set / generated-at fingerprint.

### 4. Prevent token spend on stale `review_autofix` runs that are later cancelled

- **Evidence:**  
  - `review_autofix` had **63 cancelled** runs. Several cancellations occurred after long review execution: `25426794858` (1,042s), `25427857784` (815s), `25425264833` (814s), `25426295569` (694s).
- **Root cause:** AI work begins before stale-run detection/concurrency pruning completes.
- **Exact change:** Same as speed item #3: cancel stale runs before reviewer execution starts.
- **Estimated savings:** Medium-to-high recurring spend reduction; exact tokens unavailable.
- **Quality-risk notes:** Low.

### 5. Improve prompt-cache effectiveness before scaling model usage

- **Evidence:**  
  - Cache probes in `review_autofix` show `cache_enabled=true` but all sampled usage counters are `na`.  
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in recent successful workflows.
- **Root cause:** Cache is turned on, but either prompt prefixes are unstable, the provider is not returning counters, or the workflow is not recording them.
- **Exact change:** Make the first prompt segment deterministic across retries/runs, move volatile fields later, and emit actual create/read counters in logs.
- **Estimated savings:** Currently unquantifiable; likely medium if reviewer prompts are large and repetitive.
- **Quality-risk notes:** Low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Deterministically resolve or regenerate `tests/e2e_smoke_canary.txt` instead of sending it through generic conflict resolution

- **Failure evidence:**  
  - `review_autofix` failures `25370025320`, `25370115370`, and `25371432937` all failed in `review / codex-agent` → `Run Codex resolver, validate, stage, commit`.  
  - `25371432937` logs include `CONFLICT (content): Merge conflict in tests/e2e_smoke_canary.txt`, `Smoke override: conflict markers detected`, and literal unresolved conflict markers.
- **Root cause category:** Deterministic/generated file conflict incorrectly delegated to generic AI conflict handling.
- **Exact fix:** Special-case the canary file: regenerate it, or use a deterministic ours/theirs policy tied to the smoke-test contract, before the generic resolver runs.
- **Expected reliability impact:** Should remove the main observed hard-failure mode behind all **3 sampled `review_autofix` failures**.
- **Rollback / fail-open considerations:** Easy rollback; if deterministic regeneration fails, fall back to current resolver.

### 2. Repair `test_and_mark_stable` retry-run detection and terminal-state handling

- **Failure evidence:**  
  - Failure `25375729485` logged `Phase 4b first attempt failed — re-dispatching review_autofix and retrying once` then waited until `Retry review run did not complete within 25 minutes`.  
  - Failure `25416934394` stayed at `ai:implementing` for more than an hour and ended with `Alt-model run timed out before reaching review stage`.
- **Root cause category:** Harness control-flow bug / terminal failure misclassification.
- **Exact fix:** Persist exact retry run IDs, poll those IDs directly, stop on first terminal failure, and do not continue later release phases after a terminal Phase 4b/Phase 7 result is known.
- **Expected reliability impact:** High. The repo’s own audits describe this family as release-blocking and estimate moving from effectively unusable failing behavior to materially better release reliability.
- **Rollback / fail-open considerations:** Keep current timeout path behind a feature flag initially.

### 3. Convert runner-starvation `orchestrate_poll` failures into retryable neutral outcomes

- **Failure evidence:**  
  - `25424218738`, `25383797907`, `25381014761` failed after **903s** with only system scheduling logs and no poll-body execution.
- **Root cause category:** Infrastructure/queue starvation, not business logic.
- **Exact fix:** Detect “no body step started” and reschedule or neutral-exit instead of marking the workflow failed.
- **Expected reliability impact:** Should remove most of the observed **8.1% `orchestrate_poll` failure rate** in this window.
- **Rollback / fail-open considerations:** Very safe if restricted to runs with zero body execution.

### 4. Explicitly wire `github-token` on every `copilot_pull_request_reviewer` path

- **Failure evidence:**  
  - The only failure in that family, `25389586417`, failed in `Prepare` with `Error: Input required and not supplied: github-token`; `Upload results` also failed with the same error.
- **Root cause category:** Missing required secret/input propagation.
- **Exact fix:** Pass the token explicitly into all jobs/actions that need it, including Prepare and Upload/Cleanup paths.
- **Expected reliability impact:** Likely removes the family’s only observed hard failure in-window.
- **Rollback / fail-open considerations:** Low risk; token is already expected by the actions.

### 5. Add a distinct failure class for implement “no actionable output” loops

- **Failure evidence:**  
  - `25417040196` and `25417030055` failed after two no-op attempts.
- **Root cause category:** Model-behavior mismatch / retriable-policy bug.
- **Exact fix:** Classify this pattern separately from ordinary model failure so downstream orchestrators can choose alternate handling instead of repeating the same attempt.
- **Expected reliability impact:** Medium. It reduces noisy failures and pointless implement reruns.
- **Rollback / fail-open considerations:** Low if limited to the known log signature.

### 6. Stabilize CI around the orchestrate-poll and clarify-loop test clusters

- **Failure evidence:**  
  - CI failures `25424602678`, `25424891815`, `25425170301`, `25425264723`, and `25425830472` clustered around `Orchestrate poll process unit tests`, `Validate process cross-cycle escalation unit tests`, and `Clarify loop guard unit tests`.
- **Root cause category:** Rapidly changing orchestration logic with insufficient isolation.
- **Exact fix:** Keep these suites as separate shards with clearer ownership and faster feedback, and preserve the new regression tests already added in later successful CI runs.
- **Expected reliability impact:** Medium; should reduce bursts of CI red runs during orchestrator changes.
- **Rollback / fail-open considerations:** Low.

## AI Memory Health

- **Telemetry coverage:** Deep-dive logs contained **62** `AI_MEMORY_TELEMETRY` JSON entries.
- **Observed operations:**  
  - `record-run-event`: **33**  
  - `retrieve`: **15**  
  - `record-candidate`: **6**  
  - `summarize_unselected_runs`: **4**  
  - `processed-command-check`: **2**  
  - `processed-command-claim`: **2**
- **Not observed in this window:** `finalize-task`, `promote`, `compact`, and `processed-command-complete`. If those are expected in production, emission should be verified.

### Retrieval effectiveness

- **Retrieve hit rate:** **13.3%** (**2/15** had `records_selected > 0`)
- **Average `estimated_tokens`:** **7.5**
- **Keyword method distribution:**  
  - `none`: **13**  
  - `plain`: **2**  
  - `llm`: **0**
- **Role distribution:**  
  - `reviewer`: **13** retrieves  
  - `implementation`: **2** retrieves
- **Zero-result retrieves:** **13**
- **`fail_open: true` entries:** **0**
- **`enabled: false` entries:** **0**

### Findings

1. **Reviewer memory retrieval is effectively non-functional in this sample.**  
   All **13 reviewer retrieves** returned `records_selected=0`, used `keyword_method="none"`, and budgeted `estimated_tokens=0`. This strongly suggests reviewer retrieval is running but not producing usable matches.

2. **Implementation retrieval works better than reviewer retrieval.**  
   Both implement failures (`25417040196`, `25417030055`) logged successful retrieve hits with `records_selected=2`, `estimated_tokens=56`, `keyword_method="plain"`, which is a materially better pattern.

3. **Push retry pressure is low, but one retry spike was observed.**  
   One `record-run-event` entry in `errors/.../implement/25417030055/step-001-implement_implement.log` reported `push_attempts: 2`.

### Recommendations

- Add reviewer retrieval diagnostics that log why `keyword_method` fell back to `none`.
- Seed reviewer retrieval with PR/file/topic keywords instead of leaving budget at zero.
- Track per-role retrieval hit rate as a first-class metric; the current **0/13 reviewer hit** rate is actionable.
- Verify whether `finalize-task`/`promote`/`compact` should have appeared in this workflow window; if yes, telemetry emission is incomplete.

## GH API Call Audit

### 1. Biggest hotspot: `test_and_mark_stable` Phase 4b / Phase 7 poll loops

- **Evidence:**  
  - Failures `25375729485` and `25416934394` spent **2,999s** and **4,579s** in retry/poll logic.  
  - Repo deep-audit `25375766109`, `25378679803`, and `25416954546` explicitly call this the top API hotspot, with repeated PR-state + workflow-run polling every **10–15s** and an estimated **50–80%** reducible read volume.
- **Pattern:** Unbatched repeated reads in loops.
- **Recommendation:** Persist dispatched run IDs, poll direct run endpoints with widening backoff, and stop polling other candidate runs once the target retry run is known.
- **Estimated API reduction:** **50–80%** on failing/slow release-validation runs.
- **Rate-limit risk reduction:** High, even though no 429s were observed in this sample.

### 2. `review_autofix` repeats baseline PR metadata and check-run lookups

- **Evidence:**  
  - Recent and deep-audit logs point to repeated calls for PR metadata, files, commits, comments, reviews, and check-run state in `review_autofix`; repo deep-audit `25416954546` summarizes this as `review_autofix / gate + check polling`.
- **Pattern:** Re-fetching overlapping PR state within the same run.
- **Recommendation:** Fetch PR metadata once per run, persist to a JSON artifact/env file, and reuse across gate, reviewer, and post-merge dispatch stages. Batch check-run reads where possible.
- **Estimated API reduction:** Remove roughly **4–8 baseline reads per run** plus poll reductions on long reviews.
- **Rate-limit risk reduction:** Medium.

### 3. `copilot_pull_request_reviewer` relists artifacts after already knowing the producing run

- **Evidence:**  
  - Recent success `25427558760` called `/repos/shubhodeep1/coding-workflows/actions/runs/25427558760/artifacts` during cleanup after artifact production and upload; logs explicitly note the list-before-delete pattern.
- **Pattern:** Artifact relisting instead of passing artifact IDs forward.
- **Recommendation:** Capture artifact IDs in the producing job and pass them to cleanup, or delete in the producing job when feasible.
- **Estimated API reduction:** At least **1 list call per run**, plus simpler cleanup logic.
- **Rate-limit risk reduction:** Low-medium.

### 4. Preemptive `/rate_limit` calls in retry wrappers

- **Evidence:**  
  - `cancel_on_pr_close` recent run `25428440261` logs `_rl_wait()` calling `gh api -i /rate_limit`.  
  - Repo deep-audit also flags similar behavior in `orchestrate_poll`.
- **Pattern:** Rate-limit endpoint consulted before a confirmed throttle event.
- **Recommendation:** Call `/rate_limit` only after a real 403/429/secondary-rate-limit response, or only on retry attempt 2+.
- **Estimated API reduction:** Small per run (**~1 background call** on affected runs), but easy and safe.
- **Rate-limit risk reduction:** Low direct reduction; medium hygiene improvement.

### 5. `review_autofix` / issue linkage fallback likely does per-item issue lookups

- **Evidence:**  
  - Repo deep-audit notes GraphQL + fallback issue-view patterns in post-merge validate dispatch.
- **Pattern:** Potential N+1 fallback when labels/linked issues are incomplete.
- **Recommendation:** Prefer batched GraphQL for linked issue labels/status and reserve REST per-issue fallback only for missing entries.
- **Estimated API reduction:** Moderate on PRs with many linked issues.
- **Rate-limit risk reduction:** Medium.

## Prompt Cache & Memory System

### Prompt cache behavior

- **Enabled state observed:**  
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` in recent `review_autofix` run `25428440267` and `orchestrate_poll` run `25426585510`.
- **Probe visibility:**  
  - Across deep-dive logs, **29** `openrouter usage phase=review_autofix_cache_probe` lines were found.  
  - Sample lines from `25371432937`, `25394267845`, and `25413999630` report `cache_enabled=true` but `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`.

### Interpretation

1. **Cache is enabled, but effectiveness is unmeasurable from current logs.**  
   There is no create/read counter evidence to prove hits, misses, or savings.

2. **Cache fragmentation is likely, but this is an inference.**  
   Given that comment-only `review_autofix` runs are long and repetitive yet there is no observable cache benefit, the most likely causes are unstable prompt prefixes, dynamic noise early in prompts, or probe instrumentation that is too shallow. This is an inference, not a directly logged fact.

3. **Memory retrieval is bifurcated by role.**  
   Implementation memory is useful in the sample; reviewer memory is not.

### Recommendations

- **Emit real cache counters** for `cache_creation_input_tokens` and `cache_read_input_tokens` in `review_autofix`, `implement`, `workflow_log_analysis`, and `test_and_mark_stable`.
- **Stabilize prompt prefixes** by moving volatile fields later in the prompt and keeping static policy/instructions first.
- **Deduplicate repeated comment-only review prompts** so the same branch-review mode shares a stable cacheable prefix.
- **Treat reviewer memory as a separate remediation track** from implementation memory; current reviewer retrieval has **0% hit rate** in-sample.
- **Expected impact:**  
  - **Tokens:** potentially medium savings, but not yet quantifiable.  
  - **Latency:** medium for repeated reviewer flows.  
  - **Reliability:** low direct impact, medium observability improvement.

## Orchestrator Health

### Current health assessment

1. **Clarify and plan are not the main bottlenecks.**  
   Family medians are extremely low (`clarify` p50 **1s**, `plan` p50 **1s**), with many runs landing in `other` statuses. They are not where the system is spending time.

2. **Implement has a small failure count but a meaningful model-policy problem.**  
   The family has only **2 failures** in-window, but both reflect the same costly no-actionable-output loop.

3. **Review/autofix is where orchestration pain accumulates.**  
   High cancellation volume, long comment-only reviews, and deterministic conflict failures all cluster here.

4. **Poller health is skewed by runner availability rather than orchestration logic.**  
   `orchestrate_poll` failures were queue starvation, not bad poll logic.

5. **Release validation orchestration is too loop-heavy and too slow to recognize terminal states.**  
   That is the clearest cross-workflow control-flow weakness.

### Smallest safe mitigations

- Add an **early PR-head freshness check** before `review_autofix` AI work.
- Add a **distinct terminal state** for implement “no actionable output.”
- Add **direct retry-run tracking** in `test_and_mark_stable`.
- Change `orchestrate_poll` to **neutral/reschedule on runner-only starvation**.
- Track cancellation reason and age at cancellation for `review_autofix`.

### Observable indicators teams should track

- `review_autofix`:
  - cancellation count before `codex-agent` start vs after start
  - comment-only run median duration
  - stale-run prevented count
- `test_and_mark_stable`:
  - time spent in Phase 4b / Phase 7
  - count of terminal failures misreported as timeouts
  - direct-run-ID poll adoption rate
- `implement`:
  - count of no-actionable-output bailouts
  - tokens spent before bailout
- `orchestrate_poll`:
  - runner-starvation failures
  - body-started vs body-never-started failures
- Memory/cache:
  - reviewer retrieve hit rate
  - cache create/read token counts

## Pipeline Flow Bottlenecks

### 1. Queueing overhead

- **Observed in:** `ci`, `review_autofix`, `copilot_pull_request_reviewer`, `orchestrate_poll`, `promote_main_to_stable`.
- **Evidence:** Multiple recent runs explicitly log `Job is waiting for a hosted runner to come online.`
- **Impact:** Adds noise and occasional hard failures (`orchestrate_poll`).
- **Fix order:** First neutralize queue-only failures; second tighten stale-run cancellation so queued work does not start unnecessarily.

### 2. Compute bottlenecks

- **Dominant:** `review_autofix` comment-only reviewer path.
- **Secondary:** monolithic `ci` lint job.
- **Release path:** `test_and_mark_stable` long verification harnesses.
- **Fix order:**  
  1. Comment-only review short-circuit  
  2. Release polling refactor  
  3. CI sharding

### 3. Retry overhead

- **Dominant:** `test_and_mark_stable` Phase 4b / Phase 7 and implement no-op retries.
- **Evidence:** Multi-minute loops after failure was already effectively determined.
- **Fix order:**  
  1. Direct retry-run tracking in release tests  
  2. Single-attempt classification for known implement no-op signature

### 4. Merge/conflict overhead

- **Dominant:** `review_autofix` canary file conflicts.
- **Evidence:** All sampled hard review failures cluster here.
- **Fix order:** Deterministic canary regeneration before generic resolver.

### 5. Clarify → plan → implement → review/autofix → validate/orchestrate map

- **Clarify:** not a bottleneck in this window.
- **Plan:** not a bottleneck in this window.
- **Implement:** mostly cheap, but costly when the no-op model behavior triggers.
- **Review/autofix:** largest day-to-day latency and cancellation hotspot.
- **Validate / release harness:** biggest release-path bottleneck.
- **Orchestrate/poll:** mostly healthy when scheduled, unhealthy when starved of runners.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` comment-only branch reviews consuming **11–36 minutes** despite skipping edit/judge paths (`25413999630`, `25426794858`, `25427857784`).
- `test_and_mark_stable` Phase 4b / Phase 7 polling consuming **50–76 minutes** on the release path (`25375729485`, `25416934394`).
- `ci` monolithic `lint` job running around **9–11 minutes** per run (`25426794728`, `25427555118`, `25427857574`).

**Top failure modes**
- Deterministic canary merge conflicts in `review_autofix` (`25370025320`, `25370115370`, `25371432937`).
- Queue-only `orchestrate_poll` failures with no workflow body execution (`25424218738`, `25383797907`, `25381014761`).
- Missing `github-token` in `copilot_pull_request_reviewer` (`25389586417`).
- Implement no-op model bailout loops (`25417040196`, `25417030055`).

**Highest-cost drivers**
- Comment-only `review_autofix` reviewer panels.
- `workflow_log_analysis` summarization spend (**918,175 tokens** across 4 `summarize_unselected_runs` ops).
- Repeated implement retries on known no-actionable-output behavior.
- Unmeasured prompt-cache misses or fragmentation in review flows.

**Top 3 prioritized actions**
1. **Short-circuit comment-only `review_autofix` before full reviewer execution and cancel stale runs early.**
2. **Refactor `test_and_mark_stable` Phase 4b / Phase 7 to persist retry run IDs, widen backoff, and stop on terminal failures.**
3. **Deterministically resolve `tests/e2e_smoke_canary.txt` conflicts before generic AI conflict resolution.**

## Metrics Appendix

### Repo-level summary

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 295 | 17 | 70 | 618 | 1.7% | 137.2 | 2.0 | 644.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Avg duration (s) | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `review_autofix` | 99 | 31 | 3 | 63 | 446.7 | 57.0 | 1703.4 | Largest latency/cancel hotspot |
| `ci` | 74 | 69 | 5 | 0 | 607.3 | 615.0 | 654.4 | Monolithic `lint` dominates |
| `orchestrate_poll` | 37 | 34 | 3 | 0 | 133.8 | 51.0 | 903.0 | Failures are queue-only |
| `test_and_mark_stable` | 4 | 2 | 2 | 0 | 3560.3 | 3414.5 | 4466.7 | Release-path bottleneck |
| `copilot_pull_request_reviewer` | 28 | 27 | 1 | 0 | 186.6 | 168.0 | 352.9 | Single token-wiring failure |
| `implement` | 164 | 18 | 2 | 7 | 23.4 | 1.0 | 167.6 | Small family average skewed by many non-terminal runs |

### Representative long / failed runs

| Run ID | Workflow family | Conclusion | Duration (s) | Failure / bottleneck point |
|---|---|---|---:|---|
| `25413999630` | `review_autofix` | success | 2160 | Comment-only review still ran full reviewer path |
| `25426794858` | `review_autofix` | cancelled | 1042 | Long `review / codex-agent (claude-branch-review)` before cancellation |
| `25371432937` | `review_autofix` | failure | 637 | Conflict resolver failure on `tests/e2e_smoke_canary.txt` |
| `25416934394` | `test_and_mark_stable` | failure | 4579 | `e2e-alt-model-test` timeout waiting for phase progression |
| `25375729485` | `test_and_mark_stable` | failure | 2999 | Phase 7 / retry-run verification timeout |
| `25424218738` | `orchestrate_poll` | failure | 903 | Runner starvation; no poll body executed |
| `25417040196` | `implement` | failure | 137 | No-actionable-output bailout in implement |
| `25389586417` | `copilot_pull_request_reviewer` | failure | 42 | Missing `github-token` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry JSON entries parsed | 62 |
| `retrieve` ops | 15 |
| Retrieve hit rate | 13.3% |
| Avg retrieve `estimated_tokens` | 7.5 |
| Zero-result retrieves | 13 |
| `enabled: false` retrieves | 0 |
| `fail_open: true` retrieves | 0 |
| Reviewer retrieves | 13 |
| Reviewer retrieve hits | 0 |
| Implementation retrieves | 2 |
| Implementation retrieve hits | 2 |

### Retrieval method distribution

| `keyword_method` | Count |
|---|---:|
| `none` | 13 |
| `plain` | 2 |
| `llm` | 0 |

### Prompt cache observability summary

| Signal | Observed value |
|---|---|
| Cache feature enabled | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) |
| Review cache probe lines found | 29 |
| Probe model example | `minimax/minimax-m2.5` |
| `cache_enabled` in probes | `true` |
| `prompt_tokens` reported | `na` |
| `cache_creation_input_tokens` reported | `na` |
| `cache_read_input_tokens` reported | `na` |
| Verdict | Cache enabled but savings unmeasurable from current logs |

### Token evidence available in-window

| Source | Tokens |
|---|---:|
| `workflow_log_analysis` `summarize_unselected_runs` total across 4 runs | 918,175 |
| Implement failure chain example (`25417040196` related) | 5,429 + 4,890 |
| Implement failure chain example (`25417030055` related) | 12,627 + 16,803 |

### GH API hotspot summary

| Workflow / step area | Observed pattern | Evidence | Estimated reduction |
|---|---|---|---|
| `test_and_mark_stable` / Phase 4b, Phase 7 | Repeated PR/workflow polling every 10–15s | `25375729485`, `25416934394`, repo deep-audits `25375766109`, `25378679803`, `25416954546` | 50–80% fewer reads on failing runs |
| `review_autofix` / gate + check polling | Repeated PR metadata / check-run lookups | Repo deep-audit `25416954546` | 4–8 baseline reads/run plus poll reduction |
| `copilot_pull_request_reviewer` / cleanup | Artifact relisting before deletion | `25427558760` | ~1 list call/run |
| `cancel_on_pr_close` / retry plumbing | Preemptive `/rate_limit` probe | `25428440261` | ~1 call on affected runs |
| `orchestrate_poll` / retry plumbing | Preemptive `/rate_limit` probe (from repo deep-audit) | `25378679803` deep-audit summary | ~1 call on affected runs |

### Data gaps

| Gap | Effect on analysis | Next collection step |
|---|---|---|
| No full per-run token totals for most workflows | Cost estimates are bounded to observed examples | Emit standardized token totals per AI step |
| Prompt cache counters are `na` | Cannot quantify cache hit/miss savings | Log create/read token counters |
| Deep-dive sample is selective | Some claims rely on `log_summary` for widening coverage | Keep attaching summaries for unselected runs and include more token/API excerpts in deep dives |
| Missing memory ops like `finalize-task` / `promote` in this sample | Cannot assess full memory lifecycle | Verify whether those ops should emit in these workflows |

## Deep Audit — Workflows & Scripts (2026-05-06)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `.github/workflows/issue_pr_status.yml:353-386,402-445`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The workflow explicitly skips orchestrator-tracking issues during the PR-close mutation pass (`continue` at lines 356-358), but then exports the unfiltered `ISSUE_NUMBERS` list to `LINKED_ISSUE_NUMBERS` (lines 383-386) and finalizes every linked issue as `merged`/`closed` in the memory lineage step (lines 431-444). That means a tracking issue mentioned in a child PR body can still be finalized as terminal in memory even though the same workflow says its terminal lifecycle is owned by the poller.  
  **Recommended fix** — Export a filtered list after classification, e.g. only non-tracking issues, and use that filtered list for downstream lineage finalization and cleanup steps. The simplest safe change is to build `FINALIZABLE_ISSUE_NUMBERS` inside the classification loop and consume that instead of raw `ISSUE_NUMBERS`.

- **ID** — CONSIST-001  
  **File path** — `.github/workflows/issue_pr_status.yml:133-171,407-419`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — The helper bootstrap step marks `MEMORY_HELPERS_READY=0` whenever required memory helper files cannot be fetched (lines 133-171), and the later lineage-finalization step hard-fails the workflow when that flag is not `1` or when `memory_helpers.sh` is missing (lines 412-419). That is inconsistent with the repository’s documented memory behavior that memory failures should fail open rather than fail the workflow. In practice, label/close mutations can succeed and the workflow still ends red because support-file bootstrap was transiently unavailable.  
  **Recommended fix** — Make lineage finalization best-effort: emit a warning and `exit 0` when helper bootstrap failed, mirroring the fail-open behavior already implemented in `scripts/memory_helpers.sh`. If you still want observability, log a structured warning or telemetry event rather than failing the job.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `.github/workflows/review_autofix.yml:497-549`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The post-merge validate-dispatch step first fetches linked issues plus their labels in one GraphQL call (lines 497-502). But when `closingIssuesReferences` is empty and the workflow falls back to regex-derived issue numbers (lines 504-515), the loop at lines 519-529 performs `gh issue view` once per issue to recover labels. That creates an N+1 path in the same execution flow.  
  **Current call count** — Fallback path is `1` GraphQL call + optional `1` PR fetch + `N` `gh issue view` calls.  
  **Proposed call count after fix** — `1` GraphQL call + optional `1` PR fetch + `1` aliased GraphQL issue batch.  
  **Batching/helper pattern to extend** — Reuse the aliased GraphQL pattern already used in `.github/workflows/issue_pr_status.yml:288-320`, or the batched-issue helper shape used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — After regex fallback resolves issue numbers, issue one aliased GraphQL query that fetches `number` and `labels` for all parsed issues, then drive the loop from that batch result instead of per-issue `gh issue view`.

- **ID** — BATCH-001  
  **File path** — `scripts/orchestrate_poll_process.sh:7466-7479`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — `_has_active_autofix_run` checks the same branch three times by calling `gh run list` once for each workflow name (`ai-review.yml`, `internal-review.yml`, `review_autofix.yml`). The code only needs to know whether any in-flight autofix run exists for the branch, so the three serial reads are redundant.  
  **Current call count** — `3` list-runs calls per invocation.  
  **Proposed call count after fix** — `1` branch-scoped runs query, filtered locally by workflow path/name.  
  **Batching/helper pattern to extend** — Extend `autofix_retrigger_has_inflight_peer()` in `scripts/gh_helpers.sh:1171-1205`, which already performs a single `/actions/runs` read and local jq filtering.  
  **Recommended fix** — Replace the per-workflow loop with one `gh api /repos/{repo}/actions/runs?branch=...&per_page=...` request and a jq filter that matches any of the allowed workflow files.

- **ID** — API-002  
  **File path** — `.github/workflows/issue_pr_status.yml:280-320,503-512`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The main close-handler step already classifies linked issues in a single batched GraphQL query (lines 280-320), but the later “Send PR merged Telegram alert” step loops over `LINKED_ISSUE_NUMBERS` and fetches each issue body individually with `_safe_gh_jq` to detect whether any linked issue is orchestrator-managed (lines 503-512). That repeats data acquisition the job already performed earlier.  
  **Current call count** — `1` batched classification call in the close step, then `N` per-issue REST reads in the alert step.  
  **Proposed call count after fix** — `1` batched classification call total, `0` extra per-issue reads in the alert step.  
  **Batching/helper pattern to extend** — Reuse the existing aliased GraphQL classification output from lines 288-320; no new API shape is needed.  
  **Recommended fix** — Export `TRACKING_ISSUES`, `MANAGED_ISSUES`, or a simple `HAS_ORCHESTRATED_LINK=true/false` flag to `$GITHUB_ENV` in the first step, and consume that value in the Telegram alert step instead of re-querying issue bodies.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `.github/workflows/validate.yml:185-480`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The support-bootstrap logic in `validate.yml` (`checkout_support_ref`, staged support dirs, `copy_from_ref_or_local`) is near-identical to the same workflow-support fetch logic in `.github/workflows/issue_pr_status.yml:41-171` and `.github/workflows/validation-improvements-intake.yml:51-130`. All three blocks independently manage `WF_REMOTE_URL`, primary/main fallback clones, and file-copy semantics. That creates drift risk for auth handling, fallback policy, and file selection.  
  **Recommended fix** — Move this into a shared module, preferably `scripts/fetch_workflow_support.sh`, with a signature like `fetch_workflow_support <wf_source> <preferred_ref> <primary_dir> <main_dir> <repo_path> <target_path> [allow_main_fallback] [require_remote]`. Update `validate.yml`, `issue_pr_status.yml`, and `validation-improvements-intake.yml` to call the shared helper instead of carrying private copies.

- **ID** — DUP-002  
  **File path** — `.github/workflows/workflow-log-analysis.yml:387-403,811-814,1139-1142`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — The workflow emits the same `AI_PHASE_FAILURE_V1` issue comment + `ai:log-analysis-failed` label mutation pattern three times: once in the shared helper, then again in the deep-audit failure path, and again in the API-redundancy failure path. The repeated JSON payload scaffold and comment/label writes make it easy for one failure path to drift from the others.  
  **Recommended fix** — Consolidate the comment+label write into a single helper function, e.g. `post_log_analysis_failure <failed_step_name> <failure_mode> <attempt_count> <summary>`, owned either inline near `emit_log_analysis_phase_failure` or in a new `scripts/log_analysis_helpers.sh`. Then update the deep-audit and API-redundancy callers to use it.

- **ID** — DUP-003  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3285-3496`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — `test-and-mark-stable.yml` repeats the same “dispatch workflow, poll for new run id, then poll run status/conclusion until deadline” shell block for multiple workflows, including `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, `internal-orchestrate`, and additional later smoke checks. The blocks differ mostly in `WF_FILE`, timeout, success criteria, and a few dispatch inputs, but the watcher loop structure is the same.  
  **Recommended fix** — Extract a shared script such as `scripts/watch_dispatched_workflow.sh` with a signature like `watch_dispatched_workflow <repo> <workflow_file> <pre_run_id> <register_deadline_secs> <complete_deadline_secs> [success_conclusions_csv]`. Callers in `test-and-mark-stable.yml` would pass only workflow-specific parameters and any dispatch command prelude.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1604-2008`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The Phase 4b retry/pytest `run:` block is already an estimated **21,303 characters** and contains GitHub interpolations. That leaves **-303 characters** of headroom against the 21,000-character expression ceiling, so further edits to this block risk runner-side workflow rejection.  
  **Recommended fix** — Extract the full Phase 4b retry logic into an external script, e.g. `scripts/e2e_phase4b_retry_guard.sh`, and pass only the required env vars from YAML.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1187-1517`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The wait-review polling block is an estimated **19,715 characters**, leaving only **1,285 characters** of headroom. It is already inside the repo’s historical danger zone for expression overflows.  
  **Recommended fix** — Split this into multiple steps or, preferably, extract it into a script such as `scripts/wait_for_review_run.sh` and keep the workflow step limited to env setup and one script invocation.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/review_autofix.yml:1272-1594`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The “Collect PR metadata” block is an estimated **16,452 characters**, leaving **4,548 characters** of headroom. That is below the hard limit today but large enough that modest future additions can push it over.  
  **Recommended fix** — Extract the metadata/comments/diff collection logic into a shared script, e.g. `scripts/collect_review_context.sh`, and keep the YAML step as a thin wrapper.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/validate.yml:188-481`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The support-file bootstrap block is an estimated **16,505 characters**, leaving **4,495 characters** of headroom. Because this block keeps growing as more support files are added, it is a clear expansion-risk hotspot.  
  **Recommended fix** — Move the support-bootstrap logic into `scripts/fetch_workflow_support.sh` or a composite action and leave only a small invocation block in YAML.

- **ID** — EXPR-005  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:817-1100`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The auto-answer / loop-guard / escalation block is an estimated **15,155 characters**, leaving **5,845 characters** of headroom. It is not over the threshold yet, but it is large enough that future prompt or guard additions can trip the limit.  
  **Recommended fix** — Extract the post-Codex answer-processing flow into a script such as `scripts/orchestrate_clarify_respond_post_answer.sh`, or split memory checks, escalation handling, and answer posting into separate steps.

No workflow currently exceeds the 800 KB file-size early-warning threshold. The largest workflow files are `review_autofix.yml` at **279,655 bytes** and `test-and-mark-stable.yml` at **261,186 bytes**.

### Section 5: Cross-Cutting Concerns

- **ID** — DEAD-001  
  **File path** — `scripts/orchestrate_lib.py:988-1368`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `parse_phase_failure_markers`, `evaluate_phase_failure_resume`, `resolve_label_repair_evidence`, and `choose_most_advanced_conclusive_evidence` are defined here, but repository-wide search shows no caller in any workflow or shell script. The code is substantial and overlaps live poller decision logic, so it can silently drift away from actual orchestrator behavior.  
  **Recommended fix** — Either wire these helpers into `scripts/orchestrate_poll_process.sh` where label-repair and phase-failure decisions are made, or explicitly move them behind a tested feature flag / reserved module boundary so unused logic does not keep diverging.

- **ID** — CONSIST-002  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — This workflow defines its own `_rl_wait`/`_gh_retry` bootstrap wrapper instead of using the shared retry behavior in `scripts/gh_helpers.sh`. Similar private wrappers also exist in `.github/workflows/orchestrate_poll.yml:67-100` and `.github/workflows/review_autofix.yml:1275-1313`. The implementations have already diverged on rate-limit probing, retryable/non-retryable error handling, and stderr capture semantics, so the same `gh` failure mode behaves differently depending on which workflow hits it.  
  **Recommended fix** — Introduce one shared early-bootstrap helper, e.g. `scripts/gh_bootstrap_retry.sh`, with the same contract as `gh_helpers.sh` but safe to source before the full support bootstrap. Then update `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, and `review_autofix.yml` to source that helper rather than carrying local copies.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | EXPR-001, EXPR-002 |
| Medium | 11 | BUG-001, CONSIST-001, API-001, BATCH-001, API-002, DUP-001, DUP-003, EXPR-003, EXPR-004, EXPR-005, CONSIST-002 |
| Low | 2 | DUP-002, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 5 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 4 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-05-06)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is proven local and semantics-preserving enough for direct implementation. `NEEDS_VERIFICATION` means the overlap is real, but a human or follow-up pass must verify freshness/error-handling assumptions before changing it. `RISKY_SKIP` means the redundancy is visible but sits in a polling/race-defense/retry-sensitive path, so it must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:2734-2737`  
  **Current call count** — `2` calls per poll iteration  
  **Proposed call count** — `1` call per poll iteration  
  **Endpoint(s)** — REST `GET /repos/{repo}/actions/runs/{run_id}`  
  **Evidence** — The loop fetches the same run resource twice, once for `status` and once for `conclusion`:

  ```bash
  EXISTING_STATUS=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
    --jq '.status // ""' 2>/dev/null || echo "")
  EXISTING_CONCLUSION=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
    --jq '.conclusion // ""' 2>/dev/null || echo "")
  ```

  The same workflow already uses the consolidated pattern elsewhere in later watcher blocks, e.g. `.github/workflows/test-and-mark-stable.yml:3299-3301`:

  ```bash
  JSON=$(gh api "repos/${TEST_REPO}/actions/runs/${NEW_ID}" --jq '{status, conclusion}' 2>/dev/null || echo "")
  STATUS=$(echo "${JSON}" | jq -r '.status // ""')
  CONCLUSION=$(echo "${JSON}" | jq -r '.conclusion // ""')
  ```

  **Proposed fix** — In the existing-run wait loop inside `.github/workflows/test-and-mark-stable.yml:2711-2742`, replace the paired `gh api ... --jq '.status'` / `gh api ... --jq '.conclusion'` calls with one `JSON=$(gh api ... --jq '{status, conclusion}')`, reusing the same parsing shape already used at `.github/workflows/test-and-mark-stable.yml:3299-3301`, `3357-3359`, `3418-3420`, `3481-3483`, `3595-3597`, and `3779-3781`.  
  **Safety rationale** — This is inside a 5-second polling loop for a recovery/verification path, so despite exact endpoint overlap it matches the `RISKY_SKIP` trigger for looped poll semantics.  
  **Downstream signal** — Do not auto-implement; manual review must confirm unchanged timeout behavior, unchanged log lines, and unchanged handling when one field fetch would have failed independently.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/orchestrate_clarify_respond.yml:65-80` and `.github/workflows/orchestrate_clarify_respond.yml:404-415`  
  **Current call count** — `2` unconditional child-issue reads, plus `2` tracking-issue reads when `TRACKING_NUM` resolves  
  **Proposed call count** — `1` unconditional child-issue read, plus `1` tracking-issue read when `TRACKING_NUM` resolves  
  **Endpoint(s)** — REST `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — The first step fetches the child issue, then optionally fetches the tracking issue title:

  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.title // ""')"
  ...
  TRACKING_TITLE="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.title // ""' 2>/dev/null || echo "")"
  ```

  Later, the same job re-fetches the same child issue and the same tracking issue:

  ```bash
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_META}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_META}" | jq -r '.title // ""')"
  ...
  TRACKING_BODY="$(gh_retry gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" --jq '.body // ""')"
  ```

  **Proposed fix** — Extend the `Check orchestrator metadata` step to persist the full child-issue payload and, when `TRACKING_NUM` is present, the full tracking-issue payload into temp files under `$RUNNER_TEMP`; then update the later `Fetch issue metadata` step to read those cached payloads before falling back to `gh_retry gh api`.  
  **Safety rationale** — The endpoint overlap is exact and it is the same workflow job, but the issue/tracking bodies are user-editable between steps, so freshness assumptions need validation before reuse is made authoritative.  
  **Downstream signal** — Verify on an orchestrator-managed issue that no step between lines 65 and 415 requires fresher issue/tracking text than the initial read, then compare prompt inputs before/after reuse on one real run.

- **ID** — `REUSE-002`  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:428-435`  
  **Current call count** — `2` calls  
  **Proposed call count** — `1` call  
  **Endpoint(s)** — REST `POST /repos/{repo}/issues` and REST `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — The step creates the issue, extracts only the number, then immediately re-reads the issue just to obtain `html_url`:

  ```bash
  ISSUE_NUMBER=$(gh api "repos/${TEST_REPO}/issues" \
    -f title="${TITLE}" \
    -f body="${BODY}" \
    --jq '.number')

  ISSUE_URL=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url')
  ```

  **Proposed fix** — In the `Create issue` step, capture the full create response once (JSON string or temp file), parse both `.number` and `.html_url` from that response, and remove the immediate follow-up `GET /issues/{ISSUE_NUMBER}`.  
  **Safety rationale** — The second call is an adjacent re-fetch of data that should already be available from the create response, but eliminating it changes the current “POST succeeded, GET failed” failure mode, so the harness behavior should be verified first.  
  **Downstream signal** — Verify with one dry run that the create-issue response always exposes `number` and `html_url`, and confirm the release-gate step should no longer fail solely because a follow-up read would have failed.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`  
  **Safety tag** — `SAFE_TO_MERGE`  
  **File path and line ranges** — `scripts/review_rb_judge.sh:146-170` and `scripts/review_rb_judge.sh:241-244`  
  **Current call count** — `N` issue-body reads, where `N` is the number of linked issues  
  **Proposed call count** — `1` issue-body read  
  **Endpoint(s)** — REST `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — The loop fetches every linked issue body, but only the first body is ever retained for downstream use:

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

  Later, only `FIRST_ISSUE_BODY` is consumed:

  ```bash
  if [ -n "${FIRST_ISSUE}" ]; then
    echo "=== ISSUE #${FIRST_ISSUE} (original requirement) ==="
    echo
    echo "${FIRST_ISSUE_BODY}"
  fi
  ```

  After the first successful assignment, subsequent `BODY=...` calls are not read by any downstream path in this block.  
  **Proposed fix** — In the linked-issue context block of `scripts/review_rb_judge.sh`, fetch the issue body only while `FIRST_ISSUE_BODY` is empty (or fetch once immediately after `FIRST_ISSUE` is chosen) and stop doing per-issue body lookups for later linked issues.  
  **Safety rationale** — The same script only consumes the first linked issue body, with no pagination, no retry-loop semantics, and no downstream use of later `BODY` values, so removing the extra reads preserves behavior.  
  **Downstream signal** — Implement directly: fetch only the first linked issue body and skip all subsequent per-issue body reads in that block.

### Cross-References to Deep Audit Section

- `API-001`: `NEEDS_VERIFICATION` — The fallback `gh issue view` N+1 in `.github/workflows/review_autofix.yml` is real, but a batched GraphQL replacement needs validation for fallback ordering and fail-open behavior before implementation.
- `BATCH-001`: `RISKY_SKIP` — The `gh run list` consolidation target is inside `scripts/orchestrate_poll_process.sh`, which this pass treats as race-defense code requiring manual review.
- `API-002`: `NEEDS_VERIFICATION` — Reusing earlier orchestrator classification data in `.github/workflows/issue_pr_status.yml` is directionally correct, but the alert step currently gets a second chance to recover from earlier transient fetch failures.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 1 | DEAD-API-001 |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 1 | MERGE-001 |

### Implement-Stage Handoff

- `DEAD-API-001`
