## Executive Summary

- **One deterministic refusal causes 8 of the 9 failures (89%) in the window.** Six clarify runs (38959970027, 38983115876, 38983214967, 38983537115, 38984124962, 38986413434) and two plan runs (38969365068, 38976601176) all failed at step `Resolve integration ref`, each in 22–31s. Counting only runs that actually executed, clarify failed 6 of 14 (43%) and plan failed 2 of 9 (22%). Issue #7516's clarify question concerns `clarify.yml` handling `reason=label_missing`. That reason is emitted by `refuse("label_missing")` in `scripts/resolve_integration_ref.sh:225`, which leads to exit 3 and then `exit 1` at `clarify.yml:205-207`. **Impact:** fixing it removes about 89% of failures and the follow-on heal runs they trigger. **Confidence:** medium, because the per-run reason lines are not in the supplied telemetry.
- **Each of these failures starts a Claude Opus heal run.** Heal-intake runs began within about 1s of each clarify failure ending:
  - clarify 38983214967 ended ~23:47:25, intake 38983250046 started 23:47:24 (278s)
  - clarify 38983537115 ended ~23:51:13, intake 38983567905 started 23:51:12 (116s)
  - clarify 38984124962 ended ~23:58:39, intake 38984163249 started 23:58:38 (77s)
  - clarify 38986413434 ended ~00:27:24, intake 38986445246 started 00:27:23 (72s)

  That is 543s of `claude-opus-5-5 effort=high` on one recurring root cause. **Impact:** about 80% of heal-intake model time in this burst could be saved by de-duplicating on the reason. **Confidence:** high for the timing; the causal link is an inference.
- **Codex token telemetry is broken.** It reports 51 calls but only 19 tokens. Run 38984124962 failed at `Resolve integration ref`, before `Install Codex CLI` (`clarify.yml:247`), yet it shows `codex_calls=6`. The `CODEX_TOKENS_RE` pattern in `scripts/cost_audit.py:62-64` matches the script text at `clarify.yml:1524` (`grep -A1 'tokens used' … | tail -1`) and captures the `1` from `tail -1`. **Impact:** Codex spend is currently unmeasured. **Confidence:** high, reproducible from the source.
- **Review/autofix is the most expensive and slowest AI path.** Three runs with telemetry used 9.24M OpenRouter tokens, about 3.08M each. Cache hit rate was 0.595 overall but varied from 0.451 (38906580296) to 0.736 (38916979717). p95 duration is 2,350s. **Impact:** raising the low-hit runs to about 0.70 would save roughly 0.4M uncached prompt tokens per run. **Confidence:** medium (n=3).
- **The analysis window is truncated and the deep-dive logs were missing.** `total_runs` is exactly 1,000, which looks like the collector cap, and 684 of those runs (68.4%) are `other`/skipped. The window therefore covers only about 12 hours (2026-10-16 12:47 to 2026-10-17 00:28). The `workflow-log-output` directory (errors/slow/recent) was not mounted, so every per-run claim below rests on the `analysis_context.json` rows, the `log_summary` fields, and the workflow and script source. **Confidence:** high.
- **Semble is healthy but has bootstrap overhead.** It had 0 runtime fallbacks; all 12 fallbacks were contract-test fallbacks from CI. Run 38903068381 (`workflow_log_analysis`) spent 128.4s on 3 bootstraps that were never used. Review/autofix spent about 37.4s of bootstrap per useful query. **Impact:** about 2 minutes saved per log-analysis run. **Confidence:** high.

## Speed Optimizations

1. **Stop re-running clarify on issues that are refused deterministically (critical path).**
   - **Evidence:** five clarify failures between 23:45:49 and 00:26:58, each 22–31s, each followed by a heal run of 72–278s.
   - **Root cause:** `label_missing` is not transient. Every new issue event re-runs the resolver and gets the same refusal.
   - **Change:** land the fix being planned on #7516 (plan run 38984434649, success, 1,108s). Until then, have heal intake skip model calls when the same workflow, step and `INTEGRATION_REF_RESOLVE`/`CI_TRIAGE_PR_BRANCH_ROUTING reason` was already filed inside `WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS=3600`.
   - **Savings:** about 540s of heal runner/model time per burst, plus faster progress through the issue pipeline.
   - **Risk:** low. The resolver stays fail-closed and only the duplicate heal work goes away.
2. **Skip the Semble bootstrap when no query will run (micro, off the critical path).**
   - **Evidence:** run 38903068381 had `semble_bootstraps=3`, `semble_bootstraps_unused=3`, `semble_bootstrap_ms_total=128407`.
   - **Change:** in lazy mode, bootstrap only on the first query in the log-analysis workflow, or emit `SEMBLE_BOOTSTRAP` with `unused=true` and have the caller stop bootstrapping again.
   - **Savings:** about 128s per log-analysis run (5.8% of its 2,215s).
   - **Risk:** low.
3. **Review/autofix Semble bootstrap.**
   - **Evidence:** 42.1s in 38916979717, 28.4s in 38906580296 and 41.7s in 38911268890, against a single query each.
   - **Change:** start the bootstrap in the background while the PR metadata and checkout steps run, if it is not already parallel. This is an inference: step ordering is not visible in the supplied data.
   - **Savings:** up to about 40s per review run (about 2% of p50).
   - **Risk:** low.
4. **CI critical path.**
   - **Evidence:** 26 runs, p50 1,368s, p95 2,168s, max 2,268s (38906641802). Twelve CI runs started between 13:02 and 13:31, so runner contention is likely.
   - **Gap:** there are no job- or step-level durations, so no specific change can be justified. Next step: add per-job durations to the CI rows in `collect_workflow_logs.py`.
5. **Orchestrator poll queueing.**
   - **Evidence:** 38983792122 waited about 100s for a runner. 38907136370 was cancelled before its first step after 817s.
   - **Assessment:** this is a concurrency or queue effect, not compute. Cancellation via concurrency is the right behaviour, and nothing needs to change beyond tracking it (see Orchestrator Health).

## Cost Optimizations

1. **Fix Codex token parsing before any model or effort tuning.**
   - **Evidence:** 51 calls and 19 tokens across clarify, plan and orchestrate_clarify_respond. A pre-Codex failure (38984124962) still counted 6 calls.
   - **Root cause:** `tokens\s+used[^0-9\n]*\n?\s*([0-9][0-9,]*)` matches echoed shell text at `clarify.yml:1524` and captures `1` from `tail -1`.
   - **Change:** anchor the match to whole log lines and allow an optional Actions timestamp prefix, keeping the single-line form:

   ```python
   _TS = r"(?:\d{4}-\d{2}-\d{2}T\S+Z\s+)?"
   CODEX_TOKENS_RE = re.compile(
       rf"^{_TS}tokens\s+used\s*\n{_TS}([0-9][0-9,]*)\s*$"
       rf"|^{_TS}tokens\s+used[:\s]+([0-9][0-9,]*)\s*$",
       re.IGNORECASE | re.MULTILINE,
   )
   # parse_log: value = m.group(1) or m.group(2)
   ```

   Add a test that uses the line at `clarify.yml:1524` as a negative fixture.
   - **Savings:** none directly, but every Codex cost decision currently depends on this number.
   - **Quality risk:** none.
2. **Review/autofix prompt-cache variance.**
   - **Evidence:**

   | Run | Hit rate | Uncached prompt | Cache write |
   |---|---|---|---|
   | 38916979717 | 0.736 | 744K | 279K |
   | 38911268890 | 0.536 | 830K | 357K |
   | 38906580296 | 0.451 | 1.14M | 365K |

   - **Root cause (inference):** the prompt prefix changes between the 10 calls in a run. A PR diff or dynamic context placed before the static rubric would force fresh cache writes.
   - **Change:** order prompts as static system rules, then the rubric, then Semble context, then the PR diff and findings. Log a prefix hash per call so fragmentation can be measured.
   - **Savings:** moving the 0.45–0.54 runs to about 0.70 saves roughly 0.3–0.45M uncached prompt tokens per run (10–15% of input spend).
   - **Quality risk:** none, since only the ordering changes.
3. **Heal-intake de-duplication on Opus (same change as Speed #1).**
   - **Evidence:** 4 intake runs in 40 minutes, all `engine=claude model=claude-opus-5-5 effort=high`, all following the same clarify failure signature.
   - **Savings:** 3 of the 4 model invocations.
   - **Quality risk:** low as long as the de-duplication key includes the structured reason.
4. **Semble is not adding noisy context.**
   - **Evidence:** 3 queries returned 49,418 B (about 16.5 KB each, roughly 4K tokens, which is under 0.2% of a 3M-token review run). There were 19 sources, and only 1,041 B (2.1%) were static duplicates.
   - **Assessment:** cost is negligible, so no change is needed. Whether Semble reduced downstream expansion cannot be measured without a control run.
5. **Serena:** there are no `SERENA_QUERY`, `SERENA_FALLBACK` or `SERENA_PROBE` lines anywhere, so it cannot be evaluated (see Reliability #4).

## Reliability Improvements

1. **Clarify/plan `Resolve integration ref` refusals (89% of failures).**
   - **Category:** deterministic input-contract mismatch. The issue body carries a `check-failure-triage:` marker that is not the `pr=` form, but the issue has no `ai:check-triage` label. `resolve_integration_ref.sh:205-225` then refuses with `label_missing`.
   - **Fix:** land #7516's planned change. Keep the fail-closed default for genuine triage issues, the same way #7511/#7512 added a `skip` path for managed children.
   - **Interim diagnostic:** have `collect_workflow_logs.py`'s `extract_failure_point` also capture the first `::error::INTEGRATION_REF_RESOLVE …` or `::error::CI_TRIAGE_PR_BRANCH_ROUTING … reason=…` line as `failure_point.reason`. Fail open: leave it null if the log fetch fails.
   - **Impact:** failures drop from 9 to about 1 per window, and future refusals can be classified without a deep dive.
   - **Rollback:** the fix is local to the resolver's Python classifier, so reverting is a one-hunk change.
2. **Review/autofix `Checkout PR head branch` failure (run 38911237933, 2,373s).**
   - **Evidence:** the failure is in job `review / codex-agent` after about 39 minutes, with no OpenRouter usage recorded. The step has four `exit 1` paths (`review_autofix.yml:2809-2866`): workspace unavailable, OpenCode config present, `Unable to determine PR head branch` (plain echo, not `::error::`), and git fetch failing after 4 attempts.
   - **Root cause:** unknown, because the deep-dive log was not supplied. One possibility (inference, low confidence) is that the head branch was deleted or the PR closed during the long run.
   - **Fix:** turn each `exit 1` into one structured line, for example `::error::REVIEW_CHECKOUT outcome=failed reason=head_ref_missing|workspace_missing|opencode_config|fetch_exhausted pr=<n>`. Register the new prefix through an `agents.d/<n>-review-checkout-log.md` fragment under "Stable log prefixes (contractual)" (§25). If `fetch_exhausted` turns out to be a deleted branch, re-check `gh pr view --json state` and exit 0 with `CAN_PUSH=false`, which matches the existing non-writable path.
   - **Impact:** this is 1 of 9 failures, but each one wastes about 40 minutes.
3. **Heal-loop amplification.**
   - **Evidence:** issue #7516 carries `ai:workflow-heal`, and its own clarify, clarify-respond and plan chain ran 506s, 722s and 1,108s. Each clarify failure also starts a new intake.
   - **Guardrails:** `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH=3`, `MAX_OPEN_ISSUES=10` and `MAX_ISSUES_PER_DAY=20` cap the loop.
   - **Fix:** de-duplicate by failure reason (see Speed #1). Track `intake runs / distinct failure reasons` per hour.
   - **Fail-open:** if the de-duplication lookup fails, run the intake as today.
4. **Serena silence.**
   - **Evidence:** zero probe, query and fallback events across 118 runs with telemetry.
   - **Assessment:** either Serena is not enabled, or `SERENA_PROBE` is not emitted when it is skipped. The second case would mean an availability failure is being hidden.
   - **Fix:** emit `SERENA_PROBE result=skipped reason=disabled` once per job wherever the MCP config is read, so absence and failure can be told apart.
5. **Semble fallbacks are healthy.** All 12 were contract-test fallbacks in CI (4 each in 38906641802, 38905028160 and 38905021394), with 0 runtime fallbacks and 0 failed bootstraps. In poller runs 38983792122 and 38986228659, `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false` with `SEMBLE_ENABLED=true`. That is expected for the poller, which has no index, but it should log one `SEMBLE_FALLBACK reason=unavailable` line so it is not mistaken for a broken rollout.
6. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** 0 of each across all families. There is no rubric or prompt-size pressure in what was observed, though truncation of 55K–940K characters in several summaries could be hiding lines.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines appear in any supplied run row or `log_summary`, and the deep-dive folder was not available. The clarify run 38983542780 and plan run 38984434649 summaries explicitly say none were visible, but about 292K and 940K characters were truncated respectively.
- Retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` and `enabled:false` counts, and push retries therefore **cannot be computed**.
- The single `memory_maintenance` run, 38905606009, succeeded in 1,245s, but no telemetry was parsed from it.
- **Recommendation:**
  - Add an `AI_MEMORY_TELEMETRY` counter (retrieve, `records_selected>0`, `fail_open`) to `cost_audit.parse_log`, next to the existing Semble and Serena counters, so it survives truncation as a structured field.
  - Confirm that clarify and plan emit `op=retrieve` before the Codex step, so the line lands early in the log rather than in the truncated middle.

## GH API Call Audit

- **Budget readings for heal intake, which shares a `reset=1792197446` window:** `remaining` went 4724 (38983250046) → 4709 (38983567905) → 4578 (38984163249) → 4326 (38986445246). That is about 398 calls in about 40 minutes, roughly 600 per hour or 12% of a 5,000-per-hour budget. There is headroom, and no 429 or retry events are visible.
- **Gap: `used_in_job=unknown` for `workflow-failure-heal-intake`.** It records only `phase=end`. Add the same `phase=start` capture that `clarify.yml:75-87` uses. That is one `gh api rate_limit` call per job, and `rate_limit` calls do not count against the core quota.
- **Sweep run 38984374301:** `used_in_job=4` with 0 candidates, which is efficient.
- **Fan-out:** each issue event triggers 4 workflows (clarify, plan, implement, orchestrate_clarify_respond). Batches with identical `created_at` appear in recent runs, e.g. 38986535484/572/569/573 at 00:28:34. About 145 events produced 559 skipped runs. Skipped runs make no PAT calls (inference from 0–1s durations), so the API cost is negligible. The cost is that they take 68% of the collector's 1,000-run window (see Pipeline Flow).
- **Cancel-on-close:** 38983372526 and 38984104554 made no cancel calls, and the merge-train scan reported `examined=0`, which is correct.
- **Resolver:** each clarify or plan resolver run performs one shallow clone plus one fetch of the workflow repo and the resolver's issue lookup. At about 8 failures per 12 hours this is negligible, and no batching change is needed. No new per-item loops were observed against the §14 hygiene rules, though poller internals (`_fetch_candidate_issue_details_graphql`) are not visible because of truncation (822K–837K characters dropped).

## Prompt Cache & Memory System

- **Overall `cache_hit_rate`:** 0.59538. Cache read was 5,464,063 tokens, cache write 1,000,452 and uncached prompt 2,712,919. Only review/autofix reports OpenRouter usage: 30 of 30 calls had `usage_available`, and 0 were unavailable.
- **Per-run variance (0.451 to 0.736)** across three runs of the same workflow points to prefix fragmentation rather than cold starts. Cache writes of 279K–365K per run mean that roughly 10% of each run's input is new prefix material.
- **Likely fragmentation causes (inference):** dynamic PR or Semble content placed before stable instructions, and per-call variation in finding lists or reviewer IDs. Two of the slow runs, 38906580296 and 38911268890, are both titled `[pr:7508]` and have the lowest hit rates. Repeated passes on one PR should reuse a prefix, which suggests the prefix includes per-iteration state.
- **Recommendations:**
  - Log a per-call `prefix_sha256` (first N KB) beside the OpenRouter usage line, so `cost_audit` can count distinct prefixes per run.
  - Move per-iteration state to the tail of the prompt.
  - Expected result: hit rate of 0.70 or better and about 10–15% fewer uncached prompt tokens.
- **Fail-open:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` in clarify. There are no cache-disable events.
- **`CONTEXT_BUDGET_WARN`:** 0, so prompt growth is not yet eroding cache value.
- **Claude-engine runs (heal intake, unblock judge, clarify-respond):** no token lines are visible, so their cache behaviour cannot be assessed. The claude_* fields are absent from the supplied rows.

## Orchestrator Health

- **Poller:** 71 runs, 61 succeeded, 10 cancelled, 0 failed. Duration p50 was 572s and p95 1,091s, and the `poll` step took about 586s of 695s in 38983792122. The 10 cancellations, such as 38907136370 cancelled before its first step after 817s, are concurrency supersession and benign.
  - **Indicator to track:** cancelled runs with `queue_wait > 600s`, which shows the poll interval is shorter than queue plus compute time.
- **Clarification loop:** issue #7516 went clarify (38983542780, 506s) → auto-answer (`/answer [auto-answered-by-orchestrator]`, Q1–Q3: A) → clarify-respond (38983917083, 722s) → plan (38984434649, 1,108s). That is about 30 minutes from issue to plan. `ORCHESTRATOR_MAX_CLARIFY_CYCLES=3` was not hit.
- **Unblock judge:** 12 of 12 succeeded. Item 7258 was `outcome=skip reason=fixup_escalated`, which is healthy terminal handling.
- **Validate / integration readiness:** validate 3 of 3 (tracking:7490 and tracking:0), integration_pr_readiness 18 of 18, validation_refresh 1 of 1. No conflict-heal retries were observed.
- **Pain point:** a heal issue (#7516) about a clarify refusal was itself processed through clarify while sibling clarify runs kept failing on the same refusal.
  - **Indicator to track:** `failures grouped by (step, INTEGRATION_REF_RESOLVE reason)` per day.
  - **Mitigation:** the reason-capture change in Reliability #1, plus the de-duplication in Speed #1.

## Pipeline Flow Bottlenecks

| Stage | Executed runs | p50 / p95 (s) | Failures | Dominant overhead |
|---|---|---|---|---|
| clarify | 14 (157 total) | 1 / 79 (all runs) | 6 | Retry: deterministic resolver refusal |
| plan | 9 (145 total) | 1 / 29 (all runs); executed runs up to 1,108 | 2 | Compute (Codex plan step about 18 min in 38984434649) |
| implement | 8 (145 total) | 1 / 21 (all); executed runs 864–1,747 | 0 | Compute |
| review/autofix | 42 | 33 / 2,350 | 1 | Compute plus about 37s Semble bootstrap |
| CI | 26 | 1,368 / 2,168 | 0 | Compute and burst queueing (13:02–13:31) |
| validate | 3 | 945 / 1,240 | 0 | Compute |
| orchestrate_poll | 71 | 572 / 1,091 | 0 | Queue (about 100s) plus long poll step |

1. **Retry overhead:** clarify refusals plus heal intake are the largest avoidable loop (Reliability #1, Speed #1).
2. **Compute:** CI at about 23 minutes p50 and review/autofix at about 38 minutes for full runs dominate end-to-end latency. Step-level timing is needed to target them.
3. **Queueing:** poller and heal runs show runner waits of about 2–100s. The poller's 10 cancellations are tolerable.
4. **Measurement bottleneck:** 684 of 1,000 rows are skipped fan-out runs, so the cap leaves about 12 hours of real signal. Change: in `collect_workflow_logs.py`, drop `conclusion=skipped` rows before applying the run cap, while still counting them in an `other_count`-style field. That keeps backward-compatible totals and gives about three times more real runs per window.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - CI, p50 1,368s
  - review/autofix full runs (2,260–2,985s: 38906589398, 38911237933, 38914407090)
  - poller compute, about 9–10 minutes per cycle
- **Top failure modes:**
  - `Resolve integration ref` refusals: 8 runs, likely `label_missing`
  - review/autofix `Checkout PR head branch`: 1 run, 38911237933, reason unlogged
- **Highest-cost drivers:**
  - review/autofix OpenRouter usage, about 3.08M tokens per run
  - Opus heal intake started by duplicate failures
  - Codex spend, which is unknown because the parser is broken
- **Top 3 prioritized actions:**
  1. Land #7516's resolver classification fix, and capture the `INTEGRATION_REF_RESOLVE` / `CI_TRIAGE_PR_BRANCH_ROUTING` reason into `failure_point.reason` in the collector.
  2. Anchor `CODEX_TOKENS_RE` in `scripts/cost_audit.py` to whole log lines, with a negative fixture taken from `clarify.yml:1524`.
  3. Add structured `REVIEW_CHECKOUT` reason lines to every `exit 1` path in the review/autofix `Checkout PR head branch` step, and de-duplicate heal intake by failure reason.

## Metrics Appendix

**Run outcomes (window: 2026-10-16 12:47Z to 2026-10-17 00:28Z; `insufficient_data=false`; `success_sample_rate=0.07`)**

| Metric | Value |
|---|---|
| Total runs | 1,000 (likely the collector cap) |
| Success / failure / cancelled / other | 295 / 9 / 12 / 684 |
| Failure rate (all / executed) | 0.9% / 3.0% (9/304) |
| p50 / p95 duration | 2s / 843s |
| Avg duration | 137.6s |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 2,371,310 (114 samples) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| Merged PR count | not available in the telemetry |

**Failures by step**

| Step | Family | Runs | Run IDs |
|---|---|---|---|
| Resolve integration ref | clarify | 6 | 38959970027, 38983115876, 38983214967, 38983537115, 38984124962, 38986413434 |
| Resolve integration ref | plan | 2 | 38969365068, 38976601176 |
| Checkout PR head branch | review_autofix | 1 | 38911237933 |

**Tokens and cache**

| Metric | Value |
|---|---|
| OpenRouter calls (usage available / unavailable) | 30 (30 / 0) |
| OpenRouter prompt / completion / total | 2,712,919 / 64,508 / 9,241,575 |
| OpenRouter cache read / write | 5,464,063 / 1,000,452 |
| `cache_hit_rate` | 0.59538 overall; 0.736 / 0.451 / 0.536 (38916979717 / 38906580296 / 38911268890) |
| Codex calls / tokens | 51 / 19 (**invalid**: parser false positives) |

**Semble**

| Workflow | query_calls | logged_bytes | sources | static_dup_bytes | bootstraps (unused) | bootstrap_ms | fallbacks (contract-test / runtime) |
|---|---|---|---|---|---|---|---|
| review_autofix | 3 | 49,418 | 19 | 1,041 (2.1%) | 3 (0) | 112,095 | 0 / 0 |
| workflow_log_analysis | 0 | 0 | 0 | 0 | 3 (3) | 128,407 | 0 / 0 |
| ci | 0 | 0 | 0 | 0 | 0 | 0 | 12 / 0 |
| **Total** | 3 | 49,418 | 19 | 1,041 | 6 (3) | 240,502 | 12 / 0 |

**Serena / MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | query_calls | response_bytes | fallbacks |
|---|---|---|---|---|---|---|
| (all) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API (PAT budget readings)**

| Run | Workflow | remaining | used_in_job |
|---|---|---|---|
| 38983250046 | heal intake | 4,724 | unknown |
| 38983567905 | heal intake | 4,709 | unknown |
| 38984163249 | heal intake | 4,578 | unknown |
| 38984374301 | review_autofix_sweep | 4,332 (different reset window) | 4 |
| 38986445246 | heal intake | 4,326 | unknown |

**Data gaps**
- The `/home/runner/work/_temp/workflow-log-output` deep-dive directory (errors/slow/recent) was not mounted, so no untruncated step logs could be read.
- The 1,000-run cap was reached, with 68.4% of rows skipped.
- Codex token counts are invalid.
- There are no Claude token fields, no `AI_MEMORY_TELEMETRY`, and no Serena events.
- Log summaries were truncated by 55K–940K characters.
- No merged-PR or review-iteration counts were available.
## Deep Audit — Workflows & Scripts (2026-10-17)

> **Scope and method.** I read these files in depth: `clarify.yml`, `workflow-failure-heal-intake.yml`, `sync_ai_labels.yml`, `pr6576-shard-followup.yml`, the resolver-handling parts of `plan.yml` and `implement.yml`, the wait-loop steps of `test-and-mark-stable.yml` and the `Checkout PR head branch` step of `review_autofix.yml`. On the script side I read `resolve_integration_ref.sh`, `gh_helpers.sh` (the retry and budget helpers), `unblock_judge.sh`, `comprehensive_test_and_release_gh_api.sh`, `workflow_failure_heal_intake.sh` (fingerprint, dedup and diagnosis order), the `budget`, `error_signature` and `fingerprint` functions in `workflow_failure_heal.py`, the `reissue` op in `unblock_actions.py`, and the wave loop and GraphQL batch helpers in `orchestrate_poll_process.sh`. For the other files I only counted patterns. Workflow sizes are character counts (1,000-character chunk counts plus non-ASCII counts), not exact byte counts. Where a finding repeats a point already in this report I give a cross-reference instead of restating it.

### Section 1: Bug & Correctness Sweep

**BUG-001 — The resolver refuses any unlabelled issue whose body contains the triage marker text anywhere (`label_missing`)**
- **File:** `scripts/resolve_integration_ref.sh:197-225`. Related: `scripts/unblock_actions.py:706-744`.
- **Severity:** High
- **Category:** `bug`
- **Description:** `CI_TRIAGE_PARSE_PY` sets `has_marker = re.search(r"<!--\s*check-failure-triage:", body) is not None` (line 198). That search runs over the whole issue body, including code fences, quoted logs and prose.
  - The only escapes for an unlabelled issue are a `pr=`-only marker (line 205) or an `ai:orchestrator-managed` child with a `Tracking issue:` line (lines 213-216). Every other unlabelled issue gets `refuse("label_missing")` at line 225.
  - That refusal becomes exit 3, and the callers turn exit 3 into a hard `exit 1` (`clarify.yml:205-207`, `plan.yml:281-283`, `implement.yml:630-632`). Retrying never helps.
  - The line-by-line header check that defines a real triage issue (lines 226-235) only runs after the label check. So an issue that merely mentions the marker is refused before the header is ever checked.
  - In this repository, which builds the triage feature, any human-written issue, heal issue or plan comment copied into an issue body that quotes `<!-- check-failure-triage:fp=…` will permanently block clarify and plan.
  - A second path: the unblock judge's `reissue` op writes model-authored text into the body (`Specification: {verdict['instructions']}` and `Why: {verdict['reason']}`, `unblock_actions.py:708`). It prepends only a `pr=` marker (line 741) and adds an `Integration branch:` line only when the owner is `open` (lines 742-744). If the model quotes a non-`pr=` header and the owner state is `verified`, the re-issue is refused the same way.
  - This is a concrete root-cause candidate for the 8 `Resolve integration ref` failures in the Executive Summary and Reliability #1 of this report. `[NEEDS VERIFICATION]`: the bodies of the affected issues were not inspected.
- **Recommended fix:** Only treat the marker as present when it is the opening line of the body, which is where `check_failure_triage.sh` writes it (header tuple at lines 227-232) and where `unblock_actions.py:741` writes the `pr=` lineage marker:
  ```python
  has_marker = re.match(r"\s*<!--\s*check-failure-triage:", body) is not None
  ```
  Apply the same anchoring to the line-205 lookahead. Keep the fail-closed `label_missing` refusal for bodies whose first line is a triage header but which have no label; that case is a genuine removed-label triage issue. Add two fixtures under `tests/fixtures/integration_ref_resolver_ci_triage/`:
  - marker quoted mid-body, no label → `expected_stdout: ""`, rc 0;
  - header at the top, no label → `expected_error_reason: label_missing`.

**BUG-002 — Clarify and plan fall back to the default branch when the resolver hits an API failure; implement fails closed**
- **File:** `.github/workflows/clarify.yml:205-214`, `.github/workflows/plan.yml:281-291`. Compare `.github/workflows/implement.yml:633-650`.
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The resolver exits 2 on API failure (`get_issue_body`, `resolve_integration_ref.sh:77-82`, after `gh_api_retry` has used up its retries). This includes the tracking-issue read for a managed child (line 437).
  - `implement.yml:645-648` treats rc 2 as `::error::INTEGRATION_REF_RESOLVE outcome=api_unavailable` and exits 1. It also fails closed on rc 75 whatever the routing setting (lines 639-642).
  - `clarify.yml:211-213` and `plan.yml:287-290` send rc 2 to `::warning::Canonical integration resolver failed; falling back to default branch.` They also fall back on rc 75 whenever CI-triage routing is off (`clarify.yml:208`, `plan.yml:284`).
  - Result: a single GitHub API failure lets an orchestrator child be clarified or planned against the default branch instead of its integration branch, and only a warning is logged.
  - `[NEEDS VERIFICATION]`: whether `plan.yml`'s downstream `resolver_status=failed` guard stops the plan before Codex runs.
- **Recommended fix:** Copy `implement.yml:639-648` into both workflows. That means `exit 1` with `INTEGRATION_REF_RESOLVE outcome=api_unavailable rc=2`, plus `outcome=rate_limited` on rc 75 regardless of routing. Keep the default-branch fallback only for rc 1. Better still, remove the drift entirely by doing DUP-001.

**BUG-003 — The `RATE_LIMIT_BACKOFF` guard in the release smoke-test wait loops can never fire**
- **File:** `.github/workflows/test-and-mark-stable.yml:861`, `:1056-1058`, `:1387`, `:2140`, `:3706`. Root cause: `scripts/comprehensive_test_and_release_gh_api.sh:11-34`.
- **Severity:** Medium
- **Category:** `bug`
- **Description:** `gh_api_safe` updates `RATE_LIMIT_BACKOFF` (lines 17, 28-31). Every loop calls it as `LABELS=$(gh_api_safe_print …)` (for example line 1052), which runs in a command-substitution subshell, so the update is lost.
  - In the parent shell the variable stays at the `0` set at lines 789, 943, 1257, 1877 and 3556. The guard `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]; then continue; fi` is therefore dead code.
  - Its comment says it should "skip this cycle (don't corrupt state)". Instead, a rate-limited empty `LABELS` goes into `CUR_STATE` and resets the inactivity timer (lines 1081-1091), which hides stalls.
  - If the guard ever did fire, its `continue` skips the `sleep "$POLL_INTERVAL"` at line 1202, which would make it a tight loop.
- **Recommended fix:** In the loops, call `gh_api_safe` in the parent shell and read `GH_API_SAFE_OUTPUT`, so the backoff state survives:
  ```bash
  LABELS=""
  if gh_api_safe "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name] | join(",")'
  then
  	LABELS="${GH_API_SAFE_OUTPUT}"
  fi
  ```
  Add `sleep "$POLL_INTERVAL"` before each guarded `continue`.

**BUG-004 — `gh_api_safe` writes its `::error::` line to stdout, where callers capture it as data**
- **File:** `scripts/comprehensive_test_and_release_gh_api.sh:39-43`. Consumers: `.github/workflows/test-and-mark-stable.yml:1052-1063`.
- **Severity:** Low
- **Category:** `bug`
- **Description:** `echo "::error::gh api call failed: $*"` (line 41) has no `>&2`. The step's own comment admits this (`test-and-mark-stable.yml:974-976`: "gh_api_safe_print echoes its "::error::gh api call failed" line to stdout").
  - Inside `$(…)`, the annotation is never shown. Its text ends up in `LABELS` and `COMMENTS_JSON`, and then in `CUR_STATE`.
  - Only `find_latest_scoped_run_field` shape-checks its input (lines 125-127).
- **Recommended fix:** Change line 41 to `echo "::error::gh api call failed: $*" >&2`. The existing shape checks still hold. The `LABELS` and `COMMENTS_JSON` callers already fall back with `|| echo ""` / `|| echo "[]"`.

**BUG-005 — The label recheck uses raw `gh api … 2>/dev/null || echo ""`, which can capture an error body**
- **File:** `.github/workflows/test-and-mark-stable.yml:1117-1118`, `:1188`
- **Severity:** Low
- **Category:** `bug`
- **Description:** `gh_helpers.sh:1225-1236` documents that a failed `gh api` writes its error JSON to stdout without applying `--jq`. On a transient failure, `LABELS_RECHECK` therefore holds `{"message":…}`. The step then reports `Plan workflow completed but issue lacks expected labels (current: {"message":…})` and fails with `status=plan_failed`. The same step uses `gh_api_safe_print` at line 1052.
- **Recommended fix:** Use `gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name] | join(",")' || echo ""`. If the recheck stays empty, treat that as "retry next tick" rather than a failure.

**SEC-001 — Event context is interpolated straight into a `run:` body**
- **File:** `.github/workflows/clarify.yml:66-73`
- **Severity:** Low
- **Category:** `security`
- **Description:** `Log trigger context` interpolates `${{ github.event.comment.user.login }}`, `${{ github.event_name }}` and `${{ github.event.action }}` into the script. Their character sets are restricted (GitHub logins are `[A-Za-z0-9-]`), so nothing can be injected today. But the step goes against the repo's own trust-boundary convention: `workflow-failure-heal-intake.yml:169-176` says "bound as env … never interpolated into the script". It is also an easy template to copy-paste with an `issue.title` field.
- **Recommended fix:** Move the three values into `env:` (`TRIGGER_EVENT`, `TRIGGER_ACTION`, `COMMENT_AUTHOR`) and reference them as `"${COMMENT_AUTHOR}"`.

**SEC-002 — If the pinned or `stable` support checkout fails, the workflow silently runs `main` scripts with production secrets**
- **File:** `.github/workflows/clarify.yml:274-301`. The same fallback appears 12 times across 10 workflows, including `plan.yml`, `implement.yml`, `orchestrate.yml` (×2), `orchestrate_poll.yml` (×2), `check_failure_triage.yml`, `cancel_on_pr_close.yml`, `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml` and `sync_ai_labels.yml:58-66`.
- **Severity:** Medium
- **Category:** `security`
- **Description:** The primary step `Checkout workflow support source` is `continue-on-error: true`. The fallback then checks out `ref: main`.
  - For a consumer that deliberately pinned this workflow to a 40-hex SHA (Q64, `clarify.yml:261-269`), a transient checkout failure swaps the pinned support scripts for unreleased `main` code. That code then runs with `GH_PAT` and `OPENROUTER_API_KEY`.
  - No structured line records the substitution: the only signal is the fallback step's outcome.
  - `[NEEDS VERIFICATION]`: whether the fallback-to-main is an intentional availability trade-off for consumers on `stable`.
- **Recommended fix:** Fail closed when `SCRIPT_REF` is a 40-hex pin: add `if: steps.checkout_support_source.outcome == 'failure' && !startsWith(env.SCRIPT_REF, …)`, or handle it in a pre-check step. When the fallback does run, emit `SUPPORT_REF ref=main outcome=fallback requested=${SCRIPT_REF}`. That reuses the existing `SUPPORT_REF` prefix (`clarify.yml:270`).

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001 — The wave reconcile loop makes per-issue timeline and per-candidate PR REST calls**
- **File:** `scripts/orchestrate_poll_process.sh:24724-24765`. Helpers: `:4420-4453` (timeline), `:2215-2219` (`_fetch_pr_json`).
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** For every issue in the wave, `_issue_cross_ref_pr_numbers_unique` runs `gh_retry gh api --paginate …/issues/${issue_num}/timeline` (line 4435, or the `gh_issue_timeline_with_cross_refs` variant). Then each cross-referenced PR gets its own `_fetch_pr_json` REST read (line 24750).
  - The §14 comment at 24746-24747 justifies this because "the existing timeline call omits PR body/head".
  - This is a per-iteration call inside a `for` over issues, which is review-blocking under §14.
  - **Current:** N × (≥1 timeline page + K PR reads) per wave.
  - **Projected:** ⌈N/25⌉ GraphQL calls, with REST kept only on cache miss.
- **Recommended fix:** Extend `_fetch_linked_pr_status_graphql` (`:18173-18221`, cache `STALL_MANAGED_LINKED_PR_CACHE`), or add a sibling mode that keeps all `CrossReferencedEvent` sources rather than only `willCloseTarget == true` (line 18248). Add `headRefName baseRefName body mergedAt headRepository { nameWithOwner }` to the `... on PullRequest` selection, then build the `_pr_json_is_issue_implementation_pr` input from that cache. Fail open: an issue missing from the batch drops back to today's REST path. Document input and output shape, call count and fail-open behaviour per §14. `[NEEDS VERIFICATION]`: the exact fields `_pr_json_is_issue_implementation_pr` reads.

**BATCH-002 — Sibling fix-PR lookups in `unblock_heal_already_fixed` are one REST call per candidate**
- **File:** `scripts/unblock_judge.sh:404-431`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** A `while read` loop issues `gh api -X GET repos/…/pulls -f state=closed -f head=owner:ai/issue-<N>` for each candidate (line 407). The header comment says there are at most 3 candidates (line 86). **Current:** up to 3 calls. **Projected:** 1.
- **Recommended fix:** Build one aliased query, `query { repository(owner:…, name:…) { c<N>: pullRequests(headRefName:"ai/issue-<N>", states:MERGED, last:1) { nodes { number mergedAt mergeCommit { oid } baseRefName headRefName } } } }`, using the same alias style as `_fetch_candidate_issue_details_graphql` (`orchestrate_poll_process.sh:18015-18045`). Candidates are already validated numeric at line 405. If the query fails, return 1 (judge as usual).

**API-001 — Every heal intake and unblock run re-downloads the full `state=all` heal-issue history**
- **File:** `scripts/workflow_failure_heal_intake.sh:1012-1014`, `:1024-1026`; `scripts/unblock_judge.sh:371-373`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** Each intake run paginates every `ai:workflow-heal` issue ever filed, in both the self repo and the source repo, with full bodies. The unblock judge repeats the same list. Pages grow without bound (⌈total heal issues / 100⌉ per repo per run) while the per-day cap allows up to 20 new issues a day. That makes this a large share of the roughly 398 calls in 40 minutes noted in the GH API Call Audit (inference).
  - `budget()` (`workflow_failure_heal.py:4115-4149`) only needs:
    - open issues (duplicate check and `max_open`);
    - issues created today (`created_today`);
    - closed issues carrying the same `fp` or `source`, or the linked heal issue (lineage).
  - **Current:** P_self + P_source pages per intake, growing over time.
  - **Projected:** about 3 bounded calls: `state=open`, `created>=<today>` via search, and a search for `"fp=<FP>" OR "source=<key>"` in the body for closed lineage.
- **Recommended fix:** Add a bounded fetch helper that produces the same JSON array shape fed to `--issues-json`. Fall back to today's full list when the search API returns `incomplete_results` or fails. `[NEEDS VERIFICATION]`: whether `resolve_reissue_roots` (used at line 4169) needs closed issues outside the lineage set.

**API-002 — The smoke-test wait loops make three REST calls on every poll tick**
- **File:** `.github/workflows/test-and-mark-stable.yml:1052-1076`, with the same shape in the other wait loops.
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** Each `POLL_INTERVAL` reads the issue (labels), its comments (`per_page=100`, used only for count and newest `updated_at`) and a runs page. **Current:** 3 calls per tick. **Projected:** 2.
- **Recommended fix:** Replace the first two with one GraphQL call: `issue(number:N){ labels(first:50){nodes{name}} comments(last:1){totalCount nodes{updatedAt}} }`. Keep the runs REST call, since GraphQL does not expose run lists.

**Retry hygiene:** I found no tight retry loops in `gh_api_retry` or `gh_retry`. They use exponential backoff with a cap (`gh_helpers.sh:1025-1026`, `:1116`) and do not retry permanent errors (`:988-993`, `:1099-1106`). The non-transient `git fetch` retry in `review_autofix.yml:2856-2871` (a deleted head branch is retried 4 times) is already covered by Reliability #2 in this report, so I have not repeated it.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The ~125-line "Resolve integration ref" staging block exists in 5 copies, and the copies have drifted**
- **File:** `.github/workflows/clarify.yml:89-214`, `.github/workflows/plan.yml:146-291`, `.github/workflows/implement.yml:530-651`, `.github/workflows/validate.yml:157-265`, `.github/workflows/orchestrate_clarify_respond.yml:247` (`resolver_stage_fail_closed` definition).
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** All five copies contain the same clone, pin and fail-closed logic. Their exit-code mapping has already diverged (BUG-002), and any fix for #7516 or BUG-001 has to be made five times. The step has to run before the repository checkout, so a `scripts/` file cannot be sourced.
- **Recommended fix:** Create a composite action `.github/actions/resolve-integration-ref/action.yml`. Reference it as `shubhodeep1/coding-workflows/.github/actions/resolve-integration-ref@stable`, the same way `install-codex` is pulled in before checkout (`clarify.yml:248`).
  - Inputs: `issue_number`, `stage` (`clarify|clarify_respond|plan|implement|validate`), `allow_pr_not_open` (true only for clarify and plan), `routing_enabled`.
  - Outputs: `ref`, `resolver_status`, `pr_not_open`.
  - Use one rc table: 0 = ref, 1 = fallback, 2/3/75 = fail closed.

**DUP-002 — Three GraphQL batch helpers share the same 25-alias skeleton**
- **File:** `scripts/orchestrate_poll_process.sh:4336-4366` (`_fetch_issue_labels_batch_graphql`), `:18015-18045` (`_fetch_candidate_issue_details_graphql`), `:18173-18239` (`_fetch_linked_pr_status_graphql`)
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** All three repeat the same count check, `batch_size=25`, `while start < count` loop, `jq -r ".[$i]"` per index, `i${i}: issue(number: N)` fragment, `gh_retry gh api graphql`, fail-open `continue` and `.[0] * .[1]` merge. Only the field selection and the jq transform differ.
- **Recommended fix:** Add one shared helper and turn the three into thin wrappers. BATCH-001 then becomes a fourth wrapper rather than a fourth copy.
  ```bash
  _graphql_issue_alias_batch()
  {
  	local numbers_json="$1" selection="$2" transform="$3"
  	...
  }
  ```

**DUP-003 — "Resolve workflow support ref" exists 17 times in 11 workflows, and only 8 copies have the commit pin**
- **File:** pinned copies: `clarify.yml:252-272` (×2), `plan.yml` (×2), `implement.yml` (×3), `orchestrate_clarify_respond.yml` (×1). Unpinned copies: `check_failure_triage.yml:306-314`, `orchestrate_poll.yml:113-121` and `:469-478`, `sync_ai_labels.yml:37-45`, `cancel_on_pr_close.yml`, `orchestrate.yml` (×2), `workflow_failure_heal.yml`. (`review_autofix.yml:2027-2040` uses its own stronger identity check.)
- **Severity:** Low
- **Category:** `duplication`
- **Description:** These are near-identical bodies. The Q64 pin (`SUPPORT_REF_WORKFLOW_JOB_JSON: ${{ toJSON(job) }}`) appears in only 8 of the 17 copies; see CONSIST-002.
- **Recommended fix:** Create a composite action `.github/actions/resolve-support-ref` with input `workflow_file` (e.g. `clarify.yml`) that writes `SCRIPT_REF` to `GITHUB_ENV`. Replace all 16 non-review copies with it.

**DUP-004 — The inline "Resolve AI engine" block appears 11 times in 10 workflows**
- **File:** `.github/workflows/workflow-failure-heal-intake.yml:243-261` and `:421-439` (byte-identical). Also in `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `check_failure_triage.yml`, `issue_pr_status.yml`, `security-audit.yml` and `validation-refresh.yml`.
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Each copy sources `scripts/ai_engine.sh`, calls `ai_engine_for_role <ROLE> || echo codex`, normalises to `claude|codex` and writes `engine=` to `GITHUB_OUTPUT`.
- **Recommended fix:** Add `ai_engine_emit_output()` (role as `$1`) to `scripts/ai_engine.sh`, containing the normalise-and-write logic. Each step body then shrinks to a symlink check plus `source scripts/ai_engine.sh && ai_engine_emit_output WORKFLOW_HEAL`.

**DUP-005 — The inline GH_PAT start-budget block duplicates `gh_pat_budget start`**
- **File:** `.github/workflows/clarify.yml:75-87`, `.github/workflows/workflow-failure-heal-intake.yml:137-148` and `:381-392`. 13 occurrences across 6 workflows, including `review_autofix.yml` (×6).
- **Severity:** Low
- **Category:** `duplication`
- **Description:** These steps re-implement `gh_pat_budget start` (`scripts/gh_helpers.sh:31-77`) inline, because they run before checkout. They also differ from the helper: they read the body only, while the helper prefers the `x-ratelimit-*` headers (lines 52-60).
- **Recommended fix:** Create a composite action `.github/actions/gh-pat-budget` (input `phase`, `workflow`, `job`, `snapshot_file`) that stages `gh_helpers.sh` from `@stable` and calls `gh_pat_budget`. Use it for both the start and end steps.

**DUP-006 — The heal-issue list fetch and its jq projection are copied three times**
- **File:** `scripts/workflow_failure_heal_intake.sh:1012-1014`, `:1024-1026`; `scripts/unblock_judge.sh:371-373`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Each copy has the same `gh api --paginate … -F state=all -F labels=… -F per_page=100 --jq '.[] | {number, state, state_reason, … }' | jq -s 'map(. + {repository: $repository})'` pipeline. The projections differ slightly (`title`, `created_at` and `closed_at` versus `labels`).
- **Recommended fix:** Add `heal_issue_list_fetch()` (`<repo> <out_file>`) to `scripts/gh_helpers.sh`, using `gh_api_retry` and the union of the fields. Call it from both scripts. This is also the single place to land API-001.

### Section 4: Expression Size Limit Risk Assessment

A CI guard already exists: `tests/test_workflow_file_size_limit.py:66-98` fails any step `run:` body that contains `${{` and is at least `RUN_EXPRESSION_GUARD_CHARS = 18_000` characters. The findings below sit under that guard, or are gaps in what it checks.

**EXPR-001 — `Phase 2: Wait for plan to complete` is an interpolated body of about 16K characters**
- **File:** `.github/workflows/test-and-mark-stable.yml:936-1203`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The body is 267 lines. Its only interpolations are three copies of `${{ steps.create-issue.outputs.created_after }}` (lines 954, 966, 984). Estimated size is about 16,000–17,000 characters, which puts it in the 71–85% band of the 21,000 limit, with roughly 4,000–5,000 characters of headroom. Every PR-review round in this step's history has added comments. `[NEEDS VERIFICATION]`: exact length, which `len(step["run"])` from the existing test will give.
- **Recommended fix:** Add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step's `env:` and use `"${CREATED_AFTER}"`. The body then has no `${{ }}` and drops out of expression evaluation entirely. This is the pattern already used at lines 1825 and 2606 ("Promoted to env so the run: script body contains no ${{ }}").

**EXPR-002 — `Phase 3b: Wait for PR creation` is an interpolated body of about 16K characters**
- **File:** `.github/workflows/test-and-mark-stable.yml:1243-1503`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The body is about 260 lines. Its single interpolation is `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` (line 1281). Estimated size is about 15,500–17,000 characters, leaving roughly 4,000–5,500 characters of headroom. `[NEEDS VERIFICATION]`.
- **Recommended fix:** Move the value to `env: IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` and delete line 1281.

**EXPR-003 — The smoke-test result summary step has about 14 inline step-output interpolations**
- **File:** `.github/workflows/test-and-mark-stable.yml:4474-4630`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** Lines such as `HOT_POLLER_GUARD="${{ steps.hot-poller-guard.outputs.status }}"` (4484) through `CANCEL_UNCONFLICT=…` (4610) make an interpolated body of about 8,000–9,000 characters (estimate), leaving roughly 12,000 of headroom. It grows by one phase line each time a smoke phase is added.
- **Recommended fix:** Move each `steps.*.outputs.*` value into the step's `env:` block.

**EXPR-004 — `Run Codex` in clarify is about 14K characters with an inline prompt, one `${{ }}` away from the limit band**
- **File:** `.github/workflows/clarify.yml:1239-1478`. Prompt heredoc at `:1249-1405`.
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** The body is about 240 lines, roughly 14,000 characters (estimate), with no `${{ }}` today, so the limit does not apply yet. Adding any interpolation would turn the whole body into an expression of about 14,000 characters. The 157-line clarification prompt is embedded inline rather than living under `prompts/`; a search of `prompts/` finds none of its headings (`QUESTION BUDGET`, `ANSWER COMPATIBILITY`).
- **Recommended fix:** Move the heredoc to `prompts/mode-clarify-inline.txt` and set `PROMPT_TEMPLATE_FILE` to that path. This shrinks `clarify.yml` by about 9,000 characters and keeps prompt edits out of workflow diffs.

**EXPR-005 — Workflow file size headroom**
- **File:** `.github/workflows/test-and-mark-stable.yml` (about 416,000 characters plus 425 non-ASCII characters, so about 417,000 bytes estimated); `implement.yml` (about 371,000); `review_autofix.yml` (about 356,000)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** Nothing is near the prompt's 800 KB threshold. That threshold does not apply to this repository, though: the effective limit is 512,000 bytes, with a 480,000-byte guard (`tests/test_workflow_file_size_limit.py:26-27` and §24 of `unattended_system_instructions.md`). `test-and-mark-stable.yml` has about 63,000 bytes of headroom to the guard, only just above §24's "aim for 50,000+".
- **Recommended fix:** Make EXPR-001, EXPR-002 and EXPR-003 the next extraction targets. Move the 780-line body at `test-and-mark-stable.yml:1830-2611` and the 660-line body at `:2611-3273` (neither contains `${{ }}`) into `scripts/test_and_mark_stable_step_<slug>.sh`, following the `review_autofix_step_<slug>.sh` pattern from agents.md "Workflow file size limit".

**EXPR-006 — Gaps in what the expression guard checks**
- **File:** `tests/test_workflow_file_size_limit.py:66-98`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - It only scans `.github/workflows/*.yml` → `jobs.*.steps[].run`. Composite actions under `.github/actions/*/action.yml` (e.g. `install-codex`, `run-soft-error-analyzer`, `claude-pool-token`) are not scanned. `[NEEDS VERIFICATION]`: whether any of their `run:` bodies contain `${{ inputs.* }}`.
  - It does not scan `if:` expressions. The largest one I saw is clarify's job `if:` (`clarify.yml:24-27`, about 1,600 characters), so that is not a risk today.
  - There is no early-warning tier at 15,000 characters.
- **Recommended fix:** Extend the glob to `.github/actions/*/action.yml` (`runs.steps[].run`). Also emit a non-failing warning (via `print`, from `main()`) for bodies at or above 15,000 characters, so EXPR-001 and EXPR-002 show up in CI output.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — The single-use `pr6576-shard-followup.yml` still runs a test job on every push to `main`**
- **File:** `.github/workflows/pr6576-shard-followup.yml:14-64`. Registry entry: `docs/scripts-pending-removal.md:331-340`.
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** The `self-test` job (pip install plus pytest, up to 10 minutes) runs on every `push: branches: [main]`. Only the later `locate` job checks for the completion marker (line 77, `steps.guard.outputs.done`). Once #7072 has its marker comment, every push still uses a runner and adds to the collector's run window.
- **Recommended fix:** Run the three removal preflight checks in the registry entry. If they pass, delete the workflow, `scripts/pr6576_shard_followup.py`, the test file and the registry entry in one PR (§20.F). Otherwise, restrict `self-test` to `pull_request` events.

**CONSIST-001 — `unblock_judge.sh` uses raw `gh api` everywhere instead of the rate-limit-aware helpers**
- **File:** `scripts/unblock_judge.sh:94-106` (sources only `label_helpers.sh`). Call sites include `:131`, `:234`, `:248`, `:262`, `:371`, `:1070`, `:1075`, `:1113` and `:1197`, about 45 in total.
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** None of the reads use `gh_api_retry` or `gh_retry`, unlike `resolve_integration_ref.sh:14-17`, which uses `gh_api_retry` when `gh_helpers.sh` is present. A single 5xx or secondary rate limit ends the run with `outcome=skip reason=item_unreadable`, `comments_unavailable` or `fixup_unavailable`, and the blocked item waits for the next dispatch. Several call sites use the `gh api … --jq … 2>/dev/null || echo '{}'` shape that `gh_helpers.sh:1225-1236` warns about (e.g. line 1197). They happen to fail closed through the follow-up `jq -e`.
- **Recommended fix:** Source `${SUPPORT_DIR}/scripts/gh_helpers.sh` with the same guard used in `resolve_integration_ref.sh:14-17`. Wrap the GET reads in `gh_api_retry`. Writes are safe to route the same way, because `gh_api_retry` already drops unsafe POSTs to a single attempt (`gh_helpers.sh:931-933`).

**CONSIST-002 — The support-ref commit pin (Q64) is only applied in some workflows**
- **File:** `.github/workflows/check_failure_triage.yml:306-314`, `.github/workflows/orchestrate_poll.yml:113-121` and `:469-478`, `.github/workflows/sync_ai_labels.yml:37-45`, plus `cancel_on_pr_close.yml`, `orchestrate.yml` (×2) and `workflow_failure_heal.yml`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** In consumers, these copies always set `SCRIPT_REF=stable`. A consumer that pins wrappers to a SHA (as `clarify.yml:261-269` supports) runs pinned workflow YAML against newer `stable` scripts in these workflows only, which risks version skew between step contracts and helpers.
- **Recommended fix:** Apply the `SUPPORT_REF_WORKFLOW_JOB_JSON` pin logic to every reusable workflow that has a `workflow_call` trigger. The fix lands for free with DUP-003.

**CONSIST-003 — The heal-intake `used_in_job=unknown` gap is not a missing start reading `[NEEDS VERIFICATION]`**
- **File:** `.github/workflows/workflow-failure-heal-intake.yml:137-148` and `:300-310`; `scripts/gh_helpers.sh:66-72`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** This report's GH API Call Audit recommends adding a `phase=start` capture to `workflow-failure-heal-intake`. That step already exists (lines 137-148) and writes the same snapshot file the end step reads. `used` stays `unknown` only when the snapshot is missing or unreadable, the `reset` values differ, or `previous_remaining < remaining` (lines 68-70). The real cause has not been found.
- **Recommended fix:** In `gh_pat_budget`, when `used` stays `unknown`, add `used_reason=snapshot_missing|reset_changed|counter_increased` to the log line. Update the report's recommendation to match.

**CONSIST-004 — Heal intake already de-duplicates by fingerprint before the model runs `[NEEDS VERIFICATION]`**
- **File:** `scripts/workflow_failure_heal_intake.sh:936-1069` (fingerprint and budget) versus `:1688-1747` (diagnosis); `scripts/workflow_failure_heal.py:2067-2076` and `:4135-4166`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** Speed #1 and Cost #3 in this report propose de-duplicating heal intake by failure reason. The intake already runs `budget()` before diagnosis, and returns `action: duplicate` when an open heal issue has the same `fp`. `normalize_error_line` replaces digits with `<n>`, so issue numbers do not split the fingerprint. As far as the source shows, the four intake runs of 72–278s could have ended as duplicates without any model call.
- **Recommended fix:** Before implementing a new de-duplication step, check the `fingerprint fp=` and decision `log` lines in runs 38983250046, 38983567905, 38984163249 and 38986445246. If they show `duplicate`, re-estimate the Opus savings.

**SHELL-001 — Unquoted numeric tests (SC2086)**
- **File:** `.github/workflows/test-and-mark-stable.yml:1194` (`if [ $IDLE -ge $INACTIVITY_LIMIT ]; then`)
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** If either variable is ever empty, for example after a failed `$((…))`, `[` reports `integer expression expected` and `set -e` kills the step. Other tests in the same step are quoted (e.g. line 1010).
- **Recommended fix:** Quote both operands: `[ "$IDLE" -ge "$INACTIVITY_LIMIT" ]`.

**Debt markers:** none found. A case-sensitive search for `TODO`, `FIXME` and `HACK` over `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` returned zero matches.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 11 | BUG-002, BUG-003, SEC-002, BATCH-001, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002, CONSIST-001, CONSIST-002 |
| Low | 17 | BUG-004, BUG-005, SEC-001, BATCH-002, API-002, DUP-003, DUP-004, DUP-005, DUP-006, EXPR-003, EXPR-004, EXPR-005, EXPR-006, DEAD-001, CONSIST-003, CONSIST-004, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/resolve_integration_ref.sh`, `tests/fixtures/integration_ref_resolver_ci_triage/*.json` (2 new) | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/unblock_judge.sh`, `scripts/workflow_failure_heal_intake.sh`, `scripts/gh_helpers.sh`, `.github/workflows/test-and-mark-stable.yml` | Medium |
| Code modularization | New `.github/actions/resolve-integration-ref`, `.github/actions/resolve-support-ref` and `.github/actions/gh-pat-budget`; `scripts/ai_engine.sh`; `scripts/orchestrate_poll_process.sh`; `clarify.yml`, `plan.yml`, `implement.yml`, `validate.yml`, `orchestrate_clarify_respond.yml` and about 8 other workflows | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/clarify.yml`, new `prompts/mode-clarify-inline.txt`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `clarify.yml`, `plan.yml`, `scripts/comprehensive_test_and_release_gh_api.sh`, `scripts/unblock_judge.sh`, `scripts/gh_helpers.sh`, `.github/workflows/pr6576-shard-followup.yml` (and its script, test and registry entry), the support-checkout fallbacks in 10 workflows | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-17)

### Safety Tag Legend
- `SAFE_TO_MERGE` means every precondition is met: the same endpoint (or a strict subset of its fields), identical pagination, filters and auth scope, the same function or step with no mutation in between, unchanged error handling, no concurrency boundary crossed, and any cache contract kept. The implement stage may act on it directly.
- `NEEDS_VERIFICATION` means the change looks correct, but static reading cannot prove all of those preconditions. The named verification steps must pass first.
- `RISKY_SKIP` means at least one hard trigger applies: pagination, a retry, backoff or polling loop, an auth or identity probe, a rate-limit-sensitive path, `orchestrate_poll_process.sh` or race-defence code, or log keys that other code greps. These findings are listed so they are visible, but they must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001 — The post-merge validate dispatch can make a GraphQL `closingIssuesReferences` read and then a REST `pulls/{n}` title/body read for the same PR**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Calls involved:**
  - `.github/workflows/review_autofix.yml:816-829` (GraphQL).
  - `.github/workflows/review_autofix.yml:831-841` (REST fallback).
  - Both sit in the same step and read `POST_MERGE_LINKED_ISSUES_JSON` / `POST_MERGE_PR_TEXT_JSON`, which come from `needs.gate.outputs` (`review_autofix.yml:722-723`).
- **Current call count:**
  - Up to 2 per run, but only when the gate's linked-issues output is `[]` and its PR-text output is empty.
  - The common case is 1, because the REST read is skipped when `POST_MERGE_PR_TEXT_JSON` has text.
- **Proposed call count:** 1 in every case.
- **Endpoints:**
  - `POST /graphql`: `repository.pullRequest(number).closingIssuesReferences`.
  - `GET /repos/{owner}/{repo}/pulls/{number}`, of which only `.title` and `.body` are used.
- **Evidence:** both calls target the same PR, in the same step, with no write in between.
  ```bash
  -f query='query(...) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number labels(first: 100) { nodes { name } } } } } } }'
  ...
  if pr_data_live="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' 2>/dev/null)"; then
  ```
  `pullRequest { title body }` is free to add to the GraphQL selection that is already being sent.
- **Proposed fix:**
  - Add `title body` to the `pullRequest` selection at line 820.
  - Capture `{title, body}` alongside the nodes, for example by changing `--jq` to emit `{nodes: ..., text: (.title + " " + (.body // ""))}`.
  - At line 834, use that captured text instead of the REST read.
  - Keep the REST read only for the case where the GraphQL call itself failed (`post_merge_linked_issue_lookup_failed=true`).
- **Safety rationale:**
  - Endpoint scope and auth are the same, and both calls are in one step with no mutation between them.
  - Error handling does change. Today a GraphQL failure still lets the REST read rescue the text fallback, and the two failure flags (`post_merge_linked_issue_lookup_failed` and `post_merge_pr_text_lookup_failed`) feed different warning text at lines 852-855.
  - That precondition fails unless the REST fallback is kept for the GraphQL-failure path.
- **Downstream signal (verification steps):**
  1. Confirm that the GraphQL path skips the REST read only on success, and that the REST read still runs when line 816 fails. This keeps both lines 852-855 warnings reachable.
  2. Confirm that `normalize_post_merge_issue_nodes_json` (line 807) is fed only the `nodes` array, not the new wrapper object.
  3. Grep `tests/` for fixtures that assert the exact GraphQL query string, and update any that do.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — Each poll cycle fetches the same wave issue's timeline (and the linked PR) up to twice**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - The reconcile loop reads the timeline and PRs for every wave issue: `scripts/orchestrate_poll_process.sh:24724-24767`, with the timeline via `_issue_cross_ref_pr_numbers_unique` at line 24739 and each PR via `_fetch_pr_json` at line 24750.
  - Later in the same cycle, three loops re-read the timeline and then the PR for subsets of the same `WAVE_STATUS.issues`:
    - ready-to-merge: `:25009-25018` (`_issue_cross_ref_pr_number_last` at 25012, `_fetch_pr_json` at 25014);
    - in-progress/done: `:25156-25164` (25158, 25163);
    - review-blocked: `:25295-25309` (25304).
  - Timeline helper: `:4420-4436` (paginated REST fallback at 4435). Selectors: `:4438-4453` (unique) and `:4455-4470` (last). `_fetch_pr_json`: `:2215-2219`.
- **Current call count:** for each wave issue in `ready-to-merge`, `in_progress`, `done` or `review-blocked` status:
  - 1 extra timeline read of at least 1 page (more on later pages);
  - 1 extra `pulls/{n}` read.
  - This is on top of the reconcile loop's reads for the same issue.
- **Proposed call count:** 0 extra timeline reads per cycle; the PR read is reused only when the selected PR number matches.
- **Endpoints:**
  - `GET /repos/{o}/{r}/issues/{n}/timeline` (`--paginate`), or the `gh_issue_timeline_with_cross_refs` GraphQL variant.
  - `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:**
  - `_issue_cross_ref_pr_numbers_unique` and `_issue_cross_ref_pr_number_last` both call `_issue_timeline_with_cross_refs_json "${issue_num}"` (lines 4442 and 4459). They differ only in the jq selector (`unique | .[]` versus `last // empty`).
  - No cycle-local timeline cache exists. `_fetch_pr_json` has no cache either (lines 2215-2219).
- **Net-new detail that BATCH-001 does not cover:**
  - **Selection semantics differ on purpose.** `_last` returns the last cross-referencing PR of any kind. The reconcile loop rejects non-implementation PRs (`_pr_json_is_issue_implementation_pr`, prompted by the #3817/#3825 incident described at lines 18385-18393). Reusing the reconcile loop's *selected* PR would silently change which PR the auto-merge, update-branch and review-blocked loops act on. Only the raw timeline JSON can safely be reused, with each caller keeping its own selector.
  - **The cache cannot live inside the helper.** Line 24739 calls the selector inside `$(...)`, so any variable it sets is lost in that subshell. The ready-to-merge and in-progress loops (`echo ... | while read`, lines 25009 and 25156) are pipeline subshells too. A cache would have to be file-backed under the cycle's runtime directory, or filled in the parent shell before line 24739.
  - Between the reconcile loop and the later loops, `reconcile_managed_issue_labels` writes labels and `capture_intent_fingerprints_for_merged_subissue` runs (line 24987). A PR opened in that gap is seen today and would be missed by a cached timeline until the next cycle.
- **Proposed fix (for manual review only):** add a file-backed cycle-local timeline cache that the reconcile loop fills and `_issue_timeline_with_cross_refs_json` reads first, falling back to a live read on a miss (fail open). Pick a name that does not collide with existing identifiers, checked by grep. Document input and output shape, call count and fail-open behaviour per §14. Consider merging it into BATCH-001's GraphQL batch rather than adding a separate cache.
- **Safety rationale:** several hard triggers apply: the code is in `orchestrate_poll_process.sh`, the timeline read is paginated, and the later loops act on PR state (auto-merge, update-branch) and so defend against upstream races.
- **Downstream signal:** do not implement automatically. A human must decide whether a timeline up to one cycle stale is acceptable for the auto-merge loop at 25009 and the update-branch loop at 25156, and must confirm that `LINKED_PR_CROSS_REF_REJECTED` and the `Issue #N: no linked PR found in timeline.` log output stay the same.

**REUSE-002 — Heal intake reads `/user` twice for `autofix_failure` reports, although `PROVENANCE_LOGIN` already holds the login**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `scripts/workflow_failure_heal_intake.sh:559-562` sets `PROVENANCE_LOGIN` for every non-`workflow_run` kind except a consumer-repo `phase_failure`.
  - `scripts/workflow_failure_heal_intake.sh:1038-1049`: `HEAL_TRUSTED_AUTHOR` falls back only to `PHASE_COMMENT_AUTHOR`, and then calls `/user` again at line 1044.
  - `PHASE_COMMENT_AUTHOR` is set only for `phase_failure` (line 874).
- **Current call count:** 2 `/user` reads per `autofix_failure` intake whose `SOURCE_GEN` is numeric.
- **Proposed call count:** 1.
- **Endpoint:** `GET /user`.
- **Evidence:**
  - Both calls use the same token: `GH_TOKEN` is only *unset*, for the sandboxed model at lines 1729 and 1737, never swapped.
  - The recovery path already reuses the login in exactly this order (lines 1204-1206):
    ```bash
    # Every candidate below is a /user result for the intake's own token.
    recovery_login="${PROVENANCE_LOGIN:-}"
    [ -n "${recovery_login}" ] || recovery_login="${HEAL_TRUSTED_AUTHOR:-}"
    ```
  - The §14 comment at lines 1042-1043 ("the only other /user read is for self-repo phase reports") is stale: line 561 also runs for `autofix_failure`.
- **Proposed fix:** at line 1040, use `HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"` and keep the `/user` call at line 1044 as the fallback when that is empty. That fallback is still needed for consumer-repo `phase_failure`, where line 560 deliberately leaves `PROVENANCE_LOGIN` empty. Then correct the comment at lines 1042-1043.
- **Safety rationale:** the token's identity drives trust decisions here: `--trusted-author` feeds `budget` lineage (line 1054) and `--trusted-login` feeds provenance verification (line 580). That makes it an authentication or identity-probe flow, which is a hard `RISKY_SKIP` trigger, even though the endpoint, token and fail-open handling (`|| true`) are the same.
- **Downstream signal:** do not implement automatically. A reviewer must confirm that, for `autofix_failure`, the provenance login and the heal-lineage trusted author are meant to be the same identity, and must check whether any test asserts two `/user` calls.

### Dead Calls (DEAD-API-###)
No findings. I checked these call results, and each one is consumed downstream:
- `unblock_judge.sh:1075`, `:1113`, `:1723` and `:1766`. The `:1766` diff read uses a different media type from `:1113`, so they cannot merge.
- `workflow_failure_heal_intake.sh:561`, `:564`, `:1012`, `:1024` and `:1098`.
- `review_autofix.yml:834`, `:1197` and `:1599`. The reads at 1197 and 1599 are deliberate freshness re-reads in separate steps.
- `implement.yml:5710`, where `headRefName` comes free in the same call as `files`.

### Cross-References to Deep Audit Section
- BATCH-001: RISKY_SKIP — it sits in `orchestrate_poll_process.sh` and reads a paginated timeline. Net-new detail for the implementer:
  - GraphQL `PullRequest.state` is uppercase (`OPEN`/`CLOSED`/`MERGED`), but `_jq_field ... 'open|closed|merged'` (lines 24760-24761) needs lowercase.
  - The synthesised JSON must keep REST paths `.head.ref`, `.merged_at` and `.body`, because `_pr_json_is_issue_implementation_pr` reads the head branch and closing-keyword body (lines 18395-18400).
  - The `LINKED_PR_CROSS_REF_REJECTED ... head=` log key reads `.head.ref` (line 24756).
  - See REUSE-001 for the same-cycle re-reads.
- BATCH-002: NEEDS_VERIFICATION — net-new detail:
  - The loop at `unblock_judge.sh:404-431` returns on the first matching candidate, so it currently makes 1–3 calls, not always 3.
  - The REST `head=${owner}:ai/issue-N` filter (line 407) restricts results to same-owner heads. GraphQL `pullRequests(headRefName:)` does not filter by head owner, so the batched query must also select `headRepositoryOwner { login }` and filter on it. Otherwise a fork PR with that branch name could be treated as the fix.
  - REST picks the latest `merged_at` (`sort_by(.merged_at) | last`). GraphQL `last:1` orders by creation date, so fetch several nodes and sort them client-side.
  - Candidates must be evaluated in the original candidate order.
- API-001: RISKY_SKIP — the current reads are `--paginate` (`workflow_failure_heal_intake.sh:1012`, `:1024`; `unblock_judge.sh:371`). The proposed search replacement adds two risks:
  - The search API has its own, much lower rate limit.
  - Search indexing lags, so a heal issue filed seconds earlier by a concurrent intake may not appear. That defeats the `action: duplicate` dedup and can open duplicate heal issues. The existing reissue search at `:1098` is safe only because it is fail-open and runs only on the lineage path.
- API-002: RISKY_SKIP — `test-and-mark-stable.yml:1048-1076` is a polling loop with rate-limit backoff state (`RATE_LIMIT_BACKOFF`, lines 1055-1058). Net-new detail:
  - The GraphQL `comments.totalCount` differs from today's `jq 'length'` of a single REST page, which is capped at 100 (line 1061). The `CUR_STATE` activity string (line 1081) would change meaning.
  - Unverified: I believe the per-issue comments list does not support `direction=desc` (line 1060). If so, the current read returns the oldest 100 comments. The REST reference must be checked before either form is relied on.

### Summary Counts
These counts cover the findings in this pass plus the Deep Audit IDs cross-referenced above.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | MERGE-001, BATCH-002 |
| RISKY_SKIP | 5 | REUSE-001, REUSE-002, BATCH-001, API-001, API-002 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.