## Executive Summary

> Data gap: the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was **not mounted** in this sandbox, so there were no deep-dive `errors/`, `slow/` or `recent/` step logs to read. Everything below comes from `analysis_context.json`: aggregates, per-run `cost_telemetry`, and the luna `log_summary` fields. `insufficient_data=false`. The window holds 1,000 runs of one repo (`shubhodeep1/coding-workflows`) over about 6.2 h on 2026-10-17 (earliest run seen 06:27:50Z, latest 12:40:13Z).

- **Runner contention during bursts slows CI and review by 2–4×.** CI `p95=2761.5s` against `p50=1027s`. The four slowest CI runs (39010924134 3288s, 39011719399 3197s, 39011233602 3181s, 39011224392 3137s) all started between 06:42 and 06:56Z. The same 6-job CI took 694–830s at midday (39030933223, 39032397921, 39032154394). Every CI job has `timeout-minutes` ≤ 20 (`.github/workflows/ci.yml:40,505,856,1104,1459,1929`), so a 55-min run means at least about 35 min of queue or wait time (inference). Fixing this would cut median end-to-end PR latency by about 15–40 min in bursts. **Confidence: medium.**
- **`tests-release-and-log-analysis` is at its 20-min budget.** Issue #7560 reports "took 20m16s of its 20-minute timeout". Cancelled CI run 39030079651 stopped at that job's last step, `Check-failure triage workflow security tests` (`ci.yml:1911`), after 1394s. Three PR CI runs show a silent gap of about 5 min that starts at `Audit gate template atomicity contract test` (`ci.yml:1518`): 39030933223 12:07:52→12:13:15, 39032154394 12:26:09→12:31:10, 39031267268 12:12:57→12:18:23. This is the top reliability risk: timeouts will turn into red CI. **Confidence: medium-high.**
- **review_autofix is the whole AI cost centre.** 103.2M OpenRouter tokens (20.67M uncached prompt, 76.75M cache-read, 4.98M cache-write, 0.81M completion) across 106 calls in 8 runs, about 12.9M tokens per run. The aggregate cache hit rate is **75.0%** (read / (read + prompt + write)). Per-run rates range from 0.489 (39030933275) to 0.836 (39013791958). Bringing the low runs up to about 0.80 would save about 1–1.5M uncached prompt tokens per affected run. **Confidence: high** on the numbers, **medium** on the savings.
- **Six review_autofix runs were cancelled after 19–40 minutes of agent time.** Runs 39011719531 (2403s), 39011233661 (1929s), 39010924214 (1915s), 39027014394 (1526s), 39026707343 (1481s), 39026471658 (1145s): about 10,400 s (about 2.9 h) of runner time in total. None of them has cost telemetry, so their token spend is unmeasured. No cancellation reason is logged. **Confidence: high** on the waste, **low** on the cause.
- **Codex token telemetry is broken.** `codex_tokens_used=7` across `codex_calls=16`, and clarify, plan and clarify-respond runs report 1–2 tokens even though they ran 376–886s at `MODEL_REASONING_EFFORT: high`. `CODEX_TOKENS_RE` (`scripts/cost_audit.py:62-64`) is matching something other than the real Codex usage footer, or the footer is missing. So spend for clarify, plan and implement is effectively invisible. **Confidence: high.**
- **The 1,000-run window is 57.6% skipped noise.** `other_count=576`: clarify 100/104, plan 98/102, implement 96/99, orchestrate_clarify_respond 97/100, workflow_failure_heal 32/32. As a result the window covers only about 6 h, and family percentiles such as implement `p50=1s` are meaningless. **Confidence: high.**
- **Overall reliability is otherwise good.** There was 1 failure (CI 39016403020, `static-checks` / `Implement post-Codex recovery unit tests`) out of 424 runs that reached success, failure or cancelled. There were no HTTP 429s, 0 `BREAK_GLASS`, 0 Semble runtime fallbacks and 0 failed Semble bootstraps. **Confidence: high.**

## Speed Optimizations

Ranked by expected latency reduction.

1. **Critical path: measure runner queue time, then shed concurrent load in bursts.**
   - **Evidence:**
     - CI took 3100–3288s in the 06:42–06:56Z burst against 694–830s at midday.
     - review_autofix took 2000–3118s in the burst (39012420625 3118s, 39012904861 2944s, 39013109065 2776s) against 1250–1664s at midday (39030933275, 39031267441).
     - Even uncontended runs wait. In 39030933223 the job queued at 12:01:54 and steps ran about 12:07–12:13 ("roughly 6 minutes waiting"). In 39032154394, `tests-release-and-log-analysis` waited from 12:20:50 until about 12:26.
   - **Root cause (inference):** each push starts a ~6-job CI (with an `orchestrate-poll` matrix) plus a ~20-min review agent job. With several PRs active, hosted-runner concurrency saturates.
   - **Change:**
     - (a) Diagnostics first: have `scripts/collect_workflow_logs.py` record a per-job `queue_ms` from the jobs API (job `created_at` → `started_at`) for slow and failed runs, if it does not already. Report p50/p95 queue time per workflow family.
     - (b) Once that confirms queueing dominates, cancel superseded review_autofix runs earlier (see the Reliability section). Those 6 cancellations alone held runners for about 2.9 h in this window.
   - **Estimated saving:** 15–40 min off p95 PR CI and review wall-clock in bursts (inference).
   - **Risk:** low for (a), which is read-only collector work. Medium for (b), which touches concurrency semantics.

2. **Critical path: split `tests-release-and-log-analysis`, which sits at its 20-min timeout.**
   - **Evidence:** the 20m16s in #7560; the ~5-min silent gaps after `Audit gate template atomicity contract test` in 3 runs; cancelled run 39030079651 stopping at the job's final step.
   - **Root cause:** test steps have been added to one job without per-step budgets. The truncated logs cannot show whether the gap is `tests/test_audit_gate_template_atomicity.py`, `tests/test_audit_gate_identity.py` or a later step.
   - **Change:**
     - Follow the precedent in the `ci.yml:457` comment, which moved steps out of `tests-heal-plan-and-validation` after it measured 15m04s/20m. Move the slowest step or steps into a less-loaded job; `static-checks` has a 15-min timeout, so check its budget before using it.
     - Pin down the culprit from jobs-API step timestamps, not log excerpts.
     - The orchestrator is already working this exact issue: #7560 went through clarify 39031547953, clarify-respond 39031798324 and plan 39032255753. Let that plan land rather than opening a parallel change.
   - **Estimated saving:** about 5 min off that job's critical path; it removes the timeout risk.
   - **Risk:** low. This is job membership only; never raise `timeout-minutes`.

3. **Critical path: break down the review `codex-agent` job (18–25 min).**
   - **Evidence:** 39031267441 ran the agent from 12:09:49 to 12:34:48. 39030933275 ran it from 12:04:24 to 12:22:38.
   - **Change:** emit per-phase timing for reviewer, aggregator and editor inside the agent step so the 25 min can be attributed. About 1.67M characters were truncated from that step, so there is no breakdown today.
   - **Estimated saving:** unknown until measured.
   - **Risk:** low.

4. **Micro: Semble bootstrap time.** There were 10 bootstraps totalling 436,193 ms, about **43.6 s each**. Runs with 20 OpenRouter calls had 2 bootstraps (84.2–84.6 s). That is about 3–6% of the agent job. No change is recommended until we know whether the bootstrap overlaps model calls: add `overlapped=true|false` to the `SEMBLE_BOOTSTRAP` line.

5. **Micro: the 8.5-min orchestrate_poll step.** In 39031581204 the poll ran 12:12:01–12:20:28, and family `p95=933.65s`. About 822k characters were truncated, so no sub-step is visible. Add sub-phase timing lines. **Risk:** low.

6. **Micro, off the critical path: the `heal-pr-reconcile` checkout in `internal-cancel-on-pr-close.yml:45-50`.**
   - **Evidence:** it uses `fetch-depth: 0`. In 39032153645 it fetched about 100+ tags and then exited with `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`.
   - **Change:** a cheap "any heal PR for this source PR?" check before the full checkout.
   - **Estimated saving:** up to about 1–2 min of runner time per PR close.
   - **Risk:** low. The full history is still fetched when reconciliation actually runs. Confidence is low that the 114 s is checkout rather than runner wait.

## Cost Optimizations

Ranked by expected token or dollar savings.

1. **Raise the review_autofix prompt-cache hit rate on low-hit runs.**
   - **Evidence:**
     - Run 39030933275: `cache_hit_rate=0.489`, 2.69M uncached prompt, 0.87M cache-write, 3.41M cache-read over 20 calls.
     - Run 39013791958: 0.836, with 0.52M cache-write against 12.79M cache-read.
     - The write/read ratio is 0.25 on the worst run against about 0.04 on the best.
   - **Root cause (inference):** in 20-call runs (2 review rounds, 2 Semble bootstraps), the second round either re-creates the prefix or misses it because the cache expired during the ~18-min agent job. The cause cannot be confirmed without per-call prefix identity.
   - **Change:** add a stable-prefix hash as a trailing field on the `INFO: openrouter usage` line. Check first that `OPENROUTER_RE` in `cost_audit.py:71` still matches with a trailing field. Then group calls by prefix hash to find where the prefix varies.
   - **Estimated saving:** if the 0.49–0.71 runs reach about 0.80, roughly 1–1.5M uncached prompt tokens per affected run; about 3 of the 8 runs were in that band.
   - **Quality risk:** none, because prompt content is unchanged.

2. **Stop paying for review runs that get cancelled.**
   - **Evidence:** 6 runs cancelled after 1145–2403 s, all with zero cost telemetry rows.
   - **Inference:** at the observed ~12.9M tokens per completed run, each late cancellation probably burns several million tokens.
   - **Change:** see Reliability #3. Cancel superseded runs before the agent step starts, or check head-SHA freshness between reviewer rounds.
   - **Estimated saving:** up to about 6 × (partial run cost) per 6 h window.
   - **Risk:** low if the check is SHA-equality only.

3. **Fix Codex token telemetry before tuning clarify, plan or implement cost.**
   - **Evidence:** `codex_tokens_used` is 1–2 per run. Clarify 39031547953 (376 s), clarify-respond 39031798324 (795 s) and plan 39032255753 (886 s) all ran `openai/gpt-6-sol`, which is not credible at that size.
   - **Root cause:** `CODEX_TOKENS_RE = r"tokens\s+used[^0-9\n]*\n?\s*([0-9][0-9,]*)"` is unanchored, so it can match prompt or env text containing "tokens used". The real footer may also be missing from the log.
   - **Change:** emit one structured line per Codex call from the existing Codex wrapper (for example a usage line with `phase=` and `total=` fields, registered via an `agents.d/` fragment as a new log prefix). Have `cost_audit.py` prefer that line, keeping the legacy regex as a fallback.
   - **Saving:** enables measurement; there is no direct saving yet.
   - **Risk:** low and additive.

4. **Semble's context contribution is small and clean.** 10 queries returned 153,567 bytes (about 15.4 KB per query, roughly 4k tokens) from 72 sources. `static_dup_bytes=4,756` is 3.1% duplicate. Against roughly 2.6M uncached prompt tokens per review run, that is under 0.2% of prompt volume. Semble is not inflating prompts. Whether it reduces downstream tool or model work is not measurable from this data. No change is needed; keep monitoring `static_dup_bytes` (3 runs had 1,041–2,431 bytes of duplication).

5. **Serena has no data.** `SERENA_ENABLED: false` in review_autofix (39031267441, 39030933275), and every Serena counter is 0. There is nothing to evaluate.

6. **Model and reasoning choice.** Clarify, clarify-respond and plan all ran `MODEL_REASONING_EFFORT: high` on `gpt-6-sol` for the auto-answered `ai:ci-budget` issue #7560. Do not change this until item 3 makes Codex spend visible; it would be guesswork. **Quality risk** if lowered blindly: plan quality on orchestrator issues.

## Reliability Improvements

Ranked by expected reduction in failure or rerun rate.

1. **Timeout risk in `tests-release-and-log-analysis`.** See Speed #2. **Category:** CI capacity or budget.
   - **Evidence:** #7560 (20m16s/20m); 39030079651 cancelled at the job's final step after 1394 s.
   - **Fix:** move steps out of the job, with jobs-API step timing to choose which.
   - **Impact:** prevents an expected recurring red-CI failure that would also trigger heal and triage runs.
   - **Rollback:** revert the job move.

2. **The single hard failure: CI 39016403020** (`static-checks` / `Implement post-Codex recovery unit tests`, 1343 s, attempt 1).
   - The step runs `python3 tests/test_implement_post_codex_recovery.py` (`ci.yml:452-455`). `static-checks` has `timeout-minutes: 15` (`ci.yml:40`).
   - The run lasted 22 min, but wall-clock includes queueing, so a timeout and an assertion failure cannot be told apart without the step log, which was not mounted.
   - The same-commit review run 39016403095 succeeded (2249 s).
   - **Fix:** next collection, read `errors/39016403020/` and classify the failure as timeout, assertion or flaky. If it is a timeout, apply the same job-move pattern.
   - **Impact:** about 1 of 71 CI runs (1.4%).

3. **Late review_autofix cancellations (6 of 84 runs, 7.1%).**
   - **Evidence:** each cancelled review run shares a `created_at` with a CI run that succeeded: 39011719531↔39011719399, 39011233661↔39011233602, 39010924214↔39010924134, 39027014394↔39027014243, 39026707343↔39026707200, 39026471658↔39026460969. They were triggered by the same push and cancelled 19–40 min later.
   - **Root cause:** unknown. Superseded by a later push to the same PR, which may be autofix's own push, is the leading hypothesis (inference).
   - **Diagnostic addition:** when a review run is cancelled or superseded, log a line with `pr=`, `head_sha=`, `superseded_by_run=` and `reason=`. Emit it in the cancelling path; `cancel_on_pr_close` already logs `MERGE_TRAIN_RELEASE_SUMMARY`, which is the pattern to follow.
   - **Fix after diagnosis:** a head-SHA freshness check before each expensive reviewer round.
   - **Fail-open:** if the SHA check errors, continue the review.

4. **Context budget pressure is low but tied to telemetry loss.**
   - `CONTEXT_BUDGET_WARN`: 2 in total, both review_autofix, 1 each in 39013234110 and 39011708396.
   - Those are exactly the 2 runs with `or_usage_unavailable_calls=1`, and so the only 2 with `cache_hit_rate=null`.
   - Inference: the oversized call is the one whose usage was lost.
   - This points to prompt-size risk on single calls, not rubric pressure, since `BREAK_GLASS=0`.
   - **Fix:** add the `call=` id to the `CONTEXT_BUDGET_WARN` line so it can be joined to the usage line. **Impact:** diagnostic only.

5. **Semble and Serena fallbacks are healthy.**
   - `semble_fallbacks=52`, all `semble_contract_test_fallbacks`, all in CI family: 4 per run on push or slow runs, 8 per run on PR-merge runs.
   - `semble_runtime_fallbacks=0`, 10 of 10 bootstraps succeeded, 0 unused.
   - This is expected fixture behaviour, not a masked broken rollout.
   - Orchestrate poll 39031581204 logged `SEMBLE_AVAILABLE=false, SEMBLE_INDEX_AVAILABLE=false` with `SEMBLE_BOOTSTRAP_MODE=lazy` but emitted no `SEMBLE_FALLBACK`. That is consistent with lazy mode never needing Semble in that run (inference).
   - Serena has no probe data at all (0/0/0).

## AI Memory Health

- **Only one `AI_MEMORY_TELEMETRY` line appears in the whole window:** run 39032153654 (issue_pr_status), `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues","source":"issue_pr_status.yml"}`. This is benign: PR #7556 had no linked issues.
- **No `retrieve`, `record-candidate`, `record-run-event`, `promote`, `compact` or `processed-command-*` ops are visible.** So the retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution and push retries **cannot be computed**.
- **Probable cause is truncation, not absence.** The clarify (39031547953), plan (39032255753) and clarify-respond (39031798324) summaries all say about 224k–520k characters were truncated, and `MEMORY_INJECTION_SCAN_ENABLED: true` was set in those runs.
- **Recommendation:** extend the existing pre-truncation raw-archive scan in `collect_workflow_logs.py` (lines 1684-1724, which today keep only lines matching `GH_PAT_BUDGET phase=end`) to an allowlist of telemetry prefixes: `AI_MEMORY_TELEMETRY:`, `CONTEXT_BUDGET_WARN`, `BREAK_GLASS`, `SEMBLE_*`, `SERENA_*`, and the Codex usage line. Keep the existing "read only budget lines, not arbitrary raw output" safety property by matching strict per-prefix regexes. **Impact:** makes memory health measurable for every clarify, plan and implement run.

## GH API Call Audit

| Workflow / job | Run | `used_in_job` | `remaining` at end |
|---|---|---|---|
| review_autofix / codex-agent | 39031267441 | 481 | 3832 |
| review_autofix / codex-agent | 39030933275 | 359 | 4130 |
| review_autofix_sweep / sweep | 39033312202 | 3 | 3696 |
| review_autofix_sweep / sweep | 39030872661 | 2 | 4519 |
| review_autofix / post-merge-validate-dispatch | 39032153692 | 0 | 4125 |
| lint_pr_body_auto_close | 39032959294, 39032397901, 39031267263, 39030933391 | 1 paginated `pulls/N/commits` call each | — |

- **Caveat: `used_in_job` overstates per-job usage.** In `scripts/gh_helpers.sh:66-72`, `used = previous_remaining - remaining` over the same `reset` window. That counts every consumer of the shared PAT during the job, not just this job. The two codex-agent jobs overlapped (12:04–12:22 and 12:09–12:34) with the same `reset=1792241335`, so 481 and 359 double-count the overlap. Treat them as upper bounds.
- **Hotspot:** the review `codex-agent` job is the only significant consumer, at up to about 360–480 calls per 18–25 min. There is no 429, no secondary rate limit, and `remaining` never fell below 3,696. Rate-limit risk is currently low.
- **Recommendations:**
  1. Add a job-local call counter (increment inside `gh_retry`) and emit it as a **separate** line. Do not append a field to the `GH_PAT_BUDGET` line: `collect_workflow_logs.py:1704` uses `PAT_BUDGET_LINE.fullmatch`, so an extra field would silently drop the line, unless the regex is updated in the same PR. This turns the upper bound into an exact per-job figure.
  2. Once exact counts exist, audit the codex-agent path for per-thread or per-comment REST loops. `gh_review_pr_state` (`gh_helpers.sh:83+`) already shows the job-local cache pattern to reuse, and §14 favours GraphQL batching. No reduction estimate is possible until (1) lands.
  3. The sweep (2–3 calls) and the lint workflows (1 call) are already efficient. No action.

## Prompt Cache & Memory System

- **`cache_hit_rate` (review_autofix):** aggregate **0.750**. Per run:

  | Run | Hit rate |
  |---|---|
  | 39013791958 | 0.836 |
  | 39012904861 | 0.743 |
  | 39031267441 | 0.740 |
  | 39012420625 | 0.710 |
  | 39013109065 | 0.660 |
  | 39030933275 | 0.489 |
  | 39013234110 | null (computes to 0.829) |
  | 39011708396 | null (computes to 0.806) |

- **Null-rate bug:** a single usage-unavailable call nulls the whole run's `cache_hit_rate`, even though 11 of 12 and 12 of 13 calls had usage. Compute the rate over usage-available calls and add a `cache_hit_rate_partial` flag. This is additive in `cost_audit.py` and keeps the existing field.
- **Fragmentation signals:** the cache-write share varies 4×, from 0.04 to 0.25 of reads. The worst run (20 calls, 2 rounds) suggests the cache does not survive between rounds (inference). The prefix-hash field from Cost #1 is the smallest diagnostic that separates "prefix changed" from "TTL expired".
- **CONTEXT_BUDGET_WARN:** 2 warnings, each coinciding with lost usage (see Reliability #4). There is no broad sign that prompt growth is eroding cache value.
- **CI-side caches:** the `claude-cli-npm-Linux-…` primary key hit in 39032153654, 39031798324, 39031267441 and 39030933275 (post step 180–207 ms). Healthy.
- **Memory retrieval:** not measurable (see AI Memory Health).

## Orchestrator Health

- **Poller:** 42 of 42 runs succeeded. p50 536.5 s, p95 933.65 s, max 1196 s (39015695590). The poll step dominates at 8.5 min in 39031581204. Track poll-step duration p95 and alert if it goes above 15 min.
- **Clarify loop:** issue #7560 went from clarify (opened 12:11:27, 376 s) to auto-answered clarify-respond (12:15:19, 795 s, `ORCHESTRATOR_MAX_CLARIFY_CYCLES: 3`) to plan (triggered 12:22:23 by `/answer [auto-answered-by-orchestrator]`, 886 s). That is about 26 min from issue to plan with one clarify cycle. No loops or stalls are visible.
- **Autofix sweep:** `AUTOFIX_SWEEP_END dispatched=0 skipped_active=2|3 failures=0 snapshot_incomplete=false` (39033312202, 39030872661). `CI_CANCELLED_RERUN` skipped everything with `reason=other_conclusion`. Healthy and idle.
- **Merge train:** `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` (39032458139, 39032153645). Nothing to release.
- **Dedup and guards work:** `HISTORICAL_TRIAGE skip reason=already_filed issue=7300` (39032154459); `PR6576_FOLLOWUP outcome=skip reason=marker_present issue=7072` (39032154418).
- **Heal:** `workflow_failure_heal` skipped 32 of 32. Intake had 9 successes, 2 cancellations and 79 skips. It is not visible whether CI failure 39016403020 started a heal or triage run; `check_failure_triage` had 3 successes. Indicator to add: map each CI failure to its triage or heal run.
- **Pain point:** the trigger fan-out. Each issue event starts 4 workflows at the same second (clarify, plan, implement, orchestrate_clarify_respond; for example 12:32:55, 12:24:26, 12:22:05, 12:18:18), and 3 or 4 of them skip. That is cheap (0–10 s), but it is 57.6% of the run window.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix (ordered by end-to-end impact) |
|---|---|---|---|
| review/autofix | Compute (agent 18–25 min) plus queueing in bursts | p50 1311 s, p95 2649 s; burst runs 2000–3118 s | Per-phase timing; earlier cancellation of superseded runs; cache-prefix diagnostics |
| CI (validate) | Queueing plus one job near its timeout | p50 1027 s, p95 2761.5 s; 3288 s worst; #7560 | Queue-time collection; split `tests-release-and-log-analysis` |
| Retry or rerun waste | Late cancellations | 6 review cancellations, about 10,400 s | Cancellation-reason line; SHA freshness check |
| clarify → plan | Compute (high-reasoning Codex) | 376 s, 795 s, 886 s for #7560 | Fix Codex token telemetry before tuning |
| implement | Compute | 1673 s (39028203495), 1579 s (39027728665), 1191 s (39020237131) | Same as above (telemetry first) |
| orchestrate | Compute (poll step) | 516–1196 s | Sub-phase timing |
| merge/conflict | None observed | merge train idle, 0 conflict-heal signals | — |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** runner contention in bursts (CI 3288 s against about 700 s uncontended); the review codex-agent job at 18–25 min; `tests-release-and-log-analysis` at its 20-min budget.
- **Top failure modes:** the CI timeout risk (#7560, 39030079651 cancelled); the `static-checks` failure 39016403020 (cause not yet classified); 6 late review cancellations.
- **Highest-cost driver:** review_autofix at 103.2M OpenRouter tokens across 8 runs. Codex spend is unmeasured because of the broken parser.
- **Top 3 prioritized actions:**
  1. Split `tests-release-and-log-analysis` using jobs-API step timings, via the in-flight #7560 plan.
  2. Extend the collector's pre-truncation scan to an allowlist of telemetry prefixes, and fix Codex token parsing with a structured usage line.
  3. Add a review-cancellation reason line and a head-SHA freshness check before reviewer rounds.

## Metrics Appendix

**Window:** 1,000 runs, 1 repo, about 06:27–12:40Z on 2026-10-17. `success_sample_rate=0.07`; 4 sampled success runs. 115 runs had log telemetry.

| Family | Runs | Success | Fail | Cancel | Other/skip | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| review_autofix | 84 | 78 | 0 | 6 | 0 | 1311 | 2649.4 |
| ci | 71 | 69 | 1 | 1 | 0 | 1027 | 2761.5 |
| integration_pr_readiness | 66 | 66 | 0 | 0 | 0 | 35.5 | 285 |
| lint_pr_body_auto_close | 59 | 59 | 0 | 0 | 0 | 37 | 371 |
| orchestrate_poll | 42 | 42 | 0 | 0 | 0 | 536.5 | 933.65 |
| cancel_on_pr_close | 41 | 40 | 0 | 1 | 0 | 74 | 270 |
| issue_pr_status | 14 | 14 | 0 | 0 | 0 | 149.5 | 389.1 |
| check_failure_triage_historical | 13 | 13 | 0 | 0 | 0 | 18 | 222 |
| clarify | 104 | 4 | 0 | 0 | 100 | 1* | 10.85* |
| plan | 102 | 4 | 0 | 0 | 98 | 1* | 10* |
| orchestrate_clarify_respond | 100 | 3 | 0 | 0 | 97 | 1* | 10.05* |
| implement | 99 | 3 | 0 | 0 | 96 | 1* | 10* |
| workflow_failure_heal_intake | 90 | 9 | 0 | 2 | 79 | 1* | 149.35* |
| check_failure_triage | 77 | 3 | 0 | 0 | 74 | 1* | 11* |
| workflow_failure_heal | 32 | 0 | 0 | 0 | 32 | 1* | 8.45* |
| auto_release_stable / publish_sandbox_images / forward_merge / sync_claude_live_copies | 2 / 2 / 1 / 1 | all | 0 | 0 | 0 | 46 / 138.5 / 226 / 154 | — |
| **Total** | **1000** | **413** | **1** | **10** | **576** | **8** | **1526** |

\* These percentiles are dominated by skipped runs and are not meaningful. The collector should compute them over non-skipped runs.

| Cost / review telemetry | Value |
|---|---|
| OR prompt / completion / total | 20,672,536 / 805,105 / 103,206,047 |
| OR cache read / write | 76,753,491 / 4,977,056 |
| OR calls (usage available / unavailable) | 106 (104 / 2) |
| Aggregate prompt-cache hit rate (computed) | 0.750 (`cache_hit_rate` field null at summary level) |
| Codex calls / tokens (parser output, unreliable) | 16 / 7 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all) | 7,000 / 3,195,720 (n=109) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix) | 2,544,000 / 3,100,600 (n=11) |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 2 (39013234110, 39011708396; both review_autofix) |

| Semble | Value |
|---|---|
| Query calls / bytes | 10 / 153,567 (about 15.4 KB per query) |
| Sources / static dup bytes | 72 / 4,756 (3.1%) |
| Bootstraps / failed / unused / total ms | 10 / 0 / 0 / 436,193 (43.6 s average) |
| Fallbacks (contract-test / runtime) | 52 (52 / 0), all CI family |
| Echo lines dropped | 0 |

| MCP availability (per target) | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |
| Serena query calls / response bytes / tool calls / ms | 0 / 0 / 0 / 0 | | |

Other MCP servers observed: none.

| GH API | Value |
|---|---|
| Max `used_in_job` (codex-agent) | 481 (39031267441); upper bound because the PAT is shared |
| Min `remaining` observed | 3,696 (39033312202) |
| 429 or secondary rate-limit events | 0 observed |

**Data gaps and next collection steps:**
1. The run-logs directory was not mounted. Mount it, then read `errors/39016403020/` to classify the static-checks failure.
2. Collect per-job and per-step queue and run times from the jobs API.
3. Extend the pre-truncation telemetry allowlist scan in `collect_workflow_logs.py`.
4. Fix `CODEX_TOKENS_RE` or add a structured Codex usage line.
5. Exclude skipped runs from the 1,000-run cap so the window covers more than about 6 h.
6. Capture cost telemetry for cancelled runs.
## Deep Audit — Workflows & Scripts (2026-10-17)

> **Scope and method.** This is a depth-first audit, not a full sweep. The deepest reads were the GitHub API and label helpers (`scripts/gh_helpers.sh`, `scripts/label_helpers.sh`), the review path (`scripts/review_collect_pr_metadata.sh`, `.github/workflows/review_autofix.yml`), the sweep, cancellation, poller and lint/readiness workflows, and the helpers they call. The other ~40 workflows and ~130 scripts were only grep-scanned for specific patterns. No line counts in bytes or characters were measured (no shell was available), so every size estimate is marked [NEEDS VERIFICATION]. Prior report items are cross-referenced, not repeated.

### Section 1: Bug & Correctness Sweep

**BUG-001** — `.github/workflows/orchestrate_poll.yml:65-73` (job `poll`); compare `:2016-2020`
- **Severity:** High | **Category:** `bug`
- **Description:**
  - The `poll` job has no `timeout-minutes`. The only `timeout-minutes` in the file belongs to `claude_pool_daily_usage` at line 2020, and no step sets one.
  - GitHub's default job timeout is 360 min. The job sits in concurrency group `ai-orchestrate-poll-${{ github.repository }}` with `cancel-in-progress: false` (lines 72-73), and is triggered every 5 min (`internal-orchestrate-poll.yml:5`).
  - So one hung poll (a stuck `gh` call, a judge, or a conflict resolver) would block every orchestrator tick for up to 6 h. Each new tick would just replace the single pending run.
  - The prior section reports poller p95 = 933.65 s and max = 1196 s, so a 6 h ceiling is about 18× the worst observed run.
- **Recommended fix:** add `timeout-minutes: 45` to the `poll` job, at the same indent as `runs-on:` (about 2.3× the observed max). This adds a timeout where none exists; it does not raise one. Keep `cancel-in-progress: false` so in-flight conflict resolution is still never killed by a newer tick.

**BUG-002** — `.github/workflows/cancel_on_pr_close.yml:134-167`, `:284-302`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - `_cpr_fetch_active_runs` lists only `status=queued` and `status=in_progress`, both with `event=pull_request` (lines 150-151 and 160-161). The PR-close filter also requires `.event == "pull_request"` (line 293).
  - It therefore never sees two kinds of runs:
    - (a) runs held `pending` by a concurrency group. The `review_autofix.yml:1820-1821` group is `pr-autofix-<N>` with `cancel-in-progress: false`.
    - (b) `workflow_dispatch` review runs named `Internal: AI Review & Autofix [pr:<N>]` (`internal-review.yml:8`), dispatched by the sweep, the merge train and post-commit retrigger.
  - The sweep had the same blind spot and fixed it explicitly by adding `pending` (`review_autofix_sweep.yml:322-337`, PR #3895 incident).
  - Consequence (inference): after a PR closes, a pending or dispatched review for it survives, and when it starts it pays for runner setup and possibly agent work. [NEEDS VERIFICATION: whether the gate's PR-state check at `review_autofix.yml:2196-2221` exits before any model call on every path.]
- **Recommended fix:**
  - Add a third paginated listing with `-f status=pending`.
  - Match workflow_dispatch runs by `display_title` against `^Internal: AI Review & Autofix \[pr:<N>\]$`, applying the same provenance check as the sweep's `dispatch_pr` jq function (`review_autofix_sweep.yml:376-385`).
  - Cost: +1 paginated call per close event.

**BUG-003** — `.github/workflows/cancel_on_pr_close.yml:122-165`, `:349-353`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - The cancel step runs under `set -euo pipefail` (line 125). It pipes `gh_retry gh api … --paginate | jq -s` (lines 146-155, 156-165), so a listing that fails after retries aborts the step.
  - The next step, `Release merge-queued PRs (merge train)`, has no `if:`, so it inherits `success()` and is skipped.
  - A transient listing failure on a PR close therefore also skips the merge-train release that the comment at lines 350-352 says core-profile repos rely on ("core-profile repos have no poll backstop").
- **Recommended fix:** add `if: ${{ !cancelled() }}` to the release step. The release script is already fail-open: `_mt_rc` is captured and logged as a warning at lines 381-385.

**BUG-004** — `.github/workflows/internal-review.yml:49-51`; `.github/workflows/review_autofix.yml:1799-1821`; `scripts/orchestrate_poll_process.sh:22494-22497`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - This is a likely root cause for the prior section's "Six review_autofix runs were cancelled after 19–40 minutes" (Executive Summary bullet 4, Reliability #3).
  - Both concurrency levels use `cancel-in-progress: false`. With that setting, GitHub keeps one running and one pending entry per group, and cancels the older pending entry when a third arrives. The poller's own comment says so: "GitHub replaces — cancels — the older pending duplicate in the concurrency group" (`orchestrate_poll_process.sh:22494-22496`).
  - The cancelled runs have zero cost telemetry. That fits them spending their 19–40 min **pending** at the `codex-agent` job, not running the agent (inference).
  - If so, the prior report's "~2.9 h of runner time" is mostly queue wall-clock, and the "token spend unmeasured" concern is moot. [NEEDS VERIFICATION: jobs API `codex-agent.started_at` is null, or `steps == []`, for runs 39011719531, 39011233661, 39010924214, 39027014394, 39026707343, 39026471658.]
- **Recommended fix:**
  - (a) Verify with the jobs API before acting on Reliability #3's "head-SHA freshness check between reviewer rounds".
  - (b) If confirmed, the waste is duplicate *dispatch*, not mid-run supersession. Extend the existing `_has_active_autofix_run` / sweep snapshot dedupe so that the `pull_request` synchronize path also skips when a `pending` run for the same PR exists. Alternatively, accept the replacement as intended and drop it from the waste metrics.

**BUG-005** — `.github/workflows/review_autofix.yml:4930-4949`, `:5021-5039`; `scripts/review_autofix_step_post_editor_summary.sh:97-117` vs `scripts/review_autofix_step_assemble_failure_evidence.sh:63-75`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - All three copies say: "Same evidence files and reason precedence as 'Assemble failure evidence', so every failure comment of one run carries one fingerprint."
  - That is false in two ways:
    - The canonical call passes `--evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt"` (line 69) and `"${provider_outage_reason_args[@]}"` (line 74). Neither the two workflow copies nor the post-editor-summary copy pass either one.
    - The fingerprint is `fingerprint(…, failure_reason, error_signature(evidence_text))` (`scripts/workflow_failure_heal.py:3021-3030`, with `_cmd_autofix_failure_fingerprint` at `:4877-4886` reading every `--evidence-file`). So whenever `resolver_stage_stderr.txt` is non-empty, comments from different paths of one run carry different `fp=` values.
  - Effect: the identical-failure fingerprint cap can undercount across runs whose failures were reported through different paths. [NEEDS VERIFICATION: whether the resolver step at `review_autofix.yml:3918` leaves non-empty `resolver_stage_stderr.txt` on the editor-changes-lost or no-op paths.]
- **Recommended fix:** move the argument list into one helper (see DUP-004) so all four callers pass identical evidence files. Pass `--failure-reason` explicitly only where a caller overrides it, as in `review_autofix_step_post_editor_summary.sh:104`.

**BUG-006** — `.github/workflows/review_autofix.yml:1125-1159`, `:1185`, `:1375`, `:1412`; `.github/workflows/review_autofix.yml:1087-1091`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - The deterministic-skip-merge step defines its own `gh_retry` (lines 1132-1154). It retries **every** failure 4 times with 2/4/8 s backoff, with no permanent-failure or rate-limit classification and no unsafe-POST guard.
  - `ensure_label_exists` (lines 1156-1159) calls `gh label create` through it. If the label already exists, the 422 "already exists" is retried 4×: 4 API calls and about 14 s of sleep per label. Two labels per run (lines 1375, 1412) means up to 8 calls and about 28 s.
  - The same shim backs `pr_checks_lib.sh`, which `source`s with `_safe_gh_jq() { gh api "$@"; }` (line 1185). The `-X PUT …/update-branch -f expected_head_sha=…` call (`scripts/pr_checks_lib.sh:948`) is therefore retried 4× on a deterministic 422 SHA mismatch.
  - The stated reason for the inline copy is to avoid "the full support source" (lines 1125-1127). But the job already sparse-checks out `scripts/gh_helpers.sh` from the verified support SHA (lines 1087-1091).
- **Recommended fix:**
  - Inside the existing `freshness_helper_verified` branch (lines 1181-1188), `source .codex-freshness-src/scripts/gh_helpers.sh` and drop the inline `gh_retry` and `_safe_gh_jq` shims there.
  - Add `scripts/label_helpers.sh` to the sparse list and use its `ensure_label_exists`.
  - Keep the inline shim only on the unverified fallback path, and teach it to stop on `already exists` and on HTTP 4xx.

**BUG-007** — `scripts/label_helpers.sh:222-256` (`set_issue_phase_label_resilient`)
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - This is a TOCTOU read-modify-write race. The function GETs all labels (lines 223-224), computes `(. - $p) + [$t]` (lines 238-239), then **replaces** the whole set with `PUT …/labels` (lines 251-253).
  - Any label another workflow adds between the GET and the PUT is silently removed. Examples: `ai:needs-human` from the poller, `ai:merge-queued` from the merge train, `ai:claude-capacity-wait`.
  - Concurrent label writers are routine here: the poller, review_autofix, issue_pr_status and the sweeps all touch issue labels.
- **Recommended fix:**
  - Replace the PUT with `POST …/labels` for the target, then `DELETE …/labels/<name>` for each phase label present in the GET snapshot (URL-encode the name; treat 404 as success, which `_is_gh_permanent_failure` already classifies, `gh_helpers.sh:192-195`).
  - This only removes what it observed and preserves concurrent additions.
  - Calls: currently GET + PUT = 2; after the fix, GET + POST + k DELETE, where k is typically 1.
  - Keep `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME` semantics unchanged.

**BUG-008** — `scripts/gh_helpers.sh:2690-2694` (`extract_repo_scoped_issue_refs_from_text`)
- **Severity:** Low | **Category:** `bug`
- **Description:**
  - `grep -oiE` matches are non-overlapping. Each alternative both requires a leading boundary `(^|[^[:alnum:]_/-])` and consumes a trailing boundary `([^[:alnum:]_]|$)`.
  - When two references are separated by exactly one character, the first match eats that character, so the second reference has no leading boundary and is dropped. Examples: `Fixes #1 Fixes #2`, `closes #3,closes #4`.
  - Callers that depend on this fallback: `review_collect_pr_metadata.sh:262-268`, `review_autofix_step_mark_ready_to_merge.sh:64-65`, `review_autofix_step_mark_review_blocked_workflow_failure.sh:45-46`, `review_rb_judge.sh:1123-1124`. [NEEDS VERIFICATION: `extract_repo_scoped_issue_refs_from_text o/r 'Fixes #1 Fixes #2'` should print `1` and `2`.]
- **Recommended fix:** make the boundaries zero-width. For example, use `grep -oiP` with `(?<![[:alnum:]_/-])` before the keyword and `(?![[:alnum:]_#?])` after the number, or pre-split the text on non-alphanumeric runs before matching. Add both one-character-separator cases to the existing tests.

**BUG-009** — `.github/workflows/review_autofix_sweep.yml:212-237`, `:590-598`
- **Severity:** Low | **Category:** `bug`
- **Description:**
  - The open-PR snapshot is a raw `gh api --paginate` under `set -euo pipefail` (lines 213, 228-237), even though `gh_api_retry` is sourced a few lines earlier (lines 216-220).
  - A transient 5xx fails the enumerate step. `Re-run cancelled CI failed jobs once` (lines 590-598) has no `if:`, so it is then skipped. It is also the only consumer of `${RUNNER_TEMP}/ci-cancelled-pr-snapshot.json`, which is written at line 239.
- **Recommended fix:** use `GH_TOKEN="${READ_TOKEN}" gh_api_retry --paginate -X GET "repos/${REPOSITORY}/pulls" …` at line 228. Give the CI-rerun step `if: ${{ !cancelled() }}`, provided `ci_cancelled_rerun.py` already fails open on a missing snapshot [NEEDS VERIFICATION].

**BUG-010** — `.github/workflows/cancel_on_pr_close.yml:14-16`; `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
- **Severity:** Low | **Category:** `bug`
- **Description:**
  - Every PR-close run and every 5-minute scheduled run share one repo-wide group, `cancel-on-pr-close-${{ github.repository }}`, with `cancel-in-progress: false`.
  - Under the pending-replacement behaviour described in BUG-004, a PR-close run waiting behind a scheduled run is cancelled by the next schedule tick or close event. Its targeted cancellation and its merge-train release are dropped.
  - The scheduled scan (lines 237-274) recovers within about 5 min, so the impact is latency only.
- **Recommended fix:** key the group per trigger, for example `cancel-on-pr-close-${{ github.repository }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || 'schedule' }}`. Cancellation POSTs and the train release are both idempotent.

**SEC-001** — `.github/workflows/clarify.yml:66-73`
- **Severity:** Low | **Category:** `security`
- **Description:**
  - The run body interpolates `${{ github.event.comment.user.login }}`, `${{ github.event_name }}`, `${{ github.event.action }}` and `${{ github.event.issue.number }}` directly.
  - None of these is attacker-controlled free text (logins are restricted to `[A-Za-z0-9-]`), so this is not exploitable today.
  - It does break the env-indirection convention the same file follows four steps later (`HOLD_COMMENT_LOGIN`, line 96), and it puts the body into expression evaluation for no benefit.
- **Recommended fix:** move these values to `env:` (`COMMENT_LOGIN`, `EVENT_NAME`, `EVENT_ACTION`) and reference them as shell variables.

**SEC-002** — `scripts/review_collect_pr_metadata.sh:127-131` (vs `:280-284`)
- **Severity:** Low | **Category:** `security`
- **Description:**
  - `_fetch_linked_issue_bodies_graphql` builds its GraphQL document by string interpolation: `repository(owner: \"${REPOSITORY_OWNER}\", name: \"${REPOSITORY_NAME}\")`.
  - The only validation is `^[^/]+/[^/]+$` (line 45), which allows `"`. `GITHUB_REPOSITORY` is runner-supplied, so the risk is low.
  - The same script already uses GraphQL variables for the closing-references query (lines 280-284).
- **Recommended fix:** pass `-f owner=… -f name=…` with `query($owner:String!,$name:String!)`, matching lines 280-284. The aliases stay interpolated, which is safe because they are integer-validated at line 103.

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `scripts/orchestrate_poll_process.sh:22069-22088` (`_has_active_autofix_run`); call sites `:12538`, `:12617`, `:22497`
- **Severity:** Medium | **Category:** `api-redundancy`
- **Description:**
  - Each call runs up to 3 `gh_retry gh run list --workflow <wf> --branch <head>` lookups, for `ai-review.yml`, `internal-review.yml` and `review_autofix.yml`.
  - In this repo, `ai-review.yml` exists only as a consumer template (`workflow-templates/ai-review.yml`), and `review_autofix.yml` is `workflow_call`-only (`review_autofix.yml:3-4`), so it never has runs of its own.
  - When `gh run list --workflow` cannot resolve a workflow, gh prints a "could not find any workflows named …" style error with no `HTTP 404` token. `_is_gh_permanent_failure` (`gh_helpers.sh:192-195`) does not match that, so `gh_retry` would retry 5× with 1+2+4+8 s backoff. That is about 15 s and about 10 extra API calls per lookup (gh resolves the workflow before listing runs).
  - The poller already documents and avoids exactly this 404 hot loop for validation (`orchestrate_poll_process.sh:14172-14183`). [NEEDS VERIFICATION: the gh CLI's exact stderr for an unknown workflow file.]
- **Call count:** currently 3 workflow lookups per call, plus up to 4 retries on the absent workflow, plus 1 PR-named lookup. After the fix: 1 lookup per call, `GET repos/{repo}/actions/runs?branch=<head>&status=<s>` filtered client-side by `.path` across all three wrappers, plus the existing PR-named lookup.
- **Recommended fix:** reuse the single-listing pattern from `review_autofix_sweep.yml:300-439` (snapshot once per tick, look up per PR locally) and cache it for the cycle, as `STALL_MANAGED_LINKED_PR_CACHE` does. At minimum, use `_safe_gh_jq` single-attempt probes for the possibly-absent workflow names, mirroring `:14184`.

**API-002** — `.github/workflows/review_autofix_sweep.yml:337-348`, `:437-439`, `:504`
- **Severity:** Low | **Category:** `api-redundancy`
- **Description:**
  - The active-run snapshot lists 3 statuses × 2 workflows per tick.
  - `review_autofix.yml` is `workflow_call`-only (`review_autofix.yml:3-4`). Its runs are recorded under the caller workflow (`internal-review.yml`), so its 3 listings always come back empty.
  - That is 3 paginated calls per 30-min tick, about 144 calls/day.
- **Call count:** 6 per tick now, 3 after the fix.
- **Recommended fix:** drop `review_autofix.yml` from both loops at lines 437 and 504. If a consumer-template variant ever names a direct caller `review_autofix.yml`, gate it on a single workflow-existence probe instead. [NEEDS VERIFICATION: no repo or consumer dispatches `review_autofix.yml` by file name.]

**API-003** — `scripts/lint_pr_body_auto_close.py:143-178`, `:257-279`
- **Severity:** Low | **Category:** `api-batching`
- **Description:**
  - The lint makes one `gh issue view N --json labels` per unique `(repo, issue)`, with no retry.
  - Failed lookups are not cached: line 267 caches only when `labels is not None`. A failing issue referenced in K places is therefore fetched K times, all failing.
  - This runs on every `opened`/`edited`/`synchronize` event of every PR (`lint-pr-body-auto-close.yml:34`), and the commit-message scan (up to 250 commits) multiplies the references.
- **Call count:** N unique issues (+ repeats on failure) now, 1 per referenced repo after the fix.
- **Recommended fix:**
  - Collect the unique numbers per repo first.
  - Issue one aliased GraphQL query `repository(owner,name){ i0: issue(number:N){ labels(first:100){nodes{name}} } … }`, following the batching contract of `_fetch_linked_issue_bodies_graphql` (`review_collect_pr_metadata.sh:76-181`).
  - Negative-cache failures; keep the exit-2 fail-closed semantics.

**API-004** — `scripts/review_collect_pr_metadata.sh:231-248`, `:279-292`
- **Severity:** Low | **Category:** `api-batching`
- **Description:**
  - Each review run makes `GET pulls/{n}`, plus paginated `issues/{n}/comments`, plus paginated `pulls/{n}/comments`, plus a separate GraphQL `closingIssuesReferences` call: at least 4 calls (and 5 with break-glass reviews, lines 238-246).
  - `gh_helpers.sh` already has a GraphQL-first consolidated fetch, `gh_pr_with_all_comments` (`gh_helpers.sh:1459+`).
  - The `closingIssuesReferences` query could be aliased into that same GraphQL document.
- **Call count:** at least 4 now; 2 after the fix (REST `pulls/{n}` kept for the `PR_PAYLOAD_FILE` shape, plus one GraphQL for comments and closing refs, paginating only when `pageInfo.hasNextPage`).
- **Recommended fix:** extend `gh_pr_with_all_comments` with an optional closing-references alias and keep the current file contracts. [NEEDS VERIFICATION: downstream readers of `PR_ISSUE_COMMENTS_FILE` / `PR_REVIEW_COMMENTS_FILE` rely on REST-only fields such as `original_line` (line 485) and `id`.]

**API-005** — `scripts/label_helpers.sh:161-195`, `:220`
- **Severity:** Low | **Category:** `api-redundancy`
- **Description:**
  - `ensure_label_exists` issues a `gh label create` **write** on every call. On steady-state repos that is a guaranteed 422.
  - `set_issue_phase_label_resilient` calls it on every phase transition (line 220).
  - The poller's copy already memoizes results in `_ENSURED_LABELS_CACHE` (`orchestrate_poll_process.sh:3998-4003`). The shared helper does not.
  - Create calls also count toward GitHub's content-creation secondary limit.
- **Call count:** 1 write per transition now; 1 per label per process after the fix.
- **Recommended fix:** add a `declare -gA _LABEL_HELPERS_ENSURED_CACHE` fast path to `label_helpers.sh`, using a new name that does not collide with the poller's `_ENSURED_LABELS_CACHE`.

**API-006** — `.github/workflows/orchestrate_poll.yml:1513-1521`, `:1533`, `:1559`, `:1586-1596`; `scripts/orchestrate_poll_process.sh:22138-22151`
- **Severity:** Low | **Category:** `api-redundancy`
- **Description:**
  - On idle ticks, `orchestrate_poll_process.sh` runs as 4 separate processes: recovery sweep, latch release, unblock scan and PR sweeps.
  - Process-scoped memos do not survive between them. For example, the default-branch memo is keyed on `${GITHUB_RUN_ID}.$$` (line 22151), so each process re-reads `GET repos/{repo}`. Any open-PR or issue listing each mode makes is also repeated [NEEDS VERIFICATION: per-mode listing calls].
- **Call count:** up to 4 reads per tick for each shared fact now; 1 after the fix.
- **Recommended fix:** key the memo file on `${GITHUB_RUN_ID}.${GITHUB_RUN_ATTEMPT}` under `${RUNNER_TEMP}` instead of `$$`. It already rejects symlinks and malformed lines (lines 22152-22169), so sharing it within a job is safe. Apply the same treatment to any per-mode open-PR snapshot.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `ensure_label_exists` defined 9 times with 3 incompatible signatures
- **Severity:** Medium | **Category:** `duplication`
- **Locations:**
  - `scripts/label_helpers.sh:161` `(label, [repo])`
  - `scripts/orchestrate_poll_process.sh:3995` `(label)`, with cache and contract lookup
  - `scripts/validate_process.sh:1222` `(label)`, with contract lookup and `tg_notify`
  - `.github/workflows/review_autofix.yml:1156` `(label, repo, color, desc)`
  - `.github/workflows/review_autofix.yml:1574` `(label, repo)` with a hard-coded `e11d48` colour for every label
  - `.github/workflows/review_autofix.yml:4399`
  - `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`
  - `scripts/review_autofix_step_mark_ready_to_merge.sh:15`
  - `scripts/review_rb_judge.sh:924`
- **Description:**
  - The `review_autofix.yml:1574` fallback would create *any* missing label with review-blocked's colour and description.
  - The colours at lines 1375 and 1412 are duplicated by hand ("must stay in lockstep", lines 1369-1374).
- **Recommended fix:**
  - Make `scripts/label_helpers.sh` the single owner: `ensure_label_exists <label> [repo]`, catalog-driven, with the API-005 cache.
  - Keep the existing positional signature, and accept and ignore extra `color`/`desc` arguments for backward compatibility.
  - Callers in the shared support checkout should `source` it. Self-defined copies in scripts (`validate_process.sh`, `review_rb_judge.sh`, the `review_autofix_step_*` files) should become `type ensure_label_exists || source label_helpers.sh`.

**DUP-002** — `gh_retry` shims and reimplementations
- **Severity:** Medium | **Category:** `duplication`
- **Locations:**
  - Pass-through fallbacks `gh_retry() { "$@"; }`: `review_apply_fixes.sh:29`, `review_conflict_prepare.sh:42`, `review_rb_judge.sh:244`, `validate_process.sh:433`, `orchestrate_poll_process.sh:783`, `label_helpers.sh:20`, `review_run_reviewers.sh:14`, `release_ci_gate.sh:58-61`, `review_autofix.yml:1572`.
  - Full inline reimplementations: `review_autofix.yml:795`, `:1132-1154` (see BUG-006).
  - A **signature-changing** override at `scripts/review_collect_pr_metadata.sh:69-74`: `gh_retry <outfile> <gh args…>`.
- **Description:**
  - The `review_collect_pr_metadata.sh` override shadows the `gh_helpers.sh` function sourced at line 35, which violates §10's no-collision rule.
  - It is harmless today: the only `gh_helpers` function used afterwards, `extract_repo_scoped_issue_refs_from_text` (`gh_helpers.sh:2680-2695`), is text-only.
  - But any future call to `gh_review_pr_state` (`gh_helpers.sh:91,107`) or `_gh_pr_with_all_comments_rest` (`:1440`) from this script would pass an outfile as the command.
- **Recommended fix:**
  - Add a new, uniquely named wrapper `_rcpm_gh_to_file()` and point the 8 call sites at it (lines 138, 194, 231, 235, 240, 247, 280).
  - Delete the shadowing definition in the same change; this is a script-local function, not a public identifier.
  - Leave the pass-through fallbacks as they are; they are intentional fail-open behaviour.

**DUP-003** — Inline `GH_PAT_BUDGET` start/end blocks (19 copies)
- **Severity:** Low | **Category:** `duplication`
- **Locations:**
  - `review_autofix.yml:386, 715, 749, 933, 960, 1025, 1098, 1434, 1501, 1762, 1937, 5698`
  - `clarify.yml` (3, e.g. `:80-87`)
  - `workflow-failure-heal-intake.yml` (2)
  - `orchestrate_poll.yml` (1)
  - `validation-improvements-intake.yml` (1)
  - The shared helper `gh_pat_budget` (`gh_helpers.sh:31-77`) is called directly only by `review_autofix_sweep.yml:133-135, 583-585`.
- **Description:**
  - The inline copies read only the `/rate_limit` body. They skip the helper's core-header override (`gh_helpers.sh:50-60`) and are about 12 lines each, roughly 10 KB in `review_autofix.yml` alone.
  - The start steps run before any checkout, which is why they are inline.
- **Recommended fix:** convert the **end** steps, which all run after support is staged, to `source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" && gh_pat_budget end <wf> <job> "${GH_PAT_BUDGET_FILE}"`, using the existing `grep -q '^gh_pat_budget()'` guard pattern from the sweep. Keep the start steps inline. The output line format is identical, so `collect_workflow_logs.py`'s `PAT_BUDGET_LINE.fullmatch` is unaffected.

**DUP-004** — Autofix failure-marker computation (4 copies, now diverged)
- **Severity:** Medium | **Category:** `duplication`
- **Locations:** `review_autofix.yml:4930-4949`, `:5021-5039`; `review_autofix_step_post_editor_summary.sh:97-117`; `review_autofix_step_assemble_failure_evidence.sh:63-79`.
- **Description:** see BUG-005; the copies have already drifted.
- **Recommended fix:**
  - New `scripts/review_autofix_failure_marker.sh` exposing `review_autofix_failure_marker_args` (prints the canonical `--summary-line-file`/`--evidence-file …` list) and `review_autofix_failure_marker_line [--failure-reason R]` (prints the `marker=` value).
  - Source it from the 3 non-canonical sites and from `assemble_failure_evidence`.
  - Register it per the §24 pattern (`REQUIRED_BOOTSTRAP_SCRIPTS`, test registry).

**DUP-005** — Telegram notification step prologues in `review_autofix.yml`
- **Severity:** Low | **Category:** `duplication`
- **Locations:** `:4917-5007`, `:5008-5153`, `:5154-5310` (and failure alerts at `:5452+`).
- **Description:** each step repeats `source tg_helpers.sh`, `source gh_helpers.sh`, the `gh_retry` shim, and `PR_URL`/`RUN_URL` construction from `${{ github.server_url }}/${{ github.repository }}`. About 370 lines in total.
- **Recommended fix:** move each body to `scripts/review_autofix_step_telegram_<slug>.sh`, following the §24 `review_autofix_step_<slug>.sh` precedent. Build URLs from the runner's default `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID` env vars. This also shrinks the workflow file and removes these bodies from expression evaluation (EXPR-001).

**DUP-006** — "Resolve workflow support ref" block (16 copies in 10 workflows)
- **Severity:** Low | **Category:** `duplication`
- **Locations:** e.g. `sync_ai_labels.yml:37-57`, `cancel_on_pr_close.yml:25-46`; `support_ref_pin="stable"` occurs in `implement.yml` ×3, `orchestrate.yml` ×2, `orchestrate_poll.yml` ×2, `clarify.yml` ×2, `plan.yml` ×2, and once each in 5 others.
- **Description:** the copies differ only in the workflow-filename regex. The block runs before any checkout, so it cannot be a script; it must stay inline.
- **Recommended fix:** keep it inline, but add a contract test asserting that all copies are byte-identical apart from the `<file>\.yml@` token, so a security fix to the pin regex cannot miss a copy.

### Section 4: Expression Size Limit Risk Assessment

**Context:**
- The 21,000-character limit is already enforced in CI for `steps[].run` bodies containing `${{`, with an 18,000-character guard (`tests/test_workflow_file_size_limit.py:28-32`, `:66-98`). If CI is green, no run body is at or above the High (85%) threshold.
- No byte or character measurements were possible in this audit. All estimates below are lines × about 70–75 characters per line [NEEDS VERIFICATION with `len(step["run"])` using the test's own loader].

**EXPR-001** — `.github/workflows/review_autofix.yml:5014-5153`, `:5167-5310`, `:2800-2934`, `:4923-5007`
- **Severity:** Low | **Category:** `expression-limit`
- **Description:** these are the largest **interpolating** run bodies found:

  | Step | Lines | Interpolations | Estimated size | Headroom to 21k |
  |---|---|---|---|---|
  | Telegram editor-noop-suspicious warning (`:5014`) | ~140 | `${{ github.server_url }}/${{ github.repository }}` at 5018-5019, 5078, 5129, 5151 | ~10–11k chars | ~10k |
  | Telegram review-blocked judge decision (`:5167`) | ~143 | 5171-5172, 5176 | ~10–11k chars | ~10k |
  | Checkout PR head branch (`:2800`) | ~135 | `${{ github.repository }}` at 2855 | ~9–10k chars | ~11k |
  | Telegram editor-changes-lost warning (`:4923`) | ~85 | 4927-4928, 4995 | ~6k chars | ~15k |

  - All are below the 15,000 Medium threshold, but they are prompt- and comment-building bodies that grow with each incident fix.
  - By contrast, the 310-line deterministic-skip-merge body (`:1117-1427`) has **no** `${{ }}` in its body (all values come through `env:`), so it is correctly excluded from the limit.
- **Recommended fix:**
  - Replace each in-body `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` with `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`, which runners set by default. Behaviour is unchanged and each body drops out of expression evaluation entirely.
  - Then extract the bodies per DUP-005.

**EXPR-002** — `tests/test_workflow_file_size_limit.py:66-98`; long `if:` lines at `review_autofix.yml:3767`, `implement.yml:6245`, `:6341`, `:6461`, `update_workflows.yml:1030`, `:1591`, `issue_pr_status.yml:914`
- **Severity:** Low | **Category:** `expression-limit`
- **Description:**
  - The guard checks only `steps[].run`. It does not check step or job `if:`, `env:` values, `with:` inputs, or `concurrency.group`.
  - The longest `if:` conditions found are roughly 450–700 characters, about 2–3% of the limit, so there is no current risk. The gap would hide a future large `with:` prompt input.
  - The prompt cites a 1 MB workflow-file limit. This repo's measured and documented limit is **512,000 bytes** (`agents.md:2360-2366`), guarded at 480,000 (test lines 26-27, 44-59), with an early warning at 440,000 (`agents.md:2349-2354`). No workflow can exceed 800 KB while CI is green.
  - Largest files by line count: `test-and-mark-stable.yml` (~7,585 lines), `implement.yml` (~6,773), `review_autofix.yml` (~5,709). Their byte sizes relative to 480,000 need `wc -c` [NEEDS VERIFICATION].
- **Recommended fix:** extend the test to walk every string scalar under `jobs.*` that contains `${{` and apply the same 18,000-character guard. Separately, collapse `implement.yml:6245/6341/6461`'s identical 6-clause condition into one step output (DEBT-001).

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `scripts/gh_helpers.sh:91`, `:108`
- **Severity:** Low | **Category:** `dead-code`
- **Description:** `gh_review_pr_state` accepts `merged` from `gh api repos/{r}/pulls/{n} --jq .state`, but the REST `state` field is only `open` or `closed` (merge status is the separate `merged` boolean). The `merged` branch is unreachable.
- **Recommended fix:** leave the regex (it is harmless and fail-safe) and add a comment. If callers need a merged signal, use `--jq 'if .merged then "merged" else .state end'`.

**DEAD-002** — `.github/workflows/review_autofix_sweep.yml:219`
- **Severity:** Low | **Category:** `dead-code`
- **Description:** `source "${GH_API_RETRY_HELPERS:-/dev/null}"` always sources `/dev/null` here. The variable is set only by `Bootstrap GitHub API retry helper` steps in other workflows (`validate.yml:167`, `implement.yml:178`, `review_autofix.yml:376`, `security-audit.yml:122`, `orchestrate_clarify_respond.yml:132`), and the sweep has no such step. It is also redundant with lines 216-217.
- **Recommended fix:** check whether `tests/test_workflow_gh_retry_fallback_contract.py:406` requires the line verbatim before removing it. If it does, leave it.

**DEBT-001** — `.github/workflows/implement.yml:6245`, `:6341`, `:6461`
- **Severity:** Low | **Category:** `tech-debt`
- **Description:** the same 6-clause `(failure() || cancelled()) && steps.preflight_destructive_guard.outputs.… == '' && steps.commit_changes.outputs.… == ''` condition is copied verbatim into 3 steps. Adding a 4th block reason means editing all three.
- **Recommended fix:** emit one `guard_blocked=true|false` output from the commit step (or a tiny step after it), and use `if: (failure() || cancelled()) && steps.<id>.outputs.guard_blocked != 'true'`.

**TODO/FIXME/HACK markers:** none found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`. Debt is carried in issue-numbered comments instead.

**CONSIST-001** — `set -uo pipefail` in 20 run blocks across 14 workflows
- **Severity:** Low | **Category:** `consistency`
- **Locations:** e.g. `review_autofix.yml:1544`, `:4505`, `:4734`, `:5648`; `review_autofix_sweep.yml:182`.
- **Description:**
  - When `shell:` is unspecified (most steps; `shell: bash` appears only in 5 workflows), GitHub runs `bash -e {0}`, so errexit stays on despite the deliberate omission of `-e`.
  - The current bodies guard nearly every command with `|| true`, so nothing breaks today, but the idiom misleads future edits.
- **Recommended fix:** where fail-open is intended, write `set +e` followed by `set -uo pipefail`, or add a one-line comment noting that errexit is inherited.

**CONSIST-002** — `.github/workflows/review_autofix_sweep.yml:274`, `:288`, `:485-491`, `:552`
- **Severity:** Low | **Category:** `consistency`
- **Description:**
  - The `AUTOFIX_SWEEP_END` field set differs between exits:
    - line 288 omits `skipped_merge_queued` and `snapshot_incomplete`;
    - line 274 omits `snapshot_incomplete`.
  - The `claude_capacity_wait` skip (lines 488-491) increments no counter, so the totals do not add up to `candidates`.
  - The prior report reads `snapshot_incomplete=` from this line.
- **Recommended fix:** add `skipped_capacity_wait`, and emit the full field set (with defaults) on all three exits.

**CONSIST-003** — Raw `gh` calls without retry where helpers are in scope
- **Severity:** Low | **Category:** `consistency`
- **Locations and description:**
  - `lint-pr-body-auto-close.yml:85-86`: a required check that fails on one transient 5xx.
  - `review_collect_pr_metadata.sh:522`: `gh pr diff`, although `gh_helpers.sh` is sourced at line 35.
  - `retarget_merged_base.sh:54`, `:64`, `:150`: fail-open by design, so acceptable.
- **Recommended fix:**
  - Wrap the lint commits read in `gh_retry`, staging `scripts/gh_helpers.sh` from the existing checkout. `gh_retry` buffers stdout per attempt (`gh_helpers.sh:1043-1054`), so the `--jq` output stays clean.
  - Use `gh_retry_to_file "${PR_DIFF_FILE}" gh pr diff "${PR_NUMBER}"` in `review_collect_pr_metadata.sh`.

**SHELL-001** — `scripts/review_collect_pr_metadata.sh:279`, `:367` (vs trap at `:37-42`)
- **Severity:** Low | **Category:** `shellcheck`
- **Description:** `_linked_tmp="$(mktemp)"` and `_linked_json_file="$(mktemp)"` are created outside `TMP_RUNTIME_DIR`. Under `set -euo pipefail` (line 31), an exit between creation and the `rm -f` at lines 291 and 398 leaks them, because the EXIT trap cleans only `TMP_RUNTIME_DIR`.
- **Recommended fix:** use `mktemp "${TMP_RUNTIME_DIR}/linked_issues.XXXXXX"` and `mktemp "${TMP_RUNTIME_DIR}/linked_json.XXXXXX"`.

**Shellcheck spot-checks:** no SC2046 (`for x in $(gh|curl|jq …)`) or SC2006 (backtick) patterns were found in `scripts/*.sh`. The `A && B || C` constructs that were sampled (e.g. `retarget_merged_base.sh:62`) are intentional guard chains.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 10 | BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, BUG-007, API-001, DUP-001, DUP-002, DUP-004 |
| Low | 23 | BUG-008, BUG-009, BUG-010, SEC-001, SEC-002, API-002, API-003, API-004, API-005, API-006, DUP-003, DUP-005, DUP-006, EXPR-001, EXPR-002, DEAD-001, DEAD-002, DEBT-001, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/orchestrate_poll.yml` (1) | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `.github/workflows/review_autofix_sweep.yml`, `scripts/lint_pr_body_auto_close.py`, `scripts/review_collect_pr_metadata.sh`, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh` (6) | Medium |
| Code modularization | `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, `scripts/review_autofix_step_mark_ready_to_merge.sh`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh`, `scripts/review_autofix_step_post_editor_summary.sh`, `scripts/review_autofix_step_assemble_failure_evidence.sh`, new `scripts/review_autofix_failure_marker.sh`, new `scripts/review_autofix_step_telegram_*.sh`, `.github/workflows/review_autofix.yml`, `.github/workflows/clarify.yml`, plus a support-ref contract test (~15) | Large |
| Expression size reduction | `.github/workflows/review_autofix.yml`, `tests/test_workflow_file_size_limit.py` (2) | Small |
| Medium/Low fixes | `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/internal-review.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/implement.yml`, `.github/workflows/clarify.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/review_autofix_step_post_editor_summary.sh` (11) | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-17)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: every precondition has been proven by reading the code. The calls hit the same endpoint (or a strict field subset) with the same pagination, filters and auth. They sit in one function or step, with no mutation between them, the same error and retry semantics, no concurrency boundary, and no broken cache contract. The implement stage may act on these directly.
- **`NEEDS_VERIFICATION`**: the change looks correct, but at least one precondition cannot be proven from reading the code alone. A human or a later analysis must run the listed checks first.
- **`RISKY_SKIP`**: the call is in a retry, backoff or race-defence path, is paginated, is rate-limit sensitive, lives in `orchestrate_poll_process.sh`, or would change log keys. It is listed so it stays visible, and must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001: `NEEDS_VERIFICATION`**: the post-merge validate dispatch reads labels one issue at a time for issues found only in the PR body
- **Files:**
  - `.github/workflows/review_autofix.yml:887-921`: the per-issue loop.
  - `.github/workflows/review_autofix.yml:882`: where the body-fallback nodes get `labels: null`.
- **Endpoint:** `GET repos/{repo}/issues/{n}` via `gh issue view N --json labels`, once per issue.
- **Call count:** N now (one per body-text-referenced issue whose labels are unknown). Proposed: 1 aliased GraphQL query.
- **Evidence:** issues found by the PR body/title fallback get `labels: null`:
  ```
  issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0)) | map({number: tonumber, labels: null})')"
  ```
  The loop then makes one call per such issue (`:890-891`):
  ```
  if [ "${labels_known}" != "true" ]; then
    if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
  ```
  If one issue fails, only that issue is skipped (`:897-899`, `continue`).
- **Proposed fix:**
  - Before the `while` loop, collect the numbers with `labels == null` and fetch them in one query: `repository(owner,name){ iK: issue(number:N){ labels(first:100){nodes{name}} } … }`.
  - Follow the batching and fail-open contract of `_fetch_linked_issue_bodies_graphql` (`scripts/review_collect_pr_metadata.sh:76-181`), using GraphQL variables for owner and name (see SEC-002).
  - Rewrite the hydrated nodes into `issue_nodes_json` so `labels_known=true`.
  - Treat a missing alias as unknown, so that issue alone is skipped as today. Keep the per-issue `gh issue view` only as the fallback when the whole query fails.
- **Safety rationale:** the error semantics change from per-call to per-alias. The `gh_retry` in this job is the inline reimplementation at `:795`, not `gh_helpers.sh`, so retry behaviour differs. Static reading cannot prove that `labels(first:100)` covers the same set as `gh issue view --json labels`.
- **Downstream signal:** before implementing, (1) confirm how many labels `gh issue view --json labels` returns for an issue with more than 100 labels, and match it; (2) grep `tests/` for the string `Unable to read labels for issue #` and for any test that counts `gh issue view` calls in this step; (3) confirm a partial-alias GraphQL `errors` response maps to "skip this issue", not "skip all".

**MERGE-002: `RISKY_SKIP`**: the E2E bait step reads the same PR twice back to back
- **Files:** `.github/workflows/test-and-mark-stable.yml:1579-1588` (the `HEAD_B` read at `:1582`) and `:1609` (the `PR_META` read).
- **Endpoint:** `GET repos/{TEST_REPO}/pulls/{N}`, both times.
- **Call count:** 2 per stability attempt plus 1 after the loop now. Proposed: 2 per attempt plus 0, reusing the last `HEAD_B` payload.
- **Evidence:** on the iteration that breaks, `HEAD_B` (`:1582`) and `PR_META` (`:1609`) hit the same endpoint, with no sleep or mutation between them:
  ```
  HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  …
  PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" 2>/dev/null || echo "")
  ```
- **Proposed fix:** have the `HEAD_B` read keep the full payload, take `.head.sha` from it, and reuse it as `PR_META` when the loop exits via `break`. Keep the separate `PR_META` read when all 5 attempts are used up.
- **Safety rationale:** this loop exists to defend against an upstream race (comment at `:1571-1578`), and the read after the loop is the fail-fast guard against a closed or merged PR (`:1590-1608`). Both are RISKY_SKIP triggers. The saving is 1 call per E2E stable-gate run.
- **Downstream signal:** do not implement automatically. A reviewer must confirm the closed/merged guard keeps its freshness on the exhausted-loop path. The saving probably does not justify touching race-protection code.

**MERGE-003: `RISKY_SKIP`**: the deterministic-skip job refreshes the PR head over REST and reads closing references over GraphQL separately
- **Files:**
  - `.github/workflows/review_autofix.yml:1207-1225` (`deterministic_skip_head_is_current`, called at `:1263` and `:1314`).
  - `.github/workflows/review_autofix.yml:1397-1403` (the `closingIssuesReferences` query).
- **Endpoints:** `GET repos/{repo}/pulls/{n}` (`.head.sha`) and GraphQL `pullRequest.closingIssuesReferences`.
- **Call count:** 2 now, on paths where both run. Proposed: 1 GraphQL query `pullRequest(number:$n){ headRefOid closingIssuesReferences(first:50){nodes{number}} }`.
- **Evidence:** both calls read the same PR in the same step. The head refresh exists only "on paths that intentionally skip gh pr merge" (`:1213-1215`). The closing-references query runs later, after the `ai:review-skipped` label POST (`:1376`) and the `gh pr merge --auto` calls (`:1305`, `:1342`).
- **Proposed fix:** none to auto-apply. A merge would mean moving the closing-references read before the merge and labelling mutations.
- **Safety rationale:** the head read is a freshness guard against a push landing after the gate, which is race defence. The two calls have different failure semantics:
  - A failed head refresh refuses the merge-authorization labels.
  - A failed closing-references read only skips issue labelling.

  This step's `gh_retry` is also the inline shim that retries every failure 4× (BUG-006).
- **Downstream signal:** do not implement automatically. Revisit only after BUG-006 replaces the inline shim. A reviewer must also confirm that sharing the head-freshness read with the closing-references read keeps "refuse labels on refresh failure" separate from "skip issue labelling on lookup failure".

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: `NEEDS_VERIFICATION`**: the post-merge dispatch re-queries `closingIssuesReferences` after the gate has already found none
- **Files:**
  - `scripts/review_autofix_step_evaluate_gate.sh:187-207`: the gate fetch and cache.
  - `scripts/review_autofix_step_evaluate_gate.sh:1362-1363`: the gate outputs.
  - `.github/workflows/review_autofix.yml:318-319`: the job outputs.
  - `.github/workflows/review_autofix.yml:735`: the job `if:`.
  - `.github/workflows/review_autofix.yml:829-848`: the live re-fetch.
- **Endpoint:** GraphQL `repository.pullRequest(number).closingIssuesReferences(first:50){nodes{number labels(first:100){nodes{name}}}}`. The query text is identical at both sites (`evaluate_gate.sh:194` and `review_autofix.yml:839`).
- **Call count:** 2 now for every merged PR with zero closing references. Proposed: 1. Merged PRs that do have closing references already make 1 call.
- **Evidence:**
  - The gate tracks whether its cache is authoritative, but never exports that flag:
    ```
    POST_MERGE_LINKED_ISSUES_JSON="$(printf '%s' "${_post_merge_linked}" | jq -c '.' 2>/dev/null || echo '[]')"
    post_merge_linked_issues_cache_known="true"
    ```
  - The post-merge job cannot tell "known empty" from "fetch failed", so it re-fetches on any `[]`:
    ```
    if [ "${issue_nodes_json}" = "[]" ]; then
      if issue_nodes_json_live="$(gh_retry gh api graphql \
    ```
  - The only job-level skip is `post_merge_validate_context_definitely_empty` (`review_autofix.yml:735`). It requires an empty title **and** body (`evaluate_gate.sh:202-205`). `pr_title_cached` comes from the event's `PR_TITLE` or the gate's successful `pulls/{n}` read (`evaluate_gate.sh:52`, `:116-118`), and `POST_MERGE_DISPATCH` requires that read to have succeeded (`pr_state=closed`, `:154-159`).
  - Inference: GitHub PR titles are never empty, so that skip appears never to fire. The job, and its re-fetch, then run for every merged PR.
  - For the same reason, the live `pulls/{n}` title/body re-fetch at `review_autofix.yml:851-860` looks unreachable whenever the gate succeeded. It is not a separate finding, but it should be checked in the same pass.
- **Proposed fix:**
  - Add a new gate output, `post_merge_linked_issues_known=${post_merge_linked_issues_cache_known}`. The name was grepped and is unused in the repo; do not reuse the local variable name as an output name.
  - Add it to the gate job's `outputs:` block next to `:318`, and pass it to `post-merge-validate-dispatch` as an env value next to `:742`.
  - Change `:834` to re-fetch only when that flag is not `'true'`.
  - Keep the live re-fetch for the gate-failure case. The gate's call at `evaluate_gate.sh:190` is a raw `gh api graphql` with no retry, so the re-fetch is the only retried path when the gate's call fails.
- **Safety rationale:** the cached data crosses a job boundary (`gate` to `post-merge-validate-dispatch` via `needs.gate.outputs`). The two sites also have different retry semantics: one bare attempt in the gate, the inline `gh_retry` (`:795`) in the dispatch job. "Same function / same step" therefore fails, even though the query and its filters are identical.
- **Downstream signal:** before implementing, (1) grep `tests/` for `Unable to refresh closingIssuesReferences for merged PR` and `post_merge_linked_issues_json` to find contract tests that expect the live re-fetch; (2) confirm the gate's `--jq '… // [] | map(…)'` prints `[]`, not an empty string, on a successful empty result, so `cache_known=true` really means "known empty"; (3) decide whether the dead `definitely_empty` condition should become "known-empty linked issues and no body-text refs". That is a behaviour change and needs its own review.

### Dead Calls (DEAD-API-###)

**DEAD-API-001: `NEEDS_VERIFICATION`**: the merge train always tries to dispatch the missing `ai-review.yml` first in this repo
- **File:** `scripts/review_merge_train.sh:1316-1331` (`_mt_dispatch_review`, loop at `:1323-1329`). The default is documented at `:63`.
- **Endpoint:** `gh workflow run <wf>`, which resolves the workflow and then calls `POST repos/{repo}/actions/workflows/{wf}/dispatches`.
- **Call count:** at least 2 per released PR now (a failed `ai-review.yml` resolution, then the `internal-review.yml` dispatch). Proposed: 1 in this repo.
- **Evidence:**
  ```
  for wf in ${MERGE_TRAIN_DISPATCH_WORKFLOWS:-ai-review.yml internal-review.yml review_autofix.yml}; do
  	if gh_retry gh workflow run "${wf}" --repo "${MT_REPO}" \
  ```
  - `.github/workflows/ai-review.yml` does not exist in this repo (glob returns nothing). It ships only as a consumer template, per API-001.
  - `MERGE_TRAIN_DISPATCH_WORKFLOWS` is set nowhere in `.github/workflows/`, `scripts/` or `workflow-templates/` (grep: only `review_merge_train.sh:63` and `:1323`).
  - The first attempt therefore always fails here. Its result is discarded and only selects the next candidate.
  - There is no retry amplification: `_gh_cmd_is_unsafe_post` classifies `workflow run` as an unsafe POST (`scripts/gh_helpers.sh:813`), so `gh_retry` caps it at 1 attempt (`gh_helpers.sh:1067-1069`).
- **Proposed fix:** do not change the script default, because consumer repos rely on `ai-review.yml` being first. In this repo only, set `MERGE_TRAIN_DISPATCH_WORKFLOWS: "internal-review.yml review_autofix.yml"` in the `env:` of the steps that invoke the merge-train release.
- **Safety rationale:** the saving is a single unretried call per release, and the window showed `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0`. Static reading cannot show whether the invoking workflows are reusable or copied to consumers. If they are, a plain env override would break consumers' `ai-review.yml` dispatch.
- **Downstream signal:** before implementing:
  1. Grep every invoker of `review_merge_train.sh` (release and gate modes).
  2. Confirm each one is not under `workflow-templates/`, is not a `workflow_call` target used by consumer repos, and is not synced to consumers by `update_workflows.yml`.
  3. If any is shared, scope the override with an expression on `github.repository`, or drop the finding.

### Cross-References to Deep Audit Section
- API-001: `RISKY_SKIP`. The fix lives in `orchestrate_poll_process.sh` (`_has_active_autofix_run`, `:22069-22088`), which is a dispatch-dedupe race guard. One correction: `review_autofix.yml` is **not** `workflow_call`-only. It declares `workflow_dispatch:` at `review_autofix.yml:82` ("used by merge-ref fallback re-trigger") and is the third merge-train dispatch target (`review_merge_train.sh:1323`), so its `--workflow review_autofix.yml` lookup can return real runs and must stay. Only the `ai-review.yml` lookup is always empty here. Note too that `_is_gh_permanent_failure`'s `['"][^'"]+['"] not found` pattern (`gh_helpers.sh:194`) needs a quoted name, which supports the retry-amplification concern (the exact gh stderr is still unverified).
- API-002: `RISKY_SKIP`. **Do not drop `review_autofix.yml` from the sweep snapshot (`review_autofix_sweep.yml:437`, `:504`).** The finding's premise ("its 3 listings always come back empty") is wrong for the same reason as the API-001 correction: direct `workflow_dispatch` runs of `review_autofix.yml` exist through the merge train's fallback. Dropping it would open a dedupe blind spot (double review dispatch). The listings are also paginated.
- API-003: `NEEDS_VERIFICATION`. Batching per-issue `gh issue view` into one aliased GraphQL query per repo is sound. Verify the exit-2 fail-closed contract of `scripts/lint_pr_body_auto_close.py` when only some aliases fail (CLAUDE.md §21 enforcement must not fail open).
- API-004: `RISKY_SKIP`. It replaces two `--paginate` REST listings (`review_collect_pr_metadata.sh:235`, `:247`) with GraphQL cursor pagination, which changes page-boundary semantics. Downstream readers also use REST-only fields (`original_line`, `:485`).
- API-005: `NEEDS_VERIFICATION`. The memo in `label_helpers.sh:161-195` is safe within one process. Verify no caller deletes and recreates a label in the same process, and that a cached "exists" entry is never written after a failed create (only `return 0` paths at `:180-181` and `:188-190` should populate it).
- API-006: `RISKY_SKIP`. It shares memo files across the 4 `orchestrate_poll_process.sh` invocations, which falls under the blanket `orchestrate_poll_process.sh` exclusion. It also crosses process boundaries in a path that defends against upstream races.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 5 | MERGE-001, REUSE-001, DEAD-API-001, API-003 (xref), API-005 (xref) |
| RISKY_SKIP | 6 | MERGE-002, MERGE-003, API-001 (xref), API-002 (xref), API-004 (xref), API-006 (xref) |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
- Highest-value follow-up once verified: REUSE-001, which saves 1 GraphQL call per merged PR with no closing references and appears to apply to every merged PR, because the `definitely_empty` job skip looks unreachable.
- Hold API-002 (and the `review_autofix.yml` half of API-001): the source shows their premise is wrong.

Assumptions and missing context: this pass only read code; nothing was executed and no files were changed. I assumed GitHub never allows an empty PR title; REUSE-001 depends on that and lists a check for it. I did not confirm gh's exact error text for an unknown workflow, or whether the workflows that run the merge-train release are shared with consumer repos.