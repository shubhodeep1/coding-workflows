## Executive Summary

> **Data-coverage caveat (stated up front):** I couldn't open the run-logs directory `/home/runner/work/_temp/workflow-log-output`; it does not exist in this sandbox. The logs for the two zero-second failures returned HTTP 404. The evidence below therefore comes from `analysis_context.json`: the aggregates, the `failure_point` fields, and the gpt-6-luna `log_summary` rows. Where a finding rests on code, I read the repo files directly. `insufficient_data=false`. However, the 1000-run window covers only about 14.5 hours (first run 2026-10-20T11:17Z, last 2026-10-21T01:45Z), because 671 of the 1000 runs are skipped no-op runs.

- **Waiting for a runner is the main cost in wall-clock time for short workflows. Impact: high. Confidence: high for the sampled runs.** 28 `log_summary` rows give a queue time. Together they spent about 5,773s of 9,449s (about 61%) waiting for "All GitHub-hosted runners with label [ubuntu-latest] are busy". The median wait was about 213s and the worst were 607s (`39526755130`) and 617s (`39526504456`). A 3-second lint job took 617s end-to-end. The 2026-10-20 report saw the same pattern on 2026-10-19.
- **Review/autofix accounts for 99.8% of model tokens, and about a third of its input tokens are not cached. Impact: high cost. Confidence: high.** Review used 36,078,624 of the window's 36,149,483 OpenRouter tokens, all in three runs: `39520815095` (16.49M), `39524724854` (13.54M) and `39518391306` (6.05M). Their 54 calls averaged about 223k uncached prompt tokens each. The cache hit rate was 0.595. Moving it to 0.75 would shift about 5.5M input tokens per window from uncached to cache-read (an estimate).
- **The Semble numbers for the log-analysis workflow are partly a measurement artefact, and its prefetch adds little. Impact: medium. Confidence: high.** `workflow-log-analysis.yml` calls `semble_query_block ... 2>/dev/null` at lines 453, 1296, 1988 and 2588. `semble_helpers.sh` writes `SEMBLE_QUERY`/`SEMBLE_FALLBACK` to stderr, so those lines are thrown away. That is why run `39522260817` reports 6 bootstraps, 222,316 ms and **0 queries**, which the collector counts as "unused". Separately, the query text is the first 6000 bytes of the analysis-context JSON, and the six chunks returned for this report were all field-name lists from `scripts/cost_audit.py`.
- **There were only 4 failures (0.4% of all runs, 1.2% of runs that reached a conclusion), and none of them can be diagnosed from the logs. Impact: medium. Confidence: high.**
  - Two are zero-second startup failures with no logs (`39520745014` internal-review.yml and `39520744520` internal-workflow-failure-heal.yml), both at 00:20:43, the same second as CI `39520745173` on PR #7748.
  - `39525232219` failed in "Checkout PR head branch". That step has 8 distinct exit paths, and 3 of them print no reason.
  - `39520745173` failed in "Validation self-test unit tests". That step runs 20 test scripts in sequence and doesn't say which one failed.
  - The fix is additive diagnostic logging (Reliability items 1–3).
- **Each AI-memory operation takes about 2 minutes. Impact: medium latency. Confidence: medium.** "Finalize linked issue lineage state" took 118s (`39525860406`) and about 125s (`39527623451`). In `39525927004` the five `ai_memory.py` calls were spaced about 2 minutes apart. One push needed a retry (`push_attempts: 2`). No `retrieve` telemetry appears in any sampled log.
- **The orchestrator poller is a slow black box. Impact: medium. Confidence: medium.** It ran 78 times with p50 642s and p95 878s; 9 runs (11.5%) were cancelled. In `39525909764` the poll step took about 11.6 minutes and about 838k characters of its log were truncated. `POLL_PHASE_TIMING`, recommended in `analysis/workflow-optimization-2026-10-20.md`, has still not been added to the code (it only appears in `analysis/*.md`).
- **Two telemetry gaps hide spend and API load. Confidence: high.** The context has no `claude_*` cost fields, although `cost_audit.py` tracks them (lines 1080–1082) and the integration PRs #7743 and #7756 carry `ai:engine-claude`. It also has no GitHub API rollup, although `collect_workflow_logs.py:109-113` parses `GH_PAT_BUDGET phase=end`.

## Speed Optimizations

Ranked by expected latency reduction on the critical path.

1. **Request fewer runners for each PR or merge event. Critical path.**
   - **Evidence:** the PR #7744 merge and the PR #7743 sync at 01:33:00–01:33:02 started at least 6 jobs that needed a runner:
     - issue-PR status sync `39527963771`, which waited about 200s;
     - cancel-on-close `39527963782`, two jobs, with `heal-pr-reconcile` waiting 69s;
     - post-merge dispatch `39527963733` (about 237s);
     - readiness `39527967541` (about 227s);
     - lint `39527967406` (293s);
     - review gate `39527967628` (343s).
   - Each of these does 0.5–4s of actual work (e.g. readiness check 01:37:14.38→14.87; lint 01:37:58→01:38:01).
   - **Root cause (inference):** fan-out of many tiny workflows on every event, competing with long CI and review jobs for a fixed hosted-runner concurrency limit.
   - **Changes (no new infrastructure):**
     - (a) `.github/workflows/ci.yml` has no `concurrency:` block. Add one that cancels superseded **pull_request** CI runs only:
       ```yaml
       concurrency:
         group: ci-${{ github.event.pull_request.number && format('pr-{0}', github.event.pull_request.number) || github.ref }}
         cancel-in-progress: ${{ github.event_name == 'pull_request' }}
       ```
       Caveat: the sweep step "Re-run cancelled CI failed jobs once" (`CI_CANCELLED_RERUN`, seen in `39528828061`) must not re-run CI that was cancelled because a newer run superseded it. Check its `reason=` filter before shipping.
     - (b) `internal-cancel-on-pr-close.yml:50` uses `fetch-depth: 0` for `heal-pr-reconcile`. The run summaries show it fetching about 100 `v1.x` tags, and the checkout in `39525860360` ran 01:13:05→01:15:14. If `scripts/workflow_failure_heal_pr_reconcile.sh` works only through the API, switch to `fetch-depth: 1`; check this first.
   - **Do not** gate `integration-pr-readiness.yml` on branch name. Its comment (lines 45–49) says it must run on every PR so that the required status context exists.
   - **Estimated savings:** 2–6 minutes of queue time per short job while the queue is backed up (the medians above). Superseded PR CI can save 15–30 runner-minutes per redundant push (CI p50 899s).
   - **Risk:** low for (b), medium for (a) because of the interaction with the sweep re-run.
2. **Make the 2-minute AI-memory operations faster. Critical path for issue close-out and memory maintenance.**
   - **Evidence:**
     - finalize step 118s of 213s (`39525860406`);
     - finalize step about 125s of 555s (`39527623451`);
     - a 2-minute gap before `AI_MEMORY_TELEMETRY` in `39527963771`;
     - five calls spaced about 2 minutes apart (01:17:47→01:26:12) in `39525927004`, and the compact step took 128s.
   - **Root cause:** unknown. Time is spent somewhere among clone, fetch, commit and push, and nothing is instrumented.
   - **Change:** add timing fields to the existing `AI_MEMORY_TELEMETRY` JSON in `scripts/ai_memory.py` (extending an existing mechanism rather than adding a new prefix): `fetch_ms`, `commit_ms`, `push_ms`, `total_ms`. If the time turns out to be in the clone, reuse one checkout across the maintenance job's calls.
   - **Estimated savings:** if the cost is per-call setup, about 6–8 of the 16 minutes of memory maintenance, and about 1.5 minutes per issue close. This is an inference.
   - **Risk:** low (fields only added).
3. **Instrument the poller before optimizing it. Prerequisite for a large win.**
   - **Evidence:** 78 runs with p50 642s; the poll step in `39525909764` ran 01:12:13→01:23:52 with its log truncated.
   - **Change:** add the `POLL_PHASE_TIMING phase=<name> ms=<int> items=<int> gh_calls=<int>` line already specified in the 2026-10-20 report. `scripts/orchestrate_poll_process.sh` already has 33 `date +%s`/`SECONDS` call sites, but none emits a structured prefix.
   - **Savings:** unknown until it is measured.
   - **Risk:** low. Per §25, register the new prefix in an `agents.d/` fragment, not in `agents.md`.
4. **Micro: Semble bootstrap time in the log-analysis workflow.** There were 6 bootstraps totalling 222,316 ms, about 37s each, across the jobs at `workflow-log-analysis.yml` lines 358, 949, 1817 and 2416. All of them use an eager install plus index; none is gated on `SEMBLE_BOOTSTRAP_MODE`. Fix the telemetry first (Cost item 2) before deciding whether to make this lazy.

## Cost Optimizations

1. **Raise the review/autofix cache hit rate. Largest token lever.**
   - **Evidence:**
     - `review_autofix` had 54 calls: 12,066,395 uncached prompt tokens, 21,219,108 cache-read, 2,347,935 cache-write, 445,456 completion; `cache_hit_rate` 0.595.
     - Per run: `39524724854` 0.637, `39520815095` 0.582, `39518391306` 0.539.
     - Uncached prompt per call: 264k (`39520815095`), 180k, and 229k.
     - `context_budget_warn_count=0` and `break_glass_count=0`, so prompt size is not tripping guards.
   - **Root cause (inference):** large dynamic content (diff, findings, file context) sits after the last cache breakpoint, or varies between reviewer passes. About 9.7M input tokens were neither written to nor read from the cache.
   - **Change:** log a stable-prefix hash and the cached-versus-uncached split on each call (see the Prompt Cache section). Then move invariant blocks (rubric, system instructions, repo rules) ahead of the per-PR diff and place the cache breakpoint after them.
   - **Estimated savings:** about 5.5M tokens per window move from uncached to cache-read if the hit rate reaches 0.75. Output quality would not change.
   - **Quality risk:** none, since only ordering changes, provided the content is identical.
2. **Fix the hidden Semble telemetry and the low-value query text in the log-analysis workflow.**
   - **Evidence:** `semble_query_block ... 2>/dev/null` at `workflow-log-analysis.yml` lines 453, 1296, 1988 and 2588. `scripts/semble_helpers.sh:28-60` prints `SEMBLE_QUERY`/`SEMBLE_FALLBACK` to stderr (`>&2`), so every query and fallback line is lost. The collector then marks all 6 bootstraps as unused (`cost_audit.py:879-880`). The prefetch in this prompt returned 6 chunks, all of them field-name tuples from `scripts/cost_audit.py`, because the query is `head -c 6000` of a JSON file whose first 6KB is mostly keys.
   - **Change (diagnostic logging, same at each site):**
     ```bash
     wla_semble_err="$(mktemp)"
     WLA_SEMBLE_PREFETCH="$(semble_query_block "${wla_query_text}" 6 "Workflow Log Analysis Context" 2>"${wla_semble_err}" || true)"
     grep -E '^SEMBLE_(QUERY|FALLBACK) ' "${wla_semble_err}" >&2 || true
     rm -f "${wla_semble_err}"
     ```
     As a follow-up, build `wla_query_text` from `failing_runs[].failure_point` step names and workflow names, not from the raw JSON head.
   - **Savings:** about 222s of runner time per analysis run if, after measurement, the prefetch proves useless. Otherwise this restores 4 queries' worth of telemetry per run.
   - **Risk:** very low.
3. **Semble in review/autofix is cheap and adds no duplicated content.** 5 queries, 82,338 bytes (about 16.5KB each, roughly 20k tokens, 0.06% of review tokens), 33 sources, `static_dup_bytes_total=0`, and 220,847 ms of bootstrap (about 44s per run, 0 unused).
   - It is not adding noise, but there is no evidence yet that it reduces prompt growth.
   - **Change:** none now. Keep it, and judge it against the per-call prefix telemetry from item 1.
4. **Avoid long reviews on e2e smoke-test PRs that will be closed.** Review `39525908172` on #7758 (labels `force-review` and `e2e-smoke-test`) ran 1055s and was cancelled at 01:29:36, exactly when #7758 closed (cancel-on-close `39527623411` at 01:29:35).
   - **Change:** none to behavior. The smoke test may intentionally exercise review. Track it as a known cost line.
5. **Small: cache writes that are never read.** `test_and_mark_stable` (`39522103038`) made 16 calls: 53,936 cache-write tokens, 0 reads, 58,375 prompt. `memory_maintenance` (`39525927004`) wrote 7,842 tokens and read 0.
   - **Change:** find the calling path and drop cache_control on one-off calls.
   - **Savings:** about 60k write-premium tokens per window, which is negligible.

## Reliability Improvements

Ranked by expected reduction in time-to-diagnose and rerun rate. All items are additive logging that fails open.

1. **Explain every failure exit in "Checkout PR head branch" (`review_autofix.yml:2811-2945`).**
   - **Evidence:** `39525232219` failed at `review / codex-agent` → "Checkout PR head branch" after 1255s, with no AI token usage.
   - **Root cause category:** unknown, because the step has 8 failure exits:
     - `::error::` workspace unavailable (2841);
     - OpenCode configuration present (2846);
     - plain `echo "Unable to determine PR head branch"` without `::error::` (2852);
     - fetch exhausted (2895);
     - rev-parse failed (2927);
     - three silent `set -e` exits: `git_ref_health_check.sh repair` (2873), `git reset --hard` (2905) and `git checkout -f -B` (2923).
   - **Fix:**
     ```bash
     trap 'rc=$?; echo "REVIEW_HEAD_CHECKOUT_FAIL reason=unhandled line=${LINENO} rc=${rc} cmd=${BASH_COMMAND%% *}"' ERR
     # before each explicit `exit 1`, e.g. at 2852:
     echo "REVIEW_HEAD_CHECKOUT_FAIL reason=head_ref_missing"; echo "::error::Unable to determine PR head branch"
     ```
     Use these reason labels: `workspace_unavailable`, `opencode_config`, `head_ref_missing`, `fetch_exhausted`, `head_unreadable`.
   - **Impact:** turns an investigation that needs the full logs into a single grep. It also lets the heal intake distinguish infrastructure failures from data failures.
   - **Rollback/fail-open:** echo only, no behavior change. Watch the §24 file-size limit (adds about 10 lines).
2. **Name the failing script in "Validation self-test unit tests" (`ci.yml:466-488`).**
   - **Evidence:** `39520745173` failed at `static-checks`, 798s, in a step that runs 20 test scripts under `set -euo pipefail` with nothing marking which script is running. The 8 `SEMBLE_FALLBACK context=contract-test` lines in that run are expected test output.
   - **Fix:** add as the first line after `set -euo pipefail`:
     ```bash
     trap 'echo "CI_SELFTEST_SCRIPT outcome=fail rc=$? cmd=${BASH_COMMAND##*python3 }"' ERR
     ```
   - **Impact:** check-failure triage can route the fix straight to the right test owner.
   - **Rollback:** delete the line. It keeps the existing fail-fast behavior.
3. **Classify startup failures in the collector.**
   - **Evidence:** `39520745014` (internal-review.yml) and `39520744520` (internal-workflow-failure-heal.yml) both show `duration_seconds=0`, null job and step, and `missing_log_archive` with HTTP 404, at 00:20:43. That is the same second as CI `39520745173`, which the sweep in `39528828061` ties to `pr=7748`.
   - **Inference:** this is GitHub's "workflow file issue" signature (see §24) on #7748's merge ref.
   - **Fix:** in `scripts/collect_workflow_logs.py`, mark runs with conclusion=failure, duration 0 and a 404 log fetch as `failure_class=startup_failure`, and exclude them from the step-level failure tallies.
   - **Also:** run `wc -c .github/workflows/*.yml` on #7748's head to rule out the 512,000-byte limit.
   - **Impact:** stops 2 of the 4 "failures" from looking like code regressions.
4. **Record why test-and-mark-stable was cancelled.** `39522103038` was cancelled after 3920s at `validate-scripts` → "Unit tests". Its parent promote, `39521955076`, ran 4049s. A second promote, `39526410615` at 01:17, dispatched a new run `39526632477` for v1.37.0.
   - **Fix:** emit a cancel reason (timeout versus superseded) from the promote cycle. The cause can't be determined from what was available.
5. **Semble fallbacks look healthy.** 22 fallbacks in total: 20 `contract_test` (CI 12, test_and_mark_stable 8; expected test output) and 2 runtime (implement `39512526380`, reason not visible). Bootstraps failed: 0.
   - This is rare fail-open behavior, not a broken rollout.
   - **Mitigation:** none, beyond keeping `reason=` in the fallback lines.
6. **Serena is disabled.** Every summary shows `SERENA_ENABLED: false`, and probe ok/failed/skipped are all 0. There is no availability risk to assess.
7. **`BREAK_GLASS` = 0 and `CONTEXT_BUDGET_WARN` = 0** across all 113 runs with telemetry. There is no sign of rubric or prompt-size pressure.

## AI Memory Health

`AI_MEMORY_TELEMETRY` lines found in the `log_summary` rows:

| op | count | ok | push_attempts | Notes |
|---|---|---|---|---|
| finalize-task | 3 | 3/3 | 1, 1, 1 | issues 7742 (merged, `39527963771`), 7752 (closed, `39527623451`), 7751 (closed, `39525860406`) |
| record-candidate | 2 | 2/2 | 1, **2** | `39525927004`, record_ids `mem_20261021011941_a04c060414`, `mem_20261021012353_b4ee27a656` |
| compact | 1 | 1/1 | — | month 2026-09: archived_candidates 2716, archived_ledgers 3, removed 0, commit `b53627971f4c` |
| retrieve | **0** | — | — | not observed |

- **Retrieve hit rate, `estimated_tokens` against budget, and `keyword_method` mix:** these can't be computed because no `retrieve` events appear in the sampled logs. The review and implement deep-dive logs, where retrieves happen, were not readable.
  - **Action:** in the next window, confirm that `retrieve` events are emitted in an implement run and a review run. Grep for `"op": "retrieve"`.
- **Flags:**
  - There were no `fail_open: true` or `enabled: false` entries.
  - One push retry out of 6 writes is acceptable.
  - The 2-minute latency per operation is the main health concern (Speed item 2).
- **Lineage staleness (observation; cause unverified):**
  - Lineage `issue-7751` still shows implement run `39523440305` as `in_progress`, though that run succeeded in 1524s.
  - Lineage `issue-7752` shows implement run `39523619748` as `in_progress`, though it succeeded in 1791s.
  - The final state is correct, but the per-run status was never closed out.
  - I found no `record-run-event` call in `.github/workflows/`.
  - **Smallest fix:** have `finalize-task` mark child run entries as terminal when it writes the final state.

## GH API Call Audit

- **What was observed (low volume, no throttling):**
  - Sweep `39528828061`: `GH_PAT_BUDGET ... used_in_job=5 remaining=3702`.
  - Post-merge dispatch `39527963733`: `used_in_job=2 remaining=3671`, with the same `reset=1792547948`. The shared PAT dropped by 31 calls within about 1 minute across all consumers.
  - Lint runs make 1 paginated `pulls/<n>/commits?per_page=100` call each (`39527967406`, `39527624219`, `39526755130`, `39526723917`, `39525907931`).
  - Historical triage `39526504652`: about 3 calls plus a paginated issue read.
  - No HTTP 429, secondary rate-limit, or `gh_retry` backoff events appear in any summary.
- **Hotspot that can't be seen:** the poller, the largest suspected consumer (78 runs, about 11.6 minutes each). Its `GH_PAT_BUDGET phase=end` line is in the truncated part of the log (838k characters dropped in `39525909764`).
- **Recommendations:**
  1. Add `gh_calls=` to `POLL_PHASE_TIMING` (Speed item 3). This is the only way to rank the poller's phases against the §14 batch helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`).
  2. `collect_workflow_logs.py:1778` already parses `GH_PAT_BUDGET phase=end`, but `analysis_context.json` has no API rollup. Pass the per-workflow `used_in_job` sums into the context. This adds no API calls; it reuses the archives the collector already downloads.
  3. Scheduled cancel-on-close runs (`39528674448`, `39526159042`) warn "preserved 5/10 active run(s) without pull-request linkage". This is fine, but log the count of list calls per sweep so its cost is visible.
- **Estimated reduction:** can't be quantified until the poller phases are measured. Rate-limit risk this window is low (about 3.7k of the budget remaining).

## Prompt Cache & Memory System

- **cache_hit_rate:**
  - Repo-wide 0.593 (21.22M read / 35.76M total input).
  - review_autofix 0.595.
  - memory_maintenance 0.0 (2 calls, 7,842 written, 0 read).
  - test_and_mark_stable 0.0 (16 calls, 53,936 written, 0 read).
- **Likely causes of cache misses (inference; needs the telemetry below to confirm):**
  1. In review, about 9.7M input tokens were neither read from nor written to the cache. That points to large dynamic content after the last breakpoint.
  2. The hit rate varies between runs (0.539 vs 0.637), and the 10-call run has the lowest. Fewer calls per run means fewer chances to reuse a cached prefix.
  3. Single-shot callers write cache entries that are never read.
- **Diagnostic addition:** on each OpenRouter usage line, append `prefix_sha=<first 12 hex of sha256(stable prefix)>` and `breakpoint_bytes=<int>`. Then a different prefix hash between reviewer passes of the same run directly shows avoidable variation in the prompt.
- **Expected impact:** reaching 0.75 moves about 5.5M tokens per window to cache-read, and should reduce time to first token on review calls (inference).
- **Memory retrieval effectiveness:** can't be assessed (no `retrieve` telemetry; see AI Memory Health).

## Orchestrator Health

- **Projects in flight:**
  - #7741: integration PR #7743, branch `orchestrator/project-7741`.
  - #7745: integration PR #7756.
  - Readiness runs `39526755204`, `39527967541` (#7743) and `39526723926` (#7756) all warn "3/3 sub-issues ... still unchecked".
  - Sub-issue PR #7744 merged into `orchestrator/project-7741` at about 01:33 (issue 7742 `final_state: merged`). The readiness run at 01:37:14 still showed 3/3 unchecked.
  - This is expected: checkboxes are ticked by the poller ("Orchestrator-managed issue — skipping PR merged alert (poller handles completion)").
- **Indicator to track:** the delay between a sub-PR merging and its checkbox being ticked, which with the current p50 should stay under one poll interval plus 642s.
- **Poller:** 9 of 78 runs cancelled (11.5%), cause not visible. `SEMBLE_AVAILABLE=false` with `SEMBLE_BOOTSTRAP_MODE: lazy` in `39525909764` is consistent with lazy mode before the first query, and is not a fault.
- **Auto-close discipline is working:** integration PR bodies use `Refs #7741` and `Refs #7745`. The sub-issue smoke PR #7758 uses `Closes .../issues/7752`, which is correct for a sub-issue. All lint runs passed.
- **Clarification and skip noise:** 134 of 135 clarify-respond runs, 140 of 148 clarify, 129 of 137 plan and 127 of 136 implement runs were skipped. Each issue event fans out into about 4 no-op runs (for example 01:45:08: `39529117862`, `39529117851`, `39529117868`, `39529117947`). This is healthy gating, but it distorts the collector's metrics (Pipeline item 4).
- **Smallest mitigations:** ship `POLL_PHASE_TIMING`, and log a cancellation reason on poller cancels.

## Pipeline Flow Bottlenecks

Durations below are measured per workflow family; the end-to-end split is an inference.

| Stage | p50 / p95 (s) | Dominant overhead |
|---|---|---|
| clarify | 1 / 168 | skip gating (140/148 skipped) |
| plan | 1 / 205 (slow runs up to 970) | compute |
| implement | 1 / 1193 (slow runs up to 2081) | compute |
| CI | 899 / 1851 | compute plus queue; no concurrency group |
| review_autofix | 21 / 1913 (slow runs up to 3036) | model compute (3 runs = 36M tokens) |
| validate | 768.5 / 824 | compute |
| orchestrate_poll | 642 / 878 | unknown (log truncated) |
| short PR hooks (lint, readiness, status, cancel) | 15–184 / 162–676 | **runner queue (about 61%)** |

1. **Queueing.** Biggest effect end-to-end because it compounds across every stage. See Speed item 1.
2. **Review compute.** Slowest single stage on the critical path. See Cost item 1; caching gains should also reduce latency.
3. **Merge and close-out overhead:** about 2 minutes of AI-memory finalization per issue (Speed item 2).
4. **Measurement distortion.** Skipped runs (671/1000) pull the repo-wide p50 down to 1s and shrink the window to about 14.5 hours. **Collector fix:** compute p50/p95 over runs that are not skipped, and exclude `conclusion=skipped` from the 1000-run cap.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - hosted-runner queue (median wait about 213s; up to 617s);
  - review/autofix runs of 2100–3036s;
  - the poller at about 11 minutes per cycle;
  - AI-memory operations at about 2 minutes each.
- **Top failure modes:**
  - two startup failures with no logs, tied to PR #7748 (`39520745014`, `39520744520`);
  - review checkout failure with no recorded reason (`39525232219`);
  - CI self-test failure with no script named (`39520745173`);
  - test-and-mark-stable cancelled with an unknown cause (`39522103038`).
- **Highest cost drivers:**
  - review_autofix: 36.08M tokens over 3 runs, about 33.9% of input uncached;
  - about 222s of Semble bootstrap per log-analysis run whose usage can't be measured.
- **Top 3 actions:**
  1. Add the `REVIEW_HEAD_CHECKOUT_FAIL` and `CI_SELFTEST_SCRIPT` diagnostic lines and replay Semble stderr in `workflow-log-analysis.yml`. All are additive and low-risk.
  2. Add `prefix_sha` and `breakpoint_bytes` to the review usage lines, then reorder prompts to raise the cache hit rate toward 0.75.
  3. Ship `POLL_PHASE_TIMING` (with `gh_calls=`) and timing fields in `AI_MEMORY_TELEMETRY`; then add PR-scoped CI concurrency once the sweep re-run interaction is checked.

## Metrics Appendix

**Run outcomes (window: 1000 runs, about 14.5 hours)**

| Metric | Value |
|---|---|
| total / success / failure / cancelled / other(skipped) | 1000 / 313 / 4 / 12 / 671 |
| failure rate (all runs / runs with a conclusion) | 0.4% / 1.2% |
| p50 / p95 duration (all runs) | 1s / 869s |
| runs with log telemetry | 113 |
| success sample rate / sampled success runs | 0.07 / 1 |

**Per-family (selected)**

| Family | runs | success | fail | cancel | p50 s | p95 s |
|---|---|---|---|---|---|---|
| review_autofix | 55 | 50 | 2 | 1 | 21 | 1913 |
| ci | 26 | 25 | 1 | 0 | 899 | 1851 |
| orchestrate_poll | 78 | 69 | 0 | 9 | 642 | 878 |
| implement | 136 | 8 | 0 | 1 | 1 | 1193 |
| plan | 137 | 8 | 0 | 0 | 1 | 205 |
| promote_main_to_stable | 2 | 2 | 0 | 0 | 2095 | 3854 |
| test_and_mark_stable | 1 | 0 | 0 | 1 | 3920 | 3920 |
| workflow_failure_heal | 77 | 0 | 1 | 0 | 1 | 9 |

**Tokens and cache (OpenRouter; `claude_*` and `codex_*` = 0 or absent)**

| Scope | calls | prompt (uncached) | completion | cache read | cache write | total | cache_hit_rate |
|---|---|---|---|---|---|---|---|
| repo | 72 | 12,132,618 | 450,092 | 21,219,108 | 2,409,713 | 36,149,483 | 0.593 |
| review_autofix | 54 | 12,066,395 | 445,456 | 21,219,108 | 2,347,935 | 36,078,624 | 0.595 |
| `39520815095` | 22 | 5,807,464 | 256,066 | 9,456,420 | 974,172 | 16,493,852 | 0.582 |
| `39524724854` | 22 | 3,969,312 | 131,738 | 8,531,422 | 902,738 | 13,535,210 | 0.637 |
| `39518391306` | 10 | 2,289,619 | 57,652 | 3,231,266 | 471,025 | 6,049,562 | 0.539 |
| test_and_mark_stable | 16 | 58,375 | 3,736 | 0 | 53,936 | 62,111 | 0.0 |
| memory_maintenance | 2 | 7,848 | 900 | 0 | 7,842 | 8,748 | 0.0 |

**Review telemetry**

| Metric | Value |
|---|---|
| wall_clock_p50_ms / p99_ms (repo, n=109) | 8,000 / 3,849,280 |
| wall_clock_p50_ms / p99_ms (review_autofix, n=12) | 790,500 / 2,936,010 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**Runner queue (from 28 `log_summary` rows)**

| Metric | Value |
|---|---|
| total queue / total duration | ≈5,773s / 9,449s (≈61%) |
| median / max queue | ≈213s / 617s (`39526504456`) |

**Semble**

| Workflow | query_calls | logged_bytes | fallbacks | contract_test | runtime | bootstraps | failed | unused | bootstrap_ms | sources | static_dup_bytes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| review_autofix | 5 | 82,338 | 0 | 0 | 0 | 5 | 0 | 0 | 220,847 | 33 | 0 |
| workflow_log_analysis | 0* | 0* | 0* | 0 | 0 | 6 | 0 | 6* | 222,316 | 0 | 0 |
| ci | 0 | 0 | 12 | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| test_and_mark_stable | 0 | 0 | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| implement | 0 | 0 | 2 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| **total** | 5 | 82,338 | 22 | 20 | 2 | 11 | 0 | 6 | 443,163 | 33 | 0 |

\* Undercounted: the `2>/dev/null` at `workflow-log-analysis.yml` lines 453, 1296, 1988 and 2588 discards stderr, where these lines are written.

**Serena and MCP availability**

| target | query_calls | response_bytes | tool_calls | fallbacks | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|---|---|---|---|
| (all) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Serena is disabled (`SERENA_ENABLED: false`). Other MCP servers observed: none.

**GitHub API (from log summaries only)**

| Run | Job | used_in_job | remaining |
|---|---|---|---|
| `39528828061` | review_autofix_sweep / sweep | 5 | 3702 |
| `39527963733` | review_autofix / post-merge-validate-dispatch | 2 | 3671 |
| lint runs (5) | lint | 1 paginated call each | — |
| 429 / secondary rate limit / retry events | — | 0 | — |

**Data gaps and next collection steps**

1. Mount or repair `workflow-log-output` so the `errors/`, `slow/` and `recent/` deep-dives are available.
2. Classify runs with zero duration and a 404 log as startup failures.
3. Pass the `claude_*` cost fields and the `GH_PAT_BUDGET` rollup into `analysis_context.json`.
4. Exclude skipped runs from the 1000-run cap and from the percentiles.
5. Capture `retrieve` memory telemetry.
6. Raise the success sample rate (currently 0.07, with only 1 sampled success) for the review and implement families.

---

**Actions taken:** read-only analysis. I read the relevant parts of `.github/workflows/review_autofix.yml`, `ci.yml`, `workflow-log-analysis.yml`, `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml` and `internal-cancel-on-pr-close.yml`, plus `scripts/cost_audit.py`, `scripts/semble_helpers.sh`, `scripts/build_semble_wrapper.sh`, `scripts/emit_event.sh`, `scripts/collect_workflow_logs.py` and `analysis/workflow-optimization-2026-10-20.md`. No files were changed.

**Assumptions:**
- The two zero-second failures are startup failures (inferred from 0s duration, no jobs and 404 logs).
- PR #7748 is tied to them only through the matching timestamp and the sweep's `pr=7748 (run=39520745173)`.
- The queue shares rely on the timestamps in the `log_summary` rows.

**Missing context:** the run-logs directory was not available, so no step logs could be checked directly.