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
## Deep Audit — Workflows & Scripts (2026-10-25)

> Scope and method: this pass reads a smaller set of files closely rather than all of them lightly. Files read: `internal-cancel-on-pr-close.yml`, `cancel_on_pr_close.yml`, `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`, `workflow-log-analysis.yml` (the three model-pass jobs and the push steps), and selected steps of `implement.yml`, `review_autofix.yml`, `issue_pr_status.yml`, `orchestrate.yml`, `clarify.yml`, `plan.yml` and `check_failure_triage.yml`. Scripts read: `workflow_failure_heal_pr_reconcile.sh`, `gh_helpers.sh` (`gh_retry`), `review_collect_pr_metadata.sh`, `stable_e2e_step_verify_bait_removed.sh`, `ai_memory_lib.py` and `ai_memory.py`, plus `tests/test_workflow_file_size_limit.py`. Findings already in this report (ai-memory full clone, heal-pr-reconcile `fetch-depth: 0`, readiness fan-out) are cross-referenced, not repeated. Character counts were estimated from line counts and sampled line lengths, not measured, and are marked `[NEEDS VERIFICATION]`.

### Section 1: Bug & Correctness Sweep

**BUG-001 — The merge-train release is skipped whenever run cancellation fails**
- **File:** `.github/workflows/cancel_on_pr_close.yml:143-190, 391-427`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The step "Cancel queued/in-progress runs for closed PR branch" runs under `set -euo pipefail` (line 146).
  - `_cpr_fetch_active_runs` pipes `gh_retry gh api … --paginate` into `jq` with no error guard (lines 167-188). It is called as `_cpr_fetch_active_runs … > "${active_runs_file}"` (lines 260, 325).
  - So if the API is still failing after retries, or `jq` fails, the whole step fails.
  - The next step, "Release merge-queued PRs (merge train)" (line 391), has no `if:`. It therefore inherits `success()` and is skipped.
  - That step's own comment says it must run because "core-profile repos have no poll backstop" (lines 392-394). In those repos, one transient Actions API failure on PR close leaves younger merge-queued PRs blocked until some other trigger arrives.
- **Recommended fix:** add `if: ${{ !cancelled() }}` to the "Release merge-queued PRs" step. That step is already fail-open (lines 423-427, `_mt_rc` with a `::warning::`), so running it after a failed cancel pass is safe.

**BUG-002 — The no-op path can exit before applying `ai:implementation-failed`**
- **File:** `.github/workflows/implement.yml:5427-5428, 5637-5654`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The fall-through branch of "Handle no-op implementation" runs `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (5637-5638) with no `|| …` guard, under `set -euo pipefail` (5428).
  - `gh_retry` gives commands it classifies as unsafe creates a single attempt (`gh_helpers.sh:1171-1173`, classifier at `gh_helpers.sh:804-816`). Whether this implicit-POST `gh api … -f` form is classified that way is `[NEEDS VERIFICATION]` in `_gh_api_args_unsafe_post`.
  - If the comment POST fails, the step exits before the label edit at 5651-5654. The issue never gets `ai:implementation-failed`, so the orchestrator's re-issue path never sees it.
  - Every other mutation in this step ends in `|| true` (for example 5464, 5471, 5475, 5518).
- **Recommended fix:** apply the label (5643-5654) before posting the comment. Then end the comment call with `|| echo "::warning::no-op comment failed for #${ISSUE_NUMBER}"`. This matches the Guard 0 and Guard 2 branches in the same step.

**BUG-003 — `GH_API_RETRY_HELPERS` is read in 13 workflows but set in only 5, so the promote-candidate read never retries in 8**
- **Files:**
  - `.github/workflows/cancel_on_pr_close.yml:43`
  - `.github/workflows/orchestrate_poll.yml:135, 531`
  - `.github/workflows/plan.yml:389, 3244`
  - `clarify.yml`, `orchestrate.yml`, `workflow_failure_heal.yml`, `check_failure_triage.yml`, `review_autofix_sweep.yml` (grep counts only; exact lines not listed in this pass)
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - `source "${GH_API_RETRY_HELPERS:-/dev/null}"` is used in 13 workflows (28 sites).
  - `GH_API_RETRY_HELPERS=` is written only in `validate.yml`, `implement.yml` (line 198), `security-audit.yml`, `orchestrate_clarify_respond.yml` and `review_autofix.yml`.
  - In the other 8 workflows the source resolves to `/dev/null`. The inline fallback `gh_api_retry() { …; gh api "$@"; }` then makes exactly one unretried `branches/promote-candidate` read.
  - A transient failure is reported as `SUPPORT_REF source=promote_candidate_unprotected` (for example `cancel_on_pr_close.yml:51`), which is misleading. The run then takes support scripts from `github.sha` while the workflow YAML runs from `promote-candidate`.
  - Result: support code and workflow YAML can silently come from different commits during the promote cycle. That the skew causes failures is an inference.
- **Recommended fix:**
  - Log a distinct reason when the read fails: `SUPPORT_REF source=promote_candidate_read_failed`, separate from the unprotected case.
  - Give the inline fallback a bounded retry, for example 3 attempts with 2s/4s waits.
  - Alternatively, add the bootstrap step that writes `GH_API_RETRY_HELPERS` (pattern at `implement.yml:153-198`) to these 8 workflows.

**SEC-001 — Memory git errors can carry the token-bearing clone URL, and one handler skips redaction**
- **Files:** `scripts/ai_memory_lib.py:3137-3141, 3202-3204, 3211, 3447-3467` and `scripts/ai_memory.py:1263-1276`
- **Severity:** Medium · **Category:** `security`
- **Description:**
  - `_resolve_origin_url` puts `GH_TOKEN` into the HTTPS origin URL (`_inject_token_into_url`, 3161-3173).
  - `_run_git` builds its `MemoryGitError` text from `' '.join(args)` (3138). For the clone at 3211, those args include the token-bearing URL, so the exception text contains the token.
  - 23 handlers in `ai_memory.py` pass this text through `_sanitize_git_error` (line 76-79; for example 247, 499, 1129).
  - `cmd_actions_runs_cache_get` does not: it uses `error_text = str(exc)` (1264), prints `AI_MEMORY_ERROR: {exc}` to stderr (1275), and puts the raw text into its JSON stdout (1273, 1276).
  - That path's exception comes from `read_memory_root_from_branch`, whose message carries git's stderr (3466-3467).
  - Whether git's stderr includes the credentialed URL depends on the git version `[NEEDS VERIFICATION]`. Actions masks registered secrets in step logs, but the JSON stdout may be persisted or forwarded unmasked (inference).
- **Recommended fix:**
  - Use `error_text = _sanitize_git_error(str(exc))` at `ai_memory.py:1264` and print the sanitized text at 1275.
  - Fix the root cause too: in `_run_git` (3137-3141), redact `args` and stderr with the same userinfo regex before building the message, so no future handler can leak the URL.

**BUG-004 — The `heal-pr-reconcile` job has no timeout**
- **File:** `.github/workflows/internal-cancel-on-pr-close.yml:19-54`
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - The job does a full-history checkout and git merges and pushes (`workflow_failure_heal_pr_reconcile.sh:159-205`), but sets no `timeout-minutes`, so GitHub's 360-minute default applies.
  - The sibling `cancel-active-runs` job uses 5 minutes (`cancel_on_pr_close.yml:13`).
  - A hung `git fetch` or push holds a runner, during the same merge bursts this report already identifies as runner-saturated. The checkout cost itself is covered by Speed #2 of this report.
- **Recommended fix:** add `timeout-minutes: 15` to the `heal-pr-reconcile` job.

**Reviewed, no defect found:**
- Event-text handling in `lint-pr-body-auto-close.yml:58-69`, `integration-pr-readiness.yml:56-65` and `internal-cancel-on-pr-close.yml:34-43`: PR title, body, labels and refs pass through `env:`, not `${{ }}` inside `run:`, so there is no shell injection.
- Branch and ref validation in `workflow_failure_heal_pr_reconcile.sh:66-89` is strict and fails closed.

### Section 2: GitHub API Call Redundancy Audit

**API-001 — Post-merge validate dispatch reads labels one issue at a time and makes a separate PR text read**
- **File:** `.github/workflows/review_autofix.yml:855-949`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:**
  - When `POST_MERGE_LINKED_ISSUES_JSON` is `[]` (every sampled run, per this report's GH API audit table), the step:
    - runs a GraphQL `closingIssuesReferences` query (856-862);
    - then a REST `pulls/<n>` read just for title and body (874);
    - then, for each issue found in the body fallback, one `gh_retry gh issue view … --json labels` inside the `while` loop (910-914).
  - **Current calls:** 1 GraphQL + 1 REST + N REST.
  - The GraphQL re-fetch may duplicate an upstream lookup that already returned `[]` `[NEEDS VERIFICATION: where POST_MERGE_LINKED_ISSUES_JSON is computed]`.
- **Recommended fix:**
  1. Add `title body` to the `pullRequest` selection at line 860 and drop the REST call at 874.
  2. Replace the per-issue `gh issue view` with one aliased query, `i<N>: issue(number:N){number labels(first:100){nodes{name}}}`. The existing batch helper `_fetch_linked_issue_bodies_graphql` (`scripts/review_collect_pr_metadata.sh:87-119`) already selects `labels(first: 50)` per number; the chunked `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:196-256`) is the reference for batches of 50.
  - **Projected calls:** 1 GraphQL + 1 GraphQL, saving N calls per merge.
  - Fail-open is kept: if the batch fails, mark the labels unknown, as the current code does at 920-923.
  - This extends the row "post-merge-validate-dispatch" in this report's GH API audit table.

**API-002 — The no-op ancestor walk makes two REST calls per hop**
- **File:** `.github/workflows/implement.yml:5565-5583`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** each hop runs `gh api repos/…/issues/<n>` for the body (5571), then a paginated `…/comments` scan (5575-5576). Hops depend on each other (the parent number comes from the body), so they cannot be merged across hops.
  - **Current calls:** 2 × `IMPL_NOOP_ANCESTRY_THRESHOLD` + extra pages. That is 4+ calls at the default of 2.
- **Recommended fix:** use one GraphQL query per hop, `repository{issue(number:$n){body comments(last:100){nodes{body}}}}`.
  - **Projected calls:** 1 × threshold, so 2 at the default.
  - Keep the fail-open `break` on error.

**API-003 — heal-pr-reconcile makes one PR lookup per heal issue**
- **File:** `scripts/workflow_failure_heal_pr_reconcile.sh:96-101, 239-247`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** after the paginated heal-issue list (96-101), the loop runs one `gh api repos/…/pulls -f head=<owner>:ai/issue-<n>` per matching heal issue (243).
  - **Current calls:** 1 + N. N is usually 0-1, and all sampled runs ended at `noop reason=no_heal_issue`.
- **Recommended fix:** when N > 1, use one aliased GraphQL query, `h<N>: pullRequests(headRefName:"ai/issue-<N>", states:OPEN, first:10){nodes{number headRefName headRefOid baseRefName}}`.
  - **Projected calls:** 2.
  - If the pre-checkout gate from this report's Speed #2 is adopted, pass the heal-issue list result (`ISSUES_FILE`) through a step output or file so the list is not fetched twice. Today it would be 2 list calls per PR close; after the change, 1.

**API-004 — If the GraphQL batch fails, issue_pr_status falls back to one REST call per issue with no cap**
- **File:** `.github/workflows/issue_pr_status.yml:649-682`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** on any GraphQL batch failure, the step makes one `gh_retry gh api repos/…/issues/<n>` per `LOOKUP_ISSUE_NUMBERS` entry (657).
  - **Current calls:** 1 failed GraphQL + N REST.
  - The fail-open behaviour is deliberate (#1469), but N has no cap `[NEEDS VERIFICATION: typical N]`.
- **Recommended fix:** retry the batch once as two halves before falling back to REST, and cap the REST fallback (for example 20) by marking the rest conservatively as tracking, as line 666 already does on per-issue failure.
  - **Projected calls:** 1 + 2 in the common transient case.

**API-005 — Hand-written retry loops with a fixed 2-second sleep**
- **Files:** `.github/workflows/orchestrate.yml:1700-1707` and `.github/workflows/clarify.yml:2223-2240`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:**
  - `orchestrate.yml` wraps `gh_retry gh api …/comments` (a comment create, which `gh_retry` treats as single-attempt) in an outer `for attempt in 1 2 3` with a fixed `sleep 2`.
  - `clarify.yml` posts a marker with raw `gh api` and no retry (2223), then hand-retries the label POST 3 times with a fixed `sleep 2` (2227-2240).
  - Neither gets `gh_retry`'s rate-limit detection and circuit breaker (`gh_helpers.sh:1212-1218`).
- **Recommended fix:** the code comment at `orchestrate.yml:1696` says a duplicate is harmless, so use `gh_retry --idempotent gh api …` and remove the outer loop. In `clarify.yml`, use `GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST …/labels`.
  - **Calls:** the maximum stays about the same, but retries get exponential backoff and rate-limit awareness.

**Loops reviewed and left as they are (no batch endpoint exists):**
- `cancel_on_pr_close.yml:364-380` sends one `actions/runs/<id>/cancel` per run.
- `plan.yml:1320-1323` sends one `DELETE issues/comments/<id>` per comment.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The support-ref resolution block is copied 16 times across 9 workflows**
- **Files:** for example `implement.yml:463-500` (4 copies), `cancel_on_pr_close.yml:30-67`, `orchestrate_poll.yml:123-…, 519-…`, `plan.yml` (2), `clarify.yml` (2), `orchestrate.yml` (2), plus `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml` and `check_failure_triage.yml`.
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - The same body, including `promote_candidate_support_ref="${{ github.sha }}"`, the branches read, the `^…\.yml@[0-9a-f]{40}$` pin regex and the `stable` fallback, is repeated. Only the workflow filename and the output variable change (`SCRIPT_REF`, `DUPLICATE_NOTICE_SCRIPT_REF`, `PR_NOT_OPEN_SCRIPT_REF`).
  - The block runs before any support checkout, so it cannot simply source a script. That is also why BUG-003 shows up in 8 of the copies.
  - `tests/test_workflow_support_ref_follows_pin.py` exists, but no test asserts that all copies are identical (grep for `promote_candidate_support_ref` in `tests/` finds nothing).
- **Recommended fix:**
  - Add a parity test in `tests/test_workflow_support_ref_follows_pin.py`. It should pull every step body that contains `promote_candidate_support_ref=`, normalise the workflow filename and output variable name, and assert they are byte-identical.
  - Fix BUG-003 in all copies in the same PR.
  - Optionally, replace the copies with a SHA-pinned composite action under `.github/actions/resolve-support-ref` with inputs `workflow_file` and `output_var`.

**DUP-002 — The three model passes in workflow-log-analysis.yml share about 120 lines each, and the setup steps are copied four times**
- **File:** `.github/workflows/workflow-log-analysis.yml`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** "Run workflow log analysis" (1068-1401), "Run deep audit pass" (1873-2127) and "Run API redundancy pass" (2473-2727) repeat:
  - `_wla_label_retry` and `_wla_label_create` (1078-1079 = 1881-1882);
  - the `AI_PHASE_FAILURE_V1` emitter, including its 14-field `jq -cn` payload (1100-1193 against 1894-1970);
  - the attempt and backoff validation, `wla_engine_ready` (1336-1339 = 2035-2038), and the retry ladder with the exit-80 capacity wait (1340-1398 against 2039-2092).
  - The per-job setup is also copied 4 times:
    - "Resolve AI engine": 257, 834, 1677, 2284;
    - Codex CLI persist and restore: 325-348, 907-934, 1750-1777, 2357-2384;
    - semble install and index: 365, 960, 1826, 2425 and 376, 980, 1846, 2445.
- **Recommended fix:**
  - Create `scripts/workflow_log_analysis_pass.sh` with:
    - `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <summary>`
    - `wla_run_model_pass <role> <heartbeat_phase> <prompt_file> <output_file> <codex_log>`, which runs the retry ladder and returns 0, 80 or the last exit code.
  - Call it from all three steps.
  - Move the Codex CLI cache persist and restore into `scripts/codex_cli_tool_cache.sh {persist|restore}`.
  - This also clears EXPR-001 and EXPR-003.

**DUP-003 — The report commit-and-push retry loop exists twice**
- **File:** `.github/workflows/workflow-log-analysis.yml:2134-2211, 2732-2804`
- **Severity:** Low · **Category:** `duplication`
- **Description:** the two loops are identical except for the commit message and log label: `format-patch`, `pull --rebase`, `reset --hard FETCH_HEAD`, `am --3way`, and a 5-attempt backoff capped at 60s.
- **Recommended fix:** move it to `scripts/workflow_log_report_push.sh <report_file> <section_label>` and have both steps call it.

**DUP-004 — `gh_retry` is re-implemented inline in several places**
- **Files:** `review_autofix.yml:816-838`, `check_failure_triage.yml:146-174`, `scripts/stable_e2e_step_verify_bait_removed.sh:65-104`
- **Severity:** Low · **Category:** `duplication`
- **Description:**
  - `review_autofix.yml` defines a 4-attempt fallback `gh_retry` for when `.codex-workflow-src/scripts/gh_helpers.sh` is missing.
  - `check_failure_triage.yml` hand-parses `HTTP [0-9]{3}` and retries 3 times.
  - `stable_e2e_step_verify_bait_removed.sh` sources `gh_helpers.sh` (line 65) and then defines its own `gh_api_with_retry` with separate rate-limit parsing.
- **Recommended fix:**
  - `check_failure_triage.yml`: call `gh_retry` from `gh_helpers.sh`, whose `_is_gh_permanent_failure` gives the same retryable/permanent split, with `GH_RETRY_MAX_ATTEMPTS=3`.
  - `stable_e2e_step_verify_bait_removed.sh`: keep the function only if the rate-limit-returns-75 behaviour is needed, and document why `gh_retry` does not fit.

**DUP-005 — The same six-clause guard condition is pasted into several `if:` lines**
- **File:** `.github/workflows/implement.yml:6609, 6705, 6835`. Lines 6758 and 6988 are also very long `if:` conditions `[NEEDS VERIFICATION: same clauses]`.
- **Severity:** Low · **Category:** `duplication`
- **Description:** `(failure() || cancelled()) && steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' && … staged_support_rebase_conflict == ''` is repeated word for word. Adding a new guard output means editing every copy.
- **Recommended fix:** add one step, `id: guard_state`, that writes `blocked=true|false` from those six outputs, and change each `if:` to `(failure() || cancelled()) && steps.guard_state.outputs.blocked != 'true'`.

### Section 4: Expression Size Limit Risk Assessment

- **What CI already enforces:**
  - `tests/test_workflow_file_size_limit.py:66-98` fails CI when any `steps[].run` containing `${{` reaches 18,000 characters (`RUN_EXPRESSION_GUARD_CHARS`, line 32).
  - If CI is green on main, no interpolated `run:` body is at or above the 18,000-character High threshold, so no High findings are raised.
  - The test does not report the 15,000-18,000 band.
- **Exclusions:** the largest bodies found contain no `${{ }}` and are not subject to the limit: `implement.yml` "Run Codex implementation" (2853-4126, about 1,270 lines) and `review_autofix.yml` "Mark PR review-skipped…" (1150-1461).

**EXPR-001 — "Run workflow log analysis" is probably in the 15K band**
- **File:** `.github/workflows/workflow-log-analysis.yml:1068-1401`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - The body is about 333 lines, with 13 `${{ github.* }}` expressions at 1113-1115, 1128, 1134-1136 and 1173-1175.
  - **Estimated size:** about 14,000-16,500 characters after YAML indentation is stripped `[NEEDS VERIFICATION]`. **Headroom:** about 4,500-7,000 characters to the 21,000 limit, and about 1,500-4,000 to the CI guard.
  - The body embeds the failure-emitter heredoc, which grows with every new field.
- **Recommended fix:** every expression used here (`github.run_id`, `run_attempt`, `workflow`, `server_url`, `repository`) is also a default runner env var: `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW`, `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`. Replace each `${{ github.X }}` with the env var. The body then has no expressions at all, so it is no longer subject to the limit. Then do the DUP-002 extraction.

**EXPR-002 — "Handle no-op implementation" is probably in the 15K band**
- **File:** `.github/workflows/implement.yml:5427-5655`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - The body is about 228 lines of long prose comment templates (for example 5457, 5497, 5504, 5591, 5593), with `${{ }}` at 5432, 5434, 5637, 5645, 5647, 5651 and 5653.
  - **Estimated size:** about 15,000-17,000 characters `[NEEDS VERIFICATION]`. **Headroom:** about 4,000-6,000 characters to 21,000, and about 1,000-3,000 to the CI guard.
- **Recommended fix:** use `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"` and `${GITHUB_REPOSITORY}` instead of the expressions. As a follow-up, move the body to `scripts/implement_handle_noop.sh`, following the existing `scripts/implement_handle_guard_block.sh` pattern, with the support-dir resolution used at 5429.

**EXPR-003 — The deep-audit and API-redundancy pass bodies**
- **File:** `.github/workflows/workflow-log-analysis.yml:1873-2127, 2473-2727`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** each body is about 255 lines with 13 `${{ github.* }}` expressions (for example 1905-1907, 1919, 1925-1927, 1958-1960). **Estimated size:** about 11,000-12,500 characters each `[NEEDS VERIFICATION]`. **Headroom:** about 8,500-10,000 characters.
- **Recommended fix:** the same env-var substitution as EXPR-001.

**EXPR-004 — The size guard has no warning band**
- **File:** `tests/test_workflow_file_size_limit.py:66-98`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - The test fails only at 18,000 characters and gives no early signal at 15,000.
  - It measures only `steps[].run`. The longest `if:` conditions seen are well under 2,000 characters (`implement.yml:6609`, `update_workflows.yml:1057`), and no workflow uses `actions/github-script`, so the current scope is enough for today.
- **Recommended fix:** have `scripts/ci_budget_watch.py` (agents.md:2510-2520) also report interpolated `run:` bodies at or above 15,000 characters, as `CI_BUDGET kind=run_expression`. That opens the deduplicated `ai:ci-budget` issue before the hard guard trips, without making the test stricter.

- **Whole-file size:** `tests/test_workflow_file_size_limit.py:44-59` fails CI at 480,000 bytes for every `.github/workflows/*.yml`, so no workflow is anywhere near 800 KB. The largest by line count are `implement.yml` (7,180 lines), `test-and-mark-stable.yml` (6,569) and `review_autofix.yml` (5,816). Byte sizes were not measured in this pass `[NEEDS VERIFICATION]`. The 1 MB figure in this audit's prompt is wrong; see CONSIST-004.

### Section 5: Cross-Cutting Concerns

**CONSIST-001 — This report's Speed #3 contradicts the readiness workflow's documented contract**
- **File:** `.github/workflows/integration-pr-readiness.yml:20-22, 45-49`
- **Severity:** Medium · **Category:** `consistency`
- **Description:**
  - The workflow says: "Run on every PR so the status context exists when branch protection marks it required. The script itself posts a no-op success on non-orchestrator branches."
  - The required check is the commit status `orchestrator/integration-pr-not-ready`, which the script posts (lines 12-14). It is not the job's own check.
  - Speed #3 above recommends a job-level `if: startsWith(github.head_ref, 'orchestrator/project-')`. With that gate, a skipped job never posts the status. A required status that is never posted stays pending, so every non-project PR into `main` or `stable` would be blocked from merging (inference from the documented branch-protection setup).
- **Recommended fix:** do not apply Speed #3 as written. If the runner cost must come down, post the success status from a lightweight job and leave the full check in place, and confirm against branch protection first.

**CONSIST-002 — Missing retry helpers fall back silently**
- **Files:** for example `plan.yml:1297-1298`, `orchestrate.yml:1687-1688`, `cancel_on_pr_close.yml:147-148`, `scripts/workflow_failure_heal_pr_reconcile.sh:51-55`. A grep over 12 workflows found 65 matches of this and related inline-fallback patterns.
- **Severity:** Low · **Category:** `consistency`
- **Description:** `source …/gh_helpers.sh 2>/dev/null || true; type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }` drops all retry, rate-limit and circuit-breaker behaviour without any log line when staging failed.
  - Related: `workflow-log-analysis.yml:1164` and `1952` call `gh issue comment` without `gh_retry`, while the label calls next to them use `_wla_label_retry`.
- **Recommended fix:** in the fallback branch, emit one `echo "GH_RETRY_FALLBACK file=<workflow> reason=helpers_missing" >&2`. Register the prefix through an `agents.d/` fragment (§25) so the log collector can count degraded runs.

**CONSIST-003 — review_collect_pr_metadata.sh redefines `gh_retry` with a different signature**
- **File:** `scripts/review_collect_pr_metadata.sh:35, 69-74`
- **Severity:** Low · **Category:** `consistency`
- **Description:**
  - The script sources `gh_helpers.sh` (35) and then redefines `gh_retry()` as `gh_retry <outfile> <gh-args…>` (69-74). That shadows the shared `gh_retry <command…>`.
  - It works today because none of the `gh_helpers.sh` functions this script calls use `gh_retry` internally. If one does in future, it will pass the wrong arguments. This breaks the identifier-uniqueness rule (§10).
- **Recommended fix:** rename the local function to `_rcpm_gh_to_file` and update its callers (138, 194, 231, 235, 240, 247, 280). Do not rename the shared helper.

**CONSIST-004 — The audit prompt states the wrong workflow file size limit**
- **File:** `prompts/mode-workflow-audit.txt:100`
- **Severity:** Low · **Category:** `consistency`
- **Description:** the prompt says the overall limit is "1 MB (1,048,576 characters)" and asks for workflows over 800 KB to be flagged. The verified limit is 512,000 bytes, with a 480,000-byte guard (`agents.md:2521-2538`, `tests/test_workflow_file_size_limit.py:26-27`, unattended §24). At 800 KB the audit would never flag a file before GitHub stops running it.
- **Recommended fix:** change line 100 to the 512,000-byte limit, and flag files at or above 440,000 bytes (`CI_BUDGET_WORKFLOW_WARN_BYTES`) and at or above 480,000 bytes.

**DEBT-001 — Stale comments describe a parallel push race that cannot happen**
- **File:** `.github/workflows/workflow-log-analysis.yml:2154-2164, 2752-2757`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:**
  - The comments say the deep-audit and api-redundancy jobs "append a section to the same REPORT_FILE in parallel".
  - In fact `api-redundancy` has `needs: [analyze-commit-notify, deep-audit]` (2247), so they run one after the other.
  - The comments also claim `git am --3way` "merge[s] cleanly". Two different appends at the end of the same file normally conflict in a 3-way merge (inference), so the replay path would abort at 2196 or 2789 rather than merge.
- **Recommended fix:** rewrite both comments to describe the actual risk, which is a concurrent run on the same branch. Note that an end-of-file conflict ends the retries; that matches today's `exit 1`.

**DEAD-001 — `MAX_CODEX_ATTEMPTS` is validated twice in the same step**
- **File:** `.github/workflows/workflow-log-analysis.yml:1091-1094, 1320-1324`
- **Severity:** Low · **Category:** `dead-code`
- **Description:** `MAX_CODEX_ATTEMPTS` is checked and defaulted to 3 at 1091-1094. The second check on `max_attempts` at 1320-1324 can never fire.
- **Recommended fix:** delete 1320-1324 and use `max_attempts="${MAX_CODEX_ATTEMPTS}"`. This also trims EXPR-001.

**SHELL-001 — Unquoted loop over comment IDs (SC2086)**
- **File:** `.github/workflows/plan.yml:1320-1323`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** `for cid in ${CLARIFY_IDS}; do` relies on word-splitting. The values come from jq `.id`, so they are numeric in practice. Still, nothing validates them before they are interpolated into the DELETE endpoint.
- **Recommended fix:** use `mapfile -t clarify_ids <<< "${CLARIFY_IDS}"`, then `for cid in "${clarify_ids[@]}"; do [[ "${cid}" =~ ^[0-9]+$ ]] || continue; …`.

**TODO/FIXME/HACK markers:** none. Grep for `TODO|FIXME|HACK|XXX` under `scripts/` and for `# TODO|FIXME|HACK` in `.github/workflows/` returned no matches.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 10 | BUG-001, BUG-002, BUG-003, SEC-001, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002, CONSIST-001 |
| Low | 16 | BUG-004, API-002, API-003, API-004, API-005, DUP-003, DUP-004, DUP-005, EXPR-003, EXPR-004, CONSIST-002, CONSIST-003, CONSIST-004, DEBT-001, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `cancel_on_pr_close.yml`, `implement.yml`, `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`, 8 workflows that read `GH_API_RETRY_HELPERS` (BUG-001/002/003, SEC-001; all Medium, none Critical/High) | Medium |
| API call optimization | `review_autofix.yml`, `implement.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `issue_pr_status.yml`, `orchestrate.yml`, `clarify.yml` | Medium |
| Code modularization | `workflow-log-analysis.yml`, new `scripts/workflow_log_analysis_pass.sh` / `scripts/workflow_log_report_push.sh` / `scripts/codex_cli_tool_cache.sh`, `tests/test_workflow_support_ref_follows_pin.py`, `check_failure_triage.yml`, `implement.yml` | Large |
| Expression size reduction | `workflow-log-analysis.yml`, `implement.yml`, `scripts/ci_budget_watch.py` | Small |
| Medium/Low fixes | `internal-cancel-on-pr-close.yml`, `integration-pr-readiness.yml` (comment/plan only), `scripts/review_collect_pr_metadata.sh`, `prompts/mode-workflow-audit.txt`, `plan.yml`, `workflow-log-analysis.yml`, one `agents.d/` fragment | Small |
## API Call Consolidation & Dead-Call Analysis (2026-10-25)

### Safety Tag Legend
- **`SAFE_TO_MERGE`:** the change is fully proven from static reading. The calls hit the same endpoint or a strict subset of fields, with identical pagination, filters and auth. They sit in the same function or step with nothing in between that changes the data. Error handling and retries stay the same, no concurrency boundary is crossed, and any documented cache contract holds. The implement stage may act on these directly.
- **`NEEDS_VERIFICATION`:** the saving looks real, but at least one of those conditions can't be proven from source alone. Do the listed checks first.
- **`RISKY_SKIP`:** the call matches a hard exclusion: pagination, a retry or backoff loop, a rate-limit-sensitive path, a race guard, or a fail-open fallback that defends a known incident. It is listed for visibility only. Do not auto-implement it.

### Consolidation Candidates (MERGE-###)

**MERGE-001: two paginated `actions/runs` reads in `_cpr_fetch_active_runs` (queued + in_progress)**
- **Safety tag:** `RISKY_SKIP`
- **Files:**
  - `.github/workflows/cancel_on_pr_close.yml:167-176` (queued)
  - `.github/workflows/cancel_on_pr_close.yml:177-186` (in_progress)
  - Callers: `:260` (schedule) and `:325` (PR close, with a branch filter)
- **Current call count:** 2 paginated sequences per invocation, so at least 2 calls.
- **Proposed call count:** 1 sequence, if the `status` filter were dropped. **Not recommended.**
- **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs?status=…&event=pull_request[&branch=…]&per_page=100`
- **Evidence:** both calls are in the same function and differ only in `-f status=`. The code already rejects merging them:
  ```bash
  # Keep the two status-filtered calls: dropping the filters can
  # paginate through an unbounded completed-run history.
  ```
  The REST endpoint takes one `status` value per request, so one call can't cover both states without dropping the filter.
- **Proposed fix:** none. Keep both calls. This entry exists so that later passes don't raise the merge again.
- **Safety rationale:** both calls use `--paginate`, and merging them would remove the status filter. That changes page boundaries and makes the result set unbounded (pagination trigger).
- **Downstream signal:** do not auto-implement. Merging would be a semantic change to cancellation scope. It would need a maintainer to accept unbounded pagination over completed runs, which the code comment explicitly forbids.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: post-merge dispatch re-runs the gate's `closingIssuesReferences` GraphQL query even after the gate got an authoritative empty result**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:**
  - `scripts/review_autofix_step_evaluate_gate.sh:187-207` (gate query at 190-196; cache flag at 188/198)
  - `scripts/review_autofix_step_evaluate_gate.sh:1361-1363` (gate outputs)
  - `.github/workflows/review_autofix.yml:317-318` (job outputs), `:762-763` (dispatch env), `:855-869` (live re-query)
- **Current call count:** 2 GraphQL calls per merged PR whose closing references are empty. The report's GH API table shows `POST_MERGE_LINKED_ISSUES_JSON: []` in every sampled run.
- **Proposed call count:** 1 when the gate query succeeded; 2 only when it failed.
- **Endpoint:** GraphQL `repository.pullRequest(number).closingIssuesReferences(first:50){nodes{number labels(first:100){nodes{name}}}}`. The query text is byte-identical at gate.sh:194 and review_autofix.yml:860.
- **Evidence:**
  - The gate records whether its query succeeded, but never exports that flag:
    ```bash
    post_merge_linked_issues_cache_known="false"
    ...
    POST_MERGE_LINKED_ISSUES_JSON="$(printf '%s' "${_post_merge_linked}" | jq -c '.' 2>/dev/null || echo '[]')"
    post_merge_linked_issues_cache_known="true"
    ```
  - The dispatch job gets only the array, and treats a successful `[]` the same as a failed lookup:
    ```bash
    if [ "${issue_nodes_json}" = "[]" ]; then
      if issue_nodes_json_live="$(gh_retry gh api graphql \
    ```
  - The job's `if:` (review_autofix.yml:756) skips the dispatch only when both the closing references are empty **and** the title and body are empty (gate.sh:202-206). PR titles are never empty, so the job still runs and re-queries on every merge into a non-default branch.
- **Proposed fix:**
  1. In gate.sh, near 1362, add `echo "post_merge_linked_issues_cache_known=${post_merge_linked_issues_cache_known:-false}" >> "${GITHUB_OUTPUT}"`. Initialise the variable near line 30 so it is set when `POST_MERGE_DISPATCH` is not true.
  2. Expose it as a gate job output next to review_autofix.yml:318.
  3. Pass it into the dispatch env as `POST_MERGE_LINKED_ISSUES_CACHE_KNOWN: ${{ needs.gate.outputs.post_merge_linked_issues_cache_known || 'false' }}`. The default keeps today's behaviour, per §8.
  4. Change line 855 to `if [ "${issue_nodes_json}" = "[]" ] && [ "${POST_MERGE_LINKED_ISSUES_CACHE_KNOWN:-false}" != "true" ]; then`.
  5. Keep the live block and its warning string. `tests/test_review_autofix_review_pipeline_contract.py:7156` pins `"Unable to refresh closingIssuesReferences for merged PR"`.
- **Safety rationale:**
  - The two calls are in different jobs (`gate` → `post-merge-validate-dispatch`), so the "same function, no intervening change" condition fails.
  - The gate call is a bare `gh api graphql` without `gh_retry`, while the dispatch call has retries. Only a *successful* gate result would be reused, so failures still get the retried live path.
- **Downstream signal:** before implementing, check three things:
  1. Confirm with a maintainer that the live re-query at 856 is a fallback and not a deliberate freshness re-read. The warning says "refresh"; the gate comment at gate.sh:183-186 says "reuse … before any live fallback". A closing reference could only change in the seconds between the two jobs if someone edits the merged PR body.
  2. Grep `tests/` for assertions that the dispatch step always calls `closingIssuesReferences`. Only the warning-string assertion was found at :7156.
  3. Confirm the new gate output won't push `review_autofix.yml` toward the 480,000-byte guard (§24). Run `wc -c`.

**REUSE-002: the no-op ancestry walk re-fetches the current issue's body, which the job already cached in `ISSUE_META_FILE`**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:**
  - `.github/workflows/implement.yml:5569-5571` (hop 1 read)
  - Cache writers in the same `implement` job (line 98): `:221-224` (first payload), `:1019-1020`, `:2072-2073` / `:2101` ("Fetch issue metadata")
  - `:314` (`ISSUE_META_FILE` exported to `GITHUB_ENV`)
  - `:2061-2070` (`issue_meta_matches_issue` validator)
- **Current call count:** for the body, 1 call per hop, so `IMPL_NOOP_ANCESTRY_THRESHOLD` calls (default 2), plus the paginated comment scans (unchanged).
- **Proposed call count:** threshold − 1 body calls. Hop 1 reads the cache, saving 1 REST call per no-op run.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{ISSUE_NUMBER}` (`--jq '.body // ""'`)
- **Evidence:**
  ```bash
  current="${ISSUE_NUMBER}"
  for hop in $(seq 1 "${threshold}"); do
    parent_body="$(gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
  ```
  - On hop 1, `current == ISSUE_NUMBER`. The same job has already stored the full issue payload in `${ISSUE_META_FILE}` (line 222). It re-validates that payload with `issue_meta_matches_issue` (checks number and string body) at 2072-2079.
  - This is not the same as Deep Audit API-002, which proposes a GraphQL merge across body and comments.
- **Proposed fix:** before the loop, when `current = ISSUE_NUMBER` and the cached file passes the `issue_meta_matches_issue` check, take hop 1's body from `jq -r '.body // ""' "${ISSUE_META_FILE}"`. Otherwise keep the live `gh_retry` call. Also update the cost comment at 5561 ("up to 2 * threshold calls").
- **Safety rationale:** the cache-miss path stays on the existing call, so error handling is unchanged. But the cached snapshot is taken 20+ minutes before this step, and the file can be truncated on purpose (`: > "${ISSUE_META_FILE}"` at 991 and 1010). So freshness and presence can't be proven statically.
- **Downstream signal:** verify three things before implementing:
  1. Nothing adds or edits the `Re-issued from #N` line in an issue body after the issue is created. Grep the re-issue writer in `scripts/` for edits to an existing body.
  2. `issue_meta_matches_issue` is defined in, or callable from, the "Handle no-op implementation" step. It is defined inline at 2061 in another step, so it may need copying or sourcing.
  3. `RUNTIME_DIR` / `ISSUE_META_FILE` are not swapped for staged-support paths between line 2101 and line 5419.

### Dead Calls (DEAD-API-###)
No findings. The calls I reviewed all feed downstream logic. The nearest case, the REST `pulls/{n}` read at `review_autofix.yml:874`, is close to unreachable rather than dead (see API-001 below).

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — the REST read at 874 only runs if the gate's PR title/body output is lost, so step 1 saves almost no calls; REUSE-001 is the bigger win. Details:
  - The dispatch job only runs when `POST_MERGE_DISPATCH=true`. That needs a successful gate PR read (gate.sh:87-121, 154-158), which fills `pr_title_cached`, and PR titles are never empty.
  - Batching the per-issue `gh issue view` (874/910-914) changes failure granularity: one failed batch would mark every issue's labels unknown, where today each issue fails separately (920-923).
  - The batch must keep the test-pinned string `"Unable to read labels for issue #${issue_number} after retries"` (test file :7168).
- API-002: RISKY_SKIP — the comments read at `implement.yml:5575-5576` uses `--paginate`. Replacing it with `comments(last:100)` would miss no-op markers on older comment pages (pagination trigger). The non-paginated part of the saving is in REUSE-002.
- API-003: NEEDS_VERIFICATION — today N is 0 in every sampled run (`noop reason=no_heal_issue` at `workflow_failure_heal_pr_reconcile.sh:104`). Two cautions:
  - The paginated heal-issue list (96-101) must not be merged or restructured.
  - Reusing `ISSUES_FILE` from a pre-checkout gate depends on Speed #2 adding that step first. The script header (29-32) documents the API budget and must be updated with it.
- API-004: RISKY_SKIP — the per-issue REST loop at `issue_pr_status.yml:649-682` is the deliberate fail-open fallback that defends incident #1469 (comments at 650-652 and 659-663). Capping or re-batching it changes when a tracking issue could be misclassified.
- API-005: RISKY_SKIP — both sites are retry loops. `clarify.yml:2225-2226` deliberately retries "with the independent job token" as a fallback for when the PAT budget runs out (label description at 2236), which is a rate-limit-sensitive path. The `orchestrate.yml:1700-1707` loop wraps a comment create, and adding `--idempotent` changes duplicate-post behaviour.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 1 | MERGE-001 |

The counts cover only this pass's findings. Of the Deep Audit IDs, API-001 and API-003 are `NEEDS_VERIFICATION`, and API-002, API-004 and API-005 are `RISKY_SKIP`.

### Implement-Stage Handoff
No `SAFE_TO_MERGE` findings in this pass. The highest-value candidate once verified is REUSE-001: it saves 1 GraphQL call on every merge into a non-default branch. It needs the maintainer check on whether the line-856 re-query is a fallback or a deliberate freshness re-read.