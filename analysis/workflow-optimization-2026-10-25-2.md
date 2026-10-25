## Executive Summary

> Data caveat: `insufficient_data` is false, but the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox. I could not read `summary.json`, `errors/`, `slow/` or `recent/`. Findings rest on `analysis_context.json` (including `log_summary` rows) and on reading the workflow and script sources in this repo. The three failing runs have no visible log body.

- **Runner saturation is the biggest latency cost on short workflows (high confidence).** In 13 sampled runs, "All GitHub-hosted runners with label [ubuntu-latest] are busy" accounted for 41–227s, often 70–98% of the run. Examples: lint 40193755477 waited ~183s of 192s; review gate 40193016898 ~217s of 229s; cancel-on-close 40193166655 ~227s of 240s; post-merge 40192914297 ~220s. Each PR close or merge triggers 6+ workflows. Two of our own steps add avoidable runner-minutes on top (next two bullets). Impact: about 2–4 min off every PR-event workflow at peak.
- **The ai-memory `finalize-task` step clones the whole repository (high confidence).** `_clone_for_memory_branch` in `scripts/ai_memory_lib.py:3211` runs `git clone --no-tags --quiet <origin>` with no depth and no `--single-branch`. Telemetry shows `clone_ms=99651` (run 40193751420) and `clone_ms=119829` (run 40192470944), against `push_ms` of about 2s. Impact: about 95–115s saved per Issue-PR Status Sync run.
- **heal-pr-reconcile pays about 2 minutes of full-history checkout to log a noop (high confidence).** `internal-cancel-on-pr-close.yml:45-50` checks out with `fetch-depth: 0`, so every v1.x tag is fetched: 12:18:12→12:20:24 in run 40192470952 and ~12:23:57→12:26:02 in run 40192914193. The script then exits at `workflow_failure_heal_pr_reconcile.sh:104` with `noop reason=no_heal_issue` before any git command. All 3 sampled runs were noops. Impact: about 125–130s per PR close.
- **The two CI failures hit the same step and cannot be diagnosed from the telemetry (medium confidence).** Runs 40182895760 and 40188366227 both failed in `tests-release-and-log-analysis` / "Promote cycle and blocked-verdict unit tests", giving a 6.25% CI failure rate. The step prints a 40-line tail per failed shard (`ci.yml:1771-1774`) but no structured failed-test list, so the collector cannot tell a flaky test from a real regression.
- **review_autofix drives all model spend: 146.6M OR tokens over 115 calls in 16 runs (high confidence).** The aggregate cache-hit rate is about 81.6% (computed). Per-run rates range from 0.62 (40182881091) to 0.92 (40182804098). Each run sends a steady 1.4–3.5M uncached prompt tokens. The collector builds `or_phases` but does not export it, so no one can see which phase drives that volume.
- **Semble works without errors but each bootstrap is slow (medium confidence).** There were 14 bootstraps, 0 failed and 0 unused, averaging 44.3s each (620,188 ms total) to produce about 15.8 KB of context per query. Runs that bootstrap twice spend 66–102s on it (for example 40190300869: 101,650 ms). All 28 `SEMBLE_FALLBACK`s are contract-test fallbacks in CI, with 0 runtime fallbacks. That is healthy.

## Speed Optimizations

Ranked by expected latency reduction. Items 1, 2 and 4 are on the critical path; 3 and 5 also free shared runner capacity.

1. **Make the ai-memory branch clone single-branch and shallow (critical path for `issue_pr_status`).**
   - **Evidence:** `AI_MEMORY_TELEMETRY` `finalize-task` reported `clone_ms` of 99,651 (run 40193751420, total 279s) and 119,829 (run 40192470944, total 219s). `write_ms` was about 2.3s, `push_ms` about 1.7–2.2s and `push_attempts=1`.
   - **Root cause:** `scripts/ai_memory_lib.py:3211` clones every branch and the full history of the main repo, then fetches only `refs/heads/{memory_branch}` (line 3219). `scripts/memory_helpers.sh:885,1001` already uses `git clone --depth 1 --branch "${memory_branch}"` for the same branch, so this is an existing pattern.
   - **Change:** when `ls-remote` shows the branch exists, clone with `--no-tags --single-branch --branch <memory_branch> --filter=blob:none`. Keep the current full clone as the fallback when that fails or the branch does not exist. `--filter=blob:none` keeps commit history, so push-retry rebases still work.
   - **Savings:** about 95–115s per merged-PR status sync. This is roughly 40–50% of that workflow's wall clock, and it also frees runner time.
   - **Risk:** low. The fallback keeps today's behaviour.

2. **Gate heal-pr-reconcile's full-history checkout behind the noop check.**
   - **Evidence:** in runs 40192470952, 40192914193 and 40193751400, checkout took about 2 min and the reconcile logic about 0.5–3s, ending in `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`. One log was truncated by 579,065 chars of tag output.
   - **Root cause:** `fetch-depth: 0` at `internal-cancel-on-pr-close.yml:50`. History is only needed by `_retarget_heal`, which does its own `--no-tags` fetches (script lines 159–168) and merges.
   - **Change:** add a step before checkout that runs the same heal-issue list query used at script line 96 and sets `has_heal_issue`. Put `if: steps.<id>.outputs.has_heal_issue == 'true'` on the checkout. Alternatively, keep the checkout but add `filter: blob:none` and `fetch-tags: false`. Note that with `fetch-depth: 0`, actions/checkout fetches tags anyway, so the gate is the more reliable fix.
   - **Savings:** about 125s per PR close, roughly 75–90% of this job. It also releases one runner about 2 min earlier during merge bursts.
   - **Risk:** low. When the gate is false the script would have exited at line 104 anyway. Fail open: if the pre-check errors, run the checkout.

3. **Reduce the PR-event fan-out that saturates runners.**
   - **Evidence:** PR #8137 sync at 12:35:08 produced lint 40193755477 (183s queued), review gate 40193755526 (~188s queued) and readiness 40193755486 (13s queued). PR #8131 at 12:26:40 produced lint 40193123528 (134s) and readiness 40193123502 (139s). Readiness then exits immediately with "is not an orchestrator/project-* branch — readiness check does not apply" (40193123502, 40194591999).
   - **Root cause:** separate jobs, each with a full runner start and checkout, for a few seconds of logic each.
   - **Change:** move the head-ref condition into a job-level `if: startsWith(github.head_ref, 'orchestrator/project-')` on `integration-pr-readiness.yml`, so non-project PRs never request a runner.
   - **Savings:** about 2–3 min of queue removed from neighbouring jobs per non-project PR event at peak. This is an inference from the observed queue contention.
   - **Risk:** low. The script already no-ops on those refs. Confirm the check is not a required status for `ai/issue-*` PRs before gating; a skipped job reports success.

4. **Semble bootstrap: about 44s each, run twice in some review runs.**
   - **Evidence:** 14 bootstraps totalled 620,188 ms. Runs with 2 bootstraps: 40190300869 (101,650 ms), 40190232192 (97,246 ms), 40191860938 (95,892 ms), 40188366303 (84,046 ms).
   - **Root cause (inference):** each job in a review run builds the index again with `SEMBLE_BOOTSTRAP_MODE: lazy`.
   - **Change:** first add the diagnostic `SEMBLE_BOOTSTRAP ... job=<job> head_sha=<sha>` so we can tell whether the second bootstrap is for the same SHA. If it is, reuse the first job's index through the existing artifact upload and download pattern.
   - **Savings:** about 45s per two-bootstrap run, about 7 runs in the window.
   - **Risk:** medium. A stale index is a quality risk, so key reuse on `head_sha`.

5. **CI `tests-release-and-log-analysis` is the long pole (about 11.5–14 min).**
   - **Evidence:** 40192914914 ran 12:26:36→12:38:08. 40192470938 ran 12:20:30→12:34:56 after a 168s queue. CI p50 is 1263s and p95 1769s.
   - **Change:** the logs do not show where the time goes ("Test steps took seconds"). Add per-step `::notice::STEP_TIMING step=<name> secs=<n>` lines first, then shard the slowest steps the same way the promote-cycle step already does (`pc_shards=4`).
   - **Savings:** unknown until timings exist. Treat this as a micro-optimization until then.

## Cost Optimizations

Ranked by expected token savings. Only review_autofix records any model usage.

1. **Find what drives the steady 1.4–3.5M uncached prompt tokens per review run.**
   - **Evidence:**

     | Run | Uncached prompt | Cache read | Cache write | Hit rate | Calls |
     |---|---|---|---|---|---|
     | 40182881091 | 1,660,751 | 3,634,799 | 568,638 | 0.62 | 10 |
     | 40190300869 | 1,433,806 | — | 888,422 | 0.71 | — |
     | 40190232192 | 2,606,802 | — | 0 | 0.71 | — |
     | 40182804098 | 2,263,560 | 24,865,765 | 0 | 0.92 | 6 |

     Aggregate: uncached prompt 20.76M, cache read 118.68M, cache write 6.03M, completion 1.13M.
   - **Root cause:** cannot be determined. `or_phases` is collected (`cost_audit.py` aggregator dicts) but not emitted in `analysis_context.json`.
   - **Change:** export `or_phases` per run and per family in the collector output (additive field), then cut the prefix variance in whichever phase dominates (see Prompt Cache section).
   - **Savings estimate:** raising the low runs (0.62–0.74) to the 0.82–0.92 seen in 40191860938 and 40182804098 would move about 0.5–1M tokens per run from uncached to cached reads. That is roughly 5–10M uncached tokens over the window (inference).
   - **Quality risk:** none for the telemetry change.

2. **Semble value check.** It contributes about 15.8 KB (about 4k tokens) per query and about 6.9 sources per query, with `static_dup_bytes=0` and `echo_lines_dropped=0`, so it adds no duplicated noise. However, that is under 0.5% of a review run's 1.4M+ prompt tokens. The telemetry neither shows Semble reducing prompt expansion nor shows it adding noise; it is cost-neutral. Keep it enabled. Weigh it mainly on the 44s bootstrap latency (Speed #4).

3. **Serena** is disabled (`SERENA_ENABLED: false` in 40193755526, 40193016898, 40193751418), with 0 queries and 0 probes. There is nothing to evaluate.

4. **Avoidable reruns: none observed.** All runs show `run_attempt=1` and `retries=0`. The autofix sweep (40194115992) dispatched 0 runs and skipped 4 for `active_run`, so the dedup guard is working.

5. **Context-budget and break-glass pressure: none.** `context_budget_warn_count=0` and `break_glass_count=0` everywhere. There is no prompt-size risk signal.

## Reliability Improvements

1. **CI "Promote cycle and blocked-verdict unit tests": 2 failures, 6.25% of CI runs.**
   - **Evidence:** 40182895760 (09:59, 2060s) and 40188366227 (11:19, 946s), same job and step. Log bodies are unavailable.
   - **Root cause category:** unknown. It could be a test regression or a flaky test under 4-way parallel shards (inference: both runs overlapped heavy review_autofix activity).
   - **Fix (diagnostic logging):** in `ci.yml` after line 1773, emit one structured line per failed shard, so the collector can count recurrence per test ID across runs and separate flakes from regressions. pytest's default `-rfE` already prints `FAILED`/`ERROR` summary lines.
     ```bash
     pc_fail_ids="$(grep -E '^(FAILED|ERROR) ' "/tmp/promote_cycle_shard_${pc_shard}.log" | cut -d' ' -f2 | head -n 20 | paste -sd, -)"
     echo "PROMOTE_CYCLE_SHARD_FAIL shard=${pc_shard} rc=$(cat "/tmp/promote_cycle_shard_${pc_shard}.rc" 2>/dev/null || echo missing) tests=${pc_fail_ids:-none}"
     ```
   - **Impact:** turns a 6.25% opaque failure rate into a targetable one.
   - **Rollback:** the change only adds output and does not change the exit code.

2. **review_autofix "Checkout PR head branch" failure (run 40187110927, PR #8099, 449s).**
   - **Evidence:** a single failure at `review / codex-agent`. The step body (`review_autofix.yml:2832+`) runs under `set -euo pipefail`, which aborts on the first failing command.
   - **Root cause (inference):** the head ref was deleted or force-moved, or the head repo was inaccessible, between the gate and the checkout.
   - **Fix:** add a `trap` to that step that prints `CHECKOUT_HEAD_FAIL pr=<n> head_ref=<ref> head_repo=<repo> exit=<rc> line=<LINENO>` on ERR. If `workflow-templates/` has a copy of this step, update it too.
   - **Impact:** 1 of 44 review runs (2.3%). Mainly diagnosability.
   - **Rollback:** additive. `review_autofix.yml` size must stay under the 480,000-byte guard (§24); a one-line trap is negligible.

3. **Orchestrate poller: 21 cancelled runs (52.5% of 40) are benign supersession, not failures.**
   - **Evidence:** all were `cancelled_before_first_step`, for example 40194387079, 40194254795, 40193749601. The concurrency group `ai-orchestrate-poll-${{ github.repository }}` uses `cancel-in-progress: false` (`orchestrate_poll.yml:72-73`) with a `*/5` cron (`internal-orchestrate-poll.yml:5`), while the poll body runs about 12–17 min (40193186630: 12:32→12:44).
   - **Interpretation:** GitHub keeps one pending run per group and cancels older pending ones. This is healthy by design, but it means roughly two thirds of ticks are dropped and the effective poll interval equals the poll body time.
   - **Mitigation:** none needed for correctness. Have the collector report these as `superseded`, not `cancelled`, so they don't inflate failure dashboards.

4. **Semble shows as enabled but unavailable in the poller.**
   - **Evidence:** 40193186630 logs `SEMBLE_ENABLED: true`, `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false`, with no `SEMBLE_FALLBACK` line.
   - **Mitigation:** if the poller never queries Semble this is harmless. Otherwise it is a masked unavailability. Emit a single `SEMBLE_FALLBACK target=orchestrate_poll reason=unavailable` when enabled but unavailable, so this shows up in telemetry.

5. **Old queued runs that were never cleared.**
   - **Evidence:** 40193166655 warns that it "preserved 4 active run(s) without pull-request linkage". Three are old: 34748435167 and 34748435180 (2026-09-13), and 32215723558 (2026-08-19, `internal-issue-pr-status.yml`, `ai/issue-3768`).
   - **Risk (inference):** stale queued runs can hold concurrency-group pending slots.
   - **Mitigation:** extend the scheduled cleanup to cancel unlinked queued runs older than N days (default 7). Add the new env var with a default (§8).

## AI Memory Health

- **Telemetry found:** 2 `finalize-task` events, both `ok: true`, `did_push: true`, `final_state: merged` and `push_attempts=1`. They are issue 8126 (run 40193751420) and issue 8128 (run 40192470944).
- **Latency:** `clone_ms` was 99,651 and 119,829, against `write_ms` of 2,308 and 2,439 and `push_ms` of 1,699 and 2,171. The clone is about 95% of memory-operation time; fix in Speed #1.
- **retrieve:** no `retrieve` events appear in the visible evidence, so hit rate, `estimated_tokens` against budget, and the `keyword_method` mix (llm/plain/none) cannot be computed. There were no `fail_open: true` or `enabled: false` entries and no push-retry pressure.
- **Recommendation:** check that `retrieve` telemetry is emitted in the implement and review phases. Those runs (for example 40189021535 and 40182804098) have no excerpts in this window. Have the collector keep `AI_MEMORY_TELEMETRY` lines when truncating long logs (the poll log lost 876,101 chars).

## GH API Call Audit

| Workflow/job | Calls (evidence) | Pattern | Recommendation |
|---|---|---|---|
| review_autofix_sweep / sweep (40194115992) | `used_in_job=22`, remaining 4324 | 4 candidates, all `skipped_active=4` | Already batched. Short-circuit the heal backfill and security-hold sweeps when `candidates == skipped_active`; saves an estimated 5–10 calls per tick (inference). |
| review_autofix / post-merge-force-poll (40192914297) | `used_in_job=25` | per merge | Not enough detail to batch. Needs per-call logging. |
| review_autofix / post-merge-validate-dispatch | `used_in_job` 5 / 0 / 0 (40192914297, 40192470978, 40193751418) | `POST_MERGE_LINKED_ISSUES_JSON: []` every time, then PR-body fallback | GitHub only fills closing-issue links for PRs into the default branch, so orchestrator sub-PRs always take the fallback (inference). Add an issue-labels field to that lookup rather than a separate call. |
| lint_pr_body_auto_close | 1 paginated `pulls/<n>/commits` call per run | single call | Fine. |
| heal-pr-reconcile | 1 paginated `issues?labels=<heal>` call per PR close | repeated list per close | Acceptable. Reuse the result in the pre-checkout gate (Speed #2) instead of running it twice. |
| orchestrate_poll | `GH_PAT_BUDGET` is emitted (6 sites in `orchestrate_poll.yml`) but was cut by log truncation | — | **Data gap:** the collector must extract `GH_PAT_BUDGET` lines before truncating. |

- The PAT budget fell from `remaining=4846` (12:22) to `remaining=4236` (12:42) within one reset window (`reset=1792934056`), about 610 calls in 20 minutes. Most of these calls are not attributed in the visible logs. The likely source is the poller (inference).
- No HTTP 429s or secondary rate limits were observed.

## Prompt Cache & Memory System

- **cache_hit_rate:** the aggregate is about 0.816 (computed as read ÷ (read + uncached prompt + write), the same formula as the per-run values). The family field is `null` because one call lacked usage (`or_usage_unavailable_calls=1`, run 40181502655), which nulls that run's rate.
- **Fragmentation signals:**
  - Runs with large cache writes have the lowest hit rates: 40182881091 (568,638 written, 0.62) and 40190300869 (888,422 written, 0.71).
  - Runs with 0 cache writes reach 0.71–0.92.
  - Inference: per-run prefix churn from PR-specific content placed before stable instructions forces new cache entries.
  - Recommended change: order prompts as static role/rubric, then repo context, then PR diff and findings, so dynamic content comes last. Check this against the per-phase `or_phases` export (Cost #1) before changing prompts.
- **Collector fix:** compute `cache_hit_rate` over the calls that do have usage instead of nulling the whole run. Add `cache_hit_rate_partial=true` when some calls lack usage, so run 40181502655 (4.5M cache-read tokens) is not dropped.
- **CONTEXT_BUDGET_WARN:** 0 events, so prompt growth is not yet eroding cache value.
- **Memory retrieval effectiveness:** not measurable; see AI Memory Health.

## Orchestrator Health

- **Project #8125 is progressing.**
  - Sub-PRs 8133 (#8126), 8134 (#8128) and 8136 (#8127) merged into `orchestrator/project-8125` between 12:17 and 12:35.
  - Integration PR #8137 readiness reports "3/6 sub-issues on #8125 still unchecked" (triage-dedup-fail-closed, label-ensure-run-cache, close-out-source-doc), in runs 40193016847 and 40193755486. That is the correct block.
  - #8137 uses `Refs #8125`, so §21 is respected and the lint passed.
- **Poller cadence:** the effective interval is about 12–17 min, not 5 min; see Reliability #3. Indicator to track: poll body duration p50/p95 (currently p50 291s, p95 1222s).
- **Promote and release:** `PROMOTE_CANDIDATE_FOLLOW outcome=frozen protected=true` (40193580777) under the 48h freeze cap. `AUTO_RELEASE_DISPATCHED` for stable b8544b1 (40194235051).
- **Workflow heal:** `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL outcome=complete candidates=0`. There were 65 heal runs, all skipped, so heal had nothing to do.
- **Clarify loops:** clarify_respond had 160 runs, of which 159 were skipped and 1 succeeded. There is no sign of loops.
- **Indicators to add:** superseded-poll count, the oldest pending run age in a concurrency group, and integration-readiness unchecked count per project over time.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix (by end-to-end impact) |
|---|---|---|---|
| review/autofix | Compute | p50 1331s, p95 2842s; slowest 40182804098 at 3624s; Semble bootstrap 31–102s per run | Prompt-prefix stability (Cost #1), Semble index reuse (Speed #4) |
| CI | Compute plus queueing | p50 1263s; `tests-release-and-log-analysis` 11.5–14 min; 168s queue in 40192470938 | Step timings, then shard (Speed #5) |
| Post-merge (status sync, reconcile, validate-dispatch) | Queueing and git overhead | 100–120s clone, about 125s tag fetch, 76–220s queue | Speed #1, #2, #3 |
| plan / implement | Compute | Plan successes run 665–989s; implement successes 1267–1927s (for example 40189021535) | No step-level evidence; collect excerpts next window |
| orchestrate poll | Compute | 12+ min body, contents truncated | Keep `GH_PAT_BUDGET` and phase timing lines during truncation |
| Retries | None | All `run_attempt=1` | — |
| Merge/conflict | None observed | Merge train `examined=0 released=0` | — |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** runner saturation on PR-event fan-out (100–227s queues); review_autofix compute (p50 1331s); the full ai-memory clone (100–120s); heal-reconcile full-history checkout (about 125s).
- **Top failure modes:** CI promote-cycle step (2 of 32 runs, opaque); review_autofix checkout of the PR head (1 of 44); 21 poller runs superseded (benign).
- **Highest-cost drivers:** review_autofix holds 100% of the 146.6M OR tokens, with 20.76M uncached prompt tokens across 16 runs.
- **Top 3 actions:**
  1. Single-branch, blobless ai-memory clone with full-clone fallback (`ai_memory_lib.py:3211`).
  2. Gate heal-reconcile's `fetch-depth: 0` checkout behind the heal-issue pre-check (`internal-cancel-on-pr-close.yml:45-50`).
  3. Add the `PROMOTE_CYCLE_SHARD_FAIL` structured line to `ci.yml:1771-1774`, and export `or_phases` from the cost collector.

## Metrics Appendix

**Runs**

| Scope | Total | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 (s) | p95 (s) |
|---|---|---|---|---|---|---|---|---|
| All | 1000 | 222 | 3 | 21 | 754 | 0.3% (1.2% of the 246 non-skipped) | 1.0* | 1260 |
| ci | 32 | 30 | 2 | 0 | 0 | 6.25% | 1263 | 1769 |
| review_autofix | 44 | 43 | 1 | 0 | 0 | 2.27% | 1331.5 | 2841.5 |
| orchestrate_poll | 40 | 19 | 0 | 21 | 0 | 0% | 291 | 1222 |
| issue_pr_status | 8 | 8 | 0 | 0 | 0 | 0% | 226.5 | 416 |
| cancel_on_pr_close | 24 | 24 | 0 | 0 | 0 | 0% | 97 | 236 |
| lint_pr_body_auto_close | 20 | 20 | 0 | 0 | 0 | 0% | 58 | 385 |
| integration_pr_readiness | 18 | 18 | 0 | 0 | 0 | 0% | 52.5 | 220 |

\*The overall p50 is pulled to 1s by skipped runs. The success sample rate is 0.07.

**Review telemetry**

| Metric | Value |
|---|---|
| cache_hit_rate (family) | null (one usage-unavailable call); computed aggregate about 0.816 |
| wall_clock_p50_ms / p99_ms (repo) | 7,000 / 2,924,600 |
| wall_clock_p50_ms / p99_ms (review_autofix) | 1,933,500 / 3,520,350 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**Tokens (OpenRouter, review_autofix only)**

| Prompt (uncached) | Completion | Cache read | Cache write | Total | Calls | Usage unavailable |
|---|---|---|---|---|---|---|
| 20,763,597 | 1,132,952 | 118,676,932 | 6,033,547 | 146,604,829 | 115 | 1 |

Codex and Claude: 0 calls recorded. No dollar cost is available.

**GH API (GH_PAT_BUDGET)**

| Run | Job | used_in_job | remaining |
|---|---|---|---|
| 40194115992 | sweep | 22 | 4324 |
| 40192914297 | post-merge-force-poll | 25 | 4442 |
| 40192914297 | post-merge-validate-dispatch | 5 | 4493 |
| 40192470978 | post-merge-validate-dispatch | 0 | 4846 |
| 40193751418 | post-merge-validate-dispatch | 0 | 4236 |

No 429s or secondary rate limits were seen.

**Semble**

| Queries | Bytes | Avg bytes per query | Sources | Bootstraps | Bootstrap ms (avg) | Failed | Unused | Static dup bytes | Fallbacks (contract-test / runtime) |
|---|---|---|---|---|---|---|---|---|---|
| 14 | 221,275 | 15,805 | 97 | 14 | 620,188 (44,299) | 0 | 0 | 0 | 28 / 0 |

**Serena:** 0 queries, 0 response bytes, 0 tool calls, 0 fallbacks.

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all workflows) | 0 | 0 | 0 (disabled) |

**Other MCP servers observed:** none.

**AI memory:** 2 `finalize-task` events, 100% ok. `clone_ms` 99,651 and 119,829; `push_attempts` 1 and 1. 0 `retrieve` events visible.

**Data gaps and next collection steps**
1. Mount the run-logs directory so failure logs for 40182895760, 40188366227 and 40187110927 can be read.
2. Keep `GH_PAT_BUDGET`, `AI_MEMORY_TELEMETRY` and `SEMBLE_*` lines when truncating logs (the poll log lost 876,101 chars).
3. Export `or_phases`.
4. Compute a partial cache-hit rate when some calls lack usage.
5. Capture plan and implement excerpts, which have no telemetry rows in this window.