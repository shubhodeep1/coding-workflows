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
## Deep Audit — Workflows & Scripts (2026-10-21)

> Scope note: this section adds to the earlier sections of this report and does not repeat them. Findings already covered above are left out: the missing CI `concurrency:` group (Speed Optimizations item 1), zero-job startup failures on `internal-*.yml` callers (Reliability item 1), the `REVIEW_CHECKOUT_FAIL` ERR trap for `review_autofix.yml:2811-2945` (Reliability item 2), and the collector and `compute_cache_hit_rate` gaps (GH API Call Audit, Prompt Cache). There was no shell in this audit, so the character and byte counts below are estimates from line counts and are marked `[NEEDS VERIFICATION]`.

### Section 1: Bug & Correctness Sweep

**BUG-001 — Release smoke test can take a `gh api` error line as the implementation PR number**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1409-1418`; root cause in `scripts/comprehensive_test_and_release_gh_api.sh:151-155`
- **Severity:** Medium · **Category:** `bug`
- **Description:** `gh_api_safe` prints `echo "::error::gh api call failed: $*"` to **stdout**, not stderr (lines 151-155). It does this when a read fails with a non-rate-limit error, or when the transient-retry budget runs out. `gh_api_safe_print` is called inside a command substitution: `PR_NUMBER=$(gh_api_safe_print "repos/${TEST_REPO}/pulls?state=open&head=…" --jq '.[0].number // empty' || echo "")`. So that error line becomes the value of `PR_NUMBER`. Line 1418 only checks `[ -n "$PR_NUMBER" ]`, so the step logs "✓ Implementation PR #::error::gh api call failed… created" and carries on with a non-numeric PR number. The repository already knows about this stdout behaviour: the comment at `test-and-mark-stable.yml:1000-1002` says the runs-page reads have to shape-check for it. The `PR_NUMBER` read was never given that check. The `LABELS` read at 1078-1079 has the same contamination, but there it is harmless because the later checks `grep` for specific label names.
- **Recommended fix:** Validate the value right after the capture, without changing the helper's contract:
  ```bash
            if ! [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
              PR_NUMBER=""
            fi
  ```
  For the root-cause fix, see CONSIST-001.

**BUG-002 — Issue-label reads are not paginated (GitHub returns 30 labels by default)**
- **File / lines:** `scripts/orchestrate_poll_process.sh:4138-4141` (`get_issue_labels_json`); `scripts/validate_process.sh:1289-1291`
- **Severity:** Low · **Category:** `bug`
- **Description:** Both functions call `repos/…/issues/N/labels` with neither `per_page` nor `--paginate`, so GitHub returns at most 30 labels. `set_issue_phase_label` (`orchestrate_poll_process.sh:4091-4103`) and `set_tracking_phase_label` (`validate_process.sh:1295-1303`) only remove labels they see in that list. On an issue with more than 30 labels, a stale phase label could therefore stay in place, which is the exact failure their comments at 4084-4089 and 1282-1287 describe. `label_helpers.sh:223` already does this correctly with `--paginate … | jq -cs 'add // []'`.
- **Recommended fix:** Use the `label_helpers.sh:223` form, or at least add `?per_page=100`, in both places.

**BUG-003 — Unprotected raw `gh api` recheck can fail the Plan phase on a single transient error**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1143-1144`
- **Severity:** Low · **Category:** `bug`
- **Description:** `LABELS_RECHECK=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '…' 2>/dev/null || echo "")` is the pattern that `scripts/gh_helpers.sh:1225-1236` calls broken: on a non-2xx response `gh` writes the error JSON to stdout. It also does not retry. This recheck exists to tolerate delayed labels. If it fails transiently, the step falls through to `status=plan_failed` (1214-1216) whenever there are no other active Plan runs, which turns an API blip into a failed release gate.
- **Recommended fix:** Use the same helper as line 1078 (`gh_api_safe_print … || echo ""`), then validate the result as in BUG-001 before calling `grep`.

**BUG-004 — Repo-wide concurrency group lets close events and the 5-minute schedule replace each other's pending runs `[NEEDS VERIFICATION]`**
- **File / lines:** `.github/workflows/cancel_on_pr_close.yml:14-16`; trigger in `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
- **Severity:** Low · **Category:** `bug`
- **Description:** The job uses `group: cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false`. In GitHub's model a newer pending run cancels an older pending run in the same group. Both `pull_request: [closed]` events and `cron: "*/5 * * * *"` share this one group. When several PRs close within one run's lifetime, the intermediate close-event runs are dropped. Their PR-scoped cancellation (lines 276-302) and immediate merge-train release (349-385) then wait for the next scheduled sweep, which is up to 5 minutes away plus queue time. The scheduled path (237-274) is a global backstop, which is why this is Low.
- **Recommended fix:** Key event runs per PR and keep the schedule separate: `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event.pull_request.number || 'schedule' }}`. First check that `review_merge_train.sh release` does not need repo-wide serialization. If it does, move only the merge-train step into its own repo-wide group.

**SEC-001 — PAT-derived basic-auth header passed on the `git` command line in 5 workflows**
- **File / lines:** `.github/workflows/review_autofix.yml:355-370`, `implement.yml:157-172`, `validate.yml:146-161`, `security-audit.yml:101-116`, `orchestrate_clarify_respond.yml:111-126`
- **Severity:** Low · **Category:** `security`
- **Description:** `ghr_auth="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 …)"` is passed as `git -c http.extraHeader="${ghr_auth}"`. That puts the credential in the process argv (`/proc/<pid>/cmdline`), where any process on the runner can read it while the clone runs. No `::add-mask::` is emitted for the base64 value. stderr goes to `/dev/null` and the runner's masker probably covers base64 shifts of registered secrets, so log exposure is unlikely `[NEEDS VERIFICATION]`.
- **Recommended fix:** Pass the header through the environment: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="${ghr_auth}" git …`. Also emit `echo "::add-mask::${ghr_b64}"` before first use. Make the change once in the shared block proposed in DUP-003.

### Section 2: GitHub API Call Redundancy Audit

**API-001 — `gh_retry` retries `gh label create` "already exists" five times with 15 s of backoff**
- **File / lines:** classifier `scripts/gh_helpers.sh:192-195`. Affected callers: `scripts/label_helpers.sh:175-191`, `scripts/validate_process.sh:1237-1252` (called twice per transition, at 1267 and 1307), `scripts/orchestrate_poll_process.sh:4037` (mitigated by `_ENSURED_LABELS_CACHE`, line 657), `.github/workflows/implement.yml:6374-6376, 6426-6428, 6613, 6634` (plus 5359, 5406, 5493, 5544), `scripts/check_failure_triage.sh:812-813`, `scripts/review_merge_train.sh:780`, `scripts/review_autofix_step_mark_ready_to_merge.sh:20-26`, and `review_autofix.yml:1170` (inline `gh_retry`: 4 attempts, 2+4+8 = 14 s).
- **Severity:** High · **Category:** `api-redundancy`
- **Description:** `_is_gh_permanent_failure` (194) matches `HTTP 422`, but `gh label create` reports an existing label as `label with name "…" already exists; use --force…`, with no HTTP status in the text. `gh_retry` (1099-1124) therefore treats it as transient. With the default `GH_RETRY_MAX_ATTEMPTS=5` (1066), that is 5 POSTs and backoff sleeps of 1+2+4+8 = 15 s (1116). An existing label is the normal case. The repository documents this itself: `implement_handle_guard_block.sh:30-33` ("gh_retry does not treat it as permanent") and `review_rb_judge.sh:916-923`. 157 `ensure_label_exists` / `set_issue_phase_label_resilient` call sites across 26 files go through these paths.
- **Current → proposed calls:** 5 POSTs and about 15 s per ensure on an existing label → 1 POST and 0 s.
- **Recommended fix:** Make the one-line root-cause change in `scripts/gh_helpers.sh:194` (tab-indented, braces on a new line):
  ```bash
  _is_gh_permanent_failure()
  {
  	printf '%s' "$1" | grep -qiE "…existing alternation…|already[ _-]*exists|already_exists"
  }
  ```
  This also covers `gh_retry_to_file` (1184) and `_gh_api_classify` (870). Callers that grep stderr for "already exists" (`label_helpers.sh:188`, `orchestrate_poll_process.sh:4050`) still match, because gh_retry echoes the escaped stderr. Then add a per-process cache like `_ENSURED_LABELS_CACHE` to `label_helpers.sh`; see DUP-001.

**API-002 — `curl_gh_api` and `gh_api_json_to_file` retry permanent 4xx failures**
- **File / lines:** `scripts/gh_helpers.sh:1271-1335`, `1350-1403`. Callers include `scripts/workflow_failure_heal_intake.sh:479, 498, 505, 730, 1121, 1145, 1794`; `scripts/workflow_failure_heal_report.sh:110, 140, 235`; `scripts/check_failure_triage.sh:462, 601`; `scripts/tg_helpers.sh` (6 refs).
- **Severity:** Medium · **Category:** `api-redundancy`
- **Description:** `gh_retry` and `gh_retry_to_file` stop at once on `_is_gh_permanent_failure` (1099, 1184). `gh_api_json_to_file` (1303-1322) and `curl_gh_api` (1380-1395) do not: a 404, 401 or 422 gets exponential backoff for all 5 attempts. Expected 404s do happen. For example, `workflow_failure_heal_intake.sh:505` compares `${HEAD_SHA}...${BIND_PR_HEAD}` after the PR head moved, which can be a force-push, and the forged-report path at 479 → `issue_not_found`.
- **Current → proposed calls:** 5 calls and about 15 s per permanent failure → 1 call.
- **Recommended fix:** In `gh_api_json_to_file`, add the same `_is_gh_permanent_failure "${stderr_content}"` early return before the rate-limit branch. In `curl_gh_api`, treat `4xx` other than `403`/`408`/`429` as permanent. The model to follow is `_gh_api_classify`, lines 877-880.

**API-003 — Validation-failure arms read tracking labels twice and edit labels up to three times**
- **File / lines:** `scripts/orchestrate_poll_process.sh:14799-14810` (harness_error), `14846-14851` (deterministic), `14884-14886` (recovery), `14907-14912` (exhausted); helper at `4060-4131`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** Each terminal arm calls `get_issue_labels_json` (read #1), then `set_tracking_phase_label` → `set_issue_phase_label`, which reads the labels again (4091, read #2) and makes one batched edit. It then makes two more single-label edits: `--remove-label "ai:validate-failed"` and `--remove-label "ai:harness-broken"` (the harness arm does a remove and then a separate `--add-label` at 14810). Those removes run even when the label is absent, which is the usual case. The `>/dev/null || true` variants at 14803, 14850, 14885 and 14911 also leak gh_retry warnings into the log, unlike their `2>&1` siblings.
- **Current → proposed calls:** 5 per arm (2 reads + 3 edits) → 2 (1 read + 1 combined edit); 0 extra edits when neither label is present.
- **Recommended fix:** Extend `set_issue_phase_label` with optional trailing arguments, `set_issue_phase_label <issue> <phase> [preloaded_labels_json] [extra_remove_csv]`. It would reuse `_tracking_labels` where available and append `--remove-label` only for labels in that list, inside the existing single `edit_args` call (the 4093-4094 pattern). Existing two-argument callers keep their current behaviour (§10).

**API-004 — Per-row REST reads in `security_pass_unblock_filed_advisory_followups`**
- **File / lines:** `scripts/orchestrate_poll_process.sh:7916-7964` (GET at 7921, comments at 7934)
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** For each unchecked follow-up row (up to 100, capped by `.[-100:]` at 7780), the loop makes one `gh api repos/…/issues/<n>` read for state and labels. It makes a paginated comments read when the issue is `ai:blocked`. The audit comment at 7891-7895 rules out reusing existing caches but does not consider batching.
- **Current → proposed calls:** N issue GETs + B comment reads → ceil(N/25) aliased GraphQL calls + B comment reads, made only when the marker is not in the last 20 comments `[NEEDS VERIFICATION]`. Comments must stay complete before a POST, because the marker check is the dedup key.
- **Recommended fix:** Reuse `_fetch_blocked_issue_routing_graphql` (`orchestrate_poll_process.sh:115-142`). It already returns `labels` and the last 20 comments per issue, uses aliases in batches of 25, and fails open. Add an `issue.state` field. Fall back to the current per-row path when the batch fails.

**API-005 — Reclarify replay re-reads every queued issue, and two `rate_limit` reads run back-to-back**
- **File / lines:** `scripts/orchestrate_poll_process.sh:44, 46-47, 55, 157, 209-211`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** `replay_failed_reclarify_commands` lists queued issues (46-47, `--json number`) and then GETs each one again (55) to confirm it is open and labelled. The design comment at 39-41 makes this live read deliberate `[NEEDS VERIFICATION]`. In the sweep-only path the function calls `gh api rate_limit` (44) and then `detect_unrouted_blocked_comments` calls it again (157). `rate_limit` does not debit the REST quota, but it is still a round trip.
- **Current → proposed calls:** 2 + N×(1 + P) → 1 + N×(1 + P) by caching the first rate_limit value. Going down to 1 + N×P would mean adding `state,labels` to the list call and dropping line 55, but only if the TOCTOU window is acceptable.
- **Recommended fix:** Pass the budget value from the first call into `detect_unrouted_blocked_comments` through an env var with a default (§8). Make the per-issue GET change only after confirming the TOCTOU requirement.

**API-006 — Inline `gh_retry` copies in `review_autofix.yml` ignore rate-limit resets, permanent failures and the single-attempt POST rule**
- **File / lines:** `.github/workflows/review_autofix.yml:795-817` (fallback), `1144-1166` (unconditional); POST-bearing callers in that step include 1388 and 1428
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** These copies make 4 attempts with fixed 2/4/8 s delays. They make no `_is_gh_rate_limit` wait-for-reset, have no permanent-failure exit, and do not honour `_gh_cmd_is_unsafe_post` (`gh_helpers.sh:804-816`, the issue #6634 rule). The step at 1119 runs after "Bootstrap GitHub API retry helper" (331-379), which already publishes `GH_API_RETRY_HELPERS` to `$GITHUB_ENV` (376).
- **Current → proposed calls:** up to 4 calls per 404/422 → 1.
- **Recommended fix:** In the 1144 step, `source "${GH_API_RETRY_HELPERS}"` when the file exists, and keep the inline copy only as a fallback (the guard pattern at 787-790).

**API-007 — Hand-rolled retry loop re-downloads the full comment history and ignores rate-limit resets**
- **File / lines:** `.github/workflows/implement.yml:2158-2181`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** The loop runs `gh api --paginate …/comments` up to 5 times with linear 2-10 s sleeps. It retries both failures and "approval comment not yet visible". A rate-limited 403 gets the same short sleeps instead of waiting for the reset.
- **Current → proposed calls:** up to 5×P → about P per attempt, with reset-aware waits.
- **Recommended fix:** Wrap the fetch in `gh_retry_to_file "${ISSUE_COMMENTS_FILE}.raw" gh api --paginate …`, which already exists and is staged as `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/gh_helpers.sh` elsewhere in this job. Keep the outer loop only for the visibility check.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — Three full `ensure_label_exists` implementations plus inline fallbacks**
- **File / lines:** `scripts/label_helpers.sh:161-195`; `scripts/orchestrate_poll_process.sh:4003-4058` (contract-driven, cached, always returns 0); `scripts/validate_process.sh:1222-1256` (contract-driven, uncached, alerts via `tg_notify`); fallbacks at `scripts/review_rb_judge.sh:924-938`, `scripts/review_autofix_step_mark_ready_to_merge.sh:15-29`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:10-18`, `.github/workflows/review_autofix.yml:1168-1171, 1586-1590, 4411-4420`; the best-behaved variant is `scripts/implement_handle_guard_block.sh:35-66`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** The copies differ on retry behaviour (API-001), caching, return code, and where they get colour and description from (hardcoded catalog vs `label_contract.v1.json`). The review-blocked fallbacks hardcode the review-blocked colour and description for every label name.
- **Recommended fix:** Make `scripts/label_helpers.sh` the owner. Keep the signature `ensure_label_exists <label_name> [repo]` (§10). Adopt `guard_block_ensure_label`'s shape: one raw `gh label create` attempt, return 0 on "already exists", and retry only unexpected errors. Add a process-local `declare -gA _LABEL_HELPERS_ENSURED_CACHE`, checking the name is unique first. In `orchestrate_poll_process.sh`, keep a thin `ensure_label_exists` wrapper that calls the shared one and always returns 0, so `set -e` callers such as 4065 are unchanged. `validate_process.sh` would source `label_helpers.sh`.

**DUP-002 — `set_issue_phase_label` and `set_tracking_phase_label` are near-identical**
- **File / lines:** `scripts/orchestrate_poll_process.sh:4060-4131`; `scripts/validate_process.sh:1258-1331`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** About 60 shared lines: ensure the label, `ai_labels.py resolve-phase`, read current labels, build `--remove-label`/`--add-label` arguments, make one `gh issue edit`, tolerate missing labels with the same regex (`could not remove label:|['\"]…['\"] not found`). Only the issue variable and the error-file handling differ.
- **Recommended fix:** Add `set_issue_phase_label_from_contract <issue_number> <phase_label> [contract_file] [preloaded_labels_json]` to `scripts/label_helpers.sh`, checking the name is unique first. Keep `set_issue_phase_label` and `set_tracking_phase_label` as one-line wrappers (§10). This is also the natural home for the API-003 parameters.

**DUP-003 — "Bootstrap GitHub API retry helper" block copied into 5 workflows**
- **File / lines:** `.github/workflows/review_autofix.yml:331-379`, `implement.yml:~150-180`, `validate.yml:~140-170`, `security-audit.yml:~95-125`, `orchestrate_clarify_respond.yml:~105-135` (anchored on the identical `ghr_auth=` lines 355 / 157 / 146 / 101 / 111)
- **Severity:** Medium · **Category:** `duplication`
- **Description:** Each copy has the same ref-pin regex, git-transport fetch, 3-try loop and `GH_API_RETRY_HELPERS` export. It cannot move to `scripts/`, because it is the step that fetches `scripts/`.
- **Recommended fix:** Create a composite action, `.github/actions/bootstrap-gh-retry/action.yml`, following the existing `.github/actions/setup-runtime` pattern. Inputs: `gh_token`, `workflow_job_json`. Output: `helpers_path`. Each workflow keeps its step `name:`/`id:`/`continue-on-error:` (§24). Apply SEC-001 there once. Moving 5 × ~45 lines would also free bytes under the §24 guard.

**DUP-004 — "Resolve workflow support ref" + checkout/fallback/ensure sequence copied 16 times**
- **File / lines:** `support_ref_workflow_ref="$(jq -r '.workflow_ref` appears 16 times in 10 workflows, for example `sync_ai_labels.yml:37-92`, `cancel_on_pr_close.yml:25-83`, `orchestrate_poll.yml` (×2), `implement.yml` (×3), `plan.yml` (×2), `clarify.yml` (×2)
- **Severity:** Low · **Category:** `duplication`
- **Description:** Every copy repeats the same pin regex (with only the workflow filename changed), the stable→main fallback and the SHA-identity check.
- **Recommended fix:** A composite action, `.github/actions/resolve-support-ref`, with a `workflow_file` input and `script_ref` output. It runs before checkout, so a script under `scripts/` cannot own it.

**DUP-005 — 61 one-line `gh_retry() { "$@"; }` passthrough fallbacks across 12 workflows**
- **File / lines:** e.g. `.github/workflows/cancel_on_pr_close.yml:127`, `implement.yml:6347, 6373, 6425` (implement.yml ×13, plan.yml ×13, review_autofix.yml ×12, clarify.yml ×6, …)
- **Severity:** Low · **Category:** `duplication`
- **Description:** The fallback silently drops retries and also the issue #5495 stdout-buffering contract. With the fallback, `$(gh_retry gh api …)` can capture error JSON. Add the two full re-implementations from API-006.
- **Recommended fix:** Once DUP-003 exists, make each step `source "${GH_API_RETRY_HELPERS}"` first. If no helper is available, use a fallback that buffers stdout, a five-line function equivalent to `_safe_gh_jq`, instead of the bare passthrough.

**DUP-006 — Long, repeated `if:` guard conjunctions**
- **File / lines:** `.github/workflows/review_autofix.yml:3480, 3517, 3550, 3583` (identical), with variants at 3556 and 3727; `.github/workflows/implement.yml:6433, 6529, 6649` (identical)
- **Severity:** Low · **Category:** `duplication`
- **Description:** The same 6-7 term `env.PR_CLOSED != 'true' && env.AUTOFIX_STALE_BASE_SKIP != 'true' && … && env.AUTOFIX_PRE_REVIEW_RESOLVE != 'true'` (review_autofix) and the six-output destructive/scope/rebase guard (implement) are pasted into each step. Adding a new gate means editing every copy.
- **Recommended fix:** Compute one flag per job once (for example `AUTOFIX_MAIN_PATH_ACTIVE=true|false` written to `$GITHUB_ENV`, checking the name is unique first) and test `env.AUTOFIX_MAIN_PATH_ACTIVE == 'true'`. This also saves workflow bytes toward the §24 headroom.

**DUP-007 — Phase-wait loops in the release smoke test each re-implement the same polling skeleton**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml` Phase 1 (`800-940`), Phase 2 (`954-1229`), Phase 3b (`1261-1552`), Phase 4 (`1821-2552`), Phase 6 (`3423-3846`); each sets `RATE_LIMIT_BACKOFF=0` (815, 969, 1283, 1903, 3582)
- **Severity:** Low · **Category:** `duplication`
- **Description:** Each phase duplicates the inactivity timer, state-change detection, the `capture_run_id` 5-try wrapper and the dead rate-limit guard (DEAD-001).
- **Recommended fix:** Add `wait_scoped_phase_capture_run_id <name_re> <created_after> <title> <max_pages>` next to `find_latest_scoped_run_field` in `scripts/comprehensive_test_and_release_gh_api.sh`. Move the phase bodies into `scripts/test_and_mark_stable_step_<slug>.sh`, the §24 pattern already used for `review_autofix_step_*.sh`. This also resolves EXPR-001, EXPR-002 and EXPR-004.

### Section 4: Expression Size Limit Risk Assessment

The CI guard (`tests/test_workflow_file_size_limit.py:31-32, 84-97`) fails any `run:` body that contains `${{` and is 18,000 characters or longer. That makes the High band (>18,000) unreachable while CI is green. No workflow uses `actions/github-script`. The largest `if:` expressions seen are about 600 characters (`review_autofix.yml:3779`, `implement.yml:6433`), which leaves about 20,400 characters of headroom.

**EXPR-001 — Phase 2 wait loop is probably in the Medium band `[NEEDS VERIFICATION]`**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:962-1229`; interpolations at 980, 992 and 1010 (`${{ steps.create-issue.outputs.created_after }}`)
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** The body is about 267 lines. At the observed width of about 60 characters per line after stripping indentation, that is an estimated **~16,000 characters**: about 5,000 below the 21,000 limit and about 2,000 below the CI guard. The step has grown by comment-heavy edits (issues #4723 and #4730).
- **Recommended fix:** Move `created_after` into the step's `env:` (`PLAN_CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}`, checking the name is unique first) and reference `${PLAN_CREATED_AFTER}`. The body then has no `${{` and is no longer evaluated as one expression. Over time, extract it as in DUP-007.

**EXPR-002 — Phase 3b wait loop is probably in the Medium band `[NEEDS VERIFICATION]`**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1269-~1551`; interpolation at 1307 (`${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`)
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** About 282 lines, estimated at **~15,500-17,500 characters**, with about 3,500-5,500 of headroom to the limit. It can only be below 18,000 because CI is green.
- **Recommended fix:** Same as EXPR-001: add `IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` to `env:` and delete line 1307.

**EXPR-003 — Smaller interpolated bodies (Low)**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:~810-940` (Phase 1, interpolation at 848, est. ~8,000 characters); `4548-4713` ("Verify all phases passed", 16 inline `${{ steps.*.outputs.* }}` at 4564-4690, est. ~9,000-10,000 characters)
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** Both are under 71%. The verify step grows by one inline output for each new phase.
- **Recommended fix:** Move the step outputs into `env:` (`HOT_POLLER_GUARD_STATUS: ${{ steps.hot-poller-guard.outputs.status }}`, …).

**EXPR-004 — Very large non-interpolated bodies are one edit away from failing (Low)**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1821-2552` (Phase 4, about 730 lines) and `2622-3292` (Phase 4b, about 670 lines). They currently use `${{` only in `env:` (1824-1855, 2629-2636).
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** These bodies are exempt today. Each is an estimated 35,000-45,000 characters, so one inline `${{ }}` would put it at about twice the limit. The CI guard catches that, but only after the PR is written.
- **Recommended fix:** Extract both bodies to `scripts/` as in DUP-007. This also gives the largest workflow file the most §24 byte headroom.

**EXPR-005 — Workflow file size headroom (Low) `[NEEDS VERIFICATION]`**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml` (7,714 lines), `implement.yml` (6,961), `review_autofix.yml` (5,728)
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** The 480,000-byte guard (`tests/test_workflow_file_size_limit.py:27`) keeps every workflow far below the 800 KB and 1 MB thresholds; none can exceed 800 KB while CI is green. §24 also asks for 50,000+ bytes of headroom. These three files are the largest by line count, and their byte sizes could not be measured here.
- **Recommended fix:** Run `wc -c` on all three. Wherever headroom is under 50,000 bytes, apply EXPR-004, DUP-003 and DUP-006 first, because they remove the most bytes.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — `RATE_LIMIT_BACKOFF` skip guards can never fire, and rate-limited polls reset the inactivity timer**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:887, 1082-1084, 1413-1416, 2166, 3732`; helper `scripts/comprehensive_test_and_release_gh_api.sh:109-135`
- **Severity:** Low · **Category:** `dead-code`
- **Description:** `gh_api_safe` updates `RATE_LIMIT_BACKOFF` (115, 126-130), but every caller invokes it inside `$(gh_api_safe_print …)`, which is a subshell. The parent's value stays at the `0` set at 815/969/1283/1903/3582, so `[ "$RATE_LIMIT_BACKOFF" -gt 0 ]` is always false. Side effect: when the rate limit is exhausted (122-124, which returns 1 without output), `LABELS` becomes empty, so `CUR_STATE` changes and `LAST_ACTIVITY` resets (1110-1116). A rate-limited phase therefore looks active and never reaches its inactivity timeout.
- **Recommended fix:** Test the call's exit status instead, for example `if ! LABELS=$(gh_api_safe_print …); then sleep "$POLL_INTERVAL"; continue; fi`, and delete the dead guards.

**DEBT-001 — Comment about `gh_retry` and label creation is stale**
- **File / lines:** `scripts/review_rb_judge.sh:916-923`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:** The comment says to avoid `gh_retry` for `gh label create` "Same rationale as `ensure_label_exists` in `scripts/orchestrate_poll_process.sh`". That function does wrap the call in `gh_retry` (`orchestrate_poll_process.sh:4037`). The "~31 s" figure also does not match the 15 s that follows from `gh_helpers.sh:1066, 1116`.
- **Recommended fix:** Fix the comment when API-001 lands. After that fix, the reason for the workaround no longer applies.

**CONSIST-001 — `gh_api_safe` writes diagnostics to stdout, unlike every `gh_helpers.sh` helper**
- **File / lines:** `scripts/comprehensive_test_and_release_gh_api.sh:151-155`
- **Severity:** Low · **Category:** `consistency`
- **Description:** This is the root cause of BUG-001, and callers have to work around it (`test-and-mark-stable.yml:1000-1002`). `gh_helpers.sh` sends all diagnostics to stderr (for example 1100-1103 and 1132-1135).
- **Recommended fix:** Change it to `echo "::error::gh api call failed: $*" >&2`. First check every `gh_api_safe_print` consumer, including `scripts/dispatch_and_watch_workflow_run.sh`, for reliance on the stdout line `[NEEDS VERIFICATION]`.

**CONSIST-002 — Single-attempt mutations bypass `gh_retry` even though it already handles them**
- **File / lines:** `scripts/orchestrate_poll_process.sh:7769` (`gh issue create`), `7951` (`gh api …/comments -f body=`)
- **Severity:** Low · **Category:** `consistency`
- **Description:** Both comments say "do not retry this non-idempotent mutation". `gh_retry` already makes a single attempt for `issue create` and for field-bearing `gh api` POSTs (`gh_helpers.sh:804-816`, 1067-1069). Calling `gh` directly also skips `_gh_rate_limit_trip_breaker` (1111) and the Telegram rate-limit alert.
- **Recommended fix:** Change both to `gh_retry gh …` with the same flags. Behaviour stays at one attempt.

**CONSIST-003 — `review_collect_pr_metadata.sh` shadows the shared `gh_retry` with a different signature**
- **File / lines:** `scripts/review_collect_pr_metadata.sh:33-35, 69-74`
- **Severity:** Low · **Category:** `consistency`
- **Description:** After sourcing `gh_helpers.sh`, the script redefines `gh_retry <outfile> <gh args…>`. Any `gh_helpers.sh` function called later in this process that uses the shared form `gh_retry gh api …` (for example `gh_review_pr_state`, `gh_helpers.sh:91, 107`) would write to a file named `gh`. The only helper the script uses today, `extract_repo_scoped_issue_refs_from_text` (2746-2761), is text-only, so this is latent. It also breaks the §10 rule that new identifiers must be unique.
- **Recommended fix:** Add a uniquely named wrapper, for example `review_collect_gh_to_file()`, and update the 8 call sites at 138, 194, 231, 235, 240, 247 and 280. Leave the shared `gh_retry` intact.

**SHELL-001 — Unquoted numeric test operands (SC2086)**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1220` (`if [ $IDLE -ge $INACTIVITY_LIMIT ]`), with the same form in the sibling phase loops
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** This is harmless today because both values come from arithmetic. It is the only unquoted form in these otherwise quoted loops.
- **Recommended fix:** Use `[ "${IDLE}" -ge "${INACTIVITY_LIMIT}" ]`.

**TODO/FIXME/HACK markers:** none. A word-bounded search for comment markers found no matches in `scripts/` or `.github/workflows/`; the only hits were `XXXXXX` `mktemp` templates.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | API-001 |
| Medium | 9 | BUG-001, API-002, API-003, API-004, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-002 |
| Low | 20 | BUG-002, BUG-003, BUG-004, SEC-001, API-005, API-006, API-007, DUP-004, DUP-005, DUP-006, DUP-007, EXPR-003, EXPR-004, EXPR-005, DEAD-001, DEBT-001, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/gh_helpers.sh` (`_is_gh_permanent_failure`), `.github/workflows/review_autofix.yml` (inline `gh_retry` at 1144), `scripts/review_rb_judge.sh` (comment) | Small |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/review_autofix.yml`, `.github/workflows/implement.yml` | Medium |
| Code modularization | `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, `scripts/review_autofix_step_mark_ready_to_merge.sh`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh`, new `.github/actions/bootstrap-gh-retry/` and `.github/actions/resolve-support-ref/`, 10+ workflows | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml` (env moves); new `scripts/test_and_mark_stable_step_*.sh` for extraction | Small |
| Medium/Low fixes | `.github/workflows/test-and-mark-stable.yml`, `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/cancel_on_pr_close.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_collect_pr_metadata.sh`, 5 workflows carrying the bootstrap block | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-21)

### Safety Tag Legend
`SAFE_TO_MERGE` means static reading proves every precondition. The calls hit the same endpoint, or one fetches a strict subset of the other's fields. Pagination, filters and token are the same. Both sites are in the same function or step, with no mutation between them. Error handling and retry behaviour do not change, no job or concurrency boundary is crossed, and any documented cache contract is kept. The implement stage may act on these without further review. `NEEDS_VERIFICATION` means the change looks right, but at least one precondition cannot be proven from source alone; run the listed checks first. `RISKY_SKIP` means at least one hard trigger applies: a retry or backoff loop, an auth or identity probe, pagination, a rate-limit path, code in `orchestrate_poll_process.sh` or a judge, an explicit race defence, or a change to log keys. These are listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001: Two helpers query the same open `ai/issue-<n>` PRs separately** — `RISKY_SKIP`
- **Files / lines:**
  - `scripts/orchestrate_poll_process.sh:4488-4495` (`_linked_prs_by_branch_name`)
  - `scripts/orchestrate_poll_process.sh:4632-4655` (`_resolve_linked_pr_fresh_by_branch`)
  - Callers of the first helper: `_find_all_linked_prs` (4539), which `close_linked_pr` calls (16005); `_resolve_issue_implementation_pr` (18810); line 24844.
  - Callers of the second helper: `_check_fresh_push_guard_with_fallback` (19040); line 28054.
- **Current → proposed calls:** up to 2 `gh pr list` calls per issue when the stall-freshness fallback and close/recovery both hit the same issue in one cycle → 1, using a per-cycle cache keyed by issue number.
- **Endpoint:** `gh pr list --head ai/issue-<n> --state open`. This is one PR-list query each time; the first asks for `number`, the second for `number,commits`.
- **Evidence:** the filters are identical. Only the requested fields differ, and the first call's fields are a strict subset of the second's:
  ```bash
  gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" \
  	--head "ai/issue-${issue_num}" --state open \
  	--json number --jq '.[].number' 2>/dev/null
  ```
  ```bash
  prs_json="$(gh_retry gh pr list --repo "${GITHUB_REPOSITORY}" \
  	--head "ai/issue-${issue_num}" --state open \
  	--json number,commits 2>/dev/null || echo '[]')"
  ```
  The §15 comment at 4616-4623 already says the second helper "reuses the existing branch-lookup pattern". Neither helper caches its result.
- **Proposed fix:** add a cycle-local associative cache. The name must be checked as unique first; `_BRANCH_OPEN_PR_LIST_CACHE` is one option. Fill it in `_resolve_linked_pr_fresh_by_branch` with the raw `number,commits` JSON. Have `_linked_prs_by_branch_name` read `.[].number` from the cache before making its own call. Document the contract the way `STALL_MANAGED_LINKED_PR_CACHE` is documented.
- **Safety rationale:** both calls sit in `orchestrate_poll_process.sh` on stall-recovery paths, and their error handling differs. `_linked_prs_by_branch_name` passes its exit code through, and 18810 captures it as `_ripr_branch_discovery_rc`. `_resolve_linked_pr_fresh_by_branch` swallows failures (`|| echo '[]'`). Merging them would change error handling.
- **Downstream signal:** do not auto-implement. A maintainer must first:
  1. confirm from poll logs (`STALL_FRESH_PUSH_FALLBACK` lines next to `close_linked_pr` activity) that both helpers really run for the same issue in one cycle;
  2. decide whether a cached failure should be returned to the exit-code-sensitive caller at 18810;
  3. accept that number-only callers would then get the heavier `commits` payload on a cache miss.

**MERGE-002: Auto-merge enablement reads labels and PR metadata in separate calls** — `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_enable_auto_merge.sh:155` (paginated labels read)
  - `scripts/review_enable_auto_merge.sh:221` (full `/pulls/{n}` read)
  - `scripts/review_enable_auto_merge.sh:100` (`reviewed_head_is_current_for_labels`, a third `/pulls/{n}` read that only runs on the e2e early-exit path at 158)
- **Current → proposed calls:**
  - Non-e2e path: 1 labels read (P pages) + 1 PR read → 1 PR read.
  - e2e path: P label pages + 1 PR read (at 100) → 1 PR read.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}/labels?per_page=100` (paginated) and `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** the full `/pulls/{n}` response stored in `_ORCH_PR_META_JSON` also carries `.labels[]`, `.head.sha` and `.head.ref`. The script reads labels first, and then does its own PR read for the head check:
  ```bash
  if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
  ```
  ```bash
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ```
- **Proposed fix:** move the 221 read above the e2e guard. Take the `e2e-smoke-test` check from `.labels[].name` in `_ORCH_PR_META_JSON`. Let `reviewed_head_is_current_for_labels` take a preloaded head SHA from the same response. Keep the separate `::warning::` texts for "labels" and "PR metadata" failures.
- **Safety rationale:** the labels call is paginated on purpose. The comment at 144-153 records a production gap where labels on page 2 were dropped. The guard fails closed against an e2e race, and reordering would change which `::warning::` line appears on failure.
- **Downstream signal:** do not auto-implement. A maintainer must first confirm, against the GitHub REST reference and a live PR carrying more than 30 labels, that `pulls/{n}` returns the complete `labels` array without truncation. They must also check that no test or log consumer greps for the order of the "Could not fetch labels" and "Could not fetch PR metadata" warnings.

**MERGE-003: Post-merge validate dispatch reads labels one issue at a time on the PR-text fallback path** — `NEEDS_VERIFICATION`
- **Files / lines:** `.github/workflows/review_autofix.yml:889-928` (loop) and `:893` (call), job `post-merge-validate-dispatch` (728).
- **Current → proposed calls:** N `gh issue view --json labels` calls, where N is the number of issue refs found in the PR title and body → ceil(N/25) aliased GraphQL calls.
- **Endpoint:** `gh issue view <n> --json labels`, one GraphQL query per issue.
- **Evidence:** `labels_known` is false only when the linked issues came from the title/body fallback, which sets `labels: null` at 882:
  ```bash
  if [ "${labels_known}" != "true" ]; then
    if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
  ```
  The other paths already batch. The gate's `post_merge_linked_issues_json` and the live `closingIssuesReferences` GraphQL call at 835-841 both return labels for every issue in a single call.
- **Proposed fix:** before the loop, collect the numbers where `labels_known` is false and run one aliased query. Follow the shape of `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh:18216`). The closest existing label-only helper is `_fetch_issue_labels_batch_graphql` (4331-4355): batches of 25, incomplete label pages left out, and missing keys treated as cache misses. Put the helper under `scripts/`, not inline (§24). Keep the per-issue `gh issue view` as the fallback for any issue the batch leaves out.
- **Safety rationale:** the endpoint and filter match, but two things cannot be proven statically.
  - Error handling: today one failed issue sets `validation_label_unknown=1` and only that issue is skipped. A failed batch must fall back per issue to keep that behaviour.
  - Ordering: the loop makes mutations between iterations, `gh workflow run` at 916 and `gh issue edit --remove-label` at 925. A label read up front would come before them.
- **Downstream signal:** before implementing, verify:
  1. that a dispatched validate run cannot add or remove `ai:orchestrator-validate-required` on a *different* linked issue before the loop reaches it (read `validate.yml` and `scripts/validate_process.sh` for writes to that label);
  2. that the batched query checks `labels.pageInfo.hasNextPage`, the same way `review_rb_judge.sh:1179-1182` does.
  
  Impact is low because this path only runs when `closingIssuesReferences` is empty.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: The single-issue security-pass gate repeats the `gh api user` lookup the `gate` job already exports** — `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_single_issue_security_pass.sh:369-373` (the repeat lookup)
  - `scripts/review_autofix_step_evaluate_gate.sh:334` (the original lookup) and `:1371` (where it is written as an output)
  - `.github/workflows/review_autofix.yml:328` (job output), `:1787-1788` (`codex-agent` has `needs: gate`), `:4151-4179` (the security-pass step inside `codex-agent`)
- **Current → proposed calls:** 1 `GET /user` per security-pass gate run → 0 when the gate output is non-empty; otherwise the live call stays as the fallback.
- **Endpoint:** `GET /user`.
- **Evidence:** the comment at line 369 says the identity is "not exported to this job":
  ```bash
  # The gate job also probes /user, but its identity is not exported to this job.
  SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
  ```
  In fact it is exported as a job output, and other jobs already read it, at `review_autofix.yml:1500` and `:5665`:
  ```yaml
  fingerprint_cap_marker_author_login: ${{ steps.evaluate.outputs.fingerprint_cap_marker_author_login }}
  ```
  It is just not passed into this step's `env:`.
- **Proposed fix:** add an env var with a default (§8), for example `SECURITY_PASS_AUTHOR_LOGIN_PRELOADED: ${{ needs.gate.outputs.fingerprint_cap_marker_author_login }}`, after checking the name is unique. In `single_pass_gate`, use it when non-empty and fall back to `gh api user` when empty. Correct the comment at 369.
- **Safety rationale:** this is an identity probe. The login decides which marker comments are trusted. It also crosses a job boundary with different tokens: the gate step uses `secrets.GH_PAT` (`review_autofix.yml:595`), the security-pass step uses `secrets.GH_PAT || github.token` (4166). And the gate only does the lookup lazily, through `gate_fetch_marker_comments` (411, 621, 657, 841, 1218, 1294), so the output is often empty.
- **Downstream signal:** do not auto-implement. A maintainer must confirm three things:
  1. A gate output of `""` always falls through to the live probe.
  2. When `GH_PAT` is unset, the `SECURITY_PASS_AUTHOR_LOGIN_FALLBACK` (`github-actions[bot]`) behaviour at 4176 is unchanged.
  3. A login trusted from another job is acceptable for authenticating `single_pass_markers`.

  The saving is 1 non-quota-heavy call per review, so this is a trust-model decision more than a budget one.

**REUSE-002: The rb judge's final-attempt `fix` → merge path re-reads PR state the guard read already returned** — `RISKY_SKIP`
- **Files / lines:** `scripts/review_rb_judge.sh:1927-1940` (guard read, `_guard_pr_meta`, unset at 1940) and `:2345` (`fix)` branch when `IS_FINAL=true`).
- **Current → proposed calls:** 2 `GET /pulls/{n}` → 1 on the `fix` final-attempt path.
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** the guard read is a full payload, and only `merged` and `head.sha` are kept from it (1928, 1936). The later read asks again for `.state`:
  ```bash
  PR_STATE="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state' 2>/dev/null | grep -xE 'open|closed' || echo "")"
  ```
- **Proposed fix:** at 1936, also capture `.state` from `_guard_pr_meta` into a new, uniquely named variable such as `RB_LIVE_PR_STATE`. Use it at 2345 instead of the second read.
- **Safety rationale:** the comment at 1917-1923 says the guard re-read exists to defend against an upstream race ("auto-merge … could complete asynchronously"). Between the two reads the script runs `rb_security_severity_block` (2002), `post_review_blocked_assessment` (2057) and the security-gate block (from 2066). That window is non-trivial, and a stale `open` would change whether `gh pr merge` is attempted. `--match-head-commit` limits the damage.
- **Downstream signal:** do not auto-implement. It needs manual review of whether the security-gate section (2066-2190) can wait, sleep or dispatch. If it can, keep the live read.

**REUSE-003: Calling the freshness gate a second time re-lists the paginated PR files for the same head** — `RISKY_SKIP`
- **Files / lines:**
  - `scripts/pr_checks_lib.sh:797-894` (`_pr_base_freshness`; files listing at 884)
  - First and second calls in `scripts/review_enable_auto_merge.sh`: 266 and 293. The second runs only when `PR_CHECKS_WAIT_WAITED_S > 0`.
  - First and second calls in `scripts/review_rb_judge.sh`: 2258 and 2271, in the `merge)` branch.
- **Current → proposed calls:** 2 compare + 2×P file pages → 2 compare + 1×P file pages, when both calls reach the overlap check (`ahead_by` > 0 and fewer than 300 base files).
- **Endpoints:** `GET /repos/{o}/{r}/compare/{head}...{base}` and `GET /repos/{o}/{r}/pulls/{n}/files?per_page=100` (paginated, `--slurp`).
- **Evidence:** both calls pass the same bound head (`INITIAL_HEAD_SHA` / `RB_JUDGED_HEAD_SHA`). The files listing runs every time:
  ```bash
  pr_files_json="$(gh_retry _safe_gh_jq --paginate --slurp "repos/${repo}/pulls/${pr_number}/files?per_page=100" 2>/dev/null || echo "{}")"
  ```
  The API-budget comment (786-792) counts one listing "per gated merge" but does not account for the re-check after the wait.
- **Proposed fix:** cache the listing in a process-local variable keyed by `${pr_number}:${head_sha}`, with a unique name. Reuse it only when the key matches. The `compare` call stays live.
- **Safety rationale:** the call is paginated, and the second freshness call exists to defend against the base moving during the checks wait (comment at `review_enable_auto_merge.sh:288-290`). GitHub computes `pulls/{n}/files` against the merge base, so a base push that changes the merge base could change the list.
- **Downstream signal:** do not auto-implement. A maintainer must confirm that, with the head fixed, the `pulls/{n}/files` result cannot change when the merge base moves. If it can, the second listing is load-bearing and must stay.

### Dead Calls (DEAD-API-###)
No findings. In every call site read in this pass, the response is used later, either as data or through its exit code. That includes `review_rb_judge.sh:1071` (state, merged, draft, base, title and body are all read), `review_enable_auto_merge.sh:100/155/221`, `unblock_judge.sh:1075/1113/1604/1766`, `review_single_issue_security_pass.sh:600/615/634`, `pr_checks_lib.sh:256/821/884`, `review_collect_pr_metadata.sh:231-247/280` and `review_autofix.yml:835-893`. Post-write reads such as `implement_handle_guard_block.sh:112/230/404` and `validate_process.sh:4501` are deliberate latch and label checks after a write, not dead calls.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — it changes `_is_gh_permanent_failure` (`gh_helpers.sh:194`, checked: the regex matches `HTTP 422` but no "already exists" text), which every `gh_retry` / `gh_retry_to_file` / `_gh_api_classify` retry loop uses. Before landing, record the stderr of `gh label create` on an existing label with the pinned `gh` version, and grep for every non-label "already exists" path the new pattern would make permanent.
- API-002: RISKY_SKIP — it changes the retry and backoff loops of `gh_api_json_to_file` and `curl_gh_api`. The `403` / `408` / `429` exclusions must stay identical to `_gh_api_classify`, so rate-limit handling cannot regress.
- API-003: RISKY_SKIP — the code is in `orchestrate_poll_process.sh` (checked: 14799 read, 14801 `set_tracking_phase_label`, 14803 / 14810 single-label edits). Extra detail: the preloaded `_tracking_labels` comes from the unpaginated `get_issue_labels_json` (4138-4141, BUG-002). A "remove only if present" filter built on it would silently skip `ai:validate-failed` when it sits past label 30. Land BUG-002 first.
- API-004: RISKY_SKIP — the code is in `orchestrate_poll_process.sh`, and the comment read is paginated and is the dedup key before a POST.
- API-005: RISKY_SKIP — `rate_limit` probes in `orchestrate_poll_process.sh` are a hard trigger.
- API-006: RISKY_SKIP — these are inline retry and backoff loops. Sourcing `GH_API_RETRY_HELPERS` also switches POSTs to single attempts (`_gh_cmd_is_unsafe_post`), which changes retry behaviour for callers such as 1388 and 1428.
- API-007: RISKY_SKIP — a hand-rolled retry loop around a paginated read (checked: `implement.yml:2165-2176`). The outer loop also retries "approval comment not yet visible", and that must stay separate from API-error retries.

### Summary Counts
These counts cover only the new findings in this section. The seven cross-referenced Deep Audit findings (API-001 to API-007) are all RISKY_SKIP and are listed above.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-003 |
| RISKY_SKIP | 5 | MERGE-001, MERGE-002, REUSE-001, REUSE-002, REUSE-003 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass. Every saving found sits behind pagination, a race-defence re-read, an identity probe, a job boundary, or `orchestrate_poll_process.sh`. MERGE-003 is the only candidate that could be promoted, once its two verification steps are done.