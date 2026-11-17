## Executive Summary

- **Token and cost telemetry is missing for the whole window, and the cause is in our collector code (high confidence).** Every token counter is 0 across 114 log-parsed runs (`codex_calls=0`, `or_calls=0`, `cache_hit_rate=null`). That includes review/autofix runs of 66 minutes (43978903863, 4005 s) and implement runs of 27 minutes (43982826357, 1629 s). The wrapper workflows run on the Claude engine (`internal-*.yml`: "Claude engine: the job's OIDC token for claude-pool-broker"). The per-run telemetry built in `scripts/cost_audit.py` (`RUN_COST_TELEMETRY_FIELDS`, lines 209–244, and `AGGREGATABLE_COST_FIELDS`, lines 246–279) leaves out `CLAUDE_COUNT_KEYS` (lines 128–134) and `claude_cost_usd`. `compute_cache_hit_rate` (lines 454–465) only reads `or_*` fields. So every Claude token is parsed and then dropped. **Impact:** cost and cache sections cannot be measured until this is fixed. It is a small additive change.
- **The poller takes longer than its own schedule, so queued ticks get thrown away (high confidence).** It runs every 15 minutes (`internal-orchestrate-poll.yml:5`) but took 1431 s (43979463621) and 2643 s (43982696979), 1.6–2.9× the 900 s interval. With `cancel-in-progress: false` (`orchestrate_poll.yml:102`), 3 of 5 poller runs were cancelled before their first step after waiting 389 s, 831 s and 1217 s (43978553877, 43980755042, 43988494535). **Impact:** in practice the orchestrator ticks every 24–44 minutes, not every 15.
- **Both failures were GitHub API reads/writes at a gate step, not model failures (medium confidence; logs not mounted).**
  - 43979765801 (integration PR readiness): `check_integration_pr_readiness.py` only exits non-zero when the tracking-issue fetch fails (lines 201–207) or the commit-status POST fails (lines 140–153). Both are single `gh` calls with no retry. The fetch failure also throws away stderr (lines 79–80).
  - 43983512786 (implement): the "Precheck approval phase label" step can only fail on the `gh_api_retry` issue fetch (`implement.yml:232`) or on `jq`. The 1018 s duration fits retries running out (5 attempts, up to 600 s rate-limit wait: `gh_helpers.sh:1061`). This is an inference.
  - **Impact:** 2 of 64 non-skipped runs (3.1%).
- **93.6% of runs (934/998) are skipped wrapper runs, and they distort the duration metrics (high confidence).** The split is clarify 228, plan 224, implement 224, orchestrate_clarify_respond 225 and workflow_failure_heal 22. Every issue comment starts the wrapper workflows, which then skip at their job-level `if:`. Overall p50 is 1 s and p95 is 243 s because of these zero-work runs. **Impact:** no compute or token cost, but the latency metrics are misleading.
- **Semble is working; Serena produced no telemetry at all (high confidence).** Semble made 2 queries, 12,105 bytes, 6 sources, 0 duplicate bytes, 0 runtime fallbacks. All 8 fallbacks are `semble_contract_test_fallbacks` from CI test output (43982282517 ×4, 43980136627 ×4). Each poller tick spends about 50 s on bootstrap (46,245 ms and 54,425 ms) for one ~6 KB query. Serena shows 0 queries and 0 probes, not even `probe_skipped`.
- **Window and data limits.** All runs fall between 2026-11-16T23:20Z and 2026-11-17T00:53Z, about 93 minutes. The success sample rate is 0.07. The run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox, so no deep-dive excerpts, `GH_API_RETRY`, `AI_MEMORY_TELEMETRY` or `log_summary` lines could be checked. `insufficient_data=false`, but every per-step root cause below is an inference grounded in source code.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Measure where each poller tick spends its time (critical path for every orchestrated project).**
   - **Evidence:** poller runs of 1431 s and 2643 s against a 900 s cron, with 3 ticks superseded (see Executive Summary).
   - **Root cause:** a tick takes longer than the cron interval. GitHub keeps one pending run per concurrency group, so every queued tick after the first is cancelled. No per-phase timing is available in telemetry, so we can't tell which phase dominates.
   - **Change (diagnostic first):** emit one structured line per poller phase from `scripts/orchestrate_poll_process.sh`, e.g. `POLL_PHASE_TIMING phase=<name> tracking=<N> ms=<elapsed> gh_calls=<n>`. Register the prefix through an `agents.d/` fragment (§25). Then target the top phase. Do not change the cron or `cancel-in-progress`; queueing is the documented, intended behaviour (lines 98–101).
   - **Estimated saving:** unknown until measured. Bringing a tick under 900 s would restore the 15-minute cadence, roughly 10–30 minutes less latency per wave transition (inference).
   - **Risk:** logging only, so very low.
2. **Get a step breakdown for review/autofix (the longest active runs).**
   - **Evidence:** review_autofix p50 is 739 s, p95 3990.6 s. PR 9430 run 43978903863 took 4005 s and PR 9454 run 43980138828 took 3933 s.
   - **Root cause:** unknown. `cost_telemetry` is all zero and no step logs are mounted.
   - **Change:** make sure the collector selects one review_autofix run per window into `slow/` with step logs. It already sampled these two; the directory just wasn't mounted. Rerun this analysis with the mount present before changing anything.
   - **Estimated saving:** cannot be estimated yet.
3. **CI full versus CI quick.**
   - **Evidence:** `CI` runs took 3939 s (43982282517) and 2509 s (43980136627). `CI quick` ran 559–926 s.
   - **Change:** pull the per-job breakdown for 43982282517 from the jobs API (already fetched by the collector) before proposing sharding.
   - **Risk:** none until a change is chosen.
4. **Micro-optimization: Semble bootstrap in the poller.**
   - **Evidence:** about 50 s of bootstrap per tick for one query of about 6 KB (43982696979: 46,245 ms / 6051 B; 43979463621: 54,425 ms / 6054 B).
   - **Change:** if the index is not already reused across ticks, key it on the base commit SHA. Otherwise leave it alone; it is 1.7–3.8% of tick time.
   - **Risk:** low. Semble fails open.

## Cost Optimizations

Ranked by expected savings. Today nothing can be ranked by dollars because the Claude fields are dropped.

1. **Project Claude usage into the run telemetry. This is the prerequisite for every other cost finding.**
   - **Evidence:** all token counters are 0 across 114 parsed runs on a Claude-engine pipeline. `parse_log` does fill Claude counts (they are used at `cost_audit.py:1042–1044` and `1088–1090`), but `build_run_cost_telemetry` (lines 609–613) only copies `RUN_COST_TELEMETRY_FIELDS`.
   - **Exact change** (additive; no existing key is renamed):
     ```python
     # scripts/cost_audit.py
     RUN_COST_TELEMETRY_FIELDS = (
         ...existing fields...,
         *CLAUDE_COUNT_KEYS,
         "claude_cost_usd",
     )
     AGGREGATABLE_COST_FIELDS = (
         ...existing fields...,
         *CLAUDE_COUNT_KEYS,   # integer counts only; claude_cost_usd summed as float separately
     )

     def compute_cache_hit_rate(values):
         ...existing OR logic...
         # Fallback for the Claude engine when no OR usage is present.
         c_read = _to_int(values.get("claude_cache_read_tokens"), 0)
         c_den = (_to_int(values.get("claude_input_tokens"), 0)
                  + _to_int(values.get("claude_cache_write_tokens"), 0) + c_read)
         if denominator <= 0 and c_den > 0:
             return round(max(0.0, min(1.0, c_read / c_den)), 6)
     ```
     In `aggregate_run_cost_telemetry`, sum `claude_cost_usd` as a float. `_to_int` would truncate it.
   - **Estimated saving:** none directly. It unlocks measurement of every expensive run (4005 s, 3933 s, 1629 s, 1194 s).
   - **Quality risk:** none. Consumers ignore unknown keys.
2. **Semble value per byte is acceptable.** Two queries returned 12,105 bytes (about 3K tokens at an estimated 4 B/token) from 6 sources with 0 `static_dup_bytes` and 0 dropped echo lines. That is not noisy context. No change.
3. **Serena:** no `SERENA_QUERY` lines, so there is no replacement-versus-noise signal. See the reliability section about the missing probe lines.
4. **Skipped fan-out costs no tokens.** The 934 skipped runs end at the job `if:`. No change is needed for cost.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **Integration PR readiness: add one bounded retry and log stderr on fetch failure.**
   - **Failure evidence:** 43979765801, job `readiness`, step "Check integration PR readiness and post status", 349 s. This is 1 of 11 integration_pr_readiness runs, a 9.1% family failure rate.
   - **Root cause (API transient; inference):** the only non-zero exits are `_fetch_issue` returning `None` (lines 73–80, a single `gh issue view` with a 30 s timeout and stderr discarded) or `_post_commit_status` failing (lines 119–136, a single POST).
   - **Exact fix:**
     - In `_fetch_issue`, print `::warning::[integration-pr-readiness] tracking fetch failed rc=<rc> stderr=<first 200 chars>`.
     - Retry `_fetch_issue` and `_post_commit_status` once after a 5–10 s sleep. A commit-status POST with the same sha/context/state is idempotent.
     - Add a final structured line: `INTEGRATION_READINESS_RESULT outcome=<ready|not_ready|error> reason=<fetch_failed|status_post_failed|...> tracking=<N>`.
   - **Expected impact:** removes most single-blip failures in this family (inference).
   - **Fail-open/closed:** keep fail-closed. A failed fetch must still post `error` and exit 1 (lines 203–207).
2. **Implement "Precheck approval phase label": say why the gate failed.**
   - **Failure evidence:** 43983512786, job `implement / implement`, 1018 s.
   - **Root cause (inference):** under `set -euo pipefail`, the only fatal command before the label checks is `gh_api_retry "repos/.../issues/${ISSUE_NUMBER}"` (`implement.yml:232`), or the `jq` parsing after it. The 1018 s duration fits retry exhaustion with rate-limit waits (`GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS` default 600, `GH_RETRY_MAX_ATTEMPTS` default 5).
   - **Exact fix (diagnostic):** add `trap 'echo "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=issue_fetch_failed outcome=error issue=${ISSUE_NUMBER} last_status=${GH_API_RETRY_LAST_STATUS:-} last_kind=${GH_API_RETRY_LAST_KIND:-}"' ERR` at the top of the step. This reuses the existing `AI_PHASE_GATE_V1` prefix and the `GH_API_RETRY_LAST_*` variables (`gh_helpers.sh:1074–1075`). No new prefix is needed.
   - **Expected impact:** turns a silent failure into a classified one (rate-limit, 5xx or 404). It does not change behaviour.
   - **Rollback:** delete the trap line.
3. **Teach the collector the `GH_API_RETRY` lines.**
   - **Evidence:** `gh_api_retry` already writes `GH_API_RETRY outcome=… kind=… bucket=… status=… attempt=…/… wait_secs=… endpoint=…` to stderr (`gh_helpers.sh:1047`). `cost_audit.py` has no field for it, so retry storms never appear in reports.
   - **Fix:** add the additive counters `gh_api_retry_events`, `gh_api_retry_wait_secs_total` and `gh_api_retry_exhausted` to `parse_log` and both field tuples.
   - **Impact:** makes the GH API audit measurable.
4. **Promote main to stable was cancelled at 1827 s with an unknown cause.**
   - **Evidence:** 43987097224, job `cycle`, step "Run the promote cycle". The job's timeout is 340 minutes (`promote-main-to-stable.yml:125`), so this was not a timeout.
   - **Fix:** have the collector record the cancel actor and run `triggering_actor` for cancelled runs. Both are available from the runs API call it already makes.
   - **Impact:** separates a manual cancel from a supersede.
5. **No BREAK_GLASS or CONTEXT_BUDGET_WARN pressure:** 0 and 0 across 114 parsed runs. Note this is subject to the same parser blind spot only if those lines are emitted under the Claude engine (unverified).
6. **Semble fallbacks are healthy fail-open behaviour.** All 8 are contract-test fallbacks inside CI test output; there are 0 runtime fallbacks, 0 failed bootstraps and 0 unused bootstraps. This is not a hidden broken rollout.
7. **Serena:** 0 probe_ok, 0 probe_failed and 0 probe_skipped across 114 parsed runs. Either Serena is disabled in this repo or the probe line is not emitted. Check whether `SERENA_PROBE` is expected while `SERENA_AVAILABLE=false` (it defaults to false at `implement.yml:322`). If Serena is meant to be on, emit `SERENA_PROBE result=skipped reason=disabled`, so "disabled" can be told apart from "not instrumented".

## AI Memory Health

- **No `AI_MEMORY_TELEMETRY:` lines were available.** The run-logs directory is not mounted, and the analysis context has no `log_excerpts` or `log_summary` fields. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` cannot be assessed.
- **Recommendation:** in the next collection, confirm `AI_MEMORY_TELEMETRY` lines exist in a selected implement run (for example a sibling of 43982826357) and a review_autofix run (43978903863). If they are present, add counts for `memory_retrieve_calls`, `memory_retrieve_zero_hits` and `memory_fail_open` to `cost_audit.py` the same way as the Semble counters. That lets this section be computed instead of sampled.

## GH API Call Audit

- **Measured volume:** none. The collector exposes no GH API counters, and the `GH_API_RETRY` lines are not parsed (Reliability item 3).
- **Code-verified hotspots in the failing paths:**
  - `check_integration_pr_readiness.py`: up to 2 `gh issue view` calls per run (lines 201 and 221; the second only when the labels file is stale) plus 1 status POST. The volume is low, but each call is single-shot with no retry, so reliability matters more than batching here.
  - `implement.yml` precheck: 1 issue GET (line 232), plus 1 optional parent GET only when the smoke-title check misses and a `Tracking issue: #N` ref exists (line 277). This already follows §14 (the result is cached into `ISSUE_META_FILE`/`ISSUE_BODY_FILE` and reused). No redundant call to remove.
  - The poller has the documented batched helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`). Long ticks (1431–2643 s) could hide per-item REST calls inside loops, but this cannot be verified without logs. The `gh_calls=` field in the proposed `POLL_PHASE_TIMING` line would show it.
- **Token separation:** readiness uses `github.token` (`integration-pr-readiness.yml:80`) and implement uses `secrets.GH_PAT` (`implement.yml:216`). The two failures overlapped different long poller runs (43979463621 from 23:27:38, 43982696979 from 23:50:23). One shared rate-limit cause for both is unlikely (low confidence).
- **Expected impact:** for now, observability rather than fewer calls. Once `GH_API_RETRY` counters land, target any bucket with `wait_secs_total > 300` in a single run.

## Prompt Cache & Memory System

- **`cache_hit_rate` is `null` everywhere.** This comes from the telemetry gap, not from cache behaviour. `compute_cache_hit_rate` returns `None` when the `or_*` denominator is 0 (lines 461–462), and it never reads `claude_cache_read_tokens` or `claude_cache_write_tokens`, which `parse_claude_result_line` does extract (`CLAUDE_USAGE_FIELDS`, lines 122–127).
- **Fragmentation analysis:** cannot be done without prompt or usage data. After the Cost item 1 fix, compare `claude_cache_read_tokens / (input + cache_write + cache_read)` across review_autofix iterations of the same PR (e.g. PR 9430 and PR 9454). A ratio below about 0.5 on later iterations would point to unstable prefixes such as run IDs or timestamps placed early in the prompt (inference).
- **Context budget:** 0 `CONTEXT_BUDGET_WARN`, so prompt growth is not eroding cache value.
- **Semble as a prompt-expansion reducer:** 6 KB per query with 0 duplication is compact. With n=2, there is no evidence it is shrinking prompt size or making it worse.

## Orchestrator Health

- **Tick starvation:** see the poller finding in the Executive Summary. **Indicators to track:** the number of `cancelled_before_first_step` poller runs per day, and poller duration p50 against 900 s.
- **Wave progression and judge activity in the window:**
  - plan: 3 successes (e.g. 43980858817, 1194 s)
  - implement: 2 successes (43982826357, 1629 s; 43980046071, 683 s) and 1 failure (43983512786)
  - clarify: 3 successes (43983932610, 1117 s; 43979041260, 820 s; 43979997010, 422 s)
  - orchestrate_clarify_respond: 1 success (43986339438, 819 s)
  - unblock-judge dispatch: 3/3 successes (#9502 → 43982605587, #9418 → 43979132926, #9135 → 43987752385)
  - review/autofix sweep: 3/3 successes
- **Integration readiness:** 8/11 success and 1 failure (43979765801). The other 2 runs are counted as "other" (not success, failure or cancelled); their conclusion is not broken out. Reliability item 1 is the mitigation.
- **Comment fan-out:** in the recent_runs sample, most rows are skipped wrapper runs created between 00:48:09Z and 00:49:20Z. This fits a burst of issue comments, each starting the wrapper workflows (`internal-clarify.yml` and `internal-implement.yml` both trigger on `issue_comment: created`; plan and clarify-respond show matching counts, which is inferred). It does no harm, but these runs make up 93.6% of the run count.
  - **Smallest mitigation:** in `analyze_workflow_logs.py` / `collect_workflow_logs.py`, report p50/p95 both with and without `conclusion=skipped`, as an additive field. Do not change the triggers; `on:` cannot filter on comment body.
- **No conflict-heal or clarification-loop signals were available** (no log excerpts).

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Queueing at the orchestrator:** each wave transition waits for a 24–44 minute poller tick, and 3 ticks were superseded after queueing 389–1217 s. Fix: Speed item 1.
2. **Compute in review/autofix and full CI:** about 66 minutes each (43978903863 at 4005 s, 43980138828 at 3933 s, 43982282517 at 3939 s). These dominate the implement → merge leg. Fix: get step breakdowns (Speed items 2–3).
3. **Retry overhead at the gate:** the implement precheck consumed 1018 s before failing. The likely cause is retry and rate-limit waits (inference). Fix: Reliability item 2 for classification; then tune the per-step `GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS` only if the classification shows rate limiting.
4. **Merge/conflict:** the integration readiness gate had 1 failure in 11. Fix: Reliability item 1.
5. **Clarify and plan compute:** individual runs took 820–1194 s, but with n≈3 each, these are not yet bottlenecks.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - poller tick 1431–2643 s against a 900 s cron, with 3 superseded ticks
  - review/autofix p95 3990.6 s
  - full CI up to 3939 s
- **Top failure modes:**
  - single-shot GH API calls in the readiness gate (43979765801)
  - precheck issue fetch failing after about 1018 s (43983512786)
  - one unexplained cancel of the promote cycle (43987097224)
- **Highest-cost drivers:** unmeasurable because the Claude usage fields are dropped by `cost_audit.py`. Likely candidates by wall clock are review_autofix, implement and the poller (inference).
- **Top 3 actions:**
  1. Add `CLAUDE_COUNT_KEYS` and `claude_cost_usd` to the run and aggregate telemetry, plus a Claude fallback in `compute_cache_hit_rate`. This is additive and restores all cost and cache reporting.
  2. Add `POLL_PHASE_TIMING` lines to the poller, then fix the dominant phase.
  3. Add a bounded retry and stderr logging to `check_integration_pr_readiness.py`, and an `ERR` trap with an `AI_PHASE_GATE_V1 … outcome=error` line to the implement precheck.

## Metrics Appendix

**Run outcomes (window 2026-11-16T23:20Z – 2026-11-17T00:53Z)**

| Scope | Total | Success | Failure | Cancelled | Other/skipped | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| All | 998 | 57 | 2 | 5 | 934 | 1.0 | 243.0 |
| Non-skipped only (derived) | 64 | 57 | 2 (3.1%) | 5 | — | n/a | n/a |
| review_autofix | 5 | 5 | 0 | 0 | 0 | 739 | 3990.6 |
| ci | 9 | 9 | 0 | 0 | 0 | 831 | 3367 |
| orchestrate_poll | 5 | 2 | 0 | 3 | 0 | 1217 | 2400.6 |
| promote_main_to_stable | 1 | 0 | 0 | 1 | 0 | 1827 | 1827 |
| integration_pr_readiness | 11 | 8 | 1 | 0 | 2 | 199 | 388.5 |
| implement | 227 | 2 | 1 | 0 | 224 | 1.0 | 10.0 |
| plan | 227 | 3 | 0 | 0 | 224 | 1.0 | 10.0 |
| clarify | 231 | 3 | 0 | 0 | 228 | 1.0 | 10.0 |
| orchestrate_clarify_respond | 226 | 1 | 0 | 0 | 225 | 1.0 | 10.0 |
| security_audit | 4 | 3 | 0 | 1 | 0 | 473 | 653.65 |
| unblock_judge_dispatch | 3 | 3 | 0 | 0 | 0 | 383 | 576.5 |
| lint_pr_body_auto_close | 9 | 9 | 0 | 0 | 0 | 158 | 432.4 |

**Cost and review telemetry (114 runs with parsed logs, 108 wall-clock samples)**

| Metric | Value |
|---|---|
| codex_tokens_used / codex_calls | 0 / 0 |
| or_total_tokens / or_calls | 0 / 0 |
| Claude tokens / cost | not projected (collector gap) |
| cache_hit_rate | null |
| wall_clock_p50_ms / p99_ms | 1,000 / 3,938,580 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**Semble**

| Metric | Value |
|---|---|
| query_calls / bytes | 2 / 12,105 (6,051 in 43982696979; 6,054 in 43979463621) |
| sources_total / static_dup_bytes / echo_lines_dropped | 6 / 0 / 0 |
| bootstraps / failed / unused | 2 / 0 / 0 |
| bootstrap_ms_total | 100,670 (46,245 + 54,425) |
| fallbacks (contract-test / runtime) | 8 (8 / 0); CI 43982282517 ×4, 43980136627 ×4 |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes |
|---|---|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 | 0 | 0 |
| Semble (per-target breakdown not projected in context) | n/a | n/a | n/a | 2 | 12,105 |

Other MCP servers observed: none.

**GH API:** no counters are collected, and `GH_API_RETRY` lines are emitted but not parsed. There is no rate-limit event data.

**Data gaps:**
- `/home/runner/work/_temp/workflow-log-output` was not present, so there are no `errors/`, `slow/` or `recent/` excerpts.
- No `log_summary` fields.
- Claude usage fields are dropped by `cost_audit.py`.
- No `AI_MEMORY_TELEMETRY` was available.
- The window is about 93 minutes with a 0.07 success sample rate.