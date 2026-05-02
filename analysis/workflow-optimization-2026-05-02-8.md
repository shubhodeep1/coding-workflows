## Executive Summary

- **Stable-release testing is the biggest critical-path problem.** The `test_and_mark_stable` family has **0 successes in 6 sampled runs**, with **2 hard failures** and **4 cancellations**; both failures (`25247210528`, `25249170035`) died at `e2e-smoke-test / Phase 4b: Verify editor removed bait line` after **6,255s–6,478s**. The logs show the PR was sometimes **already merged before bait injection**, so no `review_autofix` run could ever fan out. **Estimated impact:** save **60–100 minutes per stable-release attempt** and likely recover the family from effectively unusable to working. **Confidence:** high.

- **Implement smoke-test runs are wasting time on fully specified tasks.** Seven `implement` failures cluster around short-lived Codex runs; in `25243564804`, the issue body specified an exact 3-line file overwrite, yet the agent made **261 Serena calls**, produced **no file changes across 2 attempts**, and bailed with “no actionable output.” **Estimated impact:** cut **2–5 minutes per smoke-test implement run** and remove a recurring failure class. **Confidence:** high.

- **Workflow-log analysis is the clearest cost hotspot with actual token telemetry.** Across sampled deep dives, `summarize_unselected_runs` emitted **8 telemetry ops** totaling **1,218,871 tokens**, with individual runs using **116,787–154,202 tokens**. **Estimated impact:** **40–60% token reduction** in that workflow by narrowing summaries to anomalous families and deduping repetitive healthy CI runs. **Confidence:** high.

- **Review/autofix is over-provisioned for many paths.** Sampled runs show `REVIEWER_MODELS` includes **6 reviewer models**, `ENABLE_REVIEWER_TWO_PASS=true`, and both reviewer/editor reasoning at **`xhigh`**; long successful runs `25249290449` and `25249326304` lasted **1,107s** and **2,108s**. **Estimated impact:** **40–70% reviewer-token savings** and **5–15 minutes faster** medium-complexity review runs on gated low-risk paths. **Confidence:** medium.

- **Queueing and unnecessary fan-out are inflating background load.** `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` are dominated by skipped/other outcomes (`clarify`: **193/225**, `plan`: **161/187**, `implement`: **152/187**, `respond`: **180/187**), while many runs still incur runner wait/setup. **Estimated impact:** lower runner contention and shave **10–25s** from many non-work runs, indirectly helping real jobs start sooner. **Confidence:** high.

- **Telemetry plumbing is partially broken in review paths.** Recent `review_autofix` cancellation `25252889046` logged `SUPPORT_SERENA_DIR: unbound variable`, skipped Serena stats/report generation, and emitted `memory helper script missing; skipping run-end failure event`. **Estimated impact:** medium reliability gain plus much better observability for cache/memory/MCP tuning. **Confidence:** high.

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fix the stable-release E2E auto-merge race before Phase 4b
- **Evidence**
  - `test_and_mark_stable` has **0/6 successes**, **p50 3,462s**, **p95 6,422s**.
  - Failures `25247210528` and `25249170035` both stop at `e2e-smoke-test / Phase 4b: Verify editor removed bait line`.
  - In `25249170035`, the log explicitly says the PR was **already merged before bait could be injected**, so no `pull_request synchronize` event could trigger review, and later also logs: **“No review workflow run ... with head_sha=${BAIT_SHA} ever appeared”**.
- **Root cause**
  - `review_autofix` auto-merge/deterministic-skip behavior can close the PR before the smoke-test bait/edit verification sequence completes.
- **Exact change**
  - Add a hard gate in `review_autofix` and any auto-merge-enabling step: if PR has `e2e-smoke-test` or equivalent force-review label, **disable auto-merge and deterministic skip merge until Phase 4b completes**.
  - In `implement`, ensure E2E labels are applied **before** any PR-open event that can trigger review/automerge.
- **Estimated time savings**
  - **60–100 minutes per stable-release attempt** by eliminating dead-end waits/polls and avoiding reruns.
- **Implementation risk**
  - **Low-medium**. Behavior change is narrow and backward-compatible if scoped only to E2E-labeled PRs.

### 2. Add a deterministic fast-path for exact-content smoke-test implement issues
- **Evidence**
  - Failed implement run `25243564804` was for issue `#1927`, whose body fully specified: overwrite `tests/e2e_smoke_canary.txt` with an exact 3-line payload.
  - Same run logged **261 Serena tool calls**, **94% Serena efficiency**, but still produced **no file changes** across attempts and bailed.
  - Similar failures recur in `25243569299`, `25244121942`, `25244127789`, `25245077011`, `25245085089`, `25246727158`.
- **Root cause**
  - The general-purpose Codex loop is being used for a task that is already deterministic and machine-checkable.
- **Exact change**
  - Before invoking Codex in `implement`, detect the smoke-test pattern:
    - exact file path,
    - exact target content,
    - single-file modification constraint.
  - Execute a direct file rewrite or a direct success-no-op check if contents already match.
- **Estimated time savings**
  - **2–5 minutes per smoke-test implement run**, plus removal of downstream E2E waiting caused by stalled implement.
- **Implementation risk**
  - **Low**. Scope it to the known smoke-test template only.

### 3. Narrow Git fetches in `orchestrate_poll` and `workflow_log_analysis`
- **Evidence**
  - `orchestrate_poll` run `25252460199` spent most visible time in checkout, executing:
    - `git fetch ... +refs/heads/*:refs/remotes/origin/* +refs/tags/*:refs/tags/*`
    - then listing a very large tag set (`v0.0.1` through `v1.0.113` and more).
  - Similar broad fetch/tag listing appears in slow `workflow_log_analysis` logs.
- **Root cause**
  - Polling/analysis jobs fetch all branches and tags although most logic only needs `main` plus a small subset of refs.
- **Exact change**
  - For `orchestrate_poll`, fetch only the default branch and any explicitly required support ref; skip `+refs/tags/*`.
  - For `workflow_log_analysis`, keep shallow fetches and only fetch tags if a specific analysis step truly needs them.
- **Estimated time savings**
  - `orchestrate_poll`: **6–10s per run** (~15–25% of total runtime).
  - `workflow_log_analysis`: likely **1–2 minutes per run** on checkout/setup.
- **Implementation risk**
  - **Low**, if tag-dependent steps are explicitly whitelisted.

### 4. Stop dispatching child workflows that are known to be skipped
- **Evidence**
  - Family totals show extremely high non-work volume:
    - `clarify`: **193/225 other**
    - `plan`: **161/187 other**
    - `implement`: **152/187 other**
    - `orchestrate_clarify_respond`: **180/187 other**
  - Bursts around `2026-05-02T10:29` and `13:23` show chains of 0–2s skipped runs after comments/events.
- **Root cause**
  - Gating mostly happens inside child workflows, after dispatch and runner assignment logic has already started.
- **Exact change**
  - Move the same `if` predicates to the caller/orchestrator layer so clarify/plan/implement/respond are only dispatched when conditions are already true.
  - Preserve current in-workflow guards as defense-in-depth.
- **Estimated time savings**
  - **10–25s** removed from many non-work runs; more importantly, lowers queue pressure for real work.
- **Implementation risk**
  - **Low**. Duplicate existing logic upstream first, then measure.

### 5. Reduce reviewer fan-out on low-risk paths
- **Evidence**
  - Long `review_autofix` runs:
    - `25249290449`: **1,107s**
    - `25249326304`: **2,108s**
  - Both show:
    - `REVIEWER_MODELS`: 6-model panel
    - `ENABLE_REVIEWER_TWO_PASS: true`
    - `REVIEWER_REASONING_EFFORT: xhigh`
    - `EDITOR_REASONING_EFFORT: xhigh`
- **Root cause**
  - Heavy review configuration is applied broadly, including paths that are small-diff, forced-review, or comment-only.
- **Exact change**
  - For low-risk diffs or comment-only/Claude-branch review paths:
    - start with 2–3 reviewers,
    - disable second pass unless reviewers disagree,
    - lower reasoning from `xhigh` to `high`/`medium`.
- **Estimated time savings**
  - **5–15 minutes** on medium review runs.
- **Implementation risk**
  - **Medium**. Keep escalation-on-disagreement to preserve quality.

---

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Cut `workflow_log_analysis` summarization scope
- **Evidence**
  - Observed `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs`:
    - **8 ops**
    - **1,218,871 total tokens**
    - per-op range **116,787–154,202**
    - examples: `25246056978` used **153,540**, `25245013179` used **154,202**, `25244066270` used **137,610**
- **Root cause**
  - The workflow is spending LLM budget summarizing large sets of unselected runs, including many repetitive healthy CI runs.
- **Exact change**
  - Only summarize unselected runs for:
    - families with failures/cancellations,
    - p95 outliers,
    - one periodic healthy sample per family.
  - Deduplicate near-identical CI success runs before summarization.
- **Estimated savings**
  - **40–60%** of current `workflow_log_analysis` token spend in the sampled window.
- **Quality-risk notes**
  - Low if failures/slow runs remain fully covered and each family retains at least one healthy baseline sample.

### 2. Downshift model breadth and reasoning on low-risk `review_autofix` paths
- **Evidence**
  - `review_autofix` sampled config includes:
    - 6 reviewer models
    - `ENABLE_REVIEWER_TWO_PASS=true`
    - reviewer/editor reasoning at `xhigh`
    - summarizer `gpt-5.4-mini`
  - Long runs `25249290449` and `25249326304` suggest this is materially expensive.
- **Root cause**
  - Overly expensive review defaults are applied where smaller policies would suffice.
- **Exact change**
  - Add a policy matrix:
    - docs-only/comment-only/small diff: 1–2 reviewers, no second pass, lower reasoning
    - normal diff: 2–3 reviewers
    - escalate to full panel only on disagreement/high-risk files
- **Estimated savings**
  - **40–70% reviewer-token reduction** on low-risk paths.
- **Quality-risk notes**
  - Medium; mitigate by escalating automatically on disagreement, risky file patterns, or failed checks.

### 3. Remove avoidable reruns and no-op implement loops
- **Evidence**
  - Implement failures repeatedly log “announced edit/apply_patch ... but produced no file changes.”
  - Stable-release E2E failures consume **6,255s–6,478s** before failing.
- **Root cause**
  - General-purpose retry loops run even when work is deterministic or impossible due to prior state.
- **Exact change**
  - Add:
    - deterministic fast-path for smoke tests,
    - fast-fail if PR is already merged/closed before bait injection,
    - early no-op success when target file already matches expected contents.
- **Estimated savings**
  - Moderate-to-high; mostly from eliminating whole reruns rather than shaving a single prompt.
- **Quality-risk notes**
  - Low, provided exact-match guards are strict.

### 4. Stabilize prompt prefixes so prompt caching can actually help
- **Evidence**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` is common in implement/review/poll runs.
  - But observed cache probe lines show `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`, so cache effectiveness is not measurable.
  - Implement logs repeatedly print long static Serena/API-hygiene instruction blocks across attempts.
- **Root cause**
  - Repeated dynamic noise and repeated full static blocks likely fragment cacheable prefixes.
- **Exact change**
  - Move invariant prompt scaffolding into shared files loaded once.
  - Append dynamic run-specific diagnostics **after** the stable prefix.
  - Emit non-`na` cache metrics for every cache-enabled call.
- **Estimated savings**
  - Likely **10–25% input-token reduction** on retried implement/review prompts, but currently unmeasurable.
- **Quality-risk notes**
  - Low. This is a packaging/layout change, not a behavior change.

### 5. Lower reasoning level for exact-content/single-file implement tasks
- **Evidence**
  - Smoke-test implement runs use `MODEL_EDITOR: openai/gpt-5.3-codex` with `MODEL_REASONING_EFFORT: xhigh`.
- **Root cause**
  - High-reasoning mode is unnecessary for exact overwrite tasks.
- **Exact change**
  - If task classifier says:
    - single known file,
    - exact output provided,
    - no architectural ambiguity,
    then use a lower-cost mode or bypass LLM entirely.
- **Estimated savings**
  - Small per run, but strong on repeated smoke-test traffic.
- **Quality-risk notes**
  - Low if restricted to deterministic tasks.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Block auto-merge/deterministic-skip until stable-release E2E review verification finishes
- **Failure evidence**
  - `test_and_mark_stable` failures `25247210528` and `25249170035`.
  - Log says PR was **already merged before bait could be injected** and that no review run for the bait SHA ever appeared.
- **Root cause category**
  - Cross-workflow race / state transition ordering.
- **Exact fix**
  - Introduce an explicit E2E hold flag/label respected by `review_autofix` auto-merge and deterministic-skip logic.
- **Expected reliability impact**
  - Very high; likely the difference between pass/fail for the entire stable test family.
- **Rollback/fail-open considerations**
  - If the hold flag is absent, current behavior continues. Safe to roll back.

### 2. Add retry/fallback around actionlint install
- **Failure evidence**
  - CI run `25249161547` failed in `Install actionlint` because `curl` returned **HTTP 502**; job exited with code 22.
- **Root cause category**
  - External fetch transient / missing retry.
- **Exact fix**
  - Wrap the `curl` download in 3-attempt exponential backoff.
  - If download still fails, try a second GitHub-native path (`gh release download` or cached local binary if present).
- **Expected reliability impact**
  - Should remove the sampled **1/57 CI** failure mode.
- **Rollback/fail-open considerations**
  - Fail closed only after retries/fallbacks are exhausted.

### 3. Fix review telemetry/support-script variable handling
- **Failure evidence**
  - `review_autofix` run `25252889046` logged:
    - `SUPPORT_SERENA_DIR: unbound variable`
    - `RUNTIME_DIR/PREVIOUS_REVIEWS_DIR not set`
    - `memory helper script missing; skipping run-end failure event`
- **Root cause category**
  - Shell strict-mode brittleness / missing support artifacts on cancelled paths.
- **Exact fix**
  - Guard all support-dir references with `${VAR:-}`.
  - Skip Serena stat/report steps cleanly if workspace/bootstrap did not complete.
  - Make memory-helper absence a silent metric, not a warning that masks real failures.
- **Expected reliability impact**
  - Medium; fewer housekeeping failures, fewer misleading warnings, better telemetry completeness.
- **Rollback/fail-open considerations**
  - These steps should remain fail-open; never block the main review outcome.

### 4. Precheck deterministic smoke-test tasks before invoking Codex
- **Failure evidence**
  - Implement smoke-test failures repeatedly ended with no diff despite exact instructions.
- **Root cause category**
  - Wrong execution strategy for deterministic work.
- **Exact fix**
  - Add content-match precheck and direct write/no-op branch.
- **Expected reliability impact**
  - High for smoke-test and similar exact-content issue templates; removes a clustered failure class in `implement`.
- **Rollback/fail-open considerations**
  - If the classifier is uncertain, fall back to the current Codex path.

### 5. Harden token/scope validation on review paths
- **Failure evidence**
  - Cancelled `review_autofix` run `25252211619` warned: `Token may be invalid, expired, or missing actions:read`.
- **Root cause category**
  - Credential/scope preflight gap.
- **Exact fix**
  - Add a lightweight preflight for required scopes before review starts; fail fast with a targeted message and skip nonessential follow-up calls.
- **Expected reliability impact**
  - Medium for reducing confusing late cancellations.
- **Rollback/fail-open considerations**
  - Nonessential steps should still fail open; only block paths that truly require the scope.

---

## AI Memory Health

- **Observed telemetry coverage**
  - Found **106 JSON `AI_MEMORY_TELEMETRY` events** across sampled deep-dive logs.
  - Operation mix:
    - `record-run-event`: **44**
    - `processed-command-check`: **17**
    - `retrieve`: **17**
    - `processed-command-claim`: **16**
    - `summarize_unselected_runs`: **8**
    - `record-candidate`: **2**
    - `compact`: **2**

- **Retrieve effectiveness**
  - `retrieve` ops observed: **17**
  - **Hit rate:** **88.2%** (`15/17` had `records_selected > 0`)
  - **Average `estimated_tokens`:** **42.8**
  - **Min/max `estimated_tokens`:** **0 / 56**
  - **Keyword method distribution:**
    - `plain`: **15**
    - `none`: **2**
    - `llm`: **0 observed**
  - **Interpretation:** memory retrieval is lightweight and generally effective where it is enabled, but there is no emitted explicit budget field, so budget compliance cannot be directly verified.

- **Zero-record retrieves**
  - Two zero-hit retrieves were observed, both in `workflow_log_analysis`:
    - `25246650500`
    - `25246056978`
  - These are worth reviewing because they imply either weak query formation or stale/irrelevant memory for analysis tasks.

- **Fail-open / disabled flags**
  - No JSON telemetry with `fail_open: true` or `enabled: false` was observed in the sampled logs.
  - However, review runs still show **fail-open behavior in shell logs**:
    - `25252889046`
    - `25252898559`
    - warning: `memory helper script missing; skipping run-end failure event`
  - So memory is not “failing loudly,” but it is also not reliably emitting end-of-run data on some review paths.

- **Push retries**
  - Three telemetry records showed elevated `push_attempts: 2`:
    - `implement` run `25246727158` (`phase_failed`)
    - `implement` runs `25244121942` and `25243569299` (`phase_started`)
  - This is not catastrophic, but it is a concrete sign that memory writes occasionally need retry.

- **Compaction health**
  - `memory_maintenance` run `25250870360` emitted:
    - `op: compact`
    - `archived_candidates: 2914`
    - `did_push: true`
    - `ok: true`
  - Compaction appears healthy in the sampled window.

- **Recommendation**
  - Keep current memory retrieval behavior, but:
    1. restore review-path helper availability,
    2. emit explicit retrieve budget fields,
    3. alert on zero-record retrieve streaks for the same workflow family.

---

## GH API Call Audit

### 1. Batch PR metadata/file-list/linked-issue fetches and reuse them within a run
- **Evidence**
  - `review_autofix` gate (`25252889046`) separately calls:
    - `gh api repos/.../pulls/${PR_NUMBER}`
    - `gh api repos/.../commits/${PR_HEAD_SHA}`
    - `gh api --paginate repos/.../pulls/${PR_NUMBER}/files`
  - `review_post-merge-validate-dispatch` (`25252910903`) separately calls:
    - `gh api graphql` for linked issues
    - then `gh api repos/.../pulls/${PR_NUMBER}` as fallback
  - `issue_pr_status` (`25252910896`) repeats the same linked-issue GraphQL + PR fallback pattern.
- **Redundancy pattern**
  - Same PR facts are looked up in multiple jobs/steps of the same lifecycle.
- **Concrete change**
  - Fetch one PR context blob once per run (PR state, labels, head SHA, changed files, linked issues, title/body) and persist it in `$RUNNER_TEMP` for cycle-local reuse.
- **Estimated call-count reduction**
  - **3–6 API calls per review/status run**, plus less repeated pagination.
- **Rate-limit risk reduction**
  - Medium; especially valuable when many PR events land close together.

### 2. Stop calling `/rate_limit` on every cancellation loop unless a 403 occurs
- **Evidence**
  - `cancel_on_pr_close` logs show `_rl_wait` uses `gh api -i /rate_limit`, even when no target runs exist.
  - Workflow-log-analysis audit also points to `/rate_limit` usage in `cancel_on_pr_close` and `mark-stable`.
- **Redundancy pattern**
  - Proactive rate-limit checks are performed even on trivial/no-op paths.
- **Concrete change**
  - Only call `/rate_limit`:
    - after a 403/rate-limit response,
    - or once before a large batch of cancellations.
- **Estimated call-count reduction**
  - **50%+** on no-op cancellation paths.
- **Rate-limit risk reduction**
  - High leverage because it removes “defensive” API traffic that itself contributes to pressure.

### 3. Collapse E2E poll loops into one cached per-iteration snapshot
- **Evidence**
  - `test_and_mark_stable` run `25249170035` shows **~41 `gh api` log hits** in `e2e-smoke-test` alone.
  - The log repeatedly polls issue labels, workflow runs, PR state, and review status across phases.
- **Redundancy pattern**
  - Each phase re-queries overlapping state independently.
- **Concrete change**
  - Per poll tick, fetch a single state bundle:
    - issue labels/state,
    - PR state/head SHA,
    - latest runs for clarify/plan/implement/review,
    then reuse it across phase logic.
- **Estimated call-count reduction**
  - **30–50%** in the E2E monitor loop.
- **Rate-limit risk reduction**
  - High, especially during long-running stable tests.

### 4. Reuse fetched data across `workflow_log_analysis` steps
- **Evidence**
  - Sampled grep shows very high `gh api` log-line volume in `workflow_log_analysis` deep dives; sampled analysis summaries cite runs like `25246056978` at **~204** `gh api` log-line hits.
- **Redundancy pattern**
  - `api-redundancy`, `deep-audit`, and `analyze-commit-notify` appear to traverse overlapping run/job metadata.
- **Concrete change**
  - Persist raw run/job/step/API extracts once, then feed the cached JSON to downstream analysis steps.
- **Estimated call-count reduction**
  - **20–40%** for that workflow.
- **Rate-limit risk reduction**
  - High, because this job already brushes rate-limit handling in prior deep-dive analyses.

### Repository-specific API hygiene alignment
- These recommendations follow the repository’s implied hygiene goals:
  - **Mandatory batching:** combine PR-linked data into one fetch.
  - **Cycle-local caches:** reuse data inside the same workflow run via temp JSON.
  - **Fail-open behavior:** if cache read fails, re-fetch once and continue rather than aborting the workflow.

---

## MCP & Serena Efficiency

- **What is working**
  - Implement run `25243564804` showed strong Serena adoption:
    - **261 Serena tool calls**
    - **94% efficiency**
    - top tools: `replace_symbol_body`, `insert_after_symbol`, `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`
    - estimated tokens with Serena: **~26,870**
    - estimated tokens without Serena: **~178,100**
  - That indicates the semantic-tool-first strategy is effective when the run reaches real editing work.

- **What is inefficient**
  - The logs repeatedly print long Serena/MCP instruction blocks and prompt scaffolding across attempts.
  - Review cancellation `25252889046` failed to produce usable Serena stats because `SUPPORT_SERENA_DIR` was unset.
  - `No Serena tool usage stats found` appears in several implement/review logs, which weakens observability.
  - `GIT_MCP_DISABLED: true` was observed in implement logs, so git context still falls back to shell/GitHub API even where targeted Git MCP could reduce churn.

### Recommendations

#### 1. Keep Serena semantic navigation; reduce prompt/tooling churn around it
- **Evidence**
  - Good Serena usage in `25243564804`, but repeated static instructions around each attempt.
- **Concrete change**
  - Load Serena policy text once; keep retry-specific deltas separate.
- **Expected impact**
  - Better token efficiency and faster retries without changing tool behavior.

#### 2. Fix Serena stats/report prerequisites on review paths
- **Evidence**
  - `25252889046`: `SUPPORT_SERENA_DIR: unbound variable`, skipped report generation.
- **Concrete change**
  - Make stats/report generation conditional on runtime workspace/support-dir existence.
- **Expected impact**
  - Better MCP observability and fewer noisy cancelled-path warnings.

#### 3. Enable optional Git MCP in review/edit flows where already supported
- **Evidence**
  - Implement logs show `GIT_MCP_DISABLED: true`.
  - Review gate still uses raw `gh api` and shell Git for scoped context.
- **Concrete change**
  - In review/edit flows, prefer targeted Git MCP reads (`git_status`, `git_diff`, `git_show`, `git_log`, `git_branch`) with fail-open fallback.
- **Expected impact**
  - Lower token/context bloat and fewer raw git shell calls.
- **Risk**
  - Medium; keep it optional and fail-open.

#### 4. Parallelize independent read-side lookups safely
- **Evidence**
  - Review/status steps serialize PR fetch, file list fetch, linked-issue lookup, and fallback fetches.
- **Concrete change**
  - Run independent metadata reads in parallel before prompt assembly.
- **Expected impact**
  - Small-to-medium latency win on review/status jobs.

---

## Prompt Cache & Memory System

- **Prompt cache state**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` is consistently present in sampled implement/review/poll runs.
  - However, observed cache probe lines show:
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - So the cache may be enabled, but **hit/miss effectiveness is not measurable** in the sampled window.

- **Cache fragmentation causes**
  - Repeated env dumps and long static instructions in implement/review retries.
  - Dynamic run-specific diagnostics appear mixed into otherwise stable system scaffolding.
  - Review/comment-only cancellation paths appear to skip support/runtime setup, reducing consistent cacheable prompt assembly.

- **Memory retrieval effectiveness**
  - Memory retrieval is cheap and usually works in implement paths (88.2% retrieve hit rate).
  - Review paths frequently miss memory end-of-run recording due missing helper scripts.

### Recommendations

#### 1. Emit real prompt-cache metrics for every cache-enabled call
- **Evidence**
  - Cache probe metrics are `na`.
- **Change**
  - Require `cache_creation_input_tokens` and `cache_read_input_tokens` to be populated in cache-enabled review/implement calls.
- **Impact**
  - Better cost visibility immediately; enables real tuning later.
- **Reliability**
  - Low risk.

#### 2. Restructure prompts into stable prefix + dynamic suffix
- **Evidence**
  - Implement logs repeatedly print large static Serena/API-hygiene blocks across attempts.
- **Change**
  - Keep invariant system/policy text at the front, append retry recaps and per-run diagnostics at the end.
- **Impact**
  - Likely medium token/latency savings from better cache reuse.
- **Reliability**
  - Low risk.

#### 3. Preserve helper/script availability on cancelled review paths
- **Evidence**
  - `memory helper script missing` and skipped Serena report generation in `25252889046`/`25252898559`.
- **Change**
  - Fetch or stage support scripts before optional review branches split, or cleanly suppress memory/cache-report steps when support files are absent.
- **Impact**
  - Better memory continuity and more trustworthy telemetry.
- **Reliability**
  - Medium.

#### 4. Keep non-prompt caches healthy
- **Evidence**
  - Recent `plan`/`implement` logs show UV/Codex cache hits and “not saving cache.”
- **Change**
  - No major change needed; this part is healthy.
- **Impact**
  - Maintain current behavior.

---

## Orchestrator Health

- **Healthy signs**
  - `orchestrate_poll` is stable: **16/16 success**, **p50 42s**, **p95 46.25s**.
  - Poll ledger events (`poll_started`, `poll_completed`) are being recorded.

- **Pain points**
  - Massive skipped-run fan-out:
    - `clarify`: **225 total**, only **32 success**
    - `plan`: **187 total**, only **26 success**
    - `implement`: **187 total**, only **21 success**
    - `orchestrate_clarify_respond`: **187 total**, only **7 success**
  - Review paths can stall or cancel after gate decisions, especially on Claude-branch/comment-only flows.
  - Stable-release orchestration is vulnerable to race conditions across implement/review/merge.

### Smallest safe mitigations
1. **Move dispatch gating upstream** so child workflows are not launched when the comment/event cannot possibly match.
2. **Add explicit E2E hold state** spanning implement → review → merge.
3. **Track queue share** for non-work runs and fail-open telemetry gaps separately from true workflow failures.

### Observable indicators to track
- Skipped-run ratio per family
- Runner-wait share of total duration
- `% review_autofix runs ending in comment-only/cancelled path`
- Stable-release E2E pass rate
- `% cache-enabled calls with non-`na` cache metrics`
- `% review runs with complete memory end-event`

---

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Merge/conflict overhead: implement → review/autofix → stable E2E
- **Observed bottleneck**
  - Stable E2E fails when review/merge state outruns bait injection and review verification.
- **Impact**
  - Dominant blocker for release flow.
- **Fix**
  - E2E hold flag respected across review and auto-merge.

### 2. Compute-heavy jobs: CI and workflow-log analysis
- **Observed bottleneck**
  - `ci` runs are consistently **551–656s**.
  - `workflow_log_analysis` runs are **3,320–6,075s**.
- **Impact**
  - Long wall-clock occupancy and queue competition.
- **Fix**
  - For analysis: reduce summarization scope and broad Git/API fetches.
  - For CI: harden transient downloads and consider separate lightweight gating for non-code changes if later evidence supports it.

### 3. Retry and polling overhead: implement + E2E monitor loops
- **Observed bottleneck**
  - Implement retries no-op on exact smoke-test tasks.
  - Stable E2E repeatedly polls every 10s/20s for long periods.
- **Impact**
  - Burns runner time and API budget without moving work forward.
- **Fix**
  - Deterministic fast-path + cached poll snapshots + early impossible-state fail.

### 4. Queueing overhead: fan-out of skipped workflows
- **Observed bottleneck**
  - High volume of skipped clarify/plan/respond/implement runs.
  - Runner wait appears even on short jobs.
- **Impact**
  - Indirectly slows real work by consuming scheduling bandwidth.
- **Fix**
  - Upstream gating and event deduplication.

### 5. Review/autofix over-processing
- **Observed bottleneck**
  - Full 6-model, 2-pass, xhigh-reasoning review runs for paths that often do not need it.
- **Impact**
  - Slows medium-complexity reviews and raises cost.
- **Fix**
  - Risk-tiered reviewer policy with escalation.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Stable-release E2E race between implement/review/automerge (`25247210528`, `25249170035`)
- Long, repetitive `ci` runtime (**p50 610s**, **p95 645.8s**)
- Heavy `workflow_log_analysis` runtime and token spend (**up to 6,075s**, **1.22M observed summary tokens**)
- High skipped-run fan-out in clarify/plan/implement/respond
- Broad-tag Git fetch in poll/analysis jobs

**Top failure modes**
- `test_and_mark_stable` fails because PR merges before bait/review verification
- `implement` smoke-test tasks fail after no-op Codex attempts on exact deterministic work
- `ci` transient actionlint download failure (`curl 502`)
- `review_autofix` support-script/runtime-context gaps (`SUPPORT_SERENA_DIR` unset, missing memory helper)

**Highest-cost drivers**
- `workflow_log_analysis` summarization token spend
- `review_autofix` multi-model/two-pass/xhigh review config
- Repeated prompt scaffolding with unmeasurable cache hits
- Reruns caused by stable E2E and implement no-op loops

**Top 3 prioritized actions**
1. **Fix the E2E auto-merge race** with an explicit E2E hold that blocks merge/deterministic skip until verification completes.
2. **Add deterministic implement fast-path** for exact smoke-test tasks to bypass Codex entirely.
3. **Reduce workflow-log-analysis scope** and cache/reuse fetched run metadata to cut both API volume and token spend.

---

## Metrics Appendix

### Overall scope

| Scope | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| All sampled runs | 1000 | 260 | 10 | 41 | 689 | 1.0% | 121.69 | 1.0 | 612.0 |
| shubhodeep1/coding-workflows | 1000 | 260 | 10 | 41 | 689 | 1.0% | 121.69 | 1.0 | 612.05 |

### Key workflow-family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 57 | 56 | 1 | 0 | 0 | 1.75% | 599.32 | 610.0 | 645.8 |
| implement | 187 | 21 | 7 | 7 | 152 | 3.74% | 31.01 | 1.0 | 207.4 |
| review_autofix | 55 | 26 | 0 | 28 | 1 | 0.0% | 333.73 | 47.0 | 1664.6 |
| test_and_mark_stable | 6 | 0 | 2 | 4 | 0 | 33.33% | 3921.67 | 3462.0 | 6422.25 |
| orchestrate_poll | 16 | 16 | 0 | 0 | 0 | 0.0% | 42.38 | 42.0 | 46.25 |
| plan | 187 | 26 | 0 | 0 | 161 | 0.0% | 12.82 | 1.0 | 143.4 |
| clarify | 225 | 32 | 0 | 0 | 193 | 0.0% | 19.60 | 1.0 | 127.0 |
| workflow_log_analysis | 6 | 4 | 0 | 2 | 0 | 0.0% | 4484.5 | 4202.0 | 6025.0 |

### Observed token and memory metrics

| Metric | Observed value | Notes |
|---|---:|---|
| `summarize_unselected_runs` ops | 8 | From JSON `AI_MEMORY_TELEMETRY` |
| `summarize_unselected_runs` total tokens | 1,218,871 | Only for sampled deep-dive runs; not whole pipeline |
| Per-op summary token range | 116,787–154,202 | Examples include runs `25246056978`, `25245013179`, `25244066270` |
| `retrieve` ops | 17 | Memory retrieval events |
| Retrieve hit rate | 88.2% | `15/17` with `records_selected > 0` |
| Avg retrieve `estimated_tokens` | 42.8 | Max observed 56 |
| Retrieve keyword method | `plain`: 15, `none`: 2 | No `llm` observed |
| Retrieve zero-hit runs | 2 | `workflow_log_analysis` runs `25246650500`, `25246056978` |

### Prompt/cache observability

| Cache metric | Observation | Gap |
|---|---|---|
| OpenRouter prompt cache enabled | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) | Present in implement/review/poll samples |
| Prompt cache hit/read metrics | `cache_creation_input_tokens=na`, `cache_read_input_tokens=na` in observed review cache probe lines | Real cache effectiveness not measurable |
| UV/setup caches | Healthy cache hits observed in plan/implement logs | No major issue seen |
| Memory helper availability | Missing on some cancelled review paths | Causes telemetry blind spots |

### GH API summary from sampled deep dives

| Run ID | Workflow family | Step / hotspot | Observed signal |
|---|---|---|---|
| 25249170035 | test_and_mark_stable | `e2e-smoke-test` | ~41 `gh api` log-line hits; repeated phase polling |
| 25246056978 | workflow_log_analysis | analysis steps | sampled analysis cites ~204 `gh api` log-line hits; high API pressure |
| 25252889046 | review_autofix | `gate`, `resolve-claude-branch-pr` | repeated PR/commit/files lookups; duplicate fetch patterns |
| 25252910896 | issue_pr_status | `Update linked issue labels when PR closes` | GraphQL linked-issue query + PR fallback + issue metadata lookups |
| 25252910890 | cancel_on_pr_close | cancellation helper | `/rate_limit` probe plus per-run cancel POST path |
| 25252460199 | orchestrate_poll | checkout/setup | broad Git fetch rather than API-heavy, but still a major latency source |

### Notable outlier runs

| Run ID | Workflow family | Conclusion | Duration (s) | Key issue |
|---|---|---:|---:|---|
| 25247210528 | test_and_mark_stable | failure | 6478 | Phase 4b bait-line verification failure |
| 25249170035 | test_and_mark_stable | failure | 6255 | PR merged before bait/review verification |
| 25249181773 | workflow_log_analysis | success | 5875 | Very long analysis runtime |
| 25249326304 | review_autofix | success | 2108 | Heavy multi-model review config |
| 25249290449 | review_autofix | success | 1107 | Heavy multi-model review config |
| 25249161547 | ci | failure | 13 | `actionlint` download `curl 502` |
| 25243564804 | implement | failure | 190 | Exact smoke-test task, no file changes after 2 attempts |

If you want, I can also turn this into a **PR-ready action list** sorted by **“edit these workflows/scripts first”**.
