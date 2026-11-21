## Executive Summary

> **Coverage caveat:** `insufficient_data` is `false`, but the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was **not present** in the sandbox, so I had no full logs from `errors/`, `slow/` or `recent/`. Findings rest on `analysis_context.json` (including `log_summary` rows) and on the repo source. Root causes for the two failures are therefore partly inferred and labelled as such. The success sample rate is 0.07, so only 4 runs carry token telemetry.

- **Release-gate failure is already fixed in the repo; it needs one clean run to confirm.** Run 39694107256 (`test_and_mark_stable`, job `orphan-workflows-test`, step `Dispatch & watch — validation-refresh`) failed after 5,422s. The matching validation-refresh run 39694338090 needed 2,434s, which is over the old 2,400s window. `.github/workflows/test-and-mark-stable.yml:5395-5403` now cites this run and sets `--completion-timeout-secs 3720`. That leaves about 1,286s of headroom over the observed worst case. Impact: removes 1 of the 2 failures in the window (50% of failures). Confidence: high.
- **AI token spend comes almost entirely from two review runs.** `review_autofix` used 30,062,260 of 30,325,579 tokens (99.1%). Run 39707851300 alone used 25,702,172 (84.8%) across 22 calls, about 1.17M tokens per call. The other review run, 39706563694 (PR #7844), had a cache-hit rate of only 0.337 (2,109,852 uncached prompt tokens). At the 0.857 rate seen in the other run, about 1.49M full-price input tokens would have been cache reads instead. Confidence: medium (2 runs only).
- **The 7 "unused" Semble bootstraps in Workflow Log Analysis (260,443 ms) are a telemetry artefact, not waste.** `semble_helpers.sh:28-60` writes `SEMBLE_QUERY` to stderr. `workflow-log-analysis.yml:1296` and `:453` call `semble_query_block ... 2>/dev/null`, so the line is discarded. `cost_audit.py:879-880` then marks every bootstrap as unused. This prompt contains a 6-source SEMBLE block, so the queries do run. Confidence: high.
- **One review failure, likely a race with the PR closing (inferred).** Run 39711230881 failed at `review / codex-agent` → `Checkout PR head branch` after 870s. It reviewed PR #7849, which was closed unmerged around 02:27:58 (cancel run 39711517848) while the review was still running. Inference: the head branch was deleted, and the 4-attempt `git fetch` at `review_autofix.yml:2911-2918` then exited 1. Fix: run a cheap `git ls-remote` check and exit cleanly. Impact: about 1 of 51 review runs (2%). Confidence: low-medium until the run log is checked.
- **Every PR close does a full-history checkout just to no-op.** `internal-cancel-on-pr-close.yml:50` uses `fetch-depth: 0`. The reconcile was `noop reason=no_heal_issue` in all 3 observed closes (39710714526, 39711517848, 39712392621). Checkout took about 71s of 77s in 39710714526 and produced about 560K chars of tag-fetch log. Impact: about 60s per PR close. Confidence: medium.
- **Reliability is high overall.** 2 failures out of 1,000 runs (0.2%). No HTTP 429s, no `BREAK_GLASS` (0), no `CONTEXT_BUDGET_WARN` (0), and 0 Semble runtime fallbacks. Confidence: high for the sampled runs.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Release cycle critical path: validation-refresh smoke (critical path).**
   - Evidence: promote run 39700391574 took 7,791s. Its Test & Mark Stable child 39703422828 (`[cycle:39700391574]`) took 5,906s, ending about 02:41:46 against the promote's end at about 02:43:11. Validation-refresh runs inside each TMS took 2,127s (39703635217) and 2,434s (39694338090), about 36-41% of TMS wall time. The smoke already passes `discovery_dry_run=true` (`test-and-mark-stable.yml:5409`).
   - Root cause (inference): the remaining time is per-consumer clone/render/lint/self-test inside `validation-refresh.yml`. The truncated telemetry cannot attribute it further.
   - Change:
     - Add one timing line per consumer phase in validation-refresh, e.g. `VALIDATION_REFRESH_CONSUMER repo=<r> phase=<clone|render|lint|selftest> ms=<n>`. This is a new log prefix, so it needs an `agents.d/` fragment under "Stable log prefixes (contractual)".
     - Have `scripts/dispatch_and_watch_workflow_run.sh` print `registration_ms`, `queued_ms` and `run_ms` on completion, if it does not already.
   - Savings: none directly. This unblocks a targeted cut of the 35-40 min smoke.
   - Risk: none (logging only).
2. **heal-pr-reconcile full-history checkout (cuts time on every PR close).**
   - Evidence: 39710714526 spent about 02:19:11 to 02:20:22 in checkout and tag fetch, while the reconcile script ran in under 1s. 39711517848 and 39712392621 show the same full tag list (`v1.3.0`…`v1.37.1`). `cancel_on_pr_close` p95 is 146s.
   - Root cause: `fetch-depth: 0` at `internal-cancel-on-pr-close.yml:50`. Full history is only needed when `_retarget_heal` runs `git merge` (`scripts/workflow_failure_heal_pr_reconcile.sh:181-189`).
   - Change: switch to `fetch-depth: 1`. In `_retarget_heal`, before the existing fetch at line 159, deepen on demand. The fallback is a new `RETARGET_FAIL_REASON` value, so the fail-closed behaviour stays as it is.
     ```bash
     if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
     	if ! git fetch --quiet --no-tags --unshallow origin "+refs/heads/main:refs/remotes/origin/main" >/dev/null 2>&1; then
     		RETARGET_FAIL_REASON="unshallow_failed"
     		return 1
     	fi
     fi
     ```
   - Savings: about 60-70s per closed PR (3 in roughly 20 minutes in this window).
   - Risk: medium. A shallow merge-base could produce a false `merge_conflict`. Test with a stacked heal PR before rollout.
3. **Runner queueing on orchestrate_poll and CI.**
   - Evidence: in 39711448528 the poll job waited about 3 min (02:27:14 to 02:30:18). In 39710918441, `tests-release-and-log-analysis` waited about 2.5 min (02:21:21 to 02:23:49). Historical triage 39710918501 logged "All GitHub-hosted runners with label [ubuntu-latest] are busy".
   - Root cause: hosted-runner saturation during bursts. Each PR close or merge fans out 10+ runs, e.g. the 8 skipped runs at 02:25:44-45.
   - Change: no infrastructure change. Track queue time as a first-class metric, and have the collector compute `started_at - created_at` per job.
   - Savings: unknown. This is diagnostic.
4. **CI `tests-release-and-log-analysis` shard (micro, but always on the critical path).**
   - Evidence: about 10-11 min span in 39711230612, 39710918441 and 39710715030. CI p50 is 888s.
   - Change: emit per-test-file durations (`pytest --durations=25`) in that shard so it can be rebalanced against the other 4 test jobs.
   - Savings (estimated): 2-4 min per CI run if rebalanced.
   - Risk: low.
5. **Forward-merge no-op still does a full clone (micro).**
   - Evidence: 39712971599 took 78s with `ahead=0` and about 564,823 log chars truncated (tag fetch). `forward-merge-stable-to-main.yml:48` uses `fetch-depth: 0`.
   - Change: run a `git ls-remote`/`merge-base --is-ancestor`-style precheck before the full checkout.
   - Savings: about 60s per no-op run.
   - Risk: low, but `fetch-depth: 0` is required on the merge path, so keep it there.

## Cost Optimizations

Ranked by expected token savings.

1. **Cache fragmentation in review run 39706563694 (PR #7844).**
   - Evidence:

     | Run | Cache hit | Cache read | Cache write | Uncached prompt | Calls |
     |---|---:|---:|---:|---:|---:|
     | 39706563694 | 0.337 | 1,463,934 | 769,450 | 2,109,852 | 20 |
     | 39707851300 | 0.857 | 21,949,060 | 771,598 | 2,886,610 | 22 |

   - Root cause (inference): the per-call prompt prefix differs, for example across reviewer roles or because the SEMBLE block is placed early. In this prompt, `render_prompt.sh` places the `=== SEMBLE ===` block before the stable analysis context (`workflow-log-analysis.yml:1301-1306`). If review prompts do the same, a query-dependent block sits ahead of the large stable content and breaks prefix reuse.
   - Change: render byte-identical shared content (PR diff, repo rules) first in every reviewer/editor prompt, and move `SEMBLE_PREFETCH` and role-specific text after it.
   - Savings (estimated): about 1.49M full-price input tokens per run like 39706563694, moved to cache reads.
   - Quality risk: none; content is reordered, not removed.
2. **Context size per call in review run 39707851300.**
   - Evidence: 25,702,172 tokens over 22 calls, about 998K cache-read tokens per call. That run alone is 84.8% of window spend.
   - Root cause: unknown. `or_phases` is computed by `cost_audit.py` but not exported to the analysis context, so no per-phase attribution is possible.
   - Change:
     - Include `or_phases` (per-phase call and token counts) in `analysis_context.json`.
     - Log `phase=` and `turns=` on each usage line.
     - Then cap turn count or trim context for the top phase.
   - Savings: unknown until attributed. This run is where most of the spend is.
   - Quality risk: medium if turns are capped blindly, so attribute first.
3. **Paying for cache writes with no reads in Test & Mark Stable.**
   - Evidence: both TMS runs had `cache_hit_rate` 0.0, with `or_cache_write_tokens` 248,240 and `or_cache_read_tokens` 0 across 30 calls (39703422828: 184,636 written; 39694107256: 63,604 written).
   - Root cause (inference): one-shot smoke calls mark prompts cacheable but never reuse the prefix. Provider cache writes usually cost more than plain input.
   - Change: for TMS smoke phases, either drop cache-control markers or share a stable prefix across the smoke calls.
   - Savings (estimated): the write premium on about 248K tokens per release cycle. Small, but pure waste.
   - Quality risk: none.
4. **Semble.**
   - Review: 4 queries, 67,220 logged bytes, 20 sources, `static_dup_bytes` 0. That is about 16.8KB per query with no overlap with static context, so useful and non-duplicated. However, the 4 bootstraps cost 180,752 ms (about 45s each), and whether Semble reduces prompt expansion cannot be measured without per-phase tokens.
   - Workflow Log Analysis: the 7 "unused" bootstraps are false (see Reliability item 3).
   - Recommendation: keep Semble. If the two review bootstraps per run are sequential on the critical path (unverified), cache the index with `actions/cache` keyed on the head SHA.
5. **Serena:** zero `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE` lines, and `SERENA_ENABLED: false` in 39710714649. There is nothing to evaluate.

## Reliability Improvements

Ranked by expected reduction in failures or reruns.

1. **Validation-refresh completion window (fixed in the repo; verify).**
   - Failure: 39694107256 (`orphan-workflows-test` / `Dispatch & watch — validation-refresh`).
   - Category: timeout too tight. The smoke needed 2,434s against a 2,400s window.
   - Fix: already landed (`--completion-timeout-secs 3720`, with `timeout-minutes: 65` on the step). The 02:19 forward-merge 39710715031 carried `test-and-mark-stable.yml` and `tests/test_validation_refresh_workflow_contract.py` to main.
   - Action: confirm that TMS 39712973511 (v1.38.0, dispatched by 39712853589) passes this step.
   - Rollback: revert to 2400. No fail-open concern; the step still fails closed on a real timeout.
2. **Review checkout on a deleted or closed PR head (inferred root cause).**
   - Failure: 39711230881 at `Checkout PR head branch`, 870s total, PR #7849.
   - Category (inference): race between the PR closing and the review run. Run 39711517848's `cancel-active-runs` only cancels runs with `event == "pull_request"` and a populated `pull_requests` link (`cancel_on_pr_close.yml:310-319`). Separately, the scheduled cleanup 39711500650 warned "preserved 3 active run(s) without pull-request linkage". The review run may have been missed by both.
   - Fix: on the first fetch failure in the loop at `review_autofix.yml:2911-2923`, check whether the branch still exists. This is a git call, not a GitHub API call, so it is fine under §14. The `REVIEW_HEAD_REF_GONE` token needs an `agents.d/` "Stable log prefixes" fragment. Check `wc -c` on `review_autofix.yml` after the edit (§24).
     ```bash
     ls_remote_rc=0
     git ls-remote --exit-code --heads origin "refs/heads/${HEAD_REF}" >/dev/null 2>&1 || ls_remote_rc=$?
     if [ "${ls_remote_rc}" -eq 2 ]; then
       echo "::notice::REVIEW_HEAD_REF_GONE head_ref=${HEAD_REF} pr=${PR_NUMBER:-unknown} action=skip"
       echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
       echo "CAN_PUSH=false" >> "$GITHUB_ENV"
       exit 0
     fi
     ```
   - Impact: removes this failure class (about 2% of review runs this window).
   - Fail-open: only exit code 2 ("no matching ref") skips. Transient errors keep the existing retry and `exit 1`.
   - Before implementing: confirm the failing line in the 39711230881 log. The `::error::Review workspace is unavailable` (line 2862) and OpenCode-config (line 2867) exits are other candidates.
3. **Semble telemetry hidden in Workflow Log Analysis (observability).**
   - Evidence: 7 bootstraps and 0 queries counted for `workflow_log_analysis`, yet this run's prompt contains a Semble block. The cause is the `2>/dev/null` at `workflow-log-analysis.yml:1296` (and the retro prefetch at `:453`) swallowing the stderr-only `SEMBLE_QUERY` / `SEMBLE_FALLBACK` lines.
   - Fix (variable name to be checked for uniqueness in that step):
     ```bash
     wla_semble_stderr_file="$(mktemp)"
     WLA_SEMBLE_PREFETCH="$(semble_query_block "${wla_query_text}" 6 "Workflow Log Analysis Context" 2>"${wla_semble_stderr_file}" || true)"
     grep -E '^SEMBLE_(QUERY|FALLBACK|BOOTSTRAP) ' "${wla_semble_stderr_file}" >&2 || true
     rm -f "${wla_semble_stderr_file}"
     ```
   - Impact: corrects the `semble_bootstraps_unused` metric and makes a broken rollout in this workflow detectable.
   - Fail-open: unchanged.
4. **Semble fallbacks are healthy.** All 36 are contract-test fallbacks (ci 24, test_and_mark_stable 12; 8 per CI run in 39711230612, 39710918441 and 39710715030). Runtime fallbacks are 0 and failed bootstraps are 0. Orchestrate poll 39711448528 ran with `SEMBLE_AVAILABLE: false` under lazy mode, which is the expected silent fail-open.
5. **`BREAK_GLASS` / `CONTEXT_BUDGET_WARN`:** 0 and 0 across all 115 runs with telemetry. There is no rubric pressure or prompt-size signal.

## AI Memory Health

- **`retrieve` telemetry: none found** in the provided summaries, so hit rate, `estimated_tokens` against budget, and the `keyword_method` mix cannot be computed. Recommendation: verify that `retrieve` emits `AI_MEMORY_TELEMETRY` in plan, implement and review. Have the collector extract `AI_MEMORY_TELEMETRY:` lines before log truncation; poll 39711448528 lost 848,767 chars.
- **`finalize-task` (3 events):**
  - 39712392617, issue #7836: `ok: true`, `did_push: true`, `push_attempts: 2` (1 push retry), `final_state: "closed"`. The step "Finalize linked issue lineage state" took about 90s of 137s.
  - 39710714519, issue #7837: `ok: true`, `push_attempts: 1`. About 2m08s gap before the result, the dominant share of 178s.
  - 39711518188: `fail_open: true`, `reason: "no_linked_issues"`. Correct behaviour for PR #7849.
- **Stale lineage signal:** in 39712392617, the lineage for issue-7836 still shows implement run 39704851400 as `in_progress`, although that run finished `success` in 1,324s. Inference: implement does not finalize its own run event. Add a `record-run-event` with the terminal state at implement completion, or reconcile in-progress entries at finalize.
- **Finalize latency:** 90-128s per PR close or merge is high for one commit and push. Add `commit_ms` and `push_ms` fields to the finalize telemetry to locate the time (diagnostic).
- `enabled: false` entries: none seen.

## GH API Call Audit

- **Observed budgets (shared PAT, same `reset=1792638087`):**
  - heal intake 39711636763: `used_in_job=31`, `remaining=4061`
  - heal intake 39711634269: `used_in_job=53`, `remaining=4096`
  - post-merge-validate-dispatch 39710714649: `used_in_job=4`, `remaining=4329`
- No 429 or secondary-rate-limit events appear in any summary.
- **Per-item patterns:**
  - Lint PR body: 1 paginated `pulls/<n>/commits` call per run (39712645511, 39711230608). Fine.
  - Historical triage 39710918501: 4 calls (PR, run, paginated issue dedup, log probe), short-circuited by `already_filed`. Fine.
  - heal intake: 53 calls for one dispatch is the largest per-job count seen. Without step logs (about 59K chars truncated) I cannot attribute it. Recommendation: emit `GH_PAT_BUDGET` per phase inside intake, then batch any per-issue lookups via the aliased-GraphQL pattern (`_fetch_candidate_issue_details_graphql`).
- **Biggest blind spot:** `orchestrate_poll`, likely the largest consumer (57 runs, p50 628s), has no visible `GH_PAT_BUDGET` because of truncation. Collector change: grep-extract `GH_PAT_BUDGET`, `AI_MEMORY_TELEMETRY`, `SEMBLE_*`, `SERENA_*` and usage lines from each step log before truncating.
- Estimated reduction: none claimable without data. Rate-limit risk is currently low (more than 4,000 remaining).

## Prompt Cache & Memory System

- **Overall `cache_hit_rate`:** 0.768829 (23,412,994 read; 1,789,288 write; 5,250,520 uncached). This matches `read / (read + write + uncached)`.
- **Per run:**
  - 39707851300: 0.857 (healthy)
  - 39706563694: 0.337 (fragmented)
  - TMS 39703422828 and 39694107256: 0.0 (write-only)
- **Fragmentation causes (inferred):**
  1. Dynamic Semble block placed before stable content (see Cost item 1).
  2. Role-specific prefixes in parallel reviewers.
  3. One-shot TMS smoke calls that are marked cacheable.
- **Fail-open:** all 72 calls reported usage (`or_usage_unavailable_calls` 0), so there is no gap in usage accounting.
- **`CONTEXT_BUDGET_WARN`:** 0. Prompt growth is not yet hitting the 0.7 ratio threshold (`DEFAULT_CONTEXT_BUDGET_WARN_RATIO`). However, at about 1M cached tokens per call in 39707851300, the per-call context should be checked against the model window once `or_phases` is exported.
- **Claude engine gap:** the heal intake logs `AI_ENGINE_SELECTED ... engine=claude model=claude-opus-5-5 effort=high`, but the `claude_*` counters are absent from the context. Claude spend may be uncounted for the heal family.
- **Expected impact:** stabilising the review prefix could lift hit rate from 0.337 toward 0.85 on runs like 39706563694, about a 2.6x reduction in uncached input on those runs.

## Orchestrator Health

- **Poller:** 57 runs, 53 success, 4 cancelled. Two of those are `cancelled_before_first_step` (39712577719 at 131s, 39710935190 at 344s), consistent with concurrency supersede. No failures. The 909s run 39711448528 spent about 12 min in the `poll` step.
- **Clarify-respond:** 116 of 118 skipped; clarify 122 of 135 "other". `internal-clarify.yml` triggers on every `issue_comment: created`, so each bot comment fans out to several skipped runs (e.g. 8 skipped runs at 02:25:44-45). These are cheap (p50 1s) but inflate run counts and queue pressure. This cannot be filtered at `on:` level, so leave it; see the collector note under Pipeline Flow.
- **Discarded implement cycle:** issue #7836 went implement (39704851400, 1,324s) → review (39706563694, 2,011s, 4.36M tokens) → PR #7844 closed unmerged (39712392621). The close cause is not visible.
  - Indicator to track: closed-unmerged count among `ai/issue-*` PRs.
  - Mitigation: log a close reason via the existing cancel-on-close step.
- **Promote cycle:** the follower stayed `frozen protected=true` (39712270605, 39710918369) during the cycle, as designed. Cycle v1.38.0 was dispatched at 02:43 (39712973511).
- **No signals in the data** for clarification loops, conflict-heal retries or stuck waves.
- **Indicators to track:** poll `cancelled_before_first_step` rate, lineage entries `in_progress` older than 1h, finalize `push_attempts > 1`.

## Pipeline Flow Bottlenecks

| Stage | p50 (s) | p95 (s) | Slowest observed (s) |
|---|---:|---:|---|
| clarify | 1 | 404 | — |
| plan | 1 | 707 | 930 (39695124406), 925 (39704124746) |
| implement | 1 | 1,332 | 1,604 (39704911716), 1,603 (39695459947) |
| review_autofix | 283 | 2,005 | 2,758 (39706570267), 2,655 (39697668091) |
| CI (parallel to review) | 888 | 1,345 | 1,455 (39706570200) |
| validate | 737 | 853 | 866 (39694337261) |
| test_and_mark_stable | 5,664 | 5,882 | 5,906 (39703422828) |

The plan, implement and clarify p50 values are skewed by skipped runs.

- **Compute:** review (up to about 46 min) and the release smoke (about 98 min) dominate.
- **Queueing:** 2.5-3 min hosted-runner waits during bursts (see Speed item 3).
- **Retry:** minimal. There was 1 lineage push retry, and all runs were attempt 1.
- **Merge/conflict:** no conflict-heal evidence. Forward-merge succeeded directly (39710715031).
- **Fix order by end-to-end impact:**
  1. Attribute and cut review-phase time and tokens (`or_phases` export).
  2. Instrument and trim the validation-refresh smoke.
  3. Remove full-history checkouts on no-op paths.
- **Collector data-quality fixes:**
  - p50 is meaningless with skipped runs mixed in (repo p50 2s). Compute duration percentiles over `success` and `failure` only.
  - `slow_runs` lists 39695710740 twice; deduplicate by `run_id`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Release cycle: promote 7,791s, of which TMS was 5,906s and validation-refresh 2,127-2,434s.
  - Review/autofix: p95 2,005s.
  - Full-history checkouts on PR close and forward-merge.
- **Top failure modes:**
  - validation-refresh watch timeout (39694107256, fixed in the repo).
  - Review checkout failing on a closed PR (39711230881, inferred).
- **Highest-cost drivers:** review runs 39707851300 (25.7M tokens) and 39706563694 (4.36M tokens, hit rate 0.337). TMS cache writes with no reads (248K).
- **Top 3 actions:**
  1. Export `or_phases` and stabilise the review prompt prefix (Semble and role text after shared content).
  2. Add the `ls-remote` head-ref-gone skip to `Checkout PR head branch`, after confirming the failing line in 39711230881.
  3. Stop discarding Semble stderr in `workflow-log-analysis.yml:453/1296`, and have the collector extract telemetry lines before truncation.

## Metrics Appendix

**Runs**

| Metric | Value |
|---|---:|
| Total runs | 1,000 |
| Success / failure / cancelled / other | 360 / 2 / 10 / 628 |
| Failure rate | 0.2% |
| p50 / p95 duration (s) | 2.0 / 936 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 5,857,600 (111 samples) |
| Runs with log telemetry | 115 |
| Sampled success runs | 4 (rate 0.07) |

**Tokens and cache**

| Scope | Calls | Uncached prompt | Completion | Cache read | Cache write | Total | `cache_hit_rate` |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repo | 72 | 5,250,520 | 121,029 | 23,412,994 | 1,789,288 | 30,325,579 | 0.7688 |
| review_autofix | 42 | 4,996,462 | 111,768 | 23,412,994 | 1,541,048 | 30,062,260 | 0.7817 |
| test_and_mark_stable | 30 | 254,058 | 9,261 | 0 | 248,240 | 263,319 | 0.0 |
| Codex | 0 | — | — | — | — | 0 | — |

`break_glass_count` = 0 and `context_budget_warn_count` = 0 (all families).

**GH API (from `GH_PAT_BUDGET`)**

| Run | Job | used_in_job | remaining |
|---|---|---:|---:|
| 39711634269 | heal intake | 53 | 4,096 |
| 39711636763 | heal intake | 31 | 4,061 |
| 39710714649 | post-merge-validate-dispatch | 4 | 4,329 |

There were 0 HTTP 429 events.

**Semble**

| Workflow | Queries | Logged bytes | Sources | Fallbacks (contract / runtime) | Bootstraps | Unused | Bootstrap ms |
|---|---:|---:|---:|---|---:|---:|---:|
| review_autofix | 4 | 67,220 | 20 | 0 / 0 | 4 | 0 | 180,752 |
| workflow_log_analysis | 0* | 0* | 0* | 0 / 0 | 7 | 7* | 260,443 |
| ci | 0 | 0 | 0 | 24 / 0 | 0 | 0 | 0 |
| test_and_mark_stable | 0 | 0 | 0 | 12 / 0 | 0 | 0 | 0 |
| **Total** | 4 | 67,220 | 20 | 36 / 0 | 11 | 7 | 441,195 |

\*Undercounted: `SEMBLE_QUERY` is suppressed by `2>/dev/null` at `workflow-log-analysis.yml:1296`. `static_dup_bytes_total` = 0 and `echo_lines_dropped` = 0.

**Serena / MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | Queries | Response bytes | Fallbacks |
|---|---:|---:|---:|---:|---:|---:|
| serena (all workflows) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**AI memory**

| Op | Count | ok | fail_open | Push retries |
|---|---:|---:|---:|---:|
| finalize-task | 3 | 3 | 1 (`no_linked_issues`) | 1 (39712392617) |
| retrieve | 0 observed | — | — | — |

**Data gaps and next collection steps**
1. The run-logs directory was not mounted, so 39711230881 and 39694107256 could not be read at step level. Fix the staging path or warn in the analysis step.
2. `or_phases` and `claude_*` counters are absent from the context.
3. Logs are truncated before telemetry extraction (poll lost 848,767 chars; heal intake about 59K).
4. There is no `retrieve` telemetry.
5. Only 4 runs carry token data because of the 0.07 success sampling.
## Deep Audit — Workflows & Scripts (2026-10-22)

> **Scope and method.** This pass favours depth over breadth. It reads in full: the PR-close path (`internal-cancel-on-pr-close.yml`, `cancel_on_pr_close.yml`, `workflow_failure_heal_pr_reconcile.sh`), `forward-merge-stable-to-main.yml`, `validation-refresh.yml`, `semble_helpers.sh`, `emit_event.sh`, `tg_helpers.sh`, `release_tag_preflight.sh` and `git_ref_health_check.sh`. It also covers the API call sites in `workflow_failure_heal_intake.sh`, chosen regions of `orchestrate_poll_process.sh`, `review_autofix.yml`, `test-and-mark-stable.yml`, `workflow-log-analysis.yml`, `validate.yml`, `implement.yml` and `validation-improvements-intake.yml`.
>
> Findings already filed earlier today in `analysis/workflow-optimization-2026-10-22.md` are cross-referenced and not re-filed. Examples: the `heal-pr-reconcile` `fetch-depth: 0` and missing timeout (its BUG-005), `RATE_LIMIT_BACKOFF` dead guards (its DEAD-001), TMS wait-loop expression sizes (its EXPR-001…004) and `set_issue_phase_label_resilient` read-modify-write (its BUG-002).

### Section 1: Bug & Correctness Sweep

**BUG-001** · `scripts/orchestrate_poll_process.sh:22043-22076` (`extract_recommended_answers`), callers `:16940`, `:21424` · Severity: **High** · Category: `bug`
- **Description:**
  - The helper fetches `repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?sort=created&direction=desc&per_page=50` with no `--paginate` (line 22049). It then picks the newest clarification comment with `max_by(created_at)` (line 22074).
  - The comment at line 22053 says "Search from newest to oldest (direction=desc)". However, the per-issue comments endpoint ignores `sort`/`direction`. The repo documents this itself at `scripts/tg_helpers.sh:194-197`: "The issue-comments endpoint has no `direction` parameter: the former `?per_page=30&direction=desc` returned the OLDEST 30 comments".
  - So on an issue with more than 50 comments, the helper only sees the **oldest 50**. It then either:
    - picks an earlier clarification batch, and the stall-recovery path posts `/answer [auto-answered-by-orchestrator]` with that batch's `(RECOMMENDED)` letters (lines 16940-16948, 21424-21437). Q-IDs restart at `Q1` in every batch, so stale letters bind to the new questions.
    - or finds nothing and posts the "issue description is deemed sufficient" answer.
  - Both are automated, user-visible decisions taken on the wrong data.
  - The line 22046 claim "50 is the same limit used by clarify.yml" no longer holds: `clarify.yml:1015` paginates.
- **Recommended fix:** fetch the whole history, using the same shape as `get_standalone_state_comment_id` (`:16391`). This path only runs on stall recovery, so ceil(C/100) calls is acceptable:
  ```bash
  comments_json="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/comments?per_page=100" 2>/dev/null | jq -s 'add // []' 2>/dev/null || echo "[]")"
  ```
  Also correct the comment at lines 22046 and 22053. Add a regression test with 60 comments where the newest clarification comment is #55.

**BUG-002** · `.github/workflows/cancel_on_pr_close.yml:167-186, 305-323`; `.github/workflows/internal-review.yml:2-8` · Severity: **Medium** · Category: `bug`
- **Description:**
  - On PR close, active runs are listed with `-f event=pull_request` and `-f branch=${PR_HEAD_REF}` (lines 172-173, 182-183), then filtered on `.head_branch == $head_ref` and `.event == "pull_request"` (lines 311-314).
  - Review runs dispatched by `review_autofix_sweep.yml`, the forward-merge fallback (`forward-merge-stable-to-main.yml:435`) or the autofix re-trigger are `workflow_dispatch` runs on the default branch. They are named `Internal: AI Review & Autofix [pr:<N>]` (`internal-review.yml:8`), so they can never match. They keep running all reviewer and editor LLM phases after the PR closes.
  - The scheduled path (lines 277-291) also requires `event == "pull_request"`.
  - This is consistent with the report's Reliability item 2: run 39711230881 was still reviewing closed PR #7849 *(inference — [NEEDS VERIFICATION] that it was a dispatched run)*.
- **Recommended fix:**
  - In the `pull_request` branch, drop the `event` and `branch` filters from `_cpr_fetch_active_runs`. Both calls stay status-filtered, so the call count stays at 2 and pagination stays bounded by the number of active runs.
  - Then match `(.event == "pull_request" and .head_branch == $head_ref and any(.pull_requests[]; .number == $n)) or (.event == "workflow_dispatch" and (.display_title | endswith("[pr:" + $pr_number + "]")))`.
  - This reuses the `[pr:N]` contract that `review_autofix_sweep.yml` and the poller's `_has_active_autofix_run` already depend on. [NEEDS VERIFICATION] that consumer `ai-review` wrappers carry the same `run-name`.

**BUG-003** · `.github/workflows/forward-merge-stable-to-main.yml:247-255, 283-362, 364-399, 401-439` · Severity: **Medium** · Category: `bug`
- **Description:**
  - The fallback branch is `auto/forward-merge-stable-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}` (line 255), so it is unique per run.
  - The only "already exists" handling (`resolve_existing_pr_number`, lines 247-253) looks up that same branch.
  - While a conflict PR is open, every later push to `stable` opens **another** fallback PR. Each one also sends another `ERROR` Telegram alert (line 399) and another `internal-review.yml` dispatch (line 435).
  - Nothing looks for an open `auto/forward-merge-stable-*` PR first.
- **Recommended fix:**
  - Before the push at lines 289/304, run one `gh pr list --base main --state open --json number,headRefName --jq '[.[] | select(.headRefName | startswith("auto/forward-merge-stable-"))] | .[0] // empty'` (1 call).
  - On the conflict path, when that returns a PR, fast-forward its head with `git push origin "refs/remotes/origin/stable:refs/heads/<existing_branch>"`. The `stable` ruleset forbids force pushes, so new `stable` descends from the old head. Then post one comment and skip the TG alert and the dispatch.
  - Keep the per-run branch only when no fallback PR is open.

**BUG-004** · `scripts/orchestrate_poll_process.sh:27326-27341, 29221-29244`; `.github/workflows/validation-improvements-intake.yml:460-487`; `.github/workflows/forward-merge-stable-to-main.yml:320-324` · Severity: **Medium** · Category: `bug`
- **Description:**
  - `unattended_system_instructions.md` §21 says the pipeline MUST run `scripts/lint_pr_body_auto_close.py` before every `gh pr create --body`.
  - Only `implement.yml:5994` and `update_workflows.yml:487/1440/2197` do.
  - The sites listed here build bodies from content the pipeline does not control:
    - `${RB_FIX_DESC}` from the judge, interpolated after `Closes #${rb_issue}` (line 27338; that it is judge-derived is an *inference* from the "Created automatically by the orchestrator judge" footer at 27341).
    - `${JUDGE_JUSTIFICATION}` (lines 29227, 29242).
    - `/tmp/self-heal-patches/summary.md`, derived from a consumer `repository_dispatch` payload (lines 475-477).
  - If any of these contains `Fixes #<tracking>`, the merge kills the orchestrator state machine (the #2734 incident).
- **Recommended fix:** before each call, write the title and body to files and run the same pre-flight as `implement.yml:5994` (`python3 scripts/lint_pr_body_auto_close.py …`). Treat a non-zero exit as `BLOCKED`: skip the PR, log `::error::[lint_pr_body_auto_close]` and post the tracking comment. The forward-merge body is static, so a lint call there is cheap belt-and-braces.

**BUG-005** · `.github/workflows/test-and-mark-stable.yml:916-925` (Phase 1 wait-clarify) · Severity: **Medium** · Category: `bug`
- **Description:**
  - `LAST_BOT_COMMENT` reads `.../comments?per_page=5&direction=desc`. Per `tg_helpers.sh:194-197`, that is the **oldest** 5 comments.
  - It then takes `.[0]` of `select(.user.login == "github-actions[bot]")`.
  - Pipeline comments are posted with GH_PAT, so their author is the PAT owner's login (stated at `orchestrate_poll_process.sh:22057-22058`). The filter therefore matches no pipeline comment.
  - So the "clarification needed / failed" fast-fail at lines 919-924 effectively never fires. A clarify failure only surfaces after `PHASE_TIMEOUT` minutes of inactivity, which lengthens failed release gates. [NEEDS VERIFICATION: the comment author in the smoke test repo]
- **Recommended fix:** merge this read into the line 891 comments read (see API-003). Fetch once with `per_page=100`, then select the newest trusted comment with `max_by(.created_at)` and the trust rule used at `orchestrate_poll_process.sh:22066-22070`: a `[bot]` login, or OWNER/MEMBER/COLLABORATOR.

**BUG-006** · `scripts/tg_helpers.sh:124-130, 186-188` · Severity: **Medium** · Category: `bug`
- **Description:**
  - `tg_send_msg` and `tg_delete_msg` call `curl -s -X POST` with no `--max-time`.
  - The sibling `tg_send_report_msg` uses `--max-time 30` (line 156), and the rate-limit alert in `gh_helpers.sh:421` uses `--max-time 10`.
  - A stalled Telegram endpoint blocks the caller until the job's `timeout-minutes`. Affected callers include `forward-merge-stable-to-main.yml:399/515` (job timeout 5 min) and `tg_send_tracked` in `review_autofix.yml:5349`.
- **Recommended fix:** add `--max-time 30` to line 124 and `--max-time 10` to line 186. The existing `|| { … return 0; }` fail-open stays as it is.

**BUG-007** · `scripts/semble_helpers.sh:333-354, 358` · Severity: **Low** · Category: `bug`
- **Description:**
  - The query runs under `timeout --preserve-status "${timeout_secs}s"`. With `--preserve-status`, GNU `timeout` returns the child's own status: 128+15 = 143 when SIGTERM kills it, not 124. `-k` is not set, so 137 never occurs either.
  - The `reason=timeout` branch (lines 350-353) is therefore unreachable for real timeouts. They are logged as `reason=exit=143 <stderr tail>` (line 358).
  - That field holds free text with spaces, which breaks the `key=value` grammar that `cost_audit.py` and `tests/test_cost_audit_semble_metrics.py:277,374,396` expect for `reason=timeout`.
- **Recommended fix:**
  ```bash
  if [ "${status}" -eq 124 ] || [ "${status}" -eq 137 ] || [ "${status}" -eq 143 ]; then
  ```
  Also sanitise the tail into its own field. For example, `stderr_tail="$(printf '%s' "${stderr_tail}" | tr -c 'A-Za-z0-9_.:-' '_' | cut -c1-120)"`, emitted as `"reason=exit-${status}" "detail=${stderr_tail}"`.

**BUG-008** · `scripts/workflow_failure_heal_intake.sh:1205-1227` · Severity: **Low** · Category: `bug`
- **Description:** the job-log read bypasses `gh_retry` on purpose (lines 1209-1210: unredacted temp-file concern). It therefore has **no retry at all**. One transient 5xx turns the evidence into `(job log unavailable)` (line 1226), which leaves the heal issue with no error signature and no downstream dedup.
- **Recommended fix:** wrap the pipe in a bounded loop (3 attempts; 2s/4s backoff) that re-runs `gh api … | python3 "${HEAL_PY}" redact-stream > "${RAW_LOG}"`. Redaction still happens before the first filesystem write, so the security rationale holds.

**BUG-009** · `.github/workflows/validation-improvements-intake.yml:463-465` · Severity: **Low** · Category: `bug`
- **Description:** the PR body renders `**Tracking issue:** #${TRACKING_ISSUE}` next to `**Source:** consumer run on ${CONSUMER_REPO}`. If `TRACKING_ISSUE` is a consumer-repo issue, `#N` auto-links to an unrelated issue or PR in this repo. [NEEDS VERIFICATION: which repo the dispatch payload's tracking issue belongs to]
- **Recommended fix:** render it as `${CONSUMER_REPO}#${TRACKING_ISSUE}` after validating `^[1-9][0-9]*$`.

**SEC-001** · `scripts/tg_helpers.sh:124, 156, 186`; `scripts/gh_helpers.sh:417-423`; raw sites `implement.yml:5508`, `test-and-mark-stable.yml:7722`, `validation-refresh.yml:353`, `update_workflows.yml:1741, 1772`, `check_failure_triage.yml:716`, `issue_pr_status.yml:969`, `scripts/implement_handle_guard_block.sh:162, 292, 464` · Severity: **Low** · Category: `security`
- **Description:** every Telegram call puts `bot${TG_BOT_SECRET}` in the curl URL, which means in argv. GitHub log masking covers logs, but not `/proc/<pid>/cmdline`. Any process sharing the runner, including agent subprocesses in the same job (`implement.yml:5508` runs inside the implement job), can read it while curl runs. *(Inference; no observed exposure.)*
- **Recommended fix:** pass the URL through curl's config on stdin:
  ```bash
  resp="$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "${TG_BOT_SECRET}" | curl -s --max-time 30 -X POST --config - -d "chat_id=${chat_id}" -d "disable_web_page_preview=true" --data-urlencode "text=${msg}" 2>/dev/null)" || return 0
  ```
  Do this in `tg_helpers.sh` and `gh_helpers.sh`, then route the raw sites through `tg_send_msg` (DUP-003).

**SEC-002** · `.github/workflows/validation-refresh.yml:82-86, 151-160` · Severity: **Low** · Category: `security`
- **Description:** `pip install --upgrade pip` and an unpinned `pip install pyyaml jsonschema jinja2` run in a job that later exports `GH_PAT`, which has cross-repo `repo` scope on every consumer, and `OPENROUTER_API_KEY`. A compromised or yanked release changes behaviour, or exfiltrates, with no repo change.
- **Recommended fix:** pin exact versions with `--require-hashes` in a checked-in `.github/ai/validation-refresh-requirements.txt` (no such file exists today; `tests/fixtures/.../requirements.txt` is the only one). Drop the `pip` self-upgrade.

### Section 2: GitHub API Call Redundancy Audit

**API-001** · `scripts/workflow_failure_heal_intake.sh:952-955, 1427-1434, 1593-1598` · Severity: **Low** · Category: `api-redundancy`
- **Description:**
  - The provenance gate reads `GET /user` into `PROVENANCE_LOGIN` (line 954).
  - The trusted-author block reads `GET /user` again (line 1433) without checking `PROVENANCE_LOGIN`. Its comment (lines 1431-1432) even names the other read.
  - The recovery block (lines 1594-1595) already reuses both values. On the self-repo `phase_failure` path both reads fire.
- **Calls:** current 2 → proposed 1.
- **Fix:** at line 1430, use `HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"` before falling back to the live call. This is the same cascade as lines 1594-1597.

**API-002** · `scripts/orchestrate_poll_process.sh:29215-29250` · Severity: **Low** · Category: `api-redundancy`
- **Description:**
  - The "API-based revert" calls `POST repos/…/pulls` with `head="revert-${PR_TO_REVERT}-$(date +%s)"`, a branch that was never created. GitHub always answers 422, which `gh_retry` treats as non-retryable (`gh_helpers.sh:178, 194`). The `|| { … }` git fallback therefore always runs.
  - The fallback creates its own `REVERT_BRANCH` with a different timestamp (line 29232). It branches from the local `${DEFAULT_BRANCH}`, not `origin/${DEFAULT_BRANCH}`, and ignores the exit codes of `git push` and `gh pr create`.
- **Calls:** current 1 wasted call per revert, plus the real create → proposed 1.
- **Fix:** delete the dead API attempt and run the git path directly:
  - `git fetch origin "${DEFAULT_BRANCH}"`
  - `git checkout -b "${REVERT_BRANCH}" "origin/${DEFAULT_BRANCH}"`
  - check the `git push` and `gh pr create` results, logging `::warning::` on failure.
  - Lint the body first (BUG-004).

**API-003** · `.github/workflows/test-and-mark-stable.yml:883-892, 916-918` · Severity: **Low** · Category: `api-redundancy`
- **Description:** each wait-clarify iteration calls `GET issues/N` (labels), `GET issues/N/comments` (for `length`) and `GET issues/N/comments?per_page=5…` (last bot comment). The last two hit the same endpoint. This loop runs every `POLL_INTERVAL` for up to `PHASE_TIMEOUT` minutes.
- **Calls:** current 3 per iteration → proposed 2.
- **Fix:** read comments once into `COMMENTS_JSON` with `per_page=100`, as Phase 2 does at line 1086. Derive `COMMENT_COUNT` with `jq 'length'` and the newest trusted comment with `max_by(.created_at)`; this also fixes BUG-005. This is not a merge of the labels call, so the `RATE_LIMIT_BACKOFF` coupling noted in `analysis/workflow-optimization-2026-10-21-4.md` (API-005) is unaffected. [NEEDS VERIFICATION] by the downstream pass.

**BATCH-001** · `scripts/orchestrate_poll_process.sh:20481-20511` (staged-support latch release) · Severity: **Medium** · Category: `api-batching`
- **Description:**
  - Every poll cycle lists all open `ai:needs-human` issues (line 20481).
  - Then, **inside the `for` loop**, it runs a full `--paginate` comments read for each issue (line 20508), only to find a staged-support latch marker.
  - This is a per-iteration API call in a §15 loop. The cost grows with both the number of latched issues and their comment history, on every one of the ~57 poll runs in the window.
- **Calls:** current 1 + Σ ceil(Cᵢ/100) per cycle → proposed 1 + ceil(N/50).
- **Fix:**
  - Extend the aliased-GraphQL pattern of `_fetch_candidate_issue_details_graphql` with a batch helper. Each alias reads `issue(number:N){comments(last:100){nodes{databaseId body createdAt authorAssociation author{login}}}}`.
  - Fall back per issue to today's REST read when the marker is not in the last 100 comments or the batch fails, so behaviour fails open.
  - Document the batching contract in the docstring (input/output shape, call count, fail-open). [NEEDS VERIFICATION: whether the latch marker can sit more than 100 comments back]

*Cross-references (not re-filed):* `workflow_failure_heal_pr_reconcile.sh:239-247` per-heal-issue pulls lookup (`analysis/workflow-optimization-2026-10-22.md` API-007); `cancel_on_pr_close.yml` schedule-path listing (same report, API-002). BUG-002 above keeps that step at 2 listing calls.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** · `.github/workflows/validate.yml:869-907`, `.github/workflows/implement.yml:1884-1921`, `.github/workflows/review_autofix.yml:2451-2498` · Severity: **Medium** · Category: `duplication`
- **Description:**
  - "Finalize workspace metadata", "Finalize workspace contents" and "Activate workspace shell context" are copied verbatim across three workflows. That is about 35 lines each, with 9 `${{ steps.workspace_* }}` interpolations per copy.
  - The copies have already drifted (CONSIST-002).
- **Proposal:**
  - Add a `finalize-metadata` mode to `scripts/workspace_init.sh` that does the cache-hit / restored-prefix decision now inline at, for example, `validate.yml:873-882`. Its inputs are `WORKSPACE_CACHE_PATH`, `WORKSPACE_CACHE_KEY`, `WORKSPACE_CACHE_RESTORE_PREFIX_ISSUE` and `WORKSPACE_CACHE_HIT`, all passed via step `env:`.
  - Each `run:` becomes `bash "${SUPPORT_SCRIPTS_DIR:-scripts}/workspace_init.sh" finalize-metadata`.
  - Keep each step's `name:`, `id:` and `if:` as they are (§10, §24). Update all three callers in one PR.

**DUP-002** · `.github/workflows/workflow-log-analysis.yml:358-382, 948-994, 1811-1855, 2410-2454` (bootstrap trio), `:447-455, 1286-1299, 1977-1991, 2579-2590` (prefetch); `.github/workflows/review_autofix.yml:2500-2575` · Severity: **Medium** · Category: `duplication`
- **Description:**
  - The "Setup uv / Install semble / Build semble index" trio is repeated 4 times in one workflow, and once more in `review_autofix.yml`.
  - The prefetch block (`source semble_helpers.sh; head -c 6000 <file> | iconv …; semble_query_block … 2>/dev/null`) is repeated 4 times. Each copy carries the same stderr-swallowing defect (CONSIST-001).
- **Proposal:**
  - A composite action `.github/actions/semble-bootstrap` (inputs: `index_path`; it keeps `continue-on-error` semantics inside) for the trio.
  - A sourced helper in `scripts/semble_helpers.sh`, `semble_prefetch_from_file <file> <max_chunks> <header_label>`. It does the byte-truncate, iconv and query, and lets `SEMBLE_*` stderr through.
  - Callers: the 4 WLA jobs, then `review_autofix.yml`.

**DUP-003** · Raw Telegram `curl` outside `tg_helpers.sh`: `implement.yml:5508`, `test-and-mark-stable.yml:7722`, `validation-refresh.yml:345-356`, `update_workflows.yml:1741, 1772`, `check_failure_triage.yml:716`, `issue_pr_status.yml:969`, `scripts/implement_handle_guard_block.sh:162, 292, 464` · Severity: **Low** · Category: `duplication`
- **Description:**
  - There are 10 hand-rolled sends.
  - They use divergent flags: `-s`, `-sS` or `-fsS`, and none sets a timeout.
  - Level filtering is divergent too: `validation-refresh.yml:345-350` re-implements the `ALERT_MSG_LEVEL` gate by hand, and the others have none.
  - Each copy also repeats the argv exposure from SEC-001.
- **Proposal:** source `scripts/tg_helpers.sh` and call `tg_send_msg "<msg>" "<LEVEL>"`. Map `TG_CHAT_ID` to `TG_ADMIN_CHAT_ID` where `_tg_chat_id` expects it. Then delete the per-site filter code.

**DUP-004** · `.github/workflows/forward-merge-stable-to-main.yml:66-81, 115-130, 203-218`; `.github/workflows/review_autofix.yml:2908-2923`; `.github/workflows/validation-improvements-intake.yml:442-454` · Severity: **Low** · Category: `duplication`
- **Description:** the same bounded git fetch/push retry loop (attempt counter, `2 ** n` backoff, stderr capture) is written out five times. Three of them are in one 515-line file. No helper exists: there is no `git_fetch_retry`-style function in `scripts/` or `gh_helpers.sh`.
- **Proposal:** add `scripts/git_retry_helpers.sh` (§9 style):
  ```bash
  git_retry()
  {
  	local max_attempts="$1"
  	shift
  	local attempt=1 backoff=2
  	while ! git "$@"; do
  		if [ "${attempt}" -ge "${max_attempts}" ]; then
  			return 1
  		fi
  		echo "::warning::git $1 failed (attempt ${attempt}/${max_attempts}); retrying in ${backoff}s" >&2
  		sleep "${backoff}"
  		attempt=$((attempt + 1))
  		backoff=$((backoff * 2))
  	done
  }
  ```
  Callers: the three forward-merge fetches and `review_autofix.yml:2908-2923`. For the latter, stage the script through `REQUIRED_BOOTSTRAP_SCRIPTS` per §24. Keep the push loops separate: their stderr classification, for example forward-merge lines 170-174, differs.

### Section 4: Expression Size Limit Risk Assessment

**EXPR-001** · `.github/workflows/test-and-mark-stable.yml` (whole file, 7,725 lines) · Severity: **Medium** · Category: `expression-limit`
- **Description:**
  - No workflow approaches the 1 MB / 800 KB ceiling: the largest are TMS at 7,725 lines, `implement.yml` at 7,061 and `review_autofix.yml` at 5,749.
  - The binding limit is the repo's §24 split guard at **480,000 bytes**. At the ~60-65 bytes/line typical of the regions read, TMS sits at an estimated **460-500 KB** *(inference — [NEEDS VERIFICATION] with `wc -c`)*.
  - Today's earlier audit recorded 7,714 lines, so the file is still growing.
- **Recommended fix:** run `wc -c .github/workflows/test-and-mark-stable.yml`. If it is ≥ 430,000 bytes, i.e. under §24's 50 KB headroom target, move the largest inline bodies into `scripts/` in the next PR that touches the file. Start with Phase 2 (`:962-1229`) and Phase 3b (`:1261-1551`), the same blocks as the earlier report's EXPR-001/002. Keep each step's `name:`/`id:`/`if:`/`env:` and pass every `${{ }}` through `env:`.

**EXPR-002** · `.github/workflows/review_autofix.yml:4956-5040` (Telegram editor-changes-lost), `:5047-5186` (Telegram editor-noop-suspicious), `:5200-5349` (Telegram review-blocked judge decision) · Severity: **Low** · Category: `expression-limit`
- **Description:**
  - These run bodies become template expressions **only** because of `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` (lines 4960-4961, 5051-5052, 5111, 5162, 5184, 5204-5205, 5209).
  - The judge-decision body is about 150 lines, an estimated **12-15 K chars** *(inference)*: 6-9 K of headroom, near the 15 K Medium line. It grows with every new `JUDGE_ACTION` / `JUDGE_SKIP_REASON` case.
- **Recommended fix:** replace each interpolation with the runner env vars `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. That removes these blocks from the expression limit entirely, with no change in behaviour. Doing it in the same PR as any new case keeps the risk at zero.

**EXPR-003** · `.github/workflows/review_autofix.yml:2833-2967` ("Checkout PR head branch") · Severity: **Low** · Category: `expression-limit`
- **Description:** a single `${{ github.repository }}` (line 2888) makes the whole ~135-line body an expression, an estimated **~9 K chars** *(inference)*, so ~12 K of headroom. The current report's Reliability item 2 proposes adding the `ls-remote` head-gone branch here, so this block is about to grow.
- **Recommended fix:** use `"${GITHUB_REPOSITORY}"` at line 2888 in the same PR that adds the `REVIEW_HEAD_REF_GONE` logic. Check the file with `wc -c` afterwards (§24).

*Cross-reference:* the TMS wait-loop bodies (Phase 1/2/3b and "Verify all phases passed") are already sized in `analysis/workflow-optimization-2026-10-22.md` EXPR-001…004. They are not re-measured here.

### Section 5: Cross-Cutting Concerns

**CONSIST-001** · `.github/workflows/workflow-log-analysis.yml:1988, 2588` · Severity: **Low** · Category: `consistency`
- **Description:** the current report's Reliability item 3 cites the `2>/dev/null` Semble stderr swallowing at `:453` and `:1296` only. The deep-audit prefetch (line 1988) and API-redundancy prefetch (line 2588) have the same defect. All four WLA bootstraps are therefore counted as unused by `cost_audit.py`.
- **Recommended fix:** apply the report's stderr-file pattern to all four sites, or fold them into `semble_prefetch_from_file` (DUP-002).

**CONSIST-002** · `.github/workflows/implement.yml:1899-1904` vs `validate.yml:883-889`, `review_autofix.yml:2466-2472` · Severity: **Low** · Category: `consistency`
- **Description:** `implement.yml` is the only copy that omits `WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE="true"`, and it has no occurrence anywhere in the file. `workspace_init.sh:181-185` then keeps reuse enabled even when `WORKSPACE_IDENTIFIER_SOURCE` is not `explicit`. [NEEDS VERIFICATION: implement may always pass an explicit identifier, making this a no-op]
- **Recommended fix:** add the variable to both implement metadata invocations, or document why implement is exempt. DUP-001 removes the drift for good.

**CONSIST-003** · `scripts/orchestrate_poll_process.sh:16391, 16400, 16657, 20508, 27794`; `.github/workflows/clarify.yml:1015`; `.github/workflows/orchestrate_clarify_respond.yml:726`; `.github/workflows/test-and-mark-stable.yml:1086` · Severity: **Low** · Category: `consistency`
- **Description:** these per-issue comments reads pass `sort=created&direction=asc|desc`, which the endpoint ignores (`tg_helpers.sh:194-197`). They are correct today only because they paginate everything or sort client-side. The misleading parameters are exactly what produced BUG-001 and BUG-005.
- **Recommended fix:** drop the ignored parameters. Where order matters, sort in jq (`sort_by(.created_at)`). Add a lint test that rejects `issues/[^/]+/comments?…direction=` in `scripts/` and `.github/workflows/`.

**CONSIST-004** · `.github/workflows/forward-merge-stable-to-main.yml:445-446, 494-503` · Severity: **Low** · Category: `consistency`
- **Description:**
  - The hard-failure alert calls the Jobs API with raw `curl -fsSL -H "Authorization: token …"`. It does not use `curl_gh_api` or `gh_retry gh api`, so it gets no retry or 429 handling.
  - The Summary step interpolates `${{ steps.check.outputs.ahead }}` and `${{ steps.direct.outputs.merge_status }}` straight into shell, where the rest of the file uses `env:`.
- **Recommended fix:** source `scripts/gh_helpers.sh` and use `gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs" --jq …`. Move the two outputs into the step's `env:`.

**SHELL-001** · `scripts/release_tag_preflight.sh:142-152` · Severity: **Low** · Category: `shellcheck`
- **Description:** `for scope in ${scopes}` with `IFS=','` is SC2086: an unquoted expansion with pathname globbing still on. It is harmless for real scope strings, but it is the only unquoted iteration in an otherwise strict script, and it mutates the global `IFS`.
- **Recommended fix:** `IFS=',' read -r -a scope_list <<< "${scopes}"; for scope in "${scope_list[@]}"; do … done`. This removes the `IFS_SAVE` dance.

**Dead code / tech-debt markers:**
- No `TODO`, `FIXME` or `HACK` markers exist in `.github/workflows/*.yml`, `scripts/*.sh` or `scripts/*.py` (searched case-sensitively).
- New dead paths found in this pass are filed where they cause harm: the unreachable `reason=timeout` branch (BUG-007), the always-422 revert API call (API-002) and the inert clarify failure detector (BUG-005).
- `RATE_LIMIT_BACKOFF` remains as in `analysis/workflow-optimization-2026-10-22.md` DEAD-001.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 9 | BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, BATCH-001, DUP-001, DUP-002, EXPR-001 |
| Low | 17 | BUG-007, BUG-008, BUG-009, SEC-001, SEC-002, API-001, API-002, API-003, DUP-003, DUP-004, EXPR-002, EXPR-003, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` (+1 test) | Small |
| API call optimization | `scripts/workflow_failure_heal_intake.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/cancel_on_pr_close.yml` | Medium |
| Code modularization | `.github/workflows/{validate,implement,review_autofix,workflow-log-analysis,forward-merge-stable-to-main,validation-refresh,update_workflows,check_failure_triage,issue_pr_status,test-and-mark-stable}.yml`, `scripts/{workspace_init,semble_helpers,tg_helpers,implement_handle_guard_block}.sh`, new `.github/actions/semble-bootstrap`, new `scripts/git_retry_helpers.sh` | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/review_autofix.yml` | Medium |
| Medium/Low fixes | `.github/workflows/{forward-merge-stable-to-main,validation-improvements-intake,validation-refresh,implement,workflow-log-analysis}.yml`, `scripts/{semble_helpers,tg_helpers,gh_helpers,release_tag_preflight,workflow_failure_heal_intake,orchestrate_poll_process}.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-22)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: static reading proves every precondition. Same endpoint and auth token, same function or step, no mutation in between, unchanged error and retry behaviour, no concurrency boundary crossed. For dead calls, the call is skipped under exactly the condition where its result is already thrown away. The implement stage may act on it directly.
- **`NEEDS_VERIFICATION`**: the change looks correct, but something cannot be proven by reading the code alone. A named check must pass first.
- **`RISKY_SKIP`**: at least one hard trigger applies: a retry or poll loop, pagination, a rate-limit or backoff gate, `orchestrate_poll_process.sh`, a race guard, or log keys that other code greps for. It is listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001** · `RISKY_SKIP`
- **Calls involved:**
  - `scripts/validate_process.sh:3522-3526` reads the tracking issue body (top-level "Assemble context").
  - `scripts/validate_process.sh:4851-4861` reads the same issue's state (the tracker-open guard before a fix-up issue is created).
- **Current → proposed:** 2 → 1 per validation run that fails on a tracking project.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue_number}`, both times.
- **Evidence:** the same object is read twice and only `.state` is new.
  ```bash
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}" --jq '.body // ""' > "${PROJECT_SPEC_FILE}"
  ```
  ```bash
  # Tracker-open guard: never open a fix-up against a closed tracker.
  # Fail-open on API failure (empty state) so a transient blip does
  # not block legitimate fix-up creation.
  TRACKER_STATE="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_RAW}" --jq '.state' 2>/dev/null | tr -d '[:space:]' || true)"
  ```
- **Proposed fix (not authorized):** at :3523, keep the whole issue JSON, write `.body` to `PROJECT_SPEC_FILE`, and keep `.state` in a new variable. At :4854, read that variable.
- **Safety rationale:** the :4854 read is a deliberate live guard against the tracker closing during a long validation run. The two reads also handle errors differently: :3523 has no `|| true`, while :4854 fails open. They also use different variables (`TRACKING_ISSUE_NUM` vs `TRACKING_ISSUE_RAW`). Merging them turns a race guard into a stale read.
- **Downstream signal:** do not auto-implement. A maintainer must decide whether a stale state is acceptable for the fix-up guard. As it stands, the duplicate read is the intended design.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** · `RISKY_SKIP`
- **Calls involved:**
  - `.github/workflows/implement.yml:6547-6561`: step "Capture post-Codex validation errors" (`id: capture_post_codex_validation_errors`). It writes `failed_step_name` to `$GITHUB_OUTPUT` at :6561.
  - `scripts/implement_diagnose_post_codex_failure.sh:257-281`: called by step "Diagnose post-Codex failure and create fix-up issues" (`implement.yml:6607-6658`).
- **Current → proposed:** 2–6 calls (each loop makes 1–3 attempts) → 1–3 per failed implement run, when the first step produced a name.
- **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100`, both times.
- **Evidence:**
  - Both steps have the same `if:` condition (`implement.yml:6512` and `:6608`), so they always run together.
  - The script's `FAILED_STEP_JOBS_JSON` is used only at :260-265. Only `FAILED_STEP_NAME` survives.
  - The workflow already exposes the same value as `steps.capture_post_codex_validation_errors.outputs.failed_step_name`, but the diagnose step's `env:` (:6610-6624) does not pass it in.
- **Proposed fix (not authorized):**
  - Pass `CAPTURED_FAILED_STEP_NAME: ${{ steps.capture_post_codex_validation_errors.outputs.failed_step_name }}` into the diagnose step.
  - In the script, skip the loop when that value is non-empty.
- **Safety rationale:**
  - Both calls sit inside 3-attempt race-window retry loops (`implement.yml:6538-6546` documents the jobs-API finalisation race).
  - The selectors differ. The capture step matches `failure`, and `cancelled`/`in_progress` only when `job.status == cancelled` (:6554-6557). The script matches `failure|cancelled|timed_out|action_required` (:265-275).
  - The later read can also see more finalised data. Reusing the earlier result could change the step name that diagnose prompts, Semble queries (:452-534) and issue comments (:825-930) use.
- **Downstream signal:** do not auto-implement. Manual review must first:
  - align the two selectors into one predicate;
  - confirm the capture step's early exit at :6534-6536 (empty `RUNTIME_DIR`) cannot leave an empty output that hides a real failure;
  - add a test covering a `timed_out` job.

### Dead Calls (DEAD-API-###)

**DEAD-API-001** · `SAFE_TO_MERGE`
- **Call:** `.github/workflows/forward-merge-stable-to-main.yml:493-504`, step "Telegram alert — hard failure" (:459-515).
- **Current → proposed:**
  - Today: 1 call per hard-failed run.
  - After the fix: 0 when `ALERT_MSG_LEVEL` is above DEBUG (the default is `WARNING`, :464) or Telegram is not configured.
  - Still 1 when `ALERT_MSG_LEVEL=DEBUG`.
- **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}/jobs`, as raw `curl`.
- **Evidence:**
  - `failing_step` is used only to build `msg` (:507-509). It is never logged or written to an output.
  - The message is sent at DEBUG (:515: `tg_send_msg "${msg}" "DEBUG"`).
  - `tg_send_msg` returns before sending when `_tg_should_send` fails or Telegram is not configured (`scripts/tg_helpers.sh:113-122`). `_tg_should_send` compares against `${ALERT_MSG_LEVEL:-WARNING}` (:86-90).
  - So with the default `ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'WARNING' }}`, the API result is always thrown away.
- **Proposed fix:** insert this right after `source scripts/tg_helpers.sh` (:484) and before `failing_step=""` (:493).
  ```bash
            # §14: skip the jobs lookup when tg_send_msg would drop this DEBUG alert anyway.
            if { type _tg_should_send >/dev/null 2>&1 && ! _tg_should_send "DEBUG"; } \
              || [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}" ]; then
              echo "Hard-failure Telegram alert suppressed at ALERT_MSG_LEVEL=${ALERT_MSG_LEVEL:-WARNING}; skipping jobs lookup."
              exit 0
            fi
  ```
  If this lands with Deep Audit CONSIST-004 (moving this `curl` to `gh_retry gh api`), add this gate first, so that the retrying call is gated too.
- **Safety rationale:**
  - The skip condition uses the same test that `tg_send_msg` applies before sending (`tg_helpers.sh:113-122`), so nothing observable changes. The step already fails open (`|| echo ''`) and has no log keys.
  - If `_tg_should_send` is missing, the guard falls through to today's behaviour.
- **Downstream signal:** add the guard block above to the "Telegram alert — hard failure" step of `forward-merge-stable-to-main.yml`, between `source scripts/tg_helpers.sh` and `failing_step=""`.

**DEAD-API-002** · `SAFE_TO_MERGE`
- **Call:** `.github/workflows/orchestrate_poll.yml:2018-2032`, step "Notify Telegram on job failure" (:1990-2040).
- **Current → proposed:** 1 call per failed poll job → 0 when the ERROR alert would be dropped. That happens on:
  - smoke-fixture-only cycles;
  - `ALERT_MSG_LEVEL=CRITICAL` or `SILENT`;
  - repos without Telegram secrets.
- **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}/jobs`, as raw `curl`.
- **Evidence:**
  - `failing_step` is used only in `msg` (:2034-2038), which is sent at ERROR (:2040).
  - The step's env is `ALERT_MSG_LEVEL: ${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'WARNING' }}` (:1995).
  - `orchestrate_poll.yml:413-417` sets `ALERT_MSG_LEVEL=SILENT` when every tracking issue is a test-and-mark-stable smoke fixture. Under SILENT, `tg_send_msg` drops the alert (`tg_helpers.sh:58-67, 83-91`), yet the jobs call at :2023-2026 still runs.
- **Proposed fix:** the same guard as DEAD-API-001, with `"ERROR"` in place of `"DEBUG"`. Insert it after `source scripts/tg_helpers.sh` (:2013) and before `failing_step=""` (:2021). Check the file with `wc -c` afterwards (§24).
- **Safety rationale:**
  - This is the `if: failure()` alert step of the poll workflow. It is not in `orchestrate_poll_process.sh`, not in a retry or poll loop, and not a race guard.
  - The skip condition uses the same test `tg_send_msg` applies before sending, and the call already fails open.
- **Downstream signal:** add the DEAD-API-001 guard block with level `"ERROR"` to the "Notify Telegram on job failure" step of `orchestrate_poll.yml`, between `source scripts/tg_helpers.sh` and `failing_step=""`.

### Cross-References to Deep Audit Section
- API-001: SAFE_TO_MERGE — agreed.
  - Same `GET /user`, same `GH_TOKEN` (the script never reassigns it), same step, no mutation in between.
  - The proposed `${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}` keeps the live fallback, so a failed :954 read still triggers :1433.
  - Net-new detail: `tests/test_workflow_failure_heal.py:6216` asserts exactly one `/user` call on the consumer-repo phase path. That path never sets `PROVENANCE_LOGIN` (:953), so the test stays green.
  - Also update the §14 comment at :1431-1432, and add a self-repo phase-failure test asserting a single `/user` call.
- API-002 (this report): RISKY_SKIP — the revert POST at `orchestrate_poll_process.sh:29221-29227` is in the orchestrator's budgeted auto-recovery path (:29210) inside `orchestrate_poll_process.sh`. Net-new detail: its git fallback also re-reads `pulls/{n}` for `merge_commit_sha` (:29230), so removing the dead POST still leaves 1 call per revert.
- API-003: RISKY_SKIP — the calls sit inside the TMS wait-clarify poll loop, behind the `RATE_LIMIT_BACKOFF` skip (`test-and-mark-stable.yml:887-889`).
  - Net-new detail: `COMMENT_COUNT` at :891-892 counts only the first page (GitHub's default `per_page` is 30), so it stops changing past 30 comments.
  - Moving to `per_page=100` changes the `CUR_STATE` string (:894) and so the inactivity-timer reset. Pair it with BUG-005 under manual review.
- BATCH-001: RISKY_SKIP — it is inside `orchestrate_poll_process.sh` and uses `--paginate`. Net-new details for whoever reviews it:
  - The loop also makes a second per-issue paginated call, `issues/{n}/events` at :20534.
  - `latch_idx` is a positional `to_entries` index into the full ascending history, reused at :20553 to find release markers after the latch. A `comments(last:100)` window changes what that index means.
  - The trust filter tests `user.login` against `\[bot\]$` (:20516, :20556). GraphQL `author.login` for Bot actors has no `[bot]` suffix *(to verify)*, so a naive port would drop bot-authored latches.
  - The `STAGED_SUPPORT_LATCH_*` log keys (:20484, :20501, :20509, :20531, :20535, :20550) must stay as they are.
- API-007 (`analysis/workflow-optimization-2026-10-22.md`): NEEDS_VERIFICATION — the per-heal-issue `GET pulls?head=…` at `workflow_failure_heal_pr_reconcile.sh:243` runs only after the `noop reason=no_heal_issue` exit at :103-106, which was hit in all 3 observed closes. Confirm that more than one heal issue per source PR actually occurs before batching it.
- API-002 (`analysis/workflow-optimization-2026-10-22.md`, `cancel_on_pr_close.yml` listing): RISKY_SKIP — both listing calls use `--paginate` (`cancel_on_pr_close.yml:167-186`), and the comment at :165-166 explicitly keeps two status-filtered calls to bound pagination.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 3 | DEAD-API-001, DEAD-API-002, API-001 (cross-ref) |
| NEEDS_VERIFICATION | 1 | API-007 (cross-ref, earlier report) |
| RISKY_SKIP | 6 | MERGE-001, REUSE-001, API-002 (cross-ref, this report), API-003 (cross-ref), BATCH-001 (cross-ref), API-002 (cross-ref, earlier report) |

### Implement-Stage Handoff
- **API-001**: in `scripts/workflow_failure_heal_intake.sh:1429-1434`, fall back to `PROVENANCE_LOGIN` before the live `GET /user`. This saves 1 call per self-repo phase-failure intake with lineage, the most frequent of the three.
- **DEAD-API-001**: gate the Jobs API lookup in the `forward-merge-stable-to-main.yml` "Telegram alert — hard failure" step on the same DEBUG check `tg_send_msg` uses. The call is dead at the default WARNING level.
- **DEAD-API-002**: gate the Jobs API lookup in the `orchestrate_poll.yml` "Notify Telegram on job failure" step on the same ERROR check `tg_send_msg` uses. The call is dead on SILENT smoke cycles and in repos without Telegram.