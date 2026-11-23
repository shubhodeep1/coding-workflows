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
## Deep Audit — Workflows & Scripts (2026-10-24)

**Scope and method.** This audit read the pre-audit context (`CLAUDE.md`, the relevant `agents.md` sections, the `README.md` headings, all of `.github/ai/`) and the in-progress report. That report has no earlier finding IDs, so nothing here is cross-referenced. It then went deep on the highest-traffic surfaces rather than every file:

- Workflows: `implement.yml`, `review_autofix.yml`, `plan.yml`, `orchestrate_poll.yml`, `test-and-mark-stable.yml`, `workflow-log-analysis.yml`.
- Scripts: `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh` (31,270 lines), the `scripts/review_autofix_step_*.sh` helpers, `scripts/review_collect_pr_metadata.sh`, `scripts/workflow_failure_heal_queue.sh`, `scripts/plan_split_dispatch.sh`, `scripts/validate_process.sh`, `scripts/stage_workflow_support.sh`.

No shell, `wc -c` or shellcheck could be run in this sandbox. All byte and character counts are estimates from line counts and sampled line lengths, and are marked as such.

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Inline `gh_retry` in the deterministic-skip job retries "label already exists" failures 4 times
- **File:** `.github/workflows/review_autofix.yml:1165-1192` (callers at `:1408` and `:1445`)
- **Severity:** Medium · **Category:** `bug`
- **Description:** The step "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge" defines its own `gh_retry()` (`local n=0 max=4 delay=2`). It retries every non-zero exit with sleeps of 2, 4 and 8 seconds and has no permanent-failure check. Its local `ensure_label_exists()` (`:1189-1192`) runs `gh_retry gh label create "${label}" ... >/dev/null 2>&1 || true` unconditionally.
  - `gh label create` exits non-zero whenever the label already exists, which is the normal case. `scripts/label_helpers.sh:190` relies on that stderr text.
  - So each call makes 4 label-create POSTs and sleeps about 14 seconds. The two calls (`ai:review-skipped` at `:1408`, `ai:ready-to-merge` at `:1445`) waste up to 8 API calls and about 28 seconds on every deterministic-skip merge.
  - The shared `gh_retry` in `scripts/gh_helpers.sh:1099` stops at once, because `_is_gh_permanent_failure` (`:194`) matches `HTTP 422`.
- **Recommended fix:** Make the inline `ensure_label_exists` match the "already exists" text before retrying, as `scripts/label_helpers.sh:186-193` does. Or add the `_is_gh_permanent_failure` regex from `gh_helpers.sh:194` to the inline wrapper's failure branch. Keep the wrapper (the comment at `:1158-1164` explains why the job avoids the support checkout), but make it stop on 4xx.

#### SEC-001 — `gh_retry` and `gh_retry_to_file` echo raw `$*` into `::warning::` / `::error::` workflow commands
- **File:** `scripts/gh_helpers.sh:1100`, `:1117`, `:1130`, `:1132` (`gh_retry`); `:1185`, `:1203`, `:1215` (`gh_retry_to_file`)
- **Severity:** Medium · **Category:** `security`
- **Description:** On failure, the helpers print `echo "::error::gh command failed after ${max_attempts} attempts: $*"` and similar lines with the full, unescaped argument vector.
  - Callers routinely pass `-f body="..."` with multi-line text built from issue or PR content and model output. Examples: `implement.yml:5460-5461` and `:5623-5624`, `review_autofix.yml:5178`, `orchestrate_poll_process.sh:94-96`.
  - Any embedded line that starts with `::` is parsed by the runner as a workflow command (`::stop-commands::`, `::add-mask::`, spoofed annotations). Whole comment bodies are also dumped into logs.
  - Only stderr is escaped (`_gh_actions_escape` at `:1102`). `gh_api_retry` already escapes its endpoint (`:907`), so the two helpers are inconsistent.
- **Recommended fix:** Log a redacted summary instead of `$*`: the command words plus the endpoint, with every `-f`/`-F`/`--field`/`--raw-field` value dropped, passed through `_gh_actions_escape`. For example:

```bash
_gh_cmd_log_summary()
{
	local _a _skip=0 _out=""
	for _a in "$@"; do
		if [ "${_skip}" = "1" ]; then _out+=" <redacted>"; _skip=0; continue; fi
		case "${_a}" in
			-f|-F|--field|--raw-field|--input) _out+=" ${_a}"; _skip=1 ;;
			*) _out+=" ${_a}" ;;
		esac
	done
	_gh_actions_escape "${_out# }"
}
```

#### BUG-002 — Tracking-issue discovery is silently capped at 20 projects
- **File:** `.github/workflows/orchestrate_poll.yml:301-311`; consumers at `scripts/orchestrate_poll_process.sh:5826-5831`, `:21010-21033`, `:23672`
- **Severity:** Medium · **Category:** `bug`
- **Description:** The tracking list comes from `gh issue list ... --label "ai:orchestrator-tracking" --state open --json number,title,labels --limit 20`. It is written to `${RUNTIME_DIR}/tracking_issues.json`, which is the only source the poller reads.
  - With more than 20 open tracking issues, the extra projects are never processed. Nothing warns when `COUNT` equals 20.
  - Their managed sub-issues are also missing from `orchestrator_managed_set` (`:21002-21033`). Inference: standalone stall recovery could then treat orchestrator-managed issues as standalone.
  - [NEEDS VERIFICATION] whether more than 20 concurrent projects happen in practice.
- **Recommended fix:** Raise the limit (for example `--limit 100`). Emit `::warning::` plus a stable log line, for example `TRACKING_ISSUES_TRUNCATED count=<n> limit=<n>`, when `COUNT` equals the limit. Fail open by processing the returned set.

#### BUG-003 — Clarify-phase wait ignores bot failure comments
- **File:** `.github/workflows/test-and-mark-stable.yml:997-1013`
- **Severity:** Low · **Category:** `bug`
- **Description:** The block is commented "Check for failure". It tests `grep -qi "failed\|error\|clarification.*needed"` but only acts on the inner `clarification.*needed\|NEEDS_CLARIFICATION` match. A plain "failed" or "error" bot comment matches the outer `if` and then does nothing. The loop keeps polling until `INACTIVITY_LIMIT` (`:1009`) instead of failing fast, which stretches the stable gate's wall clock on a real clarify failure.
- **Recommended fix:** Add an `else` branch that writes `status=failed` to `$GITHUB_OUTPUT` and runs `exit 1`. Or drop the outer pattern if failing fast is not wanted. Check the plan, implement and validate wait loops (`:1047+`, `:1354+`, `:2524+`) for the same shape.

#### BUG-004 — No-op handler can abort before applying `ai:implementation-failed`
- **File:** `.github/workflows/implement.yml:5623-5640`
- **Severity:** Low · **Category:** `bug`
- **Description:** Under `set -euo pipefail` (`:5414`), `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` has no `|| ...` guard. It runs before the label swap at `:5629-5640`. A permanent comment failure aborts the step, for example "Commenting is disabled on issues with more than 2500 comments", which `gh_helpers.sh:194` treats as permanent. The issue is then left without `ai:implementation-failed`. Inference: the job then fails, and the `failure()` handler applies `ai:blocked` (`:6765-6770`) instead of the re-issue label the orchestrator expects.
- **Recommended fix:** Apply the label first (`:5629-5640`), then post the comment with `|| echo "::warning::..."`.

#### BUG-005 — Close-with-comment fallback can duplicate the comment and leave the issue open
- **File:** `.github/workflows/implement.yml:5458-5461`, `:5592-5595`
- **Severity:** Low · **Category:** `bug`
- **Description:** The code runs `gh_retry gh issue close ... --comment "${body}" 2>/dev/null || gh_retry gh api ".../comments" -f body=...`. If `gh issue close --comment` posts the comment and then the close fails, the fallback posts a second identical comment and never closes the issue. The issue stays open with `ai:closed`. [NEEDS VERIFICATION] that gh posts the comment before closing.
- **Recommended fix:** Split the call: post the comment once, then close via `gh_retry gh api -X PATCH "repos/${REPO}/issues/${ISSUE_NUMBER}" -f state=closed -f state_reason=completed`. That is the pattern already used at `scripts/orchestrate_poll_process.sh:14568`.

#### BUG-006 — `emit_event` is used without a fallback stub in the retrigger step
- **File:** `scripts/review_autofix_step_post_commit_retrigger.sh:8`, `:51`, `:129`, `:132`, `:140`, `:143`
- **Severity:** Low · **Category:** `bug`
- **Description:** The script runs under `set -euo pipefail` and sources `gh_helpers.sh` with `2>/dev/null || true`. It then calls `emit_event` unconditionally. `emit_event` only exists if `gh_helpers.sh` loaded (`gh_helpers.sh:129-134`). If the helper is missing, the call exits 127 and the review re-dispatch is skipped.
- **Recommended fix:** Add `type emit_event >/dev/null 2>&1 || emit_event() { return 0; }` after line 8, matching `.github/workflows/workflow-log-analysis.yml:1077`.

### Section 2: GitHub API Call Redundancy Audit

#### API-001 — `ensure_label_exists` always POSTs a label create, with no per-process cache
- **File:** `scripts/label_helpers.sh:163-197`. Call sites: `implement.yml` (14 `ensure_label_exists` plus 8 raw `gh label create`), `review_autofix.yml` (13 plus 4), `plan.yml` (8), `orchestrate.yml` (6), `clarify.yml` (4 plus 1).
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** Every call issues `POST /repos/{repo}/labels`. In steady state it returns 422 "already exists". `scripts/orchestrate_poll_process.sh:4030-4038` already keeps a per-process `_ENSURED_LABELS_CACHE`; `label_helpers.sh` does not. `set_issue_phase_label_resilient` (`:223`) adds one more create per phase transition.
- **Calls:** currently 1 per invocation (several per run). Proposed: 1 per label per process, or 0 when labels are known to be synced (`sync_ai_labels.yml` exists).
- **Pattern to extend:** the `_ENSURED_LABELS_CACHE` fast path at `orchestrate_poll_process.sh:4033-4038`.

#### API-002 — No-op ancestor-chain walk makes 2 REST calls per hop
- **File:** `.github/workflows/implement.yml:5551-5569`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** Each hop does `gh api repos/.../issues/${current} --jq '.body'` and then a full `--paginate` comments listing, only to test for the no-op marker. The default threshold is 2, so up to 4 calls plus extra pages. The chain is inherently sequential.
- **Calls:** currently 2×threshold, plus pages. Proposed: 1 GraphQL call per hop, fetching the body plus the last N comments (as `_fetch_candidate_issue_details_graphql` shapes its data), so threshold calls in total.

#### API-003 — Reclarify replay re-reads each queued issue that the listing already returned
- **File:** `scripts/orchestrate_poll_process.sh:46-58`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** `gh issue list --label ai:reclarify-requeue --json number` (`:46-47`) is followed by a per-issue `GET issues/${issue_num}` (`:55`) just to check `state`, `pull_request` and labels. The listing can return `--json number,state,labels`.
- **Calls:** currently 1 + 2N (+ pages). Proposed: 1 + N.
- **Caveat:** [NEEDS VERIFICATION] The header comment (`:38-41`) calls this a "live issue read" on purpose. Keep it if a same-tick listing counts as too stale.

#### API-004 — Tracking-issue comment history fetched several times per poll cycle
- **File:** `scripts/orchestrate_poll_process.sh:23713-23720` (main loop), `:5839-5848` (`resolve_active_orchestrator_context_for_issue`, called per review-blocked merged issue at `:27051`), `:21010-21031` (standalone stall recovery)
- **Severity:** Medium · **Category:** `api-redundancy`
- **Description:** Each path paginates `issues/${tracking}/comments?per_page=100` for the same tracking issues in the same cycle.
  - `resolve_active_orchestrator_context_for_issue` is called with `preferred_tracking_num="${TRACKING_NUM}"`, whose comments are already in `COMMENTS` / `COMMENTS_FOR_TRACKING_NUM` (`:23718-23720`).
  - Stall recovery re-paginates every tracking issue only to extract `.waves[].issues[].github_issue`.
  - Tracking issues collect many state comments, so each fetch can be several pages.
- **Calls:** currently T (main) + T (stall) + up to R×T (review-blocked merged path) paginated listings. Proposed: T, via a cycle-local cache `${RUNTIME_DIR}/tracking_comments_<n>.json` written at `:23713`, invalidated after this process posts a state comment, and read at `:5842` / `:21016`.
- **Caveat:** [NEEDS VERIFICATION] for the state-freshness rules after in-cycle state writes.
- **Pattern to extend:** the cycle-local cache convention (`_candidate_details_json`, `STALL_MANAGED_LINKED_PR_CACHE`).

#### API-005 — Check-triage resolution gate re-reads an issue the candidate batch may already hold
- **File:** `scripts/orchestrate_poll_process.sh:19868-19873`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** `_stall_check_triage_resolution_gate` always runs `gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_num}"`. The same stall pass already builds an issue JSON from `_candidate_details_json` when `.body` is cached (`:21159-21169`).
- **Calls:** currently 1 per gate. Proposed: 0 on a cache hit.
- **Caveat:** [NEEDS VERIFICATION] that `scripts/check_triage_resolution.py gate` only needs fields the batch provides (body, labels, author login, author_association).

#### API-006 — Issue metadata read again after the precheck
- **File:** `.github/workflows/implement.yml:207`, `:2058-2060`, `:2273`, `:2689`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** The same `repos/{repo}/issues/{n}` is read at the precheck (`:207`, `ISSUE_PAYLOAD`), at "Fetch issue metadata" (`:2059`, only when the file is not cached) and at two deliberate pre-advance rechecks (`:2273`, `:2689`; the comment at `:2271-2272` says they are intentional). The `:207` payload is not persisted, so `:2059` always misses.
- **Calls:** currently up to 4. Proposed: 3, by persisting the `:207` payload to a `RUNNER_TEMP` file that `issue_meta_matches_issue` (`:2047-2056`) accepts.
- **Caveat:** [NEEDS VERIFICATION] that the runtime dir is not yet created at `:207` (it is created at `:283`).

#### BATCH-001 — Security-pass advisory follow-ups read one issue per row
- **File:** `scripts/orchestrate_poll_process.sh:7943-7964`
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** `security_pass_unblock_filed_advisory_followups` runs `gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${row_issue}" --jq '{state, labels}'` inside a `while read` loop over state rows. This is a per-iteration REST call in a loop, which §14 treats as a review blocker. The header (`:7915-7924`) documents the cost but not batching.
- **Calls:** currently N issue GETs, plus one comments GET per blocked row. Proposed: ceil(N/25) aliased GraphQL calls for `state` + `labels`, plus the existing comments GET only for rows that are still `ai:blocked`.
- **Pattern to extend:** `_fetch_candidate_issue_details_graphql` (alias per issue, dict keyed by number, fail open to the per-row path).

#### BATCH-002 — Every poll tick starts with 2–3 separate listing calls
- **File:** `.github/workflows/orchestrate_poll.yml:288-306`, `:366-371`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** Each tick makes a REST listing of `ai:provider-outage` issues (`:288`), a `gh issue list` of `ai:orchestrator-tracking` (`:301`) and, on idle ticks, a GraphQL probe (`:366`). All three are repository-scoped issue reads.
- **Calls:** currently 3 per idle tick and 2 per active tick. Proposed: 1, with one GraphQL query aliasing `outage: issues(labels:["ai:provider-outage"], states:OPEN){nodes{body authorAssociation}}`, `tracking: issues(labels:["ai:orchestrator-tracking"], states:OPEN, first:100){nodes{number title labels}}`, and the existing `l0..l6`/`search` probe aliases.
- **Saving:** about 2 calls per tick (inference: about 240 per day at the 12-minute cadence the report cites).
- **Pattern to extend:** the aliased probe already at `:352-364`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — "Resolve workflow support ref" promote-candidate block has 19 copies across 11 workflows
- **File:** `.github/workflows/implement.yml:444-486`, `:747-787`, `:1328-1371`, `:7095-7137`. The same `promote_candidate_json="$(gh_api_retry ...)"` body also appears in `validate.yml` (2), `orchestrate.yml` (2), `clarify.yml` (2), `plan.yml` (2), `orchestrate_poll.yml` (2), `cancel_on_pr_close.yml`, `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml`, `check_failure_triage.yml` and `review_autofix.yml`.
- **Severity:** Medium · **Category:** `duplication`
- **Description:** About 40 identical lines per copy. They differ only in the workflow filename inside the regex and the `GITHUB_ENV` variable name. `implement.yml:757` hardcodes `"shubhodeep1/coding-workflows"` where the other copies use `${wf_source}`, which already shows drift. The block cannot live in `scripts/`, because it decides which scripts ref to check out.
- **Recommended fix:** Add a composite action `.github/actions/resolve-support-ref` with inputs `workflow_file` and `env_name` and output `ref`. Reference it SHA-pinned, as `install-codex` is at `implement.yml:279`. Update `tests/test_workflow_support_ref_follows_pin.py`, which the comment at `implement.yml:755-756` says enforces the shape.

#### DUP-002 — "Stage workflow support files" is inline in 8 workflows, though an extracted script exists
- **File:** `orchestrate.yml:460`, `implement.yml:1420-1836`, `workflow_failure_heal.yml:181`, `orchestrate_clarify_respond.yml:481`, `clarify.yml:470`, `check_failure_triage.yml:428`, `orchestrate_poll.yml:605-897`, `plan.yml:448-616`. Extracted precedent: `scripts/stage_workflow_support.sh` (used by `review_autofix.yml:2112` and `validate.yml`).
- **Severity:** Medium · **Category:** `duplication`
- **Description:** The 12-line "src=.codex-workflow-src/... else .codex-workflow-src-main/... install -m" loop repeats 7 times inside `orchestrate_poll.yml:635-733` alone, differing only in the file name and the notice text.
- **Recommended fix:** Add `stage_support_file <relpath> <required|optional> <mode> <notice>` to `scripts/stage_workflow_support.sh`, or to a sibling `scripts/stage_support_helpers.sh` sourced from `.codex-workflow-src/scripts/`. Drive each workflow from a per-workflow manifest. Migrate `orchestrate_poll.yml` first; it is also EXPR-001. Update `plan.yml:2249-2266` in the same PR (DEBT-001).

#### DUP-003 — At least 10 `ensure_label_exists` variants with different signatures and catalogs
- **File:** `scripts/label_helpers.sh:163`, `scripts/orchestrate_poll_process.sh:4030`, `scripts/validate_process.sh:1222`, `.github/workflows/review_autofix.yml:1189` (4-argument signature), `:1607`, `:4438`, `scripts/review_autofix_step_mark_ready_to_merge.sh:15`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`, `scripts/review_rb_judge.sh:924`, `.github/workflows/workflow-log-analysis.yml:1079`/`:1882`/`:2482` (`_wla_label_create`, the same line 3 times in one file)
- **Severity:** Medium · **Category:** `duplication`
- **Description:** Three catalog strategies coexist: the hardcoded map in `label_helpers.sh`, the `label_contract.v1.json` lookup in `validate_process.sh` and `orchestrate_poll_process.sh`, and per-call literals. This is the root cause of CONSIST-001.
- **Recommended fix:** Make `label_helpers.sh::ensure_label_exists <label> [repo]` canonical:
  - read `.github/ai/label_contract.v1.json` when present (as `validate_process.sh:1229-1232` does);
  - add the `_ENSURED_LABELS_CACHE` fast path (API-001);
  - turn the shims into `source label_helpers.sh || <one-line fallback>`;
  - replace the three `_wla_label_create` definitions with one step-level helper sourced from `scripts/`.

#### DUP-004 — Inline `gh_retry` and `set_issue_phase_label_resilient` fallbacks repeated
- **File:** `.github/workflows/review_autofix.yml:816-838` and `:1165-1187` (two copies of the inline retry loop); `:1613-1619`, `:4450-4465`; `scripts/review_autofix_step_mark_ready_to_merge.sh:30-38` and `:40-50` (two copies of the same fallback in one file)
- **Severity:** Low · **Category:** `duplication`
- **Description:** The inline retry copies lack `gh_helpers.sh` semantics: no permanent-failure stop (BUG-001) and no single attempt for POST creates. `agents.md:4718` says to use the shared `gh_retry` rather than ad-hoc loops.
- **Recommended fix:** Move one fail-closed minimal wrapper into a tiny file staged with the gate/fingerprint helper checkouts. The existing SHA-pinned sparse checkouts at `review_autofix.yml:485-611` already deliver single scripts. Delete the second fallback in `review_autofix_step_mark_ready_to_merge.sh`.

#### DUP-005 — GH_PAT budget recording is re-implemented 12 times in `review_autofix.yml`
- **File:** `.github/workflows/review_autofix.yml:1126-1138` / `:1461-1478` (and 10 more "Record GH_PAT budget" steps); canonical `scripts/gh_helpers.sh:31-77` (`gh_pat_budget`)
- **Severity:** Low · **Category:** `duplication`
- **Description:** The inline copies read `gh api rate_limit --jq '[.resources.core.remaining, .resources.core.reset]'`. `gh_pat_budget` prefers the `x-ratelimit-*` headers when `x-ratelimit-resource: core` (`:52-60`). So the same `GH_PAT_BUDGET` log key is computed differently across workflows; `clarify.yml`, `orchestrate_poll.yml` and `review_autofix_sweep.yml` call `gh_pat_budget`.
- **Recommended fix:** Add a composite action `.github/actions/gh-pat-budget` (inputs `phase`, `job`, `snapshot_file`) that embeds the `gh_pat_budget` body. It needs no scripts checkout, which is why the inline copies exist. Use it for all 12 steps.

#### DUP-006 — The phase-wait inactivity loop is duplicated 6 times
- **File:** `.github/workflows/test-and-mark-stable.yml:893-1018`, `:1047-1310`, `:1354-1670`, `:2524-2650` (loop bounds approximate; anchors `INACTIVITY_LIMIT=` at `:893/:1047/:1354/:2524` and checks at `:1009/:1301/:1664/:2648`); `.github/workflows/comprehensive-test-and-release.yml:80`, `:322`. Extracted precedent: `scripts/stable_e2e_step_wait_review.sh:27-41`, `:294`, `:783`.
- **Severity:** Low · **Category:** `duplication`
- **Description:** These are the same `LAST_ACTIVITY` / `IDLE` / state-change reset / poll / timeout loops. One of them carries BUG-003.
- **Recommended fix:** Add `scripts/stable_e2e_step_wait_phase.sh <phase> <success_label_regex> <timeout_min>`, modelled on `stable_e2e_step_wait_review.sh`. This also cuts `test-and-mark-stable.yml`, which was the largest workflow by bytes in an earlier measurement (EXPR-005).

### Section 4: Expression Size Limit Risk Assessment

Only `run:` bodies that contain `${{ }}` count toward the 21,000-character limit. In every flagged block, the only interpolations are `github.repository`, `github.server_url`, `github.run_id` or `vars.*`. Replacing them with `${GITHUB_REPOSITORY}`, `${GITHUB_SERVER_URL}` and `${GITHUB_RUN_ID}` (or step `env:`) takes the whole body out of the limit at once. That is the cheapest fix for each finding below.

Large bodies checked and **excluded** because they contain no `${{ }}`: `implement.yml:2839-4111` ("Run Codex implementation", about 1,270 lines), `implement.yml:4240-4633`, `implement.yml:4653-5176`, `implement.yml:1428-1835`, `plan.yml:943-1146`, `plan.yml:2620-2913`, `review_autofix.yml:1150-1460`.

#### EXPR-001 — `orchestrate_poll.yml` "Stage workflow support files"
- **File:** `.github/workflows/orchestrate_poll.yml:608-897` (`${{ }}` at `:753`, `:754`, `:875`)
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** A 289-line body with three interpolations, about 15,000–16,000 characters (71–76%), leaving about 5,000 characters of headroom. Estimated from line counts and sampled lengths; [NEEDS VERIFICATION]. It grows with every helper added to the `for f in ...` list at `:614` (already about 830 characters on one line).
- **Recommended fix:** Move `vars.PROMPT_PRELUDE_REFACTOR_ENABLED` and `vars.UNATTENDED_IDENTITY_REINJECT_ENABLED` into step `env:` and use `${GITHUB_REPOSITORY}` at `:875`. The body then has no expressions. Then extract it per DUP-002.

#### EXPR-002 — `plan.yml` "Guard plans targeting template-owned paths"
- **File:** `.github/workflows/plan.yml:2157-2356` (`${{ }}` at `:2350`, `:2352`, `:2354`)
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** About 199 lines with an embedded perl filter (`:2215-2230`, `:2283-2289`), a Python heredoc (`:2243-2275`) and a roughly 720-character literal (`:2353`). Estimated 14,500–16,000 characters (69–76%), about 5,000–6,500 headroom; [NEEDS VERIFICATION]. The heredoc and comment block are likely to grow.
- **Recommended fix:** Replace the three interpolations with `${GITHUB_REPOSITORY}`, `${GITHUB_SERVER_URL}` and `${GITHUB_RUN_ID}`. Preferably extract the body to `scripts/plan_template_path_guard.sh`, with the Python part in a `.py` file.

#### EXPR-003 — `implement.yml` "Handle no-op implementation"
- **File:** `.github/workflows/implement.yml:5413-5640` (`${{ }}` at `:5418`, `:5420`, `:5623`, `:5631`, `:5633`, `:5637`, `:5639`)
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** About 228 lines with several 250–300-character literals (`:5443`, `:5445`, `:5483`, `:5490`, `:5577`, `:5579`). Estimated 14,000–15,500 characters (67–74%), about 5,500 headroom; [NEEDS VERIFICATION]. The body sets `REPO="${{ github.repository }}"` at `:5420` but still interpolates `${{ github.repository }}` again at `:5623-5639`.
- **Recommended fix:** Use `${REPO}` / `${GITHUB_*}` throughout, then extract to `scripts/implement_handle_noop.sh`, following `scripts/implement_handle_guard_block.sh`. Fix BUG-004 and BUG-005 in the same move.

#### EXPR-004 — `review_autofix.yml` "Telegram review-blocked judge decision" (watch item)
- **File:** `.github/workflows/review_autofix.yml:5267-5416` (`${{ }}` at `:5271`, `:5272`, `:5276`)
- **Severity:** Low · **Category:** `expression-limit`
- **Description:** About 149 lines of `case` branches, estimated 12,000–13,500 characters (57–64%), about 7,500 headroom; [NEEDS VERIFICATION]. A new judge action adds about 300 characters, so this approaches the 15,000-character Medium line. The neighbouring "Telegram editor-noop-suspicious warning" (`:5114-5252`, `${{ }}` at `:5118/:5119/:5178/:5229/:5251`) is about 9,500 characters.
- **Recommended fix:** Use `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}`. Or extract to `scripts/review_autofix_step_<slug>.sh` using the resolving-wrapper pattern in `agents.md:2539-2558`.

#### EXPR-005 — Workflow file size: the repo's real limit is 512,000 bytes, not 1 MB
- **File:** `.github/workflows/implement.yml` (7,166 lines), `.github/workflows/test-and-mark-stable.yml` (6,569 lines), `.github/workflows/review_autofix.yml` (5,816 lines); limits documented at `agents.md:2502-2530`
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - The 800 KB / 1 MB thresholds in the task prompt do not apply here. GitHub refuses workflows over 512,000 bytes (`agents.md:2513-2519`), and `tests/test_workflow_file_size_limit.py` fails at 480,000 (`agents.md:2527-2530`). No file can exceed 800 KB while CI passes.
  - Earlier measurements:
    - `implement.yml`: 402,475 bytes (`docs/plans/pin-promote-cycle-to-candidate-plan.md:128`).
    - `test-and-mark-stable.yml`: about 417,000 bytes (`analysis/workflow-optimization-2026-10-17.md:470`).
  - Both are near the 440,000-byte `CI_BUDGET_WORKFLOW_WARN_BYTES` early warning. Current sizes are [NEEDS VERIFICATION with `wc -c`].
- **Recommended fix:** Use DUP-006 (stable gate) and EXPR-003 (implement) as the next byte-reducing extractions, before either file crosses 440,000.

### Section 5: Cross-Cutting Concerns

**TODO/FIXME/HACK markers:** none. A comment-anchored search (`#.*\b(TODO|FIXME|HACK)\b`) over `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` returned no matches.

#### DEAD-001 — `gh_rate_limit_breaker_tripped` has no callers
- **File:** `scripts/gh_helpers.sh:336-341` (context `:313-318`)
- **Severity:** Low · **Category:** `dead-code`
- **Description:** A search of `scripts/`, `.github/` and `tests/` finds only its definition and comments. The header admits its only consumer, the old `orchestrate_poll.yml` self-retrigger suppression, was removed. The live consumer of the breaker file is `gh_rate_limit_breaker_active` (`:894-902`).
- **Recommended fix:** Keep it for consumer-repo compatibility (§10), but mark it deprecated in the header and add a one-line contract test. Or remove it after confirming that no `workflow-templates/` consumer calls it.

#### DEBT-001 — `plan.yml` parses `implement.yml` source text to find fetched helpers
- **File:** `.github/workflows/plan.yml:2249-2266`, fallback at `:2276-2280`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:** The guard splits `implement.yml` on the literal `"      - name: Stage workflow support files\n"` and regex-matches `for f in ...; do` / `_fetched_scripts+=("${f}")` / `echo "scripts/..." >> "${FETCHED_MANIFEST}"`. Any rename, re-indent or extraction of that step (for example DUP-002) silently drops consumer repos into "blanket scripts/ rejection".
- **Recommended fix:** Publish the fetched-helper list as data (for example `scripts/implement_fetched_scripts.txt`) that both the implement staging step and this guard read. Add a CI test that the list is non-empty.

#### DEBT-002 — `review_collect_pr_metadata.sh` shadows `gh_retry` with an incompatible signature
- **File:** `scripts/review_collect_pr_metadata.sh:35`, `:69-74`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:** After `source "${SCRIPT_DIR}/gh_helpers.sh"`, the script redefines `gh_retry()` as `(outfile, <gh args without "gh">)`. Shared helpers that call `gh_retry gh api ...` internally, such as `gh_review_pr_state` (`gh_helpers.sh:91`, `:107`), would write to a file named `gh` and run `gh api` if this script ever used them. This breaks the new-identifier uniqueness rule (unattended §10). It is latent today.
- **Recommended fix:** Rename the local wrapper (for example `_rcpm_gh_to_file`) and update its 7 call sites (`:138`, `:194`, `:231`, `:235`, `:240`, `:247`, `:280`).

#### CONSIST-001 — Label colors and descriptions drift from `label_contract.v1.json`
- **File:** `.github/workflows/implement.yml:5448-5449`, `:5582-5583` (`ai:closed` color `6b7280`, description "Issue closed by automation …"), `:5495-5496` (`ai:needs-human` color `b60205`), `:6765-6766` (`ai:blocked` description "Blocked: human input required")
- **Severity:** Low · **Category:** `consistency`
- **Description:** The contract has `ai:closed` = `6a737d` / "Linked PR closed without merge" (`.github/ai/label_contract.v1.json:156-158`), `ai:needs-human` = `e11d48` (`:100-102`), and `ai:blocked` = "Orchestrator auto-answer loop blocked pending human intervention" (`scripts/label_helpers.sh:115`). The first creator wins, so the repo label depends on which path ran first.
- **Recommended fix:** Replace the literal `gh label create` calls with `ensure_label_exists`. `label_helpers.sh` is already sourced in this job (`implement.yml:2270`).

#### CONSIST-002 — Ad-hoc raw `gh api` retry loop where the shared helpers apply
- **File:** `.github/workflows/implement.yml:2234-2245`
- **Severity:** Low · **Category:** `consistency`
- **Description:** "Fetch issue comments" retries `gh api --paginate .../comments | jq -s 'add // []'` 5 times with linear sleeps. It has no rate-limit detection, and failed-attempt bodies are not buffered. `agents.md:4718-4719` requires `gh_retry`; `plan.yml:730` uses `gh_retry` for the same endpoint.
- **Recommended fix:** Use `gh_retry_to_file "${ISSUE_COMMENTS_FILE}.pages" gh api --paginate ...`, then `jq -s 'add // []'`. Keep the outer loop only to wait for the approval comment to appear (eventual consistency).

#### SHELL-001 — Unquoted operands in `[ ]` tests (SC2086)
- **File:** `.github/workflows/test-and-mark-stable.yml:1009`, `:1301`, `:1664`, `:2648`; `scripts/stable_e2e_step_wait_review.sh:783`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** `if [ $IDLE -ge $INACTIVITY_LIMIT ]; then`. Both values come from arithmetic, so word-splitting risk is low. An empty value (for example a failed `PHASE_TIMEOUT` parse) gives a `[` syntax error instead of a clean timeout.
- **Recommended fix:** Use `[ "${IDLE}" -ge "${INACTIVITY_LIMIT}" ]` or `(( IDLE >= INACTIVITY_LIMIT ))`. Fold this into DUP-006.

#### SHELL-002 — Notification steps source a helper without a guard and without `set -euo pipefail`
- **File:** `.github/workflows/review_autofix.yml:5115`, `:5268`
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** `source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"` has no guard. These `run:` blocks use the default `bash -e`, without `pipefail`. Inference: a missing helper would fail a success-path Telegram step and flip the job to failure, which triggers the `failure()` review-blocked steps (`:5500-5544`). Risk is reduced because `tg_helpers.sh` is in `REQUIRED_BOOTSTRAP_SCRIPTS` (`scripts/stage_workflow_support.sh:57`); [NEEDS VERIFICATION] for consumer-repo support refs.
- **Recommended fix:** Use `source ... 2>/dev/null || { echo "::warning::tg_helpers.sh unavailable; skipping alert"; exit 0; }` and add `set -euo pipefail`, matching the guarded pattern at `:5116-5117`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 11 | BUG-001, BUG-002, SEC-001, API-004, BATCH-001, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-002, EXPR-003 |
| Low | 22 | BUG-003, BUG-004, BUG-005, BUG-006, API-001, API-002, API-003, API-005, API-006, BATCH-002, DUP-004, DUP-005, DUP-006, EXPR-004, EXPR-005, DEAD-001, DEBT-001, DEBT-002, CONSIST-001, CONSIST-002, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | None at Critical/High. The highest-priority (Medium) bugs touch `.github/workflows/review_autofix.yml`, `scripts/gh_helpers.sh`, `.github/workflows/orchestrate_poll.yml` | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/label_helpers.sh`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml` | Medium |
| Code modularization | 11 workflows (support-ref resolver), 8 workflows (support staging), `scripts/stage_workflow_support.sh`, new composite actions under `.github/actions/`, `scripts/label_helpers.sh` and its shims, `test-and-mark-stable.yml`, `comprehensive-test-and-release.yml`, related contract tests | Large |
| Expression size reduction | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml` | Small |
| Medium/Low fixes | `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml`, `scripts/stable_e2e_step_wait_review.sh`, `scripts/review_autofix_step_post_commit_retrigger.sh`, `scripts/review_collect_pr_metadata.sh`, `scripts/gh_helpers.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-24)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: the implement stage may apply the fix without further review. Every precondition holds: same endpoint or a strict field subset, identical filters, pagination and auth, the same function or step, no mutation in between, identical error and retry behaviour, no concurrency boundary crossed, and any cache contract kept.
- **`NEEDS_VERIFICATION`**: the fix looks correct, but static reading cannot prove every precondition. The named verification steps must pass first.
- **`RISKY_SKIP`**: listed for visibility only and must not be auto-implemented. The call sits in a retry or stall-recovery path, an identity or rate-limit probe, a paginated read, `orchestrate_poll_process.sh`, a path that defends against races, or a path whose log keys would change.

### Consolidation Candidates (MERGE-###)

#### MERGE-001 — The reclarify sweep reads `GET /rate_limit` twice per poll tick
- **Safety tag:** `RISKY_SKIP`
- **Files:** `scripts/orchestrate_poll_process.sh:44` (`replay_failed_reclarify_commands`) and `scripts/orchestrate_poll_process.sh:157` (`detect_unrouted_blocked_comments`). Both are called back-to-back at `:209-211` under `RECLARIFY_REQUEUE_SWEEP_ONLY=true`, from the step `.github/workflows/orchestrate_poll.yml:900-925`, which runs on every cron tick.
- **Calls:** currently 2 per tick; proposed 1.
- **Endpoint:** `GET /rate_limit`.
- **Evidence:**
  ```bash
  budget="$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null)" || return 0
  ...
  budget="$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null)" || budget=""
  ```
  The two functions handle a failed read differently. The first returns silently. The second logs `RECLARIFY_UNROUTED outcome=skip reason=budget_low remaining=...`.
- **Proposed fix:** read once inside the `RECLARIFY_REQUEUE_SWEEP_ONLY` block (`:209`) and pass the value to both functions as an optional argument, keeping the live read as a fallback when the argument is empty.
- **Safety rationale:** both calls are rate-limit probes, they sit in `orchestrate_poll_process.sh`, and the second read is fresher by design because the replay loop can POST comments in between.
- **Downstream signal:** do not auto-implement. `/rate_limit` reads do not count against the core quota, so the saving is one round trip per tick, not budget. A human must decide whether a post-replay budget reading is still needed and must keep the `reason=budget_low` log line.

#### MERGE-002 — Two label-scoped issue listings in the same sweep
- **Safety tag:** `RISKY_SKIP`
- **Files:** `scripts/orchestrate_poll_process.sh:46-47` (`--label ai:reclarify-requeue --json number --limit 1000`) and `:162-163` (`--label ai:blocked --json number,labels --limit 1000`).
- **Calls:** currently 2 listings per tick (each paginated by `gh` at `--limit 1000`); proposed 1 aliased GraphQL query (`requeue: issues(labels:["ai:reclarify-requeue"], states:OPEN, first:100)`, `blocked: issues(labels:["ai:blocked"], states:OPEN, first:100){nodes{number labels}}`).
- **Endpoints:** `GET /repos/{o}/{r}/issues?labels=...`, issued twice.
- **Evidence:** both functions run in one process (`:210-211`) on the same repo, with only the label filter differing. The blocked set explicitly excludes `ai:reclarify-requeue` issues (`:167`), so the two sets are related but neither is a subset of the other.
- **Proposed fix:** one aliased GraphQL query modelled on `_fetch_candidate_issue_details_graphql`, with its result passed to both functions.
- **Safety rationale:** three skip triggers apply:
  - `--limit 1000` paginates.
  - The code sits in `orchestrate_poll_process.sh`.
  - The fix moves the reads from the REST core bucket to the GraphQL bucket, but the `budget` gates at `:44`/`:157` check only `core.remaining`, so the gate would no longer protect the call.
- **Downstream signal:** do not auto-implement. This needs a manual decision on a GraphQL budget gate and on `first:100` page boundaries compared with `--limit 1000`.

#### MERGE-003 — `_has_active_autofix_run` lists runs once per workflow file
- **Safety tag:** `RISKY_SKIP`
- **Files:**
  - Loop: `scripts/orchestrate_poll_process.sh:23007-23020`.
  - Callers: `:12897`, `:12976` and `:23429`.
  - Existing single-call pattern: `_direct_inflight_review_run_on_branch` at `:16039-16043`.
- **Calls:** currently 3 `gh run list --workflow <file> --branch <ref> --limit 5` per invocation, plus the PR-named lookup at `:23034`. Inference: `gh run list --workflow` also resolves the workflow file through the API first, so it may be up to 6 requests. Proposed: 1 branch-scoped `gh run list --branch <ref> --json status,name,workflowName`, plus the unchanged PR-named lookup.
- **Endpoints:** `GET /repos/{o}/{r}/actions/workflows/{file}/runs?branch=...`, three times.
- **Evidence:**
  ```bash
  for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do
      active="$(gh_retry gh run list --repo "${GITHUB_REPOSITORY}" \
          --workflow "${wf_candidate}" \
          --branch "${head_ref}" \
  ```
  The header of the sibling helper (`:15966-15972`) already justifies "a single branch-scoped `gh run list` ... rather than the 3-call per-workflow loop".
- **Proposed fix:** have `_has_active_autofix_run` reuse the listing and name filter from `_direct_inflight_review_run_on_branch` (`:16054-16063`), keeping the PR-named fallback.
- **Safety rationale:** this is `orchestrate_poll_process.sh` on a path that defends against duplicate dispatch races. The comment at `:12973-12974` says the dispatch helper "checks again afterward to cover a concurrent start". The change would also replace file-path matching with display-name matching, which can miss renamed consumer workflows (`:15983-15993`).
- **Downstream signal:** do not auto-implement. A human must confirm that every consumer repo's review workflow display names match the name filter at `:16061-16063`, or must keep a file-path match, before the three calls are collapsed.

#### MERGE-004 — `review_enable_auto_merge.sh` reads PR labels and PR metadata separately
- **Safety tag:** `RISKY_SKIP`
- **Files:**
  - `scripts/review_enable_auto_merge.sh:155`: paginated `issues/{n}/labels?per_page=100`.
  - `:221`: full `pulls/{n}`.
  - `:100`: `pulls/{n}` `.head.sha`, used only on the early-exit paths at `:112` and `:158`.
- **Calls:** currently 2 or more on the main path (label pages plus the PR read) and 2 or more on the e2e-label path (label pages plus the head read). Proposed: 1 `pulls/{n}` read placed before `:155`, supplying `.labels[].name`, `.head.ref` and `.head.sha`.
- **Endpoints:** `GET /repos/{o}/{r}/issues/{n}/labels` (paginated) and `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** the comment at `:98-99` acknowledges the split ("The full PR-metadata call below is unreachable on these intentional early-exit paths"). The labels read is paginated on purpose: the comment at `:144-153` cites PRs with more than 30 labels and Copilot review #3 on PR #1823.
- **Safety rationale:** the labels read paginates, and merging would change the logged warning text on failure (`:168` compared with `:224`). Neither static reading nor the repo shows whether the `labels` array in a REST `pulls/{n}` response is complete for PRs with many labels.
- **Downstream signal:** do not auto-implement. A human must confirm from GitHub REST docs that the `labels` array in `pulls/{n}` is not truncated. They must also accept the change in log text and keep fail-closed `exit 0` on every path.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001 — `GET /user` read up to 3 times in one implement job
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:**
  - `.github/workflows/implement.yml:856` ("Hand off CI-triage issue whose PR is no longer open", conditional).
  - `:2092` ("Fetch issue metadata", runs on every non-skipped job).
  - `:2424` ("Unblock judge bulk-delete override", only when the `override=bulk_delete -->` marker is present).
  - Every step uses `GH_TOKEN: ${{ secrets.GH_PAT }}` (`:807`, `:2040`, `:2418`) in job `implement`.
- **Calls:** currently up to 3; proposed 1, with a live fallback when the cached value is empty.
- **Endpoint:** `GET /user`.
- **Evidence:** `:2092` sets `pipeline_login="$(gh api user --jq .login 2>/dev/null || true)"` but never exports it. `:2424` repeats `pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"`. The comment at `:2089-2091` explains only why `:2092` cannot reuse `:2424`; nothing covers the reverse direction.
- **Proposed fix:** after `:2092`, validate the login against `^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$` (the regex `clarify.yml:765` uses) and write it to a new GITHUB_ENV key. Before choosing a name, check that it is not already used in `implement.yml` or the staged helpers. At `:2424`, use that key and fall back to the live call when it is empty, so the `UNBLOCK_BULK_DELETE_OVERRIDE outcome=skip reason=login_unavailable` path is unchanged.
- **Safety rationale:** same endpoint, token and job, and a token's identity cannot change mid-job. But the calls are in different steps, which fails the same-step precondition. The login also decides trust for a marker that lifts the bulk-delete guard, so the cached value must not be writable by any step in between.
- **Downstream signal:** before implementing, confirm two things.
  1. No step between `:2092` and `:2424` runs branch-controlled code that can append to `$GITHUB_ENV`. The editor runs later, at `:2801`.
  2. The validated login is written only when it matches the regex.

#### REUSE-002 — First hop of the no-op ancestor walk re-reads the current issue body
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files:**
  - Walk: `.github/workflows/implement.yml:5556-5557`.
  - Cached snapshot: written at `:208-210`, and refreshed at `:2279`, to `ISSUE_META_FILE`/`ISSUE_BODY_FILE`, which `:300-301` export to the job env.
- **Calls:** currently 1 issue GET for hop 1 (where `current=${ISSUE_NUMBER}`); proposed 0 for hop 1. Later hops are unchanged.
- **Endpoint:** `GET /repos/{o}/{r}/issues/{n}`.
- **Evidence:**
  ```bash
  current="${ISSUE_NUMBER}"
  for hop in $(seq 1 "${threshold}"); do
    parent_body="$(gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
  ```
  On the first iteration this reads the same issue whose body `:2279` has already written to `ISSUE_META_FILE`. Only the `Re-issued from #N` line is used (`:5558`).
- **Proposed fix:** on `hop == 1`, read `.body` from `ISSUE_META_FILE` after checking that `.number` matches `ISSUE_NUMBER` (the `issue_meta_matches_issue` shape at `:2047-2056`). Fall back to the live GET when the check fails.
- **Safety rationale:** the files live under `/tmp/codex-implement-${GITHUB_RUN_ID}`, and the Codex editor runs between `:2279` and `:5405` (`:2801`). The output decides whether the issue is closed with `ai:closed`. Same endpoint, but trust in the file is unproven.
- **Downstream signal:** before implementing, confirm the editor sandbox cannot write `RUNTIME_DIR`. If it can, keep the live read. This complements the Deep Audit's API-002 and is independent of it.

#### REUSE-003 — Review-blocked judge re-reads PR state it fetched moments earlier
- **Safety tag:** `RISKY_SKIP`
- **Files:**
  - `scripts/review_rb_judge.sh:1927` (`_guard_pr_meta`, full `pulls/{n}`, unset at `:1940`).
  - `:2345` (`PR_STATE` from `pulls/{n}` `.state`, on the `fix` + `IS_FINAL` "treating as merge" path).
- **Calls:** currently 2 on that path; proposed 1, by keeping `.state` from `:1927` in a new variable.
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:** between `:1927` and `:2345` this script only posts comments (`:1955`, `:2089`, `:2101`); no merge, push or close runs on this path.
- **Safety rationale:** the comment at `:1917-1923` says `:1927` exists to catch an asynchronous auto-merge race, and the script runs inside a retry-once wrapper (`review_autofix.yml:4385-4390`). That is a race-defending path inside a retry path.
- **Downstream signal:** do not auto-implement. The saving is one call on a rare terminal path. A human must accept that `:2345` would no longer see a state change in the seconds after `:1927`.

### Dead Calls (DEAD-API-###)
No findings.

I checked these and none is dead:
- `implement.yml:2321-2323`: only runs when `ISSUE_META_FILE` fails to parse.
- `review_autofix_step_mark_ready_to_merge.sh:62` and `review_rb_judge.sh:1139`: only run when the cached PR text is empty.
- `workflow_failure_heal_report.sh:210` and `clarify.yml:820`: already reuse an earlier login.

Each one is a fail-open fallback whose result is used when it runs.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — the `scripts/label_helpers.sh:163-197` path has no cache, which I confirmed.
  - Net-new: each workflow step is a new shell, so a per-process `_ENSURED_LABELS_CACHE` saves calls only when the same label is ensured twice in one step. One such case is `set_issue_phase_label_resilient` (`:223`) after a caller's own `ensure_label_exists`. Savings per run are smaller than the call-site counts suggest.
- API-002: RISKY_SKIP — the walk uses `--paginate` (`implement.yml:5561`).
  - Net-new: a GraphQL `comments(last:N)` window would change a full-history scan into a partial one, so a no-op marker older than N would be missed. See REUSE-002 for the safe part.
- API-003: RISKY_SKIP — this is in `orchestrate_poll_process.sh:46-58`, and the header at `:37-41` describes the live per-issue read as intended.
- API-004: RISKY_SKIP — paginated reads (`:23713-23714`) in `orchestrate_poll_process.sh`, with state comments written within the same cycle.
- API-005: RISKY_SKIP — this is the stall-recovery gate at `orchestrate_poll_process.sh:19868-19873`.
- BATCH-001: RISKY_SKIP — a per-row loop at `orchestrate_poll_process.sh:7948`, followed by a paginated comments read at `:7961`. I agree with the GraphQL batching direction, but it needs manual review.
- BATCH-002: NEEDS_VERIFICATION — confirmed at `.github/workflows/orchestrate_poll.yml:288`, `:301-306` and `:366-371`.
  - Net-new detail, part 1 — downstream file shapes:
    - `provider_outage_trackers.json` is read at `:1141-1143` with REST field names `.pull_request` and `.author_association`.
    - `tracking_issues.json` is read as `.labels[].name` at `:1045`/`:1048`, by `--tracking-issues-file` at `:1859`, and at `orchestrate_poll_process.sh:5826`, `:21011` and `:23672`.

    A merged GraphQL response must be reshaped to those exact formats.
  - Net-new detail, part 2 — failure behaviour: the outage listing fails open (`:294-296`). The tracking listing is unguarded under `set -euo pipefail` (`:274`, `:301`), so its failure fails the step. One merged call cannot keep both behaviours without per-alias error handling.
- **Reviewed but not agreed — API-006:** the premise is contradicted. `implement.yml:207-210` already writes the precheck payload to `${RUNTIME_DIR}/issue_meta.json`. The runtime dir is created at `:196-197`, the same path exported at `:287`/`:300`. `:974` and `:2058` fetch only when that file is invalid. No change is needed.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | REUSE-001, REUSE-002 |
| RISKY_SKIP | 5 | MERGE-001, MERGE-002, MERGE-003, MERGE-004, REUSE-003 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.