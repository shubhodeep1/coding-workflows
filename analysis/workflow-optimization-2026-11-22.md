## Executive Summary

- **Data gaps first.** The repository `shubhodeep1/coding-workflows` is **truncated** (`coverage.page_limit_reached=true`), so only part of its runs are listed. The full-log folder `/home/runner/work/_temp/workflow-log-output` was **not mounted** in this sandbox. As a result, none of the 6 failing runs has error text: there are no `errors/`, `slow/` or `recent/` logs and no `log_excerpts` in the context. The claims below rest on the context JSON, the `log_summary` fields and the current code. Confidence on root causes of the failures is **low**.
- **Failing-run rows reach the analysis with no error text (R1).** `_normalized_run_view` in `scripts/analyze_workflow_logs.py` (lines 222–253) drops `log_excerpts`. Those excerpts are also the head of each step log (`text[:max_chars]`, `collect_workflow_logs.py:785`), where the error usually isn't. All 6 failures in this window had `log_download_status=success`, but none of their errors can be cited here. The fix is to carry up to 3 sanitized `##[error]` lines from the failing step. **Impact:** every failure becomes diagnosable without the mounted archive. **Confidence: high.**
- **The same CI step failed twice in about 4 hours, with no per-module attribution (R2).** `Review autofix review-pipeline plumbing contract test` in job `orchestrate-poll (1)` failed in 45000954628 (pr:9923) and 45018554453 (pr:9952). It also failed in 43112699857 on 2026-11-12. The step (`ci.yml:4220-4235`) chains 12 modules under `set -e` with no `CI_SUBTEST_END` markers. Triage for 45018554453 skipped (`CHECK_TRIAGE skip reason=routed_head_base`, run 45024735149). **Impact:** names the failing module directly and gives the per-module timings that the four rebalancing moves in the step's comments were missing. **Confidence: medium.**
- **Successful Claude-engine triage runs log a false `##[error]` (R3).** `check_failure_triage.yml:618-620` keeps only `claude-pool-token` under `.codex-workflow-src/.github/actions`. But `install-claude` (used at `:439`) wraps `actions/cache@v5`, which has a post step. The result is "Can't find 'action.yml' … install-claude" on run 45024735149. The fix is one line. **Impact:** removes error annotations that could mislead heal or triage scanners. **Confidence: high.**
- **The `issue_comment` fan-out still dominates run volume, and whether comment signing is on can't be seen (P1).** Clarify, plan, implement and clarify-respond had 334 skipped runs out of 370. The recent sample shows 12 bursts of 4 wrappers at identical timestamps within 53 minutes, for example 45025049385/262/272/289 at 12:58:53. The opt-in signing in `_ai_comment_sig_activate` (`gh_helpers.sh:49-51`) returns silently when `AI_COMMENT_SIGNING_SECRET` is unset. The fix is to log one `AI_COMMENT_SIG outcome=inactive reason=secret_unset` line per job. **Impact:** makes a possible saving of about 90% of wrapper runs auditable. **Confidence: medium.**
- **GH API attribution blind spots (G1, O1).** All 5 sampled heal-intake runs report `GH_PAT_BUDGET … used_in_job=unknown`. The likely cause (an inference) is that the start snapshot reads the reset time from the response body before checkout, while `gh_pat_budget end` reads it from the response headers. Separately, `AUTOFIX_AUTO_MERGE_CHECKS outcome=timeout waited_s=0 pending=14 ci_state=absent` (45022677198, PRs #9943 and #9952) comes from one of two code paths that both lack a `reason=` field. **Impact:** low-risk diagnostic fixes. **Confidence: medium.**
- **Cost and cache are stable, with no new safe lever.** 70 OpenRouter calls, 32,540,608 tokens and `cache_hit_rate` 0.722049 (0.7346 on 2026-11-18). There was 1 `CONTEXT_BUDGET_WARN` (45004086477) and 0 `BREAK_GLASS`. Semble made 13 queries for 155,747 bytes with 0 duplicate bytes. Every cost lever raised before has been rejected in the processing report. **Confidence: high.**

## Speed Optimizations

The dominant latency is the hosted-runner queue. Sampled log summaries show waits of 5–16 minutes for jobs whose real work takes 4–13 seconds. Examples:
- `PR checks` 45020191620: about 16m40s queued, about 8s of steps.
- `PR checks` 45023428093: about 15m queued.
- `CI quick` 45019685011: 14m58s queued out of 1,424s.
- `Security Audit` 45019569361 and 45019567396: about 14.5m each.
- Judge dispatch 45021974048 and 45021825118: about 10m each.

There are also review_autofix bursts: 9 runs created between 09:02:48 and 09:03:05 (45005615285 through 45005637956), each lasting 5,118–8,389s.

Two earlier proposals on this were rejected in the processing report: "Speed 1 (pace review dispatch bursts)" (2026-10-07-2: medium risk, no saving established) and "queue-time separation" (2026-10-08). This window doesn't measure any saving that would answer those reasons. The per-PR check fold (pr-checks.yml, 2026-11-18) and the readiness label filter (item 10) are already on this ref. So no critical-path speed fix passes the findings check.

**S1 (micro, budget-planning): per-module timing for the plumbing step.**
- **Evidence:** the step's own comments (`ci.yml:4212-4219`) say "The run's step timings could not be read, so the pick is an estimate". It has been moved four times between 20-minute jobs.
- **Change:** the same change as R2. `CI_SUBTEST_END … elapsed_ms=` per module makes the next rebalancing a measurement rather than an estimate.
- **Estimated saving:** none directly. It prevents misplaced moves that cost a budget-watch cycle.
- **Risk:** low.

Finding check: reopened — failures 45000954628 and 45018554453 in this window (plus 43112699857 on 2026-11-12) answer the 2026-10-07-3 "Reliability 1 (CI guard-test diagnostics) — rejected: no failing assertion was demonstrated"; the cause is present at `ci.yml:4220-4235`.

## Cost Optimizations

**Review spend (OpenRouter, 9 `review_autofix` runs with usage):**
- 70 calls, all with usage reported.
- Prompt 6,422,054 tokens, completion 518,298, cache read 23,123,532, cache write 2,479,295, total 32,540,608. That is about 3.62M tokens per run.
- `cache_hit_rate` 0.722049, with a per-run range of 0.395084 (44999140865, pr:9931, 6 calls) to 0.844239 (45007998214).
- The heaviest run was 45004086477: 10 calls, 1,549,428 prompt tokens, 6,165,295 total, cache hit 0.652038, and the window's only `CONTEXT_BUDGET_WARN`. That points to prompt-size pressure in one run, not a systemic pattern.

**Semble:**
- 13 queries for 155,747 bytes (about 12.0 KB per query) and 73 sources.
- `static_dup_bytes` 0 and `echo_lines_dropped` 0, so Semble is not adding duplicate context.
- 13 bootstraps (0 failed, 0 unused), 699,462 ms in total (about 53.8 s each). That is under 1% of a 2-hour review run.
- The judge runs 45021974048 and 45021825118 each logged 2 bootstraps for 2 queries (104,762 ms and 111,040 ms). `scripts/semble_helpers.sh:180-200` reuses a state file within a process tree. Whether these were separate jobs can't be checked without logs (unverified, so no recommendation).

**Serena:** 0 queries, 0 response bytes, 0 tool calls. `SERENA_ENABLED: false` in the judge `log_summary`.

**Candidates dropped:**
- Prompt reduction or a circuit breaker at `CONTEXT_BUDGET_WARN`: rejected 2026-09-21-3 Cost 3 and 2026-09-16 Cost 4.
- Reasoning or tier experiments: rejected 2026-10-07-2 Cost 1.
- Semble query parallelism: rejected Speed 5.

No cost recommendation passes the findings check in this window.

## Reliability Improvements

**R1. Carry the failing step's error lines into the analysis context.**
- **Failure evidence:** all 6 failures (45005973368, 45018554453, 45000954628, 44995866215, 44997621624, 45005485600) have `log_download_status=success` but no error text in `analysis_context.json`, and the deep-dive folder was not mounted.
  - `_normalized_run_view` (`scripts/analyze_workflow_logs.py:222-253`) copies `log_summary`, `cost_telemetry` and the diagnostic statuses, but not `log_excerpts`.
  - `extract_log_excerpts` (`collect_workflow_logs.py:771-788`) keeps the head of each step log, and a failure's `##[error]` is usually at the tail.
- **Root cause category:** observability gap in the collector and analyzer.
- **Exact fix:**
  - In `scripts/collect_workflow_logs.py`, for `conclusion == "failure"` rows with full logs, scan the `failure_point.step_name` log from the tail for lines containing `##[error]`.
  - Keep at most 3 lines, each passed through `_sanitize_diagnostic_text` and cut to 300 characters, as an additive `failure_error_lines` list.
  - Add the key to the cached-row passthrough at `:925`.
  - Copy it in `_normalized_run_view`:

```python
	failure_error_lines = run.get("failure_error_lines")
	if isinstance(failure_error_lines, list):
		bounded = [str(line)[:300] for line in failure_error_lines if isinstance(line, str) and line.strip()][:3]
		if bounded:
			view["failure_error_lines"] = bounded
```

- **Expected impact:** for example, it would show which `IMPLEMENT_*` guard in `scripts/implement_commit_changes.sh` refused 44997621624, and why `Run Codex planning` failed after 2,602s in 45005485600.
- **Rollback / fail-open:** purely additive. A missing log yields no field. PR-controlled text is bounded, sanitized, and treated as data, the same trust level as `log_summary`.

Finding check: new — the 2026-10-08 verdicts rejected restoring the archive ("runtime artifact state") and CI first-failing-test logging ("data gap was the absent log archive"). This change makes analysis independent of the archive. The cause is present at `analyze_workflow_logs.py:222-253`.

**R2. Per-module `CI_SUBTEST_END` in `Review autofix review-pipeline plumbing contract test`.**
- **Failure evidence:** CI [pr:9923] 45000954628 (08:02) and CI [pr:9952] 45018554453 (11:41), both at `orchestrate-poll (1)`. Also 43112699857 (2026-11-12). Each failing run also logged 4 `SEMBLE_FALLBACK` lines classified `context=contract-test`, which are expected test noise.
- **Root cause category:** the failing module can't be attributed. 12 modules run in sequence under `set -euo pipefail` (`ci.yml:4222-4235`), so `failure_point` stops at the step name and later modules never run.
- **Exact fix:** reuse the existing pattern at `ci.yml:4069-4077`. Run each listed command, echo `CI_SUBTEST_END subtest=<path> elapsed_ms=<n> rc=<rc>`, continue, and fail at the end naming every module with `rc != 0`. Keep the step name and `if:` unchanged (§10). Check `wc -c` on `ci.yml` (§24).
- **Expected impact:** failures are named per module, and the same lines give the timings S1 needs.
- **Rollback:** revert the step body. Behaviour changes only on failure: later modules still run, matching the existing single-file step.

Finding check: reopened — prior verdict 2026-10-07-3 "Reliability 1 … rejected: no failing assertion was demonstrated". This window shows 2 failures of this exact step on different PRs, and automated triage skipped one of them (45024735149 `reason=routed_head_base`).

**R3. Keep the `install-claude` action directory through the check-triage cleanup.**
- **Failure evidence:** 45024735149 (success, 200s) logged `##[error]Can't find 'action.yml'... under '.codex-workflow-src/.github/actions/install-claude'` in post-job cleanup.
- **Root cause category:** code defect. `check_failure_triage.yml:620` deletes every action directory except `claude-pool-token`. But `install-claude` (`:439`) runs `actions/cache@v5` (`.github/actions/install-claude/action.yml:31-35`), whose post step needs the `action.yml` at job end.
- **Exact fix:**

```bash
            find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +
```

  Also update the comment at `:613-615`.
- **Expected impact:** no false `##[error]` annotation on successful Claude-engine triage runs. On a cache miss, the npm cache is saved again.
- **Rollback / fail-open:** the directory holds one composite `action.yml` that grants no credential (its own description says so). The sandbox snapshot still excludes `.codex-workflow-src` (`:615`).

Finding check: new — raised as R3 in `analysis/workflow-optimization-2026-11-19-2.md`, which the processing report has not reached yet (its last entry is 2026-10-08). The cause is present at `check_failure_triage.yml:620`.

**R4. Tell an out-of-memory kill from a timeout kill in the pre-push test gate.**
- **Failure evidence:** the PR #9956 body (PR checks 45022732784) reports the gate `unverified` with `gate: container_rc_137 (likely OOM kill)`, 0 tests run, and every acceptance row "not run".
- **Root cause category:** an ambiguous kill. `scripts/prepush_test_gate_run.sh:434-442` runs with `--memory 4g` under `timeout --kill-after=30s "${wall}"`, so rc 137 can be either. `synthetic_result` (`:98-106`) records only `reason=container_rc_137` and `elapsed_secs: 0`.
- **Exact fix:**
  - Record `gate_start_s="$(date +%s)"` before `docker run`.
  - In the `rc -ne 0` branch, call `synthetic_result "container_rc_${rc}" "elapsed_s=${gate_elapsed_s} wall_s=${wall}"`.
  - Let `synthetic_result` append an optional second argument to its `gate_log` line, after `reason=`.
  - The `reason` value and `result.json` stay unchanged.
- **Expected impact:** an elapsed time below the wall points to memory; one at the wall points to timeout. That decides between raising `--memory` and narrowing test selection.
- **Rollback:** additive log fields only.

Finding check: new — no prior verdict on gate kill classification (the only pre-push entry is 2026-10-02 inventory parity); the cause is present at `prepush_test_gate_run.sh:452-454`.

**Not raised:**
- `Resolve trusted review support commit` (44995866215): one fail-closed occurrence, and later pr:9898 review runs 45003366917, 45010934757 and 45018630173 succeeded. The prior verdict was already-implemented (bounded `gh_api_retry`, deliberate `exit 1`).
- `Alert inbox capture and store tests` (45005973368): no workflow on this ref has that step. Its cause is unverified and is listed only in the Metrics Appendix data gaps.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines are visible. The deep-dive excerpts are unavailable, and every `log_summary` that mentions it reports none in the visible portion. Most of those logs were truncated by 13k to 1.3M characters (for example 45021825118 and 45019569361). So retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, and `fail_open` / `enabled:false` counts **cannot be computed** for this window.

**M1. Count existing `AI_MEMORY_TELEMETRY` retrieves in `cost_audit.py`.**
- **Evidence:** `scripts/cost_audit.py` has no `ai_memory` handling (0 matches). Its per-run telemetry (`RUN_COST_TELEMETRY_FIELDS`, `AGGREGATABLE_COST_FIELDS`, the initial dict and the two aggregation key lists at about lines 1024–1045 and 1071–1093) covers OpenRouter, Semble and Serena but not memory.
- **Exact change:** parse `AI_MEMORY_TELEMETRY:` JSON lines with `op == "retrieve"` into additive counters `ai_memory_retrieve_calls`, `ai_memory_retrieve_hits` (`records_selected > 0`) and `ai_memory_fail_open`. Add them to all four places, following the `semble_*` pattern. Emitters are unchanged.
- **Expected impact:** memory hit rate reaches this report through `cost_telemetry`, like `cache_hit_rate` does, even when logs are truncated or unmounted.
- **Risk:** low. Additive and parser-only.

Finding check: reopened — prior verdicts rejected AI Memory Health asks as "observability with no consumer" (for example 2026-09-2x, line 1238 of the report). This window shows the consumer, this report's required section, got zero data from 102 telemetry-parsed runs, because the lines exist only in logs that weren't mounted.

## GH API Call Audit

| Workflow / job | Signal | Value | Run |
|---|---|---|---|
| review_autofix / codex-agent (judge dispatch) | `GH_PAT_BUDGET used_in_job` | 868 | 45021974048 |
| review_autofix / codex-agent (judge dispatch) | `GH_PAT_BUDGET used_in_job` | 911 | 45021825118 |
| review_autofix_sweep / sweep | `GH_PAT_BUDGET used_in_job` | 67 | 45022637752 |
| workflow-failure-heal-intake / intake | `used_in_job=unknown` (remaining 4799, 4760, 4704, 4562, 1591) | 5 of 5 runs | 45021813847, 45022483828, 45023285339, 45024545393, 45024659145 |
| promote-candidate follower | `GH_API_RETRY outcome=permanent status=404` (tag probe, expected) | 1 | 45021974322 |
| PR checks / pr-body-lint | one paginated `pulls/<n>/commits` read | 1 per run | 45023428093, 45022978839, 45021662957 |
| cancel-on-PR-close | `MERGE_TRAIN_RELEASE_SUMMARY … active_listing_calls=0 elapsed_ms=18087` | 0 calls | 45022677198 |

Notes on the table:
- The two judge figures overlap in time (12:31–12:52) and share one PAT. `gh_pat_budget` warns that counters "are only an estimate of job usage: other jobs may run concurrently" (`gh_helpers.sh:968-969`). Their sum (1,779) overstates the judge's own usage.
- The bucket with reset `1795353500` fell from 3,165 (12:38) to 1,591 (about 12:57), with about 21 minutes left before reset. No 429s were seen.
- Per-endpoint counters for the judge job were rejected repeatedly ("`GH_PAT_BUDGET … used_in_job` already gives per-job usage"), so that item is dropped.

**G1. Make heal-intake `used_in_job` measurable.**
- **Evidence:** 5 of 5 intake runs report `unknown`. In `workflow-failure-heal-intake.yml`:
  - The start step (`:199-210`) runs before `Checkout repository` (`:212`). It writes `.resources.core.reset` from the body of `gh api rate_limit`.
  - The end step (`:375-385`) calls `gh_pat_budget end`, which prefers the `x-ratelimit-reset` header (`gh_helpers.sh:989-998`) and requires an exact reset match (`:1008`).
  - Reset values 5 s apart appear across runs in the same hour (`…500` and `…505`).
  - The sweep, which uses `gh_pat_budget` for both start and end after checkout (`review_autofix_sweep.yml:143-155`), measures fine (67).
  - That the body/header mismatch causes the `unknown` is an inference.
- **Exact change:** move the three start steps (intake, recovery-scan, heal-verify at `:199`, `:499` and `:644`) to just after each job's checkout. Use the sweep's shape (`source scripts/gh_helpers.sh` then `gh_pat_budget start …`, with the existing `unknown` echo as fallback). Step names and `GH_PAT_BUDGET_FILE` paths stay unchanged.
- **Expected call reduction:** none. This restores attribution for a heal family with 51 runs in the window.
- **Rate-limit risk:** unchanged, since `/rate_limit` doesn't debit core.

Finding check: new — `used_in_job=unknown` was raised in 2026-10-21-5, 2026-10-22 and 2026-10-23, none of which the processing report has reached yet; the cause is present at `workflow-failure-heal-intake.yml:199-212`.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.722049 across 70 calls (0.734551 on 2026-11-18). Cache writes were 2,479,295 tokens against 23,123,532 reads, about 9.3 reads per write.
- **Fragmentation outliers:** 44999140865 at 0.395 (cache write 167,163 tokens against read 320,263, 6 calls) and 45009197739 at 0.611. Without logs, the cause can't be separated into prefix churn or a cold cache on new heads.
- **`CONTEXT_BUDGET_WARN`:** 1, in 45004086477. That run also had the highest prompt volume (1.55M uncached prompt tokens) and a below-average hit rate of 0.652. This is consistent with prompt growth eroding cache value in that one run (an inference).
- **Memory retrieval:** see AI Memory Health (no data).

Candidates dropped: prefix hashing, reordering, a 70% cap and component budgets were all rejected earlier (2026-09-21 to 2026-10-08). `REVIEWER_PROMPT_COMPONENTS` already carries `static_prefix_sha256`. No new item passes.

## Orchestrator Health

What the window shows:
- **Poller:** 33 runs, 13 success, 20 cancelled, 0 failed. Visible cancellations are `cancelled_before_first_step` after 57–1,491 s queued (45019263927, 45019339362, 45021388451, 45023164672). That is normal concurrency-group replacement. Cancellation diagnostics were rejected earlier.
- **Merge lease:** `MERGE_LEASE pr=9283 … outcome=held reason=holder_open`. PR #9872 is behind it (`MERGE_BASE_FRESHNESS outcome=overlap behind_by=15`, `FINAL_CI_MERGE_RESUME outcome=deferred reason=lease_wait`) in 45022677198.
- **Stale queued runs:** the same run preserved 3 queued runs without PR linkage, the oldest (32215723558) queued since 2026-08-19. Ghost-run cleanup is a non-goal of `docs/plans/reduce-actions-run-volume-plan.md`.
- **Validate:** 3 of 3 succeeded. **Unblock-judge dispatch:** 5 of 5 succeeded.

**O1. Add `reason=` to the two `AUTOFIX_AUTO_MERGE_CHECKS outcome=timeout` lines that lack it.**
- **Evidence:** 45022677198 logged `AUTOFIX_AUTO_MERGE_CHECKS outcome=timeout waited_s=0 pending=14 ci_state=absent` (PR #9943) and `pending=4` (PR #9952). Both were followed by `FINAL_CI_MERGE_RESUME outcome=waiting reason=required_checks_timeout`.
  - In `scripts/pr_checks_lib.sh`, lines 1175 and 1197 carry `reason=`, but 1220 (required CI check not yet `success`) and 1270 (budget spent with pending check-runs) don't.
  - So you can't tell whether CI never registered or other checks were still running. PR #9952's CI [pr:9952] 45018554453 had already failed.
- **Exact change (additive, placed before `ci_check=` so the "ends with `ci_check= ci_state=`" contract at `:778-779` holds):**
  - line 1220: `… pending=${PR_CHECKS_LAST_PENDING:-0} reason=required_ci_check_${PR_CHECKS_LAST_CI_STATE:-unknown} ci_check=…`
  - line 1270: `… pending=${PR_CHECKS_LAST_PENDING:-0} reason=required_checks_pending ci_check=…`
- **Indicator to track:** count of `reason=required_ci_check_absent` per sweep tick. A steady count means heads whose final CI never registers.
- **Risk:** low. Parsers already accept `reason=` on the sibling lines.

Finding check: new — no prior verdict on these two emitters; the cause is present at `pr_checks_lib.sh:1220` and `:1270`.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact:
1. **Queueing (dominant, account-wide).** Small jobs waited 2–16 minutes for a runner (see Speed). The review/autofix stage is the heaviest runner consumer: 219 runs, p50 2,096 s, p95 6,910 s, with bursts of 4–9 runs created within seconds. Throttling those bursts was rejected earlier.
2. **Compute in review/autofix.** About 3.62M tokens per review run. PRs re-reviewed repeatedly: #9898 four times (44995866215, 45003366917, 45010934757, 45018630173) and #9359 three times (44994556235, 45000883035, 45009208726).
3. **Retry and merge overhead.** The lease wait (#9872 behind #9283), required-check waits (#9943, #9952), and `ai:acceptance-unmet` on 5 sampled PRs (#9954, #9510, #9953, #9933, #9935).
4. **Trigger volume.** 513 of 999 runs were skipped or "other": the `issue_comment` wrappers (334 of 370), `workflow_failure_heal` on every label event (112 of 116) and `integration_pr_readiness` (30 of 30). Skipped runs take no runner but flood the queue and API (§26). A single router workflow is an explicit non-goal of the run-volume plan.

**P1. Make the comment-signing opt-in state observable.**
- **Evidence:** 12 four-wrapper bursts between 12:05:57 and 12:58:54, all skipped, for example:
  - 45020534098/122/124/138
  - 45021740229/238/369/375
  - 45025049262/272/289/385
- **The gap:** item 15 of the run-volume plan only stops PAT comments from starting wrappers when `AI_COMMENT_SIGNING_SECRET` is set. `_ai_comment_sig_activate` returns silently when it is unset (`gh_helpers.sh:51`), but already logs `AI_COMMENT_SIG outcome=inactive reason=python3_or_jq_missing` for another inactive case (`:75`).
- **Exact change:** on the unset branch, emit `AI_COMMENT_SIG outcome=inactive reason=secret_unset` once per job, as an info line (not `::warning::`). Guard it with a new env var `AI_COMMENT_SIG_INACTIVE_LOGGED` (default unset), exported and appended to `GITHUB_ENV`. Only print when `GITHUB_ACTIONS` is set.
- **Expected impact:** one grep shows whether the about-90% skipped-wrapper volume is avoidable by setting the secret, without reading secret state.
- **Risk:** low. Tests that pin `gh_helpers.sh` stderr may need the line allowed. The hardened triage job will log `secret_unset` by design (`:46-47`).

Finding check: new — `analysis/workflow-optimization-2026-11-19.md` raised the secret state as unverified, but the processing report hasn't reached that doc yet; the silent return is present at `gh_helpers.sh:51`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - Hosted-runner queue waits of 5–16 min on small jobs.
  - review_autofix p95 of 6,910 s across 219 runs.
  - Poller runs replaced while queued (20 of 33 cancelled).
- **Top failure modes:**
  - The CI plumbing contract step failing twice in this window.
  - Implement `Commit changes` (44997621624, 3,650 s).
  - Plan `Run Codex planning` (45005485600, 2,602 s).
  - The review gate trusted-commit step (44995866215), which is fail-closed by design.
  - The CI `Alert inbox capture and store tests` step (45005973368), which is not on this ref.
  - Overall: 6 failures out of 999 runs (0.6%).
- **Highest-cost drivers:**
  - Review runs at about 3.62M OpenRouter tokens each, 32.5M in total.
  - Judge dispatch jobs that saw 868–911 PAT calls each during overlapping windows.
- **Top 3 prioritized actions:**
  1. R1: carry sanitized `failure_error_lines` from collector to analyzer, so every failure is diagnosable without the archive.
     Finding check: new — the cause is at `analyze_workflow_logs.py:222-253` and `collect_workflow_logs.py:785`; it answers the 2026-10-08 absent-archive rejections with an archive-independent change.
  2. R3: keep `install-claude` in the check-triage cleanup.
     Finding check: new — evidence run 45024735149; the cause is at `check_failure_triage.yml:620`; raised in the unprocessed 2026-11-19-2 report.
  3. P1: log `AI_COMMENT_SIG outcome=inactive reason=secret_unset` once per job.
     Finding check: new — 334 of 370 wrapper runs skipped; the silent return is at `gh_helpers.sh:51`.

## Metrics Appendix

**Runs**

| Scope | Runs | Success | Failure | Cancelled | Other / skipped | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| Repo total (truncated) | 999 | 456 | 6 | 24 | 513 | 10 | 5,208 |
| review_autofix | 219 | 216 | 1 | 1 | 1 | 2,096 | 6,910 |
| ci | 65 | 59 | 3 | 3 | 0 | 1,119 | 4,875 |
| pr_checks | 49 | 49 | 0 | 0 | 0 | 272 | 1,393 |
| orchestrate_poll | 33 | 13 | 0 | 20 | 0 | 799 | 2,577 |
| workflow_failure_heal_intake | 51 | 32 | 0 | 0 | 19 | 126 | 912 |
| workflow_failure_heal | 116 | 4 | 0 | 0 | 112 | 1 | 11 |
| clarify / plan / implement / clarify_respond | 100 / 90 / 90 / 90 | 10 / 10 / 10 / 4 | 0 / 1 / 1 / 0 | 0 | 90 / 79 / 79 / 86 | 1 | 476 / 1,098 / 3,240 / 11 |
| security_audit | 13 | 13 | 0 | 0 | 0 | 578 | 1,710 |

**Tokens, cache and review telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 70 (70 / 0) |
| Prompt / completion tokens | 6,422,054 / 518,298 |
| Cache read / cache write tokens | 23,123,532 / 2,479,295 |
| Total tokens | 32,540,608 |
| `cache_hit_rate` | 0.722049 (per run 0.395084–0.844239) |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo, 101 samples) | 315,000 / 8,389,000 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix, 16 samples) | 7,198,500 / 8,482,500 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 1 (45004086477) |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 13 / 155,747 (about 12.0 KB per query) |
| `semble_sources_total` / `static_dup_bytes` / `echo_lines_dropped` | 73 / 0 / 0 |
| Bootstraps / failed / unused / total ms | 13 / 0 / 0 / 699,462 (about 53.8 s each) |
| `semble_fallbacks` (contract-test / runtime) | 14 (12 / 2): contract-test 4 each in 45005973368, 45018554453 and 45000954628; runtime 2 in implement 44997621624 |
| Serena query / fallback / response bytes / tool calls | 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API:** see the GH API Call Audit table. PAT `used_in_job` values: 868, 911 and 67, plus 5 unknown. No HTTP 429s and no secondary rate-limit hits were visible.

**Findings check**

| Status | Count |
|---|---|
| new | 6 (R1, R3, R4, G1, O1, P1) |
| recurring | 0 |
| reopened | 2 (R2/S1 as one change, M1) |
| unverified | 0 |
| dropped: already fixed on this ref | 3 (per-PR checks fold, readiness label-only runner, `cancelled_before_first_step` classification) |
| dropped: previously decided | 10 (review-burst pacing, queue-time split, judge per-endpoint API counters, prompt reduction at budget warn, cache prefix/ordering, poller cancellation diagnostics, trusted support commit retry, Semble parallelism, Semble contract/runtime split, `issue_comment` router as a plan non-goal) |

**Data gaps**
- `repositories_truncated`: shubhodeep1/coding-workflows (page limit reached).
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` doesn't exist in this sandbox, so there were no `errors/`, `slow/` or `recent/` logs.
- Root causes of all 6 failures are unknown. The `Alert inbox capture and store tests` step (45005973368) doesn't exist in any workflow on this ref.
- `success_sample_rate` 0.07, with 1 sampled success run. Most `log_summary` entries are truncated (13k–1.3M characters cut).
- The processing report could be read; its last processed doc is 2026-10-08, so later reports (2026-10-21 through 2026-11-21) have no verdicts yet.