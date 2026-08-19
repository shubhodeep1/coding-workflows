## Executive Summary

- Operational health is good: `shubhodeep1/coding-workflows` had `158` runs, `156` successes, `0` failures, `2` cancellations, and `break_glass_count=0`. The biggest opportunities are latency and observability, not correctness repair. **Estimated impact:** prioritization clarity. **Confidence:** high.
- `orchestrate_poll` is the largest systemic latency sink: `85` runs, `p50=214s`, `p95=249.6s`; recent/slow runs `32192897594` (`202s`), `32134044882` (`199s`), `32121310013` (`184s`), and `32206611869` all show hosted-runner wait before useful work. **Estimated impact:** ~`20–40s` fresher latest-poll latency plus lower runner-minute burn if superseded polls are collapsed (inference). **Confidence:** high on cause, medium on savings.
- `review_autofix` is healthy at the median but unstable in the tail: `53` runs, `p50=7s`, `avg=58.4s`, `wall_clock_p99=1,359,800ms`; outliers `32209176582` (`1196s`) and `32097799991` (`1456s`) correlate with `MODEL_EDITOR=openai/gpt-5.4`, `EDITOR_REASONING_EFFORT=xhigh`, repeated `CONTEXT_BUDGET_WARN_RATIO: 0.7`, and a `SEMBLE_QUERY ... bytes=14999`. **Estimated impact:** `6–12 min` faster worst-case review cycles. **Confidence:** medium.
- The recurring CI “failure” pattern is cancellation, not test breakage: the `ci` family has `3` runs, `2` cancelled (`32097833960`, `32097871594`), `0` failed; both tails end with `The operation was canceled` after many PASS lines. **Estimated impact:** high triage-noise reduction if cancellations are logged separately. **Confidence:** high.
- Telemetry coverage is the main blocker to deeper optimization: `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, Serena query/probe counters are all `0`, and `context_budget_warn_count=0` even though runs `32209176582` and `32209207170` emitted `CONTEXT_BUDGET_WARN_RATIO: 0.7`. `scripts/cost_audit.py` already supports these fields, so this is mostly an emitter gap. **Estimated impact:** high diagnostic value, low implementation risk. **Confidence:** high.
- Semble looks runtime-safe but under-instrumented: repo aggregate shows `3` queries / `44,514` bytes and `20` fallbacks, but all `20` are contract-test fallbacks and runtime fallbacks are `0`. **Estimated impact:** medium cost/quality visibility gain from better logging, not from disabling the feature. **Confidence:** high.

## Speed Optimizations

1. **Critical-path win: split `review_autofix` into deterministic pre-gate and model-heavy post-gate**
   - **Evidence:** `review_autofix` has `53` runs with `p50=7s` but outliers `32209176582` (`1196s`) and `32097799991` (`1456s`). Run `32209176582` logged `MODEL_EDITOR: openai/gpt-5.4`, `EDITOR_REASONING_EFFORT: xhigh`, repeated `CONTEXT_BUDGET_WARN_RATIO: 0.7`, and `SEMBLE_QUERY target=reviewer-context chunks=12 bytes=14999 ms=528`.
   - **Root cause:** expensive context expansion and high-effort reasoning start before the workflow fully proves the PR needs heavy review/autofix. The long tail is likely prompt-size pressure plus model latency (inference).
   - **Exact change:** move docs-only/materiality/small-diff gating to the very front; only after gate pass should Semble retrieval, AI-memory retrieval, and high-effort model paths activate. Add per-phase logs for `phase_wall_ms`, `prompt_tokens`, and `retrieved_bytes`.
   - **Estimated time savings:** `6–12 min` on tail runs; little effect on the `7s` median.
   - **Implementation risk:** low if the existing heavy path remains the fallback for non-skipped PRs.

2. **High-volume win: collapse superseded `orchestrate_poll` runs and expose queue-vs-compute time**
   - **Evidence:** `orchestrate_poll` ran `85` times with `p50=214s`; recent runs `32192897594`, `32134044882`, `32121310013`, `32128233450` and slow run `32206611869` all cite `Job is waiting for a hosted runner to come online.` Run `32210060132` still took `224s` even though `poll/Find active tracking issues` found only `1 active tracking issue(s)`.
   - **Root cause:** a frequent poller pays full hosted-runner queue/startup overhead on nearly every cycle.
   - **Exact change:** add or verify same-branch `concurrency` with cancel-in-progress for scheduled polls; emit `RUNNER_QUEUE_WAIT_MS` and `JOB_COMPUTE_MS`; if the poll finds no actionable work, exit before heavier follow-up.
   - **Estimated time savings:** likely `20–40s` on latest actionable polls under queue pressure, plus `20–30%` fewer runner-minutes if overlapping ticks are being queued (inference).
   - **Implementation risk:** low if only superseded scheduled polls are cancelled.

3. **Micro-optimization: fast-path no-op and skip workflows before full runner setup**
   - **Evidence:** `forward_merge_stable_to_main` run `32210704521` was a no-op and still took `33s`; `review_autofix` run `32209237463` took `17s` for post-merge validate dispatch; docs-only skip run `32209207170` still took `30s`.
   - **Root cause:** cheap predicates are evaluated after runner allocation and job setup.
   - **Exact change:** move no-op checks into job-level `if:` or the earliest shell step; reuse already-fetched PR metadata across steps instead of recomputing it.
   - **Estimated time savings:** `10–25s` on no-op/skip runs.
   - **Implementation risk:** low.

## Cost Optimizations

1. **Gate high-effort reasoning by materiality**
   - **Evidence:** long run `32209176582` used `openai/gpt-5.4` with `EDITOR_REASONING_EFFORT: xhigh`; docs-only skip run `32209207170` also configured `EDITOR_REASONING_EFFORT: xhigh` even though it skipped. Repo aggregate only shows `6` Codex calls / `12,156` tokens across `115` logged runs, so measured token cost is clearly incomplete.
   - **Root cause:** aggressive reasoning defaults are configured too early; cost telemetry is too sparse to prove where the spend lands.
   - **Exact change:** default to medium reasoning, escalate to xhigh only after gate pass or after a failed first-pass review/judge cycle.
   - **Estimated savings:** likely the largest model-cost reduction in this window; directional estimate `20–40%` on expensive review runs.
   - **Quality risk:** low if escalation is preserved.

2. **Turn on prompt-cache telemetry before tuning prompt text**
   - **Evidence:** repo-level `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`; same for `review_autofix`, `orchestrate_poll`, and `ci`.
   - **Root cause:** the cache is either unused or, more likely, not emitting parseable telemetry; today those cases are indistinguishable.
   - **Exact change:** emit per-phase cache lines already supported by `scripts/cost_audit.py`, and move volatile fields (run IDs, timestamps, queue noise) after the stable prompt prefix.
   - **Estimated savings:** not yet quantifiable; the immediate win is visibility. Once stable-prefix hits are measurable, repetitive review prompts should yield meaningful token savings.
   - **Quality risk:** none.

3. **Measure Semble’s byte efficiency; don’t disable it**
   - **Evidence:** repo aggregate shows `3` `SEMBLE_QUERY` calls totaling `44,514` bytes, `0` runtime fallbacks, and `20` contract-test fallbacks. In run `32209176582`, Semble fetched `12` chunks / `14,999` bytes in `528ms`.
   - **Root cause:** Semble is low-volume and appears safe, but the logs do not say whether those bytes replaced larger raw context.
   - **Exact change:** log `prompt_bytes_before`, `bytes_added_from_semble`, `bytes_avoided`, `selected_chunks`, and `fallback_reason`.
   - **Estimated savings:** modest direct token savings per heavy review, but high decision quality for future Semble tuning.
   - **Quality risk:** low; current runtime fail-open behavior should remain.

4. **Do not spend optimization time on Serena until it emits status**
   - **Evidence:** Serena totals are all `0` (`query_calls`, `response_bytes`, `tool_calls`, `fallbacks`, `probe_*`), and run `32209176582` logged `SERENA_ENABLED: false`.
   - **Root cause:** no active Serena usage in the sampled window.
   - **Exact change:** add a single structured Serena status line per job (`enabled`, `available`, `reason`, `probe_result`) before any deeper tuning.
   - **Estimated savings:** avoids wasted engineering effort more than direct dollar savings.
   - **Quality risk:** none.

**Avoidable reruns:** not a major cost source in this window. Most sampled recent runs show `retries=0`; the avoidable spend is runner-minutes on no-op and queue-heavy paths, not rerun storms.

## Reliability Improvements

_No `BREAK_GLASS` was observed (`break_glass_count=0`), so this window does not show rubric/policy pressure forcing unsafe overrides._

1. **Separate cancellations from failures in CI telemetry and dashboards**
   - **Failure evidence:** `ci` has `3` runs, `2` cancelled, `0` failed. Runs `32097833960` and `32097871594` ended with `##[error]The operation was canceled.` after many PASS lines, but failure metadata points at `lint / Orchestrate poll process unit tests`.
   - **Root cause category:** workflow preemption or manual/upstream cancellation misclassified as test failure.
   - **Exact fix:** emit `RUN_CANCELLED reason=<manual|concurrency|upstream> cancelled_step=<step>`, and keep cancelled conclusions out of failure-rate charts.
   - **Expected impact:** high reduction in false failure investigation and unnecessary reruns.
   - **Rollback/fail-open:** additive logging only.

2. **Emit parseable `CONTEXT_BUDGET_WARN:` lines**
   - **Failure evidence:** runs `32209176582` and `32209207170` emitted `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but repo aggregate still shows `context_budget_warn_count=0`.
   - **Root cause category:** collector/emitter mismatch.
   - **Exact fix:** emit the structured line the parser already expects, including `phase`, `prompt_tokens`, `model_context_window`, `ratio`, and `threshold`.
   - **Expected impact:** medium reliability gain by surfacing prompt-size risk before it turns into timeouts or degraded review quality.
   - **Rollback/fail-open:** keep the old ratio line if useful, but add the parseable one.

3. **Make MCP availability and fail-open state explicit**
   - **Failure evidence:** run `32208590631` logged `SEMBLE_ENABLED: true`, `SEMBLE_AVAILABLE: false`, `SEMBLE_INDEX_AVAILABLE: false`; run `32209176582` logged `SERENA_ENABLED: false`; Serena probe/query totals are all `0`. Semble fallbacks total `20`, but all are contract-test fallbacks in `ci`, not runtime breakage.
   - **Root cause category:** masked rollout ambiguity.
   - **Exact fix:** emit `MCP_STATUS server=<name> target=<target> enabled=<bool> available=<bool> probe_result=<ok|failed|skipped> reason=<...> fail_open=<bool>`.
   - **Expected impact:** medium; teams can distinguish “feature off” from “broken install/index/probe” without changing behavior.
   - **Rollback/fail-open:** preserve current fail-open behavior.

4. **De-duplicate support-ref fallback warnings**
   - **Failure evidence:** `issue_pr_status` run `32209237446` repeats `Support checkout ref ${script_ref} is unavailable; using main.`
   - **Root cause category:** repeated fallback resolution across steps.
   - **Exact fix:** resolve once, export once, and emit one structured fallback summary.
   - **Expected impact:** low-to-medium; improves anomaly signal-to-noise.
   - **Rollback/fail-open:** none.

## AI Memory Health

- **Observed telemetry is write-heavy, not read-heavy.** Sampled deep-dive logs show:
  - `orchestrate_poll` run `32210060132`: `AI_MEMORY_TELEMETRY` for `poll_started` / `poll_completed` with `did_push: true`.
  - `review_autofix` run `32209176582`: `record-run-event` and `record-candidate`.
  - `issue_pr_status` run `32209237446`: `finalize-task` with `ok=true enabled=true fail_open=true reason=no_linked_issues`.
- **Retrieval effectiveness cannot be scored.** I saw `0` sampled `retrieve` operations, so:
  - hit rate (`records_selected > 0`) is **unavailable**
  - average `estimated_tokens` vs budget is **unavailable**
  - `keyword_method` distribution (`llm` / `plain` / `none`) is **unavailable**
- **Fail-open posture looks safe but under-measured.** The sampled `finalize-task` line succeeded with `fail_open=true`, which is acceptable; the problem is that retrieve/compact/promote visibility is missing.
- **No sampled `compact`, `promote`, `processed-command-claim`, or `processed-command-complete` lines** were observed, and no push retry counts were exposed.
- **Recommendation:** emit one `AI_MEMORY_TELEMETRY` `op=retrieve` line on every attempted read, even when disabled or zero-hit, with `enabled`, `fail_open`, `records_selected`, `estimated_tokens`, `budget_tokens`, `keyword_method`, and retry counts. Add one end-of-run summary with retrieved/pushed/finalized counts.

## GH API Call Audit

| Pattern | Evidence | Root cause | Exact change | Est. reduction |
|---|---|---|---|---|
| Repeated PR file-list fetches in `review_autofix` | Run `32209207170` logs `gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files"` during docs-only skip evaluation | Same PR file list is fetched in multiple steps instead of reused | Fetch once, store `${RUNNER_TEMP}/pr_files.json`, reuse everywhere | `1–3` API calls per review run |
| Open-PR sweep work even when nothing is dispatched | Sampled sweep run `32209726358` ended with `candidates=0`, `dispatched=0`, but still paginated open PRs | Pagination happens before a cheap zero-candidate short-circuit | Exit early when upstream candidate source is empty; log `items_scanned` and `candidates_remaining` | Potentially one full repo PR pagination per empty sweep |
| No quantitative API accounting | No rate-limit or retry storm evidence in recent/slow logs, but no total GH API counts are emitted | API usage is observable only through ad hoc command lines | Add `GH_API_SUMMARY calls=<n> retries=<n> secondary_limit_hits=<n> top_endpoint=<...>` at job end | Enables real rate-limit auditing |

**Current assessment:** no rate-limit events or GH API retry storms were visible in sampled logs. The problem is redundancy, not throttling.

## Prompt Cache & Memory System

- **Cache telemetry is effectively absent.** Repo aggregate: `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0` across `115` logged runs. This prevents any real cache audit.
- **Context-budget pressure is real but hidden.** Runs `32209176582` and `32209207170` both emitted `CONTEXT_BUDGET_WARN_RATIO: 0.7`, yet aggregate `context_budget_warn_count=0`.
- **Semble appears safe and probably useful when invoked.** `3` queries / `44,514` bytes with `0` runtime fallbacks suggests it is not destabilizing runtime; the missing datum is whether those bytes actually replaced larger prompt expansion.
- **Serena has no measurable effect in this window.** All Serena counters are `0`; sampled evidence shows it disabled rather than failing.
- **Lowest-risk logging additions:**
  - `CONTEXT_BUDGET_WARN: workflow=... phase=... prompt_tokens=... model_context_window=... ratio=... threshold=...`
  - `OR_USAGE: workflow=... phase=... prompt_tokens=... completion_tokens=... total_tokens=... cache_read_tokens=... cache_write_tokens=... model=... reasoning=...`
  - `PROMPT_CACHE_STATUS: workflow=... phase=... key_prefix_hash=... hit=<bool> read_tokens=... write_tokens=...`
  - `AI_MEMORY_TELEMETRY: {"op":"retrieve", ...}`
  - `MCP_STATUS: server=semble|serena ...`
- **Expected impact:** first diagnostic, then optimization. Once stable-prefix hits are measurable, prompt-cache tuning should reduce tokens and tail latency; right now the safe move is emitter coverage.

## Orchestrator Health

- **Overall flow health is good:** `156` successes, `0` failures, `2` cancellations, `break_glass_count=0`.
- **Poller health is stable but inefficient:** `orchestrate_poll` succeeded across `85` runs, but its `p50=214s` is dominated by runner wait. Run `32210060132` found `1 active tracking issue(s)`; the orchestration decision work itself appears small relative to startup time.
- **Decision-loop visibility is weak:** repo aggregate shows `or_calls=9`, but `or_prompt_tokens=0`, `or_completion_tokens=0`, and `or_total_tokens=0`. That means clarify/plan/judge cost cannot be audited yet.
- **No direct evidence of stuck clarification loops, conflict-heal retries, or bad terminal states** was visible in sampled logs, but confidence is only medium because those states are not explicitly logged.
- **Smallest safe mitigation:** emit `ORCHESTRATOR_STATE phase=<...> iteration=<n> decision=<...> active_issues=<n> queue_ms=<n> compute_ms=<n> judge_cycles=<n> defer_reason=<...> conflict_heal_attempt=<n>` on every transition, plus one `ORCHESTRATOR_SUMMARY`.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Smallest safe fix |
|---|---|---|---|
| Queue / dispatch | Hosted-runner wait | `orchestrate_poll` `p50=214s`; many runs quote `Job is waiting for a hosted runner to come online.` | Concurrency-collapse superseded polls; log queue vs compute time |
| Clarify / plan | Observability gap | `or_calls=9` but OR token totals are all `0` | Emit OR phase/token summaries |
| Implement / review | Long-tail context/model work | `review_autofix` outliers `1196s` and `1456s`; context warnings and high reasoning in `32209176582` | Gate before heavy context/model work; log prompt size and retrieved bytes |
| Validate / CI | Cancellation noise | `ci` has `2/3` cancelled runs, `0` failed runs | Separate cancellation telemetry from failure telemetry |
| Merge / follow-up | No-op runner startup | No-op forward merge `32210704521` still took `33s`; docs-only skip `32209207170` still took `30s` | Earlier no-op predicates and metadata reuse |
| Retry / conflict overhead | Low today, under-logged | Sampled recent runs mostly `retries=0`; fetch backoff exists but was not exercised | Keep current backoff; only add structured retry counters when attempts > 1 |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `orchestrate_poll` queue wait (`85` runs, `p50=214s`, repeated hosted-runner wait lines).
  - `review_autofix` long-tail outliers (`32209176582` at `1196s`, `32097799991` at `1456s`).
  - No-op/skip workflows still paying runner startup (`32210704521`, `32209207170`, `32209237463`).

- **Top failure modes**
  - CI cancellations misread as test failures (`32097833960`, `32097871594`).
  - Context-budget warnings present in logs but absent from aggregates (`32209176582`, `32209207170`).
  - Cache/Serena/AI-memory-retrieve telemetry missing, limiting root-cause accuracy.

- **Highest-cost drivers**
  - High-effort review configuration on the heavy review path.
  - Unknown prompt-cache effectiveness (`cache_hit_rate=null`).
  - Semble used only a few times, but without byte-savings telemetry.
  - Serena not active enough to judge cost or value.

- **Top 3 prioritized actions**
  1. Emit the structured telemetry lines `scripts/cost_audit.py` already knows how to parse.
  2. Split `review_autofix` into pre-gate and post-gate phases, with late reasoning escalation.
  3. Collapse superseded poll runs and log runner queue time explicitly.

## Metrics Appendix

_Token/cache figures below are lower bounds: `115/158` runs had parsed log telemetry, and several emitters appear missing._

### Repo summary

| Repo | Runs | Success | Failure | Cancelled | Avg s | p50 s | p95 s | Parsed log telemetry |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 158 | 156 | 0 | 2 | 172.0 | 197.0 | 263.0 | 115 |

### Workflow family metrics

| Workflow family | Runs | Success | Failure | Cancelled | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| cancel_on_pr_close | 2 | 2 | 0 | 0 | 12.0 | 12.0 | 12.9 |
| ci | 3 | 1 | 0 | 2 | 1715.0 | 1817.0 | 1818.8 |
| copilot_pull_request_reviewer | 2 | 2 | 0 | 0 | 173.0 | 173.0 | 226.1 |
| drift_audit | 1 | 1 | 0 | 0 | 13.0 | 13.0 | 13.0 |
| forward_merge_stable_to_main | 1 | 1 | 0 | 0 | 33.0 | 33.0 | 33.0 |
| integration_pr_readiness | 4 | 4 | 0 | 0 | 9.0 | 9.5 | 10.0 |
| issue_pr_status | 2 | 2 | 0 | 0 | 14.0 | 14.0 | 14.9 |
| lint_pr_body_auto_close | 2 | 2 | 0 | 0 | 7.5 | 7.5 | 8.0 |
| nightly_validation_selftest | 1 | 1 | 0 | 0 | 135.0 | 135.0 | 135.0 |
| orchestrate_poll | 85 | 85 | 0 | 0 | 214.8 | 214.0 | 249.6 |
| promote_main_to_stable | 1 | 1 | 0 | 0 | 47.0 | 47.0 | 47.0 |
| review_autofix | 53 | 53 | 0 | 0 | 58.4 | 7.0 | 32.8 |
| workspace_cache_maintenance | 1 | 1 | 0 | 0 | 9.0 | 9.0 | 9.0 |

### Cost / wall-clock telemetry

| Scope | Log runs | Codex calls | Codex tokens | OR calls | OR total tokens | Cache hit rate | OR cache read/write | Wall p50 ms | Wall p99 ms | Break glass | Context warn count |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|
| Repo total | 115 | 6 | 12156 | 9 | 0 | null | 0 / 0 | 202000 | 1773880 | 0 | 0 |
| review_autofix | 38 | 6 | 12156 | 9 | 0 | null | 0 / 0 | 7000 | 1359800 | 0 | 0 |
| orchestrate_poll | 65 | 0 | 0 | 0 | 0 | null | 0 / 0 | 216000 | 273760 | 0 | 0 |
| ci | 3 | 0 | 0 | 0 | 0 | null | 0 / 0 | 1817000 | 1818960 | 0 | 0 |

**Note:** `context_budget_warn_count` is likely undercounted; sampled logs for `32209176582` and `32209207170` contain `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but the aggregate remains `0`.

### Semble / Serena telemetry

| Scope | Semble queries | Semble bytes | Semble fallbacks | Contract-test fallbacks | Runtime fallbacks | Serena queries | Serena resp bytes | Serena fallbacks | probe_ok | probe_failed | probe_skipped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 3 | 44514 | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| review_autofix | 3 | 44514 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ci | 0 | 0 | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

### GH API summary

| Workflow / run evidence | Observed pattern | Quantified call count available? | Recommended logging |
|---|---|---|---|
| `review_autofix` `32209207170` | `gh api --paginate pulls/:pr/files` used on docs-only skip path | No | `GH_API_USAGE endpoint=pulls_files pages=<n> items=<n> cache_hit=<bool>` |
| Sweep run `32209726358` | Open PR pagination despite `candidates=0`, `dispatched=0` | No | `GH_API_USAGE endpoint=pulls_list pages=<n> items_scanned=<n>` |
| Recent runs overall | No rate-limit or retry storm evidence | No | `GH_API_SUMMARY calls=<n> retries=<n> secondary_limit_hits=<n>` |

### MCP availability / coverage

| Server / target | probe_ok | probe_failed | probe_skipped | Evidence | Coverage gap |
|---|---:|---:|---:|---|---|
| Semble / `reviewer-context` | n/a | n/a | n/a | Query seen in `32209176582`; contract-test fallback target `overflow` in `32097833960` and `32097871594` | No standardized probe/status line |
| Serena / all targets | 0 | 0 | 0 | No query/fallback/probe lines; `SERENA_ENABLED:false` in `32209176582` | Cannot distinguish disabled vs broken without status logging |

- **Other MCP servers observed without standardized query/fallback/probe telemetry:** `github-mcp-server` and `playwright` were `status=connected` in copilot reviewer run `32209222893`.
