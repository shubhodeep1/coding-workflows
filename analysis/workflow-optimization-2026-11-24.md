<!-- workflow-log-analysis: window_start=2026-11-23T00:34:23Z window_end=2026-11-24T00:40:50Z run=45298949467 -->
## Executive Summary

- **Data gaps (read first).** Two repositories were truncated, so only part of their runs is listed (`page_limit_reached: true`): `shubhodeep1/tele-funtoken-msg-scoring` and `shubhodeep1/coding-workflows`. No repository failed. The run-logs folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox, so this report rests on the per-run telemetry, the `log_summary` fields and the current repository code. `insufficient_data` is false. Confidence: high.
- **18 of the 31 failures are zero-second mongo-explorer "Tier 1" runs, and they also blinded the log collector.** The six `tier1-*.yml` files failed in three bursts on 2026-11-23 (11:32, 11:44, 12:36 UTC). `fbc_shutdown`, which had 12 such failures on 2026-11-21, has 0 now, so the consumer-side fix exists and only needs applying to mongo-explorer. In `collect_workflow_logs.py::select_notable_runs_for_logs`, failed runs are taken newest first with no de-duplication. That spent 9 of the 15 log slots on these runs, all of which returned 404 (`errors[]`). As a result, 7 real failures had no logs: 4× `update_workflows` "Verify signed sync manifest", the mongo-explorer validate run, a fun-token review gate, and the tele lint gate. Impact: the failure count drops from 31 to 13 once mongo-explorer is fixed, and every failure gets a log slot once selection is de-duplicated. Confidence: high.
- **Scheduled closed-PR cleanup is broken in consumers by a GraphQL error that never succeeds on retry.** `cancel_on_pr_close` in binance-blessings (45299192051) and bitsafe.io (45299047632) failed 5 of 5 attempts with "Field must have selections". It burned about 15–17 s of backoff per tick and then preserved every run, so nothing was cancelled. The query builder in `.github/workflows/cancel_on_pr_close.yml` (lines 235–249) has no guard for a batch with no PR aliases. `_is_gh_permanent_failure` in `scripts/gh_helpers.sh` does not treat GraphQL validation errors as permanent, so they are retried. Confidence: high on the symptom, medium on the exact trigger.
- **The review sweep sends many PRs into full review at once.** 9 `Internal: AI Review & Autofix [pr:N]` runs were dispatched in 15 s (18:03:07–18:03:22) and 12 in 56 s (22:31:21–22:32:17). Each ran 1.6–3.8 h, about 45 h of workflow wall time across the two bursts. Among the 100 slowest runs alone, 18 PRs were reviewed 2–3 times. `review_autofix_sweep.yml` has no per-tick dispatch cap. Recommendation: cap dispatches per tick and log the head SHA and gate state on each dispatch. Confidence: medium (the trigger is an inference from the `[pr:N]` run names and the `*/30` cron).
- **The stable release gate failed.** Test & Mark Stable 45241972349 failed at `orchestrate-decompose-test`. The only `Internal: AI Orchestrate` run in the window, 45246760533, failed at "Run Codex (decomposer)" after 424 s; it is probably that test's child run (inference). The reason is not visible because `analyze_workflow_logs.py` drops all excerpts from the context, by design, and the log folder was not mounted. Confidence: medium.
- **Two metric errors from the 2026-11-21 report are still present.** The collector files `tier1-ci-*` runs under `ci`: 6 of the 7 `ci` failures are mongo-explorer Tier 1 runs, so real CI is 1/67 (1.5%), not 9.6%. Separately, the fleet `cache_hit_rate` is null because 2 of 53 OpenRouter calls had no usage data; computed from the token totals it is about 83%. Confidence: high.

## Speed Optimizations

1. **Critical path: bound how many reviews the sweep dispatches per tick** (shared runner pool, §26).
   - **Evidence:** In coding-workflows, the burst of 9 dispatches at 18:03 (45234198573 … 45234241747) ran 8,488–13,642 s each. The burst of 12 at 22:31–22:32 (45280476021 … 45280621240) ran 5,892–6,979 s each. Every `log_summary` for small scheduled jobs in the same hours shows "All GitHub-hosted runners with label [ubuntu-latest] are busy". For example, promote-candidate follower 45298438895 waited about 278 of 296 s, and the fbc_shutdown poller 45298349015 waited about 212 of 319 s.
   - **Root cause:** In `.github/workflows/review_autofix_sweep.yml` (lines 856–881), every eligible candidate in one tick gets `gh workflow run internal-review.yml`. Only the round cap and the active-run skips limit this.
   - **Change:** Add a per-tick cap. Keep oldest-first order, log `AUTOFIX_SWEEP_SKIP pr=#N reason=dispatch_cap`, and add `skipped_dispatch_cap` to `AUTOFIX_SWEEP_END`. The new variable needs a default (§8) and a uniqueness check before naming (§10).
   - **Savings:** Spreads 9–12 concurrent multi-hour reviews over 2–3 ticks, freeing runner slots for scheduled and short jobs. This is an inference: total review time stays about the same, but queue time for everything else drops.
   - **Risk:** Medium. Some PRs wait up to 30 more minutes. Setting the variable to 0 can mean "unlimited" as a rollback.
   - Finding check: new — the prior entry (`analysis/recommendation-processing-report.md`, 2026-05-23 ledger: "broader sweep-policy work remains deferred") is a deferral, and the cap is absent at `review_autofix_sweep.yml:856-881`.
2. **Micro-optimization: stop retrying deterministic GraphQL validation errors.**
   - **Evidence:** binance-blessings 45299192051 used about 15 s of its 38 s on backoff (2/4/8/16 s). bitsafe.io 45299047632 used about 17 s of 74 s. Both logged "gh command failed after 5 attempts: gh api graphql (flags: -f)".
   - **Root cause:** `_is_gh_permanent_failure` in `scripts/gh_helpers.sh` (lines 1167–1170) matches only HTTP 4xx and auth text. A GraphQL validation error comes back without an HTTP 4xx line, so it is treated as transient.
   - **Change:**
     ```bash
     printf '%s' "$1" | grep -qiE "…existing…|Field must have selections|is declared by .* but not used|Parse error on "
     ```
   - **Savings:** About 15 s and 4 GraphQL calls per affected tick per consumer.
   - **Risk:** Low. These patterns only appear for malformed queries, and the caller still fails open.
   - Finding check: new — no prior verdict; the pattern is absent at `scripts/gh_helpers.sh:1169`.

## Cost Optimizations

1. **Make repeated full reviews of the same PR visible, then cap them.**
   - **Evidence:** In the slow-runs list, 18 PRs had 2 or more full review runs, and pr:9902 (45239387232, 45268608486, 45280205973) and pr:9703 (45234230067, 45261220680, 45280597340) had 3. Review is the main token driver: `review_autofix` used 34,150,324 OpenRouter tokens over 44 calls, and a single run (45234198573, pr:9996) used 7,279,277. The window has no Codex calls.
   - **Root cause:** Unknown. `AUTOFIX_SWEEP_DISPATCH pr=#N head_ref=…` (line 876) does not record which head SHA or gate state caused the dispatch, so a second review of the same head cannot be told apart from a review of a new push.
   - **Change:** Add `head_sha=` and `head_gate_state=` to the existing dispatch line. Both are already in `row` (lines 819 and 842), so no API call is needed. Combine with Speed 1.
   - **Savings:** Enables skipping same-head re-reviews; each one avoided saves roughly 0.6–7 M tokens and 1.5–3.8 h. This is an estimate from the per-run token range in this window.
   - **Quality risk:** None. The change only adds log fields.
   - Finding check: new — no prior verdict on dispatch-reason fields; the line at `review_autofix_sweep.yml:876` lacks them.
- **Semble is cheap and useful here.** 12 queries returned 130,360 bytes (about 10.9 KB each) from 71 sources, with `static_dup_bytes=0` and 0 runtime fallbacks. Bootstrap averaged about 50 s (550,465 ms over 11 runs), which is under 1% of 2–3.8 h review runs. No change is recommended.
- **Serena:** no `SERENA_*` lines in this window.

## Reliability Improvements

1. **De-duplicate and de-prioritize zero-job failures in log selection.** Expected impact: the 7 unexplained failures get logs.
   - **Evidence:** All 9 `missing_log_archive` errors are mongo-explorer zero-second Tier 1 runs, from 45175921861 to 45167839803. Seven failures were left `not_selected`: 45110097830, 45109416266, 45109751275, 45109612915, 45111845646, 45161557266 and 45165874492.
   - **Root cause:** In `scripts/collect_workflow_logs.py` (lines 1336–1380), failed runs are sorted only by creation time. There is no key per (repository, workflow, failure point), and runs with 0 s and no job are not pushed to the back. The cap is `--max-log-runs` (default 15).
   - **Fix:**
     ```python
     def _failure_log_key(run):
         fp = run.get("failure_point") or {}
         return (run.get("repository"), run.get("workflow_name"), fp.get("job_name"), fp.get("step_name"))
     # first pass: newest run per key, excluding duration_seconds == 0 with no job_name;
     # second pass: the deferred duplicates / zero-job runs
     ```
   - **Rollback:** Ordering only; no fields change.
   - Finding check: new — no prior verdict; the ordering at `collect_workflow_logs.py:1336-1380` has no de-duplication.
2. **Guard the cancel-on-close PR-state batch and log its size.** Expected impact: scheduled cleanup works again in consumers on ref 6fe11ee.
   - **Evidence:** In 45299192051 and 45299047632, "Scheduled close cleanup could not resolve PR-state batch 1; preserving affected runs" was followed by "found no cancellable…".
   - **Root cause:** The `_cpr_fetch_pr_state_batches` loop (lines 232–249) always sends `repository(owner:$owner,name:$name){…}`, even when it appended no alias. The selection set is then empty, which gives exactly the logged "no selections" error. Exactly how such a batch arises on ref 6fe11ee is unverified without the logs.
   - **Fix:**
     ```bash
     [ -s "${numbers_file}" ] || return 0
     alias_count=0   # ++ inside the while loop
     echo "CLOSE_CLEANUP_PR_STATE batch=${batch_index} aliases=${alias_count}"
     [ "${alias_count}" -gt 0 ] || continue
     ```
   - Register the new log prefix in an `agents.d/` fragment (§25).
   - **Fail-open:** Unchanged. Unresolved PRs are still preserved.
   - Finding check: new — the 2026-05-17 MERGE-004 batching ask was later applied as this GraphQL batch, but the empty-batch defect is new and present at `cancel_on_pr_close.yml:232-249`.
3. **Put failing-run error lines into the analysis context when the log folder is missing.** Expected impact: the decomposer, stable-gate and resolver failures become diagnosable.
   - **Evidence:** 45246760533 (orchestrate decomposer), 45241972349 (stable gate), 45234224094 (pr:9709 "Run Codex resolver, validate, stage, commit", 8,488 s) and 45242239546 (pr:9935 "Resolve trusted review support commit", 4,268 s) all have `log_download_status: success`, but this report received no excerpt for any of them. `orchestrate.yml:1197` already writes `::error::… orchestrate failed after N attempts (last_status=…)`.
   - **Root cause:** `scripts/analyze_workflow_logs.py` (lines 481–484) deliberately drops all deep-dive logs and relies on the mounted folder, which was absent here.
   - **Fix:** Add a bounded `error_lines` field to `failing_runs` rows: at most 5 `::error::` or `##[error]` lines, each at most 300 characters, taken from the row's `log_excerpts` and passed through `_sanitize_diagnostic_text`. The change is additive.
   - **Risk:** Low. Lines are masked and capped.
   - Finding check: new — no prior verdict; `analyze_workflow_logs.py:481-484` omits excerpts.
4. **Classify only real CI workflows as `ci`.** Expected impact: an accurate CI failure rate (1.5% instead of 9.6%).
   - **Evidence:** mongo-explorer `tier1-ci-coach.yml` and `tier1-ci-doctor.yml` (45175913940, 45175912129, 45167843158, 45167832604, 45166151041, 45166152806) are filed under `ci`.
   - **Root cause:** `normalize_workflow_family` (`collect_workflow_logs.py:616`) uses `if "ci" in normalized`.
   - **Fix:**
     ```python
     if re.match(r"ci(_|$)", normalized) or Path((workflow_path or "").lower()).stem == "ci":
         return "ci"
     ```
     Tier 1 files then fall through to the path stem (`tier1_ci_coach`).
   - **Risk:** Low, but the family tests need updating.
   - Finding check: new — raised in `analysis/workflow-optimization-2026-11-21.md` with no processing verdict (the report ends at 2026-10-08); still present at line 616.
5. **mongo-explorer Tier 1 workflows.** Expected impact: 18 fewer failures in the window.
   - **Evidence:** 18 zero-second failures across all six families at a 100% failure rate. The matching fbc_shutdown runs dropped from 12 failures to 0 since 2026-11-21.
   - **Fix:** Apply the fbc_shutdown change to mongo-explorer's six `tier1-*.yml` files, or disable them. This is consumer code.
   - Finding check: unverified — consumer repository code is not readable from this checkout.
6. **`update_workflows` "Verify signed sync manifest" failed in 4 of 4 consumers within 7 minutes.**
   - **Evidence:** bitsafe.io 45109416266 (04:02), fbc_shutdown 45109612915 (04:04), mongo-explorer 45109751275 (04:05) and binance-blessings 45110097830 (04:09), all on 2026-11-23.
   - **Inference:** The cause is probably on the upstream release or manifest side, since the failures are simultaneous across repositories.
   - **Fix:** After Reliability 1 lands, read the existing `SYNC_MANIFEST outcome=rejected reason=` line (`update_workflows.yml:177`) and the `failure_reason_code` output.
   - Finding check: unverified — the reason line exists in code, but none of these runs had logs collected.

`BREAK_GLASS`: 0. `CONTEXT_BUDGET_WARN`: 0. Semble fallbacks: 8, all contract-test fallbacks (CI 45232457541: 4; Test & Mark Stable 45241972349: 4), with 0 runtime fallbacks, so this is healthy fail-open behaviour. No `SERENA_FALLBACK` or `SERENA_PROBE` lines appeared.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` line appears in any available evidence, so retrieve hit rate, token budget and `keyword_method` mix cannot be computed. The `log_summary` rows that state this (for example 45298783309, 45298658605 and 45298501610) are scheduled jobs with no AI work, where memory would not run anyway. Deep-dive logs for the AI phases were not mounted. The processing report records emission as already implemented in `scripts/ai_memory.py` and `scripts/memory_helpers.sh`, and this window shows no defect, so there is no new recommendation. Reliability 3 (`error_lines`) and mounting the log folder would restore visibility.

## GH API Call Audit

1. **`cancel_on_pr_close` scheduled tick: 5 GraphQL calls that can never succeed.**
   - **Evidence:** 45299192051 and 45299047632.
   - **Change:** Apply Reliability 2 and Speed 2 together. Batches with no aliases then cost 0 calls, and malformed queries cost 1 call instead of 5. That saves up to 4 GraphQL calls per tick in each affected consumer.
   - **API hygiene (§14):** The existing batched helper is kept; nothing new is added.
   - Finding check: new — the defect is at `cancel_on_pr_close.yml:232-249` and `gh_helpers.sh:1169`.
2. **Collector: 9 log-archive calls per collection returned 404.**
   - **Evidence:** All 9 entries in `errors[]`.
   - **Change:** With Reliability 1, zero-job runs move behind every other failure, which saves up to 9 wasted calls per collection.
   - Finding check: new — `collect_workflow_logs.py:1336-1380`.
3. **Auto-heal reconcile looks like one call per item.**
   - **Evidence:** tele-funtoken 45298658862 shows `listed=40 processed=20 in_progress=20 skipped_budget=20` at about 0.2–0.5 s per item, and 11 sources repeat `reason=human_review` on every tick.
   - **Change:** Use one aliased GraphQL batch per tick (§14), and skip `human_review` sources before the per-item lookup.
   - Finding check: unverified — `AUTO_HEAL_RECONCILE` is not emitted by any file in this repository, so it is consumer code.
- **Healthy patterns seen:** a permanent 404 was not retried (`GH_API_RETRY outcome=permanent … promote-cycle-frozen`, 45298438895). `GH_LOOP_CALL_LINT warnings=0` (45298292358). `GH_PAT_BUDGET remaining=4386` (45297409133). No HTTP 429 or secondary-rate-limit events appear in any `log_summary`.

## Prompt Cache & Memory System

1. **Report a cache hit rate even when a few calls lack usage data.**
   - **Evidence:** The fleet and `review_autofix` `cache_hit_rate` is null even though 28,197,551 cache-read tokens were logged; only 2 of 53 calls lacked usage. Computed: 28,197,551 / (4,187,918 + 1,453,248 + 28,197,551) = 83.3% for the fleet and 83.5% for review. Per run: 45231663401 0.905, 45234826740 0.738, 45234759287 0.630, 45249366028 0.439. Run 45234198573 shows null despite 6.08 M cache reads.
   - **Root cause:** `compute_cache_hit_rate` (`scripts/cost_audit.py:455-456`) returns None when any call is missing usage data.
   - **Change:** Keep `cache_hit_rate` as it is for compatibility. Add `cache_hit_rate_available_calls` (the same ratio over calls that do report usage) and `usage_coverage` (for example `51/53`).
   - **Impact:** Restores a usable fleet number; telemetry only, no cost change.
   - Finding check: recurring — the prior verdict was already-implemented (`compute_cache_hit_rate`, report line 1149), but the fleet value is still null in this window because of 2 calls without usage data.
- **Fragmentation note:** Test & Mark Stable 45241972349 made 9 calls that wrote 33,309 cache tokens and read 0 (`cache_hit_rate` 0.0). The absolute cost is small, and the prefix-hash diagnostic was already rejected (report line 1258), so this is not raised.
- `CONTEXT_BUDGET_WARN`: 0.

## Orchestrator Health

- **Indicators:**
  - `orchestrate` succeeded 0 of 1 times (45246760533, decomposer).
  - `orchestrate_poll` succeeded 586 of 630 times with 0 failures; the 44 cancellations are coalesced ticks.
  - `orchestrate_clarify_respond` correctly skipped closed issue #10022: `AI_PHASE_GATE_V1 … reason=issue_closed outcome=skip` (45298616647).
  - Pollers in fbc_shutdown, mongo-explorer and binance-blessings report `SEMBLE_AVAILABLE: false` in lazy mode, with no fallback lines; this is expected.
  - Auto-heal reconcile has a backlog: 40 listed against a cap of 20 per tick, with 11 sources parked as `human_review` (45298658862).
1. **Stable gate blocked by a decomposer failure.**
   - **Evidence:** 45241972349 failed at "Dispatch internal-orchestrate.yml with multi-issue project". Its probable child, 45246760533, failed at "Run Codex (decomposer)" after 424 s (inference from timing and from this being the window's only orchestrate run).
   - **Mitigation:** Reliability 3 surfaces the existing `orchestrate failed after N attempts (last_status=…)` line, so the next report can attribute the failure to a timeout, a lost capacity wait, or too few issues.
   - **What to track:** `orchestrate` failure rate, and the `last_status` values once surfaced.
   - Finding check: new — same defect as Reliability 3 (`analyze_workflow_logs.py:481-484`).

## Pipeline Flow Bottlenecks

| Stage | Dominant cost | Evidence |
|---|---|---|
| clarify / plan / implement | Queueing; most runs are skipped by job-level `if:` | clarify: 222 of 247 "other"; plan: 217 of 236; implement: 194 of 210. Clarify 45297901087 waited about 224 s of 515 s for a runner. |
| review/autofix | Compute plus capacity, and repeated reviews of the same PR | p50 1,718 s, p95 8,735 s; dispatch bursts of 9 and 12; 18 PRs reviewed 2 or more times |
| validate | Product outcome | mongo-explorer 45111845646 "Enforce validation outcome" (logs not selected) |
| release / merge | Stable gate failed | 45241972349 (9,215 s) |
| scheduled maintenance | Runner queue | Waits of 8–278 s in 18 `log_summary` rows |

1. **Review sweep fan-out is the largest end-to-end lever.** See Speed 1 (per-tick cap) and Cost 1 (dispatch-reason fields).
   - Finding check: new — deferred per the 2026-05-23 ledger; no cap at `review_autofix_sweep.yml:856-881`.
- Separating queue time from compute time in telemetry was proposed and rejected as measurement-only (report line 2282), and this window brings no defect that answers that reason, so it is not re-raised. The same applies to skipped-run volume (lines 1737 and 1854).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Bottlenecks:** review runs of 5,274–13,642 s; dispatch bursts at 18:03 and 22:31.
- **Failures:** stable gate 45241972349; decomposer 45246760533; pr:9709 resolver 45234224094; pr:9935 gate 45242239546; CI pr:9637 45232457541 (review-blocked budget contract tests).
- **Cost:** 34.19 M OpenRouter tokens; the largest single run is 45234198573 with 7.28 M. The run list is truncated.
- **Actions:**
  1. Cap sweep dispatches per tick (Speed 1).
     - Finding check: new — deferred in the 2026-05-23 ledger; cap absent at `review_autofix_sweep.yml:856-881`.
  2. Surface `error_lines` for failing runs (Reliability 3).
     - Finding check: new — `analyze_workflow_logs.py:481-484`.
  3. Fix log selection and the `ci` classifier (Reliability 1 and 4).
     - Finding check: new — `collect_workflow_logs.py:616` and `:1336-1380`.

### shubhodeep1/tele-funtoken-msg-scoring
- **Failures:** lint 45165874492 (S110/S112 gate) and review gate 45255524892. The run list is truncated, and 619 of 998 runs are skipped.
- **Actions:**
  1. Batch the auto-heal reconcile lookups (GH API 3).
     - Finding check: unverified — consumer code.
  2. Get lint logs selected via Reliability 1.
     - Finding check: new — `collect_workflow_logs.py:1336-1380`.

### shubhodeep1/mongo-explorer
- **Failures:** 20 failures (9.5%): 18 Tier 1, validate 45111845646, update 45109751275.
- **Actions:**
  1. Fix or disable the six `tier1-*.yml` files.
     - Finding check: unverified — consumer code.
  2. Read the sync-manifest rejection reason.
     - Finding check: unverified — run not selected for logs.

### shubhodeep1/fbc_shutdown
- **Health:** Tier 1 failures dropped from 12 to 0. One failure: update 45109612915 (15 s).
- **Action:** Read the sync-manifest reason (Reliability 6).
  - Finding check: unverified — no logs collected.

### shubhodeep1/binance-blessings
- **Failures:** update 45110097830. Cancel-on-close GraphQL failure in 45299192051.
- **Actions:** Reliability 2 and Speed 2.
  - Finding check: new — `cancel_on_pr_close.yml:232-249`.

### shubhodeep1/bitsafe.io
- **Failures:** update 45109416266. Cancel-on-close GraphQL failure in 45299047632.
- **Actions:** Reliability 2 and Speed 2.
  - Finding check: new — `cancel_on_pr_close.yml:232-249`.

### shubhodeep1/fun-token-multi-chain
- **Failures:** review gate 45161557266 (322 s, not selected). AI Review runs of 5,312–6,130 s (45165994461, 45168816235).
- **Action:** Get the gate failure logs via Reliability 1.
  - Finding check: new — `collect_workflow_logs.py:1336-1380`.

### shubhodeep1/multi-user-ai-agent
- **Health:** 0 failures in 172 runs. Scheduled jobs mostly wait in the runner queue (45298783309 waited about 19 of 29 s).
- **Action:** None beyond the fleet-wide Speed 1.
  - Finding check: unverified — no failure in this window supports a repository-specific finding.

## Metrics Appendix

**Runs**

| Repo | Runs | Success | Fail | Cancelled | Other | Fail rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| coding-workflows (truncated) | 1000 | 389 | 5 | 21 | 585 | 0.50% | 8 | 7201 |
| tele-funtoken (truncated) | 998 | 365 | 2 | 12 | 619 | 0.20% | 8 | 1821 |
| binance-blessings | 430 | 243 | 1 | 16 | 170 | 0.23% | 159 | 2067 |
| fun-token-multi-chain | 349 | 223 | 1 | 13 | 112 | 0.29% | 314 | 2220 |
| mongo-explorer | 211 | 153 | 20 | 7 | 31 | 9.48% | 219 | 2116 |
| multi-user-ai-agent | 172 | 135 | 0 | 3 | 34 | 0% | 253 | 1675 |
| fbc_shutdown | 152 | 142 | 1 | 4 | 5 | 0.66% | 336.5 | 1664 |
| bitsafe.io | 152 | 137 | 1 | 5 | 9 | 0.66% | 382 | 1932 |
| **Fleet** | **3464** | **1787** | **31** | **81** | **1565** | **0.89%** | **72** | **3099** |

**Families of note:**
- `review_autofix`: 430 runs, 4 failures, p50 1,718 s, p95 8,735 s.
- `ci`: 73 runs, 7 failures, 6 of which are Tier 1.
- `update_workflows`: 4 failures in 7 runs.
- `test_and_mark_stable`: 1 run, 1 failure.
- `orchestrate`: 1 run, 1 failure.

**Tokens and cache**

| Metric | Value |
|---|---|
| OpenRouter calls (with usage / without) | 53 (51 / 2) |
| Prompt / completion tokens | 4,187,918 / 383,707 |
| Cache read / cache write tokens | 28,197,551 / 1,453,248 |
| Total tokens | 34,186,947 |
| Codex calls / tokens | 0 / 0 |
| `cache_hit_rate` (as reported) | null |
| Cache hit rate computed from token totals | 83.3% fleet / 83.5% `review_autofix` |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 12,626,280 (105 samples) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**MCP telemetry**

| Server | Queries | Bytes | Sources | Bootstraps (ms) | Fallbacks (runtime / contract-test) | Probe ok / failed / skipped |
|---|---|---|---|---|---|---|
| Semble | 12 | 130,360 (about 10.9 KB per query) | 71 | 11 (550,465; 0 failed; 0 unused) | 0 / 8 | n/a |
| Serena | 0 | 0 | — | — | 0 | 0 / 0 / 0 |

Other MCP servers observed: none.

**GH API signals**

| Signal | Count / evidence |
|---|---|
| GraphQL attempts that could not succeed (cancel-on-close) | 5 per tick in each of 45299192051 and 45299047632 |
| Log-archive 404s (collector) | 9 |
| `GH_API_RETRY outcome=permanent` | 1 (45298438895, expected tag 404) |
| HTTP 429 / secondary rate limit | 0 observed |

**Findings check**

| Status | Count |
|---|---|
| new | 7 |
| recurring | 1 |
| reopened | 0 |
| unverified | 3 |
| dropped: already fixed on this ref | 2 (AI-memory emission check; Semble contract-test fallback classification) |
| dropped: previously decided | 2 (queue-time separation, rejected at report line 2282; skipped-run volume, rejected at lines 1737 and 1854) |

**Data gaps**
- The log folder was not mounted.
- Two repositories were truncated (tele-funtoken-msg-scoring, coding-workflows).
- 7 failures have no logs (`not_selected`) and 9 runs have no log archive (404).
- `analysis/recommendation-processing-report.md` has verdicts only up to `workflow-optimization-2026-10-08.md`. Findings first raised on 2026-11-21 therefore have no recorded verdict.