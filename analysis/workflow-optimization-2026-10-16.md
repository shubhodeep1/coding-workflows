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