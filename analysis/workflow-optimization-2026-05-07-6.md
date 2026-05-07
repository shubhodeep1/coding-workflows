## Executive Summary

- **The biggest end-to-end latency problem is `test_and_mark_stable`, not the core AI phases.** This family ran only 5 times but had **p50 4,568s, p95 5,235.8s, and 60% failures**; failing runs `25496132733` and `25474642232` spent most of their time waiting on downstream workflow state rather than doing useful work. **Estimated impact:** save **30–70 minutes per failing test run** by failing fast on missing/failed child workflows and stale labels. **Confidence:** high.

- **`review_autofix` is the largest routine compute sink and cancellation sink.** The family shows **104 total runs, 65 cancelled, p95 1,550.3s**. Slow success run `25490929374` took **2,483s** and spent time in long waits plus a full reviewer panel on a claude-branch-review path; several recent cancelled runs (`25503908717`, `25505095936`, `25505535483`) burned **503–1,553s** before being superseded or cancelled. **Estimated impact:** save **4–25 minutes per superseded review run** and materially cut reviewer-model spend with stricter concurrency and lighter comment-only review mode. **Confidence:** high.

- **There is at least one concrete reliability bug in orchestration, not just flaky AI behavior.** Failed run `25473127144` (`orchestrate`) shows Codex failing on **“unexpected argument '--ask-for-approval' found”** on attempts 2 and 3, then recording `orchestration_failed`. **Estimated impact:** likely removes a **20% failure rate** in this family immediately and prevents needless retries. **Confidence:** high.

- **Workflow-log analysis is expensive in tokens even before the final report is generated.** Failed run `25473131401` recorded `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` with **214,237 tokens used** to summarize **85 runs**. **Estimated impact:** saving **100k–200k+ tokens per workflow-log-analysis run** is realistic by caching unchanged summaries and shrinking unselected-run coverage when deep-dive evidence already exists. **Confidence:** high.

- **AI memory retrieval is operational, but weakly effective.** Across deep-dive logs, I found **24 `retrieve` operations** with only **4 hits (16.7%)**, and **20 returns with 0 selected records**; hit-bearing retrieves were tiny, e.g. failed implement run `25496323404` retrieved **2 records / estimated 56 tokens**, while review run `25490929374` retrieved **0 records** with `keyword_method: none`. **Estimated impact:** modest direct speed/cost wins, but meaningful reduction in repeated exploration loops if retrieval quality improves. **Confidence:** medium-high.

- **The repo is over-triggering cheap-but-not-free orchestration wrappers.** Overall repo p50 is only **1s** because many runs are skipped, but the system still launched **166 `implement`**, **196 `clarify`**, **167 `plan`**, and **167 `orchestrate_clarify_respond`** runs in this 1,000-run window, with many immediately skipping on conditions. **Estimated impact:** mostly **runner-queue and API-noise reduction**, plus easier debugging. **Confidence:** medium.

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1) Fail fast in `test_and_mark_stable` when child workflows are missing, failed, or stuck behind stale labels
**Critical-path win**

- **Evidence**
  - `test_and_mark_stable` has **p50 4,568s**, **p95 5,235.8s**, and **3 failures out of 5 runs**.
  - Run `25496132733` failed after **4,599s** at `e2e-alt-model-test` → `Wait for clarify→plan→implement (alt-model)`.
  - In `step-007-e2e-alt-model-test.log`, the run kept observing `labels: ai:implementing` every ~20s and finally errored with **“Alt-model run timed out before reaching review stage.”**
  - The same log then says **“⚠ alt-model clarify run not found (analyser will skip this phase)”**, which means the watcher already had evidence that the expected downstream flow was not healthy.
  - Run `25474642232` failed after **5,395s** at `orphan-workflows-test`; its log warns **“Soft-error analyser wait deadline elapsed … analyser will see partial logs.”**

- **Root cause**
  - Watchers are treating “still waiting” and “downstream state is invalid/missing” too similarly.
  - The control plane is polling labels and run status for too long even after terminal evidence exists.

- **Exact change**
  - In `test-and-mark-stable` watchers:
    1. Treat **missing expected child run** after N polls as terminal failure, not a soft warning.
    2. Treat **implement/clarify failure diagnostics comments** and **`phase_failed`/`orchestration_failed` ledger events** as terminal.
    3. Add a **stale-label TTL**: if `ai:implementing` is unchanged for a bounded time and no active implement run exists, fail immediately with the stale-run evidence.
    4. Stop waiting for downstream review when the required upstream phase never materialized.

- **Estimated time savings**
  - **30–70 minutes per failing `test_and_mark_stable` run**.
  - This is the single highest-impact latency reduction in the sample.

- **Implementation risk**
  - **Low to medium**: watcher logic changes can be isolated and are backward-compatible if you preserve the current success path.

---

### 2) Add stricter concurrency / supersession cancellation for `review_autofix`
**Critical-path win**

- **Evidence**
  - `review_autofix` has **104 total runs**, **65 cancelled**, **32 success**, **0 hard failures**, **p50 57.5s**, **p95 1,550.3s**.
  - Cancelled recent runs include:
    - `25503908717`: **1,553s**
    - `25505095936`: **503s**
    - `25505535483`: **170s**
  - Slow success `25490929374` took **2,483s**.
  - Multiple cancelled runs show the same path: **“reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.”** That means expensive review work is sometimes started on work that is later superseded or cancelled.

- **Root cause**
  - Review work is starting for PR/head states that are later invalidated by new pushes or superseding runs.
  - Existing gating prevents some work, but not enough in-flight work is cancelled early.

- **Exact change**
  - Add / tighten workflow `concurrency` for `review_autofix` keyed by **repository + PR number + head SHA or head ref**.
  - Auto-cancel previous in-progress reviewer-panel runs on `pull_request.synchronize`.
  - If the path is **comment-only / claude-branch-review**, cancel prior same-head reviewer runs before launching the panel.

- **Estimated time savings**
  - **4–25 minutes** per superseded run that would otherwise linger.
  - Also reduces queue pressure for other workflows.

- **Implementation risk**
  - **Low** if keyed correctly and limited to same logical work item.

---

### 3) Split `CI` into parallel fast-check and test jobs
**Critical-path win**

- **Evidence**
  - `ci` family: **70 runs**, **p50 614.5s**, **p95 662.2s**.
  - Recent successful runs:
    - `25505306334`: **640s**
    - `25505094747`: **604s**
    - `25503908556`: **603s**
  - Logs consistently show the `lint` job dominating runtime and running many unrelated checks sequentially:
    - `81 passed, 0 failed, 81 total`
    - `25 passed, 0 failed, 25 total`
    - coverage gates
    - shell/yaml/actionlint/jsonschema/prompt validation
  - `step-001-lint.log` also shows dependency installation at runtime (`pip install yamllint coverage pyyaml jsonschema jinja2`).

- **Root cause**
  - Too many independent validation groups are serialized into one job.
  - The job mixes static checks, Python unit tests, and coverage gates in one critical path.

- **Exact change**
  - Split into at least two parallel jobs:
    1. **Fast lint/static checks**: actionlint, yamllint, shellcheck, schema checks, prompt validation, cross-reference checks.
    2. **Python tests**: unit/contract tests and coverage gates.
  - Optionally split Python tests again into:
    - orchestrate/review/unit group
    - workflow-log analyzer/collector group
    - semantic cache / prompt-budget group
  - Keep a small top-level summary job that depends on both.

- **Estimated time savings**
  - Likely cut wall-clock CI from **~10–11 minutes down to ~5–7 minutes** if runner availability is unchanged.

- **Implementation risk**
  - **Medium**: requires workflow restructuring and possibly artifact/coverage coordination.

---

### 4) Reduce polling frequency and duplicate status checks in workflow watchers
**Important, but secondary to the items above**

- **Evidence**
  - Deep-dive GH API inventory shows very heavy polling patterns:
    - **56** occurrences of `gh api "repos/${REPO}/actions/runs/${RID}" --jq '.status …'`
    - **41** occurrences of `actions/workflows/.../runs?per_page=1`
    - **41** occurrences of `actions/runs/${NEW_ID}` status/conclusion fetches
  - These are concentrated in `test_and_mark_stable` watcher steps such as `orphan-workflows-test` and `validate-standalone-test`.

- **Root cause**
  - Tight polling loops perform multiple REST lookups per cycle instead of memoizing run IDs and backing off after initial discovery.

- **Exact change**
  - After discovering the child run ID:
    - switch to **single endpoint polling** for that run
    - increase interval over time, e.g. **5s → 10s → 20s → 30s**
    - stop calling both `status` and `{status, conclusion}` endpoints in the same cycle
    - reuse previously fetched `NEW_ID` unless a fresh dispatch was actually triggered

- **Estimated time savings**
  - Direct runtime savings are moderate, around **1–5 minutes** on long watchers.
  - Bigger win is lower API pressure and fewer rate-limit stalls.

- **Implementation risk**
  - **Low**.

---

### 5) Avoid runner-start cost for obviously no-op post-merge dispatches
**Micro-optimization**

- **Evidence**
  - `review_autofix` recent runs `25505827548` and `25505302448` executed `post-merge-validate-dispatch` and found **“No linked issues found for merged PR #2250/#2251.”**
  - The dispatch step itself completed in about **1 second**, but the workflow still pays setup/runner overhead.

- **Root cause**
  - This path runs even when no linked issue is likely to exist.

- **Exact change**
  - Move the cheapest “has linked issue?” gate into an earlier job output or caller-level condition.
  - If PR metadata indicates no linked issue and no validate label, skip the whole post-merge dispatch job.

- **Estimated time savings**
  - Only **seconds per run**, but useful for noise reduction.

- **Implementation risk**
  - **Low**.

---

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1) Cache or reuse unselected-run summaries in `workflow_log_analysis`
- **Evidence**
  - Failed run `25473131401` logged:
    - `op: "summarize_unselected_runs"`
    - `model: "openai/gpt-5.4-mini"`
    - `summarized: 85`
    - `targeted: 100`
    - `tokens_used: 214237`
  - This happened inside a run that still failed after **318s**.

- **Root cause**
  - The analyzer is paying to summarize many unselected runs repeatedly, even though most runs are immutable once completed.

- **Exact change**
  - Persist per-run summary artifacts keyed by **`run_id + updated_at`**.
  - On re-analysis:
    - reuse unchanged summaries
    - only summarize new/changed runs
    - optionally lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS` when deep-dive evidence already covers the outliers.

- **Estimated savings**
  - **100k–200k+ tokens per analysis run** in the observed pattern.

- **Quality-risk notes**
  - **Low risk** if cache invalidation uses `updated_at` or equivalent immutable completion marker.

---

### 2) Downshift `review_autofix` comment-only / claude-branch-review mode
- **Evidence**
  - Recent and slow review runs show expensive review configuration even when edit/judge/merge paths are skipped:
    - `25490929374` took **2,483s**
    - recent logs show `REVIEWER_MODELS` with **6 reviewer models**
    - `ENABLE_REVIEWER_TWO_PASS: true`
    - recent cancelled runs explicitly say **“reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.”**
  - Slow run `25490929374` also shows `MODEL_EDITOR: openai/gpt-5.3-codex`, with the full reviewer stack still enabled.

- **Root cause**
  - The system appears to apply nearly full review cost to paths that only need comments, not code changes.

- **Exact change**
  - For **comment-only / claude-branch-review** mode:
    - reduce reviewer panel size from 6 to 2–3 models
    - disable second-pass review unless the diff exceeds a higher threshold
    - lower summarizer reasoning on no-edit paths
    - use the full panel only when the run can actually commit or merge

- **Estimated savings**
  - **Inference:** likely **40–70% reviewer-token reduction** on comment-only paths, plus several minutes of latency.

- **Quality-risk notes**
  - **Medium risk**: consensus depth falls somewhat, so apply only to comment-only paths where edit automation is already disabled.

---

### 3) Lower reasoning effort on deterministic smoke/canary tasks
- **Evidence**
  - Failed implement run `25496323404` had a fully-specified smoke task in the prompt:
    - only modify `tests/e2e_smoke_canary.txt`
    - replace content exactly
    - **“no clarification is needed”**
  - The run still used `MODEL_EDITOR: openai/gpt-5.4` with `MODEL_REASONING_EFFORT: medium`.
  - Related diagnostics around this failure show two wasted attempts with token counts in the adjacent orchestrator summaries for the same issue flow:
    - upstream summaries tied to `25496323404` report **11,954** and **10,696** tokens on attempts 1 and 2 before bailing.

- **Root cause**
  - Medium reasoning is being used even for tiny, deterministic edit tasks.

- **Exact change**
  - For smoke/canary issues detected by title/body or explicit env:
    - set editor reasoning to **low**
    - reduce prompt verbosity
    - cap retries at 1 when the task is single-file and exact-output constrained

- **Estimated savings**
  - **Inference:** roughly **20–40% token reduction** on smoke tasks, plus fewer wasted retries.

- **Quality-risk notes**
  - **Low risk** for exact-output canary tasks; do not generalize to open-ended implementation.

---

### 4) Short-circuit follow-on orchestration fan-out after a terminal Codex bail
- **Evidence**
  - Failed implement runs `25496323404` and `25496338569` both ended with:
    - **“Codex produced no actionable output 2 attempts in a row”**
    - **“Codex bailed: 2 consecutive attempts with no actionable output … agent loop stuck in exploration.”**
  - Follow-on `clarify`, `plan`, `respond`, and `implement` wrapper runs were then triggered/skipped around the same issue flow, adding control-plane churn.
  - Adjacent diagnostic summaries for the same failures preserve the attempt token counts.

- **Root cause**
  - Terminal execution failures still fan out into downstream wrapper activity and diagnostics propagation, even when no recovery path is available automatically.

- **Exact change**
  - When a phase emits the explicit terminal bail reason:
    - mark the issue/work item as **terminal-failed**
    - suppress auto-fan-out to non-recovering follow-up workflows
    - route directly to a single diagnostic/comment path

- **Estimated savings**
  - Avoids **~20k–24k tokens per affected failed implement** in similar two-attempt loops, plus runner/API overhead.

- **Quality-risk notes**
  - **Low to medium**: ensure manual retry or human reapproval still works.

---

### 5) Prefer `gpt-5.4` over legacy `gpt-5.3-codex` where already validated
- **Evidence**
  - Slow review run `25490929374` still used `MODEL_EDITOR: openai/gpt-5.3-codex`.
  - Recent successful review runs use `MODEL_EDITOR: openai/gpt-5.4`.
  - CI and repo changes referenced in the same period suggest the system is actively migrating model defaults.

- **Root cause**
  - Legacy model configuration remains active in some review paths.

- **Exact change**
  - Finish default migration so slow-path reviewers and editors use the already validated current default.
  - Keep `gpt-5.3-codex` only as explicit opt-in fallback.

- **Estimated savings**
  - **Inference:** moderate cost and latency savings, but not directly quantifiable from the provided logs.

- **Quality-risk notes**
  - **Low** if migration remains behind the existing catalog/fallback mechanism.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1) Fix the Codex CLI invocation in `orchestrate`
- **Failure evidence**
  - Failed run `25473127144` (`orchestrate`, 95s) shows:
    - **“error: unexpected argument '--ask-for-approval' found”**
    - repeated on attempts 2 and 3
    - then `orchestration_failed` was recorded to memory telemetry.

- **Root cause category**
  - **Deterministic CLI contract bug**

- **Exact fix**
  - Correct the `codex exec` invocation so `--ask-for-approval` is passed using the currently supported syntax or removed if incompatible with the current Codex version.
  - Add a contract/unit test that shells the exact command line used by `internal-orchestrate.yml`.

- **Expected reliability impact**
  - Should remove the observed hard failure mode in this family immediately.
  - `orchestrate` currently shows **1 failure in 5 runs (20%)**; this bug likely accounts for a meaningful part of that.

- **Rollback / fail-open**
  - Safe rollback to the prior invocation if needed; fail-open should be “do not dispatch decomposer” rather than retrying a broken command three times.

---

### 2) Break the `ai:implementing` deadlock between implement and watcher flows
- **Failure evidence**
  - Run `25496132733` waited while the issue remained labeled `ai:implementing` across many polls, then timed out.
  - Successful implement runs `25496356128` and `25496377985` show **“Issue already has ai:implementing label. Another implement run is in progress. Skipping.”**
  - Failed implement runs `25496323404` and `25496338569` terminated after exploration-loop bailouts, which can leave watchers waiting for progress that will not happen.

- **Root cause category**
  - **State-machine deadlock / stale label coordination**

- **Exact fix**
  - Store the active implement **run ID / timestamp** alongside the `ai:implementing` state, or derive it from a ledger comment.
  - On implement failure, always clear or invalidate stale `ai:implementing` state.
  - In watchers, verify that the label corresponds to an active run before continuing to wait.

- **Expected reliability impact**
  - Reduces timeout failures and false “another run is in progress” skips.
  - Especially important for e2e and smoke suites.

- **Rollback / fail-open**
  - Fail-open should be: if active-run lookup fails, treat the label as stale after TTL rather than waiting indefinitely.

---

### 3) Treat terminal Codex “stuck in exploration” bails as non-retryable
- **Failure evidence**
  - Failed implement runs `25496323404` and `25496338569` both emitted:
    - **“Codex produced no actionable output 2 attempts in a row”**
    - **“agent loop is stuck in exploration.”**
  - The system already stops after two tries in these cases, which is good; the missing piece is broader orchestration awareness.

- **Root cause category**
  - **Terminal model-behavior class not fully propagated**

- **Exact fix**
  - Promote this bail reason to a first-class terminal status:
    - skip downstream non-recovery phases
    - attach a stable machine-readable failure code
    - differentiate from transient API/tool failures

- **Expected reliability impact**
  - Cuts wasted reruns and lowers confusion for operators and tests.

- **Rollback / fail-open**
  - If uncertain, default to current behavior; this is mostly additional classification, not a behavior break.

---

### 4) Harden watcher/analyser behavior around partial logs
- **Failure evidence**
  - Run `25474642232` warned:
    - **“Soft-error analyser wait deadline elapsed … analyser will see partial logs.”**
  - The workflow still proceeded into analysis with incomplete evidence.

- **Root cause category**
  - **Watcher timeout and degraded-evidence handling**

- **Exact fix**
  - When logs are partial:
    - mark the result explicitly as degraded
    - distinguish “analysis incomplete” from “workflow under analysis failed”
    - optionally retry log collection once with longer final backoff before declaring degraded mode

- **Expected reliability impact**
  - Fewer false-negative or misleading analysis outcomes in long-running suites.

- **Rollback / fail-open**
  - Safe fail-open is acceptable here, but it should be surfaced as degraded, not silent.

---

### 5) Add integration coverage for duplicate-implement and stale-label state transitions
- **Failure evidence**
  - The repo has recent CI tests around retry and state logic, but production runs still showed duplicate implement skips and stale-wait behavior.
  - Examples: `25496356128`, `25496377985`, `25496132733`.

- **Root cause category**
  - **State-machine regression gap**

- **Exact fix**
  - Add an integration test covering:
    1. implement starts
    2. second implement attempt sees `ai:implementing`
    3. first implement fails terminally
    4. stale state is cleared
    5. watcher exits correctly

- **Expected reliability impact**
  - Medium; this would directly defend one of the observed recurring coordination failures.

- **Rollback / fail-open**
  - N/A; test-only change.

---

## AI Memory Health

I found meaningful `AI_MEMORY_TELEMETRY` in the deep-dive logs.

### Memory retrieval effectiveness

- **Retrieve operations observed:** **24**
- **Retrieve hit count:** **4**
- **Hit rate:** **16.7%**
- **Average `estimated_tokens`:** **9.3**
- **Average observed budget field:** **0 / missing** in these records
- **`keyword_method` distribution:**
  - `plain`: **16**
  - `none`: **6**
  - `llm`: **2**

### What the logs show

- **Healthy writes**
  - `record-run-event` appeared **54** times, usually with `did_push: true`, `push_attempts: 1`.
  - `processed-command-claim` appeared **4** times, including failed implement runs `25496323404` and `25496338569`.
  - `record-candidate` appeared in slow review run `25490929374`, indicating the system is at least occasionally promoting useful review findings into memory.

- **Weak reads**
  - **20 of 24 retrieves returned 0 records.**
  - Examples:
    - `clarify` failure `25473125487`: `records_selected: 0`, `keyword_method: plain`
    - `orchestrate` failure `25473127144`: `records_selected: 0`, `keyword_method: llm`
    - slow `review_autofix` run `25490929374`: `records_selected: 0`, `keyword_method: none`
  - A positive example exists:
    - failed implement run `25496323404` retrieved **2 records** with `estimated_tokens: 56`, including prior plan/decision memory relevant to issue `#2244`.

### Flags requested in the brief

- **Retrieves returning 0 records:** yes, **20**
- **`fail_open: true` entries:** **none observed**
- **`enabled: false` entries:** **none observed**
- **High push retry counts:** **none observed**; write operations consistently showed `push_attempts: 1`

### Assessment

- The memory system is **operationally healthy on writes** but **semantically weak on retrieval**.
- The very low hit rate suggests one or more of:
  - sparse useful records for the queried context,
  - weak query/key extraction,
  - over-restrictive matching,
  - or a mismatch between what gets recorded and what the phases later need.

### Recommendations

1. **Improve retrieval keying for review/orchestrate**
   - `keyword_method: none` and repeated zero-hit `plain` retrievals are strong signals to seed better keys from issue/PR identifiers, workflow family, and recent labels.
2. **Prefer deterministic context anchors before free-text keywords**
   - Issue number, PR number, workflow family, and recent phase result should be first-class selectors.
3. **Emit retrieval miss reasons**
   - Add telemetry fields like `query_terms`, `candidate_pool_size`, and `filtered_out_reason` so misses can be debugged without rereading raw logs.
4. **Track memory usefulness by phase**
   - A simple dashboard metric such as “retrieve hit rate by workflow_family” would quickly show where memory is actually helping.

---

## GH API Call Audit

This section focuses on **high-volume** and **high-redundancy** patterns visible in the sampled logs.

### 1) `test_and_mark_stable` watchers are the largest REST-call hotspot
- **Evidence**
  - Across the sampled deep-dive logs, the most common GH API patterns were:
    - **56** calls to `repos/.../actions/runs/{id}` status lookups
    - **41** calls to `actions/workflows/.../runs?per_page=1`
    - **41** calls to `actions/runs/{NEW_ID}` status/conclusion fetches
  - These calls are concentrated in watcher steps such as:
    - run `25496132733` → `step-010-orphan-workflows-test.log`
    - run `25474642232` → `step-007-orphan-workflows-test.log`
    - related watch steps in `validate-standalone-test` and `orchestrate-decompose-test`

- **Pattern**
  - Discover latest run ID
  - Discover again with a larger page size
  - Poll status on the discovered run
  - Re-poll final status separately

- **Concrete batching/reuse change**
  - After first discovery, persist `NEW_ID` and poll only that run.
  - Do not call both list and detail endpoints repeatedly in the same loop.
  - Increase poll interval over time.

- **Estimated call-count reduction**
  - **50–70% fewer API calls** in the watcher-heavy jobs.
  - Also lowers rate-limit risk.

---

### 2) Repeated issue/PR lookups in `test_and_mark_stable` can be memoized across steps
- **Evidence**
  - Sampled inventory also showed repeated calls to:
    - `repos/${TEST_REPO}/issues/${ISSUE_NUMBER}`: **39**
    - `repos/${TEST_REPO}/issues`: **29**
    - `repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments`: **21**
    - `repos/${TEST_REPO}/pulls/${PR_NUMBER}`: **14**
  - These appear in `e2e-smoke-test`, `e2e-alt-model-test`, and `clarify-rejects-unsolvable-test`.

- **Pattern**
  - Each test step independently re-fetches issue metadata, labels, comments, and PR context.

- **Concrete batching/reuse change**
  - Fetch issue + PR metadata once per test workflow and store it in a small JSON artifact or environment file reused by downstream jobs.
  - Only re-fetch when a step mutates the issue/PR state.

- **Estimated call-count reduction**
  - **30–50 API calls per `test_and_mark_stable` run** in the observed pattern.

---

### 3) `cancel_on_pr_close` is over-checking `/rate_limit` for a usually no-op path
- **Evidence**
  - Recent successful no-op runs `25505827554` and `25505304096`:
    - found **“No matching queued/in-progress…”**
    - still include a helper that calls `gh api -i /rate_limit`
    - and wraps calls in generic retry logic
  - Total runtime is only **7–13s**, so this is not a speed crisis, but it is noisy.

- **Pattern**
  - Rate-limit metadata is fetched proactively before the workflow even knows whether it has anything to cancel.

- **Concrete batching/reuse change**
  - Only query `/rate_limit` after a failed GH API call or after receiving a 403/429 signal.
  - In the common no-op path, skip the preflight rate-limit call entirely.

- **Estimated call-count reduction**
  - Small per run, but near-constant savings on every PR-close event.

---

### 4) `issue_pr_status` already shows good API hygiene; reuse this pattern elsewhere
- **Evidence**
  - Recent run `25505827658` explicitly documents:
    - **“Single batched GraphQL call”**
    - GraphQL-first detection with REST fallback
  - This is the best API pattern in the sample.

- **Missed reuse opportunity**
  - `review` post-merge validate dispatch still does:
    - one GraphQL lookup
    - one REST PR fetch for title/body
    - optional per-issue `gh issue view` calls if labels are unknown

- **Concrete batching/reuse change**
  - Reuse the `issue_pr_status` GraphQL batching approach in `review`’s post-merge dispatch and other link-resolution paths.
  - Normalize a shared helper for “linked issues + labels + orchestrator-validate-required presence”.

- **Estimated call-count reduction**
  - **Low to moderate** per run, but it reduces duplicated resolver logic across workflows.

---

### 5) Workflow-log analysis itself appears to audit API redundancy with repeated PR lookups
- **Evidence**
  - Sampled inventory shows frequent `gh api graphql` and repeated `pulls/${PR_NUMBER}` title/body lookups in workflow-log-analysis deep dives, especially around slow run `25480827754`.

- **Concrete change**
  - Memoize PR title/body and issue-link resolution once per analyzed run.
  - Persist intermediate analysis context between `api-redundancy` and `deep-audit` steps.

- **Estimated call-count reduction**
  - Moderate, and likely synergistic with token savings because less duplicated context assembly will also reduce prompt variance.

---

## Prompt Cache & Memory System

### Prompt cache behavior observed

- `OPENROUTER_PROMPT_CACHE_DISABLED: false` was visible in multiple families:
  - `clarify`
  - `implement`
  - `review_autofix`
  - `validate-scripts`
- However, **actual prompt-cache usage metrics are mostly missing or `na`** in the logs I examined.
  - In slow review run `25490929374`, the cache probe emitted:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

### What that means

- The prompt cache is **enabled in configuration**, but the current telemetry does **not let you measure hit/miss quality** reliably.
- So the system may be caching successfully, partially, or not at all; the logs do not prove which.

### Likely cache-fragmentation causes
**Inference, based on prompt construction patterns in logs**

- Large dynamic payloads are embedded directly into prompts:
  - PR body
  - issue comments
  - linked issue context
  - diff snapshots
  - memory context
- Review and implement prompts appear to include many dynamic files and path-dependent preambles.
- Model migration (`gpt-5.3-codex` vs `gpt-5.4`) also splits the cache surface.

### Concrete improvements

1. **Emit real cache metrics per model call**
   - Record `prompt_tokens`, `completion_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and a stable `cache_key_prefix_hash`.
   - Without this, the cache system cannot be optimized scientifically.

2. **Stabilize prompt prefixes**
   - Keep system instructions, workflow instructions, and static policy text in a stable prefix.
   - Push volatile data—PR body, issue comments, diff chunks—to the suffix.

3. **Deduplicate repeated static blocks**
   - Several logs show the same model/reasoning/env context echoed many times inside one run.
   - Ensure the actual LLM input is assembled once and reused where possible, even if shell logging remains verbose.

4. **Separate cache policy by workflow class**
   - `review_autofix`, `implement`, and `workflow_log_analysis` have very different dynamic/noisy context. They likely need different cache strategies.

### Estimated impact

- **Latency:** medium, especially on repeated review/implement patterns.
- **Tokens:** potentially meaningful, but **not quantifiable from current prompt-cache telemetry**.
- **Reliability:** better observability will also help distinguish cache misses from model/execution failures.

### Memory system takeaway

- Prompt caching is **under-instrumented**.
- AI memory is **instrumented**, but retrieval usefulness is currently poor.
- The smallest safe next step is **better telemetry first**, then cache-prefix and retrieval-query tuning.

---

## Orchestrator Health

### Overall assessment

The orchestrator is **functionally alive** but showing three health issues:

1. **terminal AI loops still leak into broader orchestration**
2. **state coordination via labels is brittle**
3. **review/autofix supersession is costly**

### Observable pain points

#### 1) Stuck-in-exploration loops are recurring and clearly detectable
- Failed implement runs `25496323404` and `25496338569` both ended with the same terminal pattern:
  - two attempts
  - no actionable output
  - explicit stuck-in-exploration bail
- Follow-on wrapper runs around the same issue flow then skipped based on command/body conditions, which is operationally noisy.

**Smallest safe mitigation:** promote this failure into a first-class terminal orchestrator state so other phases do not keep probing around it.

---

#### 2) Wave/state progression is too dependent on labels alone
- `ai:implementing` is treated as evidence of in-progress work, but `25496132733` shows it can remain present long after useful progress has stopped.
- Successful implement skips (`25496356128`, `25496377985`) show the same label is already being used as a concurrency gate.

**Smallest safe mitigation:** pair the label with active run metadata or ledger status.

---

#### 3) `orchestrate_poll` itself looks healthy, which is useful
- Recent poll runs `25497081946`, `25499700973`, and `25502992533` completed successfully in **43–54s** or **52s**-class durations, recorded `poll_completed`, and showed `push_attempts: 1`.
- This suggests the poller is not the main problem; the bigger issue is what it is polling and how upstream/downstream phases classify terminal states.

**Smallest safe mitigation:** keep poller simple; improve child-state semantics rather than adding complexity to the poller.

---

#### 4) Review-autofix cancellations are an operational smell
- `review_autofix` has **65 cancellations in 104 runs**, far above any other family in the sample.
- Some cancellations happen quickly, but many occur after hundreds or thousands of seconds.

**Smallest safe mitigation:** stricter concurrency, earlier supersession detection, lighter comment-only path.

---

### Observable indicators teams should track

Add these as simple metrics:

- **Terminal empty-output bail rate** by workflow family
- **`ai:implementing` stale duration** before clear/failure
- **Review run cancellation rate after >300s**
- **Memory retrieve hit rate** by workflow family
- **Watcher API calls per successful dispatch**
- **Fraction of child-workflow waits ending in missing run / stale label / partial logs**

---

## Pipeline Flow Bottlenecks

Mapped across the main pipeline stages.

### 1) Clarify → Plan → Implement
- **Bottleneck type:** retry/terminal-state handling
- **Evidence**
  - Implement failures `25496323404` and `25496338569` consumed two attempts each before bailing.
  - Clarify failures `25473125487`, `25473129175`, `25473129346` each took about **100–115s** and failed in the Codex step.
- **Interpretation**
  - The core AI phases are not the dominant wall-clock problem overall, but they are the dominant **reliability** problem when they fail.
- **Fix order**
  1. classify terminal bails better
  2. improve memory retrieval
  3. lower reasoning for deterministic tasks

---

### 2) Implement → Review handoff
- **Bottleneck type:** state coordination / stale labels
- **Evidence**
  - `25496132733` never reached review because the watcher kept seeing `ai:implementing`.
- **Interpretation**
  - The pipeline can wait on a label instead of waiting on a live run or terminal event.
- **Fix**
  - Shift handoff from label-only coordination to run-aware coordination.

---

### 3) Review / Autofix
- **Bottleneck type:** compute + cancellation overhead + queueing
- **Evidence**
  - `review_autofix` p95 **1,550.3s**
  - slow success `25490929374`: **2,483s**
  - many cancellations after long durations
  - check-run wait loop in slow run logged repeated **20s sleeps**
- **Interpretation**
  - This is the biggest steady-state bottleneck in the human-facing pipeline.
- **Fix order**
  1. concurrency/supersession
  2. lighter comment-only mode
  3. reduce check-run wait churn

---

### 4) Validate / Workflow-log-analysis
- **Bottleneck type:** compute + token-heavy summarization
- **Evidence**
  - `workflow_log_analysis` family p50 **3,345s**
  - failed run `25473131401` spent **214,237 tokens** summarizing unselected runs
- **Interpretation**
  - This is a major analysis-phase cost center and can back up validation/test workflows that depend on it.
- **Fix order**
  1. cache summaries
  2. reduce unselected-run window
  3. memoize GH API-derived context

---

### 5) Queueing overhead
- **Bottleneck type:** runner provisioning
- **Evidence**
  - Many recent logs explicitly include runner wait:
    - CI
    - review_autofix
    - copilot reviewer
    - issue_pr_status
    - orchestrate_poll
- **Interpretation**
  - Some wall time is unavoidable hosted-runner queueing.
- **Safe mitigation**
  - The best non-infrastructure fix is to **start fewer unnecessary jobs**, especially skipped wrappers and superseded review jobs.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` extreme end-to-end latency and failure rate
- `review_autofix` long-tail runtime plus heavy cancellation
- `ci` serialized ~10-minute lint/test path
- `workflow_log_analysis` long runtime and high token burn

**Top failure modes**
- Orchestrate CLI invocation bug (`25473127144`)
- Implement terminal “stuck in exploration” bails (`25496323404`, `25496338569`)
- Stale `ai:implementing` / watcher deadlock (`25496132733`)
- Partial-log / wait-deadline degradation in analysis paths (`25474642232`)

**Highest-cost drivers**
- Workflow-log summarization of unselected runs (`214,237` tokens in `25473131401`)
- Multi-reviewer/two-pass review paths, especially comment-only or claude-branch-review mode
- Repeated failed AI attempts on deterministic smoke tasks

**Top 3 prioritized actions**
1. **Fix watcher termination logic in `test_and_mark_stable`**  
   Fail fast on missing child runs, failed child runs, and stale `ai:implementing` state.

2. **Add strict concurrency and lighter review mode for `review_autofix`**  
   Cancel superseded runs immediately and reduce reviewer fan-out for comment-only paths.

3. **Cache workflow-log unselected-run summaries by immutable run identity**  
   Cut token spend and analysis runtime without changing user-visible behavior.

---

## Metrics Appendix

### Overall repository metrics

| Scope | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 277 | 14 | 74 | 635 | 1.4% | 1.0s | 630.0s |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 70 | 68 | 2 | 0 | 0 | 2.86% | 614.5s | 662.2s |
| `review_autofix` | 104 | 32 | 0 | 65 | 7 | 0.0% | 57.5s | 1550.3s |
| `test_and_mark_stable` | 5 | 1 | 3 | 1 | 0 | 60.0% | 4568.0s | 5235.8s |
| `workflow_log_analysis` | 5 | 3 | 1 | 1 | 0 | 20.0% | 3345.0s | 3417.4s |
| `clarify` | 196 | 20 | 3 | 0 | 173 | 1.53% | 1.0s | 99.25s |
| `implement` | 166 | 18 | 3 | 7 | 138 | 1.81% | 1.0s | 235.5s |
| `orchestrate` | 5 | 4 | 1 | 0 | 0 | 20.0% | 167.0s | 186.4s |
| `orchestrate_poll` | 17 | 17 | 0 | 0 | 0 | 0.0% | 52.0s | 130.6s |
| `issue_pr_status` | 19 | 19 | 0 | 0 | 0 | 0.0% | 20.0s | 74.2s |
| `cancel_on_pr_close` | 19 | 19 | 0 | 0 | 0 | 0.0% | 13.0s | 18.4s |

### Representative long runs used in analysis

| Run ID | Workflow family | Conclusion | Duration | Main evidence used |
|---|---|---|---:|---|
| `25496132733` | `test_and_mark_stable` | failure | 4599s | alt-model watcher timed out while `ai:implementing` persisted |
| `25474642232` | `test_and_mark_stable` | failure | 5395s | orphan workflow analyser deadline / partial logs |
| `25490929374` | `review_autofix` | success | 2483s | long reviewer path, check-run waits, memory miss |
| `25505306334` | `ci` | success | 640s | 10-minute serialized lint/test path |
| `25473127144` | `orchestrate` | failure | 95s | deterministic Codex CLI flag error |
| `25496323404` | `implement` | failure | 119s | 2-attempt “stuck in exploration” bail with memory hit |
| `25473131401` | `workflow_log_analysis` | failure | 318s | unselected-run summarization used 214,237 tokens |

### Observed token-related telemetry

| Run ID | Workflow family | Token evidence | Observed value |
|---|---|---|---:|
| `25473131401` | `workflow_log_analysis` | `summarize_unselected_runs.tokens_used` | **214,237** |
| `25496323404` | related diagnostics in follow-on logs | Attempt 1 tokens | **11,954** |
| `25496323404` | related diagnostics in follow-on logs | Attempt 2 tokens | **10,696** |
| `25496338569` | related diagnostics in follow-on logs | Attempt 1 tokens | **11,805** |
| `25496338569` | related diagnostics in follow-on logs | Attempt 2 tokens | **12,152** |
| `25496323404` | `implement` memory retrieve estimate | `estimated_tokens` | **56** |
| `25496338569` | `implement` memory retrieve estimate | `estimated_tokens` | **56** |

### AI memory metrics from deep-dive logs

| Metric | Value |
|---|---:|
| `AI_MEMORY_TELEMETRY` lines found | 175 |
| `retrieve` operations | 24 |
| `retrieve` hits (`records_selected > 0`) | 4 |
| Retrieve hit rate | 16.7% |
| Zero-result retrieves | 20 |
| Avg `estimated_tokens` on retrieve | 9.3 |
| `keyword_method=plain` | 16 |
| `keyword_method=none` | 6 |
| `keyword_method=llm` | 2 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |
| Write ops with high push retry counts | 0 observed |

### Prompt cache / cache observability

| Signal | Observation |
|---|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | Frequently `false` across AI workflows |
| Actual prompt cache read/create token metrics | Mostly unavailable / `na` in sampled logs |
| Classic Actions cache evidence | `workflow_log_analysis` run `25473131401` had `Cache hit for: codex-v0.114.0` |
| Reliable prompt-cache hit-rate measurement available? | **No** |

### Sampled GH API hotspot patterns from deep-dive logs

| Pattern | Observed count in sampled logs | Main workflow areas |
|---|---:|---|
| `actions/runs/{id}` status polling | 56 | `test_and_mark_stable` watchers |
| `actions/workflows/.../runs?per_page=1` | 41 | `test_and_mark_stable` watchers |
| `actions/runs/{NEW_ID}` status/conclusion fetch | 41 | `test_and_mark_stable` watchers |
| `issues/{ISSUE_NUMBER}` fetch | 39 | smoke/e2e watcher steps |
| `issues` list/search | 29 | smoke/e2e watcher steps |
| `issues/{ISSUE_NUMBER}/comments` | 21 | smoke/e2e watcher steps |
| `gh api graphql` | 21 | workflow-log analysis, review dispatch, issue linking |
| proactive `/rate_limit` checks | 13+ | review/cancel helpers |

If you want, I can turn this report into a **prioritized implementation backlog** with:
1. exact workflow files likely to change,  
2. a one-week rollout sequence, and  
3. success metrics to verify each optimization.

## Deep Audit — Workflows & Scripts (2026-05-07)

### Section 1: Bug & Correctness Sweep

- **ID** — `SHELL-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:10600-10604`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `ISSUE_NUMS` is expanded unquoted twice: once in `printf '%s\n' ${ISSUE_NUMS}` and again in `for inum in ${_sorted_issue_nums}; do`. That makes the loop dependent on shell word-splitting and glob expansion instead of the intended newline-delimited issue list. If the variable ever contains unexpected whitespace, a leading `-`, or glob characters, iteration changes before `sort -un` even runs.  
  **Recommended fix** — Quote the source expansion and iterate line-by-line, e.g. build `_sorted_issue_nums` from `printf '%s\n' "${ISSUE_NUMS}" | sort -un`, then consume it with `while IFS= read -r inum; do ...; done`. This matches the repo’s safer line-oriented patterns elsewhere.

- **ID** — `SHELL-002`  
  **File path** — `scripts/review_commit_changes.sh:448-456`, `scripts/review_conflict_resolve.sh:1033-1035`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Both scripts call `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}` without quoting the full URL. ShellCheck flags both sites (`SC2086`). Today’s token/repo formats usually avoid spaces, but this still relies on shell expansion rules around a token-bearing string and unnecessarily passes the credentialized URL as argv.  
  **Recommended fix** — Quote the full URL at minimum. Prefer the existing safer pattern already used in the resolver staging blocks (`.github/workflows/clarify.yml:79-82`, `.github/workflows/implement.yml:256-259`, `.github/workflows/plan.yml:108-111`, etc.), which uses `git -c "http.extraHeader=Authorization: Basic ..."` instead of embedding the token in the remote URL.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/review_autofix.yml:1373-1379`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The PR-context assembly path makes four separate API fetches in the same execution path before any linked-issue lookup:  
  1. `GET /pulls/{PR_NUMBER}`  
  2. `GET /issues/{PR_NUMBER}/comments`  
  3. `GET /pulls/{PR_NUMBER}/reviews`  
  4. `GET /pulls/{PR_NUMBER}/comments`  
  Those four calls are then normalized locally into `PR_META_FILE`, `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEWS_FILE`, and `PR_REVIEW_COMMENTS_FILE`. The repo already contains a GraphQL-first consolidation pattern in `scripts/gh_helpers.sh` via `gh_pr_with_all_comments()` (`scripts/gh_helpers.sh:735-760`, `761-851`), but this workflow does not use it.  
  **Current call count** — 4 logical GitHub fetches for PR context on this path.  
  **Proposed call count after fix** — 1 logical PR-context fetch in the common case, by extending `gh_pr_with_all_comments()` to also return review bodies/state and then consuming that single payload here.  
  **Existing batching pattern to extend** — `gh_pr_with_all_comments` in `scripts/gh_helpers.sh`.  
  **Recommended fix** — Move this block onto a shared helper that returns `meta + issue comments + reviews + review comments` in one GraphQL-first payload with the existing REST parity fallback preserved. Then have `review_autofix.yml` materialize the four files from that one payload instead of re-fetching each surface independently.

- **ID** — `BATCH-001`  
  **File path** — `.github/workflows/review_autofix.yml:515-567`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The post-merge standalone-validate dispatch path is partially batched, then falls back to per-issue REST inside the loop. On the fallback branch it does:  
  1. 1 GraphQL call for `closingIssuesReferences` with labels,  
  2. 1 REST PR fetch for title/body fallback,  
  3. `N` separate `gh issue view ... --json labels` calls for each extracted issue whose labels are unknown.  
  This creates a review-blocker pattern from CLAUDE.md §15: API calls inside a loop that processes multiple issues.  
  **Current call count** — `2 + N` on the fallback path after regex extraction.  
  **Proposed call count after fix** — 3 total: 1 GraphQL `closingIssuesReferences` attempt, 1 PR title/body fetch if fallback regex is still needed, and 1 batched issue-label GraphQL lookup for all extracted issue numbers.  
  **Existing batching pattern to extend** — `_fetch_issue_labels_batch_graphql` in `scripts/orchestrate_poll_process.sh:1230-1306` (same alias-batching pattern also used by `_fetch_candidate_issue_details_graphql`).  
  **Recommended fix** — After regex extraction, batch-resolve labels for all candidate issue numbers in one alias-based GraphQL query rather than calling `gh issue view` per issue. Reuse or factor out the `_fetch_issue_labels_batch_graphql` pattern into a shared helper callable from workflows.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/clarify.yml:57-126`, `.github/workflows/plan.yml:86-138`, `.github/workflows/implement.yml:234-303`, `.github/workflows/orchestrate_clarify_respond.yml:92-161`, `.github/workflows/validate.yml:77-129`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Five reusable workflows carry near-identical inline shell blocks to stage and run the canonical integration-ref resolver: identical `resolver_repo/resolver_ref` selection, temp checkout directories, masked clone logs, `git -c http.extraHeader=...`, fallback clone path, `chmod +x`, and final `bash "${resolver_script}"` invocation. The only material variance is the source issue variable (`ISSUE_NUMBER` vs `TRACKING_ISSUE`). This is high-maintenance duplication around a security-sensitive code path that already revolves around `scripts/resolve_integration_ref.sh`.  
  **Recommended fix** — Create a shared helper module, preferably `scripts/integration_ref_helpers.sh`, with a function such as `run_canonical_integration_ref_resolver <repo> <issue_number> <ref_out_file>` or `resolve_integration_ref_via_canonical_repo <issue_number>`. Update the five callers above to source that helper instead of inlining the staging logic. Keep `scripts/resolve_integration_ref.sh` as the canonical resolver body and move only the “stage canonical repo/ref and execute it” wrapper into the helper.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:3338-3394`, `.github/workflows/test-and-mark-stable.yml:3412-3452`, `.github/workflows/test-and-mark-stable.yml:3470-3517`, `.github/workflows/test-and-mark-stable.yml:3536-3576`, `.github/workflows/test-and-mark-stable.yml:3624-3685`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The release-gate workflow repeats the same “dispatch workflow, find new run ID, poll until completion, emit `run_id` to `$GITHUB_OUTPUT`” shell block across multiple steps. The duplicated structure includes the `PRE=.../runs?per_page=1` call, repeated `NEW_ID=$(gh api .../runs?per_page=10 ...)` poll loop, and repeated `GET /actions/runs/{id}` status polling. The repeated code is already diverging in deadlines and success conditions, which makes future fixes error-prone.  
  **Recommended fix** — Extract this into a shared script, e.g. `scripts/watch_workflow_dispatch.sh`, with a signature such as `watch_workflow_dispatch <repo> <workflow_file> <deadline_secs> [--field key=value ...]`. Return the discovered run ID and final conclusion via stdout/output-file so the existing steps can keep their caller-specific success rules while sharing one implementation. Update at least the workflow-log-analysis, validation-refresh, update_workflows, internal-memory-maintenance, and orchestrate-dispatch callers.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1202-1586`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — This `run:` block contains `${{ }}` interpolation and is already approximately **19,899 characters**, leaving only about **1,101 characters** of headroom under GitHub Actions’ **21,000-character** expression limit. It is the Phase 4a “wait-review” logic block and already contains substantial inline helper code, state tracking, and commentary. This is inside the failure zone that previously hit the repo, and a modest future edit can make the workflow unloadable at parse time.  
  **Recommended fix** — Extract the whole wait-review implementation into an external script such as `scripts/test_wait_review.sh` and pass only the small set of environment variables needed by the step. That is the safest option because it removes the expression-size ceiling entirely for this logic rather than merely buying a little headroom by splitting comments.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1672-2077`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — This `run:` block also contains `${{ }}` interpolation and is approximately **17,408 characters**, leaving about **3,592 characters** of headroom. It is the Phase 4b retry-and-verify canary block, with embedded helper functions, retry loops, and extensive diagnostics. It is below the hard limit, but already above the requested 15,000-character medium-risk threshold.  
  **Recommended fix** — Move the verification/retry flow into a script such as `scripts/test_verify_bait_removed.sh`, or split the step into smaller sub-steps: one step for GH fetch helpers and attempt-1 verification, another for retry dispatch/polling, and a final step for attempt-2 verification.

**Overall file-size note:** No workflow file currently exceeds the **800 KB** early-warning threshold. The two largest workflow files in scope are `review_autofix.yml` at **281,488 bytes** and `test-and-mark-stable.yml` at **268,546 bytes**.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:9718-9719`, `scripts/orchestrate_poll_process.sh:9936-9992`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `RB_FOLLOWUP_REFUSED` and `IF_BLOCKERS_SOURCE` are write-only variables. Repository search shows assignment sites but no subsequent reads that affect control flow, logging, or outputs. That means the code carries state that is currently inert.  
  **Recommended fix** — Either remove the variables entirely, or wire them into structured telemetry / comments / state-file updates so they become observable and testable. If the original intent was operator diagnostics, emit them in the existing poller log prefixes instead of leaving them as dead locals.

- **ID** — `DEAD-002`  
  **File path** — `scripts/review_issue_ledger.sh:866-917`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — The associative array `CURRENT_FLOOR` is declared and populated, but repository search only finds the declaration and assignment. There is no later read of the array when building the final ledger state.  
  **Recommended fix** — Remove `CURRENT_FLOOR` if it is obsolete, or use it explicitly in the merge/reconciliation path so the array has a contractual purpose. As written, it adds cognitive load without affecting behavior.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53`, `.github/workflows/comprehensive-test-and-release.yml:72-96`, `.github/workflows/comprehensive-test-and-release.yml:315-338`, `.github/workflows/review_autofix.yml:1291-1329`, `.github/workflows/test-and-mark-stable.yml:1232-1254`, `.github/workflows/test-and-mark-stable.yml:1719-1749`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — The repo ships a canonical retry/rate-limit implementation in `scripts/gh_helpers.sh`, but multiple workflows re-implement their own `gh_api_safe`, `_gh_retry`, or `gh_retry` wrappers inline. These wrappers are materially inconsistent: some query `/rate_limit`, some only do exponential backoff, some swallow non-rate-limit failures into empty strings, and some preserve stderr while others discard it. That inconsistency makes API behavior harder to reason about and increases maintenance cost whenever retry policy changes.  
  **Recommended fix** — Standardize on `scripts/gh_helpers.sh` everywhere. If a caller needs a missing capability, add it there once — for example, build any needed “capture to file + retry + JSON validation” behavior on top of `gh_retry_to_file` / `gh_api_json_to_file` — then delete the inline wrappers from the workflows above.

- **ID** — `DEBT-001`  
  **File path** — `.github/workflows/review_autofix.yml:3003-3019`  
  **Severity** — Medium  
  **Category tag** — `tech-debt`  
  **Description** — The editor-summary comment is assembled into `PR_EDITOR_COMMENT_FILE` and then posted as a single `gh api ... -f body="$(cat ...)"` call with no size guard or chunking. This repo already has a purpose-built chunking/posting helper in `scripts/post_review_comment.sh:12-24` and `292-337` that exists specifically to stay under GitHub’s comment-size ceiling and split on safe boundaries. The editor-summary path bypasses that existing mechanism.  
  **Recommended fix** — Reuse the existing comment-posting helper pattern for editor summaries. Either extend `scripts/post_review_comment.sh` with a mode for “single-body summary file” or factor its chunking logic into a shared helper that both reviewer-ledger posting and editor-summary posting can call.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 7 | API-001, BATCH-001, DUP-001, DUP-002, EXPR-002, CONSIST-001, DEBT-001 |
| Low | 4 | SHELL-001, SHELL-002, DEAD-001, DEAD-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 3 | Medium |
| Medium/Low fixes | 7 | Medium |
