## Executive Summary

- **The deep-dive log folders were not available, so most findings rest on aggregates (high confidence on that gap).** The prompt said logs were mounted at `/home/runner/work/_temp/workflow-log-output`, but that path does not exist in this sandbox, so there were no `summary.json`, `errors/`, `slow/` or `recent/` folders to read. The collector also reported 14 `partial_data:missing_log_archive` errors. Everything below comes from `analysis_context.json`, the `log_summary` fields on 13 runs, and the repository source. Impact: no step-level evidence for the two real failures, and no `AI_MEMORY_TELEMETRY` data.
- **Waiting for a runner is the main source of latency (high confidence).** All 11 runs with a `log_summary` that mentions timing hit "All GitHub-hosted runners with label [ubuntu-latest] are busy". Together they waited about 2,135 s of their 3,818 s total, roughly 56%. Examples: run 39549258425 waited about 435 s and then ran for 6 s; run 39549258339 waited about 477 s and its steps took 3 s. CI has 11 jobs per run and no workflow-level `concurrency:` group (`.github/workflows/ci.yml:1-21`). CI p50 is 1,754 s even though no single job may exceed 20 minutes, which suggests the CI jobs themselves are queuing (inference). Cancelling superseded pull-request CI runs could plausibly cut queue time by 30–50% at peak (low-medium confidence).
- **15 of the 18 recorded failures are runs that never started a job, not real failures (high confidence on the pattern, medium on the cause).** Each lasted 0 s, has no job or step, has no log archive (HTTP 404), and is named after its file path. This matches the zero-job "workflow file issue" pattern described in §24. All four affected callers are `pull_request` wrappers that call a reusable workflow at `@main`. Breakdown: `internal-review.yml` ×10, `internal-issue-pr-status.yml` ×2, `internal-cancel-on-pr-close.yml` ×2, `internal-workflow-failure-heal.yml` ×1. At 03:08:34 and 04:14:53 three callers failed in the same second, and two of them only trigger on `types: [closed]`. Leading hypothesis (inference): the `closed` and other PR events run the caller files from an `orchestrator/project-*` integration branch whose copies have drifted from main. These failures are why `review_autofix` shows a 21.2% failure rate; its real failure rate is 1 in 52.
- **One review run used 63% of the window's tokens (high confidence).** Run 39544299660 used 32,444,642 of 51,503,334 tokens, made 28 calls against 10 for each of the other two token-bearing runs, bootstrapped Semble twice, and accounts for all 4 `CONTEXT_BUDGET_WARN` lines. Bringing it in line with the 10-call baseline would save about 22.5M tokens, about 44% of the window (medium confidence; the cause of the extra calls is not visible).
- **The prompt cache is working at about 81% overall, but the per-run metric hides it (high confidence).** 41,454,893 of 51,177,168 input tokens were cache reads. `compute_cache_hit_rate` (`scripts/cost_audit.py:454-456`) returns `null` if any call lacks usage data, so the 84% rate in run 39544299660 shows as `null`. Fix this in the collector only, with no runtime risk.
- **Semble and Serena:** Semble is healthy and adds little prompt volume. Over 4 queries it returned 60,615 bytes, cited 30 sources and duplicated 0 static bytes. Its 0 runtime fallbacks are a good sign, and the 28 contract-test fallbacks are expected. Serena logged nothing in this window. The one thing to fix is bootstrap time: about 46 s per bootstrap, about 185 s in total.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Cancel superseded pull-request CI runs (critical path).**
   - **Evidence:** CI p50 is 1,754 s and p95 is 3,108 s; the slowest runs took 3,319 s (39533795855), 3,303 s (39532405225) and 3,174 s (39532625923). `ci.yml` has 11 jobs per run (`static-checks`, four test jobs, `orchestrate-poll` ×4 shards, `budget-watch`, `lint`) with `timeout-minutes` of 15–20 each. A run lasting about 2× the largest job timeout means jobs waited for runners. Between 02:05 and 02:38, 18 or more CI runs were created. The light jobs that ran at the same time also queued: about 7 minutes in 39549258425 and about 5 minutes in 39549039430.
   - **Root cause:** `ci.yml` has no `concurrency:` block, so every push to a PR head keeps its 11 jobs even after a newer commit replaces it. Autofix pushes produce exactly this kind of replacement.
   - **Change (pull requests only; push CI on main, stable and integration branches stays as it is):**
     ```yaml
     concurrency:
       group: ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
   - **Estimated saving:** 30–50% less runner queue time at peak for every family on `ubuntu-latest` (inference; it depends on how often PRs are pushed again).
   - **Risk:** medium. `scripts/ci_cancelled_rerun.py` already skips superseded heads (`decision(..., "skip", "superseded_head")`, line 329), so it should not re-run these cancelled runs. Check that its "genuine vs shutdown" classifier does not raise alerts for them. Roll back by deleting the block.
2. **Reuse the Semble index between review passes (micro, on the review critical path).**
   - **Evidence:** run 39544299660 bootstrapped twice for 88,402 ms; runs 39533145139 and 39534164327 bootstrapped once each, for 49,200 ms and 47,390 ms. Average is 46.2 s per bootstrap.
   - **Change:** when the workspace SHA is unchanged, skip the second bootstrap and reuse the first index.
   - **Saving:** about 44 s per multi-pass review. Risk: low, because Semble falls back safely if the index is missing.
3. **Leave `integration-pr-readiness.yml` alone (no change recommended).** Run 39549258425 waited 7 minutes for a check that did nothing ("not an orchestrator/project-* branch"). But the workflow comment at lines 45-49 says it must run on every PR so the required status always exists. Item 1 relieves its queue time indirectly.
4. **Catch prompt-budget failures before CI.** Run 39541465718 failed at `static-checks / Prompt tier budget` after 1,479 s. The check itself is a 100-line script (`tests/prompt_size_budget.py`) that takes about 1 s. Most of the elapsed time was queue time and the 18 steps before it (inference). Map `prompts/*.txt` to `tests/prompt_size_budget.py` in the existing pre-push affected-test gate (`ci.yml` step "Pre-push affected-test gate tests"), so implement and autofix catch it before pushing. Saving: one full CI cycle, about 25 minutes, per occurrence. Risk: low.

## Cost Optimizations

Ranked by expected token savings. Dollar figures are omitted because the context has no prices.

1. **Find out why run 39544299660 made 28 calls.**
   - **Evidence:** 28 calls, 32,444,642 total tokens, 4,199,806 uncached prompt tokens and 200,012 completion tokens. Each baseline run made 10 calls and used about 9.3–9.8M tokens. The run also had 4 `CONTEXT_BUDGET_WARN`, 4 calls without usage data, and 2 Semble bootstraps and queries.
   - **Root cause (inference):** a second full review pass, suggested by the second bootstrap. The pass and its trigger cannot be confirmed without the logs.
   - **Change:** first add the per-pass summary line in Reliability item 3, then gate pass 2 so it reviews only the changed hunks where the pass-1 findings allow.
   - **Saving:** up to about 22.5M tokens in this window, about 44%. **Quality risk:** medium; a narrower pass 2 could miss regressions caused by the autofix edits, so keep full-file review for any file the editor touched.
2. **Keep context size down (`CONTEXT_BUDGET_WARN` = 4, all in 39544299660).** That run averaged about 1.16M tokens per call, against about 0.93–0.98M in the other two. Prompt size grows during the run (inference). Cap how much each later call re-sends: carry the pass-1 findings forward as a summary instead of the full transcript. Saving: about 15–20% of that run's input tokens (inference). Risk: low-medium.
3. **Semble is not a cost driver.** It added 60,615 bytes over 4 queries, about 15 KB or about 4K tokens each, which is under 0.5% of a roughly 1M-token call. `static_dup_bytes_total=0` and 30 sources mean it is not duplicating static context. Keep it as is.
4. **Serena:** no `SERENA_*` lines at all (0 probes, 0 queries). Its cost or benefit cannot be assessed. Confirm whether Serena is meant to be enabled for review.
5. **Model and reasoning level:** the clarify runs (39549483365, 39549481732, 39548974896) use `openai/gpt-6-sol` with `MODEL_REASONING_EFFORT=high`. No token lines are visible for them, so no change is recommended without usage data.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **Zero-job startup failures (15 of 18 failures).**
   - **Evidence:** run IDs 39547071001, 39546823291, 39542983739, 39542978379, 39541440182, 39541271921, 39538092875, 39536934548, 39536929046, 39532936167 (`internal-review.yml`); 39542978384 and 39536929054 (`internal-issue-pr-status.yml`); 39542978431 and 39536929058 (`internal-cancel-on-pr-close.yml`); 39547071789 (`internal-workflow-failure-heal.yml`). All have `duration_seconds: 0`, no jobs, a 404 on the log archive, and a workflow name equal to the file path. Successful CI runs were created in the same second on the same events (for example 39541440294 next to 39541440182, and 39541271922 next to 39541271921). That rules out a broken main.
   - **Root-cause category:** workflow validation at startup. Candidate causes (inference): (a) PR or `closed` events from integration branches carrying out-of-date `internal-*.yml` callers, for example with `permissions:` that no longer satisfy the reusable workflow at `@main`; (b) a PR branch that edits the callers.
   - **Fix:** add the integration-branch drift line in item 4. Then let the existing integration sync keep `.github/workflows/internal-*.yml` in step with main.
   - **Impact:** would remove up to 10 dropped reviews in this window. Each one is a PR event that never got a review.
   - **Fail-open:** the line is a warning only.
2. **`review / codex-agent` failed at "Checkout PR head branch" (run 39531372903, after 1,401 s).** The step (`review_autofix.yml:2811-2945`) has seven ways to exit with code 1. Three of them can fail with no message under `set -e`: `git_ref_health_check.sh repair` (line 2873), `git reset --hard` (2905) and `git checkout -f -B` (2923). The `HEAD_REF` empty path (2852) prints without an `::error::` prefix. The cause cannot be identified without logs. **Fix:** add an ERR trap at the start of the step that emits one structured line with the reason, without changing control flow:
   ```bash
   trap 'echo "::error::REVIEW_CHECKOUT_FAIL line=${LINENO} rc=$? cmd=${BASH_COMMAND%% *} head_ref=${HEAD_REF:-unset}"' ERR
   ```
   Check the file size with `wc -c` against the 480,000-byte guard (§24). Register the new prefix in an `agents.d/` fragment (§25). Impact: the next failure can be diagnosed from its log. Rollback is trivial.
3. **Per-pass review summary line.** `scripts/review_run_reviewers.sh` mentions pass 2 48 times but emits no structured per-pass summary. Run 39544299660, and the earlier unexplained drop of a pass-2 slot recorded in `docs/completed/opencode-review-autofix-cutover-plan.md` (run 33298934878, "cause not in logs"), show the gap. Add one line per pass, for example `REVIEW_PASS_SUMMARY pass=<n> reviewers_ok=<k>/<m> calls=<c> trigger=<reason>`. Confirm the name does not clash with existing names first. Impact: explains token outliers and dropped slots.
4. **Integration caller drift line (diagnostic).** In the integration-sync path of `orchestrate_poll`, run `git diff --quiet origin/main -- '.github/workflows/internal-*.yml'` for each `orchestrator/project-*` branch. Emit `INTEGRATION_CALLER_DRIFT project=<N> files=<list>` when it differs. This needs no API calls, because the refs are already fetched. It confirms or rules out hypothesis 1a.
5. **`CONTEXT_BUDGET_WARN` (4 lines, all in 39544299660)** points to prompt-size risk, not rubric pressure. `BREAK_GLASS` = 0 in every run. See Cost item 2.
6. **Semble and Serena fallbacks:** 28 Semble fallbacks, all from contract tests (exactly 4 per CI run across 7 CI runs, coming from the "Review Semble contract test" and "Judge Semble prefetch contract test" steps). Runtime fallbacks are 0 and bootstrap failures are 0, so this is healthy and nothing is hiding a broken rollout. Serena probes: 0 OK, 0 failed, 0 skipped. That means Serena did not probe at all, not that it failed. Confirm this is intended.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines were available. The deep-dive logs were not mounted, and every `log_summary` that mentions it (39549483365, 39549481732, 39548974896, 39548815781 and the lint/readiness runs) explicitly reports none in the visible portion. Those summaries dropped about 156K–282K characters, so the lines may be in the missing part.
- Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` entries, and push retry counts **cannot be computed** for this window.
- `memory_maintenance` ran once (run 39532223142, 944 s, success). There is no indication of degradation.
- **Action:** make the collector extract `AI_MEMORY_TELEMETRY` lines into `cost_telemetry` (counts by `op`, `records_selected>0` rate, number of `fail_open` entries) so summary truncation stops hiding them. Then check that clarify, plan and implement emit at least one `op=retrieve` line per run.

## GH API Call Audit

- **No rate-limit pressure was seen.** The visible summaries contain no 429s, secondary limits or retries (39549045918, 39549039388, 39549258339 and others).
- **Per-run usage is already lean:** the lint workflow makes one paginated `pulls/<n>/commits?per_page=100` call (11 and 3 commits respectively); cancel-on-pr-close made no cancel calls (39549801604); the readiness check took about 1.6–1.9 s.
- **Wasted calls in the collector:** it made 14 `actions/runs/<id>/logs` requests (each with retries=1, per `tests/test_collect_workflow_logs.py:1762`) for zero-job runs that can never have an archive. **Fix:** skip the archive fetch when the run's job list is empty. The data is already in hand from the jobs call. Saving: 14 calls per window, about 1.4% of the 1,000-run window. Rate-limit risk reduction is small.
- **Collector window has poor coverage:** 721 of 1,000 runs (72%) were `skipped`, which squeezes the window to about 3.5 hours (02:00–05:31 UTC). Querying the runs list once per conclusion (`status=success|failure|cancelled|timed_out|startup_failure`) instead of one unfiltered list would cover about 3.6× more time for the same number of runs. It adds about 4 list calls per collection. This is the §14 "check first, add second" trade-off, justified because the existing unfiltered call is mostly noise.
- **Duplicate row:** run 39536929054 appears twice in `failing_runs`, which inflates `issue_pr_status.failure_count` to 3 (actually 2). Deduplicate by `run_id` before aggregating.
- No orchestrator-side API hotspot data was in this window; the poller logs were not available.

## Prompt Cache & Memory System

- **Cache hit rate:** about 0.810 across the window (41,454,893 cache reads out of 51,177,168 input tokens). By run: 39533145139 0.7126, 39534164327 0.8097, and 39544299660 shown as `null` but computed at about 0.8394.
- **Metric gap:** `compute_cache_hit_rate` (`scripts/cost_audit.py:454-456`) returns `None` if any call lacks usage data (4 such calls in 39544299660). Keep `cache_hit_rate` as it is for backward compatibility, and add an extra field (for example `cache_hit_rate_available_calls`, after checking the name is unique) computed over the calls that do have usage data.
- **Fragmentation:** 39533145139 hit 71% against 81% for 39534164327, with similar call counts. The 2.30M against 1.34M uncached prompt tokens suggest prefix variance in the first run (inference). Check that per-run dynamic content (PR diff, Semble excerpt, run IDs) sits after the stable system and rubric prefix.
- **`CONTEXT_BUDGET_WARN` pressure:** the 4 warnings line up with the largest run; growing prompts reduce what the cache saves. See Cost item 2.
- **Cache writes:** 1,875,212 tokens, 3.6% of the total, which is healthy.
- **Memory retrieval:** no telemetry; see AI Memory Health.

## Orchestrator Health

- **Progress is healthy:** project #7741 reached "all 3 sub-issue(s) ... ticked — integration PR is ready" (39549039430). Validate `[tracking:7741]` succeeded in 851 s (39547245530). Integration PR #7743 passed the auto-close lint using `Refs #7741` (39549045918, 39549039388).
- **Poller:** 36 runs: 17 succeeded and 19 were cancelled (53%), all at `cancelled_before_first_step`, for example 39549217643 (126 s), 39548826471 (249 s) and 39548555254 (184 s). The concurrency group is set with `cancel-in-progress: false` (`orchestrate_poll.yml:68-73`), so these are pending runs replaced by newer ones. That is harmless, but poll p50 of 424.5 s and p95 of 1,447 s exceed the `*/5` cron, and the slowest polls took 1,687 s (39531381167) and 1,676 s (39533713982). **Indicator to track:** replaced-run rate and p95 poll duration. **Smallest mitigation:** none needed until poll p95 exceeds about 30 minutes; the force-tick path already covers progress.
- **Clarify gates behave correctly:** 39548589031 skipped closed issue #7779 (`reason=issue_closed`), and `ORCHESTRATOR_MAX_CLARIFY_CYCLES: 3`. No clarify loops were seen.
- **Risk:** startup failures on `closed` events (Reliability item 1) mean `issue_pr_status` and `cancel_on_pr_close` sometimes never run when a PR closes. That can leave stale status or orphaned runs; scheduled cancel cleanup offsets part of this (39549801604).

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| clarify / plan / implement | Queue wait, plus skipped-run noise | Clarify waited 124 s (39549483365) and 225 s (39548974896); 128–137 runs per family were skipped |
| review/autofix | Compute (p50 539 s, slowest 3,393 s) and token-heavy reruns | 39544299660 made 28 calls; Semble bootstrap about 46 s |
| CI | Queue plus compute (11 jobs, p50 1,754 s) | No concurrency group; slow runs over 3,000 s |
| validate / orchestrate | Compute | Validate 851–975 s; orchestrate 696 s with 88 s queue (39548815781) |
| merge / readiness / lint | Almost all queue | 39549258339: 477 s queue, 3 s work; 39549258425: 435 s queue, 6 s work |
| Startup failures | Dropped events (no compute) | 15 zero-job runs |

Fixes in order of end-to-end impact: (1) a CI concurrency group, which frees runners for every stage; (2) caller drift detection and resync, which stops dropped review and close events; (3) gating review pass 2 and reusing the Semble index; (4) catching prompt-budget failures with the pre-push gate.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** contention for `ubuntu-latest` runners (about 56% of sampled run time is queue); CI p50 of 1,754 s.
- **Top failure modes:** zero-job startup failures (15, all on `pull_request` wrappers calling `@main`); a review checkout failure (39531372903); a deterministic prompt-budget failure (39541465718).
- **Highest-cost driver:** `review_autofix` used all 51.5M tokens in the window, 63% of them in run 39544299660.
- **Top 3 actions:**
  1. Add a CI `concurrency` group that cancels superseded pull-request runs only.
  2. Add `INTEGRATION_CALLER_DRIFT` and `REVIEW_CHECKOUT_FAIL` diagnostics and have the collector classify zero-job runs.
  3. Add a per-pass review summary, then gate review pass 2 to cut token outliers.

## Metrics Appendix

**Run outcomes (1,000-run window, 02:00–05:31 UTC)**

| Metric | Value |
|---|---|
| Total runs | 1,000 |
| Success / Failure / Cancelled / Other (skipped) | 238 / 18 / 23 / 721 |
| Failure rate | 1.8% (0.3% excluding the 15 zero-job runs) |
| p50 / p95 duration (all) | 1 s / 1,471 s |
| wall_clock_p50_ms / p99_ms (repo) | 1,000 / 3,381,700 (106 samples) |

**Key families**

| Family | Runs | Success | Fail | Cancelled | p50 s | p95 s |
|---|---|---|---|---|---|---|
| ci | 51 | 49 | 1 | 1 | 1,754 | 3,108 |
| review_autofix | 52 | 38 | 11 (10 zero-job) | 1 | 539.5 | 2,971 |
| orchestrate_poll | 36 | 17 | 0 | 19 | 424.5 | 1,447 |
| integration_pr_readiness | 38 | 38 | 0 | 0 | 187.5 | 846.6 |
| lint_pr_body_auto_close | 34 | 34 | 0 | 0 | 113.5 | 533.6 |
| issue_pr_status | 9* | 6 | 3* | 0 | 176 | 483 |
| cancel_on_pr_close | 20 | 18 | 2 | 0 | 128.5 | 456 |

\*Includes the duplicate row for 39536929054.

**Tokens and cache (review_autofix; no other family had token data)**

| Metric | Value |
|---|---|
| or_calls (with usage / without usage) | 48 (44 / 4) |
| Prompt (uncached) / cache read / cache write / completion | 7,847,063 / 41,454,893 / 1,875,212 / 326,725 |
| Total tokens | 51,503,334 |
| cache_hit_rate (as reported) | null (because of the 4 calls without usage) |
| cache_hit_rate (computed over all calls) | about 0.810 |
| Per run | 39533145139: 9.77M, 0.7126 · 39534164327: 9.29M, 0.8097 · 39544299660: 32.44M, about 0.839 |
| break_glass_count | 0 |
| context_budget_warn_count | 4 (all in 39544299660) |
| codex_calls / tokens | 0 / 0 |

**Semble**

| Metric | Value |
|---|---|
| Query calls / logged bytes / bytes per query | 4 / 60,615 / about 15,154 |
| Sources / static_dup_bytes | 30 / 0 |
| Bootstraps / failed / unused / total ms | 4 / 0 / 0 / 184,992 (about 46.2 s each) |
| Fallbacks (contract-test / runtime) | 28 (28 / 0), exactly 4 per CI run across 7 runs |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | fallbacks |
|---|---|---|---|---|---|---|
| serena (all) | 0 | 0 | 0 | 0 | 0 | 0 |

No other MCP servers were observed.

**GH API**

| Signal | Value |
|---|---|
| 429 / secondary-limit events in visible summaries | 0 |
| Collector log-archive 404s (zero-job runs) | 14 |
| Lint commit fetch | 1 paginated call per run |

**Runner queue samples (from `log_summary`)**

| Run | Queue (s) | Total (s) |
|---|---|---|
| 39549258339 | ~477 | 485 |
| 39549258425 | ~435 | 445 |
| 39549039430 | ~314 | 324 |
| 39549045918 | ~267 | 275 |
| 39548974896 | ~225 | 686 |
| 39549483365 | ~124 | 352 |
| 39549801604 | ~116 | 129 |
| 39548815781 | ~88 | 696 |
| 39549039388 / 39548589031 / 39549481732 | ~37 / ~37 / ~15 | 46 / 48 / 332 |

**Data gaps:**
- The deep-dive directory was missing.
- 14 log archives returned 404.
- `log_summary` excerpts were truncated (by about 33K–282K characters).
- No `AI_MEMORY_TELEMETRY` lines.
- No step-level timing for CI or review.
- `sampled_success_runs: 0` contradicts `success_sample_rate: 0.07`.
- The window covers only about 3.5 hours because 72% of runs were skipped.

**Assumptions:** the 0-second runs named by file path are GitHub's zero-job "workflow file issue" runs from §24. That is inferred from their shape, because no error text could be fetched. Queue time is inferred from "runners busy" timestamps in `log_summary`.

**What I checked in the repository:** `ci.yml` (no concurrency group, job layout), `review_autofix.yml:2811-2945`, `integration-pr-readiness.yml`, `orchestrate_poll.yml:64-75`, the four `internal-*.yml` callers (triggers and `uses:` lines), `tests/prompt_size_budget.py`, `scripts/cost_audit.py:454-465` and `:805-827`, and `scripts/collect_workflow_logs.py:1025-1064`. I changed no files; this report is the deliverable.