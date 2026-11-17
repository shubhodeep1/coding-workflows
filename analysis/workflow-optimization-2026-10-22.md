## Executive Summary

- **The only failure in the window (run 39694107256, Test & Mark Stable Release) was very likely caused by the watcher giving up too early, not by validation-refresh failing.** The failing step was `orphan-workflows-test` → `Dispatch & watch — validation-refresh`. The only validation-refresh run in the window (39694338090) started at 23:35:00Z, about 2 minutes after the parent, and **succeeded after 2434s**. The watcher is called with `--completion-timeout-secs 2400` (`.github/workflows/test-and-mark-stable.yml:5393`). Its deadline is counted from the moment it dispatches (`COMPLETION_DEADLINE=$((DISPATCH_STARTED_AT + COMPLETION_TIMEOUT_SECS))`, `scripts/dispatch_and_watch_workflow_run.sh:300`). validation-refresh's own job is allowed 60 minutes (`.github/workflows/validation-refresh.yml:70`). *Inference:* the watcher stopped about 34s before the child finished. The step log was not available, so the exact `::error::` line is unconfirmed. **Impact:** this blocked a stable release. `auto_release_stable` run 39703352526 then skipped with `release_in_flight active_runs=2`, and a high-effort Claude heal intake (39703411368) was triggered. **Confidence: medium-high.**
- **The timeout path leaves no diagnostics behind.** On timeout, the watcher exits at line 329–330 without writing `run_id` to `GITHUB_OUTPUT`. The `Soft-error analyser (validation-refresh)` step only runs when `steps.dispatch_validation_refresh.outputs.run_id != ''`, so it was silently skipped. The best diagnostic fix is to write `run_id`, a `watch_timeout` conclusion and a structured timeout line before exiting. **Confidence: high (code-verified).**
- **review_autofix accounts for 99.5% of LLM tokens:** 24,921,955 of 25,054,850. Its cache hit rate is 0.757. It is the slowest high-volume family: p50 675s, p95 2574s, with outliers up to 4850s (run 39606614099). Cache hit rate varies widely between runs: 0.650 (39700843909) against 0.872 (39616808010). Getting the weak runs up to about 0.85 would cut about 1.7M uncached input tokens per such run. **Confidence: medium (only 8 of 80 runs have token telemetry).**
- **Runner queueing is a measurable share of short runs.** Push bursts at 00:13:02 and 00:23:35 fanned out into CI, the promote follower, historical triage and the PR6576 follow-up all at once. Queue waits were 49s of 58s (39698340276), 49s of 57s (39698340253) and about 164s before CI started (39700843718). Two of those families are pure no-ops (`already_filed`, `marker_present`) and are already listed in `docs/scripts-pending-removal.md`. **Impact:** about 1–3 minutes less CI queueing per push burst. **Confidence: medium.**
- **Semble bootstrap cost is poorly amortised.** Bootstraps took 490s in total for 6 queries, about 82s of bootstrap per useful query. 5 of the 11 bootstraps (217.7s) went unused, all in workflow_log_analysis run 39694341945. All 40 `SEMBLE_FALLBACK` lines are `context=contract-test` (test fixtures), and runtime fallbacks are 0, so this is healthy fail-open behaviour. **Confidence: high.**
- **API and orchestrator observability gaps hide where time and calls go.** Every sampled `GH_PAT_BUDGET` line reports `used_in_job=unknown` (runs 39703204534, 39700843909, 39703411368). Orchestrate-poll logs lose about 850k characters to truncation (39702035500), so poll hot-spots cannot be attributed. **Confidence: high.**

## Speed Optimizations

Ranked by expected reduction in end-to-end latency.

1. **Critical path: stop false-failing the release gate (validation-refresh watcher timeout).**
   - **Evidence:** parent 39694107256 ran for 5422s and failed at `Dispatch & watch — validation-refresh`. The child 39694338090 succeeded after 2434s. The watch timeout is 2400s measured from dispatch, while the child's job timeout is 60 minutes.
   - **Root cause:** the watcher's budget is shorter than the child's own budget. validation-refresh's discovery budget alone defaults to 2100s (`VALIDATION_DISCOVERY_BUDGET_SECS`, `validation-refresh.yml:175`), plus clone, render/lint and self-test time.
   - **Change:** raise `--completion-timeout-secs` (and `--registration-timeout-secs`) for this step from 2400 to 3300. Raise the step's `timeout-minutes` from 42 to 57, and the `orphan-workflows-test` job's `timeout-minutes` from 110 to 125. The job budget comment at lines 5315–5325 adds up the step deadlines (2400+600+2400), so its arithmetic needs updating too.
   - **Savings:** avoids a full release-gate rerun (about 90 minutes of wall clock) plus a heal-pipeline invocation each time this happens.
   - **Risk:** low. A run that is really stuck is still killed, at 57 minutes instead of 42.

2. **Critical path: cut runner queueing during push bursts.**
   - **Evidence:** "All GitHub-hosted runners with label [ubuntu-latest] are busy" appears in 39698340276 (49s wait), 39698340253 (49s), 39698340309 (about 38s) and 39698340389 (about 91s). CI 39700843718 waited about 164s and CI 39699408469 about 2m40s.
   - **Root cause:** per-push fan-out of no-op workflows. `check-failure-triage-historical.yml` logs `HISTORICAL_TRIAGE skip reason=already_filed issue=7300`. `pr6576-shard-followup.yml` logs `PR6576_FOLLOWUP outcome=skip reason=marker_present issue=7072`, and that run also installs pytest and runs 32 self-tests.
   - **Change:** run the removal preflight checks already recorded in `docs/scripts-pending-removal.md` (entries at lines 212 and 320), and remove whichever workflows pass them.
   - **Savings:** about 3 fewer runner jobs per push to main. *Inference:* 30–90s less CI queueing per burst.
   - **Risk:** low, because removal is gated by the documented preflights.

3. **Faster checkout in `cancel_on_pr_close` / `heal-pr-reconcile`.**
   - **Evidence:** run 39703075676 took 146s. Its visible log tail is a full `git fetch` creating many `v1.x` tags, and about 556k characters were truncated.
   - **Change (inference):** if reconcile only needs `main` HEAD, set `fetch-depth: 1` and `fetch-tags: false` on that checkout.
   - **Savings:** about 60–120s per PR close.
   - **Risk:** low. First confirm the reconcile script does not read tags or history.

4. **Micro-optimisation: Semble lazy bootstrap in workflow_log_analysis.**
   - **Evidence:** run 39694341945 had 5 bootstraps, 5 unused, costing 217,721 ms.
   - **Change:** make sure lazy mode only bootstraps on the first real query in that workflow.
   - **Savings:** about 3.6 minutes per analysis run.
   - **Risk:** low.

## Cost Optimizations

Ranked by expected token savings.

1. **Stabilise the review_autofix prompt prefix to lift the cache hit rate.**
   - **Evidence:** run 39700843909 had a hit rate of 0.650, with 1,997,372 uncached prompt tokens and 770,060 cache-write tokens across 20 calls. Run 39607014195 had 0.725 (592,449 writes, 10 calls). Run 39616808010 had 0.872 with **0** writes.
   - **Root cause (inference):** cache fragmentation. Volatile content such as PR diff, run IDs or timestamps likely sits ahead of stable instructions in some reviewer/editor prompts, and calls with more fan-out (20 calls) re-write the prefix more often.
   - **Change:** order prompts as static system rules + repo context → Semble block → PR-specific diff and findings last. Keep the reviewer prompts byte-identical across parallel reviewers.
   - **Savings:** bringing a 0.65 run up to 0.85 removes about 1.6–1.75M uncached input tokens per run. Across the 80 review_autofix runs this is substantial, but the scale-up is an inference from 8 sampled runs.
   - **Quality risk:** none. Content stays the same and only its order changes.

2. **Avoid unnecessary review_autofix rounds.**
   - **Evidence:** PR #7832 went through at least 2 review_autofix runs: 39697661636 `[pr:7832]` (1999s) and 39700843909 (1651s, about 7.9M tokens). It was then closed `merged=false` (39703075676).
   - **Change:** emit a per-PR summary of review rounds (see Reliability item 3) so repeated rounds on PRs that end up closed can be measured before anyone tunes gating.
   - **Savings:** unknown until measured.
   - **Risk:** none, because this only adds logging.

3. **Soft-error analyser in the release gate.**
   - **Evidence:** run 39694107256 made 15 OpenRouter calls with a 0.0 cache hit rate, writing 126,154 cache tokens and reading none. `scripts/analyze_soft_errors.py` contains no `cache_control`, so the writes cannot be traced to explicit cache markers.
   - **Change:** confirm which component made these 15 calls (`LOG_ANALYZER_MODEL`, default `openai/gpt-6-luna`). If the provider bills automatic cache writes, either share one stable prefix across phases or turn that off.
   - **Savings:** small, about 126k tokens per gate run.
   - **Risk:** low.

4. **Semble value per query.** review_autofix made 4 queries returning 65,584 bytes from 25 sources (about 16.4 KB per query), with `static_dup_bytes_total=0`. Semble is adding context without duplicating static prompt content, which is good. The cost is about 45s of bootstrap per query (179,484 ms / 4). No change to content is recommended; see Speed item 4 for the bootstrap waste.

5. **Serena** was not active in any sampled run: 0 queries and 0 probes, and `SERENA_ENABLED: false` in 39700843909. There is nothing to optimise.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **Align the watcher timeout with the child's budget, and fail informatively** (release-gate false negatives).
   - **Category:** timeout misconfiguration.
   - **Evidence:** see the Executive Summary.
   - **Fix (budget):** as in Speed item 1.
   - **Fix (diagnostics):** in `scripts/dispatch_and_watch_workflow_run.sh`, replace the bare timeout exit at lines 329–330:
     ```bash
     ELAPSED=$(( $(date +%s) - DISPATCH_STARTED_AT ))
     echo "run_id=${NEW_ID}" >> "${GITHUB_OUTPUT}"
     echo "workflow_conclusion=watch_timeout" >> "${GITHUB_OUTPUT}"
     echo "DISPATCH_WATCH_TIMEOUT workflow=${WORKFLOW_FILE} run_id=${NEW_ID} elapsed_secs=${ELAPSED} completion_timeout_secs=${COMPLETION_TIMEOUT_SECS} last_status=${STATUS:-unknown} last_conclusion=${CONCLUSION:-none}"
     echo "::error::${DISPLAY_NAME} run #${NEW_ID} timed out after ${ELAPSED}s (last status=${STATUS:-unknown})"
     exit 1
     ```
     This lets the soft-error analyser run on the timeout path, and a heal or triage can tell a slow child apart from a failed one.
   - **Fail-open:** the exit code is unchanged, so the gate still fails.
   - **Registration:** register the new log prefix through an `agents.d/` fragment under "Stable log prefixes (contractual)" (§25).
   - **Expected impact:** removes this class of false release failure, and turns the next real timeout into a one-line diagnosis.

2. **Record `GH_PAT_BUDGET` usage per job.**
   - **Evidence:** `used_in_job=unknown` in 39703204534 (a 13s job, so it cannot have crossed an hourly reset), 39700843909 and 39703411368.
   - **Root cause:** `gh_helpers.sh:62–73` only computes `used` when a `phase=start` snapshot exists, `reset` hasn't changed and `remaining` hasn't increased. The short-job case points to a missing start snapshot (inference).
   - **Fix:** keep the existing line as it is. `collect_workflow_logs.py:112` anchors its regex on `used_in_job=...$`, so adding a trailing field would break the parser. Instead, when `used` is unknown, emit a separate `GH_PAT_BUDGET_UNKNOWN phase=end job=... reason=no_start_snapshot|reset_rolled|remaining_increased` line.
   - **Impact:** gives a per-job API-call count, which the GH API audit needs.
   - **Risk:** none.

3. **Attribute time in truncated long runs.**
   - **Evidence:** orchestrate_poll 39702035500 lost about 850,848 characters and 39701211299 about 847,747. review_autofix 39700843909 lost about 1.67M. CI job `tests-release-and-log-analysis` takes about 10.5 minutes with no visible dominant step (39700843718, 39699408469, 39698340389).
   - **Fix:**
     - Emit a single compact end-of-step phase-timing summary line in the poll and review jobs, so it survives truncation.
     - Add pytest `--durations=20` to the test invocations in `tests-release-and-log-analysis`.
   - **Impact:** enables the next round of speed work.
   - **Risk:** none.

4. **Fix stale memory lineage.**
   - **Evidence:** run 39703075708 shows `AI_MEMORY_TELEMETRY finalize-task ok=true` and closes issue 7827, but implement run 39696343270 is still recorded as `in_progress`.
   - **Fix:** in `finalize-task`, mark any non-terminal run entries for the issue as `abandoned`/`closed`, and emit a count of how many were changed.
   - **Impact:** prevents stale "in progress" lineage from confusing later memory retrieval.

5. **Semble and Serena fallbacks are healthy.**
   - There were 40 `SEMBLE_FALLBACK` lines, all `contract_test` (counted by `_is_contract_test_semble_line`, `cost_audit.py:381`): CI 32 (8 each in 39700843718, 39699408469, 39698340389; 4 each in 39606613883, 39606990887) and test_and_mark_stable 8. Runtime fallbacks were 0 and failed bootstraps 0.
   - There were no `SERENA_PROBE` or `SERENA_FALLBACK` lines.
   - There were 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` lines in every family. No action is needed.

## AI Memory Health

- **Only one `AI_MEMORY_TELEMETRY` line was found** (from the `log_summary` of 39703075708): `{"did_push": true, "final_state": "closed", "issue_number": 7827, "ok": true, "op": "finalize-task"}`. It reported `push_attempts: 2` (one push retry) and took about 136s of the 187s run in "Finalize linked issue lineage state".
- **No `retrieve` telemetry was found**, so retrieval hit rate, `estimated_tokens` against budget, and the `keyword_method` mix cannot be calculated. No `fail_open: true` or `enabled: false` entries were observed. `AI_MEMORY_ENABLED: true` is set in 39703075708.
- **Push contention:** the one finalize needed 2 push attempts. A single sample is not a trend, but 136s for one finalize is long. *Inference:* the time goes into git push/rebase against a busy memory branch. Recommendation: emit per-attempt `push_ms` in the finalize telemetry.
- **Recommendation:** make sure plan, implement and review phases emit `AI_MEMORY_TELEMETRY op=retrieve`. None of the 13 implement or 13 plan telemetry-bearing runs show one, and the collector cannot tell "not emitted" apart from "truncated away".

## GH API Call Audit

- **Rate-limit pressure is low.** `GH_PAT_BUDGET` showed remaining=4998 (sweep 39703204534), 4962 (review 39700843909) and 4957 (heal intake 39703411368), all in reset window 1792634482. No HTTP 429 or secondary-limit events appear in any `log_summary`.
- **Known permanent 404s:**
  - `promote_candidate_follow` probes `git/ref/tags/promote-cycle-frozen` on every run and gets an expected 404, classified `outcome=permanent` with no retry (39702611013, 39699408393, 39698340276). That is 27 runs × 1 call, about 1s each. This is the correct handling; no change.
  - `check_failure_triage_historical` makes about 4 `gh api` calls per push (PR, run, job-log probe, paginated dedup) only to skip with `already_filed` (39699408694, 39698340253). That is 15 runs × about 4 calls = **about 60 calls/day of pure overhead**. Removing it (Speed item 2) eliminates them.
- **Per-item patterns:** none were observed in the visible logs. `lint_pr_body_auto_close` makes one paginated commits call per PR event (39703378176, 39700843840), which is appropriate.
- **Hotspots that cannot be measured yet:** orchestrate_poll (69 runs, p50 617s) and review_autofix (80 runs) are the most likely API-heavy jobs, but truncation and `used_in_job=unknown` hide their counts. Fix the budget delta (Reliability item 2) before claiming any batching savings. This is consistent with §14 (check the existing cache first, `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql`).
- **Watcher polling:** `dispatch_and_watch_workflow_run.sh` polls `actions/runs/{id}` every 15s. A 2400s watch is about 160 calls per child. That is acceptable at the current budget, and the timeout bump adds about 60 calls per gate run.

## Prompt Cache & Memory System

| Run | Family | calls | cache_read | cache_write | uncached prompt | hit rate |
|---|---|---|---|---|---|---|
| 39616808010 | review_autofix | 6 | 8,244,569 | 0 | 1,211,176 | 0.872 |
| 39607014195 | review_autofix | 10 | 5,360,959 | 592,449 | 1,439,417 | 0.725 |
| 39700843909 | review_autofix | 20 | 5,138,388 | 770,060 | 1,997,372 | 0.650 |
| 39694107256 | test_and_mark_stable | 15 | 0 | 126,154 | 128,599 | 0.000 |

- **Hit rate falls as the call count rises.** At 6 calls it is 0.87; at 20 calls it is 0.65. *Inference:* calls with more fan-out create new prefixes (per-reviewer or per-iteration variance) instead of reusing one. This is the main cache-fragmentation signal.
- **The 0% gate run writes cache it never reads.** Run 39694107256 paid for 126k cache writes and read nothing back. Either the calls don't share a prefix, or the cache expires between phases that are minutes apart. Make the calls share a prefix, or stop writing cache for one-shot calls.
- **Context-size pressure is zero:** `context_budget_warn_count=0` in every family. Prompt growth is not eroding cache value through context limits.
- **Claude CLI npm cache** hit on its primary key in 39703411368 and 39700843909 and is working as intended.
- **Memory retrieval effectiveness cannot be assessed** because no `retrieve` telemetry was found (see AI Memory Health).

## Orchestrator Health

- **The poller's cadence is shorter than its runtime.** 39701211299 ran from 00:41:44 to 00:50:12 (516s). 39701868201 was created at 00:48:06 and **cancelled before its first step** after 104s. 39702035500 was created at 00:49:48 and only started at about 00:50:16, after the previous run finished. Poll p50 is 617s and p95 976s, with outliers of 1213s (39696877569) and 1072s (39696037765, 39614653867). There were 2 cancellations in 69 runs.
  - *Inference:* concurrency-group supersession is working as a safety valve, but each overlap wastes a queued slot and delays the next poll by up to one full poll length.
  - **Indicators to track:** cancelled_before_first_step per day, and the gap between scheduled and actual start.
  - **Mitigation:** start with the phase-timing line (Reliability item 3) to find the dominant poll phase. Do not change the cadence until that data exists.
- **Clarify, plan and implement fan-out:** most runs are skipped no-ops (clarify 89/94, plan 85/88, implement 77/86, orchestrate_clarify_respond 86/87). Each label or comment event spawns about 4–8 runs within about 1s (for example 00:46:51–52: 39701742582, 39701741810, 39701741762, 39701741878, 39701742533, 39701742555, …). Most take 1s, but some reach 6–10s (39701725791 8s, 39703382023 9s). This is low cost but adds noise to Actions history and to the analysis sample.
- **Release chain:** the test_and_mark_stable failure leads to `AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=2` (39703352526), and then a workflow_failure_heal_intake run (39703411368, `claude-opus-5-5 effort=high`). The heal will spend high-effort tokens on what is probably a timeout misconfiguration. Fixing Reliability item 1 removes that cascade.
- **No `ai:orchestrator-tracking` clarification loops, wave stalls or conflict-heal retries are visible** in the sampled summaries. This is a data gap rather than proof that they are absent, given the poll log truncation.

## Pipeline Flow Bottlenecks

| Stage | Dominant cost type | Evidence | Fix (by end-to-end impact) |
|---|---|---|---|
| Release gate (test_and_mark_stable) | Retry/false-fail | 39694107256, 5422s, failed; child succeeded in 2434s | Watcher budget + timeout diagnostics (Reliability item 1) |
| Review/autofix | Compute + LLM | p50 675s, p95 2574s; 39606614099 4850s; `codex-agent` about 14 min of 1651s in 39700843909 | Cache-prefix stability; measure review rounds per PR |
| CI | Compute + queue | p50 941s, p95 2244s; `tests-release-and-log-analysis` about 10.5 min; 1.5–2.7 min queue | `--durations` diagnostics, then shard; remove no-op push workflows |
| Implement | Compute | 39695459947 1603s, 39695710740 1500s, 39696343270 1129s | No step data; add phase timing |
| Orchestrate poll | Compute + overlap queue | p50 617s; supersession cancel 39701868201 | Phase-timing line first |
| Issue-PR status | Memory push | 136s of 187s in finalize (39703075708) | Per-attempt push telemetry |
| Plan | Compute | 39695124406 930s (outlier; p95 147s) | Monitor |

Overhead split for the window: **queueing** is about 30–165s per job during bursts. **Compute** is dominated by review_autofix and CI. **Retry** comes from the 1 release-gate false failure. **Merge/conflict** overhead was not observed.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - review_autofix: avg 890s, p95 2574s.
  - CI: avg 1200s, p95 2244s.
  - orchestrate_poll: p50 617s, plus runtime overlapping the poll cadence.
  - ubuntu-latest runner queueing during push bursts.
- **Top failure modes:**
  - A release-gate watcher timeout shorter than the child workflow's budget (39694107256).
  - A diagnostics blind spot when the watcher times out.
  - Telemetry lost to log truncation.
  - `used_in_job=unknown`.
- **Highest-cost drivers:** review_autofix holds 24.92M of 25.05M tokens (99.5%), with weak cache reuse in high-fan-out runs (0.65). Semble bootstrap time is 490s total, 44% of it unused.
- **Top 3 actions:**
  1. Raise the validation-refresh watch budget to 3300s (step 57m, job 125m), and add the `DISPATCH_WATCH_TIMEOUT` line plus the `run_id` output on timeout.
  2. Stabilise review_autofix prompt prefixes, aiming for a hit rate of at least 0.85 on runs with 20 calls.
  3. Run the removal preflights for `check-failure-triage-historical.yml` and `pr6576-shard-followup.yml`, and add the end-of-job phase-timing and `GH_PAT_BUDGET_UNKNOWN reason=` diagnostics.

## Metrics Appendix

**Coverage note:** the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not present in this sandbox. Everything above comes from `analysis_context.json` (run rows, `log_summary`) and from reading the repository source. `insufficient_data=false`. The success sample rate is 0.07, and 115 of 1000 runs carry log telemetry.

| Metric | Value |
|---|---|
| Total runs | 1000 |
| Success / failure / cancelled / other (skipped etc.) | 477 / 1 / 19 / 503 |
| Failure rate | 0.1% |
| p50 / p95 duration | 12s / 1070s |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 4,613,400 |
| OR calls (usage available / unavailable) | 51 (51 / 0) |
| OR prompt / completion / total tokens | 4,776,564 / 172,110 / 25,054,850 |
| OR cache read / write | 18,743,916 / 1,488,663 |
| `cache_hit_rate` | 0.749 |
| Codex calls / tokens | 0 / 0 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

| Family | runs | success | cancelled | other | p50 s | p95 s | tokens | cache_hit_rate |
|---|---|---|---|---|---|---|---|---|
| review_autofix | 80 | 76 | 0 | 4 | 675 | 2574 | 24,921,955 | 0.757 |
| ci | 47 | 47 | 0 | 0 | 941 | 2244 | 0 | n/a |
| orchestrate_poll | 69 | 67 | 2 | 0 | 617 | 976 | 0 | n/a |
| workflow_failure_heal_intake | 131 | 65 | 15 | 51 | 97 | 436 | 0 | n/a |
| implement | 86 | 7 | 2 | 77 | 1 | 934 | 0 | n/a |
| clarify | 94 | 5 | 0 | 89 | 1 | 364 | 0 | n/a |
| plan | 88 | 3 | 0 | 85 | 1 | 147 | 0 | n/a |
| orchestrate_clarify_respond | 87 | 1 | 0 | 86 | 1 | 95 | 0 | n/a |
| test_and_mark_stable | 1 | 0 | 0 | 0 (1 failure) | 5422 | 5422 | 132,895 | 0.000 |
| validation_refresh | 1 | 1 | 0 | 0 | 2434 | 2434 | 0 | n/a |
| workflow_log_analysis | 1 | 1 | 0 | 0 | 2917 | 2917 | 0 | n/a |

**GH API summary**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` remaining (sampled) | 4998 / 4962 / 4957 |
| `used_in_job` | unknown in all samples |
| HTTP 429 / secondary limit | 0 observed |
| `GH_API_RETRY outcome=permanent` (expected 404) | 3 observed (promote-cycle-frozen) |

**Semble**

| Family | query_calls | logged_bytes | fallbacks (contract_test / runtime) | bootstraps (unused) | bootstrap_ms | sources |
|---|---|---|---|---|---|---|
| review_autofix | 4 | 65,584 | 0 (0/0) | 4 (0) | 179,484 | 25 |
| orchestrate_poll | 2 | 11,110 | 0 (0/0) | 2 (0) | 92,878 | 6 |
| workflow_log_analysis | 0 | 0 | 0 | 5 (5) | 217,721 | 0 |
| ci | 0 | 0 | 32 (32/0) | 0 | 0 | 0 |
| test_and_mark_stable | 0 | 0 | 8 (8/0) | 0 | 0 | 0 |
| **Total** | 6 | 76,694 | 40 (40/0) | 11 (5) | 490,083 | 31 |

`static_dup_bytes_total=0`, `echo_lines_dropped=0`, `bootstraps_failed=0`.

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | fallbacks |
|---|---|---|---|---|---|---|
| serena (all families) | 0 | 0 | 0 | 0 | 0 | 0 |

No other MCP servers were observed.

**Data gaps and next collection steps**

- Mount or attach `errors/39694107256` so the watcher's `::error::` line can confirm the timeout hypothesis.
- Fix `used_in_job=unknown`.
- Add end-of-job phase-timing lines so truncated poll and review logs can be attributed.
- Verify that `AI_MEMORY_TELEMETRY op=retrieve` is emitted.
- Raise review_autofix token-telemetry coverage, currently 8 of 80 runs.
## Deep Audit — Workflows & Scripts (2026-10-22)

**Scope and method.** I read in depth: `scripts/dispatch_and_watch_workflow_run.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/gh_helpers.sh` (lines 1–400 and 640–1840), `scripts/label_helpers.sh`, `scripts/auto_release_stable.sh`, `scripts/promote_candidate_follow.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh` (lines 1–260), and selected regions of `scripts/orchestrate_poll_process.sh`. On the workflow side I read `.github/workflows/cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `check-failure-triage-historical.yml`, `pr6576-shard-followup.yml`, `auto-release-stable.yml`, `promote-candidate-follow.yml`, `sync_ai_labels.yml`, `review_autofix_sweep.yml` (lines 75–475), the release-gate phases of `test-and-mark-stable.yml`, and the composite action `.github/actions/run-soft-error-analyzer/action.yml`. Other files got targeted pattern sweeps only. I could not measure byte or character counts in this sandbox, so every size figure below is an estimate from line counts and is marked as such.

**Cross-references to the existing report.** These are not repeated here:
- the watcher's timeout budget and missing timeout outputs ("Reliability Improvements" item 1 / "Speed Optimizations" item 1);
- `GH_PAT_BUDGET used_in_job=unknown` (Reliability item 2);
- removal of the two single-use push workflows (Speed item 2).

---

### Section 1: Bug & Correctness Sweep

**BUG-001** · `scripts/comprehensive_test_and_release_gh_api.sh:151-156`, `.github/workflows/test-and-mark-stable.yml:1409-1424` · Severity: **High** · Category: `bug`
- **Description:**
  - When a call fails with stderr output, `gh_api_safe` prints `echo "::error::gh api call failed: $*"` to **stdout** (line 153 has no `>&2`). The file's own comment at `test-and-mark-stable.yml:1001-1002` confirms this behaviour.
  - Phase 3b captures `PR_NUMBER=$(gh_api_safe_print "repos/${TEST_REPO}/pulls?state=open&head=...ai/issue-${ISSUE_NUMBER}&per_page=1" --jq '.[0].number // empty' || echo "")`. That is the non-quiet variant, so a non-rate-limit failure (401, non-limit 403, 422, or a transient error after 4 attempts) fills `PR_NUMBER` with the error text.
  - The check `if [ -n "$PR_NUMBER" ]` (line 1418) is then true. The step logs "✓ Implementation PR #::error::… created" and writes the garbage to `pr_number=` in `GITHUB_OUTPUT` (line 1423). Later phases consume `steps.wait-implement.outputs.pr_number` (for example line 4553).
  - Result: the release gate reports a phase as passed on an API failure, and the eventual failure shows up somewhere unrelated.
- **Recommended fix:**
  - At line 153, send the error line to stderr: `echo "::error::gh api call failed: ..." >&2`. The page shape check in `find_latest_scoped_run_field` (lines 237-239) still works without the stdout line.
  - At line 1411, validate the result before trusting it: `[[ "${PR_NUMBER}" =~ ^[1-9][0-9]*$ ]] || PR_NUMBER=""`.

**BUG-002** · `scripts/label_helpers.sh:222-256` (`set_issue_phase_label_resilient`) · Severity: **Medium** · Category: `bug`
- **Description:**
  - The function reads the issue's labels (GET, line 223), computes `(. - $p) + [$t]` locally (line 238), then replaces the whole label set with `PUT .../labels` (line 252).
  - This is a read-modify-write with no concurrency control. A label added by another job between the GET and the PUT is silently removed. Examples: `ai:engine-claude`, `ai:merge-queued` added by review_merge_train, or `ai:codex`.
  - The helper is referenced 217 times across 26 files, including `orchestrate_poll_process.sh` (66) and `review_autofix.yml` (20), so concurrent writers on the same issue are routine.
- **Recommended fix:** use additive or subtractive calls only:
  - one `DELETE repos/{repo}/issues/{n}/labels/{name}` for each phase label that is present and is not the target (the GET already shows which);
  - then `POST .../labels -f "labels[]=${target_label}"`. This keeps the existing `GH_RETRY_IDEMPOTENT=true` POST fallback shape.

  The call count goes from 1 PUT to 1 POST plus k DELETEs (k is usually 1), and concurrent label additions are never removed.

**BUG-003** · `scripts/dispatch_and_watch_workflow_run.sh:76-90, 219-258` · Severity: **Medium** · Category: `bug`
- **Description:**
  - Registration takes the newest `workflow_dispatch` run of the target workflow with `id > PRE_RUN_ID` (line 249), or created after the dispatch time when the pre-lookup failed (line 251).
  - Nothing ties the adopted run to *this* dispatch. Another release gate, a manual dispatch, or a duplicate created by the step's own retry can be adopted instead. On retries: the `dispatch_workflow` loop re-sends `gh workflow run` after a failure whose first attempt may already have created a run; the comment at `test-and-mark-stable.yml:5383-5386` acknowledges this.
  - For validation-refresh, both duplicate children receive the same `branch_name=ai/validation-refresh-smoke-${GITHUB_RUN_ID}` (`test-and-mark-stable.yml:5376, 5397`), so they race on one branch. *Inference* [NEEDS VERIFICATION].
- **Recommended fix:** pass a per-dispatch correlation field (for example `--field "dispatch_nonce=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-<step>"`), put it in each child's `run-name`, and match it on `display_title` in `REGISTRATION_JQ`. `internal-review.yml` already uses this pattern (`[pr:<N>]` run-name matched at `review_autofix_sweep.yml:383`). Until then, also filter on `.actor.login` and `.created_at >= REGISTRATION_WINDOW_FALLBACK_UTC` in both branches of the jq.

**BUG-004** · `scripts/promote_candidate_follow.sh:168-179, 231-243`; `.github/workflows/promote-candidate-follow.yml:23-25` · Severity: **Medium** · Category: `bug`
- **Description:**
  - The follower reads the `promote-cycle-frozen` tag (line 168) and later fast-forwards `promote-candidate` (line 237). It runs in concurrency group `promote-candidate-follow-*`, while `promote_main_cycle.sh` runs in group `promote-main-to-stable` (`promote-main-to-stable.yml:159-160`).
  - The cycle creates the tag and then PATCHes the branch (`promote_main_cycle.sh:573-590`). It handles a follower that moved *before* its PATCH (lines 593-595), but not one that PATCHes *after* the freeze.
  - In that window the follower can advance the frozen candidate past the commit the cycle recorded in `pin_candidate_sha`. This is a TOCTOU race; the window is seconds. [NEEDS VERIFICATION: impact on the proving run]
- **Recommended fix:** in step 4, re-read `git/ref/tags/promote-cycle-frozen` immediately before the PATCH, and skip with `outcome=frozen` if it now exists. This adds one read, only on the advance path. Update the "GitHub API calls per run" header at lines 22-25 to match.

**BUG-005** · `.github/workflows/internal-cancel-on-pr-close.yml:19-54` · Severity: **Low** · Category: `bug`
- **Description:**
  - The `heal-pr-reconcile` job has no `timeout-minutes`, so it gets the 360-minute default.
  - It checks out `main` with `fetch-depth: 0`, which fetches all branches and tags. The reconcile script fetches every ref it needs itself with `git fetch --quiet --no-tags` (`workflow_failure_heal_pr_reconcile.sh:159-168`) and reads no tags.
  - This confirms the report's Speed item 3 *inference* from the code side (run 39703075676: 146 s, tag fetch in the log tail).
- **Recommended fix:** add `timeout-minutes: 15` and switch to `fetch-depth: 1`. The script's explicit fetches bring in the needed history. [NEEDS VERIFICATION: confirm `git merge` in `_retarget_heal` resolves a merge base on a shallow base clone; if not, use `filter: blob:none` with `fetch-depth: 0` instead.]

**BUG-006** · `.github/workflows/cancel_on_pr_close.yml:155-189, 260, 304, 370-406` · Severity: **Low** · Category: `bug`
- **Description:**
  - `_cpr_fetch_active_runs` is called as a plain command under `set -euo pipefail` (lines 260 and 304). One failed paginated listing aborts the "Cancel queued/in-progress runs" step.
  - The next step, "Release merge-queued PRs", has no `if: always()`, so it is skipped. That contradicts its own fail-open design (lines 402-406, "ignoring (fail-open)"): a PR close with a transient listing failure never releases queued PRs.
- **Recommended fix:** wrap both call sites in `if ! _cpr_fetch_active_runs … > "${active_runs_file}"; then echo "::warning::…"; exit 0; fi`, and add `if: always()` to the release step.

**BUG-007** · `.github/workflows/cancel_on_pr_close.yml:14-16`; `.github/workflows/internal-cancel-on-pr-close.yml:3-7` · Severity: **Low** · Category: `bug`
- **Description:**
  - `pull_request: closed` runs and the `*/5` schedule share one group, `cancel-on-pr-close-${{ github.repository }}`, with `cancel-in-progress: false`. GitHub keeps only one pending run per group, so a pending PR-close run can be replaced by the next schedule tick.
  - The schedule path only cancels runs whose `pull_requests` array is non-empty (lines 285-287); runs without linkage are "preserved" (line 263). The head-ref matching of the event path (lines 310-313) is lost for that close.
  - [NEEDS VERIFICATION: frequency of runs without linkage, for example fork heads.]
- **Recommended fix:** give each event its own group: `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event_name }}`.

**SEC-001** · `scripts/gh_helpers.sh:1100, 1130, 1132, 1215`; `scripts/comprehensive_test_and_release_gh_api.sh:153` · Severity: **Medium** · Category: `security`
- **Description:**
  - On failure, `gh_retry` and `gh_retry_to_file` interpolate the full argv (`$*`) into `::warning::` / `::notice::` / `::error::` workflow commands, unescaped.
  - The same file escapes stderr with `_gh_actions_escape` (lines 145-164) precisely to prevent workflow-command injection, but argv is not escaped.
  - Callers pass multi-line, partly model- or PR-derived text as argv, for example `gh_retry gh api "repos/…/issues/${PR_NUMBER}/comments" -f body="${BODY}"` (`review_autofix.yml:5028, 5111, 5162, 5184`). A body line beginning with `::` (such as `::add-mask::` or `::stop-commands::`) is then processed as a workflow command, and whole comment bodies are dumped into annotations.
  - [NEEDS VERIFICATION: exploitable only on the failure path.]
- **Recommended fix:** log the endpoint, not argv, the way `gh_api_retry` does (`_gh_api_retry_log … endpoint=$(_gh_actions_escape "$8")`, line 907). For example:

```bash
_ghr_desc="$(_gh_actions_escape "$(_gh_api_endpoint "${@:3}")")"
echo "::error::gh command failed after ${max_attempts} attempts: ${1:-} ${2:-} ${_ghr_desc}" >&2
```

  Apply the same change at `comprehensive_test_and_release_gh_api.sh:153`, together with the stderr redirect from BUG-001.

Sweep notes:
- Untrusted event fields (`github.event.issue.body`, `comment.body`, `pull_request.title/body/head.ref`) are routed through `env:` at every site the regex sweep found. Examples: `implement.yml:1014`, `clarify.yml:679`, `issue_pr_status.yml:208-213`, `lint-pr-body-auto-close.yml:60-61`.
- The one body-derived value echoed into shell through `${{ }}`, `steps.baseline_refctx.outputs.branch` (`implement.yml:1964`), is constrained to `^ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+$` and verified against a live ref (`implement.yml:1166-1289`). No injection finding.

---

### Section 2: GitHub API Call Redundancy Audit

**API-001** · `scripts/orchestrate_poll_process.sh:9229-9307` (`evaluate_sync_superseded_by_main`) · Severity: **Medium** · Category: `api-redundancy`
- **Description:**
  - For each wave issue the function reads the cross-reference timeline (`_issue_cross_ref_pr_numbers_unique` → `gh_issue_timeline_with_cross_refs`). That GraphQL query already returns each referenced PR's `state` and `mergedAt` (`gh_helpers.sh:1742-1746`), mapped to `.source.issue.state` / `.merged` (`gh_helpers.sh:1814-1821`).
  - The loop then re-fetches every PR with `_fetch_pr_json` (REST `pulls/N`, line 9280) only to read `.state` / `.merged_at`.
  - It also makes one paginated `pulls/N/files` call per PR (line 9293), with no cache. Compare `_MT_FILES_CACHE` in `review_merge_train.sh:214-222`.
- **Current calls:** I timeline + P `pulls/N` + P × ⌈files/100⌉.
- **Proposed calls:** I timeline + 0 `pulls/N`. Read state from the timeline JSON and fall back to `_fetch_pr_json` only when `lookup_failed == true` or when the REST timeline path was used. Files go through ⌈P/25⌉ aliased GraphQL calls (`pr_<n>:pullRequest(number:<n>){files(first:100){pageInfo{hasNextPage} nodes{path}}}`), with REST fallback when `hasNextPage` is true.
- **Pattern to extend:** `_fetch_issue_labels_batch_graphql` (orchestrate_poll_process.sh:4349; batch size 25) / `_fetch_linked_pr_status_graphql` (line 18498).
- Example: 10 PRs → from 20 or more calls to 1–2.

**API-002** · `.github/workflows/orchestrate_poll.yml:239-266` and `.github/workflows/cancel_on_pr_close.yml:370-406` (schedule path, `internal-cancel-on-pr-close.yml:6-7`) · Severity: **Medium** · Category: `api-redundancy`
- **Description:**
  - The global `review_merge_train.sh release` scan runs on every poll tick (`*/5`, `internal-orchestrate-poll.yml:5`; "Runs on EVERY tick", line 240) **and** on every scheduled cancel-cleanup tick (`*/5`; "Scheduled cleanup is running the global merge-train release scan", cancel_on_pr_close.yml:389).
  - Each scan costs at least 1 open-PR listing, plus about 10 active-run status reads when a queued PR passes the base filter (`review_merge_train.sh:83-90`).
- **Current calls:** about 576 scans/day, ≥576 calls/day baseline, up to about 6,300 when queued PRs exist.
- **Proposed calls:** 288 scans/day. In the schedule path, skip the release when the repository runs the poll wrapper. The comment at cancel_on_pr_close.yml:373 says the schedule path exists because "core-profile repos have no poll backstop". [NEEDS VERIFICATION: choose the detection signal, for example a repo variable with a default per §8, and keep the `pull_request: closed` release path unchanged.]
- **Pattern:** no batching; remove the duplicate.

**API-003** · `scripts/label_helpers.sh:161-195` (`ensure_label_exists`) · Severity: **Medium** · Category: `api-redundancy`
- **Description:**
  - `gh label create` on an existing label fails with a message like `label with name "…" already exists; use '--force' …`. That text matches neither `_is_gh_permanent_failure` (gh_helpers.sh:192-195) nor the create list in `_gh_cmd_is_unsafe_post` (line 813).
  - `gh_retry` therefore treats it as transient and retries 5 times with 1 + 2 + 4 + 8 s of sleep (lines 1116-1123). Only afterwards does `ensure_label_exists` grep for `already exists` and return 0.
  - `set_issue_phase_label_resilient` calls it on every phase change (line 220), across 217 call references. [NEEDS VERIFICATION: exact gh CLI message text in the runner's gh version.]
- **Current calls:** 5 calls and about 15 s per ensure on an existing label.
- **Proposed calls:** 1 call. Create through REST instead: `gh_retry gh api -X POST "repos/${repo}/labels" -f name=… -f color=… -f description=…`. Its `HTTP 422` is classified as permanent, giving a single attempt; treat a 422 containing `already_exists` as success. Alternatively, 0 calls after the first through a per-process cache (`declare -A _AI_LABEL_ENSURED`).
- **Pattern:** `_is_gh_permanent_failure` classification already in `gh_helpers.sh`.

**API-004** · `scripts/gh_helpers.sh:1271-1335` (`gh_api_json_to_file`), `1350-1403` (`curl_gh_api`) · Severity: **Low** · Category: `api-redundancy`
- **Description:** neither helper checks `_is_gh_permanent_failure` or HTTP 4xx before retrying. `curl_gh_api` retries every status that is not 2xx and not a rate limit (lines 1389-1395). `gh_api_json_to_file` retries every failure that is not a rate limit (lines 1313-1322). A 404/401/422 therefore costs 5 calls and about 15 s. `gh_retry` (lines 1099-1106) and `gh_api_retry` (`_gh_api_classify`, lines 831-881) both stop at once.
- **Current calls:** 5 per permanent error. **Proposed calls:** 1.
- **Fix:** add the same `_is_gh_permanent_failure` early return. For `curl_gh_api`, also return immediately on `http_code` 400/401/404/410/422.

**API-005** · `.github/workflows/check-failure-triage-historical.yml:75-82` · Severity: **Low** · Category: `api-redundancy`
- **Description:**
  - On every push to main, the guard lists **all** `ai:check-triage` issues (`state=all`, paginated) to find one marker. The cost grows without bound with the triage issue count.
  - Correction to the report's estimate ("~4 `gh api` calls per push", GH API Call Audit): the code shows the `already_filed` path exits right after this single paginated listing (lines 79-82). The cost is therefore ⌈N/100⌉ calls; it never reaches the PR, run or job-log probes.
- **Current calls:** ⌈N/100⌉ per push. **Proposed calls:** 0, by removing the workflow once the registry preflights pass (`docs/scripts-pending-removal.md:212-220`). Interim alternative: a single `search/issues` query for the fingerprint marker.

**API-006** · `.github/actions/run-soft-error-analyzer/action.yml:82-111` · Severity: **Low** · Category: `api-redundancy`
- **Description:**
  - The action polls `actions/runs/{id}` every 5 s with raw `gh api … --jq … 2>/dev/null || echo ""` (lines 92, 105). There is no retry or backoff, and on error the response body goes to stdout (the bug `_safe_gh_jq` documents at gh_helpers.sh:1225-1240).
  - It does this even when the caller has just confirmed completion: the watcher writes `workflow_conclusion` only after `status=completed` (`dispatch_and_watch_workflow_run.sh:317-319`).
- **Current calls:** at least 1, up to 24 per phase (`WAIT_SECS=120`/5).
- **Proposed calls:** 0 when the caller passes a known-completed status. Add an optional input with a default, for example `known_completed: "false"`, and set it from `steps.<id>.outputs.workflow_conclusion != ''`. Otherwise reuse `gh_api_safe_quiet_print`.

**API-007** · `scripts/workflow_failure_heal_pr_reconcile.sh:239-247` · Severity: **Low** · Category: `api-batching`
- **Description:** inside the loop over matching heal issues, the script makes one REST `pulls?head=<owner>:ai/issue-<n>` call per issue. This per-item loop call is documented in the script's API budget (lines 29-31).
- **Current calls:** H (heal issues for the source PR; usually 1).
- **Proposed calls:** 1 aliased GraphQL call (`i_<n>:ref(qualifiedName:"refs/heads/ai/issue-<n>"){associatedPullRequests(states:OPEN,first:10){nodes{number headRefName headRefOid baseRefName}}}`) when H > 1. Keep REST for H = 1. [NEEDS VERIFICATION: worthwhile only if H > 1 occurs.]
- **Pattern:** `_cpr_fetch_pr_state_batches` (cancel_on_pr_close.yml:191-256).

Retry and backoff check: `gh_api_safe` (`comprehensive_test_and_release_gh_api.sh:97-160`) and `gh_api_retry` both back off exponentially and limit retries of writes. I found no tight retry loop on a non-transient error beyond API-003 and API-004.

---

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** · Support-ref resolution block, 16 copies in 9 reusable workflows: `cancel_on_pr_close.yml:30-67`, `implement.yml:446-447, 751-752, 1330-1331, 6997-6998`, `clarify.yml` (2), `orchestrate.yml` (2), `orchestrate_poll.yml` (2), `plan.yml` (2), `check_failure_triage.yml`, `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml` · Severity: **Medium** · Category: `duplication`
- **Description:**
  - Each copy re-implements `promote_candidate_support_ref="${{ github.sha }}"`, the `@refs/heads/promote-candidate` regex, the protected-branch read, and the consumer `@<sha>` pin logic.
  - It cannot simply be moved into `scripts/`: it decides which ref to check out the scripts *from*, a bootstrap problem.
  - Drift between copies changes which code runs with `GH_PAT`.
- **Recommended fix:** keep it inline, but generate it from one canonical template. Add a parity test like `tests/test_skip_ai_marker_rule.py`, which already enforces a byte-identical awk copy, asserting every copy matches the template with only the workflow file name substituted.

**DUP-002** · `.github/workflows/cancel_on_pr_close.yml:375-386` and `.github/workflows/orchestrate_poll.yml:246-256` · Severity: **Low** · Category: `duplication`
- **Description:** nine identical `MERGE_TRAIN_*` env lines and the same 10-line release wrapper body (`_mt_rc` / fail-open warning) appear in both files.
- **Recommended fix:** move the defaults into `scripts/review_merge_train.sh`. §8 already requires defaults in the helper, and the env lines would then carry only the `vars.*` values. Reduce both steps to `bash scripts/review_merge_train.sh release-fail-open` (a new subcommand alias; keep `release` unchanged).

**DUP-003** · `.github/workflows/review_autofix_sweep.yml:262` and `scripts/review_autofix_step_evaluate_gate.sh:28` · Severity: **Low** · Category: `duplication`
- **Description:** the same 400-character `SKIP_AI_BODY_AWK` program is defined twice, kept in sync only by a test.
- **Recommended fix:** the sweep already checks out trusted default-branch helpers at `.ci-rerun-support` (lines 144-149). Source the definition from there by extracting it to `scripts/review_skip_ai_marker.sh`, which defines `SKIP_AI_BODY_AWK`, and update both callers.

**DUP-004** · `.github/workflows/check-failure-triage-historical.yml:67-70` and `scripts/check_failure_triage.sh:453` · Severity: **Low** · Category: `duplication`
- **Description:** the historical fingerprint formula is copied inline ("Same formula as scripts/check_failure_triage.sh").
- **Recommended fix:** resolved by removing the single-use workflow (API-005). No shared module is warranted.

**DUP-005** · Fallback stubs `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }` and `gh_api_retry()` (108 occurrences in 49 files; for example `cancel_on_pr_close.yml:44, 148`, `review_autofix_sweep.yml:220`, `auto_release_stable.sh:57`, `workflow_failure_heal_pr_reconcile.sh:55`) · Severity: **Medium** · Category: `duplication`
- **Description:** when `gh_helpers.sh` is not staged, each stub silently drops several guarantees: retries, single-attempt POST creates (the #6634 rule), stdout buffering (#5495), and permanent-error classification. There is no log signal that this happened.
- **Recommended fix:** add one helper, `gh_helpers_fallback_stub <caller>`, to `scripts/emit_event.sh` (it is already sourced by gh_helpers.sh). It defines the stubs and prints one `GH_HELPERS_FALLBACK caller=<name>` line (register the prefix through an `agents.d/` fragment per §25). Replace the 108 inline stubs as files are touched.

**DUP-006** · Active-run listings: `cancel_on_pr_close.yml:155-189` (`_cpr_fetch_active_runs`: queued + in_progress, offset pagination), `review_autofix_sweep.yml:300-435` (`snapshot_active_review_runs`: queued/in_progress/pending), `scripts/review_merge_train.sh:1057-1100` (five statuses, keyset by `created_at`) · Severity: **Medium** · Category: `duplication`
- **Description:** three implementations of "list non-terminal workflow runs" use different status sets and pagination strategies. `review_merge_train.sh:1088-1095` documents that offset pagination skips runs under churn, yet `cancel_on_pr_close.yml` still uses offset `--paginate`.
- **Recommended fix:** add `gh_list_active_runs <repo> <workflow_file|-> <statuses_csv> <out_file>` to `gh_helpers.sh`, implemented with the keyset approach and the incomplete-snapshot return code from review_merge_train.sh. Migrate the three callers.

**DUP-007** · Retry wrappers with divergent classification: `gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file`, `curl_gh_api`, `gh_api_retry` (`gh_helpers.sh:910-1403`) and `gh_api_safe` (`comprehensive_test_and_release_gh_api.sh:97-160`) · Severity: **Low** · Category: `duplication`
- **Description:** six retry loops, three rate-limit wait strategies, and two transient classifiers. API-004 and SEC-001 are symptoms of this drift.
- **Recommended fix:** make `_gh_api_classify` (lines 831-881) the single classifier. Have `gh_retry_to_file`, `gh_api_json_to_file` and `curl_gh_api` call it. Keep `gh_api_safe` self-contained, as its header (lines 93-96) requires, but copy the 4xx short-circuit into it.

**DUP-008** · `.github/workflows/implement.yml:5355, 6512, 6608, 6661, 6728, 6869` · Severity: **Low** · Category: `duplication`
- **Description:** the same six-term guard (`destructive_commit_blocked` / `scope_violation_blocked` / `staged_support_rebase_conflict` across two steps) is repeated in several `if:` expressions, some negated.
- **Recommended fix:** emit one `guard_blocked=true|false` output from a small step after `commit_changes`, and test `steps.<id>.outputs.guard_blocked` in each `if:`.

---

### Section 4: Expression Size Limit Risk Assessment

Method: `tests/test_workflow_file_size_limit.py:66-98` fails CI for any `jobs.*.steps[*].run` body that contains `${{` and is 18,000 characters or longer. Every block listed below is therefore below 18,000 if CI is green. The figures are line-count estimates, at about 55–65 characters per de-indented line [NEEDS VERIFICATION, measure with `len(step["run"])`].

**EXPR-001** · `.github/workflows/test-and-mark-stable.yml:962-1229` ("Phase 2: Wait for plan to complete") · Severity: **Medium** · Category: `expression-limit`
- **Description:** a 267-line body with inline `${{ steps.create-issue.outputs.created_after }}` at lines 980, 992 and 1010. It also holds the long jq filters and in-line comments that grow with each incident fix. Estimate: about 15,000–17,500 characters, leaving about 3,500–6,000 of headroom against 21,000.
- **Recommended fix:** add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step's `env:` and use `${CREATED_AFTER}` in the three places. With no `${{` left, the body stops being an expression at all. Later, move the body to `scripts/release_gate_wait_plan.sh`, following the §24 `review_autofix_step_<slug>.sh` pattern.

**EXPR-002** · `.github/workflows/test-and-mark-stable.yml:1261-1551` ("Phase 3b: Wait for PR creation") · Severity: **Medium** · Category: `expression-limit`
- **Description:** about 290 lines, made an expression by `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` (line 1307). Estimate: about 15,000–17,500 characters, headroom about 3,500–6,000.
- **Recommended fix:** move it to `env: IMPL_CREATED_AFTER: ${{ … }}`. This is also where BUG-001's validation goes.

**EXPR-003** · `.github/workflows/test-and-mark-stable.yml:800-939` ("Phase 1: Wait for clarify") · Severity: **Low** · Category: `expression-limit`
- **Description:** about 135 lines with an inline `created_after` at line 848. Estimate: about 8,000–9,500 characters, headroom about 11,500.
- **Recommended fix:** the same `env:` move as EXPR-001.

**EXPR-004** · `.github/workflows/test-and-mark-stable.yml:4554-4710` ("Verify all phases passed") · Severity: **Low** · Category: `expression-limit`
- **Description:** about 156 lines with 15 inline `${{ steps.*.outputs.* }}` (lines 4564-4690), even though the step already routes `ISSUE_NUMBER` and `PR_NUMBER` through `env:` (lines 4551-4553). Estimate: about 8,500–9,500 characters.
- **Recommended fix:** move all 15 values into `env:`. The body then has no expression cost.

**EXPR-005** · `tests/test_workflow_file_size_limit.py:66-98` · Severity: **Low** · Category: `expression-limit`
- **Description:** the guard covers only workflow `jobs.*.steps[*].run`. It does not cover:
  - composite actions (`.github/actions/*/action.yml` `runs.steps[*].run`; for example `run-soft-error-analyzer` has 15 `${{` uses and `setup-runtime` 10);
  - multi-line `with:` / `env:` values such as `phase_runs: |` blocks.

  All of these are small today (the largest action is 166 lines), so there is no current risk.
- **Recommended fix:** extend the test to walk `.github/actions/**/action.yml` and to measure string `with:` / `env:` values that contain `${{`, using the same 18,000 guard.

**EXPR-006** · Workflow file sizes: `test-and-mark-stable.yml` (7,714 lines), `implement.yml` (7,061), `review_autofix.yml` (5,749), `plan.yml` (3,224) · Severity: **Low** · Category: `expression-limit`
- **Description:** I could not measure bytes. At 55–65 bytes per line, `test-and-mark-stable.yml` would be about 420–500 KB, possibly near the repo's 480,000-byte guard (§24; the hard limit is 512,000, below the 1 MB / 800 KB figure in the prompt). *Inference* [NEEDS VERIFICATION with `wc -c`]. No file can be over 800 KB unless lines average more than 104 bytes.
- **Recommended fix:** extract the two largest bodies of `test-and-mark-stable.yml` before the next growth: "Phase 4: Wait for review & autofix" (lines 1821-2552, 731 lines) and "Phase 4b" (lines 2622-3292, 670 lines). Move them into `scripts/` per §24, keeping `name:`, `id:`, `if:` and `env:` in the workflow.

`if:` conditions: the longest I found are about 500–650 characters (`implement.yml:5355`, `review_autofix.yml:3673`, `issue_pr_status.yml:1059`), far below 21,000. I found no inline prompt heredocs in the blocks I sampled.

---

### Section 5: Cross-Cutting Concerns

**DEAD-001** · `.github/workflows/test-and-mark-stable.yml:887-889, 1082-1084, 1413-1416, 2166-2169, 3732` (initialised at 815, 969, 1283, 1903, 3582) · Severity: **Low** · Category: `dead-code`
- **Description:**
  - `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$X" ]; then … continue; fi` can never be true. `gh_api_safe` updates `RATE_LIMIT_BACKOFF` inside `$(gh_api_safe_print …)`, a subshell, so the parent's value stays 0.
  - If one of these were made reachable, the copies at lines 887-889 and 1082-1084, and the copy at lines 2166-2169 (which only increments `ELAPSED`), would `continue` without sleeping and spin the loop.
- **Recommended fix:** delete the five guards. `gh_api_safe` already backs off internally (`comprehensive_test_and_release_gh_api.sh:121-135`).

**DEBT-001** · `.github/workflows/pr6576-shard-followup.yml:40-68` · Severity: **Low** · Category: `tech-debt`
- **Description:** on every push to main, the `self-test` job (pip install plus pytest) runs *before* `locate` checks the one-time marker (`locate` needs `self-test`). The marker-present no-op therefore still costs a full runner job.
- **Recommended fix:** this complements the report's Speed item 2. Run the registry preflights at `docs/scripts-pending-removal.md:320-328` and remove the workflow. If it has to stay, restrict `self-test` to `pull_request` events.

**DEBT-002** · `scripts/orchestrate_poll_process.sh:1-21` · Severity: **Medium** · Category: `tech-debt`
- **Description:**
  - The poller is 30,732 lines; its header says "~27,700-line poller" (line 14), which is stale.
  - ShellCheck's extended analysis is disabled file-wide (line 12) because CI could not finish on the file. That reduces static coverage of the busiest scheduled script.
- **Recommended fix:** keep extracting sourced modules, following the `scripts/orchestrate_stall_retired_conflict.sh` precedent (sourced, with fail-closed stubs). Good first candidates are the GraphQL batch helpers around lines 4349-4500 and 18339-18600, moved into an `orchestrate_poll_graphql.sh`. Update the line-count comment in the same change.

**CONSIST-001** · `scripts/orchestrate_poll_process.sh:2220-2224` vs `4349, 4433`; `9279-9291` (tab-indented lines inside a 2-space function); `scripts/orchestrate_stall_retired_conflict.sh:23-40`; `scripts/label_helpers.sh:161, 203` · Severity: **Low** · Category: `consistency`
- **Description:** brace style (`f()\n{` vs `f() {`) and indentation (tabs vs 2 spaces) are mixed, even within one function. The house style is tabs with the opening brace on a new line (§11).
- **Recommended fix:** do not reformat whole files (§9). Normalise only the functions a change touches; start with lines 9279-9307 when API-001 lands.

**CONSIST-002** · `.github/workflows/test-and-mark-stable.yml:1143-1144` vs `1078-1079`; `.github/workflows/review_autofix_sweep.yml:228-237` vs `340-343`; `.github/actions/run-soft-error-analyzer/action.yml:92, 105` · Severity: **Low** · Category: `consistency`
- **Description:** the same endpoint is called through a helper in one place and through raw `gh api … 2>/dev/null || echo ""` next to it. The raw form has no retry, and on failure the error JSON lands on stdout. In the sweep, a single transient 5xx on the open-PR listing aborts the tick under `set -euo pipefail`, while the run snapshot just below it uses `gh_api_retry`.
- **Recommended fix:** use `gh_api_safe_quiet_print` (release gate) and `gh_api_retry --paginate` (sweep), with an explicit `AUTOFIX_SWEEP_SKIPPED reason=pr_list_unavailable` exit 0.

**CONSIST-003** · `.github/workflows/sync_ai_labels.yml:125` vs `170, 259`; `.github/workflows/cancel_on_pr_close.yml:33-34` · Severity: **Low** · Category: `consistency`
- **Description:** `if [ "${{ inputs.dry_run }}" = "true" ]` interpolates directly into shell, while the same input is routed through `env: DRY_RUN` two steps later. `cancel_on_pr_close.yml` does the same with `${{ github.repository }}` / `${{ github.sha }}` while also defining `REPOSITORY` in the job `env:` (line 22). None of these values is exploitable today, but the pattern undermines the env-only convention.
- **Recommended fix:** use `DRY_RUN: ${{ inputs.dry_run }}` / `${REPOSITORY}` / `${GITHUB_SHA}`.

**SHELL-001** · `scripts/review_apply_fixes.sh:2803, 2903` · Severity: **Low** · Category: `shellcheck`
- **Description:** `$(ls -1 "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*… | sort -V | tail -n 1)` parses `ls` output (SC2012).
- **Recommended fix:** use `shopt -s nullglob; _f=( "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*_changes_lost.txt ); printf '%s\n' "${_f[@]}" | sort -V | tail -n 1`.

**SHELL-002** · `scripts/orchestrate_poll_process.sh:25029, 25033` · Severity: **Low** · Category: `shellcheck`
- **Description:** `for prior_idx in $(seq …)` and `for pw_inum in ${PRIOR_NON_TERMINAL}` rely on unquoted word splitting (SC2046/SC2086). The values are numeric today, but a glob character in the state file would expand.
- **Recommended fix:** use `for (( prior_idx = 0; prior_idx < WAVE_IDX; prior_idx++ ))` and `while IFS= read -r pw_inum; do … done <<< "${PRIOR_NON_TERMINAL}"`.

TODO/FIXME/HACK/XXX markers: none in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.

---

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 14 | BUG-002, BUG-003, BUG-004, SEC-001, API-001, API-002, API-003, DUP-001, DUP-005, DUP-006, EXPR-001, EXPR-002, DEBT-002, CONSIST-002 |
| Low | 21 | BUG-005, BUG-006, BUG-007, API-004, API-005, API-006, API-007, DUP-002, DUP-003, DUP-004, DUP-007, DUP-008, EXPR-003, EXPR-004, EXPR-005, EXPR-006, DEAD-001, DEBT-001, CONSIST-001, CONSIST-003, SHELL-001, SHELL-002 |

Correction: the Low row lists 22 IDs, so the count should read 22, not 21 (37 findings in total).

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/test-and-mark-stable.yml` | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/internal-cancel-on-pr-close.yml`, `.github/actions/run-soft-error-analyzer/action.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/check-failure-triage-historical.yml` | Medium |
| Code modularization | 9 reusable workflows (DUP-001), `scripts/gh_helpers.sh`, `scripts/emit_event.sh`, `scripts/review_merge_train.sh`, `.github/workflows/review_autofix_sweep.yml`, `scripts/review_autofix_step_evaluate_gate.sh`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`, plus ~49 files carrying fallback stubs | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, new `scripts/release_gate_*.sh`, `tests/test_workflow_file_size_limit.py` | Medium |
| Medium/Low fixes | `scripts/dispatch_and_watch_workflow_run.sh`, `scripts/promote_candidate_follow.sh`, `.github/workflows/internal-cancel-on-pr-close.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/pr6576-shard-followup.yml`, `scripts/review_apply_fixes.sh`, `scripts/orchestrate_poll_process.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-22)

### Safety Tag Legend
`SAFE_TO_MERGE` means the merged calls hit the same endpoint with identical parameters, in the same function or step, under an identical guard and with no mutation between them. They also share retry and error handling, and the merge crosses no concurrency boundary, so the implement stage may apply the one-line directive as written. `NEEDS_VERIFICATION` means the merge looks correct but at least one of those conditions cannot be proven by reading the code. The listed verification steps must pass before anyone implements it. `RISKY_SKIP` findings are listed so they stay visible. They touch a retry, polling or race-defence path, pagination, a fail-closed safety read, `orchestrate_poll_process.sh`, or log keys that other tooling counts. They must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001** · `NEEDS_VERIFICATION`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:891-892` and `:917-918`, in the step "Phase 1: Wait for clarify to complete" (`id: wait-clarify`, lines 800-937). The consolidated pattern already exists in Phase 2 at `:1086-1089`.
- **Current calls:** 3 per poll iteration (issue, comments, comments), every `POLL_INTERVAL: 10` seconds (`:584`). **Proposed calls:** 2 per iteration.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{n}/comments`, called twice in the same iteration. The second call is `GET /repos/{owner}/{repo}/issues/{n}`, which stays.
- **Evidence:** both calls run in the same loop iteration, and the step writes nothing in between.
  ```bash
  COMMENT_COUNT=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments" \
    --jq 'length' || echo "0")
  ...
  LAST_BOT_COMMENT=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments?per_page=5&direction=desc" \
    --jq '[.[] | select(.user.login == "github-actions[bot]")] | .[0].body // ""' || echo "")
  ```
  Phase 2 already reads comments once and derives several fields from that one response:
  ```bash
  COMMENTS_JSON=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100&direction=desc" || echo "[]")
  COMMENT_COUNT=$(echo "${COMMENTS_JSON}" | jq 'length' 2>/dev/null || echo "0")
  ```
- **Proposed fix:** in `wait-clarify`, replace the reads at lines 891 and 917 with one `COMMENTS_JSON=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100&direction=desc" || echo "[]")`. Then derive `COMMENT_COUNT` from it with `jq 'length'`, and `LAST_BOT_COMMENT` with the existing bot-comment filter applied to the same JSON.
- **Safety rationale:** the two calls use different parameters (`per_page` defaults to 30 against `per_page=5&direction=desc`), so the SAFE_TO_MERGE condition of identical pagination fails. `COMMENT_COUNT` currently cannot go above 30. `LAST_BOT_COMMENT`'s `.[0]` would read a 100-item window instead of a 5-item window, which can change whether the `clarification_needed` exit at `:919-924` fires.
- **Downstream signal (verification):**
  1. Confirm whether `direction=desc` is honoured by `GET /repos/{o}/{r}/issues/{n}/comments`. This decides whether `.[0]` is the newest or the oldest bot comment, both today and after the merge. Adjust the filter (`last` vs `.[0]`) so the merged read selects the same comment the 5-item read selects today.
  2. Confirm that letting `COMMENT_COUNT` go above 30 only affects the `CUR_STATE` activity signal (`:894`), which Phase 2 already accepts.

**MERGE-002** · `NEEDS_VERIFICATION`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:1409-1410` (open PR by head), `:1429-1430` (issue labels) and `:1451-1452` (branch head commit), in step "Phase 3b: Wait for PR creation" (lines 1261-1551).
- **Current calls:** 5 per 10 s iteration: those three reads plus `:1433` `actions/runs` and the conditional `:1446` `runs/{id}/jobs`. Over a roughly 15-minute implement phase (implement p95 934 s in the report) that is about 90 iterations, so about 450 calls. **Proposed calls:** 3 per iteration (1 GraphQL + `:1433` + `:1446`), about 180 fewer calls per gate run.
- **Endpoints:**
  - `GET /repos/{o}/{r}/pulls?state=open&head={owner}:ai/issue-{n}&per_page=1`
  - `GET /repos/{o}/{r}/issues/{n}`
  - `GET /repos/{o}/{r}/commits?sha=ai/issue-{n}&per_page=1`
  - proposed replacement: `POST /graphql`
- **Evidence:** the three reads cover one issue and its conventional branch, in the same iteration, with no writes between them.
  ```bash
  PR_NUMBER=$(gh_api_safe_print "repos/${TEST_REPO}/pulls?state=open&head=${TEST_REPO%%/*}:ai/issue-${ISSUE_NUMBER}&per_page=1" \
  LABELS=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" \
  COMMIT_COUNT=$(gh_api_safe_print "repos/${TEST_REPO}/commits?sha=${IMPL_BRANCH}&per_page=1" \
    --jq '.[0].sha // ""' || echo "")
  ```
  `COMMIT_COUNT` is actually the head SHA. All three fit in one query:
  `repository{issue(number:N){labels(first:100){nodes{name}}} ref(qualifiedName:"refs/heads/ai/issue-N"){target{oid} associatedPullRequests(states:OPEN,first:1){nodes{number}}}}`.
- **Proposed fix:** add one aliased GraphQL read at the top of the Phase 3b loop, following the alias pattern of `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`, and derive `PR_NUMBER`, `LABELS` and `COMMIT_COUNT` from it. Keep `:1498` `PR_RECHECK` as it is: it is a deliberate re-read after `sleep`.
- **Safety rationale:** error handling changes. `gh_api_safe_call_is_read` (`scripts/comprehensive_test_and_release_gh_api.sh:46-48`) classifies any call with `-f` as a write, so `gh api graphql -f query=…` loses the 5xx/transient retries (`:137-149`) that the three REST GETs get today. `PR_NUMBER` is also the step's success exit (`:1418-1425`).
- **Downstream signal (verification):**
  1. Confirm that `Ref.associatedPullRequests(states:OPEN)` returns the same PR as `pulls?head=owner:branch&state=open`.
  2. Decide how a missing ref maps. GraphQL returns `ref: null`. Today `commits?sha=<missing>` fails, and through non-quiet `gh_api_safe_print` it leaves `::error::` text in `COMMIT_COUNT` (the BUG-001 family). The `CUR_STATE` change detection at `:1454-1464` must be re-checked against the new values.
  3. Either add a GraphQL-safe read path to `gh_api_safe`, or accept losing transient retries.
  4. Confirm that the test issue never has more than 100 labels.

**MERGE-003** · `NEEDS_VERIFICATION`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:5930-5931` (issue labels) and `:5943-5944` (comments), in job `clarify-rejects-unsolvable-test` (starts `:5831`). The loop sleeps 15 s (`:5988`).
- **Current calls:** 2 per iteration. **Proposed calls:** 1 GraphQL per iteration.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}` and `GET /repos/{o}/{r}/issues/{n}/comments?per_page=20`, to be replaced by `POST /graphql`.
- **Evidence:**
  ```bash
  LABELS=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" \
    --jq '[.labels[].name] | join(",")' 2>/dev/null || echo "")
  ...
  BODY=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments?per_page=20" \
    --jq '[.[] | select(.user.type == "Bot" or (.user.login | endswith("[bot]"))) | .body] | join("\n")' 2>/dev/null || echo "")
  ```
  Both calls are raw `gh api … || echo ""` with no retry, so moving them to GraphQL does not lose any retry behaviour (unlike MERGE-002).
- **Proposed fix:** use one `issue(number:N){labels(first:100){nodes{name}} comments(first:20){nodes{author{__typename login} body}}}` read, and derive `LABELS` and `BODY` from it.
- **Safety rationale:** the bot filter cannot be carried over byte-for-byte. GraphQL `author.login` for the Actions bot is `github-actions`, without the `[bot]` suffix, so `endswith("[bot]")` must become `author.__typename == "Bot"`. That is an exact-semantics question static reading cannot settle.
- **Downstream signal (verification):**
  1. Check a captured response to confirm that `comments(first:20)` returns the same window, in the same oldest-first order, as REST `per_page=20`.
  2. Confirm that `__typename == "Bot"` matches exactly the set the REST `user.type == "Bot" or login endswith "[bot]"` filter matches today.

**MERGE-004** · `RISKY_SKIP`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:1605-1614` (the `HEAD_A`/`HEAD_B` stability loop) and `:1635-1637` (`PR_META`), in the bait-injection step.
- **Current calls:** at least 3 `GET pulls/{n}` (2 per stability attempt + 1). **Proposed calls:** 2 (reuse the final `HEAD_B` response as `PR_META`).
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** `PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" …)` runs right after the loop's last `HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' …)`, with no sleep in between. A full-JSON `HEAD_B` read could supply `.state`, `.merged`, `.merged_at` and `.closed_at`.
- **Proposed fix (manual only):** store the full `HEAD_B` JSON, derive `HEAD_B` from it, and use it as `PR_META` when the loop exits on a stable head.
- **Safety rationale:** the call sits inside a 5-attempt retry loop whose purpose is race protection against the implement push ("Race protection … Verify the PR head SHA is stable across two reads ≥3s apart", `:1597-1604`). Both are mandatory `RISKY_SKIP` triggers.
- **Downstream signal (risky skip):** do not auto-implement. A human must confirm that the closed/merged guard at `:1638` may use data captured inside the stability loop rather than a separate read. The saving is 1 call per gate run.

**MERGE-005** · `RISKY_SKIP`
- **Files/lines:** `scripts/promote_candidate_follow.sh:148` (`branches/promote-candidate`), `:168` (`git/ref/tags/promote-cycle-frozen`) and `:181` (`git/ref/heads/main`). The per-run budget is documented in the header at `:22-25`.
- **Current calls:** 3 reads per run (plus 1 when the tag is annotated). **Proposed calls:** 1 GraphQL (`repository{a:ref(qualifiedName:"refs/heads/promote-candidate"){target{oid}} b:ref(qualifiedName:"refs/tags/promote-cycle-frozen"){target{__typename oid}} c:ref(qualifiedName:"refs/heads/main"){target{oid}}}`).
- **Endpoints:** `GET /repos/{o}/{r}/branches/{b}` and `GET /repos/{o}/{r}/git/ref/{ref}` (×2), to be replaced by `POST /graphql`.
- **Evidence:** all three reads run back to back in step 1 (`:145-184`) with no write between them. The report already observes 27 runs/day, each with one expected 404 on the tag read.
- **Proposed fix (manual only):** as above. `protected` (`:158-162`) has no direct GraphQL equivalent, so the branch REST read may still be needed.
- **Safety rationale:**
  - The tag read is fail-closed by design: "Fail closed: an unreadable tag could be a frozen cycle" (`:173-174`).
  - It is the TOCTOU-sensitive read in BUG-004.
  - Its 404 handling depends on `GH_API_RETRY_LAST_STATUS` (`:116-119`).
  - Dropping the REST reads would remove the `GH_API_RETRY outcome=permanent` lines that the log-analysis report counts ("GH API summary").
  - Each of these is a `RISKY_SKIP` trigger.
- **Downstream signal (risky skip):** do not auto-implement. Changing this needs a manual review of how the freeze gate behaves when GraphQL returns `null` instead of 404, plus coordination with the log-analysis collector that counts `GH_API_RETRY outcome=permanent`.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** · `SAFE_TO_MERGE`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:2420` and `:2463-2464`, in step "Phase 4: Wait for review & autofix" (wait-review loop, lines 1821-2552).
- **Current calls:** 2 `GET runs/{id}/jobs?per_page=10` per iteration whenever the branch is taken. **Proposed calls:** 1.
- **Endpoint:** `GET /repos/{o}/{r}/actions/runs/{run_id}/jobs?per_page=10`, the same URL in both places.
- **Evidence:** both calls sit behind identical guards: `[ "$RUN_ID" != "unknown" ]` (`:2412` and `:2458`) and the else branch of `[ -n "${JOBS_JSON:-}" ]` (`:2413` and `:2460`). Neither `RUN_ID` nor `JOBS_JSON` is reassigned between them; lines 2430-2457 only read the PR head and PR comments.
  ```bash
  JOBS_STEP_JSON=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs/${RUN_ID}/jobs?per_page=10" || echo "")
  ...
  JOB_ID_FOR_SIZE=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs/${RUN_ID}/jobs?per_page=10" \
    --jq '([.jobs[] | select(.name | test("(^| / )codex-agent( \\([^)]*\\))?$"))] | last | .id) // ([.jobs[] | select(.status == "in_progress")] | last | .id) // .jobs[0].id // empty' || echo "")
  ```
  The `JOBS_JSON` branch at `:2461` already applies this exact filter to a cached response, with local `jq`. Line 2231 carries the comment "Fetch jobs once and reuse for failed-steps check, current step, and log size". The second read breaks that stated intent.
- **Proposed fix:** at `:2463-2464`, replace the API call with `JOB_ID_FOR_SIZE=$(echo "$JOBS_STEP_JSON" | jq -r '<filter copied verbatim from :2461>' 2>/dev/null || echo "")`.
- **Safety rationale:** the SAFE_TO_MERGE conditions all hold:
  - same endpoint, query string and helper (`gh_api_safe_quiet_print`, so the same retry and quiet behaviour);
  - same step and iteration, with byte-identical guards and no intervening mutation;
  - a failure gives `""` in both forms, so `JOB_ID_FOR_SIZE=""` and `LOG_SIZE=0` either way;
  - no parallel job reads these variables;
  - `JOBS_STEP_JSON` is always assigned in any iteration where `:2463` would run.
- **Downstream signal (directive):** in `test-and-mark-stable.yml` wait-review, reuse `$JOBS_STEP_JSON` (fetched at `:2420`) at `:2463-2464` with the `:2461` jq filter, and change nothing else. In particular, do not add a per-iteration reset of `JOBS_JSON` in the same change. It is assigned only at `:2231` and never cleared, which is a separate stale-data behaviour for the Deep Audit to judge.

**REUSE-002** · `NEEDS_VERIFICATION`
- **Files/lines:** `.github/workflows/review_autofix_sweep.yml:285-290` (default-branch read) and `:228-237` (paginated open-PR listing). The value is used at `:350` and `:378-379`. The cron is `*/30 * * * *` (`:80`).
- **Current calls:** 1 `GET /repos/{o}/{r}` per tick with at least one candidate (up to 48/day). **Proposed calls:** 0.
- **Endpoints:** `GET /repos/{o}/{r}`, which is redundant with `GET /repos/{o}/{r}/pulls?state=open` (paginated).
- **Evidence:** REST pull-request list items include `base.repo`, which is the full repository object and carries `default_branch`. The sweep's jq projection discards it:
  ```bash
  | jq -s '(add // []) | map(select(.draft == false) | {
    number,
    head_ref: .head.ref,
  ```
  Then it immediately re-reads the same value:
  ```bash
  sweep_default_branch="$(GH_TOKEN="${READ_TOKEN}" gh api "repos/${REPOSITORY}" --jq .default_branch 2>/dev/null || true)"
  ```
  Both calls use the same token (`READ_TOKEN`). The read is only reached when `total > 0` (`:273-276`), so at least one listed PR is always there to supply the field.
- **Proposed fix:** add `default_branch: (.base.repo.default_branch // "")` to the projection at `:230-237`. Set `sweep_default_branch` from the first element, keep the existing regex and `null` validation at `:286` with its fail-closed `AUTOFIX_SWEEP_SKIPPED reason=default_branch_unavailable` exit, and delete the `:285` call.
- **Safety rationale:** the value is a provenance gate (#6629, `:278-284`) whose comment says "no fallback: a failed read skips every dispatch". The target call is paginated, though this change does not touch its pages. Whether `base.repo.default_branch` is present in every list item cannot be proven by reading the code.
- **Downstream signal (verification):**
  1. Capture one `pulls?state=open` response and confirm that `base.repo.default_branch` is populated and equal to `repos/{repo}.default_branch`.
  2. Confirm that `tests/` fixtures for the sweep include `base.repo`, and add it where they don't.
  3. Keep the fail-closed exit unchanged.

**REUSE-003** · `NEEDS_VERIFICATION`
- **Files/lines:** `.github/actions/run-soft-error-analyzer/action.yml:105` (`FINAL_STATUS` re-read) against the poll read at `:92`. This adds to Deep Audit API-006.
- **Current calls:** 1 extra `GET actions/runs/{id}` per phase whose wait deadline expires. **Proposed calls:** 0 extra.
- **Endpoint:** `GET /repos/{o}/{r}/actions/runs/{id}`.
- **Evidence:** the in-loop `STATUS` read at `:92` (raw `gh api … 2>/dev/null || echo ""`) is at most about 5 s old when the deadline check at `:104` runs. `FINAL_STATUS` re-reads the same endpoint, with the same raw no-retry form, only to choose whether to print the `::warning::` at `:107`. A grep shows that warning text appears only in this file.
- **Proposed fix:**
  - set `STATUS=""` before the inner loop at `:91`;
  - replace `:105` with `FINAL_STATUS="${STATUS:-unknown}"`.
- **Safety rationale:** without the reset, `STATUS` is unset under `set -u` when `WAIT_SECS=0`, and it would carry over from the previous phase. Run status changes over time (the inner loop sleeps 5 s), so the reused value can be up to one poll interval older. That breaks the "no state change between calls" condition.
- **Downstream signal (verification):**
  1. Confirm that no caller depends on the warning being suppressed when a run completes in the final ≤5 s window.
  2. Confirm that no caller passes `wait_for_completion_secs: 0`, or that the `STATUS=""` reset is included.
  3. If API-006's `known_completed` input lands first, re-evaluate: this call may then be unreachable on most paths.

**REUSE-004** · `NEEDS_VERIFICATION`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:2209` (`PIN_HEAD_NOW`, on the cancelled-run branch) and `:2443-2444` (`PR_HEAD_SHA`), in the Phase 4 wait-review loop.
- **Current calls:** 2 `GET pulls/{n}` in an iteration where the review run concluded `cancelled` and the head did not advance. **Proposed calls:** 1.
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}`, both with `--jq '.head.sha // ""'` via `gh_api_safe_quiet_print`.
- **Evidence:** when `PIN_HEAD_NOW` equals `PIN_SHA`, the `continue` at `:2221` is skipped. Control then falls through the closing `fi`s at `:2222-2224` to `:2443` in the same iteration, which re-reads the same field.
- **Proposed fix:** add an iteration-scoped flag (for example `PIN_HEAD_NOW_ITER=""`, reset at the top of the loop next to `:2171-2174`). Set it at `:2209`, and use it at `:2443` when it is non-empty.
- **Safety rationale:** `PIN_HEAD_NOW` is never reset between iterations, so a naive reuse would feed a stale head into `CUR_STATE`. Making it safe needs a new reset point, which static reading cannot prove harmless to the pin-advance logic.
- **Downstream signal (verification):**
  1. Confirm that the new reset does not change the `:2210-2213` 40-hex validation exit.
  2. Confirm that `PR_HEAD_SHA` is only an activity signal on this path.
  3. Expected impact is low, because the path is rare (a cancel with no head advance).

**REUSE-005** · `RISKY_SKIP`
- **Files/lines:** `scripts/orchestrate_poll_process.sh:4741-4757` (`_subissue_closing_pr_number`, tier 2). The timeline enrichment it ignores is at `scripts/gh_helpers.sh:1814-1821`.
- **Current calls:** 1 timeline read plus 1 `GET pulls/{n}` per cross-referenced PR, including PRs that are not merged. **Proposed calls:** 1 timeline read plus 1 `GET pulls/{n}` per *merged* cross-referenced PR.
- **Endpoints:** GraphQL `issue.timelineItems`, plus `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** `_issue_cross_ref_pr_numbers_unique` (`:4451-4466`) projects the timeline down to PR numbers only. The loop then fetches every PR and skips the ones not merged:
  ```bash
  pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr}" 2>/dev/null)" || return 0
  printf '%s' "${pr_json}" | jq -e '(.merged_at // null) != null' >/dev/null 2>&1 || continue
  ```
  The GraphQL timeline already carries `merged`/`merged_at` for each source PR.
- **Proposed fix (manual only):** add a sibling helper that returns `{number, merged}` from the timeline JSON, and pre-filter to merged PRs before the per-PR body fetch. Do not change `_issue_cross_ref_pr_numbers_unique`'s output: it has callers at `:4543`, `:4742`, `:9262`, `:19056` and `:25477`.
- **Safety rationale:** the code is inside `orchestrate_poll_process.sh`, which is a mandatory `RISKY_SKIP` trigger. The REST timeline fallback may report `lookup_failed` without a reliable merged flag. The `|| return 0` on fetch failure is load-bearing, because it skips fingerprint capture.
- **Downstream signal (risky skip):** do not auto-implement. A manual review must confirm that the REST-fallback timeline shape gives a trustworthy `merged` value. Otherwise unknown entries must still get the per-PR fetch. This is the same root pattern as Deep Audit API-001.

### Dead Calls (DEAD-API-###)

**DEAD-API-001** · `RISKY_SKIP`
- **Files/lines:** `.github/workflows/test-and-mark-stable.yml:6173-6176`, in job `e2e-alt-model-test` (starts `:6042`), step `id: wait`. The loop sleeps 20 s (`:6191`) with a 4500 s deadline (`:6167`).
- **Current calls:** 1 `POST issues/{n}/comments` (`/approved`) on every 20 s iteration for as long as `ai:awaiting-approval` remains. **Proposed calls:** 1 per approval cycle.
- **Endpoint:** `POST /repos/{o}/{r}/issues/{n}/comments`. Each comment also fans out `issue_comment` workflow runs; the report already notes 4–8 runs per comment event.
- **Evidence:** every POST after the first has no consumed result (`>/dev/null 2>&1 || true`) and adds no new state until the implement workflow swaps the label:
  ```bash
  if echo "${LABELS}" | grep -q 'ai:awaiting-approval'; then
    gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments" \
      -f body="/approved" >/dev/null 2>&1 || true
  fi
  ```
  The main e2e job posts `/approved` exactly once (`:1253-1259`).
- **Proposed fix (manual only):** post once and record the time. Re-post only if `ai:awaiting-approval` is still present after a bounded interval, for example 120 s.
- **Safety rationale:** with `|| true` swallowing failures, re-posting on every iteration is this job's only retry for a dropped or ignored approval. The call is effectively inside a retry loop, which is a mandatory `RISKY_SKIP` trigger.
- **Downstream signal (risky skip):** do not auto-implement. A human must choose the re-post interval and confirm that a single `/approved` reliably triggers implement on the alt-model path, without the repeated posts acting as an accidental retry.

Sweep note: other reads with discarded output, for example `scripts/workflow_failure_heal_intake.sh:2237` (`_branch_exists`) and `test-and-mark-stable.yml:683`, are existence or permission probes whose exit code is used. They are not dead.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — the change is in `orchestrate_poll_process.sh` and merges a paginated `pulls/N/files` call. Net-new detail: `_issue_cross_ref_pr_numbers_unique` (`:4451-4466`) is shared by 5 callers, so the state reuse needs a new sibling helper rather than an output change, and the early return at `:9288-9290` must keep reading the REST state whenever the timeline came from the REST fallback.
- API-002: NEEDS_VERIFICATION — the two scans run in separate workflows (crossing a concurrency boundary). Detecting whether a repo has the poll wrapper needs a §8-defaulted signal. Removing the scheduled scan also removes the `Scheduled cleanup is running the global merge-train release scan` log line (`cancel_on_pr_close.yml:389`).
- API-003: NEEDS_VERIFICATION — the gh CLI's "already exists" stderr text must be confirmed. Net-new detail:
  - A per-process cache would be lost when callers run `set_issue_phase_label_resilient` inside `$(...)`; this is the same trap documented at `scripts/review_merge_train.sh:205-212`.
  - Alternate fix: do the issue-labels GET (`label_helpers.sh:223`) first and skip `ensure_label_exists` when `_cur` already contains the target, but keep it on the GET-failure POST-fallback path.
- API-004: RISKY_SKIP — it changes classification inside retry loops (`gh_api_json_to_file`, `curl_gh_api`), which is a mandatory trigger even though the direction (fewer retries) is correct.
- API-005: NEEDS_VERIFICATION — removal is gated on the registry preflights (`docs/scripts-pending-removal.md:212-220`). The call is paginated.
- API-006: NEEDS_VERIFICATION — it needs a new composite input with a default, wired at every caller. See REUSE-003 for the additional deadline-path re-read.
- API-007: NEEDS_VERIFICATION — a GraphQL batch must fall back to per-issue REST so the `heal_pr_lookup_failed` per-issue log (`workflow_failure_heal_pr_reconcile.sh:245`) and the `continue` semantics stay the same. `associatedPullRequests` returns `headRefOid`, not `head.sha`, so the lower-casing at `:251` must be re-applied.

### Summary Counts

These counts cover this pass's findings only; the Deep Audit IDs are tagged in the cross-references above.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 6 | MERGE-001, MERGE-002, MERGE-003, REUSE-002, REUSE-003, REUSE-004 |
| RISKY_SKIP | 4 | MERGE-004, MERGE-005, REUSE-005, DEAD-API-001 |

### Implement-Stage Handoff
- REUSE-001: in the `test-and-mark-stable.yml` Phase 4 wait-review loop, reuse `$JOBS_STEP_JSON` (fetched at `:2420`) for `JOB_ID_FOR_SIZE` at `:2463-2464`, using the jq filter from `:2461`. Do not combine this with any change to how `JOBS_JSON` is reset.