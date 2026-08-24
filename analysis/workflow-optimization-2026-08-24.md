## Executive Summary

- **No hard failures, but cancellations are masking reliability risk**: 236 runs had 178 successes, 0 failures, 4 cancellations, and 54 skipped/other; CI had 2/6 cancelled runs at `lint / Orchestrate poll process unit tests` around 1,816–1,818s. **Impact:** likely reduce CI reruns/timeouts by sharding or isolating slow tests. **Confidence: high.**
- **Critical-path latency is dominated by review/autofix tails and CI**, not median workflow behavior: overall p50 was 38s but p95 was 1,075s; `review_autofix` p95 was 3,682s with outliers 7,573s, 4,156s, 3,891s, and 3,659s. **Impact:** 30–60 min saved per heavy review if redundant reviewer passes are gated. **Confidence: high.**
- **Poller overhead is systemic runner-time waste**: `orchestrate_poll` ran 89 times, p50 123s/p95 133s, while recent runs such as 32711881577, 32713973172, and 32715820062 found 0 active tracking issues. **Impact:** ~1.5–2.5 runner-hours saved in this window by earlier no-op exit. **Confidence: high.**
- **Cost telemetry is incomplete where it matters most**: workflow-log-analysis run 32710931693 consumed 22 Codex calls / 2,696,954 tokens, but `review_autofix` OpenRouter usage logged 36 calls with token fields as `na`/0 and `cache_hit_rate=null`. **Impact:** token savings cannot be safely quantified until usage/cache fields emit correctly. **Confidence: high.**
- **MCP telemetry needs stricter structure**: Semble had 7 aggregate queries / 63,748 bytes and 32 fallbacks, but 30 fallbacks were contract-test fail-open events; local deep logs also show counter echoes like `SERENA_QUERY 0` that can be miscounted as live telemetry. **Impact:** avoid false SRE conclusions by requiring target/bytes/reason fields. **Confidence: medium.**

## Speed Optimizations

1. **Short-circuit no-work poller runs before checkout/support staging.**  
   **Evidence:** `orchestrate_poll` ran 89 times with p50 123s/p95 133s. Recent runs 32708159280, 32711881577, 32713973172, and 32715820062 logged runner waits and then found 0 active tracking issues.  
   **Root cause:** the poller still pays runner provisioning, checkout/helper setup, memory event writes, and artifact/state steps even when there is no active work.  
   **Exact change:** move `gh issue list` active-tracking detection to the first minimal step, skip repo/support checkout, state snapshot, and memory start/end writes when active count is 0; emit `POLL_NOOP_SUMMARY active_issues=0 elapsed_ms=... skipped_steps=...`.  
   **Estimated savings:** 60–100s per no-op poll; ~1.5–2.5 runner-hours in this sample.  
   **Risk:** low; keep current path when active issues >0.

2. **Shard or isolate the long CI orchestrator test suite.**  
   **Evidence:** CI p50 1,725s/p95 1,817.5s; runs 32681830722 and 32689538463 cancelled at `lint / Orchestrate poll process unit tests` after ~30 min while still printing passing tests.  
   **Root cause:** one monolithic lint/test job serializes many orchestrator, validation, fingerprint, and stall-recovery tests.  
   **Exact change:** split fast syntax/unit checks from slow orchestrator integration/fingerprint tests using an existing matrix or separate jobs; add `TEST_CASE_TIMING name=... elapsed_ms=...` around shell-invoked tests.  
   **Estimated savings:** 8–15 min on CI critical path; lower timeout/cancellation rate.  
   **Risk:** medium; preserve required branch protection by keeping all shards required.

3. **Gate redundant review/autofix model passes.**  
   **Evidence:** slow review runs 32681830960, 32682360295, and 32689552858 each logged 12 exact OpenRouter usage events: 6 `pass1` + 6 `review` calls across six models, spanning ~28–40 min of wall time.  
   **Root cause:** full multi-model review is repeated even when deterministic gates and ledger state could narrow the second pass.  
   **Exact change:** after `pass1`, run the second pass only for models/files with material findings or changed head SHA; emit `REVIEW_PASS_SUMMARY pass=1 findings=... files=... continue_models=... elapsed_ms=...`.  
   **Estimated savings:** 25–45 min per heavy review/autofix run.  
   **Risk:** medium; keep full pass for high-risk diffs or reviewer disagreement.

4. **Make free-disk cleanup conditional.**  
   **Evidence:** `jlumbroso/free-disk-space` took ~86s, 89s, 105s, and 114s in slow review/autofix logs, saving 5.3GiB in at least one run.  
   **Root cause:** disk cleanup runs unconditionally on review jobs.  
   **Exact change:** check free space first; run cleanup only below a configured threshold or before known large edit/test phases; log `DISK_CLEANUP_DECISION free_gb=... ran=true|false elapsed_ms=...`.  
   **Estimated savings:** ~1.5 min per review run when skipped.  
   **Risk:** low-medium; fail open to cleanup if disk check fails.

## Cost Optimizations

1. **Reduce workflow-log-analysis prompt fan-out.**  
   **Evidence:** run 32710931693 used 22 Codex calls / 2,696,954 tokens and lasted 1,075s. Logs show repeated report/prompt text in fan-out steps.  
   **Root cause:** repeated large context/instructions across analysis passes.  
   **Exact change:** produce one compact structured summary first, pass run IDs and selected excerpts by reference, and de-duplicate static instructions before fan-out.  
   **Estimated savings:** 30–50% of that run’s tokens, roughly 0.8–1.3M tokens on similar runs.  
   **Quality risk:** medium; retain deep-dive links/excerpts for top outliers.

2. **Trim review/autofix ensemble calls after first pass.**  
   **Evidence:** aggregate `or_calls=36`; slow review logs show 12 OpenRouter usage events per heavy run, but all token/cache fields are `na`.  
   **Root cause:** six-model, two-pass pattern is expensive and not token-attributed.  
   **Exact change:** second-pass gating as above; add `model`, `phase`, `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `latency_ms`, and `finish_reason` to usage logs.  
   **Estimated savings:** 33–50% of review model calls on heavy runs.  
   **Quality risk:** medium; preserve full ensemble for large/risky diffs.

3. **Fix cache observability before tuning cache policy.**  
   **Evidence:** `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`; review logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false` and `cache_enabled=true`, but usage fields are `na`.  
   **Root cause:** provider usage/cache fields are not captured or normalized.  
   **Exact change:** emit `PROMPT_CACHE_USAGE provider=openrouter cache_enabled=true read_tokens=... write_tokens=... stable_prefix_hash=... dynamic_suffix_bytes=...`.  
   **Estimated savings:** unknown until measured; likely meaningful on repeated review prompts.  
   **Quality risk:** none; telemetry-only.

4. **Keep Semble, but add value attribution.**  
   **Evidence:** aggregate Semble: 7 queries / 63,748 bytes; deep review logs show `target=reviewer-context` queries of 14,323 bytes and one `target=overflow` query of 6,456 bytes.  
   **Root cause:** Semble appears to supply bounded context, but logs do not show avoided prompt bytes or selected chunk utility.  
   **Exact change:** log `SEMBLE_QUERY target=... chunks=... bytes=... prompt_bytes_avoided=... selected_files=...`.  
   **Estimated savings:** likely positive, but unquantified.  
   **Quality risk:** low.

5. **Treat Serena aggregate counts as unverified until structured lines exist.**  
   **Evidence:** aggregate shows 2 Serena queries/fallbacks/probe-skips, but local deep logs show counter text like `SERENA_QUERY 0` from an audit command, not structured `target=...` telemetry.  
   **Root cause:** parser likely matches unstructured counter echoes.  
   **Exact change:** require telemetry regexes to include structured fields such as `target=`, `result=`, `tool=`, or `reason=`.  
   **Estimated savings:** small direct savings; high diagnostic value.  
   **Quality risk:** none.

## Reliability Improvements

1. **Prevent CI timeout cancellations.**  
   **Failure evidence:** CI runs 32681830722 and 32689538463 cancelled at `lint / Orchestrate poll process unit tests`; both were near 30 minutes and still printing passing tests before cancellation.  
   **Root cause category:** timeout/monolithic test execution.  
   **Exact fix:** shard slow tests, add per-test timing, and fail fast on true failures while allowing long integration shards separate budgets.  
   **Expected impact:** reduce CI cancellation rate from 2/6 in this window toward 0.  
   **Rollback/fail-open:** revert to single lint job if matrix flakiness appears.

2. **Add single-flight protection to review continuation dispatch.**  
   **Failure evidence:** review run 32682360295 logged `AUTOFIX_CONTINUATION_DISPATCH_ISSUED` and `AUTOFIX_DISPATCH_ISSUED ... continuation=true` for PR #3786 after a long run.  
   **Root cause category:** rerun/continuation coordination risk.  
   **Exact fix:** write a short-lived lease keyed by `pr_number + head_sha + workflow_family`; skip dispatch if a matching active or recently completed run exists; log `AUTOFIX_SINGLE_FLIGHT_DECISION`.  
   **Expected impact:** fewer duplicate long review runs and lower tail latency.  
   **Rollback/fail-open:** if lease write/read fails, continue current behavior with a warning.

3. **Distinguish healthy MCP contract fallbacks from production availability failures.**  
   **Failure evidence:** CI logged 30 Semble fallbacks, all `context=contract-test` and `reason=missing_semble`; aggregate also reports 2 Semble runtime fallbacks and 2 Serena fallbacks/probe-skips, but deep logs suggest some may be counter echoes.  
   **Root cause category:** mixed test telemetry and runtime telemetry.  
   **Exact fix:** emit `context=contract-test|runtime`, `target=...`, and `result=...` on every MCP line; make parser ignore lines without required fields.  
   **Expected impact:** avoids masked broken rollouts and false alerts.  
   **Rollback/fail-open:** keep existing fail-open behavior for missing MCP, but alert only on structured runtime failures.

4. **Track policy/prompt pressure even when zero.**  
   **Evidence:** aggregate `break_glass_count=0` and `context_budget_warn_count=0`.  
   **Root cause category:** no current pressure observed, but absence should be emitted per workflow.  
   **Exact fix:** add per-run `SAFETY_BUDGET_SUMMARY break_glass=0 context_warn=0 max_prompt_tokens=...`.  
   **Expected impact:** faster diagnosis when pressure appears later.  
   **Rollback/fail-open:** telemetry-only.

## AI Memory Health

- **Observed telemetry:** actual `AI_MEMORY_TELEMETRY` JSON appeared in recent `orchestrate_poll` runs such as 32715820062, 32711881577, 32713973172, and 32708159280 for `op=record-run-event`, `event_type=poll_started|poll_completed`, `ok=true`, `push_attempts=1`.
- **Retrieve health cannot be computed:** no deep-dive `AI_MEMORY_TELEMETRY` entries with `op=retrieve` were found, so retrieve hit rate, average `estimated_tokens`, budget use, and `keyword_method` distribution are unavailable.
- **Memory retrieval may be ineffective in reviewer path:** supplied log summaries for Copilot reviewer runs 32689542408 and 32682363043 report built prompts around 58k tokens with memory retrieval success count 0, but those are not structured `AI_MEMORY_TELEMETRY` lines.
- **Recommendation:** emit structured memory telemetry for `retrieve`, `record-candidate`, `finalize-task`, `promote`, and `compact` on every AI workflow, including `records_selected`, `estimated_tokens`, `budget_tokens`, `keyword_method`, `fail_open`, `enabled`, and `push_attempts`.

## GH API Call Audit

- **Poller hotspot:** `orchestrate_poll` invokes `gh issue list` per poll run; recent no-op runs found 0 active tracking issues.  
  **Recommendation:** keep the call, but make it the first minimal preflight and log `GH_API_CALL_SUMMARY endpoint=issues/list count=1 elapsed_ms=... active_issues=0`.  
  **Estimated reduction:** not fewer API calls, but 60–100s less runner work per no-op.

- **Review sweep hotspot:** recent sweep runs 32710365305, 32713073181, and 32715570139 call `gh api --paginate repos/${REPOSITORY}/pulls` and workflow-runs endpoints, then report `candidates=0`.  
  **Recommendation:** cache open PR list and active workflow-run lookup within the sweep step; skip workflow-run pagination when `candidates=0`.  
  **Estimated reduction:** 1–2 API families avoided on empty sweeps.

- **Review gate/API reuse:** PR #3786 review gate logs show PR metadata, linked-issue GraphQL, commit metadata, and file pagination paths.  
  **Recommendation:** fetch PR metadata/files once in gate, write a JSON artifact/env file, and reuse it in codex-agent/apply/push phases.  
  **Estimated reduction:** medium; exact call reduction unavailable because no per-run API counter exists.

- **Rate-limit evidence:** no HTTP 429 or secondary rate-limit events were observed in sampled logs.  
  **Diagnostic addition:** wrap `gh_retry` to emit per-step counts, retries, status class, and endpoint family; aggregate into `GH_API_CALL_SUMMARY`.

## Prompt Cache & Memory System

- **Cache status is not auditable:** aggregate `cache_hit_rate=null`; OpenRouter cache read/write tokens are 0; review usage lines show `cache_enabled=true` but token fields are `na`.
- **Likely fragmentation risks:** large dynamic review/log context, run IDs, timestamps, and per-run environment dumps appear before or around prompt content. This is an inference from logs and summaries, not directly measured by cache hashes.
- **Concrete fix:** split stable system/rubric/repo guidance into a stable prefix, put dynamic PR/log data last, and emit `stable_prefix_hash`, `dynamic_suffix_bytes`, and cache read/write tokens.
- **Memory effectiveness gap:** no retrieve telemetry means memory hit rate is unknown; Copilot summaries reporting 0 retrieval success should trigger structured memory logging.
- **Context pressure:** `CONTEXT_BUDGET_WARN=0`, so no observed prompt-size breach in this window, but 58k-token reviewer prompts and 2.7M-token log analysis run justify proactive budget summaries.

## Orchestrator Health

- **Poller is healthy but noisy:** 89/89 `orchestrate_poll` runs succeeded, but many recent runs found no active work after paying ~2 minutes each.
- **Skipped family runs look expected but under-explained:** `clarify` had 14 skipped, `plan` 13 skipped, `implement` 13 skipped, and `orchestrate_clarify_respond` 13 skipped. Current logs do not consistently expose parent issue, phase, and skip reason.
- **Review/autofix has a long-tail coordination problem:** median `review_autofix` is only 8s because many sweeps are empty, but p95 is 3,682s due heavy PR review cycles.
- **Recommended indicators:** `ORCH_STATE_TRANSITION issue=... from=... to=... reason=...`, `ORCH_SKIP_SUMMARY family=... reason=...`, `POLL_ACTIVE_WORK_COUNT`, `AUTOFIX_SINGLE_FLIGHT_DECISION`, and `REVIEW_CYCLE_AGE_SECONDS`.

## Pipeline Flow Bottlenecks

- **Clarify → plan → implement:** mostly skipped/no-op in this window; bottleneck is observability of skip reason, not runtime.
- **Poll/orchestrate:** dominant high-frequency overhead; runner wait + no-op poll execution creates systemic runner-time waste.
- **Review/autofix:** dominant critical-path tail; full multi-model review cycles and continuation dispatch drive hour-scale runs.
- **Validate/CI:** monolithic CI test execution creates timeout/cancellation risk and blocks merge confidence.
- **Merge/conflict overhead:** forward-merge and promote workflows were short in sampled data; no primary bottleneck observed.
- **Queueing:** “Waiting for a runner” / “Job is waiting for a hosted runner” appeared across poll, review, CI, and workflow-log-analysis logs; add queue-vs-execution timing to separate GitHub-hosted runner delay from pipeline work.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** `review_autofix` p95 3,682s with outliers up to 7,573s; CI p50 1,725s with 2 cancellations; `orchestrate_poll` 89 runs at p50 123s mostly no-op in recent samples.
- **Top failure modes:** CI timeout cancellations, review continuation/rerun risk, and incomplete AI/cache/MCP telemetry.
- **Highest-cost drivers:** workflow-log-analysis run 32710931693 at 2,696,954 Codex tokens; review/autofix 36 OpenRouter calls with missing token attribution.
- **Top 3 actions:**
  1. Add early no-op poll exit and `POLL_NOOP_SUMMARY`.
  2. Gate second-pass review/autofix model calls and emit `REVIEW_PASS_SUMMARY`.
  3. Shard CI slow orchestrator tests and emit `TEST_CASE_TIMING`.

## Metrics Appendix

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | Avg | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All workflows | 236 | 178 | 0 | 4 | 54 | 204.1s | 38.0s | 1,075.0s |
| shubhodeep1/coding-workflows | 236 | 178 | 0 | 4 | 54 | 204.1s | 39.5s | 996.3s |

| Workflow family | Runs | Outcome mix | Avg | p50 | p95 | Key telemetry |
|---|---:|---|---:|---:|---:|---|
| `orchestrate_poll` | 89 | 89 success | 123.0s | 123.0s | 133.0s | frequent no-op active issue count 0 in recent runs |
| `review_autofix` | 59 | 56 success, 2 cancelled, 1 skipped | 393.8s | 8.0s | 3,682.2s | 36 OR calls; Semble review-context queries |
| `ci` | 6 | 4 success, 2 cancelled | 1,718.8s | 1,725.0s | 1,817.5s | 30 Semble contract-test fallbacks |
| `workflow_log_analysis` | 1 | 1 success | 1,075.0s | 1,075.0s | 1,075.0s | 22 Codex calls / 2,696,954 tokens |
| `copilot_pull_request_reviewer` | 5 | 5 success | 177.6s | 168.0s | 226.6s | summaries report ~58k-token prompts, memory retrieval count 0 |
| `clarify` | 14 | 14 skipped | 3.3s | 1.0s | 9.3s | skip reason not consistently structured |
| `plan` | 13 | 13 skipped | 2.6s | 1.0s | 9.4s | skip reason not consistently structured |
| `implement` | 13 | 13 skipped | 3.2s | 1.0s | 9.4s | skip reason not consistently structured |

| Cost/cache metric | Assembled context value |
|---|---:|
| Runs with log telemetry | 115 |
| Wall-clock samples | 114 |
| `wall_clock_p50_ms` | 98,500 |
| `wall_clock_p99_ms` | 4,121,550 |
| Codex calls | 22 |
| Codex tokens | 2,696,954 |
| OpenRouter calls | 36 |
| OpenRouter prompt/completion/total tokens | 0 / 0 / 0 logged |
| OpenRouter cache write/read tokens | 0 / 0 logged |
| `cache_hit_rate` | null |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

| MCP/server metric | Count/bytes | Notes |
|---|---:|---|
| Semble queries | 7 / 63,748 bytes | Aggregate context; local deep logs show reviewer-context and overflow targets |
| Semble fallbacks | 32 | 30 contract-test, 2 runtime aggregate |
| Semble contract-test fallbacks | 30 | CI `target=overflow`, `context=contract-test`, `reason=missing_semble` |
| Semble runtime fallbacks | 2 | Needs verification; local workflow-log-analysis lines include counter echoes |
| Serena queries | 2 | Aggregate only; no structured target/tool bytes observed |
| Serena fallbacks | 2 | Aggregate only; likely parser needs stricter matching |
| Serena probe ok / failed / skipped | 0 / 0 / 2 | Target unavailable in aggregate |
| Other MCP servers observed | 0 | No unknown `<NAME>_QUERY/FALLBACK/PROBE` lines observed |

| Deep-dive MCP target | Observed runs | Query/fallback evidence | Availability interpretation |
|---|---|---|---|
| Semble `reviewer-context` | 32681830960, 32682360295, 32689552858 | 3 queries × 14,323 bytes | useful bounded reviewer context |
| Semble `overflow` | 32689552858 | 1 query / 6,456 bytes | bounded workflow-file overflow context |
| Semble `overflow` contract-test | CI slow runs | 30 fallbacks, `context=contract-test` | healthy fail-open test coverage |
| Serena unknown | 32710931693 aggregate | 2 query/fallback/probe-skipped counts | unverified; require structured fields |

| GH API hotspot | Evidence | Risk | Recommendation |
|---|---|---|---|
| Poll issue lookup | `gh issue list` in recent `orchestrate_poll` runs; active count 0 | low rate-limit, high runner waste | first-step preflight + `GH_API_CALL_SUMMARY` |
| Review sweep PR/workflow enumeration | recent sweeps 32710365305, 32713073181, 32715570139, candidates 0 | redundant calls on empty sweeps | skip workflow-run checks when no candidates |
| Review gate PR metadata/files/commits | PR #3786 slow review gate/codex-agent logs | repeated lookups, unmeasured count | fetch once, share JSON artifact/env |
| Tracker comment upsert | workflow-log-analysis 32710931693 uses issue/comment API | low | emit call counts/retries/status |
