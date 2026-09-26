## Executive Summary

- **CI is the dominant reliability problem.** In `shubhodeep1/coding-workflows`, 39 of 47 CI runs failed; 35 failed at `lint / Inventory parity`. Deep-dive logs for runs `36219304455`, `36220264510`, and `36220639422` repeat missing inventory entries for one workflow and three scripts. Update `docs/INVENTORY.md` and run parity before expensive tests. **Impact:** remove this confirmed failure signature and potentially save roughly 15–20 minutes on each affected failing run. **Confidence: high** for the sampled mismatch; **medium** for savings across all 35 runs.
- **Review’s resolver has a recurring fail-closed scope error.** Seven review/autofix failures, including `36221714214` and `36224174449`, end with `Resolver scope check failed closed (ValueError)`. Four inspected resolver logs show model-issued `git add` before that check. Preserve the safety gate, but log a safe reason code and defer staging to a trusted step. **Impact:** faster diagnosis and potentially fewer 475–705-second failed runs. **Confidence: high** for the pattern; **medium** that staging causes every instance.
- **A successful workflow can still lose the intended fix.** Review run `36221120963` took 2,405 seconds and logged `AUTOFIX_FAILURE_REASON: editor_changes_lost` with redispatch skipped for unavailable or exhausted budget. Add a separate logical-outcome signal; check for retained changes before further expensive work. **Impact:** prevent silent hand-back and avoid comparable wasted review cycles. **Confidence: high** for this run.
- **AI cost is concentrated, but coverage is narrow.** The enriched context reports 44,400,792 tokens across 45 usage-recorded calls and an 83.63% cache-hit rate; raw logs cover only 29 of 1,000 runs. Instrument phase-level usage before changing model tiers or reasoning. **Impact:** identify savings without weakening review quality. **Confidence: high** for measured totals, **low** for extrapolation.
- **API and queue bottlenecks cannot yet be ranked.** The supplied logs lack endpoint-level GitHub API call counts and job queue timestamps. Add aggregate diagnostics to existing helpers rather than new API calls. **Impact:** make batching and wait-time recommendations measurable. **Confidence: high** in the data gap.

## Speed Optimizations

1. **Critical path — fail CI parity early.** Run `tests/inventory_parity.py` immediately after checkout/setup, before the long `lint` test sequence in `.github/workflows/ci.yml`. Parity failed in approximately 0.2–0.4 seconds when reached in runs `36220639422` and `36219304455`, but those runs lasted 968 and 1,169 seconds. **Root cause:** a cheap deterministic check runs late. **Estimated saving:** approximately 15–20 minutes per run that would fail parity; implementation risk **low**, provided the full test suite remains unchanged for passing runs. Emit a machine-readable parity summary with missing-entry counts and elapsed time.
2. **Critical path — diagnose resolver scope failures before another cycle.** Review runs `36220574416`, `36221714214`, and `36222494360` failed after 475, 540, and 705 seconds. Inference: staging inside the model turn may change the index that `scripts/review_conflict_resolve.sh` snapshots. Log the scope-check reason code and attempt number; test a resolver-only instruction/tool boundary that leaves staging to the trusted post-check step. **Estimated saving:** up to the failed resolver run’s remaining cycle on prevented recurrences, not a guaranteed per-run speedup. Risk **medium**; never bypass the fail-closed check.
3. **Measure before retuning CI shards.** Successful CI run `36210775910` reached four-shard orchestrate-poll tests around 02:31:55 UTC and emitted shard output around 02:51:01 UTC. Log each shard’s start, end, test count, and slowest test, then rebalance only if skew is demonstrated. **Estimated saving:** unquantifiable until measured; risk **low** for logging, **medium** for changing allocation. Optimizing subsecond parity execution itself would be a micro-optimization, not a priority.

## Cost Optimizations

1. **Prevent expensive work with no retained outcome.** Review run `36221120963` used 26 calls and 29,398,890 reported tokens yet ended with `editor_changes_lost`. Make the trusted change-retention check and budget decision observable before any redispatch or further model invocation. **Potential saving:** up to a comparable run’s usage when a repeat can be safely avoided; **not** a measured recurring saving. Quality risk **low** if validation and human hand-back remain intact.
2. **Attribute usage before changing models or reasoning.** That review configured `openai/gpt-6-sol` for consolidation; run `36224183739` configured `openai/gpt-6-luna` for summarization. Resolver logs show high reasoning. Log calls, tokens, cache reads, duration, and outcome per phase/model; trial a cheaper tier only for measured low-risk summarization, not the conflict resolver. **Savings:** unknown without phase attribution or prices. Quality risk **medium** for tier changes; **none** for instrumentation.
3. **Constrain context only where useful.** The enriched totals include 7,175,139 prompt tokens, 36,643,384 cache-read tokens, zero recorded cache-write tokens, and zero observed context-budget warnings. Preserve stable prompt prefixes; log prefix identity and included-context bytes before trimming any content. **Savings:** unquantified; a 10% reduction in the measured *uncached prompt-token component* would be about 717,500 tokens if quality and call count held constant. Quality risk **medium** for trimming.
4. **Evaluate MCP output against work displaced.** Enriched telemetry reports 33 Semble query observations and 252,554 logged bytes. In the raw deep dives, job/step log duplication reduces 31 observations to 19 job-level queries: 10 `overflow` queries returned 65,740 bytes, seven `conflict-resolver-context` queries 42,604 bytes, and two `reviewer-context` queries 29,260 bytes. Semble’s effect on prompt expansion is **not measurable** without selected-versus-included byte counts. Serena has zero observed queries or tool calls and was configured off in review run `36221120963`; there is no evidence it displaced downstream work. Log selected, included, and discarded bytes by target before changing query volume. Savings and quality risk **unknown**.
5. **Avoidable reruns:** all 47 failed runs show attempt 1 and zero recorded retries, but separate dispatches and heal cycles can still repeat work. Correlate failure fingerprints across run IDs before estimating rerun cost; do not infer that recorded retries of zero mean no repeated AI spending.

## Reliability Improvements

1. **Inventory parity — documentation/validation category.** CI runs `36219304455`, `36220264510`, and `36220639422` each report 11 parity errors, including missing entries for `.github/workflows/claude-issue-intake.yml` and `scripts/claude_issue_handoff.sh`, `scripts/claude_issue_intake.sh`, and `scripts/claude_issue_route.py`. Reconcile `docs/INVENTORY.md` with those files and its references; keep parity enforced and move it early. **Expected impact:** eliminate this verified failure signature, which is the reported failure point in 35 CI runs. **Rollback:** restore test ordering if necessary, never disable parity.
2. **Resolver scope — safety/contract category.** Seven review failures identify `Run Codex resolver, validate, stage, commit`; inspected logs show `ValueError` without its safe classification. In runs `36221714214`, `36221717540`, `36222494360`, and `36224174449`, the model ran `git add` before the error. Inference: an index change may violate the captured merge-state invariant; other ValueError branches remain possible. Add `action`, `attempt`, and enumerated `reason_code` diagnostics without paths or untrusted exception text. Prevent model staging until after trusted verification. **Expected impact:** distinguish causes and reduce repeat failures once confirmed. **Fail-closed and rollback:** retain the current no-commit behavior; revert any staging change that fails scope-contract tests.
3. **Logical failure despite workflow success — outcome category.** Run `36221120963` logged `editor_changes_lost`; PR `4516` later hit `AUTOFIX_FINGERPRINT_CAP_TRIPPED count=3 max=3` and `mergeable_state=dirty` in successful gate run `36224680771`. Emit final `logical_outcome` and terminal reason alongside the Actions conclusion, then hand capped or dirty PRs back without another identical-head fix attempt. **Expected impact:** fewer ineffective cycles and clearer escalation. **Rollback:** keep existing caps and merge guards; do not raise them to obtain a green-looking run.
4. **MCP signals must be classified.** All 64 observed `SEMBLE_FALLBACK` events are `target=overflow`, `context=contract-test` in 16 CI deep-dive runs, four per run; runtime fallback count is zero. This is healthy contract-test fail-open evidence, **not** a 64-event production outage. No runtime Serena probe or fallback was observed, so availability is unknown, not proven healthy. Keep contract-test events separate from runtime alerts and emit availability when Serena is enabled. Observed `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero in enriched telemetry; sampled coverage cannot rule out policy or prompt-size pressure elsewhere.

## AI Memory Health

Nine distinct reviewer `retrieve` operations appear in raw job logs; **9/9 selected records**, averaging **1,395.7 estimated tokens against a 1,400-token budget** (99.7%). Keyword method was `llm` in all nine (`plain`: 0; `none`: 0). Runs `36221122176`, `36221714214`, and `36222494360` include failure-event pushes requiring two attempts; none observed required more than two. No observed retrieve selected zero records, set `enabled:false`, or logged `fail_open:true`. The job logs also contain 18 `record-run-event` and two `record-candidate` operations; `finalize-task`, `promote`, `compact`, and processed-command operations were not observed in these deep dives.

**Recommendation:** log retrieval eligibility, selected count, budget utilization, and safe push-retry reason by role; verify telemetry emission in memory-maintenance and issue-status workflows, which lack raw logs here. **Expected impact:** expose low-value near-budget retrieval and intermittent push failures without changing memory contents or fail-open behavior.

## GH API Call Audit

**No call-count hotspot is verified.** Neither the supplied aggregate nor raw logs provide requests by endpoint, job, or step; echoed `gh api` shell code is not evidence that a request ran. No rate-limit event is established by the available telemetry.

The review sweep in run `36224671806` found four PR candidates and dispatched four; run `36222485629` found four, dispatched two, and skipped two active candidates. Instrument its existing enumeration/dispatch path and `scripts/gh_helpers.sh` with an end-of-step summary of normalized endpoint, method, call count, cache hits, retries, elapsed time, and minimum remaining limit—without IDs, URLs containing identifiers, bodies, or credentials. Reuse the already enumerated PR data for dispatch decisions. **Verified call reduction:** unknown. **Conditional bound:** if instrumentation finds four redundant per-PR lookups replaceable by one existing-pattern GraphQL batch, that slice would drop by three calls (75%), reducing rate-limit exposure. Follow `unattended_system_instructions.md` §14: extend an existing fetch or cycle-local cache first, batch only when needed, and fail open to the smallest safe legacy lookup.

## Prompt Cache & Memory System

The enriched cache-hit rate is **83.63%**, calculated from cache reads relative to prompt, cache-read, and cache-write input tokens on usage-covered calls—not across all 1,000 runs. Run `36221120963` reports 85.04% across 26 calls; run `36214926446` reports 79.39% across 13. Zero recorded cache-write tokens alongside substantial reads warrants checking what the provider emits, not assuming writes never occur. There is no direct evidence of cache fragmentation or cache fail-open events.

Keep invariant instructions and retrieved-memory framing ahead of run IDs, timestamps, and variable Semble output; log a non-reversible prefix identifier plus cache read/write and included-memory tokens per call. Compare hit rate and latency before and after any change. **Expected impact:** measurable protection of cached tokens and possibly lower latency; magnitude unknown. The nine observed retrieves nearly fill their budgets, so track selected-record utility rather than expanding the budget. Zero observed `CONTEXT_BUDGET_WARN` does not justify further prompt growth.

## Orchestrator Health

The poller completed **27/27** runs successfully, with p50 **339 seconds** and p95 **631.3 seconds**; its work-versus-wait breakdown is unavailable. Clarify, plan, and implement show many skipped runs—135/141, 129/134, and 129/134 respectively—so their short overall medians must not be read as active-work latency. Log eligibility and skip reason with wave/stage transitions, then measure clarification, deferral, and conflict-heal dwell time before changing cadence.

Two sampled heal-intake summaries, runs `36222541399` and `36224214722`, report `lineage_cap gen=4 max=3`; run `36224680771` reports a dirty PR and a capped autofix fingerprint. Preserve both caps. Emit a deduplicated terminal fingerprint, parent run, terminal reason, and hand-back state so teams can track distinct stalled items rather than counting repeated successful intake runs. **Expected impact:** less ambiguous escalation and fewer identical re-dispatches; no safe failure-rate estimate yet.

## Pipeline Flow Bottlenecks

| Stage or overhead | Evidence | Next diagnostic or fix |
|---|---|---|
| Clarify → plan → implement | Predominantly skipped runs; implement outlier `36213324504` lasted 2,105 seconds | Log eligible/active/skipped reason and phase timings; do not optimize the skip median. |
| CI validation | 35 late `Inventory parity` failures; CI p50 1,124 seconds | Fix inventory and move parity to early preflight. |
| Review/autofix and merge/conflict | Seven resolver-step failures; review p95 1,575.2 seconds; `36221120963` lost editor changes | Add scope reason codes, trusted staging, and logical-outcome reporting. |
| Poll/coordination | Poller p50 339 seconds, with no step-level wait attribution | Log cycle fetch, evaluation, dispatch, and sleep durations. |
| Queueing | All 1,000 run rows have `run_started_at == created_at`; job queue timestamps are absent | Collect job queued/started times before claiming a queue bottleneck. |

These are ordered by observed end-to-end impact. Compute spent before late CI failure and review cycles without a retained change outrank subsecond gate optimizations; API wait, queue time, and merge overhead remain unmeasured.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** late CI parity, followed by long review/autofix and poll cycles. **Top failure modes:** 35 CI parity failure points; seven review resolver-step failures; one reported `editor_changes_lost` logical failure in successful run `36221120963`. **Highest measured cost driver:** review/autofix accounts for all 44,400,792 enriched reported tokens.

**Top three actions:** (1) reconcile inventory and fail CI early; (2) add safe resolver scope reason codes and test trusted post-check staging; (3) add logical-outcome, per-phase usage, and existing-helper API summaries. These retain current safety gates and require no new service.

## Metrics Appendix

The enriched `analysis_context.json` includes telemetry beyond the 29 raw-log runs in `summary.json`; the cohorts **overlap and must not be added together**. The 1,000-run outcome and duration window is shared.

| Window / family | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 350 (35.0%) | 47 (4.7%) | 5 | 598 | 7 / 1,110 s |
| CI | 47 | 7 | 39 (83.0%) | 1 | 0 | 1,124 / 2,660.8 s |
| Review/autofix | 147 | 134 | 8 (5.4%) | 4 | 1 | 37 / 1,575.2 s |
| Orchestrate poll | 27 | 27 | 0 | 0 | 0 | 339 / 631.3 s |

| Usage metric | Enriched context | Raw-log summary |
|---|---:|---:|
| Runs with parsed log telemetry | 124 | 29 |
| OpenRouter usage-recorded calls | 45 | 39 |
| Reported total / prompt / completion tokens | 44,400,792 / 7,175,139 / 585,559 | 39,246,390 / 6,339,289 / 535,989 |
| Cache read / recorded write tokens | 36,643,384 / 0 | 32,373,200 / 0 |
| `cache_hit_rate` | 83.6253% | 83.6247% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 9,500 / 2,673,340; 120 samples | 1,110,000 / 2,706,960; 29 samples |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | 0 / 0 |

The wall-clock cohorts differ markedly because of selection and enrichment; neither is a per-model latency percentile. Dollar totals, model-specific pricing, cache-write coverage, and active-only stage durations were not supplied.

| API and MCP signal | Observation |
|---|---|
| GitHub API calls by route; retries; rate-limit events | **Not measured**; call-count and batching savings cannot be verified. |
| Semble collector query observations / logged bytes | Enriched: 33 / 252,554; raw: 31 / 225,488. Raw job-level deduplication: **19 / 137,604**. |
| Semble job-level targets | `overflow`: 10 / 65,740 bytes; `conflict-resolver-context`: 7 / 42,604; `reviewer-context`: 2 / 29,260. |
| Semble fallbacks | 64 `overflow` contract-test events across 16 CI logs; **0 observed runtime fallbacks**. A fallback/query rate would misleadingly mix tests with review queries. |
| Serena | 0 queries, response bytes, tool calls, fallbacks, and query milliseconds observed; per-tool breakdown unavailable because no calls occurred. |
| Other MCP servers observed | None in runtime event lines; continue detecting unknown `*_QUERY`, `*_FALLBACK`, and `*_PROBE` prefixes. |

| MCP availability target | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Serena — no target emitted | 0 | 0 | 0 | No runtime probe observed; availability unknown. |
| Semble — no probe emitted | 0 | 0 | 0 | Query and contract-test fallback evidence is not an availability probe. |

**Collection gaps:** raw logs cover 29/1,000 runs; the enriched context covers 124/1,000 with parsed telemetry. Failed CI logs directly verify three matching parity signatures, while the remaining parity count comes from collector failure points. Add safe structured diagnostics at the existing CI, resolver, API-helper, and orchestrator boundaries, then compare the next window against these baselines.
