<!-- workflow-log-analysis: window_start=2026-11-27T02:32:55Z window_end=2026-11-27T13:58:18Z run=46011405479 -->
## Executive Summary

- **PR #10100 has joined #9442 in a resolver loop. Six review runs failed at the same step this window, about 6.2 runner-hours.** All six failed at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`:
  - PR #10100: 45960909393 (1,771 s), 45965322875 (3,604 s), 45979232511 (4,433 s) and 45995474473 (5,810 s).
  - PR #9442: 45953304703 (1,807 s) and 45970740796 (4,764 s).
  - Three of the four #10100 runs logged an identical Semble query (`semble_query_bytes=10290`, 3 sources). This suggests the same inputs on each retry (inference).
  - The retry-state gap is still in the code: `build_resolver_retry_state_artifact` returns `ok: False` when there are no fingerprint failures (`scripts/review_conflict_resolve.sh:3689`). So timeouts and no-progress exits never escalate.
  - Impact: about 6 runner-hours per 8.5-hour window. Confidence: medium, because the step logs could not be read.
- **The run-logs bundle is still not mounted on the Claude log-analysis path. This is the third window in a row.** This run could not read `/home/runner/work/_temp/workflow-log-output` ("Directory does not exist"). `.github/workflows/workflow-log-analysis.yml:1719-1720` still calls `claude_run_selected LOG_ANALYSIS` without `AI_ENGINE_INCLUDE_PATHS`. Impact: every failure finding below rests on `analysis_context.json` and the repository code only. Confidence: high.
- **Claude-engine spend is not in the per-run telemetry at all.** `parse_log` computes `claude_calls`, the `claude_*_tokens` counters and `claude_cost_usd`. `RUN_COST_TELEMETRY_FIELDS` (`scripts/cost_audit.py:209-245`) leaves them out, so every resolver failure shows `or_calls=0 codex_calls=0`. Impact: the resolver loop and other Claude-engine runs (PR #10167 carries `ai:engine-claude`) have unknown model cost. Confidence: high.
- **CI failed 13 of 76 times (17.1%), and its wall clock is mostly queueing.** CI p50 is 901 s and p95 is 7,186 s. Every test job is capped at 20–30 min and has no `needs:` dependency, yet failed CI runs took 3,826–8,184 s. The 32-shard `tests-system-python` matrix (`ci.yml:4507`) has no `max-parallel`. Every log summary in the window shows `All GitHub-hosted runners with label [ubuntu-latest] are busy`. Confidence: medium, and the impact estimate is an inference.
- **Reviewer attribution failed in every run that emitted it.** 3 of 3 runs with `REVIEWER_FINDINGS` lines were `outcome=unavailable reason=flagged_by_missing` for all 5 reviewers: 45976870422 (PR 10185 round 4), 45980026001 (PR 10203 round 3) and 45982155581 (PR 9902 round 1). The early return at `scripts/review_findings_record.py:744-746` still gives no counts. Confidence: high on the symptom, low on the cause.
- **Data gaps:**
  - **Truncated repository:** `shubhodeep1/coding-workflows` (the page limit was reached, so only part of its runs are listed). No repository failed.
  - **Run logs not mounted** (see bullet 2).
  - **Successful runs were sampled** at `success_sample_rate=0.07`.

## Speed Optimizations

1. **Stop the resolver loops on #10100 and #9442 (critical path; the largest per-PR latency).**
   - **Evidence:**
     - Six failures at `Run Codex resolver, validate, stage, commit` (run IDs above), totalling 22,189 s of run wall time.
     - #10100 failed 4 times in about 4.3 hours (07:47 → 12:03Z).
     - Including the earlier windows (45910259066 and 45920194253, among others), #9442 has failed at least 10 times across 4 windows.
     - Each retry also re-bootstrapped Semble: 55,042, 59,069, 53,524 and 63,468 ms for #10100, and 43,856 ms for 45970740796.
   - **Root cause (inference from code):**
     - At `review_conflict_resolve.sh:3688-3689`, the retry-state builder gives up when `regressed_by_resolver` and `pre_existing_drift` are both empty.
     - For a `timeout` or `no_progress` exit, the consecutive-failure count therefore never rises, and the tier ladder and escalation never engage.
   - **Exact change:** see Reliability 2, which fixes it, and Reliability 3, the diagnostic that confirms the exit class.
   - **Estimated time savings:** about 4–6 runner-hours per window at current rates. It also takes these PRs out of each review cycle's critical path.
   - **Implementation risk:** medium (retry-state semantics), and it can be switched off with a variable.
   - Finding check: new — raised in `analysis/workflow-optimization-2026-11-27.md` Speed 1, which has no processing-report verdict. The gap is present at `scripts/review_conflict_resolve.sh:3689` on this ref.

2. **Cap how many `tests-system-python` shards run at once (queue pressure; a critical-path win for everything else waiting on a runner).**
   - **Evidence:**
     - Failed CI runs lasted 3,826–8,184 s: 45987405876 (8,184 s), 45983884476 (7,825 s) and 45987280209 (7,784 s). Every CI test job has `timeout-minutes: 20`, or 30 for `orchestrate-poll`, and no `needs:` (`ci.yml:894-4825`).
     - So at least 35–106 min of each run was spent waiting for runners (inference).
     - Small jobs queued behind them, for example:

       | Run | Repo | Queue wait | Run duration |
       |---|---|---|---|
       | 46010292267 (poll) | bitsafe.io | about 2.5 min | 306 s |
       | 46010115903 (poll) | tele-funtoken-msg-scoring | about 87 s | — |
       | 46010922169 (judge) | fun-token-multi-chain | about 32 s | 95 s |
       | 46010983624 (PR checks) | coding-workflows | about 17 s | 29 s |
   - **Root cause:** each CI run asks for at least 32 slots at once for `tests-system-python` (`ci.yml:4507`, matrix 0–31, `fail-fast: false`, no `max-parallel`), plus about 10 other jobs. §26 ("Bound fan-out … set `max-parallel` on matrices") is not applied here.
   - **Exact change:**
     ```yaml
     strategy:
       fail-fast: false
       # §26: bound fan-out so one CI run cannot hold 32 runner slots at once.
       # Slowest shard ~11-12 min (comment above); 16 in flight => ~2 waves,
       # still inside the 30-min orchestrate-poll leg (assumption: unmeasured).
       max-parallel: 16
     ```
   - **Estimated time savings:** up to 16 freed slots per CI run at peak, for review, poll and judge jobs (inference). The system-python leg itself may grow from about 12 to about 24 min.
   - **Implementation risk:** medium.
     - Check that the system-python leg stays under the `orchestrate-poll` leg using the existing `CI_SUBTEST_*` and budget-watch timings before landing.
     - Roll back by deleting the one line. Check names and the matrix list are unchanged, so branch protection is unaffected.
   - Finding check: new — the processing report has no verdict on `max-parallel`. Earlier CI-sharding verdicts (lines 1174, 1999 and 2046) concern splitting more, not bounding concurrency. No `max-parallel` exists in `ci.yml` on this ref.

3. **Probe Claude pool accounts in parallel (micro-optimization).**
   - **Evidence:** in `Unblock judge #929` 46010922169 (fun-token-multi-chain), `Resolve Claude credential` probed 13 accounts one at a time from 13:53:58 to 13:54:17, about 20 s. The judge step itself took about 5 s.
   - **Root cause:** a sequential `for name in "${accounts[@]}"` probe loop (`scripts/claude_pool_token.sh:254`).
   - **Exact change:**
     - Run the probe subshells in the background, at most `CLAUDE_POOL_PROBE_PARALLEL` at a time (default `4`; `1` restores today's behaviour, per §8).
     - Then emit the `CLAUDE_POOL probe` lines in the original account order in the second loop (`:282`).
   - **Estimated time savings:** about 15 s per Claude-engine job.
   - **Implementation risk:** low. Each probe uses a different account, and the output order is unchanged.
   - Finding check: new — raised in `analysis/workflow-optimization-2026-11-27.md` Speed 3, which has no processing-report verdict. The loop is still sequential at `scripts/claude_pool_token.sh:254` on this ref.

## Cost Optimizations

1. **Export Claude-engine usage in per-run cost telemetry.**
   - **Evidence:**
     - All 6 resolver failures and every unblock-judge, poll and implement run show `or_calls=0`, `codex_calls=0` and `codex_tokens_used=0`, even though the Claude engine is active: `CLAUDE_POOL available=true` in 46010922169, and PR #10167 is labelled `ai:engine-claude`.
     - `analysis_context.json` has no `claude_*` fields. The 2026-11-25 report noted the same gap ("`claude_*` cost fields not exported").
   - **Root cause:**
     - `build_run_cost_telemetry` (`scripts/cost_audit.py:609-613`) copies only `RUN_COST_TELEMETRY_FIELDS` (`:209-245`), which leaves out `CLAUDE_COUNT_KEYS` (`:128-134`) and `claude_cost_usd`.
     - `parse_log` already computes them (`:783`), so they are dropped at export.
   - **Exact change:**
     ```python
     RUN_COST_TELEMETRY_FIELDS = (
         # ...existing fields unchanged...
         "wall_clock_p99_ms",
         *CLAUDE_COUNT_KEYS,
         "claude_cost_usd",
     )
     AGGREGATABLE_COST_FIELDS = (
         # ...existing fields unchanged...
         "context_budget_warn_count",
         *CLAUDE_COUNT_KEYS,
     )
     ```
     In `aggregate_run_cost_telemetry`, sum `claude_cost_usd` as a float rounded to 6 decimal places. Do not pass it through `_to_int`. Update any test that pins the exact key set.
   - **Estimated savings:** none directly. It makes the cost of the resolver loop (Speed 1) and other Claude-engine work measurable. Today, runs that spent 1,771–5,810 s each report zero model usage.
   - **Quality risk:** none. The keys are additive and default to 0.
   - Finding check: new — no processing-report verdict on exporting Claude fields. The omission is present at `scripts/cost_audit.py:209-245` on this ref.

2. **Remove repeated resolver spend on #10100 and #9442.**
   - **Evidence:**
     - 22,189 s of run wall time on 6 failed runs.
     - 4 Semble bootstraps (231,103 ms) and 4 queries (41,909 bytes) on #10100 were spent again with near-identical context.
     - Model spend cannot be measured until Cost 1 lands.
   - **Root cause and change:** see Reliability 2.
   - **Estimated savings:** about 6 runner-hours per window, plus model tokens that are not yet measured.
   - **Quality risk:** low. Escalation only adds the `ai:resolver-escalated` path earlier.
   - Finding check: new — same evidence and code gap as Speed 1.

**Other observations (no change recommended):**
- **Cache hit rate:**
  - `cache_hit_rate` is 0.7378 overall (38 OpenRouter calls, 15,623,798 tokens), 0.7425 for coding-workflows and 0.7265 for tele-funtoken-msg-scoring.
  - coding-workflows has kept falling: 0.877 in the 2026-11-25 window, 0.777 earlier today, 0.743 now.
  - All coding-workflows OpenRouter tokens came from 3 review runs: 45976870422 (0.595, 244,800 cache-write tokens), 45980026001 (0.784) and 45982155581 (0.762).
  - Prefix and cache instrumentation was rejected earlier (processing report line 2209: `REVIEWER_PROMPT_COMPONENTS` already carries `static_prefix_sha256`), so this is tracked, not recommended.
- **Semble is reducing prompt expansion, not adding noise:**
  - 12 queries, 141,434 bytes (about 11.8 KB per query), 56 sources, `static_dup_bytes=0` and 0 echo lines dropped.
  - Bootstraps: 12, 0 failed, 0 unused, 768,598 ms in total (about 64 s each), which is at most 3.6% of a 1,771-second review run.
  - The only waste is the repeated bootstraps inside the resolver loop.
- **Context budget:** `context_budget_warn_count=0` and `break_glass_count=0`, so there is no prompt-size pressure.
- **Serena:** 0 queries, 0 response bytes and 0 tool calls, so there is no replacement value to judge.

## Reliability Improvements

1. **Mount the run-logs bundle on the Claude log-analysis path (observability; a wiring defect present for 3 windows).**
   - **Failure evidence:**
     - This run got "Directory does not exist" for `/home/runner/work/_temp/workflow-log-output`.
     - The 2026-11-25 and earlier 2026-11-27 reports record the same gap.
     - None of the 19 failing runs could be root-caused from step logs.
   - **Root cause category:** config/wiring. `.github/workflows/workflow-log-analysis.yml:1719-1720` sets only `AI_ENGINE_MODEL_HINT` and `AI_ENGINE_EFFORT_HINT`. The codex fallback (`:1694`) gets `wla_run_logs_include_args`, but the Claude path never sets `AI_ENGINE_INCLUDE_PATHS`.
   - **Exact fix:**
     ```bash
     AI_ENGINE_INCLUDE_PATHS="${RUN_LOGS_DIR:-}" AI_ENGINE_MODEL_HINT="${WORKFLOW_EDITOR_MODEL}" AI_ENGINE_EFFORT_HINT="" \
       claude_run_selected LOG_ANALYSIS "${CODEX_PROMPT_FILE}" "${REPORT_FILE}" "${PWD}" -- wla_codex_analyze
     ```
     Apply the same change to the `wla_codex_api_redundancy` call at `:3585`. Use whichever variable already feeds `wla_run_logs_include_args` (assumed to be `RUN_LOGS_DIR`; check before editing).
   - **Expected impact:** deep-dive evidence returns for every failure and slow-run finding.
   - **Rollback/fail-open:** an empty value is a no-op. The pool-isolation check in `scripts/ai_engine.sh` still refuses overlapping paths.
   - Finding check: new — no processing-report verdict. The "restore full-log archive" verdict (line 2283) concerns a missing artifact, not this mount wiring. `AI_ENGINE_INCLUDE_PATHS` is absent from `workflow-log-analysis.yml` on this ref.

2. **Persist resolver retry state for terminal failures that have no fingerprint violations.**
   - **Failure evidence:** the 6 failures on #10100 and #9442 listed above, with no visible escalation.
   - **Root cause category:** logic gap (inference from code). `scripts/review_conflict_resolve.sh:3688-3689` returns `ok: False` ("no fingerprint failures detected for retry-state persistence"), so same-head timeouts never advance the ladder.
   - **Exact fix:**
     - When the exit class is `timeout` or `no_progress` and both sets are empty, use one synthetic member, `{"kind": "terminal", "fp_key": ["terminal", <exit_class>]}`.
     - This mirrors the `isolation_failure` synthetic key at `:3673-3680`.
     - Put it behind a new variable that defaults to `true` (check the name is unique first, per §10).
   - **Expected reliability impact:** repeated same-head timeouts reach the existing `strict` → `ratio` → escalated path instead of re-running full review cycles.
   - **Rollback/fail-open:** the builder already fails open, and setting the variable to `false` restores today's behaviour.
   - Finding check: new — raised in `analysis/workflow-optimization-2026-11-27.md` Reliability 3, which has no processing-report verdict. The gap is present at `:3689` on this ref.

3. **Parse `RESOLVER_TERMINAL` into run telemetry (diagnostic addition; it decides whether Reliability 2 is sufficient).**
   - **Failure evidence:** the 6 resolver failures cannot be classified as `timeout`, `no_progress`, `markers` or `fingerprint` from the collected telemetry.
   - **Root cause category:** observability gap. `scripts/review_conflict_resolve.sh:532-533` prints `RESOLVER_TERMINAL … attempts=<int> attempt_ms=<csv|none> exit_class=<enum> rc=<int> fp_violations=<int> markers=<int|unknown>`, but no `scripts/*.py` file reads it.
   - **Exact fix (`scripts/cost_audit.py`):**
     ```python
     RESOLVER_TERMINAL_RE = re.compile(
         _TELEMETRY_LINE_PREFIX_RE_SRC
         + r"RESOLVER_TERMINAL\s+run_id=\S+\s+run_attempt=\S+\s+pr=\S+\s+"
         r"attempts=(?P<attempts>\d+)\s+attempt_ms=(?P<attempt_ms>none|[0-9]+(?:,[0-9]+)*)\s+"
         r"exit_class=(?P<exit_class>ok|timeout|no_progress|markers|fingerprint|scope|commit|provenance|other)\s+"
         r"rc=(?P<rc>\d+)\s+fp_violations=(?P<fp>\d+)\s+markers=(?P<markers>\d+|unknown)(?:\s|$)"
     )
     ```
     Emit the additive per-run fields `resolver_terminal_exit_class`, `resolver_terminal_attempts` and `resolver_terminal_attempt_ms_total`. They stay null when no line matches.
   - **Expected impact:** the next window can say whether #10100 and #9442 are timeout loops (fixed by Reliability 2) or fingerprint loops (which need a different fix).
   - **Rollback/fail-open:** the fields are additive only.
   - Finding check: new — raised 2026-11-25 and in the earlier 2026-11-27 report, with no processing-report verdict. It is still unparsed on this ref.

4. **Explain `REVIEWER_FINDINGS … reason=flagged_by_missing` (diagnostic; the symptom affects 100% of runs that emitted lines).**
   - **Failure evidence:**
     - 15 unavailable lines across 3 runs (45976870422, 45980026001 and 45982155581), all 5 reviewers each time.
     - 0 runs in this window produced counted lines.
     - Earlier windows: 45552464821 (PR 9999) and 45909271013 (PR 10178).
   - **Root cause category:** observability gap.
     - One issue with an empty `flagged_by`, or an entry that does not match the slug, safe name or prefix (`match()`, `:692-700`), aborts attribution for the whole record (`:744-746`).
     - The emitted tail (`:779`) does not say how many issues were affected.
   - **Exact fix:** count issues where `not matched` and where `has_unmatched[position]`, then append `issues=<n> unattributed=<n> unmatched=<n>` to the `outcome=unavailable` tail. These are integers only.
   - **Expected impact:** shows whether the consolidator omits `FLAGGED_BY` (a prompt problem) or uses names the matcher cannot resolve (a code problem).
   - **Rollback/fail-open:** additive fields. The `counted` path is unchanged.
   - Finding check: new — raised in `analysis/workflow-optimization-2026-11-27.md` Reliability 7, which has no processing-report verdict. The early return without counts is present on this ref.

**Other observations (no change recommended):**
- **PR-scoped CI regressions that the gates caught correctly:**

  | PR | Failing step | Runs |
  |---|---|---|
  | #10000 | `Review-blocked judge label propagation contract test` (3×) | 45946930109, 45966669848, 45987280209 |
  | #10066 | `Review/judge/conflict strict-render contract test` (2×) | 45964653558, 45994476004 |
  | #10067 | `Review/judge/conflict strict-render contract test` | 45974744375 |
  | #10163, #10081, #10085 | `tests-system-python` shards 16, 14 and 28, all different | 45987405876, 45981344577, 45983884476 |

  Later review runs on #10066 (46006355514) and #10000 (46002432255) succeeded. Without logs, flakiness cannot be told apart from a PR defect.
- **A push CI failed:** `CI` 45960904114 failed at `Review autofix step-script contract and workflow file size guard tests`. An empty run-name means a non-dispatch event, so this was a push to `main`, `stable` or `orchestrator/project-*`. `CI quick` 45960183914 failed `Inventory parity (quick)` 6 minutes earlier. The later push CIs 45974465670 and 45981818403 succeeded, which suggests it healed (inference).
- **Possible slow step at cancellation:** 3 cancelled CI runs were in `Review-blocked judge security-pass helper tests` when cancelled (45978675130, 45995040066 and 45994554871), and 45955968212 failed there. This may point to a slow step. It cannot be verified without logs.
- **Semble fallbacks:** 40 `context=contract-test` (4 in each of 10 CI runs) and 1 runtime fallback (tele-funtoken implement; the run ID is not in the context). This is healthy fail-open behaviour.
- **Serena:** 0 `SERENA_PROBE` and 0 `SERENA_FALLBACK` lines.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines are available. The deep-dive logs are not mounted, and the log summaries that mention memory say none was visible: multi-user-ai-agent 46010428688 and fbc_shutdown 46010025366. In addition, all poller logs were truncated by 340K–590K characters.
- The retrieve hit rate, `estimated_tokens` versus budget, `keyword_method` mix, `fail_open` and push retries cannot be computed for this window.
- No new recommendation. Emission is already in place (processing report line 2282: "`AI_MEMORY_TELEMETRY` … already emitted"). Reliability 1 restores the evidence needed to verify it next window.

## GH API Call Audit

1. **Scheduled close cleanup can still send a GraphQL batch with no aliases, and `gh_retry` retries the validation error 5 times.**
   - **Evidence:** no run in this window shows it. The 94 `cancel_on_pr_close` runs all succeeded, but none have readable logs.
   - **Earlier-window evidence:** 7 consumer runs (for example 45928647048) failed 5 out of 5 attempts with `Field must have selections`.
   - **Still present in code:**
     - `_cpr_fetch_pr_state_batches` (`.github/workflows/cancel_on_pr_close.yml:218`) has no empty-batch guard (no `alias_count` or `empty_batch` on this ref).
     - `scripts/gh_helpers.sh` has no `Field must have selections` permanent-failure pattern.
   - **Change:** skip a batch when the alias count is 0. Separately, classify the deterministic GraphQL validation phrases as non-retryable (exact patches in `analysis/workflow-optimization-2026-11-27.md` Reliability 4 and 5).
   - **Estimated reduction:** up to about 840 GraphQL calls and about 45 runner-minutes per day across 7 consumers, if the earlier rate holds.
   - **Rate-limit risk reduction:** low to medium.
   - Finding check: new — no processing-report verdict. The cause is present on this ref, but no run in this window supports it directly.

**Other observations:**
- PR checks 46010983624 logged `GH_LOOP_CALL_LINT warnings=0 files=9`.
- No HTTP 429 or secondary-rate-limit lines appear in any log summary.
- Poller API counts cannot be assessed because of truncation. Per-job usage already exists as `GH_PAT_BUDGET … used_in_job=`.

## Prompt Cache & Memory System

- **Cache usage:**
  - Cache-read tokens: 11,330,074. Cache-write tokens: 1,387,683. Uncached prompt tokens: 2,638,079. `cache_hit_rate` = 0.7378.
  - The worst run is 45976870422 (PR 10185 round 4) at 0.595. Its 244,800 cache-write tokens over 8 calls point to a cold or changed prefix in a later round (inference).
  - The best run is 45980026001 at 0.784.
- **Fragmentation:** possible causes include round-specific context placed early in the prompt. This cannot be checked without logs, and prefix-hash telemetry (`REVIEWER_PROMPT_COMPONENTS static_prefix_sha256`) already exists. No new recommendation, per the earlier rejected verdicts.
- **Budget pressure:** `CONTEXT_BUDGET_WARN` = 0, so prompt growth is not the driver.
- **Memory retrieval:** no data (see AI Memory Health).

## Orchestrator Health

- **Poll coalescing works.**
  - 58 of 374 `orchestrate_poll` runs (15.5%) were cancelled, and the coding-workflows ones were `cancelled_before_first_step`: 46008954426 (25 s), 46008695578 (115 s) and 46006705323 (411 s). These are superseded queued ticks, which is the §26 behaviour.
  - There were 0 poll failures.
  - Poll durations: p50 390 s and p95 1,864 s. tele-funtoken polls run longest: 46005876715 (1,126 s), 46008062984 (945 s) and 46010115903 (about 9.5 min of poll body).
- **Operator-blocked item:** `UNBLOCK_JUDGE item=929 … hold=placeholder_retired ref=OPBNB_BASE_CUTOVER_RECORD_UNSET_OPERATOR_STEP outcome=holding` (fun-token-multi-chain 46010922169). The item waits on an operator step. Track it as a stall indicator.
- **Merge-lease pressure:** PR #10167 carries `ai:acceptance-unmet` and `ai:merge-lease-wait` (46010983624).
- **Resolver loops** on #10100 and #9442 are the main stuck-PR signal (Reliability 2 and 3).
- **Indicators to track:**
  - Failures of `Run Codex resolver` per PR per window. The target is at most 2 before `ai:resolver-escalated`.
  - The poll cancelled share (currently 15.5%).
  - `UNBLOCK_JUDGE outcome=holding` counts by `ref=`.

## Pipeline Flow Bottlenecks

Order by end-to-end impact:

1. **Queueing (largest share).**
   - CI p95 is 7,186 s, against compute capped at 20–30 min.
   - Review/autofix p50 is 1,723 s and p95 is 5,943 s. The slow successful runs reach 11,115 s (45976433947, PR 10085) and 9,910 s (45978078257, PR 10054).
   - Every log summary shows busy-runner waits.
   - Fix: Speed 2 (bound the matrix).
2. **Retry overhead:** the resolver loops cost about 6.2 runner-hours. Fix: Reliability 2 and 3.
3. **Compute:** Semble bootstraps are about 64 s per review run. Claude pool probing is about 20 s per Claude job (Speed 3).
4. **Merge/conflict overhead:** `ai:merge-lease-wait` on #10167, plus the resolver failures above.
5. **Skipped-run fan-out (run-record and API noise, not runner time).**
   - **Evidence:** 727 "other" runs, mostly skipped.

     | Family | Skipped / total |
     |---|---|
     | `workflow_failure_heal` | 121/121 |
     | `clarify` | 112/121 |
     | `orchestrate_clarify_respond` | 106/108 |
     | `check_failure_triage` | 104/115 |
     | `implement` | 100/108 |
     | `plan` | 98/108 |
     | `integration_pr_readiness` | 63/63 |

     One issue event at 13:50:09Z created 4 skipped runs: 46010528346, 46010528160, 46010528221 and 46010528076.
   - **Root cause:** `internal-clarify.yml`, `internal-plan.yml` and `internal-implement.yml` all trigger on `issue_comment: [created]`, and `internal-workflow-failure-heal.yml` triggers on `labeled`. GitHub cannot filter on body or label at the `on:` level, so only job-level `if:` gates apply.
   - **Change:** fold the three `issue_comment`-triggered internal wrappers into one wrapper with three `uses:` jobs. This cuts run records per comment from 3 to 1.
   - **Impact:** fewer run records and `workflow_run` events. It does not save runner time.
   - **Risk:** medium. Workflow names that log parsers and families key on would change, so it needs §10 alias handling.
   - Finding check: new — the prior verdict `event-fanout-reduction` (processing report line 551) was deferred as broader work, and the cause is still present on this ref.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Bottlenecks:** CI queueing (p95 4,731 s for the repo); review/autofix p50 1,723 s and p95 5,943 s.
- **Failures:** 19 out of 1,000 listed runs (truncated): 13 CI and 6 resolver.
- **Cost drivers:** 3 review runs used 11,056,907 OpenRouter tokens (cache hit 0.743). Claude spend is not measured.
- **Prioritized actions:**
  1. Persist resolver retry state for terminal exits (Reliability 2).
     Finding check: new — gap at `scripts/review_conflict_resolve.sh:3689`; failures 45960909393, 45965322875, 45979232511, 45995474473, 45953304703 and 45970740796.
  2. Mount the run logs on the Claude log-analysis path (Reliability 1).
     Finding check: new — `workflow-log-analysis.yml:1719-1720` lacks `AI_ENGINE_INCLUDE_PATHS`; this run had no logs directory.
  3. Bound `tests-system-python` to `max-parallel: 16` (Speed 2).
     Finding check: new — `ci.yml:4507` has no `max-parallel`; CI p95 is 7,186 s.

### shubhodeep1/tele-funtoken-msg-scoring
- **Bottlenecks:** the longest poller ticks: 46005876715 (1,126 s), 46008062984 (945 s) and 46010115903 (669 s, about 87 s queued).
- **Failures:** none.
- **Cost drivers:** 14 OpenRouter calls, 4,566,891 tokens, cache hit 0.727. 1 runtime Semble fallback (implement).
- **Prioritized actions:**
  1. Export Claude usage in run telemetry (Cost 1).
     Finding check: new — `scripts/cost_audit.py:209-245` omits `claude_*`.
  2. Restore the deep-dive logs needed to explain the 1,126-second poll ticks (Reliability 1).
     Finding check: new — `ORCH_POLL_CYCLE_SUMMARY` is already emitted, but its log was truncated by 590,690 characters in the summary for 46010115903.
  3. Parallel Claude pool probes (Speed 3).
     Finding check: new — sequential loop at `scripts/claude_pool_token.sh:254`.

### shubhodeep1/fun-token-multi-chain
- **Bottlenecks:** the judge's credential resolution took about 20 s against about 5 s of judge work (46010922169), plus about 22–32 s of runner queueing.
- **Failures:** none. Item #929 is held on an operator step.
- **Cost drivers:** not measured, because Claude usage is not exported.
- **Prioritized actions:**
  1. Parallel Claude pool probes (Speed 3).
     Finding check: new — 13 sequential probes over about 20 s in 46010922169; loop at `claude_pool_token.sh:254`.
  2. Export Claude usage (Cost 1).
     Finding check: new — same omission at `cost_audit.py:209-245`.
  3. Bound the CI matrix fan-out to reduce shared queueing (Speed 2).
     Finding check: new — the 22-second queue in 46010448450 and the 32-second queue in 46010922169 come from the shared hosted-runner pool.

### shubhodeep1/binance-blessings
- **Bottlenecks:** poll ticks of 270–366 s with about 18 s queued (46010462952).
- **Failures:** none.
- **Cost drivers:** no OpenRouter or Claude telemetry; 39 "other" runs.
- **Prioritized actions:**
  1. Bound the CI matrix fan-out (Speed 2).
     Finding check: new — `All GitHub-hosted runners … busy` in 46010462952.
  2. Export Claude usage (Cost 1).
     Finding check: new — `cost_audit.py:209-245`.
  3. Restore the deep-dive logs (Reliability 1).
     Finding check: new — 412,057 characters of poll log were truncated in the summary for 46010462952.

### shubhodeep1/mongo-explorer
- **Bottlenecks:** poll ticks of 109–243 s.
- **Failures:** none.
- **Cost drivers:** none measured.
- **Prioritized actions:**
  1. Bound the CI matrix fan-out (Speed 2).
     Finding check: new — shared runner pool; this repo's p95 is 1,510 s against a p50 of 176 s.
  2. Export Claude usage (Cost 1).
     Finding check: new — `cost_audit.py:209-245`.
  3. Restore the deep-dive logs (Reliability 1).
     Finding check: new — no step logs were readable for 46010000463.

### shubhodeep1/multi-user-ai-agent
- **Bottlenecks:** poll queueing of about 3 s and a 180-second tick (46010428688).
- **Failures:** none.
- **Cost drivers:** none measured.
- **Prioritized actions:**
  1. Bound the CI matrix fan-out (Speed 2).
     Finding check: new — busy-runner notice in 46010428688.
  2. Export Claude usage (Cost 1).
     Finding check: new — `cost_audit.py:209-245`.
  3. Restore the deep-dive logs to check memory telemetry (Reliability 1).
     Finding check: new — the summary for 46010428688 saw no `AI_MEMORY_TELEMETRY` because of truncation.

### shubhodeep1/fbc_shutdown
- **Bottlenecks:** about 11 s of poll queueing (46010025366).
- **Failures:** none.
- **Cost drivers:** none measured.
- **Prioritized actions:**
  1. Bound the CI matrix fan-out (Speed 2).
     Finding check: new — busy-runner notice in 46010025366.
  2. Export Claude usage (Cost 1).
     Finding check: new — `cost_audit.py:209-245`.
  3. Restore the deep-dive logs (Reliability 1).
     Finding check: new — about 340K characters truncated in 46010025366.

### shubhodeep1/bitsafe.io
- **Bottlenecks:** about 2.5 min of queueing on a 306-second poll (46010292267). This is the worst queue share observed.
- **Failures:** none.
- **Cost drivers:** none measured.
- **Prioritized actions:**
  1. Bound the CI matrix fan-out (Speed 2).
     Finding check: new — 13:48:32 → 13:51:00 queue in 46010292267.
  2. Export Claude usage (Cost 1).
     Finding check: new — `cost_audit.py:209-245`.
  3. Restore the deep-dive logs (Reliability 1).
     Finding check: new — about 376K characters truncated in 46010292267.

## Metrics Appendix

**Runs by repository**

| Repo | Runs | Success | Fail | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| coding-workflows (truncated) | 1,000 | 450 | 19 | 51 | 480 | 1.9% | 28 | 4,731 |
| tele-funtoken-msg-scoring | 413 | 200 | 0 | 5 | 208 | 0% | 11 | 1,834 |
| binance-blessings | 125 | 84 | 0 | 2 | 39 | 0% | 211 | 1,595 |
| fun-token-multi-chain | 81 | 79 | 0 | 2 | 0 | 0% | 262 | 1,768 |
| fbc_shutdown | 69 | 67 | 0 | 2 | 0 | 0% | 163 | 1,440 |
| bitsafe.io | 68 | 66 | 0 | 2 | 0 | 0% | 254 | 1,645 |
| mongo-explorer | 68 | 66 | 0 | 2 | 0 | 0% | 176 | 1,510 |
| multi-user-ai-agent | 67 | 65 | 0 | 2 | 0 | 0% | 248 | 1,674 |
| **All** | **1,891** | **1,077** | **19** | **68** | **727** | **1.0%** | **122** | **3,442** |

**Key workflow families**

| Family | Runs | Fail | Cancelled | p50 s | p95 s |
|---|---|---|---|---|---|
| ci | 76 | 13 (17.1%) | 3 | 901.5 | 7,186 |
| review_autofix | 319 | 6 (1.9%) | 6 | 1,723 | 5,943 |
| orchestrate_poll | 374 | 0 | 58 | 390 | 1,864 |
| cancel_on_pr_close | 94 | 0 | 0 | 124 | 1,654 |
| unblock_judge_dispatch | 24 | 0 | 0 | 102.5 | 750 |

**Tokens and review telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 38 (38 / 0) |
| Prompt / completion tokens | 2,638,079 / 269,412 |
| Cache read / cache write tokens | 11,330,074 / 1,387,683 |
| Total tokens | 15,623,798 |
| `cache_hit_rate` (all / coding-workflows / tele-funtoken) | 0.737835 / 0.742507 / 0.726532 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `claude_*` | not exported (Cost 1) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all, 42 samples) | 2,840,500 / 10,620,950 |
| review_autofix (13 samples) | 5,810,000 / 10,970,400 |
| ci (11 samples) | 5,294,000 / 8,448,600 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| Reviewer findings | 319 review runs; 3 with lines; 15 unavailable lines (`flagged_by_missing`); 0 counted |

**GH API summary**

| Signal | Value |
|---|---|
| HTTP 429 / secondary rate-limit lines | 0 visible |
| `GH_LOOP_CALL_LINT` | `warnings=0 files=9` (46010983624) |
| Poller API counts | not assessable (340K–590K characters truncated per poll log) |

**Semble and Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 12 / 141,434 (about 11.8 KB per query); coding-workflows 10 / 112,221, tele-funtoken 2 / 29,213 |
| `semble_sources_total` / `static_dup_bytes` / `echo_lines_dropped` | 56 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 12 / 0 / 0 / 768,598 (about 64 s each) |
| `semble_fallbacks` (contract-test / runtime) | 41 (40 / 1) |
| Serena query / fallback / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Findings check**

| Status | Count |
|---|---|
| new | 9 |
| recurring | 0 |
| reopened | 0 |
| unverified | 0 |
| dropped: already fixed on this ref | 0 |
| dropped: previously decided | 5 |

The 5 previously decided items:
- Semble contract-test fallback classification: already-implemented (line 2244).
- Prompt-prefix and cache instrumentation: rejected (line 2209).
- Runner-queue logging: rejected (lines 1853 and 2282).
- `AI_MEMORY_TELEMETRY` emission: already emitted (line 2282).
- Moving the strict-render test earlier: rejected (line 1563).

**Data gaps**
- **Run logs:** `/home/runner/work/_temp/workflow-log-output` was not mounted, so there was no `summary.json` and no `errors/`, `slow/` or `recent/` folders.
- **Truncation:** coding-workflows is truncated (page limit reached). Successful runs were sampled at 0.07.
- **Missing telemetry:** Claude usage is not exported, and there are no AI-memory lines.
- **Processing report:** `analysis/recommendation-processing-report.md` was readable; its latest ledger covers the 2026-10-08 doc.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 3 of 319 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 3.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| No REVIEWER_FINDINGS lines in the window | - | - | - | - | - |
