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