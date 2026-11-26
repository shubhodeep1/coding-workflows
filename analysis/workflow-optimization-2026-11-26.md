<!-- workflow-log-analysis: window_start=2026-11-25T01:24:00Z window_end=2026-11-26T02:10:22Z run=45740520924 -->
## Executive Summary

- **Data gaps: read this first.** Three repositories were not analysed. `shubhodeep1/fbc_shutdown`, `shubhodeep1/bitsafe.io` and `shubhodeep1/coding-workflows` failed when the collector hit "API rate limit exceeded for user ID 11442166" (HTTP 403). One repository was truncated, so only part of its runs is listed: `shubhodeep1/multi-user-ai-agent` reached the 1,000-run page limit. The run-logs folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox. Every log download attempted in this window also failed with the same 403. So this report rests on run rows, the `log_summary` fields and the current code. `insufficient_data` is false.
- **The collector caused most of its own data loss (Reliability 1 and 2).** The `errors[]` list has 82 rate-limit failures (34 jobs, 3 run listings, 45 logs) between 02:15:32 and 02:20:43 UTC.
  - Every one was retried, because `"rate limit"` is in `RETRY_MARKERS` (`scripts/collect_workflow_logs.py:83-93`). That is about 3 calls per endpoint, so roughly 246 wasted calls.
  - In run 45726978141 the shared PAT reported `reset=1795659703`, which is 02:21:43 UTC. If the collector uses that same PAT (an inference), the reset came about 60 s after the collector gave up.
  - `coding-workflows` is queued last (`workflow-log-analysis.yml:153`), so it was the first repository to be lost.
  - Fix: wait for the reset once, up to a bounded limit, then stop making calls. Collect `GITHUB_REPOSITORY` first.
  - Impact: all 3 failed repositories and 79 lost fetches would likely have been collected. Confidence: high on the mechanism, medium on the timing.
- **Waiting for a hosted runner is the largest latency, often more than 90% of a run.** Examples: the scheduled AI Review jobs in all 5 analysed consumers waited about 51–53 minutes for 1–12 s of work (45730207905, 45730384541, 45730497717, 45730177779). The npm audit run 45726977549 queued 26 minutes for 11 s of work. Review 45726978141 queued 26 + 52 minutes before 22 minutes of work.
  - Speed 1 combines the hourly scheduled jobs in `workflow-templates/ai-review.yml` into one job.
  - Impact: about 96–144 fewer runner jobs per day, and 9–14 minutes less latency for the security-hold sweep. Confidence: medium.
- **multi-user-ai-agent's npm Audit Gate failed 31 of 34 times, and that crowded other failures out of the log slots (Reliability 4 and 6).**
  - The failure is real. `basic-ftp` advisories GHSA-5PM8-86M8-9M6R and GHSA-R4JH-29PC-GFQR are not on the allowlist, on PR #376 (45738655934, 45726977549). Triage correctly folded the repeats into issue #382.
  - Log selection has no de-duplication. 14 of the 15 log slots went to this one failure. Failures in other places got no logs: tele-funtoken's implement step "Preflight destructive-commit guard" (×2), and a burst of 5 "Post …" steps failing at 15:40–15:49 UTC.
  - Confidence: high.
- **Scheduled closed-PR cleanup is broken in 4 of the 5 analysed consumers (Reliability 3, Speed 3).** Runs 45735952071, 45735887107, 45735803612 and 45735668123 each retried a malformed GraphQL query 5 times ("Field must have selections") and then cancelled nothing. This is the third window with this symptom, and the code on this ref is unchanged (`cancel_on_pr_close.yml:231-252`, `gh_helpers.sh:1814-1817`). Impact: stale runs are not cleaned up, and about 4 GraphQL calls plus 15–30 s of backoff are wasted per tick per consumer. Confidence: high on the symptom, medium on the exact trigger.
- **Claude-engine jobs spend 21–27 s probing 13 pool accounts one at a time, even when they only re-hold a blocked item (Speed 2, Cost 1).** Runs 45740104962, 45739409304, 45737297165 and 45735073227 all ended `op=hold … outcome=holding`. The hold check is API-only (`scripts/unblock_judge.sh:457-528`), but it runs after the credential probe. Confidence: medium.
- **Cost and cache:** 16 OpenRouter calls, 9,121,266 tokens, all in one review (45726978141). `cache_hit_rate` was 0.847, up from 0.685 in the 2026-11-25-2 report. There were 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` lines. Semble made 2 queries, 33,232 bytes, with 0 fallbacks. No cost change is proposed beyond Cost 1. Confidence: medium (one sampled run).

## Speed Optimizations

1. **Critical path: combine the hourly scheduled AI Review jobs into one runner job (§26).**
   - **Evidence:**
     - The `17 * * * *` schedule in `workflow-templates/ai-review.yml` starts up to three jobs per consumer: `non-retryable-backfill` (lines 83-106), `provider-outage-resume` (150-207) and `security-hold-sweep` (108-143). The sweep `needs:` the outage job, so it pays a second queue wait.
     - Observed queue waits against tiny amounts of work:

       | Repo | Run | Queue wait | Work |
       |---|---|---|---|
       | mongo-explorer | 45730403095 | sweep ~9 min after the outage job | ~1 s |
       | mongo-explorer | 45735190194 | sweep ~14 min | ~1 s |
       | binance-blessings | 45730384541 | ~51–53 min | ~4 s scan |
       | fun-token-multi-chain | 45730207905 | ~52 min | — |
       | multi-user-ai-agent | 45730497717 | ~51 min | — |
       | tele-funtoken-msg-scoring | 45730177779 | ~52 min | — |
       | tele-funtoken-msg-scoring | 45735048112 | ~21 min | ~5 s |

     - Every one logs "All GitHub-hosted runners with label [ubuntu-latest] are busy".
   - **Root cause:** Three runner jobs are used for three sequential scripts. Each one runs a checkout plus a single step of 1–12 s.
   - **Change:** Keep the `provider-outage-resume` job ID, since `needs:` and tests cite it. Move into it:
     - the backfill checkout and step, guarded by the backfill's current step-level `if:` vars and keeping `continue-on-error: true`;
     - the sweep checkout and step, guarded by `if: ${{ !cancelled() && vars.SINGLE_ISSUE_SECURITY_PASS_ENABLED == 'true' && steps.provider_outage.outputs.paused != 'true' }}`.

     Move the job-level `PROVIDER_OUTAGE_RESUME_ENABLED` guard onto the outage steps. Jobs per scheduled event go from 2–3 to 1. Tests to update: `tests/test_review_autofix_provider_outage_contract.py`, `tests/test_review_security_hold_sweep.py`, `tests/test_workflow_failure_heal_non_retryable_backfill.py` and `tests/test_update_workflows_guardrails.py`.
   - **Estimated savings:**
     - About 1–2 runner jobs per tick per consumer, roughly 96–144 jobs per day across the 5 analysed consumers. This is an inference: sweep logs appear only in mongo-explorer.
     - The sweep's second queue wait goes away, saving 9–14 minutes per tick.
   - **Risk:** Medium-low.
     - The backfill step would run in a job whose `github.token` can write to actions and issues. Today its job has `contents: read`. It uses `GH_PAT` for all its writes either way.
     - For strict least-privilege, combine only the sweep, whose permissions block is identical to the outage job's, and leave the backfill as its own job.
     - Rollback is a template revert. Consumers pick the change up on their next sync.
   - Finding check: new — the prior entry `scheduler/cadence retuning` (`analysis/recommendation-processing-report.md:795`) is deferred; the three separate jobs are present at `workflow-templates/ai-review.yml:83-207`.

2. **Critical path for every Claude-engine job: run the pool probes in parallel.**
   - **Evidence:** "Resolve Claude credential" took about 24 s in 45740104962, about 27 s in 45739409304, about 21 s in 45737297165 and about 26 s in 45735073227. Each logged `CLAUDE_POOL available=true reason=selected accounts=13`, at about 1.5–3 s per account.
   - **Root cause:** `scripts/claude_pool_token.sh:263-265` runs one `timeout … claude -p … "Reply OK"` per account, one after another.
   - **Change:** As proposed in `analysis/workflow-optimization-2026-11-23-2.md`:
     - Add `CLAUDE_POOL_PROBE_PARALLELISM`, defaulting to `4` (§8). The name is unused in code.
     - Run the per-account probes in the background with a `wait -n` gate.
     - Parse the transcripts in the original `accounts[@]` order, so the `CLAUDE_POOL probe` lines and the `probes` JSON keep the same order.
   - **Savings:** About 15–20 s per Claude-engine job. There were 36 unblock-judge runs alone in this window.
   - **Risk:** Low-medium. Each probe uses its own account token. A value of `1` restores today's behaviour.
   - Finding check: new — raised in the 2026-11-23-2 report with no processing verdict; the serial loop is still present at `scripts/claude_pool_token.sh:263-265`.

3. **Micro-optimization: stop retrying GraphQL validation errors.**
   - **Evidence:** 45735952071 (backoff 2/4/8/16 s), 45735887107 (1/2/4/8/16 s), 45735803612 and 45735668123 each logged "gh command failed after 5 attempts: gh api graphql (flags: -f)". The error text was the same on every attempt.
   - **Root cause:** `_is_gh_permanent_failure` (`scripts/gh_helpers.sh:1814-1817`) matches only HTTP 4xx and auth text. GraphQL validation errors do not match, so they are retried as transient.
   - **Change:** Add `|Field must have selections|is declared by .* but not used|Parse error on ` to that pattern.
   - **Savings:** About 15–31 s and 4 GraphQL calls per affected tick per consumer.
   - **Risk:** Low. Callers still fail open.
   - Finding check: new — raised in `analysis/workflow-optimization-2026-11-24.md` (Speed 2) with no verdict; the pattern is absent at `gh_helpers.sh:1816`.

## Cost Optimizations

1. **Skip pool probing and CLI installs when an unblock-judge item's hold is still in force.**
   - **Evidence:**
     - All 5 unblock-judge `log_summary` rows ended in a hold. Four were `hold=placeholder_retired` on items #941, #876 and #922 (`OPBNB_BASE_CUTOVER_RECORD_UNSET_OPERATOR_STEP`) and #5518 (`CS_HUB_WORKER_LOCKFILE_UNSET_OPERATOR_STEP`).
     - The judge step itself took 2–4 s. The job before it ran 13 Claude probe calls plus the Codex and Claude CLI installs.
     - There were 36 `unblock_judge_dispatch` runs in the window, with p50 660 s, mostly queue wait.
   - **Root cause:** In `.github/workflows/unblock_judge.yml` (step order at lines 140–160), "Install Codex CLI", "Install Claude Code CLI" and "Resolve Claude credential" all run before "Judge the blocked item". The hold re-check, `unblock_hold_condition` (`scripts/unblock_judge.sh:457-528`), uses only `gh api` and `git`.
   - **Change:** Add a cheap pre-check step after "Verify trusted unblock support". It reads the newest `ai:unblock-wait:v1` marker and calls `unblock_hold_condition`. When the result is `unmet`, it logs the existing `UNBLOCK_JUDGE … op=hold … outcome=holding` line and sets an output that skips the install and credential steps. `met`, `impossible` and `unknown` keep today's path, so the check fails open.
   - **Savings:** 13 probe calls, about 25 s and two CLI installs per held item per dispatch. This is an inference: the dispatch cadence per held item is not visible in this window.
   - **Quality risk:** Low, since only the `unmet` result short-circuits. The hold-expiry logic (`UNBLOCK_JUDGE_WAIT_MAX_DAYS=14`) must still be checked before skipping.
   - Finding check: new — no prior verdict on unblock-judge pre-credential short-circuiting; the step order is present at `unblock_judge.yml:140-160`.

- **Model and reasoning:** The unblock judge, the poller and the triage editor run `openai/gpt-6-sol` at `high` reasoning. No usage lines appear for those runs, so model spend cannot be judged. No change is proposed.
- **Semble** (review 45726978141): 2 queries, 33,232 bytes from 16 sources, `static_dup_bytes=0`, 2 bootstraps taking 34,082 ms in total (about 17 s each, under 1.3% of a 22-minute job). It is adding focused context without noise. In the pollers Semble is enabled but unavailable (`SEMBLE_AVAILABLE: false` in 45739642424, 45738429644 and others). No `SEMBLE_*` lines appear there, which is expected in lazy mode.
- **Serena:** 0 queries and 0 probes. It is neither replacing nor adding work.

## Reliability Improvements

1. **Collector: on a primary rate limit, wait once for the reset, then stop calling.** Expected impact: in a window like this one, all 3 failed repositories and 79 lost fetches would likely be collected.
   - **Failure evidence:**
     - 82 `errors[]` entries, 02:15:32–02:20:43 UTC, all "API rate limit exceeded for user ID 11442166". Their roughly 3.8 s spacing matches 3 attempts with 1 s and 2 s backoff.
     - The repository listings for fbc_shutdown, bitsafe.io and coding-workflows were lost.
     - Review 45726978141 logged `GH_PAT_BUDGET … remaining=2571 reset=1795659703` (02:21:43 UTC) at 01:52:52.
   - **Root cause category:** Retry policy. `gh_api_json` and `gh_api_bytes` (`collect_workflow_logs.py:523-561` and `587-608`) treat a primary rate limit like a transient error and keep calling the API.
   - **Exact fix:**
     ```python
     _PRIMARY_RATE_LIMIT_MARKER = "api rate limit exceeded"
     # on that marker: GET rate_limit (not counted against the core limit), and if
     # reset - now <= int(os.environ.get("WORKFLOW_LOG_RATE_LIMIT_MAX_WAIT_SECONDS", "300"))
     # and no wait has happened yet in this process: log WORKFLOW_LOG_RATE_LIMIT outcome=wait seconds=N,
     # sleep, and retry once. Otherwise set a module flag so later calls raise at once
     # (outcome=tripped) instead of calling gh.
     ```
     Both names are unused in the code. Register the new log prefix through an `agents.d/` fragment (§25).
   - **Rollback / fail-open:** Setting the variable to `0` disables the wait. A tripped flag only marks scopes as failed, as happens today.
   - Finding check: new — no prior verdict on collector rate-limit handling; `"rate limit"` is still in `RETRY_MARKERS` at `collect_workflow_logs.py:84`.

2. **Collect the home repository first.** Expected impact: coding-workflows is not the first repository lost when the budget runs out.
   - **Failure evidence:** coding-workflows was the last repository requested and it failed.
   - **Root cause:** `workflow-log-analysis.yml:153` appends `${GITHUB_REPOSITORY}` after the registry entries.
   - **Fix:** Use `repos=("${GITHUB_REPOSITORY}")` before the registry loop. The existing `seen` de-duplication at lines 156–159 keeps the list unique.
   - **Rollback:** This only changes order.
   - Finding check: new — no prior verdict; the ordering is present at `workflow-log-analysis.yml:143-153`.

3. **Guard the cancel-on-close PR-state batch so it never sends an empty selection, and log the batch size.** Expected impact: scheduled cleanup works again in 4 consumers.
   - **Failure evidence:** In 45735952071, 45735887107, 45735803612 and 45735668123, "Field must have selections (field 'repository' …)" was followed by "could not resolve PR-state batch 1; preserving affected runs" and then "found no cancellable…".
   - **Root cause category:** Input validation.
     - The batch loop (`cancel_on_pr_close.yml:231-252`) always sends `repository(owner:$owner,name:$name){…}`, even when it added no aliases.
     - Inference: `pr-numbers.txt` (built at lines 309–312) is empty when every active run has empty `pull_requests`, for example dispatched runs. If the ubuntu-26.04 `split` creates an empty part file in that case, the query has an empty selection.
   - **Exact fix:**
     ```bash
     [ -s "${numbers_file}" ] || return 0
     alias_count=0                       # increment inside the while loop
     echo "CLOSE_CLEANUP_PR_STATE batch=${batch_index} aliases=${alias_count}"
     [ "${alias_count}" -gt 0 ] || continue
     ```
     Register the new prefix through an `agents.d/` fragment.
   - **Rollback / fail-open:** Unresolved PRs are still preserved.
   - Finding check: new — raised in the 2026-11-24 report (Reliability 2) with no processing verdict; the guard is still absent at `cancel_on_pr_close.yml:231-252`.

4. **Give one log slot to each distinct failure before repeating a failure that already has one.** Expected impact: the failures that went undiagnosed in this window get logs.
   - **Failure evidence:**
     - 14 of the 15 log attempts went to multi-user-ai-agent `npm Audit Gate` / `Run npm run audit:ci` (45738655934 … 45670440637). The 15th was Finalize Project 26 (45671190918).
     - These failures stayed `not_selected`:
       - tele-funtoken `implement` "Preflight destructive-commit guard": 45623663142 and 45621820416;
       - tele-funtoken `plan` "Post implementation plan": 45648120535;
       - multi-user-ai-agent clarify "Orchestrator-managed fast path": 45647806741;
       - multi-user-ai-agent "Post check-failure triage issue": 45648226278, 45648729388 and 45649033337;
       - tele-funtoken lint `ruff-exception-gate`: 45644507074;
       - tele-funtoken `Run conditional deploy monitor`: 45652569777.
     - The burst of 5 failures in "Post …" GitHub write steps at 15:40–15:49 UTC has no explanation.
   - **Root cause:** `select_notable_runs_for_logs` (`collect_workflow_logs.py:1358-1402`) orders failures only by creation time. It de-duplicates by run, not by failure point.
   - **Fix:** First pass: the newest run for each `(repository, workflow_name, failure_point.job_name, failure_point.step_name)`. Second pass: the remaining duplicates. This is the key proposed on 2026-11-24.
   - **Rollback:** Ordering only; no fields change.
   - Finding check: new — the 2026-11-24 report (Reliability 1) has no processing verdict; the de-duplication is still absent at lines 1394–1402.

5. **check_failure_triage: keep the `install-claude` action directory so its post step can run.**
   - **Failure evidence:** 45739282961 and 45730115853 logged `##[error]Can't find 'action.yml'… under .codex-workflow-src/.github/actions/install-claude` at job cleanup, although both jobs succeeded.
   - **Root cause:** `check_failure_triage.yml:622` deletes every action directory except `claude-pool-token`. `install-claude` (line 441) uses `actions/cache@v5` (`.github/actions/install-claude/action.yml:32`), whose post step needs that `action.yml`.
   - **Fix:** `… ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +`, and update the comment at lines 615–617.
   - **Expected impact:** Removes a false `##[error]` annotation from successful triage runs, and lets a cache miss actually save the cache.
   - **Rollback:** One-line revert. The sandbox snapshot still excludes `.codex-workflow-src`.
   - Finding check: new — no prior verdict; the deletion is present at `check_failure_triage.yml:622`.

6. **multi-user-ai-agent npm audit: resolve the `basic-ftp` advisories.**
   - **Evidence:** 31 of 34 `npm_audit_gate` runs failed, all multi-user-ai-agent. The sampled runs show `[audit:ci] Found 2 unallowlisted vulnerability finding(s)` for GHSA-5PM8-86M8-9M6R and GHSA-R4JH-29PC-GFQR. The allowlist loaded 3 entries, so the gate is working as designed.
   - **Fix:** In the consumer, upgrade the dependency that pulls in `basic-ftp`, or add allowlist entries with an expiry, as decided on issue #382.
   - **Expected impact:** Removes 31 failures and about 31 triage and heal fan-out runs.
   - Finding check: unverified — consumer repository code is not readable from this checkout.

`BREAK_GLASS`: 0. `CONTEXT_BUDGET_WARN`: 0. There were no `SEMBLE_FALLBACK`, `SERENA_FALLBACK` or `SERENA_PROBE` lines, and 0 runtime or contract-test fallbacks.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` line appears in any `log_summary`. Deep-dive logs were unavailable: the folder was not mounted and every download returned 403. So retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled: false` cannot be computed. The runs with summaries are mostly schedulers that make no memory calls. The only AI-heavy run, review 45726978141, had about 1.69 M characters of log truncated. Recommendation: no code change. Verify emission once Reliability 1 restores log collection. This is a data-collection step, not a finding.

## GH API Call Audit

1. **Collector: about 246 calls spent after the limit was already exhausted** (82 endpoints × about 3 attempts; the per-endpoint count is an inference from the spacing).
   - **Change:** Reliability 1 cuts this to 1 call per endpoint plus 1 `rate_limit` read. It also removes the 403s the collector adds to the shared user budget.
   - Finding check: new — `collect_workflow_logs.py:83-93`, `523-561`.
2. **`cancel_on_pr_close` scheduled tick: 5 GraphQL calls that can never succeed, in each of 4 consumers.**
   - **Change:** Reliability 3 plus Speed 3. An empty batch costs 0 calls; a malformed query costs 1 instead of 5. That saves about 16–20 calls per tick fleet-wide.
   - **API hygiene (§14):** The existing batched helper is kept.
   - Finding check: new — `cancel_on_pr_close.yml:231-252`, `gh_helpers.sh:1816`.
3. **The shared PAT ran at about 5,300 calls per hour.**
   - **Evidence:** `used_in_job=2005` over 22.6 minutes (01:30:14–01:52:52) in 45726978141.
   - That number is a delta on the shared token, not usage by that job. The code at `review_autofix.yml:6514-6525` subtracts two `rate_limit` readings, so it includes every concurrent workflow in all repositories.
   - At that pace, the 2,571 remaining calls would last until about the reset, and they were gone by 02:15. This is an inference.
   - No new emitter is proposed. Indicator to track: `GH_PAT_BUDGET remaining` at the start of the collector job.
   - Finding check: new — route-level API accounting was deferred (`analysis/recommendation-processing-report.md:1043`, `:985`), and that cause is still present. This item records the pacing evidence; Reliability 1 is its concrete fix.
4. **Auto-heal reconcile processes items one at a time, and each tick's budget is used up on items parked for human review.**
   - **Evidence:** tele-funtoken 45735191754 and 45730370966: `listed=39 processed=20 in_progress=19 skipped_budget=19`, with about 0.22 s per item and the same `human_review` sources on every tick.
   - **Change:** Skip `human_review` sources before the per-item lookup, and batch the rest with aliased GraphQL (§14).
   - Finding check: unverified — `AUTO_HEAL_RECONCILE` is not emitted by any file in this repository, so it is consumer code.

Healthy patterns seen:
- No HTTP 429 or secondary-rate-limit lines in any `log_summary`.
- The workflow-integrity status POST runs only in `pull_request` mode (45738653904).
- The `GH_PR_READ_CACHE` instrumentation exists in review.

## Prompt Cache & Memory System

- **Cache behaviour:**
  - `cache_hit_rate` 0.847 (`or_cache_read_tokens` 7,598,954, `or_cache_write_tokens` 449,476, prompt 921,594).
  - 16 of 16 calls reported usage, so the value is not null this time.
  - This is one run (45726978141, PR #376 review), so confidence is low. It is up from 0.685 in `analysis/workflow-optimization-2026-11-25-2.md`.
- **Fragmentation:** None can be seen. Prefix-hash diagnostics were rejected before (report line 1258), so they are not raised again.
- **`CONTEXT_BUDGET_WARN`:** 0, so prompt growth is not eroding cache value in the sampled run.
- **Tool caches:**
  - In AI Plan 45725324066, `Failed to save … key codex-v0.114.0-v2 … cache write denied: token has no writable scopes` means plan runs started by `issue_comment` re-install Codex each time (`plan.yml:138-148`). Cost: about 3–10 s per run (inference).
  - The cause is GitHub cache-service scoping, which is unverified. Next collection step: count the same warning across `issue_comment`-triggered phases once logs are readable.
- No recommendation in this section; nothing here needs a finding check.

## Orchestrator Health

- **Indicators this window:**
  - `orchestrate_poll`: 336 of 494 succeeded with 0 failures. The 158 cancellations are `cancelled_before_first_step` coalesced ticks, already classified by the collector.
  - `orchestrate`: 3 of 3 succeeded (p50 3,154 s).
  - `orchestrate_clarify_respond`: 12 succeeded, 181 skipped.
- **Issue #382 (multi-user-ai-agent) is waiting on a human, by design.**
  - The plan posted `STATUS: NOT_CLEAR` with Q1 and Q2 on the `basic-ftp`/`hono` audit gate.
  - Clarify-respond 45726564526 skipped it: `reason=not_orchestrator_managed` and then `reason=not_clarify_questions`. That is the intended gate (`orchestrate_clarify_respond.yml:203-207`; #8650 loop fix).
  - Indicator: how long `ai:clarification` stays open on issue #382.
- **Unblock-judge holds that need a person:** fun-token #941, #876 and #922 (`OPBNB_BASE_CUTOVER_RECORD_UNSET_OPERATOR_STEP`), and tele-funtoken #5518 (`CS_HUB_WORKER_LOCKFILE_UNSET_OPERATOR_STEP`). Each completes only when a repository variable or secret with that name exists. Indicator: count of `outcome=holding` per item per day.
- **Auto-heal backlog:** tele-funtoken reconcile has 39 listed against a cap of 20 per tick, with 19 parked for human review (see GH API 4).
- **Smallest safe mitigation:** Cost 1, which stops held items from paying for probes and installs on every dispatch. The operator steps themselves need a person (see the §20 note in the recommendation section).
  - Finding check: new — same item as Cost 1; `unblock_judge.yml:140-160`.

## Pipeline Flow Bottlenecks

The dominant costs, in order:

1. **Queueing** for hosted runners. In review 45726978141, 78 of 101 minutes were queue: `gate` waited 26 minutes and `codex-agent` 52 minutes. Pollers wait 12–49 minutes for 1.5–8 minutes of work (45730075627, 45729990320, 45728836393). Scheduled AI Review jobs wait about 51–53 minutes. npm audit runs queue 5–26 minutes for 11 s.
2. **Compute:** review runs take 22 minutes of active work. `review_autofix` p50 is 1,486 s and p95 is 6,790 s; the slowest run was 10,122 s (tele 45595841931).
3. **Retry overhead:** collector 403 retries, about 246 calls (Reliability 1). cancel-on-close GraphQL retries take 15–31 s per tick (Speed 3).
4. **Fan-out of repeat failures:** 31 npm audit failures each trigger triage and heal listeners. There were 658 triage runs (628 skipped) and 291 heal runs (289 skipped). The skips cannot be filtered at the trigger, which matches the earlier "normal trigger filtering" verdict.

Fixes in order of end-to-end impact:

- Speed 1 (fewer scheduled runner jobs). Finding check: new — `workflow-templates/ai-review.yml:83-207`; prior entry deferred (report line 795).
- Reliability 6 (clear the npm audit failure loop at the source). Finding check: unverified — consumer code.
- Speed 2 (parallel probes). Finding check: new — `claude_pool_token.sh:263-265`.

Merge and conflict overhead cannot be seen this window: no merge-lease or resolver lines appear in the summaries apart from `MERGE_TRAIN_RELEASE_SUMMARY examined=0`.

## Per-Repo Breakdown

### shubhodeep1/multi-user-ai-agent (truncated)
- **Runs:** 1,000 (page limit reached). 311 succeeded, 41 failed (4.1%), 28 were cancelled and 620 were skipped or other. p50 7 s, p95 1,934 s.
- **Top failure modes:**
  - `npm_audit_gate`: 31 failures, `basic-ftp` advisories.
  - Finalize Project 26 "Verify Phase 3 parity": 3 of 3 failed (45671190918, 45650821319).
  - 3 "Post check-failure triage issue" failures at 15:42–15:49 UTC.
  - 21 failures whose jobs could not be fetched because of the 403.
- **Cost driver:** the only token spend in the window, 9.12 M tokens, in review 45726978141.
- **Actions:**
  1. Resolve `basic-ftp` / the issue #382 decision. Finding check: unverified — consumer code not readable.
  2. Log-slot de-duplication. Finding check: new — `collect_workflow_logs.py:1394-1402`.
  3. Keep `install-claude` in the triage cleanup. Finding check: new — `check_failure_triage.yml:622`.

### shubhodeep1/tele-funtoken-msg-scoring
- **Runs:** 944. 411 succeeded, 5 failed (0.5%), 58 were cancelled. p95 3,163 s.
- **Failures:**
  - 2× implement "Preflight destructive-commit guard" (45621820416, 45623663142);
  - plan "Post implementation plan" (45648120535);
  - ruff exception gate (45644507074);
  - deploy monitor (45652569777).
  None of them has logs.
- **Other bottlenecks:** 52-minute scheduled waits. The deploy monitor's full-history checkout takes about 2 minutes (45737483600).
- **Actions:**
  1. Speed 1. Finding check: new — `workflow-templates/ai-review.yml:83-207`.
  2. Reliability 3 (45735668123). Finding check: new — `cancel_on_pr_close.yml:231-252`.
  3. Reliability 4, so the destructive-guard failures get logs. Finding check: new — `collect_workflow_logs.py:1358-1402`.

### shubhodeep1/fun-token-multi-chain
- **Runs:** 593. 307 succeeded, 0 failed, 43 were cancelled. p95 3,557 s.
- **Bottlenecks:** the 52-minute scheduled wait (45730207905). The unblock judge holds #876, #922 and #941, each about 25 s of serial probing.
- **Actions:**
  1. Cost 1. Finding check: new — `unblock_judge.yml:140-160`.
  2. Speed 2. Finding check: new — `claude_pool_token.sh:263-265`.
  3. Reliability 3 (45735952071). Finding check: new — `cancel_on_pr_close.yml:231-252`.

### shubhodeep1/binance-blessings
- **Runs:** 635. 282 succeeded, 0 failed, 50 were cancelled. p50 39 s, p95 3,198 s.
- **Bottlenecks:** queueing of up to 53 minutes (45730384541, 45735457786). cancel-on-close is broken (45735887107).
- **Actions:**
  1. Reliability 3. Finding check: new — `cancel_on_pr_close.yml:231-252`.
  2. Speed 3. Finding check: new — `gh_helpers.sh:1816`.
  3. Speed 1. Finding check: new — `workflow-templates/ai-review.yml:83-207`.

### shubhodeep1/mongo-explorer
- **Runs:** 148. 123 succeeded, 0 failed, 25 were cancelled. p50 886 s, p95 3,250 s.
- **Bottlenecks:** `security-hold-sweep` waits a second time in the queue for 1 s of work (45730403095, 45735190194). cancel-on-close is broken (45735803612).
- **Actions:**
  1. Speed 1. Finding check: new — `workflow-templates/ai-review.yml:108-143`.
  2. Reliability 3. Finding check: new — `cancel_on_pr_close.yml:231-252`.
  3. Speed 3. Finding check: new — `gh_helpers.sh:1816`.

### shubhodeep1/fbc_shutdown (failed: not analysed)
- Its run listing returned 403 (rate limit), so no data.
- **Action:** Reliability 1. Finding check: new — `collect_workflow_logs.py:84`.

### shubhodeep1/bitsafe.io (failed: not analysed)
- Its run listing returned 403 (rate limit), so no data.
- **Action:** Reliability 1. Finding check: new — `collect_workflow_logs.py:84`.

### shubhodeep1/coding-workflows (failed: not analysed)
- Its run listing returned 403, and it was the last repository requested.
- **Action:** Reliability 2. Finding check: new — `workflow-log-analysis.yml:153`.

## Metrics Appendix

**Runs and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Fleet | 3,320 | 1,434 | 46 | 204 | 1,636 | 1.4% | 19 | 3,065 |
| multi-user-ai-agent | 1,000 | 311 | 41 | 28 | 620 | 4.1% | 7 | 1,934 |
| tele-funtoken-msg-scoring | 944 | 411 | 5 | 58 | 470 | 0.5% | 12.5 | 3,163 |
| binance-blessings | 635 | 282 | 0 | 50 | 303 | 0% | 39 | 3,198 |
| fun-token-multi-chain | 593 | 307 | 0 | 43 | 243 | 0% | 296 | 3,557 |
| mongo-explorer | 148 | 123 | 0 | 25 | 0 | 0% | 886 | 3,250 |

**Workflow families of note**

| Family | Runs | Success / Fail / Cancel | p50 s | p95 s |
|---|---|---|---|---|
| review_autofix | 275 | 268 / 0 / 7 | 1,486 | 6,790 |
| orchestrate_poll | 494 | 336 / 0 / 158 | 817 | 2,993 |
| npm_audit_gate | 34 | 1 / 31 / 2 | 296 | 1,997 |
| cancel_on_pr_close | 155 | 155 / 0 / 0 | 719 | 3,143 |
| unblock_judge_dispatch | 36 | 36 / 0 / 0 | 661 | 3,018 |
| implement | 193 | 22 / 5 / 0 (166 other) | 2 | 2,202 |
| finalize_project_26 | 3 | 0 / 3 / 0 | 448 | 2,010 |
| check_failure_triage | 658 | 27 / 3 / 0 (628 other) | 1 | 11 |

**Cost and telemetry**

| Metric | Value |
|---|---|
| OpenRouter calls (usage available / unavailable) | 16 (16 / 0) |
| Total / prompt / completion tokens | 9,121,266 / 921,594 / 153,706 |
| Cache read / write tokens | 7,598,954 / 449,476 |
| `cache_hit_rate` | 0.847 (review_autofix, multi-user-ai-agent); null elsewhere (no calls) |
| Codex calls / tokens | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (fleet, n=100) | 1,004,500 / 3,811,800 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` per repo | binance 1,539,500 / 3,209,830; fun-token 1,228,000 / 3,152,080; mongo 1,353,500 / 3,572,400; multi-user 11,000 / 5,011,050; tele 1,386,000 / 3,732,840 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**Semble and Serena**

| Metric | Value |
|---|---|
| Semble queries / bytes / bytes per query | 2 / 33,232 / 16,616 |
| Semble sources / `static_dup_bytes` | 16 / 0 |
| Semble bootstraps (failed / unused) / total ms | 2 (0 / 0) / 34,082 |
| Semble fallbacks (runtime / contract-test) | 0 (0 / 0) |
| Serena queries / tool calls / response bytes / fallbacks | 0 / 0 / 0 / 0 |

**MCP availability by target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all workflows) | 0 | 0 | 0 |
| Semble (pollers: `SEMBLE_AVAILABLE=false`, lazy mode) | n/a | n/a | n/a |

Other MCP servers observed: none.

**GH API**

| Signal | Value |
|---|---|
| Collector 403 rate-limit errors (jobs / runs / logs) | 82 (34 / 3 / 45), 02:15:32–02:20:43 UTC |
| Estimated collector calls wasted after exhaustion | about 246 (inference: about 3 attempts each) |
| Shared-PAT reset reading (45726978141) | `remaining=2571 reset=1795659703` (02:21:43 UTC) |
| Shared-PAT delta over 22.6 minutes | 2,005 calls (fleet-wide, not job-attributed) |
| cancel_on_pr_close wasted GraphQL attempts | 5 × 4 consumers = 20 |
| HTTP 429 / secondary rate-limit lines | 0 |

**Findings check**

| Status | Count |
|---|---|
| new | 9 (Speed 1–3, Cost 1, Reliability 1–5) |
| recurring | 0 |
| reopened | 0 |
| unverified | 2 (Reliability 6, GH API 4: consumer code) |
| dropped: already fixed on this ref | 1 (poller pre-step cancellation classification: `cancelled_before_first_step` is already emitted by the collector) |
| dropped: previously decided | 2 (queue-latency instrumentation rejected at report line 1571; answering plan-phase clarifications automatically was decided against in #8650, gate at `orchestrate_clarify_respond.yml:203-207`) |

**Data gaps**
- Repositories that failed: fbc_shutdown, bitsafe.io, coding-workflows.
- Repository truncated: multi-user-ai-agent.
- The run-logs folder was not mounted, and every attempted log download returned 403.
- `analysis/recommendation-processing-report.md` was readable, but it has no verdicts for the 2026-11-20 to 2026-11-25 reports.
- The consumer pin `6fe11eeda300a383fdbf5634153e1777d3abc268` could not be compared with this checkout (HEAD is `refs/heads/isolated`), so the cancel-on-close trigger is an inference.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 0 of 275 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 0.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| No REVIEWER_FINDINGS lines in the window | - | - | - | - | - |
