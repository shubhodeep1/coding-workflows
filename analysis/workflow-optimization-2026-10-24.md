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