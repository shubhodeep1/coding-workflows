## Executive Summary

> **Data-coverage caveat (read first):** The run-logs directory `/home/runner/work/_temp/workflow-log-output` was **not mounted** in this sandbox, and no `workflow_log_report.json` exists in the repo. So no `errors/`, `slow/` or `recent/` step logs, and no `log_summary` fields, could be read. Every finding below rests on the `analysis_context.json` rows plus the repo source (`.github/workflows/ci.yml`, `scripts/collect_workflow_logs.py`, `scripts/cost_audit.py`). `insufficient_data` is `false`, but the 998-run window covers only about 2h10m (first run created 22:32:39Z, last 00:42:56Z on 2026-11-11/12). That matches the collector's default page limit, explained under GH API Call Audit.

- **CI is red on 9 of its 10 runs (90%), and 7 of the 9 fail at one step.** The step is `tests-hooks-and-orchestrator` → `Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)` (runs 43114013218, 43115476765, 43115959904, 43116204712, 43116239930, 43116887837, 43117196090). The step itself failed; a job timeout did not cut it off. `extract_failure_point` (`scripts/collect_workflow_logs.py:1069-1076`) only names a step whose own conclusion is `failure`, and a timed-out step is marked `cancelled`. The failed CI runs add up to about 12.0 h of wall time (43,347 s). **Impact: very high. Confidence: high** on where it fails, **low** on the cause, because no logs were available.
- **Runner queueing, not compute, drives every long duration.** `Lint PR body…` has `timeout-minutes: 5` (`lint-pr-body-auto-close.yml:47`) but averaged 1,251 s, with a maximum of 1,755 s (run 43122666192). So each of those runs spent at least 20 minutes waiting outside job execution. CI jobs are capped at 20 min (`ci.yml:548` etc.), yet failed CI runs lasted 3,293–5,902 s. **Impact: high (≈60+ min added per CI run). Confidence: medium-high (inference from timeout ceilings).**
- **There is a feedback loop: CI failures → check-failure triage → more runner contention.** 15 `check_failure_triage` runs had p50 3,380 s and p95 4,288 s, and they overlapped the CI failure burst between 22:32 and 23:23. **Impact: high. Confidence: medium (inference).**
- **Skipped-run fan-out is 88.9% of all runs** (887 of 998). Each issue comment starts clarify (211 skipped), implement (205), plan (200) and orchestrate_clarify_respond (207), and all of them skip at the job-level `if:` (`internal-clarify.yml:17-24`). The skipped runs use up the collector's 1,000-run listing budget and its deep-dive selection: 15 of the first 16 `recent_runs` were skipped runs with `log_download_status: empty_archive`. **Impact: medium (observability and API waste). Confidence: high.**
- **Token and cost telemetry is effectively missing.** Across 115 parsed runs, `codex_calls`, `or_calls` and all token fields are 0, and `cache_hit_rate` is null. That includes a 7,080 s `Internal: AI Implement` run (43114089699) and two triage runs of 4,582 s and 4,162 s. No cost optimisation can be quantified this window. **Confidence: high that this is a gap, not zero spend.**
- **Semble did no work at runtime in the one sampled implement run.** It logged 0 queries and 0 bootstraps but 4 runtime fallbacks (run 43114089699). The 40 contract-test fallbacks (exactly 4 per CI run × 10) are expected test output. **Impact: medium. Confidence: low (n=1).**

## Speed Optimizations

Ranked by expected latency reduction.

1. **Fix the merge-train step regression (critical path).**
   - **Evidence:** 7 CI runs failed at the same step within 22:42–23:06Z. The next CI run, 43117264418 at 23:06:48Z, succeeded.
   - **Root cause:** a real test failure in `tests/test_review_merge_train.py` or one of the `test_gh_*` modules (`ci.yml:845-929`). Which shard failed cannot be determined without logs.
   - **Change:** pull the `--- merge-train step shard <id> failed; last 40 log lines:` block from the `errors/` logs of these runs and fix the failing test or the regression that broke it. Also add the structured result line described under Reliability, item 1.
   - **Savings:** every avoided red CI run saves about 1–1.6 h of wall time, plus one triage run of roughly 56 min p50.
   - **Risk:** low.
2. **Cancel superseded pull_request CI runs.**
   - **Evidence:** `ci.yml` has no `concurrency:` block (lines 1-21; grep for `concurrency` in `ci.yml` finds none). The workflow fans out to about 12 jobs per run (static-checks, 4× `tests-*`, a 5-way `orchestrate-poll` matrix, `budget-watch`, `lint`). 10 CI runs started within 34 minutes.
   - **Root cause:** stale PR heads keep their runner slots.
   - **Change (keeps push runs uncancelled, because triage needs base-branch results per the comment at `ci.yml:5-12`):**
     ```yaml
     concurrency:
       group: ci-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || format('push-{0}', github.run_id) }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
   - **Savings:** cannot be quantified yet, because the collector does not record PR number or head SHA per run. If half the PR runs are superseded, about 6 jobs per superseded run go back to the pool.
   - **Risk:** low to medium.
   - **Preflight:** confirm that `check_failure_triage` and the merge train ignore `cancelled` CI conclusions.
3. **Damp triage fan-out while CI on the base branch is red.**
   - **Evidence:** 15 triage runs, p50 3,380 s, all within the failure burst.
   - **Change:** this is an inference. Before dispatching, check whether the base branch's most recent CI conclusion is already `failure` on the same step, and dedupe per step-signature. Reuse the run-list data the triage already reads (§14).
   - **Savings:** up to about 50% fewer triage runs during a red-base burst.
   - **Risk:** medium (could hide PR-specific failures; key the dedupe on step and failing-test signature).
4. **Micro-optimisation: the poller cadence.** `orchestrate_poll` runs took 523 s p50 and 2,598 s p95, against a `*/5` cron (`internal-orchestrate-poll.yml:5`). This is a consequence of the contention above. Fix items 1–3 before tuning the poller.

## Cost Optimizations

Ranked by expected savings; all of it is blocked by missing telemetry.

1. **Restore token telemetry before any model or reasoning-level changes.**
   - **Evidence:** `codex_tokens_used`, `or_total_tokens`, `or_cache_*` are 0 and `cache_hit_rate` is null on all 115 parsed runs, including AI-heavy runs 43114089699 (implement, 7,080 s), 43119447129 (triage, 4,582 s) and 43117881850 (triage, 4,162 s).
   - **Root cause (inference):** this deployment runs the Claude engine, which `cost_audit.py` counts under the separate `CLAUDE_COUNT_KEYS` and `claude_cost_usd` (lines 1094-1097). Those fields are absent from every `cost_telemetry` row in `analysis_context.json`.
   - **Change:** make sure the analysis-context builder copies the `claude_*` fields and `claude_cost_usd` into each per-run and per-family `cost_telemetry` (an additive field).
   - **Savings:** enables the analysis; no direct savings.
   - **Quality risk:** none.
2. **Avoidable reruns.** The 9 failed CI runs and their triage runs are the largest compute cost visible in this window: about 43k s of CI wall time plus about 50k s of triage wall time (15 × 3,380 s p50, upper bound). Fixing Speed item 1 is the cost fix.
3. **Semble.** There were 0 `SEMBLE_QUERY` calls and 0 logged bytes, so Semble is neither reducing nor inflating prompts this window. Its value cannot be assessed until runtime queries appear. **Serena:** no `SERENA_*` lines at all, so there is nothing to assess.

## Reliability Improvements

Ranked by expected reduction in failure or rerun rate.

1. **Merge-train step failures: 7 of 10 CI runs (70%).**
   - **Root cause category:** a test regression or flake (unknown; logs unavailable).
   - **Fix:** as Speed item 1, plus a diagnostic line so the collector and triage can classify the failure without reading the full log. Add it inside the existing judging loop at `ci.yml:911-918`:
     ```bash
     echo "MERGE_TRAIN_SHARD_RESULT: shard=${mt_id} rc=${mt_rc} failed_tests=$(grep -c '^FAILED ' "${mt_stem}.log" 2>/dev/null || echo 0) first_failed=$(grep -m1 -o '^FAILED [^ ]*' "${mt_stem}.log" 2>/dev/null | cut -d' ' -f2 || true)"
     ```
     This is additive, and it fails open because `|| true` / `|| echo 0` keep it from failing the step.
   - **Expected impact:** CI failure rate drops from 90% toward its baseline.
2. **`orchestrate-poll (1)` failures: 2 of 10 runs.**
   - **Evidence:** run 43115726797 failed at `Orchestrate poll process unit tests`; run 43112699857 failed at `Review autofix review-pipeline plumbing contract test`.
   - **Fix:** the same structured-result pattern for the poll shards, at the `::group::orchestrate-poll shard` block around `ci.yml:2672`.
   - **Expected impact:** 20% of CI failures become self-describing.
3. **Telling timeouts apart from test failures.**
   - `extract_failure_point` drops step-conclusion context: a timed-out job comes back as `step_name: None`.
   - **Change:** add `failure_point.step_conclusion` and `failure_point.job_conclusion` to the dict at `collect_workflow_logs.py:1073-1083`. The keys are additive and the existing `job_name`/`step_name` keys are unchanged (§10).
   - **Impact:** budget-watch timeouts become distinguishable from regressions.
4. **Semble runtime fallbacks.**
   - **Evidence:** 4 fallbacks, 0 queries and 0 `SEMBLE_BOOTSTRAP` lines in implement run 43114089699. Across all families: 44 fallbacks = 40 contract-test + 4 runtime.
   - **Assessment:** the contract-test fallbacks come at exactly 4 per CI run, which is healthy and deterministic. The runtime fallbacks, with no bootstrap line at all, look like a masked rollout (Semble never started) rather than a rare fail-open. Confidence is low with n=1.
   - **Mitigation:** emit `SEMBLE_BOOTSTRAP state=skipped reason=<…>` whenever bootstrap is bypassed, and require `reason=` on every runtime `SEMBLE_FALLBACK`.
   - **Rollback:** both changes are log-only.
5. **BREAK_GLASS and CONTEXT_BUDGET_WARN:** 0 / 0 across all families. There is no rubric pressure or prompt-size signal, but given the telemetry gap above, treat this as unverified.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines could be inspected, because the deep-dive logs were not mounted and there are no `log_summary` fields in the context. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, and `fail_open`/`enabled: false` counts are all **unknown**.

Recommendation: add an `ai_memory_*` aggregate (retrieve count, hits, fail_open count) to `cost_audit.py` alongside `AGGREGATABLE_COST_FIELDS` (line 246), so the next report does not depend on raw logs. Then check a sampled implement or plan log to confirm the lines are emitted.

## GH API Call Audit

The context has no per-call GH API counts, so these findings come from collector behaviour.

1. **Wasted log downloads on skipped runs.** 15 of the first 16 `recent_runs` (for example 43129296861, 43129296976, 43129092562, 43129014878) have `log_download_status: empty_archive` and `diagnostic_failure_reason: "logs: empty_log_archive"`. Each one is a logs-zip API call that cannot return data.
   - **Change:** in the `recent/` deep-dive selection, skip runs whose `conclusion == "skipped"`. This is zero-risk.
   - **Reduction:** about 15 calls per collection, and the deep-dive slots go back to real runs.
2. **The listing budget runs out on skipped runs.** `--per-page 100 × --max-pages 10` (`collect_workflow_logs.py:429-437`) gives at most 1,000 listed runs, and `page_limit_reached` is set when `total_count >= 1000` (line 692). 998 runs were kept, 887 of them skipped, so the window shrank to about 2h10m.
   - **Change:** collect `skipped` separately or exclude it. The runs endpoint accepts one `status=` value, so list `status=completed` plus per-conclusion queries for the non-skipped set. Surface `page_limit_reached` and the actual window bounds in `summary`.
   - **Effect:** the analysed window widens from about 2 h to the intended period for a similar number of calls.
3. **No other unbatched or looped call patterns are visible.** `lint-pr-body-auto-close.yml:85` makes one paginated read, documented under §14, which is fine. The poller's batched helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) cannot be audited without poll logs.

## Prompt Cache & Memory System

- `cache_hit_rate` is null in every family, `or_cache_read_tokens` and `or_cache_write_tokens` are 0, and `context_budget_warn_count` is 0. There is **no evidence** about prompt-cache behaviour, prefix stability or cache fragmentation this window.
- Root cause: the same missing `claude_*` field passthrough as Cost item 1. Claude cache-read and cache-write counts exist in `cost_audit.py` (`claude_cache_read_tokens`, `claude_cache_write_tokens`, lines 690-694) but do not reach the analysis context.
- Recommendation: derive a Claude-side `cache_hit_rate` = `claude_cache_read_tokens / (claude_input_tokens + claude_cache_read_tokens)` and report it per family. Once that number exists, re-evaluate prompt-prefix stability for check_failure_triage, the longest AI family at p50 3,380 s.

## Orchestrator Health

- **Poller:** 9 runs, 2 succeeded and 7 were cancelled. The cancellations are `cancelled_before_first_step` on `poll / poll` (43123520137 after 2,208 s; 43115583529 after 691 s). This is expected behaviour of the `ai-orchestrate-poll-${{ github.repository }}` group with `cancel-in-progress: false` (`orchestrate_poll.yml:97-102`): a newer pending tick replaces an older one. Waits of 691–2,208 s before cancellation show that ticks pile up behind runner contention. The completed polls took 1,873 s (43113356829) and 2,858 s (43117118758), so the effective cadence is about 30–50 min, not 5.
  - **Indicator to track:** the ratio of cancelled-before-first-step to completed poll runs.
- **Judge cycles:** 3 `unblock_judge_dispatch` runs, all successful (#9236 → 43113361753, #9253 → 43117290271, #9215 → 43122945728), p50 1,556 s.
- **Clarification:** orchestrate_clarify_respond had 3 successes out of 210 runs; the other 207 were skipped. Durations of 1,526 s (43118668763) and 1,313 s (43121060078) are inflated by queueing.
- **Promotion and integration:** promote_candidate_follow 2/2 succeeded; integration_pr_readiness 14/14 (p50 1,162.5 s); forward-merge stable→main 1/1.
- **Heal:** workflow_failure_heal had 2 successes and 31 skipped; workflow_failure_heal_intake had 4 successes and 27 skipped (p95 1,388 s). No stuck or terminal states are visible.
- **Smallest mitigation:** reduce runner contention (Speed items 1–2). The poller configuration itself is behaving as designed.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| clarify / plan / implement (event dispatch) | Fan-out noise (no runner cost) | 211 / 200 / 205 skipped runs; bursts of about 30 skipped runs within 8 s (00:39:41–00:39:49Z) |
| plan / implement (executed) | Queue plus compute (not separable) | plan successes 2,694 s (43123559440), 2,272 s; implement 7,080 s (43114089699), 3,630 s (43113239408) |
| CI (gate for review and merge) | **Retry and failure** first, **queueing** second | 9/10 red; up to 5,902 s against a ≤20 min job ceiling |
| Triage (reacting to CI) | Queue plus compute, extra load on CI | 15 runs, p50 3,380 s |
| Lint / readiness checks | Pure queueing | lint p50 1,297 s with a 5 min timeout; readiness p50 1,162.5 s |
| review_autofix | Small | p50 84 s, p95 1,175.4 s (7 runs) |

Fixes in order of end-to-end impact:
1. Fix the merge-train regression.
2. Add CI PR-level cancel-in-progress.
3. Dedupe triage runs during a red-base burst.
4. Add job-level queue-time telemetry, so the queue and compute shares above can be measured instead of inferred.

The proposed collector addition is additive. It uses `jobs` already fetched for failed and cancelled runs (no new API calls) and goes in `compute_run_metrics`, `collect_workflow_logs.py:1147-1176`:
```python
job_queue = [
	(_parse_iso8601(j.get("started_at")) - _parse_iso8601(j.get("created_at"))).total_seconds()
	for j in jobs
	if _parse_iso8601(j.get("started_at")) and _parse_iso8601(j.get("created_at"))
]
row["job_queue_seconds_max"] = int(max(job_queue)) if job_queue else None
row["job_queue_seconds_sum"] = int(sum(job_queue)) if job_queue else None
```

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** runner queueing (lint at 1,251 s average against a 5 min timeout; CI up to 5,902 s against 20 min jobs), and a poller cadence of about 30–50 min.
- **Top failure modes:** the merge-train step (7 CI runs) and `orchestrate-poll (1)` unit and contract tests (2 CI runs).
- **Highest-cost drivers (inferred, telemetry missing):** check_failure_triage (15 × p50 3,380 s), implement (7,080 s single run), and the red CI reruns (43,347 s).
- **Top 3 actions:**
  1. Fix the merge-train failure and add the `MERGE_TRAIN_SHARD_RESULT:` line.
  2. Add a PR-scoped `concurrency` with `cancel-in-progress` to `ci.yml`.
  3. Fix collector coverage: drop skipped runs from deep-dive and listing budgets, pass `claude_*` cost fields through, and add `job_queue_seconds_*`.

## Metrics Appendix

**Run outcomes (window ≈ 22:32Z–00:42Z, 998 runs)**

| Family | Total | Success | Failure | Cancelled | Skipped/other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| ci | 10 | 1 | 9 | 0 | 0 | 5,365.5 | 5,867.35 |
| check_failure_triage | 15 | 12 | 0 | 0 | 3 | 3,380 | 4,288 |
| check_failure_triage_historical | 1 | 1 | 0 | 0 | 0 | 1,409 | 1,409 |
| cancel_on_pr_close | 10 | 5 | 0 | 5 | 0 | 787.5 | 2,089.4 |
| clarify | 215 | 4 | 0 | 0 | 211 | 1 | 10 |
| plan | 208 | 8 | 0 | 0 | 200 | 1 | 11 |
| implement | 208 | 3 | 0 | 0 | 205 | 1 | 10 |
| orchestrate_clarify_respond | 210 | 3 | 0 | 0 | 207 | 1 | 9 |
| orchestrate_poll | 9 | 2 | 0 | 7 | 0 | 523 | 2,598 |
| integration_pr_readiness | 14 | 14 | 0 | 0 | 0 | 1,162.5 | 1,920.45 |
| lint_pr_body_auto_close | 16 | 16 | 0 | 0 | 0 | 1,297 | 1,722 |
| review_autofix | 7 | 4 | 0 | 0 | 3 | 84 | 1,175.4 |
| security_audit | 4 | 3 | 0 | 1 | 0 | 891.5 | 1,586.9 |
| unblock_judge_dispatch | 3 | 3 | 0 | 0 | 0 | 1,556 | 2,120.3 |
| promote_candidate_follow | 2 | 2 | 0 | 0 | 0 | 1,746.5 | 2,044.85 |
| workflow_failure_heal | 33 | 2 | 0 | 0 | 31 | 1 | 482.8 |
| workflow_failure_heal_intake | 31 | 4 | 0 | 0 | 27 | 2 | 1,388 |
| forward_merge_stable_to_main / issue_pr_status | 1 / 1 | 1 / 1 | 0 | 0 | 0 | 960 / 771 | — |
| **All** | **998** | **89** | **9** | **13** | **887** | **1** | **1,595** |

Failure rate: 0.9% of all runs, 8.1% of the 111 runs that actually executed, and 90% for CI.

**CI failure points**

| Job → Step | Runs |
|---|---|
| tests-hooks-and-orchestrator → Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15) | 43114013218, 43115476765, 43115959904, 43116204712, 43116239930, 43116887837, 43117196090 |
| orchestrate-poll (1) → Orchestrate poll process unit tests | 43115726797 |
| orchestrate-poll (1) → Review autofix review-pipeline plumbing contract test | 43112699857 |

**Telemetry fields**

| Metric | Value |
|---|---|
| runs_with_log_telemetry / wall_clock_sample_count | 115 / 110 |
| wall_clock_p50_ms / wall_clock_p99_ms | 1,000 / 5,895,070 |
| cache_hit_rate | null (all families) |
| break_glass_count / context_budget_warn_count | 0 / 0 |
| codex_calls / codex_tokens_used | 0 / 0 |
| or_calls / or_total_tokens / or_cache_read / or_cache_write | 0 / 0 / 0 / 0 |
| claude_* fields | not present in analysis context (gap) |
| GH API call counts / rate-limit events | not present in analysis context (gap) |
| Collector listing budget | 100 × 10 pages = 1,000; 998 kept (window truncated, inferred) |
| Deep-dive empty-archive log downloads (recent set) | 15 |

**Semble**

| Metric | Value |
|---|---|
| query_calls / query_bytes | 0 / 0 |
| fallbacks (contract-test / runtime) | 44 (40 / 4) |
| contract-test fallbacks per CI run | 4 (×10 runs) |
| runtime fallbacks | 4, all in implement run 43114089699 |
| bootstraps / failed / unused / bootstrap_ms_total | 0 / 0 / 0 / 0 |
| sources_total / static_dup_bytes_total / echo_lines_dropped | 0 / 0 / 0 |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | tool_calls | fallbacks |
|---|---|---|---|---|---|---|---|
| (no targets observed) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none. The per-target breakdown (`semble_targets`, `serena_targets`) is not included in the analysis context, which is a coverage gap.

**Next collection steps:**
1. Mount the run-logs directory, or attach `log_summary` fields, for the 9 failed CI runs and run 43114089699.
2. Pass the `claude_*` cost fields and the `semble_targets` / `serena_targets` maps through to the analysis context.
3. Add `job_queue_seconds_max` / `job_queue_seconds_sum` and `failure_point.step_conclusion`.
4. Exclude skipped runs from the 1,000-run listing budget and the deep-dive selection.

**Assumptions and missing context:** No files were edited; the report above is the deliverable. Statements about queue time and the triage feedback loop are inferences from timeout ceilings and timing overlap. The cause of the merge-train failures cannot be determined without the step logs.