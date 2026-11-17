## Executive Summary

> **Evidence gap.** The full run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted, and no `summary.json`, `errors/`, `slow/` or `recent/` folder was reachable. Every finding below comes from `analysis_context` (aggregates, plus `log_summary` on 44 sampled runs) and from reading the workflow and collector source in the repo. `insufficient_data=false`, but the success sample rate is only 0.07.

- **Duplicate review runs: the biggest cost lead (medium-low confidence; up to about 30–40% of review_autofix spend if confirmed).** Six pairs of review_autofix runs started about 14s apart. In each pair, one run was triggered by `pull_request` (unlabeled name) and one by dispatch (`[pr:N]`). Examples: 38852845383 / 38852864905 (04:52:27 and 04:52:41), 38845785700 / 38845804312 (03:29:39 and 03:29:53), 38838865641 / 38838886738, and 38841025707 / 38841046819.
  - Both runs in a pair spent OpenRouter tokens: 4.70M + 16.05M, and 4.76M + 6.71M.
  - In `internal-review.yml:50` the two triggers use different concurrency keys (`internal-review-dispatch-pr-N` vs `internal-review-pull_request-<head_ref>`), so they never serialize.
  - Whether both runs in a pair reviewed the same PR and head SHA is **not verifiable**, because the unlabeled run name carries no PR number. The first step is a diagnostic identity line.
- **The review_autofix critical path is 40–93 min (high confidence).** wall_clock p50 is 2,901,500 ms and p99 is 5,528,020 ms over 12 telemetry runs. The slowest run is 38852845383 at 5,585s. No step timings are available to say where the time goes.
- **Recurring CI failure in "Regression tests wired by the CI wiring guard (part 2)" (high confidence).** It failed in 38835248659 and 38838865495, after 4 earlier failures cited in `analysis/workflow-optimization-2026-10-16.md:132`. CI failure rate is 3/34 = 8.8%, and each rerun costs about 13 min (p50 809s).
- **The review checkout failure still can't be diagnosed (high confidence).** 38835616619 failed at `review / codex-agent` → "Checkout PR head branch" after 2,246s. That step has at least 4 bare `exit 1` paths (`review_autofix.yml:2809-2866`). The `REVIEW_CHECKOUT_FAIL` line recommended in the earlier report has **not landed**: a grep finds no occurrence outside `analysis/`.
- **Codex token telemetry is broken (high confidence).** It reports `codex_calls=14` but `codex_tokens_used=6` across clarify, plan and clarify-respond. A 495–819s plan or clarify run cannot use 2 tokens. `CODEX_TOKENS_RE` (`scripts/cost_audit.py:62`) is matching the wrong text, so Codex-phase cost is invisible.
- **Prompt cache is healthy overall but the collector under-reports it (high confidence).** Aggregate cache-read share is 65.96M / 81.69M input = **80.7%**, yet the summary-level `cache_hit_rate` is `null`. One outlier, 38852845383, ran at 0.40.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Confirm duplicate reviews, then skip them (critical path).**
   - **Evidence:** the 6 run pairs above. Runner and agent slots are shared, so a duplicate 25–90 min run delays every other PR's review.
   - **Root cause (inference):** after an autofix push, the `[ai-autofix]` synchronize review is deliberately not skipped (`review_autofix.yml:4163-4166`). Any dispatch for the same PR (continuation or sweep) gets its own concurrency group (`internal-review.yml:50`).
   - **Exact change, phase 1 (diagnostic only):** emit `REVIEW_RUN_IDENTITY pr=<N> head_sha=<sha> event=<event_name> concurrency_group=<group> run_id=<id>` once, at review gate start.
   - **Phase 2 (only if the collector finds matching `pr` + `head_sha` pairs):** early-exit the second run with `REVIEW_DEDUP_SKIP pr= head_sha= active_run=`. The active-run snapshot the sweep already builds (`review_autofix_sweep.yml:383`) is the reuse point.
   - **Estimated savings:** one full review per duplicated push, about 25–60 min of agent wall-clock per pair.
   - **Risk:** low for phase 1. Phase 2 must fail open, meaning it runs the review whenever the lookup fails.
2. **Step-level timing for long review runs (prerequisite for any further speed work).**
   - **Evidence:** run-level durations only. The 12 telemetry runs range from 2,545s to 5,585s with nothing below run level.
   - **Change:** have the collector copy the top 5 slowest steps per run (`started_at` / `completed_at`) from the jobs payload into each run row. That payload is already fetched to fill `failure_point`, so this adds no API calls (inference).
   - **Savings:** enables targeted cuts. Risk: none, since it only changes the collector.
3. **CI `tests-release-and-log-analysis`: an unexplained ~6 min (possible critical-path win; low confidence).**
   - **Evidence:** in 38861571735, 38861565712 and 38860997660, about 6 minutes separate the "Audit gate template atomicity" output from job cleanup.
   - **Reading `ci.yml:1512-1606`:** more than 15 small test steps follow that one. The gap is most likely many quiet steps that the summary's truncation hid, not one hung step.
   - **Change:** the same step-timing capture as item 2. Only then decide whether to rebalance steps across the 20-min-timeout jobs.
   - **Savings:** unknown until measured.
4. **Semble bootstrap: about 39s per review run (micro-optimization).**
   - **Evidence:** 10 bootstraps took 393,629 ms in total, 31.4–44.0s each, with 0 failed and 0 unused.
   - **Change:** cache the Semble index with the existing `actions/cache` pattern (already used for `claude-cli-npm-*`), keyed on base SHA.
   - **Savings:** about 30–40s per review, which is about 1% of review p50. Risk: low, because a stale index falls back to lazy bootstrap.
5. **Tag-heavy checkouts (micro-optimization).**
   - **Evidence:** in 38863287448 (forward-merge), "the long tag-fetch listing dominates log volume", and that log was truncated at about 541,549 chars. In 38860996267, heal-pr-reconcile has a 60s gap that includes a full tag fetch.
   - **Source:** both use `fetch-depth: 0` (`internal-cancel-on-pr-close.yml:50`, `forward-merge-stable-to-main.yml:48`).
   - **Change:** set `fetch-tags: false` where tags aren't read. Verify that heal-pr-reconcile and forward-merge only need branches first.
   - **Savings:** about 10–40s per run, plus much smaller logs. Risk: low–medium if some later step needs tags.

## Cost Optimizations

Ranked by expected token savings.

1. **Duplicate review suppression (see Speed 1).**
   - **Evidence:** the pairs listed above spent 20.75M tokens (38852845383 + 38852864905) and 11.47M tokens (38845785700 + 38845804312).
   - **Estimated savings:** if half of these pairs are true duplicates, about 15–25M of the 82.3M OpenRouter tokens in the window.
   - **Quality risk:** none, as long as dedup keys on the exact `(pr, head_sha)`.
2. **Cache-fragmentation outlier, 38852845383.**
   - **Evidence:** cache_hit_rate 0.3998 versus 0.80–0.92 in the other runs. It had 2.01M uncached prompt tokens, 1.86M cache reads and 774K cache writes over 20 calls.
   - **Comparison:** 38852864905 also made 20 calls (2 rounds) and hit 0.796.
   - **Root cause (inference):** in the second round the prompt prefix diverged, for example because a new diff or Semble block was placed ahead of the stable rubric/system content.
   - **Change:** log a `PROMPT_PREFIX_HASH call=<n> hash=<sha256 of first N KB>` diagnostic per OpenRouter call, so the round where the prefix diverged can be identified. Then move per-round dynamic content after the stable prefix.
   - **Savings:** bringing this run to 0.80 moves about 1.9M tokens from uncached to cached reads.
3. **Context-budget pressure: 3 `CONTEXT_BUDGET_WARN` events.**
   - **Evidence:** one each in 38848252416, 38842735388 and 38835248643. All three are 12-call runs with the highest uncached prompt totals: 1.66M, 2.15M and 1.94M.
   - **Assessment:** prompt-size risk, not rubric pressure (`break_glass_count=0`).
   - **Change:** none yet. Add `bytes_by_section` to the existing warn line so the growing section can be identified.
4. **Semble is efficient and not noisy.**
   - **Evidence:** 156,024 bytes across 10 queries (about 15.6 KB each), 56 sources (about 5.6 per query) and 0 `static_dup_bytes`. That is about 0.3% of the roughly 11.7M uncached prompt tokens.
   - **Action:** none. Semble is not the source of the prompt growth.
5. **Serena** is disabled (`SERENA_ENABLED: false` in 38860996264). It logged no queries or probes, so there are no cost effects.
6. **Codex-phase cost is not measurable.**
   - **Evidence:** clarify, plan and clarify-respond use `MODEL_REASONING_EFFORT: high` (38862335829, 38860670779, 38860113853), but tokens can't be quantified (Reliability 3). Reasoning-level tuning for these phases is blocked until that is fixed.
   - **Also:** heal intake runs `claude-opus-5-5 effort=high` (38862424346, 38862402310, 38862081468, each 166–217s), but `claude_*` cost fields are absent from the per-family telemetry, so that is a data gap too.

## Reliability Improvements

Ranked by expected reduction in failures and reruns.

1. **CI "Regression tests wired by the CI wiring guard (part 2)": recurring failure.**
   - **Evidence:** 38835248659 (01:27) and 38838865495 (02:08), plus 4 earlier failures in the previous report.
   - **Root cause:** unknown, because the failing test names aren't captured. The step runs `pytest -q` (`ci.yml:1403-1411`), whose short summary already prints `FAILED <nodeid>` lines.
   - **Exact fix (diagnostic):** have the collector pull `^FAILED ` / `^ERROR ` lines from the failing step into `failure_point.tests`.
   - **Correlation (unconfirmed inference):** the step lists `tests/test_branch_rebuild_threshold_decision_log.py`, which was the conflict file in forward-merge PR #7473 (38860996264). That suggests main/stable drift in this test.
   - **Impact:** turns a 13-min rerun loop into one targeted fix. No fail-open concern, since this is read-only.
2. **Review checkout failure (38835616619), carried over and not landed.**
   - **Evidence:** the failure came after 2,246s, at a step with distinct exits: workspace unavailable (`:2809-2811`), OpenCode config present (`:2813-2817`), empty `HEAD_REF` (`:2820-2822`), and fetch failure after 4 attempts (`:2863-2865`). There is also `git_ref_health_check.sh repair` under `set -e` (`:2842`).
   - **Exact fix:**
     ```bash
     review_checkout_fail() { echo "REVIEW_CHECKOUT_FAIL pr=${PR_NUMBER:-} reason=$1 head_ref=${HEAD_REF:-}"; echo "::error::$2"; exit 1; }
     # e.g. review_checkout_fail workspace_unavailable "Review workspace is unavailable; refusing to start agents."
     ```
     Register the new prefix through an `agents.d/<pr>-review-checkout-fail.md` fragment.
   - **Impact:** classifies 100% of future failures at this step. Behavior is unchanged because the exit codes stay the same.
3. **Codex token parser.**
   - **Evidence:** 14 calls produced 6 tokens. Every Codex-bearing run shows 2 tokens per 4–6 calls (e.g. 38862335829, 38860670779, 38860113853).
   - **Root cause:** the `CODEX_TOKENS_RE` pattern `tokens\s+used[^0-9\n]*\n?\s*([0-9][0-9,]*)` probably matches an abbreviated format such as `12.3k` (captured as "12"), or prompt text. That is an inference about Codex v0.114.0 output.
   - **Fix:** have the Codex wrapper emit a structured `CODEX_USAGE input_tokens= output_tokens= cached_tokens= total_tokens=` line, and parse it ahead of the regex.
   - **Impact:** restores cost visibility for 4 workflow families. Fail-open: an absent line falls back to the regex.
4. **orchestrate-poll shard failure (38833064092, `orchestrate-poll (3)`).**
   - **Evidence:** a single occurrence. The same `FAILED`-line capture as item 1 applies. Shards are partitioned with `NR % total` (`ci.yml:1996-1999`), so the failing test name is deterministic and reproducible.
5. **Cancellations: 18 total.**
   - **Breakdown:** orchestrate_poll 10/40 (25%), heal_intake 5, implement 2, review_autofix 1 (38833666595, 1,277s).
   - **Assessment (inference):** the poll cancellations look like supersession by the concurrency group, which is healthy.
   - **Diagnostic:** emit `CANCEL_REASON superseded_by=<run_id>`, so supersession can be told apart from timeouts.
6. **MCP fallback health.**
   - **Semble:** all 36 `SEMBLE_FALLBACK` events are `contract_test` fallbacks in CI (4 or 8 per CI run). There were **0 runtime fallbacks** and 0 failed bootstraps. That is healthy fail-open test coverage, not a masked rollout failure.
   - **Serena:** no probes (disabled), so there's nothing to assess.

## AI Memory Health

- Only **one** `AI_MEMORY_TELEMETRY` line was visible in the window: `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues","source":"issue_pr_status.yml"}`, in 38860996270 (forward-merge PR #7473). That is the expected no-op.
- **No `retrieve` operations were visible.** Hit rate, `estimated_tokens` against budget, and the `keyword_method` split can't be computed.
- The runs most likely to retrieve had large truncations in their summaries: plan 38860670779 (about 738K chars), clarify 38862335829 (about 287K chars) and clarify-respond 38860113853 (about 226K chars).
- memory_maintenance ran once and succeeded (478s), with no telemetry.
- **Recommendation:** add `AI_MEMORY_TELEMETRY` to the collector's must-keep line list, so truncation never drops those lines. Then confirm that `retrieve` is emitted in plan, clarify and review. Without that, no memory regression can be detected.

## GH API Call Audit

- **Observed footprint is low, with no 429s or secondary-rate-limit events in any summary:**

  | Run | Job | `used_in_job` | `remaining` |
  |---|---|---|---|
  | 38863382758 | sweep | 13 | 4927 |
  | 38860996264 | post-merge-force-poll | 2 | 3634 |
  | 38860996264 | post-merge-validate-dispatch | 2 | 3634 |
  | 38862402310 | heal intake | 41 | 3207 |
  | 38861571840 | lint (one paginated commits call, 5 commits) | — | — |

- **Budget burn (inference):** the `remaining` value dropped from 3634 to 3207 within the same reset window (`reset=1792133170`) between 06:24 and 06:39. That is about 430 calls in 15 min from callers not visible here. The orchestrate poller is the likely large consumer, but its logs were truncated (38860936811: about 821K chars).
- **Gap:** `used_in_job=unknown` in 2 of 3 heal-intake runs (38862424346, 38862081468). The start-phase budget record is missing or unread in that workflow. Fix: add the existing `Record GH_PAT budget` start step, the same pattern as `review_autofix.yml:1913`. It costs no extra API calls beyond the one `rate_limit` read.
- **Poller:** make sure `GH_PAT_BUDGET phase=end` with `used_in_job` is emitted at the end of the poller job, and that the collector keeps it under truncation. This is the only way to attribute the roughly 430-call burn.
- **Batching:** no unbatched per-item loops are visible in the evidence. The existing GraphQL batch helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) remain the reuse points for any poller fix. Any phase-2 review dedup (Speed 1) must read the sweep's existing active-run snapshot, not add a per-PR `gh run list`.

## Prompt Cache & Memory System

- **Per-run `cache_hit_rate`:**

  | Run | Rate |
  |---|---|
  | 38842334141 | 0.915 |
  | 38845804312 | 0.877 |
  | 38845785700 | 0.824 |
  | 38835248643 | 0.801 |
  | 38852864905 | 0.796 |
  | 38852845383 | **0.400** |

- **Collector bug 1:** the rate is `null` whenever a run has any `or_usage_unavailable_calls`. Recomputing from the logged totals with the collector's own formula, read / (prompt + read + write), gives 0.778 for 38848252416 (2 unavailable calls) and 0.837 for 38842735388 (1 unavailable call). Fix: report these as partial rates with a `cache_hit_rate_partial=true` flag instead of null.
- **Collector bug 2:** the summary, per-repo and per-family `cache_hit_rate` is `null` even though totals exist. The aggregate is **0.807**. Fix: compute it from the summed tokens.
- **Fragmentation:** the only clear case is 38852845383. Its second round wrote 774K cache tokens, about 2× a one-round run. Diagnose it with the per-call `PROMPT_PREFIX_HASH` line (Cost 2).
- **Budget pressure:** the 3 `CONTEXT_BUDGET_WARN` runs still cached well (0.78–0.84), so the pressure is not yet eroding cache value.
- **Configuration:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` is confirmed (38860113853), so caching is on as intended.

## Orchestrator Health

- **Project #7467 is progressing:**
  - Integration PR #7470 is `[ready] all 3 sub-issue(s) on #7467 are ticked` (38861571704).
  - Validate `[tracking:7467]` succeeded in 813s (38857750512).
  - CI on `orchestrator/project-7467` is green (38861565712).
  - The PR body uses `Refs #7467` (38861571840), so it is compliant with §21.
  - The sweep correctly skipped #7470 with `reason=active_run` (38863382758).
- **Heal loop overhead:**
  - Issue #7476 ("Workflow heal: AI Clarify failed 1x … (clarify_failed)") went through clarify-respond (629s, auto-answered `Q1: A`) and then plan (819s).
  - Issue #7477 (`ai:workflow-heal`) ran clarify for 495s.
  - Together that is about 32 min of high-reasoning agent time on heal issues in roughly 25 min of wall-clock.
  - **Indicator to track (inference):** count heal issues that reach plan, and agent minutes per heal issue. The guards are in place (`WORKFLOW_HEAL_MAX_ISSUES_PER_DAY: 20`, `MAX_LINEAGE_DEPTH: 3`).
- **Skip fan-out is noise, not a fault:**
  - 717 of the 720 "other" runs are 1–11s skips: clarify 149, orchestrate_clarify_respond 144, plan 137, implement 135, workflow_failure_heal 80, heal_intake 40, check_failure_triage 32.
  - They distort the repo-wide p50 (1.0s). Recommendation: have the collector report p50/p95 over non-skipped runs as well.
- **Clarify cycle cap:** `ORCHESTRATOR_MAX_CLARIFY_CYCLES: 3` was set, and no evidence of hitting the cap is visible.

## Pipeline Flow Bottlenecks

Stages in end-to-end order, with the evidence per stage:

| Stage | Compute | Retry / rerun overhead | Notes |
|---|---|---|---|
| Clarify | 495s (38862335829) | — | p95 53s, because skips dominate |
| Plan | 690–819s (8 slow plan runs) | — | — |
| Implement | 1,256–1,737s (10 slow implement runs) | — | no token telemetry at all |
| Review/autofix | p50 2,901s, p99 5,528s | duplicate-run suspicion (Speed 1) | **dominant bottleneck** |
| CI | p50 809s | 8.8% failure rate, about 13 min per rerun | gates every review and merge |
| Validate / poll | validate 813s; poll p50 581s | 25% of poll runs cancelled | — |

- **Queueing:** runner pickup is negligible everywhere it was observed: 0.2s on plan 38860670779, under 1s on CI 38861565712.
- **Merge/conflict:** one forward-merge conflict (PR #7473, `tests/test_branch_rebuild_threshold_decision_log.py`), which is the likely link to the part-2 CI failures.
- **Fix order by end-to-end impact:**
  1. Review identity diagnostics, then dedup.
  2. Part-2 failing-test capture and fix.
  3. Step timings for review and CI.
  4. `REVIEW_CHECKOUT_FAIL`.
  5. Codex usage line.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - review_autofix at 40–93 min per run (38852845383: 5,585s; 38845785700: 5,067s).
  - Implement at about 28 min (38831525749: 1,737s).
  - CI at about 13.5 min per run.
- **Top failure modes:**
  - CI part-2 regression step (38835248659, 38838865495).
  - Review checkout exit with no recorded reason (38835616619).
  - orchestrate-poll shard 3 (38833064092).
- **Highest cost drivers:**
  - review_autofix accounts for 100% of the measured 82.29M OpenRouter tokens.
  - Codex and Claude phases are unmeasured.
- **Top 3 actions:**
  1. Add `REVIEW_RUN_IDENTITY` and `REVIEW_CHECKOUT_FAIL` structured lines to `review_autofix.yml`, registered via an `agents.d/` fragment.
  2. Extend `scripts/cost_audit.py`: parse a `CODEX_USAGE` line, compute aggregate and partial `cache_hit_rate`, and extract pytest `FAILED` lines from failing steps.
  3. After the identity data confirms duplicates, add head-SHA review dedup that reuses the sweep's active-run snapshot and fails open.

## Metrics Appendix

**Runs**

| Metric | Value |
|---|---|
| Total runs | 1000 |
| Success / failure / cancelled / other (skipped) | 258 / 4 / 18 / 720 |
| Failure rate (all runs / non-skipped 280) | 0.4% / 1.4% |
| p50 / p95 duration (all runs) | 1.0s / 818s |
| Success sample rate | 0.07 |

**Workflow families**

| Family | Runs | Success | Fail | Cancelled | p50 | p95 |
|---|---|---|---|---|---|---|
| review_autofix | 49 | 45 | 1 | 1 | 282s | 4,339s |
| ci | 34 | 31 | 3 | 0 | 809s | 937s |
| orchestrate_poll | 40 | 30 | 0 | 10 | 581s | 958s |
| implement | 146 | 9 | 0 | 2 | 1s | 1,359s |
| plan | 145 | 8 | 0 | 0 | 1s | 553s |
| clarify | 157 | 8 | 0 | 0 | 1s | 53s |

**Tokens and cache**

| Metric | Value |
|---|---|
| OpenRouter prompt / completion | 11,730,725 / 596,204 |
| OpenRouter cache read / cache write | 65,956,646 / 4,005,058 |
| OpenRouter total tokens | 82,287,634 |
| OpenRouter calls (usage available / unavailable) | 94 (91 / 3) |
| Aggregate cache-read share (computed here) | 0.807 (collector reports `null`) |
| Per-run `cache_hit_rate` range | 0.400–0.915 (2 runs `null`) |
| Codex calls / tokens | 14 / 6 (parser defect) |

**Review telemetry**

| Metric | Value |
|---|---|
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo) | 1,000 / 4,999,920 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix) | 2,901,500 / 5,528,020 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 3 (38848252416, 38842735388, 38835248643) |

**GH API**

| Signal | Value |
|---|---|
| 429 / secondary rate-limit events | 0 visible |
| `used_in_job` samples | 13, 2, 2, 41, unknown ×2 |
| Lowest `remaining` observed | 3,207 (38862402310) |

**Semble**

| Metric | Value |
|---|---|
| Queries / bytes | 10 / 156,024 (about 15.6 KB per query) |
| Sources / static dup bytes | 56 / 0 |
| Bootstraps (failed / unused) | 10 (0 / 0) |
| Bootstrap time | 393,629 ms total, about 39.4s average |
| Fallbacks: contract-test / runtime | 36 / 0 |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (disabled) | 0 | 0 | 0 |
| Semble (not probed, availability via bootstrap) | n/a | n/a | n/a |

- Serena queries, response bytes and tool calls are all 0.
- **Other MCP servers observed:** none.
- **Data gaps:**
  - The run-logs directory was not mounted.
  - Claude cost fields are missing from the per-family telemetry.
  - No step-level timings.
  - No `AI_MEMORY_TELEMETRY` `retrieve` lines.
  - Poller GH API usage is hidden by log truncation.

---

**Actions taken:** I read the analysis context and the relevant workflow and collector source: `review_autofix.yml:2780-2869` and `:4150-4179`, `internal-review.yml:1-69`, `ci.yml:1403-1606` and `:1973-2002`, and `scripts/cost_audit.py:59-64` and `:689-695`. I checked the earlier 2026-10-16 report so this one doesn't repeat it. No files were changed; this report is the deliverable.

**Assumptions:**
- The paired review runs may not be for the same PR, so the duplicate finding is labelled as an inference and its first step is diagnostic only.
- The 6-minute CI gap is attributed to truncation of many short steps, not to one hung step.

**Missing context:** without the mounted logs, the specific causes of the part-2 test failures and the checkout failure can't be determined.
## Deep Audit — Workflows & Scripts (2026-10-16)

> **Scope and method.** I read these in full or in the cited ranges: `CLAUDE.md`, `.github/ai/label_contract.v1.json`, `internal-review.yml`, `review_autofix_sweep.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `sync_ai_labels.yml`, `orchestrate_poll.yml:180-229`, `implement.yml:4933-5170`, `workflow-log-analysis.yml:1057-2125`, `review_autofix.yml:2770-2910, 3305-3360, 4085-4130, 4835-5139, 5430-5474`, `scripts/gh_helpers.sh:1-1430, 1880-1990`, `scripts/label_helpers.sh`, `scripts/tg_helpers.sh:15-305`, `scripts/review_merge_train.sh:1150-1440`, `scripts/orchestrate_poll_process.sh:21790-21900, 22030-22120, 22195-22284`, `scripts/workflow_failure_heal_intake.sh:432-471`, `scripts/nag_reminder.sh` and `tests/test_workflow_file_size_limit.py`. The other files were covered by targeted greps only. I measured no character or byte counts with tooling; every size below is an estimate and says so.
>
> **Already covered in this report, not repeated here:** the duplicate review runs and `internal-review.yml:50` concurrency keys (Speed 1), the `REVIEW_CHECKOUT_FAIL` exits at `review_autofix.yml:2809-2866` (Reliability 2), and the `CODEX_TOKENS_RE` parser at `scripts/cost_audit.py:62` (Reliability 3).

### Section 1: Bug & Correctness Sweep

**BUG-001**: `scripts/label_helpers.sh:220-254` · Severity: **High** · Category: `bug`
- **Description:** `set_issue_phase_label_resilient` reads the label set, computes a new one, then replaces it with `PUT …/labels`. Two things can go wrong:
  - **Empty read becomes "no labels" (can wipe labels).** The read at 221-222 is `gh_retry gh api --paginate … | jq -cs 'add // []'`. When `gh_retry` fails it writes nothing to stdout (gh_helpers.sh:1043-1054). `jq -cs 'add // []'` on empty input prints `[]` and exits 0. Unless the caller has `pipefail` set, the `if !` at 221 never fires and line 234 keeps `[]`. The PUT at 249-251 then replaces **every** label with just the target label, dropping non-phase labels such as `ai:orchestrator-managed` and `ai:orchestrator-tracking`. That is the label-loss class that stalls the orchestrator (see §21's incident). [NEEDS VERIFICATION: whether any caller sources this helper without `pipefail`.]
  - **Lost update even when the read succeeds.** Any label added or removed by another job between the GET (221) and the PUT (250) is overwritten.
- **Recommended fix:**
  - Read into a file with `gh_retry_to_file` (gh_helpers.sh:1149), so the exit status belongs to the API call and not to `jq`.
  - Treat an empty or `[]` result as a failed read and take the existing POST-add fallback (224-230).
  - Replace the full-set `PUT` with POST-add of the target plus one DELETE per phase label actually present. These are the same endpoints the merge train already uses (`review_merge_train.sh:949`, `:986`).

  ```bash
  _cur_file="$(mktemp)"
  if ! gh_retry_to_file "${_cur_file}" gh api --paginate "repos/${repo}/issues/${issue_number}/labels" --jq '.[].name' || [ ! -s "${_cur_file}" ]
  then
  	# existing POST-add fallback (lines 224-230)
  	:
  fi
  ```

**BUG-002**: `scripts/gh_helpers.sh:1271-1335` · Severity: **Medium** · Category: `bug`
- **Description:** `gh_api_json_to_file` never calls `_is_gh_permanent_failure`, while `gh_retry` (1099) and `gh_retry_to_file` (1184) both do. So 404, 422 and 401 responses are retried 5 times, with sleeps of 1+2+4+8 = 15 s. Several callers probe endpoints where a 404 is an expected answer:
  - `workflow_failure_heal_intake.sh:459` reads `pulls/${ISSUE_NUMBER}`.
  - `workflow_failure_heal_intake.sh:466` reads `compare/${HEAD_SHA}...${BIND_PR_HEAD}` after a force-push, where the old SHA may be gone.
  - `workflow_failure_heal_report.sh:235` reads `pulls/${ISSUE_NUMBER}` on an issue number.

  Each of these spends 5 calls and about 15 s instead of 1 call.
- **Recommended fix:** in the `else` branch at 1302-1323, add the same permanent-failure block as `gh_retry_to_file:1184-1192`: log a warning, truncate `outfile`, `return 1`.

**BUG-003**: `scripts/gh_helpers.sh:1350-1403` · Severity: **Medium** · Category: `bug`
- **Description:** `curl_gh_api` retries every non-2xx response, including permanent ones (404, 401, 422), up to `GH_RETRY_MAX_ATTEMPTS`. It also lacks the single-attempt rule for non-idempotent POSTs that `gh_retry` (1067-1069) and `gh_api_retry` (931-933) have. A POST through it that hits a 5xx or timeout after GitHub already accepted the write gets replayed, which can post duplicate comments. [NEEDS VERIFICATION: which of the `curl_gh_api` callers in `check_failure_triage.sh`, `workflow_failure_heal_intake.sh` and `workflow_failure_heal_report.sh` send POSTs; the `tg_helpers.sh:169,176` callers are GETs.] The header at 1340-1341 also says it "sleeps 30 s", but the code sleeps until the parsed reset (1384-1388).
- **Recommended fix:**
  - Return immediately on `4xx` other than 403/429.
  - Parse `-X POST` / `--data*` from the arguments and cap attempts at 1 unless the caller passes `--idempotent`, mirroring `_gh_cmd_is_unsafe_post`.
  - Correct the header comment.

**BUG-004**: `.github/workflows/review_autofix_sweep.yml:213, 228-239, 590-609` · Severity: **Medium** · Category: `bug`
- **Description:** The step runs under `set -euo pipefail` (213), but the open-PR listing at 228-237 is a raw `gh api --paginate`, even though `gh_api_retry` is already sourced at 216-220. One transient error fails "Enumerate open PRs…". Two later steps have no status-check function, so they default to `success()` and are skipped for that tick:
  - "Re-run cancelled CI failed jobs once" (590-598), which also loses the snapshot it reads, written at 239.
  - "Re-dispatch stale security holds" (600-601).

  The backfill step (564) already uses `!cancelled()` for exactly this reason.
- **Recommended fix:**
  - Use `GH_TOKEN="${READ_TOKEN}" gh_api_retry --paginate -X GET …` at 228.
  - Add `${{ !cancelled() && … }}` to the `if:` of steps 590 and 600.
  - Have `ci_cancelled_rerun.py` fail open when the snapshot file is missing. [NEEDS VERIFICATION: current behaviour of `ci_cancelled_rerun.py` on a missing file.]

**BUG-005**: `.github/workflows/implement.yml:5151-5168` · Severity: **Medium** · Category: `bug`
- **Description:** In "Handle no-op implementation" (`set -euo pipefail` at 4942), the fall-through comment POST `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` has no `|| true`. It is also a non-idempotent create, so `gh_retry` makes one attempt (gh_helpers.sh:1067). If that POST fails, the step exits before 5157-5168 apply `ai:implementation-failed`, which is the label the orchestrator re-issue path keys on. The three earlier guards in the same step all tolerate a comment failure (4989, 5032, 5123). The POST also prints the full comment JSON to the log, because it has no `>/dev/null`.
- **Recommended fix:** apply the label first (5157-5168), then post the comment with `>/dev/null 2>&1 || echo "::warning::no-op comment post failed for #${ISSUE_NUMBER}"`.

**BUG-006**: `.github/workflows/cancel_on_pr_close.yml:128-147, 266-284` · Severity: **Medium** · Category: `bug`
- **Description:** Close-time cleanup lists and cancels only `event=pull_request` runs (133, 143, 275). Review runs dispatched by the sweep, the poller and the merge train are `workflow_dispatch` runs from the default branch, named `Internal: AI Review & Autofix [pr:N]` (`internal-review.yml:8`). They never match, so they keep running after the PR closes. The only stop is the closed-PR watchdog (`gh_review_pr_state`, called at `review_run_reviewers.sh:4247` and `review_apply_fixes.sh:2121`), which covers the reviewer and editor phases only. [NEEDS VERIFICATION: how much runner time a closed PR's dispatched run spends before the watchdog fires.]
- **Recommended fix:** add one paged `gh_api_retry` listing of `actions/workflows/internal-review.yml/runs?event=workflow_dispatch&status=…` per status. Match `display_title` against `[pr:<PR_NUMBER>]` with default-branch provenance, reusing the `dispatch_pr` jq definition from `review_autofix_sweep.yml:376-385`. Cancel the matches with the existing loop at 304-320.

**BUG-007**: `scripts/tg_helpers.sh:124-127, 148-150` · Severity: **Medium** · Category: `bug`
- **Description:** `tg_send_msg` and `tg_delete_msg` call `curl -s -X POST https://api.telegram.org/…` with no `--max-time` or `--connect-timeout`. The rate-limit alert in `gh_helpers.sh:421, 465, 509, 526` uses `--max-time 10`. A stalled Telegram connection therefore blocks the calling step, for example the five Telegram steps in `review_autofix.yml` (4100, 4845, 4995, 5136, 5440), until the job timeout. That turns a best-effort notification into a hang.
- **Recommended fix:** add `--connect-timeout 5 --max-time 10` to every Telegram `curl` in `tg_helpers.sh`, matching `_gh_ratelimit_tg_alert`.

**BUG-008**: `scripts/tg_helpers.sh:22-29, 203-211` · Severity: Low · Category: `bug`
- **Description:** `tg_helpers.sh` sources `scripts/gh_helpers.sh` relative to the current directory. By contrast, `gh_helpers.sh:120` and `label_helpers.sh:13` resolve their own `BASH_SOURCE` directory. If the caller's working directory has no `scripts/`, and `gh_helpers.sh` has not been sourced already, `curl_gh_api` falls back to plain `curl` (28). Plain `curl` exits 0 on HTTP errors, so `_tg_newest_comments_json` fails to parse and returns 1. `tg_store_msg_id` then POSTs a **new** tracking comment on every message (206-210). That is the comment-spam failure the file itself documents at 157-159.
  - `review_autofix.yml` (4101-4102) avoids this only by sourcing `gh_helpers.sh` straight after, which redefines the fallback.
- **Recommended fix:** source `"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gh_helpers.sh"`, using the same pattern as `label_helpers.sh:13`.

**BUG-009**: `scripts/tg_helpers.sh:214-229, 282-298` · Severity: Low · Category: `bug`
- **Description:** `tg_store_msg_id` and `tg_store_phase_msg_id` read the tracking comment, edit its ID list with `sed`, and PATCH it back. Two concurrent jobs on the same issue or PR, such as a review run and a poller tick, lose one job's message IDs. Those Telegram messages are then never cleaned up.
- **Recommended fix:** the race can't be removed without a lock. Narrow it: re-read right before the PATCH and merge the ID sets. Or keep one marker comment per run (`tg_cleanup:<run_id>:…`) and have the cleanup read all of them.

**SEC-001**: `scripts/tg_helpers.sh:124, 207, 226, 233, 275, 295, 301`; `scripts/gh_helpers.sh:417` · Severity: Low · Category: `security`
- **Description:** These calls put `Authorization: token ${GH_TOKEN}` and the Telegram bot token (`…/bot${TG_BOT_SECRET}/…`) on the `curl` command line. Command lines can be read through `/proc/<pid>/cmdline` by any process running as the same user on the runner (inference). Masking hides them from the Actions log, but not from other processes. GitHub API calls made through `gh` take the token from the environment instead.
- **Recommended fix:** send GitHub calls in `tg_helpers.sh` through `gh_retry gh api`, so the token stays out of argv. For Telegram, pass the URL through a `0600` temp file (`curl --config <file>`) and delete it afterwards.
- **No injection finding:** untrusted PR and issue text reaches shell only through `env:`, for example `lint-pr-body-auto-close.yml:60-61`, `issue_pr_status.yml:204-205`, `orchestrate_clarify_respond.yml:75,122,612` and `review_autofix.yml:593-594`.

### Section 2: GitHub API Call Redundancy Audit

**API-001**: `scripts/orchestrate_poll_process.sh:21805-21853` (callers at 10523, 12688, 16927, 17355, 20627, 21695, 21723, 25075, 25158, 25420, 26259, 26318, 29160, 29594) · Severity: **Medium** · Category: `api-redundancy`
- **Description:** `_has_active_autofix_run` runs `gh_retry gh run list --workflow <wf> --branch <head>` once for each of `ai-review.yml`, `internal-review.yml` and `review_autofix.yml` (21811-21819). When those find nothing it then calls `_pr_named_review_dispatch_runs`, which reads up to 10 pages for each of two wrappers (22085-22103). It is reached through `_dispatch_review_for_conflicts` (22233) from per-PR loops: the ready-to-merge loop (25075), the in-progress loop (25158) and the review-blocked loop (26259, 26318). The sweep already proved the cheaper shape: one snapshot per tick, then local lookups (`review_autofix_sweep.yml:292-439`).
  - `ai-review.yml` does not exist in this repo. If `gh`'s "workflow not found" error text doesn't match `_is_gh_permanent_failure` (gh_helpers.sh:194), each lookup costs 5 attempts and about 15 s of backoff. [NEEDS VERIFICATION: the exact `gh run list --workflow <missing>` error text.]
- **Current calls:** 3–5+ per PR evaluated, so 3N–5N per tick.
- **Proposed:** about 6 paged calls per tick (3 statuses × 2 wrappers) plus 0 per PR.
- **Pattern to extend:** copy `snapshot_active_review_runs` (sweep 300-435) into a poller helper and cache its result in a cycle-local file next to `_CONFLICT_DISPATCH_TRACKER` (22223). Keep today's fail-closed rule: an incomplete snapshot means skip the dispatch.

**API-002**: `.github/workflows/internal-cancel-on-pr-close.yml:6-7,16`; `cancel_on_pr_close.yml:331-367`; `orchestrate_poll.yml:195-222`; `internal-orchestrate-poll.yml:5` · Severity: **Medium** · Category: `api-redundancy`
- **Description:** Two workflows run the global merge-train release scan (`review_merge_train.sh release`, which calls `_mt_release` at 1333) every 5 minutes in this repo:
  - the poller backstop, on every tick (`orchestrate_poll.yml:195-222`);
  - `cancel_on_pr_close.yml`'s scheduled path, which `internal-cancel-on-pr-close.yml` also fires on `*/5 * * * *`.

  Each scan costs:
  - an open-PR listing;
  - when any PR is queued, the in-flight listing, which reads 5 statuses twice: at least 10 calls (`review_merge_train.sh:1166`);
  - per queued PR: a paged file list (222), a marker-comment read (804) and blocker evaluation.

  The step's own comment says the scheduled release exists because "core-profile repos have no poll backstop" (cancel_on_pr_close.yml:334). Full-profile repos like this one pay twice.
- **Current calls:** 2 scans per 5 min, so about 2 × (2 + 10 + 3Q), where Q is the number of queued PRs.
- **Proposed:** 1 scan per 5 min. Halves merge-train API use.
- **Fix:** gate the scheduled release in `cancel_on_pr_close.yml` with `if: github.event_name != 'schedule' || vars.WORKFLOW_PROFILE == 'core'`. The `pull_request: closed` path stays unchanged.

**API-003**: `scripts/tg_helpers.sh:166-237, 245-251` · Severity: Low · Category: `api-redundancy`
- **Description:** Each `tg_send_tracked` call costs 3 GitHub calls: the issue GET for the comment count (169), the last comments page GET (176), then a PATCH or POST (225/232). `review_autofix.yml` makes several tracked sends per run (4118, 4861, 4940, 5051, 5102, 5124).
- **Current calls:** 3 per tracked message. **Proposed:** 1 per message after the first in a job.
- **Pattern to extend:** the `RUNNER_TEMP` + `flock` per-job cache in `gh_review_pr_state` (gh_helpers.sh:83-118). Cache the tracking comment's ID and body, and fall back to the 3-call path on a cache miss.

**API-004**: `scripts/label_helpers.sh:159-193, 218` · Severity: Low · Category: `api-redundancy`
- **Description:** `ensure_label_exists` sends `gh label create` (a POST) on every call. When the label already exists, GitHub answers 422 and the helper ignores it. `set_issue_phase_label_resilient` calls it on every phase transition (218).
- **Current calls:** 1 POST per call. **Proposed:** 0 after the first successful create or "already exists" in a job.
- **Fix:** record each confirmed label in a job-local file `${RUNNER_TEMP}/ai_labels_ensured_${GITHUB_RUN_ID}` and check it before the POST. This fails open: a missing file means the POST is sent as today.

**API-005**: `.github/workflows/review_autofix.yml:4976, 5059, 5110, 5132` · Severity: Low · Category: `api-redundancy`
- **Description:** The "Editor changes lost" and "Editor no-op suspicious" branches POST a **new** PR comment on every occurrence. A PR stuck in a no-op loop collects one comment per run. The same file already has a marker upsert (3335-3358), as does `review_merge_train.sh` (`_mt_upsert_comment`, 870-886).
- **Current calls:** 1 new comment per occurrence. **Proposed:** a marker upsert (1 PATCH), reusing the `PR_ISSUE_COMMENTS_FILE` already fetched (3326) so no extra read is needed. [NEEDS VERIFICATION: the poller's no-op recovery sweep greps for the `⚠️ **Editor no-op suspicious**` literal; check it doesn't depend on comment count or recency before switching to upsert.]

**API-006**: `scripts/orchestrate_poll_process.sh:22249-22259`; `scripts/review_merge_train.sh:1323-1329` · Severity: Low · Category: `api-redundancy`
- **Description:** Both dispatchers try `ai-review.yml` first. It doesn't exist in this repo, so every dispatch costs one failed `gh workflow run` before `internal-review.yml` succeeds.
- **Current calls:** 2 per dispatch here. **Proposed:** 1.
- **Fix:** resolve the wrapper once per process and memoize the first one that succeeds. Alternatively, order `internal-review.yml` first when `GITHUB_REPOSITORY` is `shubhodeep1/coding-workflows`.

**BATCH-001**: `.github/workflows/implement.yml:5084-5097` · Severity: Low · Category: `api-batching`
- **Description:** The ancestor-chain no-op walk makes 2 REST calls per hop: the issue body (5085) and the paged comments (5089). With the default `IMPL_NOOP_ANCESTRY_THRESHOLD=2`, that is 4 calls, and each paged comment read can be several pages.
- **Proposed:** 1 GraphQL call per hop: `repository{issue(number:$n){body comments(last:100){nodes{body}}}}`. That gives 2 calls total, still fail-open (break on any error). The hops are sequential, so they can't be aliased into one query.
- **Pattern to extend:** the aliased-query construction in `cancel_on_pr_close.yml:174-188`.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001**: five "active review run" snapshot implementations · Severity: **Medium** · Category: `duplication`
- **Description:** The same "is a review run active for PR N or branch B" question is answered five ways:
  - `review_autofix_sweep.yml:300-439` (`snapshot_active_review_runs`): statuses `queued in_progress pending`.
  - `orchestrate_poll_process.sh:21805-21853` (`_has_active_autofix_run`): `in_progress queued pending`.
  - `orchestrate_poll_process.sh:22043+` (`_pr_named_review_dispatch_runs`).
  - `gh_helpers.sh:1921+` (`_autofix_pr_named_review_runs`), nearly the same code as the poller's: same two wrappers, 10-page cap and 404-means-empty rule.
  - `review_merge_train.sh:1151+` (`_mt_inflight_review_branches`): statuses `requested pending queued waiting in_progress`, read twice.
  - `cancel_on_pr_close.yml:116-150` (`_cpr_fetch_active_runs`): `queued in_progress`.

  The status sets already differ (see CONSIST-005).
- **Recommended fix:** add one helper to `scripts/gh_helpers.sh`:
  - **Signature:** `review_active_runs_snapshot <repo> <out_file> [wrappers…]`.
  - **Output:** `{ "pr:<N>": count, "<branch>": count }`, with default-branch provenance as in sweep 372-385.
  - **Statuses:** `requested waiting pending queued in_progress`.
  - **On an incomplete listing:** rc 1.

  Callers to update: the sweep (437-439), `_has_active_autofix_run`, `_mt_inflight_review_branches` and `_autofix_pr_named_review_runs`'s callers. Document its batching contract per §15.

**DUP-002**: default-branch memo, four copies · Severity: Low · Category: `duplication`
- **Description:** Four places resolve and cache the default branch:
  - `gh_helpers.sh:1898-1919` (`_autofix_review_default_branch_prime`).
  - `orchestrate_poll_process.sh:21874-21900` (`_review_run_default_branch`).
  - `review_merge_train.sh:1142` (`_mt_resolve_default_branch`).
  - `review_autofix_sweep.yml:285-290`, inline.

  All four apply the same `^[A-Za-z0-9._/-]+$` and `!= "null"` checks.
- **Recommended fix:** one `gh_repo_default_branch <repo>` in `gh_helpers.sh`, memoized in a file under `${RUNNER_TEMP}`. Callers wrap it, keeping their existing names (§10).

**DUP-003**: `scripts/gh_helpers.sh:1060-1335` · Severity: **Medium** · Category: `duplication`
- **Description:** `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` are three copies of one retry loop: temp-file setup, rate-limit branch, backoff and final error. They have already drifted: the third copy lacks the permanent-failure check (BUG-002).
- **Recommended fix:** an internal `_gh_retry_core <mode:stdout|file|json> <outfile|-> <cmd…>`. Keep the three public names as thin wrappers so no caller changes (§10).

**DUP-004**: inline label creation, 44 sites in 14 files · Severity: **Medium** · Category: `duplication`
- **Description:** A grep finds 44 `gh label create` calls across 14 files, 43 of them outside the catalog helper in `label_helpers.sh`. Examples: `implement.yml` (8), `implement_handle_guard_block.sh` (10), `review_autofix.yml` (4), `review_rb_judge.sh` (4). Two scripts reimplement part of the catalog as `case` blocks: `review_autofix_step_mark_ready_to_merge.sh:18-27` and `review_rb_judge.sh:927-936`. The comment at `review_rb_judge.sh:916-917` justifies skipping `gh_retry` because of 422s, but `gh_retry` now treats HTTP 422 as permanent (gh_helpers.sh:194); see DEBT-001.
- **Recommended fix:** route every site through `ensure_label_exists <label> <repo>` (label_helpers.sh:159). Its catalog mirrors `label_contract.v1.json`, which also fixes CONSIST-001.

**DUP-005**: `.github/workflows/workflow-log-analysis.yml:246-385, 822-996, 1659-1856, 2264-2453; 1098-1191, 1886-1962` · Severity: **Medium** · Category: `duplication`
- **Description:** The four AI jobs repeat the same roughly 150-line setup: resolve engine, install Claude, Claude credential, Python, cache/install/persist/restore Codex, Codex config, uv, Semble install and index. The three AI passes also repeat the `AI_PHASE_FAILURE_V1` emitter (`emit_log_analysis_phase_failure` and `emit_deep_audit_failure`) and the attempt/backoff loop (1338-1396 vs 2031-2084).
- **Recommended fix:**
  - Add a composite action `.github/actions/setup-codex-cached`, extending `install-codex`, that owns the cache/persist/restore steps.
  - Add `scripts/wla_emit_phase_failure.sh <failed_step> <failure_mode> <attempts> <summary>`.
  - Add `scripts/wla_run_ai_pass.sh <role> <prompt_file> <out_file> <phase>`.
  - Update the four jobs. Moving these bodies out also removes the EXPR-001/EXPR-002 risk.

**DUP-006**: `.github/workflows/review_autofix.yml:4103-4117, 4849-4860, 4908-4909, 4999-5000, 5152-5157, 5445-5461` · Severity: Low · Category: `duplication`
- **Description:** Six steps build the same Telegram message by hand: `PR_URL` / `RUN_URL` / `ISSUE_NUM` / `MSG+="Issue: …"`.
- **Recommended fix:** add `scripts/review_autofix_step_tg_compose.sh` providing `review_tg_compose <headline>`, built from `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID`. Follow the `review_autofix_step_<slug>.sh` registration pattern described in agents.md ("Workflow file size limit").

**DUP-007**: `.github/workflows/cancel_on_pr_close.yml:337-346` vs `orchestrate_poll.yml:203-211` · Severity: Low · Category: `duplication`
- **Description:** The same 7 `MERGE_TRAIN_*` env defaults are copied in both workflows, so a default changed in one will drift from the other.
- **Recommended fix:** move the defaults into `review_merge_train.sh`'s env parsing and pass only the `vars.*` values from the workflows. [NEEDS VERIFICATION: which defaults the script already applies.]

### Section 4: Expression Size Limit Risk Assessment

> **What CI already guards:** `tests/test_workflow_file_size_limit.py:62-98` fails any `jobs.*.steps[].run` that contains `${{` and is at least 18,000 characters long. So no workflow step can currently be in the High band (≥18,000), assuming CI is green.
>
> **What's estimated:** the sizes below are inferences from line counts and visible line lengths, after YAML strips the block indentation. None were measured with tooling. Each fix is the same: replace `${{ github.* }}` inside the body with the runner's default env vars (`GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW`, `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`). With no `${{` left, the body is no longer one expression and isn't subject to the 21,000-character limit.

**EXPR-001**: `.github/workflows/workflow-log-analysis.yml:1068-1399` ("Run workflow log analysis") · Severity: **Medium** · Category: `expression-limit`
- **Description:** A 331-line run body with `${{ github.run_id }}`, `${{ github.run_attempt }}`, `${{ github.workflow }}`, `${{ github.server_url }}` and `${{ github.repository }}` at 1111-1113, 1126, 1132-1134 and 1171-1173. Several lines are over 200 characters (1253, 1258, 1310-1311, 1332).
  - **Estimated size:** 15,000–17,900 characters (inference; CI passes, so under 18,000).
  - **Headroom:** about 3,100–6,000 characters to 21,000.
- **Recommended fix:** substitute the 12 `${{ github.* }}` uses with the env vars above. That is behaviour-neutral, because they are the same values. Then extract the body as in DUP-005.

**EXPR-002**: `.github/workflows/workflow-log-analysis.yml:1867-2119` (deep audit) and `:2453-2717` (API redundancy pass) · Severity: Low · Category: `expression-limit`
- **Description:**
  - Deep audit: about 252 lines, `${{ }}` at 1897-1899, 1911, 1917-1919, 1950-1952. Estimated 12,000–14,000 characters.
  - API redundancy: about 264 lines, `${{ }}` at 2495-2497 and 2548-2550. Estimated 12,000–14,500 characters.
  - **Headroom:** about 6,500–9,000 characters each.
  - Both carry the inline `required_headings` validator and attempt loop, which tend to grow.
- **Recommended fix:** the same env-var substitution as EXPR-001.

**EXPR-003**: `.github/workflows/implement.yml:4941-5168` ("Handle no-op implementation") · Severity: **Medium** · Category: `expression-limit`
- **Description:** About 227 lines. Long prose comment and issue-body literals (4969-4975, 5008-5022, 5102-5109, 5131-5149) sit alongside `${{ }}` at 4946, 4948, 5151, 5159, 5161, 5165 and 5167. Every new guard adds another prose block.
  - **Estimated size:** 13,000–15,500 characters (inference).
  - **Headroom:** about 5,500–8,000 characters. [NEEDS VERIFICATION: exact length.]
- **Recommended fix:** use `${REPO}`, which is already assigned at 4948, or `${GITHUB_REPOSITORY}`, and build `RUN_URL` from the env vars. Move the four comment bodies to files under `prompts/`, rendered with `scripts/render_prompt.sh`, or move the whole body to `scripts/implement_handle_noop.sh`.

**EXPR-004**: `.github/workflows/review_autofix.yml:4995-5133` ("Telegram editor-noop-suspicious warning") · Severity: Low · Category: `expression-limit`
- **Description:** About 138 lines with `${{ }}` at 4999-5000, 5059, 5110 and 5132.
  - **Estimated size:** 8,000–9,500 characters.
  - **Headroom:** about 11,500 characters.
  - It grows every time a new no-op branch is added (three so far).
- **Recommended fix:** the env-var substitution, plus DUP-006.

**EXPR-005**: `tests/test_workflow_file_size_limit.py:66-98` · Severity: Low · Category: `expression-limit`
- **Description:** The guard has two blind spots:
  - It only walks `jobs.*.steps`. Composite actions under `.github/actions/*/action.yml` (for example `setup-runtime` at 146 lines and `run-soft-error-analyzer` at 166 lines) are never checked.
  - It has no early-warning tier, so EXPR-001 and EXPR-003 only surface once they cross 18,000.

  No `actions/github-script` `script:` inputs exist today (a grep found 0).
- **Recommended fix:**
  - Also iterate `runs.steps[].run` in `.github/actions/**/action.yml`.
  - Print, without failing, every `${{ }}` body of 15,000 characters or more, so growth shows up in CI logs.

**Workflow file size.** No workflow is anywhere near 800 KB. The binding limit is GitHub's 512,000 bytes (§24), and CI fails at 480,000 bytes (`tests/test_workflow_file_size_limit.py:26-27,44-59`). By line count the largest are `test-and-mark-stable.yml` (7,551 lines), `implement.yml` (6,494) and `review_autofix.yml` (5,690). Byte headroom under 480,000 was not measured. [NEEDS VERIFICATION: `wc -c` on these three.] The "1 MB / 800 KB" figures in this audit's own prompt are stale; see CONSIST-007.

### Section 5: Cross-Cutting Concerns

**Technical-debt markers.** A grep for `TODO`, `FIXME` and `HACK` across `.github/workflows/*.yml`, `scripts/*.sh` and `scripts/*.py` found 0 matches. No marker findings.

**DEAD-001**: `scripts/gh_helpers.sh:336-341` · Severity: Low · Category: `dead-code`
- **Description:** `gh_rate_limit_breaker_tripped` has no callers in workflows or scripts. Its own comment (313-318) says the only consumer, the `orchestrate_poll.yml` self-retrigger, was removed.
- **Recommended fix:** don't remove it (§10; consumer repos may call it). Mark it deprecated in the header and point to `gh_rate_limit_breaker_active` (894).

**DEAD-002**: `.github/workflows/review_autofix_sweep.yml:219` · Severity: Low · Category: `dead-code`
- **Description:** `source "${GH_API_RETRY_HELPERS:-/dev/null}"` always sources `/dev/null` here. `GH_API_RETRY_HELPERS` is only written by "Bootstrap GitHub API retry helper" steps in other workflows (`validate.yml:142`, `implement.yml:159`, `review_autofix.yml:357`, `security-audit.yml:103`, `orchestrate_clarify_respond.yml:113`). [NEEDS VERIFICATION: `tests/test_workflow_gh_retry_fallback_contract.py:406` may require this literal line.]
- **Recommended fix:** keep the line if the contract test needs it, and add a comment saying the sweep sources `scripts/gh_helpers.sh` directly (216-217).

**DEAD-003**: `scripts/gh_helpers.sh:91, 108` · Severity: Low · Category: `dead-code`
- **Description:** `gh_review_pr_state` accepts a `merged` value from `pulls/{n}` `.state`, but the REST pulls API only returns `open` or `closed`, so that branch can't be reached (inference based on the REST schema).
- **Recommended fix:** leave the behaviour as is and add a comment. A merged PR reports as `closed`, which callers already handle.

**DEBT-001**: `scripts/review_rb_judge.sh:916-936` · Severity: Low · Category: `tech-debt`
- **Description:** The comment says `gh label create` is deliberately not wrapped in `gh_retry` because of 422 "already exists". `gh_retry` now treats HTTP 422 as permanent and makes one attempt (gh_helpers.sh:194, 1099-1106), so the comment is out of date and transient 5xx failures go unretried.
- **Recommended fix:** call `ensure_label_exists` (DUP-004) and delete the stale comment.

**CONSIST-001**: label colour and description drift from the contract · Severity: Low · Category: `consistency`
- **Description:** These create-if-missing calls use different metadata from the contract:

  | Label | Call site | Hard-coded value | Contract value |
  |---|---|---|---|
  | `ai:closed` | `implement.yml:4976-4977`, `5110-5111` | colour `6b7280`, "Issue closed by automation (superseded, duplicated, or already complete)" | `label_contract.v1.json:156-158`: colour `6a737d`, "Linked PR closed without merge" |
  | `ai:needs-human` | `implement.yml:5023-5024` | colour `b60205` | `100-102`: colour `e11d48` |
  | `ai:log-analysis-failed` | `workflow-log-analysis.yml:1182-1185`, `1960` | "Workflow log analysis failed" | `84-86`: "Workflow log analysis failed before producing an analysis report" |

  Whichever path creates the label first decides its metadata until `sync_ai_labels.yml` runs.
- **Recommended fix:** use `ensure_label_exists` (DUP-004).

**CONSIST-002**: `.github/workflows/workflow-log-analysis.yml:319, 901, 1738, 2343` · Severity: Low · Category: `consistency`
- **Description:** These steps use `shubhodeep1/coding-workflows/.github/actions/install-codex@stable`, while the same jobs use local `./.github/actions/install-claude` (1707) and `./.github/actions/claude-pool-token` (1715). The workflow is not reusable (schedule and dispatch only, per the comment at 1676-1677). `security-audit.yml:273` and `workflow-failure-heal-intake.yml:228,409` already use `./.github/actions/install-codex`. As a result, changes to `install-codex` on `main` aren't exercised by this repo's own log-analysis runs until they reach `@stable`.
  - The reusable workflows (`implement.yml:254`, `plan.yml:103`, …) correctly keep `@stable`.
- **Recommended fix:** switch these four to `uses: ./.github/actions/install-codex`.

**CONSIST-003**: `.github/workflows/review_autofix_sweep.yml:274, 288, 485-491, 552` · Severity: Low · Category: `consistency`
- **Description:** The `AUTOFIX_SWEEP_END` line on the `default_branch_unavailable` path (288) is missing the `skipped_merge_queued=` and `snapshot_incomplete=` fields that 274 and 552 emit. The `claude_capacity_wait` skip (485-491) increments no counter, so the counters don't add up to `candidates`.
- **Recommended fix:** add a `skipped_capacity_wait` counter (a new key, so additive) and emit the same field set on all three END lines.

**CONSIST-004**: `.github/workflows/review_autofix_sweep.yml:577-609` · Severity: Low · Category: `consistency`
- **Description:** "Record GH_PAT budget at sweep end" (577) runs before "Re-dispatch stale security holds" (600-609), which uses `secrets.GH_PAT` (603). The step's `GH_PAT_BUDGET … used_in_job=` value therefore under-counts. This matters for the poller and sweep attribution gap in this report's "GH API Call Audit" section.
- **Recommended fix:** move the end-budget step to be the last step, keeping `if: always()`.

**CONSIST-005**: active-run status sets differ (see DUP-001) · Severity: Low · Category: `consistency`
- **Description:** The sweep (`review_autofix_sweep.yml:337`) and the poller (`orchestrate_poll_process.sh:21818`) don't treat `requested` or `waiting` runs as active, but the merge train does (`review_merge_train.sh:1166`). A review run sitting in `waiting` or `requested` is invisible to the sweep, which can then dispatch a duplicate. Inference: `waiting` normally only appears with environment protection rules.
- **Recommended fix:** use one status list (`requested waiting pending queued in_progress`) in the shared helper from DUP-001.

**CONSIST-006**: raw `gh` where neighbouring code uses the retry helpers · Severity: Low · Category: `consistency`
- **Description:** These calls use bare `gh`:
  - `review_autofix_sweep.yml:228` and `285` (raw `gh api`) and `543` (raw `gh workflow run`, with stderr discarded so the failure reason is lost).
  - `workflow-log-analysis.yml:1162`, `1182`, `1187`, `1944`, `1960-1961`.
  - `implement.yml:1949`, `2175`, `2194-2195`, `2217`.

  The same files already use `gh_retry` or `gh_api_retry` for the same endpoints, for example `implement.yml:1773` and `2403`.
- **Recommended fix:** wrap reads in `gh_api_retry` or `gh_retry`, and creates in `gh_retry` (which makes a single attempt for creates). Capture stderr into the `::warning::` line instead of discarding it.

**CONSIST-007**: `prompts/mode-workflow-audit.txt:83-84, 100` · Severity: Low · Category: `consistency`
- **Description:** The audit prompt says the workflow limit is "1 MB (1,048,576 characters)" and asks for flags above 800 KB. §24 and `tests/test_workflow_file_size_limit.py:26-27` put GitHub's limit at 512,000 bytes, with CI failing at 480,000. Its line references `review_autofix.yml:2370` and `:2576` also no longer point at the split steps; 2396-2408 is now the workspace-cache step.
- **Recommended fix:** change the prompt text to 512,000 / 480,000 bytes and refer to the incidents by PR or issue number instead of line.

**SHELL-001**: `scripts/orchestrate_poll_process.sh:21811, 22249` · Severity: Low · Category: `shellcheck`
- **Description:** `wf_candidate` is the loop variable in both `_has_active_autofix_run` and `_dispatch_review_for_conflicts`, and neither declares it `local`. It leaks into the global scope of a script of roughly 30,000 lines.
- **Recommended fix:** add `local wf_candidate` in both functions.

**SHELL-002**: `.github/workflows/implement.yml:5005`; `review_autofix_sweep.yml:458` · Severity: Low · Category: `shellcheck`
- **Description:** `printf '%s' "${var}" | grep -q…` runs under `set -o pipefail` (4942, 213). If `grep -q` exits on its first match before `printf` has finished writing (input bigger than the pipe buffer), `printf` gets SIGPIPE and the pipeline returns 141. In `implement.yml:5005` that would make the `.codex-workflow-src` hard-fail guard report a false negative. Inference: it is unlikely with today's input sizes.
- **Recommended fix:** use `grep -qE '…' <<< "${remaining_changes}"`, or a bash `[[ … =~ … ]]` test.

**SHELL-003**: `.github/workflows/review_autofix.yml:5077-5078` · Severity: Low · Category: `shellcheck`
- **Description:** `ls -1 … | wc -l` and `ls -1 … | sort -V | tail -n 1` parse `ls` output (SC2012).
- **Recommended fix:** `shopt -s nullglob; editor_errs=("${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err)`, then use `${#editor_errs[@]}` and `printf '%s\n' "${editor_errs[@]}" | sort -V | tail -n 1`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 14 | BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, BUG-007, API-001, API-002, DUP-001, DUP-003, DUP-004, DUP-005, EXPR-001, EXPR-003 |
| Low | 27 | BUG-008, BUG-009, SEC-001, API-003, API-004, API-005, API-006, BATCH-001, DUP-002, DUP-006, DUP-007, EXPR-002, EXPR-004, EXPR-005, DEAD-001, DEAD-002, DEAD-003, DEBT-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, CONSIST-005, CONSIST-006, CONSIST-007, SHELL-001, SHELL-002, SHELL-003 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/label_helpers.sh` | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/review_merge_train.sh`, `.github/workflows/cancel_on_pr_close.yml`, `scripts/tg_helpers.sh`, `scripts/label_helpers.sh`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml` | Medium |
| Code modularization | `scripts/gh_helpers.sh`, `.github/workflows/review_autofix_sweep.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/review_merge_train.sh`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/workflow-log-analysis.yml`, `.github/actions/install-codex/action.yml` (or a new composite action), `.github/workflows/review_autofix.yml`, plus the 14 files with inline `gh label create` | Large |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `scripts/gh_helpers.sh`, `scripts/tg_helpers.sh`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/implement.yml`, `.github/workflows/workflow-log-analysis.yml`, `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh`, `prompts/mode-workflow-audit.txt` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-16)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: the implement stage may make the change without further review. Both calls hit the same endpoint (or a strict subset of its fields) with identical filters, pagination and auth. They sit in the same function or step with no mutation in between. Error handling and retry behaviour stay the same, and no cycle-local cache contract or concurrency boundary is affected.
- **`NEEDS_VERIFICATION`**: the change looks correct, but static reading can't prove every one of those conditions. The listed checks must pass before anyone implements it.
- **`RISKY_SKIP`**: shown for visibility only. At least one hard trigger applies: retry or stall-recovery path, identity or permission probe, pagination, a rate-limit-sensitive path, `orchestrate_poll_process.sh`, race defence, or a change to a log key other code greps for. It must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001** · `RISKY_SKIP`
- **Files:** `.github/workflows/review_autofix.yml:1576-1582` (comments read) and `.github/workflows/review_autofix.yml:1598-1602` (PR read). Both are in the fingerprint-cap step.
- **Current calls:** 1 + P (P = extra comment pages beyond the first) + 1. **Proposed:** 1 + P GraphQL pages, using `pullRequest{state headRefOid title body comments(first:100){nodes{author{login} body}}}`.
- **Endpoints:** `GET /repos/{r}/issues/{n}/comments` (`--paginate`) and `GET /repos/{r}/pulls/{n}`.
- **Evidence:** the two reads run back to back in the same step and target the same PR. The step says the gate snapshot was re-read on purpose, after the per-PR lock:
  ```
  # The gate snapshot can predate another queued cap job. The
  # per-PR concurrency lock above serializes writers; re-read comments
  # after acquiring it so only the first job applies side effects.
  # The PR read below cannot provide comments or their author (§15).
  ```
  The PR read feeds `pr_state`, `current_head` and `pr_text` (1603-1620). The heal reporter also receives it as `PR_PAYLOAD_FILE` (1720), which expects the full REST pulls shape.
- **Proposed fix:** none for auto-implementation. A GraphQL merge would mean extending this step with one aliased query, following the pattern of `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`.
- **Safety rationale:** four triggers apply:
  - The comments read paginates.
  - Both reads are an explicit race defence: the `head_moved` and `pr_closed` checks at 1605-1614 run after the lock.
  - GraphQL `author.login` drops the `[bot]` suffix that REST `user.login` has. That would break the case-insensitive author match at 1593 and 1655.
  - `PR_PAYLOAD_FILE` consumers expect the REST pulls schema.
- **Downstream signal:** do not auto-implement. Manual review would have to prove three things: that `FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN` compares equal under GraphQL login formatting, that `workflow_failure_heal_autofix_report.sh` tolerates a non-REST PR payload, and that the post-lock re-read ordering is kept.

**MERGE-002** · `RISKY_SKIP`
- **Files:** `scripts/plan_split_dispatch.sh:98-99` (identity) and `scripts/plan_split_dispatch.sh:127-128` (default branch).
- **Current calls:** 2 on the full path (plus 1-2 workflow-state reads at 118-125, which GraphQL can't serve). **Proposed:** 1 GraphQL read, `{ viewer{login} repository(owner,name){defaultBranchRef{name}} }`.
- **Endpoints:** `GET /user` and `GET /repos/{r}`.
- **Evidence:** both calls are in one script with no write between them. Each only feeds a skip guard:
  ```
  pipeline_login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null || true)"
  [ -n "${pipeline_login}" ] || skip identity_unavailable
  ...
  default_branch="$(gh_retry gh api "repos/${REPO}" --jq '.default_branch // ""' 2>/dev/null || true)"
  [ -n "${default_branch}" ] || skip default_branch_unavailable
  ```
- **Proposed fix:** none for auto-implementation. If approved, replace both reads with one `gh_retry gh api graphql` read placed at line 98, and keep both skip lines.
- **Safety rationale:** two triggers apply:
  - `GET /user` is the identity anchor for the pipeline-authored split-marker dedupe at 106-111.
  - Merging moves the default-branch read ahead of the `already_split` and wrapper skips. One failure would then report `identity_unavailable` where today it reports `default_branch_unavailable`, which changes observable skip-reason output.
- **Downstream signal:** do not auto-implement. Manual review must confirm that nothing greps the plan-split skip reasons, and that a GraphQL `viewer` login matches the REST `.user.login` used in the comment filter at 103 and 108.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** · `NEEDS_VERIFICATION`
- **Files:** `.github/workflows/review_autofix_sweep.yml:228-237` (open-PR listing) and `.github/workflows/review_autofix_sweep.yml:285-290` (default-branch read). Both are in the same step under `set -euo pipefail` (213).
- **Current calls:** 1 per tick that reaches line 285. **Proposed:** 0. The sweep runs every 30 minutes (`cron: '*/30 * * * *'`, line 80), so this saves up to 48 calls per day per repo running the sweep.
- **Endpoints:** `GET /repos/{r}/pulls?state=open` (already fetched) and `GET /repos/{r}` (removed).
- **Evidence:** line 285 is reached only when `total > 0` (273-276), so the PR listing returned at least one PR. Every PR object's `base.repo` is this repository. The listing's jq projection (230-237) currently throws away `base`, which forces the second read:
  ```
  sweep_default_branch="$(GH_TOKEN="${READ_TOKEN}" gh api "repos/${REPOSITORY}" --jq .default_branch 2>/dev/null || true)"
  ```
  Both calls use the same `READ_TOKEN`. The only thing between them is a local file write (239).
- **Proposed fix:**
  - In the jq at 230, keep `(add // [])` as `$all`.
  - Emit `($all[0].base.repo.default_branch // "")` into a second variable, before the `.draft == false` filter, since drafts carry `base.repo` too.
  - Assign it to `sweep_default_branch` instead of making the read at 285.
  - Keep the regex and `null` validation and the `AUTOFIX_SWEEP_SKIPPED reason=default_branch_unavailable` fallback at 286-290 unchanged, so a missing field still skips the tick.
- **Safety rationale:** the two calls hit different endpoints, so the strict-subset condition for `SAFE_TO_MERGE` fails. The paginated listing's query parameters are not changed (only its local jq projection is), so the pagination trigger does not apply.
- **Downstream signal:** before implementing:
  1. Confirm from GitHub's REST schema or one recorded response that list-pulls `base.repo` includes `default_branch`.
  2. Run `grep -rn 'repos/${REPOSITORY}" --jq .default_branch' tests/` to make sure no contract test pins the literal at 285.
  3. Keep the provenance contract in the comment at 278-284 (one value per tick, no fallback) and update the comment.
  4. Coordinate with Deep Audit DUP-002 so this inline copy is not also folded into a shared memo.

**REUSE-002** · `RISKY_SKIP`
- **Files:** `.github/workflows/implement.yml:1806` (step "Fetch issue metadata", 1751; `GH_TOKEN: ${{ secrets.GH_PAT }}` at 1754) and `.github/workflows/implement.yml:2138` (step "Unblock judge bulk-delete override", 2121; `GH_TOKEN: ${{ secrets.GH_PAT }}` at 2132). Both steps are in the `implement` job (92).
- **Current calls:** 2 when a `override=bulk_delete -->` marker is present (2135); 1 otherwise. **Proposed:** 1.
- **Endpoint:** `GET /user`.
- **Evidence:** the first read is unconditional, and its value is used only locally (1814). The second step reads it again from the same token:
  ```
  pipeline_login="$(gh api user --jq .login 2>/dev/null || true)"     # 1806
  pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"   # 2138
  ```
  The comment at 1803-1805 covers the ordering in one direction only ("it cannot supply this grant's pipeline identity before the editor"). It does not cover reusing the earlier value later.
- **Proposed fix (if approved):** have step 1751 write the login as a step output with an `id:`, not `GITHUB_ENV`. Step 2121 would read `steps.<id>.outputs.pipeline_login` and fall back to `gh api user` when it's empty.
- **Safety rationale:** the value is the trust anchor that decides which `ai:unblock:v1` markers can lift the bulk-delete threshold (2143-2148), so this is an identity flow. Passing it through job state crosses the trust boundary of steps 1855-2075.
- **Downstream signal:** do not auto-implement. A human must confirm three things: no step between 1751 and 2121 can write the carrier (rule out `GITHUB_ENV`); step 1751 always runs whenever step 2121 runs (compare `if:` conditions); and the saving (1 call, only when a marker exists) justifies touching a destructive-guard override path.

**REUSE-003** · `RISKY_SKIP`
- **Files:** `scripts/review_single_issue_security_pass.sh:369-373` (gate mode, called from `.github/workflows/review_autofix.yml:4148` with `GH_TOKEN: ${{ secrets.GH_PAT || github.token }}` at 4135) versus the existing gate job output `fingerprint_cap_marker_author_login`. That output is declared at `review_autofix.yml:328` and written at `scripts/review_autofix_step_evaluate_gate.sh:1371`.
- **Current calls:** 1 `GET /user` per security-pass gate invocation. **Proposed:** 0 when the gate output is non-empty, else 1.
- **Endpoint:** `GET /user`.
- **Evidence:** the script says:
  ```
  # The gate job also probes /user, but its identity is not exported to this job.
  SECURITY_PASS_AUTHOR_LOGIN="$(gh api user --jq '.login // ""' 2>/dev/null || true)"
  ```
  That comment is out of date: the gate's login **is** exported, and two consumers already reuse it, `FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN` (1469) and `AUTOFIX_STREAK_AUTHOR_LOGIN` (5627). However, the gate fills it lazily. `gate_fetch_marker_comments` (`review_autofix_step_evaluate_gate.sh:326-340`) runs only when the partial or cap scans need it, so the output can be empty. The report-mode read at line 615 has no gate output in scope and is not part of this finding.
- **Proposed fix (if approved):** pass `SECURITY_PASS_AUTHOR_LOGIN_PRIMARY: ${{ needs.gate.outputs.fingerprint_cap_marker_author_login }}` to the step at 4134-4146. In the script, use it when non-empty, then the `/user` read, then `SECURITY_PASS_AUTHOR_LOGIN_FALLBACK`.
- **Safety rationale:** this is an identity probe that crosses a job boundary. The gate job's token must be proven identical to `secrets.GH_PAT || github.token`, or markers will be checked against the wrong author.
- **Downstream signal:** do not auto-implement. A human must check the gate job's `GH_TOKEN` expression against 4135, confirm the security-pass job has `needs: gate`, and confirm that `review_rb_judge_security_pass.sh:210` (the judge step at 4308) gets the same treatment or is deliberately left alone.

### Dead Calls (DEAD-API-###)

**DEAD-API-001** · `RISKY_SKIP`
- **Files:** `scripts/workflow_failure_heal_report.sh:209-211` (second read) and `scripts/workflow_failure_heal_report.sh:166-173` (first read).
- **Current calls:** 0 extra on success. On failure, a second full `gh_retry` series. **Proposed:** 0 extra in both cases.
- **Endpoint:** `GET /user`.
- **Evidence:** line 210 runs only when `LABEL = ai:needs-human` (208) and `HEAL_REPORTER_LOGIN` is empty (209). For that label, line 172 has always run first (166), so 210 can only be reached after 172's retries came back empty. It never fetches anything new on a success path. The comment at 206-207 ("The /user read happens only when the marker text is present") predates the shared read at 172.
- **Proposed fix:** none for auto-implementation. At most, correct the comment at 206-207 so it says 210 only re-reads after the line-172 read failed.
- **Safety rationale:** this is an identity read that only runs on retry. Removing it changes failure behaviour: a transient failure that a second retry series could recover from would instead reach `exit 1` at 213-214 (`non_retryable_marker_author_unavailable`).
- **Downstream signal:** do not auto-implement. Removing it needs a human decision that one `gh_retry` series is enough for this fail-closed path, and a check of heal-report logs for any recovery through line 210.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — the fix sits in `orchestrate_poll_process.sh`, replaces fail-closed race defence, and touches paginated `_pr_named_review_dispatch_runs`. Net-new detail: a glob confirms `ai-review.yml` is absent and only `internal-review.yml` exists, so the first lookup per PR in `_has_active_autofix_run` is always a miss in this repo.
- API-002: NEEDS_VERIFICATION — `vars.WORKFLOW_PROFILE == 'core'` is an established pattern (`orchestrate_poll.yml:34`, `review_autofix_sweep.yml:173`), but `cancel_on_pr_close.yml` doesn't reference it yet. Before gating, confirm no full-profile consumer runs without the poller backstop.
- API-003: NEEDS_VERIFICATION — the 2-read path at `tg_helpers.sh:160-163` is a deliberate fix for the 447-comment incident. A job-local cache must be updated after its own PATCH or POST (225, 232) and must never fall back to reading the oldest page. It also interacts with BUG-009.
- API-004: NEEDS_VERIFICATION — confirm that the job-local "ensured" file can't mask a label deleted mid-job by `sync_ai_labels.yml`.
- API-005: NEEDS_VERIFICATION — this changes PR-comment output that the poller's no-op recovery greps. Cadence and recency semantics must be proven unaffected first.
- API-006: NEEDS_VERIFICATION — `gh workflow run` is a POST, so memoizing must not skip the fallback after a transient failure. Net-new: the same first-candidate miss, in reverse order, exists at `plan_split_dispatch.sh:118-125`, where consumers pay a 404 on `internal-orchestrate.yml` first.
- BATCH-001: RISKY_SKIP — the per-hop comments read is paginated, and `comments(last:100)` changes page-boundary semantics for issues with more than 100 comments.

### Summary Counts
These counts cover this pass's findings plus the Deep Audit IDs cross-referenced above.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 6 | REUSE-001, API-002, API-003, API-004, API-005, API-006 |
| RISKY_SKIP | 7 | MERGE-001, MERGE-002, REUSE-002, REUSE-003, DEAD-API-001, API-001, BATCH-001 |

### Implement-Stage Handoff
- No `SAFE_TO_MERGE` findings in this pass. The best candidate is REUSE-001 (about 48 calls per day per repo running the sweep). It moves to `SAFE_TO_MERGE` once its four verification steps pass.