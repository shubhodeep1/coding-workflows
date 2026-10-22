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