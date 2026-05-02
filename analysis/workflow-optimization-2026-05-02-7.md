## Executive Summary

- **The release-validation path is the biggest end-to-end latency and reliability problem.** `test_and_mark_stable` has **6 runs, 0 successes, avg 3211.5s, p50 2647s, p95 5765.8s**; failed run **25247210528** spent over **107 minutes** and still ended with `Editor Bait. FAILED (bait_remained)`. **Estimated impact:** save **20–40 min** per release cycle and materially raise stable-release success rate. **Confidence:** high.
- **`review_autofix` is the dominant AI cost center and a major cancellation sink.** The family shows **68 runs, 37 cancelled, p95 1536.1s**. Slow run **25247372376** took **2605s**, used a six-model reviewer panel, and ended with `DID_COMMIT: false` and `EDITOR_NOOP_SUSPICIOUS: true`. **Estimated impact:** **30–60%** token/cost reduction on low-complexity reviews and fewer superseded cancellations. **Confidence:** high.
- **`implement` burns minutes on both skipped work and failed no-op retries.** Family stats show **181 runs**, **9 failures**, **p95 202s**. Failed run **25246727158** consumed at least **14,134 tokens** on attempt 2 before bailing with `2 consecutive attempts with no actionable output`, while successful-but-skipped runs like **25247302521** and **25247310871** still took **179s** and **206s** to discover they should not run. **Estimated impact:** cut failed/skipped implement cost by **1.5–3 min** per affected run. **Confidence:** high.
- **CI is stable but slower than necessary, and one transient dependency fetch already caused a hard failure.** `ci` runs are consistently around **606–642s**; run **25249161547** failed in **13s** because `curl` to the pinned actionlint release returned **502** during `Install actionlint`. **Estimated impact:** save **2–6 min** per CI run and reduce avoidable red builds. **Confidence:** medium-high.
- **AI memory is available and cheap, but retrieval quality is limited.** Deep-dive analysis in run **25247218394** observed **122** structured memory telemetry records, **22** retrieves, **81.8%** hit rate, and average retrieval size of **36.9 estimated tokens**; however, keywording was **18 plain / 4 none / 0 llm**, and zero-hit retrieves clustered in expensive flows. **Estimated impact:** modest latency/token improvement and better consistency in review/analysis paths. **Confidence:** high.
- **Workflow-log-analysis itself is expensive enough to optimize.** Slow analysis runs hit **4828s** (**25246650500**) and **6075s** (**25247218394**); the sampled analysis telemetry shows a single `summarize_unselected_runs` operation using **156,314 tokens**. **Estimated impact:** major savings on observability overhead with low product risk. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Replace long fixed polling loops in `test_and_mark_stable` with run-ID-aware adaptive waits
- **Evidence:**  
  - Workflow family `test_and_mark_stable`: **avg 3211.5s**, **p50 2647s**, **p95 5765.8s**, **0 successes in 6 runs**.  
  - Failed run **25247210528** lasted **6478s**.  
  - Its `e2e-smoke-test` loop repeatedly polled downstream state and then sat on `Evaluate review gate` with idle counters climbing while log size stayed fixed at **29776 bytes** and only **1 review** was present.
- **Root cause:** Serial orchestration waits on downstream workflows using tight polling, with no aggressive terminal-state short-circuit once the review path is clearly stuck.
- **Exact change:**  
  1. Poll by downstream `run_id` with exponential backoff after the first few reads.  
  2. Stop polling once a downstream run reaches a stable terminal condition that makes success impossible.  
  3. Distinguish “review gate only” from “editor actually ran” so Phase 4b can fail fast.
- **Estimated time savings:** **20–40 min** per release-validation cycle.
- **Implementation risk:** **Low-medium**.

### 2. Add a cheap preflight gate before full `implement` bootstrap
- **Evidence:**  
  - Run **25247302521** took **179s** only to conclude `Issue #1968 is closed.`  
  - Run **25247310871** took **206s** only to conclude `Issue #1966 is not in ai:awaiting-approval phase.`  
  - Both still paid runner wait/setup and workflow bootstrap costs.
- **Root cause:** `implement` performs expensive setup before checking conditions that can be evaluated with one lightweight API read.
- **Exact change:**  
  1. Add an initial minimal job that checks issue state, labels, approval status, and branch preconditions.  
  2. Only start checkout/Serena/Codex setup if that preflight returns runnable.  
  3. Emit the skip reason in job summary from the preflight stage.
- **Estimated time savings:** **2–3 min** per skipped implement run.
- **Implementation risk:** **Low**.
- **Critical-path status:** **Critical-path win** for implement-triggered flows.

### 3. Bail out of `implement` exploration loops earlier and switch to a compact recovery prompt
- **Evidence:**  
  - Failed run **25246727158** shows: Serena setup, `serena.activate_project`, attempt 1 no-op warning, attempt 2 no-op warning, then `Codex produced no actionable output 2 attempts in a row`.  
  - Attempt 2 alone recorded **14,134 tokens** before failure.  
  - Failed run **25243569299** also ended by recording `phase_failed` after full bootstrap and no useful output.
- **Root cause:** Retries are spending near-full context budgets on essentially the same stuck exploration pattern.
- **Exact change:**  
  1. After the first no-op/announced-edit-without-changes attempt, stop the normal retry path.  
  2. Re-issue one compact fallback prompt: task, allowed files, expected artifact, and “return a patch or explicit blocker.”  
  3. If still no-op, fail immediately with diagnostics.
- **Estimated time savings:** **90–150s** per failed implement run.
- **Implementation risk:** **Low**.
- **Critical-path status:** **Critical-path win**.

### 4. Tier `review_autofix` before full multi-model fanout
- **Evidence:**  
  - `review_autofix`: **68 runs**, **37 cancelled**, **p50 39.5s**, **p95 1536.1s**.  
  - Slow run **25247372376** lasted **2605s**.  
  - Recent docs/comment-only runs **25249154597** and **25249161567** finished in **25s** and **17s**, proving fast gated paths already exist and work.
- **Root cause:** Expensive review preparation still happens for cases that can be safely handled by deterministic skip or comment-only logic.
- **Exact change:**  
  1. Move diff-size, docs-only, branch-mode, and force-review checks ahead of reviewer-panel initialization.  
  2. Use a 1-reviewer or comment-only lane for small/doc-only changes.  
  3. Reserve full panel + editor + judge only for non-trivial diffs.
- **Estimated time savings:** **10–30 min** on low-complexity review runs.
- **Implementation risk:** **Low-medium**.
- **Critical-path status:** **Critical-path win** for PR paths.

### 5. Break the monolithic `ci/lint` job into parallel checks
- **Evidence:**  
  - `ci` family: **avg 601.9s**, **p50 615s**, **p95 649.4s**.  
  - Runs **25248916253**, **25248540381**, **25248188172**, **25247334589** all show the `lint` job dominating runtime for roughly **595–635s**.
- **Root cause:** Syntax checks, workflow checks, and Python/unit suites share one long runner slot.
- **Exact change:**  
  1. Split into at least: workflow/schema lint, shell/python syntax, and test suites.  
  2. Keep the currently pinned tools and commands, only parallelize execution.  
  3. Preserve the existing required-check names via a thin aggregate job if needed.
- **Estimated time savings:** **2–6 min** wall-clock per CI run, depending on queue conditions.
- **Implementation risk:** **Medium** because more jobs can increase queue sensitivity.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Reduce reviewer panel size and reasoning level for trivial `review_autofix` cases
- **Evidence:**  
  - Slow run **25247372376** used **6 reviewer models** plus `MODEL_EDITOR: openai/gpt-5.3-codex` and `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`.  
  - Config in recent runs still shows `REVIEWER_REASONING_EFFORT: xhigh` and `EDITOR_REASONING_EFFORT: xhigh`.  
  - The family has extremely wide runtime spread, which is typical of over-provisioned review on many easy diffs.
- **Root cause:** The same expensive reviewer/editor profile is available even when deterministic skip or a minimal panel would suffice.
- **Exact change:**  
  1. Use **tiered review modes**: docs-only/comment-only, light review, full review.  
  2. Downgrade reasoning from `xhigh` to `medium`/`high` for docs-only or tiny diffs.  
  3. Disable second-pass reviewer behavior unless the first pass finds substantive issues.
- **Estimated savings:** **30–60%** of review tokens on low-complexity PRs.
- **Quality-risk notes:** Low if gated by diff classification and fail-open escalation to full mode.

### 2. Shrink `workflow_log_analysis` model spend by summarizing less and reusing existing run summaries
- **Evidence:**  
  - Run **25246650500** lasted **4828s**.  
  - Run **25247218394** lasted **6075s**.  
  - Telemetry inside **25247218394** shows `summarize_unselected_runs` used **156,314 tokens** to summarize **80** runs out of **100** targeted.
- **Root cause:** The analysis workflow spends large model budget summarizing wide windows even when `log_summary` already exists for many runs.
- **Exact change:**  
  1. Prefer stored `log_summary` for normal runs and deep-dive only anomaly candidates.  
  2. Cap unselected-run summarization by family and novelty, not just by raw count.  
  3. Drop repeated instructions and embed metrics tables once, not per pass.
- **Estimated savings:** **100k+ tokens** per expensive analysis run.
- **Quality-risk notes:** Low if deep-dive sampling remains for failures and outliers.

### 3. Remove repeated prompt/context expansion in `implement`
- **Evidence:**  
  - Failed implement logs show repeated injection of Serena/Git MCP instruction blocks and repeated full issue/task context across attempts.  
  - Run **25246727158** still failed after repeated Serena-first guidance and retry nudges, with attempt 2 alone at **14,134 tokens**.
- **Root cause:** Retry attempts rebuild near-identical prompts with dynamic noise but little new information.
- **Exact change:**  
  1. Separate a stable prompt prefix from dynamic attempt-specific suffixes.  
  2. Keep task/allowed-files/memory context once; append only retry delta.  
  3. Strip duplicated Serena instruction sections before retry.
- **Estimated savings:** **20–40%** of implement tokens on retrying runs.
- **Quality-risk notes:** Low; quality usually improves when prompts are shorter and less repetitive.

### 4. Use early skip preflight to avoid spending Codex setup on non-runnable `implement` jobs
- **Evidence:**  
  - Runs **25247302521** and **25247310871** spent **179s** and **206s** for skip outcomes.  
  - These runs still initialized model/runtime context and workflow tooling.
- **Root cause:** Non-runnable issues enter the expensive Codex path.
- **Exact change:** Preflight state/label/approval checks before Codex setup.
- **Estimated savings:** Not mostly token-driven, but meaningful runner and setup cost reduction across the **149 “other”** implement runs.
- **Quality-risk notes:** None if the preflight remains purely conservative.

### 5. Prevent avoidable reruns from transient dependency downloads
- **Evidence:**  
  - `ci` run **25249161547** failed because the pinned `actionlint` tarball download returned **502**.  
  - Every rerun of a failed CI or release-validation path fans out more downstream work.
- **Root cause:** External transient fetches are hard-fail points with no local retry/cached fallback.
- **Exact change:**  
  1. Add bounded exponential retry to actionlint install.  
  2. Cache the verified binary between runs.  
  3. Keep SHA verification on every restored/downloaded artifact.
- **Estimated savings:** Small direct token savings, but high indirect savings from fewer reruns.
- **Quality-risk notes:** Very low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Fix the E2E editor-bait path so “review passed” cannot mask “editor never ran”
- **Failure evidence:**  
  - Failed release-validation run **25247210528** reported:  
    - `✓ Review ..... PASSED`  
    - `✗ Editor Bait. FAILED (bait_remained)`  
  - The same run then spent minutes polling `Evaluate review gate` without evidence of editor progress.
- **Root cause category:** Orchestration / gating race.
- **Exact fix:**  
  1. After bait injection, require proof that `review.codex-agent` started, not just `review gate`.  
  2. Block success if the PR is still only in gate evaluation after a short threshold.  
  3. Ensure `force-review` + `e2e-smoke-test` labels are applied and observed before bait push is considered valid.
- **Expected reliability impact:** High improvement to stable-release confidence; likely eliminates the observed false-pass/late-fail mode.
- **Rollback / fail-open:** Fail closed in E2E only; do not affect production review behavior.

### 2. Add a deterministic failover for `implement` no-actionable-output loops
- **Failure evidence:**  
  - Implement failures **25246727158**, **25243569299**, **25237690797**, **25237704374**, **25244121942**, **25244127789**, **25245077011**, **25245085089** all failed in `Run Codex implementation`.
- **Root cause category:** Agent loop / prompt-control failure.
- **Exact fix:**  
  1. After first no-op attempt, switch to compact recovery prompt.  
  2. If second attempt still no-op, emit structured blocked result and route back to clarify/human follow-up rather than continuing exploration.  
  3. Persist “no-actionable-output” as a first-class failure reason in summaries.
- **Expected reliability impact:** Significant reduction in current `implement` family failure rate (**4.97%**).
- **Rollback / fail-open:** Fail-open to existing behavior behind a flag for one release if needed.

### 3. Make `actionlint` installation resilient to transient GitHub release errors
- **Failure evidence:**  
  - CI run **25249161547** failed with `curl: (22) The requested URL returned error: 502` during `Install actionlint`.
- **Root cause category:** External dependency fetch / transient network.
- **Exact fix:**  
  1. Wrap the `curl` download in 3-attempt exponential backoff.  
  2. Reuse a verified cached binary when available.  
  3. Keep SHA256 verification mandatory.
- **Expected reliability impact:** Removes a known flaky red-build source with minimal code change.
- **Rollback / fail-open:** If download ultimately fails, surface the same error as today.

### 4. Surface failing fixtures directly in nightly validation output
- **Failure evidence:**  
  - Nightly self-test run **25242537588** ended with `fixtures=3 passed=1 failed=2`, uploaded **70 files** of artifacts, but the primary failure signal in logs is still coarse.
- **Root cause category:** Test observability / triage friction.
- **Exact fix:**  
  1. Print failing fixture names and failing stages directly in the step summary.  
  2. Preserve artifact upload, but make the top-level summary actionable without download.  
  3. Optionally soft-fail known experimental fixtures behind an allowlist.
- **Expected reliability impact:** Faster MTTR rather than direct fail-rate reduction.
- **Rollback / fail-open:** None; purely additive reporting.

### 5. Fix telemetry helper staging on cancelled review runs
- **Failure evidence:**  
  - Cancelled run **25249143529** logged `memory helper script missing; skipping run-end failure event`.
- **Root cause category:** Observability / cleanup-path drift.
- **Exact fix:** Ensure memory helper scripts are staged before any path that may cancel and guarantee cleanup-path access.
- **Expected reliability impact:** Better ledger completeness and failure analysis; low direct user-facing impact.
- **Rollback / fail-open:** Existing fail-open behavior is acceptable if staging still fails.

## AI Memory Health

- **Telemetry observed:** Yes. Deep-dive analysis run **25247218394** reported **122 structured `AI_MEMORY_TELEMETRY` records** across the sampled window.
- **Operation mix observed in that run:**  
  - `record-run-event`: **49**  
  - `retrieve`: **22**  
  - `processed-command-check`: **20**  
  - `processed-command-claim`: **19**  
  - `summarize_unselected_runs`: **8**  
  - `record-candidate`: **4**
- **Retrieve effectiveness:**  
  - **Hit rate:** **81.8%** (`18/22` retrieves returned `records_selected > 0`)  
  - **Average estimated tokens:** **36.9**  
  - **Keyword method distribution:** `plain=18`, `none=4`, `llm=0`
- **Flags:**  
  - **Zero-hit retrieves:** **4** total in the sampled window; one was directly visible in slow review run **25247372376** as `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: "none"`, role `reviewer`.  
  - **`fail_open: true` entries:** none observed in the aggregated telemetry.  
  - **`enabled: false` entries:** none observed in the aggregated telemetry.  
  - **High push retries (`push_attempts > 1`):** **3** in the sampled analysis; all were in `implement` flows.
- **Other direct observations:**  
  - Orchestrate poll runs **25248221371** and **25248717590** successfully emitted `record-run-event` telemetry for both `poll_started` and `poll_completed`.  
  - Memory maintenance run **25249009624** emitted a successful `compact` event with **2914 archived candidates** and `did_push: true`.
- **Assessment:**  
  - Memory is **enabled and functioning**.  
  - Retrieval is **cheap** but not very sophisticated; the system is relying entirely on `plain`/`none` keywording in sampled runs.  
  - The most valuable improvement is **better fallback retrieval** on expensive review/analysis paths when the first lookup hits zero.
- **Recommendation:**  
  1. Keep current cheap retrieval as default.  
  2. On zero-hit retrieve in `review_autofix` and `workflow_log_analysis`, run one richer second-stage retrieval.  
  3. Continue tracking zero-hit rate and push-retry rate by workflow family.

## GH API Call Audit

### Highest-volume patterns

1. **`test_and_mark_stable` downstream polling loops**
   - **Evidence:** Slow/cancelled test-and-mark-stable runs repeatedly call endpoints such as:
     - `repos/${TEST_REPO}/actions/workflows/${WF_FILE}/runs?per_page=10`
     - `repos/${TEST_REPO}/actions/runs/${NEW_ID}`
     - `repos/${REPO}/actions/runs/${RID}`
   - **Impact:** High call volume plus long waits; also increases rate-limit exposure.
   - **Change:** Replace fixed-interval polling with adaptive polling keyed to known downstream run IDs.
   - **Estimated reduction:** Large; likely **dozens to hundreds** fewer API reads per long release-validation run.

2. **`review_autofix` PR gate repeatedly fetching overlapping PR metadata**
   - **Evidence:** In recent runs **25249154597** and **25249161567**, the gate step fetches:
     - `repos/${REPOSITORY}/pulls/${PR_NUMBER}`
     - `repos/${REPOSITORY}/commits/${PR_HEAD_SHA}`
     - paginated `repos/${REPOSITORY}/pulls/${PR_NUMBER}/files`
   - **Root issue:** The same PR-level facts are recomputed inside the gate path instead of being fetched once and reused across review stages.
   - **Change:** Fetch PR metadata/files once into a gate artifact or env file and reuse it in editor/post-merge logic.
   - **Estimated reduction:** **2–3 GH API calls per review run**, plus one paginated file-list call.

3. **`cancel_on_pr_close` rate-limit checks before cancellation scans**
   - **Evidence:** Run **25249161562** uses `_gh_retry` plus `gh api -i /rate_limit` and POST cancel endpoints.
   - **Assessment:** Logic is safe, but the rate-limit probe can be cached per loop iteration instead of re-evaluated more often than needed.
   - **Change:** Cache one rate-limit snapshot per job iteration unless a 429/secondary-limit error actually occurs.
   - **Estimated reduction:** Small call-count reduction, modest rate-limit-risk reduction.

4. **`orchestrate_poll` paying both API and full-git sync cost**
   - **Evidence:** Poll runs **25248221371** and **25248717590** use rate-limit-aware `gh issue list` logic and also `actions/checkout@v5` with `fetch-depth: 0`, pulling many branches/tags.
   - **Root issue:** The polling job does lightweight coordination but fetches full repository history.
   - **Change:** Use shallow checkout unless a later step explicitly needs full history/tags.
   - **Estimated reduction:** Not just API; strong reduction in git network traffic and elapsed time.

5. **Artifact lookup/download churn in review/analysis flows**
   - **Evidence:**  
     - Copilot review summaries mention artifact endpoint calls for runs **25247335697** and **25247330108**.  
     - Workflow-log-analysis run **25247218394** downloaded multiple artifacts in sequence.
   - **Change:** Avoid artifact enumeration when artifact names are deterministic; request only known artifacts and skip absent optional artifacts fail-open.
   - **Estimated reduction:** Moderate in analysis/review utility workflows.

### Repository-specific API hygiene observations

- **Batching:** Review and release flows still have per-run/per-phase polling patterns that should be batched or widened less often.
- **Cycle-local caches:** PR metadata, changed-file lists, and issue linkage queries should be cached within a run and reused.
- **Fail-open behavior:** Generally healthy; several scripts already retry or warn instead of hard-failing. Keep that behavior, especially on analysis and notification helpers.

## MCP & Serena Efficiency

- **Serena is installed and activated, but successful usage does not guarantee actionable edits.**
  - Failed implement run **25246727158**:
    - set up Serena successfully,
    - called `serena.activate_project(...)`,
    - then still failed after **2 no-op attempts**.
  - The same run’s Serena efficiency report showed strong theoretical savings:
    - top tools: `replace_symbol_body (40)`, `insert_after_symbol (40)`, `get_symbols_overview (33)`, `find_symbol (33)`, `find_referencing_symbols (28)`
    - estimated tokens with Serena: **~19,050**
    - estimated tokens without Serena: **~162,100**
- **Another failed implement run still showed very heavy tool churn.**
  - Run **25237690797** produced:
    - **273 Serena tool calls**
    - **66% Serena efficiency**
    - estimated tokens with Serena **~90,050**
    - estimated tokens without Serena **~245,800**
  - Yet it still failed.
- **Main inefficiency:** too much navigation/edit-tool churn before first durable edit.
  - The logs repeatedly inject Serena guidance, activate the project, and encourage edit tools, but the model still ends in “announced edit/apply_patch without changes.”
- **Git MCP is disabled where it could help.**
  - Implement logs show `GIT_MCP_DISABLED: true`.
  - Review/autofix already has preloaded `gh_pr_diff` artifacts (`PR_DIFF_SOURCE: gh_pr_diff` in **25247372376**), but scoped Git MCP reads would still help when diff/status context must be refreshed.
- **Concrete changes:**
  1. Add a **tool-churn guardrail**: if Serena symbol lookups exceed a threshold without any edit, switch to a direct patch-only instruction.
  2. Make `tool_usage_stats.json` emission mandatory when Serena is enabled; some failed runs reported “No Serena tool usage stats found.”
  3. Trim duplicate Serena/Git MCP instruction blocks from retry prompts.
  4. Enable existing **read-only Git MCP** where already supported, starting in review/edit flows; keep current diff artifacts as fallback.
  5. Allow safe parallel read operations only for independent symbol/file discovery, then serialize edits.
- **Expected impact:** lower token spend, faster time-to-first-edit, and fewer exploration-loop failures.

## Prompt Cache & Memory System

- **Prompt cache state:**  
  - Sampled runs consistently showed `OPENROUTER_PROMPT_CACHE_DISABLED: false`, so caching is intended to be enabled.
  - However, the sampled deep-dive logs did **not** emit prompt-cache create/read/hit counters, so hit rate cannot be quantified from this window.
- **Observed fragmentation risks:**
  1. **Repeated prompt bodies across retries** in `implement`, with small dynamic differences.
  2. **Duplicated Serena/Git MCP instruction blocks** in failed implement logs.
  3. **Per-step env/context noise** repeated across review and poll flows, which can destabilize prompt prefixes if injected into model-facing content.
- **Memory system behavior:**  
  - Memory retrieval is cheap and mostly healthy.  
  - The main weakness is recall sophistication on expensive paths, not system availability.
- **Concrete improvements:**
  1. Make the first **70–80% of model prompts stable** across retries; append dynamic attempt info at the end.
  2. Deduplicate injected instruction blocks before prompt assembly.
  3. Emit explicit prompt-cache metrics per model call: created, read, hit/miss, and fail-open.
  4. On zero-hit memory retrieve for `review_autofix` and `workflow_log_analysis`, run one richer follow-up retrieval.
- **Estimated impact:**  
  - **Tokens:** moderate savings in retry-heavy flows.  
  - **Latency:** small-to-moderate.  
  - **Reliability:** moderate, because fewer cache misses and clearer prompt structure should reduce no-op loops.
- **Evidence gap:** prompt-cache hit/miss counters were not present in the sampled logs; this window is sufficient for directionally useful findings, but not for quantified cache-hit analysis.

## Orchestrator Health

- **Overall health:** Core orchestrator workflows are mostly succeeding:
  - `orchestrate`: **6/6 success**, avg **236.7s**
  - `orchestrate_poll`: **13/13 success**, avg **42.9s**
  - `validate`: **6/6 success**, avg **108s**
- **Main pain points:**
  1. **Excess skip churn**  
     - `clarify`: **217 runs**, only **30 success**, **187 other/skipped**  
     - `plan`: **182 runs**, **24 success**, **158 other/skipped**  
     - `orchestrate_clarify_respond`: **182 runs**, **6 success**, **176 other/skipped**  
     - `implement`: **181 runs**, **149 other/skipped**
  2. **Runner wait before early exits**  
     - Several implement and review runs wait for runners before discovering they should skip.
  3. **State/phase mismatch propagation**  
     - E2E cleanup comments triggered multiple no-op command-family runs around **25247210528**, indicating event fanout still creates operational noise.
- **Smallest safe mitigations:**
  1. Add a **single lightweight router/preflight** for comment-driven workflows before spawning heavier jobs.
  2. Move issue/label/phase eligibility checks ahead of checkout and model setup.
  3. Record skip reasons in a compact ledger so repeated non-actionable comment patterns can be observed and reduced.
- **Indicators to track after changes:**
  - skipped-run count by workflow family,
  - avg runner-start latency for skipped runs,
  - implement preflight-pass rate,
  - percent of review runs entering full reviewer fanout.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Validate / release path bottleneck
- **Stage:** validate -> e2e smoke -> review wait -> verify bait removed
- **Dominant overhead:** polling + downstream workflow waits + late failure detection
- **Evidence:** run **25247210528** failed after **6478s**
- **Fix:** adaptive polling, early impossible-state detection, and explicit editor-run verification

### 2. Review/autofix compute bottleneck
- **Stage:** review/autofix
- **Dominant overhead:** multi-model reviewer fanout, high reasoning levels, summarizer setup, late editor no-op outcomes
- **Evidence:** runs **25247372376** (**2605s**) and **25247334655** (**1632s**)
- **Fix:** tiered review paths, smaller panel for trivial diffs, earlier deterministic skip

### 3. Implement bootstrap + retry bottleneck
- **Stage:** implement
- **Dominant overhead:** runner wait, Serena/Codex setup, then no-op retries or late skip decisions
- **Evidence:**  
  - failed no-op run **25246727158**  
  - skipped-after-setup runs **25247302521** and **25247310871**
- **Fix:** preflight gating + compact retry path

### 4. CI monolith bottleneck
- **Stage:** CI / lint
- **Dominant overhead:** long single-job compute lane
- **Evidence:** many runs between **606–642s**
- **Fix:** parallel split of checks, resilient tool bootstrap

### 5. Queueing bottleneck on lightweight coordination jobs
- **Stage:** poll / status sync / merge helpers
- **Dominant overhead:** hosted-runner pickup and full repo sync for lightweight tasks
- **Evidence:** poll runs **25248221371** and **25248717590** both note runner wait and full-history checkout
- **Fix:** shallower checkout, fewer spawned workflows, more work in lightweight preflight jobs

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `test_and_mark_stable` release validation
  - `review_autofix` long-running/cancelled review fanout
  - `ci` monolithic 10-minute lint/test lane
  - `implement` setup and retry waste on skips/no-op failures
- **Top failure modes**
  - Editor bait remains after review path appears to pass (**25247210528**)
  - Codex no-actionable-output loops in implement (**25246727158** and peers)
  - Transient external download failures in CI (`actionlint` 502 in **25249161547**)
  - Nightly self-test fixture failures (**25242537588**)
- **Highest-cost drivers**
  - Multi-model `review_autofix` with `xhigh` reasoning
  - `workflow_log_analysis` summarization passes
  - repeated implement retry prompts and Serena setup
- **Top 3 prioritized actions**
  1. **Fix `test_and_mark_stable` Phase 4/4b detection and reduce polling**
  2. **Tier `review_autofix` and reserve full multi-model review for non-trivial diffs**
  3. **Add `implement` preflight + compact no-op recovery path**

## Metrics Appendix

### Overall repository metrics

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 263 | 12 | 51 | 674 | 1.2% | 116.8 | 1.0 | 620.0 |

### Key workflow family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Other | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 63 | 62 | 1 | 0 | 0 | 601.9 | 615.0 | 649.4 |
| implement | 181 | 17 | 9 | 6 | 149 | 28.8 | 1.0 | 202.0 |
| review_autofix | 68 | 29 | 0 | 37 | 2 | 260.6 | 39.5 | 1536.1 |
| test_and_mark_stable | 6 | 0 | 1 | 5 | 0 | 3211.5 | 2647.0 | 5765.8 |
| workflow_log_analysis | 6 | 3 | 0 | 3 | 0 | 3830.8 | 3448.0 | 5763.3 |
| orchestrate_poll | 13 | 13 | 0 | 0 | 0 | 42.9 | 42.0 | 46.4 |
| plan | 182 | 24 | 0 | 0 | 158 | 12.9 | 1.0 | 141.5 |
| clarify | 217 | 30 | 0 | 0 | 187 | 19.3 | 1.0 | 127.0 |
| orchestrate_clarify_respond | 182 | 6 | 0 | 0 | 176 | 1.34 | 1.0 | 2.0 |
| validation_refresh | 3 | 3 | 0 | 0 | 0 | 210.3 | 207.0 | 218.7 |

### Notable run outliers

| Run ID | Workflow family | Conclusion | Duration (s) | Key issue |
|---|---|---|---:|---|
| 25247210528 | test_and_mark_stable | failure | 6478 | Editor bait remained; release blocked |
| 25247218394 | workflow_log_analysis | success | 6075 | Analysis overhead very high |
| 25246650500 | workflow_log_analysis | success | 4828 | Expensive summarization/analysis run |
| 25247372376 | review_autofix | success | 2605 | Full review fanout; editor ended suspicious no-op |
| 25246727158 | implement | failure | 184 | 2 no-actionable-output attempts |
| 25249161547 | ci | failure | 13 | actionlint download 502 |

### AI memory telemetry metrics

| Metric | Value | Evidence source |
|---|---:|---|
| Structured telemetry records | 122 | workflow_log_analysis run 25247218394 |
| Retrieve ops | 22 | workflow_log_analysis run 25247218394 |
| Retrieve hit rate | 81.8% (18/22) | workflow_log_analysis run 25247218394 |
| Avg retrieve estimated tokens | 36.9 | workflow_log_analysis run 25247218394 |
| Keyword method: plain | 18 | workflow_log_analysis run 25247218394 |
| Keyword method: none | 4 | workflow_log_analysis run 25247218394 |
| Keyword method: llm | 0 | workflow_log_analysis run 25247218394 |
| Zero-hit retrieves | 4 | workflow_log_analysis run 25247218394 |
| `fail_open: true` entries | 0 observed | workflow_log_analysis run 25247218394 |
| `enabled: false` entries | 0 observed | workflow_log_analysis run 25247218394 |
| High push retries (`push_attempts > 1`) | 3 | workflow_log_analysis run 25247218394 |

### GH API hotspot summary

| Workflow / run | Hotspot pattern | Observed issue | Recommended change |
|---|---|---|---|
| test_and_mark_stable / 25247210528 and peers | repeated `actions/runs` polling | long wait loops, high API churn | adaptive run-ID polling |
| review_autofix / 25249154597, 25249161567 | PR GET + commit GET + paginated files | overlapping metadata fetches | cycle-local cache of PR facts |
| orchestrate_poll / 25248717590 | `gh issue list` + rate-limit checks + full checkout | coordination job too heavy | shallow checkout, cached rate-limit snapshot |
| cancel_on_pr_close / 25249161562 | `/rate_limit` + cancel POST | safe but chatty | cache rate-limit state per loop |
| workflow_log_analysis / 25247218394 | repeated artifact downloads | analysis overhead | request only deterministic required artifacts |

### Prompt/cache observations

| Signal | Observation |
|---|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | consistently `false` in sampled model workflows |
| Prompt cache hit/miss counters | not emitted in sampled deep-dive logs |
| Cache-fragmentation risk | high in `implement` due repeated retry prompts and duplicated instruction blocks |
| Memory retrieval cost | low |
| Memory retrieval sophistication | limited (`plain`/`none` only in sample) |


## Deep Audit — Workflows & Scripts (2026-05-02)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1214-1359`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The `wait-review` poller can declare the review phase good enough **before the editor outcome is known**. It exits successfully when the downstream review run concludes `failure` (`status=completed_with_findings`, lines 1220-1224), when failed steps appear while cleanup is still running (1241-1246), and even while the run is still `in_progress` once a reviewer majority is observed (1355-1359). None of those exits require proof that the editor ran, produced a validated summary, or removed the bait commit. In an E2E gate, that is a correctness bug: Phase 4 can advance on reviewer-only evidence and only fail later when bait verification discovers the editor never completed.  
  **Recommended fix** — Make success contingent on an **editor-complete signal**, not reviewer majority alone. Concretely: keep reviewer-majority and failed-step checks as diagnostics only, and do not emit `status=success` / `completed_with_findings` until one of these is true: (a) the review workflow completed successfully, (b) the editor emitted a non-noop completion marker, or (c) the PR head changed in a way that proves the bait was removed. Reuse the existing `EDITOR_NOOP_SUSPICIOUS`/bait-aware signals rather than inventing a new heuristic.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/review_autofix.yml:3763-3770,3884-3891,4618-4625; scripts/review_rb_judge.sh:153-156`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — Several fallback paths still treat bare prose references like `issues/123` and `issue #123` as linked issues when `LINKED_ISSUES_JSON` is empty. Those regexes are broader than the stricter contract already documented and implemented in `.github/workflows/issue_pr_status.yml:196-210`, which explicitly avoids bare references because they can be incidental documentation mentions. In `review_autofix` and `review_rb_judge`, the broader parser can therefore apply `ai:ready-to-merge` or `ai:review-blocked` to unrelated issues mentioned in PR text.  
  **Recommended fix** — Extract one shared helper for PR-text fallback issue extraction and make it match the stricter `issue_pr_status.yml` contract: only repo-scoped URLs/paths and supported closing-keyword forms. Then replace the duplicated broad regexes in `review_autofix.yml` and `scripts/review_rb_judge.sh` with that helper.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/review_autofix.yml:1371-1406`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — `Collect PR metadata` makes five logical GitHub reads on the normal path: `pulls/{pr}` (1371), issue comments (1372-1373), reviews (1374-1375), review comments (1376-1377), and a separate GraphQL `closingIssuesReferences` fetch (1401-1406). The repo already has a GraphQL-first consolidator in `scripts/gh_helpers.sh:735-900` (`gh_pr_with_all_comments`) that batches PR meta + comments into one call, but this step re-fetches the same shapes manually.  
  **Current call count** — 5 logical API calls on the happy path.  
  **Proposed call count after fix** — 1 logical API call by extending `gh_pr_with_all_comments` to also return `closingIssuesReferences { number title body }`.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh::gh_pr_with_all_comments`.  
  **Recommended fix** — Move this step onto `gh_pr_with_all_comments` and add linked issues to that helper’s GraphQL payload. Write the returned JSON once, then derive `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEWS_FILE`, `PR_REVIEW_COMMENTS_FILE`, and linked-issue context from that single cached payload.

- **ID** — `BATCH-001`  
  **File path** — `scripts/review_rb_judge.sh:146-166,191-208`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The judge first fetches only linked issue numbers via GraphQL (146-151), then loops over each issue and fetches its body individually (161-166). That is a per-item API loop in a hot path. In the normal case the script also fetches PR diff (191-192) and PR discussion context via `gh_pr_with_all_comments` or its REST fallback (200-208).  
  **Current call count** — `N+3` logical calls in the common case: 1 linked-issue GraphQL call + `N` issue-body fetches + 1 PR diff call + 1 PR-context call.  
  **Proposed call count after fix** — 2 logical calls: 1 extended PR-context GraphQL call that already includes linked issue numbers/titles/bodies, plus 1 PR diff call.  
  **Existing batching pattern to extend** — `scripts/gh_helpers.sh::gh_pr_with_all_comments`; alternatively mirror the aliased batching shape used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — Extend the existing PR-context helper to return linked issue bodies in the same GraphQL response, then remove the per-issue `_safe_gh_jq "repos/.../issues/{n}"` loop entirely.

- **ID** — `API-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1188-1435`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The review wait loop re-reads overlapping state every poll cycle. Once a review run is found, a typical iteration performs: 1 `actions/runs` listing (1188-1193), 1 `actions/runs/{id}/jobs` fetch (1235), 1 `pulls/{pr}` fetch (1384-1385), 1 `pulls/{pr}/comments` fetch (1388-1389), and 1-2 `actions/jobs/{job_id}/logs` downloads (1258-1354 and 1402-1403). That means the same loop can download the same job log twice and the same PR state on every iteration. `[NEEDS VERIFICATION]` The exact count varies by branch of the loop, but the redundant shape is present in-code.  
  **Current call count** — ~4-6 logical API reads per poll iteration.  
  **Proposed call count after fix** — ~2-3 logical API reads per iteration by pinning a single downstream `run_id`, reusing one `JOBS_JSON` payload, and reusing one log download for both marker grep and byte-count accounting.  
  **Existing batching pattern to extend** — The cycle-local cache pattern used in `scripts/orchestrate_poll_process.sh` (`_candidate_details_json`, linked-PR caches).  
  **Recommended fix** — Cache the chosen review `run_id` after first discovery, carry one per-iteration PR snapshot object instead of separate pull/comments reads, and make the log fetch populate both “marker present?” and “log size changed?” checks from the same local file.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:239-250; .github/workflows/review_autofix.yml:3718-3778,3840-3904,4580-4633`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The same fallback label-mutation helpers are redefined inline in multiple jobs: `ensure_label_exists` and `set_issue_phase_label_resilient` appear as local stubs in `issue_pr_status.yml` and in three separate `review_autofix.yml` paths. The bodies are near-identical but not fully identical, which raises drift risk in label semantics and retry behavior.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner of:  
  `ensure_label_exists <label_name> <repo>`  
  `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`  
  Then update callers in `issue_pr_status.yml` and `review_autofix.yml` to source that module once and remove the inline fallback bodies.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/review_autofix.yml:848-938; .github/workflows/validate.yml:188-338`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — Workflow-support bootstrap logic is implemented twice: both workflows resolve a source repo/ref, clone or reuse a support checkout, copy scripts/prompts/schemas into a runtime directory, and apply fallback-to-`main` rules. The two copies are already behaviorally different, so future fixes to support-file staging will have to be kept in sync by hand.  
  **Recommended fix** — Extract a shared module such as `scripts/fetch_workflow_support.sh` with a signature like:  
  `fetch_workflow_support <workflow_name> <script_ref> <dest_root> <required_list_file> <optional_list_file>`  
  Update `review_autofix.yml` and `validate.yml` to call that module rather than maintaining parallel shell implementations.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The interpolated `wait-review` `run:` block is about **19,696 characters**, which leaves only **1,304 characters of headroom** before GitHub’s 21,000-character expression hard stop. This repo has already hit that limit repeatedly; this block is now inside the danger zone.  
  **Recommended fix** — Extract the entire review wait loop into an external script under `scripts/` and pass only small env vars/arguments from YAML. That is the safest option because this block is already comment-heavy and likely to keep growing.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/validate.yml:188-481`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The interpolated `Fetch workflow support files` block is about **16,529 characters**, leaving **4,471 characters of headroom**. It embeds substantial shell logic, multi-branch fallback behavior, and long file lists, so normal maintenance could push it across the limit.  
  **Recommended fix** — Move the support-bootstrap logic into a dedicated script (preferred) or split it into smaller steps: ref resolution, checkout/copy, and optional asset staging.

- **ID** — `EXPR-003`  
  **File path** — `.github/workflows/review_autofix.yml:1286-1608`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The interpolated `Collect PR metadata` block is about **16,437 characters**, leaving **4,563 characters of headroom**. It combines retry helpers, no-PR branch mode, linked-issue GraphQL, PR-context serialization, and diff acquisition in one YAML expression.  
  **Recommended fix** — Extract this step to `scripts/review_collect_pr_metadata.sh` or split it into smaller workflow steps: PR payload fetch, linked-issues fetch, comments-context assembly, and diff capture.

- **ID** — `EXPR-004`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:845-1128`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The interpolated clarify-response post-processing block is about **15,140 characters**, leaving **5,860 characters of headroom**. It inlines claim-check logic, loop guarding, escalation comment assembly, answer posting, and memory completion recording.  
  **Recommended fix** — Split loop-guard/escalation handling from answer-posting, or extract the whole section into a script under `scripts/` so future clarify-loop changes do not risk a template-length failure.

No workflow file is currently near the **1 MB** workflow-file cap; the largest audited workflow is `review_autofix.yml` at **268,926 bytes**, followed by `test-and-mark-stable.yml` at **229,098 bytes**.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/memory_helpers.sh:226-234`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `memory_promote()` is defined here, but no audited workflow or repository shell script calls it. Within this repository, it is currently dead surface area. `[NEEDS VERIFICATION]` because this helper ships as support code and should be checked against any intended external contract before removal.  
  **Recommended fix** — Confirm whether consumer-facing workflows depend on `memory_promote`. If not, remove it; if yes, add at least one in-repo caller or CI assertion so it remains exercised.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/review_autofix.yml:1289-1327`  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — `review_autofix.yml` defines a bespoke `gh_retry` inside `Collect PR metadata` instead of using the repo-standard `scripts/gh_helpers.sh` helper. That local implementation lacks repo-standard behavior such as permanent-failure detection, JSON-validation helpers, rate-limit circuit breaking, and the Telegram rate-limit alert path implemented in `scripts/gh_helpers.sh`. This makes one of the highest-volume workflows behave differently from the rest of the repository under API stress.  
  **Recommended fix** — Source the staged `gh_helpers.sh` here and use `gh_retry_to_file` / `gh_api_json_to_file` rather than maintaining a local retry dialect.

- **ID** — `SHELL-001`  
  **File path** — `.github/workflows/mark-stable.yml:448-489`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `mark-stable` builds `REPOS` from `jq` output and then iterates with `for REPO in $REPOS; do`. That is classic word-splitting/globbing territory (SC2086-style). Repo slugs are normally safe, but the loop is still fragile against malformed JSON output, stray carriage returns, or future nonstandard entries.  
  **Recommended fix** — Replace the scalar loop with an array-safe form, e.g. `mapfile -t repos < <(jq -r '.[]' "$CONSUMER_FILE")` followed by `for REPO in "${repos[@]}"; do ...; done`.

Pattern sweep note: I did **not** find `TODO`, `FIXME`, or `HACK` markers in `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 8 | BUG-002, API-001, BATCH-001, API-002, DUP-001, EXPR-002, EXPR-003, EXPR-004 |
| Low | 4 | DUP-002, DEAD-001, CONSIST-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/test-and-mark-stable.yml` | Medium |
| API call optimization | `.github/workflows/review_autofix.yml`, `.github/workflows/test-and-mark-stable.yml`, `scripts/review_rb_judge.sh`, `scripts/gh_helpers.sh` | Medium |
| Code modularization | `.github/workflows/issue_pr_status.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/validate.yml`, new shared support helper | Medium |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/validate.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/orchestrate_clarify_respond.yml` | Large |
| Medium/Low fixes | `scripts/memory_helpers.sh`, `.github/workflows/mark-stable.yml`, `.github/workflows/review_autofix.yml` | Small |
