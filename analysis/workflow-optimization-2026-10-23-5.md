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
## Deep Audit — Workflows & Scripts (2026-10-23)

Scope and method: I read every file cited below directly in the repository. I did not run `git`, `wc` or `shellcheck`. The files audited in depth are:

- **Scripts:** `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh` (lines 1–1444), `scripts/label_helpers.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/implement_diagnose_post_codex_failure.sh` (lines 780–935), `scripts/emit_event.sh`, `scripts/nag_reminder.sh`, `scripts/mark-stable.sh`, `scripts/implement_stale_branch_check.sh`.
- **Hot spots in `scripts/orchestrate_poll_process.sh`:** lines 1–130 and 7900–7990.
- **Workflows read in full:** `.github/workflows/review_autofix_sweep.yml`, `cancel_on_pr_close.yml`, `workflow_failure_heal.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `internal-review.yml`, `review_rb_judge_dispatch.yml`, `unblock_judge_dispatch.yml`, `internal-orchestrate-poll.yml`.
- **Workflows sampled:** the large run blocks of `test-and-mark-stable.yml`, `implement.yml`, `review_autofix.yml` and `plan.yml`.

The existing report sections of `analysis/workflow-optimization-2026-10-23-5.md` have no finding IDs. Where they overlap, I cross-reference by section name. I did not repeat the `AUTOFIX_SWEEP_DISPATCH` `head_sha` item (Cost Optimizations #1), the `ci.yml` concurrency item (Speed Optimizations #1), or the collector pagination item (GH API Call Audit).

### Section 1: Bug & Correctness Sweep

**Checked and clean.** I found no workflow that writes untrusted event text straight into a `run:` body. Every issue/PR title, body, comment body and head ref I grepped is passed through `env:` first. Examples:

- `orchestrate_clarify_respond.yml:141` and `:675`
- `clarify.yml:679` and `:2199`
- `plan.yml:720` and `:1270`
- `issue_pr_status.yml:208-213` and `:1232-1234`
- `lint-pr-body-auto-close.yml:60-61`
- `integration-pr-readiness.yml:70`
- `cancel_on_pr_close.yml:21`

**SEC-001** · `scripts/tg_helpers.sh:396-440, 465-511` · **Critical** · `security`
- **Description:** `tg_cleanup_phase_msgs` and `tg_cleanup_msgs` turn untrusted issue comments into a DELETE URL that runs with the PAT.
  - **How it happens:** each function builds TSV records with `jq -r '... | "\(.id)\t\(.body)"'` (`:413`, `:482`). In `-r` mode jq prints the comment body's raw newlines. The records are then split per line by `while IFS=$'\t' read -r comment_id comment_body` (`:416`, `:485`).
  - **What gets deleted:** every line of a matching comment becomes its own record. Its first field becomes `comment_id`. The only check is `[ -n "${comment_id}" ]`. The value then goes into `curl -s -X DELETE -H "Authorization: token ${GH_TOKEN}" "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}"` (`:430-434`, `:501-505`).
  - **No author filter:** selection is body-only, `contains($m)` (`:413`) or `test("<!-- tg_(cleanup|phase):")` (`:482`). Any commenter whose comment includes the marker string can supply follow-on lines.
  - **Path traversal:** a follow-on line such as `../../../git/refs/tags/stable` would be sent through curl. Curl merges `/../` sequences by default unless `--path-as-is` is given (curl documentation). The DELETE would then hit another endpoint of the repository with the caller's token.
  - **Telegram side effect:** the same body controls `id_list` (`:419-420`, `:489-490`), which is passed to `tg_delete_msg`. A commenter can therefore have arbitrary admin-chat message IDs deleted.
  - **Collateral damage:** any human comment that quotes the marker is deleted too.
  - **Callers that run with `GH_TOKEN`:** `scripts/orchestrate_poll_process.sh:5916`, `:15200`, `:15307`, `:24451`, `:28935`; `.github/workflows/implement.yml:2533`; `plan.yml:1231`; `issue_pr_status.yml:1036` and `:1043`. The DELETE runs even when no Telegram credentials are set; only `GH_TOKEN` and `GITHUB_REPOSITORY` are required (`:386`, `:455`).
  - [NEEDS VERIFICATION] I did not test end-to-end which traversed endpoints the token can actually reach. The missing ID validation and missing author filter are confirmed in the source.
- **Recommended fix:**
  - Select only `.id` from trusted authors and validate it before building the URL.
  - Use the trusted-author filter that `scripts/orchestrate_poll_process.sh` already uses: `.user.login == $login` from one `gh api user` read (`:51`), or `github-actions[bot]` plus `.user.type == "Bot"` (`:62`).
  - Read the body only for anchored marker extraction, and require `^<!-- tg_(cleanup|phase:[a-z_]+):[0-9,]+ -->$`.

  ```bash
  tg_cleanup_trusted_ids="$(printf '%s' "${comments_json}" | jq -r --arg m "${marker}" --arg login "${tg_cleanup_trusted_login}" \
  	'.[] | select((.user.login // "") == $login and ((.body // "") | contains($m))) | .id' 2>/dev/null)" || tg_cleanup_trusted_ids=""
  while IFS= read -r comment_id
  do
  	[[ "${comment_id}" =~ ^[1-9][0-9]*$ ]] || continue
  	# read this comment's body by id from comments_json, extract the anchored id list, then DELETE
  done <<< "${tg_cleanup_trusted_ids}"
  ```

**SEC-002** · `scripts/tg_helpers.sh:252-275, 320-343` · Medium · `security`
- **Description:** `tg_store_msg_id` and `tg_store_phase_msg_id` pick the last comment containing `<!-- tg_cleanup:` or `<!-- tg_phase:<phase>:` with no author check (`:253-256`, `:321-326`). They then PATCH that comment with the PAT (`:263-267`, `:332-336`). A user's comment containing the marker is rewritten by the bot, keeping the user's text and appending a bot message ID. Its user-planted IDs are then fed to `tg_delete_msg` by the cleanup in SEC-001.
- **Recommended fix:** apply the same trusted-author predicate as SEC-001 to both `last` selections. If no trusted tracking comment exists, POST a new one, as the `else` branches already do.

**SEC-003** · `.github/workflows/lint-pr-body-auto-close.yml:49-56, 112-115`; `.github/workflows/integration-pr-readiness.yml:35-40, 51-54, 75-79` · Medium · `security`
- **Description:** both required checks run the checking script from the PR's own checkout.
  - `actions/checkout@v5` with no `ref:` uses the PR merge ref. The scripts that run are `scripts/lint_pr_body_auto_close.py` and `scripts/check_integration_pr_readiness.py`.
  - `integration-pr-readiness.yml` holds `statuses: write` and posts the `orchestrator/integration-pr-not-ready` status. A sub-issue PR merged into `orchestrator/project-<N>` that changes `check_integration_pr_readiness.py` would therefore decide its own integration PR's readiness status.
  - The postmortem P1/P3 guards these workflows implement exist to prevent exactly this kind of drift (`lint-pr-body-auto-close.yml:2-24`, `integration-pr-readiness.yml:1-22`).
  - [NEEDS VERIFICATION] A `pull_request` run also takes the workflow file from the PR, so this mainly closes the script-only drift path. I could not see the branch-protection configuration.
- **Recommended fix:** check out the script from the trusted base commit, the same SHA-pinned helper pattern documented for the review gate (agents.md:2557-2563).

  ```yaml
  - name: Checkout trusted lint helper
    uses: actions/checkout@v5
    with:
      ref: ${{ github.event.pull_request.base.sha }}
      sparse-checkout: scripts
      persist-credentials: false
  ```

**BUG-001** · `scripts/tg_helpers.sh:396-440, 465-511` · Medium · `bug`
- **Description:** both cleanup loops DELETE comments while paging through comments with `page=1,2,…` (`:398-401`, `:467-470`; page increments at `:439`, `:510`). Each deletion shifts later comments one slot toward earlier pages. Comments that move into an already-read page are never seen. Tracking comments beyond the first deleted ones can therefore survive. The file already documents a related pagination bug: 447 leftover `tg_cleanup` comments (`:195-197`).
- **Recommended fix:** collect all matching comment IDs across every page first, then delete. Alternatively, walk from the last page backwards, using the `last_page` arithmetic in `_tg_newest_comments_json` (`:213`).

**BUG-002** · `.github/workflows/cancel_on_pr_close.yml:155-189, 298-305, 331-341` · Medium · `bug`
- **Description:** two kinds of active run are never cancelled when a PR closes.
  - **Pending runs are not fetched.** `_cpr_fetch_active_runs` fetches only `status=queued` and `status=in_progress`, both with `event=pull_request`. The jq filters keep only `queued`/`in_progress` (`:300`) or `event == "pull_request"` (`:335`).
    - A review run held by a `cancel-in-progress: false` concurrency group reports `status=pending`.
    - `review_autofix_sweep.yml:322-330` documents this state from the PR #3895 incident and fetches `pending` for that reason (`:337`).
  - **Sweep-dispatched reviews do not match.** The sweep dispatches reviews by `workflow_dispatch` from the default branch (`review_autofix_sweep.yml:540-543`). Those runs are named `… [pr:<N>]` (`internal-review.yml:8`), have the default branch as `head_branch`, and carry no `pull_requests` linkage. Neither the head-ref filter (`:332-334`) nor the schedule-mode linkage filter (`:306-312`) matches them.
  - **Impact:** dispatched or pending reviews of a closed PR keep the runner. The existing report puts a review at about 35–55 minutes of runner time (Cost Optimizations #1).
  - [NEEDS VERIFICATION] `gh_review_pr_state` (`scripts/gh_helpers.sh:79-118`) suggests the reviewer/editor watchdogs may exit early on a closed PR. I did not confirm when that check runs.
- **Recommended fix:**
  - Add a `status=pending` fetch (+1 call per close).
  - Add one `actions/workflows/internal-review.yml/runs?event=workflow_dispatch&status=…` read, and match `display_title` with the sweep's `dispatch_pr` jq (`review_autofix_sweep.yml:376-385`), so `[pr:<N>]` runs are cancelled on close.

**BUG-003** · `scripts/comprehensive_test_and_release_gh_api.sh:151-155`; `.github/workflows/test-and-mark-stable.yml:1492-1508, 1512-1513, 1543-1549` · Medium · `bug`
- **Description:** `gh_api_safe` prints `echo "::error::gh api call failed: $*"` to stdout, not stderr (`:153`). `gh_api_safe_print` callers capture stdout.
  - Phase 3b runs `PR_NUMBER=$(gh_api_safe_print "repos/…/pulls?state=open&head=…" --jq '.[0].number // empty' || echo "")`. On a non-retryable failure that leaves stderr non-empty, `PR_NUMBER` becomes the `::error::…` text.
  - Line `:1501` (`if [ -n "$PR_NUMBER" ]`) then reports "✓ Implementation PR #::error::…" and writes `pr_number=::error::…` to `$GITHUB_OUTPUT` (`:1506`). The error annotation itself is swallowed by the capture.
  - The same applies to `LABELS` (`:1512`), `IMPL_STEP_NAME` (`:1543`) and `COMMIT_COUNT` (`:1548`).
  - In-repo comments already work around this by shape-checking pages (`test-and-mark-stable.yml:1034-1036`), but these four captures do no shape check.
- **Recommended fix:** change `:153` to `echo "::error::gh api call failed: $*" >&2`. Keep the existing shape checks. Also validate `PR_NUMBER` with `[[ "${PR_NUMBER}" =~ ^[1-9][0-9]*$ ]]` before `:1501`.

**BUG-004** · `scripts/implement_diagnose_post_codex_failure.sh:800-859`; `.github/workflows/implement.yml:6683-6684` · Medium · `bug`
- **Description:** creating fix-up issues is not idempotent.
  - The step runs on every `failure() || cancelled()` implement run (`implement.yml:6684`). The loop creates up to 10 issues with `gh issue create` (`:830-835`) and has no dedup marker. The only marker written is the summary comment's `IMPLEMENT_FIXUP_BLOCKERS_V1` (`:897`), and nothing checks it before creating.
  - On partial failure (`creation_failed=1`) the script exits at `:858` without recording the issues it already created. The summary/blocker comment (`:906`) is skipped, so a later failed run creates them again.
- **Recommended fix:**
  - Put `<!-- ai:implement-fixup:v1 source=<ISSUE_NUMBER> local_id=<FIX_ID> -->` in each body.
  - Before creating, run one search for open issues carrying `source=<ISSUE_NUMBER>` and skip local IDs already present.
  - On partial failure, still post the summary listing the issue numbers that were created.

**BUG-005** · `.github/workflows/review_autofix_sweep.yml:213, 228-239, 590-609` · Medium · `bug`
- **Description:** the open-PR listing that every later decision depends on uses raw `gh api --paginate` under `set -euo pipefail` (`:213`, `:228`). The run snapshot in the same step uses the classified `gh_api_retry` (`:340`).
  - A single transient failure fails the step before the snapshot file is written (`:239`).
  - "Re-run cancelled CI failed jobs once" (`:590-598`) and "Re-dispatch stale security holds" (`:600-609`) have no `!cancelled()`/`always()` guard. They are skipped for that tick.
- **Recommended fix:** list with `gh_api_retry --paginate`. On failure, write `[]` to the snapshot, print `AUTOFIX_SWEEP_SKIPPED reason=pr_list_unavailable` (the existing `AUTOFIX_SWEEP_SKIPPED` prefix at `:287`) and `exit 0`. Alternatively, add `if: ${{ !cancelled() }}` to the CI-rerun step.

**BUG-006** · `scripts/label_helpers.sh:195`; `scripts/implement_diagnose_post_codex_failure.sh:845-846` · Low · `bug`
- **Description:** raw, multi-line `gh` stderr (`${_label_err}`, `${CREATE_ERR_MSG}`) is placed directly into `::warning::` lines. The text after the first newline is not part of the annotation and can carry workflow-command syntax. The same file neutralises this at `:291-292`, and `_gh_actions_escape` exists for the purpose (`scripts/gh_helpers.sh:157-164`).
- **Recommended fix:** wrap both values in `$(_gh_actions_escape "…")`.

**SEC-004** · `scripts/gh_helpers.sh:1100, 1117-1120, 1130, 1132, 1185, 1215` · Low · `security`
- **Description:** `gh_retry` and `gh_retry_to_file` print the full argv (`$*`) unescaped inside `::warning::`, `::notice::` and `::error::` lines. For calls such as `gh_retry gh api …/comments -f body="${BODY}"` (for example `.github/workflows/review_autofix.yml:5034`), the whole comment body, which may be model-generated, ends up in the logs. A multi-line body also breaks out of the annotation.
  - [NEEDS VERIFICATION] I did not confirm that the runner processes workflow commands on stderr lines.
- **Recommended fix:** log only the endpoint through `_gh_api_endpoint` plus `_gh_actions_escape`, as `gh_api_retry` already does (`:907`).

### Section 2: GitHub API Call Redundancy Audit

The per-item writes are necessary and are not findings: sweep dispatch (`review_autofix_sweep.yml:543`), run cancellation (`cancel_on_pr_close.yml:370-379`), and reclarify replay POSTs (`orchestrate_poll_process.sh:94-98`).

**BATCH-001** · `.github/workflows/test-and-mark-stable.yml:618, 1486-1626` · Medium · `api-batching`
- **Description:** the Phase 3b wait loop runs every `POLL_INTERVAL: 10` seconds (`:618`) on `GH_PAT`.
  - Each tick issues up to 4 REST GETs: `pulls?state=open&head=…` (`:1492`), `issues/${ISSUE_NUMBER}` (`:1512`), `actions/runs/${IMPL_RUN_ID_LATEST}/jobs` (`:1543`) and `commits?sha=ai/issue-N` (`:1548`).
  - Every 30 seconds it also runs the progress-counter walk of up to `SCOPED_RUN_LOOKUP_MAX_PAGES=10` pages (`:1445-1480`, `:1525-1527`).
  - **Current:** about 18–24 calls per minute, about 1,000–1,400 per 60-minute phase (inference from the interval). That is a large share of a 5,000/hour PAT budget the existing report already sees at 41% (GH API Call Audit).
- **Recommended fix:** use one aliased GraphQL read per tick: `repository { pullRequests(headRefName:"ai/issue-N", states:OPEN, first:1){nodes{number}} issue(number:N){labels(first:50){nodes{name}}} ref(qualifiedName:"refs/heads/ai/issue-N"){target{oid}} }`. Keep the jobs read separate.
  - **Proposed:** 2 calls per tick (−50%).
  - Pattern to extend: `_fetch_blocked_issue_routing_graphql` (`scripts/orchestrate_poll_process.sh:102-130`).
  - [NEEDS VERIFICATION] Phases 2 and 4 probably poll at the same cadence.

**API-001** · `scripts/orchestrate_poll_process.sh:46-57` · Medium · `api-redundancy`
- **Description:** `replay_failed_reclarify_commands` lists `--state open --label ai:reclarify-requeue --json number`. For each listed issue it then re-reads `GET issues/<N>` (`:55`) only to re-check `state == "open"`, `pull_request == null` and the label (`:56-57`). The listing returned exactly those facts moments earlier, and `gh issue list` excludes PRs.
  - **Current:** 1 list + N issue GETs + N comment listings.
  - **Proposed:** 1 list (add `state,labels` to `--json`) + N comment listings.
  - [NEEDS VERIFICATION] I could not confirm whether the re-read is a deliberate narrowing of the time-of-check/time-of-use window. If so, keep it only for issues whose comments pass the marker checks, so the GET moves after `:73`.
- **Recommended fix:** extend `--json number` to `--json number,state,labels` and drop `:55-57`, or move them after `:73`.

**BATCH-002** · `.github/workflows/review_autofix_sweep.yml:337-348, 437-439` · Low · `api-batching`
- **Description:** the active-run snapshot runs 3 statuses × 2 workflows = 6 paginated listings per tick.
- **Recommended fix:** use repo-level `actions/runs?status=<s>` (3 listings) and filter `.path` for both workflow files in the existing jq reduce (`:350-415`). **Current:** 6 calls (more with pages). **Proposed:** 3.
  - [NEEDS VERIFICATION] Repo-level pages also contain other workflows' runs, so check the page counts before switching.

**API-002** · `.github/workflows/review_autofix_sweep.yml:285` · Low · `api-redundancy`
- **Description:** `gh api "repos/${REPOSITORY}" --jq .default_branch` runs every tick. The same value is already read from the event at `:147` (`github.event.repository.default_branch`).
- **Recommended fix:** pass `SWEEP_EVENT_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}` via `env:` and call the API only when it is empty. Saves 1 call per tick.
  - [NEEDS VERIFICATION] The `:147` fallback to `github.ref_name` suggests the payload may lack it on `schedule` events.

**API-003** · `scripts/orchestrate_poll_process.sh:7943-7948` · Low · `api-batching`
- **Description:** `security_pass_unblock_filed_advisory_followups` does one `GET issues/<n>` per row for state and labels. The in-code audit comment (`:7918-7922`) justifies the per-row read, but state and labels batch cleanly.
- **Recommended fix:** use an aliased GraphQL `issue(number:){state labels(first:100){nodes{name}}}` in batches of 50. Keep the comments read only for open `ai:blocked` rows. **Current:** N + B. **Proposed:** ceil(N/50) + B.

**API-004** · `scripts/gh_helpers.sh:1365-1398`; `.github/workflows/review_autofix.yml:1165-1187, 1338, 1375` · Low · `api-redundancy` (tight retry)
- **Description:** two retry paths do not classify failures.
  - `curl_gh_api` retries every non-2xx response, including 401, 404 and 422, up to 5 times. It also has no single-attempt rule for POST creates, unlike `gh_retry`/`gh_api_retry` (`:1062-1069`, `:931-933`, issue #6634).
  - The inline `gh_retry` in the deterministic-skip job retries any failure 4 times with 2/4/8-second waits. It is used for `gh pr merge --auto` (`:1338`, `:1375`), so a permanent 422 is retried 4 times.
- **Recommended fix:** in `curl_gh_api`, return immediately on 4xx other than 403/429 and make one attempt for `-X POST`. Replace the inline review_autofix copy with the SHA-verified `gh_helpers.sh` already resolved via `GH_API_RETRY_HELPERS` (`review_autofix.yml:376`, `:403`).

**BATCH-003** · `scripts/implement_diagnose_post_codex_failure.sh:862-883` · Low · `api-batching`
- **Description:** after creating up to 10 fix-up issues, a second loop posts up to 10 "Dependency Notes" comments.
- **Recommended fix:** create the issues in dependency order and add the notes to each issue body at creation, since `depends_on` normally points at earlier IDs. Fall back to a comment only for forward references. **Current:** up to 20 calls. **Proposed:** up to 10.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** · `.github/workflows/{cancel_on_pr_close.yml:25-67, workflow_failure_heal.yml:101-142, implement.yml (4 copies), plan.yml (2), clarify.yml (2), orchestrate.yml (2), orchestrate_poll.yml (2), check_failure_triage.yml, memory_maintenance.yml, orchestrate_clarify_respond.yml}` · Medium · `duplication`
- **Description:** the "Resolve workflow support ref" step appears 17 times across 10 workflows. I counted 17 matches of `promote_candidate_workflow_ref="$(jq`. The copies are about 40 lines each and differ only in the workflow filename in the regex. `implement.yml:751` already drifted: it hardcodes `"shubhodeep1/coding-workflows"` instead of `${wf_source}`.
  - This step runs before support code is checked out, so it cannot be moved into a staged script, and `uses:` cannot take a dynamic ref for a composite action.
- **Recommended fix:** keep the copies inline but generate them. Add a contract test that extracts each copy and asserts it is byte-identical to one canonical template after substituting the filename. Extend `tests/test_workflow_support_ref_follows_pin.py`.
  - [NEEDS VERIFICATION] I did not check whether that test already asserts parity.

**DUP-002** · `.github/workflows/implement.yml:5441-5443, 5488-5490, 5575-5577, 5626-5628, 6529-6531, 6581-6583, 6758-6760, 6799-6801`; `.github/workflows/review_autofix.yml:1189-1192, 1606-1611, 4440-4448`; `.github/workflows/workflow-log-analysis.yml:1079, 1882, 2482` · Medium · `duplication`
- **Description:** check-and-create label code is repeated 14 times instead of calling `ensure_label_exists` from `scripts/label_helpers.sh:163-197`.
  - `workflow-log-analysis.yml` defines an identical `_wla_label_create` one-liner 3 times.
  - `review_autofix.yml:1189-1192` defines its own `ensure_label_exists` with a 4-argument signature (label, repo, color, desc). That collides with the helper's `(label, [repo])` signature. A later `source label_helpers.sh` in that shell silently replaces it and drops the colors.
  - `:1606` guards with `if ! type ensure_label_exists`, so the two definitions behave differently by call site.
- **Recommended fix:** have the callers `source "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/label_helpers.sh"` and call `ensure_label_exists "<label>" "${GITHUB_REPOSITORY}"`, as `implement.yml:5622-5624` already does. Rename the 4-argument local helper in review_autofix to a distinct name (`review_gate_label_create`) instead of shadowing.

**DUP-003** · `.github/workflows/*` (61 `gh_retry() { "$@"; }` stubs in 12 files; 26 `gh_api_retry() { while …; gh api "$@"; }` stubs in 13 files); `.github/workflows/review_autofix.yml:1165-1187`; `scripts/comprehensive_test_and_release_gh_api.sh:93-160` · Low · `duplication`
- **Description:** when `source …gh_helpers.sh 2>/dev/null || true` fails, the stubs fall back to no retry and log nothing (for example `implement.yml:5408-5409`). `review_autofix.yml:1165-1187` is a third, full retry implementation, and `gh_api_safe` is a fourth. The fourth is documented as intentional (`:93-96`).
- **Recommended fix:** keep the stubs, which the contract tests rely on (`tests/test_workflow_gh_retry_fallback_contract.py`). Make each stub print one `::warning::GH_RETRY_FALLBACK step=<name>` line when installed, so silent loss of retries becomes visible.

**DUP-004** · `scripts/tg_helpers.sh:227-276 / 296-344` and `380-441 / 449-512` · Low · `duplication`
- **Description:** two pairs of near-identical functions. Store/append-tracking: `tg_store_msg_id` and `tg_store_phase_msg_id`. Paginate-and-delete: `tg_cleanup_phase_msgs` and `tg_cleanup_msgs`. Each pair differs only in the marker.
- **Recommended fix:** create `_tg_tracking_upsert <issue> <marker_prefix> <msg_id>` and `_tg_tracking_cleanup <issue> <jq_marker_filter> <sed_marker_re>` in the same file, and make the four public functions thin wrappers. This also puts the SEC-001, SEC-002 and BUG-001 fixes in one place.

**DUP-005** · `.github/workflows/test-and-mark-stable.yml:1361-1368` vs `1455-1462` · Low · `duplication`
- **Description:** the jq validator for implement-run pages (status/conclusion enums plus id shape) is copied verbatim into `scoped_implement_runs_complete` and `scoped_implement_progress_counts`.
- **Recommended fix:** move it into `scripts/comprehensive_test_and_release_gh_api.sh` as `scoped_runs_page_summary <title> <name_re>` (unique name; none exists today). Both loops call it, which also shrinks the Phase 3b body (EXPR-002).

### Section 4: Expression Size Limit Risk Assessment

**Calibration.** The repo's own comments put the Phase 4 body (`test-and-mark-stable.yml:1954-2700`, 747 lines) at about 25k characters (`:1951`) and the Phase 4b body (`:2786-3440`, 655 lines) at about 21.3k (`:2783`). That is about 33 characters per line after YAML removes the indentation. The CI guard (`tests/test_workflow_file_size_limit.py:84-90`) fails any interpolated `run:` body of 18,000 characters or more. **No interpolated body can currently be at or above the 18,000-character High threshold if CI is green.** The estimates below are inferences from line counts and are not measured.

**EXPR-001** · `tests/test_workflow_file_size_limit.py:28-32, 84-90` · Medium · `expression-limit`
- **Description:** the guard measures the raw `len(run)`.
  - When GitHub converts a body containing `${{ }}` into one expression, the literal text likely becomes a `format('…')` string in which `'`, `{` and `}` are escaped by doubling [NEEDS VERIFICATION].
  - jq-heavy bodies (for example `:1361-1368`, `:1455-1462`) contain hundreds of these characters. Their effective length would then be larger than measured, and the guard's 3,000-character headroom (`:30`) would be partly used up without the test noticing.
- **Recommended fix:** in `test_interpolated_run_bodies_below_expression_guard`, measure `len(run) + run.count("'") + run.count("{") + run.count("}")`, which overestimates a little and so fails safe. Keep the 18,000 guard value. Raising it is forbidden by agents.md:2528-2530.

**EXPR-002** · `.github/workflows/test-and-mark-stable.yml:1303-1626` ("Phase 3b: Wait for PR creation") · Low · `expression-limit`
- **Description:** a body of about 323 lines with one inline expression at `:1341` (`IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"`). Estimated at about 11–14k characters, leaving about 7–10k of headroom [NEEDS VERIFICATION]. It has grown with each issue (#4723, #7968), it is dense jq, and EXPR-001 escaping would apply.
- **Recommended fix:** move the value to `env:` (`IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`). That is the same pattern as `:1948-1952`, and the body then no longer counts against the limit.

**EXPR-003** · `.github/workflows/test-and-mark-stable.yml:996-1266` ("Phase 2", inline at `:1014`, `:1026`, `:1044`) and `:842-974` ("Phase 1", inline at `:882`) · Low · `expression-limit`
- **Description:** Phase 2 is about 270 lines, estimated at about 9–11k characters, with three uses of `${{ steps.create-issue.outputs.created_after }}`. Phase 1 is estimated at about 4–5k.
- **Recommended fix:** add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to each step's `env:` and use `${CREATED_AFTER}`. Both bodies then no longer count against the limit.

**EXPR-004** · `.github/workflows/implement.yml:5406-5633` ("Handle no-op implementation"); `.github/workflows/plan.yml:2137-2343` ("Guard plans targeting template-owned paths", inline at `:2334`, `:2338`) · Low · `expression-limit`
- **Description:** the implement body is about 227 lines, estimated at about 8–9k characters. It uses `${{ github.repository }}`, `${{ github.server_url }}` and `${{ github.run_id }}` 9 times (`:5411`, `:5413`, `:5616`, `:5624`, `:5626`, `:5630`, `:5632`), even though it already sets `REPO=` at `:5413`. The plan body is about 200 lines, estimated at about 7k.
- **Recommended fix:** replace these with the runner-provided `${GITHUB_REPOSITORY}`, `${GITHUB_SERVER_URL}` and `${GITHUB_RUN_ID}`. No expression remains, so the bodies no longer count against the limit.

**Workflow file size.** The repo's documented limit is 512,000 bytes, with the CI guard at 480,000 and the `budget-watch` warning at 440,000 (`tests/test_workflow_file_size_limit.py:26-27`; agents.md:2495-2523). The largest files by line count are `test-and-mark-stable.yml` (7,875 lines; about 330 KB estimated), `implement.yml` (7,159; about 300 KB) and `review_autofix.yml` (5,755; about 260 KB) [NEEDS VERIFICATION with `wc -c`]. None can be at or above 480,000 bytes while CI is green, and none is near the audit prompt's 800 KB threshold. That threshold is itself wrong; see CONSIST-002.

### Section 5: Cross-Cutting Concerns

**TODO/FIXME/HACK markers:** none. Case-sensitive searches of `scripts/` and `.github/workflows/` for `TODO|FIXME|HACK` found no matches.

**DEAD-001** · `.github/workflows/review_autofix_sweep.yml:219`, `cancel_on_pr_close.yml:43`, `workflow_failure_heal.yml:118`, `orchestrate_poll.yml:135, 531`, `orchestrate.yml:214, 388`, `plan.yml:360, 3170`, `clarify.yml:393, 2361`, `check_failure_triage.yml:323` · Low · `dead-code`
- **Description:** each of these lines runs `source "${GH_API_RETRY_HELPERS:-/dev/null}"`, but nothing in these workflows sets `GH_API_RETRY_HELPERS`. It is set only in `validate.yml:167`, `review_autofix.yml:376`, `implement.yml:178`, `security-audit.yml:122` and `orchestrate_clarify_respond.yml:132`. `GITHUB_ENV` does not cross into called workflows. In these steps `gh_api_retry` is therefore always the raw `gh api` stub. For example, the promote-candidate protection read at `cancel_on_pr_close.yml:45` has no retry (it fails safe to `github.sha`). In the sweep, `:216-217` already sources the helper directly.
- **Recommended fix:** add the existing "Bootstrap GitHub API retry helper" step (`implement.yml:133`) to these jobs, or drop the dead `source` line where a direct `source scripts/gh_helpers.sh` exists. Update `tests/test_workflow_gh_retry_fallback_contract.py:406` accordingly.

**DEAD-002** · `scripts/gh_helpers.sh:91, 108` · Low · `dead-code`
- **Description:** `gh_review_pr_state` matches `open|closed|merged` on the REST `pulls/<n>` `.state` field. That field is only ever `open` or `closed`; a merge shows up in the separate `merged` boolean. The `merged` branch can never be reached, and callers cannot tell merged from closed.
- **Recommended fix:** use `--jq 'if .merged then "merged" else .state end'`. Callers keep treating anything other than `open` as terminal.

**CONSIST-001** · `.github/workflows/implement.yml:5441-5443, 5575-5577, 5488-5489, 6758-6759` vs `.github/ai/label_contract.v1.json:100-111, 156-158`; `scripts/label_helpers.sh:48-62, 113-127` · Medium · `consistency`
- **Description:** inline label creation disagrees with the label contract. `gh label create` only takes effect when the label does not exist yet, so a new consumer repo's label metadata depends on which workflow happens to run first.

  | Label | Inline value (`implement.yml`) | Contract value |
  |---|---|---|
  | `ai:closed` | `6b7280`, "Issue closed by automation (superseded, duplicated, or already complete)" | `6a737d`, "Linked PR closed without merge" |
  | `ai:needs-human` | `b60205` | `e11d48` |
  | `ai:blocked` | "Blocked: human input required" | "Orchestrator auto-answer loop blocked pending human intervention" |

- **Recommended fix:** replace these sites with `ensure_label_exists` (see DUP-002), so the contract catalog is the only source of label metadata.

**CONSIST-002** · `prompts/mode-workflow-audit.txt:100`; `prompts/_templates/mode-workflow-audit.txt:93` · Low · `consistency`
- **Description:** the audit prompt says the workflow file limit is 1 MB (1,048,576 characters) and asks to flag files above 800 KB. The repo's measured and enforced limit is 512,000 bytes, with the guard at 480,000 (`tests/test_workflow_file_size_limit.py:2-16, 26-27`; agents.md:2506-2523). At the prompt's thresholds, the audit would never flag a file that has already stopped running.
- **Recommended fix:** change both prompt lines to "512,000 bytes (500 KiB); flag any workflow at or above 440,000 bytes (the `CI_BUDGET_WORKFLOW_WARN_BYTES` default)".

**CONSIST-003** · `.github/workflows/review_rb_judge_dispatch.yml:46`; `.github/workflows/unblock_judge_dispatch.yml:32` vs `internal-review.yml:56` and 14 other `@promote-candidate` wrappers · Low · `consistency`
- **Description:** the review-blocked judge and the unblock judge call `review_autofix.yml@main` and `unblock_judge.yml@main`. Every other internal wrapper pins `@promote-candidate`. During a promote cycle, a judge-only run of a PR therefore runs a different `review_autofix.yml` revision than that PR's normal reviews. They share the `pr-autofix-<N>` group (`review_autofix.yml:1853`).
  - [NEEDS VERIFICATION] The split may be intentional.
- **Recommended fix:** pin both to `@promote-candidate`, or add a header comment explaining why `@main` is required.

**CONSIST-004** · `.github/workflows/review_autofix_sweep.yml:274, 288, 488-491, 552` · Low · `consistency`
- **Description:** `AUTOFIX_SWEEP_END` is printed in three formats.
  - `:288` omits `skipped_merge_queued` and `snapshot_incomplete`, which `:552` includes.
  - The `claude_capacity_wait` skip (`:488-491`) adds to no counter, so the END counters no longer add up to `candidates`.
- **Recommended fix:** add `skipped_capacity_wait` and print every field in every END line. New fields are appended only, so existing parsers keep matching.

**CONSIST-005** · `.github/workflows/review_autofix_sweep.yml:25-26` vs `internal-review.yml:49-51`; `review_autofix.yml:1853` · Low · `consistency`
- **Description:** the sweep header says "both concurrency groups are keyed by PR number". `internal-review.yml` keys `pull_request` runs by `github.head_ref` (`:50`). The real PR-number serialization is `review_autofix.yml`'s job group `pr-autofix-<N>`. The wrong comment makes future dedup changes riskier.
- **Recommended fix:** reword the comment to cite `review_autofix.yml:1853`.

**DEBT-001** · `scripts/mark-stable.sh:1-111` · Low · `tech-debt`
- **Description:** a standalone operator script: "Usage: ./scripts/mark-stable.sh v1.1.0" (`:6`). It force-pushes the `stable` and major tags (`:109-110`) and duplicates the release path of `mark-stable.yml`/`test-and-mark-stable.yml`. Completed plans still instruct "run `scripts/mark-stable.sh` after each PR" (`docs/completed/orchestrator-validation-resilience-plan.md:106`, `:289`). It has no entry in `docs/scripts-pending-removal.md`, contrary to CLAUDE.md §18.A/§18.F.
- **Recommended fix:** add a registry entry with type `long-running`, removal trigger "permanent — review annually", and a preflight check that the `mark-stable.yml` release job succeeded for the last release tag. Alternatively, remove the script in favour of the workflow.

**SHELL-001** · `scripts/label_helpers.sh:13` · Low · `shellcheck`
- **Description:** `LABEL_HELPERS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"` has no fallback. If the `cd` fails in a caller running `set -e`, sourcing the helper aborts the caller's step. `gh_helpers.sh:120` uses `|| echo "scripts"` for this, and `nag_reminder.sh:4` uses `|| printf '.'`.
- **Recommended fix:** append `2>/dev/null || printf '%s' "scripts"` inside the command substitution, mirroring `gh_helpers.sh:120`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 1 | SEC-001 |
| High | 0 | — |
| Medium | 13 | SEC-002, SEC-003, BUG-001, BUG-002, BUG-003, BUG-004, BUG-005, API-001, BATCH-001, DUP-001, DUP-002, CONSIST-001, EXPR-001 |
| Low | 20 | SEC-004, BUG-006, API-002, API-003, API-004, BATCH-002, BATCH-003, DUP-003, DUP-004, DUP-005, EXPR-002, EXPR-003, EXPR-004, DEAD-001, DEAD-002, CONSIST-002, CONSIST-003, CONSIST-004, CONSIST-005, DEBT-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/tg_helpers.sh` (SEC-001) | Small |
| API call optimization | `.github/workflows/test-and-mark-stable.yml`, `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/review_autofix_sweep.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/implement_diagnose_post_codex_failure.sh`, `scripts/gh_helpers.sh`, `.github/workflows/review_autofix.yml` | Medium |
| Code modularization | 10 workflows carrying "Resolve workflow support ref" plus `tests/test_workflow_support_ref_follows_pin.py`; `.github/workflows/implement.yml`, `review_autofix.yml`, `workflow-log-analysis.yml`; `scripts/tg_helpers.sh`, `scripts/label_helpers.sh` | Large |
| Expression size reduction | `tests/test_workflow_file_size_limit.py`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/plan.yml` | Small |
| Medium/Low fixes | `.github/workflows/cancel_on_pr_close.yml`, `review_autofix_sweep.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `review_rb_judge_dispatch.yml`, `unblock_judge_dispatch.yml`, the DEAD-001 workflows; `scripts/tg_helpers.sh`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/implement_diagnose_post_codex_failure.sh`, `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`; `prompts/mode-workflow-audit.txt`, `prompts/_templates/mode-workflow-audit.txt`; `docs/scripts-pending-removal.md` | Medium |