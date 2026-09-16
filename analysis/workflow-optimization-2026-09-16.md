## Executive Summary

- **Review/autofix is the systemic failure center:** 61/109 runs failed (56.0%). Fifteen editor-stage failures consumed 18.82 runner-hours—49.7% of all workflow time. **Impact: critical; confidence: high.**
- **Ten verified long failures consumed 163.2M tokens before discovering `model_provider_broker_start: command not found`.** This is 96.8% of the assembled 168.6M-token total. **Impact: critical; confidence: high.**
- **Resolver workspace permissions caused 46 failures and 5.05 runner-hours.** All 12 retained deep dives failed creating `resolver-agent-home`, including runs 35023094828 and 35050164770. **Impact: high; confidence: high.**
- **Poller throughput is healthy but expensive:** 148/148 succeeded, yet consumed 11.81 runner-hours. In runs 35050517310 and 35050942040, two memory writes cost about 91 seconds per run. **Impact: high; confidence: medium—two full-run samples.**
- **Caching is valuable but under-instrumented:** 129.7M cache-read tokens versus 37.8M uncached prompt tokens, a derived 77.4% input-cache share. Aggregate `cache_hit_rate` is null despite run 35049509844 reporting 87.58%. **Impact: medium; confidence: high.**
- **Observability gaps remain:** exact GH API call counts are absent; Serena emitted no queries, fallbacks, or probes; 22/145 OpenRouter calls lack usage. **Impact: medium; confidence: high.**

## Speed Optimizations

1. **Add a support-symbol contract preflight before reviewer fan-out — critical path**
   - **Evidence:** Ten deep dives, including 35001741958, reached the consolidator/editor after 47–123 minutes, then failed because `model_provider_broker_start` was missing.
   - **Root cause:** Preflight checked that support files existed, but not that required shell APIs were defined.
   - **Exact change:** After sourcing support helpers, validate required functions with `declare -F`, including `model_provider_broker_start`, before launching reviewers.
   - **Estimated saving:** Up to 18.82 runner-hours in this window; 10 verified runs alone used 14.48 hours.
   - **Risk:** Low. Fail closed only for missing mandatory interfaces.

2. **Validate resolver-home writability before conflict preparation — critical path**
   - **Evidence:** 46 failures at `Run Codex resolver, validate, stage, commit`; 12/12 retained logs show `resolver-agent-home: Permission denied`.
   - **Root cause:** The resolver selected a path it could not create as the runner user.
   - **Exact change:** Create the agent home under `$RUNNER_TEMP` with mode `0700`; log `uid`, `gid`, parent ownership and mode, then perform a write/delete probe.
   - **Estimated saving:** Approximately 5–7 minutes per affected run; 5.05 hours in-window.
   - **Risk:** Low.

3. **Reuse one AI-memory worktree across poll start/end — critical path**
   - **Evidence:** `Record poll run start` took 45.3–47.3s and `Record poll run end` 43.8–45.9s in runs 35050517310 and 35050942040.
   - **Root cause:** Each event performs an independent Git-backed memory operation.
   - **Exact change:** Initialize one authenticated memory checkout, retain it for the job, and reuse it for the end event.
   - **Estimated saving:** About 45s per poll, or approximately 1.9 hours across 148 runs.
   - **Risk:** Medium; preserve independent fail-open handling.

4. **Lazy-install Semble in the poller — micro-optimization**
   - **Evidence:** Semble installation took 7.5–10s in the two sampled pollers, but neither emitted `SEMBLE_QUERY`.
   - **Exact change:** Install/build only when an actionable judge or review-blocked path requires retrieval.
   - **Estimated saving:** Approximately 18–25 minutes across 148 runs.
   - **Risk:** Low if the query path performs the lazy initialization.

5. **Add a lightweight no-action poll gate**
   - **Evidence:** Both sampled pollers processed the same tracking project, skipped issue 4092 as a fresh `ai:done` push, scanned four PRs, and released zero merge-train entries.
   - **Exact change:** Emit an API-only actionable-count preflight and skip checkout/tool setup when `actionable=0`.
   - **Estimated saving:** Up to 1–3 minutes for true no-op ticks.
   - **Risk:** Medium; retain periodic full reconciliation as a backstop.

## Cost Optimizations

1. **Fail before model calls when support contracts are broken**
   - **Evidence:** The ten verified broker-symbol failures used 163,194,643 tokens: 37,125,587 prompt, 124,992,922 cache-read and 1,079,592 completion tokens.
   - **Exact change:** Use the symbol preflight above and emit `SUPPORT_CONTRACT_CHECK result=failed missing_functions=...`.
   - **Estimated saving:** Nearly all 163.2M tokens from those deterministic failures.
   - **Quality risk:** Low; the editor could not execute successfully anyway.

2. **Suppress repeated deterministic same-head failures**
   - **Evidence:** Sweeps repeatedly dispatched PRs 4088 and 4093 after identical failures; active-run protection only prevents concurrent duplication.
   - **Exact change:** Persist a failure fingerprint keyed by head SHA, workflow SHA and normalized failure class. Cool down only deterministic classes such as `missing_required_function` and `agent_home_not_writable`.
   - **Estimated saving:** Removes repeated 6-minute resolver failures and hour-long broken editor runs.
   - **Quality risk:** Low when automatically invalidated by head/workflow changes.

3. **Circuit-break unhealthy reviewer slots**
   - **Evidence:** `mistralai/mistral-small-2603` failed in all ten structured long-run summaries; the same runs recorded ten stall kills across Kimi/Minimax.
   - **Exact change:** Temporarily skip a slot after repeated infrastructure failures and immediately fail back after the first stall rather than retrying the same model.
   - **Estimated saving:** One or more model attempts per affected review; exact token savings unavailable per model.
   - **Quality risk:** Medium; retain minimum reviewer quorum.

4. **Reduce healthy-run prompt growth**
   - **Evidence:** Eleven `CONTEXT_BUDGET_WARN` events occurred, with review prompts around 182K–191K tokens and ratios of 71–73%.
   - **Exact change:** Log tokens by prompt component, cap raw reviewer bundles, deduplicate repeated metadata, and keep stable instructions before dynamic PR content.
   - **Estimated saving:** Potentially 10–20% of uncached input on large reviews.
   - **Quality risk:** Medium; preserve high-severity findings and changed-file context.

5. **Keep Semble for review retrieval, not unconditional setup**
   - **Evidence:** 12 assembled review queries returned 171,758 bytes with zero runtime fallbacks. Ten deep dives each retrieved 12 chunks in roughly 0.6s.
   - **Assessment:** Retrieval is compact relative to the 644KB editor prompt observed in run 35001741958, but it is not controlling the dominant prompt expansion.
   - **Serena:** No evidence that Serena replaced downstream work; usage and probe telemetry are both zero.

## Reliability Improvements

1. **Resolver permission invariant**
   - **Failure impact:** 46/61 failures.
   - **Fix:** Writable-home preflight plus structured ownership diagnostics.
   - **Expected impact:** Could remove approximately 75% of observed failures.
   - **Rollback/fail-open:** Fall back to a newly created `$RUNNER_TEMP` path; never continue with a known-unwritable path.

2. **Support API compatibility invariant**
   - **Failure impact:** Ten verified and five additional similarly positioned editor failures.
   - **Fix:** Validate required functions and CLI options before reviewer execution.
   - **Expected impact:** Prevents deterministic late failures and token waste.
   - **Rollback:** Pin to the immutable workflow support copy when branch-owned helpers fail validation.

3. **Make run summaries reflect the primary failure**
   - **Evidence:** All 12 sampled resolver failures emitted `finalize_reason=clean_review_no_commit`, despite the resolver step exiting nonzero.
   - **Fix:** Add `primary_failure_phase`, `primary_failure_class`, `primary_exit_code`, and `job_conclusion`; reject a “clean” summary if any mandatory step failed.
   - **Expected impact:** Eliminates misleading dashboards without changing workflow behavior.

4. **Treat repeated memory fail-open as a broken rollout**
   - **Evidence:** Each sampled resolver failure produced failed force-tick get/put operations and a failed run-event record—60 fail-open events across 12 runs. Two recent folders duplicate combined and split logs.
   - **Fix:** Restore authenticated Git operations; add a stable event ID so collectors can deduplicate.
   - **Rollback:** Continue the workflow, but emit one aggregated warning with operation counts and reasons.

5. **Reduce diagnostic-warning storms**
   - **Evidence:** Ten long runs emitted 724 `ledger_emit_substate ... exceeded 120.0s` warnings.
   - **Fix:** Emit one per-slot summary: attempts, timeout count, total blocked milliseconds and final state.
   - **Expected impact:** Cleaner diagnostics and potentially lower latency if calls are currently synchronous.
   - **Fail-open:** Substate recording remains non-blocking.

Additional signals:

- `BREAK_GLASS`: 0—no evidence of rubric/policy escape pressure.
- `CONTEXT_BUDGET_WARN`: 11—all in review/autofix, indicating prompt-size risk.
- Semble fallbacks: eight contract-test fallbacks, zero runtime fallbacks; this is synthetic test coverage, not a failed rollout.
- Copilot review run 35052006787 used a deterministic finalizer fallback successfully; one fallback across four successful runs appears healthy but should remain counted.

## AI Memory Health

- **Retrieval:** 22/22 sampled review runs selected records: 100% hit rate.
- **Budget:** Average estimated retrieval was 1,386.5 of 1,400 tokens—99.0% utilization.
- **Keyword method:** `llm` in 22/22; no `plain` or `none`.
- **Misses/disabled:** No zero-record retrieves and no `enabled:false` entries.
- **Fail-open:** Concentrated in all 12 resolver failures, showing that retrieval worked but later force-tick/run-event writes did not.
- **Push retries:** Runs 34942460517 and 35001741958 required two attempts; otherwise retained successful writes used one.
- **Coverage gap:** No retained `promote`, `compact`, `finalize-task`, or processed-command claim/complete telemetry.

Recommendation: add `duration_ms`, `clone_ms`, `commit_ms`, `push_ms`, `reason`, and `event_id` to every memory event. Also log retrieval score distribution; always returning 30–31 records near the full budget may indicate weak selectivity.

## GH API Call Audit

Exact executed-call counts and rate-limit headers were not collected, so this audit is bounded.

- **Poller hotspot:** `scripts/orchestrate_poll_process.sh` contains approximately 100 static `gh api` call sites. Sampled `Process each tracking issue` steps took 69.6s and 74.5s while handling one project and scanning four PRs.
- **Review gate redundancy:** The gate contains two possible `/pulls/{PR}/files` fetches. Cache a tri-state result (`success`, `empty`, `failed`) and reuse it. Potential reduction: up to one call per eligible review, or 109 calls/window.
- **Good batching:** Sweep run 35052418479 processed five PR candidates in 14s using paginated PR retrieval and active-run caching; it dispatched one and skipped four active runs.
- **Good reuse:** Review failure paths explicitly reused early `LINKED_ISSUES_JSON`, avoiding duplicate GraphQL calls.
- **Copilot MCP:** Run 35051572141 reported two `github-mcp-server` invocations and zero Playwright invocations.
- **Rate limits:** No rate-limit event appeared in sampled evidence, but absence of call telemetry prevents a reliable risk assessment.

Add one wrapper-level event:

`GH_API_CALL method=GET endpoint=pulls/{id}/files attempts=1 status=200 duration_ms=... remaining=... cache_hit=false`

Then emit a per-job `GH_API_SUMMARY` grouped by normalized endpoint.

## Prompt Cache & Memory System

- Assembled cache reads: **129,672,788 tokens**.
- Uncached prompt tokens: **37,789,301**.
- Derived cache-read share: **77.4%**.
- Explicit `cache_hit_rate`: **87.5792%** on successful run 35049509844; aggregate remains null.
- Reported cache-write tokens: **0**, which may mean unsupported reporting rather than no cache creation.
- Usage unavailable: **22/145 calls (15.2%)**.

Run 35001741958 showed a 248,989-byte static section and a 395,257-byte dynamic editor body. **Inference:** static pre-assembly is helping cache reuse, but large dynamic reviewer/diff sections still dominate.

Recommended changes:

1. Aggregate `cache_hit_rate` with `coverage_count`, weighted mean and p50 instead of returning null.
2. Emit prompt-prefix fingerprints and component token counts.
3. Keep stable instructions and tool schemas first; append run IDs, timestamps and mutable diagnostics last.
4. Compact memory retrieval below the current 99% budget saturation when the review prompt is already above the context warning threshold.

## Orchestrator Health

- **Healthy outcomes:** 148/148 poll runs succeeded; no cancellations.
- **High duty cycle:** Polling consumed 42,508 seconds, or 31.2% of all runtime.
- **Repeated no-op state:** Runs 35050517310 and 35050942040 processed project 3965, skipped issue 4092 due to a fresh push, scanned four PRs and released zero merge-train entries.
- **State-source weakness:** Both used `STALL_FRESH_PUSH_FALLBACK ... source=branch_name`; authoritative push timestamps were unavailable.
- **Cooldown weakness:** Review failures reported “dispatching without persisted cooldown claim” when memory writes failed, increasing duplicate-dispatch risk.
- **Queueing:** Run 35049207973 waited approximately 161s for a hosted runner.
- **Stage fan-out noise:** Clarify, plan, implement and clarify-respond each generated 64 skipped runs, though their combined runtime was only about 11 minutes.

Track per tick:

- actionable issues and skipped issues by reason;
- state transitions versus unchanged issues;
- forced-tick claim success;
- memory and GH API milliseconds;
- judge calls;
- merge-train examined/released;
- queue wait separately from execution.

## Pipeline Flow Bottlenecks

| Flow stage | Evidence | Bottleneck type | Priority |
|---|---|---|---|
| Clarify → plan → implement | 256 skipped runs across four families | Dispatch noise | Low |
| Review fan-out | Ten stall kills, unhealthy reviewer slot, 724 substate timeouts | Compute/retry | High |
| Consolidate → edit | Missing broker API after expensive review execution | Deterministic failure | Critical |
| Conflict resolution | 46 permission failures, p50 394.5s | Environment setup | Critical |
| Orchestration | 148 polls, p50 273s; ~91s sampled memory overhead | State persistence | High |
| Validation | Run 35048623729 took 1,057s; 3 red and one self-test failure while workflow succeeded | Compute/masked partial failure | Medium |
| CI | Run 35049537373 took 1,677s | Compute; detailed step evidence unavailable | Medium |
| Copilot review | 4/4 succeeded, p50 242s; firewall warnings in sampled runs | Model/network | Medium |

End-to-end order: support preflight → resolver permissions → deterministic rerun suppression → poll memory reuse → reviewer circuit breaking.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** Review/autofix used 65.4% of runtime; poller used 31.2%.
- **Top failures:** 46 resolver permission failures and 15 editor-stage failures.
- **Highest cost:** 163.2M tokens spent by ten deterministic broker-contract failures.
- **Top actions:**
  1. Add support-symbol and resolver-writability preflights.
  2. Add same-head deterministic failure fingerprints and cooldowns.
  3. Reuse poll memory state and emit structured GH API/tick summaries.

## Metrics Appendix

### Window summary

| Metric | Value |
|---|---:|
| Total runs | 524 |
| Success | 207 (39.5%) |
| Failure | 61 (11.6%) |
| Other/skipped | 256 (48.9%) |
| Cancelled | 0 |
| Duration p50 / p95 | 10s / 440s |
| Average duration | 260.2s |
| Wall-clock samples | 117 |
| Wall-clock p50 / p99 | 16,000ms / 6,478,040ms |

### Major workflow families

| Family | Runs | Success | Failure | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| review_autofix | 109 | 48 | 61 | 0 | 354s | 4,934s |
| orchestrate_poll | 148 | 148 | 0 | 0 | 273s | 387s |
| clarify | 64 | 0 | 0 | 64 | 1s | 10s |
| plan | 64 | 0 | 0 | 64 | 1s | 10s |
| implement | 64 | 0 | 0 | 64 | 1s | 9s |
| orchestrate_clarify_respond | 64 | 0 | 0 | 64 | 1s | 9s |
| copilot_pull_request_reviewer | 4 | 4 | 0 | 0 | 242s | 305s |
| ci | 1 | 1 | 0 | 0 | 1,677s | 1,677s |
| validation_refresh | 1 | 1 | 0 | 0 | 1,057s | 1,057s |

### Cost and cache

| Metric | Value |
|---|---:|
| OpenRouter calls | 145 |
| Usage available / unavailable | 123 / 22 |
| Prompt tokens | 37,789,301 |
| Completion tokens | 1,151,582 |
| Cache-read tokens | 129,672,788 |
| Cache-write tokens reported | 0 |
| Total tokens | 168,608,351 |
| Derived cache-read share | 77.4% |
| Aggregate `cache_hit_rate` | unavailable |
| Explicit run 35049509844 hit rate | 87.5792% |
| Break-glass count | 0 |
| Context-budget warnings | 11 |

### Failure clusters

| Failure point | Runs | Runtime | Verified root cause |
|---|---:|---:|---|
| Apply fixes with editor model | 15 | 18.82h | Missing broker function in 10/10 retained deep dives |
| Run Codex resolver | 46 | 5.05h | Resolver-home permission denied in 12/12 retained deep dives |

### MCP telemetry

| System / target | Queries | Bytes | Runtime fallbacks | Contract fallbacks | Probe OK / failed / skipped |
|---|---:|---:|---:|---:|---:|
| Semble `reviewer-context` | 12 | 171,758 | 0 | 0 | n/a |
| Semble contract tests | 0 | 0 | 0 | 8 | n/a |
| Serena `(no target emitted)` | 0 | 0 | 0 | 0 | 0 / 0 / 0 |

No Serena per-tool breakdown or standardized “other MCP server” telemetry was emitted.

## Deep Audit — Workflows & Scripts (2026-09-16)

### Section 1: Bug & Correctness Sweep

Coverage: 46 workflows, 78 shell scripts, and 56 Python scripts. YAML, Bash, and Python syntax checks passed. No `pull_request_target`, direct secret logging, or actionable command-injection path was found. Actionlint was unavailable.

#### BUG-001 — Cancelled required checks are treated as successful

- **File:** `scripts/pr_checks_lib.sh:206-253`
- **Severity:** High
- **Category:** `bug`
- **Description:** Both check-filter branches explicitly accept `conclusion == "cancelled"`. This conflicts with `scripts/orchestrate_poll_process.sh:21443-21445`, whose merge contract says cancelled checks abort the gate. **Inference:** on an unprotected branch, a synchronous merge governed only by `ORCH_FINAL_MERGE_REQUIRED_CHECKS` could proceed after a required check was cancelled. Existing tests do not cover cancellation. [NEEDS VERIFICATION]
- **Recommended fix:** Treat only `success`, `neutral`, and `skipped` as acceptable. Update `_summarize_final_merge_blockers` at `scripts/orchestrate_poll_process.sh:708-747` and add required/non-required cancellation cases to the shared-gate tests.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Final PR state is fetched twice from one endpoint

- **File:** `scripts/orchestrate_poll_process.sh:9724-9735`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** The fallback path calls `pulls/${final_pr}` once for `.state` and again for `.merged_at`.
- **Call count:** Current: 2 calls. Proposed: 1 call.
- **Recommended fix:** Fetch the PR object once and parse both fields with `_jq_field`, matching the existing `final_pr_json_snapshot` branch immediately above.

#### API-002 — Reissue paths fetch issue title and body separately

- **File:** `scripts/orchestrate_poll_process.sh:12579-12581`, `15001-15009`, `19798-19800`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Three reissue implementations issue two identical issue GETs, changing only the `--jq` selector. The implementation-failed sweep repeats this inside a multi-issue loop.
- **Call count:** Direct paths: 2 → 1 per issue. Sweep path: 2N → `ceil(N/25)`.
- **Recommended fix:** Fetch `{title,body}` once in direct paths. Add `title` and `body` to `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:13288-13406`) for sweep-wide reuse.

#### API-003 — Release polling uses a second, weaker retry implementation

- **File:** `scripts/comprehensive_test_and_release_gh_api.sh:3-47`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** `gh_api_safe` retries only errors containing “rate limit”, uses fixed 30/60/120-second sleeps, and ignores `Retry-After`, reset headers, 5xx responses, and transient transport failures. This duplicates the stronger `gh_retry` implementation at `scripts/gh_helpers.sh:430-494`.
- **Call count:** Logical calls remain 1 → 1; retry attempts change from at most 4 rate-limit-only attempts to the shared configured retry policy.
- **Recommended fix:** Source `gh_helpers.sh` from `.github/workflows/comprehensive-test-and-release.yml:56,289` and `scripts/dispatch_and_watch_workflow_run.sh:6-7`, then implement output capture through `gh_retry_to_file`.

#### API-004 — Safety-latch handler bypasses reset-aware retries

- **File:** `scripts/implement_handle_guard_block.sh:13-52`, `98-155`, `217-283`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** The staged-support, scope, and destructive-block handlers use raw `gh label`, `gh issue edit`, `gh issue view`, and `gh issue comment` calls. A transient failure can leave the safety label unapplied and merely report an unknown latch state.
- **Call count:** Current: up to 5 calls for staged-support and 6 for scope/destructive branches. Proposed: unchanged logical count, but every call uses shared reset-aware retries.
- **Recommended fix:** Preserve `gh_helpers.sh` beside the late handler in `.github/workflows/implement.yml:967-970`, source it, and use `gh_retry`, `_safe_gh_jq`, and `ensure_label_exists`.

#### BATCH-001 — Standalone PR sweeps perform per-PR reads

- **File:** `scripts/orchestrate_poll_process.sh:21305-21349`, `21463-21475`, `21500-21743`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** The conflict and no-op sweeps reuse the open-PR list but still fetch PR metadata and comments for every PR, then commits and three additional snapshots for candidates. [NEEDS VERIFICATION]
- **Call count:** For N PRs, C no-op candidates, and T threshold candidates: current `1 + 2N + C + 3T`; proposed `1 + ceil(N/25) + 3T`.
- **Recommended fix:** Extend the aliased batching pattern from `_fetch_candidate_issue_details_graphql` with a PR-oriented helper returning state, refs, mergeability, labels, recent comments, head SHA, and latest productive commit. Retain threshold-path refreshes for safety.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Prompt-path resolution is copied between two scripts

- **File:** `scripts/render_prompt.sh:12-41,95-159`; `scripts/assemble_prompt.sh:12-93`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `resolve_prompt_file`, `resolve_render_prompt_py`, and `resolve_assembly_source_path` have identical implementations.
- **Recommended fix:** Move them to `scripts/prompt_path_helpers.sh` with signatures `resolve_prompt_file <path>`, `resolve_render_prompt_py`, and `resolve_assembly_source_path <path>`. Both callers should source the module after defining `SCRIPT_DIR`.

#### DUP-002 — Support bootstrap logic remains duplicated across workflows

- **File:** `.github/workflows/clarify.yml:217-353`; `.github/workflows/plan.yml:280-428`; `.github/workflows/orchestrate.yml:340-469`; `.github/workflows/orchestrate_clarify_respond.yml:279-414`; `.github/workflows/orchestrate_poll.yml:371-585`; `.github/workflows/implement.yml:918-1274`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** These workflows independently implement ref fallback, script/schema/prompt installation, model-catalog staging, and generated `.gitignore` handling. Review and validate already delegate substantial portions to `scripts/stage_workflow_support.sh`.
- **Recommended fix:** Generalize that script around `workflow_support_stage_from_manifest <target> <manifest-path> <destination-mode>`. Each workflow should supply a small manifest and replace its inline bootstrap body with one invocation.

#### DUP-003 — Repository batch utilities are exactly duplicated

- **File:** `scripts/validation_refresh_runner.py:86-163,701-723`; `scripts/audit_consumer_drift.py:41-113,142-164`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `CommandFailure`, `CommandExecutor.run`, and `load_target_repositories` are effectively identical.
- **Recommended fix:** Create `scripts/repo_batch_helpers.py` exporting `CommandExecutor.run(...)`, `CommandFailure`, and `load_target_repositories(path)`. Update both callers to import them.

#### DUP-004 — ISO-8601 parsing has four identical implementations

- **File:** `scripts/workflow_retro.py:50-62`; `scripts/collect_workflow_logs.py:96-108`; `scripts/cost_audit.py:283-295`; `scripts/analyze_workflow_logs.py:40-52`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** All four functions normalize `Z`, call `datetime.fromisoformat`, and coerce naïve values to UTC identically.
- **Recommended fix:** Add `scripts/time_helpers.py::parse_iso8601_utc(value)` and import it from all four callers.

#### DUP-005 — Internal implement and plan wrappers are 80% structurally identical

- **File:** `.github/workflows/internal-implement.yml:1-32`; `.github/workflows/internal-plan.yml:1-34`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** After phase-token normalization, the workflows have 0.800 sequence similarity. Differences are limited to permissions, command markers, bot markers, and reusable target.
- **Recommended fix:** Keep separate generated workflows, but render both from one generator function such as `render_issue_comment_phase_wrapper(phase, command, permissions, bot_markers)`. Preserve existing predicate-parity tests.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Support-staging expression is within 813 characters of the limit

- **File:** `.github/workflows/implement.yml:920-1274`
- **Severity:** High
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` body is approximately 20,187 characters with three `${{ }}` interpolations. Remaining headroom is only 813 characters before the 21,000-character hard limit.
- **Recommended fix:** Extract the block into `scripts/stage_workflow_support.sh` using the manifest-based interface proposed in DUP-002.

#### EXPR-002 — Preflight guard expression exceeds the medium-risk threshold

- **File:** `.github/workflows/implement.yml:3108-3413`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The interpolated `run:` body is approximately 17,313 characters with one `${{ }}` interpolation. Remaining headroom is 3,687 characters.
- **Recommended fix:** Extract the destructive-deletion and files-touched preflight into `scripts/implement_preflight_guards.sh`, preserving current `$GITHUB_OUTPUT` fields and exit codes.

No workflow exceeds 800 KB. The largest is `.github/workflows/review_autofix.yml` at approximately 493,679 characters, leaving 554,897 characters below the 1 MiB limit.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — No-op recovery duplicates and contradicts the shared check gate

- **File:** `scripts/orchestrate_poll_process.sh:21734-21782`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** The block says it validates “required checks” but independently rejects every failed check, including advisory checks. This contradicts the single-source contract documented at `agents.md:1026` and implemented by `pr_checks_lib.sh`.
- **Recommended fix:** Replace the duplicated check-run query and jq filters with `_pr_checks_completed "${N_PR}" "${N_HEAD_SHA}" "${N_BASE}"`. Add a contract test requiring the three-argument call.

#### DEAD-001 — Label-repair evidence engine has no runtime caller

- **File:** `scripts/orchestrate_lib.py:2017-2089`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `resolve_label_repair_evidence` is not invoked by any scoped workflow or production script. `agents.md:932-939` explicitly states the richer contradiction-evidence path is reserved and not wired into reconciliation.
- **Recommended fix:** Either wire `resolve_label_repair_evidence(labels, comments, linked_pr)` into `reconcile_managed_issue_labels`, or move the reserved implementation out of the production library until rollout.

#### DEAD-002 — Final-merge reset helper is never called

- **File:** `scripts/orchestrate_poll_process.sh:751-760`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `_clear_final_merge_ineligibility_state` claims to be called by the merge-success path but has no caller. Equivalent resets are duplicated inline at `4447-4452` and `10754-10756`.
- **Recommended fix:** Replace the inline resets with calls to this helper and invoke it from successful final-merge paths, or delete the stale helper and comment.

#### SHELL-001 — ShellCheck-confirmed assignments are never consumed

- **File:** `scripts/review_issue_ledger.sh:67-104,866-917`; `scripts/review_run_reviewers.sh:754-761,3535-3549,3771-3775,4045-4048,4124-4129`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `line_end`, `CURRENT_FLOOR`, `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`, and `REVIEWER_ATTEMPT_WD_REASON` are assigned but never read.
- **Recommended fix:** Remove the assignments, or consume them in the intended range, filtering, health-state, or watchdog telemetry logic. Keep ShellCheck’s SC2034 warning enabled to prevent recurrence.

No TODO, FIXME, HACK, or XXX markers were found in scoped files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 7 | API-002, API-003, API-004, BATCH-001, DUP-002, EXPR-002, CONSIST-001 |
| Low | 8 | API-001, DUP-001, DUP-003, DUP-004, DUP-005, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2–3 | Medium |
| API call optimization | 4–6 | Large |
| Code modularization | 10–14 | Large |
| Expression size reduction | 2–3 | Medium |
| Medium/Low fixes | 5–8 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-09-16)

### Safety Tag Legend

`SAFE_TO_MERGE` is directly implementable; `NEEDS_VERIFICATION` requires the stated checks first; `RISKY_SKIP` must not be auto-implemented because pagination, races, retries, or poller safety contracts are involved.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — Reuse the full clarify comment snapshot for bounded prompt context

- **Safety tag:** `RISKY_SKIP`
- **File / calls:** `.github/workflows/clarify.yml:468` and `.github/workflows/clarify.yml:470-473`
- **Current call count:** 2 logical reads when semantic caching is enabled.
- **Proposed call count:** 1 steady-state read; retain the bounded read as fallback if pagination fails.
- **Endpoint:** `GET /repos/{repo}/issues/{issue}/comments`
- **Evidence:**
  ```bash
  gh_retry gh api ".../comments?...&per_page=50" > "${ISSUE_COMMENTS_FILE}"
  gh_retry gh api --paginate --slurp ".../comments?...&per_page=100"
  ```
  The paginated response contains the same first 50 comments needed by `ISSUE_COMMENTS_FILE`.
- **Proposed fix:** In `Fetch issue comments`, fetch the paginated JSON once, derive `ISSUE_COMMENTS_FILE` from `(add // [])[0:50]`, and derive `THREAD_HISTORY_FILE` from the full array. Preserve the current bounded call as the cache-failure fallback.
- **Safety rationale:** Pagination and differing fail-hard/fail-open behavior trigger mandatory `RISKY_SKIP` treatment.
- **Downstream signal:** Do not auto-implement; manually verify first-50 ordering, multi-page behavior, and identical outcomes when either the full or bounded request fails.

#### MERGE-002 — Preserve marker body from the merge-train comments listing

- **Safety tag:** `RISKY_SKIP`
- **File / calls:** `scripts/review_merge_train.sh:257-261`, `scripts/review_merge_train.sh:275-287`, `scripts/review_merge_train.sh:354-387`
- **Current call count:** 2 GETs when an existing marker is upserted, plus the unchanged conditional PATCH.
- **Proposed call count:** 1 GET, plus the same conditional PATCH.
- **Endpoints:** `GET /repos/{repo}/issues/{pr}/comments`; `GET /repos/{repo}/issues/comments/{id}`
- **Evidence:**
  ```bash
  --jq ".[] | select(.body | startswith(\"${marker}\")) | .id"
  existing_body="$(gh_retry gh api ".../issues/comments/${existing_id}" --jq '.body' ...)"
  ```
  The collection response already contains both `.id` and `.body`.
- **Proposed fix:** Replace `_mt_find_marker_comment_id` with a helper returning the latest `{id,body}` object. Extend `_mt_upsert_comment` to accept that snapshot and avoid the detail GET.
- **Safety rationale:** The listing is paginated and the merge train explicitly protects concurrent release/bypass races.
- **Downstream signal:** Do not auto-implement; manually validate latest-comment selection, one-shot bypass behavior, concurrent label removal, and identical-body write suppression.

#### MERGE-003 — Batch archival-lint tracking-issue lookups

- **Safety tag:** `NEEDS_VERIFICATION`
- **File / calls:** `scripts/lint_plan_archival_completeness.py:85-106` and `scripts/lint_plan_archival_completeness.py:189-200`
- **Current call count:** `U` logical `gh issue view` calls for `U` unique referenced issues.
- **Proposed call count:** `ceil(U / 25)` GraphQL calls in the steady state.
- **Endpoints:** Current GraphQL-backed `gh issue view`; proposed `POST /graphql` with aliased `repository.issue(number)` fields `labels` and `body`.
- **Evidence:**
  ```python
  for issue_num in referenced_issues:
      issue = issue_fetcher(repo, issue_num)
  ```
- **Proposed fix:** Add `_fetch_issues_via_graphql(repo, issue_numbers)` using the `_fetch_candidate_issue_details_graphql` pattern at `scripts/orchestrate_poll_process.sh:13288-13406`. Keep injected `issue_fetcher` behavior unchanged and fall back per item after total batch failure.
- **Safety rationale:** Calls are mutation-free and co-located, but partial GraphQL errors could change per-issue lookup-error semantics.
- **Downstream signal:** Verify mixed alias success/failure, total batch failure, `fail_open_on_lookup_error` behavior, input ordering, and injected-fetcher tests before implementation.

#### MERGE-004 — Batch auto-close-lint label lookups by repository

- **Safety tag:** `NEEDS_VERIFICATION`
- **File / calls:** `scripts/lint_pr_body_auto_close.py:128-159` and `scripts/lint_pr_body_auto_close.py:240-266`
- **Current call count:** `U` logical calls on successful unique `(repository, issue)` lookups; failed keys may be retried on later references.
- **Proposed call count:** `sum(ceil(U_repo / 25))` steady-state GraphQL calls.
- **Endpoints:** Current GraphQL-backed `gh issue view`; proposed `POST /graphql` with aliased `repository.issue(number).labels`.
- **Evidence:**
  ```python
  if cache_key in issue_label_cache:
      labels = issue_label_cache[cache_key]
  else:
      labels = label_lookup(lookup_repo, issue)
  ```
- **Proposed fix:** Group unresolved keys by repository and batch them using the `_fetch_candidate_issue_details_graphql` alias pattern. Retain per-item fallback for failed aliases and custom `label_lookup` injection.
- **Safety rationale:** Cross-repository grouping and the current retry-on-later-reference behavior prevent proving identical error semantics statically.
- **Downstream signal:** Verify cross-repository grouping, partial alias errors, repeated failed references, fail-open/fail-closed exits, and existing injected-lookup call-count tests.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — Reuse `headRefOid` from the feature-sweep PR inventory

- **Safety tag:** `RISKY_SKIP`
- **File / calls:** `scripts/orchestrate_poll_process.sh:18187-18197` and `scripts/orchestrate_poll_process.sh:18208-18218`
- **Current call count:** For `B` behind PRs, `1+B` reads and `B` update writes.
- **Proposed call count:** 1 read and `B` unchanged update writes.
- **Endpoints:** GraphQL PR listing via `gh pr list`; `GET /repos/{repo}/pulls/{pr}`; unchanged `PUT /repos/{repo}/pulls/{pr}/update-branch`
- **Evidence:** The inventory omits `headRefOid`, forcing one REST lookup per behind PR. The same file already requests `headRefOid` from `gh pr list` at `scripts/orchestrate_poll_process.sh:816-821`.
- **Proposed fix:** Add `headRefOid` to the feature-sweep `--json` fields and parse `_fs_head_sha` from `_fs_pr`.
- **Safety rationale:** This is inside `orchestrate_poll_process.sh`, and the fresh per-PR read currently narrows a concurrent-push race.
- **Downstream signal:** Do not auto-implement; manually test concurrent head updates, missing/deleted head refs, expected-SHA rejection, and next-tick recovery.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: RISKY_SKIP — Valid duplicate, but its poller location requires manual race and error-semantics review.
- API-002: RISKY_SKIP — Valid overlap, but poller execution and paginated sweep batching prohibit automatic implementation.
- API-003: RISKY_SKIP — The replacement affects an explicit retry/backoff loop.
- API-004: NEEDS_VERIFICATION — Retry centralization is sound, but safety-latch write and verification timing must remain unchanged.
- BATCH-001: RISKY_SKIP — Poller pagination, threshold refreshes, and upstream-race defenses require manual review.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | MERGE-003, MERGE-004, API-004 |
| RISKY_SKIP | 7 | MERGE-001, MERGE-002, REUSE-001, API-001, API-002, API-003, BATCH-001 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
