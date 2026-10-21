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
## Deep Audit — Workflows & Scripts (2026-10-21)

**Scope and method.** I read these files in depth: `scripts/gh_helpers.sh` (lines 1–1763), `scripts/label_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/review_rb_judge.sh:985-1023`, `scripts/review_merge_train.sh` (API-budget header and `_mt_release`), `scripts/check_integration_pr_readiness.py`, `tests/test_workflow_file_size_limit.py`, and selected functions of `scripts/orchestrate_poll_process.sh` (about 30k lines; the label helpers, `close_merged_issues_sweep`, `evaluate_sync_superseded_by_main`, `_fetch_pr_json` and the tracking loop). I also read these workflows fully or in their relevant parts: `workflow-log-analysis.yml`, `implement.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`, `internal-review.yml` and `review_autofix.yml`.

**Not repeated here, because the report above already covers them:**
- the silent exits in `review_autofix.yml` "Checkout PR head branch" (Reliability item 1);
- the unnamed failing script in `ci.yml` "Validation self-test unit tests" (Reliability item 2);
- the missing `concurrency:` in `ci.yml` (Speed item 1a);
- `fetch-depth: 0` in `internal-cancel-on-pr-close.yml:50` (Speed item 1b);
- the `semble_query_block ... 2>/dev/null` that hides telemetry in `workflow-log-analysis.yml:453,1296,1988,2588` (Cost item 2).

**Checks that found nothing:**
- No `TODO`/`FIXME`/`HACK`/`XXX` markers exist in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.
- No `${{ secrets.* }}` value is placed directly inside a `run:` body. Every hit is a `with:` input.
- No untrusted event field (`github.event.*.title|body|head.ref`, `github.head_ref`, `inputs.pr_title|pr_body`) is placed directly inside a `run:` body. Those values go through `env:`, for example `lint-pr-body-auto-close.yml:59-61` and `integration-pr-readiness.yml:69-72`.

### Section 1: Bug & Correctness Sweep

**BUG-001: label-set retries send an empty body because stdin is piped into `gh_retry`**
- **Where:** `scripts/label_helpers.sh:251-253`, `scripts/review_rb_judge.sh:1014-1016`
- **Severity / category:** High · `bug`
- **Problem:**
  - Both sites run `printf '{"labels":%s}' "${_new}" | gh_retry gh api -X PUT "repos/.../issues/<n>/labels" --input -`.
  - `gh_retry` re-executes `"$@"` on every attempt (`gh_helpers.sh:1082-1083`). The pipe's stdin is used up by the first attempt, so attempts 2–5 send an empty request body.
  - `_gh_api_args_unsafe_post` treats an explicit `-X PUT` as safe (`gh_helpers.sh:721-724`), so a transient 5xx or network error does trigger these retries.
  - GitHub's "set labels" endpoint replaces the whole label set. An empty body therefore either fails with 4xx, or sets the issue to no labels, which would wipe phase, tracking and engine labels. [NEEDS VERIFICATION: how GitHub treats an empty PUT body.]
  - Either way, the retry does not repeat the request that failed.
- **Fix:** write the payload to a temp file and pass `--input "${file}"`, as `review_autofix.yml:3373-3377` already does:
  ```bash
  _lbl_payload="$(mktemp)"
  printf '{"labels":%s}' "${_new}" > "${_lbl_payload}"
  if gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels" --input "${_lbl_payload}" >/dev/null 2>&1
  then
  	SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="applied"
  	rm -f "${_lbl_payload}"
  	return 0
  fi
  rm -f "${_lbl_payload}"
  ```
  Apply the same change to `_resilient_phase_swap` in `review_rb_judge.sh`.

**BUG-002: the close-merged sweep fails silently when issue bodies are large (`--argjson` hits the argv limit)**
- **Where:** `scripts/orchestrate_poll_process.sh:5067-5095`
- **Severity / category:** High · `bug`
- **Problem:**
  - `merged_json` and `ready_json` each come from `gh issue list --json number,labels,body --limit 200`. They are then passed to `jq -c -n --argjson merged "${merged_json:-[]}" --argjson ready "${ready_json:-[]}"`.
  - Linux caps a single argv string at 131,072 bytes. This repo already hit that limit with `--argjson` and fixed it in `gh_helpers.sh:1451-1456` (issue #7658, "jq exit 126").
  - In this sweep the failure is hidden by `|| echo "[]"`. `count` becomes 0 and the function returns at 5104-5106, so nothing is closed and nothing is alerted. It still logs the non-zero `merged_count`/`ready_count` at 5102, which is the only trace.
  - Inference: with about 30 or more labelled issues whose bodies are over 4 KB (plan bodies usually are), the sweep stops working altogether.
- **Fix:** use the `--slurpfile` / process-substitution pattern from `gh_helpers.sh:1457-1461`:
  ```bash
  issues_json="$(jq -c -n \
  	--slurpfile merged <(printf '%s' "${merged_json:-[]}") \
  	--slurpfile ready <(printf '%s' "${ready_json:-[]}") '
  	... ($merged[0] // []) ... ($ready[0] // []) ...' 2>/dev/null)" || {
  	echo "::warning::CLOSE_MERGED_SWEEP outcome=skip reason=merge_json_failed"
  	return 0
  }
  ```
  Also log `CLOSE_MERGED_SWEEP outcome=skip` whenever `count=0` while `merged_count + ready_count > 0`.

**BUG-003: tracking-issue discovery silently stops at 20 projects**
- **Where:** `.github/workflows/orchestrate_poll.yml:280-290`
- **Severity / category:** Medium · `bug`
- **Problem:**
  - `gh issue list --label "ai:orchestrator-tracking" --state open --json number,title,labels --limit 20` produces `tracking_issues.json`.
  - Every downstream loop reads only that file: `orchestrate_poll_process.sh:22921-22942`, `5799-5801` and `20411-20414`.
  - A 21st open project is never polled, and no warning is printed.
- **Fix:** raise the limit, for example `--limit 100`. Also warn when `COUNT` equals the limit:
  ```bash
  if [ "${COUNT}" -ge 100 ]
  then
  	echo "::warning::POLL_TRACKING_LIST_TRUNCATED count=${COUNT} limit=100"
  fi
  ```

**BUG-004: Telegram cleanup deletes comments while paging, so it skips tracking comments**
- **Where:** `scripts/tg_helpers.sh:396-440` (`tg_cleanup_phase_msgs`), `scripts/tg_helpers.sh:465-511` (`tg_cleanup_msgs`)
- **Severity / category:** Medium · `bug`
- **Problem:**
  - Each loop fetches `?per_page=100&page=${page}`, deletes the matching comments on that page (430-434 and 501-505), then moves to `page+1`.
  - Deleting k comments on page N shifts the first k comments of page N+1 back onto page N. Those comments are never inspected.
  - On issues with more than 100 comments, Telegram messages and `tg_cleanup` markers leak. The 447-comment incident cited at `tg_helpers.sh:194-197` shows that busy issues do reach this size.
- **Fix:** collect all matching comment IDs across every page first, then delete them in a second pass. Alternatively, page from the last page backwards, using `_tg_newest_comments_json`'s `total` calculation.

**BUG-005: a failed no-op comment aborts the step before `ai:implementation-failed` is applied**
- **Where:** `.github/workflows/implement.yml:5534-5551`
- **Severity / category:** Medium · `bug`
- **Problem:**
  - The step runs under `set -euo pipefail` (5325). At 5534-5535 it calls `gh_retry gh api "repos/.../issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` with no `|| …`.
  - This is a POST create, so `gh_retry` makes exactly one attempt (`gh_helpers.sh:1067-1069`).
  - One transient 5xx therefore ends the step before 5540-5551 runs. That code applies `ai:implementation-failed`, the label whose contract text says the issue "will be re-issued" (`label_helpers.sh:121`).
- **Fix:** make the comment best-effort, as the sibling guards at 5414-5415 already do:
  ```bash
  gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}" >/dev/null 2>&1 \
  	|| echo "::warning::IMPLEMENT_NOOP_COMMENT outcome=failed issue=${ISSUE_NUMBER}"
  ```

**BUG-006: integration-branch creation can fail on a check-then-create race (TOCTOU)**
- **Where:** `.github/workflows/orchestrate.yml:1272-1279`
- **Severity / category:** Medium · `bug`
- **Problem:**
  - The step checks `git/ref/heads/<branch>` and, if that check fails, POSTs `git/refs`.
  - The check also "fails" when `gh_retry` gives up on transient errors, and a concurrent run may create the same ref. In both cases the POST returns 422 "Reference already exists".
  - Under `set -e`, with a single attempt, that 422 fails the whole orchestration run after the tracking issue was already created.
- **Fix:** treat 422 "Reference already exists" as success:
  ```bash
  if ! _ib_err="$(gh api "repos/${GITHUB_REPOSITORY}/git/refs" -f ref="refs/heads/${INTEGRATION_BRANCH}" -f sha="${BASE_SHA}" 2>&1 >/dev/null)"
  then
  	case "${_ib_err}" in
  		*"Reference already exists"*) echo "Integration branch already exists (race): ${INTEGRATION_BRANCH}" ;;
  		*) echo "::error::${_ib_err}"; exit 1 ;;
  	esac
  fi
  ```

**BUG-007: full-replace label PUT is a read-modify-write that can drop concurrent label additions**
- **Where:** `scripts/label_helpers.sh:223-253`, `scripts/review_rb_judge.sh:1012-1016`
- **Severity / category:** Medium · `bug`
- **Problem:**
  - The helpers GET the labels (223-224), compute `(. - $p) + [$t]` (238-239), then PUT the full set (251-253).
  - A label added by another workflow between the GET and the PUT is removed. Examples: `ai:review-blocked` from review, `ai:merged` from `issue_pr_status`, `ai:engine-claude`.
  - `review_rb_judge.sh:985-1010` already avoids this for terminal targets by using a POST add plus per-label DELETE, and then re-reading.
- **Fix:** use that additive pattern in both helpers: `POST labels[]=target`, then `DELETE` only the phase labels that are present. Keep the PUT path only behind an explicit opt-in.

**BUG-008: `gh issue close -c` is retried as if it were idempotent**
- **Where:** `scripts/gh_helpers.sh:804-816`; affected callers `orchestrate_poll_process.sh:5293-5295`, `implement.yml:5369-5370` and `5503-5504`
- **Severity / category:** Low · `bug`
- **Problem:**
  - `_gh_cmd_is_unsafe_post` lists `issue comment`, `pr comment` and similar commands, but not `issue close`.
  - `gh issue close --comment` creates a comment and then closes the issue, and `gh_retry` retries it up to 5 times. If the comment succeeds and the close fails transiently, each retry posts another comment. [NEEDS VERIFICATION: gh's internal order of comment and close.]
- **Fix:** add `"issue close"|"pr close"` to the create list at `gh_helpers.sh:813` when `-c`/`--comment` is present. Alternatively, have the callers close first and comment afterwards.

**BUG-009: deep-audit and API-redundancy validation failures leave no failure marker**
- **Where:** `.github/workflows/workflow-log-analysis.yml:2090-2118`, `.github/workflows/workflow-log-analysis.yml:2689-2716`
- **Severity / category:** Low · `bug`
- **Problem:** a missing heading or headings out of order end with `exit 1`, without calling `emit_deep_audit_failure` / `emit_api_redundancy_failure` (defined at 1890-1966 and 2490-2566). No `AI_PHASE_FAILURE_V1` marker or `ai:log-analysis-failed` label is written, unlike the empty-output and non-zero-exit paths (2077-2086, 2676-2685).
- **Fix:** call the emitter with `failure_mode=codex_output_invalid` before each `exit 1` in those blocks.

**BUG-010: `get_issue_labels_json` reads only the first 30 labels**
- **Where:** `scripts/orchestrate_poll_process.sh:4138-4141`
- **Severity / category:** Low · `bug`
- **Problem:**
  - `_safe_gh_jq "repos/.../issues/<n>/labels" --jq '[.[].name]'` uses no `--paginate`, so the default page size of 30 applies.
  - `set_issue_phase_label` only removes labels it finds in that result (4096-4103). A phase label beyond position 30 is never removed.
  - Compare `label_helpers.sh:223`, which uses `--paginate`.
- **Fix:** append `?per_page=100` to the endpoint, or read `.labels` from `repos/.../issues/<n>`, which embeds the full label array, as `get_issue_state_labels_json` (4421-4426) already does.

**SEC-001: GitHub and Telegram tokens are passed in curl argv**
- **Where:** `scripts/tg_helpers.sh:207-217, 244-248, 263-267, 398-401, 430-434, 467-470, 501-505`; `scripts/gh_helpers.sh:417-423, 509-513`
- **Severity / category:** Low · `security`
- **Problem:**
  - `-H "Authorization: token ${GH_TOKEN}"` and the URL `https://api.telegram.org/bot${TG_BOT_SECRET}/...` are curl arguments. Any same-UID process can read them through `/proc/<pid>/cmdline` while the request is in flight.
  - The AI agents are isolated with their credentials unset (for example `workflow-log-analysis.yml:1352`), so the exposure is mostly theoretical on ephemeral hosted runners. Inference: it is larger on self-hosted runners or with in-host agents.
- **Fix:** pass the headers through curl's `-H @-` / `--config -` on stdin. Alternatively, route GitHub reads through `gh api`, which reads `GH_TOKEN` from the environment.

### Section 2: GitHub API Call Redundancy Audit

**API-001: tracking-issue labels are re-fetched although `tracking_issues.json` already contains them**
- **Where:** `scripts/orchestrate_poll_process.sh:23121` (per project, per cycle); refreshes at 23156, 23517 and 23887; terminal paths at 5885, 14360, 14799, 14846, 14907, 15009, 15046
- **Severity / category:** Medium · `api-redundancy`
- **Problem:**
  - `orchestrate_poll.yml:280-290` already fetches `--json number,title,labels` for every tracking issue into `${RUNTIME_DIR}/tracking_issues.json`.
  - At the top of each project iteration, `TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"` makes another REST call for the same data.
- **Calls:**
  - Current: 1 REST call per tracking issue per cycle (up to 20) for the initial read, plus the refreshes.
  - Proposed: 0 for the initial read. Seed the value from `jq -c --argjson i "${tidx}" '[.[$i].labels[]?.name]'` on `TRACKING_ISSUES`, and keep the post-mutation refreshes at 23156, 23517 and 23887, which follow label writes.
- **Pattern to extend:** the cycle-local cache rule in CLAUDE.md §15. Fail open to `get_issue_labels_json` when the cached entry is missing.
- **[NEEDS VERIFICATION]:** whether any step between `find_tracking` and the project loop mutates tracking labels.

**BATCH-001: `close_merged_issues_sweep` makes per-issue timeline calls and per-candidate PR reads**
- **Where:** `scripts/orchestrate_poll_process.sh:5139-5302` (timeline at 5155, `_fetch_pr_json` at 5205)
- **Severity / category:** Medium · `api-batching`
- **Problem:** for each of up to 400 issues (200 `ai:merged` plus 200 `ai:ready-to-merge`) the loop runs `_issue_timeline_with_cross_refs_json`. That is 1 GraphQL call, falling back to a paginated REST timeline plus one REST call per cross-referenced PR (`gh_helpers.sh:1646-1665`). It then makes one `pulls/<n>` REST call per merged candidate.
- **Calls:**
  - Current: N + M (N issues, M merged candidates), with up to N×(1+k) on the fallback path.
  - Proposed: ceil(N/25) aliased GraphQL calls (`i<k>: issue(number:){ timelineItems(itemTypes:[CROSS_REFERENCED_EVENT], first:100){ nodes{ ... on CrossReferencedEvent { source { ... on PullRequest { number state mergedAt headRefName baseRefName body headRepository{nameWithOwner} } } } } } }`), plus an adapter to the REST shape that `_pr_json_is_issue_implementation_pr` / `_pr_json_base_is_target_merge` expect.
- **Pattern to extend:** `_fetch_candidate_issue_details_graphql` (`orchestrate_poll_process.sh:18216`, batch loop 18238-18243). Fail open per issue to the existing helper.

**BATCH-002: `evaluate_sync_superseded_by_main` runs an N+2M call chain**
- **Where:** `scripts/orchestrate_poll_process.sh:9255-9302`
- **Severity / category:** Medium · `api-batching`
- **Problem:** the function makes one timeline call per wave issue (9257), one `pulls/<n>` call per PR (9275), and one paginated `pulls/<n>/files` call per PR (9288).
- **Calls:**
  - Current: N_issues + 2×N_prs, or more when file lists paginate.
  - Proposed: ceil(N_issues/25) timeline calls plus ceil(N_prs/25) calls of `p<k>: pullRequest(number:){ state mergedAt files(first:100){ nodes{path} pageInfo{hasNextPage} } }`, falling back to REST for PRs with `hasNextPage=true`.
- **Pattern to extend:** `_fetch_linked_pr_status_graphql` (`orchestrate_poll_process.sh:18375`, batch loop 18397-18402).

**BATCH-003: the REST timeline fallback makes one PR read per cross-reference**
- **Where:** `scripts/gh_helpers.sh:1651-1665` (`_gh_issue_timeline_with_cross_refs_rest`)
- **Severity / category:** Low · `api-batching`
- **Problem:** after the paginated timeline fetch, the fallback makes one `gh_retry gh api "${pr_url}"` call per cross-referenced PR. It only runs when the GraphQL path at 1718+ fails, but it is exactly what runs inside the BATCH-001 and BATCH-002 loops when GraphQL is degraded.
- **Calls:**
  - Current: 1 + P (paginated) per issue.
  - Proposed: 1 + ceil(P/50), using one aliased `pullRequest(number:){number state mergedAt}` query per batch. Reuse the `_cpr_fetch_pr_state_batches` shape (`cancel_on_pr_close.yml:175-235`).

**API-002: merge-train `release` runs on two 5-minute schedules**
- **Where:** `.github/workflows/cancel_on_pr_close.yml:349-385` (through `internal-cancel-on-pr-close.yml:6-7`, `*/5`), `.github/workflows/orchestrate_poll.yml:218-245`
- **Severity / category:** Medium · `api-redundancy`
- **Problem:**
  - Both schedules call `scripts/review_merge_train.sh release`.
  - Per its budget header (`review_merge_train.sh:83-90`), `release` always makes at least 1 paginated open-PR list call. When any PR is queued it adds about 10 `actions/runs?status=` calls.
  - The poll step's comment (222-224) describes `cancel_on_pr_close.yml` as the *event* path. Its schedule branch (367-368), however, also runs the global scan, and its own comment (350-352) says that exists for core-profile repos, which have no poll.
- **Calls:**
  - Current: 2 or more list calls per 5 minutes, about 20 or more while PRs are queued.
  - Proposed: 1. Skip the scheduled release in `cancel_on_pr_close.yml` when the poller is active, for example `if [ "${EVENT_NAME}" = "schedule" ] && [ "${WORKFLOW_PROFILE:-}" != "core" ]; then echo "MERGE_TRAIN_RELEASE outcome=skip reason=poll_backstop"; exit 0; fi`, with `WORKFLOW_PROFILE: ${{ vars.WORKFLOW_PROFILE || '' }}` in `env:` (the same variable `implement.yml:19` reads).
- **[NEEDS VERIFICATION]:** that every non-core profile runs `orchestrate_poll.yml`.

**API-003: `label_helpers.sh` `ensure_label_exists` makes an uncached POST on every call**
- **Where:** `scripts/label_helpers.sh:161-195`, called at 220 for every `set_issue_phase_label_resilient`
- **Severity / category:** Low · `api-redundancy`
- **Problem:** every phase transition issues `gh label create`, which is a content-creation POST and counts toward secondary rate limits, even when the label already exists. `orchestrate_poll_process.sh:4009-4011, 4042, 4052` already caches results in `_ENSURED_LABELS_CACHE`.
- **Calls:**
  - Current: 1 extra write per label transition.
  - Proposed: 1 per label per process. Add `declare -gA _LABEL_HELPERS_ENSURED=()` and short-circuit on a hit.

**API-004: `curl_gh_api` retries permanent errors and has no POST guard**
- **Where:** `scripts/gh_helpers.sh:1350-1403`
- **Severity / category:** Low · `api-redundancy`
- **Problem:**
  - Every non-2xx response that is not a rate limit takes the backoff branch (1389-1395). That includes 404, 401 and 422, so `curl_gh_api` makes 5 attempts (sleeps of 1+2+4+8 = 15s).
  - `gh_retry` stops at once on these (`_is_gh_permanent_failure`, 1099-1106), and `gh_api_retry` classifies them as permanent (870-872, 877-880).
  - `curl_gh_api` also lacks the single-attempt rule for POST creates from #6634. All current callers in `tg_helpers.sh:207,214,398,467` are GETs, so the POST risk is latent.
- **Calls:**
  - Current: 5 attempts on a deterministic 4xx.
  - Proposed: 1. After the rate-limit branch, add `case "${http_code}" in 400|401|404|422) rm -f "${body_file}" "${header_file}"; return 1 ;; esac`.

**API-005: `tg_store_msg_id` makes 3 calls per tracked Telegram message**
- **Where:** `scripts/tg_helpers.sh:204-218, 227-276` (also `296-344`)
- **Severity / category:** Low · `api-redundancy`
- **Problem:** each tracked alert costs an issue GET (comment count), a last-page comments GET, and a PATCH or POST. A job that sends several tracked alerts for the same issue repeats both reads each time.
- **Calls:**
  - Current: 3 per message.
  - Proposed: 1 after the first message in a job. Cache the `tracking_id` and the current body per issue and marker in `${RUNNER_TEMP}/tg_tracking_<issue>_<marker>`, following the `gh_review_pr_state` file-cache pattern (`gh_helpers.sh:83-118`). Fail open to the current path on a cache miss.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001: the three log-analysis AI-pass run blocks are almost identical**
- **Where:** `.github/workflows/workflow-log-analysis.yml:1068-1401`, `1869-2123`, `2469-2721`
- **Severity / category:** Medium · `duplication`
- **Problem:** each block repeats, with only cosmetic differences:
  - the `_wla_label_retry` / `_wla_label_create` one-liners (1078-1079, 1877-1878, 2477-2478);
  - the `TRACKING_ISSUE` parsing;
  - an AI_PHASE_FAILURE_V1 emitter of about 75 lines (1100-1193, 1890-1966, 2490-2566);
  - the `MAX_CODEX_ATTEMPTS` / backoff validation;
  - `wla_engine_ready`;
  - the engine-or-codex retry loop with the exit-80 handling.
- **Fix:** move the shared logic into a new `scripts/wla_codex_pass.sh` (sourced by each step) that exposes:
  - `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <summary>`;
  - `wla_run_pass <phase> <role> <prompt_file> <out_file> <effort_hint> <log_file>`.

  Keep each step's `name:`, `id:`, `if:` and `env:` unchanged (§24). Register the script in `REQUIRED_BOOTSTRAP_SCRIPTS`-style staging if the workflow is called cross-repo.

**DUP-002: the Codex CLI cache/persist/restore sequence is copied five times**
- **Where:** `.github/workflows/workflow-log-analysis.yml:310-347, 887-933, 1726-1772, 2333-2379`; `.github/workflows/plan.yml:89-135`
- **Severity / category:** Low · `duplication`
- **Problem:** the same five steps (cache, install, persist to `runner.tool_cache`, restore with symlink, `codex --version`) are copied verbatim.
- **Fix:** extend the existing composite `.github/actions/install-codex/action.yml` with a `use_tool_cache` input (default `false`, so existing callers keep their behavior) that contains the `actions/cache` step plus the persist and restore logic. Replace the five blocks with a single `uses:` step pinned by SHA, as at `workflow-log-analysis.yml:319`.

**DUP-003: label creation is implemented four ways, with diverging metadata**
- **Where:**
  - `scripts/label_helpers.sh:161-195`: hardcoded catalog, returns 1 on failure, no cache;
  - `scripts/orchestrate_poll_process.sh:4003-4058`: same function name, reads `.github/ai/label_contract.v1.json`, caches, always returns 0;
  - `workflow-log-analysis.yml:1079, 1878, 2478`: `_wla_label_create`, description "Workflow log analysis failed";
  - inline `gh label create` in `implement.yml:5359-5361, 5406-5408, 5493-5495, 6374-6376, 6426-6428, 6634-6636`.
- **Severity / category:** Medium · `duplication`
- **Fix:**
  - Make `label_helpers.sh` `ensure_label_exists <label> [repo]` read the contract first and fall back to `_AI_LABEL_COLORS` / `_AI_LABEL_DESCS`, with a per-process cache (see API-003).
  - In `orchestrate_poll_process.sh`, replace the local definition by sourcing `label_helpers.sh`, keeping a one-argument wrapper so the existing call sites keep working (§10).
  - Replace `_wla_label_create` and the inline creates with `ensure_label_exists`.

**DUP-004: three implementations of the resilient phase-label swap**
- **Where:** `scripts/label_helpers.sh:203-267` (`set_issue_phase_label_resilient`), `scripts/review_rb_judge.sh:~960-1023` (`_resilient_phase_swap`), `scripts/orchestrate_poll_process.sh:4060-4131` (`set_issue_phase_label`)
- **Severity / category:** Low · `duplication`
- **Problem:** all three compute "remove phase labels, add target" from different phase sources (`_AI_PHASE_LABELS`, `_rps_phases`, `ai_labels.py resolve-phase`). Only the rb-judge copy handles the terminal-label race (985-1010).
- **Fix:** keep `set_issue_phase_label_resilient` as the single owner in `label_helpers.sh`:
  - take the phase set from `python3 scripts/ai_labels.py resolve-phase` when the contract exists;
  - fold in the rb-judge terminal handling, which also fixes BUG-007;
  - have the other two call it, keeping their names as thin aliases (§10).

**DUP-005: Telegram store and cleanup helpers are near-duplicate pairs**
- **Where:** `scripts/tg_helpers.sh:227-276` vs `296-344`; `380-441` vs `449-512`
- **Severity / category:** Low · `duplication`
- **Problem:** each pair differs only in the marker prefix and regex.
- **Fix:** add `_tg_store_marker_id <issue> <marker_prefix> <msg_id>` and `_tg_cleanup_marker_comments <issue> <jq_select> <sed_extract>`, and keep the four public functions as one-line wrappers. Fix BUG-004 once, inside the shared cleanup helper.

**DUP-006: the workflow-support resolution block is repeated 18 times**
- **Where:** the "Resolve workflow support ref" steps followed by checkout, fallback checkout, ensure and main-snapshot steps. Example: `.github/workflows/cancel_on_pr_close.yml:25-94`. There are 18 "Resolve workflow support ref" steps across 11 workflows (implement 4, clarify 2, plan 2, orchestrate 2, orchestrate_poll 2, and others).
- **Severity / category:** Low · `duplication`
- **Fix:** add a composite action, for example `.github/actions/stage-workflow-support`, that takes `workflow_file` and `token` inputs, has a `script_ref` output, and contains the SHA-pin regex and pin verification from `cancel_on_pr_close.yml:41-43, 80-83`. Consumers must pin it by SHA, as they already pin `install-codex`. This is a larger change and should be staged one workflow at a time.

### Section 4: Expression Size Limit Risk Assessment

Context: `tests/test_workflow_file_size_limit.py:66-91` already fails CI for any `steps[].run` body containing `${{` at 18,000 characters or more. So, assuming CI is green, no block is currently in the High band. The character counts below are estimates from line counts and visual line lengths; I could not run `wc` or `yq`. [NEEDS VERIFICATION with the test's own `len(run)`.]

**EXPR-001: "Run workflow log analysis" carries six trivial expressions in a ~333-line body**
- **Where:** `.github/workflows/workflow-log-analysis.yml:1068-1401`
- **Severity / category:** Medium · `expression-limit`
- **Size:** about 16,000–18,000 characters (333 lines; several lines are over 150 characters, for example 1255, 1260 and 1334). Headroom to the 18,000 guard is roughly 0–2,000 characters; to 21,000, roughly 3,000–5,000.
- **Problem:** the only interpolations are `github.run_id`, `run_attempt`, `workflow`, `server_url` and `repository`, at 1113-1115, 1128, 1134-1136 and 1173-1175.
- **Fix:** use the runner's default environment variables (`${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}`, `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}`). The body then contains no `${{` and is exempt from the limit, and behavior is unchanged. DUP-001's extraction would shrink it further.

**EXPR-002: the deep-audit and API-redundancy passes use the same trivial interpolations**
- **Where:** `.github/workflows/workflow-log-analysis.yml:1869-2123` and `2469-2721`
- **Severity / category:** Low · `expression-limit`
- **Size:** about 12,000–14,000 characters each (about 254 lines). Headroom to the 18,000 guard is roughly 4,000–6,000 characters.
- **Fix:** same as EXPR-001. The interpolations to replace are at 1901-1903, 1915, 1921-1923 and 1954-1956, and at 2501-2503, 2515, 2521-2523 and 2554-2556.

**EXPR-003: "Handle no-op implementation" interpolates into a body full of long prose lines**
- **Where:** `.github/workflows/implement.yml:5324-5551`
- **Severity / category:** Medium · `expression-limit`
- **Size:** about 15,000–17,000 characters (227 lines, with several prose lines of 240–330 characters at 5354, 5356, 5389, 5394, 5401, 5488 and 5490). Headroom to the 18,000 guard is roughly 1,000–3,000 characters. The Guard 0/1/2 blocks have grown with each incident (#1292), so this is likely to grow further.
- **Problem:** interpolations at 5329, 5331, 5534, 5542, 5544, 5548 and 5550.
- **Fix:** move the body verbatim to a new `scripts/implement_handle_noop.sh`, using the same pattern as the neighbouring `implement_handle_guard_block.sh` call at 5304. Pass `REPO` and `RUN_URL` through `env:` (`${{ github.repository }}`, `${{ github.server_url }}/...`). Keep the step's `name:`, `if:` and `env:` in the workflow.

**EXPR-004: the guard test does not cover composite actions or `if:` expressions**
- **Where:** `tests/test_workflow_file_size_limit.py:66-91`
- **Severity / category:** Low · `expression-limit`
- **Problem:**
  - The test scans only `.github/workflows/*.yml`, under `jobs.*.steps[].run`.
  - Composite `run:` bodies in `.github/actions/*/action.yml` are evaluated the same way and are not checked. All six are small today; `setup-runtime/action.yml:60-146` is the largest.
  - `if:` expressions are not measured. The longest I saw are about 330 characters (`implement.yml:5317`) and about 280 characters (`review_autofix.yml:3334`), far below the limit.
- **Fix:** extend the test to glob `.github/actions/*/action.yml` and read `runs.steps[].run`.

**Workflow file size.** By line count, no workflow comes near the 800 KB or 1 MB figures in this prompt. The largest are `test-and-mark-stable.yml` (7,710 lines), `implement.yml` (6,961) and `review_autofix.yml` (5,728). However, the binding limit in this repo is 512,000 bytes (§24, `tests/test_workflow_file_size_limit.py:26-27`, guard 480,000). The 1 MB figure in this prompt is out of date (see CONSIST-004). At about 60–70 bytes per line, `implement.yml` and `test-and-mark-stable.yml` could sit within about 30–60 KB of the 480,000-byte guard. [NEEDS VERIFICATION: `wc -c .github/workflows/{implement,test-and-mark-stable,review_autofix}.yml`.] EXPR-003's extraction also gives `implement.yml` byte headroom.

### Section 5: Cross-Cutting Concerns

**DEAD-001: `gh_rate_limit_breaker_tripped` has no callers**
- **Where:** `scripts/gh_helpers.sh:336-341`
- **Severity / category:** Low · `dead-code`
- **Problem:** a repo-wide search (scripts, workflows, tests) finds only its definition and comments. The comment at 315-318 records that its only consumer (`orchestrate_poll.yml`'s self-retrigger) was removed. `gh_rate_limit_breaker_active` (894-902) is the live API.
- **Fix:** keep it, since removing it could break consumer repos (§10), but mark it deprecated in the header comment. Optionally emit `GH_RATE_LIMIT_BREAKER_LEGACY_CALL` to stderr when it is called, so any remaining consumer callers become visible before removal.

**DEBT-001: a stale comment says two sequential jobs run in parallel**
- **Where:** `.github/workflows/workflow-log-analysis.yml:2150-2154`
- **Severity / category:** Low · `tech-debt`
- **Problem:** the comment says "The deep-audit and api-redundancy jobs both append a section to the same REPORT_FILE in parallel". But `api-redundancy` declares `needs: [analyze-commit-notify, deep-audit]` (2243), and the workflow's concurrency group uses `cancel-in-progress: false` (70-72). The retry/`git am` logic is still useful against external pushes, but the stated reason is wrong.
- **Fix:** correct the comment to name the actual race: concurrent pushes to the target branch by other workflows.

**CONSIST-001: label colors and descriptions differ from the label contract**
- **Where:** `.github/workflows/implement.yml:5359-5360, 5493-5494, 5406-5407`; `workflow-log-analysis.yml:1186, 1964, 2564`
- **Severity / category:** Low · `consistency`
- **Problem:** whichever code path runs first decides the label's metadata:

  | Label | Inline creation | `.github/ai/label_contract.v1.json` |
  |---|---|---|
  | `ai:closed` | color `6b7280`, "Issue closed by automation (superseded, duplicated, or already complete)" | `6a737d`, "Linked PR closed without merge" (156-159) |
  | `ai:needs-human` | color `b60205` | `e11d48` (100-103) |
  | `ai:log-analysis-failed` | description "Workflow log analysis failed" | "…before producing an analysis report" (84-87) |

- **Fix:** route every creation through the contract-backed `ensure_label_exists` (DUP-003).

**CONSIST-002: required checks and some failure paths call `gh` without retry**
- **Where:** `.github/workflows/lint-pr-body-auto-close.yml:85-86`; `scripts/check_integration_pr_readiness.py:69-84, 107-133`; `workflow-log-analysis.yml:1164-1167, 1948, 2548`
- **Severity / category:** Low · `consistency`
- **Problem:**
  - The lint workflow's commit-message read is a plain `gh api ... --paginate`, which fails a required check on one transient 5xx.
  - The readiness script's `gh issue view` and the status `POST` use `subprocess.run(["gh", ...])` once. A transient failure posts `error` or leaves the required context missing (196-201) until the next PR event.
  - The workflow-log-analysis failure comments use raw `gh issue comment`, while the label write right after them uses `_wla_label_retry`.
- **Fix:**
  - Lint workflow: source `scripts/gh_helpers.sh` and wrap the call in `gh_retry`.
  - Readiness script: use the Python twin `scripts/gh_api_retry.py` (referenced at `gh_helpers.sh:581-582`) for GETs, and an idempotent retry for the status POST, since a status POST is a safe replay.
  - Failure comments: wrap them in `gh_retry`, which will make one attempt because they are POSTs.

**CONSIST-003: fallback stubs silently hide `gh_helpers.sh` load failures**
- **Where:** 94 sites across 44 files (for example `cancel_on_pr_close.yml:126-127`, `orchestrate.yml:1250-1265`, `implement.yml:5326-5327`); `scripts/tg_helpers.sh:22-29`
- **Severity / category:** Low · `consistency`
- **Problem:**
  - The idiom `source scripts/gh_helpers.sh 2>/dev/null || true; type gh_retry ... || gh_retry() { "$@"; }` turns a missing or syntactically broken helper into "no retry, no rate-limit breaker, no Telegram alert" without logging anything.
  - `orchestrate.yml:1252-1265` and one site in `plan.yml` also carry a verbatim inline copy of `_safe_gh_jq` (`gh_helpers.sh:1241-1255`).
  - `tg_helpers.sh` sources a path relative to the working directory (`scripts/gh_helpers.sh`), while `label_helpers.sh:13-17` resolves its path from `BASH_SOURCE`.
- **Fix:**
  - Make each stub announce itself: `type gh_retry >/dev/null 2>&1 || { echo "::warning::GH_HELPERS_FALLBACK reason=not_loaded"; gh_retry() { "$@"; }; }`. This is the same one-line change at every site.
  - Switch `tg_helpers.sh` to `BASH_SOURCE`-relative sourcing.

**CONSIST-004: this prompt's 1 MB workflow-size figure conflicts with §24**
- **Where:** `.github/workflows/workflow-log-analysis.yml:1999` (renders `prompts/mode-workflow-audit.txt`)
- **Severity / category:** Low · `consistency`
- **Problem:** the Section 4 instructions in this prompt say the per-file limit is 1 MB (1,048,576 characters) and ask to flag files over 800 KB. The repo's own §24 and `tests/test_workflow_file_size_limit.py:4-12, 26-27` establish 512,000 bytes as the real limit (incident #4327).
- **Fix:** update the text in `prompts/mode-workflow-audit.txt` to "512,000 bytes; flag ≥ 480,000". [NEEDS VERIFICATION: the exact line in that prompt file, which I did not read.]

**SHELL-001: unquoted word-splitting and unescaped sed interpolation (SC2086)**
- **Where:**
  - `scripts/tg_helpers.sh:424, 494` (`for tg_id in ${id_list}` under a changed IFS);
  - `scripts/orchestrate_poll_process.sh:5203` (`for _sweep_candidate_pr in ${merged_pr_candidates}`);
  - `scripts/tg_helpers.sh:262, 331` (`sed "s/...${msg_id}.../"` and `${phase}` placed in a sed regex);
  - `scripts/gh_helpers.sh:1129-1131` (`$*` in a log line, which is fine).
- **Severity / category:** Low · `shellcheck`
- **Problem:** the values are numeric or constant today (for example phases are only `clarify`/`plan` at `plan.yml:1210, 2477, 2893` and `clarify.yml:2089`), so nothing breaks now. Any future phase or marker containing `/`, `*` or whitespace would break the sed expression or trigger glob expansion.
- **Fix:**
  - Read the values into arrays: `IFS=',' read -r -a _tg_ids <<< "${id_list}"; for tg_id in "${_tg_ids[@]}"`, and `mapfile -t` for the candidate list.
  - Validate `phase` against `^[a-z_]+$` before building the sed expressions.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BUG-002 |
| Medium | 13 | BUG-003, BUG-004, BUG-005, BUG-006, BUG-007, API-001, API-002, BATCH-001, BATCH-002, EXPR-001, EXPR-003, DUP-001, DUP-003 |
| Low | 21 | BUG-008, BUG-009, BUG-010, SEC-001, API-003, API-004, API-005, BATCH-003, EXPR-002, EXPR-004, DUP-002, DUP-004, DUP-005, DUP-006, DEAD-001, DEBT-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh` | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/tg_helpers.sh`, `.github/workflows/cancel_on_pr_close.yml` | Medium |
| Code modularization | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/actions/install-codex/action.yml`, new `scripts/wla_codex_pass.sh`, `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh` | Large |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml`, new `scripts/implement_handle_noop.sh`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/orchestrate.yml`, `.github/workflows/implement.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `.github/workflows/workflow-log-analysis.yml`, `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh`, `scripts/check_integration_pr_readiness.py`, `prompts/mode-workflow-audit.txt` | Medium |