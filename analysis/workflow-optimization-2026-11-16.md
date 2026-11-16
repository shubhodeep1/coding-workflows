# Workflow optimization report: shubhodeep1/coding-workflows, 2026-11-15 21:16Z to 2026-11-16 04:05Z

The deep-dive log folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted for this run. Everything below comes from `analysis_context.json` (run rows and the `log_summary` fields) plus a read of the workflow and script source in the repo. `insufficient_data` is `false`. However, the 1,000-run sample covers only about 6h49m, because 772 of the 1,000 runs (77%) are `skipped`/"other".

## Executive Summary

- **The pool of GitHub-hosted `ubuntu-24.04`/`ubuntu-latest` runners is saturated, and that is where most of the time goes.**
  - Impact: very high. Confidence: high.
  - The lint and readiness jobs have `timeout-minutes: 5` (`.github/workflows/lint-pr-body-auto-close.yml:47`, `integration-pr-readiness.yml:44`), yet they succeed after 10,173s p50 and 17,849s max (run 43775010315). A job timeout only counts execution time, so nearly all of those durations are waiting in the queue.
  - Four sweep `log_summary` entries quote "All GitHub-hosted runners with label [ubuntu-24.04] are busy". Run 43795529699 waited about 59 minutes and then executed for 57s.
- **CI fans out to roughly 39 jobs per run and has no `concurrency:` guard**, so superseded PR commits keep their jobs queued.
  - Impact: high (likely the largest runner consumer; inference). Confidence: medium.
  - Of 10 CI runs, all 10 were cancelled and none succeeded. For example, run 43764204116 was cancelled after 22,576s at `tests-heal-plan-and-validation` / `cancelled_before_first_step`.
  - Count: 24 `system-python-shard` + 5 `poll-group` + 3 `sandbox-family` + 7 single jobs (`ci.yml:2771, 3556, 3714`; no `concurrency` key anywhere in `ci.yml`).
- **The orchestrator poller is effectively stalled.**
  - Impact: high (wave progression blocked). Confidence: medium.
  - 37 of 38 poll runs were cancelled before their first step. The only success, 43763083431, finished around 23:57Z; after that, every tick from 00:01Z onward was superseded.
  - The `poll` job has no `timeout-minutes` (`orchestrate_poll.yml:95-102`; the only one in the file is `:2239`, on the daily-usage job), so GitHub's 360-minute default applies.
  - Inference: either a long active poll held `ai-orchestrate-poll-<repo>`, or the job sat waiting for a runner.
- **The AI pipeline did almost no work in this window, and token telemetry would not show Claude work anyway.**
  - Impact: observability. Confidence: high.
  - Implement had 0 successes in 159 runs, plan 1 in 163, clarify 4 in 171. Every token counter is 0 and `cache_hit_rate` is null.
  - `RUN_COST_TELEMETRY_FIELDS` (`scripts/cost_audit.py:209`) and `AGGREGATABLE_COST_FIELDS` (`:246`) leave out `CLAUDE_COUNT_KEYS` (`:128`) and `claude_cost_usd`, so Claude-engine usage never reaches this context.
- **`promote-candidate` probably lags `main`.**
  - Impact: medium (correctness drift). Confidence: medium.
  - 10 of 11 `Promote candidate follower` runs were cancelled (for example 43792011382 after 3,605s), and the one success, 43768568268, took 10,912s.
  - The internal wrappers (for example `internal-orchestrate-poll.yml:18`) call `@promote-candidate`. Inference: pipeline runs may be using stale support code.
- **About 15 runs were cancelled in one burst at about 03:41Z without ever starting.** They are trios of lint, readiness and review-gate runs created between 02:12Z and 03:41Z:
  - 43790693538, 43790693682, 43790693891 (02:12:03Z, cancelled after about 5,387s)
  - 43790790618, 43790790622 (02:12:59Z)
  - 43795271347, 43795271381, 43795271476 (02:57:53Z)
  - 43799571683, 43799571706 (03:40:19Z)
  - 43799680321, 43799680410 (03:41:30Z)

  Each one's `created_at + duration` lands at 03:41:00–03:41:53Z. These are PR checks that queued for up to 89 minutes and never ran before the PR (presumably) closed.
  - Impact: diagnostic. Confidence: high on the timing, low on the cause.

## Speed Optimizations

All of the items below are on the critical path; the window shows no micro-optimizations worth listing.

1. **Cancel superseded PR CI runs.**
   - Evidence: 10 of 10 CI runs cancelled. Job-level `cancelled_before_first_step` waits of 22,576s (43764204116), 19,017s (43771105041, `static-checks`), 17,526s (43773002936) and 9,591s (43765558368, `tests-sandbox-python (codex-isolated-codex)`).
   - Root cause: `ci.yml` triggers on `pull_request: ["**"]` and on `push` to `main`/`stable`/`orchestrator/project-*` with no concurrency group. Every autofix push queues another ~39 jobs while the old SHA's jobs are still pending.
   - Change: add a workflow-level block after `on:` in `ci.yml`. It applies only to `pull_request`, so base-branch push CI, which check-failure triage needs (`ci.yml:5-12`), is never cancelled:
     ```yaml
     concurrency:
       group: ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
   - Estimated saving: removes the ~39-job backlog for every superseded PR push. It should cut queue wait for every other workflow by a large fraction of the hours currently observed (inference, not measured).
   - Risk: low. Cancelled checks land only on stale SHAs, which cannot be merged.
2. **Give `lint-pr-body-auto-close.yml` and `integration-pr-readiness.yml` per-PR concurrency.**
   - Evidence: 22 successes each, p50 10,239.5s / 10,173s, p95 16,862s / 16,943s, against a 5-minute execution budget.
   - Root cause: every `edited`, `synchronize`, `labeled` or `unlabeled` event queues its own job, and none is ever superseded.
   - Change: add `concurrency: { group: <workflow-slug>-pr-${{ github.event.pull_request.number }}, cancel-in-progress: true }` to each file.
   - Risk: low. The newest event re-evaluates the newest title, body, labels and head SHA, and readiness posts a commit status for that SHA.
   - Saving: these are tiny jobs, so they free little runner time, but they stop older events from piling up in the queue.
3. **Sweep-type workflows: no change; the delay is entirely runner wait.** Queue time versus execution time:

   | Run | Queue wait | Execution |
   |---|---|---|
   | 43795529699 | ~3,569s | 57s |
   | 43801483983 | ~192s | ~30s |
   | 43789562661 | ~152s | not given |
   | 43786748921 | ~208s | not given |
   | 43795977490 (Drift Audit) | ~757s | ~42s |

   Fixes 1 and 2 address the cause.

## Cost Optimizations

1. **Runner minutes, not tokens, are the measurable cost this window.**
   - CI fan-out of about 39 jobs per PR push is the top driver, and the Speed fix 1 concurrency block is also the top cost fix.
   - The saving is proportional to how often a PR is pushed again within one CI duration. Autofix-driven re-pushes make that common (inference).
   - Quality risk: none, because only stale SHAs are cancelled.
2. **Token, model and cache cost cannot be evaluated.**
   - Every `codex_*`, `or_*`, Semble and Serena counter is 0 across all 115 runs with log telemetry.
   - The AI phases were mostly `skipped`: implement 159/159, plan 162/163, clarify 167/171, clarify-respond 165/165. That is plausibly a downstream effect of the stalled poller (inference).
   - Even when the Claude engine runs, its usage is dropped from per-run `cost_telemetry`, because `build_run_cost_telemetry` (`scripts/cost_audit.py:609-613`) copies only `RUN_COST_TELEMETRY_FIELDS`.
   - Change (additive, backward compatible): append the five `CLAUDE_COUNT_KEYS` and `claude_cost_usd` to `RUN_COST_TELEMETRY_FIELDS`. Also append the five count keys to `AGGREGATABLE_COST_FIELDS`, and sum `claude_cost_usd` as a float in `aggregate_run_cost_telemetry`.
3. **Semble and Serena.** No `SEMBLE_*` or `SERENA_*` lines appear in any run or `log_summary`, so there is no evidence either way on prompt-expansion reduction or noisy bytes.
4. **Wasted log fetches in Drift Audit.**
   - `scripts/drift_audit.sh:816-818` still calls `_fetch_run_log` for runs whose conclusion is in `UNSCANNABLE_CONCLUSIONS`. In run 43795977490 that was 9 of 25 fetches (36%), at about 2–5s each.
   - Change: skip the fetch when `conclusion == "skipped"`. Keep fetching for `cancelled`, because runs cancelled mid-execution do have logs.
   - Small saving: about 20–45s and up to 9 REST calls per audit.

## Reliability Improvements

1. **Poller starvation** (category: concurrency or hang).
   - Evidence: 37 of 38 `orchestrate_poll` runs `cancelled_before_first_step` (for example 43801386220 after 1,020s, 43777910055 after 2,357s). The only success is 43763083431 (9,422s). The cron is `*/5` (`internal-orchestrate-poll.yml:5`), and the job-level group uses `cancel-in-progress: false`, so GitHub keeps only one pending run and drops the older one.
   - Fix, in order:
     1. Add the heartbeat logging under Diagnostic-logging additions to confirm whether an active run is holding the group.
     2. Then set an explicit `timeout-minutes` on `poll` (`orchestrate_poll.yml:95`), sized to the measured execution p99 plus headroom. That must stay well under the implicit 360 minutes.
   - Impact: restores poll ticks and wave progression.
   - Rollback consideration: a timeout can kill a run in the middle of conflict resolution, which the comment at `:98-100` warns about. Measure before you cap.
2. **Promote-candidate follower superseded or queued** (category: runner starvation).
   - 10 of 11 cancelled. The fixes under Speed Optimizations relieve this indirectly.
   - Add an indicator: compare the `promote-candidate` SHA with the `main` SHA at the end of each poll and log the lag.
3. **Cancel-on-close sweeps cannot run.**
   - 36 of 40 runs were `cancelled_before_first_step` (for example 43801901279 after 878s, 43795013154 after 1,196s).
   - Under starvation, the scheduled sweep that cancels runs for closed PRs (`cancel_on_pr_close.yml:301-323`) is itself queued, so it cannot clear the backlog. This loop should ease on its own once Speed fix 1 lands.
4. **There are no failures:** `failure_count` is 0 in every family. The cancellation rate is 147 of 228 completed non-skipped runs (64.5%).
5. **`BREAK_GLASS`, `CONTEXT_BUDGET_WARN`, Semble/Serena fallbacks and probes:** none observed.

### Diagnostic-logging additions (SRE asks)

Each new log prefix needs an `agents.d/<id>-<slug>.md` fragment under "Stable log prefixes (contractual)", per §25.

| # | Where | Line to add | Extra API calls |
|---|---|---|---|
| D1 | `scripts/collect_workflow_logs.py`, next to `extract_failure_point` (`:1069`) | Per-run `queue_wait_seconds` (first job's `started_at − created_at`) and `exec_seconds`, taken from the jobs payload it already fetches | 0 |
| D2 | `cancel_on_pr_close.yml:372` loop | `CANCEL_ON_PR_CLOSE_TARGET run_id= workflow_path= status= created_at= linked_prs=` from `active_runs_file`, which already holds these fields | 0 |
| D3 | `orchestrate_poll.yml` poll job, first and last step | `POLL_TICK_START run_id=` and `POLL_TICK_END run_id= elapsed_s=` | 0 |
| D4 | `scripts/cost_audit.py:209/246` | Claude keys (see Cost item 2) | 0 |
| D5 | Review autofix sweep | The bare `reason=other_conclusion` line (seen in 43795529699, 43792697346, 43789562661, 43786748921) should carry `pr=`, `run_id=` and `conclusion=` | 0 |

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary`. Several summaries say explicitly that none were visible, and the logs were truncated by about 89–96k characters. The deep-dive logs were unavailable, so retrieve hit rate, token-versus-budget, `keyword_method` mix and `fail_open` counts cannot be computed.

Recommendation: once the poller runs again, check that its runs emit `retrieve` events. If they do not, make sure `AI_MEMORY_TELEMETRY` lines go to step stdout outside the truncated middle section, for example by echoing a one-line summary at the end of the step.

## GH API Call Audit

- No GH API call-count, 429, secondary-rate-limit or retry signals appear in any visible summary.
- `scripts/drift_audit.sh:818`: per-run log fetch inside a loop, including runs that cannot be scanned. Skipping `skipped` runs saves up to 36% of that audit's fetches (9 of 25 in run 43795977490).
- `cancel_on_pr_close.yml:377`: one POST per cancelled run. There is no batch endpoint for this. The active-run listing is already a single prefetch (`_cpr_fetch_active_runs`), so it complies with §14.
- Two scheduled `*/5` workflows (poller and cancel sweep) plus the `*/30` review sweep make API-heavy ticks. With 90%+ of those runs superseded, actual API use is lower than the schedule implies. No action needed.
- D1 adds queue-wait observability without new calls, because it reuses the jobs payload.

## Prompt Cache & Memory System

`cache_hit_rate` is null in every family, and cache read/write tokens are 0. There is no evidence to judge prefix stability or fragmentation in this window. `CONTEXT_BUDGET_WARN` count is 0.

The first prerequisite is D4, the Claude key propagation. Claude cache creation and read tokens are parsed (`CLAUDE_USAGE_FIELDS`, `cost_audit.py:122-127`) but never surfaced per run, so any Claude-engine cache-hit analysis is impossible today.

## Orchestrator Health

**Stuck state.** The poller completed one tick in about 6.8 hours, at about 23:57Z (43763083431). Every tick after that was superseded (37 runs).

Indicators to track:
- `POLL_TICK_END` count per hour (D3)
- poll `queue_wait_seconds` (D1)
- the `promote-candidate`–`main` SHA lag

**Downstream symptoms.**
- Implement had 0 successes. One `Unblock judge #9238` run (43762912636) succeeded after 8,467s, and one `Internal: AI Validate [tracking:0]` run (43762909983) succeeded after 9,034s.
- Clarify successes took up to 10,860s (43771053738). Whether that was queue or execution is unknown without D1.

**Conflict-heal and clarification loops.** Not observable here; the clarify and plan runs were skipped by their filters.

**Smallest safe mitigation.** Speed fix 1 frees runners, and D3 confirms whether there is a hang before adding the timeout.

## Pipeline Flow Bottlenecks

1. **Queueing dominates everything.**
   - PR checks: lint and readiness p50 about 10,200s.
   - CI: jobs pending up to 22,576s.
   - Sweeps: 40s to 59 minutes of queue against 30–57s of execution.
   - Drift audit: 757s of queue against 42s of execution.
2. **Retries contribute nothing:** `retries` is 0 and `run_attempt` is 1 on every row.
3. **Compute is small** wherever it was visible (30–57s sweeps, 42s audit). CI compute could not be measured.
4. **Merge and conflict:**
   - The 03:41Z burst cancelled about 15 never-started PR checks at once.
   - At 02:13:25Z a second burst cancelled 43790775820, 43790786288, 43790786301, 43790786392 and 43790790579 within about 30 seconds of creation.
   - D2 will attribute these cancellations to a workflow and PR.

**Order of fixes by end-to-end impact:**
1. CI concurrency (Speed fix 1)
2. PR-check concurrency (Speed fix 2)
3. Poller heartbeat, then timeout
4. Queue-wait metrics in the collector

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottleneck:** saturation of the hosted-runner queue. 1,000 runs in about 6.8 hours, with an estimated ~39 jobs per CI run.
- **Top failure modes:**
  - poller superseded: 37 of 38
  - cancel-on-close sweep superseded: 36 of 40
  - promote-candidate follower cancelled: 10 of 11
  - CI never started: 10 of 10
- **Highest cost driver:** CI runner minutes. Token cost cannot be measured (all zeros, and Claude keys are not surfaced).
- **Top 3 actions:**
  1. PR-only `cancel-in-progress` concurrency in `ci.yml`.
  2. Per-PR concurrency for the lint and readiness workflows.
  3. D1, D3 and D4 telemetry, followed by an evidence-sized poll `timeout-minutes`.

## Metrics Appendix

**Window and coverage:** 1,000 runs (77.2% skipped), from 21:16:44Z (43762784001) to 04:05:06Z (43801901279). 115 runs have parsed log telemetry, the success sample rate is 0.07 (5 sampled successes), and the deep-dive log directory was absent.

| Family | Total | Success | Cancelled | Other | p50 s | p95 s |
|---|---|---|---|---|---|---|
| ci | 10 | 0 | 10 | 0 | 1,901.5 | 20,974 |
| integration_pr_readiness | 32 | 22 | 10 | 0 | 10,173 | 16,943 |
| lint_pr_body_auto_close | 32 | 22 | 10 | 0 | 10,239.5 | 16,863 |
| orchestrate_poll | 38 | 1 | 37 | 0 | 539.5 | 1,395 |
| cancel_on_pr_close | 40 | 4 | 36 | 0 | 640 | 13,425 |
| review_autofix | 44 | 14 | 26 | 4 | 1,001.5 | 8,417 |
| promote_candidate_follow | 11 | 1 | 10 | 0 | 2,602 | 7,399 |
| security_audit | 7 | 5 | 2 | 0 | 1,409 | 2,261 |
| clarify / plan / implement | 171 / 163 / 159 | 4 / 1 / 0 | 0 | 167 / 162 / 159 | 1 | ≤10.5 |
| orchestrate_clarify_respond | 165 | 0 | 0 | 165 | 1 | 10 |
| check_failure_triage / workflow_failure_heal / heal_intake | 41 / 29 / 47 | 0 / 0 / 2 | 0 | 41 / 29 / 45 | 1 | ≤11 |
| **All** | **1,000** | **81** | **147** | **772** | **1** | **9,315** |

| Telemetry | Value |
|---|---|
| failure_count / failure_rate | 0 / 0.0 |
| codex_tokens_used, or_total_tokens, or cache read/write tokens | 0 |
| claude_* | not present in the context (D4 gap) |
| cache_hit_rate | null |
| wall_clock_p50_ms / wall_clock_p99_ms | 251,000 / 18,907,220 (112 samples; run duration including queue) |
| break_glass_count / context_budget_warn_count | 0 / 0 |
| GH API calls / 429s | not instrumented / none visible |
| Semble query/fallback/bootstrap; logged bytes | 0 / 0 / 0; 0 bytes |
| Serena query/fallback; response bytes; tool calls | 0 / 0; 0; 0 |
| Drift audit coverage (43795977490) | scanned 25, fetched 16, unscannable 9 (36%), missing 0 |

| MCP target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| semble | 0 | 0 | 0 |
| serena | 0 | 0 | 0 |

**Other MCP servers observed:** none.

**Data gaps:**
- The run-logs directory was unavailable.
- Sweep logs were truncated by 89–96k characters.
- The 1,000-run cap covered under 7 hours because of skipped runs. Excluding `skipped` from the cap in the collector would widen the window.
- Queue time and execution time are not separated (D1).

---
- **Actions taken:** read-only analysis of the analysis context plus `ci.yml`, `orchestrate_poll.yml`, `internal-orchestrate-poll.yml`, `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `promote-candidate-follow.yml`, `review_autofix_sweep.yml`, `scripts/cost_audit.py`, `scripts/collect_workflow_logs.py` and `scripts/drift_audit.sh`. No files were changed.
- **Assumptions:** a 5-minute `timeout-minutes` paired with a multi-hour duration means queue wait. The 03:41Z cancellation cluster was worked out from `created_at + duration_seconds`.
- **Missing context:** the deep-dive log directory, per-job timestamps, Claude-engine usage, and GH API counters.
## Deep Audit — Workflows & Scripts (2026-11-16)

Scope: a deep read of `cancel_on_pr_close.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `promote-candidate-follow.yml`, `drift-audit.yml`, `review_autofix_sweep.yml`, `sync_ai_labels.yml`, `internal-orchestrate-poll.yml`, selected steps of `implement.yml`, `review_autofix.yml`, `orchestrate_poll.yml` and `update_workflows.yml`, and the scripts `gh_helpers.sh`, `label_helpers.sh`, `promote_candidate_follow.sh`, `drift_audit.sh`, `nag_reminder.sh`, `unblock_judge.sh`, `workflow_failure_heal_intake.sh`, `validate_process.sh` and `orchestrate_poll_process.sh` (loop and PR-creation sites). The other workflows and scripts got targeted pattern searches only.

Some findings are already in the report above, so this section does not repeat them:
- `ci.yml` has no concurrency group (report: Speed Optimizations, item 1).
- The PR-check workflows have no per-PR concurrency (Speed, item 2).
- The `poll` job has no `timeout-minutes` (Reliability, item 1).
- `drift_audit.sh:816-818` fetches logs for skipped runs (Cost, item 4).
- `cancel_on_pr_close.yml:372` loop logging (D2).
- Claude keys are missing from `cost_audit.py` (D4).

### Section 1: Bug & Correctness Sweep

**SEC-001** — `scripts/orchestrate_poll_process.sh:10942-10952`, `:32680-32694`, `:34726-34750`
- Severity: **High** · Category: `security`
- Description: The poller creates PRs at three places with `gh pr create` or `POST /pulls` and never runs `scripts/lint_pr_body_auto_close.py` first. §21 says this lint MUST run before any unattended `gh pr create --body`. The only mention of the lint in the file is a comment at `:23223`.
  - The revert-PR body at `:34731-34733` and `:34746-34748` puts model output (`**Reason:** ${JUDGE_JUSTIFICATION}`) into a PR against `${DEFAULT_BRANCH}`.
  - The follow-up PR at `:32686-32693` adds `${RB_FIX_DESC}`, which `:32297` takes from the judge JSON `.fix_description`.
  - If the model text contains `Fixes #<tracking>`, merging the PR auto-closes the tracking issue. That is the #2734 incident class.
  - Mitigation: `lint-pr-body-auto-close.yml` re-checks PRs into `main`/`stable` at merge time. Whether that check is configured as required in branch protection is [NEEDS VERIFICATION]. Either way the runtime MUST in §21 is not met.
  - The integration PR at `:10948-10952` uses static `Refs #`, but its title `feat: ${project_title}` is not linted.
- Recommended fix:
  - Before each of the three creates, write the title and body to a temp file and run `PYTHONDONTWRITEBYTECODE=1 python3 scripts/lint_pr_body_auto_close.py --pr-body-file <f> --repo "${GITHUB_REPOSITORY}"`. This is the same CLI that `lint-pr-body-auto-close.yml:112-115` uses.
  - On a non-zero exit, skip the create and emit `::error::[lint_pr_body_auto_close]` plus a tracking comment.
  - Mirror implement.yml's "Pre-flight — lint PR title/body for auto-close keywords against tracking issues" step (`implement.yml:6026`) and `scripts/workflow_log_analysis_deliver_report.sh`, which already do this.

**SEC-002** — `.github/workflows/lint-pr-body-auto-close.yml:49-56,100-115`; `.github/workflows/integration-pr-readiness.yml:35-39,51-54,67-79`
- Severity: **Medium** · Category: `security`
- Description: Both gate workflows run `actions/checkout@v5` with the default ref, which for `pull_request` is the PR merge ref. They then execute `scripts/lint_pr_body_auto_close.py` and `scripts/check_integration_pr_readiness.py` from that tree. A same-repo PR that edits either script therefore decides its own gate result.
  - `integration-pr-readiness.yml` also grants `statuses: write` (`:39`). PR-controlled code can then post any commit status, including `orchestrator/integration-pr-not-ready=success`.
  - Pipeline PRs are same-repo branches, and grants allow `scripts/` edits, so this is reachable (inference).
- Recommended fix: Add a second checkout of the trusted base with `ref: ${{ github.event.pull_request.base.sha }}`, `path: .trusted-base`, `persist-credentials: false`, and sparse-check out only the one script. Run the script from `.trusted-base/scripts/`. The repo already does this in `review_autofix_sweep.yml:149-154` ("Check out trusted CI recovery helper").

**BUG-001** — `.github/workflows/cancel_on_pr_close.yml:150-153, 265-267, 332, 398-434`
- Severity: **Medium** · Category: `bug`
- Description: The "Cancel queued/in-progress runs" step runs under `set -euo pipefail`, and `_cpr_fetch_active_runs` pipes `gh_retry gh api --paginate` into `jq` (`:174-193`). If that listing fails, the step fails. The next step, "Release merge-queued PRs (merge train)" at `:398`, has no `if:`, so it is skipped.
  - The comment at `:399-401` says core-profile repos have no poll backstop for this release.
  - The job also has `timeout-minutes: 5` (`:13`). Under the runner and API saturation in this report, a rate-limit wait inside `gh_retry` can exhaust it before the release step starts [NEEDS VERIFICATION].
- Recommended fix: Add `if: ${{ !cancelled() }}` to the release step. Its body is already fail-open (`:430-434`).

**BUG-002** — `.github/workflows/review_autofix_sweep.yml:109-126`
- Severity: **Medium** · Category: `bug`
- Description: The `sweep` job has no `timeout-minutes`; there is none anywhere in the file. It shares the workflow-level group `review-autofix-sweep-<repo>` with `cancel-in-progress: false` (`:109-111`).
  - A hang in the provider-outage probe (`:186-204`, which calls the model provider) or in the per-PR `gh workflow run` loop (`:476-631`) would hold the group for GitHub's 360-minute default.
  - Meanwhile each `*/30` tick replaces the previous pending one (inference, same mechanism as the report's poller diagnosis).
- Recommended fix: Set a job `timeout-minutes` below the 30-minute cron interval, for example 25, sized from the measured p99. Add a step-level `timeout-minutes` on "Provider outage probe and resume".

**BUG-003** — `scripts/promote_candidate_follow.sh:192-196, 245-252`; `.github/workflows/promote-candidate-follow.yml:13-25`
- Severity: **Medium** · Category: `bug`
- Description: The "one CRITICAL alert per UTC day" for an unprotected `promote-candidate` branch only fires when `GITHUB_EVENT_NAME == schedule` and the wall-clock hour at execution is `00`. The cron is `'17 * * * *'` (`:17`). The workflow also runs on every push to `main` (`:14-15`) in a group with `cancel-in-progress: false` (`:23-25`).
  - A push between 00:00 and 00:59 replaces the pending scheduled run, so the run that executes has `GITHUB_EVENT_NAME=push`.
  - A scheduled run that waits past 01:00 checks the wrong hour.
  - Either way the day's alert is lost. The report records 10 of 11 follower runs cancelled, so this is likely, not hypothetical (inference).
- Recommended fix: Drop the wall-clock hour gate and deduplicate per day with a cache marker. `orchestrate_poll.yml:104-107` already does this for the daily Claude pool report (`steps.pool_daily_seen`). Alternatively, alert on any scheduled run whose run-creation hour is `00`.

**BUG-004** — `scripts/orchestrate_poll_process.sh:33083-33098`
- Severity: **Low** · Category: `bug`
- Description: `LABELS_JSON` is built by string concatenation, `LABELS_JSON+="\"${inum}\":${LABELS}"` (`:33096`). Unlike its sibling loops, there is no `[ -n "${LABELS}" ] || LABELS='[]'` guard; compare `:30597` and `:33108`. An empty successful capture produces invalid JSON, which goes to `orchestrate_lib.py check-wave-status --labels-json` (`:33112-33114`).
- Recommended fix: Add the same empty guard and build the object with `jq -c --arg key ... --argjson labels ...` as `:33109` does. BATCH-001 replaces the whole loop.

**BUG-005** — `scripts/drift_audit.sh:470-486, 746-759`
- Severity: **Low** · Category: `bug`
- Description: `_tracker_issue_lookup` reads at most 1,000 open issues with full bodies (`--limit 1000 --json number,title,body,url`). With more than 1,000 open issues, an existing tracker can be missed and `_create_issue` opens a duplicate.
  - `_create_issue` adds no label, so the tracker cannot be filtered server-side.
  - Whether an unlabeled `issues: opened` event starts the clarify pipeline here is [NEEDS VERIFICATION].
- Recommended fix: Give trackers a dedicated label (add it to `.github/ai/label_contract.v1.json` and the `label_helpers.sh` catalog). Look trackers up with `gh issue list --label <it>`, or with `--search '"drift-audit:cluster-key=" in:body'`.

**BUG-006** — `scripts/drift_audit.sh:159-167, 458-467`
- Severity: **Low** · Category: `bug`
- Description: `_run_gh` calls `subprocess.run(["gh", ...])` with no `timeout=` and no retry. One hung `gh run view --log` (`:459`) holds the job until `drift-audit.yml`'s 60-minute timeout (`:21`). A single transient failure marks the run as missing and degrades the coverage verdict.
- Recommended fix: Pass `timeout=` (for example 120 s) to `subprocess.run` and retry up to 2 more times with backoff on non-zero exit codes other than 404. That is the classification `gh_api_retry` uses (`gh_helpers.sh:1119-1173`).

### Section 2: GitHub API Call Redundancy Audit

**BATCH-001** — `scripts/orchestrate_poll_process.sh:33083-33110, 33538-33546`
- Severity: **Medium** · Category: `api-batching`
- Description: After review-blocked or implementation-failed handling, the poller re-reads labels with one REST `GET issues/<n>/labels` per wave issue (`:33090`) and per reissued issue (`:33107`, `:33543`). Earlier in the same tick, `:30583` fills the same structure with the batched `_fetch_issue_labels_batch_graphql`. Its contract (`:4805-4816`) is a JSON array in, an object keyed by issue number out, with per-batch fail-open.
- Current calls: N wave issues plus up to 2×M reissued issues, all REST, per state-changing tick.
- Projected calls: 1 GraphQL call per helper batch, plus REST only for keys missing from the result.
- Pattern to extend: `_fetch_issue_labels_batch_graphql`, followed by the cache-miss REST fallback loop at `:30588-30599`.

**BATCH-002** — `scripts/orchestrate_poll_process.sh:7778-7790` and `:33225-33227`
- Severity: **Low** · Category: `api-batching`
- Description: Two loops each read one blocker's state with `gh_retry _safe_gh_jq "repos/.../issues/${blocker_issue}" --jq '.state'`.
- Current calls: B REST calls per loop.
- Projected calls: one batched call per loop. `_fetch_candidate_issue_details_graphql` (`:22851`) already returns `state`; `:30574-30577` uses it for `ISSUE_STATES_JSON`.
- Pattern to extend: `_fetch_candidate_issue_details_graphql` (cache `_candidate_details_json`), keeping the per-issue REST fallback on a miss.

**BATCH-003** — `scripts/drift_audit.sh:470-486`
- Severity: **Low** · Category: `api-batching`
- Description: Every audit with markers downloads up to 1,000 open issues with full bodies to find a handful of trackers (see BUG-005).
- Current calls: 1 paginated listing of up to 1,000 issues.
- Projected calls: 1 listing filtered by label, a few items.
- Pattern to extend: none needed, just a label filter.

**API-001** — `.github/workflows/cancel_on_pr_close.yml:377-386`; `scripts/orchestrate_poll_process.sh:18738-18741`
- Severity: **Low** · Category: `api-redundancy`
- Description: `gh_retry gh api -X POST .../actions/runs/<id>/cancel` is classified as a non-idempotent POST (`gh_helpers.sh:714-778`, `:1215-1217`), so it gets exactly one attempt. A transient 5xx leaves a closed PR's run alive. Both sites also discard stderr (`>/dev/null 2>&1`, `2>/dev/null || true`), which hides `gh_retry`'s diagnostics. Cancel is idempotent.
- Current calls: 1 attempt per run.
- Projected calls: unchanged on success; at most 5 attempts on transient errors.
- Pattern to extend: the existing `--idempotent` flag: `gh_retry --idempotent gh api -X POST ...`.

**API-002** — `scripts/orchestrate_poll_process.sh:189-205`
- Severity: **Low** · Category: `api-redundancy`
- Description: `gh issue list --state open --label ai:reclarify-requeue` (`:193-194`) already guarantees open, labeled and not-a-PR. The loop still does one `GET issues/<n>` per issue (`:202`) to re-check those same predicates (`:203-204`), then a paginated comments read (`:205`). The header comment (`:186-188`) says the live read is deliberate, so dropping it is [NEEDS VERIFICATION].
- Current calls: 2 per queued issue (Q issues → 2Q).
- Projected calls: Q, with the issue read dropped and comments kept. Alternatively, ⌈Q/25⌉ GraphQL calls for the issue read in the alias style of `:273-278`.
- Pattern to extend: the aliased `issue(number:)` batch at `:273-278`.

**API-003** — `.github/workflows/review_autofix_sweep.yml:236-247, 310-315`
- Severity: **Low** · Category: `api-redundancy`
- Description: The sweep fetches `repos/<repo>` only to read `default_branch` (`:310`). The open-PR listing it already holds (`:236`) returns `base.repo.default_branch` on every PR object [NEEDS VERIFICATION, field present in the REST pulls payload]. The step only needs the value when at least one candidate exists (`:298-301`).
- Current calls: 1 per tick with candidates.
- Projected calls: 0. Add `default_branch: .base.repo.default_branch` to the jq projection at `:238-247`, and keep the regex validation at `:311`.

**API-004** — `.github/workflows/implement.yml:2276-2299`
- Severity: **Low** · Category: `api-redundancy`
- Description: "Fetch issue comments" has its own retry loop: 5 attempts, linear `sleep $((attempt * 2))`.
  - Each attempt re-downloads every comment page.
  - It retries 404 and 403 as well, and `{ ...; } 2>/dev/null` hides all `gh` and `jq` errors.
  - A permanent failure ends as the misleading "Unable to fetch triggering approval comment".
- Current calls: up to 5×P pages.
- Projected calls: P pages per attempt, with backoff, and stop on permanent errors. Keep the outer loop only for the comment-visibility race.
- Pattern to extend: `gh_retry_to_file` (`gh_helpers.sh:1312-1385`) for the fetch.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `scripts/label_helpers.sh:176-218`; `.github/workflows/review_autofix.yml:1282-1285, 1699-1705, 4630`; `scripts/orchestrate_poll_process.sh:4476`; `scripts/validate_process.sh:1231`; `scripts/review_rb_judge.sh:924`; `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`; `scripts/review_autofix_step_mark_ready_to_merge.sh:15`
- Severity: **Medium** · Category: `duplication`
- Description: There are 9 definitions of `ensure_label_exists`, with at least three incompatible signatures:
  - `label_helpers.sh` takes `<label> [repo]`, uses the catalog colors and an in-process cache.
  - `review_autofix.yml:1282` takes `label repo color desc` and swallows every error with `|| true`.
  - `orchestrate_poll_process.sh:4476` and `validate_process.sh:1231` take `<label>` only and read `.github/ai/label_contract.v1.json` relative to the working directory, which is absent in consumer checkouts (inference).
  - The signatures of the last three files listed above were not verified.
  - A caller that sources more than one of these gets whichever definition loaded last.
- Recommended fix: Make `label_helpers.sh::ensure_label_exists <label> [repo]` the only definition. Delete the inline copies and source the staged `label_helpers.sh`, keeping each file's existing `type ensure_label_exists` guard as the fallback. The callers to update are the files listed above.

**DUP-002** — `.github/workflows/review_autofix.yml:1251-1280`
- Severity: **Medium** · Category: `duplication`
- Description: The deterministic-skip-merge step defines its own `gh_retry`, which has drifted from `gh_helpers.sh:1208-1301`:
  - It retries every failure 4 times with backoff 2, 4 and 8 s, including permanent 4xx errors such as a 422 from `gh pr merge --auto` (`:1431`, `:1468`). There is no `_is_gh_permanent_failure` check.
  - It ignores `GH_RETRY_IDEMPOTENT` and the single-attempt rule for POST creates (#6634).
  - On `mktemp` failure it prints the full argv (`$*`, `:1262`), which the library replaced with `_gh_cmd_log_summary` (#8095).
- Recommended fix: The job already checks out a verified helper tree, `.codex-freshness-src` (`:1145`). Add `scripts/gh_helpers.sh` to that sparse checkout and source it under the same SHA verification. Keep the inline function only as a fallback that also short-circuits on `HTTP 4[0-9][0-9]` other than 429.

**DUP-003** — The "Resolve workflow support ref" step, present 18 times across 11 workflows (`promote_candidate_workflow_ref="$(jq -r` occurs in clarify.yml ×2, plan.yml ×2, implement.yml ×4, orchestrate.yml ×2, orchestrate_poll.yml ×2, cancel_on_pr_close.yml:37-74, and others). Variants without the promote-candidate path exist, for example `sync_ai_labels.yml:37-57`.
- Severity: **Low** · Category: `duplication`
- Description: This step runs before any checkout, so it cannot be moved into `scripts/`. The copies differ only in the workflow filename inside the regexes (for example `cancel_on_pr_close.yml:47,69`). A fix to one copy has to be repeated 18 times.
- Recommended fix: Extend `tests/test_workflow_support_ref_follows_pin.py` (its current coverage is [NEEDS VERIFICATION]) to assert that every copy is byte-identical after substituting the workflow filename. That way drift fails CI instead of shipping.

**DUP-004** — `.github/workflows/review_autofix.yml:5226-5245, 5317-5336`; `scripts/review_autofix_step_post_editor_summary.sh:103, 250`; `scripts/review_autofix_step_assemble_failure_evidence.sh:63`
- Severity: **Low** · Category: `duplication`
- Description: The `autofix-failure-fingerprint` invocation, with the same 5 `--evidence-file` paths, appears 5 times. A sixth evidence file has to be added everywhere, or the fingerprints diverge.
- Recommended fix: Add `review_autofix_failure_marker_suffix()` to a new `scripts/review_autofix_helpers.sh` that prints `$'\n\n'<marker>` or nothing. Call it from the four sites, and register the file in `REQUIRED_BOOTSTRAP_SCRIPTS`.

**DUP-005** — `scripts/orchestrate_poll_process.sh:30592-30599, 33102-33110, 33540-33546`
- Severity: **Low** · Category: `duplication`
- Description: The "per-issue labels REST fallback merged into `LABELS_JSON`" block appears three times (cross-ref BATCH-001).
- Recommended fix: Add `_refresh_wave_labels_json <nums_json> <existing_json>`, which calls `_fetch_issue_labels_batch_graphql` and then falls back per missing key. Use it at all three sites.

**DUP-006** — `.github/workflows/update_workflows.yml:111-112, 287, 415, 1406, 2132`
- Severity: **Low** · Category: `duplication`
- Description: The same one-shot git credential helper string, plus the `upstream_git` wrapper, is defined in 5 steps. It is correctly single-quoted, so the token is not exposed.
- Recommended fix: Move both into a small `scripts/git_cred_helpers.sh` that the job stages, or at minimum pin all 5 copies with a parity test.

No near-duplicate workflows above 70% similarity were found beyond the `internal-*.yml` wrappers, which are thin `uses:` stubs by design (for example `internal-orchestrate-poll.yml:16-19`).

### Section 4: Expression Size Limit Risk Assessment

**Existing guard.** `tests/test_workflow_file_size_limit.py:66-98` fails CI for any `jobs.*.steps[].run` body that contains `${{` and is 18,000 characters or longer. `:44-59` fails any workflow file of 480,000 bytes or more. As long as CI is green, no High-risk (≥18,000) run body can exist, and no workflow is anywhere near the 800 KB flag or the 1 MB limit. The binding limit is GitHub's 512,000-byte workflow limit (`agents.md` "Workflow file size limit").

Character counts below are estimates from line spans, not measurements.

**EXPR-001** — `tests/test_workflow_file_size_limit.py:35-36, 66-98`
- Severity: **Low** · Category: `expression-limit`
- Description: The guard only covers `.github/workflows/*.yml` and `steps[].run`. It does not measure:
  - `if:` expressions (for example `review_autofix.yml:5451`, about 330 characters).
  - `with:` and `env:` values.
  - Composite actions under `.github/actions/*/action.yml` (6 files, `runs.steps[].run`).
  - `workflow-templates/*.yml`.
- Recommended fix: Extend the same test to walk `.github/actions/*/action.yml` and `workflow-templates/*.yml`, and to measure every string value containing `${{` (`if`, `with`, `env`) against the same 18,000 guard.

**EXPR-002** — `.github/workflows/implement.yml:6213-6383` ("Create Pull Request")
- Severity: **Low** (Medium if the measured size is ≥15,000) · Category: `expression-limit`
- Description: The roughly 170-line body is prose-heavy and has grown a transient-retry recovery loop (`:6275-6373`). It contains 5 `${{ github.repository }}` interpolations (`:6241, 6267, 6303, 6351, 6356`), so the whole body is one expression. Estimated 12,000–14,000 characters, leaving about 7,000–9,000 of headroom against 21,000 [NEEDS VERIFICATION, measure with the test's `len(run)`].
- Recommended fix: Replace each `${{ github.repository }}` with the runner default `${GITHUB_REPOSITORY}`. The body then has no expression and is exempt from the limit, with no behavior change.

**EXPR-003** — `.github/workflows/review_autofix.yml:5219-5302`, `:5310-5448`
- Severity: **Low** · Category: `expression-limit`
- Description: The Telegram "editor-changes-lost" and "editor-noop-suspicious" steps are prose and alert-template bodies, estimated at about 6,000 and 10,000 characters. They interpolate `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` (`:5223-5224, 5291, 5314-5315, 5374, 5425, 5447`). Each new alert branch adds roughly 1,500–2,500 characters.
- Recommended fix: Use `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}`, which makes both bodies expression-free. Later, move them to `scripts/review_autofix_step_<slug>.sh` following the agents.md split pattern.

**EXPR-004** — `.github/workflows/implement.yml:2893-4218` ("Run Codex implementation")
- Severity: **Low** · Category: `expression-limit`
- Description: The body is about 1,290 lines. It is currently expression-free: every `${{` sits in its `env:` block (`:2903-2931`), so it is not subject to the limit. A single future inline `${{ }}` would put it several times over 21,000 characters. CI would reject that at review time, but the fix would then be a forced large extraction.
- Recommended fix: Move the body to `scripts/implement_step_run_codex.sh`, following the resolving-wrapper pattern in agents.md "Workflow file size limit". This also cuts about 60 KB from `implement.yml` (estimate).

No `if:` condition near the limit was found in the files read. The largest observed is about 330 characters.

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `scripts/orchestrate_poll_process.sh:34719-34758`
- Severity: **Medium** · Category: `dead-code`
- Description: The "API-based revert" calls `POST repos/<repo>/pulls` with `head="revert-${PR_TO_REVERT}-$(date +%s)"` (`:34727-34733`), but nothing has created or pushed that branch. The call therefore always fails with a validation error, and every revert falls through to the git path (`:34734-34756`).
  - Each revert wastes one API call and logs a misleading "API revert failed".
  - The fallback computes a second, different `$(date +%s)` branch name (`:34738`).
  - The loop runs in an `echo | while read -r` subshell.
- Recommended fix: Delete the API attempt and compute `REVERT_BRANCH` once. Feed the loop with `while IFS= read -r ...; done < <(jq -r ...)`, and apply SEC-001's lint before `gh pr create` (`:34743`).

**DEAD-002** — `.github/workflows/review_autofix.yml:1573-1583`
- Severity: **Low** · Category: `dead-code`
- Description: The `claude-fixer-auto-merge` job never runs; the gate never emits `claude_fixer_auto_merge_retired`. It is kept on purpose so a required check of that name keeps reporting "skipped", which is consistent with §10.
- Recommended fix: Keep it. Add a removal trigger to the comment, such as "remove once no consumer's branch protection lists this check", so the retention has an end condition.

**TODO/FIXME/HACK markers:** none. A search for `\b(TODO|FIXME|HACK|XXX)\b` over `scripts/*.sh`, `scripts/*.py` and `.github/workflows/*.yml` returned no matches.

**CONSIST-001** — `scripts/unblock_judge.sh:1085-1086, 1091`
- Severity: **Low** · Category: `consistency`
- Description: Label add and remove use raw `gh api -X POST|DELETE` with no retry, so a transient failure sets `ops_failed=true` and the verdict is not fully applied. The same idempotent operation elsewhere uses `GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST .../labels` (`review_autofix.yml:1502, 1542, 1783`).
- Recommended fix: Wrap both calls in `GH_RETRY_IDEMPOTENT=true gh_retry`, keeping the 404-as-success handling for DELETE (`:1094-1099`).

**CONSIST-002** — `.github/workflows/sync_ai_labels.yml:70-78, 125`
- Severity: **Low** · Category: `consistency`
- Description: The fallback checkout lacks `continue-on-error: true`, unlike `cancel_on_pr_close.yml:87-96`, so a failure there ends the job with a raw checkout error instead of the "Ensure" step's `::error::`. `:125` interpolates `${{ inputs.dry_run }}` inline, while the next step (`:170`) passes the same input through `env:`.
- Recommended fix: Add `continue-on-error: true` to the fallback checkout, and use `DRY_RUN` from `env:` at `:125`.

**CONSIST-003** — `.github/workflows/update_workflows.yml:108`
- Severity: **Low** · Category: `consistency`
- Description: `TMPDIR=$(mktemp -d)` overwrites the standard `TMPDIR` variable, which `gh_helpers.sh` reads (`${TMPDIR:-/tmp}`, for example `:1220`) and which child processes inherit when it is exported. This is the only such assignment in the repo, and it collides with an existing identifier in the sense of §10.
- Recommended fix: Rename it to `UPSTREAM_TMP_DIR` at `:108, 116, 117, 147`.

**CONSIST-004** — Inline `${{ github.repository }}` and `${{ github.sha }}` in run bodies, for example `cancel_on_pr_close.yml:40-41`, `implement.yml:277, 379, 1007, 2284, 2517, 2536-2537, 2559, 2771`, `orchestrate_poll.yml:154-155, 328`
- Severity: **Low** · Category: `consistency`
- Description: Most steps use `env:` or the runner defaults; these do not. They needlessly turn each body into an expression (see EXPR-002).
- Recommended fix: Mechanically replace them with `${GITHUB_REPOSITORY}` and `${GITHUB_SHA}`.

**DEBT-001** — `scripts/gh_helpers.sh:1050-1179` vs `:1208-1301`
- Severity: **Low** · Category: `tech-debt`
- Description: Two retry engines coexist in one library:
  - `gh_api_retry` classifies by HTTP status, uses a circuit breaker and jitters its backoff.
  - `gh_retry` greps stderr and backs off 1, 2, 4, 8 s without jitter.
  - Callers pick one arbitrarily. For example, `review_autofix_sweep.yml:224-228` uses `gh_api_retry` while `cancel_on_pr_close.yml:154-155` uses `gh_retry` against the same `actions/runs` endpoint.
- Recommended fix: Have `gh_retry` delegate `gh api` invocations to `gh_api_retry` internally and keep its current interface. Document `gh_api_retry` as canonical in agents.md "Utility helpers" (through an `agents.d/` fragment, §25).

**DEBT-002** — 87 occurrences of `type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }` across 47 files, for example `review_autofix.yml:5221-5222, 5312-5313` and `implement.yml:6215-6216`
- Severity: **Medium** · Category: `tech-debt`
- Description: These stubs follow `source .../gh_helpers.sh 2>/dev/null || true`. When the helper fails to load, the step silently loses retries, rate-limit handling, and the single-attempt guard for POST creates. Nothing in the log shows the degradation.
- Recommended fix: Replace the stub with one that warns once, for example:

```bash
type gh_retry >/dev/null 2>&1 || {
	echo "::warning::gh_helpers.sh unavailable; gh_retry runs without retry/rate-limit handling"
	gh_retry() { "$@"; }
}
```

**SHELL-001** — `.github/workflows/review_autofix.yml:5392-5393`; `.github/workflows/review_autofix_sweep.yml:376`
- Severity: **Low** · Category: `shellcheck`
- Description: `review_autofix.yml:5392-5393` pipes `ls -1 .../editor_attempt_*.err | wc -l` (SC2012). `review_autofix_sweep.yml:376` uses `cat "${snap_raw}" | jq` (SC2002).
- Recommended fix: Count with `compgen -G` or a glob array (`files=( "${dir}"/editor_attempt_*.err )`), and run `jq ... "${snap_raw}"` directly.

**SHELL-002** — `scripts/orchestrate_poll_process.sh:33085, 33102, 33540, 34721`
- Severity: **Low** · Category: `shellcheck`
- Description: `:33085`, `:33102` and `:33540` use `for inum in ${ISSUE_NUMS}` and `for rnum in ${REISSUED_NUMS}`, which rely on unquoted word splitting (SC2086). `:34721` uses `read -r` without `IFS=` in a pipe-fed `while` (SC2162/SC2031 class).
- Recommended fix: Read into an array with `mapfile -t` and iterate `"${arr[@]}"`, and use `while IFS= read -r` fed by process substitution.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 9 | SEC-002, BUG-001, BUG-002, BUG-003, BATCH-001, DUP-001, DUP-002, DEAD-001, DEBT-002 |
| Low | 25 | BUG-004, BUG-005, BUG-006, BATCH-002, BATCH-003, API-001, API-002, API-003, API-004, DUP-003, DUP-004, DUP-005, DUP-006, EXPR-001, EXPR-002, EXPR-003, EXPR-004, DEAD-002, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, DEBT-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` (SEC-001: three PR-creation sites) | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/drift_audit.sh`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/implement.yml` | Medium |
| Code modularization | `scripts/label_helpers.sh`, `.github/workflows/review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh`, `scripts/review_autofix_step_mark_ready_to_merge.sh`, `scripts/review_autofix_step_post_editor_summary.sh`, `scripts/review_autofix_step_assemble_failure_evidence.sh`, `.github/workflows/update_workflows.yml`, `tests/test_workflow_support_ref_follows_pin.py` | Large |
| Expression size reduction | `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `.github/workflows/lint-pr-body-auto-close.yml`, `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/promote-candidate-follow.yml`, `scripts/promote_candidate_follow.sh`, `scripts/drift_audit.sh`, `scripts/unblock_judge.sh`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/update_workflows.yml`, `scripts/gh_helpers.sh` | Medium |