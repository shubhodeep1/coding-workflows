## Executive Summary

**Data notes.** The window covers 1,000 runs in `shubhodeep1/coding-workflows` on 2026-10-15/16. The mounted logs directory (`/home/runner/work/_temp/workflow-log-output`) did not exist in this sandbox. Evidence therefore comes from `analysis_context.json` (run rows, `cost_telemetry`, and the `log_summary` fields) plus reading the repo source. `insufficient_data=false`.

- **Review serialization is the biggest latency driver (high confidence; about 12–38 min of queueing per review run).**
  - The `review / codex-agent` job is in concurrency group `pr-autofix-<PR>` with `cancel-in-progress: false` (`.github/workflows/review_autofix.yml:1801-1802`).
  - On PR #7452, all 8 codex-agent jobs ran strictly one after another from 21:50:54 to 00:31:04, about 2h40m. Each job started within 3–8 s of the previous one ending (chain table in the Appendix).
  - Every push produced two review runs: "Internal: AI Review & Autofix" and "…[pr:7452]". The plain run always ran after the `[pr:7452]` run had already pushed autofix edits (`AUTOFIX_EDITS_PUSHED: true`). That suggests it ran on a superseded head (inference).
- **The orchestrator poller runs back-to-back at 100% duty cycle (high confidence; up to about 20 min reaction latency).**
  - Group `ai-orchestrate-poll-<repo>` (`orchestrate_poll.yml:72-73`) is fed by `cron: '*/5 * * * *'` (`internal-orchestrate-poll.yml:5`). Each tick takes 7.5–12 min.
  - All 12 timed ticks started 2–8 s after the previous tick ended. For example, 38819784128 ended 23:06:50 and 38820670162 started 23:06:56.
  - The "runner wait" of 1.5–8 min in the summaries is really queueing in the concurrency group.
- **All 7 CI failures were in one job, `tests-promote-stall-and-review` (high confidence; CI failure rate 21.9%).**
  - 4 failed in "Regression tests wired by the CI wiring guard (part 2)", 2 in "Review autofix step-script contract and workflow file size guard tests", and 1 in "Clarify loop guard unit tests".
  - The failures surfaced 760–1,208 s into each run. Triage on 38817127219 reported `reason=pr_specific_failure base_conclusion=success`, meaning these are real PR regressions, not flakes.
  - Moving the cheap contract checks earlier would surface them about 10 min sooner (medium confidence).
- **Tokens concentrate in PR #7452's autofix loop (high confidence).**
  - The window logged 57.84M OpenRouter tokens over 71 calls, from only 6 `[pr:N]` review runs.
  - PR #7452's four `[pr:7452]` iterations used 42.74M (73.9%). Every iteration logged `REVIEW_THREADS_RESOLVED_COUNT: 0`.
  - The cache hit rate over the window works out to 73.7%. Per run it ranges from 0.566 (38824790605: 20 calls, 978,606 cache-write tokens) to 0.863 (38812672324).
- **The two review failures have no diagnosable cause (medium confidence; about 2 h of wasted wall clock).**
  - 38735635119 (4,818 s) and 38740625623 (2,415 s) both failed in `review / codex-agent` → "Checkout PR head branch".
  - That step has 6 distinct `exit 1` paths (`review_autofix.yml:2809-2892`), and only some of them print a structured reason. A `REVIEW_CHECKOUT_FAIL reason=` line is the cheapest fix.
- **The poller failed in "Install Codex CLI" (38804344938, 472 s); the cause is not visible.** The install action runs a single `npm install -g` with no retry (`.github/actions/install-codex/action.yml:33-36`). Adding a bounded retry is low risk (medium confidence).
- **Observability gaps hide memory, poll, and API hot-spots (high confidence).**
  - Codex-agent logs were truncated by about 1.1–1.6M chars and poll logs by about 820k chars.
  - No `AI_MEMORY_TELEMETRY` line appears in any summary, and `scripts/cost_audit.py` does not parse that prefix (only `summarize_unselected_runs.py` and `ai_memory.py` mention it).
  - Sweep runs are miscounted as `review_autofix` (see GH API Call Audit and Reliability item 5).

## Speed Optimizations

Ranked by expected latency reduction.

1. **Stop codex-agent runs that queue behind a peer on a superseded head (critical path).**
   - **Evidence:** in the PR #7452 chain, each plain run's codex-agent waited for the `[pr:7452]` run's codex-agent:

     | Waiting run | Waited for | Wait |
     |---|---|---|
     | 38812641332 | 38812672324 | about 38 min (gate 21:50:52, start 22:28:46) |
     | 38817127287 | 38817157151 | about 21 min |
     | 38820915647 | 38820943553 | about 25 min |
     | 38824769279 | 38824790605 | — |

     Each waiting run then took another 12–13 min of codex-agent time, which delayed the next push's `[pr]` run by 12–13 min:

     | Delayed run | Delay |
     |---|---|
     | 38817157151 | about 12 min |
     | 38820943553 | about 12 min |
     | 38824790605 | about 13 min |

   - **Root cause:** `cancel-in-progress: false` on `pr-autofix-${PR}` is intentional (comment at `review_autofix.yml:1783-1789`). It does not cover a queued run whose head SHA a peer's autofix push has already superseded. The "Checkout PR head branch" stale check (`review_autofix.yml:2795-2806`) did not cut these runs short: they still took about 12 min and used 168–463 GH API calls. This is an inference, since the logs are truncated.
   - **Exact change (two steps):**
     - (a) Add the `REVIEW_HEAD_FRESHNESS` diagnostic line (Reliability item 2) at codex-agent start. It should log `run_head`, `live_head` (from one `gh api repos/{repo}/pulls/{n} --jq .head.sha`, reusing the PR fetch this job already makes) and `superseded=true|false`.
     - (b) Once one week of data confirms the pattern, skip with `AUTOFIX_STALE_BASE_SKIP=true` when `superseded=true` **and** the superseding commit came from this pipeline's own autofix push.
   - **Estimated savings:** about 12 min of codex-agent runtime per autofix push, plus about 12 min less queueing for the next `[pr]` run. On PR #7452 that is about 50 min of the 2h40m chain.
   - **Risk:** medium. A wrong skip could leave the latest head unreviewed, so step (a) is logging-only.

2. **Cut poll-tick runtime, the orchestrator's critical path.**
   - **Evidence:** poll steps ran 7.5–12 min. Examples: 38816508755 about 12 min, 38819784128 11m39s, 38812097704 about 11 min. Ticks are serialized, so p95 duration was 990.7 s.
   - **Root cause:** unknown. About 820k chars of every poll log are truncated, so no phase is visible.
   - **Exact change:** emit one `POLL_PHASE_TIMING phase=<name> ms=<n>` line per major poll phase. Also emit one `POLL_TICK_SUMMARY total_ms=<n> top_phase=<name>` line in an `always()` step at the end of the job, so it survives head/middle truncation. This needs a new `agents.d/` log-prefix fragment (§25).
   - **Estimated savings:** none directly; it enables targeted cuts. Each minute saved per tick shortens orchestrator reaction time by about 1–2 min.
   - **Risk:** low.

3. **Move cheap CI contract checks earlier (fail-fast).**
   - **Evidence:** 38807437140 and 38807464295 failed "Review autofix step-script contract and workflow file size guard tests" at 772 s and 760 s. That step runs only `tests/test_review_autofix_step_scripts_contract.py` and `tests/test_workflow_file_size_limit.py` (`ci.yml:1229-1233`).
   - **Exact change:** run the same two commands in `static-checks` as well, keeping the existing step (§9: extend, not replace).
   - **Estimated savings:** failure feedback about 10 min sooner on those regressions. Inference: `static-checks` finishes earlier than `tests-promote-stall-and-review`; the logs do not show its duration.
   - **Risk:** low (duplicated, fast checks).

4. **Cap the CI `lint` aggregator tail (micro).**
   - **Evidence:** in 38812641028, `lint` started at 22:02:17, about 14 min after the other jobs queued at 21:48:32. In 38820915620, `orchestrate-poll (1)` was the long pole: its slowest test `test_standalone_pr_dependency_holds_until_pr_merges_and_string_lands` took 34,894 ms, and the top 10 tests took 13.7–34.9 s each.
   - **Exact change:** rebalance the 4-way `orchestrate-poll` matrix by recorded `TEST_CASE_EVENT` elapsed times.
   - **Estimated savings:** about 1–3 min per CI run (inference).
   - **Risk:** low.

5. **Semble bootstrap cost per review run (micro).**
   - **Evidence:** 9 bootstraps in `review_autofix` took 346,313 ms in total, about 38.5 s each. The two-query run 38824790605 took 87,278 ms. Against a p50 review duration of 777 s this is about 5%; against runs over 2,000 s it is under 2%.
   - **Exact change:** none for now. 0 bootstraps failed and 0 were unused, so the lazy mode is working. Revisit only if the queueing in item 1 is fixed and Semble becomes a larger share of time.

## Cost Optimizations

Ranked by expected token savings.

1. **Autofix loop convergence on PR #7452.**
   - **Evidence:** four `[pr:7452]` iterations used 42,736,810 tokens:

     | Run | Tokens | Calls | Cache hit |
     |---|---|---|---|
     | 38812672324 | 20.08M | 10 | 0.863 |
     | 38817157151 | 5.76M | 10 | 0.670 |
     | 38820943553 | 6.33M | 10 | 0.711 |
     | 38824790605 | 10.58M | 20 | 0.566 |

     Each logged `AUTOFIX_EDITS_PUSHED: true` and `REVIEW_THREADS_RESOLVED_COUNT: 0`, and each autofix push started a new CI run plus a new review pair.
   - **Root cause:** not determinable from the logs. The loop may have converged usefully: CI on b6f093e failed (38817127219) and the next head passed (38820915620). But there is no per-iteration findings count to tell whether later iterations were productive.
   - **Exact change (diagnostic):** emit `AUTOFIX_ITERATION pr=<n> iter=<k> findings_in=<n> findings_applied=<n> files_changed=<n>` at the end of the editor phase. Then the existing `fingerprint_cap` gate (`review_autofix.yml:1454-1457`) can be tuned from data.
   - **Estimated savings:** if one of four iterations per PR proves unproductive, about 6–10M tokens per such PR (inference).
   - **Quality risk:** none while logging-only.

2. **Cache fragmentation in multi-call runs.**
   - **Evidence:** 38824790605 has the window's worst cache hit (0.566). It made 20 calls (twice the norm), had 2 Semble bootstraps, wrote 978,606 cache tokens and sent 3,565,362 uncached prompt tokens. Uncached prompt is 11.5M of 57.84M tokens (19.9%) across the window.
   - **Root cause (inference):** a second review pass in the same job rebuilt a prefix that differs from the first pass, possibly because the per-pass Semble block changed (30,196 bytes over 2 queries).
   - **Exact change (diagnostic):** log `OR_PROMPT_PREFIX call=<k> prefix_sha=<sha256 of first stable segment> prefix_bytes=<n> dynamic_bytes=<n>` per OpenRouter call, so the collector can detect a prefix change between calls.
   - **Estimated savings:** bringing that run to the 0.737 window mean would avoid about 1.2M uncached tokens. That is a rough estimate.
   - **Risk:** low.

3. **A possible duplicate review run per push (unmeasured cost).**
   - **Evidence:** the plain "Internal: AI Review & Autofix" runs (38812641332, 38817127287, 38820915647, 38824769279) each ran codex-agent for about 12 min and logged `CLAUDE_POOL cleanup removed=true`. Their `or_*` counters are all 0.
   - **Root cause:** the `claude_*` counters (`CLAUDE_COUNT_KEYS`, `claude_cost_usd` in `scripts/cost_audit.py:1043-1044`) are computed but not exported into `analysis_context.json`. Claude spend in these runs is invisible.
   - **Exact change:** include `claude_calls`, `claude_input_tokens`, `claude_cache_read_tokens` and `claude_cost_usd` in the per-run `cost_telemetry` the analysis context is built from.
   - **Estimated savings:** unknown until measured; this could be the largest hidden cost in the window.
   - **Risk:** none.

4. **Semble.**
   - 10 queries logged 120,494 bytes (about 12 KB each) from 61 sources, with 0 static-duplicate bytes, 0 echo lines dropped and 0 runtime fallbacks.
   - At about 4k tokens per query against about 9.6M tokens per review run, Semble is under 0.1% of prompt volume. It is neither noisy nor a measurable expansion reducer, because `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED: false` per the 38824790605 summary.
   - No change is recommended.

5. **Serena:** `SERENA_ENABLED: false`. There were 0 queries, 0 probes and 0 response bytes, so there is nothing to evaluate.

## Reliability Improvements

Ranked by expected reduction in failures or reruns.

1. **CI `tests-promote-stall-and-review` concentrates failures (7 of 7 CI failures).**
   - **Evidence:**

     | Step | Runs |
     |---|---|
     | part 2 regression tests | 38754255091, 38745552143, 38745311615, 38747027233 |
     | step-script contract / size guard | 38807437140, 38807464295 |
     | Clarify loop guard | 38817127219 |

   - **Root cause category:** genuine PR regressions (triage `pr_specific_failure` on 38817127219), not infrastructure. The step-script/size-guard pair started 12 s apart (21:07:30 and 21:07:42), which suggests the same change in two contexts (inference).
   - **Exact fix:** Speed item 3 (fail-fast placement). Also, since each Python script in "Clarify loop guard unit tests" (`ci.yml:1383-1395`) runs without pytest, prefix each invocation with `echo "::group::<script>"` so the failing script is named in annotations.
   - **Expected impact:** no change in failure rate, but about 10 min faster failure detection and lower triage cost.
   - **Rollback:** delete the added lines.

2. **"Checkout PR head branch" failures have no structured reason.**
   - **Evidence:** 38735635119 (4,818 s) and 38740625623 (2,415 s), both at `review / codex-agent` → "Checkout PR head branch". The long durations mean the failure came after a long concurrency or runner wait.
   - **Root cause category:** unknown. The candidate exit paths in `review_autofix.yml` are:
     - `Review workspace is unavailable` (2810)
     - OpenCode config present (2815)
     - `Unable to determine PR head branch` (2821, plain echo with no annotation)
     - `git_ref_health_check.sh repair` (2842)
     - `git fetch … failed after 4 attempts` (2864)
     - `git checkout -f -B` (2892, unguarded)

     A plausible inference: the head branch was deleted after merge while the job sat queued.
   - **Exact fix (diagnostic):** emit one structured line per failure path, plus the freshness line from Speed item 1. Register `REVIEW_CHECKOUT_FAIL` and `REVIEW_HEAD_FRESHNESS` via an `agents.d/` fragment.

     ```bash
     review_checkout_fail() { echo "REVIEW_CHECKOUT_FAIL pr=${PR_NUMBER:-} reason=$1 head_ref=${HEAD_REF:-}"; echo "::error::$2"; exit 1; }
     # e.g. at the fetch give-up:
     if ! git ls-remote --exit-code origin "refs/heads/${HEAD_REF}" >/dev/null 2>&1; then
       review_checkout_fail head_ref_missing "PR head branch ${HEAD_REF} no longer exists on origin"
     fi
     review_checkout_fail fetch_exhausted "git fetch ... failed after ${fetch_max_attempts} attempts"
     ```

   - **Expected impact:** makes the remaining 3% `review_autofix` failure rate diagnosable. A later step can turn `head_ref_missing` into a clean skip.
   - **Fail-open note:** keep `exit 1` until the data justifies a skip.

3. **Poller "Install Codex CLI" has no retry.**
   - **Evidence:** 38804344938 failed at `poll / poll` → "Install Codex CLI" after 472 s. The install is a single `npm install -g` (`.github/actions/install-codex/action.yml:33-36`), and the poller pins that action at `@stable` (`orchestrate_poll.yml:408`).
   - **Root cause category (inference):** transient registry or network error. No log excerpt was available to confirm.
   - **Exact fix:** wrap the install in 3 attempts with 2/4/8 s backoff and log `CODEX_INSTALL_RETRY attempt=<k> exit=<rc>`. Keep `codex --version` as the final gate. It reaches consumers via the next `@stable` retag.
   - **Expected impact:** removes the poller's only failure this window (1 of 64 runs, 1.6%). A failed tick delays orchestration by one full tick (about 10 min).
   - **Rollback:** revert the action.

4. **Post-job annotation noise on successful runs.**
   - **Evidence:**
     - 38818592040 (check failure triage, success) logged `##[error]Can't find 'action.yml'… under '.../.codex-workflow-src/.github/actions/install-claude'` in post-job.
     - Every codex-agent run logs `##[warning]The process '/usr/bin/git' failed with exit code 1` from `git submodule foreach` ("cannot be used without a working tree"). Examples: 38812672324, 38820943553, 38817157151, 38824790605.
   - **Root cause category:** cleanup ordering. The local action source directory is removed before its post step runs, and the checkout's post step runs against a split `GIT_WORK_TREE`.
   - **Exact fix:** keep `.codex-workflow-src` until job end in the triage workflow. The git warning is cosmetic and low priority.
   - **Impact:** fewer false `##[error]` annotations that heal-intake or triage could treat as failures (inference).

5. **The collector misclassifies sweep runs.**
   - **Evidence:** "Internal: AI Review Autofix Sweep" runs (38827029554, 38823991194, 38820867426, 38817797651, 38814088987, 38810769180; 20–26 s each) are counted in `review_autofix`. The marker `("review_autofix", "review_autofix")` in `normalize_workflow_family` (`scripts/collect_workflow_logs.py:576`) matches before any sweep marker.
   - **Exact fix:** add `("review_autofix_sweep", "review_autofix_sweep")` above line 576. This matches the existing path map at line 102.
   - **Impact:** corrects `review_autofix` p50 and p95 and failure rates. Today the sweep's roughly 22 s runs pull down the p50 of 777 s and the 1,386 s average.

6. **BREAK_GLASS / CONTEXT_BUDGET_WARN / Semble / Serena.**
   - `break_glass_count=0` everywhere (`REVIEW_BREAK_GLASS_ENABLED: false`).
   - `context_budget_warn_count=1`, in 38770060131 (`[pr:7447]`). That run also had the window's only `or_usage_unavailable_calls=1` and was the largest call count after 38824790605 (13 calls, 10.71M tokens). This is isolated prompt-size pressure, not a trend.
   - The 52 `SEMBLE_FALLBACK` lines are all `context=contract-test` (`_is_contract_test_semble_line`, `cost_audit.py:334-335`). They come from CI only, at 4 per run in 9 runs and 8 per run in 2 runs (38828517297, 38824768988). There were 0 runtime fallbacks, so this is test fixture output, not a masked broken rollout. No Serena probes ran.

## AI Memory Health

- **No `AI_MEMORY_TELEMETRY:` lines appear** in any deep-dive excerpt or `log_summary`. The retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` rates, and push retry counts **cannot be computed** for this window.
- **Root cause of the gap:**
  - The lines are probably in the truncated middle of the codex-agent logs (about 1.1–1.6M chars each) and poll logs (about 820k chars). That is an inference.
  - `scripts/cost_audit.py`, which does parse full logs (that is where the Semble counts come from), has no `AI_MEMORY_TELEMETRY` parser. Only `scripts/summarize_unselected_runs.py` and `scripts/ai_memory.py` reference the prefix.
- **Recommendation:** add per-run counters to `cost_audit.py`'s log parse, next to the Semble block (around lines 772-780), and export them to the analysis context:
  - `memory_retrieve_calls`
  - `memory_retrieve_hits` (`records_selected>0`)
  - `memory_retrieve_tokens_total`
  - `memory_keyword_method_{llm,plain,none}`
  - `memory_fail_open_count`
  - `memory_disabled_count`
  - `memory_push_retries_total`
- **Verification step:** the CI step "Clarify loop guard unit tests" already runs `tests/test_ai_memory_push_retry_backoff.py` and `tests/test_ai_memory_processed_command_entry.py` (`ci.yml:1388-1389`). Emission exists in code, but has not been confirmed in production logs.

## GH API Call Audit

| Run | Job | `used_in_job` | `remaining` | reset window |
|---|---|---|---|---|
| 38812672324 `[pr:7452]` | codex-agent | **1190** | 3692 | 1792104305 |
| 38820943553 `[pr:7452]` | codex-agent | **993** | **3000** | 1792107906 |
| 38824790605 `[pr:7452]` | codex-agent | 568 | 4064 | 1792111507 |
| 38812641332 (plain) | codex-agent | 463 | 3220 | 1792104305 |
| 38817127287 (plain) | codex-agent | 310 | 4023 | 1792107906 |
| 38824769279 (plain) | codex-agent | 168 | 3896 | 1792111507 |
| 38820915647 (plain) | codex-agent | unknown | 4639 | 1792111507 |
| 38817157151 `[pr:7452]` | codex-agent | unknown | 4345 | 1792107906 |
| 38827029554 (sweep) | sweep | 2 | 4285 | 1792111507 |

- **Hot-spot:** `[pr:N]` codex-agent jobs average about 917 calls per run (3 known values). In window 1792107906, remaining fell from 4,345 to 3,000 across three PR #7452 review jobs, and the PAT is shared with the poller.
  - **Recommendation (diagnostic):** emit `GH_PAT_BUDGET phase=<step-slug>` checkpoints at codex-agent phase boundaries (reviewer, consolidator, editor, push, comment). This uses the existing helper (`scripts/gh_helpers.sh:74`), so it adds no API calls. It will show which phase spends the roughly 1,000 calls.
  - Then batch per-thread or per-comment lookups with GraphQL aliases, per §14 `BATCH_HELPER` patterns. The size of the reduction is unknown until the phase split is visible.
- **`used_in_job=unknown` is window rollover:** `scripts/gh_helpers.sh:68-72` computes usage only when the start and end `reset` values match. Both unknown rows ended in a new window. 38820915647 ended at reset 1792111507, and its run started in window 1792107906 (inference from timestamps).
  - **Exact change:** in the rollover branch, log `window_rolled=true new_window_used_lower_bound=$((limit - remaining))`, reading the limit from the same rate-limit response. This keeps the existing line format and appends fields.
- **Poller:** no `GH_PAT_BUDGET` line was visible in any poll summary because of truncation. The §14 caches (`_candidate_details_json`, `STALL_MANAGED_LINKED_PR_CACHE`) cannot be audited this window. The tail `POLL_TICK_SUMMARY` from Speed item 2 should include `gh_used=`.
- **No rate-limit pressure:** no HTTP 429 or secondary rate limit appeared in any visible portion. `lint_pr_body_auto_close` uses one paginated commits call per run (38828517490).

## Prompt Cache & Memory System

- **cache_hit_rate:** the window aggregate is not reported (`null`). Derived as `cache_read / (cache_read + prompt + cache_write)`, it is **0.737**. Per run:

  | Run | Cache hit | Calls | Uncached prompt tokens |
  |---|---|---|---|
  | 38812672324 | 0.863 | 10 | — |
  | 38735664522 | 0.769 | — | — |
  | 38820943553 | 0.711 | — | — |
  | 38770060131 | 0.708 (derived; 1 call missing usage) | — | — |
  | 38817157151 | 0.670 | — | — |
  | 38824790605 | **0.566** | 20 | 3.57M |

- **Fragmentation signal:** the lowest hit rate coincides with the doubled call count and second Semble query in 38824790605. That points to a second pass with a different context prefix (inference). Item 2 under Cost Optimizations adds a prefix hash per call to confirm.
- **Write amortization:** 3,540,270 cache-write tokens bought 42,213,114 cache-read tokens (about 11.9:1), which is healthy.
- **CONTEXT_BUDGET_WARN:** one occurrence, in 38770060131 (`[pr:7447]`, 2.59M uncached prompt tokens over 13 calls). It is not yet eroding cache value window-wide.
- **npm cache:** the `claude-cli-npm-Linux-ea624bf9…` key hit in every review and judge run, so it needs no action.
- **Memory retrieval:** not measurable (see AI Memory Health).

## Orchestrator Health

- **Poll duty cycle is 100%.** Twelve consecutive ticks from 21:44 to 00:11 ran back-to-back (2–8 s gaps). Indicators to track:
  - `start(N+1) - end(N)`: below 10 s means saturated.
  - Poll step minutes per hour: currently about 60.
- **Poll reliability:** 62 of 64 succeeded. The one failure was Install Codex CLI (38804344938); one run was cancelled.
- **Wave, clarify, and deferral state:** not observable this window.
  - `orchestrate_clarify_respond` had 106 of 107 runs skipped.
  - clarify, plan and implement were 92%, 92% and 91% skipped (most events are event-driven no-ops of 1–11 s).
  - No state-snapshot or wave lines are visible because of truncation.
  - `STATE_SNAPSHOT_ARTIFACT_ENABLED: true` (38824719430), so the snapshot artifact could feed the next analysis.
- **Unblock judge repeats:** "Unblock judge #7258" ran twice within an hour (38817638930 at 22:31 and 38824360509 at 23:34), both successful. This may be a recurring stall on #7258 (inference).
  - **Indicator:** count `unblock_judge_dispatch` runs per target issue per day.
  - **Mitigation:** log the judge verdict as a single `UNBLOCK_JUDGE_RESULT issue=<n> verdict=<…>` line (diagnostic).
- **Heal recovery is clean:** `WORKFLOW_HEAL heal_scope_recovery outcome=scan reason=listed count=0` appeared in 38825493990, 38819300545 and 38812273695.
- **Merge train is idle:** `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` appeared in every cancel-on-close run. Separately, "preserved 3 active run(s) without pull-request linkage" appeared in 38826806777 and 38825945308; tracking that count over time would show whether it is benign.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix order |
|---|---|---|---|
| review/autofix | **Queueing** (concurrency group `pr-autofix-<PR>`) | 12–38 min waits; 8-run chain on PR #7452 | 1. Superseded-head freshness log, then skip |
| review/autofix | **Compute** | `[pr]` codex-agent 21–38 min (38812672324: 21:50:54–22:28:43) | 3. Per-phase GH and time checkpoints |
| review/autofix → CI | **Retry/loop** | 4 autofix pushes on #7452, each starting CI (about 800 s) and a review pair | 4. `AUTOFIX_ITERATION` convergence log |
| orchestrate poll | **Queueing/compute** (serialized ticks) | 7.5–12 min per tick, 100% duty cycle | 2. `POLL_PHASE_TIMING` |
| CI | **Compute tail** | `tests-promote-stall-and-review` failures at 760–1,208 s; `lint` waits about 14 min (38812641028) | 5. Fail-fast contract checks |
| implement / plan | Compute (rare) | implement p95 1,507.7 s (38739360437: 2,265 s); plan p95 745 s | No evidence-backed change |
| merge/conflict | Not observed | no conflict-heal lines visible | — |

The end-to-end ordering is review queueing, then poll serialization, then autofix-loop iteration count, then CI fail-fast.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - codex-agent serialization per PR, plus a likely duplicate review run per push.
  - Back-to-back poll ticks of about 10–12 min each.
  - CI tail at about 800 s per run.
- **Top failure modes:**
  - CI `tests-promote-stall-and-review`: 7 failures, all PR regressions.
  - review "Checkout PR head branch": 2 failures, cause unknown.
  - poll "Install Codex CLI": 1 failure, no retry in the action.
- **Highest cost drivers:** PR #7452's `[pr:7452]` autofix iterations (42.74M of 57.84M OR tokens) and unmeasured Claude spend in the plain review runs.
- **Top 3 actions:**
  1. Add `REVIEW_HEAD_FRESHNESS` and `REVIEW_CHECKOUT_FAIL` logging in `review_autofix.yml` codex-agent (Speed 1, Reliability 2).
  2. Add `POLL_PHASE_TIMING` plus a tail `POLL_TICK_SUMMARY` to `orchestrate_poll.yml`, and a 3-attempt retry to `.github/actions/install-codex/action.yml`.
  3. Collector fixes:
     - the sweep family matcher in `collect_workflow_logs.py:576`
     - Claude and `AI_MEMORY_TELEMETRY` counters in `cost_audit.py`
     - the `GH_PAT_BUDGET` rollover lower bound in `gh_helpers.sh`

## Metrics Appendix

**Run counts and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All | 1000 | 362 | 10 | 90 | 538 | 1.0% | 7 | 1017 |
| ci | 32 | 25 | 7 | 0 | 0 | 21.9% | 821 | 1172 |
| review_autofix (includes sweep) | 67 | 62 | 2 | 1 | 2 | 3.0% | 777 | 4239.5 |
| orchestrate_poll | 64 | 62 | 1 | 1 | 0 | 1.6% | 672 | 990.7 |
| implement | 107 | 8 | 0 | 2 | 97 | 0% | 1 | 1507.7 |
| plan | 107 | 9 | 0 | 0 | 98 | 0% | 1 | 745.1 |
| clarify | 120 | 9 | 0 | 0 | 111 | 0% | 1 | 392.9 |
| workflow_failure_heal_intake | 149 | 31 | 0 | 86 | 32 | 0% | 8 | 190.4 |

**Wall clock and review telemetry**

| Metric | Value |
|---|---|
| wall_clock_p50_ms (repo, 113 samples) | 13,000 |
| wall_clock_p99_ms (repo) | 4,767,720 |
| review_autofix wall_clock_p50_ms / p99_ms (23 samples) | 2,415,000 / 5,268,840 |
| break_glass_count | 0 |
| context_budget_warn_count | 1 (38770060131) |
| cache_hit_rate (reported) | null; derived 0.737 |

**Tokens (OpenRouter; codex_tokens_used = 0, Claude counters not exported)**

| prompt | completion | cache_read | cache_write | total | calls (usage avail / unavail) |
|---:|---:|---:|---:|---:|---|
| 11,500,044 | 588,621 | 42,213,114 | 3,540,270 | 57,840,394 | 71 (70 / 1) |

**PR #7452 codex-agent serialization chain** (group `pr-autofix-7452`)

| Order | Run | Type | codex-agent start → end |
|---:|---|---|---|
| 1 | 38812672324 | [pr] | 21:50:54 → 22:28:43 |
| 2 | 38812641332 | plain | 22:28:46 → 22:41:06 |
| 3 | 38817157151 | [pr] | 22:41:15 → 23:02:21 |
| 4 | 38817127287 | plain | 23:02:27 → 23:14:36 |
| 5 | 38820943553 | [pr] | 23:14:44 → 23:39:42 |
| 6 | 38820915647 | plain | 23:39:46 → 23:53:08 |
| 7 | 38824790605 | [pr] | 23:53:14 → about 00:18:24 |
| 8 | 38824769279 | plain | 00:18:26 → 00:31:04 |

**Poll tick runner/group waits (s, approx.):** 38820670162 about 497; 38819784128 about 287; 38826357322 about 270; 38814307139 about 234; 38822413524 about 176; 38813117015 about 177; 38825526800 about 126; 38818873019 about 100; 38823591647 about 93; 38817767109 about 77; 38812097704 about 2; 38824719430 about 2.

**GH API summary:** codex-agent `used_in_job` known values are 1190, 993, 568, 463, 310 and 168 (2 unknown, from window rollover). The minimum `remaining` was 3,000 (38820943553). There were 0 HTTP 429 or secondary rate-limit events.

**Semble**

| Workflow | query_calls | logged_bytes | bytes/query | fallbacks (contract-test / runtime) | bootstraps (failed / unused) | bootstrap_ms | sources | static_dup_bytes |
|---|---:|---:|---:|---|---|---:|---:|---:|
| review_autofix | 9 | 114,527 | 12,725 | 0 (0 / 0) | 9 (0 / 0) | 346,313 | 57 | 0 |
| orchestrate_poll | 1 | 5,967 | 5,967 | 0 | 1 (0 / 0) | 41,387 | 4 | 0 |
| ci | 0 | 0 | — | 52 (52 / 0) | 0 | 0 | 0 | 0 |
| **Total** | 10 | 120,494 | 12,049 | 52 (52 / 0) | 10 (0 / 0) | 387,700 | 61 | 0 |

**Serena / MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | tool_calls | fallbacks |
|---|---:|---:|---:|---:|---:|---:|---:|
| (none observed; `SERENA_ENABLED: false`) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

**Other MCP servers observed:** none.

**Data gaps:**
- The full-logs directory was not mounted.
- Codex-agent and poll logs were truncated by about 0.8–1.6M chars.
- `claude_*` counters are absent from the analysis context.
- `AI_MEMORY_TELEMETRY` is not parsed by the collector.
- Sweep runs are folded into `review_autofix`.
- The success sample rate was 0.07.

---

**Actions taken:** read-only analysis. I read `analysis_context` and the repo sources (`review_autofix.yml`, `ci.yml`, `orchestrate_poll.yml`, `internal-review.yml`, `internal-orchestrate-poll.yml`, `install-codex/action.yml`, `collect_workflow_logs.py`, `cost_audit.py`, `gh_helpers.sh`) and produced this report. No files were changed.

**Assumptions:** the codex-agent and poll "runner waits" are concurrency-group queueing. I inferred this from the exact end-to-start timestamp matches and the configured groups. The causes of the review checkout and Codex install failures are inferences, because no excerpts were available.

**Missing context:** the run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox, so the errors/, slow/ and recent/ logs could not be inspected.
## Deep Audit — Workflows & Scripts (2026-10-16)

Scope and method: I read the workflow and script sources listed below. I ran nothing. I could not measure byte or character counts because I had no shell, so every size figure is an estimate (line count × typical width, minus YAML block indent) and is marked `[NEEDS VERIFICATION]`. Some findings are already in the report above, and I do not repeat them:
- the "Checkout PR head branch" exit paths with no stated reason (Reliability 2)
- the `install-codex` action, which installs with no retry (Reliability 3)
- sweep runs being classified as `review_autofix` (Reliability 5)
- the `GH_PAT_BUDGET` window-rollover lower bound (GH API Call Audit)

Files examined in depth:
- `.github/workflows/test-and-mark-stable.yml`
- `.github/workflows/implement.yml`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/orchestrate_poll.yml`
- `.github/workflows/clarify.yml`
- `scripts/gh_helpers.sh`
- `scripts/comprehensive_test_and_release_gh_api.sh`
- `scripts/orchestrate_poll_process.sh`
- `scripts/review_rb_judge.sh`
- `scripts/review_freshness_ci_failed.sh`
- `scripts/review_commit_changes.sh`
- `scripts/review_conflict_resolve.sh`
- `scripts/label_helpers.sh`
- `.github/actions/install-codex/action.yml`

### Section 1: Bug & Correctness Sweep

**BUG-001** — `scripts/comprehensive_test_and_release_gh_api.sh:39-44`, `.github/workflows/test-and-mark-stable.yml:1383-1399`
- **Severity:** High
- **Category:** `bug`
- **Description:** On a failure that is not a rate limit, `gh_api_safe` writes `echo "::error::gh api call failed: $*"` to **stdout** (line 41). `gh_api_safe_print` (lines 50-58) is always called inside `$(...)`, so that error line becomes the captured value. In Phase 3b, `PR_NUMBER=$(gh_api_safe_print "repos/${TEST_REPO}/pulls?state=open&head=…" --jq '.[0].number // empty' || echo "")` (1383-1384) therefore gets a non-empty string on any transient 5xx. The check `if [ -n "$PR_NUMBER" ]` (1392) then logs "✓ Implementation PR #::error::gh api call failed… created" and writes `pr_number=<error text>` and `status=success` to `GITHUB_OUTPUT` (1397-1398). The release gate goes on with a bogus PR number. The `::error::` annotation is also swallowed, so it never shows in the log.
  - The same pollution feeds `LABELS` (857, 1052, 1403) and `IMPL_STEP_NAME` (1420). Each error string changes `CUR_STATE`, which resets the inactivity timer, so a real stall can be hidden.
  - The comment at 974-976 already admits that `gh_api_safe_print` "echoes its '::error::gh api call failed' line to stdout". Only `find_latest_scoped_run_field` guards against it, with a shape check at 125.
  - The Phase 4 callers use `gh_api_safe_quiet_print` (2132, 2135) and are not affected.
- **Recommended fix:**
  - Send the diagnostic to stderr: `echo "::error::gh api call failed: $*" >&2` at line 41.
  - Validate `PR_NUMBER` before the success branch, with `[[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] || PR_NUMBER=""` after 1384.
  - Callers that read stdout keep working, because a failure now prints nothing to stdout.

**BUG-002** — `.github/workflows/implement.yml:5151-5168`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The fall-through branch of "Handle no-op implementation" runs under `set -euo pipefail` (4942). It posts the no-op comment with `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (5151-5152), which has no `|| true`.
  - `gh_retry` makes only one attempt for a POST create (`gh_helpers.sh:1062-1065`, issue #6634).
  - A single failed POST therefore ends the step before the `ai:implementation-failed` label is applied (5165-5168). The orchestrator's re-issue path keys on that label, so the issue stays in `ai:implementing` (inference).
  - The POST also prints the full comment JSON to the log, because stdout is not redirected.
- **Recommended fix:** Apply the label first, then post the comment with `>/dev/null 2>&1 || echo "::warning::no-op comment post failed for #${ISSUE_NUMBER}"`. This matches the guarded POSTs at 4988-4989 and 5031-5032 in the same step.

**BUG-003** — `.github/workflows/test-and-mark-stable.yml:865-866, 891-899, 1060`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - Phase 1 detects failure with `gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments?per_page=5&direction=desc" --jq '[.[] | select(.user.login == "github-actions[bot]")] | .[0].body // ""'`. The per-issue "list issue comments" endpoint accepts only `since`, `per_page` and `page`; `sort`/`direction` belong to the repo-wide `issues/comments` endpoint. If so, `direction=desc` is ignored, the call returns the **oldest** 5 comments, and the check never sees a later clarification-needed comment.
  - The bot-login filter may never match if clarify posts with `GH_PAT` (a User account) — inference.
  - Line 865 uses the default page size (30), so `COMMENT_COUNT` stops rising at 30.
  - Line 1060 has the same ignored `direction=desc` with `per_page=100`. It is only harmful above 100 comments, because 1063 sorts by `updated_at`.
  - `[NEEDS VERIFICATION]` — the parameter support comes from my memory of the REST API, not from docs read in this run.
- **Recommended fix:** Fetch once with `--paginate` (or `?per_page=100&page=<last>`) and select the newest comment with `sort_by(.created_at) | last` in jq. Match on the comment marker the clarify phase emits rather than on the author login. This also removes the duplicate fetch in API-001.

**BUG-004** — `.github/workflows/test-and-mark-stable.yml:1407-1412, 1446`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The Phase 3b activity metrics read `actions/runs?per_page=50&created=>…` and filter only on `.name | test("Implement")`. They do not filter on `display_title == $ISSUE_TITLE`.
  - The step's own comments say the display-title scoping is required, citing release run 36374918973 (1239-1241). The comments at 1068-1072 also say a 50-run page dropped a run in v1.13.5.
  - Another issue's in-progress Implement run keeps `IMPL_RUN_IN_PROGRESS>0`. That blocks the failure-detection branch at 1446 for the whole phase, and its status changes keep resetting the inactivity timer (1431-1438).
  - If our run sits past position 50, `IMPL_RUN_TOTAL=0` and the branch is never entered.
  - Effect: a failed implement can surface only at the job timeout, not at the issue-scoped check. This is an inference; `[NEEDS VERIFICATION]` against a real release log.
- **Recommended fix:** Add `select(.display_title == $title)` (with `--arg title "${ISSUE_TITLE}"`) to the jq filters at 1409-1412, and raise the page to `per_page=100`. This matches `scoped_implement_runs_complete` (1301-1303).

**BUG-005** — `.github/workflows/orchestrate_poll.yml:145-151, 502-509` (12 copies in 10 workflows)
- **Severity:** Low
- **Category:** `bug`
- **Description:** "Ensure workflow support source checkout" checks only `if [ ! -d .codex-workflow-src ]`. `actions/checkout` creates the target path before fetching, so after a failed checkout and a failed fallback (both `continue-on-error: true`) the directory may exist but be empty. The guard then passes, and the failure shows up later as a "missing script" error instead. `[NEEDS VERIFICATION]` — this depends on actions/checkout@v5 internals. The same guard appears in `sync_ai_labels.yml`, `cancel_on_pr_close.yml`, `orchestrate.yml` (×2), `implement.yml`, `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml`, `clarify.yml`, `check_failure_triage.yml` and `plan.yml`.
- **Recommended fix:** Check for a file that must exist, for example `[ -f .codex-workflow-src/scripts/gh_helpers.sh ]`. The stage step at `orchestrate_poll.yml:168-172` already tests that file.

**SEC-001** — `.github/workflows/clarify.yml:66-73`
- **Severity:** Low
- **Category:** `security`
- **Description:** "Log trigger context" puts `${{ github.event.issue.number }}`, `${{ github.event_name }}`, `${{ github.event.action }}` and `${{ github.event.comment.user.login }}` straight into the `run:` body. GitHub logins are limited to `[A-Za-z0-9-]`, so injection is not possible today. Even so, this is the only event-payload field in the file that skips the env indirection used elsewhere; `COMMENT_USER_LOGIN` at 523 goes through env.
- **Recommended fix:** Add `env: { COMMENT_USER_LOGIN: ${{ github.event.comment.user.login || '' }} }` and use `"${COMMENT_USER_LOGIN}"`, `"${GITHUB_EVENT_NAME}"` and `"${ISSUE_NUMBER}"`. The last is already in job env at line 51.

**SEC-002** — `scripts/review_commit_changes.sh:660`, `scripts/review_conflict_resolve.sh:3643`, `scripts/review_rb_judge.sh:2521` (21 sites in 15 files)
- **Severity:** Low
- **Category:** `security`
- **Description:** `git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"` writes the PAT into `.git/config`.
  - In `review_commit_changes.sh` the push is deliberately deferred (comment at 661-663), so the credential stays on disk through the later conflict-resolver step, which launches an agent (`review_conflict_resolve.sh:2886-2906`).
  - The resolver runs in "fresh isolated snapshots" (2891-2892), which reduces the exposure. Whether the snapshot excludes the `GIT_DIR` config is `[NEEDS VERIFICATION]`.
- **Recommended fix:** Use the per-command credential helper the repo already has at `scripts/orchestrate_poll_process.sh:10823-10824`, i.e. `git -c credential.helper= -c 'credential.helper=!f() { printf "username=x-access-token\npassword=%s\n" "$GH_TOKEN"; }; f' push …`. Otherwise reset the URL to the token-free form right after each push.

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `.github/workflows/test-and-mark-stable.yml:857-899`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Each 10 s tick (`POLL_INTERVAL: 10`, line 558) of the Phase 1 loop makes 3 REST calls: the issue (857), `issues/N/comments` (865) and `issues/N/comments?per_page=5…` (891). The last two hit the same endpoint in the same iteration.
- **Current vs. proposed:** 3 calls per tick (about 180 over a 10-minute clarify) → 2 per tick (about 120). That is roughly 33% fewer calls in this phase.
- **Recommended fix:** Fetch the comments once and derive both `COMMENT_COUNT` and the newest bot comment with jq, as Phase 2 already does at 1060-1063. Pair this with the BUG-003 fix.

**BATCH-001** — `.github/workflows/test-and-mark-stable.yml:1383, 1403, 1407, 1420, 1425`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Each Phase 3b tick makes 5 REST calls:
  - open PR by head
  - issue labels
  - runs list
  - jobs of the latest run
  - `commits?sha=ai/issue-N`

  Three of them (the PR by head ref, the issue labels and the branch head OID) can come from one GraphQL query: `repository{issue(number:N){labels(first:50){nodes{name}}} pullRequests(headRefName:"ai/issue-N",states:OPEN,first:1){nodes{number}} ref(qualifiedName:"refs/heads/ai/issue-N"){target{oid}}}`.
- **Current vs. proposed:** 5 calls per tick (about 1,800/h at 10 s) → 3 per tick (1 GraphQL + runs + jobs, about 1,080/h).
- **Recommended fix:** Add a `gh_graphql_safe_print` sibling in `scripts/comprehensive_test_and_release_gh_api.sh`, following the aliased-query style of `_fetch_candidate_issue_details_graphql` (`orchestrate_poll_process.sh`, §14 BATCH_HELPER). Fail open to the current REST calls. `[NEEDS VERIFICATION]` — check the GraphQL point cost against the REST saving.

**BATCH-002** — `scripts/orchestrate_poll_process.sh:7729-7734`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** `security_pass_unblock_filed_advisory_followups` makes one `gh_retry gh api "repos/…/issues/${row_issue}"` call per unchecked follow-up row inside a `while read` loop. The header (7700-7710) documents this as audited, but it still costs N calls.
- **Current vs. proposed:** N REST calls → ceil(N/25) GraphQL calls (`issueN: issue(number:N){state labels(first:50){nodes{name}}}`).
- **Recommended fix:** Reuse the `_fetch_candidate_issue_details_graphql` alias pattern, or add a sibling that returns `{number: {state, labels}}`. Keep the per-row REST fallback when the batch fails.

**BATCH-003** — `scripts/orchestrate_poll_process.sh:46-57`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** `replay_failed_reclarify_commands` lists queued issues with `--json number` (47), then re-reads each issue (55) only to check `state`, `pull_request` and the label. Those fields could come from the listing call.
- **Current vs. proposed:** 1 + N issue reads → 1. The per-issue comments read stays.
- **Note:** the header at 37-41 describes the per-issue "live issue read" as deliberate, so this may be an intentional TOCTOU recheck. `[NEEDS VERIFICATION]`
- **Recommended fix:** If the recheck is not needed, request `--json number,state,labels` at 46-47 and drop line 55.

**API-002** — `.github/workflows/implement.yml:5084-5097`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** Each ancestor-chain hop costs 2 REST calls: the issue body (5085) and paginated comments (5089-5090). The hops depend on each other, so they cannot be batched together, but each hop's two reads can be merged.
- **Current vs. proposed:** 2×T calls (T = `IMPL_NOOP_ANCESTRY_THRESHOLD`, default 2) → T GraphQL calls (`issue(number:N){body comments(last:100){nodes{body}}}`).
- **Recommended fix:** Merge the two reads per hop. Fall back to REST when there are more than 100 comments.

**API-003** — `scripts/review_freshness_ci_failed.sh:66-80`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** The marker POST is retried 3 times with a fixed sleep, including on permanent errors (locked conversation, the 2,500-comment cap, 403/422). Those can never succeed.
- **Current vs. proposed:** up to 3 calls on a permanent error → 1.
- **Recommended fix:** Use `gh_api_retry --idempotent -X POST …` (`gh_helpers.sh:910-1035`). It classifies permanent, transient and rate-limit failures and backs off exponentially. The comment at 66-67 notes that a duplicate marker is harmless, so `--idempotent` is safe.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — inline GH_PAT_BUDGET blocks vs. `scripts/gh_helpers.sh:31-77`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The `gh api rate_limit` → parse → write snapshot → `echo "GH_PAT_BUDGET …"` logic is copied inline in many steps:
  - `review_autofix.yml`: 362-374, 690-707, 725-737, 908-925, 936-948, 1000-1017, 1074-1086, 1409-1426, 1477-1489, 1737-1754, 1913-1925, 5673-5690
  - `clarify.yml`: 75-87, ~2091, ~2155
  - `orchestrate_poll.yml`: 97-111
  - the start blocks of `validation-improvements-intake.yml` and `workflow-failure-heal-intake.yml`

  The copies are inline because the helper is not staged yet at job start (comment at `orchestrate_poll.yml:102-103`) and has been deleted by the time the job ends (`review_autofix.yml:5669`).
  - The copies have drifted. They read the overview body, while `gh_pat_budget` overrides it with the `x-ratelimit-*` headers when the resource is `core` (gh_helpers.sh:50-60).
- **Recommended fix:** Add a self-contained composite action `.github/actions/gh-pat-budget/action.yml` (inputs `phase`, `workflow`, `job`, `snapshot_file`; body copied from `gh_pat_budget`). Reference it pinned like the other actions in `.github/actions/`. Each copy then becomes a 5-line `uses:` step, which also reduces `review_autofix.yml` bytes (see EXPR-005).

**DUP-002** — support-source checkout sequence (31 `wf_source="shubhodeep1/coding-workflows"` copies in 13 workflows); `orchestrate_poll.yml:113-162` and `469-520`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The 5-step "resolve `SCRIPT_REF` → checkout → fallback to main → ensure → main snapshot" sequence is repeated across workflows (`implement.yml` ×5, `orchestrate.yml` ×4, `plan.yml`/`clarify.yml`/`issue_pr_status.yml`/`orchestrate_poll.yml` ×3 each). Inside the single `poll` job of `orchestrate_poll.yml` it runs **twice**. The second pass re-resolves the same `SCRIPT_REF` and checks out into the same `.codex-workflow-src` and `.codex-workflow-src-main` paths. That adds 2-4 redundant authenticated fetches per tick to the poller, which is already the orchestrator's critical path (see the report's Speed item 2).
- **Recommended fix:**
  - In `orchestrate_poll.yml`, add `if: hashFiles('.codex-workflow-src/scripts/gh_helpers.sh') == ''` to the second checkout and fallback steps, or have the first sequence check out the full support tree.
  - Across workflows, put the resolve-and-ensure logic in a composite action (`.github/actions/checkout-workflow-support`) with inputs `path` and `fallback_ref`.

**DUP-003** — failure-fingerprint block: `review_autofix.yml:4915-4930`, `review_autofix.yml:5006-5021`, `scripts/review_autofix_step_post_editor_summary.sh:103`, `scripts/review_autofix_step_assemble_failure_evidence.sh:63`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The same `python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint` invocation, with six `--evidence-file` arguments, appears four times. The comment at 4911-4914 requires all copies to stay identical ("every failure comment of one run carries one fingerprint").
- **Recommended fix:** Add `autofix_failure_marker_suffix()` (no arguments; prints `$'\n\n'<marker>` or nothing) to a sourced helper, for example in `scripts/review_autofix_step_assemble_failure_evidence.sh` or a new `scripts/review_autofix_failure_helpers.sh` listed in `REQUIRED_BOOTSTRAP_SCRIPTS`. Call it from all four sites.

**DUP-004** — inline `gh_retry gh label create` (25 sites outside `label_helpers.sh`), e.g. `implement.yml:4976-4978, 5023-5025, 5110-5112, 5161-5163`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `ensure_label_exists` (`label_helpers.sh:157`) is the canonical helper and is even used in the same step (`implement.yml:5159`). The inline copies have drifted from its registry:
  - `ai:closed` is created as `6b7280` / "Issue closed by automation…" (4976-4977), but the registry says `6a737d` / "Linked PR closed without merge" (`label_helpers.sh:61, 123`).
  - `ai:needs-human` is created as `b60205` (5023-5024), but the registry says `e11d48` (`label_helpers.sh:48, 110`).
- **Recommended fix:** Source `label_helpers.sh` when it is present (pattern at 5157-5158) and call `ensure_label_exists <label> "${REPO}"`. Keep the inline create only as the fallback when the helper is missing, using the registry's colour and description.

**DUP-005** — `scripts/comprehensive_test_and_release_gh_api.sh:3-48` vs. `scripts/gh_helpers.sh:910-1035`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** `gh_api_safe` re-implements rate-limit retry with a weaker policy:
  - It matches on the text "rate limit" only.
  - It has no secondary-limit or `Retry-After` handling, and no handling for permanent vs. transient errors.
  - It never retries 5xx.

  `gh_api_retry` already provides all of these, plus the `GH_API_RETRY` log line. This duplicate is also where BUG-001 comes from.
- **Recommended fix:** Make `gh_api_safe` a thin wrapper. Source `gh_helpers.sh`, call `gh_api_retry "$@"`, store stdout in `GH_API_SAFE_OUTPUT`, and keep the 0/1 return contract that `find_latest_scoped_run_field` depends on.

**DUP-006** — `scripts/review_rb_judge.sh:2531-2551` vs. `2554-2574`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The "fix produced no effective changes → treat as merge" arms are identical: the security hold, the merge gate, the status post, the label swap loop and the three `GITHUB_OUTPUT` lines.
- **Recommended fix:** Extract `rb_judge_finish_merge_verdict()` (no arguments; reads `ISSUE_NUMBERS`, `RB_JUDGED_HEAD_SHA`, `REPOSITORY`) and call it from both arms.

### Section 4: Expression Size Limit Risk Assessment

Method: a `run:` body counts toward the 21,000-character limit only when it contains `${{ }}`. Estimates are de-indented line count × observed width, plus the long lines, and all are `[NEEDS VERIFICATION]` with `yq '.jobs.<job>.steps[<i>].run' | wc -c`. The repo's own precedent for the fix is the comment "Promoted to env so the run: script body contains no ${{ }}" (`test-and-mark-stable.yml:1825, 2606`).

**EXPR-001** — `.github/workflows/test-and-mark-stable.yml:936-1203` ("Phase 2: Wait for plan to complete")
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** A 268-line body. It contains `${{ steps.create-issue.outputs.created_after }}` 3 times (954, 966, 984) and several 200-330-character lines (997, 1144, 1170). Estimated 15.5–17.5K characters, leaving about 3.5–5.5K of headroom.
- **Recommended fix:** Add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step `env:` and replace the three uses with `${CREATED_AFTER}`. The body then has no expressions and is exempt from the limit.

**EXPR-002** — `.github/workflows/test-and-mark-stable.yml:1243-1503` ("Phase 3b: Wait for PR creation")
- **Severity:** High
- **Category:** `expression-limit`
- **Description:** A 261-line body. Its only expression is `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` (1281), and it has many 150-300-character jq and error lines (1301-1307, 1372, 1409-1412, 1478, 1490, 1501). Estimated 16–18.5K characters, leaving about 2.5–5K of headroom, with an upper bound at the 85% threshold. BATCH-001 and BUG-004 would add code to this step.
- **Recommended fix:** Move it to `env: IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` and delete line 1281. This must land before or with BATCH-001 or BUG-004.

**EXPR-003** — `.github/workflows/implement.yml:4941-5169` ("Handle no-op implementation")
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** A 229-line body with 9 expressions (4946 ×3, 4948, 5151, 5159, 5161, 5165, 5167) and long prose lines (4971, 4973, 5011, 5018, 5105, 5107). Estimated 14–16K characters, leaving about 5–7K of headroom.
- **Recommended fix:** Replace the expressions with the runner defaults `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. `REPO` is already set at 4948 and is ignored at 5151-5167. Better still, move the body to `scripts/implement_handle_noop.sh`, following `scripts/implement_handle_guard_block.sh`.

**EXPR-004** — watch list, under 15K: `test-and-mark-stable.yml:782-911` (Phase 1), `4468-4633` (Verify all phases passed), `7366-7551` (Send Telegram notification); `review_autofix.yml:4904-4987`, `4995-5133` (Telegram editor-changes-lost and noop-suspicious), `2781-2914` (Checkout PR head branch)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** These bodies contain `${{ github.repository }}`, `${{ github.server_url }}`, `${{ needs.*.result }}` or `${{ steps.*.outputs.* }}`. Estimated sizes: 7–12K (Telegram notification about 10–12K, with about 25 expressions at 7373-7393 and 7422-7544). Headroom is at least 9K.
- **Recommended fix:** Replace the `github.*` expressions with the runner defaults. Move `needs.*` and `steps.*` values into `env:` the next time each step is edited. For the two `review_autofix.yml` Telegram steps, also move the bodies to `review_autofix_step_<slug>.sh` following `agents.md:2374-2390`.

**EXPR-005** — whole-file size: `test-and-mark-stable.yml` (7,551 lines), `implement.yml` (6,494), `review_autofix.yml` (5,690)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The 1 MB / 800 KB thresholds in the audit brief are wrong for this repo. The verified limit is **512,000 bytes**: a larger file produces zero-job runs that conclude `failure` with "workflow file issue" (`agents.md:2348-2354`). CI's guard is 480,000 bytes (`agents.md:2362-2365`), and `budget-watch` warns at 440,000 (`agents.md:2337-2341`). No file can be over 800 KB and still run. At typical widths of 55–65 bytes per line, `test-and-mark-stable.yml` is roughly 415–490 KB, which may already be inside the budget-watch band. `[NEEDS VERIFICATION]` with `wc -c .github/workflows/*.yml`.
- **Recommended fix:** If `test-and-mark-stable.yml` is at or above 440,000 bytes, move the largest no-expression bodies first: Phase 4 (1795-2526), Phase 4b (2596-3266) and Phase 6 (3397-3820). Put them in `scripts/` files invoked like `scripts/comprehensive_test_and_release_gh_api.sh`. DUP-001 and DUP-003 also take bytes off `review_autofix.yml`.

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `.github/workflows/test-and-mark-stable.yml:789/861-863, 943/1056-1058, 1257/1387-1390, 1877/2140-2143, 3556/3706-3709`
- **Severity:** Medium
- **Category:** `dead-code`
- **Description:** Each wait loop sets `RATE_LIMIT_BACKOFF=0` and then checks `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z … ]` to "skip this cycle". The only place the variable changes is inside `gh_api_safe` (`comprehensive_test_and_release_gh_api.sh:17, 28-31`). Every caller runs it inside `$(...)`, so the change happens in a subshell and never reaches the loop. All five guards are therefore dead.
  - They are also unsafe if ever made reachable: 861 and 1056 `continue` without sleeping, and 2140 only adds to `ELAPSED`. Each would become a tight API loop.
- **Recommended fix:** Delete the five guards. Once DUP-005 is done, `gh_api_retry` handles the rate-limit wait itself. Otherwise add `sleep "$POLL_INTERVAL"` to every guard before `continue`.

**DEAD-002** — `scripts/gh_helpers.sh:91, 107-108`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `gh_review_pr_state` accepts `merged`, but REST `pulls/{n}` `.state` returns only `open` or `closed`. The `merged` branch of `grep -xE 'open|closed|merged'` and of the regex at 108 can never match. It does no harm, but it suggests callers might receive `merged`.
- **Recommended fix:** Either drop `merged` and document "closed includes merged", or read `--jq 'if .merged then "merged" else .state end'` to return a real merged state (behaviour change; check the callers first).

**CONSIST-001** — `.github/workflows/test-and-mark-stable.yml:1117-1118, 1472-1473`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** The label and PR rechecks inside the polling loops call raw `gh api … 2>/dev/null || echo ""`. Every other read in the same loops uses `gh_api_safe_print`, so these two have no rate-limit backoff and fail silently.
- **Recommended fix:** Switch them to `gh_api_safe_quiet_print`, which also avoids the stdout pollution in BUG-001.

**CONSIST-002** — `review_autofix.yml:3346, 3354, 4908-4909, 4976, 4999-5000, 5059, 5110, 5132`; `implement.yml:5151-5167`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** Run bodies use `${{ github.repository }}` and `${{ github.server_url }}` where `${GITHUB_REPOSITORY}` and `${GITHUB_SERVER_URL}` (or a local `REPO`, `implement.yml:4948`) are already available. Mixing the two forms keeps these bodies needlessly subject to the expression limit.
- **Recommended fix:** Use the runner default env vars (no behaviour change).

**CONSIST-003** — `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }` (75 sites in 39 files, e.g. `review_autofix.yml:4907, 4998`, `implement.yml:4944`)
- **Severity:** Low
- **Category:** `consistency`
- **Description:** When `gh_helpers.sh` fails to source, which is itself hidden by `2>/dev/null || true` (4906, 4997), retries are silently turned off. `agents.md:2384-2386` says a missing required script should fail the step with `::error::`. The shim fails open with no signal, so a staging regression cannot be seen.
- **Recommended fix:** Keep failing open but make it visible: `type gh_retry >/dev/null 2>&1 || { echo "::warning::gh_helpers.sh unavailable; gh_retry falls back to single attempt"; gh_retry() { "$@"; }; }`.

**SHELL-001** — `.github/workflows/review_autofix.yml:5077-5078`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** The step counts files with `ls -1 … | wc -l` and `ls -1 … | sort -V | tail -n 1` (SC2012). The `editor_attempt_<n>.err` names are under control, so the risk is low.
- **Recommended fix:** Use `shopt -s nullglob; editor_err_files=( "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err ); editor_attempts=${#editor_err_files[@]}`.

**SHELL-002** — `.github/workflows/test-and-mark-stable.yml:902, 1194, 1494`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `if [ $IDLE -ge $INACTIVITY_LIMIT ]` is unquoted (SC2086). Both values are always arithmetic results, so this is cosmetic. Under `set -u` an unset value would give a confusing `[: -ge: unary operator expected`.
- **Recommended fix:** Quote them: `[ "${IDLE}" -ge "${INACTIVITY_LIMIT}" ]`.

**DEBT-001** — `.github/workflows/pr6576-shard-followup.yml:14-22`; `docs/scripts-pending-removal.md:331-339`
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** This single-use workflow is in the removal registry, but it still triggers on every push to `main` (line 15-16). Once the marker is posted, each run is a no-op that still uses runner minutes and adds noise. Whether the removal trigger has fired is `[NEEDS VERIFICATION]`.
- **Recommended fix:** Run the registry preflight checks (`docs/scripts-pending-removal.md:337-339`). If all pass, remove the workflow, `scripts/pr6576_shard_followup.py`, the test file and the registry entry in one PR.

TODO/FIXME/HACK/XXX markers: a search of `.github/workflows/*.yml`, `scripts/*.sh` and `scripts/*.py` found none.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-002 |
| Medium | 12 | BUG-002, BUG-003, BUG-004, DEAD-001, API-001, BATCH-001, DUP-001, DUP-002, DUP-005, EXPR-001, EXPR-003, EXPR-005 |
| Low | 18 | BUG-005, SEC-001, SEC-002, DEAD-002, BATCH-002, BATCH-003, API-002, API-003, DUP-003, DUP-004, DUP-006, EXPR-004, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/test-and-mark-stable.yml` | Small |
| API call optimization | `.github/workflows/test-and-mark-stable.yml`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml`, `scripts/review_freshness_ci_failed.sh` | Medium |
| Code modularization | `.github/actions/gh-pat-budget/action.yml` (new), `.github/actions/checkout-workflow-support/action.yml` (new), `.github/workflows/review_autofix.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/clarify.yml`, `.github/workflows/implement.yml`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/review_rb_judge.sh`, `scripts/review_autofix_step_*` helpers, `scripts/label_helpers.sh` callers | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, optional new `scripts/implement_handle_noop.sh` | Small |
| Medium/Low fixes | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/clarify.yml`, `.github/workflows/orchestrate_poll.yml` (and 9 other workflows for BUG-005), `scripts/gh_helpers.sh`, `scripts/review_commit_changes.sh`, `scripts/review_conflict_resolve.sh`, `scripts/review_rb_judge.sh`, `.github/workflows/pr6576-shard-followup.yml` | Medium |