## Executive Summary

**Data gap.** `coverage.repositories_truncated` lists **shubhodeep1/coding-workflows**, and `page_limit_reached=true`, so only part of its runs are listed. The window covers about 5 hours (2026-11-21 01:54–06:45 UTC, 994 runs). The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) did not exist in this sandbox, so no deep-dive step log, `AI_MEMORY_TELEMETRY` line or failure marker could be read. Every root cause below comes from `analysis_context.json` plus the code on this ref, and is labelled where it is an inference.

- **The failures can't be attributed to a cause.** There are 44 failures (clarify 20, review_autofix 17, plan 3, ci 2, test_and_mark_stable 1, orchestrate_clarify_respond 1). Not one carries a cause, even though the code already emits a cause line for each type: `CLARIFY_ENGINE_ATTEMPT_FAILED`, `RUN_PLAN_CODEX_FAILED`, `RESOLVER_TERMINAL`, `INTEGRATION_REF_RESOLVE` and `GH_API_RETRY`. `scripts/collect_workflow_logs.py` never pulls these into the run rows. Fix: add a per-run `failure_markers` field (R1). Impact: every future report can name the dominant failure class for about 90% of failures. Confidence: high.
- **Clarify fails in 62% of the runs that actually execute.** 18 failed at `clarify / clarify` › `Run Codex`, against 9 successes, costing about 11,210 s of runner time. The failures come in bursts, for example 44870571804, 44870575054 and 44870578697 within 4 s at 05:22. Bursts point to a shared cause upstream, such as the engine or account pool (inference). Fix: R1 surfaces the existing `kind=`/`pool_reason=` fields. Confidence: high on the rate, low on the cause.
- **Claude-engine spend is dropped from every run row.** `build_run_cost_telemetry` (`scripts/cost_audit.py:609–613`) copies only `RUN_COST_TELEMETRY_FIELDS` (lines 209–245), and that list has no `claude_*` keys. As a result, all 10 parsed clarify runs and 4 review runs of 2.3–2.5 h show zero tokens (for example 44862416166 at 9,080 s and 44863133276 at 8,895 s). Fix: C1, an additive set of fields. Impact: closes the largest blind spot in cost accounting. Confidence: high on the gap, medium on how much spend is hidden.
- **The conflict resolver is the most expensive failure path.** 8 review_autofix runs failed at `review / codex-agent` › `Run Codex resolver, validate, stage, commit`, for 39,450 s (about 11 runner-hours). PRs 9396, 9701 and 9561 each failed twice. `RESOLVER_TERMINAL exit_class=` exists but is not collected (R1). Confidence: high.
- **The stable release gate failed.** Run 44862577465 (6,644 s) failed at `clarify-rejects-unsolvable-test` › `Wait for clarify to flag for clarification`. Clarify runs 44862601853 and 44862607065 started 19–23 s after the gate run and failed at `Run Codex` (link inferred from timing). The wait loop polls labels for its full 1,500 s and then prints only a generic timeout message. Fix: S1 logs the captured clarify run's outcome and stops the wait once that run has failed. Saves about 18 min per occurrence. Confidence: medium.
- **The prompt-cache hit rate dropped.** It went from 0.7346 in `analysis/workflow-optimization-2026-11-18.md` to **0.6687**. Runs with low hit rates also have the largest uncached prompts (44862769379: 0.580, 977,689 uncached prompt tokens). Fix: C2 adds a stable-prefix fingerprint to the existing `INFO: openrouter usage` line. Confidence: medium.
- **The review gate fails at `Resolve trusted review support commit` (7 runs).** A burst of 4 failures fell within 7 s at 03:32 (44861675062, 44861678112, 44861681861, 44861685091) while 6 sibling runs passed. Fix: R2 makes every API-failure exit in that step print a cause line. Confidence: medium.

## Speed Optimizations

**S1. Stable gate: stop the clarify negative-test wait once the captured clarify run has failed, and say why it stopped** (critical path for releases)
- **Evidence:** 44862577465 (`test_and_mark_stable`, 6,644 s) failed at `Wait for clarify to flag for clarification`. Clarify runs 44862601853 (360 s) and 44862607065 (404 s), created at 03:43:51 and 03:43:55, both failed at `Run Codex`. `.github/workflows/test-and-mark-stable.yml:4989` sets `DEADLINE=+1500` s. The loop (lines 5014–5074) only watches labels and comments. `clarify.yml` does not apply `ai:clarify-failed` itself (the only mention, line 2836, is a comment), so when clarify crashes the loop runs until the deadline. It then prints only "Clarify did not act … within timeout" (line 5083).
- **Root cause:** the loop ignores the conclusion of the clarify run it already captured in `steps.capture_clarify_run.outputs.run_id` (line 4948).
- **Exact change:** pass that run id into the step through `env:`. In each poll, run one `gh api repos/${TEST_REPO}/actions/runs/${CLARIFY_RUN_ID} --jq '.status+","+(.conclusion//"")'`. When the run is `completed,failure`, print `::error::STABLE_CLARIFY_NEGATIVE outcome=clarify_run_failed run_id=<id>` and `exit 1`. Keep the timeout path, and add the same line with `outcome=timeout clarify_status=<status>,<conclusion>` to it.
- **Estimated savings:** about 1,100–1,300 s per failure of this kind (1,500 s deadline against about 360–404 s of clarify runtime). It also ends the generic message, so triage no longer has to open the clarify run.
- **Risk:** low to medium. The step can only fail sooner; a clarify run that the heal process re-dispatches after a failure would no longer rescue the test. If that late-success path matters, gate the early exit behind a `vars.` toggle that defaults to true.
- Finding check: new — no entry for `Wait for clarify to flag for clarification` in `analysis/recommendation-processing-report.md`; the cause is present at `test-and-mark-stable.yml:4983–5084`.

**S2. CI `orchestrate-poll (3)`: record why each long run was cancelled** (critical path for PR CI, diagnostic)
- **Evidence:** CI runs 44860778326 (4,490 s) and 44865403277 (4,592 s) were both cancelled in `orchestrate-poll (3)` › `Pre-push affected-test gate tests`. CI p95 is 4,605.5 s against a p50 of 711 s. The `orchestrate-poll` job has `timeout-minutes: 20` (`ci.yml:3494`), and its comments record this shard repeatedly using 19.5 of its 20 minutes (lines 128 and 680).
- **Root cause:** unknown. The collector's `failure_point` stores the job and step names but no job start or end times, so a timeout can't be told apart from a newer run superseding this one, or from waiting in the runner queue (inference: whole-run durations of about 75 minutes against a 20-minute job cap suggest queue time).
- **Exact change:** in `scripts/collect_workflow_logs.py`, copy `started_at`/`completed_at` from the jobs API response the collector already fetches (`jobs_fetch_status: success`) into `failure_point`. Derive `job_elapsed_s` and `queue_s` (run `created_at` → first job `started_at`). This adds no API calls.
- **Estimated savings:** none directly. It decides whether the next step is moving tests out of shard 3 or reducing queue pressure.
- **Risk:** very low; collector-only, additive keys.
- Finding check: new — prior entries cover sharding and timeout budgets (report lines 941, 1047), but nothing covers recording cancellation or queue time in the collector; `failure_point` has only `job_name`/`step_name` on this ref.

Micro-optimizations: Semble bootstrap averages about 55.1 s (716,807 ms over 13) on review runs whose p50 is 3,564 s, under 2% of the run. No action recommended.

## Cost Optimizations

**C1. Carry Claude-engine usage into per-run and aggregate telemetry**
- **Evidence:** `parse_log` adds up `claude_calls`, `claude_*_tokens` and `claude_cost_usd` (`cost_audit.py:783`). `build_run_cost_telemetry` (lines 609–613), however, copies only `RUN_COST_TELEMETRY_FIELDS` (lines 209–245), and `aggregate_run_cost_telemetry` adds up only `AGGREGATABLE_COST_FIELDS` (lines 246–279). Neither list has any `claude_*` key. In this window, all 10 parsed clarify runs show `or_calls=0` and `codex_calls=0`, and so do review runs 44862416166, 44863133276, 44860384525, 44862400893 and 44866514207 (6,287–9,080 s each). `clarify.yml:1871–1889` runs a `claude` engine path (`CLAUDE_POOL` parsing at lines 1837–1857).
- **Root cause:** the per-run export list was never extended when the Claude parser was added.
- **Exact change:**
```python
RUN_COST_TELEMETRY_FIELDS = (
    ...existing fields...,
    "claude_calls", "claude_input_tokens", "claude_output_tokens",
    "claude_cache_write_tokens", "claude_cache_read_tokens", "claude_cost_usd",
)
AGGREGATABLE_COST_FIELDS = (
    ...existing fields...,
    *CLAUDE_COUNT_KEYS,   # integer counters only
)
# aggregate_run_cost_telemetry: sum claude_cost_usd separately as float, round(…, 6)
```
  `CLAUDE_COUNT_KEYS` is defined at line 128, before both tuples, so the reference resolves.
- **Estimated savings:** none directly. It makes visible the spend of about 15 of the 27 parsed runs, which is needed before any model or reasoning-level decision for clarify.
- **Quality risk:** none; additive keys. Consumers that look up keys by name are unaffected.
- Finding check: new — no prior verdict on exporting Claude fields in run telemetry; the cause is present at `scripts/cost_audit.py:209–245` and `609–613`.

**C2. Add a stable-prefix fingerprint to `INFO: openrouter usage`, to explain the cache-hit drop**
- **Evidence:** `cache_hit_rate` is 0.668659 (cache read 10,331,972 / (cache read + cache write 1,724,155 + uncached prompt 3,395,664)), down from 0.734551 on 2026-11-18. Per run: 44862769379 = 0.580 (977,689 uncached prompt, 440,775 written), 44862338191 = 0.596 (922,372 / 400,634), 44862451750 = 0.601, against 44862563321 = 0.784 (168,856 / 173,618) and 44862491411 = 0.771. The usage line in `scripts/review_run_reviewers.sh:625–640` logs tokens but nothing that identifies the prompt prefix.
- **Root cause:** unknown. Prefix drift between passes, cache expiry, or diff-size growth can't be told apart (inference: the low-hit runs are the ones writing the most cache, which fits a prefix that varies between calls).
- **Exact change:** append `prefix_sha=<12 hex of sha256(static prefix bytes)> prefix_bytes=<n>` to the existing line. Compute it from the prompt file the reviewer already builds. Do not add a new log prefix. Let `cost_audit.py` optionally parse it.
- **Estimated savings:** if prefix drift turns out to be the cause, bringing the low runs up to about 0.77 would save about 0.5–0.6 M uncached prompt tokens per affected run (inference from the per-run figures).
- **Quality risk:** none; diagnostic only. The hash leaks no content.
- Finding check: new — the prior entry `prompt-cache-and-semble-telemetry` (report line 552) is *deferred* ("prompt fingerprinting … not landed"), not decided; the cause is present at `review_run_reviewers.sh:625–640`.

**Semble / Serena:** 13 `SEMBLE_QUERY` calls returned 141,133 bytes (about 10.9 KB per query), with 69 sources (about 5.3 per query), `static_dup_bytes=0`, 0 echo lines dropped and 0 unused bootstraps. Semble brings in bounded, non-duplicated context and is not adding noise. Serena has 0 queries and 0 probes; it is not active, so there is nothing to judge. The 12 contract-test fallbacks seen on 2026-11-18 are gone (0 this window).

## Reliability Improvements

**R1. Collector: pull existing failure-cause lines into each failing run row**
- **Evidence:** none of the 44 failing rows carries a cause; only one row of the analysis context (44862577465 aside, none) has `log_summary`, and no failing row does. The cause lines are already emitted:
  - clarify: `CLARIFY_ENGINE_ATTEMPT_FAILED … kind= exit_code= pool_reason=` (`clarify.yml:1858`). 18 `Run Codex` failures.
  - plan: `::error::RUN_PLAN_CODEX_FAILED rc= exception=` (`plan.yml:2186`). Failures 44868671670, 44865830367 and 44871130391.
  - resolver: `RESOLVER_TERMINAL … attempts= attempt_ms= exit_class= rc= fp_violations= markers=` (`review_conflict_resolve.sh:532`). 8 failures, with PRs 9396, 9701 and 9561 each failing twice.
  - integration ref: `INTEGRATION_REF_RESOLVE outcome=` (`clarify.yml:174–356`). Failures 44868676905 and 44863811687.
  - API retries: `GH_API_RETRY outcome= kind= status=` (`gh_helpers.sh:1584`). Gate failures.

  A search of `collect_workflow_logs.py` and `cost_audit.py` for `RESOLVER_TERMINAL`, `ENGINE_ATTEMPT_FAILED`, `GH_API_RETRY outcome` and `INTEGRATION_REF_RESOLVE` finds nothing.
- **Root cause category:** observability gap in the collector, not in runtime logging.
- **Exact fix:** in the collector's per-run log pass (where `cost_telemetry` is built), regex-extract the *last* occurrence of each of those five prefixes, plus counts of `AI_MEMORY_TELEMETRY` retrieve operations (`records_selected`, `fail_open`, `enabled`). Store them as `failure_markers: {prefix: {field: enum/int}}`. Keep only enum and integer fields, the same way `RESOLVER_TERMINAL` is already restricted.
- **Expected impact:** about 40 of 44 failures get a cause class in the next report. It also turns the rejected "same-head resolver hold" (report line 1882, which "needs evidence from the reason codes first") into a decision backed by data.
- **Fail-open:** if extraction fails, the field is left out and the row is still written.
- Finding check: new — the emitting side was applied (`RESOLVER_TERMINAL`, report line 2153; `INTEGRATION_REF_RESOLVE`, line 1868), but collector aggregation has no verdict and is absent on this ref.

**R2. `Resolve trusted review support commit`: print a cause line on every API-failure exit**
- **Evidence:** 7 `review / gate` failures: 44865838446 (PR 9813, 2,524 s), 44870819718 (2,438 s), 44861685091 (PR 9367), 44861681861 (PR 9562), 44861678112 (PR 9722), 44861675062 (PR 9731) and 44858386735 (225 s). Four of them fall within 7 s at 03:32:45–52, while 44861654665, 44861663959, 44861666523, 44861669488, 44861672299 and 44861688108, created between 03:32:30 and 03:32:54, succeeded. PRs 9562 and 9722 later passed (44876281366, 44876276085). In `review_autofix.yml:511`, `515` and `527`, `support_*_json="$(gh_api_retry …)"` runs under `set -euo pipefail`. A failed read therefore exits the step without an `::error::`, while the identity, tag and protection branches (lines 481, 520, 530, 536) each print one.
- **Root cause category:** likely a transient or secondary rate limit under a burst of runs started at the same moment (inference). The step itself has no cause line for this exit.
- **Exact fix:** wrap each read as `if ! support_main_json="$(gh_api_retry …)"; then rc=$?; echo "::error::REVIEW_SUPPORT_RESOLVE outcome=api_failed source=main rc=${rc}"; exit 1; fi`, with the same pattern for `source=stable_tag` and `stable_tag_object`. `rc` 1, 2 and 75 already map to transient, permanent and rate-limited (`gh_helpers.sh:1575–1580`).
- **Expected impact:** diagnostic. It separates rate-limit failures (where spreading out the burst helps) from permanent failures (identity or protection).
- **Rollback:** the step still fails closed with the same exit code, so security behaviour is unchanged.
- Finding check: recurring — the prior verdict "Reliability 3 … trusted support commit retry" was `already-implemented` (report line 1910), and this window still shows 7 failures in the step.

Other signals: `BREAK_GLASS` 0, `CONTEXT_BUDGET_WARN` 0, `SEMBLE_FALLBACK` 0 (contract-test 0, runtime 0), `SERENA_FALLBACK`/`SERENA_PROBE` 0. There is no policy or prompt-size pressure, and no masked rollout.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines could be checked. The run-logs directory was not mounted, and the analysis context carries no memory fields. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` counts, and push retries are all **unknown** for this window. Recommendation: collect the retrieve summary through R1's `failure_markers` extension (`records_selected>0` ratio, `fail_open` count, `enabled` flag), so the next report does not depend on mounting deep-dive logs.
Finding check: new — the emitting side exists (`push_attempts`/`push_ms`, report line 1875), but nothing collects it on this ref (same change as R1).

## GH API Call Audit

GitHub API counters (`GH_PAT_BUDGET`, `GH_API_RETRY`) are not in the analysis context, so per-step call counts can't be measured. The only rate-limit-like evidence is the gate burst described in R2.

**G1. Stable negative-test wait loop: two unretried REST reads every 15 s**
- **Evidence:** `test-and-mark-stable.yml:5015` (issue labels) and `5028` (comments, `per_page=20`) both run every 15 s for up to 1,500 s, which is about 200 calls when the wait times out, as it did in 44862577465.
- **Exact change:** read comments only every second poll, or only when the labels are unchanged since the last poll. Detecting a comment-only rejection would take at most 15 s longer. Together with S1's early exit this removes most of the loop.
- **Estimated reduction:** about 100 calls per timed-out negative test, more when S1 stops the wait early.
- **Rate-limit risk reduction:** small but on the release path.
- Finding check: new — no prior entry for this loop; the cause is present at lines 5014–5073.

API hygiene note: the gate's `Record GH_PAT budget at review gate start` reads `rate_limit`, which does not count against the core limit, and the support read uses `gh_api_retry`, which complies with §14. No new batching opportunity can be shown without counters.

## Prompt Cache & Memory System

- **`cache_hit_rate` = 0.668659** over 44 OpenRouter calls in 6 review runs (all 44 had usage available). Writes are 11.2% of cacheable input. Per run the rate ranges from 0.5803 to 0.7837 (table in the appendix).
- **Fragmentation signal:** the three runs at about 0.58–0.60 had about 4–6× the uncached prompt tokens and about 2× the cache writes of the runs at about 0.77–0.78. That fits a prefix that varies between passes, or large changing blocks placed ahead of stable content (inference; the prompts can't be inspected without logs).
- **Recommendation:** C2's prefix fingerprint on the existing usage line. Then, if `prefix_sha` varies within a run, look at what changes between passes.
- `CONTEXT_BUDGET_WARN` = 0, so prompt growth is not yet pressing on the context window.
- Memory retrieval effectiveness: unknown (see AI Memory Health).
Finding check: new — same item as C2; prior `prompt-cache-and-semble-telemetry` deferred (report line 552); `prompt-prefix reordering` was rejected (line 1886) and is not raised again.

## Orchestrator Health

- **Poller:** 27 runs: 12 succeeded and 15 were cancelled, for example 44875747176 (24 s) and 44875774748 (660 s), both `cancelled_before_first_step`. This is consistent with healthy coalescing of pending runs under the concurrency group, so no action is needed. Indicator to track: cancellations after the first step has started, which would mean real work being lost.
- **Clarify-respond:** 1 failure, 44853628040 at `respond / respond` › `Check orchestrator metadata` (49 s), with no cause line collected (R1).
- **Stuck signals:** resolver failures repeat on the same PRs (9396, 9701, 9561, twice each, between 02:01 and 05:00). This feeds the integration judge (`review_conflict_resolve.sh` EXIT-trap dispatch, `review_autofix.yml:4455–4458`). Indicator: `RESOLVER_TERMINAL exit_class` per PR once R1 lands.

**O1. Event fan-out: about 416 of 994 runs (42%) are callers that end `skipped`**
- **Evidence:** clarify 95, plan 103, implement 111 and orchestrate_clarify_respond 107 runs in "other". They fire as a set of four for each issue comment, for example 44877081011, 44877080949, 44877080942 and 44877080988 at 06:45:47, and similar sets at 06:44:31, 06:43:07, 06:42:33, 06:36:42, 06:36:17 and 06:36:02. `internal-clarify.yml` triggers on every `issue_comment: created` and filters only in the job-level `if:` (lines 17–24). Under §26, skipped runs still flood the queue and the API.
- **Smallest safe change:** `issue_comment` can't be filtered by comment body at the trigger. The remaining option is the single comment-router caller already proposed. Keep each reusable workflow and its job id, and dispatch only the matching one. This keeps the security `if:` checks.
- **Expected impact:** about 3 of 4 runs removed per issue comment, roughly 300 runs per 5-hour window.
- **Risk:** medium, because this is orchestration-wide work.
- Finding check: new — prior `event-fanout-reduction` (report line 551) and `control-plane dispatch / no-op reduction` (line 600) are *deferred*, not decided; the cause is present at `internal-clarify.yml:3–24`.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact:
1. **Review/autofix (compute plus retries):** p50 3,564 s, p95 8,171 s. 8 resolver failures cost 39,450 s, and every run that hits a conflict goes through a resolver budget of up to 3 × 50 min (`review_autofix.yml:4439–4447`). Next step: R1, to rank `exit_class` (timeout, no_progress, scope and so on) before tuning any budget.
2. **Clarify (failed compute):** 18 failures, 11,210 s, 62% failure rate among executed runs. The bursts suggest a shared cause upstream; R1 exposes `pool_reason`.
3. **Release gate (waiting):** a 1,500 s blind wait after clarify has already failed (S1).
4. **Queueing (inference):** gate-only failures lasting 1,459–2,524 s, and CI runs of 75 minutes cancelled inside a 20-minute job, suggest heavy runner queueing during the 03:20–03:50 burst, when about 20 review runs were created. S2's `queue_s` field would confirm it.
5. **Merge/conflict overhead:** see item 1. Plan failures (3 of 9 executed) are a smaller contributor.

Each item above maps to an already-checked recommendation: R1, S1, S2 and O1.
Finding check: new — the items are the R1/S1/S2 entries above, and no extra recommendation is introduced here.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** review_autofix wall clock (p50 3,564 s, p95 8,171 s); resolver retries; the stable gate's blind wait.
- **Top failure modes:** clarify `Run Codex` (18); resolver step (8); `Resolve trusted review support commit` (7); plan `Run Codex planning` (3); CI `YAML lint` (2: 44863133183, 44853576583).
- **Highest-cost drivers:** review reviewers, at about 2.6 M tokens per run with OpenRouter telemetry. Claude-engine and OpenCode resolver spend is not measured.
- **Top 3 prioritized actions:**
  1. R1, collector `failure_markers`.
     Finding check: new — collector aggregation absent; emitters applied (report lines 1868, 2153).
  2. C1, export the `claude_*` fields.
     Finding check: new — `cost_audit.py:209–245` omits them.
  3. S1, stable-gate clarify outcome check.
     Finding check: new — `test-and-mark-stable.yml:4983–5084`.

## Metrics Appendix

**Runs (truncated window, success sample rate 0.07)**

| Scope | Total | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Repo | 994 | 408 | 44 | 19 | 523 | 4.4% (9.3% excl. skipped) | 10 | 5,137 |
| review_autofix | 137 | 116 | 17 | 2 | 2 | 12.4% | 3,564 | 8,171 |
| clarify | 124 | 9 | 20 | 0 | 95 | 16.1% (69% of executed) | 1 | 803 |
| plan | 112 | 6 | 3 | 0 | 103 | 2.7% | 1 | 203 |
| ci | 35 | 31 | 2 | 2 | 0 | 5.7% | 711 | 4,606 |
| orchestrate_poll | 27 | 12 | 0 | 15 | 0 | 0% | 788 | 2,329 |
| test_and_mark_stable | 1 | 0 | 1 | 0 | 0 | 100% | 6,644 | 6,644 |

**Failure points**

| Step | Count | Runner-seconds |
|---|---|---|
| clarify › Run Codex | 18 | 11,210 |
| review codex-agent › Run Codex resolver, validate, stage, commit | 8 | 39,450 |
| review gate › Resolve trusted review support commit | 7 | 11,907 |
| plan › Run Codex planning | 3 | 2,972 |
| clarify › Resolve integration ref | 2 | 2,029 |
| quick › YAML lint | 2 | 187 |
| review_autofix_sweep.yml (zero jobs) | 2 | 0 |
| clarify-rejects-unsolvable-test › Wait for clarify… | 1 | 6,644 |
| respond › Check orchestrator metadata | 1 | 49 |

Note: each zero-job `review_autofix_sweep.yml` failure (44853571858 at 02:01:51, 44863127734 at 03:50:17) lands 3–4 s before a `YAML lint` failure. This looks like a workflow file that GitHub could not parse, which CI caught and blocked as intended. No action.

**Token, cache and review telemetry (27 runs with parsed logs)**

| Metric | Value |
|---|---|
| `or_calls` (available / unavailable) | 44 (44 / 0) |
| Prompt / completion / total tokens | 3,395,664 / 216,226 / 15,667,081 |
| Cache read / write tokens | 10,331,972 / 1,724,155 |
| `cache_hit_rate` | 0.668659 (2026-11-18: 0.734551) |
| Per-run hit rate | 44862563321 0.7837 · 44862491411 0.7710 · 44862322686 0.7119 · 44862451750 0.6013 · 44862338191 0.5964 · 44862769379 0.5803 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| Claude usage | not exported (C1) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo) | 2,438,000 / 9,400,420 (27 samples) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix) | 8,218,500 / 9,456,710 (14 samples, biased toward slow runs) |
| clarify / plan wall clock p50 · p99 ms | 559,500 · 1,477,350 / 1,106,500 · 1,668,530 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 13 / 141,133 (about 10.9 KB per query) |
| `semble_sources_total` / `static_dup_bytes` / `echo_lines_dropped` | 69 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 13 / 0 / 0 / 716,807 (about 55.1 s each) |
| `semble_fallbacks` (contract-test / runtime) | 0 (0 / 0) |
| Serena query / fallback / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API:** no call counts or rate-limit events in the analysis context. Indirect signal: the 4-run gate failure burst at 03:32.

**Findings check**

| Status | Count |
|---|---|
| new | 7 (S1, S2, C1, C2, R1, G1, O1) |
| recurring | 1 (R2) |
| reopened | 0 |
| unverified | 0 |
| dropped: already fixed on this ref | 1 (clarify `Resolve integration ref` failures: the `INTEGRATION_REF_RESOLVE` fail-closed behaviour was applied, report line 1868) |
| dropped: previously decided | 3 (resolver usage logging, rejected at line 1885; prompt-prefix reordering, rejected at line 1886; same-head resolver hold, rejected at line 1882) |

**Data gaps**
- The repo is truncated (`page_limit_reached`).
- The run-logs directory is absent from the sandbox, so no step logs, `AI_MEMORY_TELEMETRY`, `GH_API_RETRY` or `RESOLVER_TERMINAL` lines could be read.
- No `log_summary` on any failing row.
- Claude and OpenCode-resolver token usage is not exported.
- There is no queue-time field.
- `analysis/recommendation-processing-report.md` was readable.