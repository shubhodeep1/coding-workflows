## Executive Summary

- **Repeated workflow-defect reruns are the largest avoidable loss.** PR `#4323` reached `editor_empty_noop` streak `10`; four sampled runs (`35878367998`, `35892683442`, `35923766050`, `35933627432`) consumed **19,657 seconds / 5.46 runner-hours**, at least **11.01M reported tokens**, plus **12 usage-unavailable model calls**. **Impact: very high; confidence: high.**
- **Reviewer outages are misclassified as editor failures.** Runs `35923766050` and `35933627432` had **0/6 successful reviewers**, yet invoked the editor and finalized as `editor_empty_noop`; run `35933627432` spent **3,415 seconds (91%)** in `Run reviewer models`. **Impact: high; confidence: high.**
- **Startup failures dominate review failure count but have no diagnosis.** **26/32 review failures (81.3%)** ended in zero seconds with no jobs; 12 selected archives predictably returned HTTP 404. **Impact: high diagnostic blind spot; confidence: high.**
- **One transient push failure wasted the window’s largest single token spend.** Run `35868679924` completed six reviewers and three editor attempts, used **27.25M tokens (34.2% of OpenRouter volume)**, then failed on GitHub’s `Internal Server Error`; only non-fast-forward failures were retryable. **Impact: very high; confidence: high.**
- **CI and pollers dominate non-model runner time.** CI p50 was **2,548s**; poller p50 was **326s** on a five-minute schedule, totaling about **4.61 runner-hours** across 51 runs. **Impact: medium-high; confidence: high for duration, medium for root cause.**
- **Cache, Semble, and memory retrieval are positive signals.** Explicit cache-hit samples averaged **80.6%**; 12/12 sampled memory retrieves hit; Semble had zero runtime fallbacks. **Impact: protective; confidence: medium due sampled coverage.**

## Speed Optimizations

| Rank | Finding and evidence | Root cause | Exact change | Estimated saving | Risk |
|---|---|---|---|---|---|
| 1 — critical path | PR `#4323` repeated the same fingerprint through streak `10`. The existing head-scoped cap stopped run `35903666305`, but later heads repeated the failure. | Breaker resets when the PR head changes, although the workflow defect persists. | Add a **PR + failure-fingerprint cooldown** across head SHAs. Allow one probe after workflow-support changes, then block until the fingerprint changes or an operator clears it. Log `fingerprint_scope`, prior heads, count, support ref, and decision. | Each avoided repeat saves roughly **62–114 minutes** in this sample. | Low; retain the existing disable override and one-canary escape. |
| 2 — critical path | Two runs had 0/6 reviewers; `35933627432` spent 3,415s in reviewer execution. | No quorum/unavailable-provider gate; final reason prioritizes editor no-op. | Stop before editor when reviewer quorum is impossible. Emit `reviewer_quorum_unavailable`, successful/failed counts, provider classes, and remaining-slot state. | Saves the editor/finalization tail and prevents automatic reruns of an unusable result; potentially minutes per outage. | Low-medium; feature-flag and preserve partial finalize when quorum is met. |
| 3 — critical path | Six full reviewers run at `xhigh`; `REVIEW_TIER_RESOLVER_ENABLED=false` and `REVIEWER_RISK_TIER_ENABLED=0`. | Every eligible diff receives full-tier review. | Canary the existing lite/standard tiers for documentation-only and small diffs, preserving the always-full path regex. | About **50% fewer reviewer calls per eligible run**; wall-time reduction depends on slowest-provider removal. | Medium quality risk; compare finding recall before broad rollout. |
| 4 | CI run `35929972089` failed after ~17 minutes, after 107.24s and 318.84s serial suites. | The focused Phase 4b contract test executes late. | Move `Test-and-mark-stable review-blocked budget contract tests` before long suites. | Approximately **17 minutes fail-fast saving** on recurrence; successful duration unchanged. | Low. |
| 5 | Sweep runs dispatched three PRs while two to three were already active; run `35931394370` waited 1,565s before cancellation. | Per-PR dedup exists, but there is no global review concurrency ceiling. | Add `SWEEP_MAX_ACTIVE_REVIEWS` and `SWEEP_MAX_DISPATCHES`; defer oldest remaining candidates to the next 30-minute tick. Log active total and deferred count. | Removes observed 26-minute queue waits and cancellations under bursts. | Low; affects only dispatch timing. |
| 6 — micro | Readiness runs `35938391551` and `35938697803` checked out the repository before deciding the branch was inapplicable. | Applicability test occurs after checkout. | Move the `orchestrator/project-*` predicate to a pre-checkout job/step. | Single-digit seconds per inapplicable run. | Low. |

## Cost Optimizations

1. **Retry transient push-side server failures.** Run `35868679924` lost 27.25M tokens after `remote: Internal Server Error`. Extend the push retry classifier to bounded 5xx/internal-server errors, rechecking remote branch freshness before each retry. Potential saving: the complete cost of a repeated run; no quality risk.

2. **Broaden the identical-failure breaker.** The four sampled `editor_empty_noop` runs consumed at least 11.01M known tokens, while 12 calls had unavailable usage. A PR-scoped breaker prevents model spend after the defect is already proven. Quality risk is low with a workflow-ref canary and operator override.

3. **Enable risk-based model selection.** Six reviewers at `xhigh` are not justified for every small diff. Existing tier variables permit a backward-compatible canary. Expected saving: roughly 50% of reviewer calls on eligible diffs; preserve full review for workflows, scripts, prompts, contracts, and memory paths.

4. **Enforce graceful prompt compaction at 140k tokens.** Runs `35878367998` and `35892683442` reached 162,529–165,025 prompt tokens, **16–18% above the warning threshold**, then failed in the editor path. Drop optional pass-1 artifacts, duplicate comments, and low-ranked historical context first. Expected affected-call input reduction: **14–15%**. Quality risk: medium; never truncate current diff or unresolved findings.

5. **Do not optimize Semble away.** Across the broader telemetry, Semble issued 23 queries and injected/logged 250,653 bytes; runtime fallback count was zero. Detailed reviewer-context queries took only 345–570ms, so Semble is not a latency driver. Add before/after prompt-byte telemetry to prove its reduction benefit.

6. **Serena produced no measurable value or cost.** Queries, fallbacks, and probes were all zero, while logs showed `SERENA_AVAILABLE=false`. Do not expand rollout until probe and per-tool response-byte telemetry exists.

7. **Implement cost remains under-attributed.** One implement run (`35931354231`) recorded 13 Codex calls and 1.326M tokens. Add per-call role, continuation number, input size, and outcome before changing its model or reasoning level.

## Reliability Improvements

1. **Diagnose zero-job startup failures before requesting logs.**
   - Evidence: 26 zero-duration failures named `.github/workflows/review_autofix.yml`; 12/12 selected log downloads returned 404.
   - Fix: classify `no_jobs + duration=0` as startup failure, capture event/head/ref/caller, fetch workflow/check annotations, and run `actionlint` against the workflow at the run SHA.
   - Impact: diagnoses 81% of review failures and eliminates predictable archive calls.
   - Fail-open: retain `unknown_startup_failure` if annotations are unavailable.

2. **Retry transient Git push server errors.**
   - Evidence: run `35868679924` failed on a remote Internal Server Error without retry.
   - Fix: retry 500-class/internal-server failures up to three times with exponential backoff and freshness checks.
   - Rollback: disable through a variable; preserve current hard failure after exhaustion.

3. **Correct reviewer-outage classification.**
   - Evidence: runs `35923766050` and `35933627432` showed all six reviewer slots failed, but final reason was `editor_empty_noop`.
   - Fix: prioritize `reviewer_quorum_unavailable`; include provider HTTP/exit class, response bytes, usage availability, and stderr-tail hash.
   - Impact: prevents masked outages and inappropriate editor retries.

4. **Extend fingerprint quarantine across heads.**
   - Evidence: the head-scoped breaker fired once, but the same fingerprint later reached streak `10`.
   - Fix: track PR-wide and support-ref-wide counts, not only head SHA.
   - Rollback: existing `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED` override.

5. **Add a Phase 4b release preflight.**
   - Evidence: release run `35903885958` failed after 2,776s in Phase 4b; CI run `35929972089` later detected the missing active-review lookup contract.
   - Fix: execute that contract test in Phase 0 before live smoke resources.
   - Impact: up to **46 minutes fail-fast saving** on equivalent regressions.

6. **Reduce false warning noise.**
   - Numerous successful review runs emitted `git-submodule cannot be used without a working tree` during post-job cleanup.
   - Keep checkout directories until post-actions complete or disable unnecessary credential persistence. This is non-critical but improves signal quality.

**Policy/MCP signals:** `BREAK_GLASS=0`. Three `CONTEXT_BUDGET_WARN` events occurred across two failed editor runs. The 12 Semble fallbacks were explicitly `context=contract-test`; runtime fallbacks were zero. Serena emitted no probe or fallback telemetry, so availability cannot be assessed.

## AI Memory Health

Deep-dive sample: 12 review runs, 61 telemetry events.

- **Retrieve hit rate:** 12/12, **100%**; 31–33 records selected.
- **Token use:** average **1,388.8 / 1,400 tokens (99.2%)**.
- **Keyword methods:** `llm=12`, `plain=0`, `none=0`.
- **Zero-result retrieves:** none.
- **`fail_open:true` / `enabled:false`:** none observed.
- **Push behavior:** 34 events carried push attempts; average 1.35, maximum 2, with 12 requiring a second attempt. This indicates mild branch contention, not a severe retry loop.
- **Writes:** 10/10 `record-candidate` operations pushed successfully; 15 `write_lessons_learned` events recorded zero lessons.

**Recommendation:** keep retrieval enabled, but emit `records_considered`, score range, truncation count, and selected-token ratio. The constant 99% budget saturation gives no visibility into whether lower-ranked records add value. No `finalize-task`, `promote`, or `compact` telemetry was present; verify emission in the 654-second memory-maintenance run family.

## GH API Call Audit

| Pattern | Evidence | Assessment | Recommendation / reduction |
|---|---|---|---|
| Duplicate scheduled merge-train backstops | Both `internal-orchestrate-poll.yml` and `internal-cancel-on-pr-close.yml` run every five minutes; recent logs from both repeatedly examined three queued PRs and released zero. | Redundant across workflows. Each release invocation has at least one PR-list and one active-runs call, plus changed-file calls. | Keep the immediate `pull_request: closed` release, but make only one workflow the scheduled backstop. Expected **~50% reduction** in scheduled merge-train reads. |
| Cross-tick blocker re-fetch | PRs `#4348`, `#4349`, and `#4354` remained queued behind unchanged blockers across repeated runs. | Per-process file caching is good, but there is no cross-tick unchanged-state telemetry. | Log open-PR/head hash, blocker hash, changed-file cache hits, and calls avoided. Consider an existing state-snapshot-backed cache only after measurement. |
| Review sweep batching | Sweep fetches one PR snapshot and six active-status snapshots, then filters locally; recent sweep had six candidates, three dispatches, three active skips. | Complies with §15; avoids N×2 lookups. | Retain. Add endpoint counters and global dispatch backpressure rather than per-PR calls. |
| Missing-archive calls | 12 no-job runs generated 12 HTTP 404 archive errors; collector tests intentionally use one attempt. | Predictable calls with no diagnostic value. | Skip archive download for confirmed zero-job runs; fetch annotations/actionlint diagnostics instead. Saves 12 calls in this selected set. |
| Orchestrator conditional calls | `orchestrate_poll_process.sh` contains many comments/timeline/labels endpoints, but runtime per-endpoint counts are absent. | Potential hotspots cannot be ranked safely. | Instrument `gh_retry` with method, normalized endpoint class, attempts, elapsed time, status, rate-limit wait, and cache/prefetch hit. |

No production rate-limit event was observed, but **runtime API call counts were not collected**, so rate-limit risk cannot be quantified. This is the main gap relative to `CLAUDE.md §15`.

## Prompt Cache & Memory System

- Broader totals: **61.40M cache-read tokens**, **17.55M prompt tokens**, and **0 reported cache-write tokens**. Cache reads represent **77.8% of input-like token volume**.
- Four explicit `cache_hit_rate` samples averaged **80.6%** with a 77.3–87.7% range. Aggregate `cache_hit_rate` remained `null`.
- Usage was unavailable for **23/135 OpenRouter calls (17.0%)**; it was unavailable for every call in the two all-reviewer-outage runs.
- The `Pre-assemble static context (cacheable across runs)` step is a good design. Remaining fragmentation is likely provider heterogeneity and unmeasured dynamic-prefix variance—an inference, not directly proven.
- Memory retrieval was reliable but saturated its 1,400-token budget.
- Three context warnings show that total prompt growth, not memory alone, is eroding headroom.

**Changes:**
1. Emit per-call `model`, prefix fingerprint, prompt/cache-read/cache-write tokens, usage availability, and cache status.
2. Keep immutable rubric/tool/schema text first; append timestamps, run IDs, comments, and check tails after the stable prefix.
3. When review context exceeds 140k, remove duplicate comments and optional pass-1/raw artifacts before current findings.
4. Log Semble `bytes_before`, `bytes_after`, `chunks_selected`, and `bytes_injected` to prove prompt reduction.
5. Treat missing usage as a reliability fault when an entire multi-provider run lacks accounting.

## Orchestrator Health

- **Healthy completion signal:** 51/51 `orchestrate_poll` runs succeeded; no failures or cancellations.
- **Cadence pressure:** p50 was 326s and p95 361s on a five-minute schedule. Because `cancel-in-progress=false`, overruns queue rather than replace each other.
- **Merge-train stagnation:** repeated scans examined three PRs and released zero, but logs lack blocker age and unchanged-cycle count.
- **Failure-heal dedup worked:** run `35938607639` mapped the repeated autofix defect to existing issue `#4353` rather than opening another.
- **Resume logic worked:** multiple recent review runs recovered `sandbox_initialization_failure` and restored 33 artifacts.
- **Clarification/wave evidence is incomplete:** clarify had no failures, but state-transition counts, deferral ages, and wave progression were not included in run summaries.

**Smallest safe mitigation:** add `POLL_PHASE_TIMING_V1` for support setup, merge train, discovery, each tracking issue, judge, state persistence, and snapshot publication. Also emit state hash, changed/unchanged result, blocker age, queue wait, and next-action reason. Do not change cadence until the 326-second critical handler is identified.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant overhead | Priority action |
|---|---|---|---|
| Clarify | 141 runs; 131 skipped; p50 1s, p95 257s | Expected trigger noise | Classify expected skips; no compute optimization needed. |
| Plan | 130 runs; 119 skipped; p95 397s | Occasional model execution | Emit active-phase timing and prompt size. |
| Implement | 129 runs; 118 skipped; eight successes; max 1,475s | Codex execution; one run used 1.326M tokens | Add per-call role telemetry before model tuning. |
| Review/autofix | 188 runs; 32 failures; p95 4,138s | Reviewer models, repeated fingerprints, editor no-op | Cross-head breaker, quorum gate, tiered review, transient push retry. |
| CI | 29 runs; p50 2,548s, p95 2,613s | Long serial test groups | Move focused contracts first; continue sharding slow groups. |
| Validate/release | One validate success at 499s; one release failure at 2,776s | Late Phase 4b canary | Add Phase 0 contract preflight. |
| Orchestrate poll | 51 successes; p50 326s | Unknown handler plus repeated API scans | Phase timers and one scheduled merge-train owner. |
| Queueing | Review run `35931394370` waited 1,565s before its first step and was cancelled | Sweep fan-out / hosted-runner pressure | Global sweep concurrency ceiling. |
| Retry | 12 sampled reviews had four stall kills; two recovered | Provider stalls | Log slot elapsed time and global quorum state. |
| Merge overhead | Three PRs remained queued across repeated scans | Older overlapping PRs | Log blocker age and unchanged-cycle count. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review p95 4,138s; CI p50 2,548s; poll p50 326s.
- **Top failure modes:** 26 zero-job startup failures; repeated `editor_empty_noop`; multi-provider reviewer outages; transient GitHub push failure.
- **Highest cost drivers:** 79.71M OpenRouter tokens, including a 27.25M-token run that failed only at push; 1.326M Codex tokens in implement.
- **Top three actions:**
  1. Add PR-scoped fingerprint quarantine and reviewer-quorum classification.
  2. Retry bounded transient push-side server failures.
  3. Consolidate scheduled merge-train scans and add poll/API phase telemetry.

## Metrics Appendix

### Outcome and duration

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1,000 | 388 | 34 | 11 | 567 | 8s | 2,221s |
| Review/autofix | 188 | 144 | 32 | 8 | 4 | 26s | 4,138s |
| CI | 29 | 28 | 1 | 0 | 0 | 2,548s | 2,613s |
| Orchestrate poll | 51 | 51 | 0 | 0 | 0 | 326s | 361s |
| Implement | 129 | 8 | 0 | 3 | 118 | 1s | 711s |
| Plan | 130 | 11 | 0 | 0 | 119 | 1s | 397s |
| Clarify | 141 | 10 | 0 | 0 | 131 | 1s | 257s |

Raw success rate was 38.8% because 56.5% of runs were skipped/other. Success among success-or-failure outcomes was **91.9%**.

### Model, cache, and review telemetry

| Metric | Value |
|---|---:|
| OpenRouter calls | 135 |
| Prompt tokens | 17,549,448 |
| Completion tokens | 769,521 |
| Cache-read tokens | 61,398,976 |
| OpenRouter total | 79,712,413 |
| Usage-unavailable calls | 23 |
| Codex calls / tokens | 13 / 1,326,191 |
| Aggregate `cache_hit_rate` | unavailable |
| Explicit cache-rate sample | n=4, average 80.57% |
| Wall-clock p50 / p99 | 13,000ms / 5,755,520ms |
| Wall-clock samples | 105 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 3 |

### AI memory sample

| Metric | Value |
|---|---:|
| Sampled runs | 12 |
| Retrieve hit rate | 100% |
| Average selected tokens / budget | 1,388.8 / 1,400 |
| Keyword methods | `llm=12`, `plain=0`, `none=0` |
| Zero-result / disabled / fail-open retrieves | 0 / 0 / 0 |
| Push-attempt average / maximum | 1.35 / 2 |

### MCP telemetry

| System / target | Queries | Logged bytes | Fallbacks | Runtime fallbacks | Probe OK / failed / skipped |
|---|---:|---:|---:|---:|---|
| Semble, all targets | 23 | 250,653 | 12 | 0 | not emitted |
| Semble, reviewer-context deep-dive | 12 observed | ~14–16KB/query | 0 | 0 | not emitted |
| Serena, no target observed | 0 | 0 | 0 | 0 | 0 / 0 / 0 |

**Other MCP servers observed:** none.

### GH API summary

| Signal | Value |
|---|---:|
| Runtime call counters | unavailable |
| Rate-limit events | none observed; coverage incomplete |
| Missing log archives | 12 |
| Zero-job review failures | 26 |
| Minimum merge-train release reads | 2 per invocation, plus changed-file calls |
| Sweep baseline reads | 7 per tick, plus dispatch writes |

**Material data gaps:** no per-endpoint API counters, no startup validation annotations, no poll phase timings, no merge-blocker age, no Serena probes, and only 7% direct success-log sampling; broader unselected-run summaries were used where available.

## Deep Audit — Workflows & Scripts (2026-09-24)

### Section 1: Bug & Correctness Sweep

Audit coverage: all 50 workflows, 85 shell scripts, and 57 Python scripts parsed successfully; no YAML, Bash, or Python syntax errors were found.

#### SEC-001 — GitHub ref values are interpolated directly into shell source

- **File:** `.github/workflows/review_autofix.yml:6821-6827,7437-7442`; `.github/workflows/mark-stable.yml:44-50`; `.github/workflows/test-and-mark-stable.yml:162-175`; `.github/workflows/workflow-log-analysis.yml:1155-1169,1316-1324`; `.github/workflows/implement.yml:568-572`
- **Severity:** High
- **Category:** `security`
- **Description:** Values including `github.workflow_ref`, `github.ref_name`, and the repository default branch are embedded inside `run:` source. GitHub substitutes expressions before Bash parses the script, so accepted ref names containing shell metacharacters could alter execution. The review workflow is especially sensitive because it runs with write credentials. Exploitability depends on GitHub’s accepted ref-name characters and caller permissions. [NEEDS VERIFICATION]
- **Recommended fix:** Bind every value through step `env:` and consume the environment variable in Bash. Use existing `GITHUB_REF_NAME` where available; add `CALLER_WORKFLOW_REF: ${{ github.workflow_ref }}` and `DEFAULT_REPO_BRANCH: ${{ github.event.repository.default_branch }}` for the remaining cases.

#### CONSIST-001 — Comprehensive-release API helper lacks normal transient-error retry behavior

- **File:** `scripts/comprehensive_test_and_release_gh_api.sh:3-47`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** `gh_api_safe` retries only messages containing `rate limit`. A network failure or HTTP 5xx returns immediately, unlike repository-standard `gh_retry`, which applies bounded exponential backoff and permanent-error classification. One transient GitHub response can therefore abort the comprehensive release path.
- **Recommended fix:** Source `scripts/gh_helpers.sh` and implement `gh_api_safe` as a compatibility wrapper over `gh_retry` or `gh_api_json_to_file`, retaining `GH_API_SAFE_OUTPUT` for callers.

#### SHELL-001 — Unquoted revision expression triggers ShellCheck SC1083

- **File:** `scripts/workspace_init.sh:92-118`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `git rev-parse HEAD^{tree}` leaves braces unquoted. It currently reaches Git as intended, but ShellCheck flags the expression as ambiguous shell syntax.
- **Recommended fix:** Use `git rev-parse 'HEAD^{tree}'` and add a workspace-fingerprint regression assertion.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Default branch is fetched twice on the same tracking-issue path

- **File:** `scripts/orchestrate_poll_process.sh:17464-17482,17971-18020,18561-18564`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** `DEFAULT_BRANCH_TRACKING` is populated at line 17975, but the `merge_conflict` path immediately re-fetches the same repository field into `FINAL_DEFAULT_BRANCH`; the default-branch `/revalidate` path can repeat it again.
- **Call count:** Current: 2 repository GETs on affected paths. Proposed: 1.
- **Recommended fix:** Reuse `DEFAULT_BRANCH_TRACKING` for `FINAL_DEFAULT_BRANCH` and `REVALIDATE_MEMORY_BRANCH`.
- **Pattern to extend:** The existing cycle-local `DEFAULT_BRANCH_TRACKING` and `CWS_DEFAULT_BRANCH` caches.

#### API-002 — Two shared helpers retry permanent failures and sleep after their final attempt

- **File:** `scripts/gh_helpers.sh:610-727`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** `gh_api_json_to_file` does not call `_is_gh_permanent_failure`; `curl_gh_api` treats all non-rate-limit HTTP statuses as retryable. Both also sleep after the final failed attempt. A 404/422 can therefore consume five calls and about 31 seconds of unnecessary backoff.
- **Call count:** Current: up to 5 calls for a permanent error. Proposed: 1. Transient failures retain the existing bounded budget.
- **Recommended fix:** Apply `_is_gh_permanent_failure` in `gh_api_json_to_file`, classify permanent HTTP codes directly in `curl_gh_api`, and sleep only when `attempt < max_attempts`.
- **Pattern to extend:** `gh_retry` and `gh_retry_to_file` at `scripts/gh_helpers.sh:439-560`.

#### API-003 — Clarify fetches the issue comment thread twice

- **File:** `.github/workflows/clarify.yml:468-499`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** When semantic caching is enabled, the step first fetches 50 comments into `ISSUE_COMMENTS_FILE`, then fetches the complete paginated thread into `THREAD_HISTORY_FILE`.
- **Call count:** Current: 2 logical comment-list calls, with pagination on the second. Proposed: 1 paginated logical call.
- **Recommended fix:** Fetch the complete thread once, validate it, derive the first 50 entries locally for `ISSUE_COMMENTS_FILE`, and render the complete history from the same payload.
- **Pattern to extend:** The single `--paginate --slurp` fetch in `.github/workflows/orchestrate_clarify_respond.yml:510`.

#### BATCH-001 — Standalone stall discovery performs seven label-list calls

- **File:** `scripts/orchestrate_poll_process.sh:15498-15528`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The candidate-discovery loop issues one `gh issue list` call for each of seven pipeline labels before batching candidate details.
- **Call count:** Current: 7 reads per standalone-stall sweep. Proposed: 1 aliased GraphQL search request in the normal ≤100-results-per-label case, with per-alias pagination fallback.
- **Recommended fix:** Add `_fetch_standalone_labeled_issues_graphql`, accepting the label list and returning deduplicated issue numbers.
- **Pattern to extend:** `_fetch_standalone_marker_issues_graphql` at `scripts/orchestrate_poll_process.sh:14390-14438`.

#### BATCH-002 — Security advisory follow-ups are read per issue

- **File:** `scripts/orchestrate_poll_process.sh:5930-6028`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** For `N` unchecked follow-ups, the loop performs `N` issue GETs and another comments GET for each of the `B` blocked issues. The required state, labels, and recent marker comments are GraphQL-batchable. Older markers outside a bounded comments window require a REST fallback. [NEEDS VERIFICATION]
- **Call count:** Current: `N + B` reads, plus up to `B` non-batchable comment writes. Proposed: `ceil(N/25)` GraphQL reads plus fallback reads only where comment pagination is incomplete; writes remain unchanged.
- **Recommended fix:** Add a batched helper returning issue state, labels, `comments(last:100)`, and pagination state.
- **Pattern to extend:** `_fetch_candidate_issue_details_graphql` at `scripts/orchestrate_poll_process.sh:14483-14613`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Integration-ref bootstrap is repeated across three workflows

- **File:** `.github/workflows/clarify.yml:57-129`; `.github/workflows/implement.yml:435-508`; `.github/workflows/orchestrate_clarify_respond.yml:110-183`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The workflows repeat the same resolver-repository clone, authenticated fetch, main fallback, log redaction, cleanup trap, and `resolve_integration_ref.sh` invocation.
- **Recommended fix:** Move it to `scripts/integration_ref_helpers.sh` with `resolve_integration_ref_bootstrapped <issue_number> <repository> <resolver_ref> <github_output>`. Update all three callers to stage and invoke that helper.

#### DUP-002 — Stable release implementation is duplicated in both release workflows

- **File:** `.github/workflows/mark-stable.yml:71-160,230-318,451-487,565-867`; `.github/workflows/test-and-mark-stable.yml:206-302,4033-4120,5427-5463,5552-5842`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** Version resolution, sharded poller tests, tag validation, changelog assembly, release-note extraction, verified tag publication, and GitHub Release creation appear as exact or near-exact blocks. Reliability fixes must currently be ported twice.
- **Recommended fix:** Create `scripts/stable_release.sh` with functions:
  - `stable_release_resolve_version <source_branch> <input_version> <github_output>`
  - `stable_release_validate_tag <version>`
  - `stable_release_publish_tags <version> <source_branch> <tested_sha>`
  - `stable_release_create_github_release <version> <notes_file> <source_branch>`

#### DUP-003 — Context-budget warning implementation exists three times

- **File:** `scripts/review_run_reviewers.sh:69-107`; `scripts/review_apply_fixes.sh:164-202`; `scripts/review_rb_judge.sh:256-294`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` has the same body in three review stages, risking divergent thresholds or telemetry.
- **Recommended fix:** Move the existing signature `emit_context_budget_warn_for_prompt <phase> <prompt_file> <model>` to `scripts/review_prompt_helpers.sh` and source it from all three callers.

#### DUP-004 — Stall-guard state parsing is duplicated across five scripts

- **File:** `scripts/watchdog_helpers.sh:186-201`; `scripts/self_heal_validation.sh:143-158`; `scripts/review_run_reviewers.sh:324-338`; `scripts/review_conflict_resolve.sh:304-318`; `scripts/review_rb_judge.sh:168-182`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Two duplicate clusters implement the same state-file parsing behavior.
- **Recommended fix:** Make `scripts/watchdog_helpers.sh::read_codex_stall_guard_state <state_file>` canonical and source it from the four remaining scripts.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Support-staging expression is within 674 characters of the hard limit

- **File:** `.github/workflows/implement.yml:981-1337`
- **Severity:** High
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` scalar is approximately 20,326 characters with three `${{ }}` expressions. Only 674 characters, or 3.2%, remain before the 21,000-character rejection limit.
- **Recommended fix:** Extract support-file staging into `scripts/stage_implement_support.sh`; pass repository and feature values through environment variables and return paths through `GITHUB_ENV`.

#### EXPR-002 — Commit preflight block exceeds the medium-risk threshold

- **File:** `.github/workflows/implement.yml:3218-3524`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` scalar is approximately 17,313 characters with one `${{ }}` expression. Headroom is 3,687 characters.
- **Recommended fix:** Extract the temporary-index destructive and `files_touched` scope checks into `scripts/implement_preflight_commit_guard.sh`, sharing staging semantics with `scripts/implement_commit_changes.sh`.

No workflow exceeds 800 KB. The largest is `.github/workflows/review_autofix.yml` at 540,752 bytes. No `if:` expression approaches the 15,000-character threshold; the largest observed condition was under 1,000 characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Parsed line-range end is never consumed

- **File:** `scripts/review_issue_ledger.sh:62-106`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `line_end` is parsed, defaulted, and assigned but never affects the anchor range; context is always centered solely on `line_start`.
- **Recommended fix:** Either remove `line_end` and document start-line-only fingerprinting, or set the context end to at least `line_end + 2` and add a multiline-finding fingerprint test.

#### DEAD-002 — `CURRENT_FLOOR` array is write-only

- **File:** `scripts/review_issue_ledger.sh:862-920`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `CURRENT_FLOOR` is declared and populated but never read. `floor_cat` already participates directly in issue-ID generation.
- **Recommended fix:** Remove the array declaration and assignment, or consume it when constructing final ledger rows if floor provenance was intended to persist.

#### DEAD-003 — Three workflow-local values are assigned but never read

- **File:** `.github/workflows/test-and-mark-stable.yml:1214-1223,2965-3081`; `.github/workflows/orchestrate.yml:1341-1346`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `BLOB_CONTENT` is decoded but unused; the Phase 6 `PREV_STATE` is assigned but never compared; `WAVE_COUNT` is calculated but absent from the summary.
- **Recommended fix:** Remove the assignments, or restore their intended validations/output. Keep the unrelated `PREV_STATE` variables in other polling phases, which are actively read.

#### CONSIST-002 — Shared support-staging script violates the repository indentation contract

- **File:** `scripts/stage_workflow_support.sh:4-150`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** Most function-body indentation uses spaces or no indentation, and the event/transcript blocks are inconsistently nested. CLAUDE.md §9 requires tabs for shell indentation.
- **Recommended fix:** Reformat the function body with tabs and replace singleton `for f in transcript_archive.sh` loops with direct assignments.

#### SHELL-002 — CI intentionally disables ShellCheck for workflow `run:` blocks

- **File:** `.github/workflows/ci.yml:289-304,987-992`; `.github/workflows/mark-stable.yml:197-202`; `.github/workflows/test-and-mark-stable.yml:4000-4005`
- **Severity:** Medium
- **Category:** `shellcheck`
- **Description:** `actionlint -shellcheck=` explicitly disables inline-shell analysis, while standalone ShellCheck checks only script files and only `error` severity. Consequently workflow-local dead variables, singleton loops, and interpolation hazards are not gated.
- **Recommended fix:** Enable actionlint’s ShellCheck integration with a checked-in baseline or narrow initial gate for high-value codes such as SC2086, SC2046, SC2154, SC2024, and SC2296; ratchet down the baseline over time.

No literal `TODO`, `FIXME`, or `HACK` markers were found in scoped workflow or script files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | SEC-001, EXPR-001 |
| Medium | 9 | CONSIST-001, API-001, API-002, BATCH-001, BATCH-002, DUP-001, DUP-002, EXPR-002, SHELL-002 |
| Low | 8 | SHELL-001, API-003, DUP-003, DUP-004, DEAD-001, DEAD-002, DEAD-003, CONSIST-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 5 existing workflows | Medium |
| API call optimization | 3 files | Medium |
| Code modularization | 11 existing files plus 2 new helpers | Large |
| Expression size reduction | 1 workflow plus 2 helper scripts | Medium |
| Medium/Low fixes | 7 existing files | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-24)

### Safety Tag Legend

`SAFE_TO_MERGE` is statically proven safe; `NEEDS_VERIFICATION` requires the stated checks; `RISKY_SKIP` must not be auto-implemented because it touches retry, pagination, race-defense, or orchestrator recovery behavior.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Share the editor-changes-lost workflow-run snapshot

- **Safety tag:** `SAFE_TO_MERGE`
- **Files:** `scripts/gh_helpers.sh:1219-1291`, `scripts/gh_helpers.sh:1338-1402`, `.github/workflows/review_autofix.yml:7398-7422`
- **Current call count:** 2 on the no-peer path; 1 when an active peer causes early exit.
- **Proposed call count:** 1 on either path.
- **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs?branch={head_branch}&per_page=30`
- **Evidence:** Both helpers execute the identical request back-to-back in the same workflow step:
  ```bash
  gh_retry gh api -X GET \
    "/repos/${GITHUB_REPOSITORY}/actions/runs" \
    -f "branch=${head_branch}" \
    -f "per_page=30"
  ```
  The only intervening operation is `git rev-parse HEAD`; no GitHub mutation occurs.
- **Proposed fix:** Extend `autofix_retrigger_has_inflight_peer` and `autofix_changes_lost_head_retry_consumed` to accept an optional shared snapshot file/status. Fetch once after `peer_wait`, then evaluate both predicates from that response. Preserve `AUTOFIX_PEER_QUERY_FAILED`, `AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED`, and their respective fail-open/fail-closed return behavior.
- **Safety rationale:** The endpoint, branch filter, page size, authentication, retry policy, workflow step, and mutation boundary are identical, and the existing error semantics can be preserved independently from one fetch result.
- **Downstream signal:** Consolidate the two editor-changes-lost list-runs reads into one step-local snapshot while preserving both helpers’ log keys and return semantics.

#### MERGE-002 — Batch promote-cycle baseline comment reads

- **Safety tag:** `NEEDS_VERIFICATION`
- **File:** `scripts/promote_main_cycle.sh:242-265`
- **Current call count:** `1 + K`, where the search is followed by comments reads for up to 10 candidate issues.
- **Proposed call count:** 2: one search plus one batched GraphQL request.
- **Endpoints:** `GET /search/issues`; `GET /repos/{owner}/{repo}/issues/{number}/comments?per_page=100`; proposed `POST /graphql`.
- **Evidence:**
  ```bash
  numbers="$(gh_retry gh api -X GET search/issues ... -f per_page=10 ...)"
  while IFS= read -r issue_number; do
    sha="$(gh_retry gh api \
      "repos/${GITHUB_REPOSITORY}/issues/${issue_number}/comments?per_page=100" ...)"
  done <<< "${numbers}"
  ```
- **Proposed fix:** Add a local batched helper returning issue-number-keyed `comments(first:100)` nodes containing `body`, `author.login`, and `authorAssociation`. Keep the REST search unchanged and follow `_fetch_candidate_issue_details_graphql`’s aliasing and partial-response validation pattern.
- **Safety rationale:** This changes endpoint and error-aggregation semantics, so static reading cannot prove equivalence with the current per-issue REST failure behavior.
- **Downstream signal:** Verify GraphQL `comments(first:100)` ordering, trusted-author mapping, partial-alias handling, and whole-function `return 2` behavior against fixtures before implementation.

#### MERGE-003 — Batch apply-analysis marker comment reads

- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `scripts/apply_analysis_on_main.sh:172-188`, `scripts/apply_analysis_on_main.sh:196-219`
- **Current call count:** `1 + K` per candidate document, with comments fetched for up to 20 search results.
- **Proposed call count:** 2 per candidate document.
- **Endpoints:** `GET /search/issues`; `GET /repos/{owner}/{repo}/issues/{number}/comments?per_page=100`; proposed `POST /graphql`.
- **Evidence:**
  ```bash
  numbers="$(gh_retry gh api -X GET search/issues ... -f per_page=20 ...)"
  while IFS= read -r issue_number; do
    trusted_marker_comment_present "${issue_number}" "${marker_line}"
  done <<< "${numbers}"
  ```
  `trusted_marker_comment_present` performs one comments GET for each issue.
- **Proposed fix:** Add `_fetch_trusted_marker_comments_graphql` to batch the searched issue numbers and update `doc_dispatched_before` to evaluate the existing marker and author-association predicate locally. Follow `_fetch_candidate_issue_details_graphql`’s batching contract.
- **Safety rationale:** The proposed GraphQL batch couples failures that are currently isolated per issue and therefore does not satisfy the identical error-semantics prerequisite.
- **Downstream signal:** Verify first-100 comment ordering, trusted association normalization, null/partial aliases, and exact `0/1/2` return codes before implementation.

#### MERGE-004 — Fetch recorded final-PR state once

- **Safety tag:** `RISKY_SKIP`
- **File:** `scripts/orchestrate_poll_process.sh:10230-10242`
- **Current call count:** 2 on a `final_pr_json_snapshot` cache miss.
- **Proposed call count:** 1; cache hits remain 0.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{final_pr}`
- **Evidence:**
  ```bash
  existing_pr_state="$(gh_retry _safe_gh_jq ... --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq ... --jq '.merged_at != null' || echo "")"
  ```
- **Proposed fix:** Fetch `{state, merged: (.merged_at != null)}` once in `finalize_integration_merge_if_needed` and parse both values from the same payload.
- **Safety rationale:** This is inside `orchestrate_poll_process.sh` and the final-merge race-defense path, which mandates `RISKY_SKIP` despite the adjacent identical endpoint.
- **Downstream signal:** Do not auto-implement; manually verify final-merge race tests and confirm the atomic snapshot preserves fail-closed behavior.

#### MERGE-005 — Combine issue title/body reads in stall reissue paths

- **Safety tag:** `RISKY_SKIP`
- **Files:** `scripts/orchestrate_poll_process.sh:13731-13733`, `scripts/orchestrate_poll_process.sh:16413-16421`
- **Current call count:** 2 per executed reissue path; 4 across both implementations.
- **Proposed call count:** 1 per path; 2 across both implementations.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_num}`
- **Evidence:**
  ```bash
  orig_title="$(gh_retry _safe_gh_jq ... --jq '.title // ""' || echo "")"
  orig_body="$(gh_retry _safe_gh_jq ... --jq '.body // ""' || echo "")"
  ```
- **Proposed fix:** In `execute_stall_recovery_action` and `run_standalone_stall_recovery`, fetch `{title, body}` once and derive both variables locally.
- **Safety rationale:** Both pairs are inside explicit stall-recovery paths in `orchestrate_poll_process.sh`, and combining them changes partial-failure behavior.
- **Downstream signal:** Do not auto-implement; manually review whether a single failed payload may safely blank both title and body before replacement-issue creation.

### Redundant Re-Fetch (REUSE-###)

No findings.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — Valid reuse opportunity, but it crosses orchestrator command and race-defense paths.
- API-002: `RISKY_SKIP` — Shared retry-loop and rate-limit behavior must be reviewed manually.
- API-003: `RISKY_SKIP` — Consolidation changes pagination and bounded-first-50 semantics.
- BATCH-001: `RISKY_SKIP` — It is an orchestrator discovery path with pagination fallback requirements.
- BATCH-002: `RISKY_SKIP` — It combines orchestrator recovery, paginated comments, and subsequent writes.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| `SAFE_TO_MERGE` | 1 | MERGE-001 |
| `NEEDS_VERIFICATION` | 2 | MERGE-002, MERGE-003 |
| `RISKY_SKIP` | 7 | MERGE-004, MERGE-005, API-001, API-002, API-003, BATCH-001, BATCH-002 |

### Implement-Stage Handoff

- MERGE-001
