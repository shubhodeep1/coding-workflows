## Executive Summary

- **`test_and_mark_stable` is the highest-impact bottleneck and failure source.** In `shubhodeep1/coding-workflows`, that family ran only 5 times with **40% failure** and **40% cancellation**, with **p50 4,508s** and failures at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` (`run 25474642232`, 5,395s) and `orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project` (`run 25477674617`, 3,737s). **Estimated impact:** save **20–35 minutes** on affected release-validation runs and materially raise pass rate. **Confidence:** high.

- **`workflow_log_analysis` is the clearest cost hotspot.** `summarize_unselected_runs` used **214,237 tokens** in failed run `25473131401`, **259,600** in `25470798500`, **232,690** in `25474659590`, **201,113** in `25480827754`, and **136,338** in `25477691662`, all on `openai/gpt-5.4-mini`; the same workflow family also averages **2,609s** and has a **20% failure rate**. **Estimated impact:** cut analysis cost by **30–60%** and latency by **8–15 minutes/run**. **Confidence:** high.

- **Model/tool instability caused multiple active-phase failures.** `clarify` runs `25473125487`, `25473129175`, and `25473129346` all failed after **3 Codex attempts** with `exit code 2`; `orchestrate` run `25473127144` failed the same way; `implement` run `25470900024` bailed after two “announced edit/apply_patch but produced no file changes” attempts. Deep-dive `review_autofix` runs `25490929374` and `25493479038` still showed `MODEL_EDITOR: openai/gpt-5.3-codex`, while a later recent run `25496111008` had moved to `MODEL_EDITOR: openai/gpt-5.4`. **Estimated impact:** reduce active-phase failures/reruns by **most of current Codex-attempt failures**. **Confidence:** high.

- **AI memory retrieval is emitting telemetry, but usefulness is low.** Across deep-dive logs there were **28 `retrieve` operations**, only **4 hits** (**14.3% hit rate**), with **24 zero-record retrieves** and average `estimated_tokens` of just **8.0**. Failures in `clarify` (`25473125487`) and `orchestrate` (`25473127144`) also logged fail-open warnings around missing memory helpers or semantic-cache lookup failures. **Estimated impact:** moderate quality/token improvements if retrieval precision is raised before active phases. **Confidence:** medium.

- **`review_autofix` is the dominant long-running subflow and cancellation sink.** Family metrics show **107 total runs**, **66 cancelled**, **p95 1,555.3s**. A success still took **2,483s** in `run 25490929374`; cancelled runs `25493479038` (1,564s), `25495072670` (1,362s), and `25494626684` (615s) all spent most time in `review   codex-agent (claude-branch-review)`, often on comment-only paths where editor/merge work was skipped. **Estimated impact:** save **5–20 minutes** on review-linked end-to-end paths and reduce wasted reruns. **Confidence:** high.

- **GitHub API waste is mostly redundant polling and no-op probing, not rate-limit incidents.** No sampled deep-dive run showed a 429 or secondary rate-limit failure, but `cancel_on_pr_close` `run 25496111022` still performs `/rate_limit` probing plus two `actions/runs` lookups on a no-op close event, and `test_and_mark_stable` watcher steps repeatedly poll `actions/workflows/.../runs` and `actions/runs/{id}` for minutes. **Estimated impact:** reduce GH API calls by **30–60% in hotspot paths** and lower latency variance. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1) Harden `test_and_mark_stable` dispatch/watch steps to fail fast on cancelled or orphaned child runs
- **Critical-path win**
- **Evidence:**  
  - `test_and_mark_stable` family metrics: **avg 4,250.4s**, **p50 4,508s**, **p95 5,229.6s**, **2 failures + 2 cancellations / 5 runs**.  
  - `run 25474642232` failed in `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` after **5,395s**; its watcher concluded `workflow-log-analysis run #25474659590 concluded cancelled`.  
  - `run 25477674617` failed in `orchestrate-decompose-test / Dispatch internal-orchestrate.yml with multi-issue project` after **3,737s** with `Could not locate tracking issue for run 25477674617`.
- **Root cause:** watcher logic keeps polling or hard-fails late when downstream lineage is broken, instead of entering a bounded degraded-terminal state.
- **Exact change:**  
  1. Persist child run ID and tracking issue ID immediately after dispatch.  
  2. If a pinned child run reaches `conclusion=cancelled` and no successor is found after 2–3 polls, emit a terminal diagnostic and stop waiting.  
  3. If tracking issue discovery fails, emit a structured degraded result rather than waiting on child discovery logic.
- **Estimated time savings:** **20–35 minutes** on affected release-validation runs.
- **Implementation risk:** low-medium; behavior change is confined to watcher logic.

### 2) Bound `workflow_log_analysis` scope and retry only failed sub-passes
- **Critical-path win**
- **Evidence:**  
  - Family metrics: **avg 2,609s**, **p50 3,345s**, **p95 3,417.4s**, **1 failure + 1 cancellation / 5 runs**.  
  - `run 25473131401`: `summarize_unselected_runs` processed **85/100** runs for **214,237 tokens**, then `Workflow log analysis Codex pass failed after 3 attempts with exit code 2`.  
  - `run 25470798500`: **259,600 tokens** for **94/100** runs.  
  - `run 25474659590`: **232,690 tokens** for **80/100** runs.  
  - `run 25480827754`: **201,113 tokens** for **100/100** runs.
- **Root cause:** expensive summarization runs before the Codex pass is proven healthy, and failures trigger whole-pass rework.
- **Exact change:**  
  1. Reduce `targeted` unselected-run summarization from 100 to a smaller cap (for example 25–50), preferring failures, cancellations, and high-duration outliers first.  
  2. Skip summarization entirely when enough deep-dive coverage already exists in `errors/`, `slow/`, and `recent/`.  
  3. Retry only the failed Codex analysis sub-pass, not the full summarize+analyze pipeline.
- **Estimated time savings:** **8–15 minutes/run**.
- **Implementation risk:** low if the existing `log_summary` field remains as a backstop.

### 3) Split `review_autofix` comment-only/light-review paths from full reviewer/editor paths
- **Critical-path win**
- **Evidence:**  
  - Family metrics: **107 runs**, **66 cancelled**, **p95 1,555.3s**.  
  - `run 25495072670` (1,362s cancelled) logged `reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`, but `review` still dominated runtime.  
  - `run 25494626684` (615s cancelled) and `run 25493479038` (1,564s cancelled) likewise spent most runtime in `review   codex-agent (claude-branch-review)`.
- **Root cause:** deterministic gates skip editor/merge actions, but the workflow still launches a near-full review path.
- **Exact change:**  
  1. Add an explicit light-review branch for comment-only outcomes.  
  2. For comment-only runs, cap reviewer breadth and skip deep second-pass review unless the diff/risk score requires it.  
  3. Exit earlier on superseding cancellations before launching the long-running review phase.
- **Estimated time savings:** **5–20 minutes** on affected review-linked paths.
- **Implementation risk:** medium; needs careful gate parity to avoid reducing review quality on risky diffs.

### 4) Skip full poller checkout on no-work orchestrator cycles
- **Micro-optimization with high frequency**
- **Evidence:**  
  - `orchestrate_poll` family: **31/31 success**, **avg 59.5s**, **p50 51s**, **p95 124.5s**.  
  - `run 25494806358` took **48s** and recorded `has_work=false`; log summary says `poll/Checkout repository` took ~**10s** and dominated runtime.  
  - `run 25492793996` took **52s**, with `poll` itself dominating and `push_attempts: 1`.
- **Root cause:** full repo/support-source prep happens even on no-work cycles.
- **Exact change:** query active tracking issues first; if none are present, write the ledger event and exit before repository checkout and support-file staging.
- **Estimated time savings:** **10–20s per idle poll**, which compounds over many cycles.
- **Implementation risk:** low.

### 5) Add Python dependency caching and separate fail-fast CI checks from the long monolithic `lint` job
- **Critical-path win for developer feedback; moderate wall-clock win**
- **Evidence:**  
  - `ci` family: **73 runs**, **avg 611.7s**, **p50 614s**, **p95 655.8s**.  
  - Recent successful CI runs `25495072556` (610s), `25494626619` (594s), and `25493478983` (607s) all spent almost the whole run in `lint`.  
  - Each CI run re-installs Python packages (`yamllint coverage pyyaml jsonschema jinja2`) in the main `lint` step.
  - Failed CI runs `25473514248` and `25469919488` surfaced only after ~7–8 minutes, despite many earlier checks already passing.
- **Root cause:** no dependency reuse plus a single long job delays both completion and failure visibility.
- **Exact change:**  
  1. Cache pip dependencies by Python version plus dependency manifest.  
  2. Split the most failure-prone checks into an early matrix or separate job: integration fingerprint verification, `test_implement_post_codex_recovery`, and `test_orchestrate_poll_process`.  
  3. Keep the rest of the broader lint/test suite in the slower lane.
- **Estimated time savings:** **1–3 minutes/run** on warm-cache CI, plus **7–8 minutes earlier failure feedback**.
- **Implementation risk:** medium because extra jobs may increase runner-queue exposure.

## Cost Optimizations

Ranked by expected token/dollar reduction.

### 1) Cut `workflow_log_analysis` token burn by reducing `summarize_unselected_runs` coverage
- **Evidence:**  
  - `run 25473131401`: **214,237 tokens** for **85** summarized runs.  
  - `run 25470798500`: **259,600 tokens** for **94** summarized runs.  
  - `run 25474659590`: **232,690 tokens** for **80** summarized runs.  
  - `run 25480827754`: **201,113 tokens** for **100** summarized runs.  
  - `run 25477691662`: **136,338 tokens** for **80** summarized runs.
- **Root cause:** summarizing up to 100 unselected runs is overbroad relative to the actual optimization questions.
- **Exact change:** cap unselected-run summarization to a smaller risk-based sample and reuse existing `log_summary` rows already present in `analysis_context.json` before spending more tokens.
- **Estimated savings:** **30–60%** token reduction for `workflow_log_analysis`.
- **Quality-risk notes:** low if failures/cancellations/slow outliers remain prioritized.

### 2) Finish the model rollout away from `openai/gpt-5.3-codex` on active editor/resolver paths
- **Evidence:**  
  - Failed `implement` run `25470900024` explicitly references the `gpt-5.3-codex` announce-without-emit regression and bails after two no-change attempts.  
  - Slow/cancelled `review_autofix` runs `25490929374` and `25493479038` repeatedly show `MODEL_EDITOR: openai/gpt-5.3-codex`.  
  - Later recent run `25496111008` shows `MODEL_EDITOR: openai/gpt-5.4`, indicating rollout was underway by **2026-05-07 12:35 UTC**.
- **Root cause:** older default editor model caused retries without edits.
- **Exact change:** complete rollout of `openai/gpt-5.4` (or the now-stable intended replacement) across `implement`, `review_autofix`, and `orchestrate_poll` conflict-resolver paths; keep `gpt-5.3-codex` only as opt-in fallback/canary.
- **Estimated savings:** avoid repeated failed Codex attempts and downstream reruns; moderate-to-high token savings on active-phase workflows.
- **Quality-risk notes:** low; later successful runs already use the newer default.

### 3) Reduce reviewer breadth on low-risk/comment-only `review_autofix` runs
- **Evidence:**  
  - Recent `review_autofix` success `25496111008` advertises a wide `REVIEWER_MODELS` panel.  
  - Comment-only/cancelled runs `25495072670`, `25494626684`, and `25493479038` still spent long wall time in review despite skipping editor/merge work.
- **Root cause:** broad multi-reviewer panel is invoked even when deterministic gates already narrow the task.
- **Exact change:** use a tiered reviewer policy: 1 primary reviewer on comment-only/docs/small-diff paths, full panel only for risky or conflicting diffs.
- **Estimated savings:** moderate token reduction in `review_autofix`; likely the next-largest AI cost center after log analysis.
- **Quality-risk notes:** medium; preserve full-panel escalation for risky diffs.

### 4) Reduce avoidable rerun cost from cancelled `review_autofix` and broken release watchers
- **Evidence:**  
  - `review_autofix` has **66 cancellations / 107 runs**.  
  - `test_and_mark_stable` has **0 success in the analyzed failure/cancel set** and blocks for **3,044–5,395s** when downstream logic breaks.
- **Root cause:** expensive work is started before cancellation/degradation is detected.
- **Exact change:** cancel superseded reviews earlier, and let release-test watchers fail fast on broken lineage instead of waiting.
- **Estimated savings:** moderate; reduces wasted AI work and repeated end-to-end test cycles.
- **Quality-risk notes:** low.

### 5) Make prompt-cache behavior measurable before further tuning
- **Evidence:**  
  - Many AI runs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.  
  - `review_autofix` `run 25493479038` emitted `INFO: openrouter usage phase=review_autofix_cache_probe ... cache_enabled=true`, but `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` were all `na`.  
  - No sampled deep-dive logs exposed usable prompt-cache hit/read/create counters.
- **Root cause:** cache is likely enabled, but not observable enough to tune or verify savings.
- **Exact change:** emit per-call cache read/create counters and hit/miss status in the same place as existing `openrouter usage` logs.
- **Estimated savings:** unquantified but likely moderate once cache fragmentation can be measured and fixed.
- **Quality-risk notes:** none; this is observability first.

## Reliability Improvements

Ranked by expected failure-rate/rerun-rate reduction.

### 1) Add degraded-terminal states to `test_and_mark_stable` child-workflow watchers
- **Failure evidence:**  
  - `run 25474642232` failed after **5,395s** because child `workflow-log-analysis` run `25474659590` concluded `cancelled`.  
  - `run 25477674617` failed after **3,737s** with `Could not locate tracking issue for run 25477674617`.
- **Root cause category:** orchestration/watcher state handling.
- **Exact fix:** classify `cancelled child + no successor`, `missing tracking issue`, and `stale dispatch pin` as explicit terminal outcomes with diagnostics.
- **Expected reliability impact:** highest; this directly addresses 2 of the 3 named `test_and_mark_stable` failures in the provided window.
- **Rollback / fail-open:** preserve current long-wait mode behind a flag if needed.

### 2) Remove or isolate `gpt-5.3-codex` from default active-phase execution
- **Failure evidence:**  
  - `clarify` runs `25473125487`, `25473129175`, `25473129346`: `Codex clarify failed after 3 attempts` with exit code 2.  
  - `orchestrate` run `25473127144`: `Codex orchestrate failed after 3 attempts (last_status=codex exit 2...)`.  
  - `implement` run `25470900024`: `Codex produced no actionable output 2 attempts in a row`.
- **Root cause category:** model/tool behavior regression.
- **Exact fix:** make the newer model default everywhere active edits/plans happen; leave the older model only as a non-default fallback.
- **Expected reliability impact:** high for `clarify`, `implement`, `orchestrate`, and some `review_autofix` paths.
- **Rollback / fail-open:** keep per-workflow override variables so maintainers can revert selectively.

### 3) Change `workflow_log_analysis` from full-pipeline retry to stage-local retry
- **Failure evidence:**  
  - `run 25473131401` failed in `analyze-commit-notify / Run workflow log analysis` after expensive summarization.  
  - Successful `run 25477691662` still logged `Workflow log analysis Codex pass failed after 3 attempts with exit code 2`.
- **Root cause category:** retry strategy.
- **Exact fix:** persist stage outputs and only re-run the failed Codex analysis step; skip repeating `summarize_unselected_runs` when its artifact already exists.
- **Expected reliability impact:** medium-high; fewer failures from repeated full-pipeline restarts.
- **Rollback / fail-open:** if stage artifact is absent/corrupt, fall back to current full run.

### 4) Stabilize nightly self-test fixtures and promote them into earlier CI lanes
- **Failure evidence:**  
  - `nightly_validation_selftest` `run 25474243471`: `fixtures=3 passed=1 failed=2`.
- **Root cause category:** fixture drift / validation harness regressions.
- **Exact fix:** triage the two failing fixtures, add them to earlier CI coverage, and record a consecutive-green threshold before treating nightly as authoritative.
- **Expected reliability impact:** medium.
- **Rollback / fail-open:** if nightly remains flaky, continue artifact upload but mark status as degraded instead of hard-failing dependent judgments.

### 5) Treat memory-helper absence as a tracked degraded state, not just warnings
- **Failure evidence:**  
  - `clarify` `25473125487` logged `memory_helpers.sh unavailable; using fail-open stubs`.  
  - `orchestrate` `25473127144` logged `Required memory helper scripts unavailable; skipping orchestration start memory event`.
- **Root cause category:** fail-open dependency handling.
- **Exact fix:** add a structured degraded-state marker when memory support scripts are missing, so failures can be correlated and alerted without breaking runs.
- **Expected reliability impact:** modest but important for diagnosis.
- **Rollback / fail-open:** preserve current non-blocking behavior.

## AI Memory Health

- **Telemetry presence:** good. Deep-dive logs contained **111 `AI_MEMORY_TELEMETRY` entries** across `processed-command-check`, `processed-command-claim`, `record-run-event`, `retrieve`, `summarize_unselected_runs`, and `record-candidate`.
- **Retrieve hit rate:** **4 / 28 = 14.3%** had `records_selected > 0`; **24 / 28** returned zero records.
- **Average retrieve size:** `estimated_tokens` averaged **8.0** across sampled retrieves, which is far below any practical prompt budget, so the memory layer is not adding much useful context yet.
- **`keyword_method` distribution:**  
  - `plain`: **16**  
  - `llm`: **2**  
  - `none`: **10**
- **Zero-record examples:**  
  - `clarify` `run 25473125487`: `retrieve` returned `records_selected: 0`, `keyword_method: plain`, `estimated_tokens: 0`.  
  - `orchestrate` `run 25473127144`: `retrieve` returned `records_selected: 0`, `keyword_method: llm`, `estimated_tokens: 0`.  
  - `review_autofix` `run 25490929374`: `retrieve` returned `records_selected: 0`, `keyword_method: none`, `estimated_tokens: 0`.
- **Successful retrieve example:**  
  - `implement` `run 25470900024`: `retrieve` returned **2 records** with `estimated_tokens: 56`, so the path can work.
- **`fail_open: true` telemetry entries:** none observed in telemetry JSON.
- **`enabled: false` telemetry entries:** none observed in telemetry JSON.
- **Push retry counts:** **7 telemetry writes** had `push_attempts > 1`; a concrete example is `clarify` `run 25473129346`, where the `phase_failed` event recorded `push_attempts: 2`.
- **Operational concern:** although `fail_open: true` was not present in telemetry objects, runtime logs still showed fail-open warnings such as `Semantic cache thread-history fetch failed; forcing cache bypass` and `memory_helpers.sh unavailable; using fail-open stubs` in `clarify` `25473125487`.
- **Recommendation:** keep telemetry emission as-is, but add a compact success/failure summary per run: `retrieve_hits`, `retrieve_misses`, `avg_retrieve_tokens`, and `memory_degraded=true/false`. Right now the low hit rate is visible only after log mining.

## GH API Call Audit

### 1) `test_and_mark_stable` watcher loops are the biggest API churn hotspot
- **Evidence:**  
  - `run 25474642232` `orphan-workflows-test` repeatedly called:  
    - `gh api "repos/${TEST_REPO}/actions/workflows/${WF_FILE}/runs?per_page=1"`  
    - `gh api "repos/${TEST_REPO}/actions/workflows/${WF_FILE}/runs?per_page=10"`  
    - `gh api "repos/${TEST_REPO}/actions/runs/${NEW_ID}"`  
    over a path that ultimately waited until the child run was `cancelled`.
  - `run 25477674617` `orchestrate-decompose-test` similarly polled workflow runs and then searched for a tracking issue before failing.
- **Redundancy pattern:** dispatch → discover child run by diffing lists → poll status → separately search tracking issue.
- **Concrete change:** persist child run ID and tracking issue ID as outputs/artifacts immediately on dispatch so later phases do direct `GET actions/runs/{id}` checks only.
- **Estimated call-count reduction:** likely **50%+** in these watcher steps.
- **Rate-limit risk reduction:** high, especially on long waits.

### 2) `cancel_on_pr_close` is over-querying on no-op branches
- **Evidence:** `run 25496111022` executes:  
  - `/rate_limit` probe,  
  - one `_gh_retry gh api "repos/${REPOSITORY}/actions/runs"` lookup,  
  - a second `_gh_retry gh api "repos/${REPOSITORY}/actions/runs"` lookup,  
  - then only conditionally cancels runs; in this run there were no matches.
- **Redundancy pattern:** rate-limit probing and duplicated listing even when there is nothing to cancel.
- **Concrete change:** skip `/rate_limit` until a 403/429 occurs, and collapse the two list calls into one filtered query.
- **Estimated call-count reduction:** from roughly **3 baseline calls to 1** on no-op closes.
- **Rate-limit risk reduction:** moderate.

### 3) `issue_pr_status` still mixes GraphQL batching with follow-up per-item REST lookups
- **Evidence:** `run 25496111057` uses:
  - `gh_retry gh api graphql` to discover issue numbers,
  - `gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"` for title/body,
  - `gh_retry gh api graphql` again for orchestrator metadata,
  - `gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}"` for labels/body.
- **Redundancy pattern:** batched discovery followed by per-item metadata fetches.
- **Concrete change:** extend the existing GraphQL query to return the small label/body fields now fetched via REST.
- **Estimated call-count reduction:** **2–3 calls/run** in this step.
- **Rate-limit risk reduction:** low-moderate.

### 4) `copilot_pull_request_reviewer` artifact cleanup does one list + N deletes every run
- **Evidence:** `run 25494630297` `Cleanup artifacts` calls:
  - `gh api /repos/.../actions/runs/25494630297/artifacts --jq '.artifacts[].id'`
  - then loops `gh api ... /actions/artifacts/"$artifact_id"` for deletion.
- **Redundancy pattern:** cleanup always enumerates artifacts even if upstream steps may have produced none.
- **Concrete change:** pass artifact IDs from upload steps into cleanup, and skip enumeration when no artifacts were emitted.
- **Estimated call-count reduction:** small but clean.
- **Rate-limit risk reduction:** low.

### 5) No hard rate-limit failures were observed in sampled deep dives
- **Evidence:** no sampled failing/slow/recent run in the provided window showed HTTP 429 or a GitHub secondary-rate-limit failure.
- **Implication:** the current issue is **waste and variance**, not hard limit exhaustion.
- **Recommendation:** optimize for fewer calls and less polling before adding more retry complexity.

## Prompt Cache & Memory System

- **Prompt cache enabled:** many AI workflows showed `OPENROUTER_PROMPT_CACHE_DISABLED: false`, including `clarify`, `implement`, `orchestrate_poll`, and `review_autofix`.
- **Observed prompt-cache instrumentation is insufficient:**  
  - `review_autofix` `run 25493479038` logged `INFO: openrouter usage phase=review_autofix_cache_probe ... cache_enabled=true`, but all token/cache numeric fields were `na`.  
  - No direct prompt-cache hit/read/create counters were present in sampled deep-dive AI runs.
- **Cache fragmentation risk:** `review_autofix` logs explicitly mention a split between static prompt prefix and dynamic per-PR content, which is the right design, but there is still no evidence that cache reads are actually landing.
- **Memory retrieval effectiveness:** low, based on the **14.3%** retrieve hit rate and mostly zero-token retrievals.
- **File-based workflow cache health:** mixed. In the workflow-log-analysis audit log, a prior `validate` run (`25480631539`) was called out for `Cache not found...` plus `Path Validation Error... no cache is being saved.` That suggests at least one file-cache path is misconfigured.
- **Concrete improvements:**  
  1. Emit `cache_read_input_tokens`, `cache_creation_input_tokens`, hit/miss, and fail-open status on every OpenRouter-using step.  
  2. Keep dynamic PR/issue-specific blobs at the end of prompts; keep static instruction blocks at the top, unchanged across runs.  
  3. Fix the broken validate-hints cache path so restore/save can succeed.  
  4. Only tune prompt-cache strategy after these counters exist; today savings are mostly inferential.
- **Estimated impact:**  
  - **Tokens:** medium but currently unquantified.  
  - **Latency:** low-to-moderate.  
  - **Reliability:** medium, because observable cache state will make regressions diagnosable.

## Orchestrator Health

- **Polling layer looks healthy but not cheap.** `orchestrate_poll` has **31/31 successes**, **avg 59.5s**, **p95 124.5s**. `run 25494806358` cleanly recorded `poll_started` and `poll_completed` ledger entries with `push_attempts: 1`.
- **Clarify/implement/orchestrate active phases are the unstable part.**  
  - `clarify`: **3 failures / 192 runs**, all sampled failures were Codex-attempt failures, not condition bugs.  
  - `implement`: **1 failure / 161 runs**, but the failed run was an active Codex path.  
  - `orchestrate`: **1 failure / 5 runs**, again an active Codex path.
- **Skip gating is effective but chatty.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have many 1–2 second skipped runs. That means condition guards work, but there is still orchestration overhead from many no-op invocations.
- **Operational pain point:** missing degraded terminal states. When child workflows are cancelled or tracking issues cannot be found, the system tends to fail late instead of transitioning to a diagnosable terminal status.
- **Smallest safe mitigations:**  
  1. Add explicit end states such as `child_cancelled_no_successor`, `tracking_issue_missing`, and `memory_degraded`.  
  2. Track `% active runs failing due to Codex exit 2`, `% watcher steps ending degraded-terminal`, and `% no-work poll cycles that still perform checkout`.
  3. Reduce idle poller work before considering larger orchestrator refactors.

## Pipeline Flow Bottlenecks

### 1) Queueing overhead
- **Evidence:** runner wait messages appear repeatedly in `ci`, `review_autofix`, `copilot_pull_request_reviewer`, `orchestrate_poll`, `cancel_on_pr_close`, and release workflows.
- **Impact:** adds variance across almost every family.
- **Fix:** reduce number of no-op and superseded runs; shift expensive setup after cheap gating.

### 2) Compute-heavy phases
- **Evidence:**  
  - `ci`: recurring **594–666s** runs dominated by `lint`.  
  - `review_autofix`: **615–2,483s** long-running review phases.  
  - `workflow_log_analysis`: **2,573–3,383s**.  
  - `test_and_mark_stable`: **3,044–5,395s**.
- **Impact:** these are the dominant wall-clock bottlenecks.
- **Fix:** watcher hardening, review-path slimming, CI fail-fast split, and analysis scope reduction.

### 3) Retry overhead
- **Evidence:**  
  - `clarify` and `orchestrate` both retried Codex 3 times before failing.  
  - `workflow_log_analysis` retried the Codex pass after already spending 136k–259k tokens on summarization.
- **Impact:** burns both time and tokens after low-probability recovery paths.
- **Fix:** stage-local retries and model-default cleanup.

### 4) Merge/conflict overhead
- **Evidence:** CI failures `25469919488` and `25473514248` include repeated `Integration fingerprint verification FAILED` errors before the job exits.
- **Impact:** regressions in conflict-resolution intent are detected, but only late in a long monolithic lint run.
- **Fix:** isolate fingerprint verification into an earlier fast-fail lane.

### 5) No-op API/probing overhead
- **Evidence:**  
  - `cancel_on_pr_close` no-op branch still probes `/rate_limit` and actions runs.  
  - `orchestrate_poll` no-work cycles still do repository checkout.  
  - watcher steps perform repeated child-run discovery instead of reusing IDs.
- **Impact:** small per run, large in aggregate.
- **Fix:** move cheap discovery before checkout, and persist IDs between phases.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` long failing/cancelled watchers: `25474642232`, `25477674617`.
- `workflow_log_analysis` token-heavy and slow: `25473131401`, `25470798500`, `25474659590`, `25480827754`.
- `review_autofix` long and cancellation-prone: `25490929374`, `25493479038`, `25495072670`.

**Top failure modes**
- Child workflow cancellation or missing tracking issue in release-test watcher logic.
- Codex exit-code-2 / announce-without-emit behavior in active phases.
- Nightly self-test fixture failures (`25474243471`).
- Late CI failure discovery inside the monolithic `lint` job.

**Highest-cost drivers**
- `workflow_log_analysis` summarization tokens.
- Broad/long `review_autofix` reviewer paths.
- Repeated reruns/cancellations in `review_autofix` and release-test chains.

**Top 3 prioritized actions**
1. **Fix `test_and_mark_stable` watcher lineage and terminal-state handling.**
2. **Reduce `workflow_log_analysis` summarization scope and retry only failed stages.**
3. **Complete model-default rollout off `gpt-5.3-codex` and split light-review/comment-only `review_autofix` paths.**

## Metrics Appendix

### Overall repository metrics

| Repository | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 293 | 12 | 75 | 620 | 1.2% | 140.9 | 1.0 | 635.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other/skipped | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 73 | 70 | 3 | 0 | 0 | 611.7 | 614.0 | 655.8 |
| `review_autofix` | 107 | 34 | 0 | 66 | 7 | 396.6 | 46.0 | 1555.3 |
| `test_and_mark_stable` | 5 | 1 | 2 | 2 | 0 | 4250.4 | 4508.0 | 5229.6 |
| `workflow_log_analysis` | 5 | 3 | 1 | 1 | 0 | 2609.0 | 3345.0 | 3417.4 |
| `orchestrate_poll` | 31 | 31 | 0 | 0 | 0 | 59.5 | 51.0 | 124.5 |
| `clarify` | 192 | 20 | 3 | 0 | 169 | 12.6 | 1.0 | 97.3 |
| `implement` | 161 | 16 | 1 | 6 | 138 | 24.2 | 1.0 | 238.0 |
| `orchestrate` | 5 | 4 | 1 | 0 | 0 | 147.6 | 156.0 | 171.0 |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 0 | 100.0 | 100.0 | 100.0 |
| `copilot_pull_request_reviewer` | 28 | 28 | 0 | 0 | 0 | 195.7 | 183.5 | 365.2 |

### Observed token telemetry from `workflow_log_analysis`

| Run ID | Conclusion | Summarized runs | Targeted runs | Tokens used | Model |
|---|---|---:|---:|---:|---|
| `25473131401` | failure | 85 | 100 | 214,237 | `openai/gpt-5.4-mini` |
| `25470798500` | success | 94 | 100 | 259,600 | `openai/gpt-5.4-mini` |
| `25474659590` | cancelled | 80 | 100 | 232,690 | `openai/gpt-5.4-mini` |
| `25477691662` | success | 80 | 100 | 136,338 | `openai/gpt-5.4-mini` |
| `25480827754` | success | 100 | 100 | 201,113 | `openai/gpt-5.4-mini` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total `AI_MEMORY_TELEMETRY` entries observed | 111 |
| `record-run-event` ops | 68 |
| `retrieve` ops | 28 |
| `processed-command-check` ops | 4 |
| `processed-command-claim` ops | 4 |
| `summarize_unselected_runs` ops | 6 |
| `record-candidate` ops | 1 |
| Retrieve hit rate | 14.3% (4/28) |
| Retrieve zero-record rate | 85.7% (24/28) |
| Avg `estimated_tokens` per retrieve | 8.0 |
| `keyword_method=plain` | 16 |
| `keyword_method=llm` | 2 |
| `keyword_method=none` | 10 |
| Telemetry entries with `push_attempts > 1` | 7 |
| Telemetry entries with `fail_open: true` | 0 observed |
| Telemetry entries with `enabled: false` | 0 observed |

### GH API hotspot summary

| Workflow / run | Pattern | Observable calls/signals | Main issue | Suggested reduction |
|---|---|---|---|---|
| `test_and_mark_stable` / `25474642232` | Dispatch + child-run watch loop | repeated `actions/workflows/.../runs` and `actions/runs/{id}` polling | Long wait on cancelled child | Persist child IDs; stop on degraded terminal |
| `test_and_mark_stable` / `25477674617` | Dispatch + tracking-issue search | child-run polling + tracking issue lookup | Missing tracking issue causes late failure | Persist tracking issue at dispatch |
| `cancel_on_pr_close` / `25496111022` | No-op cancel branch | `/rate_limit` + 2 `actions/runs` lookups | Over-probing on no-op runs | collapse to 1 filtered lookup; defer rate-limit probe |
| `issue_pr_status` / `25496111057` | Mixed GraphQL + REST metadata fetches | at least 2 GraphQL + 2 REST lookups visible | Batch discovery but per-item follow-ups remain | extend GraphQL payload |
| `copilot_pull_request_reviewer` / `25494630297` | Artifact cleanup | 1 artifact enumeration + N deletes | Enumeration on every run | reuse artifact IDs from upload phase |

### Prompt/cache observability summary

| Signal | Observation |
|---|---|
| OpenRouter prompt cache env | `OPENROUTER_PROMPT_CACHE_DISABLED: false` seen in sampled AI runs |
| Direct prompt-cache hit/read/create counters | Not present in sampled deep dives |
| OpenRouter cache probe logs | Present in `review_autofix` `25493479038`, but numeric cache/token fields were `na` |
| File-based cache issue | Prior `validate` run `25480631539` was reported with restore miss + save-path validation error |
| Conclusion | Cache appears enabled but is not yet measurable enough to optimize confidently |
