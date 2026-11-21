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
## Deep Audit — Workflows & Scripts (2026-10-15)

> **Scope and method.** This is a depth-first audit. I read these files directly: `workflow-failure-heal-intake.yml`, `review_autofix.yml` (gate, deterministic-skip-merge, fingerprint-cap, checkout and mark-label steps), `orchestrate_poll.yml` (`find_tracking`, `stage_support`), `implement.yml` (no-op handler and baseline refctx), `clarify.yml` (`clarify_route`), `sync_ai_labels.yml`, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/post_review_comment.sh`, `scripts/review_merge_train.sh`, both `review_autofix_step_mark_*` scripts, and selected regions of `scripts/orchestrate_poll_process.sh` and `scripts/validate_process.sh`. Other files were only checked by grep.
>
> **Size estimates.** No byte counter was available, so the expression and file sizes below are estimated from line counts. Each one is marked `[NEEDS VERIFICATION]`.
>
> **Not repeated here because the sections above already cover them:**
> - heal-intake workflow-level concurrency eviction (`workflow-failure-heal-intake.yml:71-73`, Reliability #1);
> - untagged exits in `Checkout PR head branch` (`review_autofix.yml:2809-2896`, Reliability #3);
> - the heal-intake ERR trap (Reliability #2);
> - `GH_PAT_BUDGET used_in_job=unknown` attribution (`gh_helpers.sh:62-73`, GH API Call Audit). DUP-005 adds a related root-cause observation.

### Section 1: Bug & Correctness Sweep

**BUG-001** — `scripts/review_autofix_step_mark_ready_to_merge.sh:7,76` (callee `scripts/label_helpers.sh:184-190`; step `review_autofix.yml:4187-4209`)
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The script runs under `set -euo pipefail` (line 7) and calls `ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"` at line 76 without `|| true`.
  - When `label_helpers.sh` is present, its `ensure_label_exists` returns `1` for any failure other than "already exists" (`label_helpers.sh:189-190`). Examples are a 403, or a 5xx that persists after retries.
  - The step is sourced by `review_autofix.yml:4209` and has no `continue-on-error` (`:4199-4202`). One label-create failure therefore fails the whole codex-agent job at its last stage, after the full review and editor cycle, and leaves linked issues unlabelled.
  - The sibling script guards the same call: `review_autofix_step_mark_review_blocked_workflow_failure.sh:31` uses `ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true`.
- **Recommended fix:** Change line 76 to `ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}" || true`, matching the sibling at `:31`. `set_issue_phase_label_resilient` already calls `ensure_label_exists ... || true` per issue (`label_helpers.sh:216`), so nothing is lost.

**BUG-002** — `.github/workflows/implement.yml:5150-5151, 5156-5158` (step "Handle no-op implementation", `:4932-5167`)
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - The fall-through path runs under `set -euo pipefail` (`:4941`). Two calls in it are unguarded:
    - `gh_retry gh api "repos/…/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (`:5150-5151`). This is a non-idempotent POST, so `gh_retry` makes a single attempt (`gh_helpers.sh:1067-1069`).
    - `ensure_label_exists "ai:implementation-failed" …` (`:5158`), which returns 1 on failure (`label_helpers.sh:190`).
  - If either fails, the step exits before `gh issue edit … --add-label 'ai:implementation-failed'` (`:5164-5167`). The issue then never receives the label that the orchestrator's re-issue path keys on.
  - Every earlier guard branch in the same step ends its writes with `|| true` (`:4977, 4984, 4988, 5024, 5029, 5031`).
- **Recommended fix:** Append `|| echo "::warning::IMPLEMENT_NOOP comment_post_failed issue=${ISSUE_NUMBER}"` to `:5150-5151` and `|| true` to `:5158`. The label edit at `:5164-5167` then always runs. Log the failure with a structured prefix (CLAUDE.md §8).

**BUG-003** — `scripts/label_helpers.sh:216-262` (`set_issue_phase_label_resilient`)
- **Severity:** Medium · **Category:** `bug` (race / lost update)
- **Description:**
  - The function GETs the issue's labels (`:219-220`), computes `(. - $p) + [$t]` locally (`:234-235`), then PUTs the whole set (`:247-249`). A PUT to `/issues/N/labels` replaces every label.
  - Any label added between the GET and the PUT is silently dropped. Examples: an operator's `ai:needs-human`, `force-review`, `ai:engine-claude`, or a concurrent job's label.
  - This is a classic read-modify-write TOCTOU. The helper runs from many concurrent workflows (169 `ensure_label_exists`/`set_issue_phase_label_resilient` call sites across 24 files).
  - The poller's own phase setter avoids the race by sending one `gh issue edit --add-label … --remove-label …` with only the deltas (`orchestrate_poll_process.sh:4085-4108`).
- **Recommended fix:** Keep the GET only to find which phase labels are present. Replace the PUT with the delta form already used at `orchestrate_poll_process.sh:4106-4108`: `gh_retry gh issue edit "${issue_number}" --repo "${repo}" --add-label "${target_label}" "${remove_args[@]}"`, with `remove_args` limited to phase labels currently present. Keep the POST-add fallback. `[NEEDS VERIFICATION]`: confirm that `gh issue edit` sends add/remove deltas and not a full replacement in the pinned `gh` version.

**BUG-004** — `.github/workflows/orchestrate_poll.yml:257-267`
- **Severity:** Medium · **Category:** `bug` (silent truncation)
- **Description:**
  - `TRACKING_ISSUES="$(gh_retry gh issue list … --label "ai:orchestrator-tracking" --state open --json number,title,labels --limit 20)"` caps the poll at 20 open projects.
  - Nothing warns when the cap is hit. `COUNT` (`:264`) is logged but never compared with the limit.
  - With 21 or more open tracking issues, the projects beyond the first 20 returned are never polled. Their waves stall with no signal. That `gh issue list` defaults to newest-first, so the oldest projects are dropped, is an inference `[NEEDS VERIFICATION]`.
- **Recommended fix:** Add `ORCH_POLL_TRACKING_LIMIT: ${{ vars.ORCH_POLL_TRACKING_LIMIT || '100' }}` to the step `env:` and pass `--limit "${ORCH_POLL_TRACKING_LIMIT}"`. After `:264`, emit `ORCH_POLL_TRACKING_TRUNCATED count=${COUNT} limit=${ORCH_POLL_TRACKING_LIMIT}` as a `::warning::` when `COUNT -ge` the limit, and register the prefix through an `agents.d/` fragment (§25).

**BUG-005** — `scripts/orchestrate_poll_process.sh:4130-4133` (callers `:4083`, `:22625`)
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - `get_issue_labels_json` calls `gh_retry _safe_gh_jq "repos/…/issues/${issue_num}/labels" --jq '[.[].name]' || echo '[]'`. It has no `--paginate` (the default page is 30), and it turns every API failure into "no labels".
  - In `set_issue_phase_label` (`:4083-4095`), a failed read means no `--remove-label` arguments are built. Stale phase labels then stay alongside the new one, which is the exact failure the comment at `:4076-4081` says the read exists to prevent.
  - At `:22625`, `TRACKING_LABELS` feeds checks such as `has_label "${TRACKING_LABELS}" "ai:security-pass-failed"` (`:22653`). Those checks fail open on an API error.
- **Recommended fix:** Add `--paginate` and pipe through `jq -cs 'add // []'`, the same pattern as `label_helpers.sh:219-220`. Return a distinct non-zero status on failure instead of `[]`. In `set_issue_phase_label`, fall back to removing every label in `phase_changes.remove` and treat the "not found" error as non-fatal, as `:4111-4114` already does.

**SEC-001** — `.github/workflows/implement.yml:5078-5123`
- **Severity:** Low · **Category:** `security`
- **Description:**
  - The ancestor no-op cap counts any comment whose body matches `produced no repository changes` on each `Re-issued from #N` ancestor (`:5088-5089`). It does not filter on comment author.
  - A third-party commenter who can comment on an ancestor issue could plant that string. The next no-op attempt would then close the issue as "already implemented" (`:5099-5123`) instead of re-issuing it.
  - Compare `review_merge_train.sh:803-805`, which trusts only comments from the resolved automation login.
  - The impact depends on repository visibility and who can comment `[NEEDS VERIFICATION]`.
- **Recommended fix:** Restrict the `--jq` filter to `.user.login == <GH_PAT login>` or `author_association` in `OWNER|MEMBER|COLLABORATOR`. The GH_PAT login can be resolved once with `gh api user --jq .login`, as `review_merge_train.sh:792` does.

**SEC-002** — `scripts/gh_helpers.sh:1294-1298` (`gh_api_json_to_file`)
- **Severity:** Low · **Category:** `security`
- **Description:**
  - When a response is not valid JSON, the helper prints `head -50 "${outfile}"` raw to stderr inside `::group::`.
  - Response bodies can contain user-controlled text such as issue bodies and comments. A line starting `::stop-commands::` or `::add-mask::` would be read as a workflow command.
  - Elsewhere the file escapes untrusted stderr with `_gh_actions_escape` (`:157-164`, used at `:1102`).
- **Recommended fix:** Pipe the dump through a per-line escape, for example `head -50 "${outfile}" | while IFS= read -r l; do printf '  %s\n' "$(_gh_actions_escape "${l}")"; done >&2`. Alternatively, prefix each line with two spaces so no line starts with `::`.

**SHELL-001** — `scripts/orchestrate_poll_process.sh:9196-9250` (`create_judge_fixup_issues_from_verdict`)
- **Severity:** Low · **Category:** `shellcheck` (SC2030/SC2031 pattern)
- **Description:**
  - `echo "${JUDGE_JSON}" | jq -c '.new_issues[]? …' | while read -r fix_issue; do … done` runs the loop body in a pipeline subshell.
  - State-file writes survive because they are file-based (`:9218-9220`). Shell variables set inside the loop, such as `FIX_URL` and `EXISTING_NUM`, do not reach the caller.
  - A `set -e` failure of `gh_retry gh issue create` (`:9241-9246`) aborts only the subshell. Remaining fix-ups are then skipped with no per-item log. Whether the parent aborts depends on `pipefail` and the call context `[NEEDS VERIFICATION]`.
- **Recommended fix:** Use process substitution, which this file already uses at `:4095` and `:4101`: `while IFS= read -r fix_issue; do …; done < <(printf '%s' "${JUDGE_JSON}" | jq -c '.new_issues[]? | select(type == "object")')`. Wrap `gh issue create` in `if ! FIX_URL="$(…)"; then echo "::warning::…"; continue; fi`.

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `.github/workflows/review_autofix.yml:1106-1140` (inline `gh_retry`/`ensure_label_exists` in "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")
- **Severity:** Medium · **Category:** `api-redundancy`
- **Description:**
  - The inline `gh_retry` (`:1113-1135`) retries every failure 4 times with 2/4/8 s sleeps. It does not check permanent failures (`_is_gh_permanent_failure`, `gh_helpers.sh:192-195`), unsafe POSTs (`_gh_cmd_is_unsafe_post`, `:804-816`), or rate-limit resets (`_gh_rate_limit_wait_for`, `:297-306`).
  - Concrete waste on this path:
    - `ensure_label_exists "ai:review-skipped"` (`:1137-1140`, `:1356`) gets a 422 "already exists" on every run after the first.
    - `pr_checks_lib.sh:166` (branch protection) gets a 404 on unprotected bases.
    - `pr_checks_lib.sh:948` (`update-branch` PUT) gets a 422 when the branch is already current.
  - Each of these costs 4 calls and 14 s of sleep instead of 1 call.
  - On a rate limit, the 14 s total backoff gives up long before the reset.
  - The sparse checkout at `:1068-1071` already materialises the verified `scripts/gh_helpers.sh` at `REVIEW_SUPPORT_SHA`.
- **Calls:** currently up to 4 per permanent-error call (×3 call sites → 12); proposed 1 each (→ 3).
- **Recommended fix:** In the `freshness_helper_verified=true` branch (`:1162-1169`), `source .codex-freshness-src/scripts/gh_helpers.sh` before `pr_checks_lib.sh`. Keep the inline wrapper only as the unverified fallback, and add a `grep -qiE "already exists|HTTP 404|HTTP 422"` short-circuit to it. No new pattern is needed; this reuses `gh_retry` in `gh_helpers.sh:1060-1138`.

**API-002** — `scripts/label_helpers.sh:157-191, 216`; callers `scripts/review_autofix_step_mark_ready_to_merge.sh:76-81` and `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:31, 57-61`
- **Severity:** Medium · **Category:** `api-redundancy`
- **Description:**
  - `ensure_label_exists` in `label_helpers.sh` has no cache. It issues a `gh label create` POST on every call.
  - `set_issue_phase_label_resilient` calls it again for each issue (`:216`), after the callers already called it once (`mark_ready_to_merge.sh:76`, `mark_review_blocked…:31`). For N linked issues that is N+1 label-create calls, plus N GETs and N PUTs.
  - The poller's copy already caches per process (`orchestrate_poll_process.sh:3998-4003`, `_ENSURED_LABELS_CACHE`).
- **Calls:** currently `1 + 3N` per step; proposed `1 + 2N` (cache), or `1 + N` if combined with the BUG-003 `gh issue edit` delta form.
- **Recommended fix:** Add `declare -gA _LABEL_HELPERS_ENSURED=()` and a cache check and set inside `label_helpers.sh::ensure_label_exists`, as at `orchestrate_poll_process.sh:4001-4003, 4034, 4044`. No caller changes are needed.

**API-003** — `scripts/orchestrate_poll_process.sh:22445-22447, 22625` (prefetch at `.github/workflows/orchestrate_poll.yml:257-267`)
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:**
  - `find_tracking` already fetches `--json number,title,labels` for every tracking issue into `tracking_issues.json`, and `orchestrate_poll.yml:940-943` reads `.labels[]` from it.
  - The per-project loop still re-fetches labels with `TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"` at `:22625` on every iteration. That is one REST call per project per tick, up to 20.
  - The prefetch can be minutes old by the time a later project is processed, so staleness must be checked `[NEEDS VERIFICATION]`. The cache would serve only the first read in each iteration; the re-reads after mutations (`:22660`, `:23021`, `:23287`) stay as they are.
- **Calls:** currently COUNT per tick; proposed 0 for the initial read.
- **Recommended fix:** `TRACKING_LABELS="$(printf '%s' "${TRACKING_ISSUES}" | jq -c ".[${tidx}].labels | map(.name)")"`. Fall back to `get_issue_labels_json` when the result is not an array, so a cache miss fails open (§14).

**API-004** — `scripts/review_merge_train.sh:800-806, 871-887` (`_mt_upsert_comment`)
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** `_mt_find_marker_comment` already pages through every comment with its `.body` (`:804-805`) but returns only `id created_at`. `_mt_upsert_comment` then GETs the same comment again to compare bodies (`:879`).
- **Calls:** currently +1 GET per upsert; proposed 0.
- **Recommended fix:** Pass the desired body with `--arg want "${body}"` and have the existing `--jq` program also output `(.body == $want)`. Alternatively, return a body digest from the existing paginated call and skip the `:879` GET.

**API-005** — `.github/workflows/implement.yml:5083-5096`
- **Severity:** Low · **Category:** `api-batching`
- **Description:** The ancestor walk makes 2 REST calls per hop: issue body (`:5084`) and paginated comments (`:5088`). With the default `IMPL_NOOP_ANCESTRY_THRESHOLD=2` that is 4 or more calls.
- **Calls:** currently `2 × threshold` plus pagination; proposed `threshold` (each hop's parent number depends on the previous body, so hops stay sequential).
- **Recommended fix:** Use one GraphQL query per hop, `issue(number:N){ body comments(last:100){ nodes{ body author{login} } } }`. This follows the field-selection pattern of `_fetch_candidate_issue_details_graphql` (`orchestrate_poll_process.sh:17928-17999`) and can also return the author needed for SEC-001.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `ensure_label_exists` is defined 9 times
- **Severity:** Medium · **Category:** `duplication`
- **Locations:**
  - `scripts/label_helpers.sh:157`
  - `scripts/orchestrate_poll_process.sh:3995`
  - `scripts/validate_process.sh:1204`
  - `scripts/review_autofix_step_mark_ready_to_merge.sh:15`
  - `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`
  - `scripts/review_rb_judge.sh:924`
  - `.github/workflows/review_autofix.yml:1137, 1555, 4380`
- **Description:**
  - Colour sources differ between copies: a hardcoded catalog (`label_helpers.sh:23-83`), the contract JSON (`orchestrate_poll_process.sh:4019-4024`, `validate_process.sh:1211-1214`), or a fixed colour.
  - Return codes differ (see CONSIST-001).
  - The fallback at `review_autofix.yml:1555-1559` creates *any* label with the `ai:review-blocked` colour and description.
- **Recommended fix:** Make `scripts/label_helpers.sh` the single owner, with signature `ensure_label_exists <label> [repo]`:
  - always return 0, warn on failure;
  - read colour and description from `.github/ai/label_contract.v1.json` when present, otherwise from the built-in catalog;
  - cache per process (API-002).
  
  Update callers to source it. Keep the inline fallbacks only where the helper may be missing at runtime, and make them call `gh label create` with catalog values.

**DUP-002** — `scripts/review_autofix_step_mark_ready_to_merge.sh:1-81` vs `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:1-61`
- **Severity:** Medium · **Category:** `duplication`
- **Description:** About 80% of the bodies are identical: helper bootstrap and fallbacks, the three-tier linked-issue resolution (`LINKED_ISSUES_JSON` → `LINKED_ISSUE_FALLBACK_NUMBERS_JSON` → `extract_repo_scoped_issue_refs_from_text`), and the per-issue label loop. Their `|| true` handling has already drifted (BUG-001).
- **Recommended fix:** Add `scripts/review_mark_linked_issues_label.sh` exposing `review_mark_linked_issues_label <label> <log_suffix>`. Each step script becomes a 3-line wrapper. Per §24, keep both step files and their workflow `name:`/`if:`/`env:` unchanged.

**DUP-003** — `.github/workflows/orchestrate_poll.yml:531-793`
- **Severity:** Low · **Category:** `duplication`
- **Description:** The "prefer `.codex-workflow-src`, else `.codex-workflow-src-main`, else error/notice/skip, then `install -m`" block appears about 14 times, at `:531, 552, 567, 582, 595, 608, 621, 635, 655, 667, 679, 695, 709` and in the single-file blocks `:721-779`.
- **Recommended fix:** Add `scripts/stage_support_files.sh` with `stage_support_file <relpath> <mode> <required|optional> <missing_msg>`, driven by a manifest list. This also removes most of EXPR-002.

**DUP-004** — inline "Resolve AI engine" step: 12 copies in 10 workflows
- **Severity:** Low · **Category:** `duplication`
- **Locations:** the grep for `engine="$(ai_engine_for_role` matches, for example, `workflow-failure-heal-intake.yml:243-261, 421-439`, `implement.yml` (2 copies), `plan.yml`, `clarify.yml`, `orchestrate.yml` and `issue_pr_status.yml`.
- **Recommended fix:** Add `scripts/resolve_ai_engine_step.sh <ROLE>`, which writes `engine=` to `GITHUB_OUTPUT`. Each step body becomes `bash scripts/resolve_ai_engine_step.sh WORKFLOW_HEAL`, keeping its `id:`/`env:` (§24).

**DUP-005** — inline "Record GH_PAT budget at … start" block: 13 copies in 6 workflows
- **Severity:** Low · **Category:** `duplication`
- **Locations:** for example `workflow-failure-heal-intake.yml:137-148, 381-392` and `review_autofix.yml:1074-1086`.
- **Description:**
  - Each start copy reads the JSON *body* with `gh api rate_limit --jq …`. The end step uses `gh_pat_budget`, which overrides with the *response headers* when `x-ratelimit-resource: core` (`gh_helpers.sh:50-60`).
  - The two sources can disagree on `reset`. That is one candidate cause of the `used_in_job=unknown` reported in the GH API Call Audit section above (inference).
- **Recommended fix:** Make the start reading match `gh_pat_budget` exactly. Either:
  - (a) run the start step after a sparse checkout of `scripts/gh_helpers.sh` and call `gh_pat_budget start …`; or
  - (b) change the inline snippet to `gh api -i rate_limit` with the same header-preferring parse.
  
  Then append `start_reset=` to the end line (additive) so mismatches can be counted.

**DUP-006** — `.github/workflows/workflow-failure-heal-intake.yml:98-135` vs `:355-379`, and steps `:137-310` vs `:381-485`
- **Severity:** Low · **Category:** `duplication`
- **Description:** The `intake` and `recovery-scan` jobs duplicate about 20 env keys and 10 steps: budget start, checkout, runtime dir, codex install and config, engine resolve, Claude install and credential, notify, and budget end. Edits must be made twice.
- **Recommended fix:** Move the shared steps into a local composite action (`.github/actions/heal-intake-runtime`, after checkout) that takes a `mode` input. Leave job-level `concurrency`/`if` in place.

**DUP-007** — `_safe_gh_jq` fallback redefinitions
- **Severity:** Low · **Category:** `duplication`
- **Locations:** `scripts/implement_diagnose_post_codex_failure.sh:54-64`, `scripts/check_failure_triage.sh:213`, `.github/workflows/orchestrate.yml:1199`, `implement.yml:5987`, `plan.yml:2492`, `review_autofix.yml:1166`. The canonical definition is `gh_helpers.sh:1241-1255`.
- **Description:** The `review_autofix.yml:1166` copy (`_safe_gh_jq() { gh api "$@"; }`) drops the stdout-on-failure suppression that is the helper's purpose (`gh_helpers.sh:1224-1236`). It stays safe only because every `pr_checks_lib.sh` caller wraps it in the buffering inline `gh_retry`, and `:1124` discards output.
- **Recommended fix:** Source `gh_helpers.sh` wherever it is available (see API-001). Make the remaining fallbacks use the canonical temp-file body.

### Section 4: Expression Size Limit Risk Assessment

Method: the count is the run-body length after removing YAML indentation, estimated as lines × average line length. Blocks with no `${{ }}` were excluded. Examples checked and excluded: `review_autofix.yml:1098-1408` and `:1523-1736` (env-only), and `implement.yml` "Run Codex implementation" `:2514-3757` (no inline expressions found by grep).

**EXPR-001** — `.github/workflows/clarify.yml:529-826` ("Decide clarify route")
- **Severity:** High · **Category:** `expression-limit`
- **Description:**
  - The 297-line run body contains 6 `${{ github.repository }}` interpolations (`:738, 747, 766, 775, 780, 795`), so the whole body is one template expression.
  - Many lines run 250–360 characters (`:545, 609, 641, 660, 692, 738, 766, 795, 811, 826`).
  - **Estimated** 17,000–20,000 characters, leaving roughly 1,000–4,000 of headroom against 21,000 `[NEEDS VERIFICATION]`.
  - This is the release-classification block that keeps growing with each gate (#7328, #7370, #7251, #7228, #7095).
- **Recommended fix:** The cheapest fix is to replace all 6 `${{ github.repository }}` with `${GITHUB_REPOSITORY}`. The block already uses that default env var at `:606, 608, 721`. With no `${{ }}` left, the body is no longer subject to the limit. Then move the body to `scripts/clarify_decide_route.sh` per the §24 pattern to give future gates room.

**EXPR-002** — `.github/workflows/orchestrate_poll.yml:525-795` ("Stage workflow support files")
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** A 270-line body with `${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED … }}` (`:652`), `${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED … }}` (`:653`) and `${{ github.repository }}` (`:773`). The script-list line alone (`:531`) is about 1,000 characters. **Estimated** 15,000–17,000 characters, leaving 4,000–6,000 `[NEEDS VERIFICATION]`. The block grows with each new staged file.
- **Recommended fix:**
  - Move the two `vars.*` into the step `env:` (`PROMPT_PRELUDE_REFACTOR_ENABLED: ${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED || 'false' }}`) and `echo` the env values.
  - Replace `:773` with `${GITHUB_REPOSITORY}`.
  - The block then has no expressions. Later, extract it with DUP-003.

**EXPR-003** — `.github/workflows/implement.yml:4940-5167` ("Handle no-op implementation")
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:** A 227-line body with interpolations at `:4945, 4947, 5150, 5158, 5160, 5164, 5166`, and several 280–350-character comment-text lines (`:4970, 5010, 5017, 5104, 5106`). **Estimated** 14,000–16,500 characters, leaving 4,500–7,000 `[NEEDS VERIFICATION]`.
- **Recommended fix:**
  - Set `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"` and `REPO="${GITHUB_REPOSITORY}"`.
  - Replace the remaining `${{ github.repository }}` uses with `${REPO}`.
  - Optionally move the three comment templates to `prompts/`-style text files, or extract the body to `scripts/implement_handle_noop.sh` alongside BUG-002.

**EXPR-004** — `.github/workflows/implement.yml` (6,494 lines) and `.github/workflows/review_autofix.yml` (5,691 lines): whole-file size
- **Severity:** Low · **Category:** `expression-limit`
- **Description:**
  - Neither file is near the 800 KB / 1 MB threshold in the brief.
  - The binding constraint here is GitHub's 512,000-byte limit and the repo guard at 480,000 bytes (`tests/test_workflow_file_size_limit.py:26-27`). At about 70 bytes per line, `implement.yml` would be around 455 KB `[NEEDS VERIFICATION with wc -c]`. That is close enough that a mid-size addition could trip the guard.
  - The heaviest inline body is "Run Codex implementation" (`:2514-3757`, about 1,240 lines).
- **Recommended fix:** Before the next feature lands in `implement.yml`, extract `:2514-3757` to `scripts/implement_step_run_codex.sh` using the `review_autofix_step_<slug>.sh` pattern (§24; `agents.md` "Workflow file size limit"). Separately, extend `tests/test_workflow_file_size_limit.py` with a per-step check: parse each `run:` that contains `${{`, fail at 18,000 characters and warn at 15,000. That turns these estimates into enforced numbers.

### Section 5: Cross-Cutting Concerns

**CONSIST-001** — `ensure_label_exists` return-code semantics
- **Severity:** Medium · **Category:** `consistency`
- **Description:** `scripts/label_helpers.sh:189-190` returns `1` on failure. `scripts/orchestrate_poll_process.sh:4048-4049` and `scripts/validate_process.sh:1236-1237` return `0`. Callers that assume the lenient form break under `set -e` when they source the strict one. This is the root cause of BUG-001 and BUG-002.
- **Recommended fix:** Standardise on "always return 0 and warn", as the poller and validate copies do. Callers that need the outcome can read a status variable, mirroring `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME` (`label_helpers.sh:193-198`).

**CONSIST-002** — label colour and description drift
- **Severity:** Low · **Category:** `consistency`
- **Locations:** `.github/workflows/implement.yml:4975-4976, 5109-5110` and `:5022-5023`, against `.github/ai/label_contract.v1.json:100-103, 152-155`.
- **Description:**
  - `implement.yml` creates `ai:closed` with `6b7280` / "Issue closed by automation …"; the contract has `6a737d` / "Linked PR closed without merge".
  - It creates `ai:needs-human` with `b60205` / "Automation halted …"; the contract has `e11d48` / "Escalated for human intervention …".
  - Whichever job creates the label first sets its appearance until `sync_ai_labels.yml` reconciles it.
- **Recommended fix:** Replace the 12 raw `gh_retry gh label create` calls (8 in `implement.yml`, 4 in `review_autofix.yml`) with the consolidated `ensure_label_exists` from DUP-001.

**CONSIST-003** — silent `gh_retry() { "$@"; }` shims
- **Severity:** Low · **Category:** `consistency`
- **Description:**
  - There are 95 occurrences across 48 files, for example `review_autofix_step_mark_ready_to_merge.sh:9`, `post_review_comment.sh:176` and `orchestrate_poll.yml:232`.
  - Most follow `source …/gh_helpers.sh 2>/dev/null || true`, so a missing or broken helper silently removes all rate-limit and backoff handling with no log line.
  - That makes the GH API rate-limit behaviour of a run unobservable.
- **Recommended fix:** Change the shim to `type gh_retry >/dev/null 2>&1 || { echo "GH_HELPERS_FALLBACK script=${BASH_SOURCE[0]##*/}" >&2; gh_retry() { "$@"; }; }`. Register the prefix through an `agents.d/` fragment under "Stable log prefixes (contractual)" (§25).

**DEAD-001** — `scripts/gh_helpers.sh:336-341` (`gh_rate_limit_breaker_tripped`)
- **Severity:** Low · **Category:** `dead-code`
- **Description:** A grep of `scripts/` and `.github/workflows/` finds no caller; the matches are only the definition and comments (`:313, 329, 336, 338`). The comment at `:315-318` says the last consumer, `orchestrate_poll.yml`, was removed. `gh_rate_limit_breaker_active` (`:894-902`) is the live replacement. Test or Python callers were not checked `[NEEDS VERIFICATION]`.
- **Recommended fix:** Per §10, do not remove it. Add a one-line `# Deprecated alias: prefer gh_rate_limit_breaker_active` comment and record it in `docs/scripts-pending-removal.md` if the owner wants it retired.

**TODO/FIXME/HACK markers:** none. A word-bounded grep of `\b(TODO|FIXME|HACK)\b` over `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` returned no matches.

**Shellcheck notes (no separate IDs):**
- SC2030/SC2031 is covered by SHELL-001.
- No SC2086 word-splitting issues were found in the audited bodies. Expansions in the files read are consistently quoted, and `[[ ]]` is used for regex tests.
- The direct-interpolation injection sweep found no `${{ github.event.*.title/body }}` or `inputs.*body*` inside `run:` bodies. Untrusted text is passed through `env:` (for example `clarify.yml:522`, `review_autofix.yml:593-594`, `workflow-failure-heal-intake.yml:175-176`).
- The one step-output branch interpolated into shell (`implement.yml:1684-1691`) is only emitted after `re.fullmatch(r"ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+", …)` (`:914`, `:1036`), so it is safe.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 11 | BUG-001, BUG-002, BUG-003, BUG-004, API-001, API-002, DUP-001, DUP-002, EXPR-002, EXPR-003, CONSIST-001 |
| Low | 15 | BUG-005, SEC-001, SEC-002, SHELL-001, API-003, API-004, API-005, DUP-003, DUP-004, DUP-005, DUP-006, DUP-007, EXPR-004, CONSIST-002, CONSIST-003, DEAD-001 |

Note: the Low row lists 16 IDs.

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/clarify.yml` (EXPR-001 is the only High; the `${GITHUB_REPOSITORY}` swap is 6 lines) | Small |
| API call optimization | `.github/workflows/review_autofix.yml`, `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_merge_train.sh`, `.github/workflows/implement.yml` | Medium |
| Code modularization | `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, both `scripts/review_autofix_step_mark_*.sh`, new `scripts/review_mark_linked_issues_label.sh`, new `scripts/stage_support_files.sh`, new `scripts/resolve_ai_engine_step.sh`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/workflow-failure-heal-intake.yml`, plus 8 workflows for the engine step | Large |
| Expression size reduction | `.github/workflows/clarify.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`, `tests/test_workflow_file_size_limit.py` | Medium |
| Medium/Low fixes | `scripts/review_autofix_step_mark_ready_to_merge.sh`, `.github/workflows/implement.yml`, `scripts/label_helpers.sh`, `.github/workflows/orchestrate_poll.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/gh_helpers.sh`, `agents.d/` fragment for new log prefixes | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-15)

### Safety Tag Legend
`SAFE_TO_MERGE` means static reading proves every merge precondition holds: same endpoint, or a strict subset of its fields; same pagination, filters and auth; same function or same step; no mutation in between; error and retry behaviour unchanged; no concurrency boundary crossed; existing cache contracts kept. The implement stage may apply these without further review. `NEEDS_VERIFICATION` means the consolidation looks correct, but at least one precondition can't be proven from the source alone. The finding names the exact check that must pass first. `RISKY_SKIP` means the call hits at least one hard trigger: a retry, backoff or stall-recovery path; an auth, identity or permission probe; pagination; a rate-limit-sensitive path; `orchestrate_poll_process.sh` or race-defending code; or log keys that other tools parse. These are listed for visibility only and must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001** — `RISKY_SKIP`
- **Files and lines:** 16 call sites in `scripts/orchestrate_poll_process.sh`, each fetching the repository's default branch: `:3829`, `:5114`, `:5554` (`compute_cycle_integration_ahead_by`), `:14656` (`mark_validation_complete`), `:21857` (`_review_run_default_branch`, already memoized), `:22663`, `:22835`, `:23126`, `:23173`, `:23218`, `:23624`, `:23694`, `:23771`, `:27848`, `:28193` (fetches only when `DEFAULT_BRANCH_TRACKING` is empty), `:28955`.
- **Endpoint:** REST `GET /repos/{owner}/{repo}`, reading only `.default_branch`.
- **Current call count:** one GET per site reached. 14 of the sites have no memoization. The sites from `:22663` onward come after the per-project loop header (`:22445`), so each of those can run once per tracking issue per tick. Which of them sit inside the loop body has not been checked.
- **Proposed call count:** 1 per poll process. `:21857` would remain the single fetch, with a re-fetch only after a failure.
- **Evidence:** every site uses the same call shape. The repository's default branch does not change within one poll run.
  ```bash
  default_branch="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"   # :14656
  CWS_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "")"  # :5554
  ```
  A memoizer already exists, `_review_run_default_branch` (`:21825-21880`). It caches both `ok` and `failed` in a process-keyed file (`review_run_default_branch.${GITHUB_RUN_ID}.$$`, `:21836`).
- **Proposed fix:** add a lenient accessor beside `_review_run_default_branch`. On success it returns the memoized value. On failure it still applies each caller's own fallback, and it does not persist a `failed` state for lenient callers. Then switch the call sites to it one at a time.
- **Safety rationale:** the sites are in `orchestrate_poll_process.sh`, and their error handling differs. Ten fall back to `"main"`, four to `""`, and `:21857` to `true`. The `""` fallback at `:5554-5557` is a deliberate fail-closed signal for check-wave-status. `_review_run_default_branch` caches failure for the whole poll, so one transient error early in a tick would change the behaviour of every later caller.
- **Downstream signal:** do not implement automatically. A maintainer has to decide, for each site, whether `"main"`, `""` or "fail closed" is the intended fallback. They also have to confirm that sharing a cached failure across the per-project loop is acceptable, and that `tests/test_orchestrate_poll_process.py` does not assert the per-site call strings. This would extend §14's "cycle-local caches" rule to a value that currently has no cache.

**MERGE-002** — `RISKY_SKIP` (adds to API-004)
- **Files and lines:** in `scripts/review_merge_train.sh`, the `_mt_release` loop calls `_mt_find_marker_comment_id "${num}" "${MT_MARKER}"` at `:1393`. It then calls `_mt_upsert_comment "${num}" "${MT_RELEASED_MARKER}" …` at `:1413-1414` without the 4th argument. That makes `_mt_find_marker_comment_id` list the comments a second time (`:875` → `:804-805`) and then GET the comment body (`:879`).
- **Endpoints:**
  - REST `GET /repos/{o}/{r}/issues/{n}/comments?per_page=100` with `--paginate`, twice;
  - REST `GET /repos/{o}/{r}/issues/comments/{id}`, once.
- **Current call count:** per released PR, two paginated comment listings (2 or more requests each), plus 1 GET when a released marker already exists.
- **Proposed call count:** one paginated listing that returns the latest `id created_at` for both markers plus a body comparison, and 0 GETs.
- **Evidence:** both listings use the same endpoint, the same `MT_AUTOMATION_LOGIN` author filter and the same `startswith(marker)` predicate. Only the marker differs: `<!-- merge-train:queued -->` versus `<!-- merge-train:released -->` (`:134-135`). The gate path already avoids the second listing by passing the id through (`:999`, `"${queued_comment_id}"`). The release path does not.
- **Additional detail for API-004:** API-004's fix changes the `--jq` program of a paginated call. An alternative that avoids pagination entirely is to drop the GET at `:879` and always PATCH. That keeps the cost at 1 call, but it gives up the "skip the write when the body is identical" contract (`:870`) and adds comment edit-history noise.
- **Safety rationale:** both calls paginate. There are mutations between them: the comment PATCH at `:1398`, the label DELETE claim at `:1406` and the dispatch at `:1410`. The code also explicitly defends against concurrent release invocations (`:1403-1405`). Each of these is a hard trigger.
- **Downstream signal:** do not implement automatically. A reviewer has to confirm two things. First, after the label-DELETE claim succeeds, no other actor can post a `merge-train:released` marker on this PR (the claim at `:1406-1408` suggests not). Second, the combined `--jq` output keeps today's `sort -k2,2r -k1,1nr | sed -n '1p'` tie-break for each marker separately.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** — `NEEDS_VERIFICATION` (adds to API-005)
- **Files and lines:**
  - `.github/workflows/implement.yml:5083-5085`: the first hop of the no-op ancestor walk fetches `repos/${REPO}/issues/${current}`, where `current="${ISSUE_NUMBER}"` (`:5082`).
  - The same issue's body is already on disk:
    - written at `:182-185` ("Precheck approval phase label");
    - rewritten at `:747` (refctx) and `:1801` ("Fetch issue metadata", after the `issue_meta_matches_issue` validation at `:1772-1779`);
    - exported as `ISSUE_BODY_FILE` / `ISSUE_META_FILE` at `:275-276`.
- **Endpoint:** REST `GET /repos/{o}/{r}/issues/{n}`, reading `.body`.
- **Current call count:** up to `2 × threshold` per no-op run. The step's own comment at `:5074` says "API cost: up to 2 * threshold calls per invocation". With the default `IMPL_NOOP_ANCESTRY_THRESHOLD=2`, that is 4 calls plus any extra comment pages.
- **Proposed call count:** `2 × threshold − 1`. Combined with API-005's per-hop GraphQL query, which can return the parent's `body` and `comments` together, it becomes `threshold` rather than `threshold + 1`.
- **Evidence:**
  ```bash
  current="${ISSUE_NUMBER}"
  for hop in $(seq 1 "${threshold}"); do
    parent_body="$(gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
  ```
  The `implement` job runs from `:92` to the `heal-report` job at `:6420`, so the cache files (`/tmp/codex-implement-${GITHUB_RUN_ID}`) are in the same job. Grep found writes to `ISSUE_BODY_FILE` only at `:185`, `:719`, `:738`, `:747` and `:1801`. `RUNTIME_DIR` is removed only at `:6409`, after the no-op step at `:4932`.
- **Proposed fix:** on hop 1 only, when `issue_meta_matches_issue "${ISSUE_META_FILE}"` succeeds, read `parent_body` with `jq -r '.body // ""' "${ISSUE_META_FILE}"`. Otherwise keep the existing API call as a fail-open fallback (§14). Later hops are unchanged. Update the `:5074` cost comment to match.
- **Safety rationale:** this is the same endpoint and field within the same job, and the call is not paginated. However, the cache is written in a different step, possibly many minutes earlier. Static reading can't rule out the issue body being edited in that time.
- **Downstream signal:** before applying, verify these three things:
  1. Nothing in the pipeline edits a sub-issue's body (the `Re-issued from #N` line) while an implement run is in flight. Check the orchestrator re-issue path and `reconcile_tracking_issue_body_from_state`.
  2. `issue_meta_matches_issue` is defined in, or available to, the "Handle no-op implementation" step. It is currently defined inline per step (`:1761`).
  3. The `:719`/`:738` truncation paths leave the file empty, so the fallback is taken.

**REUSE-002** — `RISKY_SKIP`
- **Files and lines:** in `scripts/orchestrate_poll_process.sh`, `mark_validation_failed` (`:14511-14513`, `:14558-14560`, `:14619-14621`) and `mark_validation_complete` (`:14721-14723`, `:14758-14760`). Each reads `_tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"`, then calls `set_tracking_phase_label`, which reads the same labels again at `:4083` (`set_issue_phase_label`).
- **Endpoint:** REST `GET /repos/{o}/{r}/issues/{n}/labels` (`get_issue_labels_json`, `:4130-4133`), twice back to back.
- **Current call count:** 2 per terminal or complete transition.
- **Proposed call count:** 1 when `ai:comprehensive-test-pending` is absent.
- **Evidence:** `_tracking_labels` is used only by `handle_comprehensive_release_callback_if_needed`. That function returns without any mutation when the label is absent (`:13927-13929`). When the label is present, it removes it and refreshes the global `TRACKING_LABELS`, not the local `_tracking_labels` (`:14071-14072`).
- **Proposed fix:** add an optional third argument to `set_issue_phase_label`, `[current_labels_json]`, and use it in place of the `:4083` read when it is passed. Pass the refreshed `TRACKING_LABELS` only when the callback did not mutate.
- **Safety rationale:** the code is in `orchestrate_poll_process.sh`. The `:4076-4081` comment says the read exists to stop stale labels surviving. BUG-005's failure-to-`[]` mapping would also feed the reused value.
- **Downstream signal:** do not implement automatically. Fix BUG-005 first. Then a reviewer has to confirm that no code path between the two reads mutates labels: `post_state_comment`, `needs_human_park_project` ordering, and the `RETRY*` early return at `:13940-13952`.

**REUSE-003** — `RISKY_SKIP`
- **Files and lines:** `.github/workflows/implement.yml:1806` (step "Fetch issue metadata": `pipeline_login="$(gh api user --jq .login …)"`) and `:2137` (step "Unblock judge bulk-delete override": `pipeline_login="$(gh api user --jq '.login' …)"`). Both are in the same `implement` job.
- **Endpoint:** REST `GET /user`.
- **Current call count:** 2. The second call happens only when a `override=bulk_delete -->` marker exists (`:2134`).
- **Proposed call count:** 1, by exporting the login through `GITHUB_ENV` at `:1806`.
- **Evidence:** both calls resolve the same token identity with the same `GH_TOKEN: ${{ secrets.GH_PAT }}`. At `:2142-2148`, the second value decides which comments count as trusted override markers.
- **Safety rationale:** this is an identity probe that feeds an authorization decision. Passing it through `GITHUB_ENV` crosses steps whose environment the file explicitly treats as a trust boundary (`:1825-1827`, #6504). The saving is at most 1 call, on rare runs.
- **Downstream signal:** do not implement. Any change needs a security review showing that no step between `:1806` and `:2137` can write a `PIPELINE_LOGIN`-style key into `GITHUB_ENV`. Given the benefit, leaving it as is is the recommended outcome.

### Dead Calls (DEAD-API-###)
No findings. I searched `scripts/` for read-only `gh_retry gh api`/`_safe_gh_jq` GETs whose output is discarded. The only match, `scripts/workflow_failure_heal_intake.sh:1852` (`_branch_exists`), is an existence probe whose exit status the caller uses, so it is not dead.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — the change replaces a retry wrapper (`review_autofix.yml:1113-1135`) and changes retry and permanent-failure semantics. The inline wrapper is also an intentional design choice: the comment at `:1106-1108` says "avoid loading the full support source here to keep this job lightweight".
- API-002: SAFE_TO_MERGE — the duplicate label-create POST happens in the same process and the same step, with no mutation in between. Caching only successful and already-exists outcomes leaves the failure and retry path unchanged. It must be keyed `repo|label` (callers pass different repos), use the unused name `_LABEL_HELPERS_ENSURED`, and keep the strings pinned by `tests/test_label_helpers_phase_transition_contract.py:29,42-44` (including the `:216` call).
- API-003: RISKY_SKIP — in `orchestrate_poll_process.sh`. The prefetch comes from another step (`orchestrate_poll.yml:257-267`) and can be up to a whole tick old (p50 654s) by the time later projects use it.
- API-004: RISKY_SKIP — the fix changes the `--jq` program of a `--paginate` listing (`review_merge_train.sh:804-805`). See MERGE-002 for a non-paginated alternative.
- API-005: NEEDS_VERIFICATION — the calls are sequential, one per hop, and the GraphQL `comments(last:100)` window differs from REST `--paginate` (all comments). That can undercount no-op markers on issues with more than 100 comments. REUSE-001 is needed to reach the stated `threshold` call count.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | API-002 (cross-ref) |
| NEEDS_VERIFICATION | 2 | REUSE-001, API-005 (cross-ref) |
| RISKY_SKIP | 7 | MERGE-001, MERGE-002, REUSE-002, REUSE-003, API-001 (cross-ref), API-003 (cross-ref), API-004 (cross-ref) |

### Implement-Stage Handoff
- API-002 — in `scripts/label_helpers.sh::ensure_label_exists`:
  - Add a per-process cache keyed `${repo}|${label_name}`: declare `declare -gA _LABEL_HELPERS_ENSURED=()` and check it with `${_LABEL_HELPERS_ENSURED[$k]+x}` so it is `set -u`-safe.
  - Set the cache only on the success and already-exists `return 0` paths.
  - Leave the `return 1` failure path, the `gh_retry gh label create` line, the already-exists regex and every caller (including `set_issue_phase_label_resilient:216`) unchanged.