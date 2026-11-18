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
## Deep Audit — Workflows & Scripts (2026-10-24)

Scope note: this pass covers fewer files in more depth. It read in full or in large part `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `forward-merge-stable-to-main.yml`, `review_autofix_sweep.yml`, `internal-review.yml`, `review_rb_judge_dispatch.yml`, `unblock_judge_dispatch.yml` and `pr6576-shard-followup.yml`. It also read selected steps of `review_autofix.yml`, `validate.yml`, `check_failure_triage.yml`, `clarify.yml`, `orchestrate_poll.yml` and `test-and-mark-stable.yml`, plus `scripts/gh_helpers.sh` (lines 1-1320), `scripts/label_helpers.sh`, `scripts/tg_helpers.sh` (lines 180-513) and parts of `scripts/orchestrate_poll_process.sh`. Byte sizes and exact expression lengths could not be measured because no shell was available in this run; estimates are labelled as such. Where a finding extends a finding in the existing report sections above, it cross-references that section.

### Section 1: Bug & Correctness Sweep

**BUG-001** · `.github/workflows/check_failure_triage.yml:414-419, 553-563` · Medium · `bug`
- **Description:** This is the root cause of report Reliability #4 (`Can't find 'action.yml' ... under '.../.codex-workflow-src/.github/actions/install-claude'` in run `40108741912`). The job runs the local composite action `uses: ./.codex-workflow-src/.github/actions/install-claude` at `:417`. On the Claude path, the staging step then deletes every action directory except `claude-pool-token` at `:560` (`find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token -exec rm -rf {} +`). The deleted `install-claude` action contains `actions/cache@v5` (`.github/actions/install-claude/action.yml:31-35`), which has a post step. At job end the runner cannot load the composite's `action.yml` for that post step. The result is a masked `##[error]` on a successful run, and the npm cache for `claude-cli-npm-*` is never saved on triage runs. The comment at `:553-555` only accounts for the `claude-pool-token` post action. Of the 11 workflows that use `install-claude`, only this one prunes the directory.
- **Recommended fix:** Keep both action directories. At `:560`, use `find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +`, and update the comment at `:553-555` to name both post actions. This removes no other cleanup and makes no API calls.

**BUG-002** · `scripts/tg_helpers.sh:396-440, 465-511` · Medium · `bug`
- **Description:** `tg_cleanup_phase_msgs` and `tg_cleanup_msgs` page forward through issue comments (`?per_page=100&page=${page}`, `page=$((page + 1))` at `:439` and `:510`). They also `DELETE` matching tracking comments on the current page (`:430-434`, `:501-505`) before fetching the next page. Each delete shifts the remaining comments one position earlier, so a tracking comment that was on page N+1 can move into page N and never be read. Its Telegram message IDs are never deleted, and the comment stays behind. This affects busy issues, which the comment at `:195-197` notes can hold hundreds of `tg_cleanup` comments.
- **Recommended fix:** Make two passes. First, read all pages into one list (for example `curl_gh_api` per page, appending `id\tbody` lines to a temp file). Then delete Telegram messages and comments from that list. Alternatively, walk from the last page back to page 1 (the last page number comes from `.comments` as in `_tg_newest_comments_json`, `:204-218`).

**BUG-003** · `scripts/tg_helpers.sh:252-275, 320-343` · Low · `bug`
- **Description:** `tg_store_msg_id` and `tg_store_phase_msg_id` read the newest tracking comment, edit the body with `sed`, and `PATCH` the whole body back. Two jobs that append to the same issue's tracking comment at the same time (for example a clarify run and a plan run) overwrite each other, so one message ID is lost and that Telegram message is never cleaned up. The write uses raw `curl -s` without `-f`, and the response goes to `/dev/null 2>&1 || true`, so HTTP failures cannot be seen. That the two jobs actually overlap this way is an inference [NEEDS VERIFICATION].
- **Recommended fix:** Switch to append-only storage: post one small comment per message ID (`<!-- tg_cleanup:<id> -->`), which the cleanup already matches. If the PATCH is kept, re-read the comment and verify the ID is present after the write. Add `-f` and log a `::warning::` when the write fails.

**BUG-004** · `.github/workflows/validate.yml:1401-1453, 1728-1746` · Medium · `bug`
- **Description:** This extends the Executive Summary item on the missing `VALIDATE_OUTCOME` line. `Collect validation status` runs under `set -euo pipefail` with `RUNTIME_DIR: ${{ steps.runtime.outputs.runtime_dir }}`. If that output is empty (the runtime step failed), `STATUS_FILE_PATH` becomes `/validation_status.json`, the `jq -n ... >` write at `:1413-1423` fails, and the step exits before writing `status` and `raw_status` to `$GITHUB_OUTPUT` (`:1446-1453`). A malformed status file has the same effect at `:1426`. `Enforce validation outcome` then gets empty `VALIDATION_STATUS` and `VALIDATION_RAW_STATUS`, takes the `exit 1` branch (`:1745-1746`), and prints `Validation did not pass.`. A harness or infrastructure failure is therefore reported as an application failure, which defeats the exit-2 split described at `:1738-1740`.
- **Recommended fix:** In `collect_status`, guard `[ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]` and fall back to `status=error raw_status=harness_error` when the directory is missing or `jq` fails (`status="$(jq -r ... 2>/dev/null)" || status=error`). In `Enforce validation outcome`, treat an empty `VALIDATION_RAW_STATUS` as a harness error (exit 2), and emit the report's proposed `VALIDATE_OUTCOME status=... raw_status=... exit=...` line before exiting. Consumers of the exit code should be checked before this change [NEEDS VERIFICATION].

**BUG-005** · `.github/workflows/review_autofix_sweep.yml:577-609` · Low · `bug`
- **Description:** `Record GH_PAT budget at sweep end` (`:577-588`) runs before `Re-run cancelled CI failed jobs once` (`:590-598`) and `Re-dispatch stale security holds` (`:600-609`). The security-hold step uses `GH_TOKEN: ${{ secrets.GH_PAT || github.token }}`, so its GH_PAT calls are never in `used_in_job`. This adds a systematic undercount to the attribution problems already described in the report's GH API Call Audit (shared-PAT contamination).
- **Recommended fix:** Move the budget-end step to the end of the job and keep `if: always()`. This is a step reorder only, with no new API calls.

**BUG-006** · `.github/workflows/review_autofix_sweep.yml:201-252, 590-598` · Low · `bug`
- **Description:** `Re-run cancelled CI failed jobs once` has no `if:`, so it gets the implicit `success()`. The enumerate step (`:201-552`, `set -euo pipefail`) writes the CI snapshot at `:239` and can then fail later, for example in `jq` or a raw `gh api` call (see API-006). When it does, the CI rerun step is skipped even though its input exists. The backfill step at `:564` uses `!cancelled()` explicitly so that it does not depend on the enumerate step, and the CI step was not given the same treatment.
- **Recommended fix:** Add `if: ${{ !cancelled() }}` to `:590`. `scripts/ci_cancelled_rerun.py` reads `CI_CANCELLED_PR_SNAPSHOT`; confirm that it fails open when the file is missing [NEEDS VERIFICATION].

**BUG-007** · `.github/workflows/cancel_on_pr_close.yml:14-16` (caller: `internal-cancel-on-pr-close.yml:3-7`) · Low · `bug`
- **Description:** Close-event runs and the 5-minute scheduled runs share one concurrency group, `cancel-on-pr-close-${{ github.repository }}`, with `cancel-in-progress: false`. GitHub keeps at most one pending run per group, and a newer pending run replaces the older one. During a burst of PR closes or merges, a pending close run can be dropped, and the merge-train release for that specific close (`:391-427`) goes with it. The next scheduled run is a backstop: it cancels closed-PR runs (`:258-316`) and runs the global merge-train scan (`:409-410`). The impact is therefore up to about 5 minutes of delay, not loss [NEEDS VERIFICATION that drops occur in practice].
- **Recommended fix:** Give scheduled runs and close runs separate keys, for example `group: cancel-on-pr-close-${{ github.repository }}-${{ github.event_name == 'schedule' && 'schedule' || github.event.pull_request.number }}`. Do this only after confirming that per-PR runs do not need to be serialised against the scheduled sweep.

**BUG-008** · `.github/workflows/forward-merge-stable-to-main.yml:238, 320-342, 401-439` · Low · `bug`
- **Description:** The fallback PR is created with `GH_TOKEN: ${{ secrets.GH_PAT || github.token }}` (`:238`). The comments at `:336-340` and `:402-407` assume a `GITHUB_TOKEN`-created PR, which emits no `pull_request` event. When `GH_PAT` is set, PR creation does emit `opened`, so `internal-review.yml` starts under the group `internal-review-pull_request-<head_ref>` (`internal-review.yml:50`). The explicit dispatch at `:435` then starts a second run under `internal-review-dispatch-pr-<N>`. The codex-agent job's `pr-autofix-<N>` group (`review_autofix.yml:1853-1854`) serialises the two runs, so this is duplicate compute and gate API calls rather than a write race [NEEDS VERIFICATION].
- **Recommended fix:** Dispatch only when the PR was created with `github.token` (export `pr_created_with_pat=true|false` from `:320`). Otherwise, check for an existing `pull_request` run on `${BRANCH}` before dispatching. Update the stale comments.

**BUG-009** · `.github/workflows/forward-merge-stable-to-main.yml:42-50, 66-81, 115-130, 160-188, 203-218` · Low · `bug`
- **Description:** The job uses `timeout-minutes: 5`. Report Speed #4 measured a 135s run, mostly the `fetch-depth: 0` all-tags checkout. The job then has up to three bounded `git fetch` retry loops (backoff 2s and 4s each) and a 3-attempt push loop. A slow checkout plus one retried fetch can reach the 5-minute timeout. The result is a hard failure, and `stable` fixes stop propagating until a re-run.
- **Recommended fix:** Apply the Speed #4 checkout change (`filter: blob:none`) and raise `timeout-minutes` to 10. Both changes are local and reversible.

**SEC-001** · `.github/workflows/clarify.yml:71-78` · Low · `security`
- **Description:** `Log trigger context` interpolates `${{ github.event.issue.number }}`, `${{ github.event_name }}`, `${{ github.event.action }}` and `${{ github.event.comment.user.login }}` directly into the shell body. GitHub logins are limited to a safe character set (bot logins add `[bot]`), so this is not exploitable today. It does break the env-indirection convention the same job uses for the same value at `:101` (`HOLD_COMMENT_LOGIN: ${{ github.event.comment.user.login }}`), and it counts the body toward the expression limit.
- **Recommended fix:** Move the values to `env:` (`TRIGGER_EVENT_NAME`, `TRIGGER_ACTION`, `TRIGGER_COMMENT_LOGIN`) and reference them as `"${TRIGGER_COMMENT_LOGIN}"`.

### Section 2: GitHub API Call Redundancy Audit

**API-001** · `scripts/label_helpers.sh:163-197`; `scripts/gh_helpers.sh:192-195, 804-816, 1060-1138` · High · `api-redundancy`
- **Description:** `ensure_label_exists` wraps `gh label create` in `gh_retry`. `gh label create` is not in `_gh_cmd_is_unsafe_post` (`gh_helpers.sh:811-814`), so it is retryable. `_is_gh_permanent_failure` (`gh_helpers.sh:194`) has no "already exists" pattern. For a label that already exists, which is the normal steady state, `gh_retry` treats the failure as transient. It makes up to `GH_RETRY_MAX_ATTEMPTS=5` create calls with 1+2+4+8 = 15s of sleep, prints `::error::gh command failed after 5 attempts`, and only then reaches the helper's own `already exists` grep (`label_helpers.sh:190`), which returns 0. The claim that gh's stderr is `label with name "<n>" already exists; use '--force' ...` with no `HTTP 422` text is an inference from the gh CLI. The only repo test (`tests/test_validate_process_fixup_labels.py:277-285`) uses synthetic `HTTP 422 ... already_exists` and `already_exists` strings and does not exercise the real text [NEEDS VERIFICATION]. There are 189 `ensure_label_exists` call sites across 27 files, 66 of them in `scripts/orchestrate_poll_process.sh`, and `set_issue_phase_label_resilient` calls it on every phase change (`:223`). This could account for part of the 300-600 `used_in_job` seen in review jobs (report GH API Call Audit).
- **Current vs. proposed calls:** up to 5 calls and 15s per call per already-existing label, against 1 call (or 0 with a per-job cache).
- **Recommended fix:** (1) Add `already[ _-]?exists` to the `_is_gh_permanent_failure` regex, so every `gh_retry` caller stops retrying idempotent "already exists" errors. (2) In `ensure_label_exists`, cache the labels already ensured per job (`${RUNNER_TEMP}/ensured_labels_${GITHUB_RUN_ID}`, checked with `grep -Fxq` before the create), using the same RUNNER_TEMP cache pattern as `gh_review_pr_state` (`gh_helpers.sh:83-118`). Fail open: a cache read error falls through to the create.

**API-002** · `.github/workflows/review_autofix.yml:1165-1192, 1408, 1445` · Medium · `api-redundancy`
- **Description:** The `deterministic-skip-merge` job defines an inline `gh_retry` (4 attempts, 2/4/8s backoff, no permanent-failure or rate-limit handling) and an inline `ensure_label_exists` that calls `gh_retry gh label create ... || true`. This is confirmed without needing CLI text: every call for an existing label makes 4 create attempts and sleeps 14s. The job calls it twice (`ai:review-skipped` at `:1408`, `ai:ready-to-merge` at `:1445`), so a deterministic-skip run can spend up to 8 wasted calls and 28s. The same wrapper also retries `gh pr merge ... --auto --match-head-commit` (`:1338`, `:1375`) on deterministic failures such as "auto-merge not allowed".
- **Current vs. proposed calls:** 8 calls and 28s, against 2 calls (or 0 with a label-existence check).
- **Recommended fix:** The job already checks out and verifies the support commit (`.codex-freshness-src`, `:1214-1221`). Source `scripts/gh_helpers.sh` and `scripts/label_helpers.sh` from that verified checkout, and keep the inline definitions only as the fallback when verification fails. With API-001 applied, existing labels then cost one call.

**API-003** · `scripts/tg_helpers.sh:396-440, 465-511` · Low · `api-batching`
- **Description:** Cleanup reads every page of issue comments (ceil(C/100) GETs) and makes one `DELETE` per tracking comment inside the loop. GitHub has no batch delete, so the DELETE count cannot drop. The read side, however, repeats work across the two functions when both run for one issue (`implement.yml:2547` and `plan.yml:1258` run phase cleanups; a full cleanup elsewhere re-reads the same pages).
- **Current vs. proposed calls:** ceil(C/100)×k GETs plus T DELETEs (k = cleanup calls per job), against ceil(C/100) GETs once per job plus T DELETEs.
- **Recommended fix:** Combine with the BUG-002 two-pass fix. Write the collected `id\tbody` list to `${RUNNER_TEMP}/tg_tracking_<issue>.tsv` once per job, and have both cleanup functions filter it by marker. Remove a row after a successful DELETE. A cache miss re-reads.

**API-004** · `.github/workflows/cancel_on_pr_close.yml:364-380`; `scripts/orchestrate_poll_process.sh:16374-16378` · Low · `api-redundancy`
- **Description:** Run cancellation is `gh_retry gh api -X POST ".../actions/runs/${run_id}/cancel"`. `_gh_api_args_unsafe_post` (`gh_helpers.sh:678-742`) classifies an explicit `-X POST` as a non-idempotent create, so the call gets one attempt (`gh_helpers.sh:1067-1069`). Cancel is idempotent: repeating it on a finished run returns 409. A transient 5xx therefore leaves a closed PR's run, or a zombie run, running until the next tick. The per-item loop is unavoidable because there is no batch cancel endpoint.
- **Current vs. proposed calls:** 1 attempt per run, against up to 5 attempts on transient failures only (steady state unchanged).
- **Recommended fix:** Use `gh_retry --idempotent gh api -X POST ...` at both sites (supported at `gh_helpers.sh:1065`).

**API-005** · `.github/workflows/review_autofix.yml:910-925` · Low · `api-batching`
- **Description:** In `post-merge-validate-dispatch`, when linked issues come from the PR body/title fallback (`labels: null`, `:903`), the loop calls `gh issue view "${issue_number}" --json labels` once per issue.
- **Current vs. proposed calls:** N calls, against 1 aliased GraphQL call.
- **Recommended fix:** Prefetch labels for all fallback issue numbers in one aliased query (`issue_<n>: issue(number:<n>){labels(first:100){nodes{name}}}`), following `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:191-256`, ≤50 aliases per call). On failure, fall back to the current per-issue call.

**API-006** · `.github/workflows/review_autofix_sweep.yml:228-237, 285, 337-348, 444-550` · Low · `api-redundancy`
- **Description:** The open-PR snapshot (`:228`) uses raw `gh api --paginate` under `set -euo pipefail`, so a transient 5xx ends the whole tick. The default-branch read (`:285`) is also raw. The same step uses the classified `gh_api_retry` for the run snapshot (`:340`). The sweep also has no cap on dispatches per tick (`:543`); the header comment at `:71-73` records 31 dispatches in one tick.
- **Current vs. proposed calls:** steady state unchanged; transient-failure ticks keep running instead of aborting; dispatches capped per tick.
- **Recommended fix:** Use `gh_api_retry --paginate` at `:228` and `gh_api_retry` at `:285`. Add a per-tick dispatch cap variable with a default (per §8), and log `AUTOFIX_SWEEP_SKIP reason=dispatch_cap` beyond it. The cap value needs a decision [NEEDS VERIFICATION].

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** · `.github/workflows/review_autofix.yml:816-838, 1165-1187` vs `scripts/gh_helpers.sh:1060-1138` · Medium · `duplication`
- **Description:** Two identical inline `gh_retry` bodies (4 attempts, buffered stdout) have drifted from the shared helper. They lack the permanent-failure short-circuit, the rate-limit reset wait and breaker, the single attempt for non-idempotent POSTs (#6634), and `--idempotent`. API-002 is one consequence.
- **Recommended fix:** The shared module is `scripts/gh_helpers.sh` and the signature stays `gh_retry [--idempotent] <cmd…>`. Callers: `review_autofix.yml` `post-merge-validate-dispatch`, which already tries `.codex-workflow-src/scripts/gh_helpers.sh` at `:808`, and `deterministic-skip-merge`, which should source it from the verified `.codex-freshness-src`. Keep one minimal fallback, `gh_retry() { "$@"; }`, as `cancel_on_pr_close.yml:148` does.

**DUP-002** · 26 copies across 13 workflows (for example `cancel_on_pr_close.yml:44`, `review_autofix_sweep.yml:220`; counts: implement 6, validate 3, orchestrate 2, orchestrate_poll 2, orchestrate_clarify_respond 2, clarify 2, plan 2, security-audit 2, and 1 each in cancel_on_pr_close, workflow_failure_heal, check_failure_triage, review_autofix, review_autofix_sweep) · Low · `duplication`
- **Description:** The one-line fallback `type gh_api_retry >/dev/null 2>&1 || gh_api_retry() { while [ "${1:-}" = --idempotent ] || [ "${1:-}" = --optional ]; do shift; done; gh api "$@"; }` is pasted in each file. Any change to `gh_api_retry` flags needs 26 edits.
- **Recommended fix:** Put a `gh_api_retry_fallback_define` function in `scripts/gh_helpers.sh` for callers that load it. For pre-checkout steps, extend the `Bootstrap GitHub API retry helper` step (`review_autofix.yml:331-379`, exports `GH_API_RETRY_HELPERS`) to other jobs, and add a test that pins the shim text so the copies cannot drift.

**DUP-003** · inline GH_PAT budget-start blocks: `review_autofix.yml:381-393, 777, 1000, 1138, 1541, 1977`; `clarify.yml:84-92, 2345`; `orchestrate_poll.yml:116`; `workflow-failure-heal-intake.yml:161, 426`; `validation-improvements-intake.yml:54` · Low · `duplication`
- **Description:** There are 12 copies of the same `gh api rate_limit --jq '[.resources.core.remaining, .resources.core.reset] | @tsv'` block. They duplicate `gh_pat_budget start` (`gh_helpers.sh:31-77`), but read only the JSON body, while `gh_pat_budget` prefers the `x-ratelimit-*` core headers (`:52-60`). Start and end readings can therefore come from different sources. The gate job's own `Bootstrap GitHub API retry helper` step (`review_autofix.yml:331`) already runs before its inline budget start at `:381`.
- **Recommended fix:** Where a helper is already available (the gate job through `GH_API_RETRY_HELPERS`), call `source "${GH_API_RETRY_HELPERS}" && gh_pat_budget start review_autofix gate "${GH_PAT_BUDGET_FILE}"` and keep the inline block as the else branch. Callers to update: the 12 sites listed. This complements the report's per-job `GH_API_CALLS` proposal.

**DUP-004** · `.github/workflows/orchestrate_poll.yml:2026-2059` vs `.github/workflows/forward-merge-stable-to-main.yml:478-515` · Low · `duplication`
- **Description:** Both workflows carry the same failing-step lookup: raw `curl -fsSL -H "Authorization: token ${GH_TOKEN}" .../actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT}/jobs`, then `jq ... select(.conclusion == "failure")`, then a Telegram alert. Neither copy retries.
- **Recommended fix:** Add `tg_send_job_failure_alert <workflow_name> <level>` to `scripts/tg_helpers.sh`, using `curl_gh_api` for the jobs read (fail open), and update both callers.

**DUP-005** · `.github/workflows/review_autofix.yml:4226, 4256` · Low · `duplication`
- **Description:** The `if:` conditions of `Enable auto-merge on PR` (`:4226`) and `Mark linked issues ready to merge` (`:4256`) share the same 12 clauses (`max_iterations_reached`/`skip_judge`, `LEDGER_ONLY_COMMIT_STRICT`, `MERGE_CONFLICT`, `PR_CLOSED`, `EDITOR_CHANGES_LOST`, `EDITOR_NOOP_SUSPICIOUS`, `AUTOFIX_STALE_BASE_SKIP`, `CLAUDE_BRANCH_REVIEW_MODE`, `AUTOFIX_PARTIAL_FINALIZE_REQUESTED`, `AUTOFIX_RESUME_TERMINAL`, `single_issue_security_pass.outputs.hold`). They differ only in `env.AUTO_MERGE_READY_LABELS_ALLOWED`. The PR #2542 incident described in the comments came from exactly this kind of gate drift.
- **Recommended fix:** Compute the shared predicate once in a small step (`id: clean_tail`, output `eligible=true|false`) and use `if: success() && steps.clean_tail.outputs.eligible == 'true'` (plus the labels-allowed clause on `:4256`).

**DUP-006** · `scripts/tg_helpers.sh:227-276` vs `296-344`; `380-441` vs `449-512` · Low · `duplication`
- **Description:** `tg_store_msg_id` and `tg_store_phase_msg_id` are near-identical apart from the marker. `tg_cleanup_phase_msgs` and `tg_cleanup_msgs` are near-identical apart from the filter regex. BUG-002 and BUG-003 would have to be fixed in two places each.
- **Recommended fix:** Introduce `_tg_store_marker_id <issue> <marker_prefix> <msg_id>` and `_tg_cleanup_by_marker <issue> <jq_filter> <sed_extract>` in `scripts/tg_helpers.sh`. Keep the four public functions as thin wrappers so their names do not change (§10).

### Section 4: Expression Size Limit Risk Assessment

Baseline: `tests/test_workflow_file_size_limit.py:66-98` already fails CI for any `steps[].run` body that contains `${{` and is 18,000 characters or longer. Every flagged body below is therefore under 18,000 if CI is green. The findings target the 15,000-18,000 band and gaps in the guard.

**EXPR-001** · `.github/workflows/test-and-mark-stable.yml:1342-1695` (step `Phase 3b: Wait for PR creation (implement phase)`) · Medium · `expression-limit`
- **Description:** The run body (`:1350` to about `:1695`, roughly 345 lines) contains one interpolation, `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` (`:1388`), so the whole body is evaluated as one expression. The estimate, based on line count times the observed width of about 50-60 characters after YAML de-indentation, is 15,000-18,000 characters. That leaves about 3,000-6,000 characters before 21,000 [NEEDS VERIFICATION: measure with the test's `len(run)`]. The body is a growing polling script.
- **Recommended fix:** Move the value into the step's `env:` (`IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`) and drop the assignment at `:1388`. This is the established pattern, cited at `:2094` ("Promoted to env so the run: script body contains no ${{ }}"). The body then falls outside the limit.

**EXPR-002** · `.github/workflows/test-and-mark-stable.yml:1035-1312` (step `Phase 2: Wait for plan to complete`); also `:881-1020` (`Phase 1`, `:929`) · Medium · `expression-limit`
- **Description:** The Phase 2 body (about 270 lines) interpolates `${{ steps.create-issue.outputs.created_after }}` three times (`:1061`, `:1073`, `:1091`). Estimated length is 13,000-17,000 characters, leaving about 4,000-8,000 characters of headroom [NEEDS VERIFICATION]. Phase 1 interpolates the same value once at `:929` and is about 140 lines (low risk).
- **Recommended fix:** Add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to each step's `env:` and replace the three (Phase 2) and one (Phase 1) inline references with `"${CREATED_AFTER}"`.

**EXPR-003** · `tests/test_workflow_file_size_limit.py:66-98` · Low · `expression-limit`
- **Description:** The guard checks only `jobs.*.steps[].run`. It does not check step or job `if:`, `with:` values, `env:` values, `concurrency.group`, `run-name`, or composite action bodies (`.github/actions/*/action.yml`). The longest `if:` found is about 900 characters (`review_autofix.yml:3855`; also `:4226` and `:4256`), so nothing is at risk today, but the blind spot covers places where prompt or template literals tend to land. There is also no warning band at 15,000.
- **Recommended fix:** Extend the test to measure every string value containing `${{` under `if`, `with`, `env`, `run-name` and `concurrency`, including composite actions. Add a non-failing 15,000-character report line (or emit it into `scripts/ci_budget_watch.py`'s output) so growth is visible before the 18,000 hard guard.

**EXPR-004** · workflow file sizes (`implement.yml` 7,174 lines; `test-and-mark-stable.yml` 6,569; `review_autofix.yml` 5,816; `ci.yml` 2,579) · Low · `expression-limit`
- **Description:** The prompt's 1 MB / 800 KB thresholds do not apply here. The binding limit is GitHub's 512,000-byte workflow limit, enforced at 480,000 by `tests/test_workflow_file_size_limit.py:26-59` (§24). No file can be above 800 KB while CI is green. Byte sizes were not measured in this audit. Based on line counts, the three largest files are the ones closest to the 480,000 guard; that is an inference [NEEDS VERIFICATION with `wc -c`]. `tests/test_ci_budget_watch.py:54` uses 481,287 bytes for `review_autofix.yml` as a fixture value only.
- **Recommended fix:** Have the existing `budget-watch` job (`scripts/ci_budget_watch.py`) log the actual size and headroom of the top three workflows each CI run. When a file passes about 430,000 bytes, move its largest inline bodies, for example `review_autofix.yml:1140-1460` (`deterministic-skip-merge`, about 320 lines with no `${{ }}`), into `scripts/review_autofix_step_<slug>.sh` following the §24 pattern.

### Section 5: Cross-Cutting Concerns

**DEAD-001** · `.github/workflows/pr6576-shard-followup.yml:14-64, 92-97`; `docs/scripts-pending-removal.md:331-340` · Low · `dead-code`
- **Description:** This single-use workflow still runs on every push to `main`. Its `self-test` job installs pytest and runs before the one-time marker guard in `locate` (`:92-97`), so every `main` push uses a runner even after the follow-up has finished. The registry entry's "Introduced in" field still reads "PR number assigned when the implementation PR opens" (`:333`), which leaves the §20.F field incomplete.
- **Recommended fix:** Run the registry's removal preflight checks (`:337-339`). If they pass, delete the workflow, its script and test, and the registry entry in one PR. Otherwise restrict `self-test` to `pull_request` (`if: github.event_name == 'pull_request'`) and fill in the PR number.

**CONSIST-001** · `.github/workflows/review_rb_judge_dispatch.yml:46`, `unblock_judge_dispatch.yml:32`, `internal-cancel-on-pr-close.yml:16, 45-50` vs other `internal-*.yml` (for example `internal-review.yml:56`) · Low · `consistency`
- **Description:** Most internal wrappers call reusable workflows `@promote-candidate`, but the two judge-dispatch wrappers use `@main`. In `internal-cancel-on-pr-close.yml`, the `cancel` job runs `cancel_on_pr_close.yml@promote-candidate` (whose support code follows the protected `promote-candidate` tip, `cancel_on_pr_close.yml:36-53`), while the sibling `heal-pr-reconcile` job checks out `ref: main` and runs `scripts/workflow_failure_heal_pr_reconcile.sh` from there. During a promote cycle, one PR-close event runs support code from two refs. Whether this is intentional is undocumented [NEEDS VERIFICATION].
- **Recommended fix:** Either pin the three to `promote-candidate`, consistent with the promote-cycle plan, or add a header comment in each file explaining why `main` is required.

**CONSIST-002** · `scripts/orchestrate_poll_process.sh:23498-23501`; `.github/workflows/review_autofix_sweep.yml:437-439, 504`; `.github/workflows/review_autofix.yml:1853-1854` · Low · `consistency`
- **Description:** The comment says the cycle-local rb-judge guard exists because "cancel-in-progress concurrency would otherwise kill one with the other". In fact `review_rb_judge_dispatch.yml` has no concurrency block, and the codex-agent group `pr-autofix-<N>` uses `cancel-in-progress: false` for PR branches. The real interaction is pending-run replacement: a queued rb-judge run can be replaced by a newer review run in the same group. The sweep's active-run snapshot covers only `internal-review.yml` and `review_autofix.yml`, so it never sees an rb-judge run and can dispatch the review that replaces it.
- **Recommended fix:** Correct the comment. Add `review_rb_judge_dispatch.yml` to the sweep's snapshot list at `:437` and `:504` (keyed by head branch or a new `run-name: "Internal: Review-Blocked Judge [pr:<N>]"`), so the sweep skips PRs with an rb-judge run in flight.

**CONSIST-003** · `scripts/tg_helpers.sh:207-217, 244-248, 263-274, 312-316, 332-342, 398-401, 430-434, 467-470, 501-505` · Low · `consistency`
- **Description:** In the same file, GETs go through `curl_gh_api` (retry plus rate-limit handling), while every POST, PATCH and DELETE uses raw `curl -s` with no `-f` and discarded output, so failures are invisible and never retried.
- **Recommended fix:** Use `curl_gh_api -f` for the writes, with `--idempotent` semantics only for PATCH and DELETE, and log one `::warning::` per failed write. Leave POST creates as a single attempt, consistent with #6634.

**CONSIST-004** · `.github/workflows/forward-merge-stable-to-main.yml:320-324` · Low · `consistency`
- **Description:** §21 requires `scripts/lint_pr_body_auto_close.py` before every pipeline `gh pr create --body`. This workflow creates a PR without the lint. The body is static and contains no `#N` references, so the practical risk is nil, but the contract is not met.
- **Recommended fix:** Run the lint on `${BODY}` and the title before `gh pr create`, treating a non-zero exit as a hard failure, or document the exemption in the workflow header.

**SHELL-001** · `.github/workflows/forward-merge-stable-to-main.yml:445-446`; `validate.yml:1714, 1719`; `cancel_on_pr_close.yml:33-34`; `orchestrate_poll.yml:2005-2007`; `clarify.yml:73-76` · Low · `shellcheck`
- **Description:** Trusted values (`steps.*.outputs`, `github.repository`, `github.sha`, `github.run_id`, `job.status`) are interpolated with `${{ }}` directly in run bodies, even though the env-indirection pattern is used elsewhere in the same files. None is injectable today, but each one pulls the whole body under the 21,000-character expression rule (Section 4) and is invisible to shellcheck.
- **Recommended fix:** Move each value into the step's `env:`, for example `CHECK_AHEAD: ${{ steps.check.outputs.ahead }}` and `DIRECT_STATUS: ${{ steps.direct.outputs.merge_status }}` at `forward-merge-stable-to-main.yml:441-446`. Use `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`, which the runner already exports.

TODO/FIXME/HACK markers: none in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`. The only `XXX` hits are `mktemp` templates.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | API-001 |
| Medium | 7 | BUG-001, BUG-002, BUG-004, API-002, DUP-001, EXPR-001, EXPR-002 |
| Low | 25 | BUG-003, BUG-005, BUG-006, BUG-007, BUG-008, BUG-009, SEC-001, API-003, API-004, API-005, API-006, DUP-002, DUP-003, DUP-004, DUP-005, DUP-006, EXPR-003, EXPR-004, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `tests/test_validate_process_fixup_labels.py` | Small |
| API call optimization | `.github/workflows/review_autofix.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/review_autofix_sweep.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh` | Medium |
| Code modularization | `.github/workflows/review_autofix.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/forward-merge-stable-to-main.yml`, `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh`, 13 workflows carrying the `gh_api_retry` shim | Medium |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `tests/test_workflow_file_size_limit.py`, `scripts/ci_budget_watch.py` | Small |
| Medium/Low fixes | `.github/workflows/check_failure_triage.yml`, `.github/workflows/validate.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/forward-merge-stable-to-main.yml`, `.github/workflows/clarify.yml`, `.github/workflows/pr6576-shard-followup.yml`, `.github/workflows/review_rb_judge_dispatch.yml`, `docs/scripts-pending-removal.md`, `scripts/tg_helpers.sh`, `scripts/orchestrate_poll_process.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-24)

### Safety Tag Legend
`SAFE_TO_MERGE` means a static reading proves every precondition. The calls hit the same endpoint with identical arguments, filters and auth. They sit in the same step or function with no mutation in between. Error handling and retries stay the same, no concurrency boundary is crossed, and no cache contract changes. The implement stage may act on these without further review. `NEEDS_VERIFICATION` means the consolidation looks valid, but at least one precondition could not be proven from source alone. The listed checks must pass before anyone implements it. `RISKY_SKIP` means a hard trigger applies: pagination, retry or race-defence paths, auth or identity probes, rate-limit probes, `orchestrate_poll_process.sh` or judges, or log keys other code greps for. These are listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001** · `SAFE_TO_MERGE`
- **Files and lines:** `.github/workflows/review_autofix.yml:1679` (pre-loop ensure), `:1681-1687` (per-issue loop), `:1689-1694` (no-issue else branch), fallback helper `:1613-1618`; shared helper `scripts/label_helpers.sh:206-223`.
- **Endpoint:** `POST /repos/{owner}/{repo}/labels` (`gh label create`), same label `ai:review-blocked`, same repo.
- **Current vs. proposed calls:** with N ≥ 1 linked issues, 1 + N creates today, N after the change. Under API-001 each create can cost up to 5 attempts. The no-issue path stays at 1.
- **Evidence:** in the `fingerprint-cap-block` job, `Apply identical-failure cap outcome` (which runs `set -uo pipefail`, no `-e`, at `:1577`) runs
  ```bash
  ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true
  ```
  and then calls `set_issue_phase_label_resilient "${issue_number}" "ai:review-blocked" "${REPOSITORY}"` for each linked issue. Both forms of that helper first call `ensure_label_exists "<label>" "<repo>" || true` for the same label:
  - shared helper: `label_helpers.sh:223`
  - inline fallback: `review_autofix.yml:1615`

  The pre-loop call therefore only matters on the else branch (`:1690`), which POSTs the label straight onto the PR.
- **Proposed fix:** delete `:1679` and put `ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true` as the first line of the `else` branch, just before the PR label POST at `:1690`.
- **Safety rationale:** both calls are in the same step and call the same function with the same label and repo, so color and description come from the same catalog or the same fallback. Nothing in between changes label state. Both calls end in `|| true`, so failure handling and `gh_retry` behaviour stay the same. The step's per-PR concurrency lock (`:1623-1625`) is not crossed, and no new log key is added or removed.
- **Downstream signal:** in `review_autofix.yml`, move `ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true` from `:1679` into the `else` branch before `:1690`, and change nothing else.

**MERGE-002** · `NEEDS_VERIFICATION`
- **Files and lines:** `scripts/review_autofix_step_mark_ready_to_merge.sh:76` (pre-loop ensure) and `:77-81` (loop calling `set_issue_phase_label_resilient`). Fallbacks at `:15-29` and `:30-50`; shared `scripts/label_helpers.sh:195-196, 223`.
- **Endpoint:** `POST /repos/{owner}/{repo}/labels` (`ai:ready-to-merge`).
- **Current vs. proposed calls:** 1 + N today, N after the change.
- **Evidence:** this is the same duplicate as MERGE-001. The difference is that `:76` has no `|| true`, and the script runs under `set -euo pipefail` (`:7`). With the shared helper loaded, `ensure_label_exists` returns 1 on a real create failure (`label_helpers.sh:195-196`), so today the step stops before any issue is labelled. The call inside `set_issue_phase_label_resilient` is `|| true`, so if `:76` is removed the step would continue and POST the labels.
- **Proposed fix:** delete `:76`, but only after confirming that the fail-fast exit is not relied on.
- **Safety rationale:** the merge changes error-handling semantics: a failed create aborts the step today and would not after the change. That breaks the SAFE_TO_MERGE "same failure semantics" precondition.
- **Downstream signal:** before editing, confirm two things.
  1. Nothing downstream depends on the `Mark linked issues ready to merge` step (`review_autofix.yml:4256-4264`) failing when label creation fails. Check the later steps' `if:` conditions and the step's `continue-on-error`.
  2. The label POST at `label_helpers.sh:229` or `:226-239` succeeds against an existing label even when the create failed.

  If both hold, delete `:76`.

**MERGE-003** · `RISKY_SKIP`
- **Files and lines:**
  - `scripts/workflow_failure_heal_non_retryable_backfill.sh`: `:150` (`gh api user`), `:161` and `:229-260` (paginated GraphQL `pullRequests(states: OPEN, first: ≤10, orderBy: UPDATED_AT ASC) { … comments(last: 100) … }`), `:172-226` (totalCount plus window-skip calls).
  - `scripts/review_security_hold_sweep.py`: `:47-60` and `:276-283` (paginated GraphQL `pullRequests(first:50, states:OPEN, orderBy: UPDATED_AT DESC) { … comments(last:100) … }`), `:203-216` (`gh api user` identity probe), `:178-186` (per-PR comment pagination).
  - Both run as consecutive steps of the sweep job: `.github/workflows/review_autofix_sweep.yml:560-575` and `:600-609`.
- **Endpoint:** GraphQL `repository.pullRequests` (two queries) and `GET /user` (two identity reads).
- **Current vs. proposed calls:**
  - Backfill: 1 + 1 + skip calls + ceil(MAX_PRS/10), which is 5 page calls at the default.
  - Security hold: 0–1 identity calls + ceil(open/50) pages, plus per-PR comment pages.
  - A single shared listing could in theory serve both.
- **Evidence:** both steps list open PRs with their last 100 comments in the same job tick. The backfill header (`:39-40`) says "the sweep keeps no PR comment snapshot to reuse".
- **Proposed fix (not authorised):** none for auto-implementation. A shared GraphQL page set written to `${RUNNER_TEMP}` would need one ordering, one page size and one token for both consumers.
- **Safety rationale:** several hard triggers apply.
  - Both are paginated, with different ordering (ASC vs DESC), page size and window rotation (`:165-226`).
  - Both identity reads are auth probes: the security hold may use a separate `SECURITY_HOLD_FINDING_AUTHOR_TOKEN` (`:236-243`).
  - The steps run under different `if:` gates, and the security step uses a different token (`GH_PAT || github.token`).
- **Downstream signal:** do not auto-implement. A manual review must decide whether one ordering can serve both. The backfill's least-recently-updated rotation is deliberate (`:41-45`). It must also confirm that both consumers accept the same token's visibility before any shared snapshot is designed.

**MERGE-004** · `RISKY_SKIP`
- **Files and lines:** `scripts/check_failure_triage.sh:567` (raw `gh api`) and `:571` (`gh_retry gh api`, same URL).
- **Endpoint:** `GET /repos/{owner}/{repo}/git/ref/heads/{ref}`.
- **Current vs. proposed calls:** on success, 1 call either way. On a non-404 failure, 1 + up to 5 today, against 1 classified call with up to 5 attempts.
- **Evidence:**
  ```bash
  if gh api "repos/${REPO}/git/ref/heads/${integration_ref_encoded}" >/dev/null 2>"${integration_ref_err_file}"; then
  …
  elif gh_retry gh api "repos/${REPO}/git/ref/heads/${integration_ref_encoded}" >/dev/null 2>&1; then
  ```
  The raw probe exists only to detect a 404 before retrying.
- **Proposed fix (not authorised):** use one `gh_api_retry` call and branch on `GH_API_RETRY_LAST_STATUS=404`, as `review_autofix_sweep.yml:339-344` already does.
- **Safety rationale:** this is a retry path, and the `integration_branch outcome=skip reason=branch_missing|lookup_failed` lines (`:570`, `:574`) are log output that triage consumers may grep.
- **Downstream signal:** do not auto-implement. A manual review must confirm that `gh_api_retry` is always loaded in this script, which uses `gh_retry` today. It must also confirm that the 404 classification produces the same `branch_missing` and `lookup_failed` outcomes, and that no consumer parses the current reason tokens differently.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** · `NEEDS_VERIFICATION`
- **Files and lines:**
  - `scripts/review_autofix_step_evaluate_gate.sh:187-207`: the gate GraphQL at `:190-196` sets `post_merge_linked_issues_cache_known=true` at `:198` but never exports it. Outputs are at `:1361-1363`.
  - `.github/workflows/review_autofix.yml:756` (job `if:`) and `:850-869`: the dispatch job re-runs the GraphQL.
- **Endpoint:** GraphQL `pullRequest.closingIssuesReferences(first: 50) { number labels(first: 100) }`. The query strings at `evaluate_gate.sh:194` and `review_autofix.yml:860` are byte-identical.
- **Current vs. proposed calls:** for a merged PR with no closing references and non-empty title or body, 2 identical GraphQL calls today, 1 after the change.
- **Evidence:** the job is skipped only when `post_merge_validate_context_definitely_empty == 'true'`, which requires an empty cache and empty PR text (`evaluate_gate.sh:202-206`). If the gate's read succeeded with `[]` and the PR text is non-empty, the job starts. At `:855`, `issue_nodes_json = "[]"` then triggers `gh_retry gh api graphql …` with the same query again before the body/title fallback.
- **Proposed fix:** add a gate output `post_merge_linked_issues_cache_known` (written next to `:1362`), pass it into the job's `env:`, and wrap `review_autofix.yml:855-869` in `if [ "${issue_nodes_json}" = "[]" ] && [ "${POST_MERGE_LINKED_ISSUES_CACHE_KNOWN:-false}" != "true" ]`. A failed or unknown gate read keeps today's live retry.
- **Safety rationale:** the two calls are in different jobs, so this crosses a job boundary. The gate uses raw `gh api graphql` with no retry, while the dispatch uses `gh_retry`. The cached value is therefore a snapshot from before the job hop, not a same-step merge.
- **Downstream signal:** before editing, confirm three things.
  1. `post_merge_linked_issues_cache_known` and the env name `POST_MERGE_LINKED_ISSUES_CACHE_KNOWN` do not collide with any existing identifier (search `.github/` and `scripts/`).
  2. A merged PR's `closingIssuesReferences` cannot change between the gate and the dispatch job.
  3. The consumer template copy of this job, if any, receives the new output (§8 default `false`).

**REUSE-002** · `NEEDS_VERIFICATION`
- **Files and lines:** `.github/workflows/review_autofix_sweep.yml:228-237` (paginated `GET /repos/{r}/pulls?state=open`, with `READ_TOKEN`) and `:285` (`GET /repos/{r}`, `--jq .default_branch`, with `READ_TOKEN`). The value is used at `:350` and `:376-380` for review-run provenance (#6629).
- **Endpoint:** `GET /repos/{owner}/{repo}` and `GET /repos/{owner}/{repo}/pulls`.
- **Current vs. proposed calls:** ceil(open/100) + 1 today, ceil(open/100) after the change.
- **Evidence:** the line-285 read runs only after `total > 0` (`:273-276`), so at least one non-draft open PR is in the listing that was just fetched. Each PR object's `base.repo` names this repository, and the jq projection at `:230-237` drops it.
- **Proposed fix:** capture `.base.repo.default_branch` from the raw listing, the first element after `add`, and validate it with the existing regex and `null` check at `:286`. Keep the exact `AUTOFIX_SWEEP_SKIPPED reason=default_branch_unavailable` no-dispatch path when the value is missing.
- **Safety rationale:** both calls use the same token in the same step with no mutation in between. However, `base.repo.default_branch` in the pulls-list payload has not been confirmed from repo evidence, and the comment at `:282-284` deliberately makes this a single provenance read with no fallback. `review_merge_train.sh:1137-1149` (`_mt_resolve_default_branch`) is the release-path equivalent. It is deliberately not covered here, because it sits in race-defended release code (RISKY_SKIP class).
- **Downstream signal:** before editing, verify three things.
  1. Against a live response, every open PR object from `repos/{r}/pulls` carries a non-null `base.repo.default_branch` equal to `repos/{r}.default_branch`.
  2. `tests/` holds no fixture or contract test that pins the separate `repos/{r}` read.
  3. The logged skip line and its keys stay unchanged. Coordinate with API-006, which edits the same lines (`:228`, `:285`).

**REUSE-003** · `NEEDS_VERIFICATION`
- **Files and lines:** `scripts/review_run_reviewers.sh:455-475`. A raw `gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state'` runs at `:464` when `PR_PAYLOAD_FILE` is older than 120s. The same file defines and uses `gh_review_pr_state` at `:16-17`, and the cached form is at `scripts/gh_helpers.sh:83-118`.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{n}`.
- **Current vs. proposed calls:** 1 per review round when the payload is stale, against 0–1, because the job-local cache is shared with the reviewer and editor watchdogs.
- **Evidence:** the output filter is identical (`grep -xE 'open|closed|merged' || echo open`). `gh_review_pr_state` adds a flock-guarded job-local cache that stores only `open`, for 120s.
- **Proposed fix:** replace `:464` with `preflight_state="$(gh_review_pr_state "${REPOSITORY}" "${PR_NUMBER}")"`.
- **Safety rationale:** the preflight is a closed-PR short-circuit, a race-defence check. A cached `open` up to 120s old could let a fan-out start on a PR closed inside that window, so the result may differ from today's live read.
- **Downstream signal:** before editing, confirm that no watchdog writes the cache before line 464 runs. If none does, the change saves nothing on the preflight and only seeds the cache for the watchdogs, which still saves 1 call per round. Also confirm that seeding is acceptable given the comment at `gh_helpers.sh:81-82` ("A cache entry is never used as merge authorization").

**REUSE-004** · `RISKY_SKIP`
- **Files and lines:** `scripts/review_merge_train.sh:800-806` (`_mt_find_marker_comment`: paginated `GET issues/{pr}/comments`, which returns bodies but emits only `id created_at`) and `:878-883` (`_mt_upsert_comment`: an extra `GET issues/comments/{id}` to compare the body before the PATCH). The caller passes the already-looked-up ID at `:993-999`.
- **Endpoint:** `GET /repos/{r}/issues/{n}/comments` (paginated) and `GET /repos/{r}/issues/comments/{id}`.
- **Current vs. proposed calls:** listing pages + 1 today, listing pages only after the change.
- **Evidence:** the body comparison at `:880` uses data the listing already returned.
- **Proposed fix (not authorised):** have `_mt_find_marker_comment` also emit a body digest, for example `\(.body | @base64)`, so the upsert can compare without a re-read.
- **Safety rationale:** the source call is paginated, and the merge train explicitly defends against races (the bypass and marker logic at `:815-858`, the listing-shift retries at `:1125-1131`). The fresh GET is also a newer read than the listing (TOCTOU).
- **Downstream signal:** do not auto-implement. A manual review must confirm that changing the `id created_at` output contract does not break `_mt_find_marker_comment_id` (`:808-813`) or the bypass check, which parses `created_at` positionally at `:963-964`.

**REUSE-005** · `RISKY_SKIP`
- **Files and lines:** `.github/workflows/review_autofix_sweep.yml:228-239` (open-PR snapshot written to `ci-cancelled-pr-snapshot.json`) and `scripts/ci_cancelled_rerun.py:117-145` (`current_pr_heads`: a second paginated `GET pulls?state=open`, same `github.token`). The snapshot is read at `:405`.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls?state=open&per_page=100`.
- **Current vs. proposed calls:** 2 paginated listings per tick today, 1 after the change (not recommended).
- **Evidence:** the second listing is documented as intentional: `# The sweep's earlier /pulls listing predates review dispatch; … a failed/partial page cannot authorize a rerun.`
- **Proposed fix:** none. It is recorded so the duplicate is not "fixed" by mistake.
- **Safety rationale:** the call is paginated, it is a deliberate freshness re-read that guards rerun authorisation against head moves, and it runs in a separate step after review dispatches.
- **Downstream signal:** do not auto-implement. Merging would let a stale head SHA authorise `rerun-failed-jobs` (`ci_cancelled_rerun.py:168`). Any change needs a design review of the freshness contract.

### Dead Calls (DEAD-API-###)

**DEAD-API-001** · `SAFE_TO_MERGE`
- **Files and lines:** `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:31` (pre-resolution ensure), `:33-55` (issue resolution, `exit 0` at `:52`) and `:57-61` (loop). Fallback helpers are at `:10-18` and `:19-29`, with the ensure at `:24`; shared ensure at `scripts/label_helpers.sh:223`.
- **Endpoint:** `POST /repos/{owner}/{repo}/labels` (`ai:review-blocked`).
- **Current vs. proposed calls:**
  - No linked issues: 1 today, 0 after the change.
  - N ≥ 1 linked issues: 1 + N today, N after the change.
  - Under API-001 each create can cost up to 5 attempts.
- **Evidence:**
  ```bash
  ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true
  ```
  runs before linked issues are known.
  - With no linked issues, the script exits at `:52` without applying the label, so the create's result is never used.
  - With linked issues, every `set_issue_phase_label_resilient` call (`:60`) re-runs `ensure_label_exists` for the same label: the shared helper at `label_helpers.sh:223`, or the fallback at `:24`.

  The step runs on every `failure()` of codex-agent (`review_autofix.yml:5500-5501`), which makes it the most frequent of the duplicate-ensure sites.
- **Proposed fix:** delete line 31.
- **Safety rationale:** both calls are in the same step and call the same function with the same label and repo. Nothing in between changes label state. Both calls end in `|| true` under `set -euo pipefail`, so failure handling and `gh_retry` behaviour stay the same. No concurrency boundary is crossed, and no log key other code consumes is affected: the helper only emits `::debug::` or `::warning::` annotations.
- **Downstream signal:** delete `ensure_label_exists "ai:review-blocked" "${REPOSITORY}" || true` at `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:31`, and change nothing else.

Considered and rejected:
- The 12 inline `gh api rate_limit` budget probes (DUP-003) are not quota-wasting dead calls. `scripts/gh_helpers.sh:30` documents that `/rate_limit does not debit the primary REST allowance`, so consolidating them saves no quota.
- The double `pulls/{n}` read at `test-and-mark-stable.yml:1750-1752` (`HEAD_A`/`HEAD_B`) is a deliberate stability check, not a duplicate.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — gh's real "already exists" stderr is not checked, and adding `already[ _-]?exists` to `_is_gh_permanent_failure` changes retry behaviour for every `gh_retry` caller. Net-new detail: DEAD-API-001 and MERGE-001/002 remove 1 duplicate create per invocation at three sites, so they cut the API-001 waste even before the regex or cache lands. Inside the per-issue `set_issue_phase_label_resilient` loops the cost multiplies (N creates of the same label), and only the proposed per-job cache removes that.
- API-002: NEEDS_VERIFICATION — sourcing the shared helpers changes retry behaviour for `gh pr merge --auto` (`review_autofix.yml:1338`, `:1375`). Net-new detail: the `.codex-freshness-src` sparse checkout (`:1120-1123`) contains `scripts/gh_helpers.sh` but not `scripts/label_helpers.sh`, so the implement stage must add it to the sparse-checkout list before it can source `ensure_label_exists` from the verified commit.
- API-003: RISKY_SKIP — paginated comment reads interleaved with DELETEs (the BUG-002 page-shift); a shared TSV cache would cross functions and a mutation boundary.
- API-004: NEEDS_VERIFICATION — retrying an explicit `-X POST` cancel goes against the #6634 single-attempt rule. Confirm `gh_retry --idempotent` treats 409 (already completed) as permanent, so it does not retry.
- API-005: NEEDS_VERIFICATION — the per-issue `gh issue view` loop (`review_autofix.yml:910-925`) could be batched, but labels must keep `labels(first:100)` parity with the gate query. Apply after REUSE-001 so the batch runs only on the body/title fallback path.
- API-006: RISKY_SKIP — the `:228` change wraps a `--paginate` listing in a new retry wrapper, whose restart semantics after a failure mid-pages need manual review. It also edits the same lines as REUSE-002, so the two must be coordinated.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 2 | DEAD-API-001, MERGE-001 |
| NEEDS_VERIFICATION | 4 | MERGE-002, REUSE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 4 | MERGE-003, MERGE-004, REUSE-004, REUSE-005 |

### Implement-Stage Handoff
- DEAD-API-001: delete the pre-resolution `ensure_label_exists` at `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:31`. It runs on every failed codex-agent run.
- MERGE-001: move `ensure_label_exists "ai:review-blocked" …` at `.github/workflows/review_autofix.yml:1679` into the no-issue `else` branch before `:1690`.