## Executive Summary

- **Fix the cost collector before using its totals for spending decisions.** In five `implement` deep dives, all 130 reported Codex “calls” and 13,261,910 “tokens” matched quoted format examples or script text, not verified usage. Aggregate job logs also repeat OpenRouter step logs: the 24 full-log runs report 64 calls, but contain 32 distinct step-level usage records. **Impact:** prevents materially misleading cost decisions; **confidence: high**.
- **CI failures are concentrated in preventable contract drift, not retries.** Three of 17 CI runs failed: inventory parity in `37102316779`, the clarify-route contract in `37102511377`, and actionlint in `37103943468`. Each ran for 660–1,041 seconds; all 1,000 collected runs were first attempts. Add cheap pre-push checks and clearer failure diagnostics rather than rerunning unchanged work. **Potential impact:** avoid an 11–17-minute failed CI run when caught before dispatch; **confidence: high**.
- **Review is the dominant measured AI critical path.** Run `37102148271` took 2,320 seconds, including 1,262 seconds in `Run reviewer models` and a 300-second check-run wait. Run `37101772623` also exhausted the 300-second wait. Instrument model and pending-check identities before changing reviewer coverage or wait policy. **Potential impact:** up to five minutes per affected run *only if* a waited-for check proves nonessential; **confidence: high** in the timing, low in that conditional saving.
- **MCP fallback totals do not indicate an outage.** The assembled context reports 48 Semble fallbacks, all in CI contract tests, versus zero observed runtime fallbacks; Serena was disabled in inspected AI runs and has no query or probe events. Separate synthetic and production events in dashboards. **Impact:** removes a false rollout alarm; **confidence: high**.
- **Outcome visibility is incomplete.** Of 1,000 runs, 848 were skipped, while only 116 have assembled log telemetry. Successful review runs `37103983037` and `37104147323` warned that standalone validation could not be dispatched for PRs #6121 and #6129. Log skip and dispatch reasons at their upstream decision points. **Impact:** faster diagnosis, not an established failure-rate reduction; **confidence: medium**.

## Speed Optimizations

Ranked by **possible critical-path reduction**, not by a promised saving:

1. **Reviewer model stage — critical path.** `review_autofix` run `37102148271` spent 1,262 of 2,320 seconds in `Run reviewer models`; its step-level logs contain 13 distinct OpenRouter usage records. The stage dominates, but serialization is **not established**. Add `REVIEW_MODEL_TIMING_V1` with run, pass, model, start/end milliseconds, and retry/failback reason. Then parallelize only demonstrably independent calls while retaining the same reviewers and synthesis gate. **Saving:** unknown until per-model timing; bounded above by the 1,262-second stage. **Risk:** medium; revert to current scheduling if verdict parity changes.
2. **CI test execution — critical path when its job is last.** The `Orchestrate poll process unit tests` step took 911 seconds in CI run `37101772277` and 891 seconds in `37102880727`. Keep the full test set but measure test-case durations and split the slowest cases into balanced existing CI shards. **Saving:** at most about 11 minutes for that step under an ideal four-way split; end-to-end saving may be zero if another job remains slower. **Risk:** low–medium; preserve the existing aggregate CI gate.
3. **Check-run wait — conditional critical-path win.** Review runs `37102148271` and `37101772623` each waited 300 seconds and proceeded with one check still queued or in progress. The waiter already excludes its own run; its warning does not identify the remaining check. Emit `CHECK_WAIT_V1` with elapsed time, poll count, final status, pending check names, and self-excluded count from the *existing* snapshots. Only consider an early exit after confirming a remaining check is not required for the decision. **Saving:** 0–300 seconds per affected run, conditional on that proof. **Risk:** low for logging; high for bypassing required checks—retain the current timeout until verified.
4. **Memory-write overhead — investigate before consolidation.** In `implement` run `37101946889`, the start event, success event, and candidate-write steps each took roughly 69 seconds; run `37101353145` also recorded five push attempts for its start event. Log clone, write, push, and backoff milliseconds per operation; reuse job-local preparation only if measurements show repetition, while preserving each write’s fail-open behavior. **Saving:** unquantified; **risk:** medium for write consolidation, low for logging.

The 29-second `Enumerate open PRs and dispatch internal-review.yml` step in sweep run `37104684353` is a **micro-optimization** relative to these stages; its 14 dispatches are legitimate work, not a candidate for removal.

## Cost Optimizations

1. **Repair accounting first; do not infer dollar savings from it.** `scripts/cost_audit.py` matches “tokens used” inside script comments in the five sampled implement runs and counts job-aggregate copies of step-level OpenRouter usage. Parse emitted usage lines only, exclude aggregate job logs or deduplicate on run/step/call, and add a regression fixture containing quoted token examples. The 24 full-log runs’ reported 129,048,364 OpenRouter total tokens correspond to **64,524,182 tokens across distinct step-level records**; actual Codex usage is unknown from this collector. **Spend saving:** none from the fix itself; **quality risk:** none.
2. **Measure reviewer marginal value before changing model selection or reasoning.** Run `37102148271` has 13 distinct reviewer usage records across several models and a 1,262-second reviewer stage. Emit model, reasoning setting, prompt/cache/completion tokens, latency, retry reason, and whether the pass contributed a distinct finding. If one call is *shown* to be redundant, removing it would avoid one of 13 calls in that run; dollars cannot be estimated without prices and attributable call costs. Retain the present reviewer set until verdict-quality checks pass. **Quality risk:** high if pruned prematurely.
3. **Bound low-yield Semble context without assuming it saves prompts.** The assembled totals are 52 `SEMBLE_QUERY` records and 374,302 logged bytes: 40 queries/216,438 bytes in `implement`, 12/157,864 in `review_autofix`. Distinct deep-dive step events include five `overflow` responses below 100 bytes, but logged query bytes are **not** measured prompt tokens. Add selected-chunk count and bytes-actually-inserted to each target’s event; suppress an empty result only after verifying the legacy context path remains available. **Possible saving:** up to those five observed low-yield queries in the sampled steps; token saving unknown. **Quality risk:** low for logging, medium for suppression.
4. **Prevent avoidable CI iterations; preserve effective caching.** The three failed CI runs consumed 660, 1,041, and 664 seconds without a run retry. Local inventory, focused-contract, and actionlint preflight before dispatch can avoid that work. Conversely, assembled review cache hit rate is 89.2%; wholesale prompt or model changes could forfeit cache value. **Token/dollar saving:** unmeasured; log corrected usage before estimating it.

Serena cannot yet be credited with replacing tool or model work: inspected runs show `SERENA_ENABLED: false`, with zero validated queries, response bytes, fallbacks, or probes. Do not enable or optimize it on an assumed return.

## Reliability Improvements

1. **Prevent and diagnose the three distinct CI regressions.** In `tests-release-and-log-analysis / Inventory parity`, run `37102316779` reported an unexpected `scripts/smoke_review_dispatch.sh` in `docs/INVENTORY.md`. In `static-checks / Actionlint`, run `37103943468` rejected three constant-false conditions in `.github/workflows/orchestrate_poll.yml`. In `tests-promote-stall-and-review / Clarify loop guard unit tests`, run `37102511377` surfaced a `CalledProcessError` but not the captured stderr or failing fixture case; the focused test passes in the current checkout, so its historical root cause remains uncertain. Add these checks to the branch preflight, keep CI authoritative, and make the contract print a case identifier, exit status, and bounded redacted stderr—not the entire shell command. **Expected impact:** fewer preventable CI failures and a diagnosable third failure; **rollback:** remove preflight wiring without weakening CI.
2. **Make check-wait fail-open explicit.** The two review runs’ `CHECK_RUNS_WAIT_TIMEOUT` warnings proceeded with one pending check apiece. Record whether the final snapshot was `ready`, `timeout`, or `api_error` and which check blocked, without changing the existing proceed-with-snapshot behavior. **Expected impact:** distinguishes legitimate slow CI from a missing exclusion; **rollback:** logging can be disabled independently.
3. **Expose validation-dispatch gaps.** `Internal: AI Review & Autofix` runs `37103983037` and `37104147323` succeeded while warning that standalone validation could not be dispatched for merged PRs #6121 and #6129. Add `VALIDATION_DISPATCH_V1` with attempted workflow, result, and a bounded reason category, plus an operator-visible follow-up on failure. Do not silently turn the warning into a blocking merge rule. **Expected impact:** reduces unnoticed validation gaps; **rollback/fail-open:** retain current merge behavior.
4. **Classify fallbacks by origin.** All 48 collected `SEMBLE_FALLBACK` records are `target=overflow`, `context=contract-test` in six CI runs; runtime fallbacks are zero. Keep the intentional failure tests, but exclude `context=contract-test` from production availability alerts and emit target/reason on any runtime fallback. No `SERENA_PROBE` failures or runtime fallbacks were observed; with Serena disabled, zeros are **not** proof of availability. **Expected impact:** fewer false alarms without masking a rollout; **rollback:** restore the prior alert query.

Across assembled telemetry, `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero. That is no evidence of current rubric pressure or context-window warnings in covered runs, not proof they never occur.

## AI Memory Health

Eight `AI_MEMORY_TELEMETRY` `retrieve` events in selected implement/review step logs had **8/8 hits** (`records_selected > 0`), selecting 25–30 records each. Mean estimated context was **1,515.6 tokens against a mean 1,525-token budget**: implementation 1,588.4/1,600 across five retrieves; reviewer 1,394.3/1,400 across three. Keyword methods were `plain` 5, `llm` 3, `none` 0. No sampled retrieve selected zero records, reported `enabled: false`, or reported `fail_open: true`; relevance and downstream benefit remain unmeasured. Log selection score and whether retrieved records were used before trimming a near-full budget.

Excluding aggregate job copies, selected steps emitted 18 `record-run-event`, 9 `record-candidate`, 5 each of `processed-command-claim`, `processed-command-complete`, and `finalize-task`, and 8 `retrieve` events. One start-event write in `implement` run `37101353145` took five push attempts; log per-attempt delay and failure category without logging record contents. No `promote` or `compact` events were found in these deep dives; verify emission and sampling coverage before calling either unhealthy. Unselected-run summaries additionally report successful `finalize-task` events in `issue_pr_status` runs `37104147236` and `37103983022`.

## GH API Call Audit

**Actual request totals, endpoint counts, rate-limit events, and retry counts are not emitted in the supplied run telemetry.** Do not treat agent text mentioning `gh api` or rate limits as requests.

- **Review sweep:** `sweep / Enumerate open PRs and dispatch internal-review.yml`, run `37104684353`, processed 16 candidates, dispatched 14, skipped two active runs, and took 29 seconds. `.github/workflows/review_autofix_sweep.yml` already snapshots PRs and the three active statuses for each of two review workflows before the candidate loop. On a single page with no retries, that design implies **at least seven reads plus 14 dispatch requests**; it does *not* establish the actual count. Keep this prefetch and its active-run guard. Add `GH_API_CALL_V1` counters for endpoint family, page count, attempts, latency, and rate-limit response at the existing call sites; avoid new quota-probe requests.
- **Claude PR sweep:** `claude-pr-catch-all / Start Claude fixers for unhandled claude PRs`, run `37104331293`, scanned 14 registered repositories, evaluated 10 candidates, skipped all 10, and took about 16 seconds. `scripts/claude_pr_sweep.py` documents per-candidate PR and active-run reads, but logs do not show which conditional reads occurred. Count calls by candidate and repository first; if repeated active-status reads are confirmed, prefetch once per repository and fall back to the legacy read on a cache miss. **Conditional opportunity:** ten candidates reaching three status reads would mean up to 30 such reads; one three-status snapshot in each of the two repositories visibly containing candidates would use six, a reduction of up to 24—not a measured saving.
- **Check-run polling:** the two 300-second waits log nine and five sleep decisions respectively, but no request or retry totals. Include API attempts and page counts in `CHECK_WAIT_V1` before changing polling cadence; shorter sleeps could increase shared-quota pressure.

These changes follow `CLAUDE.md` §15: reuse fetched data, batch/prefetch where semantics permit, and fail open on cache misses. The sweep’s 14 separate dispatches must remain separately attributable. No rate-limit incident can be confirmed or ruled out from this window.

## Prompt Cache & Memory System

Assembled OpenRouter telemetry reports **126,515,394 cache-read**, **2,253,396 cache-write**, and **13,083,302 prompt tokens**, yielding `cache_hit_rate=0.891882` over 70 reported usage records; all 70 report usage available. Aggregate-log duplication inflates token and call *totals*, although duplicating the same records leaves their ratio unchanged. Sample review runs range from 85.1% (`37102712484`) to 90.9% (`37101772623`).

Keep stable instructions and tool definitions ahead of run-specific PR, memory, and Semble material. Add a non-content prefix hash, breakpoint status, and dynamic-section byte counts to the existing usage event to test whether prefix variance explains the lower-hit run; **fragmentation is a hypothesis, not an observed cause**. Memory’s eight retrieves nearly filled their budgets, so log inserted-versus-selected tokens and relevance before reducing them. Zero covered `CONTEXT_BUDGET_WARN` events means no measured prompt-window pressure; coverage of skipped and unselected runs limits that conclusion. Expected token, latency, and reliability gains from rearranging prefixes remain unquantified until these fields exist.

## Orchestrator Health

The 848 skipped runs are not 848 failures: `clarify` skipped 183/198, `plan` 184/199, `implement` 177/197, and `orchestrate_clarify_respond` 190/197. Many run-level skips have empty log archives, so their gate reasons cannot be counted. Emit an `AI_PHASE_GATE_V1` decision at the *upstream dispatcher* when no job will start, with phase, reason, and outcome; track reason rates rather than treating skip volume as a stall.

The two collected `orchestrate_poll` runs succeeded in 402 and 501 seconds but have no deep-dive logs; wave progression, judge cycles, deferrals, conflict-heal retries, and terminal-state health are therefore **unknown**. Add a compact `ORCH_TRANSITION_V1` on existing state writes: prior/new state, wave, deferred-item count and reason, conflict retry count, judge outcome, and terminal reason. Supporting stall signals are bounded: readiness run `37103943489` had 2/12 unchecked sub-issues on #6031; close-cleanup run `37103674531` preserved four active runs without PR linkage and released none of six examined merge-train entries. Log why each entry is held; continue preserving unlinked active runs rather than canceling them speculatively. Track time-in-state and repeated unchanged poll decisions once those events exist.

## Pipeline Flow Bottlenecks

| Flow segment | Evidence | Bottleneck type and next action |
|---|---|---|
| Clarify → plan | Successful-run medians: 433 seconds across 15 `clarify` successes; 599 seconds across 15 `plan` successes. Most family runs are skipped. | Compute is visible; transition wait and skip reasons are not. Add upstream gate and handoff timestamps before altering dispatch. |
| Implement | Median 1,329.5 seconds across 20 successes; `Run Codex implementation` took 1,313 seconds in run `37101946889`. | Model/agent compute; add per-stage timing and valid token usage before changing reasoning or context. |
| Review/autofix | Family p50/p95 **935/1,632 seconds** across 27 successful runs; run `37102148271` includes 1,262 seconds of reviewers plus a 300-second check wait. | Compute plus bounded polling; instrument separately, then optimize the proven critical component. |
| Validate/CI | CI p50/p95 **951/2,167 seconds**, 3 failures in 17 runs; sampled poll-process test steps took 891–911 seconds. | Parallel-test compute and late contract failures; balance tests and run cheap branch preflight. |
| Orchestrate/merge | Poll runs 402 and 501 seconds; `37103674531` released 0/6 examined merge-train entries. | Queue, state, and conflict overhead cannot be apportioned without transition reasons. Log them before changing release rules. |

Created and run-start timestamps coincide in collected rows, but there is **no trustworthy job queue/wait metric** here. Add queued-to-start and dependency-wait durations; do not infer zero queueing. No run-attempt retries occurred, so the visible retry overhead is internal polling and memory pushes, not GitHub reruns.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** reviewer compute and 300-second check waits (`37102148271`), long implement compute (`37101946889`), and CI poll-process tests (`37101772277`). **Top failures:** three separate inventory, clarify-contract, and actionlint regressions (`37102316779`, `37102511377`, `37103943468`); validation-dispatch warnings on PRs #6121 and #6129. **Highest apparent cost drivers:** review OpenRouter usage and implement Codex usage, but collected totals are duplicated or contaminated and cannot support a dollar ranking.

**Priority actions:**
1. Correct cost-event parsing/deduplication and add fixtures for quoted examples; this makes the next spending decision defensible.
2. Add bounded failure, `CHECK_WAIT_V1`, and `GH_API_CALL_V1` diagnostics, then preflight the three observed CI failure classes before expensive runs.
3. Time reviewer calls and CI test cases individually; change parallelism only where a measured critical-path gain preserves existing verdict and test gates.

## Metrics Appendix

**Window and outcomes** — `2026-10-03 05:45:39–06:58:31 UTC`; one collected repository.

| Scope | Runs | Success | Failure | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All collected runs | 1,000 | 149 (14.9%) | 3 (0.3%) | 848 (84.8%) | 1 / 680 s |
| Non-skipped runs | 152 | 149 | 3 (2.0%) | — | 428.5 / ~1,512 s |
| CI | 17 | 14 | 3 (17.6%) | 0 | 951 / 2,167.2 s |
| Review/autofix | 27 | 27 | 0 | 0 | 935 / 1,632.2 s |

All 1,000 rows have `run_attempt=1` and `retries=0`. The all-run median is dominated by skips; it is not an active pipeline latency baseline.

**Cost and diagnostic coverage** — assembled context unless marked otherwise.

| Metric | Reported value | Interpretation |
|---|---:|---|
| Runs with assembled log telemetry | 116/1,000 | Includes summarized, non-deep-dive runs; collector `summary.json` has 24 full-log runs. |
| Codex calls / tokens | 130 / 13,261,910 | **Invalid usage estimate:** sampled matches are quoted/script text; actual total unknown. |
| OpenRouter calls / prompt / completion / total tokens | 70 / 13,083,302 / 1,047,128 / 142,896,956 | Duplicated aggregate and step logs; 24 full-log runs contain 32 distinct usage records totaling 64,524,182 tokens. |
| OpenRouter cache read / write tokens | 126,515,394 / 2,253,396 | Reported, duplication-affected totals. |
| Usage available / unavailable calls | 70 / 0 | On the reported records, not 70 distinct requests. |
| `cache_hit_rate` | 89.1882% | Reported ratio; selected-run range cited above. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 2,212,080 | 113 assembled samples, including short skips; not model-call latency. |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | Covered logs only. |

**API and MCP signals**

| Workflow or server | Observed signal | Coverage limit |
|---|---|---|
| Review sweep `37104684353` | 16 candidates; 14 dispatched; 2 active skips; 29-second enumeration/dispatch step | ≥21 requests implied by code on one-page, no-retry assumptions; actual calls and rate-limit events unmeasured. |
| Claude sweep `37104331293` | 14 repositories scanned; 10 candidates skipped; 0 queued; ~16-second decision step | Conditional per-candidate API calls unmeasured; collector scope covers only this repository. |
| Semble | 52 reported queries; 374,302 logged bytes; 48 fallbacks, **all contract-test**; 0 runtime fallbacks | Full-log aggregate copies inflate event totals: 25 distinct step-level queries/172,100 bytes in deep dives. Query bytes are not prompt tokens. |
| Serena | 0 queries, tool calls, response bytes, fallbacks, and probes | Disabled in inspected AI runs; no replacement-efficiency or availability conclusion. |
| Other MCP servers observed | None with validated `<NAME>_QUERY`, `_FALLBACK`, or `_PROBE` fields | Unknown-server coverage remains limited to collected logs and summaries. |

| MCP target observed in deep-dive steps | Distinct queries | Logged bytes | Collector-classified fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|---:|
| Semble `overflow` | 20 | 108,219 | 48 reported synthetic contract-test events across CI; 0 runtime | 0 / 0 / 0 |
| Semble `reviewer-context` | 4 | 55,866 | 0 | 0 / 0 / 0 |
| Semble `conflict-resolver-context` | 1 | 8,015 | 0 | 0 / 0 / 0 |
| Serena target not observed | 0 | 0 | 0 | 0 / 0 / 0 |

No probe line was emitted for those targets; **0/0/0 denotes absent observations, not successful availability checks**. The distinct Semble rows cover deep dives only; the assembled context adds two reported queries/30,102 bytes from a summarized review run whose target cannot be verified here.

## Deep Audit — Workflows & Scripts (2026-10-03)

### Section 1: Bug & Correctness Sweep

The read-only sweep parsed all 52 workflow YAML files, ran `bash -n` on all 97 shell scripts, and compiled all 63 Python scripts in memory; those checks found no syntax failures. It did not establish runtime correctness. The existing report’s cost-collector and check-wait findings are not repeated here.

- **SEC-001 — Critical · security · `scripts/review_merge_train.sh:415-424`** (also `.github/workflows/review_autofix.yml:6424-6467,6644-6684`). **Description:** Merge-train release dispatches a review workflow with `--ref "${head}"`; both autofix redispatch blocks likewise use `--ref "${TARGET_BRANCH}"`. In this repository, those refs can be unmerged PR branches, while `internal-review.yml:37-40,59-73` grants write permissions and passes secrets to the reusable review. **Inference:** if an unmerged branch changes its workflow, dispatch can execute that branch’s workflow definition with privileged credentials. The sweep explicitly avoids this boundary at `.github/workflows/review_autofix_sweep.yml:325-332`. **[NEEDS VERIFICATION]** for which actors can write each eligible branch. **Recommended fix:** Dispatch the trusted default-branch workflow and pass the PR number as an input, following the sweep; update active-run matching to recognize `internal-review.yml:1-8`’s PR-scoped run name.

- **BUG-001 — High · bug · `.github/workflows/review_autofix_sweep.yml:177-245`**. **Description:** Each active-status fetch ends in `|| true`. A failed fetch can therefore produce a valid-looking, incomplete `active` snapshot; the candidate loop interprets a missing key as zero at lines 297-311 and may dispatch a second review. **Recommended fix:** Track success separately for all six workflow/status reads. On any incomplete snapshot, warn and skip dispatch for that tick—or perform a bounded PR-specific fallback lookup—rather than treating absence as evidence of inactivity.

- **BUG-002 — High · bug · `scripts/review_merge_train.sh:407-412,429-452`**. **Description:** The release dedup guard reads only `actions/runs?per_page=100`, without pagination or status filtering, then treats no matching branch as no active review. Active reviews outside the latest 100 *all-workflow* runs are invisible; an API failure likewise clears the guard and permits dispatch. The missed-run outcome is an inference, **[NEEDS VERIFICATION]** against a busy-run fixture. **Recommended fix:** Fetch paginated queued, pending, and in-progress snapshots as the sweep does, preserve an explicit “snapshot unavailable” state, and leave PRs queued when that state prevents a safe dispatch decision.

- **BUG-003 — High · bug · `scripts/review_rb_judge.sh:1641-1658`**. **Description:** The pre-action merged-PR refresh deliberately addresses a race during judging, but `gh_retry _safe_gh_jq ... || echo '{}'` converts a failed refresh into `merged=false`. If a PR merged after the earlier check, `fix` or `close_and_reissue` can pass the guard; the latter closes and relabels linked work at lines 2503-2515. **Recommended fix:** For those two destructive actions, require a successful refresh containing a recognized PR state and merged indicator; otherwise skip with a diagnostic. Do not replace the separate judged-head merge constraint.

- **BUG-004 — Medium · bug · `scripts/review_merge_train.sh:273-292`**. **Description:** `_mt_upsert_comment` swallows both PATCH and POST failures and returns success. A queued label can consequently persist without its explanatory marker, while callers cannot report the failed write. **Recommended fix:** Return the write status and let callers log a bounded warning; keep the label decision independent where the documented fail-open policy requires it. Coordinate with API-001’s lookup change.

### Section 2: GitHub API Call Redundancy Audit

Counts below are **code-path estimates per successful, single-page request**, not measured request telemetry. The existing report already covers the Claude PR sweep and cost accounting.

- **API-001 — Medium · api-redundancy · `scripts/review_merge_train.sh:256-262,276-288`**. **Description:** For an existing marker, `_mt_find_marker_comment_id` lists comments but emits only the ID; `_mt_upsert_comment` then fetches that comment’s body. Current: **2 GETs per existing marker**; proposed: **1 GET**, with the same conditional PATCH. **Recommended fix:** Return the selected ID *and body* from the comments response through an output-variable helper, extending `_mt_pr_files_into`’s same-shell cache pattern at lines 115-137. Retain a single-comment fallback on incomplete data.

- **BATCH-001 — Medium · api-batching · `scripts/review_merge_train.sh:124-137,203-231`**. **Description:** `_mt_blockers_for_into` calls `GET /pulls/{n}/files` inside its older-PR loop, up to `MERGE_TRAIN_MAX_OLDER_PRS` distinct PRs. With **N ≤ 20** and one file page each, the list-plus-files path is **1 + N calls**—up to **21**—versus a projected **1 + ceil(N/25)**—**2** at N=20—with aliased PR-file connections. **Recommended fix:** Add a batched prefetch that fills the existing `_MT_FILES_CACHE`; fall back to `_mt_pr_files_into` for missing entries or additional pages. Verify filename and pagination parity before replacing REST. **[NEEDS VERIFICATION]**

- **BATCH-002 — Medium · api-batching · `scripts/review_resolve_review_threads.sh:259-267,297-339`**. **Description:** After a paginated thread read, the plan loop sends one GraphQL `resolveReviewThread` mutation per eligible thread. For **N** eligible threads and **I** required rationale replies, a one-page path uses **1 + N + I calls**; batching 25 independent mutation aliases projects **1 + ceil(N/25) + I**—at the 50-thread cap, **51 + I → 3 + I**. Replies must remain ordered before their resolutions. **Recommended fix:** Extend the aliased-query construction pattern of `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh:14717-14793`; validate every alias’s returned thread ID and state, with per-thread fallback on partial errors. Mutation failure semantics require verification. **[NEEDS VERIFICATION]**

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001 — Medium · duplication · `.github/workflows/mark-stable.yml:698-847`** (also `.github/workflows/test-and-mark-stable.yml:5893-6042`). **Description:** Both release steps carry the same large inline `publish_tag_with_remote_verification` routine and tag-publication body. A correction to remote verification must be made twice. **Recommended fix:** Put `publish_tag_with_remote_verification(tag_ref, publication_mode)` in a shared `scripts/release_tag_helpers.sh`, source it from both steps, and retain each workflow’s existing gate and outputs. Do not substitute `scripts/mark-stable.sh` wholesale: its CLI contract is different.

- **DUP-002 — Medium · duplication · `.github/workflows/review_autofix.yml:6424-6468`** (also lines 6644-6685). **Description:** The post-commit and editor-changes-lost paths repeat the direct/caller/fallback workflow-dispatch sequence, including its ref selection. **Recommended fix:** After SEC-001’s trusted-ref correction, move dispatch selection into a verified support script exposing `dispatch_review_for_pr(pr_number, allow_workflow_edits, caller_workflow, trusted_ref)`. Keep each caller’s distinct peer check, retry budget, and output handling in its current step; register the new script in `scripts/stage_workflow_support.sh:53`.

### Section 4: Expression Size Limit Risk Assessment

The scan measured the parsed YAML value—not indentation in the source file—of all **225 interpolated `run:` blocks**. Exactly one literal body exceeds 15,000 characters; none exceeds 18,000. Runtime interpolation lengths are not knowable from the static file. The longest parsed `if:` is 859 characters; no workflow exceeds 800 KB. The repository also documents a *stricter* 512,000-byte operational limit and 480,000-byte CI guard at `tests/test_workflow_file_size_limit.py:26-27`, rather than relying on the prompt’s 1 MB criterion.

- **EXPR-001 — Medium · expression-limit · `.github/workflows/implement.yml:986-1342`**. **Description:** “Stage workflow support files” contains three `${{ }}` interpolations in a parsed `run:` body of approximately **16,985 characters**, leaving **4,015** below 21,000 before runtime expansion. That headroom is a static estimate. **[NEEDS VERIFICATION]** **Recommended fix:** Extract the body into a staged `scripts/` helper, pass the three expression values through step `env:`, and add the helper to the bootstrap registry and relevant contract tests.

- **DEBT-001 — Medium · tech-debt · `.github/workflows/review_autofix.yml:1-7561`**. **Description:** The file is **458,436 bytes**, only **21,564 bytes** below this repository’s 480,000-byte CI guard, although it is nowhere near 800 KB. **Recommended fix:** Before the next substantial addition, extract another large inline step using the existing verified `review_autofix_step_*.sh` staging pattern documented in `agents.md:624-675`; retain the guard.

### Section 5: Cross-Cutting Concerns

- **CONSIST-001 — Medium · consistency · `scripts/orchestrate_poll_process.sh:14787-14803`** (also lines 14929-14946). **Description:** Both batched GraphQL helpers transform an HTTP-success response without checking its `.errors` array. Defaults such as `.data.repository // {}` can make a partial GraphQL response appear to be an ordinary cache miss; `scripts/gh_helpers.sh:897-925` explicitly checks errors and connection completeness for a comparable read. The effect of a particular partial response is an inference. **[NEEDS VERIFICATION]** **Recommended fix:** Validate `.errors` and required nodes per batch, warn with a bounded reason, and leave affected keys unavailable for the existing safe fallback.

- **DEAD-001 — Low · dead-code · `scripts/review_rb_judge.sh:2121-2140`**. **Description:** `PR_HEAD_SHA` is initialized and assigned in the mergeability poll but never read elsewhere in this script; shellcheck reports SC2034. Its adjacent comment claims it binds the eventual merge to a head, which the unused assignment cannot do. **Recommended fix:** Remove the assignment and misleading comment; retain the existing `RB_JUDGED_HEAD_SHA`-bound merge calls rather than switching them to a newly polled SHA.

No `TODO`, `FIXME`, or `HACK` marker was found in the scoped workflow and script files. The redirection and subshell shellcheck warnings inspected in `review_conflict_resolve.sh:2952-2962`, `review_commit_changes.sh:263-271`, and `resolve_integration_ref.sh:114-125` did not establish additional defects. Shellcheck timed out on the unusually large `orchestrate_poll_process.sh`; syntax checking completed for that file.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 1 | SEC-001 |
| High | 3 | BUG-001, BUG-002, BUG-003 |
| Medium | 9 | BUG-004, API-001, BATCH-001, BATCH-002, DUP-001, DUP-002, EXPR-001, DEBT-001, CONSIST-001 |
| Low | 1 | DEAD-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | 4 implementation files plus focused tests | Large |
| API call optimization | 2 scripts plus focused tests | Medium |
| Code modularization | 3 workflows, 2 shared modules, and staging/tests | Large |
| Expression size reduction | `implement.yml`, 1 new script, staging/tests | Medium |
| Medium/Low fixes | 4 existing files plus focused tests; overlaps rows above | Medium |

## API Call Consolidation & Dead-Call Analysis (2026-10-03)

### Safety Tag Legend

`SAFE_TO_MERGE` is authorized for implementation; `NEEDS_VERIFICATION` requires the specified checks first; `RISKY_SKIP` must not be auto-implemented because a stated safety trigger applies.

### Consolidation Candidates (MERGE-###)

- **MERGE-001 — `RISKY_SKIP`** · `scripts/orchestrate_poll_process.sh:10441-10442`, in `finalize_integration_merge_if_needed`. **Current → proposed:** 2 → 1 GET when the recorded PR is absent from the snapshot. **Endpoint:** `GET /repos/{owner}/{repo}/pulls/{final_pr}`. **Evidence:** Adjacent calls fetch `.state` and `.merged_at != null` from the same PR; both values are consumed at line 10444. **Proposed fix:** If manually approved, fetch one PR object and derive both fields from it, retaining the existing `final_pr_json_snapshot` path. **Safety rationale:** `RISKY_SKIP` is mandatory because this orchestrator path explicitly rechecks whether a final PR merged while integration may have advanced; the two currently independent failed reads also have distinct empty-value outcomes. **Downstream signal:** Do not auto-implement; manually review snapshot freshness and each API-failure outcome against the final-merge race before changing the calls.

- **MERGE-002 — `RISKY_SKIP`** · `scripts/gh_helpers.sh:1264-1273,1384-1393`; shared caller `.github/workflows/review_autofix.yml:6613-6639`. **Current → proposed:** 2 → 1 GET on the editor-changes-lost path when both probes run. **Endpoint:** `GET /repos/{owner}/{repo}/actions/runs?branch={head_branch}&per_page=30`. **Evidence:** `autofix_retrigger_has_inflight_peer` and `autofix_changes_lost_head_retry_consumed` request the same branch-scoped snapshot, then inspect different statuses. The peer probe fails open; the retry-budget probe fails closed. **Proposed fix:** Only after manual approval, let both helpers evaluate a shared snapshot while preserving their separate failure decisions and `AUTOFIX_PEER_CHECK` / `AUTOFIX_CHANGES_LOST_BUDGET` logs. **Safety rationale:** `RISKY_SKIP` is mandatory because these are dispatch-race guards: a run can change status between reads, and sharing a failed read must not turn the retry-budget guard into fail-open behavior. **Downstream signal:** Do not auto-implement; manually review status-transition races, partial/API failures, and log-key parity.

- **MERGE-003 — `RISKY_SKIP`** · `.github/workflows/clarify.yml:587,590-606`, step “Fetch issue comments.” **Current → proposed:** 2 → 1 GET for a successful, single-page semantic-cache fetch; retain the prompt-read fallback on failure. **Endpoint:** `GET /repos/{owner}/{repo}/issues/{issue}/comments`, ascending by creation time. **Evidence:** The required prompt read obtains the first 50 comments; when semantic caching is enabled, a paginated read fetches the full history, including those comments. **Proposed fix:** In the cache-enabled branch, derive `ISSUE_COMMENTS_FILE`’s first 50 entries and `THREAD_HISTORY_FILE` from one successful full-history response; preserve the existing required prompt read if that response fails. **Safety rationale:** `RISKY_SKIP` is mandatory because the second call implements pagination, uses a different page size, and has an optional failure path whereas the first read is required. **Downstream signal:** Do not auto-implement; manually verify first-50 ordering across page boundaries and that cache failure still leaves prompt context available while bypassing the cache.

### Redundant Re-Fetch (REUSE-###)

No findings.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — the comments-list call is paginated; the fourth-argument caller at `scripts/review_merge_train.sh:359-392` also needs its ID-and-body handoff reviewed.
- BATCH-001: `RISKY_SKIP` — the existing PR-files reads are paginated; verify page completeness and preservation of `_MT_FILES_CACHE` fallback behavior before replacing them.
- BATCH-002: `NEEDS_VERIFICATION` — the paginated thread read can remain intact, but aliased mutations need per-thread result, reply-order, and partial-error verification.

### Summary Counts

Counts cover **net-new findings only**; Deep Audit cross-references are excluded.

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 0 | — |
| RISKY_SKIP | 3 | MERGE-001, MERGE-002, MERGE-003 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
