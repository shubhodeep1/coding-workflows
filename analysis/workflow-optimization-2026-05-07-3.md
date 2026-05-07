## Executive Summary

- **The highest-priority fix is a cross-workflow Codex CLI compatibility break.** Failed runs in `clarify` (`25473125487`, `25473129175`, `25473129346`), `orchestrate` (`25473127144`), and `workflow_log_analysis` (`25473131401`) all show `error: unexpected argument '--ask-for-approval' found`, followed by repeated retries and terminal failure. **Estimated impact:** eliminate most failures in those three families immediately; save ~1.5–40 minutes per affected run depending on workflow. **Confidence:** high.

- **`review_autofix` is the dominant end-to-end latency bottleneck.** Family stats are `p95=1456.2s`, `avg=359.5s`, with `58/95` runs cancelled; slow successful runs include `25474809520` at `1234s` and `25474879472` at `1047s`, both showing runner waits plus long check-run waiting (`CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`, `CHECK_RUNS_POLL_INTERVAL_SECS=20`). **Estimated impact:** 5–15 minutes saved on long review paths by shrinking wait windows and avoiding second-runner scheduling for skip/gate-only cases. **Confidence:** medium-high.

- **CI is consistently slow even when healthy.** `ci` family `p50=619.5s`, `p95=649.3s`, and recent successful runs `25475355574` (`635s`), `25475284383` (`625s`), `25474982677` (`614s`), `25474880799` (`622s`) were all dominated by the single `lint` job. **Estimated impact:** 3–5 minutes per CI run by sharding long test groups. **Confidence:** high.

- **Workflow log analysis is burning significant tokens before failing.** Run `25473131401` emitted `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` with `summarized=85/100` and `tokens_used=214237`, then failed its Codex pass due to the same CLI flag incompatibility. **Estimated impact:** save ~214k tokens on each failed analysis run, plus 5–10 minutes runtime. **Confidence:** high.

- **Prompt cache/memory is underperforming relative to intent.** Across deep-dive logs, memory `retrieve` hit rate was only `6/20 = 30%`; `14/20` retrieves returned zero records, including `clarify` `25473125487` and `orchestrate` `25473127144`. Prompt cache was enabled (`OPENROUTER_PROMPT_CACHE_DISABLED=false`) in multiple runs, but no prompt-cache hit/read telemetry was emitted. **Estimated impact:** moderate token and latency savings if semantic cache failures and prompt-prefix instability are fixed. **Confidence:** medium.

- **GH API usage is concentrated in a few polling/audit hotspots.** Observed high-volume steps include `workflow_log_analysis/api-redundancy` (`95 gh api` calls in `25445456156`; `80 gh api + 5 github.rest + 14 paginate` in `25441969004`) and `test_and_mark_stable/e2e-smoke-test` (`67 gh api` calls in `25470780569`, `31` in `25445414047`). **Estimated impact:** 40–70% call reduction in those hotspots with batching/backoff changes, lowering rate-limit and delay risk. **Confidence:** high.

## Speed Optimizations

### 1. Reduce `review_autofix` critical-path wait time before/around Codex review
**Rank:** Critical-path win #1

- **Evidence**
  - `review_autofix` family: `avg=359.46s`, `p95=1456.2s`, `58` cancelled out of `95` total runs.
  - Slow successful runs: `25474809520` took `1234s`; `25474879472` took `1047s`.
  - Both runs logged `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`, `CHECK_RUNS_POLL_INTERVAL_SECS: 20`, plus runner wait on both `review gate` and `review codex-agent`.
  - Fast gated skip run `25475355638` finished in `13s` when the gate short-circuited on `AUTOFIX_GATE_SKIP reason=self_triggered_autofix`.

- **Root cause**
  - Long-lived wait budget in the review phase plus dual-job scheduling means many runs pay queue time and polling overhead even when they later skip, cancel, or do minimal work.

- **Exact change**
  - Keep all gate-only paths inside a **single lightweight job** and only schedule `codex-agent` after the gate produces a positive “must review” output.
  - Lower `CHECK_RUNS_WAIT_TIMEOUT_SECS` for non-blocking review paths from `1200` to a smaller tiered value, e.g. 300–600s, with a separate override only for explicitly marked long waits.
  - Increase poll interval adaptively: 20s for first 2–3 polls, then 45–60s.

- **Estimated time savings**
  - **Long runs:** 300–900s.
  - **Skip/cancel cases:** 30–120s from avoiding second-runner startup.

- **Implementation risk**
  - **Low-medium.** Backward-compatible if the longer timeout remains available behind an explicit override.

---

### 2. Split the monolithic `ci/lint` workload into parallel shards
**Rank:** Critical-path win #2

- **Evidence**
  - `ci` family stats: `p50=619.5s`, `p95=649.35s`, `avg=613.47s`.
  - Recent healthy runs: `25475355574` (`635s`), `25475284383` (`625s`), `25474982677` (`614s`), `25474880799` (`622s`) all report `lint` dominating runtime.
  - Failed CI runs `25473514248` and `25473697143` show multiple logically separate test groups finishing sequentially (`157 passed`, then `6 passed`, `7 passed`, `6 passed`, `3 passed`, then `42 passed, 1 failed`).

- **Root cause**
  - One long `lint` job serializes heterogeneous test suites that do not appear to depend on each other.

- **Exact change**
  - Split `lint` into 3–4 jobs/shards:
    1. workflow/script reference and fast unit checks
    2. orchestrator/poller tests
    3. review/autofix integration tests
    4. semantic cache / memory tests
  - Preserve a single required aggregate status using `needs`.

- **Estimated time savings**
  - 180–300s per CI run.

- **Implementation risk**
  - **Medium.** Requires some test grouping work, but no behavior changes.

---

### 3. Trim `workflow_log_analysis` scope before expensive summarization
**Rank:** Critical-path win #3

- **Evidence**
  - `workflow_log_analysis` family: `avg=2376s`, `p50=2810s`, `p95=3488.15s`.
  - Successful slow runs: `25445456156` (`3566s`), `25441969004` (`3047s`), `25470798500` (`2573s`).
  - Failed run `25473131401` still spent time and tokens on `summarize_unselected_runs` before failing.

- **Root cause**
  - The analyzer processes broad run sets even when the downstream Codex pass is not viable.

- **Exact change**
  - Add a **Codex CLI compatibility preflight** before `summarize_unselected_runs`.
  - Reduce summary target size from 100 to a smaller adaptive set: recent failures + top slow runs + changed workflow families only.
  - Skip deep audit when there are no new failures/regressions versus prior collector output.

- **Estimated time savings**
  - 300–900s on typical runs; larger on failed runs.

- **Implementation risk**
  - **Low.** Mostly collection-order and prioritization changes.

---

### 4. Collapse multi-runner overhead in `copilot_pull_request_reviewer`
**Rank:** Secondary win

- **Evidence**
  - Recent success `25475355310` took `71s`; `Prepare`, `Upload results`, and `Cleanup artifacts` all waited for hosted runners.
  - Recent success `25474818521` (`76s`) and `25474810132` (`91s`) show the same pattern.
  - `Cleanup artifacts` lists run artifacts and deletes them in a separate job.

- **Root cause**
  - Small jobs incur repeated queue/startup costs.

- **Exact change**
  - Move artifact cleanup into the same job that uploads results, guarded by `if: always()`.
  - Only perform cleanup if the artifact list is non-empty.

- **Estimated time savings**
  - 20–40s per run.

- **Implementation risk**
  - **Low.**

---

### 5. Reduce polling overhead in `test_and_mark_stable/e2e-smoke-test`
**Rank:** Secondary win

- **Evidence**
  - `test_and_mark_stable` family `avg=3767.5s`, `p95=4429.85s`.
  - Slow runs: `25470780569` (`4508s`, cancelled), `25445414047` (`3987s`, failed), `25441918019` (`3531s`, success).
  - In `25445414047` and `25441918019`, the step uses `POLL_INTERVAL: 10`, `PHASE_TIMEOUT: 30`, repeated `gh api` polling, and multiple phase waits.

- **Root cause**
  - Tight polling loops and long phase waits amplify end-to-end release verification time.

- **Exact change**
  - Use adaptive polling: 10s for the first minute, then 30s+.
  - Reuse previously fetched PR/run metadata instead of refetching each loop iteration.
  - Keep the existing PR-closed fast-fail guard early in Phase 4 and ensure it runs before long waits.

- **Estimated time savings**
  - 120–300s on normal runs; more on failure paths.

- **Implementation risk**
  - **Low-medium.**

## Cost Optimizations

### 1. Stop paying for large log summaries when the Codex pass will fail anyway
**Rank:** Highest token-savings item

- **Evidence**
  - `workflow_log_analysis` run `25473131401` emitted:
    - `AI_MEMORY_TELEMETRY: {"op":"summarize_unselected_runs", ... "summarized":85, "targeted":100, "tokens_used":214237}`
  - The same run then failed with:
    - `Workflow log analysis Codex pass failed after 3 attempts with exit code 2`
    - `error: unexpected argument '--ask-for-approval' found`

- **Root cause**
  - Token-heavy context generation happens before validating that the Codex invocation is compatible.

- **Exact change**
  - Preflight `codex exec` compatibility first.
  - Only run `summarize_unselected_runs` after the preflight succeeds.
  - If preflight fails, emit a compact failure artifact and stop.

- **Estimated savings**
  - ~214k tokens per failed analysis run of this type.
  - Likely 50%+ token reduction on successful analysis runs if the unselected-run target is also reduced.

- **Quality-risk notes**
  - **Low risk.** This removes obviously wasted work.

---

### 2. Narrow the reviewer panel on low-risk PRs before invoking the full ensemble
**Rank:** High, but partly inference-based

- **Evidence**
  - `review_autofix` env shows a large reviewer panel in recent runs (`25475355638`, `25474879472`, `25474809520`):
    - `REVIEWER_MODELS: minimax/... moonshotai/... deepseek/... z-ai/... qwen/... x-ai/...`
    - `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`
  - `25475355638` proved the gate can short-circuit the path entirely in `13s`.

- **Root cause**
  - The pipeline appears configured for a broad multi-model review path even when deterministic gates may classify a PR as low risk.

- **Exact change**
  - For small diffs/doc-only/self-triggered or otherwise low-risk cases, run:
    1. deterministic gate,
    2. then 1–2 reviewer models,
    3. escalate to the full panel only on disagreement or failure markers.
  - Keep current full-panel behavior for high-risk or explicitly forced-review PRs.

- **Estimated savings**
  - **Inference:** potentially 30–60% model spend on low-risk review paths.

- **Quality-risk notes**
  - **Medium.** Needs an escalation rule to preserve review quality.

---

### 3. Repair semantic-cache lookup fail-open paths so clarify/implement can avoid unnecessary live model calls
**Rank:** Moderate

- **Evidence**
  - `clarify` failure `25473125487` logged:
    - `Semantic cache lookup command failed; continuing with live Codex run`
    - `Semantic cache lookup fail-open`
  - Memory retrieval in that same run returned `records_selected: 0`.
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` shows caching was intended to be active.

- **Root cause**
  - Cache lookup failures and low retrieval hit rate force more live model work than intended.

- **Exact change**
  - Fix the `scripts/semantic_cache.py lookup` invocation path/input contract.
  - Treat lookup-command failures as a monitored defect, not a silent normal path.
  - Normalize clarify prompt prefixes so semantically similar issues hash more consistently.

- **Estimated savings**
  - Moderate token savings on repeated clarify/implement issues; exact $ impact not emitted in current telemetry.

- **Quality-risk notes**
  - **Low.** The live-model fallback remains available.

---

### 4. Reduce repeated prompt/context expansion in orchestrator and clarify prompts
**Rank:** Moderate

- **Evidence**
  - `clarify` `25473125487` fetches issue metadata, limited comments, and optionally full paginated thread history before prompt assembly.
  - `orchestrate` `25473127144` appends explicit retry directives to the prompt between attempts.
  - Prompt cache metrics were not emitted, so stable-prefix reuse cannot be confirmed.

- **Root cause**
  - Dynamic content is likely mixed too early into prompts, reducing prompt-cache reuse. This is an inference from the prompt-building steps.

- **Exact change**
  - Move static instructions/templates to a stable prefix.
  - Append only dynamic issue state, comments, and retry feedback at the tail.
  - Avoid re-embedding full comment history unless the cache/memory layer says it is needed.

- **Estimated savings**
  - **Inference:** low-to-moderate token and latency reduction across repeated clarify/orchestrate attempts.

- **Quality-risk notes**
  - **Low** if the full context remains available when needed.

## Reliability Improvements

### 1. Fix unsupported Codex CLI arguments everywhere they are still used
**Rank:** Highest failure-rate reduction

- **Failure evidence**
  - `clarify` runs `25473125487`, `25473129175`, `25473129346`: `error: unexpected argument '--ask-for-approval' found`, followed by three retries and failure.
  - `orchestrate` run `25473127144`: same error on all three attempts.
  - `workflow_log_analysis` run `25473131401`: same error on all three attempts.

- **Root cause category**
  - Tooling/version compatibility drift.

- **Exact fix**
  - Replace the unsupported flag with a version-compatible invocation, or gate it behind a CLI capability check.
  - Centralize Codex invocation flags in one helper so all workflows consume the same argument set.

- **Expected reliability impact**
  - Likely removes the majority of recent failures in `clarify`, `orchestrate`, and `workflow_log_analysis`.

- **Rollback / fail-open**
  - If capability detection fails, omit the optional flag and continue with the supported baseline invocation.

---

### 2. Fix workflow support-source checkout/staging drift for `.codex-workflow-src`
**Rank:** High

- **Failure evidence**
  - `clarify` `25441973385`, `25473125487`, `25473129175`, `25473129346`:
    - `Failed to checkout workflow support source from ${SCRIPT_REF} or main`
    - `Missing required support script ${f} in ${wf_source}@${SCRIPT_REF}`
  - `implement` `25470900024` and `orchestrate` `25473127144` show the same pattern.

- **Root cause category**
  - Workflow asset packaging/path resolution drift.

- **Exact fix**
  - When `wf_source` equals the current repository, stage support scripts directly from the checked-out workspace first.
  - Only clone/fallback when the source repo actually differs.
  - Add a pre-Codex manifest check that fails immediately if required scripts/prompts/schemas are missing.

- **Expected reliability impact**
  - Should stabilize multiple AI workflow families at once.

- **Rollback / fail-open**
  - Keep the existing clone fallback as secondary behavior until the direct-workspace path proves stable.

---

### 3. Stabilize flaky CI integration tests by isolating golden/fingerprint checks
**Rank:** Medium

- **Failure evidence**
  - `ci` `25469919488`: `Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent`, final `156 passed, 1 failed, 157 total`.
  - `ci` `25473514248` and `25473697143`: `FAIL test_review_pipeline_integration_chain_module_runs_clean`.

- **Root cause category**
  - Brittle integration/golden assertions.

- **Exact fix**
  - Move fingerprint and review-pipeline integration tests into a dedicated shard/job with clearer fixture ownership.
  - Regenerate and pin canonical fixtures explicitly when intended behavior changes, rather than failing deep in a monolithic `lint` job.

- **Expected reliability impact**
  - Lower CI rerun rate and faster diagnosis for the current `4.05%` CI failure rate.

- **Rollback / fail-open**
  - Keep the tests required, but isolate them so they stop obscuring unrelated CI outcomes.

---

### 4. Add bounded retries to GitHub-script based reviewer jobs
**Rank:** Medium-low

- **Failure evidence**
  - `copilot_pull_request_reviewer` recent runs `25475355310`, `25474818521`, `25474810132` show `actions/github-script@v8` with `retries: 0`.

- **Root cause category**
  - Transient API failure sensitivity.

- **Exact fix**
  - Set small bounded retries for GitHub-script API steps in `Prepare` and similar jobs.

- **Expected reliability impact**
  - Reduces failures from transient GitHub API or runner-side network errors.

- **Rollback / fail-open**
  - Keep retry-exempt status codes unchanged to avoid retrying permanent client errors.

---

### 5. Triage and split failing nightly self-test fixtures
**Rank:** Medium-low

- **Failure evidence**
  - `nightly_validation_selftest` run `25474243471`: `fixtures=3 passed=1 failed=2`, then exit code 1.

- **Root cause category**
  - Multiple failing fixtures hidden inside one nightly matrix step.

- **Exact fix**
  - Break the failing fixtures into explicit matrix rows or at least emit fixture-level pass/fail annotations into the workflow summary.

- **Expected reliability impact**
  - Faster recovery and clearer blame assignment for nightly regressions.

- **Rollback / fail-open**
  - Keep artifact upload on failure as currently implemented.

## AI Memory Health

- **Telemetry coverage**
  - Deep-dive logs contained usable `AI_MEMORY_TELEMETRY` across `clarify`, `implement`, `orchestrate`, `review_autofix`, and `workflow_log_analysis`.

- **Retrieve hit rate**
  - Observed `retrieve` operations: `20`
  - Retrieves with `records_selected > 0`: `6`
  - Retrieves with `records_selected = 0`: `14`
  - **Hit rate:** `30%`

- **Estimated token usage**
  - Average `estimated_tokens`: `16.8`
  - Max `estimated_tokens`: `56` (`implement` run `25470900024`, role `implementation`)
  - No explicit retrieval token budget was emitted, so budget adherence cannot be scored directly.

- **`keyword_method` distribution**
  - `plain`: `11/20` (`55%`)
  - `none`: `8/20` (`40%`)
  - `llm`: `1/20` (`5%`)

- **Notable zero-hit examples**
  - `clarify` `25473125487`: `{"op":"retrieve","records_selected":0,"keyword_method":"plain","estimated_tokens":0}`
  - `clarify` `25473129175`: same pattern.
  - `clarify` `25473129346`: same pattern.
  - `orchestrate` `25473127144`: `{"op":"retrieve","records_selected":0,"keyword_method":"llm","estimated_tokens":0}`
  - `review_autofix` `25442795657`: reviewer retrieve with `keyword_method:"none"` and `records_selected:0`

- **Fail-open / disabled flags**
  - No parsed `AI_MEMORY_TELEMETRY` entries showed `fail_open: true`.
  - No parsed entries showed `enabled: false`.
  - However, non-telemetry warnings still show cache fail-open behavior in `clarify` `25473125487` (`Semantic cache lookup fail-open`), so fail-open is happening at least in adjacent cache logic.

- **Push retry health**
  - Telemetry entries with `push_attempts > 1`: `6`
  - Maximum observed `push_attempts`: `2`
  - Notable examples:
    - `clarify` `25473129346` phase_failed event had `push_attempts: 2`
    - `clarify` `25441973385` phase_started event had `push_attempts: 2`
  - This is not yet alarming, but it is worth tracking.

- **Assessment**
  - The memory system is functioning, but retrieval effectiveness is modest and heavily skewed toward zero-result fetches.
  - The biggest practical issue is not storage failure; it is **low retrieval usefulness** plus nearby semantic-cache fail-open behavior.

- **Recommendation**
  - Track and alert on:
    1. retrieve hit rate,
    2. zero-hit rate by workflow family,
    3. semantic-cache lookup-command failures,
    4. `push_attempts > 1`.
  - Prioritize `clarify` and `orchestrate`, where the current retrieves are often zero-value.

## GH API Call Audit

### 1. `workflow_log_analysis` has the highest observed call volume
- **Evidence**
  - `api-redundancy` step:
    - run `25445456156`: `95 gh api` calls, `7` paginated calls
    - run `25441969004`: `80 gh api`, `5 github.rest`, `14 paginate`
    - run `25470798500`: `43 gh api`, `1 github.rest`, `7 paginate`
  - `analyze-commit-notify` step in `25470798500`: `29 gh api`, `13 github.rest`, `18 paginate`

- **Pattern**
  - High-volume audit-on-audit behavior is expected here, but it is still a major API consumer.

- **Concrete recommendation**
  - Restrict deep audit/API redundancy analysis to changed workflows, recent failures, and top-N slow runs instead of broad repo-wide sweeps every run.
  - Reuse prior analysis artifacts when no new failing/slow candidates appear.

- **Estimated call reduction**
  - 30–60% for this workflow family.

- **Rate-limit risk reduction**
  - High.

---

### 2. `test_and_mark_stable/e2e-smoke-test` repeatedly polls run status via REST
- **Evidence**
  - Observed call counts:
    - `25470780569` `step-008-e2e-smoke-test.log`: `67 gh api`
    - `25441918019` `step-013-e2e-smoke-test.log`: `67 gh api`
    - `25445414047` `step-008-e2e-smoke-test.log`: `31 gh api`
  - Logs show repeated `gh api "repos/${REPO}/actions/runs/${RID}" --jq '.status'` polling with `POLL_INTERVAL: 10`.

- **Pattern**
  - Tight per-run polling loop with repeated single-resource lookups.

- **Concrete recommendation**
  - Reuse fetched run metadata for multiple checks in the same iteration.
  - Switch to adaptive polling/backoff after the first minute.
  - Stop polling immediately when the PR is closed/merged.

- **Estimated call reduction**
  - 40–70% in this step.

- **Rate-limit risk reduction**
  - Medium-high.

---

### 3. `review_autofix` re-fetches PR metadata and files across gate/agent logic
- **Evidence**
  - Slow review runs `25442795657` and `25431921069` each showed `22 gh api` and `12 paginate` in `review_codex-agent`.
  - Recent gate run `25475355638` shows:
    - PR metadata fetch with `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"`
    - commit metadata fetch with `gh api "repos/${REPOSITORY}/commits/${PR_HEAD_SHA}"`
    - optional paginated `/pulls/${PR_NUMBER}/files`

- **Pattern**
  - Repeated per-PR metadata reads across steps.

- **Concrete recommendation**
  - Fetch PR meta/files once in the gate step, save to a small JSON artifact or outputs, and reuse in downstream review logic.
  - Do not call paginated `/files` when the small-diff gate already proves the PR is below skip thresholds.

- **Estimated call reduction**
  - 25–40% for review runs.

- **Rate-limit risk reduction**
  - Medium.

---

### 4. `issue_pr_status` still has avoidable fallback duplication
- **Evidence**
  - Recent run `25475389746`:
    - GraphQL fetch of linked issues
    - fallback `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"`
    - per-issue label POSTs
    - per-issue `_orch_meta` REST fetches
  - `workflow_log_analysis/api-redundancy` for `25470798500` explicitly flagged:
    - repeated PR body/title fallback fetch,
    - per-issue REST lookup after GraphQL classification,
    - and recommended extending existing batching patterns in `scripts/orchestrate_poll_process.sh::_fetch_candidate_issue_details_graphql()` / `_fetch_linked_pr_status_graphql()`.

- **Pattern**
  - Partially batched GraphQL path followed by per-item REST cleanup.

- **Concrete recommendation**
  - Make `PR_META_FILE` the sole source of PR title/body fallback.
  - Batch issue metadata follow-up with the existing GraphQL alias pattern instead of per-issue REST calls.
  - Persist classification outputs (`managed issue`, `tracking issue`, etc.) and reuse them later in the step.

- **Estimated call reduction**
  - 20–35% in `issue_pr_status`.

- **Rate-limit risk reduction**
  - Medium.

---

### 5. `copilot_pull_request_reviewer` lacks retry hygiene
- **Evidence**
  - `Prepare` in `25475355310` uses `github.rest.pulls.get` and `github.paginate(github.rest.pulls.listFiles)` with `retries: 0`.
  - Cleanup lists artifacts with `gh api /actions/runs/.../artifacts` then deletes each artifact individually.

- **Pattern**
  - Correct functionality, but no resilience to transient API errors.

- **Concrete recommendation**
  - Add bounded retries for GitHub-script steps.
  - Keep artifact deletion loop, but skip the delete phase entirely when the artifact list is empty.

- **Estimated call reduction**
  - Small.
- **Reliability/rate-limit impact**
  - Moderate reliability gain, low call-count gain.

## Prompt Cache & Memory System

- **Observed prompt-cache posture**
  - Multiple AI runs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, including:
    - `clarify` `25473125487`
    - `implement` `25470900024`
    - slow `review_autofix` runs `25442795657`, `25431921069`
  - But the logs do **not** emit prompt-cache creation/read counters, so actual hit rate is unknown.

- **Observed cache behavior**
  - GitHub Actions cache for Codex binary is healthy:
    - `workflow_log_analysis` `25473131401` restored `codex-v0.114.0` with a cache hit.
  - Semantic cache behavior is weaker:
    - `clarify` `25473125487` logged `Semantic cache lookup command failed; continuing with live Codex run`
    - same run logged a `Semantic cache lookup fail-open` warning.

- **Likely fragmentation causes**
  - `clarify` builds prompts from issue metadata + comment history + optional full paginated thread history, making the prompt highly variable.
  - `orchestrate` `25473127144` appends retry-specific corrective directives to the prompt between attempts.
  - `review_autofix` uses multiple reviewer models plus summarizer configuration, increasing prompt-shape variance.
  - These are strong indicators of unstable prefixes. This is an inference, not a direct cache-hit measurement.

- **Concrete improvements**
  1. **Stabilize prompt prefixes**
     - Keep policy/instructions/templates in a fixed prefix.
     - Append dynamic issue state and comments later.
  2. **Repair semantic cache lookup**
     - Fix the failing `semantic_cache.py lookup` path/input issue observed in `25473125487`.
  3. **Avoid duplicate thread-history fetches**
     - If full paginated comments are fetched for semantic cache, derive the short comment view from that same result instead of fetching both.
  4. **Emit prompt-cache metrics**
     - Add creation/read/hit/miss counters to logs so this system can be audited directly.

- **Estimated impact**
  - **Tokens:** low-to-moderate reduction, especially on repeated clarify/orchestrate runs.
  - **Latency:** moderate reduction for repeated issue classes.
  - **Reliability:** moderate improvement by reducing fail-open cache bypasses.

## Orchestrator Health

- **Control-plane noise is high**
  - `clarify`: `201` runs, only `21` success, `176` other/skipped.
  - `plan`: `167` runs, `19` success, `148` other/skipped.
  - `implement`: `167` runs, `19` success, `140` other/skipped.
  - `orchestrate_clarify_respond`: `168` runs, `3` success, `165` other/skipped.

- **Interpretation**
  - The orchestration model is functionally gating work, but many workflows are still being dispatched only to skip almost immediately. That keeps end-user latency low in happy paths, but it creates queue noise and runner churn.

- **Observable health issues**
  - Repeated clarify failures tied to Codex CLI incompatibility (`25473125487`, `25473129175`, `25473129346`).
  - Successful auto-answer clarify runs in summaries (`25474721363`, `25474720794`) show `Clarification completed: auto_answered_by_orchestrator` and `push_attempts: 2`.
  - `implement` no-op/skip runs still incur runner wait, e.g. `25474762314` (`246s`) skipped because the issue was not in `ai:awaiting-approval`.

- **Smallest safe mitigations**
  1. Move more phase checks to workflow/job `if` conditions before runner allocation.
  2. Fix the shared Codex invocation path first; it currently masks true orchestrator health.
  3. Emit a small “dispatch reason” metric per workflow family: executed vs skipped-before-runner vs skipped-after-runner.

- **Indicators teams should track**
  - `% of runs skipped after runner allocation`
  - `codex exit code 2` count by workflow family
  - `review_autofix` cancel rate
  - memory retrieve hit rate
  - `push_attempts > 1` frequency
  - average GH API calls per successful run by family

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Implement control flow has too many low-value dispatches
- **Evidence**
  - Large skipped counts across `clarify`, `plan`, `implement`, `orchestrate_clarify_respond`.
  - Recent skipped runs frequently complete in `0–1s`, but some non-executing paths still wait for runners.

- **Bottleneck type**
  - Queueing + orchestration overhead.

- **Recommendation**
  - Push comment-body/state gating earlier into workflow triggers or top-level `if` expressions where possible.

---

### 2. Implement/Clarify/Orchestrate failures are dominated by retrying non-retryable Codex errors
- **Evidence**
  - `clarify` and `orchestrate` repeatedly retried `unexpected argument '--ask-for-approval'`.
  - `workflow_log_analysis` also retried the same error three times.
- **Bottleneck type**
  - Retry overhead.
- **Recommendation**
  - Classify CLI argument errors as non-retryable and fail immediately after first detection.

---

### 3. Review/autofix is the largest compute + wait sink in the user-facing path
- **Evidence**
  - `review_autofix` `p95=1456.2s`, slow successes >1000s, `58` cancellations.
- **Bottleneck type**
  - Queueing + compute + wait-for-checks overhead.
- **Recommendation**
  - Shrink wait budgets, avoid dual-runner scheduling for gate-only paths, and defer the full reviewer panel until needed.

---

### 4. CI is a stable but expensive serialized block
- **Evidence**
  - `ci` `p50=619.5s`, many successful runs clustered around `604–635s`.
- **Bottleneck type**
  - Compute.
- **Recommendation**
  - Parallelize test shards; keep only the truly shared setup serialized.

---

### 5. Stable-release verification spends too much time polling downstream phases
- **Evidence**
  - `test_and_mark_stable` runs from `3044s` to `4508s`, including failure at `Phase 4: Wait for review & autofix to complete` in `25445414047`.
- **Bottleneck type**
  - Polling + merge/review coordination overhead.
- **Recommendation**
  - Adaptive polling, early PR-state termination, and tighter phase scoping.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-path latency (`p95=1456.2s`, many cancellations).
- `ci/lint` monolithic ~10 minute runtime.
- `test_and_mark_stable` 50–75 minute end-to-end runs.
- `workflow_log_analysis` long runtime with high API and token spend.

**Top failure modes**
- Shared Codex CLI incompatibility (`--ask-for-approval`) in `clarify`, `orchestrate`, `workflow_log_analysis`.
- Workflow support-source staging failures (`.codex-workflow-src` missing required scripts).
- CI integration/fingerprint regressions.
- Nightly validation self-test fixture failures.

**Highest-cost drivers**
- `workflow_log_analysis` unselected-run summarization (`214237` tokens in failed run `25473131401`).
- Broad reviewer ensemble in `review_autofix` (inference from multi-model config).
- Repeated polling/API churn in `test_and_mark_stable` and audit workflows.

**Top 3 prioritized actions**
1. **Fix Codex CLI compatibility and centralize invocation flags.**
2. **Split CI `lint` into parallel shards.**
3. **Shorten/reshape `review_autofix` wait behavior so skip paths never allocate a second long-lived job.**

## Metrics Appendix

### Overall Repo Window

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 286 | 12 | 67 | 635 | 1.2% | 123.2 | 2.0 | 630.0 |

### Key Workflow Family Metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 74 | 71 | 3 | 0 | 0 | 4.05% | 613.5 | 619.5 | 649.3 |
| clarify | 201 | 21 | 4 | 0 | 176 | 1.99% | 13.5 | 1.0 | 99.0 |
| implement | 167 | 19 | 1 | 7 | 140 | 0.60% | 25.7 | 1.0 | 223.2 |
| orchestrate | 5 | 4 | 1 | 0 | 0 | 20.0% | 151.6 | 165.0 | 173.4 |
| review_autofix | 95 | 35 | 0 | 58 | 2 | 0.0% | 359.5 | 58.0 | 1456.2 |
| test_and_mark_stable | 4 | 1 | 1 | 2 | 0 | 25.0% | 3767.5 | 3759.0 | 4429.9 |
| workflow_log_analysis | 4 | 3 | 1 | 0 | 0 | 25.0% | 2376.0 | 2810.0 | 3488.1 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.0% | 100.0 | 100.0 | 100.0 |

### Recent / Slow Run Evidence Used

| Run ID | Workflow family | Conclusion | Duration (s) | Key evidence |
|---|---|---|---:|---|
| 25475355574 | ci | success | 635 | `lint` dominated runtime |
| 25475284383 | ci | success | 625 | `lint` dominated runtime |
| 25474982677 | ci | success | 614 | `lint` dominated runtime |
| 25474880799 | ci | success | 622 | `lint` dominated runtime |
| 25474809520 | review_autofix | success | 1234 | long review path, queue + check waits |
| 25474879472 | review_autofix | success | 1047 | long review path, queue + check waits |
| 25475355638 | review_autofix | success | 13 | gate skipped self-triggered autofix |
| 25445414047 | test_and_mark_stable | failure | 3987 | failed at Phase 4 wait for review/autofix |
| 25441918019 | test_and_mark_stable | success | 3531 | long polling-heavy stable verification |
| 25470780569 | test_and_mark_stable | cancelled | 4508 | long polling-heavy stable verification |
| 25473131401 | workflow_log_analysis | failure | 318 | 214k-token summarization then Codex CLI failure |
| 25474243471 | nightly_validation_selftest | failure | 100 | `fixtures=3 passed=1 failed=2` |

### Observed Token Telemetry

| Run ID | Workflow family | Telemetry op | Model | Tokens used | Notes |
|---|---|---|---|---:|---|
| 25473131401 | workflow_log_analysis | summarize_unselected_runs | openai/gpt-5.4-mini | 214237 | summarized 85 of 100 targeted runs before failing later |

**Gap:** comparable per-run token totals were not emitted for most other workflows in the collected deep-dive logs.

### AI Memory Retrieval Metrics

| Metric | Value |
|---|---:|
| Total `retrieve` ops observed | 20 |
| Hit count (`records_selected > 0`) | 6 |
| Zero-hit count | 14 |
| Hit rate | 30% |
| Avg `estimated_tokens` | 16.8 |
| Max `estimated_tokens` | 56 |
| `keyword_method=plain` | 11 |
| `keyword_method=none` | 8 |
| `keyword_method=llm` | 1 |
| `fail_open: true` telemetry entries | 0 observed |
| `enabled: false` telemetry entries | 0 observed |
| Telemetry entries with `push_attempts > 1` | 6 |
| Max `push_attempts` | 2 |

### Observed GH API Hotspots from Deep-Dive Logs

| Run ID | Workflow family / step | Observed API pattern | Approx observed volume |
|---|---|---|---:|
| 25445456156 | workflow_log_analysis / api-redundancy | `gh api` + paginate | 95 `gh api`, 7 paginate |
| 25441969004 | workflow_log_analysis / api-redundancy | `gh api` + `github.rest` + paginate | 80 `gh api`, 5 `github.rest`, 14 paginate |
| 25470798500 | workflow_log_analysis / analyze-commit-notify | mixed REST + paginate | 29 `gh api`, 13 `github.rest`, 18 paginate |
| 25470780569 | test_and_mark_stable / e2e-smoke-test | polling loop | 67 `gh api` |
| 25441918019 | test_and_mark_stable / e2e-smoke-test | polling loop | 67 `gh api` |
| 25442795657 | review_autofix / review_codex-agent | PR/review fetches | 22 `gh api`, 12 paginate |
| 25431921069 | review_autofix / review_codex-agent | PR/review fetches | 22 `gh api`, 12 paginate |
| 25475355310 | copilot_pull_request_reviewer / Prepare | `github.rest.pulls.get` + `github.paginate(listFiles)` | low volume but no retries |
| 25475389746 | issue_pr_status / sync-issue-status | GraphQL + fallback REST + per-issue mutations | moderate, partially batched |

### Cache Signals

| Cache type | Evidence | Status |
|---|---|---|
| GitHub Actions cache (Codex binary) | `workflow_log_analysis` `25473131401` restored `codex-v0.114.0` successfully | Healthy |
| Prompt cache | `OPENROUTER_PROMPT_CACHE_DISABLED=false` in clarify/implement/review runs | Enabled, but hit/miss telemetry absent |
| Semantic cache | `clarify` `25473125487` logged lookup-command failure and fail-open warning | Unhealthy / needs repair |
