## Executive Summary

- **The review "Checkout PR head branch" failure has recurred for at least 2 weeks and still has no recorded cause (high confidence).** Run 40034302059 (`Internal: AI Review & Autofix [pr:8023]`, `review / codex-agent`, 1,181 s) failed at that step. Earlier `analysis/` reports list the same failure in runs 37855612051, 37860320131, 37863790469, 38735635119, 38740625623, 38835616619, 38869906326 and 39711230881. The step has five bare `exit 1` paths (`.github/workflows/review_autofix.yml:2862, 2867, 2874, 2917`, plus `git_ref_health_check.sh repair` at `:2894`, which runs under `set -e`). The `REVIEW_CHECKOUT_FAIL` line recommended in 5 earlier reports has not landed: a repo-wide grep finds it only under `analysis/`. **Impact:** turns about 1 unexplained review failure every few days into a diagnosable one. Each failure currently wastes 15–40 min of review wall-clock.
- **review_autofix is where all the tokens go, and second review rounds roughly double them (high confidence).** All 222,253,135 OpenRouter tokens in the window came from 12 telemetered review_autofix runs: 115 calls, about 18.5 M tokens per run. Runs with 20 calls and 2 Semble bootstraps used 32.2–54.0 M tokens: 40040550203 (50.7 M), 40043977269 (54.0 M) and 40043597123 (32.2 M). Runs with 10–12 calls used 7.8–21.5 M. That points to a second review/autofix round (inference). **Impact:** cutting repeat rounds is the largest token lever.
- **The prompt cache works, but the report hides it (high confidence).** Recomputed from the token totals, the aggregate cache hit rate is **86.1%** (190,417,137 / 221,075,656). The report shows `null` because `compute_cache_hit_rate` returns `None` when any call's usage is unavailable (`scripts/cost_audit.py:455-456`), and 2 of 115 calls were. Per-run rates ranged from 0.816 to 0.903. Run 40036337074 recomputes to **0.722** but is reported as `null`. **Impact:** an observability fix that restores a key metric at no cost.
- **Both CONTEXT_BUDGET_WARN lines coincide with the only two calls that had no usage data (medium confidence).** The warnings are in runs 40036337074 and 40034624820, one each. Each of those runs also has `or_usage_unavailable_calls=1`. The match is 2 for 2, which suggests the oversized call is the one that loses its usage block (inference). **Impact:** this is prompt-size risk, not rubric pressure (`break_glass_count=0`).
- **CI p95 (2,143.75 s) is 2.3× p50 (948.5 s), driven by a single 22:23–22:40 UTC burst (medium confidence).** Nine CI runs in that window took 1,521–2,311 s, against about 860–960 s outside it. Log summaries show repeated "All GitHub-hosted runners with label [ubuntu-latest] are busy" waits: 3 m 38 s in 40046906706, about 3 m in 40046307682, about 2 m in 40046561498. **Impact:** cutting runner contention could save 15–20 min per affected CI run.
- **There were 3 hard failures in 1,000 runs (0.3%). Two are CI contract tests whose cause can't be confirmed here.** They are 40034624682 at "Judge Semble prefetch contract test" (`ci.yml:588`) and 40033407420 at "Test-and-mark-stable review-blocked budget contract tests" (`ci.yml:1643`). Later CI runs on the same afternoon passed, which suggests flakiness or a branch-local break fixed by a follow-up push (inference). **Data gap:** the deep-dive log directory `/home/runner/work/_temp/workflow-log-output` was not mounted, so the failing assertion text was not available.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Critical path: review_autofix wall-clock (p50 944 s, p95 2,424.9 s; telemetered runs p50 2,330,000 ms).**
   - **Evidence:**
     - Slow runs 40036562451 (2,709 s), 40036692115 (2,481 s), 40036337074 (2,427 s), 40040550203 (2,424 s) and 40028532100 (2,398 s).
     - The 20-call runs (40040550203, 40043977269, 40043597123) took 2,288–2,424 s.
   - **Root cause:** extra review/autofix rounds. Each round re-bootstraps Semble and re-runs the reviewer set (inference from `or_calls` 10 → 20 and `semble_bootstraps` 1 → 2).
   - **Exact change:**
     - Surface the existing `REVIEW_AUTOFIX_RUN_SUMMARY_V1` line in the analysis context. The collector already anchors it at `scripts/collect_workflow_logs.py:118-119`.
     - Then gate round 2 on new findings of severity at least medium. Do that only after the summary data shows how many round-2s were low-value.
   - **Estimated savings:** if a third of runs skip round 2, about 15–20 min per affected run.
   - **Risk:** medium (quality). Don't change the gate until the iteration data is visible.
2. **Critical path: runner queueing during bursts.**
   - **Evidence:**
     - Burst-window CI runs 40033304180 (2,311 s), 40034474167 (2,263 s), 40034974600 (2,145 s), 40034855944 (2,140 s).
     - Steady-state CI took about 860 s: 40046567219 (862 s), 40046540793 (864 s).
     - Queue waits quoted in the log summaries: 3 m 38 s (40046906706), about 3 m (40046307682), about 2 m (40046561498), 42 s (40046483255), 28 s (40047282844).
   - **Root cause:** hosted-runner saturation when CI (6 jobs per run), review, implement and the poller fire together (inference: per-job `started_at − created_at` is not in the telemetry).
   - **Exact change:**
     - Add a `job_queue_ms` field per job to the run rows. It can be computed from the jobs payload the collector already fetches to derive `failure_point`, so it costs no new API calls.
     - Then decide whether to stagger the scheduled jobs (`cancel_on_pr_close`, `promote_candidate_follow`) away from push bursts.
   - **Estimated savings:** about 10–20 min per burst-window CI run once the bursts are staggered.
   - **Risk:** low. The change is telemetry only.
3. **Critical path: the `tests-release-and-log-analysis` CI shard.**
   - **Evidence:** the shard ran about 14 min in 40046540793, about 11.5 min in 40046483576, about 8 min in 40046561498 and 00:46 → 00:59:51 in 40046567219. The visible test steps finish in about 1.3 s, so the cost sits in truncated steps; the summaries name the Node 22 claude-pool-broker Worker tests (inference).
   - **Exact change:**
     - Add a `slowest_steps` field (top 3 step durations) to each slow-run row. It can be read from the jobs-API step timestamps the collector already retrieves, so it costs no new calls.
     - If the Worker tests are confirmed to dominate, move them into their own parallel job.
   - **Estimated savings:** CI p50 from about 950 s down to about 600 s (estimate; depends on the next-longest shard).
   - **Risk:** low.
4. **Semble bootstrap latency (micro-optimization).**
   - **Evidence:** 11 bootstraps took 437,550 ms in total, an average of 39.8 s (33,154–47,500 ms). The 2-bootstrap runs spent 61.7–90.7 s (40043977269, 40040550203, 40043597123).
   - **Exact change:** reuse the lazy-bootstrapped index for the second round within the same run instead of rebuilding it.
   - **Estimated savings:** about 30–45 s per 2-round run.
   - **Risk:** low. Keep the existing `lazy-bootstrap-failed` fail-open path (`scripts/semble_helpers.sh:221,266`).

## Cost Optimizations

Ranked by token savings. No `claude_cost_usd` and no per-model pricing are in the context, so savings are given in tokens.

1. **Repeated review rounds: about 30–55 M tokens per 2-round run.**
   - **Evidence:** 20-call runs used 50.7 M, 54.0 M and 32.2 M tokens. 10–12-call runs used 7.8–21.5 M.
   - **Change:** same as Speed 1. First make the iteration count observable, then gate round 2.
   - **Estimated savings:** about 25–30 M tokens per avoided round, mostly cache reads, which are cheap. Uncached prompt is about 2.0–2.6 M per round.
   - **Quality risk:** medium.
2. **Uncached prompt and cache writes: 24.4 M uncached prompt (11.0% of total) and 6.2 M cache writes (2.8%) across 115 calls.**
   - **Evidence:** run 40036337074 has the lowest recomputed hit rate (0.722): 1,660,727 uncached prompt tokens against 5,525,497 cache reads.
   - **Root cause:** prefix churn or an oversized prompt (it also carries a CONTEXT_BUDGET_WARN). This is an inference; the phase is not visible without the deep logs.
   - **Change:** add the CONTEXT_BUDGET_WARN `phase=` value to the per-run telemetry row (the regex already captures it, `cost_audit.py:198-205`). That locates the phase whose prefix is unstable.
   - **Estimated savings:** raising the worst runs to the 0.86 median saves about 0.5–1 M uncached tokens per affected run.
   - **Quality risk:** none.
3. **Semble context is cheap and not noisy.**
   - 11 queries returned 162,281 bytes (about 40 K tokens, about 0.17% of uncached prompt) from 84 sources.
   - `static_dup_bytes` is 3,123 (1.9% overall; 6.7–6.9% in 40036562451 and 40043597123).
   - Semble adds little prompt expansion and its duplicate overlap is low. No change is warranted beyond the bootstrap reuse in Speed 4.
4. **Serena:** zero queries. `SERENA_ENABLED: false` appears in 40047534354 and 40046483255. There is nothing to evaluate.
5. **Skipped fan-out runs.** 747 of 1,000 runs (74.7%) ended "other"/skipped in clarify, plan, implement, orchestrate_clarify_respond, heal and triage. They cost no tokens. Some still took 6–11 s, which suggests a gate job on a runner (for example 40047549402 at 10 s and 40047504456 at 9 s; inference). The cost here is runner slots during bursts, not tokens. See Pipeline Flow Bottlenecks.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **Classify "Checkout PR head branch" exits in review_autofix (recurring, ≥9 runs in about 2 weeks).**
   - **Failure evidence:** 40034302059 (pr:8023, 1,181 s). The previous two pr:8023 runs passed: 40027774906 (1,135 s) and 40030800643 (912 s). The earlier occurrences are listed in the Executive Summary.
   - **Root cause category:** unknown. The candidates are the workspace being missing (`:2862`, the 2026-10-09 finding), an empty `HEAD_REF` (`:2873-2874`, which prints a plain echo with no `::error::`), fetch retries exhausted (`:2916`), or the ref-repair helper (`:2894`).
   - **Exact fix:** before every `exit 1` in that step, emit one structured line. Register the new prefix with an `agents.d/<pr>-review-checkout-fail.md` fragment under "Stable log prefixes (contractual)".
     ```bash
     echo "REVIEW_CHECKOUT_FAIL reason=head_ref_missing pr=${PR_NUMBER:-unknown} head_ref=${HEAD_REF:-}"
     # reasons: workspace_missing | opencode_config | head_ref_missing | fetch_exhausted | ref_repair_failed
     ```
     Then add a `REVIEW_CHECKOUT_FAIL` regex to `STRUCTURED_COST_TELEMETRY_PATTERNS` peers in `collect_workflow_logs.py`.
   - **Expected impact:** makes 100% of these failures diagnosable. The fix after that is likely a skip when the head ref is gone, as the 10-22-2 report proposed.
   - **Rollback:** the change is log-only, with no change to control flow.
   - **Escalation:** this is the sixth report to propose it. File it as an `ai:` issue so the orchestrator picks it up, instead of leaving it as report text.
2. **CI contract-test failures (2 runs).**
   - **Evidence:** 40034624682, `tests-hooks-and-orchestrator` / "Judge Semble prefetch contract test" (`tests/test_judge_semble_prefetch_contract.py`, `ci.yml:588-591`). 40033407420, `tests-release-and-log-analysis` / "Test-and-mark-stable review-blocked budget contract tests" (`tests/test_test_and_mark_stable_review_blocked_budget.py`, `ci.yml:1643-1646`).
   - **Root cause:** cannot be confirmed because the deep logs are missing. Later CI runs passed (40034974600, 40034855944), which suggests a branch-local regression fixed by a later push (inference). The review run on the same push, 40033407540, passed (1,821 s).
   - Separately, PR #8027's body records forward-merge conflicts in `.github/workflows/test-and-mark-stable.yml` (log summary 40046483255). That points to churn in the test-and-mark-stable area; this is a correlation only.
   - **Fix:** none until the assertion text is retrieved. Collection step: rerun the collector with the `errors/` directory mounted, and read `step-*-Judge_Semble_prefetch_contract_test.log` for 40034624682.
   - **Fail-open:** not applicable; these are correct hard gates.
3. **Prompt-size risk behind CONTEXT_BUDGET_WARN.**
   - **Evidence:** 2 warnings, in 40036337074 and 40034624820, each paired with `or_usage_unavailable_calls=1`.
   - **Category:** prompt size, not policy. `break_glass_count` is 0 everywhere.
   - **Fix:** when an OpenRouter usage line is recorded with `available=false`, also log `reason=` (for example `truncated_response`, `http_error`, `missing_usage_block`) so the two signals can be joined. `cost_audit.py:756-760` already parses `available=`.
   - **Impact:** shows whether the oversized calls are also the ones failing silently.
   - **Rollback:** additive only.
4. **Semble fallbacks are healthy.**
   - All 52 fallbacks have `context=contract-test` (`cost_audit.py:381-382, 822-826`), and `semble_runtime_fallbacks=0`. They are CI fixtures, not a broken rollout.
   - The count per CI run doubled from 4 (runs around 22:xx, for example 40031081913 and 40033304180) to 8 (runs around 00:45, for example 40046540793 and 40046567219). That is consistent with a new contract test being added (inference). No action.
   - The poller 40046307682 had `SEMBLE_AVAILABLE: false` and `SEMBLE_BIN` empty with `SEMBLE_ENABLED: true`, and zero runtime fallbacks. So the judge never queried Semble in that run; when it does, `binary-unavailable` is logged (`semble_helpers.sh:304`). This is benign, but worth watching.

## AI Memory Health

- **Telemetry found:** one `AI_MEMORY_TELEMETRY` line, in 40046483206 (`issue_pr_status`):
  `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues","source":"issue_pr_status.yml"}`. This is an expected no-op for the forward-merge PR #8027.
- **retrieve operations:** none observed. Hit rate, `estimated_tokens` against budget, and `keyword_method` mix cannot be computed.
- **Gap:** nearly all implement, plan and clarify runs in the window were skipped (149/152, 146/152, 152/155). The 12 telemetered review runs have no `log_summary` or deep logs here. Memory retrieval is effectively unobserved.
- **Recommendation:** have `cost_audit.py` count `AI_MEMORY_TELEMETRY` `op=retrieve` lines (with `records_selected` and `fail_open`) into the per-run telemetry. That puts them in the analysis context without depending on sampled deep dives.

## GH API Call Audit

- **Observed budget signals (shared PAT, same reset epoch 1792803971):**
  - `GH_PAT_BUDGET phase=end workflow=review_autofix job=post-merge-validate-dispatch remaining=4008 used_in_job=0` (40046483255, ended about 00:49).
  - `GH_PAT_BUDGET phase=end workflow=review_autofix_sweep job=sweep remaining=3624 used_in_job=4` (40047797860, about 01:01).
  - That is **384 calls in about 12 min** across all workflows, roughly 32/min. Projected over an hour, that is about 1,900 calls, or about 38% of a 5,000/h budget (inference: it assumes a standard PAT limit and an even rate). No 429s appear in any log summary.
- **Per-workflow notes:**
  - lint_pr_body_auto_close (40047534270, 40046567126): one paginated `pulls/<n>/commits` call per run. This is already minimal.
  - check_failure_triage_historical (40046483766): 3 call types (PR lookup, run lookup, job-log probe), and it stopped at `already_filed`. Moving the dedup check before the job-log probe would save 1 call per duplicate (inference: call order is not visible).
  - autofix sweep: `used_in_job=4` for 3 candidates, all `active_run` skips. Efficient.
- **The heaviest consumer is invisible.** The poller (`orchestrate_poll.yml`) emits `GH_PAT_BUDGET` (6 occurrences in the workflow), but run 40046307682 had 866,524 chars truncated. The collector parses those lines only in `build_pat_budget_report`, which runs only with `--pat-budget-day` (`collect_workflow_logs.py:1726, 1978-1984, 2327-2332`), so per-family PAT usage never reaches the analysis context.
  - **Change:** sum `used_in_job` per workflow family from the log archives the collector already downloads, and add it as an additive `gh_pat_used_in_job` field. No new API calls.
  - **Impact:** brings the main GH API hotspot into view.
- **Hygiene rules (§14):** no new per-item loops observed in this window. The batching helpers `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` can't be audited without the poller body.

## Prompt Cache & Memory System

| Run | cache_hit_rate (reported) | recomputed | or_calls | CONTEXT_BUDGET_WARN |
|---|---:|---:|---:|---:|
| 40043977269 | 0.903406 | 0.903 | 20 | 0 |
| 40028532100 | 0.888135 | 0.888 | 10 | 0 |
| 40040550203 | 0.867626 | 0.868 | 20 | 0 |
| 40034624820 | null | 0.856 | 12 | 1 |
| 40036562451 | 0.850788 | 0.851 | 10 | 0 |
| 40036692115 | 0.841808 | 0.842 | 11 | 0 |
| 40043597123 | 0.816188 | 0.816 | 20 | 0 |
| 40036337074 | null | **0.722** | 12 | 1 |
| **Aggregate** | null | **0.861** | 115 | 2 |

- **Fragmentation signal.** 40036337074 is about 14 points below the median. It has a high cache write (470,221) relative to its reads, which is consistent with an unstable prefix or a prompt grown past the cached region (inference).
- **Fix 1.** Add an additive `cache_hit_rate_available_only` field to `compute_cache_hit_rate`'s callers, computed over calls with usage available. Leave the existing fail-closed `cache_hit_rate` untouched (§10). The name is unique; it appears only in `analysis/`.
- **Fix 2.** Carry the `phase=` value from CONTEXT_BUDGET_WARN into the run row, so cache drops can be tied to the phase that is growing.
- **Expected impact:** restores the headline metric, and lets future reports find which phase is growing past its cache.

## Orchestrator Health

- **Poller cadence.** `orchestrate_poll` had 40 runs: 18 success and 22 cancelled, all `cancelled_before_first_step` (for example 40047109469 at 451 s, 40046736883 at 276 s, 40037238597 at 775 s).
  - The cron is `*/5 * * * *` (`internal-orchestrate-poll.yml:5`), but p50 is 420.5 s and p95 1,235.8 s.
  - With the concurrency group `ai-orchestrate-poll-…` and `cancel-in-progress: false` (`orchestrate_poll.yml:72-73`), GitHub keeps one pending run and replaces older pending runs. This is expected supersession, not failure.
  - The effective poll interval is the run duration (about 7–20 min).
  - **Mitigation:** have the collector report these as `superseded_pending` rather than cancelled.
  - **Diagnostic addition:** emit a single end-of-run `ORCHESTRATE_POLL_PHASE_TIMING` line (prefix not in use; no `*_TIMING` marker exists in `scripts/orchestrate_poll*.sh`). It survives the 866 K-char truncation seen in 40046307682 and shows which phase dominates the 17 min.
- **Project #7913 (integration PR #7922).** 4 of 10 sub-issues are unchecked: p1d-validate-yml-heal-report-job, p4-validation-cycle-derivation, p2c-poller-hold-limit-judge-request-docs and p3b-unblock-judge-heal-report-exec (40047534281). The PR has no override label. Lint passed with `Refs #7913` and 38 commits (40047534270), so it is §21-compliant.
  - **Indicator to track:** unchecked count over time. If it stays at 4 for more than 24 h, the poller has stopped dispatching waves.
- **Project #7973 (PR #7975).** All 4 sub-issues are ticked and the PR is ready (40046567130). Validate run 40034823925 passed in 851 s. Healthy.
- **Promote cycle is frozen.** `PROMOTE_CANDIDATE_FOLLOW outcome=frozen protected=true` appears in both 40046483590 and 40047282844, with `PROMOTE_CYCLE_FREEZE_MAX_SECS: 172800`. Auto-release dispatched test-and-mark-stable 40047940209 for stable tip 8bf3763 (40047921118).
  - **Indicator:** freeze age against the 48 h cap.
- **Stale queued runs.** 40046906706 logged "preserved 3 active run(s) without pull-request linkage": 34748435167 and 34748435180 (queued since 2026-09-13) and 32215723558 (since 2026-08-19). Those are 41–66-day-old orphans.
  - **Smallest mitigation:** have the scheduled cleanup cancel unlinked queued runs older than 7 days and log a line per cancel.
- **Clarification and heal loops.** All 152 orchestrate_clarify_respond and all 46 workflow_failure_heal runs were skipped. There are no active loops and no stuck states visible.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Ordered fix |
|---|---|---|---|
| review/autofix | Compute plus repeat rounds | p50 944 s, p95 2,424.9 s; 20-call runs at 2,288–2,424 s | 1. Surface iteration count, then gate round 2 |
| CI gate | Queueing in bursts, then a long shard | Burst runs at 1,521–2,311 s against about 860 s; `tests-release-and-log-analysis` takes 8–14 min | 2. `job_queue_ms` and `slowest_steps` telemetry, then split the shard |
| orchestrate poll | Compute (opaque) plus supersession | 1,224 s run with a 3 min queue; 22 superseded | 3. `ORCHESTRATE_POLL_PHASE_TIMING` |
| implement / plan | Mostly skipped. Successful slow runs: implement 40033748989 (2,139 s), 40033287210 (1,308 s); plan 40032075657 (1,054 s) | Sampled only | Collect a deep dive next window |
| clarify → plan → implement fan-out | Each issue event fires 4 workflows (for example the 4-way runs at 00:57:53, 00:57:18 and 00:52:33). 1–2 of each group hold a runner for 6–11 s | Run timestamps in recent_runs | 4. Check `on:`/`if:` filters so skips resolve without a runner (inference) |
| merge/conflict | Forward-merge conflict on `test-and-mark-stable.yml` (PR #8027); no conflict-heal retries observed | 40046483255 | Watch; no action |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - review_autofix wall-clock (p95 2,424.9 s).
  - CI under runner contention (p95 2,143.75 s).
  - The poller runs longer than its 5-min cron.
- **Top failure modes:**
  - The unclassified review "Checkout PR head branch" failure (40034302059, recurring).
  - Two CI contract-test failures (40034624682, 40033407420).
- **Highest-cost driver:** review_autofix, at 222.25 M tokens over 12 telemetered runs. 2-round runs cost 32–54 M each.
- **Top 3 actions:**
  1. Land `REVIEW_CHECKOUT_FAIL reason=…` on every `exit 1` in `review_autofix.yml:2832+`, with an `agents.d` fragment. This is log-only and has been outstanding for 6 reports.
  2. Add `cache_hit_rate_available_only`, CONTEXT_BUDGET_WARN `phase`, and `reason=` on usage-unavailable lines to `cost_audit.py`.
  3. Add `job_queue_ms`, `slowest_steps` and `gh_pat_used_in_job` to the collector's run rows. They come from data already fetched, with no new API calls.

## Metrics Appendix

**Runs by family**

| Family | Runs | Success | Fail | Cancelled | Other/skipped | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| ci | 46 | 44 | 2 | 0 | 0 | 948.5 | 2,143.75 |
| review_autofix | 55 | 54 | 1 | 0 | 0 | 944.0 | 2,424.9 |
| orchestrate_poll | 40 | 18 | 0 | 22 | 0 | 420.5 | 1,235.8 |
| validate | 3 | 3 | 0 | 0 | 0 | 719.0 | 837.8 |
| clarify | 155 | 3 | 0 | 0 | 152 | 1.0 | 10.0 |
| implement | 152 | 3 | 0 | 0 | 149 | 1.0 | 10.0 |
| plan | 152 | 6 | 0 | 0 | 146 | 1.0 | 11.0 |
| orchestrate_clarify_respond | 152 | 0 | 0 | 0 | 152 | 1.0 | 9.0 |
| workflow_failure_heal(+intake) | 46 + 56 | 0 + 3 | 0 | 0 | 46 + 53 | 1.0 | 9.5 / 13.5 |
| check_failure_triage | 51 | 2 | 0 | 0 | 49 | 1.0 | 10.0 |
| other (10 families) | 92 | 92 | 0 | 0 | 0 | — | — |
| **Total** | **1,000** | **228** | **3 (0.3%)** | **22** | **747** | 1.0 | 1,055 |

**Token and cache totals (review_autofix only; Codex and Claude: 0)**

| Metric | Value |
|---|---:|
| or_calls (available / unavailable) | 115 (113 / 2) |
| or_prompt_tokens (uncached) | 24,435,761 |
| or_cache_read_tokens | 190,417,137 |
| or_cache_write_tokens | 6,222,758 |
| or_completion_tokens | 1,178,413 |
| or_total_tokens | 222,253,135 |
| cache_hit_rate (reported / recomputed) | null / 0.861 |
| wall_clock_p50_ms / p99_ms (all) | 4,000 / 2,476,140 |
| wall_clock_p50_ms / p99_ms (review_autofix) | 2,330,000 / 2,683,920 |
| break_glass_count | 0 |
| context_budget_warn_count | 2 (40036337074, 40034624820) |

**Semble**

| Metric | Value |
|---|---:|
| query_calls | 11 |
| query_bytes (avg/query) | 162,281 (14,753) |
| sources_total | 84 |
| static_dup_bytes_total | 3,123 (1.9%) |
| bootstraps / failed / unused | 11 / 0 / 0 |
| bootstrap_ms_total (avg) | 437,550 (39,777) |
| fallbacks: contract-test / runtime | 52 / 0 (CI only) |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | fallbacks | response_bytes |
|---|---:|---:|---:|---:|---:|---:|
| serena (all) | 0 | 0 | 0 | 0 | 0 | 0 |
| semble (review_autofix) | n/a | n/a | n/a | 11 | 0 runtime | 162,281 |

Other MCP servers observed: none.

**GH API**

| Signal | Value |
|---|---|
| PAT remaining 4008 → 3624 (same reset window) | 384 calls in about 12 min |
| 429 / secondary rate-limit events | 0 observed |
| Poller per-run PAT usage | not visible (log truncation; budget report not in context) |

**Data gaps**

- The deep-dive log directory was not mounted.
- `success_sample_rate` is 0.07.
- 747 of 1,000 runs were skipped.
- Implement and plan successes have no telemetry.
- The poller logs were truncated at 866,524 chars.
- No `AI_MEMORY_TELEMETRY` retrieve operations were observed.
- There is no dollar-cost field.
## Deep Audit — Workflows & Scripts (2026-10-24)

Scope: I read the pre-audit context (`README.md`, `agents.md` sections "Workflow file size limit", "Repo-specific batching helpers", "Utility helpers", the `CLAUDE.md` section index, and `.github/ai/label_contract.v1.json`), plus the in-progress report. I then audited these in depth: `review_autofix.yml` (codex-agent job, checkout step, notification steps, `if:` gates), `implement.yml` (largest run bodies and all `${{ }}` interpolations), `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `lint-pr-body-auto-close.yml`, `review_autofix_sweep.yml` (snapshot logic), `validate.yml` (workspace block), `scripts/orchestrate_poll_process.sh` (wave reconcile, backward scan, ready-to-merge, superseded check, revert paths, batch helpers), `scripts/gh_helpers.sh` (retry core, timeline helpers), `scripts/label_helpers.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/git_ref_health_check.sh`, two `review_autofix_step_*.sh` bodies, and `tests/test_workflow_file_size_limit.py`.

Limits: no shell was available in this audit, so byte and character counts below are estimates from line counts and observed line lengths. They are marked as such. Findings that cross-reference the in-progress report use its section names (for example "Reliability #1").

### Section 1: Bug & Correctness Sweep

**BUG-001** — `.github/workflows/review_autofix.yml:2832-2923` (with `:2229-2253` and `:5357-5358`)
- **Severity:** High
- **Category:** `bug`
- **Description:**
  - "Checkout PR head branch" is the only step in this part of the codex-agent job that has no `if: env.PR_CLOSED != 'true'` guard. Its neighbours all have one: `:2421`, `:2452`, `:2475`, `:2485`, `:2793`, `:2969`.
  - `PR_CLOSED=true` is set by "Check PR state (defense-in-depth)" at `:2250-2253` when the PR is closed or merged.
  - For a merged PR whose head branch has been auto-deleted, two things happen:
    - The first SHA comparison (`:2851-2854`) still passes, because `.head.sha` of a merged PR does not change.
    - Then `git fetch ... "+refs/heads/${HEAD_REF}:..."` (`:2912`) fails 4 times and the step takes the bare `exit 1` at `:2916-2917`.
  - The same race also exists without the flag. Several minutes of setup (OpenCode install, Semble index, memory) separate the state check at `:2229` from the fetch at `:2912`, and a PR can merge in that window, for example through auto-merge from an earlier round.
  - Every failure-alert step is gated `failure() && env.PR_CLOSED != 'true'` (`:5357-5358`). A run that hits this path is therefore red but posts no failure comment or fingerprint. That matches the "no recorded cause" pattern in the report's Reliability #1 for runs such as 40034302059. That match is an inference; I could not confirm branch deletion for those runs. [NEEDS VERIFICATION]
- **Recommended fix:**
  - Add `if: env.PR_CLOSED != 'true'` to the step.
  - Inside the fetch-exhausted branch, before `exit 1`, probe the remote ref and the live PR state (helper `gh_review_pr_state`, `scripts/gh_helpers.sh:83`) and skip cleanly when the branch is gone:
    ```bash
    if ! git ls-remote --exit-code --heads origin "${HEAD_REF}" >/dev/null 2>&1
    then
    	echo "REVIEW_CHECKOUT_FAIL reason=head_ref_deleted pr=${PR_NUMBER:-unknown} head_ref=${HEAD_REF}"
    	echo "PR_CLOSED=true" >> "$GITHUB_ENV"
    	exit 0
    fi
    ```
  - Register the `REVIEW_CHECKOUT_FAIL` prefix through an `agents.d/<pr>-review-checkout-fail.md` fragment (§25). This also completes the report's Reliability #1 recommendation.

**BUG-002** — `.github/workflows/review_autofix.yml:2836-2837, 2872-2875`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The report lists five bare exit paths in this step. There is a sixth: `HEAD_REF="$(jq -r '.headRefName' "${PR_META_FILE}")"` runs under `set -euo pipefail` (`:2834`). An unreadable or invalid `PR_META_FILE` aborts the step with only jq's stderr and no `::error::`.
  - The empty-ref branch at `:2873` prints a plain `echo` with no `::error::` annotation, so the failure-heal fingerprinting cannot classify it.
- **Recommended fix:**
  - Use `HEAD_REF="$(jq -r '.headRefName // ""' "${PR_META_FILE}" 2>/dev/null)" || { echo "::error::REVIEW_CHECKOUT_FAIL reason=pr_meta_unreadable"; exit 1; }`, and the same pattern for `HEAD_REPO`.
  - Change `:2873` to `echo "::error::REVIEW_CHECKOUT_FAIL reason=head_ref_missing ..."`.
  - Cross-reference: this extends the report's Reliability #1; it is not a duplicate.

**BUG-003** — `scripts/orchestrate_poll_process.sh:26126-26247` (merge at `:26207`, branch update at `:26220-26222`)
- **Severity:** High
- **Category:** `bug`
- **Description:**
  - The ready-to-merge path picks a PR with `RTM_PR="$(_issue_cross_ref_pr_number_last "${rtm_issue}" ...)"` (`:26129`). That is the last same-repository PR that cross-references the issue.
  - If that PR is open, mergeable and green, the code squash-merges it with `_orch_squash_merge_bound "${RTM_PR}" ...` (`:26207`) or PUTs `update-branch` on it (`:26220`). It never calls `_pr_json_is_issue_implementation_pr` (`:19195-19212`).
  - The wave-reconcile loop explicitly guards against this: "Inspect every cross-reference so a later mention-only PR cannot mask an earlier implementation PR" (`:25860-25875`).
  - Any later PR whose body or commit mentions `#N` becomes the "last" cross-reference. Examples: a sibling PR, an activation-fix PR, a heal PR, or an integration-PR body edit.
  - The same unverified helper is used at 9 other sites: `:18114`, `:21493`, `:21599`, `:21910`, `:22670`, `:26275`, `:26421` (the review-blocked judge, which labels the issue `ai:closed` at `:26441-26447` when that PR is closed and unmerged), `:29011`, and `:29596` (`PR_TO_REVERT`, which opens a revert PR).
  - [NEEDS VERIFICATION: how often a mention-only PR is the last cross-reference in practice.]
- **Recommended fix:**
  - Add `_issue_implementation_pr_number <issue>`. It iterates `_issue_cross_ref_pr_numbers_unique`, calls `_fetch_pr_json`, filters with `_pr_json_is_issue_implementation_pr`, and prefers merged, then open (the same logic as `:25865-25884`).
  - Replace `_issue_cross_ref_pr_number_last` at the merge, update-branch, revert and label-closing sites (`:26129`, `:26275`, `:26421`, `:29596`) first.
  - Log `LINKED_PR_CROSS_REF_REJECTED ... reason=not_implementation_pr`, a prefix the file already uses at `:25873`.

**BUG-004** — `scripts/orchestrate_poll_process.sh:26126` and `:26248` (also `:26273`, `:29594`, `:9442`, `:29661`); state function at `:5696-5710`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - These loops are written as `echo ... | jq ... | while read -r ...; do`, so each loop body runs in a subshell.
  - In the ready-to-merge loop, `refresh_integration_backpressure_gate_after_merge` (`:26213`) recomputes the global `INTEGRATION_BACKPRESSURE_BLOCK_MERGES` (`:5697-5708`). The new value is discarded at `done` (`:26248`).
  - Later reads in the same tick still see the pre-merge value: `:25498` (backward-scan merge gate) and `:26033`, reached by subsequent tracking projects in the outer loop. That could allow a merge past the ahead-by threshold. [NEEDS VERIFICATION: whether the gate is recomputed per project before `:25498`.]
  - `review_autofix_sweep.yml:441` already documents the fix: "Read into an array so counters survive the loop (no subshell pipe)."
- **Recommended fix:** Rewrite as `mapfile -t _rtm_issues < <(printf '%s' "${WAVE_STATUS}" | jq -r '...')` followed by `for rtm_issue in "${_rtm_issues[@]}"`. Do the same for `:26273`, `:29594` and `:29661`.

**BUG-005** — `.github/workflows/implement.yml:5616-5633`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - On the "Handle no-op implementation" fall-through, the comment POST `gh_retry gh api "repos/.../issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (`:5616-5617`) has no `|| true` and runs under `set -euo pipefail` (`:5407`).
  - `gh_retry` makes only one attempt for a POST create (`scripts/gh_helpers.sh:1067-1069`).
  - So one transient failure aborts the step before the label-create and `--add-label 'ai:implementation-failed'` edits (`:5622-5633`). The orchestrator's re-issue path keys on that label.
  - Guards 0 and 2 in the same step use `|| true` (`:5454`, `:5588`).
- **Recommended fix:** Apply the label first (`:5622-5633`), then post the comment with `|| echo "::warning::IMPLEMENT_NOOP_COMMENT_FAILED issue=${ISSUE_NUMBER}"`. If adding a new prefix, register it with an `agents.d` fragment.

**BUG-006** — `.github/workflows/implement.yml:5470`
- **Severity:** Low
- **Category:** `bug`
- **Description:** `if printf '%s' "${remaining_changes}" | grep -qE '\.codex-workflow-src(-main)?(/|$)'` runs under `pipefail`. `grep -q` exits on the first match. If `remaining_changes` exceeds the pipe buffer (about 64 KiB), `printf` gets SIGPIPE (status 141), the pipeline is false, and the pathspec hard-fail guard (Guard 1) is silently skipped.
- **Recommended fix:** `if grep -qE '\.codex-workflow-src(-main)?(/|$)' <<< "${remaining_changes}"; then`.

**BUG-007** — `.github/workflows/cancel_on_pr_close.yml:14-16`; trigger at `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The concurrency group `cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false` is shared by the `*/5` schedule and every `pull_request: closed` event.
  - GitHub keeps one running and one pending run per group, and a newer pending run replaces the older one. A PR-specific cancellation queued behind a running tick can be replaced by the next schedule tick or another close event.
  - Recovery then depends on the schedule path, so closed-PR runs keep holding runners for at least one more tick. That matters during the bursts the report describes.
- **Recommended fix:** Use `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event.pull_request.number || 'schedule' }}`. PR-close runs then never displace one another, and the schedule path stays serialized.

**BUG-008** — `.github/workflows/review_autofix.yml:5053-5056, 5206-5208, 5355`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - Both Telegram steps call `source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"` with no existence check and no `set -e`.
  - In "Telegram review-blocked judge decision" the last command is `tg_send_tracked ...` (`:5355`). If the helper did not load, the step exits 127 and turns a successful review job red at a notification step.
- **Recommended fix:** Add `type tg_send_tracked >/dev/null 2>&1 || tg_send_tracked() { echo "::warning::tg_helpers.sh unavailable; alert skipped"; }` after the `source`. This mirrors the existing `gh_retry` stub idiom (`:5056`).

**SEC-001** — `.github/workflows/lint-pr-body-auto-close.yml:49-56, 112-115`
- **Severity:** Medium
- **Category:** `security`
- **Description:**
  - The required §21 check runs `python3 scripts/lint_pr_body_auto_close.py` from `actions/checkout@v5` with the default `pull_request` ref, which is the PR merge commit.
  - A PR can therefore edit the lint script and get a pass for its own body or commits. That defeats the enforcement added after the #2734 stall.
  - The token is read-only (`:37-42`), so this is a gate-integrity risk, not an exfiltration risk.
- **Recommended fix:**
  - Check out the script from the base SHA into a separate path: `ref: ${{ github.event.pull_request.base.sha }}`, `path: .lint-base`, `sparse-checkout: scripts/lint_pr_body_auto_close.py`.
  - Invoke `.lint-base/scripts/lint_pr_body_auto_close.py`.
  - This follows the SHA-pinned helper pattern in `agents.md:2557-2567`. Changes to the script itself stay covered by its CI tests.

**SEC-002** — `.github/workflows/review_autofix.yml:2068-2077` (same pattern at `implement.yml:1320`, `plan.yml:348`, `clarify.yml:367`, `orchestrate.yml:134`, `orchestrate_clarify_respond.yml:379,1409`, `orchestrate_poll.yml:434`, `memory_maintenance.yml:58`, `issue_pr_status.yml:43`)
- **Severity:** Low
- **Category:** `security`
- **Description:**
  - `git remote set-url origin "https://x-access-token:${GH_TOKEN}@..."` writes the PAT in plain text into `.git/config` of the host checkout.
  - In `review_autofix.yml` that checkout is the PR tree whose files reviewer agents read.
  - `implement.yml:603-606` already uses the non-persisting form `git -c "http.extraHeader=Authorization: Basic ..."`.
  - Incremental exposure is limited, because `actions/checkout` persists an extraheader by default. [NEEDS VERIFICATION: whether isolated agents (`agents.md` "Isolated Codex agents") can read the host `.git/config`.]
- **Recommended fix:**
  - Replace persisted URLs with per-command `git -c http.extraHeader=...` through a shared helper (see DUP-001).
  - At minimum, do this in `review_autofix.yml`, and run `git remote set-url origin "https://${SERVER_HOST}/${GITHUB_REPOSITORY}"` in the existing "Cleanup" step.

**SEC-003** — `.github/workflows/implement.yml:1958-1971`
- **Severity:** Low
- **Category:** `security`
- **Description:**
  - `${{ steps.baseline_refctx.outputs.branch }}` and `.status` are interpolated straight into shell.
  - This is safe today only because the branch passes `re.fullmatch(r"ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+", branch)` (`:1167`), and the step is gated on a trusted author association (`:1058-1061`).
  - Every other untrusted event field in the repo goes through `env:` (for example `implement.yml:1014`, `lint-pr-body-auto-close.yml:60-61`).
- **Recommended fix:** Pass `BASELINE_STATUS`, `BASELINE_BRANCH` and `CHECKOUT_BASELINE_OUTCOME` through the step's `env:` and reference `"${BASELINE_BRANCH}"`. This is defense in depth and also shortens the expression.

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001** — `scripts/orchestrate_poll_process.sh:25841-25893`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:**
  - On every poll tick, for every current-wave issue, the code makes one timeline call (`_issue_cross_ref_pr_numbers_unique` → `gh_issue_timeline_with_cross_refs`, which is 1 GraphQL call, falling back to REST plus N, see BATCH-003). It then makes one REST `pulls/<n>` call per cross-referenced candidate (`_fetch_pr_json`, `:25867`).
  - The in-code §15 note (`:25863-25864`) says the extra call is needed because the timeline omits PR body and head.
  - Current count: N + ΣK. For a 10-issue wave with about 1.5 cross-references each, that is about 25 calls per project per tick.
  - Projected: ceil(N/25) GraphQL calls (1 for the example). Bodies are large, so a batch size of 10–15 may be safer, giving 1–2 calls. [NEEDS VERIFICATION: GraphQL node and point cost with `body`.]
- **Recommended fix:**
  - Add a sibling of `_fetch_linked_pr_status_graphql` (`:18857-18961`) that requests every same-repository `CrossReferencedEvent` PR (not only `willCloseTarget`) with `number state merged headRefName body author { login } headRepository { nameWithOwner }`.
  - Emit a dict keyed by issue in the REST-compatible shape that `_pr_json_is_issue_implementation_pr` reads (`.head.ref`, `.body`, `.merged_at`).
  - Cache it per cycle next to `_current_wave_details_json` (`:25812`). Fail open to the existing per-item loop on a cache miss (§14).

**BATCH-002** — `scripts/orchestrate_poll_process.sh:9282-9329`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:**
  - The superseded-by-main check walks every issue in every wave (`:9272`). Per issue it makes one timeline call; per PR it makes one `_fetch_pr_json` call (`:9302`) and one paginated `pulls/<n>/files` call (`:9315`).
  - It also fetches files for merged PRs before it finds the open PR that triggers the early `return 0` (`:9310-9312`).
  - Current count: N + P + P(+pages). For 10 issues and 12 PRs that is about 34 calls.
  - Projected: 1 aliased GraphQL call per 25 issues, with `timelineItems(CROSS_REFERENCED_EVENT) { source { ... on PullRequest { number state merged files(first: 100) { nodes { path } pageInfo { hasNextPage } } } } }`, plus REST `files` only for PRs where `hasNextPage` is true. That gives 1–2 calls.
- **Recommended fix:** Extend the `_fetch_linked_pr_status_graphql` pattern. Evaluate the "any open PR → return" condition on the batched states before any per-PR file fetch.

**BATCH-003** — `scripts/gh_helpers.sh:1646-1665` (fallback triggered at `:1764-1787`)
- **Severity:** Low
- **Category:** `api-batching`
- **Description:**
  - `_gh_issue_timeline_with_cross_refs_rest` makes one paginated REST timeline call and then one `gh api "${pr_url}"` per cross-referenced PR inside a `while read` loop.
  - It runs whenever the GraphQL path fails, returns errors, or has `hasNextPage=true` (`:1783-1786`). Long-lived issues with more than 100 cross-reference or close events therefore always pay N+1.
  - Current count: ceil(T/100) + K. Projected: ceil(T/100) GraphQL calls, using cursor pagination with `after:` and the PR fields inlined. [NEEDS VERIFICATION: how often `timeline_has_next_page` fires; grep `rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs` in the poller logs.]
- **Recommended fix:** In `gh_issue_timeline_with_cross_refs`, follow `pageInfo.endCursor` (bounded at 5 pages) instead of falling back to REST on `hasNextPage`. Keep REST only for real GraphQL failures.

**API-001** — `.github/workflows/implement.yml:6114-6195`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - Each failed `gh pr create` attempt (up to 3) re-paginates the whole open-PR inventory: `pulls?state=open&per_page=100` with base64 bodies (`:6132-6134`). It also re-reads the issue timeline (`:6144-6157`).
  - Only one head (`TARGET_BRANCH`) needs checking.
  - Current count: per attempt ceil(O/100) + 1, times 3. With 150 open PRs that is 9 calls.
  - Projected: 1 call per attempt to `pulls?head=${owner}:${TARGET_BRANCH}&state=open&per_page=1`, plus timeline and candidate bodies only on a miss (1 aliased GraphQL call for the candidates' `body`). That is 3–6 calls.
- **Recommended fix:** Query by `head=`. Fetch bodies only for timeline candidates with one aliased `pullRequest(number:)` GraphQL query, as in `cancel_on_pr_close.yml:196-256`.

**API-002** — `.github/workflows/implement.yml:5544-5562`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - The ancestor no-op walk makes 2 REST calls per hop: issue `.body` (`:5550`) and paginated comments (`:5554-5555`).
  - Current count: 2 × `IMPL_NOOP_ANCESTRY_THRESHOLD` (4 at the default of 2).
  - Projected: 1 GraphQL call per hop, `issue(number:) { body comments(last: 100) { nodes { body } } }`, so 2 at the default. Hops are dependent, so full batching is not possible.
- **Recommended fix:** Use one GraphQL query per hop with the same fail-open `break` semantics.

**API-003** — `.github/workflows/cancel_on_pr_close.yml:364-380`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - The per-run `POST .../actions/runs/<id>/cancel` loop is unavoidable, because there is no batch cancel endpoint.
  - But `gh_retry` classifies `-X POST` as an unsafe create and allows a single attempt (`scripts/gh_helpers.sh:1062-1069`). A transient 5xx leaves the run uncancelled until the next tick, and the code logs it as "likely already completed" (`:376-378`).
  - Call count is unchanged; reliability improves.
- **Recommended fix:** Cancel is idempotent, so use `gh_retry --idempotent gh api -X POST "${cancel_url}"`. That is the documented escape hatch (`gh_helpers.sh:1062-1065`).

Cross-reference: the report's "GH API Call Audit" says the poller's per-family PAT usage is invisible. BATCH-001 and BATCH-002 are the largest per-tick poller fan-outs found in code; I could not quantify them from telemetry.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — "Configure git auth for memory helper clones" in 9 workflows: `review_autofix.yml:2068-2077`, `implement.yml:1310-1320`, `plan.yml:348`, `clarify.yml:367`, `orchestrate.yml:134`, `orchestrate_clarify_respond.yml:379,1409`, `orchestrate_poll.yml:434`, `memory_maintenance.yml:58`, `issue_pr_status.yml:43`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The same 8-line inline body (empty-token guard, `SERVER_HOST` derivation, `git remote set-url` with the embedded token) is copied in each workflow. Two other sites hard-code `github.com` instead (`implement.yml:5647`, `review_autofix.yml:4676`).
- **Recommended fix:**
  - Create `scripts/git_auth_helpers.sh` with `configure_git_origin_auth <repo_slug>`. It would derive the host from `GITHUB_SERVER_URL` and set `http.<url>.extraHeader` instead of a token URL, which also fixes SEC-002.
  - Each workflow step becomes `bash "${SUPPORT_SCRIPTS_DIR}/git_auth_helpers.sh" "${GITHUB_REPOSITORY}"`.
  - Add the new script to `REQUIRED_BOOTSTRAP_SCRIPTS` in `scripts/stage_workflow_support.sh` and to `docs/INVENTORY.md`.

**DUP-002** — workspace lifecycle steps: `review_autofix.yml:2420-2498`, `implement.yml:1863-1931`, `validate.yml:~840-913`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - "Initialize workspace metadata", "Restore reusable workspace cache", "Finalize workspace metadata", "Finalize workspace contents" and "Activate workspace shell context" are near-identical across the three workflows. Compare `review_autofix.yml:2456-2472` with `validate.yml:873-889`, which differ only in the `${SUPPORT_SCRIPTS_DIR}` vs `scripts/` path and the `PR_CLOSED` guard.
  - Each "Finalize" body carries 7 to 9 `${{ steps.* }}` interpolations.
- **Recommended fix:**
  - Add a `workspace_init.sh finalize-metadata` subcommand that reads `WORKSPACE_META_*` and `WORKSPACE_CACHE_HIT` from `env:`. That moves the cache-matched-key logic (`:2461-2465`) out of three copies.
  - Alternatively, use a composite action `.github/actions/setup-workspace` that wraps metadata, cache and finalize.
  - Callers to update: the three workflows above.

**DUP-003** — linked-issue resolution fallback: `review_autofix.yml:4481-4500`, `scripts/review_autofix_step_mark_ready_to_merge.sh:52-74`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:33-55`
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - All three use the same chain `LINKED_ISSUES_JSON` → `LINKED_ISSUE_FALLBACK_NUMBERS_JSON` → `PR_META_FILE` title and body → live re-fetch → `extract_repo_scoped_issue_refs_from_text`, but fetch inconsistently. The workflow uses `_safe_gh_jq` with no retry (`:4491`); the scripts use `gh_retry gh api` (`mark_ready_to_merge.sh:62`, `mark_review_blocked_workflow_failure.sh:43`).
  - Both step scripts also carry their own copies of the `ensure_label_exists` and `set_issue_phase_label_resilient` fallbacks (`mark_ready_to_merge.sh:15-50`, `mark_review_blocked_workflow_failure.sh:11-29`).
  - Those fallbacks only POST the target label and never remove stale phase labels, unlike `label_helpers.sh:274-293`.
- **Recommended fix:** Add `resolve_review_linked_issue_numbers <repo> <pr_number> <pr_meta_file>` to `scripts/gh_helpers.sh` (cache-first, `gh_retry`, fail-open empty). Call it from all three sites, and drop the per-script label stubs in favour of failing loudly when `label_helpers.sh` is missing.

**DUP-004** — Telegram link preamble in `review_autofix.yml:4161-4172, 4907-4915, 4966-4967, 5057-5058, 5210-5215, 5510-5523`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `PR_URL="${{ github.server_url }}/${{ github.repository }}/pull/${PR_NUMBER}"`, `RUN_URL=...` and the optional `Issue:` line are repeated in 6 steps. Each copy adds `${{ }}` interpolations that turn the whole `run:` body into an expression (see EXPR-002).
- **Recommended fix:** Add `tg_review_links <pr_number> [issue_number]` to `scripts/tg_helpers.sh`, built from the runner's default `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID`. Replace the 6 preambles with it.

**DUP-005** — `.github/workflows/implement.yml:4226-4233, 5233-5240, 5326-5333, 5361-5368` vs workflow-level `env:` at `implement.yml:58-85`
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - Four steps redeclare the same 8 variables: `ALLOW_BULK_DELETE`, `BULK_DELETE_THRESHOLD`, `BULK_DELETE_THRESHOLD_MD`, `ENFORCE_FILES_TOUCHED`, `ALLOW_OUT_OF_SCOPE_FILES`, `ALLOW_WORKFLOW_EDITS`, `WRITE_GUARDS_ENABLED`, `SCOPE_LOCK_LABEL_ENABLED`.
  - Their expressions and defaults are identical to the workflow-level declarations (`:58, 61, 69, 70, 74, 83, 84, 85`).
  - That is 32 redundant lines, and if one default is changed in only one place the step will silently override the workflow value.
- **Recommended fix:** Delete the step-level copies. Steps inherit workflow `env:`, so variable names and behaviour are unchanged (§10).

**DUP-006** — `.github/workflows/review_autofix.yml:4226` and `:4256`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** "Enable auto-merge on PR" and "Mark linked issues ready to merge" carry the same 12-clause, roughly 700-character clean-review-tail `if:`. The second adds only `env.AUTO_MERGE_READY_LABELS_ALLOWED == 'true'`. A future edit to one gate but not the other would let labels and auto-merge diverge, which is the PR #2542 failure class cited at `:4214-4219`.
- **Recommended fix:** Compute `clean_review_tail=true|false` once, in an `id:`'d step or at the end of `review_autofix_step_partial_finalize_safety.sh`. Gate both steps on `steps.<id>.outputs.clean_review_tail == 'true'`, keeping the extra `AUTO_MERGE_READY_LABELS_ALLOWED` clause on the second.

**DUP-007** — `.github/workflows/implement.yml:5441-5454` and `:5575-5588`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The close-as-`ai:closed` sequence (label create, multi-`--remove-label` edit with fallback, `gh issue close --comment` with comment fallback) is duplicated verbatim inside one step.
- **Recommended fix:** Extract `close_issue_as_superseded <repo> <issue> <body>` into `scripts/label_helpers.sh`, using `ensure_label_exists` (which also fixes CONSIST-001). This also shortens the EXPR-001 body.

**DUP-008** — `gh_retry` fallback stubs: `review_apply_fixes.sh:29`, `review_run_reviewers.sh:14`, `review_conflict_prepare.sh:42`, `review_rb_judge.sh:244`, `orchestrate_poll_process.sh:783`, `validate_process.sh:433`, `label_helpers.sh:20`, `implement_stale_branch_check.sh:56-59`, `release_ci_gate.sh:58-61`; inline in workflows at `implement.yml:5409, 6056`, `review_autofix.yml:2238, 5056`, `cancel_on_pr_close.yml:148`
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - The no-retry stub `gh_retry() { "$@"; }` is repeated 14+ times.
  - `gh_review_pr_state` stubs are duplicated in `review_apply_fixes.sh:31-32` and `review_run_reviewers.sh:16-17`.
  - When `gh_helpers.sh` fails to load, all retry and rate-limit protection disappears silently.
- **Recommended fix:** Keep the stubs, since they exist for when the staged helper is absent. Make every stub emit one `::warning::GH_HELPERS_UNAVAILABLE caller=<file>` line so silent degradation becomes visible. Register the prefix through an `agents.d` fragment.

### Section 4: Expression Size Limit Risk Assessment

Context:
- CI already enforces an 18,000-character guard on every `${{ }}`-bearing `run:` body (`tests/test_workflow_file_size_limit.py:31-32, 66-98`). A High-tier (≥18K) block therefore cannot exist on a green `main`.
- Every count below is an estimate from line count × observed line length after YAML indentation stripping. None was measured. Confirm with `python3 tests/test_workflow_file_size_limit.py` or a `len(run)` dump.
- I verified that the four largest `implement.yml` bodies contain no `${{ }}` in the body, only in `env:`, so they are exempt: "Run Codex implementation" `:2833-4105` (~1,270 lines), "Attempt post-Codex syntax repair" `:4646-5169`, "Preflight destructive-commit guard" `:4234-4626`, and "Resolve trusted prior PR baseline branch" `:1017-1290`.

**EXPR-001** — `.github/workflows/implement.yml:5406-5633` ("Handle no-op implementation")
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The body is about 228 lines, with many 250–330-character `printf` prose lines (`:5436`, `:5476`, `:5483`, `:5570`, `:5572`).
  - Estimated 14,000–17,000 characters, which puts it in or near the Medium tier. Headroom to 21,000 is roughly 4,000–7,000. [NEEDS VERIFICATION]
  - Its only interpolations are `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` (`:5411`, `:5413`, `:5616`, `:5624`, `:5626`, `:5630`, `:5632`).
  - It is the most likely next 21K incident: three guards of prose comments have already accumulated.
- **Recommended fix:** Replace the 7 interpolations with the runner's default `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. The body then contains no `${{ }}` and is exempt from the limit. Applying DUP-007 also removes about 14 lines.

**EXPR-002** — `.github/workflows/review_autofix.yml:5206-5355` ("Telegram review-blocked judge decision") and `:5053-5191` ("Telegram editor-noop-suspicious warning")
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - About 150 lines (estimated 12,000–14,000 characters, headroom about 7,000–9,000) and about 139 lines (estimated 10,000–12,000 characters, headroom about 9,000–11,000).
  - Both grow by a `case` arm with every new judge outcome.
  - The only interpolations are `github.server_url`, `github.repository` and `github.run_id` (`:5057-5058`, `:5117`, `:5168`, `:5190`, `:5210-5215`).
- **Recommended fix:**
  - Same as EXPR-001: use the `GITHUB_*` default env vars, which exempts both bodies.
  - Longer term, move each body to `scripts/review_autofix_step_tg_<slug>.sh` using the documented resolving-wrapper pattern (`agents.md:2531-2551`).

**EXPR-003** — `.github/workflows/implement.yml:573-735` ("Resolve integration ref") and `:6053-6205` ("Create Pull Request")
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - About 163 lines (estimated 11,000–13,000 characters) and about 153 lines (estimated 11,000–12,000 characters), each with roughly 8,000–10,000 characters of headroom.
  - Interpolations: `${{ github.repository }}` and `${{ github.sha }}` at `:592-593`, `:677`, `:702`; `${{ github.repository }}` at `:6081`, `:6129`, `:6177`.
- **Recommended fix:** Replace them with `${GITHUB_REPOSITORY}` and `${GITHUB_SHA}`. Note that `github.sha` equals `GITHUB_SHA` for the caller event, which is the same value used today. Both bodies then become exempt.

**EXPR-004** — `tests/test_workflow_file_size_limit.py:66-98`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** The guard has four coverage gaps:
  - It scans only `.github/workflows/*.yml` `jobs.*.steps[].run`. Composite-action `run:` steps in the 6 actions under `.github/actions/*/action.yml` are not checked, and neither are the consumer templates under `workflow-templates/`.
  - Long `if:` and `with:` expressions are not measured. The largest found is about 750 characters (`review_autofix.yml:3855`, `4226`, `4256`), so there is no current risk.
  - There is no 15,000-character warning tier, so a body can creep from 15K to 17.9K unnoticed (EXPR-001).
  - Reusable `jobs.<id>.uses` jobs are skipped correctly, because they have no steps.
- **Recommended fix:**
  - Extend the glob to `.github/actions/*/action.y*ml` (`runs.steps[]`) and `workflow-templates/*.yml`.
  - Add a non-failing `print("WARN ...")` at 15,000 characters so the CI log shows creep. Optionally have `scripts/ci_budget_watch.py` open an `ai:ci-budget` issue at that tier, reusing its `kind=` marker scheme (`agents.md:2495-2505`).

**Workflow file size:**
- The prompt cites a 1 MB limit. This repo has measured the real limit at 512,000 bytes (`agents.md:2506-2512`, `tests/test_workflow_file_size_limit.py:26-27`), with a 480,000-byte CI guard and a 440,000-byte budget-watch warning (`agents.md:2495-2505`). No workflow can be near 800 KB.
- Largest by line count:
  - `implement.yml`: 7,159 lines
  - `test-and-mark-stable.yml`: 6,569 lines
  - `review_autofix.yml`: 5,755 lines
  - `plan.yml`: 3,241 lines
- I could not measure bytes. At about 60–65 bytes per line, `implement.yml` would be about 430–465 KB, possibly past the 440 KB warning. [NEEDS VERIFICATION: `wc -c .github/workflows/implement.yml`, and check for an open `ai:ci-budget kind=workflow_size subject=implement.yml` issue.]
- EXPR-001 to EXPR-003 remove expressions but not bytes. Byte relief needs the `scripts/` extraction pattern.

### Section 5: Cross-Cutting Concerns

**Dead code:** I confirmed no dead functions in the audited files. `_single_issue_linked_pr_status_graphql` (`orchestrate_poll_process.sh:18967`) and `_linked_prs_by_body_reference` (`:4524+`) both have live callers. `review_autofix.yml` declares `workflow_dispatch` at `:82`, so the sweep's listing of `review_autofix.yml` runs (`review_autofix_sweep.yml:437`) is not dead.

**TODO/FIXME/HACK markers:** none. A repo-wide search of `.github/workflows/*.yml`, `scripts/*.sh` and `scripts/*.py` returned 0 matches.

**DEBT-001** — stale line-number references in comments: `review_autofix.yml:4217` (":4436-4440"), `review_autofix.yml:4255` (":3315-3326"), `orchestrate_poll_process.sh:25471` ("lines 8176-8194"; the referenced merged-path code is at `:25425-25441`), `orchestrate_poll_process.sh:25476` ("line ~6463")
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** These comments cite line numbers that no longer point at the code they describe. That misleads reviewers and model editors who follow them.
- **Recommended fix:** Replace line numbers with stable anchors: step `name:` strings, function names (for example `close_merged_issues_sweep`) or marker strings.

**DEBT-002** — `.github/workflows/cancel_on_pr_close.yml:261-285`
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** The scheduled cleanup preserves unlinked active runs indefinitely and only lists them. The report's "Orchestrator Health" section records 41–66-day-old orphans: 34748435167, 34748435180, 32215723558.
- **Recommended fix:**
  - From the same snapshot (no new list call), cancel unlinked `queued` runs whose `created_at` is older than a new `CANCEL_UNLINKED_QUEUED_MAX_AGE_DAYS`. Use a default of `7` in the script, as §8 requires.
  - Log one `CANCEL_UNLINKED_STALE run=<id> age_days=<n> outcome=<canceled|failed>` line per run, and register the prefix in an `agents.d` fragment.
  - This cross-references the report and adds the code location.

**CONSIST-001** — `.github/workflows/implement.yml:5441-5443, 5575-5577, 5488-5490` vs `.github/ai/label_contract.v1.json:100-103, 156-159` and `scripts/label_helpers.sh:48, 62, 113, 127`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - Inline `gh label create` calls use colours and descriptions that differ from the contract:
    - `ai:closed`: `'6b7280'`, "Issue closed by automation (superseded, duplicated, or already complete)", vs contract `6a737d`, "Linked PR closed without merge".
    - `ai:needs-human`: `'b60205'` vs contract `e11d48`.
  - On a fresh consumer repo, whichever path runs first decides the label's metadata.
  - The repo has 44 inline `gh label create` sites across 14 files that bypass `ensure_label_exists`.
- **Recommended fix:** Route these through `ensure_label_exists`, which the same step already sources conditionally (`:5622-5624`). Separately, add a CI grep that rejects new inline `gh label create` with literal colours outside `label_helpers.sh`.

**CONSIST-002** — `scripts/review_collect_pr_metadata.sh:69-74`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - After sourcing `gh_helpers.sh` (`:35`), the script redefines `gh_retry()` with an outfile-first signature (`gh_retry_to_file "${outfile}" gh "$@"`). That shadows `gh_helpers.sh:1060` for every later function in the same shell.
  - It is harmless today, because the only helper used afterwards, `extract_repo_scoped_issue_refs_from_text`, makes no `gh` calls. Any future call to a `gh_helpers.sh` function that uses `gh_retry` internally would break silently.
  - This violates the §10 rule that new identifiers must not collide with existing ones.
- **Recommended fix:** Rename it to a unique `_collect_pr_meta_gh_to_file` and update its 7 call sites in this file (`:138`, `:194`, `:231`, `:235`, `:240`, `:247`, `:280`).

**CONSIST-003** — `.github/workflows/implement.yml:5647`, `.github/workflows/review_autofix.yml:4676`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** These push remotes hard-code `github.com`, while the auth steps derive `SERVER_HOST` from `GITHUB_SERVER_URL` (`implement.yml:1317-1320`, `review_autofix.yml:2074-2077`). That is inconsistent, and pushes would break on GHES.
- **Recommended fix:** Use the same `SERVER_HOST` derivation, or the DUP-001 helper.

**SHELL-001** — intentional unquoted word-splitting (SC2086/SC2206): `orchestrate_poll_process.sh:25412, 25760, 25798, 25841, 25865`; `review_autofix.yml:2144, 2160`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** Loops of the form `for inum in ${ISSUE_NUMS}` and `for f in ${REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS} ...` rely on word-splitting. The values are numeric or fixed filenames today, but any glob character in a value (for example from an LLM-produced state file) would expand against the CWD.
- **Recommended fix:** Use `read -ra _wave_issue_nums <<< "${ISSUE_NUMS}"`, the array already built at `:25797-25801`, or add `set -f` around the loop. Annotate any intentional cases with `# shellcheck disable=SC2086` and a reason.

Other patterns noted:
- I found no SC2006 backticks or SC2015 `A && B || C` misuse in executable lines; all matches were in comments.
- `local x="$(...)"` (SC2155) appears once in `orchestrate_poll_process.sh`.
- The pipe-to-while subshell pattern (BUG-004) is the main inconsistency. `review_autofix_sweep.yml:441` and `scripts/label_helpers.sh:275-293` use process substitution correctly.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BUG-003 |
| Medium | 9 | BUG-002, BUG-004, BUG-005, SEC-001, BATCH-001, BATCH-002, DUP-001, DUP-002, EXPR-001 |
| Low | 23 | BUG-006, BUG-007, BUG-008, SEC-002, SEC-003, BATCH-003, API-001, API-002, API-003, DUP-003, DUP-004, DUP-005, DUP-006, DUP-007, DUP-008, EXPR-002, EXPR-003, EXPR-004, DEBT-001, DEBT-002, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001 |

Correction: the Low row lists 24 IDs, not 23, so the total is 35 findings. The IDs are authoritative.

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3 (`.github/workflows/review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, `agents.d/<pr>-review-checkout-fail.md`) | Medium |
| API call optimization | 4 (`scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `.github/workflows/implement.yml`, `.github/workflows/cancel_on_pr_close.yml`) | Medium |
| Code modularization | ~16 (9 workflows with git-auth blocks, `validate.yml`, `scripts/workspace_init.sh`, `scripts/gh_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/label_helpers.sh`, 2 `review_autofix_step_*.sh`, plus a new `scripts/git_auth_helpers.sh`, `scripts/stage_workflow_support.sh` and `docs/INVENTORY.md`) | Large |
| Expression size reduction | 3 (`.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, `tests/test_workflow_file_size_limit.py`) | Small |
| Medium/Low fixes | ~9 (`implement.yml`, `review_autofix.yml`, `lint-pr-body-auto-close.yml`, `cancel_on_pr_close.yml`, `scripts/review_collect_pr_metadata.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh`, `agents.d/*` fragments for new prefixes) | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-24)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: all five preconditions were proven by reading the code. The calls hit the same endpoint (or a strict field subset) with the same filters and auth scope. They run in the same step or process with nothing in between that changes the response. The merge keeps the same error and retry behaviour, crosses no concurrency boundary, and keeps any documented cache contract. The implement stage may apply these without further review.
- **`NEEDS_VERIFICATION`**: the change looks correct, but at least one precondition can't be proven from static reading. The named check must pass first.
- **`RISKY_SKIP`**: a hard trigger applies: a retry, backoff or polling loop; pagination; an auth or identity probe; `orchestrate_poll_process.sh`; a race-defence re-read; or a change to log output. These are listed only so they are visible. They must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001**: `RISKY_SKIP`
- **Files:**
  - `scripts/review_enable_auto_merge.sh:155` (labels read)
  - `scripts/review_enable_auto_merge.sh:221` (full PR read)
  - `scripts/review_enable_auto_merge.sh:100` (head-SHA read; runs on the e2e path after `:157`)
- **Call count:**
  - Auto-merge path: 2 now → 1 proposed.
  - `e2e-smoke-test` path: 2 now (`:155` + `:100`) → 1 proposed.
- **Endpoints:** `GET repos/{o}/{r}/issues/{n}/labels?per_page=100` (paginated), `GET repos/{o}/{r}/pulls/{n}`.
- **Evidence:** both reads run in the same process. Between them there are only reads and early `exit 0`s.
  ```bash
  if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
  ...
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ```
  The pull object already contains `.labels[]` and `.head.sha`. The comment at `:144-153` justifies pagination by the 30-per-page default of the labels endpoint, a concern that does not apply to the pull object.
- **Proposed fix:**
  - Move the `:221` fetch above `:155`.
  - Derive `PR_LABELS_RAW` with `jq -r '.labels[]?.name'`.
  - On the e2e branch, compare `.head.sha` from the same payload instead of calling `reviewed_head_is_current_for_labels`.
- **Safety rationale:** `:155` uses `--paginate` (a hard trigger). The two calls also fail with different `::warning::` texts (`:168` and `:224`), so merging them changes observable log output.
- **Downstream signal:** do not auto-implement. Manual review must confirm three things:
  1. No consumer greps the `Could not fetch labels for PR` warning at `:168`.
  2. REST `pulls/{n}` returns every label on a PR carrying more than 30.
  3. Losing the separate head-freshness read on the e2e path is acceptable. It moves the freshness point about 0–1 s earlier.

**MERGE-002**: `RISKY_SKIP`
- **Files:**
  - `scripts/promote_main_cycle.sh:820-832`: idle-wait loop, `runs?per_page=30`.
  - `scripts/promote_main_cycle.sh:855`: pre-dispatch baseline, `runs?event=workflow_dispatch&per_page=50`.
- **Call count:** at least 2 now → 1 proposed (the last idle poll reused as the baseline).
- **Endpoint:** `GET repos/{o}/{r}/actions/workflows/{gate}/runs`.
- **Evidence:** the same endpoint is read twice back to back. The only work between the reads is `pin_freeze` and `require_stable_branch_ancestor` (`:837-854`). The filters are not a strict subset: `:821` has no `event=` filter and asks for 30 rows, while `:855` filters on `workflow_dispatch` and asks for 50.
- **Proposed fix:** have the final idle-wait iteration request `per_page=50` and save its response. Derive `before_gate_ids` from it with `select(.event=="workflow_dispatch")`.
- **Safety rationale:** both calls sit in dispatch-polling code. `:855` is the race baseline that `:872` uses to identify this cycle's gate run. Without the `event=` filter, the 50-row window can miss an older dispatch run, and that would misattribute the gate run.
- **Downstream signal:** do not auto-implement. A human must confirm the gate workflow never has more than 50 non-dispatch runs inside the window, and must accept that the baseline snapshot moves earlier than `pin_freeze`.

**MERGE-003**: `NEEDS_VERIFICATION`
- **File:** `.github/workflows/review_autofix.yml:910-949`. The per-issue `gh issue view` is at `:914`, inside "Dispatch standalone validate for orchestrator short-circuit issues".
- **Call count:** N now (one per fallback-resolved issue) → 1 aliased GraphQL call proposed.
- **Endpoint:** REST `GET repos/{o}/{r}/issues/{n}` (labels, via `gh issue view --json labels`) → GraphQL `repository{ iN: issue(number:N){ labels(first:100){nodes{name}} } }`.
- **Evidence:** labels are unknown only on the body/title fallback path (`:903` sets `labels: null`). Each such issue then costs one read:
  ```bash
  if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
  ```
  The closing-references path already gets labels in its own GraphQL call (`:856-861`).
- **Proposed fix:** before the loop, when any row has `labels_known != true`, make one aliased GraphQL call. Use the `_fetch_candidate_issue_details_graphql` pattern in `scripts/orchestrate_poll_process.sh` and the `_fetch_linked_issue_bodies_graphql` shape (`scripts/review_collect_pr_metadata.sh:87-181`). Merge the labels into `issue_nodes_json`. If the batch fails, fall back to the existing per-issue read.
- **Safety rationale:** error semantics change. Today a failure marks only one issue `validation_label_unknown` (`:921-923`); a batch failure would affect all of them unless the per-item fallback is kept. The loop also mutates state between reads (`gh issue edit --remove-label` at `:946`), although only on other issues.
- **Downstream signal:**
  1. Confirm the fallback path can return more than one issue in practice: grep the `post-merge-validate-dispatch` logs for `Found linked issues via PR body/title fallback:` with two or more numbers.
  2. Confirm the per-issue fallback keeps the existing `validation_label_unknown` semantics.
  3. If the fallback count is almost always 1, drop this finding: the saving is nil.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001**: `SAFE_TO_MERGE`
- **File:** `scripts/workflow_failure_heal_intake.sh`
  - `:975`: first `/user` read, stored in `PROVENANCE_LOGIN`.
  - `:1449-1455`: second `/user` read, into `HEAL_TRUSTED_AUTHOR`.
  - `:1288` and `:1615-1616`: existing reuse precedents.
- **Call count:** `autofix_failure` reports that carry a numeric `source_gen` make 2 calls now → 1 proposed. Other report kinds are unchanged.
- **Endpoint:** `GET /user`.
- **Evidence:**
  - `:974-976` reads `/user` for every non-`workflow_run` report except cross-repo `phase_failure` reports.
  - `:1288` forwards it as `PHASE_COMMENT_AUTHOR` only for `phase_failure`.
  - `:1450` reads only `PHASE_COMMENT_AUTHOR`, so an `autofix_failure` report reads `/user` a second time:
    ```bash
    HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-}"
    if [ -z "${HEAL_TRUSTED_AUTHOR}" ]; then
    	# §14 audit: heal issue lists identify issue authors, not the token's
    	# account; the only other /user read is for self-repo phase reports.
    	HEAL_TRUSTED_AUTHOR="$(gh_retry gh api --method GET user --jq '.login // empty' 2>/dev/null || true)"
    ```
  - The audit comment is wrong: `:975` also runs for `autofix_failure`.
  - The same file already reuses the value at `:1615`: `recovery_login="${PROVENANCE_LOGIN:-}"`, with the comment "Every candidate below is a /user result for the intake's own token."
  - A grep of the script found no `GH_TOKEN=` or `export GH_TOKEN`. Both reads run at script top level, not in a function or subshell (`:948-1015` and `:1448-1459`).
- **Proposed fix:**
  - At `:1450`, use `HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"`.
  - Keep the `:1454` live read as the fallback when both are empty.
  - Correct the comment at `:1452-1453`.
- **Safety rationale:** all preconditions hold:
  - Same endpoint and token, in the same process and step.
  - No mutation between the reads can change `/user`.
  - An empty or failed `:975` read (`|| true`) still falls through to the unchanged `:1454` read, so the failure path and retry behaviour are the same.
  - No log key changes.
  - The file already does this exact reuse at `:1615`.
- **Downstream signal:** in `scripts/workflow_failure_heal_intake.sh:1450`, replace `"${PHASE_COMMENT_AUTHOR:-}"` with `"${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"` and update the §14 comment at `:1452-1453`. Leave `:1454` in place as the fallback.

**REUSE-002**: `NEEDS_VERIFICATION` (extends API-002)
- **Files:**
  - `.github/workflows/implement.yml:5549-5550`: hop 1 of the ancestor no-op walk.
  - `.github/workflows/implement.yml:2683`: "Recheck security dependency before edits" writes `ISSUE_META_FILE`.
  - `.github/workflows/implement.yml:6093`: precedent for reading `ISSUE_META_FILE` after Codex runs.
- **Call count:** 2 × threshold now (4 at the default) → 2 × threshold − 1 (3). This is independent of API-002's GraphQL rewrite.
- **Endpoint:** `GET repos/{o}/{r}/issues/{ISSUE_NUMBER}` (`.body`).
- **Evidence:** hop 1 starts at `current="${ISSUE_NUMBER}"` (`:5548`) and calls `gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""'` (`:5550`). The same job, `implement` (`:92-7058`), already holds a fresh snapshot of that issue in `ISSUE_META_FILE`. It is exported at `:294` and rewritten at `:2683`, and the PR-create step already trusts it (`:6091-6094`).
- **Proposed fix:** for `hop == 1` only, read `.body` from `ISSUE_META_FILE`. Accept it only after the existing `issue_meta_matches_issue` check (`.number == ISSUE_NUMBER`, as defined at `:2041-2050`). Otherwise fall back to the live read. Leave hops 2 and later unchanged.
- **Safety rationale:** precondition 2 is unproven. The snapshot is in `/tmp/codex-implement-${GITHUB_RUN_ID}` (`:190`), and the Codex editor runs between `:2683` and `:5550`. A tampered body could forge `Re-issued from #N`, which steers which ancestor is counted and can close the issue `ai:closed` (`:5565-5566`).
- **Downstream signal:**
  1. Confirm the isolated Codex agent cannot write `${RUNTIME_DIR}/issue_meta.json`. Check the sandbox mounts in "Run Codex implementation" (`:2833+`) against `agents.md` "Isolated Codex agents".
  2. Confirm no step between `:2683` and `:5398` rewrites `ISSUE_META_FILE`.
  3. Apply the change only if both hold.

**REUSE-003**: `RISKY_SKIP` (extends BATCH-002)
- **Files:**
  - `scripts/orchestrate_poll_process.sh:9284`: timeline read.
  - `scripts/orchestrate_poll_process.sh:9301-9313`: per-PR `_fetch_pr_json` used only for `.state` and `.merged_at`.
  - `scripts/gh_helpers.sh:1711-1713` and `:1743-1745`: the timeline helper's existing enrichment.
- **Call count:** N + P + P(files) now → N + P(files, closed and merged PRs only). It removes the P state reads before any GraphQL batching.
- **Endpoints:** GraphQL `timelineItems(CROSS_REFERENCED_EVENT)`; REST `GET pulls/{n}`.
- **Evidence:** `gh_issue_timeline_with_cross_refs` already returns `.source.issue.state`, `.merged_at`, `.merged` and `lookup_failed` for each cross-referenced PR. Its GraphQL query asks for `number state mergedAt`. `_issue_cross_ref_pr_numbers_unique` (`:4473-4488`) throws those fields away and keeps only `.number`. Then `:9302-9304` fetches the full PR just to read the same two fields.
- **Proposed fix:** add a sibling of `_issue_cross_ref_pr_numbers_unique` that emits `number<TAB>state<TAB>merged<TAB>lookup_failed` from the same timeline response. Evaluate the "any open PR → return" rule at `:9310` from those fields. Call `_fetch_pr_json` only when `lookup_failed == true` or the state is empty.
- **Safety rationale:** the code is in `orchestrate_poll_process.sh`, and the timeline's PR state can lag the REST pull object, which the superseded-by-main gate relies on (an upstream-race defence). `:9315` is also paginated.
- **Downstream signal:** do not auto-implement. A human must check how far GraphQL `PullRequest.state` can lag REST `pulls/{n}.state` right after a merge. Then decide whether a stale `OPEN` reading, which returns early and leaves `SYNC_SUPERSEDED_CONFIDENT=true`, is acceptable for this gate.

**REUSE-004**: `RISKY_SKIP` (extends BATCH-001 and BUG-003)
- **Files:**
  - `scripts/orchestrate_poll_process.sh:25856`: wave-reconcile timeline read for every `ISSUE_NUMS` issue.
  - `scripts/orchestrate_poll_process.sh:26129`: ready-to-merge timeline re-read through `_issue_cross_ref_pr_number_last`.
  - `scripts/orchestrate_poll_process.sh:4455-4471`: `_issue_timeline_with_cross_refs_json`, which has no cache.
- **Call count:** R extra timeline calls per tick now (R = ready-to-merge issues) → 0 with a per-tick memo.
- **Endpoint:** GraphQL `issue.timelineItems`, with REST `issues/{n}/timeline` plus per-PR reads as the fallback.
- **Evidence:** both sites call `_issue_timeline_with_cross_refs_json` for the same issue in the same tick, and that function always goes to the network. The `:26129` loop iterates `WAVE_STATUS` issues with `status == "ready-to-merge"`; those are very likely a subset of the current-wave `ISSUE_NUMS` (inference). BUG-003 lists 9 more `_issue_cross_ref_pr_number_last` sites (`:18114`, `:21493`, `:21599`, `:21910`, `:22670`, `:26275`, `:26421`, `:29011`, `:29596`) with the same pattern.
- **Proposed fix:** add an associative-array memo keyed by issue inside `_issue_timeline_with_cross_refs_json`, cleared at the start of each project iteration, in the style of `_current_wave_details_json` (`:25812`). A failed fetch is cached as a miss so the next caller fails open.
- **Safety rationale:** the code is in `orchestrate_poll_process.sh`. The `:26126` loop runs in a pipe subshell (BUG-004), so a memo filled there would not survive. Squash merges at `:26207` also add cross-reference events within the tick, and a stale memo would hide them from later callers (`:26275`, `:26421`).
- **Downstream signal:** do not auto-implement. Land the BUG-003 helper (`_issue_implementation_pr_number`) and the BUG-004 subshell fix first. Then a human must decide, per call site, whether that site may read a timeline taken before this tick's merges and label edits.

**REUSE-005**: `RISKY_SKIP`
- **Files:**
  - `.github/workflows/review_autofix.yml:2240`: "Check PR state (defense-in-depth)", full `pulls/{n}` payload.
  - `scripts/review_collect_pr_metadata.sh:231`: "Collect PR metadata" (`review_autofix.yml:2655`), full `pulls/{n}` payload again.
  - `review_autofix.yml:5366`: a third read before the failure alerts.
- **Call count:** 2 per run now in steps 2229 → 2655, plus 1 on failure. Proposed: no change.
- **Endpoint:** `GET repos/{o}/{r}/pulls/{n}`.
- **Evidence:** both reads fetch the whole pull object in the same codex-agent job. `:2240` uses only `.labels`, `.state` and `.merged`. The collector's copy feeds `PR_PAYLOAD_FILE`, whose `.head.sha` is compared at `:2850` to verify the checkout.
- **Why it stays:** several minutes of setup (OpenCode install, Semble, memory) separate the two reads. BUG-001 shows that a PR merging in that window is a real failure mode. Reusing the `:2240` payload would make the `:2850` head check stale. The step's own name, "defense-in-depth", marks it as an intentional re-read.
- **Safety rationale:** the code path explicitly defends against an upstream race (PR merged or pushed mid-run).
- **Downstream signal:** do not merge. It is listed so future passes don't propose it. If anything, BUG-001 argues for one more liveness read before the fetch at `:2912`, not fewer.

**REUSE-006**: `RISKY_SKIP` (corrects the report's "GH API Call Audit" inference)
- **Files:**
  - `.github/workflows/check-failure-triage-historical.yml:75-78` (dedup), `:97` (`pulls/{n}`), `:107` (`actions/runs/{id}`), `:123` (job-log probe).
  - `scripts/check_failure_triage.sh:462` (`pulls/{n}`), `:672-677` (dedup), `:896`, `:900`, `:921` (run, job and log reads in historical mode).
- **Call count:** PR read 2 → 1, run read 2 → 1, and the job log is downloaded twice when available.
- **Evidence:**
  - The report infers that "moving the dedup check before the job-log probe would save 1 call". The dedup is already the first call: `:75` runs before `:97`, `:107` and `:123`.
  - The real duplication is across jobs. The guard job (`GH_TOKEN: ${{ github.token }}`, `:60`) reads the PR, the run and the full job-log body (`> /dev/null`, `:123`). The triage script then reads all three again (`:462`, `:896`, `:921`).
  - The workflow is pinned to one historical run (`group: check-triage-historical-37922279826`, `:24`, and `historical_run_id: "37922279826"`, `:348`). It is a single-use path.
- **Safety rationale:**
  - The reads cross a job boundary.
  - They use a different auth scope: the guard uses `github.token`; the script's token was not verified here.
  - `:123` is an existence probe whose 404/410 result drives the `repro` matrix.
  - The script's re-reads are deliberate fail-closed bindings ("Bind the run and job … before reading any log", `:894-895`).
- **Downstream signal:** do not auto-implement. The only optional, manual change is to replace the `:123` full-log download with a header-only probe; that changes the probe semantics and needs owner review. Treat the report's suggestion as already satisfied.

### Dead Calls (DEAD-API-###)
No findings. I traced the fetched values to their consumers for the calls examined in this pass, including `review_conflict_prepare.sh:1264` (`INTEGRATION_TRACKING_TITLE`/`BODY`, used at `:1497-1539`). Every result examined is consumed downstream.

### Cross-References to Deep Audit Section
- BATCH-001: RISKY_SKIP — in `orchestrate_poll_process.sh` (`:25841-25893`), and the replacement GraphQL batch would need `body` plus paging; REUSE-004 adds a per-tick timeline memo as a cheaper first step.
- BATCH-002: RISKY_SKIP — in `orchestrate_poll_process.sh`, and `pulls/{n}/files` is paginated (`:9315`); REUSE-003 adds that the per-PR state probe repeats data the timeline already returns.
- BATCH-003: RISKY_SKIP — changes pagination (`hasNextPage` → cursor walk at `gh_helpers.sh:1782-1786`) and would change the emission of the grep-able `rate_limit_audit_fallback … reason=timeline_has_next_page` key.
- API-001: RISKY_SKIP — the inventory read sits inside the `while ! gh pr create` retry loop (`implement.yml:6114-6195`) and is paginated (`:6132`).
- API-002: RISKY_SKIP — the per-hop comments read is paginated (`:5554`), and `comments(last:100)` would change page semantics; REUSE-002 gives a non-paginated hop-1 reduction tagged NEEDS_VERIFICATION.
- API-003: NEEDS_VERIFICATION — not a merge (call count unchanged), but switching to `gh_retry --idempotent` changes retry semantics; check how `gh_retry` treats the 409 that `POST …/runs/{id}/cancel` returns for an already-completed run before applying.

### Summary Counts
Counts cover only this pass's MERGE and REUSE findings. Section 2 cross-references are listed above.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 2 | MERGE-003, REUSE-002 |
| RISKY_SKIP | 6 | MERGE-001, MERGE-002, REUSE-003, REUSE-004, REUSE-005, REUSE-006 |

### Implement-Stage Handoff
- REUSE-001: in `scripts/workflow_failure_heal_intake.sh:1450`, fall back to `PROVENANCE_LOGIN` before the live `/user` read at `:1454`, and correct the §14 comment at `:1452-1453`. This saves 1 `/user` call per `autofix_failure` heal report that carries a `source_gen`.