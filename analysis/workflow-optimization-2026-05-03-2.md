## Executive Summary

- **`test_and_mark_stable` is the highest-severity blocker:** all 5 sampled runs failed, each after **4,457–6,478s**, at `e2e-smoke-test → Phase 4b: Verify editor removed bait line`. In one deep-dive run, the canary still contained `# E2E_EDITOR_BAIT_...`, proving the editor path never removed the injected bait. **Estimated impact:** recover ~75–108 minutes per release attempt and unblock stable promotion. **Confidence: high.**
- **Comment-only `review_autofix` runs are the largest active latency/cost sink:** recent runs `25267058904` (2,304s), `25268065004` (2,175s, cancelled), and `25271276362` (1,519s) all spent most time in `review / codex-agent (claude-branch-review)` while logs said `editor/commit/judge/auto-merge skipped`. **Estimated impact:** cut affected review runs by ~10–25 minutes and materially reduce model spend. **Confidence: high.**
- **`workflow_log_analysis` is overspending tokens on re-summarizing unchanged runs:** sampled `summarize_unselected_runs` telemetry used **156,314 / 190,564 / 226,933 / 237,071 / 237,335 tokens** per run while targeting ~80–88 unselected runs each time. **Estimated impact:** save ~150k–240k tokens per analysis run with run-ID reuse. **Confidence: high.**
- **CI failures are mostly contract drift and external download fragility, not broad test instability:** two runs (`25267881013`, `25267991186`) failed because `mode-validate-generate.txt` no longer contained `Test script contract (MANDATORY):`; another failed because `actionlint` download returned HTTP 502 in `25249161547`. **Estimated impact:** reduce CI reruns/failures by ~50–70% of recent CI failures. **Confidence: high.**
- **Prompt cache is enabled but effectively unobservable:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears across implement/review/poll, yet deep-audit logs report cache token counters as `na`, so cache effectiveness cannot be optimized confidently. **Estimated impact:** likely 5–15% token/latency reduction in prompt-heavy paths once instrumentation and stable prefixes land. **Confidence: medium.**
- **AI memory retrieval is cheap but underperforming for review:** across 10 `retrieve` telemetry events, hit rate was **60%**, average `estimated_tokens` was **33.6**, and both recent review runs retrieved **0 records** with `keyword_method: "none"`. **Estimated impact:** modest quality/reliability gain, low cost impact. **Confidence: high.**

## Speed Optimizations

Ranked by expected latency reduction.

### 1. Fix the E2E editor-path regression in `test_and_mark_stable`  
**Type:** Critical-path win

- **Evidence**
  - `test_and_mark_stable` has **5/5 failures** and **0 successes**, with p50 **6,049s** and p95 **6,433.4s**.
  - All failures stopped at `e2e-smoke-test → Phase 4b: Verify editor removed bait line`.
  - In run `25252918179`, the failure log showed:
    - `##[error]Editor failed to remove bait line E2E_EDITOR_BAIT_25252918179`
    - Canary contents still included `# E2E_EDITOR_BAIT_25252918179: this line should be removed by the editor (smoke gate)`.
  - Recent review runs show the gate often takes the **comment-only** route: `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... editor/commit/judge/auto-merge skipped`.

- **Root cause**
  - The E2E smoke test expects an **editor commit path**, but the review flow is often taking a **reviewer-only/comment-only** path, so the bait line never gets removed.

- **Exact change**
  - Add an explicit gate override for E2E PRs (`e2e-smoke-test` / bait-marker / `force-review`) so `review_autofix` must execute the **editor + commit** path, not reviewer-panel-only mode.
  - Keep the current `pr_already_closed` fail-fast, but make the E2E label override authoritative before deterministic skip / comment-only logic.

- **Estimated time savings**
  - **~75–108 minutes saved per stable release attempt** by eliminating full-run failures and reruns.

- **Implementation risk**
  - **Medium**: touches review gating, but can be kept narrow to E2E-labeled PRs only.

---

### 2. Add a lightweight review mode for comment-only `review_autofix` runs  
**Type:** Critical-path win

- **Evidence**
  - `review_autofix` family p95 is **2,066.6s**.
  - Recent comment-only runs:
    - `25267058904`: **2,304s**
    - `25268065004`: **2,175s** (cancelled)
    - `25271276362`: **1,519s**
  - These runs explicitly logged that **editor/commit/judge/auto-merge were skipped**.
  - Recent logs also show large reviewer pools, e.g. `REVIEWER_MODELS` with six providers in `25271250967`.

- **Root cause**
  - The workflow is paying for a near-full reviewer panel even when the path cannot edit or merge.

- **Exact change**
  - For `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` and other comment-only paths:
    - reduce reviewer pool to **1 fast primary + 1 fallback**,
    - cap summarizer input (`XPOLL_SUMMARISER_LINES_PER_REVIEWER`) lower than 160,
    - reduce call timeout from 2400s for non-edit runs,
    - keep full reviewer pool only for edit/merge-capable paths.

- **Estimated time savings**
  - **~10–25 minutes per affected review run**.

- **Implementation risk**
  - **Medium**: slight review-quality risk on edge cases; low correctness risk if full mode remains for edit/merge paths.

---

### 3. Reorder CI to run fragile contract tests first  
**Type:** Critical-path win for failed runs

- **Evidence**
  - CI successes cluster around **540–652s**; family p50 is **606.5s**.
  - Failures in `25267881013` and `25267991186` happened late, after:
    - `81 passed, 0 failed` in `tests/test_orchestrate_lib.py`,
    - then `tests/test_validate_harness_rpc.py` failed on a prompt-string contract.
  - `25266932433` and `25266996700` similarly failed after long test progress in `tests/test_orchestrate_poll_process.py`.

- **Root cause**
  - Fast-breaking prompt/process contract tests are scheduled after longer suites, so failures arrive near the 9–10 minute mark.

- **Exact change**
  - Split or reorder `ci.yml` so these run first:
    - `tests/test_validate_harness_rpc.py`
    - `tests/test_orchestrate_poll_process.py`
    - prompt/workflow contract tests
  - Run broader suites only after these pass.

- **Estimated time savings**
  - **~7–8 minutes faster failure feedback** on bad CI runs.

- **Implementation risk**
  - **Low**: no behavior change, just earlier fail-fast.

---

### 4. Replace full-ref fetches with targeted fetches in control workflows  
**Type:** High-frequency cumulative win

- **Evidence**
  - `orchestrate_poll` recent run `25271369084` spent ~**9s of 49s** in checkout/fetch and pulled many tags.
  - Deep-audit logs flagged `orchestrate_poll`, `promote_main_to_stable`, and `forward_merge_stable_to_main` for broad fetches (`refs/heads/*`, tags, or full history).
  - `resolve-version` in stable-release runs fetched many unrelated branches/tags.

- **Root cause**
  - Control-plane jobs fetch repository-wide refs when they only need a small subset.

- **Exact change**
  - `orchestrate_poll`: shallow fetch default branch + required support ref only.
  - `forward_merge_stable_to_main`: fetch only `main` and `stable`.
  - `promote_main_to_stable`: fetch `main`, `stable`, and tags only if version resolution requires tags.
  - Keep a guarded fallback to full fetch only on explicit need.

- **Estimated time savings**
  - `orchestrate_poll`: **~5–10s/run** across **47 runs** in-window.
  - Small but frequent savings for `promote` / `forward_merge`.

- **Implementation risk**
  - **Low** if guarded fallback remains.

---

### 5. Remove artifact-cleanup tail latency in `copilot_pull_request_reviewer`  
**Type:** Moderate win

- **Evidence**
  - `25268066425`: `Cleanup artifacts` took **~4m16s** of a **289s** run.
  - `25266933519`: cleanup dominated the tail of a **384s** run.
  - `25271276989`: cleanup dominated the visible runtime in a **131s** run.
  - Logs show `gh api /repos/.../actions/runs/<id>/artifacts` followed by serial delete loop.

- **Root cause**
  - Serial list-then-delete artifact cleanup is expensive relative to the actual review work.

- **Exact change**
  - Upload fewer artifacts, or keep one consolidated artifact.
  - Skip cleanup when zero/one retained artifact is expected.
  - Delete only current-step artifacts by name/prefix, not every artifact on the run.

- **Estimated time savings**
  - **~2–4 minutes per affected run**.

- **Implementation risk**
  - **Low-medium**: ensure retention expectations stay intact.

---

### 6. Micro-optimization: stop preflighting `/rate_limit` on every cancel run  
**Type:** Micro-optimization

- **Evidence**
  - `cancel_on_pr_close` runs are short (5–6s), but always call `gh api -i /rate_limit` before doing no-op checks.
  - Recent runs `25271951089` and `25271250963` found no matching runs to cancel.

- **Root cause**
  - Defensive rate-limit probe adds one API call even when there is no work.

- **Exact change**
  - Only call `/rate_limit` after an actual 403/rate-limit error, not as preflight.

- **Estimated time savings**
  - Small per run; more valuable as call-count reduction than latency.

- **Implementation risk**
  - **Low**.

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1. Cache and reuse `workflow_log_analysis` summaries by run ID  
- **Evidence**
  - Sampled `summarize_unselected_runs` telemetry:
    - `25247218394`: **156,314 tokens**
    - `25249181773`: **190,564 tokens**
    - `25254390226`: **226,933 tokens**
    - `25265928747`: **237,071 tokens**
    - `25252928519`: **237,335 tokens**
  - These runs summarized **80–88** unselected runs while targeting **100** each time.

- **Root cause**
  - The analyzer repeatedly re-summarizes old unselected runs instead of reusing prior summaries.

- **Exact change**
  - Persist per-run summaries keyed by `run_id` + log hash.
  - Recompute only for new runs or when log content changes.
  - Lower default unselected-run expansion below 100 unless failure coverage is thin.

- **Estimated savings**
  - **~150k–240k tokens per `workflow_log_analysis` run**.

- **Quality-risk notes**
  - **Low** if invalidated on log hash change.

---

### 2. Reduce model fan-out for comment-only `review_autofix`  
- **Evidence**
  - Comment-only review runs still last **1,519–2,304s**.
  - Recent logs show a large reviewer pool and summarizer use even when edit/merge are skipped.
  - `REVIEWERS_SUCCESSFUL: 6` appeared in `25268065004`.

- **Root cause**
  - Non-mutating review paths are still paying multi-model reviewer costs.

- **Exact change**
  - Use a lighter model set on comment-only paths:
    - 1 fast reviewer by default,
    - optional second reviewer only on disagreement / high-risk diff,
    - lower reasoning and summarizer breadth on non-edit paths.

- **Estimated savings**
  - Likely the **largest ongoing AI spend reduction** among active workflows; reasonable expectation is **50–80% lower token cost** on comment-only review runs.

- **Quality-risk notes**
  - **Medium**: review breadth narrows, so keep escalation to full panel on disagreement or risky files.

---

### 3. Stop retrying implement with the same heavy prompt after a no-op  
- **Evidence**
  - Failed implement run `25246727158`:
    - first telemetry retrieve cost small (56 est. tokens),
    - visible editor token block showed **14,134 tokens used** on the failed pass,
    - deep-audit summary cited **18,600 tokens** for that failed run and **85,775 tokens across 3 failed runs**.
  - The run ended with:
    - `Codex announced an edit/apply_patch on attempt 2 ... but produced no file changes`
    - `Codex produced no actionable output 2 attempts in a row`.

- **Root cause**
  - Retry attempts repeat large static instructions and setup despite a deterministic no-change pattern.

- **Exact change**
  - After first announced-edit-without-change:
    - switch to a **minimal retry prompt** containing only delta diagnostics,
    - lower reasoning from `xhigh` to `medium`,
    - early-bail on fully specified one-file smoke tasks.

- **Estimated savings**
  - **~10k–30k tokens per failed implement run**.

- **Quality-risk notes**
  - **Low-medium**: keep full first attempt; only shrink retries.

---

### 4. Stabilize prompt prefixes so prompt cache can actually hit  
- **Evidence**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in implement/review/poll.
  - Deep-audit explicitly says prompt cache token counters are `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`.
  - Implement logs show repeated large Serena/Context7/Git MCP instruction blocks across retries.

- **Root cause**
  - Cache is enabled, but prompt prefixes likely vary too early and cache effectiveness is not measured.

- **Exact change**
  - Freeze the first prompt chunk for implement/review retries:
    - static instructions first,
    - move dynamic issue/PR/run IDs later,
    - inject Serena efficiency guidance once, not repeatedly.
  - Emit actual cache create/read counters to logs.

- **Estimated savings**
  - **Unquantified due missing counters**; likely meaningful in implement/review paths.

- **Quality-risk notes**
  - **Low** if ordering changes do not alter instruction meaning.

---

### 5. Avoid four-workflow no-op fan-out on irrelevant comments  
- **Evidence**
  - Repeated timestamp bundles launched multiple workflows that all skipped in **1–2s**, e.g. at `2026-05-03T06:15:29Z`:
    - `clarify` `25271747964`
    - `plan` `25271747967`
    - `implement` `25271747961`
    - `orchestrate_clarify_respond` `25271747962`
  - Similar bundles appeared at `03:23:06Z`, `02:39:56Z`, `01:19:15Z`, `01:18:34Z`, `01:05:16Z`.

- **Root cause**
  - Multiple slash-command workflows are triggered on the same events and only self-filter after starting.

- **Exact change**
  - Add a single front-door router or tighter event filters so only the candidate workflow dispatches.

- **Estimated savings**
  - Small per run, but steady hosted-runner savings across high-volume comment activity.

- **Quality-risk notes**
  - **Low-medium**: verify command-routing parity before consolidating.

## Reliability Improvements

Ranked by expected failure-rate / rerun-rate reduction.

### 1. Make E2E review gating require the editor path for smoke-test PRs  
- **Failure evidence**
  - `test_and_mark_stable`: **5 failures / 5 runs**.
  - Runs `25247210528`, `25249170035`, `25252918179`, `25254380200`, `25265920645` all failed at `Phase 4b`.
  - One deep-dive canary still contained the bait marker after review.

- **Root cause category**
  - Workflow gating / path-selection bug.

- **Exact fix**
  - Add an E2E-only override so smoke-test PRs bypass comment-only reviewer mode and deterministic skip behavior that prevents editor execution.

- **Expected reliability impact**
  - Potentially moves stable-release validation from **0% pass rate** toward normal operation.

- **Rollback / fail-open**
  - Scope the override only to E2E labels/markers; revert by removing the special-case gate.

---

### 2. Add retries/backoff for `actionlint` binary fetch  
- **Failure evidence**
  - CI run `25249161547` failed in **13s** at `Install actionlint`.
  - Log: `curl: (22) The requested URL returned error: 502`.

- **Root cause category**
  - External transient dependency fetch.

- **Exact fix**
  - Wrap the download in bounded retry with exponential backoff for 5xx/network failures.
  - Optionally cache the verified binary once installed during the job.

- **Expected reliability impact**
  - Eliminates a pure transient failure mode with no code-quality signal.

- **Rollback / fail-open**
  - Low risk; retain checksum verification and fail hard after bounded retries.

---

### 3. Stop prompt-contract drift from breaking CI late  
- **Failure evidence**
  - Runs `25267881013` and `25267991186` failed because `mode-validate-generate.txt` no longer included `Test script contract (MANDATORY):`.
  - Failure occurred in `tests/test_validate_harness_rpc.py`, not from runtime execution.

- **Root cause category**
  - Prompt contract drift / brittle string coupling.

- **Exact fix**
  - Generate the tested contract block from a shared source fragment, or update tests to validate the semantic contract via anchored markers rather than exact prose heading only.

- **Expected reliability impact**
  - Removes repeated prompt-edit regressions from CI.

- **Rollback / fail-open**
  - Low risk if the contract remains mandatory and machine-checkable.

---

### 4. Add early cancellation before expensive review jobs start  
- **Failure evidence**
  - `review_autofix` had **41 cancelled runs out of 79** total.
  - Several cancelled runs still ran `review / codex-agent`, e.g. `25268065004` and `25271880958`.

- **Root cause category**
  - Superseded-run churn / concurrency handling.

- **Exact fix**
  - Tighten concurrency so superseded PR-head runs cancel **before** Codex-agent startup, not mid-review.

- **Expected reliability impact**
  - Lowers noisy cancellations and reduces “did this actually fail?” investigation overhead.

- **Rollback / fail-open**
  - Keep cancellation keyed to PR + head SHA; fall back to current behavior if edge cases appear.

---

### 5. Make nightly validation self-test publish failing fixture names inline  
- **Failure evidence**
  - `nightly_validation_selftest` run `25268666198` failed with:
    - `fixtures=3 passed=1 failed=2`
  - Current log excerpt does not expose failing fixture names directly.

- **Root cause category**
  - Incomplete diagnostic surfacing.

- **Exact fix**
  - Echo failing fixture IDs/names to the job summary and step log before exit.

- **Expected reliability impact**
  - Faster repair loop; lower repeated nightly failures due to slower diagnosis.

- **Rollback / fail-open**
  - Low risk; additive logging only.

## AI Memory Health

- Deep-dive logs contained **65 `AI_MEMORY_TELEMETRY` events** across sampled runs.
- Operation mix:
  - `record-run-event`: **32**
  - `retrieve`: **10**
  - `summarize_unselected_runs`: **7**
  - `processed-command-check`: **6**
  - `processed-command-claim`: **6**
  - `compact`: **2**
  - `record-candidate`: **2**

### Retrieval effectiveness
- **Retrieve hit rate:** **60%** (`6/10` had `records_selected > 0`)
- **Average `estimated_tokens`:** **33.6**
- **`keyword_method` distribution:**
  - `plain`: **6**
  - `none`: **4**
  - `llm`: **0**

### Notable issues
- **4/10 retrieves returned 0 records**, including both recent review runs:
  - `review_autofix` `25271276362`
  - `review_autofix` `25271880958`
- Reviewer retrievals used `keyword_method: "none"` and selected `0` records, so memory is currently not helping the review path.
- I found **no `fail_open: true` retrieve entries**.
- I found **no `enabled: false` retrieve entries**.
- Push retries were generally healthy; the highest observed memory push retry count was **2** on `record-run-event` for failed implement flow `25246727158`, not on retrieval itself.

### Positive signals
- `memory_maintenance` emitted a successful `compact` op for month `2026-04` with `did_push: true`.
- `orchestrate_poll` consistently recorded `poll_started` / `poll_completed` events with `push_attempts: 1`.

### Recommendation
- Keep memory retrieval enabled, but tune review retrieval to use actual PR/file keywords so it stops returning `0` records.
- Add a per-role retrieval summary to step output: hit/miss, selected IDs, and token budget used.

## GH API Call Audit

### 1. `test_and_mark_stable` repeatedly polls workflow runs and issue state
- **Evidence**
  - In `25265920645`, multiple steps (`validate-standalone-test`, `e2e-smoke-test`, `clarify-rejects-unsolvable-test`, `orchestrate-decompose-test`, `orphan-workflows-test`) repeatedly call:
    - `repos/.../actions/workflows/<wf>/runs?per_page=1`
    - `...runs?per_page=10`
    - `repos/.../actions/runs/<id>`
    - issue labels/comments reads
  - Similar patterns recur in `25247210528`, `25249170035`, `25252918179`.

- **Problem pattern**
  - High-frequency dispatch polling and repeated re-fetch of the same run/issue state.

- **Concrete recommendation**
  - Centralize a single per-phase watcher helper:
    - cache discovered run IDs,
    - poll `runs/{id}` only after discovery,
    - reuse issue labels/comments already fetched in the same phase,
    - increase poll interval after the first few polls.

- **Estimated reduction**
  - **~30–50% fewer GH API calls** in stable-release test runs.
  - Lower risk of hitting rate-sensitive polling paths called out in repo API hygiene guidance.

---

### 2. `cancel_on_pr_close` still spends calls on no-op cancellations
- **Evidence**
  - `25271951089` and `25271250963`:
    - call `/rate_limit`,
    - fetch queued runs,
    - fetch in-progress runs,
    - often find nothing to cancel.

- **Problem pattern**
  - One preflight plus two paginated queries even when branch has no active runs.

- **Concrete recommendation**
  - Remove `/rate_limit` preflight.
  - Bound pagination aggressively for PR-close cases.
  - Consider one branch-scoped query and client-side status filter if payloads stay small enough.

- **Estimated reduction**
  - **1–2 API calls per cancel run** plus fewer paginated pages.

---

### 3. `copilot_pull_request_reviewer` artifact cleanup is an avoidable delete loop
- **Evidence**
  - `25271276989`, `25268066425`, `25266933519` all call `/actions/runs/<id>/artifacts` then delete artifacts serially.

- **Problem pattern**
  - List + N deletes on every run, even when cleanup dominates total runtime.

- **Concrete recommendation**
  - Delete only current-attempt artifacts by known prefix.
  - Skip cleanup entirely if a single retained artifact policy is acceptable.
  - Avoid deleting unrelated artifacts from the same run.

- **Estimated reduction**
  - Potentially **N+1 → 1–2 calls** for small-artifact runs.

---

### 4. `review_autofix` does small duplicate metadata lookups
- **Evidence**
  - Recent runs show:
    - `repos/${REPOSITORY}/pulls?state=open&head=...`
    - `repos/${REPOSITORY}` for `default_branch`
  - Post-merge validate dispatch separately queries linked issues with GraphQL.

- **Problem pattern**
  - Same PR/repo metadata fetched in separate calls on short paths.

- **Concrete recommendation**
  - Reuse event payload fields where available.
  - Consolidate repo default-branch + PR existence into one GraphQL read when practical.

- **Estimated reduction**
  - Small per run, but worthwhile given high `review_autofix` volume.

---

### 5. `issue_pr_status` has decent batching already, but fallback can still fan out
- **Evidence**
  - `25271951088` uses GraphQL for `closingIssuesReferences`.
  - It falls back to per-issue REST detection when GraphQL batch fails.

- **Problem pattern**
  - Safe fallback is correct, but can reintroduce per-issue loops.

- **Concrete recommendation**
  - Keep current safety behavior, but log fallback frequency and issue count so teams know if batch failure is becoming common.

- **Estimated reduction**
  - Mainly a rate-limit risk reduction, not a large baseline call reduction.

## MCP & Serena Efficiency

- **No onboarding misuse was visible** in the sampled logs. I did not see `onboarding`, `initial_instructions`, or `check_onboarding_performed` events in the deep-dive excerpts.
- The main Serena inefficiency is **not tool misuse in execution traces**, but **repeated Serena/MCP instruction payload injection** into prompts without corresponding measurable Serena usage.

### Findings

#### 1. Implement retries re-inject large Serena/Context7/Git MCP guidance blocks
- **Evidence**
  - Failed implement run `25246727158` contains repeated long instruction sections covering Serena, Context7, Git MCP, and efficiency rules.
  - The same run ended with:
    - `No Serena tool usage stats found`
    - then generated `SERENA_REPORT_FILE=/tmp/codex-implement-25246727158/serena_efficiency_report.md`.

- **Impact**
  - Token waste without proof that Serena navigation is actually being used.

- **Recommendation**
  - Inject Serena/MCP efficiency guidance **once per run**, then reference it on retries instead of re-inlining.
  - Always upload the Serena efficiency report artifact when present.

#### 2. Serena reporting is too weak to drive optimization
- **Evidence**
  - `25246727158` explicitly logged `No Serena tool usage stats found`.
  - Recent review summaries mention `SERENA_REPORT_FILE`, but direct Serena action counts are not surfaced.

- **Impact**
  - Teams cannot tell whether symbol-first lookups, targeted reads, or fallback file reads are actually happening.

- **Recommendation**
  - Add a standard step-summary block:
    - symbol lookups,
    - pattern searches,
    - fallback file reads,
    - repeated-region reads,
    - report generation status.

#### 3. Current inefficiency is prompt churn, not observable broad-read churn
- **Evidence**
  - Deep-dive logs show setup and prompt scaffolding, but not rich Serena per-call traces.
  - Therefore I cannot prove broad raw-file reads or repeated region reads in production tool use from this window.

- **Recommendation**
  - Keep the Serena-first policy, but add telemetry rich enough to confirm:
    - `get_symbols_overview` / `find_symbol` usage,
    - `search_for_pattern` vs shell grep fallback,
    - repeated file-region reads.

#### 4. Safe parallelism opportunity
- **Evidence**
  - Review/setup paths do serial support-source checkout, staging, config creation, and metadata validation.
- **Recommendation**
  - Parallelize independent non-mutating reads:
    - PR metadata fetch,
    - support-source resolution,
    - runtime config templating.
  - Keep mutating or state-dependent steps serialized.

## Prompt Cache & Memory System

### Prompt cache
- **Observed state**
  - Prompt cache is enabled in sampled implement/review/poll runs: `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - Deep-audit logs state cache counters are not usable: `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`.

- **Observed problems**
  - Large static blocks are repeated across retries.
  - Dynamic run-specific noise appears early enough that cache fragmentation is likely. This is an inference from repeated scaffolding and missing hits, not directly measured.

- **Recommendations**
  1. **Stabilize prompt prefixes**
     - Put static policy/instructions first.
     - Move issue number, PR body, timestamps, run IDs later.
  2. **Deduplicate retry scaffolding**
     - Retry prompts should append only diagnostics, not re-embed full Serena/MCP guidance.
  3. **Emit measurable cache counters**
     - Add create/read token counts to step summary so cache hit rate can be tracked.

- **Estimated impact**
  - **Tokens:** likely moderate savings on implement/review retries.
  - **Latency:** modest improvement from smaller effective prompt bodies.
  - **Reliability:** better observability, lower prompt-drift risk.

### Memory retrieval effectiveness
- Review memory is currently weak:
  - both recent review runs retrieved **0 records**,
  - `keyword_method` was `none`,
  - `estimated_tokens` was `0`.
- Implement memory is healthier:
  - sampled implement retrieves selected **2 records** with `estimated_tokens: 56`.

### Recommendation
- Add role-specific retrieval strategies:
  - review: derive keywords from changed files / PR title,
  - implement: keep current plain-keyword flow.
- Track zero-hit rate by workflow family as a first-class metric.

## Orchestrator Health

### What looks healthy
- `orchestrate_poll` itself is stable:
  - family success rate **100%** in sampled totals,
  - recent telemetry shows `poll_started` / `poll_completed`,
  - `push_attempts: 1` in recent poll events.
- No evidence in this window of stuck memory writes, fail-open retrieve spam, or repeated conflict-heal loops in production logs.

### What is operationally noisy
#### 1. Heavy skip fan-out
- `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` frequently launch together and skip in 1–2s.
- Example bundle at `2026-05-03T06:15:29Z` launched four workflows and none did useful work.

**Smallest safe mitigation**
- Route slash-command/event filtering earlier so only one workflow starts.

#### 2. `review_autofix` cancellation churn
- `review_autofix` totals:
  - **79 runs**
  - **37 success**
  - **41 cancelled**
  - p95 **2,066.6s**
- This suggests superseded work is often allowed to start expensively before cancellation.

**Smallest safe mitigation**
- Cancel superseded PR-head runs earlier, before Codex agent starts.

#### 3. Implement no-op exploration loops still happen
- `25246727158` shows the orchestrated implement loop recognized a stuck no-op pattern only after two attempts.

**Smallest safe mitigation**
- For deterministic small tasks, bail after first announced-edit-without-change.

### Indicators teams should track
- `review_autofix` cancellation rate
- % of slash-command workflows that skip after start
- `orchestrate_poll` cycles with `has_work=false`
- memory retrieve zero-hit rate by workflow
- stable-release E2E pass rate

## Pipeline Flow Bottlenecks

### Clarify / Plan / Implement entry
- **Bottleneck:** workflow fan-out and skip noise
- **Evidence:** repeated 4-workflow skip bundles around the same timestamps
- **Fix order:** medium impact, low risk

### Implement
- **Bottleneck:** no-op retry loop with heavy prompt/setup reuse
- **Evidence:** `25246727158` failed after two no-action attempts; large repeated prompt blocks
- **Fix order:** high impact on failed-run cost and feedback speed

### Review / Autofix
- **Bottleneck:** comment-only review path still runs expensive reviewer panel
- **Evidence:** `25267058904`, `25268065004`, `25271276362`
- **Fix order:** highest active latency/cost win outside stable-release blocker

### Validate / CI
- **Bottleneck:** slow fail feedback
- **Evidence:** brittle contract failures surface late in ~9–10 minute CI jobs
- **Fix order:** high impact on developer feedback loop, low risk

### Stable release test flow
- **Bottleneck:** editor-bait verification regression
- **Evidence:** 5 straight failures, 100% failure rate
- **Fix order:** top priority because it blocks release progression

### Post-processing / Cleanup
- **Bottleneck:** artifact cleanup loops
- **Evidence:** copilot review cleanup dominates tails
- **Fix order:** moderate impact, very safe

### Queueing overhead
- **Bottleneck:** hosted runner wait appears in most workflows
- **Evidence:** recurring `Job is waiting for a hosted runner to come online.`
- **Fix order:** limited room to optimize without infrastructure changes; mitigate indirectly by removing avoidable runs and shortening jobs

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` failing after ~75–108 minutes per attempt
- `review_autofix` comment-only runs taking 25–38 minutes
- `workflow_log_analysis` spending ~156k–237k tokens/run summarizing unselected runs
- CI failures surfacing late in otherwise long jobs

**Top failure modes**
- E2E editor bait not removed (`25247210528`, `25249170035`, `25252918179`, `25254380200`, `25265920645`)
- Prompt contract drift in `mode-validate-generate.txt` (`25267881013`, `25267991186`)
- `actionlint` binary download 502 (`25249161547`)
- Nightly validation self-test fixture failures (`25268666198`)

**Highest-cost drivers**
- Comment-only `review_autofix` reviewer panel fan-out
- `workflow_log_analysis` re-summarization of unselected runs
- Implement retries with repeated heavy prompt scaffolding

**Top 3 prioritized actions**
1. **Fix E2E review gating so smoke-test PRs always run the editor path**
2. **Introduce light-review mode for comment-only `review_autofix`**
3. **Persist `workflow_log_analysis` summaries by run ID to stop re-summarizing unchanged runs**

## Metrics Appendix

### Repo-level summary

| Repo | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | p50 duration (s) | p95 duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 322 | 12 | 47 | 619 | 1.2% | 1.0 | 620.0 |

### Key workflow families

| Workflow family | Total runs | Success | Failure | Cancelled | p50 (s) | p95 (s) | Notable note |
|---|---:|---:|---:|---:|---:|---:|---|
| `test_and_mark_stable` | 5 | 0 | 5 | 0 | 6049.0 | 6433.4 | 100% failure, all at bait verification |
| `review_autofix` | 79 | 37 | 0 | 41 | 47.0 | 2066.6 | High cancellation and long comment-only reviews |
| `ci` | 72 | 67 | 5 | 0 | 606.5 | 647.1 | Long jobs; recent failures mostly contract/download issues |
| `workflow_log_analysis` | 5 | 5 | 0 | 0 | 5641.0 | 6035.0 | Extremely expensive analysis family |
| `orchestrate_poll` | 47 | 47 | 0 | 0 | 45.0 | 49.0 | Stable, but checkout dominates no-work cycles |
| `clarify` | 199 | 27 | 0 | 0 | 1.0 | 133.0 | Mostly skipped |
| `plan` | 166 | 22 | 0 | 0 | 1.0 | 139.3 | Mostly skipped |
| `implement` | 166 | 23 | 1 | 6 | 1.0 | 248.0 | Mostly skipped; one failed heavy no-op loop |

### Sampled token telemetry

| Workflow / run | Telemetry op | Tokens used |
|---|---|---:|
| `workflow_log_analysis` `25247218394` | `summarize_unselected_runs` | 156,314 |
| `workflow_log_analysis` `25249181773` | `summarize_unselected_runs` | 190,564 |
| `workflow_log_analysis` `25254390226` | `summarize_unselected_runs` | 226,933 |
| `workflow_log_analysis` `25265928747` | `summarize_unselected_runs` | 237,071 |
| `workflow_log_analysis` `25252928519` | `summarize_unselected_runs` | 237,335 |

**Sampled total:** **1,048,217 tokens** across 5 analysis runs  
**Sampled average:** **209,643 tokens/run**

### AI memory retrieval metrics

| Metric | Value |
|---|---:|
| `retrieve` events | 10 |
| Hit rate (`records_selected > 0`) | 60% |
| Zero-record retrieves | 4 |
| Avg `estimated_tokens` | 33.6 |
| `keyword_method=plain` | 6 |
| `keyword_method=none` | 4 |
| `keyword_method=llm` | 0 |
| `fail_open: true` retrieves | 0 |
| `enabled: false` retrieves | 0 |

### Prompt/cache signals

| Signal | Observation |
|---|---|
| Prompt cache enabled | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false` seen in implement/review/poll) |
| Prompt cache read/create counters | Not usable (`cache_creation_input_tokens=na`, `cache_read_input_tokens=na` in deep audit) |
| Non-LLM cache hits | `setup-uv` cache hits observed in review runs |
| Measurable prompt-cache hit rate | Not available in current window |

### GH API hotspot summary

| Workflow / step | Pattern | Evidence | Est. avoidable reduction |
|---|---|---|---|
| `test_and_mark_stable` / multiple E2E steps | Repeated run polling + issue re-reads | `runs?per_page=1/10`, `runs/{id}`, issue labels/comments in loops | 30–50% of GH calls in those runs |
| `cancel_on_pr_close` / `cancel-active-runs` | `/rate_limit` preflight + dual paginated run fetches | Recent no-op runs still call both | 1–2 calls/run |
| `copilot_pull_request_reviewer` / `Cleanup artifacts` | List + serial delete loop | Cleanup dominates 131–289s sampled runs | N+1 → 1–2 calls for small runs |
| `review_autofix` short control paths | Duplicate PR/repo metadata lookups | separate PR existence + repo default branch calls | small per run |
| `issue_pr_status` fallback path | GraphQL-to-REST fallback loops | safe fallback on orchestrator issue detection | mostly risk reduction, not baseline reduction |

## Deep Audit — Workflows & Scripts (2026-05-03)

### Section 1: Bug & Correctness Sweep

#### BUG-001 — `review_autofix` fallback phase-label helper can leave contradictory phase labels behind
- **File path** — `.github/workflows/review_autofix.yml:3774-3792, 3894-3913`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — When `label_helpers.sh` is unavailable, the workflow defines `set_issue_phase_label_resilient()` as a one-way `POST /labels` helper that only adds the target label. It does **not** remove any existing phase labels. The canonical implementation in `scripts/label_helpers.sh:160-196` does a GET → compute phase swap → PUT/POST fallback, which preserves the single-phase-label contract from `.github/ai/label_contract.v1.json`. In the fallback path here, an issue can retain `ai:done`/`ai:planning`/`ai:clarification` while also gaining `ai:ready-to-merge` or `ai:review-blocked`, which is a correctness bug for any automation that expects one active phase label.
- **Recommended fix** — Remove the ad-hoc fallback and either:
  1. re-fetch/source `scripts/label_helpers.sh` before label mutation, as `scripts/review_rb_judge.sh` already does, or  
  2. inline the same GET/PUT/POST phase-swap logic from `scripts/label_helpers.sh:set_issue_phase_label_resilient` so the fallback is behaviorally identical to the shared helper.

#### BUG-002 — review-blocked judge reports `merge` as handled even when no merge is actually confirmed
- **File path** — `scripts/review_rb_judge.sh:416-478, 491-499; .github/workflows/review_autofix.yml:3860-3861`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — In the `merge` path, the script relabels linked issues to `ai:ready-to-merge`, then runs `gh pr merge ... || gh pr merge ... || true`, and finally unconditionally writes `judge_handled=true` / `judge_action=merge`. There is no post-merge verification that the PR actually closed or that auto-merge was successfully armed. Because `review_autofix.yml` only enters the `ai:review-blocked` fallback when `judge_handled != 'true'`, a failed merge attempt can suppress the recovery path and leave an open PR advertised as already resolved. `[NEEDS VERIFICATION]`
- **Recommended fix** — After the merge attempt, re-query the PR state with the existing `_safe_gh_jq` path and only set `judge_handled=true` when:
  - the PR is already merged/closed, or
  - auto-merge was positively enabled.  
  Otherwise emit a distinct non-handled result so the downstream `ai:review-blocked` fallback still runs.

#### SEC-001 — multiple workflows/scripts inject GitHub tokens directly into Git URLs
- **File path** — `.github/workflows/validate.yml:204-219; .github/workflows/issue_pr_status.yml:59-78, 478-490, 567-579; .github/workflows/validation-improvements-intake.yml:66-77; scripts/review_commit_changes.sh:448-455; scripts/review_conflict_resolve.sh:852-853`
- **Severity** — High
- **Category tag** — `security`
- **Description** — Several paths construct authenticated URLs like `https://x-access-token:${GH_TOKEN}@...` or rewrite `origin` with `https://x-access-token:${GH_PAT}@...`. That exposes credentials in process arguments and, for the `git remote set-url` cases, persists them into `.git/config` for the working copy. This repo already has a safer pattern in `.github/workflows/validation-refresh.yml:69-75`, which uses `gh auth setup-git` instead of embedding tokens in URLs.
- **Recommended fix** — Standardize on the `validation-refresh.yml` pattern:
  - call `gh auth setup-git` before `git clone` / `git fetch` / `git push`, or
  - use a non-persistent credential helper / temporary `http.extraheader` for single commands.  
  Remove all `x-access-token:${...}` URL construction from both workflow YAML and shell scripts.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — final-merge polling re-reads the same PR snapshot up to 8 times per tick
- **File path** — `scripts/orchestrate_poll_process.sh:3412-3524`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — Once `final_pr` is known, the poller reads the same PR through separate `pulls/{pr}` calls for `.state`, `.merged_at`, and `.mergeable`:
  - lines 3416-3417: 2 calls
  - lines 3471-3473: 3 calls
  - lines 3522-3524: 3 calls  
  That is **8 REST reads** on the same endpoint in one execution path before counting `gh pr list` / `gh pr create`. The values are all part of the same PR snapshot and can drift between calls.
- **Recommended fix** — Add a single helper such as `fetch_final_pr_snapshot <pr_number>` that returns `{state, mergeable, merged_at, head_sha}` in one call, then reuse that object before and after merge attempts. Extend the existing batched-snapshot style already used by `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`.
- **Current call count** — 8 PR-read calls per tick after `final_pr` is known.
- **Proposed call count after fix** — 2 PR-read calls per tick (one pre-merge snapshot, one post-merge snapshot).

#### BATCH-001 — feature sweep makes one extra PR API read per PR just to fetch `head.sha`
- **File path** — `scripts/orchestrate_poll_process.sh:8885-8917`
- **Severity** — High
- **Category tag** — `api-batching`
- **Description** — The sweep already batches up to 100 open PRs with one `gh pr list --json number,headRefName,baseRefName,mergeable,mergeStateStatus`, but then immediately performs an extra `GET /pulls/{n}` inside the loop for every behind PR to read `.head.sha`. That makes the path **1 batched list call + N per-PR reads + up to N update-branch writes**. Since `gh pr list --json` supports `headRefOid`, the loop can avoid the extra read entirely.
- **Recommended fix** — Add `headRefOid` to the existing `gh pr list --json ...` call and use that as `expected_head_sha` for `update-branch`. If richer batching is needed later, extend the same snapshot/batch approach used elsewhere in `orchestrate_poll_process.sh` rather than adding per-PR REST reads.
- **Current call count** — 1 batched list call + **N** `pulls/{pr}` reads + up to **N** `update-branch` writes.
- **Proposed call count after fix** — 1 batched list call + up to **N** `update-branch` writes.
- **Existing batching pattern to extend** — the current `gh pr list` batch at lines 8885-8889, or the GraphQL alias batching style used by `_fetch_linked_pr_status_graphql`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — six workflows carry near-identical inline GitHub retry/rate-limit wrappers
- **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:307-334, 456-483; .github/workflows/orchestrate_poll.yml:66-97; .github/workflows/review_autofix.yml:1289-1327; .github/workflows/comprehensive-test-and-release.yml:72-98; .github/workflows/test-and-mark-stable.yml:1133-1155`
- **Severity** — Low
- **Category tag** — `duplication`
- **Description** — `_rl_wait`, `_gh_retry`, and `gh_api_safe` variants are copied inline across multiple workflows. They already drift:
  - some paths inspect `/rate_limit`,
  - some only retry `gh api`,
  - some return empty-string fail-open,
  - some log hard errors,
  - backoff caps differ.  
  This duplication is why fixes like the `/rate_limit` preflight removal have to be repeated by hand.
- **Recommended fix** — Consolidate into a shared helper module, preferably `scripts/gh_helpers.sh`, with a minimal API such as:
  - `gh_retry <cmd...>`
  - `gh_retry_to_file <outfile> <cmd...>`
  - `gh_api_safe_json <endpoint> <jq_expr>`  
  Update callers in `cancel_on_pr_close.yml`, `mark-stable.yml`, `orchestrate_poll.yml`, `review_autofix.yml`, `comprehensive-test-and-release.yml`, and `test-and-mark-stable.yml` to source the shared helper instead of carrying local copies.

#### DUP-002 — support-repo bootstrap logic is duplicated and already diverging across workflows
- **File path** — `.github/workflows/validate.yml:214-282; .github/workflows/issue_pr_status.yml:69-131, 484-499, 573-593; .github/workflows/validation-improvements-intake.yml:72-140`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — `checkout_support_ref`, staged `primary/main` clones, `copy_from_ref_or_local` / `fetch_from_ref_or_local`, and local fallback handling are reimplemented in several workflows. `issue_pr_status.yml` duplicates the pattern three times in one file alone (main support fetch, alert helper fetch, cleanup helper fetch). This creates drift risk in auth handling, fallback semantics, and token hygiene.
- **Recommended fix** — Extract a shared bootstrap helper, e.g. `scripts/support_checkout_helpers.sh`, with a narrow interface such as:
  - `bootstrap_support_checkout <wf_source> <preferred_ref> <stage_root>`
  - `copy_from_support <repo_path> <target_path> [allow_main_fallback=true]`  
  Then update `validate.yml`, `issue_pr_status.yml`, and `validation-improvements-intake.yml` to call the shared functions instead of carrying local clones of the logic.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — `test-and-mark-stable` review-wait block is already inside the medium-risk zone
- **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — This interpolated `run:` block measures **16,626 characters**, leaving only **4,374 characters** of headroom before the 21,000-character GitHub expression hard limit. It already mixes multiple `${{ }}` insertions with a large inline shell program, live-log shortcuts, and timeout diagnostics. Given this repo’s prior history of hitting the limit, this block is a realistic regression candidate.
- **Recommended fix** — Extract the wait-loop into an external script such as `scripts/wait_for_review_run.sh`, and pass only small environment variables from YAML. If extraction is not possible immediately, split the step into:
  1. run discovery / status polling, and
  2. live-log shortcut logic.  
  Externalizing the shell body is the safer long-term fix.
- **Estimated current character count** — 16,626
- **Headroom remaining** — 4,374

_No workflow file in scope exceeded the 800 KB file-size warning threshold._

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — deprecated `caller_workflow` input is still wired internally even though runtime ignores it
- **File path** — `.github/workflows/orchestrate_poll.yml:5-20; .github/workflows/internal-orchestrate-poll.yml:16-18`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `orchestrate_poll.yml` explicitly marks `caller_workflow` as a deprecated no-op whose value is ignored, but the internal wrapper still passes `caller_workflow: internal-orchestrate-poll.yml`. That keeps dead interface surface alive inside this repo and makes future readers think the value still affects poller behavior.
- **Recommended fix** — Stop passing `caller_workflow` from `internal-orchestrate-poll.yml` and keep the input only for external backward compatibility. Add a short inline comment in the wrapper noting that the runtime no longer consumes it.

#### SHELL-001 — `review_apply_fixes.sh` uses `ls` for control flow to pick retry artifacts
- **File path** — `scripts/review_apply_fixes.sh:986-1018`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — The script uses `ls -1 ... | sort -V | tail -n 1` to find the latest `editor_attempt_*` artifacts. `shellcheck` flags this as SC2012, and it is a brittle pattern for control flow under `set -euo pipefail`. The filenames are currently workflow-generated, so the practical risk is low, but this still violates the repo’s shell hygiene bar and makes the selection logic harder to reason about than a direct tracked-path approach.
- **Recommended fix** — Record the latest artifact path in a variable when each attempt finishes, or replace the `ls` pipeline with a `find`/glob-safe approach that does not parse `ls` output.

_No new `TODO` / `FIXME` / `HACK` markers were found in the audited workflow and script files._

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | SEC-001, BATCH-001 |
| Medium | 5 | BUG-001, BUG-002, API-001, DUP-002, EXPR-001 |
| Low | 3 | DUP-001, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 6 | Medium |
| API call optimization | 1 | Medium |
| Code modularization | 9 | Large |
| Expression size reduction | 1 | Small |
| Medium/Low fixes | 5 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-03)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap/deadness is proven within one local execution context and can be implemented directly without changing retry/concurrency semantics. `NEEDS_VERIFICATION` means the overlap looks real, but static reading does not fully prove semantic equivalence or freshness across steps/jobs. `RISKY_SKIP` means the call lives in a retry/race/polling-sensitive path where auto-consolidation must not be applied without manual review.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — `internal-review` can collapse branch-PR detection and default-branch lookup into one metadata read
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/internal-review.yml:98-101`
- **Current call count** — 2
- **Proposed call count** — 1
- **Endpoint(s)** — REST `GET /repos/{repo}/pulls?state=open&head={owner}:{branch}` and REST `GET /repos/{repo}`; proposed single GraphQL repository query for open PR + default branch.
- **Evidence**
```bash
existing_pr="$(gh api \
  "repos/${REPOSITORY}/pulls?state=open&head=${REPOSITORY%/*}:${HEAD_REF}" \
  --jq '[.[] | .number] | first // empty' 2>/dev/null || echo "")"
base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
```
  Both values are only used to decide whether to proceed and which `base_ref_override` to pass to `review_autofix.yml`.
- **Proposed fix** — In the `Resolve PR for head branch` step, replace the two `gh api` calls with one `gh api graphql` query that returns `repository.defaultBranchRef.name` and the first open pull request matching `headRefName=$HEAD_REF`; keep the current REST pair as a fail-open fallback if GraphQL returns null/empty.
- **Safety rationale** — `NEEDS_VERIFICATION` because this changes API surface and filter semantics, so the implement stage must prove GraphQL `pullRequests(... states: OPEN ...)` matches the current REST `head=` behavior for `claude/**` pushes.
- **Downstream signal** — Verify on one `claude/**` push with an open PR and one without that the GraphQL result produces the same `existing_pr`, `base_ref`, and `RESOLVE_CLAUDE_BRANCH_PR_*` branch decision before replacing the REST calls.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — `issue_pr_status` re-reads issue bodies after it already classified managed issues
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/issue_pr_status.yml:295-320`, `.github/workflows/issue_pr_status.yml:327-348`, `.github/workflows/issue_pr_status.yml:503-512`
- **Current call count** — successful batch path: 1 GraphQL batch + up to N later REST issue reads; fallback path: up to N REST classification reads + up to N later REST issue reads
- **Proposed call count** — successful batch path: 1 GraphQL batch only; fallback path: up to N REST classification reads only
- **Endpoint(s)** — GraphQL repository issue alias batch; REST `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
```bash
ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
...
MANAGED_ISSUES="${_managed_issues}"
```

```bash
while IFS= read -r issue_number; do
  [ -n "${issue_number}" ] || continue
  BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
  if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
    IS_ORCHESTRATED="true"
    break
  fi
done <<< "${LINKED_ISSUE_NUMBERS}"
```
  The later alert step recomputes a boolean that the earlier classification step already derived as `MANAGED_ISSUES`.
- **Proposed fix** — In the linked-issue classification step, export either `MANAGED_ISSUES` or a simpler `HAS_ORCHESTRATED_LINKED_ISSUE=true|false` flag to `GITHUB_ENV`; update the later alert step to consume that exported classification instead of re-fetching every linked issue body.
- **Safety rationale** — `NEEDS_VERIFICATION` because the reuse crosses workflow steps rather than staying inside one step, so a human must confirm no intermediate step relies on recomputing orchestration status from live issue bodies.
- **Downstream signal** — Verify that the merged-alert step always runs after the classification step in the same job and that no intervening step mutates the managed-vs-standalone classification before replacing the body-read loop.

#### REUSE-002 — `review_rb_judge.sh` refetches PR title/body even though `PR_META_FILE` is already the script’s local PR snapshot
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `scripts/review_rb_judge.sh:153-156`, `scripts/review_rb_judge.sh:193-199`, `scripts/review_rb_judge.sh:213-214`
- **Current call count** — 1 extra PR REST read on the fallback path
- **Proposed call count** — 0 extra PR REST reads on the fallback path
- **Endpoint(s)** — REST `GET /repos/{repo}/pulls/{pr_number}`
- **Evidence**
```bash
if [ -z "${ISSUE_NUMBERS}" ]; then
  PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
```

```bash
PRELOADED_PR_META="$(jq -c '{
  title: (.title // ""),
  body: (.body // ""),
  head_ref: (.head_ref // .head.ref // .headRefName // ""),
  base_ref: (.base_ref // .base.ref // .baseRefName // ""),
  head_sha: (.head_sha // .head.sha // .headSha // "")
}' "${PR_META_FILE}" 2>/dev/null || echo '{}')"
```

```bash
if [ "${PR_META_JSON}" = "{}" ]; then
  PR_META_JSON="$(jq '.' "${PR_META_FILE}" 2>/dev/null || echo "{}")"
fi
```
  The script already trusts `PR_META_FILE` as its PR metadata source later in the same run.
- **Proposed fix** — In `scripts/review_rb_judge.sh`, derive fallback `PR_DATA` from `PR_META_FILE` first (`jq -r '[.title // "", .body // ""] | join(" ")'`), and only fall back to `_safe_gh_jq pulls/{pr}` if the file is absent or empty.
- **Safety rationale** — `NEEDS_VERIFICATION` because the reusable data is produced by an upstream workflow step, so the implement stage must confirm `PR_META_FILE` is always present and acceptable on every judge entry path.
- **Downstream signal** — Verify all invocations of `review_rb_judge.sh` populate `PR_META_FILE` before script start, including retry/recovery dispatches, then switch the fallback to file-first.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — `COMMITS_AFTER` in the E2E bait-removal check is fetched and never consumed
- **Safety tag** — `SAFE_TO_MERGE`
- **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:1508-1509`
- **Current call count** — 1
- **Proposed call count** — 0
- **Endpoint(s)** — REST `GET /repos/{repo}/commits?sha={branch}&per_page=20`
- **Evidence**
```bash
COMMITS_AFTER=$(gh api "repos/${TEST_REPO}/commits?sha=${BRANCH}&per_page=20" \
  --jq "[.[] | select(.sha != \"${BAIT_SHA}\") | .sha] | length" 2>/dev/null || echo "0")
# The PR head SHA should differ from the bait SHA.
PR_HEAD=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
```
  `COMMITS_AFTER` is assigned but never referenced afterward; the actual gate uses `PR_HEAD`.
- **Proposed fix** — Delete the `COMMITS_AFTER=$(...)` assignment from the `Verify editor removed bait line` step and keep the existing `PR_HEAD != BAIT_SHA` check as the sole commit-presence test.
- **Safety rationale** — `SAFE_TO_MERGE` because the API result is provably unused in the same step and removing it does not alter control flow, retry handling, or log keys.
- **Downstream signal** — Remove `.github/workflows/test-and-mark-stable.yml:1508-1509` directly; no replacement call is needed.

#### DEAD-API-002 — `review_rb_judge.sh` fetches every linked issue body even though only the first body is ever used
- **Safety tag** — `SAFE_TO_MERGE`
- **File path and line ranges** — `scripts/review_rb_judge.sh:161-170`
- **Current call count** — N `GET /issues/{issue_number}` calls for N linked issues
- **Proposed call count** — 1 `GET /issues/{first_issue}` call
- **Endpoint(s)** — REST `GET /repos/{repo}/issues/{issue_number}`
- **Evidence**
```bash
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
  Downstream consumers only reference `FIRST_ISSUE` / `FIRST_ISSUE_BODY`; there is no later use of `BODY` from the second and subsequent iterations.
- **Proposed fix** — In `scripts/review_rb_judge.sh`, select the first non-empty issue number once, fetch that issue’s body once, and remove the per-issue body fetch inside the loop; keep the later `ISSUE_NUMBERS` looping logic for label/close actions unchanged.
- **Safety rationale** — `SAFE_TO_MERGE` because only the first issue body is consumed downstream, the change stays inside one function, and it preserves auth/retry behavior while deleting unused calls.
- **Downstream signal** — Refactor `scripts/review_rb_judge.sh:161-170` so only the first linked issue triggers `_safe_gh_jq "repos/.../issues/{n}"`.

### Cross-References to Deep Audit Section
- API-001: `RISKY_SKIP` — sits in `scripts/orchestrate_poll_process.sh` final-merge/race-handling logic, so auto-consolidation must not bypass its stall-defense semantics.
- BATCH-001: `RISKY_SKIP` — also lives in `scripts/orchestrate_poll_process.sh` and changes an update-branch sweep path that explicitly guards against upstream races and per-PR drift.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 2 | DEAD-API-001, DEAD-API-002 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-001, REUSE-002 |
| RISKY_SKIP | 0 | — |

### Implement-Stage Handoff
- DEAD-API-002
- DEAD-API-001
