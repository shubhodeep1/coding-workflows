<!-- workflow-log-analysis: window_start=2026-11-27T13:58:18Z window_end=2026-11-28T01:01:58Z run=46089827103 -->
## Executive Summary

- **Every review/autofix failure in this window failed in the merge-conflict resolver, and the same PRs keep failing there.** All 15 `review_autofix` failures (15 of 311 runs, 4.8%) stopped at `codex-agent` / `Run Codex resolver, validate, stage, commit`. The failures repeat on the same PRs: PR 10125 failed 5 times (16,226 s wall clock), PR 10000 4 times (15,608 s), PR 9442 twice (11,517 s), and `Review-Blocked Judge Dispatch` 3 times (9,460 s). In total that is 56,057 s, about 15.6 h of wall clock. Fix: classify the `RESOLVER_TERMINAL` exit classes for these runs and lower the repeat-failure escape threshold (Reliability 1). Estimated impact: up to about 8 h of wall clock and runner time per window. Confidence: high that the failures concentrate on these PRs, medium on the cause, because the full logs were not available.
- **Scheduled close cleanup sends a GraphQL query with an empty selection, and `gh_retry` retries that deterministic error five times.** In fun-token-multi-chain run 46089440831, all 5 attempts returned `Field must have selections` and about 17 s went on backoff before the cleanup skipped the batch. The same reusable workflow (`cancel_on_pr_close.yml`) runs for every consumer. Fix: skip a batch that has no aliases (Reliability 2) and treat GraphQL validation errors as non-retryable (Reliability 3). Estimated impact: about 5 wasted GraphQL calls and about 15 s of backoff saved on each affected tick. Confidence: medium-high.
- **The pipeline's most expensive failure mode has no recorded token spend.** `codex_calls` and `codex_tokens_used` are 0 across all 130 parsed runs. All 15 failed resolver runs show `or_calls=0`, even though they ran 1,293–6,376 s. The editor and resolver run through OpenCode, and `scripts/cost_audit.py` has no OpenCode usage parser (Cost 1). Estimated impact: makes the resolver and editor spend visible. Confidence: medium.
- **`cache_hit_rate` is `null` for the window and for coding-workflows**, because `compute_cache_hit_rate` returns `None` whenever any call lacks usage data (`or_usage_unavailable_calls=3` of 35). Applied to the calls that did report usage, the collector's own formula gives **0.788** (25,652,554 / 32,559,679). Fix: add a rate computed over the usage-available calls (Prompt Cache 1). Confidence: high.
- **Data gaps:**
  - `shubhodeep1/coding-workflows` was truncated: the page limit was reached, so only 1,000 of its runs are listed.
  - The full run-logs directory (`/home/runner/work/_temp/workflow-log-output`) is not mounted, so no deep-dive excerpts could be read. Findings rely on `analysis_context.json` and `log_summary` fields.
  - The log archive for run 46074359581 returned HTTP 404.
  - No repository failed collection.

## Speed Optimizations

1. **Critical path: stop spending 30–100 min per review run on a merge-conflict resolver that keeps failing on the same PR.** This is the same root finding as Reliability 1.
   - **Evidence:** PR 10125's resolver failed at 18:59, 20:06, 21:04, 21:55 and 22:41 (runs 46053420527, 46061356988, 46067865422, 46073417445, 46078134836). PR 10000's failed at 19:00, 20:35, 21:55 and 22:41 (46053608408, 46064705450, 46073438071, 46078156141). Each run took 1,810–5,963 s.
   - **Root cause:** the resolver step allows up to 3 attempts of 50 min each (step comment in `.github/workflows/review_autofix.yml`, `timeout-minutes: 170`). Escalation waits for `RESOLVER_ESCAPE_THRESHOLD_N`, which defaults to 5 (`review_autofix.yml`, read in `scripts/review_conflict_resolve.sh`).
   - **Change:** see Reliability 1.
   - **Estimated saving:** at a threshold of 3, PR 10125 would have stopped after its third run. That saves the last two runs (7,184 s), and PR 10000 would have saved its fourth (5,963 s).
   - **Risk:** low, because it is a repo-variable change.

   Finding check: new — evidence in runs 46078134836 and 46078156141 (`review / codex-agent`, `Run Codex resolver, validate, stage, commit`). The processing report has no verdict on repeated resolver failures on the same PR. Its "Reliability 1 (classify resolver terminal failures)" entry (2026-10-07-2) is `applied` and covers only the `RESOLVER_TERMINAL` record, not a repeat cap.

2. **Micro-optimization: skip the empty PR-state GraphQL batch in scheduled close cleanup.** This is the same change as Reliability 2.
   - **Evidence:** in run 46089440831 the job's 180 s included about 17 s of backoff (00:46:28 to 00:46:45) on a request that could never succeed.
   - **Estimated saving:** about 15–17 s per affected tick for each consumer.
   - **Risk:** low.

   Finding check: new — `_cpr_fetch_pr_state_batches` in `.github/workflows/cancel_on_pr_close.yml` (lines 218-262) still sends `repository(...){}` when a batch holds no numeric PR. The processing report has no matching verdict.

## Cost Optimizations

1. **Record writer-role token usage (editor and conflict resolver), so the most expensive failure mode has a measured cost.**
   - **Evidence:**
     - `codex_calls=0` and `codex_tokens_used=0` across all 130 runs with parsed telemetry.
     - The 15 failed `review_autofix` and judge runs show `or_calls=0` while running 1,293–6,376 s. For example, run 46078156141 ran 5,963 s and its only recorded cost signal is `semble_query_calls=1`.
     - Successful review runs record only reviewer usage, for example `or_calls=8` and 11,191,972 tokens in run 46071545242.
   - **Root cause:** `scripts/cost_audit.py` parses only the Codex CLI `tokens used` lines, the reviewer `INFO: openrouter usage` lines and Claude usage. It has no OpenCode parser: a search for `opencode` in that file returns nothing. The editor and resolver run through OpenCode, and `MODEL_EDITOR` is `openai/gpt-6-sol`.
   - **Change:** after each OpenCode writer call (editor, and each resolver attempt), emit one `INFO: openrouter usage phase=editor|resolver ...` line in the format `OPENROUTER_RE` already parses. Use `usage_available=false usage_missing_reason=...` when the OpenCode output has no token counts. Inference: the token counts should come from OpenCode's own run output; check that the pinned OpenCode version exposes them before wiring this. This is additive, and no model or prompt behaviour changes.
   - **Estimated saving:** none directly. It prices the 56,057 s of failed resolver time, so Reliability 1 can be ranked by money, not only by time.
   - **Quality risk:** none, because it is log output only.

   Finding check: reopened — a prior row rejected "per-phase model counters" as "diagnostic experiments ... that need a quality baseline before any change" (2026-10-06 entry, "Exec 5 / Cost 2 / Cost 3 / Cost 4"). This proposal changes no model or prompt, so it needs no quality baseline. The new evidence is 15 failed runs and about 15.6 h of resolver wall clock with zero recorded tokens.

2. **Avoidable reruns: the repeated resolver failures in Reliability 1 are the largest avoidable spend in this window.** No separate change is needed; see Reliability 1 for the evidence and the fix. One prompt-related note: each failed review run still ran one Semble query (9,784–11,715 bytes, 5–8 sources) and one Semble bootstrap (53–65 s) before failing.

   Finding check: new — same evidence as Reliability 1 (runs 46073417445, 46073438071 and others), with no prior verdict on a repeat cap.

**Semble and Serena:**
- **Semble is cutting prompt context without adding noise.**
  - 16 queries returned 178,177 bytes, about 11.1 KB per query, from 108 sources (6.75 per query).
  - `semble_static_dup_bytes_total=0` and `semble_echo_lines_dropped=0`.
  - 15 bootstraps took 886,417 ms in total (about 59 s each), with 0 failed and 0 unused.
  - The roughly 59 s bootstrap is 1–3% of a review run.
  - No change is recommended.
- **Serena is effectively disabled.** All Serena query, tool-call and probe counters are 0, and run 46089780844 logged `SERENA_ENABLED: false`.

## Reliability Improvements

1. **Merge-conflict resolver failures repeat on the same PRs. They are 100% of `review_autofix` failures (15/15).**
   - **Failure evidence:**

     | PR / workflow | Run IDs | Total wall clock |
     |---|---|---|
     | PR 10125 | 46053420527, 46061356988, 46067865422, 46073417445, 46078134836 | 16,226 s |
     | PR 10000 | 46053608408, 46064705450, 46073438071, 46078156141 | 15,608 s |
     | PR 9442 | 46038523823, 46050107346 | 11,517 s |
     | `judge / codex-agent` | 46074865888, 46077695722, 46087324244 | 9,460 s |
     | Untagged run | 46083681741 | 3,246 s |

   - **Root cause category:** non-convergent conflict resolution, plus a high escape threshold. The `RESOLVER_TERMINAL ... exit_class=` record that would separate `timeout`, `no_progress`, `fingerprint`, `scope` and `provenance` exists in `scripts/review_conflict_resolve.sh` (lines 432-534). It could not be read because the run-logs directory is not mounted.
   - **Exact fix:**
     1. On the next collection, extract `RESOLVER_TERMINAL` from these 15 runs.
     2. If the exit class repeats per PR (for example `no_progress` or `fingerprint` on every attempt), set the existing repo variable `RESOLVER_ESCAPE_THRESHOLD_N=3`. It is read at `review_autofix.yml` line 4658 with default `'5'`, so no code change is needed.
     3. Check that PR 10125's fifth failure (run 46078134836) actually escalated: an `ai:resolver-escalated` label or an `AUTOFIX_RESOLVER_ESCALATED_V1` comment should exist. This code read could not confirm whether the retry-state threshold applies outside `orchestrator/project-*` integration-sync PRs.
   - **Expected impact:** about 30–40% fewer resolver runs per stuck PR. At the observed pace, PRs 10125 and 10000 would have each saved 1–2 runs, roughly 13,000 s of wall clock.
   - **Rollback:** reset the variable to 5. Escalation fails toward human review (`ai:resolver-escalated` is on the heal intake's label list in `internal-workflow-failure-heal.yml`), so nothing is merged unsafely.

   Finding check: new — the processing report has no verdict on per-PR repeat failures. The 2026-10-07-2 "Reliability 1" row (`applied`) added only the `RESOLVER_TERMINAL` record.

2. **Scheduled close cleanup sends a GraphQL query with an empty selection.**
   - **Failure evidence:** in fun-token-multi-chain run 46089440831 (`cancel` / `cancel-active-runs`), every attempt returned `Field must have selections (field 'repository' returns Repository but has no selections)`. The job then logged `could not resolve PR-state batch 1` and `found no cancellable queued/in-progress pull_request workflow runs`.
   - **Root cause:** a code defect. `_cpr_fetch_pr_state_batches` (`.github/workflows/cancel_on_pr_close.yml`, lines 231-248) always sends `repository(owner:$owner,name:$name){` plus `}}`, even when the batch file has no numeric lines. The `log_summary` claim that `$owner`/`$name` are never used is wrong, because the query does use them; the error describes an empty selection set. Inference: the PR-number list was empty in that tick, and whether `split` writes an empty part file for empty input depends on the `split` build in the runner image (the summary mentions ubuntu-26.04).
   - **Exact fix**, inside the existing function (additive, fail-open behaviour unchanged):
     ```bash
     [ -s "${numbers_file}" ] || { echo "CANCEL_CLEANUP_PR_STATE batch=0 aliases=0 outcome=skipped_empty"; return 0; }
     ...
                   local alias_count=0
                   while IFS= read -r pr_number; do
                     case "${pr_number}" in ''|*[!0-9]*) continue ;; esac
                     query+="pr_${pr_number}:pullRequest(number:${pr_number}){number state}"
                     alias_count=$((alias_count + 1))
                   done < "${batch_file_part}"
                   query+='}}'
                   if [ "${alias_count}" -eq 0 ]; then
                     echo "CANCEL_CLEANUP_PR_STATE batch=${batch_index} aliases=0 outcome=skipped_empty"
                     continue
                   fi
     ```
   - **Expected impact:** removes a deterministic GraphQL failure, its retries and the `::warning::` noise on every tick with no PR-linked runs, across 8 repos.
   - **Rollback:** revert. The `printf '{}'` output default already makes downstream consumers preserve runs.

   Finding check: new — the current code still builds the empty selection. Prior `cancel_on_pr_close` rows (MERGE-004 and DUP-002 at report lines 250 and 523; unlinked-run listing `applied` at line 1425) do not cover the query construction.

3. **`gh_retry` retries GraphQL validation errors that can never succeed.**
   - **Failure evidence:** run 46089440831 made 5 attempts with backoff of 1, 2, 4, 8 and 16 s, then logged `gh command failed after 5 attempts`.
   - **Root cause:** `_is_gh_permanent_failure` (`scripts/gh_helpers.sh` line 2036-2039) matches only HTTP 400/401/404/422, auth and not-found patterns. It has nothing for GraphQL schema or validation messages, which `gh` reports with a generic exit 1.
   - **Exact fix:** add a narrow alternation for deterministic validation messages only:
     ```bash
     ...|Field must have selections|Parse error on |doesn't exist on type|is declared by [^ ]+ but not used|Variable \$[A-Za-z_]+ of type"
     ```
     Do not match `rate limit`, `timeout` or `Something went wrong`, so transient GraphQL failures keep retrying.
   - **Expected impact:** 4 fewer calls per occurrence, and no 15 s backoff, for every caller of `gh_retry gh api graphql`.
   - **Rollback:** remove the alternation. A misclassified error fails fast instead of retrying; every caller observed here already fails open.

   Finding check: new — the processing report has only API-003 (HTTP 401, `already-implemented`). Earlier retry-classification items (API-001/002) were rejected because their source docs tagged them RISKY_SKIP. This proposal is limited to deterministic validation strings, with direct evidence from run 46089440831.

**Other signals (no change recommended):**
- **`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`:** 0 and 0 in all parsed runs, so there is no rubric or prompt-size pressure.
- **Semble fallbacks:** 32, all `contract-test` (4 each in CI runs 46074644597, 46074373587, 46081028662, 46066262809, 46071890461, 46062579282, 46044281397, 46075567359), with 0 runtime fallbacks and 0 failed bootstraps. This is healthy fail-open behaviour in tests, not a broken rollout.
- **CI failures (10 of 75, 13.3%):** these are spread across 8 distinct steps and look like normal per-PR gating. PR 10133 failed `Clarify loop guard unit tests` twice (46064436013, 46081028662), and PR 9902 failed `YAML lint` in both `CI quick` (46074364896, 70 s) and full CI (46074373587). Two were infrastructure: `Checkout repository` (46040693324) and `Fetch base commits for the shallow checkout` (46066262809), and that step already retries 3 times (`ci.yml` line 4561). No change without logs.

## AI Memory Health

- **No `AI_MEMORY_TELEMETRY` lines were available.** The run-logs directory is not mounted, and every `log_summary` that mentions the prefix says it was not visible because of truncation (for example 46090210236, about 390k characters dropped; 46089648800, about 768k dropped). So the following cannot be computed:
  - retrieve hit rate
  - `estimated_tokens` against the budget
  - the `keyword_method` mix (`llm`/`plain`/`none`)
  - `fail_open` and `enabled: false` counts
  - push retry counts

1. **Confirm emission on the next collection.** Mount `/home/runner/work/_temp/workflow-log-output` for the analysis job, and grep the deep-dive logs of implement, plan and review runs for `AI_MEMORY_TELEMETRY: {"op":"retrieve"`. If zero retrieves appear in runs that ran a full plan or implement phase (not 1 s skipped wrappers), treat that as an emission regression. Expected impact: restores memory-health coverage. Risk: none.

   Finding check: unverified — the run-logs directory could not be read. The processing report (2026-10-08 row, Cost #1/#2/#4) records `AI_MEMORY_TELEMETRY` as already emitted.

## GH API Call Audit

1. **`cancel_on_pr_close` scheduled cleanup wastes calls on an empty PR-state batch.**
   - **Evidence:** 5 GraphQL calls returned the same validation error in fun-token-multi-chain run 46089440831.
   - **Change:** apply Reliability 2 and 3. The batching contract stays at at most 1 aliased GraphQL call per 50 PRs, and becomes 0 calls when there are no PR-linked active runs.
   - **Estimated reduction:** 5 calls per affected tick, per repo. Inference: `cancel_on_pr_close` had 100 runs this window, so the upper bound is about 500 GraphQL calls, if most scheduled ticks had no PR-linked runs.
   - **Rate-limit risk reduction:** small but continuous, on the shared PAT.

   Finding check: new — same evidence and code as Reliability 2. The workflow complies with the §14 batching rules; the defect is that it calls when there are zero items.

**Observation (no change recommended):**
- The shared PAT's `GH_PAT_BUDGET ... remaining=` fell from 4,455 (heal intake 46089755415, about 00:49) to 4,351 (deterministic-skip-merge 46089780844, about 00:52) and to 1,827 (clarify 46090718646, about 01:01). Inference, low confidence: if these are the same token and window, about 2,500 calls were spent across the 8 repos in roughly 10 minutes.
- Per-job counts stay small: `calls_in_job=17`, `13` and `1`. No 429s appear in any summary.
- `used_in_job=unknown` in all three lines is expected, because the token is shared (`gh_pat_budget` in `scripts/gh_helpers.sh` lines 1222-1228), and `calls_in_job=` already gives per-job usage.

## Prompt Cache & Memory System

1. **`cache_hit_rate` is reported as `null` for the window whenever any call lacks usage data.**
   - **Evidence:** the summary, the coding-workflows aggregate and the `review_autofix` family all show `cache_hit_rate: null` with `or_usage_unavailable_calls=3`. The null comes from PR 10167's runs 46050220727 (1 call without usage) and 46077005582 (2). Runs with complete usage report a rate: 0.82122 for 46071545242 and 0.713082 for binance-blessings 46076249301.
   - **Root cause:** `compute_cache_hit_rate` in `scripts/cost_audit.py` (lines 454-456) returns `None` when any call lacks usage. That is correct per call, but at window scale 3 of 35 calls hide the signal.
   - **Change:** add the field `cache_hit_rate_usage_available` (the name was checked and is unused), alongside the unchanged `cache_hit_rate`:
     ```python
     def compute_cache_hit_rate_usage_available(values):
         # Same formula, over calls that reported usage only (unavailable calls
         # contribute no token counts to the sums).
         if _to_int(values.get("or_usage_available_calls"), 0) <= 0:
             return None
         ...  # read / (prompt + write + read)
     ```
     Thread it through `RUN_COST_TELEMETRY_FIELDS` and the aggregation.
   - **Estimated impact:** restores the window rate, which is **0.788** with this formula (25,652,554 / (5,085,401 + 1,821,724 + 25,652,554)). That is down from the 0.857 reported on 2026-11-25, though that figure may have used different inputs.
   - **Risk:** none, because the field is additive.

   Finding check: new — `scripts/cost_audit.py` lines 454-456 on this ref. The prior OpenRouter-usage-availability row (line 1048, applied) introduced the unavailable split but not an available-only rate.

**Cache behaviour:**
- Cache reads are 78.8% of review input tokens: 25.65 M read against 5.09 M uncached prompt and 1.82 M write.
- binance-blessings' single run is lower at 0.713. With one sample, cache fragmentation cannot be attributed.
- The one Claude sample, unblock judge 46090113640, wrote 101,386 cache tokens, read 246,859 and cost $0.92.
- The prior verdicts rejected prefix restructuring as an experiment (2026-10-06, 2026-09-26-3), and nothing in this window answers that, so no prefix change is proposed.

## Orchestrator Health

- **Flow is healthy.**
  - `orchestrate_poll` ran 353 times: 304 succeeded and 48 were cancelled (superseded ticks).
  - The single failure (46074359581, 0 s, log archive HTTP 404) started in the same minute as PR 9902's `YAML lint` failures (46074364896, 46074373587). Inference: it was a workflow-file-issue start failure, which the collector already records as `startup_failure`.
  - In consumer pollers, the real poll work took about 96–240 s (fbc_shutdown 46090213130, fun-token-multi-chain 46089848513).
  - `SEMBLE_AVAILABLE=false` with `SEMBLE_BOOTSTRAP_MODE=lazy` is expected in pollers.
  - Auto-release correctly deferred (`AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=2`, 46089484343).
  - The merge-train was idle (`MERGE_LEASE ... outcome=no_waiter`).
  - The unblock judge held item 9709 until #10073 closes (`hold=item_closed`, 46090113640), which is correct behaviour.

1. **The main pain point is the conflict-resolver loop on PRs 10125 and 10000. Track and cap it as in Reliability 1.**
   - Indicators to track: `RESOLVER_TERMINAL exit_class` counts per PR, the count of open PRs labelled `ai:resolver-escalated`, and the number of `review_autofix` failures per PR per day.
   - Alert threshold: more than 2 resolver failures on the same PR in 24 h.

   Finding check: new — same evidence as Reliability 1 (runs 46053420527 to 46078134836 on PR 10125).

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| Clarify | Queueing | 46089877602: 798 s total, about 7.5 min waiting for a runner. 46090210236: 557 s, about 4 min queued. |
| Plan | Compute, with some queueing | 46089711171 / 46089648800: 545–550 s, of which 74–79 s queued and about 7.5 min in the plan phase. |
| Implement | Mostly 1 s skipped wrappers (73 of 79) | There is no deep-dive data for full implement runs. |
| Review/autofix | Compute and retry (resolver) | p50 2,022 s, p95 5,319 s. 56,057 s spent in failed resolver runs. |
| CI | Compute; queueing inferred | p50 1,505 s, p95 6,075 s. A failing full CI run took 5,402 s against 70 s for `CI quick` on the same lint failure. |
| Orchestrate | Queueing ≈ compute | 46089658795: 828 s, of which 7m22s queued (about 53%). |

- **Queueing:** almost every `log_summary` reports "All GitHub-hosted runners with label [ubuntu-latest] are busy", with waits from 7 s to 7.5 min. This is the account-wide concurrency limit covered by §26 and `docs/plans/reduce-actions-run-volume-plan.md`, and no new fix is proposed here.
- **Retry and merge-conflict overhead:** this is the largest avoidable cost and is ordered first by end-to-end impact; see Reliability 1. The empty-batch fix (Reliability 2) is second and small.

Finding check for the resolver item: new — runs 46078134836 and 46078156141. Same finding as Reliability 1, not counted again.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Runs:** 1,000 (truncated), with 26 failures (2.6%), 30 cancelled and 480 skipped/other; p50 57 s, p95 4,596 s.
- **Bottlenecks:** review/autofix duration, plus runner queueing.
- **Failure modes:**
  - resolver step (15)
  - CI per-PR test failures (10)
  - one start failure (46074359581)
- **Cost drivers:** reviewer tokens, 31.18 M across 27 calls. Editor and resolver spend is not measured.
- **Actions:**
  1. Lower the resolver escape threshold after classifying `RESOLVER_TERMINAL` (Reliability 1). Finding check: new — PR 10125 failed 5 times and PR 10000 4 times.
  2. Add OpenCode writer usage lines (Cost 1). Finding check: reopened — 15 failed runs with `or_calls=0`, answering the 2026-10-06 "needs quality baseline" reason.
  3. Add `cache_hit_rate_usage_available` (Prompt Cache 1). Finding check: new — `cost_audit.py` lines 454-456.

### shubhodeep1/fun-token-multi-chain
- **Runs:** 78, with 0 failures, p50 596 s and p95 1,950 s.
- **Issue:** scheduled close cleanup hit the empty GraphQL batch (46089440831).
- **Actions:**
  1. Skip the empty batch in `cancel_on_pr_close.yml` (Reliability 2). Finding check: new — run 46089440831.
  2. Classify GraphQL validation errors as permanent (Reliability 3). Finding check: new — run 46089440831.

### shubhodeep1/binance-blessings
- **Runs:** 196, with 0 failures and 81 skipped/other; p50 128 s, p95 1,754 s.
- **Review:** one 4,556 s review (46076249301), with `cache_hit_rate` 0.713 over 8 calls and 1.63 M tokens.
- **Workflow Integrity:** the push run 46089961886 skipped as `no_managed_wrapper_changed`. That skip path is already documented in `workflow-templates/ai-workflow-integrity.yml` (lines 17-26).
- **Actions:**
  1. Fixes from Reliability 2 and 3 reach this repo through the reusable `cancel_on_pr_close.yml`. Finding check: new — same code path as run 46089440831.

### shubhodeep1/tele-funtoken-msg-scoring
- **Runs:** 167, with 0 failures, p50 497 s and p95 2,004 s.
- **Notes:** deploy monitor 46089882583 correctly reported nothing to deploy. Most of its 208 s went on a full-history checkout before the 2 s monitor script.
- **Actions:**
  1. Fixes from Reliability 2 and 3, as above. Finding check: new — same reusable code path.

### shubhodeep1/mongo-explorer
- **Runs:** 65, with 0 failures, p50 414 s and p95 2,106 s.
- **Notes:** poller 46090187452 took 174 s, of which about 64 s was queueing.
- **Actions:**
  1. Fixes from Reliability 2 and 3. Finding check: new — same reusable code path.

### shubhodeep1/multi-user-ai-agent
- **Runs:** 64, with 0 failures, p50 402 s and p95 1,946 s.
- **Notes:** poller 46089814291 queued about 74 s.
- **Actions:**
  1. Fixes from Reliability 2 and 3. Finding check: new — same reusable code path.

### shubhodeep1/fbc_shutdown
- **Runs:** 65, with 0 failures, p50 396 s and p95 1,585 s.
- **Notes:** poller 46090213130 queued about 138 s of its 240 s.
- **Actions:**
  1. Fixes from Reliability 2 and 3. Finding check: new — same reusable code path.

### shubhodeep1/bitsafe.io
- **Runs:** 64, with 0 failures, p50 450 s and p95 2,120 s.
- **Notes:** poller 46089666262 queued about 32 s.
- **Actions:**
  1. Fixes from Reliability 2 and 3. Finding check: new — same reusable code path.

## Metrics Appendix

**Runs**

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| All | 1,699 | 1,015 | 26 | 50 | 608 | 1.53% | 244 | 3,657 |
| coding-workflows (truncated) | 1,000 | 464 | 26 | 30 | 480 | 2.6% | 57 | 4,596 |
| review_autofix | 311 | 291 | 15 | 2 | 3 | 4.8% | 2,022 | 5,319 |
| ci | 75 | 65 | 10 | 0 | 0 | 13.3% | 1,505 | 6,075 |
| orchestrate_poll | 353 | 304 | 1 | 48 | 0 | 0.28% | 616 | 1,973 |
| workflow_failure_heal | 133 | 1 | 0 | 0 | 132 | 0% | 1 | 9.4 |
| clarify / plan / implement | 96 / 81 / 79 | 10 / 7 / 6 | 0 | 0 | 86 / 74 / 73 | 0% | 1 / 1 / 1 | 617 / 550 / 759 |

**Tokens and cost telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 35 (32 / 3) |
| Prompt / completion tokens | 5,085,401 / 258,129 |
| Cache read / write tokens | 25,652,554 / 1,821,724 |
| Total tokens | 32,817,207 |
| `cache_hit_rate` (summary / coding-workflows / binance-blessings) | null / null / 0.713082 |
| `cache_hit_rate` computed over usage-available calls, same formula | 0.788 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all, 126 samples) | 8,000 / 9,042,250 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (`review_autofix`, 19 samples) | 4,556,000 / 10,105,300 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| Claude sample (unblock judge 46090113640) | $0.9198; cache write 101,386; cache read 246,859; output 2,966 |
| Failed resolver wall clock (15 runs) | 56,057 s |

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` samples | remaining 4,455 → 4,351 → 1,827; `calls_in_job` 17 / 13 / 1 |
| 429 / rate-limit events | 0 observed |
| Deterministic GraphQL retries | 5 attempts in 46089440831 |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 16 / 178,177 (about 11.1 KB per query). `review_autofix` 15 / 173,237; `orchestrate_poll` 1 / 4,940 |
| `semble_sources_total` / `static_dup_bytes` / `echo_lines_dropped` | 108 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 15 / 0 / 0 / 886,417 (about 59.1 s each) |
| `semble_fallbacks` (contract-test / runtime) | 32 (32 / 0), all in CI |
| Serena queries / fallbacks / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Reviewer findings:** 311 review runs, of which only 3 had finding lines. 2 runs (PR 10167, rounds 1 and 2) are `unavailable` with `reason=flagged_by_missing` (10 lines). PR 10100 round 2 raised 0 findings.

**Findings check**

| Status | Count |
|---|---|
| new | 5 (resolver repeat cap, empty GraphQL batch, GraphQL permanent classification, cache-rate field, resolver avoidable-rerun cost) |
| recurring | 0 |
| reopened | 1 (writer-role usage telemetry) |
| unverified | 1 (AI memory emission) |
| dropped: already fixed on this ref | 3 (Workflow Integrity push skip: template path filter and documented edge case; zero-second start failure: `startup_failure` / missing-archive classification; Semble contract-test fallback separation) |
| dropped: previously decided | 5 (runner-queue pacing: 2026-10-07-2 Speed 1; skipped-wrapper volume / comment router: plan non-goal and 2026-10-07-3 Speed 1; poller Semble unavailable: 2026-10-07-3 Rel 4; CI fail-fast after the quick lint: 2026-10-06 Speed 4; per-job PAT usage: 2026-10-06 GH API row) |

**Data gaps**
- `shubhodeep1/coding-workflows` was truncated (page limit reached, 1,000 runs).
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` is not mounted, so there are no `errors/`, `slow/` or `recent/` excerpts.
- The log archive for run 46074359581 returned HTTP 404.
- The success sample rate is 0.07.
- `log_summary` text was heavily truncated (up to about 768k characters dropped), so `AI_MEMORY_TELEMETRY`, `RESOLVER_TERMINAL` and token lines were not visible.
- Aggregated `claude_*` cost fields are not exported in `analysis_context.json`.
- `analysis/recommendation-processing-report.md` was readable.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 3 of 311 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 2.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | 1 | 0 | 0 | 0 | 0 |
| `google/gemini-3.1-flash-lite` | 1 | 0 | 0 | 0 | 0 |
| `minimax/minimax-m3` | 1 | 0 | 0 | 0 | 0 |
| `openai/gpt-6-luna` | 1 | 0 | 0 | 0 | 0 |
| `z-ai/glm-5.3-flash` | 1 | 0 | 0 | 0 | 0 |
