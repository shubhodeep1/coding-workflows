## Executive Summary

- **Fix metering before using it to set cost priorities.** In `shubhodeep1/coding-workflows`, failed implement runs `37856245085`, `37861086888`, and `37862435230` account for 6,610,695 of 6,610,697 reported Codex tokens. Their matches come from *echoed examples in workflow shell code*, not verified model usage. Add producer-side structured usage events and exclude echoed commands from parsing. **Impact:** removes a material false cost signal; **confidence: high**.
- **Review scheduling and execution dominate elapsed time.** Across 183 review/autofix runs, p50 is 483 seconds and p95 is 2,143.5 seconds. In failed runs `37855612051` and `37863790469`, the gate finished roughly 24 and 28 minutes before the reviewer job began waiting for a runner. Log gate-ready, job-queued, runner-picked, and concurrency-state timestamps before changing dispatch behavior. **Potential impact:** up to the observed gap on affected runs if avoidable serialization is confirmed; **confidence: medium**.
- **CI has a concentrated regression.** Seven of 16 CI runs failed; five failed the merged-PR guard’s `test_template_copies_are_identical` (`37860827117`, `37864440428`, `37864932438`, `37864981340`, `37865800241`). Restore template parity and run that contract early; do not weaken the guard. **Impact:** addresses five observed failures; **confidence: high**.
- **Review-workspace initialization needs an earlier, classified failure.** Three reviews (`37855612051`, `37860320131`, `37863790469`) reached *Checkout PR head branch* before reporting that the review workspace was unavailable. Record whether the workspace path was unset, absent, or removed, and validate it immediately after creation. **Impact:** avoids later setup and makes recurrence diagnosable; **confidence: high**.
- **MCP and token totals also contain duplicate log events.** The full logs contain six distinct Semble queries totaling 76,213 bytes, while their collector rows count nine and 115,152 bytes. Aggregate-job and individual-step copies have slightly different timestamps, defeating exact-line deduplication. Deduplicate by event identity, not whole timestamped line. **Impact:** corrects measured volume rather than saving runtime; **confidence: high**.

## Speed Optimizations

1. **Critical path — explain the gate-to-review gap first.** Review gate logs end at approximately 22:59:39 UTC in `37855612051`, 23:49:36 in `37860320131`, and 00:17:01 in `37863790469`; reviewer-system logs next show runner waits beginning about **24, 16, and 28 minutes** later. The cause is *not established*: dependency or concurrency delay may account for the gap, and `run_started_at` equals `created_at` in all 1,000 collector rows, so it cannot separate queueing. Emit `gate_ready_at`, `job_queued_at`, `runner_picked_at`, concurrency key, PR number, and head identifier as bounded diagnostic fields. If these show redundant same-head dispatches, coalesce only those dispatches while preserving forced and changed-head reviews. **Estimated saving:** up to 16–28 minutes per confirmed redundant wait; **risk: medium** until state transitions are measured.
2. **Critical path — fail earlier on missing review workspace.** The three checkout failures above lasted 2,199–2,489 seconds end-to-end, although the workspace error occurs inside the reviewer job; those totals must **not** be presented as checkout-step time. Validate `WORKSPACE_PATH` immediately after workspace setup, logging a categorical reason and setup-step duration before memory retrieval or agent preparation. **Estimated saving:** subsequent setup time, plausibly minutes rather than the entire run duration; measure it with the new timestamps. **Risk: low**; keep the existing refusal to start agents.
3. **Failed-CI path — surface template drift sooner.** CI runs `37864440428` and `37865800241` took 696 and 589 seconds and failed the same guard-copy assertion. Put the existing parity check in the earliest applicable CI stage, without dropping later tests. **Estimated saving:** up to the remaining CI time on drifted commits, not on passing runs; **risk: low**.
4. **Micro-optimization — retain lazy Semble loading.** Six distinct full-log bootstraps took 210,401 ms combined; reviewer queries themselves logged 404–604 ms. In `37859850305`, bootstrap was 36,406 ms against a 2,147-second run. Log install and index cache reuse before optimizing startup; disabling context retrieval is not supported by this latency evidence. **Estimated saving:** at most tens of seconds per affected run; **risk: low** for reuse, higher for removal.

## Cost Optimizations

1. **Repair cost attribution before model or budget changes.** `scripts/cost_audit.py` matches “tokens used” inside echoed shell examples: each of the three failed implement logs contributes the same spurious 1,322,139-token pattern, with two runs counted twice. Emit a structured `CODEX_USAGE` event from the actual CLI result, keyed by run, attempt, and invocation; test against echoed-script fixtures. **Estimated saving:** none directly; **accounting correction:** at least 6,610,695 reported tokens cannot be treated as verified usage. Dollar savings and actual Codex consumption remain unknown. **Quality risk:** none.
2. **Prevent work that cannot pass authorization or task preflight.** Implement runs `37856245085` and `37862435230` failed after model work when automation-path grants rejected respectively three and two staged paths; `37861086888` ended in a deliberate BLOCKED verdict because the expected target was not in its checkout. Check branch/task identity and permitted path scope *before* model execution where those inputs are knowable. Log preflight decision, permitted-scope category, and elapsed time. **Estimated saving:** one otherwise avoidable model attempt per confirmed preflight mismatch; tokens and dollars are unquantifiable with current metering. **Quality risk:** low if the existing final guard remains authoritative; never fail open on a denial.
3. **Measure, then trim repeated review context.** Review runs `37856496606`, `37859850305`, and `37860827535` contain **five distinct** review `CONTEXT_BUDGET_WARN` events, with logged prompts from 142,051 to 191,462 tokens. The assembled collector counts eight because three events are duplicated. Log stable-prefix bytes, dynamic-context bytes, retrieved-context bytes, and per-pass prompt size; move changing status/noise behind reusable instructions and trial bounded context trimming against review findings. **Illustrative saving:** a verified 5% reduction on a 190,728-token warned prompt would be about 9,536 prompt tokens; this is a scenario, **not** an observed saving. **Quality risk: medium**; compare findings before rollout.
4. **Do not change model tiers on this evidence alone.** `37866817342` configured a high-reasoning unblock judge, while review logs show multiple configured models; the assembled OpenRouter usage has 32 unavailable of 101 counted calls, and counted lines can repeat. Add unique invocation IDs, model, reasoning setting, outcome, and provider usage availability. Only then trial lower reasoning for non-decision summaries, retaining the judge and security decisions. **Estimated saving:** unknown pending corrected per-invocation costs; **quality risk: medium**.
5. **Keep MCP contributions selective.** Distinct Semble `reviewer-context` queries supplied 70,534 logged bytes and 30 sources across five full-log queries; one `conflict-resolver-context` query supplied 5,679 bytes and five sources in `37860118534`. Logged `static_dup_bytes=0` on reviewer events is encouraging, but there is no comparison proving reduced prompt expansion or improved findings. Record included-versus-discarded bytes and downstream prompt delta per target before reducing chunks. Serena has zero queries, tool calls, and response bytes, so no replacement benefit or low-value-response problem can be assessed. **Estimated saving:** unknown; **quality risk: medium** for context cuts.

## Reliability Improvements

1. **Restore guard-copy parity without bypassing protection.** The five CI failures named in the executive summary assert that merged-PR guard copies differ. Root-cause category: **repository/template drift**. Sync the copies and retain the equality test; log both copy identifiers and the first differing location, without dumping file contents. **Expected impact:** remove the repeated CI failure mode; **rollback:** revert the synchronization, never disable the guard.
2. **Classify and fail early on review workspace loss.** Three `review / codex-agent` checkout failures report “Review workspace is unavailable.” Root-cause category: **workspace setup or lifecycle**, not demonstrated Git fetch failure. Log setup result and categorical missing-path reason at the setup boundary; allow a bounded retry only for a demonstrated transient creation failure. **Expected impact:** earlier detection and targeted recovery; **fail-open:** none when workspace integrity is uncertain.
3. **Preserve authorization and isolation stops.** Implement runs `37856245085`/`37862435230` had grant denials; review run `37860118534` stopped because a host-only conflicted path required manual merge and resolver isolation was unavailable (`reason=sandbox_path_host_only`). Root-cause categories: **scope authorization** and **conflict isolation**. Log denial counts, scope category, isolation decision, and handoff state; route the latter to manual resolution. **Expected impact:** fewer blind redispatches, not automatic clearance; **rollback/fail-open:** retain both hard stops.
4. **Separate network transience from application failures.** `37862635273` failed *Install OpenCode CLI* with npm `ECONNRESET`. Add a small bounded retry around that installation, logging attempt, failure class, and backoff, while retaining a terminal failure. **Expected impact:** reduce reruns for transient install errors; **risk: low**.
5. **Treat Semble fallbacks by context, not one global alarm.** The assembled 48 fallbacks comprise **44 counted CI contract-test events** targeting `overflow` with an intentionally missing binary and **four runtime** `binary-unavailable` events, all in implement run `37856245085` on `overflow`. Full-log inspection finds 28 distinct CI test events: duplicate aggregate/step logs explain the higher count. The four runtime events are healthy *fail-open behavior* for that run but, with zero successful implement queries observed, warrant checking whether the optional binary was staged; they do **not** prove a broadly broken rollout. Add one per-run availability result and preserve the legacy-context fallback. Serena emitted no probe, query, or fallback; a recent review (`37866853445`) reports it disabled, so zero probe failures are **not** proof of availability.
6. **Interpret pressure signals narrowly.** `BREAK_GLASS` is zero in available telemetry: no observed rubric override. The five distinct review budget warnings indicate **prompt-size pressure**, not a demonstrated policy failure. Log warning by invocation and unique prompt identifier; avoid counting aggregate and step copies twice.

## AI Memory Health

Across **14 deduplicated `retrieve` events** in the available deep-dive logs, **14/14 selected records**; average `estimated_tokens` was **1,430** against an average **1,443-token budget**. Keyword methods were **11 `llm`, three `plain`, zero `none`**. No sampled retrieve returned zero records or reported `enabled: false` or `fail_open: true`. This is good sampled retrieval coverage, not a 1,000-run hit-rate estimate.

Writes merit a small diagnostic addition: `review_autofix` run `37860320131` logged three push attempts for both its start and failure events, and failed-install run `37862635273` logged four for its failure event. Emit retry cause, backoff time, and final push outcome once per operation; alert on repeated high attempts without turning memory writes into a workflow blocker. A summarized, non-deep-dive status-sync run (`37866165895`) reports successful `finalize-task` and two push attempts, but its underlying step log is not in this archive.

## GH API Call Audit

- **Highest observed budget signal:** `GH_PAT_BUDGET phase=end` reports `used_in_job=506` for `review_autofix / codex-agent` run `37860118534`, a 469-second run. This is a PAT-budget delta, **not an endpoint-level inventory**; the shared quota and concurrent work limit attribution. Of 25 distinct end-budget rows inspected, the other **24 say `unknown`**. No explicit rate-limit error was found in the inspected full logs, which does not establish that none occurred elsewhere.
- **Redundancy remains unmeasured.** Neither the supplied aggregates nor these logs provide calls by normalized endpoint, item, page, retry, or cache hit. Add counters at the existing `gh` helper boundary: workflow/job/step, endpoint template, read/write, pages, attempts, HTTP or rate-limit class, cache-hit, and bounded latency. Do **not** log tokens, URLs with query contents, or response bodies, and do not make another API call solely to measure one.
- **Optimize only a demonstrated hotspot.** Repo rule `CLAUDE.md` §15 requires checking existing reads first, batching per-item lookups, and reusing cycle-local caches; `agents.md` documents `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`. If endpoint counters show 50 independent lookups for the same cycle, replacing them with one supported 50-alias fetch could remove **up to 49 calls** for that case; this is a conditional estimate, not a claim about the 506-unit run. Preserve the documented cache-miss fallback. Log repeated-key counts in orchestrator loops to identify missed prefetch opportunities and rate-limit risk before changing them.

## Prompt Cache & Memory System

The assembled telemetry reports **60,943,081 OpenRouter cache-read tokens** and **2,908,942 cache-write tokens**, but `cache_hit_rate` is **unavailable**, not zero: `scripts/cost_audit.py` returns null when any usage call is unavailable, and the context records **32 unavailable of 101 counted calls**. Aggregate/step duplication also affects counted OpenRouter usage. Neither a dollar cache benefit nor a provider hit percentage is defensible yet.

Keep stable rubric and tool instructions ahead of changing PR state, timestamps, and retrieved excerpts; log stable-prefix hash and byte length, dynamic-context length, cache breakpoint, provider cache fields, and invocation ID. This makes fragmentation and prompt variance measurable without logging prompt content. The five distinct budget warnings in three review runs warrant a bounded-context trial; memory retrieval itself averaged roughly 1.4k tokens, so blaming memory for approximately 190k-token warned prompts would be unsupported. No cache fail-open rate is supplied: emit cache-attempt/result and fallback-retry status per invocation, then assess latency, billed tokens, and review quality together.

## Orchestrator Health

Eight `orchestrate_poll` runs succeeded (p50 **627.5 s**, p95 **964.7 s**), but no wave-transition or conflict-heal retry totals are supplied. Do not treat CI’s deliberately exercised “integration fingerprint verification FAILED” test output as live orchestrator failures; CI run `37860675537` actually failed an orchestrate-poll test shard (**32 passed, one failed**).

There are observable handoffs needing clearer state logs: implement run `37861086888` gave a deliberate BLOCKED verdict on attempt one, with no retry; unblock-judge run `37866817342` recorded issue `#6785` as `scope-blocked`, fixup `#6794`, `outcome=waiting`; intake run `37865439033` skipped with `provenance_rejected` and `unexpected_workflow_path`. Emit one structured transition per issue/PR with prior state, next state, reason category, attempt, and next allowed action. Track age in waiting/blocked, unchanged-head review dispatches, wave advancement, deferrals, conflict-heal attempts, and terminal-without-handoff counts. Preserve provenance rejection and BLOCKED no-retry behavior.

## Pipeline Flow Bottlenecks

| Stage | Evidence and bottleneck class | Safest next action |
|---|---|---|
| Clarify → plan | Clarify: 153/162 skipped; plan: 140/152 skipped. Their 1-second family medians largely describe skipped dispatches, **not** task-processing speed. | Log eligibility and skip reason before interpreting cadence. |
| Implement | Only five of 152 runs executed to success/failure: two success, three failure; two grant denials and one deliberate BLOCKED verdict. **Preflight/authorization overhead** is demonstrated; model time is not separately metered. | Validate scope and target identity early; retain guards. |
| Review/autofix | 183 runs, p50 483 s, p95 2,143.5 s; gate-to-review gaps of about 16–28 minutes in three failures. **Scheduling/dependency delay versus compute** is unresolved. | Timestamp gate-ready, concurrency wait, runner pickup, model passes, and checkout. |
| Validate/CI | Seven failures in 16 runs, five on one guard-copy test. **Retry/rework overhead** is likely if commits are resubmitted, but no `run_attempt>1` or measured rerun link exists. | Restore parity and run the test early; link CI run to PR head for rework measurement. |
| Orchestrate/merge | Eight successful poll runs still have p50 627.5 s; `37860118534` stopped a host-only conflict for manual merge. **Merge/conflict overhead** is evident in that review, not quantified across projects. | Log conflict classification, manual handoff, and time to next state. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review tail latency and unexplained gate-to-review gaps; slow CI and poll runs. **Top failure modes:** guard-template drift (five CI runs), unavailable review workspace (three runs), and implement scope/checkout stops (three runs). **Highest-cost drivers:** review OpenRouter usage is large but duplicated and partly unavailable; reported Codex usage is largely invalid. **Prioritized actions:** (1) repair metering and add queue/API event identities; (2) restore guard parity and classify workspace setup failures; (3) use corrected per-invocation data to trial prompt/context reductions without weakening review or authorization.

## Metrics Appendix

**Window:** October 8, 2026 22:26:56 UTC–October 9, 2026 00:52:29 UTC; collector generated October 9 at 00:56:16 UTC. One repository; success-log sampling configured at 7%. “Skipped” is separate from failure; overall rates use all 1,000 runs.

| Workflow family | Runs | Success | Failure | Skipped | Success / failure rate | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 305 | 15 | 680 | 30.5% / 1.5% | 2 / 794 s |
| Review/autofix | 183 | 178 | 5 | 0 | 97.3% / 2.7% | 483 / 2,143.5 s |
| CI | 16 | 9 | 7 | 0 | 56.3% / 43.8% | 1,086.5 / 1,275.75 s |
| Implement | 152 | 2 | 3 | 147 | 1.3% / 2.0% | 1 / 11 s |
| Plan | 152 | 12 | 0 | 140 | 7.9% / 0% | 1 / 655.4 s |
| Clarify | 162 | 9 | 0 | 153 | 5.6% / 0% | 1 / 166.8 s |
| Orchestrate poll | 8 | 8 | 0 | 0 | 100% / 0% | 627.5 / 964.7 s |

| Assembled cost/review metric | Reported value | Interpretation |
|---|---:|---|
| Codex calls / tokens | 59 / 6,610,697 | **Not valid spend data:** 6,610,695 tokens attributable to echoed examples in three implement logs. |
| OpenRouter counted calls; usage available / unavailable | 101; 69 / 32 | Calls and tokens may include duplicate archive entries. |
| OpenRouter prompt / completion / total tokens | 13,441,535 / 792,148 / 78,080,587 | Reported, **not deduplicated**; total includes reported cache usage. |
| OpenRouter cache write / read tokens; `cache_hit_rate` | 2,908,942 / 60,943,081; **null** | Null is required by incomplete usage coverage; no hit-rate estimate. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 2,000 / 2,691,120 | Assembled telemetry, **118 samples**; not the full-run p95 above. |
| `break_glass_count` / `context_budget_warn_count` | 0 / 8 | Warnings comprise **five distinct full-log events** in three review runs. |
| GH API | One logged `used_in_job=506`; 24/25 inspected end rows `unknown` | No endpoint/call, retry, or cache-hit summary available. |

| MCP telemetry | Assembled reported totals | Verified distinct full-log evidence / limitation |
|---|---|---|
| Semble query / logged bytes / sources | **11 / 116,326 B / 53** | **6 / 76,213 B / 35** across inspected full logs; two additional counted queries totaling 1,174 B appear on summarized run `37866206379` without full logs here. The nine full-log collector query counts include three duplicates. |
| Semble targets | Target totals not separated in assembled aggregate | Full logs: `reviewer-context` **5 queries, 70,534 B, 30 sources**; `conflict-resolver-context` **1 query, 5,679 B, 5 sources**. Reviewer `static_dup_bytes=0` where logged; conflict event does not supply that field. |
| Semble bootstrap / failed / unused / time | **11 / 0 / 0 / 381,247 ms** | Six distinct full-log bootstraps total **210,401 ms**; three full-log bootstrap events are counted twice. |
| Semble fallback | **48:** 44 CI contract-test, four runtime | Full logs: **28 distinct** intentional `overflow` contract-test events and four runtime `overflow`/`binary-unavailable` events in `37856245085`. Query-to-fallback rate is not meaningful across these different contexts. |
| Serena query / response bytes / tool calls / fallbacks | **0 / 0 / 0 / 0** | No tool-level breakdown or replacement-rate estimate possible. |
| Serena probe ok / failed / skipped | **0 / 0 / 0** | No probe availability measurement was emitted. |

| MCP availability target | `probe_ok` | `probe_failed` | `probe_skipped` | Qualification |
|---|---:|---:|---:|---|
| Semble `reviewer-context` | — | — | — | Bootstrap/query observed; no per-target probe fields supplied. |
| Semble `conflict-resolver-context` | — | — | — | One distinct query; no probe fields supplied. |
| Semble `overflow` | — | — | — | Fallback target, not a measured probe. |
| Serena, target not emitted | 0 | 0 | 0 | Disabled in recent review summary `37866853445`; availability unknown. |

**Other MCP servers observed:** none in inspected emitted full-log lines. **Material collection gaps:** corrected invocation-level usage and event deduplication; endpoint-level GitHub calls and retries; reliable queue/concurrency timestamps; full logs for most non-deep-dive runs. These gaps bound the savings estimates above.
