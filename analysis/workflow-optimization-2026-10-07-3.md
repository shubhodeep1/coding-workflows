## Executive Summary

- **Queueing is the clearest measured latency loss.** In `shubhodeep1/coding-workflows`, integration-readiness run `37624388873` spent about 138 of 148 seconds waiting for a hosted runner; `cancel_on_pr_close` run `37623918885` waited about 510 of 645 seconds. Reducing *optional* workflow fan-out could recover tens to hundreds of seconds during contention, but the effect needs measurement. **Confidence: high on the waits; medium on the saving.**
- **Failures cluster at four boundaries:** check-failure triage (5 runs), the CI merged-PR guard test step (4), implementation (2), and review checkout (2). Add reason-coded, timed failure events at those boundaries before changing retry policy. The potential benefit is fewer failed cycles; a defensible reduction cannot yet be estimated. **Confidence: high on clustering; low on root causes.**
- **Review is the largest measured OpenRouter token driver:** five telemetry-bearing `review_autofix` runs account for 51,269,846 reported total tokens. One run, `37615213934`, accounts for 28,751,940. Record model, reasoning level, review tier, round, and per-call usage before changing quality-sensitive settings. **Estimated savings: unknown pending attribution. Confidence: high on measured totals.**
- **Semble’s 24 fallbacks are contract-test events, not observed runtime failures.** Separately, poller run `37624857038` reported Semble enabled but binary and index unavailable. Log availability by target so an unavailable rollout does not disappear behind a zero runtime-fallback count. **Estimated impact: faster detection, not a measured speed saving. Confidence: high.**
- **The evidence has a material collection gap.** The supplied summary reports log telemetry for 121/1,000 runs, and the specified full-log directory is absent in this environment. Findings below distinguish measured outcomes from unverified causes. Restoring failed-run logs is the highest-value diagnostic addition. **Confidence: high.**

## Speed Optimizations

1. **Reduce avoidable runner demand — critical-path opportunity.** Runs `37625197758` and `37624388873` waited about 92 and 138 seconds in `Integration PR readiness check`; run `37623918885` waited about 510 seconds in `heal-pr-reconcile`. The window also contains 763 skipped runs, including near-simultaneous skipped clarify, plan, implement, and orchestration runs around `37625485051`, `37625484977`, and `37625485365`. **Inference:** optional dispatch fan-out may contribute to contention; the logs do not measure its share. Emit `dispatch_decision` with eligibility reason and event key, then suppress only demonstrably redundant *optional* dispatches upstream. **Saving:** potentially 15–510 seconds on affected queue-bound runs, not guaranteed. **Risk: medium.** Do **not** skip the integration-readiness job: its repository workflow intentionally posts a status even for non-orchestrator PRs so the required context exists.

2. **Classify expensive failures sooner — critical-path opportunity.** Review runs `37615186690` and `37616298146` failed at `review / codex-agent → Checkout PR head branch` after 2,909 and 1,868 seconds of run time. The step already retries `git fetch`; total run time is **not** evidence that fetch itself took that long. Emit a bounded `review_checkout` event with entry time, fetch attempts and elapsed time, exit category (`fetch`, missing workspace, invalid ref, stale head, checkout), and job queue time. Apply an earlier gate only to a category proven permanent. **Saving:** unknown until step timing exists; **risk: low** for logging, higher for any new gate.

3. **Measure review and implementation compute separately from waiting — critical-path opportunity.** Successful review `37615213934` lasted 2,506 seconds; successful implement `37621495660` lasted 1,985 seconds. Emit per-round/per-attempt start, end, model, tool-call count, changed-file outcome, and idle/wall-time classification without transcript contents. Use that breakdown to target repeated no-change attempts rather than shortening all runs. **Saving:** not measurable from run durations alone; **risk: low** for instrumentation.

4. **Keep context trimming a measured micro-optimization.** Implement recorded 46 Semble queries and 303,060 logged bytes; review recorded 10 and 98,024 bytes. Record query target, elapsed milliseconds, selected chunks, and bytes actually inserted into the prompt. Trim consistently unused chunks only after relevance checks. **Saving:** unknown; **risk: medium** if useful context is removed.

## Cost Optimizations

1. **Attribute review usage before changing models.** `review_autofix` reports 76 OpenRouter calls, 51,269,846 total tokens, 40,807,016 cache-read tokens, and 2,592,092 cache-write tokens. Run `37615213934` alone reports 32 calls and 28,751,940 total tokens, with usage unavailable on 10 calls. The repository already has review tiers and a smaller-diff second-pass reasoning setting; the telemetry does not establish which tier or model caused this outlier. Emit per-call `model`, reasoning level, tier, pass, round, usage-available flag, and outcome. Trial a cheaper setting only for a measured low-risk tier, retaining the existing fallback and quality checks. **Estimated dollar/token saving: unknown without model prices and comparable tier outcomes. Quality risk: material if reviewer depth is reduced indiscriminately.**

2. **Investigate failed implementation spend.** Failed runs `37618246580` and `37620975934` each report 22 Codex token-count matches and 2,644,278 tokens—5,288,556 reported tokens together—at `Run Codex implementation`. Log each internal attempt’s terminal reason and whether it changed files; stop only on a verified permanent verdict, preserving transient retries. The identical totals also warrant reconciliation against individual usage records, **not** an assumption of duplicate billing. **Potential saving:** up to one affected run’s reported 2,644,278 tokens *if* a future full-work rerun is shown avoidable; no such rerun is established here. **Quality risk: low for logging, medium for early termination.**

3. **Test Semble’s net context value rather than treating queries as savings.** Across implement and review there were 56 `SEMBLE_QUERY` events and 401,084 logged output bytes, about 7,162 bytes/query. Run `37620975934` contributed 8 queries/64,340 bytes. The input does not show query targets or what material those bytes displaced, so neither prompt-expansion savings nor noisy low-value context can be established. Log target, bytes returned versus inserted, and downstream read/tool calls; cap by measured usefulness. **Estimated saving: unknown. Quality risk: avoid trimming relevant results.**

4. **Do not assume Serena is an alternative yet.** Serena has zero reported queries, tool calls, response bytes, fallbacks, and probes; `implement.yml` and `review_autofix.yml` default `SERENA_ENABLED` to false. There is no evidence that Serena replaced model/tool work—or added noisy bytes—in this window. Keep model selection and reasoning-level changes behind usage and outcome comparisons. **Estimated saving: none established.**

## Reliability Improvements

1. **Diagnose the CI guard-test cluster without blaming Semble.** All four CI failures reached `tests-hooks-and-orchestrator → Merged-PR commit guard hook tests`; examples `37614235364`, `37615186167`, `37621516620`, and `37622361139` lasted 687–892 seconds. Their 24 `SEMBLE_FALLBACK` events are all classified `context=contract-test`; zero are classified runtime fallbacks. Preserve the failing guard. Collect failed test names, assertion category, and a bounded traceback as a structured test artifact, then fix the demonstrated assertion or fixture. **Expected impact:** addresses a cluster affecting 4/11 CI runs if one cause is confirmed. **Rollback:** remove diagnostic output independently; do not make the guard fail-open.

2. **Instrument both triage failure stages.** `check_failure_triage` has 5 failures in 13 runs and no successes: collect failed in `37614754749`, `37615539647`, and `37624187656`; issue posting failed in `37616500676` and `37623261808`. The script fetches PR/parent metadata and lists open triage issues; the posting step uses a separate token. **Root-cause category remains unknown without logs.** Emit stage, sanitized API operation, HTTP/error class, retry count, elapsed time, and whether an issue was created or deduplicated—never token or issue-body contents. Keep existing self-loop and lineage protections; retry only classified transient failures. **Expected impact:** a diagnosable path to reducing 5 affected runs; **rollback:** instrumentation-only initially.

3. **Separate checkout failures from stale-head safety skips.** The two failed review runs ended at checkout, while the checkout code also has deliberate stale-head and unwritable-head exits. Emit distinct outcomes and fetch-attempt timing so a transient transport failure cannot be mistaken for a policy skip. Retain stale-head fail-closed behavior; adjust only a proven transient fetch path. **Expected impact:** fewer unnecessary failed review cycles if transport is confirmed; rate unknown.

4. **Expose availability separately from fallback.** CI’s 24 Semble fallbacks are simulated; the supplied telemetry records zero runtime Semble fallbacks. Yet poller run `37624857038` reported `SEMBLE_ENABLED=true`, `SEMBLE_AVAILABLE=false`, and `SEMBLE_INDEX_AVAILABLE=false` without a counted runtime fallback. Emit a per-target availability result and reason even when no query is attempted. Treat rare query fail-open as healthy, but alert on repeated unavailable targets in an enabled rollout. Serena has zero probes, so neither probe failure nor availability can be inferred. `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero **in parsed coverage**, not proof of no policy or prompt pressure across all runs.

## AI Memory Health

No deep-dive excerpts were accessible at the stated logs path. One supplied `log_summary` does preserve a positive event: `issue_pr_status` run `37623918910` reports `AI_MEMORY_TELEMETRY` `finalize-task`, `ok=true`, `did_push=true`, `final_state=merged` for issue `6210`.

No `retrieve` entries are available here. **Retrieve hit rate (`records_selected > 0`), mean `estimated_tokens` versus `token_budget`, and `keyword_method` distribution (`llm`/`plain`/`none`) are undefined—not zero.** Zero-record retrievals, `fail_open=true`, `enabled=false`, and push retries likewise cannot be counted. Restore the failed/slow logs and aggregate the existing `AI_MEMORY_TELEMETRY` JSON by operation, workflow, and run; emit a coverage count when a memory-enabled step produces no telemetry. This would distinguish ineffective retrieval from absent collection without changing memory behavior.

## GH API Call Audit

**No observed per-job API call counts, endpoint histogram, rate-limit events, or inner retry counts were supplied.** A high-volume claim or numerical call reduction would be speculative.

| Auditable code path | Specific low-risk action | Call impact to test |
|---|---|---|
| `check_failure_triage.sh`, collect stage; three failures above | Count PR GET, conditional parent-issue GET, paginated open-triage listing, and check-run listing separately; attach sanitized error class and elapsed time. Reuse already-fetched PR fields within the stage. | Baseline and reduction unknown; diagnose failures without adding lookups. |
| `review_merge_train.sh`, review release | Its documented active-run check queries five statuses twice, normally 10 listing calls before follow-up pages. Emit calls/pages per status and pass, plus incomplete-listing reason; preserve the second pass until tests establish that reuse is safe. | **Code-path budget**, not an observed count or proven redundancy. |
| `implement.yml`, issue metadata steps | Record when repeated issue reads use an immutable cached field versus require fresh labels/state. Reuse only demonstrably unchanged data. | Conditional reduction; no measured duplicate-call count. |

Apply `CLAUDE.md` §15: extend an existing response or cycle-local cache before adding a lookup, batch genuine per-item reads, and use the smallest safe legacy call on cache miss. Add a sanitized `GH_API_CALL` rollup by workflow/job/step and endpoint *template*, with attempts, latency, response class, and rate-limit wait. The repository already emits `GH_PAT_BUDGET` in some jobs; bring its start/end values into this report rather than interpreting absent API data as zero. Never log credentials, request bodies, or full URLs containing parameters.

## Prompt Cache & Memory System

OpenRouter reports 40,807,016 cache-read and 2,592,092 cache-write tokens, but aggregate `cache_hit_rate` is **null** because usage is unavailable on 18/76 calls. The one supplied valid run-level rate is **71.0896%** for review run `37619963052` (16/16 calls with usage). It cannot be generalized to the other runs. Cache-read tokens are usage, not tokens that can simply be removed from a cost total.

`implement.yml` already puts a static prefix before targeted-file, memory, and implementation context. Preserve that ordering. Log a non-sensitive static-prefix fingerprint and section byte counts per attempt, alongside breakpoint enabled/fallback and provider usage availability; compare misses across the *same* model and phase before blaming dynamic noise or changing prompt order. Its existing stdin-cap warning should be paired with section sizes. With zero collected `CONTEXT_BUDGET_WARN` events and incomplete coverage, prompt-size risk is unquantified. This addition can expose token and latency opportunities while keeping cache fail-open behavior; expected savings remain unknown until coverage improves. Memory-retrieval effectiveness is likewise unmeasured, as detailed above.

## Orchestrator Health

`orchestrate_poll` completed 5/5 runs, with p50 315 seconds; run `37624857038` succeeded in 279 seconds despite Semble being unavailable. `orchestrate_clarify_respond` shows 12 successes and 160 skips across 172 runs; `workflow_failure_heal` shows 92 skips across 92 runs. **Inference:** much of this is intentional gating, but the supplied rows cannot distinguish healthy no-ops from repeated deferrals. The one observed `finalize-task` event in run `37623918910` is a positive completion signal, not a throughput rate.

Emit one bounded transition record per project tick: current/next phase, wave, decision reason (`ineligible`, waiting, deferred, conflict-heal retry, terminal), age in state, and linked run ID. Track skipped dispatches, clarification-loop count, waves advanced per poll, conflict-heal attempts, and time since last state change. Do not infer stuck projects or judge-cycle counts from workflow names alone; no linked state transitions or verdicts were supplied.

## Pipeline Flow Bottlenecks

| Flow component | Evidence | Next diagnostic or safe fix |
|---|---|---|
| **Queueing** | Readiness run `37624388873`: ~138/148 seconds waiting; PR-close run `37623918885`: ~510/645 seconds waiting. | Emit created→job-start wait per job; reduce verified optional dispatches while retaining required status checks. |
| **Clarify → plan** | Clarify: 190 runs, p50 1 second, p95 432 seconds; plan: 171 runs, p50 1 second, p95 811 seconds. Both contain many skips. | Log eligibility and phase-transition IDs; do not interpret their skip-dominated medians as work time. |
| **Implement compute/retry** | Runs `37618246580` and `37620975934` failed at implementation with 22 token-count matches each. | Log internal attempt outcomes and changed-file state; distinguish internal attempts from GitHub run retries. |
| **Review/autofix** | 102 runs, p50 524.5 seconds, p95 1,566.2 seconds; run `37615213934` took 2,506 seconds. | Attribute queue, reviewer passes, editor rounds, checkout, and merge/conflict time separately. |
| **Validation/CI** | CI: 11 runs, p50 1,063 seconds, p95 1,412.5 seconds; four fail at the guard-test step. | Capture failed test cases and step timing. No separate `validate` workflow family appears in the supplied window. |

The data does not link issue→PR→run transitions, so it cannot produce an end-to-end critical-path percentile or split review time into compute, retry, and merge/conflict overhead. Add a shared transition correlation key and the timing fields above before ranking those subcomponents.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** observed hosted-runner waits up to ~510 seconds (`37623918885`); review p95 1,566.2 seconds and CI p95 1,412.5 seconds. **Top failure modes:** five triage failures, four CI guard-test failures, two review-checkout failures, two implement failures. **Highest measured costs:** review’s 51,269,846 OpenRouter total tokens and implementation’s 18,530,206 reported Codex tokens; these are different usage measures and should not be added as a dollar estimate.
- **Prioritized actions:** **(1)** restore full failed/slow-run logs and emit reason-coded step timing; **(2)** measure optional dispatches and queue time before suppressing verified redundant work, preserving required checks; **(3)** attribute review model/cache usage and implement attempt outcomes before changing reasoning or retries. These actions preserve existing safety gates while targeting the largest observed losses.

## Metrics Appendix

*Scope:* supplied GitHub Actions API analysis for one repository, with `insufficient_data=false`. The specified `/home/runner/work/_temp/workflow-log-output/summary.json` and its `errors/`, `slow/`, and `recent/` directories were absent here. Run-specific claims therefore use the supplied rows and `log_summary` fields; failure causes, per-target MCP lines, and API hotspots could not be checked against full logs. `sampled_success_runs=0` is also inconsistent with success rows carrying cost telemetry; audit that collector field. Durations below are **run** durations, not step durations.

| Repository / family | Runs | Success | Failure | Other, predominantly skipped | Success / failure of all runs | p50 / p95 run duration |
|---|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows`, all | 1,000 | 224 | 13 | 763 | 22.4% / 1.3% | 2 / 856 s |
| `review_autofix` | 102 | 100 | 2 | 0 | 98.0% / 2.0% | 524.5 / 1,566.2 s |
| `implement` | 173 | 12 | 2 | 159 | 6.9% / 1.2% | 1 / 1,446.6 s |
| `ci` | 11 | 7 | 4 | 0 | 63.6% / 36.4% | 1,063 / 1,412.5 s |
| `check_failure_triage` | 13 | 0 | 5 | 8 | 0% / 38.5% | 9 / 444.2 s |
| `clarify` / `plan` | 190 / 171 | 15 / 15 | 0 / 0 | 175 / 156 | — | 1 / 432.2 s; 1 / 811 s |
| `orchestrate_clarify_respond` / `orchestrate_poll` | 172 / 5 | 12 / 5 | 0 / 0 | 160 / 0 | — | 1 / 681.35 s; 315 / 339 s |

Across **concluded success/failure** runs only, success is 224/237 (94.5%) and failure is 13/237 (5.5%); the all-run rates above include skips in the denominator.

| Parsed-cost and review metric | Supplied value | Coverage qualification |
|---|---:|---|
| Runs with log telemetry | 121/1,000 (12.1%) | Full-log archive unavailable here |
| Codex tokens / count matches | 18,536,284 / 167 | Implementation: 18,530,206 / 164 |
| OpenRouter total / prompt / completion tokens | 51,269,846 / 7,539,574 / 331,644 | 76 calls; 58 usage-available, 18 unavailable |
| OpenRouter cache read / creation tokens | 40,807,016 / 2,592,092 | Aggregate `cache_hit_rate=null`; run `37619963052`: 71.0896% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 2,000 / 2,425,250 ms | 120 sampled run durations; p50 is skip-dominated |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | Parsed coverage only |
| GH API calls / rate-limit events / inner retries | **Not supplied** | `GH_PAT_BUDGET` values not present in the assembled metrics |

| MCP metric | Supplied value | Interpretation |
|---|---:|---|
| Semble queries / logged bytes | 56 / 401,084 | Implement 46 / 303,060; review 10 / 98,024; query targets unavailable |
| Semble fallbacks | 24 | All 24 CI `contract-test`; **0 logged runtime** fallbacks |
| Serena query rollups / underlying tool calls / response bytes / query ms | 0 / 0 / 0 / 0 | Per-tool breakdown: none observed |
| Serena fallbacks; probes ok / failed / skipped | 0; 0 / 0 / 0 | No availability conclusion without probes |

| MCP availability target evidenced in supplied data | `probe_ok` | `probe_failed` | `probe_skipped` | Other availability signal |
|---|---:|---:|---:|---|
| Semble, `orchestrate_poll` run `37624857038` | Not emitted | Not emitted | Not emitted | Enabled; binary and index reported unavailable |
| Semble, CI contract-test targets not supplied | Not emitted | Not emitted | Not emitted | 24 simulated fallbacks across four failed CI runs |
| Serena, target not observed | 0 | 0 | 0 | No query, fallback, or probe lines reported |

**Other MCP servers observed:** none in the supplied aggregates or summaries; raw logs are required to verify that none were missed.
