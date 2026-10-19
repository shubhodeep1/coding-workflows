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
## Deep Audit — Workflows & Scripts (2026-10-19)

> **Scope and method.** This audit goes deep on fewer files rather than wide. Read in full or in the relevant ranges:
> - **Workflows:** `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `internal-clarify.yml`, `lint-pr-body-auto-close.yml`, `sync-claude-live-copies.yml`; and parts of `implement.yml`, `test-and-mark-stable.yml`, `clarify.yml` and `orchestrate.yml`.
> - **Scripts:** `scripts/gh_helpers.sh` (the retry helpers), `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh` (`_resilient_phase_swap`), `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal_report.sh`, `scripts/check_failure_triage.sh` and `scripts/comprehensive_test_and_release_gh_api.sh`.
> - **Guard test:** `tests/test_workflow_file_size_limit.py`.
>
> Byte and character counts could not be measured: the toolset had no `wc` and no shell. Sizes are **inferred** from line counts and labelled as such. No TODO/FIXME/HACK markers exist in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.
>
> **Cross-references to the existing report sections (not raised again here):**
> - The two single-use workflows `pr6576-shard-followup.yml` and `check-failure-triage-historical.yml` (Executive Summary; Reliability item 4).
> - The historical-triage API calls (GH API Call Audit table).
> - The `fetch-depth: 0` checkout on `heal-pr-reconcile` (Speed item 2). See the caution at the end of Section 1 before acting on it.

### Section 1: Bug & Correctness Sweep

**BUG-001 — Retried label PUT re-reads stdin that the first attempt already used up**
- **File / lines:** `scripts/label_helpers.sh:251-253`; `scripts/review_rb_judge.sh:1014-1016`
- **Severity:** High
- **Category:** `bug`
- **Description:**
  - Both phase-label swaps pipe the new label set into a retried call: `printf '{"labels":%s}' "${_new}" | gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels" --input -` (the same shape is used with `${_rps_new}` in `review_rb_judge.sh`).
  - `gh_retry` treats an explicit `-X PUT` as idempotent: `_gh_api_args_unsafe_post` returns 1 for any non-POST method (`scripts/gh_helpers.sh:721-724`). So `max_attempts` stays at `GH_RETRY_MAX_ATTEMPTS:-5` (`gh_helpers.sh:1066-1069`).
  - Each attempt re-runs `"$@"` with the same inherited stdin (`gh_helpers.sh:1083`). Attempt 1 reads the whole pipe, so attempts 2–5 send an empty request body.
  - Best case, every retry is wasted and the function falls back to POST-add without removing the old phase label. The issue then carries two phase labels, which breaks the one-phase-label rule the `_AI_PHASE_LABELS` subtraction exists to keep (`label_helpers.sh:151`, `238-239`).
  - Worst case, an empty-body PUT to `/issues/{n}/labels` replaces the label set with nothing. The function would then record `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="applied"` (`label_helpers.sh:254`) after wiping every label. [NEEDS VERIFICATION: how GitHub treats an empty-body PUT on this endpoint.]
- **Recommended fix:**
  - Make each attempt replayable. Pass the labels as fields, which `gh api` re-encodes on every attempt and which keeps `gh_retry` semantics unchanged:

    ```bash
    _put_args=()
    while IFS= read -r _l
    do
    	_put_args+=(-f "labels[]=${_l}")
    done < <(printf '%s' "${_new}" | jq -r '.[]')
    gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels" "${_put_args[@]}" >/dev/null 2>&1
    ```

  - Alternatively, write the JSON to a `mktemp` file and use `--input "${payload_file}"`.
  - Apply the same change to `_resilient_phase_swap`.
  - Add a regression test that stubs `gh` to fail once, then asserts the second attempt receives a non-empty body.

**BUG-002 — Phase-label swap reads, modifies and rewrites the whole label set, so concurrent label writes are lost**
- **File / lines:** `scripts/label_helpers.sh:223-253`; `scripts/review_rb_judge.sh:1012-1016`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - `set_issue_phase_label_resilient` reads all labels (`223-224`), computes `(. - $p) + [$t]` (`238-239`), then PUTs the full set (`251-253`).
  - A label added by another workflow between the GET and the PUT is silently removed. Examples are `ai:merge-queued`, `ai:destructive-blocked` or `ai:scope-blocked`, which `implement.yml:2263-2280` uses as circuit breakers.
  - `_resilient_phase_swap` has the same full-set PUT at `1012-1016`. The GET that feeds `_rps_cur` is earlier in the function. [NEEDS VERIFICATION: exact line.]
  - The repo already has an additive pattern that avoids this race, in the other branch of the same function (`review_rb_judge.sh:991-996`): DELETE each stale phase label, then POST-add the target.
- **Recommended fix:** Replace the PUT path in both helpers with the additive pattern from `review_rb_judge.sh:991-996`:
  1. POST-add the target with `GH_RETRY_IDEMPOTENT=true` (as at `label_helpers.sh:226`).
  2. DELETE each label that is in both `_AI_PHASE_LABELS` and `_cur` and is not the target.
  3. Keep the PUT only as a fallback, if at all.

  Non-phase labels are then never rewritten. Keep `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME` semantics unchanged (§10).

**BUG-003 — A failed no-op comment aborts the step before `ai:implementation-failed` is applied**
- **File / lines:** `.github/workflows/implement.yml:5534-5551`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The step runs under `set -euo pipefail` (`5325`). The fall-through path posts its comment with no failure guard: `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (`5534-5535`).
  - That call is an unsafe POST, so `gh_retry` makes a single attempt (`gh_helpers.sh:1067-1069`). One transient 5xx or secondary rate limit exits the step at once.
  - The label update at `5540-5551`, the signal the orchestrator uses to re-issue the task, then never runs, and the issue stays in its prior phase.
  - The two earlier guards in the same step already guard their comment posts: `5371-5372` and `5414-5415` both end in `>/dev/null 2>&1 || true`.
- **Recommended fix:** Append `|| echo "::warning::no-op comment post failed for #${ISSUE_NUMBER}; still applying ai:implementation-failed"` to `5534-5535`, matching the guarded posts at `5371-5372`. Alternatively, move the label update (`5540-5551`) before the comment.

**BUG-004 — `gh_api_safe` writes its `::error::` line to stdout, so callers capture it as data**
- **File / lines:** `scripts/comprehensive_test_and_release_gh_api.sh:39-44`; consumed at `.github/workflows/test-and-mark-stable.yml:1052-1053` and `1060`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - On a failure other than a rate limit, `gh_api_safe` runs `echo "::error::gh api call failed: $*"` without `>&2` (line 41).
  - Every caller captures through `$(gh_api_safe_print …)`, for example `LABELS=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq … || echo "")`. So:
    - the annotation never reaches the log, which hurts observability;
    - `LABELS` holds the error text, so `CUR_STATE` (`1081`) changes;
    - the waiter counts that change as activity and resets `LAST_ACTIVITY` (`1084-1091`).
  - When reads alternate between failing and succeeding, every flip resets the timer, so a phase can stall past `PLAN_PHASE_TIMEOUT` without tripping the inactivity exit (inference).
  - The step's own comment knows about the stdout leak (`test-and-mark-stable.yml:975-976`) and works around it with shape checks only in the run-ID capture.
- **Recommended fix:** Change line 41 to `echo "::error::gh api call failed: $*" >&2`. Line 42 already sends `cat "${err_file}"` to stderr. Callers that shape-check the output (`find_latest_scoped_run_field`) still work, because they treat empty or invalid stdout as a failure.

**Checked without findings:**
- `lint-pr-body-auto-close.yml:58-69` passes `PR_TITLE` and `PR_BODY` through `env:`, so there is no injection.
- `implement.yml:972` passes `ISSUE_BODY` through `env:`.
- `workflow_failure_heal_pr_reconcile.sh:43` uses `set -uo pipefail` without `-e` on purpose ("Never fails the job", lines 40-41). Its branch names are validated by `_valid_branch` (`66-74`), and its push is fast-forward only (`204-205`).
- `cancel_on_pr_close.yml:144-167` uses `gh_retry` inside a pipeline. That is safe, because `gh_retry` buffers each attempt and replays only the attempt that succeeded (`gh_helpers.sh:1043-1055`).

**Caution for report Speed item 2 (no ID; the current code is fine):** `workflow_failure_heal_pr_reconcile.sh` runs `git merge` (`187-189`) and `git diff` (`198`) against refs it fetches without `--depth` (`159-168`). When a merge fails, `RETARGET_FAIL_REASON="merge_conflict"` (`192`) leads to `_close_heal` (`272-273`), which **closes the heal PR and its issue**. If moving the checkout to `fetch-depth: 1` leaves a missing merge-base, heal PRs would be closed rather than skipped. Before changing the checkout, either map a merge-base failure to a non-closing reason (as `push_state_unknown` is at `265-270`), or confirm that `git merge-base` resolves on a shallow checkout. [NEEDS VERIFICATION]

### Section 2: GitHub API Call Redundancy Audit

**API-001 — `gh_api_json_to_file` retries permanent 404/401/422 failures five times**
- **File / lines:** `scripts/gh_helpers.sh:1271-1335`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:**
  - `gh_retry` (`1099-1106`) and `gh_retry_to_file` (`1184-1192`) both stop at once on `_is_gh_permanent_failure` (`192-195`: 404, 400, 401, 422, scope errors). `gh_api_json_to_file` has no such check: any failure that is not a rate limit goes to exponential backoff (`1313-1321`).
  - A 404 therefore costs 5 calls and 1+2+4+8 = 15 s of sleep.
  - Affected callers where "not found" is an expected rejection path:
    - `scripts/workflow_failure_heal_intake.sh:479` (`issue_not_found`), `:498` (`pull_request_not_found`) and `:505` (compare)
    - `scripts/check_failure_triage.sh:601` (parent issue)
    - `scripts/workflow_failure_heal_report.sh:110` and `:235`
- **Call count:** today 5 calls per permanent failure; after the fix, 1.
- **Recommended fix:** Insert the `_is_gh_permanent_failure` short-circuit from `gh_retry_to_file:1184-1192` at the top of the failure branch (`1302-1304`): emit the warning, truncate `${outfile}`, return 1. Rate-limit handling and JSON validation stay as they are. Extend the existing `gh_helpers` tests with a 404 stub that asserts a single attempt.

**API-002 — Heal reconcile makes one REST pull lookup per matching heal issue**
- **File / lines:** `scripts/workflow_failure_heal_pr_reconcile.sh:239-247`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** Inside `for heal_issue in "${HEAL_ISSUES[@]}"`, each iteration calls `gh_retry gh api --method GET "repos/${REPO}/pulls" -f state=open -f head="${REPO%%/*}:${heal_branch}"`. The header budget (`29-31`) documents this as "one GET of the open PRs per matching heal issue". N is usually 1, but it is unbounded.
- **Call count:** today N+1 (1 issue list plus N pull lookups); after the fix, 2.
- **Recommended fix:** Prefetch once and filter locally. The simplest option is one paginated `GET repos/${REPO}/pulls?state=open&per_page=100`, joined in jq on `.head.ref == "ai/issue-<n>"`. A GraphQL alias batch also works, following `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:170-235`; batches of 50, fail open per batch). Update the budget comment at `29-31`.

**API-003 — The no-op ancestor walk makes two REST reads per hop**
- **File / lines:** `.github/workflows/implement.yml:5458-5480`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** Each hop reads the issue body (`5468`) and then pages through all comments (`5472-5473`), only to count those matching `produced no repository changes`. The cost is documented as "up to 2 * threshold calls" (`5458`); with the default `IMPL_NOOP_ANCESTRY_THRESHOLD=2` that is 4 calls.
- **Call count:** today 2 per hop (more if comments paginate); after the fix, 1 per hop.
- **Recommended fix:** Fetch both in one GraphQL query per hop: `repository{issue(number:$n){body comments(last:100){nodes{body}}}}`, through `gh_retry gh api graphql`. This fails open the same way (break on error). [NEEDS VERIFICATION: `last:100` must cover where the marker comment usually sits.]

**Already satisfied:**
- `implement.yml` issue metadata: `201-202` writes `ISSUE_META_FILE`, and `926-931` and `1989-1991` reuse it through `issue_meta_matches_issue`. The fresh reads at `2204` and `2620` are deliberate security-dependency re-checks against a time-of-check/time-of-use gap (comment at `2202-2203`), so they stay.
- `cancel_on_pr_close.yml:322-338` sends one POST cancel per run. GitHub has no batch-cancel endpoint, so this is the minimum.
- The PR-state lookups at `175-235` are already batched GraphQL.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The `gh_api_json_to_file` and `_safe_gh_jq` fallback stubs are copied into three scripts**
- **File / lines:** `scripts/workflow_failure_heal_report.sh:56-61`; `scripts/workflow_failure_heal_intake.sh:170-175`; `scripts/check_failure_triage.sh:207-226`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Three copies of the same `type gh_api_json_to_file >/dev/null 2>&1 || gh_api_json_to_file() { … "$@" > "${outfile}"; }` stub, plus a full copy of `_safe_gh_jq` in `check_failure_triage.sh:213-226`. The stubs silently drop retries and JSON validation, and nothing is logged when they activate. So a run whose staged `gh_helpers.sh` failed to load looks normal.
- **Recommended fix:**
  - Keep one fail-open shim as `gh_helpers_require_or_stub` (new name; checked against the function list at `gh_helpers.sh:31-2888`). Put it in a tiny new file, `scripts/gh_helpers_fallback.sh`, sourced right after the `source scripts/gh_helpers.sh 2>/dev/null || true` line in all three scripts.
  - The shim emits `::warning::gh_helpers.sh unavailable; using unretried fallbacks` exactly once.
  - If the new file is missing too, callers fall back to the current inline stubs, so behaviour is unchanged.

**DUP-002 — Identical "close as `ai:closed`" blocks inside one step**
- **File / lines:** `.github/workflows/implement.yml:5359-5372` and `5493-5506`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Both blocks run the same sequence: label create `ai:closed` (same colour and description), the same 7-label `--remove-label` edit with the same fallback, then `gh issue close --comment` with a comment-POST fallback.
- **Recommended fix:** Move the step body into `scripts/implement_handle_noop.sh`, following the `review_autofix_step_<slug>.sh` pattern from §24. In it, define a function `_impl_close_as_ai_closed()` taking `<body>` that both guards call. Moving the body also fixes EXPR-001.

**DUP-003 — The "Resolve workflow support ref" bootstrap is copied 27 times across 13 workflows**
- **File / lines:** for example `.github/workflows/cancel_on_pr_close.yml:25-46`, `implement.yml:1280-1302` and `implement.yml:6841-6862`. Counts of `SUPPORT_REF_WORKFLOW_JOB_JSON: ${{ toJSON(job) }}`: implement 6, clarify 3, plan 3, orchestrate_clarify_respond 3, validate 2, orchestrate 2, orchestrate_poll 2, and one each in sync_ai_labels, cancel_on_pr_close, workflow_failure_heal, check_failure_triage, security-audit and review_autofix.
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The step has to run before any support checkout exists, so it cannot source a script from `scripts/`. The duplication is structural. `tests/test_workflow_support_ref_follows_pin.py` already covers the pin behaviour.
- **Recommended fix:**
  - Do not extract it.
  - Extend `tests/test_workflow_support_ref_follows_pin.py` to assert every copy is byte-identical except for the workflow-filename regex. A fix to one copy then cannot skip the other 26.
  - When copies are next edited, consider the local composite-action pattern under `.github/actions/setup-runtime/action.yml`. [NEEDS VERIFICATION: whether consumer wrappers can resolve a same-repo composite action at the pinned SHA.]

**DUP-004 — Inline Telegram `curl` duplicates `tg_send_msg`**
- **File / lines:** `api.telegram.org/bot` appears in `implement.yml` (`5429-5433`), `update_workflows.yml` (2×), `test-and-mark-stable.yml`, `validation-refresh.yml`, `issue_pr_status.yml` and `check_failure_triage.yml`: 7 occurrences in 6 files.
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Each copy builds its own `curl -s -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage"` with its own error handling. `implement.yml:5418-5419` explains that `tg_helpers.sh` "may have been cleaned up" in consumer runs.
- **Recommended fix:** Where the helper is guaranteed to be staged (`tg_helpers.sh` is in `REQUIRED_BOOTSTRAP_SCRIPTS`, `scripts/stage_workflow_support.sh:57`), source it from the staged support directory (`${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/tg_helpers.sh` in implement) instead of the workspace copy. Keep the inline curl only as a fallback when the staged copy is absent.

### Section 4: Expression Size Limit Risk Assessment

**Context:**
- `tests/test_workflow_file_size_limit.py:66-98` fails CI (`ci.yml:1295`) for any `jobs.*.steps[*].run` that contains `${{` and is ≥ 18,000 characters. So no run body on `main` can be at High risk (≥ 18,000) while CI passes.
- The findings below are Medium-risk candidates. Their character counts are **inferred** from line counts (block indentation stripped, about 55–65 characters per line) and are [NEEDS VERIFICATION].
- The quickest exact measurement is to run the guard test locally with a 15,000 threshold.

**EXPR-001 — "Handle no-op implementation"**
- **File / lines:** `.github/workflows/implement.yml:5324-5551`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - About 228 run lines; estimated ~13,000–15,000 characters, leaving about 6,000–8,000 below the 21,000 hard limit (inference).
  - The interpolations are all incidental, at `5329`, `5331`, `5534`, `5542`, `5544`, `5548` and `5550`: `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}`.
  - The body carries long inline prompt-style comment templates (`5351-5358`, `5391-5405`, `5485-5492`, `5514-5532`), which will grow.
- **Recommended fix:** Replace the context expressions with runner default variables: `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"` and `REPO="${GITHUB_REPOSITORY}"`. The body then contains no `${{ }}` and is no longer an expression, so headroom is unlimited. Better still, extract it to a script as in DUP-002.

**EXPR-002 — "Phase 2: Wait for plan to complete"**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:936-1203`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** About 268 run lines; estimated ~15,000–17,000 characters, leaving about 4,000–6,000 (inference). The only interpolations are three copies of `${{ steps.create-issue.outputs.created_after }}` (`954`, `966`, `984`). The file already uses the fix elsewhere ("Promoted to env so the run: script body contains no ${{ }}", `1825`, `2606`).
- **Recommended fix:**
  - Add `PLAN_CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to the step's `env:` and replace the three uses with `${PLAN_CREATED_AFTER}`. The new name was checked against the file's existing env names (`CREATED_AFTER`-style names exist only as step outputs).
  - Phase 1 has the same shape (`822`, about 140 lines) and should get the same fix.

**EXPR-003 — "Phase 3b: Wait for PR creation (implement phase)"**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1243-1525`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** About 282 run lines; estimated ~15,000–17,500 characters, leaving about 3,500–6,000 (inference). There is a single interpolation at `1281`: `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"`.
- **Recommended fix:** Move it to `env:` as `IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}` and delete line 1281. The body then has no expression. Behaviour is unchanged, because the shell variable keeps the same name.

**EXPR-004 — Workflow file size headroom under the §24 guard**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml` (7,585 lines), `implement.yml` (6,891 lines), `review_autofix.yml` (5,718 lines)
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The prompt's 1 MB / 800 KB thresholds are superseded by this repo's documented limit: 512,000 bytes, with the CI guard at 480,000 (`tests/test_workflow_file_size_limit.py:26-27`; §24). No workflow can be over 800 KB while CI passes.
  - At an inferred 55–70 bytes per line, `test-and-mark-stable.yml` comes to about 417–531 KB. That is near or above the 480,000 guard, so its real headroom is probably below the 50,000+ bytes §24 asks for. [NEEDS VERIFICATION: run `python3 tests/test_workflow_file_size_limit.py`, which prints the largest file's size.]
- **Recommended fix:**
  - If headroom is under 50,000 bytes, move the largest phase bodies of the `e2e-smoke-test` job into `scripts/`, keeping each step's `name:`, `id:`, `if:` and `env:` (§24). The biggest are Phase 4 (`1795-2526`, about 730 lines) and Phase 4b (`2596-3266`, about 670 lines); both already have no `${{ }}` in their bodies.
  - Do not split the workflow into several files, and do not raise the guard.

**EXPR-005 — The release notification step interpolates more than 20 `needs.*` values inline**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:7350-7585` (interpolations at `7373-7393`, `7422-7465` and `7578`)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** About 225 lines with about 25 inline `${{ needs.*.result }}`, `${{ needs.resolve-version.outputs.version }}` and `${{ github.run_id }}` expressions. Estimated ~10,000–12,000 characters (inference). The whole body counts as one expression and grows with every new gated job.
- **Recommended fix:**
  - Move every value into the step's `env:`. `RELEASE_ENABLED` already sits there, at `7361`.
  - Use new, unique names such as `NOTIFY_E2E_RESULT` and `NOTIFY_VERSION`, and keep the shell variable names (`E2E`, `SCRIPTS`, …) by assigning them from env.
  - Replace `${{ github.run_id }}` with `${GITHUB_RUN_ID}`.

**Large `if:` conditions:** `internal-clarify.yml:17-20`, `clarify.yml:27` and `workflow-templates/ai-clarify.yml:20` hold a multi-clause gate of about 1.7 K characters, well under the limit. No `actions/github-script` `script:` inputs exist in any workflow.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — The `RATE_LIMIT_BACKOFF` skip guards can never fire**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:789`/`861`, `943`/`1056`, `1257`/`1387`, `1877`/`2140`, `3556`/`3706`; `.github/workflows/comprehensive-test-and-release.yml:55`, `288`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:**
  - Each waiter sets `RATE_LIMIT_BACKOFF=0` and later checks `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]; then continue; fi`.
  - The variable is only changed inside `gh_api_safe` (`scripts/comprehensive_test_and_release_gh_api.sh:11-13`, `28-34`), and every caller runs it inside `$(…)`. The update dies with the subshell, so the parent value is always 0.
  - Two side effects follow (inference): the "skip this cycle" protection described at `1055` never applies, and backoff restarts at 30 s on every call instead of escalating.
  - If the guard ever did fire, its `continue` skips the `sleep "$POLL_INTERVAL"` at the end of the loop and would spin.
- **Recommended fix:** Either delete the dead guards, or make backoff state survive. For example, `gh_api_safe` could write the backoff to `${RUNNER_TEMP}/gh_api_safe_backoff`, and the guard would read that file and `sleep` before `continue`. Pair this with BUG-004.

**DEAD-002 — Stale comment and an effectively unreachable fallback in "Validate approval phase label"**
- **File / lines:** `.github/workflows/implement.yml:2241-2254`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** The comment says the step will "Reuse the issue snapshot fetched in 'Fetch issue metadata'". In fact `ISSUE_META_FILE` was just overwritten (`2210`) with the fresh `DEPENDENCY_META` read at `2204`. The empty-check at `2205-2209` already exits when that read fails, so the API fallback at `2252-2254` can only run if `jq` fails on a body that was just fetched.
- **Recommended fix:** Update the comment to say the labels come from the fresh security re-check snapshot. Keep the fallback; it is harmless and fails safe.

**CONSIST-001 — A raw `gh api` call sits inside a loop that otherwise uses `gh_api_safe_print`**
- **File / lines:** `.github/workflows/test-and-mark-stable.yml:1117-1118`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `LABELS_RECHECK=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq … 2>/dev/null || echo "")` skips the rate-limit backoff that the same loop's `gh_api_safe_print` calls get (`1052`, `1060`). A rate limit here reads as "no labels" and fails the phase with `plan_failed` (`1188-1190`).
- **Recommended fix:** Use `gh_api_safe_print`, as line `1052` does.

**CONSIST-002 — Inline label creation drifts from the label catalog**
- **File / lines:** `.github/workflows/implement.yml:5359-5360`, `5406-5407`, `6543-6544`, compared with `scripts/label_helpers.sh:48`, `50`, `62`, `112`, `114`, `126`
- **Severity:** Low
- **Category:** `consistency`
- **Description:**

  | Label | Inline in `implement.yml` | Catalog in `label_helpers.sh` |
  |---|---|---|
  | `ai:closed` | colour `6b7280`, "Issue closed by automation…" | colour `6a737d`, "Linked PR closed without merge" |
  | `ai:needs-human` | colour `b60205` | colour `e11d48` |
  | `ai:blocked` | "Blocked: human input required" | "Orchestrator auto-answer loop blocked pending human intervention" |

  Whichever path creates a label first decides its appearance. `implement.yml` (8 sites) and `review_autofix.yml` (4 sites) use `gh_retry gh label create` inline instead of `ensure_label_exists`.
- **Recommended fix:** Replace the inline `gh label create` calls with `ensure_label_exists "<label>" "${GITHUB_REPOSITORY}"` from the staged `label_helpers.sh`, as `5540-5542` already does. Keep the inline create only in the `else` branch where the helper is missing.

**SHELL-001 — `${{ github.* }}` interpolated directly into shell where runner variables exist**
- **File / lines:** `.github/workflows/cancel_on_pr_close.yml:32-33`; `implement.yml:445-446`, `729-730`, `1287-1288`, `6847-6848` (and the other DUP-003 copies)
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `if [ "${{ github.repository }}" = "${wf_source}" ]; then echo "SCRIPT_REF=${{ github.sha }}" …`. These values are not attacker-controlled, so this is not an injection. It does turn every such body into an expression (Section 4), and it is inconsistent with the env-first pattern the same job uses (`cancel_on_pr_close.yml:19-23`).
- **Recommended fix:** Use `"${GITHUB_REPOSITORY}"` and `"${GITHUB_SHA}"`. Update all copies together and keep them identical (DUP-003).

**SEC-001 — The clarify gate starts a runner for untrusted commenters who post the release-marker prefix**
- **File / lines:** `.github/workflows/clarify.yml:27`; `.github/workflows/internal-clarify.yml:20`; `workflow-templates/ai-clarify.yml:20`
- **Severity:** Low
- **Category:** `security`
- **Description:**
  - The third gate clause accepts comments from users who are **not** `OWNER`, `MEMBER` or `COLLABORATOR`, as long as the body starts with `/reclarify\n<!-- ai:pr-dependency-released:`.
  - Authorization is enforced later, inside the job: a live `GET /user` compared with `COMMENT_USER_LOGIN`, failing closed (`clarify.yml:697-735`). So this is not a privilege escalation.
  - But on public consumer repos, any outside user can start clarify jobs, and each one burns a runner slot and setup time before the check rejects it. This adds to the runner contention described in the report's Speed item 1 (inference).
- **Recommended fix:** Add an optional, defaulted gate term: `(vars.PR_DEPENDENCY_RELEASE_LOGIN == '' || github.event.comment.user.login == vars.PR_DEPENDENCY_RELEASE_LOGIN)`. This is a new variable that defaults to unset, which keeps current behaviour (§8). Apply it to all three files so the template stays in parity, and document it in an `agents.d/` fragment (§25).

**DEBT-001 — The expression guard skips composite actions and non-`run:` expressions**
- **File / lines:** `tests/test_workflow_file_size_limit.py:66-98`; `.github/actions/{claude-pool-token,install-claude,install-codex,install-opencode,run-soft-error-analyzer,setup-runtime}/action.yml`
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** The guard only walks `jobs.*.steps[*].run` in `.github/workflows/*.yml`. A composite action's `runs.steps[*].run` with `${{ inputs.* }}` is also evaluated as one expression, and so are large `if:` and `with:` values. None of these are checked.
- **Recommended fix:** Extend `test_interpolated_run_bodies_below_expression_guard` to also load `.github/actions/*/action.yml` (`runs.steps`) and to measure step-level `if:` strings with the same 18,000 threshold. This is additive; the existing constants are unchanged.

**Not re-raised:** the two single-use workflows that do nothing (see the report's Reliability item 4), and the historical-triage API calls (see the report's GH API Call Audit). No TODO, FIXME or HACK markers were found in scope.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 8 | BUG-002, BUG-003, BUG-004, API-001, EXPR-001, EXPR-002, EXPR-003, EXPR-004 |
| Low | 15 | API-002, API-003, DUP-001, DUP-002, DUP-003, DUP-004, EXPR-005, DEAD-001, DEAD-002, CONSIST-001, CONSIST-002, SHELL-001, SEC-001, DEBT-001 |

*Note: the Low row lists 14 IDs, so the Low count should read 14 and the total is 23. The table is reproduced as written.*

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/label_helpers.sh`, `scripts/review_rb_judge.sh` (+1 regression test) | Small |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/implement.yml` | Small |
| Code modularization | `.github/workflows/implement.yml`, new `scripts/implement_handle_noop.sh`, new `scripts/gh_helpers_fallback.sh`, `scripts/workflow_failure_heal_report.sh`, `scripts/workflow_failure_heal_intake.sh`, `scripts/check_failure_triage.sh`, `tests/test_workflow_support_ref_follows_pin.py`, the 6 workflows with inline Telegram curl | Medium |
| Expression size reduction | `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml` (+ `scripts/` extraction if EXPR-004 confirms headroom under 50 KB) | Medium |
| Medium/Low fixes | `scripts/comprehensive_test_and_release_gh_api.sh`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/comprehensive-test-and-release.yml`, `.github/workflows/implement.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/clarify.yml`, `.github/workflows/internal-clarify.yml`, `workflow-templates/ai-clarify.yml`, `tests/test_workflow_file_size_limit.py`, `agents.d/` fragment | Medium |