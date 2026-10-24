## Executive Summary

- **Data window and coverage gap (high confidence):** The window has 1,000 runs from `shubhodeep1/coding-workflows`. The collector's `insufficient_data` flag is `false`. The deep-dive folder (`/home/runner/work/_temp/workflow-log-output`: `errors/`, `slow/`, `recent/`) was not mounted, so no untruncated logs could be read. Every finding below comes from `analysis_context.json` (counters, plus the `log_summary` text on each run) and from reading the repo source. Review and poller log summaries are cut by 0.87M–1.6M characters each.
- **Review/autofix is the main cost and latency driver (high):** It accounts for 100% of model tokens: 342.6M OpenRouter tokens over 152 calls in the 18 runs with telemetry, about 19.0M per run. Its p95 is 3,004s, and the telemetry runs have a wall-clock p50 of 2,160,500 ms. The prompt-cache hit rate is good at 0.868. However, review runs that do two rounds (20–22 model calls) pay about twice the cache-write tokens (1.03–1.12M against roughly 0.52–0.69M for one round). Fixing round-2 prefix reuse could save about 0.5M cache-write tokens per two-round run (medium confidence; this is an inference).
- **The only project stall in the window is a validation-harness break with no structured outcome log (high):** Validate run `40083109127` (`Internal: AI Validate [tracking:7913]`) failed at `Enforce validation outcome` after 1,210s. Unblock-judge run `40113573900` then logged `UNBLOCK_JUDGE item=7913 kind=project stop=harness-broken op=hold hold=item_closed ref=8064 outcome=holding`. The step in `.github/workflows/validate.yml:1728-1746` only echoes `Validation did not pass (harness error).` and prints no status fields. The `VALIDATE_OUTCOME` line proposed in `analysis/workflow-optimization-2026-10-15*.md` was never added. Adding it is a small, safe diagnostic change that makes harness-versus-application failures machine-routable.
- **The CI critical path is one job (medium-high):** CI p50 is 895s and p95 976s over 33 runs. In several runs, `tests-release-and-log-analysis` ran about 11–14 minutes and set the run's duration (`40112590944`, `40111335080`, `40112554345`), with up to about 2.5 minutes of runner queue wait (`40112554345`). Splitting it into a matrix the way `orchestrate-poll` already is (4 shards) could save an estimated 5–8 minutes per CI run.
- **GH API budget telemetry is ambiguous (high):** `GH_PAT_BUDGET used_in_job` is measured as a shared-PAT delta in `scripts/gh_helpers.sh:62-75`. Two review jobs overlapped (`40110205488` 16:11–16:40 and `40111335148` 16:26–16:42), and they report 610 and 338, so each figure includes the other's calls. One job (`40108887056`) reports `used_in_job=unknown` because the rate-limit window reset mid-job. Per-job call counting is needed before any batching can be prioritised.
- **Run-count noise (high, low impact):** 704 of 1,000 runs are `skipped` fan-out runs: clarify 148, implement 145, clarify-respond 146, plan 138, heal-intake 52, triage 44, heal 30, review 1. Each comment event starts 3–4 sibling workflows within the same second (for example `40112746391`, `40112746477`, `40112746455`, `40112746400` at 16:41:46). They cost about 1s each, but they swamp the repo p50 (1.0s), so family-level metrics should be read instead.
- **Fail-open behaviour is healthy apart from one masked case (medium):** All 44 `SEMBLE_FALLBACK` events are contract-test fixtures (`semble_runtime_fallbacks=0`), and 0 of 20 Semble bootstraps failed. The orchestrate poller, though, runs with `SEMBLE_ENABLED: true` but `SEMBLE_AVAILABLE: false` and emits no fallback line (`40111981605`, `40110907043`, `40109299683`). That is a silent disablement.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Split CI `tests-release-and-log-analysis` into shards (critical path).**
   - Evidence: `40112590944` ran this job from about 16:39:48 to 16:54:07 (≈14 min of 985s). `40112554345` waited ≈2.5 min for a runner, then ran ≈11 min. In `40111335080` the post-checkout step began about 11 minutes after the last visible test step (16:26:11 → 16:37:27); that gap is unexplained, see Reliability #6.
   - Root cause: one serial job holds the slowest test groups, while `orchestrate-poll` already uses `strategy.job-index` sharding (`ci.yml:2503`).
   - Change: add a `strategy.matrix.shard: [0,1]` to that job, gate step groups with `if: strategy.job-index == N` (same pattern as `ci.yml:2502-2506`), and add both shards to the `lint` aggregate `needs`.
   - Savings: about 5–8 minutes per CI run × 33 runs in the window. Risk: low, as long as the `lint` gate's job list is updated in the same PR.
2. **Run the cheap "is there a heal issue?" probe before the full-history checkout in `heal-pr-reconcile` (critical path for PR close).**
   - Evidence: in `40112590754`, the checkout fetched every tag (about 570k log characters) and finished about 2m20s after the job started, followed by `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue` in under 1s. The step uses `fetch-depth: 0` (`internal-cancel-on-pr-close.yml:45-50`).
   - Change: add a pre-step that runs the existing no-heal-issue lookup with `gh` (no checkout needed), and gate the checkout and reconcile steps on its output. Keep `fetch-depth: 0` for the non-noop path, which needs history to move commits.
   - Savings: about 2 minutes of runner time per closed PR. Risk: low, provided the pre-step fails open by setting `has_heal=true` on error.
3. **Reuse the Semble index across review rounds in the same run.**
   - Evidence: bootstraps average 40.9s (818,355 ms / 20). The two-round review runs bootstrap twice: `40107619974` 94,770 ms, `40111335148` 94,590 ms, `40108887056` 89,852 ms. The visible part of each bootstrap is `SEMBLE_INSTALL_MS` 7,224–10,346; that the rest is indexing is an inference.
   - Change: cache the built index keyed on head SHA under `RUNNER_TEMP` and skip rebuilding when the key matches.
   - Savings: about 40–47s per two-round run. Risk: low, because the lazy mode already fails open.
4. **Forward-merge checkout (micro-optimization).** `40113558658` took 135s, mostly an all-tags `fetch-depth: 0` checkout (`forward-merge-stable-to-main.yml:48`), while the logic steps took under 1s. Adding `filter: blob:none` keeps the history needed for merge-base. Estimated saving is about 1 minute per run, over 3 runs in the window. Risk: low.
5. **Orchestrate poller (no actionable split yet).** p50 is 708s and p95 1,154s, but every summary says the logs are truncated by about 866K characters, so no step can be identified as dominant (`40111981605`, `40108690301`). See Pipeline item 4 for the diagnostic.

## Cost Optimizations

Ranked by expected token savings. No dollar costs are available: `claude_cost_usd` is not in the context and `codex_tokens_used=0` everywhere.

1. **Round-2 cache reuse in review/autofix.**
   - Evidence: single-round runs write 512,875–693,246 cache tokens (`40104957702`, `40095966946`, `40076730200`). Two-round runs write 1,028,048 (`40107619974`), 1,062,004 (`40110205488`) and 1,123,550 (`40108887056`), and their hit rates are lower: 0.805, 0.833 and 0.817, against 0.86–0.92 for single-round runs.
   - Root cause (inference): the round-2 prompt prefix differs from round 1, for example because a new head SHA or diff sits before the stable instructions, or the round gap exceeds the cache TTL.
   - Change: first, have the collector emit the existing `or_phases` breakdown per round. Then move volatile content (head SHA, diff, prior findings) after the static rubric and system text in the reviewer prompt assembly in `scripts/review_run_reviewers.sh`.
   - Savings: about 0.5M cache-write tokens per two-round run, and the hit rate should rise from about 0.82 to about 0.87. Quality risk: none, since only ordering changes.
2. **Uncached prompt volume per call.**
   - Evidence: 36.68M uncached prompt tokens over 152 calls is about 241k per call. The highest single runs are `40107619974` (6.93M over 20 calls) and `40110205488` (4.97M over 22 calls).
   - Change: emit a `CONTEXT_BUDGET_WARN` per call. The count was 0 in the window, which suggests the threshold (`DEFAULT_CONTEXT_BUDGET_WARN_RATIO = 0.7`) is never reached, or the line is not emitted on this path. Add per-call prompt-section byte accounting so the large section can be found.
   - Savings: unknown until measured. This is a diagnostic first.
3. **Semble value check.** 21 queries returned 266,431 bytes (about 12.7 KB per query, about 8 sources per query). `static_dup_bytes=9,375` (3.5%) is low noise. That is roughly 67k tokens in total, under 0.1% of review input, so Semble is neither bloating prompts nor measurably shrinking them. Its main cost is the bootstrap time (Speed #3). Recommendation: keep it as is, and add a per-run `SEMBLE_QUERY ... used_in_findings=` field if attribution is wanted.
4. **Serena:** `SERENA_ENABLED: false` in every review summary, and there are 0 queries. There are no cost implications.
5. **Avoidable reruns:** none observed (`retries=0` on all runs). The autofix sweep skipped active PRs correctly (`AUTOFIX_SWEEP_SKIP ... reason=active_run`, `40112121908`).

## Reliability Improvements

Ranked by expected reduction in failures or stalls.

1. **Validation harness break stalls project #7913 (application/harness defect).**
   - Evidence: `40083109127` failed at `validate / validate` → `Enforce validation outcome` after 1,210s. Its counters show `serena_fallbacks=2` with no probe lines, and one Semble query of 3,951 bytes. The project is now held under `stop=harness-broken` (`40113573900`), and the fix is in flight in PR #8067, whose body reads `Refs #8064, #7913`.
   - Fix (diagnostic): in `validate.yml:1733-1746`, emit one stable line before exiting. Register the prefix through an `agents.d/` fragment under "Stable log prefixes (contractual)".
     ```bash
     echo "VALIDATE_OUTCOME status=${VALIDATION_STATUS:-unknown} raw_status=${VALIDATION_RAW_STATUS:-unknown} exit=${_rc}"
     ```
   - Impact: the unblock judge and heal can tell a harness failure (exit 2) from an application failure without reading logs. Rollback: the change only adds output, and exit codes stay the same.
2. **CI `orchestrate-poll (3)` → `Validation harness sandbox tests` failure (test regression on PR #8067).**
   - Evidence: `40107619936` failed after 891s. Check-triage `40108741912` correctly skipped it with `CHECK_TRIAGE skip reason=pr_specific_failure base=main base_conclusion=success pr=8067`. Autofix then recovered it: the later CI runs `40110205349` and `40112554345` passed.
   - Fix: no new mechanism is needed. Track it as harness-related, together with #1.
3. **CI `static-checks` → `Drift check (generated docs)` (`40095966868`).**
   - Evidence: `make generate-check` runs two generators (`Makefile:7-9`: `generate_codex_model_reference.py --check` and `update_repo_tree.py --check`), and the failure line does not say which one drifted.
   - Fix: wrap each command so it emits `GENERATE_CHECK_DRIFT generator=<script> rc=<n>`, so triage can route the fix ("run `make generate`") automatically.
   - Impact: cuts time-to-fix for a recurring, deterministic failure class. Fail-open: the exit code is unchanged.
4. **Missing local action during check-triage post-job cleanup (latent).** `40108741912` logged `##[error]Can't find 'action.yml', 'action.yaml' or 'Dockerfile' under '.../.codex-workflow-src/.github/actions/install-claude'`, but the run ended `success`. Root cause (inference): `.codex-workflow-src` is removed or replaced before the action's post step runs. Fix: do not delete the support checkout before job end, or move cleanup into an `if: always()` final step. Impact: removes a masked error that would hide real post-step failures.
5. **Orchestrate-poll cancellations.** 9 of 57 runs (15.8%) were cancelled, and these are the repo's only cancellations. The cause is unknown; supersession under the concurrency group is an inference. Diagnostic: on cancel-in-progress supersession, log `ORCH_POLL_SUPERSEDED prev_run=<id> elapsed_s=<n>`, or have the collector record each cancelled run's concurrency group.
6. **Unexplained 11-minute idle gap.** In `40111335080`, `tests-release-and-log-analysis` shows no output between 16:26:11 and 16:37:27. Diagnostic: the collector should pull per-step `started_at`/`completed_at` from the jobs API data it already fetches, instead of relying on truncated step logs.
7. **MCP fallbacks.**
   - Semble: all 44 fallbacks are `semble_contract_test_fallbacks` in CI (4 or 8 per run). These are healthy fixtures, not a broken rollout, with 0 runtime fallbacks and 0 failed bootstraps.
   - Masked case: the orchestrate poller has `SEMBLE_AVAILABLE: false` with `SEMBLE_ENABLED: true` and emits no `SEMBLE_FALLBACK target=... reason=unavailable` line. Emit one per run so the disablement shows up in metrics.
   - Serena: the 2 fallbacks in `40083109127` came with no `SERENA_PROBE` lines, so availability failure cannot be told apart from runtime fallback. Emit `SERENA_PROBE result=skipped reason=disabled` when the server is off.
8. **BREAK_GLASS / CONTEXT_BUDGET_WARN:** both 0 across all 115 runs with telemetry (`REVIEW_BREAK_GLASS_ENABLED: false` in `40108887056`). This shows no rubric or prompt-size pressure, but see Cost #2 on whether the warning path is wired.

## AI Memory Health

- Only one `AI_MEMORY_TELEMETRY` line is visible in the window: `{"op":"finalize-task","ok":true,"enabled":true,"fail_open":true,"reason":"no_linked_issues","source":"issue_pr_status.yml"}` in `40112590749`.
- There are no visible `retrieve`, `record-candidate`, `promote`, `compact` or `processed-command-*` operations. Retrieve hit rate, `estimated_tokens` against budget, and the `keyword_method` mix therefore cannot be computed. The review and implement logs, where retrieves would appear, are truncated by 1.16–1.6M characters.
- The single line has `fail_open: true`, but it is a benign no-op (no linked issues).
- Recommendation: add additive counters to `scripts/cost_audit.py`'s `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS`: `ai_memory_retrieve_calls`, `ai_memory_retrieve_hits` (`records_selected>0`), `ai_memory_fail_open_count`, `ai_memory_disabled_count`. These would be parsed from the full logs the same way the `or_*` counters already survive truncation. Impact: memory health becomes measurable every window without depending on deep-dive selection.

## GH API Call Audit

| Workflow/job | Run | `used_in_job` | `remaining` at end | Notes |
|---|---|---|---|---|
| review_autofix / codex-agent | 40110205488 | 610 | 4360 | overlaps 40111335148 |
| review_autofix / codex-agent | 40112554503 | 404 | 3934 | lowest remaining seen |
| review_autofix / codex-agent | 40111335148 | 338 | 4331 | overlaps 40110205488 |
| review_autofix / codex-agent | 40108887056 | unknown | 4684 | window reset mid-job |
| review_autofix_sweep / sweep | 40112121908 | 4 | 4547 | efficient |
| review_autofix / post-merge-validate-dispatch | 40112590852 | 0 | 4325 | — |

All rows share `reset=1792861743`.

- **Hotspot:** the review `codex-agent` jobs, at roughly 300–600 calls each. Because the counter is a delta on the shared PAT (`gh_helpers.sh:66-72`), overlapping jobs count each other's calls, so the true per-job load is lower but unknown. The `unknown` value comes from the `reset == previous_reset` guard (`gh_helpers.sh:69`).
- **Diagnostic addition (first step, required before any batching work):** add a process-local counter to the existing `gh_retry` wrapper and print it next to the current line, for example `GH_API_CALLS workflow=review_autofix job=codex-agent count=<n> top_endpoints=<path:n,...>`. Also replace `used_in_job=unknown` with `used_in_job=unknown reason=window_rolled|no_start_snapshot`. This only adds output and needs no extra API calls.
- **Low-volume paths are fine:** lint-pr-body makes one paginated `pulls/<n>/commits` call (`40113604973`), and integration readiness makes about one (`40113604949`).
- **Expected 404:** `promote_candidate_follow` hits a permanent 404 on `git/ref/tags/promote-cycle-frozen` each run (`40112590969`, `40111513476`, logged as `GH_API_RETRY outcome=permanent ... attempt=1/5 wait_secs=0`). This is the expected not-frozen path with no retry. Optionally tag it `expected_absent=true` so it does not count as an error.
- No HTTP 429 or secondary-rate-limit events in any summary. Batching against the §14 hygiene rules cannot be judged without per-endpoint counts.

## Prompt Cache & Memory System

- `cache_hit_rate` is 0.868 overall: 295.75M cache-read, 8.31M cache-write and 36.68M uncached prompt tokens. All 152 calls reported usage (`or_usage_unavailable_calls=0`).
- Per-run range: 0.758 (`40100218024`) to 0.924 (`40096005000`).
- Fragmentation signal: two-round runs double their cache writes and lose about 5 points of hit rate (Cost #1). The most likely causes (inference) are a volatile head SHA or diff ahead of the stable prefix, or TTL expiry between rounds 15+ minutes apart.
- Fail-open: `OPENROUTER_PROMPT_CACHE_DISABLED: false` (`40109299683`). There are no cache-disable events.
- npm/codex action caches hit on their primary keys (`claude-cli-npm-Linux-...` and `codex-v0.114.0-v2` in `40110197729`).
- There is no `CONTEXT_BUDGET_WARN` pressure (0).
- Recommendations:
  - Order the reviewer prompt as stable rubric, then repo context, then diff and head SHA.
  - Emit per-round `or_phases` with `round=1|2`, so a measured hit rate per round can confirm the cause.
  - Expected impact: hit rate up about 3–5 points on multi-round runs, and about 0.5M fewer cache-write tokens per such run.

## Orchestrator Health

- **Poller:** 57 runs, 48 success, 9 cancelled, 0 failures; p50 708s, p95 1,154s, max 1,277s (`40088073659`). Queue wait reached about 3.5 minutes (`40109299683`, created 15:57:10, started 16:00:39). Semble is enabled but unavailable on every poller run sampled.
- **Project #7913:** held in a terminal-ish hold, `stop=harness-broken op=hold hold=item_closed ref=8064` (`40113573900`). The wait settings are `UNBLOCK_JUDGE_FIXUP_WAIT_HOURS: 72` and `UNBLOCK_JUDGE_WAIT_MAX_DAYS: 14`. This is the only stall indicator in the window.
- **Plan stall recovery:** it works. `40110197729` re-triggered planning ("Standalone stall recovery: planning stalled") and succeeded in 33s.
- **Stale queued runs:** the scheduled cancel-on-close job keeps preserving the same 3 unlinked queued runs: `34748435167` and `34748435180` (about 41 days old) and `32215723558` (about 66 days old). Each scheduled run repeats the warning (`40113653539`, `40112991279`, `40112105210`, `40111079837`).
  - Smallest safe mitigation: an age gate that cancels unlinked queued runs older than `CANCEL_UNLINKED_QUEUED_MAX_AGE_DAYS`. The variable needs a default per §8; 30 is suggested, with 0 meaning disabled. Emit `CANCEL_CLEANUP_STALE_UNLINKED run=<id> age_days=<n> action=cancel|preserve`.
  - That these runs pin a concurrency group is an inference.
- **Merge train:** `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` on every sampled run, so it was idle.
- **Indicators to track:** count of `UNBLOCK_JUDGE ... outcome=holding` lines per item, poller cancelled/total ratio, stale queued-run age, and `VALIDATE_OUTCOME raw_status=harness_error` count once that line is added.

## Pipeline Flow Bottlenecks

1. **Review → autofix → re-review loop (compute plus retry):** each autofix push starts a new review.
   - PR #8066 had reviews `40106477246` (2,021s), `40108887056` (2,055s) and `40111335148` (1,129s).
   - PR #8067 had `40107619974` (2,193s), `40110205488` (1,920s) and `40112554503` (952s).
   - That is about 3 iterations per PR (sampled; medium confidence), plus about 15 minutes of CI per push.
   - Two of these runs pushed edits yet resolved no threads (`AUTOFIX_EDITS_PUSHED: true`, `REVIEW_THREADS_RESOLVED_COUNT: 0` in `40110205488` and `40108887056`). Diagnostic: log `AUTOFIX_THREADS findings_applied=<n> threads_open=<n> threads_resolved=<n> reason=<...>` so the loop's convergence can be measured.
2. **CI (compute plus queue):** about 15 minutes, gated by `tests-release-and-log-analysis`, with 2.5–3.5 minute runner waits (Speed #1).
3. **Validate (failure-driven stall):** a single 1,210s failure parked an entire project (Reliability #1).
4. **Poller (compute, opaque):** about 12-minute cycles with no step visibility. Add a one-line end-of-cycle summary; no existing `ORCHESTRATE_POLL_*` summary echo was found in `scripts/`. Example: `ORCH_POLL_CYCLE items=<n> dispatched=<n> held=<n> elapsed_ms=<n> gh_calls=<n>`.
5. **Clarify, plan and implement:** essentially idle in this window. Successes: 2, 10 and 2, against 148, 138 and 145 skipped. The implement slow outlier was `40096713266` at 1,727s. Merge/conflict overhead: no conflict-heal or merge-train activity was observed.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix (p95 3,004s, about 3 iterations per PR), CI `tests-release-and-log-analysis` (11–14 min), poller cycles (p50 708s), Semble bootstrap (40.9s average).
- **Top failure modes:** the validation harness break (`40083109127`, and the sandbox test in `40107619936`), generated-docs drift (`40095966868`), and the latent missing post-step action in triage (`40108741912`).
- **Highest-cost driver:** review_autofix, at 342.6M tokens over 152 calls, with two-round runs paying double cache writes.
- **Top 3 actions:**
  1. Add the `VALIDATE_OUTCOME` line in `validate.yml`, and `GENERATE_CHECK_DRIFT generator=` around `make generate-check`.
  2. Add per-job `GH_API_CALLS` counting in the `gh_retry` wrapper, and fix the `used_in_job=unknown` reason.
  3. Shard `tests-release-and-log-analysis`, and move volatile content after the stable prefix in the reviewer prompt.

## Metrics Appendix

**Runs and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Fail rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Repo total | 1000 | 284 | 3 | 9 | 704 | 0.3% | 1.0 | 897.0 |
| ci | 33 | 31 | 2 | 0 | 0 | 6.1% | 895 | 976 |
| review_autofix | 53 | 52 | 0 | 0 | 1 | 0% | 170 | 3004.2 |
| orchestrate_poll | 57 | 48 | 0 | 9 | 0 | 0% | 708 | 1154.4 |
| validate | 1 | 0 | 1 | 0 | 0 | 100% | 1210 | 1210 |
| plan | 148 | 10 | 0 | 0 | 138 | 0% | 1 | 24.3 |
| clarify | 150 | 2 | 0 | 0 | 148 | 0% | 1 | 10 |
| implement | 147 | 2 | 0 | 0 | 145 | 0% | 1 | 10.7 |

**Tokens and cache (OpenRouter; review_autofix only)**

| Metric | Value |
|---|---|
| or_calls (usage available / unavailable) | 152 (152 / 0) |
| prompt / completion / total | 36,684,093 / 1,849,408 / 342,589,560 |
| cache write / cache read | 8,309,289 / 295,753,717 |
| cache_hit_rate | 0.867957 (per-run range 0.758–0.924) |
| codex_tokens_used / claude_cost_usd | 0 / not provided |

**Review telemetry**

| Metric | Value |
|---|---|
| wall_clock_p50_ms (repo / review_autofix) | 10,000 / 2,160,500 |
| wall_clock_p99_ms (repo / review_autofix) | 3,285,690 / 3,510,820 |
| wall_clock_sample_count (repo) | 110 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |

**Semble**

| Metric | Value |
|---|---|
| query calls / bytes / bytes per query | 21 / 266,431 / ≈12,687 |
| sources / static_dup_bytes | 167 / 9,375 (3.5%) |
| bootstraps / failed / unused / total ms | 20 / 0 / 0 / 818,355 (avg 40,918) |
| fallbacks (contract-test / runtime) | 44 (44 / 0); all in `ci` |

**Serena and other MCP servers**

| Metric | Value |
|---|---|
| queries / response bytes / tool calls | 0 / 0 / 0 |
| fallbacks | 2 (validate `40083109127`) |
| probe ok / failed / skipped | 0 / 0 / 0 |
| Other MCP servers observed | none |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (validate) | 0 | 0 | 0 |
| serena (review_autofix) | 0 | 0 | 0 (`SERENA_ENABLED: false`) |

**GH API:** see the table in the GH API Call Audit. The visible range is 0–610 calls per job, with no 429s.

**Data gaps:**
- The deep-dive folder was not mounted.
- Review and poller logs are truncated (0.87M–1.6M characters).
- There is no Claude cost or model breakdown.
- There are no AI-memory retrieve events.
- `used_in_job` is shared-PAT contaminated.
- The success-sample rate is 0.07, so the 3-iterations-per-PR figure is a sampled estimate.

---

Nothing in the repo was edited; this report is the deliverable.

**Assumptions:**
- The 2× cache-write pattern on two-round runs comes from round-2 prefix variance or TTL expiry. This is an inference.
- The 9 poller cancellations come from concurrency supersession. This is an inference.

**Missing context:**
- The `workflow-log-output` directory with untruncated logs was absent, so no quoted log line could be checked against full logs. Every quote above comes from the `log_summary` fields or from repo source.