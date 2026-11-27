## Executive Summary

- **Data gaps first.** The window is incomplete: `coverage.repositories_truncated` lists **shubhodeep1/coding-workflows**, the page limit was reached, and only part of its runs (999 rows, 2026-11-23 11:35–19:25 UTC) are listed. The full-log directory `/home/runner/work/_temp/workflow-log-output` was not mounted, so there are no `errors/`, `slow/` or `recent/` deep dives. Evidence comes from `analysis_context.json` rows, `log_summary` fields and the current repository code. Confidence on root causes is therefore medium at best.
- **Runner saturation dominates latency (high confidence).** In 26 sampled `log_summary` runs, the median runner-queue wait was about 262 s and the maximum about 23 min. Some runs did seconds of work after long waits: run 45239455419 queued 848 s of its 867 s, run 45247568254 waited 6.5 min and then ran 6 s. Two changes are proposed:
  - Run the Claude pool's per-account probes in parallel (**Speed 1**). This removes about 19 s from every Claude-engine job.
  - Have the collector record per-job queue time (**Speed 2**, reopened).
- **Claude-engine spend is invisible to this analysis (high confidence, code-verified).** `scripts/cost_audit.py` parses `claude_*` usage, but `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS` drop it. Only 4 of 15 telemetry-bearing review runs show any token usage. The fix is additive (**Cost 1**).
- **Failure diagnosis is blind (high confidence, code-verified).** `scripts/analyze_workflow_logs.py::_normalized_run_view` never forwards the collector's `root_failure_class`. As a result:
  - The two ~3 h resolver failures on PR 9709 (runs 45180800682 and 45203595709) arrive without a reason.
  - The 6-run `review / gate` `pre_runner_failure` burst at 14:12–14:13 (runs 45191667201 to 45191815022, each about 67–72 min) has no annotation evidence.

  Fixes: **Reliability 1/2** and **Cost 2**.
- **`check_failure_triage.yml` emits a post-job `##[error]` on every Claude-engine run (high confidence, code-verified).** Run 45243815088 shows it. The staging step deletes `install-claude/action.yml` before that action's post step runs. A one-line fix is proposed (**Reliability 3**).
- **Merge-lease and comment-flood pressure (medium confidence).**
  - `MERGE_LEASE_QUEUE base=main holder=9935 held_s=5194 waiters=22` (run 45237889619).
  - 346 skipped `issue_comment` wrapper runs in about 7.8 h, roughly 1,060 a day against the about 55 a day the run-volume plan's D16 projected.

  Two log-only additions are proposed (**Orchestrator Health 1**, **Pipeline Flow 1**).

## Speed Optimizations

**1. Probe Claude pool accounts in parallel (critical path of every Claude-engine job)**
- Evidence: run 45237889651 (Issue-PR Status Sync, `Resolve Claude credential`) logged `CLAUDE_POOL available=true reason=selected accounts=13`. The probe took about 27 s (18:49:32 to 18:49:59). The same action runs in `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate.yml`, `review_autofix.yml`, `validate.yml`, `issue_pr_status.yml`, `check_failure_triage.yml` and others.
- Root cause: in `scripts/claude_pool_token.sh`, lines 254-272 run one `timeout ... claude -p --model "${probe_model}" "Reply OK"` per account, one after another.
- Change:
  - Add `CLAUDE_POOL_PROBE_PARALLELISM` (default `4`, per §8; the name is unused in the repo).
  - Run each account's subshell in the background, writing to its existing per-account `probe-${name}.jsonl` transcript, with a bounded `wait -n` gate.
  - After all probes finish, parse the transcripts in the original `accounts[@]` order. This keeps the `CLAUDE_POOL probe` lines and the `probes` JSON byte-identical in order, so `engine_py choose` is unaffected.
- Estimated saving: about 27 s down to about 8 s, roughly 19 s per Claude-engine job. Inference: at about 100 Claude jobs per 8 h, that is about 30 runner-minutes per 8 h returned to a saturated pool.
- Risk: low to medium. Each probe uses a different account token, so there is no shared quota race. Keep the per-probe `timeout` unchanged, and keep parallelism at 1 as the rollback value.
- Finding check: new — no prior verdict on pool-probe concurrency in `analysis/recommendation-processing-report.md`; serial loop present at `scripts/claude_pool_token.sh:254-272`.

**2. Record per-job runner-queue seconds in the collector (diagnostic; enables §26 fold/split decisions)**
- Evidence: queue wait was most of the wall time in sampled runs:

  | Run | Workflow | Queue wait | Total |
  |---|---|---|---|
  | 45239455419 | Auto release stable | about 848 s | 867 s |
  | 45238956327 | cancel_on_pr_close | about 16 min | 1,008 s |
  | 45239119456 | review sweep | about 16.5 min | 1,064 s |
  | 45237889651 | Issue-PR Status Sync (`activation-verify`) | about 23 min | 1,636 s |
  | 45238582426 | Orchestrate Poller | about 20 min | 1,902 s |
  | 45234826440 | PR checks | about 8 m 47 s | 539 s |
  | 45241725945 | heal recovery-candidates | about 4 min | 8 s of work |

  The only source for this today is the sampled `log_summary` text.
- Root cause: `extract_failure_point` and the run rows in `scripts/collect_workflow_logs.py` carry no queue field. The jobs payload the collector already fetches (`jobs_fetch_status`) contains `created_at` and `started_at`.
- Change: add an additive `queue_seconds_max` field (max of `started_at - created_at` across jobs) to run rows whose jobs payload is already fetched, and forward it in `_normalized_run_view`. This adds zero API calls.
- Estimated saving: no direct saving. It turns the queue share into a measured number for each run family, which §26 requires before folding or splitting jobs.
- Risk: low; additive JSON field.
- Finding check: reopened — prior verdicts rejected runner-queue logging ("saving of 0s", 2026-10-03 doc row at line 1853) and queue-duration fields ("telemetry with no consumer", line 1236). This window shows queue time as most of the wall time in more than 10 sampled runs, and the consumer is the §26 decision the policy now requires.

## Cost Optimizations

**1. Project Claude-engine usage into run-level and aggregate telemetry**
- Evidence:
  - Engine selection: `AI_ENGINE_SELECTED role=WORKFLOW_HEAL engine=claude model=claude-opus-5-5` (runs 45248006641 and 45243815042), and `CLAUDE_POOL ... accounts=13` (run 45237889651).
  - Coverage gap: review_autofix has 15 runs with log telemetry, but only 4 report usage (`or_calls=8` each: 45183178692, 45182695072, 45195132609, 45180964481). Every row's `cost_telemetry` lacks `claude_calls`, `claude_input_tokens` and `claude_cost_usd`.
- Root cause: `scripts/cost_audit.py:209` `RUN_COST_TELEMETRY_FIELDS` and `:246` `AGGREGATABLE_COST_FIELDS` omit `CLAUDE_COUNT_KEYS` (`:128`). `build_run_cost_telemetry` (`:609-613`) copies only those fields, so `collect_workflow_logs.py:1697` drops the Claude numbers.
- Change (additive):

```python
RUN_COST_TELEMETRY_FIELDS = (
	...,  # existing entries unchanged
	*CLAUDE_COUNT_KEYS,
	"claude_cost_usd",
)
AGGREGATABLE_COST_FIELDS = (
	...,  # existing entries unchanged
	*CLAUDE_COUNT_KEYS,
)
# aggregate_run_cost_telemetry: sum claude_cost_usd as float separately (_to_int would truncate it).
```

- Estimated saving: indirect. This exposes the dominant spend source for heal, triage, security audit, issue status and (inference) 11 of 15 review runs, which is a prerequisite for any model or effort right-sizing.
- Quality risk: none; reporting only.
- Finding check: new — no prior verdict on projecting Claude usage; omission present at `scripts/cost_audit.py:209-293`.

**2. Expose the resolver terminal class for PR 9709's repeat failures**
- Evidence: `Run Codex resolver, validate, stage, commit` failed twice on PR 9709:
  - run 45180800682: 11,085 s
  - run 45203595709: 9,828 s

  That is about 5.8 h of runner time with no merge. Both runs show an identical Semble query (`semble_query_bytes=9541`, `sources=6`), which suggests the same conflict set (inference). The step allows 3 × 50-minute attempts within `timeout-minutes: 170` (`.github/workflows/review_autofix.yml:4450-4471`). The gate's identical-failure cap defaults to 3 runs (`REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`, per the comment at `scripts/review_conflict_resolve.sh:2763-2764`), so a third run of up to about 3 h is still allowed.
- Root cause: `RESOLVER_TERMINAL` (attempts, per-attempt ms, exit class) is emitted but never reaches the analysis view. Policy cannot be tuned without it.
- Change: in `scripts/collect_workflow_logs.py`, next to `_root_failure_class_from_full_logs`, parse the last `RESOLVER_TERMINAL` line's exit class (enum token, same `ROOT_FAILURE_CLASS_RE` validation) into an additive `resolver_exit_class` field, and forward it with Reliability 1. Do not change caps until two windows show the class.
- Estimated saving: up to about 3 h runner time and one full model pass per capped PR, once a timeout-class cap is justified by data (inference).
- Quality risk: none for the diagnostic.
- Finding check: recurring — prior "Reliability 1 (classify resolver terminal failures)" was `applied` (2026-10-07-2, `RESOLVER_TERMINAL`), and this window still shows two unattributed resolver failures on one PR.

**Semble / Serena assessment (no separate action):**
- Semble: 11 queries returned 107,298 bytes (about 9.8 KB per query) and 55 sources, with `static_dup_bytes=0` and 0 echo lines. That is bounded, non-redundant context. 11 bootstraps took 578,854 ms (about 52.6 s each), with 0 failed and 0 unused. Against a 2 h review p50, bootstrap is under 1%.
- Serena: 0 queries, 0 probes. It is not active, so it is neither replacing nor adding work.

## Reliability Improvements

**1. Forward `root_failure_class` into the analysis run view**
- Failure evidence: failing rows 45180800682 and 45203595709 have `log_download_status=success`, but no `root_failure_class` or `finalize_reason`. The full-log directory was also absent this run, so the analysis cannot see why the 8 review failures happened.
- Root cause category: observability plumbing. The collector sets `run["root_failure_class"]` (`scripts/collect_workflow_logs.py:1753-1761`), but `scripts/analyze_workflow_logs.py:222-249` `_normalized_run_view` copies only a fixed field list.
- Fix:

```python
	root_failure_class = run.get("root_failure_class")
	if isinstance(root_failure_class, str) and root_failure_class.strip():
		view["root_failure_class"] = root_failure_class[:RUN_DIAGNOSTIC_FIELD_MAX_CHARS]
```

- Expected impact: every failed or cancelled review run with downloaded logs gets a structured cause even when the full-log artifact is missing, as in this window.
- Rollback: additive key; fails open when absent.
- Finding check: recurring — prior "Reliability 5 (structured `root_failure_class`)" was `applied` (PR #7725, report line 1372), but this window's failing rows still lack it because the analyzer drops the field.

**2. Capture the annotation for `pre_runner_failure` jobs**
- Failure evidence: 6 of the 8 review_autofix failures are `review / gate` `pre_runner_failure` with `logs: empty_log_archive`. They are runs 45191667201, 45191692066, 45191786637, 45191808370, 45191808583 and 45191815022, all created 14:12:15–14:13:05, each lasting 4,023–4,315 s. That is 75% of review failures and 46% of all 13 failures. The `gate` job (`review_autofix.yml:338`) sets no `timeout-minutes`, so the ~70 min failures were not a job timeout. The cause is unknown. Inference: a hosted-runner acquisition failure during the 14:02–14:13 dispatch burst (8 or more review runs and CI run 45191389997 created then).
- Root cause category: unknown, with diagnostics missing. `scripts/collect_workflow_logs.py:82` sets `WORKFLOW_VALIDATION_ANNOTATIONS_STATUS = "unavailable_from_existing_payloads"`.
- Fix: only for jobs classified `pre_runner_failure`, fetch that job's check-run annotations (one call per such job; 6 here) and store a bounded, escaped `pre_runner_annotation` string, capped like `diagnostic_failure_reason` at 512 chars. Fail open on any error.
- Expected impact: separates runner-acquisition loss, startup failure and concurrency eviction, which decides whether a rerun policy or a dispatch change is needed.
- Rollback: gate it behind an env default `true`; failure leaves the field unset.
- Finding check: recurring — prior "Reliability 2 (pre-runner failures)" was `applied in part` (classification; report line 1236). This window still shows 6 unexplained pre-runner failures. The annotation fetch was not among the rejected timestamp, runner and queue fields.

**3. Keep `install-claude` alive for its post step in `check_failure_triage.yml`**
- Failure evidence: run 45243815088 (Check Failure Triage, success) logged `##[error]Can't find 'action.yml'... under .codex-workflow-src/.github/actions/install-claude` at post-job.
- Root cause category: lifecycle bug. Line 439 uses `./.codex-workflow-src/.github/actions/install-claude`, which wraps `actions/cache@v5` and `actions/setup-node@v6`, both of which have post steps. The staging step at lines 616-620 keeps only `claude-pool-token` and deletes the rest of `.github/actions`. No other workflow uses this pruning (search for `! -name claude-pool-token`: 1 hit).
- Fix (`.github/workflows/check_failure_triage.yml:620`, plus the comment at 613-615):

```bash
find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +
```

- Expected impact: removes a false `##[error]` from every Claude-engine triage run, which reduces heal-intake and annotation noise, and lets the npm cache post-save run on a cache miss.
- Rollback: the same comment says the sandbox snapshot never includes `.codex-workflow-src`, so keeping one more action directory exposes nothing new.
- Finding check: new — no prior verdict on this post-step error (the post-job items at report lines 2050 and 2173 concern `actions/checkout` cleanup); the bug is present at lines 616-620.

**4. Remove the expected-404 warning from the promote-candidate follower**
- Failure evidence: runs 45247568254, 45237888456 and 45237461070 each emit `GH_API_RETRY outcome=permanent ... status=404 ... endpoint=repos/shubhodeep1/coding-workflows/git/ref/tags/promote-cycle-frozen` plus `##[warning]  stderr: gh: Not Found (HTTP 404)`, although tag-absent is the normal between-cycles state.
- Root cause category: warning noise. `scripts/promote_candidate_follow.sh:168` reads `git/ref/tags/${FOLLOW_TAG}` through `gh_api_retry`, which annotates every final non-2xx as a warning (`scripts/gh_helpers.sh` lines around 2204-2319).
- Fix: read `repos/${REPO}/git/matching-refs/tags/${FOLLOW_TAG}` instead. It returns `200 []` when the tag is absent. Select the element whose `.ref == "refs/tags/promote-cycle-frozen"` exactly, keeping the call count at 1 (the §14 header comment stays accurate).
- Expected impact: about 24 fewer warning annotations a day (hourly schedule plus pushes), and cleaner heal and alert signal.
- Rollback: revert one call; outcome semantics are unchanged.
- Finding check: new — no prior verdict mentions `promote-cycle-frozen`; the lookup is present at `scripts/promote_candidate_follow.sh:168`.

**5. Bump `actions/cache/{restore,save}@v4` to `@v5`**
- Failure evidence: run 45236305889 (Orchestrate Poller) logged a "Node.js 20 is deprecated" warning for `actions/cache/restore@v4`. There are 14 `@v4` pins across `orchestrate_poll.yml` (7), `review_autofix.yml` (3), `clarify.yml` (2) and `orchestrate_clarify_respond.yml` (2). `.github/actions/install-claude/action.yml:32` already uses `actions/cache@v5` on the same runners. Observed runner version: 2.338.2.
- Root cause category: dependency deprecation.
- Fix: replace the 14 pins with `@v5`, keeping inputs unchanged.
- Expected impact: removes the forced-Node-24 warning and the risk of a hard break when Node 20 actions stop being forced.
- Rollback: revert the pins.
- Finding check: new — prior "cache-action refresh" was deferred ("directionally valid but changes runtime dependencies", report line 605), and the `@v4` pins are still present.

Not raised:
- The 12 `SEMBLE_FALLBACK` events are all contract-test fallbacks (CI runs 45173107888, 45213036523 and 45232457541, 4 each; runtime 0). That split is already implemented. This is healthy fail-open behaviour, not a masked rollout.
- `BREAK_GLASS` = 0 and `CONTEXT_BUDGET_WARN` = 0 across 119 telemetry runs, so there is no rubric or prompt-size pressure signal.
- The 4 CI failures each hit a different PR-scoped test step: `Inventory parity` (PR 9981), `Orchestrate poll process unit tests` (PR 9832), `Test-and-mark-stable review-blocked budget contract tests` (PR 9637), and `Validation self-test unit tests` (CI quick). This pattern looks like regressions caught on PRs, not systemic failure.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines are visible in any `log_summary`. Several summaries say the middle of the log was truncated (for example runs 45246471344, 45245914759 and 45238582426), and the full-log directory was not mounted. So retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` and `push_attempts` cannot be computed for this window.

This is a collection gap, not evidence of non-emission. Next collection step: confirm the `workflow-log-output` artifact is mounted on the next run, then grep `recent/` and `slow/` for `AI_MEMORY_TELEMETRY:`. No recommendation is raised: "verify memory emission" proposals were rejected as instrumentation-only (2026-10-07-2 and -3, report lines 2174 and 2209), and this window adds no new defect evidence.

## GH API Call Audit

**1. Feed the existing hourly `GH_PAT` budget table into the analysis input**
- Evidence: the review sweep reported `GH_PAT_BUDGET ... remaining=2609 reset=1795461533 used_in_job=13` at about 18:48 (run 45239119456), then `remaining=1204 ... used_in_job=17` at about 19:02 (run 45244352944). The reset epoch 1795461533 is 19:18:53 UTC. That is about 1,400 core calls in about 14 min (about 100 per minute) from consumers other than the sweep. Inference: at that rate the PAT would run out before reset.
- Root cause: per-job deltas exist (`GH_PAT_BUDGET` in `scripts/gh_helpers.sh:1013`), and `build_pat_budget_report` (`scripts/collect_workflow_logs.py:1784`) ranks consumers by hour. However, `workflow-log-analysis.yml:232-362` writes that table only to the step summary and the `gh-pat-budget-day` artifact. The analysis prompt never sees the hotspot ranking.
- Change: append `${RUNNER_TEMP}/gh-pat-budget.md`, already produced, to the analysis context the prompt-assembly step builds. Add no new API calls.
- Expected impact: names the top PAT consumers each report, so batching work can target the actual hotspot instead of guesses.
- Rate-limit risk reduction: indirect.
- Finding check: new — prior verdicts rejected building new GH_API_AUDIT rollups (report line 2209); this item adds no telemetry and only routes an existing artifact.

Other observations:
- Run 45245534901 (PR checks) showed `GH_LOOP_CALL_LINT warnings=0`.
- Merge-train releases report `file_list_fetches=0 active_listing_calls=0` (runs 45238956327 and 45237889619).
- No HTTP 429 or secondary-rate-limit events appear in any visible summary.
- No new per-item loop patterns are evidenced in this window.

## Prompt Cache & Memory System

- Aggregate `cache_hit_rate` was **0.78671**, up from 0.734551 in `analysis/workflow-optimization-2026-11-18.md`, over 32 OpenRouter calls (32 usage available, 0 unavailable). Cache reads were 19,417,274 tokens and cache writes 1,336,424.
- Per run, hit rates were 0.614 (45180964481), 0.688 (45195132609), 0.764 (45182695072) and 0.883 (45183178692).
- Run 45183178692 alone used 11,692,782 tokens: 47% of the window total and 55% of completion tokens (147,125).
- Prefix stability: `REVIEWER_PROMPT_COMPONENTS ... static_prefix_sha256` exists (report line 2209). Without deep-dive logs, fragmentation by run cannot be attributed, and there is no `CONTEXT_BUDGET_WARN` pressure (0).
- Cache coverage is partial: 11 of 15 telemetry-bearing review runs show zero OpenRouter usage. Inference: Claude engine, whose cache read/write tokens are parsed but dropped.

**1. Make Claude prompt-cache reads and writes visible (same change as Cost 1)**
- Evidence and change: see Cost 1. Once forwarded, `claude_cache_read_tokens` and `claude_cache_write_tokens` give the cache hit rate for the Claude-engine half of the pipeline, which is currently unmeasured.
- Finding check: new — same evidence as Cost 1 (`scripts/cost_audit.py:209-293`); counted once in the Findings check table.

## Orchestrator Health

- Clarify, plan and implement flow worked. Issue #10009 went clarify (run 45244343535, 619 s), then auto-answer, then plan (run 45245914759, 628 s) within about 20 min, and issue #10010 was clarified (run 45246471344).
- One `orchestrate / orchestrate` failure at `Run Codex (decomposer)` (run 45246760533, 424 s). Its logs were not readable and it is a single event, so no recommendation is raised.
- Poller: 29 runs, 18 success, 11 cancelled. The cancellations look like the expected concurrency supersession of queued ticks (run 45240568190: `cancelled_before_first_step` after 709 s queued). The poll step ran 11–15 min, with about 1.1M log chars per run (runs 45236305889, 45238582426 and 45242605981). `ORCH_POLL_CYCLE_SUMMARY` already covers per-tick timing.
- `SEMBLE_AVAILABLE=false` in the poller is the lazy-bootstrap default and was previously decided, so it is not raised.

**1. Add holder progress to `MERGE_LEASE_QUEUE`**
- Evidence: run 45237889619 logged `MERGE_LEASE pr=9935 ... outcome=held reason=holder_open holders=2 hold_s=5193 waiters=22` and `MERGE_LEASE_QUEUE base=main holder=9935 held_s=5194 waiters=22`. A second holder, PR 9838, has `hold_s=2311`. PR 9502 was deferred with `FINAL_CI_MERGE_RESUME outcome=deferred reason=lease_wait` and `MERGE_BASE_FRESHNESS ... behind_by=24` (runs 45237889619 and 45238956327). The lease TTL defaults to 240 min (`scripts/pr_checks_lib.sh:2808`), so a stalled holder can block 22 PRs for up to 4 h.
- Root cause: the line at `scripts/review_merge_train.sh:1712` reports only holder, age and waiter count. The tick already computes holder idleness and CI activity (`tm_since`, `tm_idle_s` and `tm_ci_active`, declared at line 2093) but does not log them.
- Change: append `holder_idle_s=` and `holder_ci_active=` to the `MERGE_LEASE_QUEUE` line from those existing locals, emitting `-` when unset. This adds no API calls and is an additive log field.
- Observable indicators: `held_s` p95, `waiters` max, and the share of ticks with `holder_ci_active=false` combined with large `holder_idle_s` (stalled holder).
- Finding check: new — no prior verdict on merge-lease telemetry (no `lease` match in the processing report); the line format is present at `scripts/review_merge_train.sh:1676-1712`.

## Pipeline Flow Bottlenecks

Stage map for this window (wall time includes runner queue):

| Stage | p50 | p95 | Notes |
|---|---|---|---|
| Clarify (real runs) | about 620–695 s | – | Runs 45244343535 and 45246471344; 170–274 s of that was queue |
| Plan | about 628 s | 966 s (family) | – |
| Implement | 2 s (mostly skips) | 1,078 s | One 7,616 s outlier: run 45170678716 |
| Review/autofix | 7,113 s | 12,118 s | Dominant compute; resolver failures about 3 h each |
| CI | 1,255 s | 5,828 s | – |
| Merge | lease-bound | – | `waiters=22` |

Overhead split (inference from summaries):
- **Queueing:** median about 262 s, max about 23 min (Speed 1 and 2).
- **Compute:** review_autofix holds runners about 2 h per run.
- **Retry:** resolver repeats (Cost 2).
- **Merge/conflict:** one lease holder for 85 min or more (Orchestrator Health 1).

**1. Log when comment signing is inactive, so the skipped-run flood can be attributed**
- Evidence:
  - Skipped `issue_comment` wrapper runs in about 7.8 h: clarify 91, plan 83, implement 84, clarify-respond 88. That is 346 runs, about 87 comments that started nothing, or roughly 1,060 skipped runs a day.
  - `docs/plans/reduce-actions-run-volume-plan.md` D16 projected about 55 runs a day after signed job-token comments (baseline about 2,500).
  - Skipped runs take no runner, but under §26 they still flood the queue and the API.
- Root cause: `scripts/gh_helpers.sh:51` `_ai_comment_sig_activate` returns silently when `AI_COMMENT_SIGNING_SECRET` is unset. `AI_COMMENT_SIG outcome=` lines only appear when signing is active. As a result, "secret not configured" cannot be told apart from "comment kind not covered".
- Change: on that early return, emit one line per job, guarded by a `GITHUB_ENV` flag so later steps don't repeat it:

  ```text
  AI_COMMENT_SIG outcome=disabled reason=secret_unset
  ```

  The secret value is never printed, and behaviour is unchanged.
- Expected impact: one window of data shows whether D16 is off or leaking. Fixing either cuts about 1,000 skipped runs a day.
- Finding check: new — prior "control-plane dispatch / no-op reduction" was deferred (report line 600); the silent return is present at `scripts/gh_helpers.sh:51`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Hosted-runner saturation: queue waits up to about 23 min, median about 262 s, across nearly every sampled family.
  - Review/autofix wall time: p50 7,113 s, p95 12,118 s.
  - Merge lease on `main`: 22 waiters, holder held 5,194 s.
- **Top failure modes:**
  - 6 `review / gate` `pre_runner_failure` runs in one 2-minute burst, cause unknown.
  - 2 resolver failures on PR 9709 (about 5.8 h total).
  - 4 PR-scoped CI test failures.
  - 1 orchestrate decomposer failure.
  - A false post-job `##[error]` in check triage.
- **Highest-cost drivers:**
  - Review run 45183178692 used 11.69M tokens (47% of observed usage).
  - Repeated resolver runs.
  - Claude-engine spend is unmeasured (Cost 1).
- **Top 3 prioritized actions:**
  1. Forward `root_failure_class`, plus `resolver_exit_class`, into the analysis view (Reliability 1 and Cost 2).
     Finding check: recurring — prior `applied` (report line 1372 and 2026-10-07-2 `RESOLVER_TERMINAL`); this window's failing rows still lack a cause.
  2. Project `claude_*` usage into run telemetry (Cost 1).
     Finding check: new — omission at `scripts/cost_audit.py:209-293`, no prior verdict.
  3. Parallelize Claude pool probes (Speed 1).
     Finding check: new — serial loop at `scripts/claude_pool_token.sh:254-272`, no prior verdict.

## Metrics Appendix

**Run outcomes (window 2026-11-23 about 11:35–19:25 UTC, truncated listing)**

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Repo total | 999 | 386 | 13 | 22 | 578 | 1.30% (3.1% of 421 non-skipped) | 8 | 7,730 |
| review_autofix | 143 | 134 | 8 | 1 | 0 | 5.59% | 7,113 | 12,118 |
| ci | 75 | 63 | 4 | 8 | 0 | 5.33% | 1,255 | 5,828 |
| orchestrate_poll | 29 | 18 | 0 | 11 | 0 | 0% | 1,303 | 2,918 |
| orchestrate | 1 | 0 | 1 | 0 | 0 | 100% | 424 | 424 |
| security_audit | 19 | 19 | 0 | 0 | 0 | 0% | 1,119 | 2,466 |
| pr_checks | 47 | 47 | 0 | 0 | 0 | 0% | 500 | 1,883 |
| clarify / plan / implement / clarify_respond | 94 / 91 / 90 / 91 | 3 / 8 / 6 / 2 | 0 | 0 / 0 / 0 / 1 | 91 / 83 / 84 / 88 | 0% | 1 / 1 / 2 / 1 | 11 / 967 / 1,078 / 10 |
| workflow_failure_heal / heal_intake | 93 / 56 | 1 / 24 | 0 | 0 | 92 / 32 | 0% | 1 / 10 | 11 / 1,213 |
| integration_pr_readiness / check_failure_triage | 76 / 36 | 0 / 4 | 0 | 0 | 76 / 32 | 0% | 1 / 2 | 10 / 1,017 |

**Failure points (13)**

| Count | Job / step | Runs |
|---|---|---|
| 6 | review / gate · pre_runner_failure (empty log archive) | 45191667201, 45191692066, 45191786637, 45191808370, 45191808583, 45191815022 |
| 2 | review / codex-agent · Run Codex resolver, validate, stage, commit (PR 9709) | 45180800682, 45203595709 |
| 4 | CI test steps | 45173107888, 45213036523, 45232457541, 45191808207 |
| 1 | orchestrate / orchestrate · Run Codex (decomposer) | 45246760533 |

**Token and cache telemetry (OpenRouter; 4 review runs with usage)**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 32 (32 / 0) |
| Prompt / completion tokens | 3,927,909 / 267,622 |
| Cache read / cache write tokens | 19,417,274 / 1,336,424 |
| Total tokens | 24,948,914 |
| `cache_hit_rate` | 0.78671 (per-run 0.6144–0.8834) |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `claude_*` | not projected (see Cost 1) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo) | 7,000 / 14,290,910 (118 samples) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix) | 12,142,000 / 15,525,060 (15 samples) |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**Runner queue samples (from `log_summary`, n=26)**

| Stat | Value |
|---|---|
| Median queue wait | about 262 s |
| Max | about 1,383 s (run 45237889651 `activation-verify`) |
| Runs where queue was at least 90% of duration | 45239455419, 45247568254, 45241725945, 45234826440 |

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` remaining (sweep) | 2,609 at about 18:48, then 1,204 at about 19:02 (reset 19:18:53 UTC) |
| Heal intake budget | 4,892 then 4,642 (different reset window) |
| 429 / secondary rate limit | 0 visible |
| `GH_API_RETRY` permanent 404 (expected tag-absent) | 3 runs |
| `GH_LOOP_CALL_LINT` | warnings=0 |

**Semble / Serena**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 11 / 107,298 (about 9.75 KB per query; review 10 / 102,361, poll 1 / 4,937) |
| `semble_sources_total` / `semble_static_dup_bytes_total` / `semble_echo_lines_dropped` | 55 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 11 / 0 / 0 / 578,854 (about 52.6 s each) |
| `semble_fallbacks` (contract-test / runtime) | 12 (12 / 0); CI runs 45173107888, 45213036523, 45232457541 |
| Serena query / fallback / response bytes / tool calls / ms | 0 / 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Findings check**

| Status | Count |
|---|---|
| new | 8 (Speed 1, Cost 1, Reliability 3, Reliability 4, Reliability 5, GH API 1, Orchestrator Health 1, Pipeline Flow 1) |
| recurring | 3 (Reliability 1, Reliability 2, Cost 2) |
| reopened | 1 (Speed 2) |
| unverified | 0 |
| dropped: already fixed on this ref | 3 (Semble contract-test vs runtime split in `scripts/cost_audit.py`; hourly PAT-budget aggregation already in `build_pat_budget_report`; wedged queued runs from 2026-09-13 already ignored by the `ZOMBIE_RUN_MAX_QUEUED_HOURS` guards) |
| dropped: previously decided | 3 (poller `SEMBLE_AVAILABLE=false`, rejected 2026-10-07-3; AI-memory emission checks, rejected 2026-10-07-2/-3; pacing review dispatch bursts, rejected 2026-10-07-2) |

**Data gaps**
- `repositories_truncated`: shubhodeep1/coding-workflows. Page limit reached, so only part of its runs are listed.
- The full-log directory `/home/runner/work/_temp/workflow-log-output` was not present, so there were no `errors/`, `slow/` or `recent/` deep dives. `log_summary` text is LLM-summarized and itself truncated in many runs.
- `analysis/recommendation-processing-report.md` was readable. Its newest processed doc is 2026-10-08, so October–November docs carry no verdicts yet.
- The `claude_*` usage fields and `root_failure_class` are dropped before analysis (Cost 1, Reliability 1).