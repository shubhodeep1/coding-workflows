<!-- workflow-log-analysis: window_start=2026-11-28T11:53:13Z window_end=2026-11-28T18:04:50Z run=46173713323 -->
Checkpoint:
- No files edited (read-only audit). Prior verdicts gathered from `analysis/recommendation-processing-report.md`. The run-logs directory is not mounted.
- Code checked for each finding: the resolver retry state (`scripts/review_conflict_resolve.sh`), the triage cleanup (`check_failure_triage.yml:653-663`), the comment-signing shim (`scripts/gh_helpers.sh:49-51`), the Claude cost fields (`scripts/cost_audit.py:209,246,611`), the reviewer attribution check (`scripts/review_findings_record.py:745`) and the poller cron template.

## Executive Summary

- **Every review/autofix failure in this window came from the merge-conflict resolver, and two PRs kept failing on retry.** All 7 failures stopped at `review / codex-agent` → `Run Codex resolver, validate, stage, commit`. PR 10066 failed 5 times (runs 46149045911, 46152599654, 46161338694, 46164251484, 46168873110) and PR 9396 twice (46163869004, 46168721329). Together that is 17,363 s, about 4.8 runner-hours, with no fix produced. The review gate's identical-failure cap (default 3) did not stop PR 10066 at 3. The logs cannot tell us why, because the retry-state line is free text and is skipped on several early-return paths. Fix: add a structured `RESOLVER_RETRY_STATE` line (Reliability R1). Impact: the next window can show whether the cap is missing a real loop; up to about 3 runner-hours a window if it is. Confidence: high on the failure facts, low on the cause.
- **Waiting for a runner, not doing work, takes most of the wall-clock time.** In 30 of the 31 log summaries that report a wait, the job waited for "All GitHub-hosted runners … are busy". Examples: a 12-minute wait for a 726 s pr-body-lint run (46170918796), 9 minutes for a review gate (46170934504), and 6–10 minutes for every consumer poller. The worst waits start right after the `*/15` poller ticks at :30 and :45. Fix: move the internal poller off the shared tick first, then give each consumer its own minute (Speed S1). Impact: shorter peak waits for PR-facing jobs. Confidence: low–medium.
- **The analyzer cannot see Claude spend.** `build_run_cost_telemetry` copies only `RUN_COST_TELEMETRY_FIELDS` (`scripts/cost_audit.py:611`), and that list has no `claude_*` keys. Runs on the Claude engine (`ai:engine-claude` PRs, the resolver) therefore report 0 tokens. All 7 resolver failures show `codex_calls=0` and `or_calls=0` despite running 30–67 minutes. Fix: Cost C1. Impact: Claude cost becomes visible for this report and the next. Confidence: high.
- **Check-failure triage deletes an action before its post-step runs.** The cleanup at `check_failure_triage.yml:660` keeps only `claude-pool-token`, but `install-claude` (line 479) uses `actions/cache@v5`, whose post-step needs the action's `action.yml`. Run 46172150663 logged `##[error]Can't find 'action.yml' … install-claude`. Fix: Reliability R2, a one-token change. Impact: removes an error line on every Claude-engine triage run and restores the npm cache save on a miss. Confidence: high.
- **Comment-triggered wrapper runs that end up skipped are still frequent: 251 in this window.** By family: clarify 70, clarify-respond 68, plan 58, implement 55. They arrive in bursts of four per comment, for example 46170958346/379/471/351 at 17:31:47. The logs never say whether run-volume plan item 15 (signed job-token comments) is active, because `_ai_comment_sig_activate` returns silently when the secret is unset (`scripts/gh_helpers.sh:51`). Fix: log once per job when signing is disabled (GH API G2). Confidence: medium.
- **Data gaps.** The run-logs directory `/home/runner/work/_temp/workflow-log-output` is not mounted, so every claim rests on `analysis_context.json` and the per-run `log_summary` text. Coverage lists no failed or truncated repositories.

## Speed Optimizations

**S1. Spread the 15-minute poller ticks: internal poller first, consumers second (critical path for PR-facing jobs).**
- **Evidence:**
  - Consumer pollers were created in two bursts, 17:32:18–17:36:03 and 17:46:52–17:49:56: fbc_shutdown 46171006979/46172298625, tele-funtoken 46171025096/46172349158, binance 46171063486/46172362158, bitsafe 46171133632/46172358442, multi-user 46171286510/46172505450, fun-token 46171311296/46172472608, mongo 46171346863/46172566316.
  - The PR-facing jobs created in the same minutes waited the longest: pr-body-lint 46170918796 (17:31:22) about 713 s; CI quick 46170918765 about 425 s; review gate 46170934504 about 559 s; plan 46170959831 about 403 s.
  - After the burst cleared, waits shrank: 46172405861 at 17:48 waited 149 s, and 46172405844 at 17:48 waited 29 s.
- **Root cause:** `workflow-templates/ai-orchestrate-poll.yml:6` and `.github/workflows/internal-orchestrate-poll.yml:5` both use `cron: '*/15 * * * *'`. Every poller, plus the review sweep seen at 17:30:26 and 18:00:43, asks for a runner in the same minute, and all of them draw from one account-wide pool (§26). That consumers sharing a minute causes the PR-job waits is an inference: other waits also occur off-burst, for example heal intake 46171606784 waited 6m48s at 17:39.
- **Exact change:**
  1. Low risk, in this repo only: change `internal-orchestrate-poll.yml` to `cron: '7-59/15 * * * *'`. First check that minute 7 is compatible with `CLAUDE_POOL_HEALTH_WINDOW_MINUTES` (plan line 217).
  2. Medium risk, consumers: render a per-repo minute offset into the copied template, and teach `scripts/audit_consumer_drift.py` to ignore that one cron line so the drift audit does not flag it.
- **Estimated savings:** not measurable from this data. The plausible range is a few minutes off the peak wait for jobs created in a :30/:45 burst; total runner minutes do not change.
- **Risk:** low for step 1, medium for step 2 (drift audit and template parity).

Finding check: new — no stagger or offset verdict in the processing report. Plan item 8 (`docs/plans/reduce-actions-run-volume-plan.md:212`) only moved the cadence from `*/5` to `*/15`. Runner-queue *logging* was rejected (report line 1853), and that is not what this item proposes.

**S2. Reuse the Semble index between runs (micro-optimization).**
- **Evidence:**
  - review_autofix had 15 bootstraps totalling 860,531 ms, about 57 s each, and each one served a single query (`semble_query_calls=15`, 169,713 B).
  - Run 46170805507 logged `SEMBLE_INSTALL_MS: 7619`, so the remaining ~50 s of each bootstrap is index build.
- **Root cause:** lazy bootstrap builds the index from scratch on every run. `scripts/semble_helpers.sh:253-267` builds through `build_semble_wrapper.sh` into `_semble_index_path`, and no cross-run cache was found.
- **Exact change:** cache the built index with `actions/cache` keyed on the reviewed tree SHA, and keep the current fail-open path on a miss.
- **Estimated savings:** about 45–50 s per review run when the cache hits (about 12 minutes across this window's 15 bootstraps). That is under 5% of a typical 1,134 s (p50) review run.
- **Risk:** low–medium. It needs the §24 size check on `review_autofix.yml`, and a stale index is possible if the cache key is wrong.

Finding check: new — the processing report records "broader Semble/bootstrap reuse work has not [landed]" (line 359, deferred), and no index cache exists in `semble_helpers.sh` on this ref.

## Cost Optimizations

**C1. Carry Claude usage into the per-run and aggregate cost telemetry.**
- **Evidence:**
  - Every `cost_telemetry` row in this window lacks `claude_*` keys.
  - Resolver runs of 1,788–4,010 s show `codex_calls=0`, `or_calls=0` and `codex_tokens_used=0` (for example 46152599654).
  - Recent PRs carry `ai:engine-claude` ("Opus 5.5, high effort"; 46173598382, 46172513334), and pollers run `AI_ENGINE_FALLBACK_POLICY: capacity`.
- **Root cause:** `parse_log` already extracts Claude usage (`scripts/cost_audit.py:783`). The keys are dropped because `RUN_COST_TELEMETRY_FIELDS` (line 209) and `AGGREGATABLE_COST_FIELDS` (line 246) omit `CLAUDE_COUNT_KEYS` (line 128) and `claude_cost_usd`.
- **Exact change:** additive.
  ```python
  RUN_COST_TELEMETRY_FIELDS = (... existing ..., *CLAUDE_COUNT_KEYS, "claude_cost_usd")
  AGGREGATABLE_COST_FIELDS = (... existing ..., *CLAUDE_COUNT_KEYS)
  # aggregate_run_cost_telemetry: sum claude_cost_usd as float separately (_to_int would truncate it)
  ```
  Cached rows without these keys read as 0 through `_to_int(None, 0)`.
- **Estimated savings:** none directly. It makes the cost of the 4.8 resolver-failure hours measurable and lets Claude vs OpenRouter spend be compared.
- **Quality risk:** none; this is reporting only.

Finding check: new — no `claude_*` telemetry verdict appears in the processing report (searched `claude_calls|claude_cost|claude usage`), and the omission is present at `scripts/cost_audit.py:209/246/611`.

**Model, cache and context evaluation (no separate recommendation).**
- **OpenRouter usage:** 31,374,089 tokens over 43 calls, all in 5 review runs. Uncached prompt 3,779,620; cache read 25,087,842; cache write 2,057,526; completion 449,537. The computed aggregate hit rate, cache read ÷ (read + uncached prompt + write), is **81.1%**. Per run: 46163541815 0.890, 46154557812 0.870, 46170934504 0.764, 46147370519 0.665 (PR 10246, round 4: 396,165 write vs 2,125,696 read).
- **Prompt growth:** `context_budget_warn_count=0` and `break_glass_count=0` everywhere, so there are no prompt-size pressure signals.
- **Semble:**
  - Not noisy. 17 queries returned 178,081 B (about 10.5 KB each) from 69 sources, with `static_dup_bytes=0` and `echo_lines_dropped=0`.
  - The PR 10066 resolver runs each fetched exactly 8,379 B from 1 source, so the same context was likely re-fetched on every retry (inference). That points back at R1, not at Semble.
- **Serena:** disabled (`SERENA_ENABLED: false` in 46170934504 and 46170902042). No queries, fallbacks or probes, so there is nothing to evaluate.
- **Avoidable reruns:** the resolver loop is the largest avoidable cost; see R1.

## Reliability Improvements

**R1. Make the resolver retry and cap state visible, so the next window shows why PR 10066 failed 5 times.**
- **Failure evidence:** 7 of 7 review_autofix failures (failure rate 4.2% of 165 runs) stopped at `Run Codex resolver, validate, stage, commit`. PR 10066 failed at 13:19, 14:02, 15:43, 16:16 and 17:08 (12,624 s in total); PR 9396 at 16:12 and 17:06 (4,739 s). Sweep 46173502866 later classed 10066 as `retryable`, with `CI_CANCELLED_RERUN … reason=other_conclusion final_ci=true`.
- **Root cause category:** retry-loop control. Two stops exist:
  - the identical-failure fingerprint cap, default 3 identical failures per head (`review_autofix.yml:797-798, 1921-2130`);
  - `AUTOFIX_RESOLVER_RETRY_STATE_V1`, which counts only same head and same failure signature (`scripts/review_conflict_resolve.sh:3708-3711`).

  Five failures past a cap of 3 means the head changed or the signature differed between attempts. That is an inference: the logs that would show it were not available. The only summary is the free-text `Persisted AUTOFIX_RESOLVER_RETRY_STATE_V1 to PR #N (count=…, signature=…, tier=…)` (line 4003). It has no head SHA and is never printed on the early-return paths at lines 3963, 3968, 3978, 3986 and 3993.
- **Exact fix:** additive. Keep the existing line and also print one stable line on every path:
  ```bash
  echo "RESOLVER_RETRY_STATE pr=${PR_NUMBER} head=${PR_HEAD_SHA:0:12} outcome=<persisted|skipped> reason=<ok|fp_verifier_missing|builder_failed|artifact_unavailable|empty_body|patch_failed> count=${_count:-na} signature=${_signature:0:12} tier=${_verification_tier:-na} escalated=<true|false>"
  ```
  Register the prefix with an `agents.d/` fragment (§25) and add a counter in `scripts/collect_workflow_logs.py`.
- **Expected impact:** next window can tell "head moved every attempt" (expected) apart from "the cap missed an identical loop" (a defect). If it is the defect, about 3 runner-hours a window are avoidable at the current rate.
- **Rollback / fail-open:** logging only; the commit, escalation and exit paths are unchanged.

Finding check: new — the prior verdict covers only the *primary failure reason* (`conflict_resolver_failed`, report line 1144) and the scope reason code (`RESOLVER_SCOPE_CHECK`, line 1828). Nothing covers retry or cap state visibility, and the free-text-only line is present at `scripts/review_conflict_resolve.sh:4003`.

**R2. Keep `install-claude` through the triage job's cleanup.**
- **Failure evidence:** run 46172150663 (Internal: AI Check Failure Triage, PR 10252) logged `##[error]Can't find 'action.yml'... under .codex-workflow-src/.github/actions/install-claude` during post-job cleanup. The run still concluded success.
- **Root cause category:** the step order breaks a post-action. `install-claude` (line 479) runs `actions/cache@v5`, whose post-step reloads the composite action at job end. The cleanup at lines 656-660 preserves only `claude-pool-token`, even though its own comment says it keeps the post-action directory.
- **Exact fix:**
  ```diff
  -            find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token -exec rm -rf {} +
  +            find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +
  ```
  Also update the comment at lines 653-655 to name both directories.
- **Expected impact:** removes one `##[error]` per Claude-engine triage run, which heal and triage parsers could otherwise misread, and lets the npm cache save on a miss.
- **Rollback / fail-open:** `install-claude` holds trusted support content only, and the sandbox snapshot never includes `.codex-workflow-src` (line 655). Revert is one token.

Finding check: new — no `install-claude` verdict in the processing report, and the single-directory keep list is present at `check_failure_triage.yml:660`. It is the only such cleanup in the repo.

**R3. Count unattributed issues when reviewer-findings attribution is unavailable.**
- **Failure evidence:** only 4 of 165 review runs carried `REVIEWER_FINDINGS` lines. Of those, 3 runs (13 lines) were `unavailable`:
  - PR 10269 round 0: `flagged_by_missing` × 5 reviewers (46154557812);
  - PR 10246 round 4: `flagged_by_missing` × 5 (46147370519);
  - PR 10250 round 2: `consolidator_missing` × 3 (46148287048, the same run with `or_usage_unavailable_calls=1`).
- **Root cause category:** all-or-nothing attribution. A single issue with an unmatched `flagged_by` entry discards the whole round (`scripts/review_findings_record.py:744-746`), and the line does not say how many issues failed.
- **Exact fix:** before returning at line 745, compute and append `unattributed=<n>` (issues where `not matched or has_unmatched`) to the unavailable line, together with the total issue count.
- **Expected impact:** separates a single stray `reviewer_N` from wholesale attribution loss in the consolidator. Today 2 of 4 instrumented runs give no lone/shared data.
- **Rollback / fail-open:** additive field; the collector regex must accept trailing keys.

Finding check: new — no `REVIEWER_FINDINGS` / `flagged_by` verdict in the processing report, and the early return without counts is at `scripts/review_findings_record.py:745`.

**Semble, Serena, BREAK_GLASS and CONTEXT_BUDGET_WARN (no new recommendation).**
- 31 Semble fallbacks: 24 are synthetic contract-test fallbacks in CI (4 each in 46148101475, 46167441100, 46153686556, 46144877801, 46149424616 and 46162139761); 7 are runtime fallbacks, all in implement run 46150716059 (9,254 s, the longest run in the window).
- Semble had 0 bootstrap failures and 0 unused bootstraps.
- The runtime fallbacks are concentrated in one run: a localized fail-open, not a broken rollout. Their reasons are emitted (`SEMBLE_FALLBACK target= reason=`) but not visible without the logs.
- Serena had 0 probes and 0 fallbacks. `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` were both 0.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines appear in this window's evidence. Every `log_summary` that mentions it says none were visible, and several note truncation of 340k–1.3M characters, for example 46170805507 and 46171945420. Hit rate, `estimated_tokens` against budget, `keyword_method` mix, `fail_open` and `enabled:false` cannot be computed.
- `memory_maintenance` ran once and was cancelled after 1,932 s.

**M1. Confirm memory telemetry is emitted, using the full deep-dive logs of the next window.**
- **Exact change:** none to code. Grep `errors/` and `slow/` for `AI_MEMORY_TELEMETRY` in the review and implement runs when the `workflow-log-output` artifact is present.

Finding check: unverified — the run-logs directory was not mounted, so emission could not be checked. The processing report records the emitter as present (line 2282: "`AI_MEMORY_TELEMETRY` and `CONTEXT_BUDGET_WARN` are already emitted").

## GH API Call Audit

| Run / job | calls_in_job | paginated | PAT remaining at end | used_in_job* | PR read cache |
|---|---|---|---|---|---|
| 46170934504 review codex-agent | 52 | 18 | 2,430 | 1,180 | hits=1 misses=6 |
| 46170805507 review codex-agent | 36 | 5 | 2,802 | 966 | hits=1 misses=4 |
| 46170902042 deterministic-skip-merge | 20 | 3 | 3,606 | 37 | — |
| 46170888377 deterministic-skip-merge | 13 | 2 | 4,731 | unknown | — |
| 46173502866 review sweep | 45 (pat 38, gt 7) | 15 | 2,335 | 54 | — |
| 46170830076 review sweep | 60 (pat 55, gt 5) | 17 | 4,538 | 66 | — |
| 46172150560 / 46171606784 / 46171535411 heal intake | 11 / 27 / 24 | 5 / 5 / 3 | 4,657 / 4,681 / 4,717 | 58 / unknown / unknown | — |

\*`used_in_job` is the change in the shared PAT bucket and includes concurrent jobs (`scripts/collect_workflow_logs.py:1995-1996`). Rank jobs by `calls_in_job`. No HTTP 429 or secondary rate limit appears anywhere. The lowest remaining values (2,335 and 2,430 out of about 5,000) were seen at about 18:03 and during the 17:31 review: about half the PAT budget left in the late afternoon.

Other observations:
- `GH_LOOP_CALL_LINT warnings=1 files=5` on PR 10272 (46172513334) flags `GH_RETRY_MAX_ATTEMPTS="${jobs_attempts}" gh_retry gh api` inside a loop with no `# gh-loop-ok:` annotation. This is a PR-diff warning for that PR's reviewers, not a main-branch defect.
- pr-body-lint makes one paginated commits call per run (46173598382 and others). Minimal.

**G1. Break `GH_PR_READ_CACHE` down by kind.**
- **Evidence:** hits 1 / misses 6 (46170934504) and 1/4 (46170805507). The cache has exactly 6 kinds (`scripts/gh_helpers.sh:1620`), so misses=6 may simply be one unavoidable first fetch per kind rather than a cache failure. The summary cannot tell these apart from re-fetches after the 120 s TTL.
- **Exact change:** the `.stats` file already records `hit <kind>` / `miss <kind>` (line 1637). Append `by_kind=pull:1/1,issue_comments:0/1,…` to the end-of-job `GH_PR_READ_CACHE` line.
- **Estimated impact:** shows whether raising the TTL for `pull`/`check_runs` is worth considering. No call change.
- **Rate-limit risk:** none.

Finding check: new — no `GH_PR_READ_CACHE` verdict in the processing report, and the summary on this ref carries only totals (`scripts/gh_helpers.sh:1637-1639`).

**G2. Log when comment signing is disabled, once per job.**
- **Evidence:** 251 skipped `issue_comment` wrapper runs in bursts of four, for example 46170958346/379/471/351 at 17:31:47 and 46171557923/941/952/971 at 17:38:27. Plan item 15 removes these when `AI_COMMENT_SIGNING_SECRET` is set. No summary shows `AI_COMMENT_SIG outcome=active`, and the disabled case prints nothing (`scripts/gh_helpers.sh:51`). `GH_CALL_COUNTER` already prints `outcome=disabled` (line 1125).
- **Exact change:**
  ```bash
  if [ -z "${AI_COMMENT_SIGNING_SECRET:-}" ]; then
  	[ -n "${AI_COMMENT_SIG_DISABLED_LOGGED:-}" ] || { echo "AI_COMMENT_SIG outcome=disabled reason=secret_unset" >&2; export AI_COMMENT_SIG_DISABLED_LOGGED=1; }
  	return 0
  fi
  ```
- **Estimated impact:** shows whether item 15 is active. If it is not, enabling the secret could remove most of the roughly 251 comment-triggered skipped runs per window, plus their API and queue load.
- **Rate-limit risk:** none.

Finding check: new — no `AI_COMMENT_SIG` verdict in the processing report, and the silent `return 0` is at `scripts/gh_helpers.sh:51`. A single comment router remains a plan non-goal (`docs/plans/reduce-actions-run-volume-plan.md:43`) and is not proposed.

## Prompt Cache & Memory System

- The aggregate `cache_hit_rate` is null in the collector output. Computed from the OpenRouter totals it is 81.1%, and per run it ranges from 0.665 to 0.890 (see Cost).
- The weakest run, 46147370519 (PR 10246, round 4), wrote 396,165 cache tokens against 2,125,696 read. That is consistent with later rounds rebuilding more of the prompt (inference); there is no prefix-instability evidence without the logs.
- No `CONTEXT_BUDGET_WARN` lines appeared (`CONTEXT_BUDGET_WARN_RATIO: 0.7` is configured), so prompt growth is not eroding cache value in this window.
- Memory retrieval: no data (see AI Memory Health).
- No new cache recommendation passes the findings check. Prompt-layout and cache-breakpoint changes are recorded as kept without the broader rewrite (report line 864), and cache measurement-only proposals were rejected (line 2282). Claude-side cache tokens become visible once C1 lands.

## Orchestrator Health

- **Poller state:** internal orchestrate_poll had 160 runs (136 success, 24 cancelled). The cancelled runs show `cancelled_before_first_step` (for example 46172584438, 46171539742), which is the normal replacement of a pending run by its concurrency group. That was previously decided (report line 1284), so it is not raised.
- **Poller runtime:** internal poll 46171945420 ran its `poll` step for about 16.5 minutes. `ORCH_POLL_CYCLE_SUMMARY` exists for the breakdown, but it was not visible in truncated logs.
- **Clarification and planning flow:**
  - orchestrate_clarify_respond: 69 runs, 1 success, 68 skipped.
  - plan 46170959831 (issue 10273) was triggered by an `/answer [auto-answered-by-clarify]` comment and succeeded in 1,128 s, about 403 s of it queued.
  - No stuck clarify loops are visible.
- **Heal:**
  - WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL found 0 candidates in both sweeps.
  - Heal intakes waited 2–85 s on the intake ticket queue (`HEAL_INTAKE_QUEUE_WAITED_SECONDS`).
  - workflow_failure_heal: 120 runs, all skipped (they are triggered by `labeled` events).
- **Indicators to track:**
  - `RESOLVER_RETRY_STATE` counts per PR (after R1);
  - `identical_failure_cap` trips;
  - `AI_COMMENT_SIG outcome=` (after G2);
  - skipped-wrapper runs per comment.

**O1. Bring consumer pollers onto the current support ref.**
- **Evidence:**
  - All 7 consumer pollers ran `orchestrate_poll.yml@6fe11eeda300a383fdbf5634153e1777d3abc268` with `CODEX_VERSION: v0.114.0`.
  - Internal runs used `f9b3ff877842…`, and current `orchestrate_poll.yml:78` defaults to `v0.171.0`.
  - fbc_shutdown 46172298625 printed `Optional support script … not found in coding-workflows@6fe11eed… merge-train release will be skipped this tick` (emitted at `orchestrate_poll.yml:279`).
- **Root cause:** inferred to be consumer wrappers pinned to an older SHA, or a repo-level `CODEX_VERSION` var. Which one cannot be determined without the consumer files.
- **Exact change:** run the existing `update_workflows` sync for the consumers (its one run in this window was skipped), confirm with `scripts/audit_consumer_drift.py`, and check whether consumers set `vars.CODEX_VERSION`.
- **Expected impact:** merge-train release resumes in fbc_shutdown, and consumers pick up the current fixes.
- **Risk:** low; this is the existing release flow.

Finding check: new — the only consumer-drift verdict is the wrapper-audit coverage fix (report line 1327, applied). The 6fe11eed pin and the skipped merge-train notice are present in this window.

## Pipeline Flow Bottlenecks

| Stage | Runs (success/fail/cancel/other) | p50 / p95 duration | Dominant overhead |
|---|---|---|---|
| clarify | 80 (10/0/0/70) | 1 s / 776 s | skipped fan-out |
| plan | 69 (11/0/0/58) | 1 s / 1,046 s | queue (46170959831 about 403 s) |
| implement | 67 (6/0/6/55) | 1 s / 2,672 s | compute (46150716059: 9,254 s, 7 Semble runtime fallbacks) |
| review_autofix | 165 (154/7/3/1) | 1,134 s / 4,457 s | resolver retries (about 4.8 h) and queue (review gates waited 256–559 s) |
| ci | 48 (39/4/5/0) | 1,082 s / 6,262 s | compute (full CI 4,700–7,750 s; covered by project #9528) |
| orchestrate_poll | 160 (136/0/24/0) | 454 s / 1,379 s | queue (consumer polls waited 400–640 s of 500–820 s) |
| integration_pr_readiness | 39 (0/0/0/39) | 1 s / 10 s | skipped |

Overhead in order of end-to-end impact:
1. **Retry:** the resolver loop on PR 10066 and PR 9396 (R1).
2. **Queueing:** the runner pool; consumer polls spend 60–90% of their wall time waiting (S1).
3. **Compute:** full CI, already tracked by `docs/plans/cut-ci-runner-time-plan.md` / #9528; not re-raised.
4. **Merge/conflict:** this is the resolver itself; same as item 1.

All 4 CI failures were template or contract-parity checks:
- `Prompt template parity (quick)` in 46143499824 and 46148104799;
- `Review/judge/conflict strict-render contract test` in 46148101475, created 3 s before 46148104799; both run `tests/test_assemble_prompt.py`;
- `Live .claude/ copies match their templates` in 46167441100 (PR 10252), whose repair was taken over by final-CI repair (`CHECK_TRIAGE skip reason=final_ci_repair_owns`).

Without the logs it is unclear whether these were PR-branch or main commits, so no change is proposed. Moving strict-render earlier was previously rejected (report line 1563).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Bottlenecks:** runner queue; review_autofix p95 4,457 s; full CI 4,700–7,750 s.
- **Failure modes:** resolver failures (7, on PRs 10066 and 9396); CI parity tests (4).
- **Cost drivers:** OpenRouter 31.4M tokens over 5 review runs; Claude spend not measured.
- **Actions:**
  1. Add `RESOLVER_RETRY_STATE` (R1).
     Finding check: new — free-text-only line at `scripts/review_conflict_resolve.sh:4003`; prior verdicts cover only reason codes (lines 1144, 1828).
  2. Add Claude keys to cost telemetry (C1).
     Finding check: new — keys missing at `scripts/cost_audit.py:209/246`; no prior verdict.
  3. Keep `install-claude` through the triage cleanup (R2).
     Finding check: new — `check_failure_triage.yml:660`; no prior verdict.

### shubhodeep1/tele-funtoken-msg-scoring
- 59 runs (54 success, 1 cancelled, 4 other), 0 failures, p50 492 s.
- **Bottlenecks:** poller queue (46172349158 and 46171025096); conditional deploy monitor 46171576405 queued 286 s and logged a `Node.js 20 is deprecated` warning.
- **Cost driver:** AI Review 46152246187 (2,367 s, no token lines).
- **Actions:**
  1. Refresh the poller pin (O1).
     Finding check: new — `@6fe11eed` with `CODEX_VERSION v0.114.0` in 46172349158; no prior verdict.
  2. Poller cron offset (S1).
     Finding check: new — `*/15` at `workflow-templates/ai-orchestrate-poll.yml:6`; no prior verdict.
  3. Claude cost visibility (C1).
     Finding check: new — same evidence as above.

### shubhodeep1/fun-token-multi-chain
- 37 runs, all success, p50 287 s.
- **Bottleneck:** poller queue (443 s of 696 s in 46172472608; 435 s of 669 s in 46171311296).
- **Actions:**
  1. O1. Finding check: new — `@6fe11eed` in 46171311296.
  2. S1. Finding check: new — `*/15` at template line 6.
  3. C1. Finding check: new — `scripts/cost_audit.py:209`.

### shubhodeep1/binance-blessings
- 29 runs, all success, p50 275 s.
- **Bottleneck:** poller queue (about 496 s of 757 s in 46171063486).
- **Actions:**
  1. O1. Finding check: new — `@6fe11eed` in 46171063486.
  2. S1. Finding check: new — `*/15` at template line 6.
  3. C1. Finding check: new — `scripts/cost_audit.py:209`.

### shubhodeep1/mongo-explorer
- 29 runs, all success, p50 220 s.
- **Bottleneck:** poller queue (498 s of 604 s in 46172566316; 310 s of 413 s in 46171346863).
- **Cost driver:** AI Review 46144562469 (2,389 s).
- **Actions:**
  1. O1. Finding check: new — `@6fe11eed` in 46172566316.
  2. S1. Finding check: new — `*/15` at template line 6.
  3. C1. Finding check: new — `scripts/cost_audit.py:209`.

### shubhodeep1/multi-user-ai-agent
- 29 runs, all success, p50 239 s.
- **Bottleneck:** poller queue (636 s of 823 s in 46172505450; 534 s of 693 s in 46171286510).
- **Actions:**
  1. S1. Finding check: new — `*/15` at template line 6.
  2. O1. Finding check: new — `@6fe11eed` in 46172505450.
  3. C1. Finding check: new — `scripts/cost_audit.py:209`.

### shubhodeep1/fbc_shutdown
- 29 runs, all success, p50 301 s.
- **Bottleneck:** poller queue (406 s of 510 s in 46171006979).
- **Failure mode:** merge-train release skipped every tick (46172298625).
- **Actions:**
  1. O1 (highest priority here). Finding check: new — the optional-helper notice fired in 46172298625.
  2. S1. Finding check: new — `*/15` at template line 6.
  3. C1. Finding check: new — `scripts/cost_audit.py:209`.

### shubhodeep1/bitsafe.io
- 29 runs, all success, p50 300 s.
- **Bottleneck:** poller queue (468 s of 637 s in 46171133632).
- **Cost driver:** AI Review 46144723495 (2,281 s).
- **Actions:**
  1. O1. Finding check: new — `@6fe11eed` in 46171133632.
  2. S1. Finding check: new — `*/15` at template line 6.
  3. C1. Finding check: new — `scripts/cost_audit.py:209`.

## Metrics Appendix

**Run outcomes**

| Scope | Runs | Success | Failure | Cancelled | Other (mostly skipped) | Failure rate (excl. other) | p50 / p95 |
|---|---|---|---|---|---|---|---|
| All | 1,058 | 544 | 11 | 39 | 464 | 1.85% | 93 s / 2,797 s |
| coding-workflows | 817 | 308 | 11 | 38 | 460 | 3.1% | 9 s / 3,317 s |
| 7 consumers combined | 241 | 236 | 0 | 1 | 4 | 0% | 220–492 s p50 |

**Additive review telemetry:** `cache_hit_rate` null (computed OpenRouter rate 81.1%); `wall_clock_p50_ms` 171,000; `wall_clock_p99_ms` 8,070,060 (118 samples); `break_glass_count` 0; `context_budget_warn_count` 0.

**Tokens (OpenRouter):** prompt 3,779,620; completion 449,537; cache write 2,057,526; cache read 25,087,842; total 31,374,089; 43 calls (42 with usage, 1 without). Codex 0. Claude not exported (C1).

**Semble:** 17 queries; 178,081 B (about 10,475 B per query); 69 sources; static-dup 0; echo-dropped 0; 31 fallbacks (24 contract-test in `ci`, 7 runtime in `implement` 46150716059); 17 bootstraps (0 failed, 0 unused); 981,987 ms in total (about 57.8 s each).

**Serena:** queries 0, response bytes 0, tool calls 0, fallbacks 0.

**MCP availability by family** (per-target rows unavailable; no `semble_targets` / `serena_targets` in the context)

| Family | Semble bootstraps ok/failed | Serena probe_ok / probe_failed / probe_skipped |
|---|---|---|
| review_autofix | 15 / 0 | 0 / 0 / 0 |
| orchestrate_poll | 2 / 0 (consumers `SEMBLE_AVAILABLE=false`, lazy) | 0 / 0 / 0 |
| implement | 0 / 0 (7 runtime fallbacks) | 0 / 0 / 0 |

Other MCP servers observed: none.

**Reviewer findings:** 165 review runs, 4 with lines, 3 runs with unavailable attribution (13 lines). Counted run 46163541815 (PR 10272):

| Reviewer | Raised | Lone | Lone accepted | Lone rejected | Shared |
|---|---|---|---|---|---|
| anthropic/claude-sonnet-5.5 | 6 | 2 | 2 | 0 | 4 |
| minimax/minimax-m3 | 4 | 1 | 0 | 1 | 3 |
| z-ai/glm-5.3-flash | 5 | 0 | 0 | 0 | 5 |
| google/gemini-3.1-flash-lite | 4 | 0 | 0 | 0 | 4 |
| openai/gpt-6-luna | 3 | 0 | 0 | 0 | 3 |

**GH API:** see the GH API Call Audit table. 0 rate-limit events; minimum PAT remaining observed 2,335.

**Findings check**

| Status | Count |
|---|---|
| new | 9 (S1, S2, C1, R1, R2, R3, G1, G2, O1) |
| recurring | 0 |
| reopened | 0 |
| unverified | 1 (M1) |
| dropped: already fixed on this ref | 3 (Semble contract-test/runtime split; `SEMBLE_FALLBACK` reason codes; `cancelled_before_first_step` classification) |
| dropped: previously decided | 4 (poller pre-step cancellations, line 1284; runner-queue logging, line 1853; strict-render earlier, line 1563; poller Semble availability, line 2208) |

**Data gaps**
- `/home/runner/work/_temp/workflow-log-output` was not mounted, so no `errors/`, `slow/` or `recent/` logs were read. Resolver failure reasons, `SEMBLE_FALLBACK` reasons and `AI_MEMORY_TELEMETRY` are therefore unknown.
- Log summaries were truncated by 89k–1.9M characters.
- `success_sample_rate` is 0.07.
- Claude usage is not exported (C1).
- The processing report was readable, so the findings check ran in full.

<!-- reviewer-findings-table v1 -->
## Reviewer Findings

Counts come from REVIEWER_FINDINGS lines in 4 of 165 review runs in the window; lines are read only from the capped sample of run logs this analysis downloaded, which favours failed and slow runs, so this is a sample, not every review run; 'lone' depends on the consolidator's merge of duplicate findings; runs reporting outcome=unavailable: 3.

Percentages (of lone findings) are shown only for reviewers with at least 20 lone findings in the window; below that, only the counts are shown.

| Reviewer | Runs | Findings raised | Lone | Lone rejected | Lone accepted |
|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | 1 | 6 | 2 | 0 | 2 |
| `google/gemini-3.1-flash-lite` | 1 | 4 | 0 | 0 | 0 |
| `minimax/minimax-m3` | 1 | 4 | 1 | 1 | 0 |
| `openai/gpt-6-luna` | 1 | 3 | 0 | 0 | 0 |
| `z-ai/glm-5.3-flash` | 1 | 5 | 0 | 0 | 0 |
