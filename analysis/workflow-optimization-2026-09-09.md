## Executive Summary

- **All five failures share one deterministic root cause:** PR #4057, head `c08e0d0…`, executed the PR’s copy of `scripts/review_apply_fixes.sh`, which aborted because `PR_CLOSED_SENTINEL_FILE` was unset. The workflow then incorrectly classified the pre-model abort as `editor_empty_noop`. **Impact:** 25,944 seconds and 98.95M tokens, 43.0% of all measured tokens. **Confidence: high.**
- **Review/autofix is the dominant bottleneck:** 89 runs, p95 8,745s, five failures, eight cancellations, and a 13,259s maximum. **Impact:** roughly 47.4 runner-hours in the window. **Confidence: high.**
- **Orchestrator polling is reliable but inefficient:** 107/107 successes, p50 266s. Two recent runs each spent ~87s writing start/end memory events and ~39s fetching every branch/tag. **Potential saving:** 70–80s per poll. **Confidence: medium** because step decomposition covers two recent runs.
- **Prompt caching is effective overall:** derived weighted hit rate is 76.8%, but seven context warnings reached 70–77% of model windows and 32/247 model calls lacked usage data. **Confidence: high.**
- **CI is healthy but serial:** 13/13 successes, p50 1,775s; 107 steps run within one `lint` job. Parallel sharding could cut critical-path CI by approximately 10–18 minutes. **Confidence: medium.**

## Speed Optimizations

### 1. Prevent untrusted runtime-script self-poisoning — critical path

- **Evidence:** Runs `34257678201`, `34266425657`, `34283641666`, `34290972060`, and `34297628750` all logged `PR_CLOSED_SENTINEL_FILE must be set`. All six reviewers had already succeeded.
- **Root cause:** For the workflow-source repository, `.github/workflows/review_autofix.yml` sets `SCRIPT_REF=${{ github.sha }}`. `stage_workflow_support.sh` consequently executes the PR-modified `review_apply_fixes.sh`.
- **Exact change:** Stage `review_apply_fixes.sh`, `opencode_helpers.sh`, and the config writer from the immutable default-branch snapshot. Review the PR copy as data, but never execute it. Add a pre-review environment check.
- **Logging addition:** `REVIEW_EDITOR_PREFLIGHT_V1 source_sha=<sha> target_sha=<sha> missing_env=<list> result=pass|fail`
- **Estimated saving:** 68–150 minutes whenever this defect recurs; observed aggregate saving would have been 7.2 hours.
- **Risk:** Low; fail closed before model spend.

### 2. Parallelize the 107-step CI job — critical path

- **Evidence:** CI p50 is 1,775s and p95 1,850s; recent runs `34307377665`, `34316284475`, and `34323400629` spent 26–31 minutes in `lint`.
- **Root cause:** Static checks, review contracts, orchestrator tests, and memory/validation tests execute serially.
- **Exact change:** Split into 3–4 independent jobs with separate static, review, orchestrator, and validation/memory groups.
- **Logging addition:** `CI_SUITE_TIMING_V1 suite=<name> tests=<n> failures=<n> duration_ms=<n>`
- **Estimated saving:** 10–18 minutes per CI run.
- **Risk:** Medium; preserve a final aggregate required check.

### 3. Batch poll memory events and narrow Git fetches

- **Evidence:** Poll runs `34328191600` and `34329931706` spent ~44s on `poll_started`, ~43s on `poll_completed`, and ~39s fetching all repository refs.
- **Root cause:** Two independent clone/commit/push operations plus `fetch-depth: 0`.
- **Exact change:** Buffer both memory events and push once at completion. Initially fetch default branch shallowly, then fetch only active integration refs on demand.
- **Logging additions:** `AI_MEMORY_BATCH_V1 events=<n> duration_ms=<n>` and `GIT_FETCH_TELEMETRY_V1 refs=<n> fallback_full_fetch=<bool> duration_ms=<n>`.
- **Estimated saving:** 70–80s per 266s poll; approximately two runner-hours across 107 runs.
- **Risk:** Medium for targeted fetch; low for event batching.

### 4. Resume completed reviewer work after infrastructure failures

- **Evidence:** All five failed summaries reported `resume_state=fresh`, despite identical head SHA and completed reviewer phases.
- **Exact change:** Cache the reviewer bundle by `PR + head_sha + reviewer-config-hash`; resume directly at editor preflight when unchanged.
- **Estimated saving:** 60–145 minutes and most reviewer tokens on a retry.
- **Risk:** Low if cache keys include all prompt inputs and model configuration.

**Micro-optimization:** Semble queries averaged roughly 0.4–0.8s; they are not material to end-to-end latency.

## Cost Optimizations

### 1. Stop the repeated preflight-failure loop

- **Evidence:** The five deterministic failures consumed 98,945,680 tokens—43.0% of 230,297,118 total tokens—without invoking the editor model.
- **Exact change:** Trusted script staging, early preflight, and same-head failure suppression after one identical infrastructure fingerprint.
- **Estimated saving:** Up to 98.95M tokens in this observed window.
- **Quality risk:** None; no model output is being removed.

### 2. Enable risk-tiered reviewer selection in shadow mode

- **Evidence:** `REVIEW_TIER_RESOLVER_ENABLED=false`, `REVIEWER_RISK_TIER_ENABLED=0`, six reviewers, two passes, and xhigh reasoning. There were 247 model calls.
- **Exact change:** First log proposed tiers without changing execution. Then use three reviewers for tests/docs and retain six for `scripts/`, workflows, security, migrations, and contracts.
- **Estimated saving:** 10–30% of review tokens in this repository.
- **Quality risk:** Medium; retain full-tier path matching and escalation on disagreement.

### 3. Reduce uncached prompt variance

- **Evidence:** Weighted cache hit rate is 76.8%, but model-level deep-dive rates ranged from 35.5% for Mistral pass 1 to 89.3% for Grok pass 1. Seven context warnings reached 195,941 tokens.
- **Exact change:** Keep invariant policy and tool instructions at the prefix; move timestamps, run IDs, comments, and changing check summaries later. Log prefix hashes and dynamic byte counts.
- **Estimated saving:** 5–15% of uncached prompt tokens.
- **Quality risk:** Low.

### 4. Preserve Semble; cap repeated overflow queries

- **Evidence:** 31 calls produced 370,647 bytes. Deep dives showed targeted `reviewer-context`, `overflow`, and `conflict-resolver-context` queries, normally under 1s.
- **Assessment:** Semble appears to substitute bounded excerpts for broader file expansion; no baseline exists to quantify avoided prompt tokens. It is not a major cost source.
- **Exact change:** Cache overflow results by file SHA and query hash.
- **Estimated saving:** Small latency/context reduction without quality loss.

Serena performed zero queries, tool calls, fallbacks, or probes; no efficiency comparison is possible.

## Reliability Improvements

### 1. Correct failure classification

- **Failure evidence:** A missing environment variable was reported as editor `empty_noop` in all five failures.
- **Root-cause category:** Configuration contract and trusted-code provenance.
- **Exact fix:** Add `preflight_missing_env` and `script_provenance_violation` failure classes. Reserve `empty_noop` for an invoked model returning zero usable output.
- **Expected impact:** Eliminates misleading retries and accelerates diagnosis.
- **Rollback:** Retain current fail-closed behavior; only classification and timing change.

Recommended marker:

`EDITOR_ATTEMPT_V1 invoked=<bool> model=<name> exit_code=<n> duration_ms=<n> stdout_bytes=<n> summary_bytes=<n> failure_class=<class>`

### 2. Add same-head infrastructure-failure quarantine

- **Evidence:** Five failures targeted PR #4057 and the same head SHA over approximately nine hours.
- **Exact fix:** After two identical preflight fingerprints, stop automatic full-review redispatch until the head or trusted support SHA changes.
- **Expected impact:** Prevents repeat storms and concurrency backlog.
- **Fail-open:** Allow an explicit force-review command.

### 3. Circuit-break non-transient memory authentication failures

- **Evidence:** Runs `34208354420` and `34221671378` repeatedly exhausted 16 push attempts with an authentication error; candidate and terminal writes failed open.
- **Exact fix:** Retry ref-lock, 5xx, and rate-limit failures, but classify authentication failures as non-retryable. Set a job-local memory-write breaker after the first occurrence.
- **Expected impact:** Avoids repeated 100+ second stalls while retaining fail-open workflow behavior.
- **Rollback:** Environment flag to restore existing retry behavior.

### 4. Act on context pressure before model invocation

- **Evidence:** Seven warnings: one each in `34283641666` and `34290972060`, two in `34297628750`, and three in `34303958911`.
- **Exact fix:** Emit section-level token contributions and truncate lowest-authority duplicated context before crossing 70%.
- **Expected impact:** Fewer provider failures and more stable caching.
- **Fail-open:** Continue with the current prompt when token estimation fails.

`BREAK_GLASS` count was zero, so the window shows prompt-size pressure rather than rubric/policy escape pressure.

### 5. Clean up misleading post-job warnings

Multiple review runs reported `git submodule ... cannot be used without a working tree`. Guard cleanup with `git rev-parse --is-inside-work-tree`; this is low impact but reduces false incident noise.

Semble’s 20 fallbacks were all contract-test fallbacks in CI; runtime fallbacks were zero. This is healthy test behavior, not a production rollout failure. Serena probe telemetry is absent rather than healthy.

## AI Memory Health

- **Retrieval:** 12/12 deep-review retrieves selected records: **100% hit rate**.
- **Budget:** Average 1,379.6 estimated tokens of 1,400, or **98.5% utilization**.
- **Records:** Average 30.1 selected records.
- **Keyword method:** `llm` 12/12; `plain` 0; `none` 0.
- **Zero-result retrieves:** None observed.
- **Fail-open writes:** Four operations across runs `34208354420` and `34221671378`: one `record-candidate` and one `record-run-event` per run.
- **High retries:** The above failures exhausted 16 pushes; three otherwise successful operations needed two pushes.

**Inference:** Five same-head failure runs each recorded closely related review candidates, risking duplicate memory and retrieval crowding.

Recommendations:

1. Deduplicate candidates by `PR + head_sha + normalized-finding IDs`.
2. Add `elapsed_ms`, `push_attempts`, `error_class`, and sanitized `last_error` directly to every failure telemetry object.
3. Add retrieval effectiveness fields: `records_considered`, `records_deduped`, `same_head_records`, and post-run usefulness feedback.
4. Lower the default selection target slightly or reserve 10% of the budget for genuinely new records.

## GH API Call Audit

Actual GitHub API call counts, pagination pages, retries, and rate-limit waits were not included in the assembled telemetry. No production rate-limit event was visible in the selected deep dives.

### Observed or source-supported hotspots

| Pattern | Evidence | Recommendation | Estimated reduction |
|---|---|---|---:|
| Sweep active-run snapshot | Source-code inference: three status requests × two workflows per tick | Fetch each workflow once without status filtering and filter locally; paginate only while inside the active-age window | Up to 67% of these snapshot calls |
| Fresh-push branch fallback | `STALL_FRESH_PUSH_FALLBACK` in poll runs `34328191600` and `34329931706` | Log the primary GraphQL miss reason and cache fallback result for the cycle | One repeated call per affected issue/tick |
| Per-review PR watchdog | Documented periodic PR-state lookup; no measured count | Aggregate counts and cache the unchanged head-state response | Unknown |
| Issue status synchronization | `.github/workflows/issue_pr_status.yml` already uses batched GraphQL | Preserve; run `34329958051` showed no batch-fallback warning | Already efficient |

Add one job-final marker from `gh_helpers.sh`:

`GH_API_CALL_SUMMARY_V1 endpoint=<template> method=<verb> logical_calls=<n> attempts=<n> pages=<n> retries=<n> cache_hits=<n> rate_limit_wait_ms=<n>`

Direct `gh api` call sites in gates and sweeps must either use the wrapper or increment the same job-local counter.

## Prompt Cache & Memory System

- `cache_hit_rate` was null at aggregate level, but token totals yield a weighted rate of **76.76%**: `175,249,693 / (175,249,693 + 53,071,950)`.
- Failed runs still achieved a weighted **79.3%** cache rate, meaning caching reduced marginal cost but could not offset repeated complete reviewer panels.
- Cache creation/write tokens were zero across telemetry. This may mean implicit or unavailable write reporting, not necessarily zero cache creation.
- Seven context warnings indicate prompt expansion is starting to erode headroom.
- Memory retrieval is reliable but nearly saturates its independent budget and lacks usefulness/dedup metrics.

Concrete improvements:

1. Compute and persist aggregate weighted cache hit rate rather than returning null.
2. Emit `PROMPT_CACHE_PREFIX_V1 phase=<phase> prefix_hash=<hash> stable_bytes=<n> dynamic_bytes=<n>`.
3. Reuse same-head reviewer artifacts rather than relying only on provider caching.
4. Deduplicate memory records before retrieval.

## Orchestrator Health

- **Healthy outcomes:** `orchestrate_poll` completed 107/107 runs successfully; p50 266s, p95 307s.
- **Recurring degradation:** Both recent deep dives used `STALL_FRESH_PUSH_FALLBACK` for issue #4056, indicating the primary cross-reference lacked usable push-time data.
- **Stall recovery:** Four sampled slow review runs recorded eight reviewer stall kills; three were recovered, one run (`34303958911`) eventually skipped on stale base.
- **State progression gap:** The five failed reviews had completed reviewers but remained `resume_state=fresh`.
- **Clarification evidence gap:** Clarify, plan, implement, and clarify-response families were mostly classified as skipped/other with empty archives; no supported claim about clarification-loop frequency is possible.

Track:

- Same-head failure fingerprint count.
- Pending concurrency age.
- Memory-write p50/p95.
- Poll overhead versus issue-processing time.
- `STALL_FRESH_PUSH_FALLBACK` rate and miss reason.
- Reviewer completion reuse rate.

## Pipeline Flow Bottlenecks

| Stage | Dominant issue | Evidence | Priority fix |
|---|---|---|---|
| Clarify | Mostly skipped; reason unavailable | 38/39 runs classified other | Collect skip reason from jobs metadata |
| Plan | Mostly skipped, one long tail | p50 2s; observed 849s outlier | Add phase timing and outcome markers |
| Implement | Mostly skipped, one 2,501s outlier | p50 2s | Collect model/tool phase timings |
| Review/autofix | Main compute and failure bottleneck | p95 8,745s; 98.95M failed-run tokens | Trusted support scripts and resumable reviewer bundles |
| CI/validate | Serial test bottleneck | CI p50 1,775s; 107 steps | Parallel CI shards |
| Orchestrate | High fixed overhead | 107 runs at p50 266s | Batch memory writes and narrow fetches |
| Queue/concurrency | Long pending replacement waits | Run `34304993091` waited about 90 minutes before its heavy job started; three known cancelled waits totaled 6.48h | Remove deterministic retry storms before changing concurrency policy |
| Merge/conflict | Occasional long corrective path | Run `34221682984` finished `conflict_resolved_pushed` after 8,709s | Preserve bounded resolver attempts and add phase timing |

The collector should record `job_pending_ms`, `runner_queue_ms`, and `execution_ms` separately; workflow duration currently conflates concurrency waits with compute.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**

- Review/autofix: 89 runs, p95 8,745s.
- CI: p50 1,775s.
- Orchestrator poll: 107 runs, p50 266s.

**Top failure modes**

- PR-controlled runtime script aborted on missing sentinel.
- Failure misclassified as editor no-op.
- Memory authentication failures retried 16 times.
- Pending review runs cancelled or replaced after long waits.

**Highest-cost drivers**

- Five identical failed reviews: 98.95M tokens.
- Always-full six-reviewer, two-pass xhigh configuration.
- Context growth up to 195,941 prompt tokens.

**Top three actions**

1. Make editor runtime scripts default-branch-primary and add pre-review contract checks.
2. Resume reviewer bundles and quarantine repeated same-head infrastructure failures.
3. Parallelize CI while batching poll memory events and narrowing Git fetches.

## Metrics Appendix

### Repository outcomes

| Runs | Success | Failure | Cancelled | Other/skipped | p50 | p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 414 | 251 (60.6%) | 5 (1.21%) | 8 (1.93%) | 150 (36.2%) | 13s | 4,207s |

### Major workflow families

| Family | Runs | S/F/C/O | p50 | p95 |
|---|---:|---:|---:|---:|
| review_autofix | 89 | 76/5/8/0 | 11s | 8,745s |
| orchestrate_poll | 107 | 107/0/0/0 | 266s | 307s |
| CI | 13 | 13/0/0/0 | 1,775s | 1,850s |
| copilot reviewer | 17 | 17/0/0/0 | 257s | 367s |
| clarify | 39 | 1/0/0/38 | 1s | 17s |
| plan | 38 | 1/0/0/37 | 2s | 25s |
| implement | 38 | 1/0/0/37 | 2s | 13s |
| orchestrate clarify-response | 38 | 0/0/0/38 | 1s | 15s |
| validation refresh | 1 | 1/0/0/0 | 1,216s | 1,216s |

### Model and cache telemetry

| Metric | Value |
|---|---:|
| OpenRouter calls | 247 |
| Usage available/unavailable | 215 / 32 |
| Prompt tokens | 53,071,950 |
| Completion tokens | 1,998,126 |
| Cache-read tokens | 175,249,693 |
| Cache-write tokens reported | 0 |
| Total tokens | 230,297,118 |
| Derived weighted cache hit rate | 76.76% |
| Reported aggregate `cache_hit_rate` | unavailable |
| `wall_clock_p50_ms` | 167,000 |
| `wall_clock_p99_ms` | 9,053,140 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 7 |

### MCP telemetry

| Server | Queries | Bytes | Fallbacks | Contract-test | Runtime |
|---|---:|---:|---:|---:|---:|
| Semble | 31 | 370,647 | 20 | 20 | 0 |
| Serena | 0 | 0 | 0 | — | 0 |

| Server/target | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---:|---:|---:|
| Serena / target not emitted | 0 | 0 | 0 |

Deep-review Semble targets: `reviewer-context` 12 calls/185,995 bytes; `overflow` 10/74,966; `conflict-resolver-context` 1/4,805.

### Material data gaps

- GitHub API call, retry, pagination, and rate-limit totals were not collected.
- Serena availability was logged as disabled/unavailable but no `SERENA_PROBE` marker was emitted.
- 150 skipped/other runs lack actionable skip reasons.
- Cache-write semantics and dollar-cost totals are unavailable.
- Success deep-dive sampling was 7%; failure claims above were verified against full logs.
- Other MCP servers observed: none.

## Deep Audit — Workflows & Scripts (2026-09-09)

### Section 1: Bug & Correctness Sweep

All 46 workflows and 134 scoped scripts were statically audited. YAML, Bash, and Python syntax checks passed. The previously documented untrusted `review_apply_fixes.sh` execution and failure-classification defects are not duplicated here.

#### SEC-001

- **File path and lines:** `scripts/resolve_integration_ref.sh:8-20,43-56`; `.github/workflows/clarify.yml:119-141`; `.github/workflows/plan.yml:182-209`; `.github/workflows/implement.yml:383-389,1254-1272`; `.github/workflows/orchestrate_clarify_respond.yml:173-198`; `.github/workflows/validate.yml:154-176`
- **Severity:** High
- **Category:** `security`
- **Description:** `extract_integration_branch` accepts any non-newline/non-backtick text and only verifies that the named branch exists. Five workflows then interpolate that output directly into shell source, for example `echo "Resolved ref: ${{ steps.refctx.outputs.ref ... }}"`. **Inference:** an existing branch containing shell substitutions such as `$()` could execute commands when Actions expands the expression before Bash parses the step. Exploitability depends on GitHub’s accepted branch-name character set and the attacker’s ability to create the branch. [NEEDS VERIFICATION]
- **Recommended fix:** Validate resolver output against a restrictive ref pattern, then pass it through step `env`, never directly inside `run:`. Follow `.github/workflows/implement.yml:398-401`’s `DEFAULT_CHECKOUT_REF` pattern and log with `printf '%s\n' "${RESOLVED_REF}"`.

#### BUG-001

- **File path and lines:** `scripts/label_helpers.sh:174-224`; `scripts/review_rb_judge.sh:782-814`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** Both phase-swap helpers perform a GET of every issue label, compute a replacement list, then PUT the complete list. A concurrent workflow adding a non-phase label between GET and PUT can have its update silently overwritten.
- **Recommended fix:** Centralize phase mutation in `label_helpers.sh`; remove only known phase labels with targeted DELETE operations, POST the target label, then re-read and verify. Do not replace unrelated labels from a stale snapshot.

#### BUG-002

- **File path and lines:** `scripts/orchestrate_poll_process.sh:3598-3616`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** For `ai:ready-to-merge` issues, the sweep logs a failed `ai:merged` backfill but proceeds to close the issue. This contradicts the immediately documented requirement that concurrent readers see `ai:merged` before closure.
- **Recommended fix:** If the label transition fails, skip closure and retry on the next poll. Close only after a verified label read contains `ai:merged`.

#### BUG-003

- **File path and lines:** `scripts/resolve_integration_ref.sh:38-92`; `.github/workflows/clarify.yml:125-129`; `.github/workflows/plan.yml:188-195`; `.github/workflows/implement.yml:389-393`; `.github/workflows/orchestrate_clarify_respond.yml:179-183`; `.github/workflows/validate.yml:160-164`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The resolver uses raw `gh api` calls without `gh_retry`. Any transient issue-body or ref lookup failure causes callers to discard the integration ref and fall back to the default branch. For orchestrator-managed work, that can produce planning or implementation against the wrong base. [NEEDS VERIFICATION]
- **Recommended fix:** Source sibling `gh_helpers.sh`, use `gh_retry _safe_gh_jq` for issue reads, and retry ref verification while preserving the existing non-retryable 404 path.

### Section 2: GitHub API Call Redundancy Audit

#### API-001

- **File path and lines:** `scripts/gh_helpers.sh:610-663,665-727`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** `gh_api_json_to_file` and `curl_gh_api` retry all non-rate-limit failures, including permanent authentication and validation errors. This is the helper-level form of the authentication retry problem already described in Reliability Improvements §3.
- **Current call count:** Up to 5 attempts per permanent failure.
- **Proposed call count:** 1 attempt for classified permanent 4xx failures.
- **Recommended fix:** Apply `_is_gh_permanent_failure`, already used by `gh_retry` at `scripts/gh_helpers.sh:458-465`, before sleeping or retrying.

#### API-002

- **File path and lines:** `scripts/orchestrate_poll_process.sh:12501-12625,16663-16687`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** `_fetch_candidate_issue_details_graphql` already returns current-wave labels, but the caller immediately invokes `_fetch_issue_labels_batch_graphql` for the same issue set.
- **Current call count:** `2 × ceil(N/25)` GraphQL calls.
- **Proposed call count:** `ceil(N/25)` calls.
- **Recommended fix:** Populate `LABELS_JSON` from `_current_wave_details_json`; invoke `_fetch_issue_labels_batch_graphql` only for keys missing after a failed or partial details batch.

#### BATCH-001

- **File path and lines:** `scripts/orchestrate_poll_process.sh:13308-13320`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Standalone stall discovery executes one `gh issue list` per seven phase labels.
- **Current call count:** 7 logical list calls per poll.
- **Proposed call count:** 1 aliased GraphQL query for up to 100 results per label, with paginated fallback only for overflowing aliases.
- **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql`’s aliased-search pattern to include the seven label queries and return a deduplicated issue-number set.

#### BATCH-002

- **File path and lines:** `scripts/orchestrate_poll_process.sh:18486-18520`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** After review-blocked handling, labels are re-fetched through one REST call per current or reissued issue.
- **Current call count:** Up to `U` calls for `U` unique issue numbers.
- **Proposed call count:** `ceil(U/25)` GraphQL calls.
- **Recommended fix:** Build one deduplicated JSON number array and call the existing `_fetch_issue_labels_batch_graphql` helper.

#### BATCH-003

- **File path and lines:** `.github/workflows/review_autofix.yml:1152-1191`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The deterministic-skip path discovers all closing issues in one GraphQL query but applies `ai:ready-to-merge` through one REST POST per issue.
- **Current call count:** `1 + N` calls for `N` linked issues, excluding label creation.
- **Proposed call count:** 2 calls: one discovery query returning node IDs and one aliased GraphQL mutation.
- **Recommended fix:** Return issue node IDs and the target label ID from the discovery query, then issue aliased `addLabelsToLabelable` mutations. Extend the alias-building pattern used by `_fetch_candidate_issue_details_graphql`. [NEEDS VERIFICATION]

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001

- **File path and lines:** `.github/workflows/clarify.yml:57-129`; `.github/workflows/implement.yml:320-393`; `.github/workflows/orchestrate_clarify_respond.yml:110-183`; `.github/workflows/plan.yml:120-195`; `.github/workflows/validate.yml:96-164`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The 2,964-character “Resolve integration ref” block is byte-equivalent across three workflows and near-identical in two more.
- **Recommended fix:** Create `.github/actions/resolve-integration-ref` with inputs `(issue_number, repository, workflow_source_ref, token)` and output `ref`; replace all five inline clone/stage/run blocks.

#### DUP-002

- **File path and lines:** `scripts/review_apply_fixes.sh:164-202`; `scripts/review_rb_judge.sh:256-294`; `scripts/review_run_reviewers.sh:69-107`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` is duplicated verbatim in three model-execution scripts.
- **Recommended fix:** Move it to `scripts/cost_audit_helpers.sh` as `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>` and source that module from all three callers.

#### DUP-003

- **File path and lines:** `scripts/review_apply_fixes.sh:909-918`; `scripts/review_conflict_prepare.sh:605-614`; `scripts/review_run_reviewers.sh:1760-1769`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `append_semble_query_section` is duplicated verbatim.
- **Recommended fix:** Add `semble_append_query_section <label> <path> [max_bytes]` to `scripts/semble_helpers.sh` and update the three callers.

#### DUP-004

- **File path and lines:** `scripts/watchdog_helpers.sh:186`; `scripts/review_conflict_resolve.sh:255-269`; `scripts/review_rb_judge.sh:168-182`; `scripts/review_run_reviewers.sh:324-338`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Three scripts duplicate `read_codex_stall_guard_state` even though `watchdog_helpers.sh` already defines the shared helper.
- **Recommended fix:** Make `watchdog_helpers.sh` authoritative with signature `read_codex_stall_guard_state <status_file>` and remove the three local copies.

### Section 4: Expression Size Limit Risk Assessment

All 46 workflows were parsed and every interpolated `run:` scalar measured. No block crosses the 15,000-character Medium threshold or 18,000-character High threshold; therefore no `EXPR-###` finding is emitted.

| Workflow block | Characters | Headroom to 21,000 |
|---|---:|---:|
| `implement.yml:3850-4079` | 12,608 | 8,392 |
| `implement.yml:855-1135` | 12,571 | 8,429 |
| `implement.yml:2963-3216` | 11,403 | 9,597 |
| `validate.yml:207-427` | 10,986 | 10,014 |
| `plan.yml:1476-1678` | 10,938 | 10,062 |
| `workflow-log-analysis.yml:881-1140` | 10,884 | 10,116 |

`validate.yml:207-427` contains an inline manifest heredoc, but currently retains 10,014 characters of headroom. The largest `if:` expression is 739 characters. No workflow exceeds the 800 KB warning threshold; the largest is `review_autofix.yml` at approximately 462,186 characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001

- **File path and lines:** `scripts/orchestrate_poll_process.sh:9476-9484,11067-11086,11196-11206`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `get_last_validation_run_conclusion`, `read_standalone_state_json`, and `stall_recovery_action_is_terminal` have no references elsewhere in workflows, scripts, or tests.
- **Recommended fix:** Remove the three helpers, or add explicit callers and focused tests if they represent deferred compatibility contracts.

#### CONSIST-001

- **File path and lines:** `.github/ai/label_contract.v1.json:1-203`; `scripts/label_helpers.sh:9-128`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** Label colors, descriptions, and phase membership are maintained in both JSON and hardcoded Bash maps. The comment explicitly says the shell catalog “mirrors” the contract, creating two writable authorities.
- **Recommended fix:** Generate the Bash fallback catalog from `label_contract.v1.json` during support staging, or commit a generated shell artifact with a CI parity check.

#### DEBT-001

- **File path and lines:** `scripts/orchestrate_poll_process.sh:1-20817`
- **Severity:** Medium
- **Category:** `tech-debt`
- **Description:** The poller is 20,817 lines and approximately 1.07 MB, with 338 GitHub API-related call sites. **Inference:** this concentration increases review, API-accounting, and state-transition regression risk.
- **Recommended fix:** Incrementally extract sourced modules for label/state mutation, stall recovery, validation lifecycle, merge/finalization, and API-prefetch caches. Preserve the current entrypoint and add contract tests before each extraction.

No TODO/FIXME/HACK markers were found. ShellCheck found no high-confidence SC2086, SC2046, SC2006, or equivalent unquoted-expansion defect not already represented above. The raw-API consistency issue is captured by BUG-003.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 8 | BUG-001, BUG-002, BUG-003, API-001, API-002, BATCH-001, BATCH-003, DEBT-001 |
| Low | 7 | BATCH-002, DUP-001, DUP-002, DUP-003, DUP-004, DEAD-001, CONSIST-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 6 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 13 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 7 | Medium |
