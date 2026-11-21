## Executive Summary

**Coverage note.** The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) does not exist in this sandbox, so I could not read any `errors/`, `slow/` or `recent/` excerpts. Everything below comes from `analysis_context.json` (metrics plus the `log_summary` fields) and from reading the workflow and script code in this repo. The window is 1,000 runs (the collector cap) for one repo. The earliest run I saw is 2026-10-14T20:17Z and the latest is 2026-10-15T02:22Z, so the window is at least about 6.1 hours. `insufficient_data=false`. This report extends `analysis/workflow-optimization-2026-10-15.md` (an earlier window) and does not repeat it.

- **One review & autofix failure has a likely, fixable cause: the PR closed while the run waited in the per-PR queue.** The failed run 38664219310 (3,161s, step "Checkout PR head branch", 0 tokens) ended at 02:03:25. That is 01:10:44 + 3,161s. It ended in the same second that run 38667887108 finished. That run was created at 01:51:20, when PR 7403 closed (alongside cancel run 38667886930 and status-sync run 38667886921 for PR 7403), and it waited 725s before ending as "skipped". Both runs were therefore almost certainly in the PR 7403 queue (`pr-autofix-<PR>`, `cancel-in-progress: false`, `review_autofix.yml:1801-1802`). The checkout step at `review_autofix.yml:2780` is the only step in that area with no `if: env.PR_CLOSED != 'true'` guard (its neighbours at 2741 and 2917 have one). Its fetch loop calls `exit 1` after 4 attempts (`:2863-2866`) without checking whether the branch was deleted. **Impact:** removes about 1 of the 2 review failures; 4,116 runner-seconds were lost across the two failed runs. **Confidence:** medium (the timestamps line up to the second; I have no log excerpt).
- **All 4 validate runs failed (100%) and nothing records why.** Runs 38646893482, 38649799512 and 38652432980 (tracking issue 7355, about 25 minutes apart) and 38660958249 (`[tracking:0]`) all failed at "Enforce validation outcome". That step (`validate.yml:1583-1601`) prints only "Validation did not pass." even though `VALIDATION_STATUS` and `VALIDATION_RAW_STATUS` are already in its `env`. The `VALIDATE_OUTCOME` line proposed in the earlier report has still not landed: grep finds it only in the analysis file. **Impact:** this is the gating diagnostic for the highest failure-rate family. **Confidence:** high.
- **The aggregate prompt-cache hit rate (0.689) is inflated by one run; a typical review run is about 0.29–0.49.** Run 38665383967 alone used 21.84M of the 41.97M review tokens (52%) at a 0.915 hit rate. Without it, the hit rate is **0.443**. Cache writes per 20-call run are almost identical across different PRs: 588,876 / 588,728 / 588,652 / 584,304. That points to the same PR-independent prefix being written again every run instead of read from cache. **Impact:** cross-run prefix reuse could move about 0.3–0.6M tokens per run from write price to read price (estimate). **Confidence:** high for the numbers, medium for the cause.
- **Semble bootstraps once per query instead of once per run, costing about 42s each time.** In every review run with telemetry, bootstraps equal queries: 2/2 in 38668778684, 38668776052, 38664211684, 38665508491 and 38665383967, and 1/1 in 38630799532. Each run spends about 83s on bootstraps (82,956–93,018 ms). The lazy-bootstrap state file is keyed by `RUNTIME_DIR` (`semble_helpers.sh:13`). **Impact:** about 40s saved per two-query review run. **Confidence:** medium (the counts are exact; the second context's `RUNTIME_DIR` is an inference).
- **`GH_PAT_BUDGET used_in_job` overstates per-job usage when jobs run at the same time.** Review runs 38668778684 and 38668776052 overlapped (both about 02:01–02:14) and report `used_in_job=477` and `469`. Yet the shared PAT's `remaining` fell only from 4,124 (sweep 38668749821, about 02:01) to 3,624/3,633. `used_in_job` is computed as `previous_remaining − remaining` on the shared PAT (`gh_helpers.sh:68-71`), so each job also counts the other job's calls and the poller's. **Impact:** the hotspot numbers used for API-hygiene decisions are inflated, probably about 2x here. **Confidence:** high.
- **The release cycle lost about 80 minutes.** Promote run 38660611655 took 4,778s. The test-and-mark-stable run it dispatched (38660755875) was cancelled at "Phase 4: Wait for review & autofix to complete" after 4,658s, with a 0.0 cache-hit rate (12 calls, 28,486 cache-write tokens). The retry, promote run 38670328812 at 02:18 (v1.33.0, downstream run 38670511643), finished after this window. **Confidence:** medium (the cancel cause is not visible).

## Speed Optimizations

Ranked by expected latency reduction.

1. **Stop reviewing PRs that closed while their run was queued.** Critical path and runner occupancy.
   - **Evidence:** 38664219310 took 3,161s and failed with 0 tokens. 38667887108 sat queued for 725s and then skipped. Timing details are in the Executive Summary.
   - **Root cause:** "Check PR state" (`:2177-2201`) runs before 12 or more minutes of setup. "Checkout PR head branch" (`:2780`) has no `PR_CLOSED` guard, and a deleted head branch looks the same as a transient fetch error.
   - **Change:** the shell snippet under Reliability item 1, plus `if: env.PR_CLOSED != 'true'` on the step, once you have checked that downstream steps tolerate a missing `INITIAL_HEAD_SHA`.
   - **Estimated saving:** the failure becomes a short skip. About 725s of queue occupancy per close event goes away.
   - **Risk:** low. The change reuses the existing `AUTOFIX_STALE_BASE_SKIP` mechanism.
2. **Bootstrap Semble once per review run.** Critical path.
   - **Evidence:** bootstraps equal queries (11 and 11). That is 469,474 ms over 11 bootstraps, about 42.7s each.
   - **Root cause (inference):** the second query context resolves a different `RUNTIME_DIR` or runner temp directory, so it never sees the first context's `semble_bootstrap-<run>-<attempt>.state` file and builds the index again.
   - **Change:** first apply diagnostic 3 (Reliability item 5) to confirm. Then export a single `SEMBLE_BOOTSTRAP_STATE_FILE` and `SEMBLE_INDEX_PATH` at job level, so both contexts share them.
   - **Estimated saving:** about 40s per two-query run.
   - **Risk:** low. Semble fails open (`build_semble_wrapper.sh` always exits 0).
3. **Skip Semble in workflow log analysis when nothing queries it.** Micro-optimization.
   - **Evidence:** run 38660964102 had 3 bootstraps totalling 96,129 ms, all unused (`semble_query_calls=0`).
   - **Change:** in that workflow, gate the bootstrap on whether the analysis prompt actually queries Semble. Otherwise rely on lazy mode alone.
   - **Estimated saving:** about 96s per analysis run.
   - **Risk:** low.
4. **Orchestrate poller: runner wait and supersession.**
   - **Evidence:** 38668654563 waited about 3 minutes for a hosted runner (02:00:07 → 02:03:08). 16 of 51 poll runs (31%) were cancelled, for example 38661767114 (`cancelled_before_first_step` after 711s queued). p50 is 711s and p95 is 1,116s.
   - **Change:** keep the queueing behaviour as it is. Add the per-phase timing line `ORCH_POLL_PHASE` proposed in the earlier report before changing anything.
   - **Risk:** none for the diagnostic.
5. **Heal-PR reconcile does a full-history checkout even when it has nothing to do.** Micro-optimization.
   - **Evidence:** 38670308625 spent about 80s fetching tags before the reconcile, which then ran for 3s and logged `noop reason=no_heal_issue`. 38667886930 shows the same pattern (116s).
   - **Root cause:** `internal-cancel-on-pr-close.yml:45-50` checks out with `fetch-depth: 0` unconditionally.
   - **Change:** add a cheap step before checkout that asks whether this PR has any heal PR, and gate the full checkout on it. Keep `fetch-depth: 0` for the cases that do real work.
   - **Estimated saving:** about 80s per PR close.
   - **Risk:** low.

## Cost Optimizations

1. **Stabilise the cached prefix across review runs.**
   - **Evidence:** cache writes of about 588k tokens per 20-call run, near-identical across PRs 7403, 7404 and 7407. Hit rates are 0.286 (38668776052), 0.449 (38668778684), 0.482 (38664211684) and 0.488 (38665508491).
   - **Root cause (inference):** either the runs are spaced further apart than the cache lifetime, or something in the prefix varies between runs. Volatile metadata placed before the stable block, or a different provider route per call, would both cause this. The step "Pre-assemble static context (cacheable across runs)" (`:2549`) exists to prevent it, so the prefix is probably not reaching the model byte-identical.
   - **Change:** use the per-phase breakdown `cost_audit.py` already computes (`or_phases`) to find which phase does the writing. Hash the assembled prefix per call and log the hash, then compare hashes across runs.
   - **Estimated saving:** if half of the writes became reads, about 0.3M tokens per run would move to read price.
   - **Quality risk:** none, since the prompt content does not change.
2. **Investigate the outlier run 38665383967.**
   - **Evidence:** 12 calls, 21,844,388 tokens (about 1.82M per call), 99,408 completion tokens.
   - **Gap:** `CONTEXT_BUDGET_WARN` stayed at 0 for this run. The warning appears to measure the assembled prompt, not how much an agent session grows over many turns (inference).
   - **Change:** emit a per-session token warning (Reliability item 6) and pull the `or_phases` breakdown for this run.
   - **Estimated saving:** unknown. This one run is 52% of the window's model spend.
3. **Uncached input is the main cost driver.** 9,505,495 uncached prompt tokens against 241,840 completion tokens (39:1). Per call that is about 79–97k uncached tokens in the 20-call runs. Reasoning level and model choice are secondary; prefix reuse (item 1) is the lever.
4. **Semble is cheap.** 11 queries returned 177,629 bytes (about 16.1 KB and 5.2 sources per query) with `static_dup_bytes=0`, so nothing it adds duplicates the static context. At roughly 4 bytes per token (an assumption), the 33.6 KB per run is about 0.5% of a review run's uncached prompt. Its cost is the bootstrap time (Speed item 2), not context size. Its effect on output quality is not measured.
5. **Serena** shows zero queries, probes and fallbacks, and `SERENA_ENABLED: false` appears in the review-run summaries. There is nothing to evaluate.
6. **Data gap: Claude engine usage is not visible.** `cost_audit.py` parses `claude_*` usage (`CLAUDE_COUNT_KEYS`, used at `:995` and `:1043`). However, `RUN_COST_TELEMETRY_FIELDS` (`:162-199`) and `AGGREGATABLE_COST_FIELDS` (`:199-233`) leave it out. As a result validate, plan, implement and clarify all show 0 tokens. Add the `claude_*` keys to both tuples. This is additive and does not rename anything.

## Reliability Improvements

1. **Review & autofix: fail open when the PR head branch no longer exists.** Removes about 1 of 2 review failures.
   - **Category:** race between PR close and queued run.
   - **Fix:** in `review_autofix.yml:2863-2866`, check the remote branch before `exit 1`. `git ls-remote --exit-code` returns 2 only when the ref is absent, so a network error still fails the step:
     ```bash
     if [ "${fetch_attempt}" -ge "${fetch_max_attempts}" ]; then
       ls_rc=0
       git ls-remote --exit-code --heads origin "refs/heads/${HEAD_REF}" >/dev/null 2>&1 || ls_rc=$?
       if [ "${ls_rc}" -eq 2 ]; then
         echo "REVIEW_CHECKOUT_HEAD outcome=skip reason=head_branch_missing pr=${PR_NUMBER:-none}"
         echo "::warning::PR head branch ${HEAD_REF} no longer exists on origin; skipping this review."
         echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
         echo "CAN_PUSH=false" >> "$GITHUB_ENV"
         exit 0
       fi
       echo "REVIEW_CHECKOUT_HEAD outcome=fail reason=fetch_exhausted attempts=${fetch_max_attempts} ls_remote_rc=${ls_rc} pr=${PR_NUMBER:-none}"
       echo "::error::git fetch origin +refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF} failed after ${fetch_max_attempts} attempts; giving up."
       exit 1
     fi
     ```
   - **Rollback:** a single hunk; revert it.
   - **Unknown:** 38663245224 (955s) failed at the same step, but I cannot tell which PR it was for. The new `REVIEW_CHECKOUT_HEAD` line will classify the next one.
   - **Note:** the file is near the 480,000-byte workflow guard. Check `wc -c` before adding lines, and move the body into `scripts/review_autofix_step_<slug>.sh` if needed (§24).
2. **Validate: log why each run failed.** This is the prerequisite for any fix to the 100% failure rate.
   - **Fix:** at the top of the `validate.yml:1588` `run:` block, add `echo "VALIDATE_OUTCOME status=${VALIDATION_STATUS:-unknown} raw_status=${VALIDATION_RAW_STATUS:-unknown}"`. Both values are already in the step's `env`.
   - **Follow-up:** if the three tracking-7355 runs share a `raw_status` and the same head, add a guard that skips re-validating an unchanged head.
   - **Investigate:** `[tracking:0]` (38660958249) means validate ran with no tracking issue. It was created 4 seconds before validation-refresh run 38660965646 (inference: the refresh dispatched it).
3. **Test-and-mark-stable Phase 4.** Run 38660755875 was cancelled after 4,658s, and nothing in the telemetry says why.
   - **Fix:** in the Phase 4 wait loop, log a heartbeat every N polls with the review run ID it is waiting on, that run's status, and elapsed time against `review_timeout` (60 in the later dispatch).
   - **Impact:** separates an external cancel from a timeout from a stuck review.
4. **Semble fallbacks are healthy.** All 20 are `context=contract-test` (CI 16, test-and-mark-stable 4), and `semble_runtime_fallbacks=0`. There were 0 failed bootstraps in 14. This is a tests-only artefact, not a broken rollout. No action needed.
5. **Diagnostic: show which context bootstrapped Semble.** Add `target=` and a hash of `RUNTIME_DIR` to the lazy-path `SEMBLE_BOOTSTRAP` event in `semble_helpers.sh`, for example by exporting `SEMBLE_BOOTSTRAP_TARGET` before calling `build_semble_wrapper.sh` and appending it at `:40`. This confirms or rules out the double-bootstrap cause behind Speed item 2.
6. **Diagnostic: warn on per-session token growth.** Emit `CONTEXT_BUDGET_WARN scope=session` when one agent call's total tokens pass a threshold set by an env var with a default (§8). 38665383967 would have triggered it.
7. **`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`:** 0 and 0 in every run. There is no sign of policy or rubric pressure.

New log prefixes (`REVIEW_CHECKOUT_HEAD`, `VALIDATE_OUTCOME`, `GH_PAT_JOB_CALLS`) must be registered through an `agents.d/<id>-<slug>.md` fragment under "Stable log prefixes (contractual)" (§25), not by editing `agents.md` directly.

## AI Memory Health

- **`AI_MEMORY_TELEMETRY` entries seen (2, both `finalize-task`):**
  - 38670308629: `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues",...}`. Fail-open on a PR with no linked issues is expected behaviour.
  - 38667886921: `did_push: true`, `push_attempts: 1`, `issue_number: 7395`, `final_state: closed`. No push retries. The step took about 102s of the 148s run.
- **No `retrieve` operations were visible**, so hit rate, `estimated_tokens` against budget, and `keyword_method` mix cannot be computed. Review summaries say the memory lines were truncated away (for example, about 1.67M characters were cut from 38668776052).
- **Stale lineage:** after issue 7395 closed, implement run 38662160324 is still recorded as `in_progress` in `issue-7395` lineage. Check whether `finalize-task` should close open run entries.
- **Next step:** have the collector count `AI_MEMORY_TELEMETRY:` lines by `op` before truncation, using the same full-zip scan it already does for `GH_PAT_BUDGET` (`collect_workflow_logs.py:106-109`).

## GH API Call Audit

- **Measurement flaw (fix first).** `used_in_job` is a delta of the shared PAT's `remaining` (`gh_helpers.sh:68-71`). Jobs that overlap each count everyone's calls: the two concurrent review jobs report 477 and 469 against a combined drop of about 500.
  - **Change:** keep the contractual `GH_PAT_BUDGET` line as it is. The collector regex is anchored at the end of the line (`collect_workflow_logs.py:109`), so appending a field would break it.
  - Instead, add a separate `GH_PAT_JOB_CALLS job=<job> calls=<n>` line counted by a job-local counter inside `gh_retry`.
- **Observed:**
  - Review `codex-agent` jobs: 469–477 by delta, so the true per-job figure is lower.
  - Autofix sweep 38668749821: 6 calls, `remaining=4124`.
  - Lint workflow: 1 paginated commits read per PR, taking 9s in 38669408350 and 0.6s in 38668304063.
  - No HTTP 429s or secondary rate limits in any summary.
- **Missed localisation:** the gate job has paired start/end budget records for each sub-phase (`:362`, `:690`, `:725`, …). `codex-agent` has only a start record (`:1913`) and an end record.
  - **Change:** add `Record GH_PAT budget` checkpoints around "Run reviewer models" (`:3253`) and "Apply fixes with editor model" (`:3551`), reusing the existing helper.
  - These checkpoints read the rate limit and call nothing else. Per §14 I audited the existing per-phase budget calls; none of them covers the reviewer and editor phases.
- **Batching:** I found no new unbatched loops in this window's evidence. The candidates from the earlier report (REUSE-002 and others) still stand. Re-rank them once `GH_PAT_JOB_CALLS` gives real per-job counts.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.688 for the repo and 0.689 for review & autofix. That is 28,765,231 tokens read from cache, 3,454,974 written and 9,505,495 uncached. Without 38665383967 it is **0.443**. The per-run range is 0.286–0.915. Test-and-mark-stable is 0.0 (28,486 written, 0 read).
- **Usage data is complete:** `or_usage_unavailable_calls=0` (114 of 114 calls).
- **Fragmentation signals:**
  - The constant write of about 588k tokens per 20-call run (about 29.4k per call) for different PRs.
  - The best run (0.915) had 12 calls and a very large read volume. That pattern fits long multi-turn sessions reusing their own prefix, not reuse across runs (inference).
- **`CONTEXT_BUDGET_WARN`:** 0. The prompt-size check is not catching session growth (see Reliability item 6).
- **Actions:**
  1. Log a prefix hash per call and check it is identical across runs.
  2. Make sure volatile fields (PR number, SHA, timestamps) come after the stable block assembled by "Pre-assemble static context".
  3. Check whether provider routing is pinned for review calls. A cache written through one provider route is probably not readable through another (inference).
  - **Expected effect:** typical runs move from 0.44 toward about 0.6. This is an estimate.

## Orchestrator Health

- **Poller:** 51 runs, 35 succeeded and 16 were cancelled (superseded while queued). p50 711s, p95 1,116s. Run 38668654563 shows `SEMBLE_AVAILABLE: false`; the poller has Semble configured but unavailable, which is worth confirming is intended.
- **Wave progress:** tracking issue 7355 is active. PR 7405 uses the correct `Refs #7355` (lint run 38668304063 passed). PR 7403 for issue 7395 closed and the issue moved to `ai:closed` (38667886921). Validate failing 3 times on 7355 is the main stall risk.
- **Clarify respond:** 150 runs, 148 skipped by fan-out. One real run, 38665816382, took 859s.
- **Self-heal:** heal intake and heal ran 127 times, with 121 skipped. The recovery scan found `count=0` (38668121592). No heal loops.
- **Indicators to track:**
  - `VALIDATE_OUTCOME raw_status` counts per tracking issue
  - `REVIEW_CHECKOUT_HEAD outcome=skip|fail`
  - poll cancellation ratio
  - `MERGE_TRAIN_RELEASE_SUMMARY released` (0 in every observed run)

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix, by end-to-end impact |
|---|---|---|---|
| review & autofix | Compute plus per-PR queueing | p95 2,735s; agent step about 12.7 min (38668776052); 725s queued then skipped (38667887108) | 1. Closed-PR fail-open; 2. single Semble bootstrap; 3. prefix cache reuse |
| validate | Unclassified failures, about 25 min apart | 4 of 4 failed, 397–628s each | 4. `VALIDATE_OUTCOME`, then a same-head guard |
| release gate | Waiting on the e2e review | 4,778s promote; Phase 4 cancelled at 4,658s | 5. Phase 4 heartbeat |
| implement / plan | Compute | implement 1,617–1,744s (38667723331, 38642030993); plan 700–866s | Waiting on `claude_*` telemetry (Cost item 6) |
| CI | Compute | p50 1,093s; a 103-test step took 45.5s (38668304039) | Low priority: `--durations` |
| Fan-out | Skipped-run noise | 738 of 1,000 "other" | Shrinks the collector window to about 6h; filter skipped runs in the collector |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - review & autofix compute plus per-PR queue (p95 2,735s)
  - poller cycle (p50 711s)
  - release gate Phase 4 (4,658s)
- **Top failure modes:**
  - validate at 100% (4 of 4), cause not logged
  - review checkout after the PR closed (38664219310; 38663245224 unclassified)
- **Highest-cost drivers:**
  - review & autofix, 99.9% of model tokens (41.97M)
  - one outlier run at 52% of that
  - uncached prompt 9.5M and cache writes 3.45M
- **Top 3 actions:**
  1. Land the `REVIEW_CHECKOUT_HEAD` deleted-branch fail-open and the `VALIDATE_OUTCOME` line.
  2. Fix the overlapping-job count in `GH_PAT_BUDGET` with `GH_PAT_JOB_CALLS`, and add `claude_*` to the `cost_audit.py` field tuples.
  3. Share Semble bootstrap state within a run, and add prefix-hash logging to find the cache-reuse break.

## Metrics Appendix

**Runs (1,000-run window)**

| Scope | Total | Success | Failure | Cancelled | Other (skipped) | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| Repo | 1,000 | 236 | 6 (0.6%) | 20 | 738 | 1 | 952 |
| review_autofix | 39 | 34 | 2 (5.1%) | 1 | 2 | 196 | 2,735 |
| validate | 4 | 0 | 4 (100%) | 0 | 0 | 525 | 619 |
| orchestrate_poll | 51 | 35 | 0 | 16 | 0 | 711 | 1,117 |
| ci | 32 | 32 | 0 | 0 | 0 | 1,093 | 1,726 |
| implement | 152 | 9 | 0 | 2 | 141 | 1 | 1,184 |
| plan | 150 | 9 | 0 | 0 | 141 | 1 | 651 |
| clarify | 161 | 10 | 0 | 0 | 151 | 1 | 206 |
| orchestrate_clarify_respond | 150 | 2 | 0 | 0 | 148 | 1 | 10 |
| test_and_mark_stable | 1 | 0 | 0 | 1 | 0 | 4,658 | 4,658 |
| promote_main_to_stable | 2 | 2 | 0 | 0 | 0 | 2,454 | 4,546 |

**Tokens and cache (OpenRouter; Claude not exported)**

| Metric | Repo | review_autofix | review_autofix without 38665383967 |
|---|---|---|---|
| calls | 114 | 102 | 90 |
| prompt (uncached) | 9,536,086 | 9,505,495 | 8,351,477 |
| cache read | 28,765,231 | 28,765,231 | 8,857,877 |
| cache write | 3,483,460 | 3,454,974 | 2,771,366 |
| completion | 244,176 | 241,840 | 142,432 |
| total | 42,000,467 | 41,967,540 | 20,123,152 |
| `cache_hit_rate` | 0.688 | 0.689 | 0.443 |

**Per-run review telemetry**

| Run | PR | Calls | Total tokens | Hit rate | Cache write | Semble bootstraps / queries / ms |
|---|---|---|---|---|---|---|
| 38665383967 | — | 12 | 21,844,388 | 0.915 | 683,608 | 2 / 2 / 82,956 |
| 38665508491 | — | 20 | 4,830,816 | 0.488 | 584,304 | 2 / 2 / 93,018 |
| 38668778684 | 7404 | 20 | 4,608,762 | 0.449 | 588,876 | 2 / 2 / 84,534 |
| 38664211684 | 7403 | 20 | 4,265,666 | 0.482 | 588,652 | 2 / 2 / 83,348 |
| 38630799532 | 7389 | 10 | 3,364,084 | 0.465 | 420,806 | 1 / 1 / 42,540 |
| 38668776052 | 7407 | 20 | 3,053,824 | 0.286 | 588,728 | 2 / 2 / 83,078 |

**Review telemetry fields (repo):** `wall_clock_p50_ms` = 6,000; `wall_clock_p99_ms` = 4,493,330 (112 samples); `break_glass_count` = 0; `context_budget_warn_count` = 0. For review & autofix: `wall_clock_p50_ms` = 1,510,000 and `wall_clock_p99_ms` = 3,122,940 (12 samples).

**Semble:** 11 queries, 177,629 logged bytes (16,148 per query), 57 sources, 0 static duplicate bytes. 14 bootstraps (0 failed, 3 unused) totalling 565,603 ms (40.4s each). 20 fallbacks: 20 contract-test, 0 runtime. 0 echo lines dropped.

**Serena and other MCP servers:**

| Target | probe_ok | probe_failed | probe_skipped | queries | fallbacks | response_bytes |
|---|---|---|---|---|---|---|
| serena (all targets) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API (`GH_PAT_BUDGET phase=end`):**

| Run | Job | remaining | used_in_job (shared-PAT delta) |
|---|---|---|---|
| 38668749821 (sweep) | sweep | 4,124 | 6 |
| 38668776052 | codex-agent | 3,633 | 469 |
| 38668778684 | codex-agent | 3,624 | 477 |

All three share reset 1792032165. No 429s were observed.

**Data gaps and next collection steps**

- Mount the run-logs directory.
- Export the `claude_*` fields in `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS`.
- Add the `VALIDATE_OUTCOME`, `REVIEW_CHECKOUT_HEAD` and `GH_PAT_JOB_CALLS` lines, and add a target field to `SEMBLE_BOOTSTRAP`.
- Count memory, break-glass and context-budget lines before truncation.
- Filter skipped fan-out runs in the collector so the 1,000-run cap covers 24 hours or more.

**Assumptions**

- The PR 7403 attribution for 38664219310 rests on the timestamp match, not on its workflow name, which has no `[pr:N]` suffix.
- Token-to-byte conversions assume about 4 bytes per token.
- The cache-cause statements are inferences.

I changed no files; this report is the only output.
## Deep Audit — Workflows & Scripts (2026-10-15)

**Scope and method.** I read the following in full or in part: `CLAUDE.md`, `scripts/gh_helpers.sh` (1-1066), `scripts/semble_helpers.sh`, `scripts/orchestrate_poll_process.sh` (selected regions), `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/lint_pr_body_auto_close.py`, the fingerprint call sites in two `review_autofix_step_*.sh` scripts, and the workflows `review_autofix.yml`, `implement.yml`, `clarify.yml`, `validate.yml`, `test-and-mark-stable.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `internal-review.yml`, `review_autofix_sweep.yml`, `lint-pr-body-auto-close.yml` and `sync_ai_labels.yml` (selected regions).

**Limits.** No byte-count tool was available, so every character count in Section 4 is an estimate from line counts and the line widths I saw. I did not read `agents.md` or the `.github/ai/` files in full; I only searched `label_contract.v1.json`.

**Already covered in this report, not repeated here:**
- The missing `PR_CLOSED` guard on "Checkout PR head branch" (`review_autofix.yml:2780`), Executive Summary item 1.
- The missing `VALIDATE_OUTCOME` line (`validate.yml:1583-1601`).
- The Semble state file keyed by `RUNTIME_DIR` (`semble_helpers.sh:13`).
- The shared-PAT overcount in `GH_PAT_BUDGET used_in_job` (`gh_helpers.sh:68-71`).

### Section 1: Bug & Correctness Sweep

**BUG-001**
- **File:** `.github/workflows/cancel_on_pr_close.yml:126-149, 258-284` (with `.github/workflows/internal-review.yml:2-8, 50`)
- **Severity:** High · **Category:** `bug`
- **Description:** The PR-close cleanup misses two kinds of review run for the closed PR.
  - **`workflow_dispatch` runs are never cancelled.** Both listing calls hard-code `-f event=pull_request` (lines 133 and 143), and the PR filter adds `select(.event == "pull_request")` and `select(.head_branch == $head_ref)` (lines 272-275). Dispatched review runs are named `Internal: AI Review & Autofix [pr:N]` and run on the default branch (`internal-review.yml:2-8`), so the cleanup can never match them.
  - **The `pull_requests` filter may also drop `pull_request` runs.** The filter also requires `.pull_requests[]` to contain the closed PR (lines 273-274). The evidence in this report fits this: run 38664219310 was a `pull_request` run with no `[pr:N]` suffix, was still in progress when PR 7403 closed at 01:51:20, ran on until 02:03:25 and failed. The cancel job ran at the close (38667886930), and that run was not cancelled. Inference: GitHub may stop listing a closed PR in a run's `pull_requests` array, so the filter matches nothing. [NEEDS VERIFICATION]
- **Recommended fix:**
  1. Pass `CPR_PR_CLOSED_AT: ${{ github.event.pull_request.closed_at }}` through `env:`, defaulting to empty (§8). In the PR path, match `head_branch == $head_ref` and `head_repository.full_name == $repo`, and accept either an empty `pull_requests` array or one that contains the PR.
  2. When `CPR_PR_CLOSED_AT` is set, also require `created_at < $closed_at`. Without this the post-merge run started by the same `closed` event would also be cancelled (`internal-review.yml:55`).
  3. Add a dispatch-run lookup: `actions/workflows/internal-review.yml/runs?event=workflow_dispatch&status=queued|in_progress`, matched on the exact `display_title` and checked for provenance with the `dispatch_pr` rule at `review_autofix_sweep.yml:376-385`. That is 2 extra calls per close; record the §14 audit in a comment.
  4. Log `CANCEL_ON_CLOSE_MATCH linked=<n> unlinked_same_branch=<n> dispatch=<n>` and register the prefix in an `agents.d/` fragment (§25).

**BUG-002**
- **File:** `.github/workflows/review_autofix.yml:4911-4930, 5002-5021`; `scripts/review_autofix_step_post_editor_summary.sh:97-117`; `scripts/review_autofix_step_assemble_failure_evidence.sh:63-75`
- **Severity:** Medium · **Category:** `bug`
- **Description:** Four places compute the `review-autofix-failure:v1` marker, and they do not use the same inputs.
  - The comment beside each copy says it uses the "Same evidence files and reason precedence as 'Assemble failure evidence', so every failure comment of one run carries one fingerprint."
  - The canonical call adds `--evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt"` (line 69), `--evidence-out` (line 71) and `"${provider_outage_reason_args[@]}"` (line 74). The three inline copies have none of these, and `post_editor_summary.sh` adds its own `--failure-reason`.
  - If the fingerprint hashes evidence content, a run with resolver stderr or a provider outage posts comments whose markers differ. The gate's identical-failure cap could then miss them. [NEEDS VERIFICATION: whether `autofix-failure-fingerprint` hashes `resolver_stage_stderr.txt` content.]
- **Recommended fix:** Extract one function, `autofix_failure_marker_suffix <failure_reason-or-empty>`. The four call sites and the suggested module are in DUP-004. Make the function pass the full canonical argument list, including the resolver evidence and the provider-outage reason. "Assemble failure evidence" runs later in the job (line 5305), so its exported `AUTOFIX_FAILURE_MARKER` cannot be reused by the earlier Telegram steps.

**BUG-003**
- **File:** `scripts/orchestrate_poll_process.sh:15977, 15986, 19580`; `.github/workflows/clarify.yml:863`; `.github/workflows/test-and-mark-stable.yml:1057`
- **Severity:** Low · **Category:** `bug`
- **Description:** These calls pass `sort=created&direction=desc|asc` to the per-issue comments endpoint, `repos/{o}/{r}/issues/{n}/comments`. As far as I know, that endpoint accepts only `since`, `per_page` and `page`, so the parameters are ignored. [NEEDS VERIFICATION against GitHub REST docs]
  - **Poller:** no functional impact, because the extractors use `max_by(.created_at)` (`orchestrate_poll_process.sh:15929, 15937`).
  - **Release-gate Phase 2:** the single page at `test-and-mark-stable.yml:1057` returns the oldest 100 comments, not the newest. `COMMENT_COUNT` stops at 100 and `LATEST_COMMENT_UPDATED` stops changing once an issue has more than 100 comments.
- **Recommended fix:** Remove the ignored parameters from the poller and `clarify.yml` calls (no behaviour change). At line 1057, either paginate with `--paginate | jq -s 'add'` or use the `since=` parameter with the last-seen timestamp.

**BUG-004**
- **File:** `.github/workflows/implement.yml:5150-5151`
- **Severity:** Low · **Category:** `bug`
- **Description:** The step runs under `set -euo pipefail` (line 4941). It posts the no-op comment with `gh_retry gh api ".../comments" -f body="${noop_body}"`, which has no `|| true` and no `>/dev/null`. `gh_retry` gives a POST create only one attempt (`gh_helpers.sh:1060-1062`). A single transient failure therefore stops the step before the `ai:implementation-failed` label is applied (lines 5156-5167), and that label is what the orchestrator re-processes. [NEEDS VERIFICATION: whether "Comment on issue failure" at line 6119 re-labels.]
- **Recommended fix:** Apply the label first, then post the comment with `>/dev/null 2>&1 || echo "::warning::..."`. This matches the guards in the same step at lines 4987-4988 and 5030-5031.

**BUG-005**
- **File:** `.github/workflows/test-and-mark-stable.yml:1404-1409`
- **Severity:** Low · **Category:** `bug`
- **Description:** Phase 3b reads a single unscoped page with `per_page=50` and has no `display_title` filter. The comment in Phase 2 (lines 1062-1069) says `per_page=50` truncation already broke release v1.13.5. The terminal decision is protected by `scoped_implement_runs_complete`, but the activity signals (`IMPL_RUN_TOTAL`, `IMPL_RUN_IN_PROGRESS`) can count another issue's Implement runs or miss ours.
- **Recommended fix:** Use `per_page=100` and add `select(.display_title == $title)`, the same as `find_latest_scoped_run_field`.

**BUG-006**
- **File:** `scripts/lint_pr_body_auto_close.py:196-213`; `.github/workflows/lint-pr-body-auto-close.yml:58-69`
- **Severity:** Low · **Category:** `bug`
- **Description:** The workflow writes the PR title and body into one file, and the script scans that file with `markdown=True`. Inline code spans are therefore masked in the title as well (`INLINE_CODE_RE.sub("", raw_line)`). The title becomes the squash commit subject as plain text, so `` `Fixes #N` `` in a title would pass the lint. [NEEDS VERIFICATION: whether GitHub honours keywords inside backticks in a commit subject.]
- **Recommended fix:** Add a `--pr-title-file` argument and scan the title with `markdown=False`. Keep the existing `--pr-body-file` behaviour (§10 additive).

**BUG-007**
- **File:** `.github/workflows/cancel_on_pr_close.yml:14-16`; `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
- **Severity:** Low · **Category:** `bug`
- **Description:** The 5-minute schedule and every `pull_request: closed` event share the single group `cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false`. GitHub keeps only one pending run per group. A PR-close run waiting behind a running scheduled run is therefore replaced by the next scheduled tick, and the scheduled path has the same linkage filter as BUG-001 (lines 246-252). [NEEDS VERIFICATION]
- **Recommended fix:** Key the event path on the PR, for example `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event.pull_request.number || 'schedule' }}`, so close events never compete with schedule ticks.

**SEC-001**
- **File:** `scripts/orchestrate_poll_process.sh:15926-15938, 20015-20035, 20226-20227, 16008-16015`
- **Severity:** Medium · **Category:** `security`
- **Description:** The standalone stall state is read from any comment that contains `<!-- AI_STANDALONE_STALL_STATE_V1`. The selector is `[.[] | select((.body // "") | contains($marker))] | max_by(.created_at)`, with no author or association filter. The comment lists passed in are not trust-filtered either (lines 20015-20033).
  - Any commenter on a public repo could post a newer marker comment with chosen fields, such as `status_since_ts` or `stall_recovery_count`. That could force or suppress stall recovery.
  - The `has_marker` check at line 20035 lets the same comment pull a non-pipeline issue into the stall loop.
  - `write_standalone_state_json` then PATCHes that comment id (line 16014).
  - By contrast, the tracking-issue state path filters by login (`trusted_comments_json`, line 3919).
- **Recommended fix:** Pass the trusted login from `unblock_trusted_login` / `UNBLOCK_TRUSTED_LOGIN` (already used at line 20106) into both extractors. Add `select(((.user.login // .author.login // "")) == $login)` to them and to the `has_marker` count at line 20035. Fail closed to the default state JSON when the login is unknown. [NEEDS VERIFICATION: the GraphQL-cache comment shape at lines 17900-17902 exposes `author{login}`.]

**SEC-002**
- **File:** `.github/workflows/implement.yml:5171-5181`
- **Severity:** Medium · **Category:** `security`
- **Description:** Line 5181 runs `git remote set-url origin "https://x-access-token:${{ secrets.GH_PAT }}@github.com/${{ github.repository }}"`.
  - The PAT is substituted into the generated step script.
  - It is written into `${GITHUB_WORKSPACE}/.git/config` and stays there for every later step in the job, including the workspace hooks at lines 6372-6376 and the log and artifact uploads.
  - `GH_TOKEN` is already in the step's `env` (line 5172).
- **Recommended fix:** Set the remote to `https://github.com/${GITHUB_REPOSITORY}` and authenticate the push with the existing `GH_TOKEN` env. Two patterns already in this repo do that: the credential-helper form at `update_workflows.yml:2069`, or the per-command `http.extraHeader` form at `review_autofix.yml:344-351`.

**SEC-003**
- **File:** `.github/workflows/implement.yml:5078-5096`; `scripts/orchestrate_poll_process.sh:15879-15910`
- **Severity:** Low · **Category:** `security`
- **Description:** The no-op ancestry cap follows `Re-issued from #N` from the issue body. It counts any comment on the parent that matches `produced no repository changes`, whoever wrote it. Inference: a non-collaborator comment, or a crafted body reference, can make the step close an issue as "already implemented" (`ai:closed`) instead of re-issuing it. [NEEDS VERIFICATION]
- **Recommended fix:** In the comments `--jq` filter, count only comments from `github-actions[bot]` or the trusted PAT login. Follow the parent link only when the issue author is trusted (the same check as the `ISSUE_AUTHOR_ASSOCIATION` test at `implement.yml:805`).

**SEC-004**
- **File:** `.github/workflows/clarify.yml:66-71`
- **Severity:** Low · **Category:** `security`
- **Description:** `echo "Comment author: ${{ github.event.comment.user.login }}"` puts event data straight into the script. GitHub's login character set makes this hard to exploit, but it breaks the repo's env-only rule. The same step also interpolates `github.event_name` and `github.event.action`.
- **Recommended fix:** Move these into `env:` and read them as `${COMMENT_USER_LOGIN}` and so on. `COMMENT_USER_LOGIN` is already defined at line 523, so reuse it.

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001**
- **File:** `scripts/orchestrate_poll_process.sh:42-101` (`replay_failed_reclarify_commands`)
- **Severity:** Low · **Category:** `api-batching`
- **Description:** For each issue labelled `ai:reclarify-requeue`, the loop makes two calls: `gh api repos/.../issues/${issue_num}` (line 55) and a paginated comments read (line 58).
  - **Current calls:** 2 + 2N, plus up to 2 writes per issue.
  - **Proposed calls:** 2 + ceil(N/25), with a REST fallback only for issues that have more than 100 comments.
  - The comments must be complete for the replay check to be safe (line 59), so the GraphQL batch needs a `hasPreviousPage` fallback. [NEEDS VERIFICATION]
- **Recommended fix:** Extend `_fetch_blocked_issue_routing_graphql` (lines 102-142; same file, same alias pattern). Use `comments(last: 100){ pageInfo{ hasPreviousPage } nodes{ databaseId body createdAt authorAssociation author{login __typename} } }`, plus `state` and `labels`. Fall back to the per-issue REST read only where `hasPreviousPage == true`. If the batch fails, return and let the next tick retry, matching the existing fail-open contract.

**BATCH-002**
- **File:** `scripts/orchestrate_poll_process.sh:6791-6814`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** The loop makes one `gh_retry _safe_gh_jq "repos/.../issues/${blocker_issue}" --jq '.state'` call per blocker fix-up issue.
  - **Current calls:** N.
  - **Proposed calls:** ceil(N/50).
- **Recommended fix:** Use one aliased query, `i<N>: issue(number:N){ state }`. Follow the batching contract of `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:152-217`) or `_fetch_linked_pr_status_graphql`. Keep the current `unknown` handling for any alias that comes back null.

**BATCH-003**
- **File:** `scripts/orchestrate_poll_process.sh:9036-9083`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** The superseded-check makes calls in three nested steps:
  - One timeline walk per wave issue (`_issue_cross_ref_pr_numbers_unique`, line 9038).
  - For each cross-referenced PR, `_fetch_pr_json` (line 9056).
  - For each PR, a paginated `pulls/${pr_num}/files` read (line 9069).
  - **Current calls:** I + P × (1 + file pages).
  - **Proposed calls:** I + ceil(P/25), plus REST only for PRs with more than 100 files.
- **Recommended fix:** Batch the P PR reads into one GraphQL alias set, `pr<N>: pullRequest(number:N){ state merged files(first:100){ nodes{path} pageInfo{hasNextPage} } }`. Extend `_fetch_linked_pr_status_graphql`, which already caches PR state, rather than adding a new helper. Keep `SYNC_SUPERSEDED_CONFIDENT=false` when a batch fails.

**API-001**
- **File:** `scripts/orchestrate_poll_process.sh:14508-14621` (with `set_issue_phase_label` at lines 4083 and 4106-4108)
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** Each validation-failure branch reads the tracking labels into `_tracking_labels` (for example lines 14508, 14555 and 14616). It then calls `set_tracking_phase_label`, which reads the labels again (line 4083) and edits once, and then makes two more unconditional `gh issue edit --remove-label` calls for `ai:validate-failed` and `ai:harness-broken` (for example lines 14559-14560).
  - **Current calls:** 2 label reads + 3 edits.
  - **Proposed calls:** 1 read + 1 edit.
- **Recommended fix:** Add an optional extra-removals argument to `set_issue_phase_label`. It should accept the already-fetched labels JSON and fold the two removals into its single `edit_args` call; the existing present-label filter avoids the "not found" error. Callers that omit the argument keep today's behaviour.

**API-002**
- **File:** `.github/workflows/review_autofix_sweep.yml:285`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** The sweep makes one `gh api "repos/${REPOSITORY}" --jq .default_branch` call per tick to get the default branch. For `workflow_dispatch` runs, `github.event.repository.default_branch` already has it. [NEEDS VERIFICATION: whether `schedule` payloads carry `repository`.]
  - **Current calls:** 1 per tick.
  - **Proposed calls:** 0 when the payload has the field, 1 otherwise.
- **Recommended fix:** Add `SWEEP_EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}` to `env:`, and call the API only when it is empty.

**API-003**
- **File:** `.github/workflows/review_autofix_sweep.yml:335-348, 437-439`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** The snapshot makes 3 status listings for each of 2 workflows, so 6 paginated calls per tick. When `review_autofix.yml/runs` returns 404 (rc 2 with status 404, which the code treats as "no runs", line 344), it still makes the other two status calls for that workflow.
  - **Current calls:** 6 per tick.
  - **Proposed calls:** 4 per tick when the second workflow returns 404.
- **Recommended fix:** In `snapshot_active_review_runs`, `break` out of the status loop on the first 404 for that workflow. This uses the existing `GH_API_RETRY_LAST_STATUS` check; no new calls are added.

**Fail-open and backoff check:** `gh_api_retry` (`gh_helpers.sh:903-1028`) and `gh_retry` back off exponentially and classify failures properly. The test helper `gh_api_safe` (`comprehensive_test_and_release_gh_api.sh:15-47`) retries only on rate limits; see CONSIST-001.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001**
- **File:** `ensure_label_exists` (or an equivalent) is defined 9 times:
  - `scripts/label_helpers.sh:157`
  - `scripts/validate_process.sh:1204`
  - `scripts/orchestrate_poll_process.sh:3995`
  - `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`
  - `scripts/review_autofix_step_mark_ready_to_merge.sh:15-26`
  - `scripts/review_rb_judge.sh:924-935`
  - `.github/workflows/review_autofix.yml:1137, 1555, 4380-4388`
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - `mark_ready_to_merge.sh:15-26` and `review_rb_judge.sh:924-935` hard-code the same color and description table.
  - Raw `gh label create` is also repeated with no retry in `workflow-log-analysis.yml:1182, 1960, 2558`, and inline in `implement.yml:4975, 5022, 5109, 5160, 5920, 5972, 6151, 6172`.
  - Colors and descriptions drift between copies. For example, `ai:needs-human` is `b60205` at `implement.yml:5023`, while `ai:review-blocked` is `e11d48` at `review_autofix.yml:4385`.
- **Recommended fix:** Keep `scripts/label_helpers.sh` as the one owner. Give it `ensure_label_exists <label> [repo]`, with colors and descriptions read from `.github/ai/label_contract.v1.json` (already used by `ai_labels.py`). Replace the local definitions with `source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh"`, keeping a `type ensure_label_exists || ...` fallback for consumer repos (§8).

**DUP-002**
- **File:** 12 inline `GH_PAT_BUDGET` start/end bodies in `.github/workflows/review_autofix.yml`:
  - `:362-374`, `:690-707`, `:725-737`, `:908-934`, `:936-948`, `:1000-1017`, `:1074-1086`, `:1409-1435`, `:1477-1489`, `:1737-1760`, `:1913-1925`, `:5666+`
  - plus `.github/workflows/clarify.yml:75-87`, `:2079-2090`, `:2138+`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** Each inline body re-implements `gh_pat_budget()` (`scripts/gh_helpers.sh:31-77`), but without its header-based override (lines 52-60). `clarify.yml` mixes the inline start (`:79-87`) with the helper's end (`:2028`), so start and end can be read from different sources. The copies also add bytes to `review_autofix.yml`, which is close to the 480,000-byte guard (§24).
- **Recommended fix:** Each step becomes `source "${GH_API_RETRY_HELPERS:-scripts/gh_helpers.sh}" && gh_pat_budget start review_autofix gate "${GH_PAT_BUDGET_FILE}"`, using the `GH_API_RETRY_HELPERS` bootstrap at `review_autofix.yml:331-360`. Add that bootstrap to the other jobs that need it. Keep the inline body only as the fallback when the helper is missing. The contractual `GH_PAT_BUDGET` line format is unchanged.

**DUP-003**
- **File:** The "Finalize workspace metadata" and "Finalize workspace contents" bodies appear 3 times:
  - `.github/workflows/implement.yml:1604-1634`
  - `.github/workflows/validate.yml:761-790`
  - `.github/workflows/review_autofix.yml:2399-2430`
- **Severity:** Low · **Category:** `duplication`
- **Description:** The three copies are about 95% the same. Their `if:` guards differ, and `implement.yml` leaves out `WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE="true"` (see CONSIST-003).
- **Recommended fix:** Add a `finalize-metadata` subcommand to `scripts/workspace_init.sh`. It reads `WORKSPACE_META_PATH`, `WORKSPACE_META_CACHE_KEY`, `WORKSPACE_META_RESTORE_PREFIX_ISSUE` and `WORKSPACE_CACHE_HIT` from env, with defaults (§8), and computes `WORKSPACE_CACHE_MATCHED_KEY` itself. The three steps then pass `${{ }}` values only through `env:`, which also takes them out of expression evaluation.

**DUP-004**
- **File:** The failure-marker invocation appears 4 times: `review_autofix.yml:4915-4930`, `review_autofix.yml:5006-5021`, `review_autofix_step_post_editor_summary.sh:101-117`, `review_autofix_step_assemble_failure_evidence.sh:63-75`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** These copies have diverged; see BUG-002.
- **Recommended fix:** Create `scripts/review_failure_marker_helpers.sh` with `autofix_failure_marker_suffix()` (failure reason as an optional first argument, printing `\n\n<marker>` or nothing). Before creating it, check that the module and function names are unused (§10). Source it from both scripts and both workflow steps. Add it to `REQUIRED_BOOTSTRAP_SCRIPTS` per the `agents.md` "Workflow file size limit" pattern.

**DUP-005**
- **File:** `.github/workflows/implement.yml:5078-5096` duplicates `count_noop_ancestors()` at `scripts/orchestrate_poll_process.sh:15879-15910`
- **Severity:** Low · **Category:** `duplication`
- **Description:** The loop, the marker, the `awk` sum and the fail-open behaviour are the same. Only the transport differs (`gh api` versus `_safe_gh_jq`).
- **Recommended fix:** Move `count_noop_ancestors` into `scripts/gh_helpers.sh`, or a new `scripts/reissue_helpers.sh`. Have `implement.yml` call `ancestor_noop_count="$(count_noop_ancestors "${ISSUE_NUMBER}" "${threshold}")"`, and apply the SEC-003 author filter in that single copy.

**DUP-006**
- **File:** `.github/workflows/review_autofix.yml:771-800` and `:1106-1135`
- **Severity:** Low · **Category:** `duplication`
- **Description:** Two full inline `gh_retry()` re-implementations, each about 25 lines, that buffer stdout (#5495). Ten other steps fall back to the passthrough stub `gh_retry() { "$@"; }` instead (see CONSIST-002).
- **Recommended fix:** In those two jobs, source the bootstrapped helper (`GH_API_RETRY_HELPERS`, `review_autofix.yml:331-360`) and keep one compact fallback. This also reduces the file's size (§24).

**DUP-007**
- **File:** `.github/workflows/test-and-mark-stable.yml:1021-1043` (Phase 2 `capture_run_id`) and `:1336-1358` (Phase 3b `capture_run_id`), plus the `RATE_LIMIT_BACKOFF`, `LAST_ACTIVITY` and `PREV_STATE` scaffolding at `:786`, `:940`, `:1254`, `:1875` and `:3554`
- **Severity:** Low · **Category:** `duplication`
- **Description:** Five wait loops repeat the same inactivity timer and run-ID capture logic, with small differences between them.
- **Recommended fix:** Move `capture_scoped_run_id <name_re> <created_after> <title> <max_pages>` into `scripts/comprehensive_test_and_release_gh_api.sh`, next to `find_latest_scoped_run_field`, and call it from each phase. This also removes most of the bodies flagged in EXPR-002 and EXPR-003.

### Section 4: Expression Size Limit Risk Assessment

The estimates below come from line counts and the line widths I saw, after removing the 10-space YAML indent. To verify a count, for example: `awk 'NR>=530 && NR<=827' .github/workflows/clarify.yml | sed 's/^          //' | wc -c`. Every count here is **[NEEDS VERIFICATION]**.

**EXPR-001**
- **File:** `.github/workflows/clarify.yml:529-827` ("Decide clarify route")
- **Severity:** High · **Category:** `expression-limit`
- **Description:** About 298 lines with many 200-340-character lines. Estimated **19,000–21,000 characters**, leaving roughly 0–2,000 of headroom. The only reason the body counts against the limit is six `${{ github.repository }}` uses (lines 738, 747, 766, 775, 780, 795). The same block already uses `${GITHUB_REPOSITORY}` at lines 606, 608, 658, 659 and 721. Recent features (#7328, #7370) have added blocks here, so it keeps growing.
- **Recommended fix:** Replace the six interpolations with `${GITHUB_REPOSITORY}`. The body then has no expressions, so the limit no longer applies; this is a minimal and behaviour-neutral change. As a follow-up, move the release and capacity-wait classifiers into `scripts/clarify_route_classify.sh`.

**EXPR-002**
- **File:** `.github/workflows/test-and-mark-stable.yml:1240-1500` ("Phase 3b: Wait for PR creation")
- **Severity:** High · **Category:** `expression-limit`
- **Description:** About 260 lines with several 150-300-character lines (1298-1305, 1369, 1406-1409, 1475, 1487, 1498). Estimated **17,500–19,000 characters**, leaving about 2,000–3,500. The only interpolation is at line 1278 (`steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after`).
- **Recommended fix:** Add `IMPL_CREATED_AFTER_INPUT: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` to the step `env:`, after checking the name is unique (§10). Line 1278 becomes `IMPL_CREATED_AFTER="${IMPL_CREATED_AFTER_INPUT:-}"`, and the body has no expressions.

**EXPR-003**
- **File:** `.github/workflows/test-and-mark-stable.yml:933-1200` ("Phase 2: Wait for plan")
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** About 267 lines. Estimated **15,500–17,000 characters**, leaving about 4,000–5,500. Three interpolations, at lines 951, 963 and 981, all `steps.create-issue.outputs.created_after`.
- **Recommended fix:** Add one step env var, `PLAN_CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` (check the name is unique first), and use `${PLAN_CREATED_AFTER}` in the three call sites.

**EXPR-004**
- **File:** `.github/workflows/implement.yml:4940-5167` ("Handle no-op implementation")
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** About 227 lines with long `printf` prose. Estimated **15,000–16,500 characters**. The interpolations are at lines 4945, 4947, 5150, 5158, 5160, 5164 and 5166, and all are `github.server_url`, `github.repository` or `github.run_id`.
- **Recommended fix:** Use `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`, and reuse `${REPO}` (set at line 4947). Then, as DUP-005 describes, call the shared helper instead of the inline ancestry loop.

**EXPR-005**
- **File:** `.github/workflows/review_autofix.yml:4995-5133` (about 138 lines, estimated 9,500–10,500 characters), plus the shorter interpolated bodies at `:2836`, `:3346`, `:3354`, `:4849-4857`, `:4908-4909`, `:4976`, `:5152-5157` and `:5438-5439`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** Every interpolation in these bodies is a `github.*` value that the runner already exports as `GITHUB_*`. No single block is near the limit today, but the alert bodies grow with each incident note.
- **Recommended fix:** Replace them with `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. The bodies then have no expressions, and the file shrinks (§24).

**Large `run:` bodies with no interpolation** are not subject to the limit, so they are not flagged:
- `test-and-mark-stable.yml:1828-2523` (Phase 4, about 696 lines) and `:2609-3263`
- `implement.yml:2549-3757`, `:3827-4219`, `:4239-4762` and `:1142-1549`
- `clarify.yml:1209-1449`
- `review_autofix.yml:1098-1408` and `:1524-1736`

**Large `if:` conditions:** `review_autofix.yml:4884`, `:4990` and `:5136` are each under 600 characters. They pose no risk.

**Workflow file size:** The real hard limit is 512,000 bytes (§24), not 1 MB. `tests/test_workflow_file_size_limit.py:27` enforces `WORKFLOW_FILE_SIZE_GUARD_BYTES = 480_000` in CI, so no workflow can approach 800 KB. `review_autofix.yml` is the file closest to the guard (see Reliability item 1's note in this report). DUP-002, DUP-006 and EXPR-005 are the cheapest ways to reduce its size.

### Section 5: Cross-Cutting Concerns

**DEAD-001**
- **File:** `.github/workflows/test-and-mark-stable.yml:858, 1053, 1384, 2138, 3704`
- **Severity:** Low · **Category:** `dead-code`
- **Description:** The guards `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && ...` can never be true. `gh_api_safe` changes `RATE_LIMIT_BACKOFF` (`comprehensive_test_and_release_gh_api.sh:11-34`), but every call site runs it inside `$(...)`, for example `LABELS=$(gh_api_safe_print ...)` at line 1049. The change happens in the subshell, so the parent's value stays 0. If one of these guards were ever made reachable, the one at line 1053 would `continue` without sleeping, which is a tight loop. [NEEDS VERIFICATION for the bodies at 858, 2138 and 3704.]
- **Recommended fix:** Either delete the guards, or record the backoff in a file the parent reads (for example `${RUNNER_TEMP}/gh_api_safe_backoff`) and add a `sleep "$POLL_INTERVAL"` before each `continue`.

**DEAD-002**
- **File:** `scripts/gh_helpers.sh:91, 108`
- **Severity:** Low · **Category:** `dead-code`
- **Description:** `gh_review_pr_state` accepts `merged` as a state, but REST `pulls/{n}.state` only ever returns `open` or `closed`.
- **Recommended fix:** Leave it as harmless, or add a comment that `merged` is never returned by REST, so readers do not assume merge detection happens here.

**CONSIST-001**
- **File:** `.github/workflows/workflow-log-analysis.yml:1182, 1960, 2558`; `.github/workflows/test-and-mark-stable.yml:1114, 1469`; `.github/workflows/review_autofix_sweep.yml:228-237, 285`; `scripts/orchestrate_poll_process.sh:7582, 7764`; `scripts/comprehensive_test_and_release_gh_api.sh:15-47`
- **Severity:** Low · **Category:** `consistency`
- **Description:** These calls use raw `gh api`, `gh issue create` or `gh label create` without the retry wrappers that are available in the same file. Two cases matter most:
  - `review_autofix_sweep.yml:228` runs under `set -euo pipefail`, so one 5xx on the PR listing aborts the whole tick.
  - `gh_api_safe` retries only on rate limits, never on 5xx.
- **Recommended fix:** Wrap the reads in `gh_api_retry` or `gh_retry`; `gh_api_retry` is already sourced at sweep lines 216-220. Add a 5xx and 408 retry branch to `gh_api_safe` that reuses the classification in `_gh_api_classify` (`gh_helpers.sh:824-874`).

**CONSIST-002**
- **File:** The passthrough stub `type gh_retry ... || gh_retry() { "$@"; }` appears at `.github/workflows/review_autofix.yml:1553, 2186, 3332, 4073, 4379, 4848, 4907, 4998, 5292, 5436`, `implement.yml:4943, 5176` and `cancel_on_pr_close.yml:109`
- **Severity:** Low · **Category:** `consistency`
- **Description:** When `gh_helpers.sh` fails to source, these steps lose both the retry and the protection against failed-attempt stdout (#5495), which the inline wrappers at `review_autofix.yml:771` and `1106` exist to provide.
- **Recommended fix:** Fall back to the bootstrapped `GH_API_RETRY_HELPERS` copy before using the stub, and emit `::warning::gh_retry fallback stub in use` when the stub is used.

**CONSIST-003**
- **File:** `.github/workflows/implement.yml:1588-1591, 1619-1624`
- **Severity:** Low · **Category:** `consistency`
- **Description:** Implement leaves out `WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE="true"`, which `validate.yml:745, 776` and `review_autofix.yml:2383, 2415` both pass. Implement keys the workspace on `ISSUE_NUMBER`, which is stable, so this may be intentional. [NEEDS VERIFICATION]
- **Recommended fix:** If it is intentional, add a comment saying so. Otherwise pass the flag, folded into DUP-003.

**SHELL-001**
- **File:** `.github/workflows/clarify.yml:905`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** `-f labels[]='ai:clarification'` leaves the bracket glob characters unquoted (SC2125-like). Lines 747 and 775 quote the same argument (`'labels[]=ai:clarification'`).
- **Recommended fix:** Quote the whole word: `-f 'labels[]=ai:clarification'`.

**SHELL-002**
- **File:** `.github/workflows/review_autofix.yml:4845-4848, 4904-4907, 4995-4998, 5148-5150, 5433-5436`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** These bodies have no `set -euo pipefail`, and the workflow has no `defaults.run.shell` (the only `shell: bash` is at line 2315). They therefore run as `bash -e {0}`, without `-u` or `pipefail`, so unset variables and pipeline failures pass silently.
- **Recommended fix:** Add `set -uo pipefail` at the top of each, and keep `|| true` on the intentionally failable calls.

**SHELL-003**
- **File:** `.github/workflows/test-and-mark-stable.yml:1191, 1491`; `.github/workflows/implement.yml:5083`; `scripts/orchestrate_poll_process.sh:15891`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** `[ $IDLE -ge $INACTIVITY_LIMIT ]` is unquoted (SC2086). `for hop in $(seq ...)` declares a loop variable it never uses (SC2034).
- **Recommended fix:** Quote the variables, and use `for _ in $(seq 1 "${threshold}")`.

**DEBT-001**
- **File:** `scripts/orchestrate_poll_process.sh:12-19`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:** ShellCheck's extended analysis is turned off for this file (about 27,700 lines, per the header) because it ran too long in CI (#6680). The findings in this audit concentrate here (SEC-001, BATCH-001 to BATCH-003, API-001, DUP-005).
- **Recommended fix:** Split the reclarify, standalone-stall and validation-outcome sections into `scripts/orchestrate_poll_*.sh` modules sourced by the poller. Turn extended analysis back on for the new files.

**DEBT-002**
- **File:** `.github/workflows/test-and-mark-stable.yml:4059-4430` (Phase 7)
- **Severity:** Medium · **Category:** `tech-debt`
- **Description:** The release gate's cancel-on-close check confirms only that an `internal-cancel-on-pr-close` run concluded `success` (lines 4368-4401). It never confirms that a target run was actually cancelled, so a cleanup that matches nothing (BUG-001) still passes the gate.
- **Recommended fix:** Before closing the smoke PR, record any queued or in-progress review run on its head. After the cancel run finishes, assert that each recorded run's conclusion is `cancelled`, or that none existed. Also assert the new `CANCEL_ON_CLOSE_MATCH` line.

**TODO/FIXME/HACK markers:** none found in `.github/workflows/*.yml`, `scripts/*.sh` or `scripts/*.py`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 3 | BUG-001, EXPR-001, EXPR-002 |
| Medium | 10 | BUG-002, SEC-001, SEC-002, BATCH-003, DUP-001, DUP-002, DUP-004, EXPR-003, EXPR-004, DEBT-002 |
| Low | 26 | BUG-003, BUG-004, BUG-005, BUG-006, BUG-007, SEC-003, SEC-004, BATCH-001, BATCH-002, API-001, API-002, API-003, DUP-003, DUP-005, DUP-006, DUP-007, EXPR-005, DEAD-001, DEAD-002, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001, SHELL-002, SHELL-003, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 3: `cancel_on_pr_close.yml`, `clarify.yml`, `test-and-mark-stable.yml`, plus an `agents.d/` fragment for the new log prefix | Medium |
| API call optimization | 2: `scripts/orchestrate_poll_process.sh`, `review_autofix_sweep.yml` | Medium |
| Code modularization | About 15, including `label_helpers.sh`, `gh_helpers.sh`, `workspace_init.sh`, a new `review_failure_marker_helpers.sh`, `review_autofix.yml`, `implement.yml`, `validate.yml`, `clarify.yml`, `orchestrate_poll_process.sh`, `validate_process.sh`, `review_rb_judge.sh`, 3 `review_autofix_step_*.sh` scripts, `comprehensive_test_and_release_gh_api.sh` | Large |
| Expression size reduction | 4: `clarify.yml`, `test-and-mark-stable.yml`, `implement.yml`, `review_autofix.yml` | Small |
| Medium/Low fixes | About 12, including `implement.yml`, `orchestrate_poll_process.sh`, `lint_pr_body_auto_close.py`, `lint-pr-body-auto-close.yml`, `comprehensive_test_and_release_gh_api.sh`, `workflow-log-analysis.yml`, `review_autofix_sweep.yml`, `review_autofix.yml`, `clarify.yml`, `gh_helpers.sh`, `test-and-mark-stable.yml`, `cancel_on_pr_close.yml` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-15)

**Method.** I re-read every Deep Audit Section 2 finding (BATCH-001 to BATCH-003, API-001 to API-003) against the current code. I then looked for net-new duplicates in these places:
- the `gh api user` identity reads across `scripts/*.sh` and `.github/workflows/*.yml`
- the per-PR and per-issue object re-reads in `review_autofix.yml`, `implement.yml`, `plan.yml` and `clarify.yml`
- the sweep tick in `review_autofix_sweep.yml`

On "retry loop" (a RISKY_SKIP trigger): I apply it to explicit bash retry, backoff or poll loops. I do not apply it to every call routed through the `gh_retry` / `gh_api_retry` wrappers, because nearly every call in the repo goes through them.

### Safety Tag Legend
- **`SAFE_TO_MERGE`:** the calls hit the same endpoint, or a strict subset of its fields. They have the same pagination, filters and token, sit in the same function or step with no mutation in between, keep the same error and retry behaviour, and do not cross a job or concurrency boundary. The implement stage may act on these directly.
- **`NEEDS_VERIFICATION`:** the change looks correct, but static reading cannot prove every precondition. Do the listed checks before implementing.
- **`RISKY_SKIP`:** the call is paginated, rate-limit-sensitive, auth-related, inside `orchestrate_poll_process.sh` or a race-defence path, or feeds a log key other workflows grep for. It is listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001: sweep active-run snapshot, 2 workflows × 3 statuses → 3 repo-level listings**
- **Safety tag:** `RISKY_SKIP`
- **Files:** `.github/workflows/review_autofix_sweep.yml:335-348` (status loop inside `snapshot_active_review_runs`) and `:437-439` (the per-workflow driver loop)
- **Current call count:** 6 paginated listing calls per tick (at least 6 requests).
- **Proposed call count:** 3 paginated calls (`status=queued|in_progress|pending` against the repo-level endpoint), filtered client-side by `.path`. The page count may rise.
- **Endpoints:**
  - current: `GET /repos/{o}/{r}/actions/workflows/{internal-review.yml|review_autofix.yml}/runs?status=…`
  - proposed: `GET /repos/{o}/{r}/actions/runs?status=…`
- **Evidence:** the same status loop runs once per workflow:
  ```bash
  for status in queued in_progress pending; do
    ...
    GH_TOKEN="${READ_TOKEN}" gh_api_retry --paginate -X GET "repos/${REPOSITORY}/actions/workflows/${workflow}/runs" \
  ```
  ```bash
  for wf in internal-review.yml review_autofix.yml; do
    snapshot_active_review_runs "${wf}"
  done
  ```
  The jq reduce already checks `.path` for provenance (`:380`), so a repo-level listing filtered on the two workflow paths would give the same input set.
- **Proposed fix (for manual review only):** call the status loop once against `repos/${REPOSITORY}/actions/runs`. Partition the runs by `.path` ending in `/internal-review.yml` or `/review_autofix.yml` before the existing reduce, and keep the `active_review_runs["${workflow}:…"]` key shape.
- **Safety rationale:** it is paginated (`--paginate`). It drives the dispatch-deduplication path that protects against the PR #3895 duplicate-dispatch incident (`:322-330`). A repo-level listing also returns queued runs from every fan-out workflow, so the number of pages could exceed the 6 calls it replaces.
- **Downstream signal:** do not auto-implement. A human must first measure the repo-level queued, in-progress and pending page counts during a busy window. They must also confirm that the per-workflow 404 handling (`:344`, `AUTOFIX_SWEEP_SNAPSHOT_INCOMPLETE`) has an equivalent, because a repo-level listing never returns 404 for a missing workflow file.

**MERGE-002: post-merge validate dispatch, per-issue label reads in the PR-text fallback → one aliased GraphQL read**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/review_autofix.yml:863` (fallback issue list built with `labels: null`) and `:868-902` (the loop, with the per-issue read at `:872`)
- **Current call count:** N, one `gh issue view --json labels` per text-referenced issue. This happens only after `closingIssuesReferences` came back empty (`:816-831`).
- **Proposed call count:** ceil(N/50).
- **Endpoint:** GraphQL `repository.issue…{labels}`. `gh issue view` is already GraphQL under the hood.
- **Evidence:**
  ```bash
  issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc '... map({number: tonumber, labels: null})')"
  ...
  if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
  ```
- **Proposed fix:**
  - At `:863`, replace the `labels: null` construction with one aliased query, `n<N>: issueOrPullRequest(number:N){ ... on Issue { number labels(first:100){nodes{name}} } ... on PullRequest { number labels(first:100){nodes{name}} } }`. Follow the alias and batch pattern of `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:157-217`).
  - Map each result to `{number, labels:[…]}` so the existing `labels_known=true` branch in the loop is used.
  - Keep the `:872` per-issue read only for aliases that come back null.
  - Use `issueOrPullRequest`, not `issue`: `gh issue view` accepts PR numbers, and plain `issue(number:)` would return null for them.
- **Safety rationale:** same step and same token. However, merging changes the error semantics: today one failed issue only `continue`s past that issue (`:879-880`), while a failed batch would affect every issue unless the per-issue fallback is kept.
- **Downstream signal:** before implementing, verify:
  1. The fallback keeps per-issue `continue` semantics on batch failure, either by falling back to the `:872` call per issue or by skipping with the same warning text.
  2. `issueOrPullRequest` returns labels for both types in this repo.
  3. N is realistically more than 1. If PR text usually references a single issue, the saving is 0, so drop the finding.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: sweep default branch, reuse the open-PR listing instead of `GET /repos/{r}` (an alternative to API-002)**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/review_autofix_sweep.yml:228-237` (the open-PR listing) and `:285` (the default-branch read)
- **Current call count:** 1 `GET /repos/{r}` per tick that has at least one candidate.
- **Proposed call count:** 0.
- **Endpoints:**
  - re-fetched: `GET /repos/{o}/{r}`
  - already fetched: `GET /repos/{o}/{r}/pulls?state=open`
- **Evidence:** the `/repos/{r}` read only runs after `total -eq 0` has already exited (`:273-276`). So whenever it runs, the PR listing has returned at least one PR, and each PR object carries `base.repo` (this repository). The listing's jq map drops that field:
  ```bash
  | jq -s '(add // []) | map(select(.draft == false) | {
    number,
    head_ref: .head.ref,
  ```
  ```bash
  sweep_default_branch="$(GH_TOKEN="${READ_TOKEN}" gh api "repos/${REPOSITORY}" --jq .default_branch 2>/dev/null || true)"
  ```
- **Proposed fix:**
  - In the `:228-237` pipeline, also derive `sweep_default_branch` from `(add // []) | map(.base.repo.default_branch // empty) | first // ""`. The listing is a single pipe today, so this means teeing the slurped pages to a temp file and running two jq passes. Leave the per-row snapshot schema in `ci-cancelled-pr-snapshot.json` unchanged.
  - Keep the `:286` regex check and the `default_branch_unavailable` skip. Keep the `:285` call only as the fallback when the derived value is empty.
  - Unlike API-002's event-payload approach, this works for `schedule` triggers too.
- **Safety rationale:** same token (`READ_TOKEN`) and same step, with no mutation in between. Failure behaviour is kept: a failed PR listing already aborts under `set -euo pipefail`, and an empty derived value falls back to today's call. The one thing static reading cannot prove is that `base.repo.default_branch` is present in list-endpoint items.
- **Downstream signal:** verify:
  1. `gh api repos/<r>/pulls?state=open&per_page=1 --jq '.[0].base.repo.default_branch'` returns the default branch.
  2. Draft PRs carry the same `base.repo` field. The derivation should read from the unfiltered pages, because a tick where every PR is a draft still reaches `:285`.

**REUSE-002: codex-agent `/user` reads, reuse the gate's already-exported PAT login**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:**
  - `scripts/review_single_issue_security_pass.sh:369-373` (gate mode), invoked from `.github/workflows/review_autofix.yml:4132-4148`
  - secondary candidates in the same job: `scripts/review_rb_judge_security_pass.sh:210` and `scripts/review_conflict_prepare.sh:1081-1083`
  - source of the reusable value: `scripts/review_autofix_step_evaluate_gate.sh:334` and `:1371`, exported at `review_autofix.yml:328`
- **Current call count:** 1 `GET /user` per security-pass gate evaluation, plus 1 each for the RB-judge marker check and the integration-sync conflict prepare, when those paths run.
- **Proposed call count:** 0 for each, whenever the gate output is non-empty.
- **Endpoint:** `GET /user`
- **Evidence:** the script's own comment states the gap. The gate does export the login, and another codex-agent step already consumes it (`review_autofix.yml:5620`, `AUTOFIX_STREAK_AUTHOR_LOGIN: ${{ needs.gate.outputs.fingerprint_cap_marker_author_login }}`):
  ```bash
  # The gate job also probes /user, but its identity is not exported to this job.
  SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
  ```
  ```bash
  echo "fingerprint_cap_marker_author_login=${gate_marker_author_login}" >> "${GITHUB_OUTPUT}"
  ```
- **Proposed fix:**
  - Add a new step env, defaulting to empty (§8), on the "Single-issue security pass" step (`review_autofix.yml:4134-4146`), for example `SECURITY_PASS_GATE_AUTHOR_LOGIN: ${{ needs.gate.outputs.fingerprint_cap_marker_author_login }}`. Grep the repo first to confirm the name is unique (§10).
  - In `review_single_issue_security_pass.sh:370`, use it when it matches `^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$`. Otherwise make the existing live call, then apply the existing `SECURITY_PASS_AUTHOR_LOGIN_FALLBACK`.
  - Update the stale comment at `:369`.
  - Follow `workflow_failure_heal_autofix_report.sh:216-228`, which already uses the env-first, live-fallback pattern.
- **Safety rationale:**
  - It crosses a job boundary (gate → codex-agent), so it fails the same-step precondition for `SAFE_TO_MERGE`.
  - The tokens differ: the gate uses `secrets.GH_PAT` (`:576`), while the security-pass step uses `secrets.GH_PAT || github.token` (`:4135`). They resolve to the same identity only when `GH_PAT` is set.
  - The gate fills the output only when `gate_fetch_marker_comments` ran (lazy, `:326-352`).
  - The value authenticates trust markers, so a wrong login would let forged markers count.
- **Downstream signal:** verify:
  1. When `GH_PAT` is empty, the gate output is empty, so the live path and `SECURITY_PASS_AUTHOR_LOGIN_FALLBACK=github-actions[bot]` behave as today.
  2. On "re-run failed jobs", codex-agent reuses the earlier attempt's gate outputs. Confirm that rotating `GH_PAT` to a different account between attempts is not a supported operation, or keep the live read when `github.run_attempt > 1`.
  3. `SINGLE_ISSUE_SECURITY_PASS_ENABLED` defaults to `'false'` (`:4138`), so the saving applies only where it is enabled.
  4. For `review_rb_judge_security_pass.sh:210` and `review_conflict_prepare.sh:1081-1083`, confirm that both run in the codex-agent job, where `needs.gate` is available, before extending the change to them.

**REUSE-003: implement job, second `GET /user` in "Unblock judge bulk-delete override"**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/implement.yml:1806` ("Fetch issue metadata" step, `GH_TOKEN: ${{ secrets.GH_PAT }}` at `:1754`) and `:2137` ("Unblock judge bulk-delete override", `GH_TOKEN: ${{ secrets.GH_PAT }}` at `:2131`)
- **Current call count:** 2 per run when an `override=bulk_delete -->` marker is present (`:2134`), otherwise 1.
- **Proposed call count:** 1.
- **Endpoint:** `GET /user`
- **Evidence:** both reads are in the same `implement` job and use the same token:
  ```bash
  pipeline_login="$(gh api user --jq .login 2>/dev/null || true)"
  ```
  ```bash
  pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"
  ```
  The comment at `:1803-1805` only rules out the reverse direction, where the later read would feed the earlier grant.
- **Proposed fix:**
  - At `:1806`, after a successful read, append a validated login to `$GITHUB_ENV` under a new, grep-verified unique name such as `IMPLEMENT_PIPELINE_LOGIN`, using the `^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$` check.
  - At `:2137`, use `${IMPLEMENT_PIPELINE_LOGIN:-}` first, then make the existing live call only when it is empty.
  - Keep the `login_unavailable` skip.
- **Safety rationale:** same job and same token, but different steps. That breaks the same-step precondition. The login also feeds a destructive-guard override, so a stale value matters.
- **Downstream signal:** verify:
  1. No step between `:1751` and `:2120` changes `GH_TOKEN` or re-authenticates `gh`.
  2. The `:1806` step's `if: env.SKIP_IMPLEMENT != 'true'` matches `:2129`, so the env var is always set or always empty on the paths where `:2137` runs.
  3. Writing a login, which is not a secret, to `GITHUB_ENV` is acceptable under the workflow's env-hygiene rules.

**REUSE-004: plan job, second `GET /user` in "Auto-approve clear plan"**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/plan.yml:926` ("Close superseded CI-repair issue (merged PR)" step, `:868-872`, `GH_PAT`) and `:2641` ("Auto-approve clear plan" step, `:2476-2480`, `GH_PAT`)
- **Current call count:** 2 when the plan gate returns `hold/identity_unavailable`, otherwise 1.
- **Proposed call count:** 1.
- **Endpoint:** `GET /user`
- **Evidence:**
  ```bash
  login="$(gh_retry gh api user --jq .login 2>/dev/null || true)"
  ```
  ```bash
  if printf '%s' "${plan_gate_result}" | jq -e '.decision == "hold" and .reason == "identity_unavailable"' >/dev/null 2>&1; then
    plan_gate_login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null || true)"
  ```
- **Proposed fix:**
  - At `:926`, export a validated login under a new, grep-verified unique name such as `PLAN_PIPELINE_LOGIN`.
  - At `:2641`, use `${PLAN_PIPELINE_LOGIN:-}` before the live call. Keep the live call as the fallback.
- **Safety rationale:** same job and token, but different steps. The `:926` read only runs when that step's conditions and helper lookup (`:916-920`) pass, so the env var is often unset.
- **Downstream signal:** verify:
  1. The conditions under which the `:868` step reaches `:926`. If it rarely runs, the saving is negligible, so drop the finding.
  2. Where `plan_gate_login` is initialised before `:2636`. Confirm that seeding it before the first `run_plan_gate` gives the same decision the current hold-then-retry path reaches.

**REUSE-005: intentional freshness re-reads, listed so they are not "optimised"**
- **Safety tag:** `RISKY_SKIP`
- **Files:**
  - `.github/workflows/implement.yml:1986-1992` ("Validate approval phase label") and `:2402` ("Recheck security dependency before edits"). Both re-read the issue already cached in `ISSUE_META_FILE` by `:723`, `:746` and `:1773`.
  - `.github/workflows/review_autofix.yml:2188` ("Check PR state (defense-in-depth)") and `:5294` ("Check PR state before failure alerts"). Both re-read the PR that the gate already read (`review_autofix_step_evaluate_gate.sh:87-88`).
- **Current call count:** 4 single-object reads per run on the relevant paths.
- **Proposed call count:** no change.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}` and `GET /repos/{o}/{r}/pulls/{n}`
- **Evidence:** the code documents the intent: `# Recheck just before advancing the phase. /approved must not bypass a still-open security prerequisite or a failed API read.` (`implement.yml:1984-1985`), and the step name "defense-in-depth" (`review_autofix.yml:2177`). The 2026-10-15 window's failure, run 38664219310, was a PR closing while its run was queued, which is exactly the race these re-reads defend against.
- **Proposed fix:** none. Keep them.
- **Safety rationale:** these calls exist to defend against TOCTOU and upstream races (PR closed or issue relabelled between steps). That is an explicit RISKY_SKIP trigger.
- **Downstream signal:** do not merge these into earlier snapshots. Any change needs a human to confirm that the race window it closes no longer exists.

### Dead Calls (DEAD-API-###)
No findings.

I checked three live-fallback reads whose results are always consumed when they run: `implement.yml:2035`, `plan.yml:2857` and `review_autofix.yml:4433`. None is dead. `implement.yml:2035` is unreachable in practice, because `:1992` has just written a non-empty REST issue object to `ISSUE_META_FILE`. It costs 0 calls, so there is nothing to save. Its comment at `:2023` ("fetched in 'Fetch issue metadata'") is stale, which is a Deep Audit documentation item, not an API saving.

### Cross-References to Deep Audit Section
- BATCH-001: RISKY_SKIP — It is in `orchestrate_poll_process.sh` and paginated (`:58`). Additional detail:
  - GraphQL reports the bot author as `login: "github-actions"` with `__typename: "Bot"`, not `github-actions[bot]`. The `.user.login == "github-actions[bot]"` filters at `:62` and `:72` therefore need a mapping, or replay will silently stop. [verify]
  - `.id` comparisons (`:72`, `:77`, `:87`) need `databaseId`.
  - `:51` might reuse `UNBLOCK_TRUSTED_LOGIN` if it is resolved earlier in the same tick. [verify ordering]
- BATCH-002: RISKY_SKIP — It is in `orchestrate_poll_process.sh` (`:6794`). Additional detail: REST `issues/{n}` returns a state for a PR number, but GraphQL `issue(number:)` returns null. Use `issueOrPullRequest`, or a blocker that is a PR becomes `unknown` and defers forever.
- BATCH-003: RISKY_SKIP — It is in `orchestrate_poll_process.sh` and paginated (`:9069`). Additional detail:
  - `_fetch_pr_json` (`:2215-2219`) is a plain REST read with no cache.
  - GraphQL `state` is `OPEN|CLOSED|MERGED` in upper case, while `_jq_field … 'open|closed|merged'` (`:9057`) matches lower case. Lower-case it, or every PR becomes "unable to fetch state".
  - Check whether `evaluate_sync_superseded_by_main` (`:9005`) runs after `STALL_MANAGED_LINKED_PR_CACHE` is populated (`:27542`) before extending `_fetch_linked_pr_status_graphql` (`:18083`).
- API-001: RISKY_SKIP — It is in `orchestrate_poll_process.sh` (`:14508-14621`). Additional detail:
  - `gh issue edit` makes its own GraphQL lookup before mutating, so each extra removal costs at least 2 calls.
  - The harness-error branch (`:14512`, `:14519`) can fold its add of `ai:harness-broken` into the same edit.
  - Verify that `ai:validate-failed` is not already in the phase contract's `.remove` list for `ai:validation-failed`.
- API-002: NEEDS_VERIFICATION — The event-payload approach depends on whether `schedule` payloads carry `repository`. REUSE-001 is a trigger-independent alternative with zero calls.
- API-003: RISKY_SKIP — The call is `--paginate` under `gh_api_retry` in the dispatch-deduplication snapshot (`review_autofix_sweep.yml:340`). Skipping later statuses after a 404 is probably safe, but it touches the `AUTOFIX_SWEEP_SNAPSHOT_INCOMPLETE` log path, so a human should confirm a 404 is deterministic per workflow within a tick.

### Summary Counts
| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 6 | MERGE-002, REUSE-001, REUSE-002, REUSE-003, REUSE-004, API-002 (cross-ref) |
| RISKY_SKIP | 7 | MERGE-001, REUSE-005, BATCH-001 (cross-ref), BATCH-002 (cross-ref), BATCH-003 (cross-ref), API-001 (cross-ref), API-003 (cross-ref) |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass. Every net-new candidate crosses a step or job boundary, changes per-item error semantics, or feeds a trust or race-defence decision. Once their verification steps pass, the best-value candidates are, in order: REUSE-002 (when `SINGLE_ISSUE_SECURITY_PASS_ENABLED=true`), REUSE-001, REUSE-003 and REUSE-004.