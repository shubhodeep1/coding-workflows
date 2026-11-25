<!-- workflow-log-analysis: window_start=2026-11-21T00:27:02Z window_end=2026-11-23T00:34:23Z run=45089289049 -->
## Executive Summary

**Coverage gaps.** Three repositories hit the 1,000-run page limit, so only part of their runs are listed: `shubhodeep1/tele-funtoken-msg-scoring`, `shubhodeep1/binance-blessings` and `shubhodeep1/coding-workflows` (truncated). No repository failed collection (`repositories_failed` is empty). The full run-logs folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted. All evidence below therefore comes from `analysis_context.json` (per-run rows, `cost_telemetry`, `log_summary`) plus a read of the current code. `insufficient_data` is false.

- **Consumer sync has been broken for at least two days, and the cause cannot be seen.** `AI Update Workflows` failed 11 of 13 runs (84.6%). Every failure stopped at `update / update-wrappers → Verify signed sync manifest`, across all 7 consumer repos, on two daily ticks (2026-11-21 ~04:01–04:05 and 2026-11-22 ~04:02–04:14). One `AI Workflow Integrity` run failed at the same step (44865020211).
  - The step logs a reason code (`::error::SYNC_MANIFEST outcome=rejected reason=…` in `.github/workflows/update_workflows.yml`). None of these runs was picked for a deep-dive.
  - The reason is that `select_notable_runs_for_logs` in `scripts/collect_workflow_logs.py` takes failures newest-first with no clustering.
  - Impact: high. Consumers keep running the reusable workflows at `6fe11eed…` while this repo's own runs use `promote-candidate`/`main`. Confidence: high on the pattern; the root cause is unknown.
- **78.5% of all failures (168 of 214) are startup failures of six external `tier1-*.yml` workflows** in `fbc_shutdown` and `mongo-explorer`: 0 s runs with no job.
  - The collector files `tier1-ci-doctor.yml` and `tier1-ci-coach.yml` under the `ci` family, because `"ci" in normalized` (line 616) still matches them. That inflates the `ci` failure rate to 45.0%; the real coding-workflows CI rate is about 4% (inference).
  - It also spends deep-dive slots on runs that have no log archive (four HTTP 404s).
  - Impact: high for report accuracy. Confidence: high.
- **`heal-pr-reconcile` clones full history on every PR close, then usually does nothing.** Run 45086931583 took 652 s: about 163 s in the queue, about 262 s before the reconcile step, and 221 s in the reconcile step, which ended in `noop reason=no_heal_issue`. The checkout in `.github/workflows/internal-cancel-on-pr-close.yml` uses `fetch-depth: 0`. Expected saving: about 4 min of runner time per merged or closed PR. Confidence: medium.
- **The Claude credential probe checks 13 accounts one after another.** It took about 26 s in run 45089115172 (the loop in `scripts/claude_pool_token.sh`, lines 254–272). Bounded parallel probing would save about 18 s per Claude-engine job. Confidence: medium.
- **Review/autofix wall-clock figures mix queue wait with compute.** Pairs of runs on the same PR queue behind each other in the `pr-autofix-<pr>` concurrency group (`cancel-in-progress: false`).
  - For pr:9950, the second run's 15,622 s is about 9,061 s of waiting plus about 6,950 s of its own work (inference from timestamps).
  - Splitting the two would stop speed rankings from chasing the wrong number. Confidence: medium.
- **The aggregate prompt-cache hit rate shows as null even though cache tokens were recorded.** `compute_cache_hit_rate` returns `None` when any call lacks usage data. One call in run 45063269354 nulls the repo, family and summary values. Recomputed by hand, the rate is 0.727 overall and 0.747 for coding-workflows. Confidence: high.

## Speed Optimizations

**1. Clone full history only when a heal PR actually needs reconciling** (critical path for runner slots on every PR close)
- **Evidence:** `shubhodeep1/coding-workflows`, run 45086931583, "Internal: Cancel on PR Close", job `heal-pr-reconcile`, 652 s total.
  - About 163 s queued ("All GitHub-hosted runners … are busy").
  - About 262 s (00:10:30 → 00:14:52) before the reconcile step. The `log_summary` notes that checkout "fetched a very large tag list" and that 686,339 characters of log were truncated.
  - The step ended in `WORKFLOW_HEAL_PR_RECONCILE noop reason=no_heal_issue pr=9954 merged=true`.
  - The `cancel_on_pr_close` family ran 364 times (p95 1,376 s).
- **Root cause:** `.github/workflows/internal-cancel-on-pr-close.yml` (lines 45–50) checks out `main` with `fetch-depth: 0`. But `scripts/workflow_failure_heal_pr_reconcile.sh` decides `no_heal_issue` (lines 99–108) from one REST listing, before its first git operation (the `git fetch` at line 162).
- **Exact change:**
  - Switch the checkout to `fetch-depth: 1`.
  - In the script, deepen the history only on the reconcile path, immediately before line 162:
  ```bash
  if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
  	git fetch --quiet --no-tags --unshallow origin >/dev/null 2>&1 \
  		|| { log "skip reason=unshallow_failed pr=${SOURCE_PR}"; exit 0; }
  fi
  ```
  - Also add `elapsed_ms=` to the `noop` log line, so the 221 s spent in the noop step can be attributed. The cause of that time is unknown; a slow paginated label listing is an inference.
- **Estimated saving:** about 4 min of runner time on each noop PR close, and a shorter hold on the shared runner pool (§26).
- **Risk:** low to medium. The reconcile path keeps full history through `--unshallow`, and the job already fails open (`continue-on-error: true`, and the script always exits 0).
- Finding check: new — no prior verdict covers this checkout. The 2026-09-22 "narrow the orchestrator checkout" rejection concerns the poll job, which reads origin refs broadly. This script does no git work on the noop path. Evidence: run 45086931583.

**2. Probe Claude pool accounts in parallel, with a bound** (critical path of every Claude-engine job)
- **Evidence:** run 45089115172 (`Unblock judge #9618`). The `Resolve Claude credential` step ran 00:31:44 → 00:32:10, about 26 s for `CLAUDE_POOL available=true reason=selected accounts=13`, at roughly 1.7–2 s per account.
- **Root cause:** in `scripts/claude_pool_token.sh`, the `for name in "${accounts[@]}"` loop (lines 254–272) runs one `timeout … claude -p` probe at a time.
- **Exact change:**
  - Run the probe subshells in the background, at most N at a time. Use a new default-carrying variable, for example `CLAUDE_POOL_PROBE_CONCURRENCY=4`, after confirming the name is unused.
  - Give each probe its own working directory and `HOME` (or CLI config directory) under `${work}`, keep the per-account `transcript` files, `wait`, and only then build `probes` in the original account order.
  - Keep `choose` and all fail-closed paths unchanged.
- **Estimated saving:** about 18 s per job (4 rounds of about 2 s instead of 13 probes in a row). The Haiku token cost does not change.
- **Risk:** medium. Concurrent CLI start-ups could write shared config, which per-probe `HOME` isolation mitigates. Rollback: set the concurrency to 1.
- Finding check: new — the processing report has no prior verdict on probe parallelism. Evidence: run 45089115172, `scripts/claude_pool_token.sh` lines 254–272.

**3. Fail inventory drift in CI quick as well as full CI** (critical path for the PR feedback loop)
- **Evidence:** "CI [pr:9936]" failed twice at `tests-release-and-log-analysis → Inventory parity`: run 45047544402 (run length 5,238 s) and run 45058718225 (2,702 s). CI quick (`static-checks`) finishes in 534–657 s (runs 45088237470 and 45088711709). When the parity step itself failed inside each run is not visible, because the logs are absent.
- **Root cause:** `tests/inventory_parity.py` runs only at line 2818 of `.github/workflows/ci.yml`, inside a job that `ci_pr_quick.yml` does not run.
- **Exact change:** add one extra step to the `static-checks` quick path that runs `PYTHONDONTWRITEBYTECODE=1 python3 tests/inventory_parity.py`, under a distinct name (for example "Quick inventory drift check"). Keep the existing step, its name and its position, so `tests/test_ci_inventory_parity_order_contract.py` stays green.
- **Estimated saving:** for drift-only failures, the signal arrives within about 10 min instead of after a full CI run (about 45–87 min of run time in this window).
- **Risk:** low. The step is additive and deterministic.
- Finding check: recurring — the prior verdict on "Inventory parity reconcile + run early" is `already-implemented`, and "move it to the earliest deterministic lint gate" was left deferred. This window still shows two drift failures on PR 9936 (45047544402, 45058718225).

Micro-optimizations, not ranked: each Semble bootstrap takes about 53 s (424,447 ms over 8 bootstraps for 9 queries in `review_autofix`). That is under 1% of a review run lasting about 2 h, so no change is recommended.

## Cost Optimizations

**1. Attribute cache reads and writes to review phases, so the low-hit runs can be targeted**
- **Evidence:** `review_autofix` produced every model token observed in this window. Totals: 21,510,581 tokens over 56 OpenRouter calls (55 with usage data, 1 without): 3,895,380 prompt, 477,407 completion, 15,286,520 cache-read and 1,851,907 cache-write tokens. Per-run cache hit rate ranges from 0.506 to 0.838:

  | Run | PR | Cache hit rate | Cache-write tokens | Uncached prompt tokens | Calls |
  |---|---|---|---|---|---|
  | 45062939721 | pr:9935 | 0.506 | 253,713 | 501,683 | 8 |
  | 45069755123 | pr:9954 | 0.694 | 279,576 | 481,779 | 8 |
  | 45069086206 | pr:9898 | 0.765 | 316,977 | 505,044 | 8 |
  | 45072160538 | — | 0.838 | 350,502 | 860,423 | 8 |

- **Root cause:** the per-call lines (`INFO: openrouter usage phase= call= … cache_creation_input_tokens= cache_read_input_tokens=` in `scripts/review_run_reviewers.sh`, lines 625–635) already carry cache fields. But `scripts/cost_audit.py` adds only prompt, completion, total and calls into `or_phases` (lines 766–769). `cw` and `cr` are parsed at lines 748–749 and then dropped at the phase level. So the report cannot show which phase (reviewer, aggregator or editor) loses the cache in run 45062939721.
- **Exact change:** add two keys at line 769, and check that the readers of `or_phases` (around lines 1166–1180) handle the extra keys:
  ```python
  out["or_phases"][phase]["cache_write_tokens"] += cw
  out["or_phases"][phase]["cache_read_tokens"] += cr
  ```
  Then pass `or_phases` through to the per-run `cost_telemetry` rows in the analysis context.
- **Estimated saving:** no direct saving. It enables a targeted fix for the worst-hit phase. Lifting the run at 0.506 to the 0.73 median would avoid roughly 230k uncached prompt tokens per such run (inference).
- **Quality risk:** none. The change is telemetry only and additive.
- Finding check: new — prior verdicts reject *automatic prompt reduction* and *deduplicating reviews* (2026-09-21-3 and 2026-10-07-4). This change does neither and only fills a gap in the parser. Evidence: `scripts/cost_audit.py` lines 748–769, run 45062939721.

**Semble and Serena assessment (no change recommended).**
- Semble ran 9 queries for 120,691 bytes (about 13.4 KB each), with 56 sources (about 6.2 per query), `static_dup_bytes=0` and 0 echo lines dropped.
- That is a small, non-duplicated contribution against about 3.9M uncached prompt tokens, so Semble is not adding noisy context.
- Serena logged 0 queries; it is disabled (`SERENA_ENABLED: false` in run 45088783628).
- The Claude probe's 13 Haiku calls per job are covered under Speed 2.

## Reliability Improvements

**1. Make the deep-dive selector cover every distinct failure cluster, and read annotations for clusters it skips** (highest impact: it unblocks the root cause of an 85% failure rate)
- **Evidence:** 11 `update_workflows` failures, all at `Verify signed sync manifest`:
  - binance-blessings: 44983701886
  - bitsafe.io: 44863999145
  - fun-token-multi-chain: 44864150429, 44983319064
  - fbc_shutdown: 44864223961, 44983361638
  - mongo-explorer: 44864326917, 44983479625
  - multi-user-ai-agent: 44864091702, 44983288310
  - tele-funtoken-msg-scoring: 44984134654

  Every one has `log_download_status: not_selected`. A burst of other consumer failures fell in the same window (2026-11-22 02:42–05:23 UTC: review gate, clarify, plan and respond failures), and every one of those is also `not_selected`. Correlation with a stable-release problem is an inference.
- **Root cause:** `select_notable_runs_for_logs` (`scripts/collect_workflow_logs.py`, lines 1336–1380) sorts failures by `created_at` descending and fills the cap. The newest coding-workflows failures crowd out older clusters, whatever their size.
- **Exact fix:**
  - (a) Before the newest-first fill, reserve one slot per `(repository, workflow_family, failure_point.step_name)` cluster, taking the newest run in each, within the existing `max_log_runs` cap.
  - (b) For failing runs in clusters that still miss the cap, fetch `GET repos/{repo}/check-runs/{job_id}/annotations` once per cluster. The job IDs already come from the jobs fetch (`jobs_fetch_status: success`). Copy the `::error::` text into the run row.

  The step already emits the reason code (`SYNC_MANIFEST outcome=rejected reason=<code>`), and annotations survive even without the log archive.
- **Expected impact:** the reason code is visible for every recurring failure cluster on the next analysis run, including the `asset_missing`, `attestation_failed` and `verifier_hash_mismatch` family in `update_workflows.yml`.
- **Rollback / fail-open:** if the annotations fetch fails, leave the row unchanged. That costs about 20–25 calls per collection (see GH API Call Audit).
- Finding check: new — the 2026-10-08 rejection of "restoring the full-log archive" concerns artifact state, not selection. No prior verdict covers cluster coverage. Evidence: the 11 runs above, plus `select_notable_runs_for_logs`.

**2. Stop filing `tier1-*` as `ci`, and skip deep-dives of runs that never started**
- **Evidence:** the four tier1-only families `tier1_mcp_inspector`, `tier1_observability_daily`, `tier1_portfolio_analyst` and `tier1_workflow_health_manager` have 28 runs each, every one a 0 s failure.
  - `tier1-ci-doctor.yml` and `tier1-ci-coach.yml` are counted under `ci`. That gives `ci` 59 failures out of 131 (45.0%). Excluding them leaves about 3 real failures out of about 75 runs (≈4%; inference, assuming 28 runs each like the other four).
  - Four fbc_shutdown tier1 runs that were selected (44988823888, 44988823201, 44988822499, 44988821673) returned `missing_log_archive (HTTP 404)`.
  - These workflows come from the external Netwask installer (`netwask/tier1-agent-suite.json`, branch `ai/netwask/tier1-agent-suite`). No `tier1-*.yml` exists in `workflow-templates/` or `.github/workflows/`.
- **Root cause:** `"ci" in normalized` (`scripts/collect_workflow_logs.py`, line 616) is a substring match. `_eligible_runs_for_log_selection` (lines 1301–1314) also accepts zero-duration runs that have no jobs.
- **Exact fix:**
  ```python
  if normalized.startswith("tier1_"):
      return normalized          # before the "ci" substring check
  ```
  In `_eligible_runs_for_log_selection`, also exclude rows where `duration_seconds == 0` and the failure point has no job.
  - Add cases for both to `tests/test_collect_workflow_logs.py`.
  - Fixing the six workflow files themselves is the Netwask owner's job. No change in this repo can fix them.
- **Expected impact:** the `ci` failure rate reads about 4% instead of 45%, and about 4 deep-dive slots per window are freed.
- **Rollback:** revert one line. Only the `tier1_*` families move.
- Finding check: new — `analysis/workflow-optimization-2026-11-21.md` (Reliability 2) raised this, but the processing report has no verdict for November docs. The match at line 616 is still present.

**3. Give "trusted support commit" failures a machine-readable reason**
- **Evidence:** four fail-closed stops in about 5 h, across three workflows:
  - `security-audit → Resolve trusted audit support commit`: run 45047411148 (PR #9944, 17:05) and run 45047494329 (PR #9950, 17:05)
  - `judge / unblock-judge → Resolve trusted unblock support commit`: run 45059113348 (19:09)
  - `review / gate → Resolve trusted review support commit`: run 45077522101 (22:24)
- **Root cause category:** unknown, because the logs are absent. Each path in `.github/workflows/security-audit.yml` (lines 187–229) prints a different free-text `::error::` ("identity is unapproved", "main branch is not confirmed protected", "stable tag is unavailable", …). Nothing can count them by reason.
- **Exact fix:**
  - Before each `exit 1`, emit `SUPPORT_TRUST outcome=fail workflow=<security_audit|unblock_judge|review_autofix> reason=<identity_unapproved|main_unprotected|stable_tag_unavailable|annotated_tag_unavailable|tag_not_commit|sha_unresolved>`.
  - Keep the existing messages and the exits.
  - Register the prefix through an `agents.d/<issue>-support-trust-prefix.md` fragment (§25).
- **Expected impact:** it tells transient API failures apart from real trust rejections, together with Reliability 1(b).
- **Fail-open:** none needed. The step stays fail-closed (§6).
- Finding check: recurring — the prior verdict (2026-10-01-2, the `review_autofix.yml` trusted support commit retry) is `already-implemented` through `gh_api_retry`. This window still shows 4 failures across 3 workflows.

**Fallbacks, `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` (no new item).**
- `break_glass_count=0` and `context_budget_warn_count=0` everywhere.
- 12 `SEMBLE_FALLBACK` events are contract-test fixtures, 4 per CI run (45047544402, 45082781842, 45058718225). That is healthy and already classified separately.
- 4 runtime Semble fallbacks occurred in tele-funtoken `AI Implement` run 45087147825. That is a single run that failed open and still succeeded in 945 s, so it is not a masked broken rollout.
- `SERENA_PROBE`: none.
- Qodo PR-Agent failed 7 of 47 runs in fbc_shutdown (for example 44989226051 and 44993338987). It is a third-party action, its logs are absent, and no recommendation is made.

## AI Memory Health

- **Telemetry found:** one `AI_MEMORY_TELEMETRY` line (from `log_summary`, run 45087147825, tele-funtoken `AI Implement`): `{"op":"record-run-event","event_type":"phase_completed","ok":true,"did_push":true,"push_attempts":1,"push_retry_reasons":[],"clone_ms":12631,"write_ms":567,"push_ms":1757,"workflow":"implement"}`.
- **`retrieve`:** no lines were visible, so the hit rate, the `estimated_tokens` against budget and the `keyword_method` mix cannot be computed. No `fail_open: true`, `enabled: false` or push retries were seen.

**1. Record which clone path produced `clone_ms`**
- **Evidence:** `clone_ms=12631` is the largest timed operation in run 45087147825. That is 7× the push time.
- **Root cause:** `scripts/ai_memory_lib.py` (lines 3247–3284) tries a shallow single-branch clone and silently falls back to a full repository clone (`_full_repo_clone_for_memory_branch`). Diagnostics are off by default (`AI_MEMORY_CLONE_DIAGNOSTICS`). The telemetry cannot show which path ran.
- **Exact change:** return the path taken (`shallow` or `full_fallback`) alongside the clone directory, and add `"clone_path": …` to the record next to `clone_ms` (lines 3489–3546). The change is additive, and the fail-open behaviour is unchanged.
- **Expected impact:** if `full_fallback` dominates, fixing the shallow path saves about 10 s per memory write. This is an inference from a single sample.
- Finding check: new — 2026-10-08 rejected AI Memory Health items as measurement-only "with no defect identified". This item rests on a measured 12.6 s outlier, and no prior verdict covers clone-path attribution.

## GH API Call Audit

**Observations, all from `log_summary`:**
- `GH_PAT_BUDGET phase=end workflow=review_autofix_sweep job=sweep remaining=1298 reset=1795393112 used_in_job=71` (run 45086321799, about 00:02 UTC). That is about 74% of the hourly budget gone about 16 min before reset (00:18:32 UTC).
- After the reset, run 45088783628 reported `remaining=4974`.
- No HTTP 429 or secondary-limit events in any visible log.
- `pr-body-lint` makes one paginated commits call (runs 45089508629 and 45089290042).
- `workflow_integrity` makes one status POST.

**1. Silence the expected "tag absent" 404 in the promote-candidate follower**
- **Evidence:** run 45086932105 logs `GH_API_RETRY outcome=permanent kind=permanent bucket=core status=404 attempt=1/5 … endpoint=repos/shubhodeep1/coding-workflows/git/ref/tags/promote-cycle-frozen` plus `##[warning] stderr: gh: Not Found (HTTP 404)`. That happens on every run of the 15-run `promote_candidate_follow` family, while the cycle is not frozen.
- **Root cause:** `scripts/promote_candidate_follow.sh` treats 404 as "not frozen" (`follow_is_not_found`, lines 116–119, used after the probe at line 168). But `gh_api_retry` in `scripts/gh_helpers.sh` always prints `::warning:: stderr:` for permanent failures, and `--optional` only short-circuits on breaker state (lines 1572–1573).
- **Exact change:** add an opt-in, default-off flag to `gh_api_retry` (for example `--expect-404`, after confirming no clash), which logs a 404 as a `notice` with no warning annotation. Pass it only at line 168.
- **Estimated reduction:** no change in call count. It removes about 15 false warnings per window, which keeps heal and triage warning scans clean.
- **Risk:** low. Default behaviour is unchanged.
- Finding check: new — the processing report has no prior verdict on this probe. Evidence: run 45086932105.

**Cross-reference.** Reliability 1(b) adds about 20–25 annotation calls per collection, one per failure cluster not deep-dived, not one per run. It runs in the collector, outside the orchestrator hot paths, and falls back silently if the calls fail (§14). Calls checked first: the existing jobs fetch returns job IDs but no annotation text, so it cannot provide the reason codes.

## Prompt Cache & Memory System

**1. Publish a cache hit rate over usage-available calls instead of nulling the aggregate**
- **Evidence:** the summary, `coding-workflows` and `review_autofix` all show `cache_hit_rate: null`, despite 15,286,520 cache-read tokens. The single cause is `or_usage_unavailable_calls=1` (run 45063269354, pr:9936, 7 of 8 calls with usage data).
  - Recomputed as cache_read ÷ (prompt + cache_read + cache_write): 0.727 overall and 0.747 for coding-workflows.
  - binance-blessings (0.643) is reported because it had no unavailable call.
  - Per-run rates of 0.506–0.838 show the cache is fragmenting (inference: the variable part of the prompt sits near the front in the lowest-hit runs; Cost 1 adds the per-phase data needed to confirm).
- **Root cause:** `compute_cache_hit_rate` (`scripts/cost_audit.py`, lines 454–456) returns `None` if any call lacks usage data. The aggregate is computed in `aggregate_run_cost_telemetry` (line 641).
- **Exact change:** keep `cache_hit_rate` exactly as it is, and add `cache_hit_rate_usage_available` (with coverage `or_usage_available_calls / or_calls`), computed from the same token sums. Calls without usage data contribute zero tokens, so the partial rate is unbiased over the calls that were measured.
- **Expected impact:** the cache trend stays visible in every window. No token or latency change.
- `CONTEXT_BUDGET_WARN`: 0. Prompt growth is not eroding cache value in this window.
- Finding check: recurring — "aggregate `cache_hit_rate`" was recorded `already-implemented` (2026-09-16, 2026-09-21 and others). This window still shows a null aggregate at summary, repo and family level.

## Orchestrator Health

**Indicators:**

| Area | Observation |
|---|---|
| `orchestrate_poll` | 1,184 runs, 0 failures, 80 cancelled (superseded); p50 410 s, p95 1,775 s |
| Clarify → plan auto-answer | Worked for issue #9972: clarify-respond 45086162643 (418 s) → `/answer [auto-answered-by-orchestrator]` → plan 45086625947 (967 s, success) |
| Unblock judge #9618 | `stop=blocked op=triage_resolution outcome=judge status=unverified`, then `triage_superseded … already_noted` (run 45089115172) |
| Consumer failures, 2026-11-22 02:42–05:23 | clarify `Run Codex` 44978624486, 44983263203; `Resolve integration ref` 44983077853, 44987722984, 44987239630 (plus tele-funtoken 44938957988 on 11-21); `Check orchestrator metadata` 44978804858, 44983305679 |
| Version pin | Consumer pollers run `orchestrate_poll.yml@6fe11eed…` (runs 45089109862, 45087295715, 45086805409, 45086384121). Because `update_workflows` is failing (Reliability 1), consumers cannot advance. |

The root causes of the consumer failures are unknown, because none was deep-dived. Reliability 1 is the mitigation.

**1. Make the missing approval-signing credentials visible**
- **Evidence:** tele-funtoken `AI Plan` run 45086420857, job `sign-automated-approval`: about 100 s waiting for a runner, then `Signed auto-approval skipped: required credentials are unset`. `APPROVAL_SECRET` is empty while `AI_PIPELINE_APPROVER_LOGIN` is set.
- **Root cause:** `workflow-templates/ai-plan.yml` (line 159) uses `print`, so the configuration gap is silent. No script in this repo provisions `AI_PIPELINE_APPROVAL_SECRET` (a grep of `scripts/` and `.github/workflows/` finds nothing). The job-level `if` (lines 79–83) cannot test a secret, so the runner slot is spent anyway.
- **Exact change:** change the line to `print("::warning::PLAN_APPROVAL_SIGN outcome=skip reason=credentials_unset")`, registering the new prefix through `agents.d/` (§25). The behaviour stays the same: exit 0.
- **Expected impact:** clarify → plan → implement can no longer silently skip signing. This reaches consumers only once `update_workflows` recovers.
- Finding check: new — the processing report has no prior verdict. Evidence: run 45086420857, `workflow-templates/ai-plan.yml` line 159.

## Pipeline Flow Bottlenecks

**Queueing (dominant for short jobs).** Twenty-six `log_summary` samples show "All GitHub-hosted runners … are busy". Waits range from 2 s to 272 s, with a median of about 64 s. Worst cases:
- 45086635577, Security Audit: 272 s (`ubuntu-24.04`)
- 45088405029, integrity: 228 s of a 244 s run
- 45088404509, ruff gate: about 195 s of 209 s
- 45086931674, Issue-PR status: 177 s
- 45086931583, heal-pr-reconcile: 163 s

**Compute.**
- `review_autofix`: p50 932 s, p95 7,023 s.
- `ci`: p50 534 s, p95 5,348 s, inflated by the tier1 0 s runs (Reliability 2).
- `implement`: 945 s in tele-funtoken run 45087147825.
- `plan`: 967 s in run 45086625947.

**Retry and merge overhead.** No retries (`run_attempt=1` everywhere). The 80 cancelled poller runs are healthy supersession.

**1. Report queue-in-group time separately from compute for review/autofix runs**
- **Evidence:** the slowest review runs come in same-PR pairs:

  | PR | First run (start, length) | Second run (start, length) | Second run's own work |
  |---|---|---|---|
  | pr:9950 | 45061330595 (19:32:30, 9,061 s) | 45061953765 (19:39:02, 15,622 s) | about 6,950 s |
  | pr:9953 | 45055712603 (18:32:58, 6,648 s) | 45055879736 (18:34:44, 12,654 s) | about 6,110 s |
  | pr:9947 | 45058638924 (19:04:26, 6,398 s) | 45059103802 (19:09:07, 12,196 s) | — |
  | pr:9723 | 45075120905 (21:59:26, 8,853 s, success) | 45075149278 (21:59:45, cancelled at 8,788 s) | — |

  The "own work" column subtracts the time until the first run ended (inference from timestamps).
- **Root cause:** the `pr-autofix-<pr>` concurrency group in `.github/workflows/review_autofix.yml` (lines 2059–2060) queues new runs behind a running peer (`cancel-in-progress: false`, by design). Run duration and `wall_clock_p99_ms` (15,294,580 ms for `review_autofix`) therefore include the wait in the group.
- **Exact change:** for slow runs, have the collector record `queue_in_group_s = autofix_job.started_at − run.created_at` from the jobs API. That costs one jobs call per slow run already selected, and the field is additive. Speed rankings should then use the compute portion. The concurrency semantics stay as they are.
- **Expected impact:** this corrects the biggest speed metric, which is about 2× too high for paired runs, and shows that a single review run takes about 1.7–1.9 h of real compute.
- Finding check: reopened — "queue-time separation" was rejected on 2026-10-08 as measurement-only "with no defect identified". This window shows a concrete defect in the metric: paired runs double-count the wait in the group (pr:9950 and pr:9953 above).

The fixes are ordered by end-to-end impact: Reliability 1 (restores consumer delivery), then Speed 1 and Speed 2 (runner-slot pressure on the critical path), then Pipeline 1 (correct targeting), then Speed 3.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
Truncated at 1,000 runs. 442 successes, 7 failures, 29 cancelled, 522 other (skipped). p50 10 s, p95 6,864 s.
- **Bottlenecks:** review/autofix runs of about 2 h each, plus waits in the concurrency group; runner queueing.
- **Failures:** trusted-support-commit fail-closed stops (×4); `Inventory parity` drift on PR 9936 (×2); `Alert inbox capture and store tests` (45082781842).
- **Cost:** 17.3M tokens; cache hit 0.747 recomputed (reported as null).

Top actions:
1. Clone full history only on the heal reconcile path (Speed 1).
   Finding check: new — run 45086931583; `internal-cancel-on-pr-close.yml` `fetch-depth: 0`.
2. Run Claude pool probes in parallel, with a bound (Speed 2).
   Finding check: new — run 45089115172; `claude_pool_token.sh` lines 254–272.
3. Add the `SUPPORT_TRUST` reason prefix (Reliability 3).
   Finding check: recurring — prior `already-implemented` (gh_api_retry); 4 failures in this window.

### shubhodeep1/binance-blessings
Truncated at 1,000 runs. 384 successes, 9 failures, 32 cancelled, 575 other. p95 1,383 s.
- **Failures:** review gate (44985410047, 44982801165, 44983158668); `Resolve integration ref` (44983077853, 44987722984, 44987239630); `Run Codex` (44983263203); `Verify signed sync manifest` (44983701886).
- **Cost:** 4.2M tokens; cache hit 0.643.

Top actions:
1. Cluster-aware deep-dive selection plus annotations (Reliability 1).
   Finding check: new — all 9 failures here are `not_selected`.
2. Per-phase cache attribution to explain the 0.643 hit rate (Cost 1).
   Finding check: new — `cost_audit.py` lines 766–769 drop `cw`/`cr` at phase level.
3. Usage-available hit rate (Prompt Cache 1).
   Finding check: recurring — prior `already-implemented`; aggregate still null in this window.

### shubhodeep1/tele-funtoken-msg-scoring
Truncated at 1,000 runs. 500 successes, 6 failures, 484 other.
- **Failures:** review gate (44987240995, 44987314598, 44987222953); sync manifest (44984134654); `Resolve integration ref` (44938957988); auto-heal provenance (45001500557).
- **Notes:** 4 runtime Semble fallbacks in 45087147825; memory clone 12.6 s; approval signing credentials missing (45086420857).

Top actions:
1. Make the approval-signing gap visible (Orchestrator 1).
   Finding check: new — run 45086420857.
2. Record `clone_path` in memory telemetry (AI Memory 1).
   Finding check: new — `clone_ms=12631` in 45087147825.
3. Cluster-aware selection (Reliability 1).
   Finding check: new — sync-manifest and review-gate failures not selected.

### shubhodeep1/fbc_shutdown
652 runs, 89 failures (13.7%), mostly tier1 startup failures (inference: about 84 of them).
- **Failures:** Qodo 7 of 47; `Run Codex` (44978624486); `Check orchestrator metadata` (44978804858); sync manifest (44864223961, 44983361638).

Top actions:
1. Re-file `tier1-*` runs and skip zero-duration runs when picking deep-dives (Reliability 2).
   Finding check: new — `"ci" in normalized` line 616; four 404 archives.
2. Cluster-aware selection plus annotations (Reliability 1).
   Finding check: new — sync-manifest failures 44864223961 and 44983361638 not selected.
3. Silence the expected 404 warning (GH API 1). This one is shared, not repo-specific.
   Finding check: new — run 45086932105.

### shubhodeep1/mongo-explorer
549 runs, 95 failures (17.3%), mostly tier1 bursts (6 workflows per push at 04:02, 04:19, 04:23, 04:25, 04:27 and 04:37 on 11-22).
- **Other failures:** review gate (44978659458, 44978618297, 44983252075); sync manifest (44864326917, 44983479625).

Top actions:
1. Reliability 2 (tier1 family and eligibility).
   Finding check: new — tier1 runs filed under `ci`.
2. Reliability 1.
   Finding check: new — sync-manifest failures not selected.
3. Pipeline 1 (split group wait from compute). This one is shared.
   Finding check: reopened — paired review runs double-count the wait in the group.

### shubhodeep1/fun-token-multi-chain
530 runs, 4 failures: sync manifest (44864150429, 44983319064), integrity (44865020211), review gate (44987528076).

Top actions:
1. Reliability 1.
   Finding check: new — integrity and update failures at the same verify step, none deep-dived.
2. Reliability 2. This one is shared.
   Finding check: new — `ci` family rate inflated.
3. Prompt Cache 1. This one is shared.
   Finding check: recurring — aggregate null.

### shubhodeep1/multi-user-ai-agent
333 runs, 3 failures: sync manifest (44864091702, 44983288310); security audit `Resolve audit target` (45001314994).

Top actions:
1. Reliability 1.
   Finding check: new — both sync-manifest failures not selected.
2. Reliability 3, extended to the `Resolve audit target` exits.
   Finding check: recurring — prior `already-implemented` (gh_api_retry); trusted-path failures persist.
3. Speed 2. This one is shared.
   Finding check: new — sequential probe.

### shubhodeep1/bitsafe.io
286 runs, 1 failure: sync manifest 44863999145. No telemetry was parsed.

Top actions:
1. Reliability 1.
   Finding check: new — run 44863999145 not selected.
2. Reliability 2. This one is shared.
   Finding check: new.
3. Pipeline 1. This one is shared.
   Finding check: reopened — same evidence as above.

## Metrics Appendix

**Runs**

| Scope | Runs | Success | Failure | Cancelled | Other | Failure rate | p50 (s) | p95 (s) |
|---|---|---|---|---|---|---|---|---|
| All | 5,350 | 2,983 | 214 | 119 | 2,034 | 4.0% (6.5% excluding other) | 89 | 2,155 |
| coding-workflows | 1,000 | 442 | 7 | 29 | 522 | 0.7% | 10 | 6,864 |
| binance-blessings | 1,000 | 384 | 9 | 32 | 575 | 0.9% | 9 | 1,383 |
| tele-funtoken-msg-scoring | 1,000 | 500 | 6 | 10 | 484 | 0.6% | 16 | 1,356 |
| fbc_shutdown | 652 | 386 | 89 | 8 | 169 | 13.7% | 105 | 1,149 |
| mongo-explorer | 549 | 344 | 95 | 7 | 103 | 17.3% | 107 | 1,160 |
| fun-token-multi-chain | 530 | 377 | 4 | 16 | 133 | 0.8% | 240 | 1,550 |
| multi-user-ai-agent | 333 | 275 | 3 | 7 | 48 | 0.9% | 214 | 1,628 |
| bitsafe.io | 286 | 275 | 1 | 10 | 0 | 0.3% | 262 | 1,817 |

Families with notable failure rates: `tier1_*` ×4 at 100% (28 each); `update_workflows` 84.6% (11/13); `ci` 45.0% (59/131, includes tier1-ci-*); `qodo_pr_review` 14.9%; `security_audit` 7.3%; `review_autofix` 1.7% (11/662).

**Tokens, cache and wall clock**

| Metric | Value |
|---|---|
| `or_calls` (usage available / unavailable) | 56 (55 / 1) |
| Prompt / completion tokens | 3,895,380 / 477,407 |
| Cache read / write tokens | 15,286,520 / 1,851,907 |
| Total tokens | 21,510,581 |
| `cache_hit_rate` (reported) | null (summary, coding-workflows, review_autofix); 0.643385 (binance-blessings) |
| Cache hit rate (recomputed) | 0.727 overall; 0.747 coding-workflows; per-run 0.506–0.838 |
| `codex_calls` / `codex_tokens_used` | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (all) | 9,000 / 13,445,920 (125 samples) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (review_autofix) | 9,427,000 / 15,294,580 (18 samples) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |
| Runner queue wait (26 `log_summary` samples) | median about 64 s, max 272 s |

**Semble, Serena and MCP**

| Metric | Value |
|---|---|
| `semble_query_calls` / `semble_query_bytes` | 9 / 120,691 (about 13.4 KB per query) |
| `semble_sources_total` / `static_dup_bytes` / `echo_lines_dropped` | 56 / 0 / 0 |
| `semble_bootstraps` / failed / unused / total ms | 8 / 0 / 0 / 424,447 (about 53.1 s each) |
| `semble_fallbacks` (contract-test / runtime) | 16 (12 / 4); contract-test in 45047544402, 45082781842, 45058718225; runtime in 45087147825 |
| Serena query / fallback / response bytes / tool calls | 0 / 0 / 0 / 0 |

| MCP target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| Serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` sweep (45086321799) | remaining=1298, used_in_job=71, about 16 min before reset |
| `GH_PAT_BUDGET` deterministic-skip-merge (45088783628) | remaining=4974 |
| `GH_API_RETRY` permanent 404 | 1 visible (45086932105, expected tag-absent probe) |
| HTTP 429 / secondary limit | 0 visible |

**Findings check**

| Status | Count |
|---|---|
| new | 9 |
| recurring | 3 |
| reopened | 1 |
| unverified | 0 |
| dropped: already fixed on this ref | 1 (classifying Semble contract-test vs runtime fallbacks; `semble_contract_test_fallbacks` in `scripts/cost_audit.py`) |
| dropped: previously decided | 2 (restoring the full-log archive, rejected 2026-10-08; deduplicating or skipping self-triggered review runs, rejected 2026-10-07-4 and 2026-09-20) |

Per-repo actions repeat the items above and are not counted twice.

**Data gaps**
- The full run-logs directory `/home/runner/work/_temp/workflow-log-output` was absent. No step logs, no `errors/`, `slow/` or `recent/` folders, and no `summary.json` were read.
- The `log_summary` excerpts are truncated (for example, 1,498,102 characters cut in 45086625947).
- `analysis/recommendation-processing-report.md` was readable, but its last ledger is `analysis/workflow-optimization-2026-10-08.md`. The November reports (2026-11-12 through 2026-11-22) have no recorded verdicts.
- The collection hit the page limit (`page_limit_reached: true`) in three repositories.
- There is no step-level timing for review/autofix compute, no `retrieve` memory telemetry, and no GH API call totals per workflow.