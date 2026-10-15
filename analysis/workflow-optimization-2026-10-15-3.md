## Executive Summary

> **Evidence boundary:** The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) is not in this sandbox. Glob returned `Directory does not exist`, so I could not read `summary.json`, `errors/`, `slow/` or `recent/`. Everything below comes from `analysis_context` (aggregates, `failure_point`, `log_summary`) plus repository source I read directly. Root causes for the failed runs are therefore **not log-confirmed**, so most fixes here are diagnostic-logging additions that will make next week's analysis conclusive. `insufficient_data=false`. The window is 1,000 runs, roughly 03:49Z–12:04Z on 2026-10-15 (inferred from `created_at`).

- **Heal-intake concurrency is likely dropping failure reports (high impact, medium confidence).** `workflow_failure_heal_intake` cancelled 84 of 158 runs (53%), in bursts: 8 runs at 11:22:29–11:23:00 (e.g. 38726871044…38726933543) and 6 at 11:36:13–11:36:27 (38728506961…38728534906). `.github/workflows/workflow-failure-heal-intake.yml:71-73` puts every non-schedule event, including every `workflow_run: completed` of CI, into the single group `workflow-failure-heal-intake` with `cancel-in-progress: false`. In that setup GitHub keeps one running run plus one pending run and cancels any older pending run. The job `if:` (lines 86-95) runs later, so no-op events can evict a real pending report. Moving the concurrency group to job level stops no-op evictions at no extra cost.
- **The review/autofix long tail is the main latency and runner-cost sink (high impact, high confidence on the numbers).** p50 793s, p95 3,259.6s, max 5,254s. Four runs were cancelled after 1,169–1,827s (5,899 runner-seconds lost), and run 38711491856 failed after 5,254s. The recorded failure point for that run and for 38682341980 (514s) is `review / codex-agent` → `Checkout PR head branch`. That step has 5 untagged `exit 1` paths (`review_autofix.yml` lines 2811, 2816, 2822, 2865, 2896) plus several `set -e` exits. Adding a `REVIEW_CHECKOUT_FAIL reason=` line would make the cause countable.
- **The two CI failures are the agents.md policy gate working as designed (medium impact, high confidence).** Runs 38686808723 and 38686824470 failed at `static-checks` / `agents.md direct-edit check`. `scripts/check_agents_md_direct_edits.py` is called only from `ci.yml:277`, so implement/autofix agents first learn about a violation after a ~13-minute CI run. Running the check before push would turn these into in-loop repairs.
- **Prompt-cache hit rate is 0.669, but uncached input is ~150k tokens per call (medium impact, medium confidence).** Over 19 OpenRouter calls: 2,863,832 uncached prompt tokens, 7,423,096 cache-read, 811,166 cache-write, 11.26M total. That is 1,216,842/8 and 1,646,990/11 uncached per call in runs 38711520263 and 38678256501. The per-reviewer-role split (`or_phases`) is needed to find which variable block is uncached.
- **Cost telemetry is incomplete in three ways (high confidence that the gap exists):**
  - `codex_tokens_used` = 6 for 14 `codex_calls` (e.g. plan 38728850218: 4 calls, 2 tokens). The `CODEX_TOKENS_RE` match is not real usage.
  - Claude usage (`claude_*`, which heal intake (`engine=claude model=claude-opus-5-5`) and the unblock judge use) is absent from the run rows.
  - Only 2 of 74 review runs carry token data.
- **The GH_PAT budget is healthy but runs at ~38% of the hourly limit, and per-job attribution is broken for heal intake (low–medium impact).** On one rate-limit window (reset 1792064625, 11:43:45Z), `remaining` fell from 4,972 to 3,192 in ~56 minutes, about 1,780 calls. All 4 intake `GH_PAT_BUDGET` end lines show `used_in_job=unknown`.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Review/autofix paired-run waste (critical path).**
   - **Evidence:** Each review trigger produces an unnamed run plus a `[pr:N]` run about 10–15s apart:
     - 38711491856 (09:14:33, failed after 5,254s) and 38711520263 [pr:7429] (09:14:47, 4,818s)
     - 38678237076 and 38678256501 [pr:7415]
     - 38681989850 and 38682010986 [pr:7415]
     - 38685354699 and 38685378063 [pr:7415]
     - 38695382346 and 38695407859 [pr:7424]

     In 4 pairs the second run was cancelled late: 38690979393 at 1,827s, 38686824540 at 1,533s, 38706227304 at 1,370s and 38711227077 at 1,169s.
   - **Root cause (inference):** A per-PR concurrency or supersede rule cancels a run only after it has done most of its reviewer work.
   - **Change:** Log a structured line at job start: event name, PR number, head SHA, concurrency group, and whether a sibling run for the same head SHA is in flight. This tells us whether the pairs are the same PR (duplicate work) or different PRs (legitimate). If they are duplicates, deduplicate on head SHA in the existing `gate` job before codex-agent starts.
   - **Estimated saving:** up to ~5,900 runner-seconds per 8h window from cancellations alone, more if the pairs are duplicates.
   - **Risk:** low for logging; medium for any dedup change.
2. **Orchestrate poller runs close to its own interval.**
   - **Evidence:** p50 654s, p95 870.8s, 5 of 47 cancelled. Observed starts are 9–15 minutes apart (10:45:48, 10:54:39, 11:11:26, 11:26:21, 11:40:34, 11:50:21), and each log has ~819k characters truncated (38730190144, 38729018208, 38727332465, 38725564858). No summary could identify the dominant sub-step. Grep found no per-phase timing prefix in `scripts/orchestrate_poll_process.sh`.
   - **Change:** Emit one `ORCH_POLL_PHASE name=<phase> ms=<n>` line per phase plus an end summary. Register the prefix through an `agents.d/` fragment under "Stable log prefixes (contractual)" (§25). The end of the log survives truncation, so the summary stays visible.
   - **Estimated saving:** none directly. It unlocks targeted cuts to a 10–11 minute critical path that drives the 5 cancellations.
   - **Risk:** very low.
3. **`heal-pr-reconcile` full-history checkout (micro).**
   - **Evidence:** Run 38722095165 spent ~65s of 72s on checkout and tag fetch (log ~536k chars), then logged `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`. `internal-cancel-on-pr-close.yml:45-50` uses `fetch-depth: 0` on every PR close.
   - **Change:** Add a cheap probe step that checks for heal PRs before the full-history checkout, and skip the checkout when there are none. Keep `fetch-depth: 0` when work exists, because the merged-PR path moves commits.
   - **Estimated saving:** ~60s per PR close.
   - **Risk:** low.
4. **CI (p50 801s): no action.** `lint` and `budget-watch` deliberately `need: static-checks` (`ci.yml:2192`, `ci.yml:2229`; `lint` is the aggregate status). `CI_BUDGET findings=0 … job_ratio=0.75` in 38722095749 means no job is near its timeout.

## Cost Optimizations

1. **Locate the ~150k uncached prompt tokens per review call.**
   - **Evidence:** Aggregate `cache_hit_rate` is 0.668862. Uncached `or_prompt_tokens` is 2,863,832 against 7,423,096 cache-read tokens, i.e. 25.8% of input is uncached. Cache writes are 811,166, so reads are 9.2× writes, which means prefixes are being reused. The uncached part is the variable suffix.
   - **Change:** Break `cost_audit.py` `or_phases` down per reviewer/editor role in the report. If the diff or targeted-file context is the uncached block, put a stable per-PR block (base-file context) before the per-iteration diff so iterations 2–5 of the autofix loop read it from cache.
   - **Estimated saving (inference):** moving half the uncached input (~1.4M tokens per 2 runs) to cache reads saves ~10–12% of review input cost.
   - **Quality risk:** none if content is unchanged and only reordered.
2. **Semble is not a cost driver.** There were 2 queries, 31,444 bytes, 15 sources and `static_dup_bytes_total=0`. That is roughly 4k tokens per run against more than 1.2M uncached tokens per run. It adds context cheaply with no duplication. Its cost is latency: bootstraps took 43,226ms and 36,952ms for one query each. If the index can be keyed on base SHA and reused, that saves ~40s per review run. It is not on the token-cost path.
3. **Serena is disabled.** `SERENA_ENABLED: false` (38722095123) and all `serena_*` counters are 0, so there is nothing to evaluate.
4. **Fix the cost blind spots before tuning models.**
   - `codex_tokens_used` is 2 per run for 4–6 calls (clarify 38727564233, plan 38728850218, clarify-respond 38728098881). `CODEX_TOKENS_RE` (`cost_audit.py:62-64`) is matching non-usage text. Have the codex wrapper emit one structured `CODEX_USAGE total_tokens=<n>` line per call (the prefix is unused; grep found none) and parse that first, falling back to the regex.
   - Claude spend from heal intake (`model=claude-opus-5-5 effort=high`) and the unblock judge is missing from the run rows, although `cost_audit.py` computes `CLAUDE_COUNT_KEYS` and `claude_cost_usd`. Add them to the collector's `cost_telemetry`.
5. **Model and reasoning choice: no recommendation.** Everything uses `gpt-6-sol` at `high`. With no per-call usage outside two review runs, any downgrade would be unsupported.

## Reliability Improvements

Ranked by expected failure-rate and rerun reduction.

1. **Heal-intake evictions: likely silent data loss.**
   - **Evidence:** 84 cancelled and 46 skipped out of 158. The cancelled runs last 1–12s, consistent with a pending run being replaced. The workflow comment at lines 68-70 acknowledges that "a newly queued group member replaces the pending one". The workflow-level group also covers `workflow_run` completions of CI, which the job-level `if` (lines 89-93) then skips unless the CI run was a push to the default branch.
   - **Category:** config / concurrency semantics.
   - **Fix (smallest safe):** Give non-schedule runs a per-run group at workflow level and add the shared group to the `intake` job. The `recovery-scan` job already does this at line 352. Then an event that is filtered out by `if` never enters the queue.
     ```yaml
     concurrency:
       group: ${{ github.event_name == 'schedule' && format('workflow-failure-heal-recovery-list-{0}', github.run_id) || format('workflow-failure-heal-intake-run-{0}', github.run_id) }}
       cancel-in-progress: false
     jobs:
       intake:
         if: >- ...unchanged...
         concurrency:
           group: workflow-failure-heal-intake
           cancel-in-progress: false
     ```
   - **Residual risk:** genuine bursts of more than 2 real reports still drop the middle ones. To measure this, `workflow_failure_heal_report.sh` should log the fingerprint it sends and intake should log the fingerprint it receives (both under the existing `WORKFLOW_HEAL` prefix), so a diff counts losses.
   - **Rollback:** revert the expression; the serialisation guarantee is unchanged.
2. **Heal-intake hard failures (6 runs, 174–309s, step `Run workflow failure heal intake`):** 38686057824, 38685785879, 38686754997, 38696241876, 38695652241, 38687951703.
   - `scripts/workflow_failure_heal_intake.sh:159` runs with `set -euo pipefail`. Only some exits log `WORKFLOW_HEAL error …` first (lines 275, 1016, 1028, 1370, 2313). A failing command anywhere else exits with no reason line.
   - **Fix:** add an ERR trap that uses the existing `log()` prefix, so no new identifier is needed. Use `set -E` so it fires inside functions. It is separate from the conditional EXIT trap at lines 683/711.
     ```bash
     set -E
     trap 'rc=$?; log "error stage=unhandled line=${LINENO} cmd=${BASH_COMMAND%% *} exit=${rc}"' ERR
     ```
   - **Expected impact:** every failure becomes classifiable. The fix itself does not change behaviour.
3. **`Checkout PR head branch` failures (38711491856 at 5,254s, 38682341980 at 514s).**
   - Exit paths in `review_autofix.yml`:
     - 2811: workspace unavailable
     - 2816: OpenCode config
     - 2822: `HEAD_REF` null; plain `echo`, no `::error::`
     - 2865: fetch failed after 4 tries
     - 2896: head unreadable
     - Implicit `set -e` exits at 2784 (`jq` on `PR_META_FILE`), 2842 (`git_ref_health_check.sh repair`), 2874 (`git reset --hard`) and 2892 (`git checkout -f -B`)
   - **Fix:** before each explicit `exit 1`, emit `REVIEW_CHECKOUT_FAIL reason=<workspace_unavailable|opencode_config|head_ref_missing|fetch_exhausted|head_unreadable> pr=${PR_NUMBER:-} head_ref=${HEAD_REF:-}`, and add an ERR trap with `reason=unhandled line=${LINENO}`. The new prefix (no existing match; `summarize_reviewer_consensus.sh` matched only `used_reason=`) needs an `agents.d/` fragment.
   - **Open question:** a failure at checkout should not take 5,254s. Either earlier steps ran ~87 minutes or `failure_point` is attributed wrongly. Logs are needed to tell which.
4. **Agents.md gate caught after CI (2 of 39 CI runs, 5.1%).**
   - The policy failure itself is correct.
   - **Fix:** in the implement and review-autofix pre-push guards, run `scripts/check_agents_md_direct_edits.py --base <base> --head HEAD` and feed any violation back to the editor. Fail open with a `::warning::` if the script is missing.
   - **Expected impact:** removes this class of CI failure plus the follow-up autofix iteration (~13 minutes of CI each).
5. **MCP fallbacks are healthy.** All `semble_fallbacks`=12 are `semble_contract_test_fallbacks`, 4 per CI run in 38686808723, 38686824470 and 38722095749. These are contract tests exercising fail-open paths. `semble_runtime_fallbacks`=0 and `semble_bootstraps_failed`=0. Orchestrate-poll's `SEMBLE_AVAILABLE: false` with `SEMBLE_BOOTSTRAP_MODE: lazy` is expected before first use, not a masked broken rollout. There were no `SERENA_PROBE` lines.
6. **BREAK_GLASS and CONTEXT_BUDGET_WARN are 0 everywhere,** so there is no policy or prompt-size pressure to report.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary`, and the deep-dive logs were not readable. Retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` and `enabled:false` cannot be assessed. Clarify 38727564233 shows `MEMORY_INJECTION_SCAN_ENABLED=true`, so memory is configured. The plan run 38728850218 lost 847,721 characters to truncation and clarify lost ~285,955, which is where `retrieve` lines would appear.

**Recommendation:** have the collector extract `AI_MEMORY_TELEMETRY:` lines from full logs before truncation, as it already does for `SEMBLE_*`. Then confirm emission in one clarify and one plan run.

## GH API Call Audit

| Source | Workflow / job | `remaining` (reset) | `used_in_job` |
|---|---|---|---|
| 38722095123 | review_autofix / post-merge-force-poll | 4,972 (1792064625) | 3 |
| 38724272137 | review_autofix_sweep / sweep | 4,332 (1792064625) | 12 |
| 38726858241 | heal intake | 3,655 (1792064625) | unknown |
| 38728161313 | review_autofix_sweep / sweep | 3,390 (1792064625) | 14 |
| 38728487076 | heal intake | 3,192 (1792064625) | unknown |
| 38726937241 | heal intake | 4,983 (1792067222) | unknown |
| 38728549549 | heal intake | 4,671 (1792067222) | unknown |
| 38731458090 | review_autofix_sweep / sweep | 4,287 (1792068246) | 5 |

- **Burn rate:** about 1,780 calls in ~56 minutes on the 11:43:45Z window, roughly 38% of 5,000/h. There were no 429s or secondary rate-limit events in any summary. The sweeps (5–14 calls) and force-poll (3) are cheap. The consumer is invisible, which suggests orchestrate-poll and review/autofix, but that is an inference.
- **Attribution bug:** `gh_pat_budget` (`scripts/gh_helpers.sh:66-73`) silently reports `unknown` whenever the start snapshot is missing or `reset` differs. All 4 intake runs show `unknown`. Two different reset values (1792064625 and 1792067222) appear for the same workflow within minutes, which suggests either a second identity or a broken snapshot handoff (inference). The start step reads the JSON body (`workflow-failure-heal-intake.yml:141`), while the end step prefers response headers (`gh_helpers.sh:52-60`).
- **Fix:** append fields to the existing line (additive; `_extract_log_field` parses by key): `start_remaining=`, `start_reset=`, `unknown_reason=<snapshot_missing|reset_changed|remaining_increased>`.
- **Missed batching:** none could be confirmed from this data. `historical triage` 38722096039 makes 2–3 single calls per run, which is appropriate. Record `used_in_job` for the poll job (start and end steps exist at `orchestrate_poll.yml:97-111` and `:1887-1897`) and include it in the collector output. The poll lines are currently lost to truncation.

## Prompt Cache & Memory System

- `cache_hit_rate`=0.669 (review_autofix only; null elsewhere because there is no OpenRouter usage). Per run: 0.655958 (38711520263) and 0.678333 (38678256501). The two runs are close, so prefix stability is consistent.
- **Fragmentation signal:** a read:write ratio of 9.2:1 alongside ~150k uncached tokens per call. The stable prefix caches well, and a large per-call variable region follows it. The likely candidates are the per-iteration diff and targeted-file context (inference). Fix as in Cost #1: order stable per-PR context before the per-iteration context.
- **OpenRouter cache config:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` in plan and clarify runs. `or_usage_unavailable_calls`=0, so usage reporting is complete for the calls that were captured.
- No `CONTEXT_BUDGET_WARN` lines appeared, so prompt growth is not yet eroding cache value.
- **Gap:** Claude-engine cache read and write are not surfaced in this report (see Cost #4).

## Orchestrator Health

- **Flow observed (issue #7434, a heal issue):**
  - clarify 38727564233 (11:28:20, 486s)
  - clarify-respond 38728098881 (11:32:46, 720s; Q1 put the root cause outside the frozen heal scope)
  - plan 38728850218 (11:39:10, 806s, auto-answered `/answer`)
  - implement 38730265778 (11:50:59, 40s, `/approved [auto-approved-by-plan]`)

  That is about 23 minutes from clarify to implement with no human input. The 40s implement is short (logs truncated, so unverified).
- **Stuck or terminal indicators:**
  - The unblock judge 38730065765 logged `outcome=skip reason=fixup_escalated` for item 7258.
  - The heal report 38730341116 logged `WORKFLOW_HEAL_REPORT skip reason=heal_scope_unverified issue=7434 label=ai:needs-human`.
  - Together these show #7434 was escalated to `ai:needs-human` and the report was skipped because its scope marker was not verified. Track the count of `heal_scope_unverified` skips per day.
- **Skipped fan-out noise:** 560 of 1,000 runs (56%) are skipped `if:` gates. Clarify, plan, implement and clarify_respond each fire on every issue comment (e.g. the 11:51:37 and 11:50:59 bursts). Skipped jobs use no runner time, but they cut the analysis window to about 8 hours. The collector should exclude `skipped` from the 1,000-run cap.
- **Cancel-on-PR-close** warned "preserved 3 active run(s) without pull-request linkage" in 4 runs (38730731687, 38729613288, 38728033186, 38726389681). Log the preserved run IDs and workflow names so a run that never finishes is visible.
- **Indicators to track:** intake cancelled/total, poll p95 against the cron interval, `ORCH_POLL_PHASE` per-phase ms, and `heal_scope_unverified` count.

## Pipeline Flow Bottlenecks

| Stage | Compute (p50 / p95 or observed) | Queue | Retry / rerun | Merge / conflict |
|---|---|---|---|---|
| clarify / clarify-respond / plan | 486–806s observed | ~3s runner wait | none seen | — |
| implement | 40s–1,754s (slow list) | — | — | — |
| review/autofix | 793s / 3,259.6s | not measured | 4 late cancels (5,899s), 1 failure (5,254s) | unknown |
| CI | 801s / 841.6s | ≤2s | 2 policy failures | — |
| orchestrate poll | 654s / 870.8s | ~70s queue once (38723588098) | 5 cancels | — |
| heal intake | 6s / 214.2s | concurrency-pending | 84 cancels, 6 failures | — |

Fixes ordered by end-to-end impact:
1. Diagnose and deduplicate review/autofix paired runs.
2. Run the agents.md check before push.
3. Fix heal-intake concurrency eviction.
4. Add poll phase timing.
5. Gate the reconcile checkout.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - review/autofix tail (p95 3,259.6s; 5,254s max)
  - orchestrate poll at 9–13 minutes per tick
  - CI ~13 minutes
- **Top failure modes:**
  - heal-intake eviction (84 cancels) and unlabeled intake failures (6)
  - review checkout failures (2)
  - agents.md CI gate (2)
- **Highest-cost drivers:** review_autofix OpenRouter input (11.26M tokens over 19 calls, 2.86M uncached). Claude-engine heal and judge spend is unmeasured.
- **Top 3 actions:**
  1. Move heal-intake concurrency to job level and add the ERR trap.
  2. Add `REVIEW_CHECKOUT_FAIL` and paired-run start logging in review_autofix.
  3. Fix codex/Claude token capture (`CODEX_USAGE`, collector `claude_*` fields) before any model tuning.

## Metrics Appendix

**Run outcomes by family** (S = success, F = failure, C = cancelled, O = other/skipped)

| Family | Total | S | F | C | O | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| review_autofix | 74 | 67 | 2 | 4 | 1 | 793 | 3,259.6 |
| workflow_failure_heal_intake | 158 | 22 | 6 | 84 | 46 | 6 | 214.2 |
| ci | 39 | 37 | 2 | 0 | 0 | 801 | 841.6 |
| orchestrate_poll | 47 | 42 | 0 | 5 | 0 | 654 | 870.8 |
| clarify | 116 | 5 | 0 | 0 | 111 | 1 | 11 |
| plan | 109 | 5 | 0 | 0 | 104 | 1 | 10.6 |
| implement | 109 | 5 | 0 | 0 | 104 | 1 | 11 |
| orchestrate_clarify_respond | 111 | 4 | 0 | 0 | 107 | 1 | 11 |
| workflow_failure_heal | 49 | 2 | 0 | 0 | 47 | 1 | 10.6 |
| check_failure_triage | 42 | 2 | 0 | 0 | 40 | 1 | 11 |
| cancel_on_pr_close | 43 | 43 | 0 | 0 | 0 | 14 | 120.8 |
| lint_pr_body_auto_close | 31 | 31 | 0 | 0 | 0 | 10 | 12.5 |
| integration_pr_readiness | 30 | 30 | 0 | 0 | 0 | 11 | 19.75 |
| issue_pr_status | 13 | 13 | 0 | 0 | 0 | 147 | 248.4 |
| unblock_judge_dispatch | 10 | 10 | 0 | 0 | 0 | 39 | 61.7 |
| other 8 families | 19 | 19 | 0 | 0 | 0 | — | — |
| **Total** | **1,000** | **337** | **10 (1.0%)** | **93** | **560** | **6** | **821** |

**Cost and cache**

| Metric | Value |
|---|---|
| or_calls / usage available | 19 / 19 |
| or_prompt / completion / total tokens | 2,863,832 / 165,270 / 11,263,230 |
| or_cache_write / cache_read | 811,166 / 7,423,096 |
| cache_hit_rate | 0.668862 |
| codex_calls / codex_tokens_used | 14 / 6 (parser artefact) |
| wall_clock_p50_ms / p99_ms (repo, n=119) | 10,000 / 4,566,720 |
| wall_clock_p50_ms / p99_ms (review_autofix, n=15) | 2,776,000 / 5,192,960 |
| break_glass_count / context_budget_warn_count | 0 / 0 |

**MCP**

| Metric | Value |
|---|---|
| semble_query_calls / bytes / sources | 2 / 31,444 / 15 |
| semble_bootstraps / ms / failed / unused | 2 / 80,178 / 0 / 0 |
| semble_fallbacks (contract-test / runtime) | 12 (12 / 0) |
| semble_static_dup_bytes / echo_lines_dropped | 0 / 0 |
| serena query / fallbacks / tool_calls / response_bytes | 0 / 0 / 0 / 0 |

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all workflows) | 0 | 0 | 0 (disabled: `SERENA_ENABLED: false`) |

Other MCP servers observed: none.

**GH API:** about 1,780 core calls between ~10:44Z and ~11:40Z on one identity, with 0 HTTP 429s and 0 secondary limits. `used_in_job` is known only for sweep (5/12/14) and force-poll (3).

**Assumptions and gaps**
- The run-logs mount is missing, so no deep-dive excerpts were read.
- Review/autofix pairing is inferred from timestamps only.
- The heal-intake eviction mechanism comes from the workflow config and GitHub concurrency semantics. The event type of the cancelled runs has not been verified. Next collection step: add `event` and `head_sha` to the collector's run rows.