## Executive Summary

- **PR #9442's conflict resolver failed 3 more times this window.** All three failed in `review / codex-agent` → `Run Codex resolver, validate, stage, commit`: runs 45535387550 (6,175 s), 45548625087 (6,060 s) and 45560786855 (7,685 s). That is 19,920 runner-seconds (about 5.5 h). With today's earlier report (`analysis/workflow-optimization-2026-11-25.md`), the count is at least 9 failures across three windows. The code gap that earlier report found is still on this ref:
  - A failure with no fingerprint violations, such as a timeout, is never saved to retry state (`scripts/review_conflict_resolve.sh:3096-3097`).
  - A new head SHA resets the count (`:3116-3119`).
  - Escalation needs threshold × 4 identical failures (`:3122`).

  Impact: about 5–6 runner-hours per window. Confidence: medium that the gap exists, low that it caused these specific failures, because the run logs were not readable.
- **The cancelled-CI rerun safety net stops working when CI is congested.** In `Internal: AI Review Autofix Sweep` run 45581342861, every PR (pr=10006 down to pr=9359) logged `CI_CANCELLED_RERUN ... action=skip reason=listing_truncated`. The cause is in `scripts/ci_cancelled_rerun.py:252-275`: the queued and in-progress listings make one unpaginated `per_page=100` read, and the script stops without rerunning anything once 100 or more CI runs are active. The merge-train listing in 45575592854 shows 283 queued runs account-wide. Impact: cancelled CI is not rerun at exactly the times it gets cancelled most. Confidence: high.
- **Hosted-runner queueing is the dominant latency for small jobs.** Waits of 15–32 minutes sat in front of 5–90 s of work, for example:
  - 45576302512 `promote_candidate_follow`: about 29 min of wait for about 5 s of work.
  - 45576979173 `cancel_on_pr_close`: about 31 min of wait for about 35 s.
  - 45576443388 `security_audit`: about 32 min of wait.
  - 45575592865 `issue_pr_status`: two jobs in series, waiting 18 min and then 21 min.

  Earlier verdicts already decided on pacing and queue instrumentation, so the only new change proposed here is a small one (Speed 2). Confidence: high.
- **The `main` merge lease is the throughput bottleneck.** Two holders, #9692 (`hold_s` about 25,844–27,152) and #9867 (`hold_s` about 11,671–12,979), held it for 7.2–7.5 h with `waiters=32` (45575592854, 45576301823, 45576979173). The existing guards are already on this ref: `revoked_final_ci_failed`, the 720-minute `MERGE_LEASE_MAX_HOLD_MINUTES` cap, and `holder_runs_active` keeping holders whose runs are queued. No code change is proposed; this is an indicator to track. Confidence: medium.
- **Cost and cache.** 20 OpenRouter calls used 6,541,957 tokens. `cache_hit_rate` was 0.685, down from 0.877 for coding-workflows in the prior window. Run 45552464821 had a 0.554 hit rate with 680,496 uncached prompt tokens. There were 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` lines. Semble was healthy: 8 queries, 93,843 bytes, 0 runtime fallbacks. Observation only, because prefix work was rejected earlier. Confidence: medium.
- **Data gaps.**
  - Truncated repository (page limit reached, so only part of its runs are listed): `shubhodeep1/coding-workflows`. No repository failed.
  - The run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist, so no deep-dive log excerpts could be read. Evidence comes from `analysis_context.json` (`cost_telemetry`, `failure_point`, and the gpt-6-luna `log_summary` fields).

## Speed Optimizations

1. **Stop the #9442 resolver loop (critical path).**
   - Evidence: 3 failures at the resolver step this window (45535387550, 45548625087, 45560786855), 19,920 s in total. Each run did one identical Semble query: 11,370 bytes, 6 sources, with 36.7–48.1 s of bootstrap. That suggests the same conflict set every time (inference).
   - Root cause (inference, from code): failures without fingerprint violations are never saved, and a head change resets the count. So the tier ladder and the `AUTOFIX_RESOLVER_ESCALATED_V1` escalation never engage, and every review cycle pays the full resolver budget again: 3 × 50-minute attempts under a 170-minute step cap (`.github/workflows/review_autofix.yml:4457-4478`).
   - Exact change: see Reliability 1.
   - Estimated saving: about 5.5 runner-hours per window for this PR, and #9442 stops coming back onto the critical path every cycle.
   - Risk: medium (it changes when escalation fires).
   - Finding check: new — no verdict in `analysis/recommendation-processing-report.md`. The same gap was raised in `analysis/workflow-optimization-2026-11-25.md` (not yet processed), and `scripts/review_conflict_resolve.sh:3096-3097` and `:3116-3122` are unchanged.

2. **Skip the full-history checkout in `heal-pr-reconcile` (micro-optimization).**
   - Evidence: this job runs on every PR close. In 45576301823, 45576137756 and 45575592854 its only work was `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue`, which took about 2 s. Yet the checkout fetched the entire tag list (v1.4.0 through v1.47.6) and inflated the logs: about 690k characters truncated in 45576301823 and 41.6k in 45576137756.
   - Root cause: `.github/workflows/internal-cancel-on-pr-close.yml:45-50` checks out `main` with `fetch-depth: 0`. The script exits with the no-op at `scripts/workflow_failure_heal_pr_reconcile.sh:107`, before any git operation. Its merge path already fetches what it needs with `git fetch --no-tags` (`:162`, `:168`).
   - Exact change: set `fetch-depth: 1` on that checkout. In the retarget path, before the first `git fetch` at `:162`, add `[ "$(git rev-parse --is-shallow-repository)" = true ] && git fetch --quiet --no-tags --unshallow origin` so merge bases are available exactly as today.
   - Estimated saving: seconds per PR close, plus far less log volume. It does not cut runner-queue time.
   - Risk: low. The merge path ends up with the same history; only the no-op path changes.
   - Finding check: new — the earlier rejection ("Speed #4 (narrower sync checkout)", 2026-10-08) concerned the issue-status sync checkout, not this job. The current code still uses `fetch-depth: 0`.

## Cost Optimizations

1. **Stop paying repeatedly for the #9442 resolver.**
   - Evidence: 19,920 runner-seconds this window. The failing runs recorded `or_calls=0` and `codex_calls=0`, so the resolver's model spend (it uses OpenCode) is not measured, and `claude_*` cost fields are not exported.
   - Root cause and change: see Reliability 1 and 2.
   - Estimated saving: about 5.5 runner-hours per window, plus model tokens that are currently unmeasured.
   - Quality risk: low. Escalation hands the PR to the existing judge and rebuild path instead of re-attempting.
   - Finding check: new — same evidence and code as Speed 1.

Other observations (no change recommended):
- **Tokens:** 1,299,071 prompt, 83,332 completion, 4,427,229 cache-read and 732,325 cache-write; 6,541,957 in total over 20 calls, all with usage available. Per run, the cache hit rate was 0.626 for 45533322461, 0.827 for 45543404617 and 0.554 for 45552464821.
- **Semble is shrinking prompt expansion, not adding noise.** 8 queries, 93,843 bytes (about 11.7 KB each), 46 sources, `static_dup_bytes=0`, 0 echo lines dropped. Bootstraps averaged about 43.7 s (349,974 ms over 8), with 0 failed and 0 unused. Against review runs of more than 3 h, that is under 0.5% of runtime.
- **Serena:** no traffic at all (0 queries, 0 probes).
- **Security audits:** the full-range audit in 45582912761, 45582794334 and 45581913890 (`reason=no_clean_ancestor checked=0`) is the designed fail-open for PRs with no prior clean marker (`scripts/review_single_issue_security_pass.sh:1916-1967`). Delta mode did engage in 45576443388 and 45579751815.

## Reliability Improvements

1. **Save retry state for resolver failures that have no fingerprint, and count them per PR as well as per head.**
   - Failure evidence: 45535387550, 45548625087 and 45560786855 (PR #9442), at least 9 failures in total across windows.
   - Root cause category: a gap in the retry and escalation logic.
     - `build_resolver_retry_state_artifact` returns `{"ok": False, "reason": "no fingerprint failures detected ..."}` when there are no fingerprint failures (`scripts/review_conflict_resolve.sh:3096-3097`), so timeout, no-progress and marker failures never advance `consecutive_failure_count`.
     - The count also restarts at 1 whenever `head_sha` changes (`:3116-3119`).
   - Exact fix (additive):
     - Add a keyword parameter `terminal_exit_class: str = ""`, fed from `RESOLVER_TERMINAL_EXIT_CLASS` (`:439`, `:523-524`). When both failure sets are empty and the class is one of `timeout|no_progress|markers|commit`, use a synthetic `fp_key ["resolver_exit", <class>]` instead of returning.
     - Add an additive state field `pr_consecutive_failure_count`. It is not reset by a head change; it resets only when the signature changes or the resolver succeeds.
     - Escalate through the existing `AUTOFIX_RESOLVER_ESCALATED_V1` path when that count reaches `CONFLICT_RESOLVER_PR_ESCALATE_AFTER`, a new environment variable defaulting to 6.
     - Extend `tests/test_review_conflict_resolve_retry_state.py`.
   - Expected impact: #9442-style loops end after about 6 runs instead of continuing indefinitely.
   - Rollback / fail-open: setting `CONFLICT_RESOLVER_PR_ESCALATE_AFTER=0` disables the new trigger. A missing exit class keeps today's behaviour.
   - Finding check: new — no processing verdict, and the code at `:3096-3097` and `:3116-3122` is unchanged (raised in the unprocessed `analysis/workflow-optimization-2026-11-25.md`).

2. **Parse `RESOLVER_TERMINAL` into run telemetry (a diagnostic tied to defect 1).**
   - Failure evidence: none of the three #9442 failures can be classified from the collected data. The record is printed at `scripts/review_conflict_resolve.sh:532` (fields `exit_class= attempts= attempt_ms= fp_violations= markers=`), but no `scripts/*.py` file reads it.
   - Root cause category: observability gap for a defect that keeps recurring.
   - Exact fix: in `scripts/cost_audit.py`, add an anchored regex that uses `_TELEMETRY_LINE_PREFIX_RE_SRC`, and emit additive per-run fields `resolver_terminal_exit_class`, `resolver_terminal_attempts` and `resolver_terminal_attempt_ms_total`. Add a fixture beside `tests/test_cost_audit_semble_metrics.py`.
   - Expected impact: the next window can confirm or refute the root cause of Reliability 1.
   - Rollback / fail-open: purely additive. Runs without the line get empty values.
   - Finding check: new — no verdict. Earlier "instrumentation only" rejections cited no defect, and here a defect is recurring.

3. **Paginate the active CI listing in `ci_cancelled_rerun.py`.**
   - Failure evidence: sweep 45581342861 logged `CI_CANCELLED_RERUN pr=<n> run=none action=skip reason=listing_truncated` for every PR, so no rerun was evaluated. The earlier sweep 45577059246, during less congestion, evaluated normally (`no_attributable_run`, `other_conclusion final_ci=true`).
   - Root cause category: a hard listing bound. `list_runs` (`scripts/ci_cancelled_rerun.py:252-275`) makes one `per_page=100` read for each of the `queued` and `in_progress` statuses, and raises `listing_truncated` when `len(runs) == 100` or `count != len(runs)`. Wedged queued runs are filtered out only after the listing (`:586-591`), so they still count toward the cap.
   - Exact fix: for active statuses only, loop `page=1..N` with `N = CI_CANCELLED_RERUN_MAX_PAGES`, a new environment variable defaulting to 5, until `len(collected) == total_count`. Keep the same per-run validation, and keep raising `listing_truncated` if the total still exceeds `N × 100`. Leave the bounded `completed` read unchanged. Add cases to `tests/test_ci_cancelled_rerun.py`.
   - Expected reliability impact: the cancelled-CI rerun keeps working with up to 500 active CI runs, which covers the congestion seen this window.
   - Rollback / fail-open: `CI_CANCELLED_RERUN_MAX_PAGES=1` restores today's behaviour, and an unreadable or over-limit listing still stops without rerunning anything.
   - Finding check: new — the processing report has no verdict on `ci_cancelled_rerun`, and the current code makes the single-page read.

Other observations:
- The 3 CI failures had different causes with no shared pattern:
  - 45562812576: `tests-system-python (5)` → `System-Python test shard`.
  - 45533890079: `orchestrate-poll (1)` → `Review autofix review-pipeline plumbing contract test`.
  - 45543786613: `tests-hooks-and-orchestrator` → `Telegram alert levels page only when a person must act`.

  The CI failure rate was 3.6% (3 of 84).
- `SEMBLE_FALLBACK`: 20, all from contract tests (4 each in CI runs 45562812576, 45533890079, 45543786613, 45545499880 and 45552022378), and 0 at runtime. This is healthy fail-open test noise and was already separated in `scripts/cost_audit.py`.
- `SERENA_PROBE` and `SERENA_FALLBACK`: none.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were available: the deep-dive directory is missing, and the sampled `log_summary` fields (for example 45582912761 and 45576137756) say explicitly that no such lines were visible in their truncated logs. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` cannot be measured this window. No change is proposed: emission already exists (`scripts/memory_helpers.sh`, `scripts/orchestrate_poll_process.sh`), and a recommendation to verify telemetry was rejected earlier (processing report, 2026-10-08 entry).

## GH API Call Audit

1. **Bounded pagination for the cancelled-CI rerun listing.** This is Reliability 3. Call cost: at most 4 extra `actions/workflows/ci.yml/runs` reads per active status per sweep, and only when more than 100 CI runs are active. Otherwise it is unchanged at 1 read per status. The trade is fewer silent skips for a bounded number of extra calls, which fits the §14 rule to extend the existing call rather than add a new lookup.
   - Finding check: new — same evidence as Reliability 3.

Observations (no change):
- **`GH_PAT_BUDGET` is healthy.** The sweep token read `remaining=2374 used_in_job=21` (45577059246) and then `remaining=1796` (45581342861) with `reset=1795598459` (about 09:21 UTC), so it was nowhere near exhaustion. The heal-intake token read `remaining=4154 used_in_job=30` (45582100791). No HTTP 429 or secondary-rate-limit lines appear in any summary.
- **The merge-train listing pages through about 283 queued runs per tick** (`MERGE_TRAIN_RUNS_LISTING ... read=99 total=283 attempt=1/2`, 45575592854), with `elapsed_ms` of 20,154–26,210. That is load-driven and already capped to one listing pass per tick.
- **The promote follower's 404 is expected.** `GH_API_RETRY outcome=permanent ... status=404 ... tags/promote-cycle-frozen` (45576302512) is handled as "tag absent" (`scripts/promote_candidate_follow.sh:168-172`). Only the warning annotation is noise.
- **`pr_checks` uses one paginated commits read per PR** (45582701939, 45581283534), and `GH_LOOP_CALL_LINT warnings=0`.

## Prompt Cache & Memory System

- `cache_hit_rate` was 0.685475 for review_autofix (the only family with OpenRouter usage). The other 5,159,554 tokens (cache reads plus cache writes) still show heavy cache use, but writes (732,325) reached 16.5% of reads.
- Per run:

  | Run | Hit rate | Cache writes | Uncached prompt tokens |
  |---|---|---|---|
  | 45543404617 | 0.827 | 169,729 | not listed |
  | 45533322461 | 0.626 | 216,666 | not listed |
  | 45552464821 | 0.554 | 345,930 | 680,496 (8 calls) |

  Run 45552464821 was round 3 for PR #9999, and its per-reviewer telemetry was `unavailable reason=flagged_by_missing` for all 5 reviewers.
- `CONTEXT_BUDGET_WARN`: 0, so prompt growth is not putting pressure on the context budget.
- What is causing the lower hit rate cannot be seen without logs. The `static_prefix_sha256` in `REVIEWER_PROMPT_COMPONENTS` is the existing signal to compare across the rounds of 45552464821.
- No new recommendation. Prompt-prefix and reordering work was rejected earlier (processing report, the "Prompt Cache prefix fingerprint and reordering" row).

## Orchestrator Health

Indicators to track (no new code change):
- **`main` merge-lease backlog.** `MERGE_LEASE_QUEUE base=main holder=9692 held_s=25845–27152 waiters=32`, with a second holder #9867 (`holder_open`). Every `orchestrator/project-*` base had `outcome=no_waiter reason=lease_free` (45576979173). The guards for failed CI (`revoked_final_ci_failed` in `scripts/pr_checks_lib.sh`) and maximum hold (720 min) are already on this ref. Holders are kept while their runs sit queued (`holder_runs_active`, #9983), so runner queueing directly lengthens lease holds (inference).
- **Poller.** 30 of 40 `orchestrate_poll` runs were cancelled before their first step (for example 45579803702 after 1,462 s, 45574931452 after 1,066 s), which is the concurrency group replacing pending runs. That was decided earlier as normal.
- **Clarify/plan/implement/clarify-respond fan-out.** At least 9 events each produced 4 skipped runs; for example, 45584188009, 45584187990, 45584188307 and 45584188080 all at 09:18:42. Narrowing these predicates was rejected earlier because the phase wrapper predicate parity contract pins them.
- **Heal.** `workflow_failure_heal` had 102 runs, 1 of them a success and 101 skipped. Intake applied its throttle settings (`WORKFLOW_HEAL_MAX_OPEN_ISSUES: 10`, `HEAL_INTAKE_QUEUE_WAITED_SECONDS` 2–3 s), and the non-retryable backfill found `candidates=0`.

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact:
1. **Runner queueing (queue).** Waits of 15–32 min between 08:21 and 08:36 (see the Executive Summary runs), easing to about 2 min by 09:10 (45582912761, 45582794334). It inflates every stage, including `CI quick` (11.5–12 min waits in 45580183396 and 45574733550). Pacing was decided earlier, so no change is proposed.
2. **Review/autofix (compute and retry).** p50 6,487.5 s and p95 11,032 s over 150 runs. Runs whose 4 reviewers all raised 0 findings still took 13,535 s (45533322461) and 12,111 s (45543404617). That suggests the time is spent outside reviewer and editor work, in queueing and CI gates (an inference; step timings are unavailable). The fixable retry overhead is the #9442 resolver loop: Speed 1 and Reliability 1.
3. **Merge and conflict.** The 7.5 h, 32-waiter lease on `main`; see Orchestrator Health.
4. **Safety-net rerun gap.** Cancelled CI is not rerun under congestion: Reliability 3.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** hosted-runner queue (up to about 32 min per job); review_autofix p50 of about 1.8 h; the `main` merge lease (32 waiters).
- **Top failure modes:** the #9442 conflict-resolver loop (3 of the 3 review_autofix failures); 3 unrelated CI test-shard failures; the CI rerun safety net going inert at 100 or more active runs.
- **Highest-cost drivers:** review_autofix accounts for all 6.54M OpenRouter tokens; about 19,920 runner-seconds went to resolver failures.
- **Top 3 prioritized actions:**
  1. Count no-fingerprint resolver failures and add a per-PR escalation trigger (Reliability 1).
     - Finding check: new — `scripts/review_conflict_resolve.sh:3096-3097` and `:3116-3122` are unchanged, with no processing verdict.
  2. Paginate the active listing in `scripts/ci_cancelled_rerun.py` (Reliability 3).
     - Finding check: new — `listing_truncated` in 45581342861 and the single-page read at `:252-275`.
  3. Parse `RESOLVER_TERMINAL` in `scripts/cost_audit.py` (Reliability 2).
     - Finding check: new — no Python consumer of the `:532` record.

## Metrics Appendix

**Runs (coding-workflows, truncated window)**

| Metric | Value |
|---|---|
| Total runs | 998 (402 success, 6 failure, 54 cancelled, 536 other/skipped) |
| Failure rate | 0.60% of all runs; 1.3% of runs that were not skipped |
| p50 / p95 duration | 9 s / 7,780 s (summary); 7,714.55 s (per-repo) |
| review_autofix | 150 runs; 140 success / 3 failure / 6 cancelled; p50 6,487.5 s, p95 11,032 s |
| ci | 84 runs; 70 / 3 / 11; p50 2,247 s, p95 8,436 s |
| orchestrate_poll | 40 runs; 10 success / 30 cancelled; p50 724.5 s |
| Skipped-heavy families | workflow_failure_heal 101/102, clarify 90/96, orchestrate_clarify_respond 87/88, plan 82/89, implement 77/88 |

**Cost and review telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 20 (20 / 0) |
| Prompt / completion tokens | 1,299,071 / 83,332 |
| Cache read / write tokens | 4,427,229 / 732,325 |
| Total tokens | 6,541,957 |
| `cache_hit_rate` | 0.685475 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all runs, 116 samples) | 6,500 / 13,449,800 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix, 13 samples) | 11,518,000 / 14,847,080 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| Reviewer findings | 150 review runs, 3 with lines; counted rounds: PR 10051 and 10020, `raised=0` for all 4 reviewers; PR 9999 round 3 unavailable (`flagged_by_missing`, 5 reviewers) |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / bytes | 8 / 93,843 (about 11.7 KB per query) |
| Sources / static dup bytes / echo lines dropped | 46 / 0 / 0 |
| Bootstraps / failed / unused / total ms | 8 / 0 / 0 / 349,974 (about 43.7 s each) |
| `semble_fallbacks` (contract-test / runtime) | 20 (20 / 0) |
| Serena query / fallback / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` (sweep token) | remaining 2,374 → 1,796, reset about 09:21 UTC |
| `GH_PAT_BUDGET` (heal intake) | remaining 4,154, `used_in_job=30` |
| 429 / secondary-limit lines | 0 seen |
| Queued runs (merge-train listing) | 283 |

**Findings check**

| Status | Count |
|---|---|
| new | 4 (Reliability 1, Reliability 2, Reliability 3, Speed 2) |
| recurring | 0 |
| reopened | 0 |
| unverified | 0 |
| dropped: already fixed on this ref | 2 (lease release on failed CI: `revoked_final_ci_failed`; the security delta fallback is designed fail-open) |
| dropped: previously decided | 6 (Semble contract-test/runtime split; review dispatch pacing and queue timestamps; poller `cancelled_before_first_step`; clarify/plan fan-out narrowing; prompt-prefix reordering; AI-memory emission verification) |

**Data gaps**
- `coverage.repositories_truncated`: `shubhodeep1/coding-workflows`. No repository failed.
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` was missing, so there were no deep-dive excerpts. `AI_MEMORY_TELEMETRY`, `RESOLVER_TERMINAL`, and step timings could not be read.
- The processing report was readable, but its latest entry is 2026-10-08, so the 2026-11 analysis docs, including today's earlier report, have no verdicts yet.
- `claude_*` cost fields are not exported, so resolver and security-audit model spend is unmeasured.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 3 of 150 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 1.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | 2 | 0 | 0 | 0 | 0 |
| `google/gemini-3.1-flash-lite` | 2 | 0 | 0 | 0 | 0 |
| `minimax/minimax-m3` | 2 | 0 | 0 | 0 | 0 |
| `openai/gpt-6-luna` | 2 | 0 | 0 | 0 | 0 |
