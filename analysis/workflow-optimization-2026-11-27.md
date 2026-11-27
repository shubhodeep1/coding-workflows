<!-- workflow-log-analysis: window_start=2026-11-26T02:10:22Z window_end=2026-11-27T02:32:55Z run=45928903260 -->
## Executive Summary

- **Fix the log-analysis mount first. It caused the data gap in this window and the last one.** On the Claude-engine path, `.github/workflows/workflow-log-analysis.yml:1701-1702` calls `claude_run_selected` without `AI_ENGINE_INCLUDE_PATHS`. As a result, the staged run-logs directory (`--include` at `:1593`) is never mounted. `scripts/ai_engine.sh:568-573` only mounts paths that are listed in `AI_ENGINE_INCLUDE_PATHS`. The `logs=missing` coverage line at `:1569` does not fire either, because the directory exists on the host. This run could not read `/home/runner/work/_temp/workflow-log-output`, and the 2026-11-25 report hit the same gap. A one-line fix restores deep-dive evidence for every failure finding below. Confidence: high.
- **PR #9442 is still in a resolver loop. It has failed at least 8 times across 3 windows.** Two more runs failed at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`: 45910259066 (3,473 s) and 45920194253 (2,831 s), about 1.75 runner-hours. The retry-state gap at `scripts/review_conflict_resolve.sh:3097` is still present, and no script parses `RESOLVER_TERMINAL`. Impact: about 1.75–5 runner-hours per window. Confidence: medium (logs not readable).
- **Scheduled close cleanup sends a malformed GraphQL query in all 7 consumer repos and retries it 5 times.** The 7 runs (45928834799, 45928711307, 45928703253, 45928656736, 45928647048, 45928624524, 45928523313) all logged `Field must have selections (field 'repository' …)`, then `gh command failed after 5 attempts`. That is about 15–17 s of backoff and 5 wasted GraphQL calls per tick. The root cause is in `cancel_on_pr_close.yml:235-249`: when no PR number is appended, the query is sent with an empty selection set. `gh_retry` also treats GraphQL validation errors as transient. Impact: up to about 840 GraphQL calls and about 45 runner-minutes per day, plus a false `##[error]` annotation on every tick. Confidence: high on the cause; the function itself is unaffected (inference: an empty batch has nothing to cancel).
- **Hosted-runner queueing dominates wall-clock time.** Review `gate` jobs waited 9–15 min (45926055571 about 15m17s, 45927749316 about 14m40s, 45925896322 about 11.7m). Tiny scheduled jobs waited most of their runtime: `provider-outage-resume` 43–59 s for under 1 s of work, and cancel run 45928831552 waited 2m54s for 16 s of work. The smallest safe lever that has no prior verdict is folding one of the three scheduled jobs in `workflow-templates/ai-review.yml` (up to 168 fewer jobs per day). Confidence: medium on impact (inference).
- **Cost and cache are healthy overall, but coding-workflows' hit rate fell.** `cache_hit_rate` is 0.798 overall, across 64 OpenRouter calls and 31.1M tokens. coding-workflows dropped from 0.877 to 0.777; tele-funtoken-msg-scoring rose from 0.495 to 0.861. There were 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` events. Prefix and per-phase cache work was rejected earlier, so this is an observation only. Confidence: medium.
- **Data gaps:**
  - **Truncated repository:** `shubhodeep1/coding-workflows` (the page limit was reached, so only part of its runs are listed). No repository failed.
  - **Run logs not mounted:** the run-logs directory could not be read in the sandbox (see the first bullet). All failure-cause statements below rest on `analysis_context.json`, `log_summary` fields and the repository code.

## Speed Optimizations

1. **Stop the #9442 resolver loop (critical path, largest per-PR latency).**
   - **Evidence:** 45910259066 (3,473 s) and 45920194253 (2,831 s) failed at `Run Codex resolver, validate, stage, commit`, after 3 failures in each of the 2026-11-20 and 2026-11-25 windows. Each run made 1 Semble query (11,370 bytes, 6 sources). The sweep run 45925828338 logged `pr=9442 reason=retryable`, so the PR is never routed to non-retryable handling.
   - **Root cause (inference from code):**
     - `build_resolver_retry_state_artifact` returns `ok: False` when there are no fingerprint failures (`scripts/review_conflict_resolve.sh:3097`).
     - For a timeout or no-progress exit, the retry count therefore never rises, and the existing tier ladder and escalation never engage.
   - **Exact change:** Reliability 2 (diagnostic) and Reliability 3 (fix).
   - **Estimated time savings:** about 1.75–5 runner-hours per window on this PR. It also removes the PR from every review cycle's critical path.
   - **Risk:** low for the diagnostic, medium for the retry-state change.
   - Finding check: new — the 2026-11-25 report raised it, but it has no entry in `analysis/recommendation-processing-report.md`. The gap is still at `scripts/review_conflict_resolve.sh:3097` on this ref.

2. **Fold `non-retryable-backfill` into the `provider-outage-resume` job in the consumer `ai-review.yml` (queue pressure; a critical-path win for everything waiting on a runner).**
   - **Evidence:**
     - Every hourly `AI Review` schedule tick starts up to 3 single-purpose jobs.
     - `provider-outage-resume` waited 43 s (45928135804), 54 s (45928096710), 57 s (45928411377) and 59 s (45928420035) for about 0.4–0.6 s of work.
     - Run 45928270405 shows both `non-retryable-backfill` (`scanned=1 candidates=0`) and `provider-outage-resume` (about 57 s queued) in one tick.
     - `security-hold-sweep` queued about 2.5 min (45928427582).
   - **Root cause:** `workflow-templates/ai-review.yml:83-106` defines `non-retryable-backfill` as its own job. It has no §26 split-reason comment, and it uses the same `GH_PAT` as `provider-outage-resume`.
     - `security-hold-sweep` keeps a valid isolation reason: it is `github.token`-scoped with `SECURITY_HOLD_SWEEP_TOKEN_MODE: actions_token`, so leave it separate.
   - **Exact change:**
     - Move the backfill checkout and run steps to the end of `provider-outage-resume`, after the `provider_outage` step, so the `paused` output is unchanged.
     - Keep `continue-on-error: true`, the `.non-retryable-backfill-support` checkout path (`scripts/workflow_wrapper_refs.py:26` renders it), and the existing `if:` var gates as a step-level `if:`.
     - Update `tests/test_workflow_failure_heal_non_retryable_backfill.py:483-499` and `tests/test_update_workflows_guardrails.py:393`, which key on the job id.
   - **Estimated savings:** 1 runner slot per tick per consumer, up to 168 jobs per day across 7 consumers. Each saved job queued 10–60 s in this window. The latency saved for other queued work is an inference.
   - **Risk:** medium-low.
     - The backfill step would run under the job's broader `github.token` grants, but it already authenticates with `GH_PAT`, which is broader.
     - The backfill's own exit-0 contract is unchanged, and a backfill failure stays non-fatal.
   - Finding check: new — no prior verdict on these jobs. The split is present at `workflow-templates/ai-review.yml:83-176` on this ref.

3. **Probe Claude pool accounts in parallel (micro-optimization).**
   - **Evidence:**
     - `Resolve Claude credential` took about 24 s (45927823937), about 21 s (45927666257) and about 21 s (45925989667).
     - Each run probed 12–13 accounts at about 1.5–2 s each. The judge itself took 3–4 s.
     - 42 `unblock_judge_dispatch` runs ran in this window.
   - **Root cause:** `scripts/claude_pool_token.sh:254-272` runs one Haiku probe per account in a sequential `for` loop.
   - **Exact change:** run each probe subshell in the background with a cap of 4 at a time, then `wait`, and build `probes` in the original account order from the per-account transcript files. The `CLAUDE_POOL probe` lines and the `choose` input stay byte-identical.
     ```bash
     for name in "${accounts[@]}"; do
       ( probe_one "${name}" ) &            # writes ${work}/probe-${name}.{jsonl,rc}
       while [ "$(jobs -rp | wc -l)" -ge "${CLAUDE_POOL_PROBE_PARALLEL:-4}" ]; do wait -n || true; done
     done
     wait || true
     for name in "${accounts[@]}"; do record=...; echo "CLAUDE_POOL probe ..."; done
     ```
   - **Estimated time savings:** about 15–18 s per Claude-engine job (about 10–13 min per window on unblock judges alone, more in review jobs).
   - **Risk:** low.
     - Concurrent probes use different accounts.
     - A new `CLAUDE_POOL_PROBE_PARALLEL` setting would default to 4, and 1 restores today's behaviour (§8).
   - Finding check: new — no prior verdict on `claude_pool_token.sh`. The loop is sequential on this ref.

## Cost Optimizations

1. **Remove repeated resolver spend on #9442.**
   - **Evidence:** 6,304 runner-seconds this window on 2 failing runs. Those runs report `or_calls=0` and `codex_calls=0`; the resolver uses OpenCode or Claude, and `claude_*` cost is not exported, so model spend is unmeasured.
   - **Root cause and change:** Reliability 2 and 3.
   - **Estimated savings:** about 1.75 runner-hours per window plus unmeasured model tokens (3 attempts × up to 50 min each per run).
   - **Quality risk:** none for the diagnostic. The retry-state change only alters when the existing tier ladder engages.
   - Finding check: new — same evidence and code gap as Speed 1.

2. **Stop paying runner time and GraphQL calls for the malformed cancel-cleanup query.**
   - **Evidence:**
     - The 7 consumer runs listed in the Executive Summary each spent about 15–17 s in backoff (for example 02:31:12–02:31:29 in 45928647048, and 02:28:38–02:28:54 in 45928523313).
     - Each failed attempt dropped 694 bytes of stdout.
   - **Root cause and change:** Reliability 4 and 5.
   - **Estimated savings:** up to 168 ticks per day × about 16 s, about 45 runner-minutes per day, plus 840 GraphQL calls per day.
   - **Quality risk:** none. A batch with no aliases has nothing to resolve.
   - Finding check: new — `analysis/workflow-optimization-2026-11-24.md` item 2 raised it, but it has no processing-report verdict. The code at `.github/workflows/cancel_on_pr_close.yml:235-249` still sends an empty selection set.

**Other observations (no change recommended):**
- **Model and token mix:**
  - 4,237,864 prompt tokens, 367,490 completion tokens, 24,518,868 cache-read tokens and 1,984,776 cache-write tokens, over 64 calls, all with usage available.
  - The low-hit runs are 45925825086 (0.592, 420,466 cache-write tokens over 12 calls), 45925802073 (0.692, 511,302 writes) and 45925625121 (0.689, 0 writes).
  - The high-hit runs are 45909271013 (0.888) and 45755784922 (0.861).
- **No prompt-size pressure:** `context_budget_warn_count=0`, `break_glass_count=0`.
- **Semble is reducing prompt expansion cheaply, not adding noise:**
  - 18 queries, 196,997 bytes (about 10.9 KB per query), 102 sources, `static_dup_bytes=0`, 0 echo lines dropped.
  - Per review run that is about 5–33 KB, against 430K–1.0M prompt tokens (for example 45925625121: 33,258 bytes against 756,880 prompt tokens), well under 1% of the prompt.
  - Bootstraps: 18, 0 failed, 0 unused, about 58 s each, which is under 5% of a 30-minute review job.
- **Serena:** 0 queries, 0 response bytes, 0 tool calls. Its only events are 2 `SERENA_FALLBACK` lines in tele-funtoken validate run 45835946261, so there is no replacement value to assess.

## Reliability Improvements

1. **Mount the run-logs bundle on the Claude log-analysis path (observability; fixes the data gap two windows running).**
   - **Failure evidence:**
     - This run could not read `/home/runner/work/_temp/workflow-log-output`.
     - `analysis/workflow-optimization-2026-11-25.md:11` records the same gap.
     - Every failure in `failing_runs` (10 runs) could not be root-caused from step logs.
   - **Root cause category:** config/wiring defect.
     - `wla_run_logs_include_args` reaches only the codex fallback (`workflow-log-analysis.yml:1676`).
     - The Claude path (`:1701-1702`) does not set `AI_ENGINE_INCLUDE_PATHS`, the only way `scripts/ai_engine.sh:568-573` adds `--include`.
     - `WORKFLOW_LOG_ANALYSIS_COVERAGE logs=missing` is not emitted, because the host directory exists.
   - **Exact fix:**
     ```bash
     AI_ENGINE_INCLUDE_PATHS="${RUN_LOGS_DIR}" AI_ENGINE_MODEL_HINT="${WORKFLOW_EDITOR_MODEL}" AI_ENGINE_EFFORT_HINT="" \
       claude_run_selected LOG_ANALYSIS "${CODEX_PROMPT_FILE}" "${REPORT_FILE}" "${PWD}" -- wla_codex_analyze
     ```
     `RUN_LOGS_DIR` is already `""` when staging failed, so the change is a no-op in that case.
   - **Expected reliability impact:** deep-dive evidence is restored for all failure and slow-run findings, which turns the "unverified" items below into verifiable ones.
   - **Rollback/fail-open:**
     - `_ai_engine_pool_isolated` (`ai_engine.sh:383-389`) refuses to run if the include path overlaps the account pool.
     - Today's `RUNNER_TEMP/workflow-log-output[-bounded]` is a sibling of the pool, so no overlap is expected.
     - A refusal still falls through to the existing failure handling.
   - Finding check: new — no prior verdict. `AI_ENGINE_INCLUDE_PATHS` is absent from `workflow-log-analysis.yml` on this ref (it is set only in `implement.yml:6011` and `orchestrate.yml:1023`).

2. **Parse `RESOLVER_TERMINAL` into run telemetry (diagnostic addition).**
   - **Failure evidence:** 45910259066 and 45920194253 (#9442) cannot be classified as timeout, no-progress, markers or fingerprint from the collected telemetry.
   - **Root cause category:** observability gap. `scripts/review_conflict_resolve.sh:532` prints `RESOLVER_TERMINAL … attempts= attempt_ms= exit_class= rc= fp_violations= markers=`, but no file under `scripts/*.py` reads it (grep: only `review_conflict_resolve.sh` matches).
   - **Exact fix:** add an anchored regex using `_TELEMETRY_LINE_PREFIX_RE_SRC` in `scripts/cost_audit.py`, and emit additive per-run fields `resolver_terminal_exit_class`, `resolver_terminal_attempts` and `resolver_terminal_attempt_ms_total`. The 2026-11-25 report gives the regex sketch.
   - **Expected impact:** the next report can tell whether #9442 is a timeout loop or a same-head fingerprint loop, which decides whether item 3 stops it.
   - **Rollback/fail-open:** the fields are additive and stay null on no match.
   - Finding check: new — raised 2026-11-25 with no processing-report verdict. It is still unparsed on this ref.

3. **Persist resolver retry state for terminal failures with no fingerprint violations.**
   - **Failure evidence:** at least 8 resolver failures on #9442 across 3 windows with no visible escalation, and the sweep still classifies the PR `reason=retryable` (45925828338).
   - **Root cause category:** logic gap (inference from code). `scripts/review_conflict_resolve.sh:3097` returns `ok: False` ("no fingerprint failures detected for retry-state persistence"). The caller only warns (`:3386`), so `consecutive_failure_count` (`:2817`) never rises for timeouts.
   - **Exact fix:** when `exit_class` is `timeout` or `no_progress` and no fingerprint failures exist, use a synthetic member `{"kind": "terminal", "fp_key": ["terminal", <exit_class>]}`, mirroring the existing isolation-failure synthetic key. Same-head and same-signature rules stay unchanged. Put it behind a variable that defaults to `true`, like `RESOLVER_NON_RETRYABLE_ESCALATE_ENABLED`.
   - **Expected impact:** repeated same-head timeouts move through `strict` → `ratio` and reach `ai:resolver-escalated` instead of re-spending up to 150 min per review cycle.
   - **Rollback/fail-open:** the builder already fails open (`:3376-3379`), and the variable switch reverts the change.
   - Finding check: new — no processing-report verdict. The gap is present at `:3097` on this ref.

4. **Skip PR-state batches that have no aliases in scheduled close cleanup.**
   - **Failure evidence:** all 7 consumer `AI Cancel on PR Close` runs listed above failed 5 out of 5 GraphQL attempts with `Field must have selections` and `$owner`/`$name declared … but not used`, then logged `could not resolve PR-state batch 1; preserving affected runs`.
   - **Root cause category:** input-validation bug.
     - `_cpr_fetch_pr_state_batches` (`.github/workflows/cancel_on_pr_close.yml:231-249`) always sends `repository(owner:$owner,name:$name){…}`, even when the `while` loop appended no alias.
     - This happens when a split chunk has no numeric lines. Inference: an empty `pr-numbers.txt` (`:312`) on the ubuntu-26.04 image yields one empty chunk; this is unverified without logs.
   - **Exact fix:**
     ```bash
     printf '{}\n' > "${output_file}"
     [ -s "${numbers_file}" ] || return 0
     ...
     local alias_count=0
     while IFS= read -r pr_number; do
       case "${pr_number}" in ''|*[!0-9]*) continue ;; esac
       query+="pr_${pr_number}:pullRequest(number:${pr_number}){number state}"
       alias_count=$((alias_count + 1))
     done < "${batch_file_part}"
     if [ "${alias_count}" -eq 0 ]; then
       echo "CLOSE_CLEANUP_PR_STATE batch=${batch_index} aliases=0 outcome=skip reason=empty_batch"
       continue
     fi
     ```
     Register the `CLOSE_CLEANUP_PR_STATE` prefix through an `agents.d/<pr>-close-cleanup-pr-state.md` fragment (§25).
   - **Expected impact:** removes 7 false `##[error]` annotations per hour and the wasted retries.
   - **Rollback/fail-open:** unchanged. Unresolved PRs are still preserved.
   - Finding check: new — `analysis/workflow-optimization-2026-11-24.md` item 2 raised it, with no processing-report verdict. No guard and no test exists on this ref (grep for `_cpr_fetch_pr_state` in `tests/` and `changelog.d/` finds nothing).

5. **Treat GraphQL validation errors as non-retryable in `gh_retry`.**
   - **Failure evidence:** the same 7 runs each backed off 1+2+4+8+16 s on a deterministic validation error.
   - **Root cause category:** retry classification.
     - `_is_gh_permanent_failure` (`scripts/gh_helpers.sh:2036-2039`) matches 400/401/404/422 and scope errors.
     - GraphQL validation errors return HTTP 200 with an `errors` body, so they fall through to the backoff branch (`:3113`).
   - **Exact fix:** add only deterministic GraphQL validation phrases:
     ```bash
     ...|Commenting is disabled on issues with more than [0-9]+ comments|Field must have selections|is declared by [^ ]+ ?(query|operation)? ?but not used|doesn't exist on type|Parse error on"
     ```
     Leave transient GraphQL messages (`Something went wrong`, timeouts) retryable.
   - **Expected impact:** 4 fewer calls and about 15 s less per occurrence, for every `gh_retry gh api graphql` caller.
   - **Rollback/fail-open:** callers see the same non-zero exit, just sooner.
   - Finding check: new — no prior verdict on GraphQL validation classification. The pattern is absent at `gh_helpers.sh:2038`.

6. **Log listing counts when `ci_cancelled_rerun.py` skips a whole tick as `listing_truncated` (diagnostic).**
   - **Failure evidence:** in sweep 45928794875 every visible `CI_CANCELLED_RERUN` line was `action=skip reason=listing_truncated run=none`, so no cancelled CI run could be re-run that tick. The earlier sweep 45925828338 did not hit this.
   - **Root cause category:** fail-closed guard with no visible inputs. `list_runs` raises when `total_count != len(runs)` or `len(runs) == 100` for an active status (`scripts/ci_cancelled_rerun.py:382-384`). The log does not say which status or counts tripped it, so a stale `total_count` cannot be told apart from a real backlog of more than 100 runs.
   - **Exact fix:** before the `raise ValueError("listing_truncated")`, print `CI_CANCELLED_RERUN_LISTING status=<status> returned=<len> total_count=<count> outcome=truncated` (integers and enum only), with the prefix registered through an `agents.d/` fragment.
   - **Expected impact:** the next window can decide whether the guard needs pagination or just tolerance for a stale count.
   - **Rollback/fail-open:** log-only. The skip behaviour is unchanged.
   - Finding check: new — no prior verdict on this skip reason. The guard logs nothing at `:382-384` on this ref.

7. **Explain `REVIEWER_FINDINGS … outcome=unavailable reason=flagged_by_missing` (diagnostic).**
   - **Failure evidence:** run 45909271013 (PR 10178) emitted `unavailable` for all 5 reviewers. Only 2 of 456 review runs produced reviewer-findings lines this window.
   - **Root cause category:** observability gap. A single issue with an empty or unmatched `flagged_by` aborts attribution for the whole run (`scripts/review_findings_record.py:744-746`), and the emitted line (`:779`) does not say how many issues were affected.
   - **Exact fix:** count issues where `not matched` and where `has_unmatched`, and append `issues=<n> unattributed=<n> unmatched=<n>` to the `unavailable` tail. These are integer counts only, with no untrusted text.
   - **Expected impact:** the reviewer table becomes trustworthy, or the precise attribution defect becomes visible.
   - **Rollback/fail-open:** additive fields. The `counted` path is unchanged.
   - Finding check: new — no prior verdict on `REVIEWER_FINDINGS`. The early return without counts is present on this ref.

**Other observations (no change recommended):**
- **`BREAK_GLASS` / `CONTEXT_BUDGET_WARN`:** 0 and 0 in all 126 runs that had log telemetry. There is no rubric or prompt-size pressure.
- **Semble fallbacks:** 32, all `context=contract-test`, 4 in each of 8 CI runs (for example 45905990959, 45905037416 and 45918818903). There were 0 runtime fallbacks. This is healthy and already split out (`semble_contract_test_fallbacks` in `scripts/cost_audit.py`).
- **Serena:** 2 `SERENA_FALLBACK` lines in tele-funtoken `AI Validate [tracking:5842]` run 45835946261 (failed at `Enforce validation outcome`). There were 0 `SERENA_PROBE` lines. This looks like a rare fail-open, not a probe-availability outage. The reason field is unreadable until Reliability 1 lands.
- **One-off failures with no repeat across windows:**
  - CI [pr:10173] 45905037416 (`orchestrate-poll (1)` / review-pipeline plumbing contract test): CI on the same PR later passed (45920216234).
  - CI [pr:10085] 45918818903 (`tests-system-python (27)`).
  - `npm Audit Gate` 45742055116 and 45745357802 (multi-user-ai-agent; advisory-driven).
  - `Finalize Project 26` 45796069421.
  - CI [pr:10000] failed twice at the same step, `Merge-base freshness gate (pr_checks_lib) tests` (45887653625, 45905990959). That looks like a PR-specific regression the gate caught correctly.

## AI Memory Health

- **What was observed:**
  - No `AI_MEMORY_TELEMETRY:` lines are present in any `log_summary`. Every summary that mentions them says none were visible, often in heavily truncated logs (for example 45928011529 with about 590K chars truncated, and 45926355265).
  - Consumer pollers run with `PERSIST_JOURNAL_ENABLED=false`, `PERSIST_BUDGET_ENABLED=false` and `EVENTS_JSONL_ENABLED=false` (45926355265, 45925899126).
  - Because nothing was observed, retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` counts, and push retries cannot be computed.
- **Recommendation: verify emission once the run-logs mount is fixed.** After Reliability 1 lands, grep `recent/*/step-*.log` in the next window for `AI_MEMORY_TELEMETRY:` (`op=retrieve`) in poller and review runs. If there are still zero hits, compare against the emitter.
  - Finding check: unverified — the run-logs directory could not be read in the sandbox. The 2026-10-08 ledger records emission as already in place ("`AI_MEMORY_TELEMETRY` and `CONTEXT_BUDGET_WARN` are already emitted").

## GH API Call Audit

1. **Scheduled close cleanup (cancel_on_pr_close, 7 consumers): 5 wasted GraphQL calls per tick.**
   - **Evidence:** the 7 runs listed above, each `gh_retry` attempt 1/5 through 5/5 on `gh api graphql (flags: -f)`.
   - **Change:** Reliability 4 sends zero calls when there are no PR numbers. Reliability 5 stops retrying validation errors for every caller.
   - **Estimated reduction:** up to 840 GraphQL calls per day across consumers, with lower secondary-limit exposure on the shared `GH_PAT`.
   - Finding check: new — same as Reliability 4 and 5.

2. **Review `codex-agent` PR read cache: 1–2 hits against 4–5 misses per job.**
   - **Evidence:** `GH_PR_READ_CACHE hits=1 misses=4` (45925896322, 45925781395), `hits=1 misses=5` (45925825086, 45925625121) and `hits=2 misses=4` (45925802073), with `calls_in_job` of 35–44 (all PAT).
   - **Root cause:** the stats file already records `hit <kind>` / `miss <kind>` (`scripts/gh_helpers.sh:1623-1625`), but the summary at `.github/workflows/review_autofix.yml:6590` collapses kinds. That hides whether the misses are unavoidable first reads or repeated misses of the same kind.
   - **Exact change (diagnostic):** add a per-kind tally to the same echo, for example:
     ```bash
     by_kind=$(awk '{t[$2]++; if($1=="hit")h[$2]++} END{for(k in t){printf "%s%s:%d/%d", s, k, h[k]+0, t[k]; s=","}}' "${gpc_stats}")
     echo "GH_PR_READ_CACHE hits=… misses=… job=codex-agent by_kind=${by_kind:-none}"
     ```
   - **Size constraint:** check `wc -c .github/workflows/review_autofix.yml` against the 480,000-byte guard (§24). If headroom is tight, move this step body to `scripts/review_autofix_step_<slug>.sh`.
   - **Estimated reduction:** none directly. It identifies whether up to about 4 calls per review job are avoidable.
   - Finding check: new — no prior verdict on this summary line. It is kind-less on this ref.

3. **Auto-heal reconcile in tele-funtoken-msg-scoring rechecks the same 20 `human_review` sources every tick.**
   - **Evidence:** run 45928277441 logged `AUTO_HEAL_RECONCILE outcome=summary listed=40 processed=20 … in_progress=20 skipped_budget=20`, at about 0.25–0.6 s (about one API call) per item. Sources 5552 down to 5379 were starved by the `AUTO_HEAL_RECONCILE_MAX_ITEMS: 20` cap.
   - **Change:** order candidates least-recently-reconciled first, or defer `reason=human_review` sources until their issue's `updated_at` changes, so the skipped half rotates in.
   - **Estimated reduction:** about 20 per-item reads per tick that change nothing.
   - Finding check: unverified — `scripts/auto_heal_issue_dispatch.py` is not in this checkout (consumer-side script), so the ordering could not be read.

**Other observations (no change recommended):**
- **Sweep PAT budget:** `GH_PAT_BUDGET phase=end … remaining=1769 … calls_in_job=101 … pat:90` (45925828338) shows the shared PAT at about 65% used late in its hour. Sweep-wide GraphQL batching was rejected earlier (RISKY_SKIP), so no change is proposed.
- **No rate-limit events:** no HTTP 429 or secondary-limit events appear in any summary.
- **Repository hygiene rules (§14):** items 1 and 2 add no new calls. Item 1 removes calls.

## Prompt Cache & Memory System

- **Cache hit rate:**
  - `cache_hit_rate` is 0.797582 overall (cache reads / (reads + prompt + writes)).
  - coding-workflows is 0.776744, down from 0.877 in the 2026-11-25 window. tele-funtoken-msg-scoring is 0.860978, up from 0.495.
- **Where coding-workflows loses cache:**
  - Run 45925825086 (0.592) wrote 420,466 cache tokens in 12 calls, and 45925802073 (0.692) wrote 511,302.
  - Run 45925625121 (0.689) wrote 0 but still had 756,880 uncached prompt tokens.
  - Inference: per-PR dynamic content sits ahead of a stable prefix in some reviewer calls.
- **Budget pressure:** `context_budget_warn_count=0`, so cache loss is not driven by prompt growth near the window limit.
- **Fail-open behaviour:** `or_usage_unavailable_calls=0`, so usage reporting is complete.
- **No new recommendation.** Prompt-prefix reordering was rejected earlier (medium quality risk), and per-phase cache logging was rejected as measurement-only. This window gives no new evidence answering either reason. `scripts/cost_audit.py:766-769` already tracks per-phase prompt, completion and total tokens. If the hit rate keeps falling across two more windows, that trend would answer the "no defect shown" reason.
- **Memory retrieval:** no telemetry visible (see AI Memory Health).

## Orchestrator Health

1. **The `main` merge lease is held idle while 25 PRs wait, and the log cannot say why.**
   - **Evidence:**
     - Run 45928831552: `base=main holder=10021 held_s=8635` (about 2.4 h), `waiters=25`, and a second holder 10069 `hold_s=3914`.
     - Both show `outcome=held reason=holder_open holders=2`, with queue head `9359,9902,10054`. `MERGE_LEASE_TTL_MINUTES=240`.
   - **Root cause (observability):**
     - In the multi-holder path, `holder_open` (`scripts/review_merge_train.sh:2551-2555`) covers "not idle long enough yet", "CI-active state unknown" (`tm_runs_rc` other than 0 or 1, `:2473` / `:2497-2500`) and "no login match". Only `holder_runs_active` is broken out.
     - An idle holder can therefore block 25 waiters up to the 240-minute TTL with no signal of which branch kept it.
   - **Exact change (diagnostic):** set `tm_idle_state` to `not_due`, `active`, `idle_revoked`, `unknown` or `no_login` in the existing branches, and append `idle_check=${tm_idle_state:-not_due} idle_for_s=<now - tm_since>` to the `outcome=held` line. Do the same at `:2184` for the single-holder path.
   - **Expected impact:** the next window can tell whether idle revocation is failing (unknown CI state) or simply not yet due.
   - **Fail-open:** log-only. The existing metric call is unchanged.
   - Finding check: new — no prior verdict on `MERGE_LEASE` held reasons. Both held lines lack an idle classification on this ref.

2. **Queued runs older than any PR lifecycle are preserved forever.**
   - **Evidence:**
     - Run 45928831552 logged `preserved 4 active run(s) without pull-request linkage`.
     - Queued runs 34748435167 and 34748435180 date from 2026-09-13, and 32215723558 dates from 2026-08-19, for branches such as `ai/issue-3768`.
   - **Root cause:** scheduled cleanup only cancels runs whose linked PR resolves closed. Unlinked runs are always preserved, a decision that only logged them.
   - **Exact change:** in `cancel_on_pr_close.yml` scheduled mode, cancel unlinked runs with `status=queued` and `created_at` older than `CLOSE_CLEANUP_UNLINKED_MAX_AGE_DAYS`. That variable should default to 14 (§8) and be capped by the existing listing.
   - **Expected impact:** clears months-old wedged entries from active-run snapshots that the merge train and sweep read. This is low runner impact (inference: queued runs hold no slot).
   - **Fail-open:** age-gated, so in-flight unlinked runs (under 14 days) are untouched.
   - Finding check: recurring — the prior verdict "Close-cleanup unlinked-run logging: already-implemented" (also applied as `cancel-cleanup-unlinked-runs`) covered logging only, and the same months-old runs are still queued in this window.

**Health indicators:**
- **Pollers:** `orchestrate_poll` had 717 runs, 611 success, 0 failures and 106 cancelled. The cancellations are superseded ticks, which is expected coalescing.
- **Skip-heavy families:** `clarify` (189 runs, 170 skipped/other), `plan` (168 runs, 150 skipped) and `orchestrate_clarify_respond` (168 runs, 163 skipped) show no clarification loop.
- **Unblock judge:** #9709 is `stop=needs-human fixup=10073 outcome=waiting` (45925989667). #922 is `hold=placeholder_retired … outcome=holding` (45927823937, fun-token-multi-chain). #9618 is `triage_superseded outcome=noted` (45927666257).
- **Claude pool:** account `SVET2` is at `five_hour=1.0 status=rejected` (45927823937, 45927666257) and `0.99 allowed_warning` (45925989667), with 11–12 accounts still available. Track `CLAUDE_POOL available=… accounts=`.

## Pipeline Flow Bottlenecks

The bottlenecks are listed in order of end-to-end impact.

1. **Queueing (largest share).**
   - **Evidence:**
     - Review `gate` waits of 3m12s to 15m17s: 45925625121, 45925781395, 45925802073, 45925825086, 45925896322, 45927749316, 45926055571.
     - Poller waits of 4–260 s (for example 45927363161 about 4m20s, 45927181033 about 3.5m).
     - `pr-body-lint` waited about 5.5 min for about 6 s of work (45927645404).
     - The conditional deploy monitor waited about 4.5 min (45927678708).
   - **Cluster on 2026-11-26:** between 03:47 and 05:54 UTC, trivial families in every consumer took over an hour:
     - `AI Cancel on PR Close` 4,300–4,469 s (45755340371, 45755397401, 45755374453, 45755576589, 45755523671, 45755559791, 45755574167).
     - `AI Workflow Integrity` 3,629–4,285 s.
     - `AI Review` 4,580–7,537 s.
     - That pattern points to account-wide runner starvation (inference).
   - **Fix:** Speed 2, plus item 2 below.
   - Finding check: new — same as Speed 2. Queue-timestamp instrumentation is excluded because it was rejected earlier.

2. **Release and update fan-out bursts (inference).**
   - **Evidence:**
     - The 03:47–05:54 cluster above includes `AI Update Workflows` 45753899598 (4,201 s, created 04:14).
     - All consumers share the fixed cron `'0 4 * * *'` (`workflow-templates/ai-update-workflows.yml:49`).
     - The release dispatch loop posts `coding-workflows-stable-released` to every consumer back-to-back (`.github/workflows/test-and-mark-stable.yml:6503-6520`).
     - Every update PR then starts each consumer's PR-triggered checks.
   - **Root cause (inference):** synchronized fan-out across 7 repos into one shared runner pool.
   - **Exact change:** add a `sleep "${CONSUMER_DISPATCH_SPACING_SECS:-60}"` between consumer dispatches in the release loop. The variable defaults to 60, and 0 restores today's behaviour (§8).
   - **Expected impact:** spreads about 7 × (update + review + integrity + scan) jobs over about 7 minutes instead of 1. Release delivery is delayed at most about 6 minutes.
   - **Risk:** low.
   - Finding check: new — no prior verdict on dispatch spacing. The 2026-08-19 ledger defers "sweep cadence" as policy, which is a different surface and not a decision against this.

3. **Retry overhead.**
   - **Evidence:** #9442 resolver (6,304 s this window) and the cancel-cleanup GraphQL retries (about 16 s × 7 per hour).
   - **Fix:** Reliability 2–5.
   - Finding check: new — same as Reliability 2–5.

4. **Compute.**
   - **Evidence:**
     - Review `codex-agent` runs about 12.7–13 min (45925825086, 45925781395). `review_autofix` p50 is 1,104.5 s and p95 4,627.5 s.
     - `CI quick` self-test runs about 7–8.5 min of 515–550 s (45927988969, 45927645430).
     - Full CI has p50 1,052 s and p95 5,608 s, including 32 `tests-system-python` shards with no `max-parallel` (`.github/workflows/ci.yml:4476`).
   - **Fix:** no change proposed. CI shard timing was rejected earlier as measurement-only. Adding `max-parallel` would lengthen CI's own critical path, so it fails §26 "keep delivery fast" without queue data.
   - Finding check: not applicable — this is an observation, not a recommendation.

5. **Merge and conflict overhead.**
   - **Evidence:** the `main` lease has 25 waiters, with holder 10021 held 8,635 s.
   - **Fix:** Orchestrator Health 1 (diagnostic first).
   - Finding check: new — same as Orchestrator Health 1.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
Only part of this repository's runs are listed.
- **Bottlenecks:** review `gate` queueing (9–15 min); full CI p95 of about 93 min; the `main` merge lease with 25 waiters.
- **Failure modes:** the #9442 resolver loop (45910259066, 45920194253); PR-specific CI failures (45905990959, 45887653625, 45905037416, 45918818903).
- **Cost drivers:** OpenRouter, 56 calls and 23.4M tokens (`cache_hit_rate` 0.777); Semble, 16 queries and 178,129 bytes.
- **Top actions:**
  1. Mount run logs on the Claude log-analysis path (Reliability 1).
     - Finding check: new — `AI_ENGINE_INCLUDE_PATHS` is absent from `workflow-log-analysis.yml` on this ref.
  2. Parse `RESOLVER_TERMINAL` and persist terminal retry state (Reliability 2 and 3).
     - Finding check: new — no processing-report verdict; the gap is at `review_conflict_resolve.sh:3097`.
  3. Add idle classification to `MERGE_LEASE … outcome=held` (Orchestrator Health 1).
     - Finding check: new — the held lines lack it on this ref.

### shubhodeep1/tele-funtoken-msg-scoring
- **Bottlenecks:** poller runs up to 535 s (45928011529, about 76 s queued); the auto-heal reconcile cap starves 20 of 40 sources (45928277441).
- **Failure modes:** `AI Validate [tracking:5842]` 45835946261 at `Enforce validation outcome`, with 2 Serena fallbacks; the cancel-cleanup GraphQL failure (45928624524).
- **Cost drivers:** OpenRouter, 8 calls and 7.69M tokens (`cache_hit_rate` 0.861, review run 45755784922); Semble, 2 queries and 18,868 bytes.
- **Top actions:**
  1. Guard empty PR-state batches (Reliability 4).
     - Finding check: new — the 2026-11-24 report raised it with no processing verdict; still unguarded on this ref.
  2. Rotate auto-heal reconcile candidates (GH API 3).
     - Finding check: unverified — the consumer script is not in this checkout.
  3. Fold `non-retryable-backfill` into `provider-outage-resume` (Speed 2).
     - Finding check: new — no prior verdict; the split is at `workflow-templates/ai-review.yml:83-176`.

### shubhodeep1/multi-user-ai-agent
- **Bottlenecks:** a poller queue of about 2m24s (45928524275); provider-outage queue of 57 s (45928411377).
- **Failure modes:** `npm Audit Gate` ×2 (45742055116, 45745357802); `Finalize Project 26` 45796069421; cancel-cleanup GraphQL (45928656736).
- **Cost drivers:** no LLM telemetry in sampled runs.
- **Top actions:**
  1. Reliability 4.
     - Finding check: new — as above.
  2. Reliability 5.
     - Finding check: new — the validation-error pattern is absent at `gh_helpers.sh:2038`.
  3. Speed 2.
     - Finding check: new — as above.

### shubhodeep1/fun-token-multi-chain
- **Bottlenecks:** a 4,300–7,537 s cluster on 11-26 around 04:00 (45757529021, 45755574167).
- **Failure modes:** no failed runs; cancel-cleanup GraphQL (45928834799); unblock judge #922 holding (45927823937).
- **Cost drivers:** no LLM telemetry in sampled runs.
- **Top actions:**
  1. Space the release dispatches (Pipeline Flow 2).
     - Finding check: new — no prior verdict on dispatch spacing.
  2. Reliability 4.
     - Finding check: new — as above.
  3. Speed 2.
     - Finding check: new — as above.

### shubhodeep1/binance-blessings
- **Bottlenecks:** a poller queued about 4m20s (45927363161); cancel run 45928711307 queued about 72 s.
- **Failure modes:** no failed runs; cancel-cleanup GraphQL.
- **Cost drivers:** none measured.
- **Top actions:**
  1. Reliability 4.
     - Finding check: new — as above.
  2. Reliability 5.
     - Finding check: new — as above.
  3. Speed 2.
     - Finding check: new — as above.

### shubhodeep1/mongo-explorer
- **Bottlenecks:** poller queues of 35–98 s (45927911720, 45925899126); a 6,191 s AI Review in the 11-26 cluster (45754905516).
- **Failure modes:** no failed runs; cancel-cleanup GraphQL (45928703253).
- **Cost drivers:** none measured.
- **Top actions:**
  1. Reliability 4.
     - Finding check: new — as above.
  2. Reliability 5.
     - Finding check: new — as above.
  3. Pipeline Flow 2.
     - Finding check: new — as above.

### shubhodeep1/fbc_shutdown
- **Bottlenecks:** poller queue of about 78 s (45926130543); provider-outage queue of 59 s (45928420035).
- **Failure modes:** no failed runs; cancel-cleanup GraphQL (45928523313).
- **Cost drivers:** none measured.
- **Top actions:**
  1. Reliability 4.
     - Finding check: new — as above.
  2. Reliability 5.
     - Finding check: new — as above.
  3. Speed 2.
     - Finding check: new — as above.

### shubhodeep1/bitsafe.io
- **Bottlenecks:** security-hold-sweep queued about 2.5 min (45928427582); cancel run 45928647048 queued about 83 s of 112 s.
- **Failure modes:** no failed runs; cancel-cleanup GraphQL.
- **Cost drivers:** none measured.
- **Top actions:**
  1. Reliability 4.
     - Finding check: new — as above.
  2. Reliability 5.
     - Finding check: new — as above.
  3. Speed 2.
     - Finding check: new — as above.

## Metrics Appendix

**Run counts by repository**

| Repo | Runs | Success | Failure | Cancelled | Other/skipped | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| coding-workflows (truncated) | 997 | 392 | 6 | 24 | 575 | 8.0 | 3,251.8 |
| tele-funtoken-msg-scoring | 934 | 431 | 1 | 39 | 463 | 16.5 | 2,191.7 |
| multi-user-ai-agent | 327 | 204 | 3 | 9 | 111 | 215.0 | 2,060.3 |
| fun-token-multi-chain | 320 | 209 | 0 | 14 | 97 | 257.0 | 2,145.1 |
| binance-blessings | 292 | 195 | 0 | 11 | 86 | 268.5 | 2,160.5 |
| fbc_shutdown | 148 | 139 | 0 | 9 | 0 | 327.5 | 2,211.2 |
| mongo-explorer | 148 | 139 | 0 | 9 | 0 | 320.5 | 2,248.5 |
| bitsafe.io | 145 | 136 | 0 | 9 | 0 | 434.0 | 2,328.4 |
| **All** | **3,311** | **1,845** | **10** (0.30%; 0.51% of completed non-skipped) | **124** | **1,332** | **138** | **2,494** |

**Key workflow families**

| Family | Runs | Failures | p50 s | p95 s |
|---|---|---|---|---|
| review_autofix | 456 | 2 | 1,104.5 | 4,627.5 |
| orchestrate_poll | 717 (106 cancelled) | 0 | 531 | 2,030.4 |
| ci | 63 | 4 (6.3%) | 1,052 | 5,608 |
| cancel_on_pr_close | 213 | 0 | 263 | 2,281.2 |
| validate | 4 | 1 | 1,473 | 4,306.5 |
| npm_audit_gate | 12 | 2 | 274.5 | 988.3 |
| unblock_judge_dispatch | 42 | 0 | 107 | 1,214.9 |

**Tokens, cache and review telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 64 (64 / 0) |
| Prompt / completion tokens | 4,237,864 / 367,490 |
| Cache read / write tokens | 24,518,868 / 1,984,776 |
| Total tokens | 31,106,766 |
| `cache_hit_rate` (all / coding-workflows / tele-funtoken) | 0.797582 / 0.776744 / 0.860978 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all, 123 samples) | 102,000 / 8,737,420 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix, 30 samples) | 2,002,000 / 9,165,460 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| `claude_*` cost | not exported in `analysis_context.json` |

**GH API**

| Signal | Value |
|---|---|
| Review `codex-agent` `calls_in_job` | 35–44 (PAT only; 45925896322, 45925825086, 45925802073, 45925781395, 45925625121) |
| `GH_PR_READ_CACHE` hits / misses | 1/4, 1/5, 2/4, 1/4, 1/5 |
| Sweep `calls_in_job` / remaining | 101 / 1,769 (45925828338); 91 / 4,168 (45928794875) |
| Cancel-cleanup GraphQL failures | 7 runs × 5 attempts (one per consumer) |
| HTTP 429 / secondary limit | 0 observed |

**Semble and Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 18 / 196,997 (about 10.9 KB per query) |
| `semble_sources_total` / `static_dup_bytes` / `echo_lines_dropped` | 102 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 18 / 0 / 0 / 1,047,446 (about 58 s each) |
| `semble_fallbacks` (contract-test / runtime) | 32 (32 / 0) |
| Serena query / response bytes / tool calls / ms / fallbacks | 0 / 0 / 0 / 0 / 2 (45835946261) |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Reviewer findings:** 456 review runs. 2 runs had lines; 1 run was unavailable (5 lines, `flagged_by_missing`, 45909271013). PR 10173 round 4 showed `raised=0` for all 5 reviewers.

**Findings check**

| Status | Count |
|---|---|
| new | 12 |
| recurring | 1 |
| reopened | 0 |
| unverified | 2 |
| dropped: already fixed on this ref | 2 (Semble contract-test/runtime fallback split; close-cleanup unlinked-run logging) |
| dropped: previously decided | 4 (queue-timestamp instrumentation; poller `SEMBLE_AVAILABLE=false` under lazy mode; prompt-prefix and per-phase cache instrumentation; sweep-wide GraphQL batching) |

Counts are per unique finding. Restatements in Cost, Pipeline Flow and Per-Repo reuse the same finding.

**Data gaps**
- **Truncated repository:** `shubhodeep1/coding-workflows` (page limit reached).
- **Run-logs directory not readable:** `/home/runner/work/_temp/workflow-log-output` could not be read in the sandbox; the cause is Reliability 1.
- **Processing report:** `analysis/recommendation-processing-report.md` was read. It has no ledger entries for the 2026-11-24 or 2026-11-25 reports.
- **Consumer-side code:** `scripts/auto_heal_issue_dispatch.py` is not in this checkout.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 2 of 456 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 1.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | 1 | 0 | 0 | 0 | 0 |
| `google/gemini-3.1-flash-lite` | 1 | 0 | 0 | 0 | 0 |
| `minimax/minimax-m3` | 1 | 0 | 0 | 0 | 0 |
| `openai/gpt-6-luna` | 1 | 0 | 0 | 0 | 0 |
| `z-ai/glm-5.3-flash` | 1 | 0 | 0 | 0 | 0 |
