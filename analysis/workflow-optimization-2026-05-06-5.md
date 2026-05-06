## Executive Summary

- **Replace label-only release-test waits with workflow-run-aware waits in `test_and_mark_stable`.** In `shubhodeep1/coding-workflows`, run `25416934394` failed after `4579s` at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`, and run `25428461223` failed after `3427s` at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`; the workflow family’s failure rate is `40%` (`2/5`). **Estimated impact:** cut failed release-test time by ~`10–20 min/run` and materially reduce false timeout failures. **Confidence:** high.

- **`review_autofix` tail latency and cancellation waste are the biggest runtime tax after release tests.** The family has `99` runs with `67` cancelled and `p95=1621.4s`; in successful release-test run `25441918019`, the nested review run `25442427009` sat `pending` from `14:44:41Z` to `14:51:31Z`, then spent minutes in `Free disk space`, `Install dependencies`, and `Run reviewer models`. **Estimated impact:** save ~`7–15 min` on tail runs and reduce wasted cancelled work. **Confidence:** medium-high.

- **CI is a stable but expensive ~10-minute gate dominated by `lint`.** Sample successful runs `25444835462` (`595s`), `25444732612` (`652s`), `25444598193` (`632s`), `25443463307` (`620s`), `25442795600` (`647s`), and `25442618003` (`660s`) all show `lint` occupying nearly the full run. **Estimated impact:** return failures ~`7–9 min` earlier via job splitting/fail-fast ordering, with modest or neutral total wall-clock change. **Confidence:** high.

- **`workflow_log_analysis` is the clearest token-cost hotspot in the window.** `summarize_unselected_runs` used `255,754` tokens in run `25416954546`, `203,550` in `25428493736`, `241,574` in `25431219427`, `225,273` in `25441969004`, and `213,986` in `25405996019`—`1,140,137` total across five sampled runs. **Estimated impact:** save ~`100k–150k tokens/run` by tightening breadth and skipping trivial runs earlier. **Confidence:** high.

- **AI memory is working, but retrieval quality is uneven and write friction exists.** In the primary deep-dive sample, `retrieve` hit `2/3` times (`66.7%`) with average `estimated_tokens=37.3`; both implement failures (`25417030055`, `25417040196`) retrieved `2` records, while clarify failure `25441973385` retrieved `0`. `phase_started` memory writes needed `push_attempts=2` in both `25417030055` and `25441973385`. **Estimated impact:** moderate reliability lift from better clarify retrieval and fewer memory write retries. **Confidence:** medium.

- **Runner queue time is a recurring hidden bottleneck.** `orchestrate_poll` failure `25424218738` spent its visible lifetime repeating runner-wait messages and failed after `903s`; short utility workflows such as `forward_merge_stable_to_main`, `issue_pr_status`, and `cancel_on_pr_close` also show runner wait dominating. **Estimated impact:** small per run, but meaningful aggregate savings by avoiding unnecessary dispatches and adding earlier closed/no-op exits. **Confidence:** high.

## Speed Optimizations

Ranked by expected latency reduction.

### 1. Make `test_and_mark_stable` waits run-aware, not label-only
**Critical-path win**

- **Evidence:**  
  - Run `25416934394` failed after `4579s` at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`. Its wait step observed labels progress from `none` → `ai:clarification` → `ai:planning` → `ai:awaiting-approval` → `ai:implementing`, then stayed on `ai:implementing` for dozens of polls until timeout.  
  - Run `25428461223` failed after `3427s` at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`.  
  - The workflow family `test_and_mark_stable` has `5` runs, `2` failures, `p50=3531s`, `p95=4461.2s`.
- **Root cause:** Polling relies too heavily on labels/comments and only secondarily on workflow state, so “stuck in intermediate label” and “replacement/successor run” scenarios consume the full timeout budget.
- **Exact change:**  
  1. As soon as clarify/plan/implement/review dispatch is detected, latch onto concrete `run_id`s and poll those runs directly.  
  2. If label remains `ai:implementing` but no implement run exists, or the run is absent/cancelled for `N` consecutive polls, fail early with a diagnostic instead of burning the whole timeout.  
  3. Use adaptive polling: keep `10–20s` early, then back off to `30–60s` after state is unchanged for 2–3 minutes.  
  4. Cache issue labels/comments/PR head SHA inside each poll iteration so the same cycle does not re-fetch unchanged state multiple times.
- **Estimated time savings:** ~`10–20 min` on failed release-test runs; ~`2–6 min` on successful runs by reducing unnecessary poll cycles.
- **Implementation risk:** low-medium. Logic is localized to wait steps; preserve existing timeout as a fallback during rollout.

### 2. Add a true fast-exit path before heavy `review_autofix` setup and reviewer execution
**Critical-path win**

- **Evidence:**  
  - `review_autofix` family: `99` total, `67` cancelled, `p95=1621.4s`.  
  - In successful release-test run `25441918019`, Phase 4 watched review run `25442427009` remain `pending` until `14:51:31Z`, then spend long stretches in `Free disk space`, `Install dependencies`, and `Run reviewer models` before completion.  
  - Run-row evidence for cancelled run `25444598317` shows it ran `1011s` and still had `PR_CLOSED: true`, meaning expensive work survived into a closed-PR state.
- **Root cause:** Review jobs are often allowed to reach full setup/model execution before the workflow re-checks whether the PR is still open, whether the head SHA changed, or whether the path is comment-only.
- **Exact change:**  
  1. Re-check `PR_CLOSED`, branch head SHA, and comment-only/Claude-branch gates immediately after runner start and again immediately before `Free disk space` / dependency install.  
  2. If the run is superseded or the PR is closed, exit cleanly before reviewer setup.  
  3. Keep the existing successor-run pin advancement logic, which appears to have fixed a prior bait-SHA cancellation bug in the current successful smoke run.
- **Estimated time savings:** ~`7–15 min` on cancelled/superseded review runs; smaller but real tail reduction on successful runs.
- **Implementation risk:** low. This is a safe fail-open check that only exits when the run is definitely obsolete.

### 3. Split CI into a fast-fail test tier ahead of the full `lint` gate
**Critical-path win for developer feedback**

- **Evidence:**  
  - CI family: `77` runs, `72` success, `5` failures, `avg=604.7s`, `p50=613s`, `p95=653.4s`.  
  - Successful runs `25444835462` (`595s`), `25444732612` (`652s`), `25444598193` (`632s`), `25443463307` (`620s`), `25442795600` (`647s`) all show `lint` dominating.  
  - Failures often surface late: `25424602678` and `25424891815` ended with `136 passed, 21 failed, 157 total` in `tests/test_orchestrate_poll_process.py`; `25425170301` and `25425264723` failed prompt-contract tests after earlier suites had passed; `25425830472` failed after later clarify-loop/prompt-contract work.
- **Root cause:** High-signal regression suites and lower-risk coverage/tests are serialized in one long job.
- **Exact change:**  
  1. Create a small “fast-fail” CI job for `tests/test_orchestrate_poll_process.py`, prompt-contract tests, and clarify loop guard tests.  
  2. Run the broader `lint`/coverage job in parallel or only after fast-fail passes.  
  3. Stop downstream work on fast-fail failure.
- **Estimated time savings:** little change to total compute, but failures arrive ~`7–9 min` earlier.
- **Implementation risk:** low. This is a workflow decomposition change, not a behavior change.

### 4. Reduce review setup overhead in `review_autofix`
**Secondary but worthwhile**

- **Evidence:** In review run `25442427009` observed from `25441918019`, setup steps `Free disk space` and `Install dependencies` together consumed several minutes before model work began.
- **Root cause:** Large generic setup runs even on comment-only or short-lived review paths.
- **Exact change:**  
  - Skip disk cleanup and dependency-heavy prep when the job is in reviewer-panel/comment-only mode and no editor/apply path will run.  
  - Reuse existing workspace state where the workflow already knows it will not mutate code.
- **Estimated time savings:** ~`2–4 min` on reviewer-only paths.  
- **Implementation risk:** medium. Needs careful verification that reviewer containers/tools still have enough space.

### 5. Avoid checkout-heavy no-work `orchestrate_poll` cycles
**Micro-optimization**

- **Evidence:** Recent successful `orchestrate_poll` run `25445236438` took `53s`; `Checkout repository` alone took about `9s`, and the run ended with `has_work=false`.
- **Root cause:** Full repo checkout happens even for empty poll cycles.
- **Exact change:** Move `Find active tracking issues` before repository checkout when possible; only checkout if there is work or if local scripts truly require repo state.
- **Estimated time savings:** ~`8–10s` per empty poll cycle.
- **Implementation risk:** low-medium. Depends on which scripts currently require a checkout.

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1. Tighten `workflow_log_analysis` summarization breadth
- **Evidence:**  
  - Run `25416954546`: `summarized=84/100`, `skipped_empty_logs=16`, `tokens_used=255754`.  
  - Run `25428493736`: `79/100`, `21` empty, `203550` tokens.  
  - Run `25431219427`: `100/100`, `241574` tokens.  
  - Run `25441969004`: `95/100`, `225273` tokens.  
  - Run `25405996019`: `96/100`, `213986` tokens.
- **Root cause:** The summarizer spends model budget on large samples of unselected runs, including many trivial or low-yield runs.
- **Exact change:**  
  1. Lower the unselected-run target from `100` to a smaller adaptive cap.  
  2. Exclude skipped/no-op runs before log download/summarization whenever their metadata already proves low value.  
  3. Deduplicate near-identical short runs by family + outcome + similar duration before summarization.
- **Estimated savings:** ~`100k–150k tokens/run`; sampled mean today is `228,027` tokens/run.
- **Quality-risk notes:** low if errors/slow/recent deep dives remain unchanged; medium if the cap is cut too aggressively. Start with a modest reduction.

### 2. Narrow the expensive reviewer panel and two-pass review for low-risk review paths
- **Evidence:**  
  - Review-related summaries show `REVIEWER_MODELS: minimax/minimax-m2.5 moonshotai/kimi-k2.5 deepseek/deepseek-v4-pro z-ai/glm-5 qwen/qwen3.6-plus x-ai/grok-4.1-fast`.  
  - `ENABLE_REVIEWER_TWO_PASS: true` is active in the same review flow.  
  - Cancelled run `25444598317` still entered the reviewer-panel/comment-only path before cancellation.
- **Root cause:** The same heavy reviewer configuration appears to run even on smaller, comment-only, or short-lived review paths.
- **Exact change:**  
  - For comment-only/Claude-branch review paths and small diffs, use 1–2 reviewers and disable second pass.  
  - Keep the full multi-reviewer/two-pass path only for larger or riskier diffs.
- **Estimated savings:** inference: likely `50–80%` reviewer-model cost on comment-only review runs, plus lower tail latency.
- **Quality-risk notes:** medium. Protect quality by keeping the full path for larger diffs or when judge confidence is low.

### 3. Eliminate avoidable `review_autofix` cancellations before model execution
- **Evidence:** `review_autofix` has `67` cancelled runs out of `99`; cancelled run `25444598317` lasted `1011s`.
- **Root cause:** Obsolete runs survive long enough to incur setup and likely model-related cost.
- **Exact change:** Add closed-PR/head-SHA supersession checks before reviewer setup and before each expensive model batch.
- **Estimated savings:** inference: substantial on high-churn PRs; saves both machine time and reviewer-model spend.
- **Quality-risk notes:** low if cancellation only happens on definitively obsolete runs.

### 4. Reduce repeated prompt/context expansion in implement/clarify retries
- **Evidence:**  
  - Implement failures `25417030055` and `25417040196` both retrieved only `56` estimated memory tokens yet still bailed after “`2 consecutive attempts with no actionable output`”.  
  - Clarify failure `25441973385` retried to “`Codex clarify failed after 3 attempts`” despite `retrieve.records_selected=0`.
- **Root cause:** When the failure mode is structural (no-actionable-output, lookup failure, or missing context), retries likely replay similar large prompts with little marginal value.
- **Exact change:**  
  - On first detection of announced-edit-without-changes or zero-hit clarify retrieval plus lookup failure, switch to a compact fallback prompt or short-circuit with a targeted failure classification instead of repeating the full prompt.
- **Estimated savings:** moderate on failing AI-phase runs; higher if failures cluster.
- **Quality-risk notes:** medium. Keep one fallback attempt, but avoid repeating the same full-context prompt three times.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Make release-test waits resilient to missing/stuck successor runs
- **Failure evidence:**  
  - `test_and_mark_stable` failure rate is `40%` (`2/5`).  
  - Run `25416934394` timed out while stuck on `ai:implementing`; run `25428461223` timed out waiting for review completion.
- **Root cause category:** orchestration/wait-state detection.
- **Exact fix:**  
  - Track concrete phase run IDs once known.  
  - Treat “label advanced but expected workflow run never appears” as an explicit failure mode.  
  - Keep the current pin-advance logic for cancelled bait-triggered review runs and add equivalent logic for earlier phases where needed.
- **Expected reliability impact:** high for the release pipeline; this directly targets the family with the highest observed failure rate in the window.
- **Rollback/fail-open:** keep current long timeout as a fallback during the first rollout; emit diagnostics rather than silently passing.

### 2. Reclassify implement “no actionable output” earlier and route it explicitly
- **Failure evidence:**  
  - Implement runs `25417030055` and `25417040196` both failed at `Run Codex implementation`.  
  - Their logs explicitly show `Codex produced no actionable output ... agent loop is stuck in exploration` and `Codex bailed: 2 consecutive attempts with no actionable output`.
- **Root cause category:** agent-loop failure / retry policy.
- **Exact fix:**  
  - When the first attempt yields announced edits without file changes or equivalent no-op behavior, mark the run as `stuck_in_exploration` and route to a concise remediation path instead of burning a second near-identical attempt.  
  - Attach the compact diagnostic to the issue/run summary for operator clarity.
- **Expected reliability impact:** medium. The family-level failure rate is low (`2/162`), but this removes a clear repeated failure mode.
- **Rollback/fail-open:** keep the second attempt behind a feature flag until classification is proven accurate.

### 3. Stabilize the CI prompt-contract and orchestrate-poll test cluster
- **Failure evidence:**  
  - `25424602678` and `25424891815` failed `Orchestrate poll process unit tests` with `136 passed, 21 failed, 157 total`.  
  - `25425170301` and `25425264723` later showed `157 passed, 0 failed, 157 total` but still failed `test_audit_gate_prompt_contract_forbids_unsatisfiable_wording` and `test_audit_gate_prompt_contract_requires_concrete_supported_remediation`.  
  - `25425830472` failed `Clarify loop guard unit tests` with an `AssertionError` in `tests/test_plan_clarify_blocked_output.py`.
- **Root cause category:** regression churn in prompt/test contracts.
- **Exact fix:**  
  - Promote these suites into a dedicated fast-fail job and require green before merging workflow/prompt changes.  
  - For prompt-contract tests, snapshot the expected contract text more narrowly so unrelated edits do not cause broad churn.
- **Expected reliability impact:** medium. This reduces CI reruns and broken-main risk from prompt/contract regressions.
- **Rollback/fail-open:** none needed; this is test/workflow structure only.

### 4. Harden clarify when retrieval is empty and semantic lookup fails
- **Failure evidence:**  
  - Clarify run `25441973385` had `retrieve.records_selected=0`, `estimated_tokens=0`, `keyword_method=plain`.  
  - The same run logged `Semantic cache lookup command failed; continuing with live Codex run`, then `Codex clarify failed after 3 attempts`.
- **Root cause category:** missing context / fallback behavior.
- **Exact fix:**  
  - When clarify has zero memory hits plus lookup failure, switch to a smaller deterministic clarify prompt or fail fast with a targeted operator/user-visible reason.  
  - Add a metric for “clarify zero-hit retrieve + lookup_command_failed”.
- **Expected reliability impact:** medium for clarify failures; low overall because clarify failures are rare (`1/194`), but the failure mode is identifiable.
- **Rollback/fail-open:** keep the live Codex fallback available behind a threshold.

### 5. Surface nightly self-test fixture failures earlier and avoid silent follow-on commits
- **Failure evidence:** Nightly validation self-test run `25414664546` reported `fixtures=3 passed=1 failed=2`, then updated `analysis/validation-selftest-status.json` and created commit `b8486aa`.
- **Root cause category:** validation visibility / failure handling.
- **Exact fix:**  
  - Upload the failing fixture summary as the primary artifact and include failing fixture names in the workflow summary before any status-file commit.  
  - Consider gating the status-file commit on successful artifact generation and summary emission.
- **Expected reliability impact:** low-medium. Mainly improves debuggability and prevents ambiguous failed-nightly states.
- **Rollback/fail-open:** status-file commit behavior can remain, but only after failure context is safely emitted.

## AI Memory Health

- **Telemetry coverage:** Primary deep-dive logs contained `19` non-analysis `AI_MEMORY_TELEMETRY` entries in this window. Operation mix: `record-run-event=10`, `retrieve=3`, `compact=2`, `processed-command-check=2`, `processed-command-claim=2`.
- **Retrieve hit rate:** `2/3` retrieves had `records_selected > 0` (`66.7%`).
- **Average retrieval size:** `estimated_tokens` averaged `37.3` across those 3 retrieves.
- **Keyword method distribution:** `plain=100%`; no primary-run `llm` or `none` retrieve events were observed.
- **Zero-hit retrieves:** Clarify run `25441973385` returned `records_selected=0`, `estimated_tokens=0`, `keyword_method=plain`, then failed after three Codex attempts.
- **Positive-hit retrieves:** Implement runs `25417030055` and `25417040196` each selected `2` records with `estimated_tokens=56`.
- **Push retry friction:** `phase_started` write events needed `push_attempts=2` in implement run `25417030055` and clarify run `25441973385`; `phase_failed` writes in those runs completed with `push_attempts=1`.
- **Compaction health:** Memory maintenance run `25444973512` compacted month `2026-04`, archived `2914` candidate records, removed `0`, and pushed successfully on the first attempt.
- **Fail-open / disabled flags:** No primary-run retrieve entries with `fail_open: true` or `enabled: false` were found in the sampled deep-dive logs.
- **Gap:** Telemetry exists but is not ubiquitous across all successful deep-dive runs, so memory health reporting is still partial.

**Recommendations**
1. Track a dedicated KPI for clarify retrieve misses (`records_selected=0`) and alert if the miss rate exceeds implement’s miss rate by a wide margin.
2. Track memory push retry rate (`push_attempts > 1`) by workflow; the current sample shows mild write friction in both clarify and implement starts.
3. Ensure more successful runs emit retrieve/write telemetry consistently so health can be measured from primary logs rather than inferred from a small sample.

## GH API Call Audit

### Highest-volume pattern: release-test polling in `test_and_mark_stable`
- **Evidence:**  
  - `25416934394` alt-model wait polled labels every ~`20s` while stuck on `ai:implementing`.  
  - `25428461223` smoke-test wait logic polls labels, comments, plan run status, action runs, and review state repeatedly; the step text shows repeated use of `gh api "repos/${REPO}/actions/runs/${RID}"`, issue label reads, and related run probes.  
  - Successful run `25441918019` Phase 4 also repeatedly polled review run state and jobs for nested run `25442427009`.
- **Observed redundancy:** Multiple API reads per poll loop, often against unchanged state.
- **Recommendation:**  
  1. Cache labels/comments/PR head/run state inside each loop iteration.  
  2. Move from fixed-frequency polling to adaptive backoff after state stops changing.  
  3. Prefer one workflow-run fetch plus one jobs fetch only when run status changed or when diagnosing in-progress hangs.
- **Estimated reduction:** inference: likely `40–70%` fewer GH API calls in long waits, plus lower rate-limit risk.

### `issue_pr_status` mixes useful GraphQL batching with per-item follow-up REST calls
- **Evidence:** Recent run `25445377577` used `gh api graphql` to fetch issue numbers, then per-item calls such as `gh api "repos/.../pulls/${PR_NUMBER}"`, `gh api -X POST "repos/.../issues/${issue_number}/labels"`, `gh api "repos/.../issues/${_orch_num}"`, and `gh issue close`.
- **Observed redundancy:** Batched discovery is followed by per-issue metadata/label/close operations, which grows linearly with linked issues.
- **Recommendation:**  
  - Extend the initial GraphQL query to return more of the issue metadata/labels now fetched per-item.  
  - Reuse the fetched PR metadata across the whole step rather than re-reading it for each linked issue path.
- **Estimated reduction:** inference: `30–50%` API-call reduction on PRs with multiple linked issues.
- **Rate-limit risk reduction:** medium.

### `cancel_on_pr_close` is comparatively hygienic already
- **Evidence:** Recent run `25445377559` shows:  
  - bounded `_gh_retry`,  
  - `gh api -i /rate_limit` only inside retry support,  
  - separate filtered calls for `status=queued` and `status=in_progress`,  
  - ID dedupe via `awk '!seen[$0]++'`,  
  - no matching runs found for the sample PR branch.
- **Assessment:** Good API hygiene; this is low priority.
- **Recommendation:** Only minor tuning if GitHub API semantics allow merging queued/in-progress discovery into a single reliable query.
- **Estimated reduction:** small.

### `orchestrate_poll` is reasonably bounded, but empty cycles still do work
- **Evidence:** `25445236438` uses `_gh_retry`, rate-limit handling, and a single `gh issue list` to discover active tracking issues, then reports `has_work=false`.
- **Observed redundancy:** API footprint is not the issue; checkout overhead is larger than API overhead on empty cycles.
- **Recommendation:** Optimize checkout before further API tuning.
- **Estimated reduction:** low API savings, more meaningful time savings.

### Cross-reference to repo API hygiene
- **Repo-local standard surfaced in logs:** Implement flow logs explicitly include guidance to “prefer batched GraphQL over per-item REST” for multi-item issue/PR/comment/label queries. Current `issue_pr_status` partially follows that standard; release-test polling does not.
- **Recommendation:** Apply that repo rule consistently first in `test_and_mark_stable` waits and issue/PR status sync.

## Prompt Cache & Memory System

### Prompt cache state
- **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` is visible in recent `orchestrate_poll` run `25445236438` and in review-related summaries for the same repo window.
- **Assessment:** Prompt caching is enabled in configuration.

### Main problem: cache effectiveness is not measurable from primary logs
- **Evidence:** No direct prompt-cache create/read counters were present in the primary deep-dive runs reviewed here. This means the window does not show concrete cache hit/miss rates or token deltas from cache reads.
- **Impact:** Cache cannot be optimized confidently because effectiveness is effectively unaudited in the primary logs.

### Cache fragmentation risks
- **Evidence:** Implement logs include prompt instructions emphasizing preserving stable prefixes for cache hits, which implies prompt-prefix stability matters to this system. The same flows also inject dynamic run-specific context, warnings, and diagnostics.
- **Inference:** Dynamic noise placed before the stable prompt body is likely fragmenting cache prefixes.
- **Recommendation:**  
  1. Keep the stable system/template prefix first.  
  2. Move highly variable run metadata, timestamps, and diagnostics to the tail or into structured attachments/side inputs where possible.  
  3. Avoid retry prompts that prepend new dynamic text ahead of the reusable prefix.

### Memory retrieval effectiveness
- **Evidence:** Primary retrieve sample is mixed: implement hit twice (`2` records each, `56` estimated tokens), clarify missed once (`0` records).
- **Recommendation:**  
  - Tune clarify retrieval keywords/query construction separately from implement.  
  - Compare clarify miss rate vs implement miss rate weekly.

### Cache-restore path reliability issue
- **Evidence:** Recent `issue_pr_status` run `25445377577` contains a workflow note saying clarify run `25441973385` failed because `npm install -g @openai/codex@v0.114.0 --include=optional` skipped the platform-specific dependency `@openai/codex-linux-x64`, and further notes that `plan.yml` and `workflow-log-analysis.yml` still restore Codex from cache using `npm install -g "<runner.tool_cache>/codex/package" ...`, with the cache storing only the `@openai/codex` package directory, not the platform tarball.
- **Recommendation:**  
  - Fix the cache-hit restore path so it restores the full platform-specific artifact set, not only the package directory.  
  - Verify both cache-hit and cache-miss installs with `codex --version` in `plan` and `workflow_log_analysis`.
- **Estimated impact:** improves both speed and reliability on cache hits; likely removes a class of “CLI installed but unusable” failures.
- **Risk:** low.

## Orchestrator Health

- **Healthy signals:**  
  - Recent `orchestrate_poll` run `25445236438` completed successfully in `53s`, recorded both `poll_started` and `poll_completed` memory events, and ended with `has_work=false`.  
  - Many `clarify`, `plan`, and `implement` runs are intentionally skipped quickly (`p50=1s` for each of those families, with large `other` counts), so their low p50s reflect gating/no-op behavior, not hidden compute cost.
- **Operational pain points:**  
  1. **Poll-based handoff detection** remains brittle in the release-test harness.  
  2. **Successor/cancellation complexity** is concentrated in review handoffs; the current Phase 4 logic in successful run `25441918019` shows this is being actively managed.  
  3. **Queueing noise** distorts orchestration health: `orchestrate_poll` failure `25424218738` appears to be runner-allocation failure, not orchestration logic failure.  
  4. **Clarify fallback quality** is weaker than implement’s in the observed memory sample.
- **Smallest safe mitigations:**  
  - Keep successor-run detection logic that advances from `BAIT_SHA` to the new PR head after cancellation.  
  - Add “no concrete run exists for expected phase” as a first-class orchestrator state instead of letting label waits consume full timeouts.  
  - Add explicit counters for `stuck_same_label_polls`, `review_run_replacements`, `retrieve_zero_hit`, and `memory_push_attempts_gt_1`.
- **Observable indicators to track:**  
  1. `% of waits with unchanged label for >5 min`  
  2. `% of review runs cancelled and replaced`  
  3. `% of clarify retrieves with `records_selected=0``  
  4. `% of memory writes with `push_attempts>1``  
  5. runner-pending time before first meaningful step  
  6. obsolete review runs exiting before reviewer setup

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Implement
- **Observed bottleneck:** usually not raw compute. Family p50s are `1s` because most runs are skipped/gated; when real failures happen, the issue is retry quality, not runtime.
- **Evidence:**  
  - Implement failures `25417030055` and `25417040196` died quickly (`130–137s`) on no-actionable-output.  
  - Clarify failure `25441973385` died in `122s` after 3 Codex attempts and a zero-hit retrieve.
- **Fix order:** improve failure classification and fallback behavior before trying to optimize runtime.

### 2. Review / Autofix
- **Observed bottleneck:** dominant end-to-end compute and queueing stage.
- **Evidence:**  
  - `review_autofix` family `p95=1621.4s`, `67/99` cancelled.  
  - In successful smoke run `25441918019`, the nested review run `25442427009` spent minutes `pending`, then long setup and reviewer execution.
- **Fix order:** obsolete-run fast exit, lighter reviewer path for low-risk cases, then setup trimming.

### 3. Validate / Release harness
- **Observed bottleneck:** long polling loops and state-detection overhead.
- **Evidence:** `test_and_mark_stable` `p50=3531s`, `p95=4461.2s`, `40%` failure.
- **Fix order:** run-aware waits first; API reduction second.

### 4. CI gate
- **Observed bottleneck:** consistent ~10-minute serialized test gate.
- **Evidence:** CI family `avg=604.7s`, `p50=613s`; many recent runs cluster around `595–660s`.
- **Fix order:** fast-fail tier first, then optional parallelization.

### 5. Queueing overhead
- **Observed bottleneck:** short workflows spend a large share of runtime waiting for a runner.
- **Evidence:** `25424218738` effectively spent its run waiting for a hosted runner; recent short utility runs also mention hosted-runner wait.
- **Fix order:** avoid dispatching unnecessary work; add early no-op/closed checks before expensive jobs.

### 6. Merge/conflict/cancellation overhead
- **Observed bottleneck:** successor review runs replace bait-triggered runs after editor pushes.
- **Evidence:** successful Phase 4 log in `25441918019` contains explicit pin-advance logic for cancelled predecessor review runs and successor polling.
- **Fix order:** preserve current fix and add metrics so regressions are obvious.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Release-test harness waits in `test_and_mark_stable` (`25416934394`, `25428461223`; family `p50=3531s`, `p95=4461.2s`).
- `review_autofix` tail latency and cancellation waste (`67/99` cancelled, `p95=1621.4s`).
- CI `lint` gate clustering at ~`10 min` (`595–660s` samples).

**Top failure modes**
- Label-based wait loops timing out without robust run-state reconciliation.
- Implement no-actionable-output / stuck-in-exploration failures (`25417030055`, `25417040196`).
- Clarify fallback failure after zero-hit retrieve and lookup failure (`25441973385`).
- Prompt-contract/orchestrate-poll CI regressions (`25424602678`, `25424891815`, `25425170301`, `25425264723`, `25425830472`).

**Highest-cost drivers**
- `workflow_log_analysis` summarization (`203k–256k` tokens/run in sampled runs).
- Multi-reviewer, two-pass `review_autofix`.
- Cancelled review runs that survive deep into setup/execution.

**Top 3 prioritized actions**
1. **Refactor `test_and_mark_stable` waits around concrete workflow run IDs and adaptive polling.**
2. **Add obsolete-run fast exits plus a lighter reviewer path in `review_autofix`.**
3. **Cap `workflow_log_analysis` summarization breadth and split CI into a fast-fail test tier.**

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 303 | 12 | 74 | 611 | 1.2% | 140.6 | 2.0 | 643.1 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 5 | 3 | 2 | 0 | 0 | 40.0% | 3672.0 | 3531.0 | 4461.2 |
| `review_autofix` | 99 | 30 | 0 | 67 | 2 | 0.0% | 411.4 | 62.0 | 1621.4 |
| `ci` | 77 | 72 | 5 | 0 | 0 | 6.5% | 604.7 | 613.0 | 653.4 |
| `workflow_log_analysis` | 5 | 5 | 0 | 0 | 0 | 0.0% | 2742.6 | 2700.0 | 3045.8 |
| `orchestrate_poll` | 32 | 31 | 1 | 0 | 0 | 3.1% | 88.3 | 52.0 | 163.8 |
| `clarify` | 194 | 24 | 1 | 0 | 169 | 0.5% | 14.0 | 1.0 | 99.7 |
| `implement` | 162 | 18 | 2 | 7 | 135 | 1.2% | 24.5 | 1.0 | 193.2 |
| `plan` | 163 | 20 | 0 | 0 | 143 | 0.0% | 11.0 | 1.0 | 124.9 |

### Sample CI runtime evidence

| Run ID | Workflow | Duration (s) | Dominant step / note |
|---|---|---:|---|
| `25444835462` | `CI` | 595 | `lint` dominated |
| `25444598193` | `CI` | 632 | `lint` dominated |
| `25443463307` | `CI` | 620 | `lint` dominated |
| `25442795600` | `CI` | 647 | `lint` dominated |
| `25442618003` | `CI` | 660 | `lint` dominated |
| `25444732612` | `CI` | 652 | `lint` dominated |

### `workflow_log_analysis` token telemetry

| Run ID | Summarized / Targeted | Skipped empty logs | Tokens used |
|---|---:|---:|---:|
| `25416954546` | 84 / 100 | 16 | 255,754 |
| `25428493736` | 79 / 100 | 21 | 203,550 |
| `25431219427` | 100 / 100 | 0 | 241,574 |
| `25441969004` | 95 / 100 | 5 | 225,273 |
| `25405996019` | 96 / 100 | 4 | 213,986 |
| **Total** | — | — | **1,140,137** |
| **Average** | — | — | **228,027** |

### AI memory telemetry summary (primary deep-dive runs only)

| Metric | Value |
|---|---:|
| Total primary telemetry entries | 19 |
| `record-run-event` | 10 |
| `retrieve` | 3 |
| `compact` | 2 |
| `processed-command-check` | 2 |
| `processed-command-claim` | 2 |
| Retrieve hit rate | 66.7% (2/3) |
| Avg retrieve estimated tokens | 37.3 |
| Retrieve keyword method distribution | `plain`: 3 |
| Retrieve zero-hit sample | `25441973385` (`clarify`) |
| Retrieve hit samples | `25417030055`, `25417040196` (`implement`) |
| Push retry sample (`push_attempts=2`) | `25417030055`, `25441973385` |
| Latest compaction sample | `25444973512`: archived `2914` candidates |

### GH API hotspot summary

| Workflow / step | Observed pattern | Evidence | Optimization priority |
|---|---|---|---|
| `test_and_mark_stable` wait steps | Repeated polling of labels/comments/action runs/jobs | `25416934394`, `25428461223`, `25441918019` | High |
| `review_autofix` wait logic inside release test | Repeated polling of review run status/jobs while pending/in-progress | `25441918019` Phase 4 watching run `25442427009` | High |
| `issue_pr_status` linked-issue sync | GraphQL discovery plus per-issue REST updates/closes | `25445377577` | Medium |
| `cancel_on_pr_close` | Bounded filtered queries, dedupe, retry wrapper | `25445377559` | Low |
| `orchestrate_poll` | Single issue-list query with retry wrapper; checkout heavier than API cost | `25445236438` | Low |

### Reliability incident table

| Run ID | Workflow family | Failure point | Key evidence |
|---|---|---|---|
| `25416934394` | `test_and_mark_stable` | `Wait for clarify→plan→implement (alt-model)` | Stuck on `ai:implementing` until timeout |
| `25428461223` | `test_and_mark_stable` | `Phase 4: Wait for review & autofix to complete` | Review wait timeout |
| `25417030055` | `implement` | `Run Codex implementation` | No-actionable-output / stuck-in-exploration |
| `25417040196` | `implement` | `Run Codex implementation` | No-actionable-output / stuck-in-exploration |
| `25441973385` | `clarify` | `Run Codex` | Zero-hit retrieve, lookup failure, then 3-attempt clarify failure |
| `25414664546` | `nightly_validation_selftest` | `Run validation self-test matrix` | `fixtures=3 passed=1 failed=2` |
| `25424602678` | `ci` | `Orchestrate poll process unit tests` | `136 passed, 21 failed, 157 total` |
| `25425170301` | `ci` | `Validate process cross-cycle escalation unit tests` | prompt-contract failures |
| `25425830472` | `ci` | `Clarify loop guard unit tests` | `AssertionError` in blocked-output test |

## Deep Audit — Workflows & Scripts (2026-05-06)

### Section 1: Bug & Correctness Sweep

#### Finding 1
- **ID** — `BUG-001`
- **File path** — `.github/workflows/issue_pr_status.yml:399-445`; `scripts/memory_helpers.sh:216-224`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — `issue_pr_status.yml` runs lineage finalization under `set -euo pipefail` and exits hard when memory helpers are unavailable (`issue_pr_status.yml:412-419`). It then calls `memory_finalize_task` without any fail-open guard (`issue_pr_status.yml:435-444`). In `scripts/memory_helpers.sh`, `memory_finalize_task()` directly executes `ai_memory.py finalize-task` and returns its exit status unchanged (`memory_helpers.sh:216-224`), unlike `memory_record_run_event`, `memory_record_candidate`, `memory_retrieve`, and `memory_processed_command_complete`, which explicitly log warnings and return success on failure. That means a transient AI-memory error can fail the PR-close/PR-merge sync workflow even though the repo contract says memory errors must not fail workflows.
- **Recommended fix** — Make `memory_finalize_task()` mirror the existing fail-open wrappers in `scripts/memory_helpers.sh` by catching non-zero exits, emitting `AI_MEMORY_TELEMETRY`, logging a warning, and returning success. In `issue_pr_status.yml`, downgrade missing helper-script cases to warnings and continue, matching the fail-open pattern already used in `scripts/orchestrate_poll_process.sh:439-440`.

#### Finding 2
- **ID** — `BUG-002`
- **File path** — `scripts/tg_helpers.sh:118-128`; `.github/workflows/issue_pr_status.yml:531-537`; `.github/workflows/update_workflows.yml:451-457`; `.github/workflows/implement.yml:2337-2340`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — Telegram sends are treated as successful on HTTP 4xx/5xx because the curl invocations do not use `--fail`/`--fail-with-body`. In `scripts/tg_helpers.sh`, `tg_send_msg()` only warns when curl itself exits non-zero, but a 401/403/429 response still yields exit code 0 and returns an empty `.result.message_id` silently (`scripts/tg_helpers.sh:118-128`). The raw workflow fallbacks in `issue_pr_status.yml`, `update_workflows.yml`, and `implement.yml` have the same silent-failure behavior (`curl -s/-sS ... || echo "::warning::..."`), so alert delivery can fail without any warning or retry.
- **Recommended fix** — Route all Telegram sends through `scripts/tg_helpers.sh` and harden that helper to use `curl --fail-with-body -sS`, then validate the response with `jq -e '.ok == true and .result.message_id != null'`. Remove the raw curl fallbacks from workflows once the helper is reliable.

#### Finding 3
- **ID** — `SEC-001`
- **File path** — `scripts/tg_helpers.sh:119-123`; `.github/workflows/issue_pr_status.yml:534-537`; `.github/workflows/update_workflows.yml:454-457`; `.github/workflows/implement.yml:2337-2340`
- **Severity** — Medium
- **Category tag** — `security`
- **Description** — The Telegram bot token is embedded directly in the curl URL (`https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage`) in both the shared helper and multiple workflow fallbacks. Even when logs mask secrets, argv-based secret exposure still leaves the token visible to other processes on the same runner via process inspection. This repo runs untrusted/generated code paths in several workflows, so keeping secrets out of process arguments matters.
- **Recommended fix** — Centralize Telegram delivery in one helper and move the token out of argv. A practical repo-local fix is to switch `scripts/tg_helpers.sh` to a short Python sender or a curl `--config`/stdin-fed request so the token is read from environment/config rather than appearing in the process command line. Then delete the workflow-local raw curl fallbacks.

#### Finding 4
- **ID** — `SHELL-001`
- **File path** — `scripts/validate_changed_files_syntax.sh:70-74`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — The denylist `case` arm contains broader patterns before narrower ones: `*.env*` is listed before `*,*.envrc|*,.env*`. ShellCheck flags this as SC2221/SC2222, and the later arm is unreachable. The current behavior still redacts `.env*`, but the dead alternation makes the policy misleading and harder to maintain safely.
- **Recommended fix** — Collapse the redundant patterns into one canonical branch or reorder the specific basename checks before the broad `*.env*` glob. Add a focused shell test around `skip_dump` so future edits preserve the intended redaction set.

### Section 2: GitHub API Call Redundancy Audit

#### Finding 5
- **ID** — `API-001`
- **File path** — `.github/workflows/issue_pr_status.yml:295-320`; `.github/workflows/issue_pr_status.yml:503-512`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — `issue_pr_status.yml` already performs a batched GraphQL classification fetch (`ORCH_QUERY`) that returns each linked issue’s `number`, `labels`, and `body` (`issue_pr_status.yml:295-320`). Later in the same execution path, the Telegram-alert gate loops over `LINKED_ISSUE_NUMBERS` and re-fetches every issue body with `_safe_gh_jq "repos/.../issues/${issue_number}"` to detect `Managed by: AI Orchestrator` (`issue_pr_status.yml:503-512`).  
  **Current call count:** `1` GraphQL batch + `N` per-issue REST reads.  
  **Proposed call count after fix:** `1` GraphQL batch + `0` per-issue REST reads.  
  **Existing batching pattern to extend:** Reuse the existing alias-batched `ORCH_QUERY` pattern in this workflow, or lift the same shape into a helper modeled on `_fetch_candidate_issue_details_graphql` from `scripts/orchestrate_poll_process.sh`.
- **Recommended fix** — Persist the `ORCH_RESP` payload (or a normalized JSON file/env var) and reuse it in the alert step instead of re-querying each issue. If the early batch fails, perform one fallback batch for all needed issue bodies rather than an `N`-call loop.

#### Finding 6
- **ID** — `BATCH-001`
- **File path** — `.github/workflows/issue_pr_status.yml:353-381`
- **Severity** — Medium
- **Category tag** — `api-batching`
- **Description** — After discovery/classification, `issue_pr_status.yml` loops each linked issue and performs one label mutation plus one close mutation with `gh issue close`/REST per item (`issue_pr_status.yml:366-377`). This is linear API growth on multi-issue PRs.  
  **Current call count:** `2N` mutation calls after the initial discovery/classification requests.  
  **Proposed call count after fix:** `2` GraphQL mutation requests total for the batch (`addLabelsToLabelable` aliases + `closeIssue` aliases), after the existing discovery query is extended to include issue node IDs.  
  **Existing batching pattern to extend:** The workflow’s alias-based `ORCH_QUERY` builder, or the repo’s canonical GraphQL batching style in `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`.
- **Recommended fix** — Extend the existing batch query to fetch each issue node `id`, then send one aliased GraphQL mutation to add the terminal label and one aliased GraphQL mutation to close only the issues that should close. Keep the current per-issue REST path only as a fail-open fallback.

#### Finding 7
- **ID** — `API-002`
- **File path** — `scripts/orchestrate_poll_process.sh:3407-3468`; `scripts/orchestrate_poll_process.sh:3517-3529`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — `finalize_integration_merge_if_needed()` repeatedly fetches the same pull request through separate `_safe_gh_jq` calls: `state` and `merged_at` for an existing PR (`3411-3412`), then `state`, `mergeable`, and `merged_at` again before merge (`3466-3468`), then the same three fields again after merge attempt (`3517-3519`).  
  **Current call count:** `8` logical `GET /repos/{repo}/pulls/{n}` field reads on the success path (`2 + 3 + 3`), excluding `gh pr list` / `gh pr create`.  
  **Proposed call count after fix:** `2` pull-fetches total: one pre-merge PR snapshot and one post-merge refresh.  
  **Existing batching/cache pattern to extend:** The cycle-local PR JSON reuse pattern already documented and used near `scripts/orchestrate_poll_process.sh:6591-6605`.
- **Recommended fix** — Introduce a local `fetch_pr_snapshot()` helper that returns one JSON payload and derive `state`, `mergeable`, and `merged` from that cached object. Refresh it once after `gh pr merge`. This removes six redundant calls from every final-merge poll cycle.

#### Finding 8
- **ID** — `BATCH-002`
- **File path** — `.github/workflows/review_autofix.yml:504-549`
- **Severity** — Low
- **Category tag** — `api-batching`
- **Description** — In the post-merge validate-dispatch step, when `closingIssuesReferences` is empty, the workflow falls back to regex extraction (`505-515`) and then loops the derived issue numbers, calling `gh issue view ... --json labels` once per issue to find `ai:orchestrator-validate-required` (`519-529`).  
  **Current call count:** `1` PR text fetch + `N` per-issue label fetches on the fallback path.  
  **Proposed call count after fix:** `1` PR text fetch + `1` aliased GraphQL issue batch.  
  **Existing batching pattern to extend:** The alias-building GraphQL pattern already used in `issue_pr_status.yml` and the repo-standard `_fetch_candidate_issue_details_graphql` style in `scripts/orchestrate_poll_process.sh`.
- **Recommended fix** — After regex extraction, batch the issue label lookup in one GraphQL request instead of `gh issue view` inside the loop. Store the result in the same TSV stream consumed by the existing `while read` block.

### Section 3: Code Duplication & Modularization Opportunities

#### Finding 9
- **ID** — `DUP-001`
- **File path** — `.github/workflows/issue_pr_status.yml:195-217`; `.github/workflows/review_autofix.yml:504-515`; `.github/workflows/review_autofix.yml:3776-3789`; `.github/workflows/review_autofix.yml:3897-3910`; `.github/workflows/review_autofix.yml:4631-4644`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The repo repeats the same “fallback linked-issue extraction from PR title/body” logic in at least five places, including the same repo-escaped regex family and the same `sort -un` normalization. These copies have already drifted slightly in allowed keywords and comments, which raises the chance of future behavior splits between issue closing, ready-to-merge labeling, and post-merge validation dispatch.
- **Recommended fix** — Extract a shared shell helper such as `scripts/linked_issue_helpers.sh` with a function like `extract_linked_issue_numbers_from_pr_text <repository> <text>` plus an optional `load_pr_text_from_meta_or_api <repo> <pr_number> <pr_meta_file>`. Update `issue_pr_status.yml` and the three `review_autofix.yml` tail steps to call the shared helper instead of maintaining separate regex copies.

#### Finding 10
- **ID** — `DUP-002`
- **File path** — `.github/workflows/issue_pr_status.yml:69-120`; `.github/workflows/validate.yml:214-282`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — The support-ref checkout/copy machinery is duplicated across workflows: both files define nearly the same `checkout_support_ref` plus “copy from primary ref, else main, else local fallback” logic, including the same temp-dir staging and `realpath` equality guard. This is operationally sensitive code because it controls how workflows bootstrap helper scripts from `coding-workflows`.
- **Recommended fix** — Move this into a shared module, e.g. `scripts/support_fetch_helpers.sh`, with functions such as `support_checkout_ref <remote_url> <ref> <dest>` and `copy_from_support_ref <primary_root> <main_root> <repo_path> <target_path> <require_remote> <allow_main_fallback>`. Update both workflows to source that helper and keep the fallback policy in one place.

#### Finding 11
- **ID** — `DUP-003`
- **File path** — `.github/workflows/workflow-log-analysis.yml:392-408`; `.github/workflows/workflow-log-analysis.yml:821-824`; `.github/workflows/workflow-log-analysis.yml:1154-1157`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — `workflow-log-analysis.yml` repeats the same “post `AI_PHASE_FAILURE_V1` comment + ensure label + add label” failure tail three times for the main analysis, deep audit, and API-redundancy jobs. The bodies differ only by heading text. This is already copy-heavy enough that one block uses explicit warnings while the later blocks silently `|| true`.
- **Recommended fix** — Extract a shared shell helper, e.g. `scripts/log_analysis_failure_helpers.sh`, with a function signature like `post_log_analysis_failure <tracking_issue> <heading> <failure_summary> <run_url> <attempt_count>`. Update all three jobs to call the same helper so comments, retries, and warnings stay consistent.

### Section 4: Expression Size Limit Risk Assessment

#### Finding 12
- **ID** — `EXPR-001`
- **File path** — `.github/workflows/test-and-mark-stable.yml:1187-1557`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block is currently about **19,117 characters** and contains `${{ }}` interpolation, so it remains inside the expression-limit blast radius. That leaves only about **1,883 characters of headroom** before the 21,000-character hard failure. This same workflow family has already hit the expression ceiling multiple times, and this block is exactly the kind of polling logic that tends to accrete more diagnostics over time.
- **Recommended fix** — Extract the whole wait loop to an external script, preferably `scripts/test_and_mark_stable_wait_review.sh`, and pass only the small set of required env vars from YAML. That follows the repo’s existing pattern of moving oversized logic into scripts such as `scripts/review_conflict_prepare.sh`, `scripts/review_conflict_resolve.sh`, and `scripts/implement_diagnose_post_codex_failure.sh`.

#### Finding 13
- **ID** — `EXPR-002`
- **File path** — `.github/workflows/test-and-mark-stable.yml:1644-2048`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The `Phase 4b: Verify editor restored canary (pytest + retry)` `run:` block is about **17,408 characters**, leaving roughly **3,592 characters of headroom**. It is below the hard-risk threshold but already above the 15,000-character medium-risk line, and its retry/poll/pytest diagnostics are still growing.
- **Recommended fix** — Extract this block to a dedicated script such as `scripts/test_and_mark_stable_verify_bait_removed.sh`, or split it into separate “fetch/setup”, “attempt 1”, and “retry” steps. Externalizing the retry helper and fetch helpers would buy the most headroom with the least behavior change.

No workflow file exceeds the 800 KB early-warning threshold. The largest audited workflow is `review_autofix.yml` at `279,307` characters, followed by `test-and-mark-stable.yml` at `258,259`.

### Section 5: Cross-Cutting Concerns

#### Finding 14
- **ID** — `DEAD-001`
- **File path** — `scripts/orchestrate_lib.py:988-1374`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `parse_phase_failure_markers`, `evaluate_phase_failure_resume`, `resolve_label_repair_evidence`, and `choose_most_advanced_conclusive_evidence` are defined here, but repository-wide references only point back into `scripts/orchestrate_lib.py` itself; there are no call sites in workflows, scripts, or tests outside this module. `agents.md:111-118` also says the richer contradiction-evidence helpers are “reserved and not yet wired into poller reconciliation,” which confirms they are presently dormant.
- **Recommended fix** — Either wire these helpers into the poller’s label-repair/recovery path now, or remove/park them behind tests until the rollout is ready. If they remain intentionally reserved, isolate them in a clearly named compatibility module so the live poller path is easier to audit.

#### Finding 15
- **ID** — `DEAD-002`
- **File path** — `scripts/orchestrate_poll_process.sh:9744-9774`; `scripts/orchestrate_poll_process.sh:9993-10047`; `scripts/review_issue_ledger.sh:866-917`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — Several variables are written but never read anywhere else in the repo: `RB_FOLLOWUP_REFUSED` is assigned in the review-blocked follow-up path, `IF_BLOCKERS_SOURCE` is assigned for blocker provenance, and `CURRENT_FLOOR` is stored in the review ledger map. Repository-wide search shows no consumers beyond those writes. This increases mental overhead and makes it harder to tell which state actually drives behavior.
- **Recommended fix** — Remove these assignments if they are obsolete, or start emitting/consuming them explicitly in logs/output if they are meant to be operational signals. Keeping only live state in these large shell scripts will simplify future audits.

#### Finding 16
- **ID** — `CONSIST-001`
- **File path** — `.github/workflows/review_autofix.yml:582-594`; `.github/workflows/cancel_on_pr_close.yml:26-53`; `.github/workflows/issue_pr_status.yml:184-186`
- **Severity** — Medium
- **Category tag** — `consistency`
- **Description** — GitHub retry behavior is inconsistent across workflows. `issue_pr_status.yml` sources the central `scripts/gh_helpers.sh` helper, `cancel_on_pr_close.yml` implements its own rate-limit-aware retry loop, while `review_autofix.yml`’s deterministic-skip path uses a lightweight inline `gh_retry` with no rate-limit parsing at all. That means the same class of transient GitHub failures is handled differently depending on which code path is executing.
- **Recommended fix** — Standardize on `scripts/gh_helpers.sh` for workflow API retries, or extract a single composite action/shared shell helper that wraps the canonical behavior. The deterministic-skip path in `review_autofix.yml` is the most obvious first caller to migrate.

No `TODO`, `FIXME`, or `HACK` markers were present in the audited `.github/workflows/*.yml` and `scripts/*.{sh,py}` files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 8 | BUG-002, SEC-001, API-001, BATCH-001, API-002, DUP-001, EXPR-002, CONSIST-001 |
| Low | 6 | SHELL-001, BATCH-002, DUP-002, DUP-003, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 4 | Medium |
| Expression size reduction | 1 | Medium |
| Medium/Low fixes | 8 | Medium |
