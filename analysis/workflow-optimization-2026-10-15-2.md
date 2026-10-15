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