## Executive Summary

- **`test_and_mark_stable` is the highest-risk bottleneck: 3 of 4 runs failed (75% failure rate), each after ~50–64 minutes.** The failures cluster around `e2e-smoke-test` Phase 4b/Phase 7, where the PR is already closed or the editor/canary state is still corrupted when the verification step runs. Estimated impact: **cut release reruns by 50–75% and save ~50 minutes per avoided failed release**. Confidence: **high**.

- **`review_autofix` spends most of its time in long `codex-agent` runs, especially on the comment-only Claude branch review path.** Multiple successful runs still took **1,599–2,027s** (`25388139273`, `25391097516`, `25392711750`, `25394267845`) while explicitly saying *editor/commit/judge/auto-merge skipped*. Estimated impact: **2–10 minutes faster per long review run**, plus meaningful token savings. Confidence: **high**.

- **Unresolved merge conflicts are a direct, repeated reliability failure in `review_autofix`.** Failed runs `25370025320`, `25370115370`, and `25371432937` all end with `MERGE_CONFLICT: true`, `CONFLICT_RESOLVED: false`, and the canary file still containing conflict markers. Estimated impact: **eliminate 3 observed hard failures and reduce rerun churn on autofix PRs**. Confidence: **high**.

- **`orchestrate_poll` is mostly idle work, and two failures were pure queue starvation, not code failures.** Failed runs `25381014761` and `25383797907` both died after **903s** with only repeated “Waiting for a runner to pick up this job...” lines; recent successful polls show `has_work=false` and finish in **44–65s**. Estimated impact: **reduce idle runner occupancy by ~8–15s per idle poll and lower poller failure rate from 5.3% toward 0%**. Confidence: **high**.

- **Prompt cache is enabled but effectively unauditable, and AI memory retrieval is not helping.** In sampled `review_autofix` runs, `OPENROUTER_PROMPT_CACHE_DISABLED=false`, but cache counters are all `na`; across structured memory telemetry, `retrieve` hit rate was **0/6**, average `estimated_tokens` was **0**, and `keyword_method` was always `none`. Estimated impact: **medium token/latency upside once instrumented; current state blocks optimization**. Confidence: **high**.

- **`workflow_log_analysis` is a measurable token cost center.** Its own logs emitted `tokens_used` samples in the **170,953–229,172** range, and `summarize_unselected_runs` processed up to **92 of 100 targeted runs** in one sampled run (`25369803376`). Estimated impact: **save ~100k–200k tokens per analysis run with tighter summarization budget/sample policy**. Confidence: **medium**.

## Speed Optimizations

### 1. Build a lightweight path for `review_autofix` comment-only Claude branch reviews
**Type:** Critical-path win

- **Evidence**
  - Successful `review_autofix` runs `25388139273` (1,599s), `25391097516` (1,610s), `25392711750` (1,697s), `25394267845` (2,027s), and `25389584507` (1,672s) all report:
    - `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`
    - `REVIEWERS_SUCCESSFUL: 6`
  - Despite the skip, runtime is still dominated by `review / codex-agent (claude-branch-review)`.

- **Root cause**
  - The system is still running the heavy review scaffolding for a path that is explicitly not allowed to edit, commit, judge, or merge.

- **Exact change**
  - Split `review_autofix` into:
    1. **review-only path** for `claude-branch-review` comment-only mode
    2. **full autofix path** only when edits/commit/judge are actually needed
  - On the lightweight path, skip:
    - pre-editor merge/conflict machinery
    - commit/push setup
    - check-run polling if no edit will be attempted
    - editor-specific prompt/context assembly

- **Estimated time savings**
  - **2–5 minutes per long comment-only review run** from plumbing removal alone.
  - If paired with reviewer-panel downsizing in the Cost section, total savings could be larger.

- **Implementation risk**
  - **Low to medium.**
  - Safe if the route is keyed off the already-emitted gate condition (`claude-branch-review` comment-only path).

---

### 2. Parallelize the serialized `CI` “lint” workload
**Type:** Critical-path win

- **Evidence**
  - `ci` family: **68 runs**, **p50 617s**, **p95 650.9s**, no failures.
  - Recent runs `25388138974`, `25389584409`, `25392489757`, `25392711730`, `25394267792` all say the `lint` step dominated runtime for **~590–623s**.
  - The step currently bundles tests, coverage, and workflow/script validation.

- **Root cause**
  - Independent validation work is serialized into one long job.

- **Exact change**
  - Split current `lint` into parallel jobs:
    - `pytest + coverage`
    - workflow/script reference validation
    - formatting/linting/static checks
  - Keep a small fan-in summary job if needed for branch protection.

- **Estimated time savings**
  - **3–5 minutes per CI run** on wall-clock latency.
  - Indirectly reduces `review_autofix` wait on check-runs.

- **Implementation risk**
  - **Low.**
  - Behavior does not change; only job graph changes.

---

### 3. Add an idle preflight to `orchestrate_poll` and avoid full checkout on no-work cycles
**Type:** Critical-path/control-plane win

- **Evidence**
  - Recent successful poll runs `25397168454`, `25398925351`, `25400701992`, `25401994001`, `25403394957`, `25404534668`, `25405777759` all report:
    - `has_work=false`
    - durations of **44–65s**
  - In `25405777759`, `poll/Checkout repository` is the dominant step.
  - In `25404534668`, checkout uses `actions/checkout@v5` with `fetch-depth: 0` and fetches many tags.
  - Failed poll runs `25381014761` and `25383797907` never got past runner allocation and died at **903s**.

- **Root cause**
  - Every poll cycle pays for a full runner + checkout path even when there is no work.
  - Idle poll load increases queue pressure and makes the poller itself more likely to starve.

- **Exact change**
  - Add a lightweight API-only preflight before checkout:
    - if no work is pending, exit without checkout
    - if work exists, continue into checkout
  - When checkout is needed, use shallow fetch/no tags unless a specific phase requires full history.

- **Estimated time savings**
  - **~8–15s per idle poll run** immediately.
  - Additional queue-time reduction is likely but not directly quantifiable from the current window.

- **Implementation risk**
  - **Low to medium.**
  - Must verify the preflight matches current “has work” semantics.

---

### 4. Stop dispatching `plan` on already-closed issues
**Type:** Micro-optimization with high frequency

- **Evidence**
  - `plan` family has **148 runs**, but only **21 success** and **127 other/skipped**.
  - Several “successful” plan runs still spent **20–45s** only to say the issue was closed:
    - `25383023212`: “Issue #2137 is closed. Skipping planning run.” in **20s**
    - `25383021005`: “Issue #2136 is closed. Skipping planning run.” in **25s**
    - `25383021552`: “Issue #2135 is closed. Skipping planning run.” in **27s**
    - `25383020646`: “Issue #2134 is closed. Skipping planning run.” in **45s**

- **Root cause**
  - The closed-issue gate is happening after runner allocation and workflow startup.

- **Exact change**
  - Move the “issue still open?” check into the parent orchestrator/dispatcher before starting `internal-plan.yml`.

- **Estimated time savings**
  - **20–45s per closed-issue misfire**, plus reduced queue contention.

- **Implementation risk**
  - **Low.**

---

### 5. Avoid full tag/history fetches in release/version-resolution paths when only tag discovery is needed
**Type:** Micro-optimization

- **Evidence**
  - `test_and_mark_stable` and related release flows use full fetches:
    - `resolve-version` shows `fetch-depth: 0`, `fetch-tags: true`
    - sampled logs show many refs/branches being fetched during version resolution
  - Similar heavy tag fetching appears in recent `orchestrate_poll` checkout logs.

- **Root cause**
  - Tag discovery is coupled to full repository fetch.

- **Exact change**
  - Replace full checkout for version computation with targeted tag-only queries where possible.
  - Use `git ls-remote --tags` or equivalent minimal fetch in the version resolver.

- **Estimated time savings**
  - **~15–60s per release/version-resolution run**, depending on ref volume.

- **Implementation risk**
  - **Low to medium**, because release/version code is sensitive and should be validated carefully.

## Cost Optimizations

### 1. Reduce reviewer-panel spend on comment-only `review_autofix` runs
- **Evidence**
  - Long comment-only runs still invoke **6 reviewers** (`REVIEWERS_SUCCESSFUL: 6`) while skipping editor/commit/judge.
  - These runs take **1,599–2,027s** and are likely the highest recurring LLM spend among hot-path workflows.
  - `MODEL_EDITOR: openai/gpt-5.3-codex`; multiple external reviewer models are also loaded.

- **Root cause**
  - The expensive multi-reviewer panel is being used even when the path cannot apply code changes.

- **Exact change**
  - For comment-only branch review:
    - start with a **smaller panel** (e.g. 2–3 reviewers)
    - escalate to full 6-reviewer panel only on disagreement, workflow edits, or high-risk files
  - Keep the full panel for merge-blocking/full-autofix paths.

- **Estimated savings**
  - **Largest likely token/dollar reduction in the active pipeline**, but not precisely measurable because total prompt/completion counts are not emitted for these runs.
  - Expect **meaningful savings on every long comment-only review**.

- **Quality-risk notes**
  - **Medium risk** if applied globally.
  - **Low to medium risk** if gated only to comment-only/non-editing paths.

---

### 2. Tighten `workflow_log_analysis` summarization budget
- **Evidence**
  - In sampled log-analysis runs, `summarize_unselected_runs` telemetry was emitted by `workflow_log_analysis`.
  - Run `25369803376` reported `summarized: 92`, `targeted: 100`.
  - Observed `tokens_used` values in log-analysis telemetry ranged from **170,953** to **229,172**.

- **Root cause**
  - The workflow is spending significant tokens broadening coverage over unselected runs.

- **Exact change**
  - Lower the unselected-run sample budget, or skip summarization for workflow families that already have deep-dive coverage in `errors/slow/recent`.
  - Prefer summarizing only:
    - families with no deep dive in the window
    - families with elevated failure or p95 duration

- **Estimated savings**
  - **~100k–200k tokens per workflow-log-analysis run**.

- **Quality-risk notes**
  - **Low risk** if deep-dive coverage remains intact.

---

### 3. Make prompt-cache telemetry real before tuning prompts further
- **Evidence**
  - In failed `review_autofix` runs `25370025320`, `25370115370`, `25371432937` and slow runs `25353743396`, `25355461484`:
    - `OPENROUTER_PROMPT_CACHE_DISABLED: false`
    - but probe logs show `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`
  - One review log also notes the editor prompt contains a **~32K-token system-instruction prefix**, implying cacheability could matter a lot.

- **Root cause**
  - Cache is enabled, but metrics are missing, so hits/misses cannot be optimized.

- **Exact change**
  - Emit real values for:
    - `prompt_tokens`
    - `completion_tokens`
    - `total_tokens`
    - `cache_creation_input_tokens`
    - `cache_read_input_tokens`
  - Then move the most stable system/policy prefix ahead of volatile PR-specific context.

- **Estimated savings**
  - **Potentially high on `review_autofix`, currently unquantifiable**.
  - Without telemetry, no evidence-based cache tuning is possible.

- **Quality-risk notes**
  - **Low risk** instrumentation change.

---

### 4. Prevent avoidable reruns from known merge-conflict and release-race modes
- **Evidence**
  - `review_autofix` had 3 hard failures from unresolved conflicts.
  - `test_and_mark_stable` failed 3 of 4 times after ~50 minutes each.

- **Root cause**
  - Expensive workflows are being retried after deterministic, known failure modes.

- **Exact change**
  - Add targeted fail-fast detection:
    - generated canary-file conflict => regenerate file rather than run generic conflict resolver
    - closed/merged PR before bait verification => short-circuit with explicit race classification

- **Estimated savings**
  - **High indirect savings** from avoided reruns and less repeated AI work.

- **Quality-risk notes**
  - **Low**, because these are targeted guards for already-known bad states.

## Reliability Improvements

### 1. Add deterministic conflict handling for `tests/e2e_smoke_canary.txt`
- **Failure evidence**
  - Failed `review_autofix` runs `25370025320`, `25370115370`, and `25371432937` all end with:
    - `MERGE_CONFLICT: true`
    - `CONFLICT_RESOLVED: false`
    - the file still contains conflict markers:
      ```
      <<<<<<< HEAD
      run_id: 25369768571
      =======
      run_id: alt-25369768571
      >>>>>>> origin/main
      ```
  - The step fails with `Conflict resolver failed after retries.`

- **Root cause category**
  - Merge/conflict resolution failure on a generated/spec-driven file.

- **Exact fix**
  - Special-case this file in the autofix conflict handler:
    - if the conflicted path is `tests/e2e_smoke_canary.txt`, regenerate it from the authoritative spec template instead of doing textual merge resolution
    - alternatively, regenerate after merge if conflict markers are detected

- **Expected reliability impact**
  - **High** for the currently observed failure class; should eliminate the 3 repeated hard failures in this window.

- **Rollback/fail-open considerations**
  - Safe rollback: fall back to generic resolver if the spec file is unavailable.
  - Prefer fail-open comment/report over hard failure if regeneration cannot be performed.

---

### 2. Eliminate the PR-close race in `test_and_mark_stable`
- **Failure evidence**
  - `25347776357`: failure at `Phase 4b: Verify editor removed bait line`
  - `25369768571`: failure at `Phase 4b: Verify editor restored canary (pytest + retry)`
  - `25375729485`: failure at `Phase 7: Close PR and verify cancel_on_pr_close fires`
  - Logs explicitly say:
    - `Editor Bait. FAILED (pr_already_closed) — PR was merged/closed before bait injection; editor never invoked. See Phase 3c log.`
    - comments in the harness identify an auto-merge/deterministic-skip race

- **Root cause category**
  - Test harness race / orchestration race.

- **Exact fix**
  - During the e2e bait/cancel-on-close phases:
    - temporarily suppress auto-merge/deterministic skip on the test PR, or
    - force the test harness to re-open/recreate the test path when PR state becomes closed before the bait/cancel verification phases
  - Keep the explicit `pr_already_closed` classification, but stop treating it as a normal editor failure.

- **Expected reliability impact**
  - **Very high** for release validation; this is the dominant cause of release failures in the sample.

- **Rollback/fail-open considerations**
  - If suppression logic misbehaves, fall back to explicit race classification and soft fail rather than consuming another 30+ minutes.

---

### 3. Pass `github-token` into the dynamic Copilot reviewer workflow or fail fast before dependent jobs
- **Failure evidence**
  - `copilot_pull_request_reviewer` run `25389586417` failed in `Prepare` with:
    - `Error: Input required and not supplied: github-token`
  - The same error also appears in `Upload results`.
  - The environment shows `GH_TOKEN:` and `GITHUB_TOKEN:` blank.

- **Root cause category**
  - Misconfiguration / missing required auth input.

- **Exact fix**
  - Wire `${{ github.token }}` explicitly into each `actions/github-script@v8` step in the dynamic workflow.
  - Also gate cleanup/upload jobs on token availability to avoid cascading failures.

- **Expected reliability impact**
  - **High** for this family; likely removes the observed failure entirely.

- **Rollback/fail-open considerations**
  - If token is intentionally unavailable, mark the workflow as skipped or degraded rather than failed.

---

### 4. Increase visibility on nightly validation self-test fixture failures before changing behavior
- **Failure evidence**
  - `nightly_validation_selftest` run `25356037835` ended with:
    - `validation-selftest: fixtures=3 passed=1 failed=2`
    - exit code 1
  - No fixture-level failure names were visible in the provided excerpt.

- **Root cause category**
  - Insufficient observability / limited failure detail.

- **Exact fix**
  - Emit failing fixture IDs and summary lines directly into the main job log before artifact upload.
  - Keep artifact upload, but surface the names inline so operators do not need artifact fetch for first triage.

- **Expected reliability impact**
  - **Medium**, because it reduces diagnosis time; root cause is not proven from this single sampled run.

- **Rollback/fail-open considerations**
  - Logging-only change; very low risk.

## AI Memory Health

- **Telemetry found:** yes, but only in a subset of deep-dive runs.
- **Structured operations observed:** **49**
  - `record-run-event`: **34**
  - `retrieve`: **6**
  - `record-candidate`: **5**
  - `summarize_unselected_runs`: **4**

### Retrieval effectiveness
- **Retrieve hit rate:** **0.0%** (**0/6** had `records_selected > 0`)
- **Average `estimated_tokens`:** **0.0**
- **`keyword_method` distribution:** `none` **6/6**, `plain` **0**, `llm` **0**
- **`enabled: false` retrieves:** **0**
- **`fail_open: true` retrieves:** **0**
- **High push retry counts in memory ops:** **none observed**; all structured memory push attempts were **1**

### Evidence
- Failed `review_autofix` runs:
  - `25370025320`
  - `25370115370`
  - `25371432937`
- Slow `review_autofix` runs:
  - `25353743396`
  - `25355461484`

Each sampled retrieve looked like:
- `{"enabled": true, "estimated_tokens": 0, "keyword_method": "none", "records_selected": 0, "op": "retrieve", ...}`

### Assessment
- Memory recording is happening.
- Memory retrieval is **not helping reviewers in the sampled window**.
- The system is not failing open or disabled; it is simply retrieving nothing useful.

### Recommendation
1. Verify why retrieval is running with `keyword_method: none`.
2. Add a lightweight retrieval debug line that includes:
   - candidate count before filtering
   - retrieval budget
   - why `keyword_method` fell back to `none`
3. Expand telemetry emission coverage: many recent successful runs had **no visible `AI_MEMORY_TELEMETRY` lines**, so health reporting is currently partial.

## GH API Call Audit

### High-volume / high-redundancy patterns

#### 1. `review_autofix` duplicates PR metadata fetches across `gate` and `codex-agent`
- **Evidence**
  - `review / gate` in failed runs `25370025320`, `25370115370`, `25371432937` calls:
    - `gh api repos/${REPOSITORY}/pulls/${PR_NUMBER}`
    - `gh api repos/${REPOSITORY}/commits/${PR_HEAD_SHA}`
    - `gh api --paginate repos/${REPOSITORY}/pulls/${PR_NUMBER}/files`
  - `review / codex-agent` then calls:
    - `gh api repos/${REPOSITORY}/pulls/${PR_NUMBER}` again
    - `gh api repos/shubhodeep1/coding-workflows` for default branch
    - `gh api graphql` for linked issues
    - comment POSTs
    - check-run polling

- **Redundancy issue**
  - PR state, file list, and linked-issue data are partly re-fetched after already being computed upstream.

- **Concrete change**
  - Persist gate outputs as a JSON context file and pass them into `codex-agent`.
  - Reuse already-fetched PR metadata and linked issues instead of re-calling REST/GraphQL.

- **Estimated call-count reduction**
  - **~4–8 API calls per `review_autofix` run**, before accounting for avoided check-run re-polls.

- **Rate-limit risk reduction**
  - **Medium**, mostly by eliminating repeated per-run metadata lookups.

---

#### 2. `review_autofix` check-run polling is potentially the largest API multiplier
- **Evidence**
  - The workflow documents:
    - `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200`
    - `CHECK_RUNS_POLL_INTERVAL_SECS: 20`
    - each iteration uses `gh api --paginate --slurp "/repos/{repo}/commits/{sha}/check-runs?per_page=100"`
  - This is explicitly logged in failed runs `25370025320`, `25370115370`, and `25371432937`.

- **Redundancy issue**
  - Polling persists even on paths that later skip editor/commit behavior.

- **Concrete change**
  - Skip or sharply shorten check-run polling when:
    - gate says comment-only Claude branch review
    - no code changes will be attempted
  - Snapshot once instead of waiting the full budget for review-only paths.

- **Estimated call-count reduction**
  - Potentially **dozens of check-run API calls per long run**.

- **Rate-limit risk reduction**
  - **High**, even though no 429s were observed in this sample.

---

#### 3. `copilot_pull_request_reviewer` uses paginated file enumeration and artifact listing with `retries: 0`
- **Evidence**
  - Recent successful runs `25387716796`, `25394269998` logged:
    - `github.paginate(github.rest.pulls.listFiles, ...)`
    - `gh api /repos/shubhodeep1/coding-workflows/actions/runs/.../artifacts`
  - `actions/github-script@v8` in both `Prepare` and `Upload results` uses `retries: 0`.

- **Redundancy issue**
  - Not obviously duplicated, but brittle and unbuffered against transient failures.

- **Concrete change**
  - Keep pagination, but:
    - reuse the file list across downstream jobs
    - set `retries` to a small non-zero value for transient GitHub failures

- **Estimated call-count reduction**
  - **Small to medium**.
  - Larger gain is reliability rather than raw call count.

- **Rate-limit risk reduction**
  - **Low to medium**.

---

#### 4. No 429 or secondary rate-limit events were visible in the supplied window
- **Assessment**
  - Current problem is **redundancy and avoidable polling**, not observed hard rate limiting.
  - More than half the value here is simply reducing background API noise.

### Repo-specific API hygiene notes
- No repository-specific API hygiene rules were present in the supplied telemetry.
- Best current candidates for batching/reuse are all in `review_autofix`.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Enabled:** yes
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` in sampled `review_autofix` runs
- **Measurable:** no
  - cache probe lines emitted:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

### Memory retrieval behavior
- Retrieval exists, but it is ineffective in the sample:
  - **0/6 retrieves hit**
  - all used `keyword_method: none`
  - all reported `estimated_tokens: 0`

### Likely cache-fragmentation causes
This is partly an inference from the logs, not a direct counter:
- very large stable system prefix
- highly dynamic PR comments / linked issues / check-run context
- reviewer outputs and prior-review context differing run-to-run
- comment-only review path still assembling broad context even when no edit occurs

### Concrete improvements
1. **Instrument cache counters first**
   - Emit real token/cache fields before further tuning.
2. **Stabilize prompt prefix**
   - Keep the large static policy/system block byte-stable.
   - Push volatile material later in the prompt.
3. **Use a smaller prompt shape for review-only paths**
   - Especially for `claude-branch-review` comment-only mode.
4. **Make retrieval debuggable**
   - Explain why `keyword_method` becomes `none`.
   - Record candidate counts before selection.

### Estimated impact
- **Tokens:** likely medium-to-high on `review_autofix`, but currently not measurable.
- **Latency:** moderate, especially across repeated runs on the same PR.
- **Reliability:** improved by separating stable prompt/cache behavior from volatile run context.

## Orchestrator Health

### What looks healthy
- Recent `orchestrate_poll` successes (`25397168454` through `25405777759`) consistently report:
  - `job_status: success`
  - `has_work: false`
  - `push_attempts: 1`
- Successful `clarify` runs such as `25382966707`, `25382969984`, `25382971814` complete in **84–96s** and report `Clarification completed: auto_answered_by_orchestrator`.

### Recurring pain points
1. **Queue starvation**
   - `orchestrate_poll` failures `25381014761` and `25383797907` are pure queue timeout failures.
2. **Too many child workflows that start only to skip**
   - `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have very high `other/skipped` counts.
3. **Closed-issue work still consumes runners**
   - `plan` runs on closed issues and burns 20–45s before skipping.
4. **Occasional multi-push completion behavior**
   - Clarify log summaries reported `push_attempts: 3` in `25382971814` and `25382967753`, suggesting some completion writes are retrying outside the memory-op telemetry set.

### Smallest safe mitigations
- Add upstream gating before dispatching child workflows.
- Add idle backoff / preflight for poller.
- Track queue-wait percentiles separately from execution time.
- Track “skip-after-run-start” rate by family.

### Observable indicators to track
- `% poll cycles with has_work=false`
- `% child workflows skipped after runner allocation`
- poll runner queue wait p50/p95
- `clarify` / orchestrator push_attempts > 1
- `review_autofix` merge-conflict rate
- `test_and_mark_stable` race-classified failures vs genuine product regressions

## Pipeline Flow Bottlenecks

### 1. Queueing overhead
- **Dominant in:** `orchestrate_poll`, `plan`, `clarify`, short maintenance/status workflows
- **Evidence**
  - `orchestrate_poll` failures were 903s queue timeouts.
  - Many short runs explicitly waited for runners.
- **Fix order**
  1. Reduce idle poll work
  2. stop dispatching closed-issue plan runs
  3. reduce unnecessary child workflow fan-out

### 2. Compute-heavy review loop
- **Dominant in:** `review_autofix`
- **Evidence**
  - `p95` duration **2035.7s**
  - multiple runs at **1,599–2,027s**
- **Root causes**
  - oversized comment-only review path
  - multi-reviewer fan-out
  - check-run polling
  - conflict-resolution retries

### 3. Release/e2e retry and race overhead
- **Dominant in:** `test_and_mark_stable`
- **Evidence**
  - **75% failure rate**
  - failures land after **2,999–3,359s**
- **Root causes**
  - PR closed before bait/cancel-on-close phases
  - editor verification still sees corrupted canary state

### 4. CI serialization
- **Dominant in:** `ci`
- **Evidence**
  - `lint` dominates **~10 minutes** consistently
- **Root cause**
  - tests/coverage/script validation serialized into one job

### 5. Merge/conflict overhead
- **Dominant in:** `review_autofix`
- **Evidence**
  - repeated unresolved canary conflicts
- **Root cause**
  - generic merge resolver on a spec-driven generated file

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` release validation at **p50 3,249s**, **p95 3,759.35s**
- `review_autofix` long-tail runs at **p95 2,035.7s**
- `ci` serialized `lint` at **p50 617s**
- idle `orchestrate_poll` cycles consuming runners despite `has_work=false`

**Top failure modes**
- release e2e PR-close/editor-bait race (`25347776357`, `25369768571`, `25375729485`)
- unresolved merge conflicts in `review_autofix` (`25370025320`, `25370115370`, `25371432937`)
- poller queue starvation (`25381014761`, `25383797907`)
- missing `github-token` in dynamic Copilot workflow (`25389586417`)

**Highest-cost drivers**
- multi-reviewer `review_autofix` comment-only path
- `workflow_log_analysis` unselected-run summarization budget
- CI’s monolithic 10-minute `lint`
- repeated reruns from deterministic failure modes

**Top 3 prioritized actions**
1. **Fix `test_and_mark_stable` PR-close race** and classify race failures separately from genuine editor regressions.
2. **Add deterministic canary-file conflict regeneration** in `review_autofix`.
3. **Split `ci` into parallel jobs** and create a **lightweight review-only `review_autofix` path**.

## Metrics Appendix

### Overall repository metrics

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 916 | 291 | 10 | 57 | 558 | 1.09% | 155.3 | 2.0 | 643.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `test_and_mark_stable` | 4 | 1 | 3 | 0 | 0 | 75.0% | 3331.8 | 3249.0 | 3759.4 |
| `review_autofix` | 95 | 39 | 3 | 52 | 1 | 3.16% | 570.2 | 54.0 | 2035.7 |
| `ci` | 68 | 68 | 0 | 0 | 0 | 0.0% | 614.2 | 617.0 | 650.9 |
| `orchestrate_poll` | 38 | 36 | 2 | 0 | 0 | 5.26% | 106.0 | 51.0 | 310.6 |
| `clarify` | 180 | 25 | 0 | 0 | 155 | 0.0% | 14.7 | 1.0 | 100.1 |
| `plan` | 148 | 21 | 0 | 0 | 127 | 0.0% | 10.4 | 1.0 | 89.9 |
| `implement` | 148 | 16 | 0 | 5 | 127 | 0.0% | 22.3 | 1.0 | 176.8 |
| `copilot_pull_request_reviewer` | 27 | 26 | 1 | 0 | 0 | 3.70% | 210.8 | 212.0 | 372.7 |
| `workflow_log_analysis` | 4 | 4 | 0 | 0 | 0 | 0.0% | 2883.3 | 2859.5 | 3239.8 |

### Notable failed runs

| Run ID | Workflow family | Duration (s) | Failure point |
|---|---|---:|---|
| `25347776357` | `test_and_mark_stable` | 3139 | `e2e-smoke-test` → `Phase 4b: Verify editor removed bait line` |
| `25369768571` | `test_and_mark_stable` | 3359 | `e2e-smoke-test` → `Phase 4b: Verify editor restored canary (pytest + retry)` |
| `25375729485` | `test_and_mark_stable` | 2999 | `e2e-smoke-test` → `Phase 7: Close PR and verify cancel_on_pr_close fires` |
| `25370025320` | `review_autofix` | 1295 | `review / codex-agent` → `Run Codex resolver, validate, stage, commit` |
| `25370115370` | `review_autofix` | 1836 | `review / codex-agent` → `Run Codex resolver, validate, stage, commit` |
| `25371432937` | `review_autofix` | 637 | `review / codex-agent` → `Run Codex resolver, validate, stage, commit` |
| `25381014761` | `orchestrate_poll` | 903 | no step body executed; queue timeout |
| `25383797907` | `orchestrate_poll` | 903 | no step body executed; queue timeout |
| `25389586417` | `copilot_pull_request_reviewer` | 42 | `Prepare` → `Prepare` |
| `25356037835` | `nightly_validation_selftest` | 100 | `validation-selftest` → `Run validation self-test matrix` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Structured memory ops observed | 49 |
| `retrieve` ops | 6 |
| Retrieve hit rate | 0.0% |
| Avg `estimated_tokens` on retrieve | 0.0 |
| `keyword_method=none` | 100% of retrieves |
| `enabled=false` retrieves | 0 |
| `fail_open=true` retrieves | 0 |
| `record-run-event` ops | 34 |
| `record-candidate` ops | 5 |

### Prompt cache telemetry coverage

| Family / runs | Cache enabled seen? | Measurable cache reads/writes? | Notes |
|---|---|---|---|
| `review_autofix` (`25370025320`, `25370115370`, `25371432937`, `25353743396`, `25355461484`) | Yes | No | `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` all `na` |
| `orchestrate_poll` (`25405777759`) | Yes | No quantitative counters | only `OPENROUTER_PROMPT_CACHE_DISABLED=false` visible |
| Most other hot-path workflows | Unknown / not emitted | No | telemetry gap |

### Observed token totals

| Workflow / source | Observed token signal |
|---|---|
| `workflow_log_analysis` deep-dive logs | emitted `tokens_used` samples in the **170,953–229,172** range |
| `review_autofix` conflict-resolver failure tails (`25370025320`, `25370115370`, `25371432937`) | local “tokens used” values of **3,485**, **504**, **408** respectively, but not full run totals |
| `ci`, `test_and_mark_stable`, `orchestrate_poll` | no full prompt/completion/total token telemetry in sampled logs |

### GH API hotspots observed

| Workflow family | API pattern | Evidence |
|---|---|---|
| `review_autofix` | repeated `pulls/{PR}`, `commits/{SHA}`, `pulls/{PR}/files` | `review / gate` in `25370025320`, `25370115370`, `25371432937` |
| `review_autofix` | check-run polling `commits/{sha}/check-runs?per_page=100` | documented in same failed runs via env/help text |
| `review_autofix` | GraphQL linked-issues lookup + issue comment POSTs | `25370115370`, `25371432937` |
| `copilot_pull_request_reviewer` | `github.paginate(pulls.listFiles)` + artifacts API | `25387716796`, `25394269998`, `25389586417` |
| `issue_pr_status` / `review_autofix` | GraphQL `closingIssuesReferences` | recent runs `25405947633`, `25405947651` |

If you want, I can turn this into a shorter exec-ready action plan ranked by “do this next week” vs “later cleanup.”
