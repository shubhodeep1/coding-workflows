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