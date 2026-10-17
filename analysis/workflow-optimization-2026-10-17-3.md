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