## Executive Summary

- **CI is the dominant reliability and latency problem.** In `shubhodeep1/coding-workflows`, 12 of 32 CI runs were cancelled near the 45-minute job limit; ten downloaded logs reach the same `lint/Orchestrate poll process unit tests` step. **Estimated impact:** recovering roughly two minutes of headroom could prevent some timeouts; **confidence: high** in the pattern, medium in the prevention estimate.
- **The late CI step has a specific, measurable cost.** In run `36369772622`, four poll-test shards each passed 109 tests, then `test_state_snapshot.py` passed; the step was cancelled about 104 seconds later. The next test file contains a deliberate 120-second timeout test. Run that independent test concurrently with the shards, while still requiring its result. **Estimated saving:** approximately 120 seconds on long CI runs; **confidence: medium**, pending a timed trial.
- **Three CI failures are actionable test or prompt defects, not MCP outages.** Runs `36365736479` and `36373055709` exceeded the 250-line DEFAULT prompt limit by 16 and 17 lines; run `36363171261` invoked a test requiring an unavailable `tmp_path` argument. Fix those defects and move the cheap budget check earlier. **Estimated impact:** prevent those three observed failure types and detect prompt violations roughly 19 minutes sooner; **confidence: high**.
- **Cost and API savings cannot yet be priced.** The assembled context has telemetry for 115 of 1,000 runs, but the supplied folder has full logs for only 26; neither set records a model-usage call or a measured GH API call count. Zero recorded tokens is **not** evidence of zero pipeline spend. Add per-call usage and API counters before changing models or cache policy. **Estimated impact:** measurable decisions rather than speculative savings; **confidence: high**.
- **MCP alarms need test-context separation.** All 70 collected `SEMBLE_FALLBACK` events are CI contract-test events; the sole `SEMBLE_QUERY` logged 11 bytes in run `36363171261`. No production fallback or Serena probe was observed. **Estimated impact:** fewer false rollout alarms; **confidence: high** for collected logs, low for uncollected runtime runs.

## Speed Optimizations

1. **Critical path — overlap the ledger timeout contract with poll shards.** CI run `36369772622` spent about 1,417 seconds before `lint/Orchestrate poll process unit tests` and another 1,303 seconds in it. Its four 109-test shards finished before the final tests; `tests/test_run_substate_ledger.py` deliberately waits for the default 120-second bound. Start that test alongside the independent shards in `.github/workflows/ci.yml`, capture its exit status, and fail the existing step if it fails. Emit `CI_SUBTEST_START/END` with subtest name, elapsed milliseconds and exit status; emit a heartbeat during the wait. **Estimated saving:** about 120 seconds per affected completed CI run, subject to contention; **risk: medium**—benchmark and retain fail-closed result collection. Do not simply increase the 45-minute limit.
2. **Failed-path win — run the prompt-budget gate early.** `lint/Prompt tier budget` failed after 1,122 seconds in `36365736479` and 1,143 seconds in `36373055709`, on prompts of 266 and 267 lines against a 250-line limit. Move the unchanged `tests/prompt_size_budget.py` gate immediately after checkout/setup. **Estimated saving:** approximately 18–19 minutes *when a prompt is invalid*, not on green runs; **risk: low**. Log `PROMPT_BUDGET_CHECK file tier lines limit result`, without prompt contents.
3. **Do not optimize short no-ops ahead of CI.** Integration-readiness runs `36374807199` and `36374799926` finished in 10 and 8 seconds when their branches were not orchestrator branches; the required status still has to be posted. Review sweep `36374882766` spent about 18 of 25 seconds in `sweep`. First add step and API timing there; **time saving: unquantified**, **risk: low** for logging. Removing required-check or trust checks to save seconds is not justified.

## Cost Optimizations

1. **Avoid timeout-consumed CI capacity first.** The 12 CI cancellations lasted about 45 minutes each—roughly **9 cumulative run-hours**, not a measured billing figure. Overlapping the ledger contract and measuring each CI subtest may recover that capacity if it removes the timeouts; measure the cancellation rate before claiming savings. No model-quality risk; implementation contention is the principal risk.
2. **Remove prompt duplication without weakening the gate.** The two `mode-judge-review-blocked.txt` failures were line-count violations, **not** observed `CONTEXT_BUDGET_WARN` events. Trim at least 17 lines while retaining required rubric behavior and its tests; do not relabel the prompt LARGE merely to pass CI. Token and dollar savings are unknown because no actual call usage was collected; quality risk requires contract-test review.
3. **Defer model and MCP cost changes until calls are measured.** Recent review configuration names a summariser and consolidator model, but collected `codex_calls` and `or_calls` are both zero. Record model, phase, reasoning level, prompt/completion/cache tokens, usage availability and elapsed time for each executed call before testing cheaper choices. The one Semble query—`target=overflow`, `bytes=11`, `context=contract-test`—cannot demonstrate reduced prompt expansion. Serena has zero queries and response bytes and is shown disabled in recent review runs, so there is no evidence that it replaced downstream tool/model work or added noisy responses. **Savings: not estimable; quality risk:** changing selection now would be unmeasured.
4. **Measure avoidable dispatches.** Review sweep `36374882766` saw 14 candidates, dispatched 9 and skipped 5 active runs; five recent review-gate runs, including `36374907305` and `36374897791`, skipped with `reason=claude_fixer_awaiting_session`. Emit a terminal gate/dispatch summary per run, including whether model work began. This will distinguish inexpensive protective no-ops from costly reruns; no token saving is established yet.

## Reliability Improvements

1. **CI timeout pressure — compute/test-layout category.** CI had 17 successes, 12 cancellations and 3 failures out of 32 runs. All ten downloaded cancelled CI logs show four passing poll shards followed by a short post-shard test and then cancellation; nine end before the ledger test prints its summary, while `36363178320` prints its passing summary just before cancellation. Keep the 120-second production safety bound, overlap its independent contract test, and log each post-shard subtest start, heartbeat and end. **Expected impact:** fewer 45-minute cancellations if the recovered headroom suffices. **Rollback:** restore sequential ordering if a concurrent-run trial reveals interference; never ignore either test result.
2. **Deterministic gate failures — prompt/test-contract category.** Fix the 266/267-line DEFAULT prompt violations in runs `36365736479`/`36373055709`; fix the direct-run test signature or runner mismatch reported as missing `tmp_path` in `36363171261`. Run both checks locally and early in CI. **Expected impact:** eliminate these three observed failure causes; **rollback/fail-open:** retain both hard gates rather than suppressing failures. The `##[error]` lines about auto-close keywords and integration fingerprints inside these CI logs are expected test assertions, not the terminal failures.
3. **Successful run with missing validation dispatch — dispatch-observability category.** Review run `36374592078` succeeded but warned that no standalone validation workflow could be dispatched for merged PR `#4633`. Log attempted workflow, eligibility decision, response category and whether an alternative validation run was found; alert on an unvalidated merge after a bounded check. **Expected impact:** detect validation gaps, not a quantified failure-rate reduction. Preserve current merge behavior until dispatch eligibility is verified; do not silently turn the warning into success evidence.
4. **Classify cancellations separately from failures.** Review/autofix has six cancellations among 225 runs. Run `36367430257` was cancelled while the active step was `Record review run completion in memory (fail-open)`; that does **not** prove memory caused cancellation. Add cancellation initiator/reason, concurrency group, active step and step elapsed time to the diagnostic summary. **Expected impact:** distinguish supersession from a wedged step; logging is rollback-safe.

The 70 Semble fallbacks are `target=overflow`, `context=contract-test` (including deliberately missing executables); zero collected runtime fallbacks do not establish production availability. No `SERENA_PROBE` failure or runtime fallback was observed, and recent review logs say Serena is disabled. Keep fail-open behavior and report contract-test and runtime events separately. Collected `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are zero; there is no observed policy/rubric-pressure or token-window incident, but coverage is insufficient to declare those risks absent.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` operation appears in the downloaded deep-dive logs. Consequently **retrieve hit rate, average `estimated_tokens` versus budget, and `keyword_method` distribution (`llm`/`plain`/`none`) are N/A, not 0%**. No deep-dive `retrieve` with zero records, `fail_open: true`, `enabled: false`, or push-retry count can be assessed.

One unselected-run `log_summary` provides positive but narrow evidence: `issue_pr_status` run `36374592082` reported successful `ingest_implement_plan_lessons` for PR `#4633`, with 19 parsed and 2 written, `did_push: true`; it reported no retry count. Verify telemetry emission in an **executed** clarify/plan/review memory-retrieval run. Log only operation, enabled/fail-open state, `records_selected`, `estimated_tokens`, budget, keyword method, push attempts and elapsed time—never retrieved text. Expected impact is diagnosing misses and retry stalls, not a presently measurable hit-rate gain.

## GH API Call Audit

**Measured API calls, endpoint hotspots, rate-limit events and retry counts are unavailable** in this window. No runtime rate-limit event was identified in the downloaded logs; CI test text is not a production API event. Add a per-run, per-step `GH_API_CALL` counter at existing CLI/helper boundaries with endpoint *template*, attempt count, status category, rate-limit category, elapsed milliseconds and cache hit; redact query values and responses.

- **Review sweep: preserve existing batching.** `.github/workflows/review_autofix_sweep.yml` fetches open PRs once and snapshots `queued`, `in_progress` and `pending` for two workflows before its per-PR loop—six configured status-query invocations before pagination. In run `36374882766`, that loop considered 14 candidates and skipped five active runs. Log pages fetched and snapshot age; do **not** replace the snapshot with per-PR lookups. An N-per-item redesign would increase calls; actual request counts and rate-limit reduction cannot be calculated without pagination telemetry.
- **Claude catch-all: investigate candidate fanout.** Its documented contract in `scripts/claude_pr_sweep.py` includes a PR read, comment/check pages and three active-run reads *per `claude/*` candidate*. The `claude-pr-catch-all` job took about 17 of 24 seconds in `36374565034`, but its candidate and call counts were not supplied. Reuse fields from its already-fetched PR-list snapshot where safe, retaining a fresh head/claim check before writes, and prefetch cycle-stable active-run data. Potential reduction is up to one redundant PR read per unchanged candidate; **N is unknown**, so this is a testable ceiling, not observed savings.
- Apply `CLAUDE.md §15`: extend existing same-scope calls, batch per-item reads, use cycle-local caches, and fall back to the smallest safe legacy read on cache miss. Instrument first so a cache miss cannot silently become an unbounded retry loop.

## Prompt Cache & Memory System

`cache_hit_rate` is **null**: there are no collected OpenRouter usage calls from which to form its denominator. Logged cache-create and cache-read tokens are both zero for the sampled logs, not measured misses across the pipeline. Preserve usage-availability reporting; record cache read/write tokens and a stable, non-sensitive prefix fingerprint for executed calls. **Inference to test:** placing run-specific diff, timestamps, memory text or Semble prefetch ahead of reusable instructions could fragment the prefix cache. Keep stable instructions first and dynamic material later *if call-level measurements confirm this ordering is relevant*; report hit rate by phase/model before estimating token or latency gains.

`CONTEXT_BUDGET_WARN: phase=...` was not emitted in collected runs. The two 250-line CI prompt-budget failures are a different signal and should not be counted as model-window warnings. Emit both prompt line-budget and rendered-token/window measurements; correlate subsequent memory retrieval selections and cache rates. This provides an early warning of prompt growth without relaxing either gate or exposing prompt content.

## Orchestrator Health

Clarify had 13 successes and 127 skipped runs; plan had 10 successes and 134 skipped; implement and orchestrate-clarify-respond had 135/135 and 134/134 skipped, respectively. These are wrapper outcomes, **not proof of stuck tasks**. The eight observed orchestrate-poll runs all succeeded, with p50 353 seconds, but none has a downloaded deep-dive log here; wave progression, judge cycles, deferrals and conflict-heal retries therefore cannot be counted.

There is a concrete hand-off signal: review-gate runs `36374907305` (PR `#4546`) and `36374897791` (PR `#4610`), among five recent examples, returned `claude_fixer_awaiting_session`. Log the age of that state, claim/queue transition and final disposition; compare with watchdog run `36374651956`, which checked one open item and found zero newly stale at a three-hour threshold. For each poll tick, emit bounded counts for wave/state, deferred issues and reasons, judge invocation, conflict attempt, terminal state and time since last progress. These indicators will separate healthy guarded skips from a stalled hand-off without changing dispatch policy.

## Pipeline Flow Bottlenecks

| Segment | Evidence and bottleneck | Next diagnostic/action |
|---|---|---|
| Clarify → plan → implement | `clarify` p50 1s and `plan` p50 1s include mostly skips; successful clarify run `36374817892` took 154s. | Log gate result and actual work start/end before interpreting phase latency. |
| Review/autofix | 225 runs, p50 13s but p95 about 1,301s; `36374907305` skipped awaiting a Claude session, while successful `36367994745` lasted 2,604s. | Separate gate wait, model compute, external-session wait and conflict-heal time; do not treat every success as completed autofix. |
| CI/validation | CI p50 2,681s, p95 2,720s; run `36369772622` completed four 109-test shards before consuming its remaining timeout budget. `validation_refresh` run `36371152041` lasted 1,069s (one observation). | Overlap the independent ledger contract; emit post-shard subtest timing and validation-dispatch outcome. |
| Orchestrate/merge | Poll p50 353s across eight runs; forward-merge `36374922206` took 60s and was a no-op. | Log per-tick compute/API/decision time and merge/conflict attempts before attributing these durations to conflicts. |

Queue time and retry overhead cannot be separated reliably from the supplied timestamps and counters: reported `retries=0` describes run attempts, not internal API or conflict retries. Emit job queued/start/end timestamps plus `cancel_reason`, and separate `queue_ms`, `compute_ms`, `retry_wait_ms` and `merge_conflict_ms` in the existing run summary. No new service is needed.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** CI’s near-45-minute tail (32 runs; 12 cancelled), followed by long-tail review/autofix (p95 about 1,301s). **Top failure modes:** two DEFAULT prompt-budget violations and one direct-run test fixture mismatch; a successful review run also warned of missing validation dispatch. **Highest-cost driver supported by evidence:** approximately nine cumulative hours occupied by subsequently cancelled CI runs. Model spend and GH API volume are unmeasured.

**Prioritized actions:** (1) overlap the independent 120-second ledger test with poll shards, keeping both results mandatory and logging each subtest; (2) repair the three deterministic CI defects and move the prompt budget gate early; (3) add executed-call model/cache/API and orchestrator hand-off summaries before tuning models, batching further or changing retry policy.

## Metrics Appendix

*Window:* collected through September 28, 2026, 03:49 UTC. The assembled `analysis_context.json` covers 1,000 API run rows and reports telemetry on 115; the supplied `summary.json` folder downloaded 26 full log archives. Its 26-log wall-clock median is **591,000 ms**, whereas the wider 113-sample assembled figure below is **8,000 ms**. Neither is a model-call latency percentile.

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 421 (42.1%) | 3 (0.3%) | 19 (1.9%) | 557 (55.7%; skipped) | 7s / 1,158s |
| CI | 32 | 17 | 3 | 12 | 0 | 2,681s / 2,720s |
| Review/autofix | 225 | 218 | 0 | 6 | 1 | 13s / 1,300.8s |
| Clarify / plan / implement | 140 / 144 / 135 | 13 / 10 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 127 / 134 / 135 | 1s / 150.05s; 1s / 58.1s; 1s / 10s |
| Orchestrate poll | 8 | 8 | 0 | 0 | 0 | 353s / 372.6s |

| Collected signal | Assembled value | Interpretation |
|---|---:|---|
| Log-telemetry runs / wall-clock samples | 115 / 113 | Selected and summarized coverage, not all 1,000 runs |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 2,720,000 | Sampled run-duration proxy |
| Codex calls / tokens; OpenRouter calls / prompt / completion / total tokens | 0 / 0; 0 / 0 / 0 / 0 | No usage observed in collected calls; total spend unknown |
| OpenRouter cache write / read tokens; `cache_hit_rate` | 0 / 0; **N/A** | No usage denominator |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | Collected logs only |
| GH API calls / retry counts / rate-limit events | **N/A / N/A / not observed in downloaded logs** | No request-level counter or complete runtime-log coverage |
| AI memory retrieves / hit rate / average tokens versus budget | 0 observed / **N/A / N/A** | No deep-dive retrieve operation; one separate ingestion summary |

| MCP scope and target | Queries | Logged query bytes | Fallbacks | Runtime fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|---:|---:|
| Semble, CI `target=overflow` | 1 | 11 | 70, all `context=contract-test` | 0 observed | 0 / 0 / 0; no Semble availability probe observed |
| Serena, no observed target | 0 | 0 response bytes; 0 tool calls; 0 query ms | 0 | 0 | 0 / 0 / 0; recent review configuration disabled |
| Other MCP servers observed | None | — | — | — | — |

**MCP rates and tool breakdown:** a production Semble fallback rate is N/A—the query and fallbacks are independent contract-test events, not runtime successes/failures. Serena per-tool and per-target response-byte breakdowns are empty, not evidence of efficiency. No unknown MCP telemetry prefix was observed in the downloaded logs.
