## Executive Summary

- **The deep-dive logs were not available, so evidence is limited (high confidence on this point).** The stated run-logs path `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox. No `summary.json`, `errors/`, `slow/` or `recent/` files could be read. Every finding below comes from `analysis_context.json` aggregates, run-row `log_summary` fields and the repo source. `insufficient_data` is `false`, but root causes of the 3 CI failures cannot be confirmed.
- **The collector window covers only about 4.5 hours, because skipped runs fill the 1,000-run cap (high).** 747 of 1,000 runs are "other", nearly all skipped. Earliest `created_at` is 01:47:39Z and latest is 06:20:18Z on 2026-10-24. Expected impact: excluding or separately capping skipped runs would widen real coverage several times over at no extra API cost.
- **Review & Autofix accounts for 100% of measured model spend (high).** All 230.8M OpenRouter tokens (92 calls) sit in `review_autofix`, and 4 runs make up 89% of them: 40063935493 (76.7M), 40065164399 (72.9M), 40052406186 (33.7M) and 40057595876 (22.2M). Per-call input is very large: 3.84M tokens/call in 40063935493. Estimated saving from capping agentic turns or context per call: 20–40% of review spend (medium).
- **CI is the only failing family: 3 of 40 runs (7.5%), on 3 different steps, 2 of them in matrix job `orchestrate-poll (3)` (medium).** All 3 failed on attempt 1 with no retry recorded. The `Session automation retirement guard…` step has none of the failing-test summary lines the poll-shard step has (#6888/#6889). The top fix is a diagnostic one: emit failing test node IDs outside log groups.
- **Runner saturation stretches the clarify→plan path (high, from `log_summary`).** "All GitHub-hosted runners with label [ubuntu-latest] are busy":
  - plan 40069254028 queued about 7.6 min of 481s;
  - plan 40069224239 queued about 4 min of 270s;
  - clarify 40069110912 queued 74s, and clarify 40069109767 queued 47s.

  Queueing, not compute, dominated these plan runs.
- **Serena is completely silent and AI-memory telemetry is absent (medium).** There are 0 `SERENA_*` lines of any kind, including probes, and no `AI_MEMORY_TELEMETRY:` lines were visible. Either emission is broken or these systems are off. Both need an emission check.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Plan/clarify runner queueing (critical path).**
   - Evidence: the 4 `log_summary` queue waits above. All belong to the orchestrator project-8054 burst (issues #8055/#8056) between 06:09 and 06:20.
   - Root cause (inference): one issue comment fans out to 5–6 workflows (clarify, plan, implement, orchestrate_clarify_respond, workflow_failure_heal, check_failure_triage). Run 40069049189 is one example, triggered by a comment on #8051. Long CI and review jobs hold the same `ubuntu-latest` pool at the same time. CI p95 is 1,754s; review_autofix p95 is 3,383s.
   - Change: do not add infrastructure. Instead log the queue time explicitly so it can be tracked. Add a first-step line `RUNNER_QUEUE_WAIT phase=<phase> wait_s=<job.started_at - run.created_at>` in the plan and clarify reusable workflows. Then evaluate moving pure-test CI jobs off the shared pool's peak with path filters.
   - Estimated saving: 1–7 min per orchestrated plan/clarify hop. Risk: low, logging only.
2. **Review & Autofix wall clock.**
   - Evidence: p50 480s, p95 3,383s. Runs 40052406186 took 4,369s (13 calls) and 40057588130 took 4,130s (14 calls).
   - Root cause (inference): the number of sequential model calls times multi-million-token contexts. There is no per-phase timing in the context to split reviewer, aggregator and editor time.
   - Change: emit `REVIEW_PHASE_TIMING phase=<reviewer|aggregator|editor> ms=<n> or_calls=<n>` per phase, then cap autofix iterations in the slowest phase.
   - Estimated saving: 10–20 min on p95 runs once the slowest phase is identified. Risk: low for logging, medium for capping.
3. **Semble bootstrap on the review critical path.**
   - Evidence: 8 bootstraps took 340,704 ms in total, about 42.6s on average. 40065164399 spent 100,028 ms on 2 bootstraps; 40063935493 spent 63,542 ms on 2.
   - Change: reuse one bootstrap per run rather than one per phase. Two runs bootstrapped twice. Estimated saving: 30–50s per affected run. This is a micro-optimization next to 40–70 min runs. Risk: low.
4. **CI p50 of 895s.**
   - Evidence: the slowest runs were 40051830786 (1,819s), 40051391633 (1,774s) and 40051388407 (1,753s). Without step logs, the per-job critical path is unknown.
   - Change: once logs are available, rank steps by duration. The ci.yml comments already flag `orchestrate-poll` as the critical path (ci.yml:2153).

## Cost Optimizations

Ranked by expected token savings.

1. **Very large per-call context in review_autofix.**

   | Run | Total tokens | OR calls | Tokens/call |
   |---|---|---|---|
   | 40063935493 | 76.7M | 20 | 3.84M |
   | 40065164399 | 72.9M | 24 | 3.04M |
   | 40052406186 | 33.7M | 13 | 2.59M |

   - Root cause (inference): agentic multi-turn calls re-send the conversation each turn. Cache reads are 209.0M of about 229.6M input tokens (91%), so caching works, but cache reads still cost money and the absolute volume is huge.
   - Change: add a per-call turn/context ceiling for the editor and reviewer phases. Log `OR_CALL_USAGE phase= turns= prompt_tokens= cache_read_tokens=` so the 3–4M-token calls can be attributed.
   - Estimated saving: 20–40% of the 230.8M (medium confidence). Quality risk: medium, since caps can cut off fixes. Roll out behind an env var with the current behaviour as default.
2. **Model and reasoning level.** Plan and clarify run `MODEL_EDITOR=openai/gpt-6-sol`, `MODEL_REASONING_EFFORT=high` and `THINKING_LEVEL_CLARIFY_ORCHESTRATOR=high` (40069110912, 40069109767). Their token usage is not visible (logs truncated about 119–158k chars), so savings cannot be quantified. Recommendation: collect the data first, then reconsider before lowering effort.
3. **Context-budget pressure.** There were 3 `CONTEXT_BUDGET_WARN` events: 1 in 40052406186 and 2 in 40057588130. Both runs are also the two longest, at over 4,100s each. 40057588130 also had 6 of its 14 calls marked `or_usage_unavailable`, so its cost is understated. This is prompt-size risk rather than rubric pressure: `break_glass_count` is 0.
4. **Semble is efficient, not noisy.** 8 queries returned 123,373 bytes (about 15.4 KB per query, roughly 31k tokens in total) from 57 sources, with `static_dup_bytes_total=0`. That is under 0.2% of the 16.0M non-cached prompt tokens. Semble is not inflating the prompt, but at this size its effect on reducing prompt growth also cannot be shown. Keep it as is.
5. **Serena**: no queries, so there is no replacement value to assess.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **CI failures cannot be diagnosed from the collected telemetry.**
   - Evidence:

     | Run | Job | Step | Duration |
     |---|---|---|---|
     | 40052406057 | `orchestrate-poll (3)` | `Orchestrate poll process unit tests` | 1,264s |
     | 40065164413 | `orchestrate-poll (3)` | `Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)` (ci.yml:2471, runs only when `strategy.job-index == 3`) | 860s |
     | 40066148233 | `tests-hooks-and-orchestrator` | `Session automation retirement guard and retired labels/files tests` (ci.yml:760, 9 pytest modules incl. release-manifest tests) | 887s |

   - Root cause category: unknown, probably test regressions introduced by PRs (inference). Each CI failure has a review_autofix run with the same `created_at`: 40052406186 at 02:01:23, 40065164399 at 05:09:37, and 40066148385 at 05:24:44 (that one was cancelled at 581s). The failures look tied to PRs under autofix, not to flaky infrastructure.
   - Fix (diagnostic): the pytest one-liners at ci.yml:763 and ci.yml:2475 should add `-rfE`. They should also print a `CI_TEST_FAILURE step=<name> nodeid=<id>` line outside any `::group::`, as the poll step already does (ci.yml:2215–2251).

     ```bash
     PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -rfE -p no:cacheprovider --junitxml=/tmp/junit_step.xml <modules> || rc=$?
     python3 - <<'PY' || true
     import xml.etree.ElementTree as ET, re
     for tc in ET.parse('/tmp/junit_step.xml').iter('testcase'):
         if tc.find('failure') is not None or tc.find('error') is not None:
             nid = re.sub(r'[^A-Za-z0-9_.:/\[\]-]', '', f"{tc.get('classname')}::{tc.get('name')}")
             print(f"CI_TEST_FAILURE nodeid={nid}")
     PY
     exit "${rc:-0}"
     ```

   - Impact: future failure triage becomes evidence-grade, and check_failure_triage/heal can key off a stable prefix. Fail-open: the summary block ends in `|| true` and the step's original exit code is kept.
2. **Two of the three failures land in matrix job `orchestrate-poll (3)`.** Run 40052406057 failed in the main sharded step; 40065164413 failed in the index-3-only step. Two points do not make a trend. Track `failed_job=orchestrate-poll (3)` frequency over the next window before rebalancing.
3. **Semble fallbacks: healthy, plus 4 runtime fallbacks that need explaining.**
   - 24 of 28 are `context=contract-test` (classified at cost_audit.py:381). They are spread over CI runs 40052406057 (4), 40066148233 (8), 40065164413 (4), 40051830786 (4) and 40051391633 (4). These are test-emitted, not production.
   - The 4 runtime fallbacks are all in implement run 40063869266 (1,765s, success). Without the log, the `reason=` is unknown. semble_helpers.sh can emit `binary-unavailable`, `index-unavailable`, `timeout`, `lazy-bootstrap-failed` and others. That run had `semble_bootstraps=0` and `query_calls=0`, so Semble effectively never worked in it. This looks like a masked bootstrap or availability failure in implement, not a rare query fail-open (inference).
   - Mitigation: make `cost_audit` report per-reason fallback counts. Have implement emit `SEMBLE_BOOTSTRAP` (or an explicit skip reason) before its first query, so a "never bootstrapped" run is visible.
4. **Unavailable usage data.** 9 of 92 OR calls (9.8%) had no usage data, 6 of them in 40057588130. Cost is under-counted there. Log the provider and HTTP status on `or_usage_unavailable` events.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were found. The deep-dive logs were not mounted. Every available `log_summary` explicitly says none were visible: 40069254028, 40069224239, 40069110912 and 40069109767. Those logs were truncated by about 119k–158k chars, so the lines may exist in the hidden middle section. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled: false` therefore **cannot be computed**.

Recommendation: confirm emission by grepping for `AI_MEMORY_TELEMETRY` in the next collector's `recent/` plan logs. Then have the collector move memory and telemetry lines into a protected head/tail region that survives `log_summary` truncation.

## GH API Call Audit

- **No GH API call-count, hotspot or rate-limit telemetry exists in this window.** No such fields appear in `analysis_context.json`. No 429s or retries were seen in any `log_summary`. A repo grep for `GH_API_CALLS|GH_API_USAGE|RATE_LIMIT_REMAINING` in `scripts/` returned nothing.
- Highest-risk surface (inference): `orchestrate_poll` runs about every 12 minutes for 633s on average (35 runs, p50 712s) with `runs_with_log_telemetry=0`. Per the API-hygiene rules (§14), its batching helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) are the expected call paths, but nothing confirms the cache hit or miss behaviour.
- Diagnostic addition: at the end of each poll cycle, emit one line `GH_API_BUDGET phase=orchestrate_poll core_remaining=<n> graphql_remaining=<n> core_used_cycle=<n>`. This needs one `gh api rate_limit` call per cycle, which is free against the quota. Add a matching `cost_audit` parser.
- Collector cost: `collect_workflow_logs.py:679` already handles `total_count >= 1000`. Filtering skipped runs (747 of 1000) server-side or after fetch would cut wasted run and job API pages by about 75% for the same window.

## Prompt Cache & Memory System

- `cache_hit_rate` is only populated on 3 runs: 40063935493 = 0.9367, 40057595876 = 0.8804, 40057303940 = 0.8525. The aggregate is `null`.
- Token-derived cache-read share for review_autofix is 209.03M / (16.04M + 209.03M + 4.54M) ≈ 91.0%, and cache writes are 4.54M (about 2% of input). The prefix is stable. Fragmentation is not the problem; the absolute context size is.
- Runs with `null` cache_hit_rate despite heavy cache reads (40052406186 with 30.3M cache-read, 40065164399 with 66.8M) suggest the hit-rate line is emitted only on some code paths (inference). Fix: emit `cache_hit_rate` from the same place `or_cache_read_tokens` is summed, so coverage matches.
- The 3 `CONTEXT_BUDGET_WARN` events (40052406186, 40057588130) are where prompt growth starts to erode cache value. Prior iterations accumulate. Trimming older iteration transcripts before the editor call would reduce both warnings and cache-read volume.

## Orchestrator Health

- **Pending poller runs are superseded, not failing.** 10 of 35 poll runs were cancelled at `poll / poll` → `cancelled_before_first_step` after 489–781s (40051817933, 40054604560, 40065659532, 40063782695, 40062970279). `orchestrate_poll.yml:72-73` uses `group: ai-orchestrate-poll-${{ github.repository }}` with `cancel-in-progress: false`. GitHub keeps one pending run per group and cancels older pending ones, so a poll lasting about 12 minutes guarantees supersession (inference from config and timings). This is expected behaviour, but it inflates the "cancelled" count.
  - Indicator to track: poll duration p50 compared with the cron interval.
  - Mitigation: annotate these runs in the collector as `superseded` rather than `cancelled`.
- **Clarify loop activity**: orchestrator project-8054 auto-answered clarifications (`/answer [auto-answered-by-orchestrator]`) on #8055 and #8056, then planned successfully (40069224239, 40069254028), with `AUTO_IMPLEMENT_ON_CLEAR_PLAN=true`. Run 40069049189 correctly skipped closed E2E issue #8051 (`gate=standalone reason=issue_closed outcome=skip`). No stuck states were observed.
- **Engine fallback**: `AI_ENGINE_FALLBACK_REPORT skip reason=no_records` in all 4 summarized runs, so there was no capacity fallback.
- Observable indicators to add: `RUNNER_QUEUE_WAIT`, `GH_API_BUDGET`, and poll-cycle duration per run.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| clarify | queueing + compute | 40069109767 waited 47s of 362s; real runs took 537–697s (40068691209, 40068739598, 40068692957) |
| plan | **queueing** | 40069254028 waited about 7.6 min of 481s with only seconds of step time; 40062676688 ran 1,217s |
| implement | compute + masked Semble | 40063869266: 1,765s with 4 runtime Semble fallbacks |
| review/autofix | **compute (model turns)** | p95 3,383s; 4 runs over 2,800s; 230.8M tokens |
| CI | compute + test failures | p50 895s; 3 failures caused PR rework |
| orchestrate_poll | compute + superseded pending runs | p50 712s; 10 cancelled pending runs |

Fixes ordered by end-to-end impact:
1. Review per-call context cap, plus phase timing.
2. Runner-queue visibility, then smoothing contention in the 06:09–06:20 burst.
3. CI failing-test summaries, so autofix and heal can target fixes on the first pass.
4. Bootstrapping implement's Semble correctly.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** review_autofix wall clock (p95 3,383s); runner queueing for plan/clarify (up to about 7.6 min); CI p50 895s.
- **Top failure modes:** 3 CI test failures (40052406057, 40065164413, 40066148233) with no captured root cause; 4 runtime Semble fallbacks in implement run 40063869266; 10 superseded poll runs (expected).
- **Highest-cost drivers:** review_autofix at 230.8M tokens, led by 40063935493 (76.7M) and 40065164399 (72.9M).
- **Top 3 actions:**
  1. Add `CI_TEST_FAILURE` node-ID summaries plus `-rfE` to the single-command pytest steps in ci.yml (lines 763 and 2475 first).
  2. Add per-phase `REVIEW_PHASE_TIMING` and `OR_CALL_USAGE` lines, then cap editor turns behind an env var whose default keeps current behaviour.
  3. Exclude skipped runs from the collector's 1,000-run cap, and protect telemetry lines from truncation in `log_summary`.

## Metrics Appendix

**Run counts (window: 2026-10-24 01:47Z–06:20Z)**

| Scope | Total | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Repo | 1000 | 238 | 3 | 12 | 747 | 0.3% | 1 | 902 |
| ci | 40 | 37 | 3 | 0 | 0 | 7.5% | 895 | 1754 |
| review_autofix | 47 | 43 | 0 | 1 | 3 | 0% | 480 | 3383 |
| orchestrate_poll | 35 | 25 | 0 | 10 | 0 | 0% | 712 | 1171 |
| clarify | 164 | 6 | 0 | 0 | 158 | 0% | 1 | 11 |
| plan | 153 | 7 | 0 | 0 | 146 | 0% | 1 | 11 |
| implement | 153 | 1 | 0 | 1 | 151 | 0% | 1 | 9 |
| orchestrate_clarify_respond | 155 | 1 | 0 | 0 | 154 | 0% | 1 | 10 |

**Tokens and review telemetry (all in review_autofix)**

| Metric | Value |
|---|---|
| or_calls (usage available / unavailable) | 92 (83 / 9) |
| or_prompt / completion tokens | 16,035,768 / 1,251,936 |
| or_cache_read / cache_write tokens | 209,028,970 / 4,536,001 |
| or_total_tokens | 230,846,168 |
| codex_tokens_used / claude tokens | 0 / 0 |
| cache_hit_rate (aggregate) | null; per run 0.937 (40063935493), 0.880 (40057595876), 0.853 (40057303940) |
| token-derived cache-read share | ≈91.0% |
| wall_clock_p50_ms / p99_ms (repo) | 1,000 / 4,083,520 |
| wall_clock_p50_ms / p99_ms (review_autofix) | 3,272,000 / 4,357,050 |
| break_glass_count | 0 |
| context_budget_warn_count | 3 (40052406186: 1, 40057588130: 2) |

**Semble**

| Metric | Value |
|---|---|
| query_calls / logged bytes | 8 / 123,373 (≈15.4 KB/query) |
| sources_total / static_dup_bytes | 57 / 0 |
| bootstraps (failed / unused) / total ms | 8 (0 / 0) / 340,704 ms (avg ≈42.6s) |
| fallbacks total | 28 |
| contract-test fallbacks | 24 (ci: 40052406057 = 4, 40066148233 = 8, 40065164413 = 4, 40051830786 = 4, 40051391633 = 4) |
| runtime fallbacks | 4 (implement 40063869266), 14.3% of all fallbacks, reason unavailable |

**Serena / MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | fallbacks |
|---|---|---|---|---|---|---|
| serena (all) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**: no call-count, rate-limit or 429 telemetry in this window, and no emitting prefix found in `scripts/`.

**Data gaps**
- The deep-dive log directory was absent.
- `log_summary` truncation was about 119k–158k chars.
- There is no AI_MEMORY or GH API telemetry.
- `cache_hit_rate` covers only 3 runs.
- Success sampling rate is 0.07.

---
- **Actions taken:** read-only analysis. I read the failing steps in `.github/workflows/ci.yml`, the poller concurrency config in `orchestrate_poll.yml`, the Semble fallback classification in `scripts/cost_audit.py` and the emitters in `scripts/semble_helpers.sh`, and the run-cap handling in `scripts/collect_workflow_logs.py`. No files were changed.
- **Assumptions:** "other" runs are mostly skipped runs. Superseded poll runs come from GitHub's pending-run replacement under `cancel-in-progress: false`.
- **Missing context:** step logs for the 3 CI failures, the `reason=` on the implement Semble fallbacks, and any GH API or memory telemetry.