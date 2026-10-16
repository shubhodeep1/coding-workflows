## Executive Summary

> **Evidence gap:** the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox. Every finding below rests on `analysis_context.json` (metrics plus gpt-6-luna `log_summary` rows) and on repo source read in this run. Root causes for the two failures cannot be confirmed from raw logs. `insufficient_data` is `false`, but per-run logs only exist for successes sampled at 7% (`success_sample_rate: 0.07`), and most poll, plan and clarify logs were truncated by 146k–827k chars.

- **The orchestrator poller is running at roughly 100% duty cycle and holding up every event.** Each new poll run waits until the previous one finishes, to the second:
  - run 38896972053 was queued at 11:55:13 and started at 11:58:06; the previous poll, 38896051549, ended at 11:58:03;
  - run 38901192024 was queued at 12:32:10 and started at 12:34:57; the previous poll, 38900121136 (12:23:05 + 712s), ended at about 12:34:57.

  The logs call this "waiting for a runner", but it is actually the `concurrency` hold (`cancel-in-progress: false`, `orchestrate_poll.yml:68-73`). 14 of 47 poll runs (30%) were cancelled as superseded pending runs. Poll p50 is 603s and p95 is 1010s. An event can therefore wait up to about 20 minutes (one full cycle plus its own). **Impact: high. Confidence: high** for the serialization, medium for the latency estimate.
- **Review/autofix is the critical-path and cost driver.** p50 is 1396s and p95 is 5585s; the slowest run is 38873366294 at 6557s. All 46.67M OpenRouter tokens in the window come from 9 review runs, with a cache hit rate of 0.684. Two patterns stand out:
  - Cache writes are about 400k tokens per review round regardless of call count: 383k–428k for runs with 6–10 calls, and about 785k for runs with 20 calls.
  - Uncached prompt input is about 103k tokens per call, every call.

  **Impact: high (cost). Confidence: medium** (the per-round pattern is inferred from aggregates).
- **Reliability is good.** There were 2 failures out of 290 conclusive or cancelled runs. Neither failure can be classified, because the failing steps do not emit a reason line:
  - run 38869906326 failed at `Checkout PR head branch`; that step has 4 or more `exit 1` paths plus `set -e` git calls;
  - run 38866882920 failed at `Clarify loop guard unit tests`, a step that runs 16 test scripts serially under `set -e`.

  **Impact: medium (diagnosability). Confidence: high.**
- **Codex token telemetry is unreliable for plan and clarify.**
  - The plan family reports `codex_calls=4` but `codex_tokens_used=2` (run 38901191015, 694s at `MODEL_REASONING_EFFORT: high`). This is not credible. The likely cause (an inference) is that `CODEX_TOKENS_RE` (`cost_audit.py:62-64`) is matching non-usage text while the real usage lines are lost to truncation (634k chars).
  - Clarify run 38901066302 (349s) reports 0 Codex calls.

  **Impact: medium (cost blind spot). Confidence: medium.**
- **71% of all runs (710 of 1000) are skip-gated no-ops.** Each `issue_comment` or label event fans out to clarify, plan, implement and orchestrate_clarify_respond at the same second (for example 38902386523/548/552/580 at 12:42:12). Most finish in about 1s, but some take 7–11s (38901689840 11s, 38901070006 8s). **Impact: low (runner minutes / API listing noise). Confidence: high.**
- **Semble is healthy but costs about 41.6s per bootstrap for one query.** There were 11 bootstraps totalling 458,016 ms, for 11 queries returning 167,359 bytes, with 0 failed and 0 unused. All 24 CI fallbacks are contract-test fixtures, not production fallbacks. **Impact: low–medium (latency). Confidence: high.**

## Speed Optimizations

Ranked by expected latency reduction.

1. **Make poll-cycle time visible, then cut it (critical path).**
   - **Evidence:**
     - Poll runs 38895021430 (640s), 38896051549 (670s), 38896972053 (771s), 38900121136 (712s) and 38901192024 (828s) run back-to-back with no gap.
     - `scripts/orchestrate_poll_process.sh` has no timing lines (no matches for `TIMING|_ms=|elapsed_ms`).
     - Poll logs are truncated by about 826k chars, so the slow inner phase cannot be found.
   - **Root cause:** an unmeasured monolithic poll step. Per-cycle work grows with the number of active projects (inference).
   - **Exact change:** this is a diagnostic-only, additive change.
     - In `orchestrate_poll_process.sh`, wrap each top-level phase with a helper that appends `POLL_PHASE_TIMING phase=<name> ms=<n> items=<n>` to `${RUNTIME_DIR}/poll_timing.log`.
     - Print that file from the existing always-run step `Record GH_PAT budget at poll end` (`orchestrate_poll.yml:1887`). That step's log is separate from the truncated `poll` step, so the lines survive truncation.
   - **Estimated savings:** none directly. It is the prerequisite for removing several minutes per cycle. Each minute removed from a cycle removes about 1–2 minutes of event-to-action latency, because of the serialization.
   - **Risk:** very low (log-only, fail-open).

   ```bash
   # orchestrate_poll_process.sh (sketch; new identifiers, fail-open)
   _poll_phase_timing_begin() { _POLL_PHASE_T0="$(date +%s%3N)"; }
   _poll_phase_timing_end()
   {
   	local phase="$1" items="${2:-0}" now
   	now="$(date +%s%3N)"
   	printf 'POLL_PHASE_TIMING phase=%s ms=%s items=%s\n' "${phase}" "$(( now - ${_POLL_PHASE_T0:-now} ))" "${items}" \
   		>> "${RUNTIME_DIR:-/tmp}/poll_timing.log" 2>/dev/null || true
   }
   ```

2. **Reuse the Semble index across review rounds inside one run (review critical path).**
   - **Evidence:** runs with 2 review rounds paid 2 bootstraps: 38889727735 took 85,338 ms and 38889753714 took 87,094 ms. Single-round runs took about 42–43s each (38873366294 43,348 ms; 38879899445 42,596 ms).
   - **Root cause:** lazy bootstrap (`semble_helpers.sh:190-266`) runs once per job. The index is rebuilt per round.
   - **Exact change:** persist `SEMBLE_INDEX_PATH` keyed by the base SHA through the job's existing `RUNNER_TEMP` artifact handoff, if rounds share a runner. Otherwise use `actions/cache` keyed by `semble-index-${base_sha}`. Fall back to the lazy bootstrap on a miss.
   - **Estimated savings:** about 42s per extra round. 2 of 9 telemetry runs had 2 rounds, so roughly 85s across this window. This is a micro-optimization.
   - **Risk:** low. Stale-index risk is avoided by keying on the SHA.

3. **Stop full-history checkout on no-op heal reconciles.**
   - **Evidence:** run 38900119157 spent about 118s on checkout before a script that took about 0.5s and logged `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`. The job uses `fetch-depth: 0` (`internal-cancel-on-pr-close.yml:45-50`). The noop decision (`workflow_failure_heal_pr_reconcile.sh:96-105`) uses only `gh api`, never git.
   - **Exact change:** add `filter: blob:none` to that checkout. This keeps full history for the merge path in `_retarget_heal` but skips blob download.
   - **Alternative:** move the heal-issue scan into a cheap pre-step and gate the full checkout on a non-empty result.
   - **Estimated savings:** about 90–110s per merged PR close (inference). This is off the critical path.
   - **Risk:** low.

4. **CI wall time (p50 1059s, p95 1888s) gates merges.**
   - **Evidence:**
     - CI run 38900120438 has about 5.8 minutes between the last test step (12:30:06) and `Post Setup Python` (12:35:56) with nothing visible in between.
     - The slowest CI runs (38879061374 at 2225s, 38867049145 at 2062s) cluster in the 07:11–09:30 burst (inference: runner contention).
   - **Change:** add the per-script timing described in Reliability item 2. That would show where the 5.8-minute gap goes.
   - **Estimated savings:** unknown until measured.
   - **Risk:** none.

## Cost Optimizations

Ranked by expected token savings.

1. **Uncached prompt input: about 103k tokens per call (10,193,892 over 99 calls).**
   - **Evidence:**
     - The lowest-hit run, 38881801503 (pr:7498, cache hit 0.374), had 1,192,380 uncached tokens over 10 calls.
     - The best run, 38879899445 (0.876), still had 664,595 uncached tokens over 7 calls.
     - `TARGETED_FILE_CONTEXT_MAX_BYTES: 102400` is roughly 25k tokens on its own.
   - **Root cause (inference):** a large per-call dynamic suffix (diff, targeted file context, reviewer-specific instructions) that never lands in a cacheable prefix.
   - **Exact change:**
     1. **Diagnostic.** `review_run_reviewers.sh:4087-4088` already computes `reviewer_base_prompt_bytes` and `reviewer_effective_prompt_bytes`. Emit them as a structured line, if they are not already logged in that form: `REVIEWER_PROMPT_BYTES slot=<model> attempt=<n> base_bytes=<n> effective_bytes=<n>`. Teach `cost_audit.py` to sum it.
     2. Once measured, move whatever is identical across reviewer slots (diff, targeted context) ahead of the slot-specific instructions.
   - **Estimated savings:** if 30–50% of the uncached 10.2M becomes cache reads, about 3–5M tokens per window are converted to cache-read pricing.
   - **Quality risk:** none. This only reorders content.

2. **About 400k cache-write tokens per review round (4,410,064 total, 9.4% of tokens).**
   - **Evidence:**
     - Single-round runs write 383,720–428,086 tokens regardless of 6, 7 or 10 calls.
     - Two-round runs write 784,890 and 786,628.
   - **Root cause (inference):** parallel reviewer slots start at the same moment, so each writes its own copy of the shared prefix before any read can hit. Alternatively, the prefix changes between rounds.
   - **Exact change:** prime the cache with one call on the static prefix (output from the existing `Pre-assemble static context (cacheable across runs)` step at `review_autofix.yml:2549`) before fanning out reviewers. Then check whether round 2's writes drop.
   - **Estimated savings:** 50–75% of writes, about 2.2–3.3M tokens per window.
   - **Quality risk:** none. It adds a few seconds of serialization.

3. **Semble context is efficient and not noisy.**
   - 167,359 bytes over 11 queries is about 15.2KB (about 3.8k tokens) per query, which is under 0.4% of per-run prompt volume.
   - The queries returned 62 sources (about 5.6 per query) with `static_dup_bytes_total=0` and `echo_lines_dropped=0`.
   - **Recommendation:** keep as is. Semble does not visibly reduce prompt expansion either (uncached input stays about 100k per call). The next step is to measure whether its sources displace targeted-file-context bytes, using the line from item 1.

4. **Serena:** disabled (`SERENA_ENABLED: false` in 38900128444 and 38900119266). There are no query or response bytes to evaluate.

5. **Codex cost is blind for clarify and plan.** See Reliability item 3. With `MODEL_REASONING_EFFORT: high` on runs of 349–694s, this is likely a material unmetered spend (inference).

## Reliability Improvements

1. **Make the `Checkout PR head branch` failure classifiable (review_autofix, run 38869906326, pr:7486, failed after 2370s).**
   - **Evidence:**
     - The step (`review_autofix.yml:2780-2893`) has these exit paths: workspace unavailable (2811), OpenCode config present (2816), empty `HEAD_REF` (2822), fetch failure after 4 attempts (2865), plus `set -e` exits from `git_ref_health_check.sh repair`, `git reset --hard` and `git checkout -f -B`.
     - Only some of these paths print `::error::`, and none prints a stable prefix.
     - Run 38867043038 (pr:7486, created 07:24:44, 2630s) was still running when 38869906326 started at 07:51:55. **Inference:** the overlapping runs on the same PR may have raced on the head ref.
   - **Root cause category:** unknown (logs unavailable). The candidates are a git/ref race and transient fetch failure.
   - **Exact fix (diagnostic):** add an ERR trap plus an explicit reason on each exit path, keeping exit codes unchanged.

   ```bash
   # top of the "Checkout PR head branch" run block
   _review_checkout_reason="unclassified"
   trap 'echo "REVIEW_HEAD_CHECKOUT_FAIL pr=${PR_NUMBER:-none} reason=${_review_checkout_reason} line=${LINENO} rc=$?"' ERR
   # before each existing `exit 1`, set e.g.:
   _review_checkout_reason="workspace_unavailable"   # / opencode_config / empty_head_ref / fetch_exhausted
   echo "REVIEW_HEAD_CHECKOUT_FAIL pr=${PR_NUMBER:-none} reason=${_review_checkout_reason}"
   ```

   This step body lives in `review_autofix.yml`, so check its size with `wc -c` against the 480,000-byte guard before growing it.
   - **Expected impact:** next occurrence becomes root-causable.
   - **Rollback:** this is log-only.

2. **Make the `Clarify loop guard unit tests` failure attributable (CI run 38866882920, 1404s).**
   - **Evidence:** `ci.yml:1383-1401` runs 16 scripts serially under `set -euo pipefail`. The first failure aborts the step, and nothing names the failing script. The review run on the same head at the same time (38866883085, 07:23:13) succeeded, so this may be a flake (inference).
   - **Exact fix:** wrap each invocation so it echoes `CI_TEST_SCRIPT_RESULT step=clarify_loop_guard script=<path> rc=<n> ms=<n>`. Keep fail-fast semantics, or run all scripts and exit non-zero at the end.
   - **Expected impact:** flake vs. regression becomes visible, and the CI wiring stays unchanged.

3. **Fix Codex usage telemetry coverage.**
   - **Evidence:** plan family `codex_calls=4, codex_tokens_used=2`. Clarify 38901066302 shows 0 calls despite a 349s model step. `CODEX_TOKENS_RE` matches any "tokens used … <digits>" text.
   - **Exact fix:**
     - After each Codex invocation, emit one anchored line `CODEX_USAGE phase=<phase> tokens_used=<n>` (no such prefix exists in `scripts/` today) in a small trailing step outside the truncated region.
     - In `cost_audit.py`, prefer `CODEX_USAGE` when present and fall back to the regex otherwise.
     - Add `CODEX_USAGE` as a new log prefix via an `agents.d/` fragment.
   - **Expected impact:** restores the cost signal for clarify, plan, implement and validate.

4. **Semble fallbacks are healthy fail-open behavior, not a masked rollout.**
   - All 24 CI fallbacks are `semble_contract_test_fallbacks` (fixtures counted by `_is_contract_test_semble_line`, `cost_audit.py:334`): 4 in 38866882920, 8 each in 38900128510 and 38900120438.
   - There was 1 runtime fallback, in implement run 38876450639 (1133s, 11 Codex calls). It was rare and its reason is unknown without logs.
   - `SEMBLE_AVAILABLE=false` in poll runs is the expected initial value in lazy mode (`orchestrate_poll.yml:91`), not a failure. No action is needed beyond the next item.

5. **Poll runs show zero `SEMBLE_*` lines across 7 telemetry runs.** It cannot be told apart whether the poller never queries Semble or the lines are hidden in the about 826k truncated chars. Have the poll-end step print the counts of `SEMBLE_QUERY` and `SEMBLE_FALLBACK` lines from the runtime log.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN` were 0 across all 115 runs with log telemetry. There is no observed rubric or prompt-size pressure. Note that the poll, plan and clarify logs were truncated.

## AI Memory Health

- Only one `AI_MEMORY_TELEMETRY` line is visible in the window, from run 38900119195 (issue_pr_status, PR #7497): `{"did_push": true, "final_state": "merged", "issue_number": 7492, "ok": true, "op": "finalize-task"}`. It needed `push_attempts: 2`, so one push retry. That is acceptable, but track it.
- **No `retrieve` operations are visible.** Hit rate, `estimated_tokens` vs. budget and the `keyword_method` distribution cannot be computed. The likely carriers (plan, clarify and poll logs) were truncated by 146k–827k chars.
- **Recommendation:** verify emission by printing a per-job tally (`AI_MEMORY_TELEMETRY_SUMMARY retrieves=<n> zero_hit=<n> fail_open=<n>`) from an always-run trailing step. Truncation currently hides the per-op lines.

## GH API Call Audit

- **Observed budgets are healthy:**

  | Run | Job | `used_in_job` | `remaining` |
  |---|---|---|---|
  | 38902594306 | sweep | 2 | 4483 |
  | 38897600904 | sweep | 16 | 4425 |
  | 38900119266 | post-merge-force-poll | 11 | 3697 |
  | 38900119266 | post-merge-validate-dispatch | 6 | 3704 |

  No HTTP 429 or secondary rate-limit lines appeared in any `log_summary`.
- **Gap:** the poller, the most likely API hotspot at 10–14 minutes per cycle, emits `GH_PAT_BUDGET phase=end` from `orchestrate_poll.yml:1887-1898`. No `log_summary` reports its value, so per-cycle poll API spend is unmeasured. **Next step:** have the collector extract `GH_PAT_BUDGET` lines from the poll-end step into `cost_telemetry`.
- **No redundant per-item patterns are visible** in the retained evidence. The `lint_pr_body_auto_close` run 38900128061 uses one paginated commits call, which is correct. The heal reconcile's issue scan (`--paginate … labels=<heal>`) is one call per PR close, which is fine.
- **Skip fan-out:** each of the 707 skip-gated runs costs GitHub-side workflow scheduling but no PAT calls (inference: 1s runs, no budget lines). No change is required on API grounds.

## Prompt Cache & Memory System

- **`cache_hit_rate` = 0.684** (31.6M read / 46.2M input-side tokens) across 9 review runs. The range is 0.374 (38881801503, pr:7498) to 0.876 (38879899445, pr:7481).
- **Fragmentation signals (inference):**
  1. Write volume is fixed per round (about 400k), which points to parallel cold writes.
  2. Uncached input is a steady about 100k per call, which points to a large dynamic suffix.
  3. The 0.374 run had 10 calls but the lowest read volume (941k). Its prefix probably varied across slots, or its cache entries had expired.
- **Recommendations:** use the priming call and the dynamic-suffix reordering from Cost items 1–2. Add `REVIEWER_PROMPT_BYTES` so the next report can tie hit rate to prefix stability.
- **Expected effect:** hit rate moves toward 0.8 or higher, saving about 2–5M tokens per window, with no reliability risk.
- `OPENROUTER_PROMPT_CACHE_DISABLED: false` is set (plan run 38901191015). No `CONTEXT_BUDGET_WARN` lines were seen, so there is no prompt-growth pressure.

## Orchestrator Health

- **Project #7490 is progressing.**
  - Sub-issue #7492 merged via PR #7497 (sync run 38900119195, `final_state: merged`).
  - Issue #7500 moved clarify (38901066302, 349s) → `/answer [auto-answered-by-orchestrator]` → plan (38901191015, 694s). The auto-answer path is working with no human loop.
  - Integration readiness 38900128057 reports `2/5 sub-issues on #7490 still unchecked`. This is expected mid-project.
  - The integration PR #7499 body uses `Refs #7490` and the lint passed.
- **The poller is saturated** (see Executive Summary): 30% of runs were cancelled before their first step, and the rest run back-to-back. This is the top orchestrator pain point. Smallest safe mitigation: measure with `POLL_PHASE_TIMING` first; do not change concurrency (it guards conflict-heal state).
- **Indicators to track:**
  - poll run duration p50/p95, with a target below the trigger interval;
  - `cancelled_before_first_step` share of poll runs, currently 14/47;
  - queue-to-start delay compared with the previous run's end;
  - `POLL_PHASE_TIMING` top phase.
- **Low-noise items:**
  - `##[warning]No standalone validation workflow could be dispatched for merged PR #7497` fires on the expected path where the issue lacks `ai:orchestrator-validate-required`. Consider downgrading it to `::notice::` to keep warning counts meaningful.
  - The Node.js 20 deprecation warning on `actions/cache/restore@v4` appears in polls 38896051549 and 38895021430.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| Event → poll | **Queueing (concurrency hold)** about 1.5–3 minutes, plus a full cycle | 38896051549 (82s), 38901192024 (167s), 38896972053 (173s) |
| Poll | **Compute** about 10–14 minutes, unattributed | p50 603s, p95 1010s |
| Clarify → plan | Compute, high reasoning | 349s + 694s for #7500 |
| Implement | Compute | 38876429077 1903s, 38876450639 1133s |
| Review/autofix | **Compute + model calls**, about 23 minutes to 1.8 hours | p50 1396s, p95 5585s, 6–20 OR calls per run |
| CI | Compute, about 14–37 minutes | p50 1059s, p95 1888s |
| Merge/conflict | Minor | 1 retried memory push; no conflict-heal signals visible |

The fixes ordered by end-to-end impact are: (1) poll timing then trimming, (2) review prompt-cache restructuring, which cuts both cost and per-call latency, (3) reusing the Semble index across rounds, (4) CI per-script timing.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** poller serialization (back-to-back cycles, about 10–14 minutes each); review/autofix p95 of 5585s; CI p50 of 1059s.
- **Top failure modes:**
  - unclassified checkout failure in review/autofix (38869906326);
  - unattributed CI test-script failure (38866882920);
  - otherwise only superseded or cancelled runs (20).
- **Highest-cost drivers:** review/autofix OpenRouter calls (46.67M tokens over 9 runs; the top two were 8.84M for 38889753714 and 8.66M for 38879899445); implement Codex usage (1.32M tokens in 38876450639); clarify and plan Codex usage is unmetered.
- **Top 3 actions:**
  1. Add `POLL_PHASE_TIMING` and print it from the poll-end always-run step.
  2. Add the `REVIEW_HEAD_CHECKOUT_FAIL`, `CI_TEST_SCRIPT_RESULT` and `CODEX_USAGE` structured lines.
  3. Add the review cache-priming call and emit `REVIEWER_PROMPT_BYTES`.

## Metrics Appendix

**Run outcomes (window: 1000 runs)**

| Family | Runs | Success | Fail | Cancelled | Skipped/other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| review_autofix | 60 | 54 | 1 | 3 | 2 | 1396 | 5585 |
| ci | 47 | 46 | 1 | 0 | 0 | 1059 | 1888 |
| orchestrate_poll | 47 | 33 | 0 | 14 | 0 | 603 | 1010 |
| clarify | 145 | 5 | 0 | 0 | 140 | 1 | 10 |
| plan | 139 | 5 | 0 | 0 | 134 | 1 | 11 |
| implement | 138 | 4 | 0 | 0 | 134 | 1 | 11 |
| orchestrate_clarify_respond | 138 | 0 | 0 | 0 | 138 | 1 | 10 |
| workflow_failure_heal | 59 | 0 | 0 | 0 | 59 | 1 | 10 |
| workflow_failure_heal_intake | 67 | 10 | 0 | 3 | 54 | 7 | 177 |
| check_failure_triage | 49 | 1 | 0 | 0 | 48 | 1 | 11 |
| cancel_on_pr_close | 31 | 31 | 0 | 0 | 0 | 73 | 245 |
| integration_pr_readiness | 26 | 26 | 0 | 0 | 0 | 123 | 417 |
| lint_pr_body_auto_close | 23 | 23 | 0 | 0 | 0 | 63 | 415 |
| **Total** | **1000** | **268** | **2** | **20** | **710** | **2** | **1329** |

**Token and cache metrics**

| Metric | Value |
|---|---|
| OR calls | 99 (99 with usage, 0 without) |
| OR total tokens | 46,667,574 |
| OR uncached prompt / completion | 10,193,892 / 444,453 |
| OR cache read / write | 31,619,410 / 4,410,064 |
| `cache_hit_rate` | 0.684057 (range 0.374–0.876) |
| Codex calls / tokens | 15 / 1,322,141 (plan's 4 calls / 2 tokens are suspect) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all) | 2,000 / 5,834,320 |
| review_autofix `wall_clock_p50_ms` / `p99` | 4,646,000 / 6,457,040 (slow-sample biased) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**GH API (`GH_PAT_BUDGET`)**

| Run | Job | `used_in_job` | `remaining` |
|---|---|---|---|
| 38902594306 | review_autofix_sweep / sweep | 2 | 4483 |
| 38897600904 | review_autofix_sweep / sweep | 16 | 4425 |
| 38900119266 | post-merge-force-poll | 11 | 3697 |
| 38900119266 | post-merge-validate-dispatch | 6 | 3704 |
| poll runs | orchestrate_poll / poll | not extracted | — |

**Semble**

| Metric | Value |
|---|---|
| Query calls / logged bytes | 11 / 167,359 (about 15.2KB per query) |
| Sources / static dup bytes / echo dropped | 62 / 0 / 0 |
| Bootstraps (ok / failed / unused) | 11 / 0 / 0; 458,016 ms total (about 41.6s each) |
| Fallbacks total | 25: 24 contract-test (CI), 1 runtime (implement 38876450639) |

**Serena / MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | fallbacks |
|---|---|---|---|---|---|
| serena (all families) | 0 | 0 | 0 | 0 | 0 (`SERENA_ENABLED: false`) |

Other MCP servers observed: none.

**Data gaps:**
- The logs directory was not mounted.
- Poll, plan and clarify logs were truncated by 146k–827k chars.
- There were no `AI_MEMORY_TELEMETRY` retrieve ops.
- Poll `GH_PAT_BUDGET` end values were not extracted.
- Codex usage was not captured for clarify or plan.

---
**Actions taken:** this was a read-only analysis; no files were changed. I read the failing steps in `ci.yml` and `review_autofix.yml`, the concurrency and Semble setup in `orchestrate_poll.yml`, the noop path in `workflow_failure_heal_pr_reconcile.sh`, and the Codex token regex in `cost_audit.py`.
**Assumptions:** poll queue time equals the concurrency hold (based on matching start and end timestamps). The about-400k cache write per round comes from parallel cold writes (inference).
**Missing context:** raw run logs for 38869906326 and 38866882920 are needed to confirm the failure root causes.