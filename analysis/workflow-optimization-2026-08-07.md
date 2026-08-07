## Executive Summary

- All 13 failed runs in this window were **queue-only cancellations before the first step**, not in-workflow crashes: 7 `orchestrate_poll` runs (`31119277360`, `31121706626`, `31123796470`, `31125606385`, `31126416665`, `31127100007`, `31128176347`) and 6 `review_autofix` sweep runs (`31118932302`, `31121899283`, `31124915847`, `31126540575`, `31127165655`, `31128352239`) each had a single job with `conclusion=cancelled`, `runner_name=""`, and `steps=[]`; representative GitHub log fetches returned `log not found` for `31127100007` and `31127165655`. Estimated impact: removes most of the visible 18.1% failure rate and ~5.34 hours of wasted wall time if fixed. Confidence: **high** on symptom, **medium** on root cause.

- Scheduled cadence is materially slower than configured. `.github/workflows/internal-orchestrate-poll.yml` is `*/5`, but the last 30 observed gaps were `min=23.1m`, `p50=51.7m`, `max=130.5m`; `.github/workflows/review_autofix_sweep.yml` is `*/30`, but observed gaps were `min=24.6m`, `p50=53.3m`, `max=133.5m`. Estimated impact: 20–45+ minutes slower poll/review response on the critical path. Confidence: **high**.

- Successful poller ticks are mostly **runner-wait bound**. `orchestrate_poll` is `24` runs with `p50=209.5s`; `16/17` successful assembled summaries explicitly mention hosted-runner wait, and sampled successes such as `31130941452`, `31116081048`, `31094379742`, `31085998054`, and `31078149659` say `poll/system` waited for a runner. Estimated impact: queue-pressure fixes should save ~3–4 minutes on healthy poll ticks. Confidence: **high**.

- `review_autofix` sweep is frequently a **no-op runner burn**. `15/20` successful `review_autofix` summaries ended with `candidates=0`; run `31104154939` spent ~0.6s doing sweep work and the rest waiting for a hosted runner. Estimated impact: quiet-period backoff would save most empty sweep starts and reduce queue pressure on more important jobs. Confidence: **high**.

- Cost observability is currently unreliable. `analysis/analysis_context.json` reports `runs_with_log_telemetry=72`, `or_calls=18`, `semble_query_calls=3`, while `workflow_log_report.json` / bundle `summary.json` reports `runs_with_log_telemetry=20`, `or_calls=12`, `semble_query_calls=1`; both still show `or_total_tokens=0`, `or_cache_* = 0`, `cache_hit_rate=null`. Estimated impact: no safe model/cost tuning can be justified until telemetry is repaired. Confidence: **high**.

- Semble looks low-volume and mostly healthy; Serena is effectively absent. Current aggregate telemetry shows `SEMBLE_QUERY` only on `review_autofix` (`3` calls, `46,178` bytes) with `0` runtime fallbacks; CI produced `10` `SEMBLE_FALLBACK` events, all `context=contract-test`; `SERENA_QUERY`, `SERENA_FALLBACK`, and `SERENA_PROBE` are all `0` while logs repeatedly show `SERENA_AVAILABLE: false`. Estimated impact: low immediate cost leverage, high observability leverage. Confidence: **high**.

## Speed Optimizations

1. **Critical-path: replace wrapper-level pending cancellation with an explicit in-job lease**
   - **Evidence:** All 13 failures were pre-step cancellations; both `.github/workflows/orchestrate_poll.yml` and `.github/workflows/review_autofix_sweep.yml` use workflow/job concurrency with `cancel-in-progress: false`; observed cadence is far slower than cron (`*/5` behaving like ~52m p50, `*/30` behaving like ~53m p50).
   - **Root cause:** Inference: GitHub hosted-runner queueing plus concurrency-group pending replacement is likely canceling older scheduled ticks before a runner is assigned.
   - **Exact change:** For scheduled wrappers only, stop using Actions concurrency as the only dedupe boundary. Let the run start, emit a structured `RUN_DIAGNOSTICS` line, acquire a cheap repo-scoped lease, and self-exit `success` with `LEASE_SKIP` if another run owns the lease. Keep actual mutating phases serialized behind the lease.
   - **Estimated time savings:** Restores tens of minutes of schedule responsiveness and removes ~5.34 hours of queue-only failed wall time from this window.
   - **Implementation risk:** **Medium**; concurrency semantics change, so keep the lease fail-open and start with the wrapper workflows only.

2. **Critical-path: add quiet-period backoff to `review_autofix_sweep`**
   - **Evidence:** `15/20` successful `review_autofix` summaries had `candidates=0`; combined empty-sweep runtime was ~144s in this window; `6/20` successful sweep summaries also explicitly mention runner wait.
   - **Root cause:** Fixed 30-minute cadence continues even when there are no open non-draft PRs to review.
   - **Exact change:** Persist a consecutive-empty counter and, after 3 empty ticks, run every other scheduled tick until a PR open/synchronize/close event or a non-empty sweep resets the streak. Emit `AUTOFIX_SWEEP_IDLE streak=<n> next_allowed_at=<ts>`.
   - **Estimated time savings:** Removes most empty sweep starts in quiet periods; direct savings were small here (~2.4 runner minutes), but indirect queue relief should help the poller.
   - **Implementation risk:** **Low**.

3. **Micro: skip post-merge validation dispatch before spending a second runner**
   - **Evidence:** Run `31071292976` (`47s`) waited for hosted runners in both `review.gate` and `review.post-merge-validate-dispatch`, then logged `No tracking issue resolved for force-tick dispatch; skipping.`
   - **Root cause:** The workflow learns there is nothing to dispatch only after another job has already started.
   - **Exact change:** Promote `has_linked_tracking_issue` to a gate output and add a job-level `if:` so `post-merge-validate-dispatch` never requests a runner when linked tracking data is empty.
   - **Estimated time savings:** Tens of seconds and one runner allocation per such post-merge no-op.
   - **Implementation risk:** **Low**.

## Cost Optimizations

1. **Highest impact: repair OpenRouter and prompt-cache accounting before changing model mix**
   - **Evidence:** `review_autofix` aggregate telemetry shows `or_calls=18` but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`; deep-dive run `31068222331` alone had `12` OR calls across a `3966s` run; `cache_hit_rate=null` everywhere.
   - **Root cause:** Structured usage emission is missing or not being parsed consistently.
   - **Exact change:** Emit per-call `OR_USAGE` and per-phase `PROMPT_CACHE_TELEMETRY` with model, phase, prompt/completion/total tokens, cache read/write tokens, latency, and a stable prefix hash. Make `cost_audit.py` consume only those lines for OR totals.
   - **Estimated savings:** Unlocks measurement of almost all AI spend in this window; direct dollar savings cannot be safely estimated until this exists.
   - **Quality-risk notes:** **Low**; additive telemetry only. No safe recommendation to lower reasoning/model size yet.

2. **Cut empty sweep compute instead of tuning AI models blindly**
   - **Evidence:** `15/20` successful sweep runs were `candidates=0`; sample runs `31132137231`, `31114791033`, `31104154939`, `31096474195`, and `31089014006` did no review work.
   - **Root cause:** Fixed polling cadence spends runner time even when there is nothing to review.
   - **Exact change:** Implement the quiet-period backoff above.
   - **Estimated savings:** At least `15` sweep starts and `15` open-PR list API calls in this window would scale down proportionally.
   - **Quality-risk notes:** **Low** if PR lifecycle events reset the idle streak.

3. **Measure whether Semble is reducing prompt expansion**
   - **Evidence:** Aggregate assembled telemetry shows `SEMBLE_QUERY` only in `review_autofix` (`3` calls, `46,178` bytes, `0` runtime fallbacks). One deep-dive call in `31068222331` logged `SEMBLE_QUERY target=reviewer-context chunks=12 bytes=15306 ms=546`. `scripts/targeted_file_context.py` currently logs only `bytes`, not response bytes or downstream prompt savings.
   - **Root cause:** Current Semble telemetry measures request/rendered-byte volume, not value delivered.
   - **Exact change:** Add `response_bytes`, `accepted_chunks`, and `downstream_prompt_bytes_saved` to `SEMBLE_QUERY`.
   - **Estimated savings:** Unknown today; this is the measurement needed to decide whether Semble is shrinking prompts or just adding context noise.
   - **Quality-risk notes:** **Low**.

4. **Do not expand Serena usage until it produces measurable replacements**
   - **Evidence:** Aggregate telemetry: `SERENA_QUERY=0`, `SERENA_FALLBACK=0`, `SERENA_PROBE=0`; sampled logs repeatedly show `SERENA_AVAILABLE: false` in CI (`31071292869`) and review (`31068222331`).
   - **Root cause:** Serena is mostly disabled/unavailable on the paths that ran in this window.
   - **Exact change:** Keep rollout unchanged, but add startup `SERENA_PROBE` in poll/review paths and only make tool-substitution decisions after real `SERENA_QUERY`/per-tool byte/latency data appears.
   - **Estimated savings:** Avoids blind rollout cost and debugging overhead; no direct savings yet.
   - **Quality-risk notes:** **Low**.

## Reliability Improvements

Current window note: `break_glass_count=0`, `context_budget_warn_count=0`; all `SEMBLE_FALLBACK` events were contract-test only, and there were no structured Serena fallbacks/probes.

1. **Highest impact: turn queue-only scheduled-wrapper failures into visible self-skips**
   - **Failure evidence:** All 13 failures were job-level cancellations before any step/log existed; representative failures `31127100007` and `31127165655` had no downloadable logs from GitHub.
   - **Root cause category:** Scheduler/concurrency/runner-availability interaction (inference), not workflow logic failure.
   - **Exact fix:** Replace wrapper-level pending cancellation with an explicit lease + early `success` skip; emit `RUN_DIAGNOSTICS` and `LEASE_SKIP`/`LEASE_ACQUIRED`.
   - **Expected reliability impact:** Removes most of the window’s apparent failures and makes remaining ones attributable.
   - **Rollback / fail-open:** If lease behavior is wrong, revert wrappers to current concurrency; keep diagnostic emission either way.

2. **Patch the collector so pre-step cancellations are diagnosable**
   - **Failure evidence:** `scripts/collect_workflow_logs.py` `extract_failure_point()` only returns a point for `failure`, not `cancelled`; `_write_run_log_bundle()` writes only `metadata.json` when `full_logs` is absent; both `analysis_context.errors` and bundle `summary.json.errors` were `0` despite metadata-only failed runs and GitHub `log not found`.
   - **Root cause category:** Collector observability gap.
   - **Exact fix:** When a failed run has only cancelled jobs/no steps, write a synthetic failure point such as `step_name=cancelled_before_first_step` and add `job_conclusion`, `runner_name`, `labels`, `steps_count`, `started_at`, `completed_at`, and `log_archive_status=missing|empty|ok`.
   - **Expected reliability impact:** Large MTTR reduction; this is the only reliable evidence path for pre-run cancellations.
   - **Rollback / fail-open:** None; additive metadata only.

3. **Separate expected negative-test output from production-severity error lines**
   - **Failure evidence:** CI run `31071292869` succeeded but emitted `10` `SEMBLE_FALLBACK ... context=contract-test` lines plus many `##[error]` lines from expected test cases (`lint_pr_body_auto_close`, integration fingerprint verification).
   - **Root cause category:** Test telemetry sharing the same severity channel as live failures.
   - **Exact fix:** Prefix expected failures with `TEST_EXPECTED_ERROR` or `severity=test`, and/or route them to a side artifact while preserving pass/fail assertions.
   - **Expected reliability impact:** Fewer false escalations and faster real-incident triage.
   - **Rollback / fail-open:** Trivial; log-format-only.

4. **Guard known noisy warning / false-success paths**
   - **Failure evidence:** `review_autofix` runs `31068169410` and `31073757304` logged `fatal: /usr/lib/git-core/git-submodule cannot be used without a working tree.` and `exit code 1` warnings but still succeeded; `workspace_cache_maintenance` run `31072218162` logged `raise SystemExit(1)` text while ending `success`.
   - **Root cause category:** Cleanup and maintenance result propagation ambiguity.
   - **Exact fix:** Guard worktree-dependent cleanup with `git rev-parse --is-inside-work-tree` and emit `WORKTREE_CLEANUP_SKIP`; add a final `MAINTENANCE_RESULT delete_failures=<n> lookup_errors=<n> exit_status=<n>` line and fail explicitly when non-zero.
   - **Expected reliability impact:** Small-to-medium; fewer misleading warnings and fewer hidden maintenance defects.
   - **Rollback / fail-open:** Low risk.

## AI Memory Health

- **Observed coverage is sparse:** only `6` `AI_MEMORY_TELEMETRY` events were found across the deep-dive bundle, from two runs: `review_autofix` `31068222331` and `orchestrate_poll` `31130941452`.
- **Retrieve health (observed only once):**
  - `retrieve` events: `1`
  - hit rate: `100%` (`records_selected=30` on `1/1` retrieves)
  - average `estimated_tokens`: `1371`
  - average `token_budget`: `1400`
  - budget utilization: `97.9%`
  - `keyword_method` distribution: `llm=100%`, `plain=0%`, `none=0%`
- **Write/record health:** observed `record-run-event` for `phase_started`, `phase_completed`, `poll_started`, `poll_completed`, plus one `record-candidate`; every write-like event had `push_attempts=1`.
- **No bad memory telemetry was observed** in the sampled deep dives: no `records_selected=0`, no `fail_open: true`, no `enabled: false`, no high push-retry counts.
- **But memory coverage is incomplete:** no observed `finalize-task`, `promote`, `compact`, or processed-command telemetry; most runs had no memory lines at all.
- **Related warning signal:** Copilot code review run `31068225867` logged `[memory] vote completed ... success=false` after `HTTP 404: Could not verify access to this repository`, which suggests at least one memory-adjacent integration path is permission-sensitive outside the standardized telemetry.

**Recommendation:** verify `AI_MEMORY_TELEMETRY` emission in every long-running review/orchestrate/validate path, and add explicit `reason`, `enabled`, and `fail_open` fields to all memory operations so missing coverage is distinguishable from healthy no-op behavior.

## GH API Call Audit

- **Existing batching is good in `review_autofix_sweep.yml`:**
  - one paginated open-PR snapshot,
  - immediate exit when `candidates=0`,
  - otherwise only **two** active-run snapshots (`internal-review.yml`, `review_autofix.yml`) reused locally for all PRs.
  - This avoids the worst anti-pattern: per-PR `/actions/runs` polling.
- **Existing peer dedupe is also good:** `scripts/gh_helpers.sh` `autofix_retrigger_has_inflight_peer()` uses a single `/actions/runs` query and emits `AUTOFIX_PEER_CHECK`.
- **No rate-limit evidence was found:** sampled logs showed no `429`, secondary-rate-limit, or retry/backoff bursts.
- **The remaining API waste is schedule-level, not loop-level:** `15` empty sweeps still spent one pull-list API call each simply to discover `candidates=0`.
- **Best concrete change:** add quiet-period backoff to remove empty ticks, then add structured API audit emission around `gh api` / `gh workflow run`.

Recommended additive log shape:
```text
GH_API_AUDIT step=sweep endpoint_group=pulls calls=1 pages=1 bytes=... retries=0 ms=...
GH_API_AUDIT step=sweep endpoint_group=workflow_runs calls=2 pages=2 bytes=... retries=0 ms=...
GH_API_AUDIT step=peer_check endpoint_group=actions_runs calls=1 pages=1 bytes=... retries=0 ms=...
```

This would let future reports quantify call-count reduction directly instead of inferring from script structure.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is effectively absent.** In both aggregate artifacts, `cache_hit_rate=null`, `or_cache_read_tokens=0`, and `or_cache_write_tokens=0`.
- **Current window does not show prompt-budget distress.** `context_budget_warn_count=0` and `break_glass_count=0`; the system is configured with `CONTEXT_BUDGET_WARN_RATIO=0.7` in review logs, but no actual warning events were emitted.
- **The expensive path is clear, the cache behavior is not.** `review_autofix` accounted for `18` OR calls in the assembled context, but there is no way to tell whether shared prompt prefixes are hitting cache or being fragmented.
- **Semble telemetry is incomplete for cache/prompt analysis.** `scripts/targeted_file_context.py` logs `SEMBLE_QUERY ... bytes=...`, but not returned bytes or effective prompt savings.
- **Memory retrieval itself looks healthy in the one sampled retrieve** (`1371/1400` tokens, `30` records), but memory and cache are not correlated in telemetry.

Recommended additive telemetry:
```text
OR_USAGE phase=reviewer model=openai/gpt-5.4 prompt_tokens=... completion_tokens=... total_tokens=... latency_ms=...
PROMPT_CACHE_TELEMETRY phase=reviewer hit=true cache_read_tokens=... cache_write_tokens=... prefix_hash=... dynamic_tail_bytes=...
TARGETED_CONTEXT_TELEMETRY source=semble target=reviewer-context request_bytes=... response_bytes=... accepted_chunks=... downstream_prompt_bytes_saved=...
```

- **Estimated impact:** high observability gain; this is the minimum needed to diagnose cache fragmentation from unstable prefixes or dynamic prompt tails.
- **Current recommendation:** do **not** shrink prompts or downshift reasoning based on this window; there is no evidence of `CONTEXT_BUDGET_WARN`, and cache data is missing.

## Orchestrator Health

- The orchestrator’s visible problem in this window is **entry into the loop**, not mid-loop state corruption. `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all appeared only as skipped runs; there was no evidence of clarify-plan ping-pong or stuck wave transitions.
- `orchestrate_poll` itself is usually healthy once it starts (`17/24` succeeded), but it is slowed by runner wait and distorted by queue-only cancellations.
- The active branch-review path still progresses: `review_autofix` runs `31068169410` (`1376s`) and `31073757304` (`1110s`) both completed successfully for `claude/validate-consumer-issue-1zebie`, so the system is not broadly deadlocked.
- Observability for wave/deferral health is too thin. There were no structured counters for “claimed work,” “deferred work,” “active tracking issue count,” or “state transition reason.”

**Track these indicators next week:**
1. `queue_only_cancelled_runs`
2. `cron_gap_p50/p90` by scheduled workflow
3. `runner_wait_ms` at first runnable step
4. `empty_sweep_ratio`
5. `or_usage_coverage_ratio` and `ai_memory_telemetry_coverage_ratio`

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Highest-value fix |
|---|---|---|---|
| clarify → plan → implement | Inactive / skipped, not a live bottleneck | `clarify`, `plan`, `implement`, `orchestrate_clarify_respond` each appeared only as skipped runs in this window | No change until poller cadence is fixed |
| orchestrate poll | Queueing + pre-step cancellation | `7/24` poller runs failed before step start; `16/17` successes mention runner wait; p50 `209.5s` | Replace wrapper concurrency with explicit lease + queue diagnostics |
| review sweep wrapper | Idle no-op ticks | `15/20` successful sweep runs had `candidates=0`; one more (`31070173153`) only skipped for `active_run` | Quiet-period backoff; keep PR batching |
| full review/autofix | AI compute time | `31068222331` `3966s` with `12` OR calls; `31068169410` `1376s`; `31073757304` `1110s` | Fix OR/cache telemetry first, then optimize prompts/models from data |
| validate / CI / integration | Diagnostic noise obscures real issues | CI `31071292869` succeeded while emitting many `##[error]` lines and contract-test fallbacks | Separate expected negative-test output from production-severity errors |

**Order of attack:** queue/cadence first, no-op sweep second, OR/cache telemetry third, log-noise cleanup fourth.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - Scheduled wrapper queueing: `orchestrate_poll` p50 `209.5s`, `7` queue-only failures.
  - Mixed `review_autofix` family: fast no-op sweeps (`p50 13s`) and very slow full reviews (`1110s` to `3966s`) are aggregated together.
  - Runner wait dominates successful poller ticks.

- **Top failure modes**
  - Pre-step cancelled scheduled runs with no logs (`13` total failures).
  - CI success logs containing production-shaped `##[error]` output (`31071292869`).
  - Non-fatal worktree cleanup warnings in successful review runs (`31068169410`, `31073757304`).

- **Highest-cost drivers**
  - `Internal: AI Review & Autofix` main path, especially `31068222331` (`3966s`, `12` OR calls, `1` deep-dive `SEMBLE_QUERY`).
  - Cost attribution remains blind because OR tokens/cache metrics are zero/null.

- **Top 3 prioritized actions**
  1. Move scheduled wrapper dedupe from Actions concurrency to an explicit in-job lease and log queue diagnostics.
  2. Repair OR/cache telemetry and resolve the `analysis_context.json` vs `workflow_log_report.json` coverage mismatch.
  3. Add quiet-period backoff to `review_autofix_sweep` and skip post-merge dispatch jobs that have no tracking issue to act on.

## Metrics Appendix

**Aggregate source note:** aggregate counts below use `analysis/analysis_context.json` as primary because it matches the prompt’s assembled input; `workflow_log_report.json` is used below to show the current telemetry drift.

### Overall window

| Metric | Value |
|---|---:|
| Total runs | 72 |
| Success | 50 |
| Failure | 13 |
| Cancelled | 1 |
| Other / skipped | 8 |
| Avg duration | 475.1s |
| p50 duration | 186.0s |
| p95 duration | 2204.0s |

### Key workflow-family summary

| Workflow family | Runs | Success | Failure | Cancelled | p50 (s) | p95 (s) | Notable telemetry / note |
|---|---:|---:|---:|---:|---:|---:|---|
| `orchestrate_poll` | 24 | 17 | 7 | 0 | 209.5 | 1886.2 | `16/17` successful summaries mention runner wait; all 7 failures were queue-only cancelled |
| `review_autofix` | 26 | 20 | 6 | 0 | 13.0 | 2278.25 | Mixed family: fast sweep wrapper + slow full review path; `or_calls=18`, `SEMBLE_QUERY=3`, `SEMBLE bytes=46178` |
| `ci` | 2 | 1 | 0 | 1 | 1638.0 | 1802.7 | `SEMBLE_FALLBACK=10`, all contract-test only |
| `copilot_pull_request_reviewer` | 1 | 1 | 0 | 0 | 234.0 | 234.0 | Memory-adjacent 404 in log summary |
| `workspace_cache_maintenance` | 1 | 1 | 0 | 0 | 13.0 | 13.0 | Success log still contains `raise SystemExit(1)` text |

### Queue-only failure pattern

| Family | Failed run IDs | Common traits | Wasted wall time |
|---|---|---|---:|
| `orchestrate_poll` | `31119277360`, `31121706626`, `31123796470`, `31125606385`, `31126416665`, `31127100007`, `31128176347` | Sole job `poll / poll`; `conclusion=cancelled`; `runner_name=""`; `steps=[]`; labels `ubuntu-latest` | 10,412s (2.89h) |
| `review_autofix` sweep | `31118932302`, `31121899283`, `31124915847`, `31126540575`, `31127165655`, `31128352239` | Sole job `sweep`; `conclusion=cancelled`; `runner_name=""`; `steps=[]`; labels `ubuntu-24.04` | 8,807s (2.45h) |

### Cost / cache / orchestration telemetry

| Metric | `analysis_context.json` | Notes |
|---|---:|---|
| Runs with log telemetry | 72 | Primary assembled context |
| `or_calls` | 18 | All in `review_autofix` family |
| `or_prompt_tokens` | 0 | Blind spot |
| `or_completion_tokens` | 0 | Blind spot |
| `or_total_tokens` | 0 | Blind spot |
| `or_cache_write_tokens` | 0 | Blind spot |
| `or_cache_read_tokens` | 0 | Blind spot |
| `cache_hit_rate` | null | Prompt cache not usable yet |
| `break_glass_count` | 0 | No break-glass usage observed |
| `context_budget_warn_count` | 0 | No prompt-budget warning events observed |
| `wall_clock_p50_ms` | 187,500 | Aggregate assembled metric |
| `wall_clock_p99_ms` | 2,923,720 | Interpret cautiously given telemetry drift |

### Telemetry artifact drift that must be fixed

| Metric | `analysis/analysis_context.json` | `workflow_log_report.json` / bundle `summary.json` | Implication |
|---|---:|---:|---|
| `runs_with_log_telemetry` | 72 | 20 | Coverage mismatch |
| `or_calls` | 18 | 12 | Cost totals disagree |
| `semble_query_calls` | 3 | 1 | MCP totals disagree |
| `semble_query_bytes` | 46,178 | 15,306 | Byte totals disagree |
| `wall_clock_p50_ms` | 187,500 | 1,178,500 | Wall-clock summary drift |
| `wall_clock_p99_ms` | 2,923,720 | 3,687,080 | Wall-clock summary drift |
| `cache_hit_rate` | null | null | Still missing everywhere |

### Semble / Serena / other MCP summary

| MCP server | Target | Query calls | Logged bytes | Response bytes | Fallbacks | Probe ok | Probe failed | Probe skipped | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Semble | `reviewer-context` | 3 | 46,178 | not logged | 0 | n/a | n/a | n/a | One deep-dive sample: run `31068222331` logged `chunks=12 bytes=15306 ms=546` |
| Semble | `overflow` (`context=contract-test`) | 0 | 0 | n/a | 10 | n/a | n/a | n/a | All from CI run `31071292869`; no runtime fallbacks observed |
| Serena | all observed targets | 0 | 0 | 0 | 0 | 0 | 0 | 0 | Repeated `SERENA_AVAILABLE: false` env lines, but no structured query/probe telemetry |
| Other MCP servers | none in standardized telemetry | 0 | 0 | 0 | 0 | 0 | 0 | 0 | Copilot summary mentioned `github-mcp-server` and `playwright` connected/invocations=0 in run `31068225867` |

### AI memory snapshot

| Metric | Value |
|---|---:|
| `AI_MEMORY_TELEMETRY` events found | 6 |
| Runs with any memory telemetry | 2 (`31068222331`, `31130941452`) |
| Retrieve events | 1 |
| Retrieve hit rate | 100% |
| Avg `estimated_tokens` | 1371 |
| Avg `token_budget` | 1400 |
| `keyword_method=llm` share | 100% |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Push attempts >1 observed | 0 |

### GH API summary

| Workflow / helper | Observed pattern | Evidence | Estimated call reduction opportunity |
|---|---|---|---|
| `review_autofix_sweep.yml` empty ticks | One paginated open-PR snapshot, then exit on `candidates=0` | Script inspection + `15` empty successful sweeps | Quiet-period backoff removes up to `15` pull-list calls/window |
| `review_autofix_sweep.yml` non-empty ticks | Two batched active-run snapshots reused locally | Script inspection | Keep as-is; already good batching |
| `scripts/gh_helpers.sh` `autofix_retrigger_has_inflight_peer()` | One `/actions/runs` query, emits `AUTOFIX_PEER_CHECK` | Script inspection | Keep as-is |
| All sampled logs | No 429 / backoff bursts observed | Deep-dive logs and summaries | Main gain is fewer empty scheduled invocations, not per-call batching |

## Deep Audit — Workflows & Scripts (2026-08-07)

### Section 1: Bug & Correctness Sweep

Repo-wide shell/YAML/security sweep did not surface new secret-exposure or broad unquoted-variable defects worth separate findings. The material correctness risks were both “read failed, but workflow treated it as a clean no-op” paths.

- **BUG-001**
  - **File path:** `.github/workflows/review_autofix.yml:911-925,944-953,1083-1101`
  - **Severity:** High
  - **Category tag:** bug
  - **Description:** Two `review_autofix` paths collapse GitHub API read failures into successful “nothing to do” outcomes. In post-merge validate dispatch, the fallback GraphQL read at `914-919` uses raw `gh api graphql ... || true`, the PR text fallback at `925` uses raw `gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" ... || echo ""`, and the per-issue label check at `948-953` uses `gh issue view ... || true` and then treats empty output as `has_validate_label="false"`. In deterministic-skip merge, the linked-issue lookup at `1083-1088` also uses raw `gh api graphql ... || true`, and `1100-1101` logs “No linked issues found” and proceeds. A transient API failure can therefore skip standalone validate dispatch or skip linked-issue advancement on a successful workflow run.
  - **Recommended fix:** Extract a shared linked-issue resolver that returns a tri-state result (`resolved`, `empty`, `read_failed`) instead of collapsing failures into `[]`/empty string. Wrap the GraphQL and REST fallbacks with `gh_retry`/`gh_retry_to_file` from `scripts/gh_helpers.sh:407-575`, and reuse the batching approach already present in `scripts/review_collect_pr_metadata.sh:70-171`. On `read_failed`, emit a warning and stop the no-op path rather than treating the data as absent.

- **BUG-002**
  - **File path:** `scripts/comprehensive_test_and_release_gh_api.sh:3-38`; representative consumers in `.github/workflows/test-and-mark-stable.yml:563-572,596-597,699-707` and `scripts/dispatch_and_watch_workflow_run.sh:221-305`
  - **Severity:** Medium
  - **Category tag:** bug
  - **Description:** `gh_api_safe()` detects rate limiting, increases `RATE_LIMIT_BACKOFF`, sleeps, and then returns `1` at line `38` without retrying the same request. The calling workflows then coerce that failure into real-looking empty state (`""`, `0`, or `[]`). In `test-and-mark-stable.yml`, that can make labels/comments look absent; in `dispatch_and_watch_workflow_run.sh`, it can make a freshly dispatched run look unregistered or status-unknown until timeout.
  - **Recommended fix:** Replace this bespoke helper with `scripts/gh_helpers.sh:407-575` (`gh_retry`, `gh_api_json_to_file`) or make `gh_api_safe()` perform its own bounded retry loop before returning failure. Callers should branch on “request failed” separately from “request succeeded with empty JSON”.

### Section 2: GitHub API Call Redundancy Audit

Excluding the already-documented empty scheduled sweep cadence waste, these were the highest-value API call reductions.

- **API-001**
  - **File path:** `.github/workflows/issue_pr_status.yml:209-265,362-406`
  - **Severity:** Medium
  - **Category tag:** api-redundancy
  - **Description:** `issue_pr_status` already fetches `closingIssuesReferences.nodes { number body labels }` at `253-258`, and `classify_orchestrator_issues_from_payload()` at `209-234` already derives `TRACKING_ISSUES` and `MANAGED_ISSUES` from that payload. The workflow then builds `ORCH_ALIAS_FRAGMENT` and re-fetches `body` and `labels` for `LOOKUP_ISSUE_NUMBERS` at `362-373`, with an `N`-item REST fallback loop at `398-406`.
  - **Current call count:** `2` GraphQL calls on the common path, plus up to `N` REST issue reads if the second batch fails.
  - **Proposed call count:** `1` GraphQL call when `LOOKUP_ISSUE_NUMBERS` is a subset of `CLOSING_ISSUE_NUMBERS`; only batch-fetch the set difference when branch/body fallback adds extra issue numbers.
  - **Existing pattern to extend:** Keep `classify_orchestrator_issues_from_payload()` as the primary classifier, or reuse `scripts/orchestrate_poll_process.sh:10774-10850` (`_fetch_candidate_issue_details_graphql()`).
  - **Recommended fix:** Carry the first GraphQL payload forward as the authoritative classification source. Only construct `ORCH_ALIAS_FRAGMENT` for issue numbers introduced after the first call.

- **BATCH-001**
  - **File path:** `.github/workflows/review_autofix.yml:911-925,944-953`
  - **Severity:** Medium
  - **Category tag:** api-batching
  - **Description:** The post-merge validate-dispatch step first resolves linked issues in batch (`914-919`) and may also fetch PR text once (`923-925`), but when labels are unknown it then performs `gh issue view --json labels` once per issue inside the loop at `944-953`. That is an avoidable `N+1` fallback.
  - **Current call count:** `1` GraphQL call, optional `1` PR REST read, then `N` per-issue `gh issue view` calls when labels were not already supplied.
  - **Proposed call count:** `2` GraphQL calls maximum, `0` per-issue REST reads.
  - **Existing pattern to extend:** `.github/workflows/issue_pr_status.yml:362-373` alias batching, or `scripts/review_collect_pr_metadata.sh:70-171` (`_fetch_linked_issue_bodies_graphql()`).
  - **Recommended fix:** When `POST_MERGE_LINKED_ISSUES_JSON` lacks labels, batch-hydrate all linked issue numbers once via an alias GraphQL query and pass `{number, labels}` into the loop.

- **API-002**
  - **File path:** `scripts/orchestrate_poll_process.sh:8551-8616,8741-8795`
  - **Severity:** Low
  - **Category tag:** api-redundancy
  - **Description:** `_load_actions_runs_cached()` performs a conditional `in_progress` fetch at `8555-8566`, then unconditionally fetches `queued` at `8608-8610` and `completed` at `8613-8616` on every cache miss before `prime_phase_concurrency_snapshot()` consumes the merged blob. This is three Actions REST calls to build one local cache blob. [NEEDS VERIFICATION]
  - **Current call count:** `3` REST calls per cache miss.
  - **Proposed call count:** `1-2` REST calls per cache miss, depending on whether the completed-window data must stay separate. [NEEDS VERIFICATION]
  - **Existing pattern to extend:** `_ACTIONS_RUNS_BLOB_CACHE` plus `prime_phase_concurrency_snapshot()`.
  - **Recommended fix:** Make the cold-path cache loader fetch a single broader snapshot first and only fetch a dedicated completed window lazily if stall-diagnostics actually need it.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001**
  - **File path:** `scripts/gh_helpers.sh:548-562`; duplicates in `.github/workflows/orchestrate.yml:1012-1027`, `.github/workflows/plan.yml:1999-2017`, `.github/workflows/implement.yml:4885-4898`, `scripts/check_failure_triage.sh:58-79`, `scripts/implement_diagnose_post_codex_failure.sh:49-62`
  - **Severity:** Low
  - **Category tag:** duplication
  - **Description:** `_safe_gh_jq` is already implemented canonically in `scripts/gh_helpers.sh`, but near-identical fallback copies are embedded in multiple workflows/scripts. They have already drifted: some emit the mktemp diagnostic, some suppress stderr entirely, and some omit the canonical error annotation.
  - **Recommended fix:** Introduce a tiny shared bootstrap such as `scripts/gh_bootstrap.sh` that guarantees `gh_retry`, `_safe_gh_jq`, and `gh_api_json_to_file` are defined, then replace the inline copies in the listed callers with a single `source` call.

- **DUP-002**
  - **File path:** `.github/workflows/issue_pr_status.yml:253-295`; `.github/workflows/review_autofix.yml:914-939,4814-4936,5111-5126`; `scripts/review_collect_pr_metadata.sh:237-304`; `scripts/review_rb_judge.sh:834-854`
  - **Severity:** Medium
  - **Category tag:** duplication
  - **Description:** Linked-issue resolution is reimplemented in at least four call chains: GraphQL `closingIssuesReferences`, then repo-scoped PR text fallback, then “no linked issues found” handling. The copies already diverge in caching, retry behavior, label hydration, and fail-open semantics.
  - **Recommended fix:** Move this into a dedicated shared module, e.g. `scripts/linked_issue_helpers.sh`, with functions such as `fetch_closing_issue_nodes <repo> <pr_number>`, `fallback_issue_numbers_from_pr_text <repo> <title_body>`, and `hydrate_issue_labels_graphql <repo> <numbers_json>`. Update `issue_pr_status.yml`, the post-merge/deterministic-skip/late-review paths in `review_autofix.yml`, `scripts/review_collect_pr_metadata.sh`, and `scripts/review_rb_judge.sh` to call that module and reuse `scripts/gh_helpers.sh:1509-1524`.

### Section 4: Expression Size Limit Risk Assessment

Repo-wide scan found no `if:` expressions near the 21,000-character ceiling (longest scanned `if:` was `163` characters) and no workflow file over `800 KB`; the largest workflow is `.github/workflows/review_autofix.yml` at `438,698` bytes. No scanned `run:` block exceeded the `18,000`-character High-risk threshold; the two blocks below exceeded the `15,000`-character Medium-risk threshold.

- **EXPR-001**
  - **File path:** `.github/workflows/plan.yml:998-1287`
  - **Severity:** Medium
  - **Category tag:** expression-limit
  - **Estimated current expression length:** `16,837` characters
  - **Remaining headroom:** `4,163` characters
  - **Description:** The `Run Codex planning` step keeps a large inline prompt heredoc in the `run:` body (`1003-1210`) alongside multiple `${{ }}` interpolations and retry/progress-update logic. It is already within ~20% of GitHub’s hard expression ceiling.
  - **Recommended fix:** Move the inline planning prompt into a checked-in prompt asset under `prompts/` (adjacent to `prompts/mode-plan.txt`) and render it via `scripts/render_prompt.sh`, leaving only small variable interpolation in workflow YAML.

- **EXPR-002**
  - **File path:** `.github/workflows/implement.yml:3675-3920`
  - **Severity:** Medium
  - **Category tag:** expression-limit
  - **Estimated current expression length:** `15,979` characters
  - **Remaining headroom:** `5,021` characters
  - **Description:** The `Destructive-commit guard — label + alert on rejection` step combines two large branches (scope violation and destructive deletion), label mutations, issue-comment assembly, and Telegram alert formatting in one interpolated `run:` block.
  - **Recommended fix:** Split the scope-block and destructive-delete handling into separate steps, or extract both branches into a new helper under `scripts/` so the workflow YAML only passes env/arguments.

### Section 5: Cross-Cutting Concerns

- **DEBT-001**
  - **File path:** `scripts/orchestrate_poll_process.sh:1-18436`
  - **Severity:** Low
  - **Category tag:** tech-debt
  - **Description:** `orchestrate_poll_process.sh` is now an audit and change-management hotspot: `18,436` lines and `931,029` bytes, with unrelated contracts (actions-runs cache, GraphQL batching, wave reconciliation, stall recovery, linked-PR handling) co-located in one file. That size materially raises review risk and makes targeted testing/reuse harder.
  - **Recommended fix:** Split the file into domain modules (for example, `scripts/orchestrate/actions_runs_cache.sh`, `scripts/orchestrate/linked_pr_graphql.sh`, `scripts/orchestrate/stall_recovery.sh`, `scripts/orchestrate/tracking_sync.sh`) and keep `orchestrate_poll_process.sh` as a thin coordinator that sources them.

Static sweep notes:
- `scripts/check_workflow_script_refs.py` passed (`All workflow script references resolve to existing files.`).
- `python -m py_compile scripts/*.py` passed.
- No `TODO`/`FIXME`/`HACK`/`XXX` markers were found under `.github/workflows` or `scripts`.
- I did not substantiate additional dead-code or shellcheck-only defects worth separate findings beyond the retry/helper drift issues above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 6 | BUG-002, API-001, BATCH-001, DUP-002, EXPR-001, EXPR-002 |
| Low | 3 | API-002, DUP-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1-3 | Medium |
| API call optimization | 3-4 | Medium |
| Code modularization | 8-10 | Large |
| Expression size reduction | 2-4 | Medium |
| Medium/Low fixes | 3-5 | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-08-07)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is statically proven safe to consolidate without changing filters, retries, failure behavior, or concurrency boundaries. `NEEDS_VERIFICATION` means the overlap is real, but payload completeness, freshness, or fail-open/fail-closed semantics still need human proof. `RISKY_SKIP` means the overlap sits in retry/race-defensive code, so this pass records it for manual review only and does **not** authorize automation.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — NEEDS_VERIFICATION**
  - **File paths:** `scripts/review_enable_auto_merge.sh:64-75`, `scripts/review_enable_auto_merge.sh:127-140`
  - **Current call count:** 2 logical calls on the common path (1 paginated label snapshot + 1 PR metadata read).
  - **Proposed call count:** 1 logical call if `/pulls/{n}` is verified to carry the complete label set needed by the `e2e-smoke-test` guard.
  - **Endpoints:** `GET /repos/{owner}/{repo}/issues/{issue_number}/labels?per_page=100`; `GET /repos/{owner}/{repo}/pulls/{pull_number}`
  - **Evidence:**
    ```sh
    if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' ... )"; then
      if printf '%s\n' "${PR_LABELS_RAW}" | grep -qx 'e2e-smoke-test'; then
        exit 0
      fi
    fi
    ...
    if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" ... )"; then
      exit 0
    fi
    _orch_pr_head_ref="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.head.ref // ""' ...)"
    _orch_pr_body="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.body // ""' ...)"
    ```
  - **Proposed fix:** Verify whether `GET /pulls/{PR_NUMBER}` always exposes the full PR label set; if it does, extend the existing `_ORCH_PR_META_JSON` parse to read `.labels[].name`, keep the current fail-closed warning behavior, and delete the dedicated paginated labels call.
  - **Safety rationale:** `NEEDS_VERIFICATION` because the current code explicitly defends against labels beyond page 1, and static reading cannot prove that `/pulls/{n}` preserves identical label completeness or fail-closed semantics.
  - **Downstream signal:** Verify with a PR carrying more than 30 labels—including `e2e-smoke-test` past the first page—that `.labels` from `GET /pulls/{n}` matches the paginated `/issues/{pr}/labels` result and that the merged failure path still suppresses auto-merge on fetch failure.

- **MERGE-002 — RISKY_SKIP**
  - **File paths:** `.github/workflows/test-and-mark-stable.yml:1061-1069`, `.github/workflows/test-and-mark-stable.yml:1091-1096`
  - **Current call count:** 3 logical `GET /pulls/{n}` reads on a stability attempt that succeeds immediately (2 SHA probes + 1 post-loop metadata read), with more reads on later stability attempts.
  - **Proposed call count:** 2 logical reads on the winning attempt by carrying full PR JSON from the successful second probe into the closed/merged guard.
  - **Endpoints:** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
  - **Evidence:**
    ```sh
    for stability_attempt in 1 2 3 4 5; do
      HEAD_A=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' ...)
      sleep 3
      HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' ...)
      ...
    done

    PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" ...)
    PR_STATE=$(printf '%s' "${PR_META}" | jq -r '.state // ""' ...)
    PR_MERGED=$(printf '%s' "${PR_META}" | jq -r '.merged // false' ...)
    ```
  - **Proposed fix:** If manually approved, make the `HEAD_B` read capture full PR JSON, derive both `head.sha` and `{state, merged, merged_at, closed_at}` from that payload, and drop the third `GET /pulls/{n}` while keeping the two-read stability probe intact.
  - **Safety rationale:** `RISKY_SKIP` because the overlap is inside a stability/retry loop that explicitly defends against GitHub indexing races.
  - **Downstream signal:** Do **not** auto-implement; manual review must prove that reusing the successful second probe preserves the ≥3s stability check and does not hide a PR-close race that the third read currently catches.

### Redundant Re-Fetch (REUSE-###)

- **REUSE-001 — NEEDS_VERIFICATION**
  - **File paths:** `.github/workflows/check_failure_triage.yml:118-147`, `scripts/check_failure_triage.sh:151-178`
  - **Current call count:** 2 logical full-payload `GET /pulls/{n}` reads on the same-repo common path, excluding retries.
  - **Proposed call count:** 1 logical full-payload read on the common path, with the existing script fetch retained only as a cache-miss/invalid-cache fallback.
  - **Endpoints:** `GET /repos/{owner}/{repo}/pulls/{pull_number}` at both call sites
  - **Evidence:**
    ```sh
    # workflow guard
    if gh api "repos/${GITHUB_REPOSITORY}/pulls/${{ inputs.pr_number }}" > "${pr_payload}" ...; then
      ...
    fi
    HEAD_REPO="$(jq -r '.head.repo.full_name // ""' "${pr_payload}" ...)"

    # script startup
    if gh_api_json_to_file "${PR_JSON_FILE}" gh api "repos/${REPO}/pulls/${PR_NUMBER}"; then
      PR_JSON="$(cat "${PR_JSON_FILE}")"
    fi
    PR_STATE="$(printf '%s' "${PR_JSON}" | jq -r '.state // ""')"
    HEAD_REPO_FULL_NAME="$(printf '%s' "${PR_JSON}" | jq -r '.head.repo.full_name // ""')"
    ```
  - **Proposed fix:** Create/persist the runtime PR payload before `pr_origin_guard` exits (for example under `${RUNTIME_DIR}/pr_payload.json` or `${RUNNER_TEMP}`), pass that file path into `scripts/check_failure_triage.sh`, and have the script seed `PR_JSON` from the cached file before falling back to `gh_api_json_to_file`.
  - **Safety rationale:** `NEEDS_VERIFICATION` because the second read currently doubles as a freshness check for `.state`, and static review cannot prove that a stale payload is acceptable after the intervening setup steps.
  - **Downstream signal:** Verify whether a PR closing between `pr_origin_guard` and `Run check-failure triage` must suppress issue creation; if yes, reuse only cached non-volatile fields or add an explicit freshness strategy before deleting the second full `/pulls/{n}` fetch.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — the first GraphQL payload already covers `body`/`labels` for the closing-issue subset, but the implementation must preserve the branch/body-fallback set-difference logic.
- BATCH-001: NEEDS_VERIFICATION — batching linked-issue label hydration is directionally correct, but the merged-PR validate-dispatch path must keep its current fail-open fallback behavior.
- API-002: RISKY_SKIP — this lives in `scripts/orchestrate_poll_process.sh`’s actions-run cache / race-defense path, which this pass does not authorize for automatic consolidation.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | MERGE-001, REUSE-001 |
| RISKY_SKIP | 1 | MERGE-002 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
