I now have enough evidence to write the report. The final-CI loop is the dominant finding, and the logging proposals are tied to it.

## Executive Summary

- **Data gaps.** `shubhodeep1/coding-workflows` is in `coverage.repositories_truncated` (`page_limit_reached: true`), so only part of its runs are listed. No repository failed. The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted for this analysis. Evidence therefore comes from `analysis_context.json` (run rows, `log_summary`) and the repository code. The exact `CI_FINAL_MERGE_REF ... outcome=` token of each failure could not be read.
- **Top finding: final CI fails at its own merge-ref freshness check, and that failure sends PRs back through a full review cycle.**
  - 19 of 21 CI failures (90%) stop at step `Verify the final-CI merge ref is current`, across 16 PRs and 7 different jobs. The step was added on 2026-11-17 (`scripts/ci_final_merge_ref_fresh.sh`).
  - The script's own error text says "this is not a defect in the PR". Even so, `scripts/review_final_ci_failed.sh` treats it as a genuine failure: it posts the `ai:final-ci-failed` marker and dispatches review again.
  - Six re-review runs were created 4–5 s before the computed end of their PR's failed CI run (PRs 9683, 9685, 9698, 9722, 9239, 9562). Together they took 57,952 s (about 16.1 h) of review wall-clock.
  - Three of those PRs (9683, 9685, 9698) then failed final CI again at the same step.
  - Estimated impact: about 3.5–4 h saved per avoided loop, and up to about 24M OpenRouter tokens saved in this window. Confidence: high for the pattern, medium for the token estimate.
- **Proposed fix (smallest safe one).** Add a `merge_ref_stale` class to `classify_runner_shutdown` in `scripts/ci_cancelled_rerun.py` and to the infra-verdict list in `review_final_ci_failed.sh`. A stale-ref failure then gets the existing once-per-head re-run of failed jobs instead of a new review cycle. A conflict (`outcome=conflict`) stays a hand-back. Confidence: high.
- **Proposed diagnostics.** The `CI_FINAL_MERGE_REF` line does not record which job failed, how long the refresh loop ran, the last `mergeable` value, or how many base tips it saw. That leaves "GitHub was slow to recompute" indistinguishable from "the base moved" or "the job sat in the runner queue". Add these fields before tuning `CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS`. Confidence: medium.
- **Runner queue saturation sets the latency floor.**
  - Every `log_summary` with timing shows a 5–28 min runner wait for jobs that run 5–8 s, for example `lint` in 44382811997 (24 min) and `readiness` in 44382811822 (28 min).
  - The 19 merge-ref failures took 3,285–8,296 s wall-clock (median 6,786 s), although their jobs have 15–20 min timeouts. Inference: almost all of that is queue time.
  - Pacing and queue-logging proposals were rejected before (see Speed), so the only recommendation here is to remove avoidable loop runs. Confidence: high.
- **Cost profile is healthy apart from the loop.** 7 review runs with telemetry: 57 OpenRouter calls, 27,627,830 tokens, `cache_hit_rate` 0.7346. `break_glass_count` and `context_budget_warn_count` are 0. Semble runtime fallbacks are 0 and Serena shows no activity. Confidence: medium (only 7 runs sampled).

## Speed Optimizations

**1. (Critical path) Stop the final-CI → review → final-CI loop for stale merge refs.** Expected saving is the largest in this window.

- **Evidence.** Re-review runs created seconds before the computed end of the failed CI run (`created_at + duration_seconds`):

  | PR | Failed CI run (merge-ref step) | Re-review run | Re-review duration |
  |---|---|---|---|
  | 9683 | 44321898713 (14:13:27 + 4,408 s = 15:26:55) | 44335731047, created 15:26:51 | 9,909 s |
  | 9685 | 44321891376 | 44334932903 | 9,779 s |
  | 9698 | 44313651939 | 44334500021 | 9,503 s |
  | 9722 | 44336385478 | 44354774348 | 9,275 s |
  | 9239 | 44327456700 | 44338718200 | 7,939 s |
  | 9562 | 44328421953 | 44351032929 | 11,547 s |

  Then 9683 (44369656136), 9685 (44369610428) and 9698 (44365931719) failed the same step again.
- **Root cause.**
  - `review_final_ci_failed_infra_verdict` (`scripts/review_final_ci_failed.sh:96-98`) defers only `shutdown|action_resolution|checkout_fetch|abandoned`.
  - `classify_runner_shutdown` (`scripts/ci_cancelled_rerun.py:392-496`) returns `genuine` for a job that failed at the merge-ref step.
  - So the hand-back posts the marker and dispatches review (`review_final_ci_failed.sh:187-216`), although `ci_final_merge_ref_fresh.sh:181` declares the failure "not a defect in the PR".
- **Change.** See Reliability 1.
- **Estimated saving.** Per avoided loop, one review run (mean 9,659 s across the six above) plus one full final-CI run (median 6,786 s for the merge-ref failures) is replaced by a re-run of the failed jobs. Inference: that re-run costs about 15–45 min, mostly queue time. Net saving is about 3.5–4 h per PR per occurrence, with 6 confirmed and up to 19 candidate occurrences in this window.
- **Risk.** Low. The re-run is capped at attempt 1 per head (`review_final_ci_failed.sh:90`), and a second failure hands back as today.

Finding check: new — no entry in `analysis/recommendation-processing-report.md` mentions the merge-ref check or final-CI hand-back classification, and `INFRA_VERDICTS` at `scripts/ci_cancelled_rerun.py:103` still lacks a merge-ref class.

Micro-optimisations not raised: Semble bootstrap averages about 50.4 s per review run (403,089 ms over 8 bootstraps), under 1% of review p50.

## Cost Optimizations

**1. Avoid re-review token spend caused by stale-merge-ref hand-backs.**

- **Evidence.**
  - The 7 review runs with OpenRouter telemetry used 2,152,413–5,796,753 tokens each, mean about 3.95M:
    - 44327282082: 2,152,413
    - 44326055985: 3,192,541
    - 44329336775: 3,821,509
    - 44347371747: 4,084,544
    - 44322877517: 4,111,475
    - 44320068424: 4,468,595
    - 44331739470: 5,796,753
  - The six loop-driven re-reviews (Speed 1) were not log-sampled.
- **Root cause.** Same as Speed 1.
- **Change.** Reliability 1.
- **Estimated saving.** About 6 × 3.95M ≈ 23.7M tokens in this window (inference: extrapolated from the sampled runs). It also saves about 38 final-CI runner jobs per avoided re-dispatch: 1+1+1+1+1 single jobs, 5 `orchestrate-poll`, 24 `tests-system-python` and 3 `tests-sandbox-python` legs, plus `lint`, `budget-watch` and `final-ci-failed` (`.github/workflows/ci.yml:3426, 4228, 4540`). That matters under the §26 runner-slot pressure.
- **Quality risk.** None to review quality. A PR that is really broken still goes back to review after the second failure.

Finding check: new — same root cause as Speed 1; no prior verdict.

**Semble and Serena.**

- Semble is cheap and not noisy:
  - 8 `SEMBLE_QUERY` calls, 114,946 bytes (about 14.4 KB per query), 57 sources (about 7 per query).
  - `static_dup_bytes_total` = 0 and `echo_lines_dropped` = 0.
  - Against 4,933,002 prompt tokens, Semble context is under 1% of review input.
  - All 12 `SEMBLE_FALLBACK` lines are contract-test fallbacks inside CI runs 44345803230, 44331323917 and 44365931719 (4 each). Runtime fallbacks are 0.
- Serena shows zero query, probe and fallback counters. It is either not enabled for these workflows or not emitting.
- No change recommended. Cache-prefix restructuring was rejected before (2026-10-06 Cost 2–4, "need a quality baseline"), so it is not raised again.

## Reliability Improvements

**1. Classify a stale-merge-ref final-CI failure as an infra re-run, not a PR defect.** Expected impact: up to 19 of 21 CI failures (90%) handled without a review cycle.

- **Failure evidence.** 19 runs failed at `Verify the final-CI merge ref is current`, spread across 7 job families:

  | Failing job | Runs | Run IDs |
  |---|---|---|
  | `static-checks` | 7 | 44340223993, 44313651939, 44336385478, 44336886248, 44321898713, 44327456700, 44379101151 |
  | `tests-release-and-log-analysis` | 3 | 44340510362, 44340566861, 44364270740 |
  | `tests-promote-stall-and-review` | 3 | 44369610428, 44366307380, 44321891376 |
  | `tests-heal-plan-and-validation` | 2 | 44336041953, 44351314658 |
  | `tests-system-python` | 2 | (14) 44331323917, (20) 44365931719 |
  | `tests-sandbox-python (review)` | 1 | 44328421953 |
  | `tests-hooks-and-orchestrator` | 1 | 44369656136 |

  Because every job family is affected, this is a per-job race, not a defect in one job.
- **Root cause category.** Infra/timing.
  - Each of about 38 final-CI jobs re-checks `refs/pull/<n>/merge` independently, with a 60 s refresh budget (6 × 10 s, `ci_final_merge_ref_fresh.sh:110-115`).
  - Jobs start minutes to hours apart because of the runner queue, so a late job can meet a base that moved after GitHub last recomputed the merge ref (inference).
  - The classifier has no class for this, so the hand-back treats it as a PR defect.
- **Exact fix (additive, follows the existing `checkout_fetch` precedent):**

```python
# scripts/ci_cancelled_rerun.py
MERGE_REF_STEP_NAME = "Verify the final-CI merge ref is current"
# Only the stale message; the conflict message ("no longer merges cleanly")
# stays "genuine" so a real conflict still goes back to review/autofix.
MERGE_REF_STALE_MESSAGE = re.compile(r"CI_FINAL_MERGE_REF: refs/pull/[1-9][0-9]*/merge still merges into .+")
INFRA_VERDICTS = ("shutdown", "action_resolution", "checkout_fetch", "abandoned", "merge_ref_stale")
# in classify_runner_shutdown, next to the checkout_fetch branch:
#   elif merge_ref_stale_enabled and job_failed_at_step(job, MERGE_REF_STEP_NAME) \
#           and annotations and all(MERGE_REF_STALE_MESSAGE.fullmatch(m) or m == GENERIC_STEP_FAILURE
#                                   for m in (a["message"].strip() for a in annotations)):
#       shutdown_ids.add(job["id"]); merge_ref_stale_ids.add(job["id"])
```

  - `scripts/review_final_ci_failed.sh:97`: extend the case to `shutdown|action_resolution|checkout_fetch|abandoned|merge_ref_stale`.
  - `ci_cancelled_rerun.py:622`: add `merge_ref_stale` to the passthrough tuple.
  - New switch `CI_MERGE_REF_STALE_RERUN_ENABLED`, defaulting to on (§8), parsed like `action_resolution_rerun_enabled`.
  - Tests to extend: `tests/test_review_final_ci.py` plus the classifier tests.
- **Rollback and fail-open.**
  - Switch off: behaviour returns to today's hand-back.
  - A mixed run (any other failed step), a conflict annotation, more than `MAX_FAILED_JOBS_CLASSIFIED` (10) failed jobs, or a classifier timeout all return `genuine` or `unverified`, which hands back as today (fail-closed).
  - Security: the step runs a PR-controlled script, so a PR could forge the annotation. As with `job_failed_at_checkout` (`ci_cancelled_rerun.py:159-166`), the once-per-head cap bounds that to one extra re-run.
- **Caveat.** If a stale event fails more than 10 of the 38 jobs, the 10-job cap yields `unverified`. The diagnostics in item 2 will show how often that happens.

Finding check: new — no prior verdict; `INFRA_VERDICTS` (`scripts/ci_cancelled_rerun.py:103`) and the case at `review_final_ci_failed.sh:97` lack the class on this ref.

**2. Add diagnostic fields to the `CI_FINAL_MERGE_REF` terminal line.** Expected impact: these fields decide whether to raise the 60 s refresh budget or leave it, so the stale rate itself can be cut.

- **Evidence.**
  - The 19 failures above show only the failing step.
  - The line format `CI_FINAL_MERGE_REF pr= head= outcome= reason= [base=] [tested_base=] [tip=]` (`ci_final_merge_ref_fresh.sh:45, 51`) carries no job name, elapsed time, `mergeable` value or tip count.
  - So it cannot tell apart:
    - GitHub never recomputing within 60 s (`mergeable` stays null),
    - a fast-moving base,
    - a refreshed ref whose first parent was not in `seen_tips`.
- **Root cause category.** Observability gap tied to a recurring failure, not instrumentation for its own sake.
- **Exact change.** Track these values in `ci_final_merge_ref_main`:
  - `mergeable_last` (set in the loop at line 143),
  - the loop counter `attempt`,
  - the start `SECONDS`,
  - a count of distinct entries in `seen_tips`.

  Append them only to the `stale`, `conflict` and `refreshed` emits (lines 147, 167, 182):

```bash
# additive suffix; existing tokens and order unchanged
ci_final_merge_ref_log stale not_refreshed "base=${base_ref} tested_base=${first:-none} tip=${tip:-none} job=${GITHUB_JOB:-?} attempts_used=${attempts} elapsed_s=$((SECONDS - merge_ref_start_s)) mergeable_last=${mergeable_last:-unknown} tips_seen=$(wc -w <<<"${seen_tips}")"
```

  Choose new local names so they don't collide with existing ones (e.g. `merge_ref_start_s`, `mergeable_last`; §10). Update any exact-string assertions in `tests/test_ci_final_merge_ref_fresh.py`.
- **Expected follow-up.**
  - If `mergeable_last=null` dominates, raise the default `CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS` (cap 20).
  - If `tips_seen` exceeds 1 often, base churn is the driver.
- **Rollback.** Additive log fields only, so there is no behaviour change and nothing to fail open.

Finding check: new — no prior verdict covers the merge-ref check. Prior rejections of instrumentation-only proposals (e.g. 2026-10-06 Exec 3, 2026-10-07-2 Cost 2–5) cited "no defect established", whereas these fields serve a failure seen in 19 runs.

**Observed, no recommendation.**

- `tests-promote-stall-and-review` / `Review autofix step-script contract and workflow file size guard tests` failed in push-style run 44323961849 (`CI`, 5,828 s) and in 44345803230 (`CI [pr:9602]`, 8,868 s). The root cause can't be verified without logs. CI guard-test diagnostics were rejected before (2026-10-07-3 Reliability 1).
- `BREAK_GLASS` = 0 and `CONTEXT_BUDGET_WARN` = 0 in every parsed run: no policy or prompt-size pressure.
- `SERENA_PROBE` / `SERENA_FALLBACK` are absent.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines are available in this window. Retrieve hit rate, `estimated_tokens` against budget, and the `keyword_method` split can't be computed. Several `log_summary` rows state explicitly that no `AI_MEMORY_TELEMETRY` appeared in the visible logs:

- 44383840974
- 44384992935
- 44382812177
- 44382300207

Those are lint, readiness, CI-quick and clarify runs, where memory ops are not expected. The memory-using review runs (e.g. 44320068424, 44331739470) were parsed for cost telemetry, but their raw logs were not mounted.

**1. Verify emission from the next window's mounted logs before drawing conclusions.** Grep `slow/` review runs for `AI_MEMORY_TELEMETRY` with `op=retrieve` and `fail_open`. No code change is proposed.

Finding check: unverified — the run-logs directory was not mounted, so neither emission nor absence in the review/autofix logs could be confirmed.

## GH API Call Audit

**Volume and health.**

- `GH_PAT_BUDGET phase=end workflow=review_autofix_sweep job=sweep remaining=4655 ... used_in_job=40` (44386473434).
- Heal intake reported `remaining=4969` (44384810242) and `remaining=4996` (44384059074), both with `used_in_job=unknown`.
- No HTTP 429, secondary rate limit or retry storm appears in any `log_summary`.
- The only `GH_API_RETRY` is an expected permanent 404 on `git/ref/tags/promote-cycle-frozen` (44381915561, attempt 1/5, `wait_secs=0`).
- The lint workflow makes one paginated commits call per run (e.g. 44384992780, 44383840974).

**1. Remove the API cost of the stale-merge-ref hand-back loop (via Reliability 1).**

- Each hand-back in `review_final_ci_failed.sh` does:
  - one `GET pulls/<n>`,
  - one paginated comments read,
  - one marker POST (up to 3 attempts),
  - one review workflow dispatch,
  - plus every call of the review run it starts, and of the next final CI.
- The merge-ref check itself adds one `GET pulls/<n>` per job (about 38 per final run) and one per refresh attempt (`ci_final_merge_ref_fresh.sh:41-44`). The re-run of failed jobs repeats only those for the failed jobs.
- Estimated reduction: about 4–6 PAT calls per avoided hand-back, plus the dispatched review run's calls (not measurable here: the re-review logs were not sampled). Rate-limit risk is low today. This is waste reduction, not a fix for a limit breach.
- Batching the per-job `GET pulls/<n>` into one read is not recommended. The script documents that the check must run before the job's other PR read (`ci_final_merge_ref_fresh.sh:41-43`), and jobs share no `needs:`.

Finding check: new — no prior verdict on the final-CI hand-back API path.

The `GH_API_CALL` per-endpoint counters and the `used_in_job=unknown` gap in heal intake are not raised. They were rejected before as measurement-only (2026-10-06 GH API Call Audit; 2026-10-07-3).

## Prompt Cache & Memory System

**Cache behaviour.**

- Overall `cache_hit_rate` 0.734551: 19,989,631 cache-read and 2,290,781 cache-write tokens across 57 calls, all 57 with usage available.
- Per-run range:
  - 0.501706 (44327282082, pr:9701): the highest prompt at 719,353 and 339,746 cache-write tokens.
  - 0.834632 (44320068424, pr:9720).
  - 0.657561 (44329336775): 423,406 cache-write tokens, the most in the sample.
- Context-budget warnings: 0. No `CONTEXT_BUDGET_WARN` pressure.

**1. Stop paying fresh cache writes on loop-driven re-reviews (via Reliability 1).**

- **Evidence.** Sampled review runs write 239,520–423,406 cache tokens each. The six loop re-reviews start 2–3 h after the previous review of the same PR, so they are new runs that must pay their own cache writes.
- **Estimated impact.** About 6 × ~330K ≈ 2M cache-write tokens avoided in this window (inference), on top of Cost 1.
- **Risk.** None to cache behaviour.

Finding check: new — same root cause as Reliability 1; no prior verdict.

Prefix restructuring to raise the 0.50 outlier is not raised again: it was rejected in 2026-10-06 (Cost 2–4) pending a quality baseline, and this window adds no such baseline.

## Orchestrator Health

**Indicators observed.**

- Merge train:
  - `MERGE_LEASE pr=9359 ... outcome=held reason=holder_open`, with PRs 9602 and 9614 also held (44380898794).
  - `MERGE_LEASE pr=9359 ... outcome=released reason=expired`, then `pr=9367 ... granted` (44381912705).
- `AUTOFIX_AUTO_MERGE_CHECKS pr=9675` and `pr=9691` show `ci_check=lint ci_state=failure`, then `FINAL_CI_MERGE_RESUME ... outcome=dropped reason=required_checks_failed` (44381912705). Those `lint` failures are the merge-ref failures 44366307380 (pr:9675) and 44364270740 (pr:9691).
- Integration readiness for `orchestrator/project-9421` warns: 1 of 3 sub-issues still unchecked (`phase3-queue-backpressure-issue-linkage`, 44382811822). This is a healthy gate.
- Review sweep `CI_CANCELLED_RERUN` skipped every PR with `no_attributable_run`, `runner_shutdown_unverified final_ci=true` or `other_conclusion final_ci=true`, and dispatched 0 re-runs (44386473434). `other_conclusion` is how today's classifier labels a merge-ref failure.

**1. Track the hand-back outcome split after Reliability 1 lands.** Observable indicators, all already-existing log keys:

- `REVIEW_FINAL_CI_FAILED outcome=infra_rerun_deferred reason=merge_ref_stale` should replace most `outcome=redispatched`.
- `CI_CANCELLED_RERUN ... rerun merge_ref_stale final_ci=true` should appear in the sweep.
- `FINAL_CI_MERGE_RESUME ... outcome=dropped reason=required_checks_failed` should fall.
- A rising `runner_shutdown_unverified final_ci=true` count would mean the per-tick cap (`MAX_FAILURE_CLASSIFICATIONS_PER_TICK = 5`, `ci_cancelled_rerun.py:105`) is limiting throughput.

Finding check: new — tied to the Reliability 1 change; no prior verdict.

**Not raised again.**

- `orchestrate_poll`: 30 of 37 runs cancelled (e.g. 44384584586 after 1,325 s, 44382701376, 44382156729, all `cancelled_before_first_step`). The 2026-09-21-2 Reliability 3 verdict rejected this: these are the concurrency group replacing a pending run.
- Clarify, plan, implement, clarify-respond and heal wrappers: 72–78 skipped runs per family. This is the `issue_comment` fan-out, already handled on this ref (run-volume plan items 6 and 15; signed job-token comments in `review_final_ci_failed.sh:60-62`).

## Pipeline Flow Bottlenecks

**Bottleneck map (shubhodeep1/coding-workflows):**

| Stage | Runs (success / total) | p50 / p95 duration | Dominant overhead |
|---|---|---|---|
| clarify / plan / implement wrappers | 5/83, 5/77, 4/77 (rest skipped) | 1 s / 1,071–1,609 s | Queue: e.g. clarify 44382300207 queued 17m20s for about 2m24s of work |
| review_autofix | 148/156 | 7,925.5 s / 11,900 s | Compute plus queue; longest is 44316050653 (pr:9692) at 21,428 s |
| final CI (`ci`) | 76/98, 21.4% failure | 2,769.5 s / 7,986.4 s | Queue (jobs have 15–20 min timeouts); retries via the hand-back loop |
| merge / merge-train | n/a | n/a | Leases held behind red `lint` (9675, 9691) |
| side workflows (lint, readiness, heal intake, security audit) | 60/60, 55/92, 36/62, 18/18 | p50 about 1,119–1,576 s | About 5–28 min runner wait each, a few seconds of work |

**Split of overhead.**

- **Queueing**: account-wide runner saturation (§26), shown by every timed `log_summary`.
- **Retry**: the stale-merge-ref hand-back, which repeats review (about 2.7 h) and final CI (about 1.9 h) per occurrence.
- **Merge**: dropped resumes on red `lint`.

**Fixes ordered by end-to-end impact.**

1. **Reliability 1.** Removes the retry overhead, which is the largest avoidable cost, and frees about 38 runner slots per avoided final-CI re-dispatch, easing the queue for everything else.

   Finding check: new — no prior verdict on merge-ref hand-back classification.

2. **Reliability 2, then tune the refresh budget.** Only if `mergeable_last=null` dominates the new fields, raise the default `CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS` (`ci_final_merge_ref_fresh.sh:110`, currently 6, cap 20) so fewer jobs go stale at all.

   Finding check: new — no prior verdict; the default is 6 on this ref.

Queue pacing and queue-time logging are not raised again: they were rejected in 2026-10-07-2 (Speed 1, "queueing reflects hosted-runner capacity") and 2026-09-30-2 (Speed 4, "a saving of 0s").

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks.**
  - Runner queue waits of 5–28 min on 5–8 s jobs.
  - Review/autofix p50 of 7,925.5 s.
  - Final-CI wall-clock dominated by queueing: merge-ref failures took 3,285–8,296 s.
- **Top failure modes.**
  - Stale merge ref at `Verify the final-CI merge ref is current`: 19 of 21 failures, 16 PRs, 3 repeats.
  - `Review autofix step-script contract and workflow file size guard tests`: 2 failures (44323961849, 44345803230); root cause not verifiable without logs.
- **Highest-cost drivers.**
  - Review/autofix OpenRouter usage: 27.6M tokens over 7 sampled runs.
  - Loop-driven re-reviews: about 6 × 3.95M tokens, inferred.
  - Final-CI fan-out of about 38 runner jobs per dispatch.
- **Top 3 prioritized actions.**

  1. Add a `merge_ref_stale` infra verdict so a stale merge ref gets the once-per-head re-run of failed jobs instead of a review hand-back (Reliability 1).

     Finding check: new — `INFRA_VERDICTS` at `scripts/ci_cancelled_rerun.py:103` lacks it; no prior verdict.

  2. Append `job`, `attempts_used`, `elapsed_s`, `mergeable_last` and `tips_seen` to the `CI_FINAL_MERGE_REF` stale, conflict and refreshed lines (Reliability 2).

     Finding check: new — the line format at `scripts/ci_final_merge_ref_fresh.sh:45` has none of these; no prior verdict.

  3. After action 2, raise the refresh-attempt default only if `mergeable_last=null` dominates (Pipeline fix 2).

     Finding check: new — default 6 at `ci_final_merge_ref_fresh.sh:110`; no prior verdict.

## Metrics Appendix

**Run counts and outcomes (window ending 2026-11-18, truncated listing)**

| Scope | Total | Success | Failure | Cancelled | Other / skipped | Failure rate | p50 (s) | p95 (s) |
|---|---|---|---|---|---|---|---|---|
| Repo total | 999 | 480 | 21 | 42 | 456 | 2.10% | 849.0 | 8,865.3 |
| `ci` | 98 | 76 | 21 | 1 | 0 | 21.43% | 2,769.5 | 7,986.4 |
| `review_autofix` | 156 | 148 | 0 | 8 | 0 | 0% | 7,925.5 | 11,900.0 |
| `orchestrate_poll` | 37 | 7 | 0 | 30 | 0 | 0% | 733.0 | 3,862.2 |
| `integration_pr_readiness` | 92 | 55 | 0 | 3 | 34 | 0% | 1,119.0 | 2,618.75 |
| `lint_pr_body_auto_close` | 60 | 60 | 0 | 0 | 0 | 0% | 1,488.0 | 2,703.2 |
| `workflow_failure_heal_intake` | 62 | 36 | 0 | 0 | 26 | 0% | 1,237.0 | 2,628.75 |
| `clarify` / `plan` / `implement` | 83 / 77 / 77 | 5 / 5 / 4 | 0 | 0 | 78 / 72 / 73 | 0% | 1.0 | 1,071.3 / 1,609.0 / 431.4 |

**CI failure points**

| Failing step | Runs | Distinct PRs | Sum of durations | Median duration |
|---|---|---|---|---|
| Verify the final-CI merge ref is current | 19 | 16 (9683, 9685, 9698 twice) | 121,240 s (about 33.7 h) | 6,786 s |
| Review autofix step-script contract and workflow file size guard tests | 2 | 1 PR + 1 untagged (`CI`) run | 14,696 s | — |

**Hand-back loop (confirmed by timing)**

| Metric | Value |
|---|---|
| Re-review runs created at a failed merge-ref final-CI end | 6 |
| Their total wall-clock | 57,952 s (mean 9,659 s) |
| PRs failing the merge-ref step again after re-review | 3 |

**Token and cache telemetry (OpenRouter; 7 review runs)**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 57 (57 / 0) |
| Prompt / completion tokens | 4,933,002 / 415,637 |
| Cache read / cache write tokens | 19,989,631 / 2,290,781 |
| Total tokens | 27,627,830 |
| `cache_hit_rate` | 0.734551 (per-run 0.5017–0.8346) |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo) | 10,500 / 19,378,200 (122 samples) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (`review_autofix`) | 13,863,000 / 21,242,000 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 8 / 114,946 (about 14.4 KB per query) |
| `semble_sources_total` / `semble_static_dup_bytes_total` / `semble_echo_lines_dropped` | 57 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 8 / 0 / 0 / 403,089 (about 50.4 s each) |
| `semble_fallbacks` (contract-test / runtime) | 12 (12 / 0); CI runs 44345803230, 44331323917, 44365931719 |
| Serena query / fallback / response bytes / tool calls | 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` sweep (44386473434) | `remaining=4655 used_in_job=40` |
| `GH_PAT_BUDGET` heal intake (44384810242 / 44384059074) | `remaining=4969 / 4996`, `used_in_job=unknown` |
| HTTP 429 / secondary limit events | 0 observed |
| `GH_API_RETRY` | 1 (permanent 404, expected tag absence, 44381915561) |

**Findings check**

| Status | Count |
|---|---|
| new | 9 |
| recurring | 0 |
| reopened | 0 |
| unverified | 1 |
| dropped: already fixed on this ref | 1 |
| dropped: previously decided | 5 |

How the counts were made:

- The 9 `new` items are Speed 1, Cost 1, Reliability 1, Reliability 2, GH API 1, Prompt Cache 1, Orchestrator 1 and Pipeline fixes 1–2. Per-Repo actions restate Reliability 1 and 2 and Pipeline fix 2.
- The `unverified` item is AI Memory 1.
- Already fixed on this ref: the `issue_comment` wrapper fan-out.
- Previously decided:
  - runner-queue pacing and logging (2026-10-07-2 Speed 1; 2026-09-30-2 Speed 4)
  - poller cancelled-before-first-step diagnostics (2026-09-21-2 Reliability 3)
  - cache-prefix restructuring (2026-10-06 Cost 2–4)
  - CI guard-test diagnostics (2026-10-07-3 Reliability 1)

**Data gaps**

- `shubhodeep1/coding-workflows` listing is truncated (`page_limit_reached`).
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` was not mounted, so the `CI_FINAL_MERGE_REF outcome=` tokens (`stale` vs `conflict`), the number of failed jobs per run, and `AI_MEMORY_TELEMETRY` could not be read.
- The six loop re-review runs and 4 failed CI runs (44328421953, 44313651939, 44323961849, 44321898713, …) were `log_download_status: not_selected`.
- Claude-engine usage (`claude_*` keys) is not present in the run rows. Review runs 44316050653 and 44327302874 report 0 OpenRouter calls despite parsed logs.
- `analysis/recommendation-processing-report.md` was readable and used for the findings check.