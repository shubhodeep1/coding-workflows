## Executive Summary

- **Review/autofix is the dominant bottleneck:** p95 **8,200s**; eight deep runs consumed a median **6,982s** of active budget. **Impact:** highest. **Confidence:** high.
- **Self-trigger deduplication is disabled:** `AUTOFIX_SKIP_SELF_TRIGGERED` was empty in sampled runs; seven slow runs dispatched continuations, while review had **22 cancellations** and **18.1 hours** of reported cancelled-run lifetime. Enable the existing guarded skip. **Estimated impact:** up to **40–50% fewer verification-review calls per autofix cycle**. **Confidence:** medium-high.
- **One reviewer slot is systemically unhealthy:** `mistralai/mistral-small-2603` succeeded in only **1/8** deep summaries, requiring **13 attempts** and eight retryable failures. **Impact:** fewer stalls and calls by enabling the existing circuit breaker. **Confidence:** high.
- **Prompt pressure is severe despite strong cache reuse:** **21** context warnings; sampled ratios reached **1.0**. Cache-read tokens were **84.3%** of total tokens, but `cache_hit_rate` was unavailable. **Impact:** lower latency and overflow risk through dynamic context caps. **Confidence:** high.
- **Poller overhead is largely telemetry/state persistence:** run `35477466036` spent about **94s** recording start/end memory events and **92s** processing issues within a 297s run. **Impact:** approximately **47s saved per poll** by batching memory writes. **Confidence:** high.
- **Failure diagnostics are incomplete:** failed review run `35452034565` lasted **7,688s**, but its archive contains only the gate log—not the failing `review / codex-agent` log. **Impact:** materially faster incident triage. **Confidence:** high.

## Speed Optimizations

1. **Critical path — suppress bot-authored synchronize reruns**
   - **Evidence:** seven deep runs emitted `AUTOFIX_CONTINUATION_DISPATCH_ISSUED`; `AUTOFIX_SKIP_SELF_TRIGGERED` was unset. Review/autofix recorded 22 cancellations.
   - **Root cause:** an autofix push can trigger both `pull_request:synchronize` and an explicit continuation. Job concurrency prevents simultaneous compute but leaves pending/replaced runs.
   - **Exact change:** set repository variable `AUTOFIX_SKIP_SELF_TRIGGERED=true`. Preserve workflow-dispatch, merge-resolve, force-review, and sweep safety paths.
   - **Savings:** potentially one full verification panel per productive autofix cycle; **40–50%** on affected cycles.
   - **Risk:** low; this is an existing narrowly scoped guard.

2. **Critical path — quarantine degraded reviewer slots**
   - **Evidence:** Mistral succeeded in 1/8 summaries versus 8/8 for Qwen and Grok; slow runs recorded 12 retryable reviewer failures and three stall kills.
   - **Root cause:** `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`; repeated provider failures remain eligible.
   - **Exact change:** enable the existing circuit breaker with threshold `3` and TTL `1800`; add a single half-open probe after TTL.
   - **Savings:** approximately **5–10% overall review calls**, potentially up to a **600s stall window** per affected pass.
   - **Risk:** low-medium; five other reviewers remain.

3. **Critical path — abort stale work earlier**
   - **Evidence:** run `35451202045` spent **4,343s** of active budget, then emitted `AUTOFIX_PRE_PUSH_STALE_BASE` and pushed nothing.
   - **Root cause:** the branch advanced after the pre-editor freshness check.
   - **Exact change:** recheck head SHA after consolidation and before every editor retry, not only before editor and push.
   - **Savings:** approximately **20–40 minutes** when a concurrent push occurs.
   - **Risk:** low; fail softly and let the new-head run proceed.

4. **Critical path — batch poll memory persistence**
   - **Evidence:** run `35477466036` spent 47.1s recording poll start and 46.6s recording poll completion.
   - **Exact change:** log poll start locally, then persist start and completion in one final git transaction carrying both timestamps.
   - **Savings:** about **47s/run**, roughly **16%** of the sampled poll duration.
   - **Risk:** low; a hard runner crash may leave only the stdout start marker.

5. **Fail-fast — move structural CI contracts forward**
   - **Evidence:** runs `35433648629` and `35436921310` failed after **490s** and **855s**, respectively, on deterministic workflow contracts.
   - **Exact change:** run untrusted-input and `gh_retry` adjacency contracts before broad test groups.
   - **Savings:** **8–14 minutes** on equivalent failures.
   - **Risk:** negligible.

## Cost Optimizations

1. **Enable self-trigger skipping first.** It uses the safest existing boundary and can remove an entire redundant panel/editor cycle. Dollar savings cannot be calculated because model pricing was not collected.

2. **Circuit-break degraded reviewers.**
   - Mistral represented **13/57 final-panel attempts** but succeeded once.
   - Prefer temporary health-based exclusion over permanently deleting model diversity.
   - Add `REVIEWER_HEALTH_V1 slot=... successes=... failures=... circuit_state=...`.

3. **Enforce dynamic context budgets.**
   - **Evidence:** 21 warnings; deep samples reached 168k–236k prompt tokens and ratios of 0.755–1.0.
   - **Change:** cap dynamic context at 70% of each selected model’s context window; route overflow through Semble and record dropped/injected bytes.
   - **Estimated savings:** **15–25% of uncached prompt tokens** on warning-producing calls.
   - **Quality risk:** medium; retain filenames and summaries for omitted material.

4. **Do not enable review tiering blindly.**
   - Logs repeatedly showed `REVIEW_TIER ... enabled=false` and `REVIEWER_RISK_TIER ... loc=0 files=0`.
   - First add shadow-mode logging of actual diff source, LOC, file count, forced-full matches, and “would select” models. Enabling tiering with zero metrics could under-review large changes.

5. **Semble is not a primary cost driver.**
   - 30 successful queries returned **313,029 bytes**, averaging **10.4KB/query**.
   - Only two operational fallbacks occurred, both `target=overflow reason=budget-exhausted`; the other 16 fallbacks were contract-test emissions.
   - **Inference:** chunked responses appear bounded, but savings cannot be proven until `source_bytes`, `injected_bytes`, and `bytes_avoided` are logged.

6. **Serena provided no replacement value in this window:** zero queries, probes, fallbacks, or tool calls; sampled runs showed it disabled. No rollout failure is indicated.

## Reliability Improvements

1. **Fix failed-job log coverage**
   - **Failure evidence:** run `35452034565` failed in `review / codex-agent`, but only gate logs were archived.
   - **Category:** observability/collector.
   - **Fix:** emit `JOB_LOG_COVERAGE_V1 expected_jobs=... downloaded_jobs=... missing_jobs=...`; mark archive status partial when the failing job is absent.
   - **Expected impact:** high triage improvement.
   - **Fail-open:** retain available logs, but never report `log_download_status=success` for incomplete failure coverage.

2. **Prevent repeated provider degradation**
   - **Evidence:** Mistral failed in 7/8 summaries; Minimax caused two stall kills; Moonshot caused one.
   - **Fix:** enable circuit breaker and log failure class, elapsed time, provider response ID, and selected next action.
   - **Impact:** fewer partial panels and stall recoveries.
   - **Rollback:** disable the circuit breaker variable.

3. **Strengthen CI contract diagnostics**
   - Run `35433648629`: unsafe interpolation in `test-and-mark-stable.yml`.
   - Run `35436921310`: missing immediate `gh_retry` fallback in `.github/workflows/orchestrate.yml`.
   - Add `CI_TEST_FAILURE_V1 suite=... test=... contract_id=... workflow=... step=...`; the collector’s step label was broader than the actual failing test.
   - Keep these hard-failing; no fail-open is appropriate.

4. **Improve memory fail-open detail**
   - Six of 48 deep memory events failed open: `record-candidate` and `record-run-event` in runs `35435550041`, `35456048680`, and `35464726455`.
   - Current payload only reports `source=shell`.
   - Add `failure_stage`, `error_class`, `push_attempts`, and redacted stderr category; preserve fail-open behavior.

5. **Telemetry-pressure signals**
   - `BREAK_GLASS`: **0**, so no observed policy/rubric bypass pressure.
   - `CONTEXT_BUDGET_WARN`: **21**, indicating real prompt-size risk.
   - Semble’s two runtime fallbacks are rare and healthy fail-open behavior; 16 contract fallbacks must remain separately classified.
   - Serena availability is unknown because no probes ran, but it was disabled rather than silently failing.

## AI Memory Health

- **Retrieval hit rate:** 8/8 deep slow-run retrievals selected records — **100%**.
- **Average retrieval:** 30.75 records and **1,391 estimated tokens / 1,400 budget**—99.4% budget utilization.
- **Keyword method:** `llm` 8, `plain` 0, `none` 0.
- **Failures:** six fail-open writes across three runs; retrieval itself remained successful.
- **Push pressure:** five telemetry operations needed more than one push attempt; maximum was two.
- **Missing evidence:** no sampled `promote`, `compact`, `finalize-task`, or processed-command telemetry.

**Recommendation:** reduce retrieval target to roughly 1,100–1,200 tokens in shadow mode and log selected-record marginal scores. Batch candidate and completion writes into one transaction to reduce conflicts and latency.

## GH API Call Audit

**Measured GitHub API call counts are unavailable.** No structured GitHub rate-limit event was observed; provider `rate_limit` signals in review logs must not be treated as GitHub API events.

- **Positive hygiene:** the sweep snapshots active runs once per workflow and filters PRs locally, following `CLAUDE.md §15` rather than issuing N×2 per-PR lookups.
- **Sweep call estimate — inference:** run `35477670449` had three candidates and zero dispatches. Its implementation performs at least seven logical paginated calls: one PR inventory plus three statuses across two review workflows.
  - **Change:** query repository-wide runs once per status and filter by workflow ID locally.
  - **Estimated reduction:** seven to four logical calls/tick, about **43%**.
- **Poller fallback calls:** run `35477466036` emitted branch fallbacks for issues `4143` and `4113`; based on the documented helper, these imply at least two additional branch-scoped lookups.
- **Reliability risk:** sweep status requests currently suppress errors. An incomplete active-run snapshot can appear empty and dispatch duplicate work. Emit `AUTOFIX_SWEEP_SNAPSHOT_INCOMPLETE` and skip dispatch for that tick.

Add a shared summary marker:

`GH_API_CALL_SUMMARY_V1 step=... logical_calls=... requests=... pages=... retries=... cache_hits=... rate_limit_wait_ms=... failures=...`

Do not log URLs containing sensitive query parameters.

## Prompt Cache & Memory System

- `cache_hit_rate`: unavailable.
- Cache reads: **154.3M tokens**, or **84.3%** of total reported tokens.
- Cache writes: zero reported; this may mean unsupported write accounting, not necessarily zero cache creation.
- Usage was available for 123/178 calls (**69.1%**); 55 calls lacked usage, limiting cost attribution.
- Reviewer logs showed supported/reused prompts, but reuse varied materially by model; Mistral frequently had unavailable usage.

**Changes:**

1. Emit `prompt_prefix_hash`, stable-prefix tokens, dynamic tokens, cache-read tokens, and model per call.
2. Compute a clearly named `cache_read_token_share`; do not label it `cache_hit_rate`.
3. Keep stable instructions first and move issue state, timestamps, check-run tails, and memory results after the cacheable prefix.
4. Log context truncation and Semble substitution so cache fragmentation can be correlated with the 21 context warnings.

## Orchestrator Health

- `orchestrate_poll`: **93/93 successful**, p50 **293s**, p95 **323.4s**—stable but close to a five-minute cycle.
- Run `35477466036` spent:
  - 91.8s processing tracking issues.
  - 93.8s persisting poll start/end memory events.
  - 42.9s checking out the repository.
  - These account for **76.9%** of total runtime.
- The fresh-push guard worked: issue `4113` was skipped at pushed age 66s, preventing an unnecessary review retrigger.
- Repeated review sweeps found PRs `#4144`, `#4136`, and `#4133` active and dispatched nothing, indicating review-stage saturation rather than an orchestrator terminal-state failure.
- No deep evidence showed clarification loops, conflict-heal retries, judge cycles, or terminal-state churn.

Add `ORCHESTRATOR_ISSUE_TIMING_V1 issue=... phase=... total_ms=... api_calls=... cache_hits=... state_writes=... action=...` and include active review run IDs, ages, statuses, and head SHAs in sweep-skip logs.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify → plan → implement | Trigger noise, mostly skipped | p50 1s; 491 combined skipped/other runs | Keep low priority; collect explicit skip reasons upstream |
| Review/autofix queue | Pending-run replacement and long active reviews | 22 cancellations; 18.1h reported cancelled lifetime | Enable self-trigger skip |
| Review compute | Six-reviewer, two-pass panels with degraded slots | p95 8,200s; 57 attempts; 12 retryable failures | Circuit-break unhealthy models |
| Review context | Near-window prompts | 21 warnings; ratio up to 1.0 | Dynamic context cap and Semble substitution metrics |
| Editor/merge | Work discarded after branch advance | Run `35451202045` ended `stale_base_skip` | Recheck head between phases |
| CI | Large monolithic lint job | p50 1,999s; deterministic failures after 490/855s | Move structural contracts first |
| Poll/orchestrate | Memory writes and per-issue processing | 297s sample; 94s memory persistence | Batch memory writes |
| Copilot review | Moderate compute; cleanup/firewall noise | p95 470s, no failures | Add outbound-domain and orphan-process summaries |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottleneck:** review/autofix, with p95 136.7 minutes and all reported model-token usage.
- **Top failure modes:** workflow contract regressions, unhealthy reviewer providers, stale-base work loss, and missing failing-job logs.
- **Highest cost drivers:** repeated self-trigger verification, full six-model panels, 27.5M prompt tokens, and 21 context-budget warnings.
- **Top three actions:**
  1. Set `AUTOFIX_SKIP_SELF_TRIGGERED=true`.
  2. Enable the existing reviewer circuit breaker and shadow-log tier decisions.
  3. Batch AI-memory writes and add complete failed-job/API call telemetry.

## Metrics Appendix

### Workflow outcomes

| Family | Runs | Success | Failure | Cancelled/other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 322 | 3 | 22 / 653 | 1s | 2,387s |
| review_autofix | 119 | 96 | 1 | 22 / 0 | 20s | 8,200.3s |
| ci | 25 | 23 | 2 | 0 / 0 | 1,999s | 2,389.4s |
| orchestrate_poll | 93 | 93 | 0 | 0 / 0 | 293s | 323.4s |
| copilot reviewer | 41 | 41 | 0 | 0 / 0 | 276s | 470s |
| clarify | 167 | 2 | 0 | 0 / 165 | 1s | 9.7s |
| plan | 164 | 2 | 0 | 0 / 162 | 1s | 10s |
| implement | 164 | 2 | 0 | 0 / 162 | 1s | 10.85s |

### Cost and cache

| Metric | Value |
|---|---:|
| OpenRouter calls | 178 |
| Prompt tokens | 27,539,444 |
| Completion tokens | 1,171,375 |
| Cache-read tokens | 154,263,246 |
| Cache-write tokens | 0 |
| Total tokens | 182,967,488 |
| Usage available/unavailable | 123 / 55 |
| Cache-read token share | 84.3% |
| `cache_hit_rate` | unavailable |
| Wall-clock p50 / p99 | 8,000ms / 10,096,260ms |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 21 |

### MCP telemetry

| System/target | Queries | Bytes | Fallbacks | Probe OK/failed/skipped |
|---|---:|---:|---:|---:|
| Semble, all assembled | 30 | 313,029 | 18 | n/a |
| Semble, reviewer-context deep sample | 8 | 114,459 | 0 | n/a |
| Semble, overflow deep sample | 18 | 141,942 | 2 runtime | n/a |
| Serena, no target observed | 0 | 0 | 0 | 0 / 0 / 0 |

- Semble fallbacks: 16 contract-test, two runtime.
- Serena tool calls/response bytes/query time: all zero.
- Other MCP servers observed: none.

### GH API summary

| Signal | Result |
|---|---|
| Measured total calls/pages/retries | Not collected |
| GitHub rate-limit events | None observed |
| Sweep minimum logical calls | Approximately 7/tick, inference |
| Poll branch-fallback lookups | At least 2 in run `35477466036`, inference |
| Required next collection | `GH_API_CALL_SUMMARY_V1` per step/run |

### Data coverage

- Assembled context reports **115 runs with log telemetry**; the deep-dive directory contains **20 downloaded full-log runs**.
- Broad aggregate metrics use the assembled context; detailed model/memory findings use eight complete slow review logs.
- Ten skipped runs had empty archives; 970 runs were not selected for full-log download.
- Failure run `35452034565` is missing its failing job log, despite archive status being reported successful.

## Deep Audit — Workflows & Scripts (2026-09-20)

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Integration-ref failures silently select the default branch
- **File path / lines:** `scripts/resolve_integration_ref.sh:51-69`; `.github/workflows/clarify.yml:118-129`; `.github/workflows/plan.yml:181-195`; `.github/workflows/implement.yml:447-458`; `.github/workflows/orchestrate_clarify_respond.yml:172-183`; `.github/workflows/validate.yml:153-164`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** The resolver uses raw `gh api` calls. Any transient issue/branch lookup failure returns non-zero, and all five callers convert every resolver failure into an empty ref, causing checkout of the repository default branch. **Inference:** an orchestrator-managed task can therefore plan, implement, or validate against the wrong branch during a transient GitHub failure.
- **Recommended fix:** Stage and source `gh_helpers.sh` with the resolver, wrap reads in `gh_retry`, and distinguish “no integration metadata” from “lookup unavailable.” Only the former should select the default branch; transient/auth failures should stop that run so the next trigger retries safely.

#### BUG-002 — Branch-review dedup treats API failure as “no open PR”
- **File path / lines:** `.github/workflows/internal-review.yml:92-124`; `scripts/review_collect_pr_metadata.sh:174-185`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** The open-PR lookup suppresses errors with `|| echo ""`, then sets `proceed=true` when the result is empty. A transient failure can launch a no-PR branch review while the PR-triggered review is already running. The metadata helper also converts a failed default-branch lookup to literal `main`.
- **Recommended fix:** Use `gh_retry`; emit `proceed=false` when PR-state retrieval is unconfirmed. Require a validated base ref in branch-review mode instead of falling back to `main`.

Audit checks found no additional actionable secret logging, unsafe issue/PR body interpolation, SC2086/SC2046 violations, YAML parse failures, Bash syntax failures, or Python syntax failures.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — Clarify fetches the same comment thread twice
- **File path / lines:** `.github/workflows/clarify.yml:457-484`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** With semantic caching enabled, the step first fetches 50 comments, then fetches the complete paginated thread from the same endpoint.
- **Current call count:** 2 logical calls.
- **Proposed call count:** 1 logical paginated call.
- **Recommended fix:** Fetch once through `gh_retry_to_file`; derive the bounded 50-comment prompt artifact and full cache history locally.

#### API-002 — PR-context collector bypasses the existing consolidated helper
- **File path / lines:** `scripts/review_collect_pr_metadata.sh:209-269`; `scripts/gh_helpers.sh:783-899`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** Metadata, issue comments, review comments, optional reviews, and closing issues are fetched separately despite the existing GraphQL-first `gh_pr_with_all_comments` pattern.
- **Current call count:** 4 normally; 5 with break-glass review retrieval.
- **Proposed call count:** 1 GraphQL call.
- **Recommended fix:** Extend `gh_pr_with_all_comments` to return top-level review metadata and `closingIssuesReferences`, then populate all existing artifact files from that result.

#### BATCH-001 — Standalone conflict sweep performs N+1 PR reads
- **File path / lines:** `scripts/orchestrate_poll_process.sh:22457-22517`
- **Severity:** High
- **Category tag:** `api-batching`
- **Description:** The sweep lists up to 100 open PRs, then fetches each PR individually to obtain mergeability and labels.
- **Current call count:** `1 + N`, up to 101 logical calls.
- **Proposed call count:** 1 paginated GraphQL snapshot.
- **Recommended fix:** Fetch open PR number, refs, head SHA, labels, `mergeable`, and `mergeStateStatus` together. Follow `_fetch_candidate_issue_details_graphql`’s transform-and-cache pattern.

#### BATCH-002 — Standalone issue inventory uses seven label-specific calls
- **File path / lines:** `scripts/orchestrate_poll_process.sh:15124-15131`; existing pattern at `scripts/orchestrate_poll_process.sh:14225-14264`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** Seven `gh issue list` requests collect one pipeline label each and are merged locally.
- **Current call count:** 7 logical calls per sweep.
- **Proposed call count:** 1 GraphQL call with seven aliases.
- **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql` or add a sibling helper returning the union of all label queries.

#### BATCH-003 — Security follow-up reconciliation reads each issue separately
- **File path / lines:** `scripts/orchestrate_poll_process.sh:5861-5946`; batching pattern at `scripts/orchestrate_poll_process.sh:14320-14436`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** Each unchecked follow-up receives an issue GET and, when blocked, a comments GET. `[NEEDS VERIFICATION]`
- **Current call count:** `R + B` reads, plus up to `B` required comment writes, where `R` is unchecked rows and `B` is blocked rows.
- **Proposed call count:** `ceil(R/25)` GraphQL reads plus required writes.
- **Recommended fix:** Extend `_fetch_candidate_issue_details_graphql` with the trusted unblock-marker fields, retaining REST fallback when comment pagination exceeds GraphQL limits.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — Context-budget telemetry function is triplicated
- **File path / lines:** `scripts/review_apply_fixes.sh:164-202`; `scripts/review_run_reviewers.sh:69-107`; `scripts/review_rb_judge.sh:256-294`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `emit_context_budget_warn_for_prompt` is byte-equivalent across three review paths.
- **Recommended fix:** Move it to `scripts/review_context_helpers.sh` as `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>` and source it from all three callers.

#### DUP-002 — Workflow metrics duplicate timestamp and percentile utilities
- **File path / lines:** `scripts/analyze_workflow_logs.py:40-72`; `scripts/collect_workflow_logs.py:96-108`; `scripts/cost_audit.py:283-295`; `scripts/workflow_retro.py:50-99`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Identical ISO-8601 parsing appears four times; identical percentile interpolation appears twice.
- **Recommended fix:** Add `scripts/workflow_metrics_utils.py` with `parse_iso8601(value)` and `percentile(values, pct)`, then update the four callers.

#### DUP-003 — Thread-reuse wrappers duplicate an existing helper
- **File path / lines:** `scripts/review_apply_fixes.sh:680-710`; `scripts/review_conflict_resolve.sh:150-171`; `scripts/validate_process.sh:2890-2911`; canonical helper at `scripts/codex_thread_reuse.sh:407-422`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** Three paths implement identical asset resolution and feature-gate wrappers despite sourcing `codex_thread_reuse.sh`.
- **Recommended fix:** Call `codex_thread_reuse_resolve_asset <repo_path>` and `codex_thread_reuse_truthy <value>` directly; remove the local wrappers after compatibility review.

#### DUP-004 — Consumer-repository runners duplicate command and registry infrastructure
- **File path / lines:** `scripts/audit_consumer_drift.py:51-164`; `scripts/validation_refresh_runner.py:96-163,701-723`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** `CommandResult.__str__`, command execution, and target-repository loading have identical implementations.
- **Recommended fix:** Add `scripts/github_cli_utils.py` exposing `run_command(...) -> CommandResult` and `load_target_repositories(path) -> list[str]`; update both callers.

No workflow pair exceeded the requested 70% similarity threshold.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001 — Implement support-staging block has limited expression headroom
- **File path / lines:** `.github/workflows/implement.yml:920-1276`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The parsed `run:` scalar contains `${{ }}` interpolation and is approximately **16,985 characters**, or 80.9% of the 21,000-character limit.
- **Headroom remaining:** Approximately **4,015 characters**.
- **Recommended fix:** Extract the support-file staging implementation to `scripts/stage_implement_support.sh`; keep only environment setup and one script invocation in YAML.

No other interpolated `run:` scalar exceeded 15,000 characters. No workflow exceeded 800 KB; the largest remained below 500 KB. No oversized `if:` expression was found.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001 — Log-analysis failure reporting bypasses `gh_retry`
- **File path / lines:** `.github/workflows/workflow-log-analysis.yml:889-1003`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The step sources `gh_helpers.sh` but posts its failure comment and label through raw `gh issue`/`gh label` commands. Transient failures can lose the diagnostic marker and failure label.
- **Recommended fix:** Use `gh_retry` for idempotent label operations. Replace the comment write with a marker-keyed upsert so retries cannot create duplicates.

#### DEAD-001 — Poller contains unreferenced internal functions
- **File path / lines:** `scripts/orchestrate_poll_process.sh:11266-11274,12876-12895,13005-13015`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `get_last_validation_run_conclusion`, `read_standalone_state_json`, and `stall_recovery_action_is_terminal` have definitions but no repository call sites.
- **Recommended fix:** Remove them in a reviewed compatibility cleanup, or wire them into their intended paths and add direct tests if they remain contractual.

#### DEAD-002 — Reviewer script retains unread variables and an uncalled mutator
- **File path / lines:** `scripts/review_run_reviewers.sh:753-761,3562-3571`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, and `reviewer_patch_reasoning_config_file` are never read or called.
- **Recommended fix:** Remove these internals after the §6 compatibility review, or document them as deliberate shims and add targeted shellcheck suppressions.

#### SHELL-001 — Single-element loops trigger SC2043
- **File path / lines:** `scripts/stage_workflow_support.sh:136-146,213-225`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** Two `for` loops iterate over one literal filename, obscuring control flow and producing SC2043 warnings.
- **Recommended fix:** Replace each loop with a direct variable assignment and retain the existing fallback/install logic.

No exact TODO, FIXME, HACK, or XXX debt markers were found in scoped files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BATCH-001 |
| Medium | 8 | BUG-002, API-001, API-002, BATCH-002, BATCH-003, DUP-001, EXPR-001, CONSIST-001 |
| Low | 6 | DUP-002, DUP-003, DUP-004, DEAD-001, DEAD-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 7 | Medium |
| API call optimization | 4–6 | Medium |
| Code modularization | 12–15 | Large |
| Expression size reduction | 2 | Medium |
| Medium/Low fixes | 5–7 | Medium |
