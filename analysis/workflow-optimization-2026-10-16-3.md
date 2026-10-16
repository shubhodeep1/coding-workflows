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
## Deep Audit — Workflows & Scripts (2026-10-16)

> **Scope and method.** This was a read-only audit. I went deep on the orchestrator poll path (`orchestrate_poll.yml`, `scripts/orchestrate_poll_process.sh`), the shared helpers (`scripts/gh_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/label_helpers.sh`, `scripts/stage_workflow_support.sh`), the review path (`review_autofix.yml`, `review_autofix_sweep.yml`, `internal-review.yml`), `implement.yml`, `plan.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh` and `tests/test_workflow_file_size_limit.py`. The other 47 workflows were covered only by targeted greps (in-body `${{ }}` use, inline Telegram `curl`, `gh label create`, the `gh_retry` stub, TODO markers). No shell was available, so file byte counts and `run:` body lengths are **estimates from line counts** and are marked `[NEEDS VERIFICATION]`. Findings already in this report are not repeated: the `Checkout PR head branch` failure classification is Reliability item 1, and poll serialization and timing are Speed item 1.

### Section 1: Bug & Correctness Sweep

**SEC-001 — Review Telegram steps can run a PR-head `scripts/gh_helpers.sh` while the PAT is in the environment**
- **File / lines:** `scripts/tg_helpers.sh:20-25`; called from `.github/workflows/review_autofix.yml:4101, 4507, 4846, 4905, 4996, 5149, 5441`
- **Severity:** High · **Category:** `security`
- **Description:**
  - `tg_helpers.sh` sources gh_helpers through a path relative to the current directory: `if [ -f "scripts/gh_helpers.sh" ]; then source scripts/gh_helpers.sh; fi`.
  - In the `codex-agent` job of `review_autofix.yml` the current directory is `GITHUB_WORKSPACE`. No `working-directory` or `defaults:` is set anywhere in the file (grep returned no matches).
  - That workspace is checked out at the PR head: `ref: ${{ needs.gate.outputs.review_checkout_sha || github.sha }}`, `review_autofix.yml:1956-1987`.
  - The seven Telegram steps source `${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh` **before** `${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh` (for example lines 4905-4906, 4996-4997, 5149-5150). At that point `tg_helpers.sh` sources the workspace copy of `scripts/gh_helpers.sh`.
  - Those steps export `GH_TOKEN: ${{ secrets.GH_PAT }}` and `TG_BOT_SECRET` (lines 4901-4903, 4992-4994, 5138-5140).
  - This breaks the trust model stated in `scripts/stage_workflow_support.sh:11-12` ("Never stage executable support from the reviewed worktree") and `:112-113` ("never execute the PR copy").
  - The exposure: in this repo any same-repo PR, including one the AI editor changed in the same run, can change `scripts/gh_helpers.sh`. The trigger is `pull_request` (`internal-review.yml:11-12`), so fork PRs get no secrets and are not exposed.
  - `[NEEDS VERIFICATION]`: whether `GITHUB_WORKSPACE` still holds the PR-head `scripts/` tree at these late steps. Lines 2792-2796 mention a separate `GIT_WORK_TREE` split, but nothing seen removes `GITHUB_WORKSPACE/scripts`.
- **Recommended fix:** resolve the sibling helper from the script's own location, using the same pattern as `scripts/workflow_failure_heal_pr_reconcile.sh:50-54`. As defence in depth, also swap the source order in the seven steps so `gh_helpers.sh` loads first. `_tg_helpers_dir` is a new identifier; it did not appear in any of this run's grep output, but grep `scripts/` once to confirm it is unused. Add a contract test asserting that `tg_helpers.sh` never sources a CWD-relative path.
  ```bash
  # scripts/tg_helpers.sh, replacing lines 20-25
  _tg_helpers_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "${_tg_helpers_dir}/gh_helpers.sh" ]; then
  	# shellcheck disable=SC1091
  	source "${_tg_helpers_dir}/gh_helpers.sh"
  fi
  ```

**BUG-001 — The tracking-issue listing silently stops at 20 projects**
- **File / lines:** `.github/workflows/orchestrate_poll.yml:257-267`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - `gh issue list ... --label "ai:orchestrator-tracking" --state open --json number,title,labels --limit 20` writes `tracking_issues.json`.
  - That file is the only input to the poller loop (`scripts/orchestrate_poll_process.sh:22514-22535`) and to the snapshot step (`orchestrate_poll.yml:1685`).
  - When there are 21 or more open projects, the extra ones are never polled, and nothing is logged. A grep of `orchestrate_poll_process.sh` found no second listing that would cover them.
- **Recommended fix:** keep the 20-item bound, since raising it lengthens the already saturated cycle (Speed item 1), but make truncation visible. After computing `COUNT` (line 264), add `if [ "${COUNT}" -ge 20 ]; then echo "::warning::Tracking-issue listing hit --limit 20; projects beyond the first 20 are not polled this tick."; fi`. Alternatively, sort by `updated` ascending so starved projects rotate in.

**BUG-002 — A failed no-op comment aborts the step before the failure label is applied**
- **File / lines:** `.github/workflows/implement.yml:5151-5168`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The step runs under `set -euo pipefail` (line 4942).
  - `gh_retry gh api "repos/.../issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` has no `|| ...` guard.
  - `gh_retry` makes **one** attempt for a non-idempotent POST (`scripts/gh_helpers.sh:1066-1069`). A single transient 5xx therefore exits the step before the label edit at 5165-5168.
  - The result is an issue that never gets `ai:implementation-failed` and keeps `ai:done` or `ai:implementing`, which makes it look stalled to the poller.
  - The sibling paths in the same step all guard their writes (`|| true` at 4989, 5032, 5123).
- **Recommended fix:** apply the label change first, then guard the comment, so the state change cannot be lost:
  ```bash
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
  	-f body="${noop_body}" >/dev/null \
  	|| echo "::warning::No-op comment post failed for issue #${ISSUE_NUMBER}; label update still applied."
  ```

**BUG-003 — Loops piped into `while read` lose their variable updates (latent)**
- **File / lines:** `scripts/orchestrate_poll_process.sh:25009-25131`, `:25156`
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - `echo "${WAVE_STATUS}" | jq -r ... | while read -r rtm_issue; do ... done` runs the loop body in a subshell.
  - Inside it, `refresh_integration_backpressure_gate_after_merge` (line 25096; defined at `:5637-5651`) changes `INTEGRATION_BACKPRESSURE_BLOCK_MERGES` and `CWS_AHEAD_BY`. Those changes are thrown away when the loop ends.
  - Today the only readers of `INTEGRATION_BACKPRESSURE_BLOCK_MERGES` are lines 24381, 24916 and 25020, all before or inside the loop, so nothing breaks yet.
  - The review-blocked loop at 25295 already uses the safe `done < <(...)` form, so the two styles sit side by side.
- **Recommended fix:** switch to process substitution, e.g. `while read -r rtm_issue; do ... done < <(printf '%s' "${WAVE_STATUS}" | jq -r '.issues[] | select(.status == "ready-to-merge") | .github_issue')`, and do the same at 25156.

**BUG-004 — The template-path plan guard is bypassed by any repo named `*/coding-workflows`**
- **File / lines:** `.github/workflows/plan.yml:2106`
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - The step condition is `!endsWith(github.repository, '/coding-workflows')`.
  - Every other source-repo check uses the exact slug: `wf_source="shubhodeep1/coding-workflows"` (`plan.yml:332`) and `WORKFLOW_SOURCE_REPO` (`scripts/stage_workflow_support.sh:7`).
  - So a fork or consumer repo that happens to be called `coding-workflows` skips the guard and can produce the silent no-op PRs the guard exists to prevent (comment at 2111-2116).
- **Recommended fix:** use `github.repository != 'shubhodeep1/coding-workflows'`.

**BUG-005 — Snapshot history filenames get a trailing underscore**
- **File / lines:** `.github/workflows/orchestrate_poll.yml:1746`
- **Severity:** Low · **Category:** `bug`
- **Description:** In `jq -r '.tick_at // empty' ... | tr -c 'A-Za-z0-9._-' '_'`, `tr -c` also replaces jq's trailing newline, so every slug ends in `_` (for example `2026-10-16T12_00_00Z_.json`). History pruning still sorts correctly (inference), but the names no longer match `tick_at`.
- **Recommended fix:** use `jq -j` (no trailing newline) or add `| tr -d '\n'` before `tr -c`.

**BUG-006 — Whether a failed timeline fetch is detected depends on the caller's `pipefail`**
- **File / lines:** `scripts/gh_helpers.sh:1640`; `scripts/orchestrate_poll_process.sh:4435`
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - Both use `gh_retry gh api --paginate ... | jq -s 'add // []'`.
  - Without `pipefail`, a failed fetch gives empty input, so jq prints `[]` and exits 0. The caller then treats it as "no cross-references".
  - `orchestrate_poll_process.sh:21` and `workflow_failure_heal_pr_reconcile.sh:43` set `pipefail`, but `gh_helpers.sh` is a library sourced by many callers. `[NEEDS VERIFICATION]` which callers run without it.
  - The main loop already uses the safe pattern at `orchestrate_poll_process.sh:22555-22560`.
- **Recommended fix:** use that pattern here too: `gh_retry_to_file "${tmp}" gh api --paginate ...`, then `jq -s 'add // []' "${tmp}"`, returning 1 when the fetch fails.

**SEC-002 — Credentials passed on the command line (argv) in inline `curl` calls**
- **File / lines:**
  - Telegram bot token in the URL: `.github/workflows/implement.yml:5046`, `test-and-mark-stable.yml:7548`, `validation-refresh.yml:348`, `check_failure_triage.yml:669`, `issue_pr_status.yml:824`, `update_workflows.yml:1723, 1754`, `scripts/implement_handle_guard_block.sh:162, 292, 464`.
  - GitHub token in a header: `.github/workflows/orchestrate_poll.yml:1868-1871`.
- **Severity:** Low · **Category:** `security`
- **Description:** `https://api.telegram.org/bot${TG_BOT_SECRET}/...` and `-H "Authorization: token ${GH_TOKEN}"` put secrets in the process argv. Actions log masking hides them from logs, but not from `/proc/*/cmdline` for other processes on the runner, which in these jobs include LLM agent CLIs from earlier steps (inference about co-residency).
- **Recommended fix:** route Telegram sends through `tg_send_msg` (`scripts/tg_helpers.sh:108-134`). That function still uses argv internally, so a follow-up should move it to `curl --config -` with the URL fed on stdin. Replace the raw jobs `curl` at `orchestrate_poll.yml:1868` with `gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs"`, which reads the token from the environment.

**SHELL-001 — `ls` used for counting and selecting, and a useless `cat`**
- **File / lines:** `.github/workflows/review_autofix.yml:5077-5078`; `.github/workflows/review_autofix_sweep.yml:350`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:**
  - `ls -1 "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err | wc -l` and `ls -1 ... | sort -V | tail -n 1` (SC2012).
  - `cat "${snap_raw}" | jq ...` (SC2002).
- **Recommended fix:** use a glob array, e.g. `shopt -s nullglob; _editor_errs=("${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err); editor_attempts="${#_editor_errs[@]}"`, and pass the file to `jq` directly.

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001 — The close-merged sweep makes one timeline call per issue and one PR call per candidate**
- **File / lines:** `scripts/orchestrate_poll_process.sh:5059-5070, 5131-5199`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** each tick with candidates makes:
  - 2 `gh issue list` calls (`ai:merged` and `ai:ready-to-merge`, each `--limit 200`);
  - N `_issue_timeline_with_cross_refs_json` calls inside the per-issue `for` loop (line 5147), each one GraphQL request through `gh_issue_timeline_with_cross_refs` (`gh_helpers.sh:1706-1752`), plus REST fan-out when it falls back;
  - one `_fetch_pr_json` REST call per merged cross-referenced candidate (line 5196).
  - **Current total:** 2 + N + M calls (M ≥ N in the common case).
- **Recommended fix:**
  - Extend the `_fetch_candidate_issue_details_graphql` pattern (`:18015-18044`, `batch_size=25`) with a sweep-specific helper. Each aliased `issue(number:)` selects `labels`, `body` and `timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT]) { nodes { source { ... on PullRequest { number merged headRefName baseRefName } } } }`.
  - **Projected total:** 2 + ⌈N/25⌉ calls. `_fetch_pr_json` is kept only as a fail-open fallback per candidate.
  - The two listings can also become two aliases in one GraphQL query: 2 → 1.
  - `[NEEDS VERIFICATION]`: which fields `_pr_json_is_issue_implementation_pr` and `_pr_json_base_is_target_merge` read beyond head and base ref (for example `head.repo.full_name` or `body`). Add them to the selection before switching.

**BATCH-002 — The ready-to-merge, in-progress and review-blocked loops each fetch timeline and PR per issue**
- **File / lines:** `scripts/orchestrate_poll_process.sh:25009-25014, 25156-25163, 25295-25304`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:**
  - Each loop over `WAVE_STATUS` calls `_issue_cross_ref_pr_number_last` (1 timeline call, more on REST fallback) and `_fetch_pr_json` (1 REST call) per issue.
  - **Current total:** about 2 × (N_rtm + N_ip_done + N_rb) calls per tracking issue per tick, which is per-iteration API work inside loops (a §15 concern).
- **Recommended fix:**
  - Before the three loops, prefetch the wave's issue numbers once with `_fetch_linked_pr_status_graphql` (`:18173`), extended to return `mergeable`, `headRefOid` and `headRefName`. Store the result in a cycle-local file cache, as `STALL_MANAGED_LINKED_PR_CACHE` does.
  - **Projected total:** ⌈N/25⌉ calls, keeping the per-issue calls as the cache-miss fallback.
  - **Caution:** keep the "last cross-referenced PR" semantics that these loops rely on. The comments at `:18388` and `:18511` explain why `linked_pr` (`willCloseTarget` only) is **not** a drop-in replacement. `[NEEDS VERIFICATION]`

**BATCH-003 — The REST timeline fallback fetches each cross-referenced PR separately**
- **File / lines:** `scripts/gh_helpers.sh:1644-1656`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** the `while read pr_url` loop runs `gh_retry gh api "${pr_url}"` once per unique cross-referenced PR. **Current total:** 1 (paginated timeline) + K calls.
- **Recommended fix:** collect PR numbers and run one aliased GraphQL query (`p<N>: pullRequest(number: N) { number state mergedAt }`) using the alias style of `_fetch_candidate_issue_details_graphql`. **Projected total:** 1 + ⌈K/25⌉. Keep per-URL REST only when that GraphQL call fails. This path runs only after the GraphQL-first helper falls back, so the impact is bounded.

**BATCH-004 — The heal-PR reconcile does one PR lookup per heal issue**
- **File / lines:** `scripts/workflow_failure_heal_pr_reconcile.sh:239-247`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** each matching heal issue triggers `gh api repos/${REPO}/pulls -f head=<owner>:ai/issue-<n>`. **Current total:** 1 + N calls, already documented at lines 29-31.
- **Recommended fix:** list open PRs once with `-f base="${HEAD_REF}"`, plus a second listing with `base="${BASE_REF}"` when `MERGED=true`, since heal PRs target the source head or the re-pointed base (line 252). Then filter by `.head.ref == "ai/issue-<n>"` in jq. **Projected total:** 1 + 1 (or 1 + 2 when merged). N is usually 1, so this only matters when several heal issues stack up.

**BATCH-005 — The implement no-op ancestry walk makes two calls per hop**
- **File / lines:** `.github/workflows/implement.yml:5084-5097`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** each hop makes one `issues/<n>` body read and one paginated `issues/<parent>/comments`. **Current total:** up to 2 × `IMPL_NOOP_ANCESTRY_THRESHOLD` calls (stated at line 5075).
- **Recommended fix:** use one GraphQL call per hop: `issue(number: $n) { body } parent: issue(number: $p) { comments(last: 100) { nodes { body } } }`. **Projected total:** threshold + 1 calls. Fail open to the current REST pair.

**API-001 — The sweep re-fetches the default branch that is already in the PR listing**
- **File / lines:** `.github/workflows/review_autofix_sweep.yml:228-237, 285`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** the paginated `repos/${REPOSITORY}/pulls` response already contains `.base.repo.default_branch` on every PR object, but the jq projection at 230-237 drops it. Line 285 then makes a separate `gh api "repos/${REPOSITORY}" --jq .default_branch` call. **Current total:** 1 extra call per tick that has candidates.
- **Recommended fix:** add `default_branch: .base.repo.default_branch` to the projection and read it from `prs_json` (it is identical across PRs). Keep the line-285 call only when the field is empty. **Projected total:** 0 extra calls.

**API-002 — Every poll tick makes two separate label listings**
- **File / lines:** `.github/workflows/orchestrate_poll.yml:244, 257-262`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** each tick makes a REST `issues?labels=ai:provider-outage` listing and a GraphQL-backed `gh issue list --label ai:orchestrator-tracking`, i.e. 2 calls per tick.
- **Recommended fix:** use one aliased GraphQL query (`outage: issues(labels: ["ai:provider-outage"], states: OPEN, first: 100)` and `tracking: issues(labels: ["ai:orchestrator-tracking"], states: OPEN, first: 20)`), as the probe at lines 308-320 already does for counts. **Projected total:** 1 call per tick. Keep the fail-open semantics separate for each alias.

**Retry and backoff check (no finding):** `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` use exponential backoff `2^(attempt-1)` (`gh_helpers.sh:1116, 1202, 1294, 1314`), reset-aware waits on rate limits (`:1108-1114`), and do not retry permanent failures (`:1099-1106`). I found no tight retry loops in the audited files.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — Nine copies of the truthy-flag parser, two of them in the same file**
- **File / lines:**
  - `scripts/orchestrate_poll_process.sh:964-975` (`is_truthy`) and `:22449-22454` (`_is_truthy`), both in the same file;
  - `scripts/apply_analysis_on_main.sh:170`, `scripts/auto_release_stable.sh:93`, `scripts/review_parse_consolidator.sh:102`, `scripts/promote_main_cycle.sh:150`, `scripts/review_consolidate.sh:47`;
  - inline `case` copies at `.github/workflows/orchestrate_poll.yml:281-284, 1641-1647` and `review_autofix_sweep.yml:269-271`.
- **Severity:** Low · **Category:** `duplication`
- **Recommended fix:** add one canonical `gh_helpers_is_truthy <value>` to `scripts/gh_helpers.sh`, which nearly every caller already sources. It uses a new name per §10; check uniqueness before adding. Keep `is_truthy` and `_is_truthy` as one-line wrappers that delegate to it, then migrate the five standalone scripts. Leave the inline workflow `case` blocks alone: they run before helper staging.

**DUP-002 — Three `ensure_label_exists` implementations with different signatures and color sources**
- **File / lines:** `scripts/label_helpers.sh:159` (`<label> [repo]`, colors from `_AI_LABEL_COLORS`); `scripts/orchestrate_poll_process.sh:3995` (`<label>`, process cache plus `label_contract.v1.json`); `scripts/validate_process.sh:1204` (`<label>`, contract file)
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - There are also 16 inline `gh label create` calls in workflows: `implement.yml` 8, `review_autofix.yml` 4, `workflow-log-analysis.yml` 3, `clarify.yml` 1.
  - Any script that sources `label_helpers.sh` after defining its own copy (or the reverse) silently changes signature, which is a name-collision risk.
  - The metadata already diverges (see CONSIST-002).
- **Recommended fix:** make `scripts/label_helpers.sh` the owner. Add the `_ENSURED_LABELS_CACHE` fast path (`orchestrate_poll_process.sh:3998-4003`) and the contract-file lookup there. Change the two local copies into wrappers (`ensure_label_exists() { label_helpers_ensure "$1" "${GITHUB_REPOSITORY}"; }`, a new helper name). Replace the inline workflow `gh label create` calls with `ensure_label_exists`, as `implement.yml:5157-5159` already does when the helper is present.

**DUP-003 — Ten inline Telegram `curl` senders duplicate `tg_send_msg`**
- **File / lines:** see SEC-002; canonical sender at `scripts/tg_helpers.sh:108-134`
- **Severity:** Low · **Category:** `duplication`
- **Description:** the inline copies bypass `ALERT_MSG_LEVEL` filtering and level prefixes. That is intentional only for the release-gate summary (`tg_helpers.sh:50-55`). The `implement.yml:5034-5036` comment justifies its copy because "tg_helpers.sh may have been cleaned up".
- **Recommended fix:** source `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/tg_helpers.sh` (the staged copy survives cleanup, as line 4943 does for `gh_helpers.sh`) and call `tg_send_msg "${TG_MSG}" "CRITICAL"`. Keep the raw `curl` only for the documented SILENT-bypass gate notification.

**DUP-004 — The autofix-failure fingerprint block is copied verbatim between two steps**
- **File / lines:** `.github/workflows/review_autofix.yml:4911-4930` and `:5002-5021` (and, per the comment at 4912-4913, the "Assemble failure evidence" step)
- **Severity:** Low · **Category:** `duplication`
- **Recommended fix:** move the block into a function `review_autofix_failure_marker_suffix()` that echoes the suffix. Put it in `scripts/review_autofix_step_failure_comment.sh`, or a new `review_autofix_step_<slug>.sh` registered per `agents.md:2384-2400`, and call it from both steps. This also takes about 1.4 KB out of `review_autofix.yml`.

**DUP-005 — About 105 identical env keys duplicated between two poll steps**
- **File / lines:** `.github/workflows/orchestrate_poll.yml:1122-1303` and `:1323-1431`
- **Severity:** Low · **Category:** `duplication`
- **Description:** the two step `env:` blocks match except for `STANDALONE_RECOVERY_SWEEP_ONLY`. The comment at 1312-1314 says a contract test keeps them equal, so drift is guarded, but the duplication costs about 9-10 KB of workflow bytes (estimate).
- **Recommended fix:** move the `vars`/`secrets`-only keys to `jobs.poll.env`, since job-level env can read `vars` and `secrets`. Keep `CLAUDE_POOL_REASON` (needs the `steps` context) and `STANDALONE_RECOVERY_SWEEP_ONLY` at step level. **Risk:** every other poll step then sees those variables. Audit `review_merge_train.sh` and the snapshot steps for reads of shared names such as `ALLOW_WORKFLOW_EDITS` first.

**DUP-006 — The five-step support-source checkout sequence is repeated five times**
- **File / lines:** `.github/workflows/orchestrate_poll.yml:113-164` and `:469-522`; `implement.yml:419-476` and `:1069-1134`; `plan.yml:326-388`
- **Severity:** Low · **Category:** `duplication`
- **Description:** each copy is the same sequence: resolve ref, checkout, fallback checkout, ensure, main snapshot.
- **Recommended fix:** a composite action (`.github/actions/checkout-workflow-support`, inputs `path`, `token`). **Caveat:** consumer repos run these as reusable workflows, so the action must be referenced as `shubhodeep1/coding-workflows/.github/actions/...@<same ref>`. Pinning it to the calling workflow's SHA needs design work. `[NEEDS VERIFICATION]`

**DUP-007 — The label-swap-and-close sequence appears twice in one step**
- **File / lines:** `.github/workflows/implement.yml:4976-4989` and `:5110-5123`
- **Severity:** Low · **Category:** `duplication`
- **Recommended fix:** move the whole "Handle no-op implementation" body to `scripts/implement_handle_noop.sh`, with an internal `_implement_close_as_closed <body>` function. Use the same staged-support wrapper pattern as `scripts/implement_handle_guard_block.sh`. This also resolves EXPR-001.

### Section 4: Expression Size Limit Risk Assessment

The CI guard `tests/test_workflow_file_size_limit.py:66-98` fails any step `run:` containing `${{` at **18,000** characters or more, so no single `run:` body can currently be at High risk. The items below are in the 15,000–18,000 band or are gaps in that guard.

**EXPR-001 — implement.yml "Handle no-op implementation" is close to the 18,000 guard**
- **File / lines:** `.github/workflows/implement.yml:4941-5168`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** a 227-line body with `${{ }}` at 4946, 4948, 5151, 5159, 5161, 5165 and 5167. It is long-comment and long-literal heavy (4971, 4973, 5006, 5011, 5018, 5105 and 5107 are each 260–340 characters).
  - **Estimated size:** about 16,500–17,800 characters. Estimated headroom is about 200–1,500 to the CI guard and about 3,200–4,500 to the 21,000 hard limit. `[NEEDS VERIFICATION — measure with len(step["run"]) as the guard test does]`
- **Recommended fix:**
  1. Immediately, replace all seven interpolations with runner default env vars: `${GITHUB_REPOSITORY}`, `${GITHUB_SERVER_URL}`, `${GITHUB_RUN_ID}`. The body then contains no `${{` and is no longer subject to the limit.
  2. Then move it to `scripts/implement_handle_noop.sh` (DUP-007).

**EXPR-002 — plan.yml "Guard plans targeting template-owned paths" is about 15,600 characters**
- **File / lines:** `.github/workflows/plan.yml:2109-2309`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** about 200 lines with embedded Perl (2167-2182, 2235-2241) and Python (2195-2227) programs and a roughly 760-character literal at 2305. `${{ }}` appears only at 2302, 2304 and 2306.
  - **Estimated size:** about 15,600 characters. Headroom is about 2,400 to the guard and about 5,400 to the hard limit. `[NEEDS VERIFICATION]`
- **Recommended fix:** replace the three interpolations with `${GITHUB_REPOSITORY}`, `${GITHUB_SERVER_URL}` and `${GITHUB_RUN_ID}`. Move the Perl and Python programs to `scripts/plan_template_path_guard.py`, which the trusted support checkout already stages.

**EXPR-003 — review_autofix.yml Telegram and alert bodies will keep growing**
- **File / lines:** `.github/workflows/review_autofix.yml:4904-4987`, `4995-5133`, `5148-5292`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** these bodies are about 84, 139 and 144 lines (estimated 6,000–11,000 characters each) and interpolate only `github.server_url`, `github.repository` and `github.run_id` (4908-4909, 4999-5000, 5059, 5110, 5132, 5152-5157). Each incident adds another message branch (for example 5060-5110 for PR #4077).
- **Recommended fix:** switch to the default env vars now, so future growth costs file bytes only and not expression budget. When the file approaches the 440,000-byte budget-watch threshold (`agents.md:2347-2352`), extract them with the `review_autofix_step_<slug>.sh` pattern.

**EXPR-004 — The guard test only measures step `run:` bodies**
- **File / lines:** `tests/test_workflow_file_size_limit.py:72-91`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** the test walks `jobs.*.steps[*].run` only. Step and job `if:` expressions are not measured (the longest seen is `implement.yml:4934`, about 330 characters), nor `with:` values (multi-line `path:`/`key:` at `review_autofix.yml:2702-2707, 3690-3693, 4822-4825`), job- and step-level `env:` values, `run-name`, or `concurrency.group`. All are well under the limit today.
- **Recommended fix:** extend the test to check every string scalar that contains `${{` against the same 18,000 guard. The change is additive and cheap.

**EXPR-005 — Workflow byte headroom was not measured**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml` (7,551 lines), `implement.yml` (6,494), `review_autofix.yml` (5,690)
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - No workflow can exceed 800 KB: CI fails at 480,000 bytes (`tests/test_workflow_file_size_limit.py:27, 44-59`) and budget-watch warns at 440,000 (`agents.md:2347-2352`).
  - The largest files by line count are the three above. `test-and-mark-stable.yml` would reach the 480,000-byte guard at an average of about 63.6 bytes per line.
  - Byte counts were not measured in this run. `[NEEDS VERIFICATION — wc -c .github/workflows/*.yml]`
- **Recommended fix:** none beyond the existing budget-watch. If `test-and-mark-stable.yml` is over 440,000 bytes, extract its largest `${{`-free bodies first: Phase 4 at 1830-2526 (about 700 lines) and Phase 4b at 2611-3266.

### Section 5: Cross-Cutting Concerns

**Debt markers:** a word-bounded search for `TODO|FIXME|HACK` found **zero** matches in `scripts/` and `.github/workflows/`. A naive `XXX` search matches only `mktemp` templates.

**DEAD-001 — The final REST timeline fallback is effectively unreachable**
- **File / lines:** `scripts/orchestrate_poll_process.sh:4435` (fallback inside `_issue_timeline_with_cross_refs_json`, `:4420-4436`)
- **Severity:** Low · **Category:** `dead-code`
- **Description:** the last-resort `gh_retry gh api --paginate .../timeline` needs `gh_retry`, which comes only from `scripts/gh_helpers.sh` (sourced at `:27-29`). That same file always defines `gh_issue_timeline_with_cross_refs` (`gh_helpers.sh:1706`), which returns first at `:4425-4427`. So the fallback runs only for a staged `gh_helpers.sh` that predates the GraphQL helper. `[NEEDS VERIFICATION for older pinned support refs]`
- **Recommended fix:** keep it as defence, but give it the safe `gh_retry_to_file` form (BUG-006) so the dormant path does not hide failures.

**DEBT-001 — plan.yml finds implement.yml's helper list by matching its YAML text**
- **File / lines:** `.github/workflows/plan.yml:2200-2229`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:** the embedded Python splits `implement.yml` on the literal string `"      - name: Stage workflow support files\n"` and regex-matches `for f in ...; do` loops. Renaming or re-indenting that step silently drops back to blanket `scripts/` rejection (only a `::warning::` at 2228/2232). Consumer plans that touch their own `scripts/` files are then rejected.
- **Recommended fix:** have `implement.yml`'s staging step (or `stage_workflow_support.sh`) write a machine-readable manifest that the plan guard reads. Alternatively, add a CI test asserting that this parser returns a non-empty set against the current `implement.yml`.

**CONSIST-001 — Two env var names for the Telegram chat ID**
- **File / lines:** `TG_CHAT_ID` at `.github/workflows/review_autofix.yml:4902, 4993, 5139`; `TG_ADMIN_CHAT_ID` at `orchestrate_poll.yml:1126, 1839` and `implement.yml:4938`
- **Severity:** Low · **Category:** `consistency`
- **Description:** both names feed the same value. `scripts/tg_helpers.sh:32-35` accepts either, so nothing breaks today, but new helpers that read only `TG_ADMIN_CHAT_ID` would fail silently in review steps.
- **Recommended fix:** also export `TG_ADMIN_CHAT_ID` in the review steps, keeping `TG_CHAT_ID` per §10.

**CONSIST-002 — Inline creation of `ai:closed` disagrees with the label contract**
- **File / lines:** `.github/workflows/implement.yml:4976-4978, 5110-5112` vs `.github/ai/label_contract.v1.json:156-159` and `scripts/label_helpers.sh:62, 125`
- **Severity:** Low · **Category:** `consistency`
- **Description:** implement.yml creates the label with color `6b7280` and description "Issue closed by automation (superseded, duplicated, or already complete)". The contract says `6a737d` and "Linked PR closed without merge". Whichever creator runs first wins. `[NEEDS VERIFICATION]` whether `sync_ai_labels.yml` repairs the drift.
- **Recommended fix:** use `ensure_label_exists "ai:closed"` (DUP-002), and update the contract description if the implement wording is the intended meaning.

**CONSIST-003 — Raw `gh api` / `curl` used where a retry wrapper is available**
- **File / lines:** `.github/workflows/review_autofix_sweep.yml:228` (raw paginated PR listing in a step that loads `gh_api_retry` at 216-220); `orchestrate_poll.yml:1868-1871` (raw `curl`); `implement.yml:1949` and `plan.yml:644` (raw `gh api`)
- **Severity:** Low · **Category:** `consistency`
- **Description:** at `review_autofix_sweep.yml:228` a single transient failure aborts the sweep tick under `set -euo pipefail`.
- **Recommended fix:** use `gh_api_retry --paginate` or `gh_retry_to_file` with `jq -s 'add // []'`, matching `scripts/orchestrate_poll_process.sh:22555-22560`.

**CONSIST-004 — Mixed indentation inside single blocks**
- **File / lines:** `scripts/orchestrate_poll_process.sh` (2-space at `:964-975` and `:5047-5273`; tabs at `:2215-2235` and `:4480-4512`; tabs nested in a 2-space block at `:25041-25100`); `scripts/stage_workflow_support.sh:137-153`
- **Severity:** Low · **Category:** `consistency`
- **Description:** §11 asks for tabs. The mixing inside one `if` block (25041-25100) makes the nesting hard to read in review.
- **Recommended fix:** do not reformat opportunistically (§9). Normalize only the lines a functional change already touches.

**ShellCheck notes:** see SHELL-001. `orchestrate_poll_process.sh` turns off extended analysis (`:12-19`), so SC2030/SC2031 (subshell variable loss, BUG-003) is no longer reported for that file.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 7 | BUG-001, BUG-002, BATCH-001, BATCH-002, DUP-002, EXPR-001, EXPR-002 |
| Low | 26 | BUG-003, BUG-004, BUG-005, BUG-006, SEC-002, SHELL-001, API-001, API-002, BATCH-003, BATCH-004, BATCH-005, DUP-001, DUP-003, DUP-004, DUP-005, DUP-006, DUP-007, EXPR-003, EXPR-004, EXPR-005, DEAD-001, DEBT-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/tg_helpers.sh`, `.github/workflows/review_autofix.yml` (source order in 7 steps), new contract test under `tests/` | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml` | Large |
| Code modularization | `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`, `scripts/validate_process.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/{implement,review_autofix,orchestrate_poll,plan,workflow-log-analysis,clarify}.yml`, new `scripts/implement_handle_noop.sh` | Medium |
| Expression size reduction | `.github/workflows/implement.yml`, `.github/workflows/plan.yml`, `.github/workflows/review_autofix.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`, `.github/workflows/plan.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `.github/workflows/review_autofix_sweep.yml` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-16)

### Safety Tag Legend
`SAFE_TO_MERGE`: the change stays inside one step or function and has no intervening mutation, no change to error or retry behaviour, no pagination or concurrency boundary, and no change to log keys. The implement stage may apply it without further review. `NEEDS_VERIFICATION`: the change looks correct, but at least one precondition cannot be proven by reading the code. The listed verification steps must pass before anyone implements it. `RISKY_SKIP`: the call sits in a retry, stall-recovery, security-gate, pagination or race-defence path, or merging it would change failure semantics. It is listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001 — Post-merge validate dispatch reads labels once per issue on the PR-body fallback path**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:** `.github/workflows/review_autofix.yml:863` (fallback sets `labels: null`), `:868-902` (loop), `:872` (per-issue call). The batched path at `:816-822` already returns labels.
- **Current / proposed calls:** N `gh issue view` calls, where N is the number of issue refs pulled from the PR title/body, only when `closingIssuesReferences` was empty. Proposed: 1 aliased GraphQL call, keeping the per-issue call as a fallback when that call fails.
- **Endpoints:** GraphQL `issue(number:){labels}` (through `gh issue view --json labels`), once per issue.
- **Evidence:**
  ```bash
  issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0)) | map({number: tonumber, labels: null})')"
  ...
  if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
  ```
- **Proposed fix:** before the loop at `:868`, when any node has `labels == null`, run one `gh_retry gh api graphql` with aliases `i<N>: issue(number: N) { labels(first: 100) { nodes { name } } }` under `repository(owner:, name:)`. Use the alias style of `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh`). Fill in `labels` on the nodes. Any alias that is still `null` keeps the existing `:872` call.
- **Safety rationale:** error semantics change. Today a failed read skips only that issue (`continue` at `:880`); a batched call would skip every issue unless the per-issue fallback stays. The loop also mutates state between iterations (`gh workflow run` at `:891`, label removal at `:899`), on other issues only.
- **Downstream signal:** before implementing, count the `Found linked issues via PR body/title fallback:` lines (`:861`) in recent `post-merge-validate-dispatch` logs and confirm N > 1 actually happens. If N is almost always 1, drop this finding; it saves nothing. If you implement it, add a test that a failed GraphQL call still falls through to the per-issue `gh issue view`.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — The no-op ancestry walk re-reads the current issue body, which is already in `ISSUE_META_FILE`**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `.github/workflows/implement.yml:5084-5085` (first hop of the loop, with `current="${ISSUE_NUMBER}"` at `:5083`);
  - `:275` (`ISSUE_META_FILE` exported to the job env);
  - `:2396-2407` ("Recheck security dependency before edits" writes the full issue payload to `ISSUE_META_FILE`, or sets `SKIP_IMPLEMENT=true` on failure);
  - `:1761-1770` (`issue_meta_matches_issue` validator).
- **Current / proposed calls:** 2 × hops (up to 2 × `IMPL_NOOP_ANCESTRY_THRESHOLD`). Proposed: one fewer, because hop 1 needs no body read when the cached file is valid.
- **Endpoints:** REST `GET repos/{repo}/issues/{n}` (`.body`).
- **Evidence:** every write to `ISSUE_META_FILE` in `implement.yml` (`:183, 718, 737, 746, 1773, 1993, 2403`) comes before the "Handle no-op implementation" step (`:4933`). That step only runs when `SKIP_IMPLEMENT != 'true'`, so by then the `:2403` fetch has succeeded. Both steps are in the single `implement` job (`:92-6420`) and use the same `GH_PAT`.
  ```bash
  parent_body="$(gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
  ```
- **Proposed fix:** on the first hop only, when `current == ISSUE_NUMBER` and `jq -e --arg n "${ISSUE_NUMBER}" '(.number|tostring)==$n and has("body")' "${ISSUE_META_FILE}"` succeeds, read `parent_body` from the file. Otherwise keep the live call unchanged. Later hops stay live.
- **Safety rationale:** the two reads are in different steps with a Codex run in between, and nobody has proven that no step edits the issue body in that window. The rule against intervening mutations is therefore not shown to hold.
- **Downstream signal:**
  1. Grep `implement.yml` (`:2407-4933`) and the scripts it stages for any `PATCH .../issues/${ISSUE_NUMBER}` or `gh issue edit --body`. If one exists, drop this finding.
  2. Confirm that the `Re-issued from #N` lineage line is written only when the issue is created.
  3. Because of EXPR-001, the change must not add any `${{ }}` to this step body, and should land with or after EXPR-001.

**REUSE-002 — The codex-agent "Check PR state" payload and the "Collect PR metadata" payload are separate full fetches of the same PR**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `.github/workflows/review_autofix.yml:2177-2201` (`pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" || echo 'null')"`, which uses only `.labels`, `.state` and `.merged*`);
  - `:2603-2610`, which runs `scripts/review_collect_pr_metadata.sh:231` (`gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"`).
- **Current / proposed calls:** 2 full `GET pulls/{n}` per review run. Proposed: 1 when the two steps run within the freshness window, otherwise 2 (unchanged).
- **Endpoints:** REST `GET repos/{repo}/pulls/{n}`.
- **Evidence:** the job already uses a freshness-bounded reuse pattern in `scripts/review_run_reviewers.sh:457-461`: `PR_PAYLOAD_FILE` is reused only when it is 120 seconds old or less, otherwise the script makes a live call at `:464`. The `:2188` payload is a strict superset of what `:231` needs (`review_collect_pr_metadata.sh:250-256` reads title, body, base.ref, head.ref and head.repo.full_name).
- **Proposed fix:** have `:2188` also write `pr_meta` to a new `${RUNTIME_DIR}` file (check that the name is unique). In `review_collect_pr_metadata.sh`, before `:231`, copy that file into `PR_PAYLOAD_FILE` only when all of these hold: it is 120 seconds old or less (same `stat -c %Y` rule as `review_run_reviewers.sh:458-459`), it is an object with `.number == PR_NUMBER`, and it has a non-empty `.head.sha`. Otherwise make the existing call.
- **Safety rationale:** the error semantics differ. `:2188` fails open to `null` and assumes the PR is open, while `:231` is the authoritative payload whose `head.sha` drives the review. The OpenCode, Claude and static-context steps between them probably take longer than 120 seconds, so the reuse may never trigger.
- **Downstream signal:** measure the gap between the end of "Check PR state (defense-in-depth)" and the start of "Collect PR metadata" across recent codex-agent runs. Implement only if p50 is 120 seconds or less. Then confirm that no consumer of `PR_PAYLOAD_FILE` needs a head SHA fresher than the copy guarded by `review_run_reviewers.sh:457-461`.

**REUSE-003 — Capacity resume lists the provider-outage trackers again after `find_tracking` cached them**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `.github/workflows/orchestrate_poll.yml:244-246` (one page, `per_page=100`, cached to `${RUNTIME_DIR}/provider_outage_trackers.json`; already reused without an API call by `pool_window` at `:1036-1038`);
  - `scripts/provider_outage.py:1096` (`find_trackers(gh, "capacity")`) → `:480` → `list_issues` at `:355-361` (`--paginate --slurp`), run from "Resume Claude capacity waits" at `orchestrate_poll.yml:1069-1093`.
- **Current / proposed calls:** 1 paginated listing per tick in the hourly window with `capacity_wait=true`. A naive reuse would make 0.
- **Endpoints:** REST `GET repos/{repo}/issues?labels=ai:provider-outage&state=open`.
- **Evidence:** the two reads differ in pagination (single page vs `--paginate`) and in trust model. The workflow checks `author_association ∈ OWNER/MEMBER/COLLABORATOR` (`:247`); Python checks `author == gh.login()` (`provider_outage.py:486-491`). `capacity_resume` also PATCHes the tracker body it just read (`render_wait_probe(tracker["body"], ...)` → `gh.patch_body`, `:1118-1119`).
- **Proposed fix:** none. Keep the live listing.
- **Safety rationale:** pagination differs, so this hits a RISKY_SKIP trigger. A stale body feeding the `:1119` PATCH would also reopen a lost-update race against the review sweep's tracker writes.
- **Downstream signal:** do not auto-implement. Changing this needs a manual design review of the tracker read-modify-write contract in `provider_outage.py` (docstring `:46-62`). The saving is at most 1 call per hour.

**REUSE-004 — The review-blocked judge re-reads PR state in the final-`fix` merge branch after its post-LLM guard read**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_rb_judge.sh:1888` (`_guard_pr_meta`, full payload, read after the LLM call per the comment at `:1878-1884`);
  - `:2207-2222` (`fix)` with `IS_FINAL=true` re-reads `.state` before the bound merge).
  - Same family, for reference: `:1071` (early guard), `:2109` and `:2635` (mergeable poll loops).
- **Current / proposed calls:** 2. A naive reuse would make 1.
- **Endpoints:** REST `GET repos/{repo}/pulls/{n}`.
- **Evidence:** the comment at `:1878-1884` explicitly defends against auto-merge finishing while the judge runs. `RB_JUDGED_HEAD_SHA` / `RB_LIVE_HEAD_SHA` bind the merge to the head that was evaluated. The security-severity and action dispatch (`:1964-2072`) run between the two reads.
- **Proposed fix:** none.
- **Safety rationale:** this is a stall-recovery judge path whose re-reads exist to defend against an upstream merge race (TOCTOU), which is a RISKY_SKIP trigger.
- **Downstream signal:** do not auto-implement. Any change needs a manual review of the head-binding and merged-state guards (`:1888-1901`, `:2211-2216`).

**REUSE-005 — implement.yml reads the full issue twice for the security-dependency gate**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:** `.github/workflows/implement.yml:1985-1993` ("Validate approval phase label": `DEPENDENCY_META` fetch) and `:2396-2407` ("Recheck security dependency before edits"). Both overwrite `ISSUE_META_FILE`. The label reads at `:2031-2037` already reuse the file.
- **Current / proposed calls:** 2. A naive reuse would make 1.
- **Endpoints:** REST `GET repos/{repo}/issues/{n}`.
- **Evidence:** the comment at `:1985-1986` says "Recheck just before advancing the phase. /approved must not bypass a still-open security prerequisite or a failed API read." Each read fails closed (`SKIP_IMPLEMENT=true`).
- **Proposed fix:** none.
- **Safety rationale:** these are permission and security-gate rechecks that must stay fresh at two separate decision points, which is a RISKY_SKIP trigger.
- **Downstream signal:** do not auto-implement. It is listed so later passes do not propose it again.

### Dead Calls (DEAD-API-###)

**DEAD-API-001 — The forward-merge failure alert fetches the run's jobs for a DEBUG message that the default threshold drops**
- **Safety tag:** `SAFE_TO_MERGE`
- **Files / lines:** `.github/workflows/forward-merge-stable-to-main.yml:459-515` (lookup at `:493-504`, send at `:515`; `ALERT_MSG_LEVEL` defaults to `'WARNING'` at `:464`); filter in `scripts/tg_helpers.sh:83-91, 113-115, 120-122`.
- **Current / proposed calls:** 1 per failed run. Proposed: 0 when the alert cannot be delivered (always the case under the default `WARNING` threshold), still 1 when `ALERT_MSG_LEVEL=DEBUG` and Telegram is configured.
- **Endpoints:** REST `GET repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs` (raw `curl`).
- **Evidence:** `failing_step` is used only to build `msg`, which goes only to `tg_send_msg "${msg}" "DEBUG"`. `tg_send_msg` returns before sending when `_tg_should_send DEBUG` fails (DEBUG=0 < WARNING=1) or when `TG_BOT_SECRET` or the chat ID is empty:
  ```bash
  if ! _tg_should_send "${level}"; then
  	return 0
  fi
  ...
  if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${chat_id}" ]; then
  	return 0
  fi
  ```
  No test pins this lookup (grep of `tests/` for `failing_step` / `attempts/` finds only unrelated fixtures).
- **Proposed fix:** guard the existing `if [ -n "${GH_TOKEN:-}" ]` block at `:494` with a deliverability check that uses the same predicate. Leave the `tg_send_msg` call unchanged, since it still filters on its own. Check that the new local name is unique in the step:
  ```bash
  fm_alert_deliverable=true
  if declare -F _tg_should_send >/dev/null 2>&1 && ! _tg_should_send DEBUG; then fm_alert_deliverable=false; fi
  if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}" ]; then fm_alert_deliverable=false; fi
  if [ "${fm_alert_deliverable}" = "true" ] && [ -n "${GH_TOKEN:-}" ]; then
  	# existing jobs lookup, unchanged
  fi
  ```
- **Safety rationale:** the call and its only consumer are in the same step. The call is a fail-open single attempt with no retry loop, pagination, auth probe or emitted log key. The guard repeats exactly the predicate `tg_send_msg` applies, and it falls back to making the call when `_tg_should_send` is missing (an older CWD copy of `tg_helpers.sh`). Delivered alerts are unchanged.
- **Downstream signal:** in `forward-merge-stable-to-main.yml` "Telegram alert — hard failure", skip the jobs-API lookup unless `_tg_should_send DEBUG` passes and both `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID`/`TG_CHAT_ID` are set. Compatible with SEC-002 (switching the `curl` to `gh api`).

**DEAD-API-002 — The poll job-failure alert fetches the run's jobs even when the ERROR alert cannot be delivered**
- **Safety tag:** `SAFE_TO_MERGE`
- **Files / lines:** `.github/workflows/orchestrate_poll.yml:1835-1885` (lookup at `:1866-1877`, send at `:1885` as `"ERROR"`; `ALERT_MSG_LEVEL` comes from `env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'WARNING'` at `:1840`); filter in `scripts/tg_helpers.sh:83-91, 113-122`.
- **Current / proposed calls:** 1 per failed poll run. Proposed: 0 when Telegram is not configured (consumer repos without `TG_BOT_SECRET` or a chat ID) or when the threshold is `CRITICAL`/`SILENT` (the release-gate SILENT mode documented at `tg_helpers.sh:47-55`); otherwise 1, as today.
- **Endpoints:** REST `GET repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs` (raw `curl`).
- **Evidence:** the pattern is the same as DEAD-API-001. `failing_step` feeds only `msg`, and `msg` feeds only `tg_send_msg "${msg}" "ERROR"`, which returns early under the same two conditions.
- **Proposed fix:** the same guard as DEAD-API-001 with level `ERROR` (`_tg_should_send ERROR`), around the `if [ -n "${GH_TOKEN:-}" ]` block at `:1867`. Use a step-local variable name that does not collide with existing names. Do not change the step's `if: failure()`, env or send call.
- **Safety rationale:** this is a failure-notification step in `orchestrate_poll.yml`, not code in `orchestrate_poll_process.sh` or a race-defence path. The call is a single fail-open read consumed only within its own step, and no log output or log key changes.
- **Downstream signal:** in `orchestrate_poll.yml` "Notify Telegram on job failure", skip the jobs-API lookup unless `_tg_should_send ERROR` passes and both `TG_BOT_SECRET` and `TG_ADMIN_CHAT_ID`/`TG_CHAT_ID` are set. Keep the call when `_tg_should_send` is undefined.

### Cross-References to Deep Audit Section
- BATCH-001: RISKY_SKIP — lives in `orchestrate_poll_process.sh` (`:5059-5199`). Added detail: `_fetch_pr_json` (`:2215-2219`) has no cycle-local cache across its 36 call sites, so any sweep prefetch must be a cache read layered under it, not a replacement.
- BATCH-002: RISKY_SKIP — these are poll-script loops on the merge path. Added detail: the review-blocked branch deliberately re-fetches the PR (`:25692, 26257, 26326, 26376, 26646, 26717`) as race defence, and a wave prefetch must not feed those reads. The last-cross-reference semantics (`:4455-4470`) must be kept.
- BATCH-003: RISKY_SKIP — this degraded fallback (`gh_helpers.sh:1640-1657`) feeds stall recovery through `_issue_cross_ref_pr_number_last` (`orchestrate_poll_process.sh:20481, 21657`). Batching would turn the per-URL `ok:false` isolation (`:1653-1654`) into all-or-nothing.
- BATCH-004: NEEDS_VERIFICATION — per-heal-issue failure isolation (`workflow_failure_heal_pr_reconcile.sh:243-246`, `continue`) and the `per_page=10` head filter would become a base-filtered listing that needs pagination. Confirm stacked heal issues (N > 1) actually occur.
- BATCH-005: RISKY_SKIP — the proposal replaces `--paginate` comments (`implement.yml:5089-5090`) with `comments(last:100)` in the #1292 duplicate-sub-issue circuit breaker. REUSE-001 covers the safe part (hop 1).
- API-001: NEEDS_VERIFICATION — `tests/test_review_autofix_sweep_stale_queued.py:341-362` pins the read (#6629: "one default-branch read per tick… no fallback"; `count("--jq .default_branch") == 1`). The test and its contract docstring have to change along with the code.
- API-002: NEEDS_VERIFICATION — the two error semantics differ. The outage listing fails open (`orchestrate_poll.yml:250-252`); the tracking listing aborts under `set -e` (`:257-262`). The REST-shaped `provider_outage_trackers.json` is consumed at `:1036-1038` and pinned by `tests/test_claude_capacity_wait_call_sites.py:556`. GraphQL `issues` drops `.pull_request` and renames `author_association`.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 2 | DEAD-API-001, DEAD-API-002 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-001, REUSE-002 |
| RISKY_SKIP | 3 | REUSE-003, REUSE-004, REUSE-005 |

These counts cover only the findings raised in this section. The Deep Audit Section 2 items are tagged under Cross-References above: 4 RISKY_SKIP (BATCH-001, BATCH-002, BATCH-003, BATCH-005) and 3 NEEDS_VERIFICATION (BATCH-004, API-001, API-002).

### Implement-Stage Handoff
- DEAD-API-001 — the call is dead on every failed forward-merge run under the default `ALERT_MSG_LEVEL=WARNING`.
- DEAD-API-002 — the call is dead on failed poll runs in repos without Telegram, or under a CRITICAL/SILENT threshold.

Both save at most one call per failed run, so the gain is small; their value is that they are provably safe.