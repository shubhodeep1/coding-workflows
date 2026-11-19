# Workflow optimization report: shubhodeep1/coding-workflows, window ending 2026-11-19 09:10 UTC

## Executive Summary

- **Data gaps.** `shubhodeep1/coding-workflows` is listed in `coverage.repositories_truncated` (`page_limit_reached: true`), so only some of its runs are in the window (1,000 runs). No repository failed. The run-logs directory `/home/runner/work/_temp/workflow-log-output` was not mounted, so this report uses `analysis_context.json` (run rows and `log_summary`) and the repository code. `insufficient_data` is false.
- **The collector drained the shared PAT and then hit the rate limit 43 times (high confidence).** From 09:13:37 to 09:16:24 UTC, 43 jobs-list fetches failed with `API rate limit exceeded for user ID 11442166`. The bucket that pipeline jobs report in `GH_PAT_BUDGET … reset=1795079786` resets at 09:16:26 UTC, two seconds after the last error. That bucket fell from `remaining=2933` (44488256028, about 08:43) to `remaining=789` (44492505906, about 09:02) and then to 0.
  - `gh_api_json` in `scripts/collect_workflow_logs.py:533` treats "rate limit" as retryable and retries 3 times with 1-2 s backoff for every run. Nothing stops it after the first primary-limit 403.
  - Off-06:00 runs use `secrets.GH_PAT` (`workflow-log-analysis.yml:178`), the pipeline's PAT.
  - Fix: a primary-rate-limit breaker plus a remaining-budget floor (R1/G1). Impact: about 129 wasted requests per exhausted window, and the pipeline keeps its last few hundred calls.
- **Two cancelled review runs used 91% of all tokens in the window (high confidence on the numbers, medium on the cause).**
  - 44459756477 used 84,802,470 tokens and ran 14,018 s, close to the codex-agent `timeout-minutes: 240`. That it timed out is an inference.
  - 44447007564 used 28,502,880 tokens and was cancelled in "Run reviewer models" after 6,554 s.
  - Together that is 113.3M of 124.4M tokens. Neither "Run reviewer models" (`review_autofix.yml:3631`) nor "Apply fixes with editor model" (`:3972`) has a step `timeout-minutes`.
  - Fix: a cause line in the existing cancelled() step (C1), then a step cap (C2).
- **Final-CI merge-ref failures dropped from 15 of 21 (Nov 19 doc) to 4 of 12 CI failures (medium confidence).** The failures are 44445920977 (PR 9709), 44447795119 (PR 9687), 44455754191 (PR 9735) and 44480944167 (PR 9772). The outcome token (`stale` or `conflict`) still can't be read. Fix: parse `CI_FINAL_MERGE_REF` in the collector (R2).
- **The runner queue dominates wall-clock time (high confidence).** In 38 `log_summary` rows, jobs wait 1-31 minutes for a hosted runner. Examples: 44484740574 waited 31 min, 44488218380 26 min, and 44486417985 24 min, while its steps ran for 5 s. This is the §26 backlog. The smallest safe reductions are S1 (fold per-PR micro-jobs) and S2 (stop the repeated same-PR review dispatches).
- **A post-step error in check triage hides a real defect (high confidence).** In 44490437632, the post step logged `Can't find 'action.yml' … install-claude`. The cleanup at `check_failure_triage.yml:603-607` keeps only the `claude-pool-token` action directory, but `install-claude` (`:436`) also has a post step. The one-line fix is R3.

## Speed Optimizations

**S1. Fold the per-PR `Integration PR readiness check` job into `lint-pr-body-auto-close.yml`'s `lint` job.** This is a micro-optimization for queue pressure, not a critical-path win.
- **Evidence:**
  - In 44486633939 the readiness job waited about 19 min for a runner, ran about 5 s, and then printed "head ref 'claude/unblock-chain-depth-cap' is not an orchestrator/project-* branch — readiness check does not apply".
  - 44491603140 shows the same: a 5m47s wait, 4 s of work, and a no-op on head `claude/poller-sandbox-agents-budget`.
  - The lint job fires on the same PR events: 44486633908 waited about 22 min and ran about 11 s; 44491603100 waited 5.6 min and ran 5 s.
  - Window volume: readiness had 54 runs (32 ran), lint had 36.
- **Root cause:** both workflows run on `pull_request` against `[main, stable]` and each takes its own runner for about 5 s of work. Readiness must not be skipped (prior verdict 2026-10-07-3 Speed 1), but it can run as steps inside another job.
- **Exact change:** move the readiness steps into the `lint` job as extra steps and keep the same script.
  - The merged job needs `statuses: write` added. That is a §26 permission trade-off: the lint job currently has read-only scopes.
  - Do this only after confirming that neither job's check-run name is a required status check (§26 "Keep cited jobs"). The readiness gate is a commit-status context, not the job name. If either is required, leave them separate and record that.
- **Estimated savings:** one runner slot per PR `opened`/`synchronize`/`reopened`/`ready_for_review` event, about 30-50 slots per window. Each slot also carries about 30-60 s of runner setup.
- **Risk:** medium (permission widening and required-check naming).
- Finding check: new — no prior verdict covers folding. 2026-10-07-3 Speed 1 only rejected skipping readiness, and the fold keeps it running. Evidence: 44486633939, 44491603140, 44486633908.

**S2. Stop repeated review dispatches for the same old PRs.** This is a critical-path win for the queue.
- **Evidence:** review runs for the same PRs were dispatched in bursts and then cancelled.
  - 05:37-05:38 UTC: 44466476227 (9505), 44466496448 (9502), 44466511873 (9497) and 44466530404 (9456), each cancelled after 3,184-3,285 s.
  - Also at 05:37: 44466453099 (9573), cancelled inside "Run Codex resolver, validate, stage, commit" after 3,277 s.
  - 06:39: 44473712798 (9502) and 44473747795 (9456), about 3,240 s each.
  - 08:27-08:28: seven `review / gate` runs, all `cancelled_before_first_step` after 156-219 s: 44487757006 (9573), 44487779630 (9561), 44487802978 (9538), 44487827737 (9505), 44487847924 (9502), 44487867982 (9497) and 44487888515 (9456).
  - review_autofix totals in the window: 53 cancelled out of 154 runs.
- **Root cause (inference):** `internal-review.yml:49-51` uses group `internal-review-dispatch-pr-<n>` with `cancel-in-progress: false`, so each new dispatch replaces the older pending run in that group. The sweep already holds back while a run is `waiting_for_runner` (`review_autofix_sweep.yml:524`, threshold 120 min). The dispatcher behind these bursts could not be identified without logs; a repo search found PR-review dispatch callers only in the sweep, test-and-mark-stable and forward-merge.
- **Exact change:**
  1. For two of the runs above, read `triggering_actor` and the dispatch inputs with `gh run view <id> --json event,triggeringActor,displayTitle` (2 calls) to identify the dispatcher.
  2. Apply the sweep's existing `AUTOFIX_SWEEP_STALE_QUEUED … outcome=waiting_for_runner` suppression to that path through `_pr_named_review_dispatch_runs`.
- **Estimated savings:** about 13 superseded review runs per window, each of which sits in the queue (inference).
- **Risk:** low if it reuses the existing guard.
- Finding check: recurring — 2026-09-21-4 Speed 1 records same-PR review dispatch suppression as `already-implemented` (`_pr_named_review_dispatch_runs`, #4927). This window still shows three bursts for PRs 9456/9497/9502/9505.

## Cost Optimizations

**C1. Record why a review run was cancelled and how far it got, in the existing cancelled() step.**
- **Evidence:**
  - 44459756477: 84,802,470 tokens, 8 calls, 1,264,897 completion tokens, cache hit 0.9395, 14,018 s, cancelled. The jobs fetch failed, so the step is unknown.
  - 44447007564: 28,502,880 tokens, 7 calls, cancelled at "Run reviewer models" after 6,554 s.
  - Together that is 91.1% of the window's 124,436,339 tokens and 89.6% of its completion tokens.
- **Root cause:** the 14,018 s run matches the 240-minute job timeout plus gate time (inference). The second run was cancelled from outside while running (cause unknown). "Record review run failure in memory (fail-open)" (`review_autofix.yml:6208-6229`) records only `run_attempt`, so timeout, supersession and PR close look identical.
- **Exact change:** in that step's existing `run:` body, print one line and pass the same fields into `--metadata-json`, fail-open:
  ```bash
  echo "REVIEW_RUN_CANCELLED pr=${PR_NUMBER:-} run=${GITHUB_RUN_ID} attempt=${GITHUB_RUN_ATTEMPT} job_status=${JOB_STATUS:-unknown} elapsed_s=${elapsed_s:-unknown} job_timeout_min=240 cause=${cause:-unknown}"
  # cause=job_timeout_likely when elapsed_s >= 235*60, else external
  ```
  The new `REVIEW_RUN_CANCELLED` prefix needs an `agents.d/` fragment (§25).
- **Estimated savings:** none directly. It decides whether C2 caps the right step. Quality risk: none.
- Finding check: reopened — 2026-09-21-2 Reliability 3 rejected cancellation diagnostics because "runs cancelled before their first step cannot log, and those cancellations are the concurrency group's normal replacement of a pending run". 44447007564 was cancelled inside "Run reviewer models" after 6,554 s, so it could log, and it was not a pending-run replacement.

**C2. Give "Run reviewer models" its own `timeout-minutes`.**
- **Evidence:** as in C1. The step at `review_autofix.yml:3631` has no `timeout-minutes`, while the resolver has 170 (`:4454`) and the integration repair has 120 (`:3604`).
- **Root cause:** a reviewer fan-out that hangs or loops is bounded only by the 240-minute job cap, and the editor step after it gets no time.
- **Exact change:** add `timeout-minutes: ${{ fromJSON(vars.REVIEW_REVIEWERS_STEP_TIMEOUT_MINUTES || '90') }}` to the step. The default comes from the variable fallback. The workflow's existing failure path then marks the PR review-blocked (workflow failure), as it does for any failed step.
- **Estimated savings:** if C1 shows timeouts are the cause, up to about 60% of the 84.8M-token run, roughly 150 of 240 minutes (inference).
- **Quality risk:** medium. A legitimately slow multi-reviewer pass could be cut off. Set the default at or above the observed successful p95 of the reviewer step once C1 data exists.
- Finding check: new — no prior verdict on a reviewer step timeout; the current code has none (`review_autofix.yml:3631-3679`).

**Semble and Serena cost check.**
- Semble produced 5 `SEMBLE_QUERY` calls with 78,709 bytes (about 15.7 KB per query), 37 sources and `static_dup_bytes_total=0`, all in review_autofix.
- Per run that is about 4k tokens, against millions of prompt tokens (for example 4,586,968 in 44459756477). Semble is neither driving prompt growth nor adding noisy duplicate bytes.
- The 5 bootstraps took 207,528 ms in total (41.5 s each), none failed and none went unused. That is negligible next to review runs of 5,779-14,018 s.
- Serena is off (`SERENA_ENABLED: false` in 44489945764 and 44486418192), and all Serena counters are 0.
- No change is recommended.

## Reliability Improvements

**R1. Stop the collector's jobs-fetch loop at the first primary rate limit.**
- **Evidence:** 43 errors with `scope: jobs` and `API rate limit exceeded for user ID 11442166`, from 09:13:37 to 09:16:24 UTC. Because of them, 5 failing runs and many cancelled ones have `failure_point` null:
  - 44459871048 (Test & Mark Stable)
  - 44468836305, 44458994103, 44459557215 and 44476634913 (CI)
  - 44468253598 (CI quick)
- **Root cause:**
  - `RETRY_MARKERS` includes "rate limit" (`collect_workflow_logs.py:83-93`), so a primary-limit 403 is retried 3 times with 1 s and 2 s backoff, about 3.5 s per run, which matches the timestamps.
  - The jobs loop (`:2264-2283`) catches the error and moves on to the next run without a breaker.
- **Exact change** (inside `scripts/collect_workflow_logs.py`, no new workflow):
  - Treat stderr containing `api rate limit exceeded` without `secondary` as non-retryable in `gh_api_json`/`gh_api_bytes`.
  - Set a module-level `_PRIMARY_RATE_LIMITED` flag. While it is set, skip `list_jobs_for_run` with `jobs_fetch_status="skipped_rate_limited"`.
  - Emit one `::warning::COLLECTOR_RATE_LIMIT outcome=breaker_open scope=jobs skipped=<n>`.
- **Expected impact:** about 129 failed requests avoided in this window. Leftover budget stays with live pipeline jobs.
- **Fail-open:** rows keep the current `diagnostic_failure_reason` semantics; only the retry count changes.
- Finding check: new — no prior verdict on collector rate-limit handling; the current code retries primary limits (`collect_workflow_logs.py:533-536`).

**R2. Parse the `CI_FINAL_MERGE_REF` outcome per failed final-CI run.**
- **Evidence:** 4 of 12 CI failures stop at "Verify the final-CI merge ref is current":

  | Run | PR | Job | Duration |
  |---|---|---|---|
  | 44445920977 | 9709 | `tests-system-python (21)` | 4,446 s |
  | 44480944167 | 9772 | `tests-system-python (19)` | 4,173 s |
  | 44455754191 | 9735 | `tests-hooks-and-orchestrator` | 3,467 s |
  | 44447795119 | 9687 | `lint` | 1,422 s |

  Downstream, 44488218380 logged `FINAL_CI_MERGE_RESUME pr=9772 outcome=dropped reason=required_checks_failed`.
- **Root cause category:** unknown between `stale` and `conflict`. The script logs `CI_FINAL_MERGE_REF … outcome= reason= pin=` (`scripts/ci_final_merge_ref_fresh.sh:108`), but no consumer outside the script parses it.
- **Exact change:** in the collector's per-run log parsing, capture the last `CI_FINAL_MERGE_REF` line of the failing job into `final_ci_merge_ref_outcome`, `_reason` and `_pin`, and add a count by reason to the summary. This is collector-only, with no workflow change.
- **Expected impact:** separates PR conflicts (correct behaviour) from refresh or local-merge defects. Decides whether `CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS` or `_MERGE_DEPTH` need tuning.
- **Fail-open:** the field stays null when no line is present.
- Finding check: new — no verdict in `analysis/recommendation-processing-report.md`. `analysis/workflow-optimization-2026-11-19.md` R1 proposes the same change and is still unprocessed. Present in code: no parser outside the script.

**R3. Keep the `install-claude` action directory through check-triage cleanup.**
- **Evidence:** 44490437632 (success, 1,010 s) logged the post-job error `##[error]Can't find 'action.yml'… under '.codex-workflow-src/.github/actions/install-claude'`.
- **Root cause:** a code defect. `check_failure_triage.yml:603-607` deletes every `.github/actions/*` directory except `claude-pool-token`, but `install-claude` (used at `:436`) has a post step that runs at job end.
- **Exact change:** keep both directories:
  ```bash
  find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +
  ```
  Also widen the `[ -d …claude-pool-token ]` guard to cover `install-claude`.
- **Expected impact:** removes a false `##[error]` from every Claude-engine triage run and restores the npm-cache save on a cache miss.
- **Rollback:** a one-line revert.
- Finding check: new — no prior verdict; the cleanup code at `check_failure_triage.yml:607` is present.

**R4. Confirm whether 44468251148 was a §24 "workflow file issue" run.**
- **Evidence:** a run named `.github/workflows/orchestrate_poll.yml` (named after the file path) with conclusion `failure`, `duration_seconds=0` and a log archive that returns 404. `CI quick` 44468253598 failed in 48 s, one second later (05:53:30-31).
- **Root cause (inference):** the zero-duration, path-named, log-less run matches the §24 "workflow file issue" signature. It could also be an invalid YAML push on a branch.
- **Exact change:** run `wc -c .github/workflows/orchestrate_poll.yml` and `gh run view 44468251148 --json headBranch,headSha`. If the file is over 480,000 bytes, move `run:` bodies into `scripts/` as §24 requires.
- **Risk:** none; this is a check.
- Finding check: unverified — file sizes could not be measured (this run has no shell), and the run's logs are missing (`partial_data:missing_log_archive`).

**BREAK_GLASS / CONTEXT_BUDGET_WARN / Semble and Serena fallbacks.**
- `break_glass_count=0` and `context_budget_warn_count=0` in every family. There is no policy or prompt-size pressure.
- `SEMBLE_FALLBACK`: 34 in total. 32 are `semble_contract_test_fallbacks`: 4 in each of 8 CI runs (for example 44453299520, 44480944167), which is healthy test noise. 2 are runtime fallbacks, both in implement 44486357244, which still succeeded; the reason isn't in the summary. That is a rare fail-open, not a broken rollout.
- `SERENA_PROBE` and `SERENA_FALLBACK`: 0 (disabled).

## AI Memory Health

- **Only one `AI_MEMORY_TELEMETRY` line is visible in the window:** implement 44486357244 logged `{"op": "record-run-event", "event_type": "phase_completed", "ok": true, "did_push": true, "push_attempts": 2, "push_retry_reasons": ["non_fast_forward"], "clone_ms": 25826, "write_ms": 2932, "push_ms": 5121}`.
  - The one push retry was a `non_fast_forward` race with a concurrent writer, and it recovered.
  - The 25.8 s clone is the largest memory cost.
  - With one sample there is no trend.
- **No `retrieve` operations were visible,** so retrieve hit rate, `estimated_tokens` against budget, and the `keyword_method` split can't be computed. No `fail_open: true` or `enabled: false` entries were seen.
- **The gap comes from the run-logs directory not being mounted,** not from emission: retrieve telemetry is already emitted, per prior verdicts 2026-08-23 and 2026-08-24.
- **No recommendation.** Next collection step: mount `workflow-log-output` so `slow/` review runs (for example 44469558933 and 44474475914) give retrieve samples.

## GH API Call Audit

**G1. Protect the shared PAT before the collector's jobs loop starts.**
- **Evidence:** reset epoch `1795079786` is 09:16:26 UTC.

  | Time (UTC, approx.) | Source run | Remaining | Used in job |
  |---|---|---|---|
  | 08:43 | 44488256028 | 2933 | 61 |
  | about 09:00 | 44484740574, `post-merge-validate-dispatch` | 1068 | 24 |
  | about 09:00 | 44484740574, `post-merge-force-poll` | 950 | 126 |
  | 09:02 | 44492505906 | 789 | 28 |
  | 09:13-09:16:24 | collector | 0 (43 × HTTP 403) | — |

  A second bucket with reset `1795079803` (09:16:43) had 4,565-4,787 remaining (44487690933, 44485585319), so it is probably a different token.
- **Root cause:** the collector spends the pipeline's PAT on per-run `actions/runs/<id>/jobs` calls: up to 2 pages for each failed or cancelled run, and 92 such runs are in this window. It has no remaining-budget floor.
- **Exact change:**
  - Before the jobs loop, read `gh api rate_limit --jq .resources.core.remaining`. This call doesn't count against the limit.
  - If remaining is below `WLA_COLLECTOR_PAT_FLOOR` (default 500), mark the remaining rows `jobs_fetch_status=skipped_budget_floor` and log `COLLECTOR_RATE_LIMIT outcome=floor remaining=<n> floor=<n>`.
  - §14 audit: no existing call in the collector reads `rate_limit`; the `rate_limit_audit_fallback` warning at `:337` is the cache path, not a budget read.
- **Estimated reduction:** at least 500 calls kept for the pipeline in an exhausted hour. Removes the 09:13-09:16 window where every PAT call failed.
- Finding check: new — no prior verdict; the collector code has no floor.

**G2. Merge-train release re-fetches file lists every tick for the same blocked pair.**
- **Evidence:**
  - 44486410277 logged `MERGE_TRAIN_RELEASE_SUMMARY examined=1 released=0 overlap=1 … file_list_fetches=3 file_list_cache_hits=0 active_listing_calls=1 elapsed_ms=38541`.
  - 44488218380 repeated the same counters (`oldest_queued_age_secs=5764`, `elapsed_ms=39227`) with `MERGE_TRAIN_GATE pr=9774 older=9773 overlap=paths conflict=conflict action=block`.
- **Root cause:** the cache is per tick only, and heads 9773/9774 did not change between ticks (inference).
- **Exact change:** key the existing per-tick file-list cache by `pr:head_sha` and keep it in the existing ledger artifact or cache, so an unchanged head reuses its list. This is low value: about 3 calls and about 39 s per tick.
- Finding check: new — 2026-10-08 GH API #1 applied the counters, and a page count was deferred. Cross-tick reuse has no verdict.

**Other observations (no change).**
- Lint (`pulls/<n>/commits?per_page=100`, one page) and final triage (one `pulls/<n>` read) are minimal.
- `promote_candidate_follow` 44487121843 logged one permanent 404 (`git/ref/tags/promote-cycle-frozen`, `attempt=1/5 wait_secs=0`). It is correctly not retried.

## Prompt Cache & Memory System

- **Overall `cache_hit_rate` is 0.9255:** 113,744,028 cache-read against 7,343,072 prompt and 1,812,849 cache-write tokens. review_autofix accounts for 0.9264 of that.
- **Per-run review hit rates range from 0.671 to 0.940:**
  - 0.671: 44468570055 (PR 9396)
  - 0.831: 44474475914
  - 0.868: 44469558933
  - 0.936: 44447007564
  - 0.940: 44459756477
- **The two runs with the lowest hit rates are the shortest,** with 6-8 calls. That points to cache creation on the first call of a new prefix more than to fragmentation (inference).
- **No `CONTEXT_BUDGET_WARN` lines** in the window.

**P1. Test & Mark Stable writes cache it never reads.**
- **Evidence:** 44459871048 had `or_calls=12`, `or_prompt_tokens=61,966`, `or_cache_write_tokens=60,654`, `or_cache_read_tokens=0` and `cache_hit_rate=0.0`. About 98% of its prompt was written to cache at the cache-write premium and never reused.
- **Root cause (inference):** each of the 12 calls carries a different leading segment, or the calls are spaced past the cache TTL (the run lasted 10,007 s).
- **Exact change:** in the release gate's model-call helper, either place static instructions before the per-call variable text, or turn off cache-control markers for these one-shot calls. Which one depends on the call sites, which this report did not read.
- **Estimated savings:** small, about 60k cache-write tokens per release gate. Quality risk: none.
- Finding check: new — no prior verdict on release-gate cache behaviour; the telemetry shows the zero reads.

## Orchestrator Health

- **Release gate.** Test & Mark Stable 44459871048 failed after 10,007 s; the failing job is unknown because of the R1 rate limit. Heal routed it, and implement 44486357244 (`HEAL_ROUTE=true`, "Workflow heal: Test & Mark Stable Release failed on stable") opened PR #9779, which touches `tests/test_orchestrate_poll_process.py`. Auto-release dispatched a new gate (44490333814, outcome outside the window). Related: CI 44447007404 failed at "Orchestrate poll process unit tests". This self-heal loop is working; no change.
- **Red base.** PR 9770's pre-push gate (44484740574) listed 4 inherited failures that also fail on the merge base:
  - `tests/test_phase_wrapper_predicate_contract.py`
  - `tests/test_promote_candidate_follow.py`
  - `tests/test_workflow_file_size_limit.py`
  - `generate-check`

  CI 44453299520 failed at "Review autofix step-script contract and workflow file size guard tests". Guard-test diagnostics were rejected before (2026-10-07-3 Reliability 1), so this report adds no recommendation; R4 covers the file-size half. The base being red is a pipeline-wide risk: final CI can't go green on inherited failures.
- **Merge train.**
  - PR 9774 is blocked behind 9773 (`overlap=paths conflict=conflict action=block`) for at least 5,764 s.
  - PR 9773 logged `outcome=timeout … ci_check=lint ci_state=absent` and `FINAL_CI_MERGE_RESUME … reason=required_checks_timeout`.
  - PR 9772 was dropped on a failed lint (R2).
- **Integration project #9421.** 4 of 7 sub-issues are still unchecked (#9766-#9769, 44486418054). The readiness gate is working as intended.
- **Forward merge.** 44488325688 opened fallback PR #9778 with conflicts in 9 files, including `agents.md` and `agents.d/8808-heal-stale-auto-close.md`. Whether `agents.md` was edited directly or the conflict came from a fragment fold was not verified.
- **Unblock judge.** 44487604236 acted on #9733 (`verdict=retry_budget round=1`, claude-opus-5-5, $0.1473). 44491652135 is waiting on fixup 9670 for #9283 (`stop=security-pass-failed`).
- **Indicators to track:**
  - `CI_FINAL_MERGE_REF outcome=` counts (R2)
  - `REVIEW_RUN_CANCELLED cause=` (C1)
  - `MERGE_TRAIN_RELEASE_SUMMARY oldest_queued_age_secs`
  - `GH_PAT_BUDGET remaining=` at 09:00 and later

## Pipeline Flow Bottlenecks

1. **Queueing is the dominant overhead.** Jobs whose steps take seconds wait 10-31 minutes for a runner:
   - 44484740574: 31 min (`post-merge-validate-dispatch`)
   - 44488218380: 26 min (`cancel-active-runs`, 1,613 s total)
   - 44486417985: 24 min, then 5 s of work
   - 44486418393: 23 min, then 8 min of work
   - 44486418192: 22 min (review gate)
   - 44486418054: 21 min, then 5 s of work
   - 44487690933: 20 min (heal intake)
   - 44490028225: 17 min (implement)
   - 44489945764: 17 min (review gate)

   The fixes are S1 and S2. Rejected and deferred cadence work is not reopened.
2. **Compute in review/autofix:** p50 1,783.5 s, p95 5,671.95 s. Long tails come from reviewer and editor steps without step caps (C1, C2).
3. **Retry and rerun cost:**
   - 53 of 154 review runs were cancelled (S2).
   - `CI_CANCELLED_RERUN` correctly skipped all candidates (44488256028, 44492505906; for example `runner_shutdown_unverified` for PRs 9693 and 9675).
4. **Merge and conflict:** the merge-train block and forward-merge conflicts are covered under Orchestrator Health.
5. **Fan-out of skipped wrapper runs.** Each issue comment creates 4 wrapper runs (clarify, plan, implement, clarify-respond), for example the 4 runs at 08:48:27 (44490747710, 44490747795, 44490747857, 44490747825). Totals: clarify 84 of 97 skipped, plan 71 of 82, implement 69 of 82, clarify-respond 79 of 82. workflow_failure_heal has 112 of 113 skipped because it fires on every `issues: labeled` event (`internal-workflow-failure-heal.yml:3-7`). Skipped runs take no runner, but §26 notes they still flood the queue and the API.
   - **Exact change:** route `issue_comment` through one wrapper that calls the matching reusable workflow by an `if:` on the job. This is a control-plane change and needs its own plan.
   - Finding check: new — prior entry 2026-06-23 `queue-noop-fanout-reduction` is "Deferred as risky / broader-scope work", not a decision against it. The cause is still present: four separate `internal-*.yml` wrappers.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** runner queue waits of 10-31 minutes across all families; review/autofix p95 of 5,672 s.
- **Top failure modes:**
  - Rate-limited jobs fetch hides the failure step for 5 of 14 failures.
  - Final-CI merge ref: 4 of 12 CI failures.
  - The base is red on 4 inherited checks.
  - A Test & Mark Stable failure (10,007 s).
- **Highest-cost drivers:** two cancelled review runs, 113.3M of 124.4M tokens (91%).
- **Top 3 prioritized actions:**
  1. Add the collector's primary-rate-limit breaker and PAT floor (R1/G1).
     Finding check: new — `collect_workflow_logs.py:533` retries primary limits, and there is no floor; 43 × HTTP 403 between 09:13 and 09:16 UTC.
  2. Print the cancel cause in the existing cancelled() memory step, then cap "Run reviewer models" (C1 → C2).
     Finding check: reopened — 2026-09-21-2 Reliability 3 rejected this because pre-first-step runs can't log; 44447007564 was cancelled mid-step after 6,554 s with 28.5M tokens.
  3. Keep `install-claude` in the check-triage cleanup (R3).
     Finding check: new — `check_failure_triage.yml:607` deletes it; 44490437632 logged the post-step error.

## Metrics Appendix

**Runs and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| repo total | 1,000 | 405 | 14 | 78 | 503 | 1.4% | 11 | 3,517 |
| ci | 71 | 55 | 12 | 4 | 0 | 16.9% | 869 | 4,598.5 |
| review_autofix | 154 | 99 | 0 | 53 | 2 | 0% | 1,783.5 | 5,671.95 |
| orchestrate_poll | 42 | 21 | 1 | 20 | 0 | 2.4% | 806.5 | 1,847.55 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 100% | 10,007 | 10,007 |
| implement | 82 | 12 | 0 | 1 | 69 | 0% | 1 | 2,279.2 |
| workflow_failure_heal | 113 | 1 | 0 | 0 | 112 | 0% | 1 | 10 |

**CI failure points (12)**

| Failure point | Count | Runs |
|---|---|---|
| Verify the final-CI merge ref is current | 4 | 44445920977, 44447795119, 44455754191, 44480944167 |
| Unknown (jobs fetch rate-limited) | 5 | 44468836305, 44458994103, 44459557215, 44476634913, 44468253598 |
| Checkout repository | 1 | 44478695673 |
| Guard tests (file size / step-script contract) | 1 | 44453299520 |
| Orchestrate poll process unit tests | 1 | 44447007404 |

**Tokens and cache**

| Metric | Value |
|---|---|
| or_calls | 48 (36 review_autofix, 12 test_and_mark_stable) |
| total / prompt / completion tokens | 124,436,339 / 7,343,072 / 1,599,466 |
| cache read / write | 113,744,028 / 1,812,849 |
| cache_hit_rate (repo / review / release gate) | 0.9255 / 0.9264 / 0.0 |
| wall_clock_p50_ms / p99_ms (n=120) | 321,000 / 9,490,770 |
| break_glass_count / context_budget_warn_count | 0 / 0 |
| codex_calls | 0 |
| Top 2 runs (cancelled) | 44459756477: 84,802,470; 44447007564: 28,502,880 |

**GH API**

| Signal | Value |
|---|---|
| Collector jobs-fetch 403 (primary rate limit) | 43 (09:13:37 to 09:16:24 UTC) |
| Collector log 404 | 1 (44468251148) |
| PAT bucket reset 1795079786 | 09:16:26 UTC; remaining 2933 → 1068/950 → 789 → 0 |
| Highest `used_in_job` | 126 (`post-merge-force-poll`, 44484740574) |
| Merge-train per tick | `file_list_fetches=3`, `cache_hits=0`, about 39 s |

**Semble / Serena / MCP**

| Metric | Value |
|---|---|
| SEMBLE_QUERY calls / bytes / sources | 5 / 78,709 / 37 |
| SEMBLE_BOOTSTRAP count / total ms / failed / unused | 5 / 207,528 / 0 / 0 |
| SEMBLE_FALLBACK total / contract-test / runtime | 34 / 32 / 2 (implement 44486357244) |
| static_dup_bytes_total | 0 |
| SERENA_QUERY / FALLBACK / response bytes | 0 / 0 / 0 |

| MCP target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Findings check**

| Status | Count |
|---|---|
| new | 9 (S1, C2, R1, R2, R3, G1, G2, P1, Pipeline fan-out) |
| recurring | 1 (S2) |
| reopened | 1 (C1) |
| unverified | 1 (R4) |
| dropped: already fixed on this ref | 2 (Semble contract-test/runtime split; AI-memory push-retry telemetry) |
| dropped: previously decided | 2 (runner-queue timing logging, rejected 2026-09-30-2 Speed 4 / 2026-10-08; restoring the full-log archive, rejected 2026-10-08) |

**Data gaps**

- `coverage.repositories_truncated`: `shubhodeep1/coding-workflows` (`page_limit_reached: true`).
- The run-logs directory was not mounted, so there are no deep-dive excerpts, no `CI_FINAL_MERGE_REF` tokens and no `retrieve` telemetry.
- `analysis/recommendation-processing-report.md` was readable, but its latest processed doc is `workflow-optimization-2026-10-08.md`. The Nov 13-19 docs have no verdicts yet, so overlaps with them are reported as `new`.
- Workflow file sizes (`wc -c`) were not measured: no shell in this run.