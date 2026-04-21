## Executive Summary

- **`review_autofix` is the dominant end-to-end bottleneck and reliability drag** (222 runs, **p50 498s / p95 3893.7s**, 22 failures, 71 cancelled). Failures cluster at late-stage git push (`Push all pending commits`) in long runs such as `24654694004` (7518s) and `24654188112` (5103s). **Estimated impact:** 25–45% reduction in total CI wall time for AI-driven repos if fixed first. **Confidence: high**.
- **`update_workflows` is fast but extremely failure-prone** (91 runs, **81 failures = 89.0% failure rate**) despite short durations (p50 10s, p95 15s), indicating logic/guardrail errors rather than compute limits. **Estimated impact:** 70–85% failure reduction in that family with low latency cost. **Confidence: high**.
- **Security workflows are currently mostly red in sampled window**: `security_codeql` 4/4 failed; `security_dependency_audit` 3/4 failed. This is a release-risk amplifier despite moderate runtimes. **Estimated impact:** major reduction in blocked PRs/releases once stabilized. **Confidence: high**.
- **Orchestrator fan-out is generating many skipped micro-runs** (e.g., multiple clarify/plan/implement/orchestrate_clarify_respond runs at `2026-04-21T03:58:55Z` in `coding-workflows`), adding control-plane noise and API/log overhead without progress. **Estimated impact:** 10–20% control-plane efficiency gain. **Confidence: medium**.
- **Observability gaps are limiting cost optimization precision**: no token/model/cache telemetry in provided sample; no MCP/Serena trace events; GH API counts absent except two log-download 404s. **Estimated impact:** enables 15–30% token/dollar optimization once instrumented. **Confidence: high**.

## Speed Optimizations

Ranked by expected **end-to-end latency reduction**.

1) **Critical path: Reduce `review_autofix` long-tail reruns/failures before final push**
- **Evidence:** Workflow family `review_autofix` p95 = **3893.7s**; worst run `24654694004` failed at final push after **7518s**.
- **Root cause:** Late-failing git push causes full expensive run waste.
- **Exact change:** Add **early preflight step** before model-heavy work:
  - verify branch writeability/up-to-date (`git fetch`, fast-forward check),
  - dry-run push permission check (or protected-branch detection),
  - fail fast with actionable status if push is impossible.
- **Estimated time savings:** 20–40 min saved per prevented late failure; portfolio-level p95 reduction 10–25%.
- **Risk:** Low (fail-fast behavior only).

2) **Critical path: Split reviewer phase and commit/push into isolated jobs with conditional continuation**
- **Evidence:** Failures often at commit/push while review compute already completed (`24654188112`, `24663133055`, `24657329504`).
- **Root cause:** Monolithic job couples expensive inference with volatile SCM operations.
- **Exact change:** Persist patch/artifact after review; perform push in short downstream job; on push failure keep artifact for manual/apply-on-retry.
- **Estimated time savings:** 15–30% on failing runs; improves retry efficiency.
- **Risk:** Medium (workflow refactor, artifact plumbing).

3) **High impact: Concurrency cancellation for superseded review runs**
- **Evidence:** `review_autofix` has 71 cancelled runs and many multi-thousand-second executions.
- **Root cause:** New commits likely start fresh expensive runs while old runs keep executing.
- **Exact change:** Set workflow/job `concurrency` with `cancel-in-progress: true` keyed by PR/branch.
- **Estimated time savings:** 10–25% compute-time reduction in active PRs.
- **Risk:** Low.

4) **Local optimization: Avoid repeated full checkouts in tightly coupled jobs where artifacts suffice**
- **Evidence:** Deep-dive logs repeatedly show full `actions/checkout` setup across jobs (`update`, `CodeQL`, `Dependency Audit`, `Reproducible Compile/Test`).
- **Root cause:** Re-initialization overhead repeated per workflow/job.
- **Exact change:** Reuse artifacts for generated outputs and gate downstream jobs to run only when relevant paths change.
- **Estimated time savings:** 30–120s per skipped/trimmed job (not critical-path unless many parallel jobs serialize).
- **Risk:** Low–medium.

## Cost Optimizations

Ranked by expected token/$ savings.

1) **Eliminate wasted long AI runs failing at push**
- **Evidence:** Multiple AI review failures at `Push all pending commits` after long durations (e.g., `24654694004`, `24654188112`, `24653577331`).
- **Root cause:** Expensive inference done before validating write path.
- **Exact change:** Preflight SCM checks + split push into isolated terminal step/job.
- **Estimated savings:** Potentially highest; each avoided failure saves near-full AI inference budget for that run.
- **Quality risk:** None if preflight is conservative (fail-open for transient network issues optional).

2) **Apply adaptive model policy by stage**
- **Evidence:** `review_autofix` dominates runtime/cost; clarify/plan/implement families are short/light in aggregate.
- **Root cause:** Likely overuse of high-cost model tiers across all review subtasks.
- **Exact change:** Route:
  - lightweight model for triage/classification/reformatting,
  - premium model only for ambiguous semantic fixes/high-risk code edits.
- **Estimated savings:** 20–40% token spend in review workflows (needs telemetry confirmation).
- **Quality risk:** Medium; mitigate with confidence thresholds and escalation rules.

3) **Reduce avoidable orchestrator fan-out skips**
- **Evidence:** bursts of skipped clarify/plan/implement/orchestrate_clarify_respond runs at same timestamp.
- **Root cause:** Trigger graph emits runs that immediately self-skip.
- **Exact change:** move conditions to trigger-level (`if`, `paths`, dispatch predicates) so non-actionable runs are not scheduled.
- **Estimated savings:** small per run, large cumulative in high-volume repos.
- **Quality risk:** Low.

4) **Token telemetry gap closure (required for real optimization)**
- **Evidence:** No prompt/completion/cache metrics present in provided telemetry.
- **Root cause:** Missing/partial instrumentation ingestion.
- **Exact change:** Emit per-step model, prompt, completion, cache create/read counters into run summary artifacts.
- **Estimated savings:** Indirect but foundational; enables targeted 15–30% optimization.
- **Quality risk:** Low.

## Reliability Improvements

Ranked by expected failure-rate reduction.

1) **Fix git push failure class in review workflows**
- **Failure evidence:** `coding-workflows` runs `24654694004`, `24654188112`, `24663133055`, `24657329504`; `tele-funtoken-msg-scoring` run `24653577331`.
- **Root cause category:** SCM integration / branch-protection / race.
- **Exact fix:** preflight branch state + rebase/pull strategy + idempotent push with bounded retry + artifact fallback.
- **Expected impact:** Significant drop in `review_autofix` failures and late aborts.
- **Rollback/fail-open:** keep fallback “comment-only/no-push mode” if push fails.

2) **Stabilize `update_workflows` family (89% failure rate)**
- **Failure evidence:** 81/91 failures in family-level telemetry.
- **Root cause category:** Workflow logic/config mismatch (not runtime capacity; durations short).
- **Exact fix:** add explicit validation checks before mutate/commit steps; fail with categorized reason codes.
- **Expected impact:** 50%+ relative failure reduction quickly.
- **Rollback/fail-open:** switch to dry-run on validation error.

3) **Repair CI test flake/fail hotspot in `coding-workflows`**
- **Failure evidence:** multiple CI failures at `Orchestrate poll process unit tests` (`24654491977`, `24654174533`, `24664664293`, `24663119074`, `24664253553`).
- **Root cause category:** test instability/regression.
- **Exact fix:** isolate flaky test subset, add deterministic fixtures/time controls, and quarantine until stable.
- **Expected impact:** Lower CI red rate and reruns in core repo.
- **Rollback/fail-open:** non-blocking quarantine label + nightly strict run.

4) **Address poller step failures in `digital_pa`**
- **Failure evidence:** repeated failures in `AI Orchestrate Poller` at `Process each tracking issue` / `Fetch workflow support scripts` (multiple run IDs around Apr 13–15).
- **Root cause category:** external dependency/script retrieval robustness.
- **Exact fix:** cached support scripts + exponential backoff + per-item fail-open continuation.
- **Expected impact:** reduce poller failure cluster.
- **Rollback/fail-open:** skip problematic item, continue queue.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines were observed in the provided deep-dive excerpts.
- Therefore, **retrieve hit rate, token-budget usage, keyword method distribution, fail-open counts, and retry metrics cannot be computed** from this sample.
- Recommendation: verify telemetry emission in sampled runs for operations:
  `retrieve`, `record-candidate`, `record-run-event`, `finalize-task`, `promote`, `compact`, `processed-command-claim`, `processed-command-complete`.

## GH API Call Audit

- **Observed issue:** 2 GH API failures when fetching logs for `shubhodeep1/coding-workflows` (`run_id` `24702725895`, `24702470430`) returned **404 Not Found**.
- **Implication:** log-harvest gaps can hide true failure causes and skew optimization priorities.
- **Likely root cause:** attempting log download before availability/retention window mismatch/permissions mismatch.
- **Concrete changes:**
  1. Add retry with backoff for log fetch (e.g., 3 attempts over 60–120s).
  2. Treat 404 on logs as soft-fail; continue metrics ingestion with partial flag.
  3. Cache run metadata and avoid repeated log fetch attempts for same missing run within cycle.
- **Estimated effect:** modest call-count reduction + better rate-limit posture + improved audit completeness.
- **Data gap:** no endpoint-level call volume/hotspot totals were included, so unbatched loop patterns cannot be quantified in this window.

## MCP & Serena Efficiency

- **Telemetry gap:** No MCP/Serena tool usage traces were present in provided data, so direct efficiency scoring is not possible.
- **Bounded assessment:** Given repeated long review loops, likely gains exist from stricter targeted reads/tool calls.
- **Concrete actions:**
  - Capture per-run MCP call counts and durations by tool/action.
  - Enforce symbol-targeted retrieval before broad file reads.
  - Deduplicate repeated reads of same file region within a run.
  - Parallelize independent metadata reads (repo state, workflow config, changed files) before model invocation.
- **Expected impact:** lower token/context bloat and faster turnaround in review/edit loops.

## Prompt Cache & Memory System

- **Cache metrics unavailable:** no cache creation/read hit data present.
- **Observed symptom:** long review durations and repeated run patterns suggest potential cache misses from unstable prompt prefixes.
- **Recommendations:**
  1. Stabilize system/instruction prefix ordering; append dynamic data at tail.
  2. Remove volatile noise (timestamps/random IDs) from cache-keyed prompt prefix.
  3. Reuse normalized diff summaries across retries/attempts.
  4. Add explicit cache hit/miss counters to telemetry output.
- **Expected impact:** 10–25% token + latency reduction in repetitive review flows (pending measurement).
- **Reliability angle:** fail-open cache reads with deterministic fallback prompt path.

## Orchestrator Health

- **Healthy:** clarify/plan/implement families are generally short and low-failure.
- **Concern:** many near-simultaneous skipped runs in orchestrator-adjacent families (e.g., `coding-workflows` around `2026-04-21T03:58:55Z`), indicating trigger over-fan-out.
- **Concern:** `orchestrate_poll` has high cancellation volume (319 cancelled of 3048 total), suggesting superseded polling cycles.
- **Smallest safe mitigations:**
  1. tighten trigger predicates before dispatch,
  2. add concurrency groups to suppress stale poll cycles,
  3. emit reason codes for each skip/cancel for observability.
- **Track these indicators:** skip ratio by family, cancel ratio by family, median time-to-terminal-state, and stale-run count.

## Pipeline Flow Bottlenecks

1) **Review/autofix loop is the primary bottleneck**
- Compute-heavy and failure-prone; dominates p95.
- Fixes: early SCM preflight, split push job, adaptive model routing.

2) **Retry/late-failure overhead**
- Long runs terminating in push step waste full cycle.
- Fixes: fail-fast gating + artifactized partial outputs.

3) **Orchestrator queue/control-plane overhead**
- Many skipped runs and poll cancellations create noise.
- Fixes: trigger gating + concurrency cancellation.

4) **Security lane instability**
- CodeQL/dependency-audit failures block downstream confidence.
- Fixes: stabilize toolchain/setup and precondition checks.

5) **Queue wait overhead (minor in sample)**
- System logs show runner acquisition delays generally sub-second to a few hundred ms in excerpts; not current dominant bottleneck.

## Per-Repo Breakdown

### shubhodeep1/tele-funtoken-msg-scoring
- **Top bottlenecks:** long `review_autofix` runs (up to 6552s).
- **Top failure modes:** `review / codex-agent` failures, sometimes at push.
- **Highest-cost drivers:** repeated long AI review attempts.
- **Top 3 actions:** (1) push preflight + split push job, (2) adaptive model tiering in review, (3) concurrency cancel superseded review runs.

### shubhodeep1/digital_pa
- **Top bottlenecks:** long poller cycles (p50 high at repo level).
- **Top failure modes:** `orchestrate_poll` script-fetch and issue-processing failures.
- **Highest-cost drivers:** poll reruns/cancellations and update workflow failures.
- **Top 3 actions:** (1) cache support scripts, (2) per-item fail-open processing, (3) add poll concurrency guard.

### shubhodeep1/fun-token-multi-chain
- **Top bottlenecks:** security workflows (CodeQL/dependency audit).
- **Top failure modes:** security family failures and occasional AI review fail.
- **Highest-cost drivers:** repeated security setup + review runs.
- **Top 3 actions:** (1) stabilize security workflow prerequisites, (2) path-filter security jobs, (3) review push preflight.

### shubhodeep1/btc_sweeper
- **Top bottlenecks:** moderate long-tail (p95 ~494s).
- **Top failure modes:** 13.36% failure rate.
- **Highest-cost drivers:** failed runs with moderate duration.
- **Top 3 actions:** (1) identify top failing workflow family, (2) add fail-fast guards, (3) enforce retry budget + reason codes.

### shubhodeep1/atlas-bridge.gd
- **Top bottlenecks:** very high failure rate (48.85%) with many short runs.
- **Top failure modes:** early exits/config or trigger issues likely.
- **Highest-cost drivers:** volume of failed/other runs.
- **Top 3 actions:** (1) classify failures by first failing step, (2) tighten trigger conditions, (3) add structured failure taxonomy.

### shubhodeep1/binance-blessings
- **Top bottlenecks:** cancellation/other-heavy traffic.
- **Top failure modes:** low outright failures.
- **Highest-cost drivers:** control-plane churn.
- **Top 3 actions:** (1) reduce skipped/other fan-out, (2) poll concurrency, (3) skip-reason instrumentation.

### shubhodeep1/mongo-explorer
- **Top bottlenecks:** extremely high failure rate (74.68%).
- **Top failure modes:** likely immediate workflow/config failures.
- **Highest-cost drivers:** failed run churn.
- **Top 3 actions:** (1) fail-step histogram, (2) enforce preflight checks, (3) quarantine unstable workflows until fixed.

### shubhodeep1/multi-user-ai-agent
- **Top bottlenecks:** low duration profile, modest failure rate (4.8%).
- **Top failure modes:** update workflow path inherited from shared reusable workflow.
- **Highest-cost drivers:** repeated update workflow attempts.
- **Top 3 actions:** (1) harden reusable update workflow, (2) add no-op detection before commit/push, (3) improve failure reason reporting.

### shubhodeep1/fbc_shutdown
- **Top bottlenecks:** very high failure rate (51.85%) despite short durations.
- **Top failure modes:** likely trigger/config/runtime precondition.
- **Highest-cost drivers:** repeated failures.
- **Top 3 actions:** (1) baseline successful path, (2) add environment validation, (3) disable non-critical failing lanes until corrected.

### shubhodeep1/coding-workflows
- **Top bottlenecks:** long `review_autofix` and `test_and_mark_stable`.
- **Top failure modes:** push failures in review workflows; CI unit test hotspot; occasional log API 404 during telemetry collection.
- **Highest-cost drivers:** long-tail review runs and retries.
- **Top 3 actions:** (1) push preflight/split push job, (2) stabilize `Orchestrate poll process unit tests`, (3) reduce skipped orchestrator fan-out.

### shubhodeep1/bitsafe.io
- **Top bottlenecks:** insufficient sample (1 run).
- **Top failure modes:** none observed.
- **Highest-cost drivers:** none visible.
- **Top 3 actions:** (1) gather more runs, (2) enable full telemetry (token/cache/API), (3) set baseline SLOs once volume exists.

## Metrics Appendix

### Global Summary

| Metric | Value |
|---|---:|
| Total runs | 6,934 |
| Success | 3,258 |
| Failure | 555 |
| Cancelled | 392 |
| Other | 2,729 |
| p50 duration | 15s |
| p95 duration | 476s |
| Avg duration | 173.37s |

### Key Workflow Families

| Workflow family | Runs | Failure rate | p50 (s) | p95 (s) | Avg (s) |
|---|---:|---:|---:|---:|---:|
| review_autofix | 222 | 9.91% | 498 | 3893.7 | 1166.0 |
| orchestrate_poll | 3048 | 1.18% | 411 | 476 | 276.0 |
| ci | 175 | 78.86% | 0 | 478.9 | 160.3 |
| update_workflows | 91 | 89.01% | 10 | 15 | 17.4 |
| security_codeql | 4 | 100% | 234 | 238.4 | 235.0 |
| security_dependency_audit | 4 | 75% | 49 | 54.4 | 49.5 |
| clarify | 688 | 0.44% | 1 | 9 | 4.9 |
| plan | 656 | 0% | 1 | 25.25 | 11.1 |
| implement | 655 | 0% | 1 | 8.3 | 18.8 |

### Sampled Slow/Failing Run Evidence

| Repo | Run ID | Family | Duration (s) | Conclusion | Failure point |
|---|---:|---|---:|---|---|
| coding-workflows | 24654694004 | review_autofix | 7518 | failure | Push all pending commits |
| tele-funtoken-msg-scoring | 24658114293 | review_autofix | 6552 | failure | review/codex-agent |
| coding-workflows | 24654188112 | review_autofix | 5103 | failure | Push all pending commits |
| coding-workflows | 24654028264 | test_and_mark_stable | 3759 | failure | Unit tests |
| tele-funtoken-msg-scoring | 24653577331 | review_autofix | 2972 | failure | Push all pending commits |

### Per-Repo Snapshot

| Repository | Runs | Failure rate | p50 (s) | p95 (s) | Avg (s) |
|---|---:|---:|---:|---:|---:|
| atlas-bridge.gd | 565 | 48.85% | 1 | 151.6 | 43.1 |
| binance-blessings | 1000 | 0.00% | 1 | 417 | 91.5 |
| bitsafe.io | 1 | 0.00% | 198 | 198 | 198.0 |
| btc_sweeper | 262 | 13.36% | 1 | 494.15 | 77.5 |
| coding-workflows | 1000 | 3.50% | 2 | 656.1 | 261.7 |
| digital_pa | 1000 | 6.40% | 412 | 734.05 | 298.0 |
| fbc_shutdown | 27 | 51.85% | 1 | 116.1 | 17.4 |
| fun-token-multi-chain | 1000 | 1.20% | 1 | 418.1 | 126.0 |
| mongo-explorer | 79 | 74.68% | 0 | 129.5 | 15.8 |
| multi-user-ai-agent | 1000 | 4.80% | 17 | 25 | 19.1 |
| tele-funtoken-msg-scoring | 1000 | 1.20% | 471 | 479 | 359.3 |

### Token/Cache/API Telemetry Availability

| Telemetry area | Availability in provided data | Notes |
|---|---|---|
| Token usage (prompt/completion/total) | Not available | Cannot compute token-cost totals |
| Prompt cache create/read metrics | Not available | Cannot compute hit/miss rates |
| AI memory telemetry (`AI_MEMORY_TELEMETRY`) | Not observed | Retrieval/learning metrics unavailable |
| GH API call counts by endpoint | Not available | Only 2 observed 404 log-fetch errors |

## Deep Audit — Workflows & Scripts (2026-04-21)

Audited scope: **29 workflows** under `.github/workflows/` and **42 scripts** under `scripts/` (`.sh` + `.py`), plus required pre-context files (`README.md`, `agents.md`, `CLAUDE.md`, `.github/ai/*`) and existing report contents.

### Section 1: Bug & Correctness Sweep

- **ID**: **SEC-001**  
  **File path**: `.github/workflows/clarify.yml:60-79`, `.github/workflows/plan.yml:89-108`, `.github/workflows/implement.yml:226-245`, `.github/workflows/validate.yml:81-100`, `.github/workflows/orchestrate_clarify_respond.yml:83-102`  
  **Severity**: **High**  
  **Category tag**: `security`  
  **Description**: Each workflow fetches `scripts/resolve_integration_ref.sh` from `coding-workflows@stable` at runtime via `gh api .../contents/... | base64 --decode`, then executes it with `bash`. This is remote code execution without integrity pinning (digest/signature) in jobs that hold `GH_PAT`.  
  **Recommended fix**: Reuse the staged-support pattern from `.github/workflows/orchestrate_poll.yml:207-345` (checkout known ref into `.codex-workflow-src`) and execute only local checked-in/staged script; add SHA-256 verification for fetched helper when fallback remote fetch is unavoidable.

- **ID**: **BUG-001**  
  **File path**: `.github/workflows/test-and-mark-stable.yml:482-485`  
  **Severity**: **Medium**  
  **Category tag**: `bug`  
  **Description**: `OTHER_ACTIVE_PLAN_RUNS=$(gh api ... --jq ... || echo "0")` is later used in numeric test (`-gt`). If `gh api` emits JSON error text to stdout on failure, the variable can become non-numeric/multi-line, causing `integer expression expected` under `set -euo pipefail`, which can fail phase polling incorrectly.  
  **Recommended fix**: Use the same guarded fetch + schema validation style already implemented later in this file (`_jobs_json_valid` block at `1331-1403`), or route through a shared `gh_api_json_to_file` helper and parse numeric output only after validation.

- **ID**: **BUG-002**  
  **File path**: `.github/workflows/issue_pr_status.yml:240-257`, `.github/workflows/review_autofix.yml:3529-3545`, `.github/workflows/review_autofix.yml:3640-3662`, `.github/workflows/review_autofix.yml:4237-4253`  
  **Severity**: **Medium**  
  **Category tag**: `bug`  
  **Description**: Label transitions are implemented as **GET labels -> compute -> PUT full label set**. This has a TOCTOU window: concurrent label updates from other workflows can be overwritten by stale PUT payloads.  
  **Recommended fix**: Move to targeted add/remove label operations (GraphQL `addLabelsToLabelable`/`removeLabelsFromLabelable`, or REST add/remove with retry), and centralize in one helper (extend `scripts/label_helpers.sh` or extract `set_issue_phase_label` from `scripts/orchestrate_poll_process.sh:950-1021`).

- **ID**: **BUG-003**  
  **File path**: `.github/workflows/review_autofix.yml:4189-4195`  
  **Severity**: **Low**  
  **Category tag**: `bug`  
  **Description**: “Check PR state before failure alerts” uses raw `gh api` without `set -euo pipefail` and defaults to `"open"` on lookup failure. Transient API failure can incorrectly trigger downstream failure labeling/comments for a PR that is already closed/merged.  
  **Recommended fix**: Add `set -euo pipefail` and use `gh_retry _safe_gh_jq` with explicit unknown-state handling (`PR_CLOSED=true` fail-open for alert suppression).

- **ID**: **SHELL-001**  
  **File path**: `scripts/orchestrate_poll_process.sh:9309-9310`  
  **Severity**: **Medium**  
  **Category tag**: `shellcheck`  
  **Description**: `_sorted_issue_nums="$(printf '%s\n' ${ISSUE_NUMS} | sort -un)"` and `for inum in ${_sorted_issue_nums}; do` use unquoted expansions (SC2086), allowing word-splitting/globbing side effects.  
  **Recommended fix**: Keep issue numbers in arrays (`mapfile -t`) and iterate with quoted array expansion (`for inum in "${issue_nums[@]}"`).

---

### Section 2: GitHub API Call Redundancy Audit

- **ID**: **API-001**  
  **File path**: `.github/workflows/implement.yml:2540-2544`, `.github/workflows/implement.yml:2684-2688`  
  **Severity**: **Medium**  
  **Category tag**: `api-redundancy`  
  **Description**: The same endpoint (`GET /actions/runs/{run_id}/jobs`) is fetched repeatedly in two failure-path steps, with additional fallback call for cancelled state.  
  **Current call count**: **2–4 calls/run** (depending on status path).  
  **Proposed call count**: **1 call/run** (fetch once, persist JSON, run multiple jq queries).  
  **Batching/cache pattern to extend**: Reuse guarded JSON reuse pattern from `.github/workflows/test-and-mark-stable.yml:1352-1403` plus `gh_helpers.sh` JSON helpers.  
  **Recommended fix**: Add one “fetch run jobs JSON” step, store artifact/env file, consume in both downstream failure steps.

- **ID**: **BATCH-001**  
  **File path**: `scripts/orchestrate_poll_process.sh:7357-7363` (loop), helper at `4876-4989`  
  **Severity**: **High**  
  **Category tag**: `api-batching`  
  **Description**: Inside current-wave loop, each issue does `GET /issues/{n}` to read `.state` (`ISSUE_STATE=...`). This is per-item REST in a hot path.  
  **Current call count**: **N calls per wave**.  
  **Proposed call count**: **ceil(N/25)** by extending existing GraphQL batch fetch.  
  **Batching/cache pattern to extend**: `_fetch_candidate_issue_details_graphql` already batches labels/comments/linked PR in 25-item chunks.  
  **Recommended fix**: Add issue `state` to `_fetch_candidate_issue_details_graphql` payload and read from cached JSON instead of per-issue REST.

- **ID**: **BATCH-002**  
  **File path**: `scripts/orchestrate_poll_process.sh:8825-8841`  
  **Severity**: **High**  
  **Category tag**: `api-batching`  
  **Description**: For each blocker issue in `IF_BLOCKERS_JSON`, code calls `GET /issues/{blocker}` individually during implementation-failed recovery. This is a looped API call in a review-blocker path per CLAUDE §15.  
  **Current call count**: **B calls per source issue** (`B = blocker count`).  
  **Proposed call count**: **ceil(B/25)** with aliased GraphQL issue-state batch.  
  **Batching/cache pattern to extend**: `_fetch_linked_pr_status_graphql` chunked alias query (`5009-5105`) is a direct template.  
  **Recommended fix**: Introduce `_fetch_issue_states_graphql(numbers_json)` and consume cached state map in blocker-status loop.

- **ID**: **BATCH-003**  
  **File path**: `scripts/orchestrate_poll_process.sh:7037-7044`  
  **Severity**: **Medium**  
  **Category tag**: `api-batching`  
  **Description**: Backward scan of prior waves fetches labels one issue at a time (`/issues/{n}/labels`) for non-terminal issues.  
  **Current call count**: **P calls per poll cycle** (`P = prior non-terminal issues`).  
  **Proposed call count**: **ceil(P/25)** using one batched GraphQL labels query.  
  **Batching/cache pattern to extend**: Reuse same alias-label pattern already present for current wave (`7277-7331`).  
  **Recommended fix**: Build a prior-wave issue list and run one batched labels query before the loop.

- **ID**: **API-002**  
  **File path**: `.github/workflows/review_autofix.yml:259-269`  
  **Severity**: **Medium**  
  **Category tag**: `api-redundancy`  
  **Description**: In post-merge validate dispatch, fallback issue list path performs per-issue `gh issue view ... --json labels`. This can become N serial calls when labels are “unknown” in fallback mode. **[NEEDS VERIFICATION]**  
  **Current call count**: **N calls** in fallback path.  
  **Proposed call count**: **1 batched GraphQL call** for all fallback issue numbers.  
  **Batching/cache pattern to extend**: GraphQL alias query pattern already used in `issue_pr_status.yml:186-191` and orchestrator batch helpers.  
  **Recommended fix**: Build fallback issue-number array, fetch label membership in one GraphQL query, then loop on local JSON only.

---

### Section 3: Code Duplication & Modularization Opportunities

- **ID**: **DUP-001**  
  **File path**: `.github/workflows/clarify.yml:60-79`, `.github/workflows/plan.yml:89-108`, `.github/workflows/implement.yml:226-245`, `.github/workflows/validate.yml:81-100`, `.github/workflows/orchestrate_clarify_respond.yml:83-102`  
  **Severity**: **Medium**  
  **Category tag**: `duplication`  
  **Description**: Same “fetch canonical `resolve_integration_ref.sh` -> chmod -> execute with REPO/ISSUE/GH_TOKEN” block appears in 5 workflows.  
  **Recommended fix**: Add shared helper script (e.g., `scripts/fetch_and_run_integration_resolver.sh <issue_number> <output_var>`) and call it from all five workflows.

- **ID**: **DUP-002**  
  **File path**: `.github/workflows/review_autofix.yml:3486-3501`, `.github/workflows/review_autofix.yml:3599-3613`, reference helper `scripts/label_helpers.sh:102-136`  
  **Severity**: **Low**  
  **Category tag**: `duplication`  
  **Description**: `ensure_label_exists()` is defined twice inline in one workflow, while a canonical implementation already exists in `scripts/label_helpers.sh`.  
  **Recommended fix**: Source `scripts/label_helpers.sh` and remove inline duplicates; keep a single label catalog contract source.

- **ID**: **DUP-003**  
  **File path**: `.github/workflows/cancel_on_pr_close.yml:26-53`, `.github/workflows/mark-stable.yml:199-225`, `.github/workflows/mark-stable.yml:321-348`, `.github/workflows/orchestrate_poll.yml:62-93`  
  **Severity**: **Medium**  
  **Category tag**: `duplication`  
  **Description**: Repeated bespoke `_rl_wait` + `_gh_retry` implementations instead of shared `scripts/gh_helpers.sh`.  
  **Recommended fix**: Standardize on `gh_helpers.sh` wrappers (`gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`) by staging support scripts early in those workflows.

- **ID**: **DUP-004**  
  **File path**: `.github/workflows/issue_pr_status.yml:229-258`, `.github/workflows/review_autofix.yml:3518-3547`, `.github/workflows/review_autofix.yml:3629-3667`, `.github/workflows/review_autofix.yml:4226-4254`  
  **Severity**: **Medium**  
  **Category tag**: `duplication`  
  **Description**: Near-identical “phase label set replacement” code blocks repeated across workflows.  
  **Recommended fix**: Move to one shared function in `scripts/label_helpers.sh`, e.g. `set_issue_phase_label_atomic <repo> <issue_number> <target_label>`, and invoke from all callers.

---

### Section 4: Expression Size Limit Risk Assessment

No `run:` interpolation block exceeded the requested thresholds after measuring `${{ }}` expression-body sizes across all workflows.

- Largest measured interpolated `run:` block expression payloads:
  - `.github/workflows/implement.yml:2598-3087` → **~477 chars** (headroom ~20,523 vs 21,000 limit)
  - `.github/workflows/workflow-log-analysis.yml:258-534` → **~257 chars**
  - `.github/workflows/test-and-mark-stable.yml:1521-1611` → **~261 chars**

- Workflow file size check (800 KB warning threshold / 1 MB hard cap):
  - Largest file: `.github/workflows/review_autofix.yml` ≈ **241,033 bytes** (well below 800 KB).

No `EXPR-###` findings triggered.

---

### Section 5: Cross-Cutting Concerns

- **ID**: **DEAD-001**  
  **File path**: `scripts/orchestrate_poll_process.sh:4010-4017`  
  **Severity**: **Low**  
  **Category tag**: `dead-code`  
  **Description**: `read_standalone_state_json()` is defined but has no call sites in workflows/scripts.  
  **Recommended fix**: Remove it or wire it where intended; if intentionally reserved, annotate with explicit comment and keep test coverage.

- **ID**: **DEAD-002**  
  **File path**: `scripts/orchestrate_poll_process.sh:4119-4129`  
  **Severity**: **Low**  
  **Category tag**: `dead-code`  
  **Description**: `stall_recovery_action_is_terminal()` is defined but unused across repository call sites.  
  **Recommended fix**: Delete or integrate into stall action selection logic; add unit test if retained.

- **ID**: **CONSIST-001**  
  **File path**: `.github/workflows/cancel_on_pr_close.yml:26-53`, `.github/workflows/review_autofix.yml:113-245`, `.github/workflows/issue_pr_status.yml:183-191`, `scripts/gh_helpers.sh:332-540`  
  **Severity**: **Medium**  
  **Category tag**: `consistency`  
  **Description**: GH API retry strategy is inconsistent: some flows use centralized `gh_helpers.sh`, others use local wrappers or raw `gh api`. This fragments rate-limit behavior, alerting, and fail-open semantics.  
  **Recommended fix**: Enforce one policy: source `gh_helpers.sh` everywhere and disallow ad-hoc wrappers except bootstrapping phase before support scripts are staged.

- **ID**: **SHELL-002**  
  **File path**: `scripts/validate_driver.sh:713`, `scripts/validate_driver.sh:732`  
  **Severity**: **Low**  
  **Category tag**: `shellcheck`  
  **Description**: `[[ "${candidate_name}" == ${HELPER_PATTERN} ]]` and `[[ "$(basename "${test_file}")" == ${CANARY_PATTERN} ]]` trigger SC2053. Pattern expansion behavior depends on variable content and can become surprising if values contain wildcard meta. **[NEEDS VERIFICATION]**  
  **Recommended fix**: If literal compare is intended, quote RHS; if glob compare is intended, keep unquoted but add explicit shellcheck disable + contract comment constraining allowed pattern values.

- **ID**: **DEBT-001**  
  **File path**: `scripts/orchestrate_poll_process.sh:1-10049`, `.github/workflows/review_autofix.yml:1-4375`  
  **Severity**: **Medium**  
  **Category tag**: `tech-debt`  
  **Description**: Extremely large monolithic control-plane script/workflow increases regression risk and makes API hygiene and race analysis difficult.  
  **Recommended fix**: Split by domain modules: `stall_recovery`, `review_blocked`, `label_reconcile`, `batch_queries`, and invoke from thin workflow steps.

- **ID**: **DEBT-002**  
  **File path**: `.github/workflows/test-and-mark-stable.yml:109-117`, `.github/workflows/test-and-mark-stable.yml:115-174`  
  **Severity**: **Low**  
  **Category tag**: `tech-debt`  
  **Description**: Temporary checkout exit-128 diagnostic block is still present with “Remove after root-cause…” note; this adds maintenance noise to release flow.  
  **Recommended fix**: Gate behind explicit debug input flag or remove after creating a tracked issue with reproducer artifacts.

---

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | SEC-001, BATCH-001, BATCH-002 |
| Medium | 11 | BUG-001, BUG-002, SHELL-001, API-001, BATCH-003, API-002, DUP-001, DUP-003, DUP-004, CONSIST-001, DEBT-001 |
| Low | 6 | BUG-003, DUP-002, DEAD-001, DEAD-002, SHELL-002, DEBT-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 6 | Medium |
| API call optimization | 3 | Large |
| Code modularization | 8 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 10 | Medium |
