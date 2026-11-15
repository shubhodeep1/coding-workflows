## Executive Summary

> **Evidence gap:** the run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox, and no `workflow_log_report.json` was found under `/home/runner/work`. Every finding below comes from the run rows and aggregates in `analysis_context.json`, plus a read of the workflow and script source in this repo. I could not read any deep-dive log lines (no `errors/`, `slow/` or `recent/` folders, no `log_summary` fields). `insufficient_data` is `false`, but there is **no token, cache, GH-API or AI-memory telemetry**, so the cost and cache sections are bounded.

- **Two of the three failures happened in the same step type: "Resolve trusted … support commit".** Run 43663343175 (review_autofix, job `review / gate`) and run 43665676617 (security_audit, job `security-audit`) both failed there, about 17 minutes apart by computed end time (≈05:26:56Z and ≈05:43:25Z). Both in-repo paths read `repos/shubhodeep1/coding-workflows/branches/main`: `review_autofix.yml:487` and `security-audit.yml:152`. The review path has no explicit error message if that call fails, so the root cause cannot be told apart from the step's other exit paths. **Impact:** 2 of 3 failures (67%) in the window. **Confidence:** high on where it failed, low on why.
- **Runner or queue starvation from about 01:30Z to 07:35Z was the main latency driver (inference).** Lightweight checks show multi-hour durations: `lint_pr_body_auto_close` p50 17,117s / p95 19,774.7s, and `integration_pr_readiness` p50 14,900s / p95 19,984.5s. These durations are `updated_at − run_started_at` (`scripts/collect_workflow_logs.py:1147-1151`), so they include queue time. **Impact:** about 4–5.5 hours of wall clock per PR check. **Confidence:** medium. This is an inference because no split between queue time and compute time is collected.
- **The orchestrator poller made zero successful cycles in the window.** All 39 `orchestrate_poll` runs ended `cancelled` with `cancelled_before_first_step`, after 597–1,319s pending. This matches pending-run supersession in the group `ai-orchestrate-poll-${{ github.repository }}` (`orchestrate_poll.yml:97-102`, `cancel-in-progress: false`), triggered every 5 minutes (`internal-orchestrate-poll.yml:5`). **Impact:** wave progression stalls for the whole ~6.6h window. **Confidence:** medium-high on the symptom, medium on the cause.
- **84.5% of the 1,000-run sample is skipped fan-out noise.** That is 845 `other` runs: clarify 184, implement 184, plan 182, orchestrate_clarify_respond 184, workflow_failure_heal 45, workflow_failure_heal_intake 37, check_failure_triage 29. As a result the collector window covers only about 6.6h (00:56:51Z → 07:34:35Z), and only 2 successful runs were sampled for deep-dive. **Impact:** no AI-phase telemetry at all. **Confidence:** high.
- **Cost and cache telemetry is completely blank.** `codex_tokens_used`, `or_total_tokens` and `claude_cost_usd` are all 0, `cache_hit_rate` is null everywhere, and `break_glass_count` and `context_budget_warn_count` are both 0 across 112 runs with log telemetry. This reflects missing coverage, not zero spend. **Confidence:** high.
- **The Semble fallbacks are healthy fixture noise.** There were 4 `SEMBLE_FALLBACK`, all `semble_contract_test_fallbacks`, in CI run 43663335268. `semble_runtime_fallbacks` is 0. **Confidence:** high.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Measure and then relieve the queue backlog (critical path).**
   - **Evidence:**
     - Slow successes end clustered at about 07:10–07:36Z: 43663945752 ends ≈07:10:44Z and 43665571638 ends ≈07:36:51Z.
     - Trivial lint runs take 11,346–20,005s, e.g. 43663945752 and 43666007604.
     - `cancel_on_pr_close` has a job `timeout-minutes: 5` (`cancel_on_pr_close.yml:13`), yet 34/35 runs were cancelled after 597–1,253s pending, before their first step.
   - **Root cause (inference):** the concurrent-job capacity of the `ubuntu-latest` pool was saturated by long runs, such as review_autofix runs of up to 21,531s.
   - **Change, part 1 (collector diagnostic, no runtime risk):** in `scripts/collect_workflow_logs.py`, add `queue_seconds` = earliest `job.started_at − run.run_started_at`, plus `exec_seconds`. Only compute them for runs where jobs are already fetched (`jobs_fetch_status=success`) and for the `slow_runs` set. That keeps it to about 1 jobs call per slow run and adds no new calls for failure rows.
   - **Change, part 2:** only after `queue_seconds` confirms the backlog, cap review_autofix sweep concurrency.
   - **Estimated saving:** if queue time is confirmed, PR-check latency drops from hours to minutes.
   - **Risk:** none for the diagnostic. The concurrency cap carries medium risk.
2. **Unstick the poller (critical path for orchestration).**
   - **Evidence:** 39/39 runs cancelled before their first step. Examples: 43683259715 at 1,319s, 43665494434 at 1,177s, 43663370295 at 597s.
   - **Root cause (inference):** the active holder of the group ran longer than 10–20 minutes, or was itself stuck in the runner queue. GitHub keeps only one pending run per group and cancels older pending ones.
   - **Change:** add the diagnostic in Reliability item 3. Then set `timeout-minutes` on the `poll` job (`orchestrate_poll.yml:95-96`) to a value just above the observed p99 cycle time.
   - **Saving:** restores the 5-minute poll cadence.
   - **Risk:** low once the value comes from measured cycle times.
3. **Micro-optimization: skipped fan-out.** About 734 skipped runs came from 4 families firing on the same events; one example burst is about 100 runs between 07:33:16Z and 07:34:35Z. Skipped jobs use no runner minutes, so latency gains are negligible. The value is in collector coverage (see GH API Call Audit).

## Cost Optimizations

There is no token data to rank. `codex_calls`, `or_calls` and the Claude counters are all 0, and `cache_hit_rate` is null.

1. **Restore cost visibility (highest priority, prerequisite for any savings estimate).**
   - **Root cause:** `success_sample_rate=0.07` produced 2 sampled successes. AI-heavy successes are `not_selected`, for example review_autofix 43663553820, 43663313367 and 43663423200.
   - **Change:** in the collector's deep-dive selection, always include at least one successful run per AI family (review_autofix, clarify, plan, implement, validate, orchestrate).
   - **Savings:** none directly; this unlocks measurement.
   - **Quality risk:** none.
2. **Semble and Serena.**
   - Semble shows 0 `SEMBLE_QUERY` and 0 bytes. Its 4 fallbacks are all contract-test fixtures in CI 43663335268. No conclusion is possible about whether Semble reduces prompt expansion.
   - Serena shows 0 queries and 0 probes, so there is no evidence on whether it replaces tool or model work.
3. **Reruns.** All rows have `run_attempt=1` and `retries=0`, so no rerun cost is observed.

## Reliability Improvements

1. **"Resolve trusted support commit" failures (2 of 3 failures). Root-cause category: trust-gate / API-read failure, not yet attributed.**
   - **Evidence:**
     - Run 43663343175 failed at `review_autofix.yml:418` (step `Resolve trusted review support commit`).
     - Run 43665676617 failed at `security-audit.yml:127` (step `Resolve trusted audit support commit`).
     - The review step has six `exit 1` paths with `::error::` (lines 441, 480, 490, 496). It also has three implicit `set -e` exits with no message: the `gh_api_retry` calls at lines 471, 475 and 487.
     - The audit step wraps its API calls with explicit errors (lines 152-155, 163-172).
     - The `GH_PAT_BUDGET phase=start` line just before the review step (`review_autofix.yml:416`) would show the PAT's remaining budget at that point, but the logs are unavailable.
   - **Hypothesis (inference):** both failures fall in the same window about 4 hours after creation, which points to a shared dependency such as a `branches/main` read failure or PAT budget exhaustion during the backlog drain, rather than two separate bugs.
   - **Fix (diagnostic, fail-closed preserved):** extend the already-registered `SUPPORT_REF` prefix (`agents.md:4877`) with additive fields on every exit path. Also give line 487 the same explicit guard the audit step uses:
     ```bash
     # review_autofix.yml, Resolve trusted review support commit (replaces line 487 only)
     if ! support_main_json="$(gh_api_retry repos/shubhodeep1/coding-workflows/branches/main)"; then
       echo "SUPPORT_REF result=fail reason=main_read_failed workflow=review_autofix caller=${CALLER_REPOSITORY} status=${GH_API_RETRY_LAST_STATUS:-unknown}"
       echo "::error::Review support main branch read failed."
       exit 1
     fi
     ```
     Apply the same pattern to the other exits, with these reasons: `identity_unapproved` (441), `stable_tag_read_failed` (471/475), `stable_tag_not_commit` (480), `main_unprotected` (490) and `sha_invalid` (496). Mirror it in `security-audit.yml:146,154,164,171,178,183` with `workflow=security_audit`.
   - **Expected impact:** the next occurrence becomes attributable from one log line, and a known transient read failure can then get a targeted retry.
   - **Rollback:** this only adds echo lines. Exit semantics do not change, so the gate still fails closed.
2. **CI 43663335268. Root-cause category: unknown test failure.**
   - **Evidence:** job `tests-promote-stall-and-review`, step at `ci.yml:1642-1647`. The step runs three tests in sequence: `test_review_autofix_step_scripts_contract.py`, `test_workflow_file_size_limit.py` (480,000-byte guard at line 27) and `test_test_and_mark_stable_step_scripts_contract.py`.
   - **Fix (diagnostic):** prefix each command with `echo "CI_TEST_FILE=<name>"` so the failing file can be identified from a log tail.
   - **Next step:** read the run's step log to find which test failed. If it is the size guard, follow §24 and split `run:` bodies into `scripts/review_autofix_step_<slug>.sh`.
3. **Poller and cancel-on-close supersession is invisible.**
   - **Evidence:** 39/39 and 34/35 runs cancelled before their first step.
   - **Fix (collector diagnostic):** include `status=in_progress|queued` runs in the window, with their age. This exposes the run holding `ai-orchestrate-poll-*` or `cancel-on-pr-close-*`.
   - **Impact:** the poller stall can be attributed.
   - **Fail-open:** this is collector-only.
4. **Long-cancelled review_autofix runs.**
   - **Evidence:** 43663500848 (21,531s), 43663912641 (21,477s, `[pr:9455]`) and 43663935320 (21,471s). All have `empty_log_archive`, end within 69–129s of 21,600s (6h), and end at about 07:30–07:35Z, the same time as the skipped-run burst.
   - **Inference:** either a 360-minute limit or a cancel-on-close wave. These cannot be told apart without job data.
   - **Fix:** the `queue_seconds`/`exec_seconds` split from Speed item 1 settles it.
5. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** 0 / 0 in every family. There is no policy or prompt-size pressure signal, although coverage excludes AI phases.
6. **Semble and Serena fallbacks:**
   - The 4 Semble fallbacks are contract-test only. This is healthy fail-open fixture behaviour, not a masked broken rollout.
   - There were no Serena probe failures or runtime fallbacks.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines are available: there are no deep-dive logs and no `log_summary` fields. Retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open`/`enabled:false` entries and push retries are all **unknown**.

**Recommendation:** once the collector always deep-dives one successful clarify, plan, implement and review_autofix run (Cost item 1), check that `retrieve` and `finalize-task` events are emitted. Separately, run the analyst with the log directory actually mounted.

## GH API Call Audit

- **No GH API counts or rate-limit events are in the supplied context.** The collector does parse `GH_PAT_BUDGET phase=end … used_in_job=` (`scripts/collect_workflow_logs.py:112-116`), but no such aggregate appears in `analysis_context.json`. **Recommendation:** surface per-workflow/job `used_in_job` sums and the minimum `remaining` in the context. Also parse the `GH_API_RETRY outcome=… status=… endpoint=` stderr lines (format at `scripts/gh_helpers.sh:1047`).
- **The resolve steps make 1–3 sequential REST reads each** (`branches/main`, or `git/ref/tags/stable` + `git/tags/<sha>`). In-code comments already justify why they cannot be served by earlier calls (`review_autofix.yml:469-470`, `security-audit.yml:150-151,161-162`). There is no batching opportunity here; they are 1 call per gate run.
- **Collector budget:** 845 of 1,000 listed runs are skipped rows. Counting skipped runs against a separate cap, or paging until N non-skipped runs are collected, would widen the useful window from about 6.6h to an estimated full day without extra per-run calls. That estimate is an inference because pages are mostly skipped rows.
- Hygiene rules (§14) were checked against the resolve steps, and they comply. There is no evidence of loops issuing per-item calls in this window.

## Prompt Cache & Memory System

`cache_hit_rate` is null in every family and run. `or_cache_read_tokens`, `or_cache_write_tokens` and the Claude cache counters are 0, and `context_budget_warn_count` is 0. So cache fragmentation, prefix stability and fail-open behaviour **cannot be evaluated**.

The cause is coverage (see Cost item 1), not cache behaviour. **Next collection step:** deep-dive at least one successful review_autofix run per window, so its `REVIEW_AUTOFIX_RUN_SUMMARY_V1` line (`collect_workflow_logs.py:121-123`) and the reviewer cache counters get parsed.

## Orchestrator Health

- **Poller:** 0 completed cycles out of 39 runs; all cancelled before the first step after 597–1,319s pending. This is the top stall signal.
  - **Indicators to track:** poll success count per hour, and the age of the in-progress run holding the poll concurrency group.
- **Orchestrate:** 1 success (43663786787, 17,174s).
- **Validate:** 1 success (43663087699, `[tracking:0]`, 11,945s).
- **Unblock judge:** 1 success (43663970704, "Unblock judge #9135", 20,638s).
- **promote_candidate_follow:** 7/7 cancelled at 1,963–3,988s, e.g. 43680554340 and 43662482598. No follower completed in the window.
- **Clarify loops:** orchestrate_clarify_respond had 184 runs, all skipped. That is no evidence of clarification churn. Clarify had 2 successes (43663949913 and 43663752634), the latter at 17,134s.
- **Smallest safe mitigations:**
  1. Collector visibility into in-progress runs.
  2. A measured `timeout-minutes` on `poll`.
  3. The `queue_seconds` metric to confirm that runner starvation, not poller logic, is the cause.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant overhead |
|---|---|---|
| clarify | 2 success, 184 skipped, 1 cancelled (43663017955, cancelled before first step after 2,439s) | Queue (inference) |
| plan | 1 success (43663708819, 17,329s) | Queue (inference) |
| implement | 0 success, 184 skipped | No implement work completed in the window |
| review/autofix | 9 success, 1 failure, 12 cancelled; p50 1,814.5s, p95 21,476.7s | Queue plus long or cancelled runs, plus the gate trust-resolve failure |
| PR checks (lint/readiness) | p50 14,900–17,117s | Queue (inference: trivial compute) |
| validate/orchestrate | 1 success each; poller 0/39 | Concurrency supersession under backlog |

Fixes in order of end-to-end impact:

1. Get the split between queue time and execution time.
2. Relieve runner contention, starting with the review_autofix runs that last up to 6h.
3. Bound the poller's run time.
4. Add `SUPPORT_REF result=fail` attribution in the gate steps.

Merge and conflict overhead cannot be seen in this data.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Queueing of lightweight PR checks (4–5.5h).
  - The poller starved (0/39).
  - promote_candidate_follow cancelled every time (7/7).
- **Top failure modes:**
  - Trusted support-commit resolution, 2 failures: 43663343175 and 43665676617.
  - A CI contract or size-guard test, 1 failure: 43663335268.
  - 6h-adjacent cancellations of review_autofix runs, 3 of them.
- **Highest-cost drivers:** unknown, because there is no token telemetry. By inference, the long review_autofix runs are the likely driver.
- **Top 3 actions:**
  1. Add `SUPPORT_REF result=fail reason=…` to every exit of `review_autofix.yml:418-501` and `security-audit.yml:127-189`, and give line 487 an explicit guard.
  2. Add `queue_seconds`/`exec_seconds` and in-progress-run visibility to `scripts/collect_workflow_logs.py`.
  3. Exclude skipped runs from the 1,000-run cap and force one AI-family success per window into the deep-dive.

## Metrics Appendix

**Run outcomes (window ≈ 00:56:51Z – 07:34:35Z)**

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| All | 1000 | 55 | 3 | 97 | 845 | 0.3% (5.2% of 58 completed non-cancelled) | 1.0 | 11,570 |
| review_autofix | 22 | 9 | 1 | 12 | 0 | 4.5% | 1,814.5 | 21,476.7 |
| security_audit | 7 | 2 | 1 | 4 | 0 | 14.3% | 2,269 | 11,040.8 |
| ci | 1 | 0 | 1 | 0 | 0 | 100% | 14,159 | 14,159 |
| orchestrate_poll | 39 | 0 | 0 | 39 | 0 | 0% | 526 | 892.6 |
| cancel_on_pr_close | 35 | 1 | 0 | 34 | 0 | 0% | 574 | 1,152.2 |
| promote_candidate_follow | 7 | 0 | 0 | 7 | 0 | 0% | 3,211 | 3,952.9 |
| integration_pr_readiness | 19 | 19 | 0 | 0 | 0 | 0% | 14,900 | 19,984.5 |
| lint_pr_body_auto_close | 15 | 15 | 0 | 0 | 0 | 0% | 17,117 | 19,774.7 |
| clarify / plan / implement / orch_clarify_respond | 187 / 183 / 184 / 184 | 2 / 1 / 0 / 0 | 0 | 1 / 0 / 0 / 0 | 184 / 182 / 184 / 184 | 0% | 1.0 | ≤10 |
| workflow_failure_heal / heal_intake / check_failure_triage | 45 / 38 / 29 | 0 / 1 / 0 | 0 | 0 | 45 / 37 / 29 | 0% | 1–2 | 9–11 |

**Review and cost telemetry**

| Metric | Value |
|---|---|
| `cache_hit_rate` | null (all scopes) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo) | 1,000 / 20,562,540 (99 samples, dominated by skipped runs) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| codex / OR / Claude tokens | 0 / 0 / 0 (coverage gap) |
| GH API calls / rate-limit events | not present in context |
| `runs_with_log_telemetry` | 112 |

**Semble and Serena**

| Metric | Value |
|---|---|
| SEMBLE_QUERY calls / bytes | 0 / 0 |
| SEMBLE_FALLBACK total / contract-test / runtime | 4 / 4 / 0 (all CI 43663335268) |
| SEMBLE_BOOTSTRAP total / failed / unused / ms | 0 / 0 / 0 / 0 |
| SERENA_QUERY calls / tool calls / response bytes / ms | 0 / 0 / 0 / 0 |
| SERENA_FALLBACK | 0 |
| Per-target MCP availability (`probe_ok` / `probe_failed` / `probe_skipped`) | no targets observed (0 / 0 / 0) |
| Other MCP servers observed | none |

**Assumptions and missing context**
- I assumed a run's failure happened near its `updated_at`. This rests on the resolve step being early in the gate job, and on duration including queue time. It is unverified because there are no step timestamps.
- I could not get the log directory, `log_summary` fields, GH API aggregates, `AI_MEMORY_TELEMETRY` lines or token counters. I made no file changes; this is report output only.