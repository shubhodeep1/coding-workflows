## Executive Summary

- **Biggest critical-path win: short-circuit no-op orchestrator polls before checkout/support staging.** Recent `orchestrate_poll` runs `25368600796`, `25366694043`, `25364497697`, `25362725399`, `25360913721`, and `25359368714` all completed with `has_work=false`, yet still took **40–55s** each. In `25368600796`, the poll found **0 active tracking issues at 09:31:18Z** and still continued through checkout/support-file staging before ending at **49s**. **Estimated impact:** save **30–40s per no-op poll run**. **Confidence:** high.

- **`review_autofix` is the dominant latency and likely token-cost hotspot.** Full comment-only `claude/*` review runs such as `25368082752`, `25365010380`, `25356794150`, `25362354614`, and `25363663935` spent **1,885–2,058s**; run `25368082752` executed **two passes across 6 reviewers**, with pass 1 from **09:30:00Z–09:42:21Z** and pass 2 from **09:42:45Z–09:52:52Z**. **Estimated impact:** cut **8–12 min/run** and substantial reviewer-token spend by skipping pass 2 on comment-only paths. **Confidence:** high.

- **Release reliability is currently blocked by the e2e smoke/editor verification path.** `test_and_mark_stable` failed **2/2 times** on **May 4, 2026** (`25324103531`, `25347776357`) at `e2e-smoke-test → Phase 4b: Verify editor removed bait line`, after **3,139–3,303s**. The linked `review_autofix` failure `25324565713` shows the root failure was the conflict resolver repeatedly failing on `tests/e2e_smoke_canary.txt`. **Estimated impact:** restore the stable-release gate from **0% success in this window** to normal operation. **Confidence:** high.

- **Prompt cache is enabled but effectively unauditable.** `review_autofix` logs in failed run `25324565713` and slow run `25353743396` emitted cache probes with `cache_enabled=true` but `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. That blocks evidence-based cache tuning. **Estimated impact:** observability first; likely unlocks **10–20% repeat-review token savings** later. **Confidence:** high.

- **AI memory retrieval is mostly missing useful hits.** Across deep-dive logs there were **11 `retrieve` operations**, only **1** selected any records (**9.1% hit rate**), with **10/11 returning 0 records** and `keyword_method` almost always `none`. **Estimated impact:** modest direct latency improvement, but meaningful quality/reliability improvement once retrieval targeting is fixed. **Confidence:** high.

- **CI is stable but consistently serial and slow.** `ci` has **44/44 successes**, but p50 is **608s** and p95 **641.6s**; repeated runs (`25368082690`, `25366562925`, `25363663843`, `25362354516`, `25359051541`) all show the single `lint` job dominating the full ~10-minute duration. **Estimated impact:** **3–4 min/run** by splitting the monolithic lint/test job into parallel lanes. **Confidence:** medium.

## Speed Optimizations

### 1. Early-exit `orchestrate_poll` before repo checkout on `has_work=false`
**Type:** critical-path win

- **Evidence**
  - Recent no-op poll runs: `25368600796` (**49s**), `25366694043` (**48s**), `25364497697` (**50s**), `25362725399` (**53s**), `25360913721` (**40s**), `25359368714` (**55s**).
  - In `25368600796`, `Found 0 active tracking issue(s)` and `No active orchestrator projects. Exiting gracefully` were logged before later checkout/staging activity still occurred.
- **Root cause**
  - The workflow performs hosted-runner startup plus repository/support-source checkout even when the poll determines there is no work.
- **Exact change**
  - Split poll into:
    1. a lightweight preflight step/job that only calls GitHub APIs to detect tracking issues,
    2. a follow-on checkout/support-staging path only when `has_work=true`.
  - Keep the existing memory run-event writes in the preflight path.
- **Estimated time savings**
  - **30–40s per no-op poll run**.
- **Implementation risk**
  - **Low**. Behavior is unchanged for `has_work=true`; only the no-op path exits earlier.

### 2. Skip pass 2 for `claude/*` comment-only `review_autofix` runs
**Type:** critical-path win

- **Evidence**
  - `review_autofix` runs `25368082752` (**2058s**), `25365010380` (**2056s**), `25356794150` (**2073s**), `25362354614` (**1926s**), `25363663935` (**1885s**) all show comment-only `claude-branch-review` mode.
  - In `25368082752`, six reviewers ran in pass 1 and the same six ran again in pass 2. The reviewer phase alone spans roughly **23 minutes**.
  - Gate log shows: `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`
- **Root cause**
  - The expensive two-pass panel is still used even when the path is comment-only and not driving an editor/commit/judge decision.
- **Exact change**
  - On `CLAUDE_BRANCH_REVIEW=true`, run only pass 1 by default.
  - Run pass 2 only if one of these is true:
    - pass 1 produces a high-confidence finding,
    - diff size exceeds a configured threshold,
    - `[force-review]` is present.
- **Estimated time savings**
  - **8–12 min per full comment-only review run**.
- **Implementation risk**
  - **Medium**. Could reduce reviewer consensus depth on some PRs; mitigated by leaving a force-review override.

### 3. Parallelize the monolithic `ci` lint/test lane
**Type:** critical-path win

- **Evidence**
  - `ci` family: **44 total runs**, **100% success**, p50 **608s**, p95 **641.6s**.
  - Runs `25368082690`, `25366562925`, `25363663843`, `25362354516`, `25359051541` all show the single `lint` job dominating the entire workflow.
  - Recent run `25368082690` includes dozens of test/lint steps in one job, including Python tests, contract tests, shell checks, YAML/actionlint, JSON schema, and script cross-reference checks.
- **Root cause**
  - Independent checks are serialized inside one hosted job.
- **Exact change**
  - Split CI into at least 3 parallel jobs:
    - **unit/contracts**,
    - **shell/YAML/actionlint/schema**,
    - **Python lint/syntax/workflow-reference checks**.
  - Keep the current pass/fail contract unchanged with a final aggregate status.
- **Estimated time savings**
  - **180–260s per CI run**.
- **Implementation risk**
  - **Low to medium**. Main risk is reproducing current artifact/test environment across jobs.

### 4. Collapse Copilot review teardown into the same job or reuse uploaded artifact IDs directly
**Type:** critical-path win

- **Evidence**
  - `copilot_pull_request_reviewer` runs `25366565178` (**406s**), `25363665314` (**278s**), `25362090734` (**291s**), `25359918526` (**368s**), `25357757494` (**275s**) were dominated by `Cleanup artifacts` and runner wait.
  - These runs explicitly hit `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<run_id>/artifacts`.
  - `25363665314` logged `Upload results/system: Result: '6800976178'`, meaning the artifact ID already existed and did not need to be re-listed.
- **Root cause**
  - A separate cleanup phase waits for another runner and re-queries artifact listings instead of consuming the prior upload result.
- **Exact change**
  - Pass uploaded artifact IDs directly from upload to cleanup.
  - Prefer in-job cleanup where possible, or at minimum skip the artifact-list API call when the artifact ID is already known.
- **Estimated time savings**
  - **90–180s per Copilot review run**.
- **Implementation risk**
  - **Low**. This is workflow plumbing, not behavior change.

### 5. Avoid full branch/tag-heavy checkout in no-op poll and similar utility workflows
**Type:** micro-optimization

- **Evidence**
  - Recent `orchestrate_poll` log summary for `25368600796`: `Checkout repository fetched many tags; checkout logs are the longest visible section.`
  - Similar lightweight utility runs (`cancel_on_pr_close`, `issue_pr_status`, `forward_merge_stable_to_main`) finish in **15–21s**, so checkout overhead is meaningful at this scale.
- **Root cause**
  - Utility workflows pull more repo history/reference data than they use.
- **Exact change**
  - For workflows that only read scripts or dispatch actions:
    - `fetch-depth: 1`
    - `fetch-tags: false`
    - limit refspecs to required branches only
  - Only keep full-history fetch in release/promotion workflows that genuinely need tags.
- **Estimated time savings**
  - **5–10s per utility run**.
- **Implementation risk**
  - **Low**, if scoped only to non-release workflows.

## Cost Optimizations

### 1. Halve model calls on comment-only `review_autofix` paths
- **Evidence**
  - `25368082752` ran **12 successful reviewer calls**: 6 in pass 1 + 6 in pass 2.
  - All observed long `claude/*` comment-only review runs stayed in the same mode and skipped editor/commit/judge/auto-merge.
- **Root cause**
  - Model spend is allocated as if these runs were full autofix decisions, even though they only need a reviewer comment.
- **Exact change**
  - Default `claude/*` comment-only runs to one-pass review; reserve pass 2 for escalation conditions.
- **Estimated savings**
  - **40–50% reviewer-token savings** on this path; likely the largest single dollar reduction in the current pipeline.
- **Quality-risk notes**
  - Moderate risk of lower consensus depth; offset by conditional pass-2 escalation.

### 2. Lower reviewer reasoning effort for comment-only review mode
- **Evidence**
  - Failed and slow `review_autofix` runs show `REVIEWER_REASONING_EFFORT: medium`.
  - The same path is explicitly comment-only.
- **Root cause**
  - Medium reasoning is being spent on a path that does not proceed to automatic edits/merge judgment.
- **Exact change**
  - Set reviewer reasoning to `low` when `CLAUDE_BRANCH_REVIEW=true`; preserve `medium` for full autofix/editor flows.
- **Estimated savings**
  - **10–20% additional reviewer-token reduction** on top of pass-count reduction.
- **Quality-risk notes**
  - Low to medium; best paired with keeping the full reviewer set or the pass-2 escalation rule.

### 3. Eliminate avoidable reruns in release smoke/editor verification
- **Evidence**
  - `test_and_mark_stable` failed twice (`25324103531`, `25347776357`) after **52–55 minutes** each.
  - The upstream review run `25324565713` failed in **925s** on the same canary-conflict theme.
- **Root cause**
  - A deterministic conflict pattern in `tests/e2e_smoke_canary.txt` is consuming full release-test cycles.
- **Exact change**
  - Add a deterministic resolver for the smoke canary file or bypass merge-style conflict resolution entirely for the smoke fixture branch by rewriting the canary to the expected 3-line spec before verification.
- **Estimated savings**
  - Avoids **~1 hour of wasted compute per failed stable-release attempt** in addition to reliability gains.
- **Quality-risk notes**
  - Low, if restricted to the dedicated smoke-test fixture path.

### 4. Emit real prompt-cache counters before further model tuning
- **Evidence**
  - `review_autofix` cache probes in `25324565713` and `25353743396` logged `cache_enabled=true` but all token/cache counters as `na`.
- **Root cause**
  - Cost optimization is blind; cache is configured but not measurable.
- **Exact change**
  - Emit per-model-call:
    - `prompt_tokens`
    - `completion_tokens`
    - `total_tokens`
    - `cache_creation_input_tokens`
    - `cache_read_input_tokens`
    - cache-hit boolean
- **Estimated savings**
  - **No immediate savings**, but this is the prerequisite for safely extracting **10–20%** from repeated prompts.
- **Quality-risk notes**
  - Very low risk; telemetry-only.

### 5. Reduce repeated prompt/context expansion in `review_autofix`
- **Evidence**
  - The workflow explicitly pre-assembles a stable prefix for cacheability, but still includes dynamic PR metadata, linked issue context, diff context, and reviewer outputs in a large multi-step build-up.
  - Workflow-log-analysis runs called out repeated PR metadata and linked-issue re-fetch/rebuild patterns in the same path.
- **Root cause**
  - Prompt inputs vary more than necessary across steps/runs, which likely fragments cache reuse.
- **Exact change**
  - Canonicalize prompt assembly:
    - stable instructions first,
    - deterministic sort order for files/findings,
    - avoid embedding volatile run IDs and timestamps early,
    - reuse cached PR/linkage artifacts across late steps.
- **Estimated savings**
  - **Low-confidence 5–15% token reduction** on repeated review patterns once counters are available.
- **Quality-risk notes**
  - Low, if only ordering/canonicalization changes.

## Reliability Improvements

### 1. Fix the smoke-canary merge/conflict resolver path
- **Failure evidence**
  - `test_and_mark_stable` runs `25324103531` and `25347776357` both failed at `Phase 4b: Verify editor removed bait line`.
  - `review_autofix` failure `25324565713` shows repeated merge conflicts in `tests/e2e_smoke_canary.txt`, unresolved conflict markers, and final `Conflict resolver failed after retries.`
- **Root cause category**
  - Deterministic content-conflict handling defect.
- **Exact fix**
  - Special-case the e2e smoke canary in the conflict-resolver allowlist:
    - if the only unresolved file is `tests/e2e_smoke_canary.txt`, replace it with the known expected 3-line content and continue;
    - alternatively skip merge-replay conflict resolution for the smoke branch and validate the post-editor head directly.
- **Expected reliability impact**
  - Highest-priority fix; likely restores the stable-release test family from **2/2 failures** in this window.
- **Rollback/fail-open considerations**
  - Restrict the special-case to the dedicated smoke fixture only; if the file set differs, keep the current hard fail.

### 2. Add retries to transient GitHub API calls in Copilot review preparation/cleanup
- **Failure evidence**
  - Copilot review logs repeatedly show `actions/github-script@v8` with `retries: 0`.
  - API-heavy steps include `pulls.listFiles` pagination and artifact-list cleanup calls.
- **Root cause category**
  - Transient API fragility.
- **Exact fix**
  - Set bounded retries for `actions/github-script` and `gh api` calls that read PR files/artifacts, with existing exempt-status behavior preserved.
- **Expected reliability impact**
  - Moderate reduction in flaky review failures during GitHub API turbulence.
- **Rollback/fail-open considerations**
  - Keep retry budget low (2–3 attempts) and retain existing fail-open/non-blocking behavior for cleanup steps.

### 3. Make nightly validation self-test failure output actionable
- **Failure evidence**
  - `nightly_validation_selftest` run `25356037835` failed with `fixtures=3 passed=1 failed=2`, but the top-level log does not surface which 2 fixtures failed.
- **Root cause category**
  - Observability gap on a failing quality gate.
- **Exact fix**
  - Emit the failed fixture names and failing stage names from `artifacts/validation-selftest-summary.json` before exit 1.
- **Expected reliability impact**
  - Faster diagnosis and repair; indirect but meaningful improvement to MTTR.
- **Rollback/fail-open considerations**
  - No behavior change to pass/fail semantics.

### 4. Use workflow-level concurrency to pre-empt superseded `review_autofix` runs earlier
- **Failure evidence**
  - `review_autofix` has **42 cancelled runs out of 69 total**.
  - Several cancelled runs still performed `resolve-claude-branch-pr` API work before ending, e.g. `25358829273`, `25359916008`, `25360958556`.
- **Root cause category**
  - Superseded-run waste / cancellation timing.
- **Exact fix**
  - Tighten `concurrency` grouping on PR/head-ref so newer runs cancel older ones before they enter API resolution/setup.
- **Expected reliability impact**
  - Lower rerun noise and fewer half-started review cycles competing for runners.
- **Rollback/fail-open considerations**
  - Low risk if scoped by PR/head ref.

### 5. Standardize rate-limit wrappers across API-heavy workflows
- **Failure evidence**
  - `orchestrate_poll`, `cancel_on_pr_close`, and e2e smoke flows all carry local retry/rate-limit wrappers, but they are inconsistent in attempt counts and backoff behavior.
  - Rate-limit handling appears repeatedly in logs and scripts.
- **Root cause category**
  - Retry policy fragmentation.
- **Exact fix**
  - Reuse one common helper for:
    - exponential backoff,
    - secondary-rate-limit detection,
    - reset-based sleep,
    - clear fail-open vs fail-closed semantics per call type.
- **Expected reliability impact**
  - Small-to-moderate improvement in resilience to GitHub API turbulence.
- **Rollback/fail-open considerations**
  - Preserve current call-specific fail-open behavior for non-critical lookups.

## AI Memory Health

- **Telemetry presence:** found in deep-dive `review_autofix`, `orchestrate_poll`, and `workflow_log_analysis` logs; absent in most CI/copilot/utility runs.
- **Retrieve hit rate:** **1/11 = 9.1%**
- **Average `estimated_tokens`:** **2.5**
- **Budget telemetry:** no `budget_tokens` values were emitted in the retrieved deep-dive samples.
- **`keyword_method` distribution:** `none=10`, `plain=1`, `llm=0`

### What the telemetry shows
- `review_autofix` memory retrieval is usually enabled but ineffective:
  - failed run `25324565713`: `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`
  - slow runs `25368082752`, `25365010380`, `25356794150`, `25353743396`, `25355461484`, `25352551157`: same pattern
- The only positive retrieval hit in the deep dives came from `workflow_log_analysis` (`25324145530`), which selected **1 record** with `estimated_tokens=28` and `keyword_method=plain`.

### Flags
- **Zero-record retrieves:** **10/11**
- **`fail_open: true` entries:** none observed in parsed `AI_MEMORY_TELEMETRY` payloads
- **`enabled: false` entries:** none observed
- **High push retry counts:** not observed; `push_attempts` was almost always **1**, with one entry at **2**

### Assessment
- Memory write plumbing is active (`record-run-event`, `record-candidate`), but retrieval quality is poor for the hottest workflow (`review_autofix`).
- The near-total use of `keyword_method=none` suggests retrieval queries are not being seeded with strong keywords or are bypassing keyword derivation in practice.
- This is not a stability failure today, but it means the memory system is not yet paying for itself on the most expensive path.

### Recommended actions
1. **Emit retrieval inputs/constraints** in telemetry: query source, candidate count before filtering, and `budget_tokens`.
2. **Promote deterministic keywords** for review context (repo, workflow family, path clusters, PR topic, failure mode).
3. **Add a retrieval hit SLO**: e.g. alert when `review_autofix` retrieve hit rate falls below 25% over a rolling window.
4. **Track zero-hit reasons** explicitly: no candidates, budget trim, keyword failure, filter mismatch.

## GH API Call Audit

### 1. `review_autofix` repeats PR and linked-issue reads inside the same run
- **Evidence**
  - `review_gate` in runs like `25324565713` and `25369747657` calls:
    - `GET /repos/{repo}/pulls/{PR}`
    - `GET /repos/{repo}/commits/{sha}`
    - paginated `GET /repos/{repo}/pulls/{PR}/files`
  - Workflow-log-analysis deep-dive (`25347801271`) explicitly flagged repeated fallback PR reads and late linked-issue re-resolution in `review_autofix`.
- **Why it matters**
  - This breaches the repo’s own API hygiene rules logged in workflow analysis: reuse existing calls, prefer batched GraphQL, and treat cycle-local caches as first-class.
- **Concrete fix**
  - Persist one canonical `PR_META_FILE`/`LINKED_ISSUE_NUMBERS` artifact from the early metadata step and have all later steps read it.
- **Estimated reduction**
  - **Up to 3 PR read calls per run** in the tail path.
- **Rate-limit risk reduction**
  - Moderate; reduces repeated reads on the hottest workflow.

### 2. `review_gate` still uses paginated `/files` reads for doc-only classification
- **Evidence**
  - Gate logs in `25324565713` and `25369747657` show paginated `gh api --paginate repos/.../pulls/${PR_NUMBER}/files`.
- **Why it matters**
  - This is the most expensive gate-side REST call and runs before expensive review work even starts.
- **Concrete fix**
  - Only call `/files` when the cheap size check cannot decide; cache the filenames JSON for any later step that needs it.
- **Estimated reduction**
  - **1 paginated call avoided** on many small-diff PRs.
- **Rate-limit risk reduction**
  - Moderate on bursty PR activity.

### 3. Copilot review cleanup re-lists artifacts instead of consuming known artifact IDs
- **Evidence**
  - Runs `25366565178`, `25363665314`, `25362090734`, `25359918526`, `25357757494` all call `gh api /repos/.../actions/runs/<run_id>/artifacts`.
- **Why it matters**
  - Adds one extra API read and a separate runner-backed teardown phase.
- **Concrete fix**
  - Pass artifact IDs directly from upload step outputs into cleanup.
- **Estimated reduction**
  - **1 artifact-list API call per run**.
- **Rate-limit risk reduction**
  - Low but easy win.

### 4. `orchestrate_poll` spends runner time even when API result says “no work”
- **Evidence**
  - `25368600796` used `gh issue list --label ai:orchestrator-tracking`, found **0** tracking issues, then still continued through workflow-support and repository staging.
- **Why it matters**
  - Not a high call-count problem, but an API-to-runner inefficiency problem.
- **Concrete fix**
  - Keep the preflight API call, but avoid the downstream checkout path when the API says there is no work.
- **Estimated reduction**
  - Same API count, but **substantial runner-min reduction**.

### 5. `cancel_on_pr_close` already has good retry hygiene; keep it as the template
- **Evidence**
  - Recent run `25369747613` and its summary show bounded retry/backoff for `git fetch` and rate-limit-aware handling on API calls.
- **Why it matters**
  - This is the cleanest current implementation of safe bounded retries.
- **Concrete fix**
  - Reuse this pattern across other utility workflows instead of maintaining workflow-specific wrappers.

## Prompt Cache & Memory System

### Current state
- **Prompt cache configured:** yes
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in `review_autofix` and related flows.
- **Cache-friendly prompt assembly exists:** yes
  - In `review_autofix` slow run `25353743396`, the workflow explicitly says it pre-assembles static system instructions so the provider can cache a stable prefix across runs.
- **Cache observability:** poor
  - Cache probe lines in `25324565713` and `25353743396` show:
    - `cache_enabled=true`
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

### What this implies
- The team has done the right structural work for cacheability, but cannot prove whether cache entries are:
  - being created,
  - being reused,
  - fragmenting because of prompt variance,
  - failing open silently.

### Likely cache-fragmentation causes
1. **Dynamic PR/run metadata leaking too early** into the prompt prefix.
2. **Repeated fallback reconstruction** of linked-issue / PR text state in late steps.
3. **Non-canonical ordering** of files/findings/reviewer outputs between similar runs.
4. **Branch-specific support-source/ref noise** affecting prompt prefix stability.

### Recommended improvements
1. **Emit real per-call cache counters**
   - Include token totals plus cache-create/cache-read counters and cache-hit bool.
   - **Impact:** unlocks evidence-based tuning; low-risk telemetry change.

2. **Keep the stable prefix truly stable**
   - Ensure run IDs, timestamps, transient branch refs, and volatile API outputs do not appear before the static instructions block.
   - **Impact:** likely reduces cache fragmentation; low-risk.

3. **Canonicalize dynamic sections**
   - Sort file lists, findings, and linked issue numbers before prompt rendering.
   - **Impact:** likely **5–15%** token reduction on repeated review patterns; medium confidence.

4. **Reuse cached PR/linkage artifacts across late steps**
   - The workflow already caches some of this; finish the job so late steps become reads rather than recomputation.
   - **Impact:** token, API, and latency savings together.

### Reliability note
- No parsed `AI_MEMORY_TELEMETRY` entries showed `fail_open: true`, but many logs and step names are labeled “fail-open”; verify the runtime payloads actually emit that field when the path is taken.

## Orchestrator Health

### What looks healthy
- Clarify/plan/implement/respond workflows are usually cheap no-ops:
  - `clarify` p50 **1s**
  - `plan` p50 **1s**
  - `implement` p50 **1s**
  - `orchestrate_clarify_respond` p50 **1s**
- No evidence in the sampled window of runaway clarify/respond loops or stuck terminal states.
- `push_attempts` in memory telemetry stayed almost entirely at **1**, so memory event recording is not thrashing.

### Recurring operational pain points
1. **Poller no-op overhead**
   - Healthy logic, unhealthy cost profile on empty cycles.
2. **Long recovery window**
   - Gate comments explicitly note the orchestrator stall cron can re-kick autofix on roughly a **30-minute** cadence.
3. **Bimodal review path**
   - The orchestration around PR reviews either exits fast or spends ~30+ minutes in reviewer panels.
4. **Post-merge validate dispatch is functional but still GH-API dependent**
   - Recent merged-PR run `25369747657` succeeded in **17s**, but the dispatch step still reconstructs linked issues via API/regex fallback.

### Smallest safe mitigations
- Add the no-op poll short-circuit first.
- Track a dedicated metric for `% poll cycles with has_work=false`.
- Track `review_autofix` split by mode:
  - comment-only reviewer path
  - full editor/autofix path
- Track `workflow_dispatch` retrigger counts from stable-release/editor smoke recovery separately from organic runs.

### Observable indicators to track
- `orchestrate_poll` median duration when `has_work=false`
- `review_autofix` median duration by gate mode
- retriggered `review_autofix` count per PR
- post-merge validate dispatch success rate
- zero-hit AI memory retrieval rate in `review_autofix`

## Pipeline Flow Bottlenecks

### Clarify → Plan → Implement
- **Observed state:** mostly not the bottleneck.
- **Evidence:** many grouped runs at the same timestamps end in **1–2s skipped** outcomes.
- **Interpretation:** command routing is noisy, but not materially slowing end-to-end delivery.

### Review / Autofix
- **Dominant bottleneck**
- **Evidence:** `review_autofix` slow/success runs cluster around **1,715–2,058s**, with p95 **2057.2s** for the family.
- **Overhead types**
  - compute: multi-model two-pass reviews
  - queueing: hosted-runner wait before the long job starts
  - retry/cancel: many superseded or cancelled runs
  - conflict overhead: smoke-canary resolver failures
- **Best fixes**
  1. skip pass 2 on comment-only paths
  2. pre-empt superseded runs earlier
  3. fix the canary conflict resolver

### Validate / Orchestrate loops
- **Observed state:** logic is mostly healthy; empty-cycle efficiency is poor.
- **Evidence:** no-op poll runs take **40–55s**.
- **Overhead types**
  - queueing + runner startup
  - unnecessary checkout/support staging on empty cycles
- **Best fix**
  - API-only preflight and early exit.

### CI
- **Observed state:** reliable but serial.
- **Evidence:** repeated **~10 min** single-job runs.
- **Overhead types**
  - compute serialization
  - some runner wait
- **Best fix**
  - parallelize independent check classes.

### Release / Stable gate
- **Observed state:** currently the sharpest reliability bottleneck.
- **Evidence:** `test_and_mark_stable` failed **2/2** in this window after **52–55 min** each.
- **Overhead types**
  - compute waste from late failure
  - retry/re-dispatch logic around editor verification
  - merge/conflict handling on a synthetic canary file
- **Best fix**
  - deterministic smoke-canary conflict handling before the expensive release loop reruns.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long comment-only reviewer runs: **1,715–2,058s** common, up to **2,987s** observed.
- `ci` monolithic lint lane: p50 **608s**.
- `orchestrate_poll` empty cycles still cost **40–55s**.
- `copilot_pull_request_reviewer` teardown/cleanup overhead: **239–406s**.

**Top failure modes**
- Stable-release smoke/editor verification fails at `Phase 4b`.
- Conflict resolver fails on `tests/e2e_smoke_canary.txt` in `review_autofix`.
- Nightly validation self-test fails with 2 of 3 fixtures failing, but logs under-report which ones.

**Highest-cost drivers**
- Two-pass 6-reviewer `review_autofix` panel on comment-only paths.
- Long CI wall time from serialized checks.
- Expensive release failures that occur late.
- Prompt-cache instrumentation present but not measurable.

**Top 3 prioritized actions**
1. **Fix the smoke-canary conflict resolver path** to unblock `test_and_mark_stable`.
2. **Disable pass 2 by default on comment-only `claude/*` review runs**.
3. **Short-circuit no-op `orchestrate_poll` before checkout/staging**.

## Metrics Appendix

### Overall repository metrics

| Repo | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 514 | 178 | 4 | 44 | 288 | 0.78% | 2s | 672s |

### Workflow family summary

| Workflow family | Total runs | Success | Failure | Cancelled | p50 | p95 | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| review_autofix | 69 | 26 | 1 | 42 | 49s | 2057.2s | Highly bimodal; comment-only review path is dominant hotspot |
| ci | 44 | 44 | 0 | 0 | 608s | 641.6s | Stable but serialized |
| orchestrate_poll | 33 | 33 | 0 | 0 | 48s | 53.4s | Many no-op cycles still expensive |
| copilot_pull_request_reviewer | 20 | 20 | 0 | 0 | 257s | 417.0s | Cleanup/runner waits dominate |
| test_and_mark_stable | 2 | 0 | 2 | 0 | 3221s | 3294.8s | Release blocker in this window |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 100s | 100s | 2 of 3 fixtures failed |
| workflow_log_analysis | 2 | 2 | 0 | 0 | 2834.5s | 2913.3s | Expensive background analysis |

### Representative long-run evidence

| Run ID | Workflow | Conclusion | Duration | Dominant issue |
|---|---|---|---:|---|
| 25368082752 | review_autofix | success | 2058s | Two-pass 6-reviewer comment-only review |
| 25365010380 | review_autofix | success | 2056s | Same pattern |
| 25353743396 | review_autofix | success | 2987s | Same pattern + cache telemetry still `na` |
| 25368082690 | ci | success | 605s | Monolithic `lint` job |
| 25366565178 | copilot_pull_request_reviewer | success | 406s | Cleanup artifacts + runner waits |
| 25324103531 | test_and_mark_stable | failure | 3303s | Phase 4b smoke/editor verify failed |
| 25347776357 | test_and_mark_stable | failure | 3139s | Same failure |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total parsed `AI_MEMORY_TELEMETRY` entries | 50 |
| `retrieve` operations | 11 |
| Retrieve hit rate | 9.1% (1/11) |
| Zero-hit retrieves | 10 |
| Avg `estimated_tokens` on retrieve | 2.5 |
| `keyword_method=none` | 10 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| `enabled=false` retrieves | 0 |
| `fail_open=true` retrieves | 0 |
| Max observed `push_attempts` | 2 |

### Prompt-cache observability summary

| Workflow / Run | Cache intended | Real token counters present? | Observed cache fields |
|---|---|---|---|
| review_autofix / 25324565713 | Yes | No | `cache_enabled=true`, all token/cache counters `na` |
| review_autofix / 25353743396 | Yes | No | same |
| review_autofix / 25368082752 | Yes | No usable totals | cache enabled, no real totals surfaced in deep dive |
| ci / 25368082690 | N/A for model-cost analysis | No | no model token totals emitted |
| orchestrate_poll / 25368600796 | N/A for model-cost analysis | No | no model token totals emitted |

### Observed token telemetry

| Source workflow | Observed values | Interpretation |
|---|---|---|
| workflow_log_analysis (`25324145530`, `25347801271`) | `tokens_used=170,953`, `189,772`, `214,279`, `218,559`, `221,799` | Token figures were present in analysis logs, but core hot-path workflows did not emit comparable totals |
| review_autofix | unavailable | biggest cost center lacks real token totals |
| ci | unavailable | no token totals emitted |
| test_and_mark_stable | unavailable | no token totals emitted |

### GH API hotspot summary

| Workflow / step | Observed pattern | Evidence run IDs | Likely avoidable calls |
|---|---|---|---:|
| review_gate | `pulls/{PR}`, `commits/{sha}`, paginated `pulls/{PR}/files` | 25324565713, 25369747657 | 1 paginated `/files` on many small PRs |
| review_autofix tail | repeated fallback PR/linkage reads inside same run | 25347801271 analysis deep dive | up to 3 PR reads/run |
| copilot cleanup | `actions/runs/{id}/artifacts` listing | 25366565178, 25363665314, 25362090734, 25359918526, 25357757494 | 1 call/run |
| orchestrate_poll preflight | issue-list preflight, then still full checkout on no-work cycles | 25368600796 | same API count, but large runner waste |
| cancel_on_pr_close | active-run lookup with bounded retries | 25369747613 | already acceptable |

### Data gaps

| Gap | Impact on analysis | Next collection step |
|---|---|---|
| No real token totals for core `review_autofix` runs | Cost ranking is directional, not dollar-precise | Emit per-call token/cache counters in hot workflows |
| Nightly validation self-test top-level log hides failed fixture names | Limits exact remediation advice | Surface fixture/stage names from summary JSON before exit |
| No direct GH API call counts by endpoint | Call-reduction estimates are pattern-based | Add lightweight endpoint/count aggregation per run |

## Deep Audit — Workflows & Scripts (2026-05-05)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `scripts/tg_helpers.sh:155-277`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — `tg_store_msg_id()` and `tg_store_phase_msg_id()` both implement a read-modify-write update against a shared tracking comment: they fetch recent issue comments, select the first matching marker, append the new Telegram ID with `sed`, and PATCH the whole body back. That sequence is not atomic. If two jobs send tracked alerts for the same issue/phase close together, both can read the same pre-update comment and the later PATCH overwrites the earlier one, dropping one message ID from the ledger. This repo exercises these helpers from multiple workflows (`clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `orchestrate_clarify_respond.yml`, `validate_process.sh`), so the race is not theoretical. Lost IDs mean `tg_cleanup_msgs()` will orphan Telegram messages because it only deletes IDs that survived the last write.  
  **Recommended fix** — Stop appending into a shared mutable comment. The safest pattern here is one immutable tracking comment per message ID, e.g. `<!-- tg_cleanup:12345 -->` / `<!-- tg_phase:clarify:12345 -->`, because the existing cleanup scanners already iterate all matching marker comments. If keeping a shared comment is required, add optimistic concurrency protection around the PATCH and retry on conflict instead of last-writer-wins replacement.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/internal-review.yml:91-118`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The `resolve-claude-branch-pr` gate uses raw `gh api` calls with fail-open defaults: `existing_pr` becomes empty on any API error, and `base_ref` falls back to `main`. Under `set -euo pipefail`, the command substitutions themselves do not fail the step because both calls are wrapped in `|| echo ...`. That means a transient 403/5xx/rate-limit event is interpreted as “no open PR exists,” so the workflow emits `proceed=true` and dispatches the no-PR claude-branch review path even when a real PR already exists. The normal `pull_request:synchronize` leg can then run in parallel, duplicating an expensive reviewer-panel run on the same branch.  
  **Recommended fix** — Replace both raw calls with `gh_retry`/`_safe_gh_jq` from `scripts/gh_helpers.sh`, and fail closed on lookup failure for this gate (`proceed=false` with a warning) rather than defaulting to “no PR.” If you also batch the default-branch lookup into the PR-existence query, keep the step’s outputs unchanged but derive them from one retried request.

- **ID** — `BUG-003`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:59-80`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The early “Check orchestrator metadata” step hard-depends on raw `gh api` calls for data that is already mostly present in the event payload. `ISSUE_PAYLOAD="$(gh api .../issues/${ISSUE_NUMBER})"` is not wrapped in `gh_retry`, so any transient GitHub API failure aborts the entire workflow before the memory dedupe, loop guard, or fail-open logic runs. The optional tracking-title probe at line 76 is also raw `gh api`. This produces stricter failure behavior than the rest of the repo’s GitHub-call contract and creates an avoidable single-point failure at the top of the workflow.  
  **Recommended fix** — Use `github.event.issue.body` / `github.event.issue.title` as the primary source for orchestrator detection, since this workflow is triggered from issue comments. Only fetch the tracking issue title when a tracking number is actually present, and do that through `gh_retry` so the smoke-alert suppression path cannot fail the whole workflow.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `.github/workflows/internal-review.yml:98-101`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — `resolve-claude-branch-pr` issues two separate GitHub reads on every `claude/**` push: one REST call for open PR existence and one REST call for the repo default branch. Those values are consumed together to produce one gate decision/output set.  
  **Current call count** — 2 calls per push (`GET /pulls?state=open&head=...` + `GET /repos/{repo}`)  
  **Proposed call count after fix** — 1 call per push  
  **Batching/reuse pattern to extend** — Follow the aliased GraphQL batching style used by `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`: fetch `repository.defaultBranchRef.name` and `pullRequests(headRefName: ..., states: OPEN, first: 1)` in one query.  
  **Recommended fix** — Collapse the two reads into one GraphQL query and feed both `existing_pr` and `base_ref` from that result. That also gives one retry boundary instead of two independent failure modes.

- **ID** — `BATCH-001`  
  **File path** — `.github/workflows/issue_pr_status.yml:288-349,503-512`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The main label-sync step already batch-fetches linked issue labels/bodies through `ORCH_QUERY`, but the later “Send PR merged Telegram alert” step loops `LINKED_ISSUE_NUMBERS` and re-fetches each issue body individually with `_safe_gh_jq` to decide whether the PR belongs to an orchestrator-managed issue. That is duplicate data retrieval in the same execution path: the earlier batch already knows which linked issues are tracking/managed.  
  **Current call count** — 1 GraphQL batch call + up to `N` REST calls in the merged-alert step, where `N` is the number of linked issues  
  **Proposed call count after fix** — 1 GraphQL batch call total  
  **Batching/reuse pattern to extend** — Reuse the existing `ORCH_QUERY` result in this workflow; if you want a helper, mirror the cached-batch contract style from `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — Export a step output or `GITHUB_ENV` flag derived from `ORCH_RESP` (`HAS_ORCHESTRATED_LINKED_ISSUE=true/false`, or serialized managed/tracking sets) and make the merged-alert step consume that instead of issuing per-issue body lookups.

- **ID** — `API-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1325-1475`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — Once `ELAPSED >= 600`, the Phase 4a review wait loop downloads the same job log twice in a single poll iteration: first into `LOG_FILE` at lines 1359-1362 for early-success/editor-noop scanning, then again at lines 1471-1472 solely to compute `LOG_SIZE`. The second download adds API and bandwidth cost without providing new data.  
  **Current call count** — 2 `/actions/jobs/{id}/logs` downloads per qualifying poll iteration  
  **Proposed call count after fix** — 1 log download per qualifying poll iteration  
  **Batching/reuse pattern to extend** — Reuse the already-downloaded `LOG_FILE`, just like the step already reuses `JOBS_JSON` instead of re-fetching jobs in the happy path.  
  **Recommended fix** — Keep `LOG_FILE` until after `LOG_SIZE=$(wc -c < "$LOG_FILE")`, then remove it. That eliminates one high-volume API call every loop iteration after the 10-minute mark.

- **ID** — `BATCH-002`  
  **File path** — `scripts/review_conflict_resolve.sh:91-123`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — `_maybe_dispatch_orchestrator_poll_on_failure()` probes for active poller runs with two separate `gh run list` calls: one for `in_progress`, then one for `queued`. Both calls hit the same workflow/repo scope and only differ by status filtering.  
  **Current call count** — 2 list-runs calls per resolver failure path  
  **Proposed call count after fix** — 1 list-runs call per resolver failure path  
  **Batching/reuse pattern to extend** — Reuse the single-call local filtering approach already implemented in `autofix_retrigger_has_inflight_peer()` in `scripts/gh_helpers.sh:1171-1228`.  
  **Recommended fix** — Fetch a small run window once, then filter locally for `queued`/`in_progress`. That keeps the dedupe logic identical while cutting one API round-trip from a failure-heavy path.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:309-335,457-483; .github/workflows/orchestrate_poll.yml:66-97; .github/workflows/review_autofix.yml:1254-1292; .github/workflows/comprehensive-test-and-release.yml:75-92,319-335`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The repo has multiple hand-rolled copies of the same `_rl_wait` + `_gh_retry`/`gh_retry` logic instead of using `scripts/gh_helpers.sh`. The duplicated blocks all parse `/rate_limit`, sleep until reset on 403/429-like errors, and apply exponential backoff otherwise, but they are already drifting in names, stderr handling, temp-file paths, and retry semantics. That fragmentation is exactly the kind of retry-policy divergence called out in the current report’s reliability section.  
  **Recommended fix** — Consolidate on `scripts/gh_helpers.sh` and add any missing helper surface there instead of cloning wrapper logic inline. A concrete module shape would be:
  - `gh_retry <gh subcommand...>`
  - `gh_retry_to_file <outfile> <gh subcommand...>`
  - `gh_api_json_to_file <outfile> <gh api ...>`
  
  Update callers in `cancel_on_pr_close.yml`, `mark-stable.yml`, `orchestrate_poll.yml`, `review_autofix.yml`, and `comprehensive-test-and-release.yml` to source the shared helper rather than redefining local retry functions.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/issue_pr_status.yml:69-131,467-498,555-590; .github/workflows/validate.yml:209-277`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Support-file staging is duplicated in several places with near-identical helper bodies: `checkout_support_ref`, `fetch_from_ref_or_local` / `copy_from_ref_or_local`, staging-root setup, `stable`→`main` fallback, and local-self-repo shortcuts. `issue_pr_status.yml` then duplicates the same support-clone pattern a second and third time inside its Telegram alert and cleanup steps instead of reusing the first staged checkout.  
  **Recommended fix** — Extract support staging into a shared module. The lowest-friction option is a shell helper such as `scripts/stage_workflow_support.sh` exposing:
  - `stage_support_checkout <wf_source> <requested_ref> <stage_root>`
  - `copy_from_support_or_local <repo_path> <target_path> <require_remote> <allow_main_fallback>`
  
  Callers to update: `validate.yml`, `issue_pr_status.yml` (both the initial memory-helper fetch and the later Telegram helper steps), and then `review_autofix.yml` if you want one canonical staging path repo-wide.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1602-2001`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The Phase 4b “Verify editor removed bait line” `run:` block still embeds a very large `${{ }}`-bearing shell body. Estimated current template body size is **20,849 chars**, leaving only **151 chars** of headroom before the 21,000-char hard failure. This is the same workflow family that has already tripped the expression ceiling multiple times.  
  **Recommended fix** — Extract the entire Phase 4b retry/poll/pytest orchestration into a dedicated script under `scripts/` and pass only env vars from YAML. This block is too close to the hard ceiling to safely evolve inline.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1187-1518`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — The Phase 4a review wait-loop block is also near the ceiling. Estimated current template body size is **19,700 chars**, leaving about **1,300 chars** of headroom. The block contains one `${{ }}` expression but a large amount of inline polling logic, comments, and failure handling, so even modest edits can push it over.  
  **Recommended fix** — Move the wait-loop into a script, e.g. `scripts/e2e_wait_for_review.sh`, and keep the workflow step to wiring inputs/outputs only.

- **ID** — `EXPR-003`  
  **File path** — `.github/workflows/review_autofix.yml:1251-1573`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — “Collect PR metadata” is still a large inline `${{ }}`-bearing block. Estimated current template body size is **16,437 chars**, with about **4,563 chars** of remaining headroom. It already contains retry logic, GraphQL fetches, three comment/review collection calls, Python post-processing, and diff capture in one step.  
  **Recommended fix** — Split this into at least two steps or extract it to `scripts/review_collect_pr_metadata.sh`. The natural split boundary is: (1) API fetch/caching, (2) comment-context/diff assembly.

- **ID** — `EXPR-004`  
  **File path** — `.github/workflows/validate.yml:183-476`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — “Fetch workflow support files” is another large interpolated `run:` block. Estimated current size is **16,485 chars**, leaving about **4,515 chars** of headroom. The block keeps growing because it mixes clone logic, fallback policy, schema copying, prompt copying, optional self-heal support, and `.gitignore` setup in one inline body.  
  **Recommended fix** — Extract the support-staging logic to a shared script or composite action. This is the same code family flagged under `DUP-002`, so one modularization fix can also remove the expression-risk hot spot.

- **ID** — `EXPR-005`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:813-1096`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The “Parse and post answer” block is above the 15k warning threshold. Estimated current size is **15,140 chars**, leaving about **5,860 chars** of headroom. The step contains multiple memory-guard branches, loop-break comment generation, tracked-alert handling, and processed-command completion writes in one inline body.  
  **Recommended fix** — Extract this path to a script such as `scripts/orchestrate_parse_and_post_answer.sh`, or split the loop-guard/escalation path from the happy-path answer-posting path.

No workflow currently exceeds the **800 KB** warning threshold for total file size; the largest audited workflow in this repo is `review_autofix.yml` at roughly **277,694 chars**, well below the 1 MB hard cap.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:4765-4771`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is defined but has no in-repo call sites. Repository-wide search only finds the definition, while writers route through `write_standalone_state_json()` and readers use cached comment JSON directly. That leaves an unexercised helper in the repo’s hottest shell script.  
  **Recommended fix** — Remove `read_standalone_state_json()` if the cached-comment path is the intended contract. If the wrapper is meant to stay, convert one real caller to it and document the pagination/fail-open behavior above the function.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/internal-review.yml:91-101; .github/workflows/orchestrate_clarify_respond.yml:59-76`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — Two early workflow gates bypass the repo-standard GitHub-call patterns in opposite ways: `internal-review.yml` uses raw `gh api ... || echo ""` fail-open defaults, while `orchestrate_clarify_respond.yml` uses raw `gh api` with hard-fail semantics. The rest of the repo generally standardizes on `gh_retry`/`_safe_gh_jq` from `scripts/gh_helpers.sh` or on event-payload reuse. This inconsistency is already causing divergent behavior: `BUG-002` turns transient API errors into duplicate review runs, while `BUG-003` turns them into premature workflow failures.  
  **Recommended fix** — Standardize early metadata gates on one contract: prefer event payload when available, otherwise use `scripts/gh_helpers.sh` and make the fail-open/fail-closed choice explicit in the step comment and outputs.

- **ID** — `SHELL-001`  
  **File path** — `scripts/check_external_branch_advance.sh:180-184`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `for sha in ${self_subject_shas}; do` relies on unquoted word splitting (`SC2086`). Today the values are expected to be commit SHAs, so the loop usually works, but the code is still brittle: any future delimiter change or unexpected whitespace will silently alter iteration behavior.  
  **Recommended fix** — Convert `self_subject_shas` to a newline-delimited stream and iterate with `while IFS= read -r sha; do ...; done`, or populate a bash array with `mapfile -t` and iterate over `"${array[@]}"`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-002, EXPR-001, EXPR-002 |
| Medium | 11 | BUG-001, BUG-003, API-001, BATCH-001, API-002, DUP-001, DUP-002, EXPR-003, EXPR-004, EXPR-005, CONSIST-001 |
| Low | 3 | BATCH-002, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 4 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 5 | Large |
| Medium/Low fixes | 4 | Medium |
