## Executive Summary

- **Fix the stable-release smoke gate first.** All 4 sampled `test_and_mark_stable` runs failed (`run_ids` `25300046587`, `25305535590`, `25308071039`, `25310399716`), each after **40–53 minutes** total, and all failures converged on `e2e-smoke-test` → `Phase 4b: Verify editor removed bait line`. Expected impact: **save 25–55 minutes per failed release attempt** and restore releaseability. **Confidence: high**.
- **The biggest routine latency sink is still CI lint.** `ci` has `p50 615s`, `p95 648s`, and repeated runs show `lint` consuming **~543–635s** almost end-to-end (`25316638748`, `25314580423`, `25312583943`, `25311004437`). Expected impact: **2–5 minutes off every CI run** if split/cached/guarded. **Confidence: high**.
- **`review_autofix` is expensive and cancellation-heavy.** The family has **75 total runs, 37 cancelled, p95 1652.6s**; long runs (`25313222796` 1877s, `25303629616` 1730s, `25316638883` 1586s) are dominated by long-lived review/editor paths, often for comment-only or cancelled outcomes. Expected impact: **10–20 minutes saved on hot PRs and fewer wasted runner-minutes**. **Confidence: medium-high**.
- **Implement failures are wasting retries on stale or mismatched context.** Error runs `25293932552`, `25293940145`, `25293966619`, `25294005792` all died inside `Run Codex implementation`; logs show repeated “no actionable output” loops, and one run explicitly hit a **plan/code mismatch** before consuming multiple retries. Expected impact: **150–300s and tens of thousands of tokens per failed implement run**. **Confidence: high**.
- **Memory retrieval works for implement, but is ineffective for review paths.** Across deep-dive logs, `retrieve` hit rate was **68.8% (11/16)**, but every sampled reviewer retrieval in `review_autofix` returned **0 records** with `keyword_method:"none"` (`25300219172`, `25303629616`, `25313222796`). Expected impact: **small latency win, moderate quality/reliability win** if reviewer retrieval is fixed or disabled when nonproductive. **Confidence: high**.
- **Prompt-cache instrumentation is enabled but not observable enough to optimize.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present in implement/review flows, yet cache probe lines in `review_autofix` report `cache_enabled=true` with `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. Expected impact: **better cost tuning and cache-hit diagnosis**, but only after telemetry is made complete. **Confidence: high**.
- **GH API usage is not rate-limiting today, but there are clear redundancy hotspots.** Hotspots include repeated review/run polling in smoke tests, repeated checkout+diff fetching in `review_autofix`, artifact cleanup lookups in Copilot review, and repeated workflow dispatches in post-merge validation. Expected impact: **20–40 fewer calls per release/smoke path and lower secondary-rate-limit risk**. **Confidence: medium**.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Stop failing release smoke after 40–53 minutes on known Phase 4b conditions
- **Evidence**
  - `test_and_mark_stable` family: **4 runs, 4 failures, avg 2755.8s, p50 2697.5s, p95 3161.5s**.
  - Failing runs: `25300046587` (2424s), `25305535590` (2474s), `25308071039` (2921s), `25310399716` (3204s).
  - In `25310399716`, `step-005-e2e-smoke-test.log` ends with `Editor failed to remove bait line E2E_EDITOR_BAIT_25310399716` and `✗ Editor Bait. FAILED (bait_remained)`.
  - The same log also documents an earlier fast-failable race: `PR #... is already ... before bait could be injected`.
- **Root cause**
  - The workflow allows the PR/review path to proceed deep into the pipeline before discovering either:
    1. the PR was already merged/closed before bait injection, or
    2. the editor path never removed the bait line.
- **Exact change**
  - Split Phase 4b into two earlier guards:
    - **Guard A:** fail immediately after bait injection preconditions if the PR is already merged/closed.
    - **Guard B:** hold deterministic auto-merge until the bait-removal review path completes.
  - Treat “PR already closed before bait injection” as a distinct terminal outcome, not as a later bait-removal failure.
- **Estimated time savings**
  - **25–55 minutes per failed stable-release attempt**.
- **Implementation risk**
  - **Low-medium.** Behavior-preserving if implemented as earlier terminal checks; does not require changing release semantics, only failure timing.
- **Type**
  - **Critical-path win.**

### 2. Reduce CI `lint` dominance
- **Evidence**
  - `ci` family: **59 runs**, **avg 611.4s**, **p50 615s**, **p95 648.3s**.
  - Repeated runs show `lint` dominates almost all runtime:
    - `25316638748`: `lint` ~543s of 552s.
    - `25314580423`: `lint` ~606s of 615s.
    - `25312583943`: `lint` ~635s of 636s.
    - `25311004437`: `lint` ~630s of 642s.
- **Root cause**
  - The `lint` job is effectively the whole workflow; other steps are negligible.
- **Exact change**
  - Split `lint` into smaller independently cacheable chunks and short-circuit unchanged scopes:
    - workflow/script reference validation separate from Python lint/test coverage;
    - only run expensive lint/test subsets when matching paths change.
- **Estimated time savings**
  - **120–300s per CI run** if path-guarded; more on documentation/light-workflow PRs.
- **Implementation risk**
  - **Low** if path filters are additive and conservative.
- **Type**
  - **Critical-path win.**

### 3. Eliminate repeated full-checkout overhead in `review_autofix`
- **Evidence**
  - Long review runs:
    - `25313222796` = 1877s
    - `25303629616` = 1730s
    - `25316638883` = 1586s
  - In `25313222796` and `25303629616`, the `review_codex-agent_claude-branch-review` logs show **multiple `actions/checkout@v5` sequences** and repeated support-source checkout/bootstrap phases before the main work begins.
- **Root cause**
  - The job repeatedly rehydrates workflow support sources and performs bootstrap/checkouts inside the same run.
- **Exact change**
  - Consolidate support-source checkout/bootstrap into a single prepared workspace per run and reuse it for reviewer/editor/judge paths.
  - Pass derived artifacts (prompt files, support file staging, resolved refs) forward instead of recalculating.
- **Estimated time savings**
  - **30–90s per long `review_autofix` run**.
- **Implementation risk**
  - **Medium.** Needs careful preservation of fail-open behavior when support-source fallback is required.
- **Type**
  - **Critical-path win.**

### 4. Move no-op gating ahead of heavy checkout in `orchestrate_poll`
- **Evidence**
  - `orchestrate_poll` runs are only ~44–46s, but a large fraction is setup:
    - `25321758145`: checkout dominates ~9s.
    - Similar no-work runs `25317908642`, `25319968616`, `25316050158` complete with `has_work:"false"`.
  - Recent logs show hosted-runner wait plus repository/support checkout before discovering no work.
- **Root cause**
  - No-op detection happens after expensive repository/setup steps.
- **Exact change**
  - Front-load the minimal issue/work probe before repository checkout and support-source staging.
  - Only do the full checkout when `has_work=true` or a write path is needed.
- **Estimated time savings**
  - **8–12s per no-work poll cycle**; with 38 runs in sample, this is meaningful aggregate runner time.
- **Implementation risk**
  - **Low-medium.** Needs a narrow preflight path that avoids needing repo-local scripts.
- **Type**
  - **Critical-path win** for the orchestrator loop.

### 5. Stop heavy failure-log analysis from re-checking out the whole repo for static analysis
- **Evidence**
  - `workflow_log_analysis` runs are very long: **2046s, 2091s, 2556s, 2832s**.
  - `25310429821` `api-redundancy` and `deep-audit` logs show checkout fetching a very large set of branches before analysis begins.
- **Root cause**
  - Analysis workflows pay full repository checkout cost even though their dominant inputs are already-collected workflow logs and summaries.
- **Exact change**
  - Use sparse/targeted fetch or skip branch-heavy fetch for steps that only need the current tree and log artifacts.
- **Estimated time savings**
  - **30–120s per log-analysis run**.
- **Implementation risk**
  - **Low.**
- **Type**
  - **Secondary critical-path win.**

### 6. Micro-optimization: reduce runner cleanup and package-removal overhead in review jobs
- **Evidence**
  - `review_autofix` logs include aggressive disk cleanup (`apt-get remove`, tool cache cleanup) before work starts.
- **Root cause**
  - Disk reclamation is paying startup tax on every long review run.
- **Exact change**
  - Only enable aggressive cleanup when expected artifact/prompt volume exceeds a threshold; skip for comment-only or claude-branch-review-only paths.
- **Estimated time savings**
  - **20–60s per affected review run**.
- **Implementation risk**
  - **Medium**, because disk-pressure regressions are possible.
- **Type**
  - **Micro-optimization.**

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1. Cut implement retries after first stale-context/no-change failure
- **Evidence**
  - Error runs `25293932552`, `25293940145`, `25293966619`, `25294005792` all ended inside `Run Codex implementation`.
  - `25293966619` explicitly shows:
    - a plan/code mismatch on `.github/workflows/orchestrate_poll.yml`,
    - repeated retries,
    - final failure `Codex implement failed after 5 attempts`.
  - The deep-audit workflow itself summarized visible token burn on that run as **42,989 visible tokens**, including a **23,176-token** failed attempt.
- **Root cause**
  - Retry loop reuses stale context after the first structurally invalid or no-change patch attempt.
- **Exact change**
  - After the first “announced edit with no diff” or plan/code mismatch:
    - refresh file context from disk,
    - shrink prompt to only failed target files + last validation error,
    - cap retries lower for repeated no-change outcomes.
- **Estimated savings**
  - **~40k tokens and 150–300s per stale-context implement failure**.
- **Quality-risk notes**
  - Low if refresh only triggers after structural no-change signals.

### 2. Reduce `review_autofix` token burn on smoke-test editor empty-output loops
- **Evidence**
  - `25310399716` evidence is echoed in later logs: all 6 editor attempts returned 0-byte stdout in smoke mode; bait remained.
  - `25313222796` log notes the motivating failure used **49,934 tokens** without removing the bait line.
  - `25303629616` documents another failure mode: smoke-test reasoning override (`reasoning=none`) leaked into the later conflict resolver path.
- **Root cause**
  - Retrying the same editor model/shape after a known empty-output signature.
- **Exact change**
  - On first empty-stdout smoke-test editor failure:
    - switch to a minimal forced-write prompt,
    - require `apply_patch`/direct-write,
    - avoid replaying the full reviewer/editor prompt stack multiple times.
- **Estimated savings**
  - **20k–50k tokens per smoke-editor failure run**.
- **Quality-risk notes**
  - Low for smoke-only paths; do not generalize this to normal PRs without guardrails.

### 3. Trim workflow-log-analysis summarization budget
- **Evidence**
  - `workflow_log_analysis` `summarize_unselected_runs` telemetry:
    - `25310429821`: `summarized=98`, `tokens_used=221799`
    - `25308096512`: `summarized=93`, `tokens_used=218559`
    - `25305555946`: `summarized=88`, `tokens_used=214279`
    - `25300062692`: `summarized=76`, `tokens_used=170953`
- **Root cause**
  - The summarizer spends ~171k–222k tokens per run summarizing up to 100 unselected runs, even when many runs are trivial skips/no-ops.
- **Exact change**
  - Exclude obviously low-signal runs from summarization input:
    - `duration<=2s`,
    - `conclusion=skipped`,
    - condition-only gate runs with no work.
  - Keep those as metric rows, not LLM summaries.
- **Estimated savings**
  - **50k–120k tokens per log-analysis run**.
- **Quality-risk notes**
  - Low if skip/no-op runs remain counted in metrics tables.

### 4. Avoid paying full reviewer-panel cost on comment-only review paths
- **Evidence**
  - `review_autofix` success runs `25316638883` (1586s) and `25315301289` (1414s) show:
    - `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`
- **Root cause**
  - Full multi-reviewer panel still runs even when downstream editor/commit stages are intentionally skipped.
- **Exact change**
  - Introduce a reduced reviewer set or single-pass summary mode for comment-only/claude-branch review paths.
- **Estimated savings**
  - **30–60% of model cost** on those long comment-only runs.
- **Quality-risk notes**
  - Medium; keep full panel on higher-risk or merge-blocking paths.

### 5. Improve prompt-cache observability before further model tuning
- **Evidence**
  - Cache probes in `25300219172`, `25313222796`, `25303629616` show `cache_enabled=true` but token/cache counters are `na`.
- **Root cause**
  - Cache is likely enabled, but instrumentation is too incomplete to prove hit quality or fragmentation.
- **Exact change**
  - Emit concrete cache-hit metrics for creation/read tokens and stable prefix length.
  - Then stabilize prompt prefixes for reviewer/editor prompts.
- **Estimated savings**
  - **Unknown now; likely moderate** once measurable.
- **Quality-risk notes**
  - None; telemetry-only first.

## Reliability Improvements

Ranked by expected failure-rate/rerun-rate reduction.

### 1. Split the two stable-release failure modes and fail earlier
- **Failure evidence**
  - `test_and_mark_stable` failed 4/4 sampled runs.
  - `25310399716`: `Phase 4b: Verify editor removed bait line` failed with bait still present.
  - Earlier runs also show PR already merged/closed before bait injection.
- **Root cause category**
  - **Workflow race / state-transition bug**.
- **Exact fix**
  - Add explicit pre-bait PR-open check and defer auto-merge until smoke review completes.
- **Expected reliability impact**
  - Should convert the current **100% sampled failure rate** on this family into either success or fast, diagnosable failure.
- **Rollback / fail-open**
  - Fail closed for stable-release gating; this is a release workflow and should not silently pass.

### 2. Fix implement retry-loop convergence
- **Failure evidence**
  - 4 implement failures, including:
    - repeated `Codex produced no actionable output ... agent loop is stuck in exploration`,
    - `request_user_input in exec mode`,
    - `Codex implement failed after 5 attempts`.
- **Root cause category**
  - **Agent retry policy / stale prompt context**.
- **Exact fix**
  - Refresh context after first no-change output; abort earlier on repeated identical failure signatures; route ambiguity straight back to clarify.
- **Expected reliability impact**
  - Lower implement rerun rate and clearer operator diagnosis.
- **Rollback / fail-open**
  - Safe to fail open only for posting diagnostics; not safe to silently continue implementation.

### 3. Prevent smoke-test reasoning overrides from contaminating later resolver paths
- **Failure evidence**
  - `25303629616` explicitly documents: smoke-test step set `reasoning=none`, later shared config caused the conflict resolver to inherit it and fail.
- **Root cause category**
  - **Config leakage across stages**.
- **Exact fix**
  - Scope smoke-test editor overrides to the smoke editor invocation only; restore default reasoning before resolver/judge paths.
- **Expected reliability impact**
  - Reduces false failures in `review_autofix` on smoke PRs and conflict-resolution runs.
- **Rollback / fail-open**
  - Low-risk; scoped env/config reset.

### 4. Validate reviewer-manifest / support-source prerequisites before invoking editor
- **Failure evidence**
  - `25300219172` and long review runs show repeated support-source fallback warnings and later repeated manifest/editor failures.
- **Root cause category**
  - **Precondition validation gap**.
- **Exact fix**
  - Preflight required prompt/support files and reviewer manifest inputs before invoking the expensive editor path.
- **Expected reliability impact**
  - Prevents expensive retries on structurally invalid runs.
- **Rollback / fail-open**
  - Fail open only where current behavior already tolerates missing optional support files; fail closed on missing required prompt files.

### 5. Make nightly validation self-test actionable
- **Failure evidence**
  - `nightly_validation_selftest` has **1 run, 1 failure** (`25299383150`) with `fixtures=3 passed=1 failed=2`.
- **Root cause category**
  - **Broken test fixture coverage / harness regression**.
- **Exact fix**
  - Persist the failing fixture names into the job summary/artifact and fail on first regression with explicit fixture IDs.
- **Expected reliability impact**
  - Faster MTTR on validation harness regressions.
- **Rollback / fail-open**
  - Fail closed; this is a self-test.

## AI Memory Health

- **Telemetry present:** yes, from implement, review, orchestrate_poll, memory_maintenance, and workflow_log_analysis deep-dive logs.
- **Retrieve hit rate:** **68.8%** (`11/16` retrieves had `records_selected > 0`).
- **Average `estimated_tokens` on retrieve:** **19.2** tokens; **max 28**.
- **`keyword_method` distribution:** `plain=11`, `none=5`, `llm=0`.
- **0-record retrieves**
  - Reviewer retrievals returned 0 records in:
    - `25300219172`
    - `25303629616`
    - `25313222796`
  - Workflow-analysis logs also surfaced 0-record reviewer retrieves for:
    - `25305555946`
    - `25310429821`
- **`enabled:false` retrieves:** none observed.
- **`fail_open:true` retrieves:** none observed in sampled telemetry.
- **High push retry counts**
  - `push_attempts=2` observed on:
    - implement `25293966619` (`record-run-event`)
    - workflow_log_analysis `25300062692` (`record-run-event`)
- **Operational read**
  - Implement memory retrieval is working and lightweight: all sampled implement retrieves used `keyword_method:"plain"`, `records_selected:1`, `estimated_tokens:28`.
  - Reviewer retrieval is not adding value in sampled long review runs: every sampled reviewer retrieve had `keyword_method:"none"`, `records_selected:0`, `estimated_tokens:0`.
- **Recommendation**
  - For `review_autofix`, either:
    1. improve reviewer retrieval query generation so it can select records, or
    2. skip reviewer retrieval entirely when it would run with `keyword_method:none`.
  - Add a run-summary counter for reviewer retrieve hit/miss so teams can track whether memory is helping.

## GH API Call Audit

### 1. Release/smoke path is over-polling review runs
- **Evidence**
  - `25310399716` `e2e-smoke-test` repeatedly polls review workflow status, jobs, live logs, and bait state while waiting for the review run.
  - The same step contains rate-limit-aware wrappers and repeated run-state checks around the bait-triggered review path.
- **Pattern**
  - Repeated workflow-run polling plus repeated job/log fetches for a single target run.
- **Concrete change**
  - Fetch review run metadata once per poll interval, reuse the same payload for:
    - status,
    - failed steps,
    - current step,
    - head SHA validation.
  - Only fetch live logs when status changed or every Nth poll.
- **Estimated reduction**
  - **10–20 API calls per stable-release smoke run**.
- **Rate-limit risk reduction**
  - Moderate; this is one of the longest call-heavy flows.

### 2. `review_autofix` retry helper still probes `/rate_limit` on the slow path
- **Evidence**
  - `25313222796` and `25303629616` call `gh api -i /rate_limit` inside `gh_retry` to determine reset time.
- **Pattern**
  - Meta-call inside retry helper; can amplify call count under repeated transient failures.
- **Concrete change**
  - Prefer parsing headers/body from the failed call when available; only fall back to `/rate_limit` if reset data is missing.
- **Estimated reduction**
  - Small in normal operation; **meaningful during transient API turbulence**.
- **Rate-limit risk reduction**
  - Medium during degraded GitHub API periods.

### 3. Post-merge validate dispatch performs repeated per-PR lookups
- **Evidence**
  - `25324039493` post-merge step uses:
    - `gh api graphql` for `closingIssuesReferences`,
    - `gh api repos/.../pulls/...`,
    - repeated `gh workflow run`.
- **Pattern**
  - PR metadata and linked-issue resolution are split across calls.
- **Concrete change**
  - Merge PR title/body and linked issue data into one GraphQL query and dispatch validation from the single resolved issue set.
- **Estimated reduction**
  - **2–4 calls per merged PR**.
- **Rate-limit risk reduction**
  - Small but cheap to implement.

### 4. Copilot review still does per-run artifact cleanup lookups
- **Evidence**
  - Copilot review runs `25312146090`, `25312857787`, `25314582143`, `25316297209` all hit `gh api /repos/.../actions/runs/.../artifacts`.
  - `Prepare` also uses `github.rest.pulls.get` plus `github.paginate pulls.listFiles`.
- **Pattern**
  - Cleanup and PR file enumeration are done separately on every run.
- **Concrete change**
  - Skip artifact cleanup lookup when no artifact-producing step ran, and reuse PR file metadata across prepare/cleanup if both need it.
- **Estimated reduction**
  - **1–3 calls per run**.
- **Rate-limit risk reduction**
  - Low.

### 5. Workflow-log-analysis is correctly finding API hygiene issues, but itself is checkout-heavy
- **Evidence**
  - `25310429821` deep audit explicitly calls out repeated `gh workflow run`, `gh api`, artifact loops, and PR refetching as concentration points.
- **Pattern**
  - Analysis is valuable, but the repo checkout itself is expensive.
- **Concrete change**
  - Keep the audit logic, but reduce branch-heavy fetch volume before static analysis.
- **Estimated reduction**
  - Mostly runner time, not API count.

### Repo-specific API hygiene alignment
The repository’s own review instructions explicitly require:
- batch GraphQL over per-item REST,
- cycle-local caches in orchestrator loops,
- fail-open on cache miss.

Observed hotspots that still merit cleanup:
- repeated run polling in smoke/review flows,
- repeated PR metadata fetches across post-merge/prepare paths,
- `/rate_limit` helper probing,
- artifact cleanup calls even on lightweight runs.

## Prompt Cache & Memory System

### Prompt cache behavior
- `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears consistently in implement/review flows, so cache-aware behavior is intended.
- Cache probe lines exist in:
  - `25300219172`
  - `25303629616`
  - `25313222796`
- But the probe output is not diagnostic enough:
  - `prompt_tokens=na`
  - `completion_tokens=na`
  - `total_tokens=na`
  - `cache_creation_input_tokens=na`
  - `cache_read_input_tokens=na`

### What this implies
- Cache instrumentation is on, but **cache hit/miss effectiveness is not measurable** from current logs.
- That blocks cost tuning and makes fragmentation analysis mostly inferential.

### Likely fragmentation causes
- Repeated dynamic prompt preambles in `review_autofix`.
- Multi-stage support-file regeneration and repeated prompt-file assembly inside one run.
- Different review/editor/resolver phases sharing large but slightly shifting prompts.
- Smoke-test overrides that mutate config/prompt shape mid-run.

### Concrete improvements
1. **Emit real cache counters**
   - Add non-`na` values for prompt, total, cache-read, and cache-create tokens.
   - Impact: unlocks cost and latency optimization.
2. **Stabilize prompt prefixes**
   - Keep invariant instructions and tool rules first.
   - Push dynamic PR/review state later.
   - Impact: likely moderate cache hit-rate improvement.
3. **Deduplicate prompt assembly within `review_autofix`**
   - Build shared invariant sections once, then append stage-specific suffixes.
   - Impact: lower prompt variance and lower assembly overhead.
4. **Separate smoke-test override from general review config**
   - Avoid modifying shared config files that later stages inherit.
   - Impact: reliability first, cache consistency second.

### Memory system effectiveness
- Implement memory retrieval is productive.
- Reviewer memory retrieval is consistently unproductive in sampled deep dives.
- Recommendation: measure reviewer retrieval hit rate as a first-class KPI and disable/short-circuit when `keyword_method:none` would yield zero records.

## Orchestrator Health

### Observable health
- `orchestrate_poll` itself is healthy in sampled recent runs:
  - `25317908642`, `25319968616`, `25321758145` all succeeded in **44–46s**.
  - Telemetry shows `poll_started` and `poll_completed` with `has_work:"false"` and `push_attempts:1`.
- Clarify/plan/implement/respond wrappers are frequently skipped quickly:
  - `clarify`, `plan`, `implement`, `orchestrate_clarify_respond` all have many **1s skipped runs**.
  - This is efficient, but also indicates a high volume of event churn.

### Pain points
1. **No-work poll cycles still do too much setup**
   - checkout/support staging before discovering no work.
2. **Wave health is hard to read from top-level telemetry**
   - Poll completion is logged, but not enough about queue depth, stalled issue counts, or recovery action rates in the sampled logs.
3. **Continuation/retrigger logic is complex**
   - Repo docs/logs mention continuation dispatch, peer bypass, stall recovery, merged-PR guards; these are robust but operationally hard to reason about.

### Smallest safe mitigations
- Track and surface per poll:
  - `has_work`,
  - active issue count,
  - stalled issue count,
  - recovery action count,
  - merged-PR guard activations.
- Add “no-work fast path” before checkout.
- Record continuation dispatch counts vs. actual productive successor runs.

### Indicators teams can track
- `orchestrate_poll` no-work ratio.
- Average checkout time on no-work poll runs.
- Stalled issue recoveries per poll cycle.
- Number of continuation dispatches that result in productive successor runs.
- Count of skipped clarify/plan/implement runs relative to real issue throughput.

## Pipeline Flow Bottlenecks

### Clarify → Plan
- **Bottleneck type:** low compute, high event churn.
- Evidence: `clarify`, `plan`, `orchestrate_clarify_respond`, `implement` all show many 1–2s skipped runs.
- Recommendation: keep behavior, but reduce trigger fan-out where multiple wrappers wake up only to skip.

### Plan → Implement
- **Bottleneck type:** retry/token overhead on failures.
- Evidence: implement failures loop on no-change/stale context, with multiple attempts before terminal error.
- Recommendation: shorter convergence path after first structural failure.

### Implement → Review/Autofix
- **Bottleneck type:** compute-heavy and cancellation-heavy.
- Evidence: `review_autofix` p95 **1652.6s**, many cancellations, repeated comment-only long runs.
- Recommendation: reduce reviewer scope on comment-only paths; consolidate setup; separate smoke overrides from shared config.

### Review/Autofix → Validate
- **Bottleneck type:** merge/conflict and editor-empty-output overhead.
- Evidence:
  - smoke-test bait-removal failures,
  - conflict resolver failures when smoke reasoning override leaks,
  - editor empty-output loops.
- Recommendation: special-case smoke editor path and isolate config.

### Validate / Release / Promote
- **Bottleneck type:** queueing + long failure detection.
- Evidence:
  - stable-release failures consume 40–53 minutes before failing,
  - promote run `25324054089` notes hosted runner wait,
  - forward-merge run is fast when no-op.
- Recommendation: fail earlier on deterministic release blockers and guard auto-merge timing.

### Queueing overhead
- Seen repeatedly across `ci`, `review_autofix`, `copilot_pull_request_reviewer`, `orchestrate_poll`.
- Recommendation: prioritize shortening long-running jobs first; that indirectly reduces concurrent queue pressure.

### Retry overhead
- Implement retry loops and GH API wrappers are the main visible retry tax.
- Recommendation: retry less when failure signature is deterministic; retry smarter when transient.

### Merge/conflict overhead
- Present in `review_autofix` conflict/merge-resolve flows.
- Recommendation: restore normal reasoning for resolver paths after smoke override and validate inputs before invoking expensive conflict resolution.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `ci` `lint` dominates runtime: typical **~9–10.5 minutes**.
- `review_autofix` long-tail runs: **~23–31 minutes** on slow samples.
- `test_and_mark_stable` currently fails after **~40–53 minutes**.

**Top failure modes**
- Stable-release smoke gate fails at Phase 4b bait removal.
- Implement loops fail after repeated no-change/stale-context retries.
- Smoke-specific reasoning override can break later conflict-resolver behavior.

**Highest-cost drivers**
- CI lint runtime.
- `review_autofix` multi-reviewer/comment-only runs.
- Workflow-log-analysis summarization budget (`170k–222k` tokens/run).
- Implement stale-context retries.

**Top 3 prioritized actions**
1. **Fix stable-release smoke race and Phase 4b failure timing**
   - Highest release impact; fastest operational payoff.
2. **Shorten implement retry convergence after first no-change failure**
   - High token and latency savings with low behavior risk.
3. **Refactor `review_autofix` setup and comment-only reviewer scope**
   - Best combined speed/cost improvement on active PR traffic.

## Metrics Appendix

### Overall run metrics

| Scope | Total Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| All sampled workflows | 1000 | 282 | 10 | 41 | 667 | 1.0% | 108.3 | 1.0 | 623.0 |

### Key workflow-family metrics

| Workflow Family | Total | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 59 | 59 | 0 | 0 | 0 | 611.4 | 615.0 | 648.3 |
| review_autofix | 75 | 35 | 1 | 37 | 2 | 424.7 | 48.0 | 1652.6 |
| test_and_mark_stable | 4 | 0 | 4 | 0 | 0 | 2755.8 | 2697.5 | 3161.5 |
| implement | 176 | 17 | 4 | 4 | 151 | 23.3 | 1.0 | 171.0 |
| orchestrate_poll | 38 | 38 | 0 | 0 | 0 | 50.1 | 45.0 | 86.0 |
| clarify | 207 | 25 | 0 | 0 | 182 | 12.2 | 1.0 | 94.9 |
| plan | 176 | 21 | 0 | 0 | 155 | 16.2 | 1.0 | 136.0 |
| workflow_log_analysis | 4 | 4 | 0 | 0 | 0 | 2381.3 | 2323.5 | 2790.6 |
| copilot_pull_request_reviewer | 23 | 23 | 0 | 0 | 0 | 193.5 | 165.0 | 399.5 |

### Notable run durations

| Run ID | Workflow | Conclusion | Duration (s) | Key bottleneck/failure |
|---|---|---|---:|---|
| 25310399716 | test_and_mark_stable | failure | 3204 | `e2e-smoke-test` Phase 4b bait removal failed |
| 25308071039 | test_and_mark_stable | failure | 2921 | same Phase 4b failure |
| 25313222796 | review_autofix | success | 1877 | long review/editor path |
| 25303629616 | review_autofix | success | 1730 | long review path; smoke override leaked into resolver |
| 25316638883 | review_autofix | success | 1586 | comment-only reviewer panel still long |
| 25314580423 | ci | success | 615 | `lint` dominated |
| 25312583943 | ci | success | 636 | `lint` dominated |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events observed | 70 |
| `record-run-event` events | 31 |
| `retrieve` events | 16 |
| `processed-command-check` | 8 |
| `processed-command-claim` | 8 |
| `record-candidate` | 3 |
| `summarize_unselected_runs` | 4 |
| Retrieve hit rate | 68.8% |
| Avg retrieve estimated tokens | 19.2 |
| Max retrieve estimated tokens | 28 |
| Retrieve keyword method: `plain` | 11 |
| Retrieve keyword method: `none` | 5 |
| Retrieve keyword method: `llm` | 0 |
| Retrieves with `enabled:false` | 0 |
| Retrieves with `fail_open:true` | 0 |

### Retrieve breakdown by role/run

| Run ID | Workflow Context | Role | Records Selected | Est. Tokens | Keyword Method |
|---|---|---|---:|---:|---|
| 25293932552 | implement | implementation | 1 | 28 | plain |
| 25293940145 | implement | implementation | 1 | 28 | plain |
| 25293966619 | implement | implementation | 1 | 28 | plain |
| 25294005792 | implement | implementation | 1 | 28 | plain |
| 25300219172 | review_autofix | reviewer | 0 | 0 | none |
| 25303629616 | review_autofix | reviewer | 0 | 0 | none |
| 25313222796 | review_autofix | reviewer | 0 | 0 | none |

### Workflow-log-analysis summarization token usage

| Run ID | Targeted Runs | Summarized | Skipped Empty Logs | Tokens Used | Model |
|---|---:|---:|---:|---:|---|
| 25300062692 | 100 | 76 | 24 | 170,953 | openai/gpt-5.4-mini |
| 25305555946 | 100 | 88 | 12 | 214,279 | openai/gpt-5.4-mini |
| 25308096512 | 100 | 93 | 7 | 218,559 | openai/gpt-5.4-mini |
| 25310429821 | 100 | 98 | 2 | 221,799 | openai/gpt-5.4-mini |

### Prompt-cache observability

| Run ID | Workflow | Cache Enabled | Token Fields Present? | Observation |
|---|---|---|---|---|
| 25300219172 | review_autofix | true | No (`na`) | cache probe exists but not measurable |
| 25303629616 | review_autofix | true | No (`na`) | same |
| 25313222796 | review_autofix | true | No (`na`) | same |

### GH API hotspot summary

| Pattern | Count in Deep-Dive Logs | Distinct Runs | Notes |
|---|---:|---:|---|
| `gh workflow run` | 138 | 11 | heavy in release/post-merge flows |
| `gh api graphql` | 44 | 7 | linked issue / PR graph lookups |
| `gh pr diff` | 33 | 3 | review diff fallback/hot path |
| `gh api /repos` | 19 | 4 | artifact and repo metadata lookups |
| `github.rest.pulls.get` | 3 | 2 | Copilot review / post-merge |
| `github.paginate github.rest.pulls.listFiles` | 1 | 1 | Copilot review prepare step |

### Queue/wait observations

| Workflow | Evidence |
|---|---|
| ci | multiple runs note “Job is waiting for a hosted runner to come online.” |
| review_autofix | long runs often include runner wait before gate/review starts |
| orchestrate_poll | recent run `25321758145` waited for hosted runner before ~44s successful no-work poll |
| copilot_pull_request_reviewer | prepare/cleanup jobs show runner pickup waits |

If you want, I can turn this into a **prioritized implementation checklist** mapped to specific workflow/script files next.
