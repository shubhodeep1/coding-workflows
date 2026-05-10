## Executive Summary

- **`review_autofix` is the dominant long-tail bottleneck in `shubhodeep1/coding-workflows`.** In the current 1,000-run window it had **134 runs**, **74 successes**, **5 failures**, and **51 cancellations**; average duration was **836.65s** and p95 was **3012.65s**. The worst observed failure, run **`25616314314`**, ran **6469s** before failing in `review / codex-agent / Run Codex resolver, validate, stage, commit`. **Estimated impact:** cutting the resolver timeout path and review-only overwork should reduce `review_autofix` p95 by roughly **15–30%** and save **30–50 minutes** on pathological runs. **Confidence:** **high**.

- **Current CI failures are highly concentrated and mostly self-inflicted by a contract drift.** In `ci`, **7 of 9 failures (77.8%)** were `lint / Review Semble contract test` on runs **`25628658712`**, **`25628703787`**, **`25630232392`**, **`25630365046`**, **`25630494983`**, **`25630670009`**, and **`25630678102`**. The failing assertion is in `tests/test_review_semble_contract.py:36`, while `.github/workflows/review_autofix.yml:154` currently sets `SEMBLE_ENABLED` with a different default. **Estimated impact:** resolving that drift could remove **most current CI failures** and, if the test is moved earlier, save about **9–10 minutes per bad CI revision**. **Confidence:** **high**.

- **The prompt-cache probe is costing model calls without yielding usable cache data.** `scripts/review_run_reviewers.sh:118-160` makes **2 extra `codex exec` calls** before reviewer fan-out, but sampled `review_autofix` runs **`25620282902`**, **`25617161072`**, and **`25616314314`** all logged `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. **Estimated impact:** moderate recurring token and latency savings across every heavy review run if the probe is disabled or heavily sampled. **Confidence:** **high** on waste, **medium** on absolute dollar savings because aggregate token totals are unavailable.

- **Review-path AI memory retrieval is active but not effective.** The embedded audit in `workflow_log_analysis` run **`25621103657`** reported **20 retrieves**, only **2 hits (10.0%)**, average `estimated_tokens` **2.8**, and `keyword_method=none` **18** times. Direct raw logs in `review_autofix` runs **`25616314314`**, **`25620282902`**, and **`25617161072`** all show reviewer retrieves with `records_selected: 0`. **Estimated impact:** medium quality improvement and small cost reduction if reviewer retrieval inputs are enriched and telemetry coverage is tightened. **Confidence:** **high**.

- **Orchestrator correctness still has a real failure mode around oversized state and weak post-merge handoff.** The embedded audit in run **`25621103657`** states that a state comment above GitHub’s **65,536-byte** limit caused stale orchestrator state on tracking issue **`#2373`** and duplicate issue creation. Separately, recent `review_autofix` run **`25631578938`** found **40** linked issues via fallback but still warned `No standalone validation workflow could be dispatched for merged PR #2450.` **Estimated impact:** high reliability gain and reduced duplicate-work churn if state writes are byte-budgeted and validate-dispatch success is made observable/retriable. **Confidence:** **high**.

## Speed Optimizations

Ranked by expected latency reduction. Critical-path wins come first; micro-optimizations are last.

### 1) Add a budgeted watchdog to the merge-conflict resolver  
**Type:** Critical-path win

- **Evidence:** `shubhodeep1/coding-workflows` run **`25616314314`** failed after **6469s** in `Internal: AI Review & Autofix` → `review / codex-agent` → `Run Codex resolver, validate, stage, commit`. The raw log shows `DID_COMMIT: true`, `LEDGER_ONLY_COMMIT: true`, `EDITOR_CHANGES_LOST: true`, `MERGE_CONFLICT: true`, `CONFLICT_RESOLVED: false`. The workflow step itself is capped only by `.github/workflows/review_autofix.yml:3757-3795`, which sets `timeout-minutes: 60`. By contrast, the editor path already has a progress-aware watchdog in `scripts/review_apply_fixes.sh:956-1065`, while `scripts/review_conflict_resolve.sh:573-610` enters its retry loop without an equivalent per-attempt wall/idle guard.

- **Root cause:** The conflict resolver has retry logic, but no editor-style watchdog to terminate a hung or non-progressing `codex exec` attempt early.

- **Exact change:** Port the editor watchdog pattern into `scripts/review_conflict_resolve.sh`:
  - per-attempt wall-clock cap,
  - idle/heartbeat timeout,
  - PR-closed check,
  - two-strike no-progress abort,
  - keep the existing 60-minute workflow timeout as a final backstop.

- **Estimated time savings (inference):** **30–50 minutes** on pathological conflict runs; meaningful reduction to the `review_autofix` p95 tail.

- **Implementation risk:** **Low-medium.** The change is behavior-preserving for healthy runs because the 60-minute outer timeout remains in place.

### 2) Reduce work in `claude-branch-review` / comment-only review mode  
**Type:** Critical-path win

- **Evidence:** Recent `review_autofix` summaries show long runs even when the pipeline explicitly skipped editor/commit/judge/auto-merge:
  - **`25630994302`** — success, **1453s**, `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`
  - **`25630951946`** — cancelled, **1739s**, same path
  - **`25630795512`** — cancelled, **575s**, same path  
  A slow successful deep-dive run, **`25621222522`** (duration **5616s**), logged `MODEL_EDITOR: openai/gpt-5.4`, `REVIEWER_REASONING_EFFORT: xhigh`, and repeated `REVIEWERS_SUCCESSFUL: 6`. Workflow defaults in `.github/workflows/review_autofix.yml:99,103,113-125` also set `openai/gpt-5.4` and `xhigh` reasoning by default.

- **Root cause:** The expensive six-reviewer/xhigh reasoning policy is being reused on a comment-only path that does not proceed to editor/commit/judge/auto-merge.

- **Exact change:** When `CLAUDE_BRANCH_REVIEW_MODE=true` or the gate has already decided the run is comment-only:
  - lower pass-2 reviewer reasoning,
  - and/or run a smaller reviewer subset,
  - keep the full policy only for merge-blocking/editor paths.

- **Estimated time savings (inference):** **20–40%** on these comment-only review runs, which is roughly **3–12 minutes** on the observed **575–1739s** cases.

- **Implementation risk:** **Medium.** Safe if scoped strictly to the non-blocking comment-only path.

### 3) Move `Review Semble contract test` to the front of CI  
**Type:** Critical-path win for failing CI revisions

- **Evidence:** In `.github/workflows/ci.yml:111-185`, `Review Semble contract test` runs at lines **174-177**, after long-running test steps such as:
  - `Orchestrate poll process unit tests` at **116-119**
  - `Targeted file context contract tests` at **168-172**  
  In failing run **`25630232392`**:
  - `step-004-lint_Orchestrate_poll_process_unit_tests.log` ran from **13:41:19** to **13:50:36** (~**557s**)
  - `step-013-lint_Targeted_file_context_contract_tests.log` ran from **13:50:45** to **13:51:15** (~**30s**)
  - `step-015-lint_Review_Semble_contract_test.log` then failed almost immediately, from **13:51:15.556** to **13:51:15.590**, on an `AssertionError`  
  This same failure step appears in **7** CI failures in the current window.

- **Root cause:** A deterministic contract mismatch is being detected at the end of a serial CI job.

- **Exact change:** Move `Review Semble contract test` and the closely related Semble contract steps immediately after Python setup and syntax checks, or split them into a lightweight early-fail job.

- **Estimated time savings (inference):** roughly **9–10 minutes** per bad CI revision.

- **Implementation risk:** **Low.**

### 4) Shorten `review_autofix` check-run polling before building autofix context  
**Type:** Critical-path win

- **Evidence:** In `review_autofix` run **`25620282902`**, `review / codex-agent / Collect PR check-run failures (CI/lint autofix context)` waited on the same SHA from **04:59:54** to **05:01:36**, logging six repeated messages like `Waiting for 1 in-progress/queued check-run(s) ... (sleep 20s, deadline in 1199s)…`. The polling loop is in `.github/workflows/review_autofix.yml:1810-1854`, with defaults set at **1200s** timeout and **20s** poll interval in lines **1785-1792**.

- **Root cause:** The step waits for all in-flight checks to settle, even when the count is stable and the autofix run is already expensive.

- **Exact change:** Keep fail-open behavior, but:
  - stop after two unchanged polls,
  - or stop once at least one failing check is already present,
  - and reduce the default timeout for this context-only step.

- **Estimated time savings (inference):** **1–2 minutes** on affected `review_autofix` runs.

- **Implementation risk:** **Low-medium.** The step already degrades gracefully by writing a sentinel context on failure.

### 5) Merge or conditionalize the extra Copilot artifact-cleanup job  
**Type:** Micro-optimization with some queue-time upside

- **Evidence:** Recent `copilot_pull_request_reviewer` runs **`25631579899`**, **`25631587881`**, **`25631602343`**, and **`25631604780`** all flagged artifact cleanup and artifact API access in `log_summary`. Raw logs for **`25631604780`** confirm:
  - a list call to `/repos/shubhodeep1/coding-workflows/actions/runs/25631604780/artifacts`
  - a delete call to `/repos/shubhodeep1/coding-workflows/actions/artifacts/"$artifact_id"`  
  The raw cleanup API work itself was short (~1s), so the bigger opportunity is likely the **extra job / runner pickup exposure**, not the HTTP time.

- **Root cause:** An extra job exists mainly to enumerate and delete artifacts, even when only one small artifact is present.

- **Exact change:** Either:
  - delete the known uploaded artifact from the existing results job, or
  - pass the artifact ID forward so the cleanup job can skip the list call, and avoid the cleanup job entirely for the single-artifact case.

- **Estimated time savings (inference):** typically **10–60s** end-to-end when a separate runner pickup is avoided.

- **Implementation risk:** **Low.**

### 6) Skip default-branch lookup when `resolve-claude-branch-pr` already found the PR  
**Type:** Micro-optimization

- **Evidence:** Recent run **`25631603346`** logged both:
  - `gh api "repos/${REPOSITORY}/pulls?state=open&head=..."`
  - `gh api "repos/${REPOSITORY}" --jq '.default_branch'`  
  and then immediately emitted `RESOLVE_CLAUDE_BRANCH_PR_SKIP ... existing_pr=2449 reason=pull_request_synchronize_handles`.

- **Root cause:** The default-branch lookup runs even when the open PR lookup was sufficient to decide the path.

- **Exact change:** Defer the default-branch lookup until:
  - no existing PR is found, and
  - no caller-supplied base ref is available.

- **Estimated time savings (inference):** **1 API call** and sub-second latency per run on this path.

- **Implementation risk:** **Very low.**

## Cost Optimizations

Window-wide prompt/completion totals are **not reliably available** from the current collector sample, so this section ranks opportunities by observed waste patterns rather than exact dollar totals.

### 1) Disable or heavily sample the two-call prompt-cache probe until it emits real cache counters

- **Evidence:** `scripts/review_run_reviewers.sh:118-160` defines `run_cache_probe()` and makes **two** `codex exec` calls before the reviewer fan-out. Direct logs from `review_autofix` runs **`25620282902`**, **`25617161072`**, and **`25616314314`** show both calls emitted only:
  - `prompt_tokens=na`
  - `completion_tokens=na`
  - `total_tokens=na`
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`  
  Also, the script chooses only the **first reviewer model** for the probe (`probe_model=... | head -n1` at line **120**), so even a working probe would not describe the full panel.

- **Root cause:** A synthetic cache-measurement path is adding model calls without producing usable observability.

- **Exact change:** Disable the probe by default, or sample it very lightly. If cache measurement is still needed, emit real provider cache counters from the actual reviewer/editor calls instead of a synthetic probe.

- **Estimated savings (inference):** **2 model invocations per `review_autofix` reviewer step**, plus the corresponding latency. Exact token/dollar savings cannot be quantified from the current logs.

- **Quality-risk notes:** **Low.** The probe is observability-only and already fail-open.

### 2) Stop paying for repeated no-progress retries in editor/implement paths

- **Evidence:** In direct raw logs for `review_autofix` run **`25617161072`**, the pipeline reached `REVIEWERS_SUCCESSFUL: 6` repeatedly and then logged `Editor summary file is missing or empty — editor never produced output.` The embedded audit in `workflow_log_analysis` run **`25621103657`** reported the same pattern in additional `review_autofix` failures (**`25613657806`**, **`25615068886`**, **`25613659201`**) and two `implement` failures:
  - **`25614767039`** — five consecutive “Codex returned output but produced no file changes” attempts, with **`42,103` tokens** on attempt 5
  - **`25615174460`** — same repeated no-diff pattern

- **Root cause:** Retry policy assumes later attempts may become productive even after repeated identical “no summary” or “no file changes” signatures.

- **Exact change:** Add a two-strike no-progress guard:
  - if the same head/input hash produces two consecutive no-summary or no-diff attempts, stop retrying,
  - and jump to the diagnose/failure-comment path.
  - On `review_autofix`, if reviewers already succeeded and only the editor failed, prefer reusing the existing reviewer consensus instead of paying the reviewer panel again on rerun.

- **Estimated savings (inference):** roughly **60–80%** of token spend on affected failure cases, plus **5–20 minutes** saved on those runs.

- **Quality-risk notes:** **Low-medium.** Scope the guard to identical inputs and repeated no-progress signatures only.

### 3) Lower reasoning cost on comment-only review paths

- **Evidence:** `review_autofix` defaults remain expensive:
  - `.github/workflows/review_autofix.yml:99` — `MODEL_EDITOR: openai/gpt-5.4`
  - `:103` — `REVIEWER_REASONING_EFFORT: xhigh`
  - `:113-114` — both reviewer pass-2 reasoning defaults are also `xhigh`
  - `:125` — `EDITOR_REASONING_EFFORT: xhigh`  
  Yet recent comment-only runs **`25630994302`**, **`25630951946`**, and **`25630795512`** skipped editor/commit/judge/auto-merge and still ran for **575–1739s**.

- **Root cause:** A high-cost reasoning profile is being applied even where the workflow only needs a review comment, not an automated fix/merge decision.

- **Exact change:** On comment-only `claude-branch-review` paths:
  - lower reviewer pass-2 reasoning,
  - and/or reduce the reviewer panel,
  - leaving the current profile intact for fix-producing paths.

- **Estimated savings (inference):** **20–40%** token and runtime reduction on comment-only review runs.

- **Quality-risk notes:** **Medium.** Contain the change to non-blocking review paths only.

### 4) Treat Semble as either a real prompt reducer or a strict no-op, not a half-enabled feature

- **Evidence:** Recent `orchestrate_poll` run **`25630962603`** logged:
  - `SEMBLE_ENABLED: true`
  - `SEMBLE_AVAILABLE: false`
  - `SEMBLE_INDEX_AVAILABLE: false`  
  so Semble did **not** reduce prompt expansion on that sampled production run. Separately, the embedded reviewer findings recorded in slow `review_autofix` run **`25617161072`** repeatedly flagged that `build_judge_semble_prefetch()` and `build_rb_judge_semble_prefetch()` still do unnecessary prep work when Semble is unavailable.  
  The only repeated structured Semble events directly observed in raw logs were **`SEMBLE_FALLBACK`** lines in CI contract tests, not production `SEMBLE_QUERY ... bytes=` lines, so current evidence shows **fail-open coverage**, but **not measured production prompt-compression benefit**.

- **Root cause:** Semble availability is inconsistent, and some helper logic still executes even when Semble cannot be used.

- **Exact change:** 
  - short-circuit prefetch/query-building helpers before stdin-drain or Python work when `SEMBLE_AVAILABLE!=true` or `SEMBLE_INDEX_AVAILABLE!=true`,
  - separately fix the workflow/contract drift so the paths that should use Semble can actually do so.

- **Estimated savings (inference):** **Low-moderate** recurring savings on orchestrate/judge/review paths when Semble is unavailable. Actual byte/token reduction is **not measurable** from the current sample.

- **Quality-risk notes:** **Low** if fail-open behavior is preserved.

### Note on repeated prompt/context expansion

The repository is already trying to preserve a stable cached prefix via `pre_assembled_static.txt`, but the current logs do not expose real reviewer/editor cache hit rates. The current synthetic probe cannot tell whether prompt-prefix caching is working or whether prompt variance is fragmenting the cache. The next safe measurement step is to log:
- a stable prefix hash,
- real cache creation/read counters from live calls,
- and whether volatile sections were appended after the shared prefix.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1) Resolve the `review_autofix` Semble contract drift that is driving most CI failures

- **Failure evidence:** In the current window, **7 of 9** `ci` failures hit `lint / Review Semble contract test`. Raw run **`25630232392`** failed on `tests/test_review_semble_contract.py:36`, which asserts `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}`. The workflow currently contains `SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'true' }}` at `.github/workflows/review_autofix.yml:154`.

- **Root cause category:** Workflow/test contract drift.

- **Exact fix:** Decide the intended default once, then update the workflow and contract tests to share a single source of truth. Because the same contract test also runs in `.github/workflows/mark-stable.yml` and `.github/workflows/test-and-mark-stable.yml`, align all three callers at the same time.

- **Expected reliability impact:** Could remove **77.8% of current CI failures**.

- **Rollback / fail-open considerations:** No fail-open needed. Keep the contract test active after alignment so this drift cannot recur silently.

### 2) Make unresolved-placeholder detection a mandatory preflight before any model call

- **Failure evidence:** `review_autofix` runs **`25620282902`** and **`25619919130`** failed in `Run reviewer models` on `Unresolved WORKFLOW_EDIT_RESTRICTION placeholder in rendered output for ... reviewer_prompt_body.txt`.

- **Root cause category:** Prompt rendering validation happens too late.

- **Exact fix:** Add a shared placeholder assertion helper and run it immediately:
  - after every prompt render,
  - before reviewer/editor/implement model invocation,
  - and before any prompt file is handed to `codex exec`.

- **Expected reliability impact:** Converts several-minute failures into immediate deterministic failures and reduces noisy reruns.

- **Rollback / fail-open considerations:** Safe to gate behind an env override initially if you want a short rollout window.

### 3) Add the same watchdog discipline to the conflict resolver that the editor already has

- **Failure evidence:** Run **`25616314314`** failed after the full **60-minute** step timeout in `Run Codex resolver, validate, stage, commit`. The embedded audit in `workflow_log_analysis` run **`25621103657`** also flagged a second 60-minute timeout case (`25613903546`).

- **Root cause category:** Missing watchdog / no-progress guard around resolver execution.

- **Exact fix:** Reuse the editor watchdog pattern in `scripts/review_conflict_resolve.sh`.

- **Expected reliability impact:** Reduces hard timeouts, makes failures more diagnosable, and lowers rerun pressure on conflict-heavy PRs.

- **Rollback / fail-open considerations:** Keep the current 60-minute workflow timeout as the final backstop.

### 4) Add a two-strike no-progress guard for editor/implement retries

- **Failure evidence:** Direct raw logs in run **`25617161072`** show six successful reviewers followed by an empty editor result. Embedded audit evidence in run **`25621103657`** shows similar repeated no-progress patterns in multiple `review_autofix` failures and in `implement` runs **`25615174460`** and **`25614767039`**.

- **Root cause category:** Repeated non-durable model output.

- **Exact fix:** After two consecutive no-summary or no-diff attempts on the same input state, stop retrying and move to the diagnose/failure-comment branch.

- **Expected reliability impact:** Fewer misleading reruns, clearer operator signals, less wasted model work before a terminal failure.

- **Rollback / fail-open considerations:** Make the threshold configurable if you want a conservative rollout.

### 5) Put a byte-budget guard in front of orchestrator state writes

- **Failure evidence:** The embedded audit in **`25621103657`** says that a state snapshot above GitHub’s **65,536-byte** comment limit silently no-op’d, which then re-advanced stale orchestrator state and created duplicate issues from tracking issue **`#2373`**.

- **Root cause category:** Oversized state persistence with silent write failure.

- **Exact fix:** Measure serialized state size before write, compact before crossing a safe threshold, and log the byte size plus write result every time.

- **Expected reliability impact:** High for orchestrator correctness; prevents stale-wave duplicate issue creation.

- **Rollback / fail-open considerations:** If compaction fails, do not advance state; emit an explicit warning instead.

### Note on `SEMBLE_FALLBACK`: this looks healthy in CI, not like a broken rollout

- **Observed pattern:** In the five deep-dive CI runs that retained `step-013` logs — **`25630232392`**, **`25630365046`**, **`25630494983`**, **`25630670009`**, and **`25630678102`** — `lint / Targeted file context contract tests` emitted **5 `SEMBLE_FALLBACK` lines per run** (**25 total**):
  - 4 for `target=overflow file=src/big.py`
  - 1 for `target=overflow file=src/small.py`
  - all with `reason=[Errno 2] ... missing_semble`
  - all with `ms=0`

- **Interpretation:** This is a **healthy fail-open contract-test path** with intentionally missing binaries. It is **not** the direct cause of those CI failures; the sampled runs went on to fail later in `Review Semble contract test`.

- **Smallest safe mitigation:** Keep fallback behavior unchanged. Optimize diagnosis by:
  - moving the Semble contract steps earlier,
  - and adding a per-step fallback counter to production logs so test-only fallbacks are easy to distinguish from rollout regressions.

- **Data gap:** Two older CI failures at `Orchestrate poll process unit tests` (**`25619909717`**, **`25620680564`**) retained only aggregate `step-001-lint.log` in this collector snapshot, so their exact failing assertion is not recoverable here. The next collection improvement should preserve full per-step logs for older CI failures.

## AI Memory Health

Deep-dive logs **did** contain `AI_MEMORY_TELEMETRY:` lines.

| Memory metric | Observed value | Evidence |
|---|---:|---|
| Retrieve operations | 20 | Embedded audit in `workflow_log_analysis` run `25621103657` |
| Retrieve hits (`records_selected > 0`) | 2 / 20 | Same |
| Retrieve hit rate | 10.0% | Same |
| Average `estimated_tokens` | 2.8 | Same |
| Min / max `estimated_tokens` | 0 / 28 | Same |
| `keyword_method=none` | 18 | Same |
| `keyword_method=plain` | 2 | Same |
| `keyword_method=llm` | 0 | Same |

### What the telemetry says

- **Reviewer retrieval is effectively not helping today.** Direct raw logs from `review_autofix` runs **`25616314314`**, **`25620282902`**, and **`25617161072`** all show reviewer retrieves with:
  - `enabled: true`
  - `records_selected: 0`
  - `estimated_tokens: 0`
  - `keyword_method: none`

- **The only observed hits were on the implement path.** The embedded audit in **`25621103657`** reports successful retrieves only for `implement` runs **`25615174460`** and **`25614767039`**, both with:
  - `role=implementation`
  - `records_selected=1`
  - `estimated_tokens=28`
  - `keyword_method=plain`

- **Push/write health looks good in the sampled logs.** Observed `record-run-event` and `record-candidate` operations in the reviewed raw logs all had `push_attempts: 1`.

- **I did not find sampled retrieve entries with `fail_open: true` or `enabled: false`.** That is good, but it should be interpreted narrowly: it means they were not present in the inspected deep-dive logs, not that they never happen.

### Recommended actions

1. **Enrich reviewer retrieval inputs** to look more like the working implement path. Right now `.github/workflows/review_autofix.yml:1325-1335` passes only `--role reviewer --pr-number`, while `.github/workflows/implement.yml:946-957` passes issue number, issue title, and issue body. Add PR title/body, linked issue numbers/titles, and a changed-files summary to the reviewer retrieval call.

2. **Track reviewer and implementation hit rates separately.** The combined **10.0%** hit rate hides the fact that the reviewer path appears near-zero while implement has the only successful hits.

3. **Verify intended telemetry coverage on success paths.** Recent summaries for runs such as `review_autofix` **`25631570869`** and `orchestrate_poll` **`25631497379`** reported no `AI_MEMORY_TELEMETRY` lines. Those runs may not have exercised memory-heavy steps, but if broader telemetry coverage is intended, verify emission explicitly.

## GH API Call Audit

This repo already has some explicit API hygiene patterns — for example, `scripts/orchestrate_poll_process.sh:6835-6849` caches PR JSON specifically to avoid duplicate `gh api` calls. The biggest opportunity is to copy that discipline into other hot paths.

### 1) Highest redundancy: `review_autofix` check-run polling

- **Workflow / step:** `Internal: AI Review & Autofix` → `review / codex-agent` → `Collect PR check-run failures (CI/lint autofix context)`
- **Observed pattern:** repeated `GET repos/{repo}/commits/{HEAD_SHA}/check-runs?per_page=100`
- **Evidence:** Run **`25620282902`** polled the same head SHA every **20s** for about **123s** before proceeding.
- **Concrete batching/reuse change:** stop after unchanged polls, cache the last result, and reuse the final snapshot instead of polling until all in-flight checks settle.
- **Estimated call-count reduction (inference):** from **6+** calls to **2–3** on affected runs.
- **Rate-limit risk reduction:** moderate.

### 2) Unbatched per-issue fetches in the linked-issue fallback path

- **Workflow / step:** `Internal: AI Review & Autofix` → `review / codex-agent` → `Collect PR metadata`
- **Observed pattern:** one GraphQL `closingIssuesReferences` call, then up to **20** REST calls to `repos/{repo}/issues/{n}` in a loop when GraphQL returns empty (`.github/workflows/review_autofix.yml:1510-1549`).
- **Evidence:** The code explicitly says each distinct fallback issue costs one REST call. Recent run **`25631578938`** later logged `Found linked issues via PR body/title fallback: 40`, showing this path can become large on orchestrator-managed PRs.
- **Concrete batching/reuse change:** keep the first GraphQL call, but batch fallback issue body/title fetches into one aliased GraphQL request instead of N REST calls.
- **Estimated call-count reduction (inference):** from **1 + N** to roughly **2** total fetches on the metadata path.
- **Rate-limit risk reduction:** high on fallback-heavy PRs.

### 3) Redundant branch metadata lookup in `resolve-claude-branch-pr`

- **Workflow / step:** `Internal: AI Review & Autofix` → `resolve-claude-branch-pr`
- **Observed pattern:** open-PR lookup plus unconditional default-branch lookup
- **Evidence:** Recent run **`25631603346`** performed both API calls and then immediately skipped because it had already found the existing PR.
- **Concrete batching/reuse change:** defer default-branch lookup until the open-PR lookup returns empty.
- **Estimated call-count reduction (inference):** **1 API call per run** on this path.
- **Rate-limit risk reduction:** low but free.

### 4) Artifact lifecycle cleanup in Copilot reviewer runs is over-separated

- **Workflow / step:** `Copilot code review` → `Cleanup artifacts`
- **Observed pattern:** one list-artifacts API call, then per-artifact delete calls in a separate cleanup job
- **Evidence:** Recent runs **`25631579899`**, **`25631587881`**, **`25631602343`**, and **`25631604780`** all surfaced the artifacts endpoint as a hotspot in summaries; raw run **`25631604780`** confirms the list and delete sequence.
- **Concrete batching/reuse change:** reuse the uploaded artifact ID directly and, where possible, delete in the existing job instead of paying a separate cleanup job/runner.
- **Estimated call-count reduction (inference):** modest API savings, but the bigger gain is fewer queued jobs.
- **Rate-limit risk reduction:** low-to-moderate.

### 5) Review the fallback-driven validate-dispatch path for fan-out behavior

- **Workflow / step:** `Internal: AI Review & Autofix` → `review / post-merge-validate-dispatch / Dispatch standalone validate for orchestrator short-circuit issues`
- **Observed pattern:** GraphQL lookup plus workflow dispatch attempts on a broad fallback issue set
- **Evidence:** Recent run **`25631578938`** logged `Found linked issues via PR body/title fallback: 40` and still ended with `No standalone validation workflow could be dispatched for merged PR #2450.`
- **Concrete batching/reuse change:** cap the fallback candidate set earlier, stop after the first successful dispatch when one is enough, and log the number of dispatch attempts explicitly.
- **Estimated call-count reduction (inference):** potentially large on fallback-heavy PRs.
- **Rate-limit risk reduction:** moderate.

## Prompt Cache & Memory System

### Current state

- **Prompt cache observability is not actionable yet.** The repo has a dedicated synthetic probe in `scripts/review_run_reviewers.sh:118-160`, but sampled runs **`25620282902`**, **`25617161072`**, and **`25616314314`** only emitted `na` values for prompt/cache counters.
- **The system is already trying to build a stable prefix.** Both `review_autofix` and `implement` build `pre_assembled_static.txt`, which is the right direction for prefix caching.
- **Memory retrieval effectiveness is uneven by workflow.** Reviewer retrieves are near-zero hit; implement retrieves are the only observed successful ones.

### Likely cache-fragmentation causes  
These are **inferences** based on the workflow design, because real cache hit/miss counters are not currently available.

1. **Synthetic probe ≠ live prompt behavior.** The probe only uses the first reviewer model and a tiny synthetic prompt, so it cannot tell you whether real reviewer/editor calls are benefiting from shared prefixes.

2. **Dynamic context likely dominates variance after the shared prefix.** Review runs append PR comments, linked-issue context, check-run snapshots, Semble context, and memory context. That is normal, but without live cache counters you cannot tell whether unstable content is leaking into the prefix boundary.

3. **Success-path cache accounting is missing.** Because the only visible cache metrics today come from the synthetic probe — and those are all `na` — current logs cannot distinguish a good cache from a fragmented one.

### Recommendations

1. **Replace the synthetic probe with real-call observability.**
   - Log cache creation/read counters from actual reviewer/editor/provider responses.
   - Add a `sha256` of the stable prompt prefix so you can correlate reuse without storing prompt text.

2. **Keep volatile content strictly after the shared prefix.**
   - This includes check-run snapshots, linked issue bodies, dynamic warnings, and any temporary-path noise.
   - This is a design recommendation, not a measured failure.

3. **Make reviewer memory retrieval richer, not larger.**
   - The implement path shows that small, targeted memory retrieval can work.
   - Reuse that pattern for reviewers rather than simply raising token budgets.

4. **Preserve fail-open behavior.**
   - Memory retrieval is already `fail-open`.
   - Prompt-cache measurement should also remain non-blocking if you reintroduce it.

### Estimated impact

- **Tokens / latency:** moderate if the probe is removed and live cache metrics let you stabilize the prefix properly.
- **Reliability:** moderate because better telemetry will make cache regressions diagnosable instead of invisible.

## Orchestrator Health

### What looks healthy

- **Clarify / plan / respond are not the current problem.** In the current window:
  - `clarify` p50 = **1s**, p95 = **2s**
  - `plan` p50 = **1s**, p95 = **2s**
  - `orchestrate_clarify_respond` p50 = **1s**, p95 = **2s**
- **`orchestrate_poll` is succeeding.** It had **21/21 successes**, avg **160.19s**, p50 **103s**, p95 **209s**.

### Recurring pain points

1. **State writer can silently fail when payloads exceed GitHub comment size limits.**
   - Embedded audit in **`25621103657`** reports a stale state write above **65,536 bytes** caused duplicate issues from tracking issue **`#2373`**.
   - This is the highest-severity orchestrator health problem in the sample.

2. **Post-merge validation dispatch is fragile.**
   - Recent `review_autofix` run **`25631578938`** found **40** linked issues via fallback, issued `gh api graphql` / `gh workflow run` activity, and still finished with `No standalone validation workflow could be dispatched for merged PR #2450.`

3. **Forward-merge is safe but conflict-heavy.**
   - Recent runs **`25631570902`** and **`25631578918`** both opened fallback PRs because `STATUS="conflict"`.
   - This is a healthy degradation path, but it is still operational overhead.

4. **Semble is often configured but unavailable in orchestrator flows.**
   - Run **`25630962603`** logged `SEMBLE_ENABLED: true`, `SEMBLE_AVAILABLE: false`, `SEMBLE_INDEX_AVAILABLE: false`.
   - That means the orchestration path often pays the complexity cost of Semble wiring without getting the context benefit.

### Smallest safe mitigations

- **Preflight state size before every orchestrator write** and compact before write, not after failure.
- **Emit explicit dispatch outcome counters** for post-merge validation:
  - candidate issues found,
  - dispatch attempts,
  - successful dispatches,
  - label-removal success/failure.
- **Track forward-merge fallback PR rate** as an operational KPI rather than treating fallback PR creation as “success and done.”
- **Short-circuit Semble prefetch builders** whenever availability/index flags are false.

### Observable indicators to track

- `% orchestrator state writes above 60 KB`
- `% state writes compacted`
- duplicate issues created per tracking issue / wave
- `% post-merge validate dispatches that successfully start a workflow`
- `% `orchestrate_poll` runs with `SEMBLE_AVAILABLE=true``
- forward-merge fallback PR count per week

## Pipeline Flow Bottlenecks

| Stage | Current observed behavior | Dominant overhead type | Evidence | Ordered fix |
|---|---|---|---|---|
| Clarify → Plan → Respond | Mostly skipped, usually **1s** | None / negligible | `clarify`, `plan`, `orchestrate_clarify_respond` p50 all ~1s | No action needed now |
| Implement | Usually skipped, but expensive when the model loops without producing a diff | Retry / token waste | Embedded audit in `25621103657` cites `implement` runs `25615174460`, `25614767039`; one reached **42,103** tokens on attempt 5 | Add two-strike no-progress guard |
| Review / Autofix | Main end-to-end bottleneck; huge long tail; many cancellations | Compute + retry + merge/conflict | `review_autofix` avg **836.65s**, p95 **3012.65s**, **51 cancellations**; worst failure `25616314314` ran **6469s** | Add resolver watchdog, slim comment-only mode, shorten check-run wait |
| Validate / Post-merge handoff | Sometimes no-ops after broad fallback issue discovery | GH API + orchestration handoff | `25631578938` found **40** fallback issues but dispatched no standalone validate workflow | Cap/track fallback fan-out and make dispatch success explicit |
| Orchestrate poll | Healthy success rate, but Semble often unavailable and runner wait is common | Queueing + reduced context quality | `25630962603` success in **90s** with `SEMBLE_AVAILABLE=false`; many recent summaries mention runner wait | Short-circuit Semble when unavailable; reduce extra jobs |
| CI | Long, steady serial runtime; late-fail pattern wastes most of the job | Serial compute + late failure | `ci` avg **645.15s**; failing run `25630232392` spent ~**557s** before a near-instant Semble contract failure | Move Semble contract tests to the front |

### Dominant end-to-end bottlenecks, in order

1. **`review_autofix` compute/retry tail**
2. **Late-failing CI contract tests**
3. **Queueing overhead from extra jobs**  
   Repeated `Job is waiting for a hosted runner to come online.` messages appear across recent `review_autofix`, `copilot_pull_request_reviewer`, `issue_pr_status`, `cancel_on_pr_close`, `forward_merge`, `orchestrate_poll`, and `ci` summaries. Exact queue-time totals are not available in the current collector output, but the pattern is broad.
4. **Post-merge validate / forward-merge operational overhead**
5. **Implement retry loops when the model produces no durable diff**

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long tail: avg **836.65s**, p95 **3012.65s**, **51 cancellations / 134 runs**
- `ci` steady 10–12 minute runtime with late Semble contract failures
- Post-merge orchestrator / validation handoff that sometimes finds many candidate issues but dispatches nothing

**Top failure modes**
- Repeated `ci / lint / Review Semble contract test` failures caused by workflow/test contract drift
- `review_autofix` unresolved prompt placeholder failures in `Run reviewer models` (`25620282902`, `25619919130`)
- `review_autofix` resolver timeout / conflict-path failures (`25616314314`)
- Residual `ci / lint / Orchestrate poll process unit tests` failures (`25619909717`, `25620680564`) with incomplete retained detail in this collector snapshot

**Highest-cost drivers**
- Six-reviewer `review_autofix` fan-out with `xhigh` reasoning even on comment-only paths
- Two extra prompt-cache probe calls per heavy review run with unusable metrics
- Repeated no-progress retries in editor/implement paths
- Queue exposure from separate cleanup and helper jobs

**Top 3 prioritized actions**
1. **Fix the `review_autofix` Semble contract drift and move the Semble contract tests to the front of CI.**
2. **Disable/sample the prompt-cache probe and add two-strike no-progress guards to editor/implement/reviewer follow-on loops.**
3. **Add an editor-style watchdog to the merge-conflict resolver and reduce reviewer cost on comment-only `claude-branch-review` runs.**

## Metrics Appendix

### Run summary

| Scope | Total runs | Success | Failure | Cancelled | Other / skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo: `shubhodeep1/coding-workflows` | 1000 | 249 | 15 | 52 | 684 | 1.5% | 182.71 | 1.0 | 1045.0 |
| `review_autofix` | 134 | 74 | 5 | 51 | 4 | 3.73% | 836.65 | 63.5 | 3012.65 |
| `ci` | 48 | 39 | 9 | 0 | 0 | 18.75% | 645.15 | 646.5 | 722.65 |
| `copilot_pull_request_reviewer` | 38 | 38 | 0 | 0 | 0 | 0.0% | 214.71 | 230.5 | 291.75 |
| `orchestrate_poll` | 21 | 21 | 0 | 0 | 0 | 0.0% | 160.19 | 103.0 | 209.0 |
| `clarify` | 181 | 6 | 0 | 0 | 175 | 0.0% | 4.46 | 1.0 | 2.0 |
| `plan` | 173 | 5 | 0 | 0 | 168 | 0.0% | 6.15 | 1.0 | 2.0 |
| `implement` | 173 | 8 | 0 | 1 | 164 | 0.0% | 14.35 | 1.0 | 8.0 |

### Failure concentration

| Workflow family | Failing job / step | Count | Example run IDs |
|---|---|---:|---|
| `ci` | `lint / Review Semble contract test` | 7 | `25628658712`, `25628703787`, `25630232392`, `25630365046`, `25630494983`, `25630670009`, `25630678102` |
| `ci` | `lint / Orchestrate poll process unit tests` | 2 | `25619909717`, `25620680564` |
| `review_autofix` | `review / codex-agent / Apply fixes with editor model` | 2 | `25617161072`, `25616324582` |
| `review_autofix` | `review / codex-agent / Run reviewer models` | 2 | `25620282902`, `25619919130` |
| `review_autofix` | `review / codex-agent / Run Codex resolver, validate, stage, commit` | 1 | `25616314314` |
| `nightly_validation_selftest` | `validation-selftest / Run validation self-test matrix` | 1 | `25618624980` |

### Token, cache, and memory metrics

| Metric | Value | Evidence / note |
|---|---:|---|
| Window total prompt/completion tokens | Not reliably available | Current collector sample does not expose trustworthy aggregate provider totals |
| Concrete token example | 42,103 | Embedded audit in `workflow_log_analysis` run `25621103657` for `implement` run `25614767039`, attempt 5 |
| Prompt-cache probe extra calls | 2 per `review_autofix` reviewer step | `scripts/review_run_reviewers.sh:118-160` |
| Sampled prompt-cache counters | All `na` | Direct logs from `25620282902`, `25617161072`, `25616314314` |
| AI memory retrieves | 20 | Embedded audit in `25621103657` |
| AI memory hits | 2 | Same |
| AI memory hit rate | 10.0% | Same |
| Average retrieve size (`estimated_tokens`) | 2.8 | Same |
| `keyword_method=none / plain / llm` | 18 / 2 / 0 | Same |

### GH API call summary

| Pattern | Sampled evidence | Current risk | Safe reduction |
|---|---|---|---|
| Repeated check-run polling in `review_autofix` | Run `25620282902` polled the same SHA every 20s for ~123s | Redundant calls and added latency | Stop after unchanged polls / earlier snapshot |
| Per-issue REST fallback in linked-issue context assembly | `.github/workflows/review_autofix.yml:1510-1549`; recent run `25631578938` found 40 fallback issues later in the flow | Can scale to many calls on orchestrator PRs | Batch fallback issue fetches with GraphQL aliases |
| Double lookup in `resolve-claude-branch-pr` | Run `25631603346` called open-PR lookup and default-branch lookup before skipping | Low redundancy | Skip default-branch lookup when PR already found |
| Artifact cleanup list + delete job | Raw `25631604780` shows artifact list/delete sequence; recent summaries repeatedly flag artifacts endpoint | Extra job/queue exposure | Reuse artifact ID; inline cleanup when possible |

### Semble telemetry summary

| Context | Sampled count / state | Logged bytes available? | Interpretation |
|---|---:|---|---|
| `ci / lint / Targeted file context contract tests` in deep-dive runs `25630232392`, `25630365046`, `25630494983`, `25630670009`, `25630678102` | **25 `SEMBLE_FALLBACK` events** total (**5 per run**) | No (`bytes` not emitted on fallback lines) | Healthy fail-open contract coverage with intentionally missing binaries |
| `orchestrate_poll` run `25630962603` | `SEMBLE_ENABLED=true`, `SEMBLE_AVAILABLE=false`, `SEMBLE_INDEX_AVAILABLE=false` | N/A | No observed prompt-reduction benefit in this sampled production run |
| Sampled production `review_autofix` runs | No trustworthy runtime `SEMBLE_QUERY ... bytes=` totals captured | No | Current window cannot quantify real Semble byte savings on production paths |

If you want, I can turn this report into a prioritized implementation checklist mapped to specific files and owners.

## Deep Audit — Workflows & Scripts (2026-05-10)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path** — `scripts/review_commit_changes.sh:83-117`; `scripts/review_apply_fixes.sh:672-715`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — In consumer repositories, `review_commit_changes.sh` deletes every newly created path except `scripts/*` and `prompts/*` before staging (`rm -rf -- "${created_file}"` at lines 102-117), and the log message explicitly says “editor may not create new files.” That conflicts with the review editor contract in `review_apply_fixes.sh`, which allows file creation when “absolutely required to fix a broken import or dependency” (lines 707-715). The same commit script also documents that new files are legitimate in the workflow-source repo (lines 83-90). The net effect is that a valid editor fix that requires a new module/config/template in a consumer repo can be silently removed before commit, producing false “no changes” or partial-fix outcomes.
- **Recommended fix** — Align policy in one direction, not both. Preferred: port the workflow-source repo’s touched-file reconciliation path (`scripts/review_commit_changes.sh:172-269`) to the consumer-repo branch so newly created files are kept when they were actually produced by the editor and are not protected workflow artifacts. If new-file creation is truly unsupported in consumer repos, then tighten the editor prompt in `review_apply_fixes.sh` to forbid it outright instead of allowing it conditionally.

#### BUG-002
- **File path** — `scripts/tg_helpers.sh:296-356`; `scripts/tg_helpers.sh:365-427`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — Both `tg_cleanup_phase_msgs()` and `tg_cleanup_msgs()` paginate GitHub issue comments with `?per_page=100&page=${page}`, delete matching tracking comments from the current page, and then increment `page`. Because the loop mutates the same comment collection that it is paginating, deleting entries from page 1 can shift former page-2 rows into page 1 before the next fetch. Those shifted rows are then skipped. This can leave stale tracking comments behind and can miss Telegram message deletions for issues with enough comments to span multiple pages.
- **Recommended fix** — Split cleanup into two phases: first collect all matching comment IDs and tracked Telegram IDs across all pages without deleting anything; then perform the Telegram deletes and GitHub comment deletes after the enumeration phase. A smaller alternative is to keep re-fetching `page=1` until no matching tracking comments remain.

#### BUG-003
- **File path** — `.github/workflows/review_autofix.yml:532-564`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — The standalone validate-dispatch loop only starts a workflow once (`validation_dispatched` gates `gh workflow run` at lines 551-558), but it removes `ai:orchestrator-validate-required` from **every** later matched issue in the same loop at lines 561-563. The dispatch itself passes only `tracking_issue="0"` and no per-issue identifier. That means the code clears issue-local validate-required labels even when only one generic dispatch was started. This is a correctness risk if a single dispatch is not actually authoritative for every linked issue. **[NEEDS VERIFICATION]**
- **Recommended fix** — Either:  
  1. remove the label only from the issue that successfully triggered the dispatch, or  
  2. pass an explicit issue list / issue number payload into the validate workflow and clear labels only after that workflow confirms it handled those exact issues.

#### CONSIST-001
- **File path** — `README.md:25-29`; `.github/workflows/issue_pr_status.yml:133-171`; `.github/workflows/issue_pr_status.yml:388-445`
- **Severity** — Medium
- **Category tag** — `consistency`
- **Description** — The repo contract says AI memory operations are fail-open (“a memory error never fails the workflow”), but `issue_pr_status.yml` does not honor that contract. The bootstrap step records `MEMORY_HELPERS_READY=0` when helper fetches fail (lines 133-171), and the later “Finalize linked issue lineage state” step exits with status 1 when helpers are missing (lines 412-419). That turns a support-script/bootstrap problem into a hard failure in the PR-close handler, even though other workflows already treat missing memory helpers as warnings and continue, e.g. `orchestrate_poll.yml:451-472` and `:548-565`.
- **Recommended fix** — Make lineage finalization fail-open like the poller’s run-event steps: if helpers are missing or bootstrap failed, emit a warning and `exit 0` instead of failing the workflow. Reuse the existing “memory helpers not found; skipping …” pattern from `orchestrate_poll.yml`.

---

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path** — `.github/workflows/review_autofix.yml:1430-1444`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The PR metadata hydration path in `review_autofix.yml` does four logical GitHub fetches in one execution path:  
  1. `GET /pulls/{PR_NUMBER}` into `PR_PAYLOAD_FILE`  
  2. paginated `GET /issues/{PR_NUMBER}/comments`  
  3. paginated `GET /pulls/{PR_NUMBER}/reviews`  
  4. paginated `GET /pulls/{PR_NUMBER}/comments`  
  That is before pagination expands the real request count. The repository already has a GraphQL-first consolidation helper, `gh_pr_with_all_comments()` in `scripts/gh_helpers.sh:735-900`, which is explicitly designed to collapse PR meta + comment hydration into one request with REST fallback. Current logical call count: **4**. Proposed logical call count after fix: **1** on the common path, with existing REST fallback preserved for pagination edge cases.
- **Recommended fix** — Extend `scripts/gh_helpers.sh:735-900` so `gh_pr_with_all_comments()` also emits the review objects currently fetched from `/pulls/{n}/reviews`, then replace the four inline calls in `review_autofix.yml` with a single helper invocation plus file writes derived from the returned JSON. Existing batching pattern to extend: **`gh_pr_with_all_comments()` in `scripts/gh_helpers.sh`**.

#### API-002
- **File path** — `.github/workflows/issue_pr_status.yml:253-350`; `.github/workflows/issue_pr_status.yml:501-513`
- **Severity** — Low
- **Category tag** — `api-redundancy`
- **Description** — `issue_pr_status.yml` already classifies linked issues into tracking vs. managed-child buckets with one batched GraphQL call plus fail-open REST fallback in the label-sync step (lines 253-350). The later Telegram alert step then discards that classification and re-fetches each linked issue body one-by-one with `_safe_gh_jq` to decide whether the PR is orchestrator-managed (lines 501-513). Current extra call count in the alert step: **N REST calls for N linked issues**, even though the earlier step already had enough information to answer the question once. Proposed extra call count after fix: **0**.
- **Recommended fix** — Export a boolean like `HAS_ORCHESTRATED_LINKED_ISSUE=true|false` or persist the `TRACKING_ISSUES` / `MANAGED_ISSUES` sets into `GITHUB_ENV` alongside `LINKED_ISSUE_NUMBERS`. Then let the alert step consume that cached decision instead of re-fetching issue bodies. Existing cache pattern to extend: the repo’s cycle-local/cache-through-env approach used for `LINKED_ISSUES_JSON` and the per-iteration caches in `scripts/orchestrate_poll_process.sh`.

#### API-003
- **File path** — `.github/workflows/test-and-mark-stable.yml:997-1033`
- **Severity** — Low
- **Category tag** — `api-redundancy`
- **Description** — The bait-injection stability guard polls the same `GET /pulls/{PR_NUMBER}` endpoint twice per attempt (`HEAD_A`, then `HEAD_B`) for up to 5 attempts, and then immediately performs a third fetch for `PR_META` after the loop. Worst-case current logical call count in this one block is **11** calls to the same PR endpoint (10 head-SHA reads + 1 final metadata read). The later metadata fetch overlaps fields already available from the stability reads (`head.sha`, `state`, `merged`, timestamps). Proposed logical call count after fix: **5** worst-case by fetching the PR JSON once per attempt and reusing that response for both stability and state checks.
- **Recommended fix** — Replace the `HEAD_A`/`HEAD_B` split reads with one cached PR JSON object per attempt, compare `head.sha` across iterations, and reuse the last successful object for the closed/merged fail-fast guard. Existing cache pattern to extend: the iteration-local PR JSON caching used in `scripts/orchestrate_poll_process.sh:6835-6849`.

#### BATCH-001
- **File path** — `scripts/gh_helpers.sh:916-932`
- **Severity** — Low
- **Category tag** — `api-batching`
- **Description** — The REST fallback for `_gh_issue_timeline_with_cross_refs_rest()` fetches one issue timeline and then loops over each cross-referenced PR URL, issuing one `gh api` call per PR to enrich merged state. Current logical call count on fallback: **1 + N** (one timeline request plus one PR request per cross-reference). That fallback is acceptable for correctness, but it is still an unbatched N-loop over PRs. Proposed logical call count after fix: **1** when the main GraphQL path works, or **1 + ceil(N / batch_size)** if the fallback itself used aliased GraphQL PR batching. **[NEEDS VERIFICATION]**
- **Recommended fix** — Keep the current fail-open behavior, but replace the per-PR REST enrichment loop with a batched GraphQL enrichment helper when fallback is needed. Existing batching pattern to extend: **`_fetch_linked_pr_status_graphql()` in `scripts/orchestrate_poll_process.sh:6204-6275`**.

---

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path** — `.github/workflows/issue_pr_status.yml:41-170`; `.github/workflows/validate.yml:185-275`; `.github/workflows/validation-improvements-intake.yml:48-134`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — These workflows each carry near-identical support-bootstrap logic: choose `wf_source`, resolve `script_ref`, construct `support_stage_root`, define `checkout_support_ref()`, prefer `${script_ref}` with `main` fallback, and copy support files from the checked-out workflow-source tree into the working directory. The duplicated blocks differ mostly by manifest contents and destination folders, not by control flow. That duplication already contributes to the expression-size pressure in `validate.yml`.
- **Recommended fix** — Extract the shared bootstrap into a new module, for example `scripts/bootstrap_workflow_support.sh`, with a signature such as:  
  `bootstrap_workflow_support.sh --wf-source <owner/repo> --script-ref <ref> --stage-root <dir> --dest-root <dir> --manifest <file> [--allow-local-fallback]`  
  Update callers: `issue_pr_status.yml`, `validate.yml`, and `validation-improvements-intake.yml`. `review_autofix.yml` can remain on its specialized bootstrap path initially, then converge later.

#### DUP-002
- **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53`; `.github/workflows/review_autofix.yml:1348-1386`; `.github/workflows/comprehensive-test-and-release.yml:72-98`; `.github/workflows/comprehensive-test-and-release.yml:315-341`; `.github/workflows/test-and-mark-stable.yml:468-482`; `.github/workflows/test-and-mark-stable.yml:593-605`; `.github/workflows/test-and-mark-stable.yml:1233-1255`; `.github/workflows/test-and-mark-stable.yml:4628-4653`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The repository has an existing standard GitHub API helper module in `scripts/gh_helpers.sh`, but multiple workflows still embed custom `_rl_wait`, `_gh_retry`, or `gh_api_safe` functions inline. These wrappers all re-implement the same rate-limit parsing and retry semantics slightly differently, which increases maintenance cost and makes API behavior inconsistent across workflows.
- **Recommended fix** — Centralize on `scripts/gh_helpers.sh` for all non-bootstrap-time GH calls. If a workflow truly cannot source the full helper that early, create a tiny bootstrap-safe shim, e.g. `scripts/bootstrap_gh_helpers.sh`, exposing:
  - `gh_retry <cmd...>`
  - `gh_retry_to_file <outfile> <cmd...>`
  - `gh_api_json_to_file <outfile> <cmd...>`
  
  Update callers in the workflows above to source the shared helper instead of maintaining local copies.

#### DUP-003
- **File path** — `.github/workflows/test-and-mark-stable.yml:460-503`; `.github/workflows/test-and-mark-stable.yml:593-620`; `.github/workflows/test-and-mark-stable.yml:1233-1265`; `.github/workflows/test-and-mark-stable.yml:2387-2410`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — `test-and-mark-stable.yml` repeats the same polling scaffolding across multiple phases: `RATE_LIMIT_BACKOFF`, `gh_api_safe()`, run-ID capture, last-activity tracking, and loop-based state polling. The duplicated logic is substantial and contributes directly to both file size and expression-limit risk.
- **Recommended fix** — Extract the repeated E2E polling logic into a new helper, for example `scripts/e2e_wait_helpers.sh`, with functions such as:
  - `gh_api_safe <endpoint> [--jq <expr>]`
  - `capture_run_id <repo> <created_after> <name_regex>`
  - `wait_for_issue_state <repo> <issue_number> <poll_interval_secs> <inactivity_limit_secs> <predicate_name>`
  
  Update the clarify wait, plan wait, review wait, and review-blocked wait callers to use the shared helper.

---

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:1203-1586`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Description** — This `run:` block contains `${{ }}` interpolations and is approximately **19,900 characters**, leaving only about **1,100 characters** of headroom before GitHub’s **21,000-character** expression hard stop. This is the highest-risk block found in the current audit set and is already in the same workflow family that previously hit the limit.
- **Recommended fix** — Extract the entire wait-review loop into an external script, preferably `scripts/e2e_wait_review.sh`, and pass only the small set of needed env vars (`PR_NUMBER`, `BAIT_SHA`, timeouts, repo slug) from YAML.

#### EXPR-002
- **File path** — `.github/workflows/review_autofix.yml:1345-1733`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — This PR-context assembly block is approximately **17,409 characters**, leaving about **3,591 characters** of headroom. It contains multiple `${{ }}` interpolations and a large amount of inline bash/Python glue, so ordinary feature growth could push it over the runner limit.
- **Recommended fix** — Move the metadata/linkage assembly into a dedicated script, e.g. `scripts/review_collect_pr_context.sh`, and keep the workflow step limited to env wiring and a single script invocation.

#### EXPR-003
- **File path** — `.github/workflows/test-and-mark-stable.yml:1673-2077`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — This verify-bait block is approximately **17,409 characters**, leaving about **3,591 characters** of headroom. It mixes shell logic, PR state checks, content mutation, and retry behavior in one interpolated `run:` expression.
- **Recommended fix** — Extract the bait-verification and mutation logic into an external script such as `scripts/e2e_verify_bait.sh`, or split the block into smaller steps that separate PR-state checks, canary content generation, and the `PUT /contents` mutation.

#### EXPR-004
- **File path** — `.github/workflows/validate.yml:188-511`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The validate support-bootstrap block is approximately **15,135 characters**, leaving about **5,865 characters** of headroom. That is still above the 15,000-character medium-risk threshold, and the block duplicates logic already present elsewhere.
- **Recommended fix** — Extract the bootstrap logic into a shared script, ideally the same `scripts/bootstrap_workflow_support.sh` proposed in `DUP-001`, so the workflow file stops carrying the entire support-resolution program inline.

- **Overall workflow file size check** — No workflow exceeded the **800 KB** early-warning threshold. The largest audited workflow files were `review_autofix.yml` (**288,363 chars**) and `test-and-mark-stable.yml` (**273,682 chars**), both below the **1,048,576-char** hard cap.

---

### Section 5: Cross-Cutting Concerns

- No `TODO`, `FIXME`, or `HACK` markers were present in the audited `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py` scope.

#### DEAD-001
- **File path** — `scripts/orchestrate_poll_process.sh:9985-10015`; `scripts/orchestrate_poll_process.sh:10234-10288`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `RB_FOLLOWUP_REFUSED` is assigned at lines 9985 and 10015, and `IF_BLOCKERS_SOURCE` is initialized/updated at lines 10234, 10284, and 10288, but neither identifier is read later in the file. These assignments therefore have no behavioral effect and can mislead future maintainers into believing the values feed later control flow or telemetry.
- **Recommended fix** — Either remove the unused variables entirely, or wire them into a real consumer such as structured logging, state persistence, or a downstream decision branch.

#### SHELL-001
- **File path** — `scripts/validate_changed_files_syntax.sh:70-74`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — ShellCheck reports `SC2221`/`SC2222` here: the early `*.env*` pattern already covers the later `*,*.envrc` and `*,.env*` branches, so those later alternatives are unreachable. This is low-risk, but it obscures the real matching semantics of the redaction denylist.
- **Recommended fix** — Remove the redundant `*,*.envrc|*,.env*` alternatives, or tighten the first `*.env*` branch if `.envrc` was meant to receive distinct handling.

---

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 10 | BUG-002, BUG-003, CONSIST-001, API-001, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004 |
| Low | 5 | API-002, API-003, BATCH-001, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3–4 | Medium |
| API call optimization | 4–5 | Medium |
| Code modularization | 7–9 | Large |
| Expression size reduction | 4–6 | Medium |
| Medium/Low fixes | 4–5 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-10)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation can be implemented directly without changing call semantics; `NEEDS_VERIFICATION` means the overlap is real but a human or follow-up analysis must prove freshness/error-handling/caller-contract safety first; `RISKY_SKIP` means the redundancy is visible but sits in a pagination/race/retry-sensitive path that must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — NEEDS_VERIFICATION
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/review_autofix.yml:512-529` and `.github/workflows/review_autofix.yml:537-544`
- **Current call count** — On the body/title-fallback branch, **N extra per-issue label lookups** after the initial linked-issue discovery call.
- **Proposed call count** — **1 batched label lookup** for the same fallback issue set.
- **Endpoint(s)** — Existing GraphQL `pullRequest(number) { closingIssuesReferences(first: 50) { nodes { number labels(first: 100) { nodes { name } } } } }`; current fallback per-issue label reads via `gh issue view ... --json labels`; proposed aliased GraphQL `issue(number: N) { labels(first: 100) { nodes { name } } }`.
- **Evidence** — The step already does one batched GraphQL fetch with labels, but when `closingIssuesReferences` is empty it degrades to a numbers-only JSON array and then re-fetches labels one issue at a time:
  ```bash
  issue_nodes_json="$(gh api graphql \
    ... \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // [] | map({number: .number, labels: ((.labels.nodes // []) | map(.name))})' || true)"

  if [ -z "${issue_nodes_json}" ] || [ "${issue_nodes_json}" = "[]" ]; then
    ...
    issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0)) | map({number: tonumber, labels: null})')"
  fi

  if [ "${labels_known}" != "true" ]; then
    issue_labels="$(gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null || true)"
  fi
  ```
- **Proposed fix** — In the `Dispatch standalone validate for orchestrator short-circuit issues` step, once `issue_numbers` is synthesized at `.github/workflows/review_autofix.yml:519-529`, issue one aliased GraphQL batch to populate `{number, labels}` for all fallback issues before the `while` loop. Reuse the existing batching style from `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh`, and keep the current per-issue `gh issue view` only as a fail-open fallback if the batch query fails or returns null nodes.
- **Safety rationale** — `NEEDS_VERIFICATION` is required because this changes per-issue fail-open behavior into a batched query, and static reading alone does not prove that missing/closed/inaccessible issues will be handled identically before workflow dispatch and label removal.
- **Downstream signal** — Verify on at least one fallback-heavy merged PR that batched GraphQL label results exactly match the current `gh issue view` decisions for: existing issue with label, existing issue without label, missing issue, and closed issue; only then replace the N-loop.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — RISKY_SKIP
- **Safety tag** — `RISKY_SKIP`
- **File path and line ranges** — `.github/workflows/review_autofix.yml:1430-1444`; `scripts/review_rb_judge.sh:214-216`; `scripts/review_rb_judge.sh:274-282`; `scripts/review_rb_judge.sh:307-308`; `scripts/review_rb_judge.sh:343-344`
- **Current call count** — **2 additional logical PR-context fetches** in the common `review_rb_judge.sh` path on top of the earlier `Collect PR metadata` step: one `/pulls/{PR_NUMBER}` title/body fetch plus one `gh_pr_with_all_comments(...)` fetch. On helper fallback, that second fetch can expand into paginated REST comment calls.
- **Proposed call count** — **0 additional common-path fetches** if the judge consumes the already-materialized PR files, retaining the current network path only as a missing-file/unreadable-file fallback.
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr}`; GraphQL/REST PR comment hydration via `gh_pr_with_all_comments`; paginated REST `/repos/{repo}/issues/{pr}/comments` and `/repos/{repo}/pulls/{pr}/comments` on helper fallback.
- **Evidence** — `Collect PR metadata` already materializes the raw PR payload and comment files:
  ```bash
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"
  gh_retry /tmp/gh_issue_comments_raw.json api --paginate repos/${{ github.repository }}/issues/"${PR_NUMBER}"/comments
  gh_retry /tmp/gh_reviews_raw.json api --paginate repos/${{ github.repository }}/pulls/"${PR_NUMBER}"/reviews
  gh_retry /tmp/gh_review_comments_raw.json api --paginate repos/${{ github.repository }}/pulls/"${PR_NUMBER}"/comments
  ```
  But the late judge path re-reads the same PR context instead of consuming those files:
  ```bash
  PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"

  PR_CONTEXT_JSON="$(gh_pr_with_all_comments "${REPOSITORY%%/*}" "${REPOSITORY##*/}" "${PR_NUMBER}" "${PRELOADED_PR_META}" || echo '{}')"

  echo "Title: $(jq -r '.title // ""' "${PR_META_FILE}")"
  echo "Body: $(jq -r '.body // ""' "${PR_PAYLOAD_FILE}")"
  ```
  Net-new beyond Deep Audit `API-001`: even after the main metadata hydration is consolidated, the review-blocked judge still performs a second PR-context fetch instead of reading the files the workflow already created.
- **Proposed fix** — Teach `scripts/review_rb_judge.sh` to prefer `PR_META_FILE` / `PR_PAYLOAD_FILE` for `PR_DATA`, and to assemble `PR_CONTEXT_JSON` from `PR_ISSUE_COMMENTS_FILE`, `PR_REVIEW_COMMENTS_FILE`, and `PR_META_FILE` when those files are present and parseable. Keep `gh_pr_with_all_comments` only as a fallback for missing/unreadable files.
- **Safety rationale** — `RISKY_SKIP` is mandatory because the current path includes paginated comment hydration, and the judge runs late enough that collapsing to the earlier snapshot can change both page-boundary semantics and freshness in a path that informs terminal PR handling.
- **Downstream signal** — Do **not** auto-implement. Manual review must decide whether `review_rb_judge.sh` is allowed to use the earlier PR snapshot; if yes, retain the current network fetches only as a missing-file/unreadable-file fallback and validate against a long-running PR where new human comments land after the initial metadata snapshot.

#### REUSE-002 — NEEDS_VERIFICATION
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/review_autofix.yml:15-23`; `.github/workflows/review_autofix.yml:74-84`; `.github/workflows/review_autofix.yml:519-520`; `.github/workflows/internal-review.yml:57-63`
- **Current call count** — On the `closingIssuesReferences == []` post-merge fallback path, **1 extra `GET /pulls/{PR_NUMBER}`** to recover PR title/body.
- **Proposed call count** — **0 extra `GET /pulls/{PR_NUMBER}`** on the repo’s normal `pull_request.closed` wrapper path; keep the current API read only when `inputs.pr_title` and `inputs.pr_body` are blank (for direct/manual callers).
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr}` for `.title + " " + (.body // "")`.
- **Evidence** — The reusable workflow already accepts `pr_title` / `pr_body`, and the in-repo wrapper passes them through:
  ```yaml
  # .github/workflows/review_autofix.yml
  pr_title:
    description: "PR title for [skip ai] gating (passed by wrapper workflows)"
  pr_body:
    description: "PR body for [skip ai] gating (passed by wrapper workflows)"
  ```
  ```yaml
  # .github/workflows/internal-review.yml
  pr_title: >-
    ${{ github.event_name != 'workflow_dispatch' && format('{0}', github.event.pull_request.title) || '' }}
  pr_body: >-
    ${{ github.event_name != 'workflow_dispatch' && format('{0}', github.event.pull_request.body) || '' }}
  ```
  Yet the merged-PR fallback step still re-fetches the same two fields:
  ```bash
  pr_data="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
  ```
- **Proposed fix** — In `post-merge-validate-dispatch`, build `pr_data` from `${{ inputs.pr_title }}` / `${{ inputs.pr_body }}` first, and call `gh api /pulls/${PR_NUMBER}` only if both are blank. Keep the existing direct-API fallback for `workflow_dispatch` / nonstandard callers, because `.github/workflows/review_autofix.yml:74-84` does not declare `pr_title` / `pr_body` for direct dispatch.
- **Safety rationale** — `NEEDS_VERIFICATION` is appropriate because the in-repo caller clearly provides the data, but static reading alone does not prove that every supported caller that can reach `post_merge_dispatch == true` does the same.
- **Downstream signal** — Verify every supported caller that can trigger the merged-PR path passes non-empty `pr_title` / `pr_body`; if yes, switch this step to input-first and preserve the current API call only for blank-input cases.

#### REUSE-003 — NEEDS_VERIFICATION
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/implement.yml:66-89`; `.github/workflows/implement.yml:607-621`
- **Current call count** — **2 calls** to `GET /repos/{repo}/issues/{ISSUE_NUMBER}` before implementation starts.
- **Proposed call count** — **1 full issue fetch** on the reuse path, with any second fetch narrowed or removed only if verification shows live refresh is unnecessary.
- **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`
- **Evidence** — The workflow first fetches issue state + labels to decide whether to skip:
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ```
  Then, after install/setup, it fetches the same issue again to get the full payload:
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"
  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ```
- **Proposed fix** — Either move `Create runtime workspace` ahead of `Precheck approval phase label` so the first fetch can be written directly to `${ISSUE_META_FILE}`, or write the precheck payload to a temp file under `${RUNNER_TEMP}` and promote it later. If a second read must remain, narrow it to only the fields that truly need a fresh read instead of re-fetching the whole issue.
- **Safety rationale** — `NEEDS_VERIFICATION` is required because the second read currently happens after setup/install and under different retry behavior (`gh api` vs `gh_retry gh api`), so static reading does not prove that the first snapshot is fresh enough for downstream steps.
- **Downstream signal** — Verify whether any automation or human edit between `Precheck approval phase label` and `Fetch issue metadata` must be visible before Codex runs; if not, persist the first payload and delete the second GET, otherwise keep only a narrow refresh for the mutable fields.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001 — NEEDS_VERIFICATION
- **Safety tag** — `NEEDS_VERIFICATION`
- **File path and line ranges** — `.github/workflows/issue_pr_status.yml:180-182`; `.github/workflows/issue_pr_status.yml:205-208`; `.github/workflows/internal-issue-pr-status.yml:4-12`
- **Current call count** — **1 latent fallback call site** to `GET /pulls/{PR_NUMBER}` on the “PR title/body empty” branch.
- **Proposed call count** — **0 latent fallback call sites** after removal, or **0 on the repo’s normal wrapper path** if the fallback is hidden behind an explicit compatibility guard.
- **Endpoint(s)** — `GET /repos/{repo}/pulls/{pr}` for `.title + " " + (.body // "")`
- **Evidence** — The workflow already has PR title/body from the event:
  ```bash
  PR_TITLE: ${{ github.event.pull_request.title }}
  PR_BODY: ${{ github.event.pull_request.body || '' }}

  PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"
  if [ -z "$(printf '%s' "${PR_DATA}" | tr -d '[:space:]')" ]; then
    PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null || echo "")"
  fi
  ```
  And the only in-repo caller is a `pull_request.closed` wrapper:
  ```yaml
  on:
    pull_request:
      types: [closed]

  jobs:
    sync-status:
      uses: shubhodeep1/coding-workflows/.github/workflows/issue_pr_status.yml@main
  ```
- **Proposed fix** — Remove the fallback PR fetch for the repo’s standard wrapper path, or gate it behind an explicit “nonstandard caller” compatibility flag so the reusable workflow only performs that API read when a caller truly cannot provide pull-request event title/body.
- **Safety rationale** — `NEEDS_VERIFICATION` is the correct tag because the dead branch is evident for the in-repo wrapper, but changing a reusable workflow without checking external consumers could remove a compatibility fallback that some out-of-repo caller still depends on.
- **Downstream signal** — Verify that every supported caller of `issue_pr_status.yml` triggers it from a `pull_request.closed` event with populated `github.event.pull_request.title/body`; only then delete this fallback or move it behind an explicit compatibility switch.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — real overlap, but the proposed consolidation touches paginated PR comment/review hydration and must preserve the helper’s page-boundary + fail-open fallback contract.
- API-002: NEEDS_VERIFICATION — the cached linked-issue classification should be reusable, but alert suppression behavior still depends on whether later issue-body changes are intentionally meant to affect the Telegram step.
- API-003: RISKY_SKIP — the duplicate `/pulls/{PR_NUMBER}` reads live inside a stability/polling loop, so collapsing them changes the race-detection observation points.
- BATCH-001: RISKY_SKIP — the fallback path paginates timeline data after GraphQL failure, so batching must preserve both fail-open behavior and cross-reference enrichment semantics.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 4 | MERGE-001, REUSE-002, REUSE-003, DEAD-API-001 |
| RISKY_SKIP | 1 | REUSE-001 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
