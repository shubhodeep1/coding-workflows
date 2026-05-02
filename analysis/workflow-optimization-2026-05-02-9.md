## Executive Summary

- **The biggest end-to-end drag is `review_autofix`, especially on comment-only Claude-branch reviews.** Deep-dive runs `25253939361` (`1066s`), `25253051580` (`1284s`), and `25253094074` (`2062s`) all spent 17–34 minutes in review flows, even when logs show the path was effectively “reviewer panel + comment-only” or had long setup/checkout spans. **Estimated impact:** save 12–20 minutes on affected runs. **Confidence:** high.

- **`test_and_mark_stable` is currently the highest-severity reliability issue.** All 3 completed runs in the window failed at `e2e-smoke-test / Phase 4b: Verify editor removed bait line` (`25247210528`, `25249170035`, `25252918179`), and the canary still contained `E2E_EDITOR_BAIT_*`. A related note in run `25254373266` also says review editor output stayed empty across attempts on PR `#1992`. **Estimated impact:** remove a 50% workflow-family failure rate and avoid 74–108 minute failed release tests. **Confidence:** high.

- **`workflow_log_analysis` is the largest clear token-cost hotspot.** `summarize_unselected_runs` used `116,787` to `237,335` tokens per sampled pass (avg `164,825` across 8 telemetry entries), and run `25252928519` logged `304,969` tokens in its API redundancy pass. **Estimated impact:** 100k–200k+ token savings per analysis run with low-risk scope reduction/delta reuse. **Confidence:** high.

- **Implement failures are wasting tokens despite strong Serena adoption.** Failed implement runs `25245077011`, `25245085089`, and `25246727158` consumed about `37,646`, `29,529`, and `18,600` tokens respectively before bailing with “2 consecutive attempts with no actionable output,” even though one run reports `254` Serena calls and `98%` Serena efficiency. **Estimated impact:** cut failed-attempt token burn by ~30k per bad run and improve success rate. **Confidence:** high.

- **Git fetch scope is broader than needed in several fast-control workflows.** `orchestrate_poll` (`25254213178`), `promote_main_to_stable` (`25254375144`), and `forward_merge_stable_to_main` (`25254380023`) all use `fetch-depth: 0` or full `refs/heads/* + refs/tags/*` fetches, pulling many branch/tag refs for jobs that mostly need main/stable state. **Estimated impact:** 5–35 seconds per run, especially on no-work poll cycles. **Confidence:** high.

- **Prompt cache is enabled but not measurable enough to optimize confidently yet.** Logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but sampled cache probe lines report `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`. **Estimated impact:** instrumentation first, then moderate latency/token savings on retries and repeated review prompts. **Confidence:** medium.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Shrink the `review_autofix` comment-only path
- **Evidence:**  
  - Run `25253939361` took `1066s`; log summary says `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`  
  - Run `25253051580` took `1284s`; `review.codex-agent` dominated from `13:32:06` to `13:53:16`.  
  - Run `25253094074` took `2062s`; log summary says checkout/setup in `review codex-agent` spanned roughly `13:53:22` to `14:08:35`.
- **Root cause:** Expensive reviewer-panel execution and heavy setup are still happening on paths that are intentionally non-mutating.
- **Exact change:**  
  - Add a lighter review mode for `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` / comment-only paths:
    - use 1–2 reviewer models instead of the full pool,
    - lower reasoning from `xhigh` to `medium` for comment-only review,
    - skip editor bootstrap and any judge-side prep that is irrelevant when no patch/merge is possible,
    - short-circuit artifact/setup steps not used by comment-only output.
- **Estimated time savings:** `12–20 minutes` on affected runs.
- **Implementation risk:** **Medium.** Safe if scoped only to comment-only/non-merge paths; avoid changing full autofix/merge behavior.

### 2. Narrow `orchestrate_poll` checkout/fetch scope on no-work cycles
- **Evidence:**  
  - Run `25254213178` (`48s`) and `25253748231` (`43s`) were dominated by `poll/Checkout repository`.  
  - Deep log shows `fetch-depth: 0` plus `git fetch ... +refs/heads/*:refs/remotes/origin/* +refs/tags/*:refs/tags*`.
- **Root cause:** Poller is paying for full-graph fetches even when it only needs current workflow support files and active issue state.
- **Exact change:**  
  - Switch the repo checkout in `orchestrate_poll` to shallow/targeted fetch by default.
  - Fetch only the specific refs actually needed for the poll decision path.
  - Keep a fallback full fetch only when a downstream script explicitly requires history/tags.
- **Estimated time savings:** `20–35s` per no-work poll run.
- **Implementation risk:** **Low-Medium.** Verify no hidden dependency on tag/history traversal in `orchestrate_poll_process.sh`.

### 3. Stop full branch/tag enumeration in `promote` and `forward-merge`
- **Evidence:**  
  - `25254375144` and `25254380023` both used `fetch-depth: 0` and full ref fetches.  
  - Logs show many unrelated `claude/*` branches and numerous tags being fetched.
- **Root cause:** Control-plane jobs are using repository-wide fetch patterns for branch comparisons and version resolution.
- **Exact change:**  
  - For `forward_merge_stable_to_main`, fetch only `stable` and `main`.
  - For `promote_main_to_stable`, fetch `main`, `stable`, and tags only when version resolution needs them.
  - Avoid `+refs/heads/*` in these jobs.
- **Estimated time savings:** `5–10s` per run.
- **Implementation risk:** **Low.**

### 4. Reduce `workflow_log_analysis` scope for repeated runs
- **Evidence:**  
  - Workflow family average duration is `4617s`, p50 `4428s`, with sampled runs at `4028–6075s`.  
  - `summarize_unselected_runs` spans about `2–3 minutes` inside sampled runs.
- **Root cause:** The analyzer is repeatedly summarizing large sets of runs and re-running expensive sections even when many runs are unchanged.
- **Exact change:**  
  - Reuse prior per-run summaries for unchanged run IDs.
  - Skip summarization for already-summarized unselected runs unless the source logs changed.
  - Prioritize failing/slow/new runs first; cap routine success-run expansion.
- **Estimated time savings:** `2–10 minutes` per analysis run, depending on repeat overlap.
- **Implementation risk:** **Low.**

### 5. Trim duplicate setup/cleanup work in failed `implement` runs
- **Evidence:**  
  - Failed implement runs show repeated Serena prompt blocks, repeated MCP startup, and repeated git post-job cleanup.  
  - `25246727158` still emitted `254` Serena calls before failure.
- **Root cause:** Retry attempts repeat nearly the full setup stack before the agent makes no edit.
- **Exact change:**  
  - Reuse initialized runtime/session state across retry attempts within a run.
  - Stop re-injecting the full Serena instruction block on every retry.
  - Early-abort after first no-change attempt for fully specified one-file smoke tasks.
- **Estimated time savings:** `30–90s` per failed implement run.
- **Implementation risk:** **Medium.** Needs care to preserve retry correctness.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Cap and de-duplicate `workflow_log_analysis` summarization
- **Evidence:**  
  - `summarize_unselected_runs` telemetry used:
    - `154,202` tokens,
    - `116,787`,
    - `153,540`,
    - `237,335`,
    - `190,564`,
    - `156,314` (and repeats in other sampled runs).  
  - Average across 8 sampled telemetry entries: about `164,825` tokens/run.
  - Run `25252928519` also logged `304,969` tokens in the API-redundancy pass.
- **Root cause:** High-volume re-summarization of unselected runs and large secondary analysis passes.
- **Exact change:**  
  - Persist run-summary artifacts by run ID and reuse them.
  - Only summarize newly observed runs or runs whose deep-dive status changed.
  - Lower the default unselected-run target below `100` unless failure coverage is thin.
- **Estimated savings:** `100k–200k+ tokens` per analysis run.
- **Quality-risk notes:** Low if failures/slow/outliers stay prioritized.

### 2. Downshift reviewer-model fanout on comment-only review paths
- **Evidence:**  
  - Comment-only review path in `25253939361` still took `1066s`.  
  - Similar review jobs expose large reviewer pools: `REVIEWER_MODELS` includes six providers in recent runs like `25254373300` and `25253039282`.
- **Root cause:** The same multi-model panel appears to be used even when the workflow cannot auto-edit or auto-merge.
- **Exact change:**  
  - For comment-only paths, use a reduced reviewer set or a single fast primary reviewer plus optional fallback.
  - Lower reasoning from `xhigh` to `medium` on non-patch flows.
- **Estimated savings:** Likely the largest review-token reduction; exact token totals were not emitted, but duration suggests material savings.
- **Quality-risk notes:** Medium. Keep the full panel for merge-affecting paths; only trim non-mutating review modes.

### 3. Fix retry-prompt inflation in failed `implement` runs
- **Evidence:**  
  - `25245077011`: `24,112 + 13,534 = 37,646` tokens before bail.  
  - `25245085089`: `5,380 + 24,149 = 29,529`.  
  - `25246727158`: `4,466 + 14,134 = 18,600`.  
  - Total across 3 failed runs: about `85,775` tokens with no successful edit.
- **Root cause:** Retries repeat large static instructions and setup despite highly deterministic tasks.
- **Exact change:**  
  - Add a “minimal retry prompt” mode that reuses stable context and appends only delta diagnostics.
  - For one-file, fully specified tasks, stop after first announced-edit-without-change and route to diagnosis.
- **Estimated savings:** `10k–30k tokens` per failed implement run.
- **Quality-risk notes:** Low-Medium. Keep full retries for ambiguous multi-file tasks only.

### 4. Make prompt-cache prefixes stable across retries
- **Evidence:**  
  - Cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`), but observed cache probe lines show `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`.  
  - Implement failure logs show repeated large Serena/system instruction blocks across attempts.
- **Root cause:** Cache instrumentation is incomplete, and retry prompts likely vary early in the prefix. This is an inference from the repeated attempt scaffolding.
- **Exact change:**  
  - Freeze a static prompt prefix for implement/review retries.
  - Move attempt counters, failure diagnostics, and ephemeral metadata to the end of the prompt.
  - Emit real cache read/create token counters on every cache-enabled call.
- **Estimated savings:** Not measurable from current logs; likely moderate on retry-heavy flows.
- **Quality-risk notes:** Low if prompt semantics stay identical.

### 5. Avoid unnecessary full-history fetches in control workflows
- **Evidence:** Full fetches in `orchestrate_poll`, `promote`, and `forward_merge`.
- **Root cause:** Over-fetching burns network/runtime; indirectly raises runner cost.
- **Exact change:** Shallow targeted fetch by default.
- **Estimated savings:** Small dollar impact, moderate cumulative savings due to high run frequency.
- **Quality-risk notes:** Low.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Fix `review_autofix` empty-output behavior on smoke-test editor paths
- **Failure evidence:**  
  - All 3 `test_and_mark_stable` failures (`25247210528`, `25249170035`, `25252918179`) stopped at `e2e-smoke-test / Phase 4b: Verify editor removed bait line`.  
  - In `25252918179`, the canary still contained `# E2E_EDITOR_BAIT_25252918179`.  
  - `25254373266` includes a note referencing analysis of run `25253051580`, stating the `review_autofix` editor at `reasoning=low` produced empty output across 4 attempts on PR `#1992`.
- **Root cause category:** AI execution failure / agent non-action on deterministic edit.
- **Exact fix:**  
  - Add a smoke-test-specific review profile:
    - force a patch-capable editor path,
    - disable comment-only downgrade for smoke PRs carrying the editor-bait marker,
    - raise reasoning only for the smoke editor substep if low reasoning is implicated,
    - fail fast when the editor returns announced-edit-without-change on bait-removal tasks.
- **Expected reliability impact:** High; should address the `50%` failure rate in `test_and_mark_stable` and turn long failures into deterministic pass/fail earlier.
- **Rollback/fail-open considerations:** Keep existing generic review path as fallback; scope changes to bait-marked smoke PRs first.

### 2. Add retry/backoff to `actionlint` install
- **Failure evidence:**  
  - CI run `25249161547` failed in `lint / Install actionlint` because `curl` returned `502`.
- **Root cause category:** External fetch flake.
- **Exact fix:**  
  - Wrap the tarball download in retry/backoff with checksum verification preserved.
  - Optionally cache the verified binary/tarball per version.
- **Expected reliability impact:** Removes a class of transient CI failures with near-zero behavior change.
- **Rollback/fail-open considerations:** Fail closed if checksum mismatches; fail open is not appropriate here.

### 3. Reduce support-script ref drift
- **Failure evidence:**  
  - `issue_pr_status` runs `25254373266` and `25253471403` warn: `Support checkout ref ... is unavailable; using main.`
- **Root cause category:** Workflow-to-support-script version mismatch.
- **Exact fix:**  
  - Validate support ref existence before dispatching dependent workflows, or materialize support files from the same commit as the workflow whenever possible.
- **Expected reliability impact:** Medium; lowers silent drift between workflow revision and helper scripts.
- **Rollback/fail-open considerations:** Preserve current `main` fallback, but surface an explicit metric and warning count.

### 4. Upgrade deprecated Node-20-based actions
- **Failure evidence:**  
  - `workflow_log_analysis` emitted `Node.js 20 is deprecated ... actions/download-artifact@v4 forced to run on Node.js 24`.  
  - `copilot_pull_request_reviewer` logs also showed `Buffer()` deprecation warnings.
- **Root cause category:** Toolchain deprecation risk.
- **Exact fix:**  
  - Upgrade deprecated action versions and remove Node-20-bound revisions.
- **Expected reliability impact:** Medium long-term; avoids future hard failures when compatibility shims are removed.
- **Rollback/fail-open considerations:** Low-risk version bumps with pinning.

### 5. Preserve memory-helper availability for run-end bookkeeping
- **Failure evidence:**  
  - Cancelled `review_autofix` runs `25253930796`, `25253454529`, and `25253449112` warn: `memory helper script missing; skipping run-end failure event`.
- **Root cause category:** Observability/bookkeeping gap.
- **Exact fix:**  
  - Stage memory helper scripts before any branch-specific cleanup path and make the run-end event emitter independent of optional review artifacts.
- **Expected reliability impact:** Low-Medium direct workflow impact, but important for postmortem accuracy and idempotency.
- **Rollback/fail-open considerations:** Keep fail-open behavior if helpers are truly unavailable.

## AI Memory Health

- **Telemetry coverage:** Observed in sampled deep-dive logs; `96` valid `AI_MEMORY_TELEMETRY` JSON entries.
- **Operation mix:**  
  - `record-run-event`: `40`  
  - `retrieve`: `15`  
  - `processed-command-check`: `14`  
  - `processed-command-claim`: `13`  
  - `summarize_unselected_runs`: `8`  
  - `compact`: `4`  
  - `record-candidate`: `2`

### Retrieve effectiveness
- **Hit rate:** `13 / 15 = 86.7%`
- **Average `estimated_tokens`:** `42.9`
- **`keyword_method` distribution:**  
  - `plain`: `13`  
  - `none`: `2`  
  - `llm`: `0`
- **Roles:**  
  - `implementation`: `13` retrieves  
  - `reviewer`: `2` retrieves

### Flags
- **Zero-record retrieves:** `2`, both reviewer retrieves in workflow-log-analysis sampled runs (`25246650500`, `25246056978`), each with `keyword_method: none` and `estimated_tokens: 0`.
- **`fail_open: true`:** none observed.
- **`enabled: false`:** none observed.
- **High push retry counts:** 2 entries with `push_attempts: 2`, both on `record-run-event` failures, including implement run `25246727158`.

### Assessment
- Memory is **working well for implement flows**: high retrieve hit rate, low token footprint, no disabled/fail-open noise.
- Memory is **not helping reviewer/workflow-analysis retrieval** in the sampled runs: reviewer fetches returned zero records and never used `llm` keywording.

### Recommendation
- Add a reviewer-specific retrieval strategy before heavy review/log-analysis passes:
  - try `plain` first,
  - escalate to `llm` keyword extraction on zero-hit review paths,
  - emit the token budget alongside retrieve telemetry so budget use can be judged directly.

## GH API Call Audit

### Highest-volume / highest-redundancy patterns

1. **Implement re-fetches the same issue repeatedly**
   - **Evidence:** Failed implement logs (`25245077011`, `25245085089`, `25246727158`) show:
     - initial `GET /issues/{ISSUE_NUMBER}` for state/labels,
     - another `GET /issues/{ISSUE_NUMBER}` to write `ISSUE_META_FILE`,
     - later guarded label re-fetches,
     - paginated comment fetches.
   - **Root cause:** Early and later steps independently fetch overlapping issue metadata.
   - **Exact change:** Cache the first full issue payload locally and reuse it for labels/state/body consumers within the run.
   - **Estimated call-count reduction:** 1 guaranteed issue GET removed per non-skipped implement run; more on fallback paths.
   - **Rate-limit risk reduction:** Moderate across high-frequency issue workflows.

2. **Issue/PR status sync still falls back from GraphQL batching to per-issue REST**
   - **Evidence:** In `25254373266`, `issue_pr_status` uses batched `closingIssuesReferences(first: 50)` and a batched orchestrator query, but still has per-issue fallback body/label reads.
   - **Root cause:** Partial batching; downstream steps re-derive facts already available earlier.
   - **Exact change:** Export orchestrator-managed classification from the first GraphQL pass and reuse it in merged-alert/finalization steps.
   - **Estimated call-count reduction:** 1–N issue GETs per merged PR, depending on linked issue count.
   - **Rate-limit risk reduction:** Medium.

3. **`cancel_on_pr_close` scans `/actions/runs` and checks `/rate_limit` even on no-op runs**
   - **Evidence:** `25254373292` and `25253471399` both call `_rl_wait()` with `/rate_limit` support and query `/actions/runs`, even when no matching runs exist.
   - **Root cause:** Defensive retry wrapper is always armed on a mostly empty-path workflow.
   - **Exact change:**  
     - Keep fail-open retry logic, but defer `/rate_limit` probing until an actual rate-limit response occurs.  
     - Avoid duplicate run-list scans if the first filtered query is empty.
   - **Estimated call-count reduction:** Small per run, meaningful due to frequent lifecycle triggers.
   - **Rate-limit risk reduction:** Low-Medium.

4. **`review_autofix` post-merge validate dispatch re-fetches PR metadata after GraphQL linked-issue lookup**
   - **Evidence:** `25254373300` step `Dispatch standalone validate...` runs GraphQL for `closingIssuesReferences`, then falls back to `GET /pulls/{PR_NUMBER}` for title/body text.
   - **Root cause:** Linked-issue resolution and PR metadata resolution are split.
   - **Exact change:** Reuse preloaded PR metadata artifact or include fallback-needed fields earlier in the run.
   - **Estimated call-count reduction:** 1 PR GET on fallback path.
   - **Rate-limit risk reduction:** Low.

### Cross-reference to repo API hygiene rules
The repo already documents:
- **mandatory batching,**
- **cycle-local caches,**
- **fail-open behavior.**

Observed behavior partially follows that standard, but sampled logs show the remaining gaps are mostly **reuse gaps**, not absence of policy. The next wins are to:
- export earlier batched results to later steps,
- avoid re-reading issue/PR bodies inside the same job,
- preserve fail-open semantics when removing redundant reads.

### Deep-audit corroboration
`workflow_log_analysis` run `25252928519` produced:
- `0` `SAFE_TO_MERGE` findings,
- `5` `NEEDS_VERIFICATION` findings (`MERGE-001`, `MERGE-002`, `REUSE-001`, `REUSE-002`, `REUSE-003`),
- `1` `RISKY_SKIP` (`MERGE-003`).

That matches the sampled telemetry: there are clear reuse opportunities, but most need careful rollout because they sit near fallback logic or pagination semantics.

## MCP & Serena Efficiency

### What is working
- Failed implement run `25246727158` shows **strong Serena adoption**:
  - `254` Serena tool calls,
  - reported **`98%` Serena efficiency**,
  - top tools: `replace_symbol_body (40)`, `insert_after_symbol (40)`, `get_symbols_overview (33)`, `find_symbol (33)`, `find_referencing_symbols (28)`,
  - estimated tokens `~19,050` with Serena vs `~162,100` without.
- The agent correctly uses `serena.activate_project(...)` directly and does **not** show onboarding calls in sampled logs.

### Inefficiencies observed
1. **High tool churn without a successful edit**
   - **Evidence:** `25246727158` still ended with “2 consecutive attempts with no actionable output” despite 254 Serena calls.
   - **Issue:** Navigation was efficient, but execution got stuck in explore/edit-announcement loops.

2. **Repeated large Serena instruction blocks per retry**
   - **Evidence:** Failed implement logs repeat the full “Serena (MCP) Semantic Tooling” and “SERENA MCP EFFICIENCY (MANDATORY)” blocks before multiple attempts.
   - **Issue:** Good rules are being re-sent too often.

3. **Tool-usage stats file missing even when Serena was clearly used**
   - **Evidence:** `25246727158` says `No Serena tool usage stats found`, then later generates a Serena efficiency report showing 254 calls.
   - **Issue:** Reporting path is inconsistent, which makes automated efficiency monitoring noisy.

### Concrete changes
- **Early no-change circuit breaker:**  
  When a fully specified task shows “announced edit/apply_patch but produced no file changes” on attempt 1, switch from full retry to targeted diagnosis instead of re-running the same large tool-guidance stack.
- **Retry prompt compaction:**  
  Send the Serena policy block once, then retry with delta-only instructions.
- **Stabilize Serena stats emission:**  
  Ensure `.serena/tool_usage_stats.json` is written before cleanup/reporting so run-end monitoring reflects actual usage.
- **Increase safe parallelism in read phase:**  
  For complex review paths, batch initial symbol lookups (`get_symbols_overview`, `find_symbol`, `find_referencing_symbols`) before the first edit decision rather than interleaving many tiny reads.

### Net assessment
- **Serena itself is not the bottleneck.**  
  In sampled failures, Serena use was high-quality; the waste came from **agent retry behavior** and **prompt churn**, not from broad raw-file reads.

## Prompt Cache & Memory System

### Prompt cache
- **Observed state:** Enabled in sampled runs (`OPENROUTER_PROMPT_CACHE_DISABLED: false`).
- **Observed problem:** Cache effectiveness is not measurable because sampled cache probe lines report:
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`
- **Likely fragmentation causes (inference):**
  - large repeated retry preambles,
  - dynamic attempt counters and failure diagnostics inserted early,
  - repeated Serena policy blocks.

### Concrete cache improvements
1. **Emit non-`na` cache metrics for every cache-enabled call**
   - **Impact:** Enables actual hit-rate tracking; prerequisite for tuning.
   - **Risk:** Low.

2. **Freeze prompt prefixes across retries**
   - **Impact:** Moderate token and latency savings on implement/review retries.
   - **Risk:** Low.

3. **Separate static tooling policy from run-specific diagnostics**
   - **Impact:** Better cache locality and smaller retry prompts.
   - **Risk:** Low.

### Memory system
- **Strength:** Memory retrieval for implement role is healthy and cheap.
- **Weakness:** Reviewer/log-analysis retrieval showed 0-hit behavior in the sampled runs.
- **Concrete improvements:**
  - add reviewer-role keyword generation fallback,
  - log token budgets with retrieves,
  - flag zero-hit reviewer retrieves separately from implement retrieves.

### Estimated impact
- **Tokens:** moderate savings on retry-heavy runs; higher once cache metrics are measurable.
- **Latency:** modest direct savings, larger on repeated failures/retries.
- **Reliability:** better diagnosis when cache/memory are not helping.

## Orchestrator Health

### Observed health signals
- **High skip volume but fast short-circuiting:**  
  `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all have p50 durations of `1s`, which suggests gating is cheap and mostly server-side.
- **No stuck terminal loops surfaced in sampled orchestrate runs.**
- **Poller completes reliably:**  
  `orchestrate_poll` success rate is strong in the sample; sampled runs recorded `poll_started` and `poll_completed` ledger events.
- **But control-plane overhead is still visible:**  
  no-work `orchestrate_poll` runs still take `43–49s`.

### Recurring pain points
1. **Review path ambiguity on Claude branches**
   - Long comment-only reviews are operationally expensive and hard to distinguish from meaningful work.

2. **Support-ref fallback drift**
   - Warnings about unavailable support refs undermine confidence that the run used the intended helper version.

3. **Run-end bookkeeping occasionally missing**
   - Missing memory helper scripts mean some failure events are never recorded.

### Smallest safe mitigations
- Add explicit metrics for:
  - comment-only review count,
  - support-ref fallback count,
  - missing run-end bookkeeping count,
  - no-change retry bails in implement/review.
- Add a dedicated “no-op control-path” optimization mode for poll/promote/merge flows.

### Indicators to track
- `% of review_autofix runs that are comment-only`
- `median duration of comment-only review_autofix`
- `% of implement runs ending in no-actionable-output bail`
- `support checkout ref unavailable` warning count
- `memory helper missing` warning count
- `orchestrate_poll` no-work median duration

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Review/autofix compute bottleneck**
   - Dominant in the current window.
   - Affects implement → review → validate progression most.
   - Includes multi-model reviewer overhead, setup cost, and long comment-only reviews.

2. **Release validation loop bottleneck**
   - `test_and_mark_stable` runs are both long and failing.
   - The loop reaches late-stage Phase 4 before discovering bait removal did not happen, wasting most of the run.

3. **Workflow-log-analysis compute cost**
   - Not on the critical product path, but it is a major background drain on tokens and runner time.

4. **Queue + fetch overhead on control workflows**
   - Poll/promote/forward-merge all pay checkout and runner-start cost disproportionate to their logic.

5. **Retry overhead in implement**
   - Small in duration compared with review, but significant in wasted tokens and failed smoke runs.

### Bottleneck fixes by impact
1. **Slim comment-only review paths**
2. **Make smoke-test bait-removal path deterministic**
3. **De-duplicate workflow-log-analysis summaries**
4. **Use targeted/shallow fetch in poll/promote/merge**
5. **Short-circuit deterministic implement failures earlier**

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail runs (`p95 1685.4s`; examples up to `2062s`)
- `test_and_mark_stable` failures and 67–108 minute durations
- `workflow_log_analysis` long/costly background passes
- unnecessary full-history fetches in control-plane workflows

**Top failure modes**
- Smoke-test bait line not removed (`25247210528`, `25249170035`, `25252918179`)
- `implement` empty-output / announced-edit-without-change bails (`25245077011`, `25245085089`, `25246727158`)
- transient external download failure in CI (`25249161547`, actionlint tarball `502`)

**Highest-cost drivers**
- `workflow_log_analysis` summarization and API-redundancy passes
- long multi-model `review_autofix` runs
- repeated retry prompts on implement failures

**Top 3 prioritized actions**
1. **Create a lightweight `review_autofix` mode for comment-only Claude-branch reviews.**
2. **Fix the smoke-test editor path so bait-removal reviews cannot degrade into empty-output/comment-only behavior.**
3. **Add run-ID-based reuse/delta summarization in `workflow_log_analysis` to cut 100k–200k tokens per analysis run.**

## Metrics Appendix

### Repository-level run summary

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 285 | 7 | 40 | 668 | 0.7% | 132.4 | 1.0 | 615.0 |

### Workflow-family summary

| Workflow family | Total runs | Success | Failure | Cancelled | Avg duration (s) | p50 (s) | p95 (s) | Key note |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| ci | 63 | 62 | 1 | 0 | 597.4 | 609.0 | 643.9 | Stable but ~10m baseline; one actionlint download flake |
| implement | 181 | 23 | 3 | 7 | 31.6 | 1.0 | 219.0 | Most runs skipped; sampled failures were empty-output bails |
| review_autofix | 62 | 32 | 0 | 29 | 369.2 | 47.0 | 1685.4 | Largest latency hotspot |
| test_and_mark_stable | 6 | 0 | 3 | 3 | 4115.3 | 4043.0 | 6422.3 | Worst reliability hotspot |
| orchestrate_poll | 19 | 19 | 0 | 0 | 43.1 | 43.0 | 48.1 | No-work polls still expensive |
| workflow_log_analysis | 6 | 5 | 0 | 1 | 4617.0 | 4428.0 | 6025.0 | Major background cost center |
| plan | 181 | 26 | 0 | 0 | 13.4 | 1.0 | 144.0 | Mostly skipped |
| clarify | 217 | 30 | 0 | 0 | 19.8 | 1.0 | 128.2 | Mostly skipped |
| validation_refresh | 3 | 3 | 0 | 0 | 210.7 | 220.0 | 220.0 | `refresh` dominates runtime |

### Notable failed runs

| Run ID | Workflow family | Duration (s) | Failure point |
|---|---|---:|---|
| 25252918179 | test_and_mark_stable | 4457 | `e2e-smoke-test / Phase 4b: Verify editor removed bait line` |
| 25249170035 | test_and_mark_stable | 6255 | `e2e-smoke-test / Phase 4b: Verify editor removed bait line` |
| 25247210528 | test_and_mark_stable | 6478 | `e2e-smoke-test / Phase 4b: Verify editor removed bait line` |
| 25246727158 | implement | 184 | `implement / Run Codex implementation` |
| 25245085089 | implement | 188 | `implement / Run Codex implementation` |
| 25245077011 | implement | 176 | `implement / Run Codex implementation` |
| 25249161547 | ci | 13 | `lint / Install actionlint` |

### Sampled token metrics

| Source | Sample count | Token metric |
|---|---:|---:|
| `workflow_log_analysis` `summarize_unselected_runs` telemetry | 8 | Avg `164,825` tokens/run |
| `workflow_log_analysis` API redundancy pass (`25252928519`) | 1 | `304,969` tokens |
| Failed implement run `25245077011` | 1 | `37,646` tokens across 2 attempts |
| Failed implement run `25245085089` | 1 | `29,529` tokens across 2 attempts |
| Failed implement run `25246727158` | 1 | `18,600` tokens across 2 attempts |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Valid telemetry entries | 96 |
| Retrieve count | 15 |
| Retrieve hit rate | 86.7% |
| Avg retrieve estimated tokens | 42.9 |
| `keyword_method=plain` | 13 |
| `keyword_method=none` | 2 |
| `keyword_method=llm` | 0 |
| Zero-record retrieves | 2 |
| `fail_open: true` entries | 0 |
| `enabled: false` entries | 0 |
| High push-attempt entries (`>1`) | 2 |

### Prompt-cache / cache signals

| Signal | Observation |
|---|---|
| OpenRouter prompt cache enabled | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) |
| Cache hit/read token metrics | Not usable; observed as `na` in sampled cache probe lines |
| Actions cache hits | Observed for `codex-v0.114.0` and `setup-uv-*` keys |
| Memory compaction | Run `25254305081` archived `2914` candidates in ~14s |

### GH API hotspot summary

| Workflow / step | Observed hotspot |
|---|---|
| `implement / Run Codex implementation` | repeated `GET /issues/{ISSUE_NUMBER}` and paginated comment fetches |
| `issue_pr_status / Update linked issue labels when PR closes` | `POST /graphql` for `closingIssuesReferences`, then per-issue REST fallback/lookups |
| `review_autofix / Dispatch standalone validate...` | GraphQL linked-issue lookup + fallback `GET /pulls/{PR_NUMBER}` + `gh workflow run` |
| `cancel_on_pr_close / Cancel queued/in-progress runs...` | `/actions/runs` scan plus `/rate_limit`-aware retry wrapper |
| `test_and_mark_stable / Phase 4 wait/verify` | repeated Actions/PR polling in long-running smoke-test coordination |

If you want, I can turn this into a prioritized implementation backlog with owner/team labels and a 1-week vs 1-month plan.
