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
## Deep Audit — Workflows & Scripts (2026-11-15)

> **Scope and method.** This audit read the source directly rather than sweeping every file. Files read: `security-audit.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `promote-candidate-follow.yml`, `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`; targeted ranges of `review_autofix.yml`, `orchestrate_poll.yml`, `orchestrate.yml`, `implement.yml`, `workflow-log-analysis.yml`; `scripts/gh_helpers.sh` (retry primitives), `scripts/label_helpers.sh`, `scripts/promote_candidate_follow.sh`; and targeted ranges of `scripts/orchestrate_poll_process.sh` (36,151 lines). Other workflows were only checked with repo-wide pattern searches.
>
> **Limits.** Byte and character sizes could not be measured with `wc -c` in this sandbox. Every size below is an estimate and is marked **[NEEDS VERIFICATION]**.
>
> **Overlap with the current report.** It already covers diagnostics for the support-commit resolve step (Reliability item 1), the poller `timeout-minutes` (Speed item 2) and the queue split (Speed item 1). Findings here cross-reference those items instead of repeating them.

### Section 1: Bug & Correctness Sweep

- **BUG-001** — `.github/workflows/orchestrate_poll.yml:147-188, 572-614`; `.github/workflows/orchestrate.yml:202-…, 376-…` — Severity: **Medium** — Category: `bug`
  - **Description:** The `poll` job resolves the support ref twice, and both steps always run.
    - The two steps are "Resolve workflow support ref for gh retry" (147) and "Resolve workflow support ref" (572). Neither has an `if:`. The job starts at line 95, and the only other job is `claude_pool_daily_usage` (2227).
    - On the promote-candidate path, each step does its own `branches/promote-candidate` read (166 and 592) and overwrites `SCRIPT_REF` in `GITHUB_ENV` (175 and 601).
    - `scripts/promote_candidate_follow.sh` fast-forwards that branch on every push to `main` (`promote-candidate-follow.yml:14-15`; script lines 237-238). So the two reads can return different tips.
    - The result is mixed support versions in one job. Steps 190-251 stage `gh_helpers.sh`, `review_merge_train.sh` and `label_helpers.sh` from the first SHA. "Stage workflow support files" (664-760) stages everything else, and re-installs `gh_helpers.sh` (673), from the second SHA. Steps in between run with first-SHA helpers.
    - `orchestrate.yml` repeats the pattern: "Resolve workflow support ref for run-start" (202) and "Resolve workflow support ref" (376), both unconditional in job `orchestrate`.
    - How likely the race is to hit is an inference; the mixed-version outcome follows from the code.
  - **Recommended fix:**
    - Have the first resolve step also write the resolved candidate SHA to `GITHUB_ENV`, e.g. `PROMOTE_CANDIDATE_RESOLVED_SHA=<40-hex|none>`. That name is currently unused in the repo.
    - The second step uses that value when it is a valid SHA. It does the `branches/promote-candidate` read only when the value is unset, so it fails open to the current behaviour.
    - Keep the step shape that `tests/test_workflow_support_ref_follows_pin.py` asserts (cited at `implement.yml:780-781`) **[NEEDS VERIFICATION: exact assertions]**.

- **BUG-002** — `.github/workflows/cancel_on_pr_close.yml:50-58`; `orchestrate_poll.yml:164-173, 590-599`; `orchestrate.yml:219, 393`; `clarify.yml:406, 2523`; `plan.yml:525, 3868`; `check_failure_triage.yml:332`; `workflow_failure_heal.yml:118`; `review_autofix_sweep.yml:227` — Severity: **Medium** — Category: `bug`
  - **Description:**
    - These 11 steps across 8 workflows run `source "${GH_API_RETRY_HELPERS:-/dev/null}"`, but nothing in those workflows ever sets `GH_API_RETRY_HELPERS`.
    - It is only exported by the "Bootstrap GitHub API retry helper" step in `validate.yml:143`, `review_autofix.yml:354`, `implement.yml:164`, `security-audit.yml:77` and `orchestrate_clarify_respond.yml:91`. No composite action under `.github/actions/` sets it either.
    - So the inline fallback `gh_api_retry() { …; gh api "$@"; }` always runs: a single attempt with no retry. The call is wrapped in `2>/dev/null || true`, which discards the error.
    - A transient read failure is then logged as `SUPPORT_REF source=promote_candidate_unprotected`, the same line as a genuinely unprotected branch. The step silently keeps `github.sha` support code instead of the candidate.
    - Every `internal-*.yml` wrapper calls `@promote-candidate` (e.g. `internal-orchestrate-poll.yml:18`, `internal-cancel-on-pr-close.yml:16`). That makes this the hot path, every 5 minutes, for both the poller and the cancel sweep.
  - **Recommended fix:**
    1. Where `scripts/gh_helpers.sh` is already staged earlier in the same job, source it instead of the unset variable. For example, `orchestrate_poll.yml:590` runs after the staging at line 251.
    2. In the other copies, add the existing bootstrap step (`review_autofix.yml:354-402`).
    3. Separate the two outcomes in the log, as an additive change to the existing `SUPPORT_REF` prefix that the current report's Reliability item 1 also extends. Log `SUPPORT_REF source=promote_candidate_read_failed status=${GH_API_RETRY_LAST_STATUS:-unknown}` when the read exits non-zero, and keep `promote_candidate_unprotected` only for `.protected != true`.

- **BUG-003** — `scripts/label_helpers.sh:196-214`; `scripts/gh_helpers.sh:228-231, 840-852, 1262-1287` — Severity: **Medium** — Category: `bug`
  - **Description:** In production, creating a label that already exists costs about 15 s and several extra POSTs.
    - `ensure_label_exists` calls `gh_retry gh label create`.
    - `gh label create` is not in the single-attempt list of `_gh_cmd_is_unsafe_post` (849).
    - gh's "already exists" message (the repo's own fakes model it as `label with name "x" already exists; use --force …`, in `tests/test_label_helpers_phase_transition_contract.py:94` and `tests/test_implement_post_codex_recovery.py:288`) does not match the `_is_gh_permanent_failure` regex (230). That regex matches `HTTP 422`, not this text.
    - So `gh_retry` retries up to `GH_RETRY_MAX_ATTEMPTS=5` times, with 1+2+4+8 s of backoff (1279-1286), before `ensure_label_exists` finally recognises "already exists" in the stderr excerpt.
    - The tests miss this. `test_label_helpers_phase_transition_contract.py:172-174` copies only `label_helpers.sh`, so the no-retry fallback `gh_retry` (`label_helpers.sh:19-20`) runs. `test_orchestrate_poll_process.py:10669` uses the `HTTP 422 … already_exists` text, which does match.
    - The result is cached only per process (`label_helpers.sh:164-166`); subshells repeat it.
    - **[NEEDS VERIFICATION]** exact stderr of the gh CLI version installed on runners.
  - **Recommended fix:**
    - Add the narrow alternative `label with name .* already exists` to the `_is_gh_permanent_failure` regex. `gh_retry` then returns on the first attempt and still writes the stderr excerpt that `ensure_label_exists` greps (210).
    - Add a test that sources the real `gh_helpers.sh` beside `label_helpers.sh` and asserts one create call.
    - This keeps the `"gh_retry gh label create"` text that `test_label_helpers_phase_transition_contract.py:54` asserts.

- **BUG-004** — `scripts/promote_candidate_follow.sh:203-227` — Severity: **Medium** — Category: `bug`
  - **Description:** A lightweight or unparsable freeze tag freezes the candidate branch indefinitely, with no alert.
    - The age-based stale-tag deletion only works for annotated tags whose message carries `frozen_at=<epoch>` (208-219).
    - A lightweight `promote-cycle-frozen` tag, or an annotated tag without that field, always falls through to `ACTION=frozen` (226), and no alert is sent.
    - If a crashed promote cycle leaves such a tag, `promote-candidate` stops following `main`. All 14 `internal-*.yml` wrappers then keep running stale support code, and the only trace is a `PROMOTE_CANDIDATE_FOLLOW outcome=frozen` line per run.
  - **Recommended fix:** When `ACTION=frozen` and `FROZEN_AT` is empty, call `follow_alert` once per UTC day (same gating as BUG-005's fix) with reason `freeze_tag_unparsable`. Optionally, read the tagged commit's committer date (one extra `git/commits/<sha>` read, only on this path) and apply `FREEZE_MAX_SECS` to it.

- **BUG-005** — `scripts/promote_candidate_follow.sh:192, 246-247`; `.github/workflows/promote-candidate-follow.yml:17, 23-25` — Severity: **Medium** — Category: `bug`
  - **Description:** The CRITICAL "unprotected branch" alert can be skipped for whole days.
    - The once-per-day alert fires only when a `schedule` run sees `date -u +%H` equal to `00`. With cron `17 * * * *`, that means only the 00:17 run can alert.
    - The workflow-level concurrency group (23-25) keeps only one pending run. The current report shows 7/7 `promote_candidate_follow` runs cancelled while pending, with multi-hour queueing.
    - A 00:17 run that is delayed past 01:00 or superseded by a push-triggered run skips that day's alert entirely.
  - **Recommended fix:** Replace the hour check with a daily marker using the cache pattern already used for the Claude pool report (`orchestrate_poll.yml:104-107`, `claude_pool_daily_due` from `steps.pool_daily_seen.outputs.cache-hit`). Alert on the first `schedule` run of each UTC day that has no marker, then save the marker.

- **BUG-006** — `.github/workflows/lint-pr-body-auto-close.yml:85-86` — Severity: **Low** — Category: `bug`
  - **Description:** The required auto-close lint reads PR commits with a raw `gh api … --paginate` and no retry. The comment at 83-84 says a failed read fails the step. A transient 5xx therefore turns the required check red until someone re-runs it.
  - **Recommended fix:** `source scripts/gh_helpers.sh` (the checkout is present at 49-56) and wrap the call in `gh_retry gh api …`. The step stays fail-closed but survives transient errors.

- **SEC-001** — `.github/workflows/implement.yml:1351, 5788`; `review_autofix.yml:2178, 4873`; `clarify.yml:380`; `plan.yml:505`; `orchestrate.yml:139`; `orchestrate_poll.yml:493`; `orchestrate_clarify_respond.yml:403, 1504`; `memory_maintenance.yml:58`; `issue_pr_status.yml:43` — Severity: **Low** — Category: `security`
  - **Description:** These 12 steps persist `GH_PAT` in the workspace `.git/config` via `git remote set-url origin "https://x-access-token:${GH_TOKEN}@…"`.
    - The isolated agent workspace excludes `.git` and credential paths (`scripts/codex_isolated_workspace.py:5, 128`), and the pre-push test gate runs in `docker run --network none` (`scripts/prepush_test_gate_run.sh:15, 342`). So no exposure path is known.
    - It is still defence-in-depth debt: any future non-isolated step that runs PR or repo code would inherit the PAT.
    - Newer steps already use a one-shot credential helper instead (`workflow-log-analysis.yml:2239-2243`, "§23.E").
    - **[NEEDS VERIFICATION]** that no non-isolated PR-code step runs after these lines.
  - **Recommended fix:** Migrate these steps to the `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_0=credential.helper` env pattern (`workflow-log-analysis.yml:2241-2243`), scoped to the steps that push or clone.

- **SEC-002** — `.github/workflows/workflow-log-analysis.yml:789, 797-798, 1472, 1679, 1683, 2335, 2950` — Severity: **Low** — Category: `security`
  - **Description:** Step outputs are interpolated straight into shell source, e.g. `REPORT_FILE="${{ steps.analyze.outputs.report_file }}"` (1472). The values come from internal steps (the report path is built at 1247-1257), so injection risk is low. But the pattern breaks the env-passing convention used elsewhere in the same file (1096-1103), and it adds to the expression length (see EXPR-002).
  - **Recommended fix:** Move each value to the step's `env:` block, e.g. `REPORT_FILE_OUT: ${{ steps.analyze.outputs.report_file }}`, and reference `"${REPORT_FILE_OUT}"` in the body.

### Section 2: GitHub API Call Redundancy Audit

- **API-001** — `.github/workflows/orchestrate_poll.yml:166, 592`; `orchestrate.yml:219, 393`; `implement.yml:489, 794, 1373` — Severity: **Medium** — Category: `api-redundancy`
  - **Description:** The same `repos/shubhodeep1/coding-workflows/branches/promote-candidate` read happens more than once in one job (see BUG-001).
    - **Current:** 2 calls per poll tick, 2 per orchestrate run, and up to 3 per implement run when the conditional steps at 469 and 772 fire.
    - **Proposed:** 1 call per job.
    - At the poller's `*/5` cadence that saves about 288 REST calls a day once ticks actually run (estimate).
  - **Recommended fix:** Cache the result for the rest of the cycle in `GITHUB_ENV` (`PROMOTE_CANDIDATE_RESOLVED_SHA`, see BUG-001), following the §14 rule that cycle-local caches are first-class. Fall back to the read on a cache miss.
  - **Batching pattern to extend:** none needed; this is a single scalar.

- **API-002** — `scripts/orchestrate_poll_process.sh:189-247` (`replay_failed_reclarify_commands`) — Severity: **Medium** — Category: `api-redundancy`
  - **Description:** The function makes per-iteration REST calls inside the `while read issue_num` loop.
    - The per-issue `GET issues/${issue_num}` (202) only re-checks open state, not-a-PR and label presence. The `gh issue list --state open --label ai:reclarify-requeue` at 193-194 already guarantees all three in the same tick.
    - **Current:** 1 rate-limit read + 1 list + 1 user read + 2N calls (issue + comments), plus DELETE/POST.
    - **Proposed:** 1 + 1 + 1 + N, by dropping line 202 and checking `state`/`labels` from `--json number,state,labels` on the existing list.
    - The documented contract at 186-188 says "live issue read per queued issue". Whether the list's snapshot is fresh enough is a judgement call **[NEEDS VERIFICATION]**.
    - Batching the comments read is not proposed: line 206 requires complete history, which a single aliased GraphQL query cannot guarantee.
  - **Recommended fix:** Extend the existing list call's `--json` fields and remove the per-issue GET. Keep the comments pagination.

- **API-003** — `scripts/orchestrate_poll_process.sh:8817-8838` (`security_pass_unblock_filed_advisory_followups`) — Severity: **Low** — Category: `api-batching`
  - **Description:** The function does one `gh_retry gh api issues/${row_issue}` per follow-up row inside the loop (8822).
    - **Current:** N REST calls, plus K comments paginations for blocked rows.
    - **Proposed:** ceil(N/25) GraphQL calls, plus K.
    - N is likely small (security follow-ups per tracking issue), which is why the severity is Low.
  - **Recommended fix:** Extend `_fetch_issue_labels_batch_graphql` (4778-4791; 25 aliases per batch, missing keys fall back to REST) with `state`, or add a sibling with the same contract. Use it to prefetch all rows before the loop. Fall back per issue on cache misses.

- **API-004** — `scripts/orchestrate_poll_process.sh:18984-19000` — Severity: **Low** — Category: `api-redundancy`
  - **Description:** `get_standalone_state_comment_id` and `read_standalone_state_json` each make the same full paginated `issues/${issue_num}/comments` fetch.
    - `get_standalone_state_comment_id` is reachable only from the 2-argument path of `write_standalone_state_json` (19018-19022).
    - All 19 call sites (25461-26512) pass 3 arguments.
    - `read_standalone_state_json` has no callers (see DEAD-001).
  - **Recommended fix:** No runtime change needed. Add a contract test asserting that every `write_standalone_state_json` call passes 3 arguments, so the paginated fallback cannot quietly come back into use.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — `.github/workflows/review_autofix.yml:354-402`; `security-audit.yml:77-125`; `validate.yml:143-…`; `implement.yml:164-…`; `orchestrate_clarify_respond.yml:91-…` — Severity: **Medium** — Category: `duplication`
  - **Description:** The 49-line "Bootstrap GitHub API retry helper" step is copied verbatim into 5 workflows: `ghr_ref`/`ghr_pin` resolution, the base64 auth header, a 3-try git clone, and the `GH_API_RETRY_HELPERS` export. It has to run before any checkout, so a `./.github/actions/*` composite action cannot replace it.
  - **Recommended fix:**
    - Keep the copies inline, but add a parity test (e.g. `tests/test_gh_api_retry_bootstrap_parity.py`) that extracts the step body from every workflow and asserts the bodies are byte-identical.
    - Also add the step to the 8 workflows in BUG-002 that consume `GH_API_RETRY_HELPERS` without producing it, so one template governs every consumer.

- **DUP-002** — the "Resolve workflow support ref" promote-candidate block, 16 copies across 9 workflows (`implement.yml:478, 783, 1362, 7299`; `clarify.yml:397, 2514`; `plan.yml:516, 3859`; `orchestrate.yml:210, 384`; `orchestrate_poll.yml:155, 581`; `cancel_on_pr_close.yml:41`; `orchestrate_clarify_respond.yml`; `check_failure_triage.yml`; `workflow_failure_heal.yml`) — Severity: **Medium** — Category: `duplication`
  - **Description:** The same 40-line block (jq `workflow_ref`, regex, branches read, `SCRIPT_REF` export) differs only in the workflow file name inside the regex. BUG-002 and BUG-001 exist in every copy, so a fix has to land 16 times.
  - **Recommended fix:**
    - The block also runs pre-checkout, so keep it inline but parameterise only the file-name token.
    - Extend the parity test in `tests/test_workflow_support_ref_follows_pin.py` to assert all 16 bodies are identical after substituting the file name.
    - Apply the BUG-001 and BUG-002 changes in a single PR across all copies.

- **DUP-003** — `.github/workflows/workflow-log-analysis.yml:1105-1438, 1979-2233, 2596-2848` — Severity: **Medium** — Category: `duplication`
  - **Description:** The analyze, deep-audit and api-redundancy run bodies repeat four blocks:
    - the `AI_PHASE_FAILURE_V1` emitter (1137-1230 vs 2000-2076, and again in the api-redundancy block);
    - the `_wla_label_retry` / `_wla_label_create` helpers (1115-1116 vs 1987-1988);
    - the Semble prefetch (1323-1336 vs 2088-2101);
    - the Claude/codex engine retry loop (1366-1435 vs 2134-2198).
  - This was already recommended in `analysis/workflow-optimization-2026-11-13.md:434` and is still unapplied: `scripts/workflow_log_analysis_helpers.sh` does not exist (only `workflow_log_analysis_deliver_report.sh` does).
  - **Recommended fix:**
    - Create `scripts/workflow_log_analysis_helpers.sh` with:
      - `wla_emit_phase_failure <phase> <resume_action> <failed_step> <failure_mode> <attempt_count> <summary>`, reading `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW` and `GITHUB_SERVER_URL` from the environment;
      - `wla_run_engine_pass <role> <phase> <prompt_file> <out_file> <max_attempts> <backoff_base>`.
    - Source the helpers in all three steps.
    - This also removes the expressions behind EXPR-001 and EXPR-002.

- **DUP-004** — `.github/workflows/workflow-log-analysis.yml:1116, 1988` (plus the api-redundancy copy); 16 inline `gh label create` calls across `implement.yml` (8), `review_autofix.yml` (4), `workflow-log-analysis.yml` (3) and `clarify.yml` (1) — Severity: **Low** — Category: `duplication`
  - **Description:** `_wla_label_create` re-implements `ensure_label_exists` (`scripts/label_helpers.sh:176-218`), including the "already exists" check.
  - **Recommended fix:** After BUG-003 lands, replace the inline creates with `ensure_label_exists <label> "${GITHUB_REPOSITORY}"` wherever `label_helpers.sh` is staged. That also gives the per-process cache (164-166) and the catalog colours.

- **DUP-005** — `.github/workflows/orchestrate_poll.yml:694-760` — Severity: **Low** — Category: `duplication`
  - **Description:** The "stage optional support script" loop (`src=…; fall back to -main; notice; install -m 0755; _fetched_scripts+=`) appears at least 5 times in one step, for `render_prompt.py`, `orchestrate_stall_retired_conflict.sh`, `check_triage_resolution.py`, `validate_editor_audit.sh` and the semble trio. Each copy differs only in the file list and the notice text.
  - **Recommended fix:** Define one inline function at the top of the step, `_stage_optional_support <file> <notice>` (inline because helpers are not staged yet), and call it once per file. Behaviour is unchanged.

- **DUP-006** — `.github/workflows/cancel_on_pr_close.yml:203-263`; `scripts/orchestrate_poll_process.sh:262, 4791, 22509, 22575, 22694, 22879, 22989` — Severity: **Low** — Category: `duplication`
  - **Description:** There are at least 8 hand-rolled aliased-GraphQL batch builders, and they are inconsistent:
    - batch sizes of 50 vs 25;
    - `cancel_on_pr_close.yml:220-233` passes `$owner`/`$name` as GraphQL variables, while `orchestrate_poll_process.sh:4832-4835` interpolates them into the query string;
    - partial-error handling differs.
  - **Recommended fix:** Add `gh_graphql_alias_batch <numbers_file> <batch_size> <alias_prefix> <selection> <output_file>` to `scripts/gh_helpers.sh` (name currently unused). Use GraphQL variables for owner/name, numeric validation per alias (as at `cancel_on_pr_close.yml:223-225`), and the "omit failed batch, caller falls back" contract from `orchestrate_poll_process.sh:4785-4790`. Migrate new callers first; existing helpers can move incrementally.

### Section 4: Expression Size Limit Risk Assessment

- **EXPR-001** — `.github/workflows/workflow-log-analysis.yml:1105-1438` (step "Run workflow log analysis") — Severity: **High** — Category: `expression-limit`
  - **Description:** This 333-line `run:` body contains 12 `${{ }}` interpolations (1150-1152, 1165, 1171-1173, 1210-1212), so the whole body is evaluated as one expression.
    - **Estimated size:** about 16,000–19,000 characters (333 lines × roughly 50–57 characters after the 10-space indent is stripped). This is an inference, not a measurement.
    - **Headroom:** about 2,000–5,000 characters before the 21,000 limit.
    - The CI guard `test_interpolated_run_bodies_below_expression_guard` fails at 18,000 (`tests/test_workflow_file_size_limit.py:32, 87`). If this body is at or above 18,000, it is a candidate explanation for CI failure 43663335268 in the current report (the step at `ci.yml:1642-1647` runs that test) **[NEEDS VERIFICATION]**.
  - **Recommended fix:** This is a zero-behaviour-change fix. Replace every `${{ }}` in the body with the runner's built-in env vars, so the body stops being an expression at all:
    ```bash
    "workflow_run_id=${GITHUB_RUN_ID}" \
    "workflow_run_attempt=${GITHUB_RUN_ATTEMPT}" \
    "workflow_name=${GITHUB_WORKFLOW}" \
    ```
    ```bash
    local run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
    ```
    Then extract the duplicated blocks into scripts (DUP-003).

- **EXPR-002** — `.github/workflows/workflow-log-analysis.yml:1979-2233` (deep audit) and `2596-2848` (API redundancy); `.github/workflows/workflow-log-analysis.yml:1664-1720, 2319-2345, 2934-2960` (Telegram steps) — Severity: **Medium** — Category: `expression-limit`
  - **Description:** The two audit bodies are about 254 lines each, with 9 interpolations each (2011-2013, 2025, 2031-2033, 2064-2066; and 2628-2683).
    - **Estimated size:** about 12,000–14,500 characters each **[NEEDS VERIFICATION]**.
    - **Headroom:** about 6,500–9,000 characters.
    - Both embed prompt-contract lists (e.g. `required_headings` at 2206-2215) that grow every time a report section is added.
    - The Telegram steps are small, but they interpolate `${{ github.repository }}` and step outputs inline.
  - **Recommended fix:** Same as EXPR-001: use `GITHUB_*` env vars, and move step outputs to `env:` (SEC-002). Move the heading list into the prompt file or a helper so it can grow without touching the workflow.

- **EXPR-003** — `.github/workflows/orchestrate_poll.yml:154-155, 580-581`; `cancel_on_pr_close.yml:40-41`; `review_autofix.yml:534`; the other 13 resolver copies (DUP-002) — Severity: **Low** — Category: `expression-limit`
  - **Description:** Short bodies use inline `${{ github.repository }}` / `${{ github.sha }}` / `${{ steps.checkout_fingerprint_cap.outcome }}`. None is near the limit, but each one turns its body into an expression.
  - **Recommended fix:** Use `${GITHUB_REPOSITORY}` and `${GITHUB_SHA}` (identical in reusable workflows), and pass step outcomes through `env:`.

- **EXPR-004** — Workflow file size: `.github/workflows/review_autofix.yml` (6,049 lines), `implement.yml` (7,362 lines), `test-and-mark-stable.yml` (7,045 lines) — Severity: **High** — Category: `expression-limit`
  - **Description:** The real limit is **512,000 bytes**, with the repo guard at 480,000 (CLAUDE.md §27; `tests/test_workflow_file_size_limit.py:26-27`). It is not the 1 MB / 800 KB stated in this prompt (see CONSIST-003). No workflow is near 800 KB.
    - The last recorded sizes are 465,643 bytes for `review_autofix.yml` (`docs/plans/unattended-claude-pipeline-completion-plan.md:211`) and 433,073 bytes for `implement.yml` (`docs/plans/ci-reissue-branch-routing-and-sandbox-solc-plan.md:43`).
    - Several features have landed since, so `review_autofix.yml` may be at or above the 480,000 guard **[NEEDS VERIFICATION: run `wc -c`]**.
  - **Recommended fix:** Run `wc -c .github/workflows/*.yml`. For any file within 50,000 bytes of 480,000, move the largest inline `run:` bodies into `scripts/review_autofix_step_<slug>.sh`, following the registered pattern (agents.md "Workflow file size limit", line 2901). Do this in the same PR, per §27.

- **EXPR-005** — `tests/test_workflow_file_size_limit.py:66-98` — Severity: **Low** — Category: `expression-limit`
  - **Description:** The expression guard only checks `run:` strings. Job- and step-level `if:`, `with:` and `env:` values are not measured. No large `if:` was found in the files read; the longest seen is `workflow-log-analysis.yml:76`, well under 1,000 characters.
  - **Recommended fix:** Extend the test to measure every string value that contains `${{`, including `if:` (which is implicitly an expression), using the same 18,000-character guard.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — `scripts/orchestrate_poll_process.sh:18993-19000` (`read_standalone_state_json`) — Severity: **Low** — Category: `dead-code`
  - **Description:** A repo-wide search finds only the definition. This was raised in `analysis/workflow-optimization-2026-11-12.md:372` and rejected under naming immutability (`analysis/recommendation-processing-report.md:1218`).
  - **Recommended fix:** Keep it, per §10. Add a `# Deprecated: no callers; kept for naming immutability` comment and the 3-argument contract test from API-004 so the paginated path cannot quietly return.

- **DEBT-001** — `scripts/orchestrate_poll_process.sh:1-21` — Severity: **Medium** — Category: `tech-debt`
  - **Description:** The poller is a single 36,151-line bash file.
    - Its header still says "~27,700-line poller" (14) and lists only 12 required env vars (6-10), while the calling step passes far more.
    - ShellCheck extended analysis is disabled for the file because it hung CI (12-19).
    - 441 GitHub API call sites live in one file. That makes §14 audits (API-002, API-003) hard, and reviewers cannot run full static analysis.
  - **Recommended fix:**
    - Correct the header line count and the env list.
    - Move self-contained subsystems (e.g. standalone stall recovery around 24,900-26,500, the reclarify replay at 189-247) into sourced modules. Stage them the way `orchestrate_stall_retired_conflict.sh` is staged (`orchestrate_poll.yml:709-720`, optional with fail-closed stubs).
    - Re-enable extended analysis per module.

- **CONSIST-001** — `scripts/gh_helpers.sh:1279-1286` vs `1169-1172` — Severity: **Low** — Category: `consistency`
  - **Description:** The two retry primitives back off differently:
    - `gh_retry` waits 1, 2, 4, 8 s with no jitter;
    - `gh_api_retry` waits `2·2^(n-1) + RANDOM%3`, capped by `GH_RETRY_BACKOFF_CAP_SECS`.
  - Under the concurrent poller, review and cancel load, `gh_retry`'s fixed schedule can synchronise retries across jobs (inference).
  - **Recommended fix:** Add the same `RANDOM % 3` jitter and `GH_RETRY_BACKOFF_CAP_SECS` cap to `gh_retry` and `gh_retry_to_file` (1365-1372). Keep the existing env names.

- **CONSIST-002** — `.github/workflows/security-audit.yml:405, 498, 518` — Severity: **Low** — Category: `consistency`
  - **Description:** `GH_PAT` is optional (36-37), but the "Report synced-file security findings upstream" and "Report refused AI engine fallbacks" steps set `GH_TOKEN: ${{ secrets.GH_PAT }}` with no fallback. Both are `continue-on-error`, so in a consumer without `GH_PAT` they quietly run with an empty token. Line 405 also uses `secrets.GITHUB_TOKEN`, while every other step uses `github.token`.
  - **Recommended fix:** If those scripts can work with the job token, use `${{ secrets.GH_PAT || github.token }}` as at 80, 132 and 464. Otherwise add an explicit `GH_PAT`-empty skip line with a log prefix. Normalise 405 to `github.token`. **[NEEDS VERIFICATION]** which token scope `security_audit_upstream_report.sh` needs.

- **CONSIST-003** — `prompts/mode-workflow-audit.txt:83-85, 100`; `prompts/_templates/mode-workflow-audit.txt:76-78, 93` — Severity: **Medium** — Category: `consistency`
  - **Description:** The deep-audit prompt states a "1 MB (1,048,576 characters)" workflow limit and asks auditors to flag files above 800 KB. That contradicts CLAUDE.md §27, unattended §24 and `tests/test_workflow_file_size_limit.py:26-27` (512,000 / 480,000 bytes). An auditor following the prompt would never flag a workflow that has really stopped running. The historical anchors at 83-85 are line references that drift **[NEEDS VERIFICATION: current validity]**.
  - **Recommended fix:** Change line 100 (and template line 93) to: "GitHub stops starting runs for a workflow file over 512,000 bytes; the repo guard is 480,000 (CLAUDE.md §27). Flag any workflow at or above 430,000 bytes." Replace the line anchors with incident references (PR or issue numbers).

- **CONSIST-004** — `.github/workflows/lint-pr-body-auto-close.yml:27-47`; `.github/workflows/integration-pr-readiness.yml:25-44` — Severity: **Medium** — Category: `consistency`
  - **Description:** Neither required PR check declares a `concurrency:` group, unlike `promote-candidate-follow.yml:23-25` and `cancel_on_pr_close.yml:14-23`. Every `edited`, `synchronize` or `labeled` event queues another full run. The current report measures p50 of 14,900–17,117 s for these two families (Speed item 1), so superseded runs add to the runner backlog. Whether this helps depends on the queue split confirming starvation **[NEEDS VERIFICATION]**.
  - **Recommended fix:** Add to both workflows:
    ```yaml
    concurrency:
      group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    ```
    The newest event re-posts the status or check for the same head SHA. Check that branch protection treats a superseded cancelled run as replaced.

- **SHELL-001** — `scripts/promote_candidate_follow.sh:232, 241, 249-250, 254-255` — Severity: **Low** — Category: `shellcheck`
  - **Description:** `FROM_TO` is a space-joined string that is deliberately expanded unquoted, with SC2086 suppressed. It works today because both values are validated 40-hex SHAs. It breaks silently if a field value ever contains whitespace or glob characters.
  - **Recommended fix:** Use an array: `local -a follow_from_to=()` → `follow_from_to=("from=${BRANCH_SHA}" "to=${MAIN_SHA}")` → `follow_emit "${ACTION}" "${follow_from_to[@]}"`. Then drop both SC2086 disables.

- **TODO/FIXME/HACK markers:** none in `.github/workflows/*.yml`, and no `TODO|FIXME|HACK` comments in `scripts/*.sh` or `scripts/*.py`. No findings.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 2 | EXPR-001, EXPR-004 |
| Medium | 14 | BUG-001, BUG-002, BUG-003, BUG-004, BUG-005, API-001, API-002, DUP-001, DUP-002, DUP-003, EXPR-002, DEBT-001, CONSIST-003, CONSIST-004 |
| Low | 14 | BUG-006, SEC-001, SEC-002, API-003, API-004, DUP-004, DUP-005, DUP-006, EXPR-003, EXPR-005, DEAD-001, CONSIST-001, CONSIST-002, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/workflow-log-analysis.yml` (EXPR-001); size check and possible step extraction for `.github/workflows/review_autofix.yml`, `implement.yml`, `test-and-mark-stable.yml` (EXPR-004) | Small |
| API call optimization | `.github/workflows/orchestrate_poll.yml`, `orchestrate.yml`, `implement.yml`, `scripts/orchestrate_poll_process.sh` | Medium |
| Code modularization | 9 workflows with resolver copies plus 5 with bootstrap copies, new `scripts/workflow_log_analysis_helpers.sh`, `scripts/gh_helpers.sh`, `.github/workflows/orchestrate_poll.yml`, new parity tests under `tests/` | Large |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/promote_candidate_follow.sh`, `.github/workflows/lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `security-audit.yml`, the 12 git-auth steps (SEC-001), `prompts/mode-workflow-audit.txt`, `prompts/_templates/mode-workflow-audit.txt`, `scripts/orchestrate_poll_process.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-11-15)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: every precondition was proven by reading the code. That means the same endpoint and filters, the same function or step, no mutation in between, identical error and retry behaviour, no concurrency or job boundary crossed, and any cache contract kept. The implement stage may act on these directly.
- **`NEEDS_VERIFICATION`**: the change looks sound, but at least one precondition can't be proven from the code alone. A named check has to pass first.
- **`RISKY_SKIP`**: the call sits in a retry, auth or identity, pagination, rate-limit, `orchestrate_poll_process.sh` or race-defence path, or changing it would alter log keys other code reads. It is listed for visibility only and must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

- **MERGE-001** — `NEEDS_VERIFICATION` — **Extends API-001.** It does not restate API-001's fix. It adds a precedent already in this repo, a scope correction and the gates the implement stage needs.
  - **Files and lines:**
    - `.github/workflows/orchestrate_poll.yml:147-188` (read at 166) and `:572-614` (read at 592), both in job `poll`.
    - `.github/workflows/orchestrate.yml:202` (read at 221) and `:376` (read at 395), both in job `orchestrate` (79).
    - `.github/workflows/implement.yml:469-470` (read at 489, `if: … existing_pr == 'true'`), `:772-773` (read at 794, `if: steps.refctx.outputs.pr_not_open == 'true'`) and `:1353-1354` (read at 1373, `if: env.SKIP_IMPLEMENT != 'true'`), all in job `implement` (109).
  - **Current call count:** 2 per job in `poll` and `orchestrate`; 1–3 per job in `implement`. This only applies on the `@refs/heads/promote-candidate` path.
  - **Proposed call count:** 1 per job.
  - **Endpoint:** `GET repos/shubhodeep1/coding-workflows/branches/promote-candidate`.
  - **Evidence:**
    - `validate.yml` already does this consolidation in one job, using step outputs rather than `GITHUB_ENV`. The first step emits `promote_candidate_checked=true` (`validate.yml:228`) and `promote_candidate_sha=…` (`:238`). The second step reuses them:
      ```bash
      # Reuse the SHA "Resolve integration ref" accepted (a step output,
      # which later steps cannot overwrite). Read the branch only when that
      # step never read it (no tracking issue); a rejection there keeps main.
      promote_candidate_sha="${VALIDATE_PROMOTE_CANDIDATE_SHA:-}"
      if [[ ! "${promote_candidate_sha}" =~ ^[0-9a-f]{40}$ ]] && [ "${VALIDATE_PROMOTE_CANDIDATE_CHECKED:-}" != "true" ]; then
      ```
      (`validate.yml:494-498`)
    - So `validate.yml:231`/`:501` is already compliant and should not be counted against API-001.
    - The reads at `clarify.yml:408`/`:2525`, `plan.yml:527`/`:3870`, `validate.yml:1947` and `implement.yml:7310` are in separate `heal-report` jobs (`clarify.yml:2468`, `plan.yml:3826`, `validate.yml:1884`, `implement.yml:7262`). Merging those would cross a job boundary, so they are out of scope here.
    - The two `orchestrate_poll.yml` steps handle errors differently. Step 572 has `continue-on-error: ${{ steps.find_tracking.outputs.has_work != 'true' }}` (`:573`); step 147 has none.
    - Each step emits its own `SUPPORT_REF source=promote_candidate…` line (`:170/172`, `:596/598`). The current report's Reliability item 1 and BUG-002 both plan to extend that prefix.
  - **Proposed fix:**
    - Don't use API-001's `GITHUB_ENV` variable `PROMOTE_CANDIDATE_RESOLVED_SHA`. Follow the `validate.yml` pattern instead.
    - Give the first resolve step in each job an `id:`. Have it write `promote_candidate_checked=true` and `promote_candidate_sha=<40-hex>` to `$GITHUB_OUTPUT`.
    - Pass both values into the later step(s) through `env:`.
    - In the later step, read the branch only when `checked != true`.
    - Keep emitting the `SUPPORT_REF` line in every step, so log consumers still see one line per step.
    - Step outputs can't be overwritten by later steps, whereas `GITHUB_ENV` can be rewritten by any step that runs code staged from the first SHA. That matters because this value picks which code gets executed.
  - **Safety rationale:** the calls cross separate steps that handle errors differently (`continue-on-error` at `orchestrate_poll.yml:573`), and `tests/test_workflow_support_ref_follows_pin.py` asserts the step shape, so the "same error-handling semantics" precondition isn't proven.
  - **Downstream signal (verification required):**
    1. Read `tests/test_workflow_support_ref_follows_pin.py`. Confirm it allows a step output plus a conditional read; `validate.yml` passing it is evidence it does.
    2. In `implement.yml`, confirm whether `existing_pr == 'true'` (469) leads to `SKIP_IMPLEMENT=true` before 1353. If it does, those two steps never both run, and the saving there is only 772 vs 1353.
    3. Confirm no step between the two resolve steps rewrites the `promote-candidate` protection state. It doesn't today: no workflow step writes that branch, and only `scripts/promote_candidate_follow.sh:237-238` does.

- **MERGE-002** — `RISKY_SKIP` — two `rate_limit` probes back to back in the reclarify sweep.
  - **Files and lines:**
    - `scripts/orchestrate_poll_process.sh:191` (`replay_failed_reclarify_commands`).
    - `scripts/orchestrate_poll_process.sh:304` (`detect_unrouted_blocked_comments`).
    - These are called one after the other at `:357-358` under `RECLARIFY_REQUEUE_SWEEP_ONLY=true`, which is set by `orchestrate_poll.yml:972-977`.
    - A third probe runs at job start: `orchestrate_poll.yml:138`.
  - **Current call count:** 2 per sweep tick (3 counting the job-start budget probe).
  - **Proposed call count:** 1 (2 with the job-start probe).
  - **Endpoint:** `GET /rate_limit`.
  - **Evidence:**
    ```bash
    budget="$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null)" || return 0
    ```
    (`:191`)
    ```bash
    budget="$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null)" || budget=""
    ```
    (`:304`)
    - Both compare against the same threshold of 500.
    - Per GitHub's docs, `GET /rate_limit` doesn't count against the REST rate limit ([REST API endpoints for rate limits](https://docs.github.com/en/rest/rate-limit/rate-limit)). Merging therefore saves **zero quota** and only one round-trip of latency.
  - **Proposed fix:** none. If it were ever done: read once in the sweep entry block at `:356-359` and pass the value to both functions.
  - **Safety rationale:** this is a rate-limit probe inside `orchestrate_poll_process.sh`. The two outcomes are logged differently (191 returns silently; 304 emits `RECLARIFY_UNROUTED outcome=skip reason=budget_low remaining=…`), and the second probe also sees calls the replay loop made in between, so merging changes both the log and the gating.
  - **Downstream signal:** do not implement automatically. It saves no quota, and a reused value would let `detect_unrouted_blocked_comments` act on a budget that the replay loop's DELETE/POST calls (`:228`, `:241`, `:245`) have already spent. It needs a manual review of whether a stale budget is acceptable.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001** — `RISKY_SKIP` — the pipeline identity is read in the sweep step and read again in the full poller.
  - **Files and lines:**
    - Sweep step (`orchestrate_poll.yml:972`): `scripts/orchestrate_poll_process.sh:198`.
    - Full poller: the cache lines `:21047-21049`, `:21171-21173`, `:21407-21409`, `:21784-21786` and `:21853-21855`, all guarded by `PERSIST_JOURNAL_LOGIN_CACHE`, which is seeded from the environment at `:21024`.
  - **Current call count:** up to 2 per tick (1 in the sweep when issues are queued, 1 in the full poller when a journal or judge path needs it).
  - **Proposed call count:** 1.
  - **Endpoint:** `GET /user`.
  - **Evidence:**
    ```bash
    trusted_login="$(gh_retry gh api user --jq .login 2>/dev/null)" || trusted_login=""
    ```
    (`:198`, a local variable that is never exported)
    ```bash
    PERSIST_JOURNAL_LOGIN_CACHE="${PERSIST_JOURNAL_LOGIN_CACHE:-}"
    ```
    (`:21024`, which already accepts a value from the environment)
    - The sweep process exits at `:359`, so the login it read never reaches the full poller.
    - The two places validate the login differently: `:199` uses `^[A-Za-z0-9][A-Za-z0-9-]*$`, while `:21049` uses `…(\[bot\])?$`.
  - **Proposed fix (for manual review only):** have the sweep hand its validated login to the later poller step as a step output, not through `GITHUB_ENV`, and fall back to the existing `gh api user` read when it is missing.
  - **Safety rationale:** this is an identity lookup that decides which comment author is trusted to authorize replays and journal entries (`:185-186`, "The marker is data, not authorization"). It sits in `orchestrate_poll_process.sh`, it would cross a step boundary, and the two validation regexes differ.
  - **Downstream signal:** do not implement automatically. A security reviewer would need to approve carrying a trust-defining identity across steps, and settle which regex is authoritative. The saving is at most 1 REST call per tick.

### Dead Calls (DEAD-API-###)

No new findings. The one dead or unreachable API path in scope (`read_standalone_state_json` at `scripts/orchestrate_poll_process.sh:18993-19000`, and the 2-argument fallback in `get_standalone_state_comment_id` at `:18984-18991` / `:19021`) is already covered by API-004 and DEAD-001; see the cross-references below.

I confirmed that no caller passes 2 arguments: all 19 `write_standalone_state_json` call sites (`:25461`–`:26512`) pass 3. I also checked that `orchestrate_poll.yml:329`'s `provider_outage_trackers.json` is consumed (`orchestrate_poll.yml:1219`), so that call is not dead.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — the finding holds, but use the step-output pattern from `validate.yml:228/238/494-498` rather than `GITHUB_ENV`, drop `validate.yml` from the count, and see MERGE-001 for the `continue-on-error` and step-shape gates.
- API-002: RISKY_SKIP — the per-issue GET at `orchestrate_poll_process.sh:202` re-checks the label right before a POST (`:241`) that triggers a pipeline run, in an authorization path (`:185-186`) inside `orchestrate_poll_process.sh`. A list snapshot would weaken that live check, and the comments read at `:205` is paginated.
- API-003: RISKY_SKIP — the per-row GET at `:8822` is in a loop in `orchestrate_poll_process.sh` that ends in a non-idempotent POST (`:8852`) guarded only by a marker. `_fetch_issue_labels_batch_graphql` (`:4778-4791`) returns no `state` and drops issues with an incomplete label page, so it can't replace the GET as it stands.
- API-004: RISKY_SKIP — no runtime change is proposed, and the paginated call is unreachable (all 19 callers pass 3 arguments, verified). The contract test it suggests touches no API call and can go through normal review, but the call itself is in `orchestrate_poll_process.sh` and must not be edited automatically.
- DEAD-001 (Section 5, API-related): RISKY_SKIP — the function has no callers (verified), but §10 naming immutability means it is kept; the only safe action is the contract test noted under API-004.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-001 |
| RISKY_SKIP | 2 | MERGE-002, REUSE-001 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass. MERGE-001 is the highest-value candidate once its three checks pass.