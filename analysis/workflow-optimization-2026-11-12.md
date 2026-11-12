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
## Deep Audit — Workflows & Scripts (2026-11-12)

> **Scope and method.** This pass goes deep on a few files rather than skimming all of them: `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/check_failure_triage.sh`, `scripts/workflow_failure_heal_intake.sh`, the API and loop paths of `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml`, `review_autofix.yml`, `update_workflows.yml`, `test-and-mark-stable.yml`, `ci.yml` (merge-train step), `lint-pr-body-auto-close.yml` and `workflow-log-analysis.yml`. Byte and character counts are estimates from line spans, because no shell was available to run `wc -c`. Every count marked "est." is labelled **[NEEDS VERIFICATION]**.
>
> **Cross-references to the existing report sections.** The missing `concurrency:` block in `ci.yml` (Speed item 2) and the unknown cause of the merge-train failures (Speed item 1 / Reliability item 1) are already covered there, so they are not repeated. BUG-002 below adds to Reliability item 1.

### Section 1: Bug & Correctness Sweep

**BUG-001** · `.github/workflows/implement.yml:5544-5774` (comment POST at `5757-5758`; labels at `5763-5774`) · Severity: **Medium** · Category: `bug`
- **Description:** The fall-through branch of "Handle no-op implementation" runs under `set -euo pipefail` (line 5545). It posts its comment with `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"`, which has no `|| …` guard. `gh_retry` makes only one attempt for a non-idempotent POST (`scripts/gh_helpers.sh:1210-1213`). So one transient failure aborts the step before the `ai:implementation-failed` label is applied at 5763-5774. The earlier guards in the same step add `>/dev/null 2>&1 || true` to their comment POSTs (5637-5638, 5728-5729).
  - Downstream, "Comment on issue failure" (6939-7014) then finds `ai:implementing` still set and restores `ai:awaiting-approval` (7001-7006). The issue is parked for re-approval instead of being routed to orchestrator re-processing. This routing consequence is an inference from the step `if:` conditions **[NEEDS VERIFICATION]**.
  - The same unguarded-POST shape exists at 6979-6980. There the `ai:blocked` latch is applied first, but a failed POST still fails the step after terminalization.
- **Recommended fix:** Apply the labels (5763-5774) before the advisory comment, and guard the POST the way 5637-5638 does: `… -f body="${noop_body}" >/dev/null 2>&1 || echo "::warning::Could not post no-op comment on #${ISSUE_NUMBER}; label applied."`. Apply the same guard at 6979-6980.

**BUG-002** · `.github/workflows/ci.yml:908-910` vs `920-929` · Severity: **Low** · Category: `bug`
- **Description:** The merge-train step prints each shard log raw (`cat "${mt_stem}.log"`). Yet the comment at 920-921 says lines get a `"| "` prefix "so log text cannot act as a workflow command", and only the 40-line tail (929) is actually prefixed.
  - The modules under test exercise `gh_helpers.sh`, which writes `::error::`/`::warning::` lines (for example `gh_helpers.sh:1276`). Pytest replays captured output for failing tests, so those lines become real run annotations and can mislead failure attribution. That attribution is the gap the existing Reliability item 1 describes.
- **Recommended fix:** Prefix the full log too: `{ cat "${mt_stem}.log" 2>/dev/null || true; } | sed 's/^/| /'` inside the `::group::` at 909. Alternatively, wrap it in `::stop-commands::<random-token>` / `::<random-token>::`.

**SEC-001** · `.github/workflows/update_workflows.yml:913`, `1849-1851` (producers `614-632`, `936-945`, `961`, `969`) · Severity: **Low** · Category: `security`
- **Description:** The Summary step interpolates step outputs directly into double-quoted shell assignments:
  - `AUDIT_GATE_MESSAGE="${{ steps.audit_gate.outputs.message }}"`
  - `FAILURE_REASON_DETAIL="${{ steps.update.outputs.failure_reason_detail }}"`
  - Line 913 does the same for `vars.WORKFLOW_PROFILE`.

  `failure_reason_detail` contains `${SELECTED_PROFILE}`, manifest entries and paths (`fail_with_reason` calls at 950-1023). The message comes from a Python-parsed result file. A `"`, backtick or `$(` in those values would execute as shell code. The sources are an admin-controlled variable and the verified upstream release, which is why this is rated Low.
- **Recommended fix:** Move these values into the step's `env:` block, the pattern the same file already uses at 1073-1090, for example `FAILURE_REASON_DETAIL: ${{ steps.update.outputs.failure_reason_detail }}`. Then read `"${FAILURE_REASON_DETAIL}"`.

**SEC-002** · `.github/workflows/review_autofix.yml:1261-1263` · Severity: **Low** · Category: `security`
- **Description:** On a `mktemp` failure, the inline `gh_retry` logs `aborting without running: $*`, which includes every argument, comment bodies (`-f body=…`) among them. `gh_helpers.sh` replaced this exact pattern with `_gh_cmd_log_summary` (lines 854-860, issue #8095 SEC-001), which never prints flag values.
- **Recommended fix:** Print only the first two arguments (`${1:-} ${2:-}`), or fold this wrapper into DUP-002.

**No finding (verified):** `github.event.comment.body`, `issue.title`, `pull_request.title/body/head.ref` reach shell only through `env:` in every workflow (for example `lint-pr-body-auto-close.yml:60-61`, `issue_pr_status.yml:239-240`, `orchestrate_clarify_respond.yml:145`, `implement.yml:1045`). No direct `${{ github.event.* }}` interpolation of untrusted text was found in any `run:` body.

### Section 2: GitHub API Call Redundancy Audit

**API-001** · `scripts/gh_helpers.sh:228-231`, `1259-1268`; callers `scripts/label_helpers.sh:196-214`, `scripts/orchestrate_poll_process.sh:4463-4479`, `scripts/validate_process.sh:1246-1259`, `scripts/check_failure_triage.sh:1242-1248` · Severity: **High** · Category: `api-redundancy`
- **Description:** When a label already exists, `gh label create` fails with `label with name "…" already exists; use --force …`. That message contains no `HTTP 422` (repo fixtures: `tests/test_label_helpers_phase_transition_contract.py:94`, `tests/test_implement_post_codex_recovery.py:288`). So `_is_gh_permanent_failure` (line 230) does not match it, and `gh_retry` takes the transient path: 5 attempts with 1+2+4+8 = **15 s** of sleeps.
  - The repo already documents this: `scripts/implement_handle_guard_block.sh:30-33` says "gh_retry does not treat it as permanent", and `scripts/review_rb_judge.sh:916-923` says the same.
  - The central helper `ensure_label_exists` (`label_helpers.sh:196`) only recognises "already exists" (line 210) after `gh_retry` has given up. The process-local cache (159-166) helps only on repeat calls.
  - Other affected call sites: `implement.yml:5581, 5629, 5716, 5767, 6732, 6784, 6961, 7002`; `review_autofix.yml:1284, 1701, 4634, 4637` (inline retry: 4 attempts, 14 s); `implement_diagnose_post_codex_failure.sh:222, 242, 245`; `review_merge_train.sh:812`; `review_autofix_step_mark_ready_to_merge.sh:20-26`; `review_autofix_step_mark_review_blocked_workflow_failure.sh:14`.
  - `check_failure_triage.sh:1244-1245` creates 2 labels on every triage run. The existing report counts 15 triage runs in about 2 h.
- **Calls:** today 5 POSTs and about 15 s per (label, process) when the label exists, so 10 POSTs and about 30 s per triage run. After the fix: 1 POST and 0 s.
- **Recommended fix:** Append `|already[ _-]*exists|already_exists` to the ERE at `gh_helpers.sh:230`. The permanent path still prints the stderr excerpt (1245-1247), so the check at `label_helpers.sh:210` keeps working. Add a case to the existing `tests/test_gh_retry_permanent_auth_failures.py`. Then update the now-stale comments at `implement_handle_guard_block.sh:30-33` and `review_rb_judge.sh:916-923`.

**API-002** · `scripts/gh_helpers.sh:1415-1489` · Severity: **Medium** · Category: `api-redundancy`
- **Description:** Unlike `gh_retry` (1243-1250) and `gh_retry_to_file` (1328-1336), `gh_api_json_to_file` never calls `_is_gh_permanent_failure`. The comment at 220-222 names only those two helpers. As a result, 404, 401 and 422 responses are retried 5 times with 15 s of backoff. Several callers expect a 404 and treat it as a rejection outcome:
  - `workflow_failure_heal_intake.sh:627/643` (`issue_not_found`)
  - `workflow_failure_heal_intake.sh:646/659` (`pull_request_not_found`)
  - `workflow_failure_heal_intake.sh:653` (compare against an unknown SHA)
  - `check_failure_triage.sh:848` and `987`
- **Calls:** 5 → 1 per permanent failure, which also saves about 15 s on each rejection path.
- **Recommended fix:** In the `else` branch (1456-1477), check `_is_gh_permanent_failure "${stderr_content}"` first. If it matches, print the escaped excerpt, run `: > "${outfile}"` and `return 1`, mirroring 1328-1336.

**API-003** · `scripts/gh_helpers.sh:1519-1552` · Severity: **Low** · Category: `api-redundancy`
- **Description:** `curl_gh_api` retries every non-2xx status except a rate-limited 429/403, including 400, 401, 404, 410 and 422, using 1-8 s backoff. That is 5 calls where 1 would do.
- **Recommended fix:** After the rate-limit branch, add `case "${http_code}" in 400|401|404|410|422) rm -f …; return 1 ;; esac`, matching the `permanent` kind in `gh_api_retry` (1128-1135).

**API-004** · `scripts/workflow_failure_heal_intake.sh:2072-2079` (vs `1303-1306`, `2248-2253`) · Severity: **Low** · Category: `api-redundancy`
- **Description:** `HEAL_TRUSTED_AUTHOR` calls `GET /user` whenever `PHASE_COMMENT_AUTHOR` is empty, even if `PROVENANCE_LOGIN` was already read from the same token at line 1305. The recovery path at 2249-2252 already reuses `PROVENANCE_LOGIN`.
- **Calls:** 2 → 1 on self-repo phase reports.
- **Recommended fix:** At 2074, use `HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"` before the fallback call.

**API-005** · `scripts/check_failure_triage.sh:1282-1303` · Severity: **Low** · Category: `api-redundancy`
- **Description:** `historical_api_get` uses raw `gh api` with a fixed 2 s/4 s retry and no rate-limit detection. A 403 or 429 rate-limit response is retried twice inside the limit window, which is a tight retry on a non-transient condition.
- **Recommended fix:** Detect `_is_gh_rate_limit` on `${hist_api_err}`, then call `_gh_rate_limit_wait_for` (gh_helpers.sh:333-342) or return 1. Alternatively switch to `gh_api_retry --optional`, where exit code 2 maps to 4 **[NEEDS VERIFICATION: `gh_api_retry` uses `gh api -i` for non-paginated calls; confirm that job-log redirects still work]**.

**BATCH-001** · `scripts/orchestrate_poll_process.sh:18551-18582` (callers `20337`, `25962`, `32788`); `.github/workflows/implement.yml:5690-5703` · Severity: **Low** · Category: `api-batching`
- **Description:** Each hop of `count_noop_ancestors` makes 2 REST calls (`GET issues/{n}` for the body, then a paginated `GET issues/{parent}/comments`). That is up to 2×`MAX_IMPL_NOOP_REISSUES` calls per issue, inside poll loops.
- **Calls:** 2 per hop → 1 per hop, using one GraphQL `issue(number:){ body comments(last:100){ nodes { body } } }`. The first hop's body can come from `_candidate_details_json` when the issue is already in the batch **[NEEDS VERIFICATION]**.
- **Recommended fix:** Extend the `_fetch_candidate_issue_details_graphql` pattern with a single-issue variant and fail open to the current REST path.

**BATCH-002** · `scripts/orchestrate_poll_process.sh:189-248` · Severity: **Low** · Category: `api-batching`
- **Description:** `replay_failed_reclarify_commands` makes a per-issue `GET issues/{n}` (202) plus a paginated comments read (205) inside a `while read` loop over the queued issues.
- **Calls:** N issue reads + N comment listings. Batching the issue reads gives ceil(N/25) + N. The comment reads must stay full-history REST, because line 206 requires a complete history to authorize a replay.
- **Recommended fix:** Fold the issue reads into the batched-GraphQL contract documented at 249-257. Keep the comment REST read.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** · `ensure_label_exists` defined 9 times · Severity: **Medium** · Category: `duplication`
- **Description:** The definitions are:
  - `scripts/label_helpers.sh:176` (canonical, cached)
  - `scripts/orchestrate_poll_process.sh:4429` (overlays `label_contract.v1.json`, own cache)
  - `scripts/validate_process.sh:1231` (contract overlay, **no cache**)
  - `scripts/review_rb_judge.sh:924`
  - `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`
  - `scripts/review_autofix_step_mark_ready_to_merge.sh:15`
  - `.github/workflows/review_autofix.yml:1282` (4-argument signature), `:1700` and `:4629`

  Behaviour differs: caching, metadata source, and argument count.
- **Recommended fix:** Make `scripts/label_helpers.sh` own `ensure_label_exists <label> [repo]`, with an optional overlay from `.github/ai/label_contract.v1.json` when the file is present (moved from `orchestrate_poll_process.sh:4453-4458`). Replace the definitions in `validate_process.sh` and `orchestrate_poll_process.sh` with `source label_helpers.sh`. Keep the inline copies only as `type ensure_label_exists || …` fallbacks that delegate to catalog values.

**DUP-002** · `.github/workflows/review_autofix.yml:1251-1280` vs `scripts/gh_helpers.sh:1204-1282` · Severity: **Medium** · Category: `duplication`
- **Description:** The inline `gh_retry` copy has drifted from the shared helper:
  - no permanent-failure classification
  - no rate-limit wait or breaker
  - retries POST creates (no `_gh_cmd_is_unsafe_post`)
  - `max=4` instead of 5
  - logs raw argv (SEC-002)

  This step already verifies a support checkout (`.codex-freshness-src`, 1307-1310) before sourcing `pr_checks_lib.sh`.
- **Recommended fix:** When `freshness_helper_verified=true`, source `.codex-freshness-src/scripts/gh_helpers.sh` and keep the inline copy only for the unverified fallback. Note that 100 call sites in 48 files define the no-retry stub `type gh_retry … || gh_retry() { "$@"; }`; that stub is a correct fail-open, so leave it.

**DUP-003** · `scripts/orchestrate_poll_process.sh:18551-18582` vs `.github/workflows/implement.yml:5685-5703` · Severity: **Low** · Category: `duplication`
- **Description:** The ancestor no-op walk appears twice, nearly identical: same marker, same `Re-issued from #N` regex, same awk sum. The only difference is the transport (`_safe_gh_jq` vs `gh_retry gh api`).
- **Recommended fix:** Create a new `scripts/noop_ancestry_helpers.sh` exporting `count_noop_ancestors <repo> <issue_num> [max_depth]`. Source it from the poller and from implement via `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}`, which already supplies `gh_helpers.sh` at 5546. This also shrinks EXPR-001.

**DUP-004** · "Resolve workflow support ref" block: 18 copies across 11 workflows (`implement.yml:469-511`, `772-812`, `1353-1397`, `7291-7334`; 2 each in `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_poll.yml`; 1 each in 5 more workflows) · Severity: **Low** · Category: `duplication`
- **Description:** The copies have started to drift: `implement.yml:782` hardcodes `"shubhodeep1/coding-workflows"`, while 476 uses `wf_source`. The block cannot move to `scripts/`, because it runs before the support source is checked out, and consumer repos have no `scripts/`.
- **Recommended fix:** Keep the blocks inline but normalize them to the `wf_source` form. Extend `tests/test_workflow_support_ref_follows_pin.py` (cited at 780-781) to assert the bodies are identical apart from the output variable name.

**DUP-005** · `.github/workflows/workflow-log-analysis.yml:1105-1106`, `1908-1909`, `2508-2509` · Severity: **Low** · Category: `duplication`
- **Description:** `_wla_label_retry` and `_wla_label_create` are defined three times with identical one-line bodies.
- **Recommended fix:** After API-001, use `ensure_label_exists` from `label_helpers.sh` when it is sourced, and keep a single fallback definition.

### Section 4: Expression Size Limit Risk Assessment

The repo already guards interpolated `run:` bodies at 18,000 characters (`tests/test_workflow_file_size_limit.py:28-32, 66-98`), so a High-risk body (18,000 or more) cannot merge while CI is green. The findings below are in the 15,000-18,000 band or close to it.

**EXPR-001** · `.github/workflows/implement.yml:5545-5774` ("Handle no-op implementation") · Severity: **Medium** · Category: `expression-limit`
- **Description:** The body is 230 lines with long prose `printf` lines (5574, 5617, 5624, 5711, 5713). Estimated at about 15,500-17,000 characters, leaving about 4,000-5,500 of headroom against 21,000 and about 1,000-2,500 against the 18,000 guard **[NEEDS VERIFICATION]**. Its only interpolations are `${{ github.server_url/repository/run_id }}` (5549, 5551, 5757, 5765, 5767, 5771, 5773).
- **Recommended fix:** Replace them with the runner's default environment variables (`${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}`, `${GITHUB_RUN_ID}`). The body then contains no `${{ }}` and drops out of the limit entirely. DUP-003 shrinks it further.

**EXPR-002** · `.github/workflows/test-and-mark-stable.yml:1069-1344` ("Phase 2: Wait for plan to complete") · Severity: **Medium** · Category: `expression-limit`
- **Description:** The body is 276 lines, estimated at about 16,000-17,000 characters (about 4,000-5,000 headroom) **[NEEDS VERIFICATION]**. The only interpolation is `${{ steps.create-issue.outputs.created_after }}` at 1086, 1098 and 1116. Phase 1 (907-1045) has the same pattern at 947.
- **Recommended fix:** Add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to `env:` (1062-1067) and use `${CREATED_AFTER}`. Do the same in Phase 1.

**EXPR-003** · `.github/workflows/test-and-mark-stable.yml:6602-6833` ("Send Telegram notification") · Severity: **Low** · Category: `expression-limit`
- **Description:** The body is 232 lines, about 50 of them comments, estimated at about 14,000-15,000 characters **[NEEDS VERIFICATION]**. It has more than 20 interpolations (6609-6629, 6826).
- **Recommended fix:** Move the `needs.*.result` and `outputs` values into `env:`, as the step already does for `GATE_ONLY` and the others at 6594-6600.

**EXPR-004** · `.github/workflows/implement.yml` (7,362 lines) · Severity: **Low** · Category: `expression-limit`
- **Description:**
  - **The 1 MB / 800 KB figures in the brief are superseded.** CLAUDE.md §27 and `tests/test_workflow_file_size_limit.py:26-27` set the effective limit at 512,000 bytes, with the guard at 480,000. No workflow can approach 800 KB while that guard passes.
  - **implement.yml may be close to the 480,000-byte guard.** At an inferred 60-65 bytes per line, it would sit at roughly 440-480 KB. Next largest: `test-and-mark-stable.yml` (6,833 lines), `review_autofix.yml` (6,012) **[NEEDS VERIFICATION: `wc -c`]**.
  - **`if:` conditions are far from the expression limit.** The largest are at 5537 and 6940, under 500 characters each.
- **Recommended fix:** Pre-emptively extract "Run Codex implementation" (body 2932-4217, about 1,286 lines, no `${{ }}`) into `scripts/implement_step_run_codex.sh`. Follow the `review_autofix_step_<slug>.sh` and `REQUIRED_BOOTSTRAP_SCRIPTS` pattern in CLAUDE.md §27.

### Section 5: Cross-Cutting Concerns

**DEAD-001** · `scripts/orchestrate_poll_process.sh:18646-18662` · Severity: **Low** · Category: `dead-code`
- **Description:** `read_standalone_state_json` has no callers anywhere in `scripts/`. `get_standalone_state_comment_id` is reachable only from the 2-argument path of `write_standalone_state_json` (18680-18684), and all 22 call sites (25123-26174) pass 3 arguments.
- **Recommended fix:** Remove `read_standalone_state_json`. Either remove the 2-argument fallback with a test asserting the 3-argument contract, or keep it and document it as a deliberate fallback.

**CONSIST-001** · `.github/workflows/implement.yml:5581-5583`, `5629-5631`, `5716-5718`, `6961-6963` vs `scripts/label_helpers.sh:48, 50, 62, 115, 117, 129` · Severity: **Medium** · Category: `consistency`
- **Description:** Inline label creation disagrees with the catalog (which mirrors `label_contract.v1.json`):

  | Label | implement.yml | Catalog |
  |---|---|---|
  | `ai:closed` | `6b7280` | `6a737d` |
  | `ai:needs-human` | `b60205` + "Automation halted…" | `e11d48` + "Escalated for human intervention…" |
  | `ai:blocked` | "Blocked: human input required" | "Orchestrator auto-answer loop blocked…" |

  `gh label create` never updates a label that already exists, so in a fresh consumer repo the first workflow to create a label fixes its metadata.
- **Recommended fix:** Replace these inline creates with `ensure_label_exists` (already sourced in this job at 5763-5765) and keep the inline create only as a fallback that uses the catalog values.

**CONSIST-002** · `.github/workflows/review_autofix.yml:1311` · Severity: **Low** · Category: `consistency`
- **Description:** `_safe_gh_jq() { gh api "$@"; }` drops the documented contract (`gh_helpers.sh:1369-1399`): nothing on stdout when the call fails. The effect is mostly masked because `pr_checks_lib.sh` calls it through the inline `gh_retry`, which buffers stdout (1265-1270).
- **Recommended fix:** Resolved by DUP-002 (source the verified `gh_helpers.sh`). Otherwise, copy the temp-file body from `gh_helpers.sh:1385-1399`.

**CONSIST-003** · `scripts/check_failure_triage.sh:215`, `220`; `.github/workflows/implement.yml:2283-2294` · Severity: **Low** · Category: `consistency`
- **Description:** These are raw `gh api` reads with no rate-limit handling, while the same paths use `gh_retry` elsewhere (`check_failure_triage.sh:1182-1201`; `implement.yml:2101`).
  - In triage, the `GET /user` and issue reads fail closed after one attempt and skip the supersede. The writes at 263-266 are unretried on purpose (comment 236-237), which is fine.
  - In implement, the loop can make up to 5 full paginated fetches, sleeping 2-10 s between attempts with no rate-limit awareness.
- **Recommended fix:** Wrap the reads in `gh_retry`. In implement, keep the 5-pass eventual-consistency loop but use `gh_retry gh api --paginate …` as the transport.

**DEBT-001** · `scripts/orchestrate_poll_process.sh` (at least 32,788 lines; a call site sits at line 32788) · Severity: **Low** · Category: `tech-debt`
- **Description:** A single sourced script of this size holds unrelated subsystems: reclarify replay (189-248), label ensure (4429), standalone state (18588-18692) and no-op ancestry (18551). That makes duplication (DUP-001, DUP-003) and dead code (DEAD-001) hard to spot.
- **Recommended fix:** Continue the extraction pattern from `review_autofix_step_*.sh`. Move the self-contained helper groups into `scripts/orchestrate_poll_<area>.sh` modules, source them from the top of the file, and register them in the existing bootstrap and test registries.

**TODO/FIXME/HACK markers:** none. A search for `TODO|FIXME|HACK|XXX:` across `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` returned 0 matches.

**ShellCheck:** no new SC2086, SC2046 or SC2006 violations were found in the sampled bodies. Numeric `$(seq …)` loops at `ci.yml:865, 896` and `implement.yml:5690` iterate over validated integers. ShellCheck already runs in `static-checks` (`ci.yml:28-29`).

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | API-001 |
| Medium | 7 | BUG-001, API-002, DUP-001, DUP-002, EXPR-001, EXPR-002, CONSIST-001 |
| Low | 17 | BUG-002, SEC-001, SEC-002, API-003, API-004, API-005, BATCH-001, BATCH-002, DUP-003, DUP-004, DUP-005, EXPR-003, EXPR-004, DEAD-001, CONSIST-002, CONSIST-003, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/gh_helpers.sh`, `tests/test_gh_retry_permanent_auth_failures.py`, comment updates in `scripts/implement_handle_guard_block.sh` and `scripts/review_rb_judge.sh` | Small |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/workflow_failure_heal_intake.sh`, `scripts/check_failure_triage.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml` | Medium |
| Code modularization | `scripts/label_helpers.sh`, `scripts/validate_process.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_rb_judge.sh`, `scripts/review_autofix_step_mark_*.sh`, `.github/workflows/review_autofix.yml`, `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml`, new `scripts/noop_ancestry_helpers.sh` | Large |
| Expression size reduction | `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml` | Small |
| Medium/Low fixes | `.github/workflows/implement.yml`, `.github/workflows/ci.yml`, `.github/workflows/update_workflows.yml`, `.github/workflows/review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/check_failure_triage.sh` | Medium |