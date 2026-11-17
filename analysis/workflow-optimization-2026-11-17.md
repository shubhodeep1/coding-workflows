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
## Deep Audit — Workflows & Scripts (2026-11-17)

Scope: this pass covers a subset of files in depth rather than every file lightly. Files read in full or in their relevant ranges:

- **Workflows:** `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`, `review_rb_judge_dispatch.yml`, `unblock_judge.yml`, `sync_ai_labels.yml`, `internal-check-failure-triage.yml`, `cancel_on_pr_close.yml`, plus targeted ranges of `orchestrate_poll.yml`, `implement.yml`, `workflow-log-analysis.yml` and `review_autofix_sweep.yml`.
- **Scripts:** `check_integration_pr_readiness.py`, `label_helpers.sh`, and targeted ranges of `gh_helpers.sh`, `orchestrate_poll_process.sh` and `implement_diagnose_post_codex_failure.sh`.

The report sections above this one (Executive Summary through Metrics Appendix) cover the run-telemetry findings: the `cost_audit.py` Claude gap, the readiness and precheck retries, and `POLL_PHASE_TIMING`. Those are not repeated here; findings that touch the same code cross-reference them.

### Section 1: Bug & Correctness Sweep

**SEC-001 — Required merge gates run the checker script from the PR under test**
- **File:** `.github/workflows/integration-pr-readiness.yml:62-65,78-90`; `.github/workflows/lint-pr-body-auto-close.yml:49-56,100-115`; `.github/ai/write_guards.v1.json:4-21,32-49`
- **Severity:** Medium
- **Category:** `security`
- **Description:**
  - Both workflows trigger on `pull_request` and use `actions/checkout@v5` with no `ref:`, so they check out the PR merge commit. They then run `scripts/check_integration_pr_readiness.py` and `scripts/lint_pr_body_auto_close.py` from that checkout.
  - In the readiness workflow, that script posts the required status `orchestrator/integration-pr-not-ready` with `statuses: write`.
  - So a PR that edits either script decides its own gate result.
  - The write guards for the `implement` and `review_editor` phases allow `**` and block `.github/workflows/**` only when `ALLOW_WORKFLOW_EDITS` is not `true`. `scripts/**` is never guarded.
  - Inference: in a repo with `ALLOW_WORKFLOW_EDITS=false`, an AI sub-issue PR that is merged into `orchestrator/project-<N>` can still change the readiness script that gates the integration PR.
  - `unblock_judge.yml:44-119` already shows the trusted-support pattern: resolve the SHA, check out into a separate path, then verify the SHA.
  - [NEEDS VERIFICATION]: whether CODEOWNERS or branch protection requires human review of `scripts/` changes on integration branches.
- **Recommended fix:**
  - Add a second checkout of the base commit into its own path: `ref: ${{ github.event.pull_request.base.sha }}`, `path: .gate-src`, `persist-credentials: false`.
  - Run the checker from `.gate-src/scripts/...`.
  - The PR's own copy is then validated only by CI tests, never by the gate.

**BUG-001 — Two writers post the readiness status with no ordering, so a stale `failure` can be posted last**
- **File:** `.github/workflows/integration-pr-readiness.yml:41-60`; `scripts/check_integration_pr_readiness.py:200-214,245-287`; `scripts/orchestrate_poll_process.sh:4757-4790`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The workflow job has no `concurrency:`. It fetches the tracking body (`_fetch_issue`, lines 200-214) and later posts the status.
  - The poller also posts the same context on the same head SHA (`python3 scripts/check_integration_pr_readiness.py … --tracking-body-file`, line 4774).
  - The poller re-posts only when `desired_hash != last_refresh_hash` (line 4759), and latches `tracking_body_last_readiness_refresh_hash` after a successful post (lines 4781-4784).
  - Interleaving (inference):
    1. The workflow, triggered by a push, reads the old body with one box unchecked.
    2. The poller ticks the last box and posts `success`.
    3. The workflow then posts `failure` from its stale read.
    4. The poller never re-posts, because the hash is already latched.
  - The required check stays red until the next push or override-label event. This fails closed, so it is a stall, not a bypass.
- **Recommended fix:**
  - Re-read the body immediately before posting, and post only if the unchecked count is unchanged. One extra `gh issue view`, only on the not-ready path.
  - Or have the poller skip the hash latch while any `integration-pr-readiness.yml` run for that SHA is in progress.
  - Or, simplest: the poller re-asserts `success` every tick while the rendered body has zero unchecked boxes, and compares against the live status instead of a hash latch.

**BUG-002 — Partial fix-up issue creation orphans the issues already created, and a re-run duplicates them**
- **File:** `scripts/implement_diagnose_post_codex_failure.sh:806-858`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The loop creates up to 10 fix-up issues with `gh_retry gh issue create` (lines 830-835), labelled `ai:clarification` and `ai:implement-fix-up`.
  - If any create fails, it sets `creation_failed=1`. It then posts only a generic failure comment that does not list the issues already in `CREATED_FIX_ISSUES_JSON` (lines 852-857), and exits 1.
  - The summary comment with `IMPLEMENT_FIXUP_BLOCKERS_V1` is skipped. That summary is what the poller parses for blockers (`orchestrate_poll_process.sh:7763` matches `## Post-Codex validation diagnosed follow-up fixes`).
  - So the issues already created run through the pipeline untracked as blockers.
  - No idempotency marker is written into the issue body, so a re-diagnosis creates the same fixes again.
- **Recommended fix:**
  - On partial failure, still post the summary block with the created numbers, or close the created issues with `state_reason=not_planned`.
  - Add a hidden body marker `<!-- implement-fixup:v1 source=<ISSUE_NUMBER> id=<FIX_ID> run=<GITHUB_RUN_ID> -->`.
  - Before each create, check for it with one `gh issue list --label ai:implement-fix-up --search "<marker>"` call per diagnose run (not per fix).

**BUG-003 — The per-issue label fallback caches `[]` on failure, and its unpaginated read can return a partial list; reconcile then treats either as truth**
- **File:** `scripts/orchestrate_poll_process.sh:30788-30804,30866-30867`; contract at `4811-4816`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - When the GraphQL label batch misses an issue, the fallback runs `gh_retry _safe_gh_jq "repos/.../issues/${inum}/labels" --jq '[.[].name]' || echo '[]'` (line 30801) and stores the result in `LABELS_JSON`.
  - Two problems:
    - A failed read is stored as "no labels".
    - The REST call has no `--paginate`, so it returns at most the default 30 labels.
  - `_fetch_issue_labels_batch_graphql`'s own contract (lines 4813-4816) says a partial list "cannot be used as evidence" and that misses must stay misses.
  - The value then feeds `reconcile_managed_issue_labels "${inum}" "${BEFORE_LABELS}" …` (line 30867), which computes label repairs from it.
  - `check_integration_pr_readiness.py:217-220` documents the symptom: "a failed label read cached as [] and then extended by another label write".
  - [NEEDS VERIFICATION]: which repair the empty set triggers for each phase.
- **Recommended fix:**
  - On fallback failure, `continue` without inserting the key.
  - Change the read to `--paginate` plus `jq -s 'add // []'`.
  - In the reconcile loop, skip issues with no `LABELS_JSON` key (`jq -e 'has($key)'`) instead of defaulting to `[]` at line 30866.

**BUG-004 — orchestrate_poll.yml resolves `SCRIPT_REF` twice per job, so the gh helper and the support files can come from different commits**
- **File:** `.github/workflows/orchestrate_poll.yml:147-188,237-251,572-614`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The first resolution (lines 147-188) writes `SCRIPT_REF` and stages `scripts/gh_helpers.sh` from it (lines 237-251).
  - A second, byte-identical resolution (lines 572-614) re-reads `branches/promote-candidate` and overwrites `SCRIPT_REF` before the main support checkout (line 616).
  - On the promote-candidate path, if the branch tip moves between the two steps, the gh helper and the rest of the support tree come from different SHAs.
  - Both steps also use `GH_API_RETRY_HELPERS`, which this job never sets (DEAD-001). The read is therefore a single raw `gh api`, and on a transient error it silently falls back to `github.sha` with the misleading log `SUPPORT_REF source=promote_candidate_unprotected` (lines 172 and 598).
- **Recommended fix:**
  - Delete the second resolution body. Keep the step name and id only if referenced, and have it re-emit the existing `SCRIPT_REF`.
  - In the first step, log `source=promote_candidate_read_failed` separately from `unprotected` when the read itself fails.

**BUG-005 — The implement phase-label transition swallows failures**
- **File:** `.github/workflows/implement.yml:2412-2415`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - `gh_retry gh issue edit … --add-label 'ai:implementing' --remove-label 'ai:awaiting-approval' >/dev/null 2>&1 || true` discards all output and errors.
  - `RESTORE_AWAITING_APPROVAL_ON_SKIP=true` is then set unconditionally.
  - If the edit fails, the issue stays in `ai:awaiting-approval` while implementation runs. The orchestrator's view diverges, and nothing records the failure.
  - Duplicate dispatch is mitigated by the per-issue concurrency group (lines 147-149) and the `/approved` claim (lines 2417-2429).
- **Recommended fix:**
  - Use the existing `set_issue_phase_label_resilient "${ISSUE_NUMBER}" ai:implementing` (`label_helpers.sh:227-316`). It only adds the target and deletes stale phase labels; it never replaces the label set.
  - Log `AI_PHASE_GATE_V1 … reason=phase_label_${SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME}` when the outcome is not `applied`.

**BUG-006 — A deep-audit output with malformed headings is not retried, even with attempts left**
- **File:** `.github/workflows/workflow-log-analysis.yml:2145-2198,2200-2228`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The retry loop breaks on any non-empty output (line 2180).
  - Heading and order validation runs only after the loop (lines 2200-2228) and exits 1.
  - So one malformed but non-empty response fails the job even when `MAX_CODEX_ATTEMPTS` (default 3) has attempts left.
  - [NEEDS VERIFICATION]: the API-redundancy step (from line 2596) appears to follow the same structure.
- **Recommended fix:** move the heading checks into a function `wla_audit_output_valid` and use `[ "${AUDIT_EXIT}" -eq 0 ] && wla_audit_output_valid` as the loop's success condition. A malformed result then consumes one attempt with backoff.

**BUG-007 — `set_issue_phase_label_resilient` relies on the caller's `pipefail` to notice a failed label GET**
- **File:** `scripts/label_helpers.sh:247-258`
- **Severity:** Low
- **Category:** `bug`
- **Description:**
  - The code is `if ! _cur="$(gh_retry gh api --paginate … 2>/dev/null | jq -cs 'add // []')"`.
  - Without `pipefail` in the sourcing shell, the pipeline's status is `jq`'s. A failed GET then yields `[]` and the "GET failed → POST fallback" branch never runs.
  - The target label is still added, but stale phase labels are never removed, leaving two phase labels on the issue.
  - Callers that `set -euo pipefail` are safe. GitHub's default `bash -e {0}` shell does not set `pipefail`.
  - [NEEDS VERIFICATION]: whether any caller sources this without `pipefail`.
- **Recommended fix:** capture first and filter second: `_raw="$(gh_retry gh api --paginate … --jq '[.[].name]')" || { …fallback…; }`, then `_cur="$(printf '%s' "${_raw}" | jq -cs 'add // []')"`.

No direct shell injection was found. Every `github.event.{issue,comment,pull_request}.{title,body,head.ref}` reference in `.github/workflows/` goes through `env:`; the only inline `inputs.*` in a run body is the boolean at `sync_ai_labels.yml:125` (CONSIST-003). `pull_request_target` appears only in comments of `workflow_integrity.yml`. `internal-check-failure-triage.yml`'s `workflow_run` jobs pass event fields only as reusable-workflow `with:` inputs, and the callee serialises per PR and check (`check_failure_triage.yml:227-235`).

### Section 2: GitHub API Call Redundancy Audit

**API-001 — The current wave's issues are fetched by two GraphQL batches per tick, and both return labels**
- **File:** `scripts/orchestrate_poll_process.sh:30779-30791`; query fields at `23060` and `23119-23120`; helper at `4817-4885`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:**
  - `_fetch_candidate_issue_details_graphql "${_wave_issue_nums_json}"` (line 30783) already selects `labels(first: 100) { nodes { name } pageInfo { hasNextPage } }`. It returns `labels` and `labels_complete` per issue (lines 23119-23120).
  - The next line, 30788, runs `_fetch_issue_labels_batch_graphql` over the same list.
  - The comment at lines 30779-30782 says the split is deliberate, "so the two cache paths remain independently fail-open".
- **Call count:**
  - Current: `2 × ceil(n/25)` GraphQL calls per tracking project per tick.
  - Projected: `ceil(n/25)`, plus one label batch only for issues missing from the details cache or with `labels_complete == false`.
- **Recommended fix:**
  - Seed `LABELS_JSON` from `_current_wave_details_json | with_entries(select(.value.labels_complete == true) | .value = .value.labels)`.
  - Call `_fetch_issue_labels_batch_graphql` only with the remaining keys. This keeps independent fail-open: a failed details batch still falls through to the label batch.
  - Extend the existing `_fetch_candidate_issue_details_graphql` (`cache=_candidate_details_json`, agents.md "Repo-specific batching helpers").

**BATCH-001 — Fix-up blocker states are read with one REST call per blocker inside loops**
- **File:** `scripts/orchestrate_poll_process.sh:7778-7795,33425-33446`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:**
  - Both loops run `while IFS= read -r blocker_issue; do … gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${blocker_issue}" --jq '.state'` over a JSON array of blocker numbers.
  - The poller rules list this kind of per-iteration call as a review blocker.
  - Blockers come from the diagnose summary, which is capped at 10 per source issue (`implement_diagnose_post_codex_failure.sh:800-804`). Both loops can run for several issues in one tick.
- **Call count:** currently N REST calls per affected issue (N ≤ 10); projected 1 aliased GraphQL call per issue, or 1 per tick if blockers are collected first.
- **Recommended fix:** add `_fetch_issue_states_batch_graphql <numbers_json>`, modelled on `_fetch_issue_labels_batch_graphql` (lines 4817-4885): batches of 25, `iN: issue(number: N) { number state }`, misses omitted so callers keep their existing `unknown` branch. Replace both loops (see DUP-005).

**API-002 — `branches/promote-candidate` is read twice in one poll job**
- **File:** `.github/workflows/orchestrate_poll.yml:166,592`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** the same `gh_api_retry repos/shubhodeep1/coding-workflows/branches/promote-candidate` read runs in two steps of the `poll` job whenever the workflow ref is `promote-candidate`. The comment above each call ("no earlier call in this step returns…") checks only the current step, not the earlier one in the same job.
- **Call count:** currently 2 per tick on that path; projected 1.
- **Recommended fix:** reuse `SCRIPT_REF` from `$GITHUB_ENV` in the second step (BUG-004).

Reviewed and judged acceptable, no finding:
- `replay_failed_reclarify_commands` (`orchestrate_poll_process.sh:189-229`): one issue read and one comments read per queued issue, documented and budget-gated at lines 184-192; the replay needs the complete comment history.
- `cancel_on_pr_close.yml:371-387`: one cancel POST per run; GitHub has no batch cancel.
- `orchestrate_poll_process.sh:31720` and `32415`: live reads on mutually exclusive branches before a label mutation.
- `implement.yml:2329`: an intentional pre-advance recheck, commented at lines 2327-2328.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The "Resolve workflow support ref" body is copied 16 times across 9 workflows**
- **File:**
  - `implement.yml:478,783,1362,7299`
  - `orchestrate_poll.yml:155,581`
  - `clarify.yml` ×2, `plan.yml` ×2, `orchestrate.yml` ×2
  - `check_failure_triage.yml`, `cancel_on_pr_close.yml`, `orchestrate_clarify_respond.yml`, `workflow_failure_heal.yml`
  - Each copy anchors on `promote_candidate_support_ref="${{ github.sha }}"`; a reduced variant is at `sync_ai_labels.yml:37-57`.
- **Severity:** Medium
- **Category:** `duplication`
- **Description:**
  - Security-relevant pin logic is copy-pasted: the promote-candidate protection check, the consumer 40-hex pin regex and the `stable` fallback.
  - Each copy differs only in the workflow filename embedded in the regex.
  - Drift between copies changes which commit's code runs with `GH_PAT`.
  - These steps run before any support checkout, so a `scripts/` file cannot hold the logic.
- **Recommended fix:**
  - Create a composite action `.github/actions/resolve-support-ref/action.yml` with:
    - inputs `workflow_file`, `gh_token`
    - output `script_ref`
    - the same `SUPPORT_REF …` log lines
  - Reference it by commit SHA, following the `install-codex@91b30f6…` pinning precedent (agents.md "Immutable consumer wrapper pins").
  - Update all 16 call sites and `tests/test_workflow_support_ref_follows_pin.py` in the same PR.

**DUP-002 — Three near-identical retry loops in gh_helpers.sh**
- **File:** `scripts/gh_helpers.sh:1208-1301` (`gh_retry`), `1312-1385` (`gh_retry_to_file`), `1434-` (`gh_api_json_to_file`)
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - The three functions repeat the same steps:
    - `--idempotent` parsing
    - `_gh_cmd_is_unsafe_post` single-attempt gating
    - mktemp handling
    - permanent → rate-limit → backoff classification
    - final-failure annotation
  - They have already diverged on output delivery: `gh_retry` replays with builtin `printf` for issues #9225/#9227, while `gh_retry_to_file` uses `cat` (line 1339).
- **Recommended fix:**
  - Extract `_gh_retry_core <mode:stdout|file|json-file> <outfile|-> <idem> <cmd…>`.
  - Keep the three public names as thin wrappers, so no identifiers are renamed.
  - Cover the change with the existing `gh_retry` contract tests.

**DUP-003 — The Codex CLI tool-cache trio (cache, persist, restore) is copied 5 times**
- **File:** `workflow-log-analysis.yml:310-347,924-971,1818-1865,2442-2489`; `plan.yml:127,145,158`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** each copy has the same `actions/cache@v5` key, the same `cp -r "${NPM_GLOBAL_ROOT}/@openai/codex…"` persist body and the same restore and symlink body.
- **Recommended fix:**
  - Add a `use_tool_cache` input to `.github/actions/install-codex`, or add `scripts/codex_tool_cache.sh {persist|restore}` that uses `${RUNNER_TOOL_CACHE}` instead of `${{ runner.tool_cache }}`.
  - Keep `actions/cache` in the workflow so cache keys stay visible.

**DUP-004 — Three copies of the phase-failure emitter and label helpers in workflow-log-analysis.yml**
- **File:** `workflow-log-analysis.yml:1115-1116,1137-1230` (`emit_log_analysis_phase_failure`), `1987-1988,2000-2076` (`emit_deep_audit_failure`), `2604-2605,2617-2693` (`emit_api_redundancy_failure`)
- **Severity:** Low
- **Category:** `duplication`
- **Description:**
  - Each copy builds the same `AI_PHASE_FAILURE_V1` payload with `jq -cn`, posts the comment, emits the event, and creates and applies `ai:log-analysis-failed`.
  - Each redefines the one-line `_wla_label_retry` and `_wla_label_create` helpers.
- **Recommended fix:**
  - Add `scripts/wla_phase_failure.sh` exporting `wla_emit_phase_failure <phase> <step> <mode> <attempts> <summary>`. It reads `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW` and `GITHUB_SERVER_URL` from the environment.
  - It should use `ensure_label_exists` from `label_helpers.sh` (this also fixes CONSIST-001).
  - Source it in the three steps. This also shrinks EXPR-002 and EXPR-003.

**DUP-005 — Blocker-state loop copied twice**
- **File:** `scripts/orchestrate_poll_process.sh:7778-7795,33425-33446`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** the two blocks differ only in variable names (`blocker_open_count` vs `IF_BLOCKER_OPEN_COUNT`, etc.).
- **Recommended fix:** add `_summarize_blocker_states <blockers_json>` that prints `open=<n> unknown=<n> summary=<…>` on top of the BATCH-001 batch helper, and call it from both sites.

### Section 4: Expression Size Limit Risk Assessment

The guard is enforced in CI: `tests/test_workflow_file_size_limit.py:66-98` fails any `run:` body that contains `${{` and is at least 18,000 characters, and lines 44-59 fail any workflow file of 480,000 bytes or more. Character counts below are estimates from line counts and sampled line lengths; exact `len(run)` was not measured in this sandbox.

**EXPR-001 — orchestrate_poll.yml "Stage workflow support files" inlines `${{ }}` in a ~300-line body**
- **File:** `.github/workflows/orchestrate_poll.yml:667-969` (interpolations at 812, 813, 947)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The body is about 302 lines, and line 673 alone is a 51-entry script list of over 1,100 characters that grows with every new staged helper.
  - Estimated 15,000–18,000 characters, so headroom against 21,000 is about 3,000–6,000 [NEEDS VERIFICATION].
  - The interpolations are `${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED || 'false' }}`, `${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED || 'false' }}` and `${{ github.repository }}`.
  - `implement.yml:1445-1452` already removed expressions from its equivalent step. Its comment cites the 21,000-character rejection, and it uses `env:` keys `STAGE_PROMPT_PRELUDE_REFACTOR_ENABLED` and `STAGE_UNATTENDED_IDENTITY_REINJECT_ENABLED`.
- **Recommended fix:**
  - Mirror implement.yml: add the same two `env:` keys and echo them.
  - Replace line 947's interpolation with `"${GITHUB_REPOSITORY}"`.
  - The body then contains no `${{` and is exempt from the expression limit.

**EXPR-002 — workflow-log-analysis.yml "Run workflow log analysis"**
- **File:** `.github/workflows/workflow-log-analysis.yml:1105-1438` (interpolations at 1150-1152, 1165, 1171-1173, 1210-1212)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - About 333 lines; estimated 14,000–18,000 characters, so headroom is about 3,000–7,000 [NEEDS VERIFICATION].
  - Every interpolation is a value the runner already exports: `github.run_id`, `github.run_attempt`, `github.workflow`, `github.server_url` and `github.repository`.
- **Recommended fix:**
  - Replace them with `${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}`, `${GITHUB_SERVER_URL}` and `${GITHUB_REPOSITORY}`, which takes the body out of the expression limit with no behaviour change.
  - DUP-004's extraction then removes about 95 more lines.

**EXPR-003 — Deep-audit and API-redundancy steps**
- **File:** `.github/workflows/workflow-log-analysis.yml:1979-2233` (interpolations at 2011-2013, 2025, 2031-2033, 2064-2066); `2596-2849` (interpolations at 2628-2630, 2642, 2648-2650, 2681-2683)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** about 254 lines each; estimated 11,000–15,000 characters, so headroom is about 6,000–10,000 [NEEDS VERIFICATION]. The interpolations are the same default-env values as EXPR-002.
- **Recommended fix:** same as EXPR-002.

**EXPR-004 — implement.yml "Handle no-op implementation"**
- **File:** `.github/workflows/implement.yml:5544-5774` (interpolations at 5549, 5551, 5757, 5765, 5767, 5771, 5773)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** about 230 lines including multi-line comment templates; estimated 11,000–14,000 characters, so headroom is about 7,000–10,000 [NEEDS VERIFICATION]. All interpolations are `github.server_url`, `github.repository` and `github.run_id`.
- **Recommended fix:** use `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`, or the already-assigned `${REPO}` and `${RUN_URL}`.

**EXPR-005 — Largest workflow files against the file-size limit**
- **File:** `.github/workflows/implement.yml` (7,362 lines), `.github/workflows/test-and-mark-stable.yml` (7,163 lines), `.github/workflows/review_autofix.yml` (6,085 lines)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - The audit brief cites a 1 MB limit. This repo's documented and tested limit is 512,000 bytes, with the CI guard at 480,000 (§24; `tests/test_workflow_file_size_limit.py:26-27`). No file can approach 800 KB without failing CI first.
  - These three are the largest by line count. Byte sizes were not measured [NEEDS VERIFICATION].
- **Recommended fix:**
  - Record `wc -c` for these three in the next run.
  - If any is within 50,000 bytes of 480,000, extract the largest expression-free bodies first. Candidates:
    - `implement.yml` "Run Codex implementation", lines 2893-4218 (no `${{` in the body)
    - "Attempt post-Codex syntax repair", lines 4753-5284
  - Use the `scripts/<workflow>_step_<slug>.sh` pattern.

Excluded:
- `implement.yml` "Run Codex implementation" (2893-4218), "Preflight destructive-commit guard" (4328-4741) and "Stage workflow support files" (1445-1876) contain no `${{` in their bodies.
- Composite actions under `.github/actions/*/action.yml` are 39–166 lines with no body interpolation.
- No `if:` condition close to the limit was found; the longest seen is `implement.yml:5537` at about 330 characters.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — `GH_API_RETRY_HELPERS` is read in 8 workflows that never set it**
- **File:**
  - `orchestrate_poll.yml:164,590`
  - `clarify.yml:406,2523`
  - `plan.yml:525,3915`
  - `orchestrate.yml:219,393`
  - `check_failure_triage.yml:335`
  - `cancel_on_pr_close.yml:50`
  - `workflow_failure_heal.yml:118`
  - `review_autofix_sweep.yml:229`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:**
  - `GH_API_RETRY_HELPERS=` is written only in `implement.yml:209`, `validate.yml:188`, `review_autofix.yml:409`, `security-audit.yml:122` and `orchestrate_clarify_respond.yml:136`, each by its "Bootstrap GitHub API retry helper" step.
  - Every other `source "${GH_API_RETRY_HELPERS:-/dev/null}"` sources `/dev/null`, and the `gh_api_retry` stub then degrades to a single `gh api`.
  - In `orchestrate_poll.yml:590`, `scripts/gh_helpers.sh` has already been staged (line 251) but is not used.
  - This was first noted in `analysis/workflow-optimization-2026-10-25-2.md:286` and is still present.
- **Recommended fix:**
  - Where a staged helper exists by that step, source it directly: `source scripts/gh_helpers.sh` in `orchestrate_poll.yml:590`.
  - Elsewhere, either add the bootstrap step (agents.md documents it as identical across workflows) or delete the dead `source` line and keep the stub, to make the no-retry behaviour explicit.

**CONSIST-001 — The inline label creator uses a description that differs from the contract**
- **File:** `workflow-log-analysis.yml:1221-1224,2074,2691`; `.github/ai/label_contract.v1.json:84-87`; `scripts/label_helpers.sh:44,111`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**
  - `_wla_label_create "ai:log-analysis-failed" … --description "Workflow log analysis failed"`.
  - The contract and `label_helpers.sh` both say "Workflow log analysis failed before producing an analysis report".
  - A label first created by this path is later rewritten by `sync_ai_labels.yml`.
- **Recommended fix:** use `ensure_label_exists "ai:log-analysis-failed" "${GITHUB_REPOSITORY}"` (folded into DUP-004).

**CONSIST-002 — Mixed `on:` and `"on":` keys**
- **File:** 25 workflows use an unquoted `on:` (e.g. `review_rb_judge_dispatch.yml:24`, `internal-check-failure-triage.yml:3`); the rest use `"on":` (e.g. `integration-pr-readiness.yml:25`)
- **Severity:** Low
- **Category:** `consistency`
- **Description:** YAML 1.1 parsers, and yamllint's `truthy` rule, read a bare `on` as boolean `true`. Both forms work on GitHub, but tooling such as PyYAML in `tests/test_workflow_file_size_limit.py:73` sees the key as `True` in the unquoted files.
- **Recommended fix:** normalise to `"on":` when those files are next edited. Do not do a standalone churn PR (§9).

**CONSIST-003 — An inline input sits next to its own env alias**
- **File:** `.github/workflows/sync_ai_labels.yml:125`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `if [ "${{ inputs.dry_run }}" = "true" ]` is the only body interpolation in the file. The sibling steps already pass `DRY_RUN: ${{ inputs.dry_run }}` through `env:` (lines 170 and 259).
- **Recommended fix:** add `DRY_RUN: ${{ inputs.dry_run }}` to the `sync` step's `env:` and test `"${DRY_RUN}"`.

**SHELL-001 — Mixed tab and space indentation inside the space-indented poller**
- **File:** `scripts/orchestrate_poll_process.sh:23073-23076,23159-23162` (tab-indented lines inside 2-space GraphQL and jq blocks); whole functions such as lines 18974 and 19062 are tab-indented while the surrounding file is 2-space
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** harmless at runtime, but it causes noisy diffs and conflicts in a 36,802-line file. It conflicts with the tabs rule in §11.
- **Recommended fix:** normalise only the lines touched by the next functional change (§9). No other obvious SC2086, SC2046 or SC2006 problems were found in the reviewed ranges; the `for … in $(seq …)` loops iterate numbers only.

**DEBT-001 — The poller monolith**
- **File:** `scripts/orchestrate_poll_process.sh` (36,802 lines; 490 lines matching `gh_retry`, `_safe_gh_jq`, `gh api`, `gh issue`, `gh pr`, `gh run` or `gh workflow`)
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:**
  - One file holds every poller phase, so per-phase timing, the proposed `POLL_PHASE_TIMING` lines and API-call attribution are hard to review.
  - This is the likely place for the 1431–2643 s ticks reported in the Executive Summary (inference).
- **Recommended fix:** after `POLL_PHASE_TIMING` lands (report Speed item 1), split the top two phases into sourced `scripts/orchestrate_poll_<phase>.sh` modules, keeping function names unchanged (§10).

TODO, FIXME, HACK and XXX markers: none found in `scripts/` or `.github/workflows/`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 9 | SEC-001, BUG-001, BUG-002, BUG-003, API-001, BATCH-001, DUP-001, EXPR-001, EXPR-002 |
| Low | 18 | BUG-004, BUG-005, BUG-006, BUG-007, API-002, DUP-002, DUP-003, DUP-004, DUP-005, EXPR-003, EXPR-004, EXPR-005, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 0 (no Critical/High findings) | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml` | Medium |
| Code modularization | 9 workflows (DUP-001) + new `.github/actions/resolve-support-ref/action.yml`, `scripts/gh_helpers.sh`, `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/plan.yml`, new `scripts/wla_phase_failure.sh`, `tests/test_workflow_support_ref_follows_pin.py` | Large |
| Expression size reduction | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml` | Small |
| Medium/Low fixes | `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `scripts/check_integration_pr_readiness.py`, `scripts/implement_diagnose_post_codex_failure.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml`, `scripts/label_helpers.sh`, `.github/workflows/sync_ai_labels.yml`, 8 workflows for DEAD-001 | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-11-17)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: every precondition was proven by reading the code. Both calls use the same endpoint and fields, sit in the same function, have nothing that could change the data between them, keep the same error and retry behaviour, and cross no concurrency boundary. The implement stage may act on these without further review.
- **`NEEDS_VERIFICATION`**: the change looks correct, but at least one precondition can't be proven from the code alone. Do the listed check before changing anything.
- **`RISKY_SKIP`**: the call sits in a path that must not be changed automatically. That covers `orchestrate_poll_process.sh`, auth or identity flows, retry or stall-recovery paths, pagination, and log keys other code greps for. These are listed for visibility only and need manual review.

### Consolidation Candidates (MERGE-###)

**MERGE-001: `branches/promote-candidate` is read up to three times in the single `implement` job of `implement.yml`**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Calls involved:**
  - `.github/workflows/implement.yml:470-498`: duplicate-PR notice ref, call at line 489.
  - `.github/workflows/implement.yml:772-803`: CI-triage PR-not-open ref, call at line 794.
  - `.github/workflows/implement.yml:1353-1382`: main "Resolve workflow support ref", call at line 1373.
  - All three are in the `implement` job (`implement.yml:109`). The `heal-report` job copy (`implement.yml:7262+`) is excluded because it is a separate job.
- **Current call count:** 1–3 per run, only when `workflow_ref` is `…implement.yml@refs/heads/promote-candidate`. **Proposed:** 1.
- **Endpoint:** `GET /repos/shubhodeep1/coding-workflows/branches/promote-candidate`
- **Evidence:** each step has the same body, and each comment only checks its own step:
  ```bash
  # One branches read, only on this path: no earlier call in this step
  # returns the promote-candidate tip or its protection flag.
  promote_candidate_json="$(gh_api_retry repos/shubhodeep1/coding-workflows/branches/promote-candidate 2>/dev/null || true)"
  ```
  The results go to three different env keys: `DUPLICATE_NOTICE_SCRIPT_REF` (line 498), `PR_NOT_OPEN_SCRIPT_REF` (line 803) and `SCRIPT_REF` (line 1382).
- **Why this is net-new:** API-002 covers the same pattern, but only in `orchestrate_poll.yml`. DUP-001 lists these `implement.yml` sites as code duplication, not as repeated API reads. This finding also carries over BUG-004's risk: steps that read at different times can pin support code to different promote-candidate tips within one job.
- **Proposed fix:**
  - Whichever of these steps runs first exports the resolved SHA once, for example `PROMOTE_CANDIDATE_RESOLVED_SHA` (name checked as unused per §10), to `$GITHUB_ENV`.
  - Later steps reuse it when it is a 40-hex value and fall back to the existing read otherwise.
  - Keep each step's existing `SUPPORT_REF source=…` lines and its own env key.
- **Safety rationale:** the steps are conditional (`existing_pr == 'true'`, `pr_not_open == 'true'`, `SKIP_IMPLEMENT != 'true'`). It is not proven whether more than one runs in a single job; if they are mutually exclusive the saving is zero. The change also touches `SUPPORT_REF` log output, which `tests/test_workflow_support_ref_follows_pin.py` asserts on.
- **Downstream signal (verify first):**
  1. Check whether `existing_pr == 'true'` or `pr_not_open == 'true'` ever lets the later "Resolve workflow support ref" (line 1353) run in the same job, i.e. whether `SKIP_IMPLEMENT` is always set on those paths. If they are mutually exclusive, close this as no-saving.
  2. Check that `tests/test_workflow_support_ref_follows_pin.py` accepts an "env-reuse" branch in the step body.
  3. Fold the fix into DUP-001's composite action instead of patching three inline copies.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: `check_integration_pr_readiness.py` fetches the tracking issue twice when the body file is empty and the labels file lacks the tracking label**
- **Safety tag:** `SAFE_TO_MERGE`
- **Calls involved:**
  - `scripts/check_integration_pr_readiness.py:200-214`: first `_fetch_issue`, line 201.
  - `scripts/check_integration_pr_readiness.py:216-223`: second `_fetch_issue`, line 221.
  - Both use `_fetch_issue` (lines 73-88): `gh issue view <n> --repo <repo> --json labels,body`.
- **Current call count:** 2 on this path. **Proposed:** 1. Other paths are unchanged.
- **Endpoint:** `gh issue view` (GraphQL `issue { labels body }`). Both calls use the same command, fields and token.
- **Evidence:**
  - When `--tracking-labels-file` is non-empty (`labels_from_file` is true, line 198) but the body is empty, line 201 fetches the issue.
  - Line 211 then keeps the file labels and drops `fetched_issue["labels"]`:
    ```python
    if not issue["labels"]:
        issue["labels"] = fetched_issue["labels"]
    ```
  - If the file labels lack `TRACKING_LABEL`, line 221 runs the same fetch again, milliseconds later:
    ```python
    fresh_issue = _fetch_issue(args.repo, tracking_num)
    if fresh_issue is not None and fresh_issue["labels"]:
        issue["labels"] = fresh_issue["labels"]
    ```
  - The existing test `tests/test_check_integration_pr_readiness.py:361-399` asserts `fetch_calls == [2734]` only for the body-present case. The double fetch on the empty-body path is not tested.
- **Proposed fix:**
  - In `main()`, declare `fetched_issue: dict | None = None` before line 200.
  - At line 221, use `fresh_issue = fetched_issue if fetched_issue is not None else _fetch_issue(args.repo, tracking_num)`, keeping the guard `fresh_issue is not None and fresh_issue["labels"]`.
  - Add a test with an empty body file and a labels file containing `enhancement` that asserts `fetch_calls == [n]`.
  - This works alongside the report's Reliability item 1 (a retry inside `_fetch_issue`).
- **Safety rationale:**
  - Both calls are the same command in the same function, with only pure Python between them.
  - If line 201 fails, the script already returns 1 at line 207 and never reaches line 221, so error behaviour is unchanged. Neither call retries.
  - There is no concurrency boundary.
  - The stale-poller-snapshot defence described at lines 217-220 is still met, because the reused result is a live read from the same process.
- **Downstream signal:** In `scripts/check_integration_pr_readiness.py` `main()`, reuse the line-201 `fetched_issue` result as `fresh_issue` at line 221 when it is not `None`, and add the single-fetch test described above.

**REUSE-002: the poller's readiness refresh fetches the integration PR, then fetches that PR's labels again**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `scripts/orchestrate_poll_process.sh:4761`: `_fetch_pr_json`, defined at lines 2534-2538.
  - `scripts/orchestrate_poll_process.sh:4772`: `get_issue_labels_json`, defined at lines 4611-4614.
- **Current call count:** 2 REST calls per readiness refresh. **Proposed:** 1.
- **Endpoints:** `GET /repos/{repo}/pulls/{final_pr}` and `GET /repos/{repo}/issues/{final_pr}/labels`.
- **Evidence:**
  - The pulls response already includes a `labels` array, and `pr_json` is already in scope:
    ```bash
    pr_json="$(_fetch_pr_json "${final_pr}")"
    ...
    pr_labels_json="$(get_issue_labels_json "${final_pr}")"
    ```
  - `get_issue_labels_json` runs only after `pr_state == open` and a non-empty `pr_head_sha` (line 4766), so `pr_json` has already been parsed successfully at that point.
  - `get_issue_labels_json` has no `--paginate`, so it caps at 30 labels. This is the same pattern as BUG-003.
  - A failure falls back to `'[]'`, which hides `ai:override-incomplete-merge` and so fails closed.
- **Proposed fix:** `pr_labels_json="$(jq -c '[.labels[]?.name]' <<< "${pr_json}")"`, falling back to `get_issue_labels_json` only when `.labels` is missing.
- **Safety rationale:** the code is inside `orchestrate_poll_process.sh`, a mandatory `RISKY_SKIP` trigger. The change also merges two separate fail-closed paths into one, and the output feeds a required merge gate (`orchestrator/integration-pr-not-ready`) that BUG-001 shows is already exposed to a race.
- **Downstream signal (manual review):**
  - Do not auto-implement.
  - A reviewer must confirm that `_safe_gh_jq` returns the unfiltered pulls object, including `.labels`.
  - They must also confirm that reusing it does not hide a label added between the two reads in a way that matters for the override path.
  - Consider bundling with BUG-003's pagination fix and the `POLL_PHASE_TIMING` measurement.

**REUSE-003: the bulk-delete override step looks up `GET /user` again, although the same job already exported the login**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `.github/workflows/implement.yml:2134-2141`: "Fetch issue metadata" runs `gh api user --jq .login` and, after a shape check, exports `IMPLEMENT_SCOPE_PIPELINE_LOGIN` (line 2140).
  - `.github/workflows/implement.yml:2480`: "Unblock judge bulk-delete override" runs `gh api user --jq '.login'` again.
- **Current call count:** 2 per run when an `override=bulk_delete -->` marker is present (gated at line 2477); otherwise 1. **Proposed:** 1.
- **Endpoint:** `GET /user`. Both steps use `GH_TOKEN: ${{ secrets.GH_PAT }}` (lines 2082 and 2474).
- **Evidence:**
  ```bash
  pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"
  if [ -z "${pipeline_login}" ]; then
    echo "UNBLOCK_BULK_DELETE_OVERRIDE outcome=skip reason=login_unavailable"
  ```
  The comment at lines 2131-2133 covers only the reverse direction: the later read cannot feed the earlier grant. Nothing stops the later step from reading `IMPLEMENT_SCOPE_PIPELINE_LOGIN`.
- **Proposed fix:** `pipeline_login="${IMPLEMENT_SCOPE_PIPELINE_LOGIN:-}"`, falling back to the live `gh api user` read when it is empty.
- **Safety rationale:**
  - The login is the trust anchor for deciding which comments the pipeline wrote. Those comments lift the bulk-delete threshold, so this is an authentication or identity-probe flow, a mandatory `RISKY_SKIP` trigger.
  - `GITHUB_ENV` values can be written by any earlier step in the job, which a live read is not exposed to.
  - The reuse would also change when `reason=login_unavailable` is logged.
- **Downstream signal (manual review):**
  - Do not auto-implement.
  - A security reviewer must decide whether a `GITHUB_ENV`-sourced identity is acceptable for the override marker check. The saving is at most one call, and only on rare override runs, so declining is a reasonable outcome.

### Dead Calls (DEAD-API-###)

**DEAD-API-001: the label-read fallback in "Validate approval phase label" can effectively never run**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Calls involved:**
  - `.github/workflows/implement.yml:2377-2379`: the fallback call.
  - `.github/workflows/implement.yml:2329-2335`: the earlier read it depends on.
- **Current call count:** 0–1 per run, in practice 0. **Proposed:** 0.
- **Endpoint:** `GET /repos/{repo}/issues/{n}` (`--jq '[.labels[].name]'`).
- **Evidence:**
  1. Line 2329 fetches the issue. An empty result exits at lines 2330-2333 (`reason=metadata_unavailable`).
  2. Line 2335 writes that result to `${ISSUE_META_FILE}` unconditionally.
  3. Line 2346 runs `security_dependency.py` on the file. Unparseable JSON is caught (`scripts/security_dependency.py:119-124`, `except (OSError, ValueError, AttributeError)`) and returns `held`, which exits at lines 2347-2350.
  4. The fallback therefore runs only if the file holds valid JSON whose `.labels[].name` jq fails, which a successful `GET /issues/{n}` response never produces:
     ```bash
     if [ -z "${ISSUE_LABELS_JSON}" ]; then
       ISSUE_LABELS_JSON="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]')"
     fi
     ```
  5. The comment at lines 2366-2372 ("Reuse the issue snapshot fetched in 'Fetch issue metadata'") is out of date: line 2335 now replaces that snapshot within the same step.
- **Proposed fix:**
  - Replace the fallback with the existing fail-closed skip: `echo "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=metadata_unavailable outcome=skip issue=${ISSUE_NUMBER}"`, set `SKIP_IMPLEMENT=true` and `exit 0`.
  - Update the stale comment.
  - This reuses an existing reason token and adds no new prefix.
- **Safety rationale:** the dead path depends on `security_dependency.py` and `pr_dependency.py` always returning `held` for malformed metadata. That holds for `security_dependency.py` (lines 119-124) but was not checked for `pr_dependency.py`. Also, under `set -euo pipefail` the current fallback fails the step, while the proposed fix skips, which is a different error behaviour.
- **Downstream signal (verify first):**
  1. Confirm that `scripts/pr_dependency.py pr-dependency` returns `held` (or exits non-zero) on unparseable `--issue-json`.
  2. Check `tests/` for any harness that runs the "Validate approval phase label" step with a valid JSON file that has no `labels`.
  3. Decide whether "skip" or "fail the step" is the right result for unreadable labels. Then drop lines 2377-2379.

### Cross-References to Deep Audit Section
- API-001: `RISKY_SKIP`. The finding is correct, but it sits in `orchestrate_poll_process.sh` (lines 30779-30791) and changes a fail-open split that is documented on purpose.
  - Net-new detail: before seeding `LABELS_JSON` from the details cache, check that `_fetch_candidate_issue_details_graphql` leaves out (rather than returns empty) issues whose batch failed. Otherwise a missing entry becomes `[]` and repeats BUG-003.
- BATCH-001: `RISKY_SKIP`. The per-blocker REST loops are in `orchestrate_poll_process.sh` (lines 7778-7795 and 33425-33446), inside blocker and stall handling.
  - Net-new detail: the new batch helper must keep "miss leads to `unknown`", so a blocker that failed to read is never counted as closed.
- API-002: `NEEDS_VERIFICATION`. Reusing `SCRIPT_REF` from `$GITHUB_ENV` changes the second step's `SUPPORT_REF source=…` lines, which `tests/test_workflow_support_ref_follows_pin.py` asserts on. Apply it together with BUG-004 and MERGE-001.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 3 | MERGE-001, DEAD-API-001, API-002 (cross-ref) |
| RISKY_SKIP | 4 | REUSE-002, REUSE-003, API-001 (cross-ref), BATCH-001 (cross-ref) |

### Implement-Stage Handoff
- REUSE-001: in `scripts/check_integration_pr_readiness.py` `main()`, reuse the line-201 `fetched_issue` as `fresh_issue` at line 221 when it is not `None`, and add a single-fetch regression test to `tests/test_check_integration_pr_readiness.py`.