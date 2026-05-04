## Executive Summary

- **Fix the stable-release gate first.** All 4 sampled `test_and_mark_stable` runs failed, with durations from **2,424s to 3,461s**; failures clustered in `e2e-smoke-test / Phase 4b: Verify editor removed bait line` and `sync-to-main / Dispatch forward-merge-stable-to-main`. This is currently the largest end-to-end blocker and wastes **25–55 minutes per release attempt**. **Estimated impact:** high. **Confidence:** high.

- **`review_autofix` is the main latency and AI-cost sink.** Family p95 is **1,713.8s** over 79 runs, and slow runs such as `#25278175531` (**1,898s**), `#25308327160` (**1,396s**), and `#25308285783` (**867s**) were dominated by `review / codex-agent`, often on comment-only/Claude-branch paths. **Estimated impact:** save **6–17 minutes** on affected runs plus major token savings. **Confidence:** high.

- **The implement retry loop is burning tokens on stale context instead of converging.** Failed implement run `#25293966619` consumed at least **42,989 visible tokens** across attempts 2–5, including a **23,176-token** attempt that still ended in a no-change failure after a plan/code mismatch on `.github/workflows/orchestrate_poll.yml`. **Estimated impact:** save **tens of thousands of tokens** and **2–5 minutes** per failed stale-context run. **Confidence:** high.

- **`workflow_log_analysis` is itself expensive enough to deserve optimization.** Slow run `#25308096512` took **2,556s**, and its two visible Codex-heavy sections consumed **373,375 tokens** (`deep-audit`) and **165,545 tokens** (`api-redundancy`) before counting other analysis steps. **Estimated impact:** cut analysis cost/time by **50%+** and **15–25 minutes** per run. **Confidence:** high.

- **AI memory is helping implement flows but not reviewer flows.** Across deep-dive logs there were **17** `retrieve` operations with a **64.7% hit rate**; implement retrieves usually returned **1 record / 28 estimated tokens**, while reviewer retrieves in slow/failing `review_autofix` runs repeatedly returned **0 records**. **Estimated impact:** moderate latency/token reduction on long reviews once reviewer retrieval is improved. **Confidence:** medium.

- **GH API hygiene issues are concentrated in release, review, issue-sync, and artifact-cleanup paths.** Evidence shows repeated run polling, re-fetching PR context, `/rate_limit` probing in retry helpers, and per-artifact cleanup loops. **Estimated impact:** remove **20–40 calls** from release-gate runs and **dozens** from long review runs, reducing rate-limit risk and some runner time. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Repair the `test_and_mark_stable` critical path
- **Evidence**
  - Family metrics: `test_and_mark_stable` has **4 runs, 4 failures, avg 2,820s, p50 2,697.5s, p95 3,380s**.
  - Run `#25281876234` failed in `sync-to-main / Dispatch forward-merge-stable-to-main`; the step ran `gh workflow run forward-merge-stable-to-main.yml --ref stable` and then hit `failed to run git: fatal: not a git repository`.
  - Run `#25308071039` failed in `Phase 4b: Verify editor removed bait line`; the verify step logged `Editor failed to remove bait line E2E_EDITOR_BAIT_25308071039`.
  - Earlier failures (`#25300046587`, `#25305535590`) show the PR could already be merged before bait injection, which turns a race into a later smoke failure.
- **Root cause**
  - Two reliability bugs on the same critical path:
    1. dispatching from a non-repo context;
    2. a brittle bait-removal sequence that races auto-merge/review timing.
- **Exact change**
  - In `sync-to-main`, call `gh workflow run ... --repo shubhodeep1/coding-workflows` from a guaranteed checked-out repo root.
  - In the smoke test, gate auto-merge until the smoke review phase completes, and fail immediately if the PR is already merged/closed before bait injection instead of surfacing it later as a Phase 4b failure.
  - Keep bait verification pinned to the bait commit SHA, but short-circuit when no qualifying review run ever appears.
- **Estimated time savings**
  - **25–55 minutes per failed release attempt** by preventing full-path failures.
- **Implementation risk**
  - **Low-medium**; behavior stays the same when the happy path works, but the smoke-test orchestration logic changes.

### 2. Slim `review_autofix` on comment-only / Claude-branch-review paths
- **Evidence**
  - `review_autofix` family: **79 total**, **38 success**, **1 failure**, **38 cancelled**, **p95 1,713.8s**.
  - Slow run `#25278175531` took **1,898s**; `review / codex-agent (claude-branch-review)` dominated.
  - Success run `#25308327160` took **1,396s** and was dominated by `review   codex-agent`.
  - Success run `#25308285783` still took **867s** with the review step dominating.
  - Cancelled run `#25310281231` explicitly logged `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`.
- **Root cause**
  - The pipeline still pays for a heavy multi-model reviewer path and full runner setup even when it already knows the run is comment-only.
- **Exact change**
  - On comment-only / Claude-branch-review paths:
    - cut reviewer fan-out from 6 models to 2–3;
    - skip `free-disk-space` and other heavy setup steps unless the path will actually edit or validate code;
    - avoid launching extra runner-backed downstream jobs when gate output already guarantees comment-only.
- **Estimated time savings**
  - **6–17 minutes per affected run**, plus **45–120s** more from skipping disk-reclaim/setup overhead.
- **Implementation risk**
  - **Medium**; review quality risk exists if the panel is reduced too aggressively. Keep the full panel for force-review, large diffs, or risky file classes.

### 3. Move no-op implement gating ahead of runner allocation
- **Evidence**
  - From run summaries:
    - `implement #25308222359` took **150s** and then skipped because **issue #2071 was closed**.
    - `implement #25308218751` took **225s** and skipped because **issue #2069 was not in `ai:awaiting-approval`**; runner allocation alone spanned roughly **08:10:33Z → 08:14:09Z**.
- **Root cause**
  - Closed-state / phase-label checks are happening after the job has already claimed a hosted runner.
- **Exact change**
  - Replicate the existing closed/phase-label prechecks in the workflow-level `if:` or in the caller that dispatches implement runs.
  - Only start the runner-backed implement job when the issue is open and still in the approval phase.
- **Estimated time savings**
  - **2.5–4 minutes per no-op implement run**.
- **Implementation risk**
  - **Low**; this is moving existing logic earlier, not changing semantics.

### 4. Refresh implement context after the first failed patch instead of replaying 5 attempts
- **Evidence**
  - `implement #25293966619`:
    - attempt 2: **5,339 tokens**, no file changes;
    - attempt 3: **23,176 tokens**, explicit plan/code mismatch on `.github/workflows/orchestrate_poll.yml`;
    - attempt 4: **1,082 tokens**, announced edit but no changes;
    - attempt 5: **13,392 tokens**, still no file changes.
  - The run ended after 5 attempts and **331s** total.
- **Root cause**
  - The retry loop keeps replaying a stale prompt against changed or mismatched file contents instead of reacquiring the exact target context.
- **Exact change**
  - On the first patch/apply miss:
    - do one scoped reread of the target file(s),
    - regenerate a smaller retry prompt from current contents,
    - abort after 2 consecutive no-change retries instead of exhausting all 5 attempts.
- **Estimated time savings**
  - **150–300s** and **40k+ tokens** on stale-context failures.
- **Implementation risk**
  - **Low**; first-attempt behavior stays intact.

### 5. Skip `free-disk-space` on lightweight review paths
- **Evidence**
  - In slow `review_autofix #25278175531`, `free-disk-space` started around **11:41:28Z** and the next major progress markers appeared around **11:43:30Z** — roughly **2 minutes** of overhead.
  - In cancelled `#25310281231`, the same path still spent roughly **47s** before later runtime warnings surfaced.
- **Root cause**
  - Disk reclaim is being paid even when the job is not going to perform large editor/build workloads.
- **Exact change**
  - Guard disk-reclaim behind a size/risk predicate:
    - full editor path or large diff => keep it;
    - comment-only/docs-only => skip it.
- **Estimated time savings**
  - **45–120s** per lightweight review run.
- **Implementation risk**
  - **Low** if the current safeguard remains enabled for edit-heavy paths.

### 6. Batch `workflow_log_analysis` report commits into one push
- **Evidence**
  - In run `#25308096512`, both `deep-audit` and `api-redundancy` separately did:
    - `git add`,
    - `git commit`,
    - `git pull --rebase`,
    - `git push`.
- **Root cause**
  - The workflow commits partial report sections independently instead of appending all sections and pushing once.
- **Exact change**
  - Produce all report sections first, then do a single `git add/commit/pull --rebase/push` at workflow end.
- **Estimated time savings**
  - **10–30s per analysis run** plus lower rebase/churn risk.
- **Implementation risk**
  - **Low**.

### 7. Reduce full-history/no-op fetch work in `forward_merge_stable_to_main` and similar maintenance flows
- **Evidence**
  - `forward_merge_stable_to_main #25310399556` finished in **21s** and was a no-op (`AHEAD="0"`), but still spent most of the run fetching refs and remote branches.
  - `orchestrate_poll #25310004315` spent about **9.5s** in checkout out of a **47s** run.
- **Root cause**
  - No-op checks happen after heavyweight fetch/checkout work.
- **Exact change**
  - Use a shallow, branch-limited fetch for the initial no-op test; deepen only if real work is needed.
- **Estimated time savings**
  - **8–15s** per poll/promote/forward-merge run.
- **Implementation risk**
  - **Low**.

## Cost Optimizations

Ranked by expected token/dollar reduction.

### 1. Narrow `workflow_log_analysis` by default
- **Evidence**
  - `workflow_log_analysis #25308096512` ran **2,556s**.
  - Visible token usage inside the run:
    - `deep-audit`: **373,375 tokens**
    - `api-redundancy`: **165,545 tokens**
  - That is **538,920 observed tokens** before counting the main synthesis step.
- **Root cause**
  - The analysis workflow is doing broad offline analysis on each run, including expensive Codex sections and report-writing overhead.
- **Exact change**
  - Default to:
    - all failures,
    - top slow outliers,
    - the newest N successful runs with changed behavior,
    - and reserve full-scope sweeps for scheduled runs or regression spikes.
- **Estimated savings**
  - **50%+ token reduction** and **15–25 minutes** per analysis run.
- **Quality-risk notes**
  - **Low-medium**; reduce scope by default, but keep a scheduled full pass for coverage.

### 2. Stop paying for stale implement retries
- **Evidence**
  - `implement #25293966619` used at least **42,989 visible tokens** across attempts 2–5:
    - 5,339
    - 23,176
    - 1,082
    - 13,392
  - It still failed after no-change retries.
- **Root cause**
  - Repeated prompt/context expansion with no refreshed target-state read.
- **Exact change**
  - Refresh target file context after the first no-change patch failure, and cap no-change retries lower.
- **Estimated savings**
  - **~40k tokens per stale-context failure**.
- **Quality-risk notes**
  - **Low**; more targeted retries usually improve quality.

### 3. Cut review/editor retry waste in failing `review_autofix` runs
- **Evidence**
  - `review_autofix #25300219172` visibly spent at least **15,059 tokens** in editor/conflict retries:
    - 5,943
    - 1,198
    - 1,144
    - 1,144
    - 1,100
    - 1,199
    - 966
    - 1,536
    - 419
    - 410
  - The same run repeatedly failed reviewer manifest validation, then exhausted conflict retries.
- **Root cause**
  - Token spend is happening after validation has already shown the editor output is structurally invalid.
- **Exact change**
  - Validate reviewer manifest inputs before invoking the editor, and short-circuit to fallback/noop-comment after the first repeated manifest failure instead of re-running the editor.
- **Estimated savings**
  - **10k–15k tokens per failing review run**.
- **Quality-risk notes**
  - **Low-medium**; this trades repeated low-value retries for earlier deterministic fallback.

### 4. Reduce the reviewer panel for comment-only and docs-only paths
- **Evidence**
  - Reviewer model list in recent review runs includes:
    - `minimax/minimax-m2.5`
    - `moonshotai/kimi-k2.5`
    - `deepseek/deepseek-v4-pro`
    - `z-ai/glm-5`
    - `qwen/qwen3.6-plus`
    - `x-ai/grok-4.1-fast`
  - `review_autofix #25308265463` was docs-only and skipped downstream work, yet the workflow still carries the heavy review framework.
- **Root cause**
  - Model selection does not appear tightly coupled to the already-known risk level of the path.
- **Exact change**
  - Use a smaller reviewer subset for:
    - docs-only,
    - comment-only,
    - known Claude-branch-review paths.
- **Estimated savings**
  - **50–80% AI-call savings** on those low-risk review runs.
- **Quality-risk notes**
  - **Medium**; keep the full ensemble for force-review and risky diffs.

### 5. Make prompt-cache effectiveness measurable on real requests
- **Evidence**
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears consistently in implement/review flows.
  - In `review_autofix #25300219172`, cache probes logged `cache_enabled=true` but all useful fields were `na`:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
- **Root cause**
  - Cache probes exist, but the actual reviewer/editor requests do not emit hit/miss metrics.
- **Exact change**
  - Emit cache-read/cache-create counters on the real reviewer/editor calls, not just cache probes.
- **Estimated savings**
  - Unlocks likely **10–30% token savings** on repetitive review loops once the cache is stabilized.
- **Quality-risk notes**
  - **Low**; mostly observability and prompt-shape stabilization.

### 6. Avoid no-op implement/review runs entirely
- **Evidence**
  - Closed / wrong-phase implement runs still consume runner time before skipping.
  - Skipped families are high-volume:
    - `clarify`: **205 total**, **180 other/skipped**
    - `plan`: **174 total**, **153 other/skipped**
    - `orchestrate_clarify_respond`: **174 total**, **170 other/skipped**
- **Root cause**
  - Some no-op flows are filtered too late.
- **Exact change**
  - Push gating earlier at dispatch/workflow-entry level wherever possible.
- **Estimated savings**
  - Modest per run, but meaningful at repo scale.
- **Quality-risk notes**
  - **Low**.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Split and fix the two stable-release failure modes
- **Failure evidence**
  - `#25281876234`: dispatch failure outside a git repo.
  - `#25300046587`, `#25305535590`: smoke sequence can race merged PR state before bait injection.
  - `#25308071039`: bait line remained and Phase 4b failed.
- **Root cause category**
  - Orchestration bug + race condition.
- **Exact fix**
  - Force repo context / `--repo` on workflow dispatch.
  - Prevent auto-merge until smoke review verification completes.
  - Fail fast on already-merged PRs before bait injection.
- **Expected reliability impact**
  - Highest in the dataset; can recover the currently broken release path.
- **Rollback / fail-open**
  - If smoke orchestration cannot guarantee a valid PR state, mark the phase as infra/race failure early rather than as an editor failure.

### 2. Treat missing support assets as preflight failures for high-risk review paths
- **Failure evidence**
  - `review_autofix #25300219172` logged:
    - `memory helper script missing; skipping run-start event`
    - `memory helper script missing; writing fallback reviewer memory context`
    - `memory helper script missing; skipping consensus candidate record`
    - `check_external_branch_advance.sh missing from support scripts; fail-open (skipping gate)`
    - `RUNTIME_CONTEXT_DIR not set; runtime workspace step did not complete`
  - `#25310281231` also logged `memory helper script missing; skipping run-end failure event`.
- **Root cause category**
  - Support-source drift / incomplete bootstrap.
- **Exact fix**
  - Add a single preflight step that verifies required support scripts exist before starting the expensive codex-agent path.
  - If support scripts are missing, downgrade to a safe comment-only outcome rather than proceeding into partial fail-open behavior.
- **Expected reliability impact**
  - Moderate; reduces hard-to-diagnose partial failures.
- **Rollback / fail-open**
  - Keep existing fail-open as fallback behind a flag if preflight causes false positives.

### 3. Fix stale-context implement failures by changing retry strategy
- **Failure evidence**
  - `implement #25293966619` failed after five no-change attempts and explicit plan/code mismatch.
- **Root cause category**
  - Prompt/context staleness.
- **Exact fix**
  - Refresh file context after first apply miss and route back to clarify after bounded no-change attempts.
- **Expected reliability impact**
  - Moderate-high for implement-family failures, which are clustered around this failure mode.
- **Rollback / fail-open**
  - Fall back to the old 5-attempt loop via env flag if needed.

### 4. Reclassify nightly validation self-test failures
- **Failure evidence**
  - `nightly_validation_selftest #25299383150` ended with `fixtures=3 passed=1 failed=2` and exit code 1.
- **Root cause category**
  - Harness/result-classification ambiguity.
- **Exact fix**
  - Split “fixture produced expected red result” from “self-test harness failed,” and fail the workflow only on harness errors.
- **Expected reliability impact**
  - Moderate for nightly signal quality.
- **Rollback / fail-open**
  - Keep current strict failure mode behind a flag while validating fixture expectations.

### 5. Add a smoke-specific conflict resolver short-circuit
- **Failure evidence**
  - `review_autofix #25300219172` logged merge markers in `tests/e2e_smoke_canary.txt`, then `Conflict resolver retry 2/3`, `retry 3/3`, then `Conflict resolver failed after retries`.
- **Root cause category**
  - Generic conflict handling on a known single-file canary case.
- **Exact fix**
  - Detect the smoke-canary conflict signature and resolve it with exact-file overwrite logic before generic resolver retries.
- **Expected reliability impact**
  - Moderate on smoke-related review runs.
- **Rollback / fail-open**
  - Fall back to the generic resolver when the conflict pattern is not an exact canary match.

## AI Memory Health

- **Telemetry coverage**
  - I found **71** `AI_MEMORY_TELEMETRY:` entries across deep-dive logs.

- **Retrieve performance**
  - `retrieve` operations: **17**
  - Retrieves with `records_selected > 0`: **11**
  - **Hit rate:** **64.7%**
  - **Average `estimated_tokens`:** **18.1**
  - **Budget comparison:** not possible in this window because the telemetry did **not** emit an explicit retrieval budget field.

- **`keyword_method` distribution**
  - `plain`: **11**
  - `none`: **6**
  - `llm`: **0 observed**

- **Zero-hit retrievals**
  - **6** retrieves returned **0 records**.
  - All observed zero-hit cases were reviewer-role retrievals in `review_autofix`, including:
    - `#25300219172`
    - `#25278175531`
    - `#25279043495`

- **Fail-open / disabled flags**
  - `fail_open: true` entries: **0 observed in telemetry**
  - `enabled: false` entries: **0 observed**

- **Push retry health**
  - Most memory writes pushed successfully on the first attempt.
  - I observed **2** telemetry entries with `push_attempts: 2`, including `implement #25293966619` on `record-run-event`.
  - This is not severe, but it is worth tracking.

- **Operation mix**
  - `record-run-event`: **28**
  - `retrieve`: **17**
  - `record-candidate`: **3**
  - `processed-command-check`: **8**
  - `processed-command-claim`: **8**
  - `compact`: **2**
  - `summarize_unselected_runs`: **5**

- **Positive signs**
  - `memory_maintenance #25310036626` compacted memory successfully and archived **2,914 candidates** with `push_attempts: 1`.
  - `review_autofix #25300219172` still recorded a consensus candidate despite the overall run failing.

- **Main issues**
  - Reviewer retrieval quality is weak.
  - Memory-helper support assets are intermittently missing in `review_autofix`, which causes degraded memory behavior even when telemetry exists.

- **Recommendations**
  - Add `retrieval_budget_tokens` (or equivalent) to telemetry.
  - Improve reviewer retrieval keys so reviewer-role lookups stop defaulting to `keyword_method=none`.
  - Promote `memory helper script missing` to a tracked operational metric.

## GH API Call Audit

### 1. `test_and_mark_stable` is the highest-value API cleanup target
- **Evidence**
  - Deep-dive `e2e-smoke-test` logs show repeated phase polling, repeated “latest run” discovery, and repeated status checks across clarify/plan/implement/review phases.
  - The repo’s own `workflow_log_analysis #25308096512` identified this path as the most API-dense and estimated **20–40 reducible API calls per release-gate run**.
- **Redundant pattern**
  - Re-listing workflow runs, then polling `actions/runs/{id}`, then re-checking labels/comments separately per phase.
- **Concrete change**
  - Capture the dispatched run ID once per phase and persist it.
  - Reuse a single phase-state bundle for later checks instead of rediscovering runs repeatedly.
- **Expected reduction**
  - **20–40 calls per run** and lower rate-limit risk.

### 2. `review_autofix` re-fetches PR context across adjacent steps
- **Evidence**
  - In slow `review_autofix #25278175531`, `review_codex-agent` fetched:
    - PR payload,
    - issue comments,
    - PR reviews,
    - review comments,
    - linked issues via GraphQL,
    - then paginated check-run snapshots.
  - The same run also configured a rate-limit-aware `gh_retry` path and a long check-run wait loop.
- **Redundant pattern**
  - Same PR context is rebuilt multiple times instead of being assembled once and handed forward.
- **Concrete change**
  - Build one PR context artifact/output at gate time:
    - PR metadata,
    - file list,
    - linked issues,
    - comment/review snapshots,
    - head/base SHAs.
  - Reuse it in reviewer/editor/consolidator steps.
- **Expected reduction**
  - **Dozens of calls** on long review runs.

### 3. `issue_pr_status` uses GraphQL discovery then per-issue REST enrichment
- **Evidence**
  - Recent `issue_pr_status` runs use linked-issue discovery and later per-issue cleanup/lineage steps.
  - The internal `workflow_log_analysis #25308096512` estimated **3–5 reducible calls per PR-close event** here.
- **Redundant pattern**
  - Hybrid GraphQL + N-per-issue REST follow-ups for fields that could be fetched together.
- **Concrete change**
  - Expand the GraphQL query to include the linked-issue fields currently fetched one-by-one, or persist the first REST payload and reuse it.
- **Expected reduction**
  - **3–5 calls per PR-close event**.

### 4. Frequent helper flows probe `/rate_limit` too eagerly
- **Evidence**
  - Recent `cancel_on_pr_close #25310341491` and older poll/review helpers contain `_rl_wait()` / retry wrappers that call `gh api -i /rate_limit`.
  - In the sampled recent runs I did **not** see live 429 failures; this is defensive overhead, not active incident handling.
- **Redundant pattern**
  - `/rate_limit` can be called even when the path is healthy.
- **Concrete change**
  - Only query `/rate_limit` after the first 403/429/rate-limit match, then cache the reset timestamp for the rest of the step.
- **Expected reduction**
  - **1–2 calls per poll/cancel cycle**.

### 5. `copilot_pull_request_reviewer` duplicates artifact metadata retrieval
- **Evidence**
  - `copilot_pull_request_reviewer #25310122827`:
    - `Cleanup artifacts` listed artifacts via `/actions/runs/25310122827/artifacts`,
    - then deleted them one by one,
    - while the agent/upload path separately fetched PR diff/base metadata.
- **Redundant pattern**
  - Metadata is retrieved in separate jobs instead of being passed forward.
- **Concrete change**
  - Persist artifact IDs and PR file/base metadata from `Prepare` or `Agent` for downstream reuse.
- **Expected reduction**
  - **2–4 calls per run**.

### Repo-specific API hygiene alignment
- The repo’s own instructions, echoed inside implement/review logs, explicitly say to batch or reuse existing `gh api` data whenever possible.
- The main violations I observed are **re-fetching already-known context**, not a lack of batching primitives.

## Prompt Cache & Memory System

- **Current state**
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears consistently in:
    - `implement`
    - `review_autofix`
    - `orchestrate_poll`

- **What is working**
  - Cache is at least configured.
  - GitHub Actions cache for Codex CLI is working in `workflow_log_analysis` (`Cache hit occurred on the primary key codex-v0.114.0`).

- **What is missing**
  - Real prompt-cache effectiveness is not measurable from current logs.
  - In `review_autofix #25300219172`, cache probe logs showed `cache_enabled=true` but all useful cache/token fields were `na`.

- **Observed cache-fragmentation risks**
  - Long prompts appear to include dynamic run-specific noise early in the prompt.
  - Implement and review retry loops regenerate prompts multiple times.
  - Comment-only paths still seem to carry large support context and reviewer machinery.

- **Memory interaction**
  - Implement memory retrieval is cheap and often useful (`1 record`, `estimated_tokens=28`).
  - Reviewer memory retrieval is frequently empty, which likely forces more raw prompt context and retries.

- **Concrete improvements**
  1. Emit cache-read/cache-create metrics on actual reviewer/editor requests.
  2. Keep the static prefix stable:
     - system instructions,
     - repo rules,
     - support prompts,
     - reusable context first.
  3. Move volatile data to the suffix:
     - run IDs,
     - retry nudges,
     - transient diagnostics,
     - latest log snippets.
  4. Reuse a preassembled static-context artifact across retries instead of rebuilding the entire prompt.
  5. Improve reviewer memory retrieval so cache and memory both reduce repeated long-context work.

- **Estimated impact**
  - **10–30% token and latency reduction** on repetitive review loops once measured and stabilized.
- **Reliability note**
  - Current cache behavior appears fail-open in practice, which is good; the main issue is wasted opportunity and weak observability.

## Orchestrator Health

- **Healthy signals**
  - `orchestrate_poll` looks operational:
    - **45/45 success**
    - avg **51.8s**
    - p95 **90.6s**
  - Recent run `#25310004315` emitted `poll_completed` telemetry with `push_attempts: 1`.
  - Implement idempotency guards are active: `#25293966619` emitted both `processed-command-check` and `processed-command-claim`.

- **Operational pain points**
  - Workflow chatter is high:
    - `clarify`: **205 total**, **180 other/skipped**
    - `plan`: **174 total**, **153 other/skipped**
    - `orchestrate_clarify_respond`: **174 total**, **170 other/skipped**
  - Most are cheap, but some still incur runner waits when gating happens too late.
  - Support-script availability is inconsistent in `review_autofix`.
  - Comment-only review runs are still expensive enough to get cancelled or linger for many minutes.

- **Smallest safe mitigations**
  - Push more gating into workflow-entry `if:` expressions.
  - Track and alert on:
    - `memory helper script missing`
    - reviewer `retrieve` zero-hit rate
    - implement no-change exhaustion count
    - comment-only `review_autofix` median duration
    - runner allocation time for skipped/no-op runs

- **What to watch**
  - `% of skipped/no-op runs that still waited >30s for a runner`
  - `% of reviewer retrieves with `records_selected=0``
  - `review_autofix` median duration split by:
    - full edit path
    - comment-only path
  - `implement` failure reasons by:
    - stale patch context
    - empty-output/no-change
    - issue already closed/wrong phase

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Respond
- **Bottleneck type:** workflow noise, not compute.
- **Evidence:** p50 for `clarify`, `plan`, and `orchestrate_clarify_respond` is **1s**, but run counts are high and mostly skipped.
- **Fix:** tighten dispatch gating earlier.  
- **Impact:** small per run, moderate at repo scale.

### 2. Implement
- **Bottleneck type:** avoidable runner allocation + retry waste.
- **Evidence:**
  - `#25308222359` and `#25308218751` consumed **150–225s** before determining no work should happen.
  - `#25293966619` consumed **331s** and at least **42,989 visible tokens** in stale retries.
- **Fix order:** move no-op gating before runner allocation, then refresh context on first patch miss.

### 3. Review / Autofix
- **Bottleneck type:** dominant compute, model fan-out, retry overhead.
- **Evidence:**
  - Family p95 **1,713.8s**
  - slow runs from **867s** to **1,898s**
  - failure `#25300219172` hit manifest failures, conflict retries, and missing-helper degradation.
- **Fix order:** slim comment-only mode first, then reuse PR context and shorten retry ladders.

### 4. Validate / Release
- **Bottleneck type:** serial orchestration and merge/race overhead.
- **Evidence:**
  - `test_and_mark_stable` runs fail after **40–58 minutes**.
  - Distinct issues exist in sync dispatch and smoke-review orchestration.
- **Fix order:** repair dispatch context first, then remove the bait/auto-merge race.

### 5. Offline Analysis
- **Bottleneck type:** expensive analysis loop with repo writes.
- **Evidence:**
  - `workflow_log_analysis` runs take **2,046s–3,024s**.
  - `#25308096512` showed **538,920 observed tokens** in just two analysis sections.
- **Fix order:** narrow default scope, then batch report commits.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-running comment-only/full-review paths
- Broken `test_and_mark_stable` release gate
- Heavy `workflow_log_analysis`
- No-op implement runs still claiming runners

**Top failure modes**
- Smoke bait/removal race and verification failures (`#25300046587`, `#25305535590`, `#25308071039`)
- Dispatch from non-repo context (`#25281876234`)
- Stale implement retry exhaustion (`#25293966619`)
- Review manifest/conflict retry failure (`#25300219172`)
- Nightly self-test classification failure (`#25299383150`)

**Highest-cost drivers**
- Multi-model `review_autofix` reviewer path
- Offline workflow-log analysis Codex passes
- Replayed implement retries with stale prompts
- Redundant GH API polling in release/review flows

**Top 3 prioritized actions**
1. **Repair `test_and_mark_stable` orchestration**  
   Fix `gh workflow run` context and remove the smoke bait/auto-merge race.

2. **Shrink `review_autofix` for comment-only paths**  
   Reduce reviewer panel and skip heavy setup when no edits/validation will occur.

3. **Refresh implement context after first patch miss**  
   Stop paying for repeated no-change retries on stale file snapshots.

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other/skipped | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1000 | 290 | 10 | 42 | 658 | 112.1 | 1.0 | 623.0 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other/skipped | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 58 | 58 | 0 | 0 | 0 | 609.1 | 611.0 | 648.4 |
| `review_autofix` | 79 | 38 | 1 | 38 | 2 | 457.1 | 49.0 | 1713.8 |
| `implement` | 174 | 17 | 4 | 4 | 149 | 24.1 | 1.0 | 180.7 |
| `test_and_mark_stable` | 4 | 0 | 4 | 0 | 0 | 2820.0 | 2697.5 | 3380.0 |
| `workflow_log_analysis` | 4 | 4 | 0 | 0 | 0 | 2429.3 | 2323.5 | 2953.8 |
| `orchestrate_poll` | 45 | 45 | 0 | 0 | 0 | 51.8 | 45.0 | 90.6 |
| `clarify` | 205 | 25 | 0 | 0 | 180 | 12.6 | 1.0 | 95.6 |
| `plan` | 174 | 21 | 0 | 0 | 153 | 16.4 | 1.0 | 136.4 |
| `orchestrate_clarify_respond` | 174 | 4 | 0 | 0 | 170 | 1.2 | 1.0 | 2.0 |
| `copilot_pull_request_reviewer` | 24 | 24 | 0 | 0 | 0 | 166.3 | 163.5 | 304.1 |
| `validation_refresh` | 5 | 5 | 0 | 0 | 0 | 202.2 | 202.0 | 210.2 |
| `memory_maintenance` | 4 | 4 | 0 | 0 | 0 | 34.0 | 33.5 | 39.3 |

### Observed deep-dive token metrics from logs

| Run ID | Workflow family | Evidence step | Observed tokens |
|---|---|---|---:|
| `25308096512` | `workflow_log_analysis` | `deep-audit` | 373,375 |
| `25308096512` | `workflow_log_analysis` | `api-redundancy` | 165,545 |
| `25293966619` | `implement` | visible retries 2–5 only | 42,989 |
| `25300219172` | `review_autofix` | visible editor/conflict retries only | 15,059 |

**Note:** these are lower bounds from visible log lines, not full-run token totals.

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events | 71 |
| `retrieve` ops | 17 |
| Retrieve hit rate (`records_selected > 0`) | 64.7% |
| Avg `estimated_tokens` per retrieve | 18.1 |
| `keyword_method=plain` | 11 |
| `keyword_method=none` | 6 |
| Zero-hit retrieves | 6 |
| `fail_open: true` telemetry entries | 0 |
| `enabled: false` telemetry entries | 0 |
| Writes with `push_attempts > 1` | 2 |

### Prompt cache / cache observability summary

| Signal | Observation |
|---|---|
| Prompt cache configured | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED=false` widely present) |
| Real cache hit/miss metrics on reviewer/editor calls | No |
| Cache probe present | Yes (`cache_enabled=true` in `review_autofix`) |
| Cache token fields populated | No (`prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` were `na`) |
| GitHub Actions cache hit observed | Yes (`codex-v0.114.0` in `workflow_log_analysis`) |

### GH API hotspot summary

| Workflow / step | Observed pattern | Likely issue | Est. reducible calls/run |
|---|---|---|---:|
| `test_and_mark_stable / e2e-smoke-test` | repeated run discovery + status polling | unbatched phase polling | 20–40 |
| `review_autofix / codex-agent` | PR payload + comments + reviews + linked issues + check-runs rebuilt | context re-fetch across adjacent steps | dozens |
| `issue_pr_status` | GraphQL discovery followed by per-issue REST enrichment | missed batching/reuse | 3–5 |
| `orchestrate_poll` / cancel helpers | `/rate_limit` helper on healthy path | avoidable probe calls | 1–2 |
| `copilot_pull_request_reviewer / Cleanup artifacts` | list artifacts then delete in loop | per-item cleanup without reuse | 2–4 |


## Deep Audit — Workflows & Scripts (2026-05-04)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`
  **File path** — `.github/workflows/plan.yml:341-347,430-695`
  **Severity** — High
  **Category tag** — `bug`
  **Description** — The `Fetch issue comments` step writes `gh api --paginate` output straight into `ISSUE_COMMENTS_FILE` without collapsing pages into one JSON array. Every downstream consumer then treats that file as a single array (`jq` at lines 430-435, 561-568, 594-632, and 695). For issues with more than one comments page, the file becomes multiple top-level JSON documents, so these later `jq` reads can fail under `set -euo pipefail` or silently mis-read stale/partial data.
  **Recommended fix** — Merge paginated pages before storing them, using the same pattern already used elsewhere in-repo: `gh api --paginate ... | jq -s 'add // []' > "${ISSUE_COMMENTS_FILE}"`, or switch to `gh_retry_to_file`/JSON-validation helpers from `scripts/gh_helpers.sh`.

- **ID** — `BUG-002`
  **File path** — `.github/workflows/test-and-mark-stable.yml:1136-1143,1258-1349`
  **Severity** — Medium
  **Category tag** — `bug`
  **Description** — `gh_api_safe()` captures all `gh api` stdout with command substitution (`output=$(gh api "$@" ...)`). Bash command substitution strips NUL bytes, but the later log-scanning code explicitly relies on preserving raw job-log bytes and claims the tempfile path is “byte-exact.” For `/actions/jobs/{id}/logs`, that claim is false: the bytes have already been normalized before they are redirected to `LOG_FILE`, so the noop-marker and reviewer-progress shortcuts can miss matches or merge lines incorrectly.
  **Recommended fix** — Split `gh_api_safe` into text and raw-stream variants. For raw log endpoints, write `gh api ...` directly to a tempfile/stdout and inspect only exit status/stderr; keep command substitution only for JSON/text endpoints.

- **ID** — `CONSIST-001`
  **File path** — `.github/workflows/review_autofix.yml:3696-3735,3810-3855; .github/workflows/issue_pr_status.yml:239-249; scripts/label_helpers.sh:146-189`
  **Severity** — Medium
  **Category tag** — `consistency`
  **Description** — When `label_helpers.sh` is unavailable, both workflows fall back to inline `set_issue_phase_label_resilient()` implementations that only `POST` the target label. The canonical helper in `scripts/label_helpers.sh` does a phase-replacement flow (read labels, remove conflicting phase labels, then `PUT` the final set). In the exact missing-support-script scenarios already seen in repo logs, these fallbacks can leave contradictory phase labels like `ai:done` + `ai:review-blocked` or `ai:ready-to-merge` + `ai:closed`, which undermines later phase inference.
  **Recommended fix** — Stop using POST-only fallbacks for phase labels. Stage `scripts/label_helpers.sh` once into `SUPPORT_SCRIPTS_DIR`, or inline one shared copy of the canonical GET/PUT algorithm and have all fallback callers source that single implementation.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`
  **File path** — `.github/workflows/issue_pr_status.yml:288-344,499-509`
  **Severity** — Medium
  **Category tag** — `api-redundancy`
  **Description** — `Update linked issue labels when PR closes` already batches linked-issue metadata into `ORCH_RESP` with labels and body for every linked issue, but `Send PR merged Telegram alert` later re-fetches each linked issue body one-by-one with `_safe_gh_jq` just to test for the orchestrator marker. Current call count is **1 batched GraphQL call + up to N REST issue reads**; proposed call count is **1 total** by persisting `body`/managed-state from the first batch into an env/file cache and reusing it.
  **Recommended fix** — Export a `LINKED_ISSUE_META_JSON` cache from the first step and consume it in the alert step. Follow the cycle-local cache pattern already used in `scripts/orchestrate_poll_process.sh` (`ACTIVE_WORKFLOW_ISSUES`, `_candidate_details_json`) instead of re-fetching per issue.

- **ID** — `API-002`
  **File path** — `scripts/orchestrate_poll_process.sh:1927-1948,6320-6334`
  **Severity** — Medium
  **Category tag** — `api-redundancy`
  **Description** — The poller fetches each tracking issue’s comments once while extracting orchestrator state (`issues/{tracking}/comments` at 1927-1948), then fetches the same comments again later to rebuild `orchestrator_managed_set` (6320-6334). For `T` active tracking issues, current cost is **2T comment-list calls per poll cycle**; proposed cost is **T** by caching the parsed tracking state / managed issue numbers from the first pass.
  **Recommended fix** — Persist extracted tracking-state JSON or a derived managed-issue cache in `${RUNTIME_DIR}` during the first pass, then reuse it later. Extend the same cycle-local cache contract used elsewhere in this script rather than re-reading issue comments.

- **ID** — `BATCH-001`
  **File path** — `scripts/orchestrate_poll_process.sh:6346-6353`
  **Severity** — Medium
  **Category tag** — `api-batching`
  **Description** — The standalone stall sweep enumerates seven pipeline labels with seven separate `gh issue list` calls, then unions the results before the batched candidate-details fetch. Current call count is **7 label-list calls + 1 candidate-details batch call**; proposed call count is **1 aliased GraphQL/search batch call + 1 candidate-details batch call**.
  **Recommended fix** — Add a batched helper alongside `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` that takes the fixed label set and returns the union in one request. This is exactly the “prefer batched GraphQL over per-item REST/per-label loops” contract from `CLAUDE.md §15`.

- **ID** — `BATCH-002`
  **File path** — `.github/workflows/review_autofix.yml:478-530`
  **Severity** — Low
  **Category tag** — `api-batching`
  **Description** — In `post-merge-validate-dispatch`, the regex fallback path synthesizes `issue_numbers`, then loops and calls `gh issue view` per issue when labels were not present in the initial GraphQL response. Current call count on that path is **1 PR fetch + N issue-label reads**; proposed call count is **1 PR fetch + 1 batched issue-label lookup**.
  **Recommended fix** — When the fallback regex path is used, batch-fetch labels for the synthesized issue numbers with a GraphQL alias helper before entering the loop. Reuse the same batching shape used by `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/orchestrate_poll.yml:63-97; .github/workflows/review_autofix.yml:1254-1292`
  **Severity** — Low
  **Category tag** — `duplication`
  **Description** — The repo carries multiple bespoke `_rl_wait`/`gh_retry` implementations even though `scripts/gh_helpers.sh` already provides `gh_retry`, `gh_retry_to_file`, `_safe_gh_jq`, permanent-failure detection, rate-limit breaker support, and Telegram alerts. This duplication has already drifted: inline copies always probe `/rate_limit`, lack `_is_gh_permanent_failure`, and diverge in backoff behavior.
  **Recommended fix** — Consolidate retry logic into `scripts/gh_helpers.sh` (or a bootstrap-safe shim sourced before checkout). Target signatures should remain `gh_retry <cmd...>`, `gh_retry_to_file <outfile> <cmd...>`, `_safe_gh_jq <endpoint> --jq ...`. Update `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, `review_autofix.yml`, `mark-stable.yml`, and similar callers to source the shared helper instead of redefining it.

- **ID** — `DUP-002`
  **File path** — `.github/workflows/review_autofix.yml:3701-3735,3824-3855,4571-4585; .github/workflows/issue_pr_status.yml:239-249`
  **Severity** — Low
  **Category tag** — `duplication`
  **Description** — `ensure_label_exists` / `set_issue_phase_label_resilient` are reimplemented inline at least four times outside `scripts/label_helpers.sh`. These copies duplicate label descriptions/colors and phase-mutation behavior, and they have already drifted from the canonical helper’s phase-replacement semantics.
  **Recommended fix** — Make `scripts/label_helpers.sh` the sole owner of `ensure_label_exists <label_name> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Stage that helper once per job into `SUPPORT_SCRIPTS_DIR`, then have `review_autofix.yml` and `issue_pr_status.yml` source it rather than carrying inline variants.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`
  **Severity** — Medium
  **Category tag** — `expression-limit`
  **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block contains `${{ }}` interpolations and is already about **16,626 characters**, leaving only about **4,374 characters** of headroom before GitHub’s **21,000-character** expression hard limit. This block already embeds polling, rate-limit handling, live-log shortcuts, and timeout diagnostics in one interpolated body, so routine edits could push it over the threshold that this repo has already hit elsewhere.
  **Recommended fix** — Extract the entire wait loop to an external script under `scripts/` (preferred), or split log-shortcut logic into a second step fed by env vars. This is the same mitigation pattern already used elsewhere in the repo when large expressions were externalized.
  
Additional assessment:
- No workflow file exceeded the **800 KB** early-warning threshold. Largest observed files were `review_autofix.yml` (**269,146 bytes**) and `test-and-mark-stable.yml` (**227,918 bytes**).

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`
  **File path** — `scripts/memory_helpers.sh:56-57`
  **Severity** — Low
  **Category tag** — `dead-code`
  **Description** — `memory_ensure_branch()` assigns `local token="${GH_TOKEN:-}"` and never uses it. That dead assignment is misleading in an auth-sensitive helper because it suggests token-based remote rewriting exists here when the function actually relies on the existing `origin` remote configuration.
  **Recommended fix** — Remove the unused variable, or wire it into authenticated remote construction if that was the intended design.

Cross-cutting notes:
- I did **not** find additional `TODO` / `FIXME` / `HACK` markers in `.github/workflows/*.yml` or `scripts/*.sh`.
- The most actionable shellcheck-style gap in scope is the unused assignment above; the larger maintainability risk is helper drift, not lint volume.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 6 | BUG-002, CONSIST-001, API-001, API-002, BATCH-001, EXPR-001 |
| Low | 4 | BATCH-002, DUP-001, DUP-002, DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 5 | Medium |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 3 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-04)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is statically proven and can be implemented directly without changing retries, filters, concurrency, or cache contracts. `NEEDS_VERIFICATION` means the overlap looks real, but a human must confirm freshness/error-handling assumptions before changing it. `RISKY_SKIP` means the redundancy is visible but sits in a retry/race-sensitive/paginated/orchestrator-defense path, so it must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

- **ID** — `MERGE-001`
  - **Safety tag** — `RISKY_SKIP`
  - **File path and line ranges** — `scripts/orchestrate_poll_process.sh:3407-3412`, `scripts/orchestrate_poll_process.sh:3463-3468`, `scripts/orchestrate_poll_process.sh:3517-3519`
  - **Current call count** — `8` calls to the same PR endpoint in one final-merge pass
  - **Proposed call count** — `3` calls (one full PR snapshot per checkpoint)
  - **Endpoint(s)** — `GET /repos/{repo}/pulls/{pull_number}`
  - **Evidence**
    ```bash
    existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    ```
    ```bash
    pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
    pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    ```
    ```bash
    pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
    pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
    pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
    ```
  - **Proposed fix** — Add a local helper in `finalize_tracking_issue_if_done()` that fetches the full PR JSON once per checkpoint and derives `.state`, `.mergeable`, and `.merged_at` locally; keep the three checkpoints (preexisting PR check, pre-merge gate, post-merge verification) separate.
  - **Safety rationale** — This is inside `orchestrate_poll_process.sh`’s final-merge race-handling path, which the repo’s rules explicitly classify as `RISKY_SKIP`.
  - **Downstream signal** — Do not auto-implement; manually validate on a live final-merge path that `mergeable=null/false` handling, `final_merge_status` transitions, and existing log lines remain byte-for-byte equivalent.

### Redundant Re-Fetch (REUSE-###)

- **ID** — `REUSE-001`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/review_autofix.yml:1366-1371`, `.github/workflows/review_autofix.yml:1394-1424`, `scripts/review_rb_judge.sh:146-151`, `scripts/review_rb_judge.sh:161-168`
  - **Current call count** — `1` GraphQL linked-issue lookup in the judge + `up to N` REST issue-body reads
  - **Proposed call count** — `0` extra judge-side linked-issue calls on cache hit; retain current path only as cache-miss fallback
  - **Endpoint(s)** — GraphQL `pullRequest(number){ closingIssuesReferences(first:50){ nodes{ number title body } } }`; `GET /repos/{repo}/issues/{issue_number}`
  - **Evidence**
    ```bash
    if gh_retry "${_linked_tmp}" api graphql \
      ... \
      -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
      --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
    ```
    ```bash
    lines.append(f"Issue #{num}: {title}")
    if body:
        lines.append(body)
    ```
    ```bash
    ISSUE_NUMBERS="$(gh_retry gh api graphql \
      ... \
      --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"
    ...
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if [ -z "${FIRST_ISSUE_BODY}" ]; then
      FIRST_ISSUE_BODY="${BODY}"
    fi
    ```
  - **Proposed fix** — Extend the early metadata step to persist a machine-readable linked-issue cache with `{number,title,body}` (either enrich `LINKED_ISSUES_JSON` or add a sibling JSON file next to `LINKED_ISSUE_CONTEXT_FILE`), then update `scripts/review_rb_judge.sh` to read `ISSUE_NUMBERS` and `FIRST_ISSUE_BODY` from that cache before falling back to GraphQL/REST.
  - **Safety rationale** — The overlap is strong, but safety depends on confirming every `review_rb_judge.sh` entry path inherits that cache and on preserving the current first-issue/body selection semantics.
  - **Downstream signal** — Verify that all `review_rb_judge.sh` invocations run after the metadata step has populated the linked-issue cache, and explicitly decide whether an empty first linked-issue body should stay empty or continue falling through before removing the REST loop.

- **ID** — `REUSE-002`
  - **Safety tag** — `NEEDS_VERIFICATION`
  - **File path and line ranges** — `.github/workflows/implement.yml:56-76`, `.github/workflows/implement.yml:528-555`
  - **Current call count** — `2`
  - **Proposed call count** — `1` steady-state call, with the current later fetch retained only as a cache-miss fallback
  - **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`
  - **Evidence**
    ```bash
    ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
    ```
    ```bash
    gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
    ```
    The workflow already documents the intended reuse pattern later:
    ```bash
    # Reuse the issue snapshot fetched in "Fetch issue metadata"
    # rather than re-hitting the API.
    ```
  - **Proposed fix** — Preserve the precheck response (for example via `$GITHUB_ENV` or a temporary JSON blob) and have `Fetch issue metadata` populate `ISSUE_META_FILE` from that cached payload first, while keeping the existing `gh_retry gh api ...` path as a parse-failure/missing-cache fallback.
  - **Safety rationale** — The endpoint is the same and the job is the same, but the second call currently has stronger retry behavior and happens after multiple setup steps, so freshness and failure semantics need confirmation.
  - **Downstream signal** — Verify that no step between `Precheck approval phase label` and `Fetch issue metadata` relies on a fresher issue body/title/labels, and retain the current `gh_retry` fallback if the cached JSON is missing or invalid.

- **ID** — `REUSE-003`
  - **Safety tag** — `RISKY_SKIP`
  - **File path and line ranges** — `.github/workflows/implement.yml:3047-3061`, `scripts/implement_diagnose_post_codex_failure.sh:124-150`
  - **Current call count** — `2` logical snapshots of the same jobs endpoint, each with up to `3` retry attempts
  - **Proposed call count** — `1` logical snapshot, still retaining a single retry loop
  - **Endpoint(s)** — `GET /repos/{repo}/actions/runs/{run_id}/jobs?per_page=100`
  - **Evidence**
    ```bash
    for _attempt in 1 2 3; do
      RUN_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}/actions/runs/${{ github.run_id }}/jobs?per_page=100" || true)"
      ...
      FAILED_STEP_NAME="$(printf '%s' "${RUN_JOBS_JSON}" | jq -r '[.jobs[].steps[] | select(.conclusion == "failure")] | first | .name // ""' 2>/dev/null || true)"
    done
    ```
    ```bash
    for _attempt in 1 2 3; do
      FAILED_STEP_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/jobs?per_page=100" || true)"
      ...
      FAILED_STEP_NAME="$(printf '%s' "${FAILED_STEP_JOBS_JSON}" | jq -r '
        [.jobs[].steps[] | select(.conclusion == "failure" ... )] | first | .name // ""'
      )"
    done
    ```
  - **Proposed fix** — Persist `FAILED_STEP_NAME` (and optionally the already-fetched jobs JSON) from `capture_post_codex_validation_errors` into `${RUNTIME_DIR}` or env, and update `scripts/implement_diagnose_post_codex_failure.sh` to consume that cache before entering its own jobs-API retry loop.
  - **Safety rationale** — Both sites intentionally wrap the Actions jobs API in retry logic to survive post-failure eventual-consistency races, which forces `RISKY_SKIP`.
  - **Downstream signal** — Do not auto-implement; manually compare a fresh failing implement run before/after the change to confirm `failed_step`, retry timing, and captured diagnostics are unchanged.

### Dead Calls (DEAD-API-###)

- **ID** — `DEAD-API-001`
  - **Safety tag** — `RISKY_SKIP`
  - **File path and line ranges** — `scripts/orchestrate_poll_process.sh:4765-4771`
  - **Current call count** — `1` unused paginated helper call site
  - **Proposed call count** — `0`
  - **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_num}/comments?sort=created&direction=desc&per_page=100` via `--paginate`
  - **Evidence**
    ```bash
    read_standalone_state_json() {
      local issue_num="$1"
      local comments_json
      if ! comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=100" | jq -s 'add // []' 2>/dev/null)"; then
        comments_json='[]'
      fi
      _extract_standalone_state_json_from_comments "${comments_json}"
    }
    ```
    Repository-local symbol search found no workflow/script caller outside the definition itself.
  - **Proposed fix** — Manually verify that no external sourcing/test harness depends on `read_standalone_state_json()`. If none does, remove the helper; if a wrapper is still desired, convert one real caller to use it and document the fetch/pagination contract.
  - **Safety rationale** — Even though the helper appears dead in-repo, it lives in `orchestrate_poll_process.sh` and performs a paginated API read, both of which force `RISKY_SKIP`.
  - **Downstream signal** — Do not auto-implement; manually verify no out-of-file sourcing contract references `read_standalone_state_json()` before deleting it.

### Cross-References to Deep Audit Section

- `API-001`: `NEEDS_VERIFICATION` — The earlier GraphQL batch already carries linked-issue body data, but reusing it across steps must preserve the merged-alert fail-open behavior.
- `API-002`: `RISKY_SKIP` — The duplicate tracking-comment fetches are real, but they sit inside `scripts/orchestrate_poll_process.sh` poll-cycle logic.
- `BATCH-001`: `RISKY_SKIP` — The seven-label sweep is batchable in principle, but it is part of standalone stall recovery inside `scripts/orchestrate_poll_process.sh`.
- `BATCH-002`: `NEEDS_VERIFICATION` — The regex fallback’s per-issue label reads are batchable, but a human should confirm false-positive handling and fallback semantics before changing them.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 3 | MERGE-001, REUSE-003, DEAD-API-001 |

### Implement-Stage Handoff

- No SAFE_TO_MERGE findings in this pass.
