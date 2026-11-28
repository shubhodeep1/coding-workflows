<!-- workflow-log-analysis: window_start=2026-11-28T01:01:58Z window_end=2026-11-28T11:53:13Z run=46141944973 -->
## Executive Summary

- **Data gaps (stated first).** `coverage.repositories_truncated` lists **shubhodeep1/coding-workflows**: only part of its runs are listed. No repository failed. The run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist in this sandbox. So this report rests on `analysis_context.json` rows and the gpt-6-luna `log_summary` fields only, plus a read of the current repository code. `analysis/recommendation-processing-report.md` only covers the source docs up to `workflow-optimization-2026-10-08.md`, so none of the November findings have a recorded verdict yet.
- **One PR caused 11 of the 17 `review_autofix` failures.** PR #10054 failed 9 times at `review / codex-agent` › `Run Codex resolver, validate, stage, commit`, using 28,818 runner-seconds. Two `Internal: Review-Blocked Judge Dispatch` runs failed at the same step for another 4,260 s; the PR link for those two is an inference, from the same 9,897-byte Semble result. That is about 9.2 runner-hours in 7 h. The sweep 46140203502 still classed the PR as `retryable`. The resolver already prints a `RESOLVER_TERMINAL … exit_class=` line, but the collector never extracts it, so the exit cause is not visible here. Fix: R1 and O1, diagnostic first. Impact: up to about 9 runner-hours per stuck PR once the cause can be seen. Confidence: high on the symptom, low on the cause.
- **The scheduled closed-PR cleanup sends a GraphQL query with an empty selection set.** This happened in 6 consumer repos in the 11:24–11:28 tick (46139708256, 46139832984, 46139879508, 46139901727, 46139965508, 46139974203). In current `.github/workflows/cancel_on_pr_close.yml:231-249`, the batch loop sends `repository(...){}` when a batch holds no PR numbers. `gh_retry` then retries 5 times, because `_is_gh_permanent_failure` (`scripts/gh_helpers.sh:2071`) only matches HTTP-status text. The waste is about 30 GraphQL calls and about 96 runner-seconds per hourly tick (`cron: "23 * * * *"`), plus a false `##[error]`. Fix: R2 and G1. Confidence: high.
- **Model spend for the resolver and Claude engine is not measured.** `codex_calls=0` across all 131 parsed runs, and every failed PR #10054 resolver run except 46105939578 shows `or_calls=0`. `scripts/cost_audit.py:611` builds run telemetry from `RUN_COST_TELEMETRY_FIELDS`, which still leaves out `claude_*` (C1). Impact: puts a number on the largest failure-mode spend. Confidence: high.
- **The `ci` failure rate is overstated.** 4 of the 9 `ci` failures are zero-second mongo-explorer `tier1-ci-*` startup failures. They are filed as `ci` because of `"ci" in normalized` at `scripts/collect_workflow_logs.py:645`. The real CI rate is 5/83 (6.0%), not 10.3%. All 12 mongo-explorer failures are `tier1-*` startup failures (R3). Confidence: high.
- **Semble costs a lot of time for little context.** 29 bootstraps took 1,600 s (about 55 s each), yet they supplied about 10 KB per query, under 1% of a 0.6–2.2M-token review prompt. The 9 runs that logged an identical 9,897-byte result spent 484 s re-indexing. Reusing a cached index (S1) saves about 45 s per review run. Confidence: medium.
- **Runner concurrency is the main latency in short jobs.** Almost every log summary shows "All GitHub-hosted runners … are busy", with waits of up to 217 s (46139753922), 180 s (46140819921) and 162 s (46140677376). Run rows have no field for this queue time (F1). Confidence: medium.

## Speed Optimizations

**S1. Reuse the Semble BM25 index across runs of the same tree (critical path: every review and autofix run).**
- **Evidence:** in `review_autofix`, 25 bootstraps took 1,379,630 ms total, about 55 s each. Per run, `semble_bootstrap_ms_total` was 39,747–66,244 ms, while `SEMBLE_INSTALL_MS` was only 6,133–7,038 ms (46140145783, 46140483048). So about 85–90% of each bootstrap is the index build in `scripts/build_semble_wrapper.sh`.
- **Repeat work:** the 7 failed PR #10054 resolver runs and both judge runs (46123477240, 46133334328) each logged exactly `semble_query_bytes=9897 sources=5`. That is 484 s of bootstrap spent producing the same result again.
- **Root cause:** the pickle is written to `${RUNTIME_DIR:-${RUNNER_TEMP}}/.semble-index` (`build_semble_wrapper.sh:127-131`), and no workflow restores it. The only Semble-adjacent cache is "Pre-assemble static context" (`review_autofix.yml:3044`).
- **Exact change:** add an `actions/cache` restore and save around the wrapper build. Key it on `semble-index-${{ runner.os }}-<pinned semble version>-<git tree SHA of HEAD>`, so any source change yields a new key. Skip the build when `SEMBLE_INDEX_PATH` already exists and loads. Keep the existing fail-soft path for any load error.
- **Estimated saving:** about 45 s per review or judge run that re-reviews an unchanged tree; 484 s in this window for PR #10054 alone.
- **Risk:** low to medium. The cache adds no new runner job. A stale index cannot be served because the key includes the tree SHA. Rollback: remove the cache step.
- Finding check: new — the processing report (line 359) records "broader Semble/bootstrap reuse work has not [landed]" as an open item, not a decision. Current `build_semble_wrapper.sh` still always rebuilds.

**S2. Measure, then split, the `CI quick` lane-0 module (micro to moderate: about 6.5 of about 11 min).**
- **Evidence:** in 46139368609, `CI_SUBTEST_HEARTBEAT running=tests/test_security_audit_workflow_contract.py` reached `elapsed_ms=390071` before the run was superseded. The successful 46140267927 `static-checks` ran from 11:31:36 to 11:42:22. `ci.yml:728-729` documents this module as "about six minutes observed" and runs it alone in lane 0.
- **Root cause:** one monolithic contract module sets the step's wall clock. The other 22 modules finish in about 2 min (`ci.yml:734-735`).
- **Exact change (diagnostic first):** in lane 0, wrap the module with per-test timing: `python3 -m pytest -q -p no:cacheprovider --durations=25 tests/test_security_audit_workflow_contract.py`. Do this only if a local check shows that pytest collects the same `unittest` cases as `main()`. Otherwise add a `CI_SUBTEST_SLOW case=<name> elapsed_ms=` line from the module's own runner. Then split the slowest test classes across two lanes.
- **Estimated saving:** 2–3 min off `CI quick` once split (an inference from the 6.5 vs 2 min lane imbalance).
- **Risk:** low for the diagnostic. Medium for the split, because of shared temp paths; rerun the concurrency grep noted at `ci.yml:743-745`.
- Finding check: new — no prior verdict in the processing report. `ci.yml:734-736` says the lane-0 timing was "an estimate, not a measurement".

## Cost Optimizations

**C1. Export Claude-engine and OpenCode resolver spend into the per-run telemetry.**
- **Evidence:** every listed run in the window has `codex_calls=0` and `codex_tokens_used=0`. 8 of the 9 failed PR #10054 resolver runs show `or_calls=0`; the 9th, 46105939578, used 8.09M OpenRouter tokens, presumably in the review stage before the resolver failed. Those 8 runs still ran 1,177–7,017 s. Fleet OpenRouter spend is visible: 56,274,338 total tokens over 85 calls. Resolver and Claude spend is not.
- **Root cause:** `cost_audit.py:611` builds `telemetry = {field: parsed[field] for field in RUN_COST_TELEMETRY_FIELDS}`. The tuple (`:209-245`) has no `claude_*` keys, even though `parse_log` fills them and the run-level aggregation (`:1092-1095`) sums them. The resolver's OpenCode calls emit no line that `OPENROUTER_RE` can parse.
- **Exact change:**
  1. Add `"claude_calls", "claude_input_tokens", "claude_output_tokens", "claude_cache_write_tokens", "claude_cache_read_tokens", "claude_cost_usd"` to `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS`. The change is additive.
  2. After each resolver attempt, emit one `INFO: openrouter usage phase=resolver …` line. Use `usage_available=false` when OpenCode gives no counts.
- **Estimated savings:** indirect. It makes the single largest failure loop (about 9 runner-hours in this window) cost-attributable, which is a precondition for capping it.
- **Quality risk:** none. No model, prompt or reasoning level changes.
- Finding check: reopened — the processing report (line 2078) rejected "per-phase model counters" as "Diagnostic experiments … that need a quality baseline before any change". This change alters no model or prompt behaviour, so it needs no quality baseline. The new evidence is the 8 zero-token resolver failures above.

**Semble and Serena assessment (commentary, no separate item).**
- Semble: 34 queries returned 349,019 bytes (about 10.3 KB each, 5.3 sources per query) with `static_dup_bytes_total=0` and `semble_runtime_fallbacks=0`. Semble is not adding noise. Its context value is small next to per-run OpenRouter prompts of 0.35–2.16M tokens, and its time cost of about 55 s per bootstrap is large, so S1 is the lever.
- Serena: disabled (`SERENA_ENABLED: false` in every log summary that shows config) and has zero telemetry, so there is nothing to assess.

## Reliability Improvements

**R1. Make the resolver's exit cause visible per run, then bound repeated resolver failures on one PR (largest rerun reduction).**
- **Failure evidence:** the 9 PR #10054 failures at `Run Codex resolver, validate, stage, commit` were 46105939578, 46106656725, 46117235686, 46121705972, 46124187996, 46129524596, 46132461451, 46135169455 and 46138536995. Two `judge / codex-agent` failures at the same step followed (46123477240, 46133334328). The sweep 46140203502 logged `skip reason=retryable (pr=10054)`.
- **Root cause category:** unknown resolver failure class plus missing observability. The script already prints `RESOLVER_TERMINAL run_id= … exit_class=timeout|no_progress|markers|fingerprint|scope|commit|provenance|other` (`scripts/review_conflict_resolve.sh:532-533`). No `scripts/*.py` parses it (grep: no matches), so neither the collector nor this report can tell whether the loop is `timeout` (the 7,017 s run suggests it) or a deterministic `fingerprint` or `scope` failure. Fingerprint-tier relaxation (`:2404-2410`) only applies to integration-sync PRs. On a normal PR the threshold `RESOLVER_ESCAPE_THRESHOLD_N=5` never changes behaviour (inference).
- **Exact fix (diagnostic logging):**
  1. `scripts/review_conflict_resolve.sh:532`: append ` fp_tier=%s integration_sync=%s` to the `RESOLVER_TERMINAL` printf. Fill them from `${RESOLVER_FP_VERIFICATION_TIER}` (enum `strict|ratio|count_only|warn_only`) and `${IS_INTEGRATION_SYNC:-false}` (`true|false`). Update `tests/test_review_conflict_resolve_terminal_record.py`.
  2. `scripts/collect_workflow_logs.py`: next to `REVIEWER_FINDINGS_LINE` (`:140`), add a parser and attach `run["resolver_terminal"]` in the same pass as `:1797-1852`:
  ```python
  RESOLVER_TERMINAL_LINE = re.compile(
      r"^(?:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z )?RESOLVER_TERMINAL "
      r"run_id=(?:\d+|none) run_attempt=(?:\d+|none) pr=(?P<pr>\d+|none) "
      r"attempts=(?P<attempts>\d{1,3}) attempt_ms=(?P<attempt_ms>[\d,]{1,200}|none) "
      r"exit_class=(?P<exit_class>[a-z_]{1,20}) rc=(?P<rc>\d{1,3}) "
      r"fp_violations=(?P<fp>\d{1,7}) markers=(?P<markers>\d{1,7}|unknown)(?: [a-z_]+=[a-z0-9_]{1,20})*$"
  )
  ```
  3. Once one window shows the dominant `exit_class` for repeat PRs, add the breaker: on the Nth consecutive same-head failure with the same `exit_class`, label `ai:resolver-escalated`, which the reporter step already honours (`review_autofix.yml:6357`).
- **Expected impact:** step 3 alone would have stopped PR #10054 after about 3 attempts, saving about 6 runner-hours (inference). Steps 1–2 cost nothing at runtime.
- **Rollback / fail-open:** the new fields are additive tokens on an existing line, and the collector parser fails open to no field.
- Finding check: new — the `RESOLVER_TERMINAL` emitter came from wfopt-2026-10-07-2. The processing report (line 2153) records the emitter, but no `scripts/*.py` parses it on this ref.

**R2. Skip the PR-state GraphQL lookup when a batch has no PR numbers.**
- **Failure evidence:** on the 11:24–11:28 tick, 6 consumer runs each failed 5 of 5 `gh api graphql` attempts with `Field must have selections (field 'repository' …)`. They then logged `could not resolve PR-state batch 1` and `found no cancellable …`:
  - fbc_shutdown 46139708256
  - bitsafe.io 46139832984
  - multi-user-ai-agent 46139879508
  - mongo-explorer 46139901727
  - binance-blessings 46139965508
  - fun-token-multi-chain 46139974203
- **Root cause category:** a request-construction bug.
  - `pr-numbers.txt` is empty whenever no active run has PR linkage (`:308-312`).
  - The batch loop still processes a batch file. `query` then becomes `…repository(owner:$owner,name:$name){}}` because no alias is appended (`:235-243`).
  - Inference: the runner image is ubuntu-26.04, which ships Rust uutils coreutils (the image is cited in `ci.yml:784-786`). On that image `split` emits one empty chunk for empty input, which GNU split does not.
  - Inference on impact: with no PR numbers there were no PR-linked runs to cancel, so "found no cancellable" is the correct outcome. The cost is noise and API calls, not missed cancellations.
- **Exact fix** in `.github/workflows/cancel_on_pr_close.yml` (no new log prefix):
  ```bash
              printf '{}\n' > "${output_file}"
              if ! grep -qE '^[0-9]+$' "${numbers_file}" 2>/dev/null; then
                echo "Scheduled close cleanup: no PR-linked active runs; skipping PR-state lookup."
                return 0
              fi
              ...
                local alias_count=0
                while IFS= read -r pr_number || [ -n "${pr_number}" ]; do
                  case "${pr_number}" in ''|*[!0-9]*) continue ;; esac
                  query+="pr_${pr_number}:pullRequest(number:${pr_number}){number state}"
                  alias_count=$((alias_count + 1))
                done < "${batch_file_part}"
                query+='}}'
                [ "${alias_count}" -gt 0 ] || continue
  ```
  The `|| [ -n … ]` also handles a final line without a trailing newline. Consumers pin `6fe11eeda300…`, so the fix reaches them through the next stable release.
- **Expected impact:** removes 6 of 6 false `##[error]` lines per consumer tick, about 30 GraphQL calls per hour, and about 15–17 s per run.
- **Rollback:** the guard is fail-open. A non-empty file follows the old path unchanged.
- Finding check: new — reported in `workflow-optimization-2026-11-24/-26/-27/-28.md`, but none of these has a processing-report verdict yet. Current code at `:231-249` still sends the empty query.

**R3. Stop misfiling `tier1-*` workflows as `ci`, and fix or disable mongo-explorer's six `tier1-*.yml` files.**
- **Failure evidence:** mongo-explorer has 12 zero-second failures (13.3% repo failure rate), all with `job_name: null`, in two bursts:
  - 06:35:42–06:35:46: 46117332752, 46117333483, 46117334239, 46117334935, 46117335611, 46117336324
  - 06:41:47–06:41:50: 46117796836, 46117797686, 46117798343, 46117799251, 46117799966, 46117800912

  Of these, `tier1-ci-doctor.yml` and `tier1-ci-coach.yml` land in family `ci`.
- **Root cause category:** a workflow-definition or startup failure in consumer code (inference, as in earlier reports), plus a collector classification bug: `if "ci" in normalized: return "ci"` at `collect_workflow_logs.py:645`.
- **Exact fix:**
  1. Collector: before `:645`, return the path-stem-derived family when `Path(workflow_path).stem` starts with `tier1-`. Add a case to `tests/test_collect_workflow_logs.py`.
  2. Consumer: apply to mongo-explorer the change that cleared fbc_shutdown. fbc_shutdown shows 0 failures in this window, compared with the tier1 bursts in 11-23.
- **Expected impact:** reported `ci` failure rate goes from 10.3% to 6.0%. Fleet failure count drops by 12 of 37 (32%) once the consumer files are fixed.
- **Rollback:** a one-line collector revert.
- Finding check: new — raised in 11-21, 11-23 and 11-24, with no processing-report verdict. Line `:645` is unchanged on this ref.

**R4. Make `REVIEWER_FINDINGS … reason=flagged_by_missing` say why (diagnostic).**
- **Evidence:** all three counted-attempt runs with reviewer lines for PR #10054, #10246 and #10247 (46105939578, 46106089981, 46107422410) were `flagged_by_missing` for all 5 reviewers. So was PR #10167 round 3 (46107988870). In total: 20 unavailable lines across 4 runs, against 2 counted runs (PR 10163, PR 10209), both with `raised=0`.
- **Root cause category:** an attribution or schema gap. The single early `return` at `scripts/review_findings_record.py:744-746` fires on the first issue with an empty `flagged_by` or an unmatched name, and records no counts.
- **Exact fix:**
  1. Before the early return, count the non-folded issues with empty `matched` (`empty`) and with `has_unmatched` (`unmatched`). Emit `outcome=unavailable reason=flagged_by_missing issues=<n> empty=<n> unmatched=<n>` (integers only).
  2. Extend `REVIEWER_FINDINGS_LINE` (`collect_workflow_logs.py:145`), which is `$`-anchored, to `|outcome=unavailable reason=([a-z_]{1,64})(?: issues=([0-9]{1,5}) empty=([0-9]{1,5}) unmatched=([0-9]{1,5}))?)$`.
- **Expected impact:** separates "the consolidator omits `flagged_by`" (`empty` > 0) from "reviewer slug mismatch" (`unmatched` > 0). That decides the fix for 100% of runs that currently emit unavailable.
- **Rollback / fail-open:** the fields are optional and the old lines still parse.
- Finding check: new — raised as a diagnostic in 11-27 and 11-27-2, with no processing-report verdict. The early return is unchanged.

**Break-glass, context-budget and fallback summary.** `break_glass_count=0` and `context_budget_warn_count=0` in every run, so there is no rubric or prompt-size pressure signal. `SEMBLE_FALLBACK`: all 16 are `semble_contract_test_fallbacks` from CI test runs (4 in 46106008312, 4 in 46122704074, 8 in 46139365190). There are 0 runtime fallbacks and 0 failed bootstraps, which is healthy and not a masked rollout. Serena has no probes and no fallbacks (disabled).

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` line is visible in any `log_summary`. Most summaries explicitly say the logs were truncated (up to 1.88M characters, in 46139305756), and the deep-dive log folder is not mounted. So retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` and push retries **cannot be measured this window**. This is a gap, not evidence that memory is healthy or unhealthy.

**M1. Summarise AI-memory telemetry in the collector so truncation cannot hide it.**
- **Evidence:** `scripts/collect_workflow_logs.py` imports `ai_memory_lib` (`:30-38`), but no code extracts `AI_MEMORY_TELEMETRY` lines into run rows (grep: no parser).
- **Exact change:** while scanning the logs, as for `REVIEWER_FINDINGS` (`:1797-1852`), add `run["ai_memory"] = {"retrieve": n, "retrieve_hits": n, "retrieve_tokens_sum": n, "fail_open": n, "disabled": n, "keyword_method": {"llm": n, "plain": n, "none": n}, "push_retries_max": n}`. Parse each line's JSON and fail open on any error.
- **Expected impact:** memory hit rate and the fail-open rate become measurable in every window, with no runtime change.
- Finding check: new — the processing report (line 985) lists "AI-memory per-operation duration/retry/selection summaries" as "explicitly deferred". The cause, no collector summary, is still present.

## GH API Call Audit

- **Volumes seen** (from `GH_PAT_BUDGET phase=end` in log summaries):

  | Run | Job | calls_in_job | paginated_in_job | remaining |
  |---|---|---|---|---|
  | 46140483048 | `review_autofix` codex-agent | 48 | 8 | 3,242 |
  | 46140145783 | `review_autofix` codex-agent | 48 | 6 | 3,541 |
  | 46139526477 | `review_autofix` codex-agent | 40 | 21 | 3,694 |
  | 46139305756 | `review_autofix` codex-agent | 38 | 16 | 3,254 |
  | 46140203502 | sweep | 85 | 21 | 4,422 |
  | 46140356503 | heal intake | 24 | 3 | 4,753 |

  No HTTP 429 or secondary rate-limit line appears anywhere. The lowest `remaining` is 3,242.
- **PR read cache:** `GH_PR_READ_CACHE` hit rates are low: 1/4 (46140483048), 1/6 (46140145783, 46139526477) and 2/6 (46139305756). With only about 4–6 reads per job, the absolute saving available is under 5 calls per job. No change is recommended.
- **Loop-lint:** `GH_LOOP_CALL_LINT warnings=3 files=2` in 46141364421 (PR #9902) flags `gh_retry _safe_gh_jq` on `commits/$` and `actions/workf…`, and `gh issue close` inside loops. These are warning-only. Review them in that PR rather than here.

**G1. Classify GraphQL validation errors as permanent in `gh_retry`.**
- **Evidence:** the 6 consumer runs listed in R2 each made 5 attempts with 1/2/4/8/16 s backoff on a deterministic `Field must have selections` error. That is 30 wasted calls per hourly tick (`workflow-templates/ai-cancel-on-pr-close.yml:8`, `cron: "23 * * * *"`), about 720 a day across the consumer fleet.
- **Root cause:** `_is_gh_permanent_failure` (`scripts/gh_helpers.sh:2071`) matches HTTP 400/401/404/422 text only. `gh api graphql` reports schema-validation errors without an HTTP status, so they are retried as transient.
- **Exact change:** add `Field must have selections|is declared by anonymous query but not used|Parse error on ".*" \(|Field '[A-Za-z_]+' doesn't exist on type` to that regex. Rate-limit and transient detection are untouched.
- **Estimated reduction:** 4 of 5 calls per malformed-query occurrence, about 576 calls a day today, and future query bugs fail in 1 attempt.
- **Rate-limit risk:** it reduces API use; there is no added call.
- Finding check: new — no processing-report verdict on GraphQL error classification. The regex at `:2071` is unchanged.

## Prompt Cache & Memory System

- **Fleet cache:** the fleet `cache_hit_rate` is **null**. Computed from token totals it is 42,710,522 / (42,710,522 + 9,136,303 + 3,763,250) = **76.8%** (inference from the same formula as `compute_cache_hit_rate`).
- **Per-run cache hit rates:**
  - High: 0.906 (46106089981), 0.888 (46107422410), 0.843 (46105939578), 0.794 (46106215406), 0.738 (46115428538).
  - Low: 0.660 (46139526477, PR 10163, `cache_write=950,936`), 0.576 (46106122797), 0.564 (46139305756, PR 10110, `cache_write=539,002`).
  - The two PR 10209 runs wrote 0 cache tokens and read 1.33–1.55M, which is stable-prefix behaviour working as intended.
- **Fragmentation:** the low-hit runs pair high `cache_write` with high `prompt_tokens` (2.16M in 46139526477). Inference: a large uncached volatile suffix such as the diff or resolver context. Without logs, the prefix-variance source cannot be confirmed. `CONTEXT_BUDGET_WARN` is 0, so prompt growth is not tripping the 0.7 ratio guard.

**P1. Report a usage-available cache hit rate alongside the strict one.**
- **Evidence:** 1 of 85 OpenRouter calls had no usage data (run 46107988870, `or_usage_unavailable_calls=1`). `compute_cache_hit_rate` returns `None` whenever `or_usage_unavailable_calls > 0` (`scripts/cost_audit.py:455-456`), which nulls the fleet, per-repo and `review_autofix` rates.
- **Exact change:** add a new field, `cache_hit_rate_usage_available`, computed over usage-available calls only, plus `cache_hit_rate_coverage = available / (available + unavailable)`. Keep `cache_hit_rate` unchanged for backward compatibility (§10).
- **Impact:** the fleet and family cache metrics become trackable each window (76.8% now). No runtime change.
- Finding check: new — the processing report (line 1048) records only the available/unavailable split, not a coverage-qualified rate. Line `:455` still nulls the whole aggregate.

## Orchestrator Health

- **Poller and cleanup indicators:**
  - Poller: 237 runs, 0 failures, 34 cancelled (14.3%). For example, 46140688369 was `cancelled_before_first_step` after 106 s, which is consistent with concurrency-group coalescing (§26). Poll runs take 682–1,158 s, with logs truncated by about 1.1M characters (46138595931, 46139478361, 46140819921).
  - Merge lease: `MERGE_LEASE_QUEUE base=main holder=9902 held_s=147 waiters=15` (46140172756). PR #10227 was deferred 3 times with `reason=lease_wait` and `MERGE_BASE_FRESHNESS … behind_by=16→18` on shared `ci.yml` and `tests/test_ci_poll_test_sharding.py` (46139950247, 46140094365, 46140172756).
  - Integration PR #9396 shows "4/6 sub-issues on #9394 still unchecked" (46139368530, 46140267754).
  - Auto-heal: `AUTO_HEAL_RECONCILE … in_progress=20 skipped_budget=21` (tele-funtoken 46139454669), with 17 of 20 at `human_review`.
- **Indicators to track:** consecutive same-head resolver failures per PR (R1); `MERGE_LEASE_QUEUE waiters`; `skipped_budget` in reconcile; `retryable` skips in the backfill for PRs with 3 or more failures.

**O1. Log the failure class on the backfill's `retryable` skip.**
- **Evidence:** sweep 46140203502 logged `skip reason=retryable` for pr=10054 after 9 resolver failures. The skip row only prints `pr=` and `reason=` (`scripts/workflow_failure_heal_non_retryable_backfill.sh:312, 344`), even though `failure_reason` is already in the selector result (`scripts/workflow_failure_heal.py:6131-6133`).
- **Exact change:** in the selector's skip print at `:312`, add a fourth tab column, `tok(row.get("failure_reason"), r"[a-z][a-z0-9_]{0,79}") or "none"`. At `:344`, log `pr=${pr} outcome=skip reason=${head} failure=${failure}`.
- **Impact:** an operator or analyst sees which retryable class (for example a timeout) keeps a PR in the retry loop. This feeds R1's breaker decision. No API calls are added.
- Finding check: new — no processing-report verdict. `:344` still omits the failure class.

## Pipeline Flow Bottlenecks

1. **Retry overhead (largest):** review/autofix resolver loops. PR #10054 accounts for 33,078 runner-seconds of failures (R1, O1). The `review_autofix` family has p50 680 s and p95 3,760 s, and slow successful runs reach 7,957 s (46106089981) and 6,397 s (46107422410).
2. **Compute on the critical path:**
   - Semble bootstrap, about 55 s per run (S1).
   - The codex-agent review step: about 13–20 min in 46140483048, 46140145783 and 46139526477.
   - CI shards: `CI` p50 687 s and p95 3,599 s. Shard 9 stalled about 4 min 14 s in 46139365190.
   - `CI quick` lane 0: about 6.5 min (S2).
3. **Queueing:** runner-busy waits dominate short jobs. Examples: 217 s of 231 s (46139753922), 162 s of 177 s (46140677376), 107 s of 125 s (46139412440), and 135 s of 294 s (46140094365). Seven consumer cancel-on-close crons were created within 11:24–11:28.
4. **Merge and conflict overhead:** lease contention with 15 waiters, plus base drift on shared CI files for PR #10227 (Orchestrator Health).

**F1. Record runner queue wait as a run-row field (diagnostic).**
- **Evidence:** run `duration_seconds` is `updated_at − run_started_at` (`collect_workflow_logs.py:1179-1183`), so job-level queue time is hidden inside the duration. Queue wait currently appears only in free-text `log_summary`.
- **Exact change:** while the collector scans downloaded logs, parse the runner system lines' timestamps: the first "Waiting for a runner to pick up this job" or "All GitHub-hosted runners … are busy", and the next "Job is about to start running". Store `runner_wait_seconds_max` and `runner_wait_seconds_sum` per run, with no new API calls. Aggregate p50 and p95 per family.
- **Impact:** puts a number on the §26 concurrency tax, so job-consolidation work can be ranked by measured queue time.
- Finding check: new — no processing-report verdict. No queue field exists in `_build_run_row` (`:1195-1214`).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Bottlenecks:** `review_autofix` p95 of about 3,760 s, Semble bootstrap, CI shards, runner queueing. **Failure modes:** PR #10054 resolver (9), judge (2), `Resolve trusted review support commit` (3: 46117986042, 46118488772, 46118739550, within 06:44–06:54), CI `port P1` on PR #10070 (2; the PR later merged), system-python shards (2), unblock-judge (1). **Cost:** 85 OpenRouter calls, 56.3M tokens, about 76.8% cache hit rate; resolver spend unmeasured. Truncated: only part of its runs are listed.
- **Actions:**
  1. R1: parse `RESOLVER_TERMINAL`, then add a same-head failure breaker.
     Finding check: new — `RESOLVER_TERMINAL` is not parsed by any `scripts/*.py` on this ref.
  2. S1: cache the Semble index by tree SHA.
     Finding check: new — processing report line 359 lists Semble reuse as not landed.
  3. C1: export `claude_*` and resolver usage into run telemetry.
     Finding check: reopened — line 2078 rejection answered: there is no model change and 8 resolver failures show zero tokens.

### shubhodeep1/tele-funtoken-msg-scoring
- **Failure modes:** 3 `AI Review` failures at `review / gate` › `Evaluate review gate` (46117930882, 46118057133, 46118120949, 06:43–06:46). They coincide with the coding-workflows gate failures at 06:44–06:54; inference: a shared upstream condition. There are no excerpts to confirm the cause. **Bottleneck:** runner queueing (217 s of 231 s in 46139753922). **Cost:** none measured.
- **Actions:**
  1. F1: record runner wait per run.
     Finding check: new — no queue field in `_build_run_row`.
  2. R2: pick up the cancel-on-close fix in the next stable release (this repo's tick, 46139753922, did not error, but it shares the code).
     Finding check: new — `cancel_on_pr_close.yml:231-249` is unchanged.
  
  No third action is warranted: the gate-failure cause cannot be seen without the deep-dive logs.

### shubhodeep1/binance-blessings
- **Failure modes:** `update / update-wrappers` › `Verify signed sync manifest` (46106093022), and `validate` › `Enforce validation outcome` (46109959258, tracking:788). Both have `log_download_status: not_selected`. Cancel-on-close GraphQL error in 46139965508. **Bottleneck:** orchestrate poll up to 2,775 s (46094262437).
- **Actions:**
  1. R2: empty-batch guard (released via stable).
     Finding check: new — the error appears in 46139965508 and the code is unchanged.
  2. G1: GraphQL validation errors classed as permanent.
     Finding check: new — `gh_helpers.sh:2071` is unchanged.
  
  No third action: the update and validate failures have no excerpts. Next collection step: select failed runs for deep-dive.

### shubhodeep1/fun-token-multi-chain
- **Failure modes:** `CI Reproducible Validation` › `Enforce compile and test success` (46117709496, 110 s, no excerpt). Cancel-on-close GraphQL error in 46139974203. **Bottleneck:** orchestrate poll about 2,645 s (46117750288).
- **Actions:**
  1. R2.
     Finding check: new — the error appears in 46139974203.
  2. G1.
     Finding check: new — the regex is unchanged.

### shubhodeep1/mongo-explorer
- **Failure modes:** 12 of 12 failures are zero-second `tier1-*` startup failures (13.3% failure rate). There is also a cancel-on-close GraphQL error (46139901727).
- **Actions:**
  1. R3 (consumer): fix or disable the six `tier1-*.yml` files, as done in fbc_shutdown.
     Finding check: new — 12 failures this window, with no processing-report verdict.
  2. R3 (collector): stop filing `tier1-ci-*` as `ci`.
     Finding check: new — `collect_workflow_logs.py:645` is unchanged.
  3. R2.
     Finding check: new — the error appears in 46139901727.

### shubhodeep1/multi-user-ai-agent
- 0 failures in 50 runs. Cancel-on-close GraphQL error in 46139879508; poller 2,650 s (46117702142).
- **Actions:**
  1. R2.
     Finding check: new — the error appears in 46139879508.
  2. G1.
     Finding check: new — the regex is unchanged.

### shubhodeep1/fbc_shutdown
- 0 failures in 51 runs. The `tier1-*` startup failures seen in earlier reports are gone. Cancel-on-close GraphQL error in 46139708256.
- **Actions:**
  1. R2.
     Finding check: new — the error appears in 46139708256.
  2. G1.
     Finding check: new — the regex is unchanged.

### shubhodeep1/bitsafe.io
- 0 failures in 51 runs. Cancel-on-close GraphQL error in 46139832984.
- **Actions:**
  1. R2.
     Finding check: new — the error appears in 46139832984.
  2. G1.
     Finding check: new — the regex is unchanged.

## Metrics Appendix

**Run counts and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Fleet | 1,940 | 1,071 | 37 | 51 | 781 | 1.9% | 73 | 2,340 |
| coding-workflows (truncated) | 998 | 515 | 19 | 20 | 444 | 1.9% | 63 | 3,146 |
| tele-funtoken-msg-scoring | 320 | 161 | 3 | 15 | 141 | 0.9% | 22 | 1,382 |
| binance-blessings | 281 | 125 | 2 | 6 | 148 | 0.7% | 9 | 1,438 |
| fun-token-multi-chain | 99 | 66 | 1 | 3 | 29 | 1.0% | 85 | 1,498 |
| mongo-explorer | 90 | 58 | 12 | 1 | 19 | 13.3% | 110.5 | 1,115 |
| bitsafe.io | 51 | 49 | 0 | 2 | 0 | 0% | 280 | 1,512 |
| fbc_shutdown | 51 | 50 | 0 | 1 | 0 | 0% | 164 | 1,515 |
| multi-user-ai-agent | 50 | 47 | 0 | 3 | 0 | 0% | 223 | 1,508 |
| `review_autofix` family | 373 | 350 | 17 | 5 | 1 | 4.6% | 680 | 3,760 |
| `ci` family (as filed) | 87 | 75 | 9 | 3 | 0 | 10.3% (6.0% excl. tier1) | 687 | 3,599 |
| `orchestrate_poll` | 237 | 203 | 0 | 34 | 0 | 0% | 479 | 2,133 |

**Failure-point concentration**

| Workflow › job › step | Failures | Runner-s |
|---|---|---|
| review / codex-agent › Run Codex resolver… (PR #10054) | 9 | 28,818 |
| judge / codex-agent › Run Codex resolver… | 2 | 4,260 |
| mongo-explorer `tier1-*` (no job) | 12 | 0 |
| review / gate › Resolve trusted review support commit | 3 | 1,005 |
| review / gate › Evaluate review gate (tele-funtoken) | 3 | 1,131 |
| tests-release-and-log-analysis › Single-issue security pass tests (port P1) | 2 | 8,900 |
| tests-system-python shard (28), (13) | 2 | 5,196 |
| Other (update-wrappers, validate, unblock-judge, fun-token CI) | 4 | 2,499 |

**Tokens and cache**

| Metric | Value |
|---|---|
| OpenRouter calls (usage available / unavailable) | 85 (84 / 1) |
| or_prompt / completion / cache_read / cache_write tokens | 9,136,303 / 665,220 / 42,710,522 / 3,763,250 |
| or_total_tokens | 56,274,338 |
| `cache_hit_rate` (fleet, strict) | null (1 unavailable call) |
| Cache hit rate, computed from totals | 76.8% |
| codex_calls / codex_tokens_used | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (fleet, 128 samples) | 92,000 / 6,849,600 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (`review_autofix`, 44) | 588,500 / 7,552,800 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**GH API**

| Source | Value |
|---|---|
| codex-agent `calls_in_job` (4 runs) | 38–48; min `remaining` 3,242 |
| Sweep 46140203502 | 85 calls, 21 paginated |
| HTTP 429 / secondary rate limit | 0 seen |
| Cancel-on-close doomed GraphQL attempts | 30 per hourly tick (6 repos × 5) |
| `GH_PR_READ_CACHE` hits / misses | 1/3, 1/5, 1/5, 2/4 |

**Semble and Serena**

| Metric | Value |
|---|---|
| Semble query calls / bytes | 34 / 349,019 (about 10.3 KB per query) |
| Semble sources / static_dup_bytes | 179 / 0 |
| Semble bootstraps (ok / failed / unused) | 29 (29 / 0 / 0), 1,600,380 ms total, about 55.2 s avg |
| Semble fallbacks (contract-test / runtime) | 16 (16 / 0) |
| Serena query calls / response bytes / tool calls / ms | 0 / 0 / 0 / 0 |
| Serena fallbacks | 0 |

**Per-target MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | Notes |
|---|---|---|---|---|
| Serena (all workflows) | 0 | 0 | 0 | `SERENA_ENABLED: false` in log-summary configs |
| Semble (review_autofix) | n/a | n/a | n/a | 25 bootstraps ok, 28 queries |
| Semble (orchestrate_poll) | n/a | n/a | n/a | 4 bootstraps ok, 6 queries |

Other MCP servers observed: none.

**Reviewer findings:** 373 review runs; 7 runs had lines. 20 unavailable lines across 4 runs, all `flagged_by_missing`. 2 counted runs (PR 10163 round 1, PR 10209 round 0), both with `raised=0`.

**Findings check**

| Status | Count |
|---|---|
| new | 11 (S1, S2, R1, R2, R3, R4, M1, G1, P1, O1, F1) |
| recurring | 0 |
| reopened | 1 (C1) |
| unverified | 0 |
| dropped: already fixed on this ref | 1 (close-cleanup unlinked-run listing; working in 46139950247, processing report line 1570) |
| dropped: previously decided | 1 (`Resolve trusted review support commit` cause lines; `already-implemented` per the prior verdict cited in 11-22; the 3 failures are fail-closed by design, and no evidence here shows a missing cause line) |

**Data gaps**
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` is missing, so no `summary.json`, `errors/`, `slow/` or `recent/` files were read.
- coding-workflows is truncated (`page_limit_reached: true`).
- `analysis/recommendation-processing-report.md` was readable, but covers source docs only through 2026-10-08, so November findings have no verdicts.
- `success_sample_rate` is 0.07.
- `AI_MEMORY_TELEMETRY`, `RESOLVER_TERMINAL` and token lines are not visible in the truncated `log_summary` text.
- No run IDs are available for the PR #10054 judge runs' PR link (inferred from identical Semble output).
- Next collection step: mount the run-logs folder and deep-dive the failed runs that were `not_selected` (46117235686, 46116464513, 46112540965, 46106093022, 46109959258).

Actions taken: read-only analysis. I read `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/ci.yml`, `scripts/collect_workflow_logs.py`, `scripts/cost_audit.py`, `scripts/review_findings_record.py`, `scripts/review_conflict_resolve.sh`, `scripts/build_semble_wrapper.sh`, `scripts/gh_helpers.sh`, `scripts/workflow_failure_heal*.{py,sh}` and `analysis/*.md`. No files were changed.

Assumptions: two causes are inferences and are labelled as such: the uutils `split` behaviour behind the empty GraphQL batch, and the PR #10054 link for the judge runs.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 7 of 373 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 4.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | 3 | 0 | 0 | 0 | 0 |
| `google/gemini-3.1-flash-lite` | 3 | 0 | 0 | 0 | 0 |
| `minimax/minimax-m3` | 3 | 0 | 0 | 0 | 0 |
| `openai/gpt-6-luna` | 1 | 0 | 0 | 0 | 0 |
| `z-ai/glm-5.3-flash` | 3 | 0 | 0 | 0 | 0 |
