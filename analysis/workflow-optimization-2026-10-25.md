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
## Deep Audit — Workflows & Scripts (2026-10-25)

Scope: this run did not cover every file. It read in depth: `review_autofix.yml` (gate/checkout/Telegram steps), `implement.yml` (bootstrap, no-op, failure-path steps), `review_autofix_sweep.yml`, `orchestrate_poll.yml` (support-ref steps), `lint-pr-body-auto-close.yml`, `internal-review.yml`, `test-and-mark-stable.yml` (run-block inventory), and the scripts `gh_helpers.sh`, `label_helpers.sh`, `git_ref_health_check.sh`, `implement_handle_guard_block.sh`, `implement_diagnose_post_codex_failure.sh`, `review_autofix_step_evaluate_gate.sh`, `retarget_merged_base.sh`, `check_external_branch_advance.sh`, `orchestrate_force_tick.sh`, `nag_reminder.sh`, plus sampled ranges of `orchestrate_poll_process.sh`. It also ran repo-wide grep sweeps for duplicated patterns. Two findings in the report above already cover parts of this ground: `REVIEW_CHECKOUT_FAIL` (Reliability #1) and the lint `concurrency:` gap (GH API Call Audit). They are cross-referenced below, not repeated. No shell was available in this sandbox, so byte and character counts are estimates from line counts and are marked as such.

### Section 1: Bug & Correctness Sweep

**BUG-001: `GH_API_RETRY_HELPERS` is sourced in 7 workflows that never set it, so the retry wrapper is always a bare stub**
- **File:** `.github/workflows/orchestrate_poll.yml:135-144`, `:531-540`; `.github/workflows/cancel_on_pr_close.yml:43-52`; `.github/workflows/plan.yml:387-396`, `:3213`; `.github/workflows/orchestrate.yml:214`, `:388`; `.github/workflows/clarify.yml:396`, `:2364`; `.github/workflows/workflow_failure_heal.yml:118`; `.github/workflows/check_failure_triage.yml:323`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - Only 5 workflows export the variable: `implement.yml:192`, `review_autofix.yml:376`, `validate.yml:167`, `security-audit.yml:122` and `orchestrate_clarify_respond.yml:134`. Each does it from a "Bootstrap GitHub API retry helper" step.
  - The 11 sites listed above have no such step in their job. `source "${GH_API_RETRY_HELPERS:-/dev/null}"` therefore always sources `/dev/null`, and the fallback stub `gh_api_retry() { …; gh api "$@"; }` (e.g. `orchestrate_poll.yml:136`) runs with no retry.
  - At the verified sites (`orchestrate_poll.yml:135,531`, `cancel_on_pr_close.yml:43`, `plan.yml:387`), the unretried call is the promote-candidate branch read. A single transient 5xx is swallowed by `|| true`. The step then logs `SUPPORT_REF source=promote_candidate_unprotected` (`orchestrate_poll.yml:143`, `cancel_on_pr_close.yml:51`), which reports an API failure as a protection-state result. It also silently keeps `github.sha` as the support ref instead of the promote-candidate tip.
  - The other sites (`orchestrate.yml:214,388`, `clarify.yml:396,2364`, `plan.yml:3213`, `workflow_failure_heal.yml:118`, `check_failure_triage.yml:323`) were only grep-sampled. **[NEEDS VERIFICATION]** that they guard the same call.
- **Recommended fix:**
  - Add the existing bootstrap step (`implement.yml:147-195`, `continue-on-error: true`) as the first step of each affected job.
  - In the support-ref step, emit a distinct additive log value `SUPPORT_REF source=promote_candidate_read_failed` when `promote_candidate_json` is empty, and keep `promote_candidate_unprotected` for `.protected != true`.
  - No new API calls (§14).

**BUG-002: In `Handle no-op implementation`, the unguarded comment POST runs before the failure label, so a failed POST strands the issue**
- **File:** `.github/workflows/implement.yml:5422`, `:5631-5648`; same pattern at `:6791-6792` and `:6825-6826`
- **Severity:** Medium
- **Description:**
  - The step runs under `set -euo pipefail` (`:5422`). `gh_retry gh api ".../comments" -f body="${noop_body}"` (`:5631-5632`) has no `|| …`, and `gh_retry` makes a single attempt for POST creates (`gh_helpers.sh:1062-1063`).
  - If that POST fails, the step exits before `ensure_label_exists "ai:implementation-failed"` / `gh issue edit … --add-label 'ai:implementation-failed'` (`:5637-5648`).
  - The `failure()` path then runs `Comment on issue failure`. While `ai:implementing` is present, that step restores `ai:awaiting-approval` (`:6813-6819`). The issue is routed back to approval instead of the "Labeling `ai:implementation-failed` for orchestrator re-processing" path promised at `:5627` (inference on downstream routing).
  - Every other write in the same step ends in `|| true` (`:5458`, `:5465`, `:5469`), so this one is inconsistent.
  - At `:6791-6792` the BLOCKED comment POST follows the latch write. A failure there skips the `IMPLEMENT_BLOCKED_TERMINALIZED` log line (`:6793`).
- **Recommended fix:**
  - Apply the label first (`:5637-5648`), then post the comment with `|| echo "::warning::IMPL_NOOP_COMMENT_FAILED issue=${ISSUE_NUMBER}"`.
  - At `:6791` and `:6825`, append `|| echo "::warning::…"` so the comment stays best-effort, as it already is at `:5469`.

**BUG-003: On `workflow_dispatch`, the review gate's draft and `[skip ai]` checks read inputs that `internal-review.yml` blanks**
- **File:** `scripts/review_autofix_step_evaluate_gate.sh:88`, `:116-121`, `:160-167`, `:744`; `.github/workflows/internal-review.yml:59-64`
- **Severity:** Medium
- **Description:**
  - For dispatch events, `internal-review.yml` passes `pr_is_draft: false` and empty `pr_title` / `pr_body` (`:59-64`).
  - The gate tests `PR_IS_DRAFT` at `:160` and `PR_TITLE` / `PR_BODY` for `[skip ai]` at `:163-164`.
  - The live PR read at `:88` already returns `title` and `body`, which are cached into `pr_title_cached` / `pr_body_cached` at `:116-121`. The `[force-review]` check at `:398` uses the cached value; the skip-ai check at `:163` and the sync-skip force-review check at `:744` do not.
  - The `--jq` projection at `:88` does not request `draft`. As a result, a dispatched review of a draft or `[skip ai]` PR runs the full panel.
  - The sweep pre-filters both conditions (`review_autofix_sweep.yml:230`, `:467-468`). Other dispatchers (post-commit retrigger, editor-changes-lost redispatch, poller) were not audited. **[NEEDS VERIFICATION]** whether those dispatchers intend to bypass the checks.
- **Recommended fix:**
  - Add `draft: (.draft // false)` to the existing `--jq` at `:88` (same call, §14).
  - Use `pr_title_cached` / `pr_body_cached` at `:163-164` and `:744`.
  - When the event is not `pull_request`, derive the draft flag from the live read.

**BUG-004: `orchestrate_force_tick.sh` passes the full PR JSON and issue body as a single argv argument**
- **File:** `scripts/orchestrate_force_tick.sh:66`, `:72`, `:280`; `:46`, `:292`
- **Severity:** Low
- **Description:**
  - `_force_tick_fetch_pull_request_json` returns the full `pulls/N` payload (`:66`). That payload is handed to `python3 - "${pr_json}"` (`:72`), and the issue body is passed the same way (`:46`).
  - Linux caps a single argument at 131,072 bytes (MAX_ARG_STRLEN). A 65,536-character body with multi-byte UTF-8, plus PR metadata, can exceed it.
  - Under `set -euo pipefail` (`:3`), the `E2BIG` failure aborts the assignment at `:280`. The script then fails instead of taking its documented skip path. **[NEEDS VERIFICATION]**: inference from the argv limit, not reproduced.
- **Recommended fix:**
  - Project at the source: `gh api … --jq '{head:{ref:.head.ref},base:{ref:.base.ref},body:.body}'`.
  - Feed the Python helpers through stdin (`python3 - <<'PY'` reading `sys.stdin`) instead of argv.

**BUG-005: Two more uninstrumented abort paths in `Checkout PR head branch` (adds to Reliability #1)**
- **File:** `.github/workflows/review_autofix.yml:2836-2837`, `:2850`
- **Severity:** Low
- **Description:**
  - The report's failure-path list includes the `jq` read of `PR_META_FILE`. Two details are not listed:
    - `:2836-2837` use `jq -r` with no existence check. A missing file aborts under `set -e`, and a non-object yields `null`, which surfaces as the plain-echo exit at `:2873`.
    - `:2850` expands `"${PR_PAYLOAD_FILE}"` inside `$( … || true)` under `set -u`. If the variable were unset, the unbound-variable error exits the subshell before `|| true` runs, and the assignment then trips `set -e` (inference; `PR_PAYLOAD_FILE` is normally exported by `scripts/review_autofix_step_init_runtime_workspace.sh:44`).
  - Neither path emits a stable prefix.
- **Recommended fix:** use Reliability #1's EXIT trap with `_rcf_stage=pr_meta_read` before `:2836` and `_rcf_stage=stale_head_probe` before `:2847`. Add `[ -s "${PR_META_FILE:-}" ] || { echo "::error::REVIEW_CHECKOUT_FAIL reason=pr_meta_missing"; exit 1; }`.

**SEC-001: The external-advance spoof check accepts the GitHub author login, which GitHub assigns from the commit email**
- **File:** `scripts/check_external_branch_advance.sh:180-201`
- **Severity:** Low
- **Description:**
  - A commit counts as "ours" if either `.author.login` or `.committer.login` equals `AUTOFIX_BOT_LOGIN` (`:196-197`).
  - GitHub assigns `.author.login` from the commit's author email. Anyone who can push to the head branch can set the bot's noreply email and get `ADVANCE=self_only`. The editor would then proceed against a base that an external push changed.
  - Exposure is limited to same-repo heads: the checkout step rejects fork heads (`review_autofix.yml:2888-2892`). **[NEEDS VERIFICATION]**: inference from GitHub's email-based attribution.
- **Recommended fix:** also require `.commit.verification.verified == true`, or require that `.committer.login` alone matches. Return both fields from the same call (see BATCH-003, which reduces it to one compare call).

**Checked, no finding:**
- `git_ref_health_check.sh` (quoting, `set -euo pipefail`, repair/verify loop) is correct.
- `lint-pr-body-auto-close.yml` passes title/body through `env:` (`:59-61`), so there is no injection. Its missing `concurrency:` block is already in the GH API Call Audit.
- `nag_reminder.sh` fails open correctly.
- `retarget_merged_base.sh` handles 404 vs other errors correctly (`:64-71`).

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001: `replay_failed_reclarify_commands` makes 2 REST calls per queued issue**
- **File:** `scripts/orchestrate_poll_process.sh:42-101`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:**
  - For each `ai:reclarify-requeue` issue, the loop at `:53-100` makes one `issues/N` GET (`:55`) to check `state` / `labels` / `pull_request`, then one paginated comments GET (`:58`).
  - The header comment (`:39-41`) documents this cost. The batched helper `_fetch_blocked_issue_routing_graphql` is defined directly below (`:102-130`, 25 aliases per call) and already returns labels.
- **Calls:** current 1 list + 1 `user` + 2N (+ extra comment pages). Proposed 1 + 1 + ceil(N/25) + N.
- **Recommended fix:**
  - Add a sibling alias query on `_fetch_blocked_issue_routing_graphql`'s pattern that returns `number state labels(first:100){nodes{name}}`. Read it before the loop.
  - Keep the paginated REST comments read: `:59-60` requires the complete comment history before a replay is authorized.
  - Fail open on a batch failure by falling back to the current per-issue GET.

**BATCH-002: `security_pass_unblock_filed_advisory_followups` makes one issue GET per follow-up row**
- **File:** `scripts/orchestrate_poll_process.sh:7927-7989` (GET at `:7948`)
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** each unchecked row costs one `issues/N` GET for state and labels. The header (`:7918-7922`) notes that the candidate-details batch is scoped to other issues; it does not consider a new alias batch.
- **Calls:** current N GET + B comment reads + ≤B POST (B = blocked rows). Proposed ceil(N/25) + B + ≤B.
- **Recommended fix:** collect the row issue numbers from `rows_json` (`:7933-7937`) and fetch `state` / `labels` with one aliased GraphQL query, following `_fetch_blocked_issue_routing_graphql`'s contract and batch size of 25. Fall back to the current per-row GET if the batch fails.

**BATCH-003: `check_external_branch_advance.sh` makes one GET per self-like commit**
- **File:** `scripts/check_external_branch_advance.sh:175-202`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** each `[ai-autofix]` / `[ai-merge-resolve]` SHA costs one `commits/{sha}` call (`:182`). The comment at `:175-177` acknowledges the per-commit cost.
- **Calls:** current K. Proposed 1.
- **Recommended fix:**
  - Make one `gh_retry gh api "repos/${REPOSITORY}/compare/${LOCAL_HEAD_SHA}...${REMOTE_TIP_SHA}" --jq '.commits[] | [.sha, (.author.login // ""), (.committer.login // ""), (.commit.verification.verified // false)] | @tsv'` call and match SHAs locally. The compare endpoint returns up to 250 commits.
  - Keep the `ADVANCE=unknown` fail-open path when the call fails.

**BATCH-004: The no-op ancestor-chain walk makes 2 REST calls per hop**
- **File:** `.github/workflows/implement.yml:5555-5577`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** each hop reads the current issue body (`:5565`) and then the parent's comments (`:5569-5570`). `:5555` documents "up to 2 * threshold calls".
- **Calls:** current 2T (default T=2, so 4). Proposed T+1: one GraphQL `issue(number:){ body comments(last:100){ nodes{ body } } }` per parent returns both the next parent pointer and the no-op marker evidence.
- **Recommended fix:** use the alias shape from `_fetch_blocked_issue_routing_graphql`, and keep the existing fail-open `break`.

**BATCH-005: The sweep's active-run snapshot makes 6 paginated listings per tick**
- **File:** `.github/workflows/review_autofix_sweep.yml:335-348`, `:437-439`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** 3 statuses (`queued`, `in_progress`, `pending`) × 2 workflows, each a `--paginate` listing. `:322-323` notes that the endpoint has no multi-status filter.
- **Calls:** current ≥6. Proposed ≥3: query `repos/${REPOSITORY}/actions/runs?status=<s>` once per status and filter `.path` client-side for `internal-review.yml` / `review_autofix.yml`. The jq at `:380` already inspects `.path`.
- **Recommended fix:** as above. **[NEEDS VERIFICATION]**: a busy repo-wide queue could need more pages than two workflow-scoped listings, so measure page counts before switching.

**API-001: The sweep re-fetches `default_branch`, which is already in its PR listing**
- **File:** `.github/workflows/review_autofix_sweep.yml:228-237`, `:285`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:**
  - `:285` makes a separate `repos/${REPOSITORY}` GET for `.default_branch`. It runs only when there is at least one candidate, i.e. exactly when the `pulls` listing at `:228` is non-empty.
  - Each `pulls` item carries `.base.repo.default_branch` **[NEEDS VERIFICATION]**.
- **Calls:** current 1 per active tick. Proposed 0.
- **Recommended fix:** add `default_branch: .base.repo.default_branch` to the map at `:230-237`. Read it from `prs_json[0]` and keep the existing fail-closed regex check at `:286`.

**API-002: `orchestrate_force_tick.sh` looks up the same number twice in sequence, without retry**
- **File:** `scripts/orchestrate_force_tick.sh:57-67`, `:103-115`, `:274-296`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** when no tracking issue is supplied, the script calls `pulls/N` and then `issues/N` for the same number. Both are raw `gh api` calls with no retry. For a plain issue, the first call always 404s.
- **Calls:** current 2. Proposed 1: GraphQL `repository{ issueOrPullRequest(number:N){ … on Issue{body} … on PullRequest{body headRefName baseRefName} } }`.
- **Recommended fix:** as above. Source `gh_helpers.sh` with the stub fallback used in `implement_diagnose_post_codex_failure.sh:52-53`, and route the call through `gh_retry`.

**Tight-retry review:**
- `gh_api_retry` (`gh_helpers.sh:910-1035`) classifies failures and uses exponential backoff with a cap.
- The 3× outer loop at `implement_diagnose_post_codex_failure.sh:259-278` wraps `gh_retry`. That is a deliberate race-window retry with a 4s sleep, and invalid responses break immediately (`:261-263`). No finding.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001: The support-ref and promote-candidate resolution block is inlined 17 times across 10 workflows**
- **File:** e.g. `.github/workflows/orchestrate_poll.yml:122-159`, `:518-555`; `.github/workflows/plan.yml:374-410`; `.github/workflows/cancel_on_pr_close.yml:30-60`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - `promote_candidate_workflow_ref=` appears 17 times. Copies: `implement.yml` 4, `orchestrate.yml` 2, `orchestrate_poll.yml` 2, `clarify.yml` 2, `plan.yml` 2, and one each in `cancel_on_pr_close`, `workflow_failure_heal`, `check_failure_triage`, `memory_maintenance`, `orchestrate_clarify_respond`.
  - The copies differ only in the filename embedded in the regex (e.g. `plan\.yml` at `plan.yml:384`, `orchestrate_poll\.yml` at `orchestrate_poll.yml:132`).
  - BUG-001 shows the copies have already drifted on the retry bootstrap.
- **Recommended fix:**
  - Add `scripts/resolve_support_ref.sh` and fetch it in the same git-transport bootstrap that already pulls `gh_helpers.sh` and `emit_event.sh` (`implement.yml:185-186`). It must be fetched that way because it runs before any support checkout.
  - Signature, using CLAUDE.md §9 style:
    ```bash
    resolve_support_ref()
    {
    	# $1 workflow file basename (e.g. plan.yml); $2 SUPPORT_REF_WORKFLOW_JOB_JSON
    	# prints SCRIPT_REF; logs SUPPORT_REF source=... unchanged
    }
    ```
  - Update all 17 callers. Until then, add a copy-parity test like the one that keeps both `[skip ai]` matcher copies identical (`review_autofix_sweep.yml:257-261`).

**DUP-002: The "Bootstrap GitHub API retry helper" step exists in only 5 workflows; the stub fallback is repeated 33 times**
- **File:** `.github/workflows/implement.yml:147-195`; setters at `review_autofix.yml:376`, `validate.yml:167`, `security-audit.yml:122`, `orchestrate_clarify_respond.yml:134`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - The ~45-line clone-and-verify loop is copied 5 times. The 7 workflows in BUG-001 lack it.
  - The `type gh_api_retry … || gh_api_retry() { … }` stub is repeated at every source site (33 total).
  - `type gh_retry &>/dev/null || gh_retry() { "$@"; }` is repeated 12 times in `implement.yml` alone.
- **Recommended fix:**
  - Keep the bootstrap inline, since it has to run before support staging, but copy it verbatim into the 7 missing workflows.
  - Add a test asserting the copies are byte-identical, so they cannot drift again.
  - Inside `implement.yml`, source the staged `gh_helpers.sh` once per step and drop the per-step stubs after `Stage workflow support files` (`:1428`) has succeeded.

**DUP-003: An inline Telegram `sendMessage` curl appears at 10 sites outside `tg_helpers.sh` / `gh_helpers.sh`**
- **File:** `.github/workflows/implement.yml:5526-5530`; `scripts/implement_handle_guard_block.sh:162-166`, `:292-296`, `:464-468`; one each in `test-and-mark-stable.yml`, `validation-refresh.yml`, `check_failure_triage.yml`, `issue_pr_status.yml`; two in `update_workflows.yml`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** the reason for inlining is stated at `implement.yml:5515-5516`: `tg_helpers.sh` may be removed by consumer cleanup. Each copy hand-rolls level, escaping and failure handling.
- **Recommended fix:**
  - Add `tg_send_critical_raw <text>` to `scripts/tg_helpers.sh`, and stage that file next to the guard handler in `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` (which the handler already relies on for `gh_helpers.sh`, `implement_handle_guard_block.sh:12`). Then replace the three copies in the guard handler and the one in `implement.yml`.
  - **[NEEDS VERIFICATION]** that the staged directory survives the cleanup step.

**DUP-004: An inline GH_PAT budget reader is repeated 19 times, alongside the shared `gh_pat_budget()` helper**
- **File:** e.g. `.github/workflows/review_autofix.yml:1794-1806` (12 copies in that file), `clarify.yml` (3), `workflow-failure-heal-intake.yml` (2), `orchestrate_poll.yml:109` (1), `validation-improvements-intake.yml` (1); helper at `scripts/gh_helpers.sh:31-77`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** start-of-job copies are needed because the helper is not staged yet (`orchestrate_poll.yml:107-108`). End-of-job copies run after staging and could use the helper.
- **Recommended fix:** for `phase=end` steps, use the pattern at `review_autofix_sweep.yml:583-588`: prefer `gh_pat_budget end <workflow> <job> "${GH_PAT_BUDGET_FILE}"` and keep the inline fallback only when the helper is absent. **[NEEDS VERIFICATION]** which end steps can see `SUPPORT_SCRIPTS_DIR`.

**DUP-005: Inline `gh label create` calls bypass `ensure_label_exists`, and their color/description values drift**
- **File:** `.github/workflows/implement.yml:5456-5458`, `:5503-5505`, `:5590-5592`, `:5641-5643`, `:6773-6775`, `:6814-6816`; `scripts/implement_diagnose_post_codex_failure.sh:222-224`, `:242-247`; `scripts/implement_handle_guard_block.sh:35-66`, `:98-100`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** `scripts/label_helpers.sh:163-197` already provides an idempotent `ensure_label_exists` backed by the contract catalog. `implement_handle_guard_block.sh` defines its own variant (`guard_block_ensure_label`). The resulting value drift is listed under CONSIST-001.
- **Recommended fix:** source `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/label_helpers.sh` where it is present (as `implement.yml:5637-5639` already does) and call `ensure_label_exists <label> "${REPO}"`. Keep the inline call only as the fallback branch, with values copied from `.github/ai/label_contract.v1.json`.

### Section 4: Expression Size Limit Risk Assessment

**Context:**
- `tests/test_workflow_file_size_limit.py:28-32`, `:66-98` already fails CI for any interpolated `run:` body at or above 18,000 characters, measured as `len(run)`. Assuming that test passes on main, no block is at the 85% (High) threshold.
- The workflow file-size limit this repo enforces is 512,000 bytes, with a 480,000-byte guard (`:4-5`, `:26-27`), not 1 MB. While that guard passes, no workflow can exceed 800 KB.
- All counts below are estimates from line spans (no shell in this sandbox). Each is marked **[NEEDS VERIFICATION]**; measure with `python3 -c 'import yaml,sys; …len(step["run"])'`.

**EXPR-001: `test-and-mark-stable.yml` "Phase 3b: Wait for PR creation", a roughly 345-line body made an expression by one `${{ }}`**
- **File:** `.github/workflows/test-and-mark-stable.yml:1350-1695` (expression at `:1388`)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** the only interpolation is `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"`. It turns the whole body into one expression. **Estimated** 15,000–18,000 characters (the guard sets the ceiling), leaving about 3,000–6,000 of headroom.
- **Recommended fix:** move the value to `env:` as `IMPL_CREATED_AFTER: ${{ … }}`. The same file already uses this pattern at `:1995-1999` ("Promoted to env so the run: script body contains no `${{ }}`"). The block then leaves the expression budget entirely.

**EXPR-002: `implement.yml` "Handle no-op implementation", a roughly 228-line body with long prose lines**
- **File:** `.github/workflows/implement.yml:5421-5649` (expressions at `:5426`, `:5428`, `:5631`, `:5639`, `:5641`, `:5645`, `:5647`)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** every interpolation is `github.server_url`, `github.repository` or `github.run_id`. Lines such as `:5451`, `:5491`, `:5498`, `:5585` and `:5587` are 240–330 characters of comment-body prose that will keep growing. **Estimated** 15,000–17,500 characters, leaving about 3,500–6,000 of headroom.
- **Recommended fix:** replace the interpolations with `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`, which the runner always sets, so the body stops being an expression. Longer term, move the three comment templates to `prompts/` or into a `scripts/implement_handle_noop.sh` step script, following the `review_autofix_step_<slug>.sh` pattern (§24).

**EXPR-003: `test-and-mark-stable.yml` "Phase 2: Wait for plan to complete"**
- **File:** `.github/workflows/test-and-mark-stable.yml:1043-1312` (expressions at `:1061`, `:1073`, `:1091`, all `steps.create-issue.outputs.created_after`)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** a roughly 270-line polling body. **Estimated** 12,000–16,000 characters, leaving about 5,000–9,000 of headroom.
- **Recommended fix:** promote `created_after` to `env:` once, as in EXPR-001.

**EXPR-004: `review_autofix.yml` Telegram alert templates that grow with each new judge outcome**
- **File:** `.github/workflows/review_autofix.yml:5267-5416` ("Telegram review-blocked judge decision", expressions at `:5271-5272`, `:5276`); `:5114-5252` ("Telegram editor-noop-suspicious warning", expressions at `:5118-5119`, `:5178`, `:5229`, `:5251`)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** both are message-template case ladders (`:5290-5410` adds one arm per new `judge_action` / `judge_skip_reason`). **Estimated** 10,000–13,000 and 9,000–12,000 characters respectively. Every interpolation is a `github.*` value with an equivalent runner env var.
- **Recommended fix:** replace `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` with `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. That removes both bodies from the limit at no cost. Optionally move each body to `scripts/review_autofix_step_tg_<slug>.sh`.

**EXPR-005: The CI guard counts raw `len(run)`, which may be lower than what GitHub counts**
- **File:** `tests/test_workflow_file_size_limit.py:87`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - GitHub evaluates an interpolated body as one `format()` expression.
  - Public incident write-ups report that its count includes escaping overhead: doubled braces and quotes, and per-line overhead. One measured `len(parsed_run) + run.count('\n')`. A script of about 20.9K characters was rejected after that accounting.
  - Bash bodies here are dense with `${VAR}`. A body that passes the 18,000 raw guard could exceed 21,000 effective characters. That would fail at startup as a zero-job run, which the size test cannot catch. **[NEEDS VERIFICATION]**: the exact accounting is not documented by GitHub.
  - Sources: [tuist/tuist#14472](https://github.com/tuist/tuist/pull/14472), [Abilityai/trinity#2625](https://github.com/Abilityai/trinity/issues/2625), [FRIKKern/barkpark#19307](https://github.com/FRIKKern/barkpark/issues/19307), [rhysd/actionlint#356](https://github.com/rhysd/actionlint/issues/356).
- **Recommended fix:** compute a conservative effective length, e.g. `len(run) + run.count("\n") + run.count("{") + run.count("}") + run.count("'")`, and compare that against `RUN_EXPRESSION_GUARD_CHARS`. Report both numbers in the failure message. Do not raise the guard.

**EXPR-006: File size headroom and repeated long `if:` conditions in `implement.yml`**
- **File:** `.github/workflows/implement.yml` (7,174 lines); `:5414`, `:6699`, `:6752`, `:6829`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - At 7,174 lines, the largest workflow could plausibly sit around 430–490 KB, close to the 480,000-byte guard **[NEEDS VERIFICATION]**. For comparison, `review_autofix.yml` was at 505,283 bytes before its split, per the test docstring `:9`.
  - The same roughly 550-character guard-output conjunction (`steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' && … staged_support_rebase_conflict == ''`) is repeated in the `if:` at `:6699`, `:6752` and `:6829`, with a subset at `:5414`. Each `if:` is far below 21,000 characters, but the repetition adds bytes and invites drift.
- **Recommended fix:**
  - Run `wc -c .github/workflows/implement.yml`. If it is above about 430,000 bytes, move the largest inline bodies (e.g. `Create Pull Request`, `:6068-6221`) into `scripts/` per §24.
  - Compute one `guard_block_active` output in a tiny step after `commit_changes`, and reference that output in the four `if:` conditions.

### Section 5: Cross-Cutting Concerns

**DEAD-001: `gh_rate_limit_breaker_tripped` has no callers**
- **File:** `scripts/gh_helpers.sh:336-341` (comment at `:313-318`)
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** a repo-wide grep over workflows, scripts and tests finds only the definition and its comments. The comment notes that the `orchestrate_poll.yml` consumer was removed. The breaker file is still read by `gh_rate_limit_breaker_active` (`:894-902`).
- **Recommended fix:** keep the function, since removing it is a §10 change, but mark it deprecated in its header comment. Alternatively, add a test that pins its contract so it stays intentional.

**CONSIST-001: Label colors and descriptions created inline drift from the label contract**
- **File:** `.github/workflows/implement.yml:5456-5458`, `:5590-5592`, `:5503-5505`, `:6773-6775`; `scripts/implement_diagnose_post_codex_failure.sh:222-224`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** compared with `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh:23-151`:

  | Label | Location | Inline value | Contract value |
  |---|---|---|---|
  | `ai:closed` | `implement.yml:5456-5458`, `:5590-5592` | `6b7280` / "Issue closed by automation (superseded, duplicated, or already complete)" | `6a737d` / "Linked PR closed without merge" |
  | `ai:needs-human` | `implement.yml:5503-5505` | `b60205` / "Automation halted — human review required before re-dispatch" | `e11d48` / "Escalated for human intervention; autonomous stall recovery is paused" |
  | `ai:blocked` | `implement.yml:6773-6775` | "Blocked: human input required" | "Orchestrator auto-answer loop blocked pending human intervention" |
  | `ai:implementation-failed` | `implement_diagnose_post_codex_failure.sh:222-224` | different description | catalog description |

  Whichever call runs first in a fresh consumer repo wins, until `sync_ai_labels.yml` (`:116-124`, `sync-labels`) reconciles it.
- **Recommended fix:** apply DUP-005. Until then, copy the contract values verbatim into the fallbacks.

**CONSIST-002: Some calls use raw `gh api` even where a retry wrapper is available**
- **File:** `.github/workflows/review_autofix_sweep.yml:228`, `:285` (`gh_api_retry` is loaded at `:216-220`); `scripts/orchestrate_force_tick.sh:66`, `:114`; `scripts/retarget_merged_base.sh:54`, `:64`, `:150`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - A transient 5xx on the sweep's open-PR listing (`:228`) fails the whole step under `set -euo pipefail` (`:213`), while the snapshot calls in the same step retry (`:340`).
  - `retarget_merged_base.sh` fails open, so a single blip skips retargeting for that run.
- **Recommended fix:**
  - Route the read-only GETs through `gh_api_retry` (sweep) or `gh_retry` (scripts; source `gh_helpers.sh` beside the script as `check_external_branch_advance.sh:63-67` does).
  - Leave the non-idempotent `PATCH` at `retarget_merged_base.sh:150` single-attempt.

**CONSIST-003: The `AUTOFIX_SWEEP_END` log line has a different field set on the early-exit path**
- **File:** `.github/workflows/review_autofix_sweep.yml:288` vs `:274`, `:552`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** the `default_branch_unavailable` exit omits `skipped_merge_queued=` and `snapshot_incomplete=`, which `:552` emits and `:274` partly emits. Log consumers that parse the key set will see gaps.
- **Recommended fix:** emit the same key set at all three sites, with zero or `false` defaults.

**CONSIST-004: The poller's initial `SEMBLE_AVAILABLE=false` explains Reliability #5's contradiction**
- **File:** `.github/workflows/orchestrate_poll.yml:96-97`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `Create runtime workspace` writes `SEMBLE_AVAILABLE=false` and an empty `SEMBLE_BIN=` to `GITHUB_ENV` before the lazy bootstrap (`:49`, `SEMBLE_BOOTSTRAP_MODE` defaults to `lazy`). The env dump in run 40146046089 therefore shows the pre-bootstrap default, not a failed install. This cross-references Reliability #5 and does not duplicate it.
- **Recommended fix:** after a lazy bootstrap succeeds, emit one state line (the `SEMBLE_BOOTSTRAP … mode=lazy trigger=<target>` proposed in Reliability #5) and update `GITHUB_ENV`, so the env dump stops being the only signal.

**SHELL-001: Indentation and brace style are mixed against CLAUDE.md §9**
- **File:** `scripts/implement_handle_guard_block.sh:21-87` (tabs) vs `:92-477` (2 spaces); `scripts/implement_diagnose_post_codex_failure.sh:100`, `:155`, `:216`, `:235` (`name() {` on one line) and `:165-178` (tabs inside a 2-space file)
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** this is a style-only inconsistency, with no behavior impact.
- **Recommended fix:** normalize these to tabs and next-line braces only when the files are next edited for a functional reason. A standalone reformat would violate §9.

**SHELL-002: Benign shellcheck patterns**
- **File:** `scripts/implement_diagnose_post_codex_failure.sh:277` and `scripts/orchestrate_force_tick.sh:442` (SC2015, `A && B || C`); `.github/workflows/review_autofix.yml:5196-5197` (SC2012, `ls -1 … | wc -l`)
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** each is safe in context (the right-hand side is idempotent). A future edit to the middle command could change the semantics.
- **Recommended fix:** rewrite as `if …; then …; fi`, and count files with `find "${PREVIOUS_REVIEWS_DIR}" -maxdepth 1 -name 'editor_attempt_*.err' | wc -l`.

**DEBT-001: Repeated `jq` forks over the same JSON in hot paths**
- **File:** `scripts/review_autofix_step_evaluate_gate.sh:100-115` (15 `jq` invocations on `_pr_gate`); `.github/workflows/review_autofix_sweep.yml:445-448` (4 `jq` forks per PR row)
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** the cost is CPU and fork overhead only; there are no API calls.
- **Recommended fix:** use a single `jq -r '[.state, .head_ref, …] | @tsv'` with `IFS=$'\t' read -r …`. Keep the per-field defaults.

**Markers:** no `TODO`, `FIXME` or `HACK` markers in `.github/workflows/*.yml`, `scripts/*.sh` or `scripts/*.py`. Grep matches for `XXX` are only `mktemp` templates.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 11 | BUG-001, BUG-002, BUG-003, BATCH-001, BATCH-002, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-003, EXPR-005 |
| Low | 21 | BUG-004, BUG-005, SEC-001, API-001, API-002, BATCH-003, BATCH-004, BATCH-005, DUP-003, DUP-004, DUP-005, EXPR-004, EXPR-006, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 0 (no Critical/High findings) | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/check_external_branch_advance.sh`, `scripts/orchestrate_force_tick.sh`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/implement.yml` | Medium |
| Code modularization | 10 workflows carrying the support-ref and bootstrap copies; new `scripts/resolve_support_ref.sh`; `scripts/tg_helpers.sh`; `scripts/label_helpers.sh` callers in `implement.yml` and the two implement scripts; one copy-parity test | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | 7 workflows for BUG-001; `.github/workflows/implement.yml`; `scripts/review_autofix_step_evaluate_gate.sh`; `scripts/orchestrate_force_tick.sh`; `scripts/check_external_branch_advance.sh`; `scripts/retarget_merged_base.sh`; `.github/workflows/review_autofix_sweep.yml`; `.github/workflows/orchestrate_poll.yml`; `scripts/gh_helpers.sh` | Medium |