<!-- workflow-log-analysis: window_start=2026-11-24T00:40:50Z window_end=2026-11-25T01:24:00Z run=45521010534 -->
## Executive Summary

- **PR #9442 keeps failing in the conflict resolver.** The step `review / codex-agent` → `Run Codex resolver, validate, stage, commit` failed 3 more times this window: runs 45460702687 (5,026 s), 45478625333 (7,127 s) and 45502713160 (7,008 s). That is about 5.3 runner-hours, on top of 3 failures in the 2026-11-20 window. The code has a gap that lets this go on without escalating. When a resolver failure has no fingerprint violations (for example a timeout), the retry state is never saved (`scripts/review_conflict_resolve.sh:3096-3097`). Any change of head SHA also resets the count (`:3116-3119`). Escalation needs 20 identical failures (`threshold × 4`, `:3122`). Impact: about 5 runner-hours per window on one PR. Confidence: medium on the code gap, low on whether it caused these particular runs, because the logs were not readable.
- **No script reads the resolver's `RESOLVER_TERMINAL` record.** The record exists (`scripts/review_conflict_resolve.sh:532`), but no Python script parses it, so this report cannot say how the #9442 runs failed. Parsing it into run telemetry is a low-risk diagnostic addition, and its consumer is this report. Confidence: high.
- **One head was pushed three times over the prompt budget, and each push also started a full review.** `CI quick` / `quick / static-checks` / `Prompt tier budget` failed 3 times in 18 minutes: 45459992755, 45461193016 and 45463238279. Review runs were created at the same timestamps (45459994426, 45461193135, 45463238771) and took about 6.25 runner-hours in total. Neither pre-push gate runs `tests/prompt_size_budget.py`: `scripts/` has no reference to it. Impact: up to about 6 runner-hours per incident. Confidence: low-medium. Pairing the runs by timestamp is an inference, and the pusher is unknown.
- **Hosted-runner queueing still dominates small jobs.** The bitsafe.io poller run 45520893436 waited about 33 minutes for about 3 minutes of work. Auto-release run 45520763499 waited 165 s of its 178 s. Earlier verdicts already decided both pacing and queue instrumentation, so no new change is proposed. Confidence: high.
- **Cost and cache are healthy overall but weak in one consumer.** `cache_hit_rate` is 0.857 across 24 OpenRouter calls (22.6M tokens). tele-funtoken-msg-scoring sits at 0.495, and in run 45383779337 cache writes (157,718) exceeded cache reads (149,157). Prompt-prefix work was rejected earlier, so this is an observation only. There were 0 `BREAK_GLASS` and 0 `CONTEXT_BUDGET_WARN` lines. Confidence: medium.
- **Data gaps.**
  - Truncated repositories (only part of their runs are listed; the 1,000-run page limit was reached): `shubhodeep1/tele-funtoken-msg-scoring` and `shubhodeep1/coding-workflows`. No repository failed.
  - The run-logs directory `/home/runner/work/_temp/workflow-log-output` was not present, so no deep-dive log excerpts could be read.

## Speed Optimizations

1. **Stop the #9442 resolver loop (critical path).** This shares a root cause with Reliability 2.
   - Evidence: 3 failures at the resolver step totalling 19,161 s this window, after 3 in the prior window (44646017321, 44650487981, 44657650075; about 4,394 s). Per-run duration has grown from about 25 minutes to 84–119 minutes. Each failing run did one Semble query (11,370 bytes, 6 sources).
   - Root cause (inference, from code): failures without fingerprint violations are never counted, and a head change resets the count. The tier ladder and escalation therefore never engage, and each new review cycle pays the full resolver budget again: 3 attempts × 50 min, with a 170-minute step cap (`.github/workflows/review_autofix.yml:4457-4478`).
   - Exact change: see Reliability 1 and 2.
   - Estimated saving: about 5 runner-hours per window for this PR. It also stops the PR re-entering the critical path every review cycle.
   - Risk: low for Reliability 1, medium for Reliability 2.
   - Finding check: new — no recorded verdict. `analysis/workflow-optimization-2026-11-20.md` flagged #9442 ("act only if #9442 reaches a 4th failure") but is not yet in `analysis/recommendation-processing-report.md`. The failure count is now at least 6.

2. **Run the prompt-budget check before pushing (micro-optimization with a large knock-on saving).**
   - Evidence: the 3 `Prompt tier budget` failures listed above. The matching review runs took 7,828 s, 6,924 s and 7,760 s on heads that fail a static gate.
   - Root cause: `tests/prompt_size_budget.py` runs only in CI (`.github/workflows/ci.yml:608-611`). Neither `scripts/review_autofix_step_prepush_test_gate.sh` nor `scripts/implement_step_prepush_test_gate.sh` reaches it, because its name is not a `test_*` module the selection picks up.
   - Exact change: see Reliability 3.
   - Estimated saving: 1,264 s of CI quick time plus up to about 22,500 s of review time per incident. This applies only when the pusher is the pipeline itself.
   - Risk: low. The script takes under a second and reads only files.
   - Finding check: new — the prior verdict ("run the prompt budget gate early: already-implemented", 2026-10 ledger) covers where the check sits in CI, not the pre-push gates. Current code has no pre-push call.

## Cost Optimizations

1. **Remove repeated resolver spend on #9442.**
   - Evidence: 19,161 runner-seconds this window. The failing runs logged `or_calls=0` and `codex_calls=0`. The resolver uses OpenCode, and `claude_*` cost fields are not exported, so model spend is unmeasured.
   - Root cause and change: Reliability 1 and 2.
   - Estimated saving: about 5.3 runner-hours per window, plus unmeasured model tokens.
   - Quality risk: none for the diagnostic. Saving retry state for timeout failures only changes when the existing tier ladder engages.
   - Finding check: new — no prior verdict. The code gap is at `scripts/review_conflict_resolve.sh:3096-3097` and `:3116-3122`.

2. **Avoid full reviews of heads that fail the prompt budget.**
   - Evidence: the 3 paired review runs (about 6.25 h). Their token totals are not in the exported telemetry: those runs are `not_selected`.
   - Change: Reliability 3.
   - Estimated saving: up to 3 review runs per incident.
   - Quality risk: none. The check is the same one CI enforces.
   - Finding check: new — same evidence as Speed 2.

Other observations (no change recommended):
- **Model and token profile.** 2,057,396 prompt tokens, 263,634 completion tokens, 19,154,805 cache-read tokens and 1,135,273 cache-write tokens. Run 45472313548 alone used 14.5M tokens over 8 calls (about 1.8M per call, hit rate 0.90).
- **Semble.** 10 queries, 112,735 bytes (about 11.3 KB each), 57 sources, `static_dup_bytes=0` and 0 echo lines dropped. That is bounded, non-duplicated context. Bootstrap took about 55 s per review run against runs of more than 2 h, well under 1%.
- **Serena.** No traffic at all: 0 queries and 0 probes.

## Reliability Improvements

1. **Parse `RESOLVER_TERMINAL` into run telemetry (diagnostic addition, do first).**
   - Failure evidence: 3 resolver-step failures on #9442 (45460702687, 45478625333, 45502713160) cannot be classified from the collected telemetry.
   - Root cause category: observability gap. `scripts/review_conflict_resolve.sh:532` prints `RESOLVER_TERMINAL run_id= run_attempt= pr= attempts= attempt_ms= exit_class= rc= fp_violations= markers=`, but no Python script reads it.
   - Exact fix: in `scripts/cost_audit.py`, add an anchored regex that uses the existing `_TELEMETRY_LINE_PREFIX_RE_SRC`, and emit additive per-run fields `resolver_terminal_exit_class`, `resolver_terminal_attempts` and `resolver_terminal_attempt_ms_total`. Add a fixture test next to `tests/test_cost_audit_semble_metrics.py`. Sketch:

     ```python
     RESOLVER_TERMINAL_RE = re.compile(
         _TELEMETRY_LINE_PREFIX_RE_SRC
         + r"RESOLVER_TERMINAL\s+run_id=\S+\s+run_attempt=\d+\s+pr=(?P<pr>\d*)\s+"
         r"attempts=(?P<attempts>\d+)\s+attempt_ms=(?P<attempt_ms>[0-9,]*)\s+"
         r"exit_class=(?P<exit_class>timeout|no_progress|markers|fingerprint|scope|commit|other)\b"
     )
     ```
   - Expected impact: the next report can tell timeout loops apart from markers or fingerprint loops for every resolver failure. That decides whether Reliability 2 is the right fix.
   - Rollback / fail-open: the fields are additive. An unmatched line leaves them null, and existing counters are unchanged.
   - Finding check: new — no prior verdict on parsing this record. The 2026-10-07-2 ledger applied only the emitter ("Reliability 1 … `RESOLVER_TERMINAL` record").

2. **Save resolver retry state for terminal failures that have no fingerprint violations.**
   - Failure evidence: 6 resolver failures on #9442 across two windows with no escalation visible in the telemetry.
   - Root cause category: logic gap (inference from code). `build_resolver_retry_state_artifact` returns `ok: False` ("no fingerprint failures detected for retry-state persistence") when there are no regressed or drifted fingerprints (`:3096-3097`). A timeout before any patch, or an exit with no progress, therefore never raises `consecutive_failure_count`. The caller only logs `::warning::Resolver retry-state artifact unavailable; skipping persistence` (`:3384-3387`).
   - Exact fix: when `RESOLVER_TERMINAL_EXIT_CLASS` is `timeout` or `no_progress` and no fingerprint failures exist, use one synthetic member `{"kind": "terminal", "fp_key": ["terminal", <exit_class>]}`. This mirrors the existing `isolation_failure` synthetic key at `:3081-3088`. Keep the same-head and same-signature counting rules unchanged.
   - Expected impact: repeated same-head timeouts move through the existing ladder (`strict` → `ratio` at 5) and reach the existing `ai:resolver-escalated` path, instead of re-running the full 150-minute budget indefinitely. Reliability 1 confirms whether #9442's heads stayed the same. If they did not, this fix will not stop its loop.
   - Rollback / fail-open: the builder already fails open (`:3375-3379`), and a variable switch can guard the change, in the same way as `RESOLVER_NON_RETRYABLE_ESCALATE_ENABLED`. Medium risk: it changes when tier downgrades happen for timeout loops.
   - Finding check: new — no prior verdict on this path. The gap is present at `scripts/review_conflict_resolve.sh:3096-3097` on this ref.

3. **Add the prompt-budget check to the pre-push gates when `prompts/**` changes.**
   - Failure evidence: 45459992755 (194 s), 45461193016 (529 s) and 45463238279 (541 s), all failing at `Prompt tier budget`.
   - Root cause category: gate gap. Limits are `DEFAULT 250 / LARGE 500 / XL 800` plus per-file operational limits (`tests/prompt_size_budget.py:13-24`), and neither pre-push gate script enforces them.
   - Exact fix: in `scripts/review_autofix_step_prepush_test_gate.sh` and `scripts/implement_step_prepush_test_gate.sh`, when the changed-file list contains `prompts/`, run `PYTHONDONTWRITEBYTECODE=1 python3 tests/prompt_size_budget.py` from the worktree. Treat a non-zero exit like a failing selected test, so the existing `repair` mode trims the prompt before pushing. If the script is missing (consumer repos), skip with a notice.
   - Expected impact: removes this failure class for pushes the pipeline makes itself. Human pushes are unaffected.
   - Rollback / fail-open: the check is skipped when the script is missing or when no prompt files changed.
   - Finding check: new — the prior verdict covered only placement in CI. There is no pre-push call on this ref.

Observations (no recommendation):
- **Semble fallbacks.** 12 contract-test fallbacks: 4 each in CI runs 45474796778, 45460191582 and 45460711937. Expected noise, already split out. 3 runtime fallbacks, all in the tele-funtoken `AI Implement` run 45326617768. That is rare, healthy fail-open behaviour; Semble bootstraps show 0 failed and 0 unused.
- **Serena.** No `SERENA_PROBE` or `SERENA_FALLBACK` lines.
- **One-off failures.** None repeats on the same step, and none has readable logs:
  - CI [pr:9944] `Telegram alert levels page only when a person must act` (45474796778). PR 9944's later review, 45504910113, succeeded.
  - CI [pr:9679] `Review/judge/conflict strict-render contract test` (45460191582).
  - tele-funtoken `Checkout PR head branch` (45329137070). `REVIEW_HEAD_CHECKOUT stage=` already carries the diagnosis.
  - tele-funtoken `Preflight destructive-commit guard` (45326617768). This is a deliberate fail-fast guard.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines were available. The run-logs directory was absent and no run row carries a `log_summary` with memory lines; the bitsafe summary says it "can't" assess them because of truncation. Retrieve hit rate, `estimated_tokens` against budget, `keyword_method` mix, `fail_open` and `enabled:false` counts cannot be computed.
- **Recommendation:** verify emission in the next window that has the log archive. Count `op=retrieve` lines with `records_selected>0` in review and implement runs. No code change is proposed.
  - Finding check: unverified — the run-logs directory `/home/runner/work/_temp/workflow-log-output` could not be read. Emission itself was recorded as already in place in the 2026-10-08 ledger ("`AI_MEMORY_TELEMETRY` and `CONTEXT_BUDGET_WARN` are already emitted").

## GH API Call Audit

- This window has no GH API evidence: no call counts, no `GH_PAT_BUDGET` or `GH_API_RETRY` lines in the input, and no rate-limit or 429 events. The two log summaries explicitly report none.
- No batching recommendation is made, because no run in this window supports one. The earlier deferred items (for example `BATCH-001` in `review_autofix.yml`, `REUSE-002` in `orchestrate_clarify_respond.yml`) remain deferred on this ref, but there is no new evidence to re-raise them.
- Indicator to track: `GH_PAT_BUDGET ... used_in_job=` per review run, once logs are collected.

## Prompt Cache & Memory System

- **Overall:** `cache_hit_rate` 0.857135 over 24 calls; read 19,154,805, write 1,135,273, uncached prompt 2,057,396.
- **coding-workflows:** 0.87693 over 16 calls; cache writes are 4.2% of reads. Per-run examples: 45472313548 at 0.900673 and 45489583096 at 0.826378.
- **tele-funtoken-msg-scoring:** 0.494715 over 8 calls; writes are 61% of reads. Run 45383779337 scored 0.486 with writes (157,718) above reads (149,157) over 2 calls. Run 45369525870 scored 0.498 over 6 calls.
  - Inference: the short consumer review runs make too few calls to pay back the cost of writing the cache.
  - No change is proposed. Prefix restructuring was rejected earlier pending a quality baseline, and `REVIEWER_PROMPT_COMPONENTS ... static_prefix_sha256=` already exists to confirm whether calls share a prefix.
- **Context pressure:** `CONTEXT_BUDGET_WARN` count 0 and `BREAK_GLASS` count 0. No prompt-size risk is visible.
- **Memory retrieval effectiveness:** not measurable this window (see AI Memory Health).

## Orchestrator Health

- **Poller:** `orchestrate_poll` had 694 runs: 505 success, 189 cancelled, 0 failures; p50 937.5 s, p95 2,646 s.
  - The cancellations are `cancelled_before_first_step` after 121–1,058 s queued (for example 45524452770, 45524194065, 45521963527). That is normal concurrency coalescing.
  - The successful bitsafe.io tick 45520893436 spent about 33 of 36.6 minutes waiting for a runner.
- **Phase wrappers:** clarify (214 of 234), plan (207 of 234), implement (187 of 209) and clarify-respond (202 of 208) are mostly skipped, with 0 failures.
  - The 4-wrapper fan-out per comment is visible at 00:42:41, 00:44:08, 00:45:11 (twice), 00:51:14, 00:53:12, 00:53:41, 00:54:58, 01:03:50, 01:13:59, 01:24:08 and 01:27:34.
  - Three implement wrappers were cancelled while queued after 534–618 s (45521780183, 45522898665, 45524192787).
- **Downstream jobs:** validate 45520418783 succeeded (819 s); `unblock_judge_dispatch` 27 of 27 succeeded; `integration_pr_readiness` 33 of 33 skipped, consistent with plan item 10. Auto-release run 45520763499 logged `AUTO_RELEASE_SKIPPED reason=release_in_flight active_runs=2`, a healthy guard.
- **Main pain point:** the integration and conflict-resolve loop on #9442 (Reliability 1 and 2). The resolver's EXIT trap hands off to the integration judge, but repeated failures do not escalate.
- **Indicators to track:**
  - `RESOLVER_TERMINAL exit_class` per PR (after Reliability 1)
  - count of `ai:resolver-escalated` labels
  - resolver failures per PR per 24 h
  - poller cancellations after the first step started (this should stay at 0)

## Pipeline Flow Bottlenecks

1. **Review/autofix (compute plus queue).**
   - coding-workflows `review_autofix` wall p50 is 8,449,500 ms and p99 13,655,010 ms. The longest runs are 45450325004 (13,714 s) and 45472243448 (13,367 s).
   - Inference: these are close to the 240-minute `codex-agent` job cap if queue time is excluded. The stage split is already in `REVIEW_AUTOFIX_RUN_SUMMARY_V1`.
2. **Retry overhead.**
   - #9442 resolver retries: 19,161 s (Reliability 1 and 2).
   - Reviews of heads that fail the prompt budget: about 22,500 s (Reliability 3).
   - These are the only retry costs this window that can be fixed in code.
3. **Queueing.** Hosted-runner waits on small jobs (45520893436 about 33 minutes; 45520763499 165 s). Runner capacity is a non-goal in `docs/plans/reduce-actions-run-volume-plan.md`, and pacing was rejected earlier.
4. **CI (compute).** 82 CI runs, p50 1,200.5 s, p95 5,987.75 s, longest 45460711937 at 10,216 s. Plan #9528 (`docs/plans/cut-ci-runner-time-plan.md`) already covers this.
5. **Merge and conflict.** The only visible conflict overhead is #9442.
6. **Order by end-to-end impact:** Reliability 1, then Reliability 2, then Reliability 3. The checks for these items are recorded under Reliability Improvements.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Bottlenecks:** review/autofix runs of 2–3.8 h; CI p95 about 6,900 s; runner queueing.
- **Failure modes:**
  - resolver failures on #9442 (3)
  - `Prompt tier budget` (3)
  - one-off CI contract-test failures (45474796778, 45460191582)
- **Cost drivers:** 16 OpenRouter calls and 21.4M tokens (cache hit 0.877); 8 Semble queries (80,898 bytes).
- **Actions:**
  1. Parse `RESOLVER_TERMINAL` into run telemetry (Reliability 1).
     - Finding check: new — the record is not parsed anywhere under `scripts/*.py` on this ref.
  2. Save resolver retry state for timeout and no-progress failures (Reliability 2).
     - Finding check: new — the gap is at `scripts/review_conflict_resolve.sh:3096-3097`.
  3. Run the prompt-budget check in the pre-push gates (Reliability 3).
     - Finding check: new — `scripts/` has no reference to `prompt_size_budget`.

### shubhodeep1/tele-funtoken-msg-scoring
- **Data gap:** truncated (1,000 runs listed).
- **Bottlenecks:** review runs of 1.6–2.1 h (45356770937 at 7,704 s; 45383779337 at 7,448 s).
- **Failure modes:** one `Checkout PR head branch` failure (45329137070) and one implement destructive-commit guard stop (45326617768, with 3 runtime Semble fallbacks).
- **Cost drivers:** 8 calls, 1.18M tokens, cache hit 0.495, with a high write-to-read ratio.
- **Actions:**
  1. Receives Reliability 2 through `@stable` (its resolver runs share `review_conflict_resolve.sh`).
     - Finding check: new — same code gap as above.
  2. Otherwise no action is warranted: each failure is a single occurrence of a guard that is already instrumented.

### shubhodeep1/binance-blessings
- **Bottlenecks:** a review outlier of 6,175 s (45394339366); `issue_pr_status` 5,853 s (45307679819).
- **Failure modes:** `Secret Scan` / `gitleaks` / `Scan Git history` failure (45392286424, 2,068 s).
- **Actions:**
  1. Triage the gitleaks result as a possible secret exposure. Do not suppress it.
     - Finding check: unverified — the run's log is not readable (run-logs directory absent), so a real finding cannot be told apart from a tool error.

### shubhodeep1/fun-token-multi-chain
- **Failure modes:** `opBNB Migration Credit` / `Stage A snapshot (dry run, no signing key)` / `Run step 009 snapshot (dry run)` failure (45387089496, 2,368 s). This is a repo-specific workflow outside the pipeline.
- **Actions:**
  1. Inspect the step 009 dry-run log before any non-dry-run stage runs.
     - Finding check: unverified — the run's log is not readable.

### shubhodeep1/bitsafe.io
- 145 runs, 0 failures.
- **Bottleneck:** poller queue wait of about 33 minutes (45520893436).
- No action warranted; the queue issue was decided earlier.

### shubhodeep1/fbc_shutdown
- 145 runs, 0 failures, p95 2,634 s.
- No action warranted.

### shubhodeep1/mongo-explorer
- 146 runs, 0 failures, p95 2,971.75 s.
- No action warranted.

### shubhodeep1/multi-user-ai-agent
- 167 runs, 0 failures. The skipped `Operator Step 264 Resume Plan` runs (45525620104, 45520364560) do nothing.
- No action warranted.

## Metrics Appendix

**Runs**

| Scope | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| All | 3,504 | 1,776 | 12 | 226 | 1,490 | 0.34% | 274 | 3,870 |
| coding-workflows | 1,000 | 422 | 8 | 38 | 532 | 0.8% | 10 | 6,901.2 |
| tele-funtoken-msg-scoring | 1,000 | 399 | 2 | 45 | 554 | 0.2% | 9 | 2,698.6 |
| binance-blessings | 546 | 268 | 1 | 33 | 244 | 0.18% | 237 | 2,908.8 |
| fun-token-multi-chain | 355 | 198 | 1 | 21 | 135 | 0.28% | 367 | 2,329.7 |
| multi-user-ai-agent | 167 | 119 | 0 | 23 | 25 | 0% | 737 | 2,422.6 |
| mongo-explorer | 146 | 124 | 0 | 22 | 0 | 0% | 933 | 2,971.8 |
| bitsafe.io | 145 | 123 | 0 | 22 | 0 | 0% | 1,033 | 2,861.0 |
| fbc_shutdown | 145 | 123 | 0 | 22 | 0 | 0% | 906 | 2,634.2 |
| family `review_autofix` | 443 | 432 | 4 | 4 | 3 | 0.9% | 2,461 | 8,235.1 |
| family `ci` | 82 | 75 | 5 | 2 | 0 | 6.1% | 1,200.5 | 5,987.8 |
| family `orchestrate_poll` | 694 | 505 | 0 | 189 | 0 | 0% | 937.5 | 2,646.1 |

**Tokens and review telemetry**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 24 (24 / 0) |
| Prompt / completion tokens | 2,057,396 / 263,634 |
| Cache read / cache write tokens | 19,154,805 / 1,135,273 |
| Total tokens | 22,610,772 |
| `cache_hit_rate` (all / coding-workflows / tele-funtoken) | 0.857135 / 0.87693 / 0.494715 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all, 86 samples) | 149,500 / 13,419,050 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (`review_autofix`) | 8,449,500 / 13,655,010 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |
| `claude_*` cost fields | not exported in `analysis_context.json` |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 10 / 112,735 (about 11.3 KB per query); coding-workflows 8 / 80,898, tele-funtoken 2 / 31,837 |
| `semble_sources_total` / `semble_static_dup_bytes_total` / `semble_echo_lines_dropped` | 57 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 10 / 0 / 0 / 554,543 (about 55.5 s each) |
| `semble_fallbacks` (contract-test / runtime) | 15 (12 / 3). Contract-test: CI 45474796778, 45460191582, 45460711937. Runtime: implement 45326617768 |
| Serena query / fallback / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**

| Metric | Value |
|---|---|
| Call counts / hotspots | not present in input |
| Rate-limit / 429 events | none reported (log summaries 45520893436 and 45520763499) |

**Retry cost (this window)**

| Item | Runs | Runner seconds |
|---|---|---|
| #9442 resolver failures | 45460702687, 45478625333, 45502713160 | 19,161 |
| `Prompt tier budget` CI quick failures | 45459992755, 45461193016, 45463238279 | 1,264 |
| Reviews paired by timestamp with those heads (inference) | 45459994426, 45461193135, 45463238771 | 22,512 |

**Findings check**

| Status | Count |
|---|---|
| new | 3 (Reliability 1, Reliability 2, Reliability 3; repeated in the Speed, Cost and Per-Repo sections) |
| recurring | 0 |
| reopened | 0 |
| unverified | 3 (AI memory emission, binance-blessings gitleaks triage, fun-token opBNB dry-run triage) |
| dropped: already fixed on this ref | 4 (Semble contract-test vs runtime split in `scripts/cost_audit.py`; `cancelled_before_first_step` classification in `scripts/collect_workflow_logs.py`; prompt-budget gate already early in parallel `static-checks`; review checkout `REVIEW_HEAD_CHECKOUT stage=` diagnostics) |
| dropped: previously decided | 6 (poller cancellation diagnostics; runner-queue pacing / dispatch bursts; queue and job timestamp instrumentation; prompt-prefix / cache restructuring; Semble latency tuning; extra review stage timings beyond `REVIEW_AUTOFIX_RUN_SUMMARY_V1`). The comment-router fix for the 4-wrapper fan-out is also not proposed: `docs/plans/reduce-actions-run-volume-plan.md` lists it as a non-goal, though that is not a processing-report verdict. |

**Data gaps**
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` was not present. No deep-dive excerpts, `RESOLVER_TERMINAL` lines, `AI_MEMORY_TELEMETRY` lines or GH API counts could be read. Only two runs (45520893436, 45520763499) carry a `log_summary`.
- Truncated repositories: `shubhodeep1/tele-funtoken-msg-scoring` and `shubhodeep1/coding-workflows` (`page_limit_reached: true`).
- `analysis/recommendation-processing-report.md` was readable, but its ledger ends at `analysis/workflow-optimization-2026-10-08.md`. Reports from 2026-10-15 to 2026-11-24 have no recorded verdicts.
- The success sample rate is 0.07, so most slow review runs are `not_selected` and have no token telemetry.