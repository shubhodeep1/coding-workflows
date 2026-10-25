## Executive Summary

- **The `Checkout PR head branch` failure is still not diagnosable, and the fix has been deferred for 6+ reports.** `review_autofix` run 40115972196 failed at `review / codex-agent` → `Checkout PR head branch` after 993s and spent 0 tokens. That step (`.github/workflows/review_autofix.yml:2832-2945`) has at least 7 different `exit 1` / `set -e` failure paths, and none of them emits a stable prefix. `REVIEW_CHECKOUT_FAIL` has been recommended in `analysis/workflow-optimization-2026-10-16.md`, `-10-16-2.md`, `-10-21-3.md` and `-10-24.md`, but a repo search (excluding `analysis/`) finds no hits in code. **Impact:** turns an unclassifiable 50% share of this window's failures into a single-line root cause. **Confidence: high** (code verified).
- **One run is two-thirds of all measured spend.** Of 158.83M OpenRouter tokens, `review_autofix` accounts for 158.73M (99.94%), and run 40142416871 alone used 106.45M (67%) across 20 calls in 3,096s. The aggregate `cache_hit_rate` of 0.912 is healthy. Run 40107619974 is the outlier at 0.805, with 3.47M uncached prompt tokens over 10 calls. **Impact:** capping per-run iteration or context on the long tail is the largest cost lever. **Confidence: medium**, because only 4 of 48 review runs have token telemetry.
- **The orchestrator poller's cadence is limited by its own run time.** It is scheduled `*/5 * * * *` (`internal-orchestrate-poll.yml:5`) with a single-run concurrency group and `cancel-in-progress: false` (`orchestrate_poll.yml:68-73`). Its p50 is 728s, p95 1,116s, max 2,876s (run 40112840093). Result: 8/61 runs were cancelled while queued (e.g. 40114098806, cancelled before its first step after 969s), and the real poll cadence is about 12–14 min, not 5. The poll step's internals are invisible because the log was truncated by about 871K characters (run 40146046089). **Impact:** phase timing would expose where the 14 minutes go. **Confidence: high** on cadence, low on the cause.
- **Semble is cheap context but has a slow, sometimes wasted bootstrap.** It returned 99,121 bytes from 8 queries and 53 sources, with only 2.1% static duplication. That is roughly 25k tokens against 11.0M prompt tokens, so it is not inflating prompts. Bootstrap took 432.6s in total (43.3s average), and 3 bootstraps (128.7s) in `workflow_log_analysis` run 40113719384 had zero queries. **Impact:** about 2 min saved per log-analysis run. **Confidence: high.**
- **All 24 `SEMBLE_FALLBACK` lines are contract-test fixtures, not runtime failures.** They are tagged `context=contract-test`: 20 in CI and 4 in `test_and_mark_stable`, with 0 runtime fallbacks. Semble is healthy, not a masked broken rollout. There was no Serena activity (0 probes, 0 queries) and no `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` lines. **Confidence: high.**
- **Headline latency numbers are distorted by skipped runs.** 666 of 1,000 runs were "other" (mostly skipped), so the overall p50 of 1s and `wall_clock_p50_ms` of 1,000 are meaningless. Every `issue_comment` fans out to clarify, plan, implement and orchestrate-clarify-respond, which are skipped at job level (`internal-plan.yml:3-5,15+`). **Impact:** a collector-only fix restores usable p50s. **Confidence: high.**
- **Data gap:** the run-logs directory `/home/runner/work/_temp/workflow-log-output` was not mounted in this sandbox. Every root-cause statement about the two failures is therefore bounded to metadata, the code shown, and the `log_summary` fields.

## Speed Optimizations

1. **Critical path: add phase timing to the orchestrator poll loop (diagnostic first).**
   - **Evidence:** Run 40146046089 executed its poll step from 00:19:28 to 00:33:47 (about 14 min) after a 3-min runner queue. The log was truncated by about 871K characters. Poll p50 is 728s and p95 is 1,116s.
   - **Root cause:** Unknown. `scripts/orchestrate_poll_process.sh` uses `date +%s` only for guards (e.g. :11844, :15051, :21403) and emits no per-phase duration line.
   - **Change:** Emit one line per phase. `ORCH_POLL_PHASE` is unused in the repo. Register it through an `agents.d/<pr>-orch-poll-phase.md` fragment (§25), and add a matching regex to the log collector so the line survives truncation.
     ```bash
     _opp_t0="$(date +%s%3N)"
     # ... existing phase body ...
     echo "ORCH_POLL_PHASE phase=<name> ms=$(( $(date +%s%3N) - _opp_t0 )) projects=${#projects[@]:-0}"
     ```
   - **Estimated savings:** none directly. It lets us target the poll's 12–14 min floor. An inferred 30% cut would be about 4 min per orchestrator wave tick.
   - **Risk:** very low (log-only).
2. **Critical path: CI `tests-release-and-log-analysis` is the long pole.**
   - **Evidence:** Run 40146681628 lists that job at 00:26:13–00:40:48 (about 14.5 min) against a total of 885s. CI p50 is 936s, p95 1,354s.
   - **Change:** Add `--durations=15` to that job's pytest invocations so the slowest tests become visible, then split them across shards.
   - **Estimated savings:** 2–4 min per CI run (inference). CI ran 33 times in this window and sits on every implement and review cycle.
   - **Risk:** low.
3. **Skip unused Semble bootstraps in `workflow_log_analysis`.**
   - **Evidence:** Run 40113719384 had 3 bootstraps and 3 unused, costing 128,725 ms.
   - **Change:** Use `SEMBLE_BOOTSTRAP_MODE=lazy` for that workflow, the same mode the poller already uses (log summary of 40146046089: `SEMBLE_BOOTSTRAP_MODE: lazy`).
   - **Estimated savings:** about 2.1 min per run.
   - **Risk:** low, since lazy mode is already in production on the poller.
4. **Long-tail review runs (critical path for PR merge).**
   - **Evidence:** 15 `review_autofix` runs took more than 1,400s: 40142416871 at 3,096s, 40115876886 at 2,785s, 40116600401 at 2,347s, and so on. The family p50 is 282.5s and p95 is 2,300s.
   - **Change:** See Cost #1. Fewer reviewer/editor iterations means a shorter wall clock.
   - **Estimated savings:** 10–25 min on p95 runs (inference).
5. **Micro-optimization: `test_and_mark_stable` takes 5,876s (98 min, run 40113559006).**
   - **Change:** Add per-step durations to the analysis input before optimizing. No step-level evidence is available.

## Cost Optimizations

1. **Bound the review/autofix long tail.**
   - **Evidence:** Run 40142416871: 20 calls, 106.45M total tokens (about 5.3M per call), 4.34M uncached prompt tokens, 303k completion tokens, `cache_hit_rate` 0.942. Next largest: 40104957702 (23.6M, 10 calls) and 40107619974 (20.5M, 10 calls).
   - **Root cause (inference):** repeated autofix/re-review iterations re-send a multi-million-token context. The cache makes each resend cheap per token, but the volume stays very large.
   - **Change:** Log the iteration count at each autofix loop boundary and the per-call `prompt_tokens` so the collector can attribute spend to iterations. Then apply the existing iteration cap more aggressively to runs that exceed about 10 calls.
   - **Estimated savings:** if the 20-call run had stopped at 10 calls, it would have used about 53M fewer tokens, roughly a third of the window's spend.
   - **Quality risk:** medium, because capping iterations may leave fixable findings unaddressed. Gate it on the finding-severity trend.
2. **Investigate cache fragmentation in run 40107619974.**
   - **Evidence:** hit rate 0.805 versus 0.886–0.942 for its peers, and 347k uncached prompt tokens per call versus 217k for 40142416871.
   - **Root cause (inference):** a dynamic segment early in the prompt, such as a timestamp, run ID or diff placed before static policy text.
   - **Change:** Move per-run dynamic blocks after the stable system and policy prefix.
   - **Estimated savings:** raising the hit rate to 0.94 would make about 1.4M fewer tokens uncached on that run.
   - **Quality risk:** none.
3. **Stop paying for cache writes that are never read in `test_and_mark_stable`.**
   - **Evidence:** run 40113559006 had 12 calls, 93,330 prompt tokens, 91,034 cache-write tokens and 0 cache-read tokens (`cache_hit_rate` 0.0). 97.5% of its prompt was written to cache and never reused.
   - **Change:** Either stabilize the prefix across its 12 calls or drop cache-control markers on that path.
   - **Estimated savings:** small, around 91k cache-write tokens per release, but it is pure waste.
4. **Semble's cost is low and its value is positive.**
   - **Evidence:** 99,121 logged bytes / 53 sources (about 1.9 KB per source), `static_dup_bytes` 2,082 (2.1%). In `review_autofix`: 87,765 bytes, 47 sources, 6 queries.
   - **Assessment:** Semble is not adding noisy context. At about 0.2% of prompt volume, it is not a cost lever. No change is needed beyond Speed #3.
5. **Serena:** 0 queries, 0 response bytes, 0 tool calls (`SERENA_ENABLED: false` in validate run 40146897582). There is nothing to evaluate.

## Reliability Improvements

1. **Add `REVIEW_CHECKOUT_FAIL` to `Checkout PR head branch`. This is the highest priority and has been outstanding for 6+ reports.**
   - **Failure evidence:** run 40115972196 (`review / codex-agent`, `Checkout PR head branch`, attempt 1, 0 tokens).
   - **Root cause category:** unknown. The step can fail at any of these points:
     - `jq` read of `PR_META_FILE` (:2836-2837, under `set -e`)
     - missing workspace (:2862)
     - OpenCode config present (:2867)
     - empty `HEAD_REF` (:2873, a plain `echo` with no `::error::`)
     - `git_ref_health_check.sh repair` failing (:2894; the script exits 1 at its own :8 or :28)
     - fetch failing after 4 attempts (:2916)
     - `git reset --hard` or `git checkout -f -B` failing (:2926/:2944)
   - **Inference:** the sibling runs 40115876886 (review, 17:23:10) and 40115972110 (CI, 17:24:27) succeeded at the same time. That points to a head-ref race, such as a deleted or force-pushed branch, rather than an infrastructure fault.
   - **Exact fix:** log-only. An EXIT trap covers both the explicit `exit 1` paths and `set -e` aborts:
     ```bash
     set -euo pipefail
     _rcf_stage="pr_meta_read"
     trap '_rcf_rc=$?; if [ "${_rcf_rc}" -ne 0 ]; then echo "::error::REVIEW_CHECKOUT_FAIL reason=${_rcf_stage} rc=${_rcf_rc} pr=${PR_NUMBER:-unknown} head_ref=${HEAD_REF:-}"; fi' EXIT
     # set _rcf_stage=workspace_check | opencode_config | head_ref_missing | ref_health_repair | fetch | checkout before each block
     ```
     Register the prefix through `agents.d/<pr>-review-checkout-fail.md` (§25). Run `wc -c .github/workflows/review_autofix.yml` to confirm the file stays under the 480,000-byte guard (§24).
   - **Expected impact:** every future failure of this step is classified on the first occurrence.
   - **Rollback:** remove the trap. Behavior is unchanged.
2. **Make the CI `Validation harness sandbox tests` failure self-describing.**
   - **Failure evidence:** run 40107619936, `orchestrate-poll (3)`, step at `ci.yml:2520-2524`. It runs `pytest -q -p no:cacheprovider tests/test_validation_harness_sandbox.py`. The review run in the same event (40107619974) succeeded.
   - **Root cause category:** unknown; flaky timing versus a real regression can't be told apart because logs weren't mounted.
   - **Fix:** change the invocation to `pytest -q -ra --durations=10 ...`. `-ra` prints a short summary line for every failed or errored test even when the middle of the log is truncated.
   - **Impact:** single-line triage.
   - **Rollback:** trivial.
3. **CI rerun:** run 40113605012 succeeded on attempt 2 (`retries=1`). Together with Reliability #2, this is the only rerun evidence. Track the per-test rerun rate once `-ra` output exists.
4. **Fallback and probe health:** all 24 `SEMBLE_FALLBACK` lines are `context=contract-test`, as classified by `_is_contract_test_semble_line` (`scripts/cost_audit.py:381-382`). This is healthy fail-open fixture behavior. There were 0 runtime fallbacks, 0 failed bootstraps, 0 Serena probe failures, and 0 `BREAK_GLASS` / `CONTEXT_BUDGET_WARN` lines, so there is no rubric or prompt-size pressure.
5. **Semble availability looks contradictory in poller run 40146046089.** The log shows `SEMBLE_AVAILABLE: false` and an empty `SEMBLE_BIN`, yet the run recorded 2 bootstraps and 2 queries (11,356 bytes). Inference: lazy bootstrap fixed availability after the env dump. Add `SEMBLE_BOOTSTRAP ... mode=lazy trigger=<target>` so this is explicit.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines are present in any supplied evidence. The `log_summary` fields for 40146687534, 40146684757 and 40146681628 explicitly state none were visible, and the deep-dive folders were not mounted.
- That leaves the retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, and `fail_open` / `enabled:false` counts all **unmeasurable** for this window.
- `memory_maintenance` ran once: 727s, success, no telemetry captured.
- **Recommendation:** add `AI_MEMORY_TELEMETRY` to the collector's structured-pattern allowlist (the same mechanism as `CONTEXT_BUDGET_WARN_RE`, `cost_audit.py:198-206`) so the lines survive log truncation. Then confirm emission on the next `review_autofix` run, the largest memory consumer.

## GH API Call Audit

- **Observed calls:** lint runs 40146684757 and 40146687534 each made one paginated `gh api repos/.../pulls/7922/commits?per_page=100` call (about 0.8s, 55 commits). No 429s or secondary rate limits appear anywhere in the supplied evidence.
- **Redundancy:** both lint runs target PR #7922, 3 seconds apart (00:26:15 and 00:26:18). `lint-pr-body-auto-close.yml:34` triggers on `[opened, edited, synchronize, reopened, ready_for_review]` and has no `concurrency:` block. Adding `concurrency: { group: lint-pr-body-${{ github.event.pull_request.number }}, cancel-in-progress: true }` removes duplicate runs. That saves 1 API call and about 9s per duplicate: a micro-optimization, low risk, and the latest run still reports the check.
- **Gap:** the poller (the main API consumer, using the `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` batches) was truncated, so per-cycle call counts are invisible. Add `calls=<n>` to the proposed `ORCH_POLL_PHASE` line for phases that touch the API. Gather that before proposing any further batching. No new unbatched pattern was identified.

## Prompt Cache & Memory System

- **Aggregate:** `cache_hit_rate` 0.912. That is 144.49M read, 2.92M written and 11.01M uncached prompt tokens. The ratio is read / (read + write + uncached), which matches the per-run values.
- **Per-run:** 40142416871 at 0.942, 40116600401 at 0.885 (0 cache writes, a pure reuse run), 40104957702 at 0.886, **40107619974 at 0.805** (the fragmentation candidate, Cost #2), and `test_and_mark_stable` 40113559006 at **0.0** (Cost #3).
- There was no `CONTEXT_BUDGET_WARN` pressure (count 0).
- **Coverage:** only 6 of 48 `review_autofix` runs and 115 of 1,000 runs overall have log telemetry (success sample rate 0.07). Token totals are therefore a lower bound.
- **Recommendation:** emit a per-call prefix hash, such as a short hash of the first N KB of the stable prefix, next to the usage line. That would show directly whether 40107619974's low hit rate comes from a changed prefix.

## Orchestrator Health

- **Wave progression is healthy.** Project #7913 reached `[ready] all 10 sub-issue(s) on #7913 are ticked` (run 40146684743). Integration PR #7922 uses `Refs #7913` with no auto-close keyword, which complies with §21. Validate ran for `tracking:7913` twice (40141408219 at 829s, 40146897582 at 705s), both successful.
- **Odd signal:** validate run 40113713982 is titled `[tracking:0]`. Inference: the tracking-ID placeholder was empty. Worth a guard or log line where the run title is composed.
- **Poll saturation:** 8 cancelled poller runs out of 61. These are queue-replacement cancellations under `cancel-in-progress: false` and they are by design, not failures. The only cost is runner-queue noise, since jobs that never started are not billed. Indicators to track:
  - poll run duration p50/p95 (currently 728s / 1,116s)
  - cancelled-before-first-step count per day
  - runner queue time (3 min in 40146046089)
- **No visibility** into clarification loops, deferrals or conflict-heal retries this window, because the poll logs were truncated. `ORCH_POLL_PHASE` (Speed #1) is the smallest safe mitigation.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| Event fan-out | Queueing/noise | Each `issue_comment` starts 4 workflows that skip at job level. That accounts for 532 of 562 runs across clarify, plan, implement and orchestrate-clarify-respond. Example burst: about 40 skipped runs between 00:29:15 and 00:29:40. |
| Clarify/plan | Compute (rare long runs) | Clarify 40113729642 took 2,921s. Plan runs took 965s, 926s, 823s and 804s. |
| Implement | Compute | 1,464s, 1,431s, 1,403s, 1,372s, 1,321s, 1,217s. |
| Review/autofix | Compute plus iteration loops | p95 2,300s. 40142416871 took 3,096s over 20 calls. |
| CI gate | Compute (one long shard) | p50 936s. `tests-release-and-log-analysis` took about 14.5 min. |
| Orchestrate poll | Compute plus queue | About 14 min per poll, 3-min runner wait, 8 queue-replacement cancels. |
| Release | Compute | `test_and_mark_stable` took 5,876s. |

Fixes in order of end-to-end impact:
1. Bound review iterations (Cost #1).
2. Poll phase timing, then targeted cuts (Speed #1).
3. Shard the CI long pole (Speed #2).
4. Collector fix: exclude `conclusion=skipped` from p50 and `wall_clock` samples, so the fan-out stops masking real latency. This is log/metrics-only, and the workflows themselves can't filter on comment body at the event level.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - review/autofix long tail (p95 2,300s)
  - poller run time of about 14 min against a 5-min cron
  - CI long shard (about 14.5 min)
- **Top failure modes:**
  - review checkout failure 40115972196, unclassified
  - CI sandbox-test failure 40107619936, unclassified
- **Highest-cost drivers:**
  - `review_autofix`: 99.94% of tokens, with 40142416871 alone at 67%
  - `test_and_mark_stable` zero-hit cache writes
- **Top 3 actions:**
  1. Land the `REVIEW_CHECKOUT_FAIL` EXIT trap and its `agents.d` fragment (Reliability #1).
  2. Add `ORCH_POLL_PHASE` timing with API call counts (Speed #1, GH API gap).
  3. Add `-ra --durations=10` to the sandbox and log-analysis pytest steps (Reliability #2, Speed #2).

## Metrics Appendix

**Run outcomes (1,000 runs)**

| Metric | Value |
|---|---|
| success / failure / cancelled / other | 322 / 2 / 10 / 666 |
| failure rate (all / non-skipped 334) | 0.2% / 0.6% |
| p50 / p95 duration (all, skip-distorted) | 1s / 1,004s |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 3,090,750 (104 samples) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**Key families**

| Family | Runs | Success | Fail | Cancel | p50 s | p95 s |
|---|---|---|---|---|---|---|
| review_autofix | 48 | 45 | 1 | 0 | 282.5 | 2,300 |
| ci | 33 | 31 | 1 | 1 | 936 | 1,354 |
| orchestrate_poll | 61 | 53 | 0 | 8 | 728 | 1,116 |
| validate | 3 | 3 | 0 | 0 | 752 | 821 |
| clarify / plan / implement / orch_clarify_respond | 147 / 138 / 138 / 139 | 7 / 12 / 8 / 2 | 0 | 0 / 0 / 1 / 0 | 1 | 10.7 / 28.6 / 292 / 11 |

**Tokens and cache**

| Scope | Calls | Prompt | Completion | Cache write | Cache read | Total | `cache_hit_rate` |
|---|---|---|---|---|---|---|---|
| All | 58 | 11,005,790 | 513,980 | 2,916,659 | 144,486,250 | 158,830,851 | 0.912 |
| review_autofix | 46 | 10,912,460 | 510,275 | 2,825,625 | 144,486,250 | 158,733,816 | 0.913 |
| test_and_mark_stable | 12 | 93,330 | 3,705 | 91,034 | 0 | 97,035 | 0.0 |
| run 40142416871 | 20 | 4,337,246 | 303,068 | 1,798,726 | 100,014,662 | 106,453,312 | 0.942 |
| run 40107619974 | 10 | 3,465,038 | 89,276 | 514,024 | 16,449,672 | 20,517,799 | 0.805 |

There were no Codex calls (`codex_calls` = 0).

**Semble**

| Workflow | Queries | Logged bytes | Sources | Static dup bytes | Bootstraps (unused) | Bootstrap ms | Fallbacks (contract-test / runtime) |
|---|---|---|---|---|---|---|---|
| review_autofix | 6 | 87,765 | 47 | 2,082 | 5 (0) | 226,798 | 0 / 0 |
| orchestrate_poll | 2 | 11,356 | 6 | 0 | 2 (0) | 77,072 | 0 / 0 |
| workflow_log_analysis | 0 | 0 | 0 | 0 | 3 (3) | 128,725 | 0 / 0 |
| ci | 0 | 0 | 0 | 0 | 0 | 0 | 20 / 0 |
| test_and_mark_stable | 0 | 0 | 0 | 0 | 0 | 0 | 4 / 0 |
| **Total** | 8 | 99,121 | 53 | 2,082 | 10 (3) | 432,595 | 24 / 0 |

**Serena / MCP availability**

| Target | `probe_ok` | `probe_failed` | `probe_skipped` | Queries | Response bytes | Tool calls |
|---|---|---|---|---|---|---|
| (none observed) | 0 | 0 | 0 | 0 | 0 | 0 |

No other MCP servers were observed.

**GH API**

| Workflow | Calls observed | Rate-limit events |
|---|---|---|
| lint_pr_body_auto_close (×2, PR #7922) | 1 paginated each | 0 |
| orchestrate_poll | not visible (log truncated) | not visible |

**Data gaps**
- The run-logs directory was not mounted.
- The poll log was truncated by about 871K characters.
- The validate log was truncated by about 366K characters.
- Token telemetry covers only 6 of 48 review runs.
- There is no `AI_MEMORY_TELEMETRY`.
- Semble per-target breakdown (`semble_targets`) was not in the supplied context.

**Next collection steps**
- Rerun this analysis with `errors/40115972196` and `errors/40107619936` mounted.
- Land the three diagnostic prefixes before the next report.