## Executive Summary

> **Data gap (read first):** the full run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was **not mounted** in this sandbox, so `errors/`, `slow/` and `recent/` could not be read. Findings rely on `analysis_context.json` aggregates, the `log_summary` rows, and repo source. Log archives for runs 39206105887 and 39206105194 returned HTTP 404. `insufficient_data` is `false`, but root causes of the CI test failures cannot be confirmed from logs.

- **Waiting for a free GitHub runner is the main source of delay.** Nine `log_summary` rows show waits of 57 s to 4 m 52 s, for example: 39212931139 (4 m 52 s of 298 s), 39212929970 (~4 m 11 s of 599 s), 39212930043 (3 m 49 s), 39212930955 (200 s of 274 s), 39214344118 (102 s of 114 s). CI jobs are capped at `timeout-minutes: 20` (`ci.yml` lines 506–1927), yet CI runs take p50 1295 s and up to 3097 s (run 39203228199). By inference that means **≥ 25 min of queueing** in the worst runs. *Impact: high (5–30 min per PR cycle). Confidence: high for the waits, medium for the CI inference.*
- **CI is the only family with a meaningful failure rate: 5/49 (10.2 %).** Three of the five failures are in `tests-promote-stall-and-review` (runs 39203228199, 39205956187, 39210403855). Each failing CI run was created at the same second as a review/autofix run that succeeded (for example 39203228199 and 39203228442). By inference the pipeline is catching and fixing these. *Impact: medium. Confidence: medium (no error logs).*
- **Possible broken workflow file around 05:44.** `clarify.yml` (39206105887) and `internal-clarify.yml` (39206105194) failed with 0 s duration and no logs. Their run names are the file paths, which matches the "workflow file issue" pattern in §24. Three seconds later, CI 39206108566 failed at `static-checks / Actionlint — reusable workflows and consumer templates`. By inference one commit broke both. *Impact: high when it recurs, because clarify cannot start. Confidence: medium.*
- **Review/autofix is the main cost and works.** It used **138.09 M OpenRouter tokens over 131 calls**: 21.87 M uncached prompt, 109.71 M cache read, 5.39 M cache write and 1.12 M completion. The derived aggregate cache-hit rate is ≈ **0.80**. Two runs are clear outliers: 39203031858 (0.374) and 39203941972 (0.535). *Impact: 10–20 % of uncached prompt tokens recoverable. Confidence: medium.*
- **Two single-use workflows look ready to delete.** `pr6576-shard-followup.yml` and `check-failure-triage-historical.yml` still run on every push to `main`. Each run only logs a skip (`PR6576_FOLLOWUP outcome=skip reason=marker_present issue=7072`; `HISTORICAL_TRIAGE skip reason=already_filed issue=7300`). Both took runner slots during the 06:57 contention burst. Their registry removal checks in `docs/scripts-pending-removal.md` appear to be met. *Impact: fewer contended runner slots per push to `main`. Confidence: high.*
- **Telemetry gaps hide signals.** `cache_hit_rate` becomes `null` whenever one call lacks usage data (39209049101 has 2 such calls, 39202040913 has 1). No `AI_MEMORY_TELEMETRY` `retrieve` events are visible. `GH_PAT_BUDGET ... used_in_job=unknown` appears in 39212930043. *Confidence: high.*

## Speed Optimizations

Ranked by expected latency reduction.

1. **Cut contention on runners: fewer jobs per merge (critical path).**
   - **Evidence:** the merges of PR #7661 and PR #7668 (06:56–06:57) started about 15 workflows within a minute: cancel-on-close, issue-PR status, review_autofix post-merge, live-copy sync, historical triage, PR6576 follow-up, and four or more clarify/plan/implement/clarify-respond quartets. Queue waits then ran 2–5 min (39212931139, 39212929970, 39212930043, 39212930010, 39212930955).
   - **Root cause:** the hosted-runner job concurrency limit (logs say "job-concurrency-limits") is saturated by fan-out, and some of those jobs do nothing.
   - **Exact change:**
     - Delete the two single-use workflows (Cost item 3 / Reliability item 4) after running their registry checks.
     - For gated families (clarify, plan, implement, clarify-respond: 534 of 554 runs ended `skipped`), make sure the skip decision is a job-level `if:` on event fields, so no runner is allocated. Some skipped runs still lasted 7–11 s (39212903947 11 s, 39213355329 11 s, 39212915731 9 s). By inference a gate job ran on a runner first.
   - **Estimated savings:** 2–5 runner slots per merge to `main`, and 1–3 min less queueing for the work that matters.
   - **Risk:** low (deletion follows the registry; gate rewrites need a contract test).
2. **Skip the full-history checkout where it is not needed (micro, but on every PR close).**
   - **Evidence:** `internal-cancel-on-pr-close.yml:50` and `sync-claude-live-copies.yml:35` use `fetch-depth: 0`. The log summaries show about 545–550 K characters of `[new tag]` output (39212825827, 39212930955).
   - **Note:** `scripts/workflow_failure_heal_pr_reconcile.sh` fetches its own refs (`git fetch --quiet --no-tags origin`, lines 159 and 165).
   - **Exact change:** confirm the reconcile script never needs history beyond what it fetches itself. If so, set `fetch-depth: 1` on the `heal-pr-reconcile` checkout. Leave the live-copy sync alone until its diff logic is checked.
   - **Estimated savings:** ~5–20 s per run and much smaller logs.
   - **Risk:** medium; a missing ancestor would make `merge-base` fail. Ship with the existing `continue-on-error: true`.
3. **Avoid a second Semble bootstrap per review run (micro).**
   - **Evidence:** 13 bootstraps took 529,729 ms in total (~40.7 s each). Four runs bootstrapped twice: 39204204584 84.8 s, 39204004477 89.1 s, 39209049101 87.8 s, 39206108675 76.5 s.
   - **Exact change:** if the two queries are in the same job, reuse the index built by the first. If they are in separate jobs, cache it with `actions/cache`, keyed on the head SHA.
   - **Estimated savings:** ~40 s per two-query run, about 1.3 % of a 55-min run.
   - **Risk:** low (fails open to a fresh bootstrap).

## Cost Optimizations

1. **Raise the cache-hit rate on the low-hit review runs.**
   - **Evidence:**
     - Run 39203031858: hit rate 0.374, 768,277 cache read against 440,856 cache write and 842,491 uncached prompt, over 6 calls.
     - Run 39203941972: 0.535 (2.12 M read, 397 K write, 1.45 M prompt).
     - Best case 39204004477: 0.916 with **0 cache write** over 14 calls.
     - Hit rate is computed as read / (read + prompt + write); the per-run values check out against that formula, for example 39203228442 gives 0.8089.
   - **Root cause (inference):** fewer calls per run means the prefix cache is written but rarely reused, and dynamic content placed early in the prompt fragments the prefix.
   - **Exact change:** keep static content (system rules, rubric, Semble block) before per-PR dynamic content such as the diff and the run ID. Emit a prefix-hash field on each call (see Prompt Cache).
   - **Estimated savings:** moving the two outliers to the 0.78 median saves about 0.9 M uncached prompt tokens. Across the family, each +5 pp of hit rate moves about 6.8 M tokens from uncached to cache-read.
   - **Quality risk:** none.
2. **Watch context pressure on the heaviest run.**
   - **Evidence:** 39209049101 used 26 calls and 32.8 M tokens (24 % of the family's total). It had 2 `CONTEXT_BUDGET_WARN` and 2 calls without usage data. 39202251305 (12 calls, 12.06 M tokens) and 39202040913 had one warning each.
   - **Exact change:** log the prompt-component breakdown when a context warning fires (Reliability item 3) so the growing component can be identified before capping it.
   - **Estimated savings:** not quantifiable without the breakdown.
   - **Quality risk:** low, since this only adds logging.
3. **Delete the no-op single-use workflows.** Runner minutes only, no tokens: about 2 × 300 s of wall time per push to `main` (39212931139 298 s, 39212930932 281 s), mostly queueing.
4. **Semble is doing its job.**
   - **Evidence:** 13 queries returned 195,421 bytes (~15 KB and 7.3 sources per query), with `static_dup_bytes_total=0` and `bootstraps_unused=0`.
   - **Assessment:** that is about 49 K tokens, or about 0.2 % of uncached prompt tokens. It adds little context and nothing is duplicated, so there is no evidence of noise.
   - **Gap:** it is unknown whether Semble reduced downstream tool calls; no A/B data exists.
5. **Serena** is disabled (`SERENA_ENABLED: false` in 39212930043 and 39212825840) and has zero telemetry, so there is nothing to assess.

## Reliability Improvements

1. **Prevent the "workflow file issue" startup failure.**
   - **Evidence:** 39206105887 (`.github/workflows/clarify.yml`) and 39206105194 (`.github/workflows/internal-clarify.yml`) both have `duration_seconds: 0`, no jobs and a log 404. CI 39206108566 then failed at Actionlint.
   - **Root cause category:** an invalid workflow file reached a ref that GitHub evaluates (inference: the same commit).
   - **Exact fix:**
     - Make sure the Actionlint step gates merges, which it already does via the `lint` aggregate.
     - Add a collector rule: a run with `conclusion=failure`, no jobs, and a name equal to its file path gets classified as `startup_failure:workflow_file`. It should not be reported as missing logs.
   - **Impact:** turns silent clarify outages into a named, countable signal.
   - **Rollback:** collector-only change.
2. **Clustered failures in the `tests-promote-stall-and-review` CI job.**
   - **Evidence:** 3 of 5 CI failures, at three different steps:
     - `Review-blocked judge label propagation contract test` (`ci.yml:1413`, `tests/test_review_rb_judge_label_propagation.py`), run 39203228199
     - `Promote cycle and blocked-verdict unit tests` (`ci.yml:1188`), run 39205956187
     - `Clarify loop guard unit tests` (`ci.yml:1446`, `tests/test_phase_wrapper_predicate_contract.py`), run 39210403855
     - The other two: `orchestrate-poll (0)` / `Orchestrate poll process unit tests` (39209049038) and Actionlint (39206108566).
   - **Root cause category:** unknown without logs. These are probably real regressions in implementation PRs that the matching review/autofix runs repaired, since all 55 review/autofix runs succeeded.
   - **Exact fix (diagnostic logging):** have each pytest step write `--junitxml` and print one line per failure, `CI_TEST_FAILURE job=<job> step=<step> test=<nodeid>`, through a small summary step. The collector can then group failures by test without the full logs.
   - **Impact:** it makes it possible to tell flaky tests from real regressions.
   - **Fail-open:** the summary step uses `if: failure()` and never changes the job result.
3. **Context budget warnings are prompt-size risk, not policy pressure.**
   - **Evidence:** `CONTEXT_BUDGET_WARN` = 4, all in review/autofix: 39209049101 ×2, 39202251305 ×1, 39202040913 ×1. `BREAK_GLASS` = 0.
   - **Exact fix:** add `component_bytes=` fields (diff, rubric, Semble, memory, prior findings) to the warning line. This is additive and backward compatible.
   - **Impact:** the source of prompt growth becomes visible before warnings turn into truncation.
4. **Single-use workflows whose job looks done** (`docs/scripts-pending-removal.md` lines 223–232 and 331–340).
   - **Exact fix:** run the listed preflight checks. The log summaries already show the expected skip lines in 39212827911, 39212931139, 39212827769 and 39212930932. Then remove the workflows, their scripts, their tests and their registry entries in one PR.
   - **Impact:** removes two workflows that run on every push to `main` and could break if CI or test layout changes.
5. **Semble fallbacks are healthy.** All 32 fallbacks are `context=contract-test` (classified by `_is_contract_test_semble_line` at `scripts/cost_audit.py:334`). There were 0 runtime fallbacks and 0 failed bootstraps. They come from CI test fixtures in runs 39203228199, 39209049038, 39210403855, 39206108566 and 39212909492, not from a broken rollout. The headline `semble_fallbacks=32` could mislead a reader; the report header should show runtime fallbacks only.

## AI Memory Health

- **Telemetry found:** two `AI_MEMORY_TELEMETRY` `finalize-task` events, both `ok: true`, `did_push: true`, `final_state: merged`:
  - issue 7650, run 39212929970
  - issue 7662, run 39212825776, `push_attempts: 1`, so there was no push contention
  - The finalize step in 39212825776 took ~104 s of 432 s. That is the largest timed step and is worth a breakdown.
- **Not found:** `retrieve`, `record-candidate`, `record-run-event`, `promote`, `compact` or `processed-command-*` events. Retrieve hit rate, `estimated_tokens` against budget, and `keyword_method` distribution **cannot be computed**. No `fail_open: true` or `enabled: false` entries were visible.
- **Recommendation:**
  - Check that `retrieve` events are emitted in the plan, implement and review runs. Those deep-dive logs were not available; for example, slow implement run 39202879183 (2689 s) has no telemetry row.
  - Have the collector count `AI_MEMORY_TELEMETRY` ops into `cost_telemetry`, for example `memory_retrieve_calls` and `memory_retrieve_hits`. Today the ops appear only in free-text summaries.

## GH API Call Audit

| Workflow / job | Signal | Assessment |
|---|---|---|
| review_autofix_sweep / sweep (39213484216) | `GH_PAT_BUDGET ... remaining=4682 used_in_job=2`; 5 candidates, all `skipped_active` | Efficient |
| review_autofix / post-merge-force-poll (39212825840) | `remaining=4674 used_in_job=2` | Efficient |
| review_autofix / post-merge-force-poll (39212930043) | `remaining=4968 used_in_job=unknown` | **Telemetry gap**: the starting snapshot was lost |
| post-merge-validate-dispatch | `used_in_job=0` (×2) | Efficient |
| lint_pr_body_auto_close | 1 paginated `pulls/<n>/commits?per_page=100` call per run (3–9 commits) | Efficient |
| check_failure_triage_historical | a few sequential `gh api` reads (PR, run, job log, issue), then a skip | Wasted calls every push until the workflow is removed |

- No HTTP 429, secondary rate-limit or retry events appear in any `log_summary`. PAT headroom stayed at or above 4650 of about 5000.
- **Actions:**
  1. Fix the `used_in_job=unknown` path: when the start snapshot is missing, emit `used_in_job=na reason=<...>`.
  2. Removing the historical-triage workflow saves about 4–5 calls per push to `main`.
- No unbatched per-item loops were seen; this is consistent with the §14 batching helpers.

## Prompt Cache & Memory System

- **Per-run `cache_hit_rate`:** 0.809, 0.747, 0.374, 0.776, 0.916, 0.535 and 0.774. It is `null` for 39209049101 and 39202040913, which each had calls with no usage data. The summary and family `cache_hit_rate` are always `null`, even though a weighted hit rate (≈ 0.80) can be computed from the token totals.
- **Fragmentation signals:**
  - **Cache writes in every run except one.** 39204004477 had zero cache writes and the best hit rate (0.916), so a stable prefix is achievable.
  - **High cache-write share in short runs.** In 39203031858, writes were 52 % of reads.
- **Fail-open:** 3 of 131 calls had no usage data. Nothing failed, but those calls blind the metric.
- **Recommended diagnostic additions:**
  1. In `scripts/cost_audit.py`, compute `cache_hit_rate` over calls with usage data only. Add a separate `cache_hit_rate_coverage` field (calls with usage / all calls) rather than nulling the rate, and aggregate a weighted rate at family and summary level. This is additive, so existing field names are unchanged.
  2. Emit a `prompt_prefix_sha` (hash of the static prefix) on each model call. If the hash changes within a run, the prefix is unstable.
- **Expected impact:** better visibility right away. Token savings come from item 1 of Cost Optimizations once the causes are visible.

## Orchestrator Health

- **Project #7640 completed end to end.**
  - Validation ran in 39204347864 (1078 s).
  - Integration readiness in 39212911305 reported "[ready] all 5 sub-issue(s) on #7640 are ticked".
  - The squash PR #7647 used `Refs #7640`, and the auto-close lint passed (39212911330), consistent with §21.
- **Orchestrate poller:** 27 runs, 20 succeeded, **7 cancelled** (these are all of the repository's cancellations). The p95 is 1374 s; the slowest runs were 39202424215 (1576 s), 39207237045 (1412 s) and 39211703339 (1285 s). By inference the cancellations are concurrency-group supersession, but no logs were available to confirm. Have the poller log `POLL_SUPERSEDED by_run=<id>` before it is cancelled, so cancellations can be told apart from failures.
- **Clarify loop:** clarify-respond had 136 runs, 134 of them ended `skipped`, with no failures. There is no evidence of clarification ping-pong in this window.
- **Indicators to track:** poller cancellations per day, the gap between readiness `[ready]` and merge, and the count of `No standalone validation workflow could be dispatched` warnings (39212930043 and 39212825840, which are expected for issues without `ai:orchestrator-validate-required`).

## Pipeline Flow Bottlenecks

| Stage | Compute | Queueing | Retry / overhead |
|---|---|---|---|
| clarify / plan / implement | slow runs: implement 39202879183 2689 s, plan 39201334080 1236 s | contended | ~95 % skipped fan-out, some on runners |
| review/autofix | p50 1456 s, p95 3439 s, 6–26 model calls per run | 2–4 min post-merge (39212930043, 39212825840) | ~40 s Semble bootstrap, ×2 in 4 runs |
| CI | ≤ 20 min per job by timeout | **inferred ≥ 25 min worst case** (3097 s run against a 25-min ceiling) | 10.2 % failure, then an autofix cycle |
| validate / merge | 1078 s (39204347864) | post-merge burst of about 15 workflows | single-use no-ops |

Fixes in order of end-to-end impact:

1. Cut the merge-time fan-out: delete the single-use workflows and make gates skip without a runner.
2. Measure CI queue time per job. Have the collector record `queue_seconds = started_at - created_at` from the jobs data it already fetches to set `failure_point`.
3. Reduce CI failures by grouping them with `CI_TEST_FAILURE` lines.
4. Run fewer model calls in each review/autofix run.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - Runner queueing (up to 4 m 52 s per job; CI wall clock about twice its execution ceiling)
  - review/autofix duration (p95 3439 s)
- **Top failure modes:**
  - CI test failures concentrated in `tests-promote-stall-and-review` (3 of 5)
  - Workflow-file startup failures of `clarify.yml` and `internal-clarify.yml` (2)
- **Highest cost driver:** review/autofix, 138.09 M tokens over 131 calls, with 32.8 M in a single run (39209049101).
- **Top 3 actions:**
  1. Run the removal checks and delete `pr6576-shard-followup.yml` and `check-failure-triage-historical.yml`, together with their scripts, tests and registry entries.
  2. Add `CI_TEST_FAILURE` summary lines and a collector `queue_seconds` field, so the next report can separate queueing from test cost.
  3. Fix `cache_hit_rate` nulling and add family-level aggregation and a `prompt_prefix_sha` in `scripts/cost_audit.py` and the model-call wrappers.

## Metrics Appendix

**Runs and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other (skipped) | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Repo total | 1000 | 290 | 7 | 7 | 696 | 0.7 % | 1 | 1548 |
| ci | 49 | 44 | 5 | 0 | 0 | 10.2 % | 1295 | 2527 |
| review_autofix | 55 | 55 | 0 | 0 | 0 | 0 % | 1456 | 3439 |
| orchestrate_poll | 27 | 20 | 0 | 7 | 0 | 0 % | 398 | 1374 |
| clarify | 146 | 8 | 2 | 0 | 136 | 1.4 % | 1 | 368 |
| implement | 135 | 8 | 0 | 0 | 127 | 0 % | 1 | 1331 |
| plan | 136 | 8 | 0 | 0 | 128 | 0 % | 1 | 774 |
| integration_pr_readiness | 49 | 49 | 0 | 0 | 0 | 0 % | 70 | 482 |
| lint_pr_body_auto_close | 42 | 42 | 0 | 0 | 0 | 0 % | 75 | 394 |
| workflow_failure_heal | 72 | 0 | 0 | 0 | 72 | 0 % | 1 | 10 |

**Tokens and cache (OpenRouter; all in review/autofix)**

| Metric | Value |
|---|---|
| or_calls (usage available / unavailable) | 131 (128 / 3) |
| prompt / completion | 21,872,629 / 1,119,705 |
| cache read / cache write | 109,707,281 / 5,391,453 |
| total | 138,091,068 |
| derived weighted cache_hit_rate | ≈ 0.80 (the reported value is `null`) |
| per-run cache_hit_rate range | 0.374 – 0.916 (2 runs `null`) |
| codex_calls / codex_tokens | 0 / 0 |
| wall_clock_p50_ms / p99_ms (repo) | 2,000 / 3,503,320 (n = 113) |
| wall_clock_p50_ms / p99_ms (review_autofix) | 3,288,000 / 3,816,270 (n = 12) |
| break_glass_count | 0 |
| context_budget_warn_count | 4 (39209049101 ×2, 39202251305 ×1, 39202040913 ×1) |

**Semble and Serena**

| Metric | Value |
|---|---|
| semble_query_calls / bytes | 13 / 195,421 (~15.0 KB per query) |
| semble_sources_total / static_dup_bytes | 95 / 0 |
| semble_bootstraps (failed / unused) | 13 (0 / 0), 529,729 ms total, ~40.7 s average |
| semble_fallbacks (contract-test / runtime) | 32 (32 / 0) |
| serena query / fallback / probe | all 0 (`SERENA_ENABLED: false`) |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**

| Run | Job | remaining | used_in_job |
|---|---|---|---|
| 39213484216 | sweep | 4682 | 2 |
| 39212825840 | post-merge-force-poll | 4674 | 2 |
| 39212825840 | post-merge-validate-dispatch | 4676 | 0 |
| 39212930043 | post-merge-force-poll | 4968 | unknown |
| 39212930043 | post-merge-validate-dispatch | 4650 | 0 |

No 429 or rate-limit events in any summarised run.

**Data gaps**
- The run-logs directory was not mounted, so the deep-dive logs could not be read.
- Log archives for 39206105887 and 39206105194 returned 404.
- There are no queue-time fields per job.
- `claude_*` cost fields are absent from the analysis context, although `CLAUDE_COUNT_KEYS` exists in `cost_audit.py`.
- No `retrieve` memory telemetry is visible.

---

**Actions taken:**
- Analysed the supplied telemetry.
- Read the relevant parts of `.github/workflows/ci.yml`, `scripts/cost_audit.py`, `docs/scripts-pending-removal.md`, `internal-cancel-on-pr-close.yml`, `pr6576-shard-followup.yml`, `check-failure-triage-historical.yml` and `scripts/workflow_failure_heal_pr_reconcile.sh`.
- Made no file changes.

**Assumptions:**
- CI queue time is inferred from job timeouts.
- The two clarify startup failures and the Actionlint failure are assumed to share one commit.
- Skipped runs lasting 7–11 s are assumed to have run a gate job on a runner.

**Missing context:** the deep-dive logs, so the CI failure messages and the poller cancellation reasons are unconfirmed.