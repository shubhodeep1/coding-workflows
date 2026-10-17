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