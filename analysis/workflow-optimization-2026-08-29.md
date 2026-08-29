## Executive Summary

- **Move CI policy guards to the start of `lint`.** Five runs—33230824404, 33232922397, 33234826104, 33243011464, and 33245461068—failed after 551–593s with the identical missing `codex_config_assemble` guard. Potential saving: **up to 47 minutes per comparable window**. **Confidence: high.**
- **Review/autofix is the dominant tail and cancellation source.** Its p95 was **6,392.6s**; 17/84 runs were cancelled, consuming **29,346s (8.15h) of workflow wall time**. Seven were labelled `cancelled_before_first_step`, accounting for 4.94h of queue/wait time. **Confidence: high; cancellation cause is medium-confidence.**
- **Three reviewer failures shared one deterministic render defect.** Runs 33245886964, 33246465915, and 33246480829 failed on unresolved `REFERENCE_SECURITY_MONEY_LENS` content after lengthy check-run waits. Pre-render validation could save roughly **five minutes per deterministic failure**. **Confidence: high.**
- **Wrapper routing creates substantial noise.** Clarify, plan, implement, and clarify-response produced **718 skipped runs** and 1,993s of lifecycle time. Mirroring reusable-workflow predicates in the wrappers can remove nearly all of these runs. **Confidence: high.**
- **Prompt caching is effective but incompletely measured.** OpenRouter cache hit rate was **85.53%**, with 34.88M cache-read tokens; increasing this to 90% would shift approximately **1.82M tokens** from uncached prompts to cache reads. Only 39/116 usage records contained numeric totals. **Confidence: medium.**
- **Telemetry aggregation is inflated.** Run 33246562953 contains both combined-job and per-step copies: 116 OpenRouter calls dedupe to 101, and 18 Semble queries dedupe to 16. **Confidence: high.**

## Speed Optimizations

| Rank | Finding and evidence | Exact change | Estimated saving | Risk |
|---|---|---|---|---|
| 1 — Critical path | The cheap guard at `.github/workflows/ci.yml:743-810` ran after roughly nine minutes in all five CI failures. | Move “Shared shell-block anti-regression checks” directly after checkout/setup. Emit `CI_GUARD_FAILURE guard=codex_config_assemble file=... expected=...`. | Up to **551–593s per affected run; 47m total observed**. | Low |
| 2 — Routing | 718 phase workflows were skipped: clarify 180, plan 179, implement 179, clarify-response 180. | Copy each reusable workflow’s complete event/author/body predicate into its `internal-*` wrapper. Most importantly, filter `/reclarify` and `Clarification required` at wrapper level. | Removes **718 workflow records and 33.2m lifecycle time**; likely reduces burst queueing. | Low–medium |
| 3 — Critical path | Reviewer runs waited for checks before encountering a deterministic prompt-render failure. Runs 33245886964 and 33246465915 explicitly reached `CHECK_RUNS_WAIT_TIMEOUT` after 300s. | Perform prompt assembly/hydration preflight before check-run polling. Keep strict hydration for trusted templates; leave unresolved placeholders intact only in untrusted assembled diff content. | Approximately **300s per deterministic render failure**. | Low |
| 4 — Queueing | Seven review runs were cancelled before their first step; three waited 5,801–6,004s. Sweep runs 33249093537 and 33250276921 skipped PRs because active runs existed. | Extend existing sweep snapshots with `head_sha` and run ID, using fields already available from current API responses. Suppress only runs matching the current PR head; cancel superseded queued heads. | Could recover most of the observed **4.94h pre-step wait**. | Medium; fail safe when SHA is unknown |
| 5 — Diagnostic prerequisite | Poller p50 was 266.5s against a five-minute schedule, with serialized concurrency. | Emit `ORCHESTRATOR_PHASE_START/END` for state fetch, reconstruction, stall scan, judge, writes, and cleanup before changing cadence. | Enables identification of the dominant portion of **13,285s across 46 polls**. | None |

The existing four-way poll-test sharding is a successful critical-path fix: previous sequential runs produced eight 1,817–1,820s cancellations; current CI documents approximately 3.6× test acceleration.

## Cost Optimizations

| Rank | Evidence/root cause | Exact change | Estimated saving | Quality risk |
|---|---|---|---|---|
| 1 | Cache hit rate was 85.53%; logical OpenRouter input was 40.79M tokens. | Keep stable instructions, tool schemas, and rubric text before the cache breakpoint. Move SHAs, timestamps, diffs, check results, and memory results to a canonical dynamic suffix. Log prefix fingerprint and static/dynamic byte counts. | Reaching 90% would shift approximately **1.82M uncached tokens** to cache reads. | Low |
| 2 | Two-pass review runs logged 13–15 calls. Both small- and large-diff pass-two reasoning currently default to `xhigh`. | Canary `REVIEWER_PASS2_REASONING_SMALL=medium` for diffs below the existing 200-LOC threshold; retain `xhigh` for large/sensitive changes. | Expected **10–25% pass-two latency/completion reduction** on qualifying PRs. | Medium; monitor unique findings |
| 3 | Cancelled review runs consumed 8.15h of workflow wall time. | Apply current-head-aware queue cleanup described above. Emit cancellation reason, concurrency group, superseding run ID, and queue age. | Significant runner/queue savings; token saving is unquantified because cancelled runs lacked usage telemetry. | Low–medium |
| 4 | Copilot runs 33249586102 and 33250310937 built 45,514- and 49,338-token prompts. | Log prompt sections separately and deduplicate repeated repository instructions, check summaries, and memory excerpts before model invocation. | Potential **5–15% prompt reduction**, pending section telemetry. | Low |
| 5 | Semble reported 189,144 bytes over 18 calls and only 10.46s total query time. All 45 full-window fallbacks were contract tests, not runtime failures. | Keep Semble enabled. Add `candidate_bytes`, `selected_bytes`, `bytes_avoided`, and `chunks_considered` to prove context reduction. | Current latency cost is negligible; token benefit cannot yet be quantified. | None |

Model deletion is not justified. Moonshot accounted for 20.05M reported tokens, or 48.6%, but most were cache reads and only 39/116 usage records were numeric. Add per-model `unique_findings`, `consensus_adopted`, and `editor_actioned` metrics first.

Serena recorded zero queries, tool calls, response bytes, fallbacks, or probes. There is no evidence that it replaced downstream work in this window.

## Reliability Improvements

1. **Fix untrusted placeholder handling.**  
   - **Evidence:** Runs 33245886964, 33246465915, and 33246480829 failed with the same missing reference file.
   - **Category:** Deterministic input/rendering defect.
   - **Fix:** Fail open only for unresolved `REFERENCE_*` tokens originating from untrusted assembled content; retain strict trusted-template behavior. Emit `PROMPT_RENDER_RESULT strict=... unresolved_count=... source=untrusted_diff`.
   - **Impact:** Removes the entire observed reviewer-failure cluster.
   - **Rollback:** Restore strict mode without changing trusted rendering.

2. **Make policy-guard failures immediately actionable.**  
   - The existing annotation lost the offending call-site detail in collected logs. Emit the file, matched line, expected helper, scanned files, and guard version before exiting.
   - Expected impact: faster diagnosis and no repeated blind reruns.

3. **Instrument cancellation semantics.**  
   - `Free disk space` was merely the active step in seven cancellations, not demonstrated disk failure.
   - Emit `WORKFLOW_CANCELLATION reason=concurrency|manual|pr_closed|superseded superseding_run_id=... queue_ms=... active_step=...`.
   - Fail open when GitHub does not expose a reason.

4. **Monitor the new CI sharding fix.**  
   - Add per-shard start/end timestamps, test count, duration, slowest test, and timeout status. Current logs provide test counts but no shard durations.
   - Alert only if a shard exceeds its historical p95; preserve sequential fallback.

5. **Expose MCP availability failures.**  
   - Poll runs 33249588669, 33249951589, and 33250260532 reported `SEMBLE_ENABLED=true`, `SEMBLE_AVAILABLE=false`, and `SEMBLE_INDEX_AVAILABLE=false`, but emitted no `SEMBLE_PROBE`.
   - Emit separate install and index probes with target, result, reason, and duration. Continue failing open.

6. **Deduplicate collector inputs.**  
   - Run 33246562953 duplicated 15 OpenRouter events and two Semble events between `step-001-codex-agent.log` and step-specific logs.
   - Prefer step-specific logs when present, or add a stable telemetry `event_id`. Rollback is trivial.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts were both zero, so this window shows neither rubric-pressure escapes nor collector-detected context-window pressure.

## AI Memory Health

- **Retrieval:** 10/10 valid deep-dive retrieves selected records: **100% hit rate**.
- **Budget use:** Average estimated context was **1,391.9 of 1,400 tokens (99.4%)**.
- **Keyword method:** `llm` 10, `plain` 0, `none` 0.
- **Zero-result/disabled retrieves:** None observed.
- **Push reliability:** 29 events reported push attempts; six required retries. Run 33232695908 needed three attempts for completion, but all reported success.
- **Empty learning writes:** Eight `write_lessons_learned` events produced `count=0`, `did_push=false`. Add `reason=no_candidates|deduplicated|disabled`.
- **Healthy fail-open:** Run 33249762124 emitted `finalize-task` with `reason=no_linked_issues`; this is an expected no-op.
- No evidence was available for `promote`, `compact`, or processed-command operations. Verify their telemetry emission paths.

Memory retrieval is healthy and too small to be a priority cost target. The main gap is retry-cause and relevance telemetry.

## GH API Call Audit

- **Exact call counts are not collected.** No logged rate-limit, HTTP 429, or secondary-rate-limit events were found.
- Repository hygiene is strong: `CLAUDE.md:439-473` requires reuse, batched GraphQL, cycle-local caches, and fail-open fallback.
- The review sweep already snapshots active runs per workflow/status instead of performing N×workflow lookups. With four candidates in runs 33249093537 and 33250276921, this avoids the earlier per-PR fanout pattern.
- **Highest unquantified risk:** `orchestrate_poll`, because it ran 46 times and contains multiple issue/PR/check-run loops. This is an inference from code structure, not measured call volume.

Add wrapper-level telemetry without new API calls:

- `GH_API_CALL endpoint_class=... method=... attempt=... duration_ms=... result=... cache_hit=...`
- `GH_API_SUMMARY total=... retries=... rate_limits=... permanent_failures=... cache_hits=...`
- Normalize endpoints and omit query contents, tokens, bodies, and identifiers that may contain sensitive data.

For sweep active-run checks, include run IDs, status, age, and head SHA in `AUTOFIX_SWEEP_SKIP`; these fields already exist in current responses.

## Prompt Cache & Memory System

- **Cache hit rate:** 85.5272%.
- **Cache reads:** 34,883,076 tokens.
- **Cache writes:** 0 reported. High reads with zero writes indicate provider/collector observability mismatch rather than proof that no cache entries were created.
- **Uncached prompt:** 5,902,869 tokens.
- **Context warnings:** 0.
- **Memory:** Retrievals were consistently successful but consumed almost their complete 1,400-token budget.

Likely fragmentation sources—an inference—are dynamic SHAs, timestamps, check-run snapshots, and changing memory content appearing before the cache boundary. Add:

- stable-prefix SHA/fingerprint;
- static prefix and dynamic suffix byte/token counts;
- cache lookup result per call;
- cache creation/read fields as returned by the provider;
- memory record count and score distribution.

Do not reduce the memory budget until relevance or downstream-use telemetry indicates low value.

## Orchestrator Health

- Polling completed successfully in all **46 runs**, but p50 was 266.5s and p95 482.25s. Since concurrency is serialized and the schedule is every five minutes, median utilization is roughly 89% of the interval.
- Recent polls 33249588669, 33249951589, and 33250260532 each found one active tracking issue while Semble remained unavailable.
- Review sweeps repeatedly found all candidates blocked by active runs: PRs #3883, #3882, #3880, and #3848 in run 33250276921.
- The latest promotion and forward merge were healthy: runs 33250620994 and 33250638467 completed in 35s and 41s.
- No evidence of conflict-heal loops or terminal-state corruption was supplied.

Track per poll:

- state-fetch/reconstruction/judge/write durations;
- active issue count and actionable issue count;
- skipped recovery counts by reason;
- queued poll delay;
- active review run age/head SHA;
- conflict dispatches and dedupe suppressions.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify/plan/implement routing | Broad event fanout | 718 skipped runs | Mirror predicates in wrappers |
| Review/autofix | Two sequential review passes, long model/editor calls, queue churn | p95 6,392.6s; model gaps 500–624s; editor gaps 300–839s | Current-head cancellation cleanup; small-diff reasoning canary |
| CI | Large process-level test suite and late guards | p50 1,202s; eight historical 30m cancellations; five late guard failures | Keep sharding; move guards first |
| Orchestrate poll | Cadence nearly saturated | p50 266.5s on five-minute schedule | Add phase timers, then optimize dominant phase |
| Copilot review | Large prompt/model processing | p50 191s; 45–49k-token prompts | Section-level prompt telemetry and dedupe |
| Validate | Sparse evidence | One 409s success | Collect additional samples |
| Merge/promote | No material bottleneck | Latest runs 35–41s | No change |

Queueing and redundant run creation are safer first targets than reducing reviewer coverage.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 106.5m; CI p50 20m; poller p50 4.4m.
- **Top failure modes:** five identical late CI guard failures; three identical prompt-render failures; 25 cancellations.
- **Highest-cost drivers:** 41.2M reported OpenRouter tokens, two-pass six-model review, and 8.15h of cancelled-run wall time.
- **Top actions:**
  1. Move deterministic CI guards immediately after setup and add structured failure fields.
  2. Complete untrusted-reference fail-open handling and run prompt preflight before check polling.
  3. Add head-aware cancellation plus deduplicated GH/API/model/MCP telemetry.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1,000 | 248 | 8 | 25 | 719 | 2s | 609s |
| CI | 33 | 20 | 5 | 8 | 0 | 1,202s | 1,819.4s |
| Review/autofix | 84 | 63 | 3 | 17 | 1 | 31.5s | 6,392.6s |
| Orchestrate poll | 46 | 46 | 0 | 0 | 0 | 266.5s | 482.25s |
| Copilot reviewer | 25 | 25 | 0 | 0 | 0 | 191s | 266s |

### AI and cache metrics

| Metric | Value |
|---|---:|
| Codex calls / tokens | 22 / 32,422 |
| OpenRouter calls | 116 reported; 101 deduped |
| Prompt tokens | 5,902,869 |
| Completion tokens | 433,817 |
| Cache-read tokens | 34,883,076 |
| Cache-write tokens | 0 |
| Total tokens | 41,217,524 |
| Cache hit rate | 85.5272% |
| Numeric usage coverage | 39/116 calls |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |
| Full-window wall-clock p50 / p99 | 8,000ms / 6,731,440ms |
| Runs with assembled log telemetry | 118 |

The downloaded deep-dive `summary.json` covered 33 selected logs and therefore had a selection-biased wall-clock p50 of 561,000ms.

### Cancellation and failure clusters

| Cluster | Count | Wall time |
|---|---:|---:|
| Review cancellations | 17 | 29,346s |
| Cancelled before first step | 7 | 17,768s |
| CI poll-test cancellations | 8 | 14,548s |
| Late CI guard failures | 5 | 2,828s |
| Reviewer render failures | 3 | 2,281s |

### Semble

| Target | Calls | Bytes | Query time |
|---|---:|---:|---:|
| Reviewer context | 8 reported | 116,167 | 4,453ms |
| Overflow | 10 reported | 72,977 | 6,008ms |
| Total | 18 reported / 16 deduped | 189,144 reported / 167,878 deduped | 10,461ms |

Full assembled telemetry recorded **45 fallbacks**, all `target=overflow`, `context=contract-test`; runtime fallbacks were zero.

### Serena and MCP availability

| System/target | Queries | Response bytes | Tool calls | Fallbacks | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|---:|
| Serena / no target emitted | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Semble / orchestrate-poll | — | — | — | 0 runtime | 0 | 0 | 0 |

Semble unavailability was visible only through environment booleans, not probe events. **Other MCP servers observed:** none.

### GH API signals

| Metric | Result |
|---|---|
| Exact calls | Not collected |
| Rate-limit events | 0 observed |
| Retry events | 0 observed |
| Primary structural hotspot | Scheduled orchestrate poller |
| Existing batching | Review sweep snapshots; orchestrator GraphQL prefetch/cache |

## Deep Audit — Workflows & Scripts (2026-08-29)

### Section 1: Bug & Correctness Sweep

Audit coverage: 46 workflows and 132 scripts. All YAML, Python, and Bash files passed syntax parsing. Previously documented routing, late-CI-guard, and prompt-render findings are not duplicated here.

#### SEC-001 — Untrusted triage inputs reach a secret-bearing shell before fork validation
- **File path / lines:** `.github/workflows/internal-check-failure-triage.yml:20-27`; `.github/workflows/check_failure_triage.yml:96-126,288-295`
- **Severity:** High
- **Category:** `security`
- **Description:** The wrapper forwards raw check-run fields, including `check_name`, into a job carrying `GH_TOKEN`, `OPENROUTER_API_KEY`, and Telegram credentials. The first step interpolates these inputs directly into Bash source before the fork-origin guard runs. Quotes, backticks, or `$(...)` in an input would be interpreted by Bash. The guard and failure notifier repeat this pattern. Whether a fork can control a check name that reaches this event is deployment-dependent. [NEEDS VERIFICATION]
- **Recommended fix:** Pass every input through `env:`, validate numeric IDs and SHA formats, and use `printf '%s\n'`. Move fork validation into a credential-minimal prerequisite job and gate the secret-bearing job on its `same_repo` output.

#### SEC-002 — Dispatch and reusable-workflow inputs are interpolated directly into Bash
- **File path / lines:** `.github/workflows/validation-refresh.yml:89-155`; `.github/workflows/workflow-log-analysis.yml:82-176`; `.github/workflows/mark-stable.yml:71-85`; `.github/workflows/test-and-mark-stable.yml:176-190`; `.github/workflows/validate.yml:425-435`
- **Severity:** High
- **Category:** `security`
- **Description:** Values such as `repos_file`, `branch_name`, `repos_override`, `since`, `version_tag`, and `tracking_issue` are inserted into shell source before their runtime validation. Several affected jobs possess cross-repository PATs or content-write capability. Validation after interpolation cannot prevent command substitution during shell parsing.
- **Recommended fix:** Declare each value in the step’s `env:` block and reference only quoted shell variables. Validate path inputs against an allow-list, repository names against `owner/repo`, numeric inputs before arithmetic, and version tags before use.

#### BUG-001 — Wrapper-update invocations can race direct pushes
- **File path / lines:** `.github/workflows/update_workflows.yml:42-52,453-552`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The reusable job has no concurrency group, checks out the caller’s branch, creates a commit, and runs an unconditional `git push`. Concurrent scheduled, dispatch, or `workflow_call` invocations can start from the same parent; the later push then fails non-fast-forward after doing all update work. This is an inferred overlap race. [NEEDS VERIFICATION]
- **Recommended fix:** Add job-level concurrency using `update-workflows-${{ github.repository }}` with `cancel-in-progress: false`. Before committing, refresh the target branch and recompute or safely rebase generated changes.

#### BUG-002 — Changelog-only updates bypass the update notification
- **File path / lines:** `.github/workflows/update_workflows.yml:453-555,589-640`
- **Severity:** Low
- **Category:** `bug`
- **Description:** The commit predicate includes `changelog_assets_has_changes` and `changelog_assembled`; the notification predicate omits both. The notification body also never adds the already-computed changelog asset or assembly details. An enabled notification therefore remains silent for a changelog-only commit.
- **Recommended fix:** Reuse one shared `HAS_MANAGED_CHANGES` output for commit and notification gates, and include changelog asset and assembly summaries in `MSG`.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Label synchronization performs one existence read per contract label
- **File path / lines:** `scripts/ai_labels.py:433-624`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** The current 46-label contract causes 46 individual label GETs before any required writes. A create race can add another re-read. Current reads: **46**; proposed reads: **1 paginated repository-label listing**, with mutation counts unchanged.
- **Recommended fix:** Fetch `GET /repos/{repo}/labels?per_page=100` once, build a name-keyed cache, and retain the existing 422 re-read only as a race fallback. Mirror the cycle-local `LABELS_JSON` cache pattern used by `_fetch_issue_labels_batch_graphql`.

#### API-002 — Ad-hoc retry loops retry permanent failures or miss transient failures
- **File path / lines:** `.github/workflows/check_failure_triage.yml:117-135`; `scripts/dispatch_and_watch_workflow_run.sh:66-90`; `scripts/comprehensive_test_and_release_gh_api.sh:3-47`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Triage retries every failure up to three times; workflow dispatch retries every failure up to `DISPATCH_MAX_ATTEMPTS`; the release helper retries only messages containing “rate limit,” missing normal 5xx/network failures. Current permanent-error counts are up to **3**, **D**, and **1** respectively; proposed permanent-error count is **1**, while transient failures retain their existing bounded caps.
- **Recommended fix:** Route these paths through a shared status-aware retry helper based on `gh_helpers.sh::gh_retry`, including reset-aware rate-limit handling, transient HTTP classification, exponential backoff, and immediate termination for permanent 4xx responses.

#### BATCH-001 — Consumer drift audit scales as repositories × templates
- **File path / lines:** `scripts/audit_consumer_drift.py:397-480,530-531`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Each repository uses one directory-list call followed by one raw-content call per installed canonical template. With 13 registered consumers and 15 templates, the current worst case is **208 logical calls** before retries. A GraphQL alias query can fetch all 15 paths per repository in **13 calls**.
- **Recommended fix:** Add a per-repository GraphQL query using aliased `object(expression: "HEAD:.github/workflows/<file>")` blob selections. Extend the alias-batching pattern used by `_fetch_candidate_issue_details_graphql`; retain current REST reads only as per-repository fallback.

#### BATCH-002 — Current-wave reconciliation discards an existing batched PR timeline
- **File path / lines:** `scripts/orchestrate_poll_process.sh:10896-11018,14710-14787`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** `_fetch_candidate_issue_details_graphql` already fetches up to 50 PR cross-references for every issue, but transforms them into only the last closing PR. Reconciliation then performs one additional timeline call per issue and one REST PR fetch per candidate. Current calls: **ceil(N/25) + N + M**; proposed: **ceil(N/25)**, where `M` is the number of cross-referenced PR candidates.
- **Recommended fix:** Add PR body and repository identity to the existing GraphQL selection and retain a `linked_pr_candidates[]` array in its cache. Perform `_pr_json_is_issue_implementation_pr`-equivalent classification from that cached data.

#### BATCH-003 — Post-mutation label and blocker refreshes revert to per-item REST
- **File path / lines:** `scripts/orchestrate_poll_process.sh:16501-16535,16550-16666`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** After review-blocked mutations, every current/reissued issue’s labels are fetched individually. Implementation-failed blocker states are then fetched individually. Current calls: **N + R + B**; proposed: **ceil((N+R)/25) + ceil(B/25)**.
- **Recommended fix:** Re-run `_fetch_issue_labels_batch_graphql` for the union of current and reissued issues, and use `_fetch_candidate_issue_details_graphql` for blocker states. Preserve per-item REST only for missing batch keys.

#### BATCH-004 — Review-thread resolution issues one mutation per thread
- **File path / lines:** `scripts/review_resolve_review_threads.sh:150-167,259-339`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The script batches thread discovery but performs one GraphQL resolve mutation per selected thread. With `REVIEW_RESOLVE_THREADS_MAX=50`, the mutation phase costs **R + I** calls, where `I` is ignored-thread replies. Batched aliases reduce this to **ceil(R/25) + I**, or at most 2 resolve requests.
- **Recommended fix:** Generate bounded aliased `resolveReviewThread` mutations, parse success per alias, and leave failed aliases unresolved. Extend the alias-fragment approach in `_fetch_issue_labels_batch_graphql`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Integration-ref bootstrap is copied byte-for-byte across three workflows
- **File path / lines:** `.github/workflows/clarify.yml:61-130`; `.github/workflows/implement.yml:323-392`; `.github/workflows/orchestrate_clarify_respond.yml:115-184`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** These workflows contain identical 2,963-character blocks that clone the workflow source, select a fallback ref, redact clone logs, and invoke `resolve_integration_ref.sh`.
- **Recommended fix:** Introduce `scripts/resolve_integration_ref_bootstrap.sh resolve <repo> <issue> <script-ref>`, or a composite action exposing the same three inputs and a `ref` output. Update all three callers.

#### DUP-002 — Seven workflows retain bespoke support-staging implementations
- **File path / lines:** `.github/workflows/check_failure_triage.yml:196-259`; `.github/workflows/clarify.yml:215-351`; `.github/workflows/implement.yml:851-1153`; `.github/workflows/orchestrate.yml:340-469`; `.github/workflows/orchestrate_clarify_respond.yml:277-412`; `.github/workflows/orchestrate_poll.yml:331-523`; `.github/workflows/plan.yml:278-414`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** Each block independently maintains script, schema, prompt, fallback-ref, and optional-asset lists. `review_autofix.yml` and `validate.yml` already use `scripts/stage_workflow_support.sh`, proving a centralized pattern exists.
- **Recommended fix:** Extend `stage_workflow_support.sh` with `stage --profile <phase> --manifest <path>`. Keep shared required assets in one base profile and phase-specific additions in data manifests.

#### DUP-003 — Context-budget warning logic is implemented four times
- **File path / lines:** `scripts/review_apply_fixes.sh:103-141`; `scripts/review_rb_judge.sh:203-241`; `scripts/review_run_reviewers.sh:44-82`; `scripts/review_consolidate.sh:258-297`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** All four functions construct the same Python path, import `build_context_budget_warn_line_for_file`, and print an optional warning.
- **Recommended fix:** Add `scripts/prompt_budget_helpers.sh::emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>` and source it from all four scripts.

#### DUP-004 — Codex cache restoration is repeated five times
- **File path / lines:** `.github/workflows/plan.yml:72-119`; `.github/workflows/workflow-log-analysis.yml:212-248,695-741,1378-1424,1885-1931`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The same cache key, package validation, copy, symlink, and version-check sequence appears in five jobs.
- **Recommended fix:** Create a `setup-cached-codex(codex_version, cache_key_suffix)` composite-action interface, backed by a single cache restore/persist helper.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block exceeds the 15,000-character threshold
- **File path / lines:** `.github/workflows/implement.yml:853-1153`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` scalar is approximately **16,339 characters** with three `${{ }}` interpolations, leaving approximately **4,661 characters** before GitHub’s 21,000-character limit. Runtime substitutions can reduce that headroom further.
- **Recommended fix:** Apply DUP-002 by moving asset manifests and copy logic into `stage_workflow_support.sh`. Split optional MCP assets and prompt/schema installation into separate steps if extraction cannot land atomically.

No block exceeded 18,000 characters. No workflow exceeded 800 KB; the largest was `review_autofix.yml` at approximately 454 KB.

### Section 5: Cross-Cutting Concerns

No TODO/FIXME/HACK markers were found in the scoped files.

#### DEAD-001 — `render_prompt.py` is staged twice in the same step
- **File path / lines:** `.github/workflows/implement.yml:868-925`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** The required-script loop includes and installs `render_prompt.py`; the later “optional backend” loop installs it again and appends a duplicate `_fetched_scripts` entry. Its missing-file warning is unreachable because the earlier required loop would already exit.
- **Recommended fix:** Keep the file in exactly one list. Based on the current required-path behavior, remove the second loop and its stale optionality comment.

#### DEAD-002 — Several values are assigned but never consumed
- **File path / lines:** `scripts/orchestrate_poll_process.sh:14758-14787,16559-16616`; `scripts/review_issue_ledger.sh:866-917`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `LINKED_PR_NUM`, `IF_BLOCKERS_SOURCE`, and `CURRENT_FLOOR` are populated but have no subsequent reads in the repository. They add state-maintenance cost without affecting behavior.
- **Recommended fix:** Remove them, or make them operational by including the selected linked PR, blocker source, and floor category in the resulting cache/ledger telemetry.

#### SHELL-001 — Ledger range parsing ignores the range endpoint
- **File path / lines:** `scripts/review_issue_ledger.sh:62-107`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `read_anchor_context` parses both `line_start` and `line_end`, but always renders only `line_start ± 2`. A finding covering `120-145` therefore ignores lines 123-145. Shellcheck reports the unused endpoint. This may be intentional start-line anchoring. [NEEDS VERIFICATION]
- **Recommended fix:** Either calculate bounded context from `line_start - 2` through `line_end + 2`, with a maximum-line cap, or remove range parsing and document that only the first line is authoritative.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | SEC-001, SEC-002 |
| Medium | 10 | BUG-001, API-001, API-002, BATCH-001, BATCH-002, BATCH-003, BATCH-004, DUP-001, DUP-002, EXPR-001 |
| Low | 6 | BUG-002, DUP-003, DUP-004, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 7 | Medium |
| API call optimization | 7 | Large |
| Code modularization | 14–15 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 4–6 | Medium |
