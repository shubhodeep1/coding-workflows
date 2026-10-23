## Executive Summary

Data scope: 1,000 runs from `shubhodeep1/coding-workflows`, created between 2026-10-23 16:51 and 19:04 UTC (about 2h13m). `insufficient_data=false`. The mounted run-logs directory (`/home/runner/work/_temp/workflow-log-output`) does not exist in this sandbox. Evidence therefore comes from `analysis_context.json` (metrics plus the `log_summary` fields) and the repository source. Step-level failure text for the 4 CI failures could not be read.

- **Runners being busy is the main delay for light workflows (high confidence on the symptom; the cause is an inference).** 9 `log_summary` rows show waits from 38s to about 7 min on "All GitHub-hosted runners with label [ubuntu-latest] are busy" for jobs that ran for ≤8s:
  - lint 40007334137: about 7m wait, about 5s of work.
  - readiness 40007293573: about 3m42s wait.
  - lint 40007293489: about 3m20s wait.
  - clarify-respond 40007436510: about 145s wait, then a gate skip.
  - CI p50 is 2,146s and p95 3,942s, yet every CI job I checked has `timeout-minutes: 20`. Run 39991448189 succeeded in 4,359s. That implies most CI wall-clock is queueing (inference).
- **Review/autofix drives almost all token cost, and 39 of its 43 runs are not metered (high).** 123.6M tokens came from only 4 runs: 39996936194 (57.8M), 39998244730 (32.3M), 39992707066 (29.7M), 39991593165 (3.8M). That is about 30.9M per run, against a p50 of 2,149s. The aggregate `cache_hit_rate` is 0.876, but run 39991593165 reached only 0.639.
- **CI failure rate is 14.3% (4/28), and 2 of the 4 failures are in one step (medium).** "Workflow failure heal contract and driver tests" failed in 39990854601 and 39999013651. In-repo comments say this step already used 15m12s of its 20-minute job budget. Without logs, I cannot tell a timeout from an assertion failure.
- **The same PRs are reviewed repeatedly, and the dispatch line cannot say why (medium).** PR #7959 was reviewed 3 times (39991597718, 39999877607, 40005242681). #7989 and #7990 were reviewed twice each. Sweep 40007996422 dispatched all three again at 19:00. `AUTOFIX_SWEEP_DISPATCH` (`review_autofix_sweep.yml:544`) logs no `head_sha`, so new commits cannot be told apart from churn. Each avoidable run costs about 35–55 min of runner time and possibly about 30M tokens.
- **The collector window is too short to show trends (high).** 797 of 1,000 rows are skipped runs from one event fanning out to clarify, plan, implement and clarify-respond. The 1,000-run cap therefore covers about 2h13m, not a week.
- **Semble and Serena are healthy (high).**
  - Semble: 0 runtime fallbacks. All 32 fallbacks are `context=contract-test` lines from CI. 14 queries returned 173,709 bytes (about 12.4 KB each, 5.6 sources per query). 0 static-duplicate bytes, 0 unused bootstraps.
  - Serena: disabled (`SERENA_ENABLED: false` in 40007293665), with 0 telemetry of any kind.

## Speed Optimizations

**1. Measure and reduce hosted-runner queueing (critical path, largest win)**
- **Evidence:** busy-runner waits in 40007996422 (about 68s of 97s), 40007436510 (about 145s of 155s), 40007334421 (3m11s of 200s), 40007334137 (about 7m of 423s), 40007293489 (3m20s of 210s), 40007293665 (3m13s of 211s), 40007293573 (3m42s of 231s), 40007284890 (about 90s of 227s) and 40007285053 (38s).
  - CI runs take 36–73 min (39991448189 4,359s; 39991457053 4,144s) even though jobs are capped at 20 min.
  - `ci.yml` has no `concurrency:` group, so CI for a superseded PR head keeps running and holding runners. This is an inference; superseded runs have not been counted yet.
- **Root cause:** about 40 long-running review/autofix and CI runs overlapped in the 17:00–18:30 window. CI fans out wide (for example the 4-way `orchestrate-poll` matrix, `ci.yml:2082-2086`). Event fan-out adds short gate jobs on top.
- **Exact change, phased:**
  1. **Diagnostic first, zero extra API calls.** In `scripts/collect_workflow_logs.py`, add a per-run `queue_wait_seconds` field (name checked; unused today). For failed or cancelled runs, compute it from the job `created_at` / `started_at` already in the jobs payload (`list_jobs_for_run`, called at `:2168`). For deep-dive runs, count "Waiting for a runner" lines in the log archive the collector already downloads.
  2. **Then**, if CI runs for superseded heads are shown, add a PR-scoped concurrency group to `ci.yml`. `scripts/ci_cancelled_rerun.py:329` already skips `superseded_head`, so cancelled superseded runs will not be re-run.
     ```yaml
     concurrency:
       group: ci-${{ github.event.pull_request.number || github.sha }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
- **Estimated savings:** light jobs could get back 1–7 min each. Savings on CI wall-clock are unknown until `queue_wait_seconds` is collected.
- **Risk:** low for the diagnostic. Medium for the concurrency group: it must not apply to `push` on the default branch, and the required-check behaviour must be confirmed.

**2. Orchestrator poll cycles run longer than the cron interval**
- **Evidence:** `internal-orchestrate-poll.yml:5` runs every 5 min (`*/5 * * * *`). Poll p50 is 440s and p95 1,242s; the longest were 39996752824 at 1,433s and 39992417054 at 1,194s.
  - 10 of 17 poll runs were cancelled. 40005943510 was cancelled before its first step after 562s.
  - Expected behaviour: `orchestrate_poll.yml:68-73` sets `cancel-in-progress: false`, so a newer queued run replaces the pending one.
- **Root cause:** the poll body takes longer than the interval, so the real poll cadence is about 7–20 min.
- **Exact change:** add a diagnostic `POLL_SECTION_TIMING section=<name> elapsed_ms=<n>` line at the main phase boundaries of `scripts/orchestrate_poll_process.sh` (prefix checked; unused). Do not change the concurrency setting.
- **Estimated savings:** none directly; it identifies which section to cut. Wave-progression latency is bounded by poll cycle time.
- **Risk:** very low. The change only adds log lines.

**3. Semble is bootstrapped twice per review run (micro-optimization)**
- **Evidence:** 12 bootstraps took 559,841 ms in total (46.7s on average). Runs with 2 bootstraps spent 92.5–102.9s on them (39996936194, 39992707066, 39992698001, 39998244730, 39993724109).
- **Root cause:** `SEMBLE_BOOTSTRAP_STATE_FILE` lives under `RUNNER_TEMP` (`semble_helpers.sh:13`), so each job on a separate runner builds its own index.
- **Change:** only consider sharing the index between jobs after the bigger items. The saving is about 46s per run, about 2% of the 2,149s p50.
- **Risk:** low.

**4. Smaller items**
- `heal-pr-reconcile` in 40007284890 fetches the full tag list (v1.3.0 through v1.38.2 and more). Use a shallow checkout with no tags if the step does not need tags. Saves seconds.
- 40007285053 has an unexplained gap of about 106s before `finalize-task`. Add timing around the AI-memory finalize call before optimizing it.

## Cost Optimizations

**1. Find out why the same PRs are reviewed repeatedly (largest potential saving)**
- **Evidence:**
  - #7959: 39991597718 (3,270s), 39999877607 (2,305s), 40005242681 (1,289s), then dispatched again by sweep 40007996422.
  - #7989: 39991593165 (3,234s, 3.83M tokens) and 39999873244 (2,082s).
  - #7990: 39991589039 (3,065s) and 39999868916 (2,058s).
  - The sweep printed `AUTOFIX_SWEEP_END dispatched=3 skipped_active=5`.
- **Root cause:** unknown. `AUTOFIX_SWEEP_DISPATCH pr=#N head_ref=…` (`review_autofix_sweep.yml:544`) has no `head_sha` and no reason field, so a real new push cannot be told apart from a re-review of an unchanged head.
- **Exact change (diagnostic, zero extra API calls):** `head_sha` is already in each row's projection (`:233`).
  ```bash
  head_sha="$(printf '%s' "${row}" | jq -r '.head_sha // "unknown"')"
  echo "AUTOFIX_SWEEP_DISPATCH pr=#${pr_number} head_ref=${head_ref} head_sha=${head_sha}"
  ```
  Then compare against the head SHA of the last completed `[pr:N]` run. Only after that data exists, consider an `AUTOFIX_SWEEP_SKIP reason=head_already_reviewed` gate.
- **Estimated savings:** each avoided run saves about 35–55 runner-minutes and possibly about 30.9M tokens (the average of the 4 metered runs).
- **Quality risk:** none from the logging. A future skip gate must still re-review when CI or review-thread state changes.

**2. Low prompt-cache reuse on short review sessions**
- **Evidence:** 39991593165 had a cache hit rate of 0.639 over 10 calls: 423,436 tokens written to cache against 2,439,897 read, plus 955,604 uncached prompt tokens. For comparison:

  | Run | Calls | Cache hit rate |
  |---|---|---|
  | 39996936194 | 22 | 0.921 |
  | 39992707066 | 20 | 0.854 |
  | 39998244730 | 22 | 0.845 |

- **Root cause (inference):** short sessions pay to write the cache prefix but get few reads back. A prefix that varies between calls (dynamic PR or Semble context placed ahead of the static instructions) would make this worse.
- **Exact change (diagnostic):** emit `PROMPT_PREFIX_FINGERPRINT phase=<p> static_bytes=<n> static_sha=<first-12-hex> dynamic_bytes=<n>` once per model call (name checked; unused). It shows whether the static prefix hash is stable within a run.
- **Estimated savings:** raising a 39991593165-type run to 0.85 would cut uncached-plus-write input from about 1.38M to about 0.57M tokens per run.
- **Quality risk:** none.

**3. Close the metering gap**
- **Evidence:** only 4 of 43 review/autofix runs have usage lines (`or_calls` totals 22+20+22+10 = 74 = the window total).
  - 39992698001, 39993724109 and 39991597718 ran Semble but report 0 for both `or_calls` and `codex_calls`. This suggests a different engine whose `claude_*` counters (`CLAUDE_COUNT_KEYS`, aggregated at `cost_audit.py:1041-1045`) are missing from `analysis_context`. That is an inference.
- **Exact change:** add `claude_calls`, `claude_*_tokens` and `claude_cost_usd` to the per-run and per-family `cost_telemetry` that the analysis context is built from.
- **Estimated savings:** none directly. It is needed to size every other cost item. No dollar figure can be given today.

**4. Semble: keep it, it adds little prompt**
- 173,709 bytes is roughly 43k tokens at about 4 bytes per token (inference). That is about 0.4% of the 11.57M uncached prompt tokens, with 0 static-duplicate and 0 echo-dropped bytes. The context is not noisy.
- The cost is bootstrap time (Speed #3), not tokens.

**5. Serena:** disabled, with 0 calls, so there is nothing to evaluate.

**6. Model choice:** `MODEL_EDITOR: openai/gpt-6-sol` runs at `MODEL_REASONING_EFFORT: high` (40007436510 env). There is no per-phase usage split, so do not change the model or reasoning level until item 3 lands.

## Reliability Improvements

**1. "Workflow failure heal contract and driver tests": 2 of 4 failures**
- **Evidence:** 39990854601 (16:55, 2,349s) and 39999013651 (17:55, 2,309s), both in job `tests-heal-plan-and-validation`, which has `timeout-minutes: 20` (`ci.yml:950`). The step comment (`ci.yml:968-977`) records 15m12s of the 20-minute budget on main, with 297 of 397 tests in `tests/test_workflow_failure_heal.py`, run as local shards.
- **Root cause category:** unknown. It is either budget pressure on a busy runner (inference) or a real regression. The logs are not mounted.
- **Exact fix (diagnostic first):** after each shard, emit `CI_STEP_RESULT step=heal-contract shard=<i>/<n> exit=<rc> elapsed_s=<n> budget_s=1200` (name checked; unused), and add `--durations=15` to the shard pytest command. A job timeout then shows up as a missing final line or a cancelled conclusion, and an assertion failure as `exit=1`.
- **Then:** if it is a timeout, raise the existing shard count, which is already the mechanism this step uses. If it is an assertion, it is a code regression for the owning PR.
- **Expected impact:** up to 50% of this window's CI failures.
- **Rollback:** log lines only.

**2. `orchestrate-poll (1)`: "Orchestrate poll process unit tests" (39996848455)**
- One matrix group out of 4 failed (`fail-fast: false`, `ci.yml:2084`). Apply the same `CI_STEP_RESULT` line per group and per local shard, so a flaky shard can be told from a real failure.
- CI run 40001124873 succeeded on attempt 2 (`retries=1`). Re-runs are already happening and are not being tracked as flakiness.

**3. "Inventory parity" (39992706892)**
- `tests/inventory_parity.py` (`ci.yml:1988-1991`) is a deterministic contract check. The failure probably comes from PR-introduced drift, not a flake (inference).
- Check that the script prints the missing and extra entries. Without the logs I could not confirm whether it already does.

**4. Semble fallbacks are healthy; report the runtime count, not the headline**
- All 32 `SEMBLE_FALLBACK` lines are `context=contract-test` (classified by `cost_audit.py:822-827`), with 0 runtime fallbacks.
- The rate is 4 per CI run, but 8 in 39999013651. That suggests the contract test ran twice in that run, for example on a retry (inference).
- Mitigation: dashboards and alerts should use `semble_runtime_fallbacks`.

**5. Serena probes:** 0 `SERENA_PROBE` lines and 0 fallbacks, consistent with `SERENA_ENABLED: false`. This is not a masked broken rollout.

**6. `BREAK_GLASS` / `CONTEXT_BUDGET_WARN`:** 0 and 0 in every family. There is no policy or prompt-size pressure in this window.

**7. `GH_PAT_BUDGET used_in_job=unknown`** (sweep 40007996422)
- `gh_helpers.sh:62-72` leaves `used` unknown in three cases: no start snapshot was taken, the reset window rolled over, or `remaining` went up.
- **Do not** append a field to the existing line: the collector regex is anchored with `used_in_job=([0-9]+|unknown)$` (`collect_workflow_logs.py:112`) and would stop matching.
- Instead, emit a separate `GH_PAT_BUDGET_DIAG reason=<no_start_snapshot|reset_rolled|remaining_increased>` line (name checked; unused).

## AI Memory Health

- **Telemetry found:** one line, in 40007285053: `AI_MEMORY_TELEMETRY: {"did_push": true, "final_state": "merged", "issue_number": 7994, "ok": true, "op": "finalize-task"}`, with `push_attempts` 1 and lineage committed.
- **Not found:** no `retrieve`, `record-candidate`, `promote`, `compact` or `processed-command-*` lines in any `log_summary`. Several summaries (40007293665, 40007334421) state that no `AI_MEMORY_TELEMETRY` appeared. Retrieve hit rate, `estimated_tokens` against budget, and the `keyword_method` mix cannot be computed.
- **Flags:** no `fail_open: true`, no `enabled: false`, and no high push-retry counts were seen.
- **Recommendation:** once the logs are mounted, grep the slow review/autofix runs (39996936194, 39992707066) and the implement runs (39994951313, 39994624391) for `"op": "retrieve"`. If there are none, verify that retrieve emission is wired into those phases.

## GH API Call Audit

| Location | Pattern | Volume / evidence | Assessment |
|---|---|---|---|
| `lint_pr_body_auto_close` (40007334137, 40007293489) | One `gh api …/pulls/N/commits?per_page=100 --paginate` | 4 and 31 commits, 1 page each, no 429s | Efficient; no change |
| Review sweep 40007996422 | Open-PR snapshot, then one `gh workflow run` per dispatched PR | 8 candidates, 3 dispatches; `ci_cancelled_rerun.py` took about 6s, all 8 decisions `skip` | Dispatches are necessary per-item writes. Adding `head_sha` to the log reuses the existing projection, so 0 new calls |
| Shared PAT | `GH_PAT_BUDGET phase=end … remaining=2947` at about 19:02 | `used_in_job=unknown` | If the core limit is 5,000/h, about 41% was used in the window (inference). Add `GH_PAT_BUDGET_DIAG` (Reliability #7) |
| Collector `list_jobs_for_run` (`collect_workflow_logs.py:719-727`) | The page loop stops only on an empty page | Called for each failed or cancelled run (16 here) | One wasted request per run. Fix: `if len(page_jobs) < per_page: break` after `extend`, saving about 16 calls per collection with no risk |
| Collector run listing | The 1,000-run cap is filled by skipped fan-out runs | 797 of 1,000 rows skipped | Count skipped runs without letting them use up the cap, for example by filtering on `status`/conclusion when listing (inference: check that the extra list query costs fewer calls than it saves) |

No HTTP 429s, secondary rate limits or retry storms appear in any `log_summary`. Against §14, I saw no new unbatched per-item loops. The poller's batched helpers were not exercised in any visible evidence.

## Prompt Cache & Memory System

- **Aggregate:** 107,826,373 tokens read from cache, 3,653,034 written, 11,565,708 uncached prompt tokens, so `cache_hit_rate` = 0.876.
- **Completion tokens:** 572,269, about 0.46% of the 123.6M total. Cost is input-dominated.
- **Uncached input per call:** about 156k tokens (11.57M / 74 calls). Total input per call in 39996936194 is about 2.6M tokens (57.5M / 22), consistent with multi-turn agent sessions counted as one call.
- **Fragmentation:** the 0.639 outlier (39991593165, 10 calls) writes about 42k tokens to cache per call and reads only about 244k back. The likely cause is short-session amortization or prefix variance (inference). The `PROMPT_PREFIX_FINGERPRINT` diagnostic (Cost #2) will tell which.
- **No `CONTEXT_BUDGET_WARN`:** prompt growth is not yet eroding cache value.
- **Fail-open:** `or_usage_unavailable_calls` = 0, so every metered call reported its usage.
- **Memory retrieval:** cannot be evaluated (see AI Memory Health).
- **Expected impact:** bringing low-reuse runs to about 0.85 saves about 0.8M uncached-plus-write tokens per affected run, with no reliability risk.

## Orchestrator Health

- **Poller:** 17 runs, 7 succeeded, 10 cancelled pending runs (normal for a single-pending concurrency group). The cycle time (p50 440s, p95 1,242s) is longer than the 5-minute cron.
  - Indicators to track: `poll_cycle_seconds` p50/p95 against 300s, and pending-cancel count per hour.
- **Project #7913:** integration PR #7922 (`orchestrator/project-7913`) reports `5/10 sub-issues on #7913 still unchecked`: p1c-validate-process-owner-routing, p1d-validate-yml-heal-report-job, p4-validation-cycle-derivation, p2c-poller-hold-limit-judge-request-docs and p3b-unblock-judge-heal-report-exec.
  - Sub-issue PR #8005 (issue #7994) merged into the project branch at about 18:55, and lineage was finalized.
  - The readiness gate is holding correctly: there is no `ai:override-incomplete-merge` label.
  - PR #7922 uses `Refs #7913`, so §21 auto-close discipline was followed.
- **Clarification loop:** E2E negative-test issue #8012 posted a Q1–Q3 batch. Clarify-respond 40007436510 then gate-skipped (`not_orchestrator_managed`, `issue_closed`) after waiting about 145s for a runner. The behaviour is correct; the only cost is the queue wait.
- **Heal pipeline:** 75 `workflow_failure_heal` runs, all skipped (0 succeeded). 51 intake runs: 8 succeeded, 43 skipped. `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL … candidates=0 dispatched=0`. No heal was dispatched for the 4 CI failures. These were PR-branch CI failures, so this is probably intended scope (inference).
- **Merge train:** `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` (40007284890). It is idle and healthy.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact:

1. **Queueing:** hosted-runner saturation. Its share of the light-workflow wait is measured (60–98% of run duration in the 9 summarized runs). Its share of CI is inferred. Fix: Speed #1.
2. **Review/autofix compute:** p50 2,149s, p95 3,411s, max 4,700s (39996936194, PR #7986). This is the longest loop stage, and it repeats per PR (Cost #1). Fix: `head_sha` diagnostics, then a re-review gate.
3. **CI compute plus retries:** p50 2,146s, 14.3% failure rate, 1 manual or automatic re-attempt (40001124873). Fix: Reliability #1–#2 instrumentation.
4. **Orchestrate/validate:** poll cycle longer than cron (Speed #2). Waves advance only once per completed poll.
5. **Clarify → plan → implement:** mostly skipped fan-out; 157–167 skips per family against 7–11 successes. The successful runs are moderate: plan max 1,204s (39992495973), implement max 1,937s (39994951313), clarify max 845s (39994015747). This stage is not the bottleneck; its main side effect is filling the collector cap and adding gate jobs to the queue.
6. **Merge/conflict:** no conflict-heal or resolver activity is visible in this window.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Hosted-runner queueing: 38s to about 7 min for jobs doing ≤8s of work.
  - Review/autofix wall-clock: p50 about 36 min, max 78 min.
  - Poll cycle longer than its 5-minute cron.
- **Top failure modes:**
  - CI heal-contract step (2 failures).
  - Sharded poll unit-test group 1 (1 failure).
  - Inventory parity (1 failure).
  - Overall failure rate 0.4%; CI 14.3%.
- **Highest-cost drivers:**
  - Review/autofix: 123.6M tokens across just 4 metered runs.
  - Repeated reviews of #7959, #7989 and #7990.
  - About 90% of review runs are unmetered.
- **Top 3 actions:**
  1. Add `head_sha` to `AUTOFIX_SWEEP_DISPATCH` (`review_autofix_sweep.yml:544`; no API cost).
  2. Add per-job `queue_wait_seconds` to the collector, then decide on a PR-scoped CI `concurrency` group.
  3. Add `CI_STEP_RESULT` and `--durations` to the heal-contract and poll-unit shard steps, to separate timeouts from regressions.

## Metrics Appendix

**Run outcomes by workflow family**

| Family | Runs | Success | Failure | Cancelled | Skipped/other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| ci | 28 | 24 | 4 | 0 | 0 | 2,146.5 | 3,942.0 |
| review_autofix | 43 | 42 | 0 | 0 | 1 | 2,149.0 | 3,410.7 |
| orchestrate_poll | 17 | 7 | 0 | 10 | 0 | 440.0 | 1,241.8 |
| clarify | 178 | 11 | 0 | 0 | 167 | 1.0 | 427.5 |
| plan | 167 | 10 | 0 | 0 | 157 | 1.0 | 83.5 |
| implement | 164 | 7 | 0 | 0 | 157 | 1.0 | 11.0 |
| orchestrate_clarify_respond | 166 | 2 | 0 | 0 | 164 | 1.0 | 10.0 |
| workflow_failure_heal | 75 | 0 | 0 | 0 | 75 | 1.0 | 11.0 |
| workflow_failure_heal_intake | 51 | 8 | 0 | 0 | 43 | 1.0 | 506.0 |
| check_failure_triage | 42 | 9 | 0 | 0 | 33 | 1.0 | 526.8 |
| integration_pr_readiness | 19 | 19 | 0 | 0 | 0 | 169.0 | 371.5 |
| lint_pr_body_auto_close | 20 | 20 | 0 | 0 | 0 | 166.5 | 430.1 |
| cancel_on_pr_close | 17 | 15 | 0 | 2 | 0 | 187.0 | 452.4 |
| issue_pr_status | 8 | 8 | 0 | 0 | 0 | 273.5 | 513.4 |
| promote_candidate_follow | 3 | 3 | 0 | 0 | 0 | 110.0 | 147.8 |
| auto_release_stable | 1 | 1 | 0 | 0 | 0 | 105.0 | 105.0 |
| check_failure_triage_historical | 1 | 1 | 0 | 0 | 0 | 303.0 | 303.0 |
| **Total** | **1,000** | **187** | **4 (0.4%)** | **12** | **797** | **1.0** | **1,709.0** |

**Failed runs**

| Run | Job | Step | Duration (s) | Semble contract-test fallbacks |
|---|---|---|---|---|
| 39990854601 | tests-heal-plan-and-validation | Workflow failure heal contract and driver tests | 2,349 | 4 |
| 39992706892 | tests-release-and-log-analysis | Inventory parity | 1,883 | 4 |
| 39996848455 | orchestrate-poll (1) | Orchestrate poll process unit tests | 2,167 | 4 |
| 39999013651 | tests-heal-plan-and-validation | Workflow failure heal contract and driver tests | 2,309 | 8 |

**Tokens and cache (OpenRouter-metered; review_autofix only)**

| Metric | Value |
|---|---|
| Model calls (`or_calls`) / usage unavailable | 74 / 0 |
| Prompt (uncached) | 11,565,708 |
| Cache read / cache write | 107,826,373 / 3,653,034 |
| Completion | 572,269 |
| Total | 123,617,384 |
| `cache_hit_rate` | 0.876316 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo, 114 samples) | 1,000 / 4,473,840 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix, 9 samples) | 3,275,000 / 4,683,280 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (ci, 8 samples) | 2,329,000 / 4,343,950 |

**Review runs with telemetry**

| Run | Duration (s) | Model calls | Total tokens | Cache hit rate | Semble queries / bytes / sources | Semble bootstraps / ms |
|---|---|---|---|---|---|---|
| 39996936194 (pr:7986) | 4,700 | 22 | 57,770,864 | 0.921 | 2 / 28,204 / 16 | 2 / 102,918 |
| 39992707066 | 4,491 | 20 | 29,744,966 | 0.854 | 4 / 51,168 / 24 | 2 / 96,184 |
| 39992698001 | 3,411 | 0 | — | — | 2 / 20,866 / 4 | 2 / 96,360 |
| 39998244730 | 3,408 | 22 | 32,267,746 | 0.845 | 2 / 32,302 / 22 | 2 / 92,774 |
| 39993724109 | 3,275 | 0 | — | — | 2 / 19,336 / 4 | 2 / 92,508 |
| 39991597718 (pr:7959) | 3,270 | 0 | — | — | 1 / 5,028 / 3 | 1 / 47,466 |
| 39991593165 (pr:7989) | 3,234 | 10 | 3,833,808 | 0.639 | 1 / 16,805 / 5 | 1 / 31,631 |

**Semble totals**

| Metric | Value |
|---|---|
| Queries / bytes / average bytes per query | 14 / 173,709 / 12,408 |
| Sources / static-duplicate bytes / echo lines dropped | 78 / 0 / 0 |
| Fallbacks: total / contract-test / runtime | 32 / 32 / 0 |
| Bootstraps: total / failed / unused | 12 / 0 / 0 |
| Bootstrap ms: total / average | 559,841 / 46,653 |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | Queries | Response bytes | Tool calls | Fallbacks |
|---|---|---|---|---|---|---|---|
| serena (all) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

- Semble emits no probe prefix, so its availability is taken from bootstraps: 12 OK, 0 failed.
- Other MCP servers observed: none.

**GH API signals**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` at end of sweep 40007996422 | remaining=2947, `used_in_job=unknown` |
| HTTP 429 / secondary rate-limit events seen | 0 |
| Lint commit-list calls per run | 1 (paginated) |
| Sweep dispatches / candidates | 3 / 8 |

**Data gaps**
- The run-logs directory was not mounted.
- The 1,000-run window covers about 2h13m.
- Review usage is metered for only 4 of 43 runs, and `claude_*` counters are missing from the context.
- The context has no per-job queue or wait fields.
- No AI-memory `retrieve` telemetry was found.

Next collection steps: mount the logs directory, add `queue_wait_seconds`, surface the `claude_*` counters, and exclude skipped runs from the 1,000-run cap.